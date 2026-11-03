"""Import aus WISO Steuer (Paket G): wiederholbar, mit Abgleich bei jedem Lauf.

Alle Faelle laufen auf einer ERFUNDENEN Datei im exakten Format des
WISO-Exports „Buchungen“ (``tests/wiso_beispiel.py``): Erstimport, zweiter
Import identisch (nichts neu), zweiter Import mit neuen, geaenderten und
geloeschten Buchungen, Verknuepfen mit vorhandenen Eintraegen, Datei
nachtraeglich anhaengen (je Beleg und ueber den Eingang), Einstufung je
Zahler und § 19-Zaehler, Anlagegut mit abgeleiteter Nutzungsdauer,
„Angaben fehlen“, Privat, Erstattung, EUeR-Summen gleich WISO-Summen,
Sicherheit der Datei, Excel und Sicherung.
"""

from __future__ import annotations

import os
import re
import sqlite3
import zipfile
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import (
    angaben,
    anlagen,
    backup,
    einnahmen,
    euer,
    export_excel,
    grenzwert,
    kategorien,
    wiso,
)
from app.logik.beleg import anhaengen, loeschen, pipeline, sammelbuchung
from app.logik.beleg.ergebnis import BelegFehler
from app.main import erzeuge_app
from tests import test_belegordner as bo
from tests import wiso_beispiel as b

HEUTE = date(2026, 10, 27)
NAME = "20261027_Buchungen.csv"
JAHR = 2026


# ------------------------------------------------------------------ Helfer

def anzahl(conn, sql: str, *werte) -> int:
    return conn.execute(sql, werte).fetchone()[0]


def kat(conn, schluessel: str) -> int:
    return kategorien.nach_schluessel(conn, schluessel)["id"]


def lesen(buchungen=b.BUCHUNGEN, *, inhalt: bytes | None = None, name: str = NAME):
    return wiso.lies(name, b.datei(buchungen) if inhalt is None else inhalt, heute=HEUTE)


def planen(conn, buchungen=b.BUCHUNGEN, auswahl: wiso.Auswahl | None = None) -> wiso.Plan:
    return wiso.plane(conn, lesen(buchungen), auswahl, heute=HEUTE)


def einspielen(conn, ordner: Path, buchungen=b.BUCHUNGEN, *,
               auswahl: wiso.Auswahl | None = None) -> tuple[wiso.Plan, wiso.Ergebnis]:
    """Vorschau und Uebernahme -- wie die beiden Klicks auf der Seite."""
    auswahl = auswahl or wiso.Auswahl()
    inhalt = b.datei(buchungen)
    plan = wiso.plane(conn, wiso.lies(NAME, inhalt, heute=HEUTE), auswahl, heute=HEUTE)
    ergebnis = wiso.uebernehmen(conn, ordner, NAME, inhalt, fingerabdruck=plan.fingerabdruck,
                                auswahl=auswahl, heute=HEUTE)
    return plan, ergebnis


def aktiv(conn, nummer: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM wiso_buchung WHERE jahr = ? AND buchungsnummer = ?"
                        " AND status = 'AKTIV'", (JAHR, str(nummer))).fetchone()


def posten(plan: wiso.Plan, nummer: int) -> wiso.Posten:
    return next(p for p in plan.posten if p.zeile.buchungsnummer == str(nummer))


def summe_einnahmen(a) -> int:
    return sum(z.betrag_cent for z in a.einnahmen if not z.nachrichtlich)


def summe_ausgaben_ohne_afa(a) -> int:
    return sum(z.betrag_cent for z in a.ausgaben if z.kennzahl != anlagen.KZ_BEWEGLICH)


def afa_zeile(a) -> int:
    return sum(z.betrag_cent for z in a.ausgaben if z.kennzahl == anlagen.KZ_BEWEGLICH)


@pytest.fixture
def ordner(tmp_path) -> Path:
    return tmp_path


@pytest.fixture
def importiert(conn, betrieb, ordner):
    """Der Erstimport der Beispieldatei ohne Entscheidungen."""
    return einspielen(conn, ordner)


#: Die geaenderte Folgedatei: Buchung 1 mit anderem Betrag, Buchung 2 in WISO
#: geloescht, Buchungen 18 und 19 neu.
NEU_18 = b.Buchung(18, "10.06.2026", "Beispiel Hosting Server", ("4900", "Sonstige betriebliche "
                   "Aufwendungen"), soll="29,75")
NEU_19 = b.Buchung(19, "15.06.2026", "Udemy Auszahlung", b.ERLOES, haben="33,00")


def folgedatei() -> list[b.Buchung]:
    return [*(replace(z, soll="13,99") if z.nummer == 1 else z
              for z in b.BUCHUNGEN if z.nummer != 2), NEU_18, NEU_19]


# ------------------------------------------------------------------ Lesen

class TestLesen:
    def test_beispieldatei(self):
        datei = lesen()
        assert (len(datei.zeilen), datei.jahr, datei.kodierung, datei.fehler) == (
            17, 2026, "UTF-8", [])
        erste = datei.zeilen[0]
        assert (erste.schluessel, erste.datum, erste.konto, erste.gegenkonto,
                erste.soll_cent, erste.haben_cent) == ("1-1", "2026-01-05", "4900", "1200",
                                                       1299, 0)

    def test_windows_1252_ohne_bom(self):
        inhalt = b.text().lstrip("\ufeff").encode("cp1252")
        datei = lesen(inhalt=inhalt)
        assert datei.kodierung == "Windows-1252"
        assert any(z.kontobezeichnung.endswith("Wirtschaftsgüter") for z in datei.zeilen)
        assert [z.pruefsumme() for z in datei.zeilen] == [z.pruefsumme() for z in
                                                          lesen().zeilen]

    @pytest.mark.parametrize(("name", "inhalt", "meldung"), [
        ("buchungen.xlsx", b.datei(), "Endung .csv"),
        ("buchungen.csv", b"", "leer"),
        ("buchungen.csv", b"PK\x03\x04" + b"x" * 200, "Excel-Datei"),
        ("buchungen.csv", b"\xd0\xcf\x11\xe0" + b"x" * 200, "Excel-Datei"),
        ("buchungen.csv", b"a;b\x00;c\r\n", "Nullzeichen"),
        ("buchungen.csv", b"Datum;Betrag\r\n01.01.2026;1,00\r\n", "kein WISO-Export"),
        ("buchungen.csv", (b.KOPF + "\r\n").encode(), "keine lesbare Buchung"),
    ])
    def test_unbrauchbare_datei(self, name, inhalt, meldung):
        with pytest.raises(wiso.ImportFehler, match=meldung):
            wiso.lies(name, inhalt, heute=HEUTE)

    def test_zu_gross(self):
        with pytest.raises(wiso.ImportFehler, match="zu groß"):
            wiso.lies(NAME, b"x" * (wiso.MAX_BYTES + 1), heute=HEUTE)

    def test_kaputte_zeile_haelt_die_anderen_nicht_auf(self):
        kaputt = [
            *b.BUCHUNGEN,
            b.Buchung(20, "01.02.2026", "Klingel\x07zeichen", ("4900", "Sonstige"), soll="1,00"),
            b.Buchung(21, "01.02.2026", "Beides", ("4900", "Sonstige"), soll="1,00",
                      haben="1,00"),
            b.Buchung(22, "31.02.2026", "Kein Tag", ("4900", "Sonstige"), soll="1,00"),
            b.Buchung(23, "01.02.2026", "Zu genau", ("4900", "Sonstige"), soll="1,001"),
            b.BUCHUNGEN[0],
        ]
        datei = lesen(kaputt)
        assert len(datei.zeilen) == 17
        gruende = " | ".join(g for _, g in datei.fehler)
        assert len(datei.fehler) == 5
        for teil in ("Steuerzeichen", "Soll UND Haben", "Zahlungsdatum", "zwei Nachkommastellen",
                     "steht zweimal"):
            assert teil in gruende

    def test_dateiname_ohne_pfad(self):
        assert wiso.lies(r"C:\Users\Beispiel\..\x.csv", b.datei(), heute=HEUTE).name == "x.csv"


# ---------------------------------------------------------------- Erstimport

class TestErstimport:
    def test_vorschau_schreibt_nichts(self, conn, betrieb):
        vorher = anzahl(conn, "SELECT COUNT(*) FROM journal")
        plan = planen(conn)
        assert anzahl(conn, "SELECT COUNT(*) FROM journal") == vorher
        assert plan.anzahl == {"neu": 13, "verknuepfen": 0, "offen": 1, "bekannt": 0,
                               "geaendert": 0, "in_wiso_geloescht": 0, "umgebucht": 0,
                               "privat": 1, "afa": 2,
                               "nicht_uebernehmbar": 0, "unlesbar": 0}
        assert posten(plan, 9).gruppe == wiso.PRIVAT
        assert {posten(plan, n).gruppe for n in (11, 12)} == {wiso.AFA}

    def test_erstimport(self, conn, betrieb, ordner):
        _, ergebnis = einspielen(conn, ordner)
        assert (ergebnis.belege, ergebnis.einnahmen, ergebnis.anlagegueter, ergebnis.spaeter,
                ergebnis.angaben_fehlen) == (7, 6, 1, 1, 2)
        assert ergebnis.zusammenfassung().startswith("13 Buchungen übernommen")
        assert anzahl(conn, "SELECT COUNT(*) FROM v_beleg_gueltig") == 7
        assert anzahl(conn, "SELECT COUNT(*) FROM v_einnahme_gueltig") == 6
        assert anzahl(conn, "SELECT COUNT(*) FROM wiso_buchung WHERE status = 'AKTIV'") == 13
        eintrag = conn.execute("SELECT * FROM wiso_import").fetchone()
        assert (eintrag["jahr"], eintrag["dateiname"], eintrag["zeilen"]) == (2026, NAME, 17)
        # Die Datei liegt schreibgeschuetzt mit ihrer Pruefsumme im Datenordner.
        abgelegt = ordner / eintrag["pfad"]
        assert eintrag["pfad"].startswith("importe/wiso/2026/")
        assert abgelegt.read_bytes() == b.datei()
        assert os.stat(abgelegt).st_mode & 0o222 == 0
        assert eintrag["sha256"] == wiso.pruefsumme(b.datei())
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'WISO_IMPORT'") == 1

    def test_ausgaben_sind_belege_ohne_datei(self, conn, importiert):
        beleg_id = aktiv(conn, 2)["beleg_id"]
        beleg = conn.execute("SELECT b.*, l.name AS lieferant, l.notiz AS lieferant_notiz"
                             " FROM beleg b JOIN lieferant l ON l.id = b.lieferant_id"
                             " WHERE b.id = ?", (beleg_id,)).fetchone()
        assert (beleg["belegdatum"], beleg["betrag_brutto_cent"], beleg["kategorie_id"]) == (
            "2026-01-12", 12900, kat(conn, "FORTBILDUNG"))
        assert beleg["geprueft_am"] is not None
        assert beleg["notiz"].startswith("Aus WISO übernommen (Buchung 2/2026, Konto 4945")
        assert beleg["lieferant"] == "Mustermesse 2026"
        assert "WISO" in beleg["lieferant_notiz"]
        assert [tuple(z) for z in conn.execute(
            "SELECT datum, betrag_cent FROM zahlung WHERE beleg_id = ?", (beleg_id,))] == [
            ("2026-01-12", -12900)]
        assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?", beleg_id) == 0
        assert "WISO" in conn.execute("SELECT verfahren FROM extraktion WHERE beleg_id = ?",
                                      (beleg_id,)).fetchone()[0]
        assert anhaengen.ohne_datei_aus_wiso(conn) == 7

    def test_privat_wird_uebersprungen(self, conn, importiert):
        assert aktiv(conn, 9) is None
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg WHERE betrag_brutto_cent = ?",
                      b.PRIVAT_CENT) == 0
        assert anzahl(conn, "SELECT COUNT(*) FROM einnahme WHERE betrag_cent = ?",
                      b.PRIVAT_CENT) == 0

    def test_erstattung_ist_einnahme_ohne_umsatz(self, conn, importiert):
        einnahme = conn.execute("SELECT e.*, k.schluessel FROM einnahme e JOIN euer_kategorie k"
                                " ON k.id = e.kategorie_id WHERE e.id = ?",
                                (aktiv(conn, 8)["einnahme_id"],)).fetchone()
        assert (einnahme["betrag_cent"], einnahme["schluessel"], einnahme["herkunft"]) == (
            b.ERSTATTUNG_CENT, "EINNAHME_ERSTATTUNG", "WISO")
        a = euer.auswertung(conn, JAHR)
        zeile = next(z for z in a.einnahmen if z.bezeichnung.startswith("Erstattung"))
        assert (zeile.kennzahl, zeile.betrag_cent) == (111, b.ERSTATTUNG_CENT)

    def test_einstufung_je_zahler_und_19(self, conn, importiert):
        stufen = {z["zahler"]: z["schluessel"] for z in conn.execute(
            "SELECT e.zahler, k.schluessel FROM einnahme e JOIN euer_kategorie k"
            " ON k.id = e.kategorie_id")}
        assert stufen == {
            "Beispiel": "EINNAHME_HONORAR_INLAND",
            "Amazon": "EINNAHME_HONORAR_AUSLAND",
            "YouTube / Google": "EINNAHME_HONORAR_AUSLAND",
            "Rad Beispiel": "EINNAHME_ANLAGENVERKAUF",
            "Beispiel Newsletter": "EINNAHME_ERSTATTUNG",
        }
        # Zwei Schreibweisen ("Youtube Ads", "YouTube Ads") -- ein Zahler.
        assert sorted(z["schluessel"] for z in einnahmen.zahler_liste(conn)) == [
            "amazon", "beispiel", "radbeispiel", "youtubegoogle"]
        # § 19 zaehlt nur das Inland; Ausland und Erstattung stehen nachrichtlich
        # in KZ 119 (KZ 111 minus KZ 119 ist der § 19-Umsatz).
        assert grenzwert.vereinnahmt_cent(conn, JAHR) == 25000
        a = euer.auswertung(conn, JAHR)
        assert [z.betrag_cent for z in a.einnahmen if z.nachrichtlich] == [
            12050 + 8000 + 9525 + b.ERSTATTUNG_CENT]

    def test_anlagegut_mit_abgeleiteter_nutzungsdauer(self, conn, importiert):
        gut = conn.execute("SELECT * FROM anlagegut WHERE id = ?",
                           (aktiv(conn, 10)["anlagegut_id"],)).fetchone()
        assert (gut["bezeichnung"], gut["anschaffung_am"], gut["ak_brutto_cent"],
                gut["nutzungsdauer_jahre"], gut["methode"]) == (
            "Plotter Beispiel", "2026-04-15", b.ANLAGE_CENT, 3, "LINEAR")
        assert "aus WISO abgeleitet" in gut["notiz"]
        assert conn.execute("SELECT ist_anlagegut FROM beleg WHERE id = ?",
                            (gut["beleg_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT afa_cent FROM anlagegut_afa_jahr WHERE anlagegut_id = ?"
                            " AND jahr = ?", (gut["id"], JAHR)).fetchone()[0] == 30000
        assert anlagen.afa_summe(conn, JAHR) == {anlagen.KZ_BEWEGLICH: 30000}

    def test_software_unter_gwg_wird_software_kauf(self, conn, importiert):
        assert conn.execute("SELECT kategorie_id FROM beleg WHERE id = ?",
                            (aktiv(conn, 13)["beleg_id"],)).fetchone()[0] \
            == kat(conn, "SOFTWARE_KAUF")

    def test_konto_ohne_vorgabe_wartet(self, conn, importiert):
        assert aktiv(conn, 17) is None
        offen = angaben.offene(conn, "KONTO")
        assert [(a.schluessel, a.angaben["cent"]) for a in offen] == [("4500", 4500)]

    def test_euer_summen_gleich_wiso_summen(self, conn, importiert, ordner):
        """Ohne Privat und ohne AfA: dieselben Summen wie die WISO-Konten."""
        a = euer.auswertung(conn, JAHR)
        assert summe_einnahmen(a) == b.EINNAHMEN_CENT + b.ERSTATTUNG_CENT
        assert summe_ausgaben_ohne_afa(a) == b.AUSGABEN_BEKANNT_CENT
        assert afa_zeile(a) == 30000
        abgleich = wiso.abgleich(conn, ordner, JAHR)
        ungleich = {(z.bereich, z.bezeichnung): z.differenz_cent
                    for z in abgleich.zeilen if z.differenz_cent}
        assert ungleich == {("Ausgaben", "ohne Kategorie"): -b.AUSGABEN_UNBEKANNT_CENT,
                            ("AfA", "Abschreibungen (AfA)"): -40000}
        afa = next(z for z in abgleich.zeilen if z.bereich == "AfA")
        assert afa.gruende == ["Kamera Beispiel: Angaben fehlen — im Tool noch keine AfA "
                               "(WISO: 400,00 €)"]
        assert abgleich.privat_cent == b.PRIVAT_CENT


# ------------------------------------------------------------ Zweiter Import

class TestZweiterImport:
    def test_identisch_nichts_neu(self, conn, importiert, ordner):
        stand = [anzahl(conn, f"SELECT COUNT(*) FROM {t}")
                 for t in ("beleg", "einnahme", "anlagegut", "lieferant", "fehlende_angabe")]
        plan, ergebnis = einspielen(conn, ordner)
        assert plan.anzahl["neu"] == 0 and plan.anzahl["bekannt"] == 13
        assert (ergebnis.neu, ergebnis.anlagegueter, ergebnis.angaben_fehlen) == (0, 0, 0)
        assert [anzahl(conn, f"SELECT COUNT(*) FROM {t}")
                for t in ("beleg", "einnahme", "anlagegut", "lieferant",
                          "fehlende_angabe")] == stand
        assert anzahl(conn, "SELECT COUNT(*) FROM wiso_import") == 2
        # Dieselbe Datei liegt nur einmal im Archiv.
        assert len(list((ordner / "importe" / "wiso" / "2026").iterdir())) == 1

    def test_neue_geaenderte_und_geloeschte_buchung(self, conn, importiert, ordner):
        alt_1, alt_2 = aktiv(conn, 1), aktiv(conn, 2)
        plan = planen(conn, folgedatei())
        assert {k: plan.anzahl[k] for k in ("neu", "geaendert", "in_wiso_geloescht",
                                             "bekannt", "offen")} == {
            "neu": 2, "geaendert": 1, "in_wiso_geloescht": 1, "bekannt": 11, "offen": 1}
        assert [z["id"] for z in plan.geloescht] == [alt_2["id"]]
        auswahl = wiso.Auswahl(aktionen={"1-1": wiso.UEBERNEHMEN},
                               geloescht={alt_2["id"]: wiso.LOESCHEN})
        _, ergebnis = einspielen(conn, ordner, folgedatei(), auswahl=auswahl)
        assert (ergebnis.belege, ergebnis.einnahmen, ergebnis.ersetzt, ergebnis.geloescht) == (
            2, 1, 1, 1)
        # Geaendert: der alte Beleg traegt die Loeschmarkierung, ein neuer entsteht.
        alt_beleg = conn.execute("SELECT * FROM beleg WHERE id = ?",
                                 (alt_1["beleg_id"],)).fetchone()
        assert alt_beleg["geloescht_am"] is not None
        assert "in WISO geändert" in alt_beleg["geloescht_grund"]
        neu_1 = aktiv(conn, 1)
        assert neu_1["beleg_id"] != alt_1["beleg_id"]
        assert conn.execute("SELECT betrag_brutto_cent FROM beleg WHERE id = ?",
                            (neu_1["beleg_id"],)).fetchone()[0] == 1399
        assert conn.execute("SELECT status FROM wiso_buchung WHERE id = ?",
                            (alt_1["id"],)).fetchone()[0] == "ERSETZT"
        # Geloescht: Beleg markiert, WISO-Zeile GELOESCHT.
        assert "in WISO gelöscht" in conn.execute(
            "SELECT geloescht_grund FROM beleg WHERE id = ?", (alt_2["beleg_id"],)).fetchone()[0]
        assert conn.execute("SELECT status FROM wiso_buchung WHERE id = ?",
                            (alt_2["id"],)).fetchone()[0] == "GELOESCHT"
        # Neu: Udemy -- bekannte Plattform, Ausland.
        einnahme = conn.execute("SELECT e.*, k.schluessel FROM einnahme e JOIN euer_kategorie k"
                                " ON k.id = e.kategorie_id WHERE e.id = ?",
                                (aktiv(conn, 19)["einnahme_id"],)).fetchone()
        assert (einnahme["zahler"], einnahme["schluessel"]) == ("Udemy",
                                                                "EINNAHME_HONORAR_AUSLAND")
        a = euer.auswertung(conn, JAHR)
        assert summe_ausgaben_ohne_afa(a) == b.AUSGABEN_BEKANNT_CENT - 1299 + 1399 - 12900 + 2975
        assert summe_einnahmen(a) == b.EINNAHMEN_CENT + b.ERSTATTUNG_CENT + 3300
        assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion IN"
                            " ('WISO_GEAENDERT', 'WISO_GELOESCHT')").fetchone()[0] == 2
        # Und ein dritter Lauf derselben Datei: nichts mehr zu tun.
        plan = planen(conn, folgedatei())
        assert (plan.anzahl["neu"], plan.anzahl["geaendert"],
                plan.anzahl["in_wiso_geloescht"]) == (0, 0, 0)

    def test_ohne_entscheidung_bleibt_alles(self, conn, importiert, ordner):
        alt_1, alt_2 = aktiv(conn, 1), aktiv(conn, 2)
        _, ergebnis = einspielen(conn, ordner, folgedatei())
        assert (ergebnis.belege, ergebnis.einnahmen, ergebnis.ersetzt, ergebnis.geloescht) == (
            1, 1, 0, 0)
        assert aktiv(conn, 1)["id"] == alt_1["id"] and aktiv(conn, 2)["id"] == alt_2["id"]
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg WHERE geloescht_am IS NOT NULL") == 0
        plan = planen(conn, folgedatei())
        assert (plan.anzahl["geaendert"], plan.anzahl["in_wiso_geloescht"]) == (1, 1)

    def test_aenderung_ignorieren_und_loeschung_behalten(self, conn, importiert, ordner):
        alt_2 = aktiv(conn, 2)
        auswahl = wiso.Auswahl(aktionen={"1-1": wiso.IGNORIEREN},
                               geloescht={alt_2["id"]: wiso.BEHALTEN})
        _, ergebnis = einspielen(conn, ordner, folgedatei(), auswahl=auswahl)
        assert (ergebnis.ignoriert, ergebnis.behalten) == (1, 1)
        plan = planen(conn, folgedatei())
        assert (plan.anzahl["geaendert"], plan.anzahl["in_wiso_geloescht"]) == (0, 0)
        assert conn.execute("SELECT betrag_brutto_cent FROM v_beleg_gueltig WHERE id = ?",
                            (aktiv(conn, 1)["beleg_id"],)).fetchone()[0] == 1299
        assert conn.execute("SELECT geloescht_am FROM beleg WHERE id = ?",
                            (alt_2["beleg_id"],)).fetchone()[0] is None

    def test_im_tool_geloescht_kommt_nicht_wieder(self, conn, importiert, ordner):
        loeschen.loeschen(conn, aktiv(conn, 1)["beleg_id"], grund="doppelt erfasst")
        plan, ergebnis = einspielen(conn, ordner)
        assert posten(plan, 1).gruppe == wiso.BEKANNT and posten(plan, 1).im_tool_geloescht
        assert ergebnis.neu == 0

    def test_ausgelassen_bleibt_ausgelassen(self, conn, betrieb, ordner):
        _, ergebnis = einspielen(conn, ordner,
                                 auswahl=wiso.Auswahl(aktionen={"2-1": wiso.AUSLASSEN}))
        assert ergebnis.ausgelassen == 1
        assert conn.execute("SELECT status FROM wiso_buchung WHERE buchungsnummer = '2'"
                            ).fetchone()[0] == "AUSGELASSEN"
        plan, ergebnis = einspielen(conn, ordner)
        assert posten(plan, 2).gruppe == wiso.BEKANNT and ergebnis.neu == 0

    def test_zukuenftige_zahlung_nicht_uebernehmbar(self, conn, betrieb):
        spaeter = b.Buchung(30, "30.11.2026", "Beispiel Zukunft", ("4900", "Sonstige"),
                            soll="5,00")
        plan = planen(conn, [*b.BUCHUNGEN, spaeter])
        assert posten(plan, 30).gruppe == wiso.NICHT
        assert "in der Zukunft" in posten(plan, 30).grund

    def test_veraltete_vorschau_schreibt_nichts(self, conn, betrieb, ordner):
        plan = planen(conn)
        _beleg(conn, "WERBUNG", 5000, "2026-01-25")
        vorher = anzahl(conn, "SELECT COUNT(*) FROM journal")
        with pytest.raises(wiso.VorschauVeraltet) as fehler:
            wiso.uebernehmen(conn, ordner, NAME, b.datei(), fingerabdruck=plan.fingerabdruck,
                             auswahl=wiso.Auswahl(), heute=HEUTE)
        assert fehler.value.plan.anzahl["verknuepfen"] == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM journal") == vorher
        assert anzahl(conn, "SELECT COUNT(*) FROM wiso_import") == 0
        assert not (ordner / "importe").exists() or not any((ordner / "importe").rglob("*.csv"))


# --------------------------------------------------------------- Verknuepfen

def _beleg(conn, schluessel: str, cent: int, datum: str, lieferant: str = "Muster Werbung"):
    lieferant_id = conn.execute("INSERT INTO lieferant (name, angelegt_am) VALUES (?, ?)",
                                (lieferant, "2026-01-01")).lastrowid
    beleg_id = conn.execute(
        "INSERT INTO beleg (art, lieferant_id, kategorie_id, belegdatum, betrag_brutto_cent,"
        " aufteilungsquote, betrag_betrieblich_cent, erfasst_am)"
        " VALUES ('EINGANG', ?, ?, ?, ?, 100, ?, ?)",
        (lieferant_id, kat(conn, schluessel), datum, cent, cent, db.jetzt_utc())).lastrowid
    conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                 (beleg_id, datum, -cent))
    return beleg_id


def _zahlung_auf_rechnung(conn, cent: int, datum: str) -> int:
    kunde = conn.execute(
        "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
        " VALUES ('K-0100', 'Beispiel Partner GmbH', 'Musterstraße 1', '12345', 'Musterstadt',"
        " '2026-01-01')").lastrowid
    rechnung = conn.execute(
        "INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,"
        " netto_cent, brutto_cent, leistungsort, betreff, status, festgeschrieben_am,"
        " erstellt_am) VALUES ('RECHNUNG', 'RE-2026-0001', 1, 2026, ?, '2026-01-02', ?, ?,"
        " 'INLAND', 'Leistung Januar', 'FESTGESCHRIEBEN', ?, ?)",
        (kunde, cent, cent, db.jetzt_utc(), db.jetzt_utc())).lastrowid
    return conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, ?, ?)",
                        (rechnung, datum, cent)).lastrowid


class TestVerknuepfen:
    def test_mit_vorhandenem_beleg(self, conn, betrieb, ordner):
        beleg_id = _beleg(conn, "WERBUNG", 5000, "2026-01-24")
        plan = planen(conn)
        p = posten(plan, 7)
        assert (p.kandidat.typ, p.kandidat.id, p.kandidat.sicher, p.vorschlag) == (
            "beleg", beleg_id, True, wiso.VERKNUEPFEN)
        assert plan.anzahl["verknuepfen"] == 1 and plan.anzahl["neu"] == 12
        _, ergebnis = einspielen(conn, ordner)
        assert (ergebnis.verknuepft, ergebnis.belege) == (1, 6)
        zeile = aktiv(conn, 7)
        assert (zeile["beleg_id"], zeile["verknuepft"]) == (beleg_id, 1)
        # Die Ausgabe zaehlt einmal -- mit der Zahlung des vorhandenen Belegs.
        a = euer.auswertung(conn, JAHR)
        assert summe_ausgaben_ohne_afa(a) == b.AUSGABEN_BEKANNT_CENT
        # Ein verknuepfter Beleg wartet nicht auf eine Datei aus WISO.
        assert anhaengen.ohne_datei_aus_wiso(conn) == 6

    def test_mit_zahlung_auf_eigene_rechnung(self, conn, betrieb, ordner):
        zahlung_id = _zahlung_auf_rechnung(conn, 25000, "2026-01-11")
        plan = planen(conn)
        p = posten(plan, 3)
        assert (p.kandidat.typ, p.kandidat.id, p.vorschlag) == ("zahlung", zahlung_id,
                                                                  wiso.VERKNUEPFEN)
        _, ergebnis = einspielen(conn, ordner)
        assert (ergebnis.verknuepft, ergebnis.einnahmen) == (1, 5)
        assert aktiv(conn, 3)["zahlung_id"] == zahlung_id
        a = euer.auswertung(conn, JAHR)
        assert summe_einnahmen(a) == b.EINNAHMEN_CENT + b.ERSTATTUNG_CENT
        assert grenzwert.vereinnahmt_cent(conn, JAHR) == 25000

    def test_verknuepfen_abgewaehlt(self, conn, betrieb, ordner):
        _beleg(conn, "WERBUNG", 5000, "2026-01-24")
        _, ergebnis = einspielen(conn, ordner,
                                 auswahl=wiso.Auswahl(aktionen={"7-1": wiso.UEBERNEHMEN}))
        assert (ergebnis.verknuepft, ergebnis.belege) == (0, 7)

    def test_geloeschter_beleg_ist_kein_kandidat(self, conn, betrieb):
        loeschen.loeschen(conn, _beleg(conn, "WERBUNG", 5000, "2026-01-25"),
                          grund="doppelt erfasst")
        assert posten(planen(conn), 7).kandidat is None

    def test_anderer_betrag_oder_zu_weit_weg(self, conn, betrieb):
        _beleg(conn, "WERBUNG", 5001, "2026-01-25")
        _beleg(conn, "WERBUNG", 5000, "2026-02-05", lieferant="Beispiel Agentur")
        assert posten(planen(conn), 7).kandidat is None

    def test_weiter_weg_mit_aehnlichem_namen_nur_angeboten(self, conn, betrieb):
        # Zahlungsziel: Belegdatum im Tool, Bankdatum in WISO (bis 45 Tage).
        _beleg(conn, "WERBUNG", 5000, "2026-02-05", lieferant="Muster Agentur")
        p = posten(planen(conn), 7)
        assert (p.kandidat.sicher, p.vorschlag) == (False, wiso.UEBERNEHMEN)


# ------------------------------------------------------- Zahler und Einstufung

class TestZahler:
    def test_einstufung_wird_gemerkt(self, conn, betrieb, ordner):
        einspielen(conn, ordner, auswahl=wiso.Auswahl(zahler={"beispiel": einnahmen.AUSLAND}))
        assert grenzwert.vereinnahmt_cent(conn, JAHR) == 0
        neu = b.Buchung(18, "10.07.2026", "Beispiel Partnerprogramm", b.ERLOES, haben="70,00")
        plan = planen(conn, [*b.BUCHUNGEN, neu])
        p = posten(plan, 18)
        assert (p.einstufung, p.kategorie_herkunft) == (einnahmen.AUSLAND, "Zahler")
        assert next(z for z in plan.zahler if z.schluessel == "beispiel").herkunft == "gemerkt"

    def test_zahler_spaeter_und_in_angaben_einstufen(self, conn, betrieb, ordner):
        _, ergebnis = einspielen(conn, ordner,
                                 auswahl=wiso.Auswahl(zahler={"beispiel": wiso.SPAETER}))
        assert ergebnis.einnahmen == 5 and aktiv(conn, 3) is None
        angabe = angaben.offene(conn, "ZAHLER")[0]
        assert (angabe.schluessel, angabe.angaben["cent"]) == ("beispiel", 25000)
        fertig = angaben.zahler_einstufen(conn, ordner, angabe.id, einnahmen.INLAND, heute=HEUTE)
        assert "1 Buchung übernommen" in fertig.text
        assert aktiv(conn, 3)["einnahme_id"] is not None
        assert grenzwert.vereinnahmt_cent(conn, JAHR) == 25000
        assert angaben.offene(conn, "ZAHLER") == []
        assert einnahmen.gemerkt(conn, "beispiel") is not None

    @pytest.mark.parametrize(("text", "name", "einstufung"), [
        ("Amazon Affiliate", "Amazon", einnahmen.AUSLAND),
        ("Youtube Ads", "YouTube / Google", einnahmen.AUSLAND),
        ("Udemy Auszahlung", "Udemy", einnahmen.AUSLAND),
        ("Apple App Store", "Apple App Store", einnahmen.AUSLAND),
        ("Rad Beispiel - Verkauf", "Rad Beispiel", einnahmen.ANLAGENVERKAUF),
        ("Musterfirma Zusendung", "Musterfirma", einnahmen.INLAND),
        ("Beispiel Partnerprogramm", "Beispiel", einnahmen.INLAND),
    ])
    def test_zahler_aus_dem_text(self, text, name, einstufung):
        gefunden = einnahmen.zahler_aus_text(text)
        assert (gefunden.name, gefunden.vorschlag) == (name, einstufung)


# ---------------------------------------------------------- Angaben fehlen

class TestAngabenFehlen:
    def test_vorjahresgut_mit_hinweis(self, conn, importiert):
        offen = angaben.offene(conn, "ANLAGEGUT")
        assert [(a.titel, a.schluessel, a.afa) for a in offen] == [
            ("Kamera Beispiel", "anl:2", {2026: 40000})]
        assert angaben.fehlende_afa(conn, JAHR) == [("Kamera Beispiel", 40000)]
        assert angaben.anzahl_offen(conn) == 2

    def test_nach_dem_ausfuellen_ist_die_afa_da(self, conn, importiert, ordner):
        angabe = angaben.offene(conn, "ANLAGEGUT")[0]
        fertig = angaben.anlagegut_ergaenzen(conn, angabe.id, anschaffung_am="02.01.2025",
                                             ak_brutto_cent=120000, nutzungsdauer_jahre=None,
                                             heute=HEUTE)
        assert "3 Jahre (aus der WISO-AfA abgeleitet)" in fertig.text
        assert "AfA 2026 wie in WISO" in fertig.text
        assert "AfA 2025 steht im Tool nur zur Übersicht" in fertig.text
        assert fertig.hinweise == []
        assert sum(anlagen.afa_summe(conn, JAHR).values()) == b.AFA_CENT
        assert angaben.fehlende_afa(conn, JAHR) == []
        assert afa_zeile(euer.auswertung(conn, JAHR)) == b.AFA_CENT
        afa = next(z for z in wiso.abgleich(conn, ordner, JAHR).zeilen if z.bereich == "AfA")
        assert afa.differenz_cent == 0
        # Der naechste Import kennt das Gut ueber seine Anlagennummer.
        plan = planen(conn)
        kamera = next(a for a in plan.afa if a.anl_nr == "2")
        assert (kamera.status, kamera.tool_cent) == ("VERZEICHNIS", 40000)

    def test_abweichende_angaben_werden_gemeldet(self, conn, importiert):
        angabe = angaben.offene(conn, "ANLAGEGUT")[0]
        fertig = angaben.anlagegut_ergaenzen(conn, angabe.id, anschaffung_am="02.01.2025",
                                             ak_brutto_cent=120000, nutzungsdauer_jahre=5,
                                             heute=HEUTE)
        assert fertig.hinweise == ["AfA 2026: das Tool rechnet 240,00 €, WISO 400,00 € — "
                                   "Anschaffungstag, -kosten und Nutzungsdauer bitte prüfen."]

    def test_ohne_passende_nutzungsdauer_wird_gefragt(self, conn, importiert):
        angabe = angaben.offene(conn, "ANLAGEGUT")[0]
        with pytest.raises(angaben.AngabenFehler, match="Nutzungsdauer angeben"):
            angaben.anlagegut_ergaenzen(conn, angabe.id, anschaffung_am="02.01.2025",
                                        ak_brutto_cent=123456, nutzungsdauer_jahre=None,
                                        heute=HEUTE)
        assert angaben.anzahl_offen(conn) == 2
        assert anzahl(conn, "SELECT COUNT(*) FROM anlagegut") == 1

    def test_konto_zuordnen_legt_die_buchung_an(self, conn, importiert, ordner):
        angabe = angaben.offene(conn, "KONTO")[0]
        fertig = angaben.konto_zuordnen(conn, ordner, angabe.id, kat(conn, "UEBRIGE"),
                                        heute=HEUTE)
        assert "1 Buchung übernommen" in fertig.text
        assert conn.execute("SELECT kategorie_id FROM beleg WHERE id = ?",
                            (aktiv(conn, 17)["beleg_id"],)).fetchone()[0] == kat(conn, "UEBRIGE")
        assert conn.execute("SELECT kategorie_id FROM wiso_konto WHERE konto = '4500'"
                            ).fetchone()[0] == kat(conn, "UEBRIGE")
        a = euer.auswertung(conn, JAHR)
        assert summe_ausgaben_ohne_afa(a) == b.AUSGABEN_BEKANNT_CENT + b.AUSGABEN_UNBEKANNT_CENT
        assert angaben.offene(conn, "KONTO") == []
        # Ein naechster Import nimmt die gemerkte Kategorie.
        assert posten(planen(conn), 17).gruppe == wiso.BEKANNT

    def test_konto_braucht_ausgabenkategorie(self, conn, importiert, ordner):
        angabe = angaben.offene(conn, "KONTO")[0]
        with pytest.raises(angaben.AngabenFehler, match="Ausgabenkategorie"):
            angaben.konto_zuordnen(conn, ordner, angabe.id, kat(conn, "EINNAHME_HONORAR_INLAND"))
        assert aktiv(conn, 17) is None


# ---------------------------------------------------- Datei nachtraeglich

def rechnung_xml(brutto: str = "29.75") -> bytes:
    """Eine erfundene E-Rechnung (CII) zur WISO-Buchung 18."""
    return bo.cii("RE-2026-0100", "20260610", brutto=brutto, netto="25.00", ust="4.75").replace(
        b"Hetzner Online GmbH", b"Beispiel Hosting GmbH")


@pytest.fixture
def mit_hosting(conn, betrieb, ordner):
    einspielen(conn, ordner, [*b.BUCHUNGEN, NEU_18])
    return aktiv(conn, 18)["beleg_id"]


class TestAnhaengen:
    def test_je_beleg(self, conn, mit_hosting, ordner):
        eingang = pipeline.aufnehmen(rechnung_xml(), "rechnung.xml", ordner)
        lesung = pipeline.lies(conn, eingang, heute=HEUTE)
        fertig = anhaengen.anhaengen(conn, ordner, mit_hosting, lesung)
        assert fertig.abweichungen == []
        datei = conn.execute("SELECT * FROM belegdatei WHERE beleg_id = ?",
                             (mit_hosting,)).fetchone()
        assert (datei["rolle"], datei["original_name"]) == ("ORIGINAL", "rechnung.xml")
        assert os.stat(ordner / datei["pfad"]).st_mode & 0o222 == 0
        assert pipeline.liegen_geblieben(ordner) == []
        # Betrag und Datum bleiben die des Belegs.
        assert tuple(conn.execute("SELECT belegdatum, betrag_brutto_cent FROM beleg WHERE id = ?",
                                  (mit_hosting,)).fetchone()) == ("2026-06-10", 2975)
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'BELEG_DATEI_ANGEHAENGT'"
                      " AND objekt_id = ?", mit_hosting) == 1
        assert anhaengen.ohne_datei_aus_wiso(conn) == 7
        # Ein zweites Mal geht nicht.
        zweite = pipeline.aufnehmen(rechnung_xml("29.76"), "zweite.xml", ordner)
        with pytest.raises(BelegFehler, match="schon eine Datei"):
            anhaengen.anhaengen(conn, ordner, mit_hosting,
                                pipeline.lies(conn, zweite, heute=HEUTE))
        assert [p.name for p in pipeline.liegen_geblieben(ordner)] == [zweite.pfad.name]

    def test_abweichung_wird_gesagt(self, conn, mit_hosting, ordner):
        eingang = pipeline.aufnehmen(rechnung_xml("30.75"), "rechnung.xml", ordner)
        fertig = anhaengen.anhaengen(conn, ordner, mit_hosting,
                                     pipeline.lies(conn, eingang, heute=HEUTE))
        assert fertig.abweichungen == ["Betrag laut Datei 30,75 €, am Beleg 29,75 €"]

    def test_ueber_den_eingang(self, conn, mit_hosting, ordner):
        """Die Vorschau stellt die Datei in die Gruppe "zuordnen" (Paket H:
        Betrag gleich, Datum gleich, Name passt -- sicher), der Klick haengt
        sie an den WISO-Beleg statt einen zweiten zu buchen."""
        pipeline.aufnehmen(rechnung_xml(), "rechnung.xml", ordner)
        belege_vorher = anzahl(conn, "SELECT COUNT(*) FROM beleg")
        plaene = sammelbuchung.planen(conn, ordner, heute=HEUTE, textleser=None)
        assert [p.status for p in plaene] == [sammelbuchung.ZUORDNEN]
        plan = plaene[0]
        assert (plan.zuordnung.vorschlag.id, plan.zuordnung.stufe) == (mit_hosting, "sicher")
        assert plan.grund.startswith(f"passt vielleicht zu Beleg {mit_hosting} (WISO-Eintrag "
                                     "Nr. 18)")
        ergebnis = sammelbuchung.buchen(
            conn, ordner, [sammelbuchung.Auswahl(plan.eingangsname, plan.sha256, None,
                                                 anhaengen_an=mit_hosting)],
            heute=HEUTE, textleser=None)
        assert [(a.ziel_id, a.abweichungen) for a in ergebnis.angehaengt] == [(mit_hosting, [])]
        assert ergebnis.gebucht == []
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == belege_vorher
        assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?",
                      mit_hosting) == 1

    def test_eingang_ohne_passenden_wiso_beleg_bucht_normal(self, conn, mit_hosting, ordner):
        pipeline.aufnehmen(rechnung_xml("99.00"), "rechnung.xml", ordner)
        plaene = sammelbuchung.planen(conn, ordner, heute=HEUTE, textleser=None)
        assert [p.status for p in plaene] != [sammelbuchung.ZUORDNEN]


# ------------------------------------------------------- Excel und Sicherung

class TestExcelUndSicherung:
    def test_excel_hat_die_neuen_blaetter_und_keine_formel(self, conn, betrieb, ordner):
        from openpyxl import load_workbook

        formel = b.Buchung(18, "10.06.2026", "=HYPERLINK(\"http://example.invalid\")",
                           ("4900", "Sonstige betriebliche Aufwendungen"), soll="1,00")
        einspielen(conn, ordner, [*b.BUCHUNGEN, formel])
        pfad = ordner / "export.xlsx"
        pfad.write_bytes(export_excel.als_bytes(conn))
        mappe = load_workbook(pfad)
        for blatt in ("Einnahmen ohne Rechnung", "WISO-Importe", "WISO-Buchungen",
                      "WISO-Kontozuordnung", "Angaben fehlen", "Zahler (Einstufung)"):
            assert blatt in mappe.sheetnames
        for blatt in mappe.worksheets:
            for zeile in blatt.iter_rows():
                for zelle in zeile:
                    assert zelle.data_type != "f", (blatt.title, zelle.coordinate, zelle.value)

    def test_sicherung_enthaelt_import_und_abrechnung(self, conn, importiert, ordner):
        einnahmen.erfassen(conn, zufluss_am="01.03.2026", betrag_cent=1000,
                           zahler="Beispiel Plattform",
                           kategorie_id=kat(conn, "EINNAHME_HONORAR_INLAND"),
                           datei=einnahmen.datei_pruefen(b"%PDF-1.4\n%%EOF\n", "abr.pdf"),
                           datenordner=ordner, heute=HEUTE)
        archiv = backup.erzeuge(conn, ordner)
        with zipfile.ZipFile(archiv.pfad) as z:
            namen = z.namelist()
        assert any(n.startswith("importe/wiso/2026/") for n in namen)
        assert any(n.startswith("einnahmen/2026/") for n in namen)
        assert backup._verzeichnete_dateien(conn) >= {
            conn.execute("SELECT pfad FROM wiso_import").fetchone()[0],
            conn.execute("SELECT pfad FROM einnahmedatei").fetchone()[0]}


# ------------------------------------------------------------ Datenbank

class TestDatenbank:
    def test_wiso_buchung_inhalt_fest(self, conn, importiert):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE wiso_buchung SET pruefsumme = 'x' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM wiso_buchung WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM wiso_import")

    def test_ein_eintrag_nur_einmal_aktiv(self, conn, importiert):
        alt = aktiv(conn, 1)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO wiso_buchung (jahr, buchungsnummer, teilbuchung, pruefsumme,"
                " inhalt_json, art, einnahme_id, import_id, status, erfasst_am)"
                " VALUES (2026, '99', '1', 'x', '{}', 'EINNAHME', NULL, ?, 'AKTIV', 'x')",
                (alt["import_id"],))

    def test_beleg_nur_mit_ausgabenkategorie(self, conn, importiert):
        with pytest.raises(sqlite3.IntegrityError, match="Ausgabenkategorie"):
            conn.execute("UPDATE beleg SET kategorie_id = ? WHERE id = ?",
                         (kat(conn, "EINNAHME_HONORAR_INLAND"), aktiv(conn, 1)["beleg_id"]))


# ---------------------------------------------------------------- Seiten

@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=bo.EINRICHTUNG, follow_redirects=False)
        yield c


def hochladen(klient, inhalt: bytes = b.datei(), name: str = NAME):
    return klient.post("/wiso/import", files={"datei": (name, inhalt, "text/csv")})


def felder(html: str) -> dict[str, str]:
    return {name: re.search(rf'name="{name}" value="([0-9a-f]{{64}})"', html).group(1)
            for name in ("schluessel", "fingerabdruck")}


def datenbank(tmp_path) -> sqlite3.Connection:
    verbindung = sqlite3.connect(tmp_path / "steuer.sqlite")
    verbindung.row_factory = sqlite3.Row
    return verbindung


class TestSeiten:
    def test_navigation_und_leere_seiten(self, klient):
        start = klient.get("/").text
        assert 'href="/einnahmen"' in start and 'href="/wiso"' in start
        for pfad in ("/wiso", "/einnahmen", "/angaben", "/wiso/abgleich"):
            assert klient.get(pfad).status_code == 200, pfad
        assert "noch kein WISO-Export" in klient.get("/wiso/abgleich").text

    def test_vorschau_und_uebernehmen(self, klient, tmp_path):
        antwort = hochladen(klient)
        assert antwort.status_code == 200
        assert "Plotter Beispiel" in antwort.text
        assert datenbank(tmp_path).execute("SELECT COUNT(*) FROM wiso_import").fetchone()[0] == 0
        antwort = klient.post("/wiso/uebernehmen", data=felder(antwort.text),
                              follow_redirects=False)
        assert antwort.status_code == 303
        seite = klient.get(antwort.headers["location"]).text
        assert "13 Buchungen übernommen" in seite
        conn = datenbank(tmp_path)
        assert conn.execute("SELECT COUNT(*) FROM v_einnahme_gueltig").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM v_beleg_gueltig").fetchone()[0] == 7
        # Zweiter Import derselben Datei: nichts Neues.
        antwort = klient.post("/wiso/uebernehmen", data=felder(hochladen(klient).text),
                              follow_redirects=False)
        assert "Keine%20neue%20Buchung%20angelegt%2C%201%20zur%C3%BCckgestellt" in \
            antwort.headers["location"]
        # Abgleich, Angaben, EUeR, Belege und Einnahmen zeigen den Stand.
        abgleich = klient.get("/wiso/abgleich?jahr=2026").text
        assert "Kamera Beispiel: Angaben fehlen" in abgleich
        assert "Kamera Beispiel" in klient.get("/angaben").text
        assert "AfA für Kamera Beispiel fehlt (WISO: 400,00 €)" in klient.get(
            "/euer?jahr=2026").text
        assert "AfA für Kamera Beispiel fehlt" in klient.get("/steuer?jahr=2026").text
        assert "Angaben fehlen: 2" in klient.get("/").text
        assert "aus WISO übernommen · Datei fehlt" in klient.get(
            "/belege?jahr=2026&ohne_datei=1").text
        assert "YouTube / Google" in klient.get("/einnahmen?jahr=2026").text

    def test_veraltete_vorschau_409(self, klient, tmp_path):
        antwort = hochladen(klient)
        conn = datenbank(tmp_path)
        conn.execute("BEGIN")
        _beleg(conn, "WERBUNG", 5000, "2026-01-25")
        conn.execute("COMMIT")
        antwort = klient.post("/wiso/uebernehmen", data=felder(antwort.text),
                              follow_redirects=False)
        assert antwort.status_code == 409
        assert "Übernommen wurde nichts" in antwort.text
        assert conn.execute("SELECT COUNT(*) FROM wiso_import").fetchone()[0] == 0

    @pytest.mark.parametrize(("name", "inhalt", "meldung"), [
        ("buchungen.xlsx", b"x", "Endung .csv"),
        ("buchungen.csv", b"PK\x03\x04" + b"x" * 100, "Excel-Datei"),
        ("buchungen.csv", b"x" * (wiso.MAX_BYTES + 10), "zu groß"),
    ])
    def test_falsche_datei(self, klient, tmp_path, name, inhalt, meldung):
        antwort = hochladen(klient, inhalt, name)
        assert antwort.status_code == 422
        assert meldung in antwort.text
        assert not (tmp_path / "importe").exists()

    def test_abgelaufene_vorschau(self, klient):
        antwort = klient.post("/wiso/uebernehmen", data={"schluessel": "a" * 64,
                                                         "fingerabdruck": "b" * 64},
                              follow_redirects=False)
        assert antwort.status_code == 303 and "abgelaufen" in antwort.headers["location"] or \
            "abgelaufen" in klient.get(antwort.headers["location"]).text

    def test_angaben_formulare(self, klient, tmp_path):
        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient).text))
        conn = datenbank(tmp_path)
        gut = conn.execute("SELECT id FROM fehlende_angabe WHERE art = 'ANLAGEGUT'").fetchone()[0]
        konto = conn.execute("SELECT id FROM fehlende_angabe WHERE art = 'KONTO'").fetchone()[0]
        antwort = klient.post(f"/angaben/{gut}/anlagegut", data={
            "anschaffung_am": "02.01.2025", "ak_brutto": "1.200,00", "nutzungsdauer_jahre": "",
            "zurueck": "/anlagen"}, follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith("/anlagen?erfolg=")
        uebrige = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel = 'UEBRIGE'"
                               ).fetchone()[0]
        antwort = klient.post(f"/angaben/{konto}/konto", data={
            "kategorie_id": str(uebrige), "zurueck": "https://example.invalid"},
            follow_redirects=False)
        assert antwort.headers["location"].startswith("/angaben?erfolg=")
        assert conn.execute("SELECT COUNT(*) FROM fehlende_angabe WHERE erledigt_am IS NULL"
                            ).fetchone()[0] == 0
        assert "Es fehlt nichts" in klient.get("/angaben").text

    def test_datei_je_beleg_anhaengen(self, klient, tmp_path):
        datei = b.datei([*b.BUCHUNGEN, NEU_18])
        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient, datei).text))
        conn = datenbank(tmp_path)
        beleg_id = conn.execute("SELECT beleg_id FROM wiso_buchung WHERE buchungsnummer = '18'"
                                ).fetchone()[0]
        antwort = klient.post(f"/belege/{beleg_id}/datei-anhaengen", files={
            "datei": ("rechnung.xml", rechnung_xml(), "application/xml")},
            follow_redirects=False)
        assert antwort.status_code == 303
        assert "erfolg=" in antwort.headers["location"]
        assert conn.execute("SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?",
                            (beleg_id,)).fetchone()[0] == 1

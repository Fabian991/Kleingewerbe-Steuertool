"""Nachbesserung des WISO-Imports (Paket G) nach der Pruefung.

Je Befund eine Klasse; alle Faelle laufen wie tests/test_wiso_import.py auf
ERFUNDENEN Dateien im exakten Format des WISO-Exports „Buchungen“.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.logik import euer, grenzwert, wiso
from app.main import erzeuge_app
from tests import test_belegordner as bo
from tests import wiso_beispiel as b
from tests.test_wiso_import import (
    HEUTE,
    JAHR,
    NAME,
    aktiv,
    anzahl,
    einspielen,
    planen,
    posten,
    summe_ausgaben_ohne_afa,
    summe_einnahmen,
)

BUERO = ("4980", "Sonstiger Betriebsbedarf")
SONSTIGE = ("4900", "Sonstige betriebliche Aufwendungen")
PRIVATENTNAHME = ("1800", "Privatentnahmen allgemein")
PRIVATEINLAGE = ("1890", "Privateinlagen")


@pytest.fixture
def ordner(tmp_path) -> Path:
    return tmp_path


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=bo.EINRICHTUNG, follow_redirects=False)
        yield c


def stand(conn) -> tuple[int, int, int]:
    """(Einnahmen, Ausgaben ohne AfA, § 19-Umsatz) des Jahres in Cent."""
    a = euer.auswertung(conn, JAHR)
    return summe_einnahmen(a), summe_ausgaben_ohne_afa(a), grenzwert.vereinnahmt_cent(conn, JAHR)


# ------------------------------------------- Befund: in WISO umgebucht

class TestUmgebucht:
    """Eine uebernommene Buchung wird in WISO auf ein Konto ohne Geldfluss umgebucht."""

    ERST = [
        b.Buchung(1, "05.01.2026", "Beispiel Office Abo", SONSTIGE, soll="12,99"),
        b.Buchung(2, "10.01.2026", "Beispiel Partnerprogramm", b.ERLOES, haben="250,00"),
        b.Buchung(3, "12.01.2026", "Beispiel Bürostuhl", BUERO, soll="80,00"),
    ]
    FOLGE = [ERST[0], replace(ERST[1], konto=PRIVATEINLAGE),
             replace(ERST[2], konto=PRIVATENTNAHME)]

    def test_wird_erkannt_und_nicht_still_uebersprungen(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ERST)
        assert stand(conn) == (25000, 9299, 25000)
        plan = planen(conn, self.FOLGE)
        assert [posten(plan, n).gruppe for n in (1, 2, 3)] == [
            wiso.BEKANNT, wiso.UMGEBUCHT, wiso.UMGEBUCHT]
        assert (plan.anzahl["umgebucht"], plan.anzahl["privat"]) == (2, 0)
        assert "Privat" in posten(plan, 3).umgebucht
        assert posten(plan, 3).vorschlag == wiso.SPAETER
        # Ohne Wahl bleibt alles -- aber die Meldung sagt nicht "Nichts Neues".
        _, ergebnis = einspielen(conn, ordner, self.FOLGE)
        assert ergebnis.zusammenfassung() == "Keine neue Buchung angelegt, 2 zurückgestellt."
        assert stand(conn) == (25000, 9299, 25000)
        # Der Abgleich nennt den Grund statt "Beträge weichen ab".
        gruende = [g for z in wiso.abgleich(conn, ordner, JAHR).zeilen for g in z.gruende]
        assert sum("in WISO umgebucht" in g for g in gruende) == 2
        assert not any("Beträge weichen ab" in g for g in gruende)

    def test_im_tool_loeschen(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ERST)
        beleg_id = aktiv(conn, 3)["beleg_id"]
        _, ergebnis = einspielen(conn, ordner, self.FOLGE, auswahl=wiso.Auswahl(
            aktionen={"2-1": wiso.LOESCHEN, "3-1": wiso.LOESCHEN}))
        assert ergebnis.umgebucht == 2
        assert "2 in WISO umgebucht" in ergebnis.zusammenfassung()
        assert stand(conn) == (0, 1299, 0)
        grund = conn.execute("SELECT geloescht_grund FROM beleg WHERE id = ?",
                             (beleg_id,)).fetchone()[0]
        assert "in WISO umgebucht" in grund
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'WISO_UMGEBUCHT'") == 2
        # Ein weiterer Import fragt nicht wieder: die Zeilen sind jetzt privat.
        plan = planen(conn, self.FOLGE)
        assert [posten(plan, n).gruppe for n in (2, 3)] == [wiso.PRIVAT, wiso.PRIVAT]
        assert plan.anzahl["umgebucht"] == 0

    def test_im_tool_behalten(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ERST)
        _, ergebnis = einspielen(conn, ordner, self.FOLGE, auswahl=wiso.Auswahl(
            aktionen={"3-1": wiso.IGNORIEREN}))
        assert ergebnis.ignoriert == 1
        plan = planen(conn, self.FOLGE)
        assert posten(plan, 3).gruppe == wiso.BEKANNT and posten(plan, 3).umgebucht
        assert posten(plan, 2).gruppe == wiso.UMGEBUCHT
        assert stand(conn) == (25000, 9299, 25000)

    def test_zahlungsdatum_in_die_zukunft_verschoben(self, conn, betrieb, ordner):
        erst = [b.Buchung(1, "20.10.2026", "Beispiel Fortbildung", ("4945", "Fortbildungskosten"),
                          soll="300,00")]
        einspielen(conn, ordner, erst)
        folge = [replace(erst[0], datum="05.11.2026")]
        p = posten(planen(conn, folge), 1)
        assert p.gruppe == wiso.UMGEBUCHT and "in der Zukunft" in p.umgebucht
        einspielen(conn, ordner, folge, auswahl=wiso.Auswahl(aktionen={"1-1": wiso.LOESCHEN}))
        assert stand(conn)[1] == 0
        # Nach dem Zahltag kommt die Buchung als neue wieder.
        spaeter = wiso.plane(conn, wiso.lies(NAME, b.datei(folge), heute=HEUTE),
                             heute=HEUTE.replace(month=11, day=10))
        assert posten(spaeter, 1).gruppe == wiso.NEU

    def test_verknuepfter_eintrag_bleibt(self, conn, betrieb, ordner):
        from tests.test_wiso_import import _beleg

        beleg_id = _beleg(conn, "UEBRIGE", 8000, "2026-01-12", lieferant="Beispiel Bürostuhl")
        einspielen(conn, ordner, self.ERST)
        assert aktiv(conn, 3)["verknuepft"] == 1
        einspielen(conn, ordner, self.FOLGE, auswahl=wiso.Auswahl(
            aktionen={"3-1": wiso.LOESCHEN}))
        assert conn.execute("SELECT geloescht_am FROM beleg WHERE id = ?",
                            (beleg_id,)).fetchone()[0] is None
        assert aktiv(conn, 3) is None

    def test_vorschau_zeigt_die_gruppe(self, klient, tmp_path):
        from tests.test_wiso_import import felder, hochladen

        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient, b.datei(self.ERST)).text))
        seite = hochladen(klient, b.datei(self.FOLGE)).text
        assert "In WISO umgebucht (2)" in seite
        assert 'name="aktion_3-1"' in seite and "im Tool löschen" in seite
        assert "Privatentnahme bzw. -einlage" in seite


# ------------------------- Befund: in WISO geloescht und neu erfasst (neue Nummer)

class TestNeueNummer:
    ERST = [
        b.Buchung(1, "10.01.2026", "Beispiel Partnerprogramm", b.ERLOES, haben="250,00"),
        b.Buchung(2, "12.02.2026", "Beispiel Kurs Fotografie", ("4945", "Fortbildungskosten"),
                  soll="199,00"),
    ]
    FOLGE = [replace(ERST[0], nummer=3),
             replace(ERST[1], nummer=4, konto=("4940", "Zeitschriften, Bücher"))]

    def test_vorschlag_haengt_den_eintrag_um(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ERST)
        assert stand(conn) == (25000, 19900, 25000)
        alt_1, alt_2 = aktiv(conn, 1), aktiv(conn, 2)
        plan = planen(conn, self.FOLGE)
        p3, p4 = posten(plan, 3), posten(plan, 4)
        assert (p3.vorschlag, p3.kandidat.typ, p3.kandidat.id, p3.kandidat.vorgaenger) == (
            wiso.VERKNUEPFEN, "einnahme", alt_1["einnahme_id"], alt_1["id"])
        assert (p4.vorschlag, p4.kandidat.id) == (wiso.VERKNUEPFEN, alt_2["beleg_id"])
        assert any("Kategorie im Tool" in h for h in p4.hinweise)
        assert plan.nachfolger == {alt_1["id"]: "3-1", alt_2["id"]: "4-1"}
        # Mit den Vorgaben: nichts doppelt.
        _, ergebnis = einspielen(conn, ordner, self.FOLGE)
        assert stand(conn) == (25000, 19900, 25000)
        assert ergebnis.umgehaengt == 2 and ergebnis.neu == 0
        neu_3 = aktiv(conn, 3)
        assert (neu_3["einnahme_id"], neu_3["verknuepft"]) == (alt_1["einnahme_id"], 0)
        assert conn.execute("SELECT status FROM wiso_buchung WHERE id = ?",
                            (alt_1["id"],)).fetchone()[0] == "ERSETZT"
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'WISO_UMGEHAENGT'") == 2
        # Der naechste Import kennt alles.
        plan = planen(conn, self.FOLGE)
        assert [p.gruppe for p in plan.posten] == [wiso.BEKANNT, wiso.BEKANNT]
        assert plan.geloescht == []

    def test_loeschen_der_alten_nummer_wird_dann_nicht_ausgefuehrt(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ERST)
        alt_1 = aktiv(conn, 1)
        einspielen(conn, ordner, self.FOLGE,
                   auswahl=wiso.Auswahl(geloescht={alt_1["id"]: wiso.LOESCHEN}))
        assert stand(conn) == (25000, 19900, 25000)

    def test_trotzdem_neu_uebernehmen_bleibt_moeglich(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ERST)
        alt_1 = aktiv(conn, 1)
        einspielen(conn, ordner, self.FOLGE, auswahl=wiso.Auswahl(
            aktionen={"3-1": wiso.UEBERNEHMEN}, geloescht={alt_1["id"]: wiso.LOESCHEN}))
        assert stand(conn) == (25000, 19900, 25000)
        assert conn.execute("SELECT geloescht_am IS NOT NULL FROM einnahme WHERE id = ?",
                            (alt_1["einnahme_id"],)).fetchone()[0] == 1


# ------------------------------- Befund: Verknuepfen mit einem unbezahlten Beleg

def _beleg_offen(conn, cent: int, datum: str, lieferant: str, schluessel: str = "HOSTING",
                 waehrung: str = "EUR") -> int:
    """Ein im Tool gebuchter Beleg OHNE Zahlung (Rechnung, noch nicht bezahlt)."""
    from app import db
    from tests.test_wiso_import import kat

    lieferant_id = conn.execute("INSERT INTO lieferant (name, angelegt_am) VALUES (?, ?)",
                                (lieferant, "2026-01-01")).lastrowid
    return conn.execute(
        "INSERT INTO beleg (art, lieferant_id, kategorie_id, belegdatum, waehrung,"
        " betrag_brutto_cent, aufteilungsquote, betrag_betrieblich_cent, erfasst_am)"
        " VALUES ('EINGANG', ?, ?, ?, ?, ?, 100, ?, ?)",
        (lieferant_id, kat(conn, schluessel), datum, waehrung, cent, cent,
         db.jetzt_utc())).lastrowid


class TestUnbezahltVerknuepft:
    BUCHUNG = [b.Buchung(1, "05.03.2026", "Beispiel Hosting Server", SONSTIGE, soll="47,60")]

    def test_zahlung_aus_wiso_wird_angelegt(self, conn, betrieb, ordner):
        beleg_id = _beleg_offen(conn, 4760, "2026-03-03", "Beispiel Hosting GmbH")
        p = posten(planen(conn, self.BUCHUNG), 1)
        assert (p.kandidat.id, p.kandidat.sicher, p.kandidat.zahlung_fehlt) == (
            beleg_id, True, True)
        assert "unbezahlt" in p.kandidat.text
        _, ergebnis = einspielen(conn, ordner, self.BUCHUNG)
        assert ergebnis.verknuepft == 1
        zahlungen = conn.execute("SELECT datum, betrag_cent FROM zahlung WHERE beleg_id = ?",
                                 (beleg_id,)).fetchall()
        assert [tuple(z) for z in zahlungen] == [("2026-03-05", -4760)]
        assert stand(conn)[1] == 4760
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'ZAHLUNG'"
                      " AND objekt_id = ?", beleg_id) == 1
        zeilen = wiso.abgleich(conn, ordner, JAHR).zeilen
        assert all(z.differenz_cent == 0 for z in zeilen if z.bereich == "Ausgaben")

    def test_fremdwaehrung_unbezahlt_nur_angeboten(self, conn, betrieb, ordner):
        _beleg_offen(conn, 4760, "2026-03-03", "Beispiel Hosting GmbH", waehrung="USD")
        p = posten(planen(conn, self.BUCHUNG), 1)
        assert p.kandidat is not None and not p.kandidat.sicher
        assert p.vorschlag == wiso.UEBERNEHMEN

    def test_abgleich_nennt_unbezahlt(self, conn, betrieb, ordner):
        beleg_id = _beleg_offen(conn, 4760, "2026-03-03", "Beispiel Hosting GmbH")
        einspielen(conn, ordner, self.BUCHUNG)
        # Danach im Tool zurueckgenommen: verknuepft, aber unbezahlt.
        conn.execute("DELETE FROM zahlung WHERE beleg_id = ?", (beleg_id,))
        gruende = [g for z in wiso.abgleich(conn, ordner, JAHR).zeilen for g in z.gruende]
        assert any("im Tool unbezahlt" in g for g in gruende)


# ------------------------------- Befund: Zahlungsziel ueber sieben Tage

class TestZahlungsziel:
    BUCHUNG = [b.Buchung(1, "15.03.2026", "Online-Kurs Beispiel Akademie",
                         ("4945", "Fortbildungskosten"), soll="299,00")]

    def test_beleg_mit_zahlungsziel_wird_angeboten(self, conn, betrieb, ordner):
        from tests.test_wiso_import import _beleg

        beleg_id = _beleg(conn, "FORTBILDUNG", 29900, "2026-03-01",
                          lieferant="Beispiel Akademie GmbH")
        p = posten(planen(conn, self.BUCHUNG), 1)
        assert (p.kandidat.id, p.kandidat.sicher) == (beleg_id, False)
        assert "14 Tage" in p.kandidat.text
        # Nicht vorbelegt -- aber waehlbar.
        assert p.vorschlag == wiso.UEBERNEHMEN
        _, ergebnis = einspielen(conn, ordner, self.BUCHUNG, auswahl=wiso.Auswahl(
            aktionen={"1-1": wiso.VERKNUEPFEN}))
        assert ergebnis.verknuepft == 1 and stand(conn)[1] == 29900

    def test_ohne_aehnlichen_namen_kein_weiter_treffer(self, conn, betrieb):
        from tests.test_wiso_import import _beleg

        _beleg(conn, "FORTBILDUNG", 29900, "2026-03-01", lieferant="Muster Verlag")
        assert posten(planen(conn, self.BUCHUNG), 1).kandidat is None

    def test_mehr_als_45_tage_kein_treffer(self, conn, betrieb):
        from tests.test_wiso_import import _beleg

        _beleg(conn, "FORTBILDUNG", 29900, "2026-01-20", lieferant="Beispiel Akademie GmbH")
        assert posten(planen(conn, self.BUCHUNG), 1).kandidat is None


# ------------------- Befund: in WISO geaendert, im Tool aber schon geloescht

class TestGeaendertImToolGeloescht:
    BUCHUNG = [b.Buchung(1, "15.03.2026", "Online-Kurs Beispiel Akademie",
                         ("4945", "Fortbildungskosten"), soll="299,00")]

    def test_vorschau_sagt_es_und_schlaegt_ignorieren_vor(self, conn, betrieb, ordner):
        from app.logik.beleg import loeschen
        from tests.test_wiso_import import _beleg

        _beleg(conn, "FORTBILDUNG", 29900, "2026-03-01", lieferant="Beispiel Akademie GmbH")
        einspielen(conn, ordner, self.BUCHUNG)
        assert stand(conn)[1] == 59800
        loeschen.loeschen(conn, aktiv(conn, 1)["beleg_id"], grund="doppelt erfasst")
        assert stand(conn)[1] == 29900
        folge = [replace(self.BUCHUNG[0], text="Online-Kurs Beispiel Akademie (Fotografie)")]
        p = posten(planen(conn, folge), 1)
        assert (p.gruppe, p.im_tool_geloescht, p.vorschlag) == (
            wiso.GEAENDERT, True, wiso.IGNORIEREN)
        _, ergebnis = einspielen(conn, ordner, folge)
        assert ergebnis.ignoriert == 1 and stand(conn)[1] == 29900

    def test_vorschau_zeigt_im_tool_geloescht(self, klient, tmp_path):
        from tests.test_wiso_import import datenbank, felder, hochladen

        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient, b.datei(self.BUCHUNG)).text))
        conn = datenbank(tmp_path)
        beleg_id = conn.execute("SELECT beleg_id FROM wiso_buchung").fetchone()[0]
        klient.post(f"/belege/{beleg_id}/loeschen", data={"grund": "doppelt erfasst"})
        folge = [replace(self.BUCHUNG[0], text="Online-Kurs Beispiel Akademie (Fotografie)")]
        seite = hochladen(klient, b.datei(folge)).text
        assert "im Tool schon gelöscht" in seite
        assert "trotzdem neu anlegen" in seite


# ------------------- Befund: Datei geht beim Ersetzen einer geaenderten Buchung verloren

class TestGeaendertMitDatei:
    ERST = [b.Buchung(2, "10.06.2026", "Beispiel Hosting Server", SONSTIGE, soll="29,75")]

    def _mit_datei(self, conn, ordner) -> int:
        from app.logik.beleg import anhaengen, pipeline
        from tests.test_wiso_import import rechnung_xml

        einspielen(conn, ordner, self.ERST)
        beleg_id = aktiv(conn, 2)["beleg_id"]
        eingang = pipeline.aufnehmen(rechnung_xml(), "rechnung.xml", ordner)
        anhaengen.anhaengen(conn, ordner, beleg_id, pipeline.lies(conn, eingang, heute=HEUTE))
        return beleg_id

    def test_reine_textaenderung_schlaegt_ignorieren_vor(self, conn, betrieb, ordner):
        self._mit_datei(conn, ordner)
        folge = [replace(self.ERST[0], text="Beispiel Hosting Server Juni")]
        p = posten(planen(conn, folge), 2)
        assert (p.gruppe, p.vorschlag) == (wiso.GEAENDERT, wiso.IGNORIEREN)
        assert p.aenderungen == ["Text"]
        assert p.datei_am_alten == "rechnung.xml"

    def test_uebernehmen_traegt_die_datei_mit(self, conn, betrieb, ordner):
        from app.logik.beleg import anhaengen

        alt_id = self._mit_datei(conn, ordner)
        folge = [replace(self.ERST[0], text="Beispiel Hosting Server Juni", soll="31,75")]
        p = posten(planen(conn, folge), 2)
        assert p.vorschlag == wiso.SPAETER and "Betrag" in p.aenderungen
        einspielen(conn, ordner, folge, auswahl=wiso.Auswahl(aktionen={"2-1": wiso.UEBERNEHMEN}))
        neu_id = aktiv(conn, 2)["beleg_id"]
        assert neu_id != alt_id
        assert not anhaengen.ohne_datei(conn, neu_id)
        alt, neu = (conn.execute("SELECT pfad, sha256 FROM belegdatei WHERE beleg_id = ?",
                                 (i,)).fetchone() for i in (alt_id, neu_id))
        assert tuple(alt) == tuple(neu)
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion ="
                      " 'BELEG_DATEI_UEBERTRAGEN' AND objekt_id = ?", neu_id) == 1
        assert stand(conn)[1] == 3175


# ------------------- Befund: Einstufung je Zahler zwischen Import und Hand-Erfassung

class TestZahlerSchluesselGeteilt:
    BUCHUNGEN = [b.Buchung(1, "05.01.2026", "Amazon Affiliate", b.ERLOES, haben="100,00"),
                 b.Buchung(2, "06.01.2026", "Beispiel Partnerprogramm", b.ERLOES, haben="200,00")]

    def test_wiso_einstufung_gilt_fuer_die_hand_erfassung(self, conn, betrieb, ordner):
        from app.logik import einnahmen
        from app.routen.einnahmen import _kategorie_fuer
        from tests.test_wiso_import import kat

        einspielen(conn, ordner, self.BUCHUNGEN, auswahl=wiso.Auswahl(
            zahler={"amazon": einnahmen.INLAND, "beispiel": einnahmen.AUSLAND}))
        assert stand(conn)[2] == 10000
        kategorie, woher = _kategorie_fuer(conn, "", "Amazon Auszahlung")
        assert kategorie == kat(conn, "EINNAHME_HONORAR_INLAND") and "gemerkt" in woher
        kategorie, woher = _kategorie_fuer(conn, "", "Beispiel Partnerprogramm")
        assert kategorie == kat(conn, "EINNAHME_HONORAR_AUSLAND") and "gemerkt" in woher

    def test_hand_gemerkt_gilt_fuer_den_import(self, conn, betrieb):
        from app.logik import einnahmen
        from tests.test_wiso_import import kat

        einnahmen.erfassen(conn, zufluss_am="2026-02-01", betrag_cent=31000,
                           zahler="Amazon Auszahlung",
                           kategorie_id=kat(conn, "EINNAHME_HONORAR_INLAND"),
                           merken_fuer_zahler=True, heute=HEUTE)
        assert einnahmen.gemerkt(conn, "amazon") is not None
        assert einnahmen.gemerkt(conn, "amazonauszahlung") is None
        p = posten(planen(conn, [b.Buchung(1, "05.03.2026", "Amazon Auszahlung", b.ERLOES,
                                           haben="50,00")]), 1)
        assert (p.einstufung, p.kategorie_herkunft) == (einnahmen.INLAND, "Zahler")
        assert next(z for z in planen(conn, [b.Buchung(1, "05.03.2026", "Amazon Auszahlung",
                                                       b.ERLOES, haben="50,00")]).zahler
                    ).herkunft == "gemerkt"

    def test_geraeteverkauf_ueberschreibt_nicht_die_plattform(self, conn, betrieb, ordner):
        from app.logik import einnahmen

        buchungen = [*self.BUCHUNGEN,
                     b.Buchung(3, "07.01.2026", "Amazon Echo - Verkauf", b.ERLOES, haben="40,00")]
        einspielen(conn, ordner, buchungen, auswahl=wiso.Auswahl(
            zahler={"amazon": einnahmen.AUSLAND}))
        assert einnahmen.einstufung_der_kategorie(
            conn, einnahmen.gemerkt(conn, "amazon")["kategorie_id"]) == einnahmen.AUSLAND
        assert einnahmen.einstufung_der_kategorie(
            conn, einnahmen.gemerkt(conn, "amazonecho")["kategorie_id"]
        ) == einnahmen.ANLAGENVERKAUF


# ------------------- Befund: Vorbelegung "Verkauf" und Plattformen

class TestVerkaufVorbelegung:
    @pytest.mark.parametrize(("text", "name", "einstufung"), [
        ("Verkaufsprovision Beispiel GmbH", "Verkaufsprovision Beispiel GmbH", "INLAND"),
        ("Provision Verkauf Beispielshop", "Beispielshop", "INLAND"),
        ("Apple Watch - Verkauf", "Apple Watch", "ANLAGENVERKAUF"),
        ("Amazon Echo - Verkauf", "Amazon Echo", "ANLAGENVERKAUF"),
        ("Google Pixel Verkauf", "Google Pixel", "ANLAGENVERKAUF"),
        ("Verkauf Kamera Beispiel", "Kamera Beispiel", "ANLAGENVERKAUF"),
        ("Rad Beispiel - Verkauf", "Rad Beispiel", "ANLAGENVERKAUF"),
        ("Amazon Affiliate", "Amazon", "AUSLAND"),
    ])
    def test_vorbelegung(self, text, name, einstufung):
        from app.logik import einnahmen

        z = einnahmen.zahler_aus_text(text)
        assert (z.name, z.vorschlag) == (name, einstufung)
        if einstufung == "ANLAGENVERKAUF":
            # Nie derselbe Zahler wie die Plattform-Auszahlungen.
            plattform = {einnahmen.zahler_aus_text(t).schluessel for t in (
                "Amazon Affiliate", "Apple App Store", "YouTube Ads")}
            assert z.schluessel not in plattform


# ------------------- Befund: Erstattung in KZ 111, aber nicht nachrichtlich in KZ 119

class TestErstattungKz119:
    def test_kz_111_minus_119_ist_der_19_umsatz(self, conn, betrieb, ordner):
        einspielen(conn, ordner, [
            b.Buchung(1, "05.01.2026", "Beispiel Partnerprogramm", b.ERLOES, haben="250,00"),
            b.Buchung(2, "06.01.2026", "Amazon Affiliate", b.ERLOES, haben="100,00"),
            b.Buchung(3, "27.03.2026", "Beispiel Newsletter", BUERO, haben="41,01"),
        ])
        a = euer.auswertung(conn, JAHR)
        kz111 = sum(z.betrag_cent for z in a.einnahmen
                    if z.kennzahl == euer.KZ_BETRIEBSEINNAHMEN and not z.nachrichtlich)
        kz119 = sum(z.betrag_cent for z in a.einnahmen
                    if z.kennzahl == euer.KZ_NICHT_STEUERBAR and z.nachrichtlich)
        assert (kz111, kz119) == (39101, 14101)
        assert kz111 - kz119 == grenzwert.vereinnahmt_cent(conn, JAHR) == 25000


# ------------------- Befund: abgeleitete Nutzungsdauer fuer ein Vorjahresgut mehrdeutig

class TestNutzungsdauerVorjahresgut:
    AFA = [b.Buchung(1, "31.12.2026", "AfA: Kamera Beispiel [Anl.-Nr.: 2]",
                     ("490", "Sonstige Betriebs- und Geschäftsausstattung"), haben="500,00",
                     gegenkonto=b.AFA)]

    @pytest.mark.parametrize(("ak", "tag", "afa", "jahr", "erwartet"), [
        (150000, "2025-05-10", 50000, 2026, [1, 3]),
        (120000, "2025-04-01", 30000, 2026, [1, 4]),
        (200000, "2024-07-01", 50000, 2026, [2, 4]),
        (120000, "2025-01-02", 40000, 2026, [3]),
        (120000, "2026-04-15", 30000, 2026, [3]),
    ])
    def test_kandidaten(self, ak, tag, afa, jahr, erwartet):
        from app.logik import anlagen

        assert [d for d, _ in anlagen.nutzungsdauer_kandidaten(ak, tag, afa, jahr=jahr)] == \
            erwartet
        eindeutig = anlagen.nutzungsdauer_ableiten(ak, tag, afa, jahr=jahr)
        assert (eindeutig[0] if eindeutig else None) == (erwartet[0] if len(erwartet) == 1
                                                         else None)

    def test_mehrdeutig_wird_gefragt_mit_kandidaten(self, conn, betrieb, ordner):
        from app.logik import angaben, anlagen

        einspielen(conn, ordner, self.AFA)
        angabe = angaben.offene(conn, "ANLAGEGUT")[0]
        with pytest.raises(angaben.AngabenFehler) as fehler:
            angaben.anlagegut_ergaenzen(conn, angabe.id, anschaffung_am="10.05.2025",
                                        ak_brutto_cent=150000, nutzungsdauer_jahre=None,
                                        heute=HEUTE)
        assert "1 Jahr" in str(fehler.value) and "3 Jahre" in str(fehler.value)
        assert "Nutzungsdauer angeben" in str(fehler.value)
        assert anzahl(conn, "SELECT COUNT(*) FROM anlagegut") == 0
        fertig = angaben.anlagegut_ergaenzen(conn, angabe.id, anschaffung_am="10.05.2025",
                                             ak_brutto_cent=150000, nutzungsdauer_jahre=3,
                                             heute=HEUTE)
        assert "AfA 2026 wie in WISO" in fertig.text
        gut_id = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        assert [anlagen.afa_summe(conn, j).get(anlagen.KZ_BEWEGLICH, 0)
                for j in (2026, 2027, 2028)] == [50000, 50000, 16667]
        # Die Vorjahre stehen nur zur Uebersicht: die Dauer bleibt korrigierbar.
        anlagen.nutzungsdauer_aendern(conn, gut_id, 4, laufend=2026)
        assert conn.execute("SELECT nutzungsdauer_jahre FROM anlagegut").fetchone()[0] == 4

    def test_sonst_bleibt_ein_vorjahr_gesperrt(self, conn, betrieb):
        from app.logik import anlagen

        gut_id = anlagen.anlegen(conn, bezeichnung="Beispiel Rechner", anschaffung_am="2025-05-10",
                                 ak_brutto_cent=150000, nutzungsdauer_jahre=3)
        with pytest.raises(anlagen.AnlagenFehler, match="2025"):
            anlagen.nutzungsdauer_aendern(conn, gut_id, 4, laufend=2026)


# ------------------- Befund: Eingangsordner kennt die WISO-Belege ohne Datei nicht

class TestOrdnerKenntWisoBelege:
    @pytest.fixture
    def umgebung(self, tmp_path, monkeypatch):
        from app import db
        from app.logik.beleg import abholen

        monkeypatch.setattr(abholen, "MINDESTALTER_SEKUNDEN", 0)
        monkeypatch.setattr(abholen, "UNLESBAR_MINDESTALTER_SEKUNDEN", 0, raising=False)
        abholen._NACHZUHOLEN.clear()
        daten = tmp_path / "daten"
        daten.mkdir()
        postfach = tmp_path / "Steuer-Tool Belege"
        postfach.mkdir()
        verbindung = db.oeffne(daten / "steuer.sqlite")
        verbindung.execute(
            "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, ust_idnr,"
            " belegordner_pfad) VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095',"
            " 'Hamburg', '12/345/67890', 'DE999999999', ?)", (str(postfach),))
        # Ein bekannter Lieferant mit gemerkter Kategorie -- sonst bucht das
        # Abholen ohnehin nicht.
        bo.lieferant(verbindung, name="Beispiel Hosting GmbH")
        yield verbindung, daten, postfach
        verbindung.close()
        abholen._NACHZUHOLEN.clear()

    @staticmethod
    def rechnung() -> bytes:
        return bo.cii().replace(b"Hetzner Online GmbH", b"Beispiel Hosting GmbH")

    def test_rechnung_zum_wiso_beleg_wird_nicht_zweimal_gebucht(self, umgebung):
        from app.logik.beleg import abholen, anhaengen

        conn, daten, postfach = umgebung
        einspielen(conn, daten, [b.Buchung(1, "16.03.2026", "Cloud Server Maerz - Beispiel "
                                           "Hosting", SONSTIGE, soll="5,46")])
        assert stand(conn)[1] == 546 and anhaengen.ohne_datei_aus_wiso(conn) == 1
        bo.lege(postfach, "rechnung.xml", self.rechnung())
        ergebnis = abholen.abholen(conn, daten, postfach, automatisch=True, heute=HEUTE)
        assert ergebnis.gebucht == []
        assert any("WISO-Eintrag Nr. 1" in w.grund for w in ergebnis.wartend)
        assert stand(conn)[1] == 546
        assert anzahl(conn, "SELECT COUNT(*) FROM v_beleg_gueltig") == 1

    def test_ohne_wiso_beleg_bucht_das_abholen_weiter(self, umgebung):
        from app.logik.beleg import abholen

        conn, daten, postfach = umgebung
        bo.lege(postfach, "rechnung.xml", self.rechnung())
        ergebnis = abholen.abholen(conn, daten, postfach, automatisch=True, heute=HEUTE)
        assert len(ergebnis.gebucht) == 1


# ------------------- Befund: Eingabefehler beim Erfassen einer Einnahme

class TestEinnahmeFormular:
    def test_fehler_zeigt_formular_mit_eingaben(self, klient, tmp_path):
        antwort = klient.post("/einnahmen", data={
            "zufluss_am": "2026-10-01", "betrag": "12,5x", "zahler": "Beispiel Partnerprogramm",
            "notiz": "Auszahlung September", "kategorie_id": ""}, follow_redirects=False)
        assert antwort.status_code == 422
        seite = antwort.text
        # Die Meldung steht im Formular (unter #neu), nicht nur oben auf der Seite.
        formular = seite[seite.index('id="neu"'):]
        assert "Betrag:" in formular[:3000]
        assert 'action="/einnahmen#neu"' in seite
        assert 'value="Beispiel Partnerprogramm"' in formular
        assert 'value="2026-10-01"' in formular and 'value="12,5x"' in formular
        assert "Auszahlung September</textarea>" in formular

    def test_notiz_und_grund_haben_eine_grenze(self, conn, betrieb):
        from app.logik import einnahmen
        from tests.test_wiso_import import kat

        with pytest.raises(einnahmen.EinnahmeFehler, match="Notiz"):
            einnahmen.erfassen(conn, zufluss_am="2026-02-01", betrag_cent=1000, zahler="Beispiel",
                               kategorie_id=kat(conn, "EINNAHME_HONORAR_INLAND"),
                               notiz="x" * 1001, heute=HEUTE)
        einnahme_id = einnahmen.erfassen(conn, zufluss_am="2026-02-01", betrag_cent=1000,
                                         zahler="Beispiel",
                                         kategorie_id=kat(conn, "EINNAHME_HONORAR_INLAND"),
                                         notiz="x" * 1000, heute=HEUTE)
        with pytest.raises(einnahmen.EinnahmeFehler, match="Grund"):
            einnahmen.loeschen(conn, einnahme_id, grund="y" * 201)
        assert einnahmen.loeschen(conn, einnahme_id, grund="y" * 200)

    def test_route_kuerzt_lange_eingaben_nicht_still(self, klient, tmp_path):
        antwort = klient.post("/einnahmen", data={
            "zufluss_am": "2026-10-01", "betrag": "9,00", "zahler": "Beispiel Partner",
            "notiz": "n" * 5000}, follow_redirects=False)
        assert antwort.status_code == 422 and "Notiz" in antwort.text
        from tests.test_wiso_import import datenbank

        assert datenbank(tmp_path).execute("SELECT COUNT(*) FROM einnahme").fetchone()[0] == 0


# ------------------- Befund: Dublette beim Erfassen von Hand

class TestEinnahmeDublette:
    WISO = [b.Buchung(1, "10.01.2026", "Beispiel Partnerprogramm", b.ERLOES, haben="250,00")]

    def test_logik_fragt_nach(self, conn, betrieb, ordner):
        from app.logik import einnahmen
        from tests.test_wiso_import import kat

        einspielen(conn, ordner, self.WISO)
        werte = {"zufluss_am": "2026-01-10", "betrag_cent": 25000,
                 "zahler": "Beispiel Partnerprogramm",
                 "kategorie_id": kat(conn, "EINNAHME_HONORAR_INLAND"), "heute": HEUTE}
        with pytest.raises(einnahmen.AehnlicheEinnahme) as fehler:
            einnahmen.erfassen(conn, **werte)
        assert [z["id"] for z in fehler.value.aehnliche] == [aktiv(conn, 1)["einnahme_id"]]
        assert stand(conn) == (25000, 0, 25000)
        einnahmen.erfassen(conn, **werte, trotz_aehnlicher=True)
        assert stand(conn) == (50000, 0, 50000)

    def test_seite_fragt_nach(self, klient, tmp_path):
        from tests.test_wiso_import import datenbank, felder, hochladen

        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient, b.datei(self.WISO)).text))
        daten = {"zufluss_am": "2026-01-10", "betrag": "250,00",
                 "zahler": "Beispiel Partnerprogramm", "kategorie_id": ""}
        antwort = klient.post("/einnahmen", data=daten, follow_redirects=False)
        assert antwort.status_code == 409
        assert "Vermutlich schon erfasst" in antwort.text and "Einnahme 1" in antwort.text
        assert 'name="trotzdem"' in antwort.text
        conn = datenbank(tmp_path)
        assert conn.execute("SELECT COUNT(*) FROM einnahme").fetchone()[0] == 1
        antwort = klient.post("/einnahmen", data={**daten, "trotzdem": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert conn.execute("SELECT COUNT(*) FROM einnahme").fetchone()[0] == 2


# ------------------- Befund: Zahlung auf eine eigene Rechnung nach dem WISO-Import

class TestRechnungszahlungDublette:
    WISO = [b.Buchung(1, "11.01.2026", "Beispiel Partner GmbH Rechnung RE-2026-0001", b.ERLOES,
                      haben="250,00")]

    @staticmethod
    def rechnung(tmp_path) -> int:
        """Eine festgeschriebene Rechnung ueber 250,00 € ohne Zahlung."""
        from app import db
        from tests.test_wiso_import import datenbank

        conn = datenbank(tmp_path)
        conn.execute("BEGIN")
        kunde = conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
            " VALUES ('K-0100', 'Beispiel Partner GmbH', 'Musterstraße 1', '12345',"
            " 'Musterstadt', '2026-01-01')").lastrowid
        rechnung_id = conn.execute(
            "INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,"
            " netto_cent, brutto_cent, leistungsort, betreff, status, festgeschrieben_am,"
            " erstellt_am) VALUES ('RECHNUNG', 'RE-2026-0001', 1, 2026, ?, '2026-01-02', 25000,"
            " 25000, 'INLAND', 'Leistung Januar', 'FESTGESCHRIEBEN', ?, ?)",
            (kunde, db.jetzt_utc(), db.jetzt_utc())).lastrowid
        conn.execute("COMMIT")
        return rechnung_id

    def test_fragt_nach_und_ersetzt(self, klient, tmp_path):
        from tests.test_wiso_import import datenbank, felder, hochladen

        rechnung_id = self.rechnung(tmp_path)
        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient, b.datei(self.WISO)).text))
        conn = datenbank(tmp_path)
        einnahme_id = conn.execute("SELECT einnahme_id FROM wiso_buchung").fetchone()[0]
        daten = {"datum": "2026-01-11", "betrag_euro": "250,00", "art": "UEBERWEISUNG",
                 "erwartet_bezahlt": "0"}
        antwort = klient.post(f"/rechnung/{rechnung_id}/zahlung", data=daten,
                              follow_redirects=False)
        assert antwort.status_code == 409
        assert "Vermutlich schon als Einnahme erfasst" in antwort.text
        assert f"Einnahme {einnahme_id}" in antwort.text
        assert conn.execute("SELECT COUNT(*) FROM zahlung").fetchone()[0] == 0
        antwort = klient.post(f"/rechnung/{rechnung_id}/zahlung", data={
            **daten, "dublette": "ersetzen", "dublette_einnahme": str(einnahme_id)},
            follow_redirects=False)
        assert antwort.status_code == 303
        a = euer.auswertung(conn, JAHR)
        assert summe_einnahmen(a) == 25000 and grenzwert.vereinnahmt_cent(conn, JAHR) == 25000
        assert conn.execute("SELECT geloescht_am IS NOT NULL FROM einnahme WHERE id = ?",
                            (einnahme_id,)).fetchone()[0] == 1
        # Die WISO-Buchung haengt jetzt an der Zahlung: der naechste Import kennt sie.
        zahlung_id = conn.execute("SELECT id FROM zahlung").fetchone()[0]
        zeile = conn.execute("SELECT * FROM wiso_buchung WHERE status = 'AKTIV'").fetchone()
        assert (zeile["zahlung_id"], zeile["verknuepft"]) == (zahlung_id, 1)
        plan = wiso.plane(conn, wiso.lies(NAME, b.datei(self.WISO), heute=HEUTE), heute=HEUTE)
        assert plan.posten[0].gruppe == wiso.BEKANNT

    def test_andere_zahlung_zusaetzlich(self, klient, tmp_path):
        from tests.test_wiso_import import datenbank, felder, hochladen

        rechnung_id = self.rechnung(tmp_path)
        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient, b.datei(self.WISO)).text))
        antwort = klient.post(f"/rechnung/{rechnung_id}/zahlung", data={
            "datum": "2026-01-11", "betrag_euro": "250,00", "art": "UEBERWEISUNG",
            "erwartet_bezahlt": "0", "dublette": "zusaetzlich"}, follow_redirects=False)
        assert antwort.status_code == 303
        conn = datenbank(tmp_path)
        assert summe_einnahmen(euer.auswertung(conn, JAHR)) == 50000

    def test_ohne_aehnliche_einnahme_wie_bisher(self, klient, tmp_path):
        rechnung_id = self.rechnung(tmp_path)
        antwort = klient.post(f"/rechnung/{rechnung_id}/zahlung", data={
            "datum": "2026-01-11", "betrag_euro": "250,00", "art": "UEBERWEISUNG",
            "erwartet_bezahlt": "0"}, follow_redirects=False)
        assert antwort.status_code == 303


# ------------------- Befund: Verkauf eines Anlageguts doppelt in KZ 102

class TestAnlagenverkauf:
    BUCHUNGEN = [
        b.Buchung(10, "15.04.2026", "Plotter Beispiel [Anl.-Nr.: 1]", ("470", "Computeranlagen"),
                  soll="1200,00"),
        b.Buchung(11, "31.12.2026", "AfA: Plotter Beispiel [Anl.-Nr.: 1]",
                  ("470", "Computeranlagen"), haben="300,00", gegenkonto=b.AFA),
        b.Buchung(14, "20.09.2026", "Plotter Beispiel - Verkauf", b.ERLOES, haben="700,00"),
    ]

    @staticmethod
    def kz102(conn) -> int:
        from app.logik import anlagen

        return sum(z.betrag_cent for z in euer.auswertung(conn, JAHR).einnahmen
                   if z.kennzahl == anlagen.KZ_ANLAGENVERKAUF and not z.nachrichtlich)

    def test_erloes_schon_als_einnahme_zaehlt_einmal(self, conn, betrieb, ordner):
        from app.logik import anlagen

        einspielen(conn, ordner, self.BUCHUNGEN)
        gut_id = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        einnahme_id = aktiv(conn, 14)["einnahme_id"]
        assert self.kz102(conn) == 70000
        # Erloes von Hand eingetippt, obwohl er als Einnahme da ist: abgewiesen.
        with pytest.raises(anlagen.AnlagenFehler, match=f"Einnahme {einnahme_id}"):
            anlagen.abgang(conn, gut_id, am="2026-09-20", grund="verkauft", erloes_cent=70000)
        anlagen.abgang(conn, gut_id, am="2026-09-20", grund="verkauft",
                       erloes_einnahme_id=einnahme_id)
        assert self.kz102(conn) == 70000
        gut = conn.execute("SELECT abgang_erloes_cent FROM anlagegut").fetchone()
        assert gut[0] == 70000
        assert conn.execute("SELECT anlagegut_id FROM einnahme WHERE id = ?",
                            (einnahme_id,)).fetchone()[0] == gut_id
        # Restbuchwert als Ausgabe ist da (die AfA 2026 bis September).
        _, restbuchwert = anlagen.abgaenge(conn, JAHR)
        assert restbuchwert > 0
        # Ruecknahme loest die Verbindung.
        anlagen.abgang_zuruecknehmen(conn, gut_id, grund="Datum vertippt")
        assert conn.execute("SELECT anlagegut_id FROM einnahme WHERE id = ?",
                            (einnahme_id,)).fetchone()[0] is None
        assert self.kz102(conn) == 70000

    def test_anderer_verkauf_ausdruecklich(self, conn, betrieb, ordner):
        from app.logik import anlagen

        einspielen(conn, ordner, self.BUCHUNGEN)
        gut_id = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        anlagen.abgang(conn, gut_id, am="2026-09-20", grund="verkauft", erloes_cent=70000,
                       erloes_neu=True)
        assert self.kz102(conn) == 140000

    def test_seite_bietet_die_einnahme_an(self, klient, tmp_path):
        from tests.test_wiso_import import datenbank, felder, hochladen

        klient.post("/wiso/uebernehmen", data=felder(hochladen(
            klient, b.datei(self.BUCHUNGEN)).text))
        conn = datenbank(tmp_path)
        gut_id = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        einnahme_id = conn.execute("SELECT einnahme_id FROM wiso_buchung WHERE"
                                   " buchungsnummer = '14'").fetchone()[0]
        seite = klient.get("/anlagen?jahr=2026").text
        assert "Verkauf ohne Abgang" in seite
        assert f'<option value="{einnahme_id}"' in seite
        antwort = klient.post(f"/anlagen/{gut_id}/abgang", data={
            "am": "2026-09-20", "grund": "verkauft", "erloes_euro": "",
            "erloes_einnahme": str(einnahme_id)}, follow_redirects=False)
        assert "erfolg=" in antwort.headers["location"]
        assert self.kz102(conn) == 70000


# ------------------- Befund: "Datei anhängen" blockierte die Ereignisschleife

class TestAnhaengenBlockiertNicht:
    def test_lesen_laeuft_nicht_in_der_ereignisschleife(self, klient, tmp_path, monkeypatch):
        import asyncio

        from app.routen import belegkorrektur
        from tests.test_wiso_import import NEU_18, datenbank, felder, hochladen, rechnung_xml

        klient.post("/wiso/uebernehmen", data=felder(hochladen(
            klient, b.datei([*b.BUCHUNGEN, NEU_18])).text))
        beleg_id = datenbank(tmp_path).execute(
            "SELECT beleg_id FROM wiso_buchung WHERE buchungsnummer = '18'").fetchone()[0]
        gesehen: list[bool] = []
        echt_lies, echt_aufnehmen = belegkorrektur._lies, belegkorrektur.pipeline.aufnehmen

        def in_der_schleife() -> bool:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return False
            return True

        def lies(*args, **kwargs):
            gesehen.append(in_der_schleife())
            return echt_lies(*args, **kwargs)

        def aufnehmen(*args, **kwargs):
            gesehen.append(in_der_schleife())
            return echt_aufnehmen(*args, **kwargs)

        monkeypatch.setattr(belegkorrektur, "_lies", lies)
        monkeypatch.setattr(belegkorrektur.pipeline, "aufnehmen", aufnehmen)
        antwort = klient.post(f"/belege/{beleg_id}/datei-anhaengen", files={
            "datei": ("rechnung.xml", rechnung_xml(), "application/xml")},
            follow_redirects=False)
        assert antwort.status_code == 303 and "erfolg=" in antwort.headers["location"]
        # Aufnehmen und Lesen (Textschicht bis 30 s) laufen im Threadpool --
        # in der Ereignisschleife stuende sonst der ganze Server.
        assert gesehen == [False, False]


# ------------------- Befund: Skript in einer XML-Abrechnung lief im Ursprung des Tools

XHTML = (b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
         b"<script>document.title='SKRIPT LAEUFT'</script></body></html>")
MAIL = (b"From: abrechnung@beispiel.example\r\nDate: Thu, 01 Oct 2026 10:00:00 +0200\r\n"
        b"Subject: Abrechnung September\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>Auszahlung <script>alert(1)</script></p>\r\n")


class TestDateienOhneSkript:
    @staticmethod
    def einnahme_mit(klient, name: str, inhalt: bytes) -> int:
        antwort = klient.post("/einnahmen", data={
            "zufluss_am": "2026-10-01", "betrag": "9,00", "zahler": "Beispiel Partner"},
            files={"datei": (name, inhalt, "application/octet-stream")},
            follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:500]
        return int(re.search(r"Einnahme%20(\d+)", antwort.headers["location"]).group(1))

    def test_xml_einer_einnahme_als_text(self, klient):
        einnahme_id = self.einnahme_mit(klient, "abrechnung.xml", XHTML)
        antwort = klient.get(f"/einnahmen/{einnahme_id}/datei")
        assert antwort.status_code == 200
        assert antwort.headers["content-type"].startswith("text/plain")
        assert antwort.headers["x-content-type-options"] == "nosniff"
        csp = antwort.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "sandbox" in csp
        # Herunterladen bleibt moeglich -- als Datei, nicht im Browser.
        laden = klient.get(f"/einnahmen/{einnahme_id}/datei?download=1")
        assert laden.headers["content-disposition"].startswith("attachment")
        assert laden.content == XHTML

    def test_mail_einer_einnahme_als_textansicht(self, klient):
        einnahme_id = self.einnahme_mit(klient, "abrechnung.eml", MAIL)
        antwort = klient.get(f"/einnahmen/{einnahme_id}/datei")
        assert antwort.status_code == 200
        assert antwort.headers["content-type"].startswith("text/html")
        assert "default-src 'none'" in antwort.headers["content-security-policy"]
        assert "Abrechnung September" in antwort.text and "<script>alert" not in antwort.text

    def test_pdf_bleibt_pdf(self, klient):
        from tests.test_einnahmen import PDF

        einnahme_id = self.einnahme_mit(klient, "abrechnung.pdf", PDF)
        antwort = klient.get(f"/einnahmen/{einnahme_id}/datei")
        assert antwort.headers["content-type"] == "application/pdf"
        assert antwort.headers["x-content-type-options"] == "nosniff"

    def test_xml_an_einem_beleg_als_text(self, klient, tmp_path):
        from tests.test_wiso_import import NEU_18, datenbank, felder, hochladen

        klient.post("/wiso/uebernehmen", data=felder(hochladen(
            klient, b.datei([*b.BUCHUNGEN, NEU_18])).text))
        beleg_id = datenbank(tmp_path).execute(
            "SELECT beleg_id FROM wiso_buchung WHERE buchungsnummer = '18'").fetchone()[0]
        klient.post(f"/belege/{beleg_id}/datei-anhaengen", files={
            "datei": ("abrechnung.xml", XHTML, "application/xml")})
        antwort = klient.get(f"/belege/{beleg_id}/datei")
        assert antwort.status_code == 200
        assert antwort.headers["content-type"].startswith("text/plain")
        assert "sandbox" in antwort.headers["content-security-policy"]


# ------------------- Befund: "Angaben fehlen" -- Fehler ausserhalb des Fensters, Eingaben weg

class TestAngabenFormular:
    AFA = [b.Buchung(1, "31.12.2026", "AfA: Kamera Beispiel [Anl.-Nr.: 2]",
                     ("490", "Sonstige Betriebs- und Geschäftsausstattung"), haben="500,00",
                     gegenkonto=b.AFA)]

    def angabe(self, klient, tmp_path) -> int:
        from tests.test_wiso_import import datenbank, felder, hochladen

        klient.post("/wiso/uebernehmen", data=felder(hochladen(klient, b.datei(self.AFA)).text))
        return datenbank(tmp_path).execute(
            "SELECT id FROM fehlende_angabe WHERE art = 'ANLAGEGUT'").fetchone()[0]

    @pytest.mark.parametrize("ziel", ["/angaben", "/anlagen", "/wiso"])
    def test_fehler_in_der_karte_mit_eingaben(self, klient, tmp_path, ziel):
        kennung = self.angabe(klient, tmp_path)
        antwort = klient.post(f"/angaben/{kennung}/anlagegut", data={
            "anschaffung_am": "2024-03-15", "ak_brutto": "2.400,00 EUR",
            "nutzungsdauer_jahre": "7", "zurueck": ziel}, follow_redirects=False)
        ort = antwort.headers["location"]
        assert ort.startswith(ziel) and ort.endswith(f"#angabe-{kennung}")
        seite = klient.get(ort).text
        karte = seite[seite.index(f'id="angabe-{kennung}"'):]
        karte = karte[:karte.index("</form>")]
        assert "kein Betrag" in karte
        assert 'value="2024-03-15"' in karte and 'value="2.400,00 EUR"' in karte
        assert 'value="7"' in karte

    def test_gespeichert_mit_hinweis_ist_eine_warnung(self, klient, tmp_path):
        kennung = self.angabe(klient, tmp_path)
        antwort = klient.post(f"/angaben/{kennung}/anlagegut", data={
            "anschaffung_am": "2024-03-01", "ak_brutto": "2.000,00",
            "nutzungsdauer_jahre": "3", "zurueck": "/angaben"}, follow_redirects=False)
        assert "warnung=" in antwort.headers["location"]
        assert "fehler=" not in antwort.headers["location"]
        seite = klient.get(antwort.headers["location"]).text
        assert 'class="meldung warn"' in seite and "ist im Anlagenverzeichnis" in seite


# ------------------- Befund: ein Anfuehrungszeichen verschluckte alle Folgezeilen

class TestAnfuehrungszeichen:
    def test_anfuehrungszeichen_am_textanfang(self):
        buchungen = b.geaendert(3, text='"Beispiel Partnerprogramm')
        datei = wiso.lies(NAME, b.datei(buchungen), heute=HEUTE)
        assert (len(datei.zeilen), datei.fehler) == (17, [])
        assert next(z for z in datei.zeilen if z.buchungsnummer == "3").text == \
            '"Beispiel Partnerprogramm'

    def test_semikolon_im_text_kostet_nur_die_zeile(self):
        buchungen = b.geaendert(3, text="Beispiel; Partnerprogramm")
        datei = wiso.lies(NAME, b.datei(buchungen), heute=HEUTE)
        assert len(datei.zeilen) == 16
        # Zeile 1 ist der Kopf: Buchung 3 steht in Zeile 4.
        assert len(datei.fehler) == 1 and datei.fehler[0][0] == 4
        assert "19 statt 18 Felder" in datei.fehler[0][1]


# ------------------- Befund: aus jeder Adresse im Buchungstext wurde der Lieferant "https"

class TestLieferantAusAdresse:
    def test_zwei_shops_zwei_lieferanten(self, conn, betrieb, ordner):
        einspielen(conn, ordner, [
            b.Buchung(1, "05.03.2026", "https://www.beispiel-shop.example/rechnung", BUERO,
                      soll="12,00"),
            b.Buchung(2, "06.03.2026", "https://www.anderer-laden.example", BUERO, soll="8,00"),
        ])
        namen = sorted(z[0] for z in conn.execute("SELECT name FROM lieferant"))
        assert namen == ["anderer-laden.example", "beispiel-shop.example"]


# ------------------- Befund: zweites Fenster meldete "abgelaufen", obwohl uebernommen

class TestZweitesFenster:
    def test_schon_uebernommen_statt_abgelaufen(self, klient):
        from urllib.parse import unquote

        from tests.test_wiso_import import felder, hochladen

        erstes, zweites = felder(hochladen(klient).text), felder(hochladen(klient).text)
        assert klient.post("/wiso/uebernehmen", data=erstes,
                           follow_redirects=False).status_code == 303
        antwort = klient.post("/wiso/uebernehmen", data=zweites, follow_redirects=False)
        ort = unquote(antwort.headers["location"])
        assert "abgelaufen" not in ort
        assert "schon übernommen" in ort and "Abgleich" in ort
        assert "schon übernommen" in klient.get(antwort.headers["location"]).text


# ------------------- Befunde: Handy-Vorschau und lange Adresse in der Notiz

class TestDarstellung:
    def test_konten_und_zahler_als_bloecke_am_handy(self, klient):
        from tests.test_wiso_import import hochladen

        seite = hochladen(klient).text
        konten = seite[seite.index('id="konten"'):seite.index('id="zahler"')]
        zahler = seite[seite.index('id="zahler"'):seite.index('id="neu"')]
        assert '<table class="sammel">' in konten and 'data-label="Kategorie"' in konten
        assert '<table class="sammel">' in zahler and 'data-label="Einstufung"' in zahler

    def test_lange_notiz_bricht_um(self, klient):
        from pathlib import Path

        import app

        adresse = ("Abrechnung: https://partnerprogramm.beispiel.example/abrechnungen/2026/09/"
                   "auszahlung?konto=123456789&format=pdf&sprache=de&id=abcdef0123456789")
        klient.post("/einnahmen", data={"zufluss_am": "2026-10-01", "betrag": "9,00",
                                        "zahler": "Beispiel Partner", "notiz": adresse})
        seite = klient.get("/einnahmen?jahr=2026").text
        assert 'class="gedaempft notiz"' in seite
        css = (Path(app.__file__).parent / "static" / "stil.css").read_text(encoding="utf-8")
        assert "table.belegliste .notiz { overflow-wrap: anywhere; }" in css

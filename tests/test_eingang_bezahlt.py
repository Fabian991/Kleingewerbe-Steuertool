"""Bezahlt als Vorgabe: Eingangsbelege sind in der Regel schon bezahlt.

Geprueft wird die Einstellung (Migration 24, Journal), die Zahlung in
DERSELBEN Transaktion wie der Beleg (``pipeline.buche``), die Pruefmaske
(vorausgewaehlt "bezahlt am <Belegdatum>", "offen" nur ausdruecklich), das
Abholen aus dem Eingangsordner (Zahlung mit, ausser am Jahreswechsel) und
"weiter zur nächsten Datei". Alle Lieferanten und Betraege sind erfunden.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import euer, kategorien
from app.logik.beleg import abholen, bezahlt, pipeline
from app.logik.beleg.ergebnis import BelegFehler
from app.main import erzeuge_app
from tests import test_belegordner as bo
from tests import test_fremdwaehrung as fw

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
HEUTE = bo.HEUTE

# Die Fixtures aus test_belegordner.py: Datenordner, Eingangsordner, Datenbank
# mit Betrieb -- und die Altersgrenzen der Dateien im Eingangsordner.
datenordner = bo.datenordner
postfach = bo.postfach
conn = bo.conn
_dateien_gelten_als_fertig = bo._dateien_gelten_als_fertig
_nichts_nachzuholen = bo._nichts_nachzuholen


def anzahl(verbindung, sql: str, *werte) -> int:
    return verbindung.execute(sql, werte).fetchone()[0]


# --------------------------------------------------------- pipeline.buche

def _lesung(conn, datenordner, inhalt=None, name="Rechnung.xml"):
    eingang = pipeline.aufnehmen(inhalt or bo.cii(), name, datenordner)
    return pipeline.lies(conn, eingang, heute=HEUTE)


BESTAETIGT = {"belegdatum": "2026-03-14", "brutto": 546, "netto": 459, "ust": 87,
              "ust_satz_promille": 190, "waehrung": "EUR"}


class TestBuche:
    def test_zahlung_entsteht_mit_dem_beleg(self, conn, datenordner):
        """Mit ``bezahlt_am``: eine Zahlung ueber den Bruttobetrag (negativ),
        in der Zahlungsart der Buchung, mit Journaleintrag samt Begruendung --
        und der Zahltag steht im bestaetigten Ergebnis."""
        buchung = pipeline.buche(conn, _lesung(conn, datenordner), BESTAETIGT, datenordner,
                                 heute=HEUTE, bezahlt_am="2026-03-14", zahlungsart="KARTE",
                                 zahlung_begruendung=bezahlt.BEGRUENDUNG_BESTAETIGT)
        assert [tuple(z) for z in conn.execute(
            "SELECT beleg_id, datum, betrag_cent, art FROM zahlung")] == [
            (buchung.beleg_id, "2026-03-14", -546, "KARTE")]
        [journal] = conn.execute("SELECT * FROM journal WHERE aktion = 'ZAHLUNG'").fetchall()
        assert (journal["objekt_id"], journal["wert_neu"], journal["begruendung"]) == (
            buchung.beleg_id, "2026-03-14", bezahlt.BEGRUENDUNG_BESTAETIGT)
        extraktion = conn.execute("SELECT bestaetigt_json FROM extraktion").fetchone()[0]
        assert json.loads(extraktion)["bezahlt_am"] == "2026-03-14"
        assert conn.execute("SELECT zahlungsart FROM beleg").fetchone()[0] == "KARTE"

    def test_ohne_zahltag_keine_zahlung(self, conn, datenordner):
        pipeline.buche(conn, _lesung(conn, datenordner), BESTAETIGT, datenordner, heute=HEUTE)
        assert anzahl(conn, "SELECT COUNT(*) FROM zahlung") == 0

    def test_fehler_nach_der_zahlung_nimmt_beides_zurueck(self, conn, datenordner,
                                                         monkeypatch):
        """Scheitert etwas NACH dem Schreiben der Zahlung, gibt es weder Beleg
        noch Zahlung, und die Datei liegt wieder im Eingang -- vorher schrieb
        die Pruefmaske die Zahlung in einer zweiten Transaktion, und ein
        Fehler dort liess den Beleg ohne Zahlung stehen."""
        lesung = _lesung(conn, datenordner)
        echt = db.protokolliere

        def kaputt(verbindung, aktion, *args, **kwargs):
            if aktion == "ZAHLUNG":
                raise sqlite3.OperationalError("Platte voll")
            return echt(verbindung, aktion, *args, **kwargs)

        monkeypatch.setattr(db, "protokolliere", kaputt)
        with pytest.raises(sqlite3.OperationalError):
            pipeline.buche(conn, lesung, BESTAETIGT, datenordner, heute=HEUTE,
                           bezahlt_am="2026-03-14")
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0
        assert anzahl(conn, "SELECT COUNT(*) FROM zahlung") == 0
        assert lesung.eingang.pfad.is_file()

    @pytest.mark.parametrize("tag, meldung", [
        ("2026-10-25", "liegt in der Zukunft"), ("31.02.2026", "ist kein Datum")])
    def test_zahltag_wird_geprueft(self, conn, datenordner, tag, meldung):
        with pytest.raises(BelegFehler, match=meldung):
            pipeline.buche(conn, _lesung(conn, datenordner), BESTAETIGT, datenordner,
                           heute=HEUTE, bezahlt_am=tag)
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_fremdwaehrung_zahlung_ist_der_euro_betrag(self, conn, datenordner):
        """Eine Rechnung in Dollar, bezahlt am Belegdatum: umgerechnet zum
        EZB-Kurs dieses Tages, und die Zahlung ist genau der Euro-Betrag des
        Belegs -- Beleg und Zahlung widersprechen sich nicht."""
        fw.tabelle_ablegen(datenordner)
        lesung = _lesung(conn, datenordner, bo.cii("RE-USD-1", "20260313", waehrung="USD",
                                                   brutto="123.45", netto="103.74",
                                                   ust="19.71"))
        pipeline.buche(conn, lesung, {"belegdatum": "2026-03-13", "brutto": 12345,
                                      "waehrung": "USD"}, datenordner,
                       heute=fw.HEUTE, bezahlt_am="2026-03-13")
        beleg = conn.execute("SELECT * FROM beleg").fetchone()
        assert beleg["kurs_datum"] == "2026-03-13"
        assert beleg["betrag_fremdwaehrung_cent"] == 12345
        [zahlung] = conn.execute("SELECT * FROM zahlung").fetchall()
        assert zahlung["betrag_cent"] == -beleg["betrag_brutto_cent"]


# ------------------------------------------------------------ Pruefmaske

EINRICHTUNG = bo.EINRICHTUNG


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen():
        verbindung = sqlite3.connect(tmp_path / "steuer.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung

    return oeffnen


def _hochladen(klient, inhalt: bytes, name: str) -> str:
    antwort = klient.post("/belege/hochladen", files={"datei": (name, inhalt, "text/xml")},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text
    return antwort.headers["location"].rsplit("/", 1)[1]


def _feld(html: str, name: str) -> str:
    """Das ``<input>`` bzw. ``<select>`` eines Feldes als Text."""
    treffer = re.search(rf'<(?:input|select)[^>]*name="{name}"[^>]*>', html)
    assert treffer, name
    return treffer.group(0)


def _radio(html: str, wert: str) -> str:
    treffer = re.search(rf'<input type="radio" name="zahlung" value="{wert}"[^>]*>', html)
    assert treffer, wert
    return treffer.group(0)


FORMULAR = {"belegdatum": "2026-03-14", "brutto_euro": "5,46", "netto_euro": "4,59",
            "ust_euro": "0,87", "ust_satz": "19", "waehrung": "EUR",
            "lieferant_neu": "Beispiel Hosting GmbH", "quote": "100"}


class TestPruefmaske:
    def test_bezahlt_ist_vorausgewaehlt_mit_dem_belegdatum(self, klient):
        name = _hochladen(klient, bo.cii(), "Rechnung.xml")
        maske = klient.get(f"/belege/pruefen/{name}").text
        assert "checked" in _radio(maske, "bezahlt")
        assert "checked" not in _radio(maske, "offen")
        assert 'value="2026-03-14"' in _feld(maske, "bezahlt_am")
        # Ohne frühere Zahlung: die neutrale Zahlungsart, nicht "Überweisung".
        assert '<option value="SONSTIGES" selected>' in maske
        # Kein Jahreswechsel: der Hinweis ist versteckt.
        assert re.search(r'id="jahreswechsel" hidden', maske)

    def test_ohne_einstellung_ist_offen_vorausgewaehlt(self, klient):
        klient.post("/belege/vorgaben", data={})
        name = _hochladen(klient, bo.cii(), "Rechnung.xml")
        maske = klient.get(f"/belege/pruefen/{name}").text
        assert "checked" in _radio(maske, "offen")
        assert "checked" not in _radio(maske, "bezahlt")

    def test_bezahlt_ohne_datum_heisst_am_belegdatum(self, klient, datenbank):
        """Die Vorgabe bestaetigt: eine Zahlung am Belegdatum, ueber den
        Bruttobetrag, im Journal mit der Vorgabe als Begruendung -- und die
        Meldung sagt es."""
        name = _hochladen(klient, bo.cii(), "Rechnung.xml")
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **FORMULAR, "zahlung": "bezahlt", "bezahlt_am": "", "zahlungsart": "PAYPAL"},
            follow_redirects=True)
        assert "Bezahlt am 14.03.2026." in antwort.text
        assert "in keinem EÜR-Jahr" not in antwort.text
        with datenbank() as verbindung:
            assert [tuple(z) for z in verbindung.execute(
                "SELECT datum, betrag_cent, art FROM zahlung")] == [("2026-03-14", -546, "PAYPAL")]
            assert verbindung.execute(
                "SELECT begruendung FROM journal WHERE aktion = 'ZAHLUNG'").fetchone()[0] == \
                bezahlt.BEGRUENDUNG_BESTAETIGT

    def test_anderer_zahltag(self, klient, datenbank):
        """Ein anderer Tag als das Belegdatum ist kein Fall der Vorgabe -- die
        Begruendung entfaellt."""
        name = _hochladen(klient, bo.cii(), "Rechnung.xml")
        klient.post(f"/belege/pruefen/{name}", data={
            **FORMULAR, "zahlung": "bezahlt", "bezahlt_am": "2026-03-20"})
        with datenbank() as verbindung:
            assert [tuple(z) for z in verbindung.execute(
                "SELECT datum, betrag_cent FROM zahlung")] == [("2026-03-20", -546)]
            assert verbindung.execute(
                "SELECT begruendung FROM journal WHERE aktion = 'ZAHLUNG'").fetchone()[0] is None

    def test_offen_bucht_ohne_zahlung_auch_mit_datum_im_feld(self, klient, datenbank):
        name = _hochladen(klient, bo.cii(), "Rechnung.xml")
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **FORMULAR, "zahlung": "offen", "bezahlt_am": "2026-03-14"}, follow_redirects=True)
        assert "Ohne Zahlungsdatum zählt er noch in keinem EÜR-Jahr" in antwort.text
        with datenbank() as verbindung:
            assert verbindung.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1
            assert verbindung.execute("SELECT COUNT(*) FROM zahlung").fetchone()[0] == 0

    def test_ohne_auswahl_wie_bisher(self, klient, datenbank):
        """Ein Formular ohne die Auswahl (aelter, oder ein Aufruf von anderswo):
        ohne Datum keine Zahlung, mit Datum die Zahlung -- wie vorher."""
        erste = _hochladen(klient, bo.cii(), "a.xml")
        klient.post(f"/belege/pruefen/{erste}", data=FORMULAR)
        zweite = _hochladen(klient, bo.cii("RE-2026-0816", "20260320"), "b.xml")
        klient.post(f"/belege/pruefen/{zweite}", data={
            **FORMULAR, "belegdatum": "2026-03-20", "bezahlt_am": "2026-03-21"})
        with datenbank() as verbindung:
            assert [tuple(z) for z in verbindung.execute(
                "SELECT beleg_id, datum FROM zahlung")] == [(2, "2026-03-21")]

    def test_abgewiesene_eingabe_behaelt_die_auswahl(self, klient):
        """Nach einem abgewiesenen Buchen (422) gilt, was der Nutzer gewaehlt
        hatte -- nicht wieder die Vorgabe."""
        name = _hochladen(klient, bo.cii(), "Rechnung.xml")
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **FORMULAR, "brutto_euro": "abc", "zahlung": "offen", "bezahlt_am": "2026-03-14"})
        assert antwort.status_code == 422
        assert "checked" in _radio(antwort.text, "offen")

    def test_jahreswechsel_zeigt_den_hinweis(self, klient):
        """Belegdatum am 28.12.: der Hinweis steht sichtbar da, vorbelegt bleibt
        der Tag -- ein Klick, wenn er stimmt."""
        name = _hochladen(klient, bo.cii("RE-2025-1228", "20251228"), "Dezember.xml")
        maske = klient.get(f"/belege/pruefen/{name}").text
        assert re.search(r'id="jahreswechsel">', maske)
        assert bezahlt.JAHRESWECHSEL in maske
        assert 'value="2025-12-28"' in _feld(maske, "bezahlt_am")

    def test_zahlungsart_der_letzten_zahlung_an_den_lieferanten(self, klient, datenbank):
        name = _hochladen(klient, bo.cii(), "a.xml")
        klient.post(f"/belege/pruefen/{name}", data={
            **FORMULAR, "lieferant_neu": "Hetzner Online GmbH",
            "lieferant_ust_idnr": "DE812871812", "zahlung": "bezahlt", "zahlungsart": "KARTE"})
        zweite = _hochladen(klient, bo.cii("RE-2026-0901", "20260414"), "b.xml")
        maske = klient.get(f"/belege/pruefen/{zweite}").text
        assert '<option value="KARTE" selected>' in maske

    def test_weiter_zur_naechsten_datei(self, klient):
        """Nach dem Buchen bietet die Belegseite die naechste Datei im Eingang
        an, mit der Zahl der verbleibenden."""
        erste = _hochladen(klient, bo.cii(), "a.xml")
        zweite = _hochladen(klient, bo.cii("RE-2026-0901", "20260414"), "b.xml")
        dritte = _hochladen(klient, bo.cii("RE-2026-0902", "20260415"), "c.xml")
        antwort = klient.post(f"/belege/pruefen/{erste}", data=FORMULAR,
                              follow_redirects=False)
        assert "weiter=1" in antwort.headers["location"]
        seite = klient.get(antwort.headers["location"]).text
        naechste = sorted([zweite, dritte])[0]
        assert (f'<a href="/belege/pruefen/{naechste}">Weiter zur nächsten\n'
                "    Datei im Eingang (noch 2)</a>") in seite
        # Ohne "weiter=1" kein Angebot, und ohne Datei im Eingang auch nicht.
        assert "Weiter zur nächsten" not in klient.get("/belege?erfolg=x").text


class TestEinstellung:
    def test_speichern_mit_journal(self, klient, datenbank):
        seite = klient.get("/belege").text
        assert re.search(r'name="bezahlt" value="1"\s+checked', seite)
        antwort = klient.post("/belege/vorgaben", data={}, follow_redirects=True)
        assert "ohne Zahlung gebucht" in antwort.text
        assert not re.search(r'name="bezahlt" value="1"\s+checked', antwort.text)
        klient.post("/belege/vorgaben", data={})  # nichts geaendert: kein zweiter Eintrag
        klient.post("/belege/vorgaben", data={"bezahlt": "1"})
        with datenbank() as verbindung:
            assert verbindung.execute(
                "SELECT belege_bezahlt_vorgabe FROM betrieb").fetchone()[0] == 1
            assert [(z["wert_alt"], z["wert_neu"]) for z in verbindung.execute(
                "SELECT wert_alt, wert_neu FROM journal WHERE aktion = 'AENDERN'"
                " AND feld = 'belege_bezahlt_vorgabe' ORDER BY id")] == [("1", "0"), ("0", "1")]


# --------------------------------------------------------- Eingangsordner

def hole(conn, datenordner, postfach):
    return abholen.abholen(conn, datenordner, postfach, automatisch=True, heute=HEUTE)


class TestEingangsordner:
    def test_mit_vorgabe_entsteht_die_zahlung_am_belegdatum(self, conn, datenordner,
                                                             postfach):
        bo.lieferant(conn)
        bo.lege(postfach, "Rechnung.xml", bo.cii())
        [gebucht] = hole(conn, datenordner, postfach).gebucht
        [zahlung] = conn.execute("SELECT * FROM zahlung").fetchall()
        assert (zahlung["beleg_id"], zahlung["datum"], zahlung["betrag_cent"],
                zahlung["art"]) == (gebucht.beleg_id, "2026-03-14", -546, "SONSTIGES")
        assert conn.execute("SELECT begruendung FROM journal WHERE aktion = 'ZAHLUNG'"
                            ).fetchone()[0] == bezahlt.BEGRUENDUNG_AUTOMATISCH
        # Bezahlt und unbestaetigt: EUeR-Seite und Uebersicht sagen es gleich.
        assert euer.unbestaetigt_bezahlt(conn, 2026).anzahl == 1

    def test_ohne_vorgabe_ohne_zahlung(self, conn, datenordner, postfach):
        conn.execute("UPDATE betrieb SET belege_bezahlt_vorgabe = 0")
        bo.lieferant(conn)
        bo.lege(postfach, "Rechnung.xml", bo.cii())
        assert len(hole(conn, datenordner, postfach).gebucht) == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM zahlung") == 0

    def test_zahlungsart_der_letzten_zahlung(self, conn, datenordner, postfach):
        lid = bo.lieferant(conn)
        beleg = bo.von_hand_buchen(conn, datenordner, bo.cii("RE-2026-0001", "20260201"),
                                   "alt.xml", lid)
        conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                     " VALUES (?, '2026-02-01', -546, 'KARTE')", (beleg,))
        bo.lege(postfach, "Rechnung.xml", bo.cii())
        [gebucht] = hole(conn, datenordner, postfach).gebucht
        assert conn.execute("SELECT art FROM zahlung WHERE beleg_id = ?",
                            (gebucht.beleg_id,)).fetchone()[0] == "KARTE"

    @pytest.mark.parametrize("datum", ["20251222", "20251231", "20260101", "20260110"])
    def test_jahreswechsel_wartet(self, conn, datenordner, postfach, datum):
        """Belegdatum zwischen 22.12. und 10.01.: der Zahltag entscheidet das
        Jahr -- die Rechnung wartet, statt mit Zahltag = Belegdatum im
        womoeglich falschen Jahr zu landen."""
        bo.lieferant(conn)
        bo.lege(postfach, "Rechnung.xml", bo.cii("RE-JW", datum))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund == bezahlt.JAHRESWECHSEL_GRUND
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_vorjahr_wartet(self, conn, datenordner, postfach):
        """Belegdatum in einem Vorjahr: die Zahlung aenderte unbeaufsichtigt
        die EUeR dieses Jahres, die womoeglich schon abgegeben ist. Der Beleg
        wartet mit Grund auf die Pruefmaske; die EUeR 2025 bleibt, wie sie war."""
        bo.lieferant(conn)
        vorher = euer.auswertung(conn, 2025).ausgaben_cent
        bo.lege(postfach, "Juni-2025.xml", bo.cii("RE-2025-0614", "20250614"))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund == bezahlt.vorjahr_grund(2025)
        assert "EÜR 2025" in wartend.grund
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0
        assert anzahl(conn, "SELECT COUNT(*) FROM zahlung") == 0
        assert euer.auswertung(conn, 2025).ausgaben_cent == vorher

    def test_vorjahr_ohne_vorgabe_wird_gebucht(self, conn, datenordner, postfach):
        """Ohne Zahlung aendert ein Beleg aus dem Vorjahr keine EUeR."""
        conn.execute("UPDATE betrieb SET belege_bezahlt_vorgabe = 0")
        bo.lieferant(conn)
        bo.lege(postfach, "Juni-2025.xml", bo.cii("RE-2025-0614", "20250614"))
        assert len(hole(conn, datenordner, postfach).gebucht) == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM zahlung") == 0

    def test_jahreswechsel_ohne_vorgabe_wird_gebucht(self, conn, datenordner, postfach):
        """Ohne Zahlung gibt es am Jahreswechsel nichts zu entscheiden."""
        conn.execute("UPDATE betrieb SET belege_bezahlt_vorgabe = 0")
        bo.lieferant(conn)
        bo.lege(postfach, "Rechnung.xml", bo.cii("RE-JW", "20251228"))
        assert len(hole(conn, datenordner, postfach).gebucht) == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM zahlung") == 0

    @pytest.mark.parametrize("titel", ["Kreditkartenabrechnung", "Kontoauszug",
                                       "Umsatzübersicht", "Credit Card Statement"])
    def test_abrechnung_und_kontoauszug_warten(self, conn, datenordner, postfach, titel):
        """Eine Kreditkartenabrechnung listet Zahlungen, die einzeln als Beleg
        gebucht sind -- als Ausgabe gebucht stuende alles doppelt in der EUeR.
        Vorher genuegte "rechnung" als Wortteil von "...abrechnung"."""
        bo.lieferant(conn)
        bo.lege(postfach, "Abrechnung.pdf",
                bo.hetzner_pdf(titel, "Abrechnungsnummer: A-2026-10", "Vielen Dank."))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund.startswith("Dokumentart nicht eindeutig Rechnung")


def test_vorjahr():
    from datetime import date
    assert [bezahlt.vorjahr(t, date(2026, 10, 24)) for t in (
        "2025-12-31", "2026-01-01", "2024-03-01", None)] == [2025, None, 2024, None]


def test_jahreswechsel_grenzen():
    assert [bezahlt.am_jahreswechsel(t) for t in (
        "2026-12-21", "2026-12-22", "2027-01-10", "2027-01-11")] == [False, True, True, False]


def test_kategorie_katalog_hat_die_neutrale_zahlungsart(conn):
    """Die neutrale Zahlungsart ist eine erlaubte (CHECK in schema.sql)."""
    beleg = conn.execute(
        "INSERT INTO beleg (belegdatum, waehrung, betrag_brutto_cent, betrag_betrieblich_cent,"
        " erfasst_am, zahlungsart, kategorie_id) VALUES ('2026-03-03', 'EUR', 100, 100, 'x', ?,"
        " ?)", (bezahlt.NEUTRAL, kategorien.nach_schluessel(conn, "HOSTING")["id"])).lastrowid
    conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art) VALUES (?, ?, ?, ?)",
                 (beleg, "2026-03-03", -100, bezahlt.NEUTRAL))
    assert set(bezahlt.ZAHLUNGSARTEN) >= {bezahlt.NEUTRAL}

"""Belege: Eingang (Paket E) und Loeschmarkierung (Paket F) zusammen.

Paket E bucht ganze Stapel ("Alle prüfen und buchen"), legt die Zahlung als
Vorgabe gleich mit an und bestaetigt viele Belege auf einmal. Paket F loescht
Belege als Markierung und erlaubt, dieselbe Datei danach neu zu buchen. Was
beide zusammen leisten muessen:

- Ein geloeschter Beleg ist fuer die Sammelbuchung keine Dublette: der
  falsch ausgelesene Beleg wird geloescht und dieselbe Datei neu gebucht --
  auch ueber den Stapel. Die EUeR zaehlt danach genau einmal.
- Die Zahlungsart als Vorgabe kommt nur von gueltigen Belegen.
- "Alle angezeigten bestaetigen" laesst einen inzwischen geloeschten Beleg
  aus, statt am Trigger mit einem Serverfehler abzubrechen.

Lieferanten und Betraege sind erfunden.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.logik import euer
from app.logik.beleg import belegzahlung, bezahlt, loeschen, sammelbuchung
from app.main import erzeuge_app
from tests import test_belegordner as bo
from tests import test_eingang_sammel as es

datenordner = bo.datenordner
postfach = bo.postfach
conn = bo.conn


def _ausgaben(conn, jahr=2026) -> int:
    return euer.auswertung(conn, jahr).ausgaben_cent


@pytest.fixture(autouse=True)
def _bekannter_lieferant(request):
    """Der Lieferant der Testrechnung mit gemerkter Kategorie (Hosting) --
    sonst stuende der Beleg ohne Kategorie in keiner EUeR-Zeile."""
    if "conn" in request.fixturenames:
        bo.lieferant(request.getfixturevalue("conn"))


class TestSammelbuchungNachDemLoeschen:
    def test_dieselbe_datei_wird_wieder_gebucht_und_zaehlt_einmal(self, conn, datenordner):
        es.lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        plaene = es.plan(conn, datenordner)
        assert plaene["Hetzner.xml"].status == sammelbuchung.BUCHEN
        erstes = es.alles_buchen(conn, datenordner, plaene)
        assert len(erstes.gebucht) == 1
        beleg_id = erstes.gebucht[0].beleg_id
        betrag = _ausgaben(conn)
        assert betrag > 0

        loeschen.loeschen(conn, beleg_id, grund="falsch ausgelesen")
        assert _ausgaben(conn) == 0

        # Dieselbe Datei noch einmal in den Eingang: keine Dublette mehr --
        # sie wird gebucht, nicht zum Wegwerfen vorgehakt.
        es.lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        plaene = es.plan(conn, datenordner)
        assert plaene["Hetzner.xml"].status == sammelbuchung.BUCHEN
        zweites = es.alles_buchen(conn, datenordner, plaene)
        assert len(zweites.gebucht) == 1
        assert zweites.gebucht[0].beleg_id != beleg_id
        assert _ausgaben(conn) == betrag, "genau einmal in der EUeR"

    def test_gueltig_gebuchte_datei_bleibt_dublette(self, conn, datenordner):
        es.lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        es.alles_buchen(conn, datenordner, es.plan(conn, datenordner))
        es.lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        assert es.plan(conn, datenordner)["Hetzner.xml"].status == sammelbuchung.DUBLETTE


class TestZahlungsartNurAusGueltigenBelegen:
    def test_geloeschter_beleg_gibt_keine_vorgabe(self, conn, datenordner):
        es.lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        beleg_id = es.alles_buchen(conn, datenordner, es.plan(conn, datenordner)
                                   ).gebucht[0].beleg_id
        lieferant_id = conn.execute("SELECT lieferant_id FROM beleg WHERE id = ?",
                                    (beleg_id,)).fetchone()[0]
        conn.execute("UPDATE zahlung SET art = 'KARTE' WHERE beleg_id = ?", (beleg_id,))
        assert bezahlt.zahlungsart(conn, lieferant_id) == "KARTE"
        loeschen.loeschen(conn, beleg_id, grund="Testbeleg")
        assert bezahlt.zahlungsart(conn, lieferant_id) == bezahlt.NEUTRAL


class TestBezahltVorgabeUndBelegzahlung:
    """Die Zahlung aus der Vorgabe (E) ist eine Belegzahlung: Zahltag aendern
    und zuruecknehmen (F) gehen, die Sperren fuer Rechnungszahlungen (C)
    greifen dort nicht."""

    def test_zahltag_aendern_und_zuruecknehmen(self, conn, datenordner):
        es.lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        gebucht = es.alles_buchen(conn, datenordner, es.plan(conn, datenordner)).gebucht[0]
        beleg_id = gebucht.beleg_id
        assert [tuple(z) for z in conn.execute(
            "SELECT datum, rechnung_id FROM zahlung WHERE beleg_id = ?", (beleg_id,))] == [
            ("2026-03-14", None)]
        betrag = _ausgaben(conn)
        belegzahlung.zahltag_aendern(conn, datenordner, beleg_id, "2026-03-20", laufend=2026)
        assert conn.execute("SELECT datum FROM zahlung WHERE beleg_id = ?",
                            (beleg_id,)).fetchone()[0] == "2026-03-20"
        assert _ausgaben(conn) == betrag
        belegzahlung.zahlung_zuruecknehmen(conn, datenordner, beleg_id, laufend=2026)
        assert conn.execute("SELECT COUNT(*) FROM zahlung WHERE beleg_id = ?",
                            (beleg_id,)).fetchone()[0] == 0
        assert _ausgaben(conn) == 0


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=bo.EINRICHTUNG, follow_redirects=False)
        yield c


def test_alle_bestaetigen_laesst_geloeschte_aus(klient, tmp_path):
    """Die Liste "nur ungeklärte" zeigte zwei Belege; einer wird inzwischen
    (anderer Tab) geloescht. Das Bestaetigen der alten Liste bestaetigt den
    gueltigen und laesst den geloeschten unberuehrt -- statt am Trigger
    beleg_geloescht_unveraenderlich mit einem Serverfehler abzubrechen."""
    es.hochladen(klient, ("a.xml", bo.cii()), ("b.xml", bo.cii("RE-2026-0901", "20260414")))
    vorschau = klient.get("/belege/alle-buchen").text
    namen = re.findall(r'name="buchen" value="([^"]+)"', vorschau)
    felder = dict(re.findall(r'<input type="hidden" name="(sha_[^"]+)" value="([^"]+)">',
                             vorschau))
    klient.post("/belege/alle-buchen", data={"zahlung": "bezahlt", "buchen": namen, **felder})
    liste = klient.get("/belege?offen=1").text
    kennungen = re.findall(r'<input type="hidden" name="beleg" value="(\d+)">', liste)
    assert sorted(kennungen) == ["1", "2"]

    verbindung = sqlite3.connect(tmp_path / "steuer.sqlite", isolation_level=None)
    verbindung.row_factory = sqlite3.Row
    try:
        loeschen.loeschen(verbindung, 1, grund="Testbeleg")
    finally:
        verbindung.close()

    antwort = klient.post("/belege/geprueft-alle", data={"beleg": kennungen},
                          follow_redirects=True)
    assert antwort.status_code == 200
    assert "1 Beleg bestätigt." in antwort.text
    verbindung = sqlite3.connect(tmp_path / "steuer.sqlite")
    try:
        assert [tuple(z) for z in verbindung.execute(
            "SELECT id, geprueft_am IS NOT NULL, geloescht_am IS NOT NULL FROM beleg"
            " ORDER BY id")] == [(1, 0, 1), (2, 1, 0)]
    finally:
        verbindung.close()

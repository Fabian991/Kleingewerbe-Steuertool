"""Freie Rechnungspositionen: Beträge ohne Zeiteintrag.

Die Affiliate-Provision zum Monatsende, eine Pauschale ohne Zeiterfassung:
Betrag = Menge x Einzelpreis, die Position haengt an keiner Zeit und bleibt
bei jeder Neuberechnung der Zeitpositionen stehen. Ein Entwurf ohne Zeiten
beginnt leer mit einem Leistungszeitraum.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import festschreiben as f
from app.logik import validierung
from app.main import erzeuge_app
from tests import test_belegordner as bo
from tests.test_festschreiben import _zeiten

ZEITRAUM = ("2026-10-01", "2026-10-31")


@pytest.fixture
def ordner(tmp_path):
    return tmp_path / "belege"


@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    # Wie in tests/test_festschreiben.py: Betrieb mit Bank und Rundungsregel,
    # Kunde mit Satz -- damit sich ein Entwurf festschreiben laesst.
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01', iban = 'DE02120300000000202051',"
        " bic = 'BYLADEM1001', institut = 'DKB',"
        " rundung_modus = 'KAUFMAENNISCH', rundung_intervall_min = 15 WHERE id = 1"
    )
    conn.execute("UPDATE kunde SET satz_cent = 8000, zahlungsziel_tage = 14 WHERE id = ?",
                 (kunde_id,))
    return kunde_id


def _position(conn, rid, **werte):
    vorgabe = {"bezeichnung": "Affiliate-Provision Oktober 2026", "untertext": None,
               "menge_tausendstel": 1000, "einheit": "C62", "einzelpreis_cent": 35000}
    vorgabe.update(werte)
    return f.freie_position_anlegen(conn, rid, **vorgabe)


def _zeilen(conn, rid):
    return conn.execute(
        "SELECT id, pos_nr, bezeichnung, quelle, betrag_cent, darstellung, einheit"
        " FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr", (rid,),
    ).fetchall()


def _kopf(conn, rid):
    return conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()


def _codes(conn, rid):
    return {b.code for b in validierung.pruefe(conn, rid)}


class TestMenge:
    @pytest.mark.parametrize("text, erwartet", [
        ("1", 1000), ("12", 12000), ("2,5", 2500), ("0,125", 125), ("3.75", 3750),
    ])
    def test_lesen(self, text, erwartet):
        assert f.lies_menge(text) == erwartet

    @pytest.mark.parametrize("text", ["", "0", "0,000", "abc", "1,2345", "1.000.000", "-1"])
    def test_abgewiesen(self, text):
        with pytest.raises(f.FestschreibenFehler):
            f.lies_menge(text)


class TestLogik:
    def test_entwurf_ohne_zeiten(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, [], zeitraum=ZEITRAUM)
        r = _kopf(conn, rid)
        assert (r["brutto_cent"], r["leistung_von"], r["leistung_bis"]) == (0, *ZEITRAUM)
        assert r["betreff"].startswith("Rechnung – Oktober 2026")
        assert _zeilen(conn, rid) == []
        # Ohne Betrag haelt R06 das Festschreiben auf -- bis eine Position da ist.
        assert "R06" in _codes(conn, rid)
        _position(conn, rid, menge_tausendstel=12000, einzelpreis_cent=2500,
                  untertext="12 Verkäufe × 25,00 €")
        r = _kopf(conn, rid)
        assert (r["netto_cent"], r["brutto_cent"]) == (30000, 30000)
        assert [(z["pos_nr"], z["quelle"], z["betrag_cent"], z["darstellung"])
                for z in _zeilen(conn, rid)] == [(1, "FREI", 30000, "BETRAG")]
        # Kein Rundungssatz auf dem Papier: er spricht von Zeitsummen, und
        # hier gibt es keine.
        assert r["rundung_hinweis"] is None
        assert "R06" not in _codes(conn, rid) and "R09" not in _codes(conn, rid)
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)
        assert ergebnis.nummer and _kopf(conn, rid)["status"] == "FESTGESCHRIEBEN"
        # Festgeschrieben: keine Position mehr dazu, keine weg.
        with pytest.raises(f.FestschreibenFehler, match="nur an einem Entwurf"):
            _position(conn, rid)
        with pytest.raises(f.FestschreibenFehler):
            f.freie_position_loeschen(conn, rid, _zeilen(conn, rid)[0]["id"])

    def test_ohne_zeiten_braucht_zeitraum(self, conn, eingerichtet):
        # Ohne Zeitraum bleibt es bei der alten Regel: keine Zeit, kein Entwurf.
        with pytest.raises(f.FestschreibenFehler, match="abrechenbar"):
            f.entwurf_anlegen(conn, eingerichtet, [])
        with pytest.raises(f.FestschreibenFehler, match="endet vor"):
            f.entwurf_anlegen(conn, eingerichtet, [], zeitraum=("2026-10-31", "2026-10-01"))
        ids = _zeiten(conn, eingerichtet, anzahl=1)
        with pytest.raises(f.FestschreibenFehler, match="Entweder"):
            f.entwurf_anlegen(conn, eingerichtet, ids, zeitraum=ZEITRAUM)

    def test_bleibt_bei_neuberechnung_hinter_den_zeiten(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet, anzahl=2, minuten=60)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        zeitsumme = _kopf(conn, rid)["brutto_cent"]
        assert zeitsumme > 0
        pid = _position(conn, rid)
        assert _kopf(conn, rid)["brutto_cent"] == zeitsumme + 35000
        vorher = [(z["pos_nr"], z["quelle"]) for z in _zeilen(conn, rid)]
        assert vorher[-1] == (len(vorher), "FREI")
        # Gruppierung wechseln: die Zeitpositionen entstehen neu, die freie
        # bleibt -- mit derselben id, wieder ganz hinten, Summe stimmt.
        conn.execute("BEGIN IMMEDIATE")
        f.positionen_neu(conn, rid)
        conn.execute("COMMIT")
        nachher = _zeilen(conn, rid)
        assert nachher[-1]["id"] == pid and nachher[-1]["quelle"] == "FREI"
        assert [z["pos_nr"] for z in nachher] == list(range(1, len(nachher) + 1))
        assert _kopf(conn, rid)["brutto_cent"] == zeitsumme + 35000
        assert "R09" not in _codes(conn, rid)
        # Mit Zeitpositionen bleibt der Rundungssatz.
        assert _kopf(conn, rid)["rundung_hinweis"]

    def test_loeschen_und_journal(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet, anzahl=1, minuten=60)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        version = _kopf(conn, rid)["version"]
        pid = _position(conn, rid)
        assert _kopf(conn, rid)["version"] > version
        zeitposition = next(z for z in _zeilen(conn, rid) if z["quelle"] == "ZEIT")
        with pytest.raises(f.FestschreibenFehler, match="aus Zeiten"):
            f.freie_position_loeschen(conn, rid, zeitposition["id"])
        f.freie_position_loeschen(conn, rid, pid)
        assert [z["quelle"] for z in _zeilen(conn, rid)] == ["ZEIT"]
        aktionen = [z[0] for z in conn.execute(
            "SELECT aktion FROM journal WHERE objekttyp = 'rechnung' AND objekt_id = ?"
            " ORDER BY id", (rid,))]
        assert aktionen[-2:] == ["POSITION_FREI", "POSITION_FREI_WEG"]
        with pytest.raises(f.FestschreibenFehler):
            f.freie_position_loeschen(conn, rid, pid)

    def test_pauschal_ohne_menge_gedruckt(self, conn, eingerichtet):
        rid = f.entwurf_anlegen(conn, eingerichtet, [], zeitraum=ZEITRAUM)
        _position(conn, rid, einheit="LS", bezeichnung="Betreuung Oktober")
        zeile = _zeilen(conn, rid)[0]
        assert (zeile["darstellung"], zeile["einheit"]) == ("NUR_TEXT", "LS")

    @pytest.mark.parametrize("werte, text", [
        ({"bezeichnung": "  "}, "Bezeichnung"),
        ({"bezeichnung": "x" * 201}, "zu lang"),
        ({"einheit": "KGM"}, "Einheit"),
        ({"einzelpreis_cent": 0}, "Einzelpreis"),
        ({"menge_tausendstel": 0}, "Menge"),
        ({"untertext": "y" * 2001}, "Beschreibung"),
    ])
    def test_abgewiesen(self, conn, eingerichtet, werte, text):
        rid = f.entwurf_anlegen(conn, eingerichtet, [], zeitraum=ZEITRAUM)
        with pytest.raises(f.FestschreibenFehler, match=text):
            _position(conn, rid, **werte)
        assert _zeilen(conn, rid) == [] and _kopf(conn, rid)["brutto_cent"] == 0

    def test_rundung_auf_den_cent(self, conn, eingerichtet):
        rid = f.entwurf_anlegen(conn, eingerichtet, [], zeitraum=ZEITRAUM)
        # 2,5 x 33,33 € = 83,325 € -> 83,33 € (kaufmaennisch, wie die Datenbank prueft)
        _position(conn, rid, menge_tausendstel=2500, einzelpreis_cent=3333)
        assert _zeilen(conn, rid)[0]["betrag_cent"] == 8333


# ------------------------------------------------------------------ Seite

KUNDE = {
    "name": "Beispiel Affiliate GmbH", "strasse": "Hauptstr. 1", "plz": "10115",
    "ort": "Berlin", "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=bo.EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _datenbank(tmp_path) -> sqlite3.Connection:
    verbindung = sqlite3.connect(tmp_path / "steuer.sqlite")
    verbindung.row_factory = sqlite3.Row
    return verbindung


def _entwurf_ohne_zeiten(klient, kunde_id: int) -> int:
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": str(kunde_id), "ohne_zeiten": "1", "datum": db.heute(),
        "leistung_von": ZEITRAUM[0], "leistung_bis": ZEITRAUM[1]}, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text
    treffer = re.fullmatch(r"/rechnung/(\d+)#positionen", antwort.headers["location"])
    assert treffer
    return int(treffer.group(1))


class TestSeite:
    def test_assistent_bietet_rechnung_ohne_zeiten(self, klient, tmp_path):
        kunde = _datenbank(tmp_path).execute("SELECT id FROM kunde").fetchone()[0]
        text = klient.get(f"/rechnung/neu?kunde={kunde}").text
        assert "Rechnung ohne Zeiten" in text and 'name="ohne_zeiten"' in text
        assert f'name="leistung_von" type="date" value="{db.heute()[:8]}01"' in text

    def test_ohne_zeiten_anlegen_position_hinzu_und_weg(self, klient, tmp_path):
        conn = _datenbank(tmp_path)
        kunde = conn.execute("SELECT id FROM kunde").fetchone()[0]
        rid = _entwurf_ohne_zeiten(klient, kunde)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Noch keine Position" in seite and "Freie Position hinzufügen" in seite
        antwort = klient.post(f"/rechnung/{rid}/position", data={
            "bezeichnung": "Affiliate-Provision Oktober 2026", "menge": "12",
            "einheit": "C62", "einzelpreis": "25,00", "untertext": "12 Verkäufe × 25,00 €"},
            follow_redirects=False)
        assert antwort.status_code == 303
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Affiliate-Provision Oktober 2026" in seite and "300,00" in seite
        assert "Position entfernen" in seite and "Noch keine Position" not in seite
        kopf = conn.execute("SELECT brutto_cent FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert kopf["brutto_cent"] == 30000
        pid = conn.execute("SELECT id FROM rechnungsposition WHERE rechnung_id = ?",
                           (rid,)).fetchone()[0]
        antwort = klient.post(f"/rechnung/{rid}/position/{pid}/loeschen", follow_redirects=False)
        assert antwort.status_code == 303
        assert conn.execute("SELECT brutto_cent FROM rechnung WHERE id = ?",
                            (rid,)).fetchone()[0] == 0

    def test_fehler_bleibt_mit_eingaben_auf_der_seite(self, klient, tmp_path):
        kunde = _datenbank(tmp_path).execute("SELECT id FROM kunde").fetchone()[0]
        rid = _entwurf_ohne_zeiten(klient, kunde)
        antwort = klient.post(f"/rechnung/{rid}/position", data={
            "bezeichnung": "Provision", "menge": "1", "einheit": "C62", "einzelpreis": "abc"})
        assert antwort.status_code == 422
        assert "„abc“ ist kein Betrag" in antwort.text and 'value="Provision"' in antwort.text
        antwort = klient.post(f"/rechnung/{rid}/position", data={
            "bezeichnung": "Provision", "menge": "0", "einheit": "C62", "einzelpreis": "10"})
        assert antwort.status_code == 422 and "Menge" in antwort.text

    def test_ohne_zeiten_abgewiesen_bei_falschem_zeitraum(self, klient, tmp_path):
        kunde = _datenbank(tmp_path).execute("SELECT id FROM kunde").fetchone()[0]
        antwort = klient.post("/rechnung/neu", data={
            "kunde_id": str(kunde), "ohne_zeiten": "1", "datum": db.heute(),
            "leistung_von": "2026-10-31", "leistung_bis": "2026-10-01"})
        assert antwort.status_code == 422 and "endet vor seinem Beginn" in antwort.text
        antwort = klient.post("/rechnung/neu", data={
            "kunde_id": str(kunde), "ohne_zeiten": "1", "datum": db.heute(),
            "leistung_von": "", "leistung_bis": "2026-10-01"})
        assert antwort.status_code == 422 and "Leistungszeitraums fehlt" in antwort.text

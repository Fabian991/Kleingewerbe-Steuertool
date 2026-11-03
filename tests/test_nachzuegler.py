"""Nachzügler der Fehleranalyse: was zwischen zwei Bereichen liegen blieb.

Jeder Test hält einen Fall fest, den ein Korrektur-Agent als "außerhalb meiner
Dateien" gemeldet hatte: das kompakte Datum beim Anlegen und beim
Versandvermerk, der Hinweis am Haken "Leistungszeitraum" bei Reverse Charge,
leere Jahresfilter auf /anlagen und /euer, der Systemname in der
Bilderkennungs-Meldung und der Filter "nur ungeklärte" für Anlagegüter.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.logik.beleg import vision_mac
from app.main import erzeuge_app
from tests.test_festschreiben_befunde import (
    EINRICHTUNG,
    HEUTE,
    KUNDE,
    _db,
    _entwurf,
    _entwurf_id,
    _festgeschrieben,
    _zeit,
)


@pytest.fixture
def daten(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
    return tmp_path / "daten"


@pytest.fixture
def klient(daten):
    with TestClient(erzeuge_app(daten / "steuer.sqlite"), raise_server_exceptions=False) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _haken_text(seite: str) -> str:
    """Der Text am Haken "Leistungszeitraum ausweisen" im Kopf-Formular."""
    treffer = re.search(r'name="leistungszeitraum".*?</label>', seite, re.S)
    assert treffer, "Haken nicht gefunden"
    return treffer.group(0)


class TestKompaktesDatum:
    def test_anlegen_speichert_iso(self, klient, daten):
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)], datum="20260310"))
        conn = _db(daten)
        try:
            datum = conn.execute("SELECT datum FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        finally:
            conn.close()
        assert datum == "2026-03-10"

    def test_versandvermerk_speichert_iso(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        kompakt = HEUTE.replace("-", "")
        antwort = klient.post(f"/rechnung/{rid}/versendet", data={"datum": kompakt},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"]
        conn = _db(daten)
        try:
            gespeichert = conn.execute(
                "SELECT versendet_am FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        finally:
            conn.close()
        assert gespeichert == HEUTE


class TestHinweisLeistungszeitraum:
    def test_inland_sagt_keine_pflichtangabe(self, klient, daten):
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        assert "keine Pflichtangabe" in _haken_text(klient.get(f"/rechnung/{rid}").text)

    def test_reverse_charge_nennt_die_pflicht(self, klient, daten):
        klient.post("/kunden/1", data={
            **KUNDE, "strasse": "Ringstr. 1", "plz": "1010", "ort": "Wien", "land": "AT",
            "leistungsort": "EU_B2B_REVERSE_CHARGE", "ust_idnr": "ATU12345678",
        }, follow_redirects=False)
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        haken = _haken_text(klient.get(f"/rechnung/{rid}").text)
        assert "bei Reverse Charge" in haken
        assert "keine Pflichtangabe" not in haken


class TestLeereJahresfilter:
    def test_anlagen_mit_leerem_jahr(self, klient):
        assert klient.get("/anlagen?jahr=").status_code == 200
        assert klient.get("/anlagen?jahr=abc").status_code == 200

    def test_euer_mit_leerem_jahr(self, klient):
        assert klient.get("/euer?jahr=").status_code == 200
        assert klient.get("/euer?jahr=abc").status_code == 200


def test_bilderkennung_nennt_keinen_rohen_systemnamen(monkeypatch):
    monkeypatch.setattr(vision_mac.sys, "platform", "linux")
    text = vision_mac.ist_verfuegbar().begruendung
    assert "linux" not in text
    assert "nur auf macOS" in text


class TestFilterUngeklaert:
    def test_anlagegut_im_verzeichnis_gilt_als_geklaert(self, klient, daten):
        conn = _db(daten)
        try:
            conn.execute(
                "INSERT INTO beleg (art, belegdatum, betrag_brutto_cent, aufteilungsquote,"
                " betrag_betrieblich_cent, ist_anlagegut, geprueft_am, erfasst_am, notiz)"
                " VALUES ('EINGANG', '2026-03-02', 119000, 100, 119000, 1, ?, ?, 'Notebook')",
                (HEUTE, HEUTE),
            )
            beleg_id = conn.execute("SELECT MAX(id) FROM beleg").fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        anker = f'id="beleg-{beleg_id}"'
        assert anker in klient.get("/belege?offen=1").text, "ohne Verzeichnis: ungeklärt"

        antwort = klient.post("/anlagen", data={
            "bezeichnung": "Notebook", "anschaffung_am": "2026-03-02",
            "brutto_euro": "1.190,00", "netto_euro": "", "ust_satz": "19",
            "nutzungsdauer_jahre": "3", "art": "BEWEGLICH", "beleg_id": str(beleg_id),
            "aufteilungsquote": "100",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
        assert anker not in klient.get("/belege?offen=1").text
        assert anker in klient.get("/belege").text, "in der vollen Liste bleibt er"

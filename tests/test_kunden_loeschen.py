"""Kunde loeschen oder stilllegen.

Loeschen ist echtes Loeschen -- aber nur, solange der Kunde auf keinem
festgeschriebenen Dokument steht. Offene Zeiten und Entwuerfe gehen nach
Rueckfrage mit; was verschwand, steht vollstaendig im Journal. Steht er auf
einer Rechnung, bleibt er (§ 147 AO) und wird stillgelegt.
"""

import json
import re
import sqlite3
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.logik import festschreiben as fs
from app.logik import kunden as stamm
from app.main import erzeuge_app


def _kunde(conn, nummer="K-0001", name="Beispiel GmbH") -> int:
    return conn.execute(
        "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
        " VALUES (?, ?, 'Musterstraße 1', '10115', 'Berlin', '2026-01-01')",
        (nummer, name)).lastrowid


def _zeit(conn, kunde_id, titel="Beratung", **werte) -> int:
    zeile = {"kunde_id": kunde_id, "titel": titel, "datum": "2026-03-02",
             "dauer_minuten": 60, "erfasst_am": "2026-03-02"} | werte
    return conn.execute(
        f"INSERT INTO zeiteintrag ({', '.join(zeile)}) VALUES ({', '.join('?' * len(zeile))})",
        tuple(zeile.values())).lastrowid


def _festgeschrieben(conn, kunde_id, status="FESTGESCHRIEBEN", nummer="RE-2026-0001") -> int:
    """Eine festgeschriebene Rechnung, direkt in die Tabelle (ohne PDF)."""
    return conn.execute(
        "INSERT INTO rechnung (typ, nummer, kunde_id, datum, netto_cent, brutto_cent, status,"
        " festgeschrieben_am, erstellt_am) VALUES ('RECHNUNG', ?, ?, '2026-03-31', 10000,"
        " 10000, ?, '2026-03-31', '2026-03-31')", (nummer, kunde_id, status)).lastrowid


def _anzahl(conn, tabelle, bedingung="1 = 1", werte=()) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {tabelle} WHERE {bedingung}", werte).fetchone()[0]


class TestLogik:
    def test_kunde_ohne_bezuege(self, conn, betrieb):
        kid = _kunde(conn)
        assert stamm.loeschbarkeit(conn, kid).loeschbar
        stamm.loeschen(conn, kid, grund="Testkunde")
        assert _anzahl(conn, "kunde") == 0
        eintrag = conn.execute("SELECT * FROM journal WHERE aktion = 'LOESCHEN'"
                               " AND objekttyp = 'kunde'").fetchone()
        alt = json.loads(eintrag["wert_alt"])
        assert alt["kunde"]["name"] == "Beispiel GmbH"
        assert alt["kunde"]["strasse"] == "Musterstraße 1"
        assert alt["kunde"]["kundennummer"] == "K-0001"
        assert eintrag["begruendung"] == "Testkunde"

    def test_mit_zeiten_und_entwurf(self, conn, betrieb):
        """Offene, abgeschriebene, interne Zeiten und ein Entwurf: alles weg, alles im Journal."""
        kid = _kunde(conn)
        offen = [_zeit(conn, kid, "Beratung"), _zeit(conn, kid, "Konzept")]
        _zeit(conn, kid, "Abgeschrieben", abgeschrieben_am="2026-04-01",
              abgeschrieben_grund="Kulanz")
        _zeit(conn, kid, "Akquise", abrechnungsmodus="INTERN", grund="eigene Akquise")
        rid = fs.entwurf_anlegen(conn, kid, offen)
        assert _anzahl(conn, "position_zeiteintrag") == 2
        stand = stamm.loeschbarkeit(conn, kid)
        assert stand.loeschbar and len(stand.zeiten) == 4 and len(stand.entwuerfe) == 1
        stamm.loeschen(conn, kid, stand=stand.stand)
        for tabelle in ("kunde", "zeiteintrag", "rechnung", "rechnungsposition",
                        "position_zeiteintrag"):
            assert _anzahl(conn, tabelle) == 0, tabelle
        alt = json.loads(conn.execute(
            "SELECT wert_alt FROM journal WHERE aktion = 'LOESCHEN' AND objekttyp = 'kunde'"
        ).fetchone()[0])
        assert {z["titel"] for z in alt["zeiten"]} == {"Beratung", "Konzept", "Abgeschrieben",
                                                       "Akquise"}
        assert alt["entwuerfe"][0]["id"] == rid and alt["entwuerfe"][0]["positionen"] >= 1
        assert _anzahl(conn, "journal", "aktion = 'ENTWURF_VERWORFEN' AND objekt_id = ?",
                       (rid,)) == 1

    @pytest.mark.parametrize("status", ["FESTGESCHRIEBEN", "STORNIERT"])
    def test_mit_festgeschriebenem_dokument(self, conn, betrieb, status):
        kid = _kunde(conn)
        _zeit(conn, kid)
        _festgeschrieben(conn, kid, status=status)
        stand = stamm.loeschbarkeit(conn, kid)
        assert not stand.loeschbar and stand.belege == ("RE-2026-0001",)
        with pytest.raises(stamm.KundeNichtLoeschbar, match="§ 147 AO") as fehler:
            stamm.loeschen(conn, kid)
        assert "Stattdessen stilllegen" in str(fehler.value)
        assert _anzahl(conn, "kunde") == 1 and _anzahl(conn, "zeiteintrag") == 1
        assert not conn.in_transaction

    def test_zeit_mit_rechnungsgeschichte(self, conn, betrieb):
        """Eine nach Storno freigegebene Zeit eines anderen Kunden haengt am Beleg."""
        anderer = _kunde(conn, "K-0002", "Anderer")
        alt = _festgeschrieben(conn, anderer, status="STORNIERT")
        kid = _kunde(conn)
        _zeit(conn, kid, freigegeben_aus_rechnung_id=alt)
        stand = stamm.loeschbarkeit(conn, kid)
        assert not stand.loeschbar and stand.belege == ()
        with pytest.raises(stamm.KundeNichtLoeschbar, match="Zeiten des Kunden lagen"):
            stamm.loeschen(conn, kid)

    def test_laufender_timer(self, conn, betrieb):
        kid = _kunde(conn)
        _zeit(conn, kid, start_zeit="2026-03-02T09:00:00", end_zeit=None)
        with pytest.raises(stamm.KundeNichtLoeschbar, match="läuft ein Timer"):
            stamm.loeschen(conn, kid)
        assert _anzahl(conn, "kunde") == 1

    def test_veraenderte_rueckfrage(self, conn, betrieb):
        """Seit der Rueckfrage kam eine Zeit dazu: geloescht wird nichts."""
        kid = _kunde(conn)
        stand = stamm.loeschbarkeit(conn, kid).stand
        _zeit(conn, kid)
        with pytest.raises(stamm.KundeNichtLoeschbar, match="Seit der Rückfrage"):
            stamm.loeschen(conn, kid, stand=stand)
        assert _anzahl(conn, "kunde") == 1 and _anzahl(conn, "zeiteintrag") == 1

    def test_zweimal_ein_eintrag(self, conn, betrieb):
        kid = _kunde(conn)
        assert stamm.loeschen(conn, kid) is not None
        assert stamm.loeschen(conn, kid) is None
        assert _anzahl(conn, "journal", "aktion = 'LOESCHEN' AND objekttyp = 'kunde'") == 1

    def test_trigger_sperrt_direktes_loeschen(self, conn, betrieb):
        """Auch ohne Fremdschluessel (wie waehrend einer Migration): der Trigger haelt."""
        kid = _kunde(conn)
        _festgeschrieben(conn, kid)
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with pytest.raises(sqlite3.IntegrityError, match="stilllegen statt loeschen"):
                conn.execute("DELETE FROM kunde WHERE id = ?", (kid,))
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def test_trigger_laesst_entwurfskunden_durch(self, conn, betrieb):
        kid = _kunde(conn)
        conn.execute("DELETE FROM kunde WHERE id = ?", (kid,))
        assert _anzahl(conn, "kunde") == 0

    def test_nummer_wird_wieder_frei(self, conn, betrieb):
        """Der geloeschte Kunde stand auf keinem Beleg -- seine Nummer auch nicht."""
        _kunde(conn, "K-0001")
        test = _kunde(conn, "K-0002", "Testkunde")
        stamm.loeschen(conn, test)
        assert stamm.naechste_kundennummer(conn) == "K-0002"

    def test_stilllegen_mit_journal(self, conn, betrieb):
        kid = _kunde(conn)
        assert stamm.stilllegen(conn, kid, aktiv=False) is True
        assert stamm.stilllegen(conn, kid, aktiv=False) is False
        assert stamm.stilllegen(conn, kid, aktiv=True) is True
        felder = conn.execute("SELECT feld, wert_alt, wert_neu FROM journal"
                              " WHERE aktion = 'AENDERN' AND objekttyp = 'kunde'").fetchall()
        assert [tuple(z) for z in felder] == [("aktiv", "1", "0"), ("aktiv", "0", "1")]
        assert stamm.stilllegen(conn, 999, aktiv=False) is None


# ========================================================== Oberflaeche

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}
KUNDE = {
    "name": "Beispiel GmbH", "strasse": "Musterstraße 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen():
        verbindung = sqlite3.connect(tmp_path / "web.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung
    return oeffnen


def _anlegen(klient, **werte) -> int:
    antwort = klient.post("/kunden", data={**KUNDE, **werte}, follow_redirects=False)
    return int(antwort.headers["location"].split("?")[0].rsplit("/", 1)[1])


def _stand(html: str) -> str:
    return re.search(r'name="stand" value="([^"]+)"', html).group(1)


class TestOberflaeche:
    def test_rueckfrage_zaehlt_auf_und_loescht(self, klient, datenbank):
        kid = _anlegen(klient)
        klient.post("/zeiten", data={"kunde_id": kid, "titel": "Konzeptarbeit",
                                     "datum": "2026-03-02", "dauer": "90"})
        seite = klient.get(f"/kunden/{kid}").text
        rueckfrage = " ".join(seite[seite.index('<details class="loeschen"'):].split())
        assert "Mitgelöscht werden" in rueckfrage and "Konzeptarbeit" in rueckfrage
        assert "Ja, K-0001 Beispiel GmbH mit allem Aufgezählten endgültig löschen" in rueckfrage
        antwort = klient.post(f"/kunden/{kid}/loeschen",
                              data={"bestaetigt": "1", "stand": _stand(seite),
                                    "grund": "Testkunde"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "K-0001 Beispiel GmbH gelöscht (mit 1 Zeit)." in unquote(
            antwort.headers["location"])
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0

    def test_ohne_bestaetigung_nichts(self, klient, datenbank):
        kid = _anlegen(klient)
        antwort = klient.post(f"/kunden/{kid}/loeschen", data={}, follow_redirects=False)
        assert "fehler=" in antwort.headers["location"]
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1

    def test_doppelklick(self, klient, datenbank):
        kid = _anlegen(klient)
        stand = _stand(klient.get(f"/kunden/{kid}").text)
        daten = {"bestaetigt": "1", "stand": stand}
        klient.post(f"/kunden/{kid}/loeschen", data=daten)
        zweiter = klient.post(f"/kunden/{kid}/loeschen", data=daten, follow_redirects=False)
        assert "bereits gelöscht" in unquote(zweiter.headers["location"])
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'LOESCHEN'"
                                " AND objekttyp = 'kunde'").fetchone()[0] == 1

    def test_mit_rechnung_nur_stilllegen(self, klient, datenbank):
        kid = _anlegen(klient)
        with datenbank() as conn:
            _festgeschrieben(conn, kid)
        seite = klient.get(f"/kunden/{kid}").text
        assert "Kann nicht gelöscht werden" in seite and "RE-2026-0001" in seite
        assert 'name="stand"' not in seite
        assert f'action="/kunden/{kid}/stilllegen"' in seite
        # Ein Formular von vorher (oder ein manipuliertes) loescht trotzdem nicht.
        antwort = klient.post(f"/kunden/{kid}/loeschen",
                              data={"bestaetigt": "1", "stand": "x"}, follow_redirects=False)
        assert "147" in unquote(antwort.headers["location"])
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1

    def test_stilllegen_blendet_aus(self, klient):
        aktiv = _anlegen(klient, name="Aktiv GmbH")
        still = _anlegen(klient, name="Ruhend GmbH")
        antwort = klient.post(f"/kunden/{still}/stilllegen", follow_redirects=False)
        assert "Stillgelegt" in unquote(antwort.headers["location"])
        liste = klient.get("/kunden").text
        assert "Aktiv GmbH" in liste and "Ruhend GmbH" not in liste
        assert "1 stillgelegter Kunde" in liste and 'href="/kunden?alle=1"' in liste
        alle = klient.get("/kunden?alle=1").text
        assert "Ruhend GmbH" in alle
        assert re.search(r"Ruhend GmbH</a>\s*<span class=\"chip\">stillgelegt</span>", alle)
        assert "inaktiv</span>" not in alle
        seite = klient.get(f"/kunden/{still}").text
        assert "Dieser Kunde ist <strong>stillgelegt</strong>" in seite
        klient.post(f"/kunden/{still}/aktivieren")
        assert "Ruhend GmbH" in klient.get("/kunden").text
        assert aktiv

    def test_unbekannter_kunde(self, klient):
        for weg in ("stilllegen", "aktivieren"):
            antwort = klient.post(f"/kunden/999/{weg}", follow_redirects=False)
            assert antwort.headers["location"].startswith("/kunden?fehler=")

    def test_fremde_herkunft(self, klient, datenbank):
        kid = _anlegen(klient)
        stand = _stand(klient.get(f"/kunden/{kid}").text)
        antwort = klient.post(f"/kunden/{kid}/loeschen", data={"bestaetigt": "1",
                                                                "stand": stand},
                              headers={"origin": "https://angreifer.example"})
        assert antwort.status_code == 403
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1

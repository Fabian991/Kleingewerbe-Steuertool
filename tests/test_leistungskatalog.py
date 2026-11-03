"""Leistungskatalog und Journalansicht.

Der Katalog hat genau einen Zweck: konsistente Titel. Deshalb pruefen die Tests
nicht die Oberflaeche, sondern ob zwei Schreibweisen desselben Titels
nebeneinander entstehen koennen -- denn genau das macht die Gruppierung
"pro Titel" wertlos.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import journal_export
from app.logik import leistungen as logik
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def kunde(klient):
    antwort = klient.post("/kunden", data=KUNDE, follow_redirects=False)
    return int(antwort.headers["location"].rsplit("/", 1)[1])


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen():
        verbindung = sqlite3.connect(tmp_path / "web.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung

    return oeffnen


# ------------------------------------------------------------------- Logik

class TestMerke:
    def test_neuer_titel_wird_aufgenommen(self, conn):
        assert logik.merke(conn, "Beratung") is not None
        assert [z["titel"] for z in logik.katalog(conn)] == ["Beratung"]

    def test_bekannter_titel_wird_nicht_doppelt_aufgenommen(self, conn):
        logik.merke(conn, "Beratung")
        assert logik.merke(conn, "Beratung") is None
        assert len(logik.katalog(conn)) == 1

    def test_schreibweise_zaehlt_nicht(self, conn):
        """Genau der Fall, den der Katalog verhindern soll."""
        logik.merke(conn, "Beratung")
        assert logik.merke(conn, "beratung") is None
        assert logik.merke(conn, "BERATUNG") is None
        assert len(logik.katalog(conn)) == 1

    def test_bekannter_titel_behaelt_seine_schreibweise(self, conn):
        """Wer den Katalog pflegt, bestimmt die Schreibweise."""
        logik.merke(conn, "Beratung")
        logik.merke(conn, "beratung")
        assert logik.katalog(conn)[0]["titel"] == "Beratung"

    def test_leerer_titel_wird_ignoriert(self, conn):
        assert logik.merke(conn, "   ") is None
        assert logik.merke(conn, "") is None
        assert logik.katalog(conn) == []

    def test_datenbank_verhindert_doppelte_titel(self, conn):
        """Die Eindeutigkeit liegt in der Datenbank, nicht in der Oberflaeche."""
        conn.execute("INSERT INTO leistung (titel) VALUES ('Beratung')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO leistung (titel) VALUES ('beratung')")


class TestUnbekannteTitel:
    def test_altbestand_wird_gefunden(self, conn, kunde_id):
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Altes Thema', '2026-01-02', 60, ?)",
            (kunde_id, db.jetzt_utc()),
        )
        assert [z["titel"] for z in logik.unbekannte_titel(conn)] == ["Altes Thema"]

    def test_bekannter_titel_fehlt_in_der_liste(self, conn, kunde_id):
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', '2026-01-02', 60, ?)",
            (kunde_id, db.jetzt_utc()),
        )
        logik.merke(conn, "beratung")
        assert logik.unbekannte_titel(conn) == []


# --------------------------------------------------------------- Oberflaeche

class TestKatalogUeberDieOberflaeche:
    def test_zeiteintrag_fuellt_den_katalog(self, klient, kunde, datenbank):
        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Webentwicklung", "datum": "2026-03-02",
            "dauer": "1:30",
        })
        with datenbank() as conn:
            assert [z["titel"] for z in conn.execute("SELECT titel FROM leistung")] == [
                "Webentwicklung"
            ]
            # Die Aufnahme steht im Protokoll, mit dem Grund.
            eintrag = conn.execute(
                "SELECT * FROM journal WHERE objekttyp = 'leistung'"
            ).fetchone()
            assert eintrag["begruendung"] == "aus der Zeiterfassung uebernommen"

    def test_zweiter_eintrag_legt_nicht_doppelt_an(self, klient, kunde, datenbank):
        for titel in ("Beratung", "beratung", "BERATUNG"):
            klient.post("/zeiten", data={
                "kunde_id": kunde, "titel": titel, "datum": "2026-03-02", "dauer": "30",
            })
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM leistung").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 3

    def test_timer_fuellt_den_katalog(self, klient, kunde, datenbank):
        klient.post("/zeiten/timer/start", data={"kunde_id": kunde, "titel": "Wartung"})
        with datenbank() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM leistung WHERE titel = 'Wartung'"
            ).fetchone()[0] == 1

    def test_abgewiesene_zeit_legt_keinen_titel_an(self, klient, kunde, datenbank):
        """Entsteht der Zeiteintrag nicht, soll auch der Titel nicht entstehen."""
        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Unsinn", "datum": "2026-03-02", "dauer": "blabla",
        }, follow_redirects=False)
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM leistung").fetchone()[0] == 0

    def test_anlegen_ueber_das_formular(self, klient, datenbank):
        klient.post("/leistungen", data={
            "titel": "Workshop", "einheit": "DAY", "preis_euro": "960,00",
            "beschreibung_vorlage": "Tagessatz inkl. Vorbereitung",
        })
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM leistung").fetchone()
        assert zeile["einheit"] == "DAY"
        assert zeile["preis_cent"] == 96000
        assert "Vorbereitung" in zeile["beschreibung_vorlage"]

    def test_doppelter_titel_wird_abgewiesen(self, klient, datenbank):
        klient.post("/leistungen", data={"titel": "Beratung", "einheit": "HUR"})
        antwort = klient.post("/leistungen", data={"titel": "beratung", "einheit": "HUR"},
                              follow_redirects=True)
        assert "steht schon im Katalog" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM leistung").fetchone()[0] == 1

    def test_unsinniger_preis_wird_abgewiesen(self, klient, datenbank):
        antwort = klient.post("/leistungen", data={
            "titel": "Beratung", "einheit": "HUR", "preis_euro": "achtzig",
        }, follow_redirects=True)
        assert "ist kein Betrag" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM leistung").fetchone()[0] == 0

    def test_negativer_preis_wird_abgewiesen(self, klient, datenbank):
        antwort = klient.post("/leistungen", data={
            "titel": "Beratung", "einheit": "HUR", "preis_euro": "-80",
        }, follow_redirects=True)
        assert "negativer Preis" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM leistung").fetchone()[0] == 0

    def test_stilllegen_statt_loeschen(self, klient, datenbank):
        klient.post("/leistungen", data={"titel": "Altes", "einheit": "HUR"})
        with datenbank() as conn:
            lid = conn.execute("SELECT id FROM leistung").fetchone()[0]
        # Ein nicht angehakter Haken wird vom Browser nicht gesendet.
        klient.post(f"/leistungen/{lid}", data={"titel": "Altes", "einheit": "HUR"})
        with datenbank() as conn:
            assert conn.execute("SELECT aktiv FROM leistung").fetchone()[0] == 0
        assert "stillgelegt" in klient.get("/leistungen").text

    def test_umbenennen_laesst_die_zeiteintraege_unberuehrt(self, klient, kunde, datenbank):
        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Tippfhler", "datum": "2026-03-02", "dauer": "30",
        })
        with datenbank() as conn:
            lid = conn.execute("SELECT id FROM leistung").fetchone()[0]
        klient.post(f"/leistungen/{lid}", data={
            "titel": "Tippfehler", "einheit": "HUR", "aktiv": "1",
        })
        with datenbank() as conn:
            assert conn.execute("SELECT titel FROM leistung").fetchone()[0] == "Tippfehler"
            assert conn.execute("SELECT titel FROM zeiteintrag").fetchone()[0] == "Tippfhler"

    def test_nachtragen_uebernimmt_den_altbestand(self, klient, kunde, datenbank):
        with datenbank() as conn:
            conn.execute(
                "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
                " VALUES (?, 'Aus Word-Zeiten', '2026-01-02', 60, ?)",
                (kunde, db.jetzt_utc()),
            )
            conn.commit()
        seite = klient.get("/leistungen").text
        # Ein einzelner Titel wird im Singular gemeldet; der Test prueft deshalb
        # nicht mehr den Plural "stehen nicht im Katalog".
        assert "1 benutzter Titel steht nicht im Katalog" in seite
        antwort = klient.post("/leistungen/nachtragen", follow_redirects=True)
        assert "1 Titel aus der Zeiterfassung übernommen" in antwort.text
        with datenbank() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM leistung WHERE titel = 'Aus Word-Zeiten'"
            ).fetchone()[0] == 1
        danach = klient.get("/leistungen").text
        assert "steht nicht im Katalog" not in danach
        assert "stehen nicht im Katalog" not in danach

    def test_katalog_speist_die_autovervollstaendigung(self, klient, kunde):
        klient.post("/leistungen", data={"titel": "Webentwicklung", "einheit": "HUR"})
        assert 'value="Webentwicklung"' in klient.get("/zeiten").text


class TestJournalAnsicht:
    def test_vorgaenge_sind_lesbar(self, klient):
        text = klient.get("/journal").text
        assert "EINRICHTUNG" in text
        assert "§ 146 Abs. 4 AO" in text

    def test_filter_nach_objekt(self, klient, kunde):
        text = klient.get("/journal", params={"objekttyp": "kunde"}).text
        assert "Mustermann" in text
        # Im Auswahlfeld steht EINRICHTUNG weiter; in der Tabelle nicht mehr.
        assert "<td>EINRICHTUNG</td>" not in text
        assert "<td>ANLEGEN</td>" in text

    def test_spiegel_wird_angemahnt_und_geschrieben(self, klient, tmp_path):
        assert "noch nicht" in klient.get("/journal").text
        antwort = klient.post("/journal/export", follow_redirects=True)
        assert "geschrieben" in antwort.text
        spiegel = tmp_path / journal_export.DATEINAME
        assert spiegel.exists()
        inhalt = spiegel.read_text(encoding="utf-8")
        assert "§ 146 Abs. 4 AO" in inhalt
        assert "EINRICHTUNG" in inhalt
        assert "ist aktuell" in klient.get("/journal").text

    def test_spiegel_meldet_sich_als_veraltet(self, klient, kunde, tmp_path):
        klient.post("/journal/export")
        klient.post("/kunden", data=dict(KUNDE, name="Noch eine GmbH"))
        assert "ist damit veraltet" in klient.get("/journal").text

    def test_sicherung_schreibt_den_spiegel_mit(self, klient, tmp_path):
        """Im Ernstfall fehlt genau die Anwendung, die das Journal lesen kann."""
        klient.post("/backup")
        assert (tmp_path / journal_export.DATEINAME).exists()

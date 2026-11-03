"""Querschnitt: deutsche Fehlerseiten, Statustexte, deutsche Sortierung.

Drei Dinge, die jede Seite betreffen und bei der Fehleranalyse in mehreren
Bereichen zugleich auffielen: FastAPIs englisches JSON bei einer unlesbaren
Eingabe, Jinjas ``capitalize`` ("Im entwurf", "Ueberfaellig") und SQLites
``NOCASE``, das Umlaute hinter Z sortiert.
"""

from fastapi.testclient import TestClient

from app import db
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890",
}


class TestEingabefehlerSeite:
    def test_unlesbare_kennung_im_pfad_gibt_deutsche_seite(self, tmp_path):
        with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
            # Ohne Betrieb fuehrt jeder Weg zur Einrichtung -- erst danach
            # kommt die Anfrage bis zur Pfadpruefung.
            c.post("/einrichtung", data=EINRICHTUNG)
            antwort = c.get("/kunden/abc")
        assert antwort.status_code == 422
        assert antwort.headers["content-type"].startswith("text/html")
        assert "muss eine ganze Zahl sein" in antwort.text
        assert '"detail"' not in antwort.text

    def test_fehlendes_pflichtfeld_gibt_deutsche_seite(self, tmp_path):
        """Geprueft wird der Handler, nicht eine bestimmte Route.

        Bis hierher diente POST /einrichtung ohne Namen als Beispiel. Die
        Einrichtung prueft ihre Pflichtfelder inzwischen selbst und zeigt
        das Formular mit den Eingaben -- der Handler ist nur noch das Netz
        darunter, und das laesst sich mit einer eigenen Route pruefen.
        """
        from fastapi import Form

        anwendung = erzeuge_app(tmp_path / "web.sqlite")

        def pflicht(name: str = Form(...)):
            return {"name": name}

        anwendung.add_api_route("/pflicht", pflicht, methods=["POST"])
        with TestClient(anwendung) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            antwort = c.post("/pflicht", data={"strasse": "Weg 1"})
        assert antwort.status_code == 422
        assert "„name“ fehlt" in antwort.text
        assert "Zurück" in antwort.text

    def test_leerer_zahlenfilter_als_text_gibt_deutsche_seite(self, tmp_path):
        """Ein Browser sendet ein leeres Zahlenfeld als leeren Text.

        Geprueft an einer eigenen Route: alle Filterseiten der Anwendung lesen
        ihre Zahlen inzwischen selbst (leer oder Unsinn heisst "nicht
        einschraenken") und antworten nie mehr mit 422.
        """
        anwendung = erzeuge_app(tmp_path / "web.sqlite")

        def zahlenfilter(jahr: int | None = None):
            return {"jahr": jahr}

        anwendung.add_api_route("/zahlenfilter", zahlenfilter)
        with TestClient(anwendung) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            antwort = c.get("/zahlenfilter?jahr=abc")
        assert antwort.status_code == 422
        assert "„jahr“ muss eine ganze Zahl sein" in antwort.text


class TestServerfehlerSeite:
    def test_unerwartete_ausnahme_wird_deutsche_seite(self, tmp_path):
        anwendung = erzeuge_app(tmp_path / "web.sqlite")

        def kaputt():
            raise ZeroDivisionError("Probe")

        anwendung.add_api_route("/kaputt", kaputt)
        with TestClient(anwendung, raise_server_exceptions=False) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            antwort = c.get("/kaputt")
        assert antwort.status_code == 500
        assert "Da ist etwas schiefgegangen" in antwort.text
        assert "ZeroDivisionError: Probe" in antwort.text
        assert "zurück" in antwort.text.lower()

    def test_im_test_steigt_die_ausnahme_weiter_hoch(self, tmp_path):
        """Der Handler darf Fehler in Tests nicht verschlucken."""
        import pytest

        anwendung = erzeuge_app(tmp_path / "web.sqlite")

        def kaputt():
            raise ZeroDivisionError("Probe")

        anwendung.add_api_route("/kaputt", kaputt)
        with TestClient(anwendung) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            with pytest.raises(ZeroDivisionError):
                c.get("/kaputt")


class TestStatustexte:
    def test_filteroptionen_schreiben_substantive_gross(self, tmp_path):
        with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            seite = c.get("/zeiten").text
        assert "In Rechnung gestellt" in seite
        assert "Im Entwurf" in seite
        assert "Ohne Berechnung gestellt" in seite
        assert "In rechnung gestellt" not in seite


class TestDeutscheSortierung:
    def test_umlaute_sortieren_bei_ihrem_grundbuchstaben(self, tmp_path):
        conn = db.oeffne(tmp_path / "s.sqlite")
        namen_roh = [
            "zebra", "Ärzte GmbH", "apfel", "Österreich AG", "Bäcker", "Zoo", "Ölmühle", "éclair",
        ]
        for name in namen_roh:
            conn.execute(
                "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am) "
                "VALUES (?, ?, 'S 1', '1', 'O', ?)",
                (f"K-{name}", name, db.heute()),
            )
        namen = [z[0] for z in conn.execute("SELECT name FROM kunde ORDER BY name COLLATE DEUTSCH")]
        assert namen == [
            "apfel", "Ärzte GmbH", "Bäcker", "éclair", "Ölmühle", "Österreich AG", "zebra", "Zoo",
        ]

    def test_kundenliste_nutzt_die_sortierung(self, tmp_path):
        with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            for name in ["Zoo", "Ärzte GmbH", "Bäcker"]:
                c.post("/kunden", data={"name": name, "strasse": "S 1", "plz": "1", "ort": "O"})
            seite = c.get("/kunden").text
        assert seite.index("Ärzte GmbH") < seite.index("Bäcker") < seite.index("Zoo")

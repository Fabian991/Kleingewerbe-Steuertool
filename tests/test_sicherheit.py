"""Querschnitt Sicherheit: Herkunft, riesige Kennungen, 405-Text, favicon, Katalog.

Je Befund der Gegenprobe (Abschnitt QUERSCHNITT) mindestens eine Regression.
Die Anfragen laufen ueber den TestClient mit ``raise_server_exceptions``: ein
HTTP 500 kaeme hier als Ausnahme hoch und liesse den Test scheitern.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.logik import journal_export
from app.logik import leistungen as leistungslogik
from app.main import erzeuge_app
from app.web import VORLAGEN

WURZEL = Path(__file__).resolve().parent.parent
STIL = (WURZEL / "app" / "static" / "stil.css").read_text(encoding="utf-8")

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "90,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}
IBAN_ALT = "DE02120300000000202051"
IBAN_ANGREIFER = "DE89370400440532013000"
FREMD = "https://angreifer.example"


@pytest.fixture
def anwendung(tmp_path):
    return erzeuge_app(tmp_path / "web.sqlite")


@pytest.fixture
def klient(anwendung):
    with TestClient(anwendung) as c:
        antwort = c.post("/einrichtung", data={**EINRICHTUNG, "iban": IBAN_ALT},
                         follow_redirects=False)
        assert antwort.status_code == 303
        yield c


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen() -> sqlite3.Connection:
        verbindung = sqlite3.connect(tmp_path / "web.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung

    return oeffnen


def _iban(datenbank) -> str | None:
    verbindung = datenbank()
    try:
        return verbindung.execute("SELECT iban FROM betrieb WHERE id = 1").fetchone()["iban"]
    finally:
        verbindung.close()


def _ueberschrift(html: str) -> str:
    treffer = re.search(r"<h1>(.*?)</h1>", html, re.S)
    return treffer.group(1).strip() if treffer else ""


# ------------------------------------------------- 1 Fremde Herkunft (CSRF)

class TestFremdeHerkunft:
    """Ein Auto-Submit-Formular einer fremden Seite an 127.0.0.1:8737."""

    def test_fremde_origin_tauscht_keine_iban(self, klient, datenbank):
        antwort = klient.post(
            "/einrichtung", data={**EINRICHTUNG, "iban": IBAN_ANGREIFER},
            headers={"origin": FREMD, "referer": FREMD + "/seite.html"},
            follow_redirects=False,
        )
        assert antwort.status_code == 403
        assert "Diese Aktion kam von einer anderen Webseite und wurde abgewiesen" in antwort.text
        assert "Gespeichert wurde nichts" in antwort.text
        assert _iban(datenbank) == IBAN_ALT

    @pytest.mark.parametrize("kopf", [
        {"origin": "null"},                                  # abgeschottetes iframe
        {"origin": "http://testserver:9999"},                # anderer Port
        {"origin": "https://testserver"},                    # anderes Schema
        {"sec-fetch-site": "cross-site"},                    # ohne Origin
        {"origin": "http://testserver", "sec-fetch-site": "cross-site"},
        {"referer": FREMD + "/seite.html"},                  # nur Referer
        {"referer": "kein-url"},
    ])
    def test_weitere_fremde_merkmale(self, klient, datenbank, kopf):
        antwort = klient.post("/einrichtung", data={**EINRICHTUNG, "iban": IBAN_ANGREIFER},
                              headers=kopf, follow_redirects=False)
        assert antwort.status_code == 403
        assert _iban(datenbank) == IBAN_ALT

    @pytest.mark.parametrize("kopf", [
        {"origin": "http://testserver"},
        {"origin": "HTTP://TestServer:80"},
        {"origin": "http://testserver", "sec-fetch-site": "same-origin"},
        {"referer": "http://testserver/einrichtung"},
        {"sec-fetch-site": "none"},                          # Adresszeile/Lesezeichen
        {},                                                  # curl, TestClient
    ])
    def test_eigene_formulare_gehen_weiter(self, klient, datenbank, kopf):
        antwort = klient.post("/einrichtung", data={**EINRICHTUNG, "iban": IBAN_ANGREIFER},
                              headers=kopf, follow_redirects=False)
        assert antwort.status_code == 303
        assert _iban(datenbank) == IBAN_ANGREIFER

    def test_eigene_herkunft_unter_127_0_0_1_mit_port(self, klient, datenbank):
        antwort = klient.post(
            "/einrichtung", data={**EINRICHTUNG, "iban": IBAN_ANGREIFER},
            headers={"host": "127.0.0.1:8737", "origin": "http://127.0.0.1:8737"},
            follow_redirects=False,
        )
        assert antwort.status_code == 303
        assert _iban(datenbank) == IBAN_ANGREIFER

    def test_localhost_seite_darf_nicht_an_127_0_0_1_schreiben(self, klient, datenbank):
        """Andere Herkunft heisst Schema, Rechner UND Port -- auch lokal."""
        antwort = klient.post(
            "/einrichtung", data={**EINRICHTUNG, "iban": IBAN_ANGREIFER},
            headers={"host": "127.0.0.1:8737", "origin": "http://localhost:3000"},
            follow_redirects=False,
        )
        assert antwort.status_code == 403
        assert _iban(datenbank) == IBAN_ALT

    def test_backup_ziel_bleibt(self, klient, datenbank, tmp_path):
        ziel = tmp_path / "fremdziel"
        ziel.mkdir()
        antwort = klient.post("/backup/ziel", data={"pfad": str(ziel)},
                              headers={"origin": FREMD}, follow_redirects=False)
        assert antwort.status_code == 403
        verbindung = datenbank()
        try:
            zeile = verbindung.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
        finally:
            verbindung.close()
        assert str(ziel) not in [str(wert) for wert in tuple(zeile)]

    def test_timer_stop_leitet_nicht_auf_fremde_seite(self, klient):
        antwort = klient.post("/zeiten/timer/stop",
                              headers={"referer": FREMD + "/zeiten"}, follow_redirects=False)
        assert antwort.status_code == 403
        assert "angreifer" not in antwort.headers.get("location", "")

    def test_fehlerseite_verlinkt_nicht_die_fremde_seite(self, klient):
        antwort = klient.post("/journal/export",
                              headers={"origin": FREMD, "referer": FREMD + "/x"})
        assert antwort.status_code == 403
        assert "angreifer" not in antwort.text.split("</h1>", 1)[1].split("http://testserver")[0]
        assert 'href="/"' in antwort.text

    def test_lesen_mit_fremder_origin_bleibt_moeglich(self, klient):
        """GET aendert nichts; der Schutz vor fremdem Lesen ist der Host-Kopf."""
        assert klient.get("/", headers={"origin": FREMD}).status_code == 200


class TestFremderHost:
    """DNS-Rebinding: eine fremde Domain, die auf 127.0.0.1 zeigt."""

    @pytest.mark.parametrize("host", [
        "angreifer.example", "angreifer.example:8737", "127.0.0.1.angreifer.example",
        "localhost.angreifer.example:8737", "localhost:abc", "[::1", "",
    ])
    def test_fremder_host_bekommt_nichts(self, klient, host):
        antwort = klient.get("/backup/excel", headers={"host": host})
        assert antwort.status_code == 421
        assert antwort.headers["content-type"].startswith("text/html")
        assert "Diese Adresse gehört nicht zum Steuer-Tool" in antwort.text

    @pytest.mark.parametrize("host", [
        "127.0.0.1", "127.0.0.1:8737", "localhost", "LOCALHOST:8737", "[::1]",
        "[::1]:8737", "testserver",
    ])
    def test_eigene_namen_gehen(self, klient, host):
        assert klient.get("/", headers={"host": host}).status_code == 200

    def test_gilt_auch_fuer_schreibende_und_statische_anfragen(self, klient, datenbank):
        kopf = {"host": "angreifer.example:8737"}
        assert klient.get("/static/stil.css", headers=kopf).status_code == 421
        antwort = klient.post("/einrichtung", data={**EINRICHTUNG, "iban": IBAN_ANGREIFER},
                              headers=kopf, follow_redirects=False)
        assert antwort.status_code == 421
        assert _iban(datenbank) == IBAN_ALT

    def test_gilt_auch_vor_der_einrichtung(self, anwendung):
        with TestClient(anwendung) as c:
            antwort = c.get("/einrichtung", headers={"host": "angreifer.example"})
            assert antwort.status_code == 421


class TestRahmenschutz:
    """Clickjacking: eine fremde Seite rahmt das Werkzeug unsichtbar ein und
    laesst den Nutzer im Rahmen klicken -- das Formular kaeme mit der eigenen
    Herkunft an. Jede Antwort verbietet deshalb fremde Rahmen."""

    @pytest.mark.parametrize("pfad", ["/", "/zeiten", "/belege", "/static/stil.css",
                                      "/gibt-es-nicht"])
    def test_jede_antwort_verbietet_fremde_rahmen(self, klient, pfad):
        antwort = klient.get(pfad)
        assert antwort.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in antwort.headers["content-security-policy"]

    def test_auch_umleitungen_und_abweisungen(self, klient):
        umleitung = klient.post("/zeiten/timer/weiter", data={}, follow_redirects=False)
        abgewiesen = klient.post("/zeiten/timer/weiter", data={},
                                 headers={"origin": FREMD}, follow_redirects=False)
        fremder_host = klient.get("/", headers={"host": "angreifer.example"})
        for antwort in (umleitung, abgewiesen, fremder_host):
            assert antwort.headers["x-frame-options"] == "SAMEORIGIN", antwort.status_code
            assert antwort.headers["content-security-policy"] == "frame-ancestors 'self'"

    def test_eigene_richtlinie_bleibt(self):
        """Die Mail-Ansicht setzt ihre eigene, strengere Richtlinie -- sie
        wird nicht ueberschrieben (und nennt frame-ancestors 'self' selbst)."""
        from fastapi.responses import Response

        from app.main import _rahmenschutz

        antwort = _rahmenschutz(Response(headers={
            "content-security-policy": "default-src 'none'; frame-ancestors 'self'"}))
        assert antwort.headers["content-security-policy"] == (
            "default-src 'none'; frame-ancestors 'self'")
        assert antwort.headers["x-frame-options"] == "SAMEORIGIN"


# ---------------------------------------------- 2 Riesige Kennungen im Pfad

#: Die Routen aus der Gegenprobe, die bei 2**63 und 2**70 mit 500 endeten.
ROUTEN_MIT_KENNUNG = [
    ("GET", "/kunden/{}", None),
    ("POST", "/kunden/{}", KUNDE),
    ("POST", "/zeiten/{}/modus", {"modus": "STUNDEN"}),
    ("POST", "/zeiten/{}/abschreiben", {"grund": "Probe"}),
    ("POST", "/zeiten/{}/zuruecknehmen", {}),
    ("POST", "/zeiten/{}/freigeben", {}),
    ("POST", "/zeiten/{}/loeschen", {}),
    ("POST", "/backup/{}/pruefen", {}),
    ("POST", "/leistungen/{}", {"titel": "Beratung"}),
    ("GET", "/belege/{}/datei", None),
    ("POST", "/belege/{}/kategorie", {"kategorie_id": "1"}),
    ("POST", "/belege/{}/bezahlt", {"datum": "2026-03-10"}),
    ("POST", "/reisen/{}/verpflegung", {}),
    ("POST", "/reisen/{}/kilometer", {"datum": "2026-03-04", "km": "10", "strecke": "a",
                                       "zweck": "b"}),
    ("POST", "/anlagen/{}/abgang", {"am": "2026-06-30"}),
    ("POST", "/anlagen/{}/abgang/zurueck", {}),
    ("GET", "/rechnung/{}", None),
    ("GET", "/rechnung/{}/pdf", None),
]


class TestRiesigeKennungen:
    @pytest.mark.parametrize("zahl", [2**63, 2**63 + 1, 2**70, 10**30])
    @pytest.mark.parametrize(("methode", "muster", "daten"), ROUTEN_MIT_KENNUNG)
    def test_nie_500_sondern_404(self, klient, methode, muster, daten, zahl):
        antwort = klient.request(methode, muster.format(zahl), data=daten,
                                 follow_redirects=False)
        assert antwort.status_code == 404
        assert "Diese Seite gibt es nicht" in antwort.text

    def test_18_stellen_erreichen_weiter_die_route(self, klient):
        """Bis 18 Stellen antwortet die Route selbst (Umleitung mit Meldung)."""
        antwort = klient.get("/kunden/" + "9" * 18, follow_redirects=False)
        assert antwort.status_code == 303

    @pytest.mark.parametrize("pfad", [
        "/kunden/-9223372036854775809", "/kunden/-1180591620717411303424",
    ])
    def test_negative_riesenzahl_ist_kein_500(self, klient, pfad):
        antwort = klient.get(pfad, follow_redirects=False)
        assert antwort.status_code in (303, 404, 422)

    def test_ueberlauf_wird_zur_deutschen_422_seite(self, tmp_path):
        """Das Netz fuer Formularfelder: OverflowError ergibt 422 statt 500."""
        anwendung = erzeuge_app(tmp_path / "web.sqlite")

        def ueberlauf(wert: int):
            verbindung = sqlite3.connect(":memory:")
            try:
                verbindung.execute("SELECT ?", (wert,))
            finally:
                verbindung.close()

        anwendung.add_api_route("/probe/ueberlauf", ueberlauf, methods=["POST"])
        with TestClient(anwendung) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            antwort = c.post("/probe/ueberlauf", params={"wert": str(2**70)})
        assert antwort.status_code == 422
        assert "Eine Zahl in der Anfrage ist unplausibel groß" in antwort.text
        assert "OverflowError" not in antwort.text

    def test_unendlich_wird_ebenfalls_abgefangen(self, tmp_path):
        anwendung = erzeuge_app(tmp_path / "web.sqlite")

        def unendlich():
            return int(float("inf"))

        anwendung.add_api_route("/probe/unendlich", unendlich, methods=["GET"])
        with TestClient(anwendung) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            antwort = c.get("/probe/unendlich")
        assert antwort.status_code == 422
        assert "unplausibel groß" in antwort.text


# ------------------------------------------------------------- 3 405-Text

class TestFalscheMethode:
    @pytest.mark.parametrize(("methode", "pfad"), [
        ("POST", "/rechnungen"), ("PATCH", "/rechnung/1"),
        ("OPTIONS", "/"), ("POST", "/euer"), ("POST", "/static/stil.css"),
    ])
    def test_leseseite_laesst_sich_nur_anzeigen(self, klient, methode, pfad):
        """Angepasst (Schlussprobe, Bereich quer): hier standen auch PUT und
        DELETE /kunden/1 -- und hielten genau den falschen Text fest, denn
        /kunden/1 nimmt per POST das Kundenformular entgegen. Fuer Adressen mit
        Anzeige UND Formular prueft das jetzt tests/test_schluss_quer.py; an
        ihre Stelle tritt PATCH /rechnung/1, eine reine Leseseite."""
        antwort = klient.request(methode, pfad, follow_redirects=False)
        assert antwort.status_code == 405
        assert _ueberschrift(antwort.text) == "Diese Seite lässt sich nur anzeigen"
        assert "nur Formulare" not in antwort.text
        assert "Method Not Allowed" not in antwort.text

    def test_allow_nennt_alle_methoden_der_adresse(self, klient):
        antwort = klient.put("/kunden/1")
        assert antwort.headers["allow"] == "GET, POST"
        assert klient.post("/rechnungen").headers["allow"] == "GET"

    def test_formularadresse_per_get_bleibt_beim_alten_text(self, klient):
        antwort = klient.get("/zeiten/timer/start")
        assert antwort.status_code == 405
        assert _ueberschrift(antwort.text) == "Diese Adresse ist nur für ein Formular"
        assert "nimmt nur Formulare entgegen" in antwort.text
        assert antwort.headers["allow"] == "POST"

    def test_andere_methode_auf_formularadresse(self, klient):
        antwort = klient.put("/zeiten/timer/start")
        assert antwort.status_code == 405
        assert _ueberschrift(antwort.text) == "Diese Adresse ist nur für ein Formular"
        assert "lässt sich nicht direkt aufrufen" not in antwort.text


# ------------------------------------------------------------- 4 favicon

class TestFavicon:
    def test_204_statt_404(self, klient):
        antwort = klient.get("/favicon.ico")
        assert antwort.status_code == 204
        assert antwort.content == b""

    def test_auch_vor_der_einrichtung(self, anwendung):
        with TestClient(anwendung) as c:
            antwort = c.get("/favicon.ico", follow_redirects=False)
            assert antwort.status_code == 204


# ------------------------------------------- 5 Leistung anlegen per Doppelklick

def _gleichzeitig(anzahl: int, aufgabe):
    ergebnisse: list = [None] * anzahl
    schranke = threading.Barrier(anzahl)

    def lauf(i: int) -> None:
        try:
            schranke.wait()
            ergebnisse[i] = aufgabe(i)
        except Exception as fehler:  # noqa: BLE001 - der Test wertet sie aus
            ergebnisse[i] = fehler

    faeden = [threading.Thread(target=lauf, args=(i,)) for i in range(anzahl)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join()
    return ergebnisse


class TestLeistungDoppelklick:
    @pytest.mark.parametrize("runde", range(4))
    def test_zwei_gleichzeitige_anlagen(self, klient, datenbank, runde):
        titel = f"ParL{runde}"
        ergebnisse = _gleichzeitig(2, lambda i: klient.post(
            "/leistungen", data={"titel": titel, "einheit": "HUR"}, follow_redirects=False))
        for ergebnis in ergebnisse:
            assert not isinstance(ergebnis, Exception), ergebnis
            assert ergebnis.status_code == 303
        orte = sorted(unquote(e.headers["location"]) for e in ergebnisse)
        assert orte[0] == "/leistungen"
        assert orte[1] == (f"/leistungen?fehler=„{titel}“ steht schon im Katalog — auch "
                           "mit anderer Groß- und Kleinschreibung.")
        verbindung = datenbank()
        try:
            anzahl = verbindung.execute(
                "SELECT COUNT(*) FROM leistung WHERE titel = ?", (titel,)).fetchone()[0]
            journal = verbindung.execute(
                "SELECT COUNT(*) FROM journal WHERE objekttyp = 'leistung'"
                " AND aktion = 'ANLEGEN' AND wert_neu LIKE ?", (f"%{titel}%",)).fetchone()[0]
        finally:
            verbindung.close()
        assert anzahl == 1
        assert journal == 1

    def test_unique_index_als_meldung_statt_500(self, klient, monkeypatch):
        """Das Netz: rutscht ein Duplikat an der Pruefung vorbei, meldet der Index."""
        klient.post("/leistungen", data={"titel": "Beratung"})
        monkeypatch.setattr(leistungslogik, "finde", lambda conn, titel: None)
        antwort = klient.post("/leistungen", data={"titel": "beratung"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "steht schon im Katalog" in unquote(antwort.headers["location"])

    def test_umbenennen_auf_vorhandenen_titel(self, klient, datenbank, monkeypatch):
        klient.post("/leistungen", data={"titel": "Beratung"})
        klient.post("/leistungen", data={"titel": "Workshop"})
        verbindung = datenbank()
        try:
            wid = verbindung.execute(
                "SELECT id FROM leistung WHERE titel = 'Workshop'").fetchone()[0]
        finally:
            verbindung.close()
        antwort = klient.post(f"/leistungen/{wid}", data={"titel": "BERATUNG", "aktiv": "1"},
                              follow_redirects=False)
        assert "steht schon im Katalog" in unquote(antwort.headers["location"])
        monkeypatch.setattr(leistungslogik, "finde", lambda conn, titel: None)
        antwort = klient.post(f"/leistungen/{wid}", data={"titel": "BERATUNG", "aktiv": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "Groß- und Kleinschreibung" in unquote(antwort.headers["location"])

    def test_riesenpreis_nennt_die_kataloggrenze(self, klient):
        """euro_nach_cent() weist 20 Stellen ab; gemeldet wird die Katalog-Grenze."""
        antwort = klient.post("/leistungen", data={"titel": "Riesig", "preis_euro": "9" * 20},
                              follow_redirects=False)
        assert "Ein Preis über 1.000.000 €" in unquote(antwort.headers["location"])
        antwort = klient.post("/leistungen", data={"titel": "Kaputt", "preis_euro": "inf"},
                              follow_redirects=False)
        assert "„inf“ ist kein Betrag." in unquote(antwort.headers["location"])

    def test_unbekannte_leistung_beim_speichern(self, klient):
        antwort = klient.post("/leistungen/999", data={"titel": "X"}, follow_redirects=False)
        assert "Diese Leistung gibt es nicht mehr." in unquote(antwort.headers["location"])


# ------------------------------------------------------------------ 6 Druck

class TestDruck:
    def test_ueberschrift_ueber_formular_wird_nicht_gedruckt(self):
        druck = re.findall(r"@media print\s*\{(.*?)\n\}", STIL, re.S)
        assert any(':is(h2, h3):has(+ form[method="post" i])' in block
                   and "display: none" in block for block in druck)

    def test_filterformular_versteckt_seine_ueberschrift_nicht(self):
        """Unter "Bestand und Abschreibung" folgt nach dem GET-Filter die Tabelle."""
        assert ':has(+ form)' not in STIL

    def test_leistungen_neu_ist_nicht_drucken(self, klient):
        html = klient.get("/leistungen").text
        assert '<h2 class="nicht-drucken">Neue Leistung</h2>' in html


# ------------------------------------------------------- Journal und Spiegel

class TestJournal:
    def test_einzahl_im_protokoll(self, tmp_path):
        vorlage = VORLAGEN.get_template("journal.html")
        html = vorlage.render(zeilen=[], typen=[], aktionen=[], filter={}, gesamt=1,
                              spiegel=tmp_path / "j.txt", spiegel_zeilen=1)
        assert "1 Vorgang. Die Tabelle" in html
        assert "ist aktuell: 1 Vorgang." in html
        html = vorlage.render(zeilen=[], typen=[], aktionen=[], filter={}, gesamt=1,
                              spiegel=tmp_path / "j.txt", spiegel_zeilen=0)
        assert "enthält 0 von 1\n  Vorgang und ist" in html

    def test_mehrzahl_im_protokoll(self, tmp_path):
        vorlage = VORLAGEN.get_template("journal.html")
        html = vorlage.render(zeilen=[], typen=[], aktionen=[], filter={}, gesamt=3,
                              spiegel=tmp_path / "j.txt", spiegel_zeilen=2)
        assert "3 Vorgänge. Die Tabelle" in html
        assert "enthält 2 von 3\n  Vorgängen und ist" in html

    def test_spiegelfehler_mit_umlaut(self, klient, monkeypatch):
        def kaputt(conn, ziel):
            raise PermissionError(13, "Keine Berechtigung")

        monkeypatch.setattr(journal_export, "schreibe", kaputt)
        antwort = klient.post("/journal/export", follow_redirects=False)
        assert "Der Spiegel ließ sich nicht schreiben" in unquote(antwort.headers["location"])

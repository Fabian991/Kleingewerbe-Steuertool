"""Befunde des Browser-Durchlaufs (Oberflaeche) -- je Befund eine Regression.

Der Pruefer hat die Anwendung im echten Browser bedient: Filter, Formulare,
Fehlerseiten, Druckansicht, schmale Bildschirme. Was er fand, sind keine
Rechenfehler, sondern Stellen, an denen die Oberflaeche den Nutzer im Stich
laesst -- eine 422-Seite fuer "alle", verlorene Eingaben, rohes JSON, ein
Download statt einer Anzeige, zerrissene Daten. Die Tests pruefen das am
gerenderten HTML und am Stylesheet; die Breiten selbst misst ein
Browserlauf ausserhalb der Testsuite.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.logik.beleg import eigenbeleg, xml_leser
from app.main import erzeuge_app

WURZEL = Path(__file__).resolve().parent.parent
FIXTURES = WURZEL / "tests" / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
UBL = FIXTURES / "xrechnung_ubl.xml"
STIL = (WURZEL / "app" / "static" / "stil.css").read_text(encoding="utf-8")
VORLAGEN = WURZEL / "app" / "vorlagen"

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


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def kunde(klient):
    antwort = klient.post("/kunden", data=KUNDE, follow_redirects=False)
    assert antwort.status_code == 303
    return int(antwort.headers["location"].rsplit("/", 1)[1])


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen() -> sqlite3.Connection:
        verbindung = sqlite3.connect(tmp_path / "web.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung

    return oeffnen


def _hochladen(klient, datei: Path, name: str | None = None) -> str:
    antwort = klient.post(
        "/belege/hochladen",
        files={"datei": (name or datei.name, datei.read_bytes(), "application/octet-stream")},
        follow_redirects=False,
    )
    assert antwort.status_code == 303, antwort.text[:300]
    return antwort.headers["location"].rsplit("/", 1)[1]


def _beleg(klient, **felder) -> None:
    daten = {"belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100",
             "lieferant_neu": "Hetzner Online GmbH", **felder}
    antwort = klient.post(f"/belege/pruefen/{_hochladen(klient, PDF_DE)}", data=daten,
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:500]


def _meldung_aus_redirect(antwort, name: str) -> str:
    assert antwort.status_code == 303, antwort.text[:300]
    return unquote(parse_qs(urlsplit(antwort.headers["location"]).query).get(name, [""])[0])


# ------------------------------------------------- 1 Belege-Filter "alle"

class TestBelegeFilter:
    @pytest.mark.parametrize("query", [
        "jahr=&kategorie=&offen=1", "jahr=2026&kategorie=", "jahr=&kategorie=5",
        "jahr=abc", "kategorie=x&offen=ja", "jahr=99999999999999999999999",
    ])
    def test_leere_und_unsinnige_werte_schraenken_nicht_ein(self, klient, query):
        """Das Formular sendet fuer "alle" einen leeren Wert; vorher 422."""
        _beleg(klient)
        antwort = klient.get(f"/belege?{query}")
        assert antwort.status_code == 200
        if "jahr=2026" in query or "kategorie=5" not in query:
            assert 'id="beleg-1"' in antwort.text

    def test_jahr_filtert_weiter(self, klient):
        _beleg(klient)
        assert 'id="beleg-1"' in klient.get("/belege?jahr=2026&kategorie=").text
        assert 'id="beleg-1"' not in klient.get("/belege?jahr=2025&kategorie=").text

    def test_kategorie_filtert_weiter(self, klient, datenbank):
        with datenbank() as conn:
            kat = conn.execute(
                "SELECT id FROM euer_kategorie WHERE schluessel = 'HOSTING'").fetchone()[0]
        _beleg(klient, kategorie_id=str(kat))
        assert 'id="beleg-1"' in klient.get(f"/belege?jahr=&kategorie={kat}").text
        assert 'id="beleg-1"' not in klient.get(f"/belege?jahr=&kategorie={kat + 1}").text

    def test_nur_ungeklaerte_allein(self, klient):
        _beleg(klient)
        seite = klient.get("/belege?jahr=&kategorie=&offen=1").text
        assert 'id="beleg-1"' in seite
        assert "Filter aufheben" in seite


# ------------------------------------------- 2 Zeiterfassung: Eingaben bleiben

class TestZeitEingabenBleiben:
    def test_erfassen_zeigt_formular_mit_eingaben(self, klient, kunde, datenbank):
        antwort = klient.post("/zeiten", data={
            "kunde_id": str(kunde), "datum": "2026-10-01", "titel": "Kickoff Workshop",
            "dauer": "x", "beschreibung": "lang & ausführlich",
        }, follow_redirects=False)
        assert antwort.status_code == 422
        seite = antwort.text
        assert "ist keine Dauer" in seite
        assert f'<option value="{kunde}" selected>' in seite
        assert 'value="2026-10-01"' in seite
        assert 'value="Kickoff Workshop"' in seite
        assert 'name="dauer" required placeholder="1:45" size="7" aria-label="Dauer"' in seite
        assert 'value="x"' in seite
        # Seit Version 21 ein mehrzeiliges Feld (textarea) statt einer Zeile.
        assert '>lang &amp; ausführlich</textarea>' in seite
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0

    def test_timer_start_zeigt_formular_mit_eingaben(self, klient, kunde):
        antwort = klient.post("/zeiten/timer/start", data={"kunde_id": "", "titel": "Support"},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "Bitte einen Kunden wählen." in antwort.text
        assert 'value="Support"' in antwort.text
        antwort = klient.post("/zeiten/timer/start",
                              data={"kunde_id": str(kunde), "titel": "  "},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "Der Titel fehlt." in antwort.text
        timerformular = antwort.text.split('action="/zeiten/timer/start"')[1].split("</form>")[0]
        assert f'<option value="{kunde}" selected>' in timerformular

    def test_erfolg_bleibt_ein_redirect(self, klient, kunde):
        antwort = klient.post("/zeiten", data={
            "kunde_id": str(kunde), "datum": "2026-10-01", "titel": "Workshop", "dauer": "1:00",
        }, follow_redirects=False)
        assert antwort.status_code == 303


# ------------------------------------------------------------- 3 404-Seite

class TestNichtGefunden:
    @pytest.mark.parametrize("pfad", ["/gibtsnicht", "/rechnung/1/gibtsnicht", "/a/b/c"])
    def test_deutsche_seite_statt_json(self, klient, pfad):
        antwort = klient.get(pfad)
        assert antwort.status_code == 404
        assert antwort.headers["content-type"].startswith("text/html")
        assert "Diese Seite gibt es nicht" in antwort.text
        assert 'href="/"' in antwort.text
        assert "Not Found" not in antwort.text

    def test_formularadresse_per_get(self, klient):
        """Nach einer 422-Antwort steht die Formularadresse in der Adresszeile."""
        antwort = klient.get("/zeiten/timer/start")
        assert antwort.status_code == 405
        assert "nur Formulare" in antwort.text
        assert "Method Not Allowed" not in antwort.text

    def test_umleitungen_mit_meldung_bleiben(self, klient):
        antwort = klient.get("/rechnung/999", follow_redirects=False)
        assert antwort.status_code == 303

    def test_lange_adresse_wird_gekuerzt(self, klient):
        antwort = klient.get("/" + "x" * 500)
        assert antwort.status_code == 404
        assert "x" * 100 not in antwort.text


# -------------------------------------------------------------- 4 favicon

def test_leeres_favicon_verhindert_404_in_der_konsole(klient):
    assert '<link rel="icon" href="data:,">' in klient.get("/").text


# ------------------------------------------- 5 PDF und Datei im Browser zeigen

class TestAnzeigenStattHerunterladen:
    def test_belegdatei_inline_mit_dateiname(self, klient):
        name = _hochladen(klient, PDF_DE, "Rechnung März.pdf")
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100"})
        kopf = klient.get("/belege/1/datei").headers["content-disposition"]
        assert kopf.startswith("inline;")
        assert "filename*=utf-8''Rechnung%20M%C3%A4rz.pdf" in kopf
        assert 'filename="Rechnung Marz.pdf"' in kopf
        kopf = klient.get("/belege/1/datei?download=1").headers["content-disposition"]
        assert kopf.startswith("attachment;")
        seite = klient.get("/belege").text
        assert 'href="/belege/1/datei?download=1"' in seite

    def test_rechnungs_pdf_inline(self, klient, kunde, datenbank):
        klient.post("/zeiten", data={"kunde_id": str(kunde), "titel": "Beratung",
                                     "datum": "2026-03-02", "dauer": "60"})
        antwort = klient.post("/rechnung/neu", data={
            "kunde_id": kunde, "datum": "2026-03-10", "modus": "PRO_TITEL",
            "einleitung": "", "schluss": "", "zeit": [1]}, follow_redirects=False)
        rid = int(antwort.headers["location"].rsplit("/", 1)[1])
        seite = klient.get(f"/rechnung/{rid}").text
        version = re.search(r'name="version" value="(\d+)"', seite).group(1)
        codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
        klient.post(f"/rechnung/{rid}/festschreiben",
                    data={"version": version, "bestaetigt": codes})
        antwort = klient.get(f"/rechnung/{rid}/pdf")
        assert antwort.status_code == 200
        assert antwort.headers["content-type"] == "application/pdf"
        kopf = antwort.headers["content-disposition"]
        assert kopf.startswith("inline;") and "RE-2026-0001" in kopf
        kopf = klient.get(f"/rechnung/{rid}/pdf?download=1").headers["content-disposition"]
        assert kopf.startswith("attachment;")
        seite = klient.get(f"/rechnung/{rid}").text
        assert f'href="/rechnung/{rid}/pdf?download=1">herunterladen' in seite


# ------------------------------------------------ 6 Umbruch: Daten und Woerter

class TestUmbruch:
    def test_woerter_brechen_nur_wenn_noetig(self):
        assert "overflow-wrap: anywhere; }" not in STIL.split("code.pfad")[0]
        assert re.search(r"td:not\(\.zahl\):not\(\.mono\):not\(\.aktionen\)\s*\{\s*"
                         r"overflow-wrap:\s*break-word;\s*hyphens:\s*auto", STIL)

    def test_datum_mono_und_chip_bleiben_einzeilig(self):
        assert re.search(r"td\.datum,\s*\.mono:not\(pre\)\s*\{\s*white-space:\s*nowrap", STIL)
        chip = STIL.split("\n.chip {", 1)[1].split("}", 1)[0]
        assert "white-space: nowrap" in chip

    def test_nummer_im_fliesstext_und_pfad(self):
        """Die Nummer wandert als Ganzes (inline-block) in die naechste Zeile;
        ein langer Pfad bricht in seinem Kasten um, statt die Seite aufzuziehen
        (nowrap tat das auf /backup bei 420 px)."""
        regel = STIL.split("p.hinweis code {", 1)[1].split("}", 1)[0]
        assert "display: inline-block" in regel
        assert "max-width: 100%" in regel
        assert "overflow-wrap: anywhere" in regel
        assert "nowrap" not in regel
        assert re.search(r"code\.pfad\s*\{\s*overflow-wrap:\s*anywhere", STIL)

    def test_sprache_fuer_silbentrennung(self, klient):
        assert '<html lang="de">' in klient.get("/").text

    def test_datumszellen_tragen_die_klasse(self, klient):
        _beleg(klient, bezahlt_am="2026-03-15")
        seite = klient.get("/belege").text
        assert '<td class="datum">14.03.2026' in seite
        assert '<td class="datum">15.03.2026' in seite
        assert 'class="mono datum"' in (VORLAGEN / "rechnungen.html").read_text(encoding="utf-8")

    def test_journalpfad_darf_umbrechen(self, klient):
        klient.post("/journal/export")
        assert '<code class="pfad">' in klient.get("/journal").text


# ------------------------------------------------------ 7 Mobil: Reisen

def test_felder_passen_in_schmale_karten():
    """Bei 420 px ist die Kilometer-Karte 176 px breit, die Felder waren 210 px."""
    felder = STIL.split(".felder {", 1)[1].split("}", 1)[0]
    assert "minmax(min(210px, 100%), 1fr)" in felder
    assert "minmax(min(170px, 100%), 1fr)" in STIL


# ------------------------------------------------------------- 8 Labels

class _Felder(HTMLParser):
    """Sammelt Eingabefelder ohne Label: weder umschliessendes <label>, noch
    label[for], noch aria-label."""

    def __init__(self) -> None:
        super().__init__()
        self.tiefe_label = 0
        self.ohne: list[str] = []

    def handle_starttag(self, tag, attrs):
        werte = dict(attrs)
        if tag == "label":
            self.tiefe_label += 1
        elif tag in ("input", "select", "textarea"):
            if werte.get("type") in ("hidden", "submit"):
                return
            if not (self.tiefe_label or werte.get("aria-label") or werte.get("id")):
                self.ohne.append(f"{tag} name={werte.get('name')}")

    def handle_endtag(self, tag):
        if tag == "label":
            self.tiefe_label -= 1


def test_jedes_feld_der_zeiterfassung_hat_ein_label(klient, kunde, datenbank):
    klient.post("/zeiten", data={"kunde_id": str(kunde), "titel": "Beratung",
                                 "datum": "2026-03-02", "dauer": "60"})
    klient.post("/zeiten", data={"kunde_id": str(kunde), "titel": "Kulanz",
                                 "datum": "2026-03-03", "dauer": "30"})
    klient.post("/zeiten/2/modus", data={"modus": "KULANZ", "grund": "Kulanz"})
    seite = klient.get("/zeiten").text
    pruefer = _Felder()
    pruefer.feed(seite)
    assert pruefer.ohne == []
    # Und vorher stillen Felder tragen jetzt eines.
    assert seite.count('aria-label="Grund für') >= 3
    assert 'aria-label="Titel"' in seite
    assert 'aria-label="Beschreibung (optional)"' in seite
    klient.post("/zeiten/2/modus", data={"modus": "STUNDEN"})
    klient.post("/zeiten/1/loeschen")
    klient.post("/zeiten/2/loeschen")
    seite = klient.get("/zeiten").text
    assert 'aria-label="Woran arbeitest du? (Titel für den Timer)"' in seite


# ------------------------------------------------------ 9 Laufender Timer

class TestLaufenderTimer:
    def test_banner_und_liste_zeigen_dieselbe_dauer_ohne_betrag(self, klient, kunde):
        klient.post("/zeiten/timer/start", data={"kunde_id": str(kunde), "titel": "Support"})
        seite = klient.get("/zeiten").text
        banner = re.search(r'Läuft seit.*?<span class="mono">([\d:]+)</span>', seite, re.S)
        assert banner
        zeile = seite.split('<table class="zeitenliste">', 1)[1].split("</tr>", 1)[0]
        assert f'<td class="mono">{banner.group(1)}</td>' in zeile
        assert "läuft" in zeile
        assert "€" not in zeile

    def test_banner_und_liste_nach_einer_stunde(self, klient, kunde, datenbank):
        klient.post("/zeiten/timer/start", data={"kunde_id": str(kunde), "titel": "Support"})
        start = (_dt.datetime.now() - _dt.timedelta(minutes=75)).strftime("%H:%M:%S")
        if _dt.datetime.now().strftime("%H:%M:%S") < start:
            pytest.skip("Kurz nach Mitternacht laeuft die Probe ueber den Tag")
        from app.logik import zeiten

        with datenbank() as conn:
            conn.execute("UPDATE zeiteintrag SET start_zeit = ?", (start,))
            # Seit Version 21 rechnet der Timer aus seinen Arbeitsabschnitten:
            # der offene Abschnitt wandert mit dem Beginn zurueck.
            conn.execute("UPDATE zeit_abschnitt SET beginn_utc = ? WHERE ende_utc IS NULL",
                         (zeiten.ortszeit_als_utc(_dt.date.today().isoformat(), start),))
        seite = klient.get("/zeiten").text
        banner = re.search(r'Läuft seit.*?<span class="mono">([\d:]+)</span>', seite, re.S)
        # Der Banner rechnet minutengenau gegen die Uhrzeit: 1:14 oder 1:15.
        assert banner and banner.group(1) in ("1:14", "1:15"), banner
        zeile = seite.split('<table class="zeitenliste">', 1)[1].split("</tr>", 1)[0]
        assert f'<td class="mono">{banner.group(1)}</td>' in zeile
        assert "0:01" not in seite, "nicht der Platzhalter von einer Minute"

    def test_uebersicht_zaehlt_laufenden_timer_nicht(self, klient, kunde):
        klient.post("/zeiten/timer/start", data={"kunde_id": str(kunde), "titel": "Support"})
        seite = klient.get("/").text
        assert "Keine offenen Zeiten." in seite
        klient.post("/zeiten", data={"kunde_id": str(kunde), "titel": "Beratung",
                                     "datum": "2026-03-02", "dauer": "30"})
        seite = klient.get("/").text
        assert "0,50 h" in seite
        assert "0,52 h" not in seite and "0,51 h" not in seite

    def test_stoppen_meldet_den_erfolg(self, klient, kunde):
        """Seit Version 21 sagt die Meldung auch, wie abgerechnet wird: erfasst
        wird minutengenau, gerundet erst je Rechnungsposition."""
        klient.post("/zeiten/timer/start", data={"kunde_id": str(kunde), "titel": "Support"})
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        erwartet = ("Gestoppt: 0:01 erfasst (Support) — abgerechnet wird je "
                    "Rechnungsposition auf volle 15 Minuten aufgerundet.")
        assert _meldung_aus_redirect(antwort, "erfolg") == erwartet
        seite = klient.get(antwort.headers["location"]).text
        assert f'<div class="meldung erfolg">{erwartet}</div>' in seite

    def test_stoppen_ohne_timer_meldet_nichts(self, klient, kunde):
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert "erfolg=" not in antwort.headers["location"]


# ------------------------------------------------- 10 Spalten buendig

def test_zeitenliste_hat_feste_spalten(klient, kunde):
    for tag in ("2026-03-02", "2026-03-03"):
        klient.post("/zeiten", data={"kunde_id": str(kunde), "titel": "Beratung",
                                     "datum": tag, "dauer": "60"})
    seite = klient.get("/zeiten").text
    assert seite.count('<table class="zeitenliste">') == 2
    assert seite.count('<col class="sp-status">') == 2
    assert re.search(r"table\.zeitenliste\s*\{\s*table-layout:\s*fixed", STIL)
    for spalte in ("sp-dauer", "sp-status", "sp-betrag", "sp-aktionen"):
        assert re.search(rf"col\.{spalte}\s*\{{\s*width:", STIL), spalte


# ------------------------------------------------- 11 Anlagegut in der Liste

class TestAnlagegutInDerListe:
    def test_noch_nicht_im_verzeichnis(self, klient):
        _beleg(klient, anlagegut="1", brutto_euro="1.190,00")
        seite = klient.get("/belege").text
        assert "Anlagegut — noch nicht im Verzeichnis" in seite
        assert "nicht zugeordnet</span>" not in seite

    def test_ueber_afa(self, klient):
        _beleg(klient, anlagegut="1", brutto_euro="1.190,00", belegdatum="2026-01-05")
        antwort = klient.post("/anlagen", data={
            "bezeichnung": "Schreibtisch", "anschaffung_am": "2026-01-05",
            "brutto_euro": "1.190,00", "ust_satz": "19", "nutzungsdauer_jahre": "3",
            "art": "BEWEGLICH", "aufteilungsquote": "100", "beleg_id": "1",
        }, follow_redirects=False)
        assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
        seite = klient.get("/belege").text
        assert '<span class="chip">über AfA</span>' in seite
        assert "nicht zugeordnet</span>" not in seite
        assert "noch nicht im Verzeichnis" not in seite

    def test_gewoehnlicher_beleg_bleibt_rot(self, klient):
        _beleg(klient)
        assert '<span class="chip ueberfaellig">nicht zugeordnet</span>' in (
            klient.get("/belege").text)


# ------------------------------------------------- 12 Dublette nur einmal

def test_dublettenmeldung_steht_einmal(klient):
    _beleg(klient)
    zweiter = _hochladen(klient, PDF_DE, "nochmal.pdf")
    seite = klient.get(f"/belege/pruefen/{zweiter}").text
    assert seite.count("Byte für Byte") == 1
    antwort = klient.post(f"/belege/pruefen/{zweiter}", data={
        "belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100"})
    assert antwort.text.count("Byte für Byte") == 1
    assert "bereits als Beleg" not in antwort.text


# ---------------------------------------- 13 / 14 Deutsche Zahlen und Daten

def test_anzahlungshinweis_im_deutschen_betragsformat():
    vorschlag = xml_leser.lies(UBL.read_bytes())
    hinweis = next(m for m in vorschlag.meldungen if "Anzahlung" in m)
    assert "Zu zahlen sind 150,00 €, die Leistung kostet aber 180,00 €" in hinweis
    assert "150.00" not in hinweis


def test_eigenbeleg_begruendung_mit_deutschen_daten(klient, datenbank):
    klient.post("/reisen", data={
        "bezeichnung": "PyCon DE 2026", "ziel": "Darmstadt", "abreise_am": "2026-04-20",
        "rueckkehr_am": "2026-04-22", "anlass": "Fachkonferenz", "inland": "1",
        "abwesend_stunden": "", "uebernachtung": ["0", "1"], "notiz": "",
    })
    antwort = klient.post("/reisen/1/verpflegung", follow_redirects=False)
    assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
    with datenbank() as conn:
        text = conn.execute("SELECT notiz FROM beleg WHERE ist_eigenbeleg = 1").fetchone()[0]
    assert "vom 20.04.2026 bis 22.04.2026" in text
    assert "2026-04-20" not in text
    assert eigenbeleg._datum_de("2026-04-20") == "20.04.2026"


# ------------------------------------------------------------- 15 Druck

class TestDruck:
    def test_druckregel_blendet_formulare_aus(self):
        druck = STIL.split("@media print", 1)[1]
        assert re.search(r"form,\s*details,\s*\.nicht-drucken\s*\{\s*display:\s*none", druck)

    def test_rechnung_markiert_die_bearbeitungsabschnitte(self):
        vorlage = (VORLAGEN / "rechnung.html").read_text(encoding="utf-8")
        aktionen = vorlage.split("<h2>Aktionen</h2>")[0].rsplit("<div", 1)[1]
        assert 'class="nicht-drucken"' in aktionen
        # Seit dem Rechnungsablauf steht "Stornieren" im Abschnitt "Etwas
        # falsch?" (Korrigieren, Zuruecknehmen, nur Stornieren), die
        # Ablaufleiste (Versand, Zahlung) oben -- beide nicht auf Papier.
        korrektur = vorlage.split("<h2>Etwas falsch?</h2>")[0].rsplit("<div", 1)[1]
        assert 'class="nicht-drucken korrektur"' in korrektur
        assert '<ol class="ablauf nicht-drucken">' in vorlage
        assert 'action="/rechnung/{{ r.id }}/zahlung" class="zeile nicht-drucken"' in vorlage
        assert 'rueckzahlung" class="zeile nicht-drucken"' in vorlage
        # Drei seit Version 21: dazu kommt im Entwurf der Hinweis auf eine
        # geaenderte Rundungsregel des Kunden (mit Knopf, also nicht drucken).
        assert vorlage.count('<div class="meldung warn nicht-drucken">') == 3
        for titel in ("Gruppierung ändern", "Rundung", "Kopf und Texte"):
            assert f'<h2 class="nicht-drucken">{titel}</h2>' in vorlage
        # Entwurf loeschen: oben, zwei Schritte ohne Skript, nicht auf Papier.
        assert '<div class="entwurfkopf nicht-drucken">' in vorlage
        assert '<details class="loeschen">' in vorlage



# ---------------------------------- Nachbesserung der Integration (Pruefung)

def _regel(selektor: str) -> str:
    """Der Rumpf der CSS-Regel mit genau diesem Selektor."""
    treffer = re.search(re.escape(selektor) + r"\s*\{([^}]*)\}", STIL)
    assert treffer, selektor
    return treffer.group(1)


class TestAktionsspalten:
    """Aufgeklappte Formulare in der Aktionsspalte duerfen die Seite nicht
    seitlich aufziehen (bei 1280 px liefen Rechnungs- und Belegliste rechts
    hinaus; gemessen mit Playwright, hier das, was es verhindert)."""

    def test_rechnungsliste_untereinander_und_eines_je_zeile(self):
        vorlage = (VORLAGEN / "rechnungen.html").read_text(encoding="utf-8")
        assert '<table class="rechnungsliste">' in vorlage
        # Je Zeile hoechstens ein offenes Formular (exklusive details).
        assert vorlage.count('<details name="aktion-{{ r.id }}">') == 2
        assert "white-space: normal" in _regel("table.rechnungsliste td.aktionen")
        assert "display: block" in _regel("table.rechnungsliste td.aktionen details")
        assert "max-width" in _regel("table.rechnungsliste td.aktionen details form")
        # "Rückzahlung offen" ueber dem Betrag statt in einer breiten Zeile.
        assert '<span class="offen-art">Rückzahlung offen</span>' in vorlage
        assert "display: block" in _regel("table.rechnungsliste .offen-art")

    def test_belegliste_stimmt_so_eigener_block(self):
        vorlage = (VORLAGEN / "belege.html").read_text(encoding="utf-8")
        assert '<table class="belegliste">' in vorlage
        assert "white-space: normal" in _regel("table.belegliste td.aktionen")
        assert "display: block" in _regel("table.belegliste td.aktionen form.inline")
        assert "display: block" in _regel("table.belegliste td.aktionen details")

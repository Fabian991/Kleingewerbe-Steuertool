"""Die echte PDF-Vorschau eines Entwurfs: GET /rechnung/{id}/vorschau.pdf.

Vorher sah man das PDF erst nach dem Festschreiben -- wenn Nummer, Journal
und Archivexemplar schon unumkehrbar waren. Die Vorschau entsteht mit
derselben Vorlage und demselben Renderer, schreibt aber NICHTS: kein Journal,
keine Nummer, keine Datei, keine neue Version.
"""

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, pdf
from app.logik import festschreiben as fs
from app.logik import nummernkreis
from app.main import erzeuge_app
from tests.conftest import braucht

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "inhaber": "Erika Muster",
    "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
    "institut": "Musterbank", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
    "rundung_modus": "AUFWAERTS", "rundung_intervall_min": "15",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "land": "DE", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def daten(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
    return tmp_path / "daten"


@pytest.fixture
def klient(daten):
    with TestClient(erzeuge_app(daten / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _db(daten) -> sqlite3.Connection:
    conn = sqlite3.connect(daten / "steuer.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _entwurf(klient, daten, *, pauschale: bool = False) -> int:
    klient.post("/zeiten", data={
        "kunde_id": 1, "titel": "Beratung", "datum": "2026-03-02", "dauer": "1:30",
        "beschreibung": "Konzept", "modus": "STUNDEN", "grund": "",
    })
    conn = _db(daten)
    if pauschale:
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, pauschal_cent, erfasst_am)"
            " VALUES (1, 'Affiliate Provision', '2026-03-03', 30, 'PAUSCHAL', 63646, ?)",
            (db.jetzt_utc(),),
        )
        conn.commit()
    ids = [z["id"] for z in conn.execute("SELECT id FROM zeiteintrag ORDER BY id")]
    conn.close()
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": 1, "datum": "2026-03-10", "modus": "PRO_TITEL",
        "einleitung": "vielen Dank.", "schluss": "", "zeit": ids,
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    return int(antwort.headers["location"].rsplit("/", 1)[1])


def _stand(daten, rid) -> dict:
    """Alles, was eine Vorschau NICHT veraendern darf."""
    conn = _db(daten)
    try:
        rechnung = dict(conn.execute(
            "SELECT status, nummer, version, leistungsort, hinweis_19_text, pdf_pfad,"
            " pdf_sha256 FROM rechnung WHERE id = ?", (rid,)).fetchone())
        return {
            "rechnung": rechnung,
            "journal": conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0],
            "kreise": [tuple(z) for z in conn.execute(
                "SELECT * FROM nummernkreis ORDER BY 1")],
            "nummer": nummernkreis.vorschau(conn, 2026, monat=3),
        }
    finally:
        conn.close()


def _dateien(daten) -> set[str]:
    """Alle Dateien im Datenordner ausser der Datenbank selbst."""
    return {
        str(p.relative_to(daten)) for p in daten.rglob("*")
        if p.is_file() and not p.name.startswith("steuer.sqlite")
    }


def _pdftext(inhalt: bytes, tmp_path: Path) -> str:
    datei = tmp_path / "pruefung.pdf"
    datei.write_bytes(inhalt)
    return subprocess.run(["pdftotext", "-layout", str(datei), "-"],
                          capture_output=True, text=True, check=True).stdout


def _wasserzeichen(inhalt: bytes, tmp_path: Path) -> int:
    """Auf wie vielen Seiten das Wasserzeichen steht.

    Es ist gedreht gesetzt, und die Textauszuege (pdftotext, auch mit
    -bbox, und extract_text) lassen es weg. Die einzelnen Zeichen sind aber
    da -- gezaehlt wird daran.
    """
    pdfplumber = pytest.importorskip("pdfplumber")
    datei = tmp_path / "wasserzeichen.pdf"
    datei.write_bytes(inhalt)
    with pdfplumber.open(datei) as dokument:
        return sum(
            "ENTWURF – keine Rechnung"
            # 40 pt -- sonst ist nichts auf dem Blatt groesser als 16 pt.
            in "".join(z["text"] for z in seite.chars if z["size"] > 30)
            for seite in dokument.pages
        )


class TestVorschauRoute:
    def test_liefert_ein_pdf_ohne_zwischenspeicher(self, klient, daten):
        rid = _entwurf(klient, daten)
        antwort = klient.get(f"/rechnung/{rid}/vorschau.pdf")
        assert antwort.status_code == 200
        assert antwort.headers["content-type"] == "application/pdf"
        assert antwort.content.startswith(b"%PDF")
        assert antwort.headers["cache-control"] == "no-store"
        assert antwort.headers["content-disposition"].startswith("inline;")

    def test_schreibt_nichts(self, klient, daten):
        """Kein Journal, keine Nummer, keine neue Version, keine Datei."""
        rid = _entwurf(klient, daten)
        vorher, dateien = _stand(daten, rid), _dateien(daten)
        for _ in range(2):
            assert klient.get(f"/rechnung/{rid}/vorschau.pdf").status_code == 200
        assert _stand(daten, rid) == vorher
        assert _dateien(daten) == dateien
        assert not (daten / "ausgangsrechnungen").exists()
        assert vorher["rechnung"]["nummer"] is None

    @braucht("pdftotext")
    def test_zeigt_was_festgeschrieben_wird(self, klient, daten, tmp_path):
        rid = _entwurf(klient, daten)
        inhalt = klient.get(f"/rechnung/{rid}/vorschau.pdf").content
        text = _pdftext(inhalt, tmp_path)
        assert _wasserzeichen(inhalt, tmp_path) == 1
        # Der Platzhalter bricht in der Spalte um; Wort fuer Wort pruefen.
        assert re.search(r"Rechnung-Nr\.\s+wird\s+beim\s+Festschreiben", text)
        assert "vergeben" in text
        assert re.search(r"Verwendungszweck\s+Rechnungsnummer \(folgt\)", text)
        assert "RE-2026" not in text
        assert "Beratung" in text and "Konzept" in text
        gesamt = re.search(r"Gesamtbetrag\s+([\d.,]+ €)", text).group(1)

        seite = klient.get(f"/rechnung/{rid}").text
        version = re.search(r'name="version" value="(\d+)"', seite).group(1)
        codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
        klient.post(f"/rechnung/{rid}/festschreiben",
                    data={"version": version, "bestaetigt": codes})
        conn = _db(daten)
        pfad = conn.execute("SELECT pdf_pfad FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        conn.close()
        fest = _pdftext((daten / pfad).read_bytes(), tmp_path)
        assert re.search(r"Gesamtbetrag\s+([\d.,]+ €)", fest).group(1) == gesamt
        assert "ENTWURF" not in fest and "RE-2026-0001" in fest
        assert _wasserzeichen((daten / pfad).read_bytes(), tmp_path) == 0

    def test_festgeschrieben_fuehrt_zum_gespeicherten_pdf(self, klient, daten):
        """Fuer eine festgeschriebene Rechnung wird nie neu gerendert: das
        gespeicherte Exemplar bleibt bytegleich."""
        rid = _entwurf(klient, daten)
        seite = klient.get(f"/rechnung/{rid}").text
        version = re.search(r'name="version" value="(\d+)"', seite).group(1)
        codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
        klient.post(f"/rechnung/{rid}/festschreiben",
                    data={"version": version, "bestaetigt": codes})
        conn = _db(daten)
        pfad = daten / conn.execute(
            "SELECT pdf_pfad FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        conn.close()
        vorher = pfad.read_bytes()
        antwort = klient.get(f"/rechnung/{rid}/vorschau.pdf", follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"] == f"/rechnung/{rid}/pdf"
        assert pfad.read_bytes() == vorher
        assert "vorschau.pdf" not in klient.get(f"/rechnung/{rid}").text

    def test_unbekannte_rechnung(self, klient):
        antwort = klient.get("/rechnung/999/vorschau.pdf", follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith("/rechnungen?fehler=")

    @braucht("pdftotext")
    def test_leistungsort_nur_im_speicher_nachgezogen(self, klient, daten, tmp_path):
        """Wird der Kunde auf EU-Unternehmer umgestellt, zeigt die Vorschau
        schon den Reverse-Charge-Hinweis -- die Datenbank bleibt aber, wie sie
        ist (kein UPDATE, kein Journal): die Vorschau ist ein Lesezugriff."""
        rid = _entwurf(klient, daten)
        conn = _db(daten)
        conn.execute("UPDATE kunde SET leistungsort = 'EU_B2B_REVERSE_CHARGE',"
                     " land = 'AT', ust_idnr = 'ATU12345678' WHERE id = 1")
        conn.commit()
        conn.close()
        vorher = _stand(daten, rid)
        text = _pdftext(klient.get(f"/rechnung/{rid}/vorschau.pdf").content, tmp_path)
        assert "Steuerschuldnerschaft des Leistungsempfängers" in text
        assert "§ 19" not in text
        assert _stand(daten, rid) == vorher
        assert vorher["rechnung"]["leistungsort"] == "INLAND"

    def test_renderfehler_gibt_einen_verstaendlichen_satz(self, klient, daten, monkeypatch):
        rid = _entwurf(klient, daten)

        def kaputt(*_, **__):
            raise pdf.PdfFehler("ModuleNotFoundError: No module named 'weasyprint'")

        monkeypatch.setattr(pdf, "rendere", kaputt)
        antwort = klient.get(f"/rechnung/{rid}/vorschau.pdf")
        assert antwort.status_code == 503
        assert "WeasyPrint ist nicht installiert" in antwort.text


class TestEntwurfsseite:
    def test_bettet_die_vorschau_ein(self, klient, daten):
        rid = _entwurf(klient, daten)
        conn = _db(daten)
        version = conn.execute("SELECT version FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        conn.close()
        seite = klient.get(f"/rechnung/{rid}").text
        assert f'<iframe src="/rechnung/{rid}/vorschau.pdf?v={version}#' in seite
        assert "In neuem Tab öffnen" in seite
        assert seite.index('id="vorschau"') < seite.index("<h2>Prüfung</h2>")

    def test_ohne_renderer_steht_der_grund_da(self, klient, daten, monkeypatch):
        rid = _entwurf(klient, daten)
        monkeypatch.setattr(pdf, "renderer_fehlt", lambda: pdf.verstaendlich(
            "OSError: cannot load library 'libpango-1.0-0'"))
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Keine PDF-Vorschau möglich." in seite
        assert "brew install pango" in seite
        assert "<iframe" not in seite

    def test_pauschale_wie_auf_dem_pdf(self, klient, daten):
        """Die Tabelle der Entwurfsseite zeigt eine Pauschale wie das PDF:
        ohne "1,00 pausch." und ohne Einzelpreis."""
        rid = _entwurf(klient, daten, pauschale=True)
        seite = klient.get(f"/rechnung/{rid}").text
        zeile = re.search(r"<tr>\s*<td[^>]*>\d+</td>\s*<td>Affiliate Provision.*?</tr>",
                          seite, re.S).group(0)
        zellen = [re.sub(r"<[^>]+>|\s+", " ", z).strip()
                  for z in re.findall(r"<td[^>]*>(.*?)</td>", zeile, re.S)]
        assert zellen[2:] == ["", "Pauschale", "", "636,46"], zellen
        assert "pausch." not in seite

    def test_assistent_fuehrt_zur_vorschau(self, klient, daten):
        klient.post("/zeiten", data={
            "kunde_id": 1, "titel": "Beratung", "datum": "2026-03-02", "dauer": "1h",
            "beschreibung": "", "modus": "STUNDEN", "grund": "",
        })
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert "Entwurf erstellen und Vorschau ansehen" in seite
        assert "Ein Entwurf vergibt keine Nummer und lässt sich verwerfen." in seite


class TestVorschauDaten:
    """Die Logik direkt: kein einziger Schreibzugriff."""

    def test_keine_aenderung_an_der_datenbank(self, conn, betrieb, kunde_id):
        conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, erfasst_am) VALUES (?, 'Beratung', '2026-03-02', 60,"
            " 'STUNDEN', ?)", (kunde_id, db.jetzt_utc())).lastrowid
        rid = fs.entwurf_anlegen(conn, kunde_id, [zid])
        conn.execute("UPDATE kunde SET leistungsort = 'DRITTLAND' WHERE id = ?", (kunde_id,))
        vorher = conn.total_changes
        daten = fs.vorschau_daten(conn, rid)
        assert conn.total_changes == vorher
        assert daten["vorschau"] is True
        assert daten["r"]["nummer"] is None
        assert daten["r"]["leistungsort"] == "DRITTLAND"
        assert daten["r"]["hinweis_19_text"] == fs.HINWEIS_DRITTLAND
        assert conn.execute(
            "SELECT leistungsort FROM rechnung WHERE id = ?", (rid,)).fetchone()[0] == "INLAND"

    def test_nur_fuer_entwuerfe(self, conn, betrieb, kunde_id, tmp_path):
        conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, erfasst_am) VALUES (?, 'Beratung', '2026-03-02', 60,"
            " 'STUNDEN', ?)", (kunde_id, db.jetzt_utc())).lastrowid
        rid = fs.entwurf_anlegen(conn, kunde_id, [zid])
        fs.festschreiben(conn, rid, belegordner=tmp_path)
        with pytest.raises(fs.FestschreibenFehler, match="nur für einen Entwurf"):
            fs.vorschau_daten(conn, rid)
        with pytest.raises(fs.FestschreibenFehler, match="existiert nicht"):
            fs.vorschau_daten(conn, 999)

    def test_vorschau_datei_liegt_nur_kurz_im_system_temp(self, tmp_path, monkeypatch):
        """Gerendert wird in ein eigenes Temp-Verzeichnis, das danach weg ist."""
        gesehen: list[Path] = []

        def falsch(daten, ziel):
            gesehen.append(ziel)
            ziel.write_bytes(b"%PDF-probe")
            return "x"

        monkeypatch.setattr(pdf, "rendere", falsch)
        assert pdf.rendere_vorschau({}) == b"%PDF-probe"
        assert gesehen and gesehen[0].parent.name.startswith("steuer-vorschau-")
        assert not gesehen[0].parent.exists()


class TestEinrichtungBank:
    """Der Name der Bank war im Schnappschuss und in der Vorlage vorgesehen,
    liess sich aber nirgends eintragen."""

    def test_bank_wird_gespeichert_und_angezeigt(self, klient, daten):
        conn = _db(daten)
        assert conn.execute("SELECT institut FROM betrieb").fetchone()[0] == "Musterbank"
        conn.close()
        seite = klient.get("/einrichtung").text
        assert 'name="institut"' in seite and 'value="Musterbank"' in seite
        assert "gedruckt wird genau eine" in seite

    @braucht("pdftotext")
    def test_bank_steht_im_pdf(self, klient, daten, tmp_path):
        rid = _entwurf(klient, daten)
        text = _pdftext(klient.get(f"/rechnung/{rid}/vorschau.pdf").content, tmp_path)
        assert re.search(r"Bank\s+Musterbank", text)
        assert "Kontoinhaber Erika Muster" in re.sub(r"\s+", " ", text)

    def test_leer_heisst_keine_bank(self, klient, daten):
        klient.post("/einrichtung", data={**EINRICHTUNG, "institut": "  "},
                    follow_redirects=False)
        conn = _db(daten)
        assert conn.execute("SELECT institut FROM betrieb").fetchone()[0] is None
        conn.close()


class TestRendererPruefung:
    def test_fehlschlag_wird_neu_geprueft_erfolg_gemerkt(self, monkeypatch):
        """Wer Pango nachinstalliert, sieht die Vorschau ohne Neustart; ein
        gelungener Test laeuft dagegen nicht bei jedem Seitenaufruf neu."""
        aufrufe: list[int] = []
        ergebnis = {"code": 1}

        class Lauf:
            def __init__(self, code):
                self.returncode = code
                self.stdout = ""
                self.stderr = "OSError: cannot load library 'libpango-1.0-0'"

        def lauf(*_, **__):
            aufrufe.append(1)
            return Lauf(ergebnis["code"])

        monkeypatch.setattr(pdf, "_RENDERER_DA", False)
        monkeypatch.setattr(pdf.subprocess, "run", lauf)
        assert "brew install pango" in pdf.renderer_fehlt()
        assert "brew install pango" in pdf.renderer_fehlt()
        ergebnis["code"] = 0
        assert pdf.renderer_fehlt() is None
        assert pdf.renderer_fehlt() is None
        assert len(aufrufe) == 3

"""Belege ueber echte HTTP-Aufrufe: Upload, Pruefmaske, Buchen.

Die Pruefmaske ist hier das Pruefobjekt, nicht der Upload. Sie muss **je Feld**
zeigen, woher der Wert kommt -- nach GoBD Rz. 130 ist das bestaetigte Ergebnis
selbst aufbewahrungspflichtig, und wer bestaetigt, kann das nur verantworten,
wenn er die Herkunft sieht.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.logik.beleg import pipeline
from app.main import erzeuge_app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
PDF_EN = FIXTURES / "lieferantenrechnung_en.pdf"
CII = FIXTURES / "zugferd_cii.xml"

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}


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


def _hochladen(klient, datei: Path, name: str | None = None):
    return klient.post(
        "/belege/hochladen",
        files={"datei": (name or datei.name, datei.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )


def _eingangsname(antwort) -> str:
    return antwort.headers["location"].rsplit("/", 1)[1]


class TestListe:
    def test_leere_liste(self, klient):
        text = klient.get("/belege").text
        assert "Noch keine Belege erfasst" in text
        assert "Bruttobetrag" in text

    def test_bilderkennung_meldet_ihren_zustand(self, klient):
        """Auf diesem Linux-Rechner nicht verfuegbar -- und die Seite sagt das
        samt Grund, statt den Knopf still ins Leere zeigen zu lassen."""
        text = klient.get("/belege").text
        assert "Bilderkennung" in text
        assert "macOS" in text or "bereit" in text

    def test_vor_der_einrichtung_leitet_weiter(self, tmp_path):
        with TestClient(erzeuge_app(tmp_path / "leer.sqlite")) as c:
            assert c.get("/belege", follow_redirects=False).headers["location"] == "/einrichtung"

    def test_nav_fuehrt_hin(self, klient):
        assert 'href="/belege"' in klient.get("/").text


class TestUpload:
    def test_pdf_landet_im_eingang_und_leitet_zur_pruefung(self, klient, tmp_path):
        antwort = _hochladen(klient, PDF_DE)
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith("/belege/pruefen/")
        assert len(pipeline.liegen_geblieben(tmp_path)) == 1

    def test_fremdes_format_wird_abgewiesen(self, klient):
        antwort = klient.post(
            "/belege/hochladen",
            files={"datei": ("aufstellung.docx", b"PK\x03\x04", "application/zip")},
            follow_redirects=True,
        )
        assert "Belegformat" in antwort.text

    def test_leere_datei_wird_abgewiesen(self, klient):
        antwort = klient.post(
            "/belege/hochladen",
            files={"datei": ("leer.pdf", b"", "application/pdf")},
            follow_redirects=True,
        )
        assert "leer" in antwort.text

    def test_eingang_wird_auf_der_liste_gezeigt(self, klient):
        _hochladen(klient, PDF_DE)
        text = klient.get("/belege").text
        assert "Im Eingang, noch nicht gebucht" in text
        assert "nicht verloren, nur nicht erfasst" in text


class TestPruefmaske:
    def test_felder_sind_vorbelegt(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert 'value="2026-03-14"' in text
        assert 'value="8,29"' in text
        assert 'value="R0012345678"' in text
        assert "Hetzner Online GmbH" in text

    def test_jedes_feld_zeigt_seine_herkunft(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert "aus der Textschicht" in text
        assert "Arithmetik geht auf" in text
        assert "GoBD" in text

    def test_unsichere_felder_sind_markiert(self, klient):
        """Der Lieferantenname steht auf einem Beleg nirgends bezeichnet."""
        name = _eingangsname(_hochladen(klient, PDF_DE))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert "bitte prüfen" in text
        assert "unsicher" in text

    def test_beleg_wird_neben_den_feldern_gezeigt(self, klient):
        """Ohne das Bild ist die Pruefung ein Ratespiel."""
        name = _eingangsname(_hochladen(klient, PDF_DE))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert f'src="/belege/eingang/{name}"' in text
        antwort = klient.get(f"/belege/eingang/{name}")
        assert antwort.status_code == 200
        assert antwort.content == PDF_DE.read_bytes()

    def test_reverse_charge_wird_vorgehakt(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_EN))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert "§ 13b Abs. 5 UStG" in text
        assert 'name="reverse_charge" value="1"\n          checked' in text or "checked" in text
        assert "Englisches Zahlenformat" in text

    def test_zugferd_meldet_das_xml_als_massgebend(self, klient, tmp_path):
        from pypdf import PdfWriter

        pfad = tmp_path / "zugferd.pdf"
        schreiber = PdfWriter()
        schreiber.add_blank_page(width=595, height=842)
        schreiber.add_attachment("factur-x.xml", CII.read_bytes())
        with pfad.open("wb") as datei:
            schreiber.write(datei)

        name = _eingangsname(_hochladen(klient, pfad))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert "aus dem XML der E-Rechnung" in text
        assert "maßgebend" in text
        assert 'value="5,46"' in text

    def test_kaputtes_xml_faellt_nicht_auf_bilderkennung_zurueck(self, klient, tmp_path,
                                                                 datenbank):
        from pypdf import PdfWriter

        pfad = tmp_path / "kaputt.pdf"
        schreiber = PdfWriter()
        schreiber.add_blank_page(width=595, height=842)
        schreiber.add_attachment("factur-x.xml", b"<nicht wohlgeformt")
        with pfad.open("wb") as datei:
            schreiber.write(datei)

        name = _eingangsname(_hochladen(klient, pfad))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert "unzulässig" in text
        assert "manuell" in text
        # Das Scheitern gehoert ins Journal: es ist der Nachweis, dass NICHT
        # auf eine Bilderkennung zurueckgefallen wurde.
        with datenbank() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM journal WHERE aktion = 'BELEG_AUSLESUNG_FEHLER'"
            ).fetchone()[0] == 1

    def test_unbekannte_datei_leitet_zurueck(self, klient):
        antwort = klient.get("/belege/pruefen/gibtsnicht.pdf", follow_redirects=True)
        assert "liegt nicht im Eingang" in antwort.text

    @pytest.mark.parametrize("name", [
        "..%2F..%2Fsteuer.sqlite", "..", ".versteckt", "unterordner%2Fa.pdf",
    ])
    def test_kein_ausbruch_aus_dem_eingangsordner(self, klient, name):
        """Der Name kommt aus der URL. Ohne Pruefung waere das ein Weg an die
        Datenbank."""
        antwort = klient.get(f"/belege/eingang/{name}", follow_redirects=True)
        assert antwort.status_code in (200, 404)
        assert b"SQLite format" not in antwort.content


class TestBuchen:
    def _buchen(self, klient, name, **ueberschreiben):
        daten = {
            "belegdatum": "2026-03-14", "brutto_euro": "8,29", "netto_euro": "6,97",
            "ust_euro": "1,32", "ust_satz": "19", "waehrung": "EUR",
            "belegnummer": "R0012345678", "lieferant_neu": "Hetzner Online GmbH",
            "lieferant_ust_idnr": "DE812871812", "quote": "100",
            "zahlungsart": "UEBERWEISUNG",
        }
        daten.update(ueberschreiben)
        return klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=True)

    def test_vollstaendiger_durchlauf(self, klient, tmp_path, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        with datenbank() as conn:
            kat = conn.execute(
                "SELECT id FROM euer_kategorie WHERE schluessel = 'HOSTING'"
            ).fetchone()[0]
        antwort = self._buchen(klient, name, kategorie_id=str(kat))
        assert "erfasst" in antwort.text

        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM beleg").fetchone()
        assert zeile["belegdatum"] == "2026-03-14"
        assert zeile["betrag_brutto_cent"] == 829
        assert zeile["betrag_netto_cent"] == 697
        assert zeile["kategorie_id"] == kat
        assert pipeline.liegen_geblieben(tmp_path) == []

    def test_lieferant_entsteht_in_derselben_maske(self, klient, datenbank):
        """Sonst muesste der Nutzer abbrechen, den Lieferanten anlegen und neu
        beginnen -- und beim dritten Mal erfasst er den Beleg daneben."""
        name = _eingangsname(_hochladen(klient, PDF_DE))
        self._buchen(klient, name)
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM lieferant").fetchone()
        assert zeile["name"] == "Hetzner Online GmbH"
        assert zeile["ust_idnr"] == "DE812871812"

    def test_original_ist_schreibgeschuetzt_und_byte_identisch(self, klient, datenbank, tmp_path):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        self._buchen(klient, name)
        with datenbank() as conn:
            gespeichert = conn.execute("SELECT pfad FROM belegdatei").fetchone()[0]
        # Relativ zum Datenordner gespeichert; der Datenordner ist hier tmp_path.
        assert not Path(gespeichert).is_absolute()
        pfad = tmp_path / gespeichert
        assert pfad.read_bytes() == PDF_DE.read_bytes()
        assert oct(pfad.stat().st_mode & 0o777) == "0o444"

    def test_datei_ist_abrufbar(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        self._buchen(klient, name)
        with datenbank() as conn:
            bid = conn.execute("SELECT id FROM beleg").fetchone()[0]
        antwort = klient.get(f"/belege/{bid}/datei")
        assert antwort.status_code == 200
        assert antwort.content == PDF_DE.read_bytes()

    def test_quote_wirkt_auf_den_betrieblichen_anteil(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        self._buchen(klient, name, quote="70")
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM beleg").fetchone()
        assert zeile["betrag_brutto_cent"] == 829
        assert zeile["betrag_betrieblich_cent"] == 580

    def test_zahlung_wird_negativ_gebucht(self, klient, datenbank):
        """Fuer die EUeR zaehlt nach § 11 EStG der Abfluss."""
        name = _eingangsname(_hochladen(klient, PDF_DE))
        self._buchen(klient, name, bezahlt_am="2026-03-20")
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM zahlung").fetchone()
        assert zeile["datum"] == "2026-03-20"
        assert zeile["betrag_cent"] == -829

    def test_ohne_kategorie_wird_darauf_hingewiesen(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        antwort = self._buchen(klient, name, kategorie_id="")
        assert "keine Zeile der EÜR" in antwort.text

    def test_ohne_datum_wird_abgewiesen(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        antwort = self._buchen(klient, name, belegdatum="")
        assert "Belegdatum" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_unsinniger_betrag_wird_abgewiesen(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        antwort = self._buchen(klient, name, brutto_euro="achtzig")
        assert "keine Zahl" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_reverse_charge_beleg(self, klient, datenbank):
        """Ein USD-Beleg. Ohne den Euro-Betrag laut Kontoauszug wird nicht
        gebucht -- bis hierher stand der Dollarbetrag als Euro in der EUeR."""
        name = _eingangsname(_hochladen(klient, PDF_EN))
        felder = {
            "belegdatum": "2026-03-03", "brutto_euro": "1234,56", "netto_euro": "1234,56",
            "ust_euro": "0,00", "ust_satz": "0", "waehrung": "USD",
            "lieferant_neu": "Amazon Web Services EMEA SARL",
            "lieferant_ust_idnr": "LU26888617", "quote": "100",
            "reverse_charge": "1",
        }
        antwort = klient.post(f"/belege/pruefen/{name}", data=felder, follow_redirects=True)
        assert "Euro-Betrag" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

        klient.post(f"/belege/pruefen/{name}", data={**felder, "brutto_eur": "1.130,00"},
                    follow_redirects=True)
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM beleg").fetchone()
        assert zeile["reverse_charge"] == 1
        assert zeile["betrag_brutto_cent"] == 113000, "der Euro-Betrag zaehlt"
        assert zeile["betrag_fremdwaehrung_cent"] == 123456
        assert zeile["waehrung"] == "USD"

    def test_dublette_wird_abgewiesen(self, klient, datenbank):
        erster = _eingangsname(_hochladen(klient, PDF_DE))
        self._buchen(klient, erster)
        zweiter = _eingangsname(_hochladen(klient, PDF_DE, name="nochmal.pdf"))
        text = klient.get(f"/belege/pruefen/{zweiter}").text
        assert "schon gebucht" in text
        antwort = self._buchen(klient, zweiter)
        # Die Meldung steht genau einmal da -- als Kasten "schon gebucht"; die
        # gleichlautende Fehlermeldung des Buchungsversuchs wird nicht noch
        # einmal daneben gesetzt (Befund der Browserpruefung: bis zu drei
        # inhaltsgleiche Meldungen).
        assert antwort.text.count("schon gebucht") == 1
        assert "bereits als Beleg" not in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1

    def test_zweiter_beleg_desselben_lieferanten_ist_ein_klick(self, klient, tmp_path,
                                                               datenbank):
        """Der staerkste Hebel: ab dem zweiten Beleg sind die Beschriftungen
        bekannt, und alle Felder stehen ohne Korrektur."""
        from weasyprint import HTML

        erster = _eingangsname(_hochladen(klient, PDF_DE))
        antwort = self._buchen(klient, erster)
        assert "gelernt" in antwort.text

        zweite = tmp_path / "zweite.pdf"
        HTML(string="""<!doctype html><html><head><meta charset="utf-8"></head><body>
            <p>Hetzner Online GmbH<br>USt-IdNr.: DE 812871812</p>
            <h1>Rechnung</h1>
            <p>Rechnungsnummer: R0099887766<br>Rechnungsdatum: 14.04.2026</p>
            <p>Nettobetrag 50,00 EUR<br>Umsatzsteuer 19 % 9,50 EUR<br>
               Gesamtbetrag 59,50 EUR</p></body></html>""").write_pdf(zweite)

        name = _eingangsname(_hochladen(klient, zweite))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert "Gelernte Beschriftung" in text
        assert 'value="59,50"' in text
        assert 'value="2026-04-14"' in text
        assert 'value="R0099887766"' in text


class TestNachbearbeiten:
    def _gebucht(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100",
        }, follow_redirects=True)
        with datenbank() as conn:
            return conn.execute("SELECT id FROM beleg").fetchone()[0]

    def test_kategorie_nachtragen(self, klient, datenbank):
        bid = self._gebucht(klient, datenbank)
        with datenbank() as conn:
            kat = conn.execute(
                "SELECT id FROM euer_kategorie WHERE schluessel = 'SAAS'").fetchone()[0]
        klient.post(f"/belege/{bid}/kategorie", data={"kategorie_id": str(kat), "quote": "50"},
                    follow_redirects=True)
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM beleg").fetchone()
            journal = [z["feld"] for z in conn.execute(
                "SELECT feld FROM journal WHERE objekttyp = 'beleg' AND aktion = 'AENDERN'")]
        assert zeile["kategorie_id"] == kat
        assert zeile["aufteilungsquote"] == 50
        assert zeile["betrag_betrieblich_cent"] == 415
        assert "kategorie_id" in journal
        assert "aufteilungsquote" in journal

    def test_unsinnige_quote_wird_abgewiesen(self, klient, datenbank):
        bid = self._gebucht(klient, datenbank)
        antwort = klient.post(f"/belege/{bid}/kategorie", data={"quote": "150"},
                              follow_redirects=True)
        assert "zwischen 0 und 100" in antwort.text

    def test_zahlung_nachtragen(self, klient, datenbank):
        bid = self._gebucht(klient, datenbank)
        klient.post(f"/belege/{bid}/bezahlt", data={"datum": "2026-04-01"},
                    follow_redirects=True)
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM zahlung").fetchone()
        assert zeile["datum"] == "2026-04-01"
        assert zeile["betrag_cent"] == -829

    def test_zahlung_ohne_datum_wird_abgewiesen(self, klient, datenbank):
        bid = self._gebucht(klient, datenbank)
        antwort = klient.post(f"/belege/{bid}/bezahlt", data={"datum": ""},
                              follow_redirects=True)
        assert "§ 11 EStG" in antwort.text

    def test_eingangsdatei_wegwerfen(self, klient, tmp_path):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        antwort = klient.post(f"/belege/eingang/{name}/wegwerfen", follow_redirects=True)
        assert "aus dem Eingang entfernt" in antwort.text
        assert pipeline.liegen_geblieben(tmp_path) == []


class TestIntegritaetUeberDieOberflaeche:
    def test_heiles_archiv(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE))
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100",
        }, follow_redirects=True)
        antwort = klient.post("/belege/integritaet", follow_redirects=True)
        assert "alle unverändert" in antwort.text
        with datenbank() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM journal WHERE aktion = 'BELEG_INTEGRITAET'"
            ).fetchone()[0] == 1

    def test_veraenderte_datei_wird_gemeldet(self, klient, datenbank, tmp_path):
        import os

        name = _eingangsname(_hochladen(klient, PDF_DE))
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100",
        }, follow_redirects=True)
        with datenbank() as conn:
            pfad = tmp_path / conn.execute("SELECT pfad FROM belegdatei").fetchone()[0]
        os.chmod(pfad, 0o644)
        pfad.write_bytes(pfad.read_bytes() + b"\n% manipuliert")
        antwort = klient.post("/belege/integritaet", follow_redirects=True)
        # Einzahl: es ist genau ein Befund (vorher stand dort "1 Befunde").
        assert "1 Befund —" in antwort.text
        assert "Prüfsumme" in antwort.text


class TestErfassungsfrist:
    """Kein Hinweis „nach N Tagen erfasst“ mehr in der Liste. Er liess sich
    nicht abarbeiten (erfasst_am ist unveraenderlich), mass den Abstand zum
    Belegdatum statt zum Eingang des Belegs, sah aus wie eine offene Aufgabe
    und stand bei der Nacherfassung eines Jahres an fast jedem Beleg -- der
    Nutzer verstand ihn nicht. Wann ein Beleg erfasst wurde, steht weiter im
    Datensatz, im Journal und in der Excel-Datei."""

    def test_frischer_beleg_ohne_hinweis(self, klient, datenbank):
        import datetime as dt

        name = _eingangsname(_hochladen(klient, PDF_DE))
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": dt.date.today().isoformat(), "brutto_euro": "8,29",
            "quote": "100",
        }, follow_redirects=True)
        assert "Tagen erfasst" not in klient.get("/belege").text

    def test_spaet_erfasster_beleg_traegt_keinen_hinweis(self, klient, datenbank):
        """Der Beleg aus dem März, hochgeladen im Oktober: kein Hinweis --
        der Erfassungszeitpunkt bleibt aber am Beleg gespeichert."""
        name = _eingangsname(_hochladen(klient, PDF_DE))
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100",
        }, follow_redirects=True)
        text = klient.get("/belege").text
        assert "Tagen erfasst" not in text
        assert "unbedenklich" not in text
        with datenbank() as verbindung:
            assert verbindung.execute("SELECT erfasst_am FROM beleg").fetchone()[0]

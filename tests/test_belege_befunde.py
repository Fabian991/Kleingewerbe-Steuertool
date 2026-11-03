"""Regressionstests zu den Befunden der Fehleranalyse im Bereich Eingangsbelege.

Je Befund mindestens ein Test. Die Tests laufen gegen die echten Routen
(TestClient), weil die meisten Befunde erst im Zusammenspiel von Pipeline,
Route und Vorlage sichtbar wurden -- etwa die Stufen-Chips, die eine
Bilderkennung behaupteten, die nie gelaufen war.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.logik.beleg import pipeline as p
from app.logik.beleg import profil
from app.logik.beleg.ergebnis import BelegFehler
from app.main import erzeuge_app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
PDF_EN = FIXTURES / "lieferantenrechnung_en.pdf"
CII = FIXTURES / "zugferd_cii.xml"
UBL = FIXTURES / "xrechnung_ubl.xml"

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}

PFLICHT = {"belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100"}


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


@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


def _hochladen(klient, inhalt: bytes, name: str, mime: str = "application/pdf"):
    return klient.post("/belege/hochladen", files={"datei": (name, inhalt, mime)},
                       follow_redirects=False)


def _eingangsname(antwort) -> str:
    return antwort.headers["location"].rsplit("/", 1)[1]


def _fehler(antwort) -> str:
    from urllib.parse import parse_qs, urlparse

    frage = parse_qs(urlparse(antwort.headers["location"]).query)
    return (frage.get("fehler") or frage.get("erfolg") or [""])[0]


def _kategorie(datenbank, schluessel: str) -> int:
    with datenbank() as conn:
        return conn.execute(
            "SELECT id FROM euer_kategorie WHERE schluessel = ?", (schluessel,)
        ).fetchone()[0]


# ------------------------------------------------------------------- Upload

class TestUploadInhalt:
    """Die Endung behauptet nur, was die Datei ist; die ersten Bytes zeigen es."""

    def test_programm_mit_pdf_endung_wird_nicht_aufgenommen(self, klient, tmp_path):
        antwort = _hochladen(klient, b"MZ\x90\x00\x03" + b"\x00" * 200, "programm.pdf")
        assert antwort.status_code == 303
        assert "kein PDF" in _fehler(antwort)
        assert p.liegen_geblieben(tmp_path) == []

    @pytest.mark.parametrize("inhalt, name", [
        (b"%PDF-1.7\n%...", "rechnung.pdf"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "foto.png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 20, "foto.jpg"),
        (b"II*\x00" + b"\x00" * 20, "scan.tif"),
        (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, "foto.heic"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "foto.webp"),
        (b"\xef\xbb\xbf\n  <?xml version='1.0'?><CrossIndustryInvoice/>", "e.xml"),
    ])
    def test_passender_inhalt_wird_aufgenommen(self, datenordner, inhalt, name):
        assert p.aufnehmen(inhalt, name, datenordner).pfad.exists()

    @pytest.mark.parametrize("inhalt, name", [
        (b"%PDF-1.7 aber png", "foto.png"),
        (b"\xff\xd8\xff als pdf", "rechnung.pdf"),
        (b"kein xml, nur text", "e.xml"),
        (b"RIFF\x00\x00\x00\x00AVI ", "foto.webp"),
    ])
    def test_fremder_inhalt_wird_abgewiesen(self, datenordner, inhalt, name):
        with pytest.raises(BelegFehler, match="Endung passt nicht zum Inhalt"):
            p.aufnehmen(inhalt, name, datenordner)
        assert p.liegen_geblieben(datenordner) == []

    def test_dateiname_wird_vor_der_endungspruefung_getrimmt(self, datenordner):
        eingang = p.aufnehmen(b"%PDF-1.7 x", "rechnung.pdf ", datenordner)
        assert eingang.original_name == "rechnung.pdf"
        assert eingang.pfad.suffix == ".pdf"

    def test_groessenmeldung_widerspricht_sich_nicht(self, datenordner):
        with pytest.raises(BelegFehler) as fehler:
            p.aufnehmen(b"%PDF" + b"\0" * (p.MAX_BYTES - 3), "riesig.pdf", datenordner)
        assert "ist größer als 50 MB" in str(fehler.value)
        assert "ist 50 MB groß" not in str(fehler.value)


def _pdf(tmp_path: Path, name: str, html: str) -> Path:
    """Ein PDF mit Textschicht, erzeugt wie im Lernspeicher-Test."""
    from weasyprint import HTML

    ziel = tmp_path / name
    HTML(string=f'<!doctype html><html><head><meta charset="utf-8"></head><body>{html}'
                "</body></html>").write_pdf(ziel)
    return ziel


def _buchen(klient, name: str, **felder):
    daten = {**PFLICHT, **felder}
    return klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)


def _anzahl(datenbank, tabelle: str = "beleg") -> int:
    with datenbank() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]


HETZNER = {
    "belegdatum": "2026-03-14", "brutto_euro": "8,29", "netto_euro": "6,97",
    "ust_euro": "1,32", "ust_satz": "19", "belegnummer": "R0012345678",
    "lieferant_neu": "Hetzner Online GmbH", "lieferant_ust_idnr": "DE812871812",
    "quote": "100",
}


# ---------------------------------------------------------------- Gutschrift

class TestGutschrift:
    """Eine Gutschrift ist keine Ausgabe; als Ausgabe gebucht verfaelschte sie
    die EUeR um den doppelten Betrag."""

    def _hochladen(self, klient):
        gut = CII.read_text(encoding="utf-8").replace(
            "<ram:TypeCode>380</ram:TypeCode>", "<ram:TypeCode>381</ram:TypeCode>", 1)
        return _eingangsname(_hochladen(klient, gut.encode(), "gut.xml", "application/xml"))

    def test_maske_zeigt_die_gutschrift_und_kein_buchungsformular(self, klient):
        name = self._hochladen(klient)
        text = klient.get(f"/belege/pruefen/{name}").text
        assert "Das ist eine Gutschrift, keine Rechnung." in text
        assert "Beleg buchen" not in text
        assert "Datei aus dem Eingang entfernen" in text

    def test_buchen_als_ausgabe_wird_abgewiesen(self, klient, datenbank):
        name = self._hochladen(klient)
        antwort = _buchen(klient, name, brutto_euro="5,46")
        assert antwort.status_code == 422
        assert "Eine Gutschrift ist keine Ausgabe" in antwort.text
        assert "dort als Erstattung erfassen" in antwort.text
        assert _anzahl(datenbank) == 0


# ------------------------------------------------------- inhaltliche Dublette

class TestInhaltlicheDublette:
    def _zweites_pdf(self, tmp_path):
        return _pdf(tmp_path, "kopie.pdf", """
            <p>Hetzner Online GmbH<br>USt-IdNr.: DE 812871812</p><h1>Rechnung (Kopie)</h1>
            <p>Rechnungsnummer: R0012345678<br>Rechnungsdatum: 14.03.2026</p>
            <p>Nettobetrag 6,97 EUR<br>Umsatzsteuer 19 % 1,32 EUR<br>
               Gesamtbetrag 8,29 EUR</p>""")

    def test_warnung_in_der_maske_und_buchen_nur_mit_bestaetigung(
        self, klient, datenbank, tmp_path
    ):
        erster = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "de.pdf"))
        assert _buchen(klient, erster, **HETZNER).status_code == 303
        zweiter = _eingangsname(_hochladen(
            klient, self._zweites_pdf(tmp_path).read_bytes(), "kopie.pdf"))

        text = klient.get(f"/belege/pruefen/{zweiter}").text
        assert "Zu Lieferant Hetzner Online GmbH gibt es bereits Beleg 1" in text
        assert "trotzdem buchen — es ist ein anderer Beleg" in text

        daten = {**HETZNER, "lieferant_id": "1"}
        abgewiesen = _buchen(klient, zweiter, **daten)
        assert abgewiesen.status_code == 422
        assert "gibt es bereits Beleg 1" in abgewiesen.text
        assert _anzahl(datenbank) == 1

        assert _buchen(klient, zweiter, **daten, trotzdem="1").status_code == 303
        assert _anzahl(datenbank) == 2

    def test_gleiches_datum_und_betrag_ohne_nummer(self, klient, datenbank):
        with datenbank() as conn:
            conn.execute("INSERT INTO lieferant (name, angelegt_am) VALUES ('X', '2026-01-01')")
            conn.execute(
                "INSERT INTO beleg (lieferant_id, belegdatum, betrag_brutto_cent,"
                " betrag_betrieblich_cent, erfasst_am) VALUES (1, '2026-03-14', 829, 829,"
                " '2026-03-14')")
            treffer = p.aehnliche_belege(conn, 1, None, "2026-03-14", 829)
            assert [z["id"] for z in treffer] == [1]
            assert p.aehnliche_belege(conn, 1, None, "2026-03-14", 830) == []


# ---------------------------------------------------------- Lieferantenprofil

TEXT_EINE_POSITION = """Hetzner Online GmbH
Rechnungsnummer: R0012345678
Rechnungsdatum: 14.03.2026
Cloud Server CX22 6,97 EUR
Nettobetrag 6,97 EUR
Umsatzsteuer 19 % 1,32 EUR
Gesamtbetrag 8,29 EUR"""

TEXT_ZWEI_POSITIONEN = """Hetzner Online GmbH
Rechnungsnummer: R0012345679
Rechnungsdatum: 14.04.2026
Cloud Server CX22 4,00 EUR
Storage Box 2,00 EUR
Nettobetrag 6,00 EUR
Umsatzsteuer 19 % 1,14 EUR
Gesamtbetrag 7,14 EUR"""

TEXT_OHNE_POSITION = TEXT_ZWEI_POSITIONEN.replace("Cloud Server CX22 4,00 EUR\n", "")


class TestLieferantenprofil:
    @pytest.fixture
    def conn(self, klient, datenbank):
        with datenbank() as verbindung:
            verbindung.execute(
                "INSERT INTO lieferant (name, angelegt_am) VALUES ('Hetzner', '2026-01-01')")
            yield verbindung

    def _lerne(self, conn):
        return profil.lerne(conn, 1, TEXT_EINE_POSITION, {
            "belegnummer": "R0012345678", "belegdatum": "2026-03-14", "brutto": 829,
            "netto": 697, "ust": 132, "ust_satz_promille": 190, "waehrung": "EUR",
        })

    def test_netto_wird_an_der_beschriftung_gelernt_nicht_an_der_position(self, conn):
        self._lerne(conn)
        muster = {z["feld"]: z["muster"] for z in conn.execute(
            "SELECT feld, muster FROM lieferant_profil")}
        assert muster["netto"] == "Nettobetrag"
        assert muster["ust"] == "Umsatzsteuer"
        assert "waehrung" not in muster, "die Waehrung haengt an keiner Zeile"

    def test_steuersatz_wird_gelernt(self, conn):
        gelernt = self._lerne(conn)
        assert "ust_satz" in gelernt
        vorschlag = profil.anwenden(conn, 1, TEXT_ZWEI_POSITIONEN, quelle="pdf_text")
        assert vorschlag.wert("ust_satz_promille") == 190

    def test_zwei_positionen_ergeben_den_richtigen_nettobetrag(self, conn):
        self._lerne(conn)
        vorschlag = profil.anwenden(conn, 1, TEXT_ZWEI_POSITIONEN, quelle="pdf_text")
        assert vorschlag.wert("netto") == 600
        assert vorschlag.wert("brutto") == 714

    def test_kein_fehlalarm_wenn_nur_die_positionszeile_fehlt(self, conn):
        self._lerne(conn)
        vorschlag = profil.anwenden(conn, 1, TEXT_OHNE_POSITION, quelle="pdf_text")
        assert not any("Layout" in m for m in vorschlag.meldungen)

    def test_alte_muster_aus_der_positionszeile_werden_nicht_angewandt(self, conn):
        """Profile aus der Zeit vor der Korrektur: Netto und Waehrung an der
        Positionszeile gelernt."""
        self._lerne(conn)
        for feld in ("netto", "waehrung"):
            conn.execute(
                "INSERT INTO lieferant_profil (lieferant_id, feld, muster, kontext,"
                " zahlenformat, treffer, gelernt_am, bestaetigt_am)"
                " VALUES (1, ?, 'Cloud Server CX', '', 'DE', 5, '', '')", (feld,))
        vorschlag = profil.anwenden(conn, 1, TEXT_OHNE_POSITION, quelle="pdf_text")
        assert vorschlag.wert("netto") == 600
        assert "waehrung" not in vorschlag.felder
        assert not any("Layout" in m for m in vorschlag.meldungen)

    def test_erfolgsmeldung_nennt_die_felder_mit_umlauten(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "de.pdf"))
        meldung = _fehler(_buchen(klient, name, **HETZNER))
        assert "Für diesen Lieferanten gelernt:" in meldung
        assert "Steuersatz" in meldung
        assert "Bestätigungsklick" in meldung


# -------------------------------------------------------------- Originalname

class TestOriginalname:
    def test_wird_gespeichert_und_beim_abruf_benutzt(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(),
                                        "Rechnung März 2026 Nr. 7.pdf"))
        assert _buchen(klient, name, belegdatum="2026-05-02",
                       brutto_euro="50,00").status_code == 303
        with datenbank() as conn:
            assert conn.execute("SELECT original_name FROM belegdatei").fetchone()[0] == (
                "Rechnung März 2026 Nr. 7.pdf")
        kopf = klient.get("/belege/1/datei").headers["content-disposition"]
        assert "filename*=utf-8''Rechnung%20M%C3%A4rz%202026%20Nr.%207.pdf" in kopf

    def test_begleitdatei_ist_nach_dem_buchen_weg(self, klient, tmp_path):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "x.pdf"))
        _buchen(klient, name)
        assert list((tmp_path / p.EINGANG).iterdir()) == []

    def test_wegwerfen_nennt_den_originalnamen(self, klient, tmp_path):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "Strom April.pdf"))
        antwort = klient.post(f"/belege/eingang/{name}/wegwerfen", follow_redirects=False)
        assert _fehler(antwort) == "Strom April.pdf aus dem Eingang entfernt."
        assert list((tmp_path / p.EINGANG).iterdir()) == []


# ------------------------------------------------------------ Reverse Charge

class TestReverseCharge:
    def test_steuer_ist_null_und_sicher(self):
        from app.logik.beleg import felder as fe

        text = ("Steuerschuldnerschaft des Leistungsempfängers\n"
                "Zwischensumme 240,00 EUR\nGesamtbetrag 240,00 EUR")
        urteil = fe.bewerte_betraege(text, reverse_charge=True)
        assert urteil.ust is not None and urteil.ust.cent == 0
        assert urteil.netto.cent == 24000

    def test_maske_belegt_die_steuer_mit_null_vor(self, klient, tmp_path):
        datei = _pdf(tmp_path, "rc.pdf", """
            <p>Muster GmbH<br>Wien<br>UID: ATU12345678</p><h1>Rechnung</h1>
            <p>Rechnungsnummer: AT-2026-77<br>Rechnungsdatum: 10.06.2026</p>
            <p>Steuerschuldnerschaft des Leistungsempfängers</p>
            <p>Zwischensumme 240,00 EUR<br>Gesamtbetrag 240,00 EUR</p>""")
        name = _eingangsname(_hochladen(klient, datei.read_bytes(), "rc.pdf"))
        text = klient.get(f"/belege/pruefen/{name}").text
        feld = re.search(r'<input type="text" name="ust_euro"[^>]*>', text).group(0)
        assert 'value="0,00"' in feld
        assert "unsicher" not in feld

    def test_leere_steuer_speichert_netto_gleich_brutto(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "rc.pdf"))
        _buchen(klient, name, brutto_euro="240,00", netto_euro="240,00", ust_euro="",
                ust_satz="0", reverse_charge="1")
        with datenbank() as conn:
            zeile = conn.execute("SELECT betrag_netto_cent, betrag_ust_cent,"
                                 " ust_satz_promille, reverse_charge FROM beleg").fetchone()
        assert tuple(zeile) == (24000, 0, 0, 1)

    def test_ausgewiesene_steuer_schliesst_reverse_charge_aus(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "rc.pdf"))
        antwort = _buchen(klient, name, netto_euro="6,97", ust_euro="1,32", ust_satz="19",
                          reverse_charge="1")
        assert antwort.status_code == 422
        assert "Reverse Charge und ausgewiesene Umsatzsteuer schließen sich aus." in antwort.text
        assert _anzahl(datenbank) == 0


# --------------------------------------------------------------------- Datum

class TestDaten:
    def _zukunft(self) -> str:
        return (dt.date.today() + dt.timedelta(days=40)).isoformat()

    @pytest.mark.parametrize("wert", ["gestern", "2026-13-45"])
    def test_zahlungsdatum_muss_ein_datum_sein(self, klient, datenbank, wert):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        antwort = _buchen(klient, name, bezahlt_am=wert)
        assert antwort.status_code == 422
        assert f"„{wert}“ ist kein Datum" in antwort.text
        assert _anzahl(datenbank) == 0
        assert f'name="bezahlt_am" value="{wert}"' in antwort.text, "Eingabe bleibt"

    def test_zahlungsdatum_in_der_zukunft(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        antwort = _buchen(klient, name, bezahlt_am=self._zukunft())
        assert "liegt in der Zukunft" in antwort.text
        assert _anzahl(datenbank) == 0

    def test_belegdatum_in_der_zukunft(self, klient, datenbank, tmp_path):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        antwort = _buchen(klient, name, belegdatum="2099-12-31")
        assert antwort.status_code == 422
        assert "Das Belegdatum 31.12.2099 liegt in der Zukunft" in antwort.text
        assert _anzahl(datenbank) == 0
        assert not (tmp_path / p.ARCHIV / "2099").exists()


# ------------------------------------------------------- Formularwerte, 500er

class TestFormularwerte:
    @pytest.mark.parametrize("feld, wert, meldung", [
        ("kategorie_id", "abc", "Diese Kategorie gibt es nicht."),
        ("kategorie_id", "9999", "Diese Kategorie gibt es nicht."),
        ("lieferant_id", "abc", "Diesen Lieferanten gibt es nicht."),
        ("reise_id", "abc", "Diese Reise gibt es nicht."),
        ("reise_id", "9999", "Diese Reise gibt es nicht."),
        ("quote", "abc", "ist keine Quote"),
    ])
    def test_buchen_meldet_statt_abzustuerzen(self, klient, datenbank, feld, wert, meldung):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        antwort = _buchen(klient, name, **{feld: wert})
        assert antwort.status_code == 422
        assert meldung in antwort.text
        assert _anzahl(datenbank) == 0

    def test_einnahmen_kategorie_wird_beim_buchen_abgewiesen(self, klient, datenbank):
        with datenbank() as conn:
            einnahme = conn.execute(
                "SELECT id FROM euer_kategorie WHERE bereich = 20 LIMIT 1").fetchone()[0]
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        antwort = _buchen(klient, name, kategorie_id=str(einnahme))
        assert "keine Ausgabenkategorie" in antwort.text
        assert _anzahl(datenbank) == 0

    @pytest.mark.parametrize("wert, meldung", [
        ("abc", "Diese Kategorie gibt es nicht."),
        ("9999", "Diese Kategorie gibt es nicht."),
        ("EINNAHME", "keine Ausgabenkategorie"),
    ])
    def test_kategorie_aendern_meldet_statt_abzustuerzen(self, klient, datenbank, wert,
                                                          meldung):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        _buchen(klient, name)
        if wert == "EINNAHME":
            with datenbank() as conn:
                wert = str(conn.execute(
                    "SELECT id FROM euer_kategorie WHERE bereich = 20 LIMIT 1").fetchone()[0])
        antwort = klient.post("/belege/1/kategorie", data={"kategorie_id": wert,
                                                           "quote": "70"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert meldung in _fehler(antwort)
        with datenbank() as conn:
            assert conn.execute("SELECT kategorie_id, aufteilungsquote FROM beleg"
                                ).fetchone()[:] == (None, 100)

    def test_eingaben_bleiben_nach_der_meldung_erhalten(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        antwort = _buchen(klient, name, kategorie_id="abc", quote="70", notiz="Serverkosten")
        assert 'name="quote" min="0" max="100" value="70"' in antwort.text
        assert ">Serverkosten</textarea>" in antwort.text

    @pytest.mark.parametrize("eingabe, gespeichert", [
        ("Euro", "EUR"), ("€", "EUR"), ("EUR ", "EUR"), ("usd", "USD"),
    ])
    def test_waehrung_wird_normalisiert(self, klient, datenbank, eingabe, gespeichert):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        _buchen(klient, name, waehrung=eingabe, brutto_eur="7,50")
        with datenbank() as conn:
            assert conn.execute("SELECT waehrung FROM beleg").fetchone()[0] == gespeichert

    def test_waehrung_als_freitext_wird_abgewiesen(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        antwort = _buchen(klient, name, waehrung="US-Dollar", brutto_eur="10,00")
        assert "„US-Dollar“ ist kein Währungscode" in antwort.text
        assert _anzahl(datenbank) == 0


# ------------------------------------------------------------- Eingangsdatei

class TestEingang:
    def test_null_byte_im_namen(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        for url in (f"/belege/eingang/{name}%00.txt", f"/belege/pruefen/{name}%00"):
            antwort = klient.get(url, follow_redirects=False)
            assert antwort.status_code == 303
            assert _fehler(antwort) == "Diese Datei liegt nicht im Eingang."
        antwort = klient.post(f"/belege/pruefen/{name}%00", data=PFLICHT,
                              follow_redirects=False)
        assert _fehler(antwort) == "Diese Datei liegt nicht im Eingang."

    def test_auslesefehler_nur_einmal_im_journal(self, klient, datenbank):
        kaputt = b"<?xml version='1.0'?><rsm:CrossIndustryInvoice xmlns:rsm='x'><ab"
        name = _eingangsname(_hochladen(klient, kaputt, "kaputt.xml", "application/xml"))
        for _ in range(3):
            assert klient.get(f"/belege/pruefen/{name}").status_code == 200
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion ="
                                " 'BELEG_AUSLESUNG_FEHLER'").fetchone()[0] == 1

    def test_stufen_chips_nennen_nur_was_geliefert_hat(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        chips = re.findall(r'<span class="chip">([^<]*)</span>',
                           klient.get(f"/belege/pruefen/{name}").text)
        assert "aus der Textschicht" in chips
        assert "aus dem XML der E-Rechnung" not in chips

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        name = _eingangsname(_hochladen(klient, png, "foto.png", "image/png"))
        chips = re.findall(r'<span class="chip">([^<]*)</span>',
                           klient.get(f"/belege/pruefen/{name}").text)
        assert [c for c in chips if c.startswith("aus ") or c == "manuell"] == ["manuell"]

    def test_links_sind_url_kodiert(self, klient, tmp_path):
        eingang = tmp_path / p.EINGANG
        eingang.mkdir(exist_ok=True)
        (eingang / "Rechnung März 2026 #1.pdf").write_bytes(PDF_DE.read_bytes())
        text = klient.get("/belege").text
        assert 'href="/belege/pruefen/Rechnung%20M%C3%A4rz%202026%20%231.pdf"' in text
        assert klient.get("/belege/pruefen/Rechnung%20M%C3%A4rz%202026%20%231.pdf"
                          ).status_code == 200

    def test_grosse_endung_wird_als_pdf_gezeigt(self, klient, tmp_path):
        eingang = tmp_path / p.EINGANG
        eingang.mkdir(exist_ok=True)
        (eingang / "GROSS.PDF").write_bytes(PDF_DE.read_bytes())
        text = klient.get("/belege/pruefen/GROSS.PDF").text
        assert '<iframe src="/belege/eingang/GROSS.PDF"' in text

    def test_fremde_endung_wird_gelistet_aber_nicht_gebucht(self, klient, tmp_path,
                                                            datenbank):
        eingang = tmp_path / p.EINGANG
        eingang.mkdir(exist_ok=True)
        (eingang / "aufstellung.docx").write_bytes(b"PK\x03\x04 kein Beleg")
        text = klient.get("/belege").text
        assert "aufstellung.docx" in text
        assert "kein Belegformat — wegwerfen oder umbenennen" in text
        assert 'href="/belege/pruefen/aufstellung.docx"' not in text
        antwort = klient.post("/belege/pruefen/aufstellung.docx", data=PFLICHT,
                              follow_redirects=False)
        assert "kein Belegformat" in _fehler(antwort)
        assert _anzahl(datenbank) == 0
        assert "kein Belegformat" in _fehler(
            klient.get("/belege/pruefen/aufstellung.docx", follow_redirects=False))


# ---------------------------------------------------------------- Belegliste

class TestBelegliste:
    def _einfuegen(self, conn, anzahl: int) -> None:
        conn.executemany(
            "INSERT INTO beleg (belegdatum, betrag_brutto_cent, betrag_betrieblich_cent,"
            " erfasst_am) VALUES ('2025-06-01', 100, 100, '2025-06-01')",
            [()] * anzahl)

    def test_summe_ueber_alle_gefilterten_belege(self, klient, datenbank):
        with datenbank() as conn:
            self._einfuegen(conn, 401)
            conn.execute(
                "INSERT INTO beleg (belegdatum, betrag_brutto_cent, betrag_betrieblich_cent,"
                " erfasst_am) VALUES ('2026-01-01', 123456, 123456, '2026-01-01')")
        text = klient.get("/belege").text
        summe = re.search(r'Betrieblich gesamt.*?<th class="zahl mono">([^<]*)</th>',
                          text, re.S).group(1)
        assert summe == "1.635,56 €"
        assert "Die Liste zeigt 400 von 402 Belegen" in text

        text = klient.get("/belege?jahr=2025").text
        summe = re.search(r'Betrieblich gesamt.*?<th class="zahl mono">([^<]*)</th>',
                          text, re.S).group(1)
        assert summe == "401,00 €"
        assert "Die Liste zeigt 400 von 401 Belegen" in text

    def test_ohne_kuerzung_kein_hinweis(self, klient, datenbank):
        with datenbank() as conn:
            self._einfuegen(conn, 3)
        assert "Die Liste zeigt" not in klient.get("/belege").text

    def test_fremdwaehrung_steht_neben_dem_eurobetrag(self, klient, datenbank):
        with datenbank() as conn:
            conn.execute(
                "INSERT INTO beleg (belegdatum, waehrung, betrag_fremdwaehrung_cent,"
                " betrag_brutto_cent, betrag_betrieblich_cent, erfasst_am)"
                " VALUES ('2026-03-03', 'USD', 123456, 113000, 113000, '2026-03-03')")
        zeile = re.search(r'<tr id="beleg-1">.*?</tr>', klient.get("/belege").text,
                          re.S).group(0)
        assert "1.130,00 €" in zeile
        # Ein Altbestand ohne Kurs (vor Version 19): der Euro-Betrag kam von
        # Hand laut Kontoauszug oder Kreditkarte -- und so steht er da.
        assert "1.234,56 USD · Euro-Betrag laut Kontoauszug oder Kreditkarte" in zeile


# ------------------------------------------------------------- Integritaet

class TestIntegritaet:
    def test_abweichende_rechte_werden_gemeldet_und_zurueckgesetzt(
        self, klient, datenbank, tmp_path
    ):
        """Nach einer von Hand entpackten Sicherung haben die Belegdateien
        normale Rechte. Das ist kein Befund (vorher: rote Meldung "1 Befunde"),
        sondern ein gruener Hinweis -- und die Rechte stehen danach auf 0444."""
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        _buchen(klient, name)
        with datenbank() as conn:
            pfad = tmp_path / conn.execute("SELECT pfad FROM belegdatei").fetchone()[0]
        os.chmod(pfad, 0o644)
        antwort = klient.post("/belege/integritaet", follow_redirects=False)
        assert "erfolg=" in antwort.headers["location"]
        meldung = _fehler(antwort)
        assert meldung.startswith("1 Belegdatei geprüft, alle unverändert.")
        assert "Die Dateirechte von 1 Belegdatei" in meldung
        assert pfad.stat().st_mode & 0o777 == 0o444
        with datenbank() as conn:
            eintrag = conn.execute(
                "SELECT begruendung FROM journal"
                " WHERE aktion = 'BELEG_INTEGRITAET' ORDER BY id DESC").fetchone()[0]
        assert "Dateirechte auf 0444 zurückgesetzt" in eintrag
        meldung = _fehler(klient.post("/belege/integritaet", follow_redirects=False))
        assert meldung == "1 Belegdatei geprüft, alle unverändert."

    def test_rechte_nicht_setzbar_bleibt_befund(self, klient, datenbank, tmp_path, monkeypatch):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        _buchen(klient, name)
        with datenbank() as conn:
            pfad = tmp_path / conn.execute("SELECT pfad FROM belegdatei").fetchone()[0]
        os.chmod(pfad, 0o644)

        def verweigert(*_a, **_k):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(p.os, "chmod", verweigert)
        antwort = klient.post("/belege/integritaet", follow_redirects=False)
        assert "fehler=" in antwort.headers["location"]
        meldung = _fehler(antwort)
        assert meldung.startswith("1 Befund — ")
        assert "ließen sich nicht auf 0444 zurücksetzen" in meldung
        assert "Errno" not in meldung and "not permitted" not in meldung

    def test_waise_mit_umlauten(self, klient, tmp_path):
        archiv = tmp_path / p.ARCHIV / "2026"
        archiv.mkdir(parents=True)
        (archiv / "fremd.pdf").write_bytes(b"%PDF")
        meldung = _fehler(klient.post("/belege/integritaet", follow_redirects=False))
        assert "prüfen und erneut über den Eingang buchen oder wegräumen" in meldung


# ------------------------------------------------------- Lieferanten-Vorgaben

class TestLieferantenVorgaben:
    def test_maske_belegt_quote_kategorie_und_reverse_charge_vor(self, klient, datenbank):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        _buchen(klient, name, **HETZNER)
        hosting = _kategorie(datenbank, "HOSTING")
        with datenbank() as conn:
            conn.execute("UPDATE lieferant SET quote_default = 70, standard_kategorie_id = ?,"
                         " reverse_charge_default = 1 WHERE id = 1", (hosting,))
        zweiter = _eingangsname(_hochladen(
            klient, PDF_DE.read_bytes().replace(b"%%EOF", b"%%EOF\n% zwei"), "b.pdf"))
        text = klient.get(f"/belege/pruefen/{zweiter}").text
        assert 'name="quote" min="0" max="100" value="70"' in text
        assert re.search(rf'<option value="{hosting}"[^>]*selected', text)
        assert re.search(r'name="reverse_charge" value="1"\s*checked', text)

    def test_vorgabe_merken_beim_buchen(self, klient, datenbank):
        hosting = _kategorie(datenbank, "HOSTING")
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        meldung = _fehler(_buchen(klient, name, **{**HETZNER, "quote": "80"},
                                  kategorie_id=str(hosting), vorgabe_merken="1"))
        assert "Kategorie und Quote sind ab jetzt Vorgabe für Hetzner Online GmbH" in meldung
        with datenbank() as conn:
            zeile = conn.execute("SELECT standard_kategorie_id, quote_default FROM lieferant"
                                 ).fetchone()
            assert tuple(zeile) == (hosting, 80)
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE objekttyp = 'lieferant'"
                                " AND aktion = 'AENDERN'").fetchone()[0] == 2

    def test_checkbox_steht_in_der_maske(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        assert ("Kategorie und Quote als Vorgabe für diesen Lieferanten merken"
                in klient.get(f"/belege/pruefen/{name}").text)


# --------------------------------------------------- Dublette byteidentisch

class TestDubletteByteidentisch:
    def test_formular_ist_ausgeblendet(self, klient):
        erster = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        _buchen(klient, erster)
        zweiter = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "nochmal.pdf"))
        text = klient.get(f"/belege/pruefen/{zweiter}").text
        assert "schon gebucht" in text
        assert "Beleg buchen" not in text
        assert 'href="/belege#beleg-1"' in text
        assert "Datei aus dem Eingang entfernen" in text

    def test_doppelklick_meldet_die_gelungene_buchung(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        assert _buchen(klient, name).status_code == 303
        zweiter = _buchen(klient, name)
        assert zweiter.status_code == 303
        assert "erfolg=" in zweiter.headers["location"]
        assert _fehler(zweiter) == "Diese Datei wurde bereits als Beleg 1 gebucht."


# -------------------------------------------------------------- Dateiabruf

class TestDateiabruf:
    @pytest.mark.parametrize("beleg_id", ["9999", "0", "-1"])
    def test_unbekannter_beleg(self, klient, beleg_id):
        antwort = klient.get(f"/belege/{beleg_id}/datei", follow_redirects=False)
        assert _fehler(antwort) == "Diesen Beleg gibt es nicht."

    def test_eigenbeleg_hat_keine_datei(self, klient, datenbank):
        with datenbank() as conn:
            conn.execute(
                "INSERT INTO beleg (belegdatum, betrag_brutto_cent, betrag_betrieblich_cent,"
                " ist_eigenbeleg, notiz, erfasst_am) VALUES ('2026-04-01', 2490, 2490, 1,"
                " 'Parkautomat ohne Belegausgabe, Kundentermin', '2026-04-01')")
        antwort = klient.get("/belege/1/datei", follow_redirects=False)
        assert _fehler(antwort) == "Ein Eigenbeleg hat keine Datei."


# ----------------------------------------------------------- Anlagegut, Text

class TestAnlagegutUndTexte:
    def test_anlagegut_wird_per_script_vorbelegt(self, klient, datenbank):
        anlage = _kategorie(datenbank, "HARDWARE_ANLAGE")
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        text = klient.get(f"/belege/pruefen/{name}").text
        assert re.search(rf'<option value="{anlage}"\s+data-anlage="1"', text)
        assert "n > 800" in text
        # Ohne JavaScript bleibt der Haken, wie er ist: nicht vorgehakt.
        assert re.search(r'name="anlagegut" value="1"\s*>', text)

    def test_vision_meldung_mit_umlaut(self):
        from app.logik.beleg import vision_mac

        stand = vision_mac.ist_verfuegbar()
        if not stand.moeglich:
            assert "muessen" not in stand.begruendung
            assert "Bruecke" not in stand.begruendung


# ------------------------------------------- Anlagegut nur bei Wirtschaftsgütern

class TestAnlagegutVorbelegung:
    """Die Prüfmaske hakte „Anlagegut“ bei JEDEM Beleg über 800 € netto an, auch
    bei einer Dienstleistung. Eine Hosting-Rechnung über 1.000 € netto fehlte
    danach in der EÜR, und Gewinn wie Steuerrücklage fielen zu hoch aus. Die
    800-€-Grenze gilt nur für Wirtschaftsgüter: Geräte und gekaufte Software,
    also die Kategorien mit Wertgrenze. Im Browser geprüft (Chromium): Hosting
    und Software-Abos bleiben ohne Haken, Hardware und Software-Kauf nicht."""

    def _optionen(self, klient) -> dict[str, str]:
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "a.pdf"))
        seite = klient.get(f"/belege/pruefen/{name}").text
        import html

        return {html.unescape(m.group(2)).strip(): m.group(1) for m in re.finditer(
            r'<option value="\d+"([^>]*)>([^<]+?) \(KZ', seite)}

    def test_nur_kategorien_mit_wertgrenze_tragen_die_markierung(self, klient):
        optionen = self._optionen(klient)
        wirtschaftsgut = {name for name, attribute in optionen.items()
                          if 'data-wirtschaftsgut="1"' in attribute}
        assert "Hosting, Domains & Cloud" not in wirtschaftsgut
        assert "Software-Abos (SaaS)" not in wirtschaftsgut
        assert "Software-Kauf (Dauerlizenz)" in wirtschaftsgut
        assert "Hardware & Einrichtung bis 800 EUR netto" in wirtschaftsgut

    def test_skript_prueft_die_800_euro_nur_bei_wirtschaftsguetern(self, klient):
        name = _eingangsname(_hochladen(klient, PDF_DE.read_bytes(), "b.pdf"))
        seite = klient.get(f"/belege/pruefen/{name}").text
        assert "(wirtschaftsgut && !isNaN(n) && n > 800)" in seite
        assert "|| (!isNaN(n) && n > 800)" not in seite

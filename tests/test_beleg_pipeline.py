"""Textschicht, Lernspeicher, Bilderkennung und der Weg in die Buchfuehrung.

Zwei Dinge pruefen die Tests hier besonders:

* **Die Stufenfolge.** Ist XML vorhanden, darf die Textschicht nicht laufen --
  bei einer E-Rechnung ist das XML massgebend.
* **Dass vor der Bestaetigung nichts in der Datenbank entsteht.** Ein Beleg
  braucht Datum und Betrag; einen Datensatz vorher anzulegen hiesse, beide zu
  erfinden. Eine erfundene Zahl in der Buchfuehrung ist schlimmer als eine
  fehlende, weil die fehlende auffaellt.
"""

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from app import db
from app.logik import kategorien
from app.logik.beleg import pipeline as p
from app.logik.beleg import profil, text_leser, vision_mac
from app.logik.beleg.ergebnis import BelegFehler, Fund, Vorschlag

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
PDF_EN = FIXTURES / "lieferantenrechnung_en.pdf"
CII = FIXTURES / "zugferd_cii.xml"
HEUTE = date(2026, 10, 12)


@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


@pytest.fixture
def eingerichtet(conn):
    conn.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, ust_idnr)"
        " VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095', 'Hamburg',"
        " '12/345/67890', 'DE999999999')"
    )
    return 1


@pytest.fixture
def hosting(conn):
    return kategorien.nach_schluessel(conn, "HOSTING")["id"]


def _lieferant(conn, name="Hetzner Online GmbH", ust_idnr="DE812871812"):
    return conn.execute(
        "INSERT INTO lieferant (name, ust_idnr, angelegt_am) VALUES (?, ?, ?)",
        (name, ust_idnr, db.heute()),
    ).lastrowid


# ------------------------------------------------------------- Stufe 2: Text

class TestTextschicht:
    def test_digitales_pdf_wird_gelesen(self):
        gelesen = text_leser.lies_datei(PDF_DE, heute=HEUTE)
        assert gelesen.quelle == "pdf_text"
        assert gelesen.wert("brutto") == 829
        assert gelesen.wert("netto") == 697
        assert gelesen.wert("belegdatum") == "2026-03-14"
        assert gelesen.wert("belegnummer") == "R0012345678"

    def test_englischer_beleg_ohne_dezimalfehler(self):
        """Der stille Fehler: 1,234.56 mit deutschen Regeln sind 1,23."""
        gelesen = text_leser.lies_datei(PDF_EN, heute=HEUTE)
        assert gelesen.wert("brutto") == 123456
        assert gelesen.wert("reverse_charge") is True
        assert any("Englisches Zahlenformat" in m for m in gelesen.meldungen)

    def test_eigene_ust_idnr_wird_nicht_als_lieferant_gelesen(self):
        gelesen = text_leser.lies_text(
            "Lieferant: LU26888617\nLeistungsempfaenger: DE999999999\n"
            "Gesamtbetrag 100,00 EUR",
            eigene_ust_idnr="DE999999999",
        )
        assert gelesen.wert("lieferant_ust_idnr") == "LU26888617"

    def test_unbezeichnetes_datum_ist_nicht_sicher(self):
        """Ohne "Rechnungsdatum" davor ist das frueheste Datum ein Fund --
        vielleicht das Lieferdatum. Gruen darf es nicht sein."""
        vorschlag = text_leser.lies_text(
            "Lieferung am 03.03.2026\nGesamtbetrag 10,00 EUR", heute=HEUTE
        )
        assert vorschlag.wert("belegdatum") == "2026-03-03"
        assert vorschlag.felder["belegdatum"].sicher is False
        bezeichnet = text_leser.lies_text(
            "Rechnungsdatum 03.03.2026\nGesamtbetrag 10,00 EUR", heute=HEUTE
        )
        assert bezeichnet.felder["belegdatum"].sicher is True

    def test_zwei_fremde_ust_idnrn_machen_den_fund_unsicher(self):
        vorschlag = text_leser.lies_text(
            "Verkäufer DE812871812\nZahlungsabwicklung LU26888617\nGesamtbetrag 10,00 EUR",
            heute=HEUTE, eigene_ust_idnr="DE999999999",
        )
        assert vorschlag.wert("lieferant_ust_idnr") == "DE812871812"
        assert vorschlag.felder["lieferant_ust_idnr"].sicher is False
        assert "2 verschiedene" in vorschlag.felder["lieferant_ust_idnr"].nachweis

    def test_lieferantenname_ist_nie_sicher(self):
        """Er steht auf einem Beleg nirgends bezeichnet -- die erste Zeile ist
        nur die wahrscheinlichste Stelle."""
        gelesen = text_leser.lies_datei(PDF_DE, heute=HEUTE)
        assert gelesen.felder["lieferant"].sicher is False
        assert gelesen.wert("lieferant") == "Hetzner Online GmbH"

    def test_leeres_pdf_gilt_als_scan(self, tmp_path):
        from pypdf import PdfWriter

        leer = tmp_path / "scan.pdf"
        schreiber = PdfWriter()
        schreiber.add_blank_page(width=595, height=842)
        with leer.open("wb") as datei:
            schreiber.write(datei)
        assert text_leser.lies_datei(leer) is None
        assert text_leser.hole_text(leer).brauchbar is False

    def test_kaputtes_pdf_bricht_ab(self, tmp_path):
        kaputt = tmp_path / "x.pdf"
        kaputt.write_bytes(b"%PDF-1.7 und dann nichts")
        with pytest.raises(BelegFehler):
            text_leser.hole_text(kaputt)


class TestLieferantenname:
    @pytest.mark.parametrize("text,erwartet", [
        ("Hetzner Online GmbH\nIndustriestr. 25", "Hetzner Online GmbH"),
        ("RECHNUNG\nFigma, Inc.\n760 Market St", "Figma, Inc."),
        ("Max Mustermann\nMusterweg 1", "Max Mustermann"),
        ("Amazon Web Services EMEA SARL\nLuxembourg", "Amazon Web Services EMEA SARL"),
    ])
    def test_erkennung(self, text, erwartet):
        assert text_leser.finde_lieferant(text)[0] == erwartet

    def test_formularwoerter_werden_uebersprungen(self):
        text = "Rechnung\nKundennummer: 4711\nSeite 1 von 2\nEchte Firma GmbH"
        assert text_leser.finde_lieferant(text)[0] == "Echte Firma GmbH"

    def test_rechtsform_gewinnt_gegen_erste_zeile(self):
        text = "Vielen Dank fuer Ihren Auftrag\nSoftwarehaus Nord GmbH"
        assert text_leser.finde_lieferant(text)[0] == "Softwarehaus Nord GmbH"


class TestWaehrung:
    @pytest.mark.parametrize("text,erwartet", [
        ("Gesamtbetrag 8,29 EUR", "EUR"),
        ("Total $1,234.56", "USD"),
        ("Betrag 100,00 €", "EUR"),
        ("Total CHF 55.00", "CHF"),
        ("Kein Geld hier", None),
    ])
    def test_erkennung(self, text, erwartet):
        assert text_leser.finde_waehrung(text) == erwartet


# ------------------------------------------------------- Stufe 3: Vision

class TestVision:
    def test_meldet_sich_auf_diesem_system_ab(self):
        """Dieser Rechner laeuft nicht unter macOS -- und die Stufe sagt das,
        statt den Import zu sprengen.

        Den rohen Systemnamen ("linux") nennt die Meldung nicht mehr: sie steht
        auf der Belegseite, und dort hilft er dem Nutzer nicht weiter.
        """
        stand = vision_mac.ist_verfuegbar()
        if sys.platform != "darwin":
            assert not stand
            assert "macOS" in stand.begruendung
            assert sys.platform not in stand.begruendung

    def test_aufruf_ohne_vision_wirft_mit_begruendung(self, tmp_path):
        if sys.platform == "darwin" and vision_mac.ist_verfuegbar():
            pytest.skip("Auf diesem Mac ist Vision verfuegbar")
        foto = tmp_path / "beleg.jpg"
        foto.write_bytes(b"\xff\xd8\xff\xe0")
        with pytest.raises(BelegFehler, match="macOS|ocrmac|pypdfium2"):
            vision_mac.lies_datei(foto)

    def test_kaesten_werden_zu_zeilen(self):
        """Vision liefert Kaesten in Lesereihenfolge, nicht Zeilen.

        Haengt man sie einfach mit Umbruechen zusammen, steht 'Gesamtbetrag'
        allein auf einer Zeile und der Betrag auf einer anderen -- und dann
        findet die Feldsuche nichts, weil sie die Zeile als Einheit braucht.
        """
        erkannt = [
            ("8,29 EUR", 0.98, (0.70, 0.300, 0.12, 0.02)),
            ("Gesamtbetrag", 0.99, (0.10, 0.302, 0.20, 0.02)),
            ("Rechnung", 0.99, (0.10, 0.800, 0.15, 0.02)),
            ("1,32 EUR", 0.97, (0.70, 0.350, 0.12, 0.02)),
            ("Umsatzsteuer 19 %", 0.96, (0.10, 0.351, 0.25, 0.02)),
        ]
        assert vision_mac.zeilen_aus_kaesten(erkannt) == (
            "Rechnung\nUmsatzsteuer 19 % 1,32 EUR\nGesamtbetrag 8,29 EUR"
        )

    def test_leere_erkennung(self):
        assert vision_mac.zeilen_aus_kaesten([]) == ""
        assert vision_mac.zeilen_aus_kaesten([("  ", 0.5, (0, 0, 0, 0))]) == ""

    def test_aufgezeichnete_ausgabe_wird_ausgewertet(self):
        """Der Teil, der sich hier pruefen laesst: von erkanntem Text zu
        Feldern. Die Erkennung selbst wird auf dem Mac abgenommen."""
        erkannt = [
            ("Hetzner Online GmbH", 0.99, (0.10, 0.950, 0.30, 0.02)),
            ("Rechnungsnummer: R0012345678", 0.98, (0.10, 0.880, 0.35, 0.02)),
            ("Rechnungsdatum: 14.03.2026", 0.98, (0.10, 0.850, 0.33, 0.02)),
            ("Nettobetrag", 0.99, (0.10, 0.400, 0.15, 0.02)),
            ("6,97 EUR", 0.97, (0.70, 0.401, 0.12, 0.02)),
            ("Umsatzsteuer 19 %", 0.98, (0.10, 0.370, 0.22, 0.02)),
            ("1,32 EUR", 0.97, (0.70, 0.371, 0.12, 0.02)),
            ("Gesamtbetrag", 0.99, (0.10, 0.340, 0.18, 0.02)),
            ("8,29 EUR", 0.96, (0.70, 0.341, 0.12, 0.02)),
        ]
        text = vision_mac.zeilen_aus_kaesten(erkannt)
        gelesen = vision_mac._aus_text(text, heute=HEUTE)
        assert gelesen.quelle == "ocr_lokal"
        assert gelesen.wert("brutto") == 829
        assert gelesen.wert("netto") == 697
        assert gelesen.wert("belegdatum") == "2026-03-14"

    def test_bilderkennung_ist_niemals_sicher(self):
        """Ein verlesenes Zeichen ist bei einem Betrag ein anderer Betrag.

        Die aufgehende Arithmetik senkt das Risiko erheblich, beseitigt es
        aber nicht -- und die Pruefmaske soll das zeigen.
        """
        text = ("Hetzner Online GmbH\nRechnungsdatum: 14.03.2026\n"
                "Nettobetrag 6,97 EUR\nUmsatzsteuer 19 % 1,32 EUR\n"
                "Gesamtbetrag 8,29 EUR")
        gelesen = vision_mac._aus_text(text, heute=HEUTE)
        assert gelesen.unsichere_felder
        assert set(gelesen.unsichere_felder) == set(gelesen.felder)
        assert any("Bilderkennung" in m for m in gelesen.meldungen)


# ------------------------------------------------------------- Lernspeicher

class TestLernspeicher:
    AWS_1 = """Amazon Web Services EMEA SARL
Invoice Number: EUINDE26-1234567
Invoice Date: March 3, 2026

Subtotal                      $1,234.56
VAT (0%)                           $0.00
TOTAL AMOUNT DUE               $1,234.56
"""

    def test_lernt_die_beschriftung(self, conn):
        lid = _lieferant(conn, "AWS", "LU26888617")
        conn.execute("BEGIN IMMEDIATE")
        gelernt = profil.lerne(conn, lid, self.AWS_1, {
            "belegnummer": "EUINDE26-1234567",
            "belegdatum": "2026-03-03",
            "brutto": 123456,
            "netto": 123456,
        })
        conn.execute("COMMIT")
        assert set(gelernt) >= {"belegnummer", "belegdatum", "brutto"}
        nach_feld = {m.feld: m.beschriftung for m in profil.muster(conn, lid)}
        assert nach_feld["belegnummer"] == "Invoice Number"
        assert nach_feld["belegdatum"] == "Invoice Date"
        assert nach_feld["brutto"] == "TOTAL AMOUNT DUE"

    def test_gesamtbetrag_lernt_die_letzte_fundstelle(self, conn):
        """Bei steuerfreien Belegen steht derselbe Betrag auf allen drei
        Summenzeilen. Lernte man die erste, waere der Gesamtbetrag beim
        naechsten Beleg mit Steuer um die Steuer zu niedrig."""
        lid = _lieferant(conn, "AWS", "LU26888617")
        conn.execute("BEGIN IMMEDIATE")
        profil.lerne(conn, lid, self.AWS_1, {"brutto": 123456, "netto": 123456})
        conn.execute("COMMIT")
        nach_feld = {m.feld: m.beschriftung for m in profil.muster(conn, lid)}
        assert nach_feld["brutto"] == "TOTAL AMOUNT DUE"
        assert nach_feld["netto"] == "Subtotal"

    def test_zahlungshinweis_wird_nicht_als_beschriftung_gelernt(self, conn):
        """Unter dem Summenblock steht "Bitte ueberweisen Sie 119,00 EUR bis
        zum ..." -- dieselbe Zahl, letzte Fundstelle. Gelernt werden muss die
        Summenzeile, nicht der Satz darunter."""
        lid = _lieferant(conn, "Druckerei", "DE111111111")
        text = ("Nettobetrag 100,00 EUR\nUmsatzsteuer 19 % 19,00 EUR\n"
                "Gesamtbetrag 119,00 EUR\n\n"
                "Bitte überweisen Sie 119,00 EUR bis zum 30.04.2026 auf das Konto")
        conn.execute("BEGIN IMMEDIATE")
        profil.lerne(conn, lid, text, {"brutto": 11900, "netto": 10000, "ust": 1900})
        conn.execute("COMMIT")
        nach_feld = {m.feld: m.beschriftung for m in profil.muster(conn, lid)}
        assert nach_feld["brutto"] == "Gesamtbetrag"

    def test_zweiter_beleg_wird_ohne_raten_gelesen(self, conn):
        lid = _lieferant(conn, "AWS", "LU26888617")
        conn.execute("BEGIN IMMEDIATE")
        profil.lerne(conn, lid, self.AWS_1, {
            "belegnummer": "EUINDE26-1234567", "belegdatum": "2026-03-03",
            "brutto": 123456,
        })
        conn.execute("COMMIT")

        zweiter = (self.AWS_1
                   .replace("EUINDE26-1234567", "EUINDE26-7654321")
                   .replace("March 3, 2026", "April 7, 2026")
                   .replace("TOTAL AMOUNT DUE               $1,234.56",
                            "TOTAL AMOUNT DUE               $2,000.00"))
        gelesen = profil.anwenden(conn, lid, zweiter, quelle="pdf_text", heute=HEUTE)
        assert gelesen.wert("belegnummer") == "EUINDE26-7654321"
        assert gelesen.wert("belegdatum") == "2026-04-07"
        assert gelesen.wert("brutto") == 200000
        assert gelesen.unsichere_felder == ()

    def test_zahlenformat_haengt_am_lieferanten(self, conn):
        """AWS, Google und Figma rechnen in Euro und schreiben 1,234.56.
        Beim zweiten Beleg muss darueber nicht mehr geraten werden."""
        lid = _lieferant(conn, "AWS", "LU26888617")
        conn.execute("BEGIN IMMEDIATE")
        profil.lerne(conn, lid, self.AWS_1, {"brutto": 123456})
        conn.execute("COMMIT")
        assert profil.zahlenformat(conn, lid) == "EN"

    def test_layoutwechsel_wird_gemeldet(self, conn):
        lid = _lieferant(conn, "AWS", "LU26888617")
        conn.execute("BEGIN IMMEDIATE")
        profil.lerne(conn, lid, self.AWS_1, {"brutto": 123456})
        conn.execute("COMMIT")
        gelesen = profil.anwenden(conn, lid, "Voellig anderes Layout\nBetrag 50,00 EUR",
                                  quelle="pdf_text")
        assert gelesen.felder == {}
        assert any("Layout geändert" in m for m in gelesen.meldungen)

    def test_wert_am_zeilenanfang_wird_nicht_gelernt(self, conn):
        """Ohne Beschriftung ist die Zeile als Anker unbrauchbar. Etwas
        Falsches zu lernen ist schlechter als nichts zu lernen."""
        lid = _lieferant(conn, "Irgendwer", None)
        conn.execute("BEGIN IMMEDIATE")
        gelernt = profil.lerne(conn, lid, "1.234,56 EUR Gesamt", {"brutto": 123456})
        conn.execute("COMMIT")
        assert gelernt == []

    def test_steuersatz_bleibt_aus_der_beschriftung(self, conn):
        """'Umsatzsteuer 19 %' wird als 'Umsatzsteuer' gelernt -- sonst passt
        das Muster nicht mehr, wenn derselbe Lieferant einmal 7 % abrechnet."""
        lid = _lieferant(conn, "Hetzner", "DE812871812")
        conn.execute("BEGIN IMMEDIATE")
        profil.lerne(conn, lid, "Umsatzsteuer 19 % 1,32 EUR", {"ust": 132})
        conn.execute("COMMIT")
        nach_feld = {m.feld: m.beschriftung for m in profil.muster(conn, lid)}
        assert nach_feld["ust"] == "Umsatzsteuer"

    def test_ueberholtes_muster_wird_nicht_mehr_angewandt(self, conn):
        lid = _lieferant(conn, "AWS", "LU26888617")
        conn.execute("BEGIN IMMEDIATE")
        profil.lerne(conn, lid, self.AWS_1, {"brutto": 123456})
        conn.execute("COMMIT")
        for _ in range(3):
            profil.notiere_fehlschlag(conn, lid, ["brutto"])
        assert all(m.ueberholt for m in profil.muster(conn, lid))
        gelesen = profil.anwenden(conn, lid, self.AWS_1, quelle="pdf_text")
        assert "brutto" not in gelesen.felder


# ---------------------------------------------------------------- Pipeline

class TestAufnehmen:
    def test_datei_landet_im_eingang(self, datenordner):
        eingang = p.aufnehmen(b"%PDF-1.7 Inhalt", "rechnung.pdf", datenordner)
        assert eingang.pfad.parent.name == p.EINGANG
        assert eingang.pfad.read_bytes() == b"%PDF-1.7 Inhalt"
        assert eingang.original_name == "rechnung.pdf"
        assert eingang.groesse == 15

    def test_name_kommt_aus_der_pruefsumme(self, datenordner):
        """Der Originalname kommt von aussen: '../../steuer.sqlite' waere ein
        Weg aus dem Ordner heraus.

        Der Inhalt beginnt seit der Magic-Bytes-Pruefung mit %PDF; vorher
        genuegte beliebiger Inhalt, und genau das liess ein Programm mit der
        Endung .pdf ins Archiv."""
        eingang = p.aufnehmen(b"%PDF-1.7 " + b"x" * 100, "../../steuer.sqlite.pdf", datenordner)
        assert eingang.pfad.parent == datenordner / p.EINGANG
        assert ".." not in eingang.pfad.name
        assert eingang.original_name == "steuer.sqlite.pdf"

    def test_fremdes_format_wird_abgewiesen(self, datenordner):
        """Ein .docx ist kein Beleg, sondern eine Aufstellung -- und
        veraenderliche Formate haben in einem Archiv nichts zu suchen."""
        with pytest.raises(BelegFehler, match="Belegformat"):
            p.aufnehmen(b"PK\x03\x04", "aufstellung.docx", datenordner)

    def test_leere_datei_wird_abgewiesen(self, datenordner):
        with pytest.raises(BelegFehler, match="leer"):
            p.aufnehmen(b"", "leer.pdf", datenordner)

    def test_zu_grosse_datei_wird_abgewiesen(self, datenordner):
        with pytest.raises(BelegFehler, match="größer als 50 MB"):
            p.aufnehmen(b"%PDF" + b"x" * (p.MAX_BYTES - 3), "riesig.pdf", datenordner)

    def test_liegen_geblieben_zeigt_den_eingang(self, datenordner):
        assert p.liegen_geblieben(datenordner) == []
        p.aufnehmen(b"%PDF-1.7 a", "a.pdf", datenordner)
        p.aufnehmen(b"%PDF-1.7 b", "b.pdf", datenordner)
        assert len(p.liegen_geblieben(datenordner)) == 2


class TestStufenfolge:
    def test_zugferd_laeuft_nicht_durch_die_textschicht(self, conn, eingerichtet, datenordner):
        """Bei einer E-Rechnung ist das XML massgebend; eine Auslesung des
        Bildteils ist unzulaessig."""
        from pypdf import PdfWriter

        pfad = datenordner / "zugferd.pdf"
        schreiber = PdfWriter()
        schreiber.add_blank_page(width=595, height=842)
        schreiber.add_attachment("factur-x.xml", CII.read_bytes())
        with pfad.open("wb") as datei:
            schreiber.write(datei)

        eingang = p.aufnehmen(pfad.read_bytes(), "zugferd.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        assert lesung.stufen == ("xml",)
        assert lesung.vorschlag.quelle == "xml"
        assert lesung.vorschlag.wert("brutto") == 546
        assert any("maßgebend" in m for m in lesung.meldungen)

    def test_digitales_pdf_nimmt_die_textschicht(self, conn, eingerichtet, datenordner):
        eingang = p.aufnehmen(PDF_DE.read_bytes(), "hetzner.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        assert lesung.stufen == ("xml", "pdf_text")
        assert lesung.rohtext
        assert lesung.vorschlag.wert("brutto") == 829

    def test_scan_ohne_vision_endet_bei_manuell(self, conn, eingerichtet, datenordner):
        from pypdf import PdfWriter

        if sys.platform == "darwin" and vision_mac.ist_verfuegbar():
            pytest.skip("Auf diesem Mac laeuft die Bilderkennung")
        pfad = datenordner / "scan.pdf"
        schreiber = PdfWriter()
        schreiber.add_blank_page(width=595, height=842)
        with pfad.open("wb") as datei:
            schreiber.write(datei)

        eingang = p.aufnehmen(pfad.read_bytes(), "scan.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        assert lesung.stufen == ("xml", "pdf_text", "ocr_lokal", "manuell")
        assert lesung.vorschlag.felder == {}
        assert any("macOS" in m for m in lesung.meldungen)

    def test_kaputtes_xml_bricht_die_folge_ab(self, conn, eingerichtet, datenordner):
        from pypdf import PdfWriter

        pfad = datenordner / "kaputt.pdf"
        schreiber = PdfWriter()
        schreiber.add_blank_page(width=595, height=842)
        schreiber.add_attachment("factur-x.xml", b"<nicht wohlgeformt")
        with pfad.open("wb") as datei:
            schreiber.write(datei)
        eingang = p.aufnehmen(pfad.read_bytes(), "kaputt.pdf", datenordner)
        with pytest.raises(BelegFehler, match="unzulässig"):
            p.lies(conn, eingang, heute=HEUTE)

    def test_lesen_schreibt_nichts_in_die_datenbank(self, conn, eingerichtet, datenordner):
        """Ein Beleg braucht Datum und Betrag. Einen Datensatz vorher anzulegen
        hiesse, beide zu erfinden."""
        eingang = p.aufnehmen(PDF_DE.read_bytes(), "hetzner.pdf", datenordner)
        p.lies(conn, eingang, heute=HEUTE)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM belegdatei").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM extraktion").fetchone()[0] == 0


class TestLieferantenerkennung:
    def test_an_der_ust_idnr(self, conn, eingerichtet, datenordner):
        """'Amazon Web Services EMEA SARL' und 'AWS EMEA SARL' sind derselbe
        Lieferant -- ein Namensvergleich sieht das nicht."""
        lid = _lieferant(conn, "AWS EMEA", "LU26888617")
        gefunden = p.finde_lieferant(conn, "Irgendein anderer Name", "LU 26888617")
        assert gefunden["id"] == lid

    def test_am_namen(self, conn):
        lid = _lieferant(conn, "Hetzner Online GmbH", None)
        assert p.finde_lieferant(conn, "hetzner online gmbh", None)["id"] == lid

    def test_bekannter_name_ersetzt_den_geratenen(self, conn, eingerichtet, datenordner):
        lid = _lieferant(conn, "Hetzner Online GmbH", "DE812871812")
        eingang = p.aufnehmen(PDF_DE.read_bytes(), "hetzner.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        assert lesung.lieferant_id == lid
        assert lesung.vorschlag.felder["lieferant"].sicher is True

    def test_unbekannter_lieferant_bleibt_offen(self, conn, eingerichtet, datenordner):
        eingang = p.aufnehmen(PDF_DE.read_bytes(), "hetzner.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        assert lesung.lieferant_id is None
        assert lesung.vorschlag.felder["lieferant"].sicher is False


class TestBuchen:
    def _lesung(self, conn, datenordner, datei=PDF_DE, name="hetzner.pdf"):
        eingang = p.aufnehmen(datei.read_bytes(), name, datenordner)
        return p.lies(conn, eingang, heute=HEUTE)

    def _bestaetigt(self, lesung, **ueberschreiben):
        werte = {feld: fund.wert for feld, fund in lesung.vorschlag.felder.items()}
        werte.update(ueberschreiben)
        return werte

    def test_vollstaendiger_durchlauf(self, conn, eingerichtet, datenordner, hosting):
        lid = _lieferant(conn)
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          lieferant_id=lid, kategorie_id=hosting)

        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?",
                             (buchung.beleg_id,)).fetchone()
        assert zeile["belegdatum"] == "2026-03-14"
        assert zeile["betrag_brutto_cent"] == 829
        assert zeile["betrag_netto_cent"] == 697
        assert zeile["betrag_ust_cent"] == 132
        assert zeile["betrag_betrieblich_cent"] == 829
        assert zeile["geprueft_am"]

    def test_original_liegt_nach_belegdatum_und_ist_schreibgeschuetzt(
        self, conn, eingerichtet, datenordner, hosting
    ):
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          kategorie_id=hosting)
        assert buchung.pfad.parent == datenordner / p.ARCHIV / "2026"
        assert buchung.pfad.name.startswith("2026-03-14_")
        assert oct(buchung.pfad.stat().st_mode & 0o777) == "0o444"
        assert p.liegen_geblieben(datenordner) == []

    def test_original_bleibt_byte_identisch(self, conn, eingerichtet, datenordner, hosting):
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          kategorie_id=hosting)
        assert buchung.pfad.read_bytes() == PDF_DE.read_bytes()
        assert p.pruefsumme(buchung.pfad) == p.pruefsumme(PDF_DE)

    def test_extraktion_wird_archiviert(self, conn, eingerichtet, datenordner, hosting):
        """Nach GoBD Rz. 130 ist das bestaetigte Ergebnis selbst
        aufbewahrungspflichtig und kein Zwischenergebnis."""
        import json

        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          kategorie_id=hosting)
        zeile = conn.execute("SELECT * FROM extraktion WHERE beleg_id = ?",
                             (buchung.beleg_id,)).fetchone()
        assert zeile["quelle"] == "pdf_text"
        assert zeile["regel_version"]
        assert "pdfplumber" in zeile["verfahren"]
        roh = json.loads(zeile["rohantwort_json"])
        assert roh["brutto"]["wert"] == 829
        assert roh["brutto"]["nachweis"]
        assert json.loads(zeile["bestaetigt_json"])["brutto"] == 829

    def test_quote_wird_einmal_gerundet(self, conn, eingerichtet, datenordner, hosting):
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          kategorie_id=hosting, quote=70)
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?",
                             (buchung.beleg_id,)).fetchone()
        assert zeile["betrag_brutto_cent"] == 829
        assert zeile["betrag_betrieblich_cent"] == 580   # 70 % von 8,29 EUR

    def test_dublette_wird_abgewiesen(self, conn, eingerichtet, datenordner, hosting):
        erste = self._lesung(conn, datenordner)
        p.buche(conn, erste, self._bestaetigt(erste), datenordner, kategorie_id=hosting)
        zweite = self._lesung(conn, datenordner, name="nochmal.pdf")
        assert zweite.ist_dublette
        with pytest.raises(BelegFehler, match="bereits als Beleg"):
            p.buche(conn, zweite, self._bestaetigt(zweite), datenordner,
                    kategorie_id=hosting)

    def test_dublette_bleibt_im_eingang_liegen(self, conn, eingerichtet, datenordner, hosting):
        """Es gab einen Schalter "bewusst buchen". Er konnte nie gelingen -- der
        UNIQUE-Index auf der Pruefsumme laesst dieselbe Datei kein zweites Mal
        zu --, und bei einem anderen Belegdatum war die Datei vorher schon ins
        Archiv verschoben: weg aus dem Eingang, ohne Datensatz. Jetzt wird die
        Dublette abgewiesen und die Datei bleibt, wo sie war."""
        erste = self._lesung(conn, datenordner)
        p.buche(conn, erste, self._bestaetigt(erste), datenordner, kategorie_id=hosting)
        zweite = self._lesung(conn, datenordner, name="nochmal.pdf")
        with pytest.raises(BelegFehler, match="bleibt im Eingang"):
            p.buche(conn, zweite, self._bestaetigt(zweite, belegdatum="2026-04-01"),
                    datenordner, kategorie_id=hosting)
        assert zweite.eingang.pfad.exists()
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1

    def test_scheitert_die_buchung_wandert_die_datei_zurueck(
        self, conn, eingerichtet, datenordner, hosting
    ):
        """Nach dem Verschieben bricht die Transaktion ab: ohne diesen Weg
        zurueck laege die Datei als Waise im Archiv, und der Prueflauf, der
        von den Datensaetzen ausgeht, faende sie nie."""
        lesung = self._lesung(conn, datenordner)
        with pytest.raises(sqlite3.IntegrityError):
            # Eine Kategorie, die es nicht gibt: der Fremdschluessel weist ab.
            p.buche(conn, lesung, self._bestaetigt(lesung), datenordner, kategorie_id=99999)
        assert lesung.eingang.pfad.exists(), "zurueck im Eingang"
        assert not list((datenordner / p.ARCHIV).rglob("*.pdf")), "keine Waise im Archiv"
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_waise_im_archiv_wird_vom_prueflauf_gefunden(
        self, conn, eingerichtet, datenordner, hosting
    ):
        lesung = self._lesung(conn, datenordner)
        p.buche(conn, lesung, self._bestaetigt(lesung), datenordner, kategorie_id=hosting)
        waise = datenordner / p.ARCHIV / "2026" / "2026-05-05_deadbeefdead.pdf"
        waise.write_bytes(b"%PDF-1.7 niemand zeigt auf mich")
        befunde = p.pruefe_integritaet(conn, datenordner)
        assert [b["befund"][:19] for b in befunde] == ["Datei im Archiv, au"]
        assert befunde[0]["pfad"] == str(waise)

    def test_fremdwaehrung_braucht_den_euro_betrag(
        self, conn, eingerichtet, datenordner, hosting
    ):
        """Ein Beleg ueber 100 USD stand mit 100 Euro in der EUeR -- ohne
        Umrechnung und ohne Hinweis. In die EUeR gehoert, was laut
        Kontoauszug abgeflossen ist."""
        lesung = self._lesung(conn, datenordner)
        with pytest.raises(BelegFehler, match="Euro-Betrag"):
            p.buche(conn, lesung, self._bestaetigt(lesung, waehrung="USD"),
                    datenordner, kategorie_id=hosting)
        assert lesung.eingang.pfad.exists()

        buchung = p.buche(
            conn, lesung, self._bestaetigt(lesung, waehrung="USD", brutto_eur=9250),
            datenordner, kategorie_id=hosting,
        )
        beleg = conn.execute("SELECT * FROM beleg WHERE id = ?", (buchung.beleg_id,)).fetchone()
        assert beleg["waehrung"] == "USD"
        assert beleg["betrag_fremdwaehrung_cent"] == 829, "der Nominalbetrag des Belegs"
        assert beleg["betrag_brutto_cent"] == 9250, "der Euro-Betrag zaehlt"
        assert beleg["betrag_betrieblich_cent"] == 9250
        assert beleg["betrag_netto_cent"] is None and beleg["betrag_ust_cent"] is None

    def test_ohne_datum_wird_nicht_gebucht(self, conn, eingerichtet, datenordner, hosting):
        lesung = self._lesung(conn, datenordner)
        with pytest.raises(BelegFehler, match="Belegdatum"):
            p.buche(conn, lesung, self._bestaetigt(lesung, belegdatum=None),
                    datenordner, kategorie_id=hosting)

    def test_ohne_betrag_wird_nicht_gebucht(self, conn, eingerichtet, datenordner, hosting):
        lesung = self._lesung(conn, datenordner)
        with pytest.raises(BelegFehler, match="Bruttobetrag"):
            p.buche(conn, lesung, self._bestaetigt(lesung, brutto=0),
                    datenordner, kategorie_id=hosting)

    def test_unpassendes_netto_wird_verworfen_und_nicht_gespeichert(
        self, conn, eingerichtet, datenordner, hosting
    ):
        """Ein halb ausgelesener Beleg gilt spaeter als vollstaendig, und dann
        rechnet jemand mit einer Zahl, die nie geprueft wurde."""
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung, netto=500, ust=100),
                          datenordner, kategorie_id=hosting)
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?",
                             (buchung.beleg_id,)).fetchone()
        assert zeile["betrag_brutto_cent"] == 829
        assert zeile["betrag_netto_cent"] is None
        assert zeile["betrag_ust_cent"] is None

    def test_grenzwert_netto_wird_mitgeschrieben(
        self, conn, eingerichtet, datenordner, hosting
    ):
        """Die GWG-Wertgrenze wird netto geprueft, gebucht wird brutto."""
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          kategorie_id=hosting)
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?",
                             (buchung.beleg_id,)).fetchone()
        assert zeile["netto_fuer_grenzwert_cent"] == 697

    def test_reverse_charge_beleg(self, conn, eingerichtet, datenordner):
        """Der AWS-Beleg lautet auf USD. Bis hierher buchte dieser Test 1.234,56
        Dollar als 1.234,56 Euro -- und hielt das fuer richtig. Gebucht wird,
        was laut Kontoauszug in Euro abgeflossen ist."""
        lesung = self._lesung(conn, datenordner, datei=PDF_EN, name="aws.pdf")
        assert lesung.vorschlag.wert("reverse_charge") is True
        assert lesung.vorschlag.wert("waehrung") == "USD"
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung, brutto_eur=113000),
                          datenordner)
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?",
                             (buchung.beleg_id,)).fetchone()
        assert zeile["reverse_charge"] == 1
        assert zeile["betrag_brutto_cent"] == 113000
        assert zeile["betrag_fremdwaehrung_cent"] == 123456
        assert zeile["betrag_ust_cent"] in (0, None)

    def test_lernt_beim_buchen(self, conn, eingerichtet, datenordner, hosting):
        lid = _lieferant(conn)
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          lieferant_id=lid, kategorie_id=hosting)
        assert "brutto" in buchung.gelernt
        assert {m.feld for m in profil.muster(conn, lid)} >= {"brutto", "belegdatum"}

    def test_journal_haelt_den_hash(self, conn, eingerichtet, datenordner, hosting):
        lesung = self._lesung(conn, datenordner)
        buchung = p.buche(conn, lesung, self._bestaetigt(lesung), datenordner,
                          kategorie_id=hosting)
        zeile = conn.execute(
            "SELECT * FROM journal WHERE aktion = 'BELEG_ERFASSEN'"
        ).fetchone()
        assert zeile["objekt_id"] == buchung.beleg_id
        assert zeile["hash"] == p.pruefsumme(buchung.pfad)


class TestIntegritaet:
    def test_heiles_archiv_meldet_nichts(self, conn, eingerichtet, datenordner, hosting):
        eingang = p.aufnehmen(PDF_DE.read_bytes(), "a.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        p.buche(conn, lesung, {feld: fund.wert for feld, fund in lesung.vorschlag.felder.items()},
                datenordner, kategorie_id=hosting)
        assert p.pruefe_integritaet(conn, datenordner) == []

    def test_veraenderte_datei_wird_gemeldet(self, conn, eingerichtet, datenordner, hosting):
        """Ohne diesen Prueflauf ist die gespeicherte Pruefsumme eine
        Behauptung."""
        eingang = p.aufnehmen(PDF_DE.read_bytes(), "a.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        buchung = p.buche(
            conn, lesung,
            {feld: fund.wert for feld, fund in lesung.vorschlag.felder.items()},
            datenordner, kategorie_id=hosting,
        )
        os.chmod(buchung.pfad, 0o644)
        buchung.pfad.write_bytes(buchung.pfad.read_bytes() + b"\n% manipuliert")
        befunde = p.pruefe_integritaet(conn, datenordner)
        assert len(befunde) == 1
        assert "Prüfsumme" in befunde[0]["befund"]

    def test_fehlende_datei_wird_gemeldet(self, conn, eingerichtet, datenordner, hosting):
        eingang = p.aufnehmen(PDF_DE.read_bytes(), "a.pdf", datenordner)
        lesung = p.lies(conn, eingang, heute=HEUTE)
        buchung = p.buche(
            conn, lesung,
            {feld: fund.wert for feld, fund in lesung.vorschlag.felder.items()},
            datenordner, kategorie_id=hosting,
        )
        os.chmod(buchung.pfad, 0o644)
        buchung.pfad.unlink()
        befunde = p.pruefe_integritaet(conn, datenordner)
        assert befunde and befunde[0]["befund"] == "Datei fehlt"


class TestVorschlag:
    def test_ergaenzen_ueberschreibt_nicht(self):
        """Die erste Stufe, die etwas gefunden hat, ist die verbindlichere."""
        spezifisch = Vorschlag("pdf_text", {"brutto": Fund(100, "pdf_text", "gelernt")})
        allgemein = Vorschlag("pdf_text", {
            "brutto": Fund(999, "pdf_text", "geraten"),
            "netto": Fund(80, "pdf_text", "geraten"),
        })
        zusammen = spezifisch.ergaenzt_um(allgemein)
        assert zusammen.wert("brutto") == 100
        assert zusammen.wert("netto") == 80

    def test_fehlende_pflichtfelder(self):
        leer = Vorschlag("manuell", {})
        assert set(leer.fehlende_felder) == {"lieferant", "belegdatum", "brutto"}

    def test_unbekannte_quelle_wird_abgewiesen(self):
        with pytest.raises(ValueError, match="Unbekannte Quelle"):
            Fund(1, "cloud", "")

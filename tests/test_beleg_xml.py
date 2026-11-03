"""Stufe 1: das eingebettete XML einer E-Rechnung.

Der wichtigste Test ist nicht, dass ein ZUGFeRD-PDF gelesen wird -- sondern
dass bei **unlesbarem** XML abgebrochen wird und kein Rueckfall auf
Bilderkennung erfolgt. Bei einer E-Rechnung ist das XML massgebend, und die
Auslesung des Bildteils ist unzulaessig; ein Rueckfall waere also nicht
Bequemlichkeit, sondern ein Rechtsfehler mit einer Zahl als Ergebnis, die
niemand bemerkt.
"""

from pathlib import Path

import pytest

from app.logik.beleg import xml_leser as x
from app.logik.beleg.ergebnis import BelegFehler

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CII = FIXTURES / "zugferd_cii.xml"
UBL = FIXTURES / "xrechnung_ubl.xml"


def _pdf_mit_anhang(ziel: Path, name: str, inhalt: bytes) -> Path:
    from pypdf import PdfWriter

    schreiber = PdfWriter()
    schreiber.add_blank_page(width=595, height=842)
    schreiber.add_attachment(name, inhalt)
    with ziel.open("wb") as datei:
        schreiber.write(datei)
    return ziel


def _pdf_ohne_anhang(ziel: Path) -> Path:
    from pypdf import PdfWriter

    schreiber = PdfWriter()
    schreiber.add_blank_page(width=595, height=842)
    with ziel.open("wb") as datei:
        schreiber.write(datei)
    return ziel


class TestCii:
    @pytest.fixture
    def gelesen(self):
        return x.lies(CII.read_bytes())

    def test_kopfdaten(self, gelesen):
        assert gelesen.wert("lieferant") == "Hetzner Online GmbH"
        assert gelesen.wert("belegnummer") == "RE-2026-0815"
        assert gelesen.wert("waehrung") == "EUR"

    def test_datum_im_format_102_wird_umgesetzt(self, gelesen):
        """CII schreibt 20260314, nicht 2026-03-14."""
        assert gelesen.wert("belegdatum") == "2026-03-14"

    def test_betraege_als_cent(self, gelesen):
        assert gelesen.wert("brutto") == 546
        assert gelesen.wert("netto") == 459
        assert gelesen.wert("ust") == 87
        assert gelesen.wert("ust_satz_promille") == 190

    def test_ust_idnr_nicht_die_steuernummer(self, gelesen):
        """Beide stehen als SpecifiedTaxRegistration, unterschieden nur durch
        schemeID. Ohne die Pruefung bekaeme man 201/123/45678."""
        assert gelesen.wert("lieferant_ust_idnr") == "DE812871812"

    def test_kein_reverse_charge_bei_kategorie_s(self, gelesen):
        assert "reverse_charge" not in gelesen.felder

    def test_summen_gehen_auf(self, gelesen):
        assert gelesen.meldungen == ()

    def test_alles_aus_dem_xml_ist_sicher(self, gelesen):
        """Aus dem XML ist nichts geraten -- es IST der Datensatz."""
        assert gelesen.unsichere_felder == ()
        assert gelesen.fehlende_felder == ()

    def test_faelligkeit_wird_nicht_als_zahlungsdatum_uebernommen(self, gelesen):
        """Fuer die EUeR zaehlt nach § 11 EStG der Abfluss. Eine Faelligkeit
        ist eine Ankuendigung, keine Zahlung."""
        assert "zahlungsdatum" not in gelesen.felder


class TestUbl:
    @pytest.fixture
    def gelesen(self):
        return x.lies(UBL.read_bytes())

    def test_kopfdaten(self, gelesen):
        assert gelesen.wert("belegnummer") == "INV-2026-4711"
        assert gelesen.wert("belegdatum") == "2026-02-28"

    def test_eingetragener_name_vor_handelsname(self, gelesen):
        """UBL fuehrt beide. Fuer einen Beleg ist der eingetragene belastbarer."""
        assert gelesen.wert("lieferant") == "Figma Ireland Limited"

    def test_reverse_charge_wird_erkannt(self, gelesen):
        """§ 13b Abs. 5 S. 1 UStG: der Normalfall bei auslaendischen
        Software-Abos, nicht der Rand."""
        assert gelesen.wert("reverse_charge") is True
        assert gelesen.wert("ust") == 0
        assert any("13b" in m for m in gelesen.meldungen)

    def test_brutto_ist_die_leistung_nicht_der_zahlbetrag(self, gelesen):
        """PayableAmount ist um die Anzahlung gemindert. Betriebsausgabe ist,
        was die Leistung gekostet hat."""
        assert gelesen.wert("brutto") == 18000
        assert any("Anzahlung" in m for m in gelesen.meldungen)


class TestSyntaxerkennung:
    def test_entschieden_wird_am_wurzelelement(self):
        """Nicht am Dateinamen: eine xrechnung.xml kann CII enthalten."""
        assert x.lies(CII.read_bytes()).wert("lieferant") == "Hetzner Online GmbH"
        assert x.lies(UBL.read_bytes()).wert("lieferant") == "Figma Ireland Limited"

    def test_gutschrift_wird_abgewiesen(self):
        """Eine Gutschrift kehrt das Vorzeichen um. Automatisch zu buchen waere
        ein Vorzeichenfehler in der EUeR."""
        roh = (b'<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
               b'CreditNote-2"><ID>G-1</ID></CreditNote>')
        with pytest.raises(BelegFehler, match="Gutschrift"):
            x.lies(roh)

    def test_fremdes_wurzelelement_wird_abgewiesen(self):
        with pytest.raises(BelegFehler, match="unbekanntes Wurzelelement"):
            x.lies(b"<Bestellung><Nr>1</Nr></Bestellung>")

    def test_unlesbares_xml_bricht_ab_und_faellt_nicht_zurueck(self):
        """Der wichtigste Test dieser Datei."""
        with pytest.raises(BelegFehler) as fehler:
            x.lies(b'<rsm:CrossIndustryInvoice><kaputt')
        assert "unzulässig" in str(fehler.value)
        assert "manuell" in str(fehler.value)


class TestAusDemPdf:
    def test_zugferd_pdf_wird_ohne_bilderkennung_gelesen(self, tmp_path):
        pfad = _pdf_mit_anhang(tmp_path / "re.pdf", "factur-x.xml", CII.read_bytes())
        gelesen = x.lies_datei(pfad)
        assert gelesen.quelle == "xml"
        assert gelesen.wert("brutto") == 546

    @pytest.mark.parametrize("name", [
        "factur-x.xml", "ZUGFeRD-invoice.xml", "zugferd-invoice.xml", "xrechnung.xml",
    ])
    def test_alle_genormten_dateinamen(self, tmp_path, name):
        pfad = _pdf_mit_anhang(tmp_path / "re.pdf", name, CII.read_bytes())
        assert x.lies_datei(pfad).wert("belegnummer") == "RE-2026-0815"

    def test_fremder_dateiname_wird_auch_gefunden(self, tmp_path):
        """Nicht jeder Erzeuger haelt sich an die Namen; eine .xml im Anhang
        ist aber immer ein Hinweis, der geprueft werden muss."""
        pfad = _pdf_mit_anhang(tmp_path / "re.pdf", "rechnung_daten.xml", CII.read_bytes())
        assert x.lies_datei(pfad).wert("belegnummer") == "RE-2026-0815"

    def test_pdf_ohne_anhang_gibt_none(self, tmp_path):
        """Keine E-Rechnung, sondern eine sonstige Rechnung in elektronischem
        Format. Dafuer ist Stufe 2 zustaendig -- hier nur kein Ergebnis."""
        assert x.lies_datei(_pdf_ohne_anhang(tmp_path / "re.pdf")) is None

    def test_pdf_mit_kaputtem_xml_bricht_ab(self, tmp_path):
        """Kein Rueckfall auf die Textschicht: das XML ist massgebend."""
        pfad = _pdf_mit_anhang(tmp_path / "re.pdf", "factur-x.xml", b"<kaputt")
        with pytest.raises(BelegFehler, match="unzulässig"):
            x.lies_datei(pfad)

    def test_bild_gibt_none(self, tmp_path):
        jpg = tmp_path / "foto.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0 kein PDF")
        assert x.lies_datei(jpg) is None

    def test_kaputtes_pdf_bricht_ab(self, tmp_path):
        kaputt = tmp_path / "re.pdf"
        kaputt.write_bytes(b"%PDF-1.7\nund dann nichts mehr")
        with pytest.raises(BelegFehler, match="beschädigt oder unvollständig"):
            x.lies_datei(kaputt)

    def test_xml_direkt_wird_gelesen(self, tmp_path):
        pfad = tmp_path / "xrechnung.xml"
        pfad.write_bytes(UBL.read_bytes())
        assert x.lies_datei(pfad).wert("belegnummer") == "INV-2026-4711"


class TestZahlen:
    @pytest.mark.parametrize("text,cent", [
        ("5.46", 546), ("0.87", 87), ("1234.00", 123400), ("19.99", 1999),
        ("-5.46", -546), (" 5.46 ", 546), ("0", 0),
    ])
    def test_betraege(self, text, cent):
        assert x._cent(text) == cent

    def test_kein_betrag_gibt_none(self):
        assert x._cent("keine Zahl") is None
        assert x._cent(None) is None
        assert x._cent("") is None

    @pytest.mark.parametrize("text,promille", [
        ("19.00", 190), ("7", 70), ("0.00", 0), ("19", 190),
    ])
    def test_steuersaetze(self, text, promille):
        assert x._promille(text) == promille

    @pytest.mark.parametrize("text,iso", [
        ("20260314", "2026-03-14"),
        ("2026-03-14", "2026-03-14"),
        ("2026-03-14T12:00:00", "2026-03-14"),
        ("2026-03-14+01:00", "2026-03-14"),
    ])
    def test_datumsformen(self, text, iso):
        assert x._datum(text) == iso

    def test_unbrauchbares_datum_gibt_none(self):
        assert x._datum("14.03.2026") is None
        assert x._datum("") is None


class TestSummenpruefung:
    def test_nicht_aufgehende_summen_werden_gemeldet(self):
        """Aus dem XML sind die Zahlen exakt. Geht die Summenlogik des
        DOKUMENTS nicht auf, will man das vor dem Buchen wissen."""
        roh = CII.read_bytes().replace(
            b"<ram:GrandTotalAmount>5.46</ram:GrandTotalAmount>",
            b"<ram:GrandTotalAmount>5.99</ram:GrandTotalAmount>",
        )
        gelesen = x.lies(roh)
        assert any("gehen nicht auf" in m for m in gelesen.meldungen)

    def test_mehrere_steuersaetze_lassen_den_satz_leer(self):
        """Lieber kein Wert als ein falscher."""
        roh = CII.read_bytes().replace(
            b"""      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>0.87</ram:CalculatedAmount>""",
            b"""      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>0.50</ram:CalculatedAmount>
        <ram:RateApplicablePercent>7.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>0.87</ram:CalculatedAmount>""",
        )
        gelesen = x.lies(roh)
        assert "ust_satz_promille" not in gelesen.felder
        assert any("Mehrere Steuersätze" in m for m in gelesen.meldungen)


class TestGutschriftImCii:
    def test_typecode_381_ist_keine_rechnung(self):
        """Eine ZUGFeRD-Gutschrift traegt positive Betraege. Gelesen wie eine
        Rechnung wuerde eine Ausgabe gebucht, wo Geld zurueckkam."""
        from app.logik.beleg import xml_leser
        from app.logik.beleg.ergebnis import BelegFehler

        daten = CII.read_bytes().replace(
            b"<ram:TypeCode>380</ram:TypeCode>", b"<ram:TypeCode>381</ram:TypeCode>", 1
        )
        assert daten != CII.read_bytes(), "Fixture muss den TypeCode 380 tragen"
        with pytest.raises(BelegFehler, match="Gutschrift"):
            xml_leser.lies(daten)

    def test_typecode_380_bleibt_eine_rechnung(self):
        from app.logik.beleg import xml_leser

        assert xml_leser.lies(CII.read_bytes()).wert("brutto") is not None

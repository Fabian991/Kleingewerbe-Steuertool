"""Von Text zu Feldern -- hier entscheidet sich die Qualitaet.

Der teuerste Fehler der Belegauslesung ist nicht ein nicht erkannter Betrag,
sondern ein **falsch erkannter**: eine Engine, die 99,9 % der Zeichen richtig
liest, liefert bei einem Dezimalzeichen am falschen Platz den zehnfachen
Betrag und meldet dafuer hohe Konfidenz. Deshalb pruefen die Tests hier vor
allem, dass die arithmetische Kreuzpruefung **nicht** durchwinkt.
"""

from datetime import date

import pytest

from app.logik.beleg import felder as f

DE_RECHNUNG = """Hetzner Online GmbH
Industriestr. 25, 91710 Gunzenhausen
USt-IdNr.: DE 812871812

Rechnung
Rechnungsnummer: R0012345678
Rechnungsdatum: 14.03.2026

Pos  Bezeichnung           Menge  Einzelpreis   Betrag
1    Cloud Server CX22         1     4,59 EUR   4,59 EUR
2    Zusatz-IPv4               2     1,19 EUR   2,38 EUR

Nettobetrag                                     6,97 EUR
Umsatzsteuer 19 %                               1,32 EUR
Gesamtbetrag                                    8,29 EUR
Zahlbar bis 28.03.2026
"""

EN_RECHNUNG = """Amazon Web Services EMEA SARL
VAT Number: LU26888617

Invoice
Invoice Number: EUINDE26-1234567
Invoice Date: March 3, 2026

Description                    Quantity   Amount in USD
Amazon Simple Storage Service     1,024       $1,180.00
AWS Data Transfer                    98          $54.56

Subtotal                                      $1,234.56
VAT (0%)                                           $0.00
Total amount due                               $1,234.56

VAT reverse charged. Article 196 of Council Directive 2006/112/EC.
"""


class TestZahlenformat:
    @pytest.mark.parametrize("text,erwartet", [
        ("Gesamtbetrag 1.234,56 EUR", "DE"),
        ("Total: 1,234.56 USD", "EN"),
        ("Betrag 45,00 EUR", "DE"),
        ("Amount 45.00 USD", "EN"),
        ("Summe 1.234.567,89", "DE"),
        ("Total 1,234,567.89", "EN"),
    ])
    def test_erkennung(self, text, erwartet):
        assert f.erkenne_format(text)[0] == erwartet

    def test_ganze_belege(self):
        """Entschieden wird je BELEG, nicht je Waehrung: AWS rechnet in Euro
        und schreibt trotzdem 1,234.56."""
        assert f.erkenne_format(DE_RECHNUNG)[0] == "DE"
        assert f.erkenne_format(EN_RECHNUNG)[0] == "EN"

    def test_bei_gleichstand_gewinnt_deutsch(self):
        """Ein deutscher Beleg mit falschem Betrag faellt beim Vergleich mit
        dem Kontoauszug auf, ein englischer eher nicht."""
        assert f.erkenne_format("45,00 und 45.00")[0] == "DE"

    def test_ohne_zahlen_gewinnt_deutsch(self):
        assert f.erkenne_format("Keine Zahlen hier")[0] == "DE"

    @pytest.mark.parametrize("roh,format_,cent", [
        ("1.234,56", "DE", 123456),
        ("1,234.56", "EN", 123456),
        ("45,00", "DE", 4500),
        ("45.00", "EN", 4500),
        ("1.234", "DE", 123400),      # Tausendertrenner, nicht 1,234
        ("1,234", "EN", 123400),
        ("8,29", "DE", 829),
        ("0,00", "DE", 0),
        ("1 234,56", "DE", 123456),   # Leerzeichen als Tausendertrenner
    ])
    def test_umwandlung(self, roh, format_, cent):
        assert f.nach_cent(roh, format_) == cent

    def test_der_stille_fehler(self):
        """1,234.56 mit deutschen Regeln gelesen ergibt 1,23.

        Das ist der Fehler, den eine hohe Konfidenz nicht faengt -- und genau
        deshalb wird das Format je Beleg entschieden und nicht geraten.
        """
        assert f.nach_cent("1,234.56", "DE") != 123456
        assert f.nach_cent("1,234.56", "EN") == 123456

    def test_kein_betrag_gibt_none(self):
        assert f.nach_cent("abc", "DE") is None
        assert f.nach_cent("", "DE") is None


class TestZahlenAbTausend:
    """Betraege ab 1.000 ohne Tausendertrenner -- auf Rechnungen der Normalfall."""

    @pytest.mark.parametrize(
        ("zeile", "cent"),
        [
            ("Gesamtbetrag 1250,00 EUR", 125000),
            ("Gesamtbetrag 12345,67 EUR", 1234567),
            ("Gesamtbetrag 1.250,00 EUR", 125000),
            ("Gesamtbetrag 1 250,00 EUR", 125000),
            ("Gesamtbetrag 4,59 EUR", 459),
        ],
    )
    def test_betrag_bleibt_ganz(self, zeile, cent):
        """Vorher zerlegte das Muster "1250,00" in "125" und "0,00" -- der
        Beleg ueber 1.250 Euro stand mit 1,25 Euro da."""
        betraege = f.sammle_betraege(zeile, f.Zahlenformat.DE)
        assert [b.cent for b in betraege] == [cent]


class TestDatumIstKeinDezimalpunkt:
    def test_deutsche_datumsangaben_machen_den_beleg_nicht_englisch(self):
        """"05.10.2026" enthaelt "5.10" -- das sah aus wie ein englischer
        Dezimalpunkt. Zwei Datumsangaben gegen einen Betrag "45,00": der Beleg
        galt als englisch, 45,00 wurde zu 4.500,00 gelesen."""
        text = "Rechnungsdatum 05.10.2026\nLeistungsdatum 01.10.2026\nGesamtbetrag 45,00 EUR"
        assert f.erkenne_format(text)[0] == f.Zahlenformat.DE
        assert f.bewerte_betraege(text, f.Zahlenformat.DE).brutto.cent == 4500

    def test_echte_englische_betraege_bleiben_englisch(self):
        text = "Invoice date 10/05/2026\nTotal amount due 45.00 USD\nSubtotal 45.00 USD"
        assert f.erkenne_format(text)[0] == f.Zahlenformat.EN

    def test_jahreszahl_nach_dem_punkt_zaehlt_nicht(self):
        assert f.erkenne_format("Stand 10.2026, Betrag 45,00")[0] == f.Zahlenformat.DE


class TestKreuzpruefung:
    def _betraege(self, *cent):
        return [f.Betrag(c, str(c), f"Zeile {c}", "DE") for c in cent]

    def test_aufgehendes_tripel(self):
        tripel, meldungen = f.kreuzpruefung(self._betraege(697, 132, 829))
        assert meldungen == ()
        assert (tripel.netto.cent, tripel.ust.cent, tripel.brutto.cent) == (697, 132, 829)
        assert tripel.satz_promille == 190

    def test_sieben_prozent(self):
        tripel, _ = f.kreuzpruefung(self._betraege(10000, 700, 10700))
        assert tripel.satz_promille == 70

    def test_ust_muss_selbst_im_text_stehen(self):
        """DIE entscheidende Bedingung.

        Rechnet man die Steuer als brutto minus netto aus, ist
        'netto + ust == brutto' tautologisch wahr und die ganze Pruefung
        wertlos. Hier fehlen die 132 im Text -- also kein Treffer.
        """
        tripel, meldungen = f.kreuzpruefung(self._betraege(697, 829, 1000))
        assert tripel is None
        assert any("geht auf" in m for m in meldungen)

    def test_satz_muss_passen(self):
        """100 + 18 = 118 geht auf, 18 sind aber nicht 19 Prozent von 100."""
        tripel, meldungen = f.kreuzpruefung(self._betraege(10000, 1800, 11800))
        assert tripel is None
        assert meldungen

    def test_ein_cent_toleranz(self):
        """Viele Rechnungen runden je Position und summieren danach."""
        # 19 % von 697 sind 132,43 -> 132. Mit 133 im Text bleibt es gueltig.
        tripel, _ = f.kreuzpruefung(self._betraege(697, 133, 830))
        assert tripel is not None

    def test_zwei_cent_sind_zu_viel(self):
        tripel, _ = f.kreuzpruefung(self._betraege(697, 134, 831))
        assert tripel is None

    def test_mehrere_lesarten_waehlen_keine(self):
        """Eine Auswahl waere geraten. Lieber manuelle Pruefung erzwingen."""
        # 100+19=119 und 200+38=238 gehen beide auf.
        tripel, meldungen = f.kreuzpruefung(self._betraege(10000, 1900, 11900,
                                                           20000, 3800, 23800))
        assert tripel is None
        assert any("Mehr als eine Lesart" in m for m in meldungen)

    def test_steuersatz_null_wird_nicht_geprueft(self):
        """Bei 0 Prozent ist netto + 0 == brutto fuer JEDEN Betrag wahr.

        Wuerde die Pruefung das mitnehmen, ginge jede einzelne Zahl des Belegs
        als Tripel durch -- die Pruefung waere nicht nur wertlos, sondern
        irrefuehrend.
        """
        tripel, _ = f.kreuzpruefung(self._betraege(1500, 123456, 99999))
        assert tripel is None

    def test_zu_wenige_betraege(self):
        tripel, meldungen = f.kreuzpruefung(self._betraege(829, 697))
        assert tripel is None
        assert any("Zu wenige" in m for m in meldungen)

    def test_doppelte_fundstellen_sind_kein_zweites_tripel(self):
        """Zwischensumme und Gesamtbetrag stehen oft beide im Text."""
        tripel, meldungen = f.kreuzpruefung(self._betraege(697, 697, 132, 829, 829))
        assert tripel is not None
        assert meldungen == ()


class TestBetragsurteil:
    def test_deutscher_beleg_geht_auf(self):
        urteil = f.bewerte_betraege(DE_RECHNUNG)
        assert urteil.sicher
        assert (urteil.brutto.cent, urteil.netto.cent, urteil.ust.cent) == (829, 697, 132)
        assert urteil.satz_promille == 190
        assert "Arithmetik geht auf" in urteil.nachweis

    def test_reverse_charge_beleg_ist_steuerfrei(self):
        """Arithmetisch ist hier nichts zu pruefen -- es gibt keine Steuer."""
        urteil = f.bewerte_betraege(EN_RECHNUNG)
        assert urteil.sicher
        assert urteil.brutto.cent == 123456
        assert urteil.netto.cent == 123456
        assert urteil.satz_promille == 0
        assert any("13b" in m for m in urteil.meldungen)

    def test_reverse_charge_mit_einer_summenzeile_ist_nicht_sicher(self):
        """Eine einzelne bezeichnete Zeile ist ein Fund, kein Nachweis."""
        urteil = f.bewerte_betraege(
            "Reverse charge\nTotal amount due 1,234.56 EUR", f.Zahlenformat.EN,
            reverse_charge=True,
        )
        assert urteil.brutto.cent == 123456
        assert urteil.sicher is False
        assert any("nicht gegengeprüft" in m for m in urteil.meldungen)

    def test_nicht_aufgehender_beleg_bleibt_unsicher(self):
        urteil = f.bewerte_betraege(
            "Rechnung\nNettobetrag 100,00 EUR\nUmsatzsteuer 18,00 EUR\n"
            "Gesamtbetrag 119,00 EUR\n"
        )
        assert not urteil.sicher
        assert urteil.brutto.cent == 11900
        assert any("nicht auf" in m for m in urteil.meldungen)

    def test_ohne_bezeichneten_gesamtbetrag_gar_nichts(self):
        urteil = f.bewerte_betraege("Irgendein Text ohne Betraege")
        assert urteil.brutto is None
        assert not urteil.sicher

    def test_englischer_beleg_mit_steuer(self):
        text = ("Invoice\nSubtotal $100.00\nVAT 19% $19.00\n"
                "Total amount due $119.00\n")
        urteil = f.bewerte_betraege(text)
        assert urteil.sicher
        assert urteil.brutto.cent == 11900
        assert urteil.satz_promille == 190

    def test_summenzeile_gewinnt_gegen_positionszeile(self):
        """Dasselbe Wort steht oft auch in den Positionen -- unten gilt."""
        text = ("Pos 1 Beratung Betrag 50,00 EUR\n"
                "Pos 2 Beratung Betrag 50,00 EUR\n"
                "Gesamtbetrag 100,00 EUR\n")
        betrag = f.finde_betrag(text, f.BRUTTO_WOERTER, "DE")
        assert betrag.cent == 10000


class TestReverseCharge:
    @pytest.mark.parametrize("text", [
        "Reverse Charge Verfahren",
        "Steuerschuldnerschaft des Leistungsempfängers",
        "VAT reverse charged",
        "Article 196 of Council Directive",
        "Umkehr nach § 13b UStG",
        "REVERSE-CHARGE",
    ])
    def test_marker(self, text):
        treffer, stelle = f.finde_reverse_charge(text)
        assert treffer
        assert stelle

    def test_normaler_beleg_ohne_marker(self):
        assert f.finde_reverse_charge(DE_RECHNUNG)[0] is False

    def test_awsbeleg_mit_marker(self):
        """Fuer einen IT-Dienstleister der Normalfall, nicht der Rand."""
        assert f.finde_reverse_charge(EN_RECHNUNG)[0] is True


class TestUstIdNr:
    @pytest.mark.parametrize("text,erwartet", [
        ("USt-IdNr.: DE812871812", "DE812871812"),
        ("USt-IdNr.: DE 812 871 812", "DE812871812"),
        ("VAT Number: LU26888617", "LU26888617"),
        ("VAT: IE3623748GH", "IE3623748GH"),
        ("P.IVA IT12345678901", "IT12345678901"),
        ("BTW: NL123456789B01", "NL123456789B01"),
    ])
    def test_erkennung(self, text, erwartet):
        assert f.finde_ust_idnr(text) == erwartet

    def test_naechste_zeile_bricht_den_treffer_nicht(self):
        """Zieht man alle Zeilenumbrueche zusammen, haengt die Nummer am
        naechsten Wort und die Wortgrenze greift nicht mehr -- ein Fehler, der
        nichts findet und dabei aussieht wie 'nichts da'."""
        assert f.finde_ust_idnr("USt-IdNr.: DE812871812\nRechnung") == "DE812871812"

    def test_eigene_nummer_wird_uebersprungen(self):
        """Auf einem Beleg stehen zwei: die eigene und die des Lieferanten."""
        text = "Lieferant: LU26888617\nLeistungsempfaenger: DE999999999"
        assert f.finde_ust_idnr(text, ausser="DE999999999") == "LU26888617"

    def test_alle_nummern_in_lesereihenfolge_ohne_doppelte(self):
        text = "Verkäufer DE 812 871 812\nUSt-IdNr. DE812871812\nZahlungsdienst LU26888617"
        assert f.alle_ust_idnrn(text) == ["DE812871812", "LU26888617"]

    def test_keine_steuernummer(self):
        """201/123/45678 ist eine Steuernummer und keine USt-IdNr."""
        assert f.finde_ust_idnr("Steuernummer: 201/123/45678") is None


class TestDatum:
    @pytest.mark.parametrize("text,iso", [
        ("Rechnungsdatum: 14.03.2026", "2026-03-14"),
        ("Rechnungsdatum 2026-03-14", "2026-03-14"),
        ("Invoice Date: March 3, 2026", "2026-03-03"),
        ("Belegdatum 3. März 2026", "2026-03-03"),
        ("Datum: 14.3.26", "2026-03-14"),
        ("Invoice date 03/14/2026", None),   # mehrdeutig, deshalb kein Treffer
    ])
    def test_formen(self, text, iso):
        gefunden = f.finde_datum(text, heute=date(2026, 10, 12))[0]
        if iso is None:
            # 03/14/2026 waere als Tag 3, Monat 14 ungueltig -- also nichts.
            assert gefunden is None
        else:
            assert gefunden == iso

    def test_bezeichnetes_datum_gewinnt(self):
        """Neben dem Belegdatum steht oft ein Faelligkeitsdatum."""
        text = "Lieferdatum: 01.02.2026\nRechnungsdatum: 14.03.2026\nZahlbar bis 28.03.2026"
        assert f.finde_datum(text, heute=date(2026, 10, 12))[0] == "2026-03-14"

    def test_ohne_bezeichnung_das_fruehste(self):
        """Das Belegdatum ist das aeltere; Faelligkeit kommt spaeter."""
        text = "irgendwas 28.03.2026 und 14.03.2026"
        assert f.finde_datum(text, heute=date(2026, 10, 12))[0] == "2026-03-14"

    def test_zukunft_wird_verworfen(self):
        assert f.finde_datum("Datum: 14.03.2030", heute=date(2026, 10, 12))[0] is None

    def test_sehr_alte_zahl_wird_verworfen(self):
        """Dann ist es eine Kundennummer, kein Datum."""
        assert f.finde_datum("Datum: 01.01.1999", heute=date(2026, 10, 12))[0] is None

    def test_echte_belege(self):
        assert f.finde_datum(DE_RECHNUNG, heute=date(2026, 10, 12))[0] == "2026-03-14"
        assert f.finde_datum(EN_RECHNUNG, heute=date(2026, 10, 12))[0] == "2026-03-03"


class TestBelegnummer:
    @pytest.mark.parametrize("text,erwartet", [
        ("Rechnungsnummer: R0012345678", "R0012345678"),
        ("Rechnungs-Nr. 2026/0815", "2026/0815"),
        ("Invoice Number: EUINDE26-1234567", "EUINDE26-1234567"),
        ("Invoice No. ABC-123", "ABC-123"),
        ("Belegnummer 4711-XY", "4711-XY"),
    ])
    def test_erkennung(self, text, erwartet):
        assert f.finde_belegnummer(text)[0] == erwartet

    def test_nur_in_bezeichneten_zeilen(self):
        """Ohne diese Einschraenkung ist auf jedem Beleg irgendeine
        Zeichenkette 'die Nummer' -- eine IBAN, eine Artikelnummer."""
        assert f.finde_belegnummer("IBAN DE02120300000000202051")[0] is None
        assert f.finde_belegnummer("Artikel 998877-AB")[0] is None

    def test_echte_belege(self):
        assert f.finde_belegnummer(DE_RECHNUNG)[0] == "R0012345678"
        assert f.finde_belegnummer(EN_RECHNUNG)[0] == "EUINDE26-1234567"

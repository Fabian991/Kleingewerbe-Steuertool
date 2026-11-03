"""Die Anzeigefilter — gemeinsam von Oberfläche und Rechnungs-PDF benutzt.

Das Modul hatte bis hierher keinen Test. Es formatiert aber genau die Zahlen,
die auf einem versendeten, acht Jahre aufbewahrungspflichtigen Dokument landen:
Menge, Betrag, Datum, IBAN. Ein Fehler hier ist nicht zurückzunehmen, weil das
Papier schon beim Kunden liegt.
"""

import pytest
from jinja2 import Environment

from app import filter as f


class TestMenge:
    """Die Spalte führt TAUSENDSTEL, gedruckt wurden zwei Stellen."""

    @pytest.mark.parametrize(
        ("tausendstel", "erwartet"),
        [(250, "0,25"), (500, "0,50"), (1000, "1,00"), (1500, "1,50"), (8750, "8,75")],
    )
    def test_viertelstunden_bleiben_zweistellig(self, tausendstel, erwartet):
        """Der Normalfall: beim 15-Minuten-Takt ändert sich nichts."""
        assert f.menge(tausendstel) == erwartet

    def test_krumme_menge_wird_nicht_abgeschnitten(self):
        """Sonst stimmt die Rechnung auf dem Papier nicht.

        Sieben Minuten ungerundet sind 117 Tausendstel. Als „0,12" gedruckt
        ergäbe Menge × Einzelpreis einen anderen Betrag als den ausgewiesenen —
        wer nachrechnet, kommt auf eine andere Zahl und fragt zu Recht nach.
        """
        assert f.menge(117) == "0,117"
        assert f.menge(1333) == "1,333"

    def test_kleine_menge_wird_nicht_zu_null(self):
        """Eine Position ohne Menge ist nach § 34a Satz 1 Nr. 4 UStDV unzulässig."""
        assert f.menge(1) == "0,001"
        assert f.menge(5) == "0,005"

    def test_die_gedruckte_menge_traegt_den_betrag(self):
        """Gegenprobe mit echten Zahlen: Menge × Einzelpreis muss aufgehen."""
        from decimal import Decimal

        for tausendstel, einzelpreis_cent in ((117, 9000), (250, 12000), (1333, 8000)):
            gedruckt = Decimal(f.menge(tausendstel).replace(",", "."))
            assert gedruckt * einzelpreis_cent == Decimal(tausendstel) * einzelpreis_cent / 1000

    def test_leer_ist_null(self):
        assert f.menge(None) == "0,00"


class TestDatum:
    def test_iso_datum(self):
        assert f.datum_de("2026-03-02") == "02.03.2026"

    def test_echtes_datumsobjekt(self):
        from datetime import date

        assert f.datum_de(date(2026, 12, 31)) == "31.12.2026"

    def test_leer_ergibt_gedankenstrich(self):
        assert f.datum_de(None) == "—" and f.datum_de("") == "—"

    def test_zeitstempel_wird_gekuerzt(self):
        """Keine Spalte wird heute so benutzt — aber ein Anzeigefilter, der
        bei einem Zeitstempel wirft, reisst die ganze Seite mit."""
        assert f.datum_de("2026-03-02T14:20:52+00:00") == "02.03.2026"

    def test_unlesbares_reisst_die_seite_nicht_mit(self):
        """Der Rohwert macht den Fehler sichtbar, ohne alles unerreichbar zu machen."""
        assert f.datum_de("gestern") == "gestern"
        assert f.datum_de("31.12.2026") == "31.12.2026"


class TestIban:
    def test_vierergruppen(self):
        assert f.iban("DE02120300000000202051") == "DE02 1203 0000 0000 2020 51"

    def test_vorhandene_leerzeichen_werden_neu_gesetzt(self):
        assert f.iban("de02 1203 0000 0000 2020 51") == "DE02 1203 0000 0000 2020 51"

    def test_leer_bleibt_leer(self):
        assert f.iban(None) == "" and f.iban("") == ""


class TestEinheit:
    def test_bekannte_codes(self):
        assert f.einheit_de("HUR") == "Std."
        assert f.einheit_de("LS") == "pausch."

    def test_unbekanntes_bleibt_stehen(self):
        """Lieber ein roher Code als eine leere Spalte auf der Rechnung."""
        assert f.einheit_de("KGM") == "KGM"
        assert f.einheit_de(None) == ""


class TestRegistrierung:
    def test_alle_filter_haengen_an_der_umgebung(self):
        """Ein Filter, der nicht registriert ist, bricht erst beim Rendern —
        und beim PDF heisst das: mitten im Festschreiben."""
        env = Environment()
        f.registriere(env)
        erwartet = {
            "euro", "dauer", "dezimalstunden", "datum_de",
            "menge", "einheit_de", "iban", "dateigroesse",
        }
        assert erwartet <= set(env.filters)

    def test_oberflaeche_und_pdf_formatieren_gleich(self):
        """Zwei Sätze Filter wären der sichere Weg zu zwei Schreibweisen.

        Geprüft wird deshalb, dass die PDF-Vorlage dieselbe Registrierung
        benutzt wie die Oberfläche — nicht eine eigene Kopie.
        """
        from pathlib import Path

        werk = Path(__file__).resolve().parent.parent / "app" / "pdfwerk.py"
        assert "from app.filter import registriere" in werk.read_text(encoding="utf-8")

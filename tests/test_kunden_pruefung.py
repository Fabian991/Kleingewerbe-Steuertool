"""Gemeinsame Pruefung des Kundenstamms: Formular und Import pruefen gleich.

Je Befund der Leser eine Regressionspruefung: alle Fehler je Feld statt nur
des ersten, Leerraum in einzeiligen Feldern, die vierstellige deutsche PLZ,
die ROT-Regeln des Festschreibens schon beim Anlegen und die Kundennummer
ueber den Zaehler ("K-1" ist "K-0001").
"""

import pytest

from app.logik import kunden as stamm

INLAND = {"name": "Beispiel AG", "strasse": "Musterweg 3", "plz": "01067", "ort": "Dresden"}


def _kunde(conn, **werte) -> int:
    zeile = {"kundennummer": "K-0001", "name": "Müller GmbH", "strasse": "Musterstraße 1",
             "plz": "10115", "ort": "Berlin", "angelegt_am": "2026-01-01"} | werte
    return conn.execute(
        f"INSERT INTO kunde ({', '.join(zeile)}) VALUES ({', '.join('?' * len(zeile))})",
        tuple(zeile.values())).lastrowid


class TestPruefeKunde:
    def test_alle_fehler_je_feld(self):
        """Das Formular zeigte nur den ersten Fehler; der Import braucht alle."""
        _, fehler = stamm.pruefe_kunde({
            "typ": "UNTERNEHMER", "leistungsort": "INLAND", "gruppierung": "PRO_TITEL",
            "name": "X", "email": "keine", "satz_euro": "0", "zahlungsziel": "400",
            "land": "Mars"})
        assert set(fehler) == {"strasse", "plz", "ort", "email", "satz_euro",
                               "zahlungsziel", "land"}
        assert "§ 34a Satz 1 Nr. 1 UStDV" in fehler["strasse"]

    def test_klartext_und_code(self):
        """Typ, Leistungsort und Gruppierung als Code oder in jeder Schreibweise des Werkzeugs."""
        for text in ("EU_B2B_REVERSE_CHARGE", "EU B2B, Reverse Charge", "EU-B2B, Reverse Charge",
                     "EU-Unternehmer (Reverse Charge)", "eu-unternehmer  (reverse charge)"):
            werte, fehler = stamm.pruefe_kunde({**INLAND, "land": "AT", "plz": "1010",
                                                "typ": "Unternehmer", "leistungsort": text,
                                                "gruppierung": "je Titel",
                                                "ust_idnr": "ATU12345678"})
            assert not fehler, text
            assert werte["leistungsort"] == "EU_B2B_REVERSE_CHARGE"
            assert werte["gruppierung_default"] == "PRO_TITEL"

    def test_zeilenumbruch_im_namen_wird_zusammengezogen(self):
        """Ein Name „F\\r\\nZweite Zeile“ brach auf dem PDF mitten in der Anschrift um."""
        werte, _ = stamm.pruefe_kunde({**INLAND, "name": "Beispiel\r\n  GmbH ", "typ": "PRIVAT",
                                       "leistungsort": "INLAND", "gruppierung": "PRO_TAG",
                                       "notiz": "Zeile 1\nZeile 2"})
        assert werte["name"] == "Beispiel GmbH"
        assert werte["notiz"] == "Zeile 1\nZeile 2"

    def test_vierstellige_deutsche_plz(self):
        """1067 ist vermutlich 01067 -- gesagt, nicht geraten."""
        _, fehler = stamm.pruefe_kunde({**INLAND, "plz": "1067", "typ": "UNTERNEHMER",
                                        "leistungsort": "INLAND", "gruppierung": "PRO_TITEL"})
        assert "01067" in fehler["plz"]
        werte, fehler = stamm.pruefe_kunde({**INLAND, "plz": "1010", "land": "AT",
                                            "typ": "PRIVAT", "leistungsort": "INLAND",
                                            "gruppierung": "PRO_TITEL"})
        assert not fehler and werte["plz"] == "1010"

    @pytest.mark.parametrize(("werte", "feld", "text"), [
        ({"land": "AT", "typ": "UNTERNEHMER", "leistungsort": "EU_B2B_REVERSE_CHARGE",
          "ust_idnr": None}, "ust_idnr", "USt-IdNr. des Kunden Pflicht"),
        ({"land": "AT", "typ": "PRIVAT", "leistungsort": "EU_B2B_REVERSE_CHARGE",
          "ust_idnr": "ATU12345678"}, "leistungsort", "Privatperson"),
        ({"land": "FR", "typ": "UNTERNEHMER", "leistungsort": "INLAND", "ust_idnr": None},
         "leistungsort", "Frankreich"),
    ])
    def test_querpruefung_wie_beim_festschreiben(self, werte, feld, text):
        """R18, R21, R20 -- schon beim Anlegen, nicht erst beim Festschreiben."""
        assert text in stamm.querpruefung(werte)[feld]

    def test_querpruefung_laesst_richtiges_durch(self):
        assert stamm.querpruefung({"land": "AT", "typ": "PRIVAT", "leistungsort": "INLAND",
                                   "ust_idnr": None}) == {}
        assert stamm.querpruefung({"land": "DE", "typ": "UNTERNEHMER",
                                   "leistungsort": "INLAND", "ust_idnr": None}) == {}


class TestKundennummer:
    def test_schreibweise_wird_normalisiert(self, conn, betrieb):
        assert stamm.pruefe_kundennummer(conn, "k-1") == "K-0001"
        assert stamm.pruefe_kundennummer(conn, "K-00042") == "K-0042"

    def test_gleicher_zaehler_ist_belegt(self, conn, betrieb):
        """„K-1“ wurde neben „K-0001“ angenommen: zwei Kunden, eine Nummer."""
        _kunde(conn, kundennummer="K-0001")
        for eingabe in ("K-1", "K-001", "K-00001", "k-0001"):
            with pytest.raises(ValueError, match="schon an Müller GmbH vergeben"):
                stamm.pruefe_kundennummer(conn, eingabe)

    def test_geloeschte_nummer_wird_wieder_frei(self, conn, betrieb):
        """Die Nummer eines geloeschten Kunden stand auf keinem Beleg."""
        _kunde(conn, kundennummer="K-0001")
        zweiter = _kunde(conn, kundennummer="K-0002", name="Zweiter")
        assert stamm.naechste_kundennummer(conn) == "K-0003"
        conn.execute("DELETE FROM kunde WHERE id = ?", (zweiter,))
        assert stamm.naechste_kundennummer(conn) == "K-0002"

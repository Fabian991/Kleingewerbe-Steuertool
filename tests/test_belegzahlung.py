"""Die Zahlung eines Belegs korrigieren (app/logik/beleg/belegzahlung.py).

Fuer die EUeR zaehlt der Tag des Abflusses (§ 11 Abs. 2 EStG). Ein falscher
Zahltag verschiebt die Ausgabe ins falsche Jahr; ein Beleg, der doch nicht
bezahlt war, steht als Ausgabe da. Beides laesst sich jetzt berichtigen --
mit altem Wert im Journal, und bei Fremdwaehrung mit dem Kurs, der zum
neuen Stichtag gehoert.
"""

import json
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import euer
from app.main import erzeuge_app
from tests.test_belegordner import cii
from tests.test_fremdwaehrung import (
    AWS_20_MAERZ_CENT,
    AWS_EZB_CENT,
    EINRICHTUNG,
    _aws_buchen,
    tabelle_ablegen,
)


@pytest.fixture
def klient(tmp_path):
    """Eingerichtet, mit der Pruefdatei der EZB-Kurse im Datenordner."""
    tabelle_ablegen(tmp_path)
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


def _meldung(antwort) -> tuple[str, str]:
    frage = parse_qs(urlparse(antwort.headers["location"]).query)
    for art in ("fehler", "warnung", "erfolg"):
        if art in frage:
            return art, frage[art][0]
    return "", ""


def _buchen(klient, datenbank, *, nummer="RE-2026-0815", bezahlt_am="2026-03-20"):
    """Eine Rechnung in Euro (5,46 €), bezahlt am ``bezahlt_am``."""
    antwort = klient.post("/belege/hochladen",
                          files={"datei": ("rechnung.xml", cii(nummer), "application/xml")},
                          follow_redirects=False)
    name = antwort.headers["location"].rsplit("/", 1)[1]
    with datenbank() as conn:
        hosting = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel = 'HOSTING'"
                               ).fetchone()[0]
    klient.post(f"/belege/pruefen/{name}", data={
        "belegdatum": "2026-03-14", "brutto_euro": "5,46", "waehrung": "EUR",
        "belegnummer": nummer, "lieferant_neu": "Beispiel GmbH", "kategorie_id": str(hosting),
        "quote": "100", "bezahlt_am": bezahlt_am, "trotzdem": "1"})


def _zahlungen(datenbank) -> list[tuple[str, int]]:
    with datenbank() as conn:
        return [tuple(z) for z in conn.execute(
            "SELECT datum, betrag_cent FROM zahlung WHERE beleg_id IS NOT NULL ORDER BY id")]


def _ausgaben(tmp_path, jahr: int) -> int:
    conn = db.oeffne(tmp_path / "steuer.sqlite")
    try:
        return euer.auswertung(conn, jahr).ausgaben_cent
    finally:
        conn.close()


def _beleg(datenbank, beleg_id=1) -> sqlite3.Row:
    with datenbank() as conn:
        return conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()


def _zeile(klient, beleg_id=1) -> str:
    return re.search(rf'<tr id="beleg-{beleg_id}">.*?</tr>', klient.get("/belege").text,
                     re.S).group(0)


class TestZahltagAendern:
    def test_verschiebt_die_ausgabe_ins_richtige_jahr(self, klient, datenbank, tmp_path):
        _buchen(klient, datenbank)
        assert (_ausgaben(tmp_path, 2026), _ausgaben(tmp_path, 2025)) == (546, 0)
        zeile = _zeile(klient)
        assert 'action="/belege/1/zahltag"' in zeile and 'value="2026-03-20"' in zeile
        antwort = klient.post("/belege/1/zahltag", data={"datum": "31.12.2025"},
                              follow_redirects=False)
        art, text = _meldung(antwort)
        assert art == "warnung"
        assert text.startswith("Beleg 1: Zahltag von 20.03.2026 auf 31.12.2025 geändert. Die "
                               "Ausgabe zählt jetzt in der EÜR 2025 statt 2026.")
        assert "Achtung: das ändert die EÜR 2025 — ist die Erklärung schon abgegeben" in text
        assert (_ausgaben(tmp_path, 2026), _ausgaben(tmp_path, 2025)) == (0, 546)
        assert _zahlungen(datenbank) == [("2025-12-31", -546)]
        with datenbank() as conn:
            eintrag = conn.execute("SELECT * FROM journal WHERE aktion = 'AENDERN'"
                                   " AND objekttyp = 'zahlung'").fetchone()
        assert (eintrag["feld"], eintrag["wert_alt"], eintrag["wert_neu"]) == \
            ("datum", "2026-03-20", "2025-12-31")
        assert eintrag["begruendung"] == "Zahltag von Beleg 1 korrigiert"

    def test_im_laufenden_jahr_ohne_warnung(self, klient, datenbank):
        _buchen(klient, datenbank)
        antwort = klient.post("/belege/1/zahltag", data={"datum": "2026-03-21"},
                              follow_redirects=False)
        assert _meldung(antwort) == ("erfolg", "Beleg 1: Zahltag von 20.03.2026 auf "
                                               "21.03.2026 geändert.")

    def test_derselbe_tag_aendert_nichts(self, klient, datenbank):
        _buchen(klient, datenbank)
        antwort = klient.post("/belege/1/zahltag", data={"datum": "2026-03-20"},
                              follow_redirects=False)
        assert _meldung(antwort) == ("erfolg", "Beleg 1: die Zahlung steht schon am 20.03.2026 "
                                               "— nichts geändert.")
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'AENDERN'"
                                ).fetchone()[0] == 0

    def test_zukunft_und_unsinn_abgewiesen(self, klient, datenbank):
        _buchen(klient, datenbank)
        for datum in ("2099-01-01", "gestern", ""):
            art, _ = _meldung(klient.post("/belege/1/zahltag", data={"datum": datum},
                                          follow_redirects=False))
            assert art == "fehler", datum
        assert _zahlungen(datenbank) == [("2026-03-20", -546)]

    def test_nur_bei_genau_einer_zahlung(self, klient, datenbank):
        """Bei Raten ist nicht klar, welche gemeint ist."""
        _buchen(klient, datenbank, bezahlt_am="")
        with datenbank() as conn:
            for datum in ("2026-03-20", "2026-04-20"):
                conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                             " VALUES (1, ?, -273, 'UEBERWEISUNG')", (datum,))
        assert "2 Zahlungen erfasst" in _zeile(klient)
        for pfad, daten in (("zahltag", {"datum": "2026-03-21"}),
                            ("zahlung-zuruecknehmen", {})):
            art, text = _meldung(klient.post(f"/belege/1/{pfad}", data=daten,
                                             follow_redirects=False))
            assert art == "fehler" and "2 Zahlungen" in text
        assert len(_zahlungen(datenbank)) == 2

    def test_ohne_zahlung(self, klient, datenbank):
        _buchen(klient, datenbank, bezahlt_am="")
        assert 'action="/belege/1/zahltag"' not in _zeile(klient)
        art, text = _meldung(klient.post("/belege/1/zahltag", data={"datum": "2026-03-21"},
                                         follow_redirects=False))
        assert (art, text) == ("fehler", "Zu Beleg 1 ist keine Zahlung erfasst.")


class TestZahlungZuruecknehmen:
    def test_beleg_ist_danach_offen(self, klient, datenbank, tmp_path):
        _buchen(klient, datenbank)
        antwort = klient.post("/belege/1/zahlung-zuruecknehmen", follow_redirects=False)
        assert _meldung(antwort) == (
            "erfolg", "Beleg 1: Zahlung vom 20.03.2026 zurückgenommen — der Beleg ist wieder "
                      "offen und zählt in keinem EÜR-Jahr, bis die Zahlung erfasst ist.")
        assert _zahlungen(datenbank) == []
        assert _ausgaben(tmp_path, 2026) == 0
        zeile = _zeile(klient)
        assert ">offen<" in zeile and 'action="/belege/1/bezahlt"' in zeile
        with datenbank() as conn:
            eintrag = conn.execute("SELECT * FROM journal WHERE aktion = 'ZAHLUNG_ZURUECK'"
                                   ).fetchone()
        alt = json.loads(eintrag["wert_alt"])
        assert (alt["beleg_id"], alt["datum"], alt["betrag_cent"], alt["art"]) == \
            (1, "2026-03-20", -546, "UEBERWEISUNG")
        # Und neu erfasst geht.
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-25"})
        assert _zahlungen(datenbank) == [("2026-03-25", -546)]

    def test_zwei_schritte_mit_grund_im_protokoll(self, klient, datenbank):
        """Wie bei der Rechnungszahlung: aufklappen, dann bestaetigen -- kein
        einzelner Knopf direkt unter "Zahltag ändern". Der Grund ist
        freiwillig und steht in der Begruendung des Journaleintrags."""
        _buchen(klient, datenbank)
        zeile = _zeile(klient)
        dialog = re.search(r'<details class="loeschen zahlung-zurueck">.*?</details>', zeile,
                           re.S).group(0)
        assert "<summary>Zahlung zurücknehmen …</summary>" in dialog
        assert 'action="/belege/1/zahlung-zuruecknehmen"' in dialog
        assert 'name="grund"' in dialog and "Ja, Zahlung zurücknehmen" in dialog
        # Ausserhalb des Dialogs gibt es keinen Knopf, der die Zahlung entfernt.
        assert zeile.count('action="/belege/1/zahlung-zuruecknehmen"') == 1
        assert "EÜR 2026" not in dialog  # laufendes Jahr: keine Warnung
        klient.post("/belege/1/zahlung-zuruecknehmen", data={"grund": "  doppelt  erfasst "})
        with datenbank() as conn:
            begruendung = conn.execute("SELECT begruendung FROM journal"
                                       " WHERE aktion = 'ZAHLUNG_ZURUECK'").fetchone()[0]
        assert begruendung == ("Zahlung zu Beleg 1 zurückgenommen — der Beleg ist wieder "
                               "offen: doppelt erfasst")

    def test_vorjahr_warnt_vor_dem_bestaetigen(self, klient, datenbank):
        _buchen(klient, datenbank, bezahlt_am="2025-12-30")
        dialog = re.search(r'<details class="loeschen zahlung-zurueck">.*?</details>',
                           _zeile(klient), re.S).group(0)
        assert "Die Zahlung steht in der EÜR 2025" in dialog

    def test_vorjahr_warnt(self, klient, datenbank):
        _buchen(klient, datenbank, bezahlt_am="2025-12-30")
        art, text = _meldung(klient.post("/belege/1/zahlung-zuruecknehmen",
                                         follow_redirects=False))
        assert art == "warnung" and "das ändert die EÜR 2025" in text

    def test_geloeschter_beleg(self, klient, datenbank):
        _buchen(klient, datenbank)
        klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"})
        art, text = _meldung(klient.post("/belege/1/zahlung-zuruecknehmen",
                                         follow_redirects=False))
        assert art == "fehler" and "gelöscht" in text
        assert _zahlungen(datenbank) == [("2026-03-20", -546)]


class TestFremdwaehrung:
    """Der Euro-Betrag folgt dem Stichtag: Kurs des neuen Zahltags, nach dem
    Zuruecknehmen wieder der des Belegdatums -- fuer Beleg UND Zahlung."""

    def test_neuer_zahltag_neuer_kurs(self, klient, datenbank, tmp_path):
        _aws_buchen(klient, datenbank)
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"})
        assert _beleg(datenbank)["betrag_brutto_cent"] == AWS_20_MAERZ_CENT
        # Zurueck auf das Belegdatum: dort gilt dessen Kurs, nicht der des
        # alten Zahltags -- die Abkuerzung der Bezahlt-Route gilt hier nicht.
        antwort = klient.post("/belege/1/zahltag", data={"datum": "2026-03-03"},
                              follow_redirects=False)
        art, text = _meldung(antwort)
        assert art == "erfolg"
        assert ("Für die EÜR auf den Zahlungstag umgerechnet: EZB-Kurs 1,1606 vom 03.03.2026 "
                "= 1.063,73 € (vorher 1.068,42 €).") in text
        b = _beleg(datenbank)
        assert (b["kurs"], b["kurs_datum"], b["betrag_brutto_cent"]) == \
            ("1.1606", "2026-03-03", AWS_EZB_CENT)
        assert _zahlungen(datenbank) == [("2026-03-03", -AWS_EZB_CENT)]
        assert _ausgaben(tmp_path, 2026) == AWS_EZB_CENT

    def test_zuruecknehmen_kehrt_zum_kurs_des_belegdatums_zurueck(self, klient, datenbank):
        _aws_buchen(klient, datenbank)
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"})
        antwort = klient.post("/belege/1/zahlung-zuruecknehmen", follow_redirects=False)
        assert ("Für die EÜR auf das Belegdatum umgerechnet: EZB-Kurs 1,1606 vom 03.03.2026 "
                "= 1.063,73 € (vorher 1.068,42 €).") in _meldung(antwort)[1]
        b = _beleg(datenbank)
        assert (b["kurs_datum"], b["betrag_brutto_cent"]) == ("2026-03-03", AWS_EZB_CENT)
        assert _zahlungen(datenbank) == []
        # Am Belegdatum neu bezahlt: derselbe Kurs, nichts umzurechnen.
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-03"})
        assert _zahlungen(datenbank) == [("2026-03-03", -AWS_EZB_CENT)]

    def test_kartenbetrag_bleibt(self, klient, datenbank):
        _aws_buchen(klient, datenbank, brutto_eur="1.080,00")
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"})
        klient.post("/belege/1/zahltag", data={"datum": "2026-03-10"})
        assert _beleg(datenbank)["betrag_brutto_cent"] == 108000
        assert _zahlungen(datenbank) == [("2026-03-10", -108000)]

    def test_anlagegut_bleibt_beim_kurs(self, klient, datenbank):
        _aws_buchen(klient, datenbank, anlagegut="1")
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"})
        antwort = klient.post("/belege/1/zahltag", data={"datum": "2026-03-10"},
                              follow_redirects=False)
        assert "Anlagegut: der Euro-Betrag bleibt beim Kurs vom 03.03.2026" in \
            _meldung(antwort)[1]
        assert _beleg(datenbank)["betrag_brutto_cent"] == AWS_EZB_CENT
        assert _zahlungen(datenbank) == [("2026-03-10", -AWS_EZB_CENT)]

    def test_ohne_kurs_des_neuen_tags_bleibt_alles(self, klient, datenbank):
        _aws_buchen(klient, datenbank)
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"})
        antwort = klient.post("/belege/1/zahltag", data={"datum": "2026-04-14"},
                              follow_redirects=False)
        art, text = _meldung(antwort)
        assert art == "fehler" and text.startswith("Zahltag nicht geändert")
        assert _zahlungen(datenbank) == [("2026-03-20", -AWS_20_MAERZ_CENT)]

    def test_teilzahlung_bleibt_beim_kurs(self, klient, datenbank):
        _aws_buchen(klient, datenbank)
        with datenbank() as conn:
            conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                         " VALUES (1, '2026-03-20', -50000, 'KARTE')")
        antwort = klient.post("/belege/1/zahltag", data={"datum": "2026-03-10"},
                              follow_redirects=False)
        assert "Teilzahlung" in _meldung(antwort)[1]
        assert _beleg(datenbank)["betrag_brutto_cent"] == AWS_EZB_CENT
        assert _zahlungen(datenbank) == [("2026-03-10", -50000)]

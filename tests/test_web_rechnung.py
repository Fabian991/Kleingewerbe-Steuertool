"""Der ganze Klickweg: Zeit erfassen, Rechnung erzeugen, festschreiben, bezahlen.

Das ist der Test, der zaehlt -- er laeuft ueber echte HTTP-Aufrufe durch
dieselben Routen wie der Browser.
"""

import re
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "inhaber": "E. Muster",
    "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
    "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
    # Ausdruecklich gesetzt: die Vorbelegung ist AUFWAERTS auf 15 Minuten,
    # diese Tests pruefen die kaufmaennische Variante.
    "rundung_modus": "KAUFMAENNISCH", "rundung_intervall_min": "15",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "email": "buchhaltung@mustermann.example", "typ": "UNTERNEHMER",
    "leistungsort": "INLAND", "satz_euro": "90,00", "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def daten(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
    return tmp_path / "daten"


@pytest.fixture
def klient(daten, monkeypatch):
    import importlib

    from app import db

    importlib.reload(db)
    with TestClient(erzeuge_app(daten / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _db(daten) -> sqlite3.Connection:
    conn = sqlite3.connect(daten / "steuer.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _zeiten(klient, anzahl=7, minuten="7", titel="Beratung", **rest):
    for i in range(anzahl):
        klient.post("/zeiten", data={
            "kunde_id": 1, "titel": titel, "datum": f"2026-03-{2 + i:02d}",
            "dauer": minuten, "beschreibung": rest.get("beschreibung", ""),
            "modus": rest.get("modus", "STUNDEN"), "grund": rest.get("grund", ""),
        })


def _entwurf(klient, daten, **rest) -> int:
    conn = _db(daten)
    ids = [z["id"] for z in conn.execute(
        "SELECT id FROM zeiteintrag WHERE rechnung_id IS NULL ORDER BY id")]
    conn.close()
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": 1, "datum": rest.get("datum", "2026-03-10"),
        "modus": rest.get("modus", "PRO_TITEL"),
        "einleitung": rest.get("einleitung", "vielen Dank für Ihren Auftrag."),
        "schluss": "", "einzelnachweis": rest.get("einzelnachweis", ""),
        "zeit": ids,
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    return int(antwort.headers["location"].rsplit("/", 1)[1])


def _festschreiben(klient, rid, daten):
    seite = klient.get(f"/rechnung/{rid}").text
    version = re.search(r'name="version" value="(\d+)"', seite).group(1)
    codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
    return klient.post(f"/rechnung/{rid}/festschreiben",
                       data={"version": version, "bestaetigt": codes},
                       follow_redirects=False)


class TestAssistent:
    def test_kundenliste_zeigt_offene_stunden(self, klient):
        _zeiten(klient, anzahl=3, minuten="1h")
        seite = klient.get("/rechnung/neu").text
        assert "Mustermann GmbH" in seite
        assert "3,00" in seite

    def test_zeitauswahl_zeigt_die_summe_jedes_modus(self, klient):
        """Damit das Umschalten der Gruppierung keine Ueberraschung ist."""
        _zeiten(klient)
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert "pro Titel" in seite and "pro Eintrag" in seite
        assert "67,50" in seite, "Sammel/pro Titel: 0,75 h x 90,00 €"
        assert "75,60" in seite, "Pro Eintrag rechnet auf die Minute"

    def test_ohne_offene_zeiten_sagt_es_das(self, klient):
        assert "keine Zeiten offen" in klient.get("/rechnung/neu?kunde=1").text

    def test_ohne_auswahl_wird_abgewiesen(self, klient):
        _zeiten(klient, anzahl=1)
        antwort = klient.post("/rechnung/neu", data={
            "kunde_id": 1, "datum": "2026-03-10", "modus": "PRO_TITEL"},
            follow_redirects=True)
        assert "keine Zeit ausgewählt" in antwort.text


class TestEntwurf:
    def test_entwurf_hat_positionen_aber_keine_nummer(self, klient, daten):
        rid = _entwurf(klient, daten) if _zeiten(klient) is None else None
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Rechnungsentwurf" in seite
        assert "Beratung" in seite
        assert "67,50" in seite
        conn = _db(daten)
        assert conn.execute("SELECT nummer FROM rechnung WHERE id=?", (rid,)).fetchone()[0] is None
        conn.close()

    def test_gruppierung_wechseln_erzeugt_neu(self, klient, daten):
        _zeiten(klient)
        rid = _entwurf(klient, daten)
        klient.post(f"/rechnung/{rid}/gruppierung", data={"modus": "PRO_EINTRAG"})
        seite = klient.get(f"/rechnung/{rid}").text
        assert "75,60" in seite
        conn = _db(daten)
        assert conn.execute(
            "SELECT COUNT(*) FROM rechnungsposition WHERE rechnung_id=?", (rid,)
        ).fetchone()[0] == 7
        conn.close()

    def test_verwerfen_gibt_zeiten_frei_ohne_nummernluecke(self, klient, daten):
        _zeiten(klient, anzahl=2)
        rid = _entwurf(klient, daten)
        klient.post(f"/rechnung/{rid}/verwerfen")
        conn = _db(daten)
        assert conn.execute("SELECT COUNT(*) FROM rechnung").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM zeiteintrag WHERE rechnung_id IS NULL").fetchone()[0] == 2
        conn.close()
        assert "Noch keine Rechnungen" in klient.get("/rechnungen").text


#: Ein Datum im naechsten Jahr: in der Zukunft (GELB), aber noch im Fenster.
#: "2099-01-01" ist seit der Datumsplausibilisierung kein Rechnungsdatum mehr.
ZUKUNFT = f"{date.today().year + 1}-01-01"


class TestPruefung:
    def test_zukunftsdatum_wird_gelb_und_muss_abgehakt_werden(self, klient, daten):
        """Mit einem Datum im naechsten Jahr statt 2099: ein Jahr jenseits des
        laufenden ist das Ende des plausiblen Fensters, 2099 wird abgewiesen."""
        _zeiten(klient, anzahl=1, minuten="1h")
        rid = _entwurf(klient, daten, datum=ZUKUNFT)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Zukunft" in seite
        # Ohne Abhaken wird abgelehnt.
        version = re.search(r'name="version" value="(\d+)"', seite).group(1)
        antwort = klient.post(f"/rechnung/{rid}/festschreiben",
                              data={"version": version}, follow_redirects=True)
        assert "nicht abgehakt" in antwort.text

    def test_fehlende_kundenanschrift_blockiert(self, klient, daten):
        """Ein roter Punkt: "1 Punkt blockiert", nicht "1 Punkte blockieren" --
        der Test prueft das Blockieren, und die alte Zusicherung hing am
        Pluralfehler bei Anzahl 1."""
        _zeiten(klient, anzahl=1, minuten="1h")
        rid = _entwurf(klient, daten)
        conn = _db(daten)
        conn.execute("UPDATE kunde SET strasse='' WHERE id=1")
        conn.commit()
        conn.close()
        seite = klient.get(f"/rechnung/{rid}").text
        assert "1 Punkt blockiert das Festschreiben" in seite
        assert "34a" in seite
        assert "disabled" in seite

    def test_zahlendreher_wird_gemeldet(self, klient, daten):
        """Der eigentliche Zweck: Vergleich mit dem, was bisher ueblich war.

        Die 40 Stunden kommen aus vier Eintraegen: ein einzelner Eintrag ueber
        24 Stunden wird von der Zeiterfassung inzwischen abgewiesen.
        """
        for _ in range(3):
            _zeiten(klient, anzahl=1, minuten="1h")
            _festschreiben(klient, _entwurf(klient, daten), daten)
        _zeiten(klient, anzahl=4, minuten="10h")
        rid = _entwurf(klient, daten)
        assert "Zahlendreher" in klient.get(f"/rechnung/{rid}").text

    def test_offene_zeiten_ausserhalb_der_auswahl_werden_gemeldet(self, klient, daten):
        _zeiten(klient, anzahl=3, minuten="1h")
        conn = _db(daten)
        ids = [z["id"] for z in conn.execute("SELECT id FROM zeiteintrag ORDER BY id")]
        conn.close()
        antwort = klient.post("/rechnung/neu", data={
            "kunde_id": 1, "datum": "2026-03-10", "modus": "PRO_TITEL",
            "einleitung": "", "schluss": "", "zeit": ids[:1]}, follow_redirects=False)
        rid = int(antwort.headers["location"].rsplit("/", 1)[1])
        assert "nicht abgerechnet" in klient.get(f"/rechnung/{rid}").text


class TestFestschreiben:
    def test_vollstaendiger_weg_bis_zum_pdf(self, klient, daten):
        _zeiten(klient, beschreibung="Konzept und Abstimmung")
        rid = _entwurf(klient, daten)
        assert _festschreiben(klient, rid, daten).status_code == 303

        seite = klient.get(f"/rechnung/{rid}").text
        assert "RE-2026-0001" in seite
        assert "PDF öffnen" in seite

        antwort = klient.get(f"/rechnung/{rid}/pdf")
        assert antwort.status_code == 200
        assert antwort.headers["content-type"] == "application/pdf"

        conn = _db(daten)
        pfad = conn.execute("SELECT pdf_pfad FROM rechnung WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        # Relativ zum Datenordner gespeichert -- so ueberlebt der Verweis einen
        # Umzug des Ordners auf einen anderen Rechner.
        assert Path(pfad).parent == Path("ausgangsrechnungen") / "2026"
        pfad = str(daten / pfad)
        text = subprocess.run(["pdftotext", "-layout", pfad, "-"],
                              capture_output=True, text=True).stdout
        assert "RE-2026-0001" in text
        assert "67,50" in text
        assert "§ 19" in text
        assert not re.search(r"(?<![\d,])0,00\s*€", text)

    def test_nach_dem_festschreiben_ist_der_entwurfsteil_weg(self, klient, daten):
        _zeiten(klient, anzahl=1, minuten="1h")
        rid = _entwurf(klient, daten)
        _festschreiben(klient, rid, daten)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Gruppierung ändern" not in seite
        assert "Verwerfen" not in seite
        assert "Stornieren" in seite

    def test_doppeltes_festschreiben_verbraucht_keine_zweite_nummer(self, klient, daten):
        _zeiten(klient, anzahl=1, minuten="1h")
        rid = _entwurf(klient, daten)
        seite = klient.get(f"/rechnung/{rid}").text
        version = re.search(r'name="version" value="(\d+)"', seite).group(1)
        codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
        erste = klient.post(f"/rechnung/{rid}/festschreiben",
                            data={"version": version, "bestaetigt": codes},
                            follow_redirects=False)
        zweite = klient.post(f"/rechnung/{rid}/festschreiben",
                             data={"version": version, "bestaetigt": codes},
                             follow_redirects=True)
        assert erste.status_code == 303
        assert "bereits festgeschrieben" in zweite.text or "geändert" in zweite.text
        conn = _db(daten)
        assert conn.execute(
            "SELECT COUNT(*) FROM rechnung WHERE nummer IS NOT NULL").fetchone()[0] == 1
        conn.close()


class TestZahlungUndStorno:
    def _gestellt(self, klient, daten):
        _zeiten(klient, anzahl=1, minuten="1h")
        rid = _entwurf(klient, daten)
        _festschreiben(klient, rid, daten)
        return rid

    def test_teilzahlung_dann_vollzahlung(self, klient, daten):
        rid = self._gestellt(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-20", "betrag_euro": "40,00", "art": "UEBERWEISUNG"})
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Teilbezahlt" in seite
        assert "50,00" in seite  # 90,00 - 40,00 offen
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-25", "betrag_euro": "", "art": "UEBERWEISUNG"})
        assert "Bezahlt" in klient.get(f"/rechnung/{rid}").text

    def test_zahlung_zaehlt_in_den_grenzwert(self, klient, daten):
        """§ 19 Abs. 2 UStG rechnet nach vereinnahmten Entgelten."""
        rid = self._gestellt(klient, daten)
        assert "0,00 €" in klient.get("/").text
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-20", "betrag_euro": "", "art": "UEBERWEISUNG"})
        assert "90,00 €" in klient.get("/").text

    def test_versandvermerk(self, klient, daten):
        rid = self._gestellt(klient, daten)
        klient.post(f"/rechnung/{rid}/versendet", data={"datum": "2026-03-11"})
        assert "Versendet am 11.03.2026" in klient.get(f"/rechnung/{rid}").text

    def test_storno_mit_freigabe_und_neuabrechnung(self, klient, daten):
        rid = self._gestellt(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/storno",
                              data={"grund": "Tippfehler", "zeiten_freigeben": "1"},
                              follow_redirects=False)
        storno_id = int(antwort.headers["location"].rsplit("/", 1)[1])
        storno = klient.get(f"/rechnung/{storno_id}").text
        assert "RE-2026-0002" in storno
        assert "Storno zu" in storno
        assert "Gutschrift" not in storno

        original = klient.get(f"/rechnung/{rid}").text
        assert "wurde storniert" in original

        # Die Zeit ist wieder abrechenbar.
        assert "Aufgeschrieben" in klient.get("/zeiten").text
        neu = _entwurf(klient, daten)
        assert _festschreiben(klient, neu, daten).status_code == 303
        assert "RE-2026-0003" in klient.get(f"/rechnung/{neu}").text

    def test_storno_ohne_grund_wird_abgewiesen(self, klient, daten):
        rid = self._gestellt(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/storno",
                              data={"grund": "", "zeiten_freigeben": "1"},
                              follow_redirects=True)
        assert "Begründung" in antwort.text

    def _bezahlt_und_storniert(self, klient, daten):
        rid = self._gestellt(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-20", "betrag_euro": "", "art": "UEBERWEISUNG"})
        antwort = klient.post(f"/rechnung/{rid}/storno",
                              data={"grund": "Falscher Kunde", "zeiten_freigeben": "0"},
                              follow_redirects=False)
        return rid, int(antwort.headers["location"].rsplit("/", 1)[1])

    def test_rueckzahlung_nach_storno_nimmt_den_umsatz_zurueck(self, klient, daten):
        """Das Storno allein nimmt das Geld nicht zurueck.

        Vorher gab es fuer eine bezahlte, dann stornierte Rechnung kein
        Formular mehr: 90 € standen dauerhaft im § 19-Umsatz und in der EUeR,
        obwohl sie laengst zurueckueberwiesen waren.
        """
        rid, storno_id = self._bezahlt_und_storniert(klient, daten)
        assert "90,00 €" in klient.get("/").text

        original = klient.get(f"/rechnung/{rid}").text
        assert "noch nicht zurückgezahlt" in original
        assert f'action="/rechnung/{rid}/rueckzahlung"' in original
        assert f'action="/rechnung/{rid}/zahlung"' not in original, "kein Eingang mehr"
        storno = klient.get(f"/rechnung/{storno_id}").text
        assert "Rückzahlung dort erfassen" in storno

        antwort = klient.post(f"/rechnung/{rid}/rueckzahlung",
                              data={"datum": "2026-03-28", "betrag_euro": "",
                                    "art": "UEBERWEISUNG"}, follow_redirects=False)
        assert antwort.status_code == 303 and "fehler" not in antwort.headers["location"]
        assert "0,00 €" in klient.get("/").text
        seite = klient.get(f"/rechnung/{rid}").text
        assert "vollständig zurückgezahlt" in seite
        assert "Rückzahlung" in seite
        conn = _db(daten)
        betraege = [z["betrag_cent"] for z in conn.execute(
            "SELECT betrag_cent FROM zahlung WHERE rechnung_id = ? ORDER BY id", (rid,))]
        conn.close()
        assert betraege == [9000, -9000]

    def test_rueckzahlung_hoechstens_das_eingegangene(self, klient, daten):
        rid, _ = self._bezahlt_und_storniert(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/rueckzahlung",
                              data={"datum": "2026-03-28", "betrag_euro": "200,00",
                                    "art": "UEBERWEISUNG"}, follow_redirects=True)
        # Echte Umlaute in der Meldung, wie ueberall sonst auf der Oberflaeche.
        assert "höchstens" in antwort.text
        assert "90,00 €" in klient.get("/").text, "nichts gebucht"

    def test_rueckzahlung_nur_auf_stornierte_rechnung(self, klient, daten):
        rid = self._gestellt(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/rueckzahlung",
                              data={"datum": "2026-03-28", "betrag_euro": "",
                                    "art": "UEBERWEISUNG"}, follow_redirects=True)
        assert "nur zu einer stornierten Rechnung" in antwort.text

    def test_veraltete_seite_bucht_den_rest_nicht_zweimal(self, klient, daten):
        """Der Doppelklick auf "Zahlung erfassen".

        Beide Anfragen tragen den Stand der Seite (0 € bezahlt). Die erste
        bucht den Rest, die zweite trifft auf 90 € bezahlt -- und muss
        abgewiesen werden, statt weitere 90 € zu buchen.
        """
        rid = self._gestellt(klient, daten)
        seite = klient.get(f"/rechnung/{rid}").text
        assert 'name="erwartet_bezahlt" value="0"' in seite
        daten_formular = {"datum": "2026-03-20", "betrag_euro": "", "art": "UEBERWEISUNG",
                          "erwartet_bezahlt": "0"}
        erste = klient.post(f"/rechnung/{rid}/zahlung", data=daten_formular,
                            follow_redirects=False)
        assert "fehler" not in erste.headers["location"]
        zweite = klient.post(f"/rechnung/{rid}/zahlung", data=daten_formular,
                             follow_redirects=True)
        assert "nicht mehr aktuell" in zweite.text
        conn = _db(daten)
        anzahl = conn.execute("SELECT COUNT(*) FROM zahlung WHERE rechnung_id = ?",
                              (rid,)).fetchone()[0]
        conn.close()
        assert anzahl == 1

    def test_doppelklick_auf_teilzahlung_bucht_einmal(self, klient, daten):
        rid = self._gestellt(klient, daten)
        daten_formular = {"datum": "2026-03-20", "betrag_euro": "40,00",
                          "art": "UEBERWEISUNG", "erwartet_bezahlt": "0"}
        klient.post(f"/rechnung/{rid}/zahlung", data=daten_formular)
        klient.post(f"/rechnung/{rid}/zahlung", data=daten_formular)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "50,00" in seite, "nur 40 von 90 bezahlt"

    def test_vollstaendig_bezahlte_rechnung_nimmt_keinen_eingang_mehr(self, klient, daten):
        rid = self._gestellt(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-20", "betrag_euro": "", "art": "UEBERWEISUNG"})
        antwort = klient.post(f"/rechnung/{rid}/zahlung",
                              data={"datum": "2026-03-21", "betrag_euro": "",
                                    "art": "UEBERWEISUNG"}, follow_redirects=True)
        # Echte Umlaute in der Meldung, wie ueberall sonst auf der Oberflaeche.
        assert "bereits vollständig bezahlt" in antwort.text


class TestListe:
    def test_liste_zeigt_status_und_pdf(self, klient, daten):
        _zeiten(klient, anzahl=1, minuten="1h")
        rid = _entwurf(klient, daten)
        _festschreiben(klient, rid, daten)
        seite = klient.get("/rechnungen").text
        assert "RE-2026-0001" in seite
        assert "Mustermann GmbH" in seite
        assert ">PDF<" in seite

    def test_dashboard_fuehrt_zum_assistenten(self, klient):
        _zeiten(klient, anzahl=2, minuten="1h")
        seite = klient.get("/").text
        assert "/rechnung/neu?kunde=1" in seite


class TestVorbelegungen:
    """Eine Einstellung, die nicht wirkt, ist schlimmer als keine.

    ``kunde.gruppierung_default`` wurde im Formular gepflegt und in der Route
    gelesen, aber die Vorlage setzte ``PRO_TITEL`` hart. Wer am Kunden „pro Tag"
    einstellte, bekam trotzdem „pro Titel" -- und merkte es erst an der fertigen
    Rechnung.
    """

    def _angekreuzt(self, text: str, wert: str) -> bool:
        """Steht hinter diesem Radioknopf ein ``checked``?"""
        return "checked" in text.split(f'value="{wert}"')[1][:60]

    def test_gruppierung_des_kunden_ist_vorausgewaehlt(self, klient, daten):
        # Die Auswahl erscheint erst, wenn es etwas zu gruppieren gibt.
        _zeiten(klient, anzahl=2, minuten="30")
        conn = _db(daten)
        try:
            conn.execute("UPDATE kunde SET gruppierung_default = 'PRO_TAG' WHERE id = 1")
            conn.commit()
        finally:
            conn.close()

        text = klient.get("/rechnung/neu?kunde=1").text
        assert self._angekreuzt(text, "PRO_TAG"), "Die Kundeneinstellung muss greifen"
        assert not self._angekreuzt(text, "PRO_TITEL")

    def test_ohne_einstellung_bleibt_es_bei_pro_titel(self, klient, daten):
        _zeiten(klient, anzahl=2, minuten="30")
        text = klient.get("/rechnung/neu?kunde=1").text
        assert self._angekreuzt(text, "PRO_TITEL")

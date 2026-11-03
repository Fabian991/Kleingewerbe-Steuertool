"""Der Eingang als Ganzes: mehrere Dateien hochladen, alle wegwerfen, alle
pruefen und buchen (mit Vorschau), alle angezeigten bestaetigen.

Geprueft wird vor allem, was NICHT passieren darf: dass die Vorschau etwas
schreibt, dass eine unsichere Zahl ohne Pruefmaske in die Buchfuehrung kommt,
dass eine Rechnung doppelt gebucht wird (zweimal im Stapel, Doppelklick), dass
ein Fehler einer Datei die anderen mitreisst, oder dass "Alle wegwerfen" etwas
ausserhalb des Eingangs anfasst. Lieferanten und Betraege sind erfunden.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.logik import euer, kategorien
from app.logik.beleg import abholen, bezahlt, pipeline, sammelbuchung
from app.main import erzeuge_app
from tests import test_belegordner as bo
from tests import test_email_belege as em
from tests import test_fremdwaehrung as fw

HEUTE = bo.HEUTE
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
PDF_EN = FIXTURES / "lieferantenrechnung_en.pdf"

datenordner = bo.datenordner
postfach = bo.postfach
conn = bo.conn


def anzahl(verbindung, sql: str, *werte) -> int:
    return verbindung.execute(sql, werte).fetchone()[0]


def lege_in_eingang(datenordner: Path, inhalt: bytes, name: str) -> str:
    return pipeline.aufnehmen(inhalt, name, datenordner).pfad.name


def plan(conn, datenordner, **weiter) -> dict[str, sammelbuchung.Plan]:
    weiter.setdefault("heute", HEUTE)
    weiter.setdefault("textleser", None)
    return {p.originalname: p for p in sammelbuchung.planen(conn, datenordner, **weiter)}


def alles_buchen(conn, datenordner, plaene, *, kategorien_=None, **weiter):
    """Bucht alles, was die Vorschau als buchbar zeigt -- wie der Knopf."""
    weiter.setdefault("heute", HEUTE)
    weiter.setdefault("textleser", None)
    auswahl = [sammelbuchung.Auswahl(p.eingangsname, p.sha256,
                                     (kategorien_ or {}).get(p.eingangsname, abholen.VORGABE))
               for p in plaene.values() if p.status == sammelbuchung.BUCHEN]
    return sammelbuchung.buchen(conn, datenordner, auswahl, **weiter)


def stand(conn) -> tuple[int, ...]:
    return tuple(anzahl(conn, f"SELECT COUNT(*) FROM {t}")
                 for t in ("beleg", "journal", "lieferant", "zahlung", "lieferant_profil"))


def kategorie(conn, schluessel: str) -> int:
    return kategorien.nach_schluessel(conn, schluessel)["id"]


# ------------------------------------------------------------ Vorschau

class TestPlanen:
    def test_vorschau_schreibt_nichts(self, conn, datenordner):
        lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        lege_in_eingang(datenordner, PDF_DE.read_bytes(), "Hetzner.pdf")
        vorher = stand(conn)
        plaene = plan(conn, datenordner)
        assert stand(conn) == vorher
        assert len(plaene) == 2

    def test_neuer_lieferant_aus_dem_xml_wird_gebucht(self, conn, datenordner):
        """E-Rechnung eines neuen Lieferanten: der Name ist sicher (XML) -- er
        wird angelegt. Ohne Vorgabe und Auswahl ohne Kategorie, bezahlt am
        Belegdatum."""
        lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        p = plan(conn, datenordner)["Hetzner.xml"]
        assert p.status == sammelbuchung.BUCHEN
        e = p.entscheidung
        assert (e.lieferant, e.lieferant_neu, e.kategorie_id, e.zahltag) == (
            "Hetzner Online GmbH", ("Hetzner Online GmbH", "DE812871812"), None, "2026-03-14")
        assert p.neuer_lieferant

    def test_neuer_lieferant_aus_der_textschicht_bleibt(self, conn, datenordner):
        """Aus der Textschicht ist der Name eines unbekannten Lieferanten nur
        geraten -- er wuerde unveraenderlich so angelegt. Die Datei bleibt."""
        lege_in_eingang(datenordner, PDF_DE.read_bytes(), "Hetzner.pdf")
        p = plan(conn, datenordner)["Hetzner.pdf"]
        assert p.status == sammelbuchung.BLEIBT
        assert p.grund.startswith("neuer Lieferant, Name nicht sicher erkannt")

    def test_bekannter_lieferant_mit_vorgabe(self, conn, datenordner):
        bo.lieferant(conn)
        lege_in_eingang(datenordner, PDF_DE.read_bytes(), "Hetzner.pdf")
        e = plan(conn, datenordner)["Hetzner.pdf"].entscheidung
        assert (e.kategorie_id, e.kategorie_herkunft) == (kategorie(conn, "HOSTING"),
                                                         "Vorgabe des Lieferanten")

    @pytest.mark.parametrize("schluessel, grund", [
        ("HARDWARE_ANLAGE", "Anlagegut"),
        ("BEWIRTUNG", "braucht eigene Angaben"),
    ])
    def test_gewaehlte_kategorie_mit_sperre_bleibt(self, conn, datenordner, schluessel, grund):
        """In der Vorschau gewaehlt, aber mit eigenen Regeln: die Datei bleibt
        mit Grund -- dieselben Sperren wie fuer eine Vorgabe."""
        name = lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        p = plan(conn, datenordner, kategorien={name: kategorie(conn, schluessel)})["Hetzner.xml"]
        assert p.status == sammelbuchung.BLEIBT
        assert grund in p.grund

    def test_gewaehlte_kategorie(self, conn, datenordner):
        name = lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        e = plan(conn, datenordner,
                 kategorien={name: kategorie(conn, "SAAS")})["Hetzner.xml"].entscheidung
        assert (e.kategorie_id, e.kategorie_herkunft) == (kategorie(conn, "SAAS"), "gewählt")

    @pytest.mark.parametrize("inhalt, grund", [
        (bo.cii(typ="381"), "Gutschrift"),
        (bo.cii(brutto="1190.00", netto="1000.00", ust="190.00"), "über 800,00 € netto"),
        (bo.cii("RE-2025-1228", "20251228"), bezahlt.JAHRESWECHSEL_GRUND),
    ])
    def test_bleibt_mit_grund(self, conn, datenordner, inhalt, grund):
        lege_in_eingang(datenordner, inhalt, "Rechnung.xml")
        p = plan(conn, datenordner)["Rechnung.xml"]
        assert p.status == sammelbuchung.BLEIBT
        assert grund in p.grund

    def test_jahreswechsel_offen_gelassen_wird_gebucht(self, conn, datenordner):
        """Ohne Zahlung entscheidet der Zahltag nichts: offen gelassen wird
        auch eine Rechnung vom 28.12. gebucht."""
        lege_in_eingang(datenordner, bo.cii("RE-2025-1228", "20251228"), "Rechnung.xml")
        p = plan(conn, datenordner, bezahlt=False)["Rechnung.xml"]
        assert p.status == sammelbuchung.BUCHEN
        assert p.entscheidung.zahltag is None

    def test_vorjahr_wird_gebucht_mit_warnung(self, conn, datenordner):
        """Anders als das unbeaufsichtigte Abholen bucht "Alle prüfen und
        buchen" einen Beleg aus dem Vorjahr (Nacherfassung eines Jahres) --
        aber Vorschau und Ergebnis sagen, dass die Zahlung die EUeR 2025
        aendert, die womoeglich schon abgegeben ist."""
        bo.lieferant(conn)
        lege_in_eingang(datenordner, bo.cii("RE-2025-0614", "20250614"), "Juni.xml")
        lege_in_eingang(datenordner, bo.cii(), "Maerz.xml")
        plaene = plan(conn, datenordner)
        juni, maerz = plaene["Juni.xml"].entscheidung, plaene["Maerz.xml"].entscheidung
        assert plaene["Juni.xml"].status == sammelbuchung.BUCHEN
        assert (juni.zahltag, juni.vorjahr, maerz.vorjahr) == ("2025-06-14", 2025, None)
        assert sammelbuchung.vorjahr_warnung(list(plaene.values())) == (
            "Achtung: 1 Zahlung zählt in der EÜR 2025 — ist sie schon abgegeben, ändert sie "
            "sich mit und müsste berichtigt werden.")
        ergebnis = alles_buchen(conn, datenordner, plaene)
        assert sorted((g.originalname, g.vorjahr) for g in ergebnis.gebucht) == [
            ("Juni.xml", 2025), ("Maerz.xml", None)]
        assert ergebnis.vorjahre == [2025]
        assert ergebnis.zusammenfassung() == (
            "2 Belege gebucht. Achtung: 1 Zahlung zählt in der EÜR 2025 — ist sie schon "
            "abgegeben, ändert sie sich mit und müsste berichtigt werden.")
        # Offen gelassen gibt es keine Zahlung und nichts zu warnen.
        assert sammelbuchung.vorjahr_warnung(list(plan(conn, datenordner,
                                                       bezahlt=False).values())) is None

    def test_usd_aus_der_textschicht_bleibt(self, conn, datenordner):
        """Die Waehrung ist aus "$" nur geraten -- auch bei einem bekannten
        Lieferanten ohne gemerkte Waehrung."""
        bo.lieferant(conn, "Amazon Web Services EMEA SARL", "LU26888617")
        lege_in_eingang(datenordner, PDF_EN.read_bytes(), "aws.pdf")
        p = plan(conn, datenordner)["aws.pdf"]
        assert p.status == sammelbuchung.BLEIBT
        assert p.grund.startswith("Währung nicht sicher erkannt")

    def test_xml_und_pdf_derselben_rechnung_im_stapel(self, conn, datenordner):
        """Die E-Rechnung als XML und ihr PDF: das XML wird gebucht, das PDF
        bleibt -- "gleiche Rechnung wie" --, noch bevor eines gebucht ist."""
        bo.lieferant(conn)
        lege_in_eingang(datenordner, bo.cii(), "Rechnung.xml")
        lege_in_eingang(datenordner, bo.hetzner_pdf(
            nummer="Rechnungsnummer: RE-2026-0815", datum=["Rechnungsdatum: 14.03.2026"],
            netto="4,59", ust="0,87", brutto="5,46"), "Rechnung.pdf")
        plaene = plan(conn, datenordner)
        assert plaene["Rechnung.xml"].status == sammelbuchung.BUCHEN
        assert plaene["Rechnung.pdf"].status == sammelbuchung.BLEIBT
        assert plaene["Rechnung.pdf"].grund == ("gleiche Rechnung wie „Rechnung.xml“, die "
                                                "gebucht wird")

    def test_schon_gebuchte_datei_ist_dublette(self, conn, datenordner):
        lid = bo.lieferant(conn)
        beleg = bo.von_hand_buchen(conn, datenordner, bo.cii(), "erst.xml", lid)
        lege_in_eingang(datenordner, bo.cii(), "nochmal.xml")
        p = plan(conn, datenordner)["nochmal.xml"]
        assert p.status == sammelbuchung.DUBLETTE
        assert p.entscheidung.dublette_von == beleg


# ------------------------------------------------------------- Buchen

class TestBuchen:
    def test_bucht_automatisch_mit_vermerk_und_zahlung(self, conn, datenordner):
        """Gebucht wie der Eingangsordner: ungeprueft, ohne zu lernen, mit
        "automatisch" im Journal und dem Vermerk der Sammelbuchung -- und der
        Beleg steht unter "nur ungeklaerte" und in der EUeR-Luecke, solange
        er keine Kategorie hat."""
        lege_in_eingang(datenordner, bo.cii(), "Hetzner.xml")
        ergebnis = alles_buchen(conn, datenordner, plan(conn, datenordner))
        [gebucht] = ergebnis.gebucht
        assert (gebucht.originalname, gebucht.lieferant, gebucht.brutto_cent,
                gebucht.ohne_kategorie, gebucht.bezahlt_am) == (
            "Hetzner.xml", "Hetzner Online GmbH", 546, True, "2026-03-14")
        beleg = conn.execute("SELECT * FROM beleg").fetchone()
        assert beleg["geprueft_am"] is None
        assert beleg["kategorie_id"] is None
        assert beleg["notiz"] == sammelbuchung.NOTIZ
        extraktion = conn.execute("SELECT * FROM extraktion").fetchone()
        assert extraktion["verfahren"].endswith(pipeline.SAMMEL_VERMERK)
        assert json.loads(extraktion["bestaetigt_json"])["automatisch"] is True
        journal = json.loads(conn.execute(
            "SELECT wert_neu FROM journal WHERE aktion = 'BELEG_ERFASSEN'").fetchone()[0])
        assert journal["automatisch"] is True
        assert anzahl(conn, "SELECT COUNT(*) FROM lieferant_profil") == 0
        assert [tuple(z) for z in conn.execute(
            "SELECT datum, betrag_cent, art FROM zahlung")] == [("2026-03-14", -546, "SONSTIGES")]
        # Ohne Kategorie, aber bezahlt: die EUeR meldet die Luecke.
        assert euer.fehlende_zuordnung(conn, 2026).anzahl == 1
        assert euer.unbestaetigt_bezahlt(conn, 2026).anzahl == 1
        assert pipeline.liegen_geblieben(datenordner) == []

    def test_zwei_rechnungen_eines_neuen_lieferanten_ein_datensatz(self, conn, datenordner):
        lege_in_eingang(datenordner, bo.cii(), "a.xml")
        lege_in_eingang(datenordner, bo.cii("RE-2026-0901", "20260414"), "b.xml")
        ergebnis = alles_buchen(conn, datenordner, plan(conn, datenordner))
        assert len(ergebnis.gebucht) == 2
        assert anzahl(conn, "SELECT COUNT(*) FROM lieferant") == 1
        assert anzahl(conn, "SELECT COUNT(DISTINCT lieferant_id) FROM beleg") == 1

    def test_gewaehlte_kategorie_wird_gebucht(self, conn, datenordner):
        name = lege_in_eingang(datenordner, bo.cii(), "a.xml")
        saas = kategorie(conn, "SAAS")
        ergebnis = alles_buchen(conn, datenordner, plan(conn, datenordner),
                                kategorien_={name: saas})
        assert not ergebnis.gebucht[0].ohne_kategorie
        assert conn.execute("SELECT kategorie_id FROM beleg").fetchone()[0] == saas

    def test_offen_ohne_zahlung(self, conn, datenordner):
        lege_in_eingang(datenordner, bo.cii(), "a.xml")
        alles_buchen(conn, datenordner, plan(conn, datenordner, bezahlt=False), bezahlt=False)
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM zahlung") == 0

    def test_geaendert_seit_der_vorschau(self, conn, datenordner):
        """Eine andere Pruefsumme als in der Vorschau: uebersprungen, nie auf
        einen veralteten Stand gebucht."""
        name = lege_in_eingang(datenordner, bo.cii(), "a.xml")
        auswahl = [sammelbuchung.Auswahl(name, "0" * 64)]
        ergebnis = sammelbuchung.buchen(conn, datenordner, auswahl, heute=HEUTE,
                                        textleser=None)
        assert ergebnis.gebucht == []
        [geblieben] = ergebnis.geblieben
        assert "seit der Vorschau verändert" in geblieben.grund
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_zweimal_abgeschickt_bucht_einmal(self, conn, datenordner):
        lege_in_eingang(datenordner, bo.cii(), "a.xml")
        plaene = plan(conn, datenordner)
        erstes = alles_buchen(conn, datenordner, plaene)
        zweites = alles_buchen(conn, datenordner, plaene)
        assert len(erstes.gebucht) == 1
        assert zweites.gebucht == [] and zweites.geblieben == []
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_neu_entschieden_beim_buchen(self, conn, datenordner):
        """Zwischen Vorschau und Klick wurde dieselbe Rechnung von Hand
        gebucht (anderes PDF): die Datei bleibt, statt doppelt zu zaehlen."""
        lege_in_eingang(datenordner, bo.cii(), "a.xml")
        plaene = plan(conn, datenordner)
        lid = conn.execute("INSERT INTO lieferant (name, ust_idnr, angelegt_am) VALUES"
                           " ('Hetzner Online GmbH', 'DE812871812', '2026-01-01')").lastrowid
        conn.execute("INSERT INTO beleg (lieferant_id, belegdatum, belegnummer_extern, waehrung,"
                     " betrag_brutto_cent, betrag_betrieblich_cent, erfasst_am) VALUES"
                     " (?, '2026-03-14', 'RE-2026-0815', 'EUR', 546, 546, 'x')", (lid,))
        ergebnis = alles_buchen(conn, datenordner, plaene)
        assert ergebnis.gebucht == []
        [geblieben] = ergebnis.geblieben
        assert geblieben.grund.startswith("ähnlicher Beleg vorhanden")
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_fehler_einer_datei_haelt_die_anderen_nicht_auf(self, conn, datenordner,
                                                          monkeypatch):
        lege_in_eingang(datenordner, bo.cii(), "a.xml")
        lege_in_eingang(datenordner, bo.cii("RE-2026-0901", "20260414"), "b.xml")
        plaene = plan(conn, datenordner)
        echt = abholen.buche_entscheidung

        def kaputt(verbindung, entscheidung, *args, **kwargs):
            if entscheidung.lesung.eingang.original_name == "a.xml":
                raise OSError(28, "Kein Platz mehr auf dem Gerät")
            return echt(verbindung, entscheidung, *args, **kwargs)

        monkeypatch.setattr(abholen, "buche_entscheidung", kaputt)
        ergebnis = alles_buchen(conn, datenordner, plaene)
        assert [g.originalname for g in ergebnis.gebucht] == ["b.xml"]
        [geblieben] = ergebnis.geblieben
        assert geblieben.originalname == "a.xml"
        assert "Fehler" in geblieben.grund
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_laeuft_das_abholen_wird_nichts_gebucht(self, conn, datenordner):
        lege_in_eingang(datenordner, bo.cii(), "a.xml")
        plaene = plan(conn, datenordner)
        assert abholen._LAUF.acquire(blocking=False)
        try:
            with pytest.raises(abholen.LaeuftSchon):
                alles_buchen(conn, datenordner, plaene)
        finally:
            abholen._LAUF.release()
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_dubletten_wegwerfen_mit_journal(self, conn, datenordner):
        lid = bo.lieferant(conn)
        bo.von_hand_buchen(conn, datenordner, bo.cii(), "erst.xml", lid)
        name = lege_in_eingang(datenordner, bo.cii(), "nochmal.xml")
        fremd = lege_in_eingang(datenordner, bo.cii("RE-X", "20260401"), "anders.xml")
        ergebnis = sammelbuchung.buchen(conn, datenordner, [], wegwerfen=[name, fremd],
                                        heute=HEUTE, textleser=None)
        assert ergebnis.entfernt == ["nochmal.xml"]
        # Keine Dublette: bleibt, auch wenn ein manipuliertes Formular sie nennt.
        assert [p.name for p in pipeline.liegen_geblieben(datenordner)] == [fremd]
        [eintrag] = conn.execute("SELECT * FROM journal WHERE aktion = 'EINGANG_VERWORFEN'"
                                 ).fetchall()
        werte = json.loads(eintrag["wert_neu"])
        assert werte["anzahl"] == 1
        assert werte["dateien"][0]["originalname"] == "nochmal.xml"

    def test_fremdwaehrung_aus_dem_xml(self, conn, datenordner):
        """Eine E-Rechnung in Dollar: die Waehrung ist sicher, der Kurs da --
        gebucht, und die Zahlung ist der Euro-Betrag."""
        fw.tabelle_ablegen(datenordner)
        lege_in_eingang(datenordner, bo.cii("RE-USD-1", "20260313", waehrung="USD"), "usd.xml")
        plaene = plan(conn, datenordner, heute=fw.HEUTE)
        p = plaene["usd.xml"]
        assert p.status == sammelbuchung.BUCHEN, p.grund
        assert p.entscheidung.euro_cent
        alles_buchen(conn, datenordner, plaene, heute=fw.HEUTE)
        beleg = conn.execute("SELECT * FROM beleg").fetchone()
        assert beleg["waehrung"] == "USD"
        assert beleg["betrag_brutto_cent"] == p.entscheidung.euro_cent
        assert conn.execute("SELECT betrag_cent FROM zahlung").fetchone()[0] == \
            -beleg["betrag_brutto_cent"]


# ---------------------------------------------------------------- Web

@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=bo.EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen():
        verbindung = sqlite3.connect(tmp_path / "steuer.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung

    return oeffnen


def mail_mit_pdf() -> bytes:
    return em.mail("Ihre Rechnung", "Im Anhang die Rechnung.",
                   anhaenge=[("Rechnung.pdf", PDF_DE.read_bytes(), "application/pdf")])


def hochladen(klient, *dateien: tuple[str, bytes]):
    return klient.post("/belege/hochladen", files=[
        ("datei", (name, inhalt, "application/octet-stream")) for name, inhalt in dateien],
        follow_redirects=False)


def eingangsnamen(tmp_path) -> list[str]:
    return sorted(p.name for p in pipeline.liegen_geblieben(tmp_path))


class TestHochladen:
    def test_mehrere_dateien_landen_im_eingang(self, klient, tmp_path):
        antwort = hochladen(klient, ("a.xml", bo.cii()), ("b.pdf", PDF_DE.read_bytes()),
                            ("c.eml", mail_mit_pdf()))
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith("/belege?erfolg=")
        assert antwort.headers["location"].endswith("#eingang")
        assert "3 Dateien in den Eingang aufgenommen" in klient.get(
            antwort.headers["location"]).text
        assert len(eingangsnamen(tmp_path)) == 3

    def test_eine_falsche_haelt_die_anderen_nicht_auf(self, klient, tmp_path):
        antwort = hochladen(klient, ("a.xml", bo.cii()), ("notiz.docx", b"PK\x03\x04"))
        seite = klient.get(antwort.headers["location"]).text
        assert "1 Datei in den Eingang aufgenommen. Nicht aufgenommen: notiz.docx:" in seite
        assert len(eingangsnamen(tmp_path)) == 1

    def test_eine_datei_fuehrt_in_die_pruefmaske(self, klient):
        antwort = hochladen(klient, ("a.xml", bo.cii()))
        assert antwort.headers["location"].startswith("/belege/pruefen/")


class TestAlleWegwerfen:
    def _seite_und_namen(self, klient) -> tuple[str, list[str]]:
        seite = klient.get("/belege").text
        return seite, re.findall(r'<input type="hidden" name="datei" value="([^"]+)">', seite)

    def test_alle_angezeigten_mit_begleitdateien(self, klient, tmp_path, datenbank):
        """Drei Uploads, darunter eine Mail mit PDF im Anhang: danach ist der
        Eingang leer -- auch die versteckten Begleitdateien (Originalname,
        Anhang). Ein Journaleintrag mit Originalname und Pruefsumme je Datei."""
        hochladen(klient, ("a.xml", bo.cii()), ("b.pdf", PDF_DE.read_bytes()),
                  ("c.eml", mail_mit_pdf()))
        name_mail = next(n for n in eingangsnamen(tmp_path) if n.endswith(".eml"))
        klient.get(f"/belege/pruefen/{name_mail}")  # legt den Anhang versteckt ab
        assert any(".anhang" in p.name for p in (tmp_path / "eingang").iterdir())
        seite, namen = self._seite_und_namen(klient)
        assert "Alle 3 Dateien wegwerfen …" in seite
        assert "Gelöscht wird nur die Kopie im Tool" in seite
        antwort = klient.post("/belege/eingang/alle-wegwerfen", data={"datei": namen},
                              follow_redirects=True)
        assert "3 Dateien aus dem Eingang weggeworfen." in antwort.text
        assert [p.name for p in (tmp_path / "eingang").iterdir()] == []
        with datenbank() as verbindung:
            [eintrag] = verbindung.execute(
                "SELECT * FROM journal WHERE aktion = 'EINGANG_VERWORFEN'").fetchall()
        werte = json.loads(eintrag["wert_neu"])
        assert sorted(d["originalname"] for d in werte["dateien"]) == ["a.xml", "b.pdf", "c.eml"]
        assert all(len(d["sha256"]) == 64 for d in werte["dateien"])
        # Dieselbe Datei laesst sich danach wieder aufnehmen.
        assert hochladen(klient, ("a.xml", bo.cii())).status_code == 303
        assert len(eingangsnamen(tmp_path)) == 1

    def test_nur_die_angezeigten(self, klient, tmp_path):
        """Was nach dem Anzeigen dazukam, bleibt liegen."""
        hochladen(klient, ("a.xml", bo.cii()), ("b.pdf", PDF_DE.read_bytes()))
        _, namen = self._seite_und_namen(klient)
        spaeter = pipeline.aufnehmen(bo.cii("RE-NEU", "20260401"), "neu.xml", tmp_path)
        klient.post("/belege/eingang/alle-wegwerfen", data={"datei": namen})
        assert eingangsnamen(tmp_path) == [spaeter.pfad.name]

    @pytest.mark.parametrize("name", ["../steuer.sqlite", "..%2Fsteuer.sqlite",
                                      ".a.xml.name", "steuer.sqlite"])
    def test_nichts_ausserhalb_des_eingangs(self, klient, tmp_path, name):
        hochladen(klient, ("a.xml", bo.cii()), ("b.pdf", PDF_DE.read_bytes()))
        klient.post("/belege/eingang/alle-wegwerfen", data={"datei": [name]})
        assert (tmp_path / "steuer.sqlite").is_file()
        assert len(eingangsnamen(tmp_path)) == 2

    def test_gebuchter_beleg_bleibt_unberuehrt(self, klient, tmp_path, datenbank):
        name = hochladen(klient, ("a.xml", bo.cii())).headers["location"].rsplit("/", 1)[1]
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "5,46", "waehrung": "EUR"})
        with datenbank() as verbindung:
            pfad = verbindung.execute("SELECT pfad FROM belegdatei").fetchone()[0]
        hochladen(klient, ("b.pdf", PDF_DE.read_bytes()), ("c.pdf", bo.leeres_pdf(
            tmp_path / "leer.pdf")))
        _, namen = self._seite_und_namen(klient)
        klient.post("/belege/eingang/alle-wegwerfen", data={"datei": [*namen, name]})
        assert (tmp_path / pfad).is_file()
        with datenbank() as verbindung:
            assert verbindung.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1

    def test_waehrend_des_abholens_wird_nichts_geloescht(self, klient, tmp_path, datenbank):
        hochladen(klient, ("a.xml", bo.cii()), ("b.pdf", PDF_DE.read_bytes()))
        _, namen = self._seite_und_namen(klient)
        assert abholen._LAUF.acquire(blocking=False)
        try:
            antwort = klient.post("/belege/eingang/alle-wegwerfen", data={"datei": namen},
                                  follow_redirects=True)
        finally:
            abholen._LAUF.release()
        assert "Das Abholen aus dem Eingangsordner läuft gerade" in antwort.text
        assert len(eingangsnamen(tmp_path)) == 2
        with datenbank() as verbindung:
            assert verbindung.execute("SELECT COUNT(*) FROM journal WHERE aktion ="
                                      " 'EINGANG_VERWORFEN'").fetchone()[0] == 0


class TestAllePruefenUndBuchen:
    def test_vorschau_und_buchen(self, klient, tmp_path, datenbank):
        """Die Vorschau zeigt, was gebucht wird und was bleibt (mit Grund),
        schreibt nichts; der Knopf bucht, die Belegseite zeigt das Ergebnis."""
        hochladen(klient, ("Hetzner.xml", bo.cii()), ("Scan.pdf", PDF_DE.read_bytes()))
        assert "Alle prüfen und buchen …" in klient.get("/belege").text
        with datenbank() as verbindung:
            vorher = verbindung.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        vorschau = klient.get("/belege/alle-buchen").text
        assert "1 Datei wird gebucht" in vorschau
        assert "1 Beleg buchen" in vorschau
        assert 'title="Wird mit diesem Namen angelegt">neu</span>' in vorschau
        assert "neuer Lieferant, Name nicht sicher erkannt" in vorschau
        with datenbank() as verbindung:
            assert verbindung.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == vorher
            assert verbindung.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0
        felder = dict(re.findall(r'<input type="hidden" name="(sha_[^"]+)" value="([^"]+)">',
                                 vorschau))
        [name] = re.findall(r'name="buchen" value="([^"]+)"', vorschau)
        daten = {"zahlung": "bezahlt", "buchen": name, **felder, f"kategorie_{name}": ""}
        antwort = klient.post("/belege/alle-buchen", data=daten, follow_redirects=False)
        assert antwort.headers["location"] == "/belege?sammel=1#sammelergebnis"
        seite = klient.get("/belege?sammel=1").text
        assert "Alle prüfen und buchen:</strong> 1 Beleg gebucht." in seite
        assert "ohne Kategorie — in der Liste" in seite
        # Die Datei, die bleibt, steht im Eingang -- mit dem Grund aus der Vorschau.
        assert "wartet: neuer Lieferant, Name nicht sicher erkannt" in seite
        # Doppelklick: nichts doppelt.
        klient.post("/belege/alle-buchen", data=daten)
        with datenbank() as verbindung:
            assert verbindung.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1
        # Ohne Kategorie steht er unter "nur ungeklaerte".
        assert 'id="beleg-1"' in klient.get("/belege?offen=1").text
        # Ohne sammel=1 kein Ergebniskasten.
        assert "sammelergebnis" not in klient.get("/belege").text

    def test_vorschau_aktualisieren_mit_kategorie_und_offen(self, klient):
        hochladen(klient, ("a.xml", bo.cii("RE-2025-1228", "20251228")),
                  ("b.xml", bo.cii("RE-2026-0901", "20260414")))
        vorschau = klient.get("/belege/alle-buchen").text
        assert "1 Datei wird gebucht" in vorschau
        assert bezahlt.JAHRESWECHSEL_GRUND in vorschau
        offen = klient.get("/belege/alle-buchen?zahlung=offen").text
        assert "2 Dateien werden gebucht" in offen
        assert re.search(r'value="offen"\s+checked', offen)

    def test_vorjahr_warnt_vor_dem_klick_und_danach(self, klient):
        """Die Vorschau warnt oben und je Zeile, das Ergebnis danach."""
        vorjahr = date.today().year - 1
        hochladen(klient, ("a.xml", bo.cii(f"RE-{vorjahr}-0614", f"{vorjahr}0614")))
        vorschau = klient.get("/belege/alle-buchen").text
        assert "1 Datei wird gebucht" in vorschau
        assert re.search(r'<div class="meldung warn" id="vorjahr">Achtung: 1 Zahlung zählt in '
                         rf'der EÜR {vorjahr}', vorschau)
        assert f">EÜR {vorjahr}</span>" in vorschau
        # Offen gelassen: keine Zahlung, keine Warnung.
        assert 'id="vorjahr"' not in klient.get("/belege/alle-buchen?zahlung=offen").text
        felder = dict(re.findall(r'<input type="hidden" name="(sha_[^"]+)" value="([^"]+)">',
                                 vorschau))
        [name] = re.findall(r'name="buchen" value="([^"]+)"', vorschau)
        klient.post("/belege/alle-buchen", data={"zahlung": "bezahlt", "buchen": name, **felder})
        seite = klient.get("/belege?sammel=1").text
        assert re.search(r'<div class="meldung warn nicht-drucken"\s+id="sammelergebnis"', seite)
        assert f"1 Beleg gebucht. Achtung: 1 Zahlung zählt in der EÜR {vorjahr}" in seite
        assert f"zählt in der EÜR {vorjahr}</span>" in seite

    def test_mehr_als_1000_formularfelder(self, klient, tmp_path):
        """Je Datei schickt die Vorschau drei Felder. Starlettes Vorgabe von
        1000 Feldern endete ab rund 330 Dateien mit einem rohen englischen
        400er, und nichts war gebucht. 400 Dateien (1201 Felder) gehen jetzt
        durch -- hier mit einer echten Datei unter vielen inzwischen
        verschwundenen, damit der Test nicht 400 Dateien lesen muss."""
        hochladen(klient, ("a.xml", bo.cii()))
        vorschau = klient.get("/belege/alle-buchen").text
        [name] = re.findall(r'name="buchen" value="([^"]+)"', vorschau)
        felder = dict(re.findall(r'<input type="hidden" name="(sha_[^"]+)" value="([^"]+)">',
                                 vorschau))
        verschwunden = [f"weg-{i:03d}.pdf" for i in range(399)]
        daten = {"zahlung": "bezahlt", "buchen": [name, *verschwunden], **felder,
                 **{f"sha_{n}": "0" * 64 for n in verschwunden},
                 **{f"kategorie_{n}": "" for n in verschwunden}}
        antwort = klient.post("/belege/alle-buchen", data=daten, follow_redirects=False)
        assert antwort.status_code == 303
        assert "1 Beleg gebucht" in klient.get("/belege?sammel=1").text

    def test_alle_wegwerfen_mit_mehr_als_1000_namen(self, klient, tmp_path):
        hochladen(klient, ("a.xml", bo.cii()))
        [name] = eingangsnamen(tmp_path)
        antwort = klient.post("/belege/eingang/alle-wegwerfen",
                              data={"datei": [name, *(f"weg-{i}.pdf" for i in range(1200))]},
                              follow_redirects=True)
        assert antwort.status_code == 200
        assert "1 Datei aus dem Eingang weggeworfen." in antwort.text
        assert eingangsnamen(tmp_path) == []

    def test_unlesbares_formular_ist_eine_deutsche_seite(self, klient):
        """Ein 400 des Formularlesers (hier: mehrteilig ohne Grenze) -- vorher
        das rohe JSON {"detail": ...} von Starlette."""
        antwort = klient.post("/belege/alle-buchen", content=b"x",
                              headers={"content-type": "multipart/form-data"})
        assert antwort.status_code == 400
        assert antwort.headers["content-type"].startswith("text/html")
        assert "Das Formular ließ sich nicht lesen" in antwort.text
        assert "Gespeichert wurde nichts" in antwort.text

    def test_nichts_ausgewaehlt(self, klient):
        hochladen(klient, ("a.xml", bo.cii()))
        antwort = klient.post("/belege/alle-buchen", data={"zahlung": "bezahlt"},
                              follow_redirects=True)
        assert "Nichts ausgewählt" in antwort.text


class TestAlleBestaetigen:
    def test_alle_angezeigten(self, klient, tmp_path, datenbank):
        hochladen(klient, ("a.xml", bo.cii()), ("b.xml", bo.cii("RE-2026-0901", "20260414")))
        vorschau = klient.get("/belege/alle-buchen").text
        namen = re.findall(r'name="buchen" value="([^"]+)"', vorschau)
        felder = dict(re.findall(r'<input type="hidden" name="(sha_[^"]+)" value="([^"]+)">',
                                 vorschau))
        klient.post("/belege/alle-buchen", data={"zahlung": "bezahlt", "buchen": namen, **felder})
        assert "Alle 2 angezeigten bestätigen …" not in klient.get("/belege").text
        liste = klient.get("/belege?offen=1").text
        assert "Alle 2 angezeigten bestätigen …" in liste
        kennungen = re.findall(r'<input type="hidden" name="beleg" value="(\d+)">', liste)
        assert sorted(kennungen) == ["1", "2"]
        antwort = klient.post("/belege/geprueft-alle", data={"beleg": kennungen},
                              follow_redirects=True)
        assert "2 Belege bestätigt." in antwort.text
        nochmal = klient.post("/belege/geprueft-alle", data={"beleg": kennungen},
                              follow_redirects=True)
        assert "schon bestätigt" in nochmal.text
        with datenbank() as verbindung:
            assert verbindung.execute(
                "SELECT COUNT(*) FROM beleg WHERE geprueft_am IS NULL").fetchone()[0] == 0
            assert verbindung.execute(
                "SELECT COUNT(*) FROM journal WHERE aktion = 'BELEG_GEPRUEFT'").fetchone()[0] == 2
        # Ohne Kategorie bleiben sie ungeklaert -- aber ohne Bestaetigungsknopf.
        liste = klient.get("/belege?offen=1").text
        assert 'id="beleg-1"' in liste
        assert "angezeigten bestätigen" not in liste

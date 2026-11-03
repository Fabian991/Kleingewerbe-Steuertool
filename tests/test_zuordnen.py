"""Dateien im Eingang den vorhandenen Buchungen zuordnen (Paket H).

Betrag und Datum zuerst, der Name nur als Bonus; Stufen sicher /
wahrscheinlich / mehrdeutig; globale Zuordnung ueber den Stapel; Einnahmen
als Ziel; nie still doppelt buchen -- in der Sammelbuchung, in der Pruefmaske
und beim Abholen aus dem Eingangsordner. Alle Namen, Texte und Betraege sind
erfunden; die WISO-Belege entstehen aus einer erfundenen Exportdatei
(tests/wiso_beispiel.py).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import einnahmen, euer, kategorien
from app.logik.beleg import abholen, anhaengen, pipeline, sammelbuchung
from app.main import erzeuge_app
from tests import test_belegordner as bo
from tests import wiso_beispiel as b
from tests.test_wiso_import import HEUTE, aktiv, anzahl, einspielen

SONSTIGE = ("4900", "Sonstige betriebliche Aufwendungen")
WERBUNG = ("4600", "Werbekosten")


# ------------------------------------------------------------------ Helfer

def rechnung(datum: str, brutto: str, netto: str, ust: str, *, name: str,
             nummer: str | None = None) -> bytes:
    """Eine erfundene E-Rechnung (CII) mit eigenem Verkaeufer; die Summen gehen auf."""
    return bo.cii(nummer or f"RE-{datum}-{brutto}", datum, brutto=brutto, netto=netto,
                  ust=ust).replace(b"Hetzner Online GmbH", name.encode())


def gutschrift_pdf(datum: str = "17.01.2026", betrag: str = "120,50",
                   name: str = "Beispiel Plattform Ltd") -> bytes:
    """Eine Gutschrift mit Textschicht, wie eine Plattform sie ausstellt."""
    return bo.pdf_aus_zeilen([name, "1 Example Street", "Dublin", "Gutschrift",
                              "Gutschriftsnummer: GS-2026-77", f"Datum: {datum}",
                              "Auszahlung Januar", f"Gesamtbetrag {betrag} EUR"])


def ablegen(ordner: Path, inhalt: bytes, name: str) -> str:
    return pipeline.aufnehmen(inhalt, name, ordner).pfad.name


def plaene(conn, ordner) -> dict[str, sammelbuchung.Plan]:
    return {p.originalname: p for p in sammelbuchung.planen(conn, ordner, heute=HEUTE,
                                                             textleser=None)}


def auswahl_laut_vorschau(p: sammelbuchung.Plan) -> sammelbuchung.Auswahl:
    """Was der Klick fuer eine angehakte Zeile der Gruppe "zuordnen" schickt."""
    k = p.zuordnung.vorschlag
    return sammelbuchung.Auswahl(
        p.eingangsname, p.sha256, None,
        anhaengen_an=k.id if k.art == anhaengen.BELEG else None,
        einnahme_an=k.id if k.art == anhaengen.EINNAHME else None)


def buchen(conn, ordner, auswahl) -> sammelbuchung.SammelErgebnis:
    return sammelbuchung.buchen(conn, ordner, auswahl, heute=HEUTE, textleser=None)


def ausgaben(conn) -> int:
    a = euer.auswertung(conn, 2026)
    return sum(z.betrag_cent for z in a.ausgaben)


def einnahme(conn, *, tag: str, betrag: int, zahler: str) -> int:
    return einnahmen.erfassen(conn, zufluss_am=tag, betrag_cent=betrag, zahler=zahler,
                              kategorie_id=kategorien.nach_schluessel(
                                  conn, "EINNAHME_HONORAR_AUSLAND")["id"], heute=HEUTE)


@pytest.fixture
def ordner(tmp_path) -> Path:
    return tmp_path


# ------------------------------------------------------------ Bewertung

class TestBewertung:
    def test_name_passt_nicht_fuenf_tage_sicher(self, conn, betrieb, ordner):
        """WISO sagt "Claude", die Rechnung kommt von "Anthropic Beispiel PBC":
        der Name passt nicht, aber Betrag gleich und 5 Tage Abstand -- sicher.
        Vorher gab es hier keinen Vorschlag, die Datei wurde als zweiter Beleg
        gebucht (Luecke 1)."""
        einspielen(conn, ordner, [b.Buchung(1, "15.03.2026", "Claude Abo", SONSTIGE, soll="23,00")])
        beleg_id = aktiv(conn, 1)["beleg_id"]
        ablegen(ordner, rechnung("20260310", "23.00", "19.33", "3.67",
                                 name="Anthropic Beispiel PBC"), "anthropic.xml")
        p = plaene(conn, ordner)["anthropic.xml"]
        assert p.status == sammelbuchung.ZUORDNEN
        z = p.zuordnung
        assert (z.stufe, z.angehakt, z.vorschlag.id, z.vorschlag.art) == (
            "sicher", True, beleg_id, anhaengen.BELEG)
        assert (z.vorschlag.abstand, z.vorschlag.name_passt, z.vorschlag.wiso_nummer) == (
            5, False, "1")
        assert p.grund.startswith(f"passt vielleicht zu Beleg {beleg_id} (WISO-Eintrag Nr. 1)")

    def test_zwanzig_tage_mit_namen_sicher(self, conn, betrieb, ordner):
        """Zahlungsziel: die Rechnung liegt 20 Tage vor dem Zahltag, der Name
        passt -- sicher. Vorher galt ein Fenster von 7 Tagen."""
        einspielen(conn, ordner, [b.Buchung(1, "30.03.2026", "Beispiel Hosting Server", SONSTIGE,
                                            soll="29,75")])
        ablegen(ordner, rechnung("20260310", "29.75", "25.00", "4.75",
                                 name="Beispiel Hosting GmbH"), "hosting.xml")
        z = plaene(conn, ordner)["hosting.xml"].zuordnung
        assert (z.stufe, z.vorschlag.abstand, z.vorschlag.name_passt) == ("sicher", 20, True)

    def test_zwanzig_tage_ohne_namen_wahrscheinlich(self, conn, betrieb, ordner):
        """Weit weg und ohne Namen: wahrscheinlich -- vorgeschlagen, aber nicht
        angehakt; der Nutzer bestaetigt."""
        einspielen(conn, ordner, [b.Buchung(1, "30.03.2026", "Claude Abo", SONSTIGE, soll="23,00")])
        ablegen(ordner, rechnung("20260310", "23.00", "19.33", "3.67",
                                 name="Anthropic Beispiel PBC"), "anthropic.xml")
        z = plaene(conn, ordner)["anthropic.xml"].zuordnung
        assert (z.stufe, z.angehakt, z.vorschlag.abstand) == ("wahrscheinlich", False, 20)

    def test_ausserhalb_des_fensters_kein_treffer(self, conn, betrieb, ordner):
        einspielen(conn, ordner, [b.Buchung(1, "30.04.2026", "Claude Abo", SONSTIGE, soll="23,00")])
        ablegen(ordner, rechnung("20260310", "23.00", "19.33", "3.67",
                                 name="Anthropic Beispiel PBC"), "anthropic.xml")
        p = plaene(conn, ordner)["anthropic.xml"]
        assert p.status == sammelbuchung.BUCHEN and p.zuordnung is None

    def test_anderer_betrag_kein_treffer(self, conn, betrieb, ordner):
        einspielen(conn, ordner, [b.Buchung(1, "12.03.2026", "Claude Abo", SONSTIGE, soll="23,01")])
        ablegen(ordner, rechnung("20260310", "23.00", "19.33", "3.67",
                                 name="Anthropic Beispiel PBC"), "anthropic.xml")
        assert plaene(conn, ordner)["anthropic.xml"].status == sammelbuchung.BUCHEN

    def test_zwei_kandidaten_mehrdeutig_mit_auswahl(self, conn, betrieb, ordner):
        """Zwei Abo-Buchungen mit gleichem Betrag, eine Datei: mehrdeutig, der
        naechste Eintrag ist vorausgewaehlt, beide stehen zur Wahl."""
        einspielen(conn, ordner, [
            b.Buchung(1, "05.01.2026", "Beispiel Office Abo", SONSTIGE, soll="13,92"),
            b.Buchung(2, "05.02.2026", "Beispiel Office Abo", SONSTIGE, soll="13,92")])
        ablegen(ordner, rechnung("20260103", "13.92", "11.70", "2.22",
                                 name="Beispiel Office Ltd"), "januar.xml")
        z = plaene(conn, ordner)["januar.xml"].zuordnung
        assert (z.stufe, z.angehakt) == ("mehrdeutig", False)
        assert [(k.id, k.abstand) for k in z.kandidaten] == [
            (aktiv(conn, 1)["beleg_id"], 2), (aktiv(conn, 2)["beleg_id"], 33)]
        assert z.vorschlag.id == aktiv(conn, 1)["beleg_id"]
        assert z.vergeben == {}

    def test_eigenbeleg_ist_kein_kandidat(self, conn, betrieb, ordner):
        """Ein Eigenbeleg (Pauschale) hat naturgemaess keine Datei -- er ist
        kein Ziel, auch wenn Betrag und Datum passen."""
        from app.logik.beleg import eigenbeleg

        eigenbeleg.erfasse(conn, belegdatum="2026-03-10", brutto_cent=2300,
                           grund="Pauschale, erfunden",
                           kategorie_id=kategorien.nach_schluessel(conn, "UEBRIGE")["id"])
        assert anhaengen.kandidaten(conn, brutto_cent=2300, datum="2026-03-10",
                                    lieferant="Irgendwer") == []
        assert anhaengen.ohne_datei_anzahl(conn) == (0, 0)


class TestGlobaleZuordnung:
    ABOS = [b.Buchung(1, "05.01.2026", "Beispiel Office Abo", SONSTIGE, soll="13,92"),
            b.Buchung(2, "05.02.2026", "Beispiel Office Abo", SONSTIGE, soll="13,92"),
            b.Buchung(3, "05.03.2026", "Beispiel Office Abo", SONSTIGE, soll="13,92")]

    def _drei_rechnungen(self, ordner) -> None:
        # Absichtlich in umgekehrter Ordnerreihenfolge: Datei fuer Datei
        # bekaeme die Maerz-Rechnung den Januar-Eintrag.
        for monat, name in (("03", "c-maerz.xml"), ("02", "b-februar.xml"),
                            ("01", "a-januar.xml")):
            ablegen(ordner, rechnung(f"2026{monat}03", "13.92", "11.70", "2.22",
                                     name="Beispiel Office Ltd"), name)

    def test_drei_monatsabos_je_auf_den_naechsten_eintrag(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ABOS)
        self._drei_rechnungen(ordner)
        p = plaene(conn, ordner)
        # Im Fenster von 45 Tagen liegen fuer Januar und Maerz je zwei
        # Eintraege, fuer Februar alle drei.
        erwartet = {"a-januar.xml": (1, 2), "b-februar.xml": (2, 3), "c-maerz.xml": (3, 2)}
        for datei, (nummer, kandidaten) in erwartet.items():
            z = p[datei].zuordnung
            assert z.vorschlag.id == aktiv(conn, nummer)["beleg_id"], datei
            assert z.vorschlag.abstand == 2
            # Die anderen Eintraege sind fuer die anderen Dateien vorgesehen
            # -- es bleibt genau ein freier: sicher, angehakt.
            assert (z.stufe, z.angehakt, len(z.kandidaten), len(z.vergeben)) == (
                "sicher", True, kandidaten, kandidaten - 1), datei
        assert sammelbuchung.zuordnen_zusammenfassung(list(p.values())) == "3 sicher"

    def test_zuordnen_haengt_an_und_euer_zaehlt_einmal(self, conn, betrieb, ordner):
        einspielen(conn, ordner, self.ABOS)
        self._drei_rechnungen(ordner)
        vorher = (ausgaben(conn), anzahl(conn, "SELECT COUNT(*) FROM beleg"))
        assert vorher[0] == 3 * 1392
        p = plaene(conn, ordner)
        ergebnis = buchen(conn, ordner, [auswahl_laut_vorschau(x) for x in p.values()])
        assert len(ergebnis.angehaengt) == 3 and ergebnis.gebucht == ergebnis.geblieben == []
        assert ergebnis.zusammenfassung() == "3 Dateien vorhandenen Buchungen zugeordnet."
        assert (ausgaben(conn), anzahl(conn, "SELECT COUNT(*) FROM beleg")) == vorher
        for nummer in (1, 2, 3):
            assert not anhaengen.ohne_datei(conn, aktiv(conn, nummer)["beleg_id"])
        assert pipeline.liegen_geblieben(ordner) == []
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEG_DATEI_ANGEHAENGT'") == 3

    def test_vierte_datei_ohne_freien_eintrag_bleibt_liegen(self, conn, betrieb, ordner):
        """Vier Rechnungen, drei Eintraege: die Datei, deren Eintraege alle
        fuer andere vorgesehen sind, bekommt keinen Vorschlag -- "liegen
        lassen", nie ein zweiter Beleg."""
        einspielen(conn, ordner, self.ABOS)
        self._drei_rechnungen(ordner)
        ablegen(ordner, rechnung("20260320", "13.92", "11.70", "2.22",
                                 name="Beispiel Office Ltd", nummer="RE-NACHZUEGLER"),
                "d-nachzuegler.xml")
        z = plaene(conn, ordner)["d-nachzuegler.xml"].zuordnung
        assert (z.vorschlag, z.stufe, z.angehakt) == (None, "mehrdeutig", False)
        # Der Januar-Eintrag liegt ausserhalb des Fensters; Februar und Maerz
        # gehoeren den anderen Dateien.
        assert set(z.vergeben.values()) == {"b-februar.xml", "c-maerz.xml"}

    def test_greedy_ohne_datenbank(self):
        """Das beste Paar zuerst -- nicht die erste Datei."""
        k = anhaengen.Kandidat
        dateien = {
            "spaet.xml": [k(anhaengen.BELEG, 1, "2026-01-05", 1392, 10, True)],
            "frueh.xml": [k(anhaengen.BELEG, 1, "2026-01-05", 1392, 2, True)],
        }
        z = anhaengen.zuordnen(dateien)
        assert z["frueh.xml"].vorschlag.id == 1 and z["spaet.xml"].vorschlag is None
        assert z["spaet.xml"].vergeben == {(anhaengen.BELEG, 1): "frueh.xml"}


class TestNieStillDoppelt:
    def test_sammelbuchung_bucht_mit_treffer_nicht_neu(self, conn, betrieb, ordner):
        """Ohne ausdrueckliche Wahl "neu buchen" bleibt die Datei -- auch wenn
        der Klick sie als normale Buchung schickt (veraltete Vorschau)."""
        einspielen(conn, ordner, [b.Buchung(1, "30.03.2026", "Claude Abo", SONSTIGE, soll="23,00")])
        name = ablegen(ordner, rechnung("20260310", "23.00", "19.33", "3.67",
                                        name="Anthropic Beispiel PBC"), "anthropic.xml")
        p = plaene(conn, ordner)["anthropic.xml"]
        assert p.zuordnung.stufe == "wahrscheinlich" and p.neu_buchbar
        ergebnis = buchen(conn, ordner, [sammelbuchung.Auswahl(name, p.sha256)])
        assert ergebnis.gebucht == [] and ergebnis.angehaengt == []
        assert len(ergebnis.geblieben) == 1
        assert ergebnis.geblieben[0].grund.startswith("passt vielleicht zu Beleg")
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
        assert [x.name for x in pipeline.liegen_geblieben(ordner)] == [name]
        # Ausdruecklich "neu buchen": dann wird gebucht.
        ergebnis = buchen(conn, ordner, [sammelbuchung.Auswahl(name, p.sha256,
                                                               trotz_treffer=True)])
        assert len(ergebnis.gebucht) == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 2

    def test_kandidat_inzwischen_mit_datei(self, conn, betrieb, ordner):
        """Zwischen Vorschau und Klick bekam der Beleg eine Datei: Meldung,
        nichts doppelt, die Datei bleibt im Eingang."""
        einspielen(conn, ordner, [b.Buchung(1, "15.03.2026", "Claude Abo", SONSTIGE, soll="23,00")])
        beleg_id = aktiv(conn, 1)["beleg_id"]
        ablegen(ordner, rechnung("20260310", "23.00", "19.33", "3.67",
                                 name="Anthropic Beispiel PBC"), "anthropic.xml")
        p = plaene(conn, ordner)["anthropic.xml"]
        andere = pipeline.aufnehmen(rechnung("20260311", "23.00", "19.33", "3.67",
                                             name="Anthropic Beispiel PBC", nummer="RE-ANDERE"),
                                    "andere.xml", ordner)
        anhaengen.anhaengen(conn, ordner, beleg_id, pipeline.lies(conn, andere, heute=HEUTE))
        ergebnis = buchen(conn, ordner, [auswahl_laut_vorschau(p)])
        assert ergebnis.angehaengt == [] and ergebnis.gebucht == []
        assert ergebnis.geblieben[0].grund.startswith(f"Beleg {beleg_id} passt nicht mehr")
        assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?", beleg_id) == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
        assert [x.name for x in pipeline.liegen_geblieben(ordner)] == [p.eingangsname]

    def test_zwei_dateien_auf_denselben_eintrag(self, conn, betrieb, ordner):
        """Wer beide Zeilen auf denselben Eintrag stellt, bekommt genau eine
        angehaengt -- die zweite meldet es."""
        einspielen(conn, ordner, [b.Buchung(1, "15.03.2026", "Claude Abo", SONSTIGE, soll="23,00")])
        beleg_id = aktiv(conn, 1)["beleg_id"]
        a = ablegen(ordner, rechnung("20260310", "23.00", "19.33", "3.67",
                                     name="Anthropic Beispiel PBC"), "a.xml")
        c = ablegen(ordner, rechnung("20260312", "23.00", "19.33", "3.67",
                                     name="Anthropic Beispiel PBC", nummer="RE-2"), "c.xml")
        p = plaene(conn, ordner)
        ergebnis = buchen(conn, ordner, [
            sammelbuchung.Auswahl(a, p["a.xml"].sha256, None, anhaengen_an=beleg_id),
            sammelbuchung.Auswahl(c, p["c.xml"].sha256, None, anhaengen_an=beleg_id)])
        assert len(ergebnis.angehaengt) == 1 and len(ergebnis.geblieben) == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?", beleg_id) == 1

    def test_eingangsordner_wartet(self, conn, betrieb, ordner):
        """Modus ORDNER (unbeaufsichtigt): eine Datei mit Treffer wartet mit
        Grund -- fuer Belege wie fuer Einnahmen."""
        einspielen(conn, ordner, [b.Buchung(1, "15.03.2026", "Claude Abo", SONSTIGE, soll="23,00")])
        einnahme(conn, tag="17.01.2026", betrag=12050, zahler="Video Portal")
        bo.lieferant(conn, name="Anthropic Beispiel PBC", ust_idnr="DE812871812")
        eingang = pipeline.aufnehmen(rechnung("20260310", "23.00", "19.33", "3.67",
                                              name="Anthropic Beispiel PBC"), "a.xml", ordner)
        e = abholen.pruefe_automatisch(conn, eingang, ordner, modus=abholen.ORDNER, heute=HEUTE)
        assert not e.buchbar
        assert e.grund.startswith("passt vielleicht zu Beleg 1 (WISO-Eintrag Nr. 1)")
        assert "zuordnen" in e.grund
        gutschrift = pipeline.aufnehmen(gutschrift_pdf(), "gutschrift.pdf", ordner)
        e = abholen.pruefe_automatisch(conn, gutschrift, ordner, modus=abholen.ORDNER,
                                       heute=HEUTE)
        assert "Einnahme 1" in e.grund


class TestEinnahmen:
    def test_gutschrift_passt_zuerst_zur_einnahme(self, conn, betrieb, ordner):
        """Eine Gutschrift (Textschicht) mit 120,50 €: ein Beleg liegt einen Tag
        naeher, die Einnahme steht trotzdem zuerst -- beide stehen zur Wahl."""
        einspielen(conn, ordner, [b.Buchung(1, "16.01.2026", "Muster Ads", WERBUNG, soll="120,50")])
        einnahme_id = einnahme(conn, tag="17.01.2026", betrag=12050, zahler="Video Portal")
        ablegen(ordner, gutschrift_pdf(datum="15.01.2026"), "gutschrift.pdf")
        p = plaene(conn, ordner)["gutschrift.pdf"]
        assert p.status == sammelbuchung.ZUORDNEN and not p.neu_buchbar
        z = p.zuordnung
        assert z.gutschrift
        assert [(k.art, k.id, k.abstand) for k in z.kandidaten] == [
            (anhaengen.EINNAHME, einnahme_id, 2), (anhaengen.BELEG, aktiv(conn, 1)["beleg_id"], 1)]
        assert (z.stufe, z.vorschlag.art) == ("mehrdeutig", anhaengen.EINNAHME)

    def test_einnahme_allein_mit_namen_sicher(self, conn, betrieb, ordner):
        einnahme_id = einnahme(conn, tag="17.01.2026", betrag=12050, zahler="Beispiel Plattform")
        ablegen(ordner, gutschrift_pdf(datum="15.01.2026"), "gutschrift.pdf")
        z = plaene(conn, ordner)["gutschrift.pdf"].zuordnung
        assert (z.stufe, z.vorschlag.id, z.vorschlag.name_passt) == ("sicher", einnahme_id, True)

    def test_an_einnahme_anhaengen(self, conn, betrieb, ordner):
        """Die Datei kommt an die Einnahme (einnahmedatei, schreibgeschuetzt),
        verlaesst den Eingang, steht im Journal; Betrag und Tag bleiben, die
        EUeR aendert sich nicht."""
        einnahme_id = einnahme(conn, tag="17.01.2026", betrag=12050, zahler="Beispiel Plattform")
        ablegen(ordner, gutschrift_pdf(datum="15.01.2026"), "gutschrift.pdf")
        a_vorher = euer.auswertung(conn, 2026).einnahmen_cent
        p = plaene(conn, ordner)["gutschrift.pdf"]
        ergebnis = buchen(conn, ordner, [auswahl_laut_vorschau(p)])
        assert [(a.art, a.ziel_id) for a in ergebnis.angehaengt] == [
            (anhaengen.EINNAHME, einnahme_id)]
        assert ergebnis.angehaengt[0].abweichungen == [
            "Datum laut Datei 15.01.2026, Zufluss am 17.01.2026"]
        datei = conn.execute("SELECT * FROM einnahmedatei WHERE einnahme_id = ?",
                             (einnahme_id,)).fetchone()
        assert datei["original_name"] == "gutschrift.pdf"
        assert (ordner / datei["pfad"]).is_file()
        assert pipeline.liegen_geblieben(ordner) == []
        assert euer.auswertung(conn, 2026).einnahmen_cent == a_vorher
        assert tuple(conn.execute("SELECT zufluss_am, betrag_cent FROM einnahme WHERE id = ?",
                                  (einnahme_id,)).fetchone()) == ("2026-01-17", 12050)
        eintrag = conn.execute("SELECT wert_neu FROM journal WHERE aktion = 'EINNAHME_DATEI'"
                               " AND objekt_id = ?", (einnahme_id,)).fetchone()
        wert = json.loads(eintrag["wert_neu"])
        assert wert["aus_eingang"] is True and wert["abweichungen"]
        # Danach ist die Einnahme kein Kandidat mehr.
        assert anhaengen.kandidaten(conn, brutto_cent=12050, datum="2026-01-15",
                                    lieferant="Beispiel Plattform") == []

    def test_einnahme_mit_datei_ist_kein_ziel_mehr(self, conn, betrieb, ordner):
        einnahme_id = einnahme(conn, tag="17.01.2026", betrag=12050, zahler="Beispiel Plattform")
        einnahmen.datei_anhaengen(conn, ordner, einnahme_id,
                                  einnahmen.datei_pruefen(b"%PDF-1.4\n%%EOF\n", "x.pdf"))
        eingang = pipeline.aufnehmen(gutschrift_pdf(), "gutschrift.pdf", ordner)
        lesung = pipeline.lies(conn, eingang, heute=HEUTE)
        assert anhaengen.zur_lesung(conn, lesung) == []
        with pytest.raises(pipeline.BelegFehler, match="schon eine Datei"):
            anhaengen.an_einnahme(conn, ordner, einnahme_id, lesung)
        assert [x.name for x in pipeline.liegen_geblieben(ordner)] == [eingang.pfad.name]

    def test_zaehler_fuer_den_einstieg(self, conn, betrieb, ordner):
        einspielen(conn, ordner, [b.Buchung(1, "16.01.2026", "Muster Ads", WERBUNG, soll="120,50"),
                                  b.Buchung(2, "20.01.2026", "Youtube Ads", b.ERLOES,
                                            haben="80,00")])
        einnahme(conn, tag="17.01.2026", betrag=12050, zahler="Video Portal")
        assert anhaengen.ohne_datei_anzahl(conn) == (1, 2)


# -------------------------------------------------------------------- Web

@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=bo.EINRICHTUNG, follow_redirects=False)
        yield c


def datenbank(tmp_path) -> sqlite3.Connection:
    """Die Datenbank der App -- fuer Vorbereitung (WISO-Import) und Nachschau."""
    return db.oeffne(tmp_path / "steuer.sqlite")


def hochladen(klient, *dateien: tuple[str, bytes]):
    return klient.post("/belege/hochladen", files=[
        ("datei", (name, inhalt, "application/octet-stream")) for name, inhalt in dateien],
        follow_redirects=False)


def felder(vorschau: str) -> dict[str, str]:
    return dict(re.findall(r'<input type="hidden" name="((?:sha|stufe)_[^"]+)" value="([^"]+)">',
                           vorschau))


class TestVorschau:
    def _wiso_beleg(self, tmp_path, datum="15.03.2026") -> int:
        with datenbank(tmp_path) as conn:
            einspielen(conn, tmp_path, [b.Buchung(1, datum, "Claude Abo", SONSTIGE, soll="23,00")])
            return aktiv(conn, 1)["beleg_id"]

    def test_gruppe_zuordnen_und_alle_sicheren(self, klient, tmp_path):
        beleg_id = self._wiso_beleg(tmp_path)
        hochladen(klient, ("anthropic.xml", rechnung("20260310", "23.00", "19.33", "3.67",
                                                     name="Anthropic Beispiel PBC")),
                  ("neu.xml", bo.cii("RE-2026-0999", "20260401")))
        seite = klient.get("/belege").text
        assert "2 Dateien im Eingang" in seite
        assert re.search(r"1 Buchung ohne Datei \(1\s+Beleg, 0\s+Einnahmen\)", seite)
        assert 'href="/belege/alle-buchen#zuordnen"' in seite
        vorschau = klient.get("/belege/alle-buchen").text
        assert '<h2 id="zuordnen">' in vorschau
        assert vorschau.index('id="zuordnen"') < vorschau.index("Wird gebucht")
        assert "1 sicher, 1 ohne Treffer wird neu gebucht" in vorschau
        assert "1 Datei wird gebucht" in vorschau
        assert re.search(r"1 passt\s+zu vorhandenen Buchungen", vorschau)
        [name] = re.findall(r'name="zuordnen" value="([^"]+)"\s+checked', vorschau)
        assert f'<option value="beleg:{beleg_id}"\n                selected>' in vorschau
        assert "5 Tage" in vorschau and "WISO Nr. 1" in vorschau and "Claude Abo" in vorschau
        assert "Die sichere\n      Zuordnung übernehmen" in vorschau
        assert 'name="zuordnung"' not in vorschau  # das Radio gehoert zur Pruefmaske
        # "Alle sicheren Zuordnungen übernehmen": nur die -- "neu.xml" bleibt.
        daten = {"nur_sichere": "1", "zahlung": "bezahlt", "buchen": "irrelevant",
                 "zuordnen_alle": name, f"ziel_{name}": f"beleg:{beleg_id}", **felder(vorschau)}
        antwort = klient.post("/belege/alle-buchen", data=daten, follow_redirects=False)
        assert antwort.headers["location"] == "/belege?sammel=1#sammelergebnis"
        seite = klient.get("/belege?sammel=1").text
        assert "1 Datei einer vorhandenen Buchung zugeordnet." in seite
        assert re.search(rf"anthropic\.xml</a> —\s+Beleg {beleg_id}\b", seite)
        assert "neu.xml" in seite  # liegt noch im Eingang
        with datenbank(tmp_path) as conn:
            assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
            assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?",
                          beleg_id) == 1

    def test_wahrscheinlich_nicht_angehakt(self, klient, tmp_path):
        beleg_id = self._wiso_beleg(tmp_path, datum="30.03.2026")
        hochladen(klient, ("anthropic.xml", rechnung("20260310", "23.00", "19.33", "3.67",
                                                     name="Anthropic Beispiel PBC")),
                  ("neu.xml", bo.cii("RE-2026-0999", "20260401")))
        vorschau = klient.get("/belege/alle-buchen").text
        assert "1 zur Bestätigung" in vorschau
        assert not re.search(r'name="zuordnen" value="[^"]+"\s+checked', vorschau)
        assert "sicheren Zuordnungen" not in vorschau and "Die sichere" not in vorschau
        [name] = re.findall(r'name="zuordnen" value="([^"]+)"', vorschau)
        # Der Klick ohne Haken in dieser Zeile: nichts angehaengt, nichts
        # doppelt -- nur neu.xml wird gebucht.
        [neu] = re.findall(r'name="buchen" value="([^"]+)"', vorschau)
        daten = {"zahlung": "bezahlt", "buchen": neu, f"ziel_{name}": f"beleg:{beleg_id}",
                 **felder(vorschau)}
        klient.post("/belege/alle-buchen", data=daten)
        seite = klient.get("/belege?sammel=1").text
        assert "1 Beleg gebucht." in seite
        assert "wartet: passt vielleicht zu Beleg" in seite
        with datenbank(tmp_path) as conn:
            assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?",
                          beleg_id) == 0
        # Angehakt und bestaetigt: angehaengt.
        daten = {"zahlung": "bezahlt", "zuordnen": name, f"ziel_{name}": f"beleg:{beleg_id}",
                 **felder(vorschau)}
        klient.post("/belege/alle-buchen", data=daten)
        with datenbank(tmp_path) as conn:
            assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?",
                          beleg_id) == 1
            assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 2

    def test_neu_buchen_als_wahl(self, klient, tmp_path):
        beleg_id = self._wiso_beleg(tmp_path)
        hochladen(klient, ("anthropic.xml", rechnung("20260310", "23.00", "19.33", "3.67",
                                                     name="Anthropic Beispiel PBC")),
                  ("neu.xml", bo.cii("RE-2026-0999", "20260401")))
        vorschau = klient.get("/belege/alle-buchen").text
        assert '<option value="neu">neu buchen' in vorschau
        [name] = re.findall(r'name="zuordnen" value="([^"]+)"', vorschau)
        klient.post("/belege/alle-buchen", data={"zahlung": "offen", "zuordnen": name,
                                                 f"ziel_{name}": "neu", **felder(vorschau)})
        with datenbank(tmp_path) as conn:
            assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 2
            assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?",
                          beleg_id) == 0


class TestPruefmaske:
    def test_radio_sicher_vorausgewaehlt_und_anhaengen(self, klient, tmp_path):
        with datenbank(tmp_path) as conn:
            einspielen(conn, tmp_path, [b.Buchung(1, "15.03.2026", "Claude Abo", SONSTIGE,
                                                  soll="23,00")])
            beleg_id = aktiv(conn, 1)["beleg_id"]
        antwort = hochladen(klient, ("anthropic.xml", rechnung("20260310", "23.00", "19.33",
                                                               "3.67",
                                                               name="Anthropic Beispiel PBC")))
        maske = klient.get(antwort.headers["location"])
        assert maske.status_code == 200
        html = maske.text
        assert "Passt zu einer vorhandenen Buchung?" in html
        assert re.search(rf'name="zuordnung" value="beleg:{beleg_id}" required\s+checked', html)
        assert re.search(r'name="zuordnung" value="neu" required\s+>', html)
        assert f"an Beleg {beleg_id} anhängen (WISO-Nr. 1)" in html
        assert "wiso-anhaengen" not in html
        gebucht = klient.post(antwort.headers["location"],
                              data={"zuordnung": f"beleg:{beleg_id}"}, follow_redirects=False)
        assert gebucht.status_code == 303
        ziel = gebucht.headers["location"]
        # Mit Warnung: die Datei traegt das Rechnungsdatum, der WISO-Beleg
        # den Zahltag -- die Meldung sagt es, angehaengt wird trotzdem.
        assert ziel.startswith("/belege?warnung=Datei%20an%20Beleg") and f"beleg={beleg_id}" in ziel
        assert "Datum%20laut%20Datei%2010.03.2026" in ziel
        with datenbank(tmp_path) as conn:
            assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
            assert anzahl(conn, "SELECT COUNT(*) FROM belegdatei WHERE beleg_id = ?",
                          beleg_id) == 1

    def test_radio_neu_buchen_bucht(self, klient, tmp_path):
        with datenbank(tmp_path) as conn:
            einspielen(conn, tmp_path, [b.Buchung(1, "15.03.2026", "Claude Abo", SONSTIGE,
                                                  soll="23,00")])
        antwort = hochladen(klient, ("anthropic.xml", rechnung("20260310", "23.00", "19.33",
                                                               "3.67",
                                                               name="Anthropic Beispiel PBC")))
        gebucht = klient.post(antwort.headers["location"], data={
            "zuordnung": "neu", "belegdatum": "2026-03-10", "brutto_euro": "23,00",
            "waehrung": "EUR", "lieferant_neu": "Anthropic Beispiel PBC", "zahlung": "offen"},
            follow_redirects=False)
        assert gebucht.status_code == 303 and "erfolg=" in gebucht.headers["location"]
        with datenbank(tmp_path) as conn:
            assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 2

    def test_an_einnahme_aus_der_maske(self, klient, tmp_path):
        with datenbank(tmp_path) as conn:
            einnahme_id = einnahme(conn, tag="17.01.2026", betrag=12050,
                                   zahler="Beispiel Plattform")
        antwort = hochladen(klient, ("gutschrift.pdf", gutschrift_pdf(datum="15.01.2026")))
        html = klient.get(antwort.headers["location"]).text
        assert f'value="einnahme:{einnahme_id}" required\n          checked' in html
        assert "die Datei ist eine\n          Gutschrift" in html
        gebucht = klient.post(antwort.headers["location"],
                              data={"zuordnung": f"einnahme:{einnahme_id}"},
                              follow_redirects=False)
        ziel = gebucht.headers["location"]
        assert ziel.startswith("/einnahmen?jahr=2026&warnung=")
        assert ziel.endswith(f"#einnahme-{einnahme_id}")
        seite = klient.get(ziel).text
        assert f"Datei an Einnahme {einnahme_id} angehängt" in seite
        assert "Datum laut Datei 15.01.2026, Zufluss am 17.01.2026" in seite
        assert f'href="/einnahmen/{einnahme_id}/datei"' in seite
        with datenbank(tmp_path) as conn:
            assert anzahl(conn, "SELECT COUNT(*) FROM einnahmedatei") == 1
            assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0
        assert pipeline.liegen_geblieben(tmp_path) == []

    def test_eigener_knopf_mit_ziel(self, klient, tmp_path):
        with datenbank(tmp_path) as conn:
            einnahme_id = einnahme(conn, tag="17.01.2026", betrag=12050,
                                   zahler="Beispiel Plattform")
        antwort = hochladen(klient, ("gutschrift.pdf", gutschrift_pdf(datum="17.01.2026")))
        name = antwort.headers["location"].rsplit("/", 1)[1]
        weiter = klient.post(f"/belege/eingang/{name}/anhaengen",
                             data={"ziel": f"einnahme:{einnahme_id}"}, follow_redirects=False)
        assert weiter.headers["location"].startswith("/einnahmen?jahr=2026&erfolg=")
        # Ein zweites Mal: die Datei ist weg.
        nochmal = klient.post(f"/belege/eingang/{name}/anhaengen",
                              data={"ziel": f"einnahme:{einnahme_id}"}, follow_redirects=False)
        assert "nicht%20mehr%20im%20Eingang" in nochmal.headers["location"]

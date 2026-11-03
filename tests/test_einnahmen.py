"""Einnahmen ohne eigene Rechnung (Paket G): erfassen, einstufen, loeschen.

Affiliate-Auszahlungen, Werbeerloese und Plattform-Abrechnungen kommen ohne
Rechnung aus diesem Werkzeug. Geprueft wird die Logik (EUeR, § 19-Zaehler,
Datei, Loeschmarkierung, gemerkte Einstufung je Zahler), die Sperren in der
Datenbank (GoBD: Betrag und Tag fest, nie physisch geloescht) und die Seite.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.logik import einnahmen, euer, export_excel, grenzwert, kategorien
from app.main import erzeuge_app
from tests import test_belegordner as bo

HEUTE = date(2026, 10, 27)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def kat(conn, schluessel: str) -> int:
    return kategorien.nach_schluessel(conn, schluessel)["id"]


def erfassen(conn, *, betrag: int = 12000, zahler: str = "Beispiel Partner GmbH",
             schluessel: str = "EINNAHME_HONORAR_INLAND", tag: str = "15.03.2026", **weiter):
    return einnahmen.erfassen(conn, zufluss_am=tag, betrag_cent=betrag, zahler=zahler,
                              kategorie_id=kat(conn, schluessel), heute=HEUTE, **weiter)


def anzahl(conn, sql: str, *werte) -> int:
    return conn.execute(sql, werte).fetchone()[0]


class TestErfassen:
    def test_inland_zaehlt_in_euer_und_19(self, conn, betrieb):
        einnahme_id = erfassen(conn)
        zeile = conn.execute("SELECT * FROM einnahme WHERE id = ?", (einnahme_id,)).fetchone()
        assert (zeile["zufluss_am"], zeile["betrag_cent"], zeile["zahler"], zeile["herkunft"]) \
            == ("2026-03-15", 12000, "Beispiel Partner GmbH", "MANUELL")
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'EINNAHME_ERFASSEN'"
                      " AND objekt_id = ?", einnahme_id) == 1
        a = euer.auswertung(conn, 2026)
        assert [(z.kennzahl, z.bezeichnung, z.betrag_cent) for z in a.einnahmen] == [
            (111, "Honorar & Dienstleistung (Inland) — ohne eigene Rechnung", 12000)]
        assert grenzwert.vereinnahmt_cent(conn, 2026) == 12000
        assert einnahmen.zaehlt_zu_19_cent(conn, 2026) == 12000
        assert [z["beleg"] for z in euer.buchungen(conn, 2026)] == [f"Einnahme {einnahme_id}"]

    def test_ausland_zaehlt_nicht_zu_19(self, conn, betrieb):
        erfassen(conn, zahler="Amazon", schluessel="EINNAHME_HONORAR_AUSLAND")
        a = euer.auswertung(conn, 2026)
        assert a.einnahmen_cent == 12000
        assert [(z.kennzahl, z.betrag_cent) for z in a.einnahmen if z.nachrichtlich] == [
            (119, 12000)]
        assert grenzwert.vereinnahmt_cent(conn, 2026) == 0

    def test_anlagenverkauf_und_erstattung(self, conn, betrieb):
        erfassen(conn, betrag=30000, zahler="Muster Käufer",
                 schluessel="EINNAHME_ANLAGENVERKAUF")
        erfassen(conn, betrag=3840, zahler="Beispiel Newsletter",
                 schluessel="EINNAHME_ERSTATTUNG")
        a = euer.auswertung(conn, 2026)
        assert a.einnahmen_cent == 33840
        assert grenzwert.vereinnahmt_cent(conn, 2026) == 0
        # KZ 119 nachrichtlich: die Erstattung ist kein Umsatz (111 - 119 = § 19).
        assert {z.kennzahl for z in a.einnahmen} == {102, 111, 119}
        assert [z.betrag_cent for z in a.einnahmen if z.nachrichtlich] == [3840]

    def test_mit_abrechnung(self, conn, betrieb, tmp_path):
        datei = einnahmen.datei_pruefen(PDF, "abrechnung-maerz.pdf")
        einnahme_id = erfassen(conn, datei=datei, datenordner=tmp_path)
        zeile = conn.execute("SELECT * FROM einnahmedatei WHERE einnahme_id = ?",
                             (einnahme_id,)).fetchone()
        assert zeile["pfad"].startswith("einnahmen/2026/2026-03-15_")
        assert (tmp_path / zeile["pfad"]).read_bytes() == PDF
        assert os.stat(tmp_path / zeile["pfad"]).st_mode & 0o222 == 0
        assert zeile["sha256"] == datei.sha256
        # Nachtraeglich eine zweite Datei.
        einnahmen.datei_anhaengen(conn, tmp_path, einnahme_id,
                                  einnahmen.datei_pruefen(PDF + b"x", "beleg.pdf"))
        assert anzahl(conn, "SELECT COUNT(*) FROM einnahmedatei") == 2

    @pytest.mark.parametrize(("felder", "meldung"), [
        ({"betrag": 0}, "über 0,00"),
        ({"betrag": 100_000_001}, "unplausibel"),
        ({"zahler": "   "}, "Zahler"),
        ({"zahler": "x" * 121}, "zu lang"),
        ({"tag": "31.02.2026"}, "Zugeflossen"),
        ({"tag": "01.12.2026"}, "Zugeflossen"),
    ])
    def test_ungueltig(self, conn, betrieb, felder, meldung):
        with pytest.raises(einnahmen.EinnahmeFehler, match=meldung):
            erfassen(conn, **felder)
        assert anzahl(conn, "SELECT COUNT(*) FROM einnahme") == 0

    def test_nur_einnahmenkategorie(self, conn, betrieb):
        with pytest.raises(einnahmen.EinnahmeFehler, match="keine aktive Einnahmenkategorie"):
            erfassen(conn, schluessel="UEBRIGE")

    @pytest.mark.parametrize(("inhalt", "name", "meldung"), [
        (b"MZ\x90\x00", "abrechnung.exe", "kein Belegformat"),
        (b"", "abrechnung.pdf", "leer"),
        (b"kein pdf", "abrechnung.pdf", "passt nicht zur Endung"),
    ])
    def test_datei_wird_geprueft(self, inhalt, name, meldung):
        with pytest.raises(einnahmen.EinnahmeFehler, match=meldung):
            einnahmen.datei_pruefen(inhalt, name)

    def test_einstufung_wird_gemerkt(self, conn, betrieb):
        erfassen(conn, zahler="Beispiel Ads", schluessel="EINNAHME_HONORAR_AUSLAND",
                 merken_fuer_zahler=True)
        # Gemerkt unter dem bereinigten Zahler -- wie der WISO-Import ihn sucht.
        gemerkt = einnahmen.gemerkt(conn, einnahmen.zahler_kennung("BEISPIEL ads"))
        assert gemerkt["kategorie_id"] == kat(conn, "EINNAHME_HONORAR_AUSLAND")
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'ZAHLER_EINSTUFUNG'"
                      ) == 1


class TestAendernUndLoeschen:
    def test_kategorie_umstellen(self, conn, betrieb):
        einnahme_id = erfassen(conn)
        einnahmen.kategorie_setzen(conn, einnahme_id, kat(conn, "EINNAHME_HONORAR_AUSLAND"),
                                   merken_fuer_zahler=True)
        assert grenzwert.vereinnahmt_cent(conn, 2026) == 0
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'EINNAHME_KATEGORIE'"
                      ) == 1
        assert einnahmen.gemerkt(conn, "beispielpartnergmbh") is not None

    def test_loeschen_und_wiederherstellen(self, conn, betrieb):
        einnahme_id = erfassen(conn)
        with pytest.raises(einnahmen.EinnahmeFehler, match="Grund"):
            einnahmen.loeschen(conn, einnahme_id, grund="x")
        assert einnahmen.loeschen(conn, einnahme_id, grund="doppelt erfasst") is True
        assert einnahmen.loeschen(conn, einnahme_id, grund="doppelt erfasst") is False
        assert euer.auswertung(conn, 2026).einnahmen_cent == 0
        assert grenzwert.vereinnahmt_cent(conn, 2026) == 0
        assert einnahmen.liste(conn, 2026) == []
        assert len(einnahmen.liste(conn, 2026, mit_geloeschten=True)) == 1
        with pytest.raises(einnahmen.EinnahmeFehler, match="erst wiederherstellen"):
            einnahmen.kategorie_setzen(conn, einnahme_id, kat(conn, "EINNAHME_HONORAR_AUSLAND"))
        assert einnahmen.wiederherstellen(conn, einnahme_id) is True
        assert euer.auswertung(conn, 2026).einnahmen_cent == 12000
        aktionen = [z[0] for z in conn.execute(
            "SELECT aktion FROM journal WHERE objekttyp = 'einnahme' ORDER BY id")]
        assert aktionen == ["EINNAHME_ERFASSEN", "EINNAHME_GELOESCHT",
                            "EINNAHME_WIEDERHERGESTELLT"]


class TestSperren:
    def test_betrag_und_tag_fest(self, conn, betrieb):
        einnahme_id = erfassen(conn)
        for sql in ("UPDATE einnahme SET betrag_cent = 1 WHERE id = ?",
                    "UPDATE einnahme SET zufluss_am = '2026-03-16' WHERE id = ?",
                    "UPDATE einnahme SET zahler = 'jemand' WHERE id = ?",
                    "UPDATE einnahme SET herkunft = 'WISO' WHERE id = ?",
                    "DELETE FROM einnahme WHERE id = ?",
                    "UPDATE einnahme SET geloescht_am = '2026-10-27' WHERE id = ?"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (einnahme_id,))

    def test_nicht_geloescht_anlegen_und_geloescht_fest(self, conn, betrieb):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO einnahme (zufluss_am, betrag_cent, zahler, kategorie_id,"
                         " erfasst_am, geloescht_am, geloescht_grund) VALUES ('2026-01-01', 1,"
                         " 'x', ?, 'x', 'x', 'Test')", (kat(conn, "EINNAHME_HONORAR_INLAND"),))
        einnahme_id = erfassen(conn)
        einnahmen.loeschen(conn, einnahme_id, grund="Testeintrag")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE einnahme SET notiz = 'x' WHERE id = ?", (einnahme_id,))

    def test_kategorie_nur_aus_dem_einnahmenbereich(self, conn, betrieb):
        einnahme_id = erfassen(conn)
        with pytest.raises(sqlite3.IntegrityError, match="Einnahmenkategorie"):
            conn.execute("UPDATE einnahme SET kategorie_id = ? WHERE id = ?",
                         (kat(conn, "UEBRIGE"), einnahme_id))
        with pytest.raises(sqlite3.IntegrityError, match="Einnahmenkategorie"):
            conn.execute("INSERT INTO zahler (schluessel, name, kategorie_id, geaendert_am)"
                         " VALUES ('x', 'x', ?, 'x')", (kat(conn, "UEBRIGE"),))

    def test_beleg_nur_mit_ausgabenkategorie(self, conn, betrieb):
        with pytest.raises(sqlite3.IntegrityError, match="Ausgabenkategorie"):
            conn.execute("INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,"
                         " erfasst_am) VALUES ('EINGANG', ?, '2026-01-01', 100, 'x')",
                         (kat(conn, "EINNAHME_HONORAR_INLAND"),))

    def test_datei_fest(self, conn, betrieb, tmp_path):
        erfassen(conn, datei=einnahmen.datei_pruefen(PDF, "a.pdf"), datenordner=tmp_path)
        for sql in ("UPDATE einnahmedatei SET pfad = 'x'", "DELETE FROM einnahmedatei"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


class TestExcel:
    def test_blatt_mit_klartext(self, conn, betrieb, tmp_path):
        from openpyxl import load_workbook

        erfassen(conn, zahler="=SUMME(A1:A9)")
        pfad = tmp_path / "x.xlsx"
        pfad.write_bytes(export_excel.als_bytes(conn))
        blatt = load_workbook(pfad)["Einnahmen ohne Rechnung"]
        zellen = [z for zeile in blatt.iter_rows() for z in zeile]
        formel = next(z for z in zellen if z.value == "=SUMME(A1:A9)")
        assert formel.data_type == "s"
        assert all(z.data_type != "f" for z in zellen)
        assert "von Hand erfasst" in [z.value for z in zellen]


# ---------------------------------------------------------------- Seite

@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=bo.EINRICHTUNG, follow_redirects=False)
        yield c


def datenbank(tmp_path) -> sqlite3.Connection:
    verbindung = sqlite3.connect(tmp_path / "steuer.sqlite")
    verbindung.row_factory = sqlite3.Row
    return verbindung


class TestSeite:
    def test_erfassen_mit_vorschlag_und_datei(self, klient, tmp_path):
        antwort = klient.post("/einnahmen", data={
            "zufluss_am": "15.03.2026", "betrag": "120,50", "zahler": "Amazon Affiliate",
            "kategorie_id": "", "notiz": "März"},
            files={"datei": ("abrechnung.pdf", PDF, "application/pdf")},
            follow_redirects=False)
        assert antwort.status_code == 303
        seite = klient.get(antwort.headers["location"]).text
        assert "Kategorie vorgeschlagen" in seite and "Honorar Ausland" in seite
        conn = datenbank(tmp_path)
        zeile = conn.execute("SELECT * FROM einnahme").fetchone()
        assert (zeile["betrag_cent"], zeile["zahler"]) == (12050, "Amazon Affiliate")
        datei = klient.get(f"/einnahmen/{zeile['id']}/datei")
        assert datei.status_code == 200 and datei.content == PDF

    def test_fehler_bleibt_auf_der_seite(self, klient, tmp_path):
        antwort = klient.post("/einnahmen", data={
            "zufluss_am": "15.03.2026", "betrag": "abc", "zahler": "x"}, follow_redirects=False)
        # Keine Umleitung mehr: die Seite kommt mit Meldung und Eingaben zurueck.
        assert antwort.status_code == 422 and "Betrag:" in antwort.text
        assert datenbank(tmp_path).execute("SELECT COUNT(*) FROM einnahme").fetchone()[0] == 0

    def test_loeschen_wiederherstellen_umstellen(self, klient, tmp_path):
        conn = datenbank(tmp_path)
        inland = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel ="
                              " 'EINNAHME_HONORAR_INLAND'").fetchone()[0]
        ausland = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel ="
                               " 'EINNAHME_HONORAR_AUSLAND'").fetchone()[0]
        klient.post("/einnahmen", data={"zufluss_am": "15.03.2026", "betrag": "10,00",
                                        "zahler": "Beispiel GmbH", "kategorie_id": str(inland),
                                        "merken": "1"})
        einnahme_id = conn.execute("SELECT id FROM einnahme").fetchone()[0]
        antwort = klient.post(f"/einnahmen/{einnahme_id}/kategorie",
                              data={"kategorie_id": str(ausland)}, follow_redirects=False)
        assert "erfolg=" in antwort.headers["location"]
        antwort = klient.post(f"/einnahmen/{einnahme_id}/loeschen",
                              data={"grund": "doppelt erfasst"}, follow_redirects=False)
        assert "erfolg=" in antwort.headers["location"]
        assert conn.execute("SELECT geloescht_grund FROM einnahme").fetchone()[0] \
            == "doppelt erfasst"
        assert "doppelt erfasst" in klient.get("/einnahmen?jahr=2026&geloescht=1").text
        klient.post(f"/einnahmen/{einnahme_id}/wiederherstellen")
        assert conn.execute("SELECT geloescht_am FROM einnahme").fetchone()[0] is None
        # Gemerkte Einstufung des Zahlers aendern.
        antwort = klient.post("/einnahmen/zahler", data={
            "schluessel": "beispielgmbh", "kategorie_id": str(ausland)}, follow_redirects=False)
        assert "erfolg=" in antwort.headers["location"]
        assert conn.execute("SELECT kategorie_id FROM zahler").fetchone()[0] == ausland
        uebrige = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel = 'UEBRIGE'"
                               ).fetchone()[0]
        antwort = klient.post("/einnahmen/zahler", data={
            "schluessel": "beispielgmbh", "kategorie_id": str(uebrige)}, follow_redirects=False)
        assert "fehler=" in antwort.headers["location"]

    def test_uebersicht_zeigt_alle_einnahmen(self, klient, tmp_path):
        """Die Übersicht zeigt ALLE Betriebseinnahmen, daneben den § 19-Zähler.

        Vorher stand dort nur "Vereinnahmt (Inland)" -- wer Auslands- und
        Plattformerlöse hat, sah nur einen Teil seiner Einnahmen.
        """
        conn = datenbank(tmp_path)
        heute = date.today()
        tag = heute.strftime("%d.%m.%Y")
        inland = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel ="
                              " 'EINNAHME_HONORAR_INLAND'").fetchone()[0]
        ausland = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel ="
                               " 'EINNAHME_HONORAR_AUSLAND'").fetchone()[0]
        for betrag, zahler, kategorie in (("100,00", "Beispiel GmbH", inland),
                                          ("250,00", "Beispiel Plattform Ltd", ausland)):
            antwort = klient.post("/einnahmen", data={
                "zufluss_am": tag, "betrag": betrag, "zahler": zahler,
                "kategorie_id": str(kategorie)}, follow_redirects=False)
            assert antwort.status_code == 303 and "erfolg=" in antwort.headers["location"]
        text = " ".join(klient.get("/").text.split())
        gesamt = text.index(f"Betriebseinnahmen {heute.year}")
        inland_karte = text.index(f"Davon Inland {heute.year} — § 19-Grenze")
        assert gesamt < text.index("350,00 €") < inland_karte < text.index("100,00 €")
        assert "ohne eigene Rechnung 350,00 €" in text
        assert "davon 250,00 € nicht im § 19-Umsatz" in text
        # Ohne Ausgaben ist der Gewinn die Summe der Einnahmen.
        assert "350,00 €" in text[text.index(f"Gewinn {heute.year}"):]

    def test_uebersicht_zeigt_ausgaben_und_gewinn(self, klient, tmp_path):
        """Betriebsausgaben und Gewinn stehen neben den Einnahmen -- aus derselben EÜR."""
        from tests.test_euer import _beleg

        conn = datenbank(tmp_path)
        heute = date.today()
        inland = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel ="
                              " 'EINNAHME_HONORAR_INLAND'").fetchone()[0]
        klient.post("/einnahmen", data={
            "zufluss_am": heute.strftime("%d.%m.%Y"), "betrag": "350,00",
            "zahler": "Beispiel GmbH", "kategorie_id": str(inland)})
        _beleg(conn, "UEBRIGE", 7550, datum=heute.isoformat(), bezahlt=heute.isoformat())
        conn.commit()
        text = " ".join(klient.get("/").text.split())
        assert text.index(f"Betriebsausgaben {heute.year}") < text.index("75,50 €")
        assert text.index(f"Gewinn {heute.year}") < text.index("274,50 €")
        assert euer.auswertung(conn, heute.year).gewinn_cent == 27450
        # Mehr Ausgaben als Einnahmen: die Karte heisst "Verlust".
        _beleg(conn, "UEBRIGE", 100000, datum=heute.isoformat(), bezahlt=heute.isoformat())
        conn.commit()
        text = " ".join(klient.get("/").text.split())
        assert f"Verlust {heute.year}" in text and f"Gewinn {heute.year}" not in text

    @pytest.mark.parametrize("pfad", ["/einnahmen/abc/loeschen", "/einnahmen/999/wiederherstellen",
                                      "/einnahmen/0/kategorie"])
    def test_unbekannte_einnahme(self, klient, pfad):
        antwort = klient.post(pfad, data={"grund": "doppelt erfasst"}, follow_redirects=False)
        assert antwort.status_code == 303 and "fehler=" in antwort.headers["location"]

"""Regressionstests zu den Befunden der Fehleranalyse im Bereich Festschreiben.

Festschreiben, PDF, Zahlung, Storno, Rueckzahlung, Nummernkreis,
Rechnungsliste und Nebenlaeufigkeit. Je Befund mindestens ein Test, der das
alte Fehlverhalten nachstellt: 500 unter Last, Jahr 0026 im Rechnungsdatum,
toter PDF-Link, Storno-Nummer aus dem Vorjahreskreis, {MM} immer "01",
Ueberzahlung ohne Ausweg, Versandvermerk im Journal ohne Treffer, ueberlappende
Fusszeile, Gesamtbetrag auf jeder Seite, ASCII-Umschrift in Meldungen.
"""

import re
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import festschreiben as fs
from app.logik import nummernkreis
from app.logik import validierung as v
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "inhaber": "E. Muster",
    "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
    "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1", "reset_jaehrlich": "1",
    "rundung_modus": "AUFWAERTS", "rundung_intervall_min": "15",
    "beginn_am": "2020-01-01",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "email": "buchhaltung@mustermann.example", "typ": "UNTERNEHMER", "land": "DE",
    "leistungsort": "INLAND", "satz_euro": "90,00", "gruppierung": "PRO_TITEL", "aktiv": "1",
}

HEUTE = db.heute()
JAHR = int(HEUTE[:4])


@pytest.fixture
def daten(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
    return tmp_path / "daten"


@pytest.fixture
def klient(daten):
    with TestClient(erzeuge_app(daten / "steuer.sqlite"), raise_server_exceptions=False) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _db(daten) -> sqlite3.Connection:
    conn = sqlite3.connect(daten / "steuer.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _zeit(klient, daten, datum="2026-03-02", dauer="1h", titel="Beratung") -> int:
    antwort = klient.post("/zeiten", data={
        "kunde_id": 1, "titel": titel, "datum": datum, "dauer": dauer,
        "beschreibung": "", "modus": "STUNDEN", "grund": "",
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    conn = _db(daten)
    try:
        return int(conn.execute("SELECT MAX(id) FROM zeiteintrag").fetchone()[0])
    finally:
        conn.close()


def _entwurf(klient, zeit_ids, datum="2026-03-10", modus="PRO_TITEL"):
    return klient.post("/rechnung/neu", data={
        "kunde_id": 1, "datum": datum, "modus": modus, "einleitung": "", "schluss": "",
        "zeit": zeit_ids,
    }, follow_redirects=False)


def _entwurf_id(antwort) -> int:
    assert antwort.status_code == 303, antwort.text[:400]
    ort = antwort.headers["location"]
    assert "fehler=" not in ort, ort
    return int(re.search(r"/rechnung/(\d+)", ort).group(1))


def _fehler(antwort) -> str:
    assert antwort.status_code == 303, antwort.text[:400]
    return parse_qs(urlparse(antwort.headers["location"]).query).get("fehler", [""])[0]


def _version_und_codes(klient, rid) -> tuple[str, list[str], str]:
    seite = klient.get(f"/rechnung/{rid}").text
    version = re.search(r'name="version" value="(\d+)"', seite).group(1)
    codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
    return version, codes, seite


def _festschreiben(klient, rid):
    version, codes, _ = _version_und_codes(klient, rid)
    return klient.post(f"/rechnung/{rid}/festschreiben",
                       data={"version": version, "bestaetigt": codes},
                       follow_redirects=False)


def _festgeschrieben(klient, daten, datum="2026-03-10", zeit_datum="2026-03-02") -> int:
    rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten, datum=zeit_datum)], datum=datum))
    antwort = _festschreiben(klient, rid)
    assert antwort.status_code == 303, antwort.text[:400]
    assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
    return rid


def _storno_id(klient, rid, grund="Probe", freigeben="1") -> int:
    antwort = klient.post(f"/rechnung/{rid}/storno",
                          data={"grund": grund, "zeiten_freigeben": freigeben},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    ort = antwort.headers["location"]
    assert "fehler=" not in ort, ort
    return int(ort.rsplit("/", 1)[1])


def _rechnung(daten, rid) -> sqlite3.Row:
    conn = _db(daten)
    try:
        return conn.execute(
            "SELECT r.*, v.bezahlt_cent, v.offen_cent, v.zahlstatus FROM rechnung r"
            " JOIN v_rechnung_status v ON v.id = r.id WHERE r.id = ?", (rid,)
        ).fetchone()
    finally:
        conn.close()


def _journal(daten, aktion, objekt_id) -> int:
    conn = _db(daten)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM journal WHERE aktion = ? AND objekt_id = ?",
            (aktion, objekt_id),
        ).fetchone()[0])
    finally:
        conn.close()


# ------------------------------------------------------- 1: Nebenlaeufigkeit

class TestNebenlaeufigkeit:
    """Die SQLite-Verbindung war an den Thread gebunden, der sie geoeffnet
    hat. FastAPI oeffnet, nutzt und schliesst sie aber in beliebigen Threads
    des Pools -- unter Last endete jede zweite Anfrage mit 500."""

    def _parallel(self, anzahl, arbeit):
        ergebnisse: list = [None] * anzahl
        start = threading.Barrier(anzahl)

        def lauf(i):
            start.wait()
            ergebnisse[i] = arbeit(i)

        faeden = [threading.Thread(target=lauf, args=(i,)) for i in range(anzahl)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join()
        return ergebnisse

    def test_acht_gleichzeitige_gets_liefern_nur_200(self, klient, daten):
        _festgeschrieben(klient, daten)
        for _ in range(3):
            codes = self._parallel(8, lambda _: klient.get("/rechnungen").status_code)
            assert codes == [200] * 8, codes

    def test_vier_gleichzeitige_festschreibungen_eine_nummer(self, klient, daten):
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        version, codes, _ = _version_und_codes(klient, rid)
        formular = {"version": version, "bestaetigt": codes}

        antworten = self._parallel(
            4, lambda _: klient.post(f"/rechnung/{rid}/festschreiben", data=formular,
                                     follow_redirects=False),
        )
        assert all(a.status_code == 303 for a in antworten), [a.status_code for a in antworten]
        meldungen = [_fehler(a) for a in antworten]
        ohne_fehler = [m for m in meldungen if not m]
        assert len(ohne_fehler) == 1, meldungen
        for meldung in meldungen:
            if meldung:
                assert "bereits festgeschrieben" in meldung, meldung
        conn = _db(daten)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM rechnung WHERE nummer IS NOT NULL"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT naechste_seq FROM nummernkreis WHERE jahr = 2026"
            ).fetchone()[0] == 2
        finally:
            conn.close()

    def test_verbinde_threadfrei_ist_nur_fuer_die_oberflaeche(self, tmp_path):
        """Die Vorbelegung bleibt threadgebunden: Skripte und Tests sollen den
        Fehler weiter sehen, wenn sie eine Verbindung ueber Threads teilen."""
        gebunden = db.verbinde(tmp_path / "a.sqlite")
        frei = db.verbinde(tmp_path / "b.sqlite", threadfrei=True)
        fehler: list[Exception] = []

        def nutze(conn):
            try:
                conn.execute("SELECT 1").fetchone()
            except Exception as e:  # noqa: BLE001 -- der Test will genau den Typ sehen
                fehler.append(e)

        for conn in (gebunden, frei):
            faden = threading.Thread(target=nutze, args=(conn,))
            faden.start()
            faden.join()
        gebunden.close()
        frei.close()
        assert len(fehler) == 1 and isinstance(fehler[0], sqlite3.ProgrammingError)


# --------------------------------------------- 2: Rechnungsdatum plausibel

class TestRechnungsdatum:
    """Jahr "26" im Datumsfeld getippt: 0026-03-10 besteht date.fromisoformat()
    und wurde festgeschrieben -- Nummer RE-0026-0001, eigener Kreis "26",
    PDF-Ordner "26", unveraenderlich. 9999-12-31 lief in einen OverflowError."""

    def test_jahr_0026_wird_beim_anlegen_abgewiesen(self, klient, daten):
        zid = _zeit(klient, daten)
        antwort = _entwurf(klient, [zid], datum="0026-03-10")
        # 422 mit erhaltenen Eingaben statt Umleitung (letzte Korrekturrunde).
        assert antwort.status_code == 422
        assert "kein brauchbares Rechnungsdatum" in antwort.text
        assert "2000" in antwort.text
        conn = _db(daten)
        try:
            assert conn.execute("SELECT COUNT(*) FROM rechnung").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM nummernkreis WHERE jahr = 26"
                                ).fetchone()[0] == 0
        finally:
            conn.close()

    @pytest.mark.parametrize("datum", ["9999-12-31", "9999-12-20", "0001-01-01"])
    def test_kalenderende_gibt_meldung_statt_500(self, klient, daten, datum):
        zid = _zeit(klient, daten)
        antwort = _entwurf(klient, [zid], datum=datum)
        # Beim Anlegen und im Kopf-Formular 422 mit erhaltenen Eingaben statt
        # Umleitung (letzte Korrekturrunde).
        assert antwort.status_code == 422
        assert "kein brauchbares Rechnungsdatum" in antwort.text
        rid = _entwurf_id(_entwurf(klient, [zid]))
        kopf = klient.post(f"/rechnung/{rid}/kopf",
                           data={"datum": datum, "betreff": "Mein Betreff"},
                           follow_redirects=False)
        assert kopf.status_code == 422
        assert "kein brauchbares Rechnungsdatum" in kopf.text
        assert 'value="Mein Betreff"' in kopf.text, "die Eingabe bleibt im Formular"
        assert _rechnung(daten, rid)["datum"] == "2026-03-10", "gespeichert wird nichts"

    def test_naechstes_jahr_geht_uebernaechstes_nicht(self, klient, daten):
        zid = _zeit(klient, daten)
        erlaubt = _entwurf(klient, [zid], datum=f"{JAHR + 1}-06-01")
        assert "fehler=" not in erlaubt.headers["location"]
        rid = _entwurf_id(erlaubt)
        zu_spaet = klient.post(f"/rechnung/{rid}/kopf",
                               data={"datum": f"{JAHR + 2}-01-01", "betreff": "x"},
                               follow_redirects=False)
        assert zu_spaet.status_code == 422
        assert "kein brauchbares Rechnungsdatum" in zu_spaet.text

    def test_rote_regel_blockiert_das_festschreiben(self, klient, daten):
        """Ein Altbestand oder ein direkter Schreibzugriff: das Datum steht
        schon im Entwurf. Dann muss die Pruefung ROT sein, nicht GELB."""
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        conn = _db(daten)
        conn.execute("UPDATE rechnung SET datum = '0026-03-10' WHERE id = ?", (rid,))
        conn.commit()
        conn.close()
        seite = klient.get(f"/rechnung/{rid}").text
        assert "1 Punkt blockiert das Festschreiben" in seite
        assert "10.03.0026" in seite and "Tippfehler im Jahr" in seite
        version = re.search(r'name="version" value="(\d+)"', seite).group(1)
        codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
        antwort = klient.post(f"/rechnung/{rid}/festschreiben",
                              data={"version": version, "bestaetigt": codes},
                              follow_redirects=False)
        assert "Festschreiben nicht möglich" in _fehler(antwort)
        assert _rechnung(daten, rid)["status"] == "ENTWURF"

    def test_regel_direkt(self, conn, betrieb, kunde_id):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
            " erfasst_am) VALUES (?, 'Beratung', '2026-03-02', 60, 'STUNDEN', ?)",
            (kunde_id, db.jetzt_utc()),
        ).lastrowid
        rid = fs.entwurf_anlegen(conn, kunde_id, [zid], datum="2026-03-10")
        assert "R22" not in {b.code for b in v.pruefe(conn, rid)}
        conn.execute("UPDATE rechnung SET datum = '0026-03-10' WHERE id = ?", (rid,))
        befunde = {b.code: b for b in v.pruefe(conn, rid)}
        assert befunde["R22"].stufe == v.ROT
        with pytest.raises(fs.FestschreibenFehler, match="Tippfehler im Jahr"):
            fs.festschreiben(conn, rid)
        zid2 = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
            " erfasst_am) VALUES (?, 'Beratung', '2026-03-03', 60, 'STUNDEN', ?)",
            (kunde_id, db.jetzt_utc()),
        ).lastrowid
        with pytest.raises(fs.FestschreibenFehler, match="brauchbares Rechnungsdatum"):
            fs.entwurf_anlegen(conn, kunde_id, [zid2], datum="9999-12-31")
        assert conn.execute("SELECT rechnung_id FROM zeiteintrag WHERE id = ?", (zid2,)
                            ).fetchone()[0] is None, "zurueckgerollt, die Zeit bleibt frei"


# ------------------------------------------------------ 3: PDF-Datei fehlt

class TestPdfFehlt:
    def test_geloeschte_datei_zeigt_den_ausweg(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        pfad = daten / _rechnung(daten, rid)["pdf_pfad"]
        assert pfad.exists()
        alter_hash = _rechnung(daten, rid)["pdf_sha256"]
        pfad.unlink()

        seite = klient.get(f"/rechnung/{rid}").text
        assert "Die PDF-Datei fehlt auf der Platte" in seite
        assert "PDF jetzt erzeugen" in seite
        assert "PDF öffnen" not in seite
        assert ">PDF<" not in klient.get("/rechnungen").text, "kein toter Link in der Liste"

        antwort = klient.post(f"/rechnung/{rid}/pdf", follow_redirects=False)
        assert antwort.status_code == 303 and "fehler=" not in antwort.headers["location"]
        assert pfad.exists()
        assert _rechnung(daten, rid)["pdf_sha256"] != alter_hash
        seite = klient.get(f"/rechnung/{rid}").text
        assert "PDF öffnen" in seite and "fehlt auf der Platte" not in seite
        assert ">PDF<" in klient.get("/rechnungen").text

    def test_vorhandenes_pdf_wird_nicht_ueberschrieben(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        vorher = _rechnung(daten, rid)["pdf_sha256"]
        antwort = klient.post(f"/rechnung/{rid}/pdf", follow_redirects=False)
        assert "Das PDF ist vorhanden" in _fehler(antwort)
        assert "versendetes Exemplar" in _fehler(antwort)
        assert _rechnung(daten, rid)["pdf_sha256"] == vorher
        assert _journal(daten, "PDF", rid) == 1

    def test_logik_direkt(self, conn, betrieb, kunde_id, tmp_path):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
            " erfasst_am) VALUES (?, 'Beratung', '2026-03-02', 60, 'STUNDEN', ?)",
            (kunde_id, db.jetzt_utc()),
        ).lastrowid
        rid = fs.entwurf_anlegen(conn, kunde_id, [zid])
        ergebnis = fs.festschreiben(conn, rid, belegordner=tmp_path)
        assert ergebnis.pdf_fehler is None
        with pytest.raises(fs.FestschreibenFehler, match="vorhanden"):
            fs.pdf_nachziehen(conn, rid, tmp_path)
        Path(ergebnis.pdf_pfad).unlink()
        assert fs.pdf_nachziehen(conn, rid, tmp_path).pdf_fehler is None


# ------------------------------------------- 4: Storno-Nummer aus dem Jahr

class TestStornoNummer:
    def test_korrektur_zieht_aus_dem_kreis_ihres_ausstellungsjahres(self, klient, daten):
        rid = _festgeschrieben(klient, daten, datum="2024-11-10", zeit_datum="2024-11-02")
        assert _rechnung(daten, rid)["nummer"] == "RE-2024-0001"
        sid = _storno_id(klient, rid, freigeben="0")
        storno = _rechnung(daten, sid)
        assert storno["datum"] == HEUTE
        assert storno["nummer_jahr"] == JAHR
        assert storno["nummer"].startswith(f"RE-{JAHR}-")
        conn = _db(daten)
        try:
            assert conn.execute(
                "SELECT naechste_seq FROM nummernkreis WHERE jahr = 2024"
            ).fetchone()[0] == 2, "der Kreis 2024 bleibt unberuehrt"
        finally:
            conn.close()
        liste_2024 = klient.get("/rechnungen?jahr=2024").text
        assert "RE-2024-0001" in liste_2024 and storno["nummer"] not in liste_2024
        assert storno["nummer"] in klient.get(f"/rechnungen?jahr={JAHR}").text


# ----------------------------------------------------- 5: Platzhalter {MM}

class TestMonatImFormat:
    def test_formatiere_und_vorschau(self, conn):
        assert nummernkreis.formatiere("{JJ}{MM}-{NN}", 2026, 1, monat=5) == "2605-01"
        nummernkreis.sichere_kreis(conn, 2026, vorlage="{JJ}{MM}-{NN}", reset_jaehrlich=True)
        assert nummernkreis.vorschau(conn, 2026, monat=3) == "2603-01"
        assert nummernkreis.ziehe_nummer(conn, 2026, monat=5).text == "2605-01"
        assert nummernkreis.jahr_monat("2026-05-10") == (2026, 5)

    def test_mairechnung_heisst_2605(self, daten):
        with TestClient(erzeuge_app(daten / "steuer.sqlite"), raise_server_exceptions=False) as c:
            c.post("/einrichtung", data={**EINRICHTUNG, "nummer_format": "{JJ}{MM}-{NN}"})
            c.post("/kunden", data=KUNDE)
            rid = _entwurf_id(_entwurf(c, [_zeit(c, daten, datum="2026-05-02")],
                                       datum="2026-05-10"))
            assert "<code>2605-01</code>" in c.get(f"/rechnung/{rid}").text
            assert _festschreiben(c, rid).status_code == 303
            assert _rechnung(daten, rid)["nummer"] == "2605-01"
            # Der Assistent kennt noch kein Datum und zeigt den heutigen Monat.
            heute = f"{JAHR % 100:02d}{int(HEUTE[5:7]):02d}-"
            assert heute in c.get("/rechnung/neu?kunde=1").text


# --------------------------------------------------------- 6: Ueberzahlung

class TestUeberzahlung:
    def test_zu_viel_erhalten_und_rueckerstattung(self, klient, daten):
        rid = _festgeschrieben(klient, daten)   # 90,00 €
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-20", "betrag_euro": "150,00", "erwartet_bezahlt": "0"})
        seite = klient.get(f"/rechnung/{rid}").text
        assert "60,00 € zu viel erhalten" in seite
        assert "Rückerstattung erfassen" in seite
        assert "Noch offen" not in seite
        assert 'placeholder="-' not in seite
        assert 'name="rueckerstattung" value="1"' in seite

        zu_viel = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": "2026-03-21", "betrag_euro": "70,00", "rueckerstattung": "1",
            "erwartet_bezahlt": "15000"}, follow_redirects=False)
        assert "höchstens die Überzahlung: 60,00 €" in _fehler(zu_viel)

        antwort = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": "2026-03-21", "betrag_euro": "60,00", "rueckerstattung": "1",
            "erwartet_bezahlt": "15000"}, follow_redirects=False)
        assert "fehler=" not in antwort.headers["location"]
        r = _rechnung(daten, rid)
        assert r["bezahlt_cent"] == 9000 and r["offen_cent"] == 0
        conn = _db(daten)
        try:
            assert conn.execute(
                "SELECT betrag_cent FROM zahlung WHERE rechnung_id = ? ORDER BY id", (rid,)
            ).fetchall()[-1][0] == -6000
            assert conn.execute(
                "SELECT begruendung FROM journal WHERE aktion = 'ZAHLUNG' AND objekt_id = ?"
                " ORDER BY id DESC LIMIT 1", (rid,)
            ).fetchone()[0] == "Rückerstattung Überzahlung"
        finally:
            conn.close()
        seite = klient.get(f"/rechnung/{rid}").text
        assert "zu viel erhalten" not in seite and "Rückerstattung erfassen" not in seite
        assert "90,00 €" in klient.get("/").text

    def test_leer_im_rueckerstattungsformular_heisst_alles(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung", data={"datum": "2026-03-20", "betrag_euro": "100"})
        klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": "2026-03-21", "betrag_euro": "", "rueckerstattung": "1"})
        assert _rechnung(daten, rid)["offen_cent"] == 0

    def test_negativer_betrag_ohne_ueberzahlung(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/zahlung",
                              data={"datum": "2026-03-20", "betrag_euro": "-10,00"},
                              follow_redirects=False)
        assert _fehler(antwort) == (
            "Ein negativer Zahlbetrag ist nur als Rückerstattung einer Überzahlung möglich."
        )
        assert _rechnung(daten, rid)["bezahlt_cent"] == 0


# ------------------------------------------------- 7: Als versendet vermerken

class TestVersendet:
    def test_stornierte_rechnung_ohne_formular_und_ohne_journal(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        _storno_id(klient, rid)
        seite = klient.get(f"/rechnung/{rid}").text
        assert f'action="/rechnung/{rid}/versendet"' not in seite
        antwort = klient.post(f"/rechnung/{rid}/versendet", data={"datum": "2026-03-12"},
                              follow_redirects=False)
        assert _fehler(antwort) == (
            "Nur eine festgeschriebene Rechnung kann als versendet vermerkt werden."
        )
        assert _rechnung(daten, rid)["versendet_am"] is None
        assert _journal(daten, "VERSENDET", rid) == 0

    def test_entwurf_korrektur_und_unbekannte_nummer(self, klient, daten):
        """Die Rechnungskorrektur ist hier nicht mehr ausgeschlossen: sie ist
        festgeschrieben und muss dem Kunden zugehen -- die Sperre war eine
        Regression (tests/test_rechnung_rest.py, TestKorrekturVersendet)."""
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        antwort = klient.post(f"/rechnung/{rid}/versendet", data={}, follow_redirects=False)
        assert "Nur eine festgeschriebene Rechnung" in _fehler(antwort)
        assert _journal(daten, "VERSENDET", rid) == 0
        fremd = klient.post("/rechnung/999999/versendet", data={}, follow_redirects=False)
        assert fremd.status_code == 303 and "gibt es nicht" in _fehler(fremd)
        assert _journal(daten, "VERSENDET", 999999) == 0

    def test_versanddatum_plausibel(self, klient, daten):
        rid = _festgeschrieben(klient, daten)          # Rechnungsdatum 10.03.2026
        vorher = klient.post(f"/rechnung/{rid}/versendet", data={"datum": "2020-01-01"},
                             follow_redirects=False)
        assert "vor dem Rechnungsdatum (10.03.2026)" in _fehler(vorher)
        zukunft = klient.post(f"/rechnung/{rid}/versendet", data={"datum": "2030-01-01"},
                              follow_redirects=False)
        assert "in der Zukunft" in _fehler(zukunft)
        assert _rechnung(daten, rid)["versendet_am"] is None
        gut = klient.post(f"/rechnung/{rid}/versendet", data={"datum": "2026-03-12"},
                          follow_redirects=False)
        assert "fehler=" not in gut.headers["location"]
        assert _rechnung(daten, rid)["versendet_am"] == "2026-03-12"
        assert _journal(daten, "VERSENDET", rid) == 1


# -------------------------------------------------- 8 und 9: PDF-Layout

def _zeiten_direkt(conn, kunde_id, anzahl, titel="Beratung", minuten=60):
    return [
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
            " erfasst_am) VALUES (?, ?, '2026-03-02', ?, 'STUNDEN', ?)",
            (kunde_id, f"{titel} {i + 1:02d}" if anzahl > 1 else titel, minuten,
             db.jetzt_utc()),
        ).lastrowid
        for i in range(anzahl)
    ]


class TestPdfLayout:
    @pytest.fixture
    def plumber(self):
        return pytest.importorskip("pdfplumber")

    @pytest.fixture
    def betrieb_einzeilig(self, conn, betrieb, kunde_id):
        """Eine Fusszeile, die bis zum rechten Rand reicht: Inhaber, Telefon
        und E-Mail, aber keine USt-IdNr -- so lag der Seitenzaehler ueber
        der E-Mail-Adresse."""
        conn.execute(
            "UPDATE betrieb SET inhaber = 'E. Muster', telefon = '040 123',"
            " email = 'post@kleingewerbe.example', beginn_am = '2020-01-01',"
            " iban = 'DE02120300000000202051' WHERE id = 1"
        )
        conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
        return kunde_id

    @staticmethod
    def _zeilen_im_fuss(seite) -> dict[int, list[dict]]:
        """Die Woerter der untersten 25 mm, gruppiert nach Zeile.

        Woerter, nicht Zeichen: zwischen zwei Buchstaben eines Wortes
        ueberlappen sich die Kaestchen durch Kerning um Bruchteile eines
        Punkts, und das ist kein Ueberdruck.
        """
        grenze = float(seite.height) - 25 * 72 / 25.4
        zeilen: dict[int, list[dict]] = {}
        for wort in seite.extract_words():
            if wort["top"] >= grenze:
                zeilen.setdefault(round(wort["top"]), []).append(wort)
        return zeilen

    def test_seitenzaehler_ueberdruckt_die_fusszeile_nicht(
        self, conn, betrieb_einzeilig, tmp_path, plumber
    ):
        kid = betrieb_einzeilig
        rid = fs.entwurf_anlegen(conn, kid, _zeiten_direkt(conn, kid, 1))
        ergebnis = fs.festschreiben(conn, rid, belegordner=tmp_path)
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
        with plumber.open(ergebnis.pdf_pfad) as pdf:
            zeilen = self._zeilen_im_fuss(pdf.pages[0])
        text = " ".join(
            " ".join(w["text"] for w in sorted(zeile, key=lambda w: w["x0"]))
            for zeile in zeilen.values()
        )
        assert "Seite" in text and "post@kleingewerbe.example" in text, text
        for zeile in zeilen.values():
            zeile.sort(key=lambda w: w["x0"])
            for a, b in zip(zeile, zeile[1:], strict=False):
                assert a["x1"] <= b["x0"] + 0.5, (
                    f"„{a['text']}“ und „{b['text']}“ liegen übereinander"
                )

    def test_gesamtbetrag_nur_einmal_am_tabellenende(
        self, conn, betrieb_einzeilig, tmp_path, plumber
    ):
        kid = betrieb_einzeilig
        conn.execute("UPDATE kunde SET gruppierung_default = 'PRO_EINTRAG' WHERE id = ?", (kid,))
        rid = fs.entwurf_anlegen(conn, kid, _zeiten_direkt(conn, kid, 46, "Position"),
                                 modus="PRO_EINTRAG")
        ergebnis = fs.festschreiben(conn, rid, belegordner=tmp_path)
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
        with plumber.open(ergebnis.pdf_pfad) as pdf:
            seiten = [s.extract_text() or "" for s in pdf.pages]
        assert len(seiten) >= 3, "die Positionsliste soll ueber mehrere Seiten gehen"
        text = "\n".join(seiten)
        assert text.count("Gesamtbetrag") == 1, [s.count("Gesamtbetrag") for s in seiten]
        # Und zwar am Ende der Tabelle: nach der letzten Position, nicht
        # unter der ersten.
        assert "Position 46" in text
        assert text.index("Gesamtbetrag") > text.index("Position 46")


# ------------------------------------- 10: Korrektur nicht stornieren/bezahlen

class TestKorrekturdokument:
    def test_storno_eines_stornos(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        sid = _storno_id(klient, rid)
        antwort = klient.post(f"/rechnung/{sid}/storno",
                              data={"grund": "y", "zeiten_freigeben": "1"},
                              follow_redirects=False)
        assert _fehler(antwort) == "Eine Rechnungskorrektur kann nicht storniert werden."
        conn = _db(daten)
        try:
            assert conn.execute("SELECT COUNT(*) FROM rechnung WHERE typ = 'STORNO'"
                                ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_zahlung_auf_die_korrektur(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        sid = _storno_id(klient, rid)
        antwort = klient.post(f"/rechnung/{sid}/zahlung",
                              data={"datum": "2026-03-22", "betrag_euro": "1,00"},
                              follow_redirects=False)
        assert _fehler(antwort) == "Auf eine Rechnungskorrektur geht kein Geld ein."
        assert _rechnung(daten, sid)["bezahlt_cent"] == 0
        assert "0,00 €" in klient.get("/").text


# --------------------------------------------------------- 11: Rechnungsliste

class TestListe:
    def _zeile(self, html, nummer) -> list[str]:
        zeile = re.search(rf"<tr>\s*<td class=\"mono\"><a[^>]*>{nummer}</a>.*?</tr>", html, re.S)
        assert zeile, nummer
        return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", z)).strip()
                for z in re.findall(r"<td[^>]*>(.*?)</td>", zeile.group(0), re.S)]

    def test_storniert_ohne_faellig_und_offen(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        _storno_id(klient, rid)
        zellen = self._zeile(klient.get("/rechnungen").text, "RE-2026-0001")
        assert zellen[3] == "—", "Faellig"
        assert zellen[5] == "—", "Offen"
        assert "Storniert" in zellen[6]

    def test_storniert_mit_offener_rueckzahlung(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung", data={"datum": "2026-03-20", "betrag_euro": ""})
        _storno_id(klient, rid, freigeben="0")
        zellen = self._zeile(klient.get("/rechnungen").text, "RE-2026-0001")
        assert zellen[3] == "—"
        assert zellen[5] == "Rückzahlung offen 90,00 €"
        klient.post(f"/rechnung/{rid}/rueckzahlung",
                    data={"datum": "2026-03-28", "betrag_euro": ""})
        assert self._zeile(klient.get("/rechnungen").text, "RE-2026-0001")[5] == "—"

    def test_entwuerfe_stehen_oben(self, klient, daten):
        for jahr in (2024, 2025):
            _festgeschrieben(klient, daten, datum=f"{jahr}-11-10", zeit_datum=f"{jahr}-11-02")
        _festgeschrieben(klient, daten)
        _entwurf_id(_entwurf(klient, [_zeit(klient, daten, datum="2026-03-05")]))
        nummern = re.findall(r'<td class="mono"><a href="/rechnung/\d+">([^<]+)</a>',
                             klient.get("/rechnungen").text)
        assert nummern[0] == "Entwurf"
        assert nummern[1:] == ["RE-2026-0001", "RE-2025-0001", "RE-2024-0001"]


# ---------------------------------- 12: Festschreiben auf Festgeschriebenem

class TestBereitsFestgeschrieben:
    def test_meldung_nennt_den_status_nicht_die_hinweise(self, klient, daten):
        rid = _festgeschrieben(klient, daten, zeit_datum="2026-03-02")
        _zeit(klient, daten, datum="2026-03-04")          # erzeugt G12 auf der Rechnung
        version = _rechnung(daten, rid)["version"]
        antwort = klient.post(f"/rechnung/{rid}/festschreiben", data={"version": version},
                              follow_redirects=False)
        assert _fehler(antwort) == (
            "Die Rechnung ist bereits festgeschrieben. Bitte die Seite neu laden."
        )
        sid = _storno_id(klient, rid)
        antwort = klient.post(f"/rechnung/{sid}/festschreiben", data={"version": "1"},
                              follow_redirects=False)
        assert "bereits festgeschrieben" in _fehler(antwort)
        assert "abgehakt" not in _fehler(antwort)


# ---------------------------------------------- 13 und 16: Meldungen, Datum

class TestZahlungsmeldungen:
    def test_umlaute(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung", data={"datum": "2026-03-20", "betrag_euro": ""})
        voll = klient.post(f"/rechnung/{rid}/zahlung",
                           data={"datum": "2026-03-21", "betrag_euro": ""},
                           follow_redirects=False)
        assert _fehler(voll) == "Die Rechnung ist bereits vollständig bezahlt."
        veraltet = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": "2026-03-21", "betrag_euro": "1", "erwartet_bezahlt": "0"},
            follow_redirects=False)
        assert "bitte prüfen" in _fehler(veraltet)
        falsch = klient.post(f"/rechnung/{rid}/rueckzahlung",
                             data={"datum": "2026-03-21", "betrag_euro": "1"},
                             follow_redirects=False)
        assert _fehler(falsch) == "Eine Rückzahlung gibt es nur zu einer stornierten Rechnung."
        _storno_id(klient, rid, freigeben="0")
        eingang = klient.post(f"/rechnung/{rid}/zahlung",
                              data={"datum": "2026-03-22", "betrag_euro": "1"},
                              follow_redirects=False)
        assert "nur eine Rückzahlung" in _fehler(eingang)
        zu_viel = klient.post(f"/rechnung/{rid}/rueckzahlung",
                              data={"datum": "2026-03-22", "betrag_euro": "200"},
                              follow_redirects=False)
        assert "höchstens, was eingegangen ist: 90,00 €" in _fehler(zu_viel)
        nichts = klient.post(f"/rechnung/{rid}/rueckzahlung",
                             data={"datum": "2026-03-22", "betrag_euro": "0"},
                             follow_redirects=False)
        assert _fehler(nichts) == "Es ist nichts zurückzuzahlen."
        meldungen = [_fehler(a) for a in (voll, veraltet, falsch, eingang, zu_viel, nichts)]
        for umschrift in ("ae", "oe", "ue"):
            assert not any(
                re.search(rf"[a-z]{umschrift}[a-z]", m) and umschrift != "ue" for m in meldungen
            ), meldungen

    @pytest.mark.parametrize("route", ["zahlung", "rueckzahlung"])
    def test_zahlungsdatum_plausibel(self, klient, daten, route):
        rid = _festgeschrieben(klient, daten)
        zukunft = klient.post(f"/rechnung/{rid}/{route}",
                              data={"datum": "2099-01-01", "betrag_euro": "1,00"},
                              follow_redirects=False)
        assert "in der Zukunft" in _fehler(zukunft) and "§ 11 EStG" in _fehler(zukunft)
        frueher = klient.post(f"/rechnung/{rid}/{route}",
                              data={"datum": "0001-01-01", "betrag_euro": "1,00"},
                              follow_redirects=False)
        assert "vor dem Jahr 2000" in _fehler(frueher)
        assert _rechnung(daten, rid)["bezahlt_cent"] == 0

    def test_englische_schreibweise(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/zahlung",
                              data={"datum": "2026-03-20", "betrag_euro": "1,234.56"},
                              follow_redirects=False)
        assert "fehler=" not in antwort.headers["location"]
        assert _rechnung(daten, rid)["bezahlt_cent"] == 123456

    def test_unbekannte_zahlungsart(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        for route in ("zahlung", "rueckzahlung"):
            antwort = klient.post(f"/rechnung/{rid}/{route}",
                                  data={"datum": "2026-03-20", "betrag_euro": "1,00",
                                        "art": "BITCOIN"}, follow_redirects=False)
            assert _fehler(antwort) == "Unbekannte Zahlungsart."
            assert "CHECK" not in _fehler(antwort)
        assert _rechnung(daten, rid)["bezahlt_cent"] == 0


# ------------------------------------------- 15: Storno mit PDF-Fehler

class TestStornoPdfFehler:
    def test_meldung_wie_beim_festschreiben(self, klient, daten, monkeypatch):
        from app import pdf as pdfmodul

        rid = _festgeschrieben(klient, daten)

        def kaputt(*_, **__):
            raise pdfmodul.PdfFehler("Pango fehlt")

        monkeypatch.setattr(pdfmodul, "rendere", kaputt)
        antwort = klient.post(f"/rechnung/{rid}/storno",
                              data={"grund": "x", "zeiten_freigeben": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        ort = antwort.headers["location"]
        sid = int(re.search(r"/rechnung/(\d+)", ort).group(1))
        assert sid != rid
        meldung = _fehler(antwort)
        assert meldung.startswith("Die Korrektur RE-")
        assert ("ist festgeschrieben, aber das PDF konnte nicht erzeugt werden: "
                "Pango fehlt") in meldung
        assert _rechnung(daten, sid)["pdf_pfad"] is None
        assert "PDF jetzt erzeugen" in klient.get(f"/rechnung/{sid}").text


# -------------------------------------------- 17: Zahl jenseits von 64 Bit

class TestRiesigeNummer:
    """Kennungen jenseits dessen, was eine Rechnung haben kann.

    Angepasst: Seit der zentralen Pfadpruefung in app/main.py beantwortet die
    Anwendung Kennungen mit mehr als 18 Stellen fuer ALLE Routen mit der
    deutschen 404-Seite; die Umleitung mit Meldung gilt weiter bis 18 Stellen.
    """

    RIESIG = "99999999999999999999"
    GROSS = "999999999999999999"  # 18 Stellen: erreicht die Route noch

    def test_get_leitet_mit_meldung_um(self, klient):
        antwort = klient.get(f"/rechnung/{self.GROSS}", follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith("/rechnungen?")
        assert "gibt es nicht" in _fehler(antwort)
        pdf = klient.get(f"/rechnung/{self.GROSS}/pdf", follow_redirects=False)
        assert pdf.status_code == 303 and "gibt es nicht" in _fehler(pdf)
        for pfad in (f"/rechnung/{self.RIESIG}", f"/rechnung/{self.RIESIG}/pdf"):
            antwort = klient.get(pfad, follow_redirects=False)
            assert antwort.status_code == 404
            assert "Diese Seite gibt es nicht" in antwort.text

    @pytest.mark.parametrize("route,formular", [
        ("festschreiben", {"version": "1"}), ("storno", {"grund": "x"}),
        ("zahlung", {"datum": "2026-03-20", "betrag_euro": "1"}),
        ("rueckzahlung", {"datum": "2026-03-20", "betrag_euro": "1"}),
        ("versendet", {"datum": "2026-03-20"}), ("kopf", {"datum": "2026-03-10"}),
        ("gruppierung", {"modus": "PRO_TAG"}), ("verwerfen", {}), ("pdf", {}),
    ])
    def test_post_routen(self, klient, route, formular):
        antwort = klient.post(f"/rechnung/{self.GROSS}/{route}", data=formular,
                              follow_redirects=False)
        assert antwort.status_code == 303, (route, antwort.status_code)
        assert "gibt es nicht" in _fehler(antwort)
        antwort = klient.post(f"/rechnung/{self.RIESIG}/{route}", data=formular,
                              follow_redirects=False)
        assert antwort.status_code == 404, (route, antwort.status_code)


# --------------------------------------------------------- 18: Mail-Link

class TestMailLink:
    def test_korrektur_heisst_im_mailtext_so(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        rechnung = re.findall(r'href="(mailto:[^"]+)"', klient.get(f"/rechnung/{rid}").text)
        assert rechnung and "anbei%20die%20Rechnung%20RE-2026-0001" in rechnung[0]
        sid = _storno_id(klient, rid)
        storno = re.findall(r'href="(mailto:[^"]+)"', klient.get(f"/rechnung/{sid}").text)
        assert storno and "anbei%20die%20Rechnungskorrektur%20RE-" in storno[0]
        assert "anbei%20die%20Rechnung%20RE-" not in storno[0]

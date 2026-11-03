"""Rechnungsablauf: korrigieren, vor dem Versand zuruecknehmen, bezahlt
markieren, Zahlung zuruecknehmen, Versandvermerk, Nummernluecken.

Die Wuensche dahinter: eine festgeschriebene Rechnung "nachtraeglich
bearbeiten" (geht nach § 146 Abs. 4 AO nicht -- also Korrigieren in einem
Schritt), eine nie verschickte Rechnung wieder loeswerden, ohne dass eine
Nummer verbrannt ist (ausdrueckliche Entscheidung des Nutzers), und "auf
bezahlt stellen" dort, wo man hinschaut. Die Tests pruefen jeweils auch die
Gegenseite: was ohne Freigabe gesperrt bleibt, was nicht zuruecknehmbar ist,
und dass EUeR und § 19-Zaehler nach einer Gegenbuchung stimmen.
"""

from __future__ import annotations

import json
import re
import sqlite3
import stat
import subprocess
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import db, pdf
from app.logik import euer, grenzwert, nummernkreis
from app.logik import festschreiben as fs
from app.logik import rechnungsablauf as ablauf
from app.main import erzeuge_app
from tests.conftest import braucht

HEUTE = db.heute()
GESTERN = (date.fromisoformat(HEUTE) - timedelta(days=1)).isoformat()
JAHR = int(HEUTE[:4])


def _de(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


def _text(html: str) -> str:
    """Die Seite mit zusammengezogenen Leerraeumen -- Vorlagen brechen Zeilen um."""
    return " ".join(html.split())


def _nummer(seq: int, jahr: int = JAHR) -> str:
    return f"RE-{jahr}-{seq:04d}"


# ------------------------------------------------------------------ Aufbau

@pytest.fixture
def schnelles_pdf(monkeypatch):
    """Ein PDF in Millisekunden statt Sekunden: die Daten als JSON in der Datei.

    Die Tests hier pruefen den Ablauf um das PDF herum (Verschieben, Hash,
    Inhalt der Vorlagendaten), nicht das Layout -- dafuer gibt es einen
    eigenen Test mit dem echten Rendering weiter unten.
    """
    def rendere(daten: dict, ziel: Path) -> str:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"%PDF-1.7\n" + json.dumps(daten, ensure_ascii=False).encode())
        return pdf.pruefsumme(ziel)

    monkeypatch.setattr(pdf, "rendere", rendere)


@pytest.fixture
def ordner(tmp_path) -> Path:
    return tmp_path / "daten"


@pytest.fixture
def kunde(conn, betrieb, kunde_id):
    conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
    conn.execute("UPDATE kunde SET satz_cent = 9000 WHERE id = ?", (kunde_id,))
    return kunde_id


def _zeit(conn, kunde_id, datum=HEUTE, minuten=60, titel="Beratung") -> int:
    return conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
        " erfasst_am) VALUES (?, ?, ?, ?, 'STUNDEN', ?)",
        (kunde_id, titel, datum, minuten, db.jetzt_utc()),
    ).lastrowid


def _festgeschrieben(conn, kunde_id, ordner, *, zeiten=2, **kopf) -> int:
    ids = [_zeit(conn, kunde_id, titel=f"Beratung {i}") for i in range(zeiten)]
    rid = fs.entwurf_anlegen(conn, kunde_id, ids, datum=HEUTE, **kopf)
    ergebnis = fs.festschreiben(conn, rid, belegordner=ordner)
    assert ergebnis.pdf_fehler is None
    return rid


def _zeile(conn, sql, werte=()):
    return conn.execute(sql, werte).fetchone()


def _naechste_seq(conn) -> int:
    return int(_zeile(conn, "SELECT naechste_seq FROM nummernkreis WHERE typ = 'RECHNUNG'")[0])


# ============================================================ Korrigieren

@pytest.mark.usefixtures("schnelles_pdf")
class TestKorrigieren:
    def test_storno_und_entwurf_mit_denselben_positionen(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner, betreff="Betreff A",
                               einleitung="Hallo", schluss="Danke", einzelnachweis=True)
        vorher = [dict(p) for p in conn.execute(
            "SELECT pos_nr, bezeichnung, menge_tausendstel, einzelpreis_cent, betrag_cent"
            " FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr", (rid,))]

        k = ablauf.korrigieren(conn, rid, "falscher Stundensatz", belegordner=ordner)

        assert k.storno.nummer == _nummer(2)
        assert _zeile(conn, "SELECT status FROM rechnung WHERE id = ?", (rid,))[0] == "STORNIERT"
        entwurf = _zeile(conn, "SELECT * FROM rechnung WHERE id = ?", (k.entwurf_id,))
        assert entwurf["status"] == "ENTWURF" and entwurf["nummer"] is None
        assert entwurf["ersetzt_rechnung_id"] == rid
        # Der Ersatz bekommt das Datum von heute. HEUTE steht seit dem Import
        # des Moduls fest; laeuft die Suite ueber Mitternacht, ist "heute"
        # schon der naechste Tag.
        assert entwurf["datum"] in (HEUTE, db.heute())
        assert (entwurf["betreff"], entwurf["einleitung"], entwurf["schluss"],
                entwurf["einzelnachweis"]) == ("Betreff A", "Hallo", "Danke", 1)
        nachher = [dict(p) for p in conn.execute(
            "SELECT pos_nr, bezeichnung, menge_tausendstel, einzelpreis_cent, betrag_cent"
            " FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr", (k.entwurf_id,))]
        assert nachher == vorher, "Positionen 1:1, keine zweite Rundung"
        original = _zeile(conn, "SELECT brutto_cent FROM rechnung WHERE id = ?", (rid,))[0]
        assert entwurf["brutto_cent"] == original
        # Die Zeiten haengen am Entwurf, mit der Herkunft aus dem Original.
        zeiten = conn.execute(
            "SELECT rechnung_id, freigegeben_aus_rechnung_id, satz_cent_snapshot"
            " FROM zeiteintrag").fetchall()
        assert {tuple(z) for z in zeiten} == {(k.entwurf_id, rid, 9000)}
        # Auch die Zuordnung Position <-> Zeit kam mit.
        assert _zeile(conn, "SELECT COUNT(*) FROM position_zeiteintrag pz JOIN rechnungsposition"
                      " p ON p.id = pz.position_id WHERE p.rechnung_id = ?",
                      (k.entwurf_id,))[0] == 2

    def test_ersatz_bekommt_beim_festschreiben_die_uebernaechste_nummer(
        self, conn, kunde, ordner
    ):
        rid = _festgeschrieben(conn, kunde, ordner)
        k = ablauf.korrigieren(conn, rid, "Tippfehler", belegordner=ordner)
        ergebnis = fs.festschreiben(conn, k.entwurf_id, belegordner=ordner)
        assert ergebnis.nummer == _nummer(3)
        daten = fs.pdf_daten(conn, k.entwurf_id)
        assert daten["ersetzt"]["nummer"] == _nummer(1)
        assert daten["ersetzt"]["storno_nummer"] == _nummer(2)

    def test_journal_nennt_storno_und_entwurf(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        k = ablauf.korrigieren(conn, rid, "Tippfehler", belegordner=ordner)
        eintrag = _zeile(conn, "SELECT * FROM journal WHERE aktion = 'KORREKTUR'")
        assert eintrag["objekt_id"] == rid and eintrag["begruendung"] == "Tippfehler"
        assert json.loads(eintrag["wert_neu"])["entwurf_id"] == k.entwurf_id
        assert _zeile(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'STORNO'")[0] == 1
        neu = _zeile(conn, "SELECT wert_neu FROM journal WHERE aktion = 'ENTWURF'"
                     " AND objekt_id = ?", (k.entwurf_id,))[0]
        assert json.loads(neu)["ersetzt"] == _nummer(1)

    def test_scheitert_der_entwurf_gilt_auch_das_storno_nicht(
        self, conn, kunde, ordner, monkeypatch
    ):
        rid = _festgeschrieben(conn, kunde, ordner)
        seq = _naechste_seq(conn)

        def kaputt(*_a, **_k):
            raise RuntimeError("Absicht")

        monkeypatch.setattr(ablauf, "_ersatzentwurf", kaputt)
        with pytest.raises(RuntimeError):
            ablauf.korrigieren(conn, rid, "Tippfehler", belegordner=ordner)
        assert _zeile(conn, "SELECT status FROM rechnung WHERE id = ?",
                      (rid,))[0] == "FESTGESCHRIEBEN"
        assert _zeile(conn, "SELECT COUNT(*) FROM rechnung")[0] == 1
        assert _naechste_seq(conn) == seq, "keine Nummer verbraucht"
        assert not list((ordner / "ausgangsrechnungen").rglob(f"{_nummer(2)}*"))
        assert _zeile(conn, "SELECT COUNT(*) FROM zeiteintrag WHERE rechnung_id = ?",
                      (rid,))[0] == 2

    def test_doppelklick_erzeugt_kein_zweites_storno(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        ablauf.korrigieren(conn, rid, "Tippfehler", belegordner=ordner)
        with pytest.raises(fs.FestschreibenFehler, match="inzwischen storniert"):
            ablauf.korrigieren(conn, rid, "Tippfehler", belegordner=ordner)
        assert _zeile(conn, "SELECT COUNT(*) FROM rechnung WHERE typ = 'STORNO'")[0] == 1
        assert _zeile(conn, "SELECT COUNT(*) FROM rechnung WHERE status = 'ENTWURF'")[0] == 1

    def test_ohne_grund_und_auf_entwurf_abgewiesen(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        with pytest.raises(fs.FestschreibenFehler, match="Begründung"):
            ablauf.korrigieren(conn, rid, "  ", belegordner=ordner)
        entwurf = fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde)], datum=HEUTE)
        with pytest.raises(fs.FestschreibenFehler, match="Entwurf"):
            ablauf.korrigieren(conn, entwurf, "x", belegordner=ordner)

    def test_bezahltes_original_behaelt_seine_zahlung(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, ?, 18000)",
                     (rid, HEUTE))
        k = ablauf.korrigieren(conn, rid, "Tippfehler", belegordner=ordner)
        assert k.bezahlt_cent == 18000
        stand = _zeile(conn, "SELECT chip, bezahlt_cent FROM v_rechnung_status WHERE id = ?",
                       (rid,))
        assert (stand["chip"], stand["bezahlt_cent"]) == ("STORNIERT", 18000)
        assert _zeile(conn, "SELECT COUNT(*) FROM zahlung WHERE rechnung_id = ?",
                      (k.entwurf_id,))[0] == 0

    def test_satz_hinweis_und_kundensatz_uebernehmen(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        k = ablauf.korrigieren(conn, rid, "falscher Satz", belegordner=ordner)
        entwurf = _zeile(conn, "SELECT * FROM rechnung WHERE id = ?", (k.entwurf_id,))
        assert ablauf.seitendaten(conn, entwurf)["satz_eingefroren"] is None, \
            "gleicher Satz wie am Kunden: kein Hinweis"

        conn.execute("UPDATE kunde SET satz_cent = 9500 WHERE id = ?", (kunde,))
        satz = ablauf.seitendaten(conn, entwurf)["satz_eingefroren"]
        assert satz["eingefroren"] == [9000] and satz["kunde"] == 9500
        assert _zeile(conn, "SELECT brutto_cent FROM rechnung WHERE id = ?",
                      (k.entwurf_id,))[0] == 18000, "der Entwurf rechnet erst nach dem Klick neu"

        alt, neu = ablauf.kundensatz_uebernehmen(conn, k.entwurf_id)
        assert (alt, neu) == ([9000], 9500)
        assert _zeile(conn, "SELECT brutto_cent FROM rechnung WHERE id = ?",
                      (k.entwurf_id,))[0] == 19000
        eintrag = _zeile(conn, "SELECT * FROM journal WHERE aktion = 'AENDERN'"
                         " AND feld = 'stundensatz'")
        assert (eintrag["wert_alt"], eintrag["wert_neu"]) == ("[9000]", "9500")
        with pytest.raises(fs.FestschreibenFehler):
            ablauf.kundensatz_uebernehmen(conn, rid)


@braucht("pdftotext")
def test_pdf_der_ersatzrechnung_nennt_die_ersetzte(conn, kunde, ordner):
    """Das echte Rendering: der Satz steht auf dem Papier."""
    rid = _festgeschrieben(conn, kunde, ordner, zeiten=1)
    k = ablauf.korrigieren(conn, rid, "Tippfehler", belegordner=ordner)
    ergebnis = fs.festschreiben(conn, k.entwurf_id, belegordner=ordner)
    text = subprocess.run(["pdftotext", ergebnis.pdf_pfad, "-"], capture_output=True,
                          text=True, check=True).stdout
    assert (f"Diese Rechnung ersetzt Rechnung Nr. {_nummer(1)} vom {_de(HEUTE)} "
            f"(storniert mit Nr. {_nummer(2)}).") in _text(text)


# ===================================================== Zuruecknehmen

@pytest.mark.usefixtures("schnelles_pdf")
class TestZuruecknehmen:
    def test_ohne_freigabe_bleibt_alles_gesperrt(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        for sql in (
            "UPDATE rechnung SET status = 'ENTWURF', nummer = NULL WHERE id = ?",
            "UPDATE rechnung SET brutto_cent = 1 WHERE id = ?",
            "UPDATE rechnung SET ersetzt_rechnung_id = id WHERE id = ?",
            "DELETE FROM rechnung WHERE id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (rid,))
        assert _zeile(conn, "SELECT status FROM rechnung WHERE id = ?",
                      (rid,))[0] == "FESTGESCHRIEBEN"

    def test_freigabe_aendert_nur_den_rueckweg_zum_entwurf(self, conn, kunde, ordner):
        """Auch MIT Freigabe bleibt der Inhalt gesperrt: nur Status und Nummer."""
        rid = _festgeschrieben(conn, kunde, ordner)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO loeschfreigabe (rechnung_id) VALUES (?)", (rid,))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE rechnung SET brutto_cent = 1 WHERE id = ?", (rid,))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE rechnung SET status = 'ENTWURF', nummer = NULL,"
                             " datum = '2020-01-01' WHERE id = ?", (rid,))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM rechnung WHERE id = ?", (rid,))
        finally:
            conn.execute("ROLLBACK")

    @pytest.mark.parametrize("fall", ["versendet", "bezahlt", "zurueckgenommene_zahlung",
                                      "storniert"])
    def test_nicht_zuruecknehmbar(self, conn, kunde, ordner, fall):
        rid = _festgeschrieben(conn, kunde, ordner)
        if fall == "versendet":
            conn.execute("UPDATE rechnung SET versendet_am = ? WHERE id = ?", (HEUTE, rid))
        elif fall in ("bezahlt", "zurueckgenommene_zahlung"):
            zid = conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent)"
                               " VALUES (?, ?, 100)", (rid, HEUTE)).lastrowid
            if fall == "zurueckgenommene_zahlung":
                ablauf.zahlung_zuruecknehmen(conn, rid, zid, "Fehlbuchung")
        else:
            fs.storniere(conn, rid, "Tippfehler", belegordner=ordner)
        probe = ablauf.ruecknahme_pruefen(conn, rid)
        assert not probe.moeglich
        with pytest.raises(fs.FestschreibenFehler):
            ablauf.zuruecknehmen(conn, rid, "Test", bestaetigt=True, datenordner=ordner)
        # Und die Datenbank selbst gibt keine Freigabe her.
        with pytest.raises(sqlite3.IntegrityError, match="Freigabe nur"):
            conn.execute("INSERT INTO loeschfreigabe (rechnung_id) VALUES (?)", (rid,))
        assert _zeile(conn, "SELECT COUNT(*) FROM loeschfreigabe")[0] == 0

    def test_storno_dokument_und_entwurf_nicht(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        storno = fs.storniere(conn, rid, "Tippfehler", belegordner=ordner)
        assert not ablauf.ruecknahme_pruefen(conn, storno.rechnung_id).moeglich
        entwurf = fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde)], datum=HEUTE)
        assert "Entwurf" in ablauf.ruecknahme_pruefen(conn, entwurf).hindernisse[0]

    def test_ohne_haken_oder_grund_abgewiesen(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        with pytest.raises(fs.FestschreibenFehler, match="bestätigen"):
            ablauf.zuruecknehmen(conn, rid, "Test", bestaetigt=False, datenordner=ordner)
        with pytest.raises(fs.FestschreibenFehler, match="Grund"):
            ablauf.zuruecknehmen(conn, rid, " ", bestaetigt=True, datenordner=ordner)
        assert _zeile(conn, "SELECT status FROM rechnung WHERE id = ?",
                      (rid,))[0] == "FESTGESCHRIEBEN"

    def test_zurueck_zum_entwurf_gibt_die_letzte_nummer_frei(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        positionen = conn.execute("SELECT bezeichnung, betrag_cent FROM rechnungsposition"
                                  " WHERE rechnung_id = ?", (rid,)).fetchall()
        assert ablauf.ruecknahme_pruefen(conn, rid).nummer_wird_frei

        r = ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                                 datenordner=ordner)
        assert r.nummer_frei and r.nummer == _nummer(1)
        rechnung = _zeile(conn, "SELECT * FROM rechnung WHERE id = ?", (rid,))
        assert rechnung["status"] == "ENTWURF"
        assert (rechnung["nummer"], rechnung["nummer_seq"], rechnung["pdf_pfad"],
                rechnung["kunde_snapshot_json"], rechnung["festgeschrieben_am"]) == (
            None, None, None, None, None)
        assert conn.execute("SELECT bezeichnung, betrag_cent FROM rechnungsposition"
                            " WHERE rechnung_id = ?", (rid,)).fetchall() == positionen
        assert _zeile(conn, "SELECT COUNT(*) FROM zeiteintrag WHERE rechnung_id = ?",
                      (rid,))[0] == 2
        assert _naechste_seq(conn) == 1
        assert _zeile(conn, "SELECT COUNT(*) FROM loeschfreigabe")[0] == 0
        # Dieselbe Nummer beim erneuten Festschreiben -- keine Luecke.
        assert fs.festschreiben(conn, rid, belegordner=ordner).nummer == _nummer(1)
        assert ablauf.nummernluecken(conn) == []

    def test_ganz_loeschen_gibt_zeiten_frei(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        r = ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                                 variante=ablauf.GANZ_LOESCHEN, datenordner=ordner)
        assert r.zeiten == 2
        assert _zeile(conn, "SELECT COUNT(*) FROM rechnung")[0] == 0
        assert _zeile(conn, "SELECT COUNT(*) FROM rechnungsposition")[0] == 0
        assert _zeile(conn, "SELECT COUNT(*) FROM zeiteintrag WHERE rechnung_id IS NULL")[0] == 2
        assert _naechste_seq(conn) == 1
        verworfen = _zeile(conn, "SELECT * FROM journal WHERE aktion = 'ENTWURF_VERWORFEN'")
        assert "Zurückgenommene Rechnung" in verworfen["begruendung"]

    def test_nicht_die_letzte_nummer_bleibt_als_luecke(self, conn, kunde, ordner):
        erste = _festgeschrieben(conn, kunde, ordner)
        _festgeschrieben(conn, kunde, ordner)
        probe = ablauf.ruecknahme_pruefen(conn, erste)
        assert not probe.nummer_wird_frei and probe.nachfolger == _nummer(2)

        r = ablauf.zuruecknehmen(conn, erste, "Fehler bemerkt", bestaetigt=True,
                                 variante=ablauf.GANZ_LOESCHEN, datenordner=ordner)
        assert not r.nummer_frei and r.nachfolger == _nummer(2)
        assert _naechste_seq(conn) == 3, "der Zaehler bleibt stehen"
        luecken = ablauf.nummernluecken(conn, JAHR)
        assert [(lu.nummer, lu.grund, lu.variante) for lu in luecken] == [
            (_nummer(1), "Fehler bemerkt", "GELOESCHT")]
        assert nummernkreis.luecken(conn, JAHR) == [], \
            "die Zaehlung allein saehe die Luecke am Anfang nicht"

    def test_vermerkte_luecke_bleibt_auch_hinter_der_letzten(self, conn, kunde, ordner):
        """Erst die mittlere Nummer zurueck (Luecke), dann die letzte (frei):
        die Zaehlung reicht nur bis zur hoechsten vergebenen -- das Journal weiss mehr."""
        a = _festgeschrieben(conn, kunde, ordner)
        b = _festgeschrieben(conn, kunde, ordner)
        c = _festgeschrieben(conn, kunde, ordner)
        ablauf.zuruecknehmen(conn, b, "Doppelt", bestaetigt=True, datenordner=ordner)
        ablauf.zuruecknehmen(conn, c, "Test", bestaetigt=True, datenordner=ordner)
        assert _naechste_seq(conn) == 3
        assert [lu.nummer for lu in ablauf.nummernluecken(conn)] == [_nummer(2)]
        assert _zeile(conn, "SELECT nummer FROM rechnung WHERE id = ?", (a,))[0] == _nummer(1)

    def test_durchlaufender_kreis(self, conn, kunde, ordner):
        nummernkreis.stelle_um(conn, JAHR, vorlage="R{NNNN}", start_seq=40,
                               reset_jaehrlich=False)
        rid = _festgeschrieben(conn, kunde, ordner)
        assert _zeile(conn, "SELECT nummer FROM rechnung WHERE id = ?", (rid,))[0] == "R0040"
        r = ablauf.zuruecknehmen(conn, rid, "Test", bestaetigt=True, datenordner=ordner)
        assert r.nummer_frei
        assert _zeile(conn, "SELECT naechste_seq FROM nummernkreis WHERE jahr = 0")[0] == 40

    def test_journal_haelt_den_vollen_inhalt(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner, betreff="Betreff X")
        rechnung = _zeile(conn, "SELECT * FROM rechnung WHERE id = ?", (rid,))
        ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True, datenordner=ordner)
        eintrag = _zeile(conn, "SELECT * FROM journal WHERE aktion = ?",
                         (ablauf.AKTION_ZURUECKGENOMMEN,))
        alt, neu = json.loads(eintrag["wert_alt"]), json.loads(eintrag["wert_neu"])
        assert alt["kopf"]["nummer"] == _nummer(1)
        assert alt["kopf"]["betreff"] == "Betreff X"
        assert alt["kopf"]["brutto_cent"] == rechnung["brutto_cent"]
        assert alt["kopf"]["kunde_snapshot"]["name"] == "Mustermann GmbH"
        assert alt["kopf"]["pdf_sha256"] == rechnung["pdf_sha256"]
        assert len(alt["positionen"]) >= 1 and len(alt["zeiten"]) == 2
        assert neu["nummer"] == _nummer(1) and neu["nummer_frei"] is True
        assert neu["bestaetigt"] == ablauf.BESTAETIGUNG
        assert neu["pdf_sha256_datei"] == rechnung["pdf_sha256"]
        assert eintrag["begruendung"] == "Testrechnung"
        assert eintrag["hash"] == rechnung["pdf_sha256"]

    def test_pdf_wandert_schreibgeschuetzt_nach_verworfen(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        rechnung = _zeile(conn, "SELECT * FROM rechnung WHERE id = ?", (rid,))
        alt = ordner / rechnung["pdf_pfad"]
        inhalt = alt.read_bytes()
        r = ablauf.zuruecknehmen(conn, rid, "Test", bestaetigt=True, datenordner=ordner)
        assert not alt.exists()
        neu = ordner / r.pdf_verworfen
        assert neu.parent == ordner / "ausgangsrechnungen" / "verworfen"
        assert re.fullmatch(rf"{_nummer(1)}_.*_VERWORFEN_\d{{8}}T\d{{6}}Z\.pdf", neu.name)
        assert neu.read_bytes() == inhalt
        assert pdf.pruefsumme(neu) == rechnung["pdf_sha256"]
        assert stat.S_IMODE(neu.stat().st_mode) & 0o222 == 0, "schreibgeschuetzt"
        # Die neu vergebene Nummer schreibt ihr PDF nicht ueber das verworfene.
        fs.festschreiben(conn, rid, belegordner=ordner)
        assert neu.read_bytes() == inhalt

    def test_scheitert_der_rest_kommt_das_pdf_zurueck(
        self, conn, kunde, ordner, monkeypatch
    ):
        rid = _festgeschrieben(conn, kunde, ordner)
        pfad = ordner / _zeile(conn, "SELECT pdf_pfad FROM rechnung WHERE id = ?", (rid,))[0]

        def kaputt(*_a, **_k):
            raise RuntimeError("Absicht")

        monkeypatch.setattr(fs, "entwurf_verwerfen", kaputt)
        with pytest.raises(RuntimeError):
            ablauf.zuruecknehmen(conn, rid, "Test", bestaetigt=True,
                                 variante=ablauf.GANZ_LOESCHEN, datenordner=ordner)
        assert pfad.is_file(), "das PDF liegt wieder an seinem Platz"
        assert not list((ordner / "ausgangsrechnungen" / "verworfen").iterdir())
        assert _zeile(conn, "SELECT status FROM rechnung WHERE id = ?",
                      (rid,))[0] == "FESTGESCHRIEBEN"
        assert _naechste_seq(conn) == 2
        assert _zeile(conn, "SELECT COUNT(*) FROM loeschfreigabe")[0] == 0
        assert _zeile(conn, "SELECT COUNT(*) FROM journal WHERE aktion = ?",
                      (ablauf.AKTION_ZURUECKGENOMMEN,))[0] == 0

    def test_sicherung_und_pruefung_danach_ohne_befund(self, conn, kunde, ordner):
        from app.logik import backup

        _festgeschrieben(conn, kunde, ordner)
        rid = _festgeschrieben(conn, kunde, ordner)
        ablauf.zuruecknehmen(conn, rid, "Test", bestaetigt=True,
                             variante=ablauf.GANZ_LOESCHEN, datenordner=ordner)
        archiv = backup.erzeuge(conn, ordner)
        ergebnis = backup.pruefe(conn, conn.execute(
            "SELECT id FROM backup_log ORDER BY id DESC").fetchone()["id"])
        assert ergebnis.heil, ergebnis.meldung if hasattr(ergebnis, "meldung") else ergebnis
        import zipfile
        with zipfile.ZipFile(archiv.pfad) as zip_:
            assert any(n.startswith("ausgangsrechnungen/verworfen/") for n in zip_.namelist())


# ===================================================== Zahlung zuruecknehmen

@pytest.mark.usefixtures("schnelles_pdf")
class TestZahlungZuruecknehmen:
    def _bezahlt(self, conn, kunde, ordner, datum=HEUTE, cent=18000):
        rid = _festgeschrieben(conn, kunde, ordner)
        zid = conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art)"
                           " VALUES (?, ?, ?, 'UEBERWEISUNG')", (rid, datum, cent)).lastrowid
        return rid, zid

    def test_gegenbuchung_hebt_alles_auf(self, conn, kunde, ordner):
        rid = _festgeschrieben(conn, kunde, ordner)
        vorher = (euer.einnahmen(conn, JAHR), grenzwert.vereinnahmt_cent(conn, JAHR))
        zid = conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent)"
                           " VALUES (?, ?, 18000)", (rid, HEUTE)).lastrowid
        assert _zeile(conn, "SELECT chip FROM v_rechnung_status WHERE id = ?",
                      (rid,))[0] == "BEZAHLT"

        z = ablauf.zahlung_zuruecknehmen(conn, rid, zid, "falsch erfasst")
        assert z.warnung is None
        gegen = _zeile(conn, "SELECT * FROM zahlung WHERE id = ?", (z.gegenbuchung_id,))
        assert (gegen["datum"], gegen["betrag_cent"], gegen["storno_von_id"]) == (
            HEUTE, -18000, zid)
        assert "falsch erfasst" in gegen["notiz"]
        stand = _zeile(conn, "SELECT * FROM v_rechnung_status WHERE id = ?", (rid,))
        assert (stand["bezahlt_cent"], stand["chip"]) == (0, "GESTELLT")
        assert stand["letzte_zahlung"] is None
        assert _zeile(conn, "SELECT COUNT(*) FROM v_offene_posten WHERE id = ?", (rid,))[0] == 1
        assert (euer.einnahmen(conn, JAHR), grenzwert.vereinnahmt_cent(conn, JAHR)) == vorher
        eintrag = _zeile(conn, "SELECT * FROM journal WHERE aktion = ?",
                         (ablauf.AKTION_ZAHLUNG_ZURUECK,))
        assert json.loads(eintrag["wert_alt"])["cent"] == 18000
        assert eintrag["begruendung"] == "falsch erfasst"

    def test_zweimal_und_gegenbuchung_selbst_abgewiesen(self, conn, kunde, ordner):
        rid, zid = self._bezahlt(conn, kunde, ordner)
        z = ablauf.zahlung_zuruecknehmen(conn, rid, zid, "Fehler")
        with pytest.raises(fs.FestschreibenFehler, match="bereits"):
            ablauf.zahlung_zuruecknehmen(conn, rid, zid, "Fehler")
        with pytest.raises(fs.FestschreibenFehler, match="selbst eine Rücknahme"):
            ablauf.zahlung_zuruecknehmen(conn, rid, z.gegenbuchung_id, "Fehler")
        with pytest.raises(fs.FestschreibenFehler, match="Grund"):
            ablauf.zahlung_zuruecknehmen(conn, rid, zid, "")
        # Der Index ist die zweite Sicherung.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent, storno_von_id)"
                         " VALUES (?, ?, -18000, ?)", (rid, HEUTE, zid))

    def test_rechnungszahlung_ist_unveraenderlich(self, conn, kunde, ordner):
        rid, zid = self._bezahlt(conn, kunde, ordner)
        for sql, werte in (
            ("UPDATE zahlung SET betrag_cent = 1 WHERE id = ?", (zid,)),
            ("UPDATE zahlung SET datum = '2020-01-01' WHERE id = ?", (zid,)),
            ("DELETE FROM zahlung WHERE id = ?", (zid,)),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="Gegenbuchung"):
                conn.execute(sql, werte)

    def test_belegzahlung_bleibt_aenderbar(self, conn, kunde, ordner):
        """Die Umrechnung zum Zahlungstag passt den Euro-Betrag an
        (umrechnung.py) -- die Sperre gilt nur Rechnungszahlungen. Zur
        Rechnungszahlung umhaengen laesst sich eine Belegzahlung aber nicht:
        der Weg an zahlung_nur_festgeschrieben vorbei."""
        bid = conn.execute(
            "INSERT INTO beleg (belegdatum, waehrung, betrag_brutto_cent,"
            " betrag_betrieblich_cent, erfasst_am) VALUES (?, 'EUR', 100, 100, 'x')",
            (HEUTE,)).lastrowid
        zid = conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent)"
                           " VALUES (?, ?, -100)", (bid, HEUTE)).lastrowid
        conn.execute("UPDATE zahlung SET betrag_cent = -90 WHERE id = ?", (zid,))
        rid = _festgeschrieben(conn, kunde, ordner)
        with pytest.raises(sqlite3.IntegrityError, match="Gegenbuchung"):
            conn.execute("UPDATE zahlung SET beleg_id = NULL, rechnung_id = ?,"
                         " betrag_cent = 90 WHERE id = ?", (rid, zid))

    def test_unstimmige_gegenbuchung_abgewiesen(self, conn, kunde, ordner):
        rid, zid = self._bezahlt(conn, kunde, ordner)
        for datum, cent in ((HEUTE, -100), ("2020-01-01", -18000)):
            with pytest.raises(sqlite3.IntegrityError, match="Gegenbuchung muss"):
                conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent,"
                             " storno_von_id) VALUES (?, ?, ?, ?)", (rid, datum, cent, zid))

    def test_rueckzahlung_nach_storno_zuruecknehmen(self, conn, kunde, ordner):
        rid, zid = self._bezahlt(conn, kunde, ordner)
        fs.storniere(conn, rid, "Tippfehler", belegordner=ordner)
        rueck = conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent)"
                             " VALUES (?, ?, -18000)", (rid, HEUTE)).lastrowid
        # Die eingegangene Zahlung zuerst: danach stuende mehr zurueck als da war.
        with pytest.raises(fs.FestschreibenFehler, match="mehr zurückgezahlt"):
            ablauf.zahlung_zuruecknehmen(conn, rid, zid, "Fehler")
        # Die irrtuemliche Rueckzahlung laesst sich aufheben -- positiv auf
        # einer stornierten Rechnung, nur als Gegenbuchung.
        ablauf.zahlung_zuruecknehmen(conn, rid, rueck, "doch nicht überwiesen")
        assert _zeile(conn, "SELECT bezahlt_cent FROM v_rechnung_status WHERE id = ?",
                      (rid,))[0] == 18000
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent)"
                         " VALUES (?, ?, 100)", (rid, HEUTE))

    def test_vorjahr_bekommt_eine_warnung(self, conn, kunde, ordner):
        rid, zid = self._bezahlt(conn, kunde, ordner, datum=f"{JAHR - 1}-12-15")
        z = ablauf.zahlung_zuruecknehmen(conn, rid, zid, "Fehler")
        assert f"EÜR für {JAHR - 1} ist evtl. schon abgegeben" in z.warnung
        assert grenzwert.vereinnahmt_cent(conn, JAHR - 1) == 0

    def test_fremde_zahlung_abgewiesen(self, conn, kunde, ordner):
        rid, zid = self._bezahlt(conn, kunde, ordner)
        andere = _festgeschrieben(conn, kunde, ordner)
        with pytest.raises(fs.FestschreibenFehler, match="gibt es zu dieser Rechnung nicht"):
            ablauf.zahlung_zuruecknehmen(conn, andere, zid, "Fehler")
        assert _zeile(conn, "SELECT bezahlt_cent FROM v_rechnung_status WHERE id = ?",
                      (rid,))[0] == 18000

    def test_buchungsliste_benennt_die_gegenbuchung(self, conn, kunde, ordner):
        rid, zid = self._bezahlt(conn, kunde, ordner)
        ablauf.zahlung_zuruecknehmen(conn, rid, zid, "falsch erfasst")
        zeilen = [z for z in euer.buchungen(conn, JAHR) if z["richtung"] == "Einnahme"]
        assert sum(z["betrag_cent"] for z in zeilen) == 0
        assert any("Rücknahme der Zahlung" in z["text"] for z in zeilen)


# ======================================================== Entwurf loeschen

def test_entwurf_verworfen_mit_inhalt_im_journal(conn, kunde):
    rid = fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde)], datum=HEUTE)
    fs.entwurf_loeschen(conn, rid)
    alt = json.loads(_zeile(conn, "SELECT wert_alt FROM journal"
                            " WHERE aktion = 'ENTWURF_VERWORFEN'")[0])
    assert alt["kopf"]["kunde_name"] == "Mustermann GmbH"
    assert alt["kopf"]["brutto_cent"] == 9000
    assert alt["positionen"][0]["betrag_cent"] == 9000
    assert len(alt["zeiten"]) == 1


# ============================================================ Migration

V20 = (Path(__file__).parent / "fixtures" / "schema_v20.sql").read_text(encoding="utf-8")


def test_migration_aus_version_20(tmp_path, schnelles_pdf):
    """Eine Datenbank im Stand von Version 20 (das echte Schema aus dem Commit
    davor) mit Rechnung, Position, Zeit und Zahlung: nach der Migration ist
    alles da, die neuen Spalten und die Freigabetabelle auch, und die neuen
    Regeln greifen."""
    pfad = tmp_path / "alt.sqlite"
    alt = sqlite3.connect(pfad, isolation_level=None)
    alt.executescript(V20)
    alt.execute("INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
                " standardsatz_cent, beginn_am) VALUES (1, 'Beispiel', 'Musterstraße 1',"
                " '12345', 'Musterstadt', '12/345/67890', 8000, '2020-01-01')")
    alt.execute("INSERT INTO kunde (id, kundennummer, name, strasse, plz, ort, satz_cent,"
                " angelegt_am) VALUES (1, 'K-0001', 'Beispiel GmbH', 'Weg 1', '10115',"
                " 'Berlin', 9000, '2026-01-01')")
    for zid, rid in ((1, 1), (2, 2)):
        alt.execute("INSERT INTO zeiteintrag (id, kunde_id, titel, datum, dauer_minuten,"
                    " satz_cent_snapshot, erfasst_am) VALUES (?, 1, 'Beratung', ?, 60, 9000,"
                    " 'x')", (zid, HEUTE))
        alt.execute("INSERT INTO rechnung (id, nummer, nummer_seq, nummer_jahr, kunde_id,"
                    " kunde_snapshot_json, datum, netto_cent, brutto_cent, status,"
                    " festgeschrieben_am, erstellt_am) VALUES (?, ?, ?, ?, 1, '{}', ?, 9000,"
                    " 9000, 'FESTGESCHRIEBEN', 'x', 'x')", (rid, _nummer(rid), rid, JAHR, HEUTE))
        alt.execute("INSERT INTO rechnungsposition (id, rechnung_id, pos_nr, bezeichnung,"
                    " menge_tausendstel, einzelpreis_cent, betrag_cent, quelle)"
                    " VALUES (?, ?, 1, 'Beratung', 1000, 9000, 9000, 'ZEIT')", (rid, rid))
        alt.execute("INSERT INTO position_zeiteintrag VALUES (?, ?)", (rid, zid))
        alt.execute("UPDATE zeiteintrag SET rechnung_id = ? WHERE id = ?", (rid, zid))
    alt.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (1, ?, 9000)",
                (HEUTE,))
    alt.execute("INSERT INTO nummernkreis (typ, jahr, format, naechste_seq)"
                " VALUES ('RECHNUNG', ?, 'RE-{JJJJ}-{NNNN}', 3)", (JAHR,))
    alt.execute("DELETE FROM schema_version")
    alt.execute("INSERT INTO schema_version VALUES (20, '2026-01-01T00:00:00+00:00')")
    alt.close()

    conn = db.oeffne(pfad)
    try:
        assert _zeile(conn, "SELECT MAX(version) FROM schema_version")[0] == db.SCHEMA_VERSION
        assert _zeile(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                      " AND objekt_id = 22")[0] == 1
        # Daten unveraendert.
        assert [tuple(z) for z in conn.execute(
            "SELECT id, nummer, status, brutto_cent FROM rechnung ORDER BY id")] == [
            (1, _nummer(1), "FESTGESCHRIEBEN", 9000), (2, _nummer(2), "FESTGESCHRIEBEN", 9000)]
        assert [tuple(z) for z in conn.execute(
            "SELECT rechnung_id, betrag_cent, storno_von_id FROM zahlung")] == [(1, 9000, None)]
        # Neue Spalten und Tabelle.
        assert "ersetzt_rechnung_id" in {z["name"] for z in conn.execute(
            "PRAGMA table_info(rechnung)")}
        assert "storno_von_id" in {z["name"] for z in conn.execute("PRAGMA table_info(zahlung)")}
        assert _zeile(conn, "SELECT COUNT(*) FROM loeschfreigabe")[0] == 0
        # Erneuerte und neue Trigger.
        trigger = {z["name"]: z["sql"] for z in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
        assert "loeschfreigabe" in trigger["rechnung_gesperrt"]
        assert "ersetzt_rechnung_id" in trigger["rechnung_gesperrt"]
        assert "storno_von_id" in trigger["zahlung_nur_festgeschrieben"]
        for name in ("zahlung_rechnung_kein_update", "zahlung_rechnung_kein_delete",
                     "zahlung_gegenbuchung_stimmig", "loeschfreigabe_nur_unversendet"):
            assert name in trigger, name
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        try:
            for name in ("rechnung_gesperrt", "zahlung_nur_festgeschrieben"):
                assert trigger[name] == _zeile(
                    frisch, "SELECT sql FROM sqlite_master WHERE name = ?", (name,))[0]
        finally:
            frisch.close()

        # Und die Regeln greifen in der migrierten Datenbank.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM zahlung WHERE rechnung_id = 1")
        r = ablauf.zuruecknehmen(conn, 2, "Test", bestaetigt=True, datenordner=tmp_path)
        assert r.nummer_frei
        assert _zeile(conn, "SELECT naechste_seq FROM nummernkreis")[0] == 2
        with pytest.raises(fs.FestschreibenFehler):
            ablauf.zuruecknehmen(conn, 1, "Test", bestaetigt=True, datenordner=tmp_path)
    finally:
        conn.close()


# ============================================================ Excel

@pytest.mark.usefixtures("schnelles_pdf")
def test_excel_hat_nummernluecken_und_gegenbuchung(conn, kunde, ordner, tmp_path):
    from openpyxl import load_workbook

    from app.logik import export_excel as ex

    erste = _festgeschrieben(conn, kunde, ordner)
    zweite = _festgeschrieben(conn, kunde, ordner)
    ablauf.zuruecknehmen(conn, erste, "Testrechnung", bestaetigt=True,
                         variante=ablauf.GANZ_LOESCHEN, datenordner=ordner)
    zid = conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent)"
                       " VALUES (?, ?, 18000)", (zweite, HEUTE)).lastrowid
    ablauf.zahlung_zuruecknehmen(conn, zweite, zid, "Fehler")
    ex.schreibe(conn, tmp_path / "x.xlsx")
    mappe = load_workbook(tmp_path / "x.xlsx")

    blatt = mappe["Nummernlücken"]
    kopf = [z.value for z in blatt[1]]
    zeilen = [dict(zip(kopf, [z.value for z in r], strict=True))
              for r in blatt.iter_rows(min_row=2)]
    assert [(z["Nummer"], z["Grund"]) for z in zeilen] == [(_nummer(1), "Testrechnung")]

    blatt = mappe[ex.BLATTNAMEN["zahlung"]]
    kopf = [z.value for z in blatt[1]]
    assert "Gegenbuchung zu Zahlung-ID" in kopf and "Hebt Zahlung auf" in kopf
    werte = [z.value for z in blatt[3]]
    assert werte[kopf.index("Hebt Zahlung auf")] == f"Zahlung {zid} vom {_de(HEUTE)}"
    assert "loeschfreigabe" not in mappe.sheetnames


# ============================================================ Weboberflaeche

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "inhaber": "F. Muster",
    "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
    "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
    "rundung_modus": "KAUFMAENNISCH", "rundung_intervall_min": "15",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "email": "buchhaltung@mustermann.example", "typ": "UNTERNEHMER", "land": "DE",
    "leistungsort": "INLAND", "satz_euro": "90,00", "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def daten(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
    return tmp_path / "daten"


@pytest.fixture
def klient(daten, schnelles_pdf):
    with TestClient(erzeuge_app(daten / "steuer.sqlite"), raise_server_exceptions=False) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _abfrage(daten, sql, werte=()):
    conn = sqlite3.connect(daten / "steuer.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, werte).fetchall()
    finally:
        conn.close()


def _query(antwort, name) -> str:
    assert antwort.status_code == 303, antwort.text[:400]
    return parse_qs(urlparse(antwort.headers["location"]).query).get(name, [""])[0]


def _web_festgeschrieben(klient, daten, datum=HEUTE) -> int:
    klient.post("/zeiten", data={"kunde_id": 1, "titel": "Beratung", "datum": datum,
                                 "dauer": "2h", "beschreibung": "", "modus": "STUNDEN",
                                 "grund": ""})
    zeit = _abfrage(daten, "SELECT MAX(id) FROM zeiteintrag")[0][0]
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": 1, "datum": datum, "modus": "PRO_TITEL", "einleitung": "",
        "schluss": "", "zeit": [zeit]}, follow_redirects=False)
    rid = int(re.search(r"/rechnung/(\d+)", antwort.headers["location"]).group(1))
    seite = klient.get(f"/rechnung/{rid}").text
    version = re.search(r'name="version" value="(\d+)"', seite).group(1)
    codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
    antwort = klient.post(f"/rechnung/{rid}/festschreiben",
                          data={"version": version, "bestaetigt": codes},
                          follow_redirects=False)
    assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
    return rid


class TestWebKorrigieren:
    def test_korrigieren_fuehrt_in_den_neuen_entwurf(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Etwas falsch?" in seite and "§ 146 Abs. 4 AO" in seite
        assert f'action="/rechnung/{rid}/korrigieren"' in seite
        # Gut sichtbar: vor den Zeiten und vor der Zahlungsliste, nicht am Ende.
        assert seite.index("Etwas falsch?") < seite.index("Zugeordnete Zeiten")

        antwort = klient.post(f"/rechnung/{rid}/korrigieren", data={"grund": "Satz falsch"},
                              follow_redirects=False)
        hinweis = _query(antwort, "hinweis")
        assert f"{_nummer(1)} ist storniert (Rechnungskorrektur {_nummer(2)})" in hinweis
        neu = int(re.search(r"/rechnung/(\d+)", antwort.headers["location"]).group(1))
        entwurf = _text(klient.get(f"/rechnung/{neu}").text)
        assert f'Ersetzt Rechnung <a href="/rechnung/{rid}">{_nummer(1)}</a>' in entwurf
        assert f">{_nummer(2)}</a>)" in entwurf
        original = _text(klient.get(f"/rechnung/{rid}").text)
        assert f'Ersetzt durch <a href="/rechnung/{neu}">den neuen Entwurf</a>' in original
        sid = _abfrage(daten, "SELECT id FROM rechnung WHERE typ = 'STORNO'")[0][0]
        assert f'href="/rechnung/{neu}"' in klient.get(f"/rechnung/{sid}").text

    def test_ohne_grund_eine_meldung(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/korrigieren", data={}, follow_redirects=False)
        assert "Begründung" in _query(antwort, "fehler")

    def test_satz_neu_berechnen_im_entwurf(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/korrigieren", data={"grund": "Satz"},
                              follow_redirects=False)
        neu = int(re.search(r"/rechnung/(\d+)", antwort.headers["location"]).group(1))
        assert "neu berechnen" not in klient.get(f"/rechnung/{neu}").text
        conn = sqlite3.connect(daten / "steuer.sqlite")
        conn.execute("UPDATE kunde SET satz_cent = 9500 WHERE id = 1")
        conn.commit()
        conn.close()
        seite = klient.get(f"/rechnung/{neu}").text
        assert "Am Kunden steht heute 95,00 €" in seite
        antwort = klient.post(f"/rechnung/{neu}/satz", follow_redirects=False)
        assert "95,00 €" in _query(antwort, "hinweis")
        assert _abfrage(daten, "SELECT brutto_cent FROM rechnung WHERE id = ?",
                        (neu,))[0][0] == 19000


class TestWebZuruecknehmen:
    def test_dialog_und_zurueck_zum_entwurf(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Noch nicht verschickt?" in seite
        assert f"Die Nummer <code>{_nummer(1)}</code> wird wieder frei" in seite
        assert ablauf.BESTAETIGUNG in seite
        assert 'value="ENTWURF" checked' in seite

        ohne = klient.post(f"/rechnung/{rid}/zuruecknehmen",
                           data={"grund": "Test", "variante": "ENTWURF"},
                           follow_redirects=False)
        assert "bestätigen" in _query(ohne, "fehler")

        antwort = klient.post(f"/rechnung/{rid}/zuruecknehmen", data={
            "grund": "Testrechnung", "bestaetigt": "1", "variante": "ENTWURF"},
            follow_redirects=False)
        assert antwort.headers["location"].startswith(f"/rechnung/{rid}?")
        assert "wieder ein Entwurf" in _query(antwort, "hinweis")
        assert _abfrage(daten, "SELECT status FROM rechnung WHERE id = ?",
                        (rid,))[0][0] == "ENTWURF"

    def test_luecke_steht_vorher_da_und_danach_in_der_liste(self, klient, daten):
        erste = _web_festgeschrieben(klient, daten)
        _web_festgeschrieben(klient, daten)
        seite = _text(klient.get(f"/rechnung/{erste}").text)
        assert (f"kann nicht wieder vergeben werden, weil {_nummer(2)} schon danach "
                "vergeben wurde" in seite)
        antwort = klient.post(f"/rechnung/{erste}/zuruecknehmen", data={
            "grund": "Doppelt festgeschrieben", "bestaetigt": "1", "variante": "LOESCHEN"},
            follow_redirects=False)
        assert antwort.headers["location"].startswith("/rechnungen?")
        assert "bleibt als Lücke" in _query(antwort, "hinweis")
        liste = klient.get(f"/rechnungen?jahr={JAHR}").text
        assert f"<code>{_nummer(1)}</code>" in liste and "Doppelt festgeschrieben" in liste
        luecken = klient.get("/rechnungen/luecken").text
        assert _nummer(1) in luecken and "Doppelt festgeschrieben" in luecken
        assert "gelöscht" in luecken

    def test_versendet_kein_dialog_aber_der_grund(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/versendet", data={"datum": HEUTE})
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Noch nicht verschickt?" not in seite
        assert "Zurücknehmen statt korrigieren geht nicht mehr" in seite
        antwort = klient.post(f"/rechnung/{rid}/zuruecknehmen", data={
            "grund": "x", "bestaetigt": "1"}, follow_redirects=False)
        assert "als versendet vermerkt" in _query(antwort, "fehler")

    def test_versandvermerk_zuruecknehmen_oeffnet_den_weg_wieder(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/versendet", data={"datum": HEUTE})
        ohne = klient.post(f"/rechnung/{rid}/versendet/zuruecknehmen", data={},
                           follow_redirects=False)
        assert "Grund" in _query(ohne, "fehler")
        antwort = klient.post(f"/rechnung/{rid}/versendet/zuruecknehmen",
                              data={"grund": "nie verschickt"}, follow_redirects=False)
        assert "zurückgenommen" in _query(antwort, "hinweis")
        eintrag = _abfrage(daten, "SELECT * FROM journal WHERE aktion = ?",
                           (ablauf.AKTION_VERSAND_ZURUECK,))[0]
        assert (eintrag["wert_alt"], eintrag["wert_neu"], eintrag["begruendung"]) == (
            HEUTE, None, "nie verschickt")
        assert "Noch nicht verschickt?" in klient.get(f"/rechnung/{rid}").text

    def test_versanddatum_aendern_braucht_einen_grund(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten, datum=GESTERN)
        klient.post(f"/rechnung/{rid}/versendet", data={"datum": GESTERN})
        assert "ändern oder zurücknehmen" in klient.get(f"/rechnung/{rid}").text
        # Dasselbe Datum noch einmal (Doppelklick) braucht keinen Grund.
        gleich = klient.post(f"/rechnung/{rid}/versendet", data={"datum": GESTERN},
                             follow_redirects=False)
        assert gleich.headers["location"] == f"/rechnung/{rid}"
        ohne = klient.post(f"/rechnung/{rid}/versendet", data={"datum": HEUTE},
                           follow_redirects=False)
        assert "gehört ein Grund" in _query(ohne, "fehler")
        assert _abfrage(daten, "SELECT versendet_am FROM rechnung WHERE id = ?",
                        (rid,))[0][0] == GESTERN
        mit = klient.post(f"/rechnung/{rid}/versendet",
                          data={"datum": HEUTE, "grund": "Tippfehler"}, follow_redirects=False)
        assert f"von {_de(GESTERN)} auf {_de(HEUTE)} geändert" in _query(mit, "hinweis")
        eintrag = _abfrage(daten, "SELECT * FROM journal WHERE aktion = 'VERSENDET'"
                                  " ORDER BY id DESC LIMIT 1")[0]
        assert (eintrag["wert_alt"], eintrag["wert_neu"], eintrag["begruendung"]) == (
            GESTERN, HEUTE, "Tippfehler")


class TestWebBezahlt:
    def test_ablaufleiste_oben_mit_zahlformular(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        seite = klient.get(f"/rechnung/{rid}").text
        leiste = seite.index('<ol class="ablauf')
        assert leiste < seite.index("<table>"), "oben, vor den Positionen"
        assert "Festgeschrieben am" in seite and "Bezahlt?" in seite
        assert "leer = offener Rest (180,00 €)" in seite
        assert "„Bezahlt“ ergibt sich aus der erfassten Zahlung" in seite
        assert seite.count(f'action="/rechnung/{rid}/zahlung"') == 1, "nicht doppelt"
        klient.post(f"/rechnung/{rid}/zahlung", data={"datum": HEUTE, "betrag_euro": "",
                                                      "erwartet_bezahlt": "0"})
        assert f"Bezahlt am {_de(HEUTE)}." in klient.get(f"/rechnung/{rid}").text

    def test_schnellaktion_nur_bei_offenen_festgeschriebenen(self, klient, daten):
        offen = _web_festgeschrieben(klient, daten)
        storniert = _web_festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{storniert}/storno", data={"grund": "x"})
        klient.post("/zeiten", data={"kunde_id": 1, "titel": "B", "datum": HEUTE,
                                     "dauer": "1h", "beschreibung": "", "modus": "STUNDEN",
                                     "grund": ""})
        zeit = _abfrage(daten, "SELECT MAX(id) FROM zeiteintrag")[0][0]
        klient.post("/rechnung/neu", data={"kunde_id": 1, "datum": HEUTE, "modus": "PRO_TITEL",
                                           "zeit": [zeit]})
        liste = klient.get("/rechnungen").text
        assert liste.count("<summary>Bezahlt …</summary>") == 1
        assert f'action="/rechnung/{offen}/zahlung"' in liste
        assert f'action="/rechnung/{storniert}/zahlung"' not in liste
        assert liste.count("<summary>Löschen</summary>") == 1, "nur beim Entwurf"
        assert liste.count("<summary>Versendet …</summary>") == 2, "Rechnung und Korrektur"
        uebersicht = klient.get("/").text
        assert f'action="/rechnung/{offen}/zahlung"' in uebersicht
        assert 'name="zurueck" value="/"' in uebersicht

    def test_bezahlt_aus_der_liste_bucht_den_rest(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": HEUTE, "betrag_euro": "", "erwartet_bezahlt": "0",
            "zurueck": f"/rechnungen?jahr={JAHR}&hinweis=alt"}, follow_redirects=False)
        ort = urlparse(antwort.headers["location"])
        assert ort.path == "/rechnungen"
        assert parse_qs(ort.query)["jahr"] == [str(JAHR)]
        assert parse_qs(ort.query)["hinweis"] == [
            f"Zahlung über 180,00 € zu {_nummer(1)} vom {_de(HEUTE)} erfasst."]
        assert _abfrage(daten, "SELECT chip FROM v_rechnung_status WHERE id = ?",
                        (rid,))[0][0] == "BEZAHLT"
        # Doppelklick: der zweite Klick trifft einen anderen Stand.
        zweiter = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": HEUTE, "betrag_euro": "", "erwartet_bezahlt": "0",
            "zurueck": "/rechnungen"}, follow_redirects=False)
        assert "nicht mehr aktuell" in _query(zweiter, "fehler")
        assert urlparse(zweiter.headers["location"]).path == "/rechnungen"
        assert _abfrage(daten, "SELECT COUNT(*) FROM zahlung")[0][0] == 1
        assert "erfasst." in klient.get(antwort.headers["location"]).text

    @pytest.mark.parametrize("fremd", ["//fremd.example/rechnungen", "https://fremd.example/",
                                       "/\\fremd.example", "/rechnung/1", "javascript:x"])
    def test_fremdes_ziel_landet_beim_standard(self, klient, daten, fremd):
        rid = _web_festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": HEUTE, "betrag_euro": "10", "erwartet_bezahlt": "0", "zurueck": fremd},
            follow_redirects=False)
        assert antwort.headers["location"] == f"/rechnung/{rid}"

    def test_versendet_aus_der_uebersicht(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/versendet", data={
            "datum": HEUTE, "zurueck": "/"}, follow_redirects=False)
        assert urlparse(antwort.headers["location"]).path == "/"
        assert "als versendet vermerkt" in _query(antwort, "hinweis")
        assert "als versendet vermerkt" in klient.get(antwort.headers["location"]).text


class TestWebZahlungZuruecknehmen:
    def test_zuruecknehmen_auf_der_seite(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung", data={"datum": HEUTE, "erwartet_bezahlt": "0"})
        zid = _abfrage(daten, "SELECT id FROM zahlung")[0][0]
        seite = klient.get(f"/rechnung/{rid}").text
        assert f'action="/rechnung/{rid}/zahlung/{zid}/zuruecknehmen"' in seite
        ohne = klient.post(f"/rechnung/{rid}/zahlung/{zid}/zuruecknehmen", data={},
                           follow_redirects=False)
        assert "Grund" in _query(ohne, "fehler")
        antwort = klient.post(f"/rechnung/{rid}/zahlung/{zid}/zuruecknehmen",
                              data={"grund": "falsch erfasst"}, follow_redirects=False)
        assert "zurückgenommen" in _query(antwort, "hinweis")
        seite = klient.get(f"/rechnung/{rid}").text
        assert "· zurückgenommen" in seite and "Rücknahme einer Fehlbuchung" in seite
        assert "zuruecknehmen\"" not in seite.split("<h2>Zahlungen</h2>")[1].split("</table>")[0]
        assert "Bezahlt?" in seite, "wieder offen"
        fremd = klient.post(f"/rechnung/{rid}/zahlung/999/zuruecknehmen",
                            data={"grund": "x"}, follow_redirects=False)
        assert "gibt es zu dieser Rechnung nicht" in _query(fremd, "fehler")


class TestWebEntwurfLoeschen:
    def test_entwurf_loeschen_oben_mit_rueckfrage(self, klient, daten):
        klient.post("/zeiten", data={"kunde_id": 1, "titel": "B", "datum": HEUTE,
                                     "dauer": "1h", "beschreibung": "", "modus": "STUNDEN",
                                     "grund": ""})
        zeit = _abfrage(daten, "SELECT MAX(id) FROM zeiteintrag")[0][0]
        antwort = klient.post("/rechnung/neu", data={"kunde_id": 1, "datum": HEUTE,
                                                     "modus": "PRO_TITEL", "zeit": [zeit]},
                              follow_redirects=False)
        rid = int(re.search(r"/rechnung/(\d+)", antwort.headers["location"]).group(1))
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Ein Entwurf hat noch keine Nummer; Löschen hinterlässt keine Lücke." in seite
        assert seite.index("<summary>Entwurf löschen</summary>") < seite.index("<table>")
        assert "Ja, Entwurf löschen" in seite

        antwort = klient.post(f"/rechnung/{rid}/verwerfen",
                              data={"zurueck": f"/rechnungen?jahr={JAHR}"},
                              follow_redirects=False)
        assert urlparse(antwort.headers["location"]).path == "/rechnungen"
        assert "1 Zeit ist wieder frei" in _query(antwort, "hinweis")
        alt = json.loads(_abfrage(daten, "SELECT wert_alt FROM journal"
                                         " WHERE aktion = 'ENTWURF_VERWORFEN'")[0][0])
        assert alt["kopf"]["brutto_cent"] == 9000 and len(alt["positionen"]) == 1


def _parallel(anzahl, funktion):
    import threading

    ergebnisse = [None] * anzahl
    sperre = threading.Barrier(anzahl)

    def lauf(i):
        try:
            sperre.wait()
            ergebnisse[i] = funktion(i)
        except Exception as fehler:  # noqa: BLE001 -- im Test sichtbar machen
            ergebnisse[i] = fehler

    faeden = [threading.Thread(target=lauf, args=(i,)) for i in range(anzahl)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join()
    return ergebnisse


class TestGleichzeitig:
    """Doppelklick und zwei Tabs: vier gleichzeitige Anfragen, ein Ergebnis."""

    def test_viermal_korrigieren_ergibt_ein_storno_und_einen_entwurf(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        antworten = _parallel(4, lambda _i: klient.post(
            f"/rechnung/{rid}/korrigieren", data={"grund": "Tippfehler"},
            follow_redirects=False))
        assert all(getattr(a, "status_code", None) == 303 for a in antworten), antworten
        assert _abfrage(daten, "SELECT COUNT(*) FROM rechnung WHERE typ = 'STORNO'")[0][0] == 1
        assert _abfrage(daten, "SELECT COUNT(*) FROM rechnung WHERE status = 'ENTWURF'")[0][0] \
            == 1
        assert _abfrage(daten, "SELECT COUNT(*) FROM journal WHERE aktion = 'KORREKTUR'")[0][0] \
            == 1

    def test_viermal_zuruecknehmen_ergibt_einen_vorgang(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        antworten = _parallel(4, lambda _i: klient.post(
            f"/rechnung/{rid}/zuruecknehmen",
            data={"grund": "Test", "bestaetigt": "1", "variante": "ENTWURF"},
            follow_redirects=False))
        assert all(getattr(a, "status_code", None) == 303 for a in antworten), antworten
        assert _abfrage(daten, "SELECT COUNT(*) FROM journal WHERE aktion = ?",
                        (ablauf.AKTION_ZURUECKGENOMMEN,))[0][0] == 1
        assert _abfrage(daten, "SELECT naechste_seq FROM nummernkreis")[0][0] == 1
        assert _abfrage(daten, "SELECT COUNT(*) FROM loeschfreigabe")[0][0] == 0

    def test_viermal_zahlung_zuruecknehmen_ergibt_eine_gegenbuchung(self, klient, daten):
        rid = _web_festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/zahlung", data={"datum": HEUTE, "erwartet_bezahlt": "0"})
        zid = _abfrage(daten, "SELECT id FROM zahlung")[0][0]
        antworten = _parallel(4, lambda _i: klient.post(
            f"/rechnung/{rid}/zahlung/{zid}/zuruecknehmen", data={"grund": "Fehler"},
            follow_redirects=False))
        assert all(getattr(a, "status_code", None) == 303 for a in antworten), antworten
        assert _abfrage(daten, "SELECT COUNT(*) FROM zahlung")[0][0] == 2

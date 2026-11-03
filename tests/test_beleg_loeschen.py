"""Beleg loeschen als Loeschmarkierung (Schema 25, app/logik/beleg/loeschen.py).

Geprueft wird, was der Nutzer davon hat -- der Beleg ist aus Liste und
Auswertungen weg, dieselbe Datei laesst sich neu buchen -- und was dabei
nicht verloren gehen darf: Datensatz, Zahlungen, Datei und das vollstaendige
Abbild im Journal (§ 146 Abs. 4 AO, GoBD Rz. 107 ff.). Dazu die Sperren auf
Datenbankebene, die vorher allein an der Oberflaeche hingen.
"""

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import db
from app.logik import anlagen, euer, export_excel, kategorien
from app.logik.beleg import eigenbeleg, loeschen
from app.logik.beleg import pipeline as p
from app.main import erzeuge_app
from tests.test_belegordner import cii

HEUTE = date(2026, 10, 24)


# ------------------------------------------------------------------ Helfer

@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


@pytest.fixture
def dconn(datenordner):
    """Eine Datenbank IM Datenordner (Archivpfade relativ dazu), mit Betrieb."""
    verbindung = db.oeffne(datenordner / "steuer.sqlite")
    verbindung.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer)"
        " VALUES (1, 'Mein Kleingewerbe', 'Musterstraße 1', '20095', 'Hamburg',"
        " '12/345/67890')")
    yield verbindung
    verbindung.close()


def _lieferant(conn, name="Beispiel GmbH") -> int:
    vorhanden = conn.execute("SELECT id FROM lieferant WHERE name = ?", (name,)).fetchone()
    if vorhanden:
        return vorhanden["id"]
    return conn.execute("INSERT INTO lieferant (name, angelegt_am) VALUES (?, '2026-01-01')",
                        (name,)).lastrowid


def _kategorie(conn, schluessel="HOSTING") -> int:
    return kategorien.nach_schluessel(conn, schluessel)["id"]


def _buchen(conn, datenordner, *, nummer="RE-2026-0815", datum="20260314",
            bezahlt: str | None = "2026-03-20", inhalt: bytes | None = None,
            brutto: int = 546, kategorie="HOSTING", **mehr) -> int:
    """Bucht eine E-Rechnung wie die Pruefmaske -- und auf Wunsch die Zahlung."""
    eingang = p.aufnehmen(inhalt or cii(nummer, datum), f"{nummer}.xml", datenordner)
    lesung = p.lies(conn, eingang, heute=HEUTE)
    iso = f"{datum[:4]}-{datum[4:6]}-{datum[6:]}"
    beleg_id = p.buche(
        conn, lesung, {"belegdatum": iso, "brutto": brutto, "waehrung": "EUR",
                       "belegnummer": nummer},
        datenordner, lieferant_id=_lieferant(conn),
        kategorie_id=_kategorie(conn, kategorie) if kategorie else None, heute=HEUTE, **mehr,
    ).beleg_id
    if bezahlt:
        _zahlen(conn, beleg_id, bezahlt)
    return beleg_id


def _zahlen(conn, beleg_id, datum, cent=None):
    betrag = cent or conn.execute("SELECT betrag_brutto_cent FROM beleg WHERE id = ?",
                                  (beleg_id,)).fetchone()[0]
    return conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                        " VALUES (?, ?, ?, 'UEBERWEISUNG')", (beleg_id, datum, -betrag)).lastrowid


def _roh_beleg(conn, **felder) -> int:
    spalten = {"belegdatum": "2026-03-14", "betrag_brutto_cent": 10000,
               "betrag_betrieblich_cent": 10000, "erfasst_am": db.jetzt_utc(), **felder}
    return conn.execute(
        f"INSERT INTO beleg ({', '.join(spalten)}) VALUES ({', '.join('?' * len(spalten))})",
        tuple(spalten.values())).lastrowid


def _roh_datei(conn, beleg_id, sha="a" * 64, rolle="ORIGINAL", pfad="belege/2026/a.pdf") -> int:
    return conn.execute(
        "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse, rolle,"
        " importiert_am) VALUES (?, ?, 'rechnung.pdf', ?, 1234, ?, ?)",
        (beleg_id, pfad, sha, rolle, db.jetzt_utc())).lastrowid


def _markieren(conn, beleg_id, grund="Testbeleg"):
    conn.execute("UPDATE beleg SET geloescht_am = ?, geloescht_grund = ? WHERE id = ?",
                 (db.jetzt_utc(), grund, beleg_id))


def _anzahl(conn, sql, *werte) -> int:
    return conn.execute(sql, werte).fetchone()[0]


# ============================================================ Datenbank

class TestSperren:
    """Die Trigger aus Schema 25 -- erlaubt, was ein Programmpfad tut, sonst nichts."""

    def test_betrag_und_datum_sind_fest(self, conn):
        """Bis Version 24 sperrte das nur die Oberflaeche."""
        bid = _roh_beleg(conn, betrag_netto_cent=8403, betrag_ust_cent=1597,
                         ust_satz_promille=190)
        for anweisung in ("betrag_brutto_cent = 9000, betrag_netto_cent = 7563,"
                          " betrag_ust_cent = 1437", "belegdatum = '2026-03-15'",
                          "waehrung = 'USD'", "ust_satz_promille = 70",
                          "betrag_fremdwaehrung_cent = 100"):
            with pytest.raises(sqlite3.IntegrityError, match="unveraenderlich"):
                conn.execute(f"UPDATE beleg SET {anweisung} WHERE id = ?", (bid,))

    def test_zuordnung_bleibt_aenderbar(self, conn):
        """Kategorie, Quote, betrieblicher Anteil, geprueft, Anlagegut-Haken:
        die bestehenden Wege gehen weiter."""
        bid = _roh_beleg(conn)
        conn.execute("UPDATE beleg SET kategorie_id = ?, aufteilungsquote = 70,"
                     " betrag_betrieblich_cent = 7000, geprueft_am = 'x', ist_anlagegut = 1,"
                     " version = version + 1 WHERE id = ?", (_kategorie(conn), bid))
        # Derselbe Betrag noch einmal ist keine Aenderung.
        conn.execute("UPDATE beleg SET betrag_brutto_cent = 10000 WHERE id = ?", (bid,))

    def test_euro_betrag_bei_fremdwaehrung_nur_auf_seine_grundlage(self, conn):
        """Die eine Ausnahme: der Euro-Betrag eines Fremdwaehrungsbelegs folgt
        Kartenbetrag oder Umrechnung -- auf einen dritten Wert nicht, und auch
        ein Beleg aus der Zeit vor Version 19 (ohne Grundlage) bleibt fest."""
        bid = _roh_beleg(conn, waehrung="USD", betrag_fremdwaehrung_cent=12000,
                         betrag_umgerechnet_cent=10000, kurs="1.2", kurs_quelle="EZB")
        conn.execute("UPDATE beleg SET betrag_karte_cent = 10150, betrag_brutto_cent = 10150"
                     " WHERE id = ?", (bid,))
        conn.execute("UPDATE beleg SET betrag_karte_cent = NULL, betrag_brutto_cent = 10000"
                     " WHERE id = ?", (bid,))
        conn.execute("UPDATE beleg SET betrag_umgerechnet_cent = 9800, betrag_brutto_cent = 9800"
                     " WHERE id = ?", (bid,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE beleg SET betrag_brutto_cent = 9000 WHERE id = ?", (bid,))
        alt = _roh_beleg(conn, waehrung="USD", betrag_fremdwaehrung_cent=12000)
        with pytest.raises(sqlite3.IntegrityError, match="unveraenderlich"):
            conn.execute("UPDATE beleg SET betrag_brutto_cent = 9000 WHERE id = ?", (alt,))
        conn.execute("UPDATE beleg SET betrag_karte_cent = 9000, betrag_brutto_cent = 9000"
                     " WHERE id = ?", (alt,))
        with pytest.raises(sqlite3.IntegrityError, match="unveraenderlich"):
            conn.execute("UPDATE beleg SET betrag_fremdwaehrung_cent = 13000 WHERE id = ?",
                         (bid,))

    def test_belegdatei_wird_nie_geloescht_und_behaelt_ihre_rolle(self, conn):
        bid = _roh_beleg(conn)
        did = _roh_datei(conn, bid)
        with pytest.raises(sqlite3.IntegrityError, match="nie geloescht"):
            conn.execute("DELETE FROM belegdatei WHERE id = ?", (did,))
        with pytest.raises(sqlite3.IntegrityError, match="Rolle"):
            conn.execute("UPDATE belegdatei SET rolle = 'EMAIL' WHERE id = ?", (did,))
        # Was keine Rolle aendert, geht (derselbe Wert).
        conn.execute("UPDATE belegdatei SET rolle = 'ORIGINAL' WHERE id = ?", (did,))

    def test_kein_beleg_wird_physisch_geloescht(self, conn):
        bid = _roh_beleg(conn)
        with pytest.raises(sqlite3.IntegrityError, match="nie geloescht"):
            conn.execute("DELETE FROM beleg WHERE id = ?", (bid,))

    def test_loeschen_braucht_grund_und_beides(self, conn):
        bid = _roh_beleg(conn)
        for zeitpunkt, grund in (("x", None), ("x", "  "), ("x", "ab"), (None, "Testbeleg")):
            with pytest.raises(sqlite3.IntegrityError, match="Grund"):
                conn.execute("UPDATE beleg SET geloescht_am = ?, geloescht_grund = ?"
                             " WHERE id = ?", (zeitpunkt, grund, bid))
        _markieren(conn, bid, "doppelt")
        with pytest.raises(sqlite3.IntegrityError, match="gebucht und erst danach"):
            _roh_beleg(conn, geloescht_am="x", geloescht_grund="Testbeleg")

    def test_geloeschter_beleg_ist_unveraenderlich_bis_zum_wiederherstellen(self, conn):
        bid = _roh_beleg(conn)
        _markieren(conn, bid)
        with pytest.raises(sqlite3.IntegrityError, match="erst wiederherstellen"):
            conn.execute("UPDATE beleg SET kategorie_id = ? WHERE id = ?",
                         (_kategorie(conn), bid))
        with pytest.raises(sqlite3.IntegrityError, match="erst wiederherstellen"):
            conn.execute("UPDATE beleg SET geloescht_grund = 'anderer Grund' WHERE id = ?",
                         (bid,))
        conn.execute("UPDATE beleg SET geloescht_am = NULL, geloescht_grund = NULL"
                     " WHERE id = ?", (bid,))
        conn.execute("UPDATE beleg SET kategorie_id = ? WHERE id = ?", (_kategorie(conn), bid))

    def test_zahlung_eines_geloeschten_belegs_bleibt_wie_sie_ist(self, conn):
        bid = _roh_beleg(conn)
        zid = _zahlen(conn, bid, "2026-03-20")
        _markieren(conn, bid)
        with pytest.raises(sqlite3.IntegrityError, match="geloeschten Beleg"):
            _zahlen(conn, bid, "2026-03-21")
        with pytest.raises(sqlite3.IntegrityError, match="bleibt, wie sie ist"):
            conn.execute("UPDATE zahlung SET datum = '2026-03-22' WHERE id = ?", (zid,))
        with pytest.raises(sqlite3.IntegrityError, match="bleibt, wie sie ist"):
            conn.execute("DELETE FROM zahlung WHERE id = ?", (zid,))
        anderer = _roh_beleg(conn)
        andere_zahlung = _zahlen(conn, anderer, "2026-03-20")
        with pytest.raises(sqlite3.IntegrityError, match="bleibt, wie sie ist"):
            conn.execute("UPDATE zahlung SET beleg_id = ? WHERE id = ?", (bid, andere_zahlung))

    def test_original_einmalig_nur_unter_gueltigen_belegen(self, conn):
        """Der Dublettencheck in der Datenbank: dieselbe Datei als Original
        eines zweiten GUELTIGEN Belegs nicht, nach dem Loeschen des ersten
        schon -- und der erste kommt dann nicht mehr zurueck."""
        erster, zweiter, dritter = _roh_beleg(conn), _roh_beleg(conn), _roh_beleg(conn)
        _roh_datei(conn, erster)
        with pytest.raises(sqlite3.IntegrityError, match="schon Original"):
            _roh_datei(conn, zweiter)
        _roh_datei(conn, dritter, rolle="EMAIL")  # Begleitdateien sind nicht eindeutig
        _markieren(conn, erster)
        _roh_datei(conn, zweiter)
        with pytest.raises(sqlite3.IntegrityError, match="inzwischen als anderer Beleg"):
            conn.execute("UPDATE beleg SET geloescht_am = NULL, geloescht_grund = NULL"
                         " WHERE id = ?", (erster,))

    def test_anlagegut_geht_mit_dem_beleg(self, conn):
        """Erst das Gut, dann der Beleg; zurueck erst der Beleg, dann das Gut.
        Aus einem geloeschten Beleg entsteht kein neues Gut."""
        bid = _roh_beleg(conn, ist_anlagegut=1)
        gid = conn.execute(
            "INSERT INTO anlagegut (beleg_id, bezeichnung, anschaffung_am, ak_brutto_cent,"
            " ak_netto_cent, nutzungsdauer_jahre, erfasst_am)"
            " VALUES (?, 'Notebook', '2026-03-14', 10000, 8403, 3, 'x')", (bid,)).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="Anlagegut"):
            _markieren(conn, bid)
        conn.execute("UPDATE anlagegut SET geloescht_am = 'x', geloescht_grund = 'mit Beleg'"
                     " WHERE id = ?", (gid,))
        _markieren(conn, bid)
        with pytest.raises(sqlite3.IntegrityError, match="unveraenderlich"):
            conn.execute("UPDATE anlagegut SET abgang_am = '2026-05-01' WHERE id = ?", (gid,))
        with pytest.raises(sqlite3.IntegrityError, match="nur mit seinem Beleg"):
            conn.execute("UPDATE anlagegut SET geloescht_am = NULL, geloescht_grund = NULL"
                         " WHERE id = ?", (gid,))
        anderer = _roh_beleg(conn)
        _markieren(conn, anderer)
        with pytest.raises(sqlite3.IntegrityError, match="geloescht"):
            conn.execute(
                "INSERT INTO anlagegut (beleg_id, bezeichnung, anschaffung_am, ak_brutto_cent,"
                " ak_netto_cent, nutzungsdauer_jahre, erfasst_am)"
                " VALUES (?, 'Monitor', '2026-03-14', 10000, 8403, 3, 'x')", (anderer,))


# Die Trigger, Sicht und Index, die Version 25 neu bringt -- fuer den
# Nachbau einer Datenbank im Stand 20.
NEUE_TRIGGER = (
    "beleg_betrag_datum_fest", "beleg_kein_delete", "beleg_nicht_geloescht_anlegen",
    "beleg_loeschen_braucht_grund", "beleg_geloescht_unveraenderlich",
    "beleg_loeschen_nicht_ohne_anlagegut", "belegdatei_original_einmalig_insert",
    "belegdatei_rolle_fest", "belegdatei_kein_delete",
    "beleg_wiederherstellen_original_einmalig", "zahlung_nicht_auf_geloeschten_beleg",
    "zahlung_geloeschter_beleg_fest_update", "zahlung_geloeschter_beleg_fest_delete",
    "anlagegut_nicht_geloescht_anlegen", "anlagegut_nicht_aus_geloeschtem_beleg",
    "anlagegut_loeschen_braucht_grund", "anlagegut_geloescht_unveraenderlich",
    "anlagegut_wiederherstellen_nur_mit_beleg",
)


class TestMigration:
    def _stand_20(self, pfad: Path) -> None:
        """Eine Datenbank im Stand von Version 20, mit Daten in allen
        betroffenen Tabellen: Beleg mit Datei, Auslesung, Zahlung, Anlagegut
        mit AfA-Reihe -- und dem UNIQUE-Index auf der Pruefsumme."""
        alt = db.oeffne(pfad)
        for name in NEUE_TRIGGER:
            alt.execute(f"DROP TRIGGER {name}")
        alt.execute("DROP VIEW v_beleg_gueltig")
        alt.execute("DROP VIEW v_anlagegut_gueltig")
        alt.execute("DROP INDEX idx_belegdatei_sha256")
        for tabelle in ("beleg", "anlagegut"):
            for spalte in ("geloescht_am", "geloescht_grund"):
                alt.execute(f"ALTER TABLE {tabelle} DROP COLUMN {spalte}")
        alt.execute("CREATE UNIQUE INDEX belegdatei_original_einmalig"
                    " ON belegdatei (sha256) WHERE rolle = 'ORIGINAL'")
        bid = _roh_beleg(alt, ist_anlagegut=1)
        _roh_datei(alt, bid)
        alt.execute("INSERT INTO extraktion (beleg_id, quelle, zeitpunkt)"
                    " VALUES (?, 'xml', 'x')", (bid,))
        _zahlen(alt, bid, "2026-03-20")
        gid = alt.execute(
            "INSERT INTO anlagegut (beleg_id, bezeichnung, anschaffung_am, ak_brutto_cent,"
            " ak_netto_cent, nutzungsdauer_jahre, erfasst_am)"
            " VALUES (?, 'Notebook', '2026-03-14', 10000, 8403, 3, 'x')", (bid,)).lastrowid
        alt.execute("INSERT INTO anlagegut_afa_jahr (anlagegut_id, jahr, afa_cent, kennzahl,"
                    " restwert_cent) VALUES (?, 2026, 2777, 130, 7223)", (gid,))
        alt.execute("DELETE FROM schema_version")
        alt.execute("INSERT INTO schema_version VALUES (20, ?)", (db.jetzt_utc(),))
        alt.close()

    def test_von_20_auf_25(self, tmp_path):
        pfad = tmp_path / "alt.sqlite"
        self._stand_20(pfad)
        migriert = db.oeffne(pfad)
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        try:
            def schema(verbindung, art):
                return {z[0] for z in verbindung.execute(
                    "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'",
                    (art,))}

            def spalten(verbindung, tabelle):
                return {z["name"] for z in verbindung.execute(f"PRAGMA table_info({tabelle})")}

            for art in ("table", "trigger", "index", "view"):
                assert schema(migriert, art) == schema(frisch, art), art
            for tabelle in ("beleg", "anlagegut", "belegdatei"):
                assert spalten(migriert, tabelle) == spalten(frisch, tabelle), tabelle
            assert "belegdatei_original_einmalig" not in schema(migriert, "index")
            assert migriert.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
                == db.SCHEMA_VERSION >= 25
            assert _anzahl(migriert, "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                                     " AND objekt_id = 25") == 1
            assert migriert.execute("PRAGMA foreign_key_check").fetchall() == []
            # Die Daten sind da und gueltig.
            assert _anzahl(migriert, "SELECT COUNT(*) FROM v_beleg_gueltig") == 1
            assert _anzahl(migriert, "SELECT COUNT(*) FROM v_anlagegut_gueltig") == 1
            assert _anzahl(migriert, "SELECT COUNT(*) FROM zahlung") == 1
            assert euer.ausgaben(migriert, 2026) != []
            # Und das Neue geht: loeschen samt Anlagegut, dieselbe Datei neu.
            ergebnis = loeschen.loeschen(migriert, 1, grund="Testbeleg", anlagegut_mit=True)
            assert ergebnis.anlagegut is not None
            neu = _roh_beleg(migriert)
            _roh_datei(migriert, neu)
            with pytest.raises(sqlite3.IntegrityError, match="unveraenderlich"):
                migriert.execute("UPDATE beleg SET betrag_brutto_cent = 1 WHERE id = ?", (neu,))
        finally:
            migriert.close()
            frisch.close()


# ============================================================ Logik

class TestLoeschen:
    def test_markiert_und_protokolliert_das_ganze_abbild(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner)
        ergebnis = loeschen.loeschen(dconn, bid, grund="Testbeleg")
        assert (ergebnis.schon, ergebnis.grund, ergebnis.vorjahre) == (False, "Testbeleg", ())
        beleg = dconn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert beleg["geloescht_am"] and beleg["geloescht_grund"] == "Testbeleg"
        assert beleg["betrag_brutto_cent"] == 546, "der Datensatz bleibt"
        # Datei, Auslesung und Zahlung bleiben, die Datei schreibgeschuetzt.
        datei = dconn.execute("SELECT * FROM belegdatei WHERE beleg_id = ?", (bid,)).fetchone()
        archiv = datenordner / datei["pfad"]
        assert archiv.exists() and archiv.stat().st_mode & 0o777 == 0o444
        assert _anzahl(dconn, "SELECT COUNT(*) FROM zahlung WHERE beleg_id = ?", bid) == 1
        eintrag = dconn.execute("SELECT * FROM journal WHERE aktion = 'BELEG_GELOESCHT'"
                                ).fetchone()
        abbild = json.loads(eintrag["wert_alt"])
        assert abbild["beleg"]["betrag_brutto_cent"] == 546
        assert abbild["beleg"]["belegnummer_extern"] == "RE-2026-0815"
        assert [d["sha256"] for d in abbild["dateien"]] == [datei["sha256"]]
        assert [(z["datum"], z["betrag_cent"]) for z in abbild["zahlungen"]] == \
            [("2026-03-20", -546)]
        assert abbild["auslesung"]["bestaetigt"]["belegnummer"] == "RE-2026-0815"
        assert (eintrag["begruendung"], eintrag["hash"]) == ("Testbeleg", datei["sha256"])
        assert json.loads(eintrag["wert_neu"])["grund"] == "Testbeleg"

    def test_journalspiegel_kuerzt_das_abbild_nicht(self, dconn, datenordner, tmp_path):
        """journal.log kuerzt Felder auf 300 Zeichen -- das Abbild eines
        geloeschten Belegs stuende dort sonst abgeschnitten."""
        from app.logik import journal_export

        bid = _buchen(dconn, datenordner)
        loeschen.loeschen(dconn, bid, grund="Testbeleg")
        text = journal_export.schreibe(dconn, tmp_path / "journal.log").read_text("utf-8")
        [zeile] = [z for z in text.splitlines() if "BELEG_GELOESCHT" in z]
        assert len(zeile) > 600 and "…" not in zeile
        assert '"betrag_brutto_cent": 546' in zeile and '"zahlungen": [' in zeile

    def test_ohne_grund_abgewiesen(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner)
        for grund in ("", "  ", "x", "\x00\x01ab"):
            with pytest.raises(loeschen.BelegFehler, match="Grund"):
                loeschen.loeschen(dconn, bid, grund=grund)
        assert _anzahl(dconn, "SELECT COUNT(*) FROM v_beleg_gueltig") == 1

    def test_doppelklick_schreibt_einen_eintrag(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner)
        loeschen.loeschen(dconn, bid, grund="doppelt erfasst")
        zweiter = loeschen.loeschen(dconn, bid, grund="nochmal")
        assert (zweiter.schon, zweiter.grund) == (True, "doppelt erfasst")
        assert _anzahl(dconn, "SELECT COUNT(*) FROM journal WHERE aktion = 'BELEG_GELOESCHT'"
                       ) == 1

    def test_vorjahr_wird_genannt(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner, datum="20251114", bezahlt="2025-11-20",
                      nummer="RE-2025-0001")
        assert loeschen.loeschen(dconn, bid, grund="Testbeleg", laufend=2026).vorjahre == (2025,)

    def test_wiederherstellen(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner)
        loeschen.loeschen(dconn, bid, grund="Fehlklick")
        ergebnis = loeschen.wiederherstellen(dconn, bid)
        assert not ergebnis.schon
        beleg = dconn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert (beleg["geloescht_am"], beleg["geloescht_grund"]) == (None, None)
        eintrag = dconn.execute("SELECT * FROM journal WHERE aktion = 'BELEG_WIEDERHERGESTELLT'"
                                ).fetchone()
        assert json.loads(eintrag["wert_alt"])["grund"] == "Fehlklick"
        assert loeschen.wiederherstellen(dconn, bid).schon
        assert euer.auswertung(dconn, 2026).ausgaben_cent == 546

    def test_wiederherstellen_nicht_wenn_die_datei_neu_gebucht_ist(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner)
        loeschen.loeschen(dconn, bid, grund="falsch ausgelesen")
        neu = _buchen(dconn, datenordner)
        with pytest.raises(loeschen.BelegFehler, match=f"inzwischen als Beleg {neu} gebucht"):
            loeschen.wiederherstellen(dconn, bid)

    def test_wiederherstellen_warnt_vor_aehnlichem(self, dconn, datenordner):
        """Neu gebucht aus einer ANDEREN Datei derselben Rechnung (besserer
        Scan): kein Hindernis, aber eine Warnung."""
        bid = _buchen(dconn, datenordner)
        loeschen.loeschen(dconn, bid, grund="falsch ausgelesen")
        anders = cii() + b"\n<!-- zweiter Export -->\n"
        neu = _buchen(dconn, datenordner, inhalt=anders)
        ergebnis = loeschen.wiederherstellen(dconn, bid)
        assert [b["id"] for b in ergebnis.aehnliche] == [neu]

    def test_neu_erfassen_legt_die_datei_in_den_eingang(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner)
        with pytest.raises(loeschen.BelegFehler, match="nicht gelöscht"):
            loeschen.neu_erfassen(dconn, bid, datenordner)
        loeschen.loeschen(dconn, bid, grund="falsch ausgelesen")
        eingang = loeschen.neu_erfassen(dconn, bid, datenordner)
        assert eingang.pfad.parent == datenordner / p.EINGANG
        assert eingang.original_name == "RE-2026-0815.xml"
        assert eingang.pfad.read_bytes() == cii()
        datei = dconn.execute("SELECT pfad FROM belegdatei WHERE beleg_id = ?", (bid,)).fetchone()
        assert (datenordner / datei["pfad"]).exists(), "kopiert, nicht verschoben"


class TestDieselbeDateiNeu:
    """Hochladen -> buchen -> loeschen -> dieselbe Datei hochladen -> buchen:
    die EUeR zaehlt genau einmal, die Archivdatei ist geteilt."""

    def test_neu_buchen_nach_dem_loeschen(self, dconn, datenordner):
        erster = _buchen(dconn, datenordner)
        with pytest.raises(p.SchonGebucht):
            _buchen(dconn, datenordner)
        loeschen.loeschen(dconn, erster, grund="falsch ausgelesen")
        eingang = p.aufnehmen(cii(), "RE-2026-0815.xml", datenordner)
        lesung = p.lies(dconn, eingang, heute=HEUTE)
        assert lesung.dublette_von is None
        assert lesung.aehnliche == () and lesung.aehnliche_andere == ()
        assert any(f"war Beleg {erster} — gelöscht (falsch ausgelesen)" in m
                   for m in lesung.meldungen)
        zweiter = _buchen(dconn, datenordner)
        assert euer.auswertung(dconn, 2026).ausgaben_cent == 546
        dateien = dconn.execute("SELECT beleg_id, pfad, sha256 FROM belegdatei ORDER BY id"
                                ).fetchall()
        assert [d["beleg_id"] for d in dateien] == [erster, zweiter]
        assert dateien[0]["pfad"] == dateien[1]["pfad"], "dieselbe Archivdatei"
        archiv = datenordner / dateien[0]["pfad"]
        assert archiv.stat().st_mode & 0o777 == 0o444
        assert p.liegen_geblieben(datenordner) == []
        assert p.pruefe_integritaet(dconn, datenordner) == []
        # Und jetzt ist es wieder eine Dublette -- des neuen Belegs.
        assert p.dublette(dconn, dateien[0]["sha256"]) == zweiter

    def test_anderes_belegdatum_legt_die_datei_ein_zweites_mal_ab(self, dconn, datenordner):
        """Falsch ausgelesen war gerade das Datum: die neue Buchung bekommt
        ihren eigenen Archivpfad, die alte Datei bleibt beim geloeschten."""
        erster = _buchen(dconn, datenordner)
        loeschen.loeschen(dconn, erster, grund="falsch ausgelesen")
        eingang = p.aufnehmen(cii(), "RE-2026-0815.xml", datenordner)
        lesung = p.lies(dconn, eingang, heute=HEUTE)
        zweiter = p.buche(dconn, lesung, {"belegdatum": "2026-03-15", "brutto": 546,
                                          "waehrung": "EUR", "belegnummer": "RE-2026-0815"},
                          datenordner, lieferant_id=_lieferant(dconn), heute=HEUTE).beleg_id
        pfade = [z["pfad"] for z in dconn.execute(
            "SELECT pfad FROM belegdatei WHERE beleg_id IN (?, ?) ORDER BY beleg_id",
            (erster, zweiter))]
        assert pfade[0] != pfade[1]
        assert all((datenordner / pfad).exists() for pfad in pfade)
        assert p.pruefe_integritaet(dconn, datenordner) == []

    def test_e_mail_mit_rechnung_im_anhang(self, dconn, datenordner):
        """Rolle EMAIL: die Mail zaehlt im Dublettencheck mit -- nach dem
        Loeschen nicht mehr, und Mail und Anhang lassen sich neu buchen."""
        from tests.test_email_belege import mail

        inhalt = mail(anhaenge=[("rechnung.xml", cii(), "application/xml")])
        eingang = p.aufnehmen(inhalt, "Rechnung.eml", datenordner)
        lesung = p.lies(dconn, eingang, heute=HEUTE)
        erster = p.buche(dconn, lesung, {"belegdatum": "2026-03-14", "brutto": 546,
                                         "waehrung": "EUR", "belegnummer": "RE-2026-0815"},
                         datenordner, lieferant_id=_lieferant(dconn), heute=HEUTE).beleg_id
        rollen = [z["rolle"] for z in dconn.execute(
            "SELECT rolle FROM belegdatei WHERE beleg_id = ? ORDER BY id", (erster,))]
        assert rollen == ["ORIGINAL", "EMAIL"]
        loeschen.loeschen(dconn, erster, grund="Testbeleg")
        neu = loeschen.neu_erfassen(dconn, erster, datenordner)
        assert neu.original_name == "Rechnung.eml", "die Mail kommt zurueck"
        lesung = p.lies(dconn, neu, heute=HEUTE)
        assert lesung.dublette_von is None
        zweiter = p.buche(dconn, lesung, {"belegdatum": "2026-03-14", "brutto": 546,
                                          "waehrung": "EUR", "belegnummer": "RE-2026-0815"},
                          datenordner, lieferant_id=_lieferant(dconn), heute=HEUTE).beleg_id
        assert zweiter != erster
        assert p.pruefe_integritaet(dconn, datenordner) == []

    def test_abholen_derselben_datei_nach_dem_loeschen(self, dconn, datenordner, tmp_path):
        """Das Abholen aus dem Eingangsordner erbt das: dieselbe Datei ist
        nach dem Loeschen keine Dublette mehr."""
        from app.logik.beleg import abholen
        from tests.test_belegordner import lege

        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        erster = _buchen(dconn, datenordner)
        lege(postfach, "Rechnung.xml", cii())
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(abholen, "MINDESTALTER_SEKUNDEN", 0)
            ergebnis = abholen.abholen(dconn, datenordner, postfach, automatisch=False,
                                       heute=HEUTE)
            assert [s.beleg_id for s in ergebnis.schon_vorhanden] == [erster]
            loeschen.loeschen(dconn, erster, grund="Testbeleg")
            lege(postfach, "Rechnung.xml", cii())
            ergebnis = abholen.abholen(dconn, datenordner, postfach, automatisch=False,
                                       heute=HEUTE)
        assert ergebnis.schon_vorhanden == []
        assert len(ergebnis.wartend) == 1


class TestAnlagegut:
    def _mit_gut(self, dconn, datenordner, anschaffung="2025-11-14") -> tuple[int, int]:
        datum = anschaffung.replace("-", "")
        bid = _buchen(dconn, datenordner, nummer="RE-2025-0042", datum=datum,
                      inhalt=cii("RE-2025-0042", datum, brutto="1785.00", netto="1500.00",
                                 ust="285.00"),
                      brutto=178500, kategorie="HARDWARE_ANLAGE", bezahlt=anschaffung,
                      ist_anlagegut=True)
        gid = anlagen.anlegen(dconn, bezeichnung="Notebook", anschaffung_am=anschaffung,
                              ak_brutto_cent=178500, ak_netto_cent=150000,
                              nutzungsdauer_jahre=3, beleg_id=bid)
        return bid, gid

    def test_rueckfrage_zaehlt_afa_jahre_auf_und_warnt_vor_vorjahren(self, dconn, datenordner):
        bid, _ = self._mit_gut(dconn, datenordner)
        with pytest.raises(loeschen.AnlagegutRueckfrage) as fehler:
            loeschen.loeschen(dconn, bid, grund="Testbeleg", laufend=2026)
        text = str(fehler.value)
        assert "Anlagegut „Notebook“" in text
        assert "2025: 99,16 €, 2026: 595,00 €, 2027: 595,00 €, 2028: 495,84 €" in text
        assert "die AfA 2025 ist evtl. in einer abgegebenen EÜR enthalten" in text
        assert _anzahl(dconn, "SELECT COUNT(*) FROM v_beleg_gueltig") == 1

    def test_mitgeloescht_bringt_es_keine_afa_mehr(self, dconn, datenordner):
        bid, gid = self._mit_gut(dconn, datenordner)
        assert anlagen.afa_summe(dconn, 2026) == {130: 59500}
        ergebnis = loeschen.loeschen(dconn, bid, grund="Testbeleg", anlagegut_mit=True,
                                     laufend=2026)
        assert ergebnis.anlagegut.id == gid and ergebnis.vorjahre == (2025,)
        assert anlagen.afa_summe(dconn, 2026) == {}
        assert anlagen.verzeichnis(dconn, 2026) == []
        assert 2027 not in euer.jahre(dconn)
        assert _anzahl(dconn, "SELECT COUNT(*) FROM anlagegut_afa_jahr WHERE anlagegut_id = ?",
                       gid) == 4, "die Reihe bleibt nachvollziehbar"
        eintrag = dconn.execute("SELECT * FROM journal WHERE aktion = 'ANLAGEGUT_GELOESCHT'"
                                ).fetchone()
        assert json.loads(eintrag["wert_alt"])["afa"][0] == [2025, 9916]
        with pytest.raises(anlagen.AnlagenFehler, match="gelöscht"):
            anlagen.abgang(dconn, gid, am="2026-05-01", grund="verkauft")
        with pytest.raises(anlagen.AnlagenFehler, match="gelöscht"):
            anlagen.anlegen(dconn, bezeichnung="Notebook", anschaffung_am="2025-11-14",
                            ak_brutto_cent=178500, nutzungsdauer_jahre=3, beleg_id=bid)
        # Zurueck kommt beides.
        assert loeschen.wiederherstellen(dconn, bid).anlagegut == "Notebook"
        assert anlagen.afa_summe(dconn, 2026) == {130: 59500}

    def test_gwg_verzeichnis_ohne_geloeschte(self, dconn, datenordner):
        bid = _buchen(dconn, datenordner, inhalt=cii("RE-2026-0900", brutto="476.00",
                                                     netto="400.00", ust="76.00"),
                      nummer="RE-2026-0900", brutto=47600, ist_anlagegut=True)
        anlagen.anlegen(dconn, bezeichnung="Monitor", anschaffung_am="2026-03-14",
                        ak_brutto_cent=47600, ak_netto_cent=40000, beleg_id=bid)
        assert len(anlagen.gwg_verzeichnis(dconn, 2026)) == 1
        loeschen.loeschen(dconn, bid, grund="Testbeleg", anlagegut_mit=True)
        assert anlagen.gwg_verzeichnis(dconn, 2026) == []


class TestEigenbelegUndReise:
    def test_eigenbeleg_ist_loeschbar(self, dconn):
        bid = eigenbeleg.erfasse(dconn, belegdatum="2026-03-14", brutto_cent=1200,
                                 grund="Parkschein verloren, Parkhaus am Bahnhof",
                                 kategorie_id=_kategorie(dconn, "SONSTIGE")
                                 if kategorien.nach_schluessel(dconn, "SONSTIGE") else None,
                                 bezahlt_am="2026-03-14")
        loeschen.loeschen(dconn, bid, grund="Testbeleg")
        assert euer.fehlende_zuordnung(dconn).eigenbelege_offen == 0
        assert euer.auswertung(dconn, 2026).ausgaben_cent == 0
        with pytest.raises(loeschen.BelegFehler, match="keine Datei"):
            loeschen.neu_erfassen(dconn, bid, Path("/nirgends"))

    def test_verpflegung_neu_buchbar_und_nicht_doppelt_zurueck(self, dconn):
        reise = eigenbeleg.reise_anlegen(
            dconn, bezeichnung="Messe", anlass="Kundentermin", ziel="Köln",
            abreise_am="2026-03-10", rueckkehr_am="2026-03-12")
        erste = eigenbeleg.verpflegung_buchen(dconn, reise)
        with pytest.raises(loeschen.BelegFehler, match="schon als Beleg"):
            eigenbeleg.verpflegung_buchen(dconn, reise)
        loeschen.loeschen(dconn, erste, grund="falsche Reisedaten")
        assert eigenbeleg.reisebelege(dconn, reise) == []
        zweite = eigenbeleg.verpflegung_buchen(dconn, reise)
        with pytest.raises(loeschen.BelegFehler, match=f"inzwischen als Beleg {zweite}"):
            loeschen.wiederherstellen(dconn, erste)
        [r] = eigenbeleg.reisen(dconn)
        assert r["belege"] == 1


class TestAuswertungen:
    """Vorher/nachher: jede Auswertung laesst den geloeschten Beleg aus."""

    def test_euer_hinweise_und_buchungsblatt(self, dconn, datenordner):
        bezahlt = _buchen(dconn, datenordner)
        # Gleicher Lieferant, Tag und Betrag: ausdruecklich andere Belege.
        ohne_kategorie = _buchen(dconn, datenordner, nummer="RE-2026-0816", kategorie=None,
                                 trotzdem=True)
        automatisch = _buchen(dconn, datenordner, nummer="RE-2026-0817", automatisch=True,
                              trotzdem=True)
        anlage = _buchen(dconn, datenordner, nummer="RE-2026-0818", ist_anlagegut=True,
                         bezahlt=None, trotzdem=True)
        vorher = euer.auswertung(dconn, 2026)
        assert vorher.ausgaben_cent == 2 * 546
        assert euer.fehlende_zuordnung(dconn, 2026).anzahl == 1
        assert euer.unbestaetigt_bezahlt(dconn, 2026).anzahl == 1
        assert euer.anlagegut_ohne_verzeichnis(dconn, 2026).anzahl == 1
        assert len(euer.buchungen(dconn, 2026)) == 3
        for bid in (bezahlt, ohne_kategorie, automatisch, anlage):
            loeschen.loeschen(dconn, bid, grund="Testbeleg")
        nachher = euer.auswertung(dconn, 2026)
        assert nachher.ausgaben_cent == 0
        assert not euer.fehlende_zuordnung(dconn, 2026)
        assert not euer.unbestaetigt_bezahlt(dconn, 2026)
        assert not euer.anlagegut_ohne_verzeichnis(dconn, 2026)
        assert euer.buchungen(dconn, 2026) == []
        assert euer.jahre(dconn) == []

    def test_excel_eur_blatt_und_rohdaten(self, dconn, datenordner, tmp_path):
        bid = _buchen(dconn, datenordner)
        _buchen(dconn, datenordner, nummer="RE-2026-0816", inhalt=cii("RE-2026-0816",
                brutto="11.90", netto="10.00", ust="1.90"), brutto=1190)
        loeschen.loeschen(dconn, bid, grund="Testbeleg")
        export_excel.schreibe(dconn, tmp_path / "x.xlsx")
        mappe = load_workbook(tmp_path / "x.xlsx")
        zeilen = list(mappe["EÜR"].iter_rows(min_row=2, values_only=True))
        assert (2026, "Summe Ausgaben", None, None, 11.9, None) in zeilen
        assert len(list(mappe["Buchungen"].iter_rows(min_row=2))) == 1
        blatt = mappe["Belege"]
        kopf = [z.value for z in blatt[1]]
        assert "Gelöscht am (UTC)" in kopf and "Löschgrund" in kopf
        werte = [z.value for z in blatt[2]]
        assert werte[kopf.index("Löschgrund")] == "Testbeleg"
        assert werte[kopf.index("Gelöscht am (UTC)")] is not None
        assert mappe["Belege"].max_row == 3, "der geloeschte bleibt im Rohblatt"


class TestWaechter:
    """Jede Auswertung liest ueber v_beleg_gueltig / v_anlagegut_gueltig.
    Wer die Tabelle direkt liest, steht hier mit Grund -- ein neuer Zugriff
    faellt auf, statt den Filter still zu vergessen."""

    #: (Datei, Zeile ohne Einrueckung) -> Grund.
    ERLAUBT = {
        ("app/main.py", '"SELECT EXISTS (SELECT 1 FROM beleg WHERE waehrung <> \'EUR\')"'):
            "Start: ob die Kurstabelle ueberhaupt gebraucht wird",
        ("app/logik/export_excel.py",
         '"(SELECT COALESCE(b.belegnummer_extern, \'Beleg \' || b.id) FROM beleg b"'):
            "Rohexport: Klartext neben jeder beleg_id, auch der geloeschten",
        ("app/logik/export_excel.py",
         '"(SELECT a.bezeichnung FROM anlagegut a WHERE a.id = t.anlagegut_id)",'):
            "Rohexport: Klartext neben jeder anlagegut_id",
        ("app/logik/beleg/pipeline.py",
         '" JOIN beleg b ON b.id = d.beleg_id"'): "frueher_geloescht: gerade die geloeschten",
        ("app/logik/beleg/pipeline.py",
         '" JOIN beleg b ON b.id = d.beleg_id ORDER BY b.belegdatum, d.id"'):
            "Integritaetspruefung: jede Datei, auch die geloeschter Belege",
        ("app/routen/belege.py", '" JOIN beleg b ON b.id = d.beleg_id"'):
            "Datei nicht im Eingang: auch geloeschte nennen",
        ("app/routen/belege.py", "FROM beleg b"): "Belegliste mit „gelöschte zeigen“",
        ("app/routen/belege.py", 'f" FROM beleg b {wo}",'): "Belegliste: Summe (CASE)",
        ("app/routen/belege.py", '"SELECT COUNT(*) AS n FROM beleg b WHERE b.geloescht_am'
                                 ' IS NOT NULL"'): "Belegliste: ausgeblendete zaehlen",
        ("app/routen/belege.py", '"SELECT DISTINCT substr(belegdatum, 1, 4) AS jahr FROM beleg"'):
            "Belegliste: Jahre, mit Bedingung",
        ("app/logik/anlagen.py", '"SELECT bezeichnung FROM anlagegut WHERE beleg_id = ?",'
                                 ' (beleg_id,)'):
            "anlegen: ein Gut je Beleg, auch ein geloeschtes zaehlt (UNIQUE-Index)",
        ("app/logik/beleg/umrechnung.py",
         'return conn.execute("SELECT id, bezeichnung, ak_brutto_cent FROM anlagegut"'):
            "Aenderung am gueltigen Beleg: dessen Gut",
        ("app/logik/beleg/loeschen.py", '" JOIN beleg b ON b.id = d.beleg_id"'):
            "Loeschen selbst",
        ("app/logik/beleg/abholen.py",
         '"SELECT b.betrag_brutto_cent, b.belegdatum, l.name FROM beleg b"'):
            "Einzelzugriff per id auf den eben gebuchten Beleg",
        ("app/logik/beleg/sammelbuchung.py",
         '"SELECT b.betrag_brutto_cent, b.kategorie_id, l.name FROM beleg b"'):
            "Einzelzugriff per id auf den eben gebuchten Beleg (Sammelbuchung)",
    }
    #: Einzelzugriffe per Kennung -- sie sehen den Beleg, ob geloescht oder nicht.
    EINZELN = re.compile(r"FROM (beleg|anlagegut)(\s+[a-z])? WHERE (id|beleg_id) = \?"
                         r"|FROM anlagegut WHERE beleg_id = \?|FROM anlagegut WHERE id = \?")

    def test_kein_direkter_zugriff_ohne_grund(self):
        wurzel = Path(__file__).resolve().parent.parent
        muster = re.compile(r"\b(FROM|JOIN)\s+(beleg|anlagegut)\b(?!_)")
        funde = []
        for datei in sorted((wurzel / "app").rglob("*.py")):
            name = datei.relative_to(wurzel).as_posix()
            for zeile in datei.read_text(encoding="utf-8").splitlines():
                text = zeile.strip()
                if text.startswith("#") or not muster.search(text):
                    continue
                if self.EINZELN.search(text) or (name, text) in self.ERLAUBT:
                    continue
                funde.append(f"{name}: {text}")
        assert funde == [], "Ohne v_beleg_gueltig/v_anlagegut_gueltig:\n" + "\n".join(funde)


# ============================================================ Oberflaeche

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Musterstraße 1", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}


@pytest.fixture
def klient(tmp_path):
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


def _web_buchen(klient, datenbank, inhalt: bytes | None = None, **felder) -> str:
    antwort = klient.post("/belege/hochladen",
                          files={"datei": ("rechnung.xml", inhalt or cii(), "application/xml")},
                          follow_redirects=False)
    name = antwort.headers["location"].rsplit("/", 1)[1]
    with datenbank() as conn:
        hosting = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel = 'HOSTING'"
                               ).fetchone()[0]
    daten = {"belegdatum": "2026-03-14", "brutto_euro": "5,46", "waehrung": "EUR",
             "belegnummer": "RE-2026-0815", "lieferant_neu": "Beispiel GmbH",
             "kategorie_id": str(hosting), "quote": "100", "bezahlt_am": "2026-03-20",
             **felder}
    return klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)


def _ausgaben(tmp_path, jahr=2026) -> int:
    conn = db.oeffne(tmp_path / "steuer.sqlite")
    try:
        return euer.auswertung(conn, jahr).ausgaben_cent
    finally:
        conn.close()


class TestOberflaeche:
    def test_loeschen_in_zwei_schritten_mit_grund(self, klient, datenbank, tmp_path):
        _web_buchen(klient, datenbank)
        seite = klient.get("/belege").text
        assert 'action="/belege/1/loeschen"' in seite
        assert '<details class="loeschen">' in seite
        for grund in loeschen.GRUENDE:
            assert f'value="{grund}"' in seite
        ohne = klient.post("/belege/1/loeschen", data={}, follow_redirects=False)
        assert _meldung(ohne)[0] == "fehler" and "Grund" in _meldung(ohne)[1]
        antwort = klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"},
                              follow_redirects=False)
        art, text = _meldung(antwort)
        assert art == "erfolg"
        assert text.startswith("Beleg 1 gelöscht (Testbeleg).")
        assert _ausgaben(tmp_path) == 0
        seite = klient.get("/belege").text
        assert 'id="beleg-1"' not in seite
        assert "1 gelöschter Beleg ist" in seite and "gelöschte zeigen" in seite
        mit = klient.get("/belege?geloescht=1").text
        zeile = re.search(r'<tr id="beleg-1" class="geloescht">.*?</tr>', mit, re.S).group(0)
        assert "gelöscht" in zeile and "Testbeleg" in zeile
        assert 'action="/belege/1/wiederherstellen"' in zeile
        assert "Stimmt so" not in zeile and 'action="/belege/1/kategorie"' not in zeile
        assert "Betrieblich gesamt" in mit and "0,00 €" in mit
        # Die Datei des geloeschten Belegs bleibt einsehbar.
        assert klient.get("/belege/1/datei").status_code == 200

    def test_freier_grund_und_doppelklick(self, klient, datenbank):
        _web_buchen(klient, datenbank)
        klient.post("/belege/1/loeschen", data={"grund": "doppelt erfasst",
                                                "grund_frei": "zweites PDF"})
        zweiter = klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"},
                              follow_redirects=False)
        assert _meldung(zweiter) == ("erfolg",
                                     "Beleg 1 war schon gelöscht (doppelt erfasst: zweites PDF).")
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'BELEG_GELOESCHT'"
                                ).fetchone()[0] == 1

    def test_wiederherstellen(self, klient, datenbank, tmp_path):
        _web_buchen(klient, datenbank)
        klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"})
        antwort = klient.post("/belege/1/wiederherstellen", follow_redirects=False)
        assert _meldung(antwort) == ("erfolg", "Beleg 1 wiederhergestellt — er zählt wieder "
                                               "in der EÜR.")
        assert _ausgaben(tmp_path) == 546
        assert 'id="beleg-1">' in klient.get("/belege").text

    def test_dieselbe_datei_neu_hochladen_und_buchen(self, klient, datenbank, tmp_path):
        _web_buchen(klient, datenbank)
        klient.post("/belege/1/loeschen", data={"grund": "falsch ausgelesen"})
        antwort = klient.post("/belege/hochladen",
                              files={"datei": ("rechnung.xml", cii(), "application/xml")})
        assert "Diese Datei war Beleg 1 — gelöscht (falsch ausgelesen)" in antwort.text
        assert "bereits als Beleg" not in antwort.text
        assert _web_buchen(klient, datenbank).status_code == 303
        assert _ausgaben(tmp_path) == 546
        with datenbank() as conn:
            assert [tuple(z) for z in conn.execute(
                "SELECT id, geloescht_grund FROM beleg ORDER BY id")] == \
                [(1, "falsch ausgelesen"), (2, None)]

    def test_neu_erfassen_oeffnet_die_pruefmaske(self, klient, datenbank):
        _web_buchen(klient, datenbank)
        antwort = klient.post("/belege/1/loeschen", data={"grund": "falsch ausgelesen",
                                                          "neu_erfassen": "1"},
                              follow_redirects=False)
        assert antwort.headers["location"].startswith("/belege/pruefen/")
        maske = klient.get(antwort.headers["location"]).text
        assert "Diese Datei war Beleg 1" in maske

    def test_anlagegut_braucht_den_haken(self, klient, datenbank):
        _web_buchen(klient, datenbank, anlagegut="1")
        with datenbank() as conn:
            conn.execute("INSERT INTO anlagegut (beleg_id, bezeichnung, anschaffung_am,"
                         " ak_brutto_cent, ak_netto_cent, nutzungsdauer_jahre, erfasst_am)"
                         " VALUES (1, 'Kabel', '2026-03-14', 546, 459, 1, 'x')")
            conn.execute("INSERT INTO anlagegut_afa_jahr VALUES (1, 2026, 546, 132, 0)")
        seite = klient.get("/belege").text
        assert "Anlagegut „Kabel“" in seite and 'name="anlagegut_mit"' in seite
        ohne = klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"},
                           follow_redirects=False)
        art, text = _meldung(ohne)
        assert art == "fehler" and "2026: 5,46 €" in text and "anhaken" in text
        # Der zweite Versuch beginnt nicht mit dem Aufklappen.
        assert '<details class="loeschen" open>' in klient.get(ohne.headers["location"]).text
        mit = klient.post("/belege/1/loeschen", data={"grund": "Testbeleg",
                                                      "anlagegut_mit": "1"},
                          follow_redirects=False)
        assert "Das Anlagegut „Kabel“ ist mit gelöscht." in _meldung(mit)[1]

    def test_vorjahr_warnt(self, klient, datenbank, tmp_path):
        _web_buchen(klient, datenbank, inhalt=cii("RE-2025-0001", "20251114"),
                    belegdatum="2025-11-14", belegnummer="RE-2025-0001",
                    bezahlt_am="2025-11-20")
        assert "Die Zahlung steht in der EÜR 2025" in klient.get("/belege").text
        antwort = klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"},
                              follow_redirects=False)
        art, text = _meldung(antwort)
        assert art == "warnung"
        assert "Achtung: der Beleg stand in der EÜR 2025 — ist die Erklärung" in text
        seite = klient.get(antwort.headers["location"]).text
        assert '<div class="meldung warn' in seite

    def test_geloeschter_beleg_laesst_sich_nicht_aendern(self, klient, datenbank):
        _web_buchen(klient, datenbank, bezahlt_am="")
        klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"})
        for pfad, daten in (("kategorie", {"quote": "50"}), ("bezahlt", {"datum": "2026-03-20"}),
                            ("geprueft", {}), ("kartenbetrag", {"betrag": "5,00"}),
                            ("zahltag", {"datum": "2026-03-21"})):
            antwort = klient.post(f"/belege/1/{pfad}", data=daten, follow_redirects=False)
            art, text = _meldung(antwort)
            assert art == "fehler" and "gelöscht" in text, pfad
        with datenbank() as conn:
            assert conn.execute("SELECT aufteilungsquote FROM beleg").fetchone()[0] == 100
            assert conn.execute("SELECT COUNT(*) FROM zahlung").fetchone()[0] == 0

    def test_uebersicht_und_eur_seite_ohne_den_beleg(self, klient, datenbank):
        """Der offene Hinweis auf der Uebersicht geht mit dem Beleg -- und die
        EUeR-Seite zeigt seinen Betrag nicht mehr."""
        _web_buchen(klient, datenbank, kategorie_id="")
        _web_buchen(klient, datenbank, inhalt=cii("RE-2026-0816"), belegnummer="RE-2026-0816",
                    trotzdem="1")
        assert "1 bezahlter Beleg" in klient.get("/").text
        assert "5,46" in klient.get("/euer?jahr=2026").text
        # Die Steuerrücklage rechnet mit dem Gewinn laut EÜR.
        assert "5,46 €</strong>" in klient.get("/steuer?jahr=2026").text
        klient.post("/belege/1/loeschen", data={"grund": "Testbeleg"})
        klient.post("/belege/2/loeschen", data={"grund": "Testbeleg"})
        assert "bezahlter Beleg" not in klient.get("/").text
        assert "5,46" not in klient.get("/euer?jahr=2026").text
        assert "5,46 €</strong>" not in klient.get("/steuer?jahr=2026").text


class TestKategorieAlsVorgabe:
    """Kategorie in der Liste aendern -- und als Vorgabe des Lieferanten merken."""

    def test_haken_merkt_kategorie_und_quote(self, klient, datenbank):
        _web_buchen(klient, datenbank, kategorie_id="")
        seite = klient.get("/belege").text
        assert "als Vorgabe für Beispiel GmbH merken" in seite
        with datenbank() as conn:
            software = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel ="
                                    " 'HOSTING'").fetchone()[0]
        antwort = klient.post("/belege/1/kategorie",
                              data={"kategorie_id": str(software), "quote": "80",
                                    "vorgabe_merken": "1"}, follow_redirects=False)
        assert _meldung(antwort) == ("erfolg", "Beleg 1: gespeichert. Kategorie und Quote sind "
                                               "ab jetzt Vorgabe für Beispiel GmbH.")
        with datenbank() as conn:
            lieferant = conn.execute("SELECT * FROM lieferant WHERE name = 'Beispiel GmbH'"
                                     ).fetchone()
            assert (lieferant["standard_kategorie_id"], lieferant["quote_default"]) == \
                (software, 80)
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'AENDERN'"
                                " AND objekttyp = 'lieferant'").fetchone()[0] == 2

    def test_ohne_haken_bleibt_der_lieferant(self, klient, datenbank):
        _web_buchen(klient, datenbank)
        antwort = klient.post("/belege/1/kategorie", data={"kategorie_id": "", "quote": "50"},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/belege"
        with datenbank() as conn:
            lieferant = conn.execute("SELECT * FROM lieferant").fetchone()
            assert lieferant["standard_kategorie_id"] is None

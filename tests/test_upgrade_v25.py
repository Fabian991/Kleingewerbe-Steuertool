"""Upgrade der echten Lage: eine Datenbank im Stand 25 mit Daten -> Stand 26.

Der Nutzer arbeitet mit einer Datenbank auf Schema 25 (Pakete A bis F), in
der Daten stehen. Paket G bringt Migration 26 (Einnahmen ohne Rechnung,
WISO-Import, „Angaben fehlen“). Der Bestand kommt aus
``tests/altbestand_v25.py`` (echtes Schema 25 aus Commit 28b08a3, reines
SQL, dazu ein Beleg mit Loeschmarkierung). Geprueft wird:

- Migration 26 laeuft einmal und steht im Journal;
- KEIN Datensatz geht verloren oder aendert sich -- in keiner Tabelle,
  keiner Spalte;
- EUeR, § 19-Umsatz und Steuerruecklage ergeben auf den Cent dasselbe wie
  im Upgrade-Test ab Version 20 (der geloeschte Beleg zaehlt nicht);
- das migrierte Schema gleicht dem einer frischen Datenbank;
- der WISO-Import und die neuen Seiten arbeiten auf dem migrierten Bestand.

Die Kette ab Version 20 (20 -> 26 in einem Zug) prueft ``test_upgrade_v20.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import db
from app.logik import euer, export_excel, grenzwert, steuerschaetzung, wiso
from tests import altbestand_v25
from tests import wiso_beispiel as b
from tests.test_upgrade_v20 import REFERENZ, _schema

HEUTE = date(2026, 10, 27)
NEU = ("einnahme", "einnahmedatei", "zahler", "wiso_import", "wiso_buchung", "wiso_konto",
       "fehlende_angabe")


@pytest.fixture
def altbestand(tmp_path) -> Path:
    pfad = tmp_path / "steuer.sqlite"
    altbestand_v25.baue(pfad, tmp_path / "Eingang")
    return pfad


def _alles(pfad: Path) -> dict[str, tuple[list[str], list[tuple]]]:
    """Jede Tabelle mit Spalten und Zeilen, direkt mit sqlite3 gelesen."""
    conn = sqlite3.connect(pfad)
    try:
        ergebnis = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                    " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            cur = conn.execute(f"SELECT * FROM {name} ORDER BY rowid")
            ergebnis[name] = ([d[0] for d in cur.description], cur.fetchall())
        return ergebnis
    finally:
        conn.close()


def test_migration_26_einmal_mit_journal(altbestand):
    conn = db.oeffne(altbestand)
    try:
        assert db.SCHEMA_VERSION == 26
        assert [tuple(z) for z in conn.execute("SELECT version FROM schema_version"
                                               " ORDER BY version")] == [(25,), (26,)]
        assert [z[0] for z in conn.execute(
            "SELECT objekt_id FROM journal WHERE aktion = 'MIGRATION'")] == [26]
        for tabelle in NEU:
            assert conn.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0] == 0, tabelle
        # Der Erloes eines Anlageguts als Einnahme (Zuordnung beim Abgang).
        assert "anlagegut_id" in {z["name"] for z in conn.execute(
            "PRAGMA table_info(einnahme)")}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    conn = db.oeffne(altbestand)
    try:
        assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                            ).fetchone()[0] == 1
    finally:
        conn.close()


def test_kein_datensatz_aendert_sich(altbestand):
    vorher = _alles(altbestand)
    db.oeffne(altbestand).close()
    nachher = _alles(altbestand)
    assert set(nachher) - set(vorher) == set(NEU)
    for tabelle, (spalten, zeilen) in vorher.items():
        neu_spalten, neu_zeilen = nachher[tabelle]
        assert neu_spalten == spalten, tabelle
        if tabelle in ("journal", "schema_version"):
            # Hier kommt die Migration dazu -- das Alte bleibt vorn, unveraendert.
            assert neu_zeilen[:len(zeilen)] == zeilen, tabelle
        else:
            assert neu_zeilen == zeilen, tabelle


def test_ergebnisse_auf_den_cent_wie_vorher(altbestand):
    conn = db.oeffne(altbestand)
    try:
        jahr = altbestand_v25.JAHR
        a = euer.auswertung(conn, jahr)
        assert (a.einnahmen_cent, a.ausgaben_cent, a.gewinn_cent) == (
            REFERENZ["einnahmen_cent"], REFERENZ["ausgaben_cent"], REFERENZ["gewinn_cent"])
        assert [(z.kennzahl, z.betrag_cent, z.nachrichtlich) for z in a.einnahmen] \
            == REFERENZ["einnahmen"]
        assert [(z.kennzahl, z.betrag_cent, z.nachrichtlich) for z in a.ausgaben] \
            == REFERENZ["ausgaben"]
        assert euer.jahre(conn) == REFERENZ["jahre"]
        assert len(euer.buchungen(conn, jahr)) == REFERENZ["buchungen"]
        assert grenzwert.vereinnahmt_cent(conn, jahr) == REFERENZ["grenzwert_cent"]
        e = steuerschaetzung.berechne(conn, jahr, heute=HEUTE)
        assert (e.mehrsteuer_cent, e.ruecklage_cent, e.offen_cent, e.zurueck_cent) \
            == REFERENZ["steuer"]
        # Der geloeschte Beleg bleibt geloescht und zaehlt nicht.
        assert conn.execute("SELECT COUNT(*) FROM v_beleg_gueltig").fetchone()[0] == 3
        assert conn.execute("SELECT geloescht_grund FROM beleg WHERE id = ?",
                            (altbestand_v25.GELOESCHT_ID,)).fetchone()[0] == "doppelt erfasst"
    finally:
        conn.close()


def test_schema_gleicht_einer_frischen_datenbank(altbestand, tmp_path):
    migriert = db.oeffne(altbestand)
    frisch = db.oeffne(tmp_path / "frisch.sqlite")
    try:
        alt, neu = _schema(migriert), _schema(frisch)
        assert alt["tabellen"].keys() == neu["tabellen"].keys()
        for tabelle, spalten in neu["tabellen"].items():
            assert alt["tabellen"][tabelle] == spalten, tabelle
        assert alt["andere"] == neu["andere"]
    finally:
        migriert.close()
        frisch.close()


def test_wiso_import_auf_dem_migrierten_bestand(altbestand, tmp_path):
    """Der Import arbeitet auf dem Altbestand -- und nimmt den geloeschten
    Beleg (50,00 € am 25.01.2026, wie Buchung 7) nicht als Kandidaten."""
    conn = db.oeffne(altbestand)
    try:
        jahr = altbestand_v25.JAHR
        datei = wiso.lies("buchungen.csv", b.datei(), heute=HEUTE)
        plan = wiso.plane(conn, datei, heute=HEUTE)
        siebte = next(p for p in plan.posten if p.zeile.buchungsnummer == "7")
        assert siebte.betrag_cent == altbestand_v25.GELOESCHT_CENT and siebte.kandidat is None
        ergebnis = wiso.uebernehmen(conn, tmp_path, "buchungen.csv", b.datei(),
                                    fingerabdruck=plan.fingerabdruck, auswahl=wiso.Auswahl(),
                                    heute=HEUTE)
        assert (ergebnis.belege, ergebnis.einnahmen) == (7, 6)
        a = euer.auswertung(conn, jahr)
        assert a.einnahmen_cent == REFERENZ["einnahmen_cent"] + b.EINNAHMEN_CENT \
            + b.ERSTATTUNG_CENT
        assert grenzwert.vereinnahmt_cent(conn, jahr) == REFERENZ["grenzwert_cent"] + 25000
        assert export_excel.als_bytes(conn)[:2] == b"PK"
        zweiter = wiso.plane(conn, wiso.lies("buchungen.csv", b.datei(), heute=HEUTE),
                             heute=HEUTE)
        assert zweiter.anzahl["neu"] == 0 and zweiter.anzahl["bekannt"] == 13
    finally:
        conn.close()


def test_neue_seiten_nach_dem_upgrade(altbestand):
    from fastapi.testclient import TestClient

    from app.main import erzeuge_app

    with TestClient(erzeuge_app(altbestand)) as klient:
        for pfad in ("/", "/wiso", "/wiso/abgleich", "/einnahmen", "/angaben", "/belege",
                     "/belege?ohne_datei=1", "/euer?jahr=2026", "/steuer?jahr=2026",
                     "/anlagen", "/backup/excel"):
            assert klient.get(pfad, follow_redirects=False).status_code == 200, pfad

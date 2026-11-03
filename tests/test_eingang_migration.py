"""Migration 24: die Vorgabe "Eingangsbelege sind in der Regel schon bezahlt"
und die Tabelle der Eingangsordner je Jahr.

Eine Datenbank im Stand davor (Version 20) mit Daten in den betroffenen
Tabellen wird migriert: die Daten bleiben, die neue Spalte und die neue
Tabelle sind da -- mit ihren CHECKs, wie in einer frischen Datenbank.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import db


def anzahl(verbindung, sql: str, *werte) -> int:
    return verbindung.execute(sql, werte).fetchone()[0]


class TestMigration:
    def test_von_20_auf_24(self, tmp_path):
        """Eine Datenbank aus Version 20 mit Betrieb, Eingangsordner, Beleg und
        Zahlung: danach gibt es die Einstellung (an) und die Tabelle der
        Jahresordner, alles andere ist, wie es war. Der Schritt steht im
        Journal, und das Schema gleicht einer frischen Datenbank."""
        pfad = tmp_path / "alt.sqlite"
        alt = db.oeffne(pfad)
        alt.execute("INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
                    " belegordner_pfad, belegordner_automatisch) VALUES (1, 'Alt',"
                    " 'Musterstraße 1', '20095', 'Hamburg', '12/345/67890', '/Belege/Eingang', 0)")
        lid = alt.execute("INSERT INTO lieferant (name, angelegt_am) VALUES"
                          " ('Beispiel GmbH', '2026-01-01')").lastrowid
        alt.execute("INSERT INTO beleg (id, lieferant_id, belegdatum, waehrung,"
                    " betrag_brutto_cent, betrag_betrieblich_cent, erfasst_am)"
                    " VALUES (7, ?, '2026-03-03', 'EUR', 1190, 1190, 'x')", (lid,))
        alt.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                    " VALUES (7, '2026-03-05', -1190, 'KARTE')")
        alt.execute("DROP TABLE belegordner_jahr")
        alt.execute("ALTER TABLE betrieb DROP COLUMN belege_bezahlt_vorgabe")
        alt.execute("DELETE FROM schema_version")
        alt.execute("INSERT INTO schema_version (version, migriert_am) VALUES (20, ?)",
                    (db.jetzt_utc(),))
        alt.commit()
        alt.close()

        migriert = db.oeffne(pfad)
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        try:
            zeile = migriert.execute(
                "SELECT belegordner_pfad, belegordner_automatisch, belege_bezahlt_vorgabe"
                " FROM betrieb").fetchone()
            assert tuple(zeile) == ("/Belege/Eingang", 0, 1)
            assert [tuple(z) for z in migriert.execute(
                "SELECT beleg_id, datum, betrag_cent, art FROM zahlung")] == [
                (7, "2026-03-05", -1190, "KARTE")]
            assert anzahl(migriert, "SELECT COUNT(*) FROM beleg") == 1
            # Die Tabelle ist da, leer -- der alte Ordner wird nicht umgedeutet.
            assert anzahl(migriert, "SELECT COUNT(*) FROM belegordner_jahr") == 0
            assert anzahl(migriert, "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                                    " AND objekt_id = 24") == 1
            assert anzahl(migriert, "SELECT MAX(version) FROM schema_version") == \
                db.SCHEMA_VERSION

            def spalten(verbindung, tabelle):
                return {z["name"] for z in verbindung.execute(f"PRAGMA table_info({tabelle})")}

            for tabelle in ("betrieb", "belegordner_jahr"):
                assert spalten(migriert, tabelle) == spalten(frisch, tabelle), tabelle
            # Die CHECKs kamen mit.
            with pytest.raises(sqlite3.IntegrityError):
                migriert.execute("UPDATE betrieb SET belege_bezahlt_vorgabe = 2")
            with pytest.raises(sqlite3.IntegrityError):
                migriert.execute("INSERT INTO belegordner_jahr VALUES (1999, '/x', 'x')")
            # Und ein zweiter Start aendert nichts mehr.
            migriert.close()
            migriert = db.oeffne(pfad)
            assert anzahl(migriert, "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                                    " AND objekt_id = 24") == 1
        finally:
            migriert.close()
            frisch.close()

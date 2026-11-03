"""Ein Datenbestand im Stand von Schema 25 -- der Stand vor dem WISO-Import.

Gebaut aus dem echten Schema von Version 25 (``fixtures/schema_v25.sql``,
das schema.sql aus Commit 28b08a3 mit den Paketen A bis F) mit reinem SQL,
ohne den heutigen Code: derselbe Bestand wie ``altbestand_v20`` (Kunden,
Zeiten, Rechnungen, Zahlungen, Belege in Euro und Dollar, Anlagegut mit AfA,
Steuerrücklage), dazu, was erst Version 25 kennt: ein Beleg mit
Löschmarkierung samt Grund. Erfundene Daten.

So, wie der Nutzer ihn vor Paket G hat: Migration 26 muss ihn ohne Verlust
übernehmen, und EÜR und § 19-Umsatz ergeben auf den Cent dasselbe wie im
Upgrade-Test ab Version 20 (``test_upgrade_v20.REFERENZ``) -- der gelöschte
Beleg zählt nicht.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests import altbestand_v20

V25 = (Path(__file__).parent / "fixtures" / "schema_v25.sql").read_text(encoding="utf-8")

JAHR = altbestand_v20.JAHR

#: Der Beleg mit Loeschmarkierung (Version 25): bezahlt, dann geloescht.
GELOESCHT_ID = 4
GELOESCHT_CENT = 5000


def baue(pfad: Path, belegordner: Path) -> None:
    """Legt die Datenbank im Stand 25 unter ``pfad`` an."""
    altbestand_v20.baue(pfad, belegordner, schema=V25, version=25)
    conn = sqlite3.connect(pfad, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        hosting = conn.execute("SELECT id FROM euer_kategorie WHERE schluessel = 'HOSTING'"
                               ).fetchone()["id"]
        conn.execute(
            "INSERT INTO beleg (id, lieferant_id, kategorie_id, belegdatum, betrag_brutto_cent,"
            " netto_fuer_grenzwert_cent, betrag_betrieblich_cent, geprueft_am, erfasst_am)"
            " VALUES (?, 1, ?, '2026-01-25', ?, ?, ?, '2026-01-26T09:00:00+00:00',"
            " '2026-01-26T09:00:00+00:00')",
            (GELOESCHT_ID, hosting, GELOESCHT_CENT, GELOESCHT_CENT, GELOESCHT_CENT))
        conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                     " VALUES (?, '2026-01-25', ?, 'KARTE')", (GELOESCHT_ID, -GELOESCHT_CENT))
        conn.execute("UPDATE beleg SET geloescht_am = '2026-02-01T09:00:00+00:00',"
                     " geloescht_grund = 'doppelt erfasst' WHERE id = ?", (GELOESCHT_ID,))
        conn.execute("COMMIT")
    finally:
        conn.close()

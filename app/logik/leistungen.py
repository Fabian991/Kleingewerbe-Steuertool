"""Der Leistungskatalog.

Er sieht aus wie ein Artikelstamm, hat aber einen anderen Zweck: er haelt die
**Titel konsistent**. Ohne das ist die Gruppierung "pro Titel" wertlos, denn aus
*Beratung*, *beratung* und *Consulting* werden drei Rechnungspositionen fuer
dieselbe Arbeit.

Deshalb wird er nicht nur gepflegt, sondern **mitgeschrieben**: wer in der
Zeiterfassung einen neuen Titel tippt, nimmt ihn damit auf. Ein Katalog, den man
getrennt pflegen muss, bleibt leer -- und ein leerer Katalog macht die
Autovervollstaendigung nutzlos, die ihn erst noetig macht.
"""

from __future__ import annotations

import sqlite3

#: UN/ECE Rec 20, wie das Datenmodell sie fuehrt (ZUGFeRD-faehig).
EINHEITEN = ("HUR", "DAY", "C62", "LS")


def finde(conn: sqlite3.Connection, titel: str) -> sqlite3.Row | None:
    """Sucht einen Titel ohne Ruecksicht auf Gross- und Kleinschreibung."""
    name = (titel or "").strip()
    if not name:
        return None
    return conn.execute(
        "SELECT * FROM leistung WHERE titel = ? COLLATE NOCASE", (name,)
    ).fetchone()


def merke(conn: sqlite3.Connection, titel: str) -> int | None:
    """Nimmt einen neuen Titel auf. Gibt die neue id zurueck, sonst ``None``.

    Muss innerhalb der Transaktion des Aufrufers laufen: entsteht der
    Zeiteintrag nicht, soll auch der Katalogeintrag nicht entstehen.

    Ein bereits bekannter Titel wird NICHT angefasst -- auch nicht in seiner
    Schreibweise. Wer den Katalog pflegt, bestimmt die Schreibweise; eine
    fluechtige Eingabe in der Zeiterfassung soll sie nicht ueberschreiben.
    """
    name = (titel or "").strip()
    if not name or finde(conn, name) is not None:
        return None
    return conn.execute("INSERT INTO leistung (titel) VALUES (?)", (name,)).lastrowid


def katalog(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Alle Leistungen mit der Zahl der Zeiteintraege, die sie benutzen.

    Die Zahl ist der einzige belastbare Hinweis darauf, welche Titel gelebt sind
    und welche man stilllegen kann.
    """
    return conn.execute(
        """
        SELECT l.*,
               (SELECT COUNT(*) FROM zeiteintrag z
                 WHERE z.titel = l.titel COLLATE NOCASE) AS verwendet
          FROM leistung l
         ORDER BY l.aktiv DESC, l.titel COLLATE NOCASE
        """
    ).fetchall()


def unbekannte_titel(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Titel aus der Zeiterfassung, die der Katalog nicht kennt.

    Altbestand: bis zur Einfuehrung des Mitschreibens hat nichts in den Katalog
    geschrieben. Diese Liste ist die Nachhilfe dafuer -- und zeigt spaeter
    Schreibvarianten, die per Hand ausserhalb des Katalogs entstanden sind.
    """
    return conn.execute(
        """
        SELECT z.titel, COUNT(*) AS verwendet, MAX(z.datum) AS letzter
          FROM zeiteintrag z
         WHERE NOT EXISTS (SELECT 1 FROM leistung l
                            WHERE l.titel = z.titel COLLATE NOCASE)
         GROUP BY z.titel COLLATE NOCASE
         ORDER BY COUNT(*) DESC, z.titel COLLATE NOCASE
        """
    ).fetchall()

"""Die Zahlung eines Belegs korrigieren: Zahltag aendern, Zahlung zuruecknehmen.

Warum es das braucht
--------------------
Fuer die EUeR zaehlt nach § 11 Abs. 2 EStG der Tag des Abflusses. Steht der
Zahltag falsch -- ueber den Jahreswechsel vertippt, oder der Beleg war doch
noch nicht bezahlt --, steht die Ausgabe im falschen EUeR-Jahr, und bisher
liess sie sich nur ergaenzen, nie berichtigen.

Wo die Zahlung steht
--------------------
In ``zahlung`` mit gesetzter ``beleg_id``, NEGATIV (eine Ausgabe). Die EUeR
(``euer.ausgaben``) summiert ``ABS(betrag_cent)`` je Beleg -- eine
Gegenbuchung mit umgekehrtem Vorzeichen zaehlte dort als WEITERE Ausgabe.
Deshalb wird die Zeile umdatiert bzw. entfernt, nicht gegengebucht. Der
urspruengliche Inhalt bleibt im Journal feststellbar (§ 146 Abs. 4 AO):
alter und neuer Tag (``AENDERN``), die ganze alte Zeile beim Zuruecknehmen
(``ZAHLUNG_ZURUECK``). Rechnungszahlungen sind ein anderer Bereich und
bleiben unberuehrt -- jede Anweisung hier traegt ``beleg_id = ?``.

Die Regeln
----------
* Nur bei GENAU EINER Zahlung. Bei Raten ist nicht klar, welche gemeint ist
  (dieselbe Grenze wie beim Angleichen des Euro-Betrags in umrechnung.py).
* Fremdwaehrung: der Euro-Betrag folgt dem Stichtag (umrechnung.py,
  Entscheidungen 1 und 3). Neuer Zahltag -- Kurs des neuen Tags, fuer Beleg
  UND Zahlung. Zahlung zurueckgenommen -- wieder der Kurs des Belegdatums,
  wie bei einem Beleg, der noch nicht bezahlt ist. Ausgenommen sind wie dort
  der Kartenbetrag, ein Kurs von Hand und ein Anlagegut; eine Teilzahlung
  bleibt, wie sie ist.
* Liegt der alte oder der neue Tag in einem Vorjahr, sagt das Ergebnis es
  (``vorjahre``): eine abgegebene EUeR aendert sich dann.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app import db
from app.logik import kurse
from app.logik.beleg import umrechnung
from app.logik.beleg.ergebnis import BelegFehler


@dataclass(frozen=True, slots=True)
class Korrektur:
    beleg_id: int
    zahlung_id: int
    alt_datum: str
    #: Neuer Zahltag; ``None``, wenn die Zahlung zurueckgenommen ist.
    neu_datum: str | None
    #: Nichts geschrieben (derselbe Tag noch einmal, Doppelklick).
    unveraendert: bool = False
    #: Bei Fremdwaehrung: der Euro-Betrag vorher und nachher, und der Kurs.
    euro_alt_cent: int | None = None
    euro_neu_cent: int | None = None
    kurs: kurse.Kurs | None = None
    #: Warum nicht umgerechnet wurde (Anlagegut, Kurs von Hand, Teilzahlung).
    hinweis: str | None = None
    #: Jahre VOR dem laufenden, die sich aendern.
    vorjahre: tuple[int, ...] = ()


def einzige_zahlung(conn: sqlite3.Connection, beleg: sqlite3.Row) -> sqlite3.Row:
    """Die eine Zahlung des Belegs -- sonst eine Meldung."""
    zahlungen = conn.execute("SELECT * FROM zahlung WHERE beleg_id = ? ORDER BY datum, id",
                             (beleg["id"],)).fetchall()
    if not zahlungen:
        raise BelegFehler(f"Zu Beleg {beleg['id']} ist keine Zahlung erfasst.")
    if len(zahlungen) > 1:
        raise BelegFehler(
            f"Zu Beleg {beleg['id']} gibt es {len(zahlungen)} Zahlungen — ändern oder "
            "zurücknehmen lässt sich nur eine einzelne. Bitte von Hand prüfen.")
    return zahlungen[0]


def _gueltiger_beleg(conn: sqlite3.Connection, beleg_id: int) -> sqlite3.Row:
    beleg = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()
    if beleg is None:
        raise BelegFehler("Diesen Beleg gibt es nicht.")
    if beleg["geloescht_am"] is not None:
        raise BelegFehler(f"Beleg {beleg_id} ist gelöscht — seine Zahlung bleibt, wie sie "
                          "war. Erst den Beleg wiederherstellen.")
    return beleg


def _voll(zahlung: sqlite3.Row, beleg: sqlite3.Row) -> bool:
    return abs(int(zahlung["betrag_cent"])) == abs(int(beleg["betrag_brutto_cent"]))


def _vorjahre(laufend: int, *tage: str | None) -> tuple[int, ...]:
    """>>> _vorjahre(2026, "2025-12-31", "2026-01-02", None)
    (2025,)"""
    return tuple(sorted({int(t[:4]) for t in tage if t and int(t[:4]) < laufend}))


def _unter_sperre(conn: sqlite3.Connection, beleg: sqlite3.Row,
                  zahlung: sqlite3.Row) -> sqlite3.Row:
    """Beleg und Zahlung unter der Sperre erneut lesen: hat sie inzwischen
    jemand geaendert (zweites Fenster, Doppelklick), wird abgebrochen --
    nicht mit dem veralteten Stand weitergerechnet."""
    frisch = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg["id"],)).fetchone()
    jetzt = conn.execute("SELECT * FROM zahlung WHERE beleg_id = ? ORDER BY datum, id",
                         (beleg["id"],)).fetchall()
    if (frisch is None or frisch["version"] != beleg["version"]
            or frisch["geloescht_am"] is not None
            or [tuple(z) for z in jetzt] != [tuple(zahlung)]):
        raise BelegFehler(f"Beleg {beleg['id']} wurde gerade geändert — bitte noch einmal.")
    return frisch


def zahltag_aendern(conn: sqlite3.Connection, datenordner: Path, beleg_id: int,
                    neuer_tag: str, *, laufend: int | None = None) -> Korrektur:
    """Datiert die (einzige) Zahlung des Belegs um. ``neuer_tag`` ist ein
    gelesenes ISO-Datum (die Route prueft "nicht in der Zukunft").

    Den Kurs besorgen, BEVOR gesperrt wird: dafuer kann die Kurstabelle
    geladen werden muessen. Wirft ``kurse.KeinKurs``, wenn der Kurs des
    neuen Tags fehlt.
    """
    laufend = laufend or int(db.heute()[:4])
    beleg = _gueltiger_beleg(conn, beleg_id)
    zahlung = einzige_zahlung(conn, beleg)
    if zahlung["datum"] == neuer_tag:
        return Korrektur(beleg_id, zahlung["id"], zahlung["datum"], neuer_tag,
                         unveraendert=True)
    voll = _voll(zahlung, beleg)
    neu = (umrechnung.fuer_stichtag(conn, datenordner, beleg, date.fromisoformat(neuer_tag))
           if voll else umrechnung.Zahlungstag())
    hinweis = neu.hinweis
    if not voll and beleg["waehrung"] != "EUR" and beleg["betrag_karte_cent"] is None:
        hinweis = ("Die Zahlung ist eine Teilzahlung; der Euro-Betrag bleibt beim bisherigen "
                   "Kurs.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        frisch = _unter_sperre(conn, beleg, zahlung)
        conn.execute("UPDATE zahlung SET datum = ? WHERE id = ? AND beleg_id = ?",
                     (neuer_tag, zahlung["id"], beleg_id))
        db.protokolliere(conn, "AENDERN", "zahlung", zahlung["id"], feld="datum",
                         wert_alt=zahlung["datum"], wert_neu=neuer_tag,
                         begruendung=f"Zahltag von Beleg {beleg_id} korrigiert")
        teilzahlung = umrechnung.umdatiert_uebernehmen(conn, frisch, neu)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    nachher = conn.execute("SELECT betrag_brutto_cent FROM beleg WHERE id = ?",
                           (beleg_id,)).fetchone()["betrag_brutto_cent"]
    return Korrektur(
        beleg_id, zahlung["id"], zahlung["datum"], neuer_tag,
        euro_alt_cent=int(beleg["betrag_brutto_cent"]) if neu.kurs else None,
        euro_neu_cent=int(nachher) if neu.kurs else None, kurs=neu.kurs,
        hinweis=teilzahlung or hinweis,
        vorjahre=_vorjahre(laufend, zahlung["datum"], neuer_tag))


def zahlung_zuruecknehmen(conn: sqlite3.Connection, datenordner: Path, beleg_id: int, *,
                          laufend: int | None = None, grund: str | None = None) -> Korrektur:
    """Nimmt die (einzige) Zahlung des Belegs zurueck: der Beleg ist danach
    offen und zaehlt in keinem EUeR-Jahr, bis die Zahlung neu erfasst ist.
    ``grund`` (freiwillig) steht mit in der Begruendung des Journaleintrags.

    Bei Fremdwaehrung ohne Kartenbetrag kehrt der Euro-Betrag zum Kurs des
    Belegdatums zurueck -- wie bei einem Beleg, der noch nicht bezahlt ist
    (Entscheidung 1 in umrechnung.py). Sonst saehe die Bezahlt-Route beim
    neuen Erfassen am Belegdatum keinen Grund umzurechnen und behielte den
    Kurs des alten Zahltags. Wirft ``kurse.KeinKurs``, wenn der Kurs des
    Belegdatums fehlt; dann bleibt alles, wie es ist.
    """
    laufend = laufend or int(db.heute()[:4])
    beleg = _gueltiger_beleg(conn, beleg_id)
    zahlung = einzige_zahlung(conn, beleg)
    neu = (umrechnung.fuer_stichtag(conn, datenordner, beleg,
                                    date.fromisoformat(beleg["belegdatum"]))
           if _voll(zahlung, beleg) else umrechnung.Zahlungstag())

    conn.execute("BEGIN IMMEDIATE")
    try:
        frisch = _unter_sperre(conn, beleg, zahlung)
        conn.execute("DELETE FROM zahlung WHERE id = ? AND beleg_id = ?",
                     (zahlung["id"], beleg_id))
        begruendung = (f"Zahlung zu Beleg {beleg_id} zurückgenommen — der Beleg ist wieder "
                       "offen")
        grund = " ".join((grund or "").split())
        if grund:
            begruendung += f": {grund}"
        db.protokolliere(conn, "ZAHLUNG_ZURUECK", "zahlung", zahlung["id"],
                         wert_alt=dict(zahlung), begruendung=begruendung)
        umrechnung.zahlungstag_uebernehmen(conn, frisch, neu)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    nachher = conn.execute("SELECT betrag_brutto_cent FROM beleg WHERE id = ?",
                           (beleg_id,)).fetchone()["betrag_brutto_cent"]
    return Korrektur(
        beleg_id, zahlung["id"], zahlung["datum"], None,
        euro_alt_cent=int(beleg["betrag_brutto_cent"]) if neu.kurs else None,
        euro_neu_cent=int(nachher) if neu.kurs else None, kurs=neu.kurs,
        hinweis=neu.hinweis, vorjahre=_vorjahre(laufend, zahlung["datum"]))

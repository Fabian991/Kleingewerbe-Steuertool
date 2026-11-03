"""Bezahlt als Vorgabe: Eingangsbelege sind in der Regel schon bezahlt.

Fuer die EUeR zaehlt nach § 11 Abs. 2 EStG der Tag, an dem das Geld abgeflossen
ist -- ein Beleg ohne Zahlung steht in keinem Jahr, und das faellt nirgends auf.
Wer seine Rechnungen sofort bezahlt (Karte, PayPal, Lastschrift beim Kauf),
muesste sonst nach jedem Buchen noch einmal "Bezahlt am" eintragen; vergessen
hiesse, die Ausgabe fehlt still.

Deshalb die Betriebseinstellung ``belege_bezahlt_vorgabe`` (Vorgabe: an). Ist
sie an, entsteht die Zahlung mit der Buchung, in derselben Transaktion
(``pipeline.buche``), mit Zahltag = Belegdatum:

* In der Pruefmaske ist "bezahlt am <Belegdatum>" vorausgewaehlt und aenderbar;
  "offen" muss man ausdruecklich waehlen.
* "Alle prüfen und buchen" und das Abholen aus dem Eingangsordner buchen mit
  derselben Annahme -- ausser am Jahreswechsel (:func:`am_jahreswechsel`): dort
  entscheidet der Zahltag das Jahr, und eine Rechnung vom 28.12., abgebucht im
  Januar, gehoerte ins neue. Solche Belege warten auf die Pruefmaske.
* Ein Belegdatum in einem VORJAHR (:func:`vorjahr`): die Zahlung aenderte die
  EUeR dieses Jahres, die womoeglich schon abgegeben ist. Das Abholen aus dem
  Eingangsordner laeuft unbeaufsichtigt -- dort wartet der Beleg auf die
  Pruefmaske. "Alle prüfen und buchen" ist ein bewusster Klick nach einer
  Vorschau (der Normalfall bei der Nacherfassung eines Jahres): dort bucht
  er, und Vorschau wie Ergebnis sagen es vorher bzw. danach deutlich.

Die Annahme steht im Journal (Begruendung des Eintrags ZAHLUNG) und in
``extraktion.bestaetigt_json`` ("bezahlt_am"). Ein falscher Tag wirkt nur, wenn
er die Jahresgrenze kreuzt -- und bei Fremdwaehrung ueber den Kurs; beides
laesst sich spaeter in der Liste korrigieren.
"""

from __future__ import annotations

import sqlite3
from datetime import date

#: Die Zahlungsarten der Datenbank (CHECK in schema.sql, zahlung.art).
ZAHLUNGSARTEN = ("UEBERWEISUNG", "KARTE", "BAR", "PAYPAL", "SONSTIGES")

#: Die Zahlungsart, wenn nichts bekannt ist. "Überweisung" behauptete meist
#: etwas Falsches; auf die EUeR wirkt die Art nicht.
NEUTRAL = "SONSTIGES"

#: Die Begruendung am Journaleintrag ZAHLUNG, wenn die Vorgabe den Zahltag
#: gesetzt hat und niemand ihn gesehen hat (automatisch gebucht).
BEGRUENDUNG_AUTOMATISCH = ("Vorgabe „Eingangsbelege sind in der Regel schon bezahlt“: "
                           "Zahltag = Belegdatum, automatisch gebucht")

#: Dasselbe, wenn der Nutzer den vorbelegten Zahltag in der Pruefmaske
#: bestaetigt hat.
BEGRUENDUNG_BESTAETIGT = ("Vorgabe „Eingangsbelege sind in der Regel schon bezahlt“: "
                          "Zahltag = Belegdatum, in der Prüfmaske bestätigt")

#: Der Hinweis am Jahreswechsel -- Pruefmaske und Grund fuer das Warten.
JAHRESWECHSEL = ("Jahreswechsel: Für die EÜR zählt der Tag der Zahlung (§ 11 EStG). Bei "
                 "Lastschrift oder Überweisung im neuen Jahr bitte den echten Zahltag "
                 "eintragen.")

#: Der Grund, wenn ein automatischer Weg deshalb nicht bucht.
JAHRESWECHSEL_GRUND = ("Belegdatum am Jahreswechsel — für die EÜR zählt der Tag der Zahlung, "
                       "bitte den Zahltag in der Prüfmaske bestätigen")


def vorjahr_grund(jahr: int) -> str:
    """Der Grund, wenn das Abholen einen Beleg aus einem Vorjahr nicht bucht.

    >>> vorjahr_grund(2025)[:47]
    'Belegdatum im Vorjahr — die Zahlung zählte in d'
    """
    return (f"Belegdatum im Vorjahr — die Zahlung zählte in der EÜR {jahr}, die evtl. schon "
            "abgegeben ist; bitte den Zahltag in der Prüfmaske bestätigen")


def vorjahr_warnung(jahre: list[int] | tuple[int, ...] | set[int], anzahl: int) -> str:
    """Die Warnung in Vorschau und Ergebnis von "Alle prüfen und buchen".

    >>> vorjahr_warnung([2025], 1)[:62]
    'Achtung: 1 Zahlung zählt in der EÜR 2025 — ist sie schon abgeg'
    >>> vorjahr_warnung([2025, 2024], 3)[:56]
    'Achtung: 3 Zahlungen zählen in den EÜR 2024 und 2025 — i'
    """
    reihe = sorted(set(jahre))
    zahl = "1 Zahlung zählt" if anzahl == 1 else f"{anzahl} Zahlungen zählen"
    if len(reihe) == 1:
        wo, sie, aendert = f"der EÜR {reihe[0]}", "sie", "ändert sie sich"
    else:
        liste = ", ".join(str(j) for j in reihe[:-1]) + f" und {reihe[-1]}"
        wo, sie, aendert = f"den EÜR {liste}", "eine davon", "ändert sie sich"
    return (f"Achtung: {zahl} in {wo} — ist {sie} schon abgegeben, {aendert} mit und "
            "müsste berichtigt werden.")


def vorjahr(zahltag: str | date | None, heute: date) -> int | None:
    """Das Jahr des Zahltags, wenn es vor dem laufenden liegt -- sonst None.

    >>> vorjahr("2025-06-14", date(2026, 10, 24)), vorjahr("2026-01-02", date(2026, 10, 24))
    (2025, None)
    >>> vorjahr(None, date(2026, 10, 24)), vorjahr("kaputt", date(2026, 10, 24))
    (None, None)
    """
    if not zahltag:
        return None
    try:
        tag = zahltag if isinstance(zahltag, date) else date.fromisoformat(str(zahltag).strip())
    except ValueError:
        return None
    return tag.year if tag.year < heute.year else None


def vorgabe(conn: sqlite3.Connection) -> bool:
    """Ob Eingangsbelege als bezahlt vorbelegt werden (Vorgabe: ja).

    Ohne Betrieb -- oder in einer Datenbank, die die Spalte noch nicht kennt
    -- gilt die Vorgabe der frischen Datenbank.
    """
    try:
        zeile = conn.execute(
            "SELECT belege_bezahlt_vorgabe FROM betrieb WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return True
    return True if zeile is None else bool(zeile["belege_bezahlt_vorgabe"])


def am_jahreswechsel(belegdatum: str | date | None) -> bool:
    """Liegt das Belegdatum zwischen dem 22.12. und dem 10.01.?

    Dann ist "Zahltag = Belegdatum" am ehesten falsch: eine Rechnung vom
    28.12. wird oft erst im Januar abgebucht, eine Vorkasse im Dezember hat
    eine Rechnung vom Januar. Die zehn Tage sind die des § 11 Abs. 2 Satz 2
    EStG fuer regelmaessig wiederkehrende Ausgaben.

    >>> am_jahreswechsel("2026-12-22"), am_jahreswechsel("2027-01-10")
    (True, True)
    >>> am_jahreswechsel("2026-12-21"), am_jahreswechsel("2027-01-11"), am_jahreswechsel("")
    (False, False, False)
    """
    if not belegdatum:
        return False
    try:
        tag = belegdatum if isinstance(belegdatum, date) else date.fromisoformat(
            str(belegdatum).strip())
    except ValueError:
        return False
    return (tag.month == 12 and tag.day >= 22) or (tag.month == 1 and tag.day <= 10)


def zahlungsart(conn: sqlite3.Connection, lieferant_id: int | None) -> str:
    """Die Zahlungsart fuer eine vorbelegte Zahlung.

    Die der letzten Zahlung an denselben Lieferanten -- wer bei Hetzner per
    Lastschrift zahlt, zahlt dort meist immer so --, sonst die neutrale Art.
    Nur gueltige Belege: ein geloeschter (Testbeleg, falsch ausgelesen) gibt
    keine Vorgabe mehr (v_beleg_gueltig, Version 25).
    """
    if lieferant_id:
        zeile = conn.execute(
            "SELECT z.art FROM zahlung z JOIN v_beleg_gueltig b ON b.id = z.beleg_id"
            " WHERE b.lieferant_id = ? ORDER BY b.belegdatum DESC, b.id DESC, z.id DESC"
            " LIMIT 1", (lieferant_id,),
        ).fetchone()
        if zeile is not None and zeile["art"] in ZAHLUNGSARTEN:
            return zeile["art"]
    return NEUTRAL

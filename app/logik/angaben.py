"""„Angaben fehlen“: was ein Import nicht weiss und der Nutzer nachtragen muss.

Der Wunsch: "Wenn Informationen fehlen, erwähne das im Tool und stelle die
Felder zur Verfügung, die noch fehlen, damit ich sie ausfülle." Eine Liste
(Tabelle ``fehlende_angabe``), die die Übersicht mit einer Zeile zählt und
die jeweilige Seite als Formular mit genau den fehlenden Feldern zeigt:

* **ANLAGEGUT** -- ein Gut, von dem der WISO-Export nur die Jahres-AfA
  zeigt (angeschafft in einem Vorjahr). Es fehlen Anschaffungstag,
  Anschaffungskosten und Nutzungsdauer. Solange sie fehlen, rechnet das
  Werkzeug keine AfA; EUeR und Steuerruecklage sagen das mit dem WISO-Wert
  ("AfA für Kamera fehlt (WISO: 400 €)"). Danach rechnet das Werkzeug selbst
  und vergleicht mit dem WISO-Wert.
* **KONTO** -- ein Konto ohne Kategorie; seine Buchungen warten.
* **ZAHLER** -- ein Zahler, dessen Einstufung (Inland, Ausland ...) offen
  blieb; seine Einnahmen warten.

Fuer KONTO und ZAHLER wird nach der Angabe die abgelegte Datei des Imports
noch einmal gelesen, und genau deren wartende Buchungen entstehen
(:func:`app.logik.wiso.nachtragen`) -- in derselben Transaktion.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app import db
from app.logik import anlagen, eingabe, einnahmen, kategorien, wiso
from app.logik.rundung import euro


class AngabenFehler(ValueError):
    """Eine Angabe, die so nicht passt -- mit deutscher Meldung."""


ARTEN = {
    "ANLAGEGUT": "Anlagegut aus einem Vorjahr",
    "KONTO": "Konto ohne Kategorie",
    "ZAHLER": "Zahler ohne Einstufung",
}


@dataclass(frozen=True, slots=True)
class Angabe:
    id: int
    art: str
    schluessel: str
    titel: str
    angaben: dict
    import_id: int | None
    erfasst_am: str

    @property
    def afa(self) -> dict[int, int]:
        """WISO-AfA je Jahr (nur ANLAGEGUT)."""
        return {int(j): int(c) for j, c in (self.angaben.get("afa") or {}).items()}


def _angabe(zeile: sqlite3.Row) -> Angabe:
    try:
        angaben = json.loads(zeile["angaben_json"] or "{}")
    except ValueError:
        angaben = {}
    return Angabe(zeile["id"], zeile["art"], zeile["schluessel"], zeile["titel"],
                  angaben if isinstance(angaben, dict) else {}, zeile["import_id"],
                  zeile["erfasst_am"])


def offene(conn: sqlite3.Connection, art: str | None = None) -> list[Angabe]:
    """Die offenen Angaben, aelteste zuerst."""
    bedingung, werte = ("AND art = ?", (art,)) if art else ("", ())
    return [_angabe(z) for z in conn.execute(
        f"SELECT * FROM fehlende_angabe WHERE erledigt_am IS NULL {bedingung}"
        " ORDER BY art, id", werte)]


def anzahl_offen(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM fehlende_angabe WHERE erledigt_am IS NULL"
                            ).fetchone()[0])


def fehlende_afa(conn: sqlite3.Connection, jahr: int) -> list[tuple[str, int]]:
    """(Name, WISO-AfA) der Gueter, deren AfA im Jahr fehlt -- fuer EUeR und Steuerruecklage."""
    return [(a.titel, a.afa[jahr]) for a in offene(conn, "ANLAGEGUT") if jahr in a.afa]


def _offen(conn: sqlite3.Connection, angabe_id: int, art: str) -> Angabe:
    if not 1 <= angabe_id <= 2**63 - 1:
        raise AngabenFehler("Diese Angabe gibt es nicht.")
    zeile = conn.execute("SELECT * FROM fehlende_angabe WHERE id = ?", (angabe_id,)).fetchone()
    if zeile is None or zeile["art"] != art:
        raise AngabenFehler("Diese Angabe gibt es nicht.")
    if zeile["erledigt_am"] is not None:
        raise AngabenFehler(f"„{zeile['titel']}“ ist schon ergänzt.")
    return _angabe(zeile)


@dataclass(slots=True)
class Ergaenzt:
    """Was nach dem Ausfuellen geschah -- fuer die Meldung."""

    text: str
    hinweise: list[str] = field(default_factory=list)


def anlagegut_ergaenzen(conn: sqlite3.Connection, angabe_id: int, *, anschaffung_am: str,
                        ak_brutto_cent: int | None, nutzungsdauer_jahre: int | None,
                        art: str = "BEWEGLICH", heute: date | None = None) -> Ergaenzt:
    """Legt das Anlagegut mit den nachgetragenen Angaben an und vergleicht mit WISO.

    Ohne Nutzungsdauer wird sie aus der WISO-AfA abgeleitet (wie beim Import).
    Alles in EINER Transaktion: Gut, AfA-Reihe, Erledigt-Vermerk, Journal.
    """
    heute = heute or date.fromisoformat(db.heute())
    if not ak_brutto_cent or ak_brutto_cent <= 0:
        raise AngabenFehler("Bitte die Anschaffungskosten (brutto) angeben.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        angabe = _offen(conn, angabe_id, "ANLAGEGUT")
        try:
            tag = eingabe.datum(anschaffung_am, feld="Anschaffungsdatum", heute=heute)
        except ValueError as fehler:
            raise AngabenFehler(str(fehler)) from None
        dauer, methode = nutzungsdauer_jahre, anlagen.LINEAR
        abgeleitet = False
        if dauer is None:
            # Nur eine Dauer, die in JEDEM Jahr der WISO-AfA passt -- und nur,
            # wenn es genau eine ist. Mehrere: fragen statt raten.
            passend: set[tuple[int, str]] | None = None
            for jahr, cent in sorted(angabe.afa.items()):
                gefunden = set(anlagen.nutzungsdauer_kandidaten(ak_brutto_cent, tag, cent,
                                                                jahr=jahr))
                passend = gefunden if passend is None else passend & gefunden
            passend = sorted(passend or ())
            if len(passend) > 1:
                raise AngabenFehler(
                    "Zur WISO-AfA passen mit diesen Anschaffungskosten und diesem Tag mehrere "
                    "Nutzungsdauern: "
                    + " oder ".join(f"{d} {'Jahr' if d == 1 else 'Jahre'}" for d, _ in passend)
                    + " — bitte die Nutzungsdauer angeben (sie steht im Anlagenverzeichnis "
                    "von WISO).")
            if not passend:
                raise AngabenFehler(
                    "Aus der WISO-AfA lässt sich mit diesen Anschaffungskosten und diesem "
                    "Tag keine Nutzungsdauer ableiten — bitte die Nutzungsdauer angeben.")
            (dauer, methode), abgeleitet = passend[0], True
        herkunft = "aus der WISO-AfA abgeleitet" if abgeleitet else "eingetragen"
        notiz = (f"Aus WISO (Anl.-Nr. {angabe.angaben.get('anl_nr', '?')}); Angaben "
                 f"nachgetragen, Nutzungsdauer {herkunft}")
        try:
            gut_id = anlagen.anlegen(
                conn, bezeichnung=angabe.titel, anschaffung_am=tag,
                ak_brutto_cent=ak_brutto_cent, nutzungsdauer_jahre=dauer,
                art=art if art in ("BEWEGLICH", "IMMATERIELL") else "BEWEGLICH",
                ist_it=methode == anlagen.EIN_JAHR_IT, notiz=notiz, transaktion=False,
                heute=heute)
        except anlagen.AnlagenFehler as fehler:
            raise AngabenFehler(str(fehler)) from None
        wiso.angabe_erledigt(conn, angabe.id, "Anlagegut angelegt", anlagegut_id=gut_id)
        vergleich = []
        for jahr, wiso_cent in sorted(angabe.afa.items()):
            zeile = conn.execute(
                "SELECT j.afa_cent, g.aufteilungsquote FROM anlagegut_afa_jahr j"
                " JOIN v_anlagegut_gueltig g ON g.id = j.anlagegut_id"
                " WHERE j.anlagegut_id = ? AND j.jahr = ?", (gut_id, jahr)).fetchone()
            tool = 0 if zeile is None else (int(zeile["afa_cent"])
                                            * int(zeile["aufteilungsquote"]) + 50) // 100
            vergleich.append((jahr, wiso_cent, tool))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    hinweise = []
    for jahr, wiso_cent, tool in vergleich:
        if wiso_cent != tool:
            hinweise.append(f"AfA {jahr}: das Tool rechnet {euro(tool)} €, WISO "
                            f"{euro(wiso_cent)} € — Anschaffungstag, -kosten und "
                            "Nutzungsdauer bitte prüfen.")
    gleich = [j for j, w, t in vergleich if w == t]
    text = (f"„{angabe.titel}“ ist im Anlagenverzeichnis: {euro(ak_brutto_cent)} €, "
            f"{dauer} {'Jahr' if dauer == 1 else 'Jahre'}"
            + (" (aus der WISO-AfA abgeleitet)" if abgeleitet else "") + ".")
    if gleich:
        text += f" AfA {', '.join(map(str, gleich))} wie in WISO."
    vorjahre = sorted({int(z["jahr"]) for z in conn.execute(
        "SELECT jahr FROM anlagegut_afa_jahr WHERE anlagegut_id = ? AND jahr < ?",
        (gut_id, min(angabe.afa) if angabe.afa else heute.year))})
    if vorjahre:
        text += (f" Die AfA {', '.join(map(str, vorjahre))} steht im Tool nur zur Übersicht — "
                 "diese Jahre sind in WISO erklärt.")
    return Ergaenzt(text, hinweise)


def konto_zuordnen(conn: sqlite3.Connection, datenordner: Path, angabe_id: int,
                   kategorie_id: int | None, *, heute: date | None = None) -> Ergaenzt:
    """Merkt die Kategorie eines Kontos und legt dessen wartende Buchungen an."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        angabe = _offen(conn, angabe_id, "KONTO")
        kategorie = (conn.execute("SELECT * FROM euer_kategorie WHERE id = ?",
                                  (kategorie_id,)).fetchone() if kategorie_id else None)
        if kategorie is None or kategorie["bereich"] != kategorien.BEREICH_AUSGABEN \
                or not kategorie["aktiv"]:
            raise AngabenFehler("Bitte eine Ausgabenkategorie wählen.")
        alt = conn.execute("SELECT kategorie_id FROM wiso_konto WHERE konto = ?",
                           (angabe.schluessel,)).fetchone()
        conn.execute(
            "INSERT INTO wiso_konto (konto, bezeichnung, kategorie_id, geaendert_am)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (konto) DO UPDATE SET"
            " kategorie_id = excluded.kategorie_id, geaendert_am = excluded.geaendert_am",
            (angabe.schluessel, str(angabe.angaben.get("bezeichnung") or "")[:120],
             kategorie["id"], db.jetzt_utc()))
        db.protokolliere(conn, "WISO_KONTO", "wiso_konto", None, feld=angabe.schluessel,
                         wert_alt=alt["kategorie_id"] if alt is not None else None,
                         wert_neu=kategorie["id"])
        ergebnis = _nachtragen(conn, datenordner, angabe, konto=angabe.schluessel, heute=heute)
        wiso.angabe_erledigt(conn, angabe.id, f"Kategorie {kategorie['bezeichnung']}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return Ergaenzt(f"Konto {angabe.schluessel} gehört jetzt zu „{kategorie['bezeichnung']}“ "
                    f"(gemerkt). {ergebnis.zusammenfassung()}", ergebnis.hinweise)


def zahler_einstufen(conn: sqlite3.Connection, datenordner: Path, angabe_id: int,
                     einstufung: str, *, heute: date | None = None) -> Ergaenzt:
    """Merkt die Einstufung eines Zahlers und legt seine wartenden Einnahmen an."""
    if einstufung not in einnahmen.EINSTUFUNGEN:
        raise AngabenFehler("Bitte Inland, Ausland, Anlagenverkauf oder Erstattung wählen.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        angabe = _offen(conn, angabe_id, "ZAHLER")
        kategorie = einnahmen.kategorie_der_einstufung(conn, einstufung)
        name = str(angabe.angaben.get("name") or angabe.titel)
        # Unter dem Schluessel der Angabe merken -- so findet der Import ihn wieder.
        conn.execute(
            "INSERT INTO zahler (schluessel, name, kategorie_id, geaendert_am) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (schluessel) DO UPDATE SET kategorie_id = excluded.kategorie_id,"
            " geaendert_am = excluded.geaendert_am",
            (angabe.schluessel, name[:120], kategorie["id"], db.jetzt_utc()))
        db.protokolliere(conn, "ZAHLER_EINSTUFUNG", "zahler", None, feld=angabe.schluessel,
                         wert_neu={"name": name, "kategorie_id": kategorie["id"]})
        ergebnis = _nachtragen(conn, datenordner, angabe, zahler=angabe.schluessel, heute=heute)
        wiso.angabe_erledigt(conn, angabe.id, einnahmen.EINSTUFUNG_TEXTE[einstufung])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return Ergaenzt(f"{name}: {einnahmen.EINSTUFUNG_TEXTE[einstufung]} (gemerkt). "
                    f"{ergebnis.zusammenfassung()}", ergebnis.hinweise)


def _nachtragen(conn: sqlite3.Connection, datenordner: Path, angabe: Angabe, *,
                konto: str | None = None, zahler: str | None = None,
                heute: date | None = None) -> wiso.Ergebnis:
    if angabe.import_id is None:
        return wiso.Ergebnis()
    try:
        return wiso.nachtragen(conn, datenordner, angabe.import_id, konto=konto, zahler=zahler,
                               heute=heute)
    except wiso.ImportFehler as fehler:
        raise AngabenFehler(str(fehler)) from None

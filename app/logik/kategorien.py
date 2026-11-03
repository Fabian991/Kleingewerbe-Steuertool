"""Der Kategoriekatalog und sein Weg in die Datenbank.

Der Katalog liegt als JSON im Projekt und nicht im Code, weil er sich aendert:
Kennzahlen kommen und gehen, Bezeichnungen werden umformuliert. Er liegt
gleichzeitig **nicht** nur in der Datenbank, weil er dann bei jedem neuen
Datenbestand von Hand nachgebaut werden muesste.

Eingelesen wird darum **abgleichend**: vorhandene Kategorien werden anhand des
stabilen ``schluessel`` aktualisiert, neue angelegt, und was der Nutzer selbst
angelegt hat, bleibt unangetastet. Was in der Datei fehlt, wird **nicht
geloescht** -- an einer Kategorie koennen Belege haengen, und ein Beleg ohne
Kategorie blockiert spaeter den Jahresabschluss.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

STAMMDATEN = Path(__file__).resolve().parent.parent / "stammdaten"
KATALOGDATEI = STAMMDATEN / "kategorien.json"

#: Bereiche der Anlage EUeR.
BEREICH_EINNAHMEN = 20
BEREICH_AUSGABEN = 25

#: Felder, die aus der Datei uebernommen werden, mit ihrer Vorbelegung.
FELDER: dict[str, object] = {
    "bezeichnung": None,
    "bereich": None,
    "kennzahl": None,
    "kennzahl_nicht_abziehbar": None,
    "anteil_abziehbar_promille": 1000,
    "quote_default": 100,
    "grenze_netto_cent": None,
    "hinweis": None,
    "sortierung": 0,
}


@dataclass(frozen=True, slots=True)
class Abgleich:
    angelegt: int = 0
    aktualisiert: int = 0
    unveraendert: int = 0
    nur_in_der_datenbank: tuple[str, ...] = ()

    @property
    def gesamt(self) -> int:
        return self.angelegt + self.aktualisiert + self.unveraendert


def lies_datei(pfad: Path | None = None) -> list[dict[str, object]]:
    """Liest den Katalog und prueft ihn, bevor er die Datenbank erreicht."""
    rohdaten = json.loads((pfad or KATALOGDATEI).read_text(encoding="utf-8"))
    kategorien = rohdaten.get("kategorien", [])
    gesehen: set[str] = set()
    for eintrag in kategorien:
        schluessel = eintrag.get("schluessel")
        if not schluessel:
            raise ValueError(f"Kategorie ohne Schluessel: {eintrag!r}")
        if schluessel in gesehen:
            raise ValueError(f"Schluessel doppelt: {schluessel}")
        gesehen.add(schluessel)
        if eintrag.get("bereich") not in (BEREICH_EINNAHMEN, BEREICH_AUSGABEN):
            raise ValueError(f"{schluessel}: bereich muss 20 oder 25 sein")
        if not isinstance(eintrag.get("kennzahl"), int):
            raise ValueError(f"{schluessel}: kennzahl fehlt oder ist keine Zahl")
        unbekannt = set(eintrag) - set(FELDER) - {"schluessel"}
        if unbekannt:
            # Ein Tippfehler im Feldnamen wuerde sonst still ignoriert, und die
            # Kategorie laege mit Vorbelegung statt mit dem gemeinten Wert da.
            raise ValueError(f"{schluessel}: unbekannte Felder {sorted(unbekannt)}")
    return kategorien


def abgleichen(
    conn: sqlite3.Connection, pfad: Path | None = None, *, quelle: str = "kategorien.json"
) -> Abgleich:
    """Bringt die Tabelle auf den Stand der Datei. Idempotent.

    Muss in einer Transaktion des Aufrufers laufen.
    """
    aus_datei = lies_datei(pfad)
    vorhanden = {
        z["schluessel"]: z for z in conn.execute("SELECT * FROM euer_kategorie")
    }

    angelegt = aktualisiert = unveraendert = 0
    for eintrag in aus_datei:
        schluessel = eintrag["schluessel"]
        werte = {feld: eintrag.get(feld, standard) for feld, standard in FELDER.items()}
        alt = vorhanden.get(schluessel)
        if alt is None:
            spalten = ", ".join(("schluessel", *werte))
            platz = ", ".join("?" * (len(werte) + 1))
            conn.execute(
                f"INSERT INTO euer_kategorie ({spalten}) VALUES ({platz})",
                (schluessel, *werte.values()),
            )
            angelegt += 1
            continue
        geaendert = {f: w for f, w in werte.items() if alt[f] != w}
        if not geaendert:
            unveraendert += 1
            continue
        satz = ", ".join(f"{f} = ?" for f in geaendert)
        conn.execute(
            f"UPDATE euer_kategorie SET {satz} WHERE schluessel = ?",
            (*geaendert.values(), schluessel),
        )
        aktualisiert += 1

    # Nicht loeschen, nur melden: an einer Kategorie koennen Belege haengen,
    # und ein Beleg ohne Kategorie blockiert spaeter den Jahresabschluss.
    fremd = tuple(sorted(set(vorhanden) - {e["schluessel"] for e in aus_datei}))
    return Abgleich(angelegt, aktualisiert, unveraendert, fremd)


def katalog(
    conn: sqlite3.Connection, *, bereich: int | None = None, nur_aktive: bool = True
) -> list[sqlite3.Row]:
    bedingungen = []
    werte: list[object] = []
    if bereich is not None:
        bedingungen.append("bereich = ?")
        werte.append(bereich)
    if nur_aktive:
        bedingungen.append("aktiv = 1")
    wo = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""
    return conn.execute(
        f"SELECT * FROM euer_kategorie {wo} ORDER BY bereich, sortierung, bezeichnung",
        werte,
    ).fetchall()


def nach_schluessel(conn: sqlite3.Connection, schluessel: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM euer_kategorie WHERE schluessel = ?", (schluessel,)
    ).fetchone()


def betrieblich_cent(brutto_cent: int, quote: int) -> int:
    """Der Teil des Belegs, der in die EUeR geht.

    Gerundet wird hier und nur hier -- deshalb steht das Ergebnis als eigene
    Spalte am Beleg und nicht als Ausdruck in jeder Auswertung. Sonst rundet
    jede Abfrage selbst, und die Summen weichen voneinander ab.

    >>> betrieblich_cent(10000, 70), betrieblich_cent(9999, 50)
    (7000, 5000)
    """
    if not 0 <= quote <= 100:
        raise ValueError(f"Quote ausserhalb 0 bis 100: {quote}")
    # Kaufmaennisch auf ganze Cent, ohne Float: (a*q + 50) // 100 rundet .5 auf.
    return (brutto_cent * quote + 50) // 100


def netto_fuer_grenzwert_cent(
    brutto_cent: int, netto_cent: int | None, ust_satz_promille: int | None = None
) -> int:
    """Der Nettowert, gegen den eine Wertgrenze geprueft wird.

    Die GWG-Wertgrenzen werden nach **R 9b Abs. 2 EStR stets netto** geprueft --
    "Ob der Vorsteuerbetrag umsatzsteuerrechtlich abziehbar ist, spielt in
    diesem Fall keine Rolle." 800 EUR netto sind also rund 952 EUR brutto.
    Gebucht wird dagegen der Bruttowert (R 9b Abs. 1), weil die nicht
    abziehbare Vorsteuer zu den Anschaffungskosten gehoert.

    Steht ein Netto auf dem Beleg, gilt es. Steht keines da, wird mit dem
    Steuersatz zurueckgerechnet. Gibt es auch den nicht -- Lieferant ist selbst
    Kleinunternehmer, Auslandsbeleg, Reverse Charge --, ist Netto = Brutto; eine
    Steuer, die nicht erhoben wurde, kann man nicht herausrechnen.

    >>> netto_fuer_grenzwert_cent(95200, None, 190)
    80000
    >>> netto_fuer_grenzwert_cent(80000, None, None)
    80000
    """
    if netto_cent is not None:
        return netto_cent
    if not ust_satz_promille:
        return brutto_cent
    # brutto / (1 + satz) kaufmaennisch, in Ganzzahlen gerechnet.
    teiler = 1000 + ust_satz_promille
    return (brutto_cent * 1000 + teiler // 2) // teiler

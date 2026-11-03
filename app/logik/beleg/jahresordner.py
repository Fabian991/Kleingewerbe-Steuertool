"""Ein Eingangsordner je Jahr: Vorschlag, Pruefungen und der Jahreswechsel.

Wer seine Belege ohnehin je Jahr in einem eigenen Ordner sammelt, hinterlegt
fuer jedes Jahr einen (Tabelle ``belegordner_jahr``). Abgeholt werden alle
hinterlegten Ordner (``abholen.abholen_alle``) -- auch die alter Jahre, fuer
Nachzuegler, bis der Nutzer einen entfernt. Daneben gibt es weiter den Ordner
fuer alle Jahre (``betrieb.belegordner_pfad``).

Was hier entschieden wird:

* Vorschlag fuer ein Jahr: der Pfad des naechstaelteren Jahresordners, mit
  dem neuen Jahr an der Stelle des alten -- "…/Belege 2026" wird
  "…/Belege 2027", "…/Buchhaltung/2026/Eingang" wird "…/2027/Eingang". Ohne
  Jahresordner auf dem Mac "iCloud Drive › Steuer-Tool Belege <Jahr>".
* Kein Ordner zweimal (auch nicht als Jahres- UND Allejahre-Ordner),
  Jahresordner nicht ineinander, und kein Ordner in "Übernommen" oder "Nicht
  übernommen" eines anderen -- sonst holte ein Lauf ab, was der andere gerade
  abgelegt hat.
* Liegen in einem Ordner schon Dateien, fragt die Seite nach: das Tool
  verschiebt JEDE Datei der obersten Ebene. Wer im Jahresordner auch
  Kontoauszuege oder Bescheide hat, legt besser einen Unterordner "Eingang" an.
* Ab dem 1. Dezember ein Hinweis auf den Ordner des naechsten Jahres;
  angelegt und hinterlegt wird nur per Klick (das Tool verschiebt jede Datei
  im Ordner, das braucht ein Ja).
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.logik import eingabe
from app.logik.beleg import abholen, pipeline

#: So heisst der empfohlene Unterordner, wenn im Jahresordner anderes liegt.
UNTERORDNER = "Eingang"

#: Das Fenster der Jahre (schema.sql: CHECK jahr BETWEEN 2000 AND 2100).
JAHR_HOECHSTENS = 2100


def jahre(conn: sqlite3.Connection) -> dict[int, str]:
    """Die hinterlegten Jahresordner: Jahr -> gespeicherter Pfad."""
    return {z["jahr"]: z["pfad"] for z in conn.execute(
        "SELECT jahr, pfad FROM belegordner_jahr ORDER BY jahr")}


def jahr_lesen(roh: str, *, heute: date | None = None) -> int:
    """Das Jahr aus dem Formular -- zwischen 2000 und dem naechsten Jahr.

    >>> jahr_lesen("2027", heute=date(2026, 12, 1))
    2027
    >>> jahr_lesen("2028", heute=date(2026, 12, 1))  # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ValueError: Jahr: 2028 ist zu groß (höchstens 2027). Ein Ordner lässt sich für die ...
    """
    heute = heute or date.today()
    try:
        wert = eingabe.ganzzahl(roh, feld="Jahr", mindestens=eingabe.JAHR_MINDESTENS,
                                hoechstens=min(heute.year + 1, JAHR_HOECHSTENS))
    except ValueError as fehler:
        raise ValueError(f"{fehler} Ein Ordner lässt sich für die Jahre "
                         f"{eingabe.JAHR_MINDESTENS} bis {heute.year + 1} hinterlegen.") from None
    return wert


def mit_jahr(pfad: Path | str, alt: int, neu: int) -> Path | None:
    """Der Pfad mit ``neu`` an jeder Stelle, an der ``alt`` als ganze Zahl
    steht -- ``None``, wenn das alte Jahr nirgends vorkommt.

    >>> mit_jahr("/Users/x/Belege 2026", 2026, 2027)
    PosixPath('/Users/x/Belege 2027')
    >>> mit_jahr("/Users/x/Buchhaltung/2026/Eingang", 2026, 2027)
    PosixPath('/Users/x/Buchhaltung/2027/Eingang')
    >>> mit_jahr("/Users/x/Belege-2026-Rechnungen", 2026, 2025)
    PosixPath('/Users/x/Belege-2025-Rechnungen')
    >>> mit_jahr("/Users/x/120260/Belege", 2026, 2027) is None
    True
    """
    muster = re.compile(rf"(?<!\d){alt}(?!\d)")
    teile = Path(pfad).parts
    neu_teile = [muster.sub(str(neu), teil) for teil in teile]
    if list(teile) == neu_teile:
        return None
    return Path(*neu_teile)


def vorschlag(conn: sqlite3.Connection, jahr: int) -> Path | None:
    """Der vorgeschlagene Ordner fuer ``jahr``.

    Aus dem Jahresordner, der dem Jahr am naechsten liegt (zuerst die
    aelteren), mit dem neuen Jahr im Pfad. Sonst auf dem Mac der Ordner
    "Steuer-Tool Belege <Jahr>" in iCloud Drive; anderswo keiner.
    """
    vorhanden = jahre(conn)
    for anderes in sorted(vorhanden, key=lambda j: (j > jahr, abs(j - jahr))):
        if anderes == jahr:
            continue
        abgeleitet = mit_jahr(vorhanden[anderes], anderes, jahr)
        if abgeleitet is not None:
            return abgeleitet
    basis = abholen.vorschlag_ordner()
    if basis is None:
        return None
    return basis.with_name(f"{basis.name} {jahr}")


def kollision(conn: sqlite3.Connection, ordner: Path, *, jahr: int | None) -> str | None:
    """Verträgt sich der Ordner mit den anderen hinterlegten? Sonst der Grund.

    ``jahr`` ist das Jahr, fuer das er hinterlegt werden soll (``None``: fuer
    alle Jahre); der bisherige Ordner DIESES Jahres zaehlt nicht mit.
    """
    for anderer in abholen.ordner_liste(conn):
        if anderer.jahr == jahr:
            continue
        if abholen.derselbe_ordner(ordner, anderer.pfad):
            return (f"Dieser Ordner ist schon als {anderer.bezeichnung} hinterlegt — derselbe "
                    "Ordner wird nur einmal abgeholt. Bitte einen eigenen Ordner wählen.")
        for unter in (abholen.UEBERNOMMEN, abholen.NICHT_UEBERNOMMEN):
            if abholen.liegt_in(ordner, anderer.pfad / unter):
                return (f"Dieser Ordner liegt in „{unter}“ des {anderer.bezeichnung}s — dort "
                        "legt das Tool ab, was es abgeholt hat. Bitte einen eigenen Ordner "
                        "wählen.")
            if abholen.liegt_in(anderer.pfad, ordner / unter):
                return (f"Der {anderer.bezeichnung} liegt in „{unter}“ dieses Ordners. Bitte "
                        "einen anderen Ordner wählen.")
        if jahr is not None and anderer.jahr is not None and (
                abholen.liegt_in(ordner, anderer.pfad) or abholen.liegt_in(anderer.pfad, ordner)):
            return (f"Jahresordner dürfen nicht ineinander liegen — dieser und der "
                    f"{anderer.bezeichnung} ({abholen.anzeigename(anderer.pfad)}) tun es. "
                    "Bitte nebeneinanderliegende Ordner wählen, etwa „Belege 2025“ und "
                    "„Belege 2026“.")
    return None


@dataclass(frozen=True, slots=True)
class Belegt:
    """Was schon im Ordner liegt, bevor er hinterlegt wird."""

    belege: int
    andere: int

    @property
    def anzahl(self) -> int:
        return self.belege + self.andere


def schon_belegt(ordner: Path) -> Belegt | None:
    """Liegen in der obersten Ebene schon Dateien? ``None``, wenn nicht (oder
    der Ordner noch nicht da ist).

    Gezaehlt wird wie beim Abholen: versteckte Dateien, Systemdateien,
    Zwischenstaende und Unterordner nicht. Belegformate wuerden abgeholt,
    alles andere nach "Nicht übernommen" verschoben.
    """
    try:
        eintraege = list(os.scandir(ordner))
    except OSError:
        return None
    belege = andere = 0
    for eintrag in eintraege:
        name = eintrag.name
        if (name.startswith(".") or name in abholen.SYSTEMDATEIEN
                or name.lower().endswith(abholen.ZWISCHENENDUNGEN)):
            continue
        try:
            if not eintrag.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if Path(name.strip()).suffix.lower() in pipeline.ERLAUBT:
            belege += 1
        else:
            andere += 1
    return Belegt(belege, andere) if belege or andere else None


def belegt_text(ordner: Path, belegt: Belegt) -> str:
    """Die Rueckfrage, wenn im Ordner schon Dateien liegen."""
    teile = []
    if belegt.belege:
        teile.append(f"{belegt.belege} würde{'n' if belegt.belege != 1 else ''} als Beleg "
                     "abgeholt")
    if belegt.andere:
        teile.append(f"{belegt.andere} nach „{abholen.NICHT_UEBERNOMMEN}“ verschoben")
    anzahl = ("liegt schon 1 Datei" if belegt.anzahl == 1
              else f"liegen schon {belegt.anzahl} Dateien")
    return (f"Im Ordner {anzahl}: {' und '.join(teile)}. Das Tool holt JEDE "
            "Datei der obersten Ebene ab und verschiebt sie nach "
            f"„{abholen.UEBERNOMMEN}“ — liegen dort auch Kontoauszüge, Bescheide oder "
            f"Tabellen, besser einen eigenen Unterordner nehmen, etwa "
            f"„{abholen.anzeigename(ordner / UNTERORDNER)}“. Sollen wirklich alle Dateien "
            "abgeholt werden, „Ja, alle Dateien darin abholen“ anhaken und noch einmal "
            "speichern.")


@dataclass(frozen=True, slots=True)
class Jahreswechsel:
    """Der Hinweis: fuer ``jahr`` ist noch kein Eingangsordner hinterlegt."""

    jahr: int
    vorschlag: Path | None
    #: Den vorgeschlagenen Ordner gibt es schon.
    gefunden: bool

    @property
    def anzeige(self) -> str | None:
        return abholen.anzeigename(self.vorschlag) if self.vorschlag else None


def jahreswechsel(conn: sqlite3.Connection, *, heute: date | None = None) -> Jahreswechsel | None:
    """Fehlt ein Jahresordner fuer das kommende (ab 1. Dezember) oder das
    laufende Jahr? Nur, wenn der Nutzer ueberhaupt Jahresordner fuehrt -- und
    nicht, wenn der Ordner fuer alle Jahre das neue Jahr ohnehin abdeckt.
    """
    heute = heute or date.today()
    vorhanden = jahre(conn)
    if not vorhanden or abholen.ordner(conn) is not None:
        return None
    if heute.month == 12 and heute.year + 1 not in vorhanden:
        jahr = heute.year + 1
    elif heute.year not in vorhanden and max(vorhanden) < heute.year:
        jahr = heute.year
    else:
        return None
    ziel = vorschlag(conn, jahr)
    return Jahreswechsel(jahr, ziel, bool(ziel and ziel.is_dir()))

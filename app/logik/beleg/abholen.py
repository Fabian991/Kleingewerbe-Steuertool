"""Der Eingangsordner: Rechnungen vom Handy, beim Start abgeholt.

Der Weg, um den es geht: eine Rechnung kommt per Mail aufs iPhone. Anhang
oeffnen, Teilen, "In Dateien sichern", iCloud Drive, Ordner "Steuer-Tool
Belege". Ueber iCloud Drive liegt die Datei kurz darauf auf dem Mac -- und beim
naechsten Start holt das Werkzeug sie dort ab: in den lokalen Eingang kopiert,
ausgelesen und, wenn alles sicher ist, gleich als Ausgabe gebucht.

Abholen ist dasselbe wie ein Upload
-----------------------------------
Jede Datei geht durch ``pipeline.aufnehmen`` -- dieselben Pruefungen (Endung,
Groesse, Inhalt passt zur Endung), dieselbe Ablage unter der Pruefsumme. Was
danach nicht automatisch gebucht wird, liegt vorbelegt im Eingang und wartet
auf die Pruefmaske, genau wie eine hochgeladene Datei. Nichts wird erfunden.

Was automatisch gebucht wird -- und was nicht
---------------------------------------------
Nach § 146 Abs. 4 AO darf eine Buchung nicht so veraendert werden, dass der
urspruengliche Inhalt nicht mehr feststellbar ist; das Werkzeug laesst Betrag
und Datum eines gebuchten Belegs deshalb nicht aendern und ihn nicht
loeschen (einzige Ausnahme, mit Journal: der Euro-Betrag eines
Fremdwaehrungsbelegs, siehe app/logik/beleg/umrechnung.py). Automatisch
gebucht wird deshalb nur, was ohne Rueckfrage richtig
ist (``automatisch_buchen``): eine gewoehnliche Rechnung als PDF oder XML,
Lieferant (mit passender USt-IdNr.), Datum und Bruttobetrag sicher erkannt,
Summen, die aufgehen, ein bekannter Lieferant mit gemerkter Kategorie, Euro
oder eine Fremdwaehrung mit EZB-Kurs fuer das Belegdatum, hoechstens 800 EUR
netto, kein aehnlicher Beleg. Dieselben Regeln (``pruefe_automatisch``) gelten
fuer "Alle prüfen und buchen" (app/logik/beleg/sammelbuchung.py), dort mit
neuem Lieferanten und gewaehlter Kategorie.

Die Zahlung: fuer die EUeR zaehlt nach § 11 EStG der Abfluss. Ist "Eingangs-
belege sind in der Regel schon bezahlt" eingestellt (Vorgabe), entsteht sie
mit der Buchung, Zahltag = Belegdatum -- ausser am Jahreswechsel (22.12. bis
10.01.): dort entscheidet der Zahltag das Jahr, und der Beleg wartet auf die
Pruefmaske (app/logik/beleg/bezahlt.py). Ein Belegdatum in einem Vorjahr
wartet beim Abholen ebenfalls: die Zahlung aenderte unbeaufsichtigt eine
EUeR, die womoeglich schon abgegeben ist. Sonst wird ohne Zahlung gebucht.

Was mit der Datei im Ordner passiert
------------------------------------
* Uebernommen (in den Eingang kopiert, gebucht oder wartend, oder schon
  erfasst): sie wandert in den Unterordner "Übernommen" -- und zwar erst,
  wenn entschieden ist, ob sie gebucht wird oder wartet. Endet der Prozess
  mitten im Buchen, liegt sie beim naechsten Mal noch da. Scheitert das
  Verschieben, bleibt sie liegen; das naechste Abholen erkennt sie an der
  Pruefsumme, gebucht wird nichts doppelt.
* Kein Beleg (falsches Format, leer, Inhalt passt nicht zur Endung, zu gross):
  sie wandert nach "Nicht übernommen" und wird nicht bei jedem Start erneut
  gemeldet.
* Noch nicht fertig (juenger als ein paar Sekunden, iCloud-Platzhalter, nicht
  lesbar): sie bleibt liegen und kommt beim naechsten Abholen dran.

Gleichzeitige Laeufe
--------------------
Der Lauf beim Start und der Knopf "Jetzt abholen" koennen sich ueberschneiden.
Im selben Prozess ordnet sie eine Sperre (``_LAUF``). Zwischen zwei Prozessen
(Programm doppelt gestartet) traegt die Pruefsumme: ueber einen Inhalt
entscheidet immer nur ein Lauf (``_Inhaltssperre``), der andere laesst die
Datei liegen und meldet sie nicht. Eine schon gebuchte Datei erkennt
``pipeline.dublette``, eine schon aufgenommene die vorhandene Eingangsdatei --
und bevor sie als wartend gemeldet wird, wird noch einmal nachgesehen, ob sie
inzwischen gebucht oder weggeworfen ist. ``pipeline.buche`` prueft unter
seiner Sperre ein letztes Mal. Verschwindet eine Datei zwischen Auflisten und
Lesen, hat der andere Lauf sie gerade verschoben -- sie wird still
uebersprungen.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import sqlite3
import stat as _stat
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app import db
from app.logik import backup as sicherung
from app.logik import kategorien, kurse
from app.logik.backup import ARCHIVMUSTER, DB_IM_ARCHIV, fehlertext
from app.logik.backup import _pfad as _gespeicherter_pfad
from app.logik.beleg import bezahlt as bezahlvorgabe
from app.logik.beleg import eigenbeleg, pipeline, textdienst, xml_leser
from app.logik.beleg.ergebnis import BelegFehler, Fund
from app.logik.rundung import euro

#: Unterordner im Eingangsordner. Mit echten Umlauten: der Nutzer sieht sie
#: in der Dateien-App und im Finder.
UEBERNOMMEN = "Übernommen"
NICHT_UEBERNOMMEN = "Nicht übernommen"

#: So heisst der vorgeschlagene Ordner in iCloud Drive.
VORSCHLAG_NAME = "Steuer-Tool Belege"

#: Juenger als so viele Sekunden: die Datei wird vermutlich noch geschrieben
#: (iCloud laedt, der Finder kopiert). Sie kommt beim naechsten Abholen dran.
#: Gemessen an der juengeren von Aenderungs- und Statuszeit (mtime, ctime):
#: eine Kopie mit erhaltener Aenderungszeit (Finder, cp -p) traegt eine alte
#: mtime, waehrend sie noch geschrieben wird -- die ctime ist dann jung.
MINDESTALTER_SEKUNDEN = 5

#: Laesst sich eine Datei nicht lesen und ist sie juenger als so viele
#: Sekunden, ist sie vermutlich nur noch nicht fertig (ein Kopiervorgang, der
#: stockt). Dann bleibt sie liegen und kommt beim naechsten Mal dran, statt
#: abgeschnitten im Eingang auf eine Erfassung von Hand zu warten.
UNLESBAR_MINDESTALTER_SEKUNDEN = 60

#: So beginnt der Grund, wenn das Auslesen am Inhalt scheiterte.
NICHT_LESBAR = "nicht lesbar — "

#: Liegt die Aenderungszeit weiter als so viele Sekunden in der Zukunft, ist
#: das eine falsch gehende Uhr (Handy, Netzlaufwerk) und kein laufendes
#: Schreiben -- sonst blieb die Datei fuer immer "noch im Schreiben".
UHRENFEHLER_SEKUNDEN = 60

#: Platzhalter, die iCloud Drive (bis macOS 13) fuer noch nicht geladene
#: Dateien anlegt: ".Rechnung.pdf.icloud".
ICLOUD_ENDUNG = ".icloud"

#: Dateien, die Betriebssysteme und Browser selbst anlegen. Sie werden still
#: uebersprungen -- sie nach "Nicht übernommen" zu schieben hiesse, einem
#: laufenden Download die Datei wegzunehmen.
SYSTEMDATEIEN = frozenset({"Icon\r", "Thumbs.db", "desktop.ini"})
ZWISCHENENDUNGEN = (".crdownload", ".part", ".partial", ".download", ".tmp")

#: Nur diese Formate werden ohne Pruefmaske gebucht. Ein Foto oder Scan geht
#: durch die Bilderkennung, und deren Funde sind nie sicher. Eine E-Mail wird
#: entschieden wie ihr Anhang, wenn er die Rechnung ist; ohne Rechnung im
#: Anhang sind ihre Funde unsicher, sie wartet also (ausser ein gelerntes
#: Muster des bekannten Lieferanten bestaetigt sie, wie bei einem PDF).
AUTOMATISCH_ENDUNGEN = (".pdf", ".xml", ".eml")

#: Der Grund, wenn der Lieferant noch nicht bekannt ist -- der haeufigste.
NEUER_LIEFERANT = ("neuer Lieferant — beim ersten Beleg Kategorie wählen und „als Vorgabe "
                   "merken“ anhaken")

#: Hoechstens so viel netto wird ohne Pruefmaske gebucht, in JEDER Kategorie:
#: die GWG-Grenze (§ 6 Abs. 2 EStG). Lieferanten wie Versandhaendler liefern
#: Verbrauchsmaterial UND Anlagegueter; mit der Vorgabe "Bürobedarf" waere
#: ein Laptop sonst sofort abgezogen statt ueber das Anlagenverzeichnis
#: abgeschrieben -- und ein gebuchter Beleg laesst sich nicht nachtraeglich
#: zum Anlagegut machen.
HOECHSTENS_NETTO_CENT = 80_000

#: Notiz am automatisch gebuchten Beleg.
NOTIZ = "Automatisch aus dem Eingangsordner gebucht."

#: Die Felder, die sicher erkannt sein muessen, und wie sie in Gruenden heissen.
PFLICHTFELDER = {"lieferant": "Lieferant", "belegdatum": "Belegdatum", "brutto": "Bruttobetrag"}

#: Ordner, die als Eingangsordner nicht taugen, weil dort auch anderes liegt
#: -- abgeholt wird JEDE Datei der obersten Ebene (unter "Heim"; dazu der
#: Heimordner selbst, seine Vorfahren und iCloud Drive als Ganzes).
SAMMELORDNER_IM_HEIM = ("Desktop", "Documents", "Downloads", "Library", "Movies",
                        "Music", "Pictures", "Public")
SAMMELORDNER_IN_ICLOUD = ("Desktop", "Documents", "Downloads")

#: Ein Lauf zur Zeit in diesem Prozess: der Start und der Knopf "Jetzt
#: abholen" duerfen dieselbe Datei nicht gleichzeitig aufnehmen.
_LAUF = threading.Lock()


class LaeuftSchon(Exception):
    """Ein anderer Lauf holt gerade ab (nur bei ``warten=False``)."""


class DatenbankBelegt(Exception):
    """Die Datenbank ist von einem anderen Vorgang gesperrt ("database is
    locked"). Der Lauf hoert dann auf, statt fuer jede weitere Datei erneut
    fuenf Sekunden zu warten und sie in die Pruefmaske zu legen."""


#: Laufeintraege, die sich nicht ins Journal schreiben liessen (Datenbank
#: belegt). Der naechste Lauf in diesem Prozess schreibt sie nach. Geschuetzt
#: durch ``_LAUF``.
_NACHZUHOLEN: list[dict[str, object]] = []

#: Die Meldung, wenn das Programm mitten im Lauf beendet wird.
BEIM_BEENDEN = (
    "Das Abholen wurde beim Beenden des Programms unterbrochen. Was noch nicht "
    "abgeholt ist, liegt weiter im Eingangsordner und kommt beim nächsten Start dran."
)

#: Die Meldung, wenn der Lauf wegen einer gesperrten Datenbank aufhoert.
DATENBANK_BELEGT = (
    "Die Datenbank war gerade von einem anderen Vorgang belegt — das Abholen hat "
    "aufgehört. Was noch nicht abgeholt ist, liegt weiter im Eingangsordner und kommt "
    "mit „Jetzt abholen“ oder beim nächsten Start dran."
)


def _gesperrt(fehler: BaseException) -> bool:
    """Ist das die gesperrte Datenbank (und nicht ein anderer Datenbankfehler)?"""
    text = str(fehler).lower()
    return isinstance(fehler, sqlite3.OperationalError) and ("locked" in text
                                                             or "busy" in text)


# ------------------------------------------------------------- Einstellung

def vorschlag_ordner() -> Path | None:
    """Der vorgeschlagene Eingangsordner -- auf dem Mac in iCloud Drive.

    Warum iCloud Drive: die Dateien-App des iPhones legt eine Datei dort ab,
    und kurz darauf liegt sie auf dem Mac. Kein Kabel, kein Mailversand an
    sich selbst, keine weitere App.

    Warum der Ordner NICHT der Datenordner ist: iCloud Drive ist als Ablage
    der laufenden Datenbank ausgeschlossen (README, ``db.standard_datenordner``)
    -- defektes Locking, Kopieren waehrend laufender Transaktionen und das
    Ausduennen zu Platzhaltern beschaedigen eine SQLite-Datei. Als POSTFACH
    fuer Belege ist iCloud Drive dagegen richtig: das Werkzeug liest dort nur
    abgeschlossene Dateien und kopiert sie sofort in den lokalen Eingang; was
    danach in iCloud passiert, beruehrt die Buchfuehrung nicht mehr.

    Auf anderen Systemen gibt es keinen sinnvollen Vorschlag: ``None``.
    """
    if not _ist_mac():
        return None
    return (Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
            / VORSCHLAG_NAME)


def _ist_mac() -> bool:
    return sys.platform == "darwin"


def ordner(conn: sqlite3.Connection) -> Path | None:
    """Der eingestellte Eingangsordner; ``None`` heisst: aus.

    Die Tilde wird aufgeloest wie beim Sicherungsziel -- und ein
    "~unbekannt/..." aus einer alten Einstellung legt nichts lahm.
    """
    zeile = conn.execute("SELECT belegordner_pfad FROM betrieb WHERE id = 1").fetchone()
    if zeile is None or not (zeile["belegordner_pfad"] or "").strip():
        return None
    return _gespeicherter_pfad(zeile["belegordner_pfad"].strip())


@dataclass(frozen=True, slots=True)
class Eingangsordner:
    """Ein hinterlegter Eingangsordner: der eines Jahres oder (``jahr`` None)
    der fuer alle Jahre."""

    jahr: int | None
    pfad: Path

    @property
    def bezeichnung(self) -> str:
        """
        >>> jahr, alle = Eingangsordner(2026, Path("/x")), Eingangsordner(None, Path("/x"))
        >>> jahr.bezeichnung, alle.bezeichnung
        ('Ordner 2026', 'Ordner für alle Jahre')
        """
        return f"Ordner {self.jahr}" if self.jahr is not None else "Ordner für alle Jahre"


def ordner_liste(conn: sqlite3.Connection) -> list[Eingangsordner]:
    """Alle hinterlegten Eingangsordner: die Jahre absteigend, der fuer alle
    Jahre zuletzt. Abgeholt werden sie alle -- auch die alter Jahre, fuer
    Nachzuegler, bis der Nutzer sie entfernt."""
    liste = []
    try:
        zeilen = conn.execute("SELECT jahr, pfad FROM belegordner_jahr ORDER BY jahr DESC"
                              ).fetchall()
    except sqlite3.OperationalError:
        zeilen = []  # eine Datenbank vor Schema 24 (nur im Test denkbar)
    for zeile in zeilen:
        if (zeile["pfad"] or "").strip():
            liste.append(Eingangsordner(zeile["jahr"], _gespeicherter_pfad(zeile["pfad"].strip())))
    alle = ordner(conn)
    if alle is not None:
        liste.append(Eingangsordner(None, alle))
    return liste


def automatisch(conn: sqlite3.Connection) -> bool:
    """Ob sicher erkannte Rechnungen gleich gebucht werden (Vorgabe: ja)."""
    zeile = conn.execute(
        "SELECT belegordner_automatisch FROM betrieb WHERE id = 1"
    ).fetchone()
    return True if zeile is None else bool(zeile["belegordner_automatisch"])


def aufgeloest(pfad: Path) -> Path:
    """Absolut und ohne Umwege -- oder wie er dasteht, wenn das scheitert."""
    try:
        return Path(pfad).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(pfad)


def _textform(pfad: Path) -> str:
    """Ein Pfad als Text, wie APFS ihn vergleicht: ohne Gross-/Kleinschreibung
    und ohne Unterschied zwischen zerlegten und zusammengesetzten Umlauten."""
    return unicodedata.normalize("NFC", str(aufgeloest(pfad))).casefold()


def derselbe_ordner(a: Path, b: Path) -> bool:
    """Sind ``a`` und ``b`` derselbe Ordner -- am Dateisystem entschieden?

    ``Path.resolve`` uebernimmt die Schreibweise der Eingabe. Auf dem Mac
    sind "~/library/application support/steuer-tool", ein zerlegtes "ü" und
    der Firmlink /System/Volumes/Data/Users/... aber DERSELBE Ordner wie der
    Datenordner -- ein Textvergleich liess sie durch, und das Abholen schob
    die laufende Datenbank nach "Nicht übernommen". Deshalb Geraet und Inode
    (``os.path.samefile``), das traegt auch Bind-Mounts und Verknuepfungen.
    Gibt es einen der beiden (noch) nicht, bleibt nur der Text -- dann ohne
    Gross-/Kleinschreibung und in NFC, wie APFS vergleicht.

    >>> derselbe_ordner(Path("/gibt/es/Nicht"), Path("/gibt/es/nicht"))
    True
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return _textform(a) == _textform(b)


def liegt_in(kind: Path, eltern: Path) -> bool:
    """Ist ``kind`` der Ordner ``eltern`` selbst oder liegt darin?

    >>> liegt_in(Path("/gibt/es/nicht/eingang"), Path("/gibt/ES/nicht"))
    True
    >>> liegt_in(Path("/gibt/es"), Path("/gibt/es/nicht"))
    False
    """
    weg = aufgeloest(kind)
    return any(derselbe_ordner(stufe, eltern) for stufe in (weg, *weg.parents))


def _sammelordner() -> list[Path]:
    """Heimordner, seine Vorfahren, die Standardordner darin und iCloud Drive."""
    heim = Path.home()
    icloud = heim / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    ordner_ = [heim, *heim.parents, icloud, icloud.parent]
    ordner_ += [heim / name for name in SAMMELORDNER_IM_HEIM]
    ordner_ += [icloud / name for name in SAMMELORDNER_IN_ICLOUD]
    return ordner_


def sicherungsziele(conn: sqlite3.Connection, datenordner: Path) -> list[Path]:
    """Die eingestellten Sicherungsziele (erstes und zweites)."""
    ziele = [sicherung.zielordner(conn, datenordner)]
    zweites = sicherung.zweitziel(conn)
    if zweites is not None:
        ziele.append(zweites)
    return ziele


SICHERUNGSORDNER = ("Der Eingangsordner darf nicht der Sicherungsordner sein — das Tool "
                    "verschiebt jede Datei, die es dort findet, und die Sicherungen fehlten "
                    "danach. Bitte einen eigenen Ordner nur für Rechnungen wählen.")


def ordnerfehler(datenordner: Path, ordner: Path, *,
                 sicherungen: list[Path] | tuple[Path, ...] = ()) -> str | None:
    """Warum ein Ordner als Eingangsordner nicht taugt -- ``None``, wenn er taugt.

    * Er muss absolut sein: ein relativer Pfad hinge davon ab, aus welchem
      Ordner das Programm gestartet wurde.
    * Nicht der Datenordner, nicht darin, nicht darueber: abgeholt wird jede
      Datei der obersten Ebene und danach verschoben -- im Datenordner traefe
      das die Datenbank selbst.
    * Nicht der Programmordner, und kein Sammelordner wie Downloads oder
      Schreibtisch: dort liegt anderes, und das Werkzeug wuerde es nach
      "Nicht übernommen" schieben.
    * Nicht ein Sicherungsziel (``sicherungen``): die Archive waeren "kein
      Belegformat" und landeten in "Nicht übernommen". Geprueft beim
      Speichern UND bei jedem Lauf -- das Sicherungsziel laesst sich spaeter
      umstellen.

    >>> ordnerfehler(Path("/daten"), Path("relativ/ordner"))[:30]
    'Bitte den vollständigen Pfad a'
    >>> ordnerfehler(Path("/daten"), Path("/daten/eingang"))[:40]
    'Der Eingangsordner darf nicht im Datenor'
    >>> ordnerfehler(Path("/daten/steuer"), Path("/daten")) is not None
    True
    >>> ordnerfehler(Path("/daten"), Path("/belege/Steuer-Tool Belege")) is None
    True
    """
    if not Path(ordner).is_absolute():
        return ("Bitte den vollständigen Pfad angeben — er beginnt mit „/“ oder „~“, "
                "etwa ~/Library/Mobile Documents/com~apple~CloudDocs/Steuer-Tool Belege.")
    ziel = aufgeloest(ordner)
    if liegt_in(ziel, datenordner) or liegt_in(datenordner, ziel):
        return ("Der Eingangsordner darf nicht im Datenordner liegen, nicht der Datenordner "
                "selbst sein und nicht darüber — das Tool verschiebt jede Datei, die es dort "
                "abholt. Bitte einen eigenen Ordner wählen, etwa in iCloud Drive.")
    if liegt_in(ziel, db.PROJEKT_WURZEL):
        return ("Der Eingangsordner darf nicht im Programmordner liegen — bitte einen "
                "eigenen Ordner nur für Rechnungen wählen.")
    if any(derselbe_ordner(ziel, sicherung_) for sicherung_ in sicherungen):
        return SICHERUNGSORDNER
    if any(derselbe_ordner(ziel, sammel) for sammel in _sammelordner()):
        return (f"{ziel} ist ein allgemeiner Ordner, in dem auch anderes liegt — das Tool "
                "verschiebt jede Datei, die es dort abholt. Bitte einen eigenen Ordner nur "
                f"für Rechnungen wählen, etwa „{ziel / VORSCHLAG_NAME}“.")
    return None


def in_icloud(ordner: Path) -> bool:
    """Liegt der Ordner in iCloud Drive (``~/Library/Mobile Documents``)?

    >>> in_icloud(Path("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/B"))
    True
    >>> in_icloud(Path("/Users/x/Belege"))
    False
    """
    return "Mobile Documents" in Path(ordner).parts


def icloud_teile(ordner: Path) -> list[str] | None:
    """Der Weg in iCloud Drive, wie ihn die Dateien-App und der Finder zeigen.

    ``~/Library/Mobile Documents/com~apple~CloudDocs/Steuer-Tool Belege`` ist
    in der Dateien-App "iCloud Drive › Steuer-Tool Belege". ``None``, wenn der
    Ordner nicht in iCloud Drive liegt.

    >>> icloud_teile(Path("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/A/B"))
    ['A', 'B']
    >>> icloud_teile(Path("/Users/x/Belege")) is None
    True
    """
    teile = Path(ordner).parts
    if "com~apple~CloudDocs" not in teile:
        return None
    return list(teile[teile.index("com~apple~CloudDocs") + 1:])


def anzeigename(ordner: Path | str) -> str:
    """Ein Ordner so, wie ihn ein Nicht-Entwickler kennt.

    >>> anzeigename("/Users/x/Library/Mobile Documents/com~apple~CloudDocs/Steuer-Tool Belege")
    'iCloud Drive › Steuer-Tool Belege'
    >>> anzeigename("/Users/x/Belege")
    '/Users/x/Belege'
    """
    teile = icloud_teile(Path(ordner))
    if teile is None:
        return str(ordner)
    return " › ".join(["iCloud Drive", *teile])


def icloud_hinweis(ordner: Path) -> str:
    """Der Satz, der bei einem fehlenden Ordner in iCloud Drive weiterhilft."""
    if not in_icloud(ordner):
        return ""
    return (" Ist iCloud Drive auf diesem Mac eingeschaltet? (Systemeinstellungen → "
            "dein Name → iCloud → iCloud Drive)")


def ordner_fehlt(ordner: Path) -> str:
    """Die Meldung, wenn der Eingangsordner nicht (mehr) da ist."""
    return (f"Der Eingangsordner {ordner} fehlt.{icloud_hinweis(ordner)} Unter „Belege“ → "
            "„Eingangsordner“ lässt er sich neu wählen — oder das Feld leeren, dann holt das "
            "Tool nichts mehr ab.")


def kein_zugriff(ordner: Path, *, danach: str = "„Jetzt abholen“") -> str:
    """Die Meldung, wenn macOS den Zugriff verweigert (Datenschutz, TCC).

    ``danach`` ist der Knopf fuer den zweiten Versuch -- beim Einrichten gibt
    es "Jetzt abholen" noch nicht, dort heisst er "Vorschlag übernehmen"
    bzw. "Speichern".
    """
    wo = "iCloud Drive" if in_icloud(ordner) else "diesen Ordner"
    return (
        f"Kein Zugriff auf den Eingangsordner {ordner}. macOS fragt beim ersten Mal, ob "
        f"das Terminal auf {wo} zugreifen darf — wurde das abgelehnt: Systemeinstellungen "
        "→ Datenschutz & Sicherheit → Dateien und Ordner → Terminal → iCloud Drive (bzw. "
        f"den Ordner) erlauben, dann {danach}."
    )


# ---------------------------------------------------------------- Ergebnis

@dataclass(frozen=True, slots=True)
class Gebucht:
    beleg_id: int
    originalname: str
    lieferant: str | None
    brutto_cent: int


@dataclass(frozen=True, slots=True)
class Wartend:
    """Liegt vorbelegt im Eingang und wartet auf die Pruefmaske."""

    eingangsname: str
    originalname: str
    grund: str


@dataclass(frozen=True, slots=True)
class Abgewiesen:
    originalname: str
    grund: str
    #: True: liegt jetzt in "Nicht übernommen". False: liegt noch im Ordner
    #: (etwa nicht lesbar) und kommt beim naechsten Abholen wieder dran.
    verschoben: bool = True


@dataclass(frozen=True, slots=True)
class NichtVerschoben:
    """Erledigt, aber im Eingangsordner nicht zu verschieben: sie liegt noch
    oben. Gemeldet wird das EINMAL; danach merkt sich das Werkzeug die Datei
    und nimmt sie nicht bei jedem Start erneut auf."""

    originalname: str
    grund: str


@dataclass(frozen=True, slots=True)
class SchonVorhanden:
    """Genau diese Datei ist schon gebucht (``beleg_id``) oder liegt schon im
    Eingang (``eingangsname``)."""

    originalname: str
    beleg_id: int | None
    eingangsname: str | None = None


def _anzahl(n: int, einzahl: str, mehrzahl: str) -> str:
    return f"{n} {einzahl if n == 1 else mehrzahl}"


def _aufzaehlung(teile: list[str]) -> str:
    """Teile als deutsche Aufzaehlung.

    >>> _aufzaehlung(["a"]), _aufzaehlung(["a", "b"]), _aufzaehlung(["a", "b", "c"])
    ('a', 'a und b', 'a, b und c')
    """
    if len(teile) <= 1:
        return "".join(teile)
    return f"{', '.join(teile[:-1])} und {teile[-1]}"


def _wie_viele_davon(anzahl: int, von: int) -> str:
    """Wie viele der gezaehlten Dateien etwas betrifft, als Satzanfang.

    >>> _wie_viele_davon(1, 1), _wie_viele_davon(2, 2), _wie_viele_davon(3, 3)
    ('sie ließ', 'beide ließen', 'alle 3 ließen')
    >>> _wie_viele_davon(1, 4), _wie_viele_davon(2, 4)
    ('1 davon ließ', '2 davon ließen')
    """
    verb = "ließ" if anzahl == 1 else "ließen"
    if anzahl == von:
        return {1: "sie", 2: "beide"}.get(anzahl, f"alle {anzahl}") + f" {verb}"
    return f"{anzahl} davon {verb}"


@dataclass(slots=True)
class Ergebnis:
    """Was ein Abholvorgang getan hat -- fuer Meldung, Uebersicht und Journal."""

    gebucht: list[Gebucht] = field(default_factory=list)
    wartend: list[Wartend] = field(default_factory=list)
    abgewiesen: list[Abgewiesen] = field(default_factory=list)
    schon_vorhanden: list[SchonVorhanden] = field(default_factory=list)
    #: Namen von Dateien, die iCloud noch nicht auf diesen Mac geladen hat.
    icloud_ausstehend: list[str] = field(default_factory=list)
    #: Namen von Dateien, die eben erst entstanden sind.
    noch_im_schreiben: list[str] = field(default_factory=list)
    #: Erledigt, aber im Ordner nicht zu verschieben (geschuetzt, keine Rechte).
    nicht_verschoben: list[NichtVerschoben] = field(default_factory=list)
    #: Warum der Lauf nichts tun konnte (Ordner fehlt, kein Zugriff).
    fehler: str | None = None
    zeitpunkt: str = field(default_factory=db.jetzt_utc)
    #: Der Lauf hat mittendrin aufgehoert (Datenbank belegt).
    abgebrochen: bool = False
    #: Der Ordner, der abgeholt wurde -- die Oberflaeche zeigt das Ergebnis
    #: nur, solange er noch eingestellt ist.
    ordner: str | None = None
    #: Das Jahr des Ordners; ``None`` beim Ordner fuer alle Jahre.
    jahr: int | None = None
    #: Belege, deren Datum nicht zum Jahr des Ordners passt -- nur ein Hinweis.
    jahr_abweichend: list[str] = field(default_factory=list)

    def veraendert(self) -> bool:
        """Ist etwas aufgenommen, gebucht oder verschoben worden?"""
        return bool(self.gebucht or self.wartend or self.abgewiesen or self.schon_vorhanden)

    def leer(self) -> bool:
        """Nichts passiert und nichts zu melden.

        >>> Ergebnis().leer(), Ergebnis(fehler="x").leer(), Ergebnis(icloud_ausstehend=["a"]).leer()
        (True, False, False)
        """
        return not (self.veraendert() or self.icloud_ausstehend or self.noch_im_schreiben
                    or self.nicht_verschoben or self.fehler)

    def zusammenfassung(self) -> str:
        """Ein Satz fuer Meldung und Uebersicht, mit Einzahl und Mehrzahl.

        >>> Ergebnis().zusammenfassung()
        'Im Eingangsordner lag nichts Neues.'
        >>> Ergebnis(gebucht=[Gebucht(1, "a.pdf", "X", 100)],
        ...          wartend=[Wartend("1.pdf", "b.pdf", "g"), Wartend("2.pdf", "c.pdf", "g")],
        ...          abgewiesen=[Abgewiesen("d.docx", "g")]).zusammenfassung()
        'Aus dem Eingangsordner: 1 Rechnung automatisch gebucht, 2 Rechnungen warten in der \
Prüfmaske und 1 Datei nicht übernommen.'
        >>> Ergebnis(fehler="Der Eingangsordner /x fehlt.").zusammenfassung()
        'Der Eingangsordner /x fehlt.'

        Jede Datei zaehlt einmal. Eine erledigte, die sich nicht verschieben
        liess, steht schon unter "gebucht" oder "nicht übernommen" -- sie wird
        dort mitgezaehlt, und der Satz sagt, wie viele davon liegen blieben:

        >>> g = "keine Berechtigung"
        >>> Ergebnis(gebucht=[Gebucht(1, "a.xml", "X", 100)],
        ...          abgewiesen=[Abgewiesen("b.docx", g, verschoben=False)],
        ...          nicht_verschoben=[NichtVerschoben("a.xml", g),
        ...                            NichtVerschoben("b.docx", g)]).zusammenfassung()
        'Aus dem Eingangsordner: 1 Rechnung automatisch gebucht und 1 Datei nicht \
übernommen; beide ließen sich im Ordner nicht verschieben.'
        """
        teile = []
        if self.gebucht:
            teile.append(_anzahl(len(self.gebucht), "Rechnung", "Rechnungen")
                         + " automatisch gebucht")
        if self.wartend:
            teile.append(_anzahl(len(self.wartend), "Rechnung wartet", "Rechnungen warten")
                         + " in der Prüfmaske")
        # "Schon erfasst" las sich wie "gebucht" -- auch fuer eine Datei, die
        # nur ungebucht im Eingang lag. Deshalb getrennt.
        gebucht = [s for s in self.schon_vorhanden if s.beleg_id is not None]
        im_eingang = [s for s in self.schon_vorhanden if s.beleg_id is None]
        if gebucht:
            teile.append(_anzahl(len(gebucht), "Datei war", "Dateien waren")
                         + " schon gebucht")
        if im_eingang:
            teile.append(_anzahl(len(im_eingang), "Datei wartet", "Dateien warten")
                         + " schon in der Prüfmaske")
        if self.abgewiesen:
            teile.append(_anzahl(len(self.abgewiesen), "Datei", "Dateien") + " nicht übernommen")
        # Liegen geblieben ist eine Datei, die oben schon gezaehlt ist (gebucht,
        # wartend, nicht übernommen): sie zaehlt nicht ein zweites Mal.
        erledigt = self.gebucht + self.wartend + self.schon_vorhanden + self.abgewiesen
        namen = {e.originalname for e in erledigt}
        davon = [n for n in self.nicht_verschoben if n.originalname in namen]
        allein = [n for n in self.nicht_verschoben if n.originalname not in namen]
        if allein:
            teile.append(_anzahl(len(allein), "Datei ließ", "Dateien ließen")
                         + " sich im Ordner nicht verschieben")
        offen = []
        if self.icloud_ausstehend:
            offen.append(_anzahl(len(self.icloud_ausstehend), "Datei lädt", "Dateien laden")
                         + " noch aus iCloud")
        if self.noch_im_schreiben:
            offen.append(_anzahl(len(self.noch_im_schreiben), "Datei wird", "Dateien werden")
                         + " gerade noch gespeichert")
        if not teile and not offen:
            return self.fehler or "Im Eingangsordner lag nichts Neues."
        if davon:
            satz = (f"Aus dem Eingangsordner: {_aufzaehlung(teile)}; "
                    f"{_wie_viele_davon(len(davon), len(erledigt))} sich im Ordner nicht "
                    "verschieben.")
            if offen:
                satz += f" Noch nicht abgeholt: {_aufzaehlung(offen)}."
        else:
            satz = f"Aus dem Eingangsordner: {_aufzaehlung(teile + offen)}."
        return f"{satz} {self.fehler}" if self.fehler else satz


def _bezeichnung(ergebnis: Ergebnis) -> str:
    return f"Ordner {ergebnis.jahr}" if ergebnis.jahr is not None else "Ordner für alle Jahre"


@dataclass(slots=True)
class Gesamtergebnis:
    """Was ein Lauf ueber ALLE hinterlegten Eingangsordner getan hat -- je
    Ordner ein ``Ergebnis`` (``teile``).

    Nach aussen sieht es aus wie ein ``Ergebnis``: die Listen sind die aller
    Ordner zusammen, so zeigen Belegseite und Uebersicht es ohne Unterschied.
    Der Satz nennt bei mehreren Ordnern jeden fuer sich.
    """

    teile: list[Ergebnis] = field(default_factory=list)
    zeitpunkt: str = field(default_factory=db.jetzt_utc)

    def _alle(self, name: str) -> list:
        return [eintrag for teil in self.teile for eintrag in getattr(teil, name)]

    @property
    def gebucht(self) -> list[Gebucht]:
        return self._alle("gebucht")

    @property
    def wartend(self) -> list[Wartend]:
        return self._alle("wartend")

    @property
    def abgewiesen(self) -> list[Abgewiesen]:
        return self._alle("abgewiesen")

    @property
    def schon_vorhanden(self) -> list[SchonVorhanden]:
        return self._alle("schon_vorhanden")

    @property
    def icloud_ausstehend(self) -> list[str]:
        return self._alle("icloud_ausstehend")

    @property
    def noch_im_schreiben(self) -> list[str]:
        return self._alle("noch_im_schreiben")

    @property
    def nicht_verschoben(self) -> list[NichtVerschoben]:
        return self._alle("nicht_verschoben")

    @property
    def jahr_abweichend(self) -> list[str]:
        return self._alle("jahr_abweichend")

    @property
    def abgebrochen(self) -> bool:
        return any(teil.abgebrochen for teil in self.teile)

    @property
    def fehler(self) -> str | None:
        fehler = [(f"{_bezeichnung(t)}: {t.fehler}" if len(self.teile) > 1 else t.fehler)
                  for t in self.teile if t.fehler]
        return " ".join(fehler) or None

    @property
    def ordner(self) -> str | None:
        return self.teile[0].ordner if len(self.teile) == 1 else None

    def veraendert(self) -> bool:
        return any(teil.veraendert() for teil in self.teile)

    def leer(self) -> bool:
        return all(teil.leer() for teil in self.teile)

    def zusammenfassung(self) -> str:
        """Ein Satz -- bei einem Ordner wie bisher, bei mehreren je Ordner.

        >>> e = Gesamtergebnis([Ergebnis(gebucht=[Gebucht(1, "a.xml", "X", 100)], jahr=2026),
        ...                     Ergebnis(jahr=2025), Ergebnis(fehler="Der Ordner fehlt.")])
        >>> e.zusammenfassung()
        'Aus den Eingangsordnern: Ordner 2026: 1 Rechnung automatisch gebucht. Ordner für alle \
Jahre: Der Ordner fehlt.'
        >>> Gesamtergebnis([Ergebnis(jahr=2026)]).zusammenfassung()
        'Im Eingangsordner lag nichts Neues.'
        """
        if len(self.teile) == 1:
            return self.teile[0].zusammenfassung()
        etwas = [t for t in self.teile if not t.leer()]
        if not etwas:
            return "In den Eingangsordnern lag nichts Neues."
        saetze = [f"{_bezeichnung(t)}: "
                  + t.zusammenfassung().removeprefix("Aus dem Eingangsordner: ")
                  for t in etwas]
        return "Aus den Eingangsordnern: " + " ".join(saetze)


@dataclass(slots=True)
class Laufstand:
    """Was die Oberflaeche ueber den letzten Abholvorgang weiss.

    Liegt nur im Speicher (``app.state.belegordner``): ein Neustart holt ohnehin
    neu ab, und was dabei gebucht wurde, steht im Journal.
    """

    laeuft: bool = False
    ergebnis: Ergebnis | Gesamtergebnis | None = None
    zeitpunkt: str | None = None

    def fertig(self, ergebnis: Ergebnis | Gesamtergebnis) -> None:
        self.ergebnis = ergebnis
        self.zeitpunkt = ergebnis.zeitpunkt
        self.laeuft = False


def aktuelles_ergebnis(stand: Laufstand | None, conn: sqlite3.Connection
                       ) -> Ergebnis | Gesamtergebnis | None:
    """Das letzte Ergebnis -- aber nur fuer Ordner, die noch hinterlegt sind.

    Nach dem Ausschalten oder einem Wechsel des Ordners stand sonst bis zum
    Neustart das alte Ergebnis da, auch als roter Kasten "Kein Zugriff … dann
    „Jetzt abholen“" auf der Uebersicht, obwohl es den Knopf nicht mehr gab.
    Bei mehreren Ordnern verschwindet der Teil eines entfernten Ordners.
    """
    if stand is None or stand.ergebnis is None:
        return None
    hinterlegt = {(o.jahr, str(o.pfad)) for o in ordner_liste(conn)}

    def gilt(teil: Ergebnis) -> bool:
        if teil.ordner is None:
            return not hinterlegt  # ohne Ordner gelaufen: gilt, solange keiner da ist
        return (teil.jahr, teil.ordner) in hinterlegt

    if isinstance(stand.ergebnis, Gesamtergebnis):
        teile = [t for t in stand.ergebnis.teile if gilt(t)]
        if not teile:
            return None
        if len(teile) == len(stand.ergebnis.teile):
            return stand.ergebnis
        return Gesamtergebnis(teile, stand.ergebnis.zeitpunkt)
    return stand.ergebnis if gilt(stand.ergebnis) else None


# ------------------------------------------------------------------ Abholen

def abholen(
    conn: sqlite3.Connection,
    datenordner: Path,
    ordner: Path,
    *,
    automatisch: bool,
    heute: date | None = None,
    jetzt: float | None = None,
    stand: Laufstand | None = None,
    warten: bool = True,
    stopp: threading.Event | None = None,
    jahr: int | None = None,
) -> Ergebnis:
    """Holt alle neuen Dateien aus EINEM Eingangsordner ab. Wirft nichts.

    ``jahr``: der Ordner gehoert zu diesem Jahr (``None``: fuer alle Jahre)
    -- dann nennt das Ergebnis Belege, deren Datum nicht dazu passt.
    Alle hinterlegten Ordner auf einmal holt :func:`abholen_alle` ab.

    ``jetzt`` ist die Uhrzeit fuer die Altersgrenze (Sekunden wie
    ``time.time()``), ``heute`` das Datum fuer Plausibilitaet und Buchung --
    beides nur fuer Tests. ``stand`` wird waehrend des Laufs auf "laeuft" und
    danach auf das Ergebnis gesetzt. Mit ``warten=False`` wirft der Aufruf
    ``LaeuftSchon``, statt auf einen laufenden Vorgang zu warten -- das ist die
    einzige Ausnahme, die herauskommt.

    ``stopp`` ist das Signal zum Aufhoeren (das Programm wird beendet): der
    Lauf prueft es zwischen zwei Dateien und waehrend einer langsamen
    Textschicht, hoert sauber auf und schreibt sein Journal -- statt mitten
    im Buchen abgebrochen zu werden.
    """
    if not _LAUF.acquire(blocking=warten):
        raise LaeuftSchon("Das Abholen läuft gerade.")
    ergebnis = Ergebnis(ordner=str(ordner), jahr=jahr)
    try:
        if stand is not None:
            stand.laeuft = True
        _ein_ordner(conn, datenordner, ordner, ergebnis, automatisch=automatisch,
                    heute=heute, jetzt=jetzt, stopp=stopp)
        return ergebnis
    finally:
        if stand is not None:
            stand.fertig(ergebnis)
        _LAUF.release()


def abholen_alle(
    conn: sqlite3.Connection,
    datenordner: Path,
    liste: list[Eingangsordner],
    *,
    automatisch: bool,
    heute: date | None = None,
    jetzt: float | None = None,
    stand: Laufstand | None = None,
    warten: bool = True,
    stopp: threading.Event | None = None,
) -> Gesamtergebnis:
    """Holt alle hinterlegten Eingangsordner ab, einen nach dem anderen.
    Wirft nichts (ausser ``LaeuftSchon`` bei ``warten=False``).

    EINE Sperre fuer alle Ordner: "Jetzt abholen" und der Lauf beim Start
    ueberschneiden sich nicht, auch nicht zwischen zwei Ordnern. Je Ordner
    ein Ergebnis und ein Journaleintrag. Zwischen den Ordnern wird auf das
    Stoppsignal geachtet; ist die Datenbank belegt, hoert der ganze Lauf auf.
    """
    if not _LAUF.acquire(blocking=warten):
        raise LaeuftSchon("Das Abholen läuft gerade.")
    gesamt = Gesamtergebnis()
    try:
        if stand is not None:
            stand.laeuft = True
        for eintrag in liste:
            if stopp is not None and stopp.is_set():
                break
            ergebnis = Ergebnis(ordner=str(eintrag.pfad), jahr=eintrag.jahr)
            gesamt.teile.append(ergebnis)
            _ein_ordner(conn, datenordner, eintrag.pfad, ergebnis, automatisch=automatisch,
                        heute=heute, jetzt=jetzt, stopp=stopp)
            if ergebnis.abgebrochen:
                break
        return gesamt
    finally:
        if stand is not None:
            stand.fertig(gesamt)
        _LAUF.release()


def _ein_ordner(conn: sqlite3.Connection, datenordner: Path, ordner_: Path,
                ergebnis: Ergebnis, *, automatisch: bool, heute: date | None,
                jetzt: float | None, stopp: threading.Event | None) -> None:
    """Ein Ordner unter der schon gehaltenen Sperre -- samt Journaleintrag."""
    try:
        _abholen(conn, Path(datenordner), Path(ordner_), ergebnis,
                 automatisch=automatisch, heute=heute,
                 jetzt=time.time() if jetzt is None else jetzt, stopp=stopp)
    except Exception as fehler:  # noqa: BLE001 -- der Lauf darf nichts mitreissen
        traceback.print_exc(file=sys.stderr)
        _offene_transaktion_zuruecknehmen(conn)
        ergebnis.fehler = f"Das Abholen brach ab: {fehlertext(fehler)}"
    try:
        _protokolliere(conn, ergebnis, Path(ordner_))
    except Exception:  # noqa: BLE001 -- "wirft nichts" gilt auch hier
        traceback.print_exc(file=sys.stderr)


def _offene_transaktion_zuruecknehmen(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _nfc(name: str) -> str:
    """macOS liefert Dateinamen zerlegt (NFD): "ä" als "a" plus Trema. In der
    Datenbank und in der Oberflaeche soll derselbe Name gleich aussehen und
    sich gleich suchen lassen -- also zusammengesetzt (NFC).

    Ein Name, der kein gueltiges UTF-8 ist (Linux, eingebundene fremde
    Dateisysteme: ``b"M\\xfcller"`` in Latin-1), kommt mit Platzhaltern
    (surrogateescape) an, die sich nicht schreiben lassen -- weder in die
    Begleitdatei noch ins Journal. Er wird mit Ersatzzeichen angezeigt; die
    Datei selbst wird weiter unter ihrem echten Namen angefasst.

    >>> _nfc("M\\udcfcller.xml")
    'M\ufffdller.xml'
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        name = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return unicodedata.normalize("NFC", name)


def _reihenfolge(pfad: Path) -> tuple[int, str]:
    """XML zuerst, dann alphabetisch.

    Eine E-Rechnung kommt oft als XML MIT einer PDF-Sichtdatei. Maßgebend
    und aufzubewahren ist der strukturierte Teil (§ 14 UStG, BMF-Schreiben
    vom 15.10.2024). Alphabetisch kaeme "rechnung.pdf" vor "rechnung.xml":
    das PDF waere als Original gebucht und die E-Rechnung als "ähnlicher
    Beleg" zurueckgestellt.

    >>> sorted([Path("b.pdf"), Path("a.pdf"), Path("b.xml")], key=_reihenfolge)
    [PosixPath('b.xml'), PosixPath('a.pdf'), PosixPath('b.pdf')]
    """
    name = _nfc(pfad.name)
    return (0 if name.strip().lower().endswith(".xml") else 1, name)


#: Hier merkt sich das Werkzeug Dateien, die erledigt sind, sich aber im
#: Eingangsordner nicht verschieben liessen (geschuetzt, Ordner nur lesbar),
#: und Dateien, die sich nicht lesen lassen (keine Leserechte). Sie werden
#: nicht bei jedem Start erneut aufgenommen und gemeldet.
GEDAECHTNIS = ".belegordner-liegen-gelassen.json"

#: So steht eine unlesbare Datei im Gedaechtnis (statt eines Zielordners).
UNLESBAR = "unlesbar"


class _Gedaechtnis:
    """Erledigte und unlesbare Dateien, die noch im Eingangsordner liegen.

    Erkannt an Name, Groesse und Aenderungszeit -- ohne die Datei zu lesen --
    oder, wenn sie doch gelesen wurde, an der Pruefsumme. Bei jedem Lauf wird
    nur still versucht, eine erledigte doch noch zu verschieben und eine
    unlesbare doch noch zu lesen. Vergessen wird eine Datei, sobald sie
    verschoben oder lesbar ist, sich aendert oder nicht mehr im Ordner liegt.

    JE ORDNER: jeder Eintrag traegt seinen Ordner, und ein Lauf sieht und
    raeumt nur die Eintraege des Ordners, den er gerade abholt. Vorher loeschte
    der Lauf fuer Ordner B das Gedaechtnis von Ordner A ("liegt nicht mehr im
    Ordner"), und eine geschuetzte Datei in A wurde beim naechsten Lauf erneut
    gemeldet, samt Journaleintrag. Eintraege aus der Zeit davor (ohne Ordner)
    gehoeren zum Ordner fuer alle Jahre -- dem einzigen, den es damals gab --,
    und jeder Ordner, in dem eine solche Datei liegt, uebernimmt sie.
    """

    def __init__(self, datenordner: Path, ordner_: Path | None = None, *,
                 alte_eintraege: bool = True) -> None:
        self.pfad = datenordner / GEDAECHTNIS
        self.ordner = str(aufgeloest(ordner_)) if ordner_ is not None else None
        #: Ob Eintraege ohne Ordner (aus der Zeit vor den Jahresordnern) zu
        #: diesem Ordner zaehlen, auch beim Aufraeumen.
        self.alte_eintraege = alte_eintraege
        self.eintraege: list[dict[str, object]] = []
        self.geaendert = False
        try:
            import json

            daten = json.loads(self.pfad.read_text(encoding="utf-8"))
            self.eintraege = [e for e in daten.get("eintraege", []) if isinstance(e, dict)]
        except (OSError, ValueError, AttributeError):
            self.eintraege = []

    @staticmethod
    def _kennung(name: str, info: os.stat_result) -> tuple[str, int, int]:
        return name, info.st_size, info.st_mtime_ns

    def _meins(self, eintrag: dict[str, object], *, auch_alte: bool = True) -> bool:
        """Gehoert der Eintrag zu diesem Ordner?"""
        if self.ordner is None:
            return True
        dort = eintrag.get("ordner")
        if dort is None:
            return auch_alte
        return _textform(Path(str(dort))) == _textform(Path(self.ordner))

    def finde(self, name: str, info: os.stat_result, sha256: str | None = None
              ) -> dict[str, object] | None:
        kennung = self._kennung(name, info)
        for eintrag in self.eintraege:
            if not self._meins(eintrag):
                continue
            if ((eintrag.get("name"), eintrag.get("groesse"), eintrag.get("mtime_ns")) == kennung
                    or (sha256 is not None and eintrag.get("sha256") == sha256)):
                if eintrag.get("ordner") is None and self.ordner is not None:
                    # Ein alter Eintrag ohne Ordner: er gehoert dem, in dem die
                    # Datei liegt.
                    eintrag["ordner"] = self.ordner
                    self.geaendert = True
                return eintrag
        return None

    def merke(self, name: str, info: os.stat_result, sha256: str | None, ziel: str) -> None:
        self.vergiss(name)
        eintrag: dict[str, object] = {"name": name, "groesse": info.st_size,
                                      "mtime_ns": info.st_mtime_ns, "sha256": sha256,
                                      "ziel": ziel}
        if self.ordner is not None:
            eintrag["ordner"] = self.ordner
        self.eintraege.append(eintrag)
        self.geaendert = True

    def vergiss(self, name: str) -> None:
        vorher = len(self.eintraege)
        self.eintraege = [e for e in self.eintraege
                          if not (e.get("name") == name and self._meins(e))]
        self.geaendert |= len(self.eintraege) != vorher

    def nur_noch(self, namen: set[str]) -> None:
        """Vergisst, was nicht mehr in DIESEM Ordner liegt -- die Eintraege der
        anderen Ordner bleiben."""
        vorher = len(self.eintraege)
        self.eintraege = [e for e in self.eintraege
                          if not self._meins(e, auch_alte=self.alte_eintraege)
                          or e.get("name") in namen]
        self.geaendert |= len(self.eintraege) != vorher

    def speichern(self) -> None:
        if not self.geaendert:
            return
        import json

        try:
            if self.eintraege:
                teil = self.pfad.with_name(self.pfad.name + ".teil")
                teil.write_text(json.dumps({"eintraege": self.eintraege}, ensure_ascii=False),
                                encoding="utf-8")
                os.replace(teil, self.pfad)
            else:
                self.pfad.unlink(missing_ok=True)
        except OSError:
            pass  # dann wird die Datei beim naechsten Lauf noch einmal gemeldet


@dataclass(slots=True)
class _Lauf:
    """Was ein Abholvorgang fuer jede Datei braucht."""

    conn: sqlite3.Connection
    datenordner: Path
    ordner: Path
    ergebnis: Ergebnis
    automatisch: bool
    heute: date | None
    jetzt: float
    gedaechtnis: _Gedaechtnis
    #: Die Namen im Ordner, wie sie beim Auflisten dalagen.
    namen: frozenset[str] = frozenset()
    #: Liest die Textschicht der PDF in einem eigenen Prozess, mit Zeitgrenze.
    textdienst: textdienst.Textdienst | None = None
    #: Das Jahr des Ordners (``None``: fuer alle Jahre).
    jahr: int | None = None


def nicht_beschreibbar(ordner_: Path) -> str:
    """Die Meldung, wenn das Werkzeug im Eingangsordner nicht schreiben darf."""
    return (f"In den Eingangsordner {ordner_} kann das Tool nicht schreiben — dann ließen sich "
            f"abgeholte Dateien nicht nach „{UEBERNOMMEN}“ legen, und jeder Start nähme sie "
            "neu auf. Abgeholt wurde nichts. Bitte im Finder unter „Informationen“ → "
            "„Teilen & Zugriffsrechte“ Lesen & Schreiben erlauben, dann „Jetzt abholen“.")


def _abholen(
    conn: sqlite3.Connection, datenordner: Path, ordner_: Path, ergebnis: Ergebnis, *,
    automatisch: bool, heute: date | None, jetzt: float,
    stopp: threading.Event | None = None,
) -> None:
    grund = ordnerfehler(datenordner, ordner_,
                         sicherungen=sicherungsziele(conn, datenordner))
    if grund:
        ergebnis.fehler = grund
        return
    try:
        eintraege = sorted(ordner_.iterdir(), key=_reihenfolge)
    except FileNotFoundError:
        ergebnis.fehler = ordner_fehlt(ordner_)
        return
    except NotADirectoryError:
        ergebnis.fehler = (f"{ordner_} ist eine Datei, kein Ordner — unter „Belege“ einen "
                           "Ordner als Eingangsordner wählen.")
        return
    except PermissionError:
        ergebnis.fehler = kein_zugriff(ordner_)
        return
    except OSError as fehler:
        ergebnis.fehler = (f"Der Eingangsordner {ordner_} ließ sich nicht lesen: "
                           f"{fehlertext(fehler)}")
        return
    if not os.access(ordner_, os.W_OK):
        # Ohne Schreibrecht liesse sich keine Datei verschieben -- jeder Lauf
        # naehme dieselben Dateien neu auf, meldete und protokollierte sie, und
        # eine in der Pruefmaske weggeworfene kaeme wieder. Einmal sagen, was
        # los ist, und nichts tun.
        ergebnis.fehler = nicht_beschreibbar(ordner_)
        return

    lauf = _Lauf(conn, datenordner, ordner_, ergebnis, automatisch, heute, jetzt,
                 _Gedaechtnis(datenordner, ordner_, alte_eintraege=ergebnis.jahr is None),
                 frozenset(e.name for e in eintraege), textdienst.Textdienst(stopp=stopp),
                 ergebnis.jahr)
    try:
        for eintrag in eintraege:
            if stopp is not None and stopp.is_set():
                ergebnis.fehler = BEIM_BEENDEN
                break
            try:
                _eine_datei(lauf, eintrag)
            except textdienst.Abgebrochen:
                ergebnis.fehler = BEIM_BEENDEN
                break
            except DatenbankBelegt:
                _offene_transaktion_zuruecknehmen(conn)
                ergebnis.fehler = DATENBANK_BELEGT
                ergebnis.abgebrochen = True
                return
            except Exception as fehler:  # noqa: BLE001 -- eine Datei darf den Lauf nicht beenden
                traceback.print_exc(file=sys.stderr)
                _offene_transaktion_zuruecknehmen(conn)
                ergebnis.abgewiesen.append(Abgewiesen(
                    _nfc(eintrag.name),
                    f"Beim Abholen trat ein Fehler auf: {fehlertext(fehler)} Sie bleibt im "
                    "Ordner und kommt beim nächsten Abholen wieder dran.",
                    verschoben=False,
                ))
        lauf.gedaechtnis.nur_noch({_nfc(e.name) for e in eintraege})
    finally:
        lauf.gedaechtnis.speichern()
        lauf.textdienst.beenden()


def _eine_datei(lauf: _Lauf, eintrag: Path) -> None:
    datenordner, ergebnis = lauf.datenordner, lauf.ergebnis
    name = _nfc(eintrag.name)

    # --- Versteckt, Platzhalter, Systemdatei: nicht anfassen.
    if name.startswith("."):
        if name.endswith(ICLOUD_ENDUNG) and len(name) > len(ICLOUD_ENDUNG) + 1:
            _platzhalter(lauf, eintrag)
        return
    if name in SYSTEMDATEIEN or name.lower().endswith(ZWISCHENENDUNGEN):
        return
    if _datenbank_oder_sicherung(name):
        return  # nie anfassen -- wie immer der Ordner hierher geraten ist
    try:
        info = eintrag.stat()
    except FileNotFoundError:
        return  # gerade von einem anderen Lauf verschoben
    except OSError as fehler:
        ergebnis.abgewiesen.append(Abgewiesen(
            name, f"Ließ sich nicht lesen: {fehlertext(fehler)}", verschoben=False))
        return
    if not _stat.S_ISREG(info.st_mode):
        return  # Unterordner ("Übernommen" und alles andere) und Sonderdateien
    if getattr(info, "st_flags", 0) & getattr(_stat, "UF_HIDDEN", 0):
        return  # im Finder versteckt
    alter = lauf.jetzt - _zuletzt_geaendert(info)
    if -UHRENFEHLER_SEKUNDEN < alter < MINDESTALTER_SEKUNDEN:
        ergebnis.noch_im_schreiben.append(name)
        return

    # --- Schon erledigt, nur das Verschieben scheiterte: still noch einmal
    # versuchen, nicht noch einmal aufnehmen, melden oder protokollieren.
    # Eine gemerkte unlesbare Datei wird still noch einmal gelesen.
    gemerkt = lauf.gedaechtnis.finde(name, info)
    unlesbar_gemerkt = gemerkt is not None and gemerkt.get("ziel") == UNLESBAR
    if not unlesbar_gemerkt and _nachholen(lauf, eintrag, name, gemerkt):
        return

    # --- Kein Belegformat: gleich nach "Nicht übernommen", ohne zu lesen.
    # Getrimmt wie beim Upload: "Rechnung.pdf " ist ein PDF.
    endung = Path(name.strip()).suffix.lower()
    if endung not in pipeline.ERLAUBT:
        _abweisen(lauf, eintrag, name, info, None,
                  f"{endung or 'Eine Datei ohne Endung'} ist kein Belegformat. Möglich sind "
                  "PDF, XML einer E-Rechnung, Fotos und Scans.")
        return
    if info.st_size > pipeline.MAX_BYTES:
        _abweisen(lauf, eintrag, name, info, None, pipeline.zu_gross(info.st_size))
        return

    # --- Lesen und aufnehmen wie ein Upload.
    try:
        inhalt = eintrag.read_bytes()
    except FileNotFoundError:
        return  # gerade von einem anderen Lauf verschoben
    except OSError as fehler:
        # Dauerhaft unlesbar (keine Leserechte): wie eine geschuetzte Datei
        # EINMAL melden und merken. Vorher kam sie bei jedem Abholen wieder,
        # samt Journaleintrag je Lauf.
        if not unlesbar_gemerkt:
            lauf.gedaechtnis.merke(name, info, None, UNLESBAR)
            ergebnis.abgewiesen.append(Abgewiesen(name, _lesegrund(fehler), verschoben=False))
        return
    if unlesbar_gemerkt:
        lauf.gedaechtnis.vergiss(name)  # inzwischen lesbar: ganz normal weiter
    pruefsumme = hashlib.sha256(inhalt).hexdigest()
    if _nachholen(lauf, eintrag, name, lauf.gedaechtnis.finde(name, info, pruefsumme)):
        return
    sperre = _Inhaltssperre(datenordner, pruefsumme)
    if not sperre.nehmen():
        # Ein gleichzeitiger Lauf (Programm doppelt gestartet) entscheidet
        # gerade ueber genau diesen Inhalt: er verschiebt die Datei und meldet
        # sie. Hier gemeldet hiess sie "wartet schon in der Prüfmaske" -- und
        # war einen Augenblick spaeter gebucht.
        return
    try:
        _aufnehmen_und_entscheiden(lauf, eintrag, name, info, inhalt, pruefsumme, alter)
    finally:
        sperre.freigeben()


def _aufnehmen_und_entscheiden(lauf: _Lauf, eintrag: Path, name: str, info: os.stat_result,
                               inhalt: bytes, pruefsumme: str, alter: float) -> None:
    """Aufnehmen wie ein Upload, dann buchen, warten lassen oder als schon
    erfasst melden -- unter der Sperre fuer diesen Inhalt."""
    conn, datenordner, ergebnis = lauf.conn, lauf.datenordner, lauf.ergebnis
    try:
        eingang = pipeline.aufnehmen(inhalt, name, datenordner)
    except BelegFehler as fehler:
        _abweisen(lauf, eintrag, name, info, pruefsumme, str(fehler))
        return
    # Ob die Eingangskopie von DIESEM Lauf stammt, sagt aufnehmen() selbst --
    # eine Pruefung davor ginge an einem gleichzeitigen Prozess vorbei.
    lag_schon = not eingang.neu

    def uebernommen() -> bool | None:
        return _ablegen(lauf, eintrag, name, info, pruefsumme, UEBERNOMMEN)

    # --- Schon gebucht oder schon im Eingang: nichts doppelt.
    gebucht_als = pipeline.dublette(conn, pruefsumme)
    if gebucht_als is not None:
        if not lag_schon:
            # Die Kopie, die gerade erst entstanden ist, braucht niemand: der
            # Beleg liegt im Archiv. Eine schon vorher liegende bleibt -- die
            # hat der Nutzer im Eingang gesehen und entscheidet selbst.
            _eingangskopie_weg(eingang)
        if uebernommen() is None:
            return  # ein gleichzeitiger Lauf hat sie gebucht, verschoben und gemeldet
        ergebnis.schon_vorhanden.append(SchonVorhanden(name, gebucht_als))
        return
    if lag_schon:
        _lag_schon_im_eingang(lauf, eintrag, name, info, pruefsumme, eingang)
        return
    if not lauf.automatisch:
        uebernommen()
        ergebnis.wartend.append(Wartend(eingang.pfad.name, name,
                                        "automatisches Buchen ist ausgeschaltet"))
        return

    # --- Automatisch buchen. Die Quelle bleibt im Ordner, bis entschieden
    # ist: gebucht oder wartend. Endet der Prozess mitten im Buchen (Fenster
    # geschlossen, zweiter Start, kill), hat buche() die Eingangsdatei schon
    # ins Archiv gelegt, die Transaktion aber nie festgeschrieben -- laege die
    # Quelle dann schon in "Übernommen", waere die Rechnung unsichtbar: weder
    # gebucht noch im Eingang. So liegt sie beim naechsten Abholen noch da und
    # wird erneut gebucht; buche() findet die Archivdatei und legt sie nicht
    # doppelt ab.
    entschieden: list[Entscheidung] = []
    try:
        beleg_id, grund = automatisch_buchen(conn, eingang, datenordner, heute=lauf.heute,
                                             textleser=lauf.textdienst.text,
                                             entscheidung_aus=entschieden)
    except (DatenbankBelegt, textdienst.Abgebrochen):
        # Nichts entschieden: die Kopie dieses Laufs wieder weg, die Quelle
        # bleibt liegen -- der naechste Lauf bucht sie automatisch, statt sie
        # fuer immer in der Pruefmaske warten zu lassen.
        if not lag_schon:
            _eingangskopie_weg(eingang)
        raise
    except Exception as fehler:  # noqa: BLE001 -- die Datei liegt sicher im Eingang
        traceback.print_exc(file=sys.stderr)
        _offene_transaktion_zuruecknehmen(conn)
        beleg_id = None
        grund = f"Beim automatischen Buchen trat ein Fehler auf: {fehlertext(fehler)}"
    if beleg_id is not None:
        uebernommen()
        zeile = conn.execute(
            "SELECT b.betrag_brutto_cent, b.belegdatum, l.name FROM beleg b"
            " LEFT JOIN lieferant l ON l.id = b.lieferant_id WHERE b.id = ?", (beleg_id,),
        ).fetchone()
        ergebnis.gebucht.append(Gebucht(beleg_id, name, zeile["name"],
                                        zeile["betrag_brutto_cent"]))
        _jahr_pruefen(lauf, name, zeile["belegdatum"])
        return
    # Ein anderer Prozess hat dieselbe Datei inzwischen gebucht? Dann ist sie
    # schon erfasst -- und die Kopie, die dieser Lauf angelegt hat, waere ein
    # Doppel im Eingang.
    schon = pipeline.dublette(conn, pruefsumme)
    if schon is not None:
        if not lag_schon:
            _eingangskopie_weg(eingang)
        uebernommen()
        ergebnis.schon_vorhanden.append(SchonVorhanden(name, schon))
        return
    if (grund or "").startswith(NICHT_LESBAR) and (
            -UHRENFEHLER_SEKUNDEN < alter < UNLESBAR_MINDESTALTER_SEKUNDEN
            or _veraendert(eintrag, info)):
        # Unlesbar und eben erst geschrieben (oder seither veraendert): ein
        # Kopiervorgang, der stockt, hat die Datei vermutlich erst halb
        # abgelegt. Nicht abgeschnitten in den Eingang -- liegen lassen, beim
        # naechsten Abholen ist sie vollstaendig.
        if not lag_schon:
            _eingangskopie_weg(eingang)
        ergebnis.noch_im_schreiben.append(name)
        return
    uebernommen()
    ergebnis.wartend.append(Wartend(eingang.pfad.name, name,
                                    grund or "bitte in der Prüfmaske bestätigen"))
    if entschieden:
        _jahr_pruefen(lauf, name, entschieden[0].belegdatum)


def _jahr_pruefen(lauf: _Lauf, name: str, belegdatum: str | None) -> None:
    """Passt das Belegdatum zum Jahr des Ordners? Sonst ein Hinweis -- NUR ein
    Hinweis: eine Dezember-Rechnung, im Januar bezahlt und in den Ordner des
    neuen Jahres gelegt, ist der Normalfall (fuer die EUeR zaehlt nach § 11
    EStG der Tag der Zahlung). Gebucht und entschieden wird unabhaengig vom
    Ordner."""
    if lauf.jahr is None or not belegdatum or len(belegdatum) < 10:
        return
    if belegdatum[:4] == str(lauf.jahr):
        return
    tag = f"{belegdatum[8:10]}.{belegdatum[5:7]}.{belegdatum[:4]}"
    lauf.ergebnis.jahr_abweichend.append(
        f"„{name}“: Belegdatum {tag}, lag im Ordner {lauf.jahr} — stimmt so, wenn sie "
        f"{belegdatum[:4]} bezahlt wurde; sonst den Zahltag prüfen (für die EÜR zählt der Tag "
        "der Zahlung, § 11 EStG) oder die Datei bei dir umsortieren.")


class _Inhaltssperre:
    """Haelt einen Inhalt (Pruefsumme) fuer einen Lauf, bis ueber ihn entschieden ist.

    Zwei Prozesse (Programm doppelt gestartet) teilen ``_LAUF`` nicht. Ohne
    diese Sperre las der zweite eine Datei, deren Eingangskopie der erste
    gerade angelegt hatte und noch auslas -- und meldete sie als "wartet
    schon in der Prüfmaske", obwohl der erste sie gleich darauf buchte. Die
    Sperre ist ``flock`` auf einer versteckten Datei im Eingang
    (".<hash>.in-arbeit"); sie endet mit dem Prozess, auch bei einem Absturz.
    Wer sie nicht bekommt, laesst die Datei liegen: der andere Lauf
    verschiebt und meldet sie, und stirbt er vorher, kommt sie beim naechsten
    Abholen dran. Ohne ``flock`` (fremdes Dateisystem) gilt sie als frei.
    """

    def __init__(self, datenordner: Path, pruefsumme: str) -> None:
        self.pfad = datenordner / pipeline.EINGANG / f".{pruefsumme[:16]}.in-arbeit"
        self._fd: int | None = None

    def nehmen(self) -> bool:
        """True: dieser Lauf hat den Inhalt. False: ein anderer arbeitet daran."""
        try:
            import fcntl
        except ImportError:  # nur macOS und Linux haben flock
            return True
        try:
            self.pfad.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return True  # dann scheitert gleich das Aufnehmen mit eigenem Grund
        for _ in range(5):
            try:
                fd = os.open(self.pfad, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                return True
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            except OSError:
                os.close(fd)
                _weg(self.pfad)  # flock geht hier nicht -- keine Sperrdatei zuruecklassen
                return True
            # Der Vorgaenger raeumt seine Sperrdatei weg, waehrend er sie
            # haelt. Gehoert die gesperrte Datei nicht mehr zum Namen, gilt
            # sie nichts: noch einmal mit der Datei, die jetzt dort liegt.
            try:
                if os.path.samestat(os.fstat(fd), os.stat(self.pfad)):
                    self._fd = fd
                    return True
            except OSError:
                pass
            os.close(fd)
        return True

    def freigeben(self) -> None:
        if self._fd is None:
            return
        _weg(self.pfad)
        os.close(self._fd)
        self._fd = None


def _lag_schon_im_eingang(lauf: _Lauf, eintrag: Path, name: str, info: os.stat_result,
                          pruefsumme: str, eingang: pipeline.Eingang) -> None:
    """Die Eingangskopie stammt nicht von diesem Lauf: ein Upload, ein frueherer
    Lauf -- oder ein zweiter Prozess, der dieselbe Datei gerade bucht.

    Gemeldet wird erst nach einem zweiten Blick, nachdem die Quelle verschoben
    ist: inzwischen gebucht heisst "schon gebucht als Beleg N"; nicht mehr im
    Eingang (gebucht, weggeworfen), heisst: nicht melden. Vorher hiess eine
    Datei, die der andere Prozess Augenblicke spaeter buchte, "wartet schon in
    der Prüfmaske". Hat der andere Lauf die Quelle schon verschoben, meldet er
    sie selbst.
    """
    if _ablegen(lauf, eintrag, name, info, pruefsumme, UEBERNOMMEN) is None:
        return
    gebucht_als = _gebucht_nach_laufender_buchung(lauf.conn, pruefsumme)
    if gebucht_als is not None:
        lauf.ergebnis.schon_vorhanden.append(SchonVorhanden(name, gebucht_als))
    elif eingang.pfad.exists():
        lauf.ergebnis.schon_vorhanden.append(SchonVorhanden(name, None, eingang.pfad.name))


def _gebucht_nach_laufender_buchung(conn: sqlite3.Connection, pruefsumme: str) -> int | None:
    """``pipeline.dublette`` -- erst, wenn eine laufende Buchung fertig ist.

    ``buche`` legt die Eingangsdatei unter seiner Sperre ins Archiv und
    schreibt danach fest. Wer genau dazwischen nachsieht, faende die Datei
    weder gebucht noch im Eingang. ``BEGIN IMMEDIATE`` wartet, bis die Sperre
    frei ist; bleibt sie belegt, gilt der Stand ohne Warten.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as fehler:
        if not _gesperrt(fehler):
            raise
        return pipeline.dublette(conn, pruefsumme)
    try:
        return pipeline.dublette(conn, pruefsumme)
    finally:
        conn.execute("COMMIT")


def _platzhalter(lauf: _Lauf, eintrag: Path) -> None:
    """Ein iCloud-Platzhalter ".Rechnung.pdf.icloud": noch nicht geladen.

    Gemeldet und zum Laden angestossen wird er nur, wenn daraus ein Beleg
    wird: Liegt die echte Datei schon daneben (iCloud hat sie geladen, der
    Platzhalter ist ein Rest), zaehlt er nicht -- sonst hiesse dieselbe
    Datei zugleich "gebucht" und "lädt noch aus iCloud". Und ein
    Platzhalter fuer eine .docx ist keine ausstehende Rechnung.
    """
    echt_roh = eintrag.name[1:-len(ICLOUD_ENDUNG)]
    echt = _nfc(echt_roh)
    if Path(echt.strip()).suffix.lower() not in pipeline.ERLAUBT:
        return
    if echt_roh in lauf.namen or echt in {_nfc(n) for n in lauf.namen}:
        return
    lauf.ergebnis.icloud_ausstehend.append(echt)
    _icloud_laden(eintrag.with_name(echt_roh))


def _zuletzt_geaendert(info: os.stat_result) -> float:
    """Die juengere von Aenderungszeit (mtime) und Statuszeit (ctime).

    Die mtime laesst sich setzen -- der Finder und ``cp -p`` uebernehmen sie
    vom Original, noch waehrend sie schreiben. Die ctime setzt das System bei
    jedem Schreiben selbst.
    """
    return max(info.st_mtime, info.st_ctime)


def _veraendert(eintrag: Path, info: os.stat_result) -> bool:
    """Ist das nicht mehr die Datei, die gelesen wurde (anderes Inode, andere
    Groesse oder Aenderungszeit)?"""
    try:
        jetzt = eintrag.stat()
    except OSError:
        return True
    return ((jetzt.st_dev, jetzt.st_ino, jetzt.st_size, jetzt.st_mtime_ns)
            != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))


def _nachholen(lauf: _Lauf, eintrag: Path, name: str, gemerkt: dict[str, object] | None
               ) -> bool:
    """Eine gemerkte, schon erledigte Datei: still noch einmal verschieben.

    True heisst: erledigt, nichts weiter tun (auch wenn es wieder scheitert).
    """
    if gemerkt is None:
        return False
    ziel = NICHT_UEBERNOMMEN if gemerkt.get("ziel") == NICHT_UEBERNOMMEN else UEBERNOMMEN
    if not isinstance(_verschiebe(eintrag, lauf.ordner / ziel, melden=False), OSError):
        lauf.gedaechtnis.vergiss(name)
    return True


def _ablegen(lauf: _Lauf, eintrag: Path, name: str, info: os.stat_result,
             pruefsumme: str | None, unterordner: str) -> bool | None:
    """Verschiebt die erledigte Datei in den Unterordner.

    Scheitert das, wird sie gemerkt (nicht bei jedem Start erneut aufnehmen
    und melden) und EINMAL mit Grund gemeldet. True: verschoben, None: schon
    weg, False: liegt noch da.
    """
    if _veraendert(eintrag, info):
        # Seit dem Lesen ersetzt oder weitergeschrieben (die Dateien-App
        # ersetzt eine gleichnamige Datei): verschoben wuerde ein Inhalt, den
        # niemand gelesen hat. Liegen lassen -- der naechste Lauf nimmt ihn auf.
        if eintrag.exists():
            lauf.ergebnis.noch_im_schreiben.append(name)
            return False
        return None
    erfolg = _verschiebe(eintrag, lauf.ordner / unterordner)
    if not isinstance(erfolg, OSError):
        return erfolg
    lauf.gedaechtnis.merke(name, info, pruefsumme, unterordner)
    lauf.ergebnis.nicht_verschoben.append(NichtVerschoben(name, _verschiebegrund(erfolg)))
    return False


def _lesegrund(fehler: OSError) -> str:
    """Warum eine Datei nicht gelesen wurde -- und dass das nur einmal kommt."""
    import errno

    danach = ("Sie bleibt im Ordner und wird abgeholt, sobald sie sich lesen lässt; "
              "gemeldet wird das nur dieses eine Mal.")
    if fehler.errno in (errno.EPERM, errno.EACCES):
        return ("Ließ sich nicht lesen: keine Berechtigung — im Finder unter Ablage → "
                "Informationen → „Teilen & Zugriffsrechte“ Lesen erlauben. " + danach)
    return f"Ließ sich nicht lesen: {fehlertext(fehler)} {danach}"


def _verschiebegrund(fehler: OSError) -> str:
    """Warum eine Datei liegen blieb -- und was der Nutzer tun kann."""
    import errno

    if fehler.errno in (errno.EPERM, errno.EACCES):
        return ("keine Berechtigung, sie zu verschieben — ist sie geschützt? Im Finder: "
                "Ablage → Informationen → „Geschützt“ ausschalten, oder sie von Hand nach "
                f"„{UEBERNOMMEN}“ legen. Sie wird nicht noch einmal aufgenommen.")
    return (f"{fehlertext(fehler)} Bitte von Hand nach „{UEBERNOMMEN}“ legen; sie wird nicht "
            "noch einmal aufgenommen.")


def _eingangskopie_weg(eingang: pipeline.Eingang) -> None:
    """Die Eingangskopie samt Namens-Begleitdatei entfernen."""
    _weg(eingang.pfad)
    pipeline.vergiss_original_name(eingang.pfad)


def _abweisen(lauf: _Lauf, eintrag: Path, name: str, info: os.stat_result,
              pruefsumme: str | None, grund: str) -> None:
    """Nach "Nicht übernommen" -- damit sie nicht bei jedem Start wieder kommt."""
    verschoben = _ablegen(lauf, eintrag, name, info, pruefsumme, NICHT_UEBERNOMMEN)
    if verschoben is None:
        return  # ein anderer Lauf hat sie schon
    lauf.ergebnis.abgewiesen.append(Abgewiesen(name, grund, verschoben=verschoben))


def _datenbank_oder_sicherung(name: str) -> bool:
    """Die Datenbank dieses Werkzeugs (samt -wal/-shm) oder eines seiner
    Sicherungsarchive. ``ordnerfehler`` haelt den Datenordner und die
    Sicherungsziele fern; liegt so eine Datei trotzdem im Eingangsordner
    (von Hand kopiert, ein Ordner unter zweitem Namen), bleibt sie, wo sie ist.

    >>> [_datenbank_oder_sicherung(n) for n in ("steuer.sqlite-wal", "Steuer.SQLite",
    ...  "2026-10-24_steuer-backup.zip", "Rechnung.pdf")]
    [True, True, True, False]
    """
    klein = name.lower()
    return (klein.startswith(DB_IM_ARCHIV)
            or fnmatch.fnmatch(klein, ARCHIVMUSTER) or fnmatch.fnmatch(klein, ARCHIVMUSTER + ".*"))


#: So viele Bytes darf ein Dateiname haben (APFS, ext4: 255). In NFD -- so
#: liefert macOS die Namen -- belegt ein Umlaut drei davon.
NAME_MAX_BYTES = 255


def _gekuerzt(text: str, hoechstens: int) -> str:
    """``text`` ohne seine letzten Zeichen, bis er in ``hoechstens`` Bytes passt.

    >>> _gekuerzt("Rechnung", 4), _gekuerzt("ää", 3), _gekuerzt("abc", 10)
    ('Rech', 'ä', 'abc')
    """
    while text and len(os.fsencode(text)) > hoechstens:
        text = text[:-1]
    return text


def freier_name(zielordner: Path, name: str) -> Path:
    """Ein Name, der im Zielordner noch frei ist: "Rechnung (2).pdf" usw.

    Vom Handy kommt fast jede Rechnung als "Rechnung.pdf" -- ueberschrieben
    werden darf im "Übernommen" trotzdem keine. Der Zaehler kuerzt den Stamm,
    wenn der Name sonst ueber die 255 Bytes eines Dateinamens kaeme: ein
    langer Name scheiterte erst beim Verschieben, und die Datei blieb fuer
    immer liegen.
    """
    ziel = zielordner / name
    if not ziel.exists():
        return ziel
    stamm, endung = Path(name).stem, Path(name).suffix
    for zaehler in range(2, 10_000):
        zusatz = f" ({zaehler}){endung}"
        kurz = _gekuerzt(stamm, NAME_MAX_BYTES - len(os.fsencode(zusatz)))
        ziel = zielordner / f"{kurz}{zusatz}"
        if not ziel.exists():
            return ziel
    raise FileExistsError(f"Im Ordner {zielordner} ist kein freier Name für {name} mehr.")


def _verschiebe(quelle: Path, zielordner: Path, *, melden: bool = True
                ) -> bool | None | OSError:
    """Verschiebt ``quelle`` kollisionsfrei in ``zielordner`` -- NUR umbenennen.

    True: verschoben. Ein ``OSError``: es ging nicht, sie liegt noch da (der
    Fehler sagt, warum). None: sie ist schon weg -- ein anderer Lauf war
    schneller.

    Kein ``shutil.move``: das faellt bei JEDEM Fehler des Umbenennens auf
    Kopieren und Loeschen zurueck. Ist die Datei geschuetzt (Finder:
    "Geschützt", chflags uchg) oder der Ordner nur lesbar, gelingt die Kopie
    und das Loeschen nicht -- und jeder Lauf legte eine weitere Kopie nach
    "Übernommen". "Übernommen" liegt im selben Ordner, also auf demselben
    Laufwerk: ein Umbenennen genuegt immer, wenn es ueberhaupt geht.

    Der Zielname wird erst exklusiv angelegt (``O_EXCL``) und dann per
    ``os.replace`` ueberschrieben: zwei gleichzeitige Laeufe koennen so nicht
    zwei Dateien unter denselben Namen legen. Scheitert das Umbenennen, wird
    der reservierte Name wieder entfernt -- zurueck bleibt nichts.
    """
    try:
        zielordner.mkdir(exist_ok=True)
    except FileNotFoundError as fehler:
        return None if not quelle.exists() else fehler
    except OSError as fehler:
        if melden:
            _verschieben_gescheitert(quelle, zielordner, fehler)
        return fehler
    for _ in range(5):
        try:
            ziel = freier_name(zielordner, quelle.name)
            reserviert = os.open(ziel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue  # ein gleichzeitiger Lauf hat denselben Namen genommen
        except OSError as fehler:
            if melden:
                _verschieben_gescheitert(quelle, zielordner, fehler)
            return fehler
        os.close(reserviert)
        try:
            os.replace(quelle, ziel)
        except FileNotFoundError as fehler:
            _weg(ziel)
            return None if not quelle.exists() else fehler
        except OSError as fehler:
            _weg(ziel)
            if melden:
                _verschieben_gescheitert(quelle, zielordner, fehler)
            return fehler
        return True
    import errno

    return FileExistsError(errno.EEXIST, "kein freier Name", str(zielordner))


def _weg(pfad: Path) -> None:
    try:
        pfad.unlink(missing_ok=True)
    except OSError:
        pass


def _verschieben_gescheitert(quelle: Path, zielordner: Path, fehler: OSError) -> None:
    print(f"Eingangsordner: {quelle} ließ sich nicht nach {zielordner} verschieben: "
          f"{fehlertext(fehler)}", file=sys.stderr)


def _icloud_laden(pfad: Path) -> None:
    """Bittet iCloud Drive, eine noch nicht geladene Datei zu laden.

    ``brctl download`` stoesst den Download an; die Datei kommt dann beim
    naechsten Abholen dran. Gestartet wird es, ohne darauf zu warten: ein
    haengendes brctl hielt sonst je Platzhalter zehn Sekunden lang den Lauf,
    die Sperre und "Jetzt abholen" fest. Fehlt das Programm oder scheitert
    es, ist das kein Fehler -- iCloud laedt die Datei ohnehin irgendwann.
    """
    if not _ist_mac():
        return
    brctl = shutil.which("brctl")
    if brctl is None:
        return
    try:
        subprocess.Popen([brctl, "download", str(pfad)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def _kurz(text: str, laenge: int = 60) -> str:
    """Ein Name fuer das Journal, hoechstens ``laenge`` Zeichen.

    >>> _kurz("a" * 70)[-3:], len(_kurz("a" * 70))
    ('aa…', 60)
    """
    return text if len(text) <= laenge else text[: laenge - 1] + "…"


def _protokolliere(conn: sqlite3.Connection, ergebnis: Ergebnis, ordner_: Path) -> None:
    """Ein Journaleintrag je Lauf, wenn etwas passiert ist.

    Die einzelnen Buchungen stehen ausserdem als BELEG_ERFASSEN im Journal;
    dieser Eintrag haelt fest, was mit den uebrigen Dateien geschah -- auch
    mit denen, die nach "Nicht übernommen" gewandert sind.

    Ist die Datenbank belegt, wird der Eintrag nachgeholt: beim naechsten
    Lauf dieses Prozesses, mit dem Zeitpunkt des Laufs, zu dem er gehoert.
    Hat der Lauf wegen der Sperre aufgehoert, wird es gar nicht erst versucht
    -- das hiesse nur, noch einmal fuenf Sekunden zu warten. Wirft nichts.

    Hat der Lauf nichts bewegt und meldet er genau dasselbe wie der letzte
    (eine Datei, die bei jedem Abholen mit demselben Fehler liegen bleibt),
    entfaellt der Eintrag: das Journal bekam sonst je Lauf denselben,
    unloeschbaren Eintrag.
    """
    eintraege = list(_NACHZUHOLEN)
    if ergebnis.veraendert():
        eintrag = _laufeintrag(ergebnis, ordner_)
        if not (_nichts_bewegt(ergebnis) and _wie_zuletzt(conn, eintrag)):
            eintraege.append(eintrag)
    if not eintraege:
        return
    if ergebnis.abgebrochen:
        _NACHZUHOLEN[:] = eintraege
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for eintrag in eintraege:
                db.protokolliere(conn, "BELEGORDNER_ABGEHOLT", "beleg", None,
                                 wert_neu=eintrag["wert_neu"],
                                 begruendung=eintrag["begruendung"])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except Exception as fehler:  # noqa: BLE001 -- der Lauf ist fertig, der Eintrag kommt nach
        _NACHZUHOLEN[:] = eintraege
        zusatz = (f"Der Protokolleintrag ließ sich nicht schreiben ({fehlertext(fehler)}); "
                  "er wird beim nächsten Abholen nachgeholt.")
        ergebnis.fehler = f"{ergebnis.fehler} {zusatz}" if ergebnis.fehler else zusatz
    else:
        _NACHZUHOLEN.clear()


def _nichts_bewegt(ergebnis: Ergebnis) -> bool:
    """Nichts gebucht, aufgenommen, verschoben oder neu liegen gelassen -- nur
    Dateien, die an ihrem Platz blieben (``Abgewiesen`` mit ``verschoben=False``)."""
    return (not (ergebnis.gebucht or ergebnis.wartend or ergebnis.schon_vorhanden
                 or ergebnis.nicht_verschoben)
            and not any(a.verschoben for a in ergebnis.abgewiesen))


def _wie_zuletzt(conn: sqlite3.Connection, eintrag: dict[str, object]) -> bool:
    """Steht derselbe Laufeintrag (ohne Zeitpunkt) schon als letzter DIESES
    Ordners im Journal -- oder wartet er als letzter darauf, nachgeholt zu
    werden?

    Je Ordner: verglichen wurde vorher mit dem letzten Laufeintrag irgendeines
    Ordners. Wechselten sich zwei Ordner ab, griff die Unterdrueckung nie, und
    jeder Lauf schrieb denselben unloeschbaren Eintrag noch einmal.
    """
    import json

    def ohne_zeit(werte: object) -> object:
        return {k: v for k, v in werte.items() if k != "lauf"} if isinstance(werte, dict) else werte

    ordner_ = eintrag["wert_neu"].get("ordner") if isinstance(eintrag["wert_neu"], dict) else None
    wartend = [e for e in _NACHZUHOLEN
               if isinstance(e["wert_neu"], dict) and e["wert_neu"].get("ordner") == ordner_]
    if wartend:
        letzter = wartend[-1]
        werte, begruendung = letzter["wert_neu"], letzter["begruendung"]
    else:
        try:
            zeile = conn.execute(
                "SELECT wert_neu, begruendung FROM journal WHERE aktion = 'BELEGORDNER_ABGEHOLT'"
                " AND json_valid(wert_neu) AND json_extract(wert_neu, '$.ordner') = ?"
                " ORDER BY id DESC LIMIT 1", (ordner_,)).fetchone()
            if zeile is None:
                return False
            werte, begruendung = json.loads(zeile["wert_neu"]), zeile["begruendung"]
        except (sqlite3.Error, ValueError, TypeError):
            return False
    return (begruendung == eintrag["begruendung"]
            and ohne_zeit(werte) == ohne_zeit(eintrag["wert_neu"]))


def _laufeintrag(ergebnis: Ergebnis, ordner_: Path) -> dict[str, object]:
    """Was BELEGORDNER_ABGEHOLT fuer diesen Lauf festhaelt."""
    zeilen = (
        [f"gebucht: {_kurz(g.originalname)} (Beleg {g.beleg_id})" for g in ergebnis.gebucht]
        + [f"wartet: {_kurz(w.originalname)} ({_kurz(w.grund, 120)})" for w in ergebnis.wartend]
        + [(f"schon gebucht: {_kurz(s.originalname)} (Beleg {s.beleg_id})" if s.beleg_id
            else f"wartet schon im Eingang: {_kurz(s.originalname)}")
           for s in ergebnis.schon_vorhanden]
        + [f"nicht übernommen: {_kurz(a.originalname)} ({_kurz(a.grund, 120)})"
           for a in ergebnis.abgewiesen]
        + [f"liegt noch im Ordner: {_kurz(n.originalname)} ({_kurz(n.grund, 120)})"
           for n in ergebnis.nicht_verschoben]
    )
    begruendung = "; ".join(zeilen)
    if len(begruendung) > 4000:
        begruendung = begruendung[:3999] + "…"
    return {
        "wert_neu": {"ordner": str(ordner_), "gebucht": len(ergebnis.gebucht),
                     "wartend": len(ergebnis.wartend),
                     "schon_vorhanden": len(ergebnis.schon_vorhanden),
                     "abgewiesen": len(ergebnis.abgewiesen),
                     "lauf": ergebnis.zeitpunkt,
                     **({"jahr": ergebnis.jahr} if ergebnis.jahr is not None else {})},
        "begruendung": begruendung,
    }


# --------------------------------------------------------- Automatisch buchen

def _als_e_rechnung_gebucht(conn: sqlite3.Connection, aehnliche) -> int | None:
    """Der erste der aehnlichen Belege, dessen Original ein XML ist."""
    for beleg in aehnliche:
        zeile = conn.execute(
            "SELECT 1 FROM belegdatei WHERE beleg_id = ? AND rolle = 'ORIGINAL'"
            " AND (mime = 'application/xml' OR LOWER(pfad) LIKE '%.xml')", (beleg["id"],),
        ).fetchone()
        if zeile is not None:
            return beleg["id"]
    return None


def _idnr_kompakt(text: object) -> str:
    return str(text or "").replace(" ", "").replace(".", "").upper()


def _idnr_widerspruch(lieferant: sqlite3.Row, im_beleg: object, *, aus_text: bool) -> str | None:
    """Passt die USt-IdNr. im Beleg zum zugeordneten Lieferanten?

    Zugeordnet wird zuerst ueber die USt-IdNr., sonst ueber den Namen -- und
    der ist bei einem PDF nur die erste Zeile mit einer Rechtsform. Steht im
    Kopf einer Designrechnung die Bankverbindung "Commerzbank AG", waere die
    Rechnung sonst als Bankgebuehr der Commerzbank gebucht. Die USt-IdNr. des
    Ausstellers ist dagegen eindeutig: nennt der Beleg eine andere als die
    gespeicherte, ist die Zuordnung falsch.

    Ist beim Lieferanten keine gespeichert, laesst sich bei einem PDF nicht
    pruefen, ob die Nummer im Beleg zu ihm gehoert -- dann wartet der Beleg
    ebenfalls. Aus dem XML einer E-Rechnung stammen Name und Nummer aus
    demselben Datensatz des Verkaeufers; dort genuegt der Name.

    >>> zeile = {"name": "Commerzbank AG", "ust_idnr": "DE114103379"}
    >>> _idnr_widerspruch(zeile, "DE 114 103 379", aus_text=True) is None
    True
    >>> _idnr_widerspruch(zeile, "DE287654321", aus_text=True)[:40]
    'USt-IdNr. im Beleg (DE287654321) passt n'
    >>> _idnr_widerspruch({"name": "X", "ust_idnr": None}, "DE287654321", aus_text=False)
    """
    beleg = _idnr_kompakt(im_beleg)
    if not beleg:
        return None
    gespeichert = _idnr_kompakt(lieferant["ust_idnr"])
    if gespeichert:
        if gespeichert != beleg:
            return (f"USt-IdNr. im Beleg ({beleg}) passt nicht zu {lieferant['name']} "
                    f"({gespeichert}) — vermutlich ein anderer Aussteller, bitte in der "
                    "Prüfmaske den Lieferanten prüfen")
        return None
    if aus_text:
        return (f"USt-IdNr. im Beleg ({beleg}) ist bei {lieferant['name']} nicht hinterlegt "
                "— ob der Beleg von diesem Lieferanten ist, lässt sich nicht prüfen; bitte in "
                "der Prüfmaske bestätigen")
    return None


def _waehrung_sicher(fund: Fund | None, lieferant: sqlite3.Row | None, waehrung: str) -> bool:
    """Ob eine Fremdwaehrung ohne Rueckfrage umgerechnet werden darf.

    Sicher ist sie aus dem XML einer E-Rechnung (``Fund.sicher``) oder als
    gemerkte Vorgabe des Lieferanten -- wenn der Beleg keine nennt oder
    dieselbe. Die Textschicht raet sie nur aus Waehrungszeichen. Ein neuer
    Lieferant hat keine Vorgabe.
    """
    if fund is not None and fund.sicher:
        return True
    if lieferant is None:
        return False
    vorgabe = (lieferant["waehrung_default"] or "EUR").upper()
    if vorgabe == "EUR" or vorgabe != waehrung:
        return False
    return fund is None or pipeline._waehrung_oder_eur(fund.wert) == vorgabe


#: Die Modi von :func:`pruefe_automatisch`.
ORDNER = "ordner"
SAMMEL = "sammel"

#: "Die Kategorie, die der Lieferant vorgibt" -- im Unterschied zu ``None``
#: (ausdruecklich ohne Kategorie, nur bei "Alle prüfen und buchen").
VORGABE = "vorgabe"


@dataclass(slots=True)
class Entscheidung:
    """Was ein automatischer Weg mit einer Eingangsdatei tut -- noch ohne zu
    schreiben. ``grund`` gesetzt: sie wartet auf die Pruefmaske, und der Grund
    sagt warum. Sonst steht hier alles, was ``pipeline.buche`` braucht, und
    fuer die Vorschau von "Alle prüfen und buchen", was gebucht wuerde."""

    grund: str | None = None
    lesung: pipeline.Lesung | None = None
    bestaetigt: dict[str, object] = field(default_factory=dict)
    #: Ein bekannter Lieferant -- oder (Name, USt-IdNr.) eines neuen, den
    #: ``buche`` in derselben Transaktion wie den Beleg anlegt.
    lieferant_id: int | None = None
    lieferant_neu: tuple[str, str | None] | None = None
    lieferant: str | None = None
    kategorie_id: int | None = None
    kategorie: str | None = None
    #: Woher die Kategorie kommt: "Vorgabe des Lieferanten" oder "gewählt".
    kategorie_herkunft: str | None = None
    quote: int = 100
    waehrung: str = "EUR"
    #: Der Betrag, wie er auf dem Beleg steht (bei Fremdwaehrung in ihr).
    brutto_cent: int | None = None
    #: Bei Fremdwaehrung: der Euro-Betrag zum EZB-Kurs.
    euro_cent: int | None = None
    #: Gesetzt, wenn die Zahlung mitgebucht wird (Zahltag = Belegdatum).
    zahltag: str | None = None
    #: Das Jahr des Zahltags, wenn es vor dem laufenden liegt (nur bei
    #: "Alle prüfen und buchen" -- das Abholen bucht dann nicht): Vorschau
    #: und Ergebnis warnen, die EUeR dieses Jahres ist evtl. schon abgegeben.
    vorjahr: int | None = None
    zahlungsart: str = "UEBERWEISUNG"
    #: Genau diese Datei ist schon als Beleg gebucht (Pruefsumme).
    dublette_von: int | None = None

    @property
    def buchbar(self) -> bool:
        return self.grund is None

    @property
    def belegdatum(self) -> str | None:
        wert = self.bestaetigt.get("belegdatum")
        if wert:
            return str(wert)
        fund = self.lesung.vorschlag.felder.get("belegdatum") if self.lesung else None
        return str(fund.wert) if fund is not None and fund.wert else None


def _sperre_der_kategorie(kategorie: sqlite3.Row | None, *, vorgabe_von: str | None
                          ) -> str | None:
    """Warum mit dieser Kategorie nicht ohne Pruefmaske gebucht wird -- oder
    ``None``. Fuer die Vorgabe eines Lieferanten wie fuer eine in der
    Vorschau gewaehlte Kategorie dieselben Sperren."""
    if kategorie is None or kategorie["bereich"] != kategorien.BEREICH_AUSGABEN \
            or not kategorie["aktiv"]:
        if vorgabe_von:
            return (f"die für {vorgabe_von} gemerkte Kategorie ist keine aktive "
                    "Ausgabenkategorie")
        return "die gewählte Kategorie ist keine aktive Ausgabenkategorie"
    if kategorie["schluessel"] == "HARDWARE_ANLAGE":
        return "Anlagegut — gehört ins Anlagenverzeichnis, bitte in der Prüfmaske bestätigen"
    if kategorie["kennzahl_nicht_abziehbar"] is not None:
        return (f"{kategorie['bezeichnung']} braucht eigene Angaben am Beleg (Anlass, "
                "Teilnehmer bzw. Empfänger) — bitte in der Prüfmaske ergänzen")
    if kategorie["schluessel"] in eigenbeleg.EIGENBELEG_KATEGORIEN:
        # Pauschalen (Verpflegung, Kilometer, Homeoffice-Tage) sind von Natur
        # aus Eigenbelege: eine Fremdrechnung in dieser Kategorie ist ein
        # Kategoriefehler, und abziehbar ist dort nur die Pauschale.
        return (f"„{kategorie['bezeichnung']}“ ist eine Pauschale (Eigenbeleg) — eine "
                "Rechnung gehört nicht in diese Kategorie, bitte in der Prüfmaske prüfen")
    if kategorie["schluessel"].startswith("REISE_"):
        # Reisekosten brauchen den Anlass der Reise; die Zuordnung bietet nur
        # die Pruefmaske an.
        return "Reisekosten — bitte in der Prüfmaske einer Reise zuordnen"
    return None


def pruefe_automatisch(
    conn: sqlite3.Connection,
    eingang: pipeline.Eingang,
    datenordner: Path,
    *,
    modus: str = ORDNER,
    heute: date | None = None,
    textleser=None,
    kategorie: int | str | None = VORGABE,
    bezahlt: bool | None = None,
) -> Entscheidung:
    """Ob eine Eingangsdatei ohne Pruefmaske gebucht werden darf -- und wie.

    Schreibt nichts (bis auf den versteckten Anhang einer Mail im Eingang,
    wie jede Auslesung). Gebucht wird nur, wenn ALLES gilt:

    * PDF oder XML (oder eine Mail mit PDF oder XML im Anhang). Ein Foto oder
      Scan ginge durch die Bilderkennung, deren Funde nie sicher sind -- sie
      laeuft hier gar nicht erst.
    * Lesbar, keine Gutschrift, keine Dublette, kein aehnlicher Beleg, und
      nichts spricht dagegen, dass es eine gewoehnliche Rechnung ist (Storno,
      Mahnung, Angebot, Kreditkartenabrechnung, Kontoauszug ...).
    * Lieferant, Belegdatum und Bruttobetrag vorhanden und SICHER erkannt
      (``Fund.sicher``). Aus dem XML ist alles sicher. Aus der Textschicht nur
      ein bekannter Lieferant (``lies`` setzt seinen Namen dann sicher) und
      Betraege, die die Kreuzpruefung oder ein bestaetigtes Lernmuster traegt.
    * Eine Kategorie aus dem Ausgabenbereich -- nicht die Anlagegut-Kategorie,
      keine mit eigenen Aufzeichnungspflichten (Bewirtung, Geschenke,
      Arbeitszimmer), keine Pauschale, keine Reisekosten -- und nicht ueber
      ihrer Wertgrenze; hoechstens 800 EUR netto in jeder Kategorie.
    * Euro -- oder eine SICHER erkannte Fremdwaehrung (XML, oder die gemerkte
      des Lieferanten; aus der Textschicht ist sie nur geraten, siehe
      :func:`_waehrung_sicher`), fuer deren Belegdatum ein EZB-Kurs vorliegt.
      Die Wertgrenzen gelten dann fuer den Euro-Betrag.
    * Mit Zahlung (``bezahlt``, Vorgabe: die Betriebseinstellung): kein
      Belegdatum am Jahreswechsel (22.12. bis 10.01.) -- dort entscheidet der
      Zahltag das EUeR-Jahr, und den bestaetigt der Mensch.
    * Das Belegdatum liegt nicht in der Zukunft (prueft ``buche``).

    Zwei Modi, und sie unterscheiden sich in GENAU drei Regeln:

    * ``ORDNER`` -- das Abholen aus dem Eingangsordner, unbeaufsichtigt. Nur
      ein BEKANNTER Lieferant mit gemerkter Kategorie. Mit Zahlung kein
      Belegdatum in einem Vorjahr: sie aenderte still eine EUeR, die evtl.
      schon abgegeben ist (bei SAMMEL warnen Vorschau und Ergebnis,
      ``Entscheidung.vorjahr``).
    * ``SAMMEL`` -- "Alle prüfen und buchen", ein bewusster Klick nach einer
      Vorschau. Ein neuer Lieferant wird mit seinem SICHER erkannten Namen
      angelegt; die Kategorie ist ``kategorie`` (``VORGABE``: die des
      Lieferanten, ``None``: ausdruecklich keine -- der Beleg steht dann unter
      "nur ungeklärte" und die EUeR meldet ihn, bis er zugeordnet ist).

    Netto, Steuer, Satz und Belegnummer werden nur uebernommen, wenn sie
    selbst sicher sind.
    """
    if bezahlt is None:
        bezahlt = bezahlvorgabe.vorgabe(conn)
    if eingang.endung not in AUTOMATISCH_ENDUNGEN:
        return Entscheidung("Foto oder Scan — bitte in der Prüfmaske bestätigen")
    try:
        lesung = pipeline.lies(conn, eingang, heute=heute, bilderkennung=False,
                               textleser=textleser)
    except textdienst.ZuLangsam as fehler:
        return Entscheidung(f"{fehler} — bitte in der Prüfmaske prüfen")
    except sqlite3.OperationalError as fehler:
        if _gesperrt(fehler):
            raise DatenbankBelegt(str(fehler)) from fehler
        raise
    except BelegFehler as fehler:
        return Entscheidung(f"{NICHT_LESBAR}{fehler}")
    except OSError as fehler:
        return Entscheidung(f"{NICHT_LESBAR}{fehlertext(fehler)}")

    def warten(grund: str) -> Entscheidung:
        return Entscheidung(grund, lesung, dublette_von=lesung.dublette_von)

    felder = lesung.vorschlag.felder
    if lesung.ist_gutschrift:
        return warten("Gutschrift — keine Ausgabe")
    if lesung.ist_dublette:
        return warten(f"schon als Beleg {lesung.dublette_von} gebucht")
    if lesung.anhang is not None and lesung.anhang.endung not in AUTOMATISCH_ENDUNGEN:
        return warten("E-Mail mit Foto oder Scan im Anhang — bitte in der Prüfmaske bestätigen")
    mail_text = eingang.endung == ".eml" and lesung.anhang is None
    if not felder:
        if "manuell" in lesung.stufen:
            return warten("keine Textschicht (Scan) — bitte in der Prüfmaske bestätigen")
        return warten("nichts erkannt — bitte in der Prüfmaske eintragen")
    if modus == ORDNER:
        # Ein Beleg oder eine Einnahme ohne Datei mit gleichem Betrag in der
        # Naehe (etwa aus WISO): die Buchung steht schon, es fehlt nur das
        # Original. Gebucht entstuende ein zweiter Beleg mit Zahlung -- die
        # Ausgabe zaehlte doppelt. Die aehnlichen Belege unten finden ihn
        # nicht: er traegt das Bankdatum, nicht das Belegdatum. Vor den
        # Bedenken zur Dokumentart: "passt zu Einnahme 5" sagt bei einer
        # Gutschrift mehr als "keine gewöhnliche Rechnung". (Bei "Alle
        # prüfen und buchen" bietet die Vorschau die Zuordnung selbst an,
        # app/logik/beleg/sammelbuchung.py.)
        from app.logik.beleg import anhaengen

        treffer = anhaengen.zur_lesung(conn, lesung)
        if treffer:
            return warten(anhaengen.wartegrund(treffer))
    if lesung.vorschlag.bedenken:
        # Sicher erkannte Felder genuegen nicht, wenn das Dokument keine
        # gewoehnliche Rechnung ist (Gutschrift, Storno, Angebot ...) oder
        # seine Summen nicht aufgehen -- das entscheidet der Mensch.
        return warten(lesung.vorschlag.bedenken[0])
    if lesung.aehnliche:
        e_rechnung = _als_e_rechnung_gebucht(conn, lesung.aehnliche)
        if eingang.endung == ".pdf" and e_rechnung is not None:
            return warten(f"Sichtdatei (PDF) zur E-Rechnung Beleg {e_rechnung} — maßgebend ist "
                          "die gebuchte E-Rechnung (XML); dieses PDF braucht keinen eigenen "
                          "Beleg")
        return warten("ähnlicher Beleg vorhanden: "
                      f"{pipeline.aehnliche_als_text(lesung.aehnliche, waehrung=lesung.waehrung)}")
    if "lieferant" not in felder or not felder["lieferant"].wert:
        return warten("Lieferant nicht erkannt")
    if lesung.lieferant_id is None and modus != SAMMEL:
        return warten(NEUER_LIEFERANT)
    for feld, feldname in PFLICHTFELDER.items():
        fund = felder.get(feld)
        if fund is None or fund.wert in (None, ""):
            return warten(f"{feldname} nicht erkannt")
        if not fund.sicher:
            if mail_text:
                return warten(f"E-Mail ohne Rechnung im Anhang: {feldname} nicht sicher "
                              "erkannt — bitte in der Prüfmaske bestätigen")
            if feld == "lieferant" and lesung.lieferant_id is None:
                return warten("neuer Lieferant, Name nicht sicher erkannt — bitte in der "
                              "Prüfmaske bestätigen (Kategorie wählen und „als Vorgabe "
                              "merken“ anhaken)")
            return warten(f"{feldname} nicht sicher erkannt")
    idnr = felder.get("lieferant_ust_idnr")
    if idnr is not None and not idnr.sicher:
        return warten("USt-IdNr. des Lieferanten nicht eindeutig erkannt")

    fund_waehrung = felder.get("waehrung")
    lieferant: sqlite3.Row | None = None
    lieferant_neu: tuple[str, str | None] | None = None
    if lesung.lieferant_id is not None:
        lieferant = conn.execute("SELECT * FROM lieferant WHERE id = ?",
                                 (lesung.lieferant_id,)).fetchone()
        if lieferant is None:
            return warten(NEUER_LIEFERANT)
        if not lieferant["aktiv"]:
            return warten(f"{lieferant['name']} ist als inaktiv markiert")
        grund = _idnr_widerspruch(lieferant, idnr.wert if idnr is not None else None,
                                  aus_text=lesung.vorschlag.quelle != "xml")
        if grund:
            return warten(grund)
        roh_waehrung = (fund_waehrung.wert if fund_waehrung is not None
                        else lieferant["waehrung_default"])
        gelesen_in = pipeline._waehrung_oder_eur(roh_waehrung)
        fremde = pipeline.aehnliche_bei_anderen(
            conn, lieferant["id"], felder.get("belegnummer") and felder["belegnummer"].wert,
            felder["belegdatum"].wert, felder["brutto"].wert, waehrung=gelesen_in)
        if fremde:
            return warten("ähnlicher Beleg bei einem anderen Lieferanten-Datensatz: "
                          f"{pipeline.aehnliche_als_text(fremde, waehrung=gelesen_in)} "
                          "Ist das derselbe Lieferant "
                          f"wie {lieferant['name']}? Bitte in der Prüfmaske prüfen")
        name = lieferant["name"]
    else:
        # Neuer Lieferant ("Alle prüfen und buchen"): ohne Lieferanten-Datensatz
        # hat ``lies`` bei ALLEN Belegen nach derselben Rechnung gesucht.
        roh_waehrung = fund_waehrung.wert if fund_waehrung is not None else None
        if lesung.aehnliche_andere:
            return warten("ähnlicher Beleg vorhanden: " + pipeline.aehnliche_als_text(
                lesung.aehnliche_andere, waehrung=lesung.waehrung))
        name = str(felder["lieferant"].wert).strip()
        lieferant_neu = (name, str(idnr.wert).strip() if idnr is not None and idnr.wert
                         else None)

    # --- Die Kategorie.
    herkunft: str | None = None
    if kategorie == VORGABE or modus != SAMMEL:
        kategorie_id = lieferant["standard_kategorie_id"] if lieferant is not None else None
        if kategorie_id is None and modus != SAMMEL:
            return warten(f"für {name} ist keine Kategorie als Vorgabe gemerkt")
        herkunft = "Vorgabe des Lieferanten" if kategorie_id is not None else None
    else:
        kategorie_id = kategorie if isinstance(kategorie, int) else None
        vorgegeben = lieferant["standard_kategorie_id"] if lieferant is not None else None
        herkunft = (None if kategorie_id is None
                    else "Vorgabe des Lieferanten" if kategorie_id == vorgegeben else "gewählt")
    kategorie_zeile: sqlite3.Row | None = None
    if kategorie_id is not None:
        kategorie_zeile = conn.execute("SELECT * FROM euer_kategorie WHERE id = ?",
                                       (kategorie_id,)).fetchone()
        grund = _sperre_der_kategorie(
            kategorie_zeile, vorgabe_von=name if herkunft == "Vorgabe des Lieferanten" else None)
        if grund:
            return warten(grund)

    # --- Die Waehrung.
    try:
        waehrung = pipeline.normalisiere_waehrung(roh_waehrung)
    except BelegFehler:
        return warten("Währung nicht erkannt")
    euro_cent: int | None = None
    if waehrung != "EUR" and not _waehrung_sicher(fund_waehrung, lieferant, waehrung):
        # Aus der Textschicht ist die Waehrung nur geraten: jedes "$" gilt
        # als US-Dollar, auch in "CA$" und "A$". Umgerechnet mit dem falschen
        # Kurs stuende der Betrag ohne Rueckfrage in der EUeR -- wie jede
        # andere unsichere Angabe entscheidet hier der Mensch.
        return warten(f"Währung nicht sicher erkannt (vermutlich {waehrung}) — bitte in der "
                      "Prüfmaske bestätigen")
    if waehrung != "EUR":
        # Umgerechnet zum EZB-Kurs am Belegdatum -- dem Zahltag, wenn die
        # Zahlung mitgebucht wird. Ohne Kurs (offline, Waehrung nicht
        # gefuehrt, Kurs noch nicht veroeffentlicht) wartet der Beleg; die
        # Pruefmaske nimmt dann den Betrag laut Kreditkarte oder einen Kurs
        # von Hand.
        fremd = felder["brutto"].wert
        try:
            stichtag = date.fromisoformat(str(felder["belegdatum"].wert))
        except ValueError:
            return warten("Belegdatum nicht erkannt")
        if isinstance(fremd, int) and fremd > 0:
            try:
                kurs = kurse.kurs_besorgen(datenordner, waehrung, stichtag, heute=heute)
            except kurse.KeinKurs as fehler:
                if fehler.grund == "noch_nicht":
                    weiter = ("in der Prüfmaske buchen, sobald er veröffentlicht ist, oder "
                              "dort den Betrag laut Kreditkarte eintragen")
                else:
                    weiter = ("in der Prüfmaske den Betrag laut Kreditkarte oder einen Kurs "
                              "von Hand eintragen")
                return warten(f"Fremdwährung ({waehrung}) — {kurse.klein(str(fehler))}; {weiter}")
            euro_cent = kurse.in_euro_cent(fremd, kurs.kurs)

    def sicherer_wert(feld: str) -> object:
        fund = felder.get(feld)
        return fund.wert if fund is not None and fund.sicher else None

    brutto = felder["brutto"].wert
    netto, ust = sicherer_wert("netto"), sicherer_wert("ust")
    # Der Fund heisst "ust_satz_promille" und liegt schon in Promille vor
    # (19 % = 190) -- genau die Einheit, die buche() erwartet.
    satz = sicherer_wert("ust_satz_promille")
    rc_fund = felder.get("reverse_charge")
    rc_vorgabe = bool(lieferant["reverse_charge_default"]) if lieferant is not None else False
    if lesung.vorschlag.quelle == "xml":
        # Beim XML entscheidet die Steuerkategorie des Dokuments: fehlt AE,
        # ist das selbst eine sichere Aussage -- keine Steuerschuld nach § 13b
        # UStG. Widerspricht die gemerkte Vorgabe, entscheidet der Mensch.
        reverse_charge = bool(rc_fund is not None and rc_fund.wert)
        if rc_vorgabe and not reverse_charge:
            kategorie_fund = felder.get("steuerkategorie")
            ausgewiesen = xml_leser.steuerkategorien_text(
                kategorie_fund.wert if kategorie_fund is not None else None)
            return warten(f"Reverse Charge laut Vorgabe für {name}, die Rechnung weist aber "
                          f"{ausgewiesen} aus — bitte in der Prüfmaske prüfen")
    else:
        # Ein PDF ohne Vermerk sagt nichts: dann gilt die gemerkte Vorgabe.
        reverse_charge = bool(rc_fund.wert) if rc_fund is not None else rc_vorgabe
    if not isinstance(brutto, int) or brutto <= 0:
        return warten("Bruttobetrag nicht erkannt")
    if (not reverse_charge and isinstance(netto, int) and isinstance(ust, int)
            and netto + ust != brutto):
        return warten(f"Summen gehen nicht auf: {euro(netto)} € netto + {euro(ust)} € "
                      f"Umsatzsteuer ergeben nicht {euro(brutto)} € brutto — bitte am "
                      "Dokument prüfen")
    # Die Wertgrenze mit genau dem Netto, das buche() speichert: ein Netto,
    # das nicht zum Brutto passt, verwirft buche() und rechnet die Grenze aus
    # dem Brutto -- geprueft war sie sonst mit einer Zahl, die danach fehlt.
    # Bei Fremdwaehrung speichert buche() kein Netto: die Grenze gilt dem
    # Euro-Betrag, nicht dem Betrag in Dollar.
    if euro_cent is not None:
        grenzwert = pipeline.grenzwert_netto_cent(euro_cent, None, None)
    else:
        try:
            netto_b, _ust_b, satz_b = pipeline.netto_und_steuer(
                brutto, netto, ust, satz, reverse_charge=reverse_charge)
        except BelegFehler as fehler:
            return warten(str(fehler))
        grenzwert = pipeline.grenzwert_netto_cent(brutto, netto_b, satz_b)
    grenze = kategorie_zeile["grenze_netto_cent"] if kategorie_zeile is not None else None
    if grenze is not None and grenzwert > grenze:
        return warten(f"über der Wertgrenze von {euro(grenze)} € netto — vermutlich ein "
                      "Anlagegut, bitte in der Prüfmaske bestätigen")
    if grenzwert > HOECHSTENS_NETTO_CENT:
        return warten(f"über {euro(HOECHSTENS_NETTO_CENT)} € netto — möglicherweise ein "
                      "Anlagegut, bitte in der Prüfmaske bestätigen")

    # --- Die Zahlung: Zahltag = Belegdatum, ausser am Jahreswechsel. Vorher
    # ein Datum in der Zukunft (buche() wiese es ab): das ist der wichtigere
    # Grund, ein Tippfehler im Jahr, und die Vorschau soll ihn nennen.
    belegdatum = str(felder["belegdatum"].wert)
    try:
        tag = date.fromisoformat(belegdatum)
    except ValueError:
        return warten("Belegdatum nicht erkannt")
    if tag > (heute or date.today()):
        return warten(f"Das Belegdatum {tag:%d.%m.%Y} liegt in der Zukunft — vermutlich ein "
                      "Tippfehler im Jahr.")
    zahltag: str | None = None
    zahlungsart = "UEBERWEISUNG"
    vorjahr: int | None = None
    if bezahlt:
        if bezahlvorgabe.am_jahreswechsel(belegdatum):
            return warten(bezahlvorgabe.JAHRESWECHSEL_GRUND)
        vorjahr = bezahlvorgabe.vorjahr(tag, heute or date.today())
        if vorjahr is not None and modus == ORDNER:
            # Die EUeR des Vorjahres ist womoeglich schon abgegeben -- sie
            # aendert hier niemand unbeaufsichtigt.
            return warten(bezahlvorgabe.vorjahr_grund(vorjahr))
        zahltag = belegdatum
        zahlungsart = bezahlvorgabe.zahlungsart(conn, lieferant["id"] if lieferant else None)

    bestaetigt: dict[str, object] = {
        "belegdatum": belegdatum,
        "belegnummer": sicherer_wert("belegnummer"),
        "waehrung": waehrung,
        "brutto": brutto,
        "netto": netto,
        "ust": ust,
        "brutto_eur": None,
        "ust_satz_promille": satz,
        "reverse_charge": reverse_charge,
        "lieferant": name,
        "lieferant_ust_idnr": (lieferant["ust_idnr"] if lieferant is not None
                               else lieferant_neu[1] if lieferant_neu else None),
    }
    quote = (lieferant["quote_default"] if lieferant is not None
             and lieferant["quote_default"] is not None else 100)
    return Entscheidung(
        None, lesung, bestaetigt,
        lieferant_id=lieferant["id"] if lieferant is not None else None,
        lieferant_neu=lieferant_neu, lieferant=name,
        kategorie_id=kategorie_id,
        kategorie=kategorie_zeile["bezeichnung"] if kategorie_zeile is not None else None,
        kategorie_herkunft=herkunft, quote=quote, waehrung=waehrung, brutto_cent=brutto,
        euro_cent=euro_cent, zahltag=zahltag, zahlungsart=zahlungsart, vorjahr=vorjahr,
    )


def buche_entscheidung(
    conn: sqlite3.Connection,
    entscheidung: Entscheidung,
    datenordner: Path,
    *,
    notiz: str,
    vermerk: str | None = None,
    heute: date | None = None,
) -> tuple[int | None, str | None]:
    """Bucht, was :func:`pruefe_automatisch` als buchbar entschieden hat.

    Gibt ``(beleg_id, None)`` zurueck, oder ``(None, grund)``, wenn ``buche``
    unter seiner Sperre doch abweist (gerade gebucht, aehnlicher Beleg,
    Belegdatum in der Zukunft). Eine gesperrte Datenbank wird zu
    ``DatenbankBelegt``. Gebucht wird mit ``automatisch=True``:
    ``geprueft_am`` bleibt leer, der Lernspeicher lernt nicht, und das
    Verfahren traegt den Vermerk.
    """
    if not entscheidung.buchbar or entscheidung.lesung is None:
        return None, entscheidung.grund or "nicht buchbar"
    try:
        buchung = pipeline.buche(
            conn, entscheidung.lesung, entscheidung.bestaetigt, datenordner,
            lieferant_id=entscheidung.lieferant_id, lieferant_neu=entscheidung.lieferant_neu,
            kategorie_id=entscheidung.kategorie_id, quote=entscheidung.quote, notiz=notiz,
            zahlungsart=entscheidung.zahlungsart, heute=heute, automatisch=True,
            vermerk=vermerk, bezahlt_am=entscheidung.zahltag,
            zahlung_begruendung=(bezahlvorgabe.BEGRUENDUNG_AUTOMATISCH
                                 if entscheidung.zahltag else None),
        )
    except pipeline.SchonGebucht as fehler:
        return None, f"schon als Beleg {fehler.beleg_id} gebucht"
    except pipeline.AehnlicherBeleg as fehler:
        return None, f"ähnlicher Beleg vorhanden: {fehler}"
    except BelegFehler as fehler:
        return None, str(fehler)
    except sqlite3.Error as fehler:
        if _gesperrt(fehler):
            raise DatenbankBelegt(str(fehler)) from fehler
        return None, f"die Datenbank hat den Beleg abgewiesen: {fehlertext(fehler)}"
    return buchung.beleg_id, None


def automatisch_buchen(
    conn: sqlite3.Connection,
    eingang: pipeline.Eingang,
    datenordner: Path,
    *,
    heute: date | None = None,
    textleser=None,
    entscheidung_aus: list[Entscheidung] | None = None,
) -> tuple[int | None, str | None]:
    """Bucht eine Eingangsdatei aus dem Eingangsordner ohne Pruefmaske -- wenn
    ALLES sicher ist (die Regeln stehen bei :func:`pruefe_automatisch`, Modus
    ``ORDNER``: nur ein bekannter Lieferant mit gemerkter Kategorie).
    ``entscheidung_aus`` bekommt die Entscheidung angehaengt (fuer das Datum
    im Hinweis zum Ordnerjahr).

    Gibt ``(beleg_id, None)`` zurueck, oder ``(None, grund)``: dann bleibt die
    Datei vorbelegt im Eingang, und der Grund sagt dem Nutzer, warum sie auf
    ihn wartet. Ist "Eingangsbelege sind in der Regel schon bezahlt"
    eingestellt, entsteht die Zahlung mit, Zahltag = Belegdatum (nicht am
    Jahreswechsel und nicht in einem Vorjahr: dann wartet der Beleg).
    """
    entscheidung = pruefe_automatisch(conn, eingang, datenordner, modus=ORDNER, heute=heute,
                                      textleser=textleser)
    if entscheidung_aus is not None:
        entscheidung_aus.append(entscheidung)
    if not entscheidung.buchbar:
        return None, entscheidung.grund
    return buche_entscheidung(conn, entscheidung, datenordner, notiz=NOTIZ, heute=heute)

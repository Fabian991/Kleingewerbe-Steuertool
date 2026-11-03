"""Die Referenzkurse der Europaeischen Zentralbank -- fuer Belege in Fremdwaehrung.

Wozu
----
Fuer die EUeR zaehlt der Euro-Betrag, der tatsaechlich abgeflossen ist, und
zwar am Tag des Abflusses (§ 11 Abs. 2 EStG). Steht er nicht fest -- die
Kreditkartenabrechnung kommt Wochen spaeter --, wird zum Kurs am Zahlungstag
umgerechnet. Massstab ist der Referenzkurs der EZB (so der BFH fuer Arbeitslohn
in Fremdwaehrung; fuer Betriebsausgaben gilt dasselbe entsprechend). Die
Methode ist stetig anzuwenden, Kurs und Kursdatum sind zu dokumentieren. Was
davon am Beleg landet, entscheidet ``app/logik/beleg/umrechnung.py``; dieses
Modul liefert nur den Kurs.

Die Kurstabelle
---------------
Die EZB veroeffentlicht ihre Referenzkurse an jedem TARGET-Geschaeftstag gegen
16 Uhr, als eine Datei mit der ganzen Geschichte seit 1999
(``eurofxref-hist.zip``, darin eine CSV ``Date,USD,JPY,...``: 1 EUR = X
Einheiten der Waehrung, ``N/A`` an Tagen ohne Kurs). Die Datei ist oeffentlich
und fuer alle gleich; geladen wird sie ohne Abfrageparameter, ohne Referer und
ohne Cookies. **Belegdaten verlassen den Rechner dabei nicht** -- die Anfrage
verraet nur, dass jemand die Kurstabelle laedt.

Abgelegt wird sie unter ``<Datenordner>/kurse/``: die Rohdatei, wie sie kam
(sie ist der Nachweis, nicht eine daraus gezogene Zahl), daneben
``stand.json`` mit Abrufzeit und SHA-256. Ersetzt wird atomar -- erst eine
Arbeitsdatei, dann ``os.replace`` -- und nur durch eine Datei, die die
Pruefung besteht (Kopfzeile, Datumsformat, positive und plausible Kurse,
nicht aelter als die vorhandene). Wird eine Tabelle fuer einen Beleg
benutzt, bleibt genau diese Fassung unter ``kurse/verwendet/<SHA-256>.zip``
erhalten (eine harte Verknuepfung, solange sie die aktuelle ist): so laesst
sich jeder Kurs spaeter aus derselben Datei nachrechnen, auch wenn die
Tabelle laengst neuer ist.

Aktualisiert wird beim Start im Hintergrund (wenn die Tabelle aelter als ein
Tag ist) und bei Bedarf, wenn ein benoetigtes Datum noch fehlt. Ohne Netz
geht es mit der vorhandenen Tabelle weiter; fehlt der Kurs dann, sagt die
Meldung das, und ein Kurs von Hand oder der Kartenbetrag hilft weiter.

In Tests nie echtes Netz: der Abruf geht ueber :data:`netzabruf`, den die
Tests ersetzen, und die Umgebungsvariable ``STEUER_TOOL_OHNE_NETZ`` sperrt
den echten Abruf zusaetzlich.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: Die Quelle. Fest, ohne Abfrageparameter: nichts an der Anfrage haengt vom
#: Nutzer oder seinen Belegen ab.
QUELLE = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"

#: Unterordner im Datenordner und die Dateien darin.
ORDNER = "kurse"
ROHDATEI = "eurofxref-hist.zip"
STANDDATEI = "stand.json"
VERWENDET = "verwendet"

#: Grenzen fuer den Abruf. Die echte Datei hat 2026 rund 0,6 MB (entpackt
#: 1,9 MB) und waechst um etwa 90 KB im Jahr; das Zehnfache genuegt fuer
#: Jahrzehnte und haelt trotzdem eine falsche oder boeswillige Antwort auf.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
MAX_CSV_BYTES = 40 * 1024 * 1024
ZEITLIMIT_SEKUNDEN = 20

#: Hoechstens so viele Tage darf der letzte veroeffentlichte Kurs vor dem
#: Stichtag liegen. Ueber Ostern sind es vier Tage, ueber Weihnachten
#: hoechstens fuenf; mehr heisst, die EZB fuehrt die Waehrung nicht mehr.
HOECHSTENS_ZURUECK_TAGE = 10

#: Aelter als das, und der Start laedt die Tabelle im Hintergrund neu.
VERALTET_NACH = timedelta(days=1)
#: Nach einem gescheiterten Abruf wird so lange nicht erneut gefragt -- sonst
#: wartete ohne Netz jede Seite mit Fremdwaehrung auf die Zeitgrenze.
NEUER_VERSUCH_NACH_FEHLER = timedelta(minutes=5)
#: Nach einem gelungenen Abruf ebenso: fehlt der Kurs eines Tages dann noch,
#: ist er noch nicht veroeffentlicht, und ein zweiter Abruf aendert nichts.
NEUER_VERSUCH_NACH_ABRUF = timedelta(minutes=15)

#: Der erste Tag der Euro-Referenzkurse.
ERSTER_TAG = date(1999, 1, 4)
#: Ab hier fehlt in der Tabelle kein TARGET-Geschaeftstag (davor zwei
#: Silvestertage, an denen TARGET geschlossen hatte).
LUECKENLOS_AB = date(2002, 1, 1)

#: Gesetzt, und der echte Abruf verweigert sich (Tests, auch in Kindprozessen).
OHNE_NETZ = "STEUER_TOOL_OHNE_NETZ"

#: Plausible Spannen fuer die Hauptwaehrungen seit 1999 (mit Reserve). Ein
#: Kurs ausserhalb heisst: die Datei ist nicht, was sie zu sein vorgibt.
PLAUSIBEL = {
    "USD": (Decimal("0.5"), Decimal("2.5")),
    "GBP": (Decimal("0.4"), Decimal("1.5")),
    "CHF": (Decimal("0.5"), Decimal("2.5")),
    "JPY": (Decimal("50"), Decimal("400")),
}
#: Was die Tabelle der EZB immer fuehrt, an jedem Geschaeftstag. Fehlt eine
#: davon beim ersten Abruf, ist die Datei nicht die vollstaendige Tabelle.
HAUPTWAEHRUNGEN = ("USD", "GBP", "CHF", "JPY")
#: Obergrenze fuer jeden Kurs (die tuerkische Lira stand 2004 bei 1,9 Millionen).
KURS_HOECHSTENS = Decimal("100000000")

_DATUM = re.compile(r"\d{4}-\d{2}-\d{2}")
_ZAHL = re.compile(r"\d{1,9}(?:\.\d{1,10})?")
_CODE = re.compile(r"[A-Z]{3}")


# ------------------------------------------------------------------- Fehler

class KursFehler(Exception):
    """Grundtyp. Die Meldung ist deutsch und fuer den Nutzer bestimmt."""


class AbrufFehler(KursFehler):
    """Die Tabelle liess sich nicht laden (offline, Zeitgrenze, Zertifikat ...)."""


class TabelleUngueltig(KursFehler):
    """Die Datei ist keine brauchbare Kurstabelle -- sie wird nicht gespeichert."""


class KeinKurs(KursFehler):
    """Fuer Waehrung und Stichtag gibt es keinen EZB-Kurs (oder noch keinen).

    ``grund`` unterscheidet die Faelle fuer den Aufrufer: ``unbekannt`` (die
    EZB fuehrt die Waehrung nicht), ``luecke`` (letzter Kurs zu alt),
    ``noch_nicht`` (noch nicht veroeffentlicht), ``nicht_aktuell`` (die
    Tabelle endet vor dem Stichtag und liess sich nicht aktualisieren --
    offline), ``zukunft``, ``keine_tabelle``. Der Text beschreibt nur die
    Lage; was der Nutzer tun kann, haengt vom Ort ab (Pruefmaske,
    Eingangsordner) und steht dort (``umrechnung.hilfe``).
    """

    def __init__(self, text: str, grund: str) -> None:
        super().__init__(text)
        self.grund = grund


# ------------------------------------------------------------ TARGET-Kalender

def ostersonntag(jahr: int) -> date:
    """Ostersonntag nach dem gregorianischen Kalender (Gauss/Meeus).

    >>> ostersonntag(2026), ostersonntag(2027)
    (datetime.date(2026, 4, 5), datetime.date(2027, 3, 28))
    """
    a = jahr % 19
    b, c = divmod(jahr, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    wochentag = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * wochentag) // 451
    monat, tag = divmod(h + wochentag - 7 * m + 114, 31)
    return date(jahr, monat, tag + 1)


def target_geschlossen(tag: date) -> bool:
    """Ist ``tag`` ein Tag, an dem die EZB keinen Referenzkurs veroeffentlicht?

    Wochenende und die TARGET-Feiertage: Neujahr, Karfreitag, Ostermontag,
    1. Mai, 25. und 26. Dezember. Gegen die echte Tabelle geprueft: von 2002
    bis heute fehlt kein Geschaeftstag, und kein Schliesstag traegt einen Kurs.

    >>> target_geschlossen(date(2026, 3, 14)), target_geschlossen(date(2026, 4, 3))
    (True, True)
    >>> target_geschlossen(date(2026, 4, 6)), target_geschlossen(date(2026, 4, 7))
    (True, False)
    """
    if tag.weekday() >= 5:
        return True
    if (tag.month, tag.day) in ((1, 1), (5, 1), (12, 25), (12, 26)):
        return True
    ostern = ostersonntag(tag.year)
    return tag in (ostern - timedelta(days=2), ostern + timedelta(days=1))


def erster_geschaeftstag(nach: date, bis: date) -> date | None:
    """Der erste TARGET-Geschaeftstag in (``nach``, ``bis``] -- oder None.

    >>> erster_geschaeftstag(date(2026, 4, 2), date(2026, 4, 6)) is None
    True
    >>> erster_geschaeftstag(date(2026, 4, 2), date(2026, 4, 9))
    datetime.date(2026, 4, 7)
    """
    tag = nach + timedelta(days=1)
    while tag <= bis:
        if not target_geschlossen(tag):
            return tag
        tag += timedelta(days=1)
    return None


# ----------------------------------------------------------------- Die Tabelle

@dataclass(frozen=True)
class Tabelle:
    """Eine geladene und gepruefte Kurstabelle."""

    sha256: str
    #: Wann sie abgerufen wurde (UTC, ISO). None, wenn das nicht feststeht.
    abgerufen_utc: str | None
    erster_tag: date
    letzter_tag: date
    #: Je Waehrung die Tage mit Kurs (aufsteigend) und die Kurse als Text,
    #: genau so, wie sie in der Datei stehen -- mit allen Stellen der EZB.
    reihen: dict[str, tuple[tuple[date, ...], tuple[str, ...]]]

    @property
    def waehrungen(self) -> tuple[str, ...]:
        return tuple(sorted(self.reihen))


@dataclass(frozen=True)
class Kurs:
    """Ein Kurs fuer einen Stichtag: 1 EUR = ``kurs`` Einheiten ``waehrung``."""

    waehrung: str
    #: Der Kurs als Text mit allen Stellen der Quelle ("1.0856", "161.47").
    kurs: str
    #: Der Tag, dessen Kurs gilt -- bei Wochenende oder Feiertag der letzte
    #: Geschaeftstag davor.
    kursdatum: date
    #: Der Tag, fuer den umgerechnet wird (Zahlungstag, sonst Belegdatum).
    stichtag: date
    #: "EZB" oder "MANUELL".
    quelle: str = "EZB"
    abgerufen_utc: str | None = None
    sha256: str | None = None

    @property
    def wert(self) -> Decimal:
        return Decimal(self.kurs)


def entpacke(rohdaten: bytes) -> str:
    """Die CSV aus dem ZIP der EZB -- mit Groessengrenze und ohne Ueberraschungen.

    Genau eine CSV-Datei, hoechstens :data:`MAX_CSV_BYTES` entpackt. Eine
    Datei, die sich beim Entpacken aufblaeht, wird nicht bis zum Ende gelesen.
    """
    if not rohdaten.startswith(b"PK"):
        raise TabelleUngueltig("Die geladene Datei ist kein ZIP-Archiv — keine Kurstabelle "
                               "der EZB.")
    try:
        archiv = zipfile.ZipFile(io.BytesIO(rohdaten))
        mitglieder = [i for i in archiv.infolist() if not i.is_dir()]
        if len(mitglieder) != 1 or not mitglieder[0].filename.lower().endswith(".csv"):
            raise TabelleUngueltig("Das ZIP-Archiv enthält nicht genau eine CSV-Datei — "
                                   "keine Kurstabelle der EZB.")
        if mitglieder[0].file_size > MAX_CSV_BYTES:
            raise TabelleUngueltig("Die Kurstabelle ist entpackt unplausibel groß.")
        with archiv.open(mitglieder[0]) as datei:
            daten = datei.read(MAX_CSV_BYTES + 1)
    except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, OSError,
            EOFError):
        # Ohne den Text der Ausnahme: der ist englisch ("File is not a zip
        # file") und sagt dem Nutzer nichts.
        raise TabelleUngueltig("Das ZIP-Archiv der Kurstabelle ist beschädigt oder "
                               "unvollständig.") from None
    if len(daten) > MAX_CSV_BYTES:
        raise TabelleUngueltig("Die Kurstabelle ist entpackt unplausibel groß.")
    try:
        return daten.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TabelleUngueltig("Die Kurstabelle ist keine Textdatei.") from None


def lies_csv(
    text: str, *, heute: date | None = None
) -> tuple[date, date, dict[str, tuple[tuple[date, ...], tuple[str, ...]]]]:
    """Prueft und liest die CSV: (erster Tag, letzter Tag, Reihen je Waehrung).

    ``N/A`` (und ein leeres Feld) heisst: an diesem Tag kein Kurs. Das
    abschliessende Komma der EZB-Datei ergibt eine leere letzte Spalte; sie
    zaehlt nicht.

    >>> erster, letzter, reihen = lies_csv(
    ...     "Date,USD,GBP,RUB,\\n2026-03-13,1.1476,0.86503,N/A,\\n"
    ...     "2026-03-12,1.1547,0.86243,N/A,\\n", heute=date(2026, 3, 16))
    >>> erster, letzter
    (datetime.date(2026, 3, 12), datetime.date(2026, 3, 13))
    >>> reihen["USD"][1], reihen["RUB"]
    (('1.1547', '1.1476'), ((), ()))
    """
    zeilen = [z for z in csv.reader(io.StringIO(text)) if any(f.strip() for f in z)]
    if not zeilen:
        raise TabelleUngueltig("Die Kurstabelle ist leer.")
    kopf = [f.strip() for f in zeilen[0]]
    if kopf and kopf[-1] == "":
        kopf.pop()
    if not kopf or kopf[0] != "Date":
        raise TabelleUngueltig("Die Kopfzeile der Kurstabelle beginnt nicht mit „Date“ — "
                               "keine Kurstabelle der EZB.")
    codes = kopf[1:]
    if not codes or any(not _CODE.fullmatch(c) for c in codes) or len(set(codes)) != len(codes):
        raise TabelleUngueltig("Die Kopfzeile der Kurstabelle nennt keine gültigen "
                               "Währungscodes.")
    if "USD" not in codes:
        raise TabelleUngueltig("Die Kurstabelle enthält keinen Kurs für US-Dollar — keine "
                               "Kurstabelle der EZB.")

    grenze = (heute or date.today()) + timedelta(days=1)
    werte: dict[str, list[tuple[date, str]]] = {c: [] for c in codes}
    tage: set[date] = set()
    for nr, zeile in enumerate(zeilen[1:], start=2):
        felder = [f.strip() for f in zeile]
        while len(felder) > len(codes) + 1 and felder[-1] == "":
            felder.pop()
        if len(felder) != len(codes) + 1:
            raise TabelleUngueltig(f"Zeile {nr} der Kurstabelle hat {len(felder)} Felder "
                                   f"statt {len(codes) + 1}.")
        if not _DATUM.fullmatch(felder[0]):
            raise TabelleUngueltig(f"Zeile {nr} der Kurstabelle hat kein Datum "
                                   f"(„{felder[0][:20]}“).")
        try:
            tag = date.fromisoformat(felder[0])
        except ValueError:
            raise TabelleUngueltig(f"Zeile {nr}: „{felder[0]}“ ist kein Datum.") from None
        if not ERSTER_TAG <= tag <= grenze:
            raise TabelleUngueltig(f"Zeile {nr}: der {tag:%d.%m.%Y} liegt außerhalb der "
                                   "Euro-Referenzkurse.")
        if tag in tage:
            raise TabelleUngueltig(f"Der {tag:%d.%m.%Y} steht zweimal in der Kurstabelle.")
        tage.add(tag)
        for code, roh in zip(codes, felder[1:], strict=True):
            if roh in ("N/A", ""):
                continue
            if not _ZAHL.fullmatch(roh):
                raise TabelleUngueltig(f"Zeile {nr}: „{roh[:20]}“ ist kein Kurs ({code}).")
            wert = Decimal(roh)
            if not 0 < wert < KURS_HOECHSTENS:
                raise TabelleUngueltig(f"Zeile {nr}: {roh} ist kein möglicher Kurs ({code}).")
            spanne = PLAUSIBEL.get(code)
            if spanne and not spanne[0] <= wert <= spanne[1]:
                raise TabelleUngueltig(f"Zeile {nr}: {code} {roh} ist unplausibel — die Datei "
                                       "ist nicht die Kurstabelle der EZB.")
            werte[code].append((tag, roh))
    if not tage:
        raise TabelleUngueltig("Die Kurstabelle enthält keinen einzigen Tag.")
    letzter = max(tage)
    # Ab 2002 fehlt in der echten Datei kein TARGET-Geschaeftstag (siehe
    # target_geschlossen). Fehlt einer, ist die Datei unvollstaendig -- und
    # fuer diesen Tag galte still der Kurs des Vortags.
    tag = max(min(tage), LUECKENLOS_AB)
    while tag < letzter:
        if tag not in tage and not target_geschlossen(tag):
            raise TabelleUngueltig(f"In der Kurstabelle fehlt der {tag:%d.%m.%Y}, ein "
                                   "Geschäftstag der EZB — die Datei ist unvollständig.")
        tag += timedelta(days=1)
    if not werte["USD"] or max(werte["USD"])[0] != letzter:
        raise TabelleUngueltig("Am jüngsten Tag der Kurstabelle fehlt der US-Dollar — die "
                               "Datei ist unvollständig.")
    reihen = {}
    for code, liste in werte.items():
        liste.sort()
        reihen[code] = (tuple(t for t, _ in liste), tuple(k for _, k in liste))
    return min(tage), letzter, reihen


def pruefsumme(daten: bytes) -> str:
    return hashlib.sha256(daten).hexdigest()


def tabelle_aus_bytes(rohdaten: bytes, *, abgerufen_utc: str | None = None,
                      heute: date | None = None) -> Tabelle:
    """Prueft Rohdaten (das ZIP der EZB) und liest sie als Tabelle."""
    erster, letzter, reihen = lies_csv(entpacke(rohdaten), heute=heute)
    return Tabelle(sha256=pruefsumme(rohdaten), abgerufen_utc=abgerufen_utc,
                   erster_tag=erster, letzter_tag=letzter, reihen=reihen)


# ------------------------------------------------------------ Ablage im Ordner

def ordner(datenordner: Path) -> Path:
    return Path(datenordner) / ORDNER


def _atomar_schreiben(ziel: Path, daten: bytes) -> None:
    """Erst eine eigene Arbeitsdatei daneben, dann in einem Zug an den Namen.

    Bricht der Prozess mitten im Schreiben ab, liegt unter dem echten Namen
    weiter die vorige, vollstaendige Datei.
    """
    ziel.parent.mkdir(parents=True, exist_ok=True)
    arbeit = ziel.with_name(f".{ziel.name}.{secrets.token_hex(6)}.teil")
    try:
        with arbeit.open("wb") as datei:
            datei.write(daten)
            datei.flush()
            os.fsync(datei.fileno())
        os.replace(arbeit, ziel)
    finally:
        arbeit.unlink(missing_ok=True)


def _stand(datenordner: Path) -> dict[str, object]:
    try:
        stand = json.loads((ordner(datenordner) / STANDDATEI).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return stand if isinstance(stand, dict) else {}


def _jetzt_utc(jetzt: datetime | None = None) -> str:
    return (jetzt or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


#: Geladene Tabellen je Datei -- die CSV hat ueber 7.000 Zeilen, und die
#: Pruefmaske fragt bei jedem Tastendruck nach einem Kurs. Eine geladene
#: Tabelle belegt gut 10 MB: gehalten werden nur die zuletzt benutzten
#: (praktisch die aktuelle), sonst wuchs der Speicher mit jeder Fassung, die
#: je gelesen wurde, bis zum Neustart.
_ZWISCHENSPEICHER: dict[Path, tuple[tuple[int, int, int], Tabelle]] = {}
_ZWISCHENSPEICHER_SPERRE = threading.Lock()
ZWISCHENSPEICHER_HOECHSTENS = 2


def lade_datei(pfad: Path, *, abgerufen_utc: str | None = None) -> Tabelle:
    """Liest eine abgelegte Rohdatei (aktuell oder eine verwendete Fassung)."""
    info = pfad.stat()
    schluessel = (info.st_mtime_ns, info.st_size, info.st_ino)
    with _ZWISCHENSPEICHER_SPERRE:
        gemerkt = _ZWISCHENSPEICHER.get(pfad)
    if gemerkt is not None and gemerkt[0] == schluessel:
        tab = gemerkt[1]
    else:
        # Beim Lesen einer abgelegten Datei kein Zukunftsfenster: sie war
        # beim Ablegen geprueft, und eine falsch gehende Uhr soll sie nicht
        # nachtraeglich unbrauchbar machen.
        tab = tabelle_aus_bytes(pfad.read_bytes(), heute=date.max - timedelta(days=1))
        with _ZWISCHENSPEICHER_SPERRE:
            _ZWISCHENSPEICHER.pop(pfad, None)
            _ZWISCHENSPEICHER[pfad] = (schluessel, tab)
            while len(_ZWISCHENSPEICHER) > ZWISCHENSPEICHER_HOECHSTENS:
                del _ZWISCHENSPEICHER[next(iter(_ZWISCHENSPEICHER))]
    if abgerufen_utc != tab.abgerufen_utc:
        tab = Tabelle(tab.sha256, abgerufen_utc, tab.erster_tag, tab.letzter_tag, tab.reihen)
    return tab


def tabelle(datenordner: Path) -> Tabelle | None:
    """Die abgelegte Tabelle -- oder None, wenn noch keine geladen wurde.

    Die Abrufzeit kommt aus ``stand.json``, solange dessen Pruefsumme zur
    Datei passt; sonst (Abbruch zwischen beiden Dateien) die Aenderungszeit
    der Datei. Eine beschaedigte Datei ergibt :class:`TabelleUngueltig`.
    """
    pfad = ordner(datenordner) / ROHDATEI
    try:
        info = pfad.stat()
    except FileNotFoundError:
        return None
    stand = _stand(datenordner)
    tab = lade_datei(pfad)
    if stand.get("sha256") == tab.sha256 and isinstance(stand.get("abgerufen_utc"), str):
        abgerufen = str(stand["abgerufen_utc"])
    else:
        abgerufen = datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds")
    return Tabelle(tab.sha256, abgerufen, tab.erster_tag, tab.letzter_tag, tab.reihen)


def mindestumfang(tab: Tabelle) -> None:
    """Die absolute Mindestpruefung fuer eine Tabelle, die ohne Vorgaengerin
    abgelegt wird: Beginn am ersten Tag der Euro-Referenzkurse und die
    Hauptwaehrungen am juengsten Tag.

    Mit vorhandener Tabelle vergleicht :func:`speichere` mit ihr (nicht
    kuerzer, keine Waehrung weniger). Beim ersten Abruf gab es nichts zu
    vergleichen: eine gekuerzte Historie -- ein Zwischenspeicher unterwegs,
    eine abgeschnittene Datei, die zufaellig an einer Zeilengrenze endet --
    wurde angenommen, und jeder Beleg vor ihrem Beginn bekam keinen Kurs
    oder, schlimmer, einen aus einer Tabelle ohne Pfund und Franken.
    """
    if tab.erster_tag > ERSTER_TAG:
        raise TabelleUngueltig(
            f"Die geladene Kurstabelle beginnt erst am {tab.erster_tag:%d.%m.%Y}; die "
            f"vollständige Tabelle der EZB beginnt am {ERSTER_TAG:%d.%m.%Y}. Eine gekürzte "
            "Fassung wird nicht gespeichert — beim nächsten Abruf neu versuchen.")
    fehlend = [code for code in HAUPTWAEHRUNGEN
               if not tab.reihen.get(code, ((), ()))[0]
               or tab.reihen[code][0][-1] != tab.letzter_tag]
    if fehlend:
        raise TabelleUngueltig(
            f"In der geladenen Kurstabelle fehlt am {tab.letzter_tag:%d.%m.%Y} "
            f"{', '.join(fehlend)} — die EZB führt {', '.join(HAUPTWAEHRUNGEN)} an jedem "
            "Geschäftstag. Die Datei ist unvollständig und wird nicht gespeichert.")


def speichere(datenordner: Path, rohdaten: bytes, *, jetzt: datetime | None = None,
              heute: date | None = None, erster_abruf_pruefen: bool = False) -> Tabelle:
    """Prueft Rohdaten und legt sie als aktuelle Tabelle ab -- atomar.

    Was die Pruefung nicht besteht, wird nicht gespeichert; die vorhandene
    Tabelle bleibt. Ebenso eine Datei, die frueher endet als die vorhandene
    (ein Zwischenspeicher irgendwo unterwegs, der eine alte Fassung liefert),
    eine, die spaeter beginnt (gekuerzte Geschichte), und eine, der eine
    Waehrung der vorhandenen fehlt: die EZB-Datei waechst nur, Spalten und
    Tage verschwinden nicht.

    ``erster_abruf_pruefen`` (so ruft :func:`aktualisiere` auf): gibt es
    keine lesbare Tabelle zum Vergleich, muss die neue den
    :func:`mindestumfang` haben.
    """
    neu = tabelle_aus_bytes(rohdaten, heute=heute)
    try:
        vorher = tabelle(datenordner)
    except KursFehler:
        vorher = None
    if vorher is None and erster_abruf_pruefen:
        mindestumfang(neu)
    if vorher is not None and neu.letzter_tag < vorher.letzter_tag:
        raise TabelleUngueltig(
            f"Die geladene Kurstabelle endet am {neu.letzter_tag:%d.%m.%Y}, die vorhandene "
            f"reicht bis {vorher.letzter_tag:%d.%m.%Y} — die vorhandene bleibt."
        )
    if vorher is not None and neu.erster_tag > vorher.erster_tag:
        raise TabelleUngueltig(
            f"Die geladene Kurstabelle beginnt erst am {neu.erster_tag:%d.%m.%Y}, die "
            f"vorhandene am {vorher.erster_tag:%d.%m.%Y} — eine gekürzte Fassung; die "
            "vorhandene bleibt."
        )
    if vorher is not None:
        fehlend = sorted(code for code, (tage, _) in vorher.reihen.items()
                         if tage and code not in neu.reihen)
        if fehlend:
            raise TabelleUngueltig(
                f"In der geladenen Kurstabelle fehlt {', '.join(fehlend)}, die vorhandene "
                "führt es — die vorhandene bleibt."
            )
    zeit = _jetzt_utc(jetzt)
    ziel = ordner(datenordner)
    _atomar_schreiben(ziel / ROHDATEI, rohdaten)
    _atomar_schreiben(ziel / STANDDATEI, json.dumps({
        "quelle": QUELLE, "abgerufen_utc": zeit, "sha256": neu.sha256,
        "groesse": len(rohdaten), "erster_tag": neu.erster_tag.isoformat(),
        "letzter_tag": neu.letzter_tag.isoformat(),
    }, ensure_ascii=False, indent=2).encode("utf-8"))
    return Tabelle(neu.sha256, zeit, neu.erster_tag, neu.letzter_tag, neu.reihen)


def sichere_fassung(datenordner: Path, sha256: str | None) -> Path | None:
    """Haelt die Fassung ``sha256`` unter ``kurse/verwendet/`` fest.

    Aufgerufen, wenn ein Beleg mit einem Kurs aus dieser Fassung gebucht oder
    umgerechnet wird. Eine harte Verknuepfung kostet keinen Platz, solange die
    Fassung die aktuelle ist; ersetzt der naechste Abruf die Rohdatei, bleibt
    die verwendete hier stehen. Kann das Laufwerk keine harten
    Verknuepfungen, wird kopiert. Gibt den Pfad zurueck, None ohne Erfolg --
    der Kurs am Beleg ist auch so dokumentiert, nur nicht mehr aus genau
    dieser Datei nachrechenbar.
    """
    if not sha256 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return None
    ziel = ordner(datenordner) / VERWENDET / f"{sha256}.zip"
    if ziel.is_file():
        return ziel
    aktuell = ordner(datenordner) / ROHDATEI
    try:
        daten = aktuell.read_bytes()
        if pruefsumme(daten) != sha256:
            return None
        ziel.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(aktuell, ziel)
        except FileExistsError:
            pass
        except OSError:
            _atomar_schreiben(ziel, daten)
    except OSError:
        return None
    return ziel


#: Je (Fassung, Waehrung) die Kursreihe -- fuer "Kurs prüfen". Klein (eine
#: Reihe sind gut 7.000 Tage), begrenzt wie der Zwischenspeicher oben.
_REIHEN: dict[tuple[str, str], Tabelle] = {}
REIHEN_HOECHSTENS = 32


def _reihe_lesen(text: str, waehrung: str, sha256: str) -> Tabelle:
    """Nur die Spalte einer Waehrung aus der CSV -- ohne die volle Pruefung.

    Fuer eine abgelegte Fassung, deren Pruefsumme eben nachgerechnet ist: sie
    hat die volle Pruefung beim Ablegen bestanden, und dieselben Bytes
    bestehen sie wieder. Die ganze Tabelle zu lesen kostete je Fassung eine
    halbe Sekunde und gut 10 MB -- bei einer Fassung je Buchungstag wurde die
    Integritaetspruefung so mit jedem Jahr langsamer.
    """
    zeilen = text.splitlines()
    kopf = [f.strip() for f in zeilen[0].split(",")] if zeilen else []
    if not kopf or kopf[0] != "Date":
        raise TabelleUngueltig("Die Kopfzeile der Kurstabelle beginnt nicht mit „Date“.")
    spalte = kopf.index(waehrung) if waehrung in kopf[1:] else None
    werte: list[tuple[date, str]] = []
    erster = letzter = None
    for zeile in zeilen[1:]:
        felder = zeile.split(",")
        roh_tag = felder[0].strip()
        if not roh_tag:
            continue
        try:
            tag = date.fromisoformat(roh_tag)
        except ValueError:
            raise TabelleUngueltig(f"„{roh_tag[:20]}“ ist kein Datum.") from None
        erster = tag if erster is None or tag < erster else erster
        letzter = tag if letzter is None or tag > letzter else letzter
        if spalte is not None and spalte < len(felder):
            roh = felder[spalte].strip()
            if roh not in ("", "N/A"):
                werte.append((tag, roh))
    if erster is None or letzter is None:
        raise TabelleUngueltig("Die Kurstabelle enthält keinen einzigen Tag.")
    werte.sort()
    reihen = ({waehrung: (tuple(t for t, _ in werte), tuple(k for _, k in werte))}
              if spalte is not None else {})
    return Tabelle(sha256=sha256, abgerufen_utc=None, erster_tag=erster, letzter_tag=letzter,
                   reihen=reihen)


def fassung_reihe(datenordner: Path, sha256: str | None,
                  waehrung: str) -> tuple[Tabelle | None, str | None]:
    """Die Kurse EINER Waehrung aus der Fassung ``sha256`` -- fuer "Kurs prüfen".

    Gibt ``(Tabelle, None)`` zurueck -- die Tabelle traegt nur diese Waehrung
    -- oder ``(None, Problem)``: die Fassung "fehlt im Datenordner", "ist
    verändert" (ihr Inhalt passt nicht mehr zu ihrer Pruefsumme), "ist nicht
    lesbar". Die Pruefsumme wird bei JEDEM Aufruf nachgerechnet (das kostet
    eine Millisekunde); zwischengespeichert ist nur das Lesen der Reihe.
    Ohne ``sha256``: die aktuelle Tabelle.
    """
    kurse_ordner = ordner(datenordner)
    if sha256 is None:
        pfad = kurse_ordner / ROHDATEI
        if not pfad.is_file():
            return None, "fehlt im Datenordner"
    elif not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return None, "ist am Beleg nicht gültig vermerkt"
    else:
        pfad = kurse_ordner / VERWENDET / f"{sha256}.zip"
        if not pfad.is_file():
            # Noch die aktuelle Tabelle? Dann liegt sie (noch) nicht unter
            # verwendet/, wenn das Sichern dort scheiterte.
            pfad = kurse_ordner / ROHDATEI
    try:
        daten = pfad.read_bytes()
    except FileNotFoundError:
        return None, "fehlt im Datenordner"
    except OSError:
        return None, "ist nicht lesbar"
    summe = pruefsumme(daten)
    if sha256 is not None and summe != sha256:
        if pfad.name == ROHDATEI:
            return None, "fehlt im Datenordner"
        return None, "ist verändert (ihr Inhalt passt nicht mehr zu ihrer Prüfsumme)"
    schluessel = (summe, waehrung)
    with _ZWISCHENSPEICHER_SPERRE:
        gemerkt = _REIHEN.pop(schluessel, None)
        if gemerkt is not None:
            _REIHEN[schluessel] = gemerkt
    if gemerkt is None:
        try:
            gemerkt = _reihe_lesen(entpacke(daten), waehrung, summe)
        except KursFehler:
            return None, "ist nicht lesbar"
        with _ZWISCHENSPEICHER_SPERRE:
            _REIHEN[schluessel] = gemerkt
            while len(_REIHEN) > REIHEN_HOECHSTENS:
                del _REIHEN[next(iter(_REIHEN))]
    return gemerkt, None


# ---------------------------------------------------------------- Der Abruf

class _NurHttps(urllib.request.HTTPRedirectHandler):
    """Eine Umleitung nur auf eine andere HTTPS-Adresse."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        if not str(newurl).startswith("https://"):
            raise AbrufFehler("Die EZB-Adresse leitete auf eine Adresse ohne HTTPS um — "
                              "abgebrochen.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _ssl_kontext() -> ssl.SSLContext:
    """Der uebliche, pruefende Kontext.

    Python von python.org bringt auf dem Mac keine Stammzertifikate mit, bis
    jemand "Install Certificates.command" ausfuehrt; dann ist der Speicher
    leer. macOS liefert unter /etc/ssl/cert.pem eine Sammlung mit -- die wird
    dann zusaetzlich geladen. Geprueft wird in jedem Fall.
    """
    kontext = ssl.create_default_context()
    if not kontext.cert_store_stats().get("x509_ca"):
        for kandidat in ("/etc/ssl/cert.pem",):
            if Path(kandidat).is_file():
                try:
                    kontext.load_verify_locations(cafile=kandidat)
                except (OSError, ssl.SSLError):
                    pass
    return kontext


#: Netzfehler des Betriebssystems in Worten -- statt "[Errno 101] Network is
#: unreachable" in der Meldung.
_NETZFEHLER = {
    errno.ENETUNREACH: "das Netzwerk ist nicht erreichbar — besteht eine Internetverbindung?",
    errno.ENETDOWN: "das Netzwerk ist nicht verbunden",
    errno.EHOSTUNREACH: "der Server der EZB ist nicht erreichbar",
    errno.ECONNREFUSED: "die Verbindung wurde abgewiesen",
    errno.ECONNRESET: "die Verbindung wurde unterbrochen",
    errno.ECONNABORTED: "die Verbindung wurde unterbrochen",
    errno.ETIMEDOUT: "der Server der EZB antwortete nicht",
    errno.EADDRNOTAVAIL: "keine Netzwerkadresse verfügbar — besteht eine Internetverbindung?",
}

_PROXY_STATUS = re.compile(r"Tunnel connection failed:\s*(\d{3})")


def _netzgrund(fehler: BaseException, zeitlimit: float = ZEITLIMIT_SEKUNDEN) -> str:
    """Ein Netzfehler in Worten, die der Nutzer einordnen kann -- deutsch,
    ohne den englischen Text des Betriebssystems in Klammern.

    >>> _netzgrund(OSError(101, "Network is unreachable"))
    'das Netzwerk ist nicht erreichbar — besteht eine Internetverbindung?'
    >>> _netzgrund(OSError("Tunnel connection failed: 403 Forbidden"))
    'der Proxy-Server hat die Verbindung zur EZB abgelehnt (Status 403)'
    """
    grund = getattr(fehler, "reason", fehler)
    if isinstance(grund, http.client.IncompleteRead):
        return "der Download brach vorzeitig ab"
    if isinstance(grund, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY" in str(grund):
        return ("das Zertifikat der EZB ließ sich nicht prüfen. Auf dem Mac mit Python von "
                "python.org einmal „Install Certificates.command“ im Python-Programmordner "
                "ausführen")
    if isinstance(grund, TimeoutError | socket.timeout):
        return f"keine Antwort innerhalb von {zeitlimit:g} Sekunden"
    if isinstance(grund, socket.gaierror):
        return "keine Internetverbindung (www.ecb.europa.eu nicht erreichbar)"
    if isinstance(grund, ConnectionRefusedError):
        return "die Verbindung wurde abgewiesen"
    if isinstance(grund, ConnectionResetError | ConnectionAbortedError | BrokenPipeError):
        return "die Verbindung wurde unterbrochen"
    proxy = _PROXY_STATUS.search(str(grund))
    if proxy:
        return ("der Proxy-Server hat die Verbindung zur EZB abgelehnt "
                f"(Status {proxy.group(1)})")
    if isinstance(grund, ssl.SSLError):
        return "die sichere Verbindung (TLS) zur EZB kam nicht zustande"
    if isinstance(grund, OSError) and grund.errno is not None:
        if grund.errno in _NETZFEHLER:
            return _NETZFEHLER[grund.errno]
        from app.logik.backup import fehlertext

        return fehlertext(OSError(grund.errno, grund.strerror)).rstrip(".")
    return "keine Verbindung zur EZB"


#: In Stuecken dieser Groesse wird gelesen -- dazwischen zaehlt die Frist.
STUECK_BYTES = 64 * 1024


def _restzeit(antwort: object, rest: float) -> None:
    """Die Wartezeit des Sockets auf die restliche Frist kuerzen, wo es geht
    (http.client: antwort.fp.raw._sock). Sonst bliebe ein stockender Server
    bis zu einer vollen Zeitgrenze je Leseschritt."""
    try:
        antwort.fp.raw._sock.settimeout(max(rest, 0.05))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def _lies_mit_frist(antwort: object, hoechstens: int, frist: float, zeitlimit: float) -> bytes:
    """Liest die Antwort in Stuecken -- hoechstens ``hoechstens`` Bytes und
    nur bis zur Frist (``time.monotonic()``).

    Die Zeitgrenze von urllib gilt je Leseoperation, nicht fuer den ganzen
    Abruf: ein Server, der alle halbe Sekunde ein Kilobyte schickt, hielt die
    Pruefmaske sonst minutenlang fest. ``read1`` liest je Aufruf hoechstens
    einmal vom Netz, so kommt die Frist zwischen zwei Stuecken zum Zug.
    """
    lesen = getattr(antwort, "read1", None) or antwort.read  # type: ignore[attr-defined]
    teile: list[bytes] = []
    gelesen = 0
    while True:
        rest = frist - time.monotonic()
        if rest <= 0:
            raise AbrufFehler("Die Kurstabelle ließ sich nicht laden: keine vollständige "
                              f"Antwort innerhalb von {zeitlimit:g} Sekunden.")
        _restzeit(antwort, rest)
        try:
            stueck = lesen(min(STUECK_BYTES, hoechstens + 1 - gelesen))
        except (OSError, http.client.HTTPException) as fehler:
            if not gelesen:
                raise
            # Ein Teil kam an: weder "keine Antwort" noch "abgewiesen".
            if isinstance(fehler, TimeoutError | socket.timeout):
                warum = f"keine vollständige Antwort innerhalb von {zeitlimit:g} Sekunden"
            else:
                warum = _netzgrund(fehler, zeitlimit)
            raise AbrufFehler(f"Die Kurstabelle ließ sich nicht laden: der Download brach nach "
                              f"{_bytes_de(gelesen)} Bytes ab ({warum}).") from None
        if not stueck:
            return b"".join(teile)
        teile.append(stueck)
        gelesen += len(stueck)
        if gelesen > hoechstens:
            raise AbrufFehler(_zu_gross(hoechstens))


def _bytes_de(anzahl: int) -> str:
    return f"{anzahl:,}".replace(",", ".")


def abrufen_https(url: str = QUELLE, *, oeffne: Callable | None = None,
                  zeitlimit: float = ZEITLIMIT_SEKUNDEN,
                  hoechstens: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """Holt die Datei: nur HTTPS, mit Zeitgrenze und Groessengrenze.

    Die Zeitgrenze gilt fuer den GANZEN Abruf, nicht je Leseschritt
    (:func:`_lies_mit_frist`). Bricht die Verbindung ab, bevor die
    angekuendigte Laenge (Content-Length) da ist, sagt die Meldung das --
    statt spaeter "ZIP-Archiv beschädigt".

    Ohne Abfrageparameter, ohne Referer (urllib setzt keinen), ohne Cookies
    (kein Cookie-Speicher). Ein Proxy aus der Umgebung (HTTPS_PROXY) wird
    benutzt, wie es urllib immer tut. ``oeffne`` ersetzt das Oeffnen der
    Verbindung -- fuer Tests, die die Grenzen pruefen, ohne ins Netz zu gehen.
    """
    frist = time.monotonic() + zeitlimit
    if not url.startswith("https://"):
        raise AbrufFehler("Die Kurstabelle wird nur über HTTPS geladen.")
    if "?" in url or "#" in url:
        raise AbrufFehler("Die Adresse der Kurstabelle trägt keine Parameter.")
    if oeffne is None:
        if os.environ.get(OHNE_NETZ):
            raise AbrufFehler("Netzzugriff ist abgeschaltet (STEUER_TOOL_OHNE_NETZ).")
        oeffne = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_kontext()), _NurHttps()).open
    anfrage = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "Steuer-Tool (EZB-Referenzkurse)", "Accept": "application/zip"},
    )
    try:
        with oeffne(anfrage, timeout=zeitlimit) as antwort:
            if not str(antwort.geturl()).startswith("https://"):
                raise AbrufFehler("Die EZB-Adresse leitete auf eine Adresse ohne HTTPS um — "
                                  "abgebrochen.")
            status = getattr(antwort, "status", 200)
            if status != 200:
                raise AbrufFehler(f"Die EZB antwortete mit Status {status}.")
            laenge = (antwort.headers.get("Content-Length") or "").strip()
            if laenge.isdigit() and int(laenge) > hoechstens:
                raise AbrufFehler(_zu_gross(hoechstens))
            daten = _lies_mit_frist(antwort, hoechstens, frist, zeitlimit)
    except AbrufFehler:
        raise
    except urllib.error.HTTPError as fehler:
        raise AbrufFehler(f"Die EZB antwortete mit Status {fehler.code}.") from None
    except (urllib.error.URLError, OSError, ssl.SSLError, http.client.HTTPException,
            ValueError) as fehler:
        raise AbrufFehler("Die Kurstabelle ließ sich nicht laden: "
                          f"{_netzgrund(fehler, zeitlimit)}.") from None
    if laenge.isdigit() and len(daten) < int(laenge):
        raise AbrufFehler(f"Die Kurstabelle ließ sich nicht laden: der Download brach nach "
                          f"{_bytes_de(len(daten))} von {_bytes_de(int(laenge))} Bytes ab.")
    return daten


def _zu_gross(hoechstens: int) -> str:
    return (f"Die Antwort ist größer als {hoechstens // 1024 // 1024} MB — das ist nicht die "
            "Kurstabelle der EZB. Abgebrochen.")


#: Der Abruf, den :func:`aktualisiere` benutzt. Ein Modulattribut, damit Tests
#: ihn ersetzen koennen -- ein Test geht nie ins Netz.
netzabruf: Callable[[str], bytes] = abrufen_https

_ABRUF_SPERRE = threading.Lock()
#: Je Datenordner: Zeitpunkt (time.time()) und Text des letzten Fehlschlags,
#: und der Zeitpunkt des letzten gelungenen Abrufs in diesem Prozess.
_FEHLSCHLAG: dict[str, tuple[float, str]] = {}
_ERFOLG: dict[str, float] = {}


def _schluessel(datenordner: Path) -> str:
    try:
        return str(Path(datenordner).resolve())
    except OSError:
        return str(datenordner)


def aktualisiere(datenordner: Path, *, abruf: Callable[[str], bytes] | None = None,
                 jetzt: datetime | None = None) -> Tabelle:
    """Laedt die Tabelle neu und legt sie ab. Gibt die abgelegte Tabelle zurueck.

    Ein Abruf zur Zeit je Prozess. Wer wartet, waehrend ein anderer Faden
    laedt (Start im Hintergrund und die Pruefmaske gleichzeitig), bekommt
    dessen Ergebnis -- es wird nicht zweimal geladen. Scheitert der Abruf,
    bleibt die vorhandene Tabelle unveraendert, und :class:`AbrufFehler` bzw.
    :class:`TabelleUngueltig` sagt, warum.
    """
    schluessel = _schluessel(datenordner)
    beginn = time.time()
    with _ABRUF_SPERRE:
        if _ERFOLG.get(schluessel, 0) >= beginn:
            vorhanden = tabelle(datenordner)
            if vorhanden is not None:
                return vorhanden
        fehlschlag = _FEHLSCHLAG.get(schluessel)
        if fehlschlag is not None and fehlschlag[0] >= beginn:
            raise AbrufFehler(fehlschlag[1])
        try:
            rohdaten = (abruf or netzabruf)(QUELLE)
            neu = speichere(datenordner, rohdaten, jetzt=jetzt, erster_abruf_pruefen=True)
        except KursFehler as fehler:
            _FEHLSCHLAG[schluessel] = (time.time(), str(fehler))
            raise
        except OSError as fehler:
            from app.logik.backup import fehlertext

            text = f"Die Kurstabelle ließ sich nicht speichern: {fehlertext(fehler)}"
            _FEHLSCHLAG[schluessel] = (time.time(), text)
            raise AbrufFehler(text) from None
        _FEHLSCHLAG.pop(schluessel, None)
        _ERFOLG[schluessel] = time.time()
        return neu


def _zeit(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        zeit = datetime.fromisoformat(text)
    except ValueError:
        return None
    return zeit if zeit.tzinfo else zeit.replace(tzinfo=UTC)


def letzter_fehlschlag(datenordner: Path) -> str | None:
    """Der Grund des letzten gescheiterten Abrufs, solange er frisch ist."""
    fehlschlag = _FEHLSCHLAG.get(_schluessel(datenordner))
    if fehlschlag is None:
        return None
    if time.time() - fehlschlag[0] > NEUER_VERSUCH_NACH_FEHLER.total_seconds():
        return None
    return fehlschlag[1]


def stand(datenordner: Path) -> dict[str, object] | None:
    """Was ``stand.json`` ueber die abgelegte Tabelle sagt -- fuer die Anzeige,
    ohne die Tabelle selbst zu lesen. None, wenn keine abgelegt ist."""
    if not (ordner(datenordner) / ROHDATEI).is_file():
        return None
    return _stand(datenordner)


def abgerufen_vor(datenordner: Path, *, jetzt: datetime | None = None) -> timedelta | None:
    """Wie lange der letzte Abruf her ist -- ohne die Tabelle zu lesen.

    Fuer den Start: die CSV zu pruefen und zu lesen kostet eine gute halbe
    Sekunde, und darauf soll der Start nicht warten. Massgeblich ist
    ``stand.json``, ersatzweise die Aenderungszeit der Rohdatei. None heisst:
    noch keine Tabelle.

    Passt die Rohdatei nicht zur Pruefsumme in ``stand.json`` (abgeschnitten,
    beschaedigt, von Hand ersetzt) oder ist sie nicht lesbar, gilt sie als
    veraltet (``timedelta.max``): der Start laedt sie neu, statt bis zum
    naechsten Tag mit einer Tabelle zu leben, die sich nicht lesen laesst. Die
    Pruefsumme kostet eine Millisekunde, nicht die halbe Sekunde des Lesens.
    """
    pfad = ordner(datenordner) / ROHDATEI
    try:
        info = pfad.stat()
        daten = pfad.read_bytes()
    except OSError:
        return None if not pfad.exists() else timedelta.max
    stand = _stand(datenordner)
    if stand.get("sha256") != pruefsumme(daten):
        return timedelta.max
    zeit = _zeit(str(stand.get("abgerufen_utc") or "")) or datetime.fromtimestamp(
        info.st_mtime, UTC)
    return (jetzt or datetime.now(UTC)) - zeit


def _abruf_lohnt(datenordner: Path, tab: Tabelle | None, jetzt: datetime | None) -> bool:
    if letzter_fehlschlag(datenordner) is not None:
        return False
    if tab is None:
        return True
    abgerufen = _zeit(tab.abgerufen_utc)
    return abgerufen is None or (jetzt or datetime.now(UTC)) - abgerufen > NEUER_VERSUCH_NACH_ABRUF


# ------------------------------------------------------------ Der Kurs zum Tag

def _de(tag: date) -> str:
    return tag.strftime("%d.%m.%Y")


def kurs_aus_tabelle(tab: Tabelle, waehrung: str, stichtag: date) -> Kurs:
    """Der Kurs fuer ``stichtag`` aus ``tab``.

    Gilt der Kurs des Stichtags; ist an ihm keiner veroeffentlicht (Wochenende,
    TARGET-Feiertag), der letzte davor -- hoechstens
    :data:`HOECHSTENS_ZURUECK_TAGE` Tage zurueck. Liegt zwischen dem letzten
    Tag der Tabelle und dem Stichtag ein Geschaeftstag, kann sein Kurs noch
    fehlen: dann :class:`KeinKurs` mit ``grund="noch_nicht"``.
    """
    if waehrung not in tab.reihen:
        raise KeinKurs(f"Für {waehrung} veröffentlicht die EZB keinen Referenzkurs.",
                       "unbekannt")
    if stichtag > tab.letzter_tag:
        offen = erster_geschaeftstag(tab.letzter_tag, stichtag)
        if offen is not None:
            raise KeinKurs(
                f"Der EZB-Kurs für den {_de(offen)} ist noch nicht veröffentlicht (die "
                f"Kurstabelle reicht bis {_de(tab.letzter_tag)}; die EZB veröffentlicht "
                "werktags gegen 16 Uhr).", "noch_nicht")
    tage, kurse = tab.reihen[waehrung]
    stelle = bisect_right(tage, stichtag)
    if stelle == 0:
        raise KeinKurs(f"Für {waehrung} gibt es vor dem {_de(stichtag)} keinen EZB-Kurs.",
                       "luecke")
    kursdatum = tage[stelle - 1]
    if (stichtag - kursdatum).days > HOECHSTENS_ZURUECK_TAGE:
        raise KeinKurs(
            f"Für {waehrung} hat die EZB seit dem {_de(kursdatum)} keinen Kurs "
            f"veröffentlicht — mehr als {HOECHSTENS_ZURUECK_TAGE} Tage vor dem "
            f"{_de(stichtag)}.", "luecke")
    return Kurs(waehrung=waehrung, kurs=kurse[stelle - 1], kursdatum=kursdatum,
                stichtag=stichtag, quelle="EZB", abgerufen_utc=tab.abgerufen_utc,
                sha256=tab.sha256)


def kurs_besorgen(datenordner: Path, waehrung: str, stichtag: date, *,
                  heute: date | None = None, abruf: Callable[[str], bytes] | None = None,
                  jetzt: datetime | None = None) -> Kurs:
    """Der EZB-Kurs fuer ``stichtag`` -- und wenn noetig vorher die Tabelle laden.

    Geladen wird, wenn noch keine Tabelle da ist oder ihr der Stichtag noch
    fehlt -- nicht oefter als alle paar Minuten (siehe
    :data:`NEUER_VERSUCH_NACH_FEHLER`). Ohne Netz geht es mit der vorhandenen
    Tabelle weiter.
    """
    heute = heute or date.today()
    if stichtag > heute:
        raise KeinKurs(f"Der {_de(stichtag)} liegt in der Zukunft — dafür gibt es noch "
                       "keinen Kurs.", "zukunft")
    kaputt = None
    try:
        tab = tabelle(datenordner)
    except KursFehler as fehler:
        tab, kaputt = None, str(fehler)
    except OSError as fehler:
        from app.logik.backup import fehlertext

        grund_text = (fehlertext(OSError(fehler.errno, fehler.strerror)).rstrip(".")
                      if fehler.errno is not None else "Lesefehler")
        tab, kaputt = None, f"Die Kurstabelle ist nicht lesbar ({grund_text})."
    fehlt = tab is None or (
        stichtag > tab.letzter_tag
        and erster_geschaeftstag(tab.letzter_tag, stichtag) is not None)
    grund = None
    if fehlt:
        if _abruf_lohnt(datenordner, tab, jetzt):
            try:
                tab = aktualisiere(datenordner, abruf=abruf, jetzt=jetzt)
            except KursFehler as fehler:
                grund = str(fehler)
        else:
            grund = letzter_fehlschlag(datenordner)
    if tab is None:
        if kaputt and grund:
            text = (f"Die gespeicherte EZB-Kurstabelle ist nicht lesbar ({klein(kaputt)}) und "
                    f"ließ sich nicht neu laden: {ursache(grund)}.")
        elif kaputt:
            text = f"Die gespeicherte EZB-Kurstabelle ist nicht lesbar: {klein(kaputt)}."
        elif grund:
            text = f"Die EZB-Kurstabelle ist noch nicht geladen: {ursache(grund)}."
        else:
            text = "Die EZB-Kurstabelle ist noch nicht geladen."
        raise KeinKurs(text, "keine_tabelle")
    try:
        return kurs_aus_tabelle(tab, waehrung, stichtag)
    except KeinKurs as fehler:
        if grund and fehler.grund == "noch_nicht":
            # Offline mit einer Tabelle, die vor dem Stichtag endet: nicht
            # "noch nicht veroeffentlicht" (das stimmt womoeglich nicht),
            # sondern was wirklich los ist.
            raise KeinKurs(
                f"Die EZB-Kurstabelle reicht bis {_de(tab.letzter_tag)} und ließ sich nicht "
                f"aktualisieren: {ursache(grund)}. Den Kurs für den {_de(stichtag)} kennt das "
                "Tool deshalb noch nicht.", "nicht_aktuell") from None
        raise


def klein(text: str) -> str:
    """Ein Satz als Fortsetzung nach Doppelpunkt oder Gedankenstrich: klein
    weiter, ohne Schlusspunkt. Abkuerzungen ("EZB") bleiben, wie sie sind.

    >>> klein("Das ZIP-Archiv ist beschädigt."), klein("EZB antwortete nicht.")
    ('das ZIP-Archiv ist beschädigt', 'EZB antwortete nicht')
    """
    text = text.strip().rstrip(".")
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text


#: Saetze, mit denen ein Abruffehler beginnt -- als Ursache in einer
#: anderen Meldung waeren sie doppelt ("... nicht geladen: Die Kurstabelle
#: ließ sich nicht laden: ...").
_ABRUF_VORSAETZE = ("Die Kurstabelle ließ sich nicht laden: ",
                    "Die Kurstabelle ließ sich nicht speichern: ")


def ursache(text: str) -> str:
    """Der Kern eines Abruffehlers, als Fortsetzung eines Satzes.

    >>> ursache("Die Kurstabelle ließ sich nicht laden: keine Internetverbindung.")
    'keine Internetverbindung'
    """
    for vorsatz in _ABRUF_VORSAETZE:
        if text.startswith(vorsatz):
            text = text[len(vorsatz):]
            break
    return klein(text)


# ------------------------------------------------------------ Rechnen und Text

def in_euro_cent(fremd_cent: int, kurs: str | Decimal) -> int:
    """Fremdbetrag / Kurs, kaufmaennisch auf ganze Cent -- exakt, ohne Fliesskomma.

    Der Kurs wird als Bruch genommen und in ganzen Zahlen gerechnet; ein
    halber Cent rundet auf (vom Betrag weg), nicht zur geraden Ziffer.

    >>> in_euro_cent(10000, "1.0856")     # 100,00 USD zum Kurs 1,0856
    9211
    >>> in_euro_cent(802, "0.8")          # genau 10,025 EUR -> 10,03
    1003
    >>> in_euro_cent(1234567, "161.47")   # 12.345,67 JPY
    7646
    """
    wert = Decimal(kurs)
    if not wert.is_finite() or wert <= 0:
        raise ValueError(f"Kein gültiger Kurs: {kurs}")
    zaehler, nenner = wert.as_integer_ratio()
    betrag = abs(int(fremd_cent))
    cent = (2 * betrag * nenner + zaehler) // (2 * zaehler)
    return -cent if fremd_cent < 0 else cent


def kurs_de(kurs: str | Decimal | None) -> str:
    """Ein Kurs in deutscher Schreibweise, mit allen Stellen.

    >>> kurs_de("1.0856"), kurs_de("161.47"), kurs_de("20268.06"), kurs_de("144")
    ('1,0856', '161,47', '20.268,06', '144')
    >>> kurs_de("1E-7")
    '0,0000001'
    """
    if kurs is None or kurs == "":
        return ""
    text = str(kurs)
    if "E" in text.upper():
        # Decimal schreibt sehr kleine Zahlen als "1E-7" -- angezeigt wurde
        # daraus "1.E-7". Ein Kurs steht immer ausgeschrieben da.
        text = format(Decimal(text), "f")
    ganz, _, rest = text.partition(".")
    gruppen = []
    while len(ganz) > 3:
        gruppen.insert(0, ganz[-3:])
        ganz = ganz[:-3]
    ganz = ".".join([ganz, *gruppen]) if ganz else ".".join(gruppen)
    return f"{ganz},{rest}" if rest else ganz


def lies_kurs(text: str | None) -> str | None:
    """Ein von Hand eingetragener Kurs als Text in Punktschreibweise.

    Das Komma ist Dezimaltrenner, ein Punkt davor Tausendertrenner. Ohne
    Komma gilt ein Punkt als Dezimaltrenner -- bei Kursen ist "1.0856"
    weit haeufiger gemeint als 1.085,6. Leer ergibt None.

    >>> lies_kurs("1,0856"), lies_kurs("1.0856"), lies_kurs("17.520,85"), lies_kurs("")
    ('1.0856', '1.0856', '17520.85', None)
    >>> lies_kurs("0,0000001")
    '0.0000001'
    """
    roh = (text or "").strip().replace(" ", "").replace(" ", "")
    if not roh:
        return None
    if "," in roh:
        roh = roh.replace(".", "").replace(",", ".")
    if not _ZAHL.fullmatch(roh):
        raise ValueError(f"„{(text or '').strip()[:30]}“ ist kein Kurs — bitte als Zahl "
                         "eintragen, etwa 1,0856 (1 Euro = 1,0856 Einheiten der Währung).")
    try:
        wert = Decimal(roh)
    except InvalidOperation:
        raise ValueError(f"„{(text or '').strip()[:30]}“ ist kein Kurs.") from None
    if wert <= 0:
        raise ValueError("Ein Kurs muss größer als null sein.")
    if wert >= KURS_HOECHSTENS:
        raise ValueError(f"Der Kurs {kurs_de(format(wert, 'f'))} ist unplausibel groß — 1 Euro "
                         f"sind nie {kurs_de(str(KURS_HOECHSTENS))} Einheiten oder mehr.")
    # Ausgeschrieben, nie in Exponentenschreibweise ("1E-7").
    return format(wert, "f")

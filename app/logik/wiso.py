"""Import aus WISO Steuer: der CSV-Export "Buchungen" -- wiederholbar und abgleichend.

Der Nutzer hat bisher in WISO Steuer (EUeR-Modul) gebucht und fuehrt beides
eine Zeit lang parallel. Er spielt deshalb nicht einmal eine Datei ein,
sondern immer wieder den neuesten Export desselben Jahres -- und erwartet,
dass das Werkzeug abgleicht, was es schon hat, und nur das Neue hinzufuegt.

Das Format
----------
UTF-8 (oder Windows-1252), Semikolon, deutsche Dezimalkommas, Datum
TT.MM.JJJJ, Kopfzeile wie :data:`KOPF`. Kontenrahmen SKR03. Je Zeile ein
Sachkonto und ein Gegenkonto (meist 1200 Bank), der Betrag in Soll oder Haben
des Sachkontos:

* Erloeskonto (8xxx) im Haben: Betriebseinnahme -- eine Einnahme ohne eigene
  Rechnung (app/logik/einnahmen.py), eingestuft je Zahler (Inland, Ausland,
  Anlagenverkauf, Erstattung).
* Aufwandskonto (4xxx) im Soll: Betriebsausgabe -- ein Beleg ohne Datei
  ("aus WISO uebernommen · Datei fehlt"), mit Zahlung am Zahlungsdatum. Im
  Haben: eine Erstattung (siehe unten).
* Anlagekonto (unter 1000) im Soll mit "[Anl.-Nr.: n]": Anschaffung eines
  Anlageguts. Die Nutzungsdauer leitet der Import aus der AfA-Zeile ab.
* Gegenkonto 4830 (Abschreibungen), Datum 31.12.: die von WISO geplante
  Jahres-AfA. Sie wird NICHT gebucht -- die AfA rechnet das Werkzeug selbst
  --, dient aber dem Abgleich, der Nutzungsdauer und, fuer ein Gut aus einem
  Vorjahr, der Liste "Angaben fehlen".
* 1800 Privatentnahmen: keine Betriebsausgabe, "privat, übersprungen".

Erstattungen (Aufwandskonto im Haben) werden Einnahmen der Kategorie
"Erstattung einer Betriebsausgabe (kein Umsatz)". Eine Minderung der Ausgabe
derselben Kategorie gibt es im Werkzeug nicht: ein Beleg traegt einen
Betrag ueber null, und die EUeR zaehlt jede Zahlung auf einen Beleg als
Abfluss (euer.ausgaben summiert ``ABS(betrag_cent)``); eine Gutschrift laesst
die Belegerfassung bewusst im Eingang liegen. Der Gewinn ist derselbe -- nur
steht die Erstattung in KZ 111 statt in der Ausgabenkennzahl, und die
Abgleich-Ansicht weist sie in einer eigenen Zeile aus.

Der Abgleich
------------
Schluessel einer Buchung ist (Jahr des Exports, Buchungsnummer,
Teilbuchung), dazu eine Pruefsumme der fachlichen Felder
(:meth:`Zeile.pruefsumme`). Beim erneuten Import:

* bekannt und unveraendert: uebersprungen;
* bekannt und in WISO geaendert: "in WISO geändert" mit vorher/nachher --
  "übernehmen" (der alte Eintrag bekommt die Loeschmarkierung, der neue
  entsteht, beides im Journal) oder "ignorieren";
* frueher uebernommen, im neuen Export desselben Jahres nicht mehr da: "in
  WISO gelöscht?" -- "löschen" (Loeschmarkierung) oder "behalten";
* frueher uebernommen, in WISO aber auf ein Konto ohne Geldfluss umgebucht
  (Privat, AfA, Umbuchung) oder auf einen Zahltag in der Zukunft verschoben:
  "in WISO umgebucht" -- "im Tool löschen" oder "im Tool behalten". Die Zeile
  steht ja noch in der Datei; ohne diese Gruppe blieb der Eintrag still
  stehen, und EUeR und § 19-Umsatz waren zu hoch.

Nichts wird still ueberschrieben. Gibt es im Werkzeug schon einen passenden
Eintrag (gleicher Betrag, Datum +/- 7 Tage, aehnlicher Name) -- einen
hochgeladenen Beleg, eine Zahlung auf eine eigene Rechnung, eine von Hand
erfasste Einnahme --, schlaegt die Vorschau "verknüpfen" vor: kein neuer
Eintrag, nur der WISO-Schluessel wird daran vermerkt.

Zwei Schritte, nie auf einen veralteten Stand
--------------------------------------------
:func:`plane` liest und schreibt nichts. :func:`uebernehmen` liest dieselbe
Datei NOCH EINMAL, plant unter der Schreibsperre neu und bricht ab, wenn das
Ergebnis nicht mehr dem der Vorschau entspricht (:class:`VorschauVeraltet`).
Aus dem Formular kommen nur Entscheidungen (Kategorie je Konto, Einstufung
je Zahler, Aktion je Zeile) -- keine Betraege, Daten oder Texte. Alles laeuft
in EINER Transaktion; die Datei liegt danach unveraendert und
schreibgeschuetzt unter ``importe/wiso/<Jahr>/`` im Datenordner.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePath

from app import db, pfade
from app.logik import anlagen, eingabe, einnahmen, kategorien
from app.logik.beleg import loeschen, pipeline
from app.logik.beleg.eigenbeleg import ohne_steuerzeichen
from app.logik.rundung import euro

#: Die Kopfzeile des Exports, in dieser Reihenfolge.
KOPF = ("Buchungsnummer", "Teilbuchung", "Zahlungsdatum", "Wertstellungsdatum",
        "Buchungstext", "Konto", "Kontobezeichnung", "Gegenkonto", "Gegenkontobezeichnung",
        "Soll", "Haben", "Steuerschluessel", "Steuersatz", "Steuerschluesselbezeichnung",
        "Belegnummer", "Buchungsdatum", "UStID", "Umsatzart")
#: Ohne diese Spalten ist es kein WISO-Export "Buchungen".
PFLICHT = ("Buchungsnummer", "Zahlungsdatum", "Buchungstext", "Konto", "Gegenkonto",
           "Soll", "Haben")

#: Groesste Datei. Ein Jahr mit 1.000 Buchungen sind rund 200 KB.
MAX_BYTES = 2 * 1024 * 1024
#: Hoechstens so viele Buchungen je Datei.
MAX_ZEILEN = 5000
#: Zeichen je Feld.
MAX_ZEICHEN = 500

#: Wo die eingespielten Dateien liegen (im Datenordner).
ARCHIV = Path("importe") / "wiso"

#: Gruppen der Vorschau.
NEU = "NEU"
BEKANNT = "BEKANNT"
GEAENDERT = "GEAENDERT"
UMGEBUCHT = "UMGEBUCHT"
PRIVAT = "PRIVAT"
AFA = "AFA"
NICHT = "NICHT"

#: Was aus einer Buchung wird (wiso_buchung.art).
AUSGABE = "AUSGABE"
EINNAHME = "EINNAHME"
ERSTATTUNG = "ERSTATTUNG"
ANLAGE = "ANLAGE"

#: Aktionen je Zeile.
UEBERNEHMEN = "uebernehmen"
VERKNUEPFEN = "verknuepfen"
AUSLASSEN = "auslassen"
SPAETER = "spaeter"
IGNORIEREN = "ignorieren"
LOESCHEN = "loeschen"
BEHALTEN = "behalten"

#: Vorbelegte Zuordnung SKR03 -> Kategorie des Werkzeugs. In der Vorschau je
#: Konto aenderbar und dann gemerkt (Tabelle wiso_konto).
KONTO_VORGABEN = {
    "4210": "MIETE_BUERO",
    "4360": "BEITRAEGE_VERSICHERUNG",
    "4380": "BEITRAEGE_VERSICHERUNG",
    "4600": "WERBUNG",
    "4673": "REISE_FAHRTKOSTEN",
    "4674": "REISE_VERPFLEGUNG",
    "4676": "REISE_UEBERNACHTUNG",
    "4806": "IT_DIENSTLEISTUNG",
    "4855": "HARDWARE_GWG",
    "4900": "UEBRIGE",
    "4910": "BUEROBEDARF",
    "4920": "TELEFON_INTERNET",
    "4930": "BUEROBEDARF",
    "4940": "BUEROBEDARF",
    "4945": "FORTBILDUNG",
    "4950": "STEUERBERATUNG",
    "4955": "STEUERBERATUNG",
    "4970": "BANKGEBUEHREN",
    "4980": "UEBRIGE",
}

#: Ein Software-Titel unter "Sofortabschreibung GWG" ist eine Dauerlizenz.
SOFTWARE = re.compile(r"\b(software|lizenz|license|windows|office|adobe|"
                      r"photoshop|lightroom|final ?cut|logic pro|app)\b", re.IGNORECASE)

#: "[Anl.-Nr.: 2]" im Buchungstext einer Anschaffung oder AfA.
ANLAGENNUMMER = re.compile(r"\[\s*Anl\.?\s*-?\s*Nr\.?\s*:?\s*([0-9A-Za-z]+)\s*\]")

#: Wie weit Datum eines vorhandenen Eintrags und Zahlungsdatum auseinander
#: liegen duerfen, damit die Vorschau "verknüpfen" vorschlaegt.
TAGE_ABSTAND = 7
#: Bis zu so vielen Tagen Abstand ist ein Treffer mit gleichem Betrag auch
#: ohne aehnlichen Namen ein sicherer Vorschlag (Kartenzahlung, Lastschrift).
TAGE_SICHER = 2
#: Ein Beleg mit Zahlungsziel: im Tool ist der Zahltag (per Vorgabe) das
#: Belegdatum, in WISO das Bankdatum -- bei 14 oder 30 Tagen Ziel weit mehr
#: als TAGE_ABSTAND. Bei aehnlichem Namen und gleichem Betrag bis zu so
#: vielen Tagen ein Kandidat, aber nur angeboten (ein Monatsabo mit gleichem
#: Betrag laege sonst auf dem Vormonat).
TAGE_ZIEL = 45

#: Woerter, die zwei Namen nicht als aehnlich ausweisen: Rechtsformen,
#: Laender, Allerweltswoerter von Plattformen.
_ALLGEMEIN = frozenset({
    "gmbh", "mbh", "ag", "kg", "ug", "co", "ltd", "limited", "inc", "llc", "sarl", "sa", "bv",
    "se", "eu", "de", "ireland", "germany", "deutschland", "europe", "international", "the",
    "und", "and", "der", "die", "das", "abo", "jahresabo", "monatsabo", "rechnung", "zahlung",
    "payout", "ads", "affiliate", "online", "services", "service", "platforms", "operations",
    "www", "com", "net", "verkauf", "zusendung", "tool", "app", "premium", "jahresgebuehr",
})

#: Allgemeine Zusaetze am Ende eines Buchungstexts, die keinen Lieferanten
#: unterscheiden: "Beispiel Jahresabo" ist der Lieferant Beispiel.
_ZUSAETZE = re.compile(
    r"\b(jahresabo|monatsabo|abo|abonnement|jahresgeb(?:ü|ue)hr|monatsgeb(?:ü|ue)hr|ads?)\s*$",
    re.IGNORECASE)


class ImportFehler(ValueError):
    """Die Datei als Ganzes ist unbrauchbar; die Meldung sagt, was zu tun ist."""


class VorschauVeraltet(Exception):
    """Zwischen Vorschau und Bestaetigung hat sich der Bestand geaendert."""

    def __init__(self, plan: Plan) -> None:
        super().__init__("Der Bestand hat sich seit der Vorschau geändert.")
        self.plan = plan


# ================================================================== Lesen

@dataclass(frozen=True, slots=True)
class Zeile:
    """Eine Buchung der Datei, gelesen und geprueft."""

    nr: int
    buchungsnummer: str
    teilbuchung: str
    datum: str
    text: str
    konto: str
    kontobezeichnung: str
    gegenkonto: str
    gegenkontobezeichnung: str
    soll_cent: int
    haben_cent: int
    belegnummer: str
    steuerschluessel: str
    steuersatz: str
    wertstellung: str

    @property
    def schluessel(self) -> str:
        """Der Schluessel in Formularen und Meldungen: "12-1"."""
        return f"{self.buchungsnummer}-{self.teilbuchung}"

    @property
    def betrag_cent(self) -> int:
        return self.soll_cent or self.haben_cent

    def inhalt(self) -> dict[str, object]:
        """Die fachlichen Felder -- das, woran eine Aenderung in WISO zu erkennen ist.

        Nicht dabei: das Buchungsdatum (der Tag der Erfassung in WISO) und die
        Zeilennummer in der Datei -- beides aendert sich, ohne dass sich an der
        Buchung etwas aendert.
        """
        return {
            "zahlungsdatum": self.datum, "wertstellung": self.wertstellung,
            "text": self.text, "konto": self.konto, "gegenkonto": self.gegenkonto,
            "soll_cent": self.soll_cent, "haben_cent": self.haben_cent,
            "belegnummer": self.belegnummer, "steuerschluessel": self.steuerschluessel,
            "steuersatz": self.steuersatz,
        }

    def pruefsumme(self) -> str:
        return hashlib.sha256(json.dumps(self.inhalt(), sort_keys=True,
                                         ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Datei:
    """Eine gelesene WISO-Datei."""

    name: str
    sha256: str
    kodierung: str
    zeilen: list[Zeile]
    #: (Zeilennummer, Grund) der Zeilen, die sich nicht lesen liessen.
    fehler: list[tuple[int, str]]
    #: Das Steuerjahr des Exports: das Jahr der meisten Zahlungsdaten.
    jahr: int
    #: Anzahl Zeilen mit Zahlungsdatum in einem anderen Jahr.
    andere_jahre: dict[int, int] = field(default_factory=dict)


def pruefsumme(inhalt: bytes) -> str:
    return hashlib.sha256(inhalt).hexdigest()


def _schluesselwort(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold().replace("ß", "ss"))


def _betrag(roh: str) -> int:
    """Ein Betrag in deutscher Schreibweise als Cent.

    >>> _betrag("12,99"), _betrag("1.200,00"), _betrag("0,00"), _betrag("")
    (1299, 120000, 0, 0)
    >>> _betrag("abc")
    Traceback (most recent call last):
    ValueError: „abc“ ist kein Betrag
    """
    text = (roh or "").strip().replace("\u00a0", "").replace(" ", "")
    if not text:
        return 0
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        wert = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"„{roh.strip()[:30]}“ ist kein Betrag") from None
    if not wert.is_finite() or wert < 0 or wert >= 10**9:
        raise ValueError(f"„{roh.strip()[:30]}“ ist kein Betrag")
    cent = wert * 100
    if cent != cent.to_integral_value():
        raise ValueError(f"„{roh.strip()[:30]}“ hat mehr als zwei Nachkommastellen")
    return int(cent)


def _datum(roh: str, *, feld: str, heute: date) -> str:
    """TT.MM.JJJJ als ISO; das Jahr ab 2000, hoechstens das naechste Kalenderjahr
    (eine AfA-Zeile steht auf dem 31.12. des laufenden Jahres)."""
    return eingabe.datum(roh, feld=feld, zukunft=True, heute=heute)


def lies(name: str, inhalt: bytes, *, heute: date | None = None) -> Datei:
    """Prueft und liest eine WISO-Datei. Schreibt nichts.

    Was sich als Ganzes nicht lesen laesst, wird :class:`ImportFehler`; eine
    einzelne kaputte Zeile steht in ``Datei.fehler`` und haelt die anderen
    nicht auf.
    """
    heute = heute or date.today()
    endung = PurePath(name or "").suffix.lower()
    if endung != ".csv":
        raise ImportFehler("Bitte den CSV-Export „Buchungen“ aus WISO wählen (Endung .csv).")
    if not inhalt:
        raise ImportFehler("Die Datei ist leer.")
    if len(inhalt) > MAX_BYTES:
        raise ImportFehler(
            f"Die Datei ist zu groß ({len(inhalt) / 1024 / 1024:.1f} MB, höchstens "
            f"{MAX_BYTES // 1024 // 1024} MB).".replace(".", ",", 1))
    if inhalt.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        raise ImportFehler("Die Datei heißt .csv, ist aber eine Excel-Datei. Bitte in WISO "
                           "„Buchungen als CSV-Datei“ exportieren.")
    for kodierung, anzeige in (("utf-8-sig", "UTF-8"), ("cp1252", "Windows-1252")):
        try:
            text = inhalt.decode(kodierung)
            gelesen_als = anzeige
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover -- cp1252 nimmt fast alles; nur zur Sicherheit
        raise ImportFehler("Die Datei ist weder UTF-8 noch Windows-1252.")
    if "\x00" in text:
        raise ImportFehler("Die Datei enthält Nullzeichen — das ist keine Textdatei.")

    # Ohne Anfuehrungszeichen-Regel: WISO quotiert nicht (die echte Datei
    # enthaelt kein einziges "). Mit der Standardregel las ein getipptes " am
    # Anfang eines Buchungstexts alles bis zum Dateiende als EIN Feld -- die
    # Folgebuchungen fehlten still und erschienen beim naechsten Import unter
    # "in WISO gelöscht?". Ein Semikolon im Text verschiebt die Spalten; das
    # faellt an der Feldzahl auf und kostet nur diese Zeile.
    leser = csv.reader(io.StringIO(text, newline=""), delimiter=";", quoting=csv.QUOTE_NONE)
    index: dict[str, int] | None = None
    breite = 0
    zeilen: list[Zeile] = []
    fehler: list[tuple[int, str]] = []
    gesehen: set[tuple[str, str]] = set()
    try:
        for werte in leser:
            if index is None:
                if not any(w.strip() for w in werte):
                    continue
                kopf = {_schluesselwort(w): i for i, w in enumerate(werte)}
                fehlt = [s for s in PFLICHT if _schluesselwort(s) not in kopf]
                if fehlt:
                    raise ImportFehler(
                        "Das ist kein WISO-Export „Buchungen“: es fehlen die Spalten "
                        f"{', '.join(fehlt)}. Erwartet wird die Kopfzeile "
                        f"„{';'.join(KOPF[:6])};…“.")
                index = {s: kopf[_schluesselwort(s)] for s in KOPF if _schluesselwort(s) in kopf}
                breite = len(werte)
                continue
            if not any(w.strip() for w in werte):
                continue
            if len(zeilen) + len(fehler) >= MAX_ZEILEN:
                raise ImportFehler(f"Mehr als {MAX_ZEILEN} Buchungen in einer Datei — bitte "
                                   "je Jahr exportieren.")
            nr = leser.line_num
            if len(werte) > breite and any(w.strip() for w in werte[breite:]):
                fehler.append((nr, f"{len(werte)} statt {breite} Felder — ein Semikolon im "
                                   "Buchungstext?"))
                continue
            try:
                zeile = _zeile(nr, werte, index, heute)
            except ValueError as grund:
                fehler.append((nr, str(grund)))
                continue
            schluessel = (zeile.buchungsnummer, zeile.teilbuchung)
            if schluessel in gesehen:
                fehler.append((nr, f"Buchung {zeile.schluessel} steht zweimal in der Datei"))
                continue
            gesehen.add(schluessel)
            zeilen.append(zeile)
    except csv.Error as grund:
        raise ImportFehler(f"Die Datei ist in Zeile {leser.line_num} nicht lesbar "
                           f"(Anführungszeichen?): {grund}") from None
    if index is None:
        raise ImportFehler("Die Datei ist leer: keine Kopfzeile, keine Buchungen.")
    if not zeilen:
        raise ImportFehler("Die Datei enthält keine lesbare Buchung."
                           + (f" Erste Meldung: Zeile {fehler[0][0]}: {fehler[0][1]}."
                              if fehler else ""))
    jahre = Counter(int(z.datum[:4]) for z in zeilen)
    jahr = max(jahre, key=lambda j: (jahre[j], j))
    return Datei(name=_dateiname(name), sha256=pruefsumme(inhalt), kodierung=gelesen_als,
                 zeilen=zeilen, fehler=fehler, jahr=jahr,
                 andere_jahre={j: n for j, n in sorted(jahre.items()) if j != jahr})


def _dateiname(roh: str) -> str:
    r"""Der Name der Datei ohne Pfad und Steuerzeichen, hoechstens 80 Zeichen.

    >>> _dateiname(r"C:\Users\x\20261027_Buchungen.csv"), _dateiname("../a.csv")
    ('20261027_Buchungen.csv', 'a.csv')
    """
    name = re.split(r"[\\/]", roh or "")[-1]
    name = eingabe.STEUERZEICHEN.sub("", name).strip()
    return name[-80:] or "buchungen.csv"


def _zeile(nr: int, werte: list[str], index: dict[str, int], heute: date) -> Zeile:
    def feld(name: str) -> str:
        i = index.get(name)
        wert = werte[i] if i is not None and i < len(werte) else ""
        if eingabe.STEUERZEICHEN.search(wert):
            raise ValueError(f"Steuerzeichen in „{name}“")
        if len(wert) > MAX_ZEICHEN:
            raise ValueError(f"„{name}“ ist länger als {MAX_ZEICHEN} Zeichen")
        return " ".join(wert.split())

    nummer = feld("Buchungsnummer")
    if not nummer or not nummer.isascii() or not nummer.isalnum() or len(nummer) > 20:
        raise ValueError("ohne gültige Buchungsnummer")
    teil = feld("Teilbuchung") or "1"
    if not teil.isascii() or not teil.isalnum() or len(teil) > 10:
        raise ValueError("ohne gültige Teilbuchung")
    datum_roh = feld("Zahlungsdatum") or feld("Wertstellungsdatum")
    try:
        datum = _datum(datum_roh, feld="Zahlungsdatum", heute=heute)
    except ValueError as grund:
        raise ValueError(str(grund).rstrip(".")) from None
    wert_roh = feld("Wertstellungsdatum")
    try:
        wertstellung = _datum(wert_roh, feld="Wertstellungsdatum", heute=heute) if wert_roh \
            else ""
    except ValueError:
        wertstellung = ""
    soll, haben = _betrag(feld("Soll")), _betrag(feld("Haben"))
    if soll and haben:
        raise ValueError("Betrag in Soll UND Haben")
    if not soll and not haben:
        raise ValueError("ohne Betrag")
    konto, gegenkonto = feld("Konto"), feld("Gegenkonto")
    if not konto.isdigit() or not gegenkonto.isdigit():
        raise ValueError("Konto oder Gegenkonto ist keine Kontonummer")
    return Zeile(
        nr=nr, buchungsnummer=nummer, teilbuchung=teil, datum=datum,
        text=feld("Buchungstext"), konto=konto, kontobezeichnung=feld("Kontobezeichnung"),
        gegenkonto=gegenkonto, gegenkontobezeichnung=feld("Gegenkontobezeichnung"),
        soll_cent=soll, haben_cent=haben, belegnummer=feld("Belegnummer"),
        steuerschluessel=feld("Steuerschluessel"), steuersatz=feld("Steuersatz"),
        wertstellung=wertstellung,
    )


# ================================================================== Einordnen

def _nummer(konto: str) -> int:
    return int(konto) if konto.isdigit() else -1


def ist_geldkonto(konto: str) -> bool:
    """Kasse, Bank, Geldtransit (SKR03 1000-1299, 1360)."""
    n = _nummer(konto)
    return 1000 <= n <= 1299 or n == 1360


def ist_privatkonto(konto: str) -> bool:
    """Privatentnahmen und -einlagen (SKR03 1800-1899)."""
    return 1800 <= _nummer(konto) <= 1899


def ist_afa_konto(konto: str) -> bool:
    """Abschreibungskonten -- ohne 4855 (Sofortabschreibung GWG: eine Ausgabe)."""
    n = _nummer(konto)
    return (4820 <= n <= 4849 or 4860 <= n <= 4869) and n != 4855


def ist_anlagekonto(konto: str) -> bool:
    """Anlagevermoegen (SKR03 0001-0999)."""
    return 0 < _nummer(konto) < 1000


def ist_erloeskonto(konto: str) -> bool:
    return 8000 <= _nummer(konto) <= 8999


def ist_aufwandskonto(konto: str) -> bool:
    return 3000 <= _nummer(konto) <= 4999 and not ist_afa_konto(konto)


@dataclass(frozen=True, slots=True)
class Einordnung:
    """Was eine Zeile fachlich ist -- unabhaengig vom Bestand im Werkzeug."""

    gruppe: str                 # NEU (eine Geldbewegung), PRIVAT, AFA, NICHT
    art: str | None = None      # AUSGABE, EINNAHME, ERSTATTUNG, ANLAGE
    sachkonto: str = ""
    kontobezeichnung: str = ""
    grund: str | None = None


def einordnen(z: Zeile) -> Einordnung:
    """Ordnet eine Buchung nach Sachkonto und Richtung ein.

    Gegenkonto ist meist das Bankkonto 1200, der Betrag steht in Soll oder
    Haben des Sachkontos. Steht das Geldkonto vorn, ist die Richtung
    umgekehrt.
    """
    if ist_geldkonto(z.gegenkonto) and not ist_geldkonto(z.konto):
        sach, bez, abfluss = z.konto, z.kontobezeichnung, z.soll_cent > 0
    elif ist_geldkonto(z.konto) and not ist_geldkonto(z.gegenkonto):
        sach, bez, abfluss = z.gegenkonto, z.gegenkontobezeichnung, z.haben_cent > 0
    elif ist_geldkonto(z.konto) and ist_geldkonto(z.gegenkonto):
        return Einordnung(NICHT, grund="Geldtransit zwischen zwei Geldkonten — keine "
                                       "Betriebseinnahme und keine Ausgabe")
    else:
        if ist_afa_konto(z.konto) or ist_afa_konto(z.gegenkonto):
            anlage = z.gegenkonto if ist_afa_konto(z.konto) else z.konto
            bez = (z.gegenkontobezeichnung if ist_afa_konto(z.konto)
                   else z.kontobezeichnung)
            return Einordnung(AFA, sachkonto=anlage, kontobezeichnung=bez)
        return Einordnung(NICHT, sachkonto=z.konto, kontobezeichnung=z.kontobezeichnung,
                          grund="Umbuchung ohne Geldfluss — im Werkzeug nicht nötig")

    if ist_privatkonto(sach):
        return Einordnung(PRIVAT, sachkonto=sach, kontobezeichnung=bez,
                          grund="Privatentnahme bzw. -einlage — keine Betriebsausgabe")
    if ist_anlagekonto(sach):
        if abfluss:
            return Einordnung(NEU, ANLAGE, sach, bez)
        return Einordnung(NICHT, sachkonto=sach, kontobezeichnung=bez,
                          grund="Zufluss auf einem Anlagekonto — einen Verkauf bitte im "
                                "Tool als Abgang des Anlageguts erfassen")
    if ist_erloeskonto(sach):
        if not abfluss:
            return Einordnung(NEU, EINNAHME, sach, bez)
        return Einordnung(NICHT, sachkonto=sach, kontobezeichnung=bez,
                          grund="Rückzahlung auf einem Erlöskonto — bitte im Tool von Hand "
                                "erfassen")
    if ist_afa_konto(sach):
        return Einordnung(NICHT, sachkonto=sach, kontobezeichnung=bez,
                          grund="Zahlung auf einem Abschreibungskonto — bitte prüfen")
    if abfluss:
        return Einordnung(NEU, AUSGABE, sach, bez)
    if ist_aufwandskonto(sach):
        return Einordnung(NEU, ERSTATTUNG, sach, bez)
    return Einordnung(NEU, EINNAHME, sach, bez)


def anlagennummer(text: str) -> str | None:
    """Die Anl.-Nr. aus einem Buchungstext.

    >>> anlagennummer("Laptop Beispiel [Anl.-Nr.: 3]"), anlagennummer("ohne")
    ('3', None)
    """
    treffer = ANLAGENNUMMER.search(text or "")
    return treffer.group(1) if treffer else None


def anlagenname(text: str) -> str:
    """Der Name eines Anlageguts aus dem Buchungstext, ohne "AfA:" und Nummer.

    >>> anlagenname("AfA: Laptop Beispiel [Anl.-Nr.: 3]")
    'Laptop Beispiel'
    """
    ohne = ANLAGENNUMMER.sub("", text or "")
    ohne = re.sub(r"^\s*AfA\s*:\s*", "", ohne, flags=re.IGNORECASE)
    return " ".join(ohne.split()) or "Anlagegut"


def _worte(text: str) -> set[str]:
    zerlegt = unicodedata.normalize("NFKD", (text or "").replace("ß", "ss"))
    rein = "".join(z for z in zerlegt if not unicodedata.combining(z)).casefold()
    return {w for w in re.split(r"[^a-z0-9]+", rein)
            if len(w) >= 3 and not w.isdigit() and w not in _ALLGEMEIN}


def namen_aehnlich(a: str | None, b: str | None) -> bool:
    """Sind zwei Namen vermutlich derselbe Geschaeftspartner?

    Ein gemeinsames bedeutungstragendes Wort genuegt -- "Beispiel Ads" und
    "Beispiel Platforms Ireland Ltd" --, Rechtsformen und Laender zaehlen nicht.

    >>> namen_aehnlich("Beispiel Ads", "Beispiel Platforms Ireland Ltd")
    True
    >>> namen_aehnlich("Muster GmbH", "Beispiel GmbH")
    False
    """
    wa, wb = _worte(a or ""), _worte(b or "")
    return bool(wa & wb)


#: Eine Web-Adresse im Buchungstext: Schema und "www." fallen weg, der Pfad auch.
_ADRESSE = re.compile(r"(?:https?://|\bwww\.)(?:www\.)?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)",
                      re.IGNORECASE)


def lieferant_aus_text(text: str) -> str:
    """Ein Lieferantenname aus dem Buchungstext.

    WISO kennt keinen Lieferanten, nur einen Freitext. Steht dort "Produkt -
    Haendler", ist der kuerzere Teil meist der Haendler; Abo-Zusaetze fallen
    weg, eine lange Artikelbeschreibung wird auf ihren Anfang gekuerzt.

    >>> lieferant_aus_text("Kabel und Kanal fuer das Buero - Beispielshop")
    'Beispielshop'
    >>> lieferant_aus_text("Beispiel Cloud Jahresabo"), lieferant_aus_text("Muster Ads")
    ('Beispiel Cloud', 'Muster')
    >>> lieferant_aus_text("BEISPIEL Notizblock kariert, A5, 50 Blatt")
    'BEISPIEL Notizblock kariert'

    Ein Buchungstext, der eine Adresse ist, ergibt ihre Domain -- am ersten
    ":" getrennt hiesse jeder Shop "https":

    >>> lieferant_aus_text("https://www.beispiel-shop.example/rechnung?nr=1")
    'beispiel-shop.example'
    >>> lieferant_aus_text("Kabel - www.anderer-laden.example")
    'anderer-laden.example'
    """
    roh = " ".join((text or "").split())
    adresse = _ADRESSE.search(roh)
    if adresse is not None:
        return adresse.group(1).lower()[:80]
    teile = [t.strip() for t in re.split(r"\s+[-–—]\s+", roh) if t.strip()]
    kandidat = min(teile, key=lambda t: len(t.split())) if len(teile) > 1 else roh
    kandidat = re.split(r"[:,(]", kandidat)[0]
    kandidat = _ZUSAETZE.sub("", kandidat).strip(" -–—.;")
    name = " ".join(kandidat.split()[:4])
    return name[:80] or roh[:80] or "unbekannt"


# ================================================================== Planen

@dataclass(frozen=True, slots=True)
class Kandidat:
    """Ein vorhandener Eintrag im Werkzeug, der dieselbe Buchung sein kann."""

    typ: str            # "beleg", "einnahme", "zahlung"
    id: int
    text: str
    #: Gleicher Name oder hoechstens TAGE_SICHER Tage Abstand: Vorschlag ist
    #: "verknüpfen"; sonst nur angeboten.
    sicher: bool
    #: Der Eintrag haengt noch an einer WISO-Buchung, die im neuen Export
    #: fehlt (in WISO geloescht und neu erfasst): deren id. "verknüpfen"
    #: haengt ihn dann von der alten auf die neue Nummer um.
    vorgaenger: int | None = None
    #: Ein Beleg im Tool ohne jede Zahlung: "verknüpfen" legt die Zahlung am
    #: Zahlungsdatum aus WISO mit an -- sonst fehlte die Ausgabe in der EUeR.
    zahlung_fehlt: bool = False


@dataclass(slots=True)
class Posten:
    """Eine Buchung der Datei und was mit ihr geschieht."""

    zeile: Zeile
    gruppe: str
    art: str | None = None
    sachkonto: str = ""
    kontobezeichnung: str = ""
    grund: str | None = None
    kategorie_id: int | None = None
    kategorie: str | None = None
    #: Woher die Kategorie kommt: "gemerkt", "Vorgabe", "Zeile", "Zahler".
    kategorie_herkunft: str | None = None
    zahler: einnahmen.Zahler | None = None
    einstufung: str | None = None
    lieferant: str | None = None
    anl_nr: str | None = None
    nutzungsdauer: tuple[int, str] | None = None
    afa_wiso_cent: int | None = None
    kandidat: Kandidat | None = None
    vorschlag: str = UEBERNEHMEN
    hinweise: list[str] = field(default_factory=list)
    #: Die geltende Zeile aus wiso_buchung (bekannt, geaendert).
    alt: sqlite3.Row | None = None
    #: Bekannt, aber der Eintrag im Werkzeug ist inzwischen geloescht.
    im_tool_geloescht: bool = False
    #: Neu, weil frueher bewusst ausgelassen und in WISO seither geaendert.
    frueher_ausgelassen: bool = False
    #: Frueher uebernommen, in WISO aber keine Geldbewegung mehr: warum nicht
    #: (Privat, AfA, Zahltag in der Zukunft ...). Gruppe UMGEBUCHT -- oder
    #: BEKANNT, wenn der Nutzer den Eintrag im Tool behalten hat.
    umgebucht: str | None = None
    #: In WISO geaendert: welche Felder (fuer die Vorschau, "Text", "Betrag" ...).
    aenderungen: list[str] = field(default_factory=list)
    #: In WISO geaendert, und am bisherigen Beleg haengt schon eine Datei
    #: (nachtraeglich angehaengt): ihr Name. "übernehmen" traegt sie mit.
    datei_am_alten: str | None = None

    @property
    def schluessel(self) -> str:
        return self.zeile.schluessel

    @property
    def betrag_cent(self) -> int:
        return self.zeile.betrag_cent

    @property
    def offen(self) -> bool:
        """Anzulegen, aber ohne die Angabe, die dafuer fehlt (Kategorie, Einstufung)."""
        if self.gruppe not in (NEU, GEAENDERT):
            return False
        if self.art == AUSGABE and self.kategorie_id is None:
            return True
        return self.art == EINNAHME and self.einstufung == SPAETER


@dataclass(slots=True)
class KontoZeile:
    konto: str
    bezeichnung: str
    anzahl: int = 0
    cent: int = 0
    kategorie_id: int | None = None
    herkunft: str | None = None
    #: Ohne gemerkte und ohne vorbelegte Kategorie.
    unbekannt: bool = False


@dataclass(slots=True)
class ZahlerZeile:
    schluessel: str
    name: str
    anzahl: int = 0
    cent: int = 0
    einstufung: str = einnahmen.INLAND
    herkunft: str = ""


@dataclass(slots=True)
class AfaZeile:
    zeile: Zeile
    anl_nr: str | None
    name: str
    betrag_cent: int
    #: NEU_ANLAGE: die Anschaffung steht in derselben Datei; VERZEICHNIS: das
    #: Gut steht im Werkzeug; FEHLT: Vorjahresgut, Angaben fehlen.
    status: str
    anlagegut_id: int | None = None
    tool_cent: int | None = None
    #: Schon in "Angaben fehlen" vermerkt (bei FEHLT).
    vermerkt: bool = False


@dataclass(slots=True)
class Auswahl:
    """Die Entscheidungen aus dem Formular der Vorschau -- nur Entscheidungen."""

    konten: dict[str, int | None] = field(default_factory=dict)
    zahler: dict[str, str] = field(default_factory=dict)
    aktionen: dict[str, str] = field(default_factory=dict)
    kategorien: dict[str, int | None] = field(default_factory=dict)
    geloescht: dict[int, str] = field(default_factory=dict)


@dataclass(slots=True)
class Plan:
    datei: Datei
    posten: list[Posten]
    geloescht: list[sqlite3.Row]
    konten: list[KontoZeile]
    zahler: list[ZahlerZeile]
    afa: list[AfaZeile]
    fingerabdruck: str = ""
    #: In WISO geloescht -> neue Buchung mit demselben Eintrag als Kandidat:
    #: wiso_buchung.id -> Schluessel der neuen Buchung.
    nachfolger: dict[int, str] = field(default_factory=dict)

    def gruppe(self, gruppe: str) -> list[Posten]:
        return [p for p in self.posten if p.gruppe == gruppe]

    @property
    def neu(self) -> list[Posten]:
        """Neue Buchungen, die sich anlegen lassen (ohne Verknuepfungsvorschlag)."""
        return [p for p in self.posten if p.gruppe == NEU and not p.offen
                and not (p.kandidat and p.kandidat.sicher)]

    @property
    def verknuepfen(self) -> list[Posten]:
        return [p for p in self.posten if p.gruppe == NEU and not p.offen
                and p.kandidat is not None and p.kandidat.sicher]

    @property
    def offen(self) -> list[Posten]:
        """Neue Buchungen, denen Kategorie bzw. Einstufung fehlt."""
        return [p for p in self.posten if p.gruppe == NEU and p.offen]

    @staticmethod
    def summe(posten: list[Posten]) -> int:
        return sum(p.betrag_cent for p in posten)

    @property
    def anzahl(self) -> dict[str, int]:
        """Anzahl je Gruppe der Vorschau -- auch fuers Import-Protokoll."""
        return {
            "neu": len(self.neu), "verknuepfen": len(self.verknuepfen),
            "offen": len(self.offen), "bekannt": len(self.gruppe(BEKANNT)),
            "geaendert": len(self.gruppe(GEAENDERT)), "in_wiso_geloescht": len(self.geloescht),
            "umgebucht": len(self.gruppe(UMGEBUCHT)),
            "privat": len(self.gruppe(PRIVAT)), "afa": len(self.gruppe(AFA)),
            "nicht_uebernehmbar": len(self.gruppe(NICHT)), "unlesbar": len(self.datei.fehler),
        }


def _kategorien(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {z["schluessel"]: z for z in conn.execute("SELECT * FROM euer_kategorie")}


def _kategorie_zeile(conn: sqlite3.Connection, kategorie_id: int | None) -> sqlite3.Row | None:
    if kategorie_id is None:
        return None
    return conn.execute("SELECT * FROM euer_kategorie WHERE id = ?", (kategorie_id,)).fetchone()


def plane(conn: sqlite3.Connection, datei: Datei, auswahl: Auswahl | None = None, *,
          heute: date | None = None) -> Plan:
    """Gleicht die Datei gegen den Bestand ab. Schreibt nichts.

    ``auswahl`` (aus dem Formular) aendert nur Kategorien, Einstufungen und
    Vorschlaege -- nie die Gruppen. Der Fingerabdruck haengt deshalb nicht von
    ihr ab: er sagt, ob Bestand und Datei noch dieselben sind wie in der
    Vorschau.
    """
    auswahl = auswahl or Auswahl()
    heute = heute or date.fromisoformat(db.heute())
    katalog = _kategorien(conn)
    gemerkte_konten = {z["konto"]: z for z in conn.execute("SELECT * FROM wiso_konto")}
    bekannt = {(z["buchungsnummer"], z["teilbuchung"]): z for z in conn.execute(
        "SELECT * FROM wiso_buchung WHERE jahr = ? AND status IN ('AKTIV', 'AUSGELASSEN')",
        (datei.jahr,))}
    vergeben: set[tuple[str, int]] = set()
    # Was im neuen Export fehlt ("in WISO gelöscht?"), steht schon vor dem
    # Abgleich fest. Ihre Eintraege im Tool sind fuer eine neue Buchung mit
    # gleichem Inhalt Kandidaten -- in WISO geloescht und neu erfasst (neue
    # Nummer) ergab sonst mit den Vorgaben eine zweite Buchung.
    in_datei = {(z.buchungsnummer, z.teilbuchung) for z in datei.zeilen}
    geloescht = [z for schluessel, z in sorted(bekannt.items(),
                                               key=lambda kv: _sortierung(kv[0]))
                 if z["status"] == "AKTIV" and z["behalten_am"] is None
                 and schluessel not in in_datei]
    frei = {z["id"]: z for z in geloescht}
    posten: list[Posten] = []
    konten: dict[str, KontoZeile] = {}
    zahler: dict[str, ZahlerZeile] = {}
    lieferanten = [dict(z) for z in conn.execute("SELECT id, name FROM lieferant")]

    for zeile in datei.zeilen:
        e = einordnen(zeile)
        p = Posten(zeile, e.gruppe, e.art, e.sachkonto, e.kontobezeichnung, e.grund)
        posten.append(p)
        if e.gruppe == AFA:
            p.anl_nr = anlagennummer(zeile.text)
        if e.gruppe == NEU and zeile.datum > heute.isoformat():
            p.gruppe, p.grund = NICHT, (f"Zahlungsdatum {zeile.datum[8:10]}.{zeile.datum[5:7]}."
                                        f"{zeile.datum[:4]} liegt in der Zukunft — nach dem "
                                        "Zahltag erneut importieren")
        alt = bekannt.get((zeile.buchungsnummer, zeile.teilbuchung))
        summe = zeile.pruefsumme()
        if p.gruppe != NEU:
            # Keine Geldbewegung (mehr). War die Buchung schon uebernommen, ist
            # sie in WISO umgebucht worden -- der Eintrag im Tool darf nicht
            # still stehen bleiben (die Zeile steht in der Datei, gilt also
            # auch nicht als "in WISO gelöscht").
            if alt is not None and alt["status"] == "AKTIV":
                p.alt = alt
                p.umgebucht = p.grund or "AfA-Zeile — keine Zahlung"
                p.im_tool_geloescht = _ziel_geloescht(conn, alt)
                if summe in (alt["pruefsumme"], alt["ignoriert_pruefsumme"]):
                    p.gruppe = BEKANNT
                else:
                    p.gruppe = UMGEBUCHT
                    # Ist der Eintrag im Tool ohnehin geloescht, aendert
                    # "löschen" kein Geld -- es loest nur die Verbindung.
                    p.vorschlag = LOESCHEN if p.im_tool_geloescht else SPAETER
            continue
        if alt is not None:
            p.alt = alt
            if alt["status"] == "AUSGELASSEN":
                if alt["pruefsumme"] == summe:
                    p.gruppe = BEKANNT
                else:
                    p.frueher_ausgelassen = True
            elif summe in (alt["pruefsumme"], alt["ignoriert_pruefsumme"]):
                p.gruppe = BEKANNT
                p.im_tool_geloescht = _ziel_geloescht(conn, alt)
            else:
                p.gruppe = GEAENDERT
                p.im_tool_geloescht = _ziel_geloescht(conn, alt)
                p.aenderungen = _aenderungen(alt, zeile)
                p.datei_am_alten = _datei_am_alten(conn, alt)
        _zuordnen(conn, p, katalog, gemerkte_konten, auswahl, konten, zahler, lieferanten,
                  zaehlen=p.gruppe in (NEU, GEAENDERT))
        if p.gruppe in (NEU, GEAENDERT):
            _hinweise(p, katalog)
        if p.gruppe == NEU:
            p.kandidat = _kandidat(conn, p, vergeben, frei)
            if p.kandidat is not None:
                vergeben.add((p.kandidat.typ, p.kandidat.id))
            if p.offen:
                p.vorschlag = SPAETER
            elif p.kandidat is not None and p.kandidat.sicher:
                p.vorschlag = VERKNUEPFEN
            else:
                p.vorschlag = UEBERNEHMEN
        elif p.gruppe == GEAENDERT:
            # "ignorieren", wo "übernehmen" nur schaden kann: der Eintrag im
            # Tool ist bewusst geloescht (etwa als Dublette) -- uebernehmen
            # legte ihn neu an --, oder es aenderte sich nur der Text (der
            # Beleg mit angehaengter Datei bliebe sonst ohne Grund ersetzt).
            # Sonst "später": nichts ohne ausdrueckliche Wahl.
            nur_text = set(p.aenderungen) <= {"Text", "Belegnummer"} and (
                p.art != EINNAHME or einnahmen.zahler_aus_text(
                    str(_inhalt(p.alt).get("text", ""))).schluessel == (
                    p.zahler.schluessel if p.zahler else None))
            p.vorschlag = IGNORIEREN if p.im_tool_geloescht or nur_text else SPAETER

    afa = _afa_zeilen(conn, datei, posten)
    plan = Plan(datei, posten, geloescht, sorted(konten.values(), key=lambda k: k.konto),
                sorted(zahler.values(), key=lambda z: db.sortierschluessel(z.name)), afa)
    plan.nachfolger = {p.kandidat.vorgaenger: p.schluessel for p in posten
                       if p.kandidat is not None and p.kandidat.vorgaenger is not None}
    plan.fingerabdruck = _fingerabdruck(plan)
    return plan


def _sortierung(schluessel: tuple[str, str]) -> tuple:
    nummer, teil = schluessel
    return (int(nummer) if nummer.isdigit() else 0, nummer, teil)


#: Die Felder einer Buchung, wie die Vorschau sie bei einer Aenderung nennt.
_FELDNAMEN = {"zahlungsdatum": "Datum", "wertstellung": "Wertstellung", "text": "Text",
              "konto": "Konto", "gegenkonto": "Gegenkonto", "soll_cent": "Betrag",
              "haben_cent": "Betrag", "belegnummer": "Belegnummer",
              "steuerschluessel": "Steuerschlüssel", "steuersatz": "Steuersatz"}


def _aenderungen(alt: sqlite3.Row, zeile: Zeile) -> list[str]:
    """Was sich in WISO an einer schon uebernommenen Buchung geaendert hat."""
    vorher, nachher = _inhalt(alt), zeile.inhalt()
    namen: list[str] = []
    for feld, name in _FELDNAMEN.items():
        if vorher.get(feld) != nachher.get(feld) and name not in namen:
            namen.append(name)
    return namen


def _datei_am_alten(conn: sqlite3.Connection, alt: sqlite3.Row) -> str | None:
    """Der Name der Datei am bisherigen, aus WISO angelegten Eintrag -- falls eine haengt."""
    if alt["verknuepft"]:
        return None
    if alt["beleg_id"] is not None:
        zeile = conn.execute("SELECT original_name FROM belegdatei WHERE beleg_id = ?"
                             " AND rolle = 'ORIGINAL' ORDER BY id LIMIT 1",
                             (alt["beleg_id"],)).fetchone()
    elif alt["einnahme_id"] is not None:
        zeile = conn.execute("SELECT original_name FROM einnahmedatei WHERE einnahme_id = ?"
                             " ORDER BY id LIMIT 1", (alt["einnahme_id"],)).fetchone()
    else:
        return None
    return zeile["original_name"] if zeile is not None else None


def _dateien_uebertragen(conn: sqlite3.Connection, alt: sqlite3.Row, p: Posten) -> None:
    """Haengt die Dateien des ersetzten Eintrags an den neuen (gleicher Pfad, gleiche
    Pruefsumme). Der alte ist schon geloescht -- die Datei ist dann Original genau
    eines gueltigen Belegs, und wiederherstellen laesst sich der alte nicht mehr
    (Trigger beleg_wiederherstellen_original_einmalig)."""
    neu = conn.execute("SELECT beleg_id, einnahme_id FROM wiso_buchung WHERE jahr = ? AND"
                       " buchungsnummer = ? AND teilbuchung = ? AND status = 'AKTIV'",
                       (alt["jahr"], p.zeile.buchungsnummer, p.zeile.teilbuchung)).fetchone()
    if neu is None:
        return
    if alt["beleg_id"] is not None and neu["beleg_id"] is not None:
        dateien = conn.execute("SELECT * FROM belegdatei WHERE beleg_id = ? ORDER BY id",
                               (alt["beleg_id"],)).fetchall()
        for d in dateien:
            conn.execute(
                "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse, mime,"
                " rolle, importiert_am) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (neu["beleg_id"], d["pfad"], d["original_name"], d["sha256"], d["groesse"],
                 d["mime"], d["rolle"], db.jetzt_utc()))
        if dateien:
            db.protokolliere(conn, "BELEG_DATEI_UEBERTRAGEN", "beleg", neu["beleg_id"],
                             wert_alt={"beleg_id": alt["beleg_id"]},
                             wert_neu={"dateien": [d["original_name"] for d in dateien]},
                             hash_=dateien[0]["sha256"],
                             begruendung="in WISO geändert und neu übernommen; die Datei "
                                         "gehört zur selben Ausgabe")
    elif alt["einnahme_id"] is not None and neu["einnahme_id"] is not None:
        dateien = conn.execute("SELECT * FROM einnahmedatei WHERE einnahme_id = ? ORDER BY id",
                               (alt["einnahme_id"],)).fetchall()
        for d in dateien:
            conn.execute(
                "INSERT INTO einnahmedatei (einnahme_id, pfad, original_name, sha256, groesse,"
                " mime, importiert_am) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (neu["einnahme_id"], d["pfad"], d["original_name"], d["sha256"], d["groesse"],
                 d["mime"], db.jetzt_utc()))
        if dateien:
            db.protokolliere(conn, "EINNAHME_DATEI", "einnahme", neu["einnahme_id"],
                             wert_neu={"dateien": [d["original_name"] for d in dateien],
                                       "von_einnahme": alt["einnahme_id"]},
                             hash_=dateien[0]["sha256"],
                             begruendung="in WISO geändert und neu übernommen")


def _ziel_geloescht(conn: sqlite3.Connection, alt: sqlite3.Row) -> bool:
    if alt["beleg_id"] is not None:
        zeile = conn.execute("SELECT geloescht_am FROM beleg WHERE id = ?",
                             (alt["beleg_id"],)).fetchone()
        return zeile is not None and zeile["geloescht_am"] is not None
    if alt["einnahme_id"] is not None:
        zeile = conn.execute("SELECT geloescht_am FROM einnahme WHERE id = ?",
                             (alt["einnahme_id"],)).fetchone()
        return zeile is not None and zeile["geloescht_am"] is not None
    return False


def _zuordnen(conn: sqlite3.Connection, p: Posten, katalog: dict[str, sqlite3.Row],
              gemerkte_konten: dict[str, sqlite3.Row], auswahl: Auswahl,
              konten: dict[str, KontoZeile], zahler: dict[str, ZahlerZeile],
              lieferanten: list[dict], *, zaehlen: bool) -> None:
    """Kategorie, Zahler und Lieferant einer Geldbewegung.

    ``zaehlen``: die Zeile geht in die Tabellen "Konten" und "Zahler" der
    Vorschau ein -- nur, was angelegt werden kann (neu oder geaendert).
    """
    zeile = p.zeile
    if p.art in (AUSGABE, ANLAGE, ERSTATTUNG):
        p.lieferant = _lieferant_name(lieferant_aus_text(anlagenname(zeile.text)
                                                         if p.art == ANLAGE else zeile.text),
                                      lieferanten)
    if p.art == ANLAGE:
        p.anl_nr = anlagennummer(zeile.text)
        kategorie = katalog.get("HARDWARE_ANLAGE")
        p.kategorie_id = kategorie["id"] if kategorie is not None else None
        p.kategorie = kategorie["bezeichnung"] if kategorie is not None else None
        p.kategorie_herkunft = "Anlagegut"
        return
    if p.art == AUSGABE:
        konto = (konten.setdefault(p.sachkonto, KontoZeile(p.sachkonto, p.kontobezeichnung))
                 if zaehlen else KontoZeile(p.sachkonto, p.kontobezeichnung))
        konto.anzahl += 1
        konto.cent += zeile.betrag_cent
        if p.sachkonto in auswahl.konten:
            kategorie_id, herkunft = auswahl.konten[p.sachkonto], "gewählt"
        elif p.sachkonto in gemerkte_konten:
            kategorie_id, herkunft = gemerkte_konten[p.sachkonto]["kategorie_id"], "gemerkt"
        elif p.sachkonto in KONTO_VORGABEN and KONTO_VORGABEN[p.sachkonto] in katalog:
            kategorie_id, herkunft = katalog[KONTO_VORGABEN[p.sachkonto]]["id"], "Vorgabe"
        else:
            kategorie_id, herkunft = None, None
        konto.unbekannt = (p.sachkonto not in gemerkte_konten
                           and p.sachkonto not in KONTO_VORGABEN)
        if konto.kategorie_id is None:
            konto.kategorie_id, konto.herkunft = kategorie_id, herkunft
        zeilenwahl = auswahl.kategorien.get(zeile.schluessel)
        gwg = katalog.get("HARDWARE_GWG")
        software = katalog.get("SOFTWARE_KAUF")
        if zeilenwahl:
            kategorie_id, herkunft = zeilenwahl, "Zeile"
        elif (gwg is not None and software is not None and kategorie_id == gwg["id"]
              and SOFTWARE.search(zeile.text) and zeile.schluessel not in auswahl.kategorien):
            # Ein Software-Titel unter "Sofortabschreibung GWG" ist eine
            # Dauerlizenz: dieselbe Kennzahl, aber die richtige Kategorie.
            kategorie_id, herkunft = software["id"], "Software-Titel"
        zeile_kat = _kategorie_zeile(conn, kategorie_id)
        if zeile_kat is None or zeile_kat["bereich"] != kategorien.BEREICH_AUSGABEN:
            p.kategorie_id = None
        else:
            p.kategorie_id, p.kategorie = zeile_kat["id"], zeile_kat["bezeichnung"]
            p.kategorie_herkunft = herkunft
        return
    if p.art == ERSTATTUNG:
        kategorie = katalog.get(einnahmen.EINSTUFUNGEN[einnahmen.ERSTATTUNG])
        p.einstufung = einnahmen.ERSTATTUNG
        p.kategorie_id = kategorie["id"] if kategorie is not None else None
        p.kategorie = kategorie["bezeichnung"] if kategorie is not None else None
        p.kategorie_herkunft = f"Haben auf Konto {p.sachkonto}"
        p.zahler = einnahmen.Zahler(p.lieferant or zeile.text,
                                    einnahmen.zahler_schluessel(p.lieferant or zeile.text),
                                    einnahmen.ERSTATTUNG, "Erstattung auf einem Aufwandskonto")
        return
    # EINNAHME: je Zahler eingestuft.
    gefunden = einnahmen.zahler_aus_text(zeile.text)
    p.zahler = gefunden
    z = (zahler.setdefault(gefunden.schluessel, ZahlerZeile(gefunden.schluessel, gefunden.name))
         if zaehlen else ZahlerZeile(gefunden.schluessel, gefunden.name))
    z.anzahl += 1
    z.cent += zeile.betrag_cent
    gemerkt = einnahmen.gemerkt(conn, gefunden.schluessel)
    if gefunden.schluessel in auswahl.zahler:
        einstufung, herkunft = auswahl.zahler[gefunden.schluessel], "gewählt"
    elif gemerkt is not None:
        einstufung = (einnahmen.einstufung_der_kategorie(conn, gemerkt["kategorie_id"])
                      or einnahmen.INLAND)
        herkunft = "gemerkt"
    else:
        einstufung, herkunft = gefunden.vorschlag, gefunden.grund
    if z.anzahl == 1:
        z.einstufung, z.herkunft = einstufung, herkunft
    p.einstufung = einstufung
    if einstufung == SPAETER or einstufung not in einnahmen.EINSTUFUNGEN:
        p.einstufung = SPAETER
        return
    if gemerkt is not None and herkunft == "gemerkt":
        kategorie = _kategorie_zeile(conn, gemerkt["kategorie_id"])
    else:
        kategorie = katalog.get(einnahmen.EINSTUFUNGEN[einstufung])
    p.kategorie_id = kategorie["id"] if kategorie is not None else None
    p.kategorie = kategorie["bezeichnung"] if kategorie is not None else None
    p.kategorie_herkunft = "Zahler"


def _lieferant_name(name: str, lieferanten: list[dict]) -> str:
    """Ein vorhandener Lieferant, wenn sein erstes Wort passt -- sonst der Name."""
    eigene = sorted(_worte(name))
    erstes = next(iter(w for w in re.split(r"[^a-z0-9]+", db.sortierschluessel(name))
                       if w in eigene), None)
    for lieferant in lieferanten:
        if lieferant["name"].casefold() == name.casefold():
            return lieferant["name"]
    if erstes is None:
        return name
    for lieferant in lieferanten:
        worte = [w for w in re.split(r"[^a-z0-9]+", db.sortierschluessel(lieferant["name"]))
                 if w in _worte(lieferant["name"])]
        if worte and worte[0] == erstes:
            return lieferant["name"]
    return name


def _hinweise(p: Posten, katalog: dict[str, sqlite3.Row]) -> None:
    """Hinweise statt Blockade: beim Import zaehlt der WISO-Betrag."""
    schluessel = next((s for s, k in katalog.items() if k["id"] == p.kategorie_id), None)
    betrag = p.betrag_cent
    if schluessel in ("HARDWARE_GWG", "SOFTWARE_KAUF"):
        grenze = int(katalog[schluessel]["grenze_netto_cent"] or 80_000)
        if kategorien.netto_fuer_grenzwert_cent(betrag, None, 190) > grenze:
            p.hinweise.append(f"{euro(betrag)} € liegen auch ohne 19 % Umsatzsteuer über "
                              f"{euro(grenze)} € netto — eigentlich ein Anlagegut. Übernommen "
                              "wie in WISO (sofort abgeschrieben); bitte prüfen.")
        elif betrag > grenze:
            p.hinweise.append(f"über {euro(grenze)} € brutto — die GWG-Grenze gilt netto "
                              f"(bei 19 % USt bis {euro(grenze * 119 // 100)} €); bitte an der "
                              "Rechnung prüfen.")
    if schluessel == "REISE_VERPFLEGUNG":
        p.hinweise.append("Verpflegung erfasst das Tool sonst als Pauschale je Reise "
                          "(Eigenbeleg); übernommen mit dem WISO-Betrag.")
    if schluessel in ("REISE_FAHRTKOSTEN", "REISE_UEBERNACHTUNG"):
        p.hinweise.append("Reisekosten ohne Reise im Tool — der Anlass steht nur im "
                          "Buchungstext.")
    if p.art == EINNAHME and p.einstufung == einnahmen.ANLAGENVERKAUF:
        p.hinweise.append("Verkauf eines Anlageguts: danach auf der Seite Anlagen beim Gut den "
                          "Abgang erfassen und diese Einnahme als Erlös wählen — sonst läuft "
                          "die AfA weiter, und der Restbuchwert fehlt.")
    if p.art == ANLAGE and p.anl_nr is None:
        p.hinweise.append("ohne „[Anl.-Nr.: …]“ im Text — die Nutzungsdauer lässt sich "
                          "nicht aus der AfA-Zeile ableiten.")


def _abstand(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _kandidat(conn: sqlite3.Connection, p: Posten, vergeben: set[tuple[str, int]],
              frei: dict[int, sqlite3.Row] | None = None) -> Kandidat | None:
    """Der beste vorhandene Eintrag, mit dem sich die Buchung verknuepfen laesst.

    ``frei``: WISO-Buchungen, die im neuen Export fehlen. Ein Eintrag, der an
    einer von ihnen haengt, ist ein Kandidat (mit ``vorgaenger``); einer an
    einer anderen geltenden WISO-Buchung nicht.
    """
    frei = frei or {}
    datum, betrag = p.zeile.datum, p.betrag_cent
    name = p.lieferant if p.art in (AUSGABE, ANLAGE) else (
        p.zahler.name if p.zahler else p.zeile.text)
    treffer: list[tuple[tuple, Kandidat]] = []

    def aufnehmen(typ: str, kennung: int, partner: str | None, tage: list[str], text: str,
                  wiso_id: int | None = None, *, zahlung_fehlt: bool = False,
                  nur_anbieten: bool = False):
        if (typ, kennung) in vergeben or (wiso_id is not None and wiso_id not in frei):
            return
        abstand = min(_abstand(datum, t) for t in tage if t)
        aehnlich = namen_aehnlich(name, partner) or namen_aehnlich(p.zeile.text, partner)
        weit = abstand > TAGE_ABSTAND
        if weit and not aehnlich:
            return
        if weit:
            text += f" — {abstand} Tage entfernt (Zahlungsziel?), nur angeboten"
            nur_anbieten = True
        if wiso_id is not None:
            # Derselbe Text wie die geloeschte Buchung: dieselbe Buchung.
            alt_text = str(_inhalt(frei[wiso_id]).get("text", ""))
            aehnlich = aehnlich or alt_text == p.zeile.text or namen_aehnlich(p.zeile.text,
                                                                              alt_text)
        sicher = (aehnlich or abstand <= TAGE_SICHER) and not nur_anbieten
        treffer.append(((weit, not aehnlich, abstand, kennung),
                        Kandidat(typ, kennung, text, sicher, wiso_id, zahlung_fehlt)))

    if p.art in (AUSGABE, ANLAGE):
        for b in conn.execute(
            """
            SELECT b.id, b.belegdatum, b.betrag_brutto_cent, l.name AS lieferant,
                   (SELECT MIN(z.datum) FROM zahlung z WHERE z.beleg_id = b.id) AS bezahlt_am,
                   (SELECT w.id FROM wiso_buchung w WHERE w.beleg_id = b.id
                       AND w.status = 'AKTIV') AS wiso_id,
                   (SELECT COALESCE(SUM(ABS(z.betrag_cent)), 0) FROM zahlung z
                     WHERE z.beleg_id = b.id) AS bezahlt_cent, b.waehrung
              FROM v_beleg_gueltig b LEFT JOIN lieferant l ON l.id = b.lieferant_id
             WHERE b.betrag_brutto_cent = ?
               AND (ABS(julianday(b.belegdatum) - julianday(?)) <= ?
                    OR EXISTS (SELECT 1 FROM zahlung z WHERE z.beleg_id = b.id
                                 AND ABS(julianday(z.datum) - julianday(?)) <= ?))
            """, (betrag, datum, TAGE_ZIEL, datum, TAGE_ZIEL)):
            text = (f"Beleg {b['id']}: {b['lieferant'] or 'ohne Lieferant'}, "
                    f"{_de(b['belegdatum'])}, {euro(b['betrag_brutto_cent'])} €")
            bezahlt = int(b["bezahlt_cent"])
            fremd = (b["waehrung"] or "EUR") != "EUR"
            if bezahlt == 0 and not fremd:
                text += (f" — im Tool unbezahlt: die Zahlung vom {_de(datum)} laut WISO wird "
                         "mit angelegt")
            elif bezahlt == 0:
                # Ein Fremdwaehrungsbeleg wird zum Kurs des Zahltags umgerechnet --
                # das kann der Import nicht still nebenbei.
                text += " — im Tool unbezahlt (Fremdwährung): die Zahlung bitte am Beleg erfassen"
            elif bezahlt < int(b["betrag_brutto_cent"]):
                text += " — im Tool erst teilweise bezahlt"
            aufnehmen("beleg", b["id"], b["lieferant"], [b["belegdatum"], b["bezahlt_am"]],
                      text, b["wiso_id"], zahlung_fehlt=bezahlt == 0 and not fremd,
                      nur_anbieten=(bezahlt == 0 and fremd)
                      or 0 < bezahlt < int(b["betrag_brutto_cent"]))
    else:
        for z in conn.execute(
            """
            SELECT z.id, z.datum, z.betrag_cent, r.nummer,
                   COALESCE(json_extract(r.kunde_snapshot_json, '$.name'), k.name) AS kunde,
                   (SELECT w.id FROM wiso_buchung w WHERE w.zahlung_id = z.id
                       AND w.status = 'AKTIV') AS wiso_id
              FROM zahlung z JOIN rechnung r ON r.id = z.rechnung_id
              JOIN kunde k ON k.id = r.kunde_id
             WHERE z.storno_von_id IS NULL
               AND NOT EXISTS (SELECT 1 FROM zahlung g WHERE g.storno_von_id = z.id)
               AND z.betrag_cent = ?
               AND ABS(julianday(z.datum) - julianday(?)) <= ?
            """, (betrag, datum, TAGE_ABSTAND)):
            aufnehmen("zahlung", z["id"], z["kunde"], [z["datum"]],
                      f"Zahlung auf Rechnung {z['nummer']} ({z['kunde']}) vom "
                      f"{_de(z['datum'])}, {euro(z['betrag_cent'])} €", z["wiso_id"])
        for e in conn.execute(
            """
            SELECT e.id, e.zufluss_am, e.betrag_cent, e.zahler,
                   (SELECT w.id FROM wiso_buchung w WHERE w.einnahme_id = e.id
                       AND w.status = 'AKTIV') AS wiso_id
              FROM v_einnahme_gueltig e
             WHERE e.betrag_cent = ?
               AND ABS(julianday(e.zufluss_am) - julianday(?)) <= ?
            """, (betrag, datum, TAGE_ABSTAND)):
            aufnehmen("einnahme", e["id"], e["zahler"], [e["zufluss_am"]],
                      f"Einnahme {e['id']}: {e['zahler']}, {_de(e['zufluss_am'])}, "
                      f"{euro(e['betrag_cent'])} €", e["wiso_id"])
    if not treffer:
        return None
    kandidat = min(treffer, key=lambda t: t[0])[1]
    if kandidat.vorgaenger is not None and kandidat.typ == "beleg" and p.kategorie_id:
        im_tool = conn.execute("SELECT k.id, k.bezeichnung FROM v_beleg_gueltig b"
                               " JOIN euer_kategorie k"
                               " ON k.id = b.kategorie_id WHERE b.id = ?",
                               (kandidat.id,)).fetchone()
        if im_tool is not None and im_tool["id"] != p.kategorie_id:
            p.hinweise.append(f"Kategorie im Tool „{im_tool['bezeichnung']}“, laut WISO-Konto "
                              f"jetzt „{p.kategorie}“ — beim Verknüpfen bleibt die im Tool; "
                              "bitte dort ändern, falls nötig.")
    return kandidat


def _inhalt(zeile: sqlite3.Row) -> dict:
    try:
        wert = json.loads(zeile["inhalt_json"] or "{}")
    except ValueError:
        return {}
    return wert if isinstance(wert, dict) else {}


def _de(iso: str | None) -> str:
    if not iso:
        return "—"
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


def _gut_zur_nummer(conn: sqlite3.Connection, anl_nr: str) -> sqlite3.Row | None:
    """Das gueltige Anlagegut zu einer WISO-Anlagennummer, falls bekannt."""
    for zeile in conn.execute(
        "SELECT w.anlagegut_id, w.inhalt_json FROM wiso_buchung w"
        " JOIN v_anlagegut_gueltig g ON g.id = w.anlagegut_id"
        " WHERE w.art = 'ANLAGE' AND w.status = 'AKTIV' AND w.anlagegut_id IS NOT NULL"
        " ORDER BY w.id DESC"):
        try:
            text = json.loads(zeile["inhalt_json"]).get("text", "")
        except (ValueError, AttributeError):
            continue
        if anlagennummer(text) == anl_nr:
            return conn.execute("SELECT * FROM v_anlagegut_gueltig WHERE id = ?",
                                (zeile["anlagegut_id"],)).fetchone()
    angabe = conn.execute(
        "SELECT a.anlagegut_id FROM fehlende_angabe a"
        " JOIN v_anlagegut_gueltig g ON g.id = a.anlagegut_id"
        " WHERE a.art = 'ANLAGEGUT' AND a.schluessel = ?", (f"anl:{anl_nr}",)).fetchone()
    if angabe is not None:
        return conn.execute("SELECT * FROM v_anlagegut_gueltig WHERE id = ?",
                            (angabe["anlagegut_id"],)).fetchone()
    return None


def _afa_zeilen(conn: sqlite3.Connection, datei: Datei, posten: list[Posten]) -> list[AfaZeile]:
    """Die AfA-Zeilen zum Abgleich -- und die Nutzungsdauer neuer Anschaffungen."""
    anschaffungen = {p.anl_nr: p for p in posten if p.art == ANLAGE and p.anl_nr}
    ergebnis: list[AfaZeile] = []
    for p in posten:
        if p.gruppe != AFA:
            continue
        zeile = p.zeile
        name = anlagenname(zeile.text)
        anl_nr = p.anl_nr
        if anl_nr and anl_nr in anschaffungen:
            kauf = anschaffungen[anl_nr]
            kauf.afa_wiso_cent = zeile.betrag_cent
            kauf.nutzungsdauer = anlagen.nutzungsdauer_ableiten(
                kauf.betrag_cent, kauf.zeile.datum, zeile.betrag_cent, jahr=datei.jahr)
            if kauf.nutzungsdauer is None and kauf.gruppe in (NEU, GEAENDERT):
                kauf.hinweise.append(
                    f"Keine Nutzungsdauer ergibt die WISO-AfA von {euro(zeile.betrag_cent)} € — "
                    "das Anlagegut bitte auf der Seite Anlagen von Hand anlegen.")
            gut = (_gut_zur_nummer(conn, anl_nr) if kauf.gruppe == BEKANNT else None)
            ergebnis.append(AfaZeile(zeile, anl_nr, name, zeile.betrag_cent, "NEU_ANLAGE",
                                     gut["id"] if gut is not None else None,
                                     _afa_im_jahr(conn, gut["id"], datei.jahr)
                                     if gut is not None else None))
            continue
        gut = _gut_zur_nummer(conn, anl_nr) if anl_nr else None
        if gut is not None:
            ergebnis.append(AfaZeile(zeile, anl_nr, name, zeile.betrag_cent, "VERZEICHNIS",
                                     gut["id"], _afa_im_jahr(conn, gut["id"], datei.jahr)))
            continue
        vermerkt = anl_nr is not None and conn.execute(
            "SELECT 1 FROM fehlende_angabe WHERE art = 'ANLAGEGUT' AND schluessel = ?",
            (f"anl:{anl_nr}",)).fetchone() is not None
        ergebnis.append(AfaZeile(zeile, anl_nr, name, zeile.betrag_cent, "FEHLT",
                                 vermerkt=vermerkt))
    return ergebnis


def _afa_im_jahr(conn: sqlite3.Connection, gut_id: int, jahr: int) -> int:
    zeile = conn.execute(
        "SELECT j.afa_cent, g.aufteilungsquote FROM anlagegut_afa_jahr j"
        " JOIN v_anlagegut_gueltig g ON g.id = j.anlagegut_id"
        " WHERE j.anlagegut_id = ? AND j.jahr = ?", (gut_id, jahr)).fetchone()
    if zeile is None:
        return 0
    return (int(zeile["afa_cent"]) * int(zeile["aufteilungsquote"]) + 50) // 100


def _fingerabdruck(plan: Plan) -> str:
    teile = [
        [p.schluessel, p.gruppe, p.art, p.zeile.pruefsumme(),
         p.alt["id"] if p.alt is not None else None,
         [p.kandidat.typ, p.kandidat.id, p.kandidat.vorgaenger] if p.kandidat else None]
        for p in plan.posten
    ]
    teile.append(["geloescht", [z["id"] for z in plan.geloescht]])
    teile.append(["datei", plan.datei.sha256, plan.datei.jahr])
    return hashlib.sha256(json.dumps(teile, sort_keys=True).encode("utf-8")).hexdigest()


# ================================================================== Uebernehmen

@dataclass(slots=True)
class Ergebnis:
    """Was ein Klick auf "übernehmen" getan hat."""

    import_id: int | None = None
    belege: int = 0
    einnahmen: int = 0
    anlagegueter: int = 0
    verknuepft: int = 0
    umgehaengt: int = 0
    ausgelassen: int = 0
    ersetzt: int = 0
    geloescht: int = 0
    behalten: int = 0
    umgebucht: int = 0
    ignoriert: int = 0
    spaeter: int = 0
    angaben_fehlen: int = 0
    hinweise: list[str] = field(default_factory=list)

    @property
    def neu(self) -> int:
        return self.belege + self.einnahmen

    def zusammenfassung(self) -> str:
        """Ein Satz fuer die Meldung.

        >>> Ergebnis().zusammenfassung()
        'Nichts Neues — alle Buchungen sind schon übernommen.'
        >>> Ergebnis(belege=2, einnahmen=1, verknuepft=1).zusammenfassung()[:58]
        '3 Buchungen übernommen (2 Belege ohne Datei, 1 Einnahme), '
        >>> Ergebnis(spaeter=1).zusammenfassung()
        'Keine neue Buchung angelegt, 1 zurückgestellt.'
        """
        teile = []
        if self.neu:
            arten = []
            if self.belege:
                arten.append(f"{self.belege} Beleg{'' if self.belege == 1 else 'e'} ohne Datei")
            if self.einnahmen:
                arten.append(f"{self.einnahmen} Einnahme{'' if self.einnahmen == 1 else 'n'}")
            teile.append(f"{self.neu} Buchung{'' if self.neu == 1 else 'en'} übernommen "
                         f"({', '.join(arten)})")
        for anzahl, text in (
            (self.anlagegueter, "ins Anlagenverzeichnis"),
            (self.verknuepft, "mit vorhandenem Eintrag verknüpft"),
            (self.umgehaengt, "in WISO neu nummeriert — Eintrag im Tool umgehängt"),
            (self.ersetzt, "in WISO geändert und neu übernommen"),
            (self.geloescht, "in WISO gelöscht und hier gelöscht bzw. gelöst"),
            (self.behalten, "trotz Löschung in WISO behalten"),
            (self.umgebucht, "in WISO umgebucht und hier gelöscht bzw. gelöst"),
            (self.ignoriert, "Änderung ignoriert"),
            (self.ausgelassen, "bewusst nicht übernommen"),
            (self.spaeter, "zurückgestellt"),
            (self.angaben_fehlen, "unter „Angaben fehlen“ vermerkt"),
        ):
            if anzahl:
                teile.append(f"{anzahl} {text}")
        if not teile:
            return "Nichts Neues — alle Buchungen sind schon übernommen."
        if not self.neu:
            teile.insert(0, "Keine neue Buchung angelegt")
        return ", ".join(teile) + "."


def archivpfad(datenordner: Path, datei: Datei) -> Path:
    """Wo die Datei abgelegt wird: ``importe/wiso/<Jahr>/<Hash>_<Name>``.

    Mit Hash im Namen: dieselbe Datei zweimal eingespielt liegt einmal da.
    """
    sicher = re.sub(r"[^A-Za-z0-9._-]+", "_", datei.name)[-60:].strip("._") or "buchungen.csv"
    if not sicher.lower().endswith(".csv"):
        sicher += ".csv"
    return datenordner / ARCHIV / str(datei.jahr) / f"{datei.sha256[:12]}_{sicher}"


def _ablegen(datenordner: Path, datei: Datei, inhalt: bytes) -> tuple[Path, bool]:
    ziel = archivpfad(datenordner, datei)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    vorhanden = sorted(ziel.parent.glob(f"{datei.sha256[:12]}_*"))
    for kandidat in vorhanden:
        if kandidat.is_file() and pipeline.pruefsumme(kandidat) == datei.sha256:
            return kandidat, False
    zwischen = ziel.with_name(f".{ziel.name}.teil")
    zwischen.write_bytes(inhalt)
    os.replace(zwischen, ziel)
    os.chmod(ziel, pipeline.NUR_LESEN)
    if pipeline.pruefsumme(ziel) != datei.sha256:
        raise ImportFehler("Die Datei hat sich beim Ablegen verändert — bitte noch einmal.")
    return ziel, True


def _lieferant_id(conn: sqlite3.Connection, name: str, cache: dict[str, int]) -> int:
    schluessel = name.casefold()
    if schluessel in cache:
        return cache[schluessel]
    vorhanden = conn.execute("SELECT id FROM lieferant WHERE name = ? COLLATE NOCASE",
                             (name,)).fetchone()
    if vorhanden is not None:
        cache[schluessel] = vorhanden["id"]
        return vorhanden["id"]
    neue_id = conn.execute("INSERT INTO lieferant (name, angelegt_am, notiz) VALUES (?, ?, ?)",
                           (name, db.heute(), "aus dem Buchungstext eines WISO-Imports")
                           ).lastrowid
    db.protokolliere(conn, "ANLEGEN", "lieferant", neue_id,
                     wert_neu={"name": name, "quelle": "WISO-Import"})
    cache[schluessel] = int(neue_id)
    return int(neue_id)


def _verfahren(datei_sha: str, zeile: Zeile, jahr: int) -> str:
    return (f"Übernahme aus WISO Steuer (Export „Buchungen“ als CSV), Datei "
            f"{datei_sha[:12]}, Buchung {zeile.buchungsnummer}/{zeile.teilbuchung} ({jahr})")


def _notiz(p: Posten, jahr: int) -> str:
    return (f"Aus WISO übernommen (Buchung {p.zeile.buchungsnummer}/{jahr}, Konto "
            f"{p.sachkonto} {p.kontobezeichnung}): {p.zeile.text}")[:1000]


def _beleg_anlegen(conn: sqlite3.Connection, p: Posten, datei: Datei,
                   lieferanten: dict[str, int], heute: date) -> tuple[int, int | None]:
    """Beleg ohne Datei mit Zahlung -- und bei einer Anschaffung das Anlagegut."""
    zeile = p.zeile
    betrag = zeile.betrag_cent
    lieferant_id = _lieferant_id(conn, p.lieferant or lieferant_aus_text(zeile.text), lieferanten)
    ist_anlage = p.art == ANLAGE
    jetzt = db.jetzt_utc()
    beleg_id = conn.execute(
        """
        INSERT INTO beleg (art, lieferant_id, kategorie_id, belegdatum, belegnummer_extern,
                           waehrung, betrag_brutto_cent, netto_fuer_grenzwert_cent,
                           aufteilungsquote, betrag_betrieblich_cent, ist_anlagegut,
                           zahlungsart, notiz, geprueft_am, erfasst_am)
        VALUES ('EINGANG', ?, ?, ?, ?, 'EUR', ?, ?, 100, ?, ?, 'SONSTIGES', ?, ?, ?)
        """,
        (lieferant_id, p.kategorie_id, zeile.datum,
         zeile.belegnummer or None, betrag, betrag, betrag, int(ist_anlage),
         _notiz(p, datei.jahr), jetzt, jetzt),
    ).lastrowid
    conn.execute(
        "INSERT INTO extraktion (beleg_id, quelle, rohantwort_json, bestaetigt_json, verfahren,"
        " regel_version, datei_sha256, zeitpunkt) VALUES (?, 'manuell', NULL, ?, ?, NULL, ?, ?)",
        (beleg_id, json.dumps({**zeile.inhalt(), "kontobezeichnung": zeile.kontobezeichnung},
                              ensure_ascii=False, sort_keys=True),
         _verfahren(datei.sha256, zeile, datei.jahr), datei.sha256, jetzt))
    db.protokolliere(conn, "BELEG_ERFASSEN", "beleg", beleg_id,
                     wert_neu={"brutto_cent": betrag, "datum": zeile.datum, "quelle": "wiso",
                               "buchung": f"{zeile.buchungsnummer}/{zeile.teilbuchung}",
                               "ohne_datei": True},
                     hash_=datei.sha256)
    conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art, notiz)"
                 " VALUES (?, ?, ?, 'SONSTIGES', ?)",
                 (beleg_id, zeile.datum, -betrag, "aus WISO übernommen"))
    db.protokolliere(conn, "ZAHLUNG", "beleg", beleg_id, feld="datum", wert_neu=zeile.datum,
                     begruendung="Zahlungsdatum laut WISO-Export")
    gut_id: int | None = None
    if ist_anlage and p.nutzungsdauer is not None:
        dauer, methode = p.nutzungsdauer
        gut_id = anlagen.anlegen(
            conn, bezeichnung=anlagenname(zeile.text), anschaffung_am=zeile.datum,
            ak_brutto_cent=betrag, nutzungsdauer_jahre=dauer,
            ist_it=methode == anlagen.EIN_JAHR_IT, beleg_id=beleg_id,
            notiz=(f"Nutzungsdauer aus WISO abgeleitet ({dauer} "
                   f"{'Jahr' if dauer == 1 else 'Jahre'}: AfA {datei.jahr} laut WISO "
                   f"{euro(p.afa_wiso_cent or 0)} €), Anl.-Nr. {p.anl_nr} — änderbar"),
            transaktion=False, heute=heute)
    return int(beleg_id), gut_id


def _einnahme_anlegen(conn: sqlite3.Connection, p: Posten, datei: Datei) -> int:
    zeile = p.zeile
    zahler_name = p.zahler.name if p.zahler else zeile.text
    return einnahmen.einfuegen(
        conn, zufluss_am=zeile.datum, betrag_cent=zeile.betrag_cent,
        zahler=" ".join(ohne_steuerzeichen(zahler_name).split())[:120] or "unbekannt",
        kategorie_id=p.kategorie_id, notiz=_notiz(p, datei.jahr), herkunft=einnahmen.WISO,
        journal={"buchung": f"{zeile.buchungsnummer}/{zeile.teilbuchung}",
                 "jahr": datei.jahr, "datei_sha256": datei.sha256})


def _registrieren(conn: sqlite3.Connection, p: Posten, datei: Datei, import_id: int, *,
                  status: str = "AKTIV", beleg_id: int | None = None,
                  einnahme_id: int | None = None, zahlung_id: int | None = None,
                  anlagegut_id: int | None = None, verknuepft: bool = False) -> int:
    return int(conn.execute(
        "INSERT INTO wiso_buchung (jahr, buchungsnummer, teilbuchung, pruefsumme, inhalt_json,"
        " art, beleg_id, einnahme_id, zahlung_id, anlagegut_id, verknuepft, import_id, status,"
        " erfasst_am) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datei.jahr, p.zeile.buchungsnummer, p.zeile.teilbuchung, p.zeile.pruefsumme(),
         json.dumps(p.zeile.inhalt(), ensure_ascii=False, sort_keys=True), p.art, beleg_id,
         einnahme_id, zahlung_id, anlagegut_id, int(verknuepft), import_id, status,
         db.jetzt_utc())).lastrowid)


def _status(conn: sqlite3.Connection, wiso_id: int, status: str) -> None:
    conn.execute("UPDATE wiso_buchung SET status = ?, geaendert_am = ? WHERE id = ?",
                 (status, db.jetzt_utc(), wiso_id))


def _ziel_markieren(conn: sqlite3.Connection, alt: sqlite3.Row, grund: str) -> None:
    """Der aus WISO angelegte Eintrag bekommt die Loeschmarkierung (nie ein verknuepfter)."""
    if alt["verknuepft"]:
        return
    if alt["beleg_id"] is not None:
        loeschen.markieren(conn, alt["beleg_id"], grund=grund, anlagegut_mit=True)
    elif alt["einnahme_id"] is not None:
        einnahmen.markieren(conn, alt["einnahme_id"], grund)


def _kandidat_pruefen(conn: sqlite3.Connection, kandidat: Kandidat) -> None:
    """Unter der Sperre: gibt es den Eintrag noch, und ist er noch frei?"""
    if kandidat.typ == "beleg":
        frei = conn.execute(
            "SELECT 1 FROM v_beleg_gueltig b WHERE b.id = ? AND NOT EXISTS (SELECT 1 FROM"
            " wiso_buchung w WHERE w.beleg_id = b.id AND w.status = 'AKTIV')",
            (kandidat.id,)).fetchone()
    elif kandidat.typ == "einnahme":
        frei = conn.execute(
            "SELECT 1 FROM v_einnahme_gueltig e WHERE e.id = ? AND NOT EXISTS (SELECT 1 FROM"
            " wiso_buchung w WHERE w.einnahme_id = e.id AND w.status = 'AKTIV')",
            (kandidat.id,)).fetchone()
    else:
        frei = conn.execute(
            "SELECT 1 FROM zahlung z WHERE z.id = ? AND NOT EXISTS (SELECT 1 FROM"
            " wiso_buchung w WHERE w.zahlung_id = z.id AND w.status = 'AKTIV')",
            (kandidat.id,)).fetchone()
    if frei is None:
        raise ImportFehler(f"{kandidat.text} ist inzwischen gelöscht oder schon verknüpft — "
                           "bitte die Vorschau neu laden.")


def _zahlung_nachtragen(conn: sqlite3.Connection, p: Posten, datei: Datei) -> None:
    """Die Zahlung aus WISO an einem unbezahlten Beleg, mit dem verknuepft wurde.

    Unter der Sperre noch einmal geprueft: ist er inzwischen bezahlt (zweites
    Fenster), bleibt es bei der vorhandenen Zahlung.
    """
    beleg_id = p.kandidat.id
    if conn.execute("SELECT 1 FROM zahlung WHERE beleg_id = ?", (beleg_id,)).fetchone():
        return
    conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art, notiz)"
                 " VALUES (?, ?, ?, 'SONSTIGES', ?)",
                 (beleg_id, p.zeile.datum, -p.betrag_cent,
                  "aus WISO übernommen (beim Verknüpfen)"))
    db.protokolliere(conn, "ZAHLUNG", "beleg", beleg_id, feld="datum", wert_neu=p.zeile.datum,
                     begruendung=f"Zahlungsdatum laut WISO-Export (Buchung "
                                 f"{p.zeile.buchungsnummer}/{datei.jahr}); der Beleg war im "
                                 "Tool noch unbezahlt")


def _umhaengen(conn: sqlite3.Connection, kandidat: Kandidat) -> sqlite3.Row | None:
    """Loest den Eintrag von seiner alten, in WISO geloeschten Buchung (Stand ERSETZT)."""
    if kandidat.vorgaenger is None:
        return None
    alt = conn.execute("SELECT * FROM wiso_buchung WHERE id = ? AND status = 'AKTIV'",
                       (kandidat.vorgaenger,)).fetchone()
    if alt is None:
        raise ImportFehler(f"{kandidat.text} wurde inzwischen anders zugeordnet — bitte die "
                           "Vorschau neu laden.")
    _status(conn, alt["id"], "ERSETZT")
    return alt


def _ziel(kandidat: Kandidat) -> dict[str, int]:
    return {{"beleg": "beleg_id", "einnahme": "einnahme_id",
             "zahlung": "zahlung_id"}[kandidat.typ]: kandidat.id}


def _anlegen(conn: sqlite3.Connection, p: Posten, datei: Datei, import_id: int,
             lieferanten: dict[str, int], ergebnis: Ergebnis, heute: date) -> None:
    """Legt die Buchung im Werkzeug an und vermerkt den WISO-Schluessel."""
    if p.art in (AUSGABE, ANLAGE):
        beleg_id, gut_id = _beleg_anlegen(conn, p, datei, lieferanten, heute)
        _registrieren(conn, p, datei, import_id, beleg_id=beleg_id, anlagegut_id=gut_id)
        ergebnis.belege += 1
        if gut_id is not None:
            ergebnis.anlagegueter += 1
        elif p.art == ANLAGE:
            ergebnis.hinweise.append(
                f"{anlagenname(p.zeile.text)}: Beleg mit Haken „Anlagegut“ angelegt — das "
                "Anlagegut bitte auf der Seite Anlagen übernehmen (Nutzungsdauer fehlt).")
    else:
        einnahme_id = _einnahme_anlegen(conn, p, datei)
        _registrieren(conn, p, datei, import_id, einnahme_id=einnahme_id)
        ergebnis.einnahmen += 1


def _angabe_vermerken(conn: sqlite3.Connection, art: str, schluessel: str, titel: str,
                      angaben: dict[str, object], import_id: int) -> bool:
    """Legt eine offene Angabe an oder ergaenzt sie. True, wenn sie neu ist."""
    alt = conn.execute("SELECT * FROM fehlende_angabe WHERE art = ? AND schluessel = ?",
                       (art, schluessel)).fetchone()
    if alt is None:
        conn.execute(
            "INSERT INTO fehlende_angabe (art, schluessel, titel, angaben_json, import_id,"
            " erfasst_am) VALUES (?, ?, ?, ?, ?, ?)",
            (art, schluessel, titel, json.dumps(angaben, ensure_ascii=False, sort_keys=True),
             import_id, db.jetzt_utc()))
        db.protokolliere(conn, "ANGABE_FEHLT", "fehlende_angabe", None, feld=schluessel,
                         wert_neu={"art": art, "titel": titel, **angaben})
        return True
    bisher = json.loads(alt["angaben_json"] or "{}")
    if art == "ANLAGEGUT":
        afa = {**bisher.get("afa", {}), **angaben.get("afa", {})}
        neu = {**bisher, **angaben, "afa": afa}
    else:
        neu = {**bisher, **angaben}
    if neu != bisher or (alt["erledigt_am"] is None and alt["import_id"] != import_id):
        conn.execute("UPDATE fehlende_angabe SET angaben_json = ?, import_id = ? WHERE id = ?",
                     (json.dumps(neu, ensure_ascii=False, sort_keys=True), import_id, alt["id"]))
    return False


def uebernehmen(conn: sqlite3.Connection, datenordner: Path, name: str, inhalt: bytes, *,
                fingerabdruck: str | None, auswahl: Auswahl, heute: date | None = None
                ) -> Ergebnis:
    """Uebernimmt die Datei nach der Vorschau -- alles oder nichts, in EINER Transaktion.

    Gelesen und geplant wird NOCH EINMAL, unter der Schreibsperre. Weicht
    das Ergebnis von der Vorschau ab (``fingerabdruck``), wird nichts
    geschrieben und :class:`VorschauVeraltet` mit dem neuen Plan geworfen.
    """
    heute = heute or date.fromisoformat(db.heute())
    datei = lies(name, inhalt, heute=heute)
    angelegt: Path | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = plane(conn, datei, auswahl, heute=heute)
        if fingerabdruck is not None and plan.fingerabdruck != fingerabdruck:
            raise VorschauVeraltet(plan)
        pfad, neu = _ablegen(datenordner, datei, inhalt)
        angelegt = pfad if neu else None
        ergebnis = _ausfuehren(conn, plan, auswahl, datenordner, pfad, heute)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        if angelegt is not None:
            try:
                os.chmod(angelegt, 0o644)
                angelegt.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return ergebnis


def aktion_fuer(auswahl: Auswahl, p: Posten) -> str:
    """Die Aktion einer Buchung: die gewaehlte, wenn sie erlaubt ist, sonst der Vorschlag."""
    gewaehlt = auswahl.aktionen.get(p.schluessel)
    if p.gruppe == NEU:
        erlaubt = {UEBERNEHMEN, AUSLASSEN, SPAETER} | ({VERKNUEPFEN} if p.kandidat else set())
    elif p.gruppe == UMGEBUCHT:
        erlaubt = {LOESCHEN, IGNORIEREN, SPAETER}
    else:
        erlaubt = {UEBERNEHMEN, IGNORIEREN, SPAETER}
    aktion = gewaehlt if gewaehlt in erlaubt else p.vorschlag
    if p.offen and aktion == UEBERNEHMEN and not (
            p.gruppe == GEAENDERT and p.alt is not None and p.alt["verknuepft"]):
        # Ohne Kategorie bzw. Einstufung laesst sich nichts anlegen; ein
        # Verknuepfen braucht keine.
        return SPAETER
    return aktion


def _ausfuehren(conn: sqlite3.Connection, plan: Plan, auswahl: Auswahl, datenordner: Path,
                pfad: Path, heute: date, *, nur: set[str] | None = None,
                import_id: int | None = None) -> Ergebnis:
    """Fuehrt den Plan aus -- in der offenen Transaktion des Aufrufers."""
    datei = plan.datei
    ergebnis = Ergebnis()
    aktionen = {p.schluessel: aktion_fuer(auswahl, p) for p in plan.posten
                if p.gruppe in (NEU, GEAENDERT, UMGEBUCHT)
                and (nur is None or p.schluessel in nur)}
    geplant = Counter(aktionen.values())
    if import_id is None:
        import_id = int(conn.execute(
            "INSERT INTO wiso_import (jahr, dateiname, sha256, pfad, zeilen, anzahl_json,"
            " importiert_am) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datei.jahr, datei.name, datei.sha256, pfade.relativ(datenordner, pfad),
             len(datei.zeilen) + len(datei.fehler),
             json.dumps({"vorschau": plan.anzahl, "aktionen": dict(geplant)},
                        sort_keys=True), db.jetzt_utc())).lastrowid)
    ergebnis.import_id = import_id

    # Zuerst die gemerkten Zuordnungen: Konto -> Kategorie, Zahler -> Einstufung.
    if nur is None:
        _zuordnungen_merken(conn, plan, auswahl)

    lieferanten: dict[str, int] = {}
    umgehaengt: set[int] = set()
    for p in plan.posten:
        aktion = aktionen.get(p.schluessel)
        if aktion is None:
            continue
        if p.gruppe == NEU:
            if aktion == UEBERNEHMEN:
                if p.alt is not None and p.alt["status"] == "AUSGELASSEN":
                    _status(conn, p.alt["id"], "ERSETZT")
                _anlegen(conn, p, datei, import_id, lieferanten, ergebnis, heute)
            elif aktion == VERKNUEPFEN and p.kandidat is not None:
                vorgaenger = _umhaengen(conn, p.kandidat)
                _kandidat_pruefen(conn, p.kandidat)
                if p.alt is not None and p.alt["status"] == "AUSGELASSEN":
                    _status(conn, p.alt["id"], "ERSETZT")
                buchung = f"{p.zeile.buchungsnummer}/{p.zeile.teilbuchung}"
                if vorgaenger is not None:
                    # Die neue Nummer uebernimmt die Rolle der alten: ein aus
                    # WISO angelegter Eintrag bleibt einer (Datei fehlt,
                    # Loeschung in WISO loescht ihn), ein verknuepfter bleibt
                    # verknuepft.
                    umgehaengt.add(vorgaenger["id"])
                    _registrieren(conn, p, datei, import_id,
                                  verknuepft=bool(vorgaenger["verknuepft"]),
                                  anlagegut_id=vorgaenger["anlagegut_id"], **_ziel(p.kandidat))
                    db.protokolliere(conn, "WISO_UMGEHAENGT", p.kandidat.typ, p.kandidat.id,
                                     wert_alt={"buchung": f"{vorgaenger['buchungsnummer']}/"
                                                          f"{vorgaenger['teilbuchung']}"},
                                     wert_neu={"buchung": buchung, "jahr": datei.jahr},
                                     begruendung="in WISO gelöscht und neu erfasst")
                    ergebnis.umgehaengt += 1
                else:
                    _registrieren(conn, p, datei, import_id, verknuepft=True,
                                  **_ziel(p.kandidat))
                    db.protokolliere(conn, "WISO_VERKNUEPFT", p.kandidat.typ, p.kandidat.id,
                                     wert_neu={"buchung": buchung, "jahr": datei.jahr})
                    ergebnis.verknuepft += 1
                if p.kandidat.zahlung_fehlt:
                    _zahlung_nachtragen(conn, p, datei)
            elif aktion == AUSLASSEN:
                if p.alt is not None and p.alt["status"] == "AUSGELASSEN":
                    _status(conn, p.alt["id"], "ERSETZT")
                _registrieren(conn, p, datei, import_id, status="AUSGELASSEN")
                ergebnis.ausgelassen += 1
            else:
                ergebnis.spaeter += 1
        elif p.gruppe == GEAENDERT and p.alt is not None:
            if aktion == UEBERNEHMEN:
                alt = p.alt
                grund = (f"in WISO geändert (Buchung {p.zeile.buchungsnummer}/{datei.jahr}), "
                         "neu übernommen")
                _status(conn, alt["id"], "ERSETZT")
                if alt["verknuepft"]:
                    ziel = {k: alt[k] for k in ("beleg_id", "einnahme_id", "zahlung_id")
                            if alt[k] is not None}
                    _registrieren(conn, p, datei, import_id, verknuepft=True, **ziel)
                    ergebnis.hinweise.append(
                        f"Buchung {p.zeile.buchungsnummer}: in WISO geändert — der verknüpfte "
                        "Eintrag im Tool bleibt, wie er ist; bitte vergleichen.")
                else:
                    _ziel_markieren(conn, alt, grund)
                    _anlegen(conn, p, datei, import_id, lieferanten, ergebnis, heute)
                    if not p.im_tool_geloescht:
                        _dateien_uebertragen(conn, alt, p)
                db.protokolliere(conn, "WISO_GEAENDERT", "wiso_buchung", alt["id"],
                                 wert_alt=json.loads(alt["inhalt_json"]),
                                 wert_neu=p.zeile.inhalt(), begruendung=grund)
                ergebnis.ersetzt += 1
            elif aktion == IGNORIEREN:
                _ignorieren(conn, p)
                ergebnis.ignoriert += 1
            else:
                ergebnis.spaeter += 1
        elif p.gruppe == UMGEBUCHT and p.alt is not None:
            if aktion == LOESCHEN:
                alt = p.alt
                was = "Betriebsausgabe" if alt["art"] in (AUSGABE, ANLAGE) else "Betriebseinnahme"
                grund = (f"in WISO umgebucht (Buchung {p.zeile.buchungsnummer}/{datei.jahr}), "
                         f"keine {was} mehr")
                _ziel_markieren(conn, alt, grund)
                _status(conn, alt["id"], "GELOESCHT")
                db.protokolliere(conn, "WISO_UMGEBUCHT", "wiso_buchung", alt["id"],
                                 wert_alt=json.loads(alt["inhalt_json"]),
                                 wert_neu=p.zeile.inhalt(), begruendung=f"{grund}: {p.umgebucht}")
                ergebnis.umgebucht += 1
            elif aktion == IGNORIEREN:
                _ignorieren(conn, p)
                ergebnis.ignoriert += 1
            else:
                ergebnis.spaeter += 1

    if nur is None:
        for zeile in plan.geloescht:
            if zeile["id"] in umgehaengt:
                continue
            aktion = auswahl.geloescht.get(zeile["id"], SPAETER)
            if aktion == LOESCHEN:
                grund = f"in WISO gelöscht (Buchung {zeile['buchungsnummer']}/{zeile['jahr']})"
                _ziel_markieren(conn, zeile, grund)
                _status(conn, zeile["id"], "GELOESCHT")
                db.protokolliere(conn, "WISO_GELOESCHT", "wiso_buchung", zeile["id"],
                                 wert_alt=json.loads(zeile["inhalt_json"]), begruendung=grund)
                ergebnis.geloescht += 1
            elif aktion == BEHALTEN:
                conn.execute("UPDATE wiso_buchung SET behalten_am = ?, geaendert_am = ?"
                             " WHERE id = ?", (db.jetzt_utc(), db.jetzt_utc(), zeile["id"]))
                db.protokolliere(conn, "WISO_BEHALTEN", "wiso_buchung", zeile["id"],
                                 begruendung="in WISO gelöscht, im Tool behalten")
                ergebnis.behalten += 1
        ergebnis.angaben_fehlen += _angaben(conn, plan, aktionen, import_id)
        db.protokolliere(conn, "WISO_IMPORT", "wiso_import", import_id,
                         wert_neu={"datei": datei.name, "jahr": datei.jahr,
                                   "vorschau": plan.anzahl,
                                   "ergebnis": {"belege": ergebnis.belege,
                                                "einnahmen": ergebnis.einnahmen,
                                                "anlagegueter": ergebnis.anlagegueter,
                                                "verknuepft": ergebnis.verknuepft,
                                                "ersetzt": ergebnis.ersetzt,
                                                "geloescht": ergebnis.geloescht,
                                                "umgebucht": ergebnis.umgebucht}},
                         hash_=datei.sha256)
    return ergebnis


def _ignorieren(conn: sqlite3.Connection, p: Posten) -> None:
    """Diese Fassung aus WISO nicht uebernehmen und nicht wieder fragen."""
    conn.execute("UPDATE wiso_buchung SET ignoriert_pruefsumme = ?, geaendert_am = ?"
                 " WHERE id = ?", (p.zeile.pruefsumme(), db.jetzt_utc(), p.alt["id"]))
    db.protokolliere(conn, "WISO_AENDERUNG_IGNORIERT", "wiso_buchung", p.alt["id"],
                     wert_neu=p.zeile.inhalt())


def _zuordnungen_merken(conn: sqlite3.Connection, plan: Plan, auswahl: Auswahl) -> None:
    """Merkt Konto- und Zahlerzuordnungen, die in der Vorschau galten."""
    for konto in plan.konten:
        kategorie_id = auswahl.konten.get(konto.konto, konto.kategorie_id)
        kategorie = _kategorie_zeile(conn, kategorie_id)
        if kategorie is None or kategorie["bereich"] != kategorien.BEREICH_AUSGABEN:
            continue
        alt = conn.execute("SELECT kategorie_id FROM wiso_konto WHERE konto = ?",
                           (konto.konto,)).fetchone()
        if alt is not None and alt["kategorie_id"] == kategorie["id"]:
            continue
        conn.execute(
            "INSERT INTO wiso_konto (konto, bezeichnung, kategorie_id, geaendert_am)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (konto) DO UPDATE SET"
            " kategorie_id = excluded.kategorie_id, bezeichnung = excluded.bezeichnung,"
            " geaendert_am = excluded.geaendert_am",
            (konto.konto, konto.bezeichnung[:120], kategorie["id"], db.jetzt_utc()))
        db.protokolliere(conn, "WISO_KONTO", "wiso_konto", None, feld=konto.konto,
                         wert_alt=alt["kategorie_id"] if alt is not None else None,
                         wert_neu=kategorie["id"])
    for z in plan.zahler:
        einstufung = auswahl.zahler.get(z.schluessel, z.einstufung)
        if einstufung not in einnahmen.EINSTUFUNGEN:
            continue
        kategorie = einnahmen.kategorie_der_einstufung(conn, einstufung)
        gemerkt = einnahmen.gemerkt(conn, z.schluessel)
        if gemerkt is not None and einnahmen.einstufung_der_kategorie(
                conn, gemerkt["kategorie_id"]) == einstufung:
            continue
        einnahmen.merken(conn, z.name, kategorie["id"], schluessel=z.schluessel)


def _angaben(conn: sqlite3.Connection, plan: Plan, aktionen: dict[str, str],
             import_id: int) -> int:
    """Vermerkt, was fehlt -- und erledigt, was dieser Import nachgeholt hat."""
    neu = 0
    jahr = plan.datei.jahr
    for a in plan.afa:
        if a.status == "FEHLT" and a.anl_nr:
            if _angabe_vermerken(conn, "ANLAGEGUT", f"anl:{a.anl_nr}", a.name,
                                 {"name": a.name, "anl_nr": a.anl_nr,
                                  "konto": a.zeile.konto if ist_anlagekonto(a.zeile.konto)
                                  else a.zeile.gegenkonto,
                                  "afa": {str(jahr): a.betrag_cent}}, import_id):
                neu += 1
    offen_konten: dict[str, list[Posten]] = {}
    offen_zahler: dict[str, list[Posten]] = {}
    for p in plan.posten:
        if p.gruppe != NEU or aktionen.get(p.schluessel) != SPAETER or not p.offen:
            continue
        if p.art == AUSGABE:
            offen_konten.setdefault(p.sachkonto, []).append(p)
        elif p.zahler is not None:
            offen_zahler.setdefault(p.zahler.schluessel, []).append(p)
    for konto, liste in offen_konten.items():
        if _angabe_vermerken(conn, "KONTO", konto, f"Konto {konto} {liste[0].kontobezeichnung}",
                             {"konto": konto, "bezeichnung": liste[0].kontobezeichnung,
                              "anzahl": len(liste), "cent": Plan.summe(liste), "jahr": jahr},
                             import_id):
            neu += 1
    for schluessel, liste in offen_zahler.items():
        name = liste[0].zahler.name if liste[0].zahler else schluessel
        if _angabe_vermerken(conn, "ZAHLER", schluessel, name,
                             {"name": name, "anzahl": len(liste), "cent": Plan.summe(liste),
                              "jahr": jahr}, import_id):
            neu += 1
    # Was jetzt eine Zuordnung hat und nicht mehr wartet, ist erledigt.
    for angabe in conn.execute("SELECT * FROM fehlende_angabe WHERE erledigt_am IS NULL"
                               " AND art IN ('KONTO', 'ZAHLER')").fetchall():
        if angabe["art"] == "KONTO" and angabe["schluessel"] not in offen_konten and conn.execute(
                "SELECT 1 FROM wiso_konto WHERE konto = ?", (angabe["schluessel"],)).fetchone():
            angabe_erledigt(conn, angabe["id"], "Konto zugeordnet")
        if angabe["art"] == "ZAHLER" and angabe["schluessel"] not in offen_zahler and \
                einnahmen.gemerkt(conn, angabe["schluessel"]) is not None:
            angabe_erledigt(conn, angabe["id"], "Zahler eingestuft")
    return neu


def angabe_erledigt(conn: sqlite3.Connection, angabe_id: int, wie: str,
                     anlagegut_id: int | None = None) -> None:
    conn.execute("UPDATE fehlende_angabe SET erledigt_am = ?,"
                 " anlagegut_id = COALESCE(?, anlagegut_id) WHERE id = ?",
                 (db.jetzt_utc(), anlagegut_id, angabe_id))
    db.protokolliere(conn, "ANGABE_ERGAENZT", "fehlende_angabe", angabe_id, begruendung=wie,
                     wert_neu={"anlagegut_id": anlagegut_id} if anlagegut_id else None)


def nachtragen(conn: sqlite3.Connection, datenordner: Path, import_id: int, *,
               konto: str | None = None, zahler: str | None = None,
               heute: date | None = None) -> Ergebnis:
    """Legt die zurueckgestellten Buchungen eines Kontos bzw. Zahlers an.

    Fuer "Angaben fehlen": sobald Kategorie bzw. Einstufung da ist (vorher
    gemerkt), wird die abgelegte Datei des Imports noch einmal gelesen und
    geplant, und genau deren neue Buchungen entstehen -- mit dem Vorschlag
    der Vorschau (verknuepfen, wo ein sicherer Treffer da ist). Nur in einer
    offenen Transaktion.
    """
    heute = heute or date.fromisoformat(db.heute())
    eintrag = conn.execute("SELECT * FROM wiso_import WHERE id = ?", (import_id,)).fetchone()
    if eintrag is None:
        raise ImportFehler("Den Import zu dieser Angabe gibt es nicht mehr.")
    pfad = pfade.absolut(datenordner, eintrag["pfad"])
    try:
        inhalt = pfad.read_bytes()
    except OSError:
        raise ImportFehler(f"Die Datei des Imports fehlt im Datenordner ({eintrag['pfad']}) — "
                           "bitte den WISO-Export erneut importieren.") from None
    if pruefsumme(inhalt) != eintrag["sha256"]:
        raise ImportFehler("Die abgelegte Datei des Imports passt nicht mehr zu ihrer "
                           "Prüfsumme — bitte den WISO-Export erneut importieren.")
    datei = lies(eintrag["dateiname"], inhalt, heute=heute)
    plan = plane(conn, datei, heute=heute)
    nur = {p.schluessel for p in plan.posten if p.gruppe == NEU and not p.offen
           and ((konto is not None and p.art == AUSGABE and p.sachkonto == konto)
                or (zahler is not None and p.zahler is not None
                    and p.zahler.schluessel == zahler))}
    ergebnis = _ausfuehren(conn, plan, Auswahl(), datenordner, pfad, heute, nur=nur,
                           import_id=import_id)
    db.protokolliere(conn, "WISO_NACHTRAG", "wiso_import", import_id,
                     wert_neu={"konto": konto, "zahler": zahler, "belege": ergebnis.belege,
                               "einnahmen": ergebnis.einnahmen,
                               "verknuepft": ergebnis.verknuepft})
    return ergebnis


def einnahme_durch_zahlung_ersetzen(conn: sqlite3.Connection, einnahme_id: int,
                                    zahlung_id: int, rechnungsnummer: str) -> None:
    """Dieselbe Einnahme kam ueber WISO und als Zahlung auf eine eigene Rechnung.

    Die Einnahme ohne Rechnung bekommt die Loeschmarkierung (mit Grund), und
    ihre WISO-Buchung haengt danach an der Zahlung (verknuepft) -- der
    naechste Import kennt sie dann und legt sie nicht wieder an. Nur in einer
    offenen Transaktion.
    """
    zeile = conn.execute("SELECT * FROM v_einnahme_gueltig WHERE id = ?",
                         (einnahme_id,)).fetchone()
    if zeile is None:
        raise ImportFehler(f"Einnahme {einnahme_id} gibt es nicht mehr oder sie ist schon "
                           "gelöscht — bitte die Zahlung noch einmal erfassen.")
    einnahmen.markieren(conn, einnahme_id, f"als Zahlung auf Rechnung {rechnungsnummer} "
                                           "erfasst — dieselbe Einnahme")
    alt = conn.execute("SELECT * FROM wiso_buchung WHERE einnahme_id = ? AND status = 'AKTIV'",
                       (einnahme_id,)).fetchone()
    if alt is None:
        return
    _status(conn, alt["id"], "ERSETZT")
    neu = conn.execute(
        "INSERT INTO wiso_buchung (jahr, buchungsnummer, teilbuchung, pruefsumme, inhalt_json,"
        " art, zahlung_id, verknuepft, import_id, status, ignoriert_pruefsumme, erfasst_am)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'AKTIV', ?, ?)",
        (alt["jahr"], alt["buchungsnummer"], alt["teilbuchung"], alt["pruefsumme"],
         alt["inhalt_json"], alt["art"], zahlung_id, alt["import_id"],
         alt["ignoriert_pruefsumme"], db.jetzt_utc())).lastrowid
    db.protokolliere(conn, "WISO_UMGEHAENGT", "zahlung", zahlung_id,
                     wert_alt={"einnahme_id": einnahme_id, "wiso_buchung": alt["id"]},
                     wert_neu={"wiso_buchung": neu,
                               "buchung": f"{alt['buchungsnummer']}/{alt['teilbuchung']}"},
                     begruendung=f"als Zahlung auf Rechnung {rechnungsnummer} erfasst")


# ================================================================== Abgleich

@dataclass(slots=True)
class AbgleichZeile:
    bereich: str
    bezeichnung: str
    wiso_cent: int
    tool_cent: int
    #: Erklaerungen der Differenz, etwa "im Tool direkt gebucht, nicht in WISO".
    gruende: list[str] = field(default_factory=list)

    @property
    def differenz_cent(self) -> int:
        return self.tool_cent - self.wiso_cent


@dataclass(slots=True)
class Abgleich:
    jahr: int
    import_zeile: sqlite3.Row | None
    zeilen: list[AbgleichZeile]
    gewinn_wiso_cent: int = 0
    gewinn_tool_cent: int = 0
    privat_cent: int = 0
    fehler: str | None = None


def importe(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Alle Importe, neueste zuerst."""
    return conn.execute("SELECT * FROM wiso_import ORDER BY importiert_am DESC, id DESC"
                        ).fetchall()


def importjahre(conn: sqlite3.Connection) -> list[int]:
    return [int(z["jahr"]) for z in conn.execute(
        "SELECT DISTINCT jahr FROM wiso_import ORDER BY jahr DESC")]


def abgleich(conn: sqlite3.Connection, datenordner: Path, jahr: int) -> Abgleich:
    """WISO-Summe gegen Tool-Summe je Bereich -- fuer den Parallelbetrieb.

    Grundlage auf der WISO-Seite ist die zuletzt eingespielte Datei des
    Jahres (aus dem Datenordner, gegen ihre Pruefsumme geprueft). Je Zeile
    stehen die Gruende einer Differenz: Eintraege, die nur im Tool stehen
    (dort direkt gebucht), und Buchungen, die nur in WISO stehen (nicht
    uebernommen).
    """
    from app.logik import euer

    eintrag = conn.execute("SELECT * FROM wiso_import WHERE jahr = ?"
                           " ORDER BY importiert_am DESC, id DESC LIMIT 1", (jahr,)).fetchone()
    if eintrag is None:
        return Abgleich(jahr, None, [], fehler=f"Für {jahr} ist noch kein WISO-Export "
                                                "eingespielt.")
    pfad = pfade.absolut(datenordner, eintrag["pfad"])
    try:
        inhalt = pfad.read_bytes()
    except OSError:
        return Abgleich(jahr, eintrag, [], fehler=f"Die Datei des letzten Imports fehlt "
                                                  f"({eintrag['pfad']}).")
    if pruefsumme(inhalt) != eintrag["sha256"]:
        return Abgleich(jahr, eintrag, [], fehler="Die abgelegte Datei passt nicht mehr zu "
                                                  "ihrer Prüfsumme.")
    datei = lies(eintrag["dateiname"], inhalt)
    plan = plane(conn, datei)
    verknuepft = _verknuepfte(conn, jahr)
    # Eintraege im Tool, deren WISO-Buchung inzwischen keine Geldbewegung mehr
    # ist (umgebucht auf Privat, AfA, Zahltag in der Zukunft ...).
    umgebucht = {(typ, int(p.alt[f"{typ}_id"])): p for p in plan.posten
                 if p.umgebucht and p.alt is not None and p.alt["status"] == "AKTIV"
                 for typ in ("beleg", "einnahme", "zahlung") if p.alt[f"{typ}_id"] is not None}

    # --- Einnahmen (ohne Erstattungen) und Erstattungen.
    wiso_einnahmen = [p for p in plan.posten if p.art == EINNAHME and p.gruppe != NICHT]
    wiso_erstattungen = [p for p in plan.posten if p.art == ERSTATTUNG and p.gruppe != NICHT]
    auswertung = euer.auswertung(conn, jahr)
    erstattung_kat = einnahmen.EINSTUFUNGEN[einnahmen.ERSTATTUNG]
    alle = _tool_einnahmen(conn, jahr)
    tool_erstattung = [z for z in alle if z["schluessel"] == erstattung_kat]
    tool_einnahmen = [z for z in alle if z["schluessel"] != erstattung_kat]
    zeilen = [
        _zeile_abgleich("Einnahmen", "Betriebseinnahmen (Erlöskonten)", wiso_einnahmen,
                        tool_einnahmen, verknuepft, umgebucht),
    ]
    if wiso_erstattungen or tool_erstattung:
        zeilen.append(_zeile_abgleich(
            "Einnahmen", "Erstattungen (WISO: Haben auf Aufwandskonten; Tool: Einnahme "
            "„Erstattung“)", wiso_erstattungen, tool_erstattung, verknuepft, umgebucht))

    # --- Ausgaben je Kategorie.
    wiso_ausgaben = [p for p in plan.posten if p.art == AUSGABE and p.gruppe != NICHT]
    tool_ausgaben = _tool_ausgaben(conn, jahr)
    bezahlt = {z["id"] for z in tool_ausgaben}
    unbezahlt = {p.schluessel for p in wiso_ausgaben
                 if p.alt is not None and p.alt["status"] == "AKTIV"
                 and p.alt["beleg_id"] is not None and p.alt["beleg_id"] not in bezahlt}
    im_tool = {p.schluessel: _kategorie_im_tool(conn, p, verknuepft) for p in wiso_ausgaben}
    namen = {z["kategorie"] for z in tool_ausgaben} | set(im_tool.values())
    for kategorie in sorted(namen, key=lambda n: db.sortierschluessel(n or "~")):
        zeilen.append(_zeile_abgleich(
            "Ausgaben", kategorie or "ohne Kategorie",
            [p for p in wiso_ausgaben if im_tool[p.schluessel] == kategorie],
            [z for z in tool_ausgaben if z["kategorie"] == kategorie], verknuepft, umgebucht,
            unbezahlt))

    # --- AfA.
    wiso_afa = sum(p.betrag_cent for p in plan.posten if p.gruppe == AFA)
    tool_afa = sum(anlagen.afa_summe(conn, jahr).values())
    afa = AbgleichZeile("AfA", "Abschreibungen (AfA)", wiso_afa, tool_afa)
    for a in plan.afa:
        if a.status == "FEHLT":
            afa.gruende.append(f"{a.name}: Angaben fehlen — im Tool noch keine AfA "
                               f"(WISO: {euro(a.betrag_cent)} €)")
        elif a.tool_cent is not None and a.tool_cent != a.betrag_cent:
            afa.gruende.append(f"{a.name}: Tool {euro(a.tool_cent)} €, WISO "
                               f"{euro(a.betrag_cent)} € — Nutzungsdauer bzw. Anschaffungstag "
                               "prüfen")
    zeilen.append(afa)

    gewinn_wiso = (sum(p.betrag_cent for p in wiso_einnahmen)
                   + sum(p.betrag_cent for p in wiso_erstattungen)
                   - sum(p.betrag_cent for p in wiso_ausgaben) - wiso_afa)
    return Abgleich(jahr, eintrag, zeilen, gewinn_wiso_cent=gewinn_wiso,
                    gewinn_tool_cent=auswertung.gewinn_cent,
                    privat_cent=sum(p.betrag_cent for p in plan.posten if p.gruppe == PRIVAT))


def _verknuepfte(conn: sqlite3.Connection, jahr: int) -> dict[tuple[str, int], sqlite3.Row]:
    """Die geltenden WISO-Buchungen des Jahres je Eintrag im Tool."""
    gefunden: dict[tuple[str, int], sqlite3.Row] = {}
    for z in conn.execute("SELECT * FROM wiso_buchung WHERE jahr = ? AND status = 'AKTIV'",
                          (jahr,)):
        for typ in ("beleg", "einnahme", "zahlung"):
            if z[f"{typ}_id"] is not None:
                gefunden[(typ, int(z[f"{typ}_id"]))] = z
    return gefunden


def _kategorie_im_tool(conn: sqlite3.Connection, p: Posten,
                       verknuepft: dict[tuple[str, int], sqlite3.Row]) -> str | None:
    """Die Kategorie, unter der eine WISO-Ausgabe im Tool steht (sonst die geplante)."""
    if p.alt is not None and p.alt["status"] == "AKTIV" and p.alt["beleg_id"] is not None:
        zeile = conn.execute(
            "SELECT k.bezeichnung FROM v_beleg_gueltig b"
            " JOIN euer_kategorie k ON k.id = b.kategorie_id"
            " WHERE b.id = ?", (p.alt["beleg_id"],)).fetchone()
        if zeile is not None:
            return zeile["bezeichnung"]
    return p.kategorie


def _tool_einnahmen(conn: sqlite3.Connection, jahr: int) -> list[dict]:
    """Jede Einnahme des Jahres im Tool: Zahlungen auf Rechnungen und Einnahmen ohne Rechnung."""
    zeilen = [
        {"typ": "zahlung", "id": z["id"], "cent": int(z["betrag_cent"]), "schluessel": None}
        for z in conn.execute(
            "SELECT z.id, z.betrag_cent FROM zahlung z JOIN rechnung r ON r.id = z.rechnung_id"
            " WHERE strftime('%Y', z.datum) = ? AND r.typ IN ('RECHNUNG', 'STORNO')",
            (f"{jahr:04d}",))
    ]
    zeilen += [
        {"typ": "einnahme", "id": e["id"], "cent": int(e["betrag_cent"]),
         "schluessel": e["schluessel"]}
        for e in conn.execute(
            "SELECT e.id, e.betrag_cent, k.schluessel FROM v_einnahme_gueltig e"
            " JOIN euer_kategorie k ON k.id = e.kategorie_id"
            " WHERE strftime('%Y', e.zufluss_am) = ? AND k.bereich = 20", (f"{jahr:04d}",))
    ]
    return zeilen


def _tool_ausgaben(conn: sqlite3.Connection, jahr: int) -> list[dict]:
    """Jeder bezahlte Beleg des Jahres im Tool mit seinem EUeR-Betrag und seiner Kategorie."""
    from app.logik.euer import _betrieblicher_anteil

    return [
        {"typ": "beleg", "id": z["id"], "kategorie": z["kategorie"],
         "cent": _betrieblicher_anteil(int(z["geflossen"]), int(z["betrag_brutto_cent"]),
                                       int(z["betrag_betrieblich_cent"] or 0))}
        for z in conn.execute(
            """
            SELECT b.id, b.betrag_brutto_cent, b.betrag_betrieblich_cent, k.bezeichnung
                   AS kategorie, COALESCE(SUM(ABS(z.betrag_cent)), 0) AS geflossen
              FROM zahlung z
              JOIN v_beleg_gueltig b ON b.id = z.beleg_id
              LEFT JOIN euer_kategorie k ON k.id = b.kategorie_id
             WHERE strftime('%Y', z.datum) = ? AND b.ist_anlagegut = 0
             GROUP BY b.id
            """, (f"{jahr:04d}",))
    ]


def _zeile_abgleich(bereich: str, bezeichnung: str | None, wiso: list[Posten], tool: list[dict],
                    verknuepft: dict[tuple[str, int], sqlite3.Row],
                    umgebucht: dict[tuple[str, int], Posten] | None = None,
                    unbezahlt: set[str] | None = None) -> AbgleichZeile:
    zeile = AbgleichZeile(bereich, bezeichnung or "ohne Kategorie",
                          sum(p.betrag_cent for p in wiso), sum(t["cent"] for t in tool))
    umgebucht = umgebucht or {}
    nur_tool = [t for t in tool if (t["typ"], t["id"]) not in verknuepft]
    if nur_tool:
        zeile.gruende.append(
            f"{len(nur_tool)} {'Eintrag' if len(nur_tool) == 1 else 'Einträge'} "
            f"({euro(sum(t['cent'] for t in nur_tool))} €) im Tool direkt gebucht, nicht in "
            "WISO")
    for t in tool:
        p = umgebucht.get((t["typ"], t["id"]))
        if p is not None:
            zeile.gruende.append(
                f"Buchung {p.zeile.buchungsnummer} ({euro(t['cent'])} €) in WISO umgebucht "
                f"({p.umgebucht}) — im Tool "
                + ("bewusst behalten" if p.gruppe == BEKANNT
                   else "noch gebucht; beim Import „im Tool löschen“ wählen"))
    nur_wiso = [p for p in wiso if p.gruppe in (NEU, GEAENDERT)
                or (p.gruppe == BEKANNT and (p.im_tool_geloescht
                                             or (p.alt is not None
                                                 and p.alt["status"] == "AUSGELASSEN")))]
    if nur_wiso:
        zeile.gruende.append(
            f"{len(nur_wiso)} {'Buchung' if len(nur_wiso) == 1 else 'Buchungen'} "
            f"({euro(sum(p.betrag_cent for p in nur_wiso))} €) in WISO, im Tool nicht "
            "übernommen bzw. dort gelöscht")
    offen = [p for p in wiso if p.schluessel in (unbezahlt or set())]
    if offen:
        zeile.gruende.append(
            f"{len(offen)} {'Buchung' if len(offen) == 1 else 'Buchungen'} "
            f"({euro(sum(p.betrag_cent for p in offen))} €) verknüpft, im Tool unbezahlt "
            "(oder in einem anderen Jahr bezahlt) — die Zahlung am Beleg erfassen")
    if zeile.differenz_cent and not zeile.gruende:
        zeile.gruende.append("Beträge weichen ab (andere Quote, Teilzahlung oder ein "
                             "verknüpfter Eintrag mit anderem Betrag)")
    return zeile

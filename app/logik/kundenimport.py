"""Kundenimport: viele Kunden auf einmal aus einer Tabelle.

Der Ablauf hat drei Schritte, und keiner schreibt etwas, bevor der Nutzer
die Vorschau bestaetigt hat:

1. :func:`vorlage` -- eine Excel-Datei zum Befuellen, mit genau den Feldern
   des Kundenformulars, Auswahllisten und einem Blatt "Hilfe".
2. :func:`pruefe_datei`, :func:`lies` und :func:`plane` -- die hochgeladene
   Datei wird geprueft, gelesen und gegen den Bestand geplant: je Zeile "wird
   angelegt", "schon vorhanden", "gleicher Name wie ..." oder "fehlerhaft".
3. :func:`lege_an` -- liest und plant dieselbe Datei NOCH EINMAL, unter der
   Schreibsperre, und legt nur an, wenn das Ergebnis der Vorschau entspricht.
   Was der Browser zurueckschickt, sind nur der Schluessel der Datei, der
   Fingerabdruck der Vorschau und die Haken "trotzdem anlegen" -- keine
   Kundendaten, die sich unterwegs veraendern liessen.

Geprueft wird mit :func:`app.logik.kunden.pruefe_kunde`, derselben Pruefung
wie im Formular. Dazu kommen die Kombinationen, die erst beim Festschreiben
ROT wuerden (:func:`app.logik.kunden.querpruefung`): ein importierter Kunde,
auf den sich keine Rechnung schreiben laesst, waere nur Ballast. Vorhandene
Kunden werden nie ueberschrieben -- sie drucken auf offenen Entwuerfen, und
eine Anschrift, die sich still aendert, faellt erst beim Empfaenger auf.

Eine fremde Datei ist Eingabe von aussen. Deshalb vor dem Lesen: Endung und
Kennung pruefen, Makros abweisen, die entpackte Groesse begrenzen (eine
xlsx-Datei ist ein ZIP), Zeilen und Spalten begrenzen. XML-Entitaeten
entschaerft openpyxl selbst, sobald ``defusedxml`` installiert ist (eine
Abhaengigkeit in pyproject.toml).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import warnings
import zipfile
import zlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import PurePath

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from app import db
from app.filter import LAENDER
from app.logik import eingabe
from app.logik import kunden as stamm

#: Groesste Datei. Tausend Kunden in der Vorlage sind rund 100 KB.
MAX_BYTES = 2 * 1024 * 1024
#: Groesste entpackte Summe aller Teile einer xlsx-Datei (sie ist ein ZIP).
MAX_ENTPACKT = 20 * 1024 * 1024
#: Hoechstens so viele Teile im ZIP. Die Vorlage hat ein gutes Dutzend.
MAX_MITGLIEDER = 100
#: Entpackt / gepackt je Teil. Die Vorlage kommt auf etwa 12, gewoehnliche
#: Tabellen auf 20 bis 50; eine Zip-Bombe auf Tausende.
MAX_VERHAELTNIS = 200
#: Datenzeilen und Spalten, die gelesen werden.
MAX_ZEILEN = 1000
MAX_SPALTEN = 40
#: Bis zu welcher Zeile der Tabelle ueberhaupt gelesen wird -- Kopfzeile,
#: Kundenzeilen und reichlich Platz fuer Leerzeilen dazwischen. Leere Zeilen
#: zaehlen nicht gegen MAX_ZEILEN; ohne diese Grenze liefe das Lesen bis zur
#: letzten belegten Zeile, und openpyxl liefert im read_only-Modus jede
#: fehlende Zeile davor als leere Zeile: eine einzige Zelle in Zeile
#: 1.048.576 kostete eine Million Zeilen und gut 15 Sekunden.
MAX_TABELLENZEILEN = 3 * MAX_ZEILEN
#: Zeichen je Zelle; die Notiz darf laenger sein.
MAX_ZEICHEN = 200
MAX_ZEICHEN_NOTIZ = 2000

#: Steht das in der Spalte Kundennummer, ist die Zeile die Beispielzeile der
#: Vorlage -- sie wird nicht angelegt.
BEISPIEL = "Beispiel"

#: Status einer Zeile in der Vorschau.
NEU, VORHANDEN, NAMENSGLEICH, FEHLER, UEBERSPRUNGEN = (
    "NEU", "VORHANDEN", "NAMENSGLEICH", "FEHLER", "UEBERSPRUNGEN")

#: Vorbelegung leerer Zellen -- dieselbe wie im Formular "Neuer Kunde".
VORGABEN = {"typ": "UNTERNEHMER", "leistungsort": "INLAND", "gruppierung": "PRO_TITEL",
            "aktiv": "1"}

_JA = {"ja", "j", "x", "1", "aktiv", "true", "wahr"}
_NEIN = {"nein", "n", "0", "stillgelegt", "inaktiv", "false", "falsch"}


class ImportFehler(ValueError):
    """Die Datei als Ganzes ist unbrauchbar; die Meldung sagt dem Nutzer, was zu tun ist."""


class VorschauVeraltet(Exception):
    """Zwischen Vorschau und Bestaetigung hat sich der Kundenstamm geaendert."""

    def __init__(self, plan: Plan) -> None:
        super().__init__("Der Kundenstamm hat sich seit der Vorschau geändert.")
        self.plan = plan


@dataclass(frozen=True)
class Spalte:
    """Eine Spalte der Vorlage: Feld des Kundenformulars, Ueberschrift, Hilfetext."""

    feld: str
    ueberschrift: str
    #: Weitere Ueberschriften, unter denen die Spalte erkannt wird -- auch die
    #: des eigenen Excel-Exports (Blatt "Kunden").
    aliase: tuple[str, ...]
    pflicht: bool
    #: text, nummer, plz, geld, tage, auswahl
    art: str
    leer: str
    hilfe: str
    breite: int = 18


SPALTEN: tuple[Spalte, ...] = (
    Spalte("kundennummer", "Kundennummer", ("Kundennr", "Kunden-Nr."), False, "nummer",
           "wird automatisch vergeben",
           "Nur beim Übernehmen eines Altbestands: die Nummer, die auf den bisherigen "
           "Rechnungen steht, im eingestellten Format. „Beispiel“ markiert die "
           "Beispielzeile — sie wird nie angelegt.", 14),
    Spalte("name", "Name", ("Firma", "Firmenname", "Kundenname", "Kunde"), True, "text",
           "—", "Vollständiger Name bzw. Firma (§ 34a Satz 1 Nr. 1 UStDV).", 28),
    Spalte("rechnungsname", "Rechnungsname", (), False, "text",
           "der Name", "Nur wenn auf der Rechnung ein anderer Name stehen soll.", 22),
    Spalte("ansprechpartner", "Ansprechpartner", ("Kontakt", "Ansprechperson"), False, "text",
           "keiner", "Erscheint als z. Hd.-Zeile.", 20),
    Spalte("strasse", "Straße und Hausnummer", ("Straße", "Strasse", "Anschrift", "Adresse"),
           True, "text", "—", "Vollständige Anschrift (§ 34a Satz 1 Nr. 1 UStDV).", 26),
    Spalte("plz", "PLZ", ("Postleitzahl",), True, "plz", "—",
           "Als Text, damit die führende Null bleibt (01067). Eine deutsche PLZ hat "
           "5 Ziffern.", 9),
    Spalte("ort", "Ort", ("Stadt",), True, "text", "—", "", 18),
    Spalte("land", "Land", ("Staat", "Länderkürzel"), False, "auswahl", "Deutschland",
           "Name aus der Liste oder ISO-Kürzel (DE, AT, CH …).", 16),
    Spalte("email", "E-Mail", ("Mail", "E-Mail-Adresse"), False, "text", "keine",
           "Für den Versand der Rechnung als PDF.", 26),
    Spalte("ust_idnr", "USt-IdNr.", ("USt-Id", "UStId", "Umsatzsteuer-Id", "Steuernummer",
                                      "VAT", "UID"), False, "text", "keine",
           "Pflicht bei Reverse Charge, dann im Format des Landes (z. B. ATU12345678). "
           "Sonst jede Steuernummer, z. B. CHE-123.456.789 MWST.", 18),
    Spalte("typ", "Kundentyp", ("Typ",), False, "auswahl", "Unternehmer",
           "Unternehmer, Juristische Person oder Privatperson.", 18),
    Spalte("leistungsort", "Leistungsort", (), False, "auswahl", "Inland",
           "Inland, EU-Unternehmer (Reverse Charge) oder Drittland. Bei einem Unternehmer "
           "im Ausland nie Inland (§ 3a Abs. 2 UStG). Nur Inland zählt in die "
           "§ 19-Umsatzgrenze.", 30),
    Spalte("satz_euro", "Stundensatz (€)", ("Stundensatz", "Satz"), False, "geld",
           "Standardsatz des Betriebs", "Größer als 0, z. B. 85,00.", 14),
    Spalte("zahlungsziel", "Zahlungsziel (Tage)", ("Zahlungsfrist",), False, "tage",
           "Zahlungsziel des Betriebs", "Ganze Tage, 0 bis 365.", 12),
    Spalte("gruppierung", "Gruppierung", ("Rechnungspositionen gruppieren",), False,
           "auswahl", "pro Titel",
           "Wie die Zeiten auf der Rechnung zusammengefasst werden.", 18),
    Spalte("aktiv", "Aktiv", (), False, "auswahl", "ja",
           "„nein“ legt den Kunden gleich stillgelegt an.", 8),
    Spalte("notiz", "Notiz", ("Bemerkung", "Anmerkung", "Notizen"), False, "text", "keine",
           "Nur für dich; erscheint auf keiner Rechnung.", 30),
)
SPALTE = {s.feld: s for s in SPALTEN}

#: Auswahllisten der Vorlage: Feld -> angebotene Texte.
AUSWAHL = {
    "typ": [texte[0] for texte in stamm.TYP_TEXTE.values()],
    "leistungsort": [texte[0] for texte in stamm.LEISTUNGSORT_TEXTE.values()],
    "gruppierung": [texte[0] for texte in stamm.GRUPPIERUNG_TEXTE.values()],
    "aktiv": ["ja", "nein"],
    # Jeder Name einmal ("GB" und "UK" heissen beide Vereinigtes Koenigreich).
    "land": sorted(dict.fromkeys(LAENDER.values()), key=db.sortierschluessel),
}


def _schluessel(text: object) -> str:
    """Eine Ueberschrift ohne Gross/klein, Klammerzusatz, Sternchen und Satzzeichen.

    >>> _schluessel("Stundensatz (€)"), _schluessel("USt-IdNr."), _schluessel("Name*")
    ('stundensatz', 'ustidnr', 'name')
    >>> _schluessel("Straße und Hausnummer"), _schluessel("Gruppierung (Vorbelegung)")
    ('strasseundhausnummer', 'gruppierung')
    """
    roh = re.sub(r"\(.*?\)", "", str(text or "")).casefold().replace("ß", "ss")
    return re.sub(r"[^0-9a-zäöü]", "", roh)


_ZU_FELD: dict[str, str] = {}
for _s in SPALTEN:
    for _name in (_s.feld, _s.ueberschrift, *_s.aliase):
        _ZU_FELD.setdefault(_schluessel(_name), _s.feld)


# ------------------------------------------------------------------- Vorlage

_KOPF = PatternFill("solid", fgColor="E8E8E0")
_PFLICHT = PatternFill("solid", fgColor="F6E7C8")
_BEISPIEL = Font(italic=True, color="7A7A70")


def _beispielzeile() -> dict[str, str]:
    """Die Beispielzeile: erfunden und als Beispiel erkennbar."""
    return {
        "kundennummer": BEISPIEL, "name": "Beispiel GmbH (diese Zeile wird nicht angelegt)",
        "rechnungsname": "", "ansprechpartner": "Erika Mustermann",
        "strasse": "Musterstraße 1", "plz": "01067", "ort": "Dresden", "land": "Deutschland",
        "email": "rechnung@beispiel.example", "ust_idnr": "",
        "typ": AUSWAHL["typ"][0], "leistungsort": AUSWAHL["leistungsort"][0],
        "satz_euro": "", "zahlungsziel": "", "gruppierung": AUSWAHL["gruppierung"][0],
        "aktiv": "ja", "notiz": "Eigene Kunden ab Zeile 3 eintragen oder diese Zeile "
                               "überschreiben (dann auch „Beispiel“ löschen).",
    }


def vorlage(conn: sqlite3.Connection) -> bytes:
    """Die Importvorlage als .xlsx: Blatt "Kunden", "Hilfe" und ausgeblendet "Listen".

    Mit openpyxl erzeugt, bei jedem Abruf neu: die Hilfe nennt das
    eingestellte Format der Kundennummer und die naechste freie Nummer.
    Kundennummer, PLZ und USt-IdNr. sind als Text formatiert -- sonst macht
    das Tabellenprogramm aus 01067 die Zahl 1067.
    """
    format_ = stamm.format_aus_betrieb(conn)
    mappe = Workbook()
    blatt = mappe.active
    blatt.title = "Kunden"
    listen = mappe.create_sheet("Listen")

    for nr, s in enumerate(SPALTEN, start=1):
        zelle = blatt.cell(row=1, column=nr, value=s.ueberschrift + (" *" if s.pflicht else ""))
        zelle.font = Font(bold=True)
        zelle.fill = _PFLICHT if s.pflicht else _KOPF
        zelle.alignment = Alignment(vertical="top", wrap_text=True)
        buchstabe = get_column_letter(nr)
        blatt.column_dimensions[buchstabe].width = s.breite
        if s.art in ("nummer", "plz") or s.feld == "ust_idnr":
            for zeile in range(2, MAX_ZEILEN + 2):
                blatt.cell(row=zeile, column=nr).number_format = "@"
        elif s.art == "geld":
            for zeile in range(2, MAX_ZEILEN + 2):
                blatt.cell(row=zeile, column=nr).number_format = '#,##0.00\\ "€"'
    beispiel = _beispielzeile()
    for nr, s in enumerate(SPALTEN, start=1):
        blatt.cell(row=2, column=nr, value=beispiel[s.feld] or None).font = _BEISPIEL
    blatt.freeze_panes = "B2"

    for spalte_nr, (feld, werte) in enumerate(AUSWAHL.items(), start=1):
        for zeile, text in enumerate(werte, start=1):
            listen.cell(row=zeile, column=spalte_nr, value=text)
        buchstabe = get_column_letter(spalte_nr)
        ziel = get_column_letter([s.feld for s in SPALTEN].index(feld) + 1)
        pruefung = DataValidation(
            type="list", formula1=f"=Listen!${buchstabe}$1:${buchstabe}${len(werte)}",
            allow_blank=True, showErrorMessage=True,
            # Beim Land nur eine Warnung: ein ISO-Kuerzel wie NO oder XK
            # steht in keiner Liste und ist trotzdem richtig.
            errorStyle="warning" if feld == "land" else "stop",
            errorTitle="Bitte aus der Liste wählen",
            error=("Nicht in der Liste. Ein ISO-Kürzel (z. B. NO) geht trotzdem."
                   if feld == "land" else "Bitte einen Wert aus der Liste wählen."),
        )
        pruefung.add(f"{ziel}2:{ziel}{MAX_ZEILEN + 1}")
        blatt.add_data_validation(pruefung)
    listen.sheet_state = "hidden"

    ziel_spalte = get_column_letter([s.feld for s in SPALTEN].index("zahlungsziel") + 1)
    tage = DataValidation(type="whole", operator="between", formula1="0", formula2="365",
                          allow_blank=True, showErrorMessage=True,
                          errorTitle="Zahlungsziel", error="Ganze Tage von 0 bis 365.")
    tage.add(f"{ziel_spalte}2:{ziel_spalte}{MAX_ZEILEN + 1}")
    blatt.add_data_validation(tage)
    satz_spalte = get_column_letter([s.feld for s in SPALTEN].index("satz_euro") + 1)
    satz = DataValidation(type="decimal", operator="greaterThan", formula1="0",
                          allow_blank=True, showErrorMessage=True,
                          errorTitle="Stundensatz", error="Ein Betrag größer als 0, z. B. 85,00.")
    satz.add(f"{satz_spalte}2:{satz_spalte}{MAX_ZEILEN + 1}")
    blatt.add_data_validation(satz)

    _hilfeblatt(mappe.create_sheet("Hilfe", 1), conn, format_)
    puffer = io.BytesIO()
    mappe.save(puffer)
    return puffer.getvalue()


def _hilfeblatt(blatt, conn: sqlite3.Connection, format_: str) -> None:
    """Erklaerung je Spalte: Pflicht, leer bedeutet, erlaubte Werte."""
    zeilen: list[tuple[str, ...]] = [
        ("Kundenimport — so geht's",),
        ("",),
        ("1. Im Blatt „Kunden“ eine Zeile je Kunde ausfüllen. Pflicht sind die Spalten mit *: "
         "Name, Straße und Hausnummer, PLZ, Ort (§ 34a Satz 1 Nr. 1 UStDV).",),
        ("2. Die Beispielzeile (Kundennummer „Beispiel“) wird nie angelegt. Stehen lassen, "
         "löschen oder überschreiben — beim Überschreiben „Beispiel“ entfernen.",),
        ("3. Nur Werte, keine Formeln. Die Datei als „Excel-Arbeitsmappe (.xlsx)“ speichern, "
         "nicht mit Makros (.xlsm). Eine CSV-Datei (Trennzeichen ; oder ,) geht auch.",),
        ("4. Im Steuer-Tool unter Kunden → Kunden importieren hochladen. Die Vorschau zeigt je "
         "Zeile, was passiert; angelegt wird erst mit dem Knopf darunter.",),
        ("5. Vorhandene Kunden (gleiche Kundennummer oder USt-IdNr.) werden nie überschrieben. "
         "Dieselbe Datei noch einmal hochzuladen ist deshalb gefahrlos.",),
        ("",),
        (f"Format der Kundennummer: {format_.format(1)} · nächste freie Nummer: "
         f"{stamm.naechste_kundennummer(conn)}",),
        ("",),
        ("Spalte", "Pflicht", "Leer bedeutet", "Erklärung"),
    ]
    for s in SPALTEN:
        erlaubt = ", ".join(AUSWAHL[s.feld]) if s.feld in AUSWAHL and s.feld != "land" else ""
        hilfe = s.hilfe + (f" Erlaubt: {erlaubt}." if erlaubt else "")
        zeilen.append((s.ueberschrift, "ja" if s.pflicht else "nein", s.leer, hilfe.strip()))
    for zeile in zeilen:
        blatt.append(list(zeile))
    blatt["A1"].font = Font(bold=True, size=14)
    kopf = 11
    for zelle in blatt[kopf]:
        zelle.font = Font(bold=True)
        zelle.fill = _KOPF
    for nr, breite in enumerate((24, 9, 26, 90), start=1):
        blatt.column_dimensions[get_column_letter(nr)].width = breite
    for zeile in blatt.iter_rows(min_row=kopf + 1):
        for zelle in zeile:
            zelle.alignment = Alignment(vertical="top", wrap_text=True)


# ---------------------------------------------------------- Datei pruefen

def pruefe_datei(name: str, inhalt: bytes) -> str:
    """Weist ab, was nicht gelesen wird; gibt 'xlsx' oder 'csv' zurueck.

    Alle Meldungen auf Deutsch und ohne Pfad: sie stehen so auf der Seite.

    >>> pruefe_datei("kunden.csv", "Name;Ort\\n".encode())
    'csv'
    >>> pruefe_datei("kunden.xlsm", b"PK\\x03\\x04")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    app.logik.kundenimport.ImportFehler: Dateien mit Makros (.xlsm) nimmt der Import nicht an. ...
    """
    endung = PurePath(name or "").suffix.lower()
    if endung in (".xlsm", ".xltm", ".xlsb", ".xlam"):
        raise ImportFehler(
            f"Dateien mit Makros ({endung}) nimmt der Import nicht an. Bitte in Excel als "
            "„Excel-Arbeitsmappe (.xlsx)“ speichern und die Datei erneut wählen."
        )
    if endung in (".xls", ".xlt"):
        raise ImportFehler(
            "Das alte Excel-Format (.xls) liest der Import nicht. Bitte als "
            "„Excel-Arbeitsmappe (.xlsx)“ speichern."
        )
    if endung not in (".xlsx", ".csv"):
        raise ImportFehler(
            "Bitte eine Excel-Datei (.xlsx) oder eine CSV-Datei wählen — am einfachsten die "
            "Vorlage."
        )
    if not inhalt:
        raise ImportFehler("Die Datei ist leer.")
    if len(inhalt) > MAX_BYTES:
        raise ImportFehler(
            f"Die Datei ist zu groß ({len(inhalt) / 1024 / 1024:.1f} MB, höchstens "
            f"{MAX_BYTES // 1024 // 1024} MB). Für {MAX_ZEILEN} Kunden reichen etwa 100 KB."
            .replace(".", ",", 1)
        )
    ole = inhalt.startswith(b"\xd0\xcf\x11\xe0")
    zip_ = inhalt.startswith(b"PK\x03\x04")
    if endung == ".csv":
        if ole or zip_:
            raise ImportFehler(
                "Die Datei heißt .csv, ist aber keine Textdatei — vermutlich eine Excel-Datei. "
                "Bitte als .xlsx hochladen oder in Excel „CSV UTF-8“ speichern."
            )
        return "csv"
    if ole:
        raise ImportFehler(
            "Die Datei ist mit einem Kennwort geschützt oder im alten .xls-Format. Bitte ohne "
            "Kennwort als „Excel-Arbeitsmappe (.xlsx)“ speichern."
        )
    if not zip_:
        raise ImportFehler("Die Datei heißt .xlsx, ist aber keine Excel-Arbeitsmappe.")
    _pruefe_zip(inhalt)
    return "xlsx"


def _pruefe_zip(inhalt: bytes) -> None:
    """Vorpruefung des ZIP-Containers, bevor openpyxl ihn entpackt.

    ``zipfile`` haelt sich beim Lesen an die angegebene entpackte Groesse;
    ein gefaelschter Kopf endet mit einem Pruefsummenfehler. Die Summe der
    angegebenen Groessen ist damit eine echte Obergrenze.
    """
    kaputt = "Die Datei ist beschädigt oder keine Excel-Arbeitsmappe (.xlsx)."
    try:
        with zipfile.ZipFile(io.BytesIO(inhalt)) as archiv:
            teile = archiv.infolist()
            if len(teile) > MAX_MITGLIEDER:
                raise ImportFehler(
                    f"Die Excel-Datei besteht aus ungewöhnlich vielen Teilen ({len(teile)}); "
                    "eine Kundentabelle hat ein gutes Dutzend. Bitte nur das Blatt mit den "
                    "Kunden in eine neue Datei kopieren."
                )
            if sum(t.file_size for t in teile) > MAX_ENTPACKT:
                raise ImportFehler(
                    "Die Excel-Datei ist entpackt größer als "
                    f"{MAX_ENTPACKT // 1024 // 1024} MB — für eine Kundentabelle viel zu groß."
                )
            for teil in teile:
                if teil.file_size > 64 * 1024 and (
                        teil.file_size > MAX_VERHAELTNIS * max(teil.compress_size, 1)):
                    raise ImportFehler(
                        "Die Excel-Datei ist auffällig stark gepackt und wird nicht entpackt. "
                        "Bitte die Kunden in die Vorlage kopieren."
                    )
            namen = {t.filename.lower() for t in teile}
            if "xl/vbaproject.bin" in namen:
                raise ImportFehler(
                    "Die Datei enthält Makros. Bitte als „Excel-Arbeitsmappe (.xlsx)“ ohne "
                    "Makros speichern."
                )
            if "[content_types].xml" not in namen or "xl/workbook.xml" not in namen:
                raise ImportFehler(kaputt)
            if b"macroenabled" in archiv.read("[Content_Types].xml").lower():
                raise ImportFehler(
                    "Die Datei ist eine Arbeitsmappe mit Makros. Bitte als "
                    "„Excel-Arbeitsmappe (.xlsx)“ speichern."
                )
    except ImportFehler:
        raise
    except (zipfile.BadZipFile, zlib.error, KeyError, ValueError, OSError, EOFError,
            NotImplementedError) as fehler:
        raise ImportFehler(kaputt) from fehler


# ------------------------------------------------------------------- Lesen

@dataclass
class Zeile:
    """Eine gelesene Datenzeile."""

    #: Zeilennummer in der Datei, wie das Tabellenprogramm sie zeigt.
    nummer: int
    #: Feld -> Text; Zahlzellen sind schon in Text umgerechnet.
    felder: dict[str, str]
    #: Feld -> Grund, warum die Zelle nicht lesbar war (Formel, Datum ...).
    zellfehler: dict[str, str] = field(default_factory=dict)
    #: Feld -> Zellbezug ("F7"); bei CSV leer.
    zellen: dict[str, str] = field(default_factory=dict)


@dataclass
class Gelesen:
    """Was aus der Datei kam -- noch ohne Abgleich mit dem Bestand."""

    art: str
    zeilen: list[Zeile]
    #: Feld -> Ueberschrift, wie sie in der Datei steht.
    spalten: dict[str, str]
    #: Ueberschriften ohne Zuordnung; sie werden nicht gelesen.
    unbekannt: list[str]
    hinweise: list[str] = field(default_factory=list)


def lies(name: str, inhalt: bytes, vorlage_format: str) -> Gelesen:
    """Prueft und liest eine Datei. ``vorlage_format`` ist das Format der Kundennummer."""
    art = pruefe_datei(name, inhalt)
    if art == "xlsx":
        return lies_xlsx(inhalt, vorlage_format)
    return lies_csv(inhalt, vorlage_format)


def _kopf(werte: list[object]) -> tuple[dict[int, str], dict[str, str], list[str]]:
    """Ordnet die Ueberschriften zu: (Spaltenindex -> Feld, Feld -> Ueberschrift, unbekannt)."""
    index: dict[int, str] = {}
    gefunden: dict[str, str] = {}
    unbekannt: list[str] = []
    for i, roh in enumerate(werte):
        # Das Sternchen der Pflichtspalten gehoert nicht in die Meldungen.
        text = " ".join(str(roh or "").split()).rstrip(" *")
        if not text:
            continue
        feld = _ZU_FELD.get(_schluessel(text))
        if feld is None:
            unbekannt.append(text[:40])
            continue
        if feld in gefunden:
            raise ImportFehler(
                f"Die Spalten „{gefunden[feld]}“ und „{text[:40]}“ meinen beide "
                f"„{SPALTE[feld].ueberschrift}“. Bitte eine davon löschen oder umbenennen."
            )
        gefunden[feld] = text[:40]
        index[i] = feld
    if "name" not in gefunden:
        raise ImportFehler(
            "In der ersten Zeile fehlt die Überschrift „Name“. Erwartet werden die "
            "Überschriften der Vorlage: " + ", ".join(s.ueberschrift for s in SPALTEN) + "."
        )
    fehlen = [SPALTE[f].ueberschrift for f in stamm.PFLICHTFELDER if f not in gefunden]
    if fehlen:
        raise ImportFehler(
            "Es fehlen Spalten für die Pflichtangaben: " + ", ".join(fehlen) + ". Ohne "
            "vollständige Anschrift lässt sich kein Kunde anlegen (§ 34a Satz 1 Nr. 1 UStDV)."
        )
    return index, gefunden, unbekannt


def _zu_viele() -> ImportFehler:
    return ImportFehler(
        f"Die Datei hat mehr als {MAX_ZEILEN} Kundenzeilen. Bitte in mehrere Dateien teilen."
    )


def _zu_lang() -> ImportFehler:
    tausender = f"{MAX_TABELLENZEILEN:,}".replace(",", ".")
    return ImportFehler(
        f"Die Tabelle reicht über Zeile {tausender} hinaus — mehr als {MAX_ZEILEN} Kunden "
        "je Datei gehen nicht. Steht weit unten noch etwas (ein Wert oder eine Formatierung, "
        "etwa in der letzten Zeile)? Bitte die Zeilen unter den Kunden löschen und die Datei "
        "neu speichern, oder die Kunden auf mehrere Dateien verteilen."
    )


def _text_pruefen(feld: str, text: str) -> str | None:
    """Grund, warum ein Text nicht in die Zelle darf -- oder None."""
    if eingabe.STEUERZEICHEN.search(text):
        return ("enthält ein unsichtbares Steuerzeichen (z. B. aus einer kopierten Tabelle) — "
                "bitte neu tippen")
    grenze = MAX_ZEICHEN_NOTIZ if feld == "notiz" else MAX_ZEICHEN
    if len(text) > grenze:
        return f"ist zu lang ({len(text)} Zeichen, höchstens {grenze})"
    return None


def _cent_text(wert: int | float) -> str:
    """Eine Zahl aus einer Zahlzelle als Eurobetrag im Format des Formulars.

    Ueber ``Decimal(str(...))``, nie ``wert * 100``: 0,29 € waeren sonst 28 Cent.
    Und nie ueber den Textleser: "1.234" liest er als 1.234 Euro.

    >>> _cent_text(0.29), _cent_text(1.234), _cent_text(85), _cent_text(-5.5)
    ('0,29', '1,23', '85,00', '-5,50')
    """
    cent = int((Decimal(str(wert)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    vorzeichen = "-" if cent < 0 else ""
    euro, rest = divmod(abs(cent), 100)
    return f"{vorzeichen}{euro},{rest:02d}"


def _zahl_als_text(feld: str, wert: int | float, vorlage_format: str) -> tuple[str, str | None]:
    """Eine Zahlzelle als Text fuer die Pruefung: (Text, Zellfehler)."""
    try:
        ganz = float(wert).is_integer()
    except (OverflowError, ValueError):
        return "", "ist keine lesbare Zahl"
    art = SPALTE[feld].art
    if art == "geld":
        try:
            return _cent_text(wert), None
        except (InvalidOperation, ValueError, OverflowError):
            return "", "ist keine lesbare Zahl"
    if not ganz:
        # 14,5 Tage oder eine PLZ mit Komma: der Text geht an die Pruefung, die
        # dann sagt, was daran falsch ist.
        return str(wert).replace(".", ","), None
    zahl = int(wert)
    if art == "nummer" and zahl >= 0:
        # Excel macht aus "007" die Zahl 7. Im Format {:03d} heisst sie wieder 007.
        return vorlage_format.format(zahl), None
    if feld == "aktiv":
        return ("1" if zahl else "0"), None
    return str(zahl), None


def _zelle_lesen(feld: str, zelle, vorlage_format: str) -> tuple[str, str | None]:
    """Eine Zelle aus openpyxl: (Text, Zellfehler)."""
    wert = zelle.value
    if wert is None:
        return "", None
    if zelle.data_type == "f":
        return "", "enthält eine Formel — bitte „Werte einfügen“ statt Formeln"
    if isinstance(wert, bool):
        return ("ja" if wert else "nein"), None
    if isinstance(wert, datetime | date | time):
        return "", "enthält ein Datum — bitte als Text eingeben"
    if isinstance(wert, int | float):
        return _zahl_als_text(feld, wert, vorlage_format)
    text = str(wert)
    return text, _text_pruefen(feld, text)


def lies_xlsx(inhalt: bytes, vorlage_format: str) -> Gelesen:
    """Liest das Blatt "Kunden" (sonst das erste sichtbare) einer geprueften xlsx-Datei."""
    try:
        with warnings.catch_warnings():
            # openpyxl warnt bei unbekannten Erweiterungen (bedingte Formate
            # aus neueren Excel-Versionen) -- fuer den Import ohne Belang.
            warnings.simplefilter("ignore")
            mappe = load_workbook(io.BytesIO(inhalt), read_only=True, data_only=False)
    except Exception as fehler:  # noqa: BLE001 -- jede Ursache heisst: nicht lesbar
        raise ImportFehler(
            "Die Excel-Datei ließ sich nicht lesen. Bitte in Excel öffnen und als "
            "„Excel-Arbeitsmappe (.xlsx)“ neu speichern."
        ) from fehler
    try:
        return _blatt_lesen(mappe, vorlage_format)
    except ImportFehler:
        raise
    except Exception as fehler:  # noqa: BLE001
        raise ImportFehler(
            "Die Excel-Datei ließ sich nicht vollständig lesen. Bitte in Excel öffnen und als "
            "„Excel-Arbeitsmappe (.xlsx)“ neu speichern."
        ) from fehler
    finally:
        mappe.close()


def _blatt_lesen(mappe, vorlage_format: str) -> Gelesen:
    if "Kunden" in mappe.sheetnames:
        blatt = mappe["Kunden"]
    else:
        sichtbar = [b for b in mappe.worksheets
                    if getattr(b, "sheet_state", "visible") == "visible"
                    and b.title not in ("Hilfe", "Listen")]
        if not sichtbar:
            raise ImportFehler("Die Excel-Datei enthält kein Blatt mit Kunden.")
        blatt = sichtbar[0]
    index: dict[int, str] | None = None
    gelesen: Gelesen | None = None
    for zaehler, zeilen in enumerate(blatt.iter_rows(max_col=MAX_SPALTEN), start=1):
        if zaehler > MAX_TABELLENZEILEN:
            raise _zu_lang()
        zellen = list(zeilen)
        # Die Zeilennummer, die das Tabellenprogramm zeigt -- fuer die Meldungen.
        nummer = next((z.row for z in zellen if getattr(z, "row", None)), zaehler)
        if index is None:
            if all(z.value in (None, "") for z in zellen):
                continue
            index, spalten, unbekannt = _kopf([z.value for z in zellen])
            gelesen = Gelesen("xlsx", [], spalten, unbekannt)
            continue
        felder: dict[str, str] = {}
        fehler: dict[str, str] = {}
        bezug: dict[str, str] = {}
        for i, zelle in enumerate(zellen):
            feld = index.get(i)
            if feld is None:
                continue
            text, grund = _zelle_lesen(feld, zelle, vorlage_format)
            felder[feld] = text
            bezug[feld] = f"{get_column_letter(i + 1)}{nummer}"
            if grund:
                fehler[feld] = grund
        if not any(t.strip() for t in felder.values()) and not fehler:
            continue
        if len(gelesen.zeilen) >= MAX_ZEILEN:
            raise _zu_viele()
        gelesen.zeilen.append(Zeile(nummer, felder, fehler, bezug))
    if gelesen is None:
        raise ImportFehler("Die Tabelle ist leer: keine Überschriften, keine Kunden.")
    return gelesen


#: Kodierungen einer CSV-Datei, in dieser Reihenfolge versucht.
KODIERUNGEN = {"utf-8-sig": "UTF-8", "cp1252": "Windows-1252"}


def lies_csv(inhalt: bytes, vorlage_format: str) -> Gelesen:
    """Liest eine CSV-Datei: UTF-8 (mit oder ohne BOM) oder Windows-1252, ; , oder Tab."""
    for kodierung in KODIERUNGEN:
        try:
            text = inhalt.decode(kodierung)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ImportFehler(
            "Die CSV-Datei ist weder UTF-8 noch Windows-1252. Bitte in Excel als "
            "„CSV UTF-8 (durch Trennzeichen getrennt)“ speichern."
        )
    erste = next((z for z in text.splitlines() if z.strip()), "")
    # Excel schreibt in Deutschland ; -- bei Gleichstand gewinnt deshalb das Semikolon.
    trenner = max((";", ",", "\t"), key=erste.count)
    leser = csv.reader(io.StringIO(text, newline=""), delimiter=trenner)
    gelesen: Gelesen | None = None
    index: dict[int, str] = {}
    try:
        for werte in leser:
            if leser.line_num > MAX_TABELLENZEILEN:
                raise _zu_lang()
            werte = werte[:MAX_SPALTEN]
            if gelesen is None:
                if not any(w.strip() for w in werte):
                    continue
                index, spalten, unbekannt = _kopf(werte)
                gelesen = Gelesen("csv", [], spalten, unbekannt,
                                  [f"Gelesen als CSV ({KODIERUNGEN[kodierung]}, Trennzeichen „"
                                   f"{'Tab' if trenner == chr(9) else trenner}“)."])
                continue
            felder: dict[str, str] = {}
            fehler: dict[str, str] = {}
            for i, roh in enumerate(werte):
                feld = index.get(i)
                if feld is None:
                    continue
                felder[feld] = roh
                if grund := _text_pruefen(feld, roh):
                    fehler[feld] = grund
            if not any(t.strip() for t in felder.values()) and not fehler:
                continue
            if len(gelesen.zeilen) >= MAX_ZEILEN:
                raise _zu_viele()
            gelesen.zeilen.append(Zeile(leser.line_num, felder, fehler))
    except csv.Error as fehler:
        raise ImportFehler(
            f"Die CSV-Datei ist in Zeile {leser.line_num} nicht lesbar (Anführungszeichen?)."
        ) from fehler
    if gelesen is None:
        raise ImportFehler("Die Datei ist leer: keine Überschriften, keine Kunden.")
    return gelesen


# ------------------------------------------------------------------- Planen

@dataclass
class Planzeile:
    """Was mit einer Zeile geschieht."""

    nummer: int
    status: str
    #: Die gelesenen Texte -- so zeigt die Vorschau eine fehlerhafte Zeile.
    felder: dict[str, str]
    #: Normalisierte Werte (Spalten der Tabelle kunde); leer bei FEHLER.
    werte: dict[str, object] = field(default_factory=dict)
    #: Die Kundennummer aus der Datei (normalisiert) oder die zugeteilte.
    kundennummer: str | None = None
    nummer_angegeben: bool = False
    #: (Spalte, Grund) -- Spalte als Ueberschrift mit Zellbezug.
    fehler: list[tuple[str, str]] = field(default_factory=list)
    hinweise: list[str] = field(default_factory=list)
    #: Bei VORHANDEN, NAMENSGLEICH und UEBERSPRUNGEN: warum.
    grund: str = ""
    zaehler: int | None = None

    @property
    def name(self) -> str:
        return str(self.werte.get("name") or stamm.einzeilig(self.felder.get("name")))


@dataclass
class Plan:
    """Die Vorschau: je Zeile ein Status, dazu Hinweise zur ganzen Datei."""

    zeilen: list[Planzeile]
    hinweise: list[str]
    fingerabdruck: str
    #: Der erste automatisch vergebene Zaehler.
    auto_start: int
    vorlage_format: str

    def anzahl(self, status: str) -> int:
        return sum(1 for z in self.zeilen if z.status == status)

    def anzulegen(self, trotzdem: Collection[int] | None = None) -> list[Planzeile]:
        """Die Zeilen, die angelegt werden: NEU und die bestaetigten NAMENSGLEICH."""
        return [z for z in self.zeilen if z.status == NEU or (
            z.status == NAMENSGLEICH and (trotzdem is None or z.nummer in trotzdem))]

    def vergib_nummern(self, trotzdem: Collection[int] | None = None) -> None:
        """Teilt den anzulegenden Zeilen ohne eigene Nummer die naechsten zu.

        Erst alle NEU-Zeilen, dann die namensgleichen: so aendert ein
        abgewaehlter Haken "trotzdem anlegen" die Nummern der neuen Kunden
        nicht. Ohne ``trotzdem`` (Vorschau) gilt jede namensgleiche Zeile als
        bestaetigt.
        """
        for z in self.zeilen:
            if not z.nummer_angegeben:
                z.kundennummer = None
        zaehler = self.auto_start
        anlegen = self.anzulegen(trotzdem)
        for z in sorted(anlegen, key=lambda z: (z.status != NEU, z.nummer)):
            if z.nummer_angegeben:
                continue
            z.kundennummer = self.vorlage_format.format(zaehler)
            z.zaehler = zaehler
            zaehler += 1


def _namensschluessel(name: object) -> str:
    """Name ohne Gross/klein und doppelten Leerraum -- "MÜLLER" ist "Müller"."""
    return stamm.einzeilig(str(name or "")).casefold()


def _ust_kompakt(nummer: object) -> str:
    return re.sub(r"[\s.\-/]", "", str(nummer or "")).upper()


def _spalte_text(gelesen: Gelesen, zeile: Zeile, feld: str) -> str:
    """Die Spalte fuer die Meldung: Ueberschrift wie in der Datei, dazu der Zellbezug."""
    kopf = gelesen.spalten.get(feld) or SPALTE[feld].ueberschrift
    bezug = zeile.zellen.get(feld)
    return f"{kopf} ({bezug})" if bezug else kopf


def plane(conn: sqlite3.Connection, gelesen: Gelesen) -> Plan:
    """Gleicht die gelesenen Zeilen mit dem Bestand ab. Schreibt nichts.

    Reihenfolge je Zeile: Beispielzeile? Schon vorhanden (gleiche
    Kundennummer, gleiche USt-IdNr.)? Fehlerhaft? Doppelt in der Datei?
    Gleicher Name wie ein Kunde im Bestand oder eine Zeile davor? Sonst neu.
    """
    vorlage_format = stamm.format_aus_betrieb(conn)
    belegt = stamm.belegte_zaehler(conn, vorlage_format)
    # Nummern geloeschter Kunden, die auf einer zurueckgenommenen Rechnung
    # standen: nicht vorhanden, aber auch nicht wieder vergeben.
    verbraucht = stamm.verbrauchte_zaehler(conn, vorlage_format)
    bestand = conn.execute(
        "SELECT id, kundennummer, name, ort, ust_idnr FROM kunde ORDER BY id").fetchall()
    nach_nummer = {(k["kundennummer"] or "").upper(): k for k in bestand}
    nach_ust = {_ust_kompakt(k["ust_idnr"]): k for k in bestand if k["ust_idnr"]}
    nach_name: dict[str, list[sqlite3.Row]] = {}
    for k in bestand:
        nach_name.setdefault(_namensschluessel(k["name"]), []).append(k)

    datei_nummer: dict[int, int] = {}
    datei_ust: dict[str, int] = {}
    datei_name: dict[str, int] = {}
    zeilen: list[Planzeile] = []
    for z in gelesen.zeilen:
        felder = dict(z.felder)
        p = Planzeile(z.nummer, NEU, felder)
        zeilen.append(p)
        if stamm.einzeilig(felder.get("kundennummer")).casefold() == BEISPIEL.casefold():
            p.status = UEBERSPRUNGEN
            p.grund = ("Beispielzeile der Vorlage (Kundennummer „Beispiel“). Zum Anlegen "
                       "„Beispiel“ aus der Zelle löschen.")
            continue

        for feld, grund in z.zellfehler.items():
            p.fehler.append((_spalte_text(gelesen, z, feld), grund))
        # Kundennummer: normalisiert und ueber den Zaehler verglichen.
        roh_nummer = stamm.einzeilig(felder.get("kundennummer"))
        if roh_nummer and "kundennummer" not in z.zellfehler:
            try:
                p.kundennummer, p.zaehler = stamm.kundennummer_lesen(vorlage_format, roh_nummer)
                p.nummer_angegeben = True
            except ValueError as problem:
                p.fehler.append((_spalte_text(gelesen, z, "kundennummer"), str(problem)))

        pruefen = {f: (felder.get(f) or "") for f in stamm.FELDER}
        for f, vorgabe in VORGABEN.items():
            if not pruefen[f].strip():
                pruefen[f] = vorgabe
        aktiv = stamm.einzeilig(pruefen["aktiv"]).casefold()
        if aktiv in _JA:
            pruefen["aktiv"] = "1"
        elif aktiv in _NEIN:
            pruefen["aktiv"] = "0"
        elif "aktiv" not in z.zellfehler:
            p.fehler.append((_spalte_text(gelesen, z, "aktiv"),
                             f"„{pruefen['aktiv'][:20]}“ — bitte „ja“ oder „nein“"))
        werte, feldfehler = stamm.pruefe_kunde(pruefen)
        ust = _ust_kompakt(werte.get("ust_idnr") or pruefen["ust_idnr"])

        # Schon im Bestand? Dann bleibt der Bestand, wie er ist -- auch wenn
        # die Zeile sonst Fehler haette: angelegt wird sie so oder so nicht.
        vorhanden, warum = None, ""
        if p.zaehler is not None and p.zaehler in belegt:
            vorhanden, warum = belegt[p.zaehler], "gleiche Kundennummer"
        elif p.kundennummer and p.kundennummer.upper() in nach_nummer:
            vorhanden, warum = nach_nummer[p.kundennummer.upper()], "gleiche Kundennummer"
        elif ust and ust in nach_ust:
            vorhanden, warum = nach_ust[ust], "gleiche USt-IdNr."
        if vorhanden is not None:
            p.status = VORHANDEN
            p.grund = (f"Schon vorhanden: {vorhanden['kundennummer']} {vorhanden['name']} "
                       f"({warum}). Wird nicht verändert.")
            continue

        for feld, grund in feldfehler.items():
            if feld in z.zellfehler:
                continue
            if feld in stamm.PFLICHTFELDER and not stamm.einzeilig(pruefen[feld]):
                grund = "fehlt (Pflicht nach § 34a Satz 1 Nr. 1 UStDV)"
            p.fehler.append((_spalte_text(gelesen, z, feld), grund))
        if not feldfehler:
            for feld, grund in stamm.querpruefung(werte).items():
                if feld == "leistungsort" and not stamm.einzeilig(felder.get("leistungsort")):
                    grund = "fehlt; leer hieße Inland. " + grund
                p.fehler.append((_spalte_text(gelesen, z, feld), grund))

        if p.zaehler is not None and p.zaehler in verbraucht:
            p.fehler.append((_spalte_text(gelesen, z, "kundennummer"),
                             stamm.verbraucht_text(p.kundennummer, verbraucht[p.zaehler])))
        # Doppelt in der Datei: die spaetere Zeile ist der Fehler.
        if p.zaehler is not None:
            if p.zaehler in datei_nummer:
                p.fehler.append((_spalte_text(gelesen, z, "kundennummer"),
                                 f"dieselbe Nummer steht schon in Zeile {datei_nummer[p.zaehler]}"))
            else:
                datei_nummer[p.zaehler] = z.nummer
        if ust:
            if ust in datei_ust:
                p.fehler.append((_spalte_text(gelesen, z, "ust_idnr"),
                                 f"dieselbe USt-IdNr. steht schon in Zeile {datei_ust[ust]}"))
            else:
                datei_ust[ust] = z.nummer
        if p.fehler:
            p.status = FEHLER
            continue

        p.werte = werte
        schluessel = _namensschluessel(werte["name"])
        if schluessel in nach_name:
            gleiche = ", ".join(f"{k['kundennummer']} ({k['ort']})" for k in nach_name[schluessel])
            p.status, p.grund = NAMENSGLEICH, f"Gleicher Name wie {gleiche}."
        elif schluessel in datei_name:
            p.status, p.grund = NAMENSGLEICH, f"Gleicher Name wie Zeile {datei_name[schluessel]}."
        datei_name.setdefault(schluessel, z.nummer)
        for feld in ("name", "rechnungsname", "strasse", "ort", "notiz"):
            if str(werte.get(feld) or "")[:1] in ("=", "@", "+"):
                p.hinweise.append(f"{SPALTE[feld].ueberschrift} beginnt mit "
                                  f"„{str(werte[feld])[:1]}“ — wird als Text übernommen.")
        if werte.get("ust_idnr") and werte["leistungsort"] != "EU_B2B_REVERSE_CHARGE":
            if stamm.ist_eu_praefix(str(werte["ust_idnr"])[:2]) and (
                    form := stamm.ust_idnr_formfehler(str(werte["ust_idnr"]))):
                p.hinweise.append(f"USt-IdNr. {form}.")
        if not werte["aktiv"]:
            p.hinweise.append("Wird stillgelegt angelegt (Aktiv: nein).")

    hoechste = max((*belegt, *verbraucht), default=0)
    angegeben = [z.zaehler for z in zeilen
                 if z.status in (NEU, NAMENSGLEICH) and z.zaehler is not None]
    auto_start = max(hoechste, *angegeben, 0) + 1
    auto_start = max(auto_start, stamm.kundennummer_start(conn))
    hinweise = list(gelesen.hinweise)
    if gelesen.unbekannt:
        hinweise.append("Nicht gelesen (keine Spalte des Kundenformulars): "
                        + ", ".join(f"„{u}“" for u in gelesen.unbekannt[:12])
                        + (" …" if len(gelesen.unbekannt) > 12 else "") + ".")
    naechste = max(hoechste + 1, stamm.kundennummer_start(conn))
    if angegeben and max(angegeben) > naechste:
        hinweise.append(
            f"Die angegebene Nummer {vorlage_format.format(max(angegeben))} hebt den Zähler: "
            f"neue Kunden bekommen danach {vorlage_format.format(max(angegeben) + 1)} und "
            "folgende — Lücken werden nicht aufgefüllt."
        )
    plan = Plan(zeilen, hinweise, "", auto_start, vorlage_format)
    plan.fingerabdruck = _fingerabdruck(plan)
    plan.vergib_nummern()
    return plan


def _fingerabdruck(plan: Plan) -> str:
    """Was die Vorschau zeigt, als Pruefsumme: Status, Werte, Nummern, Zaehlerstart."""
    inhalt = [plan.vorlage_format, plan.auto_start] + [
        [z.nummer, z.status, z.grund, z.werte, z.kundennummer if z.nummer_angegeben else None,
         z.fehler]
        for z in plan.zeilen
    ]
    return hashlib.sha256(json.dumps(inhalt, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


# ------------------------------------------------------------------ Anlegen

@dataclass
class Ergebnis:
    """Was das Anlegen getan hat."""

    angelegt: list[tuple[int, str, str]]
    uebersprungen: int
    fehlerhaft: int
    #: True: genau diese Vorschau war schon angelegt (zweiter Klick).
    schon_erledigt: bool = False


def frueherer_import(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    """Der juengste Journaleintrag KUNDENIMPORT dieser Datei -- fuer den Hinweis der Vorschau."""
    return conn.execute(
        "SELECT zeitpunkt_utc, wert_neu FROM journal WHERE aktion = 'KUNDENIMPORT'"
        " AND hash = ? ORDER BY id DESC LIMIT 1", (sha256,)
    ).fetchone()


def schon_angelegt(conn: sqlite3.Connection, sha256: str, fingerabdruck: str,
                   trotzdem: Collection[int] = ()) -> bool:
    """Lief GENAU dieser Import schon -- ein zweiter Klick, ein zweiter Tab?

    Derselbe Fingerabdruck allein reicht dafuer nicht. Wer die importierten
    Kunden wieder geloescht hat (ein Probeimport, danach aufgeraeumt), bekommt
    fuer dieselbe Datei wieder dieselbe Vorschau -- und will sie jetzt
    wirklich anlegen. Erledigt ist ein Import deshalb nur, solange noch einer
    der damals angelegten Kunden besteht (Kennung UND Nummer: SQLite vergibt
    die Kennung eines geloeschten Kunden neu). Hat er keinen angelegt, gilt
    er nur bei denselben Haken "trotzdem anlegen" als erledigt.
    """
    for zeile in conn.execute(
        "SELECT wert_neu FROM journal WHERE aktion = 'KUNDENIMPORT' AND hash = ?"
        " ORDER BY id DESC", (sha256,)
    ):
        try:
            angaben = json.loads(zeile["wert_neu"] or "{}")
        except ValueError:
            continue
        if not isinstance(angaben, dict) or angaben.get("vorschau") != fingerabdruck:
            continue
        kunden = [k for k in angaben.get("kunden") or () if isinstance(k, list) and len(k) == 2]
        if kunden:
            if any(conn.execute("SELECT 1 FROM kunde WHERE id = ? AND kundennummer = ?",
                                (kid, nummer)).fetchone() for kid, nummer in kunden):
                return True
            continue
        if sorted(angaben.get("trotzdem") or ()) == sorted(trotzdem):
            return True
    return False


def pruefsumme(inhalt: bytes) -> str:
    return hashlib.sha256(inhalt).hexdigest()


def lege_an(conn: sqlite3.Connection, name: str, inhalt: bytes, *, fingerabdruck: str,
            trotzdem: Iterable[int] = ()) -> Ergebnis:
    """Legt die Kunden der Vorschau an -- in EINER Transaktion, mit Journal je Kunde.

    Die Datei wird unter der Schreibsperre noch einmal gelesen und geplant.
    Weicht der Fingerabdruck von dem der Vorschau ab (ein Kunde kam
    inzwischen dazu, das Nummernformat wurde umgestellt), wird nichts
    angelegt: :class:`VorschauVeraltet` traegt den neuen Plan. Ein zweiter
    Klick auf dieselbe Vorschau findet den Journaleintrag des ersten und legt
    nichts doppelt an (:func:`schon_angelegt`).
    """
    sha256 = pruefsumme(inhalt)
    bestaetigt = set(trotzdem)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if schon_angelegt(conn, sha256, fingerabdruck, bestaetigt):
            conn.execute("COMMIT")
            return Ergebnis([], 0, 0, schon_erledigt=True)
        plan = plane(conn, lies(name, inhalt, stamm.format_aus_betrieb(conn)))
        if plan.fingerabdruck != fingerabdruck:
            conn.execute("ROLLBACK")
            raise VorschauVeraltet(plan)
        plan.vergib_nummern(bestaetigt)
        anlegen = plan.anzulegen(bestaetigt)
        angelegt: list[tuple[int, str, str]] = []
        heute = db.heute()
        for z in anlegen:
            spalten = ["kundennummer", *z.werte, "angelegt_am"]
            cur = conn.execute(
                f"INSERT INTO kunde ({', '.join(spalten)})"
                f" VALUES ({', '.join('?' * len(spalten))})",
                (z.kundennummer, *z.werte.values(), heute),
            )
            db.protokolliere(conn, "ANLEGEN", "kunde", cur.lastrowid,
                             wert_neu={"kundennummer": z.kundennummer, "name": z.werte["name"]},
                             begruendung=f"Kundenimport {name}, Zeile {z.nummer}")
            angelegt.append((cur.lastrowid, str(z.kundennummer), str(z.werte["name"])))
        fehlerhaft = plan.anzahl(FEHLER)
        uebersprungen = len(plan.zeilen) - len(anlegen) - fehlerhaft
        db.protokolliere(conn, "KUNDENIMPORT", "kunde", None,
                         wert_neu={"datei": name, "angelegt": len(angelegt),
                                   "uebersprungen": uebersprungen, "fehlerhaft": fehlerhaft,
                                   "vorschau": fingerabdruck,
                                   # Woran schon_angelegt() einen zweiten Klick erkennt.
                                   "kunden": [[kid, nummer] for kid, nummer, _ in angelegt],
                                   "trotzdem": sorted(bestaetigt)},
                         hash_=sha256)
        conn.execute("COMMIT")
    except VorschauVeraltet:
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return Ergebnis(angelegt, uebersprungen, fehlerhaft)

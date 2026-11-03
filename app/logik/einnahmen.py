"""Einnahmen ohne eigene Rechnung.

Affiliate-Auszahlungen, Werbeerloese einer Plattform, eine Auszahlung von
Udemy, eine Sachzuwendung fuer einen Testbericht, der Verkauf eines eigenen
Geraets: Betriebseinnahmen, zu denen dieses Werkzeug keine Rechnung schreibt
-- die Plattform rechnet selbst ab. Bis Version 25 kannte das Werkzeug
Einnahmen nur ueber Zahlungen auf eigene Rechnungen; EUeR, § 19-Umsatz und
Steuerruecklage fielen um genau diese Betraege zu niedrig aus.

Als Rechnung eintragen ginge nicht: der Rechnungsnummernkreis ist
lueckenlos, und eine Schein-Rechnung waere ein Beleg, den es nicht gibt.
Deshalb eine eigene Tabelle (``einnahme``, Migration 26).

Was eine Einnahme ist, sagt ihre Kategorie aus dem Einnahmenbereich der
Anlage EUeR -- und daraus folgt, ob sie in den § 19-Gesamtumsatz zaehlt:

* **Inland** zaehlt (§ 19 Abs. 2 UStG: im Inland ausgefuehrte steuerbare
  Umsaetze).
* **Ausland** zaehlt nicht: eine sonstige Leistung an einen auslaendischen
  Unternehmer wird nach § 3a Abs. 2 UStG im Ausland ausgefuehrt und ist im
  Inland nicht steuerbar -- Amazon, Google/YouTube, Udemy, Apple rechnen
  aus dem Ausland ab.
* **Verkauf eines Anlageguts** zaehlt nicht (§ 19 Abs. 2 UStG: Umsaetze von
  Wirtschaftsguetern des Anlagevermoegens bleiben ausser Ansatz).
* **Erstattung** ist kein Umsatz.

In der EUeR sind alle vier Betriebseinnahmen.

Je Zahler merkt sich das Werkzeug die Einstufung (Tabelle ``zahler``): wer
monatlich die Amazon-Auszahlung eintraegt, waehlt "Ausland" einmal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app import db, pfade
from app.logik import eingabe, kategorien
from app.logik.beleg import pipeline
from app.logik.beleg.eigenbeleg import ohne_steuerzeichen
from app.logik.rundung import euro

#: Woher eine Einnahme kommt.
MANUELL = "MANUELL"
WISO = "WISO"

#: Unterordner im Datenordner fuer die Abrechnungen zu Einnahmen.
ARCHIV = "einnahmen"

#: Die Einstufungen eines Zahlers -- je eine Kategorie des Einnahmenbereichs.
INLAND = "INLAND"
AUSLAND = "AUSLAND"
ANLAGENVERKAUF = "ANLAGENVERKAUF"
ERSTATTUNG = "ERSTATTUNG"
EINSTUFUNGEN = {
    INLAND: "EINNAHME_HONORAR_INLAND",
    AUSLAND: "EINNAHME_HONORAR_AUSLAND",
    ANLAGENVERKAUF: "EINNAHME_ANLAGENVERKAUF",
    ERSTATTUNG: "EINNAHME_ERSTATTUNG",
}
EINSTUFUNG_TEXTE = {
    INLAND: "Inland (zählt zur Kleinunternehmergrenze)",
    AUSLAND: "Ausland (nicht steuerbar, zählt nicht)",
    ANLAGENVERKAUF: "Verkauf eines Anlageguts (zählt nicht)",
    ERSTATTUNG: "Erstattung (kein Umsatz)",
}

#: Kategorien, deren Betrag in den § 19-Gesamtumsatz zaehlt. Weiterbelastete
#: Auslagen sind Teil des Entgelts und folgen ihm (hier: Inland).
ZAEHLT_ZU_19 = ("EINNAHME_HONORAR_INLAND", "EINNAHME_AUSLAGEN")

#: Der Hilfesatz zur Einstufung -- in Vorschau und Formular derselbe.
RECHTSGRUND = (
    "§ 19 Abs. 2 UStG zählt nur im Inland ausgeführte steuerbare Umsätze. Sonstige "
    "Leistungen an ausländische Unternehmer (Amazon, Google/YouTube, Udemy, Apple …) "
    "werden nach § 3a Abs. 2 UStG im Ausland ausgeführt und zählen nicht; Umsätze von "
    "Wirtschaftsgütern des Anlagevermögens auch nicht. In der EÜR sind alle "
    "Betriebseinnahmen."
)

#: Bekannte auslaendische Plattformen: (Muster im Text, Name des Zahlers).
#: Vorbelegung "Ausland" -- sie rechnen aus Luxemburg, Irland oder den USA ab.
#: Unbekanntes ist vorsichtig "Inland": zaehlt es zu Unrecht, faellt das an
#: der Grenze auf; zaehlte ein Inlandsumsatz nicht, fiele es nie auf.
PLATTFORMEN: tuple[tuple[str, str], ...] = (
    (r"\bamazon\b", "Amazon"),
    (r"\byou ?tube\b|\bgoogle\b|\badsense\b", "YouTube / Google"),
    (r"\budemy\b", "Udemy"),
    (r"\bwhoop\b", "WHOOP"),
    (r"\bapp ?store\b|\bapple\b|\bitunes\b", "Apple App Store"),
    (r"\bmeta\b|\bfacebook\b|\binstagram\b", "Meta"),
    (r"\btik ?tok\b", "TikTok"),
    (r"\bspotify\b", "Spotify"),
    (r"\btwitch\b", "Twitch"),
    (r"\bpatreon\b", "Patreon"),
)

#: "Verkauf" als eigenes Wort am Anfang oder Ende des Texts: "Rad Beispiel -
#: Verkauf", "Google Pixel Verkauf", "Verkauf Kamera". NICHT als Wortteil
#: ("Verkaufsprovision") und nicht mitten im Text ("Provision Verkauf
#: Beispielshop"): beides sind Provisionen, also Umsatz -- als Anlagenverkauf
#: fielen sie aus dem § 19-Zaehler, die gefaehrliche Richtung.
_VERKAUF = re.compile(r"^verkauf\b|\bverkauf$")

#: Woerter, die keinen Zahler unterscheiden: "Amazon Affiliate" und "Amazon
#: Auszahlung" sind derselbe. "Affiate" ist ein haeufiger Tippfehler.
_FUELLWOERTER = re.compile(
    r"\b(affiliate|affiate|affilate|partnerprogramm|payout|auszahlung|ads?|werbeerl[oö]se?|"
    r"provision|gutschrift|zusendung|verkauf)\b", re.IGNORECASE)

#: Groesster Betrag, den eine Eingabe von Hand plausibel traegt (1 Mio. €).
BETRAG_HOECHSTENS = 100_000_000
#: Laengste Notiz und laengster Loeschgrund -- dieselben Grenzen wie im
#: Formular (maxlength); der Server prueft sie, nicht nur der Browser. Ein
#: Grund steht zweimal unloeschbar im Journal.
NOTIZ_HOECHSTENS = 1000
GRUND_HOECHSTENS = 200
#: So weit duerfen zwei Zufluesse desselben Betrags auseinander liegen, um
#: als vermutlich dieselbe Zahlung zu gelten (wie beim WISO-Abgleich).
TAGE_AEHNLICH = 7


class EinnahmeFehler(ValueError):
    """Eine Einnahme, die so nicht erfasst werden kann -- mit deutscher Meldung."""


class AehnlicheEinnahme(EinnahmeFehler):
    """Dieselbe Einnahme steht vermutlich schon im Werkzeug (etwa aus WISO).

    Erfasst wird erst nach ausdruecklicher Bestaetigung -- sonst zaehlte sie
    in EUeR und § 19-Umsatz doppelt, ohne jeden Hinweis.
    """

    def __init__(self, aehnliche: list[sqlite3.Row]) -> None:
        super().__init__("Vermutlich schon erfasst: " + "; ".join(
            beschreibung(z) for z in aehnliche) + ".")
        self.aehnliche = aehnliche


def beschreibung(zeile: sqlite3.Row) -> str:
    """Eine Einnahme in einer Zeile: "Einnahme 3 (aus WISO, Nr. 12): Beispiel, 250,00 € am …"."""
    herkunft = (f" (aus WISO, Nr. {zeile['wiso_nummer']})" if zeile["wiso_nummer"]
                else " (aus WISO)" if zeile["herkunft"] == WISO else "")
    tag = zeile["zufluss_am"]
    return (f"Einnahme {zeile['id']}{herkunft}: {zeile['zahler']}, {euro(zeile['betrag_cent'])} € "
            f"am {tag[8:10]}.{tag[5:7]}.{tag[:4]}")


def aehnliche(conn: sqlite3.Connection, *, betrag_cent: int, zufluss_am: str,
              zahler: str) -> list[sqlite3.Row]:
    """Gueltige Einnahmen ohne Rechnung, die dieselbe Zahlung sein koennen.

    Gleicher Betrag, Zufluss hoechstens sieben Tage entfernt und ein
    aehnlicher Zahler -- oder hoechstens zwei Tage entfernt. Fuer die
    Erfassung von Hand und fuer Zahlungen auf eigene Rechnungen: nach einem
    WISO-Import zaehlte dieselbe Einnahme sonst doppelt.
    """
    from app.logik.wiso import namen_aehnlich

    kennung = zahler_kennung(zahler)
    gefunden = []
    for zeile in conn.execute(
        """
        SELECT e.*, (SELECT w.buchungsnummer FROM wiso_buchung w WHERE w.einnahme_id = e.id
                        AND w.status = 'AKTIV') AS wiso_nummer,
               ABS(julianday(e.zufluss_am) - julianday(?)) AS abstand
          FROM v_einnahme_gueltig e
         WHERE e.betrag_cent = ? AND ABS(julianday(e.zufluss_am) - julianday(?)) <= ?
         ORDER BY abstand, e.id
        """, (zufluss_am, betrag_cent, zufluss_am, TAGE_AEHNLICH)):
        if (zeile["abstand"] <= 2 or namen_aehnlich(zahler, zeile["zahler"])
                or (kennung and zahler_kennung(zeile["zahler"]) == kennung)):
            gefunden.append(zeile)
    return gefunden


# ---------------------------------------------------------------- Zahler

def zahler_schluessel(name: str) -> str:
    """Der Schluessel, an dem zwei Schreibweisen derselbe Zahler sind.

    >>> zahler_schluessel("YouTube Ads"), zahler_schluessel("Youtube  Ads")
    ('youtubeads', 'youtubeads')
    >>> zahler_schluessel("Beispiel Shop") == zahler_schluessel("BEISPIELSHOP")
    True
    """
    zerlegt = unicodedata.normalize("NFKD", (name or "").replace("ß", "ss"))
    ohne = "".join(z for z in zerlegt if not unicodedata.combining(z)).casefold()
    return re.sub(r"[^0-9a-z]", "", ohne)[:80]


@dataclass(frozen=True, slots=True)
class Zahler:
    """Der Zahler einer Einnahme, aus einem Buchungstext abgeleitet."""

    name: str
    schluessel: str
    #: Vorbelegte Einstufung (INLAND, AUSLAND, ANLAGENVERKAUF, ERSTATTUNG).
    vorschlag: str
    #: Warum diese Vorbelegung -- fuer die Vorschau.
    grund: str


def zahler_aus_text(text: str) -> Zahler:
    """Zahler und vorbelegte Einstufung aus einem Buchungstext.

    Bekannte Plattformen werden zusammengefasst ("YouTube Ads" und "Youtube
    Ads", "Amazon Affiate"); ein Text, der mit "Verkauf" beginnt oder endet,
    ist der Verkauf eines eigenen Geraets, eine "Zusendung" eine
    Sachzuwendung des Herstellers (Zahler ist dann das erste Wort, die
    Marke). Der Verkauf wird VOR den Plattformen geprueft: "Amazon Echo -
    Verkauf" ist ein eigenes Geraet, nicht die Amazon-Auszahlung -- und
    bekommt einen eigenen Zahler, damit eine Einstufung des einen nie fuer
    das andere gilt.

    >>> zahler_aus_text("Amazon Affiate").name, zahler_aus_text("Amazon Affiate").vorschlag
    ('Amazon', 'AUSLAND')
    >>> zahler_aus_text("Youtube Ads").schluessel == zahler_aus_text("YouTube Ads").schluessel
    True
    >>> z = zahler_aus_text("Kamera XY - Verkauf"); (z.name, z.vorschlag)
    ('Kamera XY', 'ANLAGENVERKAUF')
    >>> z = zahler_aus_text("Beispielring 3 - Zusendung"); (z.name, z.vorschlag)
    ('Beispielring', 'INLAND')
    >>> zahler_aus_text("Beispiel GmbH").vorschlag
    'INLAND'
    >>> z = zahler_aus_text("Amazon Echo - Verkauf"); (z.name, z.vorschlag, z.schluessel)
    ('Amazon Echo', 'ANLAGENVERKAUF', 'amazonecho')
    >>> zahler_aus_text("Verkaufsprovision Beispiel GmbH").vorschlag
    'INLAND'
    """
    roh = " ".join((text or "").split())
    klein = roh.casefold()
    if _VERKAUF.search(klein.strip(" -–—.:;,")) and "provision" not in klein:
        name = _bereinigt(roh) or roh
        return Zahler(name, zahler_schluessel(name), ANLAGENVERKAUF,
                      "„Verkauf“ im Text: Verkauf eines eigenen Geräts")
    for muster, name in PLATTFORMEN:
        if re.search(muster, klein):
            return Zahler(name, zahler_schluessel(name), AUSLAND,
                          "bekannte ausländische Plattform")
    if "zusendung" in klein:
        bereinigt = _bereinigt(roh) or roh
        name = bereinigt.split()[0] if bereinigt.split() else bereinigt
        return Zahler(name, zahler_schluessel(name), INLAND,
                      "Sachzuwendung — Herkunft unbekannt, vorsichtig Inland")
    name = _bereinigt(roh) or roh
    return Zahler(name, zahler_schluessel(name), INLAND, "unbekannt — vorsichtig Inland")


def _bereinigt(text: str) -> str:
    """Ein Buchungstext ohne Fuellwoerter und Trennstriche am Rand.

    >>> _bereinigt("PAYOUT BEISPIELSHOP"), _bereinigt("Rad Beispiel - Händler - Verkauf")
    ('BEISPIELSHOP', 'Rad Beispiel - Händler')
    """
    ohne = _FUELLWOERTER.sub(" ", text)
    ohne = " ".join(ohne.split())
    return ohne.strip(" -–—:;,.").strip()


def kategorie_der_einstufung(conn: sqlite3.Connection, einstufung: str) -> sqlite3.Row:
    """Die Kategorie zu INLAND, AUSLAND, ANLAGENVERKAUF oder ERSTATTUNG."""
    zeile = kategorien.nach_schluessel(conn, EINSTUFUNGEN[einstufung])
    if zeile is None:
        raise EinnahmeFehler(f"Die Kategorie {EINSTUFUNGEN[einstufung]} fehlt im Katalog.")
    return zeile


def einstufung_der_kategorie(conn: sqlite3.Connection, kategorie_id: int | None) -> str | None:
    """Umgekehrt: welche Einstufung eine Kategorie ist (oder ``None``)."""
    if kategorie_id is None:
        return None
    zeile = conn.execute("SELECT schluessel FROM euer_kategorie WHERE id = ?",
                         (kategorie_id,)).fetchone()
    if zeile is None:
        return None
    return next((e for e, s in EINSTUFUNGEN.items() if s == zeile["schluessel"]), None)


def gemerkt(conn: sqlite3.Connection, schluessel: str) -> sqlite3.Row | None:
    """Die gemerkte Einstufung eines Zahlers (Zeile aus ``zahler``) oder ``None``."""
    return conn.execute("SELECT * FROM zahler WHERE schluessel = ?", (schluessel,)).fetchone()


def zahler_kennung(text: str) -> str:
    """Der Schluessel, unter dem ein Zahler gemerkt und nachgeschlagen wird.

    Ueberall derselbe -- im WISO-Import wie bei der Erfassung von Hand: der
    des bereinigten Zahlers aus :func:`zahler_aus_text`. Vorher merkte die
    Hand-Erfassung unter dem Rohtext ("amazonauszahlung"), der Import unter
    dem Zahler ("amazon"), und keiner fand die Einstufung des anderen.

    >>> zahler_kennung("Amazon Auszahlung"), zahler_kennung("Beispiel Partnerprogramm")
    ('amazon', 'beispiel')
    """
    return zahler_aus_text(text).schluessel


def merken(conn: sqlite3.Connection, name: str, kategorie_id: int, *,
           schluessel: str | None = None) -> bool:
    """Merkt die Kategorie eines Zahlers. Nur in einer offenen Transaktion.

    ``name`` ist der Text, wie er erfasst wurde; der Schluessel kommt aus
    :func:`zahler_kennung` -- oder ausdruecklich (der Import kennt ihn schon,
    und ein Geraeteverkauf "Amazon Echo" ist ein anderer Zahler als Amazon).
    Gibt zurueck, ob sich etwas geaendert hat -- nur dann steht es im Journal.
    """
    schluessel = schluessel or zahler_kennung(name)
    if not schluessel:
        return False
    alt = gemerkt(conn, schluessel)
    if alt is not None and alt["kategorie_id"] == kategorie_id:
        return False
    conn.execute(
        "INSERT INTO zahler (schluessel, name, kategorie_id, geaendert_am) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (schluessel) DO UPDATE SET kategorie_id = excluded.kategorie_id,"
        " geaendert_am = excluded.geaendert_am",
        (schluessel, name.strip()[:120], kategorie_id, db.jetzt_utc()))
    db.protokolliere(conn, "ZAHLER_EINSTUFUNG", "zahler", None, feld=schluessel,
                     wert_alt=alt["kategorie_id"] if alt is not None else None,
                     wert_neu={"name": name.strip()[:120], "kategorie_id": kategorie_id})
    return True


def zahler_liste(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Die gemerkten Zahler mit ihrer Kategorie, alphabetisch."""
    return conn.execute(
        "SELECT z.*, k.schluessel AS kategorie_schluessel, k.bezeichnung AS kategorie"
        "  FROM zahler z JOIN euer_kategorie k ON k.id = z.kategorie_id"
        " ORDER BY z.name COLLATE DEUTSCH").fetchall()


# ---------------------------------------------------------------- Erfassen

def _kategorie_pruefen(conn: sqlite3.Connection, kategorie_id: int | None) -> sqlite3.Row:
    if kategorie_id is None or not 1 <= kategorie_id <= eingabe.ID_HOECHSTENS:
        raise EinnahmeFehler("Bitte eine Kategorie wählen (Inland, Ausland …).")
    zeile = conn.execute("SELECT * FROM euer_kategorie WHERE id = ?", (kategorie_id,)).fetchone()
    if zeile is None or zeile["bereich"] != kategorien.BEREICH_EINNAHMEN or not zeile["aktiv"]:
        raise EinnahmeFehler("Die gewählte Kategorie ist keine aktive Einnahmenkategorie.")
    return zeile


def _zahler_pruefen(zahler: str) -> str:
    text = " ".join(ohne_steuerzeichen(zahler).split())
    if not text:
        raise EinnahmeFehler("Bitte den Zahler angeben (wer hat gezahlt?).")
    if len(text) > 120:
        raise EinnahmeFehler("Der Zahler ist zu lang (höchstens 120 Zeichen).")
    return text


def pruefe(conn: sqlite3.Connection, *, zufluss_am: str, betrag_cent: int | None, zahler: str,
           kategorie_id: int | None, heute: date | None = None
           ) -> tuple[str, int, str, sqlite3.Row]:
    """Prueft die Angaben einer Einnahme; gibt (Datum, Betrag, Zahler, Kategorie) zurueck."""
    try:
        tag = eingabe.datum(zufluss_am, feld="Zugeflossen am", heute=heute)
    except ValueError as fehler:
        raise EinnahmeFehler(str(fehler)) from None
    if betrag_cent is None or betrag_cent <= 0:
        raise EinnahmeFehler("Bitte einen Betrag über 0,00 € angeben.")
    if betrag_cent > BETRAG_HOECHSTENS:
        raise EinnahmeFehler(f"{euro(betrag_cent)} € sind als Einnahme unplausibel groß.")
    return tag, betrag_cent, _zahler_pruefen(zahler), _kategorie_pruefen(conn, kategorie_id)


def einfuegen(conn: sqlite3.Connection, *, zufluss_am: str, betrag_cent: int, zahler: str,
              kategorie_id: int, notiz: str | None, herkunft: str = MANUELL,
              journal: dict[str, object] | None = None) -> int:
    """Schreibt eine GEPRUEFTE Einnahme. Nur in einer offenen Transaktion."""
    einnahme_id = conn.execute(
        "INSERT INTO einnahme (zufluss_am, betrag_cent, zahler, kategorie_id, notiz, herkunft,"
        " erfasst_am) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (zufluss_am, betrag_cent, zahler, kategorie_id,
         (ohne_steuerzeichen(notiz) or None) if notiz else None, herkunft, db.jetzt_utc()),
    ).lastrowid
    db.protokolliere(conn, "EINNAHME_ERFASSEN", "einnahme", einnahme_id,
                     wert_neu={"zufluss_am": zufluss_am, "betrag_cent": betrag_cent,
                               "zahler": zahler, "kategorie_id": kategorie_id,
                               "herkunft": herkunft, **(journal or {})})
    return int(einnahme_id)


@dataclass(frozen=True, slots=True)
class Datei:
    """Eine geprüfte, noch nicht abgelegte Datei zu einer Einnahme."""

    inhalt: bytes
    name: str
    sha256: str

    @property
    def endung(self) -> str:
        return Path(self.name).suffix.lower()


def datei_pruefen(inhalt: bytes, name: str) -> Datei:
    """Prueft eine hochgeladene Abrechnung wie einen Beleg: Format, Groesse, Inhalt."""
    name = Path((name or "").strip()).name or "abrechnung.pdf"
    endung = Path(name).suffix.lower()
    if endung not in pipeline.ERLAUBT:
        raise EinnahmeFehler(f"{endung or 'Diese Datei'} ist kein Belegformat. Möglich sind "
                             f"{', '.join(sorted(pipeline.ERLAUBT))}.")
    if not inhalt:
        raise EinnahmeFehler("Die Datei ist leer.")
    if len(inhalt) > pipeline.MAX_BYTES:
        raise EinnahmeFehler(pipeline.zu_gross(len(inhalt)))
    if not pipeline.inhalt_passt_zur_endung(inhalt, endung):
        raise EinnahmeFehler(f"Der Inhalt von „{name}“ passt nicht zur Endung {endung}.")
    return Datei(inhalt, name, hashlib.sha256(inhalt).hexdigest())


def _archivpfad(datenordner: Path, zufluss_am: str, datei: Datei) -> Path:
    return datenordner / ARCHIV / zufluss_am[:4] / f"{zufluss_am}_{datei.sha256[:12]}{datei.endung}"


def _datei_ablegen(conn: sqlite3.Connection, datenordner: Path, einnahme_id: int,
                   zufluss_am: str, datei: Datei,
                   journal: dict[str, object] | None = None) -> tuple[Path, bool]:
    """Legt die Datei schreibgeschuetzt ab und verzeichnet sie. In der Transaktion.

    Gibt den Pfad zurueck und ob DIESER Aufruf die Datei angelegt hat (nur
    dann raeumt ein Fehlschlag sie wieder weg). ``journal`` kommt zum
    Eintrag EINNAHME_DATEI dazu (etwa "aus dem Eingang zugeordnet" und die
    Abweichungen der Datei, app/logik/beleg/anhaengen.py).
    """
    schon = conn.execute(
        "SELECT d.einnahme_id FROM einnahmedatei d JOIN v_einnahme_gueltig e"
        " ON e.id = d.einnahme_id WHERE d.sha256 = ? LIMIT 1", (datei.sha256,)).fetchone()
    if schon is not None:
        raise EinnahmeFehler(f"Diese Datei gehört schon zu Einnahme {schon['einnahme_id']}.")
    ziel = _archivpfad(datenordner, zufluss_am, datei)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    neu = False
    if not ziel.exists():
        zwischen = ziel.with_name(f".{ziel.name}.teil")
        zwischen.write_bytes(datei.inhalt)
        os.replace(zwischen, ziel)
        neu = True
    os.chmod(ziel, pipeline.NUR_LESEN)
    if pipeline.pruefsumme(ziel) != datei.sha256:
        raise EinnahmeFehler("Die Datei hat sich beim Ablegen verändert — bitte noch einmal.")
    conn.execute(
        "INSERT INTO einnahmedatei (einnahme_id, pfad, original_name, sha256, groesse, mime,"
        " importiert_am) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (einnahme_id, pfade.relativ(datenordner, ziel), datei.name, datei.sha256,
         len(datei.inhalt), pipeline._mime(datei.endung), db.jetzt_utc()))
    db.protokolliere(conn, "EINNAHME_DATEI", "einnahme", einnahme_id,
                     wert_neu={"datei": datei.name, **(journal or {})}, hash_=datei.sha256)
    return ziel, neu


def _weg(pfad: Path) -> None:
    try:
        os.chmod(pfad, 0o644)
        pfad.unlink(missing_ok=True)
    except OSError:
        pass


def notiz_pruefen(notiz: str | None) -> str | None:
    """Die Notiz ohne Steuerzeichen -- und hoechstens NOTIZ_HOECHSTENS Zeichen."""
    text = ohne_steuerzeichen(notiz or "").strip()
    if len(text) > NOTIZ_HOECHSTENS:
        raise EinnahmeFehler(f"Die Notiz ist zu lang ({len(text)} Zeichen, höchstens "
                             f"{NOTIZ_HOECHSTENS}).")
    return text or None


def erfassen(conn: sqlite3.Connection, *, zufluss_am: str, betrag_cent: int | None,
             zahler: str, kategorie_id: int | None, notiz: str | None = None,
             merken_fuer_zahler: bool = False, datei: Datei | None = None,
             datenordner: Path | None = None, heute: date | None = None,
             trotz_aehnlicher: bool = False) -> int:
    """Erfasst eine Einnahme von Hand. Oeffnet die Transaktion selbst.

    Mit ``datei`` (Abrechnung der Plattform) wird sie im selben Zug
    schreibgeschuetzt unter ``einnahmen/<Jahr>/`` abgelegt. Steht dieselbe
    Einnahme vermutlich schon da (:func:`aehnliche`, etwa aus WISO), wird
    nichts geschrieben und :class:`AehnlicheEinnahme` geworfen -- ausser mit
    ``trotz_aehnlicher``.
    """
    tag, betrag, name, kategorie = pruefe(conn, zufluss_am=zufluss_am, betrag_cent=betrag_cent,
                                          zahler=zahler, kategorie_id=kategorie_id, heute=heute)
    notiz = notiz_pruefen(notiz)
    if datei is not None and datenordner is None:
        raise EinnahmeFehler("Ohne Datenordner lässt sich keine Datei ablegen.")
    angelegt: Path | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not trotz_aehnlicher:
            schon = aehnliche(conn, betrag_cent=betrag, zufluss_am=tag, zahler=name)
            if schon:
                raise AehnlicheEinnahme(schon)
        einnahme_id = einfuegen(conn, zufluss_am=tag, betrag_cent=betrag, zahler=name,
                                kategorie_id=kategorie["id"], notiz=notiz)
        if merken_fuer_zahler:
            merken(conn, name, kategorie["id"])
        if datei is not None:
            pfad, neu = _datei_ablegen(conn, datenordner, einnahme_id, tag, datei)
            angelegt = pfad if neu else None
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        if angelegt is not None:
            _weg(angelegt)
        raise
    return einnahme_id


def datei_anhaengen(conn: sqlite3.Connection, datenordner: Path, einnahme_id: int,
                    datei: Datei, *, journal: dict[str, object] | None = None) -> None:
    """Haengt die Abrechnung an eine erfasste Einnahme. Betrag und Datum bleiben.
    ``journal``: Zusatz zum Journaleintrag (siehe :func:`_datei_ablegen`)."""
    angelegt: Path | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        zeile = _gueltig(conn, einnahme_id)
        pfad, neu = _datei_ablegen(conn, datenordner, einnahme_id, zeile["zufluss_am"], datei,
                                   journal)
        angelegt = pfad if neu else None
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        if angelegt is not None:
            _weg(angelegt)
        raise


def _kennung_der_einnahme(conn: sqlite3.Connection, zeile: sqlite3.Row) -> str:
    """Der Zahler-Schluessel einer erfassten Einnahme.

    Aus WISO steht in ``zahler`` schon der bereinigte Name ("Amazon Echo"
    eines Geraeteverkaufs); der Schluessel kommt dann aus dem Buchungstext
    wie beim Import, sonst aus dem erfassten Text.
    """
    wiso = conn.execute("SELECT inhalt_json FROM wiso_buchung WHERE einnahme_id = ?"
                        " AND status = 'AKTIV'", (zeile["id"],)).fetchone()
    if wiso is not None:
        try:
            text = json.loads(wiso["inhalt_json"]).get("text")
        except (ValueError, AttributeError):
            text = None
        if text:
            return zahler_kennung(text)
    return zahler_kennung(zeile["zahler"])


def _gueltig(conn: sqlite3.Connection, einnahme_id: int) -> sqlite3.Row:
    if not 1 <= einnahme_id <= eingabe.ID_HOECHSTENS:
        raise EinnahmeFehler("Diese Einnahme gibt es nicht.")
    zeile = conn.execute("SELECT * FROM einnahme WHERE id = ?", (einnahme_id,)).fetchone()
    if zeile is None:
        raise EinnahmeFehler("Diese Einnahme gibt es nicht.")
    if zeile["geloescht_am"] is not None:
        raise EinnahmeFehler(f"Einnahme {einnahme_id} ist gelöscht — erst wiederherstellen.")
    return zeile


def kategorie_setzen(conn: sqlite3.Connection, einnahme_id: int, kategorie_id: int | None, *,
                     merken_fuer_zahler: bool = False) -> None:
    """Ordnet eine Einnahme neu zu (etwa Inland statt Ausland) -- mit Journal."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        zeile = _gueltig(conn, einnahme_id)
        kategorie = _kategorie_pruefen(conn, kategorie_id)
        if zeile["kategorie_id"] != kategorie["id"]:
            conn.execute("UPDATE einnahme SET kategorie_id = ? WHERE id = ?",
                         (kategorie["id"], einnahme_id))
            db.protokolliere(conn, "EINNAHME_KATEGORIE", "einnahme", einnahme_id,
                             feld="kategorie_id", wert_alt=zeile["kategorie_id"],
                             wert_neu=kategorie["id"])
        if merken_fuer_zahler:
            merken(conn, zeile["zahler"], kategorie["id"],
                   schluessel=_kennung_der_einnahme(conn, zeile))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def markieren(conn: sqlite3.Connection, einnahme_id: int, grund: str) -> bool:
    """Setzt die Loeschmarkierung -- nur in einer offenen Transaktion.

    Das Journal traegt das vollstaendige Abbild. Gibt ``False`` zurueck, wenn
    die Einnahme schon geloescht war.
    """
    zeile = conn.execute("SELECT * FROM einnahme WHERE id = ?", (einnahme_id,)).fetchone()
    if zeile is None:
        raise EinnahmeFehler("Diese Einnahme gibt es nicht.")
    if zeile["geloescht_am"] is not None:
        return False
    dateien = [dict(z) for z in conn.execute(
        "SELECT id, pfad, original_name, sha256 FROM einnahmedatei WHERE einnahme_id = ?",
        (einnahme_id,))]
    zeitpunkt = db.jetzt_utc()
    conn.execute("UPDATE einnahme SET geloescht_am = ?, geloescht_grund = ? WHERE id = ?",
                 (zeitpunkt, grund, einnahme_id))
    db.protokolliere(conn, "EINNAHME_GELOESCHT", "einnahme", einnahme_id,
                     wert_alt={"einnahme": dict(zeile), "dateien": dateien},
                     wert_neu={"geloescht_am": zeitpunkt, "grund": grund}, begruendung=grund)
    return True


def loeschen(conn: sqlite3.Connection, einnahme_id: int, *, grund: str) -> bool:
    """Loescht eine Einnahme als Markierung (mit Grund). Oeffnet die Transaktion.

    ``False``: sie war schon geloescht (Doppelklick) -- nichts geschrieben.
    """
    grund = ohne_steuerzeichen(grund).strip()
    if len(grund) < 3:
        raise EinnahmeFehler("Bitte einen Grund angeben, etwa „doppelt erfasst“ — gelöscht "
                             "wird nachvollziehbar.")
    if len(grund) > GRUND_HOECHSTENS:
        raise EinnahmeFehler(f"Der Grund ist zu lang ({len(grund)} Zeichen, höchstens "
                             f"{GRUND_HOECHSTENS}).")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not 1 <= einnahme_id <= eingabe.ID_HOECHSTENS:
            raise EinnahmeFehler("Diese Einnahme gibt es nicht.")
        getan = markieren(conn, einnahme_id, grund)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return getan


def wiederherstellen(conn: sqlite3.Connection, einnahme_id: int) -> bool:
    """Nimmt die Loeschmarkierung zurueck (gegen den Fehlklick)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not 1 <= einnahme_id <= eingabe.ID_HOECHSTENS:
            raise EinnahmeFehler("Diese Einnahme gibt es nicht.")
        zeile = conn.execute("SELECT * FROM einnahme WHERE id = ?", (einnahme_id,)).fetchone()
        if zeile is None:
            raise EinnahmeFehler("Diese Einnahme gibt es nicht.")
        if zeile["geloescht_am"] is None:
            conn.execute("COMMIT")
            return False
        conn.execute("UPDATE einnahme SET geloescht_am = NULL, geloescht_grund = NULL"
                     " WHERE id = ?", (einnahme_id,))
        db.protokolliere(conn, "EINNAHME_WIEDERHERGESTELLT", "einnahme", einnahme_id,
                         wert_alt={"geloescht_am": zeile["geloescht_am"],
                                   "grund": zeile["geloescht_grund"]})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


# ---------------------------------------------------------------- Auswerten

def liste(conn: sqlite3.Connection, jahr: int | None = None, *,
          mit_geloeschten: bool = False) -> list[sqlite3.Row]:
    """Die Einnahmen eines Jahres (neueste zuerst) mit Kategorie und Herkunft."""
    bedingungen, werte = [], []
    if jahr:
        bedingungen.append("strftime('%Y', e.zufluss_am) = ?")
        werte.append(f"{jahr:04d}")
    if not mit_geloeschten:
        bedingungen.append("e.geloescht_am IS NULL")
    wo = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""
    return conn.execute(
        f"""
        SELECT e.*, k.bezeichnung AS kategorie, k.schluessel AS kategorie_schluessel,
               k.kennzahl,
               (SELECT COUNT(*) FROM einnahmedatei d WHERE d.einnahme_id = e.id) AS dateien,
               (SELECT w.buchungsnummer FROM wiso_buchung w WHERE w.einnahme_id = e.id
                   AND w.status = 'AKTIV' LIMIT 1) AS wiso_nummer
          FROM einnahme e JOIN euer_kategorie k ON k.id = e.kategorie_id
          {wo}
         ORDER BY e.zufluss_am DESC, e.id DESC
        """, werte).fetchall()


def summen(conn: sqlite3.Connection, jahr: int) -> list[sqlite3.Row]:
    """Je Kategorie die Summe eines Jahres -- nur gueltige Einnahmen."""
    return conn.execute(
        """
        SELECT k.id, k.schluessel, k.bezeichnung, k.kennzahl, COUNT(*) AS anzahl,
               SUM(e.betrag_cent) AS cent
          FROM v_einnahme_gueltig e JOIN euer_kategorie k ON k.id = e.kategorie_id
         WHERE strftime('%Y', e.zufluss_am) = ? AND k.bereich = 20
         GROUP BY k.id ORDER BY k.sortierung, k.bezeichnung
        """, (f"{jahr:04d}",)).fetchall()


def zaehlt_zu_19_cent(conn: sqlite3.Connection, jahr: int) -> int:
    """Was davon in den § 19-Gesamtumsatz zaehlt: nur die Inlandskategorien."""
    platz = ", ".join("?" * len(ZAEHLT_ZU_19))
    zeile = conn.execute(
        f"""
        SELECT COALESCE(SUM(e.betrag_cent), 0) AS cent
          FROM v_einnahme_gueltig e JOIN euer_kategorie k ON k.id = e.kategorie_id
         WHERE strftime('%Y', e.zufluss_am) = ? AND k.schluessel IN ({platz})
        """, (f"{jahr:04d}", *ZAEHLT_ZU_19)).fetchone()
    return int(zeile["cent"])


def jahre(conn: sqlite3.Connection) -> set[int]:
    """Jahre mit gueltigen Einnahmen ohne Rechnung."""
    return {int(z["jahr"]) for z in conn.execute(
        "SELECT DISTINCT CAST(strftime('%Y', zufluss_am) AS INTEGER) AS jahr"
        " FROM v_einnahme_gueltig") if z["jahr"] is not None}


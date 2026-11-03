"""Kundenstamm: Nummernvergabe, Satzermittlung und Pruefung der Eingaben.

Was hier abgewiesen wird, kaeme sonst erst viel spaeter ans Licht -- als
Datenbankfehler beim Anlegen eines Entwurfs, als falsche Landeszeile auf dem
PDF oder als Reverse-Charge-Rechnung mit einer USt-IdNr., die keine ist.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from app import db
from app.filter import LAENDER
from app.logik import rundung

#: Vorbelegung, wenn nichts eingestellt ist.
KUNDENNUMMER_FORMAT = "K-{:04d}"

#: Mehr Stellen hat kein gewachsener Nummernkreis. Ein Tippfehler wie
#: "K-10000000000" wuerde den Zaehler sonst fuer immer dorthin verschieben,
#: denn Luecken werden bewusst nicht aufgefuellt.
KUNDENNUMMER_MAX_STELLEN = 10

#: Zahlungsziel in Tagen: negativ hiesse "faellig vor dem Rechnungsdatum",
#: und mehr als ein Jahr ist kein Zahlungsziel mehr, sondern ein Tippfehler.
ZAHLUNGSZIEL_MAX_TAGE = 365

#: Die 27 Mitgliedstaaten der EU (ISO 3166-1 alpha-2). Griechenland fuehrt
#: seine USt-IdNr. mit "EL" statt "GR", deshalb steht beides drin.
EU_LAENDER = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR",
    "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI",
    "SK",
})
_UST_ID_MUSTER = re.compile(r"^[A-Z]{2}[A-Z0-9]{2,12}$")

#: Die Form der USt-IdNr. je Praefix, nach den Mustern, die VIES selbst
#: veroeffentlicht: (Ausdruck fuer den Teil NACH dem Praefix, Beschreibung,
#: Beispiel). Griechenland fuehrt "EL" statt "GR"; "XI" ist Nordirland, das
#: fuer den Warenverkehr weiter am EU-Mehrwertsteuersystem teilnimmt.
#:
#: Geprueft wird nur die Form, nicht die Pruefziffer -- ob die Nummer
#: vergeben ist, kann nur VIES sagen. Aber "ATU1234567" (eine Stelle zu kurz)
#: faellt hier auf und nicht erst beim Finanzamt des Kunden.
UST_ID_FORMEN: dict[str, tuple[str, str, str]] = {
    "AT": (r"U\d{8}", "ATU und 8 Ziffern", "ATU12345678"),
    "BE": (r"[01]\d{9}", "10 Ziffern, beginnend mit 0 oder 1", "BE0123456789"),
    "BG": (r"\d{9,10}", "9 oder 10 Ziffern", "BG123456789"),
    "CY": (r"\d{8}[A-Z]", "8 Ziffern und ein Buchstabe", "CY12345678X"),
    "CZ": (r"\d{8,10}", "8 bis 10 Ziffern", "CZ12345678"),
    "DE": (r"\d{9}", "9 Ziffern", "DE123456789"),
    "DK": (r"\d{8}", "8 Ziffern", "DK12345678"),
    "EE": (r"\d{9}", "9 Ziffern", "EE123456789"),
    "EL": (r"\d{9}", "9 Ziffern", "EL123456789"),
    "ES": (r"[A-Z0-9]\d{7}[A-Z0-9]",
           "9 Zeichen: Buchstabe oder Ziffer, 7 Ziffern, Buchstabe oder Ziffer", "ESX1234567X"),
    "FI": (r"\d{8}", "8 Ziffern", "FI12345678"),
    "FR": (r"[A-Z0-9]{2}\d{9}", "2 Zeichen (Buchstaben oder Ziffern) und 9 Ziffern",
           "FR12345678901"),
    "HR": (r"\d{11}", "11 Ziffern", "HR12345678901"),
    "HU": (r"\d{8}", "8 Ziffern", "HU12345678"),
    "IE": (r"\d[A-Z0-9+*]\d{5}[A-Z]{1,2}",
           "8 oder 9 Zeichen: Ziffern mit einem oder zwei Buchstaben", "IE1234567X"),
    "IT": (r"\d{11}", "11 Ziffern", "IT12345678901"),
    "LT": (r"\d{9}|\d{12}", "9 oder 12 Ziffern", "LT123456789"),
    "LU": (r"\d{8}", "8 Ziffern", "LU12345678"),
    "LV": (r"\d{11}", "11 Ziffern", "LV12345678901"),
    "MT": (r"\d{8}", "8 Ziffern", "MT12345678"),
    "NL": (r"\d{9}B\d{2}", "9 Ziffern, B und 2 Ziffern", "NL123456789B01"),
    "PL": (r"\d{10}", "10 Ziffern", "PL1234567890"),
    "PT": (r"\d{9}", "9 Ziffern", "PT123456789"),
    "RO": (r"\d{2,10}", "2 bis 10 Ziffern", "RO1234567890"),
    "SE": (r"\d{12}", "12 Ziffern", "SE123456789001"),
    "SI": (r"\d{8}", "8 Ziffern", "SI12345678"),
    "SK": (r"\d{10}", "10 Ziffern", "SK1234567890"),
    "XI": (r"\d{9}|\d{12}|GD\d{3}|HA\d{3}", "9 oder 12 Ziffern", "XI123456789"),
}
_UST_ID_PRAEFIXE_EU = frozenset(UST_ID_FORMEN)
_UST_ID_LAND = {"EL": "Griechenland", "XI": "Nordirland"}

#: Hoechstlaenge einer Steuernummer ausserhalb von Reverse Charge.
STEUERNUMMER_MAX_ZEICHEN = 40

_PLATZHALTER = re.compile(r"\{[^}]*\}")


def pruefe_kundennummer_format(vorlage: str) -> str:
    """Weist Formate zurueck, aus denen sich keine Nummer bauen laesst.

    >>> pruefe_kundennummer_format("{:02d}")
    '{:02d}'
    >>> pruefe_kundennummer_format("K-{:04d}")
    'K-{:04d}'
    """
    vorlage = (vorlage or "").strip()
    if len(_PLATZHALTER.findall(vorlage)) != 1:
        raise ValueError(
            "Das Format der Kundennummer braucht genau einen Zähler, z. B. "
            "{:04d} für 0001 oder {:02d} für 01."
        )
    try:
        probe = vorlage.format(7)
    except (ValueError, IndexError, KeyError) as fehler:
        raise ValueError(f"Das Format der Kundennummer ist unbrauchbar: {fehler}") from fehler
    if not any(z.isdigit() for z in probe):
        raise ValueError("Das Format der Kundennummer muss eine Zahl erzeugen.")
    return vorlage


def format_aus_betrieb(conn: sqlite3.Connection) -> str:
    """Das eingestellte Format -- oder die Vorbelegung, solange keins da ist."""
    zeile = conn.execute("SELECT kundennummer_format FROM betrieb WHERE id = 1").fetchone()
    vorlage = (zeile["kundennummer_format"] if zeile else None) or KUNDENNUMMER_FORMAT
    try:
        return pruefe_kundennummer_format(vorlage)
    except ValueError:
        # Ein kaputtes Format darf die Kundenanlage nicht blockieren.
        return KUNDENNUMMER_FORMAT


def _muster(vorlage: str) -> re.Pattern[str]:
    """Der Ausdruck, der eine Nummer DIESES Formats erkennt und den Zaehler holt.

    Gebaut wird er aus dem Format selbst: fester Text davor und dahinter, in der
    Mitte der Zaehler. Eine zweite, von Hand gepflegte Regex liefe
    unweigerlich gegen das Format.

    >>> _muster("K-{:04d}").match("K-0007").group(1)
    '0007'
    >>> _muster("{:02d}").match("07").group(1)
    '07'
    >>> _muster("K-{:04d}").match("07") is None
    True
    """
    vorne, _, hinten = vorlage.partition(_PLATZHALTER.search(vorlage).group(0))
    return re.compile(f"^{re.escape(vorne)}(\\d+){re.escape(hinten)}$")


def naechste_kundennummer(conn: sqlite3.Connection) -> str:
    """Die naechste freie Kundennummer: die hoechste vergebene plus eins.

    Luecken werden nicht aufgefuellt: eine Nummer, die auf einem
    festgeschriebenen Dokument steht, wird nie wieder vergeben -- ein solcher
    Kunde laesst sich nicht loeschen, nur stilllegen, und stillgelegte Kunden
    behalten ihre Nummer. Wieder frei wird nur die Nummer eines GELOESCHTEN
    Kunden, und auch nur, solange sie die hoechste war und auf keiner
    zurueckgenommenen Rechnung stand. Ein geloeschter Kunde steht auf keinem
    gueltigen Beleg (sonst waere er nicht loeschbar) -- wohl aber womoeglich
    auf einer festgeschriebenen und vor dem Versand zurueckgenommenen
    Rechnung (Paket C): deren PDF ist unter ``ausgangsrechnungen/verworfen/``
    aufbewahrt, und ihre Nummer steht womoeglich als dokumentierte Luecke im
    Journal. Eine solche Kundennummer bleibt verbraucht
    (:func:`verbrauchte_zaehler`), sonst verwiese die Luecke auf einen
    anderen Kunden. Fuer Testkunden ohne solche Rechnung gilt weiter: nach
    dem Aufraeumen bekommt der erste echte Kunde K-0001 bzw. den Startwert.

    ``betrieb.kundennummer_start`` hebt den Zaehler beim Umstieg an. Wer schon
    fuenfzehn Kunden ausserhalb des Werkzeugs gefuehrt hat, soll nicht bei K-0001
    wieder anfangen -- die Nummer steht auf seinen bisherigen Rechnungen, und
    zwei Kunden mit derselben Nummer sind in der Buchfuehrung nicht mehr
    auseinanderzuhalten.
    """
    vorlage = format_aus_betrieb(conn)
    hoechste = max((*belegte_zaehler(conn, vorlage), *verbrauchte_zaehler(conn, vorlage)),
                   default=0)
    return vorlage.format(max(hoechste + 1, kundennummer_start(conn)))


def kundennummer_start(conn: sqlite3.Connection) -> int:
    """Der eingestellte Startwert der Kundennummern (``betrieb.kundennummer_start``)."""
    zeile = conn.execute("SELECT kundennummer_start FROM betrieb WHERE id = 1").fetchone()
    return (zeile["kundennummer_start"] if zeile else 1) or 1


def belegte_zaehler(conn: sqlite3.Connection, vorlage: str) -> dict[int, sqlite3.Row]:
    """Zaehler -> Kunde fuer jede vergebene Nummer im eingestellten Format.

    Verglichen wird ueber den Zaehler, nicht ueber die Zeichenkette: "K-1"
    und "K-0001" sind dieselbe Nummer. Nummern in einem frueheren Format
    zaehlen hier nicht mit -- sie koennen mit einer neuen nicht
    zusammenfallen, weil sie anders aussehen.
    """
    muster = _muster(vorlage)
    belegt: dict[int, sqlite3.Row] = {}
    for zeile in conn.execute("SELECT id, kundennummer, name FROM kunde ORDER BY id"):
        treffer = muster.match(zeile["kundennummer"] or "")
        if treffer:
            belegt.setdefault(int(treffer.group(1)), zeile)
    return belegt


def verbrauchte_zaehler(conn: sqlite3.Connection, vorlage: str) -> dict[int, str]:
    """Zaehler -> Rechnungsnummer fuer jede Kundennummer, die auf einer
    zurueckgenommenen Rechnung stand.

    Gelesen aus dem Journal (RECHNUNG_ZURUECKGENOMMEN, Kundenschnappschuss im
    alten Wert): der Kunde kann inzwischen geloescht sein, die Rechnung
    ebenfalls -- das aufbewahrte PDF und der Journaleintrag nennen seine
    Nummer weiter. Sie wird deshalb nicht wieder vergeben.
    """
    muster = _muster(vorlage)
    verbraucht: dict[int, str] = {}
    for zeile in conn.execute(
        "SELECT json_extract(wert_alt, '$.kopf.kunde_snapshot.kundennummer') AS kundennummer,"
        "       json_extract(wert_neu, '$.nummer') AS rechnung"
        "  FROM journal WHERE aktion = 'RECHNUNG_ZURUECKGENOMMEN' ORDER BY id"
    ):
        treffer = muster.match(str(zeile["kundennummer"] or ""))
        if treffer:
            verbraucht.setdefault(int(treffer.group(1)), str(zeile["rechnung"] or ""))
    return verbraucht


def kundennummer_lesen(vorlage: str, nummer: str) -> tuple[str, int]:
    """Eine Kundennummer im eingestellten Format: (Schreibweise wie gespeichert, Zaehler).

    Gespeichert wird immer ``vorlage.format(zaehler)``. "K-1" und "K-00007"
    standen vorher so in der Datenbank, wie sie getippt waren -- neben
    "K-0001" und "K-0007" desselben Zaehlers, und zwei Kunden trugen
    dieselbe Nummer in zwei Schreibweisen.

    >>> kundennummer_lesen("K-{:04d}", "k-1"), kundennummer_lesen("K-{:04d}", "K-00007")
    (('K-0001', 1), ('K-0007', 7))
    >>> kundennummer_lesen("{:03d}", "7")
    ('007', 7)
    >>> kundennummer_lesen("K-{:04d}", "07")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    ValueError: „07“ passt nicht ins Format K-0001. Nur so lässt sich ...
    """
    gewuenscht = (nummer or "").strip().upper()
    if not gewuenscht:
        raise ValueError("Die Kundennummer ist leer.")
    treffer = _muster(vorlage).match(gewuenscht)
    if not treffer:
        raise ValueError(
            f"„{gewuenscht}“ passt nicht ins Format {vorlage.format(1)}. Nur so lässt "
            "sich die nächste freie Nummer bestimmen."
        )
    zaehler = int(treffer.group(1))
    if len(str(zaehler)) > KUNDENNUMMER_MAX_STELLEN:
        raise ValueError(
            f"„{gewuenscht}“ hat mehr als {KUNDENNUMMER_MAX_STELLEN} Stellen im Zähler. "
            "Eine so große Nummer verschiebt alle künftigen Kundennummern dauerhaft — "
            "Lücken werden nicht wieder aufgefüllt."
        )
    return vorlage.format(zaehler), zaehler


def pruefe_kundennummer(conn: sqlite3.Connection, nummer: str) -> str:
    """Prueft eine von Hand eingegebene Kundennummer; gibt sie in der Schreibweise
    des Formats zurueck.

    Beim Umstieg aus Word will man die gewachsenen Nummern behalten, statt alle
    Altrechnungen neu zu nummerieren. Deshalb ist die Nummer eingebbar -- aber
    nur im eigenen Format und nur, wenn sie frei ist. "Frei" heisst: der
    ZAEHLER ist frei. "K-1" ist belegt, sobald es "K-0001" gibt.
    """
    vorlage = format_aus_betrieb(conn)
    gespeichert, zaehler = kundennummer_lesen(vorlage, nummer)
    vorhanden = belegte_zaehler(conn, vorlage).get(zaehler) or conn.execute(
        "SELECT kundennummer, name FROM kunde WHERE kundennummer = ?", (gespeichert,)
    ).fetchone()
    if vorhanden:
        eingabe = (nummer or "").strip().upper()
        gleich = (f"„{eingabe}“ ist dieselbe Nummer wie {vorhanden['kundennummer']}; sie"
                  if eingabe != vorhanden["kundennummer"] else gespeichert)
        raise ValueError(
            f"{gleich} ist schon an {vorhanden['name']} vergeben. Zwei Kunden mit "
            "derselben Nummer sind in der Buchführung nicht auseinanderzuhalten."
        )
    rechnung = verbrauchte_zaehler(conn, vorlage).get(zaehler)
    if rechnung is not None:
        raise ValueError(verbraucht_text(gespeichert, rechnung))
    return gespeichert


def verbraucht_text(kundennummer: str, rechnung: str) -> str:
    """Warum die Nummer eines geloeschten Kunden nicht wieder vergeben wird."""
    return (
        f"{kundennummer} stand auf der zurückgenommenen Rechnung {rechnung} (aufbewahrt, "
        "mit Eintrag im Protokoll) und wird nicht wieder vergeben — sonst verwiese diese "
        "Rechnung auf einen anderen Kunden."
    )


def satz_cent(conn: sqlite3.Connection, kunde_id: int) -> int:
    """Der Stundensatz fuer einen Kunden.

    Zwei Ebenen: Kunde schlaegt Betrieb. Ein Festpreis am Zeiteintrag schlaegt
    beide -- das entscheidet aber die Rechnungslogik, nicht diese Funktion.
    """
    zeile = conn.execute(
        """
        SELECT COALESCE(k.satz_cent, b.standardsatz_cent, 0) AS satz
          FROM kunde k
          LEFT JOIN betrieb b ON b.id = 1
         WHERE k.id = ?
        """,
        (kunde_id,),
    ).fetchone()
    return int(zeile["satz"]) if zeile else 0


def betriebsregel(conn: sqlite3.Connection) -> rundung.Regel:
    """Die Rundungsregel des Betriebs, die Vorgabe fuer jeden Kunden ohne eigene.

    Ohne eingerichteten Betrieb gilt die Vorbelegung des Schemas.
    """
    zeile = conn.execute(
        "SELECT rundung_modus, rundung_intervall_min FROM betrieb WHERE id = 1"
    ).fetchone()
    if zeile is None:
        return rundung.Regel(rundung.AUFWAERTS, 15)
    return rundung.Regel(zeile["rundung_modus"], int(zeile["rundung_intervall_min"]))


def rundungsregel(conn: sqlite3.Connection, kunde_id: int) -> rundung.Regel:
    """Die Rundungsregel fuer einen Kunden: seine eigene, sonst die des Betriebs.

    Wie beim Stundensatz zwei Ebenen, Kunde schlaegt Betrieb. Der Takt gilt
    nur zusammen mit einer eigenen Regel; ohne Takt gilt der des Betriebs.
    Eine Rundung ist Vertragsinhalt und unterscheidet sich je Kunde -- die
    Grundlage reist deshalb mit.
    """
    betrieb = betriebsregel(conn)
    zeile = conn.execute(
        "SELECT rundung_modus, rundung_intervall_min, rundung_grundlage FROM kunde"
        " WHERE id = ?",
        (kunde_id,),
    ).fetchone()
    if zeile is None or zeile["rundung_modus"] is None:
        return rundung.Regel(
            betrieb.modus, betrieb.intervall_min, "BETRIEB",
            zeile["rundung_grundlage"] if zeile else None,
        )
    return rundung.Regel(
        zeile["rundung_modus"],
        int(zeile["rundung_intervall_min"] or betrieb.intervall_min),
        "KUNDE",
        zeile["rundung_grundlage"],
    )


def zahlungsziel_tage(conn: sqlite3.Connection, kunde_id: int) -> int:
    zeile = conn.execute(
        """
        SELECT COALESCE(k.zahlungsziel_tage, b.zahlungsziel_tage, 14) AS tage
          FROM kunde k
          LEFT JOIN betrieb b ON b.id = 1
         WHERE k.id = ?
        """,
        (kunde_id,),
    ).fetchone()
    return int(zeile["tage"]) if zeile else 14


#: Obergrenze fuer jeden eingegebenen Betrag: eine Milliarde Euro.
BETRAG_HOECHSTENS_CENT = 100_000_000_000

#: Was nach dem Entfernen von Leerzeichen und Eurozeichen von einem Betrag
#: uebrig bleiben darf. Bewusst [0-9] und nicht \d: das passt auch auf "٣".
_BETRAG_ZEICHEN = re.compile(r"[0-9.,+-]+")


class BetragZuGross(ArithmeticError):
    """Ein Betrag über BETRAG_HOECHSTENS_CENT.

    Ein ArithmeticError wie die übrigen Betragsfehler, damit jeder Aufrufer,
    der die fängt, auch diesen fängt. Eigene Klasse, damit ein Formular mit
    engerer Grenze (der Leistungskatalog) seine eigene Meldung zeigen kann.
    """


def euro_nach_cent(eingabe: str | None) -> int | None:
    """Liest einen Eurobetrag als Cent. Leere Eingabe ergibt None.

    Deutsche Schreibweise: Komma trennt die Dezimalen, Punkt die Tausender.
    Fehlt das Komma, ist ein Punkt TROTZDEM ein Tausendertrenner -- "1.234"
    sind 1234 Euro, nicht 1,23. Das ist der Fall, an dem eine naive Umsetzung
    scheitert, und er ist teuer: der Betrag landet um den Faktor 1000 zu klein
    auf einer Rechnung oder als Stundensatz im Kundenstamm.

    Erkannt wird der Tausendertrenner an seiner Form: genau drei Ziffern
    dahinter, und das bis zum Ende. Ein Punkt mit ein, zwei oder mehr als drei
    Ziffern dahinter kann kein Tausendertrenner sein und wird als
    Dezimaltrenner gelesen -- so bleibt "12.5" fuer 12,50 Euro verstaendlich,
    wie es jemand mit englischer Tastaturgewohnheit tippt.

    >>> euro_nach_cent("85,00"), euro_nach_cent("1.234,56"), euro_nach_cent("")
    (8500, 123456, None)
    >>> euro_nach_cent("1.234"), euro_nach_cent("1.234.567")
    (123400, 123456700)
    >>> euro_nach_cent("12.5"), euro_nach_cent("12.50")
    (1250, 1250)

    Die englische Schreibweise -- Komma als Tausendertrenner, Punkt als
    Dezimaltrenner -- ist an ihrer Form eindeutig: ein Komma, dahinter GENAU
    drei Ziffern, dann ein Punkt mit den Dezimalen. "1,234.56" sind
    1.234,56 Euro. Deutsch gelesen ("1,23456") waere der Betrag um den
    Faktor 1000 zu klein gebucht worden, ohne Meldung.

    Ohne den Punkt dahinter bleibt es deutsch: "1,234" sind 1,23 Euro, denn
    ein Komma mit drei Dezimalen ist kaufmaennisch gerundet eindeutig.

    >>> euro_nach_cent("1,234.56"), euro_nach_cent("12,345,678.90")
    (123456, 1234567890)
    >>> euro_nach_cent("1,234")
    123

    Erlaubt sind nur ASCII-Ziffern, Komma, Punkt, Vorzeichen, Leerzeichen und
    das Eurozeichen. ``Decimal`` las vorher auch die Exponentschreibweise
    ("1e3" = 1.000,00 €), arabisch-indische ("٣") und Vollbreiten-Ziffern
    ("２４") -- ein Tippfehler wurde so still zu einem ganz anderen Betrag.
    Unendlich, NaN und Riesenbeträge sind ebenfalls kein Betrag:

    >>> for roh in ("inf", "1e3", "1e400", "٣", "２４", "NaN", "99999999999999999999"):
    ...     try:
    ...         euro_nach_cent(roh)
    ...     except ArithmeticError as fehler:
    ...         print(fehler)
    „inf“ ist kein Betrag.
    „1e3“ ist kein Betrag.
    „1e400“ ist kein Betrag.
    „٣“ ist kein Betrag.
    „２４“ ist kein Betrag.
    „NaN“ ist kein Betrag.
    „99999999999999999999“ ist als Betrag unplausibel groß.
    >>> euro_nach_cent(" 1 234,50 € "), euro_nach_cent("-24,90"), euro_nach_cent("+5")
    (123450, -2490, 500)

    Mehr als zwei Dezimalen werden kaufmännisch auf den Cent gerundet, ohne
    Rückfrage -- so steht es auch auf jedem Kassenbon:

    >>> euro_nach_cent("24,999"), euro_nach_cent("24,994")
    (2500, 2499)
    """
    if eingabe is None:
        return None
    # Leerzeichen jeder Art (auch das geschuetzte aus einer kopierten Tabelle)
    # und das Eurozeichen sind Schmuck; alles andere muss Teil der Zahl sein.
    text = "".join(eingabe.replace("€", "").split())
    if not text:
        return None
    if not _BETRAG_ZEICHEN.fullmatch(text):
        raise ArithmeticError(f"„{eingabe}“ ist kein Betrag.")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+\.\d+", text):
        # Englisch: Tausender-Komma, Dezimalpunkt.
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", text):
        # Nur Punkte, und jeder trennt genau drei Ziffern ab: Tausendertrenner.
        text = text.replace(".", "")
    from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

    try:
        betrag = Decimal(text) * 100
    except InvalidOperation as fehler:
        # Ein ArithmeticError, damit die Aufrufer ihn wie bisher fangen.
        raise ArithmeticError(f"„{eingabe}“ ist kein Betrag.") from fehler
    if not betrag.is_finite():
        raise ArithmeticError(f"„{eingabe}“ ist kein Betrag.")
    cent = int(betrag.to_integral_value(rounding=ROUND_HALF_UP))
    if abs(cent) > BETRAG_HOECHSTENS_CENT:
        # Sonst sprengte der Wert erst die Bindung an SQLite (ab 2**63) und
        # endete als Serverfehler -- in jedem Formular mit einem Betragsfeld.
        raise BetragZuGross(f"„{eingabe}“ ist als Betrag unplausibel groß.")
    return cent


def hinweis_namensdoppel(conn: sqlite3.Connection, name: str) -> str | None:
    """Ein Hinweis, wenn es den Kundennamen schon gibt -- sonst None.

    Kein Verbot: zwei Kunden koennen tatsaechlich gleich heissen (zwei
    Filialen, Vater und Sohn). Aber in den Auswahllisten der Zeiterfassung
    stehen sie dann zweimal gleich da, und die Zeit landet leicht beim
    Falschen. Deshalb wird gesagt, wer der andere ist.
    """
    gesucht = name.strip().casefold()
    gleiche = [
        zeile
        for zeile in conn.execute(
            "SELECT kundennummer, name, ort FROM kunde ORDER BY id"
        )
        if (zeile["name"] or "").strip().casefold() == gesucht
    ]
    if not gleiche:
        return None
    if len(gleiche) == 1:
        erster = gleiche[0]
        return (
            f"Es gibt bereits einen Kunden „{erster['name']}“ "
            f"({erster['kundennummer']}, {erster['ort']})."
        )
    aufzaehlung = ", ".join(f"{z['kundennummer']} ({z['ort']})" for z in gleiche)
    return f"Es gibt bereits {len(gleiche)} Kunden „{name.strip()}“: {aufzaehlung}."


def _namensschluessel(spalte: str) -> str:
    """SQL-Ausdruck: der Name ohne Rand-Leerzeichen, klein, Umlaute eingeschlossen.

    SQLites lower() kennt nur ASCII; "MÜLLER" und "Müller" waeren sonst zwei
    verschiedene Namen, obwohl sie in der Auswahlliste gleich aussehen.
    """
    ausdruck = f"trim({spalte})"
    for gross, klein in (("Ä", "ä"), ("Ö", "ö"), ("Ü", "ü"), ("ẞ", "ß")):
        ausdruck = f"replace({ausdruck}, '{gross}', '{klein}')"
    return f"lower({ausdruck})"


def doppelt_sql(alias: str = "k") -> str:
    """SQL-Ausdruck: 1, wenn ein ANDERER Kunde gleich heisst (ohne Gross/Klein).

    Fuer die Kundenauswahlen (Zeiterfassung, Timer, Filter, Rechnungsassistent):
    zwei Kunden "Doppel GmbH" standen dort zweimal gleich, und die Zeit landete
    leicht beim Falschen. Die Vorlage haengt dann "(K-0001, Ort)" an.
    """
    return (
        f"EXISTS (SELECT 1 FROM kunde doppel WHERE doppel.id <> {alias}.id"
        f" AND {_namensschluessel('doppel.name')} = {_namensschluessel(f'{alias}.name')})"
    )


def normalisiere_land(eingabe: str | None) -> str:
    """Laenderkuerzel oder deutscher Laendername -- gespeichert wird das Kuerzel.

    Freitext im Land macht aus einem Berliner Kunden einen Auslandskunden:
    alles Weitere vergleicht hart mit "DE", und "DEUTSCHLAND" ist eben nicht
    "DE". Zwei Buchstaben gelten als ISO-Kuerzel, auch wenn die Liste sie nicht
    kennt -- sie kennt nicht jedes Land, und ein unbekanntes Kuerzel druckt
    das PDF so, wie es eingegeben wurde. Alles andere wird abgewiesen.

    >>> normalisiere_land("Deutschland"), normalisiere_land("österreich")
    ('DE', 'AT')
    >>> normalisiere_land(" at "), normalisiere_land("de"), normalisiere_land("")
    ('AT', 'DE', 'DE')
    >>> normalisiere_land("Vereinigtes Königreich")
    'GB'
    >>> normalisiere_land("DEU")
    Traceback (most recent call last):
        ...
    ValueError: „DEU“ ist kein Land. Land als Kürzel (DE, AT, CH …) oder Name eingeben.
    >>> normalisiere_land("A T")
    Traceback (most recent call last):
        ...
    ValueError: „A T“ ist kein Land. Land als Kürzel (DE, AT, CH …) oder Name eingeben.
    """
    text = (eingabe or "").strip()
    if not text:
        return "DE"
    kuerzel = text.upper()
    if re.fullmatch(r"[A-Z]{2}", kuerzel):
        return kuerzel
    gesucht = text.casefold()
    for code, name in LAENDER.items():
        if name.casefold() == gesucht:
            return code
    raise ValueError(
        f"„{text}“ ist kein Land. Land als Kürzel (DE, AT, CH …) oder Name eingeben."
    )


def _ust_idnr_passt(kompakt: str) -> bool:
    """Hat die kompakte Nummer die Form einer EU-USt-IdNr. ihres Praefixes?

    >>> _ust_idnr_passt("ATU12345678"), _ust_idnr_passt("ATU1234567")
    (True, False)
    >>> _ust_idnr_passt("CHE123456789")
    False
    """
    form = UST_ID_FORMEN.get(kompakt[:2])
    return form is not None and re.fullmatch(form[0], kompakt[2:]) is not None


def ist_eu_praefix(praefix: str) -> bool:
    """Beginnt eine USt-IdNr. mit diesem Kuerzel, stammt sie aus der EU (EL, XI)?

    >>> ist_eu_praefix("AT"), ist_eu_praefix("EL"), ist_eu_praefix("GR"), ist_eu_praefix("UN")
    (True, True, False, False)
    """
    return praefix in _UST_ID_PRAEFIXE_EU


def ust_idnr_formfehler(nummer: str | None) -> str | None:
    """Warum eine gespeicherte USt-IdNr. nicht die Form ihres EU-Staats hat -- oder None.

    Fuer die Pruefung vor dem Festschreiben. Das Kundenformular weist eine
    solche Nummer bei Reverse Charge ab; eine Datenbank aus der Zeit davor
    kann sie aber noch tragen ("ATU1234567", eine Stelle zu kurz). Leer ist
    hier kein Formfehler -- das meldet die Regel R18.

    >>> ust_idnr_formfehler("ATU12345678"), ust_idnr_formfehler("atu 123.456-78")
    (None, None)
    >>> print(ust_idnr_formfehler("ATU1234567"))
    hat nicht die Form einer USt-IdNr. aus Österreich (ATU und 8 Ziffern, z. B. ATU12345678)
    >>> print(ust_idnr_formfehler("ungültig 123"))
    beginnt nicht mit dem Kürzel eines EU-Staats
    >>> print(ust_idnr_formfehler("GR123456789"))
    beginnt mit GR — griechische USt-IdNrn. beginnen mit EL, z. B. EL123456789
    >>> ust_idnr_formfehler(""), ust_idnr_formfehler(None)
    (None, None)
    """
    kompakt = re.sub(r"[\s.\-]", "", nummer or "").upper()
    if not kompakt:
        return None
    praefix = kompakt[:2]
    if praefix == "GR":
        return "beginnt mit GR — griechische USt-IdNrn. beginnen mit EL, z. B. EL123456789"
    if not ist_eu_praefix(praefix):
        return "beginnt nicht mit dem Kürzel eines EU-Staats"
    if _ust_idnr_passt(kompakt):
        return None
    _, beschreibung, beispiel = UST_ID_FORMEN[praefix]
    land = _UST_ID_LAND.get(praefix) or LAENDER.get(praefix, praefix)
    return f"hat nicht die Form einer USt-IdNr. aus {land} ({beschreibung}, z. B. {beispiel})"


def normalisiere_ust_idnr(eingabe: str | None, leistungsort: str = "INLAND") -> str | None:
    """USt-IdNr. bzw. Steuernummer des Kunden in der Schreibweise fuer die Rechnung.

    Bei Reverse Charge (``EU_B2B_REVERSE_CHARGE``) ist die USt-IdNr. der
    Nachweis der Unternehmereigenschaft (§ 3a Abs. 2 UStG) und steht auf der
    Rechnung (§ 14a Abs. 1 UStG). Dort wird sie streng geprueft: zwei
    Buchstaben Laenderpraefix eines EU-Staats (oder EL, XI), dahinter genau
    die Form, die VIES fuer dieses Land vorgibt. Gespeichert wird sie
    kompakt -- Grossbuchstaben, ohne Leerzeichen, Punkte und Bindestriche --,
    so, wie jedes Finanzamt und VIES sie lesen. Eine Schweizer, erfundene oder
    um eine Stelle zu kurze Nummer belegt nichts.

    >>> normalisiere_ust_idnr("atu12345678", "EU_B2B_REVERSE_CHARGE")
    'ATU12345678'
    >>> normalisiere_ust_idnr("EL-123.456.789", "EU_B2B_REVERSE_CHARGE")
    'EL123456789'
    >>> rc = "EU_B2B_REVERSE_CHARGE"
    >>> [normalisiere_ust_idnr(n, rc) for n in ("DE 123 456 789", "FR 12 345678901",
    ...  "nl123456789b01", "IT12345678901", "ESB1234567X", "BE0123456789", "IE1234567WA")]
    ['DE123456789', 'FR12345678901', 'NL123456789B01', 'IT12345678901', 'ESB1234567X', \
'BE0123456789', 'IE1234567WA']
    >>> normalisiere_ust_idnr("ATU1234567", rc)  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    ValueError: „ATU1234567“ ist keine USt-IdNr. aus Österreich: dort sind es ATU und 8 ..., \
z. B. ATU12345678.
    >>> for falsch in ("DE12345678", "FR1234", "NL123456789", "IT1234567890", "ES12345678",
    ...                "BE2123456789", "IE12345678"):
    ...     try:
    ...         normalisiere_ust_idnr(falsch, rc)
    ...     except ValueError as fehler:
    ...         print(str(fehler).split(", z. B. ")[1])
    DE123456789.
    FR12345678901.
    NL123456789B01.
    IT12345678901.
    ESX1234567X.
    BE0123456789.
    IE1234567X.
    >>> normalisiere_ust_idnr("ungültig 123", rc)  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    ValueError: „ungültig 123“ ist keine USt-IdNr. Erwartet: Länderkürzel und Nummer, ...
    >>> normalisiere_ust_idnr("CHE123456789", rc)  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    ValueError: „CHE123456789“ beginnt mit CH — das ist kein EU-Land. ...

    Ohne Reverse Charge (Inland, Drittland) steht die Nummer nur zur
    Information beim Kunden. Dort ist jede Steuernummer erlaubt -- die
    Schweizer MWST-Nummer steht amtlich als "CHE-123.456.789 MWST" und wurde
    sonst abgewiesen. Entfernt werden nur Steuerzeichen; Leerzeichen bleiben.
    Hat die Nummer die Form einer EU-USt-IdNr., wird sie wie oben kompakt
    gespeichert, damit sie bei einem spaeteren Wechsel zu Reverse Charge
    schon passt.

    >>> normalisiere_ust_idnr("CHE-123.456.789 mwst", "DRITTLAND")
    'CHE-123.456.789 MWST'
    >>> normalisiere_ust_idnr("de 123 456 789"), normalisiere_ust_idnr("12/345/67890")
    ('DE123456789', '12/345/67890')
    >>> normalisiere_ust_idnr(""), normalisiere_ust_idnr(None), normalisiere_ust_idnr("\\x00")
    (None, None, None)
    >>> normalisiere_ust_idnr("X" * 41, "DRITTLAND")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    ValueError: Die Steuernummer des Kunden ist zu lang (41 Zeichen, höchstens 40).
    """
    # Steuerzeichen (NUL, Zeilenumbruch, unsichtbare Formatzeichen) haben in
    # einer Nummer nichts verloren -- und SQLite zaehlt bei NUL nicht weiter.
    roh = "".join(
        " " if z.isspace() else z
        for z in (eingabe or "")
        if z.isspace() or not unicodedata.category(z).startswith("C")
    )
    roh = " ".join(roh.split())
    if not roh:
        return None
    kompakt = re.sub(r"[\s.\-]", "", roh).upper()

    if leistungsort != "EU_B2B_REVERSE_CHARGE":
        if _ust_idnr_passt(kompakt):
            return kompakt
        if len(roh) > STEUERNUMMER_MAX_ZEICHEN:
            raise ValueError(
                f"Die Steuernummer des Kunden ist zu lang ({len(roh)} Zeichen, "
                f"höchstens {STEUERNUMMER_MAX_ZEICHEN})."
            )
        return roh.upper()

    if not _UST_ID_MUSTER.match(kompakt):
        raise ValueError(
            f"„{roh[:40]}“ ist keine USt-IdNr. Erwartet: Länderkürzel und Nummer, "
            "z. B. DE123456789 oder ATU12345678."
        )
    praefix = kompakt[:2]
    if praefix == "GR":
        raise ValueError(
            f"„{roh[:40]}“: griechische USt-IdNrn. beginnen mit EL, nicht mit GR, "
            "z. B. EL123456789."
        )
    if praefix not in _UST_ID_PRAEFIXE_EU:
        raise ValueError(
            f"„{roh[:40]}“ beginnt mit {praefix} — das ist kein EU-Land. Bei Reverse Charge "
            "innerhalb der EU muss die USt-IdNr. aus einem EU-Mitgliedstaat stammen."
        )
    if not _ust_idnr_passt(kompakt):
        _, beschreibung, beispiel = UST_ID_FORMEN[praefix]
        land = _UST_ID_LAND.get(praefix) or LAENDER.get(praefix, praefix)
        raise ValueError(
            f"„{roh[:40]}“ ist keine USt-IdNr. aus {land}: dort sind es {beschreibung}, "
            f"z. B. {beispiel}."
        )
    return kompakt


def pruefe_email(eingabe: str | None) -> str | None:
    """Mindestpruefung einer E-Mail-Adresse; leer ergibt None.

    Genau ein @, dahinter ein Punkt. Mehr prueft kein Postfach vorab -- aber
    "keine-adresse" erzeugt sonst einen mailto-Link, der ins Leere fuehrt.

    >>> pruefe_email(" post@example.org "), pruefe_email("")
    ('post@example.org', None)
    >>> pruefe_email("keine-adresse")
    Traceback (most recent call last):
        ...
    ValueError: „keine-adresse“ ist keine E-Mail-Adresse. Beispiel: buchhaltung@firma.de
    >>> pruefe_email("a@b@c.de")
    Traceback (most recent call last):
        ...
    ValueError: „a@b@c.de“ ist keine E-Mail-Adresse. Beispiel: buchhaltung@firma.de
    """
    text = (eingabe or "").strip()
    if not text:
        return None
    vorne, at, hinten = text.partition("@")
    if (
        not at
        or "@" in hinten
        or not vorne
        or "." not in hinten
        or hinten.startswith(".")
        or hinten.endswith(".")
        or any(z.isspace() for z in text)
    ):
        raise ValueError(f"„{text}“ ist keine E-Mail-Adresse. Beispiel: buchhaltung@firma.de")
    return text


def pruefe_zahlungsziel(eingabe: str | None) -> int | None:
    """Zahlungsziel in Tagen; leer heisst Betriebsstandard.

    >>> pruefe_zahlungsziel(" 7 "), pruefe_zahlungsziel(""), pruefe_zahlungsziel("0")
    (7, None, 0)
    >>> pruefe_zahlungsziel("-5")
    Traceback (most recent call last):
        ...
    ValueError: Das Zahlungsziel muss zwischen 0 und 365 Tagen liegen; leer = Standard.
    >>> pruefe_zahlungsziel("14.5")
    Traceback (most recent call last):
        ...
    ValueError: „14.5“ ist keine Anzahl Tage.
    """
    text = (eingabe or "").strip()
    if not text:
        return None
    try:
        tage = int(text)
    except ValueError as fehler:
        raise ValueError(f"„{text}“ ist keine Anzahl Tage.") from fehler
    if not 0 <= tage <= ZAHLUNGSZIEL_MAX_TAGE:
        raise ValueError(
            f"Das Zahlungsziel muss zwischen 0 und {ZAHLUNGSZIEL_MAX_TAGE} Tagen liegen; "
            "leer = Standard."
        )
    return tage


def pruefe_stundensatz(eingabe: str | None) -> int | None:
    """Stundensatz des Kunden in Cent; leer heisst Betriebsstandard.

    Null und negative Saetze werden abgewiesen, nicht stillschweigend als
    "leer" gelesen: ein gespeicherter Satz von 0 wirkte im Formular wie leer,
    galt aber als gesetzt -- und der naechste Rechnungsentwurf scheiterte an
    der Datenbank, weil eine Position ohne Nachlassgrund einen Preis ueber 0
    braucht.

    >>> pruefe_stundensatz("85,50"), pruefe_stundensatz("")
    (8550, None)
    >>> pruefe_stundensatz("0,00")
    Traceback (most recent call last):
        ...
    ValueError: Der Stundensatz muss größer als 0 sein; leer = Standardsatz.
    >>> pruefe_stundensatz("abc")
    Traceback (most recent call last):
        ...
    ValueError: „abc“ ist kein Betrag. Beispiel: 85,00
    >>> pruefe_stundensatz("NaN")
    Traceback (most recent call last):
        ...
    ValueError: „NaN“ ist kein Betrag. Beispiel: 85,00
    >>> pruefe_stundensatz("9223372036854775808")
    Traceback (most recent call last):
        ...
    ValueError: „9223372036854775808“ ist als Stundensatz unplausibel groß. Beispiel: 85,00
    """
    # "NaN" und "Infinity" sind fuer Decimal gueltig, fuer einen Stundensatz
    # nicht; Riesenbetraege weist euro_nach_cent selbst ab -- vorher sprengten
    # sie erst die Bindung an SQLite, und der Nutzer sah einen Serverfehler.
    try:
        cent = euro_nach_cent(eingabe)
    except (ArithmeticError, ValueError) as fehler:
        text = (eingabe or "").strip()[:40]
        if isinstance(fehler, BetragZuGross):
            raise ValueError(
                f"„{text}“ ist als Stundensatz unplausibel groß. Beispiel: 85,00"
            ) from fehler
        raise ValueError(f"„{text}“ ist kein Betrag. Beispiel: 85,00") from fehler
    if cent is not None and cent <= 0:
        raise ValueError("Der Stundensatz muss größer als 0 sein; leer = Standardsatz.")
    return cent


# ------------------------------------------------- Kunde pruefen (Formular, Import)

#: Erlaubte Werte -- dieselben Listen wie die CHECKs in schema.sql.
TYPEN = ("UNTERNEHMER", "JURISTISCHE_PERSON", "PRIVAT")
LEISTUNGSORTE = ("INLAND", "EU_B2B_REVERSE_CHARGE", "DRITTLAND")
GRUPPIERUNGEN = ("SAMMEL", "PRO_TITEL", "PRO_TAG", "PRO_EINTRAG")

#: Klartexte je Wert. Der erste ist der, den die Importvorlage anbietet; die
#: uebrigen sind Schreibweisen, die anderswo im Werkzeug stehen (Formular,
#: Excel-Export, Rechnungspruefung) -- ein Import soll sie alle wiedererkennen,
#: auch die Datei, die der eigene Excel-Export geschrieben hat.
TYP_TEXTE = {
    "UNTERNEHMER": ("Unternehmer", "Unternehmen"),
    "JURISTISCHE_PERSON": ("Juristische Person",),
    "PRIVAT": ("Privatperson", "privat"),
}
LEISTUNGSORT_TEXTE = {
    "INLAND": ("Inland",),
    "EU_B2B_REVERSE_CHARGE": ("EU-Unternehmer (Reverse Charge)", "EU B2B, Reverse Charge",
                              "EU-B2B, Reverse Charge", "Reverse Charge"),
    "DRITTLAND": ("Drittland",),
}
GRUPPIERUNG_TEXTE = {
    "PRO_TITEL": ("pro Titel", "je Titel"),
    "PRO_TAG": ("pro Tag", "je Tag"),
    "SAMMEL": ("eine Sammelposition", "Sammelposition"),
    "PRO_EINTRAG": ("pro Eintrag", "je Eintrag"),
}

#: Die Felder des Kundenformulars, die :func:`pruefe_kunde` liest.
FELDER = ("name", "rechnungsname", "ansprechpartner", "strasse", "plz", "ort", "land",
          "email", "ust_idnr", "typ", "leistungsort", "satz_euro", "zahlungsziel",
          "gruppierung", "notiz", "aktiv")

#: Pflichtfelder nach § 34a Satz 1 Nr. 1 UStDV, mit dem Namen, den die
#: Meldung nennt.
PFLICHTFELDER = {"name": "Name", "strasse": "Straße", "plz": "PLZ", "ort": "Ort"}

#: Einzeilige Felder: Zeilenumbrueche und doppelte Leerzeichen haben dort
#: nichts verloren -- auf dem PDF bricht ein Name sonst mitten in der Anschrift
#: um. Mehrzeilig ist nur die Notiz.
EINZEILIG = ("name", "rechnungsname", "ansprechpartner", "strasse", "plz", "ort")


def einzeilig(text: str | None) -> str:
    """Leerraum jeder Art zu einem Leerzeichen, am Rand nichts.

    >>> einzeilig("  Beispiel\\r\\n GmbH  "), einzeilig(None)
    ('Beispiel GmbH', '')
    """
    return " ".join((text or "").split())


def code_aus_text(eingabe: str | None, texte: Mapping[str, tuple[str, ...]]) -> str | None:
    """Der Datenbankwert zu einem Code oder Klartext; None, wenn nichts passt.

    Gross/klein und Leerraum spielen keine Rolle.

    >>> code_aus_text("EU-B2B, Reverse Charge", LEISTUNGSORT_TEXTE)
    'EU_B2B_REVERSE_CHARGE'
    >>> code_aus_text(" je  titel ", GRUPPIERUNG_TEXTE), code_aus_text("privat", TYP_TEXTE)
    ('PRO_TITEL', 'PRIVAT')
    >>> code_aus_text("inland", LEISTUNGSORT_TEXTE), code_aus_text("Mond", LEISTUNGSORT_TEXTE)
    ('INLAND', None)
    """
    gesucht = einzeilig(eingabe).casefold()
    if not gesucht:
        return None
    for code, namen in texte.items():
        if gesucht == code.casefold() or gesucht in (n.casefold() for n in namen):
            return code
    return None


def pflicht_fehlt(feld: str) -> str:
    """Die Meldung fuer ein leeres Pflichtfeld der Anschrift."""
    return (
        f"{PFLICHTFELDER[feld]} fehlt. § 34a Satz 1 Nr. 1 UStDV verlangt den vollständigen "
        "Namen und die vollständige Anschrift des Leistungsempfängers — ohne sie lässt "
        "sich keine Rechnung festschreiben."
    )


def pruefe_kunde(felder: Mapping[str, str | None]) -> tuple[dict[str, object], dict[str, str]]:
    """Prueft und normalisiert die Angaben eines Kunden: (Werte, Fehler je Feld).

    Eine Pruefung fuer Formular und Import. Gesammelt werden ALLE Fehler,
    jeweils unter dem Feldnamen des Formulars, in der Reihenfolge, in der das
    Formular sie bisher einzeln meldete -- das Formular zeigt den ersten, der
    Import jeden an seiner Zelle. Die Werte sind nur vollstaendig, wenn es
    keine Fehler gibt; ihre Schluessel sind die Spalten der Tabelle ``kunde``.

    Typ, Leistungsort und Gruppierung nehmen den Code oder einen der
    Klartexte (:data:`TYP_TEXTE` usw.).

    >>> werte, fehler = pruefe_kunde({"name": " Beispiel\\n GmbH ", "strasse": "Musterstraße 1",
    ...     "plz": "10115", "ort": "Berlin", "typ": "Unternehmer", "leistungsort": "Inland",
    ...     "gruppierung": "je Titel", "satz_euro": "85,50", "aktiv": "1"})
    >>> fehler, werte["name"], werte["satz_cent"], werte["gruppierung_default"]
    ({}, 'Beispiel GmbH', 8550, 'PRO_TITEL')
    >>> sorted(pruefe_kunde({"typ": "UNTERNEHMER", "leistungsort": "INLAND",
    ...                      "gruppierung": "PRO_TITEL", "zahlungsziel": "-1"})[1])
    ['name', 'ort', 'plz', 'strasse', 'zahlungsziel']
    """
    def wert(name: str) -> str:
        return felder.get(name) or ""

    fehler: dict[str, str] = {}
    typ = code_aus_text(wert("typ"), TYP_TEXTE)
    if typ is None:
        fehler["typ"] = "Unbekannter Kundentyp."
    leistungsort = code_aus_text(wert("leistungsort"), LEISTUNGSORT_TEXTE)
    if leistungsort is None:
        fehler["leistungsort"] = "Unbekannter Leistungsort."
    gruppierung = code_aus_text(wert("gruppierung"), GRUPPIERUNG_TEXTE)
    if gruppierung is None:
        fehler["gruppierung"] = "Unbekannte Gruppierung."

    zeilen = {feld: einzeilig(wert(feld)) for feld in EINZEILIG}
    for feld in PFLICHTFELDER:
        if not zeilen[feld]:
            fehler[feld] = pflicht_fehlt(feld)

    land = "DE"
    try:
        land = normalisiere_land(wert("land"))
    except ValueError as problem:
        fehler["land"] = str(problem)
    # Excel macht aus der PLZ 01067 die Zahl 1067. Geraten wird nicht: eine
    # vierstellige deutsche PLZ ist ein Fehler mit Vorschlag, keine Korrektur.
    if land == "DE" and re.fullmatch(r"[0-9]{4}", zeilen["plz"]):
        fehler["plz"] = (
            f"„{zeilen['plz']}“ hat nur 4 Ziffern; eine deutsche PLZ hat 5. Fehlt die "
            f"führende Null (0{zeilen['plz']})? Tabellenprogramme entfernen sie, wenn die "
            "Spalte als Zahl formatiert ist."
        )
    pruefungen = (
        ("email", lambda: pruefe_email(wert("email"))),
        ("ust_idnr", lambda: normalisiere_ust_idnr(wert("ust_idnr"), leistungsort or "INLAND")),
        ("satz_euro", lambda: pruefe_stundensatz(wert("satz_euro"))),
        ("zahlungsziel", lambda: pruefe_zahlungsziel(wert("zahlungsziel"))),
    )
    gelesen: dict[str, object] = {}
    for feld, pruefung in pruefungen:
        try:
            gelesen[feld] = pruefung()
        except ValueError as problem:
            fehler[feld] = str(problem)
    if fehler:
        return {}, fehler

    notiz = wert("notiz").strip()
    return {
        "name": zeilen["name"],
        "rechnungsname": zeilen["rechnungsname"] or None,
        "ansprechpartner": zeilen["ansprechpartner"] or None,
        "strasse": zeilen["strasse"],
        "plz": zeilen["plz"],
        "ort": zeilen["ort"],
        "land": land,
        "email": gelesen["email"],
        "ust_idnr": gelesen["ust_idnr"],
        "typ": typ,
        "leistungsort": leistungsort,
        "satz_cent": gelesen["satz_euro"],
        "zahlungsziel_tage": gelesen["zahlungsziel"],
        "gruppierung_default": gruppierung,
        "notiz": notiz or None,
        "aktiv": 1 if wert("aktiv") == "1" else 0,
    }, {}


def querpruefung(werte: Mapping[str, object]) -> dict[str, str]:
    """Kombinationen, auf die sich keine Rechnung festschreiben laesst -- je Feld ein Grund.

    Dieselben ROT-Regeln wie beim Festschreiben (R18, R20, R21 in
    validierung.py), nur frueher: beim Anlegen. Das Formular zeigt sie als
    Hinweis -- die USt-IdNr. darf nachgetragen werden, bevor die erste
    Rechnung entsteht. Der Import legt solche Zeilen nicht an: eine Datei
    laesst sich korrigieren und erneut hochladen.

    >>> querpruefung({"land": "AT", "typ": "UNTERNEHMER", "leistungsort": "INLAND",
    ...               "ust_idnr": None})  # doctest: +ELLIPSIS
    {'leistungsort': 'Die Anschrift liegt in Österreich, der Leistungsort steht aber auf ...'}
    >>> querpruefung({"land": "AT", "typ": "PRIVAT", "leistungsort": "INLAND", "ust_idnr": None})
    {}
    """
    gruende: dict[str, str] = {}
    land, typ, ort = werte.get("land"), werte.get("typ"), werte.get("leistungsort")
    if ort == "EU_B2B_REVERSE_CHARGE" and typ == "PRIVAT":
        gruende["leistungsort"] = (
            "Reverse Charge setzt einen Unternehmer als Empfänger voraus (§ 13b UStG); der "
            "Kunde ist als Privatperson angelegt. Bitte Leistungsort oder Kundentyp berichtigen."
        )
    elif ort == "INLAND" and typ != "PRIVAT" and land and land != "DE":
        gruende["leistungsort"] = (
            f"Die Anschrift liegt in {LAENDER.get(str(land), land)}, der Leistungsort steht "
            "aber auf Inland. Bei einem Unternehmer im Ausland liegt er nach § 3a Abs. 2 UStG "
            "an dessen Sitz: EU-Unternehmer (Reverse Charge) oder Drittland."
        )
    if ort == "EU_B2B_REVERSE_CHARGE" and not werte.get("ust_idnr"):
        gruende["ust_idnr"] = (
            "Bei Reverse Charge ist die USt-IdNr. des Kunden Pflicht: sie belegt, dass er "
            "Unternehmer ist (§ 3a Abs. 2 UStG), und steht auf der Rechnung (§ 14a UStG). "
            "Ohne sie lässt sich keine Rechnung festschreiben."
        )
    return gruende


# ------------------------------------------------ Kunde loeschen, stilllegen

class KundeNichtLoeschbar(ValueError):
    """Der Kunde laesst sich (jetzt) nicht loeschen; die Meldung sagt, warum."""


@dataclass(frozen=True)
class Loeschbarkeit:
    """Was am Loeschen eines Kunden haengt -- fuer die Rueckfrage und die Pruefung.

    Loeschbar ist ein Kunde nur, solange er auf KEINEM festgeschriebenen
    Dokument steht (Rechnung, Storno, Leistungsnachweis, festgeschrieben
    oder storniert): § 147 AO bewahrt die Dokumente auf und mit ihnen den
    Stamm, auf den sie verweisen. Offene Zeiten und Entwuerfe sind keine
    Belege -- sie werden nach Rueckfrage mitgeloescht.
    """

    kunde: sqlite3.Row
    #: Nummern der Dokumente, die das Loeschen verhindern.
    belege: tuple[str, ...]
    #: Zeiten, die einmal auf einem festgeschriebenen Dokument lagen.
    zeiten_mit_geschichte: tuple[str, ...]
    #: Entwuerfe: id, netto_cent, positionen, betreff.
    entwuerfe: tuple[dict, ...]
    #: Alle Zeiten des Kunden (offen, abgeschrieben, intern, im Entwurf).
    zeiten: tuple[dict, ...]
    timer_laeuft: bool
    #: Nummern zurueckgenommener Rechnungen an diesen Kunden. Sie hindern
    #: das Loeschen nicht (keine gueltigen Belege), aber seine Kundennummer
    #: steht auf ihren aufbewahrten PDFs und bleibt deshalb verbraucht.
    zurueckgenommen: tuple[str, ...] = ()

    @property
    def loeschbar(self) -> bool:
        return not self.belege and not self.zeiten_mit_geschichte

    @property
    def stand(self) -> str:
        """Kennung dessen, was die Rueckfrage aufzaehlt.

        Kommt zwischen Rueckfrage und Bestaetigung eine Zeit oder ein Entwurf
        dazu, passt die Kennung nicht mehr -- geloescht wird nur, was der
        Nutzer gesehen hat.
        """
        teile = [str(z["id"]) for z in self.zeiten] + ["E"]
        teile += [str(e["id"]) for e in self.entwuerfe]
        return hashlib.sha256(",".join(teile).encode()).hexdigest()[:16]


def loeschbarkeit(conn: sqlite3.Connection, kunde_id: int) -> Loeschbarkeit | None:
    """Was am Loeschen haengt; None, wenn es den Kunden nicht gibt."""
    kunde = conn.execute("SELECT * FROM kunde WHERE id = ?", (kunde_id,)).fetchone()
    if kunde is None:
        return None
    belege = tuple(
        z["nummer"] or f"{z['typ'].title()} {z['id']}"
        for z in conn.execute(
            "SELECT id, nummer, typ FROM rechnung WHERE kunde_id = ? AND status <> 'ENTWURF'"
            " ORDER BY id", (kunde_id,))
    )
    geschichte = tuple(
        f"{z['datum']} {z['titel']}"
        for z in conn.execute(
            """
            SELECT z.datum, z.titel FROM zeiteintrag z
             WHERE z.kunde_id = ?
               AND (z.freigegeben_aus_rechnung_id IS NOT NULL
                    OR EXISTS (SELECT 1 FROM rechnung r
                                WHERE r.id = z.rechnung_id AND r.status <> 'ENTWURF')
                    OR EXISTS (SELECT 1 FROM position_zeiteintrag pz
                                 JOIN rechnungsposition p ON p.id = pz.position_id
                                 JOIN rechnung r ON r.id = p.rechnung_id
                                WHERE pz.zeiteintrag_id = z.id AND r.status <> 'ENTWURF'))
             ORDER BY z.datum, z.id
            """, (kunde_id,))
    )
    entwuerfe = tuple(dict(z) for z in conn.execute(
        """
        SELECT r.id, r.netto_cent, r.betreff, r.erstellt_am,
               (SELECT COUNT(*) FROM rechnungsposition p WHERE p.rechnung_id = r.id)
                   AS positionen
          FROM rechnung r WHERE r.kunde_id = ? AND r.status = 'ENTWURF' ORDER BY r.id
        """, (kunde_id,)))
    zeiten = tuple(dict(z) for z in conn.execute(
        """
        SELECT z.id, z.datum, z.titel, z.dauer_minuten, z.abrechnungsmodus, z.start_zeit,
               z.end_zeit, z.rechnung_id, z.abgeschrieben_am, s.status
          FROM zeiteintrag z JOIN v_zeiteintrag_status s ON s.id = z.id
         WHERE z.kunde_id = ? ORDER BY z.datum, z.id
        """, (kunde_id,)))
    timer = any(z["start_zeit"] and not z["end_zeit"] for z in zeiten)
    zurueckgenommen = tuple(dict.fromkeys(
        str(z["nummer"]) for z in conn.execute(
            "SELECT json_extract(wert_neu, '$.nummer') AS nummer FROM journal"
            " WHERE aktion = 'RECHNUNG_ZURUECKGENOMMEN'"
            "   AND json_extract(wert_alt, '$.kopf.kunde_id') = ? ORDER BY id", (kunde_id,))
        if z["nummer"]))
    return Loeschbarkeit(kunde, belege, geschichte, entwuerfe, zeiten, timer, zurueckgenommen)


def nicht_loeschbar_text(stand: Loeschbarkeit) -> str:
    """Warum ein Kunde nicht geloescht werden kann -- und was stattdessen geht."""
    if stand.belege:
        anzahl = len(stand.belege)
        nummern = ", ".join(stand.belege[:5]) + (" …" if anzahl > 5 else "")
        dokumente = ("einem festgeschriebenen Dokument" if anzahl == 1
                     else f"{anzahl} festgeschriebenen Dokumenten")
        grund = f"Der Kunde steht auf {dokumente} ({nummern})."
    else:
        grund = ("Zeiten des Kunden lagen auf einer festgeschriebenen Rechnung "
                 f"({', '.join(stand.zeiten_mit_geschichte[:3])}).")
    return (
        f"{grund} Rechnungen und Stornos werden zehn Jahre aufbewahrt (§ 147 AO), und mit "
        "ihnen der Kunde, auf den sie verweisen. Stattdessen stilllegen: er verschwindet "
        "aus Kundenliste und Auswahl, seine Rechnungen bleiben."
    )


def loeschen(conn: sqlite3.Connection, kunde_id: int, *, stand: str | None = None,
             grund: str | None = None) -> Loeschbarkeit | None:
    """Loescht einen Kunden samt Zeiten und Entwuerfen -- endgueltig, in EINER Transaktion.

    Gibt zurueck, was geloescht wurde; None, wenn es den Kunden schon nicht
    mehr gibt (Doppelklick, zweiter Tab: das Ziel ist erreicht, und das
    unveraenderliche Journal bekommt keinen zweiten Eintrag). Die
    Loeschbarkeit wird unter der Schreibsperre noch einmal geprueft; mit
    ``stand`` (siehe :attr:`Loeschbarkeit.stand`) auch, ob noch genau das
    geloescht wird, was die Rueckfrage aufgezaehlt hat.

    Das Journal haelt fest, was verschwand: die vollstaendige Kundenzeile,
    jede Zeit (Datum, Titel, Minuten, Modus, Status) und jeder Entwurf mit
    Betrag -- § 146 Abs. 4 AO verlangt, dass der urspruengliche Inhalt
    feststellbar bleibt. Die Entwuerfe bekommen dazu ihr eigenes
    ENTWURF_VERWORFEN.
    """
    from app.logik import festschreiben

    conn.execute("BEGIN IMMEDIATE")
    try:
        jetzt = loeschbarkeit(conn, kunde_id)
        if jetzt is None:
            conn.execute("COMMIT")
            return None
        if not jetzt.loeschbar:
            raise KundeNichtLoeschbar(nicht_loeschbar_text(jetzt))
        if jetzt.timer_laeuft:
            raise KundeNichtLoeschbar(
                "Für diesen Kunden läuft ein Timer. Bitte erst stoppen (oder verwerfen), "
                "dann löschen."
            )
        if stand is not None and stand != jetzt.stand:
            raise KundeNichtLoeschbar(
                "Seit der Rückfrage sind Zeiten oder Entwürfe dazugekommen oder weggefallen. "
                "Gelöscht wurde nichts — bitte die Aufzählung prüfen und erneut bestätigen."
            )
        for entwurf in jetzt.entwuerfe:
            festschreiben.entwurf_verwerfen(conn, entwurf["id"],
                                            begruendung="Mit dem Kunden gelöscht")
        conn.execute("DELETE FROM zeiteintrag WHERE kunde_id = ?", (kunde_id,))
        if conn.execute("DELETE FROM kunde WHERE id = ?", (kunde_id,)).rowcount == 1:
            db.protokolliere(
                conn, "LOESCHEN", "kunde", kunde_id,
                wert_alt={
                    "kunde": dict(jetzt.kunde),
                    "zeiten": [{s: z[s] for s in ("id", "datum", "titel", "dauer_minuten",
                                                  "abrechnungsmodus", "status")}
                               for z in jetzt.zeiten],
                    "entwuerfe": [{s: e[s] for s in ("id", "netto_cent", "positionen",
                                                     "betreff")}
                                  for e in jetzt.entwuerfe],
                },
                begruendung=" ".join((grund or "").split()) or None,
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return jetzt


def stilllegen(conn: sqlite3.Connection, kunde_id: int, aktiv: bool) -> bool | None:
    """Legt einen Kunden still (``aktiv=False``) oder wieder an; True, wenn sich etwas aenderte.

    None, wenn es den Kunden nicht gibt. Die Aenderung steht im Journal wie
    jede andere am Kundenstamm (AENDERN, Feld ``aktiv``).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        zeile = conn.execute("SELECT aktiv FROM kunde WHERE id = ?", (kunde_id,)).fetchone()
        if zeile is None:
            conn.execute("COMMIT")
            return None
        neu = 1 if aktiv else 0
        if zeile["aktiv"] == neu:
            conn.execute("COMMIT")
            return False
        conn.execute("UPDATE kunde SET aktiv = ? WHERE id = ?", (neu, kunde_id))
        db.protokolliere(conn, "AENDERN", "kunde", kunde_id, feld="aktiv",
                         wert_alt=zeile["aktiv"], wert_neu=neu)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True

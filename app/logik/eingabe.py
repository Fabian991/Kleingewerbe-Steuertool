"""Gemeinsame Prüfung von Formular- und Adresswerten: Datum und Ganzzahl.

Die Gegenprobe nach der Fehleranalyse fand dieselben drei Lücken an vielen
Stellen: ein Datum wie "20261018" wurde angenommen, aber roh gespeichert (und
fiel dann aus jeder Jahresauswertung, weil SQLites strftime() es nicht liest);
ein zweistellig getipptes Jahr ("0026") landete als Jahr 26 in der EÜR; und
eine Zahl ab 2**63 sprengte die Bindung an SQLite mit einem OverflowError.
Jede Route hatte ihre eigene, leicht andere Prüfung. Hier steht sie einmal.
"""

from __future__ import annotations

import re
from datetime import date

#: Kleinstes plausibles Jahr. Davor liegt bei diesem Werkzeug nur ein
#: Tippfehler im Jahr ("0026" statt "2026").
JAHR_MINDESTENS = 2000

#: Groesster Wert, den SQLite als INTEGER binden kann.
ID_HOECHSTENS = 2**63 - 1

#: C0-Steuerzeichen ausser Tab (09), Zeilenumbruch (0A) und Wagenruecklauf
#: (0D), dazu DEL (7F). Die Middleware in main.py weist Formulare damit ab;
#: der Kundenimport prueft damit jede Zelle -- Datei-Uploads laufen an der
#: Middleware vorbei.
STEUERZEICHEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


#: Deutsche Schreibweise TT.MM.JJJJ, auch ohne fuehrende Null (T.M.JJJJ).
#: Nur ASCII-Ziffern: "٣" ist fuer ``\d`` eine Ziffer und fuer ``int()`` eine 3
#: -- als Datum tippt es niemand.
_DEUTSCHES_DATUM = re.compile(r"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})")


def datum(text: str | None, *, feld: str = "Datum", zukunft: bool = False,
          heute: date | None = None) -> str:
    """Liest ein Datum und gibt es als JJJJ-MM-TT zurück.

    Das Kalenderfeld des Browsers sendet JJJJ-MM-TT. Faellt es auf ein
    Textfeld zurueck (aeltere Browser, eingefuegter Text), tippt der Nutzer
    TT.MM.JJJJ -- das Format, das die Meldung selbst nennt. Vorher verlangte
    sie TT.MM.JJJJ und wies genau das ab. Beides wird angenommen.

    ``date.fromisoformat`` nimmt seit Python 3.11 auch "20261018" und
    Wochenformate an; gespeichert wird deshalb immer das GELESENE Datum, nie
    der Rohtext. Ohne ``zukunft=True`` ist ein Tag nach heute ein Fehler; mit
    ``zukunft=True`` ist höchstens das nächste Kalenderjahr erlaubt.

    >>> datum("2026-03-10", heute=date(2026, 10, 19))
    '2026-03-10'
    >>> datum("20260310", heute=date(2026, 10, 19))
    '2026-03-10'
    >>> datum("0026-03-10", heute=date(2026, 10, 19))
    Traceback (most recent call last):
    ValueError: Datum: „0026-03-10“ liegt vor dem Jahr 2000 — vermutlich ein Tippfehler im Jahr.
    >>> datum("2026-10-20", feld="Belegdatum", heute=date(2026, 10, 19))
    Traceback (most recent call last):
    ValueError: Belegdatum: der 20.10.2026 liegt in der Zukunft.
    >>> datum("2027-12-31", zukunft=True, heute=date(2026, 10, 19))
    '2027-12-31'
    >>> datum("31.01.2026", heute=date(2026, 10, 19)), datum("1.3.2026", heute=date(2026, 10, 19))
    ('2026-01-31', '2026-03-01')
    >>> datum("31.02.2026", heute=date(2026, 10, 19))
    Traceback (most recent call last):
    ValueError: Datum: „31.02.2026“ ist kein Datum (erwartet wird TT.MM.JJJJ, z. B. 31.01.2026).
    >>> datum("10.03.0026", heute=date(2026, 10, 19))
    Traceback (most recent call last):
    ValueError: Datum: „10.03.0026“ liegt vor dem Jahr 2000 — vermutlich ein Tippfehler im Jahr.
    >>> datum("10.03.26", heute=date(2026, 10, 19))
    Traceback (most recent call last):
    ValueError: Datum: „10.03.26“ ist kein Datum (erwartet wird TT.MM.JJJJ, z. B. 31.01.2026).
    >>> datum("  ", feld="Zahlungsdatum")
    Traceback (most recent call last):
    ValueError: Zahlungsdatum fehlt.
    """
    roh = (text or "").strip()
    if not roh:
        raise ValueError(f"{feld} fehlt.")
    deutsch = _DEUTSCHES_DATUM.fullmatch(roh)
    try:
        if deutsch:
            tag_, monat, jahr = (int(teil) for teil in deutsch.groups())
            tag = date(jahr, monat, tag_)
        else:
            tag = date.fromisoformat(roh)
    except ValueError:
        raise ValueError(
            f"{feld}: „{roh[:40]}“ ist kein Datum (erwartet wird TT.MM.JJJJ, z. B. 31.01.2026)."
        ) from None
    heute = heute or date.today()
    if tag.year < JAHR_MINDESTENS:
        raise ValueError(
            f"{feld}: „{roh[:40]}“ liegt vor dem Jahr {JAHR_MINDESTENS} — "
            "vermutlich ein Tippfehler im Jahr."
        )
    if zukunft:
        if tag.year > heute.year + 1:
            raise ValueError(f"{feld}: der {tag:%d.%m.%Y} liegt zu weit in der Zukunft.")
    elif tag > heute:
        raise ValueError(f"{feld}: der {tag:%d.%m.%Y} liegt in der Zukunft.")
    return tag.isoformat()


def ganzzahl(text: str | None, *, feld: str, mindestens: int | None = None,
             hoechstens: int = ID_HOECHSTENS) -> int:
    """Liest eine ganze Zahl aus einem Formularfeld, mit Grenzen.

    Nur ASCII-Ziffern: ``"²".isdigit()`` ist in Python wahr, ``int("²")``
    scheitert trotzdem.

    >>> ganzzahl("42", feld="Kilometer", mindestens=1)
    42
    >>> ganzzahl("²", feld="Kilometer")
    Traceback (most recent call last):
    ValueError: Kilometer: „²“ ist keine ganze Zahl.
    >>> ganzzahl("99999999999999999999", feld="Kilometer")
    Traceback (most recent call last):
    ValueError: Kilometer: 99999999999999999999 ist unplausibel groß.
    >>> ganzzahl("0", feld="Nutzungsdauer", mindestens=1, hoechstens=50)
    Traceback (most recent call last):
    ValueError: Nutzungsdauer: 0 ist zu klein (mindestens 1).
    """
    roh = (text or "").strip()
    ziffern = roh[1:] if roh[:1] in "+-" else roh
    if not ziffern or not (ziffern.isascii() and ziffern.isdigit()):
        raise ValueError(f"{feld}: „{roh[:40]}“ ist keine ganze Zahl.")
    if len(ziffern) > 19:
        raise ValueError(f"{feld}: {roh[:40]} ist unplausibel groß.")
    wert = int(roh)
    if wert > hoechstens:
        raise ValueError(
            f"{feld}: {roh[:40]} ist unplausibel groß."
            if hoechstens == ID_HOECHSTENS else
            f"{feld}: {wert} ist zu groß (höchstens {hoechstens})."
        )
    if mindestens is not None and wert < mindestens:
        raise ValueError(f"{feld}: {wert} ist zu klein (mindestens {mindestens}).")
    return wert


#: Ein Prozentsatz: ganze Zahl, wahlweise mit Komma oder Punkt und Nachkommastellen.
_PROZENT = re.compile(r"([0-9]{1,3})(?:[.,]([0-9]{1,4}))?")


def prozent(text: str | None, *, feld: str, hoechstens: int = 100) -> int | None:
    """Liest einen Prozentsatz als Promille (Zehntelprozent); leer ergibt ``None``.

    Komma oder Punkt, mit oder ohne Prozentzeichen. Mehr als eine
    Nachkommastelle wird kaufmännisch auf ein Zehntelprozent gerundet --
    genauer kennt ihn niemand. Nur ASCII-Ziffern, kein Vorzeichen, keine
    Exponentschreibweise.

    >>> prozent("35", feld="Steuersatz"), prozent(" 32,5 % ", feld="Steuersatz")
    (350, 325)
    >>> prozent("12.25", feld="Steuersatz"), prozent("0", feld="Steuersatz"), prozent("", feld="x")
    (123, 0, None)
    >>> for falsch in ("abc", "-5", "1e3", "٣٠", "101", "30,5,1"):
    ...     try:
    ...         prozent(falsch, feld="Steuersatz")
    ...     except ValueError as fehler:
    ...         print(fehler)
    Steuersatz: „abc“ ist kein Prozentsatz (z. B. 30 oder 32,5).
    Steuersatz: „-5“ ist kein Prozentsatz (z. B. 30 oder 32,5).
    Steuersatz: „1e3“ ist kein Prozentsatz (z. B. 30 oder 32,5).
    Steuersatz: „٣٠“ ist kein Prozentsatz (z. B. 30 oder 32,5).
    Steuersatz: 101 % ist zu viel (höchstens 100 %).
    Steuersatz: „30,5,1“ ist kein Prozentsatz (z. B. 30 oder 32,5).
    """
    roh = (text or "").strip()
    kompakt = "".join(roh.replace("%", "").split())
    if not kompakt:
        return None
    treffer = _PROZENT.fullmatch(kompakt)
    if not treffer or not kompakt.isascii():
        raise ValueError(f"{feld}: „{roh[:40]}“ ist kein Prozentsatz (z. B. 30 oder 32,5).")
    ganz, bruch = treffer.group(1), treffer.group(2) or ""
    # Exakt in Zehntausendstelprozent, dann kaufmaennisch auf Zehntelprozent.
    feinst = int(ganz) * 10000 + int((bruch + "0000")[:4])
    promille = (feinst + 500) // 1000
    if promille > hoechstens * 10:
        gezeigt = ganz if not bruch else f"{ganz},{bruch}"
        raise ValueError(f"{feld}: {gezeigt} % ist zu viel (höchstens {hoechstens} %).")
    return promille


#: So sieht eine IBAN aus, die jede Meldung als Beispiel nennt.
IBAN_BEISPIEL = "DE02 1203 0000 0000 2020 51"
#: Laenge einer IBAN nach ISO 13616: 15 (Norwegen) bis 34 Zeichen.
IBAN_LAENGE = (15, 34)
#: Laenderlaengen, die geprueft werden. Der Betrieb sitzt im Inland; eine
#: deutsche IBAN hat immer 22 Zeichen, eine Stelle mehr oder weniger ist ein
#: Tippfehler, den die Pruefsumme nicht immer faengt.
IBAN_LAENGE_JE_LAND = {"DE": 22}


def iban(text: str | None) -> str | None:
    """Prüft eine IBAN und gibt sie kompakt zurück (ohne Leerzeichen, groß).

    Leer ist erlaubt (``None``): die Bankverbindung ist keine Pflichtangabe.
    Sonst gilt ISO 13616: Länderkürzel, zwei Prüfziffern, danach nur Buchstaben
    und Ziffern, 15 bis 34 Zeichen (eine deutsche genau 22), und die
    Prüfsumme modulo 97 ergibt 1. Gespeichert wird die kompakte Form; die
    Rechnung druckt sie über den Filter ``iban`` in Vierergruppen.

    >>> iban(" de02 1203 0000 0000 2020 51 "), iban(""), iban(None)
    ('DE02120300000000202051', None, None)
    >>> iban("AT61 1904 3002 3457 3201"), iban("NL91ABNA0417164300")
    ('AT611904300234573201', 'NL91ABNA0417164300')
    >>> for falsch in ("DE00 1234", "DE03 1203 0000 0000 2020 51", "x" * 100, "FR76 3000 6",
    ...                "DE٠٢120300000000202051"):
    ...     try:
    ...         iban(falsch)
    ...     except ValueError as fehler:
    ...         print(fehler)
    Die IBAN „DE00 1234“ hat 8 Zeichen; eine deutsche IBAN hat genau 22, \
z. B. DE02 1203 0000 0000 2020 51.
    Die IBAN „DE03 1203 0000 0000 2020 51“ ist ungültig: die Prüfziffern passen nicht \
— vermutlich ein Tippfehler. Bitte mit dem Kontoauszug vergleichen.
    „xxxxxxxxxx…“ ist keine IBAN. Sie beginnt mit dem Länderkürzel und zwei Prüfziffern, \
z. B. DE02 1203 0000 0000 2020 51.
    Die IBAN „FR76 3000 6“ hat 9 Zeichen; eine IBAN hat 15 bis 34, z. B. DE02 1203 0000 \
0000 2020 51.
    „DE٠٢120300000000202051“ ist keine IBAN. Sie beginnt mit dem Länderkürzel und zwei \
Prüfziffern, z. B. DE02 1203 0000 0000 2020 51.
    """
    roh = " ".join((text or "").split())
    if not roh:
        return None
    gezeigt = roh if len(roh) <= 40 else roh[:10] + "…"
    kompakt = roh.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", kompakt, flags=re.ASCII) or (
        not kompakt.isascii()
    ):
        raise ValueError(
            f"„{gezeigt}“ ist keine IBAN. Sie beginnt mit dem Länderkürzel und zwei "
            f"Prüfziffern, z. B. {IBAN_BEISPIEL}."
        )
    land = kompakt[:2]
    soll = IBAN_LAENGE_JE_LAND.get(land)
    if soll is not None and len(kompakt) != soll:
        raise ValueError(
            f"Die IBAN „{gezeigt}“ hat {len(kompakt)} Zeichen; eine deutsche IBAN hat "
            f"genau {soll}, z. B. {IBAN_BEISPIEL}."
        )
    if not IBAN_LAENGE[0] <= len(kompakt) <= IBAN_LAENGE[1]:
        raise ValueError(
            f"Die IBAN „{gezeigt}“ hat {len(kompakt)} Zeichen; eine IBAN hat "
            f"{IBAN_LAENGE[0]} bis {IBAN_LAENGE[1]}, z. B. {IBAN_BEISPIEL}."
        )
    # ISO 7064 MOD 97-10: die ersten vier Zeichen ans Ende, Buchstaben als
    # Zahlen (A = 10 … Z = 35), der Rest der Division durch 97 muss 1 sein.
    zahl = int("".join(str(int(z, 36)) for z in kompakt[4:] + kompakt[:4]))
    if zahl % 97 != 1:
        raise ValueError(
            f"Die IBAN „{gezeigt}“ ist ungültig: die Prüfziffern passen nicht — "
            "vermutlich ein Tippfehler. Bitte mit dem Kontoauszug vergleichen."
        )
    return kompakt


def bic(text: str | None) -> str | None:
    """Prüft eine BIC und gibt sie kompakt und groß zurück; leer ergibt None.

    8 oder 11 Zeichen: Bankkürzel (4 Buchstaben), Land (2 Buchstaben), Ort
    (2 Zeichen) und wahlweise die Filiale (3 Zeichen).

    >>> bic("byladem1001"), bic("COBA DE FF"), bic("  ")
    ('BYLADEM1001', 'COBADEFF', None)
    >>> bic("XYZ")
    Traceback (most recent call last):
    ValueError: „XYZ“ ist keine BIC. Eine BIC hat 8 oder 11 Zeichen, z. B. BYLADEM1001 oder \
COBADEFFXXX.
    """
    roh = " ".join((text or "").split())
    if not roh:
        return None
    kompakt = roh.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?", kompakt, flags=re.ASCII):
        raise ValueError(
            f"„{roh[:40]}“ ist keine BIC. Eine BIC hat 8 oder 11 Zeichen, "
            "z. B. BYLADEM1001 oder COBADEFFXXX."
        )
    return kompakt


def kennung(text: str | None) -> int | None:
    """Eine Datensatz-Kennung aus Formular oder Adresse; Unbrauchbares ergibt None.

    >>> kennung("17"), kennung(""), kennung("abc"), kennung("²"), kennung(str(2**63))
    (17, None, None, None, None)
    """
    try:
        return ganzzahl(text, feld="Kennung", mindestens=1)
    except ValueError:
        return None

"""Zeiterfassung: Dauer einlesen, anzeigen, Timer.

Die Eingabe muss alles schlucken, was man unterwegs tippt, sonst wird am Tool
vorbeigearbeitet -- und ein Werkzeug, in das nicht erfasst wird, loest das
Ausgangsproblem gar nicht. Gespeichert werden ausschliesslich ganze Minuten und
immer UNGERUNDET; gerundet wird erst je Rechnungsposition.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, tzinfo
from decimal import ROUND_HALF_UP, Decimal

from app.logik import eingabe as eingabepruefung

#: Hoechstdauer eines einzelnen Eintrags. Mehr als ein voller Tag ist an einem
#: Datum nicht moeglich -- und eine Zahl wie 99999999999999999999 passt nicht
#: einmal in die Datenbank (OverflowError, vorher ein HTTP 500).
MAX_MINUTEN = 24 * 60

# Reihenfolge ist Rangfolge: das erste passende Muster gewinnt.
_MUSTER = (
    # 1h45, 1 h 45 m, 1h45min
    re.compile(r"^(?P<h>\d+)\s*(?:h|std|stunden?)\s*(?P<m>\d{1,2})\s*(?:m|min|minuten?)?$"),
    # 1:45, 0:30
    re.compile(r"^(?P<h>\d+):(?P<m>[0-5]?\d)$"),
    # 1,75h oder 1.75 h  -> Dezimalstunden mit Einheit
    re.compile(r"^(?P<dez>\d+[.,]\d+)\s*(?:h|std|stunden?)$"),
    # 1h, 2 Std
    re.compile(r"^(?P<h>\d+)\s*(?:h|std|stunden?)$"),
    # 105m, 90 min
    re.compile(r"^(?P<m>\d+)\s*(?:m|min|minuten?)$"),
    # 1,75 oder 1.75 ohne Einheit -> Dezimalstunden
    re.compile(r"^(?P<dez>\d+[.,]\d+)$"),
    # 105 ohne Einheit -> Minuten
    re.compile(r"^(?P<m>\d+)$"),
)


class DauerUngueltig(ValueError):
    """Die Eingabe liess sich nicht als Dauer lesen."""


def parse_dauer(eingabe: str) -> int:
    """Liest eine Dauerangabe als ganze Minuten.

    Erkannt werden ``1:45``, ``1h45``, ``1,75``, ``1,75h``, ``105``, ``105min``
    und ``2 Std``. Eine blanke Ganzzahl gilt als MINUTEN -- wer Stunden meint,
    schreibt ``2h`` oder ``2,0``.

    >>> parse_dauer("1:45"), parse_dauer("1,75"), parse_dauer("105")
    (105, 105, 105)
    """
    if eingabe is None:
        raise DauerUngueltig("Keine Dauer angegeben")
    text = eingabe.strip().lower().replace(" ", " ")
    if not text:
        raise DauerUngueltig("Keine Dauer angegeben")

    # Die Meldungen zeigen die Eingabe in deutschen Anfuehrungszeichen, nicht
    # als Python-repr: 'abc' liest ein Nutzer als Tippfehler des Programms.
    gezeigt = eingabe.strip()
    if len(gezeigt) > 40:
        # Die Meldung wandert in eine URL; ein eingefuegter Roman gehoert da nicht hin.
        gezeigt = gezeigt[:40] + "…"
    for muster in _MUSTER:
        treffer = muster.match(text)
        if not treffer:
            continue
        teile = treffer.groupdict()
        try:
            if teile.get("dez"):
                stunden = Decimal(teile["dez"].replace(",", "."))
                minuten = int((stunden * 60).to_integral_value(rounding=ROUND_HALF_UP))
            else:
                minuten = int(teile.get("h") or 0) * 60 + int(teile.get("m") or 0)
        except (ValueError, ArithmeticError):
            # Tausende Ziffern: Python verweigert die Umwandlung. Keine Dauer.
            break
        if minuten <= 0:
            raise DauerUngueltig(f"Die Dauer muss größer als null sein: „{gezeigt}“")
        if minuten > MAX_MINUTEN:
            raise DauerUngueltig(
                f"Die Dauer darf höchstens 24 Stunden je Eintrag betragen: „{gezeigt}“"
            )
        return minuten

    raise DauerUngueltig(
        f"„{gezeigt}“ ist keine Dauer. Möglich sind etwa 1:45, 1h45, 1,75, 105 oder 90min."
    )


class DatumUngueltig(ValueError):
    """Die Eingabe liess sich nicht als Datum eines Zeiteintrags lesen."""


def pruefe_datum(eingabe: str | None, *, heute: str | None = None) -> str:
    """Prueft das Datum eines Zeiteintrags und gibt es als ISO-Datum zurueck.

    Angenommen werden ``JJJJ-MM-TT`` -- das Format, das ein Datumsfeld im
    Browser sendet -- und ``TT.MM.JJJJ`` bzw. ``T.M.JJJJ``, wie es jemand tippt,
    wenn das Kalenderfeld auf ein Textfeld zurueckfaellt. Gespeichert wird
    immer ISO. Alles andere landete vorher roh in der Datenbank, die Liste
    zeigte es unformatiert, und der Rechnungsassistent stuerzte beim Lesen ab.
    Ein Datum in der Zukunft wird abgewiesen: gearbeitet wird nicht im Voraus,
    und ein Tippfehler im Jahr faellt sonst erst auf der Rechnung auf.

    >>> pruefe_datum("2026-03-02", heute="2026-03-05")
    '2026-03-02'
    >>> pruefe_datum("2026-03-05", heute="2026-03-05")
    '2026-03-05'
    >>> pruefe_datum("2.3.2026", heute="2026-03-05"), pruefe_datum("02.03.2026", heute="2026-03-05")
    ('2026-03-02', '2026-03-02')
    >>> try:
    ...     pruefe_datum("0026-03-05", heute="2026-03-05")
    ... except DatumUngueltig as fehler:
    ...     print(fehler)
    Datum: „0026-03-05“ liegt vor dem Jahr 2000 — vermutlich ein Tippfehler im Jahr.
    """
    text = (eingabe or "").strip()
    if not text:
        raise DatumUngueltig("Das Datum fehlt.")
    deutsch = re.fullmatch(r"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})", text)
    if not deutsch and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise DatumUngueltig(
            f"„{text}“ ist kein Datum. Erwartet wird TT.MM.JJJJ, z. B. 02.03.2026."
        )
    try:
        if deutsch:
            tt, mm, jjjj = (int(teil) for teil in deutsch.groups())
            tag = date(jjjj, mm, tt)
            text = tag.isoformat()
        else:
            tag = date.fromisoformat(text)
    except ValueError:
        raise DatumUngueltig(f"„{text}“ ist kein gültiges Datum.") from None
    grenze = date.fromisoformat(heute) if heute else date.today()
    if tag > grenze:
        raise DatumUngueltig(
            f"Das Datum {tag.strftime('%d.%m.%Y')} liegt in der Zukunft."
        )
    # Das gemeinsame Datumsfenster: ein zweistellig getipptes Jahr ("0026")
    # landete sonst als Jahr 26 in der Liste und fiel aus jeder Auswertung.
    try:
        return eingabepruefung.datum(text, feld="Datum", heute=grenze)
    except ValueError as fehler:
        raise DatumUngueltig(str(fehler)) from None


#: Hoechstlaenge einer Beschreibung. Das reicht fuer eine ausfuehrliche
#: Aufstellung dessen, was getan wurde; mehr gehoert in ein Angebot oder einen
#: Anhang. Ohne Grenze liess sich per Einfuegen ein Roman speichern, der auf der
#: Rechnung und im Einzelnachweis Seiten fuellt.
BESCHREIBUNG_MAX = 2000


class BeschreibungUngueltig(ValueError):
    """Die Beschreibung ist zu lang."""


def pruefe_beschreibung(eingabe: str | None) -> str | None:
    r"""Normalisiert eine Beschreibung und prueft ihre Laenge.

    Zeilenumbrueche kommen aus dem Browser als CRLF; gespeichert wird LF,
    damit Vergleich, Entdoppeln und Laenge nicht am Zeilenende haengen.
    Mehr als eine Leerzeile am Stueck wird zu einer, Leerraum am Rand und am
    Zeilenende faellt weg. Leer ergibt None. Gemessen wird NACH dem
    Normalisieren -- so zaehlt auch der Browser (``maxlength``).

    >>> pruefe_beschreibung("  Zeile 1\r\n\r\n\r\nZeile 2  \r\n")
    'Zeile 1\n\nZeile 2'
    >>> pruefe_beschreibung("   ") is None
    True
    """
    text = (eingabe or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(zeile.rstrip() for zeile in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > BESCHREIBUNG_MAX:
        raise BeschreibungUngueltig(
            f"Die Beschreibung ist {len(text):,} Zeichen lang — höchstens "
            f"{BESCHREIBUNG_MAX:,} sind möglich. Längeres gehört in ein Angebot "
            f"oder einen Anhang.".replace(",", ".")
        )
    return text or None


def formatiere_dauer(minuten: int) -> str:
    """Minuten als ``1:45``."""
    if minuten < 0:
        raise ValueError("Negative Dauer ist nicht vorgesehen")
    return f"{minuten // 60}:{minuten % 60:02d}"


def dezimalstunden(minuten: int) -> str:
    """Minuten als deutsche Dezimalstunde, z. B. ``1,75``."""
    stunden = (Decimal(minuten) / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{stunden:.2f}".replace(".", ",")


def minuten_zwischen(start: str, ende: str) -> int:
    """Minuten zwischen zwei Uhrzeiten am selben Tag, mindestens eine.

    Akzeptiert ``HH:MM`` und ``HH:MM:SS``. Eine angefangene Minute zaehlt als
    eine Minute -- ein Timer, der nach 20 Sekunden gestoppt wird, ergibt also
    einen Eintrag ueber eine Minute und nicht ueber null. Das Schema verlangt
    eine Dauer groesser null, und ein Eintrag ueber null Minuten wuerde ohnehin
    nichts aussagen.
    """
    a, b = (time.fromisoformat(t) for t in (start, ende))
    sekunden = (b.hour * 3600 + b.minute * 60 + b.second) - (
        a.hour * 3600 + a.minute * 60 + a.second
    )
    if sekunden < 0:
        raise DauerUngueltig("Die Endzeit muss nach der Startzeit liegen")
    return max(1, round(sekunden / 60))


def timer_minuten(datum: str, start_zeit: str, jetzt: datetime) -> int:
    """Laufzeit eines Timers in Minuten, ueber Tagesgrenzen hinweg, mindestens eine.

    Ein gestern um 23:00 gestarteter Timer, der heute um 10:44 gestoppt wird,
    lief 704 Minuten -- nicht eine, wie der reine Uhrzeitvergleich ergab. Ein
    Start in der Zukunft (verstellte Uhr) oder ein kaputter Wert ergibt eine
    Minute statt eines Fehlers: nicht raten, aber auch nicht abstuerzen.

    >>> from datetime import datetime
    >>> timer_minuten("2026-10-17", "23:00:00", datetime(2026, 10, 18, 10, 44))
    704
    >>> timer_minuten("2026-10-18", "09:00:00", datetime(2026, 10, 18, 9, 0, 20))
    1
    """
    try:
        start = datetime.fromisoformat(f"{datum}T{start_zeit}")
    except ValueError:
        return 1
    return max(1, round((jetzt - start).total_seconds() / 60))


#: Das Ende eines Eintrags, der ueber seinen Tag hinauslief. Das Schema kennt
#: kein Enddatum und verlangt ``end_zeit >= start_zeit``; die echte Uhrzeit des
#: Folgetags laesst sich also nicht speichern. Die Dauer bleibt die fuehrende
#: Groesse, ``23:59:59`` sagt nur: "lief bis ueber das Tagesende".
TAGESENDE = "23:59:59"


def end_zeit(start_zeit: str | None, minuten: int) -> str | None:
    """Die Endzeit zu Beginn und Dauer, am Tag des Eintrags.

    Gebraucht, wenn die Dauer von Hand geaendert wird: vorher blieb die alte
    Endzeit stehen, und Excel-Export und Sicherung zeigten „Beginn 00:00:01,
    Ende 20:50, Dauer 480 Minuten“. Reicht die Dauer ueber Mitternacht, steht
    :data:`TAGESENDE`. Das Format folgt dem Beginn (mit oder ohne Sekunden).
    Ein Beginn, der sich nicht lesen laesst, ergibt None -- nicht raten.

    >>> end_zeit("00:00:01", 480), end_zeit("09:15", 105), end_zeit("23:00:00", 120)
    ('08:00:01', '11:00', '23:59:59')
    >>> end_zeit("00:00:00", 24 * 60), end_zeit(None, 60), end_zeit("kaputt", 60)
    ('23:59:59', None, None)
    """
    if not start_zeit:
        return None
    try:
        beginn = time.fromisoformat(start_zeit)
    except ValueError:
        return None
    sekunden = beginn.hour * 3600 + beginn.minute * 60 + beginn.second + minuten * 60
    if sekunden >= 24 * 3600:
        return TAGESENDE
    stunde, rest = divmod(sekunden, 3600)
    minute, sekunde = divmod(rest, 60)
    if start_zeit.count(":") == 1:
        return f"{stunde:02d}:{minute:02d}"
    return f"{stunde:02d}:{minute:02d}:{sekunde:02d}"


def timer_ende(datum: str, start_zeit: str, jetzt: datetime) -> str:
    """Die Endzeit beim Stoppen eines Timers.

    Am Tag des Starts ist es die Uhrzeit des Stoppens. Lief der Timer ueber
    seinen Tag hinaus, ist es :data:`TAGESENDE` -- und zwar nach dem DATUM
    entschieden, nicht nach der Uhrzeit: ein gestern um 00:00:01 gestarteter
    und heute um 20:50 gestoppter Timer bekam vorher "20:50:07" als Ende, bei
    2690 Minuten Dauer. Das las sich wie ein Arbeitstag von 0 bis 21 Uhr.

    >>> timer_ende("2026-10-18", "09:00:00", datetime(2026, 10, 18, 10, 30, 5))
    '10:30:05'
    >>> timer_ende("2026-10-17", "00:00:01", datetime(2026, 10, 18, 20, 50, 7))
    '23:59:59'
    >>> timer_ende("2026-10-17", "23:00:00", datetime(2026, 10, 18, 10, 44))
    '23:59:59'
    """
    ende = jetzt.strftime("%H:%M:%S")
    if jetzt.date().isoformat() != datum or ende < start_zeit:
        return TAGESENDE
    return ende


# ------------------------------------------------------- Ortszeit und UTC

def utc_text(zeitpunkt: datetime) -> str:
    """Ein Zeitpunkt als UTC-Text im Format von ``db.jetzt_utc()``.

    Ein Zeitpunkt ohne Zeitzone gilt als Ortszeit dieses Rechners -- so
    liefert ``datetime.now()`` ihn.

    >>> from datetime import timezone, timedelta
    >>> utc_text(datetime(2026, 10, 25, 1, 30, tzinfo=timezone(timedelta(hours=2))))
    '2026-10-24T23:30:00+00:00'
    """
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.astimezone()
    return zeitpunkt.astimezone(UTC).replace(microsecond=0).isoformat()


def ortszeit_als_utc(datum: str, uhrzeit: str, zone: tzinfo | None = None) -> str:
    """Datum und Uhrzeit in Ortszeit als UTC-Text.

    ``zone`` None heisst: die Zeitzone dieses Rechners. Tests geben sie
    ausdruecklich an, damit die Zeitumstellung pruefbar ist. Eine Uhrzeit,
    die sich nicht lesen laesst, ergibt einen ValueError.
    """
    naiv = datetime.fromisoformat(f"{datum}T{uhrzeit}")
    return utc_text(naiv.replace(tzinfo=zone) if zone is not None else naiv)


def ortszeit(utc: str, zone: tzinfo | None = None) -> datetime:
    """Ein UTC-Text als Zeitpunkt in Ortszeit (``zone`` None: dieser Rechner)."""
    return datetime.fromisoformat(utc).astimezone(zone)


# ---------------------------------------------------------- Arbeitsabschnitte

@dataclass(frozen=True, slots=True)
class Abschnitt:
    """Eine Arbeitsphase eines Timers, Beginn und Ende in UTC.

    ``ende_utc`` None heisst: laeuft noch.
    """

    beginn_utc: str
    ende_utc: str | None = None

    @property
    def offen(self) -> bool:
        return self.ende_utc is None

    def sekunden(self, jetzt_utc: str) -> int:
        """Laenge in Sekunden; ein offener Abschnitt zaehlt bis ``jetzt_utc``.

        Nie negativ: eine verstellte Uhr ergibt null, nicht eine Gutschrift.
        """
        beginn = datetime.fromisoformat(self.beginn_utc)
        ende = datetime.fromisoformat(self.ende_utc or jetzt_utc)
        return max(0, int((ende - beginn).total_seconds()))


def netto_sekunden(abschnitte: Iterable[Abschnitt], jetzt_utc: str) -> int:
    """Die Arbeitszeit: Summe der Abschnitte. Pausen sind die Luecken dazwischen.

    >>> netto_sekunden([Abschnitt("2026-10-24T23:30:00+00:00", "2026-10-25T02:30:00+00:00")],
    ...                "2026-10-25T09:00:00+00:00")
    10800
    """
    return sum(a.sekunden(jetzt_utc) for a in abschnitte)


def minuten_aus_sekunden(sekunden: int) -> int:
    """Ganze Minuten, kaufmaennisch gerundet, mindestens eine.

    Gerundet wird EINMAL auf die Summe, nie je Abschnitt: sonst kaeme mit
    jeder Pause bis zu eine Minute dazu. Eine angefangene Minute ergibt eine
    Minute -- das Schema verlangt eine Dauer groesser null.

    >>> minuten_aus_sekunden(0), minuten_aus_sekunden(29), minuten_aus_sekunden(90)
    (1, 1, 2)
    """
    return max(1, (max(0, sekunden) + 30) // 60)


def pause_sekunden(abschnitte: Sequence[Abschnitt], jetzt_utc: str, *, offen: bool) -> int:
    """Die Pausen eines Timers: vom ersten Beginn bis zum Ende, abzueglich der Arbeit.

    ``offen``: der Eintrag laeuft noch (laufend oder pausiert). Dann zaehlt
    eine gerade laufende Pause bis jetzt mit; bei einem gestoppten Eintrag
    endet alles mit dem letzten Abschnitt.
    """
    if not abschnitte:
        return 0
    beginn = datetime.fromisoformat(abschnitte[0].beginn_utc)
    letztes = abschnitte[-1].ende_utc
    ende = datetime.fromisoformat(jetzt_utc if offen or letztes is None else letztes)
    gesamt = max(0, int((ende - beginn).total_seconds()))
    return max(0, gesamt - netto_sekunden(abschnitte, jetzt_utc))


def lade_abschnitte(conn: sqlite3.Connection, zeit_id: int) -> list[Abschnitt]:
    """Die gespeicherten Abschnitte einer Zeit, in zeitlicher Reihenfolge."""
    return [
        Abschnitt(z["beginn_utc"], z["ende_utc"])
        for z in conn.execute(
            "SELECT beginn_utc, ende_utc FROM zeit_abschnitt WHERE zeiteintrag_id = ?"
            " ORDER BY beginn_utc, id",
            (zeit_id,),
        )
    ]


def abschnitte_je_eintrag(
    conn: sqlite3.Connection, zeit_ids: Iterable[int]
) -> dict[int, list[Abschnitt]]:
    """Die Abschnitte vieler Zeiten mit wenigen Abfragen -- fuer die Liste."""
    ids = list(zeit_ids)
    ergebnis: dict[int, list[Abschnitt]] = {}
    # SQLite bindet nicht beliebig viele Parameter; die Liste zeigt 400 Zeilen.
    for anfang in range(0, len(ids), 500):
        teil = ids[anfang:anfang + 500]
        for z in conn.execute(
            f"SELECT zeiteintrag_id, beginn_utc, ende_utc FROM zeit_abschnitt"
            f" WHERE zeiteintrag_id IN ({', '.join('?' * len(teil))})"
            f" ORDER BY zeiteintrag_id, beginn_utc, id",
            teil,
        ):
            ergebnis.setdefault(z["zeiteintrag_id"], []).append(
                Abschnitt(z["beginn_utc"], z["ende_utc"])
            )
    return ergebnis


def alt_abschnitt(
    datum: str, start_zeit: str | None, zone: tzinfo | None = None
) -> Abschnitt | None:
    """Der eine Abschnitt eines LAUFENDEN Timers ohne gespeicherte Abschnitte.

    Altbestand von vor Version 21: er gilt als ein Abschnitt ab Datum und
    Uhrzeit des Starts. Ein Beginn, der sich nicht lesen laesst, ergibt
    None -- nicht raten.
    """
    if not start_zeit:
        return None
    try:
        return Abschnitt(ortszeit_als_utc(datum, start_zeit, zone))
    except ValueError:
        return None


def ende_als_endzeit(
    datum: str, start_zeit: str, ende_utc: str, zone: tzinfo | None = None
) -> str:
    """Die ``end_zeit`` eines Eintrags aus dem Ende seines letzten Abschnitts.

    Am Tag des Eintrags die Uhrzeit in Ortszeit, sonst :data:`TAGESENDE` --
    das Schema kennt kein Enddatum und verlangt ``end_zeit >= start_zeit``.
    Die Abschnitte tragen das genaue Ende; ``end_zeit`` ist die Kurzfassung.
    """
    return timer_ende(datum, start_zeit, ortszeit(ende_utc, zone).replace(tzinfo=None))


def abschnitte_teile(abschnitte: Sequence[Abschnitt], zone: tzinfo | None = None) -> list[str]:
    """Jeder Abschnitt als Uhrzeiten in Ortszeit, etwa ``["09:02–10:15", "10:40–…"]``.

    Endet ein Abschnitt an einem spaeteren Tag, als er begann, steht das
    dabei -- sonst liest sich 23:30–00:45 wie ein Abschnitt rueckwaerts.
    Einzeln, damit die Liste einen Abschnitt nicht mitten umbricht.
    """
    teile = []
    for abschnitt in abschnitte:
        beginn = ortszeit(abschnitt.beginn_utc, zone)
        if abschnitt.ende_utc is None:
            teile.append(f"{beginn:%H:%M}–…")
            continue
        ende = ortszeit(abschnitt.ende_utc, zone)
        tage = (ende.date() - beginn.date()).days
        zusatz = f" (+{tage} Tag{'e' if tage > 1 else ''})" if tage > 0 else ""
        teile.append(f"{beginn:%H:%M}–{ende:%H:%M}{zusatz}")
    return teile


def abschnitte_anzeige(abschnitte: Sequence[Abschnitt], zone: tzinfo | None = None) -> str:
    """Die Abschnitte als eine Zeile, etwa ``09:02–10:15, 10:40–…``."""
    return ", ".join(abschnitte_teile(abschnitte, zone))


# -------------------------------------------------------------------- Timer

#: Ab dieser Laufzeit ist ein Timer mit grosser Wahrscheinlichkeit vergessen
#: worden. Das Tool fragt dann nach, statt stillschweigend 14 Stunden zu buchen.
TIMER_VERDACHT_MINUTEN = 10 * 60


@dataclass(frozen=True, slots=True)
class LaufenderTimer:
    """Der eine offene Timer -- laufend oder pausiert.

    ``minuten`` ist die Arbeitszeit (netto, ohne Pausen) bis jetzt, gerundet
    wie beim Stoppen: so zeigen Banner, Liste und die Meldung nach dem
    Stoppen dieselbe Zahl.
    """

    id: int
    kunde_id: int
    kunde: str
    titel: str
    datum: str
    start_zeit: str
    minuten: int
    beschreibung: str | None = None
    pausiert: bool = False
    netto_sekunden: int = 0
    pause_sekunden: int = 0
    abschnitte: tuple[Abschnitt, ...] = ()
    #: Ortszeit (HH:MM), zu der die laufende Pause begann; sonst None.
    pause_seit: str | None = None

    @property
    def verdaechtig(self) -> bool:
        return self.minuten >= TIMER_VERDACHT_MINUTEN

    @property
    def pause_minuten(self) -> int:
        return (self.pause_sekunden + 30) // 60

    @property
    def start_anzeige(self) -> str:
        """Startzeit ohne Sekunden. Gespeichert wird sekundengenau, damit ein
        Timer, der innerhalb einer Minute gestoppt wird, keine Endzeit vor der
        Startzeit bekommt -- angezeigt braucht das niemand."""
        return self.start_zeit[:5]

    @property
    def abschnitte_text(self) -> str:
        return abschnitte_anzeige(self.abschnitte)


def laufender_timer(
    conn: sqlite3.Connection,
    *,
    jetzt: str | None = None,
    heute: str | None = None,
    zone: tzinfo | None = None,
) -> LaufenderTimer | None:
    """Der eine offene Timer, falls es einen gibt -- laufend oder pausiert.

    Mehr als einer kann nicht existieren: das Schema verhindert es per Trigger.
    ``jetzt`` (HH:MM) und ``heute`` (JJJJ-MM-TT) sind Ortszeit und fuer
    Tests gedacht; ohne sie gilt die Uhr des Rechners.

    Gerechnet wird ueber die Abschnitte in UTC: ueber Mitternacht, ueber die
    Zeitumstellung und ohne die Pausen. Ein gestern um 08:00 gestarteter
    Timer laeuft heute um 08:00 seit 24 Stunden -- und ist damit der
    vergessene Timer schlechthin. Vorher galt er mit "0 Minuten" als
    unauffaellig, weil nur Uhrzeiten verglichen wurden. Ein laufender Timer
    ohne Abschnitte (Altbestand) gilt als ein Abschnitt ab seinem Start.
    """
    zeile = conn.execute(
        """
        SELECT z.id, z.kunde_id, k.name AS kunde, z.titel, z.beschreibung, z.datum,
               z.start_zeit
          FROM zeiteintrag z
          JOIN kunde k ON k.id = z.kunde_id
         WHERE z.start_zeit IS NOT NULL AND z.end_zeit IS NULL
         LIMIT 1
        """
    ).fetchone()
    if zeile is None:
        return None

    if jetzt is None and heute is None:
        jetzt_utc = utc_text(datetime.now(UTC))
    else:
        tag = heute or date.today().isoformat()
        uhr = jetzt or datetime.now().strftime("%H:%M:%S")
        jetzt_utc = ortszeit_als_utc(tag, uhr, zone)

    abschnitte = lade_abschnitte(conn, zeile["id"])
    if not abschnitte:
        alt = alt_abschnitt(zeile["datum"], zeile["start_zeit"], zone)
        abschnitte = [alt] if alt else []
    pausiert = bool(abschnitte) and not abschnitte[-1].offen
    netto = netto_sekunden(abschnitte, jetzt_utc)
    pause_seit = None
    if pausiert:
        # Seit gestern Abend pausiert: dann mit Datum, sonst liest sich
        # "seit 18:30" wie heute.
        beginn = ortszeit(abschnitte[-1].ende_utc or jetzt_utc, zone)
        heute_lokal = ortszeit(jetzt_utc, zone).date()
        pause_seit = (f"{beginn:%H:%M}" if beginn.date() == heute_lokal
                      else f"{beginn:%d.%m.} {beginn:%H:%M}")
    return LaufenderTimer(
        id=zeile["id"],
        kunde_id=zeile["kunde_id"],
        kunde=zeile["kunde"],
        titel=zeile["titel"],
        datum=zeile["datum"],
        start_zeit=zeile["start_zeit"],
        # Ohne lesbaren Beginn: nicht raten.
        minuten=minuten_aus_sekunden(netto) if abschnitte else 0,
        beschreibung=zeile["beschreibung"],
        pausiert=pausiert,
        netto_sekunden=netto,
        pause_sekunden=pause_sekunden(abschnitte, jetzt_utc, offen=True),
        abschnitte=tuple(abschnitte),
        pause_seit=pause_seit,
    )

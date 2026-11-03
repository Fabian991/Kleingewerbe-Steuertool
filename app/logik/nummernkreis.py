"""Rechnungsnummern: Format, Vergabe und Lueckenerkennung.

Rechtlicher Stand: § 34a UStDV verdraengt seit 01.01.2025 den Katalog des
§ 14 Abs. 4 UStG fuer Kleinunternehmer. Die fortlaufende Rechnungsnummer ist
dort NICHT mehr aufgefuehrt, umsatzsteuerlich also entfallen. Sie bleibt hier
trotzdem Pflicht, weil die GoBD zur Belegsicherung eine laufende Nummerierung
der Ein- und Ausgangsrechnungen verlangt -- die Rechtsgrundlage wandert vom
UStG in die AO, die Anforderung bleibt.

Entscheidend fuer die Praxis: die Nummer wird erst beim FESTSCHREIBEN gezogen,
nie beim Anlegen eines Entwurfs. Genau dadurch entstehen keine Luecken durch
verworfene Entwuerfe -- der haeufigste Fehler in selbstgebauten Tools.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

TYP_RECHNUNG = "RECHNUNG"
TYP_ANGEBOT = "ANGEBOT"

STANDARD_FORMAT = "RE-{JJJJ}-{NNNN}"

#: Jahresschluessel eines jahresuebergreifend fortlaufenden Kreises.
#:
#: ``nummernkreis`` hat ``UNIQUE (typ, jahr)``, also je Jahr einen eigenen
#: Zaehler. Fuer eine durchlaufende Nummerierung ist diese Jahresdimension nicht
#: nur ueberfluessig, sondern schaedlich: traegt man die Dezemberrechnung im
#: Januar nach, zaehlt der Kreis 2026 weiter, waehrend der Kreis 2027 dieselbe
#: Zahl schon vergeben hat -- zwei Rechnungen mit derselben Nummer. Deshalb
#: haelt ein durchlaufender Kreis GENAU EINE Zeile, unter dem Jahr 0.
KREIS_DURCHLAUFEND = 0

_PLATZHALTER = re.compile(r"\{(JJJJ|JJ|MM|N+)\}")


def formatiere(vorlage: str, jahr: int, seq: int, monat: int = 1) -> str:
    """Setzt eine Nummer aus der Formatvorlage zusammen.

    Unterstuetzte Platzhalter: ``{JJJJ}`` vierstelliges Jahr, ``{JJ}``
    zweistellig, ``{MM}`` Monat, ``{N}`` bis ``{NNNNNN}`` der Zaehler,
    linksseitig mit Nullen auf die Anzahl der N aufgefuellt.

    Der Monat kommt aus dem RECHNUNGSDATUM und wird vom Aufrufer mitgegeben;
    ohne Angabe steht "01". Die Vorbelegung ist nur fuer Formate ohne {MM}
    gedacht -- wer den Monat im Format hat, muss ihn auch liefern, sonst
    heisst jede Rechnung des Jahres "01".

    >>> formatiere("RE-{JJJJ}-{NNNN}", 2026, 42)
    'RE-2026-0042'
    >>> formatiere("{JJ}{MM}-{NN}", 2026, 1, monat=5)
    '2605-01'
    """

    def ersetze(treffer: re.Match[str]) -> str:
        schluessel = treffer.group(1)
        if schluessel == "JJJJ":
            return f"{jahr:04d}"
        if schluessel == "JJ":
            return f"{jahr % 100:02d}"
        if schluessel == "MM":
            return f"{monat:02d}"
        return f"{seq:0{len(schluessel)}d}"

    return _PLATZHALTER.sub(ersetze, vorlage)


def pruefe_format(vorlage: str, *, reset_jaehrlich: bool = True) -> None:
    """Weist Formate zurueck, die keine eindeutige Nummer erzeugen koennen.

    Der zweite Parameter ist kein Beiwerk: Format und Zaehlweise sind nur
    GEMEINSAM pruefbar. Ein Format ohne Jahresanteil ist tadellos, solange der
    Zaehler durchlaeuft -- und erzeugt garantiert doppelte Nummern, sobald er
    jedes Jahr bei 1 beginnt.

    Verlangt wird GENAU EIN Zaehler: zwei Zaehler ergaeben "RE-2026-0001-0001",
    eine Nummer, die niemand so meint. Und die geschweiften Klammern muessen
    paarweise stehen -- eine ueberzaehlige Klammer stuende sonst wortwoertlich
    auf jeder Rechnung.

    >>> pruefe_format("{NNNN}", reset_jaehrlich=False)
    >>> pruefe_format("{NNNN}")          # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    ValueError
    >>> pruefe_format("RE-{JJJJ}-{NNNN}-{NNNN}")   # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    ValueError
    """
    if not vorlage.strip():
        raise ValueError("Das Nummernformat darf nicht leer sein")

    tiefe = 0
    for zeichen in vorlage:
        if zeichen == "{":
            tiefe += 1
        elif zeichen == "}":
            tiefe -= 1
        if tiefe not in (0, 1):
            break
    if tiefe != 0:
        raise ValueError(
            "Im Nummernformat sind die geschweiften Klammern nicht ausgeglichen: "
            "jedes { braucht genau ein }, und Platzhalter stehen nicht ineinander. "
            f"Eingegeben war: {vorlage}"
        )

    teile = re.findall(r"\{([^}]*)\}", vorlage)
    # Kleingeschriebene Platzhalter zuerst melden: "{jjjj}-{nnnn}" ist fast
    # richtig, und "braucht einen Zaehler" hilft dann nicht weiter.
    klein = [
        teil for teil in teile
        if teil != teil.upper()
        and (teil.upper() in ("JJJJ", "JJ", "MM") or re.fullmatch(r"[Nn]+", teil))
    ]
    if klein:
        beispiele = ", ".join(f"{{{t.upper()}}} statt {{{t}}}" for t in klein)
        raise ValueError(f"Platzhalter im Nummernformat werden großgeschrieben: {beispiele}.")
    zaehler = [teil for teil in teile if re.fullmatch(r"N+", teil)]
    if not zaehler:
        raise ValueError("Das Nummernformat braucht einen Zähler, z. B. {NNNN}")
    if len(zaehler) > 1:
        raise ValueError(
            "Das Nummernformat darf nur einen Zähler enthalten, "
            f"es hat {len(zaehler)}: " + ", ".join(f"{{{t}}}" for t in zaehler)
        )
    for teil in teile:
        if teil not in ("JJJJ", "JJ", "MM") and not re.fullmatch(r"N+", teil):
            raise ValueError(f"Unbekannter Platzhalter im Nummernformat: {{{teil}}}")
    if reset_jaehrlich and not re.search(r"\{JJJJ\}|\{JJ\}", vorlage):
        raise ValueError(
            "Ein Format ohne Jahr ({JJJJ} oder {JJ}) und ein jährlicher Neustart "
            "des Zählers ergeben doppelte Nummern. Entweder das Jahr ins Format "
            "aufnehmen oder den Zähler durchlaufen lassen."
        )


@dataclass(frozen=True, slots=True)
class Nummer:
    text: str
    seq: int
    jahr: int


def bestehender_kreis(
    conn: sqlite3.Connection, typ: str = TYP_RECHNUNG
) -> sqlite3.Row | None:
    """Der Kreis, an dem sich ein neues Jahr zu orientieren hat.

    Ein durchlaufender Kreis (Jahr 0) ist IMMER der massgebliche -- er loest die
    Jahreszeilen ab. Sonst gilt der zuletzt angelegte Jahrgang.
    """
    return conn.execute(
        """
        SELECT jahr, format, naechste_seq, reset_jaehrlich
          FROM nummernkreis
         WHERE typ = ?
         ORDER BY (jahr = ?) DESC, jahr DESC
         LIMIT 1
        """,
        (typ, KREIS_DURCHLAUFEND),
    ).fetchone()


def _aktiver_kreis(conn: sqlite3.Connection, jahr: int, typ: str) -> sqlite3.Row | None:
    """Der Kreis, aus dem fuer ``jahr`` eine Nummer gezogen wird."""
    return conn.execute(
        """
        SELECT jahr, format, naechste_seq
          FROM nummernkreis
         WHERE typ = ? AND jahr IN (?, ?)
         ORDER BY (jahr = ?) DESC
         LIMIT 1
        """,
        (typ, KREIS_DURCHLAUFEND, jahr, KREIS_DURCHLAUFEND),
    ).fetchone()


def _hoechste_vergebene(conn: sqlite3.Connection, jahr: int | None) -> int:
    """Die hoechste bereits vergebene Sequenznummer -- ueber ALLE Dokumentarten.

    Rechnung, Storno und Leistungsnachweis ziehen aus demselben Kreis; die
    Spalte ``rechnung.typ`` nennt die Dokumentart, nicht den Kreis. Wer hier
    nur ``typ = 'RECHNUNG'`` zaehlte (so war es), uebersah die Nummer des
    letzten Stornos: nach "Rechnung 62, Storno 63" stand der Zaehler wieder
    auf 63, und die naechste Rechnung bekam die Nummer des Stornos.

    ``jahr`` eingrenzen heisst: nur die Nummern dieses Kalenderjahres -- fuer
    den jaehrlich neu startenden Kreis. ``None`` zaehlt ueber alle Jahre, fuer
    den durchlaufenden.
    """
    if jahr is None:
        zeile = conn.execute(
            "SELECT COALESCE(MAX(nummer_seq), 0) AS seq FROM rechnung"
            " WHERE nummer_seq IS NOT NULL"
        ).fetchone()
    else:
        zeile = conn.execute(
            "SELECT COALESCE(MAX(nummer_seq), 0) AS seq FROM rechnung"
            " WHERE nummer_seq IS NOT NULL AND nummer_jahr = ?",
            (jahr,),
        ).fetchone()
    return int(zeile["seq"])


def sichere_kreis(
    conn: sqlite3.Connection,
    jahr: int,
    *,
    typ: str = TYP_RECHNUNG,
    vorlage: str | None = None,
    start_seq: int | None = None,
    reset_jaehrlich: bool | None = None,
) -> None:
    """Legt den Nummernkreis fuer ein Jahr an, falls er fehlt.

    ``start_seq`` ist der Umstiegsschalter: wer vorher in Word nummeriert hat,
    setzt hier die naechste freie Nummer, damit keine Nummer doppelt vergeben
    wird und keine grosse Luecke entsteht.

    Werden ``vorlage`` und ``reset_jaehrlich`` nicht angegeben, ERBT der neue
    Kreis beides vom bestehenden. Das ist der eigentliche Zweck dieser Funktion:
    aufgerufen wird sie beim Festschreiben, und dort ist das einmal eingerichtete
    Format nicht zur Hand. Fiele sie dort auf ``STANDARD_FORMAT`` zurueck, wuerde
    aus der Nummer 63 am 1. Januar still ein "RE-2027-0001" -- ein Fehler, der
    genau einmal im Jahr und ausgerechnet im laufenden Betrieb auftritt.
    """
    bestehend = bestehender_kreis(conn, typ)

    if vorlage is None:
        vorlage = bestehend["format"] if bestehend is not None else STANDARD_FORMAT
    if reset_jaehrlich is None:
        reset_jaehrlich = bool(bestehend["reset_jaehrlich"]) if bestehend is not None else True
    pruefe_format(vorlage, reset_jaehrlich=reset_jaehrlich)

    schluessel = jahr if reset_jaehrlich else KREIS_DURCHLAUFEND
    if start_seq is None:
        if bestehend is not None and not reset_jaehrlich:
            # Beim Durchlaufen zaehlt der bestehende Kreis weiter.
            start_seq = int(bestehend["naechste_seq"])
        else:
            # Beim jaehrlichen Neustart beginnt das Jahr bei 1 -- ES SEI DENN,
            # in diesem Jahr sind schon Nummern vergeben. Das passiert, wenn
            # die Zeile des Kreises fehlt, die Rechnungen aber da sind: nach
            # einer Umstellung der Zaehlweise, nach einer teilweisen
            # Wiederherstellung. Dann bei 1 zu beginnen hiesse, die erste
            # Nummer des Jahres ein zweites Mal zu vergeben.
            start_seq = _hoechste_vergebene(conn, jahr if reset_jaehrlich else None) + 1

    conn.execute(
        """
        INSERT INTO nummernkreis (typ, jahr, format, naechste_seq, reset_jaehrlich)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (typ, jahr) DO NOTHING
        """,
        (typ, schluessel, vorlage, max(1, start_seq), int(reset_jaehrlich)),
    )


def stelle_um(
    conn: sqlite3.Connection,
    jahr: int,
    *,
    typ: str = TYP_RECHNUNG,
    vorlage: str,
    start_seq: int,
    reset_jaehrlich: bool,
) -> None:
    """Setzt Format, Zaehlweise und naechste Nummer aus der Einrichtung durch.

    Anders als :func:`sichere_kreis`, die nur ergaenzt, aendert diese Funktion
    einen bestehenden Kreis -- sonst liesse sich die Zaehlweise nach der
    Ersteinrichtung nie wieder korrigieren.

    Der Zaehler wird dabei NIE kleiner: ``start_seq`` wird gegen den hoechsten
    bereits vergebenen Stand angehoben. Ein Tippfehler im Formular darf keine
    Nummer ein zweites Mal vergeben -- sie steht schon auf einer verschickten
    Rechnung, und zwei Belege mit derselben Nummer sind in einer Pruefung nicht
    auseinanderzuhalten.
    """
    pruefe_format(vorlage, reset_jaehrlich=reset_jaehrlich)
    schluessel = jahr if reset_jaehrlich else KREIS_DURCHLAUFEND

    seq = max(1, start_seq, _hoechste_vergebene(conn, jahr if reset_jaehrlich else None) + 1)

    conn.execute(
        """
        INSERT INTO nummernkreis (typ, jahr, format, naechste_seq, reset_jaehrlich)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (typ, jahr) DO UPDATE SET
            format = excluded.format,
            naechste_seq = excluded.naechste_seq,
            reset_jaehrlich = excluded.reset_jaehrlich
        """,
        (typ, schluessel, vorlage, seq, int(reset_jaehrlich)),
    )
    # Die Zeilen der ABGEWAEHLTEN Zaehlweise muessen weg, sonst entscheidet die
    # Reihenfolge in _aktiver_kreis() darueber, welche Nummer gezogen wird. Die
    # Historie steht in rechnung, nicht hier -- eine Zeile in nummernkreis traegt
    # nur den naechsten Zaehlerstand, und der ist oben uebernommen.
    #
    # Beim jaehrlichen Neustart bleiben die ANDEREN Jahrgaenge stehen: wer im
    # Januar die Dezemberrechnung nachtraegt, braucht den Zaehler des Vorjahres
    # noch. Geloescht wird nur die Zeile der jeweils anderen Zaehlweise.
    if reset_jaehrlich:
        conn.execute(
            "DELETE FROM nummernkreis WHERE typ = ? AND jahr = ?", (typ, KREIS_DURCHLAUFEND)
        )
    else:
        conn.execute(
            "DELETE FROM nummernkreis WHERE typ = ? AND jahr <> ?", (typ, KREIS_DURCHLAUFEND)
        )


def ziehe_nummer(
    conn: sqlite3.Connection, jahr: int, *, typ: str = TYP_RECHNUNG, monat: int = 1
) -> Nummer:
    """Zieht die naechste Nummer und erhoeht den Zaehler.

    Muss innerhalb der Festschreib-Transaktion aufgerufen werden. Rollt die
    Transaktion zurueck -- etwa weil das PDF-Rendering fehlschlaegt --, ist die
    Nummer nicht verbraucht.

    ``jahr`` und ``monat`` sind Jahr und Monat des Dokumentdatums: sie fuellen
    {JJJJ}, {JJ} und {MM} im Format. Ohne den Monat stand in jeder Nummer mit
    {MM} der Januar -- eine Mairechnung hiess "2601-02".
    """
    zeile = _aktiver_kreis(conn, jahr, typ)
    if zeile is None:
        raise LookupError(f"Kein Nummernkreis fuer {typ} {jahr}. Zuerst sichere_kreis() aufrufen.")

    seq = int(zeile["naechste_seq"])
    conn.execute(
        "UPDATE nummernkreis SET naechste_seq = naechste_seq + 1 WHERE typ = ? AND jahr = ?",
        (typ, int(zeile["jahr"])),
    )
    # Das Jahr der NUMMER ist immer das Kalenderjahr der Rechnung, auch wenn der
    # Zaehler durchlaeuft: {JJJJ} muss das Rechnungsjahr zeigen, und
    # rechnung.nummer_jahr traegt den Pruefpfad fuer die Lueckenanzeige.
    return Nummer(
        text=formatiere(zeile["format"], jahr, seq, monat=monat), seq=seq, jahr=jahr
    )


def vorschau(
    conn: sqlite3.Connection, jahr: int, *, typ: str = TYP_RECHNUNG, monat: int = 1
) -> str:
    """Die Nummer, die beim Festschreiben vergeben wuerde -- ohne sie zu ziehen.

    ``monat`` wie bei :func:`ziehe_nummer`: der Monat des Rechnungsdatums,
    beim Assistenten (ohne Datum) der heutige.
    """
    zeile = _aktiver_kreis(conn, jahr, typ)
    if zeile is None:
        # Noch kein Kreis fuer dieses Jahr: die Vorschau muss zeigen, was
        # sichere_kreis() beim Festschreiben anlegen WUERDE -- also das geerbte
        # Format, nicht die Werkseinstellung.
        bestehend = bestehender_kreis(conn, typ)
        if bestehend is None:
            return formatiere(STANDARD_FORMAT, jahr, 1, monat=monat)
        seq = 1 if bestehend["reset_jaehrlich"] else int(bestehend["naechste_seq"])
        return formatiere(bestehend["format"], jahr, seq, monat=monat)
    return formatiere(zeile["format"], jahr, int(zeile["naechste_seq"]), monat=monat)


def jahr_monat(datum: str | None) -> tuple[int, int]:
    """Jahr und Monat eines ISO-Datums -- fuer {JJJJ} und {MM}.

    >>> jahr_monat("2026-05-10")
    (2026, 5)
    """
    return int(datum[:4]), int(datum[5:7])


def luecken(conn: sqlite3.Connection, jahr: int, *, typ: str = TYP_RECHNUNG) -> list[int]:
    """Fehlende Sequenznummern innerhalb eines Jahres.

    Das FG Koeln (07.12.2017, 15 K 1122/16) hat entschieden, dass keine Pflicht
    zu einer lueckenlos fortlaufenden Nummerierung besteht. Luecken sind in
    einer Betriebspruefung aber erklaerungsbeduerftig, deshalb werden sie
    angezeigt -- und nicht blockiert.
    """
    # Laeuft der Zaehler durch, laeuft auch die Reihe durch: eine Luecke zwischen
    # der letzten Nummer im Dezember und der ersten im Januar waere bei einer
    # Betrachtung Jahr fuer Jahr unsichtbar. Der Kreis entscheidet, nicht der
    # Kalender.
    # Alle Dokumentarten: Storno und Leistungsnachweis ziehen aus demselben
    # Kreis. Zaehlte man nur Rechnungen, meldete die Anzeige die Nummer jedes
    # Stornos als Luecke -- und eine Luecke, die keine ist, lehrt einen, die
    # Anzeige zu ueberlesen.
    kreis = _aktiver_kreis(conn, jahr, typ)
    durchlaufend = kreis is not None and int(kreis["jahr"]) == KREIS_DURCHLAUFEND
    if durchlaufend:
        zeilen = conn.execute(
            "SELECT nummer_seq FROM rechnung WHERE nummer_seq IS NOT NULL"
        )
    else:
        zeilen = conn.execute(
            "SELECT nummer_seq FROM rechnung WHERE nummer_seq IS NOT NULL AND nummer_jahr = ?",
            (jahr,),
        )
    vorhanden = {int(z["nummer_seq"]) for z in zeilen if z["nummer_seq"] is not None}
    if not vorhanden:
        return []
    return sorted(set(range(min(vorhanden), max(vorhanden) + 1)) - vorhanden)


# ------------------------------------------- Rueckgabe vor dem Versand

def _kreis_eingrenzung(conn: sqlite3.Connection, jahr: int, typ: str) -> tuple[str, tuple]:
    """Die WHERE-Bedingung fuer "alle Nummern desselben Kreises wie ``jahr``".

    Laeuft der Zaehler durch, ist das jede vergebene Nummer; beim jaehrlichen
    Neustart nur die des Kalenderjahres -- dieselbe Unterscheidung wie in
    :func:`luecken`.
    """
    kreis = _aktiver_kreis(conn, jahr, typ)
    if kreis is not None and int(kreis["jahr"]) == KREIS_DURCHLAUFEND:
        return "nummer_seq IS NOT NULL", ()
    return "nummer_seq IS NOT NULL AND nummer_jahr = ?", (jahr,)


def ist_zuletzt_vergeben(
    conn: sqlite3.Connection, jahr: int, seq: int, *, typ: str = TYP_RECHNUNG
) -> bool:
    """Ist ``seq`` die zuletzt gezogene Nummer des Kreises, aus dem ``jahr`` zieht?

    Zwei Bedingungen, beide noetig: der Zaehler steht direkt dahinter (sonst
    wurde er verstellt oder eine Nummer danach schon gezogen), und keine
    vergebene Nummer desselben Kreises liegt hoeher -- ueber ALLE
    Dokumentarten, ein Storno zieht aus demselben Kreis.
    """
    kreis = _aktiver_kreis(conn, jahr, typ)
    if kreis is None or int(kreis["naechste_seq"]) != seq + 1:
        return False
    bedingung, werte = _kreis_eingrenzung(conn, jahr, typ)
    hoechste = conn.execute(
        f"SELECT COALESCE(MAX(nummer_seq), 0) AS seq FROM rechnung WHERE {bedingung}", werte
    ).fetchone()["seq"]
    return int(hoechste) <= seq


def gib_frei(
    conn: sqlite3.Connection, jahr: int, seq: int, *, typ: str = TYP_RECHNUNG
) -> bool:
    """Setzt den Zaehler auf ``seq`` zurueck -- nur, wenn sie die zuletzt gezogene ist.

    Gebraucht beim Zuruecknehmen einer Rechnung, die nie verschickt wurde:
    dann bekommt die naechste Rechnung dieselbe Nummer, und es entsteht
    keine Luecke. Ist nach ``seq`` schon eine weitere Nummer vergeben, bleibt
    sie verbraucht -- sie noch einmal zu vergeben hiesse, eine aeltere
    Rechnung HINTER eine juengere zu nummerieren.

    Laeuft innerhalb der Transaktion des Aufrufers; gibt zurueck, ob der
    Zaehler zurueckgesetzt wurde.
    """
    if not ist_zuletzt_vergeben(conn, jahr, seq, typ=typ):
        return False
    kreis = _aktiver_kreis(conn, jahr, typ)
    cur = conn.execute(
        "UPDATE nummernkreis SET naechste_seq = ?"
        " WHERE typ = ? AND jahr = ? AND naechste_seq = ?",
        (seq, typ, int(kreis["jahr"]), seq + 1),
    )
    return cur.rowcount == 1


def nachfolger(
    conn: sqlite3.Connection, jahr: int, seq: int, *, typ: str = TYP_RECHNUNG
) -> str | None:
    """Die erste vergebene Nummer NACH ``seq`` im selben Kreis -- fuer die Meldung,
    warum ``seq`` nicht wieder frei wird."""
    bedingung, werte = _kreis_eingrenzung(conn, jahr, typ)
    zeile = conn.execute(
        f"SELECT nummer FROM rechnung WHERE {bedingung} AND nummer_seq > ?"
        " ORDER BY nummer_seq, id LIMIT 1",
        (*werte, seq),
    ).fetchone()
    return zeile["nummer"] if zeile else None


def ist_durchlaufend(conn: sqlite3.Connection, jahr: int, *, typ: str = TYP_RECHNUNG) -> bool:
    """Zieht ``jahr`` aus einem durchlaufenden Kreis (ohne jaehrlichen Neustart)?"""
    kreis = _aktiver_kreis(conn, jahr, typ)
    return kreis is not None and int(kreis["jahr"]) == KREIS_DURCHLAUFEND

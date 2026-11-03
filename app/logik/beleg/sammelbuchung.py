"""„Alle prüfen und buchen“: den ganzen Eingang auf einmal -- nach einer Vorschau.

Der Wunsch: viele Belege hochladen, und was sicher erkannt ist, ohne weiteren
Handgriff buchen. Die Regeln sind die des Eingangsordners
(``abholen.pruefe_automatisch``), mit genau zwei Lockerungen, weil hier ein
Mensch nach einer Vorschau bewusst klickt:

* Ein NEUER Lieferant wird angelegt -- mit dem Namen, den die Auslesung SICHER
  erkannt hat (aus dem XML einer E-Rechnung; aus der Textschicht eines PDF
  ist ein unbekannter Name nur geraten, dann wartet die Datei).
* Die Kategorie: die Vorgabe des Lieferanten, sonst die in der Vorschau
  gewaehlte, sonst KEINE. Ein Beleg ohne Kategorie steht unter "nur
  ungeklärte" und die EUeR meldet ihn als Luecke, bis er zugeordnet ist --
  das ist besser als geraten: eine falsche Kennzahl fiele nie auf.

Alles andere bleibt streng, denn Betrag, Datum und Lieferant eines gebuchten
Belegs lassen sich nicht mehr aendern (§ 146 Abs. 4 AO): unsichere Pflicht-
felder, Gutschrift, Storno, Mahnung, Kreditkartenabrechnung, Summen, die
nicht aufgehen, Dubletten und aehnliche Belege, ueber 800 EUR netto,
Kategorien mit eigenen Angaben, Fremdwaehrung ohne sicheren Kurs und, wenn die
Zahlung mitgebucht wird, ein Belegdatum am Jahreswechsel -- all das bleibt im
Eingang, mit Grund.

Ein Belegdatum in einem Vorjahr wird hier -- anders als beim unbeaufsichtigten
Abholen -- gebucht: die Nacherfassung eines Jahres ist genau dieser Fall.
Weil die Zahlung dann die EUeR eines womoeglich schon abgegebenen Jahres
aendert, sagen es die Vorschau VOR dem Klick (je Zeile und oben) und das
Ergebnis danach (``Entscheidung.vorjahr``, ``bezahlt.vorjahr_warnung``).

Gebucht wird mit ``automatisch=True``: nicht "geprueft" (der Beleg steht unter
"nur ungeklärte", bis "Stimmt so"), der Lernspeicher lernt nicht, und das
Verfahren traegt den Vermerk ``pipeline.SAMMEL_VERMERK``.

Dateien den vorhandenen Buchungen zuordnen
------------------------------------------
Passt eine Datei zu einem Beleg oder einer Einnahme ohne Datei -- gleicher
Betrag, Datum bis 45 Tage entfernt, der Name nur als Bonus
(app/logik/beleg/anhaengen.py) --, steht sie in der Vorschau in der Gruppe
"zuordnen": ein Dropdown mit allen Kandidaten, "neu buchen" und "liegen
lassen". Die Buchung steht schon in der EUeR, es fehlt nur das Original; als
neuer Beleg gebucht zaehlte die Ausgabe doppelt. Deshalb wird eine Datei mit
irgendeinem Kandidaten hier NIE still als neuer Beleg gebucht -- "neu
buchen" ist eine ausdrueckliche Wahl (``Auswahl.trotz_treffer``). Zugeordnet
wird ueber den ganzen Stapel (``anhaengen.zuordnen``): drei Rechnungen eines
Monatsabos fallen auf den jeweils naechsten Eintrag. Das geht vor "wird
gebucht" und vor "ähnlicher Beleg vorhanden" (der aehnliche ist meist genau
dieser Eintrag).

Zwei Schritte, nie auf einen veralteten Stand
---------------------------------------------
``planen`` liest jede Datei und schreibt nichts. ``buchen`` liest jede
ausgewaehlte Datei NOCHMALS und entscheidet neu -- unter der Sperre des
Abholens (``abholen._LAUF``) und der Inhaltssperre je Datei. Hat sich eine
Datei seit der Vorschau geaendert (andere Pruefsumme), wird sie uebersprungen.
Ein gewaehlter Eintrag muss noch passen und noch ohne Datei sein, sonst
bleibt die Datei mit Meldung. Scheitert eine einzelne Datei, bleiben die
anderen gebucht, und das Ergebnis nennt sie.
"""

from __future__ import annotations

import sqlite3
import sys
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app import db
from app.logik.backup import fehlertext
from app.logik.beleg import abholen, anhaengen, pipeline, textdienst
from app.logik.beleg import bezahlt as bezahlvorgabe

#: Was mit einer Datei geschieht.
BUCHEN = "buchen"
BLEIBT = "bleibt"
#: Byte fuer Byte schon als Beleg gebucht -- in der Vorschau zum Wegwerfen
#: vorgehakt: die identische Datei liegt mit Pruefsumme im Archiv.
DUBLETTE = "dublette"
#: Passt zu einem Beleg oder einer Einnahme ohne Datei -- wird dort angehaengt
#: (oder auf Wunsch doch neu gebucht).
ZUORDNEN = "zuordnen"

#: Notiz am gebuchten Beleg.
NOTIZ = "Per „Alle prüfen und buchen“ aus dem Eingang gebucht."


@dataclass(slots=True)
class Plan:
    """Eine Datei im Eingang und was "Alle prüfen und buchen" mit ihr tut."""

    eingangsname: str
    originalname: str
    sha256: str
    status: str
    grund: str | None = None
    entscheidung: abholen.Entscheidung | None = None
    #: Bei ZUORDNEN: die Kandidaten, der Vorschlag und seine Stufe.
    zuordnung: anhaengen.Zuordnung | None = None

    @property
    def neuer_lieferant(self) -> bool:
        return bool(self.entscheidung and self.entscheidung.lieferant_neu)

    @property
    def neu_buchbar(self) -> bool:
        """Liesse sich die Datei auch als neuer Beleg buchen (Wahl "neu buchen")?"""
        return bool(self.entscheidung and self.entscheidung.buchbar)


@dataclass(slots=True)
class Gebucht:
    eingangsname: str
    originalname: str
    beleg_id: int
    lieferant: str | None
    brutto_cent: int
    ohne_kategorie: bool
    bezahlt_am: str | None
    #: Das Jahr der Zahlung, wenn es vor dem laufenden liegt.
    vorjahr: int | None = None


@dataclass(slots=True)
class Geblieben:
    eingangsname: str
    originalname: str
    grund: str


@dataclass(slots=True)
class Angehaengt:
    eingangsname: str
    originalname: str
    #: ``anhaengen.BELEG`` oder ``anhaengen.EINNAHME`` und die Kennung dort.
    art: str
    ziel_id: int
    abweichungen: list[str]

    @property
    def bezeichnung(self) -> str:
        return f"{'Beleg' if self.art == anhaengen.BELEG else 'Einnahme'} {self.ziel_id}"


@dataclass(slots=True)
class SammelErgebnis:
    """Was ein Klick auf "N Belege buchen" getan hat -- fuer die Belegseite.

    Liegt nur im Speicher (``app.state.sammelbuchung``); was gebucht wurde,
    steht im Journal.
    """

    gebucht: list[Gebucht] = field(default_factory=list)
    geblieben: list[Geblieben] = field(default_factory=list)
    #: An einen Beleg oder eine Einnahme ohne Datei angehaengt.
    angehaengt: list[Angehaengt] = field(default_factory=list)
    #: Originalnamen der weggeworfenen Dubletten.
    entfernt: list[str] = field(default_factory=list)
    fehler: str | None = None
    zeitpunkt: str = field(default_factory=db.jetzt_utc)

    def zusammenfassung(self) -> str:
        """Ein Satz fuer die Belegseite.

        >>> SammelErgebnis().zusammenfassung()
        'Nichts gebucht.'
        >>> e = SammelErgebnis(gebucht=[Gebucht("a", "a.xml", 1, "X", 100, True, None)],
        ...                    geblieben=[Geblieben("b", "b.pdf", "g")], entfernt=["c.pdf"])
        >>> e.zusammenfassung()
        '1 Beleg gebucht, 1 Datei bleibt im Eingang, 1 schon gebuchte Datei weggeworfen.'
        >>> SammelErgebnis(gebucht=[Gebucht("a", "a.xml", 1, "X", 100, False, "2025-06-14",
        ...                                 2025)]).zusammenfassung()[:41]
        '1 Beleg gebucht. Achtung: 1 Zahlung zählt'
        >>> SammelErgebnis(angehaengt=[Angehaengt("a", "a.pdf", "beleg", 3, [])]
        ...                ).zusammenfassung()
        '1 Datei einer vorhandenen Buchung zugeordnet.'
        """
        teile = []
        if self.gebucht:
            teile.append(abholen._anzahl(len(self.gebucht), "Beleg", "Belege") + " gebucht")
        if self.angehaengt:
            teile.append(abholen._anzahl(len(self.angehaengt), "Datei einer vorhandenen Buchung",
                                         "Dateien vorhandenen Buchungen") + " zugeordnet")
        if self.geblieben:
            teile.append(abholen._anzahl(len(self.geblieben), "Datei bleibt", "Dateien bleiben")
                         + " im Eingang")
        if self.entfernt:
            teile.append(abholen._anzahl(len(self.entfernt), "schon gebuchte Datei",
                                         "schon gebuchte Dateien") + " weggeworfen")
        satz = (", ".join(teile) + ".") if teile else "Nichts gebucht."
        if self.vorjahre:
            satz += " " + bezahlvorgabe.vorjahr_warnung(
                self.vorjahre, sum(1 for g in self.gebucht if g.vorjahr is not None))
        return f"{satz} {self.fehler}" if self.fehler else satz

    @property
    def vorjahre(self) -> list[int]:
        """Die Vorjahre, in denen eine mitgebuchte Zahlung zaehlt."""
        return sorted({g.vorjahr for g in self.gebucht if g.vorjahr is not None})


def _reihenfolge(pfad: Path) -> tuple[int, str]:
    """XML zuerst (die E-Rechnung ist massgebend, ihr PDF nur Sichtdatei),
    dann nach dem Namen, unter dem die Datei hochgeladen wurde."""
    return (0 if pfad.suffix.lower() == ".xml" else 1,
            pipeline.original_name_von(pfad).casefold())


def _eingang(pfad: Path) -> pipeline.Eingang:
    return pipeline.Eingang(pfad=pfad, original_name=pipeline.original_name_von(pfad),
                            sha256=pipeline.pruefsumme(pfad), groesse=pfad.stat().st_size)


def _schluessel(e: abholen.Entscheidung) -> tuple[str, ...]:
    """Woran zwei Dateien desselben Stapels als derselbe Lieferant gelten."""
    if e.lieferant_id is not None:
        return ("id", str(e.lieferant_id))
    name, idnr = e.lieferant_neu or (e.lieferant or "", None)
    return ("neu", abholen._idnr_kompakt(idnr) or str(name).strip().casefold())


def gleiche_rechnung(a: abholen.Entscheidung, b: abholen.Entscheidung) -> bool:
    """Sind zwei buchbare Dateien desselben Stapels dieselbe Rechnung?

    Derselbe Lieferant, und dieselbe Belegnummer -- oder dasselbe Datum mit
    demselben Betrag (in derselben Waehrung). Der haeufigste Fall: die
    E-Rechnung als XML und ihr PDF, oder dasselbe PDF zweimal heruntergeladen.
    Die Datenbank kennt beide noch nicht, die Pruefung gegen gebuchte Belege
    saehe es erst beim zweiten Buchen.
    """
    if _schluessel(a) != _schluessel(b):
        return False
    nummer_a = str(a.bestaetigt.get("belegnummer") or "").strip().casefold()
    nummer_b = str(b.bestaetigt.get("belegnummer") or "").strip().casefold()
    if nummer_a and nummer_b:
        return nummer_a == nummer_b
    return (a.belegdatum == b.belegdatum and a.waehrung == b.waehrung
            and a.brutto_cent == b.brutto_cent)


def vorjahr_warnung(plaene: list[Plan]) -> str | None:
    """Die Warnung oben in der Vorschau, wenn Zahlungen in einem Vorjahr zaehlen."""
    jahre = [p.entscheidung.vorjahr for p in plaene
             if p.status == BUCHEN and p.entscheidung is not None
             and p.entscheidung.vorjahr is not None]
    if not jahre:
        return None
    return bezahlvorgabe.vorjahr_warnung(jahre, len(jahre))


def zuordnen_zusammenfassung(plaene: list[Plan]) -> str:
    """Die Zeile ueber der Gruppe "zuordnen": wie viele sicher, zur
    Bestaetigung, mehrdeutig -- und wie viele ohne Treffer neu gebucht werden.

    >>> zuordnen_zusammenfassung([])
    ''
    """
    zaehler = {anhaengen.SICHER: 0, anhaengen.WAHRSCHEINLICH: 0, anhaengen.MEHRDEUTIG: 0}
    for p in plaene:
        if p.status == ZUORDNEN and p.zuordnung is not None:
            zaehler[p.zuordnung.stufe] = zaehler.get(p.zuordnung.stufe, 0) + 1
    if not any(zaehler.values()):
        return ""
    teile = [f"{n} {anhaengen.STUFEN_TEXT[stufe]}" for stufe, n in zaehler.items() if n]
    neu = sum(1 for p in plaene if p.status == BUCHEN)
    if neu:
        teile.append(f"{neu} ohne Treffer {'wird' if neu == 1 else 'werden'} neu gebucht")
    return ", ".join(teile)


def planen(
    conn: sqlite3.Connection,
    datenordner: Path,
    *,
    heute: date | None = None,
    bezahlt: bool | None = None,
    kategorien: dict[str, int | None] | None = None,
    textleser=textdienst.text_mit_zeitgrenze,
) -> list[Plan]:
    """Liest jede Datei im Eingang und sagt, was mit ihr geschaehe. Schreibt nichts.

    ``kategorien`` ist die Auswahl der Vorschau je Eingangsname (fehlt eine
    Datei dort, gilt die Vorgabe des Lieferanten). ``bezahlt``: die Auswahl
    "als bezahlt am Belegdatum" / "offen lassen" (Vorgabe: die Einstellung).
    """
    plaene: list[Plan] = []
    zu_buchen: list[Plan] = []
    #: Dateien mit Treffern: Eingangsname -> Kandidaten. Zugeordnet wird
    #: erst, wenn alle Dateien gelesen sind -- ueber den ganzen Stapel.
    treffer: dict[str, list[anhaengen.Kandidat]] = {}
    namen: dict[str, str] = {}
    for pfad in sorted(pipeline.liegen_geblieben(datenordner), key=_reihenfolge):
        name = pipeline.original_name_von(pfad)
        if pfad.suffix.lower() not in pipeline.ERLAUBT:
            plaene.append(Plan(pfad.name, name, "", BLEIBT, "kein Belegformat — wegwerfen "
                                                            "oder umbenennen"))
            continue
        passt = pipeline.inhalt_passt(pfad)
        if passt is None:
            continue  # gerade gebucht oder weggeworfen
        if not passt:
            plaene.append(Plan(pfad.name, name, "", BLEIBT, "Inhalt passt nicht zur Endung — "
                                                            "nicht buchbar, wegwerfen"))
            continue
        try:
            eingang = _eingang(pfad)
        except OSError:
            continue
        wahl = (kategorien or {}).get(pfad.name, abholen.VORGABE)
        entscheidung = abholen.pruefe_automatisch(
            conn, eingang, datenordner, modus=abholen.SAMMEL, heute=heute,
            textleser=textleser, kategorie=wahl, bezahlt=bezahlt)
        kandidaten = anhaengen.zur_lesung(conn, entscheidung.lesung)
        if kandidaten:
            treffer[pfad.name] = kandidaten
            namen[pfad.name] = name
            plaene.append(Plan(pfad.name, name, eingang.sha256, ZUORDNEN, None, entscheidung))
            continue
        if entscheidung.dublette_von is not None:
            plan = Plan(pfad.name, name, eingang.sha256, DUBLETTE,
                        f"schon als Beleg {entscheidung.dublette_von} gebucht — dieselbe "
                        "Datei liegt mit Prüfsumme im Archiv", entscheidung)
        elif not entscheidung.buchbar:
            plan = Plan(pfad.name, name, eingang.sha256, BLEIBT, entscheidung.grund,
                        entscheidung)
        else:
            gleich = next((p for p in zu_buchen
                           if gleiche_rechnung(p.entscheidung, entscheidung)), None)
            if gleich is not None:
                plan = Plan(pfad.name, name, eingang.sha256, BLEIBT,
                            f"gleiche Rechnung wie „{gleich.originalname}“, die gebucht wird",
                            entscheidung)
            else:
                plan = Plan(pfad.name, name, eingang.sha256, BUCHEN, None, entscheidung)
                zu_buchen.append(plan)
        plaene.append(plan)
    zuordnungen = anhaengen.zuordnen(treffer, namen)
    for plan in plaene:
        if plan.status == ZUORDNEN:
            plan.zuordnung = zuordnungen[plan.eingangsname]
            plan.grund = anhaengen.wartegrund(plan.zuordnung.kandidaten)
    return plaene


@dataclass(slots=True)
class Auswahl:
    """Eine Datei, die gebucht werden soll -- so, wie die Vorschau sie zeigte."""

    eingangsname: str
    sha256: str
    kategorie: int | str | None = abholen.VORGABE
    #: Statt zu buchen an diesen Beleg ohne Datei anhaengen ...
    anhaengen_an: int | None = None
    #: ... oder an diese Einnahme ohne Datei.
    einnahme_an: int | None = None
    #: "neu buchen" trotz passender Eintraege -- ausdruecklich gewaehlt. Ohne
    #: das bleibt eine Datei mit Treffern im Eingang (nie still doppelt).
    trotz_treffer: bool = False

    @property
    def ziel(self) -> tuple[str, int] | None:
        if self.anhaengen_an is not None:
            return (anhaengen.BELEG, self.anhaengen_an)
        if self.einnahme_an is not None:
            return (anhaengen.EINNAHME, self.einnahme_an)
        return None


def buchen(
    conn: sqlite3.Connection,
    datenordner: Path,
    auswahl: list[Auswahl],
    *,
    bezahlt: bool | None = None,
    wegwerfen: list[str] | None = None,
    heute: date | None = None,
    textleser=textdienst.text_mit_zeitgrenze,
) -> SammelErgebnis:
    """Bucht die ausgewaehlten Dateien. Wirft nur ``abholen.LaeuftSchon``.

    Jede Datei wird unter der Sperre noch einmal gelesen und neu entschieden
    -- nie auf den Stand der Vorschau. Fehlt sie (Doppelklick: die erste
    Anfrage hat sie gebucht) oder hat sie sich geaendert, wird sie
    uebersprungen. ``wegwerfen``: Eingangsnamen der Dubletten, die die
    Vorschau vorgehakt hatte -- weggeworfen nur, wenn sie wirklich schon
    gebucht sind.
    """
    if not abholen._LAUF.acquire(blocking=False):
        raise abholen.LaeuftSchon("Das Abholen läuft gerade.")
    ergebnis = SammelErgebnis()
    try:
        ordner = (datenordner / pipeline.EINGANG).resolve()
        reihe = sorted(auswahl, key=lambda a: (
            0 if a.eingangsname.lower().endswith(".xml") else 1, a.eingangsname))
        for nummer, eintrag in enumerate(reihe):
            pfad = _im_eingang(ordner, eintrag.eingangsname)
            if pfad is None:
                gebucht_als = pipeline.dublette(conn, eintrag.sha256) if eintrag.sha256 else None
                if gebucht_als is None:
                    ergebnis.geblieben.append(Geblieben(
                        eintrag.eingangsname, eintrag.eingangsname,
                        "liegt nicht mehr im Eingang"))
                continue
            name = pipeline.original_name_von(pfad)
            try:
                _eine_buchen(conn, datenordner, pfad, name, eintrag, ergebnis,
                             bezahlt=bezahlt, heute=heute, textleser=textleser)
            except abholen.DatenbankBelegt:
                abholen._offene_transaktion_zuruecknehmen(conn)
                ergebnis.fehler = ("Die Datenbank war gerade von einem anderen Vorgang belegt — "
                                   "es wurde aufgehört. Was nicht gebucht ist, liegt weiter im "
                                   "Eingang.")
                for rest in reihe[nummer:]:
                    ergebnis.geblieben.append(Geblieben(
                        rest.eingangsname, rest.eingangsname, "nicht mehr versucht"))
                break
            except Exception as fehler:  # noqa: BLE001 -- eine Datei haelt die anderen nicht auf
                traceback.print_exc(file=sys.stderr)
                abholen._offene_transaktion_zuruecknehmen(conn)
                ergebnis.geblieben.append(Geblieben(
                    eintrag.eingangsname, name,
                    f"beim Buchen trat ein Fehler auf: {fehlertext(fehler)}"))
        _dubletten_wegwerfen(conn, ordner, wegwerfen or [], ergebnis)
    finally:
        abholen._LAUF.release()
    return ergebnis


def _im_eingang(ordner: Path, eingangsname: str) -> Path | None:
    """Die Datei im Eingang -- und nur dort (der Name kommt aus dem Formular)."""
    if (not eingangsname or "/" in eingangsname or "\\" in eingangsname
            or eingangsname.startswith(".") or not eingangsname.isprintable()):
        return None
    try:
        pfad = (ordner / eingangsname).resolve()
    except (OSError, ValueError):
        return None
    if pfad.parent != ordner or not pfad.is_file():
        return None
    return pfad


def _eine_buchen(conn: sqlite3.Connection, datenordner: Path, pfad: Path, name: str,
                 eintrag: Auswahl, ergebnis: SammelErgebnis, *, bezahlt: bool | None,
                 heute: date | None, textleser) -> None:
    """Eine Datei: Pruefsumme wie in der Vorschau, Inhaltssperre, neu
    entscheiden, buchen."""
    try:
        eingang = _eingang(pfad)
    except OSError:
        return  # gerade gebucht oder weggeworfen
    if eingang.sha256 != eintrag.sha256:
        ergebnis.geblieben.append(Geblieben(
            eintrag.eingangsname, name, "die Datei hat sich seit der Vorschau verändert — "
                                        "bitte noch einmal „Alle prüfen und buchen“"))
        return
    sperre = abholen._Inhaltssperre(datenordner, eingang.sha256)
    if not sperre.nehmen():
        ergebnis.geblieben.append(Geblieben(
            eintrag.eingangsname, name, "wird gerade von einem anderen Vorgang bearbeitet"))
        return
    if eintrag.ziel is not None:
        try:
            _eine_anhaengen(conn, datenordner, eingang, name, eintrag, ergebnis,
                            heute=heute, textleser=textleser)
        finally:
            sperre.freigeben()
        return
    try:
        entscheidung = abholen.pruefe_automatisch(
            conn, eingang, datenordner, modus=abholen.SAMMEL, heute=heute,
            textleser=textleser, kategorie=eintrag.kategorie, bezahlt=bezahlt)
        if not entscheidung.buchbar:
            if entscheidung.dublette_von is None:
                ergebnis.geblieben.append(Geblieben(eintrag.eingangsname, name,
                                                    entscheidung.grund or ""))
            return
        if not eintrag.trotz_treffer:
            # Seit der Vorschau kann ein passender Eintrag entstanden sein
            # (WISO-Import in einem zweiten Tab): als neuer Beleg gebucht
            # zaehlte die Ausgabe doppelt. Nur "neu buchen" bucht trotzdem.
            treffer = anhaengen.zur_lesung(conn, entscheidung.lesung)
            if treffer:
                ergebnis.geblieben.append(Geblieben(eintrag.eingangsname, name,
                                                    anhaengen.wartegrund(treffer)))
                return
        beleg_id, grund = abholen.buche_entscheidung(
            conn, entscheidung, datenordner, notiz=NOTIZ, vermerk=pipeline.SAMMEL_VERMERK,
            heute=heute)
    finally:
        sperre.freigeben()
    if beleg_id is None:
        ergebnis.geblieben.append(Geblieben(eintrag.eingangsname, name, grund or ""))
        return
    zeile = conn.execute(
        "SELECT b.betrag_brutto_cent, b.kategorie_id, l.name FROM beleg b"
        " LEFT JOIN lieferant l ON l.id = b.lieferant_id WHERE b.id = ?", (beleg_id,),
    ).fetchone()
    ergebnis.gebucht.append(Gebucht(
        eintrag.eingangsname, name, beleg_id, zeile["name"], zeile["betrag_brutto_cent"],
        zeile["kategorie_id"] is None, entscheidung.zahltag, entscheidung.vorjahr))


def _eine_anhaengen(conn: sqlite3.Connection, datenordner: Path, eingang: pipeline.Eingang,
                    name: str, eintrag: Auswahl, ergebnis: SammelErgebnis, *,
                    heute: date | None, textleser) -> None:
    """Eine Datei an den Beleg oder die Einnahme haengen, die die Vorschau
    vorschlug -- nur, wenn der Eintrag unter der Sperre noch passt (neu
    gelesen, nicht vertraut) und noch ohne Datei ist."""
    entscheidung = abholen.pruefe_automatisch(
        conn, eingang, datenordner, modus=abholen.SAMMEL, heute=heute, textleser=textleser,
        kategorie=None, bezahlt=False)
    if entscheidung.lesung is None or entscheidung.dublette_von is not None:
        ergebnis.geblieben.append(Geblieben(eintrag.eingangsname, name,
                                            entscheidung.grund or "nicht mehr lesbar"))
        return
    ziel = eintrag.ziel
    assert ziel is not None
    passend = {k.schluessel: k for k in anhaengen.zur_lesung(conn, entscheidung.lesung)}
    if ziel not in passend:
        art = "Beleg" if ziel[0] == anhaengen.BELEG else "Einnahme"
        ergebnis.geblieben.append(Geblieben(
            eintrag.eingangsname, name, f"{art} {ziel[1]} passt nicht mehr (hat inzwischen "
                                        "eine Datei, ist gelöscht oder Betrag und Datum "
                                        "passen nicht) — nichts angehängt"))
        return
    try:
        if ziel[0] == anhaengen.BELEG:
            fertig = anhaengen.anhaengen(conn, datenordner, ziel[1], entscheidung.lesung)
            unterschiede = fertig.abweichungen
        else:
            unterschiede = anhaengen.an_einnahme(conn, datenordner, ziel[1],
                                                 entscheidung.lesung).abweichungen
    except pipeline.BelegFehler as fehler:
        ergebnis.geblieben.append(Geblieben(eintrag.eingangsname, name, str(fehler)))
        return
    ergebnis.angehaengt.append(Angehaengt(eintrag.eingangsname, name, ziel[0], ziel[1],
                                          unterschiede))


def _dubletten_wegwerfen(conn: sqlite3.Connection, ordner: Path, namen: list[str],
                         ergebnis: SammelErgebnis) -> None:
    """Wirft die vorgehakten Dubletten weg -- nur, was wirklich schon gebucht
    ist, mit EINEM Journaleintrag (Originalname und Pruefsumme je Datei)."""
    weg: list[dict[str, str]] = []
    for eingangsname in dict.fromkeys(namen):
        pfad = _im_eingang(ordner, eingangsname)
        if pfad is None:
            continue
        try:
            sha256 = pipeline.pruefsumme(pfad)
        except OSError:
            continue
        if pipeline.dublette(conn, sha256) is None:
            continue  # inzwischen keine Dublette mehr (der Beleg ... ) -- nicht anfassen
        originalname = pipeline.original_name_von(pfad)
        if pipeline.eingang_wegwerfen(pfad):
            weg.append({"originalname": originalname, "sha256": sha256})
            ergebnis.entfernt.append(originalname)
    if weg:
        verworfen_protokollieren(conn, weg, grund="schon gebucht (dieselbe Datei im Archiv)")


def verworfen_protokollieren(conn: sqlite3.Connection, dateien: list[dict[str, str]], *,
                             grund: str) -> None:
    """EIN Journaleintrag fuer weggeworfene Eingangsdateien.

    Im Eingang liegt noch kein Beleg -- geloescht wird nur die Kopie im Tool.
    Der Eintrag erklaert spaeter, wo eine Datei geblieben ist: Originalname und
    SHA-256 je Datei.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        db.protokolliere(conn, "EINGANG_VERWORFEN", "eingang", None,
                         wert_neu={"anzahl": len(dateien), "dateien": dateien},
                         begruendung=grund)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

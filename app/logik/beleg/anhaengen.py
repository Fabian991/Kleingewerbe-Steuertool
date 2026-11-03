"""Dateien den vorhandenen Buchungen zuordnen -- und nachtraeglich anhaengen.

Ausgaben aus WISO kommen als Belege ohne Datei ("aus WISO übernommen ·
Datei fehlt", app/logik/wiso.py), Einnahmen aus WISO oder von Hand ohne
Abrechnung (app/logik/einnahmen.py). Die Originale liegen beim Nutzer in
einem Ordner fuer das Finanzamt; sie sollen an den vorhandenen Eintrag --
nicht als zweiter Beleg, der dieselbe Ausgabe ein zweites Mal in die EUeR
braechte.

Zuordnen: Betrag und Datum zuerst, Name nur als Bonus
-----------------------------------------------------
Der Name allein reicht nicht: der WISO-Buchungstext sagt "Claude", die
Rechnung kommt von "Anthropic, PBC"; "Beispielshop" gegen "Beispiel Commerce
Ltd". Deshalb
(:func:`kandidaten`):

* Pflicht ist derselbe Euro-Betrag auf den Cent (nur Euro-Lesungen) und ein
  Datum der Datei (ihr Belegdatum) hoechstens :data:`FENSTER_TAGE` vom
  Belegdatum ODER Zahltag des Eintrags entfernt. 45 Tage, weil Rechnungsdatum
  und Zahltag (Bank) oft weit auseinander liegen: Zahlungsziel 14 oder 30
  Tage, die Kreditkartenabrechnung einen Monat spaeter, ein Jahresabo vor
  der Verlaengerung -- und bei Vorauskasse die Rechnung erst nach der
  Lieferung, also nach dem Zahltag.
* Rang: der Datumsabstand, klein zuerst. Ein aehnlicher Name (Lieferant,
  Buchungstext oder Notiz, Zahler -- ``wiso.namen_aehnlich``) wiegt eine
  Woche Abstand auf, bei einer Gutschrift ebenso eine Einnahme (eine
  Gutschrift ist Geld, das kommt). Bei Gleichstand der aeltere Eintrag.
* Stufen (:func:`zuordnen`): **sicher** -- genau ein freier Kandidat, und
  der liegt hoechstens :data:`TAGE_SICHER` Tage entfernt oder sein Name
  passt; **wahrscheinlich** -- genau ein freier Kandidat, weiter weg und
  ohne Namen (der Nutzer bestaetigt); **mehrdeutig** -- mehrere Kandidaten
  (oder keiner mehr frei), der Nutzer waehlt. "Frei" heisst: nicht schon
  einer anderen Datei desselben Stapels zugeteilt.
* Ein ganzer Stapel ("Alle prüfen und buchen") wird GLOBAL zugeordnet:
  alle (Datei, Eintrag)-Paare nach Rang, gierig, jede Datei und jeder
  Eintrag nur einmal. Drei Rechnungen eines Monatsabos mit gleichem Betrag
  fallen so auf den jeweils naechstliegenden Eintrag, nicht alle auf den
  ersten.

Nie still doppelt: hat eine Datei irgendeinen Kandidaten, bucht die
Sammelbuchung sie nicht als neuen Beleg (app/logik/beleg/sammelbuchung.py),
das Abholen aus dem Eingangsordner laesst sie warten (:func:`wartegrund`),
und die Pruefmaske fragt (app/routen/belege.py).

Anhaengen
---------
Betrag und Datum des Eintrags bleiben, wie sie sind -- der Beleg ist gebucht
(§ 146 Abs. 4 AO; der Trigger beleg_betrag_datum_fest sperrt sie ohnehin),
die Einnahme ebenso. Weicht die Datei ab, sagt die Meldung es
(:func:`abweichungen`); dann ist loeschen und neu buchen der Weg. Die Datei
wird archiviert wie bei jeder Buchung: Pruefsumme, schreibgeschuetzt,
Auslesung und Journal (:func:`anhaengen` fuer Belege, :func:`an_einnahme`
fuer Einnahmen). Je Beleg geht es auch per Upload (Belegliste, "Ändern").
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app import db, pfade
from app.logik.beleg import pipeline
from app.logik.beleg.ergebnis import BelegFehler
from app.logik.rundung import euro

#: So weit duerfen Belegdatum oder Zahltag des Eintrags und das Datum der
#: Datei auseinander liegen, damit der Eintrag ueberhaupt Kandidat ist.
FENSTER_TAGE = 45

#: Bis zu diesem Abstand gilt ein einzelner Kandidat auch ohne passenden
#: Namen als sicher.
TAGE_SICHER = 7

#: So viele Tage Abstand wiegt ein passender Name auf -- und bei einer
#: Gutschrift eine Einnahme gegenueber einem Beleg.
NAME_BONUS = 7

#: Die Arten eines Kandidaten.
BELEG = "beleg"
EINNAHME = "einnahme"

#: Die Stufen einer Zuordnung.
SICHER = "sicher"
WAHRSCHEINLICH = "wahrscheinlich"
MEHRDEUTIG = "mehrdeutig"

STUFEN_TEXT = {SICHER: "sicher", WAHRSCHEINLICH: "zur Bestätigung", MEHRDEUTIG: "mehrdeutig"}

#: Woran die Textschicht eine Gutschrift verraet (``Vorschlag.bedenken``).
_GUTSCHRIFT = re.compile(r"gutschrift|credit\s?note", re.IGNORECASE)

#: Die Quellen, die extraktion.quelle kennt.
_QUELLEN = ("xml", "pdf_text", "ocr_lokal", "email", "manuell")


@dataclass(slots=True)
class Kandidat:
    """Ein Eintrag ohne Datei, zu dem eine Eingangsdatei passen koennte."""

    art: str
    id: int
    #: Belegdatum des Belegs (bei WISO-Belegen der Zahltag) bzw. Tag des Zuflusses.
    datum: str
    betrag_cent: int
    #: Tage zwischen dem Datum der Datei und Belegdatum oder Zahltag -- der kleinere.
    abstand: int
    #: Der gelesene Lieferant aehnelt Lieferant, Zahler, Buchungstext oder Notiz.
    name_passt: bool
    #: Der Zahltag des Belegs, wenn er vom Belegdatum abweicht.
    bezahlt_am: str | None = None
    #: Lieferant bzw. Zahler.
    name: str | None = None
    #: Notiz; bei WISO-Eintraegen traegt sie den Buchungstext.
    text: str | None = None
    wiso_nummer: str | None = None
    #: Die Datei ist eine Gutschrift -- dann liegt eine Einnahme naeher.
    gutschrift: bool = False

    @property
    def schluessel(self) -> tuple[str, int]:
        return (self.art, self.id)

    @property
    def ziel(self) -> str:
        """Der Wert im Formular: ``beleg:12`` / ``einnahme:5``."""
        return f"{self.art}:{self.id}"

    @property
    def bezeichnung(self) -> str:
        return f"{'Beleg' if self.art == BELEG else 'Einnahme'} {self.id}"

    @property
    def wertung(self) -> int:
        """Der Abstand in Tagen, vermindert um die Boni -- kleiner ist besser."""
        bonus = NAME_BONUS if self.name_passt else 0
        if self.gutschrift and self.art == EINNAHME:
            bonus += NAME_BONUS
        return self.abstand - bonus

    @property
    def rang(self) -> tuple[int, int, str, str, int]:
        return (self.wertung, self.abstand, self.datum, self.art, self.id)

    @property
    def kurztext(self) -> str | None:
        """Der Buchungstext ohne den Vorsatz "Aus WISO übernommen (...):"."""
        if not self.text:
            return None
        text = self.text
        if text.startswith("Aus WISO") and "): " in text:
            text = text.split("): ", 1)[1]
        return text if len(text) <= 60 else text[:57].rstrip() + "…"


@dataclass(slots=True)
class Zuordnung:
    """Was fuer eine Datei mit Treffern vorgeschlagen wird."""

    kandidaten: list[Kandidat]
    #: Der vorgeschlagene Eintrag -- ``None``, wenn alle Kandidaten schon
    #: anderen Dateien des Stapels zugeteilt sind.
    vorschlag: Kandidat | None
    stufe: str
    #: Kandidaten, die eine ANDERE Datei des Stapels bekommen: Schluessel ->
    #: Name jener Datei.
    vergeben: dict[tuple[str, int], str] = field(default_factory=dict)
    gutschrift: bool = False

    @property
    def angehakt(self) -> bool:
        """Nur eine sichere Zuordnung ist in der Vorschau vorgehakt."""
        return self.stufe == SICHER and self.vorschlag is not None

    @property
    def stufe_text(self) -> str:
        return STUFEN_TEXT.get(self.stufe, self.stufe)


@dataclass(slots=True)
class Angehaengt:
    beleg_id: int
    datei_id: int
    pfad: Path
    #: Was an der Datei anders ist als am Beleg (Betrag, Datum, Lieferant).
    abweichungen: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnEinnahme:
    einnahme_id: int
    abweichungen: list[str] = field(default_factory=list)


def ohne_datei(conn: sqlite3.Connection, beleg_id: int) -> bool:
    """Hat der Beleg noch kein Original?"""
    return conn.execute("SELECT 1 FROM belegdatei WHERE beleg_id = ? AND rolle = 'ORIGINAL'",
                        (beleg_id,)).fetchone() is None


def _abstand(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return 10_000


def kandidaten(conn: sqlite3.Connection, *, brutto_cent: object, datum: object,
               lieferant: object, gutschrift: bool = False) -> list[Kandidat]:
    """Belege und Einnahmen ohne Datei, zu denen eine Datei passt -- beste zuerst.

    Derselbe Euro-Betrag auf den Cent; Belegdatum oder Zahltag hoechstens
    FENSTER_TAGE entfernt. Alle gueltigen Belege ohne Original (nicht nur
    aus WISO; Eigenbelege haben naturgemaess keine Datei und bleiben aussen
    vor) und alle gueltigen Einnahmen ohne Datei. Der Name zaehlt nur fuer
    den Rang (:attr:`Kandidat.wertung`), nicht fuer die Auswahl.
    """
    from app.logik.wiso import namen_aehnlich

    if not isinstance(brutto_cent, int) or brutto_cent <= 0 or not datum:
        return []
    tag = str(datum)
    name = str(lieferant or "").strip()

    def passt(*texte: object) -> bool:
        return bool(name) and any(namen_aehnlich(name, str(t)) for t in texte if t)

    treffer: list[Kandidat] = []
    for b in conn.execute(
        """
        SELECT b.id, b.belegdatum, b.betrag_brutto_cent, b.notiz, l.name AS lieferant,
               (SELECT MIN(z.datum) FROM zahlung z WHERE z.beleg_id = b.id) AS bezahlt_am,
               (SELECT w.buchungsnummer FROM wiso_buchung w WHERE w.beleg_id = b.id
                   AND w.status = 'AKTIV' AND w.verknuepft = 0 LIMIT 1) AS wiso_nummer
          FROM v_beleg_gueltig b
          LEFT JOIN lieferant l ON l.id = b.lieferant_id
         WHERE b.betrag_brutto_cent = ? AND b.ist_eigenbeleg = 0
           AND NOT EXISTS (SELECT 1 FROM belegdatei d WHERE d.beleg_id = b.id
                             AND d.rolle = 'ORIGINAL')
        """, (brutto_cent,)):
        abstand = min(_abstand(tag, b["belegdatum"]), _abstand(tag, b["bezahlt_am"] or ""))
        if abstand > FENSTER_TAGE:
            continue
        treffer.append(Kandidat(
            BELEG, b["id"], b["belegdatum"], b["betrag_brutto_cent"], abstand,
            passt(b["lieferant"], b["notiz"]),
            bezahlt_am=b["bezahlt_am"] if b["bezahlt_am"] != b["belegdatum"] else None,
            name=b["lieferant"], text=b["notiz"], wiso_nummer=b["wiso_nummer"],
            gutschrift=gutschrift))
    for e in conn.execute(
        """
        SELECT e.id, e.zufluss_am, e.betrag_cent, e.zahler, e.notiz,
               (SELECT w.buchungsnummer FROM wiso_buchung w WHERE w.einnahme_id = e.id
                   AND w.status = 'AKTIV' LIMIT 1) AS wiso_nummer
          FROM v_einnahme_gueltig e
         WHERE e.betrag_cent = ?
           AND NOT EXISTS (SELECT 1 FROM einnahmedatei d WHERE d.einnahme_id = e.id)
        """, (brutto_cent,)):
        abstand = _abstand(tag, e["zufluss_am"])
        if abstand > FENSTER_TAGE:
            continue
        treffer.append(Kandidat(
            EINNAHME, e["id"], e["zufluss_am"], e["betrag_cent"], abstand,
            passt(e["zahler"], e["notiz"]), name=e["zahler"], text=e["notiz"],
            wiso_nummer=e["wiso_nummer"], gutschrift=gutschrift))
    return sorted(treffer, key=lambda k: k.rang)


def ist_gutschrift(lesung: pipeline.Lesung) -> bool:
    """Sieht die Auslesung die Datei als Gutschrift -- aus dem XML (TypeCode
    381, UBL CreditNote) oder am Kopf der Textschicht?"""
    return lesung.ist_gutschrift or any(_GUTSCHRIFT.search(b) for b in lesung.vorschlag.bedenken)


def zur_lesung(conn: sqlite3.Connection, lesung: pipeline.Lesung | None) -> list[Kandidat]:
    """Die Eintraege ohne Datei, zu denen eine gelesene Eingangsdatei passt.

    Leer bei Fremdwaehrung, ohne Betrag oder Datum, und wenn genau diese
    Datei schon gebucht ist (Dublette) -- dann ist Wegwerfen der Weg.
    """
    if lesung is None or lesung.waehrung != "EUR" or lesung.ist_dublette:
        return []
    return kandidaten(conn, brutto_cent=lesung.vorschlag.wert("brutto"),
                      datum=lesung.vorschlag.wert("belegdatum"),
                      lieferant=lesung.vorschlag.wert("lieferant"),
                      gutschrift=ist_gutschrift(lesung))


def _stufe(kandidat: Kandidat, frei: int) -> str:
    """Die Stufe fuer den vorgeschlagenen Kandidaten bei ``frei`` freien."""
    if frei != 1:
        return MEHRDEUTIG
    if kandidat.gutschrift and kandidat.art == BELEG:
        # Eine Gutschrift als Original einer Ausgabe? Eher die Erstattung
        # dazu -- das entscheidet der Mensch.
        return WAHRSCHEINLICH
    if kandidat.abstand <= TAGE_SICHER or kandidat.name_passt:
        return SICHER
    return WAHRSCHEINLICH


def einzeln(treffer: list[Kandidat]) -> Zuordnung | None:
    """Die Zuordnung einer einzelnen Datei (Pruefmaske): der beste Kandidat
    ist der Vorschlag, die Stufe haengt an der Zahl der Kandidaten."""
    if not treffer:
        return None
    return Zuordnung(treffer, treffer[0], _stufe(treffer[0], len(treffer)),
                     gutschrift=treffer[0].gutschrift)


def zuordnen(dateien: dict[str, list[Kandidat]],
             namen: dict[str, str] | None = None) -> dict[str, Zuordnung]:
    """Die Zuordnung eines ganzen Stapels -- global, nicht Datei fuer Datei.

    Alle (Datei, Eintrag)-Paare nach Rang; das beste Paar bekommt den
    Zuschlag, dann das naechste, dessen Datei und Eintrag noch frei sind.
    ``namen`` uebersetzt Eingangsnamen in die hochgeladenen (fuer "für
    „rechnung.pdf“ vorgesehen"). Dateien ohne Kandidaten fehlen im Ergebnis.

    >>> a = Kandidat(BELEG, 1, "2026-03-02", 1392, 1, True)
    >>> b = Kandidat(BELEG, 2, "2026-04-02", 1392, 30, True)
    >>> z = zuordnen({"maerz": [a, b], "april": [Kandidat(BELEG, 1, "2026-03-02", 1392, 30,
    ...               True), Kandidat(BELEG, 2, "2026-04-02", 1392, 1, True)]})
    >>> z["maerz"].vorschlag.id, z["april"].vorschlag.id, z["maerz"].stufe
    (1, 2, 'sicher')
    """
    paare = sorted(((k.rang, name, k) for name, ks in dateien.items() for k in ks),
                   key=lambda p: (p[0], p[1]))
    gewaehlt: dict[str, Kandidat] = {}
    belegt: dict[tuple[str, int], str] = {}
    for _, name, k in paare:
        if name in gewaehlt or k.schluessel in belegt:
            continue
        gewaehlt[name] = k
        belegt[k.schluessel] = name
    ergebnis: dict[str, Zuordnung] = {}
    for name, ks in dateien.items():
        if not ks:
            continue
        vorschlag = gewaehlt.get(name)
        vergeben = {k.schluessel: (namen or {}).get(belegt[k.schluessel], belegt[k.schluessel])
                    for k in ks if k.schluessel in belegt and belegt[k.schluessel] != name}
        frei = sum(1 for k in ks if k.schluessel not in vergeben)
        stufe = _stufe(vorschlag, frei) if vorschlag is not None else MEHRDEUTIG
        ergebnis[name] = Zuordnung(ks, vorschlag, stufe, vergeben, ks[0].gutschrift)
    return ergebnis


def beschreibung(k: Kandidat) -> str:
    """Ein Kandidat in einer Zeile -- fuer Gruende und Meldungen."""
    text = k.bezeichnung
    if k.wiso_nummer:
        text += f" (WISO-Eintrag Nr. {k.wiso_nummer})"
    return text


def wartegrund(treffer: list[Kandidat]) -> str:
    """Warum eine Datei mit Treffern im Eingang wartet statt gebucht zu werden
    -- Eingangsordner und Sammelbuchung ohne ausdrueckliche Wahl."""
    namen = [beschreibung(k) for k in treffer[:3]]
    if len(treffer) > 3:
        namen.append(f"{len(treffer) - 3} weiteren")
    return (f"passt vielleicht zu {' oder '.join(namen)} — die Buchung steht schon da, es "
            "fehlt nur die Datei; im Eingang unter „Alle prüfen und buchen“ zuordnen")


def _abweichungen(lesung: pipeline.Lesung, betrag_cent: int, datum: str, *, wo: str,
                  datum_wo: str) -> list[str]:
    gefunden: list[str] = []
    brutto = lesung.vorschlag.wert("brutto")
    if isinstance(brutto, int) and lesung.waehrung == "EUR" and brutto != betrag_cent:
        gefunden.append(f"Betrag laut Datei {euro(brutto)} €, {wo} {euro(betrag_cent)} €")
    elif lesung.waehrung != "EUR" and brutto:
        gefunden.append(f"die Datei lautet auf {lesung.waehrung} — {wo} stehen "
                        f"{euro(betrag_cent)} €")
    gelesen = lesung.vorschlag.wert("belegdatum")
    if gelesen and str(gelesen) != datum:
        gefunden.append(f"Datum laut Datei {_de(str(gelesen))}, {datum_wo} {_de(datum)}")
    return gefunden


def abweichungen(beleg: sqlite3.Row, lesung: pipeline.Lesung) -> list[str]:
    """Was die Datei anders sagt als der Beleg -- Betrag, Datum."""
    return _abweichungen(lesung, beleg["betrag_brutto_cent"], beleg["belegdatum"],
                         wo="am Beleg", datum_wo="am Beleg (bei WISO-Belegen der Zahltag)")


def abweichungen_einnahme(einnahme: sqlite3.Row, lesung: pipeline.Lesung) -> list[str]:
    """Was die Datei anders sagt als die Einnahme -- Betrag, Datum."""
    return _abweichungen(lesung, einnahme["betrag_cent"], einnahme["zufluss_am"],
                         wo="an der Einnahme", datum_wo="Zufluss am")


def _de(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if len(iso) >= 10 else iso


def anhaengen(conn: sqlite3.Connection, datenordner: Path, beleg_id: int,
              lesung: pipeline.Lesung) -> Angehaengt:
    """Haengt die gelesene Eingangsdatei als Original an einen Beleg ohne Datei.

    Wie :func:`pipeline.buche`: die Sperre vor jeder Dateibewegung, unter ihr
    noch einmal pruefen (Beleg gueltig und ohne Original, Datei nicht schon
    Original eines anderen Belegs, noch im Eingang), dann ins Archiv
    verschieben, Pruefsumme nachrechnen, Datensaetze schreiben. Scheitert
    etwas, wandert die Datei zurueck in den Eingang.
    """
    if lesung.ist_dublette:
        raise pipeline.SchonGebucht(lesung.dublette_von)
    dateien: list[tuple[pipeline.Eingang, str]] = (
        [(lesung.anhang, "ORIGINAL"), (lesung.eingang, "EMAIL")] if lesung.anhang is not None
        else [(lesung.eingang, "ORIGINAL")])
    verschoben: list[tuple[Path, Path]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        beleg = conn.execute("SELECT b.*, l.name AS lieferant FROM v_beleg_gueltig b"
                             " LEFT JOIN lieferant l ON l.id = b.lieferant_id WHERE b.id = ?",
                             (beleg_id,)).fetchone()
        if beleg is None:
            raise BelegFehler(f"Beleg {beleg_id} gibt es nicht oder er ist gelöscht.")
        if not ohne_datei(conn, beleg_id):
            raise BelegFehler(f"Beleg {beleg_id} hat schon eine Datei.")
        for datei, _ in dateien:
            schon = pipeline.dublette(conn, datei.sha256)
            if schon is not None:
                raise pipeline.SchonGebucht(schon)
            passt = pipeline.inhalt_passt(datei.pfad) if datei.pfad.is_file() else None
            if passt is None:
                raise BelegFehler(f"{datei.original_name} liegt nicht mehr im Eingang.")
            if not passt:
                raise BelegFehler(pipeline.inhalt_fremd(datei.original_name, datei.endung))
        ids: list[int] = []
        ziele: list[Path] = []
        for datei, rolle in dateien:
            ablage = pipeline.archivpfad(datenordner, beleg["belegdatum"], datei.sha256,
                                         datei.endung)
            ablage.parent.mkdir(parents=True, exist_ok=True)
            if ablage.exists() and ablage != datei.pfad:
                os.chmod(ablage, pipeline.NUR_LESEN)
            else:
                shutil.move(str(datei.pfad), ablage)
                verschoben.append((ablage, datei.pfad))
                os.chmod(ablage, pipeline.NUR_LESEN)
            if pipeline.pruefsumme(ablage) != datei.sha256:
                raise BelegFehler("Die Datei hat sich beim Archivieren verändert.")
            ids.append(conn.execute(
                "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse, mime,"
                " rolle, importiert_am) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (beleg_id, pfade.relativ(datenordner, ablage), datei.original_name,
                 datei.sha256, ablage.stat().st_size, pipeline._mime(datei.endung), rolle,
                 db.jetzt_utc())).lastrowid)
            ziele.append(ablage)
        unterschiede = abweichungen(beleg, lesung)
        quelle = lesung.vorschlag.quelle if lesung.vorschlag.quelle in _QUELLEN else "manuell"
        conn.execute(
            "INSERT INTO extraktion (beleg_id, quelle, rohantwort_json, bestaetigt_json,"
            " verfahren, regel_version, datei_sha256, zeitpunkt)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (beleg_id, quelle,
             json.dumps({n: {"wert": f.wert, "quelle": f.quelle, "sicher": f.sicher}
                         for n, f in lesung.vorschlag.felder.items()},
                        ensure_ascii=False, sort_keys=True, default=str),
             json.dumps({"angehaengt": True, "belegdatum": beleg["belegdatum"],
                         "brutto": beleg["betrag_brutto_cent"],
                         "abweichungen": unterschiede}, ensure_ascii=False, sort_keys=True),
             "Datei nachträglich angehängt; Betrag und Datum bleiben die des Belegs; "
             f"gelesen: {pipeline._verfahren(lesung.stufen)}",
             dateien[0][0].sha256, db.jetzt_utc()))
        db.protokolliere(conn, "BELEG_DATEI_ANGEHAENGT", "beleg", beleg_id,
                         wert_neu={"datei": dateien[0][0].original_name,
                                   "abweichungen": unterschiede},
                         hash_=dateien[0][0].sha256)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        for ablage, zurueck in reversed(verschoben):
            try:
                os.chmod(ablage, 0o644)
                shutil.move(str(ablage), zurueck)
            except OSError:
                pass
        raise
    pipeline.vergiss_original_name(lesung.eingang.pfad)
    return Angehaengt(beleg_id, ids[0], ziele[0], unterschiede)


def an_einnahme(conn: sqlite3.Connection, datenordner: Path, einnahme_id: int,
                lesung: pipeline.Lesung) -> AnEinnahme:
    """Haengt die gelesene Eingangsdatei als Abrechnung an eine Einnahme ohne Datei.

    Abgelegt wird wie bei ``einnahmen.erfassen`` mit Datei: schreibgeschuetzt
    unter ``einnahmen/<Jahr>/``, mit Pruefsumme und Journal EINNAHME_DATEI
    (``einnahmen.datei_anhaengen``); danach kommt die Kopie aus dem Eingang
    weg. Bei einer Mail mit Rechnung im Anhang ist der Anhang die
    Abrechnung, die Mail kommt als zweite Datei dazu. Betrag und Tag der
    Einnahme bleiben; was die Datei anders sagt, steht in ``abweichungen``
    und im Journal.
    """
    from app.logik import einnahmen

    if lesung.ist_dublette:
        raise pipeline.SchonGebucht(lesung.dublette_von)
    zeile = conn.execute("SELECT * FROM v_einnahme_gueltig WHERE id = ?",
                         (einnahme_id,)).fetchone()
    if zeile is None:
        raise BelegFehler(f"Einnahme {einnahme_id} gibt es nicht oder sie ist gelöscht.")
    if conn.execute("SELECT 1 FROM einnahmedatei WHERE einnahme_id = ?",
                    (einnahme_id,)).fetchone() is not None:
        raise BelegFehler(f"Einnahme {einnahme_id} hat schon eine Datei.")
    dateien = [lesung.anhang, lesung.eingang] if lesung.anhang is not None else [lesung.eingang]
    geprueft: list[einnahmen.Datei] = []
    for datei in dateien:
        try:
            inhalt = datei.pfad.read_bytes()
        except OSError as fehler:
            raise BelegFehler(f"{datei.original_name} liegt nicht mehr im Eingang.") from fehler
        try:
            geprueft.append(einnahmen.datei_pruefen(inhalt, datei.original_name))
        except einnahmen.EinnahmeFehler as fehler:
            raise BelegFehler(str(fehler)) from fehler
    unterschiede = abweichungen_einnahme(zeile, lesung)
    for nummer, datei in enumerate(geprueft):
        vermerk: dict[str, object] = {"aus_eingang": True}
        if nummer == 0:
            vermerk["abweichungen"] = unterschiede
        else:
            vermerk["mail_zu"] = geprueft[0].name
        try:
            einnahmen.datei_anhaengen(conn, datenordner, einnahme_id, datei, journal=vermerk)
        except einnahmen.EinnahmeFehler as fehler:
            raise BelegFehler(str(fehler)) from fehler
    pipeline.eingang_wegwerfen(lesung.eingang.pfad)
    return AnEinnahme(einnahme_id, unterschiede)


def ohne_datei_aus_wiso(conn: sqlite3.Connection) -> int:
    """Wie viele gueltige Belege aus WISO noch keine Datei haben."""
    return int(conn.execute(
        """
        SELECT COUNT(*) FROM v_beleg_gueltig b
         WHERE EXISTS (SELECT 1 FROM wiso_buchung w WHERE w.beleg_id = b.id
                         AND w.status = 'AKTIV' AND w.verknuepft = 0)
           AND NOT EXISTS (SELECT 1 FROM belegdatei d WHERE d.beleg_id = b.id
                             AND d.rolle = 'ORIGINAL')
        """).fetchone()[0])


def ohne_datei_anzahl(conn: sqlite3.Connection) -> tuple[int, int]:
    """(Belege, Einnahmen) ohne Datei -- die moeglichen Ziele einer Zuordnung."""
    belege = int(conn.execute(
        """
        SELECT COUNT(*) FROM v_beleg_gueltig b
         WHERE b.ist_eigenbeleg = 0
           AND NOT EXISTS (SELECT 1 FROM belegdatei d WHERE d.beleg_id = b.id
                             AND d.rolle = 'ORIGINAL')
        """).fetchone()[0])
    einnahmen = int(conn.execute(
        """
        SELECT COUNT(*) FROM v_einnahme_gueltig e
         WHERE NOT EXISTS (SELECT 1 FROM einnahmedatei d WHERE d.einnahme_id = e.id)
        """).fetchone()[0])
    return belege, einnahmen

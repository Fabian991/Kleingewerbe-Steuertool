"""Gebuchte Belege korrigieren: loeschen, wiederherstellen, Zahlung berichtigen.

Die Formulare stehen in der Belegliste unter "Ändern" (Vorlage
beleg_korrektur.html); die Antwort ist eine Meldung ueber der Zeile des
Belegs, wie bei den uebrigen Aktionen der Liste. Die Logik steht in
app/logik/beleg/loeschen.py und app/logik/beleg/belegzahlung.py.

"Loeschen" heisst markieren -- Begruendung dort. Geloescht wird skriptlos in
zwei Schritten: aufklappen, Grund waehlen, bestaetigen.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.filter import datum_de
from app.logik import eingabe, kurse
from app.logik.beleg import anhaengen, belegzahlung, loeschen, pipeline
from app.logik.beleg.ergebnis import BelegFehler
from app.logik.rundung import euro
from app.routen.belege import (
    _beleg,
    _datenordner,
    _eingang,
    _eingangsdatei,
    _lies,
    _meldung,
    _url,
    _zahlungsdatum,
)
from app.web import hole_conn

router = APIRouter()


def _vorjahr_satz(jahre: tuple[int, ...], satz: str) -> str:
    """Die Warnung, wenn sich ein zurueckliegendes EUeR-Jahr aendert.
    ``satz`` nimmt die Jahre auf: "der Beleg stand in der EÜR {}"."""
    if not jahre:
        return ""
    return (f" Achtung: {satz.format(loeschen.jahre_text(jahre))} — ist die Erklärung "
            "schon abgegeben, bitte berichtigen.")


@router.post("/belege/{beleg_id}/loeschen")
def loeschen_route(
    beleg_id: str,
    request: Request,
    grund: str = Form(""),
    grund_frei: str = Form(""),
    anlagegut_mit: str = Form(""),
    neu_erfassen: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Markiert den Beleg als geloescht -- mit Grund, nach Rueckfrage bei
    einem Anlagegut. ``neu_erfassen`` legt die Datei danach gleich wieder in
    den Eingang und oeffnet die Pruefmaske ("falsch ausgelesen")."""
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    try:
        ergebnis = loeschen.loeschen(conn, beleg["id"],
                                     grund=loeschen.grund_aus(grund, grund_frei),
                                     anlagegut_mit=anlagegut_mit == "1")
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg["id"])
    if ergebnis.schon:
        return _meldung("/belege", f"Beleg {beleg['id']} war schon gelöscht "
                                   f"({ergebnis.grund}).", art="erfolg")

    text = (f"Beleg {beleg['id']} gelöscht ({ergebnis.grund}). Er zählt in keiner Auswertung "
            "mehr; Datensatz, Zahlungen und Datei bleiben im Archiv und im Journal. Über "
            "„gelöschte zeigen“ lässt er sich wiederherstellen.")
    if ergebnis.anlagegut is not None:
        text += f" Das Anlagegut „{ergebnis.anlagegut.bezeichnung}“ ist mit gelöscht."
    text += _vorjahr_satz(ergebnis.vorjahre, "der Beleg stand in der EÜR {}")
    art = "warnung" if ergebnis.vorjahre else "erfolg"

    if neu_erfassen == "1":
        try:
            eingang = loeschen.neu_erfassen(conn, beleg["id"], _datenordner(request))
        except BelegFehler as fehler:
            return _meldung("/belege", f"{text} Die Datei ließ sich nicht wieder in den "
                                       f"Eingang legen: {fehler}", art="warnung")
        return RedirectResponse(f"/belege/pruefen/{_url(eingang.pfad.name)}", status_code=303)
    return _meldung("/belege", text, art=art)


@router.post("/belege/{beleg_id}/wiederherstellen")
def wiederherstellen(beleg_id: str, conn: sqlite3.Connection = Depends(hole_conn)):
    """Nimmt die Loeschmarkierung zurueck -- gegen den Fehlklick."""
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    ziel = "/belege?geloescht=1"
    try:
        ergebnis = loeschen.wiederherstellen(conn, beleg["id"])
    except BelegFehler as fehler:
        return _meldung(ziel, str(fehler), beleg=beleg["id"])
    if ergebnis.schon:
        return _meldung("/belege", f"Beleg {beleg['id']} ist nicht gelöscht.", art="erfolg",
                        beleg=beleg["id"])
    text = f"Beleg {beleg['id']} wiederhergestellt — er zählt wieder in der EÜR."
    if ergebnis.anlagegut:
        text += f" Das Anlagegut „{ergebnis.anlagegut}“ steht wieder im Verzeichnis."
    if ergebnis.aehnliche:
        text += (" Achtung: " + pipeline.aehnliche_als_text(ergebnis.aehnliche)
                 + " Ist es dieselbe Rechnung, einen der beiden wieder löschen.")
        return _meldung("/belege", text, art="warnung", beleg=beleg["id"])
    return _meldung("/belege", text, art="erfolg", beleg=beleg["id"])


@router.post("/belege/{beleg_id}/zahltag")
def zahltag(
    beleg_id: str,
    request: Request,
    datum: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Der Zahltag der (einzigen) Zahlung ist falsch -- etwa ueber den
    Jahreswechsel. Fuer die EUeR zaehlt der Tag des Abflusses (§ 11 EStG)."""
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    try:
        tag = _zahlungsdatum(datum)
        if tag is None:
            raise BelegFehler("Bitte den richtigen Zahltag eintragen.")
        korrektur = belegzahlung.zahltag_aendern(conn, _datenordner(request), beleg["id"], tag)
    except kurse.KeinKurs as fehler:
        return _meldung("/belege", f"Zahltag nicht geändert: für die EÜR wird Beleg "
                                   f"{beleg['id']} zum Kurs des Zahlungstags umgerechnet, und "
                                   f"den kennt das Tool noch nicht. {fehler}", beleg=beleg["id"])
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg["id"])
    if korrektur.unveraendert:
        return _meldung("/belege", f"Beleg {beleg['id']}: die Zahlung steht schon am "
                                   f"{datum_de(tag)} — nichts geändert.", art="erfolg",
                        beleg=beleg["id"])
    text = (f"Beleg {beleg['id']}: Zahltag von {datum_de(korrektur.alt_datum)} auf "
            f"{datum_de(korrektur.neu_datum)} geändert.")
    alt_jahr, neu_jahr = korrektur.alt_datum[:4], (korrektur.neu_datum or "")[:4]
    if alt_jahr != neu_jahr:
        text += f" Die Ausgabe zählt jetzt in der EÜR {neu_jahr} statt {alt_jahr}."
    text += _umrechnung_satz(korrektur)
    text += _vorjahr_satz(korrektur.vorjahre, "das ändert die EÜR {}")
    return _meldung("/belege", text, art="warnung" if korrektur.vorjahre else "erfolg",
                    beleg=beleg["id"])


@router.post("/belege/{beleg_id}/zahlung-zuruecknehmen")
def zahlung_zuruecknehmen(beleg_id: str, request: Request, grund: str = Form(""),
                          conn: sqlite3.Connection = Depends(hole_conn)):
    """Der Beleg war doch noch nicht bezahlt: die Zahlung kommt weg, der
    Beleg ist wieder offen (das Journal behaelt die ganze alte Zeile). Der
    Grund ist freiwillig; die Liste fragt vorher nach (zweiter Schritt)."""
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    try:
        korrektur = belegzahlung.zahlung_zuruecknehmen(conn, _datenordner(request), beleg["id"],
                                                       grund=grund)
    except kurse.KeinKurs as fehler:
        return _meldung("/belege", f"Zahlung nicht zurückgenommen: ohne Zahlung gilt für Beleg "
                                   f"{beleg['id']} wieder der Kurs des Belegdatums, und den "
                                   f"kennt das Tool gerade nicht. {fehler}", beleg=beleg["id"])
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg["id"])
    text = (f"Beleg {beleg['id']}: Zahlung vom {datum_de(korrektur.alt_datum)} zurückgenommen — "
            "der Beleg ist wieder offen und zählt in keinem EÜR-Jahr, bis die Zahlung erfasst "
            "ist.")
    text += _umrechnung_satz(korrektur)
    text += _vorjahr_satz(korrektur.vorjahre, "das ändert die EÜR {}")
    return _meldung("/belege", text, art="warnung" if korrektur.vorjahre else "erfolg",
                    beleg=beleg["id"])


def _umrechnung_satz(korrektur: belegzahlung.Korrektur) -> str:
    """Was die Korrektur am Euro-Betrag eines Fremdwaehrungsbelegs geaendert hat."""
    if korrektur.kurs is not None and korrektur.euro_neu_cent != korrektur.euro_alt_cent:
        stichtag = ("den Zahlungstag" if korrektur.neu_datum is not None else "das Belegdatum")
        return (f" Für die EÜR auf {stichtag} umgerechnet: EZB-Kurs "
                f"{kurse.kurs_de(korrektur.kurs.kurs)} vom "
                f"{korrektur.kurs.kursdatum:%d.%m.%Y} = {euro(korrektur.euro_neu_cent)} € "
                f"(vorher {euro(korrektur.euro_alt_cent)} €).")
    if korrektur.hinweis:
        return f" {korrektur.hinweis}"
    return ""


# ------------------------------------------------- Datei nachtraeglich anhaengen

def _angehaengt_text(fertig: anhaengen.Angehaengt) -> tuple[str, str]:
    text = (f"Datei an Beleg {fertig.beleg_id} angehängt — schreibgeschützt mit Prüfsumme im "
            "Archiv; Betrag und Datum des Belegs bleiben.")
    if fertig.abweichungen:
        return (text + " Abweichung: " + "; ".join(fertig.abweichungen) + ". Stimmt der Beleg "
                "nicht, ihn löschen und die Datei neu buchen."), "warnung"
    return text, "erfolg"


@router.post("/belege/{beleg_id}/datei-anhaengen")
async def datei_anhaengen(
    beleg_id: str,
    request: Request,
    datei: UploadFile | None = File(None),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Das Original zu einem Beleg ohne Datei (etwa aus WISO uebernommen).

    Die Datei geht wie jeder Upload erst in den Eingang, wird gelesen und
    dann an den Beleg gehaengt. Scheitert das, liegt sie im Eingang und kann
    dort geprueft oder weggeworfen werden.

    Aufnehmen, Lesen (die Textschicht eines PDF bis zu 30 s, beim ersten
    Start des Textdienstes mehr) und Anhaengen laufen im Threadpool, wie
    beim WISO-Import und "Alle prüfen und buchen": in der Ereignisschleife
    beantwortete der Server solange keine einzige Anfrage.
    """
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    if datei is None or not (datei.filename or "").strip():
        return _meldung("/belege", "Bitte eine Datei wählen.", beleg=beleg["id"])
    datenordner = _datenordner(request)
    try:
        inhalt = await datei.read(pipeline.MAX_BYTES + 1)
        eingang = await run_in_threadpool(pipeline.aufnehmen, inhalt,
                                          datei.filename or "beleg.pdf", datenordner)
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg["id"])

    def lesen_und_anhaengen() -> anhaengen.Angehaengt:
        lesung = _lies(conn, _eingang(eingang.pfad))
        return anhaengen.anhaengen(conn, datenordner, beleg["id"], lesung)

    try:
        fertig = await run_in_threadpool(lesen_und_anhaengen)
    except (BelegFehler, OSError) as fehler:
        return _meldung("/belege", f"Nicht angehängt: {fehler} Die Datei liegt im Eingang.",
                        beleg=beleg["id"])
    text, art = _angehaengt_text(fertig)
    return _meldung("/belege", text, art=art, beleg=beleg["id"])


def _einnahme_text(fertig: anhaengen.AnEinnahme) -> tuple[str, str]:
    text = (f"Datei an Einnahme {fertig.einnahme_id} angehängt — schreibgeschützt mit "
            "Prüfsumme im Archiv; Betrag und Tag der Einnahme bleiben.")
    if fertig.abweichungen:
        return (text + " Abweichung: " + "; ".join(fertig.abweichungen) + ". Stimmt die "
                "Einnahme nicht, sie löschen und neu erfassen."), "warnung"
    return text, "erfolg"


def zuordnen_aus_eingang(request: Request, conn: sqlite3.Connection, dateiname: str,
                         lesung: pipeline.Lesung, ziel: str):
    """Die gelesene Eingangsdatei an den gewaehlten Eintrag haengen -- Beleg
    oder Einnahme ohne Datei (``ziel`` wie im Formular: ``beleg:12``,
    ``einnahme:5``). Aus der Pruefmaske ("Passt zu einer vorhandenen
    Buchung?") und ueber den eigenen Knopf darunter. Danach geht es zur
    Liste des Eintrags, mit der Meldung an seiner Zeile."""
    art, _, nummer = ziel.strip().partition(":")
    kennung = eingabe.kennung(nummer)
    zurueck = f"/belege/pruefen/{_url(dateiname)}"
    if kennung is None or art not in (anhaengen.BELEG, anhaengen.EINNAHME):
        return _meldung(zurueck, "Diesen Eintrag gibt es nicht.")
    datenordner = _datenordner(request)
    try:
        if art == anhaengen.BELEG:
            fertig = anhaengen.anhaengen(conn, datenordner, kennung, lesung)
        else:
            fertig = anhaengen.an_einnahme(conn, datenordner, kennung, lesung)
    except (BelegFehler, OSError) as fehler:
        return _meldung(zurueck, f"Nicht angehängt: {fehler}")
    if art == anhaengen.BELEG:
        text, art_ = _angehaengt_text(fertig)
        return _meldung("/belege", text, art=art_, beleg=fertig.beleg_id)
    text, art_ = _einnahme_text(fertig)
    jahr = conn.execute("SELECT substr(zufluss_am, 1, 4) FROM einnahme WHERE id = ?",
                        (kennung,)).fetchone()[0]
    return _meldung(f"/einnahmen?jahr={jahr}", text, art=art_,
                    anker=f"einnahme-{fertig.einnahme_id}")


@router.post("/belege/eingang/{dateiname}/anhaengen")
def eingang_anhaengen(
    dateiname: str,
    request: Request,
    beleg_id: str = Form(""),
    ziel: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Aus der Pruefmaske oder der Belegliste: die Eingangsdatei an einen
    Beleg oder eine Einnahme ohne Datei. ``ziel`` wie im Dropdown der
    Vorschau (``beleg:12``, ``einnahme:5``); ``beleg_id`` allein meint einen
    Beleg."""
    pfad = _eingangsdatei(_datenordner(request), dateiname)
    if pfad is None:
        return _meldung("/belege", "Diese Datei liegt nicht mehr im Eingang.", anker="eingang")
    if not ziel.strip() and beleg_id.strip():
        ziel = f"{anhaengen.BELEG}:{beleg_id.strip()}"
    try:
        lesung = _lies(conn, _eingang(pfad))
    except OSError:
        return _meldung("/belege", "Diese Datei liegt nicht mehr im Eingang.", anker="eingang")
    return zuordnen_aus_eingang(request, conn, dateiname, lesung, ziel)

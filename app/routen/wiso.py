"""Import aus WISO Steuer: hochladen, Vorschau, übernehmen, Abgleich -- und „Angaben fehlen“.

Die Logik steht in app/logik/wiso.py (Abgleich und Übernahme) und
app/logik/angaben.py (die Liste der fehlenden Angaben). Hier nur der Weg:
die Datei liegt zwischen Vorschau und Bestaetigung im Speicher (nicht im
Datenordner: eine Probedatei laege sonst in jeder Sicherung), und aus dem
Formular kommen nur Entscheidungen -- geplant wird beim Uebernehmen neu.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app import db
from app.logik import angaben, eingabe, einnahmen, kategorien, wiso
from app.logik.beleg import anhaengen
from app.logik.kunden import euro_nach_cent
from app.web import VORLAGEN, betrieb_oder_none, filterzahl, hole_conn

router = APIRouter()

#: Formularfelder der Vorschau: je Buchung bis zu zwei (Aktion, Kategorie),
#: dazu Konten, Zahler und geloeschte -- weit ueber jedem echten Export.
MAX_FORMULARFELDER = 4 * wiso.MAX_ZEILEN + 1000

#: Wohin ein Formular aus "Angaben fehlen" zurueckfuehren darf.
ZIELE = ("/angaben", "/anlagen", "/einnahmen", "/wiso")


def _datenordner(request: Request) -> Path:
    pfad = request.app.state.datenpfad
    return Path(pfad).parent if pfad else db.standard_datenordner()


def _meldung(ziel: str, text: str, art: str = "fehler", anker: str = "") -> RedirectResponse:
    trenner = "&" if "?" in ziel else "?"
    return RedirectResponse(f"{ziel}{trenner}{art}={quote(text)}" + (f"#{anker}" if anker else ""),
                            status_code=303)


class _Zwischenspeicher:
    """Hochgeladene Dateien zwischen Vorschau und Bestaetigung -- nur im Speicher.

    Wie beim Kundenimport: hoechstens fuenf, je eine Stunde, geschluesselt
    nach der Pruefsumme. Nach einem Neustart ist die Vorschau abgelaufen.
    """

    GRENZE = 5
    DAUER_S = 3600

    def __init__(self) -> None:
        self._sperre = threading.Lock()
        self._dateien: dict[str, tuple[float, str, bytes]] = {}

    def ablegen(self, name: str, inhalt: bytes) -> str:
        schluessel = wiso.pruefsumme(inhalt)
        with self._sperre:
            jetzt = time.monotonic()
            for alt, (zeit, _, _) in list(self._dateien.items()):
                if jetzt - zeit > self.DAUER_S:
                    del self._dateien[alt]
            self._dateien.pop(schluessel, None)
            while len(self._dateien) >= self.GRENZE:
                del self._dateien[min(self._dateien, key=lambda s: self._dateien[s][0])]
            self._dateien[schluessel] = (jetzt, name, inhalt)
        return schluessel

    def holen(self, schluessel: str) -> tuple[str, bytes] | None:
        with self._sperre:
            eintrag = self._dateien.get(schluessel)
            if eintrag is None or time.monotonic() - eintrag[0] > self.DAUER_S:
                return None
            return eintrag[1], eintrag[2]

    def entfernen(self, schluessel: str) -> None:
        with self._sperre:
            self._dateien.pop(schluessel, None)


_SPEICHER_SPERRE = threading.Lock()


def _speicher(request: Request) -> _Zwischenspeicher:
    with _SPEICHER_SPERRE:
        speicher = getattr(request.app.state, "wiso_import", None)
        if speicher is None:
            speicher = request.app.state.wiso_import = _Zwischenspeicher()
    return speicher


def angaben_kontext(conn: sqlite3.Connection) -> dict[str, object]:
    """Was die Formulare aus „Angaben fehlen“ brauchen (angaben_formular.html)."""
    return {
        "heute": db.heute(),
        "ausgabenkategorien": kategorien.katalog(conn, bereich=kategorien.BEREICH_AUSGABEN),
        "einstufung_texte": einnahmen.EINSTUFUNG_TEXTE,
    }


# ------------------------------------------------------------------ Seite

@router.get("/wiso", response_class=HTMLResponse)
def seite(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    return _seite(request, conn)


def _seite(request: Request, conn: sqlite3.Connection, *, fehler: str | None = None,
           status_code: int = 200) -> HTMLResponse:
    return VORLAGEN.TemplateResponse(request, "wiso.html", {
        "importe": [(z, _anzahl(z)) for z in wiso.importe(conn)],
        "jahre": wiso.importjahre(conn),
        "angaben": angaben.offene(conn),
        "ohne_datei": anhaengen.ohne_datei_aus_wiso(conn),
        "max_mb": wiso.MAX_BYTES // 1024 // 1024,
        "kopf": ";".join(wiso.KOPF),
        "ergebnis": getattr(request.app.state, "wiso_ergebnis", None)
        if request.query_params.get("ergebnis") == "1" else None,
        "fehler": fehler or request.query_params.get("fehler"),
        "erfolg": request.query_params.get("erfolg"),
        "warnung": request.query_params.get("warnung"),
        **angaben_kontext(conn),
    }, status_code=status_code)


def _json(text: str | None) -> dict:
    try:
        wert = json.loads(text or "{}")
    except ValueError:
        return {}
    return wert if isinstance(wert, dict) else {}


def _anzahl(zeile: sqlite3.Row) -> dict:
    vorschau = _json(zeile["anzahl_json"]).get("vorschau", {})
    return vorschau if isinstance(vorschau, dict) else {}


# ------------------------------------------------------------------ Vorschau

def _vorschau(request: Request, conn: sqlite3.Connection, plan: wiso.Plan, *, schluessel: str,
              auswahl: wiso.Auswahl | None = None, meldung: str | None = None,
              status_code: int = 200) -> HTMLResponse:
    auswahl = auswahl or wiso.Auswahl()
    inhalte = {z["id"]: _json(z["inhalt_json"]) for z in plan.geloescht}
    inhalte |= {p.alt["id"]: _json(p.alt["inhalt_json"]) for p in plan.posten
                if p.alt is not None}
    return VORLAGEN.TemplateResponse(request, "wiso_vorschau.html", {
        "inhalte": inhalte,
        "plan": plan,
        "datei": plan.datei,
        "schluessel": schluessel,
        "auswahl": auswahl,
        "aktionen": {p.schluessel: wiso.aktion_fuer(auswahl, p) for p in plan.posten
                     if p.gruppe in (wiso.NEU, wiso.GEAENDERT, wiso.UMGEBUCHT)},
        "ausgabenkategorien": kategorien.katalog(conn, bereich=kategorien.BEREICH_AUSGABEN),
        "einstufung_texte": einnahmen.EINSTUFUNG_TEXTE,
        "rechtsgrund": einnahmen.RECHTSGRUND,
        "neu_ausgaben": [p for p in plan.neu if p.art == wiso.AUSGABE],
        "neu_anlagen": [p for p in plan.neu if p.art == wiso.ANLAGE],
        "neu_einnahmen": [p for p in plan.neu if p.art in (wiso.EINNAHME, wiso.ERSTATTUNG)],
        "summe": wiso.Plan.summe,
        "meldung": meldung,
    }, status_code=status_code)


async def _datei_lesen(request: Request) -> tuple[str, bytes] | str:
    laenge = request.headers.get("content-length", "")
    if laenge.isdigit() and int(laenge) > wiso.MAX_BYTES + 64 * 1024:
        return (f"Die Datei ist zu groß (höchstens {wiso.MAX_BYTES // 1024 // 1024} MB). Ein "
                "Jahr Buchungen sind meist unter 200 KB.")
    async with request.form(max_files=1, max_fields=5) as formular:
        datei = formular.get("datei")
        if not isinstance(datei, UploadFile) or not datei.filename:
            return "Bitte die CSV-Datei aus WISO wählen."
        name = wiso._dateiname(datei.filename)
        inhalt = await datei.read(wiso.MAX_BYTES + 1)
    return name, inhalt


@router.post("/wiso/import")
async def hochladen(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Datei annehmen, lesen, abgleichen und die Vorschau zeigen. Geschrieben wird nichts."""
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    gelesen = await _datei_lesen(request)
    if isinstance(gelesen, str):
        return _seite(request, conn, fehler=gelesen, status_code=422)
    name, inhalt = gelesen
    try:
        datei = await run_in_threadpool(wiso.lies, name, inhalt)
    except wiso.ImportFehler as fehler:
        return _seite(request, conn, fehler=str(fehler), status_code=422)
    plan = await run_in_threadpool(wiso.plane, conn, datei)
    schluessel = _speicher(request).ablegen(name, inhalt)
    return _vorschau(request, conn, plan, schluessel=schluessel)


def _auswahl(formular) -> wiso.Auswahl:
    """Die Entscheidungen aus dem Formular -- nichts sonst wird gelesen."""
    auswahl = wiso.Auswahl()
    for feld, roh in formular.multi_items():
        wert = str(roh).strip()
        if feld.startswith("konto_"):
            auswahl.konten[feld.removeprefix("konto_")] = eingabe.kennung(wert) if wert else None
        elif feld.startswith("zahler_"):
            auswahl.zahler[feld.removeprefix("zahler_")] = wert
        elif feld.startswith("aktion_"):
            auswahl.aktionen[feld.removeprefix("aktion_")] = wert
        elif feld.startswith("kat_"):
            auswahl.kategorien[feld.removeprefix("kat_")] = eingabe.kennung(wert) if wert else None
        elif feld.startswith("geloescht_"):
            kennung = eingabe.kennung(feld.removeprefix("geloescht_"))
            if kennung is not None:
                auswahl.geloescht[kennung] = wert
    return auswahl


def _nicht_mehr_da(conn: sqlite3.Connection, schluessel: str) -> RedirectResponse:
    """Die Datei der Vorschau ist nicht mehr im Zwischenspeicher -- warum?

    Der Schluessel ist die Pruefsumme der Datei. Ist sie schon eingespielt
    (meist gerade eben, in einem zweiten Fenster derselben Vorschau), sagt
    die Meldung das -- "abgelaufen, übernommen wurde nichts" hiesse sonst, der
    Import sei nicht passiert.
    """
    eintrag = conn.execute("SELECT * FROM wiso_import WHERE sha256 = ?"
                           " ORDER BY importiert_am DESC, id DESC LIMIT 1",
                           (schluessel,)).fetchone() if _schluessel_gueltig(schluessel) else None
    if eintrag is not None:
        zeit = eintrag["importiert_am"]
        return _meldung(f"/wiso/abgleich?jahr={eintrag['jahr']}",
                        f"Diese Datei ({eintrag['dateiname']}) wurde am {zeit[8:10]}.{zeit[5:7]}."
                        f"{zeit[:4]} schon übernommen — hier der Abgleich mit dem Tool. Eine "
                        "neuere Fassung aus WISO bitte neu wählen.", "erfolg")
    return _meldung("/wiso", "Die Vorschau ist abgelaufen (nach einer Stunde oder einem "
                             "Neustart). Bitte die Datei erneut wählen — übernommen wurde "
                             "nichts.")


def _schluessel_gueltig(schluessel: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", schluessel or ""))


@router.post("/wiso/vorschau")
async def vorschau_aktualisieren(request: Request,
                                 conn: sqlite3.Connection = Depends(hole_conn)):
    """Dieselbe Vorschau mit den gewaehlten Kategorien und Einstufungen."""
    formular = await request.form(max_fields=MAX_FORMULARFELDER)
    schluessel = str(formular.get("schluessel", ""))
    datei = _speicher(request).holen(schluessel) if _schluessel_gueltig(schluessel) else None
    if datei is None:
        return _nicht_mehr_da(conn, schluessel)
    auswahl = _auswahl(formular)
    try:
        gelesen = await run_in_threadpool(wiso.lies, *datei)
    except wiso.ImportFehler as fehler:
        return _meldung("/wiso", str(fehler))
    plan = await run_in_threadpool(wiso.plane, conn, gelesen, auswahl)
    return _vorschau(request, conn, plan, schluessel=schluessel, auswahl=auswahl)


@router.post("/wiso/uebernehmen")
async def uebernehmen(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Übernimmt die Datei der Vorschau -- neu gelesen und geplant, in EINER Transaktion."""
    formular = await request.form(max_fields=MAX_FORMULARFELDER)
    schluessel = str(formular.get("schluessel", ""))
    fingerabdruck = str(formular.get("fingerabdruck", ""))
    if not (_schluessel_gueltig(schluessel) and _schluessel_gueltig(fingerabdruck)):
        return _meldung("/wiso", "Die Vorschau ist unvollständig. Bitte die Datei erneut wählen.")
    datei = _speicher(request).holen(schluessel)
    if datei is None:
        return _nicht_mehr_da(conn, schluessel)
    auswahl = _auswahl(formular)
    name, inhalt = datei
    try:
        ergebnis = await run_in_threadpool(
            lambda: wiso.uebernehmen(conn, _datenordner(request), name, inhalt,
                                     fingerabdruck=fingerabdruck, auswahl=auswahl))
    except wiso.VorschauVeraltet as veraltet:
        return _vorschau(request, conn, veraltet.plan, schluessel=schluessel, auswahl=auswahl,
                         status_code=409,
                         meldung="Seit der Vorschau hat sich etwas geändert (ein Beleg oder "
                                 "eine Einnahme kam dazu, oder dieselbe Datei wurde in einem "
                                 "anderen Fenster übernommen). Übernommen wurde nichts — "
                                 "bitte diese neue Vorschau prüfen.")
    except (wiso.ImportFehler, einnahmen.EinnahmeFehler) as fehler:
        return _meldung("/wiso", f"Nichts übernommen: {fehler}")
    _speicher(request).entfernen(schluessel)
    request.app.state.wiso_ergebnis = ergebnis
    return _meldung("/wiso?ergebnis=1", ergebnis.zusammenfassung(), "erfolg", anker="ergebnis")


# ------------------------------------------------------------------ Abgleich

@router.get("/wiso/abgleich", response_class=HTMLResponse)
def abgleich(request: Request, jahr: str = "", conn: sqlite3.Connection = Depends(hole_conn)):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    jahre = wiso.importjahre(conn)
    gewaehlt = filterzahl(jahr) or (jahre[0] if jahre else int(db.heute()[:4]))
    return VORLAGEN.TemplateResponse(request, "wiso_abgleich.html", {
        "abgleich": wiso.abgleich(conn, _datenordner(request), gewaehlt),
        "jahre": jahre,
        "jahr": gewaehlt,
        "erfolg": request.query_params.get("erfolg"),
    })


# ------------------------------------------------------------- Angaben fehlen

@router.get("/angaben", response_class=HTMLResponse)
def angaben_seite(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    return VORLAGEN.TemplateResponse(request, "angaben.html", {
        "angaben": angaben.offene(conn),
        "arten": angaben.ARTEN,
        "fehler": request.query_params.get("fehler"),
        "erfolg": request.query_params.get("erfolg"),
        "warnung": request.query_params.get("warnung"),
        **angaben_kontext(conn),
    })


def _zurueck(roh: str) -> str:
    return roh if roh in ZIELE else "/angaben"


def _angabe_fehler(ziel: str, kennung: int | None, text: str,
                   werte: dict[str, str] | None = None) -> RedirectResponse:
    """Zurueck zur Karte der Angabe -- mit Meldung und Eingaben IN der Karte.

    Vorher stand die Meldung oben auf der Seite (auf /anlagen und /wiso
    ausserhalb des Fensters, der Sprung ging zur Karte), und die Felder
    waren leer. ``angabe_fehler`` statt ``fehler``: die Karte zeigt es, nicht
    der Seitenkopf.
    """
    if kennung is None:
        return _meldung(ziel, text)
    abfrage = urlencode({"angabe": kennung, "angabe_fehler": text,
                         **{f"w_{k}": v[:100] for k, v in (werte or {}).items()}})
    return RedirectResponse(f"{ziel}?{abfrage}#angabe-{kennung}", status_code=303)


@router.post("/angaben/{angabe_id}/anlagegut")
def angabe_anlagegut(angabe_id: str, anschaffung_am: str = Form(""), ak_brutto: str = Form(""),
                     nutzungsdauer_jahre: str = Form(""), art: str = Form("BEWEGLICH"),
                     zurueck: str = Form("/angaben"),
                     conn: sqlite3.Connection = Depends(hole_conn)):
    ziel = _zurueck(zurueck)
    kennung = eingabe.kennung(angabe_id)
    werte = {"anschaffung_am": anschaffung_am, "ak_brutto": ak_brutto,
             "nutzungsdauer_jahre": nutzungsdauer_jahre, "art": art}
    try:
        cent = euro_nach_cent(ak_brutto)
    except (ValueError, ArithmeticError) as fehler:
        return _angabe_fehler(ziel, kennung, f"Anschaffungskosten: {fehler}", werte)
    try:
        dauer = (eingabe.ganzzahl(nutzungsdauer_jahre, feld="Nutzungsdauer in Jahren",
                                  mindestens=1, hoechstens=50)
                 if nutzungsdauer_jahre.strip() else None)
        if kennung is None:
            raise angaben.AngabenFehler("Diese Angabe gibt es nicht.")
        ergebnis = angaben.anlagegut_ergaenzen(conn, kennung, anschaffung_am=anschaffung_am,
                                               ak_brutto_cent=cent, nutzungsdauer_jahre=dauer,
                                               art=art)
    except (ValueError, angaben.AngabenFehler) as fehler:
        return _angabe_fehler(ziel, kennung, str(fehler), werte)
    if ergebnis.hinweise:
        # Gespeichert -- nur weicht die AfA von WISO ab: eine Warnung, kein Fehler.
        return _meldung(ziel, " ".join([ergebnis.text, *ergebnis.hinweise]), "warnung")
    return _meldung(ziel, ergebnis.text, "erfolg")


@router.post("/angaben/{angabe_id}/konto")
def angabe_konto(angabe_id: str, request: Request, kategorie_id: str = Form(""),
                 zurueck: str = Form("/angaben"), conn: sqlite3.Connection = Depends(hole_conn)):
    ziel = _zurueck(zurueck)
    kennung = eingabe.kennung(angabe_id)
    try:
        if kennung is None:
            raise angaben.AngabenFehler("Diese Angabe gibt es nicht.")
        ergebnis = angaben.konto_zuordnen(conn, _datenordner(request), kennung,
                                          eingabe.kennung(kategorie_id))
    except angaben.AngabenFehler as fehler:
        return _angabe_fehler(ziel, kennung, str(fehler), {"kategorie_id": kategorie_id})
    return _meldung(ziel, " ".join([ergebnis.text, *ergebnis.hinweise]), "erfolg")


@router.post("/angaben/{angabe_id}/zahler")
def angabe_zahler(angabe_id: str, request: Request, einstufung: str = Form(""),
                  zurueck: str = Form("/angaben"), conn: sqlite3.Connection = Depends(hole_conn)):
    ziel = _zurueck(zurueck)
    kennung = eingabe.kennung(angabe_id)
    try:
        if kennung is None:
            raise angaben.AngabenFehler("Diese Angabe gibt es nicht.")
        ergebnis = angaben.zahler_einstufen(conn, _datenordner(request), kennung,
                                            einstufung.strip())
    except angaben.AngabenFehler as fehler:
        return _angabe_fehler(ziel, kennung, str(fehler), {"einstufung": einstufung})
    return _meldung(ziel, " ".join([ergebnis.text, *ergebnis.hinweise]), "erfolg")

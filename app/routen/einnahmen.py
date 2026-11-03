"""Einnahmen ohne eigene Rechnung: erfassen, zuordnen, loeschen.

Affiliate-Auszahlungen, Werbeerloese, Plattform-Abrechnungen kommen jeden
Monat -- ohne Rechnung aus diesem Werkzeug. Die Seite nimmt sie von Hand auf
(die Logik steht in app/logik/einnahmen.py); was aus WISO kommt, steht hier
ebenso, gekennzeichnet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db, pfade
from app.logik import angaben, eingabe, einnahmen, kategorien
from app.logik.kunden import euro_nach_cent
from app.routen.belege import dateiantwort
from app.web import VORLAGEN, betrieb_oder_none, filterzahl, hole_conn

router = APIRouter()

#: Vorschlaege fuer den Grund beim Loeschen.
GRUENDE = ("doppelt erfasst", "falscher Betrag", "Testeintrag")


def _datenordner(request: Request) -> Path:
    pfad = request.app.state.datenpfad
    return Path(pfad).parent if pfad else db.standard_datenordner()


def _meldung(text: str, art: str = "fehler", *, jahr: int | None = None,
             anker: str = "") -> RedirectResponse:
    ziel = "/einnahmen" + (f"?jahr={jahr}&" if jahr else "?")
    return RedirectResponse(f"{ziel}{art}={quote(text)}" + (f"#{anker}" if anker else ""),
                            status_code=303)


@router.get("/einnahmen", response_class=HTMLResponse)
def seite(request: Request, jahr: str = "", geloescht: str = "",
          conn: sqlite3.Connection = Depends(hole_conn)):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    return _seite(request, conn, jahr=filterzahl(jahr), mit_geloeschten=geloescht == "1")


def _seite(request: Request, conn: sqlite3.Connection, *, jahr: int | None = None,
           mit_geloeschten: bool = False, formular: dict | None = None,
           status_code: int = 200) -> HTMLResponse:
    """Die Seite -- auf Wunsch mit dem abgewiesenen Formular.

    ``formular``: die Eingaben (``werte``), die Meldung (``fehler``) und
    vermutlich schon erfasste Einnahmen (``aehnliche``). Vorher leitete ein
    Eingabefehler nach ``/einnahmen?fehler=…#neu`` um: die Meldung stand oben
    ausserhalb des Fensters, und alle Eingaben waren weg.
    """
    laufend = int(db.heute()[:4])
    gewaehlt = jahr or laufend
    jahre = sorted(einnahmen.jahre(conn) | {laufend, gewaehlt}, reverse=True)
    summen = einnahmen.summen(conn, gewaehlt)
    return VORLAGEN.TemplateResponse(request, "einnahmen.html", {
        "jahr": gewaehlt,
        "jahre": jahre,
        "liste": einnahmen.liste(conn, gewaehlt, mit_geloeschten=mit_geloeschten),
        "mit_geloeschten": mit_geloeschten,
        "formular": formular or {"werte": {}},
        "beschreibung": einnahmen.beschreibung,
        "summen": summen,
        "gesamt_cent": sum(int(s["cent"]) for s in summen),
        "zaehlt_cent": einnahmen.zaehlt_zu_19_cent(conn, gewaehlt),
        "kategorien": kategorien.katalog(conn, bereich=kategorien.BEREICH_EINNAHMEN),
        "zahler": einnahmen.zahler_liste(conn),
        "einstufungen": einnahmen.EINSTUFUNG_TEXTE,
        "rechtsgrund": einnahmen.RECHTSGRUND,
        "zaehlt": einnahmen.ZAEHLT_ZU_19,
        "angaben": angaben.offene(conn, "ZAHLER"),
        "einstufung_texte": einnahmen.EINSTUFUNG_TEXTE,
        "gruende": GRUENDE,
        "heute": db.heute(),
        "fehler": request.query_params.get("fehler"),
        "erfolg": request.query_params.get("erfolg"),
        # Erledigt, aber mit Hinweis: eine aus dem Eingang zugeordnete Datei
        # weicht von der Einnahme ab (app/routen/belegkorrektur.py).
        "warnung": request.query_params.get("warnung"),
    }, status_code=status_code)


def _kategorie_fuer(conn: sqlite3.Connection, roh: str, zahler: str) -> tuple[int | None, str]:
    """Die gewaehlte Kategorie -- oder, bei "nach Zahler", die gemerkte bzw. vorgeschlagene."""
    gewaehlt = eingabe.kennung(roh) if roh.strip() else None
    if gewaehlt is not None:
        return gewaehlt, ""
    gemerkt = einnahmen.gemerkt(conn, einnahmen.zahler_kennung(zahler))
    if gemerkt is not None:
        return gemerkt["kategorie_id"], " (Kategorie wie für diesen Zahler gemerkt)"
    vorschlag = einnahmen.zahler_aus_text(zahler)
    kategorie = einnahmen.kategorie_der_einstufung(conn, vorschlag.vorschlag)
    return kategorie["id"], f" (Kategorie vorgeschlagen: {vorschlag.grund})"


async def _datei(datei: UploadFile | None) -> einnahmen.Datei | None:
    if datei is None or not (datei.filename or "").strip():
        return None
    inhalt = await datei.read(einnahmen.pipeline.MAX_BYTES + 1)
    return einnahmen.datei_pruefen(inhalt, datei.filename or "")


@router.post("/einnahmen")
async def erfassen(
    request: Request,
    zufluss_am: str = Form(""),
    betrag: str = Form(""),
    zahler: str = Form(""),
    kategorie_id: str = Form(""),
    notiz: str = Form(""),
    merken: str = Form(""),
    trotzdem: str = Form(""),
    datei: UploadFile | None = File(None),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    werte = {"zufluss_am": zufluss_am, "betrag": betrag, "zahler": zahler,
             "kategorie_id": kategorie_id, "notiz": notiz, "merken": merken,
             "datei": bool(datei is not None and (datei.filename or "").strip())}

    def zurueck(fehler: einnahmen.EinnahmeFehler | str, status_code: int = 422):
        # Die Seite mit Meldung und Eingaben -- wie bei Belegen und Rechnungen.
        return _seite(request, conn, formular={
            "werte": werte, "fehler": str(fehler),
            "aehnliche": getattr(fehler, "aehnliche", None)}, status_code=status_code)

    try:
        cent = euro_nach_cent(betrag)
    except (ValueError, ArithmeticError) as fehler:
        return zurueck(f"Betrag: {fehler}")
    try:
        abrechnung = await _datei(datei)
        kategorie, woher = _kategorie_fuer(conn, kategorie_id, zahler)
        einnahme_id = einnahmen.erfassen(
            conn, zufluss_am=zufluss_am, betrag_cent=cent, zahler=zahler,
            kategorie_id=kategorie, notiz=notiz, merken_fuer_zahler=merken == "1",
            datei=abrechnung, datenordner=_datenordner(request),
            trotz_aehnlicher=trotzdem == "1")
    except einnahmen.AehnlicheEinnahme as fehler:
        return zurueck(fehler, 409)
    except einnahmen.EinnahmeFehler as fehler:
        return zurueck(fehler)
    zeile = conn.execute("SELECT e.*, k.bezeichnung FROM einnahme e JOIN euer_kategorie k"
                         " ON k.id = e.kategorie_id WHERE e.id = ?", (einnahme_id,)).fetchone()
    return _meldung(
        f"Einnahme {einnahme_id} erfasst: {zeile['zahler']}, "
        f"{einnahmen.euro(zeile['betrag_cent'])} € am {zeile['zufluss_am'][8:10]}."
        f"{zeile['zufluss_am'][5:7]}.{zeile['zufluss_am'][:4]} — {zeile['bezeichnung']}{woher}.",
        "erfolg", jahr=int(zeile["zufluss_am"][:4]))


def _jahr_von(conn: sqlite3.Connection, einnahme_id: int | None) -> int | None:
    if einnahme_id is None:
        return None
    zeile = conn.execute("SELECT zufluss_am FROM einnahme WHERE id = ?",
                         (einnahme_id,)).fetchone()
    return int(zeile["zufluss_am"][:4]) if zeile else None


@router.post("/einnahmen/{einnahme_id}/kategorie")
def kategorie_setzen(einnahme_id: str, kategorie_id: str = Form(""), merken: str = Form(""),
                     conn: sqlite3.Connection = Depends(hole_conn)):
    kennung = eingabe.kennung(einnahme_id)
    try:
        if kennung is None:
            raise einnahmen.EinnahmeFehler("Diese Einnahme gibt es nicht.")
        einnahmen.kategorie_setzen(conn, kennung, eingabe.kennung(kategorie_id),
                                   merken_fuer_zahler=merken == "1")
    except einnahmen.EinnahmeFehler as fehler:
        return _meldung(str(fehler), jahr=_jahr_von(conn, kennung))
    return _meldung(f"Einnahme {kennung} neu zugeordnet.", "erfolg",
                    jahr=_jahr_von(conn, kennung))


@router.post("/einnahmen/{einnahme_id}/loeschen")
def loeschen(einnahme_id: str, grund: str = Form(""), grund_frei: str = Form(""),
             conn: sqlite3.Connection = Depends(hole_conn)):
    kennung = eingabe.kennung(einnahme_id)
    text = " — ".join(t for t in (grund.strip(), grund_frei.strip()) if t)
    try:
        if kennung is None:
            raise einnahmen.EinnahmeFehler("Diese Einnahme gibt es nicht.")
        getan = einnahmen.loeschen(conn, kennung, grund=text)
    except einnahmen.EinnahmeFehler as fehler:
        return _meldung(str(fehler), jahr=_jahr_von(conn, kennung))
    kurz = text if len(text) <= 80 else text[:79] + "…"
    return _meldung(f"Einnahme {kennung} gelöscht ({kurz}) — sie zählt nicht mehr; "
                    "unter „gelöschte zeigen“ lässt sie sich wiederherstellen."
                    if getan else f"Einnahme {kennung} war schon gelöscht.",
                    "erfolg", jahr=_jahr_von(conn, kennung))


@router.post("/einnahmen/{einnahme_id}/wiederherstellen")
def wiederherstellen(einnahme_id: str, conn: sqlite3.Connection = Depends(hole_conn)):
    kennung = eingabe.kennung(einnahme_id)
    try:
        if kennung is None:
            raise einnahmen.EinnahmeFehler("Diese Einnahme gibt es nicht.")
        einnahmen.wiederherstellen(conn, kennung)
    except einnahmen.EinnahmeFehler as fehler:
        return _meldung(str(fehler), jahr=_jahr_von(conn, kennung))
    return _meldung(f"Einnahme {kennung} wiederhergestellt.", "erfolg",
                    jahr=_jahr_von(conn, kennung))


@router.post("/einnahmen/{einnahme_id}/datei")
async def datei_anhaengen(einnahme_id: str, request: Request,
                          datei: UploadFile | None = File(None),
                          conn: sqlite3.Connection = Depends(hole_conn)):
    kennung = eingabe.kennung(einnahme_id)
    try:
        if kennung is None:
            raise einnahmen.EinnahmeFehler("Diese Einnahme gibt es nicht.")
        abrechnung = await _datei(datei)
        if abrechnung is None:
            raise einnahmen.EinnahmeFehler("Bitte eine Datei wählen.")
        einnahmen.datei_anhaengen(conn, _datenordner(request), kennung, abrechnung)
    except einnahmen.EinnahmeFehler as fehler:
        return _meldung(str(fehler), jahr=_jahr_von(conn, kennung))
    return _meldung(f"Datei an Einnahme {kennung} angehängt (schreibgeschützt, mit Prüfsumme).",
                    "erfolg", jahr=_jahr_von(conn, kennung))


@router.get("/einnahmen/{einnahme_id}/datei")
def datei_zeigen(einnahme_id: str, request: Request, download: str = "",
                 conn: sqlite3.Connection = Depends(hole_conn)):
    kennung = eingabe.kennung(einnahme_id)
    zeile = conn.execute("SELECT * FROM einnahmedatei WHERE einnahme_id = ? ORDER BY id LIMIT 1",
                         (kennung,)).fetchone() if kennung else None
    if zeile is None:
        return _meldung("Zu dieser Einnahme gibt es keine Datei.")
    pfad = pfade.absolut(_datenordner(request), zeile["pfad"])
    if not pfad.is_file():
        return _meldung(f"Die Datei von Einnahme {kennung} fehlt im Datenordner.")
    # Wie die Datei eines Belegs: PDF und Bilder zeigt der Browser, eine
    # Mail kommt als sichere Textansicht, XML als Klartext ohne Skript.
    return dateiantwort(request, pfad, zeile["original_name"], zeile["mime"], download,
                        f"/einnahmen/{kennung}/datei?download=1")


@router.post("/einnahmen/zahler")
def zahler_einstufen(schluessel: str = Form(""), kategorie_id: str = Form(""),
                     conn: sqlite3.Connection = Depends(hole_conn)):
    """Aendert die gemerkte Einstufung eines Zahlers -- fuer kuenftige Einnahmen."""
    zeile = einnahmen.gemerkt(conn, schluessel.strip())
    if zeile is None:
        return _meldung("Diesen Zahler gibt es nicht.", anker="zahler")
    kategorie = eingabe.kennung(kategorie_id)
    katzeile = (conn.execute("SELECT * FROM euer_kategorie WHERE id = ?",
                             (kategorie,)).fetchone() if kategorie else None)
    if katzeile is None or katzeile["bereich"] != kategorien.BEREICH_EINNAHMEN:
        return _meldung("Bitte eine Einnahmenkategorie wählen.", anker="zahler")
    conn.execute("BEGIN IMMEDIATE")
    try:
        einnahmen.merken(conn, zeile["name"], katzeile["id"], schluessel=zeile["schluessel"])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _meldung(f"{zeile['name']}: künftig „{katzeile['bezeichnung']}“. Schon erfasste "
                    "Einnahmen bleiben, wie sie sind — einzeln unter „Ändern“ umstellen.",
                    "erfolg", anker="zahler")

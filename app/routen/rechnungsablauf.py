"""Rechnungsablauf: korrigieren, vor dem Versand zuruecknehmen, Versandvermerk
zuruecknehmen, Zahlung zuruecknehmen, Kundensatz im Entwurf, Nummernluecken.

Die Logik steht in app/logik/rechnungsablauf.py. Hier stehen nur Eingabe,
Meldung und Umleitung -- nach demselben Muster wie in rechnungen.py: jede
Ablehnung kommt als deutscher Satz auf der Seite an, nie als Serverfehler.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.logik import rechnungsablauf as ablauf
from app.logik.festschreiben import FestschreibenFehler
from app.logik.rundung import euro
from app.routen.rechnungen import (
    NICHT_VORHANDEN,
    _datenordner,
    _hinweis,
    _meldung,
    _vorhanden,
)
from app.web import VORLAGEN, hole_conn

router = APIRouter()


def _datum_de(iso: str | None) -> str:
    return ablauf._datum_de(iso)


@router.post("/rechnung/{rechnung_id}/korrigieren")
def korrigieren(
    rechnung_id: int,
    request: Request,
    # Default statt Form(...): sonst antwortet FastAPI mit seiner generischen
    # Eingabefehlerseite, bevor die eigene Pruefung die deutsche Meldung liefert.
    grund: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Storno plus Ersatzentwurf; danach steht der Nutzer im neuen Entwurf."""
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    versendet = conn.execute(
        "SELECT versendet_am FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()["versendet_am"]
    try:
        k = ablauf.korrigieren(conn, rechnung_id, grund, belegordner=_datenordner(request))
    except FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))

    ziel = f"/rechnung/{k.entwurf_id}"
    if k.storno.pdf_fehler:
        # Wie beim reinen Storno: die Korrektur gilt, nur die Datei fehlt --
        # und der Grund soll hier stehen. Nachholen laesst sie sich auf der
        # Seite der Rechnungskorrektur.
        return _meldung(
            ziel,
            f"{k.original_nummer} ist storniert (Rechnungskorrektur {k.storno.nummer}) und "
            f"dieser Entwurf angelegt, aber das PDF der Korrektur konnte nicht erzeugt "
            f"werden: {k.storno.pdf_fehler}",
        )
    text = (
        f"{k.original_nummer} ist storniert (Rechnungskorrektur {k.storno.nummer}). Das hier "
        "ist der neue Entwurf mit denselben Positionen, Texten und Zeiten — ändern, prüfen, "
        "festschreiben."
    )
    if versendet:
        text += (f" {k.original_nummer} war schon beim Kunden: die Rechnungskorrektur "
                 f"{k.storno.nummer} gehört mit der neuen Rechnung zu ihm.")
    if k.bezahlt_cent > 0:
        text += (f" Die Zahlung über {euro(k.bezahlt_cent)} € bleibt bei {k.original_nummer}; "
                 "dort steht jetzt „Rückzahlung offen“.")
    return _hinweis(ziel, text)


@router.post("/rechnung/{rechnung_id}/zuruecknehmen")
def zuruecknehmen(
    rechnung_id: int,
    request: Request,
    grund: str = Form(""),
    bestaetigt: str = Form(""),
    variante: str = Form(ablauf.ZURUECK_ZUM_ENTWURF),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Eine festgeschriebene, nie versendete Rechnung zuruecknehmen."""
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        r = ablauf.zuruecknehmen(
            conn, rechnung_id, grund, bestaetigt=bestaetigt == "1", variante=variante,
            datenordner=_datenordner(request),
        )
    except FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))

    if r.nummer_frei:
        nummer = f"Die Nummer {r.nummer} ist wieder frei — die nächste Rechnung bekommt sie."
    else:
        nummer = (f"Die Nummer {r.nummer} bleibt als Lücke mit deinem Grund vermerkt"
                  + (f" (nach ihr wurde schon {r.nachfolger} vergeben)." if r.nachfolger
                     else "."))
    pdf = (" Das PDF ist nicht gelöscht: es liegt als „VERWORFEN“ unter "
           "ausgangsrechnungen/verworfen/." if r.pdf_verworfen else "")
    if r.variante == ablauf.GANZ_LOESCHEN:
        zeiten = ("" if not r.zeiten else "; 1 Zeit ist wieder frei" if r.zeiten == 1
                  else f"; {r.zeiten} Zeiten sind wieder frei")
        return _hinweis("/rechnungen", f"{r.nummer} ist gelöscht{zeiten}. {nummer}{pdf}")
    return _hinweis(f"/rechnung/{rechnung_id}",
                    f"{r.nummer} ist zurückgenommen und wieder ein Entwurf. {nummer}{pdf}")


@router.post("/rechnung/{rechnung_id}/versendet/zuruecknehmen")
def versand_zuruecknehmen(
    rechnung_id: int,
    grund: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        bisher = ablauf.versand_zuruecknehmen(conn, rechnung_id, grund)
    except FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    return _hinweis(
        f"/rechnung/{rechnung_id}",
        f"Der Versandvermerk vom {_datum_de(bisher)} ist zurückgenommen; die Rechnung gilt "
        "wieder als nicht versendet.",
    )


@router.post("/rechnung/{rechnung_id}/zahlung/{zahlung_id}/zuruecknehmen")
def zahlung_zuruecknehmen(
    rechnung_id: int,
    zahlung_id: int,
    grund: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        z = ablauf.zahlung_zuruecknehmen(conn, rechnung_id, zahlung_id, grund)
    except FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    text = (f"Die Zahlung vom {_datum_de(z.datum)} über {euro(z.betrag_cent)} € ist "
            "zurückgenommen — die Gegenbuchung trägt dasselbe Datum, beide zusammen ergeben "
            "null.")
    if z.warnung:
        text += " " + z.warnung
    return _hinweis(f"/rechnung/{rechnung_id}", text)


@router.post("/rechnung/{rechnung_id}/satz")
def kundensatz_uebernehmen(
    rechnung_id: int, conn: sqlite3.Connection = Depends(hole_conn)
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        alt, neu = ablauf.kundensatz_uebernehmen(conn, rechnung_id)
    except FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    vorher = ", ".join(f"{euro(c)} €" for c in alt)
    return _hinweis(
        f"/rechnung/{rechnung_id}",
        f"Die Stundenpositionen rechnen jetzt mit dem Kundensatz {euro(neu)} € "
        f"(vorher {vorher}) und sind neu berechnet.",
    )


@router.get("/rechnungen/luecken", response_class=HTMLResponse)
def nummernluecken(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Alle verbrauchten Nummern ohne gueltige Rechnung, je Jahr, mit Grund."""
    luecken = ablauf.nummernluecken(conn)
    jahre: dict[int, list[ablauf.Luecke]] = {}
    for luecke in luecken:
        jahre.setdefault(luecke.jahr, []).append(luecke)
    return VORLAGEN.TemplateResponse(
        request,
        "rechnung_luecken.html",
        {"jahre": dict(sorted(jahre.items(), reverse=True))},
    )

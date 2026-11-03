"""Die Einnahmenüberschussrechnung als Seite.

Die Zahlen stehen auch im Excel-Export; hier stehen sie ohne Umweg über ein
Tabellenprogramm — und in der Reihenfolge, in der die Anlage EÜR sie abfragt.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.logik import angaben
from app.logik import euer as logik
from app.web import VORLAGEN, betrieb_oder_none, filterzahl, hole_conn

router = APIRouter()


@router.get("/euer", response_class=HTMLResponse)
def ansicht(
    request: Request,
    jahr: str = "",
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)

    jahre = logik.jahre(conn)
    # Leer oder Unsinn in der Adresse heisst: Standardjahr (wie bei /belege).
    # Das ist das laufende Jahr und nicht das juengste der Liste: die fuehrt
    # auch kuenftige AfA-Planjahre, und die Seite oeffnete sonst in 2028 mit
    # nichts als Abschreibung (logik.standardjahr).
    gewaehlt = filterzahl(jahr) or logik.standardjahr(jahre, int(db.heute()[:4]))
    return VORLAGEN.TemplateResponse(
        request,
        "euer.html",
        {
            "jahre": jahre,
            "jahr": gewaehlt,
            "auswertung": logik.auswertung(conn, gewaehlt),
            "luecke": logik.fehlende_zuordnung(conn, gewaehlt),
            # Belege mit Anlagegut-Haken ohne Verzeichniseintrag: weder
            # Ausgabe noch AfA -- die Seite muss sagen, dass etwas fehlt.
            "anlage_offen": logik.anlagegut_ohne_verzeichnis(conn, gewaehlt),
            # Automatisch gebucht und bezahlt, aber noch nicht bestaetigt.
            "unbestaetigt": logik.unbestaetigt_bezahlt(conn, gewaehlt),
            # Anlagegueter aus WISO ohne Anschaffungsdaten: keine AfA im Tool,
            # der WISO-Wert steht im Hinweis (app/logik/angaben.py).
            "afa_fehlt": angaben.fehlende_afa(conn, gewaehlt),
        },
    )

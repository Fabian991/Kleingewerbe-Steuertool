"""Leistungskatalog und Aenderungsprotokoll.

Zwei Ansichten, die beide etwas sichtbar machen, was bisher nur in der
Datenbank lag: die Titel, von denen die Gruppierung abhaengt, und das Journal,
das ohne Ansicht niemand liest.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.logik import journal_export
from app.logik import leistungen as logik
from app.logik.kunden import BetragZuGross, euro_nach_cent
from app.web import VORLAGEN, hole_conn

router = APIRouter()


def _zurueck(ziel: str, text: str = "", art: str = "fehler") -> RedirectResponse:
    if not text:
        return RedirectResponse(ziel, status_code=303)
    return RedirectResponse(f"{ziel}?{art}={quote(text)}", status_code=303)


# ------------------------------------------------------------ Leistungskatalog

#: Obergrenze fuer einen Katalogpreis. Eine Zahl mit zwanzig Stellen passt nicht
#: in die Datenbank (OverflowError, vorher ein HTTP 500) -- und ein Kleingewerbe
#: hat keine Leistung ueber einer Million Euro im Katalog.
PREIS_HOECHSTENS_CENT = 1_000_000 * 100
PREIS_ZU_GROSS = "Ein Preis über 1.000.000 € gehört nicht in den Katalog — bitte prüfen."


def _preisfehler(eingabe: str, fehler: Exception) -> str:
    """Die Meldung fuer einen Preis, den euro_nach_cent() nicht annimmt.

    Ein Riesenbetrag scheitert schon an der allgemeinen Obergrenze von
    euro_nach_cent() ("unplausibel groß"); fuer den Katalog gilt die engere
    Grenze, und die soll die Meldung nennen -- nicht "kein Betrag".

    >>> _preisfehler("9" * 20, BetragZuGross("„…“ ist als Betrag unplausibel groß."))
    'Ein Preis über 1.000.000 € gehört nicht in den Katalog — bitte prüfen.'
    >>> _preisfehler("abc", ArithmeticError("„abc“ ist kein Betrag."))
    '„abc“ ist kein Betrag.'
    """
    if isinstance(fehler, BetragZuGross):
        return PREIS_ZU_GROSS
    return f"„{eingabe}“ ist kein Betrag."


def _schon_im_katalog(titel: str) -> str:
    """Die Meldung fuer einen Titel, den der Katalog schon kennt.

    >>> _schon_im_katalog(" Beratung ")
    '„Beratung“ steht schon im Katalog — auch mit anderer Groß- und Kleinschreibung.'
    """
    return (f"„{titel.strip()}“ steht schon im Katalog — auch mit anderer "
            "Groß- und Kleinschreibung.")


@router.get("/leistungen", response_class=HTMLResponse)
def leistungen(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    return VORLAGEN.TemplateResponse(
        request,
        "leistungen.html",
        {
            "leistungen": logik.katalog(conn),
            "unbekannt": logik.unbekannte_titel(conn),
            "einheiten": logik.EINHEITEN,
            "fehler": request.query_params.get("fehler"),
            "erfolg": request.query_params.get("erfolg"),
        },
    )


@router.post("/leistungen")
def leistung_anlegen(
    titel: str = Form(""),
    beschreibung_vorlage: str = Form(""),
    einheit: str = Form("HUR"),
    preis_euro: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not titel.strip():
        return _zurueck("/leistungen", "Der Titel fehlt.")
    if einheit not in logik.EINHEITEN:
        return _zurueck("/leistungen", "Unbekannte Einheit.")
    # Decimal wirft InvalidOperation (ein ArithmeticError), nicht ValueError.
    try:
        preis = euro_nach_cent(preis_euro)
    except (ValueError, ArithmeticError) as fehler:
        return _zurueck("/leistungen", _preisfehler(preis_euro, fehler))
    if preis is not None and preis < 0:
        return _zurueck("/leistungen", "Ein negativer Preis gehört nicht in den Katalog.")
    if preis is not None and preis > PREIS_HOECHSTENS_CENT:
        return _zurueck("/leistungen", PREIS_ZU_GROSS)

    # Die Duplikatpruefung gehoert IN die Transaktion: vorher pruefte sie
    # davor, und bei einem Doppelklick sahen beide Anfragen "noch frei" -- die
    # zweite scheiterte am UNIQUE-Index mit einem HTTP 500.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if logik.finde(conn, titel) is not None:
            conn.execute("ROLLBACK")
            return _zurueck("/leistungen", _schon_im_katalog(titel))
        cur = conn.execute(
            "INSERT INTO leistung (titel, beschreibung_vorlage, einheit, preis_cent)"
            " VALUES (?, ?, ?, ?)",
            (titel.strip(), beschreibung_vorlage.strip() or None, einheit, preis),
        )
        db.protokolliere(conn, "ANLEGEN", "leistung", cur.lastrowid,
                         wert_neu={"titel": titel.strip()})
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        if "UNIQUE" not in str(fehler):
            raise
        return _zurueck("/leistungen", _schon_im_katalog(titel))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck("/leistungen")


@router.post("/leistungen/nachtragen")
def nachtragen(conn: sqlite3.Connection = Depends(hole_conn)):
    """Uebernimmt alle in der Zeiterfassung benutzten Titel in den Katalog.

    Muss VOR der Route mit dem Platzhalter stehen: FastAPI prueft in der
    Reihenfolge der Registrierung, und ``/leistungen/{leistung_id}`` wuerde
    "nachtragen" als Zahl lesen wollen.
    """
    offen = logik.unbekannte_titel(conn)
    if not offen:
        return _zurueck("/leistungen", "Der Katalog kennt alle benutzten Titel.",
                        art="erfolg")

    conn.execute("BEGIN IMMEDIATE")
    try:
        angelegt = 0
        for zeile in offen:
            if logik.merke(conn, zeile["titel"]) is not None:
                angelegt += 1
        db.protokolliere(conn, "NACHTRAGEN", "leistung", None,
                         wert_neu={"titel": angelegt})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck("/leistungen", f"{angelegt} Titel aus der Zeiterfassung übernommen.",
                    art="erfolg")


@router.post("/leistungen/{leistung_id}")
def leistung_speichern(
    leistung_id: int,
    titel: str = Form(""),
    beschreibung_vorlage: str = Form(""),
    einheit: str = Form("HUR"),
    preis_euro: str = Form(""),
    aktiv: str = Form("0"),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not titel.strip():
        return _zurueck("/leistungen", "Der Titel fehlt.")
    try:
        preis = euro_nach_cent(preis_euro)
    except (ValueError, ArithmeticError) as fehler:
        return _zurueck("/leistungen", _preisfehler(preis_euro, fehler))
    if preis is not None and preis < 0:
        return _zurueck("/leistungen", "Ein negativer Preis gehört nicht in den Katalog.")
    if preis is not None and preis > PREIS_HOECHSTENS_CENT:
        return _zurueck("/leistungen", PREIS_ZU_GROSS)

    # Lesen und Pruefen in der Transaktion, wie beim Anlegen: zwei Tabs, die
    # gleichzeitig auf denselben Titel umbenennen, endeten sonst im UNIQUE-Index.
    conn.execute("BEGIN IMMEDIATE")
    try:
        alt = conn.execute("SELECT * FROM leistung WHERE id = ?", (leistung_id,)).fetchone()
        if alt is None:
            conn.execute("ROLLBACK")
            return _zurueck("/leistungen", "Diese Leistung gibt es nicht mehr.")
        doppelt = logik.finde(conn, titel)
        if doppelt is not None and doppelt["id"] != leistung_id:
            conn.execute("ROLLBACK")
            return _zurueck("/leistungen", _schon_im_katalog(titel))
        conn.execute(
            "UPDATE leistung SET titel = ?, beschreibung_vorlage = ?, einheit = ?,"
            " preis_cent = ?, aktiv = ? WHERE id = ?",
            (
                titel.strip(), beschreibung_vorlage.strip() or None,
                einheit if einheit in logik.EINHEITEN else alt["einheit"], preis,
                1 if aktiv == "1" else 0, leistung_id,
            ),
        )
        if alt["titel"] != titel.strip():
            # Umbenennen aendert die Zeiteintraege NICHT: ihr Titel ist die
            # Aufzeichnung dessen, was erfasst wurde. Die Rechnung traegt
            # ohnehin die Bezeichnung aus der Position, nicht aus dem Katalog.
            db.protokolliere(conn, "AENDERN", "leistung", leistung_id, feld="titel",
                             wert_alt=alt["titel"], wert_neu=titel.strip())
        if bool(alt["aktiv"]) != (aktiv == "1"):
            db.protokolliere(conn, "AENDERN", "leistung", leistung_id, feld="aktiv",
                             wert_alt=alt["aktiv"], wert_neu=1 if aktiv == "1" else 0)
        # Preis, Einheit und Vorlage ebenso: eine Preisaenderung 800 -> 950 EUR
        # ist ein Vorgang, und das Journal verspricht, vollstaendig zu sein.
        neue_einheit = einheit if einheit in logik.EINHEITEN else alt["einheit"]
        neue_vorlage = beschreibung_vorlage.strip() or None
        for feld, wert_alt, wert_neu in (
            ("preis_cent", alt["preis_cent"], preis),
            ("einheit", alt["einheit"], neue_einheit),
            ("beschreibung_vorlage", alt["beschreibung_vorlage"], neue_vorlage),
        ):
            if wert_alt != wert_neu:
                db.protokolliere(conn, "AENDERN", "leistung", leistung_id, feld=feld,
                                 wert_alt=wert_alt, wert_neu=wert_neu)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        if "UNIQUE" not in str(fehler):
            raise
        return _zurueck("/leistungen", _schon_im_katalog(titel))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck("/leistungen")


# ------------------------------------------------------------------- Journal

@router.get("/journal", response_class=HTMLResponse)
def journal(
    request: Request,
    objekttyp: str | None = None,
    aktion: str | None = None,
    conn: sqlite3.Connection = Depends(hole_conn),
):
    typen, aktionen = journal_export.arten(conn)
    ordner = _datenordner(request)
    spiegel = ordner / journal_export.DATEINAME
    return VORLAGEN.TemplateResponse(
        request,
        "journal.html",
        {
            "zeilen": journal_export.zeilen(conn, objekttyp=objekttyp, aktion=aktion),
            "typen": typen,
            "aktionen": aktionen,
            "filter": {"objekttyp": objekttyp, "aktion": aktion},
            "gesamt": conn.execute("SELECT COUNT(*) AS c FROM journal").fetchone()["c"],
            "spiegel": spiegel,
            "spiegel_zeilen": journal_export.spiegel_eintraege(spiegel),
            "erfolg": request.query_params.get("erfolg"),
            "fehler": request.query_params.get("fehler"),
        },
    )


@router.post("/journal/export")
def journal_schreiben(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    try:
        ziel = journal_export.schreibe(conn, _datenordner(request) / journal_export.DATEINAME)
    except OSError as fehler:
        return _zurueck("/journal", f"Der Spiegel ließ sich nicht schreiben: {fehler}")
    return _zurueck("/journal", f"{ziel} geschrieben.", art="erfolg")


def _datenordner(request: Request) -> Path:
    pfad = request.app.state.datenpfad
    return Path(pfad).parent if pfad else db.standard_datenordner()

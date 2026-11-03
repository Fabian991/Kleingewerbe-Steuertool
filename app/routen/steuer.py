"""Steuerrücklage: wie viel vom Gewinn für die Einkommensteuer zur Seite gehört.

Eine Schätzung, keine Buchführung: die Seite kennt den Gewinn aus der EÜR,
alles andere (Gehalt, Veranlagung, Kirchensteuer) gibt der Nutzer an. Die
Angaben werden je Jahr gespeichert, das Ergebnis bei jedem Aufruf neu
gerechnet -- eine spätere Buchung ändert es sofort mit.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from fractions import Fraction
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.filter import prozent
from app.logik import angaben, eingabe, euer, steuertarif
from app.logik import steuerschaetzung as logik
from app.logik.rundung import euro
from app.web import VORLAGEN, betrieb_oder_none, filterzahl, hole_conn

router = APIRouter()


def _jahre(conn: sqlite3.Connection, laufend: int) -> list[int]:
    """Waehlbar: Jahre mit Zahlungen oder AfA, Jahre mit Angaben, das laufende und das naechste.

    Das naechste Jahr, weil man fuer das kommende Jahr planen will ("wie viel
    lege ich 2027 je Monat zurueck?") -- und weil es dort einen Tarifentwurf
    geben kann. Kuenftige AfA-Planjahre weiter hinten bleiben aussen vor.
    """
    jahre = {j for j in euer.jahre(conn) if j <= laufend + 1}
    jahre |= set(logik.gespeicherte_jahre(conn)) | {laufend, laufend + 1}
    return sorted(jahre, reverse=True)


def _gewaehltes_jahr(roh: str, jahre: list[int], laufend: int) -> int:
    """Das Jahr aus der Adresse; Unsinn oder ein unplausibles Jahr heisst Standardjahr."""
    jahr = filterzahl(roh)
    if jahr is None or not eingabe.JAHR_MINDESTENS <= jahr <= laufend + 1:
        return euer.standardjahr(jahre, laufend)
    return jahr


def _formularwerte(e: logik.Eingaben, stand: date) -> dict[str, str]:
    """Gespeicherte Angaben als Feldinhalte, so geschrieben, wie man sie tippt.

    Die Haken der Raten zeigen den Stand an ``stand``: faellige gelten als
    gezahlt, kuenftige nicht -- es sei denn, ein gespeicherter Haken sagt
    etwas anderes.
    """

    def betrag(cent: int | None) -> str:
        return "" if cent is None else euro(cent)

    satz = e.satz_manuell_promille
    raten = {(r.empfaenger, r.quartal): r for r in e.raten}
    vz: dict[str, str] = {"vz_stand": stand.isoformat()}
    for empfaenger in logik.EMPFAENGER:
        for quartal in range(1, 5):
            rate = raten.get((empfaenger, quartal))
            vz[logik.raten_feld(empfaenger, quartal)] = betrag(rate.betrag_cent if rate else None)
            regel = logik.nach_faelligkeit_gezahlt(e.jahr, empfaenger, quartal, stand)
            gezahlt = regel if rate is None or rate.gezahlt is None else rate.gezahlt
            vz[_haken(empfaenger, quartal)] = "1" if gezahlt else ""
    return vz | {
        "brutto_lohn": betrag(e.brutto_lohn_cent),
        "partner_brutto": betrag(e.partner_brutto_cent),
        "zve_ohne": betrag(e.zve_ohne_cent),
        "veranlagung": e.veranlagung,
        "kirchensteuer": str(e.kirchensteuer_prozent),
        "gewinn_erwartet": betrag(e.gewinn_erwartet_cent),
        "vorauszahlungen": betrag(e.vorauszahlungen_cent) if e.vorauszahlungen_cent else "",
        "hebesatz": str(e.hebesatz_prozent),
        "satz_manuell": "" if satz is None else prozent(Fraction(satz, 1000), 1).removesuffix(",0"),
        "tarif_entwurf": "1" if e.tarif_entwurf else "",
    }


def _haken(empfaenger: str, quartal: int) -> str:
    """Schluessel des Hakens "gezahlt" einer Rate im Formular-Woerterbuch."""
    return f"vz_gezahlt_{logik.raten_schluessel(empfaenger, quartal)}"


def _raten_felder(jahr: int, formular: dict[str, str], stand: date) -> dict[str, list[dict]]:
    """Die acht Raten fuer das Formular: Termin, Faelligkeit, Betrag, Haken."""
    felder: dict[str, list[dict]] = {}
    for empfaenger in logik.EMPFAENGER:
        felder[empfaenger] = [
            {
                "quartal": quartal,
                "feld": logik.raten_feld(empfaenger, quartal),
                "schluessel": logik.raten_schluessel(empfaenger, quartal),
                "faellig": logik.faellig_am(jahr, empfaenger, quartal),
                "verschoben": logik.faellig_am(jahr, empfaenger, quartal)
                != date(jahr, *logik.TERMINE[empfaenger][quartal - 1]),
                # Am Tag, an dem das Formular gezeigt wird, schon faellig?
                "faellig_vorbei": logik.nach_faelligkeit_gezahlt(jahr, empfaenger, quartal,
                                                                 stand),
                "betrag": formular.get(logik.raten_feld(empfaenger, quartal), ""),
                "angehakt": bool(formular.get(_haken(empfaenger, quartal))),
            }
            for quartal in range(1, 5)
        ]
    return felder


def _stand(roh: str) -> date:
    """Der Tag, an dem das Formular gezeigt wurde; Unsinn oder Zukunft heisst heute."""
    heute = date.fromisoformat(db.heute())
    try:
        stand = date.fromisoformat((roh or "").strip())
    except ValueError:
        return heute
    return stand if stand <= heute else heute


def _seite(
    request: Request, conn: sqlite3.Connection, jahr: int, *, eingaben: logik.Eingaben | None,
    formular: dict[str, str] | None = None, fehler: str | None = None,
    fehler_feld: str | None = None, status_code: int = 200,
) -> HTMLResponse:
    heute = date.fromisoformat(db.heute())
    gespeichert = logik.laden(conn, jahr)
    # Nach einem Eingabefehler (422) kein Ergebnis: es kaeme aus den zuletzt
    # gespeicherten Angaben und passte nicht zu dem, was im Formular steht --
    # etwa ohne die gerade gewaehlte Kirchensteuer.
    rechnen_mit = None if fehler else (eingaben if eingaben is not None else gespeichert)
    ergebnis = logik.berechne(conn, jahr, rechnen_mit, heute=heute) if rechnen_mit else None
    auswertung = euer.auswertung(conn, jahr)
    if formular is None:
        formular = _formularwerte(rechnen_mit or logik.Eingaben(jahr=jahr), heute)
    stand = _stand(formular.get("vz_stand", ""))
    raten_felder = _raten_felder(jahr, formular, stand)
    # Die Gemeinde-Raten bleiben eingeklappt, solange es keine Gewerbesteuer
    # gibt und keine Rate eingetragen ist -- vier leere Felder waeren auf dem
    # Handy nur Ballast.
    gemeinde_offen = bool(
        (ergebnis is not None and ergebnis.gewst_cent > 0)
        or any(r["betrag"].strip() for r in raten_felder["GEMEINDE"])
        or (fehler_feld or "").startswith("vz_gemeinde")
    )
    # "Beträge aus dem Vorjahr übernehmen": ein Bescheid gilt fort, bis ein
    # neuer kommt. Gefuellt wird nur das Formular; gespeichert erst mit dem Knopf.
    vorjahr = logik.laden(conn, jahr - 1)
    vorjahr_werte = {
        logik.raten_feld(r.empfaenger, r.quartal): euro(r.betrag_cent)
        for r in (vorjahr.raten if vorjahr else ())
    }
    # Tarif, Stufen und der Text darueber aus derselben Quelle: dem Ergebnis,
    # sonst dem Formular.
    zusammen = ergebnis.eingaben.zusammen if ergebnis else (
        formular.get("veranlagung") == "ZUSAMMEN")
    entwurf = formular.get("tarif_entwurf") in ("1", "on", "ja")
    tarif = ergebnis.tarif if ergebnis else steuertarif.tarif_fuer(jahr, entwurf=entwurf)
    gw = steuertarif.gewerbesteuer_werte()
    return VORLAGEN.TemplateResponse(
        request,
        "steuer.html",
        {
            "jahr": jahr,
            "jahre": _jahre(conn, heute.year),
            "laufendes_jahr": jahr == heute.year,
            "heute": heute,
            "formular": formular,
            "ergebnis": ergebnis,
            "fehlt": rechnen_mit.fehlt if rechnen_mit else None,
            "eingabefehler": bool(fehler),
            "beispiel": None if ergebnis else logik.beispiel(jahr, heute=heute),
            "fehler_feld": fehler_feld,
            "gespeichert": gespeichert is not None,
            "geaendert_am": (conn.execute(
                "SELECT geaendert_am FROM steuerschaetzung WHERE jahr = ?", (jahr,)
            ).fetchone() or [None])[0],
            "auswertung": auswertung,
            # AfA, die nur in WISO steht (Angaben fehlen): der Gewinn ist zu hoch.
            "afa_fehlt": angaben.fehlende_afa(conn, jahr),
            "tarif": tarif,
            "stufen": ergebnis.stufen if ergebnis else tarif.stufen(faktor=2 if zusammen else 1),
            "zusammen": zusammen,
            "entwurf_verfuegbar": steuertarif.entwurf_verfuegbar(jahr),
            "hochrechnung": logik.hochrechnung(auswertung.gewinn_cent, jahr, heute),
            "hebesatz_vorschlag": int(gw["hebesatz_vorschlag"]),
            "hebesatz_quelle": gw["hebesatz_vorschlag_quelle"],
            "mindesthebesatz": steuertarif.mindesthebesatz(jahr),
            "gewst_freibetrag_cent": int(gw["freibetrag_euro"]) * 100,
            "kirchensteuer_saetze": steuertarif.kirchensteuer_saetze(),
            "soli_freigrenze": steuertarif.soli_freigrenze(jahr),
            "veranlagung": steuertarif.veranlagungswerte(),
            "raten_felder": raten_felder,
            "gemeinde_offen": gemeinde_offen,
            "vorjahr_werte": vorjahr_werte,
            "empfaenger_text": logik.EMPFAENGER_TEXT,
            "fehler": fehler or request.query_params.get("fehler"),
            "erfolg": None if fehler else request.query_params.get("erfolg"),
        },
        status_code=status_code,
    )


@router.get("/steuer", response_class=HTMLResponse)
def ansicht(
    request: Request,
    jahr: str = "",
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    laufend = int(db.heute()[:4])
    gewaehlt = _gewaehltes_jahr(jahr, _jahre(conn, laufend), laufend)
    return _seite(request, conn, gewaehlt, eingaben=None)


@router.post("/steuer")
def speichern(
    request: Request,
    jahr: str = Form(""),
    brutto_lohn: str = Form(""),
    partner_brutto: str = Form(""),
    zve_ohne: str = Form(""),
    veranlagung: str = Form("EINZEL"),
    kirchensteuer: str = Form("0"),
    gewinn_erwartet: str = Form(""),
    vorauszahlungen: str = Form(""),
    hebesatz: str = Form(""),
    satz_manuell: str = Form(""),
    tarif_entwurf: str = Form(""),
    vz_finanzamt_1: str = Form(""), vz_finanzamt_2: str = Form(""),
    vz_finanzamt_3: str = Form(""), vz_finanzamt_4: str = Form(""),
    vz_gemeinde_1: str = Form(""), vz_gemeinde_2: str = Form(""),
    vz_gemeinde_3: str = Form(""), vz_gemeinde_4: str = Form(""),
    vz_gezahlt: list[str] = Form(default=[]),
    vz_stand: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    laufend = int(db.heute()[:4])
    try:
        gewaehlt = eingabe.ganzzahl(jahr, feld="Jahr", mindestens=eingabe.JAHR_MINDESTENS,
                                    hoechstens=laufend + 1)
    except ValueError as fehler:
        ziel = f"/steuer?fehler={quote(str(fehler))}"
        return RedirectResponse(ziel, status_code=303)

    formular = {
        "brutto_lohn": brutto_lohn, "partner_brutto": partner_brutto, "zve_ohne": zve_ohne,
        "veranlagung": veranlagung, "kirchensteuer": kirchensteuer,
        "gewinn_erwartet": gewinn_erwartet, "vorauszahlungen": vorauszahlungen,
        "hebesatz": hebesatz, "satz_manuell": satz_manuell, "tarif_entwurf": tarif_entwurf,
        "vz_finanzamt_1": vz_finanzamt_1, "vz_finanzamt_2": vz_finanzamt_2,
        "vz_finanzamt_3": vz_finanzamt_3, "vz_finanzamt_4": vz_finanzamt_4,
        "vz_gemeinde_1": vz_gemeinde_1, "vz_gemeinde_2": vz_gemeinde_2,
        "vz_gemeinde_3": vz_gemeinde_3, "vz_gemeinde_4": vz_gemeinde_4,
    }
    stand = _stand(vz_stand)
    # Nach einer 422 stehen die Haken so da, wie sie abgeschickt wurden -- und
    # der Stand bleibt der Tag, an dem das Formular zuerst gezeigt wurde.
    formular["vz_stand"] = stand.isoformat()
    for empfaenger in logik.EMPFAENGER:
        for quartal in range(1, 5):
            angehakt = logik.raten_schluessel(empfaenger, quartal) in vz_gezahlt
            formular[_haken(empfaenger, quartal)] = "1" if angehakt else ""
    try:
        neu = logik.pruefe(gewaehlt, formular, gezahlt=vz_gezahlt, stand=stand)
    except logik.Eingabefehler as fehler:
        # Die Eingaben bleiben stehen: das Formular ist lang, und ein Tippfehler
        # in einem Feld soll nicht alle anderen wegwerfen. Das Feld selbst wird
        # markiert und bekommt den Fokus.
        return _seite(request, conn, gewaehlt, eingaben=None, formular=formular,
                      fehler=str(fehler), fehler_feld=fehler.feld, status_code=422)
    geaendert = logik.speichern(conn, neu)
    text = f"Angaben für {gewaehlt} {'gespeichert' if geaendert else 'unverändert'}."
    if not neu.rechenbar:
        text += f" Für eine Schätzung fehlt noch {neu.fehlt}."
    elif not neu.berechenbar:
        text += (f" Gerechnet wird mit deinem Satz; für einen berechneten Vergleich fehlt noch "
                 f"{neu.fehlt}.")
    return RedirectResponse(f"/steuer?jahr={gewaehlt}&erfolg={quote(text)}#ergebnis",
                            status_code=303)


@router.post("/steuer/loeschen")
def loeschen(
    jahr: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Die Angaben eines Jahres löschen; danach das Standardjahr, falls das Jahr wegfällt."""
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    laufend = int(db.heute()[:4])
    try:
        gewaehlt = eingabe.ganzzahl(jahr, feld="Jahr", mindestens=eingabe.JAHR_MINDESTENS,
                                    hoechstens=2100)
    except ValueError as fehler:
        return RedirectResponse(f"/steuer?fehler={quote(str(fehler))}", status_code=303)
    if logik.loeschen(conn, gewaehlt):
        text = f"Angaben für {gewaehlt} gelöscht."
    else:
        text = f"Für {gewaehlt} waren keine Angaben gespeichert."
    ziel = f"jahr={gewaehlt}&" if gewaehlt in _jahre(conn, laufend) else ""
    return RedirectResponse(f"/steuer?{ziel}erfolg={quote(text)}", status_code=303)

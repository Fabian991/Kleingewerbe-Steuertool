"""Der Eingang als Ganzes: Vorgaben, alles wegwerfen, alles pruefen und buchen.

Die einzelne Datei hat ihre Pruefmaske (app/routen/belege.py). Hier stehen die
Knoepfe fuer den ganzen Eingang -- fuer den Fall, der beim Ausprobieren und
bei der Nacherfassung eines Jahres der Normalfall ist: viele Dateien auf
einmal.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import db
from app.logik import kategorien
from app.logik.beleg import abholen, anhaengen, pipeline, sammelbuchung
from app.logik.beleg import bezahlt as bezahlvorgabe
from app.logik.eingabe import kennung
from app.routen.belege import _datenordner, _eingangsdatei, _meldung, _zahlwort
from app.web import VORLAGEN, betrieb_oder_none, hole_conn

router = APIRouter()

#: Wie viele Formularfelder "Alle prüfen und buchen" und "Alle wegwerfen"
#: annehmen. Die Vorschau schickt je Datei drei Felder (buchen, sha_<name>,
#: kategorie_<name>); Starlettes Vorgabe von 1000 Feldern endete schon bei
#: rund 330 Dateien -- genau bei der Nacherfassung eines Jahres -- mit einem
#: rohen englischen 400er, und nichts war gebucht. Die Grenze bleibt (Schutz
#: gegen ein endloses Formular), nur weit ueber jedem echten Eingang.
MAX_FORMULARFELDER = 50_000


@router.post("/belege/vorgaben")
def vorgaben_speichern(
    bezahlt: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Speichert "Meine Eingangsbelege sind in der Regel schon bezahlt".

    Mit Journal: die Einstellung entscheidet, ob kuenftige Buchungen eine
    Zahlung mitbringen -- und damit, in welchem EUeR-Jahr sie zaehlen.
    """
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    an = 1 if bezahlt == "1" else 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        alt = conn.execute(
            "SELECT belege_bezahlt_vorgabe FROM betrieb WHERE id = 1").fetchone()[0]
        if alt != an:
            conn.execute("UPDATE betrieb SET belege_bezahlt_vorgabe = ? WHERE id = 1", (an,))
            db.protokolliere(conn, "AENDERN", "betrieb", 1, feld="belege_bezahlt_vorgabe",
                             wert_alt=alt, wert_neu=an)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if an:
        text = ("Gespeichert: Eingangsbelege gelten als bezahlt — Zahltag = Belegdatum, in der "
                "Prüfmaske änderbar. Am Jahreswechsel (22.12. bis 10.01.) fragt die Prüfmaske.")
    else:
        text = ("Gespeichert: Eingangsbelege werden ohne Zahlung gebucht — die Zahlung in der "
                "Liste unter „Ändern“ erfassen, sonst zählt der Beleg in keinem EÜR-Jahr.")
    return _meldung("/belege", text, art="erfolg")


# ------------------------------------------------------------ Alle wegwerfen

@router.post("/belege/eingang/alle-wegwerfen")
async def alle_wegwerfen(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Wirft die Dateien weg, die die Seite im Eingang angezeigt hat.

    ``async`` nur fuer das Formular (ein Feld je Datei, mehr als Starlettes
    Vorgabe von 1000 erlaubt, siehe MAX_FORMULARFELDER); das Wegwerfen selbst
    laeuft im Threadpool.
    """
    async with request.form(max_fields=MAX_FORMULARFELDER) as formular:
        datei = [str(n) for n in formular.getlist("datei")]
    return await run_in_threadpool(_alle_wegwerfen, request, conn, datei)


def _alle_wegwerfen(request: Request, conn: sqlite3.Connection, datei: list[str]):
    """Wirft die Dateien weg, die die Seite im Eingang angezeigt hat.

    Genau die angezeigten Namen (versteckte Felder): was das Abholen zwischen
    Anzeigen und Klick abgelegt hat, bleibt liegen. Jeder Name geht durch die
    Pfadpruefung des Eingangs -- nie ins Archiv, nie in den Datenordner. Unter
    der Sperre des Abholens: buchte das gerade dieselbe Datei, waere sie
    danach weder gebucht noch im Eingang, ihre Quelle aber schon in
    „Übernommen“.

    Ein Journaleintrag fuer alle, mit Originalname und SHA-256 je Datei:
    gelöscht wird nur die Kopie im Tool, und der Eintrag erklaert spaeter, wo
    eine Datei geblieben ist.
    """
    datenordner = _datenordner(request)
    if not datei:
        return _meldung("/belege", "Im Eingang lag nichts zum Wegwerfen.", art="hinweis",
                        anker="eingang")
    if not abholen._LAUF.acquire(blocking=False):
        return _meldung("/belege", "Das Abholen aus dem Eingangsordner läuft gerade — nichts "
                                   "weggeworfen. In ein paar Sekunden noch einmal.",
                        art="hinweis", anker="eingang")
    try:
        weg: list[dict[str, str]] = []
        for name in dict.fromkeys(datei):
            pfad = _eingangsdatei(datenordner, name)
            if pfad is None:
                continue
            originalname = pipeline.original_name_von(pfad)
            try:
                sha256 = pipeline.pruefsumme(pfad)
            except OSError:
                continue  # gerade gebucht und verschoben
            if pipeline.eingang_wegwerfen(pfad):
                weg.append({"originalname": originalname, "sha256": sha256})
        if weg:
            sammelbuchung.verworfen_protokollieren(
                conn, weg, grund="„Alle Dateien wegwerfen“ im Eingang")
    finally:
        abholen._LAUF.release()
    if not weg:
        return _meldung("/belege", "Diese Dateien liegen nicht mehr im Eingang.", anker="eingang")
    return _meldung("/belege", f"{_zahlwort(len(weg), 'Datei', 'Dateien')} aus dem Eingang "
                               "weggeworfen.", art="erfolg", anker="eingang")


# ------------------------------------------------- Alle pruefen und buchen

def _wahl_lesen(werte, plaene_namen: set[str] | None = None) -> dict[str, int | None]:
    """Die Kategorien der Vorschau je Eingangsname: ``kategorie_<name>``.
    Leer heisst: ohne Kategorie; Unbrauchbares ebenso (gebucht wird nur mit
    einer Ausgabenkategorie, das prueft ``abholen.pruefe_automatisch``)."""
    wahl: dict[str, int | None] = {}
    for schluessel, roh in werte.items():
        if not schluessel.startswith("kategorie_"):
            continue
        name = schluessel.removeprefix("kategorie_")
        if plaene_namen is not None and name not in plaene_namen:
            continue
        wahl[name] = kennung(roh) if str(roh).strip() else None
    return wahl


def _ziel_lesen(roh: str) -> tuple[str, int] | str | None:
    """Die Wahl im Dropdown "zuordnen": ``("beleg", 12)``, ``("einnahme", 5)``,
    ``"neu"`` -- oder ``None`` fuer "liegen lassen" und Unbrauchbares.

    >>> _ziel_lesen("beleg:12"), _ziel_lesen("einnahme:5"), _ziel_lesen("neu")
    (('beleg', 12), ('einnahme', 5), 'neu')
    >>> _ziel_lesen("liegen"), _ziel_lesen("beleg:x"), _ziel_lesen("konto:1")
    (None, None, None)
    """
    roh = roh.strip()
    if roh == "neu":
        return roh
    art, _, nummer = roh.partition(":")
    if art not in (anhaengen.BELEG, anhaengen.EINNAHME):
        return None
    nummer_ = kennung(nummer)
    return (art, nummer_) if nummer_ is not None else None


def _bezahlt_lesen(werte, conn: sqlite3.Connection) -> bool:
    zahlung = werte.get("zahlung")
    if zahlung in ("bezahlt", "offen"):
        return zahlung == "bezahlt"
    return bezahlvorgabe.vorgabe(conn)


@router.get("/belege/alle-buchen", response_class=HTMLResponse)
def alle_buchen_vorschau(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Die Vorschau: je Datei "wird gebucht" (mit Kategorie zur Auswahl) oder
    "bleibt im Eingang" mit Grund. Schreibt nichts.

    "Vorschau aktualisieren" schickt die Auswahl (Kategorien, bezahlt/offen)
    als Abfrage hierher zurueck.
    """
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    werte = request.query_params
    wahl = _wahl_lesen(werte)
    bezahlt = _bezahlt_lesen(werte, conn)
    plaene = sammelbuchung.planen(conn, _datenordner(request), bezahlt=bezahlt,
                                  kategorien=wahl)
    # Warum eine Datei wartet, zeigt danach auch die Eingangsliste -- bis zum
    # naechsten Neustart (nur im Speicher; die Vorschau sagt es jederzeit neu).
    # Auch fuer die Dateien mit Treffern: "passt vielleicht zu Beleg 12 ...".
    request.app.state.eingang_gruende = {
        p.eingangsname: p.grund for p in plaene
        if p.status in (sammelbuchung.BLEIBT, sammelbuchung.ZUORDNEN) and p.grund}
    zuordnen = [p for p in plaene if p.status == sammelbuchung.ZUORDNEN]
    return VORLAGEN.TemplateResponse(request, "belege_sammel.html", {
        "plaene": plaene,
        "buchen": [p for p in plaene if p.status == sammelbuchung.BUCHEN],
        "zuordnen": zuordnen,
        "sichere": sum(1 for p in zuordnen if p.zuordnung and p.zuordnung.angehakt),
        "zuordnen_zusammenfassung": sammelbuchung.zuordnen_zusammenfassung(plaene),
        "bleiben": [p for p in plaene if p.status == sammelbuchung.BLEIBT],
        "dubletten": [p for p in plaene if p.status == sammelbuchung.DUBLETTE],
        "kategorien": kategorien.katalog(conn, bereich=kategorien.BEREICH_AUSGABEN),
        "wahl": wahl,
        "bezahlt": bezahlt,
        "fenster_tage": anhaengen.FENSTER_TAGE,
        "jahreswechsel_grund": bezahlvorgabe.JAHRESWECHSEL_GRUND,
        "vorjahr_warnung": sammelbuchung.vorjahr_warnung(plaene),
        "fehler": werte.get("fehler"),
    })


@router.post("/belege/alle-buchen")
async def alle_buchen(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Bucht, was die Vorschau als buchbar gezeigt hat und angehakt ist.

    Je Datei: ``buchen`` (Eingangsname), ``sha_<name>`` (Pruefsumme der
    Vorschau), ``kategorie_<name>``. Dazu ``wegwerfen`` fuer die vorgehakten
    Dubletten und ``zahlung`` (bezahlt/offen). Jede Datei wird unter der
    Sperre noch einmal gelesen und neu entschieden. Das Ergebnis steht danach
    auf der Belegseite; ein Doppelklick findet die Dateien nicht mehr vor oder
    die Sperre belegt -- doppelt gebucht wird nichts.

    Die Gruppe "zuordnen" (app/logik/beleg/anhaengen.py): ``zuordnen`` die
    angehakten Dateien, ``ziel_<name>`` die Wahl im Dropdown (``beleg:12``,
    ``einnahme:5``, ``neu``, ``liegen``). Mit ``nur_sichere`` ("Alle sicheren
    Zuordnungen übernehmen") zaehlen nur die Dateien, die die Vorschau als
    sicher einstufte (``stufe_<name>``), mit ihrem Vorschlag -- sonst nichts.
    Unter der Sperre wird neu geprueft, ob die Datei noch zu genau diesem
    Eintrag passt und er noch ohne Datei ist.
    """
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    formular = await request.form(max_fields=MAX_FORMULARFELDER)
    if formular.get("nur_sichere") == "1":
        namen, wegwerfen = [], []
        zuordnen = [str(n) for n in formular.getlist("zuordnen_alle")
                    if formular.get(f"stufe_{n}") == anhaengen.SICHER]
    else:
        namen = [str(n) for n in formular.getlist("buchen")]
        wegwerfen = [str(n) for n in formular.getlist("wegwerfen")]
        zuordnen = [str(n) for n in formular.getlist("zuordnen")]
    wahl = _wahl_lesen(formular, set(namen))
    auswahl = [sammelbuchung.Auswahl(name, str(formular.get(f"sha_{name}", "")),
                                     wahl.get(name, abholen.VORGABE))
               for name in dict.fromkeys(namen)]
    for name in dict.fromkeys(zuordnen):
        if name in namen:
            continue
        ziel = _ziel_lesen(str(formular.get(f"ziel_{name}", "")))
        sha = str(formular.get(f"sha_{name}", ""))
        if ziel == "neu":
            auswahl.append(sammelbuchung.Auswahl(name, sha, abholen.VORGABE, trotz_treffer=True))
        elif isinstance(ziel, tuple):
            auswahl.append(sammelbuchung.Auswahl(
                name, sha, None, anhaengen_an=ziel[1] if ziel[0] == anhaengen.BELEG else None,
                einnahme_an=ziel[1] if ziel[0] == anhaengen.EINNAHME else None))
    if not auswahl and not wegwerfen:
        return _meldung("/belege/alle-buchen", "Nichts ausgewählt — es wurde nichts gebucht.")
    try:
        # Im Threadpool: das Lesen und Buchen dauert je Datei einen Moment und
        # soll den Server nicht anhalten (``async`` nur fuer das Formular).
        ergebnis = await run_in_threadpool(
            sammelbuchung.buchen, conn, _datenordner(request), auswahl,
            bezahlt=_bezahlt_lesen(formular, conn), wegwerfen=wegwerfen)
    except abholen.LaeuftSchon:
        return _meldung("/belege", "Das Abholen aus dem Eingangsordner läuft gerade — nichts "
                                   "gebucht. In ein paar Sekunden noch einmal „Alle prüfen und "
                                   "buchen“.", art="hinweis", anker="eingang")
    request.app.state.sammelbuchung = ergebnis
    gruende = dict(getattr(request.app.state, "eingang_gruende", None) or {})
    gruende.update({b.eingangsname: b.grund for b in ergebnis.geblieben if b.grund})
    request.app.state.eingang_gruende = gruende
    return RedirectResponse("/belege?sammel=1#sammelergebnis", status_code=303)


# ----------------------------------------------------- Alle bestaetigen

@router.post("/belege/geprueft-alle")
def alle_bestaetigen(
    beleg: list[str] = Form([]),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """„Stimmt so“ fuer alle angezeigten, noch nicht bestaetigten Belege.

    Genau die Kennungen, die die Liste unter „nur ungeklärte“ gezeigt hat.
    Wie beim einzelnen „Stimmt so“ wird nur ``geprueft_am`` gesetzt -- und nur,
    wo es leer ist (ein Doppelklick schreibt nichts doppelt) --, mit einem
    Journaleintrag BELEG_GEPRUEFT je Beleg. Ein inzwischen geloeschter Beleg
    (anderer Tab, veraltete Liste) bleibt unberuehrt: der Trigger
    beleg_geloescht_unveraenderlich braeche sonst die ganze Transaktion mit
    einem Serverfehler ab.
    """
    kennungen = [k for k in (kennung(roh) for roh in dict.fromkeys(beleg)) if k is not None]
    if not kennungen:
        return _meldung("/belege?offen=1", "Keine Belege ausgewählt.")
    zeitpunkt = db.jetzt_utc()
    bestaetigt = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for beleg_id in kennungen:
            if conn.execute(
                "UPDATE beleg SET geprueft_am = ?, version = version + 1"
                " WHERE id = ? AND geprueft_am IS NULL AND geloescht_am IS NULL",
                (zeitpunkt, beleg_id),
            ).rowcount:
                db.protokolliere(conn, "BELEG_GEPRUEFT", "beleg", beleg_id, feld="geprueft_am",
                                 wert_neu=zeitpunkt,
                                 begruendung="„Alle angezeigten bestätigen“")
                bestaetigt += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if not bestaetigt:
        return _meldung("/belege?offen=1", "Diese Belege waren schon bestätigt.", art="erfolg")
    return _meldung("/belege?offen=1",
                    f"{_zahlwort(bestaetigt, 'Beleg', 'Belege')} bestätigt.", art="erfolg")

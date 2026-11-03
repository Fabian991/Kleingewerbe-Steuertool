"""Sicherung: erzeugen, prüfen, Zielordner einstellen.

Bis hierhin mahnte die Anwendung ein Backup an, das der Nutzer nicht erzeugen
konnte -- die Tabelle und die blaue Warnung gab es, die Funktion nicht. Bei
lokalem Betrieb und bis zu zehn Jahren Aufbewahrung (§ 147 Abs. 3 AO) ist das
die Lücke, die am teuersten wird.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import db
from app.logik import backup as logik
from app.logik import export_excel
from app.logik.beleg import abholen
from app.web import VORLAGEN, betrieb_oder_none, dateikopf, hole_conn

router = APIRouter(prefix="/backup")


def _datenordner(request: Request) -> Path:
    pfad = request.app.state.datenpfad
    return Path(pfad).parent if pfad else db.standard_datenordner()


def _meldung(text: str, art: str = "fehler") -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"/backup?{art}={quote(text)}", status_code=303)


@router.get("", response_class=HTMLResponse)
def ansicht(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    betrieb = betrieb_oder_none(conn)
    if betrieb is None:
        return RedirectResponse("/einrichtung", status_code=303)

    datenordner = _datenordner(request)
    return VORLAGEN.TemplateResponse(
        request,
        "backup.html",
        {
            "sicherungen": logik.liste(conn),
            "datenordner": datenordner,
            "ziel": logik.zielordner(conn, datenordner),
            "eigenes_ziel": betrieb["backup_pfad"],
            "zweitziel": betrieb["backup_zweitziel"],
            "letztes_geprueft": logik.letztes_geprueft(conn),
            "warnung_ab": logik.WARNUNG_AB_TAGEN,
            "fehler": request.query_params.get("fehler"),
            "erfolg": request.query_params.get("erfolg"),
        },
    )


@router.post("")
def erzeugen(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    # Bewusst ohne eigene Transaktion: VACUUM laeuft nicht innerhalb einer, und
    # erzeuge() klammert den Protokolleintrag selbst.
    try:
        archiv = logik.erzeuge(conn, _datenordner(request))
    except Exception as fehler:  # noqa: BLE001 -- die Meldung geht an den Nutzer
        # Deutsch und ohne "[Errno 2] No such file or directory": die Meldung
        # liest jemand, der kein Entwickler ist.
        return _meldung(f"Die Sicherung ist fehlgeschlagen: {logik.fehlertext(fehler)}")
    text = (f"{archiv.pfad.name} geschrieben, {archiv.dateien} Dateien, "
            f"{logik.menschenlesbar(archiv.groesse)}.")
    if archiv.zweitkopie:
        text += f" Zweitkopie nach {archiv.zweitkopie.parent} gelegt."
    text += " Jetzt bitte prüfen — ein ungeprüftes Backup ist kein Backup."
    if archiv.excel_fehler:
        # Das Archiv ist entstanden und vollstaendig; es fehlt nur das Beiblatt,
        # das den Bestand ohne dieses Werkzeug lesbar macht. Das muss gesagt
        # werden, sonst verlaesst sich jemand im Ernstfall darauf.
        return _meldung(
            f"{text} ACHTUNG: Die Tabellenkalkulation im Archiv fehlt "
            f"({archiv.excel_fehler})."
        )
    if archiv.zweitkopie_fehler:
        # Die Sicherung selbst ist gelungen, die Zweitkopie nicht. Beides sagen,
        # und zwar als Fehler: eine Sicherung, die nur an einer Stelle liegt,
        # ist nicht die, die eingestellt war.
        return _meldung(f"{text} ACHTUNG: {archiv.zweitkopie_fehler}")
    return _meldung(text, art="erfolg")


@router.get("/excel")
def excel(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Der gesamte Bestand als Tabellenkalkulation, zum Herunterladen.

    Dieselbe Datei liegt in jedem Sicherungsarchiv. Hier gibt es sie einzeln,
    weil man sie oefter braucht als eine Wiederherstellung: fuer den
    Steuerberater, fuer eine Auswertung, oder einfach um nachzusehen.

    Jede Anfrage baut ihre Datei im Speicher. Vorher schrieben alle in
    dieselbe ``steuer-daten.xlsx`` im Datenordner, waehrend eine fruehere
    Anfrage sie noch auslieferte -- acht gleichzeitige Downloads ergaben bis
    zu acht defekte Dateien, jede mit Status 200.
    """
    try:
        inhalt = export_excel.als_bytes(conn)
    except Exception:  # noqa: BLE001 -- die Meldung geht an den Nutzer
        # Ohne den technischen Text: der ist englisch und nennt Zellen und
        # Blaetter, mit denen ein Nicht-Entwickler nichts anfangen kann. Die
        # Datenbank ist davon nicht betroffen, und das ist die Nachricht.
        return _meldung(
            "Die Tabelle ließ sich nicht erzeugen. Die Datenbank selbst ist davon nicht "
            "betroffen — bitte eine Sicherung erzeugen und den Fehler melden."
        )
    return Response(
        inhalt,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=dateikopf(f"{db.heute()}_{export_excel.DATEINAME}", herunterladen=True),
    )


@router.post("/{backup_id}/pruefen")
def pruefen(backup_id: int, conn: sqlite3.Connection = Depends(hole_conn)):
    conn.execute("BEGIN IMMEDIATE")
    try:
        ergebnis = logik.pruefe(conn, backup_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if not ergebnis.heil:
        return _meldung(ergebnis.meldung)
    return _meldung(ergebnis.meldung, art="erfolg")


#: Zeichen, die in einem Pfad nichts verloren haben: Zeilenumbrueche und andere
#: Steuerzeichen. Ein "abc\\ndef" aus einem Zwischenablage-Unfall wuerde sonst
#: als Ordnername angelegt.
_STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")


def _zielordner_lesen(eingabe: str, datenordner: Path) -> tuple[Path | None, str | None]:
    """Liest eine Zieleingabe, ohne etwas anzulegen. Gibt (Ordner, Fehlertext) zurueck.

    Der Ordner kommt absolut und aufgeloest zurueck: ein relativer Pfad hinge
    vom Arbeitsverzeichnis ab, und aus einem anderen gestartet "fehlte" jede
    Sicherung. ``expanduser``/``resolve`` stehen im try: "~unbekannt/x" warf
    einen RuntimeError, ein ueberlanger Pfad einen OSError -- beides war eine
    500-Seite.
    """
    if _STEUERZEICHEN.search(eingabe):
        return None, "Der Pfad enthält Zeilenumbrüche oder Steuerzeichen — bitte prüfen."
    kurz = eingabe if len(eingabe) <= 80 else eingabe[:77] + "…"
    try:
        ordner = Path(eingabe).expanduser().resolve()
        grund = logik.zielfehler(datenordner, ordner)
        if grund:
            return None, grund
        if ordner.exists() and not ordner.is_dir():
            return None, f"{ordner} ist eine Datei, kein Ordner."
    except RuntimeError:
        return None, (
            f"„{kurz}“: den Benutzer hinter der Tilde gibt es auf diesem Rechner nicht — "
            "bitte den vollständigen Pfad angeben."
        )
    except OSError as fehler:
        return None, f"„{kurz}“ taugt nicht als Zielordner: {logik.fehlertext(fehler)}"
    return ordner, None


def _zielordner_anlegen(ordner: Path) -> str | None:
    """Legt den Zielordner an und probiert das Schreibrecht. Gibt den Fehlertext zurueck.

    Auf der internen Platte samt Elternkette; unter einem Einhaengepunkt
    (``/Volumes/<Name>`` ...) nur, wenn das Laufwerk da ist -- sonst heisst
    die Meldung "Hängt das Laufwerk?" (siehe ``logik.ordner_anlegen``).
    """
    try:
        logik.ordner_anlegen(ordner)
    except OSError as fehler:
        return f"{ordner} lässt sich nicht anlegen: {logik.fehlertext(fehler)}"
    if not os.access(ordner, os.W_OK):
        return (
            f"In den Ordner {ordner} kann nicht geschrieben werden — schreibgeschützte "
            "Platte oder fremdes Benutzerkonto?"
        )
    return None


@router.post("/ziel")
def ziel_speichern(
    request: Request,
    pfad: str = Form(""),
    zweitziel: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    datenordner = _datenordner(request)
    neu: dict[str, str | None] = {}
    ordner: dict[str, Path | None] = {}
    for feld, eingabe in (("backup_pfad", pfad), ("backup_zweitziel", zweitziel)):
        ziel = eingabe.strip()
        ordner[feld] = None
        if ziel:
            ordner[feld], fehler = _zielordner_lesen(ziel, datenordner)
            if fehler:
                return _meldung(fehler)
        neu[feld] = str(ordner[feld]) if ordner[feld] is not None else None

    # Verglichen werden die AUFGELOESTEN Pfade: "/x/gleich" und "/x/gleich/"
    # oder "/x/gleich/../gleich" sind derselbe Ordner, und die Zweitkopie laege
    # neben dem Original -- genau das, was die Meldung verhindern soll. Ein
    # leeres erstes Ziel ist der Standard <Datenordner>/backup und zaehlt mit:
    # vorher wurde genau dieses Paar gespeichert, und jede Sicherung meldete
    # danach rot, auf Englisch und mit falschem Hinweis aufs Laufwerk.
    erstes = ordner["backup_pfad"]
    if erstes is None:
        try:
            erstes = (datenordner / "backup").expanduser().resolve()
        except (RuntimeError, OSError):
            erstes = datenordner / "backup"
    if ordner["backup_zweitziel"] is not None and erstes == ordner["backup_zweitziel"]:
        return _meldung(
            "Beide Ziele zeigen auf denselben Ordner"
            + ("" if ordner["backup_pfad"] is not None
               else " (ein leeres erstes Ziel heißt: backup/ im Datenordner)")
            + ". Eine Zweitkopie daneben schützt gegen Versehen, nicht gegen einen "
            "Plattendefekt — und genau dagegen ist das zweite Ziel da."
        )

    # Nicht ein Eingangsordner fuer Belege (fuer alle Jahre oder eines Jahres):
    # das Abholen verschiebt jede Datei, die es dort findet -- die naechste
    # Sicherung laege danach in "Nicht übernommen", und die Sicherungsseite
    # meldete "Datei fehlt". Geprueft wird am Dateisystem (derselbe Ordner
    # unter anderer Schreibweise zaehlt mit).
    for eingangsordner in abholen.ordner_liste(conn):
        for feld in ("backup_pfad", "backup_zweitziel"):
            if ordner[feld] is not None and abholen.derselbe_ordner(ordner[feld],
                                                                    eingangsordner.pfad):
                return _meldung(
                    "Das Sicherungsziel darf nicht der Eingangsordner für Belege sein — "
                    "das Tool holt dort jede Datei ab und verschiebt sie; die Sicherungen "
                    "fehlten danach. Bitte einen eigenen Ordner wählen."
                )

    # Erst jetzt anlegen: vorher stand der erste Ordner schon auf der Platte,
    # wenn das Paar danach abgewiesen wurde.
    for feld in ("backup_pfad", "backup_zweitziel"):
        if ordner[feld] is not None:
            fehler = _zielordner_anlegen(ordner[feld])
            if fehler:
                return _meldung(fehler)

    conn.execute("BEGIN IMMEDIATE")
    try:
        alt = conn.execute(
            "SELECT backup_pfad, backup_zweitziel FROM betrieb WHERE id = 1"
        ).fetchone()
        conn.execute(
            "UPDATE betrieb SET backup_pfad = ?, backup_zweitziel = ? WHERE id = 1",
            (neu["backup_pfad"], neu["backup_zweitziel"]),
        )
        for feld, wert in neu.items():
            if (alt[feld] if alt else None) != wert:
                db.protokolliere(conn, "AENDERN", "betrieb", 1, feld=feld,
                                 wert_alt=alt[feld] if alt else None, wert_neu=wert)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return RedirectResponse("/backup", status_code=303)

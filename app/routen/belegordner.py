"""Der Eingangsordner: einstellen und von Hand abholen.

Das Abholen selbst steht in ``app.logik.beleg.abholen``; beim Start laeuft es
im Hintergrund (``app.main.lebenszyklus``). Hier sind die Knoepfe der
Belegseite: einen Ordner speichern -- fuer alle Jahre oder fuer ein Jahr
(app/logik/beleg/jahresordner.py) -- und "Jetzt abholen", etwa nachdem macOS
den Zugriff auf iCloud Drive erst nach dem Start erlaubt hat. Abgeholt werden
immer alle hinterlegten Ordner.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import db
from app.logik import backup as backuplogik
from app.logik.beleg import abholen, jahresordner
from app.routen.belege import HINWEIS_COOKIE, _datenordner, _meldung, liste_seite
from app.web import betrieb_oder_none, hole_conn

router = APIRouter()

#: Laenger ist kein Pfad, den jemand tippt -- und ein Zwischenablage-Unfall
#: soll nicht als Ordnername auf der Platte landen.
PFAD_HOECHSTENS = 1000


def _kurz(text: str) -> str:
    return text if len(text) <= 80 else text[:77] + "…"


def _ordner_lesen(
    eingabe: str, datenordner: Path, conn: sqlite3.Connection
) -> tuple[Path | None, str | None]:
    """Liest die Eingabe als Ordner, ohne etwas anzulegen: (Ordner, Fehlertext).

    Absolut und aufgeloest, wie das Sicherungsziel: ein relativer Pfad hinge
    davon ab, von wo das Programm gestartet wurde. ``expanduser`` und
    ``resolve`` stehen im try -- "~unbekannt/x" warf einen RuntimeError.
    """
    if len(eingabe) > PFAD_HOECHSTENS:
        return None, "Dieser Pfad ist unplausibel lang — bitte den Ordner prüfen."
    try:
        ordner = Path(eingabe).expanduser()
    except RuntimeError:
        return None, (f"„{_kurz(eingabe)}“: den Benutzer hinter der Tilde gibt es auf diesem "
                      "Rechner nicht — bitte den vollständigen Pfad angeben.")
    # Auch gegen die Sicherungsziele: abgeholt wuerden dort die Archive -- als
    # "kein Belegformat" nach "Nicht übernommen" geschoben, und die
    # Sicherungsliste faende keine Datei mehr.
    grund = abholen.ordnerfehler(datenordner, ordner,
                                 sicherungen=abholen.sicherungsziele(conn, datenordner))
    if grund:
        return None, grund
    try:
        ordner = ordner.resolve()
        if ordner.exists() and not ordner.is_dir():
            return None, f"{ordner} ist eine Datei, kein Ordner."
    except (OSError, RuntimeError) as fehler:
        return None, (f"„{_kurz(eingabe)}“ taugt nicht als Eingangsordner: "
                      f"{backuplogik.fehlertext(fehler)}")
    return ordner, None


def _ordner_bereitstellen(ordner: Path, *, danach: str = "noch einmal „Speichern“"
                          ) -> tuple[bool, str | None]:
    """Legt den Ordner an, wenn nur er selbst fehlt: (angelegt, Fehlertext).

    Nur EINE Ebene: fehlt schon der Elternordner, ist das fast immer ein
    Hinweis auf etwas anderes -- iCloud Drive ist auf diesem Mac aus, ein
    Laufwerk haengt nicht, oder der Pfad ist vertippt. Den ganzen Pfad
    anzulegen hiesse, einen Ordner zu fuellen, den iCloud nie sieht.

    Danach ein Probeblick hinein: verweigert macOS den Zugriff (das Terminal
    darf nicht auf iCloud Drive), soll das JETZT dastehen und nicht erst beim
    naechsten Start.
    """
    angelegt = False
    if not ordner.exists():
        eltern = ordner.parent
        if not eltern.is_dir():
            hinweis = abholen.icloud_hinweis(eltern)
            return False, (f"Der Ordner {eltern} fehlt — darin lässt sich „{ordner.name}“ "
                           f"nicht anlegen.{hinweis} "
                           + ("Sonst bitte den Pfad prüfen." if hinweis
                              else "Bitte den Pfad prüfen."))
        try:
            ordner.mkdir()
        except FileExistsError:
            pass
        except PermissionError:
            return False, abholen.kein_zugriff(eltern, danach=danach)
        except OSError as fehler:
            return False, (f"{ordner} lässt sich nicht anlegen: "
                           f"{backuplogik.fehlertext(fehler)}")
        else:
            angelegt = True
    try:
        next(ordner.iterdir(), None)
    except PermissionError:
        return angelegt, abholen.kein_zugriff(ordner, danach=danach)
    except OSError as fehler:
        return angelegt, (f"Der Ordner {ordner} lässt sich nicht lesen: "
                          f"{backuplogik.fehlertext(fehler)}")
    if not os.access(ordner, os.W_OK):
        return angelegt, (f"In den Ordner {ordner} kann das Tool nicht schreiben — dann ließen "
                          f"sich abgeholte Dateien nicht nach „{abholen.UEBERNOMMEN}“ legen.")
    return angelegt, None


def _schon_belegt(ordner: Path, gespeichert: str | None, bestaetigt: str) -> str | None:
    """Die Rueckfrage, wenn in einem NEU hinterlegten Ordner schon Dateien
    liegen -- das Tool verschiebt jede davon. Ein unveraenderter Ordner und
    ein bestaetigter fragen nicht."""
    if bestaetigt == "1":
        return None
    if gespeichert and abholen.derselbe_ordner(Path(gespeichert), ordner):
        return None
    belegt = jahresordner.schon_belegt(ordner)
    return jahresordner.belegt_text(ordner, belegt) if belegt is not None else None


@router.post("/belege/ordner")
def ordner_speichern(
    request: Request,
    pfad: str = Form(""),
    automatisch: str = Form(""),
    vorschlag: str = Form(""),
    jahr: str = Form(""),
    bestaetigt: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Speichert den Eingangsordner fuer alle Jahre. Leer schaltet ihn aus.

    Mit ``jahr``: den Ordner dieses Jahres (:func:`_jahresordner_speichern`).

    "Vorschlag übernehmen" (``vorschlag=1``) nimmt den Ordner in iCloud Drive
    und legt ihn an, falls iCloud Drive da ist. Ein abgewiesener Pfad kommt
    mit der Eingabe zurueck (422) -- getippt ist er schnell falsch und muehsam
    neu. Liegen im neuen Ordner schon Dateien, fragt die Seite nach
    (``bestaetigt=1``): das Tool verschiebt jede davon.
    """
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    if jahr.strip():
        return _jahresordner_speichern(request, conn, jahr, pfad, vorschlag, bestaetigt)
    datenordner = _datenordner(request)
    eingabe = {"pfad": pfad, "automatisch": automatisch}

    def zurueck(text: str, *, belegt: bool = False):
        return liste_seite(request, conn, ordner_fehler=text,
                           ordner_eingabe={**eingabe, **({"belegt": "1"} if belegt else {})},
                           status_code=422)

    text = pfad.strip()
    if vorschlag == "1":
        gemeint = abholen.vorschlag_ordner()
        if gemeint is None:
            return zurueck("Einen Vorschlag gibt es nur auf dem Mac (iCloud Drive) — bitte "
                           "einen Ordner eintragen.")
        text = str(gemeint)
        eingabe["pfad"] = text

    neu: str | None = None
    angelegt = False
    if text:
        ordner, fehler = _ordner_lesen(text, datenordner, conn)
        if fehler is None:
            fehler = jahresordner.kollision(conn, ordner, jahr=None)
        if fehler is None:
            angelegt, fehler = _ordner_bereitstellen(
                ordner, danach=("noch einmal „Vorschlag übernehmen“" if vorschlag == "1"
                                else "noch einmal „Speichern“"))
        if fehler is not None:
            return zurueck(fehler)
        gespeichert = conn.execute(
            "SELECT belegordner_pfad FROM betrieb WHERE id = 1").fetchone()[0]
        frage = _schon_belegt(ordner, gespeichert, bestaetigt)
        if frage is not None:
            return zurueck(frage, belegt=True)
        neu = str(ordner)
    an = 1 if automatisch == "1" else 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        alt = conn.execute(
            "SELECT belegordner_pfad, belegordner_automatisch FROM betrieb WHERE id = 1"
        ).fetchone()
        conn.execute(
            "UPDATE betrieb SET belegordner_pfad = ?, belegordner_automatisch = ? WHERE id = 1",
            (neu, an),
        )
        for feld, wert in (("belegordner_pfad", neu), ("belegordner_automatisch", an)):
            if alt[feld] != wert:
                db.protokolliere(conn, "AENDERN", "betrieb", 1, feld=feld,
                                 wert_alt=alt[feld], wert_neu=wert)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if neu is None and not alt["belegordner_pfad"]:
        # War nie eingeschaltet: "ausgeschaltet" hiesse, es sei vorher an gewesen.
        return _meldung("/belege", "Noch kein Eingangsordner gewählt — „Vorschlag übernehmen“ "
                                   "klicken oder einen Pfad eintragen und speichern.",
                        art="hinweis")
    if neu is None:
        return _meldung("/belege", "Eingangsordner ausgeschaltet — beim Start wird nichts "
                                   "mehr abgeholt.", art="erfolg")
    meldung = (f"Eingangsordner {abholen.anzeigename(neu)} "
               f"{'angelegt und ' if angelegt else ''}gespeichert. "
               "Beim nächsten Start holt das Tool neue Rechnungen dort ab — oder gleich "
               "„Jetzt abholen“.")
    if not an:
        meldung += " Automatisch gebucht wird nichts; alles wartet in der Prüfmaske."
    return _meldung("/belege", meldung, art="erfolg")


def _jahresordner_speichern(request: Request, conn: sqlite3.Connection, jahr_roh: str,
                            pfad: str, vorschlag: str, bestaetigt: str):
    """Hinterlegt, aendert oder entfernt (leerer Pfad) den Ordner eines Jahres.

    Dieselben Pruefungen wie fuer den Ordner fuer alle Jahre, dazu: kein
    Ordner zweimal, Jahresordner nicht ineinander. Mit Journal (ANLEGEN,
    AENDERN, LOESCHEN je Jahr) in einer Transaktion.
    """
    datenordner = _datenordner(request)
    eingabe = {"jahr": jahr_roh.strip(), "jahr_pfad": pfad}

    def zurueck(text: str, *, belegt: bool = False):
        return liste_seite(request, conn, ordner_fehler=text,
                           ordner_eingabe={**eingabe, **({"belegt": "1"} if belegt else {})},
                           status_code=422)

    try:
        jahr = jahresordner.jahr_lesen(jahr_roh)
    except ValueError as fehler:
        return zurueck(str(fehler))
    text = pfad.strip()
    if vorschlag == "1":
        gemeint = jahresordner.vorschlag(conn, jahr)
        if gemeint is None:
            return zurueck(f"Für {jahr} gibt es keinen Vorschlag — bitte den Ordner eintragen.")
        text = str(gemeint)
        eingabe["jahr_pfad"] = text

    alt = jahresordner.jahre(conn).get(jahr)
    neu: str | None = None
    angelegt = False
    if text:
        ordner, fehler = _ordner_lesen(text, datenordner, conn)
        if fehler is None:
            fehler = jahresordner.kollision(conn, ordner, jahr=jahr)
        if fehler is None:
            angelegt, fehler = _ordner_bereitstellen(
                ordner, danach=("noch einmal „Vorschlag übernehmen“" if vorschlag == "1"
                                else "noch einmal „Speichern“"))
        if fehler is not None:
            return zurueck(fehler)
        frage = _schon_belegt(ordner, alt, bestaetigt)
        if frage is not None:
            return zurueck(frage, belegt=True)
        neu = str(ordner)
    elif alt is None:
        return _meldung("/belege", f"Für {jahr} ist kein Eingangsordner hinterlegt.",
                        art="hinweis", anker="eingangsordner")

    conn.execute("BEGIN IMMEDIATE")
    try:
        alt = jahresordner.jahre(conn).get(jahr)  # unter der Sperre noch einmal
        if neu is None:
            conn.execute("DELETE FROM belegordner_jahr WHERE jahr = ?", (jahr,))
            db.protokolliere(conn, "LOESCHEN", "belegordner_jahr", jahr, feld="pfad",
                             wert_alt=alt)
        elif alt is None:
            conn.execute("INSERT INTO belegordner_jahr (jahr, pfad, angelegt_utc)"
                         " VALUES (?, ?, ?)", (jahr, neu, db.jetzt_utc()))
            db.protokolliere(conn, "ANLEGEN", "belegordner_jahr", jahr, feld="pfad",
                             wert_neu=neu)
        elif alt != neu:
            conn.execute("UPDATE belegordner_jahr SET pfad = ? WHERE jahr = ?", (neu, jahr))
            db.protokolliere(conn, "AENDERN", "belegordner_jahr", jahr, feld="pfad",
                             wert_alt=alt, wert_neu=neu)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return zurueck("Dieser Ordner ist schon für ein anderes Jahr hinterlegt.")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if neu is None:
        return _meldung("/belege", f"Eingangsordner für {jahr} entfernt — er wird nicht mehr "
                                   "abgeholt. Die Dateien darin bleiben, wo sie sind.",
                        art="erfolg", anker="eingangsordner")
    return _meldung("/belege", f"Eingangsordner für {jahr}: {abholen.anzeigename(neu)} "
                               f"{'angelegt und ' if angelegt else ''}gespeichert. Beim nächsten "
                               "Start holt das Tool neue Rechnungen dort ab — oder gleich "
                               "„Jetzt abholen“.", art="erfolg", anker="eingangsordner")


@router.post("/belege/ordner/umwidmen")
def ordner_umwidmen(
    jahr: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Fuehrt den Ordner fuer alle Jahre als Ordner EINES Jahres -- fuer wen
    ihn ohnehin je Jahr anlegt. Ein Schritt mit Journal: der Pfad wandert
    von betrieb.belegordner_pfad nach belegordner_jahr. Eine Migration tut
    das nie von selbst (keine Umdeutung einer Einstellung)."""
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    try:
        ziel_jahr = jahresordner.jahr_lesen(jahr)
    except ValueError as fehler:
        return _meldung("/belege", str(fehler), anker="eingangsordner")
    conn.execute("BEGIN IMMEDIATE")
    try:
        pfad = conn.execute("SELECT belegordner_pfad FROM betrieb WHERE id = 1").fetchone()[0]
        if not (pfad or "").strip():
            conn.execute("ROLLBACK")
            return _meldung("/belege", "Es ist kein Ordner für alle Jahre hinterlegt.",
                            anker="eingangsordner")
        if ziel_jahr in jahresordner.jahre(conn):
            conn.execute("ROLLBACK")
            return _meldung("/belege", f"Für {ziel_jahr} ist schon ein Ordner hinterlegt.",
                            anker="eingangsordner")
        conn.execute("UPDATE betrieb SET belegordner_pfad = NULL WHERE id = 1")
        db.protokolliere(conn, "AENDERN", "betrieb", 1, feld="belegordner_pfad",
                         wert_alt=pfad, wert_neu=None,
                         begruendung=f"als Eingangsordner für {ziel_jahr} geführt")
        conn.execute("INSERT INTO belegordner_jahr (jahr, pfad, angelegt_utc) VALUES (?, ?, ?)",
                     (ziel_jahr, pfad, db.jetzt_utc()))
        db.protokolliere(conn, "ANLEGEN", "belegordner_jahr", ziel_jahr, feld="pfad",
                         wert_neu=pfad, begruendung="bisher Ordner für alle Jahre")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _meldung("/belege", f"{abholen.anzeigename(pfad)} ist jetzt der Eingangsordner für "
                               f"{ziel_jahr}. Für das nächste Jahr schlägt das Tool ab "
                               "Dezember einen Ordner vor.", art="erfolg",
                    anker="eingangsordner")


@router.post("/belege/ordner/hinweis-aus")
def hinweis_aus():
    """Der Hinweis auf Ordner je Jahr kommt nicht wieder -- in diesem
    Browser. Eine Einstellung der Buchfuehrung ist das nicht, deshalb kein
    Journal und keine Spalte."""
    antwort = RedirectResponse("/belege#eingangsordner", status_code=303)
    antwort.set_cookie(HINWEIS_COOKIE, "aus", max_age=10 * 365 * 24 * 3600, httponly=True,
                       samesite="strict")
    return antwort


#: Die Meldung, wenn "Jetzt abholen" mit Aenderungen geklickt wird, die noch
#: nicht gespeichert sind.
ERST_SPEICHERN = ("Die Änderungen am Eingangsordner sind noch nicht gespeichert — abgeholt "
                  "wurde nichts. Erst „Speichern“, dann „Jetzt abholen“.")


def _ungespeichert(conn: sqlite3.Connection, pfad: str | None, automatisch: str | None,
                   ziel: Path | None) -> bool:
    """Weicht das abgeschickte Formular vom Gespeicherten ab?

    "Jetzt abholen" schickt das Einstellungsformular mit. Wer den Haken
    herausnimmt und gleich abholt, sieht "nicht automatisch buchen" -- und
    die Route buchte trotzdem nach der gespeicherten Einstellung, unloeschbar.
    Ein getippter anderer Ordner ging ebenso still verloren. Was der Nutzer
    sieht, gilt: weicht es ab, wird nicht abgeholt. Ohne Formularfelder
    (``None``) gilt das Gespeicherte.
    """
    if pfad is None:
        return False
    if (automatisch == "1") != abholen.automatisch(conn):
        return True
    text = pfad.strip()
    if not text:
        return ziel is not None
    if ziel is None:
        return True
    try:
        getippt = Path(text).expanduser()
    except RuntimeError:
        return True
    return not abholen.derselbe_ordner(getippt, ziel)


@router.post("/belege/ordner/abholen")
def jetzt_abholen(
    request: Request,
    pfad: str | None = Form(None),
    automatisch: str | None = Form(None),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Holt den Eingangsordner sofort ab -- synchron, mit derselben Sperre wie
    der Lauf beim Start. Laeuft der noch, wird nicht gewartet: die Seite sagt
    "läuft gerade", und ein Neuladen zeigt gleich das Ergebnis."""
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    ziel = abholen.ordner(conn)
    if _ungespeichert(conn, pfad, automatisch, ziel):
        return liste_seite(request, conn, ordner_fehler=ERST_SPEICHERN,
                           ordner_eingabe={"pfad": pfad or "", "automatisch": automatisch or ""},
                           status_code=422)
    liste = abholen.ordner_liste(conn)
    if not liste:
        return _meldung("/belege", "Es ist noch kein Eingangsordner eingestellt — erst den "
                                   "Ordner wählen und speichern.")
    stand = getattr(request.app.state, "belegordner", None)
    try:
        abholen.abholen_alle(conn, _datenordner(request), liste,
                             automatisch=abholen.automatisch(conn), stand=stand, warten=False)
    except abholen.LaeuftSchon:
        return _meldung("/belege", "Das Abholen läuft gerade — in ein paar Sekunden die Seite "
                                   "neu laden.", art="hinweis")
    # Keine Meldung oben: das Ergebnis steht im Abschnitt "Eingangsordner",
    # in der Farbe, die es verdient (gelb, wenn etwas wartet oder abgewiesen
    # ist, rot bei einem Fehler). Oben stand derselbe Satz vorher ein zweites
    # Mal -- und gruen, auch wenn nichts gebucht war.
    return RedirectResponse("/belege#eingangsordner", status_code=303)

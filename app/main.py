"""FastAPI-Anwendung. Laeuft lokal, erreichbar nur ueber 127.0.0.1.

Single-User auf dem eigenen Rechner: bewusst ohne Login. Der angemessene Schutz
ist das Betriebssystem -- Benutzerkonto und Festplattenverschluesselung. Jede
Zeile selbstgebauter Authentifizierung waere Angriffsflaeche ohne Gegenwert.

Das Paket enthaelt KEINE Serverstartlogik. Gestartet wird es von aussen, damit
derselbe Code im Browser und spaeter in einem eigenen Fenster laeuft.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match, Mount

from app import db
from app.logik import backup as backuplogik
from app.logik import eingabe, kurse
from app.logik.beleg import abholen, pipeline, textdienst
from app.routen import (
    anlagen,
    backup,
    belege,
    belegkorrektur,
    belegordner,
    eingang,
    einnahmen,
    euer,
    kunden,
    rechnungen,
    rechnungsablauf,
    start,
    steuer,
    verwaltung,
    wiso,
    zeiten,
)
from app.web import VORLAGEN

#: So lange wartet das Beenden auf einen laufenden Abholvorgang. Er hoert auf
#: das Stoppsignal zwischen zwei Dateien (und mitten in einer langsamen
#: Textschicht) und ist dann gleich fertig; die Grenze gilt nur, falls doch
#: etwas haengt. Was danach halb fertig war, liegt noch im Eingangsordner
#: (die Quelle wird erst verschoben, wenn entschieden ist, ob sie gebucht
#: wird oder wartet) und kommt beim naechsten Mal dran.
ABHOLEN_BEIM_BEENDEN_SEKUNDEN = 15

#: So lange nach dem Start beginnt das Abholen, falls bis dahin keine Anfrage
#: kam. Erst dann lauscht der Server wirklich: uvicorn fuehrt den Start der
#: Anwendung VOR dem Binden des Ports aus. Ein zweiter Start auf belegtem
#: Port (start.command doppelt geoeffnet) endete sonst erst nach dem Abholen
#: -- und hatte unbemerkt gebucht und verschoben. Scheitert das Binden, endet
#: der Prozess sofort, und das Abholen beginnt gar nicht.
ABHOLEN_NACH_SEKUNDEN = 2


@asynccontextmanager
async def lebenszyklus(anwendung: FastAPI):
    """Migriert einmal beim Start. Die Verbindung wird NICHT offen gehalten.

    Laesst sich die Datenbank nicht als Datenbank dieses Programms oeffnen
    (keine Datenbank, fremde Datenbank, neuer als der Code), startet die
    Anwendung trotzdem -- und zeigt auf jede Anfrage die Meldung, statt mit
    einer rohen Ausnahme im Terminal zu verschwinden, das der Nutzer nicht
    liest. Geschrieben wird dann nichts.

    Danach, wenn ein Betrieb eingerichtet und ein Eingangsordner eingestellt
    ist: das Abholen der Rechnungen vom Handy -- im Hintergrund, der Start
    wartet nicht darauf (iCloud kann eine Datei erst laden muessen, und macOS
    fragt beim ersten Mal, ob das Terminal auf iCloud Drive zugreifen darf).
    Beim Beenden wartet die Anwendung kurz, damit kein Lauf mitten im Buchen
    abreisst.

    Ebenso im Hintergrund: die EZB-Kurstabelle auffrischen, wenn sie aelter
    als einen Tag ist (app/logik/kurse.py). Ohne Netz bleibt die vorhandene;
    der Start wartet auch darauf nicht.
    """
    anwendung.state.startfehler = None
    anwendung.state.startfehler_erneut_versuchen = False
    anwendung.state.belegordner = abholen.Laufstand()
    los = anwendung.state.abholen_los = threading.Event()
    stopp = anwendung.state.abholen_stopp = threading.Event()
    anwendung.state.abholen_faden = None
    anwendung.state.kurse_faden = None
    anwendung.state.kurse_ergebnis = None
    wecker = None
    try:
        verbindung = db.oeffne(anwendung.state.datenpfad)
        try:
            eingangsordner = _eingangsordner_beim_start(verbindung)
            kurse_auffrischen = _kurstabelle_beim_start(verbindung, _datenordner(anwendung))
        finally:
            verbindung.close()
    except db.DatenbankFehler as fehler:
        anwendung.state.startfehler = str(fehler)
    except sqlite3.OperationalError as fehler:
        if not _gesperrt(fehler):
            raise
        # Ein anderer Vorgang haelt die Datenbank laenger gesperrt, als die
        # Verbindung wartet (zweites Fenster, ein Programm, das den Ordner
        # sichert). Vorher endete der Start mit einem rohen Traceback, und der
        # Server war nicht erreichbar. Jetzt startet er und versucht es bei
        # der naechsten Anfrage erneut -- und holt dann das Abholen nach
        # (``_erneut_oeffnen``).
        traceback.print_exc(file=sys.stderr)
        anwendung.state.startfehler = DATENBANK_GESPERRT
        anwendung.state.startfehler_erneut_versuchen = True
    else:
        pipeline.alte_zwischendateien_weg(_datenordner(anwendung))
        if kurse_auffrischen:
            anwendung.state.kurse_faden = _kurse_im_hintergrund(anwendung, los, stopp)
        if eingangsordner:
            anwendung.state.abholen_faden = _abholen_im_hintergrund(anwendung, los, stopp)
        if eingangsordner or kurse_auffrischen:
            # Losgehen beim ersten Aufruf (der Browser, den start.command
            # oeffnet) oder spaetestens nach ABHOLEN_NACH_SEKUNDEN -- dann
            # ist der Port gebunden.
            wecker = asyncio.get_running_loop().call_later(ABHOLEN_NACH_SEKUNDEN, los.set)
    yield
    if wecker is not None:
        wecker.cancel()
    with _ERNEUT_SPERRE:
        # Unter der Sperre: ein Startlauf, der gerade nachgeholt wird, steht
        # danach in abholen_faden und bekommt das Stoppsignal mit.
        stopp.set()
        faden = anwendung.state.abholen_faden
    if faden is not None:
        faden.join(timeout=ABHOLEN_BEIM_BEENDEN_SEKUNDEN)
    kursfaden = getattr(anwendung.state, "kurse_faden", None)
    if kursfaden is not None:
        # Ein Abbruch mitten im Laden schadet nicht: abgelegt wird atomar.
        kursfaden.join(timeout=2)
    textdienst.geteilt_beenden()


#: Die Meldung, wenn die Datenbank beim Start gesperrt ist.
DATENBANK_GESPERRT = (
    "Die Datenbank ist gerade von einem anderen Vorgang gesperrt — läuft das Steuer-Tool "
    "schon in einem zweiten Fenster, oder sichert gerade ein anderes Programm den "
    "Datenordner? Diese Seite in ein paar Sekunden neu laden; geht es dann noch nicht, "
    "das andere Fenster schließen."
)


def _gesperrt(fehler: BaseException) -> bool:
    text = str(fehler).lower()
    return "locked" in text or "busy" in text


#: Ein Wiederholungsversuch zur Zeit: zwei Anfragen, die gleichzeitig
#: ankommen, holten den Startlauf sonst zweimal nach.
_ERNEUT_SPERRE = threading.Lock()


def _erneut_oeffnen(anwendung: FastAPI) -> bool:
    """Nach einer Sperre beim Start: die Datenbank noch einmal oeffnen.

    Gelingt es, wird nachgeholt, was der Start ausgelassen hat -- einmal:
    verwaiste Zwischendateien wegraeumen und den Eingangsordner abholen.
    Vorher entfiel das Abholen fuer die ganze Sitzung, und die Rechnungen vom
    Handy lagen bis zum naechsten Start im Ordner.
    """
    with _ERNEUT_SPERRE:
        if not getattr(anwendung.state, "startfehler_erneut_versuchen", False):
            return anwendung.state.startfehler is None  # eine andere Anfrage war schneller
        try:
            verbindung = db.oeffne(anwendung.state.datenpfad)
        except sqlite3.OperationalError as fehler:
            if _gesperrt(fehler):
                return False
            raise
        try:
            eingangsordner = _eingangsordner_beim_start(verbindung)
        finally:
            verbindung.close()
        anwendung.state.startfehler = None
        anwendung.state.startfehler_erneut_versuchen = False
        _startlauf_nachholen(anwendung, eingangsordner)
        return True


def _startlauf_nachholen(anwendung: FastAPI,
                         eingangsordner: list[abholen.Eingangsordner]) -> None:
    """Was ``lebenszyklus`` nach dem Oeffnen tut -- spaeter, aus einer Anfrage."""
    try:
        pipeline.alte_zwischendateien_weg(_datenordner(anwendung))
    except OSError:
        traceback.print_exc(file=sys.stderr)
    stopp = getattr(anwendung.state, "abholen_stopp", None)
    los = getattr(anwendung.state, "abholen_los", None)
    if not eingangsordner or stopp is None or los is None or stopp.is_set():
        return
    los.set()  # eine Anfrage ist da: der Server lauscht
    anwendung.state.abholen_faden = _abholen_im_hintergrund(anwendung, los, stopp)


def _eingangsordner_beim_start(verbindung: sqlite3.Connection) -> list[abholen.Eingangsordner]:
    """Die Eingangsordner, die beim Start abgeholt werden -- alle hinterlegten:
    die je Jahr und der fuer alle Jahre.

    Ohne eingerichteten Betrieb oder ohne Ordner: leer, und es passiert
    nichts. Eine Stoerung hier darf den Start nicht verhindern -- sie landet
    im Terminal, und die Anwendung laeuft ohne Abholen.
    """
    try:
        if verbindung.execute("SELECT 1 FROM betrieb WHERE id = 1").fetchone() is None:
            return []
        return abholen.ordner_liste(verbindung)
    except sqlite3.Error:
        traceback.print_exc(file=sys.stderr)
        return []


def _kurstabelle_beim_start(verbindung: sqlite3.Connection, datenordner: Path) -> bool:
    """Ob der Start die EZB-Kurstabelle im Hintergrund auffrischen soll.

    Ist eine da: wenn sie aelter als einen Tag ist. Ist keine da: nur, wenn
    es Fremdwaehrung gibt (ein Beleg oder ein Lieferant mit Vorgabe) -- wer
    nur Euro-Rechnungen hat, laedt nichts. Die erste Rechnung in Dollar laedt
    die Tabelle dann bei Bedarf.
    """
    alter = kurse.abgerufen_vor(datenordner)
    if alter is not None:
        return alter > kurse.VERALTET_NACH
    try:
        return bool(verbindung.execute(
            "SELECT EXISTS (SELECT 1 FROM beleg WHERE waehrung <> 'EUR')"
            " OR EXISTS (SELECT 1 FROM lieferant WHERE waehrung_default <> 'EUR')"
        ).fetchone()[0])
    except sqlite3.Error:
        return False


def _kurse_im_hintergrund(
    anwendung: FastAPI, los: threading.Event, stopp: threading.Event
) -> threading.Thread:
    """Frischt die Kurstabelle in einem eigenen Thread auf.

    Wartet wie das Abholen auf ``los`` (der Server lauscht): ein zweiter Start
    auf belegtem Port endet, bevor er etwas laedt. Das Ergebnis (oder der
    Grund, warum es nicht ging) steht danach in ``state.kurse_ergebnis``.
    """
    datenordner = _datenordner(anwendung)

    def lauf() -> None:
        while not los.wait(0.05):
            if stopp.is_set():
                return
        if stopp.is_set():
            return
        try:
            tab = kurse.aktualisiere(datenordner)
        except kurse.KursFehler as fehler:
            anwendung.state.kurse_ergebnis = str(fehler)
        except Exception as fehler:  # noqa: BLE001 -- der Server laeuft weiter
            traceback.print_exc(file=sys.stderr)
            anwendung.state.kurse_ergebnis = backuplogik.fehlertext(fehler)
        else:
            anwendung.state.kurse_ergebnis = (
                f"EZB-Kurstabelle aufgefrischt, Kurse bis {tab.letzter_tag:%d.%m.%Y}.")

    faden = threading.Thread(target=lauf, name="EZB-Kurstabelle", daemon=True)
    faden.start()
    return faden


def _datenordner(anwendung: FastAPI) -> Path:
    """Der Datenordner -- wie ihn die Routen bestimmen (``belege._datenordner``)."""
    pfad = anwendung.state.datenpfad
    return Path(pfad).parent if pfad else db.standard_datenordner()


def _abholen_im_hintergrund(
    anwendung: FastAPI, los: threading.Event, stopp: threading.Event
) -> threading.Thread:
    """Startet das Abholen in einem eigenen Thread mit eigener Verbindung.

    ``laeuft`` steht schon VOR dem Start des Threads: eine Seite, die gleich
    nach dem Start geladen wird, soll "läuft gerade" zeigen und nicht das
    Ergebnis eines Laufs, der noch gar nicht begonnen hat. Der Thread wartet
    auf ``los`` (der Server lauscht) und gibt auf, wenn vorher ``stopp``
    kommt (das Binden des Ports ist gescheitert, der Prozess endet).
    """
    stand = anwendung.state.belegordner
    stand.laeuft = True
    faden = threading.Thread(
        target=_abholen_beim_start,
        args=(stand, anwendung.state.datenpfad, _datenordner(anwendung), los, stopp),
        name="Eingangsordner abholen",
        daemon=True,
    )
    faden.start()
    return faden


def _abholen_beim_start(
    stand: abholen.Laufstand, datenpfad: Path | str | None, datenordner: Path,
    los: threading.Event | None = None, stopp: threading.Event | None = None,
) -> None:
    """Der Thread: eine eigene Verbindung, ein Lauf ueber alle hinterlegten
    Eingangsordner, das Ergebnis in ``stand``.

    Eine SQLite-Verbindung gehoert dem Thread, der sie oeffnet -- die
    Verbindung des Starts ist da laengst geschlossen. Jede Ausnahme wird zum
    deutschen Text im Ergebnis (die Uebersicht zeigt ihn), der technische
    Verlauf geht ins Terminal, und der Server laeuft weiter.
    """
    while los is not None and not los.wait(0.05):
        if stopp is not None and stopp.is_set():
            stand.laeuft = False  # nie begonnen: der Prozess endet gleich
            return
    if stopp is not None and stopp.is_set():
        stand.laeuft = False
        return
    liste: list[abholen.Eingangsordner] = []
    try:
        verbindung = db.verbinde(datenpfad)
        try:
            liste = abholen.ordner_liste(verbindung)
            if not liste:
                stand.fertig(abholen.Ergebnis())
                return
            abholen.abholen_alle(verbindung, datenordner, liste,
                                 automatisch=abholen.automatisch(verbindung), stand=stand,
                                 stopp=stopp)
        finally:
            verbindung.close()
    except Exception as fehler:  # noqa: BLE001 -- der Server laeuft weiter
        traceback.print_exc(file=sys.stderr)
        # Am ersten Ordner festgemacht: so zeigen Uebersicht und Belegseite den
        # Fehler, solange er hinterlegt ist.
        erster = liste[0] if liste else None
        stand.fertig(abholen.Ergebnis(
            fehler=("Der Eingangsordner ließ sich beim Start nicht abholen: "
                    f"{backuplogik.fehlertext(fehler)}"),
            ordner=str(erster.pfad) if erster is not None else None,
            jahr=erster.jahr if erster is not None else None))


_TYPEN = {
    "missing": "fehlt",
    "int_parsing": "muss eine ganze Zahl sein",
    "int_type": "muss eine ganze Zahl sein",
    "int_from_float": "muss eine ganze Zahl sein",
    "float_parsing": "muss eine Zahl sein",
    "bool_parsing": "muss ja oder nein sein",
}


def _feldprobleme(exc: RequestValidationError) -> list[str]:
    """Pydantics Fehlerliste als deutsche Saetze, ein Satz je Feld.

    >>> class E:  # Attrappe mit derselben Schnittstelle
    ...     def errors(self): return [{"loc": ("query", "kunde"), "type": "int_parsing"},
    ...                               {"loc": ("body", "datum"), "type": "missing"}]
    >>> _feldprobleme(E())
    ['Das Feld „kunde“ muss eine ganze Zahl sein.', 'Das Feld „datum“ fehlt.']
    """
    saetze = []
    for fehler in exc.errors():
        ort = fehler.get("loc") or ("?",)
        feld = str(ort[-1])
        was = _TYPEN.get(str(fehler.get("type", "")), "hat einen ungültigen Wert")
        satz = f"Das Feld „{feld}“ {was}."
        if satz not in saetze:
            saetze.append(satz)
    return saetze


async def _eingabefehler(request: Request, exc: RequestValidationError):
    """422 als deutsche Seite statt FastAPIs englischem JSON.

    Der Fall ist selten -- die Formulare schicken die Felder mit -- aber nicht
    unmoeglich: ein alter Link, ein von Hand geaenderter Pfad, ein Browser, der
    ein leeres Zahlenfeld als leeren Text sendet. Wer dann {"detail":[...]}
    sieht, haelt das Werkzeug fuer kaputt.
    """
    return VORLAGEN.TemplateResponse(
        request,
        "fehler.html",
        {
            "ueberschrift": "Diese Eingabe konnte nicht verarbeitet werden",
            "saetze": _feldprobleme(exc),
            "zurueck": request.headers.get("referer") or "/",
        },
        status_code=422,
    )


def _gekuerzt(text: str, laenge: int = 80) -> str:
    """Eine Adresse fuer den Meldungstext, hoechstens ``laenge`` Zeichen.

    >>> _gekuerzt("/gibtsnicht"), len(_gekuerzt("/" + "x" * 200))
    ('/gibtsnicht', 80)
    """
    return text if len(text) <= laenge else text[: laenge - 1] + "…"


def _fehlerseite(
    request: Request,
    status: int,
    ueberschrift: str,
    saetze: list[str],
    *,
    zurueck: str | None = None,
    headers: dict[str, str] | None = None,
):
    """Die deutsche Fehlerseite im Layout der Anwendung, ohne Datenbank."""
    return VORLAGEN.TemplateResponse(
        request,
        "fehler.html",
        {
            "ueberschrift": ueberschrift,
            "saetze": saetze,
            "zurueck": zurueck or request.headers.get("referer") or "/",
        },
        status_code=status,
        headers=headers,
    )


def _seite_fehlt(request: Request, headers: dict[str, str] | None = None):
    return _fehlerseite(
        request, 404, "Diese Seite gibt es nicht",
        [f"Unter der Adresse {_gekuerzt(request.url.path)} gibt es keine Seite. "
         "Vielleicht ein altes Lesezeichen oder ein Tippfehler in der Adresse."],
        headers=headers,
    )


#: Die Methoden, nach denen eine 405-Antwort fragt.
_METHODEN = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _erlaubte_methoden(request: Request) -> set[str]:
    """Alle Methoden, mit denen die Adresse der Anfrage erreichbar ist.

    Starlettes Allow-Kopf nennt nur die Methoden der ERSTEN passenden Route;
    /kunden/{id} gibt es aber als Anzeige (GET) und als Formular (POST).
    Gefragt wird deshalb je Methode, ob irgendeine Route VOLL passt -- ueber
    ``matches()``, das auch eingebundene Router beantworten, ohne deren
    innere Routenliste zu kennen.
    """
    erlaubt: set[str] = set()
    # Unter /static hat der Mount root_path schon verlaengert; gefragt wird
    # mit dem root_path der Anwendung, sonst passt der Pfad nirgends mehr.
    wurzel = request.scope.get("app_root_path", request.scope.get("root_path", ""))
    for methode in _METHODEN:
        probe = dict(request.scope, method=methode, root_path=wurzel)
        for route in request.app.router.routes:
            # Ein Mount (/static) passt fuer jede Methode, liefert aber nur
            # Dateien aus: dort gilt allein GET.
            if isinstance(route, Mount) and methode != "GET":
                continue
            if route.matches(probe)[0] == Match.FULL:
                erlaubt.add(methode)
                break
    return erlaubt


async def _nicht_gefunden(request: Request, exc: StarletteHTTPException):
    """404 (und 405) als deutsche Seite im Layout der Anwendung.

    Vorher stand im Browser nur das rohe englische {"detail":"Not Found"} --
    ohne Navigation und ohne Weg zurueck. Die Routen, die einen fehlenden
    Datensatz mit einer Umleitung und Meldung beantworten, bleiben davon
    unberuehrt: sie werfen keinen 404.

    405 hat zwei Ursachen mit entgegengesetzter Erklaerung:

    * GET auf eine reine Formularadresse -- etwa nach einer abgewiesenen
      Eingabe (die Zeitenliste zeigt sie unter /zeiten/timer/start mit Status
      422 neu) und einem Enter in der Adresszeile. Dann "nimmt nur Formulare
      entgegen".
    * POST/PUT/DELETE auf eine reine Leseseite (POST /rechnungen). Dort
      stimmte der Formular-Text gerade nicht: die Seite laesst sich NUR
      anzeigen.

    Dazwischen liegen Adressen mit Anzeige UND Formular (/kunden, /kunden/1):
    PUT oder DELETE dorthin bekam "zeigt nur etwas an und nimmt keine
    Formulare entgegen" -- obwohl POST /kunden/1 das Kundenformular ist.
    Dort steht jetzt der neutrale Formulartext.

    Unterschieden wird an den Methoden, die die Routen der Adresse erlauben.

    400 kommt vom Formularleser (zu viele Felder, kaputter mehrteiliger
    Rumpf): vorher stand dann das rohe englische JSON von Starlette im
    Browser. Andere HTTP-Fehler behandelt FastAPI wie bisher.
    """
    if exc.status_code == 404:
        return _seite_fehlt(request, getattr(exc, "headers", None))
    if exc.status_code == 400:
        return _fehlerseite(
            request, 400, "Das Formular ließ sich nicht lesen",
            ["Das abgeschickte Formular war zu groß oder unvollständig. Gespeichert wurde "
             "nichts.",
             "Bitte die Seite neu laden und es noch einmal versuchen. Bei sehr vielen Dateien "
             "im Eingang hilft es, in mehreren Durchgängen zu arbeiten."],
        )
    if exc.status_code != 405:
        return await http_exception_handler(request, exc)
    erlaubt = _erlaubte_methoden(request)
    pfad = _gekuerzt(request.url.path)
    if "GET" in erlaubt and "POST" not in erlaubt:
        ueberschrift = "Diese Seite lässt sich nur anzeigen"
        saetze = [
            f"Die Seite {pfad} zeigt nur etwas an und nimmt keine Formulare oder "
            "Änderungen entgegen. Gespeichert wurde dabei nichts.",
        ]
    elif "GET" in erlaubt:
        ueberschrift = "Diese Anfrage nimmt die Seite nicht an"
        saetze = [
            f"Die Seite {pfad} lässt sich anzeigen und nimmt die Formulare des "
            "Steuer-Tools entgegen, andere Anfragen nicht. Gespeichert wurde dabei nichts.",
        ]
    elif request.method in ("GET", "HEAD"):
        ueberschrift = "Diese Adresse ist nur für ein Formular"
        saetze = [
            f"Die Adresse {pfad} nimmt nur Formulare entgegen "
            "und lässt sich nicht direkt aufrufen. Gespeichert wurde dabei nichts.",
        ]
    else:
        ueberschrift = "Diese Adresse ist nur für ein Formular"
        saetze = [
            f"Die Adresse {pfad} nimmt nur die Formulare des Steuer-Tools entgegen. "
            "Gespeichert wurde dabei nichts.",
        ]
    kopf = dict(getattr(exc, "headers", None) or {})
    if erlaubt:
        kopf["Allow"] = ", ".join(sorted(erlaubt))
    return _fehlerseite(request, 405, ueberschrift, saetze, headers=kopf or None)


async def _zahl_zu_gross(request: Request, exc: OverflowError):
    """OverflowError als deutsche 422-Seite statt "Da ist etwas schiefgegangen".

    SQLite speichert hoechstens 2**63-1; eine groessere Zahl aus Pfad, Adresse
    oder Formular endete beim Binden mit "Python int too large to convert to
    SQLite INTEGER" (ebenso ``int(float("inf"))``). Das ist eine unsinnige
    Eingabe, kein Programmfehler -- und keine Zahl im Steuer-Tool kommt in
    diese Groessenordnung. Die Routen pruefen ihre Felder weiter selbst; dies
    ist das Netz fuer die Stellen, die es (noch) nicht tun. Eine angefangene
    Aenderung ist zu diesem Zeitpunkt zurueckgerollt: jede Schreibaktion laeuft
    in einer Transaktion, und die Verbindung schliesst ohne COMMIT.
    """
    return _fehlerseite(
        request, 422, "Diese Eingabe konnte nicht verarbeitet werden",
        ["Eine Zahl in der Anfrage ist unplausibel groß. Gespeichert wurde dabei "
         "nichts — bitte die Eingabe prüfen."],
    )


async def _serverfehler(request: Request, exc: Exception):
    """500 als deutsche Seite mit dem technischen Grund.

    Jede Schreibaktion laeuft in einer Transaktion; was hier landet, ist
    zurueckgerollt. Das sagt die Seite, und sie nennt den Fehler woertlich,
    damit er sich melden laesst. Im Test (TestClient) greift der Handler nicht,
    dort kommt die Ausnahme weiter hoch -- so bleiben Fehler in Tests sichtbar.
    """
    return VORLAGEN.TemplateResponse(
        request,
        "fehler.html",
        {
            "ueberschrift": "Da ist etwas schiefgegangen",
            "saetze": [
                "Die Aktion wurde abgebrochen. Gespeichert wurde dabei nichts: eine "
                "abgebrochene Änderung rollt die Datenbank vollständig zurück.",
            ],
            "technisch": f"{type(exc).__name__}: {exc}",
            "zurueck": request.headers.get("referer") or "/",
        },
        status_code=500,
    )


async def _startfehler(request: Request, call_next):
    """Konnte die Datenbank beim Start nicht geoeffnet werden, sagt das JEDE Seite.

    Status 503: der Dienst ist da, kann aber nicht arbeiten. /gesund antwortet
    als JSON mit status "fehler" und derselben Meldung, damit ein Startskript
    sie ebenfalls sieht. Nur das Stylesheet geht durch -- die Fehlerseite soll
    aussehen wie der Rest der Anwendung.
    """
    meldung = getattr(request.app.state, "startfehler", None)
    pfad = request.url.path
    if not meldung or pfad.startswith("/static/") or pfad == "/favicon.ico":
        return await call_next(request)
    if getattr(request.app.state, "startfehler_erneut_versuchen", False):
        from starlette.concurrency import run_in_threadpool

        if await run_in_threadpool(_erneut_oeffnen, request.app):
            return await call_next(request)
    if pfad == "/gesund":
        return JSONResponse(
            {"status": "fehler", "schema_version": None, "eingerichtet": False,
             "meldung": meldung},
            status_code=503,
        )
    return VORLAGEN.TemplateResponse(
        request,
        "fehler.html",
        {
            "ueberschrift": "Die Datenbank lässt sich nicht öffnen",
            "saetze": [
                meldung,
                "Bis dahin ist die Anwendung gesperrt; geschrieben wurde nichts.",
            ],
            "zurueck": "/",
        },
        status_code=503,
    )


#: Was ohne eingerichteten Betrieb erreichbar bleibt: die Einrichtung selbst,
#: der Gesundheitspunkt, das (leere) Symbol und das Stylesheet, ohne das die
#: Einrichtung nackt waere.
_OHNE_BETRIEB_ERREICHBAR = ("/einrichtung", "/gesund", "/favicon.ico")


async def _abholen_freigeben(request: Request, call_next):
    """Die erste Anfrage zeigt: der Server lauscht -- das Abholen darf los."""
    los = getattr(request.app.state, "abholen_los", None)
    if los is not None and not los.is_set():
        los.set()
    return await call_next(request)


async def _erststart(request: Request, call_next):
    """Solange kein Betrieb existiert, fuehrt jeder Weg zur Einrichtung.

    Vorher pruefte das jede Route fuer sich -- und sieben taten es nicht.
    Kunden, Zeiten und Sicherungen liessen sich vor der Einrichtung anlegen;
    ein Kunde bekam dabei "K-0001", auch wenn der Nutzer danach ein anderes
    Nummernformat einstellte: zwei Nummernsysteme im Stamm. Eine Stelle, die
    fuer alle gilt, kann niemand vergessen.

    Die Pruefung ist eine Abfrage auf einer eigenen, sofort geschlossenen
    Verbindung: die Routen holen ihre Verbindung ueber hole_conn() in ihrem
    Thread, und eine SQLite-Verbindung gehoert dem Thread, der sie oeffnet.
    """
    pfad = request.url.path
    if pfad in _OHNE_BETRIEB_ERREICHBAR or pfad.startswith("/static/"):
        return await call_next(request)
    verbindung = db.verbinde(request.app.state.datenpfad)
    try:
        eingerichtet = verbindung.execute("SELECT 1 FROM betrieb WHERE id = 1").fetchone()
    finally:
        verbindung.close()
    if eingerichtet is None:
        return RedirectResponse("/einrichtung", status_code=303)
    return await call_next(request)


# ------------------------------------------------------ Herkunft der Anfrage

#: Die Namen, unter denen das Steuer-Tool antwortet (Port beliebig).
#: "testserver" ist der Name, den der TestClient von Starlette sendet; ein
#: Browser kommt zu ihm nur ueber einen gleichnamigen Rechner im eigenen Netz.
ERLAUBTE_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "testserver"})

#: Methoden, die nach HTTP nichts aendern. Alles andere gilt als schreibend.
_LESEND = frozenset({"GET", "HEAD", "OPTIONS"})


def _hostname(kopf: str | None) -> str | None:
    """Der Name aus dem Host-Kopf ohne Port, klein geschrieben; Unsinn -> None.

    >>> _hostname("127.0.0.1:8737"), _hostname("LocalHost"), _hostname("[::1]:8737")
    ('127.0.0.1', 'localhost', '[::1]')
    >>> _hostname("angreifer.example"), _hostname("localhost:abc"), _hostname(None)
    ('angreifer.example', None, None)
    """
    kopf = (kopf or "").strip().lower()
    if not kopf:
        return None
    if kopf.startswith("["):
        ende = kopf.find("]")
        if ende < 0:
            return None
        name, rest = kopf[: ende + 1], kopf[ende + 1 :]
    else:
        name, doppelpunkt, port = kopf.partition(":")
        rest = doppelpunkt + port
    if rest and not (rest[0] == ":" and rest[1:].isascii() and rest[1:].isdigit()):
        return None
    return name


def _herkunft(url: str | None) -> tuple[str, str, int] | None:
    """Schema, Rechner und Port einer Adresse; nicht lesbar (auch "null") -> None.

    >>> _herkunft("http://127.0.0.1:8737/zeiten?x=1")
    ('http', '127.0.0.1', 8737)
    >>> _herkunft("https://Angreifer.example/seite.html"), _herkunft("null")
    (('https', 'angreifer.example', 443), None)
    """
    try:
        teile = urlsplit((url or "").strip())
        port = teile.port
    except ValueError:
        return None
    if not teile.scheme or not teile.hostname:
        return None
    schema = teile.scheme.lower()
    return schema, teile.hostname.lower(), port or {"http": 80, "https": 443}.get(schema, 0)


def _von_fremder_seite(request: Request) -> bool:
    """Kommt die schreibende Anfrage erkennbar von einer anderen Webseite?

    Massgeblich ist zuerst ``Sec-Fetch-Site`` (setzt der Browser selbst), dann
    ``Origin`` (jeder heutige Browser sendet ihn bei POST; "null" -- etwa aus
    einem abgeschotteten iframe -- zaehlt als fremd), ersatzweise der
    ``Referer``. Fehlen Origin und Referer, kommt die Anfrage nicht aus einem
    Browserformular (curl, TestClient) und geht durch.
    """
    eigen = _herkunft(f"{request.url.scheme}://{request.headers.get('host', '')}")
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin is not None:
        return _herkunft(origin) != eigen
    referer = request.headers.get("referer")
    if referer:
        return _herkunft(referer) != eigen
    return False


async def _fremde_herkunft(request: Request, call_next):
    """Weist Anfragen ab, die nicht vom Steuer-Tool selbst kommen.

    Das Angriffsszenario: Das Steuer-Tool lauscht auf 127.0.0.1:8737, und der
    Browser, in dem es laeuft, oeffnet nebenher beliebige Webseiten. Jede davon
    kann ein unsichtbares Formular mit ``action="http://127.0.0.1:8737/einrichtung"``
    enthalten und per JavaScript sofort absenden. Der Browser schickt es ab;
    die Antwort kann die fremde Seite zwar nicht lesen, aber das Formular WIRKT:
    die IBAN auf jeder kuenftigen Rechnung ist getauscht, eine Eingangsdatei
    geloescht, das Sicherungsziel umgestellt. Darum werden schreibende
    Anfragen (alles ausser GET, HEAD, OPTIONS) abgewiesen, deren Origin,
    Sec-Fetch-Site oder Referer eine andere Herkunft nennt.

    Der zweite Weg ist DNS-Rebinding: eine fremde Domain, deren Namensauflösung
    nach dem Laden auf 127.0.0.1 umspringt. Dann gilt die fremde Seite dem
    Browser als "gleiche Herkunft" und darf auch LESEN -- etwa den
    Excel-Export mit allen Daten. Erkennbar ist das nur am Host-Kopf, der
    weiter den fremden Namen traegt. Darum antwortet das Werkzeug fuer JEDE
    Anfrage nur unter 127.0.0.1, localhost und [::1].

    Der dritte Weg ist Clickjacking: eine fremde Seite rahmt das Werkzeug
    unsichtbar ein und laesst den Nutzer im Rahmen klicken. Das Formular kommt
    dann mit der EIGENEN Herkunft an -- die Pruefung oben sieht nichts, und
    zwei Klicks genuegen fuer jeden zweistufigen Loeschdialog. Darum traegt
    jede Antwort ``X-Frame-Options: SAMEORIGIN`` und ``frame-ancestors
    'self'`` (:func:`_rahmenschutz`): einrahmen darf das Werkzeug nur sich
    selbst, etwa die PDF-Vorschau auf der Entwurfsseite.

    Die bewusste Entscheidung "kein Login" bleibt davon unberuehrt: Ein Login
    schuetzt vor fremden PERSONEN am Rechner; dagegen schuetzt das
    Benutzerkonto des Betriebssystems. Hier geht es um fremde WEBSEITEN im
    eigenen Browser des Nutzers -- die waeren auch mit Login drin, weil der
    Browser das Sitzungs-Cookie mitschicken wuerde.
    """
    return _rahmenschutz(await _herkunft_pruefen(request, call_next))


def _rahmenschutz(antwort: Response) -> Response:
    """Nur das Werkzeug selbst darf seine Seiten einrahmen.

    ``X-Frame-Options`` fuer aeltere Browser, ``frame-ancestors`` fuer alle
    heutigen. Eine Antwort mit eigener Content-Security-Policy (die
    Mail-Ansicht) behaelt sie -- sie nennt ``frame-ancestors 'self'`` selbst.
    """
    antwort.headers.setdefault("x-frame-options", "SAMEORIGIN")
    antwort.headers.setdefault("content-security-policy", "frame-ancestors 'self'")
    return antwort


async def _herkunft_pruefen(request: Request, call_next) -> Response:
    """Host und Herkunft wie in :func:`_fremde_herkunft` beschrieben."""
    host = request.headers.get("host")
    if _hostname(host) not in ERLAUBTE_HOSTS:
        return _fehlerseite(
            request, 421, "Diese Adresse gehört nicht zum Steuer-Tool",
            [f"Die Anfrage kam über den Namen „{_gekuerzt(host or '', 60)}“. Das "
             "Steuer-Tool antwortet nur unter 127.0.0.1 oder localhost, damit keine "
             "fremde Webseite über einen Umweg in der Namensauflösung an deine Daten "
             "kommt.",
             "Öffne es über die Adresse, die beim Start angezeigt wird, etwa "
             "http://127.0.0.1:8737/."],
            zurueck="/",
        )
    if request.method not in _LESEND and _von_fremder_seite(request):
        return _fehlerseite(
            request, 403,
            "Diese Aktion kam von einer anderen Webseite und wurde abgewiesen",
            ["Eine Seite außerhalb des Steuer-Tools hat versucht, hier etwas zu "
             "speichern oder zu ändern. Gespeichert wurde nichts.",
             f"Wenn du die Aktion selbst auslösen wolltest: Öffne das Steuer-Tool "
             f"direkt unter http://{host}/ und wiederhole sie dort."],
            zurueck="/",
        )
    return await call_next(request)


# ------------------------------------------------ Unsichtbare Steuerzeichen

#: C0-Steuerzeichen ausser Tab (09), Zeilenumbruch (0A) und Wagenruecklauf
#: (0D), dazu DEL (7F). Geprueft wird der DEKODIERTE Text -- also das, was
#: die Route bekaeme: "%00" und ein rohes NUL-Byte gleichermassen, der Text
#: "%00" (gesendet als "%2500") dagegen nicht. Der Ausdruck steht in
#: app/logik/eingabe.py, weil auch der Kundenimport damit prueft.
_STEUERZEICHEN = eingabe.STEUERZEICHEN

#: Der Satz, den der Nutzer sieht. Woher so ein Zeichen kommt, weiss er nicht
#: -- meist aus einer kopierten Tabelle oder einem PDF.
STEUERZEICHEN_MELDUNG = (
    "Die Eingabe enthält ein unsichtbares Steuerzeichen (z. B. aus einer kopierten "
    "Tabelle). Bitte das Feld neu tippen."
)

#: Abfrageparameter, die nur eine Meldung des Werkzeugs selbst tragen -- die
#: Umleitung nach einer Aktion haengt sie an. Angezeigt, nie gespeichert. Sie
#: koennen einen Namen aus Altdaten zitieren, der noch ein Steuerzeichen
#: enthaelt; sie abzuweisen hiesse, nach einer GELUNGENEN Aktion eine
#: Fehlerseite zu zeigen.
_MELDUNGSPARAMETER = frozenset({"fehler", "hinweis", "erfolg"})


def _felder_mit_steuerzeichen(roh: bytes, *, ausser: frozenset[str] = frozenset()) -> list[str]:
    """Die Felder eines urlkodierten Textes, die ein Steuerzeichen tragen.

    Fuer Formularrumpf und Abfrage der Adresse gleichermassen. Felder aus
    ``ausser`` werden nur am Namen geprueft, nicht am Wert.

    >>> _felder_mit_steuerzeichen(b"name=a%00b&ort=Berlin&titel=%1b&x=%7F&a=%0c")
    ['name', 'titel', 'x', 'a']
    >>> _felder_mit_steuerzeichen(b"notiz=Zeile+1%0D%0AZeile+2%09Tab&rabatt=100%2500")
    []
    >>> _felder_mit_steuerzeichen(b"name=a\\x00b"), _felder_mit_steuerzeichen(b"")
    (['name'], [])
    >>> _felder_mit_steuerzeichen(b"hinweis=%E2%80%9E%00%E2%80%9C&kunde=1%00",
    ...                           ausser=_MELDUNGSPARAMETER)
    ['kunde']
    """
    felder: list[str] = []
    for name, wert in parse_qsl(roh.decode("latin-1"), keep_blank_values=True,
                                errors="replace"):
        if _STEUERZEICHEN.search(name) or (
            name not in ausser and _STEUERZEICHEN.search(wert)
        ):
            felder.append(_STEUERZEICHEN.sub("", name))
    return felder


def _ist_urlkodiertes_formular(request: Request) -> bool:
    """Ein gewoehnliches Formular (application/x-www-form-urlencoded)?

    Datei-Uploads (multipart/form-data) bleiben aussen vor: eine PDF oder ein
    Foto besteht zu grossen Teilen aus genau diesen Bytes.
    """
    art = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return art == "application/x-www-form-urlencoded"


async def _steuerzeichen(request: Request, call_next):
    """Weist Formulare und Adressen mit unsichtbaren Steuerzeichen ab: 422.

    Ein einzelnes NUL ("\\x00") ueberstand jedes ``strip()`` und galt damit als
    ausgefuellt. So liessen sich Kundenname, Zeittitel, Leistungstitel,
    Anlagenbezeichnung und Abgangsgrund "fuellen", ohne dass etwas zu sehen
    war -- bis hin zur festgeschriebenen Rechnung ohne Empfaengernamen. Jede
    Route fuer sich zu haerten hiesse, die naechste zu vergessen; darum steht
    die Pruefung hier, vor allen Routen.

    Tab, Zeilenumbruch und Wagenruecklauf sind erlaubt: ein mehrzeiliges
    Notizfeld schickt seine Zeilen als %0D%0A. Gelesen wird der Rumpf ueber
    ``request.body()``; Starlette reicht ihn danach unveraendert an die Route
    weiter. In der Adresse bleiben die Meldungsparameter (fehler, hinweis,
    erfolg) aussen vor, siehe ``_MELDUNGSPARAMETER``.

    Die Seite nennt die betroffenen Felder: das Zeichen ist unsichtbar, und
    ein Formular aus einem Dutzend Feldern laesst sich nicht auf Verdacht
    neu tippen.
    """
    felder = _felder_mit_steuerzeichen(request.scope.get("query_string") or b"",
                                       ausser=_MELDUNGSPARAMETER)
    if not felder and _ist_urlkodiertes_formular(request):
        felder = _felder_mit_steuerzeichen(await request.body())
    if felder:
        betroffen = ", ".join(f"„{_gekuerzt(feld, 40)}“" for feld in dict.fromkeys(felder))
        return _fehlerseite(
            request, 422, "Diese Eingabe konnte nicht verarbeitet werden",
            [STEUERZEICHEN_MELDUNG,
             f"Betroffen: {'Feld' if len(set(felder)) == 1 else 'Felder'} {betroffen}. "
             "Gespeichert wurde dabei nichts."],
        )
    return await call_next(request)


def _riesige_kennung(teil: str) -> bool:
    """Ein Pfadteil aus mehr als 18 Ziffern ist keine Kennung dieses Werkzeugs.

    >>> _riesige_kennung("9223372036854775808"), _riesige_kennung("12"), _riesige_kennung("abc")
    (True, False, False)
    """
    return len(teil) > 18 and teil.isascii() and teil.isdigit()


async def _ueberlange_kennung(request: Request, call_next):
    """Kennungen ab 19 Stellen im Pfad: die deutsche 404-Seite.

    SQLite speichert hoechstens 2**63-1 (19 Stellen). /kunden/9223372036854775808
    passierte FastAPIs Pruefung als ``int`` und endete beim Binden in der
    Datenbank mit einem OverflowError -- in 16 Routen ein HTTP 500. Eine
    solche Adresse fuehrt auf keine Seite; das sagt jetzt die 404-Seite, an
    einer Stelle fuer alle Routen. Was trotzdem durchrutscht (negative
    Riesenzahlen, Formularfelder), faengt ``_zahl_zu_gross`` ab.
    """
    if any(_riesige_kennung(teil) for teil in request.url.path.split("/")):
        return _seite_fehlt(request)
    return await call_next(request)


class _KopfWieGet:
    """HEAD auf eine Seite: dieselbe Antwort wie GET, nur ohne Rumpf.

    FastAPI meldet eine mit ``@router.get`` angelegte Route nur fuer GET an;
    HEAD / endete deshalb in einer 405 "Diese Seite lässt sich nur anzeigen"
    -- obwohl HEAD genau das will. Starlette beantwortet HEAD bei seinen
    eigenen Routen ebenso: GET ausfuehren, Rumpf weglassen. Die Kopfzeilen
    (auch Content-Length) bleiben die der GET-Antwort, wie HTTP es fuer HEAD
    vorsieht. Eine reine Formularadresse antwortet weiter mit 405, eine
    unbekannte mit 404 -- beides ohne Rumpf.

    Ein reines ASGI-Glied statt ``@middleware("http")``: es muss die Methode
    im ``scope`` tauschen, bevor irgendeine Route sie sieht, und jede
    Rumpf-Nachricht der Antwort leeren, auch die einer gestreamten Datei.
    """

    def __init__(self, anwendung) -> None:
        self.anwendung = anwendung

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.anwendung(scope, receive, send)
            return

        async def ohne_rumpf(nachricht) -> None:
            if nachricht["type"] == "http.response.body":
                nachricht = {**nachricht, "body": b""}
            await send(nachricht)

        await self.anwendung(dict(scope, method="GET"), receive, ohne_rumpf)


async def _kein_symbol() -> Response:
    """/favicon.ico: leer, Status 204.

    Die Seiten setzen ein leeres Symbol (basis.html), eine im Browser
    angezeigte PDF oder Belegdatei aber nicht -- dort fragt der Browser
    /favicon.ico ab und meldete in der Konsole einen 404.
    """
    return Response(status_code=204)


def erzeuge_app(datenpfad: Path | str | None = None) -> FastAPI:
    # Ohne /docs, /redoc und /openapi.json: FastAPIs Entwicklerseiten listen
    # jede Route mit ihren Feldern und laden Skripte aus dem Netz. Fuer den
    # Nutzer eines lokalen Werkzeugs sind sie nur eine verwirrende Seite mehr.
    anwendung = FastAPI(title="Steuer-Tool", lifespan=lebenszyklus,
                        docs_url=None, redoc_url=None, openapi_url=None)
    anwendung.state.datenpfad = datenpfad
    anwendung.state.startfehler = None
    anwendung.state.belegordner = abholen.Laufstand()
    anwendung.middleware("http")(_abholen_freigeben)
    anwendung.middleware("http")(_erststart)
    # Nach _erststart registriert, also AUSSEN: die Startpruefung kommt vor
    # der Erststart-Weiche, die selbst schon die Datenbank oeffnen wuerde.
    anwendung.middleware("http")(_startfehler)
    anwendung.middleware("http")(_ueberlange_kennung)
    # Direkt innerhalb der Herkunftspruefung: eine fremde Seite bekommt ihre
    # 403, bevor hier ein Rumpf gelesen wird; jede eigene Eingabe mit
    # Steuerzeichen bekommt die 422, bevor sie Datenbank oder Route erreicht.
    anwendung.middleware("http")(_steuerzeichen)
    # Zuletzt registriert, also ganz AUSSEN: eine Anfrage von fremder Herkunft
    # erreicht weder die Datenbank noch eine Route.
    anwendung.middleware("http")(_fremde_herkunft)
    # Noch weiter aussen: HEAD wird zu GET, bevor irgendein Glied die Methode
    # liest -- so gilt fuer HEAD jede Pruefung genau wie fuer GET.
    anwendung.add_middleware(_KopfWieGet)
    anwendung.add_api_route("/favicon.ico", _kein_symbol, methods=["GET"],
                            include_in_schema=False)
    anwendung.include_router(start.router)
    anwendung.include_router(kunden.router)
    anwendung.include_router(zeiten.router)
    anwendung.include_router(rechnungen.router)
    anwendung.include_router(rechnungsablauf.router)
    anwendung.include_router(backup.router)
    anwendung.include_router(verwaltung.router)
    anwendung.include_router(belege.router)
    anwendung.include_router(belegkorrektur.router)
    anwendung.include_router(belegordner.router)
    anwendung.include_router(eingang.router)
    anwendung.include_router(anlagen.router)
    anwendung.include_router(euer.router)
    anwendung.include_router(steuer.router)
    anwendung.include_router(einnahmen.router)
    anwendung.include_router(wiso.router)
    anwendung.add_exception_handler(RequestValidationError, _eingabefehler)
    anwendung.add_exception_handler(StarletteHTTPException, _nicht_gefunden)
    anwendung.add_exception_handler(OverflowError, _zahl_zu_gross)
    anwendung.add_exception_handler(Exception, _serverfehler)
    # Das Stylesheet als eigene Datei, nicht in jeder Seite eingebettet: der
    # Browser laedt es einmal, und das Design hat einen Ort.
    anwendung.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )
    return anwendung


app = erzeuge_app()

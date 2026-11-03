"""Kundenstamm.

Der Rechnungsempfaenger ist ausschliesslich aus dem Stamm waehlbar, nie als
Freitext: § 34a Satz 1 Nr. 1 UStDV verlangt vollstaendigen Namen UND
vollstaendige Anschrift, und ein freier Textbaustein ist genau der Weg, auf dem
heute der falsche Kundenname auf der Rechnung landet.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app import db
from app.filter import LAENDER
from app.logik import eingabe, kundenimport
from app.logik import kunden as logik
from app.web import VORLAGEN, betrieb_oder_none, dateikopf, hole_conn

router = APIRouter(prefix="/kunden")

# Die erlaubten Werte stehen in der Logik: Formular und Import pruefen gleich.
GRUPPIERUNGEN = logik.GRUPPIERUNGEN
TYPEN = logik.TYPEN
LEISTUNGSORTE = logik.LEISTUNGSORTE
#: Die Rundungsregeln am Kunden. Leer heisst: wie der Betrieb.
RUNDUNGSMODI = ("KEINE", "AUFWAERTS", "KAUFMAENNISCH", "ABWAERTS")
#: Laenger als eine Stunde ist kein Rundungstakt mehr, sondern ein Tippfehler.
RUNDUNG_TAKT_MAX = 60
#: Die Grundlage ist ein Verweis ("Angebot vom 03.02.2026"), kein Vertragstext.
RUNDUNG_GRUNDLAGE_MAX = 200

#: So viele Zeiten zaehlt die Rueckfrage "Kunde loeschen" einzeln auf.
LOESCHEN_AUFZAEHLEN = 20

#: Mehr offene Zeiten zeigt die Kundenseite nicht; der Rest steht in der
#: Zeitenliste. Die AELTESTEN zuerst -- das sind die, die auf eine Rechnung
#: warten (§ 14 Abs. 2 Satz 2 UStG: sechs Monate).
OFFENE_ZEITEN_MAX = 50

KEIN_KUNDE = "Diesen Kunden gibt es nicht (mehr)."


def _meldung(ziel: str, **texte: str) -> RedirectResponse:
    """Umleitung mit Meldung(en) als Abfrageparameter: fehler=..., hinweis=..."""
    teile = [f"{schluessel}={quote(text)}" for schluessel, text in texte.items() if text]
    if not teile:
        return RedirectResponse(ziel, status_code=303)
    trenner = "&" if "?" in ziel else "?"
    return RedirectResponse(f"{ziel}{trenner}{'&'.join(teile)}", status_code=303)


@router.get("", response_class=HTMLResponse)
def liste(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    # Stillgelegte Kunden sind ausgeblendet -- dafuer legt man sie still.
    # ?alle=1 zeigt sie wieder, gedaempft und mit Chip.
    alle = request.query_params.get("alle") == "1"
    stillgelegt = conn.execute("SELECT COUNT(*) FROM kunde WHERE aktiv = 0").fetchone()[0]
    kunden = conn.execute(
        """
        SELECT k.*,
               COALESCE(o.minuten, 0) AS offene_minuten,
               COALESCE(f.offen_cent, 0) AS offen_cent
          FROM kunde k
          LEFT JOIN (
              SELECT kunde_id, SUM(dauer_minuten) AS minuten
                FROM zeiteintrag
               WHERE rechnung_id IS NULL AND abgeschrieben_am IS NULL
                 AND abrechnungsmodus <> 'INTERN'
               GROUP BY kunde_id
          ) o ON o.kunde_id = k.id
          LEFT JOIN (
              SELECT kunde_id, SUM(offen_cent) AS offen_cent
                FROM v_offene_posten GROUP BY kunde_id
          ) f ON f.kunde_id = k.id
         WHERE ? OR k.aktiv = 1
         ORDER BY k.aktiv DESC, k.name COLLATE DEUTSCH
        """,
        (alle,),
    ).fetchall()
    return VORLAGEN.TemplateResponse(
        request,
        "kunden.html",
        {
            "kunden": kunden,
            "alle": alle,
            "stillgelegt": stillgelegt,
            "fehler": request.query_params.get("fehler"),
            "erfolg": request.query_params.get("erfolg"),
            "hinweis": request.query_params.get("hinweis"),
        },
    )


# ------------------------------------------------------------------ Formular

def _formular_aus_kunde(kunde: sqlite3.Row) -> dict[str, str]:
    """Die Feldwerte des Formulars aus einer gespeicherten Zeile.

    Alles als Text, so wie es im Eingabefeld steht. Der Satz wird mit zwei
    Dezimalen und Komma gezeigt -- auch ein gespeicherter Satz von 0, damit
    der Grund sichtbar ist, wenn das Speichern ihn abweist.
    """
    satz = kunde["satz_cent"]
    ziel = kunde["zahlungsziel_tage"]
    return {
        "name": kunde["name"] or "",
        "rechnungsname": kunde["rechnungsname"] or "",
        "ansprechpartner": kunde["ansprechpartner"] or "",
        "strasse": kunde["strasse"] or "",
        "plz": kunde["plz"] or "",
        "ort": kunde["ort"] or "",
        "land": kunde["land"] or "DE",
        "email": kunde["email"] or "",
        "ust_idnr": kunde["ust_idnr"] or "",
        "typ": kunde["typ"],
        "leistungsort": kunde["leistungsort"],
        "satz_euro": f"{satz / 100:.2f}".replace(".", ",") if satz is not None else "",
        "zahlungsziel": str(ziel) if ziel is not None else "",
        "gruppierung": kunde["gruppierung_default"],
        "rundung_modus": kunde["rundung_modus"] or "",
        "rundung_takt": (str(kunde["rundung_intervall_min"])
                         if kunde["rundung_intervall_min"] is not None else ""),
        "rundung_grundlage": kunde["rundung_grundlage"] or "",
        "notiz": kunde["notiz"] or "",
        "aktiv": "1" if kunde["aktiv"] else "0",
        "kundennummer": "",
    }


def _formular_aus_eingaben(**felder: str) -> dict[str, str]:
    """Die soeben eingegebenen Werte, unveraendert -- fuer den Fehlerzweig.

    Wer ein Pflichtfeld vergisst, soll genau das nachtragen und nicht alles
    noch einmal tippen. Normalisiert wird hier nichts: der Nutzer soll
    wiederfinden, was er geschrieben hat.
    """
    return {schluessel: wert or "" for schluessel, wert in felder.items()}


def _offene_zeiten(conn: sqlite3.Connection, kunde_id: int) -> tuple[list[sqlite3.Row], int]:
    """Die aeltesten offenen Zeiten des Kunden und ihre Gesamtzahl."""
    gesamt = conn.execute(
        """
        SELECT COUNT(*) FROM v_zeiteintrag_status
         WHERE kunde_id = ? AND status IN ('AUFGESCHRIEBEN', 'IM_ENTWURF')
        """,
        (kunde_id,),
    ).fetchone()[0]
    offene = conn.execute(
        """
        SELECT * FROM v_zeiteintrag_status
         WHERE kunde_id = ? AND status IN ('AUFGESCHRIEBEN', 'IM_ENTWURF')
         ORDER BY datum ASC, id ASC LIMIT ?
        """,
        (kunde_id, OFFENE_ZEITEN_MAX),
    ).fetchall()
    return offene, int(gesamt)


def _rendere_formular(
    request: Request,
    conn: sqlite3.Connection,
    *,
    kunde: sqlite3.Row | None,
    formular: dict[str, str],
    fehler: str | None = None,
    hinweis: str | None = None,
    status_code: int = 200,
):
    """Das Kundenformular -- beim Anlegen, Bearbeiten und in beiden Fehlerzweigen.

    Ein Ort fuer die Betriebsvorgaben (Standardsatz, Zahlungsziel) und die
    offenen Zeiten, damit kein Zweig sie vergisst oder mit Platzhaltern
    fuellt: "leer = Standard 0,00 €" stand so lange im Fehlerfall, wie jeder
    Zweig seinen eigenen Kontext baute.
    """
    betrieb = betrieb_oder_none(conn)
    offene, offene_gesamt = _offene_zeiten(conn, kunde["id"]) if kunde else ([], 0)
    # Was erst beim Festschreiben ROT wuerde (Reverse Charge ohne USt-IdNr.,
    # Auslandsunternehmer mit Inland ...), steht schon hier -- als Hinweis,
    # solange es so gespeichert ist: die USt-IdNr. darf nachgetragen werden,
    # bevor die erste Rechnung entsteht. Im Fehlerzweig nicht: dort steht im
    # Formular etwas anderes als gespeichert.
    unfertig = list(logik.querpruefung(dict(kunde)).values()) if kunde and not fehler else []
    return VORLAGEN.TemplateResponse(
        request,
        "kunde_formular.html",
        {
            "kunde": kunde,
            "formular": formular,
            "kundennummer": kunde["kundennummer"] if kunde else logik.naechste_kundennummer(conn),
            "standardsatz_cent": betrieb["standardsatz_cent"] if betrieb else 0,
            "zahlungsziel_tage": betrieb["zahlungsziel_tage"] if betrieb else 14,
            # "wie Betrieb (15 Min. aufwärts)": die Vorgabe steht in der Auswahl.
            "betriebsregel": logik.betriebsregel(conn),
            "laender": LAENDER,
            "fehler": fehler,
            "hinweis": hinweis,
            "offene": offene,
            "offene_gesamt": offene_gesamt,
            "unfertig": unfertig,
            "loeschbarkeit": logik.loeschbarkeit(conn, kunde["id"]) if kunde else None,
            "loeschen_aufzaehlen": LOESCHEN_AUFZAEHLEN,
            "erfolg": request.query_params.get("erfolg") if not fehler else None,
        },
        status_code=status_code,
    )


@router.get("/neu", response_class=HTMLResponse)
def neu(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    return _rendere_formular(
        request, conn, kunde=None,
        formular=_formular_aus_eingaben(land="DE", typ="UNTERNEHMER", leistungsort="INLAND",
                                        gruppierung="PRO_TITEL", aktiv="1", rundung_modus=""),
    )


# ------------------------------------------------------------------- Import
#
# Die Routen stehen VOR /{kunde_id}: sonst faengt die Kundenseite die Adresse
# /kunden/import ab und antwortet mit 422 ("import" ist keine Kunden-ID).

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Zwischenspeicher:
    """Hochgeladene Dateien zwischen Vorschau und Bestaetigung -- nur im Speicher.

    Nicht im Datenordner: dort laege jede Probedatei danach in jeder
    Sicherung. Hoechstens fuenf Dateien, jede eine Stunde; nach einem
    Neustart ist die Vorschau abgelaufen, und die Seite sagt das.
    Geschluesselt nach der Pruefsumme des Inhalts: dieselbe Datei zweimal
    gewaehlt belegt einen Platz.
    """

    GRENZE = 5
    DAUER_S = 3600

    def __init__(self) -> None:
        self._sperre = threading.Lock()
        self._dateien: dict[str, tuple[float, str, bytes]] = {}

    def _aufraeumen(self, jetzt: float) -> None:
        for schluessel, (zeit, _, _) in list(self._dateien.items()):
            if jetzt - zeit > self.DAUER_S:
                del self._dateien[schluessel]

    def ablegen(self, name: str, inhalt: bytes) -> str:
        schluessel = kundenimport.pruefsumme(inhalt)
        with self._sperre:
            jetzt = time.monotonic()
            self._aufraeumen(jetzt)
            self._dateien.pop(schluessel, None)
            while len(self._dateien) >= self.GRENZE:
                del self._dateien[min(self._dateien, key=lambda s: self._dateien[s][0])]
            self._dateien[schluessel] = (jetzt, name, inhalt)
        return schluessel

    def holen(self, schluessel: str) -> tuple[str, bytes] | None:
        with self._sperre:
            self._aufraeumen(time.monotonic())
            eintrag = self._dateien.get(schluessel)
        return None if eintrag is None else (eintrag[1], eintrag[2])

    def entfernen(self, schluessel: str) -> None:
        with self._sperre:
            self._dateien.pop(schluessel, None)


_SPEICHER_SPERRE = threading.Lock()


def _speicher(request: Request) -> _Zwischenspeicher:
    with _SPEICHER_SPERRE:
        speicher = getattr(request.app.state, "kundenimport", None)
        if speicher is None:
            speicher = request.app.state.kundenimport = _Zwischenspeicher()
    return speicher


def _dateiname(roh: str | None) -> str:
    r"""Der Name der hochgeladenen Datei ohne Pfad und Steuerzeichen, hoechstens 80 Zeichen.

    >>> _dateiname(r"C:\Users\x\Kunden.xlsx"), _dateiname("../Kunden.csv"), _dateiname(None)
    ('Kunden.xlsx', 'Kunden.csv', 'kunden')
    """
    name = re.split(r"[\\/]", roh or "")[-1]
    name = eingabe.STEUERZEICHEN.sub("", name).strip()
    return name[-80:] or "kunden"


def _import_seite(request: Request, conn: sqlite3.Connection, *, fehler: str | None = None,
                  status_code: int = 200) -> HTMLResponse:
    vorlage_format = logik.format_aus_betrieb(conn)
    return VORLAGEN.TemplateResponse(
        request,
        "kunden_import.html",
        {
            "fehler": fehler or request.query_params.get("fehler"),
            "spalten": kundenimport.SPALTEN,
            "max_zeilen": kundenimport.MAX_ZEILEN,
            "max_mb": kundenimport.MAX_BYTES // 1024 // 1024,
            "format_beispiel": vorlage_format.format(1),
            "naechste": logik.naechste_kundennummer(conn),
        },
        status_code=status_code,
    )


def _vorschau(request: Request, conn: sqlite3.Connection, plan: kundenimport.Plan, *,
              name: str, schluessel: str, meldung: str | None = None,
              status_code: int = 200) -> HTMLResponse:
    return VORLAGEN.TemplateResponse(
        request,
        "kunden_import_vorschau.html",
        {
            "plan": plan,
            "name": name,
            "schluessel": schluessel,
            "frueher": kundenimport.frueherer_import(conn, schluessel),
            "meldung": meldung,
            "anzahl": {s: plan.anzahl(s) for s in (
                kundenimport.NEU, kundenimport.NAMENSGLEICH, kundenimport.VORHANDEN,
                kundenimport.FEHLER, kundenimport.UEBERSPRUNGEN)},
            "spalten": kundenimport.SPALTE,
        },
        status_code=status_code,
    )


@router.get("/import", response_class=HTMLResponse)
def import_seite(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    return _import_seite(request, conn)


@router.get("/import/vorlage.xlsx")
def import_vorlage(conn: sqlite3.Connection = Depends(hole_conn)):
    return Response(kundenimport.vorlage(conn), media_type=XLSX,
                    headers=dateikopf("Kundenvorlage.xlsx", herunterladen=True))


@router.post("/import")
async def import_hochladen(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Datei annehmen, pruefen, lesen und die Vorschau zeigen. Angelegt wird hier nichts.

    Das Formular wird von Hand gelesen statt ueber einen ``UploadFile``-Parameter:
    so laesst sich eine zu grosse Datei an ihrer angekuendigten Laenge abweisen,
    bevor sie ueberhaupt entgegengenommen wird.
    """
    laenge = request.headers.get("content-length", "")
    if laenge.isdigit() and int(laenge) > kundenimport.MAX_BYTES + 64 * 1024:
        return _import_seite(
            request, conn, status_code=413,
            fehler=f"Die Datei ist zu groß (höchstens {kundenimport.MAX_BYTES // 1024 // 1024} "
                   f"MB). Für {kundenimport.MAX_ZEILEN} Kunden reichen etwa 100 KB.")
    async with request.form(max_files=1, max_fields=5) as formular:
        datei = formular.get("datei")
        if not isinstance(datei, UploadFile) or not datei.filename:
            return _import_seite(request, conn, fehler="Bitte eine Datei wählen.",
                                 status_code=422)
        name = _dateiname(datei.filename)
        inhalt = await datei.read(kundenimport.MAX_BYTES + 1)
    # Lesen und Abgleichen im Threadpool: die Route ist ``async`` (nur fuer das
    # Formular), und was sie direkt tut, haelt die Ereignisschleife an -- jede
    # andere Anfrage des Tools wartete solange, auch eine Seite im zweiten Tab.
    try:
        gelesen = await run_in_threadpool(kundenimport.lies, name, inhalt,
                                          logik.format_aus_betrieb(conn))
    except kundenimport.ImportFehler as fehler:
        return _import_seite(request, conn, fehler=str(fehler), status_code=422)
    plan = await run_in_threadpool(kundenimport.plane, conn, gelesen)
    schluessel = _speicher(request).ablegen(name, inhalt)
    return _vorschau(request, conn, plan, name=name, schluessel=schluessel)


#: Die Meldung nach einem zweiten Klick auf "anlegen".
SCHON_ANGELEGT = ("Diese Kunden sind bereits angelegt — der Import lief schon. Nichts doppelt "
                  "angelegt.")


@router.post("/import/anlegen")
def import_anlegen(
    request: Request,
    schluessel: str = Form(""),
    fingerabdruck: str = Form(""),
    trotzdem: list[str] = Form(default=[]),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Legt die Kunden der Vorschau an. Der Browser schickt keine Kundendaten zurueck.

    Gelesen wird die Datei aus dem Zwischenspeicher, geplant noch einmal
    unter der Schreibsperre (``kundenimport.lege_an``). Hat sich der Bestand
    inzwischen geaendert, kommt die neue Vorschau statt eines Imports.
    """
    if not (re.fullmatch(r"[0-9a-f]{64}", schluessel)
            and re.fullmatch(r"[0-9a-f]{64}", fingerabdruck)):
        return _meldung("/kunden/import",
                        fehler="Die Vorschau ist unvollständig. Bitte die Datei erneut wählen.")
    bestaetigt = {k for k in (eingabe.kennung(n) for n in trotzdem) if k is not None}
    datei = _speicher(request).holen(schluessel)
    if datei is None:
        if kundenimport.schon_angelegt(conn, schluessel, fingerabdruck, bestaetigt):
            return _meldung("/kunden", hinweis=SCHON_ANGELEGT)
        return _meldung("/kunden/import", fehler=(
            "Die Vorschau ist abgelaufen (nach einer Stunde oder einem Neustart). Bitte die "
            "Datei erneut wählen — angelegt wurde noch nichts."))
    name, inhalt = datei
    try:
        ergebnis = kundenimport.lege_an(conn, name, inhalt, fingerabdruck=fingerabdruck,
                                        trotzdem=bestaetigt)
    except kundenimport.VorschauVeraltet as veraltet:
        return _vorschau(
            request, conn, veraltet.plan, name=name, schluessel=schluessel, status_code=409,
            meldung="Der Kundenstamm hat sich seit der Vorschau geändert (ein Kunde kam dazu "
                    "oder das Nummernformat wurde umgestellt). Angelegt wurde nichts — bitte "
                    "diese neue Vorschau prüfen.")
    except kundenimport.ImportFehler as fehler:
        return _meldung("/kunden/import", fehler=str(fehler))
    _speicher(request).entfernen(schluessel)
    if ergebnis.schon_erledigt:
        return _meldung("/kunden", hinweis=SCHON_ANGELEGT)
    # Nach Nummer, nicht nach Zeile: die namensgleichen bekommen ihre Nummern
    # nach den neuen.
    nummern = sorted((nummer for _, nummer, _ in ergebnis.angelegt),
                     key=lambda n: (len(n), n))
    if not nummern:
        text = "Keine Kunden angelegt."
    elif len(nummern) == 1:
        text = f"1 Kunde angelegt: {nummern[0]} {ergebnis.angelegt[0][2]}."
    elif len(nummern) <= 5:
        text = f"{len(nummern)} Kunden angelegt: {', '.join(nummern)}."
    else:
        text = f"{len(nummern)} Kunden angelegt ({nummern[0]} bis {nummern[-1]})."
    rest = []
    if ergebnis.uebersprungen:
        rest.append(f"{ergebnis.uebersprungen} übersprungen")
    if ergebnis.fehlerhaft:
        rest.append(f"{ergebnis.fehlerhaft} fehlerhaft, nicht angelegt")
    if rest:
        text += " " + "; ".join(rest) + "."
    return _meldung("/kunden", erfolg=text)


@router.get("/{kunde_id}", response_class=HTMLResponse)
def bearbeiten(kunde_id: int, request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    kunde = conn.execute("SELECT * FROM kunde WHERE id = ?", (kunde_id,)).fetchone()
    if kunde is None:
        return _meldung("/kunden", fehler=KEIN_KUNDE)
    return _rendere_formular(
        request, conn, kunde=kunde, formular=_formular_aus_kunde(kunde),
        fehler=request.query_params.get("fehler"),
        hinweis=request.query_params.get("hinweis"),
    )


def _rundung(
    modus: str, takt: str, grundlage: str
) -> tuple[tuple[str | None, int | None, str | None], str | None]:
    """Die Rundungsregel des Kunden aus dem Formular: (Regel, Takt, Grundlage) oder Fehler.

    Leer heisst "wie Betrieb" -- dann gilt auch dessen Takt, ein
    eingetragener wird nicht gespeichert. "Nicht runden" braucht keinen Takt.
    Ohne Takt rundet eine eigene Regel im Takt des Betriebs.
    """
    modus = (modus or "").strip()
    if modus and modus not in RUNDUNGSMODI:
        return (None, None, None), f"Unbekannte Rundungsregel: „{modus[:40]}“"
    text = (grundlage or "").strip()
    if len(text) > RUNDUNG_GRUNDLAGE_MAX:
        return (None, None, None), (
            f"Die Grundlage der Rundung ist ein Verweis, etwa „Angebot vom 03.02.2026“ — "
            f"höchstens {RUNDUNG_GRUNDLAGE_MAX} Zeichen."
        )
    minuten = None
    if modus and modus != "KEINE" and (takt or "").strip():
        roh = takt.strip()
        if not (roh.isascii() and roh.isdigit()) or not 1 <= int(roh) <= RUNDUNG_TAKT_MAX:
            return (None, None, None), (
                f"Der Rundungstakt muss eine ganze Zahl von 1 bis {RUNDUNG_TAKT_MAX} Minuten "
                f"sein. Eingegeben war: „{roh[:40]}“"
            )
        minuten = int(roh)
    return (modus or None, minuten, text or None), None


def _formularwerte(
    name: str, strasse: str, plz: str, ort: str, land: str, rechnungsname: str,
    ansprechpartner: str, email: str, ust_idnr: str, typ: str, leistungsort: str,
    satz_euro: str, zahlungsziel: str, gruppierung: str, notiz: str, aktiv: str,
    rundung_modus: str = "", rundung_takt: str = "", rundung_grundlage: str = "",
) -> tuple[dict[str, object], str | None]:
    """Prueft und normalisiert die Eingaben. Gibt (Werte, ersten Fehlertext) zurueck.

    Die Pruefung der Stammdaten steht in :func:`logik.pruefe_kunde` --
    derselben, die der Import benutzt. Das Formular zeigt eine Meldung, die
    erste. Die Rundungsregel (Version 21) prueft das Formular selbst: der
    Import nimmt sie nicht auf, ein importierter Kunde rechnet wie der Betrieb.
    """
    werte, fehler = logik.pruefe_kunde({
        "name": name, "strasse": strasse, "plz": plz, "ort": ort, "land": land,
        "rechnungsname": rechnungsname, "ansprechpartner": ansprechpartner, "email": email,
        "ust_idnr": ust_idnr, "typ": typ, "leistungsort": leistungsort, "satz_euro": satz_euro,
        "zahlungsziel": zahlungsziel, "gruppierung": gruppierung, "notiz": notiz,
        "aktiv": aktiv,
    })
    (regel, takt, grundlage), problem = _rundung(rundung_modus, rundung_takt, rundung_grundlage)
    erster = next(iter(fehler.values()), None) or problem
    if erster:
        return werte, erster
    werte.update({
        "rundung_modus": regel,
        "rundung_intervall_min": takt,
        "rundung_grundlage": grundlage,
    })
    return werte, None


@router.post("")
def anlegen(
    request: Request,
    # Bewusst mit Default statt Form(...): sonst antwortet FastAPI mit seiner
    # generischen JSON-Fehlermeldung, bevor die eigene Pruefung ueberhaupt laeuft --
    # und der Nutzer sieht 'Field required' statt des Hinweises auf § 34a UStDV.
    name: str = Form(""), strasse: str = Form(""), plz: str = Form(""), ort: str = Form(""),
    land: str = Form("DE"), rechnungsname: str = Form(""), ansprechpartner: str = Form(""),
    email: str = Form(""), ust_idnr: str = Form(""), typ: str = Form("UNTERNEHMER"),
    leistungsort: str = Form("INLAND"), satz_euro: str = Form(""), zahlungsziel: str = Form(""),
    gruppierung: str = Form("PRO_TITEL"), notiz: str = Form(""), aktiv: str = Form("1"),
    rundung_modus: str = Form(""), rundung_takt: str = Form(""),
    rundung_grundlage: str = Form(""),
    kundennummer: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    werte, fehler = _formularwerte(
        name, strasse, plz, ort, land, rechnungsname, ansprechpartner, email, ust_idnr,
        typ, leistungsort, satz_euro, zahlungsziel, gruppierung, notiz, aktiv,
        rundung_modus, rundung_takt, rundung_grundlage,
    )
    # Eine von Hand eingegebene Nummer darf den Altbestand abbilden, aber keine
    # vergebene ueberschreiben. Geprueft wird VOR der Transaktion, damit der
    # Nutzer seine Eingaben zurueckbekommt statt eines Datenbankfehlers.
    gewuenscht: str | None = None
    if not fehler and kundennummer.strip():
        try:
            gewuenscht = logik.pruefe_kundennummer(conn, kundennummer)
        except ValueError as problem:
            fehler = str(problem)
    if fehler:
        return _rendere_formular(
            request, conn, kunde=None, fehler=fehler, status_code=422,
            formular=_formular_aus_eingaben(
                name=name, strasse=strasse, plz=plz, ort=ort, land=land,
                rechnungsname=rechnungsname, ansprechpartner=ansprechpartner, email=email,
                ust_idnr=ust_idnr, typ=typ, leistungsort=leistungsort, satz_euro=satz_euro,
                zahlungsziel=zahlungsziel, gruppierung=gruppierung, notiz=notiz, aktiv=aktiv,
                kundennummer=kundennummer, rundung_modus=rundung_modus,
                rundung_takt=rundung_takt, rundung_grundlage=rundung_grundlage,
            ),
        )
    # Gleicher Name ist erlaubt, aber nicht unbemerkt: in den Auswahllisten
    # staenden sonst zwei gleiche Zeilen, und die Zeit landet beim Falschen.
    hinweis = logik.hinweis_namensdoppel(conn, werte["name"])

    conn.execute("BEGIN IMMEDIATE")
    try:
        nummer = gewuenscht or logik.naechste_kundennummer(conn)
        spalten = ["kundennummer", *werte, "angelegt_am"]
        cur = conn.execute(
            f"INSERT INTO kunde ({', '.join(spalten)})"
            f" VALUES ({', '.join('?' * len(spalten))})",
            (nummer, *werte.values(), db.heute()),
        )
        db.protokolliere(conn, "ANLEGEN", "kunde", cur.lastrowid,
                         wert_neu={"kundennummer": nummer, "name": werte["name"]})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _meldung(f"/kunden/{cur.lastrowid}", hinweis=hinweis or "")


@router.post("/{kunde_id}")
def speichern(
    kunde_id: int, request: Request,
    # Bewusst mit Default statt Form(...): sonst antwortet FastAPI mit seiner
    # generischen JSON-Fehlermeldung, bevor die eigene Pruefung ueberhaupt laeuft --
    # und der Nutzer sieht 'Field required' statt des Hinweises auf § 34a UStDV.
    name: str = Form(""), strasse: str = Form(""), plz: str = Form(""), ort: str = Form(""),
    land: str = Form("DE"), rechnungsname: str = Form(""), ansprechpartner: str = Form(""),
    email: str = Form(""), ust_idnr: str = Form(""), typ: str = Form("UNTERNEHMER"),
    leistungsort: str = Form("INLAND"), satz_euro: str = Form(""), zahlungsziel: str = Form(""),
    gruppierung: str = Form("PRO_TITEL"), notiz: str = Form(""), aktiv: str = Form("1"),
    rundung_modus: str = Form(""), rundung_takt: str = Form(""),
    rundung_grundlage: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    alt = conn.execute("SELECT * FROM kunde WHERE id = ?", (kunde_id,)).fetchone()
    if alt is None:
        return _meldung("/kunden", fehler=KEIN_KUNDE)

    werte, fehler = _formularwerte(
        name, strasse, plz, ort, land, rechnungsname, ansprechpartner, email, ust_idnr,
        typ, leistungsort, satz_euro, zahlungsziel, gruppierung, notiz, aktiv,
        rundung_modus, rundung_takt, rundung_grundlage,
    )
    if fehler:
        return _rendere_formular(
            request, conn, kunde=alt, fehler=fehler, status_code=422,
            formular=_formular_aus_eingaben(
                name=name, strasse=strasse, plz=plz, ort=ort, land=land,
                rechnungsname=rechnungsname, ansprechpartner=ansprechpartner, email=email,
                ust_idnr=ust_idnr, typ=typ, leistungsort=leistungsort, satz_euro=satz_euro,
                zahlungsziel=zahlungsziel, gruppierung=gruppierung, notiz=notiz, aktiv=aktiv,
                rundung_modus=rundung_modus, rundung_takt=rundung_takt,
                rundung_grundlage=rundung_grundlage,
            ),
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            f"UPDATE kunde SET {', '.join(f'{f} = ?' for f in werte)} WHERE id = ?",
            (*werte.values(), kunde_id),
        )
        # Nur die Felder protokollieren, die sich tatsaechlich geaendert haben.
        # Adresse und Name stehen auf Rechnungen; ihre Historie ist der Grund,
        # warum beim Festschreiben ein Snapshot in die Rechnung kopiert wird.
        for feld, neu in werte.items():
            if alt[feld] != neu:
                db.protokolliere(conn, "AENDERN", "kunde", kunde_id,
                                 feld=feld, wert_alt=alt[feld], wert_neu=neu)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return RedirectResponse(f"/kunden/{kunde_id}", status_code=303)


# ------------------------------------------------------ Stilllegen, Loeschen

@router.post("/{kunde_id}/stilllegen")
def stilllegen(kunde_id: int, conn: sqlite3.Connection = Depends(hole_conn)):
    """Aus Liste und Auswahl nehmen; Rechnungen, Zahlungen und Zeiten bleiben."""
    if logik.stilllegen(conn, kunde_id, aktiv=False) is None:
        return _meldung("/kunden", fehler=KEIN_KUNDE)
    return _meldung(f"/kunden/{kunde_id}", erfolg=(
        "Stillgelegt: der Kunde steht in keiner Auswahl für neue Zeiten und Rechnungen mehr "
        "und ist in der Kundenliste ausgeblendet."))


@router.post("/{kunde_id}/aktivieren")
def aktivieren(kunde_id: int, conn: sqlite3.Connection = Depends(hole_conn)):
    if logik.stilllegen(conn, kunde_id, aktiv=True) is None:
        return _meldung("/kunden", fehler=KEIN_KUNDE)
    return _meldung(f"/kunden/{kunde_id}", erfolg="Der Kunde ist wieder aktiv.")


@router.post("/{kunde_id}/loeschen")
def loeschen(
    kunde_id: int,
    bestaetigt: str = Form(""),
    stand: str = Form(""),
    grund: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Loescht einen Kunden ohne festgeschriebene Dokumente -- samt Zeiten und Entwuerfen.

    Zwei Schritte ohne Skript: die Rueckfrage auf der Kundenseite zaehlt
    auf, was mitgeloescht wird, und schickt ``bestaetigt=1`` und den
    ``stand`` dessen, was sie gezeigt hat. Ohne beides wird nichts geloescht.
    """
    if bestaetigt != "1" or not stand:
        return _meldung(f"/kunden/{kunde_id}", fehler=(
            "Zum Löschen bitte unten „Kunde löschen“ aufklappen und dort bestätigen."))
    try:
        geloescht = logik.loeschen(conn, kunde_id, stand=stand, grund=grund)
    except logik.KundeNichtLoeschbar as problem:
        return _meldung(f"/kunden/{kunde_id}", fehler=str(problem))
    if geloescht is None:
        return _meldung("/kunden", hinweis="Diesen Kunden gibt es nicht mehr — er ist "
                                           "bereits gelöscht.")
    k = geloescht.kunde
    mit = []
    if geloescht.zeiten:
        anzahl = len(geloescht.zeiten)
        mit.append("1 Zeit" if anzahl == 1 else f"{anzahl} Zeiten")
    if geloescht.entwuerfe:
        anzahl = len(geloescht.entwuerfe)
        mit.append("1 Entwurf" if anzahl == 1 else f"{anzahl} Entwürfen")
    text = f"{k['kundennummer']} {k['name']} gelöscht"
    text += f" (mit {' und '.join(mit)})." if mit else "."
    return _meldung("/kunden", erfolg=text + " Der Inhalt steht im Protokoll.")

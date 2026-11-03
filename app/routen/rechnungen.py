"""Rechnungen: Assistent, Vorschau, Festschreiben, Storno, Zahlungen.

Der Klickweg ist bewusst kurz: vom Dashboard aus ein Klick auf "Nicht
abgerechnet" oeffnet den Assistenten mit gesetztem Kunden und vorgewaehlten
Zeiten. Dann Vorschau, dann Festschreiben. Der Einstieg liegt also beim Problem
("da liegen Stunden") und nicht bei einem leeren Formular.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app import db, pdf, pfade
from app.logik import eingabe, einnahmen, gruppierung, kunden, nummernkreis, validierung, wiso
from app.logik import festschreiben as fs
from app.logik import rechnungsablauf as ablauf
from app.logik.rundung import euro
from app.web import VORLAGEN, betrieb_oder_none, dateikopf, filterzahl, hole_conn

router = APIRouter()


def _meldung(ziel: str, text: str) -> RedirectResponse:
    trenner = "&" if "?" in ziel else "?"
    return RedirectResponse(f"{ziel}{trenner}fehler={quote(text)}", status_code=303)


def _hinweis(ziel: str, text: str) -> RedirectResponse:
    """Wie _meldung, aber als Hinweis: etwas ist passiert, nichts ist schiefgegangen."""
    trenner = "&" if "?" in ziel else "?"
    return RedirectResponse(f"{ziel}{trenner}hinweis={quote(text)}", status_code=303)


#: Wohin eine Schnellaktion aus der Rechnungsliste oder der Uebersicht
#: zurueckfuehrt -- und welche Filter der Liste dabei mitwandern.
_RUECKWEGE = {"/": (), "/rechnungen": ("jahr", "kunde")}


def _rueckweg(ziel: str | None) -> str | None:
    """Das Rueckkehrziel einer Schnellaktion: nur Liste oder Uebersicht.

    Das versteckte Feld ``zurueck`` kommt vom Browser und ist damit Eingabe.
    Eine fremde Adresse ("https://...", "//host", "/\\host") wird verworfen,
    sonst liesse sich die Umleitung auf eine fremde Seite lenken. Von der
    Query wandern nur die Filter der Liste mit, keine alte Meldung.

    >>> _rueckweg("/rechnungen?jahr=2026&fehler=alt"), _rueckweg("/")
    ('/rechnungen?jahr=2026', '/')
    >>> _rueckweg("//fremd.example/"), _rueckweg("https://fremd.example/rechnungen")
    (None, None)
    >>> _rueckweg("/\\fremd.example"), _rueckweg("/rechnung/3"), _rueckweg("")
    (None, None, None)
    """
    text = (ziel or "").strip()
    if not text.startswith("/") or "\\" in text:
        return None
    teile = urlsplit(text)
    if teile.scheme or teile.netloc or teile.path not in _RUECKWEGE:
        return None
    erlaubt = _RUECKWEGE[teile.path]
    query = urlencode([(k, v) for k, v in parse_qsl(teile.query) if k in erlaubt])
    return f"{teile.path}?{query}" if query else teile.path


def _kunden_id(wert: str | None) -> int | None:
    """Ein Kundenschluessel aus der Adresszeile; alles andere heisst "keiner".

    Als Text angenommen und nicht als ``int``: ein von Hand geaenderter Link
    (``?kunde=abc``) soll die Seite ohne Auswahl zeigen, nicht FastAPIs
    Eingabefehler.

    Ueber ``eingabe.kennung``: "²" besteht ``str.isdigit()``, scheitert aber
    an ``int()``, und eine Zahl ab 2**63 sprengte erst die Bindung an SQLite
    -- beides endete als Serverfehler.

    >>> _kunden_id("7"), _kunden_id(" 7 "), _kunden_id(""), _kunden_id("abc"), _kunden_id(None)
    (7, 7, None, None, None)
    >>> _kunden_id("²"), _kunden_id(str(2**63))
    (None, None)
    """
    return eingabe.kennung(wert)


NICHT_VORHANDEN = "Diese Rechnung gibt es nicht (mehr)."

#: Die Zahlungsarten, die die Datenbank kennt (CHECK auf zahlung.art). Hier
#: geprueft, BEVOR die Datenbank es tut: ihre Meldung waere "CHECK constraint
#: failed: art IN (...)", und die hat auf der Oberflaeche nichts verloren.
ZAHLUNGSARTEN = ("UEBERWEISUNG", "KARTE", "BAR", "PAYPAL", "SONSTIGES")

#: Was der Nutzer liest, wenn die Datenbank eine Zahlung abweist (Trigger
#: zahlung_nur_festgeschrieben). Deren Text ist ASCII-Umschrift fuer
#: Entwickler ("Rueckzahlung") und gehoert nicht auf die Oberflaeche. Die
#: Routen pruefen die Faelle vorher selbst; das hier ist das Netz.
ZAHLUNG_ABGEWIESEN = (
    "Diese Zahlung lässt sich nicht buchen: Geld geht nur auf eine festgeschriebene "
    "Rechnung mit Betrag ein, auf eine stornierte nur als Rückzahlung und auf eine "
    "Rechnungskorrektur nie. Bitte die Seite neu laden und prüfen."
)


def _zahlung_abgewiesen(fehler: sqlite3.IntegrityError) -> str:
    """Ein IntegrityError beim Buchen einer Zahlung als deutscher Satz.

    >>> _zahlung_abgewiesen(sqlite3.IntegrityError(
    ...     "Zahlung nur zu einer festgeschriebenen Rechnung mit Betrag; ..."))[:30]
    'Diese Zahlung lässt sich nicht'
    >>> print(_zahlung_abgewiesen(sqlite3.IntegrityError("CHECK constraint failed: x")))
    Die Datenbank hat die Zahlung abgewiesen. Nichts wurde gebucht — bitte prüfen.
    """
    if "festgeschriebenen Rechnung" in str(fehler):
        return ZAHLUNG_ABGEWIESEN
    return "Die Datenbank hat die Zahlung abgewiesen. Nichts wurde gebucht — bitte prüfen."


#: Ein laufender Timer: Start ohne Ende. Die Zeit hat noch keine Dauer.
_LAEUFT = "(z.start_zeit IS NOT NULL AND z.end_zeit IS NULL)"


# ------------------------------------------------------------------ Assistent

#: Die vorbelegte Einleitung eines neuen Entwurfs.
EINLEITUNG_VORSCHLAG = "vielen Dank für Ihren Auftrag. Ich berechne wie folgt:"


@router.get("/rechnung/neu", response_class=HTMLResponse)
def assistent(
    request: Request,
    kunde: str | None = None,
    conn: sqlite3.Connection = Depends(hole_conn),
):
    return _assistent_seite(request, conn, kunde, fehler=request.query_params.get("fehler"))


def _assistent_seite(
    request: Request,
    conn: sqlite3.Connection,
    kunde: str | None,
    *,
    fehler: str | None = None,
    eingaben: dict | None = None,
    status_code: int = 200,
):
    """Die Seite des Assistenten -- leer oder mit den abgewiesenen Eingaben.

    ``eingaben`` kommt aus einem abgewiesenen Absenden (Datum, Gruppierung,
    Satz ...). Vorher leitete die Abweisung auf die leere Seite um: Betreff,
    Einleitung, Schlusstext und Haken waren weg, und die Zeitauswahl hakte
    wieder alle Zeiten an -- wer eine von drei gewaehlt hatte, schrieb nach
    der Korrektur leicht alle drei ab.
    """
    betrieb = betrieb_oder_none(conn)
    if betrieb is None:
        return RedirectResponse("/einrichtung", status_code=303)

    kundenliste = conn.execute(
        f"""
        SELECT k.id, k.name, k.gruppierung_default, k.kundennummer, k.ort,
               {kunden.doppelt_sql("k")} AS doppelt,
               COUNT(z.id) AS eintraege, COALESCE(SUM(z.dauer_minuten), 0) AS minuten
          FROM kunde k
          LEFT JOIN zeiteintrag z ON z.kunde_id = k.id AND z.rechnung_id IS NULL
               AND z.abgeschrieben_am IS NULL AND z.abrechnungsmodus <> 'INTERN'
               AND NOT {_LAEUFT}
         WHERE k.aktiv = 1
         GROUP BY k.id
         ORDER BY COUNT(z.id) DESC, k.name COLLATE DEUTSCH
        """
    ).fetchall()

    gewaehlt = _kunden_id(kunde)
    # Der Kunde selbst, nicht die Zeile aus der Liste oben: die Liste fuehrt
    # nur aktive Kunden, und die Gruppierungsvorbelegung eines inaktiven
    # Kunden mit offenen Zeiten fiel damit auf "pro Titel" zurueck.
    kunde_zeile = (
        conn.execute(
            "SELECT id, name, gruppierung_default FROM kunde WHERE id = ?", (gewaehlt,)
        ).fetchone() if gewaehlt else None
    )
    if gewaehlt and kunde_zeile is None:
        gewaehlt = None
        fehler = fehler or "Diesen Kunden gibt es nicht."

    zeiten: list[sqlite3.Row] = []
    summen: dict[str, int] = {}
    satz_fehlt = False
    timer_laeuft = False
    regel = None
    # Die am Kunden eingestellte Gruppierung. Bis hierher wurde die Spalte zwar
    # gelesen, aber nie benutzt -- die Vorlage setzte PRO_TITEL hart. Wer am
    # Kunden "pro Tag" einstellte, bekam trotzdem "pro Titel".
    modus_default = gruppierung.PRO_TITEL
    if kunde_zeile is not None:
        modus_default = kunde_zeile["gruppierung_default"] or gruppierung.PRO_TITEL
        zeiten = conn.execute(
            f"""
            SELECT z.*, v.status FROM zeiteintrag z
              JOIN v_zeiteintrag_status v ON v.id = z.id
             WHERE z.kunde_id = ? AND z.rechnung_id IS NULL AND z.abgeschrieben_am IS NULL
               AND z.abrechnungsmodus <> 'INTERN' AND NOT {_LAEUFT}
             ORDER BY z.datum, z.id
            """,
            (gewaehlt,),
        ).fetchall()
        timer_laeuft = conn.execute(
            f"SELECT 1 FROM zeiteintrag z WHERE z.kunde_id = ? AND z.rechnung_id IS NULL"
            f" AND {_LAEUFT} LIMIT 1",
            (gewaehlt,),
        ).fetchone() is not None
        if zeiten:
            satz = kunden.satz_cent(conn, gewaehlt)
            # Ohne Satz gibt es keine Vorschau, sondern den Hinweis: vier Mal
            # "0,00 €" sagt dem Erstnutzer nicht, was fehlt.
            satz_fehlt = satz <= 0 and any(
                z["abrechnungsmodus"] == "STUNDEN" and not z["satz_cent_snapshot"]
                for z in zeiten
            )
            # Die Regel des KUNDEN, sonst die des Betriebs -- dieselbe, die der
            # Entwurf gleich einfriert. Sonst zeigte die Vorschau eine andere
            # Summe als der Entwurf danach.
            regel = kunden.rundungsregel(conn, gewaehlt)
            summen = gruppierung.vorschau_alle_modi(
                [dict(z) for z in zeiten],
                standard_cent=satz,
                intervall_min=regel.intervall_min,
                modus_rundung=regel.modus,
            )

    # Was im Formular steht: die Vorbelegung, darueber das Abgewiesene.
    werte = {
        "datum": db.heute(), "modus": modus_default, "betreff": "",
        "einleitung": EINLEITUNG_VORSCHLAG, "schluss": "", "einzelnachweis": False,
        "leistungszeitraum": bool(betrieb["leistungszeitraum_default"]),
        # None heisst "alle": der Klickweg vom Dashboard waehlt alles vor.
        "zeit": None,
        # Entwurf ohne Zeiten: der laufende Monat als Leistungszeitraum, denn
        # der haeufigste Fall ist die Provision zum Monatsende.
        "ohne_zeiten": False,
        "leistung_von": db.heute()[:8] + "01", "leistung_bis": db.heute(),
    }
    werte.update(eingaben or {})
    if werte["modus"] not in gruppierung.MODI:
        werte["modus"] = modus_default

    return VORLAGEN.TemplateResponse(
        request,
        "rechnung_neu.html",
        {
            "eingabe": werte,
            "kunden": kundenliste,
            "gewaehlt": gewaehlt,
            "kunde_name": kunde_zeile["name"] if kunde_zeile is not None else None,
            "zeiten": zeiten,
            "summen": summen,
            "regel": regel,
            "satz_fehlt": satz_fehlt,
            "timer_laeuft": timer_laeuft,
            "modi": gruppierung.MODI,
            "modus_text": gruppierung.MODUS_TEXT,
            "modus_default": modus_default,
            "heute": db.heute(),
            # Das Rechnungsdatum steht hier noch nicht fest; die Vorschau gilt
            # fuer das laufende Jahr, und die Vorlage sagt das dazu. Die
            # Entwurfsseite zeigt dann die Nummer zum Jahr des Datums.
            "naechste_nummer": _nummernvorschau(conn, db.heute()),
            "leistungszeitraum_default": betrieb["leistungszeitraum_default"],
            "fehler": fehler,
        },
        status_code=status_code,
    )


@router.post("/rechnung/neu")
def anlegen(
    request: Request,
    # Alles mit Vorbelegung statt Form(...): sonst antwortet FastAPI mit seiner
    # generischen Eingabefehlerseite, bevor die eigene Pruefung die deutsche
    # Begruendung liefert -- und die Eingaben sind weg.
    kunde_id: str = Form(""),
    datum: str = Form(""),
    # Vorbelegt wird in der Vorlage, aus der Kundeneinstellung. Hier steht nur
    # der Rueckfall, falls das Feld gar nicht ankommt.
    modus: str = Form(gruppierung.PRO_TITEL),
    betreff: str = Form(""),
    einleitung: str = Form(""),
    schluss: str = Form(""),
    einzelnachweis: str = Form(""),
    leistungszeitraum: str = Form(""),
    zeit: list[str] = Form(default=[]),
    # Entwurf ohne Zeiten (freie Positionen): statt der Zeiten nennt das
    # Formular den Leistungszeitraum.
    ohne_zeiten: str = Form(""),
    leistung_von: str = Form(""),
    leistung_bis: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    kunde = _kunden_id(kunde_id)
    # Kennungen ueber eingabe.kennung: "²" oder eine Zahl ab 2**63 aus einem
    # veraenderten Formular endeten sonst als Serverfehler.
    zeit_ids = [k for k in (eingabe.kennung(z) for z in zeit) if k is not None]
    eingaben = {
        "datum": datum.strip(), "modus": modus, "betreff": betreff,
        "einleitung": einleitung, "schluss": schluss,
        "einzelnachweis": einzelnachweis == "1",
        "leistungszeitraum": leistungszeitraum == "1",
        "zeit": set(zeit_ids),
        "ohne_zeiten": ohne_zeiten == "1",
        "leistung_von": leistung_von.strip(), "leistung_bis": leistung_bis.strip(),
    }

    def abweisen(text: str):
        # Dieselbe Seite mit den Eingaben und Status 422, statt einer
        # Umleitung auf den leeren Assistenten.
        return _assistent_seite(
            request, conn, str(kunde) if kunde is not None else None,
            fehler=text, eingaben=eingaben, status_code=422,
        )

    if kunde is None:
        return abweisen("Der Kunde existiert nicht.")
    zeitraum = None
    if ohne_zeiten == "1":
        # Freie Positionen statt Zeiten: der Zeitraum muss stimmen, denn er
        # steht auf dem Papier und bestimmt die Betreffzeile.
        zeit_ids = []
        for feld, wert in (("Der Beginn", leistung_von), ("Das Ende", leistung_bis)):
            if not wert.strip():
                return abweisen(f"{feld} des Leistungszeitraums fehlt.")
            problem = validierung.datumsproblem(wert.strip())
            if problem:
                return abweisen(problem)
        zeitraum = (date.fromisoformat(leistung_von.strip()).isoformat(),
                    date.fromisoformat(leistung_bis.strip()).isoformat())
        if zeitraum[0] > zeitraum[1]:
            return abweisen("Der Leistungszeitraum endet vor seinem Beginn.")
    elif not zeit_ids:
        return abweisen("Es ist keine Zeit ausgewählt.")
    datum = datum.strip()
    if not datum:
        return abweisen("Das Rechnungsdatum fehlt.")
    # Nicht nur "ist ein Datum", sondern "ist ein brauchbares": 0026-03-10
    # (Jahr "26" im Datumsfeld getippt) und 9999-12-31 bestehen
    # date.fromisoformat() -- das eine legte einen Nummernkreis "26" an, das
    # andere lief bei der Faelligkeit in einen OverflowError und damit in die
    # 500-Seite.
    problem = validierung.datumsproblem(datum)
    if problem:
        return abweisen(problem)
    # Gespeichert wird das gelesene Datum, nie der Rohtext ("20261018").
    datum = date.fromisoformat(datum).isoformat()
    if modus not in gruppierung.MODI:
        return abweisen("Unbekannte Gruppierung.")
    try:
        rechnung_id = fs.entwurf_anlegen(
            conn, kunde, zeit_ids, modus=modus, datum=datum,
            # Leer heisst "nimm den Vorschlag", nicht "lass die Zeile weg":
            # das Formular zeigt den Vorschlag gar nicht an, bevor der Zeitraum
            # aus den gewaehlten Zeiten feststeht.
            betreff=betreff.strip() or None,
            einleitung=einleitung.strip() or None, schluss=schluss.strip() or None,
            einzelnachweis=einzelnachweis == "1",
            leistungszeitraum_zeigen=leistungszeitraum == "1",
            zeitraum=zeitraum,
        )
    except fs.FestschreibenFehler as fehler:
        return abweisen(str(fehler))
    except sqlite3.IntegrityError:
        # Die Datenbank hat eine Position abgewiesen -- etwa eine Pauschale
        # ueber 0 €. Die Transaktion ist zurueckgerollt, die Zeiten sind frei;
        # der Nutzer braucht den Grund, nicht einen Serverfehler -- und auch
        # nicht den englischen Text der Datenbank.
        return abweisen(
            "Der Entwurf konnte nicht angelegt werden: eine Position hätte keinen "
            "Betrag. Bitte Stundensatz und Pauschalbeträge prüfen."
        )
    if zeitraum is not None:
        # Der Entwurf ist leer; die Positionen kommen auf der Entwurfsseite.
        return RedirectResponse(f"/rechnung/{rechnung_id}#positionen", status_code=303)

    # Nur ein Teil der gewaehlten Zeiten war noch frei? Dann steht es auf der
    # Entwurfsseite, statt dass der Nutzer eine Rechnung ueber weniger
    # festschreibt, als er angehakt hat.
    uebernommen = conn.execute(
        "SELECT COUNT(*) FROM zeiteintrag WHERE rechnung_id = ?", (rechnung_id,)
    ).fetchone()[0]
    fehlend = len(set(zeit_ids)) - int(uebernommen)
    if fehlend > 0:
        return _hinweis(
            f"/rechnung/{rechnung_id}",
            "1 gewählte Zeit war nicht mehr frei und wurde nicht übernommen."
            if fehlend == 1
            else f"{fehlend} gewählte Zeiten waren nicht mehr frei und wurden nicht übernommen.",
        )
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


# -------------------------------------------------------------------- Ansicht

def _nummernvorschau(conn: sqlite3.Connection, datum: str) -> str:
    """Die Nummer, die ein Dokument mit diesem Datum bekaeme -- Jahr und Monat
    aus dem Datum, damit {JJJJ} und {MM} stimmen."""
    jahr, monat = nummernkreis.jahr_monat(datum)
    return nummernkreis.vorschau(conn, jahr, monat=monat)


@router.get("/rechnung/{rechnung_id}", response_class=HTMLResponse)
def ansicht(
    rechnung_id: int, request: Request, conn: sqlite3.Connection = Depends(hole_conn)
):
    # _vorhanden zuerst: es faengt auch die Zahl ab, die SQLite nicht binden
    # kann (20 Stellen in der Adresszeile) -- sonst OverflowError und 500.
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    return _rechnungsseite(request, conn, rechnung_id,
                           fehler=request.query_params.get("fehler"))


def _rechnungsseite(
    request: Request,
    conn: sqlite3.Connection,
    rechnung_id: int,
    *,
    fehler: str | None = None,
    kopf: dict | None = None,
    dublette: dict | None = None,
    position: dict | None = None,
    status_code: int = 200,
):
    """Die Rechnungsseite -- auf Wunsch mit den abgewiesenen Kopf-Eingaben.

    ``position``: die Eingaben eines abgewiesenen "Freie Position"-Formulars,
    damit sie nach der Meldung im Formular stehen bleiben.

    ``dublette``: eine Zahlung, die vermutlich schon als Einnahme ohne
    Rechnung (etwa aus WISO) im Werkzeug steht -- mit den Eingaben und den
    Treffern, fuer die Rueckfrage.

    ``kopf`` kommt aus einem abgewiesenen "Kopf und Texte"-Formular. Vorher
    leitete die Abweisung um, und Betreff, Einleitung, Schlusstext und Haken
    desselben Absendens waren weg -- wie beim Anlegen, nur eine Seite spaeter.
    """
    abfrage = """
        SELECT r.*, v.chip, v.zahlstatus, v.ueberfaellig, v.bezahlt_cent, v.offen_cent,
               v.tage_ueber_faellig, v.letzte_zahlung
          FROM rechnung r JOIN v_rechnung_status v ON v.id = r.id
         WHERE r.id = ?
    """
    rechnung = conn.execute(abfrage, (rechnung_id,)).fetchone()
    if rechnung is None:
        return _meldung("/rechnungen", NICHT_VORHANDEN)

    if rechnung["status"] == "ENTWURF":
        # Ein nachtraeglich korrigierter Leistungsort des Kunden muss den
        # Entwurf erreichen, und zwar HIER, wo der Nutzer hinschaut: sonst
        # steht nach der Korrektur am Kunden weiter der rote Befund "Bitte den
        # Leistungsort am Kunden berichtigen" -- obwohl genau das gerade
        # geschehen ist -- und der Weg heraus war, zufaellig unter "Kopf und
        # Texte" auf Speichern zu klicken.
        conn.execute("BEGIN IMMEDIATE")
        try:
            geaendert = fs.leistungsort_nachziehen(conn, rechnung_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if geaendert:
            rechnung = conn.execute(abfrage, (rechnung_id,)).fetchone()

    kunde = conn.execute(
        "SELECT * FROM kunde WHERE id = ?", (rechnung["kunde_id"],)
    ).fetchone()
    # Was auf dem Papier steht: beim Entwurf der Stamm, danach der
    # Schnappschuss. Die Kopfzeile zeigte bisher immer den Stamm -- nach einer
    # Umbenennung des Kunden also einen anderen Empfaenger als das PDF.
    empfaenger = fs.empfaenger_daten(conn, rechnung)
    befunde = validierung.pruefe(conn, rechnung_id) if rechnung["status"] == "ENTWURF" else []
    # Die Regel, mit der der Entwurf rechnet, und die, die heute am Kunden
    # gilt. Weichen sie ab, sagt die Seite es und bietet die Uebernahme an --
    # umgerechnet wird nie still.
    regel_entwurf = regel_kunde = None
    if rechnung["status"] == "ENTWURF":
        regel_entwurf = fs.regel_des_entwurfs(conn, rechnung)
        regel_kunde = kunden.rundungsregel(conn, rechnung["kunde_id"])
    naechste_nummer = None
    if rechnung["status"] == "ENTWURF" and validierung._datum(rechnung["datum"]):
        # Aus dem Jahr und Monat des RECHNUNGSDATUMS, nicht von heute: die
        # Dezemberrechnung, im Januar nachgetragen, bekommt eine Nummer des
        # Vorjahres, und {MM} zeigt ihren Monat.
        naechste_nummer = _nummernvorschau(conn, rechnung["datum"])
    return VORLAGEN.TemplateResponse(
        request,
        "rechnung.html",
        {
            "r": rechnung,
            "kunde": kunde,
            "empfaenger": empfaenger,
            "dokumenttitel": fs.DOKUMENTTITEL.get(rechnung["typ"], "Rechnung"),
            "naechste_nummer": naechste_nummer,
            # Warum es keine PDF-Vorschau geben kann (WeasyPrint oder Pango
            # fehlt) -- dann steht der Grund statt eines leeren Rahmens da.
            "vorschau_hindernis": (
                pdf.renderer_fehlt() if rechnung["status"] == "ENTWURF" else None
            ),
            # Ob die Datei noch auf der Platte liegt, weiss nur das
            # Dateisystem. Mit pdf_pfad allein zeigte die Seite nach einem
            # Loeschen "PDF oeffnen" ins Leere -- und keinen Weg zurueck.
            "pdf_fehlt": bool(rechnung["pdf_pfad"]) and not fs.pdf_vorhanden(
                rechnung["pdf_pfad"], _datenordner(request)
            ),
            "positionen": conn.execute(
                "SELECT * FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr",
                (rechnung_id,),
            ).fetchall(),
            "zeiten": conn.execute(
                "SELECT * FROM zeiteintrag WHERE rechnung_id = ? ORDER BY datum, id",
                (rechnung_id,),
            ).fetchall(),
            "zahlungen": conn.execute(
                "SELECT * FROM zahlung WHERE rechnung_id = ? ORDER BY datum", (rechnung_id,)
            ).fetchall(),
            "befunde": validierung.nach_stufe(befunde),
            "regel_entwurf": regel_entwurf,
            "regel_kunde": regel_kunde,
            "blockiert": validierung.blockiert(befunde),
            "modi": gruppierung.MODI,
            "modus_text": gruppierung.MODUS_TEXT,
            "storniert_von": conn.execute(
                "SELECT id, nummer FROM rechnung WHERE storno_von_id = ?", (rechnung_id,)
            ).fetchone(),
            # Mit dem Zahlungsstand des Originals: ist es bezahlt, muss die
            # Storno-Seite auf die ausstehende Rueckzahlung hinweisen.
            "original": conn.execute(
                "SELECT r.id, r.nummer, v.bezahlt_cent FROM rechnung r"
                " JOIN v_rechnung_status v ON v.id = r.id WHERE r.id = ?",
                (rechnung["storno_von_id"],),
            ).fetchone() if rechnung["storno_von_id"] else None,
            "heute": db.heute(),
            "fehler": fehler,
            "hinweis": request.query_params.get("hinweis"),
            "kopf": kopf,
            "dublette": dublette,
            "position_eingabe": position,
            "einheiten": fs.FREIE_EINHEITEN_TEXT,
            "beschreibung": einnahmen.beschreibung,
            # Rechnungsablauf: Kette (ersetzt/ersetzt durch), Zuruecknehmen,
            # zurueckgenommene Zahlungen, eingefrorener Satz im Entwurf.
            **ablauf.seitendaten(conn, rechnung),
        },
        status_code=status_code,
    )


def _datenordner(request: Request) -> Path:
    pfad = request.app.state.datenpfad
    return Path(pfad).parent if pfad else db.standard_datenordner()


@router.get("/rechnung/{rechnung_id}/pdf")
def pdf_ausliefern(
    rechnung_id: int, request: Request, download: str = "",
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Die PDF der Rechnung -- im Browser angezeigt, mit ``?download=1`` geladen.

    Der Knopf heisst "PDF öffnen"; mit ``Content-Disposition: attachment``
    startete der Browser einen Download statt die Datei zu zeigen.
    """
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    zeile = conn.execute(
        "SELECT nummer, pdf_pfad FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()
    if zeile is None or not zeile["pdf_pfad"]:
        return _meldung(f"/rechnung/{rechnung_id}", "Für diese Rechnung gibt es keine Datei.")
    # Gespeichert ist der Pfad relativ zum Datenordner (app/pfade.py).
    pfad = pfade.absolut(_datenordner(request), zeile["pdf_pfad"])
    if not pfad.exists():
        return _meldung(f"/rechnung/{rechnung_id}", "Für diese Rechnung gibt es keine Datei.")
    return FileResponse(pfad, media_type="application/pdf",
                        headers=dateikopf(pfad.name, herunterladen=download == "1"))


# ------------------------------------------------------------ PDF-Vorschau

@router.get("/rechnung/{rechnung_id}/vorschau.pdf")
def pdf_vorschau(rechnung_id: int, conn: sqlite3.Connection = Depends(hole_conn)):
    """Das PDF eines Entwurfs, so wie es beim Festschreiben entstuende.

    Vorher sah man das PDF erst NACH dem Festschreiben -- wenn Nummer,
    Journal und Archivexemplar schon unumkehrbar waren. Die Vorschau
    entsteht mit derselben Vorlage und demselben Renderer, traegt aber das
    Wasserzeichen "ENTWURF – keine Rechnung" und keine Nummer.

    Rein lesend: kein BEGIN, kein Journal, kein Nummernkreis, keine Datei im
    Datenordner (``fs.vorschau_daten``, ``pdf.rendere_vorschau``). Ein
    festgeschriebenes Dokument hat sein PDF -- dorthin geht es weiter, ein
    neues Rendering gaebe es dafuer nie.
    """
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    status = conn.execute(
        "SELECT status FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()["status"]
    if status != "ENTWURF":
        return RedirectResponse(f"/rechnung/{rechnung_id}/pdf", status_code=303)
    try:
        daten = fs.vorschau_daten(conn, rechnung_id)
        inhalt = pdf.rendere_vorschau(daten)
    except fs.FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    except (pdf.PdfFehler, OSError, TimeoutError) as fehler:
        # Steht im iframe der Entwurfsseite: ein kurzer Satz statt einer
        # Fehlerseite. 503, denn am Entwurf liegt es nicht.
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Vorschau nicht möglich</title>"
            "<p style=\"font-family:sans-serif;margin:16px\">Die Vorschau konnte nicht "
            f"erzeugt werden. {escape(pdf.verstaendlich(str(fehler)))}</p>",
            status_code=503,
        )
    name = daten["empfaenger"].get("name") or "Kunde"
    return Response(
        inhalt, media_type="application/pdf",
        headers={
            **dateikopf(f"ENTWURF {name}.pdf", herunterladen=False),
            # Nach "Kopf speichern" oder einer neuen Gruppierung soll der
            # Browser die Vorschau neu holen, nicht die alte zeigen.
            "cache-control": "no-store",
        },
    )


# ------------------------------------------------------------ Entwurf aendern

def _vorhanden(conn: sqlite3.Connection, rechnung_id: int) -> bool:
    """Gibt es diese Rechnung? Der Schutz jeder Route mit Rechnungsnummer.

    Der Pfadparameter ist ``int``, und eine 20-stellige Zahl besteht FastAPIs
    Pruefung -- SQLite kann sie aber nicht binden (OverflowError, Python-int
    jenseits von 64 Bit). Nur ueber eine von Hand geaenderte Adresse
    erreichbar, aber dann ein Serverfehler statt der Meldung.
    """
    try:
        return conn.execute(
            "SELECT 1 FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone() is not None
    except OverflowError:
        return False


@router.post("/rechnung/{rechnung_id}/gruppierung")
def gruppierung_wechseln(
    rechnung_id: int, modus: str = Form(""), conn: sqlite3.Connection = Depends(hole_conn)
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    if modus not in gruppierung.MODI:
        return _meldung(f"/rechnung/{rechnung_id}", "Unbekannte Gruppierung.")
    try:
        fs.gruppierung_wechseln(conn, rechnung_id, modus)
    except fs.FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


@router.post("/rechnung/{rechnung_id}/rundung")
def rundung_uebernehmen(rechnung_id: int, conn: sqlite3.Connection = Depends(hole_conn)):
    """Rechnet einen Entwurf mit der heutigen Rundungsregel des Kunden neu -- auf Klick.

    Ein Entwurf behaelt die Regel vom Anlegen; eine Aenderung am Kunden oder
    in den Einstellungen rechnet ihn nicht still um. Will der Nutzer die
    neue Regel, ist das hier der Weg (mit Journal und neuer Version).
    """
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        geaendert = fs.rundung_uebernehmen(conn, rechnung_id)
    except fs.FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    if not geaendert:
        return _hinweis(f"/rechnung/{rechnung_id}",
                        "Der Entwurf rechnet bereits mit der Regel des Kunden.")
    return _hinweis(f"/rechnung/{rechnung_id}",
                    "Die Rundungsregel des Kunden ist übernommen, die Positionen sind neu "
                    "berechnet.")


@router.post("/rechnung/{rechnung_id}/kopf")
def kopf_speichern(
    rechnung_id: int,
    request: Request,
    datum: str = Form(""),
    betreff: str = Form(""),
    einleitung: str = Form(""),
    schluss: str = Form(""),
    einzelnachweis: str = Form(""),
    leistungszeitraum: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    rechnung = conn.execute(
        "SELECT status, zahlungsziel_tage, kunde_id, leistung_von, leistung_bis"
        " FROM rechnung WHERE id = ?",
        (rechnung_id,),
    ).fetchone()
    if rechnung is None:
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    if rechnung["status"] != "ENTWURF":
        return _meldung(f"/rechnung/{rechnung_id}", "Nur ein Entwurf kann geändert werden.")

    eingaben = {
        "datum": datum, "betreff": betreff, "einleitung": einleitung, "schluss": schluss,
        "einzelnachweis": einzelnachweis == "1",
        "leistungszeitraum": leistungszeitraum == "1",
    }

    def abgewiesen(text: str):
        return _rechnungsseite(request, conn, rechnung_id, fehler=text,
                               kopf=eingaben, status_code=422)

    datum = datum.strip()
    if not datum:
        return abgewiesen("Das Rechnungsdatum fehlt.")
    problem = validierung.datumsproblem(datum)
    if problem:
        return abgewiesen(problem)
    # Gespeichert wird das GELESENE Datum, nicht der Rohtext: fromisoformat()
    # nimmt seit Python 3.11 auch "20261018" an, SQLites strftime() aber
    # nicht -- die Rechnung fehlte dann in der Jahresliste.
    datum = date.fromisoformat(datum).isoformat()
    try:
        faellig = (
            date.fromisoformat(datum) + timedelta(days=rechnung["zahlungsziel_tage"] or 14)
        ).isoformat()
    except (ValueError, OverflowError):
        # OverflowError: ein Datum am Ende des Kalenders, hinter dem es keine
        # Faelligkeit mehr gibt. Das Fenster oben faengt es; hier das Netz.
        return abgewiesen(f"„{datum}“ ist kein Datum.")

    # Leer heisst auch hier "nimm den Vorschlag" -- wie beim Anlegen, wo das
    # Formular es so verspricht. Wer die Zeile loescht, um zum Vorschlag
    # zurueckzukehren, bekam vorher eine Rechnung ohne Betreffzeile.
    betreff = betreff.strip()
    if not betreff and rechnung["leistung_von"] and rechnung["leistung_bis"]:
        kunde = conn.execute(
            "SELECT * FROM kunde WHERE id = ?", (rechnung["kunde_id"],)
        ).fetchone()
        if kunde is not None:
            betreff = fs.vorschlag_betreff(
                kunde, rechnung["leistung_von"], rechnung["leistung_bis"]
            )

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Ein nachtraeglich korrigierter Leistungsort des Kunden muss in den
        # Entwurf. Sonst traegt die Rechnung den Stand von vor der Korrektur --
        # mit dem falschen Steuerhinweis.
        fs.leistungsort_nachziehen(conn, rechnung_id)
        conn.execute(
            "UPDATE rechnung SET datum = ?, faellig_am = ?, betreff = ?,"
            " einleitung = ?, schluss = ?,"
            " einzelnachweis = ?, leistungszeitraum_zeigen = ?, version = version + 1"
            " WHERE id = ?",
            (
                datum, faellig, betreff or None,
                einleitung.strip() or None, schluss.strip() or None,
                1 if einzelnachweis == "1" else 0,
                1 if leistungszeitraum == "1" else 0, rechnung_id,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


@router.post("/rechnung/{rechnung_id}/position")
def position_anlegen(
    rechnung_id: int,
    request: Request,
    bezeichnung: str = Form(""),
    untertext: str = Form(""),
    menge: str = Form("1"),
    einheit: str = Form("C62"),
    einzelpreis: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Eine freie Position (ohne Zeiteintrag) an den Entwurf haengen.

    Betrag = Menge x Einzelpreis; die Logik rechnet und prueft
    (app/logik/festschreiben.py, freie_position_anlegen). Abgewiesen wird
    auf derselben Seite mit den Eingaben, nicht per Umleitung.
    """
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    eingaben = {"bezeichnung": bezeichnung, "untertext": untertext, "menge": menge,
                "einheit": einheit, "einzelpreis": einzelpreis}

    def abgewiesen(text: str):
        return _rechnungsseite(request, conn, rechnung_id, fehler=text,
                               position=eingaben, status_code=422)

    try:
        menge_tausendstel = fs.lies_menge(menge)
    except fs.FestschreibenFehler as fehler:
        return abgewiesen(str(fehler))
    try:
        preis = kunden.euro_nach_cent(einzelpreis)
    except kunden.BetragZuGross:
        return abgewiesen(f"„{einzelpreis.strip()}“ ist als Einzelpreis unplausibel groß.")
    except (ArithmeticError, ValueError):
        return abgewiesen(f"„{einzelpreis.strip()}“ ist kein Betrag. Beispiel: 250,00")
    if preis is None or preis <= 0:
        return abgewiesen("Der Einzelpreis fehlt oder ist 0. Beispiel: 250,00")
    try:
        fs.freie_position_anlegen(
            conn, rechnung_id, bezeichnung=bezeichnung, untertext=untertext,
            menge_tausendstel=menge_tausendstel, einheit=einheit, einzelpreis_cent=preis,
        )
    except fs.FestschreibenFehler as fehler:
        return abgewiesen(str(fehler))
    return RedirectResponse(f"/rechnung/{rechnung_id}#positionen", status_code=303)


@router.post("/rechnung/{rechnung_id}/position/{position_id}/loeschen")
def position_loeschen(
    rechnung_id: int,
    position_id: int,
    request: Request,
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        fs.freie_position_loeschen(conn, rechnung_id, position_id)
    except fs.FestschreibenFehler as fehler:
        return _rechnungsseite(request, conn, rechnung_id, fehler=str(fehler),
                               status_code=422)
    return RedirectResponse(f"/rechnung/{rechnung_id}#positionen", status_code=303)


@router.post("/rechnung/{rechnung_id}/verwerfen")
def verwerfen(
    rechnung_id: int,
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Entwurf loeschen -- von der Entwurfsseite oder aus der Liste."""
    ziel = _rueckweg(zurueck) or "/rechnungen"
    if not _vorhanden(conn, rechnung_id):
        return _meldung(ziel, NICHT_VORHANDEN)
    zeiten = conn.execute(
        "SELECT COUNT(*) FROM zeiteintrag WHERE rechnung_id = ?", (rechnung_id,)
    ).fetchone()[0]
    try:
        fs.entwurf_loeschen(conn, rechnung_id)
    except fs.FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    frei = ("" if not zeiten else " 1 Zeit ist wieder frei." if zeiten == 1
            else f" {zeiten} Zeiten sind wieder frei.")
    return _hinweis(ziel, f"Entwurf gelöscht.{frei} Eine Nummer war noch nicht vergeben.")


# ------------------------------------------------------------- Festschreiben

@router.post("/rechnung/{rechnung_id}/festschreiben")
def festschreiben(
    rechnung_id: int,
    request: Request,
    # Als Text, nicht als int: fehlt das Feld oder ist es kaputt, soll die
    # deutsche Meldung kommen, nicht FastAPIs Eingabefehlerseite.
    version: str = Form(""),
    bestaetigt: list[str] = Form(default=[]),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    # Den Status VOR der Pruefung: auf einer festgeschriebenen Rechnung (alter
    # Tab, Zuruecktaste) entstehen GELB-Befunde, und die Meldung lautete
    # "3 Hinweise sind noch nicht abgehakt" -- auf einer Seite, die keine
    # Haekchen mehr hat.
    status = conn.execute(
        "SELECT status FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()["status"]
    if status != "ENTWURF":
        return _meldung(
            f"/rechnung/{rechnung_id}",
            "Die Rechnung ist bereits festgeschrieben. Bitte die Seite neu laden.",
        )
    # eingabe.kennung statt isdigit(): "²" bestand isdigit() und scheiterte
    # erst an int(), eine Zahl ab 2**63 erst an SQLite -- beides als 500.
    version_nr = eingabe.kennung(version)
    if version_nr is None:
        return _meldung(
            f"/rechnung/{rechnung_id}",
            "Das Formular war unvollständig — bitte die Seite neu laden und erneut "
            "festschreiben.",
        )
    befunde = validierung.pruefe(conn, rechnung_id)
    offen_gelb = [
        b.code for b in befunde if b.stufe == validierung.GELB and b.code not in bestaetigt
    ]
    if offen_gelb:
        return _meldung(
            f"/rechnung/{rechnung_id}",
            "1 Hinweis ist noch nicht abgehakt."
            if len(offen_gelb) == 1
            else f"{len(offen_gelb)} Hinweise sind noch nicht abgehakt.",
        )
    try:
        ergebnis = fs.festschreiben(
            conn, rechnung_id, version=version_nr, bestaetigte_warnungen=bestaetigt,
            # Derselbe Ordner, den die Auslieferung liest. Ohne diese Angabe
            # nahm das Rendering den Standardordner -- und zeigte die
            # Anwendung auf einen anderen Datenpfad, lag das PDF woanders als
            # der Link es suchte.
            belegordner=_datenordner(request),
        )
    except fs.FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))

    if ergebnis.pdf_fehler:
        return _meldung(
            f"/rechnung/{rechnung_id}",
            f"Die Rechnung {ergebnis.nummer} ist festgeschrieben, aber das PDF konnte nicht "
            f"erzeugt werden: {ergebnis.pdf_fehler}",
        )
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


@router.post("/rechnung/{rechnung_id}/pdf")
def pdf_nachziehen(
    rechnung_id: int, request: Request, conn: sqlite3.Connection = Depends(hole_conn)
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        ergebnis = fs.pdf_nachziehen(conn, rechnung_id, belegordner=_datenordner(request))
    except fs.FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    if ergebnis.pdf_fehler:
        return _meldung(f"/rechnung/{rechnung_id}", ergebnis.pdf_fehler)
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


@router.post("/rechnung/{rechnung_id}/storno")
def storno(
    rechnung_id: int,
    request: Request,
    # Default statt Form(...): sonst antwortet FastAPI mit seiner generischen
    # JSON-Meldung, bevor die eigene Pruefung die deutsche Begruendung liefert.
    grund: str = Form(""),
    zeiten_freigeben: str = Form("1"),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    try:
        ergebnis = fs.storniere(
            conn, rechnung_id, grund, zeiten_freigeben=zeiten_freigeben == "1",
            belegordner=_datenordner(request),
        )
    except fs.FestschreibenFehler as fehler:
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    if ergebnis.pdf_fehler:
        # Wie beim Festschreiben: die Korrektur gilt, nur die Datei fehlt --
        # und der Grund soll hier stehen, nicht erst der Hinweis "hat kein
        # PDF" auf der naechsten Seite.
        return _meldung(
            f"/rechnung/{ergebnis.rechnung_id}",
            f"Die Korrektur {ergebnis.nummer} ist festgeschrieben, aber das PDF konnte "
            f"nicht erzeugt werden: {ergebnis.pdf_fehler}",
        )
    return RedirectResponse(f"/rechnung/{ergebnis.rechnung_id}", status_code=303)


# ----------------------------------------------------------- Zahlung, Versand

def _zahldatum(
    rechnung_id: int, datum: str, *, ziel: str | None = None
) -> str | RedirectResponse:
    """Das Datum einer Zahlung, geprueft -- oder die Umleitung mit der Meldung.

    Der Wert wandert in eine Datumsspalte, auf der EUeR und § 19-Zaehler
    rechnen. Was kein Datum ist, soll hier scheitern und nicht spaeter still
    in einem anderen Jahr landen.

    Zurueck kommt das Datum als JJJJ-MM-TT, nie der Rohtext:
    ``date.fromisoformat()`` liest auch "20261019", SQLites
    ``strftime('%Y', ...)`` aber nicht -- so gespeichert, fiel die Zahlung
    aus EUeR, § 19-Zaehler und Jahresliste, waehrend die Rechnung "bezahlt"
    zeigte. Gilt fuer Zahlung und Rueckzahlung, beide kommen hier durch.

    Die Pruefung selbst ist ``eingabe.datum``: Jahr ab 2000 (Jahr 0001 ist ein
    Tippfehler und landete in der EUeR eines Jahres, das niemand ansieht),
    nichts aus der Zukunft. Eine Zahlung in der Zukunft gibt es nicht: § 11
    EStG rechnet nach dem Zufluss, und der ist noch nicht passiert -- das
    sagt die Meldung dazu. Die Werte erscheinen in „…“, nicht als
    Python-repr.
    """
    try:
        return eingabe.datum(datum, feld="Zahlungsdatum")
    except ValueError as fehler:
        text = str(fehler)
        if "in der Zukunft" in text:
            text += (" Eine Zahlung wird erst erfasst, wenn das Geld eingegangen ist "
                     "(§ 11 EStG).")
        return _meldung(ziel or f"/rechnung/{rechnung_id}", text)


@router.post("/rechnung/{rechnung_id}/zahlung")
def zahlung_erfassen(
    rechnung_id: int,
    request: Request,
    datum: str = Form(""),
    betrag_euro: str = Form(""),
    art: str = Form("UEBERWEISUNG"),
    erwartet_bezahlt: str = Form(""),
    rueckerstattung: str = Form(""),
    zurueck: str = Form(""),
    dublette: str = Form(""),
    dublette_einnahme: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Zahlungseingang. Grundlage der EUeR und des § 19-Grenzwerts, nicht Komfort.

    Steht dieselbe Zahlung vermutlich schon als Einnahme ohne Rechnung im
    Werkzeug (gleicher Betrag, ±7 Tage, aehnlicher Zahler -- meist aus dem
    WISO-Import), wird nicht gebucht, sondern gefragt (409): "ersetzen"
    bucht die Zahlung und loescht die Einnahme mit Grund (ihre WISO-Buchung
    haengt danach an der Zahlung), "zusaetzlich" bucht wie bisher.

    ``erwartet_bezahlt`` ist der Stand, den die Seite beim Laden gezeigt hat.
    Stimmt er nicht mehr mit der Datenbank ueberein, war die Seite veraltet --
    der klassische Fall ist der Doppelklick auf "Zahlung erfassen": zwei
    Anfragen, beide mit dem offenen Rest von eben, und ohne diese Pruefung
    buchten beide ihn. Die zweite trifft dann auf einen anderen Stand und wird
    abgewiesen. Der Rest wird INNERHALB der Transaktion gelesen, nicht davor:
    nur dort ist er verbindlich.

    Zahlt der Kunde ZU VIEL, wird das angenommen -- der Zufluss ist da
    (§ 11 EStG). Die Rueckerstattung der Differenz ist dann eine NEGATIVE
    Zahlung auf derselben Rechnung, und zwar genau dann und hoechstens in
    Hoehe der Ueberzahlung. ``rueckerstattung`` ist das Kennzeichen des
    Formulars "Rueckerstattung erfassen": ein dort positiv getippter Betrag
    wird als Rueckfluss gelesen. Ohne Ueberzahlung ist ein negativer Betrag
    ein Fehler, keine Zahlung.

    ``zurueck`` kommt von der Schnellaktion "Bezahlt ..." in der
    Rechnungsliste und der Uebersicht: Meldung und Erfolg landen dann dort,
    wo geklickt wurde (nur Liste oder Uebersicht, siehe ``_rueckweg``).
    """
    # _vorhanden zuerst: eine Nummer, die SQLite nicht binden kann (20 Stellen
    # in der Adresszeile), scheiterte sonst erst in der Transaktion -- mit
    # OverflowError und 500 statt mit der Meldung.
    rueckweg = _rueckweg(zurueck)
    ziel = rueckweg or f"/rechnung/{rechnung_id}"
    if not _vorhanden(conn, rechnung_id):
        return _meldung(rueckweg or "/rechnungen", NICHT_VORHANDEN)
    datum = _zahldatum(rechnung_id, datum, ziel=ziel)
    if isinstance(datum, RedirectResponse):
        return datum
    try:
        cent = kunden.euro_nach_cent(betrag_euro)
    except (ArithmeticError, ValueError) as fehler:
        # Die Meldung kommt aus euro_nach_cent: „abc“ und „1e400“ sind kein
        # Betrag, eine Milliarde ist unplausibel gross -- nie ein
        # Serverfehler, nie ein repr.
        return _meldung(ziel, str(fehler))
    if cent == 0:
        return _meldung(ziel, "Ein Zahlbetrag von 0,00 € ist sinnlos.")
    if art not in ZAHLUNGSARTEN:
        return _meldung(ziel, "Unbekannte Zahlungsart.")
    if rueckerstattung == "1" and cent is not None:
        cent = -abs(cent)
    try:
        erwartet = int(erwartet_bezahlt) if erwartet_bezahlt.strip() else None
    except ValueError:
        erwartet = None

    conn.execute("BEGIN IMMEDIATE")
    try:
        stand = conn.execute(
            "SELECT nummer, typ, dokumentstatus, brutto_cent, bezahlt_cent, offen_cent"
            " FROM v_rechnung_status WHERE id = ?",
            (rechnung_id,),
        ).fetchone()
        if stand is None:
            conn.execute("ROLLBACK")
            return _meldung(rueckweg or "/rechnungen", "Die Rechnung existiert nicht.")
        # Vor dem INSERT pruefen, nicht die Datenbank entscheiden lassen: ihr
        # Text kaeme roh und in ASCII-Umschrift beim Nutzer an.
        if stand["dokumentstatus"] == "ENTWURF":
            conn.execute("ROLLBACK")
            return _meldung(
                ziel,
                "Ein Entwurf kann noch keine Zahlung erhalten — erst festschreiben.",
            )
        if erwartet is not None and erwartet != int(stand["bezahlt_cent"]):
            conn.execute("ROLLBACK")
            return _meldung(
                ziel,
                f"Die Seite war nicht mehr aktuell: inzwischen sind "
                f"{euro(stand['bezahlt_cent'])} € verbucht, offen sind "
                f"{euro(stand['offen_cent'])} €. Nichts wurde gebucht — bitte prüfen "
                "und dann erneut erfassen.",
            )
        if stand["typ"] == "STORNO":
            # Eine Rechnungskorrektur fordert nichts; sie hebt auf. Geld dazu
            # ist entweder ein Irrtum oder gehoert als Rueckzahlung ans
            # Original. Der Trigger liesse es durch (brutto != 0), und der
            # § 19-Zaehler stiege.
            conn.execute("ROLLBACK")
            return _meldung(ziel,
                            "Auf eine Rechnungskorrektur geht kein Geld ein.")
        if stand["dokumentstatus"] == "STORNIERT":
            conn.execute("ROLLBACK")
            return _meldung(
                ziel,
                "Auf eine stornierte Rechnung geht kein Geld mehr ein — nur eine "
                "Rückzahlung, und die hat ihr eigenes Formular.",
            )
        if not stand["brutto_cent"]:
            conn.execute("ROLLBACK")
            return _meldung(ziel,
                            "Auf ein Dokument ohne Betrag geht kein Geld ein.")
        offen = int(stand["offen_cent"])
        ueberzahlt = -offen if offen < 0 else 0
        if cent is None:
            # Leer heisst "der Rest": der offene Betrag -- oder, bei einer
            # Ueberzahlung, deren Rueckerstattung.
            cent = offen
        if cent == 0:
            conn.execute("ROLLBACK")
            return _meldung(ziel,
                            "Die Rechnung ist bereits vollständig bezahlt.")
        begruendung = None
        if cent < 0:
            if not ueberzahlt:
                conn.execute("ROLLBACK")
                return _meldung(
                    ziel,
                    "Ein negativer Zahlbetrag ist nur als Rückerstattung einer "
                    "Überzahlung möglich.",
                )
            if -cent > ueberzahlt:
                conn.execute("ROLLBACK")
                return _meldung(
                    ziel,
                    f"Zurückerstattet werden kann höchstens die Überzahlung: "
                    f"{euro(ueberzahlt)} €.",
                )
            begruendung = "Rückerstattung Überzahlung"
        ersetzt: int | None = None
        if cent > 0 and dublette not in ("ersetzen", "zusaetzlich"):
            zahler = conn.execute(
                "SELECT COALESCE(json_extract(r.kunde_snapshot_json, '$.name'), k.name) AS name"
                " FROM rechnung r JOIN kunde k ON k.id = r.kunde_id WHERE r.id = ?",
                (rechnung_id,)).fetchone()["name"] or ""
            schon = einnahmen.aehnliche(conn, betrag_cent=cent, zufluss_am=datum, zahler=zahler)
            if schon:
                conn.execute("ROLLBACK")
                return _rechnungsseite(
                    request, conn, rechnung_id, status_code=409,
                    dublette={"aehnliche": schon, "datum": datum, "betrag_euro": euro(cent),
                              "art": art, "erwartet_bezahlt": erwartet_bezahlt,
                              "zurueck": zurueck})
        if cent > 0 and dublette == "ersetzen":
            ersetzt = eingabe.kennung(dublette_einnahme)
            if ersetzt is None:
                conn.execute("ROLLBACK")
                return _meldung(ziel, "Welche Einnahme ersetzt werden soll, fehlt — bitte "
                                      "die Zahlung noch einmal erfassen.")
            gemeint = conn.execute("SELECT betrag_cent FROM v_einnahme_gueltig WHERE id = ?",
                                   (ersetzt,)).fetchone()
            if gemeint is None or int(gemeint["betrag_cent"]) != cent:
                conn.execute("ROLLBACK")
                return _meldung(ziel, f"Einnahme {ersetzt} passt nicht (mehr) zu dieser "
                                      "Zahlung — nichts gebucht. Bitte noch einmal erfassen.")
        zahlung_id = conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art) VALUES (?, ?, ?, ?)",
            (rechnung_id, datum, cent, art),
        ).lastrowid
        db.protokolliere(conn, "ZAHLUNG", "rechnung", rechnung_id,
                         wert_neu={"datum": datum, "cent": cent, "art": art},
                         begruendung=begruendung)
        if ersetzt is not None:
            wiso.einnahme_durch_zahlung_ersetzen(conn, ersetzt, zahlung_id, stand["nummer"])
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _meldung(ziel, _zahlung_abgewiesen(fehler))
    except wiso.ImportFehler as fehler:
        conn.execute("ROLLBACK")
        return _meldung(ziel, str(fehler))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if rueckweg:
        return _hinweis(rueckweg, f"Zahlung über {euro(cent)} € zu {stand['nummer']} "
                                  f"vom {validierung._datum_de(datum)} erfasst."
                        + (f" Einnahme {ersetzt} ist dafür gelöscht." if ersetzt else ""))
    if ersetzt is not None:
        return _hinweis(f"/rechnung/{rechnung_id}",
                        f"Zahlung erfasst. Einnahme {ersetzt} (dieselbe Zahlung, etwa aus "
                        "WISO) ist dafür gelöscht — sie zählt nur noch einmal.")
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


@router.post("/rechnung/{rechnung_id}/rueckzahlung")
def rueckzahlung_erfassen(
    rechnung_id: int,
    datum: str = Form(""),
    betrag_euro: str = Form(""),
    art: str = Form("UEBERWEISUNG"),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Geld zurueck nach einem Storno.

    Eine bezahlte Rechnung zu stornieren nimmt das Geld nicht zurueck: der
    Eingang steht weiter in der EUeR und im § 19-Gesamtumsatz, bis die
    Rueckzahlung erfasst ist -- und zwar im Jahr, in dem sie fliesst (§ 11
    EStG). Gebucht wird sie als NEGATIVE Zahlung auf dem Original, nicht auf
    dem Storno-Dokument: so bleibt die Summe ueber ``zahlung`` je Rechnung
    vorzeichenrichtig, und der Trigger ``zahlung_nur_festgeschrieben`` laesst
    auf einer stornierten Rechnung genau diese Richtung zu.

    Mehr als eingegangen ist, laesst sich nicht zurueckzahlen -- das waere
    kein Storno mehr, sondern eine Schenkung, und die gehoert nicht hierher.
    """
    if not _vorhanden(conn, rechnung_id):
        return _meldung("/rechnungen", NICHT_VORHANDEN)
    datum = _zahldatum(rechnung_id, datum)
    if isinstance(datum, RedirectResponse):
        return datum
    try:
        cent = kunden.euro_nach_cent(betrag_euro)
    except (ArithmeticError, ValueError) as fehler:
        # Die Meldung kommt aus euro_nach_cent: „abc“ und „1e400“ sind kein
        # Betrag, eine Milliarde ist unplausibel gross -- nie ein
        # Serverfehler, nie ein repr.
        return _meldung(f"/rechnung/{rechnung_id}", str(fehler))
    if art not in ZAHLUNGSARTEN:
        return _meldung(f"/rechnung/{rechnung_id}", "Unbekannte Zahlungsart.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        stand = conn.execute(
            "SELECT dokumentstatus, bezahlt_cent FROM v_rechnung_status WHERE id = ?",
            (rechnung_id,),
        ).fetchone()
        if stand is None:
            conn.execute("ROLLBACK")
            return _meldung("/rechnungen", "Die Rechnung existiert nicht.")
        if stand["dokumentstatus"] != "STORNIERT":
            conn.execute("ROLLBACK")
            return _meldung(f"/rechnung/{rechnung_id}",
                            "Eine Rückzahlung gibt es nur zu einer stornierten Rechnung.")
        eingegangen = int(stand["bezahlt_cent"])
        cent = eingegangen if cent is None else abs(cent)
        if eingegangen <= 0 or cent <= 0:
            conn.execute("ROLLBACK")
            return _meldung(f"/rechnung/{rechnung_id}", "Es ist nichts zurückzuzahlen.")
        if cent > eingegangen:
            conn.execute("ROLLBACK")
            return _meldung(
                f"/rechnung/{rechnung_id}",
                f"Zurückgezahlt werden kann höchstens, was eingegangen ist: "
                f"{euro(eingegangen)} €.",
            )
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art) VALUES (?, ?, ?, ?)",
            (rechnung_id, datum, -cent, art),
        )
        db.protokolliere(conn, "RUECKZAHLUNG", "rechnung", rechnung_id,
                         wert_neu={"datum": datum, "cent": -cent, "art": art})
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _meldung(f"/rechnung/{rechnung_id}", _zahlung_abgewiesen(fehler))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


@router.post("/rechnung/{rechnung_id}/versendet")
def versendet(
    rechnung_id: int,
    datum: str = Form(""),
    grund: str = Form(""),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Haelt den Versandzeitpunkt fest -- relevant fuer die Sechsmonatsfrist
    nach § 14 Abs. 2 Satz 2 UStG und spaeter fuer den Verzug.

    Vermerkt wird jedes FESTGESCHRIEBENE Dokument, auch eine
    Rechnungskorrektur: sie muss dem Kunden ebenso zugehen, und die Seite
    bietet dafuer eigens den Mail-Link an. Eine Einschraenkung auf
    typ = 'RECHNUNG' hatte das zwischenzeitlich verhindert -- mit der
    irrefuehrenden Meldung, das Dokument sei nicht festgeschrieben.
    Ausgeschlossen bleiben ENTWURF und STORNIERT.

    Das UPDATE war schon so eingeschraenkt -- aber das Journal bekam seinen Eintrag
    unabhaengig davon, ob eine Zeile getroffen wurde: ein Versandvermerk fuer
    eine stornierte Rechnung, einen Entwurf oder eine Nummer, die es nicht
    gibt, in einem Journal, das sich nicht korrigieren laesst (§ 146 Abs. 4
    AO). Jetzt entscheidet der rowcount, und ohne Treffer gibt es die
    Meldung.

    Und es schrieb JEDES Absenden einen Eintrag: ein Doppelklick ergab zwei
    gleiche Zeilen im Journal, und ein spaeteres anderes Datum ueberschrieb
    das erste still -- ohne ``wert_alt``, der alte Stand war nicht mehr
    feststellbar. Jetzt wird der bisherige Vermerk in der Transaktion
    gelesen: dasselbe Datum noch einmal aendert nichts und schreibt nichts
    (Erfolg, es steht ja so da); ein anderes Datum wird mit altem und neuem
    Wert protokolliert, und die Meldung nennt die Aenderung.

    Das Aendern eines gesetzten Vermerks braucht seit dem Rechnungsablauf
    (Version 22) einen Grund: der Vermerk entscheidet, ob eine Rechnung noch
    zurueckgenommen werden darf, und ein stilles Umdatieren waere genau die
    Luecke, durch die eine verschickte Rechnung wieder "unversendet" wuerde.
    Zuruecknehmen laesst er sich ueber die eigene Route
    (routen/rechnungsablauf.py), ebenfalls mit Grund.

    ``zurueck`` kommt von der Schnellaktion in Liste und Uebersicht.
    """
    rueckweg = _rueckweg(zurueck)
    ziel = rueckweg or f"/rechnung/{rechnung_id}"
    if not _vorhanden(conn, rechnung_id):
        return _meldung(rueckweg or "/rechnungen", NICHT_VORHANDEN)
    rechnung = conn.execute(
        "SELECT datum, nummer FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()
    if rechnung is None:
        return _meldung(rueckweg or "/rechnungen", NICHT_VORHANDEN)
    # Der Wert kommt aus einem Formular und wandert in eine Datumsspalte.
    # Steht dort etwas, das kein Datum ist, zeigt die Oberflaeche es nur noch
    # roh an -- und das Formular, mit dem man es korrigieren koennte, blendet
    # sie aus, sobald ein Versanddatum gesetzt ist. Also hier pruefen.
    # Dieselbe Pruefung wie bei Rechnungs- und Zahlungsdatum (eingabe.datum):
    # gelesen statt roh gespeichert, Jahr ab 2000, nicht in der Zukunft.
    try:
        datum = eingabe.datum(datum.strip() or db.heute(), feld="Versanddatum")
    except ValueError as fehler:
        return _meldung(ziel, str(fehler))
    # Versendet wird nach dem Ausstellen: aus dem Versanddatum rechnet sich
    # die Sechsmonatsfrist, und ein Datum sechs Jahre vor der Rechnung machte
    # sie sinnlos.
    rechnungsdatum = validierung._datum(rechnung["datum"])
    if rechnungsdatum and date.fromisoformat(datum) < rechnungsdatum:
        return _meldung(
            ziel,
            f"Das Versanddatum liegt vor dem Rechnungsdatum "
            f"({validierung._datum_de(rechnung['datum'])}).",
        )

    nur_festgeschrieben = (
        "Nur eine festgeschriebene Rechnung kann als versendet vermerkt werden."
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Der bisherige Vermerk INNERHALB der Transaktion: nur hier ist er
        # verbindlich. Vorher gelesen, saehen zwei gleichzeitige Klicks beide
        # "noch nicht versendet" und schrieben beide ins Journal.
        stand = conn.execute(
            "SELECT status, versendet_am FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        if stand is None or stand["status"] != "FESTGESCHRIEBEN":
            conn.execute("ROLLBACK")
            return _meldung(ziel, nur_festgeschrieben)
        bisher = stand["versendet_am"]
        if bisher == datum:
            # Doppelklick oder alter Tab: der Vermerk steht schon so da.
            # Nichts zu tun, und nichts gehoert ins Journal.
            conn.execute("ROLLBACK")
            return RedirectResponse(ziel, status_code=303)
        if bisher is not None and not grund.strip():
            conn.execute("ROLLBACK")
            return _meldung(ziel, "Zum Ändern des Versanddatums gehört ein Grund — der alte "
                                  "Vermerk und der Grund stehen im Protokoll.")
        # Die Zustandsbedingung (versendet_am IS bisher) ist das Netz: aendert
        # zwischen Lesen und Schreiben doch jemand den Vermerk, trifft das
        # UPDATE nichts, statt einen fremden Stand zu ueberschreiben.
        cur = conn.execute(
            "UPDATE rechnung SET versendet_am = ?"
            " WHERE id = ? AND status = 'FESTGESCHRIEBEN' AND versendet_am IS ?",
            (datum, rechnung_id, bisher),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return _meldung(ziel, nur_festgeschrieben)
        if bisher is None:
            db.protokolliere(conn, "VERSENDET", "rechnung", rechnung_id, wert_neu=datum)
        else:
            db.protokolliere(conn, "VERSENDET", "rechnung", rechnung_id,
                             feld="versendet_am", wert_alt=bisher, wert_neu=datum,
                             begruendung=grund.strip())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if bisher is not None:
        return _hinweis(
            ziel,
            f"Versanddatum von {validierung._datum_de(bisher)} auf "
            f"{validierung._datum_de(datum)} geändert.",
        )
    if rueckweg:
        return _hinweis(rueckweg, f"{rechnung['nummer']} als versendet vermerkt "
                                  f"({validierung._datum_de(datum)}).")
    return RedirectResponse(f"/rechnung/{rechnung_id}", status_code=303)


# --------------------------------------------------------------------- Liste

@router.get("/rechnungen", response_class=HTMLResponse)
def liste(
    request: Request,
    jahr: str | None = None,
    kunde: str | None = None,
    conn: sqlite3.Connection = Depends(hole_conn),
):
    bedingungen: list[str] = []
    werte: list[object] = []
    # filterzahl: "²" oder eine 19-stellige Zahl im Link heisst "nicht
    # einschraenken" -- vorher ein Serverfehler in der Lueckenpruefung.
    jahr_zahl = filterzahl(jahr or "")
    jahr = str(jahr_zahl) if jahr_zahl is not None else None
    if jahr:
        bedingungen.append("strftime('%Y', COALESCE(v.datum, r.erstellt_am)) = ?")
        werte.append(jahr)
    kunde_id = _kunden_id(kunde)
    if kunde_id:
        bedingungen.append("v.kunde_id = ?")
        werte.append(kunde_id)
    wo = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""

    # Der Name aus dem Schnappschuss, sobald es einen gibt: das ist der
    # Empfaenger, der auf dem Papier steht. Der Stamm gilt nur fuer den Entwurf
    # -- nach einer Umbenennung des Kunden zeigte die Liste sonst fuer jede
    # Altrechnung einen Namen, der nie darauf stand.
    #
    # Entwuerfe zuerst: sie sind das, woran gerade gearbeitet wird. Ohne
    # Nummer (nummer_jahr NULL) landeten sie hinter allen festgeschriebenen
    # Rechnungen, auch hinter denen alter Jahre -- der aktuelle Entwurf stand
    # am Listenende.
    datenordner = _datenordner(request)
    rechnungen = [
        {
            **dict(zeile),
            # Nur ein Link auf eine Datei, die es gibt.
            "pdf_da": fs.pdf_vorhanden(zeile["pdf_pfad"], datenordner),
        }
        for zeile in conn.execute(
            f"""
            SELECT v.*, r.erstellt_am, r.pdf_pfad,
                   COALESCE(json_extract(r.kunde_snapshot_json, '$.name'), k.name) AS kunde
              FROM v_rechnung_status v
              JOIN rechnung r ON r.id = v.id
              JOIN kunde k    ON k.id = v.kunde_id
              {wo}
             ORDER BY (r.status = 'ENTWURF') DESC, r.nummer_jahr DESC, r.id DESC
            """,
            werte,
        )
    ]

    jahre = [
        z["j"] for z in conn.execute(
            "SELECT DISTINCT strftime('%Y', COALESCE(datum, erstellt_am)) AS j"
            " FROM rechnung ORDER BY j DESC"
        )
    ]
    return VORLAGEN.TemplateResponse(
        request,
        "rechnungen.html",
        {
            "rechnungen": rechnungen,
            "jahre": jahre,
            # Kundennummer und Ort unterscheiden gleichnamige Kunden im Filter.
            "kunden": conn.execute(
                f"SELECT k.id, k.name, k.kundennummer, k.ort,"
                f" {kunden.doppelt_sql('k')} AS doppelt"
                f" FROM kunde k ORDER BY k.name COLLATE DEUTSCH, k.kundennummer"
            ).fetchall(),
            "filter": {"jahr": jahr, "kunde": kunde_id},
            # Mit Grund und Datum aus dem Journal, wo es einen Vermerk gibt
            # (Rechnungsablauf: zurueckgenommene Rechnungen).
            "luecken": ablauf.nummernluecken(conn, jahr_zahl) if jahr_zahl else [],
            "heute": db.heute(),
            # Fuer die Schnellaktionen: zurueck auf die Liste mit diesen Filtern.
            "zurueck": "/rechnungen" + (
                "?" + urlencode([(k, v) for k, v in (("jahr", jahr), ("kunde", kunde_id)) if v])
                if jahr or kunde_id else ""
            ),
            "zahlungsarten": ZAHLUNGSARTEN,
            "fehler": request.query_params.get("fehler"),
            "hinweis": request.query_params.get("hinweis"),
        },
    )

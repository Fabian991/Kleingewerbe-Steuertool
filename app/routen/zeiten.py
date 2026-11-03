"""Zeiterfassung.

Timer und manuelle Nacherfassung gleichwertig: der Timer deckt den Alltag, die
Nacherfassung den Fall "vergessen zu starten". Fehlt eines von beiden, wird am
Tool vorbeigearbeitet.

Rohzeiten werden NIE gerundet gespeichert. Gerundet wird erst je
Rechnungsposition -- nur so laesst sich eine Rundungsregel spaeter noch aendern,
ohne die Vergangenheit zu verfaelschen.

Jede Aktion antwortet mit einem Redirect auf die Liste und einer Meldung in der
URL -- nie mit einem HTTP 500. Wer vor einer JSON-Fehlerseite steht, haelt das
Werkzeug fuer kaputt und arbeitet daran vorbei. Ausnahme sind die Formulare mit
freiem Text (Erfassen, Timer starten, die Beschreibung im Timer-Banner, "Text
ändern"): eine Abweisung zeigt die Liste mit Status 422 und den Eingaben des
Nutzers neu, damit nichts neu getippt werden muss.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.logik import eingabe as eingabepruefung
from app.logik import festschreiben as fs
from app.logik import gruppierung
from app.logik import kunden as kundenlogik
from app.logik import leistungen as katalog
from app.logik import zeiten as logik
from app.web import VORLAGEN, hole_conn

router = APIRouter(prefix="/zeiten")

MODI = ("STUNDEN", "PAUSCHAL", "KULANZ", "INTERN")

#: So viele Eintraege zeigt die Liste hoechstens. Die Summe im Kopf zaehlt
#: trotzdem ALLE gefilterten Eintraege -- sonst ist sie bei vielen Eintraegen
#: stillschweigend zu klein.
LISTENGRENZE = 400

#: Query-Parameter, die eine Meldung tragen. Sie gelten fuer genau eine Seite
#: und duerfen beim Ruecksprung nicht mitgeschleppt werden.
MELDUNGSPARAMETER = ("fehler", "hinweis", "erfolg", "entwurf_leer")

PAUSCHAL_MELDUNG = "Pauschalen werden nicht über dieses Formular erfasst."
TIMER_LAEUFT_MELDUNG = "Der Timer läuft noch — erst stoppen."
KEIN_EINTRAG = "Diesen Zeiteintrag gibt es nicht mehr."
GEAENDERT_MELDUNG = "Der Eintrag wurde inzwischen geändert — die Liste ist neu geladen."

#: Die Filter der Liste. Nur sie wandern in den Ruecksprung.
FILTERPARAMETER = ("kunde", "status", "von", "bis")


def _sicheres_ziel(ziel: str | None) -> str | None:
    """Das Rueckkehrziel aus dem Formular -- nur ein relativer Pfad auf die Liste.

    Das versteckte Feld ``zurueck`` traegt die Filter der Liste, aus der das
    Formular abgeschickt wurde. Es kommt vom Browser und ist damit Eingabe:
    eine fremde Adresse ("https://...", "//host", "/\\host") wird verworfen,
    sonst liesse sich die Umleitung auf eine fremde Seite lenken.
    """
    text = (ziel or "").strip()
    if not text.startswith("/zeiten") or "\\" in text:
        return None
    teile = urlsplit(text)
    if teile.scheme or teile.netloc or teile.path != "/zeiten":
        return None
    return text


def _filter_query(request: Request, ziel: str | None = None) -> list[tuple[str, str]]:
    """Die Filter der Liste, von der die Anfrage kam -- ohne alte Meldungen.

    Erste Quelle ist das versteckte Feld ``zurueck``. Nach einer Abweisung
    (422) steht die Liste unter der POST-Adresse /zeiten ohne Query; der
    Referer der Korrektur hatte deshalb keine Filter mehr, und wer nach einem
    Kunden gefiltert hatte, landete nach dem Speichern in der ungefilterten
    Liste. Fehlt das Feld, gilt der Referer -- aber nur, wenn er auf DIESE
    Anwendung zeigt (gleicher Host).
    """
    sicher = _sicheres_ziel(ziel)
    if sicher is not None:
        query = urlsplit(sicher).query
    else:
        referer = urlsplit(request.headers.get("referer") or "")
        eigener_host = request.headers.get("host") or request.url.netloc
        fremd = bool(referer.netloc) and referer.netloc != eigener_host
        query = referer.query if referer.path == "/zeiten" and not fremd else ""
    return [
        (name, wert)
        for name, wert in parse_qsl(query, keep_blank_values=True)
        if name in FILTERPARAMETER
    ]


def _zurueck(
    request: Request,
    *,
    ziel: str | None = None,
    fehler: str | None = None,
    hinweis: str | None = None,
    erfolg: str | None = None,
    entwurf_leer: int | None = None,
) -> RedirectResponse:
    """Zurueck zur Liste, mit den Filtern des Aufrufers -- ohne dessen alte Meldung.

    Der Referer traegt nach einem Fehler ``?fehler=...``. Wer den Fehler
    korrigierte und erneut absandte, landete wieder auf genau dieser URL und
    sah die alte Meldung, obwohl der Eintrag laengst gespeichert war. Deshalb
    wandern nur die Filter mit, und die neue Meldung wird angehaengt.

    Das Ziel ist immer ein relativer Pfad: eine Umleitung auf den Host aus
    dem Referer fuehrte auf eine fremde Seite, wenn das Formular von dort kam.

    Die Meldung wird mit ``quote`` kodiert: ein ``&`` oder ``#`` in der Eingabe
    zerriss sonst die Query, und ein ``+`` wurde zum Leerzeichen.
    """
    parameter = _filter_query(request, ziel)
    for name, text in (("fehler", fehler), ("hinweis", hinweis), ("erfolg", erfolg)):
        if text:
            parameter.append((name, text))
    if entwurf_leer is not None:
        parameter.append(("entwurf_leer", str(entwurf_leer)))
    query = urlencode(parameter, quote_via=quote)
    return RedirectResponse(f"/zeiten?{query}" if query else "/zeiten", status_code=303)


def _ganzzahl(text: str | None) -> int | None:
    """Eine Kennung aus einem Formular- oder Query-Feld; leer oder Unsinn ergibt None.

    FastAPI selbst lehnte ``kunde=`` (das Filterformular sendet das fuer "alle
    Kunden") als Ganzzahl ab und antwortete mit einer JSON-Fehlerseite. Und
    "²" (fuer Python eine Ziffer) oder eine Zahl ab 2**63 endeten als
    Serverfehler -- ``eingabe.kennung`` nimmt nur, was SQLite binden kann.
    """
    return eingabepruefung.kennung(text)


def _merke_titel(conn: sqlite3.Connection, titel: str) -> None:
    """Nimmt einen neuen Titel in den Leistungskatalog auf.

    Der Katalog ist die Voraussetzung dafuer, dass "pro Titel" gruppieren
    funktioniert -- und ein Katalog, den man getrennt pflegen muss, bleibt leer.
    Laeuft in derselben Transaktion wie der Zeiteintrag: entsteht der Eintrag
    nicht, soll auch der Katalogeintrag nicht entstehen.
    """
    neu = katalog.merke(conn, titel)
    if neu is not None:
        db.protokolliere(conn, "ANLEGEN", "leistung", neu,
                         wert_neu={"titel": titel.strip()},
                         begruendung="aus der Zeiterfassung uebernommen")


def _kunde_pruefen(conn: sqlite3.Connection, kunde_id: str) -> tuple[int | None, str | None]:
    """Die Kundennummer aus dem Formular -- oder die Meldung, warum nicht.

    Eine unbekannte Nummer (veraltete Seite) lief vorher in den Fremdschluessel
    und endete als HTTP 500.
    """
    nummer = _ganzzahl(kunde_id)
    if nummer is None:
        roh = (kunde_id or "").strip()
        if roh.isascii() and roh.isdigit():
            # 0 oder eine Zahl jenseits jeder Kennung: gewaehlt, aber unbekannt.
            return None, "Diesen Kunden gibt es nicht."
        return None, "Bitte einen Kunden wählen."
    if conn.execute("SELECT 1 FROM kunde WHERE id = ?", (nummer,)).fetchone() is None:
        return None, "Diesen Kunden gibt es nicht."
    return nummer, None


# ------------------------------------------------------------------- Liste

# Ein Eintrag mit allem, was Liste und Aktionen brauchen. Der Status kommt aus
# der Sicht; nur STORNIERT_GEBUNDEN wird hier ergaenzt: die Sicht meldet eine
# Zeit auf einer stornierten, nicht freigegebenen Rechnung als AUFGESCHRIEBEN,
# obwohl sie noch an der Rechnung haengt und nichts von dem geht, was die Liste
# fuer Aufgeschriebenes anbietet.
_EINTRAEGE_SQL = """
    SELECT v.id, v.kunde_id, v.datum, v.titel, v.dauer_minuten, v.abrechnungsmodus, v.grund,
           v.rechnung_id, v.freigegeben_aus_rechnung_id, v.rechnung_nummer, v.wert_cent,
           CASE WHEN v.status = 'AUFGESCHRIEBEN' AND r.status = 'STORNIERT'
                THEN 'STORNIERT_GEBUNDEN' ELSE v.status END AS status,
           k.name AS kunde, z.beschreibung, z.start_zeit, z.end_zeit, z.abgeschrieben_grund,
           z.version,
           (z.start_zeit IS NOT NULL AND z.end_zeit IS NULL) AS laeuft,
           -- Loeschbar ist nur, was nie auf einer Rechnung lag: nach einem Storno
           -- mit Freigabe haelt position_zeiteintrag die Zeile fest (Fremdschluessel).
           (z.rechnung_id IS NULL
            AND z.freigegeben_aus_rechnung_id IS NULL
            AND NOT EXISTS (SELECT 1 FROM position_zeiteintrag pz
                             WHERE pz.zeiteintrag_id = z.id)) AS loeschbar
      FROM v_zeiteintrag_status v
      JOIN kunde k         ON k.id = v.kunde_id
      JOIN zeiteintrag z   ON z.id = v.id
      LEFT JOIN rechnung r ON r.id = v.rechnung_id
"""


@router.get("", response_class=HTMLResponse)
def liste(
    request: Request,
    # Als Text, nicht als Zahl: das Filterformular sendet ``kunde=`` fuer "alle
    # Kunden", und FastAPI wies den Leerstring als Ganzzahl mit 422 ab.
    kunde: str = "",
    von: str = "",
    bis: str = "",
    status: str = "",
    conn: sqlite3.Connection = Depends(hole_conn),
):
    return _seite(request, conn, kunde=kunde, von=von, bis=bis, status=status)


def _seite(
    request: Request,
    conn: sqlite3.Connection,
    *,
    kunde: str = "",
    von: str = "",
    bis: str = "",
    status: str = "",
    fehler: str | None = None,
    eingabe: dict[str, str] | None = None,
    eingabe_timer: dict[str, str] | None = None,
    eingabe_stopp: dict[str, str] | None = None,
    eingabe_text: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Die Zeitenliste -- auch als Antwort auf ein abgewiesenes Formular.

    Nach einer Fehlermeldung im Erfassen- oder Timerformular wird die Seite mit
    Status 422 und den Eingaben des Nutzers neu gezeigt. Vorher leitete die
    Route mit ``?fehler=`` um, und Kunde, Titel, Dauer und Beschreibung waren
    weg -- wer sich bei der Dauer vertippt hatte, schrieb alles noch einmal.
    Dasselbe gilt fuer die Beschreibung im Timer-Banner (``eingabe_stopp``)
    und fuer "Text ändern" an einer Zeile (``eingabe_text``).
    """
    kunde_id = _ganzzahl(kunde)
    von, bis, status = von.strip(), bis.strip(), status.strip()
    bedingungen: list[str] = []
    werte: list[object] = []
    if kunde_id:
        bedingungen.append("e.kunde_id = ?")
        werte.append(kunde_id)
    if von:
        bedingungen.append("e.datum >= ?")
        werte.append(von)
    if bis:
        bedingungen.append("e.datum <= ?")
        werte.append(bis)
    if status:
        bedingungen.append("e.status = ?")
        werte.append(status)
    wo = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""

    timer = logik.laufender_timer(conn)
    # Ein laufender Timer steht in der Tabelle mit dem Platzhalter von einer
    # Minute. Banner und Liste zeigten deshalb verschiedene Dauern (0:00 gegen
    # 0:01); jetzt kommt die Dauer an beiden Stellen aus derselben Berechnung.
    laufend_minuten = timer.minuten if timer is not None else 0

    zeilen = conn.execute(
        f"SELECT * FROM ({_EINTRAEGE_SQL}) e {wo} ORDER BY e.datum DESC, e.id DESC LIMIT ?",
        [*werte, LISTENGRENZE],
    ).fetchall()
    abschnitte = logik.abschnitte_je_eintrag(conn, (z["id"] for z in zeilen))
    jetzt_utc = db.jetzt_utc()

    def anzeigen(zeile: sqlite3.Row) -> dict[str, object]:
        eintrag = dict(zeile)
        if eintrag["laeuft"]:
            eintrag["dauer_minuten"] = laufend_minuten
            eintrag["pausiert"] = timer is not None and timer.pausiert
        # Die Arbeitsabschnitte eines Timers als Uhrzeiten unter dem Titel --
        # die Aufzeichnung, wann gearbeitet wurde. Weicht die Dauer davon ab
        # (von Hand korrigiert), steht das dabei: die Abschnitte bleiben als
        # Originalaufzeichnung stehen.
        teile = abschnitte.get(eintrag["id"], [])
        if teile:
            offen = bool(eintrag["laeuft"])
            eintrag["abschnitte_teile"] = logik.abschnitte_teile(teile)
            eintrag["pause_minuten"] = (
                logik.pause_sekunden(teile, jetzt_utc, offen=offen) + 30
            ) // 60
            netto = logik.minuten_aus_sekunden(logik.netto_sekunden(teile, jetzt_utc))
            if not offen and netto != eintrag["dauer_minuten"]:
                eintrag["abschnitte_minuten"] = netto
        return eintrag

    eintraege = [anzeigen(zeile) for zeile in zeilen]
    # Summe und Anzahl ueber ALLE gefilterten Eintraege, nicht nur die gezeigten.
    # Der laufende Timer zaehlt mit seiner echten Laufzeit, nicht mit dem
    # Platzhalter.
    gesamt = conn.execute(
        f"SELECT COUNT(*) AS anzahl,"
        f" COALESCE(SUM(CASE WHEN e.laeuft THEN 0 ELSE e.dauer_minuten END), 0) AS minuten,"
        f" COALESCE(SUM(e.laeuft), 0) AS laufend"
        f" FROM ({_EINTRAEGE_SQL}) e {wo}",
        werte,
    ).fetchone()
    summe_minuten = gesamt["minuten"] + (laufend_minuten if gesamt["laufend"] else 0)

    # Nach Tag gruppieren, damit Tagessummen sichtbar sind.
    tage: list[dict[str, object]] = []
    for eintrag in eintraege:
        if not tage or tage[-1]["datum"] != eintrag["datum"]:
            tage.append({"datum": eintrag["datum"], "eintraege": [], "minuten": 0})
        tage[-1]["eintraege"].append(eintrag)
        tage[-1]["minuten"] += eintrag["dauer_minuten"]

    leistungen = conn.execute(
        "SELECT titel, beschreibung_vorlage FROM leistung WHERE aktiv = 1"
        " ORDER BY titel COLLATE DEUTSCH"
    ).fetchall()
    doppelt = kundenlogik.doppelt_sql("k")

    # Das Rueckkehrziel fuer die Formulare: die Liste mit genau diesen Filtern.
    # Es geht als verstecktes Feld mit, weil die Seite nach einer Abweisung
    # unter der POST-Adresse steht und der Referer die Filter nicht mehr kennt.
    filter_werte = [
        (name, str(wert)) for name, wert in
        (("kunde", kunde_id or ""), ("status", status), ("von", von), ("bis", bis))
        if wert
    ]
    zurueck = "/zeiten" + (f"?{urlencode(filter_werte)}" if filter_werte else "")

    return VORLAGEN.TemplateResponse(
        request,
        "zeiten.html",
        {
            "tage": tage,
            "timer": timer,
            # Kundennummer und Ort unterscheiden gleichnamige Kunden in den
            # Auswahllisten ("Doppel GmbH (K-0001, Berlin)").
            "kunden": conn.execute(
                f"SELECT k.id, k.name, k.kundennummer, k.ort, {doppelt} AS doppelt"
                " FROM kunde k WHERE k.aktiv = 1"
                " ORDER BY k.name COLLATE DEUTSCH, k.kundennummer"
            ).fetchall(),
            # Der Filter kennt jeden Kunden, der Zeiten hat -- auch stillgelegte:
            # deren Zeiten sind sonst nur noch ueber "alle Kunden" zu finden.
            "filter_kunden": conn.execute(
                f"""
                SELECT k.id, k.name, k.aktiv, k.kundennummer, k.ort, {doppelt} AS doppelt
                  FROM kunde k
                 WHERE EXISTS (SELECT 1 FROM zeiteintrag z WHERE z.kunde_id = k.id)
                 ORDER BY k.name COLLATE DEUTSCH, k.kundennummer
                """
            ).fetchall(),
            "leistungen": leistungen,
            # Titel -> Beschreibungsvorlage, fuer den Vorschlag im Formular.
            "vorlagen": {
                zeile["titel"]: zeile["beschreibung_vorlage"]
                for zeile in leistungen if zeile["beschreibung_vorlage"]
            },
            "filter": {"kunde": kunde_id, "von": von, "bis": bis, "status": status},
            "zurueck": zurueck,
            "summe_minuten": summe_minuten,
            "anzahl": gesamt["anzahl"],
            "gezeigt": len(eintraege),
            "grenze": LISTENGRENZE,
            "heute": db.heute(),
            "eingabe": eingabe or {},
            "eingabe_timer": eingabe_timer or {},
            "eingabe_stopp": eingabe_stopp,
            "eingabe_text": eingabe_text or {},
            # Je Kunde: was aus den offenen Zeiten auf der Rechnung wuerde.
            "offen": _offen_je_kunde(conn, kunde_id),
            "fehler": fehler or request.query_params.get("fehler"),
            "hinweis": request.query_params.get("hinweis"),
            "erfolg": request.query_params.get("erfolg"),
            "entwurf_leer": _ganzzahl(request.query_params.get("entwurf_leer")),
        },
        status_code=status_code,
    )


def _abgewiesen(
    request: Request,
    conn: sqlite3.Connection,
    fehler: str,
    *,
    eingabe: dict[str, str] | None = None,
    eingabe_timer: dict[str, str] | None = None,
    eingabe_stopp: dict[str, str] | None = None,
    eingabe_text: dict[str, str] | None = None,
    ziel: str | None = None,
) -> HTMLResponse:
    """Die Liste mit Meldung und den Eingaben des Nutzers, Status 422.

    Die Filter kommen aus demselben Ruecksprung wie beim Redirect (Feld
    ``zurueck``, sonst Referer): wer gefiltert hatte, sieht nach der Meldung
    dieselbe Liste -- und die neu gezeigten Formulare tragen sie weiter.
    """
    filter_werte = dict(_filter_query(request, ziel))
    return _seite(
        request, conn,
        kunde=filter_werte.get("kunde", ""), von=filter_werte.get("von", ""),
        bis=filter_werte.get("bis", ""), status=filter_werte.get("status", ""),
        fehler=fehler, eingabe=eingabe, eingabe_timer=eingabe_timer,
        eingabe_stopp=eingabe_stopp, eingabe_text=eingabe_text, status_code=422,
    )


def _offen_je_kunde(conn: sqlite3.Connection, kunde_id: int | None) -> list[dict[str, object]]:
    """Je Kunde: erfasste und abrechenbare Zeit der noch nicht abgerechneten Zeiten.

    Gerechnet mit DERSELBEN Funktion wie die Rechnung (gruppierung.vorschau),
    mit der Gruppierung und der Rundungsregel des Kunden. So steht in der
    Liste „0:52 erfasst → 1:00 abrechenbar“, und die Rechnung sagt hinterher
    dasselbe. Vorher zeigte die Liste nur Rohzeit und Rohwert -- 0:07 und
    9,33 € --, und auf der Rechnung standen 0,25 h und 20,00 €.

    Offen heisst wie im Rechnungsassistenten: keine Rechnung, nicht
    abgeschrieben, nicht intern, kein offener Timer. Der Zeitraumfilter der
    Liste gilt hier nicht -- abgerechnet werden alle offenen Zeiten.
    """
    bedingung = "AND z.kunde_id = ?" if kunde_id else ""
    zeilen = conn.execute(
        f"""
        SELECT z.*, k.name AS kunde_name, k.gruppierung_default
          FROM zeiteintrag z
          JOIN kunde k ON k.id = z.kunde_id
         WHERE z.rechnung_id IS NULL AND z.abgeschrieben_am IS NULL
           AND z.abrechnungsmodus <> 'INTERN'
           AND NOT (z.start_zeit IS NOT NULL AND z.end_zeit IS NULL)
           {bedingung}
         ORDER BY k.name COLLATE DEUTSCH, k.id, z.datum, z.id
        """,
        (kunde_id,) if kunde_id else (),
    ).fetchall()
    je_kunde: dict[int, list[sqlite3.Row]] = {}
    for zeile in zeilen:
        je_kunde.setdefault(zeile["kunde_id"], []).append(zeile)
    ergebnis = []
    for kid, eintraege in je_kunde.items():
        regel = kundenlogik.rundungsregel(conn, kid)
        modus = eintraege[0]["gruppierung_default"] or gruppierung.PRO_TITEL
        satz = kundenlogik.satz_cent(conn, kid)
        vorschau = gruppierung.vorschau(
            [dict(z) for z in eintraege], modus=modus, standard_cent=satz,
            intervall_min=regel.intervall_min, modus_rundung=regel.modus,
        )
        ergebnis.append({
            "kunde_id": kid,
            "kunde": eintraege[0]["kunde_name"],
            "anzahl": len(eintraege),
            "erfasst": vorschau.erfasst_minuten,
            "abrechenbar": vorschau.abrechenbar_minuten,
            "betrag_cent": vorschau.betrag_cent if satz > 0 else None,
            "regel": regel,
            "gerundet": regel.wirkt(modus),
            "gruppierung": gruppierung.MODUS_TEXT.get(modus, modus),
        })
    return ergebnis


# ---------------------------------------------------------------- Erfassen

@router.post("")
def erfassen(
    request: Request,
    # Vorbelegungen statt Form(...): ein fehlendes Feld soll eine deutsche
    # Meldung ergeben, nicht die generische Fehlerseite.
    kunde_id: str = Form(""),
    titel: str = Form(""),
    datum: str = Form(""),
    dauer: str = Form(""),
    beschreibung: str = Form(""),
    modus: str = Form("STUNDEN"),
    grund: str = Form(""),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    eingabe = {
        "kunde_id": kunde_id, "titel": titel, "datum": datum, "dauer": dauer,
        "beschreibung": beschreibung, "modus": modus, "grund": grund,
    }

    def abgewiesen(text: str) -> HTMLResponse:
        return _abgewiesen(request, conn, text, eingabe=eingabe, ziel=zurueck)

    kunde, problem = _kunde_pruefen(conn, kunde_id)
    if problem:
        return abgewiesen(problem)
    titel = titel.strip()
    if not titel:
        # HTML "required" laesst "   " durch; gespeichert wurde ein leerer Titel,
        # und der Katalog meldete fortan einen unbekannten Titel ohne Namen.
        return abgewiesen("Der Titel fehlt.")
    try:
        tag = logik.pruefe_datum(datum, heute=db.heute())
    except logik.DatumUngueltig as fehler:
        return abgewiesen(str(fehler))
    try:
        minuten = logik.parse_dauer(dauer)
    except logik.DauerUngueltig as fehler:
        return abgewiesen(str(fehler))
    if modus not in MODI:
        return abgewiesen("Unbekannter Abrechnungsmodus.")
    if modus == "PAUSCHAL":
        # Eine Pauschale braucht einen Betrag, den dieses Formular nicht hat;
        # ohne ihn lief der Eintrag in den CHECK und endete als HTTP 500.
        return abgewiesen(PAUSCHAL_MELDUNG)
    if modus in ("KULANZ", "INTERN") and not grund.strip():
        return abgewiesen(f"{modus} braucht eine Begründung.")
    try:
        text = logik.pruefe_beschreibung(beschreibung)
    except logik.BeschreibungUngueltig as fehler:
        return abgewiesen(str(fehler))

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """
            INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum, dauer_minuten,
                                     abrechnungsmodus, grund, satz_cent_snapshot, erfasst_am)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kunde, titel, text, tag, minuten,
                modus, grund.strip() or None,
                # Kulanz braucht einen eingefrorenen Satz, sonst ist der Verzicht
                # nach der naechsten Satzerhoehung nicht mehr bewertbar.
                kundenlogik.satz_cent(conn, kunde) if modus == "KULANZ" else None,
                db.jetzt_utc(),
            ),
        )
        db.protokolliere(conn, "ERFASSEN", "zeiteintrag", cur.lastrowid,
                         wert_neu={"minuten": minuten, "titel": titel, "modus": modus})
        _merke_titel(conn, titel)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(request, ziel=zurueck)


# ------------------------------------------------------------------- Timer
#
# Ein Timer ist ein Zeiteintrag mit Beginn und ohne Ende; seine Arbeitsphasen
# stehen in zeit_abschnitt (Beginn und Ende in UTC). "Laeuft" heisst: ein
# Abschnitt ist offen. "Pausiert" heisst: kein Abschnitt offen, der Eintrag
# aber auch nicht beendet. Pausen werden nicht gespeichert -- sie sind die
# Luecken zwischen den Abschnitten. Die Dauer ist die Summe der Abschnitte,
# EINMAL auf Minuten gerundet.

@dataclass(frozen=True, slots=True)
class Gestoppt:
    """Was das Stoppen eines Timers ergab -- und was der Nutzer dazu erfahren muss."""

    id: int
    minuten: int
    titel: str = ""
    fehler: str | None = None
    hinweis: str | None = None
    #: Was mit der Zeit auf der Rechnung geschieht, als Satzteil -- etwa
    #: "abgerechnet wird je Rechnungsposition auf volle 15 Minuten aufgerundet".
    abrechnung: str = ""

    @property
    def erfolg(self) -> str | None:
        """Die Rueckmeldung nach dem Stoppen, z. B. "Gestoppt: 0:07 erfasst (Support) — ...".

        Vorher kam nach "Stoppen" nichts: der Eintrag stand nur stumm in der
        Liste, und ob der Klick gewirkt hatte, liess sich nur dort ablesen.
        Und es stand nirgends, dass 0:07 nicht 0:07 bleiben: die Rundung
        geschieht erst je Rechnungsposition. Die Meldung sagt das jetzt --
        mit der Regel, die fuer diesen Kunden gilt.
        """
        if self.fehler:
            return None
        text = f"Gestoppt: {logik.formatiere_dauer(self.minuten)} erfasst ({self.titel})"
        if self.abrechnung:
            text += f" — {self.abrechnung}"
        return text + "."


class _Hinweis(Exception):
    """Nichts zu tun, aber kein Fehler -- etwa "bereits pausiert" nach einem Doppelklick."""

    def __init__(self, meldung: str) -> None:
        super().__init__(meldung)
        self.meldung = meldung


#: Ein Formularfeld, das gar nicht gesendet wurde. ``None`` heisst bei der
#: Beschreibung "leer gemacht" -- das ist etwas anderes als "nicht im Formular".
_UNVERAENDERT = object()


def _beschreibung_lesen(roh: str | None) -> object:
    """Die Beschreibung aus einem Formular: normalisiert, ``None`` oder _UNVERAENDERT.

    Wirft :class:`logik.BeschreibungUngueltig`, wenn sie zu lang ist.
    """
    if roh is None:
        return _UNVERAENDERT
    return logik.pruefe_beschreibung(roh)


def _beschreibung_uebernehmen(
    conn: sqlite3.Connection, eintrag: sqlite3.Row, neu: object
) -> bool:
    """Speichert eine geaenderte Beschreibung -- in der laufenden Transaktion, mit Journal.

    Gibt True zurueck, wenn sich etwas geaendert hat.
    """
    if neu is _UNVERAENDERT or neu == eintrag["beschreibung"]:
        return False
    _aktualisiere(
        conn,
        "UPDATE zeiteintrag SET beschreibung = ?, geaendert_am = ?, version = version + 1"
        " WHERE id = ? AND version = ?",
        (neu, db.jetzt_utc(), eintrag["id"], eintrag["version"]),
    )
    db.protokolliere(conn, "AENDERN", "zeiteintrag", eintrag["id"], feld="beschreibung",
                     wert_alt=eintrag["beschreibung"], wert_neu=neu)
    return True


def _offener_timer(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Der eine offene Timer (laufend oder pausiert) samt Zustand seiner Rechnung."""
    return conn.execute(
        """
        SELECT z.*, r.status AS rechnung_status, r.nummer AS rechnung_nummer
          FROM zeiteintrag z
          LEFT JOIN rechnung r ON r.id = z.rechnung_id
         WHERE z.start_zeit IS NOT NULL AND z.end_zeit IS NULL
         LIMIT 1
        """
    ).fetchone()


def _abschnitte_sichern(conn: sqlite3.Connection, eintrag: sqlite3.Row) -> list[logik.Abschnitt]:
    """Die Abschnitte eines offenen Timers -- notfalls der erste aus seinem Beginn.

    Ein Timer aus der Zeit vor den Abschnitten (oder einer, den die Migration
    nicht erreichte) hat keinen. Er bekommt hier den ersten, ab seinem
    gespeicherten Start; danach verhaelt er sich wie jeder andere. Ein Start,
    der sich nicht lesen laesst, ergibt eine leere Liste.
    """
    abschnitte = logik.lade_abschnitte(conn, eintrag["id"])
    if abschnitte:
        return abschnitte
    alt = logik.alt_abschnitt(eintrag["datum"], eintrag["start_zeit"])
    if alt is None:
        return []
    conn.execute(
        "INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc) VALUES (?, ?)",
        (eintrag["id"], alt.beginn_utc),
    )
    return [alt]


def _abrechnung(conn: sqlite3.Connection, kunde_id: int) -> str:
    """Wie erfasste Zeit dieses Kunden abgerechnet wird -- fuer die Meldung nach dem Stoppen."""
    zeile = conn.execute(
        "SELECT gruppierung_default FROM kunde WHERE id = ?", (kunde_id,)
    ).fetchone()
    regel = kundenlogik.rundungsregel(conn, kunde_id)
    return regel.abrechnung(zeile["gruppierung_default"] if zeile else gruppierung.PRO_TITEL)


def _beende(
    conn: sqlite3.Connection,
    eintrag: sqlite3.Row,
    ende_utc: str,
    *,
    begruendung: str | None = None,
) -> Gestoppt:
    """Beendet einen offenen Timer, dessen Abschnitte alle geschlossen sind.

    Die Dauer ist die Summe der Abschnitte, EINMAL auf Minuten gerundet --
    je Abschnitt gerundet kaeme mit jeder Pause bis zu eine Minute dazu.
    ``end_zeit`` ist das Ende des letzten Abschnitts in Ortszeit, ueber den
    Tag hinaus das Tagesende (das Schema kennt kein Enddatum); das genaue
    Ende steht im Abschnitt.

    Liegt der Timer in einem Entwurf, werden dessen Positionen neu erzeugt:
    sie entstanden mit dem Platzhalter von einer Minute und wuessten sonst
    nichts von der echten Dauer -- die Rechnung ginge mit 0,25 h hinaus.
    """
    abschnitte = logik.lade_abschnitte(conn, eintrag["id"])
    minuten = logik.minuten_aus_sekunden(logik.netto_sekunden(abschnitte, ende_utc))
    ende = logik.ende_als_endzeit(eintrag["datum"], eintrag["start_zeit"], ende_utc)
    conn.execute(
        "UPDATE zeiteintrag SET end_zeit = ?, dauer_minuten = ?, geaendert_am = ?,"
        " version = version + 1 WHERE id = ?",
        (ende, minuten, db.jetzt_utc(), eintrag["id"]),
    )
    db.protokolliere(conn, "TIMER_STOP", "zeiteintrag", eintrag["id"],
                     feld="dauer_minuten", wert_neu=minuten, begruendung=begruendung)
    if eintrag["rechnung_status"] == "ENTWURF":
        fs.positionen_neu(conn, eintrag["rechnung_id"])
    hinweis = None
    if minuten > 24 * 60:
        # Mit dem Weg: das Erfassen-Formular nimmt hoechstens 24 Stunden an,
        # die Korrektur geht nur ueber "Dauer ändern" in der Liste.
        hinweis = (
            f"Der Timer lief {round(minuten / 60)} Stunden — bitte prüfen und in der "
            f"Liste unter „Dauer ändern“ korrigieren."
        )
    return Gestoppt(eintrag["id"], minuten, titel=eintrag["titel"], hinweis=hinweis,
                    abrechnung=_abrechnung(conn, eintrag["kunde_id"]))


def _stoppe_laufenden(conn: sqlite3.Connection, jetzt_utc: str) -> Gestoppt | None:
    """Stoppt den offenen Timer, falls es einen gibt -- laufend oder pausiert.

    Laeuft er, endet sein Abschnitt jetzt. Ist er pausiert, endet er mit dem
    Beginn der Pause und NICHT mit dem Klick auf "Stoppen": die Pause ist
    keine Arbeit, auch wenn sie erst spaeter als Feierabend erkannt wird.
    Ueber Mitternacht und ueber die Zeitumstellung rechnen die Abschnitte in
    UTC richtig: gestern 23:00 bis heute 10:44 sind 704 Minuten.

    Liegt der Timer auf einer FESTGESCHRIEBENEN Rechnung (Altbestand), darf
    sich die Dauer nicht mehr aendern (Trigger ``zeit_gesperrt``). Dann wird
    nur der offene Abschnitt geschlossen und die Endzeit gesetzt, damit der
    Timer nicht fuer immer laeuft und jeden neuen blockiert; die
    Rueckmeldung nennt den Ausweg (Storno).
    """
    laufend = _offener_timer(conn)
    if laufend is None:
        return None

    if laufend["rechnung_status"] == "FESTGESCHRIEBEN":
        conn.execute(
            "UPDATE zeit_abschnitt SET ende_utc = ? WHERE zeiteintrag_id = ? AND ende_utc IS NULL",
            (jetzt_utc, laufend["id"]),
        )
        abschnitte = logik.lade_abschnitte(conn, laufend["id"])
        letztes = abschnitte[-1].ende_utc if abschnitte else jetzt_utc
        ende = logik.ende_als_endzeit(laufend["datum"], laufend["start_zeit"], letztes or jetzt_utc)
        conn.execute(
            "UPDATE zeiteintrag SET end_zeit = ?, geaendert_am = ?, version = version + 1"
            " WHERE id = ?",
            (ende, db.jetzt_utc(), laufend["id"]),
        )
        db.protokolliere(conn, "TIMER_STOP", "zeiteintrag", laufend["id"],
                         feld="end_zeit", wert_neu=ende,
                         begruendung="Dauer gesperrt: liegt auf festgeschriebener Rechnung")
        return Gestoppt(
            laufend["id"], 0, titel=laufend["titel"],
            fehler=(
                f"Der Timer „{laufend['titel']}“ lag auf der festgeschriebenen Rechnung "
                f"{laufend['rechnung_nummer']}. Er wurde gestoppt, die abgerechnete Dauer "
                f"bleibt — Korrektur nur über Storno der Rechnung."
            ),
        )

    abschnitte = _abschnitte_sichern(conn, laufend)
    if not abschnitte:
        # Ein Beginn, der sich nicht lesen laesst: nicht raten. Eine Minute,
        # die Liste bietet "Dauer ändern" an.
        lokal = logik.ortszeit(jetzt_utc).replace(tzinfo=None)
        conn.execute(
            "UPDATE zeiteintrag SET end_zeit = ?, dauer_minuten = 1, geaendert_am = ?,"
            " version = version + 1 WHERE id = ?",
            (logik.timer_ende(laufend["datum"], laufend["start_zeit"], lokal),
             db.jetzt_utc(), laufend["id"]),
        )
        db.protokolliere(conn, "TIMER_STOP", "zeiteintrag", laufend["id"],
                         feld="dauer_minuten", wert_neu=1,
                         begruendung="Beginn nicht lesbar")
        return Gestoppt(laufend["id"], 1, titel=laufend["titel"],
                        hinweis="Der Beginn dieses Timers ließ sich nicht lesen — bitte die "
                                "Dauer in der Liste unter „Dauer ändern“ eintragen.")
    if abschnitte[-1].offen:
        conn.execute(
            "UPDATE zeit_abschnitt SET ende_utc = ? WHERE zeiteintrag_id = ? AND ende_utc IS NULL",
            (jetzt_utc, laufend["id"]),
        )
        return _beende(conn, laufend, jetzt_utc)
    pause_ab = abschnitte[-1].ende_utc or jetzt_utc
    return _beende(
        conn, laufend, pause_ab,
        begruendung=(
            f"aus der Pause gestoppt: Ende = Beginn der Pause "
            f"({logik.ortszeit(pause_ab):%H:%M})"
        ),
    )


def _integritaet(fehler: sqlite3.IntegrityError) -> str:
    """Die Meldung eines Datenbank-Triggers in der Sprache des Nutzers."""
    if "festgeschriebenen Rechnung" in str(fehler):
        return (
            "Diese Zeit liegt auf einer festgeschriebenen Rechnung und lässt sich nicht "
            "mehr ändern. Korrektur nur über Storno der Rechnung."
        )
    return f"Die Datenbank hat die Änderung abgelehnt: {fehler}"


def _jetzt() -> tuple[str, _dt.datetime]:
    """Jetzt als UTC-Text und als Ortszeit (ohne Zeitzone) -- dieselbe Sekunde."""
    jetzt_utc = logik.utc_text(_dt.datetime.now(_dt.UTC))
    return jetzt_utc, logik.ortszeit(jetzt_utc).replace(tzinfo=None)


@router.post("/timer/start")
def timer_start(
    request: Request,
    kunde_id: str = Form(""),
    titel: str = Form(""),
    beschreibung: str = Form(""),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Startet einen Timer. Ein offener -- laufend oder pausiert -- wird vorher beendet.

    Das Schema erlaubt nur einen offenen Timer; ohne das vorherige Beenden
    liefe der Trigger in einen Fehler, statt das Offensichtliche zu tun. Ein
    pausierter endet dabei mit dem Beginn seiner Pause.

    Die Beschreibung ist optional: woran genau gearbeitet wird, weiss man oft
    erst beim Stoppen -- dort laesst sie sich ergaenzen.
    """
    eingabe_timer = {"kunde_id": kunde_id, "titel": titel, "beschreibung": beschreibung}

    def abgewiesen(text: str) -> HTMLResponse:
        return _abgewiesen(request, conn, text, eingabe_timer=eingabe_timer, ziel=zurueck)

    kunde, problem = _kunde_pruefen(conn, kunde_id)
    if problem:
        return abgewiesen(problem)
    titel = titel.strip()
    if not titel:
        return abgewiesen("Der Titel fehlt.")
    try:
        text = logik.pruefe_beschreibung(beschreibung)
    except logik.BeschreibungUngueltig as fehler:
        return abgewiesen(str(fehler))
    jetzt_utc, lokal = _jetzt()
    conn.execute("BEGIN IMMEDIATE")
    try:
        gestoppt = _stoppe_laufenden(conn, jetzt_utc)
        cur = conn.execute(
            """
            INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum, start_zeit,
                                     dauer_minuten, abrechnungsmodus, erfasst_am)
            VALUES (?, ?, ?, ?, ?, 1, 'STUNDEN', ?)
            """,
            (kunde, titel, text, lokal.date().isoformat(), lokal.strftime("%H:%M:%S"),
             db.jetzt_utc()),
        )
        conn.execute(
            "INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc) VALUES (?, ?)",
            (cur.lastrowid, jetzt_utc),
        )
        db.protokolliere(conn, "TIMER_START", "zeiteintrag", cur.lastrowid,
                         wert_neu={"start": lokal.strftime("%H:%M:%S"), "titel": titel})
        _merke_titel(conn, titel)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return abgewiesen(f"Der Timer konnte nicht starten: {_integritaet(fehler)}")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if gestoppt is None:
        return _zurueck(request, ziel=zurueck)
    return _zurueck(request, ziel=zurueck, fehler=gestoppt.fehler, hinweis=gestoppt.hinweis)


@router.post("/timer/pause")
def timer_pause(
    request: Request,
    beschreibung: str | None = Form(None),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Pausiert den laufenden Timer: sein Abschnitt endet jetzt.

    Der Eintrag bleibt offen und zaehlt weiter als DER eine Timer; die Liste
    zeigt ihn als "pausiert", abrechenbar ist er erst nach dem Stoppen. Eine
    im Banner geaenderte Beschreibung wird mitgespeichert.
    """
    try:
        text = _beschreibung_lesen(beschreibung)
    except logik.BeschreibungUngueltig as fehler:
        return _abgewiesen(request, conn, str(fehler), eingabe_stopp={"beschreibung":
                           beschreibung or ""}, ziel=zurueck)
    jetzt_utc, lokal = _jetzt()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Zustand INNERHALB der Sperre lesen: ein Doppelklick auf "Pause"
        # schloss sonst zweimal -- und das Journal bekaeme zwei Pausen.
        laufend = _offener_timer(conn)
        if laufend is None:
            raise _Hinweis("Es läuft kein Timer.")
        if problem := _sperre(laufend):
            raise _Abbruch(problem)
        abschnitte = _abschnitte_sichern(conn, laufend)
        if not abschnitte:
            raise _Abbruch("Der Beginn dieses Timers lässt sich nicht lesen — bitte stoppen "
                           "und die Dauer in der Liste eintragen.")
        if not abschnitte[-1].offen:
            raise _Hinweis(
                f"Der Timer ist bereits pausiert "
                f"(seit {logik.ortszeit(abschnitte[-1].ende_utc or jetzt_utc):%H:%M})."
            )
        _aktualisiere(
            conn,
            "UPDATE zeit_abschnitt SET ende_utc = ? WHERE zeiteintrag_id = ? AND ende_utc IS NULL",
            (jetzt_utc, laufend["id"]),
        )
        _beschreibung_uebernehmen(conn, laufend, text)
        netto = logik.minuten_aus_sekunden(
            logik.netto_sekunden(logik.lade_abschnitte(conn, laufend["id"]), jetzt_utc)
        )
        db.protokolliere(conn, "TIMER_PAUSE", "zeiteintrag", laufend["id"], feld="abschnitt",
                         wert_neu={"ende_utc": jetzt_utc, "netto_minuten": netto})
        conn.execute("COMMIT")
    except _Hinweis as nichts:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, hinweis=nichts.meldung)
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck,
                        fehler=f"Der Timer ließ sich nicht pausieren: {_integritaet(fehler)}")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(
        request, ziel=zurueck,
        erfolg=(f"Pause ab {lokal:%H:%M} — bisher {logik.formatiere_dauer(netto)} "
                f"gearbeitet ({laufend['titel']})."),
    )


@router.post("/timer/weiter")
def timer_weiter(
    request: Request,
    beschreibung: str | None = Form(None),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Setzt den pausierten Timer fort: ein neuer Abschnitt beginnt jetzt.

    An einem ANDEREN Kalendertag als dem des Eintrags (abends pausiert,
    morgens fortgesetzt) entsteht ein neuer Eintrag fuer heute -- gleicher
    Kunde, Titel und Beschreibung --, und der alte endet mit dem Beginn
    seiner Pause. Sonst stuende Arbeit von heute unter dem Datum von
    gestern: in der Gruppierung „pro Tag“, im Leistungszeitraum und in der
    Pruefung der Rechnung.
    """
    try:
        text = _beschreibung_lesen(beschreibung)
    except logik.BeschreibungUngueltig as fehler:
        return _abgewiesen(request, conn, str(fehler), eingabe_stopp={"beschreibung":
                           beschreibung or ""}, ziel=zurueck)
    jetzt_utc, lokal = _jetzt()
    heute = lokal.date().isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        laufend = _offener_timer(conn)
        if laufend is None:
            raise _Hinweis("Es ist kein Timer pausiert.")
        if problem := _sperre(laufend):
            raise _Abbruch(problem)
        abschnitte = logik.lade_abschnitte(conn, laufend["id"])
        if not abschnitte or abschnitte[-1].offen:
            raise _Hinweis("Der Timer läuft bereits.")
        _beschreibung_uebernehmen(conn, laufend, text)
        if laufend["datum"] == heute:
            conn.execute(
                "INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc) VALUES (?, ?)",
                (laufend["id"], jetzt_utc),
            )
            db.protokolliere(conn, "TIMER_WEITER", "zeiteintrag", laufend["id"],
                             feld="abschnitt", wert_neu={"beginn_utc": jetzt_utc})
            erfolg = f"Weiter ab {lokal:%H:%M} ({laufend['titel']})."
        else:
            alt = _offener_timer(conn)  # nach einer geaenderten Beschreibung neu lesen
            pause_ab = abschnitte[-1].ende_utc or jetzt_utc
            beendet = _beende(
                conn, alt, pause_ab,
                begruendung=(
                    f"beim Fortsetzen an einem neuen Tag beendet: Ende = Beginn der Pause "
                    f"({logik.ortszeit(pause_ab):%H:%M})"
                ),
            )
            cur = conn.execute(
                """
                INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum, start_zeit,
                                         dauer_minuten, abrechnungsmodus, erfasst_am)
                VALUES (?, ?, ?, ?, ?, 1, 'STUNDEN', ?)
                """,
                (alt["kunde_id"], alt["titel"], alt["beschreibung"], heute,
                 lokal.strftime("%H:%M:%S"), db.jetzt_utc()),
            )
            conn.execute(
                "INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc) VALUES (?, ?)",
                (cur.lastrowid, jetzt_utc),
            )
            db.protokolliere(conn, "TIMER_WEITER", "zeiteintrag", cur.lastrowid,
                             feld="abschnitt",
                             wert_neu={"beginn_utc": jetzt_utc, "fortsetzung_von": alt["id"]},
                             begruendung="Fortsetzen an einem neuen Tag: neuer Eintrag für heute")
            erfolg = (
                f"Weiter als neuer Eintrag für heute. Der Eintrag vom "
                f"{_dt.date.fromisoformat(alt['datum']):%d.%m.%Y} endet mit der Pause um "
                f"{logik.ortszeit(pause_ab):%H:%M} "
                f"({logik.formatiere_dauer(beendet.minuten)} erfasst)."
            )
        conn.execute("COMMIT")
    except _Hinweis as nichts:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, hinweis=nichts.meldung)
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck,
                        fehler=f"Der Timer ließ sich nicht fortsetzen: {_integritaet(fehler)}")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(request, ziel=zurueck, erfolg=erfolg)


@router.post("/timer/stop")
def timer_stop(
    request: Request,
    beschreibung: str | None = Form(None),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Stoppt den offenen Timer -- und speichert die Beschreibung in DERSELBEN Transaktion.

    Was man getan hat, weiss man oft erst am Ende. Das Stoppen-Formular
    traegt deshalb die Beschreibung, vorbelegt mit der bisherigen; ein
    Formular ohne das Feld laesst sie, wie sie ist.
    """
    try:
        text = _beschreibung_lesen(beschreibung)
    except logik.BeschreibungUngueltig as fehler:
        return _abgewiesen(request, conn, str(fehler), eingabe_stopp={"beschreibung":
                           beschreibung or ""}, ziel=zurueck)
    jetzt_utc, _lokal = _jetzt()
    conn.execute("BEGIN IMMEDIATE")
    try:
        laufend = _offener_timer(conn)
        if laufend is not None and _sperre(laufend) is None:
            _beschreibung_uebernehmen(conn, laufend, text)
        gestoppt = _stoppe_laufenden(conn, jetzt_utc)
        conn.execute("COMMIT")
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        # Sicherheitsnetz: ein Trigger, der hier noch zuschlaegt, soll eine
        # Meldung ergeben und keinen HTTP 500.
        conn.execute("ROLLBACK")
        return _zurueck(
            request, ziel=zurueck,
            fehler=f"Der Timer konnte nicht gestoppt werden: {_integritaet(fehler)}",
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if gestoppt is None:
        return _zurueck(request, ziel=zurueck)
    return _zurueck(request, ziel=zurueck, fehler=gestoppt.fehler, hinweis=gestoppt.hinweis,
                    erfolg=gestoppt.erfolg)


# ---------------------------------------------------------- Modus, Abschreiben

def _lade(conn: sqlite3.Connection, zeit_id: int | None) -> sqlite3.Row | None:
    """Der Zeiteintrag samt Zustand der Rechnung, an der er haengt.

    Eine Zeit mit ``rechnung_id`` haengt in einem Entwurf, auf einer
    festgeschriebenen oder auf einer stornierten Rechnung. Der Unterschied ist
    alles: der Entwurf laesst sich noch aendern, die festgeschriebene Rechnung
    nie (Trigger ``zeit_gesperrt``), und die stornierte haelt die Zeit fest, bis
    sie freigegeben wird. Wer nicht unterscheidet, bekommt entweder einen HTTP
    500 aus dem Trigger oder eine Rechnung, die nicht mehr zu ihren Zeiten passt.

    ``None`` als Kennung (Unsinn oder eine Zahl ab 2**63 in der Adresse)
    ergibt None -- wie eine Zeit, die es nicht gibt.
    """
    if zeit_id is None:
        return None
    return conn.execute(
        """
        SELECT z.*, r.status AS rechnung_status, r.nummer AS rechnung_nummer,
               (z.start_zeit IS NOT NULL AND z.end_zeit IS NULL) AS laeuft
          FROM zeiteintrag z
          LEFT JOIN rechnung r ON r.id = z.rechnung_id
         WHERE z.id = ?
        """,
        (zeit_id,),
    ).fetchone()


def _sperre(eintrag: sqlite3.Row) -> str | None:
    """Warum sich diese Zeit nicht aendern laesst -- oder None, wenn sie frei ist."""
    if eintrag["rechnung_status"] == "FESTGESCHRIEBEN":
        return (
            f"Diese Zeit liegt auf der festgeschriebenen Rechnung "
            f"{eintrag['rechnung_nummer']}. Korrektur nur über Storno."
        )
    if eintrag["rechnung_status"] == "STORNIERT":
        return (
            f"Diese Zeit liegt auf der stornierten Rechnung {eintrag['rechnung_nummer']}, "
            f"ist aber nicht freigegeben — erst freigeben."
        )
    return None


def _entwurf_dahinter(eintrag: sqlite3.Row) -> int | None:
    """Der Rechnungsentwurf, in dem diese Zeit liegt -- oder None."""
    if eintrag["rechnung_status"] == "ENTWURF":
        return int(eintrag["rechnung_id"])
    return None


def _entwurf_ohne_zeiten(conn: sqlite3.Connection, entwurf: int | None) -> int | None:
    """Der Entwurf, falls er nach der Aenderung keine Zeit mehr hat.

    Sonst bleibt ein leerer 0,00-€-Entwurf in der Rechnungsliste stehen, und
    der Nutzer erfaehrt erst beim Festschreiben, dass er keine Position hat.
    """
    if entwurf is None:
        return None
    rest = conn.execute(
        "SELECT COUNT(*) AS n FROM zeiteintrag WHERE rechnung_id = ?", (entwurf,)
    ).fetchone()["n"]
    return entwurf if rest == 0 else None


class _Abbruch(Exception):
    """Eine Pruefung innerhalb der Transaktion hat abgelehnt: zurueckrollen, melden."""

    def __init__(self, meldung: str) -> None:
        super().__init__(meldung)
        self.meldung = meldung


def _in_transaktion(conn: sqlite3.Connection, zeit_id: int | None) -> sqlite3.Row:
    """Oeffnet die Schreibtransaktion und laedt den Eintrag DARIN.

    Die Pruefung des Zustands lief bisher vor ``BEGIN IMMEDIATE``. Zwei
    gleichzeitige Klicks auf "Abschreiben" sahen dann beide eine offene Zeit,
    beide schrieben, und das unveraenderliche Journal bekam zwei Eintraege.
    Erst innerhalb der Sperre ist der gelesene Zustand auch der, den das
    UPDATE vorfindet.
    """
    conn.execute("BEGIN IMMEDIATE")
    eintrag = _lade(conn, zeit_id)
    if eintrag is None:
        raise _Abbruch(KEIN_EINTRAG)
    return eintrag


def _aktualisiere(conn: sqlite3.Connection, sql: str, werte: tuple) -> None:
    """Ein UPDATE, das GENAU eine Zeile treffen muss -- sonst kein Journal."""
    if conn.execute(sql, werte).rowcount != 1:
        raise _Abbruch(GEAENDERT_MELDUNG)


@router.post("/{zeit_id}/modus")
def modus_setzen(
    zeit_id: str,
    request: Request,
    modus: str = Form(""),
    grund: str = Form(""),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Setzt den Abrechnungsmodus -- der Weg zu "mit 0 € berechnen" und zurueck.

    KULANZ heisst: die Stunde erscheint MIT 0,00 € auf der Rechnung, der Kunde
    sieht den Verzicht. INTERN heisst: sie erscheint nie auf einer Rechnung.
    Beides braucht eine Begruendung, weil die Verzichtsauswertung sie zeigt.
    STUNDEN ist der Rueckweg ("wieder berechnen").
    """
    if modus not in MODI:
        return _zurueck(request, ziel=zurueck, fehler="Unbekannter Abrechnungsmodus.")
    if modus == "PAUSCHAL":
        return _zurueck(request, ziel=zurueck, fehler=PAUSCHAL_MELDUNG)
    if modus in ("KULANZ", "INTERN") and not grund.strip():
        return _zurueck(request, ziel=zurueck, fehler=f"{modus} braucht eine Begründung.")

    try:
        eintrag = _in_transaktion(conn, _ganzzahl(zeit_id))
        if eintrag["laeuft"]:
            raise _Abbruch(TIMER_LAEUFT_MELDUNG)
        if problem := _sperre(eintrag):
            raise _Abbruch(problem)
        entwurf = _entwurf_dahinter(eintrag)
        if modus == "INTERN" and entwurf is not None:
            # Interne Arbeit erscheint auf keiner Rechnung (CHECK im Schema):
            # die Zeit muss zuerst aus dem Entwurf heraus, wie beim Abschreiben.
            conn.execute("UPDATE zeiteintrag SET rechnung_id = NULL WHERE id = ?",
                         (eintrag["id"],))
        _aktualisiere(
            conn,
            "UPDATE zeiteintrag SET abrechnungsmodus = ?, grund = ?, satz_cent_snapshot = ?,"
            " geaendert_am = ?, version = version + 1 WHERE id = ? AND version = ?",
            (
                modus,
                grund.strip() or None,
                # Der eingefrorene Satz bewertet den Verzicht. Wer wieder nach
                # Zeit berechnet, soll den beim Abrechnen gueltigen Satz bekommen
                # -- wie jede andere offene Stunde auch.
                None if modus == "STUNDEN" else (
                    eintrag["satz_cent_snapshot"]
                    or kundenlogik.satz_cent(conn, eintrag["kunde_id"])
                ),
                db.jetzt_utc(),
                eintrag["id"],
                eintrag["version"],
            ),
        )
        db.protokolliere(conn, "MODUS", "zeiteintrag", eintrag["id"], feld="abrechnungsmodus",
                         wert_alt=eintrag["abrechnungsmodus"], wert_neu=modus,
                         begruendung=grund.strip() or None)
        if entwurf is not None:
            # Sonst steht die verschenkte Stunde weiter mit vollem Betrag im
            # Entwurf: die Positionen entstehen beim Anlegen und wissen vom
            # Moduswechsel nichts. Festgeschrieben wuerde dem Kunden berechnet,
            # was ihm erlassen werden sollte.
            fs.positionen_neu(conn, entwurf)
        leer = _entwurf_ohne_zeiten(conn, entwurf)
        conn.execute("COMMIT")
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=_integritaet(fehler))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(request, ziel=zurueck, entwurf_leer=leer)


@router.post("/{zeit_id}/abschreiben")
def abschreiben(
    zeit_id: str,
    request: Request,
    grund: str = Form(""),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Stillschweigend verschenkte Arbeit: der Kunde erfaehrt davon nichts.

    Bewusst von KULANZ getrennt, weil die Auswertung beide unterscheidet --
    sichtbar verschenkt gegen unsichtbar verschenkt.

    Ein zweites Abschreiben derselben Zeit (Doppelklick, zweiter Tab) ueberschrieb
    vorher still den Grund und schrieb einen zweiten Journaleintrag. Jetzt
    meldet es "bereits abgeschrieben" und laesst den ersten Grund stehen.
    """
    if not grund.strip():
        return _zurueck(request, ziel=zurueck, fehler="Abschreiben braucht einen Grund.")
    try:
        eintrag = _in_transaktion(conn, _ganzzahl(zeit_id))
        if eintrag["abgeschrieben_am"] is not None:
            raise _Abbruch(
                f"Diese Zeit ist bereits abgeschrieben (Grund: "
                f"{eintrag['abgeschrieben_grund'] or '—'})."
            )
        if eintrag["laeuft"]:
            raise _Abbruch(TIMER_LAEUFT_MELDUNG)
        if problem := _sperre(eintrag):
            # Vorher lief das in den Trigger und endete als HTTP 500.
            raise _Abbruch(problem)
        entwurf = _entwurf_dahinter(eintrag)
        if entwurf is not None:
            # Abgeschriebene Arbeit erscheint auf keiner Rechnung -- die Zeit
            # muss also zuerst aus dem Entwurf heraus.
            conn.execute("UPDATE zeiteintrag SET rechnung_id = NULL WHERE id = ?",
                         (eintrag["id"],))
        _aktualisiere(
            conn,
            "UPDATE zeiteintrag SET abgeschrieben_am = ?, abgeschrieben_grund = ?,"
            " satz_cent_snapshot = ?, geaendert_am = ?, version = version + 1"
            " WHERE id = ? AND abgeschrieben_am IS NULL",
            (
                db.heute(), grund.strip(),
                eintrag["satz_cent_snapshot"]
                or kundenlogik.satz_cent(conn, eintrag["kunde_id"]),
                db.jetzt_utc(), eintrag["id"],
            ),
        )
        db.protokolliere(conn, "ABSCHREIBEN", "zeiteintrag", eintrag["id"],
                         begruendung=grund.strip())
        if entwurf is not None:
            fs.positionen_neu(conn, entwurf)
        leer = _entwurf_ohne_zeiten(conn, entwurf)
        conn.execute("COMMIT")
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=_integritaet(fehler))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(request, ziel=zurueck, entwurf_leer=leer)


@router.post("/{zeit_id}/zuruecknehmen")
def zuruecknehmen(
    zeit_id: str,
    request: Request,
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Hebt Abschreiben auf und stellt den Eintrag wieder zur Abrechnung."""
    try:
        # Vorher traf das UPDATE fuer eine unbekannte Zeit keine Zeile -- und
        # das Journal bekam trotzdem einen Eintrag fuer eine Zeit, die es nie gab.
        eintrag = _in_transaktion(conn, _ganzzahl(zeit_id))
        if eintrag["abgeschrieben_am"] is None:
            raise _Abbruch("Diese Zeit ist nicht abgeschrieben.")
        _aktualisiere(
            conn,
            "UPDATE zeiteintrag SET abgeschrieben_am = NULL, abgeschrieben_grund = NULL,"
            " geaendert_am = ?, version = version + 1"
            " WHERE id = ? AND abgeschrieben_am IS NOT NULL",
            (db.jetzt_utc(), eintrag["id"]),
        )
        db.protokolliere(conn, "ABSCHREIBEN_ZURUECK", "zeiteintrag", eintrag["id"],
                         wert_alt=eintrag["abgeschrieben_grund"])
        conn.execute("COMMIT")
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(request, ziel=zurueck)


@router.post("/{zeit_id}/dauer")
def dauer_aendern(
    zeit_id: str,
    request: Request,
    dauer: str = Form(""),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Korrigiert die Dauer einer Zeit, solange sie auf keiner festen Rechnung liegt.

    Bisher liess sich eine Dauer nur durch Loeschen und Neuerfassen
    korrigieren -- obwohl Banner und Rueckmeldung nach einem vergessenen
    Timer "Dauer prüfen" verlangten. Gelesen wird wie beim Erfassen (1:45,
    1h45, 1,75, 105 ...), mit derselben 24-Stunden-Grenze.

    Liegt die Zeit in einem Entwurf, entstehen dessen Positionen neu (und die
    Rechnung bekommt eine neue Version), sonst stuende die alte Dauer weiter
    auf dem Entwurf. Auf einer festgeschriebenen Rechnung bleibt die Dauer:
    Korrektur nur ueber Storno.

    Hat die Zeit einen Beginn (Timer), wandert das Ende mit: Beginn plus neue
    Dauer, im selben UPDATE. Vorher blieb die alte Endzeit stehen -- nach der
    Korrektur eines vergessenen Timers auf 8:00 zeigten Excel-Export und
    Sicherung „Beginn 00:00:01, Ende 20:50:40, Dauer 480 Minuten“.
    """
    try:
        minuten = logik.parse_dauer(dauer)
    except logik.DauerUngueltig as fehler:
        return _zurueck(request, ziel=zurueck, fehler=str(fehler))
    try:
        eintrag = _in_transaktion(conn, _ganzzahl(zeit_id))
        if eintrag["laeuft"]:
            raise _Abbruch(TIMER_LAEUFT_MELDUNG)
        if problem := _sperre(eintrag):
            raise _Abbruch(problem)
        alt = int(eintrag["dauer_minuten"])
        if alt == minuten:
            conn.execute("ROLLBACK")
            return _zurueck(
                request, ziel=zurueck,
                hinweis=f"Die Dauer ist bereits {logik.formatiere_dauer(minuten)}.",
            )
        # Ohne Beginn (nachtraeglich erfasst) gibt es kein Ende zu rechnen;
        # ein unlesbarer Beginn laesst das Ende, wie es ist. Mit Pausen gilt
        # "Ende = Beginn + Dauer" nicht: das Ende bleibt das des letzten
        # Abschnitts, und die Abschnitte bleiben als Originalaufzeichnung
        # stehen -- die Liste zeigt, dass die Dauer davon abweicht.
        if len(logik.lade_abschnitte(conn, eintrag["id"])) > 1:
            ende = eintrag["end_zeit"]
        else:
            ende = logik.end_zeit(eintrag["start_zeit"], minuten) or eintrag["end_zeit"]
        _aktualisiere(
            conn,
            "UPDATE zeiteintrag SET dauer_minuten = ?, end_zeit = ?, geaendert_am = ?,"
            " version = version + 1 WHERE id = ? AND version = ?",
            (minuten, ende, db.jetzt_utc(), eintrag["id"], eintrag["version"]),
        )
        db.protokolliere(conn, "AENDERN", "zeiteintrag", eintrag["id"], feld="dauer_minuten",
                         wert_alt=alt, wert_neu=minuten)
        entwurf = _entwurf_dahinter(eintrag)
        if entwurf is not None:
            fs.positionen_neu(conn, entwurf)
        conn.execute("COMMIT")
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=_integritaet(fehler))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(
        request, ziel=zurueck,
        erfolg=(
            f"Dauer geändert: {logik.formatiere_dauer(alt)} → "
            f"{logik.formatiere_dauer(minuten)} ({eintrag['titel']})."
        ),
    )


# ----------------------------------------------------------- Text aendern

@router.post("/{zeit_id}/bearbeiten")
def bearbeiten(
    zeit_id: str,
    request: Request,
    titel: str | None = Form(None),
    beschreibung: str | None = Form(None),
    version: str = Form(""),
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Aendert Titel und Beschreibung einer Zeit -- solange sie auf keiner festen Rechnung liegt.

    Was man getan hat, weiss man oft erst hinterher; vorher liess sich eine
    Beschreibung nur durch Loeschen und Neuerfassen nachtragen. Ein Feld,
    das das Formular nicht sendet, bleibt, wie es ist (das Timer-Banner
    sendet nur die Beschreibung).

    Liegt die Zeit in einem ENTWURF, entstehen dessen Positionen neu -- der
    Untertext der Position und bei „pro Titel“ ihre Bezeichnung kommen aus
    der Zeit --, und die Rechnung bekommt eine neue Version, damit ein offenes
    Festschreib-Formular den alten Stand nicht festschreibt. Auf einer
    festgeschriebenen Rechnung nie: der Einzelnachweis wird aus den Zeiten
    gebaut, und ein nachgezogenes PDF saehe anders aus als das versandte
    (Trigger ``zeit_gesperrt``). Jede Aenderung steht mit altem und neuem
    Wert im Journal.
    """
    eingabe_text = {"id": zeit_id, "titel": titel or "", "beschreibung": beschreibung or ""}

    def abgewiesen(text: str) -> HTMLResponse:
        return _abgewiesen(request, conn, text, eingabe_text=eingabe_text, ziel=zurueck)

    neuer_titel = None if titel is None else titel.strip()
    if neuer_titel == "":
        return abgewiesen("Der Titel fehlt.")
    try:
        text = _beschreibung_lesen(beschreibung)
    except logik.BeschreibungUngueltig as fehler:
        return abgewiesen(str(fehler))
    try:
        eintrag = _in_transaktion(conn, _ganzzahl(zeit_id))
        if problem := _sperre(eintrag):
            raise _Abbruch(problem)
        gesehen = _ganzzahl(version)
        if gesehen is not None and gesehen != eintrag["version"]:
            # Zweiter Tab oder inzwischen gestoppt: nicht ueber einen Stand
            # schreiben, den der Nutzer nie gesehen hat.
            raise _Abbruch(GEAENDERT_MELDUNG)
        geaendert = []
        if neuer_titel is not None and neuer_titel != eintrag["titel"]:
            _aktualisiere(
                conn,
                "UPDATE zeiteintrag SET titel = ?, geaendert_am = ?, version = version + 1"
                " WHERE id = ? AND version = ?",
                (neuer_titel, db.jetzt_utc(), eintrag["id"], eintrag["version"]),
            )
            db.protokolliere(conn, "AENDERN", "zeiteintrag", eintrag["id"], feld="titel",
                             wert_alt=eintrag["titel"], wert_neu=neuer_titel)
            _merke_titel(conn, neuer_titel)
            geaendert.append("Titel")
            eintrag = _lade(conn, eintrag["id"])
        if _beschreibung_uebernehmen(conn, eintrag, text):
            geaendert.append("Beschreibung")
        if not geaendert:
            raise _Hinweis("Nichts geändert.")
        entwurf = _entwurf_dahinter(eintrag)
        if entwurf is not None:
            fs.positionen_neu(conn, entwurf)
        conn.execute("COMMIT")
    except _Hinweis as nichts:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, hinweis=nichts.meldung)
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=_integritaet(fehler))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    erfolg = f"{' und '.join(geaendert)} gespeichert ({neuer_titel or eintrag['titel']})."
    if entwurf is not None:
        erfolg += f" Der Entwurf (Rechnung {entwurf}) ist neu berechnet."
    return _zurueck(request, ziel=zurueck, erfolg=erfolg)


# ------------------------------------------------------ Freigeben, Loeschen

@router.post("/{zeit_id}/freigeben")
def freigeben(
    zeit_id: str,
    request: Request,
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Loest eine Zeit von einer stornierten Rechnung, auf der sie gebunden blieb.

    Beim Storno kann der Nutzer die Zeiten gesperrt lassen. Danach haengen sie
    an einer Rechnung, die nichts mehr abrechnet: nicht neu abrechenbar, nicht
    abschreibbar, nicht loeschbar. Dieser Weg hier ist derselbe wie die Freigabe
    beim Storno, nur nachtraeglich und je Eintrag -- der Trigger
    ``zeit_gesperrt`` laesst genau diese Aenderung zu.
    """
    try:
        eintrag = _in_transaktion(conn, _ganzzahl(zeit_id))
        if eintrag["rechnung_status"] != "STORNIERT":
            raise _Abbruch("Freigeben geht nur bei Zeiten auf einer stornierten Rechnung.")
        _aktualisiere(
            conn,
            "UPDATE zeiteintrag SET rechnung_id = NULL, freigegeben_aus_rechnung_id = ?,"
            " geaendert_am = ?, version = version + 1 WHERE id = ? AND rechnung_id = ?",
            (eintrag["rechnung_id"], db.jetzt_utc(), eintrag["id"], eintrag["rechnung_id"]),
        )
        db.protokolliere(conn, "FREIGEBEN", "zeiteintrag", eintrag["id"], feld="rechnung_id",
                         wert_alt=eintrag["rechnung_id"], wert_neu=None,
                         begruendung=f"nach Storno von {eintrag['rechnung_nummer']} freigegeben")
        conn.execute("COMMIT")
    except _Abbruch as abbruch:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=abbruch.meldung)
    except sqlite3.IntegrityError as fehler:
        conn.execute("ROLLBACK")
        return _zurueck(request, ziel=zurueck, fehler=_integritaet(fehler))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(request, ziel=zurueck)


def _lag_auf_rechnung(conn: sqlite3.Connection, eintrag: sqlite3.Row) -> str | None:
    """Die Nummer der (stornierten) Rechnung, auf der diese Zeit einmal lag.

    ``position_zeiteintrag`` verweist mit einem Fremdschluessel auf die Zeit;
    Positionen einer Rechnung sind per Trigger unloeschbar. Ein DELETE lief
    deshalb in den Fremdschluessel und endete als HTTP 500.
    """
    zeile = conn.execute(
        """
        SELECT r.nummer FROM position_zeiteintrag pz
          JOIN rechnungsposition p ON p.id = pz.position_id
          JOIN rechnung r          ON r.id = p.rechnung_id
         WHERE pz.zeiteintrag_id = ?
         ORDER BY r.id DESC LIMIT 1
        """,
        (eintrag["id"],),
    ).fetchone()
    if zeile is not None:
        return zeile["nummer"]
    herkunft = eintrag["freigegeben_aus_rechnung_id"]
    if herkunft is not None:
        alt = conn.execute("SELECT nummer FROM rechnung WHERE id = ?", (herkunft,)).fetchone()
        return (alt["nummer"] if alt else None) or f"Rechnung {herkunft}"
    return None


@router.post("/{zeit_id}/loeschen")
def loeschen(
    zeit_id: str,
    request: Request,
    zurueck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Loescht einen Zeiteintrag -- nur solange er nie auf einer Rechnung lag.

    Liegt er auf einem festgeschriebenen Beleg, ist er Teil der Buchfuehrung und
    wird abgeschrieben, nicht geloescht. Das gilt auch nach einem Storno: die
    Aufzeichnung "diese Stunde lag auf RE-2026-0012" bleibt. Einen laufenden
    Timer zu loeschen ist dagegen erlaubt -- das ist der Abbruch.
    """
    eintrag = _lade(conn, _ganzzahl(zeit_id))
    if eintrag is None:
        return _zurueck(request, ziel=zurueck, fehler=KEIN_EINTRAG)
    if eintrag["rechnung_status"] == "ENTWURF":
        return _zurueck(
            request, ziel=zurueck,
            fehler=(
                f"Der Eintrag liegt auf dem Entwurf (Rechnung {eintrag['rechnung_id']}) "
                f"— erst verwerfen oder abschreiben."
            ),
        )
    if eintrag["rechnung_id"] is not None:
        return _zurueck(
            request, ziel=zurueck,
            fehler=(
                f"Der Eintrag liegt auf der Rechnung {eintrag['rechnung_nummer']} "
                f"und kann nicht gelöscht werden."
            ),
        )
    if (nummer := _lag_auf_rechnung(conn, eintrag)) is not None:
        return _zurueck(
            request, ziel=zurueck,
            fehler=(
                f"Diese Zeit lag auf {nummer} und bleibt als Aufzeichnung erhalten "
                f"— bitte abschreiben statt löschen."
            ),
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        db.protokolliere(conn, "LOESCHEN", "zeiteintrag", eintrag["id"],
                         wert_alt={"minuten": eintrag["dauer_minuten"], "titel": eintrag["titel"]})
        if conn.execute("DELETE FROM zeiteintrag WHERE id = ? AND rechnung_id IS NULL",
                        (eintrag["id"],)).rowcount != 1:
            # Inzwischen geloescht oder in einen Entwurf geholt (zweiter Tab):
            # zurueckrollen, damit auch der Journaleintrag verfaellt.
            conn.execute("ROLLBACK")
            return _zurueck(request, ziel=zurueck, fehler=GEAENDERT_MELDUNG)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        # Sicherheitsnetz fuer einen Verweis, den die Pruefung oben nicht kennt.
        conn.execute("ROLLBACK")
        return _zurueck(
            request, ziel=zurueck,
            fehler="Diese Zeit wird noch von einer Rechnung referenziert und bleibt als "
                   "Aufzeichnung erhalten — bitte abschreiben statt löschen.",
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _zurueck(request, ziel=zurueck)

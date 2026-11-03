"""Eingangsbelege: hochladen, pruefen, buchen.

Die Pruefmaske ist der Kern dieser Routen, nicht der Upload. Sie zeigt **je
Feld**, woher der Wert kommt -- Stufe, Fundstelle, und ob er ohne Rueckfrage
uebernommen werden durfte. Das ist nicht Kosmetik: nach GoBD Rz. 130 ist das
bestaetigte Ergebnis selbst aufbewahrungspflichtig. Wer bestaetigt, haftet fuer
die Zahl -- und kann das nur, wenn er sieht, ob sie aus einem XML-Feld, aus
einer Textzeile oder aus einer Bilderkennung kommt.

Zwischen Upload und Bestaetigung liegt der **Eingangsordner**. Grund steht in
``pipeline``: ein Beleg braucht Datum und Betrag, und einen Datensatz vorher
anzulegen hiesse, beide zu erfinden.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape as html_text
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from app import db, pfade
from app.filter import datum_de
from app.logik import kategorien, kurse, pauschalen
from app.logik.beleg import (
    abholen,
    anhaengen,
    eigenbeleg,
    email_leser,
    jahresordner,
    loeschen,
    pipeline,
    profil,
    textdienst,
    umrechnung,
    vision_mac,
)
from app.logik.beleg import bezahlt as bezahlvorgabe
from app.logik.beleg.eigenbeleg import ohne_steuerzeichen
from app.logik.beleg.ergebnis import BelegFehler
from app.logik.eingabe import datum as lies_datum
from app.logik.eingabe import ganzzahl, kennung
from app.logik.rundung import euro
from app.web import VORLAGEN, betrieb_oder_none, dateikopf, filterzahl, hole_conn

router = APIRouter()

#: Das Cookie, mit dem der Hinweis "als Ordner für <Jahr> führen" ausbleibt
#: (gesetzt von app/routen/belegordner.py).
HINWEIS_COOKIE = "eingangsordner_je_jahr"

ZAHLUNGSARTEN = ("UEBERWEISUNG", "KARTE", "BAR", "PAYPAL", "SONSTIGES")


def _datenordner(request: Request) -> Path:
    pfad = request.app.state.datenpfad
    return Path(pfad).parent if pfad else db.standard_datenordner()


def _meldung(
    ziel: str, text: str, art: str = "fehler", *, anker: str = "", beleg: int | None = None
) -> RedirectResponse:
    """Weiterleitung mit Meldung. ``anker`` springt auf der Zielseite an eine
    Stelle -- er gehoert HINTER die Abfrage, sonst kaeme die Meldung nie an.

    ``beleg`` heisst: die Meldung gehoert zu einer Aktion an DIESEM Beleg der
    Liste (Kurs pruefen, Kartenbetrag, Zahlung, "Stimmt so"). Die Liste zeigt
    sie dann direkt ueber seiner Zeile, mit aufgeklappten Einzelheiten, und
    der Sprung geht auf die Meldung. Vorher sprang er auf die Zeile, die
    Meldung stand oben auf der Seite, weit ausserhalb des Fensters -- fuer den
    Nutzer passierte beim Klick scheinbar nichts.
    """
    abfrage = f"{art}={quote(text)}"
    if beleg is not None:
        abfrage += f"&beleg={int(beleg)}"
        anker = "meldung"
    trenner = "&" if "?" in ziel else "?"
    sprung = f"#{anker}" if anker else ""
    return RedirectResponse(f"{ziel}{trenner}{abfrage}{sprung}", status_code=303)


def _url(name: str) -> str:
    """Ein Dateiname als URL-Bestandteil. Ein "#" im Namen schnitt den Link
    sonst ab, und die Maske meldete eine Datei, die es gab, als fehlend."""
    return urllib.parse.quote(name, safe="")


#: Hoechstens so viele Belege zeigt die Liste. Die Summe gilt trotzdem fuer
#: ALLE gefilterten Belege -- sie hat eine eigene Abfrage.
LISTE_MAX = 400


# -------------------------------------------------------------------- Liste



@router.get("/belege", response_class=HTMLResponse)
def liste(
    request: Request,
    jahr: str = "",
    kategorie: str = "",
    offen: str = "",
    geloescht: str = "",
    ohne_datei: str = "",
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    return liste_seite(request, conn, jahr=jahr, kategorie=kategorie, offen=offen,
                       geloescht=geloescht, ohne_datei=ohne_datei)


def liste_seite(
    request: Request,
    conn: sqlite3.Connection,
    *,
    jahr: str = "",
    kategorie: str = "",
    offen: str = "",
    geloescht: str = "",
    ohne_datei: str = "",
    ordner_fehler: str | None = None,
    ordner_eingabe: dict[str, str] | None = None,
    karte: dict[str, object] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Die Belegseite. Nach einer abgewiesenen Einstellung des Eingangsordners
    mit den Eingaben des Nutzers neu gezeigt (422) -- eine Weiterleitung
    verwarf den getippten Pfad, und gerade der ist lang. Ebenso nach einem
    Kartenbetrag, der stark vom umgerechneten Wert abweicht: ``karte`` traegt
    Beleg, Meldung und den getippten Betrag, die Zeile zeigt die Rueckfrage.

    Geloeschte Belege (app/logik/beleg/loeschen.py) zeigt die Liste nur mit
    ``geloescht=1`` -- gekennzeichnet und ohne Einfluss auf die Summe."""
    jahr, kategorie, offen = filterzahl(jahr), filterzahl(kategorie), filterzahl(offen)
    mit_geloeschten = geloescht == "1"

    bedingungen: list[str] = []
    werte: list[object] = []
    if jahr:
        bedingungen.append("b.belegdatum LIKE ?")
        werte.append(f"{jahr}-%")
    if kategorie:
        bedingungen.append("b.kategorie_id = ?")
        werte.append(kategorie)
    # Wie viele geloeschte der Filter ausblendet -- fuer den Hinweis.
    ausgeblendet = 0 if mit_geloeschten else conn.execute(
        "SELECT COUNT(*) AS n FROM beleg b WHERE b.geloescht_am IS NOT NULL"
        + "".join(f" AND {b}" for b in bedingungen), werte).fetchone()["n"]
    if offen:
        # Die Pruefliste "nicht zugeordnet", die spaeter den Jahresabschluss
        # sperrt: ein Beleg ohne Kategorie landet in keiner Zeile der EUeR.
        # Ein Anlagegut, das im Verzeichnis steht, ist geklaert: es geht ueber
        # die AfA in die EUeR, nicht ueber eine Kategorie. Ein geloeschter
        # Beleg ist nie ungeklaert.
        bedingungen.append(
            "(b.kategorie_id IS NULL OR b.geprueft_am IS NULL)"
            " AND NOT (b.ist_anlagegut = 1"
            " AND EXISTS (SELECT 1 FROM v_anlagegut_gueltig g WHERE g.beleg_id = b.id))"
            " AND b.geloescht_am IS NULL"
        )
    if not mit_geloeschten:
        bedingungen.append("b.geloescht_am IS NULL")
    nur_ohne_datei = ohne_datei == "1"
    if nur_ohne_datei:
        # Aus WISO uebernommen, das Original fehlt noch -- zum Anhaengen
        # (app/logik/beleg/anhaengen.py).
        bedingungen.append(
            "EXISTS (SELECT 1 FROM wiso_buchung w WHERE w.beleg_id = b.id"
            " AND w.status = 'AKTIV' AND w.verknuepft = 0)"
            " AND NOT EXISTS (SELECT 1 FROM belegdatei d WHERE d.beleg_id = b.id"
            " AND d.rolle = 'ORIGINAL')")
    wo = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""

    belege = conn.execute(
        f"""
        SELECT b.*, l.name AS lieferant, k.bezeichnung AS kategorie, k.kennzahl,
               (SELECT COUNT(*) FROM belegdatei d WHERE d.beleg_id = b.id) AS dateien,
               (SELECT COUNT(*) FROM belegdatei d WHERE d.beleg_id = b.id
                   AND d.rolle = 'EMAIL') AS mails,
               (SELECT MIN(z.datum) FROM zahlung z WHERE z.beleg_id = b.id) AS bezahlt_am,
               -- Zahltag aendern und Zahlung zuruecknehmen gibt es nur bei
               -- genau einer Zahlung (app/logik/beleg/belegzahlung.py).
               (SELECT COUNT(*) FROM zahlung z WHERE z.beleg_id = b.id) AS zahlungen,
               -- Ein Anlagegut geht ueber die AfA in die EUeR, nicht ueber die
               -- Kategorie des Belegs. Die Liste zeigt deshalb, ob es schon im
               -- Anlagenverzeichnis steht, statt "nicht zugeordnet".
               EXISTS (SELECT 1 FROM v_anlagegut_gueltig a WHERE a.beleg_id = b.id)
                   AS im_verzeichnis,
               -- Automatisch gebucht (Eingangsordner oder "Alle prüfen und
               -- buchen"), ohne dass jemand die Felder bestaetigt hat: das
               -- Verfahren traegt den Vermerk, jeder endet mit der Kennung.
               EXISTS (SELECT 1 FROM extraktion e WHERE e.beleg_id = b.id
                         AND instr(e.verfahren, ?) > 0) AS automatisch,
               -- Aus WISO uebernommen (nicht nur verknuepft): die Nummer der
               -- Buchung, fuer "aus WISO übernommen · Datei fehlt".
               (SELECT w.buchungsnummer FROM wiso_buchung w WHERE w.beleg_id = b.id
                   AND w.status = 'AKTIV' AND w.verknuepft = 0 LIMIT 1) AS wiso_nummer
          FROM beleg b
          LEFT JOIN lieferant l      ON l.id = b.lieferant_id
          LEFT JOIN euer_kategorie k ON k.id = b.kategorie_id
          {wo}
         ORDER BY b.belegdatum DESC, b.id DESC
         LIMIT {LISTE_MAX}
        """,
        [pipeline.AUTOMATISCH_KENNUNG, *werte],
    ).fetchall()
    # Summe und Anzahl ueber ALLE gefilterten Belege, nicht ueber die
    # angezeigten: ab dem 401. Beleg war "Betrieblich gesamt" sonst zu
    # niedrig, und nichts sagte, dass Zeilen fehlen. Die Summe nur ueber
    # gueltige -- auch wenn die geloeschten mit in der Liste stehen.
    gesamt = conn.execute(
        f"SELECT COUNT(*) AS anzahl, COALESCE(SUM(CASE WHEN b.geloescht_am IS NULL"
        f" THEN b.betrag_betrieblich_cent END), 0) AS summe"
        f" FROM beleg b {wo}",
        werte,
    ).fetchone()

    datenordner = _datenordner(request)
    # Gehoert die Meldung zu einem Beleg der Liste (siehe _meldung), steht sie
    # ueber seiner Zeile; sonst -- gefiltert, ausserhalb der ersten
    # LISTE_MAX -- wie jede andere oben.
    meldung_zu = filterzahl(request.query_params.get("beleg", ""))
    if meldung_zu is not None and not any(b["id"] == meldung_zu for b in belege):
        meldung_zu = None
    reihen: dict[str, kurse.Tabelle | None] = {}  # je Waehrung, fuer ezb_moeglich
    eingang = _eingangsliste(datenordner)
    # Nach dem Buchen in der Pruefmaske ("weiter=1"): die naechste Datei im
    # Eingang, die sich buchen laesst, und wie viele es noch sind.
    buchbar = [d for d in eingang if d["buchbar"]]
    weiter = ({"name": buchbar[0]["name"], "anzahl": len(buchbar)}
              if request.query_params.get("weiter") == "1" and buchbar else None)
    return VORLAGEN.TemplateResponse(
        request,
        "belege.html",
        {
            "belege": belege,
            "meldung_zu": meldung_zu,
            "anzahl_gesamt": gesamt["anzahl"],
            "jahre": [z["jahr"] for z in conn.execute(
                "SELECT DISTINCT substr(belegdatum, 1, 4) AS jahr FROM beleg"
                + ("" if mit_geloeschten else " WHERE geloescht_am IS NULL")
                + " ORDER BY jahr DESC")],
            "kategorien": kategorien.katalog(conn, bereich=kategorien.BEREICH_AUSGABEN),
            "filter": {"jahr": jahr, "kategorie": kategorie, "offen": offen,
                       "geloescht": mit_geloeschten, "ohne_datei": nur_ohne_datei},
            # Loeschen, Wiederherstellen, Zahlung korrigieren
            # (app/routen/belegkorrektur.py): die Rueckfrage zum Anlagegut
            # zaehlt dessen AfA-Jahre auf, die Vorschlaege fuer den Grund.
            "ausgeblendet": ausgeblendet,
            "anlagegueter": loeschen.anlagegueter_zu(
                conn, [b["id"] for b in belege if b["geloescht_am"] is None
                       and b["im_verzeichnis"]]),
            "loeschgruende": loeschen.GRUENDE,
            "laufendes_jahr": int(db.heute()[:4]),
            "summe": gesamt["summe"],
            "eingang": eingang,
            "eingang_namen": {d["name"] for d in eingang},
            # Buchungen ohne Datei (Belege, Einnahmen), zu denen eine Datei im
            # Eingang passen koennte -- fuer die Zeile "… · Zuordnen"
            # (app/logik/beleg/anhaengen.py).
            "ohne_datei_anzahl": anhaengen.ohne_datei_anzahl(conn) if eingang else (0, 0),
            # Warum eine Datei wartet -- aus der letzten Vorschau von "Alle
            # prüfen und buchen" (app/routen/eingang.py), nur im Speicher.
            "eingang_gruende": getattr(request.app.state, "eingang_gruende", None) or {},
            "weiter": weiter,
            # Das Ergebnis von "Alle prüfen und buchen" -- nur gleich danach.
            "sammel": (getattr(request.app.state, "sammelbuchung", None)
                       if request.query_params.get("sammel") == "1" else None),
            "bezahlt_vorgabe": bezahlvorgabe.vorgabe(conn),
            "vision": vision_mac.ist_verfuegbar(),
            "ordner": _eingangsordner(request, conn, datenordner),
            "ordner_fehler": ordner_fehler,
            "ordner_eingabe": ordner_eingabe or {},
            # Je Fremdwaehrungsbeleg die Umrechnung zum Nachpruefen: Waehrung,
            # Fremdbetrag, Kurs, Kursdatum, Quelle, Abrufzeit, SHA-256.
            "umrechnungen": {b["id"]: umrechnung.anzeige(b) for b in belege
                             if b["waehrung"] != "EUR"},
            # Belege mit Kurs von Hand, fuer die die gespeicherte Tabelle doch
            # einen EZB-Kurs hat: "Zum EZB-Kurs umrechnen" (nichts wird geladen).
            "ezb_moeglich": {b["id"] for b in belege
                             if b["waehrung"] != "EUR" and b["kurs_quelle"] == "MANUELL"
                             and b["betrag_karte_cent"] is None
                             and umrechnung.ezb_kurs_vorhanden(conn, datenordner, b,
                                                               reihen=reihen)},
            "karte": karte or {},
            # Stand der EZB-Kurstabelle, und ob das Auffrischen zuletzt scheiterte.
            "kurstabelle": kurse.stand(datenordner),
            "kurse_fehler": kurse.letzter_fehlschlag(datenordner),
            # Als Fortsetzung von "Das letzte Auffrischen scheiterte:" -- ohne
            # den zweiten Vorsatz "Die Kurstabelle ließ sich nicht laden:".
            "kurse_ursache": (kurse.ursache(kurse.letzter_fehlschlag(datenordner) or "")
                              or None),
            "fehler": request.query_params.get("fehler"),
            "erfolg": request.query_params.get("erfolg"),
            "hinweis": request.query_params.get("hinweis"),
            # Erledigt, aber mit Folgen fuer ein Vorjahr (Loeschen, Zahltag).
            "warnung": request.query_params.get("warnung"),
        },
        status_code=status_code,
    )


def _eingangsordner(
    request: Request, conn: sqlite3.Connection, datenordner: Path
) -> dict[str, object]:
    """Einstellung, Vorschlag und letzter Lauf des Eingangsordners.

    ``im_eingang`` sind die Dateinamen, die noch im Eingang liegen: eine
    wartende Datei aus dem letzten Lauf, die inzwischen gebucht oder
    weggeworfen wurde, bekommt keinen Link mehr auf eine Pruefmaske, die es
    nicht mehr gibt.
    """
    zeile = conn.execute(
        "SELECT belegordner_pfad, belegordner_automatisch FROM betrieb WHERE id = 1"
    ).fetchone()
    vorschlag = abholen.vorschlag_ordner()
    pfad = (zeile["belegordner_pfad"] or None) if zeile else None
    # Ordner je Jahr (app/logik/beleg/jahresordner.py), die Jahre absteigend.
    heute = date.today()
    jahre = jahresordner.jahre(conn)
    # Die Anleitung fuer das iPhone passt nur zu einem Ordner in iCloud Drive
    # -- und nennt den Weg, den die Dateien-App zeigt: den Ordner des
    # laufenden Jahres, sonst den fuer alle Jahre. Ohne Ordner gilt der
    # Vorschlag ("Steuer-Tool Belege").
    haupt = jahre.get(heute.year) or pfad
    icloud_weg = (abholen.icloud_teile(Path(haupt)) if haupt
                  else [abholen.VORSCHLAG_NAME])
    # Fuer das Formular "Ordner für ein Jahr": Vorjahr, laufendes und naechstes
    # Jahr; vorausgewaehlt das erste ohne Ordner (ab Dezember das naechste).
    optionen = [(heute.year - 1, "Vorjahr"), (heute.year, "laufendes Jahr"),
                (heute.year + 1, "nächstes Jahr")]
    # Aeltere Jahre, fuer die schon ein Ordner hinterlegt ist, bleiben aenderbar.
    optionen += [(j, "hinterlegt") for j in sorted(jahre, reverse=True)
                 if j < heute.year - 1]
    frei = [j for j, _ in optionen if j not in jahre]
    gewaehlt = (heute.year + 1 if heute.month == 12 and heute.year + 1 not in jahre
                else heute.year if heute.year in frei else (frei[-1] if frei else heute.year))
    jahr_vorschlag = jahresordner.vorschlag(conn, gewaehlt)
    return {
        "pfad": pfad,
        "jahre": [{"jahr": j, "pfad": jahre[j], "anzeige": abholen.anzeigename(jahre[j])}
                  for j in sorted(jahre, reverse=True)],
        "irgendeiner": bool(pfad or jahre),
        "jahr_optionen": [{"jahr": j, "text": text, "vergeben": j in jahre}
                          for j, text in optionen],
        "jahr_gewaehlt": gewaehlt,
        "jahr_vorschlag": ({"jahr": gewaehlt, "anzeige": abholen.anzeigename(jahr_vorschlag)}
                           if jahr_vorschlag else None),
        "jahreswechsel": jahresordner.jahreswechsel(conn, heute=heute),
        # Der bisherige Ordner bleibt der fuer alle Jahre -- einmal gefragt,
        # ob er der eines Jahres sein soll (bis "Nein" oder ein Jahresordner).
        "umwidmen": (heute.year if pfad and not jahre
                     and request.cookies.get(HINWEIS_COOKIE) != "aus" else None),
        # Fuer Menschen: "iCloud Drive › Steuer-Tool Belege" statt
        # ".../Library/Mobile Documents/com~apple~CloudDocs/...".
        "anzeige": abholen.anzeigename(pfad) if pfad and abholen.icloud_teile(Path(pfad))
                   else None,
        "icloud_weg": icloud_weg,
        "automatisch": bool(zeile["belegordner_automatisch"]) if zeile else True,
        "vorschlag": str(vorschlag) if vorschlag else None,
        # "Vorschlag übernehmen" auch bei gespeichertem Ordner -- solange er
        # ein anderer ist als der vorgeschlagene.
        "vorschlag_anbieten": bool(vorschlag) and not (
            pfad and abholen.derselbe_ordner(Path(pfad), vorschlag)),
        "uebernommen": abholen.UEBERNOMMEN,
        "nicht_uebernommen": abholen.NICHT_UEBERNOMMEN,
        "stand": getattr(request.app.state, "belegordner", None),
        "ergebnis": abholen.aktuelles_ergebnis(
            getattr(request.app.state, "belegordner", None), conn),
        "im_eingang": {p.name for p in pipeline.liegen_geblieben(datenordner)},
    }


def _eingangsliste(datenordner: Path) -> list[dict[str, object]]:
    """Die Dateien im Eingang fuer die Liste.

    Liegt dort etwas, das kein Belegformat ist oder dessen Inhalt nicht zur
    Endung passt (von Hand hineingelegt, am Upload vorbei), wird es gezeigt --
    aber nicht zum Buchen angeboten. Eine Datei, die waehrenddessen gebucht
    und verschoben wird, faellt einfach heraus.
    """
    liste = []
    for pfad in pipeline.liegen_geblieben(datenordner):
        try:
            groesse = pfad.stat().st_size
        except OSError:
            continue
        format_ok = pfad.suffix.lower() in pipeline.ERLAUBT
        passt = pipeline.inhalt_passt(pfad) if format_ok else False
        if passt is None:
            # Zwischen Auflisten und Lesen gebucht oder weggeworfen.
            continue
        liste.append({
            "name": pfad.name,
            "original": pipeline.original_name_von(pfad),
            "groesse": groesse,
            "format_ok": format_ok,
            "buchbar": format_ok and passt,
        })
    return liste


# ------------------------------------------------------------------- Upload

@router.post("/belege/hochladen")
async def hochladen(
    request: Request,
    datei: list[UploadFile] = File(...),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Nimmt Dateien in den Eingang. Eine einzelne fuehrt gleich in die
    Pruefmaske; mehrere landen im Eingang, und die Seite sagt, was davon
    aufgenommen wurde und was nicht (mit Grund je Datei) -- eine Datei, die
    kein Beleg ist, haelt die anderen nicht auf.

    ``async``, weil ``UploadFile.read()`` eine Koroutine ist. Alles danach ist
    synchron; der Upload selbst ist der einzige Teil, der wartet.
    """
    datenordner = _datenordner(request)
    aufgenommen: list[pipeline.Eingang] = []
    abgewiesen: list[str] = []
    for teil in datei:
        name = Path((teil.filename or "beleg.pdf").strip()).name or "beleg.pdf"
        try:
            inhalt = await teil.read()
            aufgenommen.append(pipeline.aufnehmen(inhalt, name, datenordner))
        except BelegFehler as fehler:
            abgewiesen.append(str(fehler) if len(datei) == 1 else f"{name}: {fehler}")
    if len(datei) == 1:
        if abgewiesen:
            return _meldung("/belege", abgewiesen[0])
        return RedirectResponse(f"/belege/pruefen/{_url(aufgenommen[0].pfad.name)}",
                                status_code=303)
    schon = sum(1 for e in aufgenommen if not e.neu)
    text = (f"{_zahlwort(len(aufgenommen), 'Datei', 'Dateien')} in den Eingang aufgenommen"
            + (f" (davon {schon} schon vorher dort)" if schon else "") + ".")
    if not abgewiesen:
        return _meldung("/belege", text, art="erfolg", anker="eingang")
    return _meldung("/belege", (f"{text} " if aufgenommen else "")
                    + f"Nicht aufgenommen: {'; '.join(abgewiesen)}", anker="eingang")


# ---------------------------------------------------------------- Pruefmaske

def _nicht_im_eingang(conn: sqlite3.Connection, dateiname: str) -> RedirectResponse:
    """Die Antwort, wenn eine Datei nicht (mehr) im Eingang liegt.

    Der haeufigste Grund ist harmlos: der Beleg wurde gerade gebucht, und ein
    Doppelklick oder die Zuruecktaste schickt das Formular ein zweites Mal.
    Dann sagt die Meldung, dass er gebucht ist -- und nicht, dass etwas fehlt.
    Der Eingangsname einer hochgeladenen Datei besteht aus den ersten 16
    Zeichen der Pruefsumme; eine von Hand in den Eingang gelegte Datei behaelt
    ihren Namen, und der steht nach dem Buchen in ``belegdatei.original_name``.

    Laeuft die erste Anfrage noch (Datei schon verschoben, Buchung noch nicht
    festgeschrieben), wartet die Sperre, bis sie fertig ist -- sonst saehe
    diese Anfrage den Beleg noch nicht und meldete eine fehlende Datei.
    """
    treffer = re.fullmatch(r"([0-9a-f]{16})\.[A-Za-z0-9]{1,5}", dateiname)
    conn.execute("BEGIN IMMEDIATE")
    try:
        zeile = None
        # Ein gueltiger Beleg vor einem geloeschten: dieselbe Datei kann nach
        # dem Loeschen neu gebucht sein, und gemeint ist dann die neue Buchung.
        if treffer:
            zeile = conn.execute(
                "SELECT d.beleg_id, b.geloescht_am FROM belegdatei d"
                " JOIN beleg b ON b.id = d.beleg_id"
                " WHERE substr(d.sha256, 1, 16) = ? AND d.rolle IN ('ORIGINAL', 'EMAIL')"
                " ORDER BY b.geloescht_am IS NOT NULL, d.id DESC LIMIT 1",
                (treffer.group(1),),
            ).fetchone()
        if zeile is None and dateiname.isprintable():
            zeile = conn.execute(
                "SELECT d.beleg_id, b.geloescht_am FROM belegdatei d"
                " JOIN beleg b ON b.id = d.beleg_id"
                " WHERE d.original_name = ? AND d.rolle IN ('ORIGINAL', 'EMAIL')"
                " ORDER BY b.geloescht_am IS NOT NULL, d.id DESC LIMIT 1",
                (dateiname,),
            ).fetchone()
    finally:
        conn.execute("COMMIT")
    if zeile is not None and zeile["geloescht_am"] is not None:
        return _meldung("/belege", f"Diese Datei war Beleg {zeile['beleg_id']} und ist mit "
                                   "ihm gelöscht — sie liegt nicht im Eingang. Zum neuen "
                                   "Buchen noch einmal hochladen.")
    if zeile is not None:
        return _meldung("/belege", f"Diese Datei wurde bereits als Beleg "
                                   f"{zeile['beleg_id']} gebucht.", art="erfolg")
    if dateiname.isprintable() and len(dateiname) <= 120:
        return _meldung("/belege", f"{dateiname} liegt nicht im Eingang.")
    return _meldung("/belege", "Diese Datei liegt nicht im Eingang.")


def _kein_belegformat(pfad: Path) -> RedirectResponse:
    return _meldung(
        "/belege",
        f"{pfad.name} ist kein Belegformat — wegwerfen oder umbenennen. Möglich sind "
        f"{', '.join(sorted(pipeline.ERLAUBT))}.",
    )


def _inhalt_fremd(pfad: Path) -> RedirectResponse:
    """Eine Eingangsdatei, deren Inhalt nicht zur Endung passt -- von Hand in
    den Eingang gelegt, also an der Pruefung beim Hochladen vorbei."""
    return _meldung("/belege", pipeline.inhalt_fremd(pipeline.original_name_von(pfad),
                                                     pfad.suffix.lower()))


def _eingang(pfad: Path) -> pipeline.Eingang:
    """Die Eingangsdatei mit dem Namen, unter dem sie hochgeladen wurde."""
    return pipeline.Eingang(
        pfad=pfad, original_name=pipeline.original_name_von(pfad),
        sha256=pipeline.pruefsumme(pfad), groesse=pfad.stat().st_size,
    )


def _lies(conn: sqlite3.Connection, eingang: pipeline.Eingang) -> pipeline.Lesung:
    """Liest die Eingangsdatei; ein harter Fehler wird zur manuellen Erfassung.

    Ein harter Fehler der Auslesung -- insbesondere unlesbares XML einer
    E-Rechnung -- gehoert ins Journal: er ist der Nachweis, dass NICHT auf eine
    Bilderkennung zurueckgefallen wurde. Einmal je Datei (Pruefsumme): jedes
    Neuladen der Maske schrieb sonst einen weiteren, unloeschbaren Eintrag.

    Die Textschicht eines PDF liest der Textdienst mit Zeitgrenze, wie beim
    Abholen: ein PDF mit Datenmuell am Dateiende hielt die Maske sonst drei
    Minuten fest. Zu langsam heisst dann: Handeingabe, mit Hinweis.
    """
    try:
        return pipeline.lies(conn, eingang, textleser=textdienst.text_mit_zeitgrenze)
    except BelegFehler as fehler:
        meldung = str(fehler)
        if isinstance(fehler, textdienst.ZuLangsam):
            meldung = f"{meldung}. Bitte die Angaben von Hand aus dem Dokument eintragen."
        schon = conn.execute(
            "SELECT 1 FROM journal WHERE aktion = 'BELEG_AUSLESUNG_FEHLER' AND hash = ?"
            " LIMIT 1", (eingang.sha256,),
        ).fetchone()
        if schon is None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                db.protokolliere(conn, "BELEG_AUSLESUNG_FEHLER", "beleg", None,
                                 wert_neu=eingang.pfad.name, begruendung=str(fehler),
                                 hash_=eingang.sha256)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return pipeline.Lesung(
            eingang=eingang,
            vorschlag=pipeline.Vorschlag("manuell", {}, ()),
            stufen=("manuell",),
            meldungen=(meldung,),
        )


def _vorgaben(conn: sqlite3.Connection, lieferant_id: int | None) -> dict[str, object]:
    """Was der erkannte Lieferant vorgibt: Kategorie, Quote, Reverse Charge,
    Waehrung. Das Schema traegt diese Spalten genau dafuer ("Mobilfunk 70 %");
    ohne sie war der zweite Beleg desselben Lieferanten kein Bestaetigungsklick,
    sondern dieselbe Auswahl wie beim ersten."""
    if not lieferant_id:
        return {}
    zeile = conn.execute("SELECT * FROM lieferant WHERE id = ?", (lieferant_id,)).fetchone()
    if zeile is None:
        return {}
    return {
        "kategorie_id": zeile["standard_kategorie_id"],
        "quote": zeile["quote_default"],
        "reverse_charge": bool(zeile["reverse_charge_default"]),
        "waehrung": zeile["waehrung_default"] or "EUR",
    }


def _maske(
    request: Request,
    conn: sqlite3.Connection,
    dateiname: str,
    lesung: pipeline.Lesung,
    *,
    fehler: str | None = None,
    eingabe: dict[str, str] | None = None,
    aehnlich_bestaetigen: bool = False,
    karte_rueckfrage: bool = False,
    kurs_rueckfrage: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    """Die Pruefmaske. Nach einem abgewiesenen Buchen mit den Eingaben des
    Nutzers neu gezeigt (422) -- eine Weiterleitung hatte Kategorie, Quote und
    jede Korrektur verworfen, und der Nutzer fing von vorn an."""
    katalog = kategorien.katalog(conn, bereich=kategorien.BEREICH_AUSGABEN)
    vorgabe = _vorgaben(conn, lesung.lieferant_id)
    eingabe = eingabe or {}
    # Das Belegdatum, wie es in der Maske steht: fuer "Bezahlt am" (folgt ihm)
    # und den Hinweis am Jahreswechsel.
    datum_fund = lesung.vorschlag.felder.get("belegdatum")
    belegdatum = (eingabe["belegdatum"] if "belegdatum" in eingabe
                  else str(datum_fund.wert) if datum_fund and datum_fund.wert else "")
    return VORLAGEN.TemplateResponse(
        request,
        "beleg_pruefen.html",
        {
            "lesung": lesung,
            "dateiname": dateiname,
            "dateiname_url": _url(dateiname),
            "felder": lesung.vorschlag.felder,
            "kategorien": katalog,
            "anlage_kategorien": [k["id"] for k in katalog
                                  if k["schluessel"] == "HARDWARE_ANLAGE"],
            "lieferanten": conn.execute(
                "SELECT * FROM lieferant WHERE aktiv = 1 ORDER BY name COLLATE DEUTSCH"
            ).fetchall(),
            "vorgabe": vorgabe,
            # Bei Fremdwaehrung gleich der EZB-Vorschlag: Kurs, Kursdatum,
            # Euro-Betrag. Gebucht werden darf auch ohne Kartenbetrag.
            "umrechnung": _umrechnung_vorschlag(request, lesung, vorgabe, eingabe or {}),
            "karte_rueckfrage": karte_rueckfrage,
            "kurs_rueckfrage": kurs_rueckfrage,
            "aehnliche_text": (
                pipeline.aehnliche_als_text(lesung.aehnliche + lesung.aehnliche_andere,
                                            waehrung=lesung.waehrung)
                if lesung.aehnliche or lesung.aehnliche_andere else None),
            "aehnlich_bestaetigen": (aehnlich_bestaetigen or bool(lesung.aehnliche)
                                     or bool(lesung.aehnliche_andere)),
            # Belege und Einnahmen ohne Datei, zu denen diese Datei passt
            # (gleicher Betrag, Datum in der Naehe): anhaengen statt ein
            # zweites Mal buchen (app/logik/beleg/anhaengen.py). Die Maske
            # fragt mit Radio, bei "sicher" ist der Eintrag vorausgewaehlt.
            "zuordnung": anhaengen.einzeln(anhaengen.zur_lesung(conn, lesung)),
            "fenster_tage": anhaengen.FENSTER_TAGE,
            "zahlungsarten": ZAHLUNGSARTEN,
            # Bezahlt als Vorgabe (app/logik/beleg/bezahlt.py): vorausgewaehlt
            # ist "bezahlt am <Belegdatum>", wenn die Einstellung an ist; die
            # Zahlungsart ist die der letzten Zahlung an diesen Lieferanten.
            "zahlung_vorgabe": "bezahlt" if bezahlvorgabe.vorgabe(conn) else "offen",
            "zahlungsart_vorgabe": bezahlvorgabe.zahlungsart(conn, lesung.lieferant_id),
            "belegdatum": belegdatum,
            "jahreswechsel": bezahlvorgabe.am_jahreswechsel(_datum_oder_text(belegdatum)),
            "jahreswechsel_text": bezahlvorgabe.JAHRESWECHSEL,
            # Die Rechnung im Anhang einer Mail, fuer die Ansicht rechts.
            "anhang_url": _anhang_url(dateiname, lesung),
            "reisen": eigenbeleg.reisen(conn, grenze=20),
            "eingabe": eingabe,
            "fehler": fehler or request.query_params.get("fehler"),
        },
        status_code=status_code,
    )


def _anhang_url(dateiname: str, lesung: pipeline.Lesung) -> str | None:
    """Wo die Pruefmaske die Rechnung aus dem Anhang einer Mail zeigt."""
    if lesung.anhang is None:
        return None
    nummer = re.search(r"\.anhang(\d+)", lesung.anhang.pfad.name)
    if nummer is None:
        return None
    return f"/belege/eingang/{_url(dateiname)}/anhang/{nummer.group(1)}"


def _cent_oder_none(roh: str | None) -> int | None:
    """Ein Betrag aus Maske oder Abfrage; Unlesbares ergibt None (nur Anzeige)."""
    from app.logik.kunden import euro_nach_cent

    try:
        return euro_nach_cent(roh)
    except (ArithmeticError, ValueError):
        return None


def _tag_oder_none(roh: str | None) -> date | None:
    """Ein Datum aus Maske oder Abfrage fuer den Kursvorschlag; sonst None."""
    try:
        return date.fromisoformat(lies_datum(roh or "", feld="Datum", zukunft=True))
    except ValueError:
        return None


def _umrechnung_vorschlag(
    request: Request, lesung: pipeline.Lesung, vorgabe: dict[str, object],
    eingabe: dict[str, str],
) -> dict[str, object] | None:
    """Der Kursvorschlag fuer die Pruefmaske -- aus den Eingaben, sonst aus
    der Auslesung. Stichtag ist der Zahlungstag, sonst das Belegdatum: genau
    wie beim Buchen (``pipeline.buche``)."""
    felder = lesung.vorschlag.felder
    if "waehrung" in eingabe:
        roh = eingabe["waehrung"]
    elif "waehrung" in felder:
        roh = felder["waehrung"].wert
    else:
        roh = vorgabe.get("waehrung") or "EUR"
    try:
        waehrung = pipeline.normalisiere_waehrung(roh)
    except BelegFehler:
        return None
    if waehrung == "EUR":
        return None
    if "brutto_euro" in eingabe:
        fremd = _cent_oder_none(eingabe["brutto_euro"])
    else:
        fremd = felder["brutto"].wert if "brutto" in felder else None
    if eingabe:
        # "offen" heisst: keine Zahlung -- dann gilt das Belegdatum, wie beim
        # Buchen, auch wenn noch ein Datum im Feld steht.
        gezahlt = (None if eingabe.get("zahlung") == "offen"
                   else _tag_oder_none(eingabe.get("bezahlt_am")))
        stichtag = gezahlt or _tag_oder_none(eingabe.get("belegdatum"))
    else:
        stichtag = _tag_oder_none(str(felder["belegdatum"].wert)) if "belegdatum" in felder \
            else None
    try:
        kurs_von_hand = kurse.lies_kurs(eingabe.get("kurs_manuell"))
    except ValueError:
        kurs_von_hand = None
    return umrechnung.vorschlag(
        _datenordner(request), waehrung, fremd if isinstance(fremd, int) else None, stichtag,
        karte_cent=_cent_oder_none(eingabe.get("brutto_eur")), kurs_manuell=kurs_von_hand)


@router.get("/belege/kurs")
def kursvorschlag(
    request: Request, waehrung: str = "", datum: str = "", betrag: str = "", karte: str = "",
    kurs: str = "",
):
    """Der Kursvorschlag als JSON -- die Pruefmaske fragt beim Tippen nach.

    Nur Waehrung, Tag, Betraege und ein Kurs von Hand gehen hierher, und von
    hier nichts weiter: geladen wird hoechstens die oeffentliche Kurstabelle
    der EZB. Mit einem Kurs von Hand zeigt der Vorschlag den Euro-Betrag zu
    ihm, bevor gebucht wird.
    """
    try:
        code = pipeline.normalisiere_waehrung(waehrung)
    except BelegFehler as fehler:
        return JSONResponse({"ok": False, "waehrung": waehrung.strip()[:12],
                             "text": str(fehler), "euro": None, "manuell": False,
                             "rueckfrage": False, "abweichung": None})
    tag = None
    text = None
    if datum.strip():
        try:
            tag = date.fromisoformat(lies_datum(datum, feld="Datum"))
        except ValueError as fehler:
            text = str(fehler)
    try:
        kurs_von_hand = kurse.lies_kurs(kurs)
    except ValueError as fehler:
        kurs_von_hand, kurs_fehler = None, str(fehler)
    else:
        kurs_fehler = None
    ergebnis = umrechnung.vorschlag(_datenordner(request), code, _cent_oder_none(betrag), tag,
                                    karte_cent=_cent_oder_none(karte),
                                    kurs_manuell=kurs_von_hand)
    if text and code != "EUR":
        ergebnis["text"] = text
    elif kurs_fehler and ergebnis["manuell"]:
        ergebnis["text"] = f"{ergebnis['text']} {kurs_fehler}"
    return JSONResponse(ergebnis)


@router.get("/belege/pruefen/{dateiname}", response_class=HTMLResponse)
def pruefmaske(
    dateiname: str,
    request: Request,
    conn: sqlite3.Connection = Depends(hole_conn),
):
    pfad = _eingangsdatei(_datenordner(request), dateiname)
    if pfad is None:
        return _nicht_im_eingang(conn, dateiname)
    if pfad.suffix.lower() not in pipeline.ERLAUBT:
        return _kein_belegformat(pfad)
    passt = pipeline.inhalt_passt(pfad)
    if passt is None:
        # Eben noch im Eingang, jetzt nicht mehr lesbar: gerade gebucht und
        # verschoben (zweiter Tab, Doppelklick) -- kein fremder Inhalt.
        return _nicht_im_eingang(conn, dateiname)
    if not passt:
        return _inhalt_fremd(pfad)
    try:
        lesung = _lies(conn, _eingang(pfad))
    except OSError:
        # Waehrend des Lesens gebucht und verschoben (zweiter Tab, Doppelklick).
        return _nicht_im_eingang(conn, dateiname)
    return _maske(request, conn, dateiname, lesung)


@router.get("/belege/eingang/{dateiname}")
def eingangsdatei(dateiname: str, request: Request, download: str = ""):
    """Zeigt die Datei im Eingang -- die rechte Haelfte der Pruefmaske.

    Ohne das Bild neben den Feldern ist die Pruefung ein Ratespiel: man sieht
    eine Zahl und soll bestaetigen, dass sie auf einem Beleg steht, den man
    nicht sieht. Eine E-Mail erscheint als sichere Textansicht (nie ihr
    HTML); ``?download=1`` gibt das Original.
    """
    pfad = _eingangsdatei(_datenordner(request), dateiname)
    if pfad is None:
        return _meldung("/belege", "Diese Datei liegt nicht im Eingang.")
    return dateiantwort(request, pfad, pipeline.original_name_von(pfad),
                        pipeline._mime(pfad.suffix.lower()), download,
                        f"/belege/eingang/{_url(dateiname)}?download=1")


@router.get("/belege/eingang/{dateiname}/anhang/{nr}")
def eingangsanhang(dateiname: str, nr: str, request: Request):
    """Ein Anhang einer Mail im Eingang -- die Rechnung, die in der Pruefmaske
    neben den Feldern steht. Nur Belegformate (PDF, XML, Bilder), nie HTML."""
    pfad = _eingangsdatei(_datenordner(request), dateiname)
    nummer = filterzahl(nr)
    if pfad is None or pfad.suffix.lower() != email_leser.ENDUNG or nummer is None:
        return _meldung("/belege", "Diese Datei liegt nicht im Eingang.")
    try:
        mail = email_leser.lies_datei(pfad)
    except (BelegFehler, OSError):
        return _meldung("/belege", "Diese E-Mail lässt sich nicht lesen.")
    anhang = next((a for a in mail.anhaenge if a.nr == nummer and a.art == "kandidat"), None)
    if anhang is None:
        return _meldung("/belege", "Diesen Anhang gibt es nicht.")
    return Response(anhang.daten, media_type=pipeline._mime(anhang.endung) or
                    "application/octet-stream",
                    headers={**dateikopf(anhang.name, herunterladen=False),
                             **_SICHER_KOEPFE})


#: Fuer alles, was aus einer Mail stammt: nichts nachladen, nichts ausfuehren,
#: den Typ nicht raten.
_SICHER_KOEPFE = {
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "frame-ancestors 'self'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


#: Fuer eine archivierte Datei, die kein PDF und kein Bild ist (XML): wie
#: oben, dazu ``sandbox`` -- sie wird als Text gezeigt und darf nichts tun.
_SICHER_TEXT = {**_SICHER_KOEPFE,
                "content-security-policy": _SICHER_KOEPFE["content-security-policy"] + "; sandbox"}


def dateiantwort(request: Request, pfad: Path, name: str, mime: str | None, download: str,
                 download_url: str) -> Response:
    """Eine archivierte Datei (Beleg, Abrechnung einer Einnahme) fuer den Browser.

    PDF und Bilder zeigt der Browser selbst (mit ``nosniff``). Eine Mail
    kommt als sichere Textansicht. Alles andere -- vor allem XML -- als
    Klartext ohne Skript: eine .xml mit XHTML-Namensraum, als
    application/xml ausgeliefert, fuehrte sonst ihr Skript im Ursprung des
    Werkzeugs aus und konnte alle Daten lesen (etwa /backup/excel).
    ``?download=1`` liefert immer das Original als Download.
    """
    if mime == email_leser.MIME or pfad.suffix.lower() == email_leser.ENDUNG:
        return _mail_antwort(request, pfad, name, download, download_url)
    if download == "1":
        return FileResponse(pfad, media_type=mime or "application/octet-stream",
                            headers={**dateikopf(name, herunterladen=True), **_SICHER_TEXT})
    if mime and (mime == "application/pdf" or mime.startswith("image/")):
        return FileResponse(pfad, media_type=mime,
                            headers={**dateikopf(name, herunterladen=False),
                                     "x-content-type-options": "nosniff"})
    return FileResponse(pfad, media_type="text/plain; charset=utf-8",
                        headers={**dateikopf(name, herunterladen=False), **_SICHER_TEXT})


def _mail_antwort(request: Request, pfad: Path, name: str, download: str,
                  download_url: str) -> Response:
    """Eine Mail: als sichere Textansicht oder (``download=1``) das Original."""
    if download == "1":
        return FileResponse(pfad, media_type=email_leser.MIME,
                            headers={**dateikopf(name, herunterladen=True), **_SICHER_KOEPFE})
    try:
        mail = email_leser.lies_datei(pfad)
    except (BelegFehler, OSError) as fehler:
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><p>{html_text(str(fehler))}",
                            status_code=422, headers=_SICHER_KOEPFE)
    return VORLAGEN.TemplateResponse(
        request, "email_ansicht.html",
        {"mail": email_leser.textansicht(mail), "download": download_url},
        headers=_SICHER_KOEPFE)




@router.get("/belege/{beleg_id}/datei")
def belegdatei(beleg_id: str, request: Request, download: str = "",
               conn: sqlite3.Connection = Depends(hole_conn)):
    """Das archivierte Original -- unter dem Namen, unter dem es hochgeladen
    wurde, nicht unter dem Hash, den niemand im Download-Ordner wiedererkennt.

    Angezeigt im Browser; ``?download=1`` laedt herunter.
    """
    return _archivdatei(request, conn, beleg_id, "ORIGINAL", download)


@router.get("/belege/{beleg_id}/mail")
def belegmail(beleg_id: str, request: Request, download: str = "",
              conn: sqlite3.Connection = Depends(hole_conn)):
    """Die E-Mail, mit der die Rechnung kam (Rolle EMAIL) -- als sichere
    Textansicht, ``?download=1`` als Original."""
    return _archivdatei(request, conn, beleg_id, "EMAIL", download)


def _archivdatei(request: Request, conn: sqlite3.Connection, beleg_id: str, rolle: str,
                 download: str):
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    zeile = conn.execute(
        "SELECT pfad, mime, original_name FROM belegdatei WHERE beleg_id = ?"
        " AND rolle = ?", (beleg["id"], rolle),
    ).fetchone()
    if zeile is None:
        if beleg["ist_eigenbeleg"]:
            return _meldung("/belege", "Ein Eigenbeleg hat keine Datei.")
        return _meldung("/belege", "Zu diesem Beleg fehlt die Datei.")
    pfad = pfade.absolut(_datenordner(request), zeile["pfad"])
    if not pfad.exists():
        return _meldung("/belege", "Zu diesem Beleg fehlt die Datei.")
    teil = "mail" if rolle == "EMAIL" else "datei"
    return dateiantwort(request, pfad, zeile["original_name"] or pfad.name, zeile["mime"],
                        download, f"/belege/{beleg['id']}/{teil}?download=1")


def _eingangsdatei(datenordner: Path, dateiname: str) -> Path | None:
    """Loest einen Dateinamen im Eingang auf -- und nur dort.

    Der Name kommt aus der URL. Ohne diese Pruefung waere
    ``/belege/eingang/..%2F..%2Fsteuer.sqlite`` ein Weg an die Datenbank. Ein
    Null-Byte oder ein anderer unlesbarer Name fuehrte zu einer Absturzseite;
    er liegt schlicht nicht im Eingang.
    """
    if (not dateiname or "/" in dateiname or "\\" in dateiname
            or dateiname.startswith(".") or not dateiname.isprintable()):
        return None
    try:
        pfad = (datenordner / pipeline.EINGANG / dateiname).resolve()
        ordner = (datenordner / pipeline.EINGANG).resolve()
        if pfad.parent != ordner or not pfad.is_file():
            return None
    except (OSError, ValueError):
        return None
    return pfad


# -------------------------------------------------------------------- Buchen

def _ganzzahl(roh: str, meldung: str) -> int | None:
    """Eine Kennung aus einer Auswahlliste; leer heisst "keine".

    Ueber ``kennung``: "²" ist fuer Python eine Ziffer, und ab 2**63 sprengte
    die Zahl die Bindung an SQLite -- beides endete als Absturzseite.
    """
    if not roh.strip():
        return None
    wert = kennung(roh)
    if wert is None:
        raise BelegFehler(meldung)
    return wert


def _ausgabenkategorie(conn: sqlite3.Connection, roh: str) -> int | None:
    """Die Kategorie aus dem Formular -- vorhanden und eine Ausgabenkategorie.

    Eine Einnahmen-Kategorie an einem Beleg fuehrte die EUeR sonst mit einer
    Einnahmen-Kennzahl unter den Ausgaben.
    """
    kategorie_id = _ganzzahl(roh, "Diese Kategorie gibt es nicht.")
    if kategorie_id is None:
        return None
    zeile = conn.execute("SELECT bereich FROM euer_kategorie WHERE id = ?",
                         (kategorie_id,)).fetchone()
    if zeile is None:
        raise BelegFehler("Diese Kategorie gibt es nicht.")
    if zeile["bereich"] != kategorien.BEREICH_AUSGABEN:
        raise BelegFehler("Diese Kategorie ist keine Ausgabenkategorie — ein Beleg "
                          "ist eine Betriebsausgabe.")
    return kategorie_id


def _reise(conn: sqlite3.Connection, roh: str) -> int | None:
    reise_id = _ganzzahl(roh, "Diese Reise gibt es nicht.")
    if reise_id is None:
        return None
    if conn.execute("SELECT 1 FROM reise WHERE id = ?", (reise_id,)).fetchone() is None:
        raise BelegFehler("Diese Reise gibt es nicht.")
    return reise_id


def _quote(roh: str) -> int:
    try:
        wert = ganzzahl(roh.strip() or "100", feld="Quote")
    except ValueError:
        raise BelegFehler(f"„{roh.strip()[:40]}“ ist keine Quote — bitte eine ganze Zahl "
                          "zwischen 0 und 100.") from None
    if not 0 <= wert <= 100:
        raise BelegFehler(f"Die betriebliche Quote muss zwischen 0 und 100 liegen, "
                          f"nicht {wert}.")
    return wert


def _zahlungsdatum(roh: str) -> str | None:
    """Das Zahlungsdatum der Maske: ein ISO-Datum, nicht in der Zukunft.

    Ein ungueltiger Wert fiel sonst aus jedem EUeR-Jahr, und eine Zahlung in
    der Zukunft ist noch keine -- fuer die EUeR zaehlt nach § 11 EStG der
    Abfluss.
    """
    roh = roh.strip()
    if not roh:
        return None
    # Zuerst das gemeinsame Lesen: es nimmt auch TT.MM.JJJJ an (Kalenderfeld
    # als Textfeld) und kennt das Fenster -- ein Jahr 0026 ist ein Tippfehler,
    # keine Zahlung aus der Antike. Gespeichert wird die gelesene Form.
    try:
        return lies_datum(roh, feld="Zahlungsdatum")
    except ValueError as fehler:
        text = str(fehler)
        if "Zukunft" in text:
            text = f"{text.rstrip('.')} — erfasst wird eine Zahlung, die schon geflossen ist."
        raise BelegFehler(text) from None


def _datum(roh: str, feld: str, *, zukunft: bool = False) -> str:
    """Ein Datum ueber die gemeinsame Pruefung (app.logik.eingabe)."""
    try:
        return lies_datum(roh, feld=feld, zukunft=zukunft)
    except ValueError as fehler:
        raise BelegFehler(str(fehler)) from None


#: Hoechster Steuersatz in Prozent. Mehr gibt es nirgends, und ohne Grenze
#: sprengte "1e400" die Umrechnung in Promille.
STEUERSATZ_HOECHSTENS = 100


def _steuersatz(roh: str) -> int | None:
    """Der Steuersatz der Maske in Promille; leer heisst "keiner angegeben".

    Ueber Decimal statt float: float("inf") und float("1e400") sind fuer
    Python Zahlen, und ``int(round(...))`` scheiterte daran mit einer
    Absturzseite.

    >>> _steuersatz("19"), _steuersatz("7,0"), _steuersatz("19 %"), _steuersatz("")
    (190, 70, 190, None)
    """
    text = roh.strip().removesuffix("%").strip().replace(",", ".")
    if not text:
        return None
    meldung = (f"„{roh.strip()[:40]}“ ist kein Steuersatz — bitte einen Prozentsatz "
               f"zwischen 0 und {STEUERSATZ_HOECHSTENS} eintragen, etwa 19 oder 7.")
    try:
        wert = Decimal(text)
    except InvalidOperation:
        raise BelegFehler(meldung) from None
    if not wert.is_finite() or not 0 <= wert <= STEUERSATZ_HOECHSTENS:
        raise BelegFehler(meldung)
    return int((wert * 10).to_integral_value(rounding=ROUND_HALF_UP))


@router.post("/belege/pruefen/{dateiname}")
def buchen(
    dateiname: str,
    request: Request,
    belegdatum: str = Form(""),
    brutto_euro: str = Form(""),
    netto_euro: str = Form(""),
    ust_euro: str = Form(""),
    ust_satz: str = Form(""),
    waehrung: str = Form("EUR"),
    belegnummer: str = Form(""),
    lieferant_id: str = Form(""),
    lieferant_neu: str = Form(""),
    lieferant_ust_idnr: str = Form(""),
    kategorie_id: str = Form(""),
    quote: str = Form("100"),
    reverse_charge: str = Form(""),
    zahlungsart: str = Form("UEBERWEISUNG"),
    zahlung: str = Form(""),
    bezahlt_am: str = Form(""),
    notiz: str = Form(""),
    anlagegut: str = Form(""),
    reise_id: str = Form(""),
    brutto_eur: str = Form(""),
    kurs_manuell: str = Form(""),
    karte_bestaetigt: str = Form(""),
    kurs_bestaetigt: str = Form(""),
    trotzdem: str = Form(""),
    vorgabe_merken: str = Form(""),
    zuordnung: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Bucht den bestaetigten Beleg -- oder haengt die Datei an einen
    vorhandenen Eintrag, wenn die Maske das unter "Passt zu einer vorhandenen
    Buchung?" gewaehlt hat (``zuordnung`` = ``beleg:12`` / ``einnahme:5``;
    "neu" oder leer heisst buchen).

    Bei Fremdwaehrung ist ``brutto_eur`` der Betrag laut Kreditkarte
    (optional; ohne ihn wird zum EZB-Referenzkurs umgerechnet) und
    ``kurs_manuell`` ein Kurs von Hand, nur wenn die EZB keinen liefert.

    ``zahlung`` ist die Auswahl "bezahlt" / "offen" der Maske (vorausgewaehlt
    nach der Einstellung "Eingangsbelege sind in der Regel schon bezahlt"):
    "bezahlt" ohne Datum heisst Zahltag = Belegdatum, "offen" legt keine
    Zahlung an, auch wenn ein Datum im Feld steht. Fehlt das Feld (ein
    aelteres Formular), entscheidet wie bisher, ob "Bezahlt am" gefuellt ist.
    Die Zahlung entsteht in ``pipeline.buche``, in derselben Transaktion wie
    der Beleg.
    """
    from app.logik.kunden import BetragZuGross, euro_nach_cent

    datenordner = _datenordner(request)
    pfad = _eingangsdatei(datenordner, dateiname)
    if pfad is None:
        return _nicht_im_eingang(conn, dateiname)
    if pfad.suffix.lower() not in pipeline.ERLAUBT:
        return _kein_belegformat(pfad)

    try:
        eingang = _eingang(pfad)
        passt = pipeline.inhalt_passt(pfad)
        if passt is None:
            # Zwischen Lesen und Pruefen verschoben: dieselbe Lage wie der
            # OSError unten. Vorher hiess es hier "Der Inhalt ... ist kein
            # XML", obwohl die Datei gerade als Beleg gebucht worden war.
            return _nicht_im_eingang(conn, dateiname)
        if not passt:
            return _inhalt_fremd(pfad)
        lesung = _lies(conn, eingang)
    except OSError:
        # Doppelklick: die erste Anfrage hat die Datei gerade ins Archiv
        # verschoben, waehrend diese sie noch las. Vorher eine Absturzseite.
        return _nicht_im_eingang(conn, dateiname)
    if zuordnung.strip() not in ("", "neu"):
        # Die Felder der Maske spielen dann keine Rolle: Betrag und Datum
        # bleiben die des Eintrags (app/routen/belegkorrektur.py).
        from app.routen.belegkorrektur import zuordnen_aus_eingang

        return zuordnen_aus_eingang(request, conn, dateiname, lesung, zuordnung)
    eingabe = {
        "belegdatum": belegdatum, "brutto_euro": brutto_euro, "netto_euro": netto_euro,
        "ust_euro": ust_euro, "ust_satz": ust_satz, "waehrung": waehrung,
        "belegnummer": belegnummer, "lieferant_id": lieferant_id,
        "lieferant_neu": lieferant_neu, "lieferant_ust_idnr": lieferant_ust_idnr,
        "kategorie_id": kategorie_id, "quote": quote, "reverse_charge": reverse_charge,
        "zahlungsart": zahlungsart, "zahlung": zahlung, "bezahlt_am": bezahlt_am,
        "notiz": notiz,
        "anlagegut": anlagegut, "reise_id": reise_id, "brutto_eur": brutto_eur,
        "kurs_manuell": kurs_manuell, "karte_bestaetigt": karte_bestaetigt,
        "kurs_bestaetigt": kurs_bestaetigt,
        "trotzdem": trotzdem, "vorgabe_merken": vorgabe_merken, "zuordnung": zuordnung,
    }

    def zurueck(text: str, *, aehnlich: bool = False, rueckfrage: bool = False,
                kurs_frage: bool = False) -> HTMLResponse:
        return _maske(request, conn, dateiname, lesung, fehler=text, eingabe=eingabe,
                      aehnlich_bestaetigen=aehnlich,
                      karte_rueckfrage=rueckfrage or karte_bestaetigt == "1",
                      kurs_rueckfrage=kurs_frage or kurs_bestaetigt == "1", status_code=422)

    try:
        betraege = {
            "brutto": euro_nach_cent(brutto_euro),
            "netto": euro_nach_cent(netto_euro),
            "ust": euro_nach_cent(ust_euro),
            "brutto_eur": euro_nach_cent(brutto_eur),
        }
    except ArithmeticError as fehler:
        # euro_nach_cent sagt selbst, was nicht stimmt ("ist kein Betrag",
        # "ist als Betrag unplausibel groß") -- und nennt den Wert.
        if isinstance(fehler, BetragZuGross):
            return zurueck(str(fehler))
        return zurueck(f"Einer der Beträge ist keine Zahl: {fehler}")
    except ValueError:
        return zurueck("Einer der Beträge ist keine Zahl.")

    try:
        kurs_von_hand = kurse.lies_kurs(kurs_manuell)
    except ValueError as fehler:
        return zurueck(str(fehler))

    try:
        satz_promille = _steuersatz(ust_satz)
        quote_wert = _quote(quote)
        kategorie = _ausgabenkategorie(conn, kategorie_id)
        reise = _reise(conn, reise_id)
        gezahlt_am = _zahltag_der_maske(zahlung, bezahlt_am, belegdatum)
        # Der Lieferant kann in derselben Maske neu entstehen. Sonst muesste
        # der Nutzer die Pruefung abbrechen, den Lieferanten anlegen und neu
        # beginnen -- und beim dritten Mal erfasst er den Beleg daneben von Hand.
        lieferant, lieferant_anlegen = _lieferant_bestimmen(conn, lieferant_id, lieferant_neu,
                                                            lieferant_ust_idnr)
    except BelegFehler as fehler:
        return zurueck(str(fehler))

    bestaetigt: dict[str, object] = {
        "belegdatum": belegdatum.strip(),
        "belegnummer": ohne_steuerzeichen(belegnummer) or None,
        "waehrung": waehrung,
        "brutto": betraege["brutto"],
        "netto": betraege["netto"],
        "ust": betraege["ust"],
        "brutto_eur": betraege["brutto_eur"],
        "kurs_manuell": kurs_von_hand,
        "karte_bestaetigt": karte_bestaetigt == "1",
        "kurs_bestaetigt": kurs_bestaetigt == "1",
        # Der Stichtag der Umrechnung: der Zahlungstag, falls bekannt.
        "umrechnung_am": gezahlt_am,
        "ust_satz_promille": satz_promille,
        "reverse_charge": reverse_charge == "1",
        "lieferant": (lieferant["name"] if lieferant
                      else lieferant_anlegen[0] if lieferant_anlegen else None),
        "lieferant_ust_idnr": (lieferant["ust_idnr"] if lieferant
                               else lieferant_anlegen[1] if lieferant_anlegen else None),
    }

    try:
        buchung = pipeline.buche(
            conn, lesung, bestaetigt, datenordner,
            lieferant_id=lieferant["id"] if lieferant else None,
            lieferant_neu=lieferant_anlegen,
            kategorie_id=kategorie,
            quote=quote_wert,
            notiz=ohne_steuerzeichen(notiz) or None,
            zahlungsart=zahlungsart if zahlungsart in ZAHLUNGSARTEN else "UEBERWEISUNG",
            ist_anlagegut=anlagegut == "1",
            reise_id=reise,
            trotzdem=trotzdem == "1",
            bezahlt_am=gezahlt_am,
            zahlung_begruendung=(
                bezahlvorgabe.BEGRUENDUNG_BESTAETIGT
                if zahlung == "bezahlt" and gezahlt_am == _datum_oder_text(belegdatum)
                else None),
        )
    except pipeline.SchonGebucht as fehler:
        if not pfad.exists():
            # Doppelklick: die andere Anfrage hat gebucht und die Datei ins
            # Archiv verschoben. Das ist ein Erfolg, kein Fehler.
            return _meldung("/belege", f"Diese Datei wurde bereits als Beleg "
                                       f"{fehler.beleg_id} gebucht.", art="erfolg")
        return zurueck(str(fehler))
    except pipeline.AehnlicherBeleg as fehler:
        return zurueck(str(fehler), aehnlich=True)
    except umrechnung.KartenbetragAbweichend as fehler:
        return zurueck(str(fehler), rueckfrage=True)
    except umrechnung.KursVonHandUnplausibel as fehler:
        return zurueck(str(fehler), kurs_frage=True)
    except umrechnung.KeinEuroBetrag as fehler:
        # Was zu tun ist, sagt der Kasten "In Euro" (derselbe Kursvorschlag).
        return zurueck(fehler.ohne_hilfe)
    except BelegFehler as fehler:
        if not pfad.exists():
            return _nicht_im_eingang(conn, dateiname)
        return zurueck(str(fehler))
    except sqlite3.IntegrityError as fehler:
        if "FOREIGN KEY" in str(fehler):
            return zurueck("Kategorie, Lieferant oder Reise gibt es nicht mehr — "
                           "bitte die Auswahl prüfen.")
        return zurueck("Die Datenbank hat den Beleg abgewiesen — bitte die Eingaben "
                       "prüfen und erneut buchen.")

    if lieferant is None and buchung.lieferant_id is not None:
        # Mit der Buchung neu angelegt (oder unter der Sperre doch gefunden).
        lieferant = conn.execute("SELECT * FROM lieferant WHERE id = ?",
                                 (buchung.lieferant_id,)).fetchone()

    text = f"Beleg {buchung.beleg_id} erfasst."
    if buchung.umrechnung is not None:
        text += f" {buchung.umrechnung.text()}"
    if gezahlt_am:
        text += f" Bezahlt am {datum_de(gezahlt_am)}."
    if vorgabe_merken == "1" and lieferant is not None:
        _vorgabe_merken(conn, lieferant, kategorie, quote_wert)
        text += f" Kategorie und Quote sind ab jetzt Vorgabe für {lieferant['name']}."
    if buchung.gelernt:
        text += (" Für diesen Lieferanten gelernt: "
                 f"{', '.join(profil.feldname(f) for f in buchung.gelernt)} — "
                 "der nächste Beleg ist ein Bestätigungsklick.")
    if kategorie is None:
        text += " Ohne Kategorie geht er in keine Zeile der EÜR."
    if not gezahlt_am:
        # Fuer die EUeR zaehlt nach § 11 Abs. 2 EStG der Abfluss. Ein Beleg
        # ohne Zahlung steht in keinem Jahr -- das muss der Nutzer jetzt
        # erfahren, nicht erst beim Jahresabschluss.
        text += (" Ohne Zahlungsdatum zählt er noch in keinem EÜR-Jahr — in der Liste "
                 "unter „Ändern“ die Zahlung erfassen.")
    # "weiter=1": die Belegseite bietet unter der Meldung die naechste Datei
    # im Eingang an -- wer zehn Rechnungen prueft, will nicht zehnmal suchen.
    return _meldung("/belege?weiter=1", text, art="erfolg")


def _datum_oder_text(roh: str) -> str:
    """Das Belegdatum in gelesener Form -- oder wie getippt, wenn es keins ist
    (dann weist ``pipeline.buche`` es mit eigener Meldung ab)."""
    try:
        return lies_datum(roh, feld="Belegdatum", zukunft=True)
    except ValueError:
        return roh.strip()


def _zahltag_der_maske(zahlung: str, bezahlt_am: str, belegdatum: str) -> str | None:
    """Der Zahltag aus der Maske -- ``None`` heisst: keine Zahlung.

    "bezahlt" ohne Datum: das Belegdatum (die Vorgabe "Zahltag =
    Belegdatum"). "offen": keine Zahlung, auch wenn noch ein Datum im Feld
    steht -- die Auswahl ist das, was der Nutzer gesehen hat. Ohne Auswahl
    (aelteres Formular) wie bisher: ein Datum im Feld ist die Zahlung.
    """
    if zahlung == "offen":
        return None
    if zahlung == "bezahlt" and not bezahlt_am.strip():
        return _datum_oder_text(belegdatum) or None
    return _zahlungsdatum(bezahlt_am)


def _vorgabe_merken(
    conn: sqlite3.Connection, lieferant: sqlite3.Row, kategorie_id: int | None, quote: int
) -> None:
    """Kategorie und Quote als Vorgabe am Lieferanten -- mit Journal, weil sie
    jeden kuenftigen Beleg dieses Lieferanten vorbelegen."""
    neu = {"quote_default": quote}
    if kategorie_id is not None:
        neu["standard_kategorie_id"] = kategorie_id
    conn.execute("BEGIN IMMEDIATE")
    try:
        for spalte, wert in neu.items():
            if lieferant[spalte] == wert:
                continue
            conn.execute(f"UPDATE lieferant SET {spalte} = ? WHERE id = ?",
                         (wert, lieferant["id"]))
            db.protokolliere(conn, "AENDERN", "lieferant", lieferant["id"], feld=spalte,
                             wert_alt=lieferant[spalte], wert_neu=wert)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _lieferant_bestimmen(
    conn: sqlite3.Connection, gewaehlt: str, neu: str, ust_idnr: str
) -> tuple[sqlite3.Row | None, tuple[str, str | None] | None]:
    """Der gewaehlte oder schon vorhandene Lieferant -- oder (Name, USt-IdNr.)
    eines neu getippten, den erst ``pipeline.buche`` anlegt.

    Angelegt wird hier nichts mehr: die Route schrieb den neuen Lieferanten
    fest, bevor ``buche`` den Beleg (etwa als Dublette) abwies, und er blieb
    ohne Beleg stehen. Jetzt entsteht er in derselben Transaktion wie der
    Beleg, unter derselben Sperre (auch gegen den Doppelklick).
    """
    lieferant_id = _ganzzahl(gewaehlt, "Diesen Lieferanten gibt es nicht.")
    if lieferant_id is not None:
        zeile = conn.execute("SELECT * FROM lieferant WHERE id = ?",
                             (lieferant_id,)).fetchone()
        if zeile is None:
            raise BelegFehler("Diesen Lieferanten gibt es nicht.")
        return zeile, None
    name = ohne_steuerzeichen(neu)
    idnr = ohne_steuerzeichen(ust_idnr) or None
    if not name:
        return None, None
    vorhanden = pipeline.finde_lieferant(conn, name, idnr)
    if vorhanden is not None:
        return vorhanden, None
    return None, (name, idnr)


def _zahlung_schreiben(
    conn: sqlite3.Connection, beleg_id: int, datum: str, brutto_cent: int, art: str
) -> None:
    """Die Zahlungszeile mit Journal -- innerhalb einer offenen Transaktion.

    Der Abfluss: fuer die EUeR zaehlt nach § 11 EStG er, nicht das Belegdatum.
    Negativ gebucht, weil es eine Ausgabe ist: so bleibt die Spalte
    ``betrag_cent`` in einer Summe ueber alle Zahlungen vorzeichenrichtig.
    """
    conn.execute(
        "INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
        " VALUES (?, ?, ?, ?)",
        (beleg_id, datum, -abs(brutto_cent),
         art if art in ZAHLUNGSARTEN else "UEBERWEISUNG"),
    )
    db.protokolliere(conn, "ZAHLUNG", "beleg", beleg_id, feld="datum", wert_neu=datum)


# ------------------------------------------------------------- Nachbearbeiten

@router.post("/belege/{beleg_id}/kategorie")
def kategorie_setzen(
    beleg_id: str,
    kategorie_id: str = Form(""),
    quote: str = Form("100"),
    vorgabe_merken: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Kategorie und Quote nachtraeglich setzen.

    Der Betrag bleibt unberuehrt -- nur die Zuordnung aendert sich, und
    ``betrag_betrieblich_cent`` wird mit derselben Funktion neu gerechnet, die
    ihn beim Buchen gesetzt hat. Zwei Rechenwege waeren zwei Ergebnisse.

    ``vorgabe_merken`` macht beides zur Vorgabe des Lieferanten -- wie der
    Haken in der Pruefmaske. Gerade ein automatisch gebuchter Beleg bekommt
    seine Kategorie erst hier; ohne den Haken waere der naechste desselben
    Lieferanten wieder ohne.
    """
    alt = _beleg(conn, beleg_id)
    if alt is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    if (antwort := _geloescht(alt)) is not None:
        return antwort
    beleg_id = alt["id"]
    try:
        quote_wert = _quote(quote)
        neu = _ausgabenkategorie(conn, kategorie_id)
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE beleg SET kategorie_id = ?, aufteilungsquote = ?,"
            " betrag_betrieblich_cent = ?, version = version + 1 WHERE id = ?",
            (neu, quote_wert,
             kategorien.betrieblich_cent(alt["betrag_brutto_cent"], quote_wert), beleg_id),
        )
        if alt["kategorie_id"] != neu:
            db.protokolliere(conn, "AENDERN", "beleg", beleg_id, feld="kategorie_id",
                             wert_alt=alt["kategorie_id"], wert_neu=neu)
        if alt["aufteilungsquote"] != quote_wert:
            db.protokolliere(conn, "AENDERN", "beleg", beleg_id, feld="aufteilungsquote",
                             wert_alt=alt["aufteilungsquote"], wert_neu=quote_wert)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        return _meldung("/belege", "Diese Kategorie gibt es nicht.", beleg=beleg_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if vorgabe_merken == "1":
        lieferant = conn.execute("SELECT * FROM lieferant WHERE id = ?",
                                 (alt["lieferant_id"],)).fetchone()
        if lieferant is None:
            return _meldung("/belege", f"Beleg {beleg_id}: gespeichert. Ohne Lieferant gibt "
                                       "es keine Vorgabe zu merken.", art="erfolg",
                            beleg=beleg_id)
        _vorgabe_merken(conn, lieferant, neu, quote_wert)
        if neu is None:
            text = (f" Die Quote ist ab jetzt Vorgabe für {lieferant['name']}; eine "
                    "Kategorie war nicht gewählt.")
        else:
            text = f" Kategorie und Quote sind ab jetzt Vorgabe für {lieferant['name']}."
        return _meldung("/belege", f"Beleg {beleg_id}: gespeichert.{text}", art="erfolg",
                        beleg=beleg_id)
    return RedirectResponse("/belege", status_code=303)


def _beleg(conn: sqlite3.Connection, roh: str) -> sqlite3.Row | None:
    """Der Beleg zur Kennung aus der Adresse; Unbrauchbares ergibt None.

    Die Kennung kommt als Text: als ``int`` deklariert nahm FastAPI auch
    2**63 an, und SQLite brach mit einem OverflowError ab (Absturzseite).
    """
    beleg_id = kennung(roh)
    if beleg_id is None:
        return None
    return conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()


def _geloescht(beleg: sqlite3.Row) -> RedirectResponse | None:
    """Die Antwort auf eine Aenderung an einem geloeschten Beleg -- sonst None.

    Die Datenbank wiese die Aenderung ab (Trigger
    beleg_geloescht_unveraenderlich), aber als Absturzseite oder mit einer
    Meldung, die nicht passt ("Diese Kategorie gibt es nicht"). Erreichbar
    ueber eine veraltete Seite oder die Liste mit "geloeschte zeigen".
    """
    if beleg["geloescht_am"] is None:
        return None
    return _meldung("/belege", f"Beleg {beleg['id']} ist gelöscht "
                               f"({beleg['geloescht_grund']}) — erst wiederherstellen, dann "
                               "ändern.", beleg=beleg["id"])


@router.post("/belege/{beleg_id}/bezahlt")
def bezahlt(
    beleg_id: str,
    request: Request,
    datum: str = Form(""),
    art: str = Form("UEBERWEISUNG"),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    if (antwort := _geloescht(beleg)) is not None:
        return antwort
    if not datum.strip():
        return _meldung("/belege", "Ohne Datum keine Zahlung — für die EÜR zählt "
                                   "nach § 11 EStG der Tag des Abflusses.", beleg=beleg["id"])
    # Der Wert wandert in eine Datumsspalte, auf der die EUeR rechnet: was
    # kein ISO-Datum ist, zaehlte in keinem Jahr und stand trotzdem im
    # Buchungsblatt. Gespeichert wird deshalb die gelesene Form, wie bei der
    # Rechnung -- "20251231" nahm fromisoformat() an, strftime() nicht.
    try:
        tag = _zahlungsdatum(datum)
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg["id"])

    schon = conn.execute(
        "SELECT COALESCE(SUM(ABS(betrag_cent)), 0) AS cent, MIN(datum) AS am FROM zahlung"
        " WHERE beleg_id = ?", (beleg["id"],)).fetchone()
    if int(schon["cent"]) >= int(beleg["betrag_brutto_cent"]):
        # Vor dem Kurs: ein Doppelklick soll "bereits bezahlt" hoeren und
        # nicht auf die Kurstabelle warten.
        return _meldung("/belege", f"Dieser Beleg ist bereits bezahlt (am "
                                   f"{datum_de(schon['am'])}).", beleg=beleg["id"])

    # Fremdwaehrung: die Umrechnung fuer die EUeR zieht auf den Zahlungstag
    # (app/logik/beleg/umrechnung.py, Entscheidung 3) -- Beleg UND Zahlung,
    # damit sie sich nicht widersprechen. Den Kurs besorgen, BEVOR gesperrt
    # wird: dafuer kann die Kurstabelle geladen werden muessen.
    try:
        neu = umrechnung.fuer_zahlungstag(conn, _datenordner(request), beleg,
                                          date.fromisoformat(tag))
    except kurse.KeinKurs as fehler:
        return _meldung(
            "/belege",
            f"Zahlung nicht erfasst: für die EÜR wird Beleg {beleg['id']} zum Kurs des "
            f"Zahlungstags umgerechnet, und den kennt das Tool noch nicht. {fehler} Bitte "
            "später erfassen — oder zuerst den Betrag laut Kreditkarte eintragen.",
            beleg=beleg["id"],
        )

    # Pruefen und Schreiben unter EINER Sperre: bei einem Doppelklick sahen
    # sonst beide Anfragen "noch offen" und buchten die Zahlung zweimal.
    conn.execute("BEGIN IMMEDIATE")
    try:
        frisch = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg["id"],)).fetchone()
        zeile = conn.execute(
            """
            SELECT (SELECT COALESCE(SUM(ABS(z.betrag_cent)), 0) FROM zahlung z
                     WHERE z.beleg_id = ?) AS bezahlt_cent,
                   (SELECT MIN(z.datum) FROM zahlung z WHERE z.beleg_id = ?) AS bezahlt_am
            """,
            (beleg["id"], beleg["id"]),
        ).fetchone()
        if int(zeile["bezahlt_cent"]) >= int(frisch["betrag_brutto_cent"]):
            # Doppelklick oder zweite Buchung: die EUeR kappt auf den
            # Belegbetrag, das Buchungsblatt zeigte die Ausgabe aber doppelt.
            conn.execute("ROLLBACK")
            return _meldung(
                "/belege",
                f"Dieser Beleg ist bereits bezahlt (am {datum_de(zeile['bezahlt_am'])}).",
                beleg=beleg["id"],
            )
        if neu.kurs is not None and (frisch["kurs"], frisch["kurs_datum"],
                                     frisch["betrag_karte_cent"]) != (
                beleg["kurs"], beleg["kurs_datum"], beleg["betrag_karte_cent"]):
            # Zwischen Lesen und Sperre geaendert (Kartenbetrag in einem
            # zweiten Fenster): nicht mit dem veralteten Stand umrechnen.
            conn.execute("ROLLBACK")
            return _meldung("/belege", f"Beleg {beleg['id']} wurde gerade geändert — bitte "
                                       "die Zahlung noch einmal erfassen.", beleg=beleg["id"])
        umrechnung.zahlungstag_uebernehmen(conn, frisch, neu)
        betrag = conn.execute("SELECT betrag_brutto_cent FROM beleg WHERE id = ?",
                              (beleg["id"],)).fetchone()["betrag_brutto_cent"]
        _zahlung_schreiben(conn, beleg["id"], tag, betrag, art)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if neu.kurs is not None:
        return _meldung(
            "/belege",
            f"Zahlung am {datum_de(tag)} erfasst. Für die EÜR auf den Zahlungstag "
            f"umgerechnet: {euro(beleg['betrag_fremdwaehrung_cent'])} {beleg['waehrung']} "
            f"zum EZB-Kurs {kurse.kurs_de(neu.kurs.kurs)} vom {neu.kurs.kursdatum:%d.%m.%Y} "
            f"= {euro(betrag)} € (vorher {euro(beleg['betrag_brutto_cent'])} € zum Kurs vom "
            f"{datum_de(beleg['kurs_datum'])}).",
            art="erfolg", beleg=beleg["id"],
        )
    if neu.hinweis:
        return _meldung("/belege", f"Zahlung am {datum_de(tag)} erfasst. {neu.hinweis}",
                        art="erfolg", beleg=beleg["id"])
    return RedirectResponse("/belege", status_code=303)


@router.post("/belege/{beleg_id}/kartenbetrag")
def kartenbetrag(
    beleg_id: str,
    request: Request,
    betrag: str = Form(""),
    bestaetigt: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Betrag laut Kreditkarte nachtragen -- er ersetzt den Euro-Betrag.

    Mit ihm stimmt die EUeR auf den Cent, Auslandseinsatzentgelt
    eingeschlossen. Weicht er um mehr als zehn Prozent vom umgerechneten Wert
    ab, fragt die Seite nach (422, der getippte Betrag bleibt stehen).
    """
    from app.logik.kunden import euro_nach_cent

    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    if (antwort := _geloescht(beleg)) is not None:
        return antwort
    try:
        karte = euro_nach_cent(betrag)
    except (ArithmeticError, ValueError) as fehler:
        text = str(fehler) or f"„{betrag.strip()[:40]}“ ist kein Betrag."
        return _meldung("/belege", text, beleg=beleg["id"])
    if karte is None:
        return _meldung("/belege", "Bitte den Betrag laut Kreditkartenabrechnung eintragen.",
                        beleg=beleg["id"])
    try:
        alt, hinweis = umrechnung.kartenbetrag_eintragen(conn, beleg["id"], karte,
                                                         bestaetigt=bestaetigt == "1")
    except umrechnung.KartenbetragAbweichend as fehler:
        return liste_seite(request, conn, karte={"beleg_id": beleg["id"], "text": str(fehler),
                                                  "betrag": betrag, "rueckfrage": True},
                           status_code=422)
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg["id"])
    if alt["betrag_karte_cent"] == karte:
        return _meldung("/belege", f"Beleg {beleg['id']}: Betrag laut Kreditkarte {euro(karte)} € "
                                   "war schon eingetragen — nichts geändert.",
                        art="erfolg", beleg=beleg["id"])
    text = (f"Beleg {beleg['id']}: Betrag laut Kreditkarte {euro(karte)} € übernommen — "
            f"die EÜR rechnet ab jetzt mit ihm (vorher {euro(alt['betrag_brutto_cent'])} €).")
    if alt["betrag_umgerechnet_cent"]:
        text += (f" Umgerechnet wären es {euro(alt['betrag_umgerechnet_cent'])} €, Differenz "
                 f"{umrechnung.abweichung_text(karte, alt['betrag_umgerechnet_cent'])}.")
    if hinweis:
        text += f" {hinweis}"
    return _meldung("/belege", text, art="erfolg", beleg=beleg["id"])


@router.post("/belege/{beleg_id}/ezb-kurs")
def ezb_kurs(beleg_id: str, request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Kartenbetrag entfernen bzw. einen Kurs von Hand durch den EZB-Kurs ersetzen.

    Der Euro-Betrag von Beleg und Zahlung ist danach der zum EZB-Kurs des
    Zahlungstags (sonst des Belegdatums) umgerechnete -- der Weg zurueck nach
    einem versehentlich bestaetigten Kartenbetrag, und der Weg zum EZB-Kurs,
    wenn ein offline gebuchter Kurs von Hand ihn nicht haette ersetzen duerfen.
    Den Kurs besorgen, BEVOR gesperrt wird: dafuer kann die Kurstabelle
    geladen werden muessen.
    """
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    if (antwort := _geloescht(beleg)) is not None:
        return antwort
    if beleg["waehrung"] == "EUR" or beleg["betrag_fremdwaehrung_cent"] is None:
        return _meldung("/belege", f"Beleg {beleg['id']} ist in Euro — da gibt es nichts "
                                   "umzurechnen.", beleg=beleg["id"])
    if beleg["betrag_karte_cent"] is None and beleg["kurs_quelle"] == "EZB":
        return _meldung("/belege", f"Beleg {beleg['id']} ist schon zum EZB-Kurs umgerechnet.",
                        art="erfolg", beleg=beleg["id"])
    datenordner = _datenordner(request)
    try:
        kurs = umrechnung.ezb_kurs_fuer(conn, datenordner, beleg)
    except kurse.KeinKurs as fehler:
        if not (beleg["betrag_karte_cent"] is not None and beleg["kurs_quelle"] == "MANUELL"):
            return _meldung("/belege", f"Beleg {beleg['id']}: {fehler} Der Euro-Betrag bleibt, "
                                       "wie er ist.", beleg=beleg["id"])
        kurs = None  # die EZB fuehrt die Waehrung nicht: zurueck zum Kurs von Hand
    else:
        kurse.sichere_fassung(datenordner, kurs.sha256)
    try:
        alt, hinweis = umrechnung.zum_ezb_kurs(conn, beleg["id"], kurs, version=beleg["version"])
    except BelegFehler as fehler:
        return _meldung("/belege", str(fehler), beleg=beleg["id"])
    neu = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg["id"],)).fetchone()
    was = ("Kartenbetrag entfernt" if alt["betrag_karte_cent"] is not None
           else "Zum EZB-Kurs umgerechnet")
    fremd = f"{euro(neu['betrag_fremdwaehrung_cent'])} {neu['waehrung']}"
    art = "EZB-Kurs" if neu["kurs_quelle"] == "EZB" else "Kurs von Hand"
    text = (f"Beleg {beleg['id']}: {was} — {fremd} zum {art} {kurse.kurs_de(neu['kurs'])} vom "
            f"{datum_de(neu['kurs_datum'])} = {euro(neu['betrag_brutto_cent'])} € (vorher "
            f"{euro(alt['betrag_brutto_cent'])} €). Die EÜR rechnet ab jetzt damit.")
    if hinweis:
        text += f" {hinweis}"
    return _meldung("/belege", text, art="erfolg", beleg=beleg["id"])


@router.post("/belege/{beleg_id}/kurs-pruefen")
def kurs_pruefen(beleg_id: str, request: Request,
                 conn: sqlite3.Connection = Depends(hole_conn)):
    """Rechnet die Umrechnung eines Belegs aus der gespeicherten Kurstabelle nach."""
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    if (antwort := _geloescht(beleg)) is not None:
        return antwort
    if beleg["waehrung"] == "EUR" or beleg["betrag_fremdwaehrung_cent"] is None:
        return _meldung("/belege", f"Beleg {beleg['id']} ist in Euro — da gibt es keinen Kurs "
                                   "zu prüfen.", beleg=beleg["id"])
    pruefung = umrechnung.pruefe(conn, _datenordner(request), beleg["id"])
    _kurspruefung_protokollieren(conn, pruefung, beleg["id"])
    if pruefung.befunde:
        return _meldung("/belege", f"Beleg {beleg['id']}: "
                        + "; ".join(str(b["befund"]) for b in pruefung.befunde) + ".",
                        beleg=beleg["id"])
    if not beleg["kurs"]:
        return _meldung("/belege", f"Beleg {beleg['id']}: Euro-Betrag und Zahlung stimmen "
                                   "überein; einen Kurs gibt es an diesem Beleg nicht.",
                        art="erfolg", beleg=beleg["id"])
    fremd = f"{euro(beleg['betrag_fremdwaehrung_cent'])} {beleg['waehrung']}"
    if beleg["kurs_quelle"] != "EZB":
        # Ein Kurs von Hand "stimmt" nicht mit sich selbst ueberein: gesagt
        # wird, was nachgerechnet ist, und ob die EZB fuer den Tag einen Kurs
        # haette (dann waere es ein Befund).
        if kurse.stand(_datenordner(request)) is None:
            ezb = ("ohne EZB-Kurstabelle im Datenordner lässt sich nicht prüfen, ob die EZB "
                   "für diesen Tag einen Kurs hat")
        else:
            ezb = (f"die gespeicherte EZB-Kurstabelle hat für {beleg['waehrung']} an diesem "
                   "Tag keinen Kurs, der Kurs von Hand gilt")
        return _meldung(
            "/belege",
            f"Beleg {beleg['id']}: {fremd} zum Kurs von Hand {kurse.kurs_de(beleg['kurs'])} "
            f"vom {datum_de(beleg['kurs_datum'])} ergeben "
            f"{euro(beleg['betrag_umgerechnet_cent'])} € — nachgerechnet; {ezb}.",
            art="erfolg", beleg=beleg["id"],
        )
    return _meldung(
        "/belege",
        f"Beleg {beleg['id']}: Kurs {kurse.kurs_de(beleg['kurs'])} vom "
        f"{datum_de(beleg['kurs_datum'])} stimmt mit der gespeicherten EZB-Kurstabelle "
        f"überein, {fremd} ergeben {euro(beleg['betrag_umgerechnet_cent'])} €.",
        art="erfolg", beleg=beleg["id"],
    )


def _kurspruefung_protokollieren(conn: sqlite3.Connection, pruefung: umrechnung.Pruefung,
                                 beleg_id: int | None) -> None:
    """Die Pruefung gehoert ins Journal -- wie die Belegintegritaet: sie ist der
    Nachweis, dass nachgerechnet wurde, und mit welchem Ergebnis."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        db.protokolliere(
            conn, "KURS_PRUEFUNG", "beleg", beleg_id,
            wert_neu={"geprueft": pruefung.geprueft, "befunde": len(pruefung.befunde)},
            begruendung="; ".join(f"{b['beleg_id']}: {b['befund']}"
                                  for b in pruefung.befunde) or None,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@router.post("/belege/{beleg_id}/geprueft")
def geprueft(beleg_id: str, conn: sqlite3.Connection = Depends(hole_conn)):
    """"Stimmt so": der Nutzer bestaetigt einen automatisch gebuchten Beleg.

    Gesetzt wird nur ``geprueft_am`` -- und nur, wenn es noch leer ist: die
    Bedingung steht im UPDATE selbst, damit ein Doppelklick keinen zweiten
    Zeitpunkt und keinen zweiten Journaleintrag schreibt. Betrag und Datum
    bleiben, wie sie sind; was nicht stimmt, ist kein Fall fuer "Stimmt so".
    """
    beleg = _beleg(conn, beleg_id)
    if beleg is None:
        return _meldung("/belege", "Diesen Beleg gibt es nicht.")
    if (antwort := _geloescht(beleg)) is not None:
        return antwort
    zeitpunkt = db.jetzt_utc()
    conn.execute("BEGIN IMMEDIATE")
    try:
        geaendert = conn.execute(
            "UPDATE beleg SET geprueft_am = ?, version = version + 1"
            " WHERE id = ? AND geprueft_am IS NULL",
            (zeitpunkt, beleg["id"]),
        ).rowcount
        if geaendert:
            db.protokolliere(conn, "BELEG_GEPRUEFT", "beleg", beleg["id"], feld="geprueft_am",
                             wert_neu=zeitpunkt)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if not geaendert:
        return _meldung("/belege", f"Beleg {beleg['id']} war schon bestätigt.", art="erfolg",
                        beleg=beleg["id"])
    return _meldung("/belege", f"Beleg {beleg['id']} bestätigt.", art="erfolg",
                    beleg=beleg["id"])


@router.post("/belege/eingang/{dateiname}/wegwerfen")
def wegwerfen(dateiname: str, request: Request):
    """Loescht eine Datei aus dem EINGANG -- nie aus dem Archiv.

    Im Eingang liegt noch kein Beleg, sondern eine hochgeladene Datei: ein
    Fehlgriff, eine Dublette, ein Scan in zwei Teilen. Die wegzuwerfen ist
    richtig. Ein gebuchter Beleg gehoert dagegen zur Buchfuehrung: er wird
    nur als geloescht MARKIERT (app/routen/belegkorrektur.py), Datensatz und
    Datei bleiben -- dafuer stehen Trigger und § 147 AO.
    """
    pfad = _eingangsdatei(_datenordner(request), dateiname)
    if pfad is None:
        return _meldung("/belege", "Diese Datei liegt nicht im Eingang.")
    name = pipeline.original_name_von(pfad)
    if not pipeline.eingang_wegwerfen(pfad):
        # Doppelklick, oder die Datei wurde gerade gebucht und verschoben.
        return _meldung("/belege", "Diese Datei liegt nicht im Eingang.")
    return _meldung("/belege", f"{name} aus dem Eingang entfernt.", art="erfolg")


# ------------------------------------------------------------- Belegintegritaet

@router.post("/belege/integritaet")
def integritaet(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Vergleicht jede archivierte Datei mit ihrer Pruefsumme.

    Ohne diesen Lauf ist die gespeicherte Pruefsumme eine Behauptung.
    """
    gesetzt: list[str] = []
    befunde = pipeline.pruefe_integritaet(conn, _datenordner(request), rechte_gesetzt=gesetzt)
    rechte = (
        f" Die Dateirechte von {_zahlwort(len(gesetzt), 'Belegdatei', 'Belegdateien')}"
        " waren nach einer Wiederherstellung oder Kopie wieder beschreibbar und sind"
        " jetzt wieder schreibgeschützt."
        if gesetzt else ""
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        db.protokolliere(
            conn, "BELEG_INTEGRITAET", "beleg", None,
            wert_neu={"befunde": len(befunde), "rechte_zurueckgesetzt": len(gesetzt)},
            begruendung="; ".join(
                [f"{b['beleg_id'] or b['pfad']}: {b['befund']}" for b in befunde]
                + [f"{pfad}: Dateirechte auf 0444 zurückgesetzt" for pfad in gesetzt]
            ) or None,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Und jede Umrechnung in Euro, nachgerechnet aus der gespeicherten
    # Kurstabelle. Ohne Fremdwaehrungsbelege sagt die Meldung dazu nichts.
    kurspruefung = umrechnung.pruefe(conn, _datenordner(request))
    kurstext = ""
    if kurspruefung.geprueft:
        _kurspruefung_protokollieren(conn, kurspruefung, None)
        if kurspruefung.befunde:
            kurstext = " Umrechnungen: " + "; ".join(
                f"Beleg {b['beleg_id']}: {b['befund']}" for b in kurspruefung.befunde[:5]) + "."
        else:
            kurstext = (f" {_zahlwort(kurspruefung.geprueft, 'Umrechnung', 'Umrechnungen')} "
                        "in Euro nachgerechnet, alle stimmen.")

    if not befunde and not kurspruefung.befunde:
        anzahl = conn.execute("SELECT COUNT(*) AS n FROM belegdatei").fetchone()["n"]
        return _meldung(
            "/belege",
            f"{_zahlwort(anzahl, 'Belegdatei', 'Belegdateien')} geprüft, "
            f"alle unverändert.{rechte}{kurstext}",
            art="erfolg",
        )
    if not befunde:
        anzahl = conn.execute("SELECT COUNT(*) AS n FROM belegdatei").fetchone()["n"]
        return _meldung(
            "/belege",
            f"{_zahlwort(anzahl, 'Belegdatei', 'Belegdateien')} unverändert.{rechte}{kurstext}",
        )
    text = "; ".join(
        (f"Beleg {b['beleg_id']}" if b["beleg_id"] else f"Datei {b['pfad']}") + f": {b['befund']}"
        for b in befunde[:5]
    )
    satz = f"{_zahlwort(len(befunde), 'Befund', 'Befunde')} — {text}"
    # Ein Befund endet meist ohne Satzzeichen ("... wurde verändert"), einer
    # mit Fehlertext schon mit Punkt. Ohne diese Pruefung lief der Hinweis zu
    # den Dateirechten ohne Punkt in den Befund hinein.
    if not satz.endswith((".", "!", "?")):
        satz += "."
    return _meldung("/belege", f"{satz}{rechte}{kurstext}")


def _zahlwort(anzahl: int, einzahl: str, mehrzahl: str) -> str:
    """Zahl mit passendem Wort.

    >>> _zahlwort(1, "Befund", "Befunde"), _zahlwort(3, "Befund", "Befunde")
    ('1 Befund', '3 Befunde')
    """
    return f"{anzahl} {einzahl if anzahl == 1 else mehrzahl}"


# ------------------------------------------------------- Eigenbelege und Reisen

@router.get("/belege/eigenbeleg", response_class=HTMLResponse)
def eigenbeleg_formular(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Ein Beleg ohne Fremddokument.

    Nicht versteckt und nicht als Notlösung: zwei Kategorien des Katalogs
    kommen überhaupt nie mit einem Fremdbeleg — die Verpflegungspauschale, weil
    abziehbar die Pauschbeträge sind und nicht die Essensbelege, und das
    Kilometergeld, weil der Nachweis eine Aufstellung ist.
    """
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    return _eigenbeleg_seite(request, conn)


def _eigenbeleg_seite(
    request: Request,
    conn: sqlite3.Connection,
    *,
    fehler: str | None = None,
    eingabe: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Das Eigenbeleg-Formular. Nach einer abgewiesenen Eingabe mit den Werten
    des Nutzers neu gezeigt (422): eine Weiterleitung verwarf die Begruendung,
    und die ist bei einem Eigenbeleg das Aufwendigste am Formular."""
    jahr = int(db.heute()[:4])
    return VORLAGEN.TemplateResponse(
        request,
        "eigenbeleg.html",
        {
            "kategorien": kategorien.katalog(conn, bereich=kategorien.BEREICH_AUSGABEN),
            "reisen": eigenbeleg.reisen(conn, grenze=20),
            "satz": pauschalen.fuer(jahr),
            "jahr": jahr,
            "heute": db.heute(),
            "mindestlaenge": eigenbeleg.GRUND_MINDESTLAENGE,
            "eingabe": eingabe or {},
            "fehler": fehler or request.query_params.get("fehler"),
            "erfolg": request.query_params.get("erfolg"),
        },
        status_code=status_code,
    )


@router.post("/belege/eigenbeleg")
def eigenbeleg_speichern(
    request: Request,
    belegdatum: str = Form(""),
    brutto_euro: str = Form(""),
    bezeichnung: str = Form(""),
    grund: str = Form(""),
    kategorie_id: str = Form(""),
    quote: str = Form("100"),
    reise_id: str = Form(""),
    bezahlt_am: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    """Speichert einen Eigenbeleg.

    Kategorie und Reise werden geprueft wie in der Pruefmaske: "abc" oder eine
    unbekannte Kennung endete sonst als Absturzseite, und eine
    Einnahmen-Kategorie stand als Betriebsausgabe in der EUeR.
    """
    from app.logik.kunden import euro_nach_cent

    werte = {
        "belegdatum": belegdatum, "brutto_euro": brutto_euro, "bezeichnung": bezeichnung,
        "grund": grund, "kategorie_id": kategorie_id, "quote": quote,
        "reise_id": reise_id, "bezahlt_am": bezahlt_am,
    }

    def zurueck(text: str) -> HTMLResponse:
        return _eigenbeleg_seite(request, conn, fehler=text, eingabe=werte, status_code=422)

    try:
        brutto = euro_nach_cent(brutto_euro)
    except ArithmeticError as fehler:
        return zurueck(str(fehler))
    except ValueError:
        return zurueck(f"„{brutto_euro.strip()[:40]}“ ist kein Betrag.")
    try:
        quote_wert = _quote(quote)
        kategorie = _ausgabenkategorie(conn, kategorie_id)
        reise = _reise(conn, reise_id)
        gezahlt_am = _zahlungsdatum(bezahlt_am)
        beleg_id = eigenbeleg.erfasse(
            conn,
            belegdatum=belegdatum.strip(),
            brutto_cent=brutto or 0,
            grund=grund,
            bezeichnung=bezeichnung,
            kategorie_id=kategorie,
            quote=quote_wert,
            reise_id=reise,
            bezahlt_am=gezahlt_am,
        )
    except BelegFehler as fehler:
        return zurueck(str(fehler))
    except sqlite3.IntegrityError as fehler:
        # Zwischen Pruefung und Schreiben geloescht -- oder ein Trigger.
        if "FOREIGN KEY" in str(fehler):
            return zurueck("Kategorie oder Reise gibt es nicht mehr — bitte die Auswahl "
                           "prüfen.")
        return zurueck("Die Datenbank hat den Eigenbeleg abgewiesen — bitte Begründung "
                       "und Betrag prüfen.")
    if kategorie is not None and gezahlt_am:
        return _meldung("/belege", f"Eigenbeleg {beleg_id} als Betriebsausgabe "
                                   f"{gezahlt_am[:4]} erfasst.", art="erfolg")
    # Nur mit Kategorie UND Zahlungsdatum steht er in einer Zeile der EUeR.
    # Ohne Kategorie hiess es trotzdem "als Betriebsausgabe 2026 erfasst" --
    # und die EUeR zeigte ihn nirgends.
    text = f"Eigenbeleg {beleg_id} erfasst."
    if kategorie is None:
        text += (" Ohne Kategorie geht er in keine Zeile der EÜR — bitte unter "
                 "„Ändern“ eine Kategorie wählen.")
    if not gezahlt_am:
        text += (" Ohne Zahlungsdatum zählt er noch in keinem EÜR-Jahr (§ 11 Abs. 2 "
                 "EStG) — sobald bezahlt, in der Belegliste unter „Ändern“ die Zahlung "
                 "erfassen.")
    return _meldung("/belege", text, art="erfolg")


@router.get("/reisen", response_class=HTMLResponse)
def reiseliste(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    """Geschäftsreisen als Container.

    Der einzige Vorgang, der mehrere Kennzahlen braucht: ein Konferenzbesuch
    zerfällt in Ticket (281), Hotel (221), Verpflegungspauschale (171) und
    Fahrt (146 oder 147).
    """
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)
    return _reisen_seite(request, conn)


def _reisen_seite(
    request: Request,
    conn: sqlite3.Connection,
    *,
    fehler: str | None = None,
    eingabe: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Die Reisenseite; nach einer abgewiesenen neuen Reise mit den Eingaben
    des Nutzers im Formular (422) statt einer Weiterleitung, die sie verwarf."""
    alle = eigenbeleg.reisen(conn)
    return VORLAGEN.TemplateResponse(
        request,
        "reisen.html",
        {
            "reisen": alle,
            "belege": {r["id"]: eigenbeleg.reisebelege(conn, r["id"]) for r in alle},
            "kategorien": kategorien.katalog(conn, bereich=kategorien.BEREICH_AUSGABEN),
            "satz": pauschalen.fuer(int(db.heute()[:4])),
            # Je Reise der Satz ihres REISEJAHRES: die Warnung vor einem
            # Ersatzjahr gehoert an die Reise, nicht an das laufende Jahr.
            "saetze": {r["id"]: pauschalen.fuer(int(r["abreise_am"][:4])) for r in alle},
            "heute": db.heute(),
            "eingabe": eingabe or {},
            "fehler": fehler or request.query_params.get("fehler"),
            "erfolg": request.query_params.get("erfolg"),
        },
        status_code=status_code,
    )


@router.post("/reisen")
def reise_anlegen(
    request: Request,
    bezeichnung: str = Form(""),
    anlass: str = Form(""),
    ziel: str = Form(""),
    abreise_am: str = Form(""),
    rueckkehr_am: str = Form(""),
    inland: str = Form("1"),
    abwesend_stunden: str = Form(""),
    notiz: str = Form(""),
    # Versteckt "0" plus Kontrollkaestchen "1": angekreuzt kommen beide, sonst
    # nur die "0". Fehlt das Feld ganz (kein Formular, sondern ein Aufruf von
    # anderswo), entscheidet die Logik nach der Dauer der Reise.
    uebernachtung: list[str] = Form([]),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    werte = {
        "bezeichnung": bezeichnung, "anlass": anlass, "ziel": ziel,
        "abreise_am": abreise_am, "rueckkehr_am": rueckkehr_am, "inland": inland,
        "abwesend_stunden": abwesend_stunden, "notiz": notiz,
        "uebernachtung": "1" if "1" in uebernachtung else "0",
    }

    def zurueck(text: str) -> HTMLResponse:
        return _reisen_seite(request, conn, fehler=text, eingabe=werte, status_code=422)

    try:
        stunden = (ganzzahl(abwesend_stunden, feld="Abwesenheit")
                   if abwesend_stunden.strip() else None)
    except ValueError:
        return zurueck(f"„{abwesend_stunden.strip()[:40]}“ sind keine Stunden.")
    try:
        eigenbeleg.reise_anlegen(
            conn, bezeichnung=bezeichnung, anlass=anlass, ziel=ziel,
            abreise_am=abreise_am.strip(), rueckkehr_am=rueckkehr_am.strip(),
            inland=inland == "1", abwesend_stunden=stunden, notiz=notiz,
            uebernachtung=("1" in uebernachtung) if uebernachtung else None,
        )
    except BelegFehler as fehler:
        return zurueck(str(fehler))
    return RedirectResponse("/reisen", status_code=303)


@router.post("/reisen/{reise_id}/verpflegung")
def verpflegung(reise_id: str, conn: sqlite3.Connection = Depends(hole_conn)):
    # Die Kennung als Text: als int nahm FastAPI auch 2**63 an (Absturzseite).
    kennzahl = kennung(reise_id)
    if kennzahl is None:
        return _meldung("/reisen", "Diese Reise gibt es nicht.")
    reise_id = kennzahl
    try:
        beleg_id = eigenbeleg.verpflegung_buchen(conn, reise_id)
    except BelegFehler as fehler:
        return _meldung("/reisen", str(fehler))
    beleg = conn.execute(
        "SELECT betrag_brutto_cent, belegdatum FROM beleg WHERE id = ?", (beleg_id,)
    ).fetchone()
    reise = eigenbeleg.lies_reise(conn, reise_id)
    satz = pauschalen.fuer(int(reise.abreise_am[:4]))
    text = (
        f"Verpflegungspauschale {euro(beleg['betrag_brutto_cent'])} € als Eigenbeleg "
        f"{beleg_id} und als Betriebsausgabe {beleg['belegdatum'][:4]} erfasst. "
        "Ist das Frühstück im Hotelpreis enthalten, mindert das die Pauschale um "
        "20 Prozent des Volltagessatzes — das bitte von Hand nachtragen."
    )
    if satz.veraltet:
        text += f" {satz.hinweis}"
    return _meldung("/reisen", text, art="erfolg")


@router.post("/reisen/{reise_id}/kilometer")
def kilometer(
    reise_id: str,
    datum: str = Form(""),
    km: str = Form(""),
    strecke: str = Form(""),
    zweck: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    reise = kennung(reise_id)
    if reise is None:
        return _meldung("/reisen", "Diese Reise gibt es nicht.")
    try:
        # Ueber ganzzahl: "²" und Zahlen ab 2**63 endeten sonst als
        # Absturzseite; die Obergrenze je Fahrt prueft kilometer_buchen.
        anzahl = ganzzahl(km, feld="Kilometer")
    except ValueError:
        return _meldung("/reisen", f"„{km.strip()[:40]}“ sind keine Kilometer.")
    try:
        beleg_id = eigenbeleg.kilometer_buchen(
            conn, datum=datum.strip(), kilometer=anzahl, strecke=strecke,
            zweck=zweck, reise_id=reise,
        )
    except BelegFehler as fehler:
        return _meldung("/reisen", str(fehler))
    beleg = conn.execute(
        "SELECT betrag_brutto_cent, belegdatum FROM beleg WHERE id = ?", (beleg_id,)
    ).fetchone()
    text = (f"Fahrtkosten {euro(beleg['betrag_brutto_cent'])} € als Eigenbeleg {beleg_id} "
            f"und als Betriebsausgabe {beleg['belegdatum'][:4]} erfasst.")
    satz = pauschalen.fuer(int(beleg["belegdatum"][:4]))
    if satz.veraltet:
        text += f" {satz.hinweis}"
    return _meldung("/reisen", text, art="erfolg")

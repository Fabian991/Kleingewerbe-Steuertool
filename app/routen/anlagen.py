"""Anlagenverzeichnis und GWG-Verzeichnis.

Pflichtbestandteil der EÜR, sobald Anlagevermögen existiert (Anlage AVEÜR) —
und kein Formalismus: die AfA-Beträge der EÜR kommen von hier.

Der Einstieg liegt beim Beleg, nicht bei einem leeren Formular: wer eine
Hardware-Rechnung erfasst, soll von dort aus das Anlagegut anlegen können.
Sonst entsteht das Verzeichnis erst beim Jahresabschluss, aus der Erinnerung.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.filter import datum_de
from app.logik import angaben, eingabe, pauschalen
from app.logik import anlagen as logik
from app.logik.kunden import euro_nach_cent
from app.logik.rundung import euro
from app.web import VORLAGEN, betrieb_oder_none, filterzahl, hole_conn

router = APIRouter()

ARTEN = ("BEWEGLICH", "IMMATERIELL")


def _meldung(ziel: str, text: str, art: str = "fehler") -> RedirectResponse:
    trenner = "&" if "?" in ziel else "?"
    return RedirectResponse(f"{ziel}{trenner}{art}={quote(text)}", status_code=303)


@router.get("/anlagen", response_class=HTMLResponse)
def verzeichnis(
    request: Request,
    jahr: str = "",
    conn: sqlite3.Connection = Depends(hole_conn),
):
    if betrieb_oder_none(conn) is None:
        return RedirectResponse("/einrichtung", status_code=303)

    # Leer oder Unsinn in der Adresse heisst: Standardjahr (wie bei /belege).
    gewaehlt = filterzahl(jahr) or int(db.heute()[:4])
    eintraege = logik.verzeichnis(conn, gewaehlt)
    satz = pauschalen.fuer(gewaehlt)
    # Waehlbar sind die Jahre, in denen etwas im Verzeichnis steht: jedes Jahr
    # einer AfA-Reihe, die Abgangsjahre, das laufende und das angefragte Jahr.
    # Nur die Anschaffungsjahre anzubieten liess die Zwischenjahre aus -- und
    # ein per URL angefragtes Jahr stand dann nicht im Dropdown, obwohl die
    # Ueberschrift es nannte.
    jahre = {int(z["jahr"]) for z in conn.execute(
        "SELECT DISTINCT j.jahr FROM anlagegut_afa_jahr j"
        " JOIN v_anlagegut_gueltig g ON g.id = j.anlagegut_id")}
    jahre |= {int(e["anschaffung_am"][:4]) for e in eintraege}
    jahre |= {int(e["abgang_am"][:4]) for e in eintraege if e["abgang_am"]}
    jahre |= {int(db.heute()[:4]), gewaehlt}
    return VORLAGEN.TemplateResponse(
        request,
        "anlagen.html",
        {
            "eintraege": eintraege,
            "jahr": gewaehlt,
            "jahre": sorted(jahre, reverse=True),
            # Bezahlte Belege in der Anlagekategorie (KZ 130/131) ohne
            # Anlagegut: sie stehen mit vollem Betrag in der AfA-Zeile.
            "belege_ohne_gut": logik.belege_in_anlagekategorie_ohne_gut(conn),
            "afa_summe": logik.afa_summe(conn, gewaehlt),
            "gwg": logik.gwg_verzeichnis(conn, gewaehlt),
            "satz": satz,
            "grenze_netto": satz["gwg_grenze_netto_cent"],
            "grenze_brutto_19": round(satz["gwg_grenze_netto_cent"] * 1.19),
            "verzeichnis_ab": satz["gwg_verzeichnis_ab_netto_cent"],
            "nutzungsdauern": logik.NUTZUNGSDAUERN,
            "arten": ARTEN,
            "heute": db.heute(),
            # Belege, die als Anlagegut markiert sind, aber noch keines haben.
            # Das ist die Lücke, die sonst bis zum Jahresabschluss offen bleibt.
            "offene_belege": conn.execute(
                """
                SELECT b.*, l.name AS lieferant
                  FROM v_beleg_gueltig b
                  LEFT JOIN lieferant l ON l.id = b.lieferant_id
                 WHERE b.ist_anlagegut = 1
                   AND NOT EXISTS (SELECT 1 FROM v_anlagegut_gueltig a WHERE a.beleg_id = b.id)
                 ORDER BY b.belegdatum DESC
                """
            ).fetchall(),
            # "Angaben fehlen" (app/logik/angaben.py): Gueter aus WISO, von
            # denen nur die Jahres-AfA bekannt ist -- mit den fehlenden Feldern.
            "angaben": angaben.offene(conn, "ANLAGEGUT"),
            # Einnahmen "Verkauf eines Anlageguts" ohne Abgang: im Abgang als
            # Erloes waehlbar -- und solange ohne Abgang ein Hinweis (die AfA
            # laeuft weiter, der Restbuchwert fehlt).
            "verkaeufe": logik.verkaufseinnahmen(conn),
            "einstufung_texte": {},
            "ausgabenkategorien": [],
            "fehler": request.query_params.get("fehler"),
            "erfolg": request.query_params.get("erfolg"),
            "warnung": request.query_params.get("warnung"),
        },
    )


@router.post("/anlagen/{anlagegut_id}/nutzungsdauer")
def nutzungsdauer(anlagegut_id: str, jahre: str = Form(""),
                  conn: sqlite3.Connection = Depends(hole_conn)):
    """Setzt die Nutzungsdauer neu -- etwa eine aus WISO abgeleitete, die nicht stimmt.

    Die AfA-Reihe wird neu gerechnet, solange sich dabei kein Vorjahr aendert
    (logik.nutzungsdauer_aendern).
    """
    kennung = eingabe.kennung(anlagegut_id)
    try:
        dauer = eingabe.ganzzahl(jahre, feld="Nutzungsdauer in Jahren", mindestens=1,
                                 hoechstens=logik.NUTZUNGSDAUER_HOECHSTENS)
        if kennung is None:
            raise logik.AnlagenFehler("Dieses Anlagegut gibt es nicht.")
        reihe = logik.nutzungsdauer_aendern(conn, kennung, dauer)
    except (ValueError, logik.AnlagenFehler) as fehler:
        return _meldung("/anlagen", str(fehler))
    return _meldung("/anlagen", f"Nutzungsdauer {dauer} {'Jahr' if dauer == 1 else 'Jahre'}: "
                    + ", ".join(f"AfA {j.jahr} {euro(j.afa_cent)} €" for j in reihe) + ".",
                    art="erfolg")


@router.post("/anlagen")
def anlegen(
    bezeichnung: str = Form(""),
    anschaffung_am: str = Form(""),
    brutto_euro: str = Form(""),
    netto_euro: str = Form(""),
    ust_satz: str = Form("19"),
    nutzungsdauer_jahre: str = Form(""),
    art: str = Form("BEWEGLICH"),
    ist_it: str = Form(""),
    beleg_id: str = Form(""),
    notiz: str = Form(""),
    aufteilungsquote: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    # euro_nach_cent nennt den Betrag selbst: "kein Betrag" oder "als Betrag
    # unplausibel groß" (ab 1 Mrd. €; vorher sprengte 2**63 die Bindung an
    # SQLite).
    try:
        brutto = euro_nach_cent(brutto_euro)
    except (ValueError, ArithmeticError) as fehler:
        return _meldung("/anlagen", f"Brutto: {fehler}")
    try:
        netto = euro_nach_cent(netto_euro)
    except (ValueError, ArithmeticError) as fehler:
        return _meldung("/anlagen", f"Netto: {fehler}")
    try:
        quote = (eingabe.ganzzahl(aufteilungsquote, feld="Betrieblicher Anteil")
                 if aufteilungsquote.strip() else None)
    except ValueError:
        return _meldung("/anlagen", f"„{aufteilungsquote.strip()[:40]}“ ist kein Prozentsatz.")
    if not brutto:
        return _meldung("/anlagen", "Der Bruttobetrag fehlt — er ist der Buchungswert.")

    satz_promille = None
    if ust_satz.strip():
        # float() nimmt auch "inf", "nan" und "1e400" (= inf); round(inf)
        # warf einen OverflowError und der Nutzer sah die 500-Seite.
        try:
            satz = float(ust_satz.strip().replace(",", "."))
        except ValueError:
            satz = math.nan
        if not math.isfinite(satz) or not 0 <= satz <= 100:
            return _meldung("/anlagen", f"„{ust_satz.strip()[:40]}“ ist kein Steuersatz — er "
                            "muss zwischen 0 und 100 Prozent liegen.")
        satz_promille = int(round(satz * 10))
    try:
        # Serverseitig 1 bis 50 Jahre: das max=50 im Formular gilt nur im
        # Browser, und 2**63-1 hielt die AfA-Schleife endlos fest.
        dauer = (eingabe.ganzzahl(nutzungsdauer_jahre, feld="Nutzungsdauer in Jahren",
                                  mindestens=1, hoechstens=logik.NUTZUNGSDAUER_HOECHSTENS)
                 if nutzungsdauer_jahre.strip() else None)
    except ValueError as fehler:
        return _meldung("/anlagen", str(fehler))
    # Das versteckte Feld fuellt JavaScript; was dort steht, ist nicht
    # zwingend eine Zahl -- und eine ab 2**63 sprengte die Bindung an SQLite.
    # Was kein Beleg ist, gibt es nicht.
    beleg = eingabe.kennung(beleg_id) if beleg_id.strip() else None
    if beleg_id.strip() and beleg is None:
        return _meldung("/anlagen", "Diesen Beleg gibt es nicht.")

    try:
        gut_id = logik.anlegen(
            conn,
            bezeichnung=bezeichnung,
            anschaffung_am=anschaffung_am.strip(),
            ak_brutto_cent=brutto,
            ak_netto_cent=netto,
            ust_satz_promille=satz_promille,
            nutzungsdauer_jahre=dauer,
            art=art if art in ARTEN else "BEWEGLICH",
            ist_it=ist_it == "1",
            beleg_id=beleg,
            notiz=notiz,
            aufteilungsquote=quote,
        )
    except logik.AnlagenFehler as fehler:
        return _meldung("/anlagen", str(fehler))
    except sqlite3.IntegrityError as fehler:
        return _meldung("/anlagen", f"Die Datenbank hat das Anlagegut abgewiesen ({fehler}).")

    zeile = conn.execute(
        "SELECT * FROM anlagegut WHERE id = ?", (gut_id,)
    ).fetchone()
    einstufung = logik.stufe_ein(
        zeile["ak_netto_cent"], jahr=int(zeile["anschaffung_am"][:4]),
        art=zeile["art"], ist_it=zeile["methode"] == logik.EIN_JAHR_IT,
    )
    text = f"{zeile['bezeichnung']}: {einstufung.begruendung}"
    betrieb = betrieb_oder_none(conn)
    beginn = betrieb["beginn_am"] if betrieb is not None else None
    if beginn and zeile["anschaffung_am"] < beginn:
        # Was vor dem Betriebsbeginn gekauft wurde, kommt als Einlage ins
        # Betriebsvermoegen -- zum Teilwert, nicht zu den Anschaffungskosten.
        # Das Werkzeug rechnet mit den Anschaffungskosten; der Nutzer muss den
        # Wert pruefen, und das soll er hier lesen und nicht im Bescheid.
        text += (
            f" Die Anschaffung am {datum_de(zeile['anschaffung_am'])} liegt vor dem "
            f"Betriebsbeginn ({datum_de(beginn)}): Einlage zum Teilwert, § 6 Abs. 1 "
            "Nr. 5 EStG — bitte prüfen, ob die Anschaffungskosten noch der richtige "
            "Wert sind."
        )
    return _meldung("/anlagen", text, art="erfolg")


@router.post("/anlagen/{anlagegut_id}/abgang/zurueck")
def abgang_zurueck(
    anlagegut_id: int,
    grund: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    try:
        logik.abgang_zuruecknehmen(conn, anlagegut_id, grund=grund)
    except logik.AnlagenFehler as fehler:
        return _meldung("/anlagen", str(fehler))
    return _meldung("/anlagen", "Abgang zurückgenommen — die Abschreibungsreihe ist wieder "
                    "vollständig.", art="erfolg")


@router.post("/anlagen/{anlagegut_id}/abgang")
def abgang(
    anlagegut_id: int,
    am: str = Form(""),
    grund: str = Form(""),
    erloes_euro: str = Form(""),
    erloes_einnahme: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    try:
        erloes = euro_nach_cent(erloes_euro) or 0
    except (ValueError, ArithmeticError) as fehler:
        return _meldung("/anlagen", f"Erlös: {fehler}")
    # "" = nicht gewaehlt (dann prueft die Logik, ob der Erloes schon als
    # Einnahme da ist), "neu" = ausdruecklich ein anderer Verkauf, sonst die
    # Einnahme, die der Erloes ist.
    einnahme_id = eingabe.kennung(erloes_einnahme) if erloes_einnahme not in ("", "neu") \
        else None
    try:
        # Hier streng: ein Abgang liegt nie in der Zukunft. Vorher wurde ein
        # 31.12.2099 mit "Abgang erfasst" angenommen und schnitt die
        # AfA-Reihe ab.
        am_iso = eingabe.datum(am, feld="Abgangsdatum", heute=date.fromisoformat(db.heute()))
    except ValueError as fehler:
        return _meldung("/anlagen", str(fehler))
    try:
        logik.abgang(conn, anlagegut_id, am=am_iso, grund=grund, erloes_cent=erloes,
                     erloes_einnahme_id=einnahme_id, erloes_neu=erloes_einnahme == "neu")
    except logik.AnlagenFehler as fehler:
        return _meldung("/anlagen", str(fehler))

    text = "Abgang erfasst."
    if einnahme_id is not None:
        text += (f" Der Erlös ist Einnahme {einnahme_id} und zählt dort in der EÜR (KZ 102) — "
                 "nicht ein zweites Mal; nicht in der § 19-Umsatzgrenze (§ 19 Abs. 2 Satz 2 "
                 "UStG).")
    elif erloes:
        # Der Erloes steht mit dem Abgang schon in der EUeR. Eine Rechnung
        # darueber zaehlte ihn ein zweites Mal (KZ 111) und obendrein im
        # § 19-Umsatzzaehler, wo Anlagenverkaeufe nicht hingehoeren
        # (§ 19 Abs. 2 Satz 2 UStG).
        text += (f" Der Erlös von {euro(erloes)} € steht damit in der EÜR (KZ 102). "
                 "Bitte dafür KEINE Rechnung im Werkzeug schreiben — der Kaufvertrag "
                 "oder die Quittung gehört in den Belegordner. Der Erlös zählt NICHT in "
                 "die § 19-Umsatzgrenze: Umsätze mit Anlagevermögen bleiben dort außer "
                 "Ansatz (§ 19 Abs. 2 Satz 2 UStG).")
    return _meldung("/anlagen", text, art="erfolg")

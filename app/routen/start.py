"""Routen fuer Start, Gesundheitspunkt und Einrichtung."""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.logik import angaben, eingabe, euer, fristen, grenzwert, kunden, steuerschaetzung
from app.logik import backup as backuplogik
from app.logik import nummernkreis as nk
from app.logik.beleg import abholen, jahresordner
from app.web import VORLAGEN, betrieb_oder_none, hole_conn

router = APIRouter()

#: Die Rundungsregeln, die das Schema kennt. "KEINE" ist die Vorbelegung: eine
#: Rundungsregel ohne vertragliche Grundlage ist angreifbar.
RUNDUNGSMODI = ("KEINE", "AUFWAERTS", "KAUFMAENNISCH", "ABWAERTS")

#: Hoechstwert fuer Zaehlerstaende aus dem Formular. Neun Stellen reichen fuer
#: jede denkbare Rechnungsreihe; alles darueber ist ein Tippfehler -- und ab
#: 2^63 scheiterte SQLite daran mit einem Serverfehler statt eines Hinweises.
ZAEHLER_MAX = 999_999_999

#: Zahlungsziel in Tagen: 0 ist "sofort", ein Jahr ist die Obergrenze. Negativ
#: hiesse Faelligkeit vor dem Rechnungsdatum.
ZAHLUNGSZIEL_MAX_TAGE = 365


@router.get("/gesund")
def gesund(conn: sqlite3.Connection = Depends(hole_conn)) -> dict[str, object]:
    version = conn.execute("SELECT MAX(version) v FROM schema_version").fetchone()["v"]
    return {
        "status": "ok",
        "schema_version": version,
        "eingerichtet": betrieb_oder_none(conn) is not None,
    }


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    betrieb = betrieb_oder_none(conn)
    if betrieb is None:
        return RedirectResponse("/einrichtung", status_code=303)

    # Nicht abgerechnet heisst: noch auf keiner FESTGESCHRIEBENEN Rechnung.
    # Zeiten in einem Entwurf zaehlen mit -- der Entwurf stellt nichts aus,
    # und die Fristtabelle unten rechnet genauso. Zaehlte die Karte sie nicht,
    # stuende dort "0,00 h", waehrend darueber eine abgelaufene Frist ueber
    # dieselben Stunden mahnt.
    offen = conn.execute(
        """
        SELECT k.id, k.name,
               COUNT(*)                  AS eintraege,
               SUM(z.dauer_minuten)      AS minuten,
               SUM(CASE WHEN z.rechnung_id IS NULL THEN z.dauer_minuten ELSE 0 END)
                                         AS offene_minuten,
               SUM(CASE WHEN z.rechnung_id IS NOT NULL THEN z.dauer_minuten ELSE 0 END)
                                         AS entwurf_minuten,
               MAX(z.rechnung_id)        AS entwurf_id,
               MIN(z.datum)              AS ab,
               MAX(z.datum)              AS bis
        FROM zeiteintrag z
        JOIN kunde k ON k.id = z.kunde_id
        WHERE (z.rechnung_id IS NULL OR z.rechnung_id IN (
                   SELECT id FROM rechnung WHERE status = 'ENTWURF'))
          AND z.abgeschrieben_am IS NULL
          AND z.abrechnungsmodus <> 'INTERN'
          -- Ein laufender Timer ist noch nicht abrechenbar: der Assistent bietet
          -- ihn nicht an, und mit seinem Platzhalter von einer Minute zaehlte
          -- er hier als "0,02 h, 1 Eintrag" mit.
          AND NOT (z.start_zeit IS NOT NULL AND z.end_zeit IS NULL)
        GROUP BY k.id, k.name
        ORDER BY MIN(z.datum)
        """
    ).fetchall()

    # v_offene_posten filtert Storno-Dokumente, Leistungsnachweise und 0-€-Belege
    # heraus: alle drei erwarten keinen Zahlungseingang.
    # Der Name aus dem Schnappschuss, wie in der Rechnungsliste: das ist der
    # Empfaenger, der auf dem Papier steht. Nach einer Umbenennung des Kunden
    # zeigte die Uebersicht sonst einen Namen, der nie auf der Rechnung stand.
    rechnungen = conn.execute(
        """
        SELECT v.*,
               COALESCE(json_extract(r.kunde_snapshot_json, '$.name'), k.name) AS kunde
        FROM v_offene_posten v
        JOIN rechnung r ON r.id = v.id
        JOIN kunde k ON k.id = v.kunde_id
        ORDER BY v.faellig_am
        """
    ).fetchall()

    jahr = int(db.heute()[:4])
    grenze = grenzwert.stand(conn, jahr)
    # Einnahmen, Ausgaben und Gewinn des Jahres aus derselben Auswertung wie
    # die EUeR-Seite. Die Einnahmen ALLE, nicht nur der § 19-Zaehler: der
    # zaehlt nur das Inland, und wer Auslands- und Plattformerloese hat, sah
    # auf der Uebersicht sonst nur einen Teil seiner Einnahmen.
    euer_jahr = euer.auswertung(conn, jahr)
    einnahmen_summen = euer.einnahmen_summen(euer_jahr.einnahmen)
    backup_alter = _backup_alter(conn)
    steuer_zeilen = _steuer_zeilen(conn, jahr)
    vz_erinnerungen = steuerschaetzung.erinnerungen(conn, _dt.date.fromisoformat(db.heute()))

    return VORLAGEN.TemplateResponse(
        request,
        "dashboard.html",
        {
            "betrieb": betrieb,
            "offen": offen,
            "rechnungen": rechnungen,
            "grenze": grenze,
            "einnahmen": einnahmen_summen,
            "euer_jahr": euer_jahr,
            "jahr": jahr,
            "naechste_nummer": nk.vorschau(conn, jahr),
            "fristen": fristen.offene_fristen(conn),
            "frist_monate": fristen.FRIST_MONATE,
            "eu_frist_tag": fristen.EU_FRIST_TAG,
            "backup_alter": backup_alter,
            "backup_ungeprueft": backup_alter is None and _ungepruefte_sicherung(conn),
            "backup_warnung_ab": backuplogik.WARNUNG_AB_TAGEN,
            "steuer_zeilen": steuer_zeilen,
            # Die naechste Vorauszahlung ab 14 Tagen vorher und jede als "nicht
            # gezahlt" markierte faellige Rate: der Moment, in dem das Geld auf
            # dem Konto liegen muss. Auch ohne Ruecklagenzeile fuer das Jahr.
            "vz_erinnerungen": vz_erinnerungen,
            "vz_jahre_ohne_zeile": sorted({t.termin.year for t in vz_erinnerungen}
                                          - {j for j, _ in steuer_zeilen}),
            "empfaenger_text": steuerschaetzung.EMPFAENGER_TEXT,
            # Bezahlte Belege ohne Kategorie. Sie fallen lautlos aus der EUeR
            # -- nichts ist kaputt, die Zahl ist nur zu niedrig. Genau deshalb
            # gehoert sie dorthin, wo man jeden Tag hinsieht.
            "euer_luecke": euer.fehlende_zuordnung(conn),
            # Als Anlagegut markiert, aber nicht im Verzeichnis: der Betrag
            # fehlt in der EUeR ganz (keine Ausgabe, keine AfA).
            "anlage_offen": euer.anlagegut_ohne_verzeichnis(conn),
            # Automatisch gebucht und bezahlt, noch nicht mit "Stimmt so"
            # bestaetigt: zaehlt schon in der EUeR.
            "unbestaetigt": euer.unbestaetigt_bezahlt(conn),
            # "Angaben fehlen" aus dem WISO-Import (app/logik/angaben.py): eine
            # Zeile mit der Anzahl, die Formulare stehen auf /angaben.
            "angaben_offen": angaben.anzahl_offen(conn),
            # Was das Abholen aus dem Eingangsordner beim Start getan hat: die
            # Uebersicht ist die erste Seite danach.
            "belegordner": getattr(request.app.state, "belegordner", None),
            "belegordner_ergebnis": abholen.aktuelles_ergebnis(
                getattr(request.app.state, "belegordner", None), conn),
            # Ergebnis einer Schnellaktion ("Bezahlt ...", "Versendet ...")
            # aus der Liste der offenen Rechnungen.
            "fehler": request.query_params.get("fehler"),
            "hinweis": request.query_params.get("hinweis"),
            "heute": db.heute(),
            # Ordner je Jahr: ab Dezember fehlt der fuers naechste Jahr.
            "jahreswechsel_ordner": jahresordner.jahreswechsel(conn),
        },
    )


def _steuer_zeilen(
    conn: sqlite3.Connection, jahr: int,
) -> list[tuple[int, steuerschaetzung.Ergebnis]]:
    """Die Steuerruecklagen fuer die Uebersicht: (Jahr, Ergebnis), meist eine.

    Das juengste Jahr mit Angaben bis heute -- ein Plan fuer das naechste Jahr
    verdraengt das laufende nicht. Dazu das VORJAHR, solange dort noch etwas
    zurueckzulegen ist: dessen Bescheid kommt erst im laufenden Jahr oder
    spaeter, und eine Angabe fuers neue Jahr (am 1. Januar etwa nur das
    Gehalt) darf die offene Ruecklage nicht von der Uebersicht verdraengen.
    Ohne Ergebnis keine Zeile; ohne jede Zeile zeigt die Seite einen Verweis
    -- eine Zahl ohne Gehalt waere geraten.
    """
    steuer_jahre = steuerschaetzung.gespeicherte_jahre(conn)
    if not steuer_jahre:
        return []
    bis_heute = [j for j in steuer_jahre if j <= jahr]
    erstes = (bis_heute or steuer_jahre)[0]
    zeilen = []
    if (ergebnis := steuerschaetzung.berechne(conn, erstes)) is not None:
        zeilen.append((erstes, ergebnis))
    vorjahr = erstes - 1
    if erstes == jahr and vorjahr in steuer_jahre:
        ergebnis = steuerschaetzung.berechne(conn, vorjahr)
        if ergebnis is not None and ergebnis.offen_cent > 0:
            zeilen.append((vorjahr, ergebnis))
    return zeilen


def _backup_alter(conn: sqlite3.Connection) -> int | None:
    """Tage seit der letzten GEPRUEFTEN Sicherung. ``None``, wenn es keine gibt.

    Bewusst die geprueften: ein ungeprueftes Backup ist kein Backup, und ein
    Zaehler, der es mitzaehlt, wiegt in falscher Sicherheit.

    Der Zeitpunkt steht in UTC (``db.jetzt_utc``), der Nutzer lebt in Ortszeit.
    Verglichen wird deshalb das LOKALE Datum der Sicherung mit dem heutigen:
    sonst war eine Sicherung um 00:30 Uhr in Deutschland laut Karte schon
    einen Tag alt, und die Warnung kam bis zu einen Tag zu frueh.
    """
    letztes = backuplogik.letztes_geprueft(conn)
    if not letztes:
        return None
    try:
        zeitpunkt = _dt.datetime.fromisoformat(letztes)
    except ValueError:
        return None
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.replace(tzinfo=_dt.UTC)
    tag = zeitpunkt.astimezone().date()
    return (_dt.date.today() - tag).days


def _ungepruefte_sicherung(conn: sqlite3.Connection) -> bool:
    """Gibt es ein Archiv, das noch nie geprueft wurde und noch da ist?

    Dann ist der naechste Schritt "pruefen", nicht "sichern" -- die Karte muss
    dorthin fuehren, sonst sichert der Nutzer ein zweites Mal und prueft nie.
    """
    return any(
        eintrag["vorhanden"] and not eintrag["geprueft"]
        for eintrag in backuplogik.liste(conn)
    )


def _naechste_seq(conn: sqlite3.Connection, jahr: int) -> int | None:
    """Der Zaehlerstand des LAUFENDEN Jahres -- die Zahl, die das Dashboard als
    naechste Nummer zeigt. ``None`` ohne jeden Kreis.

    Dieselbe Wahl wie :func:`nk.vorschau`: der Kreis dieses Jahres oder der
    durchlaufende; fehlt beides, bei jaehrlichem Neustart die 1, sonst der
    Stand des bestehenden Kreises. Das Formular zeigte hier bisher den zuletzt
    ANGELEGTEN Jahrgang -- im Januar also den Stand des Vorjahres --, und ein
    Speichern uebernahm ihn in den neuen Jahreskreis: RE-2027-0006 statt 0001.
    """
    aktiv = conn.execute(
        """
        SELECT naechste_seq FROM nummernkreis
         WHERE typ = ? AND jahr IN (?, ?)
         ORDER BY (jahr = ?) DESC
         LIMIT 1
        """,
        (nk.TYP_RECHNUNG, nk.KREIS_DURCHLAUFEND, jahr, nk.KREIS_DURCHLAUFEND),
    ).fetchone()
    if aktiv is not None:
        return int(aktiv["naechste_seq"])
    bestehend = nk.bestehender_kreis(conn, nk.TYP_RECHNUNG)
    if bestehend is None:
        return None
    return 1 if bestehend["reset_jaehrlich"] else int(bestehend["naechste_seq"])


def _vorgabe_seq(conn: sqlite3.Connection, jahr: int, *, jaehrlich: bool) -> int:
    """Die naechste Rechnungsnummer, wenn das Feld unveraendert blieb.

    Meist ist das der Stand, den das Formular zeigt (:func:`_naechste_seq`).
    Ausnahme: der Wechsel von "durchlaufen" auf "jedes Jahr neu" in einem
    neuen Jahr. Das Formular zeigt dann den durchlaufenden Stand (etwa 4 nach
    drei Rechnungen 2026) -- uebernommen, begann der Kreis 2027 bei 4, und
    RE-2027-0001 bis 0003 fehlten. Wer das Feld nicht anfasst, meint "wie
    ueblich", und ein neuer Jahreskreis beginnt bei 1. Vergebene Nummern
    dieses Jahres hebt :func:`nk.stelle_um` ohnehin darueber.

    Umgekehrt (jaehrlich -> durchlaufend) bleibt der Stand erhalten.
    """
    kreis = nk.bestehender_kreis(conn, nk.TYP_RECHNUNG)
    if jaehrlich and kreis is not None and not kreis["reset_jaehrlich"]:
        letztes_jahr = conn.execute(
            "SELECT MAX(nummer_jahr) FROM rechnung WHERE nummer_seq IS NOT NULL"
        ).fetchone()[0]
        if letztes_jahr is not None and int(letztes_jahr) < jahr:
            return 1
    return _naechste_seq(conn, jahr) or 1


def _euro_text(cent: int) -> str:
    """Cent als Formularwert: 8500 -> '85,00'."""
    return f"{cent / 100:.2f}".replace(".", ",")


def _ganzzahl(text: str, feld: str, *, leer: int | None) -> tuple[int | None, str | None]:
    """Liest ein Zahlenfeld. Gibt (Wert, Fehlertext) zurueck.

    Leer bedeutet ``leer`` -- fuer ein Feld mit Vorbelegung deren Wert, fuer
    die Rechnungsnummer "unveraendert". FastAPIs ``int = Form(...)`` antwortete
    auf "abc" oder "1,5" mit englischem JSON, und die Eingaben waren weg.
    """
    roh = text.strip()
    if not roh:
        return leer, None
    # Nicht int(): das liest auch "1_000" und " 7\n" -- hier zaehlt nur, was ein
    # Mensch in ein Zahlenfeld tippt.
    if not re.fullmatch(r"[+-]?[0-9]+", roh):
        return None, f"{feld} muss eine ganze Zahl sein. Eingegeben war: „{roh[:40]}“"
    wert = int(roh)
    if abs(wert) > ZAEHLER_MAX:
        return None, f"{feld} ist unplausibel: „{roh[:40]}“ hat mehr als neun Stellen."
    return wert, None


@router.get("/einrichtung", response_class=HTMLResponse)
def einrichtung_formular(request: Request, conn: sqlite3.Connection = Depends(hole_conn)):
    betrieb = betrieb_oder_none(conn)
    # Den vorhandenen Nummernkreis mitgeben: ohne das zeigte das Formular beim
    # zweiten Besuch wieder 1, und ein Speichern setzte den Zaehler zurueck --
    # ein Weg zu doppelt vergebenen Rechnungsnummern.
    kreis = nk.bestehender_kreis(conn, nk.TYP_RECHNUNG)
    seq = _naechste_seq(conn, int(db.heute()[:4]))
    return VORLAGEN.TemplateResponse(
        request,
        "einrichtung.html",
        {
            "betrieb": betrieb,
            "eingerichtet": betrieb is not None,
            "standardsatz_euro": _euro_text(betrieb["standardsatz_cent"] if betrieb else 0),
            "nummer_format": kreis["format"] if kreis else None,
            "start_seq": seq,
            # Der angezeigte Wert geht versteckt mit: nur so laesst sich beim
            # Speichern erkennen, ob der Nutzer das Feld GEAENDERT hat.
            "start_seq_angezeigt": str(seq if seq is not None else 1),
            "reset_jaehrlich": bool(kreis["reset_jaehrlich"]) if kreis else True,
            "fehler": None,
        },
    )


@router.post("/einrichtung")
def einrichtung_speichern(
    request: Request,
    # Bewusst mit Default statt Form(...): sonst antwortet FastAPI mit seiner
    # generischen Fehlerseite, bevor die eigene Pruefung ueberhaupt laeuft --
    # und die Eingaben sind weg. Zahlenfelder kommen als Text und werden hier
    # gelesen, aus demselben Grund.
    name: str = Form(""),
    strasse: str = Form(""),
    plz: str = Form(""),
    ort: str = Form(""),
    inhaber: str = Form(""),
    telefon: str = Form(""),
    email: str = Form(""),
    steuernummer: str = Form(""),
    ust_idnr: str = Form(""),
    kleinunternehmer_idnr: str = Form(""),
    iban: str = Form(""),
    bic: str = Form(""),
    institut: str = Form(""),
    standardsatz_euro: str = Form(""),
    zahlungsziel_tage: str = Form(""),
    zahlungsziel_zeigen: str = Form("1"),
    nummer_format: str = Form(nk.STANDARD_FORMAT),
    start_seq: str = Form(""),
    start_seq_angezeigt: str = Form(""),
    reset_jaehrlich: str = Form("1"),
    kundennummer_start: str = Form(""),
    kundennummer_format: str = Form(kunden.KUNDENNUMMER_FORMAT),
    rundung_modus: str = Form("AUFWAERTS"),
    rundung_intervall_min: str = Form(""),
    leistungszeitraum_default: str = Form("1"),
    beginn_am: str = Form(""),
    conn: sqlite3.Connection = Depends(hole_conn),
):
    jahr = int(db.heute()[:4])
    eingerichtet = betrieb_oder_none(conn) is not None

    def zurueck(meldung: str):
        """Das Formular erneut zeigen -- mit dem, was eingegeben WURDE.

        Hier stand "betrieb": None. Damit war nach einem Fehler nicht nur die
        Eingabe verloren (bei der Ersteinrichtung ein halbes Dutzend Felder),
        sondern das Formular zeigte auch wieder Nummernkreis 1 und
        "jaehrlich zuruecksetzen" an. Wer den Fehler behob und erneut
        speicherte, setzte damit stillschweigend den Rechnungszaehler zurueck
        -- genau die Doppelnummer, die einrichtung_formular() mit
        bestehender_kreis() verhindert.

        Alle Felder gehen als ROHTEXT zurueck: ein unlesbarer Stundensatz
        stand sonst als "0,00" im Feld, und die Ueberschrift sprang bei der
        Ersteinrichtung auf "Einstellungen", weil ein betrieb-Dict da war.
        """
        return VORLAGEN.TemplateResponse(
            request,
            "einrichtung.html",
            {
                "betrieb": {
                    "name": name,
                    "inhaber": inhaber,
                    "telefon": telefon,
                    "email": email,
                    "strasse": strasse,
                    "plz": plz,
                    "ort": ort,
                    "steuernummer": steuernummer,
                    "ust_idnr": ust_idnr,
                    "kleinunternehmer_idnr": kleinunternehmer_idnr,
                    "iban": iban,
                    "bic": bic,
                    "institut": institut,
                    "zahlungsziel_tage": zahlungsziel_tage,
                    "zahlungsziel_zeigen": zahlungsziel_zeigen == "1",
                    "leistungszeitraum_default": leistungszeitraum_default == "1",
                    "rundung_modus": rundung_modus,
                    "rundung_intervall_min": rundung_intervall_min,
                    "kundennummer_format": kundennummer_format,
                    "kundennummer_start": kundennummer_start,
                    "beginn_am": beginn_am,
                },
                "eingerichtet": eingerichtet,
                "standardsatz_euro": standardsatz_euro,
                "nummer_format": nummer_format,
                "start_seq": start_seq,
                "start_seq_angezeigt": start_seq_angezeigt,
                "reset_jaehrlich": reset_jaehrlich == "1",
                "fehler": meldung,
            },
            status_code=422,
        )

    # Pflichtfelder: nur Leerzeichen passieren das required des Browsers und
    # landeten als '' im Betrieb -- die Startseite hatte dann keine
    # Ueberschrift, und erst das Festschreiben meldete "unvollstaendig".
    for bezeichnung, wert in (
        ("Firmenname", name), ("Straße und Hausnummer", strasse), ("PLZ", plz), ("Ort", ort),
    ):
        if not wert.strip():
            return zurueck(
                f"Das Feld „{bezeichnung}“ fehlt. Die Angaben stehen auf jeder Rechnung "
                "(§ 34a Satz 1 Nr. 1 UStDV: vollständiger Name und vollständige Anschrift)."
            )
    # Der Betrieb sitzt im Inland: eine deutsche PLZ hat genau fuenf Ziffern.
    # "2OO95" mit zwei Buchstaben O stand sonst auf jeder Rechnung.
    if not re.fullmatch(r"[0-9]{5}", plz.strip()):
        return zurueck(
            f"Die PLZ des Betriebs muss aus genau fünf Ziffern bestehen. "
            f"Eingegeben war: „{plz.strip()[:40]}“"
        )

    # Der Stundensatz kommt als Text herein -- und wird genauso gelesen wie
    # der Satz im Kundenstamm: Komma als Dezimaltrenner, Punkt als
    # Tausendertrenner. "1.234" sind 1.234,00 €, nicht 1,23 €. Vorher las
    # float() hier etwas anderes als euro_nach_cent() dort, und dieselbe
    # Eingabe ergab zwei Stundensaetze um den Faktor 1000 auseinander.
    # "1e3", "inf", "nan" und fremde Ziffern weist euro_nach_cent selbst ab.
    satz_roh = standardsatz_euro.strip()
    try:
        satz_cent = kunden.euro_nach_cent(satz_roh) or 0
    except (ArithmeticError, ValueError) as problem:
        # euro_nach_cent weist Riesenbetraege selbst ab ("unplausibel gross");
        # vorher sprengte ein 20-stelliger Satz erst das INSERT -- Serverfehler.
        if isinstance(problem, kunden.BetragZuGross):
            return zurueck(
                f"Der Stundensatz „{satz_roh[:40]}“ ist unplausibel groß. "
                "Gemeint ist der Betrag für eine Stunde, zum Beispiel 120,00."
            )
        return zurueck(
            "Der Stundensatz muss eine Zahl sein, zum Beispiel 120 oder 120,00. "
            f"Eingegeben war: „{satz_roh[:40]}“"
        )
    if satz_cent < 0:
        return zurueck("Der Stundensatz kann nicht negativ sein.")

    ziel, fehler = _ganzzahl(zahlungsziel_tage, "Das Zahlungsziel", leer=14)
    if fehler:
        return zurueck(fehler)
    if not 0 <= ziel <= ZAHLUNGSZIEL_MAX_TAGE:
        return zurueck(
            f"Das Zahlungsziel muss zwischen 0 und {ZAHLUNGSZIEL_MAX_TAGE} Tagen liegen. "
            f"Eingegeben war: {ziel}"
        )

    takt, fehler = _ganzzahl(rundung_intervall_min, "Der Takt in Minuten", leer=15)
    if fehler:
        return zurueck(fehler)
    if rundung_modus not in RUNDUNGSMODI:
        return zurueck(f"Unbekannte Rundungsregel: „{rundung_modus[:40]}“")

    # Leer heisst "unveraendert": der Zaehler bleibt, wo er steht. Vorher
    # griff die Vorbelegung 1 -- und beim Umstieg aus Word stand der Kreis
    # damit still wieder auf 1, bevor die erste Rechnung geschrieben war.
    naechste, fehler = _ganzzahl(start_seq, "Die nächste Rechnungsnummer", leer=None)
    if fehler:
        return zurueck(fehler)
    # Unveraendert heisst: leer ODER der Wert, den das Formular gezeigt hat.
    # Nur ein GEAENDERTER Wert ist eine Vorgabe des Nutzers.
    if start_seq.strip() and start_seq.strip() == start_seq_angezeigt.strip():
        naechste = None
    if naechste is None:
        naechste = _vorgabe_seq(conn, jahr, jaehrlich=reset_jaehrlich == "1")

    kundennummer_ab, fehler = _ganzzahl(kundennummer_start, "Die erste Kundennummer", leer=1)
    if fehler:
        return zurueck(fehler)

    # Der Beginn der Taetigkeit entscheidet, welche § 19-Grenze gilt: im
    # Gruendungsjahr 25.000 €, sonst 100.000 €. Er wurde bisher stillschweigend
    # auf den Installationstag gesetzt und war danach nirgends korrigierbar --
    # fuer einen Betrieb, der schon Jahre laeuft, hiess das: falsche Grenze,
    # falsche Warnung. Leer heisst "nicht bekannt", und das sagt die Anzeige.
    beginn_am = beginn_am.strip()
    beginn_iso: str | None = None
    if beginn_am:
        # Gespeichert wird die ISO-Form: "20260101" liest fromisoformat zwar,
        # das Datumsfeld zeigte es aber leer, und ein erneutes Speichern
        # loeschte den Beginn. Ein Jahr vor 2000 ("0026") ist ein Tippfehler
        # und waehlte sonst still die falsche § 19-Grenze.
        try:
            beginn_iso = eingabe.datum(
                beginn_am, feld="Beginn der Tätigkeit",
                heute=_dt.date.fromisoformat(db.heute()),
            )
        except ValueError as problem:
            return zurueck(f"{problem} Gemeint ist der Tag der Gewerbeanmeldung.")

    kennungen = [steuernummer.strip(), ust_idnr.strip(), kleinunternehmer_idnr.strip()]
    if not any(kennungen):
        return zurueck(
            "§ 34a Satz 1 Nr. 2 UStDV verlangt mindestens eine von drei Kennungen: "
            "Steuernummer, Umsatzsteuer-Identifikationsnummer oder "
            "Kleinunternehmer-Identifikationsnummer. Ohne eine davon darf keine "
            "Rechnung festgeschrieben werden."
        )
    # Die Bankverbindung steht auf jeder Rechnung ("bitte ueberweisen auf
    # ..."). Vorher wurde sie ungeprueft gespeichert: "DE00 1234" und hundert
    # x druckte das PDF, und das Geld des Kunden ging ins Leere zurueck.
    # Gespeichert wird die kompakte Form; die Rechnung gruppiert sie wieder.
    try:
        iban_kompakt = eingabe.iban(iban)
        bic_kompakt = eingabe.bic(bic)
    except ValueError as fehler:
        return zurueck(str(fehler))

    jaehrlich = reset_jaehrlich == "1"
    try:
        nk.pruefe_format(nummer_format, reset_jaehrlich=jaehrlich)
        kundennummer_format = kunden.pruefe_kundennummer_format(kundennummer_format)
    except ValueError as fehler:
        return zurueck(str(fehler))

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO betrieb (id, name, inhaber, telefon, email, strasse, plz, ort,
                                 steuernummer, ust_idnr, kleinunternehmer_idnr,
                                 iban, bic, institut, standardsatz_cent, zahlungsziel_tage,
                                 zahlungsziel_zeigen,
                                 kundennummer_start, kundennummer_format, rundung_modus,
                                 rundung_intervall_min,
                                 leistungszeitraum_default, beginn_am)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, inhaber = excluded.inhaber,
                telefon = excluded.telefon, email = excluded.email,
                strasse = excluded.strasse, plz = excluded.plz, ort = excluded.ort,
                steuernummer = excluded.steuernummer, ust_idnr = excluded.ust_idnr,
                kleinunternehmer_idnr = excluded.kleinunternehmer_idnr,
                iban = excluded.iban, bic = excluded.bic, institut = excluded.institut,
                standardsatz_cent = excluded.standardsatz_cent,
                zahlungsziel_tage = excluded.zahlungsziel_tage,
                zahlungsziel_zeigen = excluded.zahlungsziel_zeigen,
                kundennummer_start = excluded.kundennummer_start,
                kundennummer_format = excluded.kundennummer_format,
                rundung_modus = excluded.rundung_modus,
                rundung_intervall_min = excluded.rundung_intervall_min,
                leistungszeitraum_default = excluded.leistungszeitraum_default,
                beginn_am = excluded.beginn_am
            """,
            (
                name.strip(), inhaber.strip() or None,
                telefon.strip() or None, email.strip() or None,
                strasse.strip(), plz.strip(), ort.strip(),
                steuernummer.strip() or None, ust_idnr.strip() or None,
                kleinunternehmer_idnr.strip() or None, iban_kompakt, bic_kompakt,
                # Der Name der Bank steht unter der IBAN (Zahlungsblock, Fusszeile).
                institut.strip() or None,
                satz_cent, ziel,
                1 if zahlungsziel_zeigen == "1" else 0,
                max(1, kundennummer_ab), kundennummer_format,
                rundung_modus,
                max(1, min(60, takt)),
                1 if leistungszeitraum_default == "1" else 0, beginn_iso,
            ),
        )
        nk.stelle_um(
            conn,
            jahr,
            vorlage=nummer_format,
            start_seq=naechste,
            reset_jaehrlich=jaehrlich,
        )
        db.protokolliere(conn, "EINRICHTUNG", "betrieb", 1, wert_neu={"name": name})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return RedirectResponse("/", status_code=303)

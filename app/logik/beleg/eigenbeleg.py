"""Belege ohne Fremddokument, und die Geschaeftsreise als Container.

Warum es diesen Weg braucht
---------------------------
Zwei Kategorien des eigenen Katalogs kommen **nie** mit einem Fremdbeleg:

* **Verpflegungspauschale** (KZ 171). Abziehbar sind die Pauschbetraege nach
  § 9 Abs. 4a EStG, nicht die tatsaechlichen Essensbelege -- die sind bei einem
  Selbststaendigen gar nicht abziehbar. Der Beleg ist also eine Berechnung, und
  die Restaurantrechnung waere der falsche Nachweis.
* **Fahrten mit privatem Pkw** (KZ 147). 0,30 € je Kilometer als
  Nutzungseinlage; der Nachweis ist eine Kilometeraufstellung.

Dazu der Alltagsfall: ein Kassenzettel, der verloren ist oder auf Thermopapier
verblasst. Ein Eigenbeleg ist dann die vorgesehene Loesung und kein Notbehelf --
aber er **braucht einen Grund**, und den erzwingt ein Trigger. Ein Eigenbeleg
ohne Begruendung ist in einer Pruefung wertlos: er ist die Ausnahme von "jeder
Ausgabe liegt ein Fremdbeleg zugrunde", und eine Ausnahme muss man begruenden
koennen. Spaeter erinnert sich niemand.

Die Reise
---------
Eine Geschaeftsreise ist der **einzige** Vorgang, der mehrere Kennzahlen
braucht: ein Konferenzbesuch zerfaellt in Ticket (281), Hotel (221),
Verpflegungspauschale (171) und Fahrt (146 oder 147). Deshalb ein Container mit
gemeinsamer ``reise_id``, der die Belege zusammenhaelt, ohne sie zu vermischen --
und nicht ein Beleg mit vier Kategorien, der in der EUeR nirgends hinpasst.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app import db
from app.logik import eingabe, kategorien, pauschalen
from app.logik.beleg import felder as f
from app.logik.beleg.ergebnis import BelegFehler
from app.logik.rundung import euro

#: Mindestlaenge der Begruendung. Keine Zierde: "verloren" allein ist keine
#: Begruendung, und der Trigger in der Datenbank verlangt dasselbe.
GRUND_MINDESTLAENGE = 10

#: Kategorien, die von Natur aus Eigenbelege sind.
EIGENBELEG_KATEGORIEN = ("REISE_VERPFLEGUNG", "FAHRT_PRIVATER_PKW", "HOMEOFFICE_PAUSCHALE")

#: Hoechstens so viele Kilometer je Fahrt. Mehr ist ein Tippfehler (eine
#: Null zu viel), und ohne Grenze rechnete das Kilometergeld mit jeder Zahl,
#: die das Formular hergab.
KM_HOECHSTENS = 10_000


def ohne_steuerzeichen(text: str | None) -> str:
    """Entfernt Steuerzeichen (NUL, Escape ...) aus einem Freitext; Zeilenumbruch
    und Tabulator bleiben.

    Ein NUL am Anfang der Begruendung sah fuer Python wie Text aus, fuer SQLite
    aber nicht: ``length()`` zaehlt nur bis zum NUL, der Trigger "Eigenbeleg
    ohne Begruendung" schlug zu, und der Nutzer sah eine Absturzseite statt
    einer Meldung.

    >>> ohne_steuerzeichen("\\x00Parken\\x1b am\\r\\nHafen ")
    'Parken am\\nHafen'
    """
    text = (text or "").replace("\r\n", "\n")
    return "".join(
        zeichen for zeichen in text
        if zeichen in "\n\t" or unicodedata.category(zeichen) != "Cc"
    ).strip()


def _datum(roh: str | None, feld: str, *, zukunft: bool = False) -> str:
    """Ein Datum als JJJJ-MM-TT, mit dem gemeinsamen Fenster aus eingabe.datum
    (Jahr ab 2000, ohne ``zukunft`` nicht nach heute)."""
    try:
        return eingabe.datum(roh, feld=feld, zukunft=zukunft)
    except ValueError as fehler:
        raise BelegFehler(str(fehler)) from None


@dataclass(frozen=True, slots=True)
class Reise:
    id: int
    bezeichnung: str
    anlass: str
    ziel: str
    abreise_am: str
    rueckkehr_am: str
    inland: bool
    abwesend_stunden: int | None
    #: Uebernachtung ausserhalb der Wohnung. Ohne sie gibt es fuer An- und
    #: Abreisetag keine Pauschale (§ 9 Abs. 4a Satz 3 Nr. 2 EStG).
    uebernachtung: bool = True

    @property
    def tage(self) -> int:
        return (date.fromisoformat(self.rueckkehr_am)
                - date.fromisoformat(self.abreise_am)).days + 1


#: Zahlungsarten, die die Tabelle zahlung kennt (CHECK in schema.sql).
ZAHLUNGSARTEN = ("UEBERWEISUNG", "KARTE", "BAR", "PAYPAL", "SONSTIGES")


def erfasse(
    conn: sqlite3.Connection,
    *,
    belegdatum: str,
    brutto_cent: int,
    grund: str,
    kategorie_id: int | None = None,
    bezeichnung: str | None = None,
    quote: int = 100,
    reise_id: int | None = None,
    zahlungsart: str = "SONSTIGES",
    berechnung: dict[str, object] | None = None,
    bezahlt_am: str | None = None,
    pruefung_unter_sperre: Callable[[sqlite3.Connection], None] | None = None,
) -> int:
    """Schreibt einen Eigenbeleg. Gibt die Beleg-ID zurueck.

    Oeffnet die Transaktion selbst, damit der Beleg und sein Protokolleintrag
    gemeinsam gelten oder gemeinsam verfallen.

    ``berechnung`` wandert in die Extraktionstabelle. Bei einer Pauschale ist
    das der eigentliche Nachweis: nicht "28 €", sondern "ein voller Tag zu
    28 €". Nach GoBD ist das bestaetigte Ergebnis selbst
    aufbewahrungspflichtig -- bei einem Eigenbeleg ist es das Einzige, was es
    gibt.

    ``bezahlt_am`` legt die Zahlungszeile gleich mit an. Die EUeR rechnet nach
    § 11 Abs. 2 EStG mit dem Tag des Abflusses, und den liefert nur eine
    Zahlung -- ein Beleg ohne Zahlung steht in keiner EUeR. Bei einer
    **Pauschale** gibt es aber keinen Geldfluss, den man spaeter "bezahlt"
    setzen koennte: die Verpflegungspauschale und das Kilometergeld sind der
    Abzug selbst, und ihr Abfluss ist der Reisetag. Ohne Zahlungszeile hatte
    die ganze Reisen-Funktion still keine EUeR-Wirkung. Beim manuellen
    Eigenbeleg (Parkuhr, verblasster Kassenzettel) ist das Geld meist auch
    laengst weg; das Formular belegt den Tag deshalb vor, laesst ihn aber
    leer, wenn die Zahlung wirklich noch aussteht.

    ``pruefung_unter_sperre`` laeuft INNERHALB der Transaktion, vor dem
    Schreiben; ein BelegFehler darin verwirft alles. Fuer jede Pruefung "gibt
    es das schon?": ausserhalb der Sperre sahen bei einem Doppelklick beide
    Anfragen "noch nicht da" und schrieben beide -- die Verpflegungspauschale
    stand dann zweimal in der EUeR.
    """
    # Gespeichert wird das GELESENE Datum: "20260314" nahm fromisoformat()
    # an, roh gespeichert fiel es aber aus jeder Jahresauswertung. Ein Beleg
    # aus der Zukunft (2099 statt 2026) oder aus dem Jahr 26 ist ein Tippfehler.
    if not (belegdatum or "").strip():
        raise BelegFehler("Ohne gültiges Belegdatum kann nicht gebucht werden.")
    belegdatum = _datum(belegdatum, "Belegdatum")
    if bezahlt_am is not None and str(bezahlt_am).strip():
        bezahlt_am = _datum(bezahlt_am, "Zahlungsdatum")
    else:
        bezahlt_am = None
    grund = ohne_steuerzeichen(grund)
    bezeichnung = ohne_steuerzeichen(bezeichnung)
    if brutto_cent <= 0:
        # Gilt fuer 0, fuer leer und fuer negativ. "Ein Eigenbeleg ueber null
        # Euro ist kein Beleg" las sich bei "-1" schief.
        raise BelegFehler("Ein Eigenbeleg braucht einen Betrag über 0 €.")
    if len(grund) < GRUND_MINDESTLAENGE:
        raise BelegFehler(
            "Ein Eigenbeleg braucht eine Begründung von mindestens "
            f"{GRUND_MINDESTLAENGE} Zeichen. Er ist die Ausnahme von „jeder Ausgabe "
            "liegt ein Fremdbeleg zugrunde“, und eine Ausnahme muss man begründen "
            "können — später erinnert sich niemand."
        )
    if not 0 <= quote <= 100:
        raise BelegFehler(f"Die betriebliche Quote muss zwischen 0 und 100 liegen, nicht {quote}.")

    notiz = grund.strip()
    if bezeichnung and bezeichnung.strip():
        notiz = f"{bezeichnung.strip()} — {notiz}"

    conn.execute("BEGIN IMMEDIATE")
    try:
        if pruefung_unter_sperre is not None:
            pruefung_unter_sperre(conn)
        beleg_id = conn.execute(
            """
            INSERT INTO beleg (art, kategorie_id, belegdatum, waehrung,
                               betrag_brutto_cent, aufteilungsquote,
                               betrag_betrieblich_cent, netto_fuer_grenzwert_cent,
                               reise_id, ist_eigenbeleg, zahlungsart, notiz,
                               geprueft_am, erfasst_am)
            VALUES ('EINGANG', ?, ?, 'EUR', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                kategorie_id, belegdatum, brutto_cent, quote,
                kategorien.betrieblich_cent(brutto_cent, quote),
                # Ohne ausgewiesene Steuer ist Netto gleich Brutto -- eine
                # Steuer, die nicht erhoben wurde, kann man nicht
                # herausrechnen. Bei einer Pauschale gibt es ohnehin keine.
                brutto_cent,
                reise_id, zahlungsart, notiz, db.jetzt_utc(), db.jetzt_utc(),
            ),
        ).lastrowid

        conn.execute(
            "INSERT INTO extraktion (beleg_id, quelle, bestaetigt_json, verfahren,"
            " regel_version, zeitpunkt) VALUES (?, 'manuell', ?, ?, ?, ?)",
            (
                beleg_id,
                json.dumps({
                    "belegdatum": belegdatum, "brutto": brutto_cent,
                    "grund": grund.strip(), "quote": quote,
                    **(berechnung or {}),
                }, ensure_ascii=False, sort_keys=True, default=str),
                "Eigenbeleg, vom Nutzer erfasst", f.REGEL_VERSION, db.jetzt_utc(),
            ),
        )
        db.protokolliere(conn, "EIGENBELEG", "beleg", beleg_id,
                         wert_neu={"brutto_cent": brutto_cent, "datum": belegdatum},
                         begruendung=grund.strip())
        if bezahlt_am:
            # Negativ wie in belege._zahlung_erfassen: eine Ausgabe, damit die
            # Summe ueber alle Zahlungen vorzeichenrichtig bleibt.
            conn.execute(
                "INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                " VALUES (?, ?, ?, ?)",
                (beleg_id, bezahlt_am, -abs(brutto_cent),
                 zahlungsart if zahlungsart in ZAHLUNGSARTEN else "SONSTIGES"),
            )
            db.protokolliere(conn, "ZAHLUNG", "beleg", beleg_id, feld="datum",
                             wert_neu=bezahlt_am)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return beleg_id


# ------------------------------------------------------------------- Reise

def reise_anlegen(
    conn: sqlite3.Connection,
    *,
    bezeichnung: str,
    anlass: str,
    ziel: str,
    abreise_am: str,
    rueckkehr_am: str,
    inland: bool = True,
    abwesend_stunden: int | None = None,
    notiz: str | None = None,
    uebernachtung: bool | None = None,
) -> int:
    """Legt eine Reise an. Anlass und Ziel sind Pflicht.

    Nicht Komfort: eine Reise ohne erkennbaren betrieblichen Anlass ist nicht
    abziehbar, und der Anlass muss aufgezeichnet sein -- nicht erst in der
    Pruefung erinnert werden.

    ``uebernachtung`` ohne Angabe heisst: ja bei einer mehrtaegigen Reise,
    nein bei einer eintaegigen. Wer ueber Mitternacht faehrt, ohne zu
    uebernachten, setzt es ausdruecklich auf False -- dann gilt die
    Teiltagregel (§ 9 Abs. 4a Satz 3 Nr. 3 EStG).
    """
    bezeichnung, anlass, ziel = (ohne_steuerzeichen(bezeichnung), ohne_steuerzeichen(anlass),
                                 ohne_steuerzeichen(ziel))
    notiz = ohne_steuerzeichen(notiz)
    for name, wert in (("Bezeichnung", bezeichnung), ("Anlass", anlass), ("Ziel", ziel)):
        if not wert:
            raise BelegFehler(f"{name} fehlt. Ohne Anlass und Ziel ist eine Reise "
                              "steuerlich nicht nachvollziehbar.")
    if not (abreise_am or "").strip() or not (rueckkehr_am or "").strip():
        raise BelegFehler("Abreise und Rückkehr brauchen ein gültiges Datum.")
    # Eine Reise darf geplant sein, also in der Zukunft liegen -- aber nicht
    # im Jahr 26. Gespeichert und verglichen wird das gelesene Datum: als Text
    # war "20260501" groesser als "2026-05-03", und die Pruefung "Rueckkehr vor
    # Abreise" ging an der Datenbank vorbei in eine Absturzseite.
    abreise_am = _datum(abreise_am, "Abreise", zukunft=True)
    rueckkehr_am = _datum(rueckkehr_am, "Rückkehr", zukunft=True)
    von = date.fromisoformat(abreise_am)
    bis = date.fromisoformat(rueckkehr_am)
    if bis < von:
        raise BelegFehler("Die Rückkehr liegt vor der Abreise.")
    if abwesend_stunden is not None and not 0 <= abwesend_stunden <= 24:
        # Das Formular begrenzt auf 0 bis 24, aber nur im Browser.
        raise BelegFehler("Die Abwesenheit muss zwischen 0 und 24 Stunden liegen.")
    if uebernachtung is None:
        uebernachtung = bis > von

    conn.execute("BEGIN IMMEDIATE")
    try:
        reise_id = conn.execute(
            "INSERT INTO reise (bezeichnung, anlass, ziel, inland, abreise_am,"
            " rueckkehr_am, abwesend_stunden, uebernachtung, notiz, erfasst_am)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bezeichnung, anlass, ziel, int(inland),
             abreise_am, rueckkehr_am, abwesend_stunden, int(uebernachtung),
             notiz or None, db.jetzt_utc()),
        ).lastrowid
        db.protokolliere(conn, "ANLEGEN", "reise", reise_id,
                         wert_neu={"bezeichnung": bezeichnung, "ziel": ziel},
                         begruendung=anlass)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return reise_id


def lies_reise(conn: sqlite3.Connection, reise_id: int) -> Reise | None:
    if not 1 <= reise_id <= eingabe.ID_HOECHSTENS:
        # Ab 2**63 sprengte die Kennung die Bindung an SQLite (OverflowError).
        return None
    zeile = conn.execute("SELECT * FROM reise WHERE id = ?", (reise_id,)).fetchone()
    if zeile is None:
        return None
    return Reise(
        id=zeile["id"], bezeichnung=zeile["bezeichnung"], anlass=zeile["anlass"],
        ziel=zeile["ziel"], abreise_am=zeile["abreise_am"],
        rueckkehr_am=zeile["rueckkehr_am"], inland=bool(zeile["inland"]),
        abwesend_stunden=zeile["abwesend_stunden"],
        uebernachtung=bool(zeile["uebernachtung"]),
    )


def reisen(conn: sqlite3.Connection, grenze: int = 100) -> list[sqlite3.Row]:
    """Alle Reisen mit ihrer Belegsumme, aufgeschluesselt nach Kennzahl --
    ohne geloeschte Belege."""
    return conn.execute(
        """
        SELECT r.*,
               (SELECT COUNT(*) FROM v_beleg_gueltig b WHERE b.reise_id = r.id) AS belege,
               (SELECT COALESCE(SUM(b.betrag_betrieblich_cent), 0) FROM v_beleg_gueltig b
                 WHERE b.reise_id = r.id) AS summe_cent,
               (SELECT COUNT(DISTINCT k.kennzahl) FROM v_beleg_gueltig b
                  JOIN euer_kategorie k ON k.id = b.kategorie_id
                 WHERE b.reise_id = r.id) AS kennzahlen
          FROM reise r
         ORDER BY r.abreise_am DESC
         LIMIT ?
        """,
        (grenze,),
    ).fetchall()


def reisebelege(conn: sqlite3.Connection, reise_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.*, k.bezeichnung AS kategorie, k.kennzahl
          FROM v_beleg_gueltig b
          LEFT JOIN euer_kategorie k ON k.id = b.kategorie_id
         WHERE b.reise_id = ?
         ORDER BY k.kennzahl, b.belegdatum
        """,
        (reise_id,),
    ).fetchall()


def _datum_de(iso: str) -> str:
    """ISO-Datum als TT.MM.JJJJ fuer Begruendungstexte; Unlesbares bleibt stehen.

    Die Begruendung eines Eigenbelegs ist nach GoBD aufzubewahren und wird von
    Menschen gelesen -- "vom 2026-04-20" las sich wie ein Datenbankauszug.

    >>> _datum_de("2026-04-20"), _datum_de("kaputt")
    ('20.04.2026', 'kaputt')
    """
    try:
        return date.fromisoformat(str(iso)).strftime("%d.%m.%Y")
    except ValueError:
        return str(iso)


def verpflegung_buchen(conn: sqlite3.Connection, reise_id: int) -> int:
    """Erzeugt den Eigenbeleg fuer die Verpflegungspauschale einer Reise.

    Nur Inland. Fuer eine Auslandsreise sind die Saetze vom BMF jaehrlich neu
    festgesetzt und bewusst nicht hinterlegt -- ein veralteter Satz sieht aus
    wie ein gepruefter.
    """
    reise = lies_reise(conn, reise_id)
    if reise is None:
        raise BelegFehler("Diese Reise gibt es nicht.")
    if not reise.inland:
        raise BelegFehler(
            "Für Auslandsreisen sind die Verpflegungspauschalen nicht hinterlegt: das "
            "Bundesfinanzministerium setzt sie jedes Jahr je Land neu fest. Bitte den "
            "Betrag als Eigenbeleg von Hand erfassen."
        )
    # Vorab fuer die schnelle Antwort beim zweiten Klick; massgebend ist
    # dieselbe Pruefung unter der Sperre in erfasse() (Doppelklick).
    _keine_verpflegung_vorhanden(conn, reise_id)
    if date.fromisoformat(reise.rueckkehr_am) > date.today():
        # Die Pauschale ist ein Beleg mit dem Rueckkehrtag als Abfluss -- eine
        # Zahlung in der Zukunft gibt es nicht.
        raise BelegFehler(
            f"Die Reise endet erst am {_datum_de(reise.rueckkehr_am)}. Die "
            "Verpflegungspauschale lässt sich nach der Rückkehr buchen."
        )

    try:
        rechnung = pauschalen.verpflegung(
            reise.abreise_am, reise.rueckkehr_am,
            abwesend_stunden=reise.abwesend_stunden, uebernachtung=reise.uebernachtung,
        )
    except (pauschalen.KeinSatz, ValueError) as fehler:
        raise BelegFehler(str(fehler)) from fehler
    if rechnung.betrag_cent <= 0:
        if reise.tage > 1 and not reise.uebernachtung:
            raise BelegFehler(
                "Für diese Reise ergibt sich keine Verpflegungspauschale: ohne "
                "Übernachtung außerhalb der Wohnung gibt es keine An- und "
                "Abreisepauschale, sondern 14 € nur bei mehr als acht Stunden "
                "Abwesenheit (§ 9 Abs. 4a Satz 3 Nr. 3 EStG). Die Stunden bitte an der "
                "Reise eintragen."
            )
        raise BelegFehler(
            "Für diese Reise ergibt sich keine Verpflegungspauschale: eine eintägige "
            "Reise zählt erst ab mehr als acht Stunden Abwesenheit (§ 9 Abs. 4a EStG)."
        )

    grund = (
        f"Pauschbeträge nach § 9 Abs. 4a EStG für die Reise „{reise.bezeichnung}“ "
        f"nach {reise.ziel} vom {_datum_de(reise.abreise_am)} bis "
        f"{_datum_de(reise.rueckkehr_am)}, Anlass: "
        f"{reise.anlass}. Berechnung: {rechnung.nachweis}."
    )
    if rechnung.veraltet:
        # Der Beleg muss es selbst sagen: wer ihn in drei Jahren liest, sieht
        # sonst "Inlandssaetze" und haelt sie fuer die des Reisejahres.
        grund += f" Hinweis: {rechnung.hinweis}"

    kategorie = kategorien.nach_schluessel(conn, "REISE_VERPFLEGUNG")
    return erfasse(
        conn,
        belegdatum=reise.rueckkehr_am,
        brutto_cent=rechnung.betrag_cent,
        bezeichnung=f"Verpflegungspauschale {reise.bezeichnung}",
        grund=grund,
        kategorie_id=kategorie["id"] if kategorie else None,
        reise_id=reise_id,
        berechnung={
            "volle_tage": rechnung.volle_tage,
            "teiltage": rechnung.teiltage,
            "satz_voll_cent": rechnung.satz_voll_cent,
            "satz_teil_cent": rechnung.satz_teil_cent,
            "fundstelle": "§ 9 Abs. 4a EStG, Inlandssätze",
            "pauschalen_aus_jahr": rechnung.aus_jahr,
        },
        # Der Abfluss einer Pauschale ist der Reisetag (siehe erfasse()).
        bezahlt_am=reise.rueckkehr_am,
        pruefung_unter_sperre=lambda sperre: _keine_verpflegung_vorhanden(sperre, reise_id),
    )


def _keine_verpflegung_vorhanden(conn: sqlite3.Connection, reise_id: int) -> None:
    """Je Reise gibt es die Verpflegungspauschale genau einmal.

    Unter der Sperre aufgerufen (siehe erfasse()): bei einem Doppelklick auf
    "Pauschale buchen" sahen sonst beide Anfragen "noch keine" und buchten
    beide -- die Pauschale stand doppelt in der EUeR. Die verlierende Anfrage
    bekommt jetzt dieselbe Meldung wie ein zweiter Klick nach dem ersten.
    """
    # Ein geloeschter Beleg zaehlt nicht: wer die Pauschale geloescht hat
    # (etwa mit falschen Reisedaten), bucht sie neu.
    vorhanden = conn.execute(
        "SELECT b.id FROM v_beleg_gueltig b JOIN euer_kategorie k ON k.id = b.kategorie_id"
        " WHERE b.reise_id = ? AND k.schluessel = 'REISE_VERPFLEGUNG'"
        " ORDER BY b.id LIMIT 1",
        (reise_id,),
    ).fetchone()
    if vorhanden:
        raise BelegFehler(
            f"Für diese Reise ist die Verpflegungspauschale schon als Beleg "
            f"{vorhanden['id']} erfasst."
        )


def kilometer_buchen(
    conn: sqlite3.Connection, *, datum: str, kilometer: int, zweck: str,
    strecke: str, reise_id: int | None = None,
) -> int:
    """Erzeugt den Eigenbeleg fuer Fahrten mit dem privaten Pkw.

    Der Nachweis ist die Strecke, nicht der Betrag: 0,30 € je Kilometer ist
    unstrittig, die Fahrt muss belegt sein. Deshalb sind Strecke und Zweck
    Pflicht und wandern in die Begruendung.
    """
    if kilometer <= 0:
        raise BelegFehler("Ohne Kilometer gibt es keine Fahrt.")
    if kilometer > KM_HOECHSTENS:
        raise BelegFehler(
            f"{kilometer} km für eine Fahrt sind unplausibel — höchstens "
            f"{KM_HOECHSTENS:_} km je Fahrt. Bitte die Kilometer prüfen.".replace("_", ".")
        )
    strecke, zweck = ohne_steuerzeichen(strecke), ohne_steuerzeichen(zweck)
    if not strecke or not zweck:
        raise BelegFehler(
            "Strecke und Zweck sind Pflicht: nachzuweisen ist die Fahrt, nicht der "
            "Betrag — 0,30 € je Kilometer sind unstrittig."
        )
    if not (datum or "").strip():
        raise BelegFehler("Ohne gültiges Datum kann nicht gebucht werden.")
    datum = _datum(datum, "Fahrtdatum")
    jahr = date.fromisoformat(datum).year
    if reise_id is not None and lies_reise(conn, reise_id) is None:
        # Sonst scheiterte erst der Fremdschluessel beim INSERT -- 500-Seite
        # statt Meldung, erreichbar ueber eine veraltete Seite.
        raise BelegFehler("Diese Reise gibt es nicht.")

    try:
        satz = pauschalen.fuer(jahr, rueckwaerts=False)
        betrag = pauschalen.kilometergeld_cent(kilometer, jahr)
    except pauschalen.KeinSatz as fehler:
        raise BelegFehler(str(fehler)) from fehler
    grund = (
        f"{kilometer} km mit dem privaten Pkw, {strecke.strip()}, Zweck: "
        f"{zweck.strip()}. Nutzungseinlage "
        f"{euro(satz['kilometergeld_pkw_cent'])} € je Kilometer."
    )
    if satz.veraltet:
        grund += f" Hinweis: {satz.hinweis}"
    kategorie = kategorien.nach_schluessel(conn, "FAHRT_PRIVATER_PKW")
    return erfasse(
        conn,
        belegdatum=datum,
        brutto_cent=betrag,
        bezeichnung=f"Fahrt {strecke.strip()}",
        grund=grund,
        kategorie_id=kategorie["id"] if kategorie else None,
        reise_id=reise_id,
        berechnung={"kilometer": kilometer, "satz_cent": satz["kilometergeld_pkw_cent"],
                    "strecke": strecke.strip(), "pauschalen_aus_jahr": satz.aus_jahr},
        # Der Abfluss einer Pauschale ist der Fahrtag (siehe erfasse()).
        bezahlt_am=datum,
    )

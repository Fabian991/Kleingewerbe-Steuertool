"""Entwurf anlegen, festschreiben, stornieren.

Die Reihenfolge im Festschreiben ist der Kern dieses Moduls und nicht beliebig:

1. In EINER Transaktion: Vorbedingungen erneut pruefen, Nummer ziehen,
   Rechnung und Positionen mit Snapshots schreiben, Journal schreiben, COMMIT.
2. ERST DANACH das PDF rendern und den Hash nachtragen.

Dateischreibvorgaenge sind nicht Teil einer SQLite-Transaktion. Lag das
Rendering innerhalb, bliebe nach einem Rollback ein PDF als Waise im
Belegordner liegen -- womoeglich mit einer Nummer im Namen, die anschliessend an
eine andere Rechnung vergeben wird. Scheitert das Rendering nach dem Commit,
existiert die Rechnung ohne PDF; das ist sichtbar (pdf_pfad ist leer), meldbar
und nachholbar. Eine Waise mit verbrauchter Nummer ist keines davon.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app import db, pdf, pfade
from app.logik import (
    gruppierung,
    kunden,
    nummernkreis,
    rechnungsdruck,
    rundung,
    validierung,
)

DOKUMENTTITEL = {
    "RECHNUNG": "Rechnung",
    "STORNO": "Rechnungskorrektur",
    "GUTSCHRIFT_SELBSTABRECHNUNG": "Gutschrift",
    "ANGEBOT": "Angebot",
    "LEISTUNGSNACHWEIS": "Leistungsnachweis",
}

#: Felder des Kunden, die beim Festschreiben in die Rechnung kopiert werden.
#: Hinweis fuer Leistungen an Empfaenger ausserhalb der EU. Vorgeschrieben ist
#: hier nichts; der Satz sagt nur, was zutrifft. Fest im Code und nicht
#: einstellbar, weil er keine Rechtsfolge hat und deshalb auch nichts
#: Falsches behaupten koennen soll.
HINWEIS_DRITTLAND = (
    "Im Inland nicht steuerbare Leistung; der Leistungsort liegt beim Empfänger "
    "(§ 3a Abs. 2 UStG)."
)

KUNDE_SNAPSHOT = (
    "kundennummer", "name", "rechnungsname", "ansprechpartner",
    "strasse", "plz", "ort", "land", "ust_idnr", "typ", "leistungsort",
)
BETRIEB_SNAPSHOT = (
    "name", "inhaber", "strasse", "plz", "ort", "land", "email", "telefon",
    "steuernummer", "ust_idnr", "kleinunternehmer_idnr", "iban", "bic", "institut",
    "logo_pfad", "hinweis_19_text",
    # Ohne dieses Feld im Schnappschuss ist absender.zahlungsziel_zeigen in der
    # Vorlage IMMER undefiniert -- und der Zahlungsabsatz verschwand auf jeder
    # Rechnung, auch bei eingeschalteter Einstellung.
    "zahlungsziel_zeigen",
)


class FestschreibenFehler(RuntimeError):
    """Das Festschreiben wurde abgebrochen. Die Meldung ist fuer den Nutzer."""


@dataclass(frozen=True, slots=True)
class Ergebnis:
    rechnung_id: int
    nummer: str
    pdf_pfad: str | None
    pdf_sha256: str | None
    pdf_fehler: str | None = None


def _snapshot(zeile: sqlite3.Row | None, felder: tuple[str, ...]) -> str:
    if zeile is None:
        return "{}"
    return json.dumps({f: zeile[f] for f in felder}, ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------ Entwurf

def entwurf_anlegen(
    conn: sqlite3.Connection,
    kunde_id: int,
    zeit_ids: list[int],
    *,
    modus: str = gruppierung.PRO_TITEL,
    datum: str | None = None,
    einleitung: str | None = None,
    schluss: str | None = None,
    betreff: str | None = None,
    einzelnachweis: bool = False,
    leistungszeitraum_zeigen: bool | None = None,
    zeitraum: tuple[str, str] | None = None,
) -> int:
    """Legt einen Rechnungsentwurf aus Zeiteintraegen an.

    Die Zeiteintraege werden sofort belegt, damit kein zweiter Entwurf dieselbe
    Zeit greift. Die Nummer wird NICHT vergeben -- das passiert erst beim
    Festschreiben, und genau dadurch hinterlaesst ein verworfener Entwurf keine
    Luecke im Nummernkreis.

    Ohne ``zeit_ids`` entsteht ein Entwurf fuer freie Positionen (Affiliate-
    Provision zum Monatsende, Pauschale ohne Zeiterfassung); dann nennt
    ``zeitraum`` (von, bis) den Leistungszeitraum, und die Positionen kommen
    danach ueber :func:`freie_position_anlegen`. Bis dahin lautet der Entwurf
    auf 0 €, und R06 haelt das Festschreiben auf.
    """
    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    if betrieb is None:
        raise FestschreibenFehler("Die Betriebsdaten fehlen. Bitte zuerst einrichten.")
    kunde = conn.execute("SELECT * FROM kunde WHERE id = ?", (kunde_id,)).fetchone()
    if kunde is None:
        raise FestschreibenFehler("Der Kunde existiert nicht.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if zeitraum is None:
            frei = _freie_zeiten(conn, kunde_id, zeit_ids)
            if not frei:
                raise FestschreibenFehler(
                    "Keiner der gewählten Zeiteinträge ist noch abrechenbar."
                )
            von, bis = min(z["datum"] for z in frei), max(z["datum"] for z in frei)
        else:
            # Ohne Zeiten, mit genanntem Zeitraum: der leere Entwurf fuer
            # freie Positionen. Zeiten UND Zeitraum zugleich gibt es nicht --
            # der Zeitraum einer Zeitrechnung kommt aus den Zeiten.
            if zeit_ids:
                raise FestschreibenFehler(
                    "Entweder Zeiten auswählen oder einen Leistungszeitraum nennen.")
            frei = []
            von, bis = zeitraum
            if von > bis:
                raise FestschreibenFehler("Der Leistungszeitraum endet vor seinem Beginn.")
        rechnungsdatum = datum or db.heute()
        ziel = kunden.zahlungsziel_tage(conn, kunde_id)
        # Die Regel wird HIER eingefroren: jede spaetere Neuberechnung der
        # Positionen nimmt sie vom Entwurf, nicht vom Kunden (kein stilles
        # Umrechnen, wenn sich die Regel am Kunden aendert).
        regel = kunden.rundungsregel(conn, kunde_id)
        try:
            faellig = (date.fromisoformat(rechnungsdatum) + timedelta(days=ziel)).isoformat()
        except (ValueError, OverflowError) as fehler:
            # OverflowError: "9999-12-31" ist ein gueltiges Datum, aber die
            # Faelligkeit dahinter gibt es nicht mehr. Die Route prueft das
            # Fenster vorher; hier ist das Netz fuer direkte Aufrufer.
            raise FestschreibenFehler(
                f"„{rechnungsdatum}“ ist kein brauchbares Rechnungsdatum."
            ) from fehler

        cur = conn.execute(
            """
            INSERT INTO rechnung (typ, kunde_id, datum, leistung_von, leistung_bis,
                                  zahlungsziel_tage, faellig_am, leistungsort,
                                  hinweis_19_text, betreff,
                                  einleitung, schluss, rundung_hinweis, gruppierung,
                                  einzelnachweis, leistungszeitraum_zeigen,
                                  rundung_modus, rundung_intervall_min,
                                  status, erstellt_am)
            VALUES ('RECHNUNG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'ENTWURF', ?)
            """,
            (
                kunde_id, rechnungsdatum, von, bis,
                ziel, faellig, kunde["leistungsort"], steuerhinweis(betrieb, kunde),
                betreff if betreff is not None else vorschlag_betreff(kunde, von, bis),
                einleitung, schluss, _rundungshinweis(betrieb, regel, modus), modus,
                1 if einzelnachweis else 0,
                # Ohne ausdrueckliche Angabe gilt die Betriebsvorbelegung.
                (betrieb["leistungszeitraum_default"]
                 if leistungszeitraum_zeigen is None
                 else int(leistungszeitraum_zeigen)),
                regel.modus, regel.intervall_min,
                db.jetzt_utc(),
            ),
        )
        rechnung_id = int(cur.lastrowid)
        if frei:
            conn.execute(
                f"UPDATE zeiteintrag SET rechnung_id = ? WHERE id IN "
                f"({', '.join('?' * len(frei))})",
                (rechnung_id, *[z["id"] for z in frei]),
            )
        _schreibe_positionen(conn, rechnung_id, modus=modus)
        db.protokolliere(conn, "ENTWURF", "rechnung", rechnung_id,
                         wert_neu={"kunde_id": kunde_id, "zeiten": len(frei), "modus": modus})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return rechnung_id


#: Monatsnamen fuer die Betreffzeile. ``strftime('%B')`` haengt an der Locale
#: des Rechners -- auf einem System mit englischer Locale stuende "October" auf
#: einer deutschen Rechnung. Eine Liste ist hier das kleinere Uebel.
MONATE = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


def vorschlag_betreff(kunde: sqlite3.Row, von: str, bis: str) -> str:
    """Die vorbelegte Betreffzeile: Zeitraum und Kundenname.

    Keine Pflichtangabe -- § 34a UStDV kennt die Betreffzeile nicht --, aber die
    Zeile, nach der der Empfaenger die Rechnung in seiner Ablage wiederfindet.
    Deshalb vorbelegt und nicht leer: eine leere Zeile fuellt niemand nach.

    >>> vorschlag_betreff({"name": "Alpenbau"}, "2026-10-01", "2026-10-31")
    'Rechnung – Oktober 2026 – Alpenbau'
    >>> vorschlag_betreff({"name": "Alpenbau"}, "2026-09-28", "2026-10-31")
    'Rechnung – September bis Oktober 2026 – Alpenbau'
    """
    jahr_von, monat_von = int(von[:4]), int(von[5:7])
    jahr_bis, monat_bis = int(bis[:4]), int(bis[5:7])
    if (jahr_von, monat_von) == (jahr_bis, monat_bis):
        zeitraum = f"{MONATE[monat_von - 1]} {jahr_von}"
    elif jahr_von == jahr_bis:
        zeitraum = f"{MONATE[monat_von - 1]} bis {MONATE[monat_bis - 1]} {jahr_bis}"
    else:
        zeitraum = (
            f"{MONATE[monat_von - 1]} {jahr_von} bis {MONATE[monat_bis - 1]} {jahr_bis}"
        )
    return f"Rechnung – {zeitraum} – {kunde['name']}"


def leistungsort_nachziehen(conn: sqlite3.Connection, rechnung_id: int) -> bool:
    """Holt den Leistungsort des Kunden in einen ENTWURF nach.

    Der Leistungsort wird beim Anlegen des Entwurfs eingefroren, damit er sich
    nicht mit dem Kundenstamm aendert -- fuer eine festgeschriebene Rechnung ist
    das richtig. Beim Entwurf ist es eine Falle: wer beim Anlegen merkt, dass
    der Kunde in Wien sitzt, korrigiert den Kundenstamm und schreibt den
    Entwurf fest -- und bekommt trotzdem eine Inlandsrechnung mit § 19-Hinweis.
    Reparieren liess sich das nirgends.

    Gibt True zurueck, wenn sich etwas geaendert hat.
    """
    rechnung = conn.execute(
        "SELECT status, kunde_id, leistungsort FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()
    if rechnung is None or rechnung["status"] != "ENTWURF":
        return False
    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    kunde = conn.execute("SELECT * FROM kunde WHERE id = ?", (rechnung["kunde_id"],)).fetchone()
    if kunde is None or kunde["leistungsort"] == rechnung["leistungsort"]:
        return False
    conn.execute(
        "UPDATE rechnung SET leistungsort = ?, hinweis_19_text = ?, version = version + 1"
        " WHERE id = ? AND status = 'ENTWURF'",
        (kunde["leistungsort"], steuerhinweis(betrieb, kunde), rechnung_id),
    )
    db.protokolliere(
        conn, "AENDERN", "rechnung", rechnung_id, feld="leistungsort",
        wert_alt=rechnung["leistungsort"], wert_neu=kunde["leistungsort"],
        begruendung="Leistungsort des Kunden nachgezogen",
    )
    return True


def steuerhinweis(betrieb: sqlite3.Row, kunde: sqlite3.Row) -> str:
    """Der Hinweis, der auf diese Rechnung gehoert -- je nach Leistungsort.

    Hier liegt ein Fehler, der sonst unauffaellig bliebe: der § 19-Hinweis ist
    auf einer Leistung an einen Unternehmer im uebrigen Gemeinschaftsgebiet
    **falsch**. Denn § 19 Abs. 1 UStG macht einen *steuerbaren* Umsatz
    steuerfrei -- eine Leistung nach § 3a Abs. 2 UStG ist dagegen in
    Deutschland gar nicht steuerbar. Der Umsatz ist nicht steuerfrei, er findet
    umsatzsteuerlich nicht hier statt.

    Was stattdessen draufmuss, sagt § 14a Abs. 1 Satz 1 UStG: die Angabe
    "Steuerschuldnerschaft des Leistungsempfaengers". Von § 14a nimmt
    § 19 Abs. 1 Satz 2 nur die USt-IdNr.-Pflicht (Satz 3) aus, nicht den
    Hinweis selbst -- er bleibt also auch fuer einen Kleinunternehmer Pflicht.

    Beim **Drittland** gibt es keine vorgeschriebene Angabe, und "Reverse
    Charge" waere dort eine Behauptung ueber einen Mechanismus, den es
    ausserhalb der EU nicht gibt. Deshalb ein neutraler Satz, der nur sagt, was
    zutrifft: der Umsatz ist im Inland nicht steuerbar.
    """
    if kunde["leistungsort"] == "EU_B2B_REVERSE_CHARGE":
        return betrieb["hinweis_rc_text"]
    if kunde["leistungsort"] == "DRITTLAND":
        return HINWEIS_DRITTLAND
    return betrieb["hinweis_19_text"]


def _rundungshinweis(
    betrieb: sqlite3.Row | None, regel: rundung.Regel, modus: str
) -> str | None:
    """Der Satz, der die Rundung auf der Rechnung sichtbar macht -- wenn gerundet wird.

    Eine Rundungsregel, die der Kunde nie gelesen hat, ist angreifbar. Sie
    gehoert ins Angebot oder in die AGB -- und sichtbar auf die Rechnung,
    und zwar so, wie sie angewandt wurde: die SUMME je Position ist gerundet.
    Vorher stand hier „Abrechnung in angefangenen 15 Minuten.“ -- das
    beschrieb eine Zeittaktklausel je Taetigkeit, die nie stattfand, und
    stand auch auf Rechnungen „pro Eintrag“, die gar nicht runden. Ohne
    Rundung steht jetzt kein Rundungssatz auf dem Beleg.

    Ein eigener Hinweistext des Betriebs gilt nur, solange der Entwurf mit
    genau der Regel des Betriebs rechnet -- fuer eine Kundenregel waere er
    falsch. Festgeschriebene Rechnungen behalten ihren gespeicherten Satz.
    """
    if not regel.wirkt(modus):
        return None
    eigener = betrieb["rundung_hinweis"] if betrieb is not None else None
    if eigener and regel.quelle != "KUNDE" and regel.gleich(rundung.Regel(
        betrieb["rundung_modus"], int(betrieb["rundung_intervall_min"])
    )):
        return eigener
    return regel.hinweis(modus)


def regel_des_entwurfs(conn: sqlite3.Connection, rechnung: sqlite3.Row) -> rundung.Regel:
    """Die Rundungsregel, mit der ein Entwurf rechnet: die beim Anlegen eingefrorene.

    Ohne eingefrorene Regel (nur bei einer Zeile aus der Zeit vor Version 21,
    die die Migration nicht erreichte) gilt die heutige des Kunden.
    """
    if rechnung["rundung_modus"] is None:
        return kunden.rundungsregel(conn, rechnung["kunde_id"])
    eigene = conn.execute(
        "SELECT rundung_modus FROM kunde WHERE id = ?", (rechnung["kunde_id"],)
    ).fetchone()
    return rundung.Regel(
        rechnung["rundung_modus"],
        int(rechnung["rundung_intervall_min"] or kunden.betriebsregel(conn).intervall_min),
        "KUNDE" if eigene is not None and eigene["rundung_modus"] is not None else "ENTWURF",
    )


def _freie_zeiten(
    conn: sqlite3.Connection, kunde_id: int, zeit_ids: list[int]
) -> list[sqlite3.Row]:
    if not zeit_ids:
        return []
    platzhalter = ", ".join("?" * len(zeit_ids))
    # Ein laufender Timer (Start ohne Ende) hat noch keine Dauer -- er steht
    # mit einer Platzhalterminute in der Tabelle. Auf einem Entwurf waere das
    # eine Position ueber eine Minute, die beim Stoppen des Timers nie mehr
    # nachgezogen wird.
    return conn.execute(
        f"""
        SELECT * FROM zeiteintrag
         WHERE id IN ({platzhalter}) AND kunde_id = ?
           AND rechnung_id IS NULL AND abgeschrieben_am IS NULL
           AND abrechnungsmodus <> 'INTERN'
           AND NOT (start_zeit IS NOT NULL AND end_zeit IS NULL)
         ORDER BY datum, id
        """,
        (*zeit_ids, kunde_id),
    ).fetchall()


#: Meldung, wenn eine Stundenposition keinen Satz haette. Die Datenbank wiese
#: die Position ab (CHECK einzelpreis_cent > 0 OR nachlass_grund), und der
#: Nutzer saehe einen Serverfehler -- ausgerechnet beim ersten Klickweg, wenn
#: der Satz nach der Einrichtung noch nirgends steht.
KEIN_STUNDENSATZ = (
    "Für diesen Kunden ist kein Stundensatz hinterlegt — am Kunden oder in den "
    "Einstellungen eintragen."
)


def _schreibe_positionen(conn: sqlite3.Connection, rechnung_id: int, *, modus: str) -> None:
    """Erzeugt die Positionen eines Entwurfs neu und traegt die Kopfsumme nach."""
    rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    if rechnung is None:
        raise FestschreibenFehler("Diese Rechnung gibt es nicht (mehr).")
    if rechnung["status"] != "ENTWURF":
        raise FestschreibenFehler("Nur ein Entwurf kann neu gruppiert werden.")

    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    zeiten = conn.execute(
        "SELECT * FROM zeiteintrag WHERE rechnung_id = ? ORDER BY datum, id", (rechnung_id,)
    ).fetchall()

    # Freie Positionen (quelle FREI) haengen an keiner Zeit: sie bleiben bei
    # jeder Neuberechnung stehen und ruecken hinter die Zeitpositionen. Erst
    # aus dem Weg (UNIQUE rechnung_id, pos_nr), dann hinten anreihen.
    freie = conn.execute(
        "SELECT id, betrag_cent FROM rechnungsposition WHERE rechnung_id = ?"
        " AND quelle = 'FREI' ORDER BY pos_nr, id", (rechnung_id,),
    ).fetchall()
    conn.execute("DELETE FROM rechnungsposition WHERE rechnung_id = ? AND quelle <> 'FREI'",
                 (rechnung_id,))
    for lauf, zeile in enumerate(freie, start=1):
        conn.execute("UPDATE rechnungsposition SET pos_nr = ? WHERE id = ?",
                     (-lauf, zeile["id"]))

    # Die Regel des ENTWURFS, nicht die heutige: ein gestoppter Timer oder
    # eine geaenderte Dauer soll den Entwurf nachziehen, ihn aber nicht still
    # auf eine inzwischen geaenderte Kunden- oder Betriebsregel umrechnen.
    regel = regel_des_entwurfs(conn, rechnung)
    positionen = gruppierung.erzeuge_positionen(
        [dict(z) for z in zeiten],
        modus=modus,
        standard_cent=kunden.satz_cent(conn, rechnung["kunde_id"]),
        intervall_min=regel.intervall_min,
        modus_rundung=regel.modus,
    )
    # Vor dem INSERT pruefen, nicht die Datenbank entscheiden lassen: ein
    # Stundensatz von null (Betrieb ohne Standardsatz, Kunde ohne eigenen
    # Satz) ergibt eine Position ohne Betrag, und die Meldung dazu muss den
    # Nutzer erreichen, nicht ein IntegrityError.
    if any(
        p.quelle == "ZEIT" and p.einzelpreis_cent <= 0 and p.nachlass_grund is None
        for p in positionen
    ):
        raise FestschreibenFehler(KEIN_STUNDENSATZ)
    for nr, position in enumerate(positionen, start=1):
        cur = conn.execute(
            """
            INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung, untertext,
                   menge_tausendstel, einheit, einzelpreis_cent, nachlass_cent,
                   nachlass_grund, darstellung, betrag_cent, quelle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rechnung_id, nr, position.bezeichnung, position.untertext,
                position.menge_tausendstel, position.einheit, position.einzelpreis_cent,
                position.nachlass_cent, position.nachlass_grund, position.darstellung,
                position.betrag_cent, position.quelle,
            ),
        )
        for zeit_id in position.zeiteintrag_ids:
            conn.execute(
                "INSERT INTO position_zeiteintrag (position_id, zeiteintrag_id) VALUES (?, ?)",
                (cur.lastrowid, zeit_id),
            )

    for lauf, zeile in enumerate(freie, start=len(positionen) + 1):
        conn.execute("UPDATE rechnungsposition SET pos_nr = ? WHERE id = ?",
                     (lauf, zeile["id"]))

    summe = gruppierung.summe_cent(positionen) + sum(int(z["betrag_cent"]) for z in freie)
    # Der Rundungssatz spricht von Zeitsummen: ohne Zeitposition (nur freie
    # Positionen, etwa eine Provision) hat er auf dem Papier nichts verloren.
    hinweis = (_rundungshinweis(betrieb, regel, modus)
               if any(p.quelle == "ZEIT" for p in positionen) else None)
    # version + 1: die Summe ist das betragstragende Feld. Das Festschreiben
    # prueft die Version, die das Formular beim Laden gesehen hat -- aendern
    # sich die Positionen dazwischen (Gruppierung, eine Zeit auf Kulanz),
    # muss diese Pruefung greifen, sonst wird festgeschrieben, was der
    # Nutzer so nicht gesehen hat.
    #
    # Der Rundungshinweis folgt der Gruppierung: „pro Eintrag“ rundet nicht,
    # und dann steht auch kein Rundungssatz auf dem Beleg.
    conn.execute(
        "UPDATE rechnung SET netto_cent = ?, brutto_cent = ?, gruppierung = ?,"
        " rundung_hinweis = ?, version = version + 1 WHERE id = ?",
        (summe, summe, modus, hinweis, rechnung_id),
    )


def positionen_neu(conn: sqlite3.Connection, rechnung_id: int) -> None:
    """Erzeugt die Positionen eines Entwurfs aus seinen Zeiten neu.

    Oeffnet BEWUSST keine Transaktion: der Aufrufer haelt sie bereits. Gebraucht
    wird das ueberall dort, wo sich eine Zeit aendert, die schon in einem
    Entwurf liegt -- die Positionen entstehen beim Anlegen des Entwurfs und
    wissen von spaeteren Aenderungen nichts. Ohne diesen Nachzug berechnete die
    Rechnung eine Stunde, die inzwischen erlassen oder abgeschrieben wurde.
    """
    zeile = conn.execute(
        "SELECT status, gruppierung FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()
    if zeile is None or zeile["status"] != "ENTWURF":
        return
    _schreibe_positionen(conn, rechnung_id, modus=zeile["gruppierung"])


def gruppierung_wechseln(conn: sqlite3.Connection, rechnung_id: int, modus: str) -> None:
    """Erzeugt die Positionen mit einem anderen Gruppierungsmodus neu."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _schreibe_positionen(conn, rechnung_id, modus=modus)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def rundung_uebernehmen(conn: sqlite3.Connection, rechnung_id: int) -> bool:
    """Uebernimmt die heutige Rundungsregel des Kunden in einen Entwurf.

    Der Entwurf rechnet mit der Regel vom Anlegen; aendert sich die Regel am
    Kunden oder im Betrieb, bleibt er, wie er ist -- bis der Nutzer es so
    will. Dann hier: Regel neu einfrieren, Positionen neu erzeugen, Journal.
    Gibt False zurueck, wenn es nichts zu uebernehmen gab.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
        if rechnung is None:
            raise FestschreibenFehler("Diese Rechnung gibt es nicht (mehr).")
        if rechnung["status"] != "ENTWURF":
            raise FestschreibenFehler("Nur ein Entwurf rechnet mit einer neuen Regel.")
        alt = regel_des_entwurfs(conn, rechnung)
        neu = kunden.rundungsregel(conn, rechnung["kunde_id"])
        if alt.gleich(neu) and rechnung["rundung_modus"] is not None:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "UPDATE rechnung SET rundung_modus = ?, rundung_intervall_min = ? WHERE id = ?",
            (neu.modus, neu.intervall_min, rechnung_id),
        )
        db.protokolliere(
            conn, "AENDERN", "rechnung", rechnung_id, feld="rundung",
            wert_alt={"modus": alt.modus, "intervall_min": alt.intervall_min},
            wert_neu={"modus": neu.modus, "intervall_min": neu.intervall_min},
            begruendung="Rundungsregel des Kunden übernommen",
        )
        _schreibe_positionen(conn, rechnung_id, modus=rechnung["gruppierung"])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def entwurf_loeschen(conn: sqlite3.Connection, rechnung_id: int) -> None:
    """Verwirft einen Entwurf und gibt die Zeiteintraege frei.

    Der Loeschpfad muss explizit sein: solange ein Zeiteintrag auf den Entwurf
    zeigt, scheitert DELETE FROM rechnung am Fremdschluessel.

    Status lesen und loeschen in EINER Transaktion, und der Journaleintrag
    nur, wenn das DELETE eine Zeile getroffen hat. Vorher lief die
    Statuspruefung davor: vier gleichzeitige Klicks auf "Verwerfen"
    (Doppelklick, zwei Tabs) sahen alle noch den Entwurf, drei DELETEs trafen
    nichts -- und das unveraenderliche Journal bekam viermal
    ENTWURF_VERWORFEN (§ 146 Abs. 4 AO). Ein schon verworfener Entwurf ist
    kein Fehler: das Ziel ist erreicht.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rechnung = conn.execute(
            "SELECT status FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        if rechnung is not None and rechnung["status"] != "ENTWURF":
            raise FestschreibenFehler(
                "Nur ein Entwurf kann verworfen werden. Eine festgeschriebene Rechnung "
                "wird storniert."
            )
        if rechnung is not None:
            entwurf_verwerfen(conn, rechnung_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def entwurf_verwerfen(
    conn: sqlite3.Connection, rechnung_id: int, *, begruendung: object = None
) -> bool:
    """Loescht einen Entwurf samt Positionen und gibt seine Zeiten frei.

    Oeffnet BEWUSST keine Transaktion: :func:`entwurf_loeschen` haelt sie,
    ebenso das Zuruecknehmen einer festgeschriebenen Rechnung
    (``rechnungsablauf``), die vorher wieder Entwurf geworden ist, und das
    Loeschen eines Kunden (``kunden.loeschen``) mit seinen Entwuerfen.

    Alles andere als ein Entwurf bleibt unberuehrt (False). Das Journal
    bekommt den INHALT, nicht nur die Kennung: Kunde, Summe, Positionen und
    Zeiten -- und nur, wenn das DELETE eine Zeile getroffen hat. Ein Eintrag
    "Entwurf 17 verworfen" sagt in einem Jahr niemandem mehr, was da
    verworfen wurde. Gibt True zurueck, wenn eine Zeile geloescht wurde.
    """
    rechnung = conn.execute("SELECT status FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    if rechnung is None or rechnung["status"] != "ENTWURF":
        return False
    inhalt = rechnungsinhalt(conn, rechnung_id)
    conn.execute(
        "UPDATE zeiteintrag SET rechnung_id = NULL, geaendert_am = ?"
        " WHERE rechnung_id = ?",
        (db.jetzt_utc(), rechnung_id),
    )
    geloescht = conn.execute(
        "DELETE FROM rechnung WHERE id = ? AND status = 'ENTWURF'", (rechnung_id,)
    ).rowcount
    if geloescht == 1:
        db.protokolliere(conn, "ENTWURF_VERWORFEN", "rechnung", rechnung_id,
                         wert_alt=inhalt, begruendung=begruendung)
    return geloescht == 1


def rechnungsinhalt(conn: sqlite3.Connection, rechnung_id: int) -> dict | None:
    """Der vollstaendige Inhalt einer Rechnung als ein Wert fuers Journal.

    Kopf (mit den Schnappschuessen als Objekt, nicht als JSON im JSON),
    Positionen und die zugeordneten Zeiten. Gebraucht, wo eine Rechnung
    verschwindet oder ihre Nummer verliert: dann ist das Journal der Ort,
    an dem der urspruengliche Inhalt feststellbar bleibt (§ 146 Abs. 4 AO).
    """
    zeile = conn.execute(
        "SELECT r.*, k.name AS kunde_name FROM rechnung r"
        " LEFT JOIN kunde k ON k.id = r.kunde_id WHERE r.id = ?",
        (rechnung_id,),
    ).fetchone()
    if zeile is None:
        return None
    kopf = dict(zeile)
    for spalte in ("kunde_snapshot_json", "absender_snapshot_json"):
        roh = kopf.pop(spalte, None)
        try:
            kopf[spalte.removesuffix("_json")] = json.loads(roh) if roh else None
        except ValueError:
            kopf[spalte.removesuffix("_json")] = roh
    positionen = [
        {k: p[k] for k in ("pos_nr", "bezeichnung", "untertext", "menge_tausendstel",
                           "einheit", "einzelpreis_cent", "nachlass_cent", "nachlass_grund",
                           "darstellung", "betrag_cent", "quelle")}
        for p in conn.execute(
            "SELECT * FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr",
            (rechnung_id,),
        )
    ]
    zeiten = [
        dict(z) for z in conn.execute(
            "SELECT id, datum, titel, dauer_minuten, abrechnungsmodus, satz_cent_snapshot"
            " FROM zeiteintrag WHERE rechnung_id = ? ORDER BY datum, id",
            (rechnung_id,),
        )
    ]
    return {"kopf": kopf, "positionen": positionen, "zeiten": zeiten}


# ------------------------------------------------------------- Festschreiben

def festschreiben(
    conn: sqlite3.Connection,
    rechnung_id: int,
    *,
    version: int | None = None,
    bestaetigte_warnungen: list[str] | None = None,
    belegordner: Path | None = None,
) -> Ergebnis:
    """Schreibt einen Entwurf fest: Nummer, Snapshot, Sperre, PDF, Hash."""
    # Zuerst den Leistungsort des Kunden nachziehen, DANN pruefen. Sonst
    # prueft die Validierung einen Entwurf, der nicht mehr zum Kunden passt --
    # und schreibt im schlimmsten Fall eine § 19-Rechnung fest, deren
    # Kundenschnappschuss "EU-Unternehmer" sagt. Aendert sich dabei etwas,
    # steigt die Version, und ein Formular mit der alten Version wird unten
    # abgewiesen: der Nutzer sieht die Seite neu, mit dem neuen Hinweis.
    conn.execute("BEGIN IMMEDIATE")
    try:
        leistungsort_nachziehen(conn, rechnung_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    befunde = validierung.pruefe(conn, rechnung_id)
    rot = [b for b in befunde if b.stufe == validierung.ROT]
    if rot:
        raise FestschreibenFehler(
            "Festschreiben nicht möglich:\n" + "\n".join(f"· {b.text}" for b in rot)
        )

    rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    kunde = conn.execute("SELECT * FROM kunde WHERE id = ?", (rechnung["kunde_id"],)).fetchone()
    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    jahr, monat = nummernkreis.jahr_monat(rechnung["datum"])

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Vorbedingungen erneut pruefen -- nicht der Oberflaeche vertrauen.
        # Zuerst der Status: liegt die Rechnung schon fest, darf unten keine
        # Zeit mehr angefasst werden -- der Sperr-Trigger wiese das ab, und
        # statt der Meldung "bereits festgeschrieben" kaeme ein Datenbankfehler.
        aktuell = conn.execute(
            "SELECT status FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        if aktuell is None or aktuell["status"] != "ENTWURF":
            raise FestschreibenFehler(
                "Die Rechnung wurde zwischenzeitlich geändert oder bereits festgeschrieben. "
                "Bitte die Seite neu laden."
            )
        for zeit in conn.execute(
            "SELECT id FROM zeiteintrag WHERE rechnung_id = ?", (rechnung_id,)
        ):
            fremd = conn.execute(
                """
                SELECT r.nummer FROM position_zeiteintrag pz
                  JOIN rechnungsposition p ON p.id = pz.position_id
                  JOIN rechnung r          ON r.id = p.rechnung_id
                 WHERE pz.zeiteintrag_id = ? AND r.status <> 'STORNIERT' AND r.id <> ?
                 LIMIT 1
                """,
                (zeit["id"], rechnung_id),
            ).fetchone()
            if fremd:
                raise FestschreibenFehler(
                    f"Eine Zeit liegt inzwischen auf Rechnung {fremd['nummer']}."
                )

        # Den ABGERECHNETEN Satz einfrieren, BEVOR die Rechnung auf
        # FESTGESCHRIEBEN geht: danach sperrt der Trigger zeit_gesperrt jede
        # Aenderung am Zeiteintrag -- zu Recht, denn dann ist er Teil eines
        # unveraenderbaren Belegs.
        #
        # Abgerechnet ist der Einzelpreis der Position, auf der die Zeit liegt
        # -- nicht der Kundensatz von heute. Wer zwischen Entwurf und
        # Festschreiben den Satz am Kunden erhoeht, bekommt die Rechnung zum
        # alten Satz (die Positionen stehen schon, G09 sagt es); der
        # Schnappschuss an der Zeit muss dasselbe sagen, sonst zeigen
        # Wertauswertung und eine spaetere Neuabrechnung nach Storno einen
        # Satz, der nie auf dem Papier stand. Fuer eine Pauschale ist der
        # Einzelpreis der Festpreis und kein Satz; dort bleibt es beim
        # bisherigen Rueckfall auf den Kundensatz.
        conn.execute(
            """
            UPDATE zeiteintrag
               SET satz_cent_snapshot = COALESCE(
                       (SELECT p.einzelpreis_cent
                          FROM position_zeiteintrag pz
                          JOIN rechnungsposition p ON p.id = pz.position_id
                         WHERE pz.zeiteintrag_id = zeiteintrag.id
                           AND p.rechnung_id = zeiteintrag.rechnung_id
                           AND p.quelle = 'ZEIT'
                         LIMIT 1),
                       satz_cent_snapshot,
                       (SELECT COALESCE(k.satz_cent, b.standardsatz_cent, 0)
                          FROM kunde k LEFT JOIN betrieb b ON b.id = 1
                         WHERE k.id = zeiteintrag.kunde_id)),
                   version = version + 1
             WHERE rechnung_id = ?
            """,
            (rechnung_id,),
        )

        nummernkreis.sichere_kreis(conn, jahr)
        nummer = nummernkreis.ziehe_nummer(conn, jahr, monat=monat)

        # Optimistisches Sperren: zwei offene Tabs oder ein Doppelklick duerfen
        # nicht zwei Nummern verbrauchen.
        erwartet = version if version is not None else rechnung["version"]
        cur = conn.execute(
            """
            UPDATE rechnung
               SET status = 'FESTGESCHRIEBEN', nummer = ?, nummer_seq = ?, nummer_jahr = ?,
                   kunde_snapshot_json = ?, absender_snapshot_json = ?,
                   festgeschrieben_am = ?, version = version + 1
             WHERE id = ? AND status = 'ENTWURF' AND version = ?
            """,
            (
                nummer.text, nummer.seq, nummer.jahr,
                _snapshot(kunde, KUNDE_SNAPSHOT), _snapshot(betrieb, BETRIEB_SNAPSHOT),
                db.jetzt_utc(), rechnung_id, erwartet,
            ),
        )
        if cur.rowcount != 1:
            raise FestschreibenFehler(
                "Die Rechnung wurde zwischenzeitlich geändert oder bereits festgeschrieben. "
                "Bitte die Seite neu laden."
            )
        db.protokolliere(
            conn, "FESTSCHREIBEN", "rechnung", rechnung_id,
            feld="nummer", wert_neu=nummer.text,
            begruendung={"bestaetigte_warnungen": bestaetigte_warnungen or []},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Ab hier ist die Rechnung gueltig. Das PDF kommt danach.
    pfad, hash_, fehler = _pdf_schreiben(conn, rechnung_id, belegordner)
    return Ergebnis(rechnung_id, nummer.text, pfad, hash_, fehler)


def _pdf_schreiben(
    conn: sqlite3.Connection, rechnung_id: int, belegordner: Path | None
) -> tuple[str | None, str | None, str | None]:
    rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    datenordner = belegordner or db.standard_datenordner()
    ordner = datenordner / "ausgangsrechnungen" / str(rechnung["nummer_jahr"])
    name = f"{rechnung['nummer']}_{_dateiname(rechnung)}.pdf"
    ziel = ordner / name
    try:
        hash_ = pdf.rendere(pdf_daten(conn, rechnung_id), ziel)
    except Exception as fehler:  # noqa: BLE001 -- die Meldung geht an den Nutzer
        db.protokolliere(conn, "PDF_FEHLER", "rechnung", rechnung_id,
                         begruendung=str(fehler)[:500])
        return None, None, str(fehler)

    # In der Datenbank steht der Pfad RELATIV zum Datenordner (app/pfade.py):
    # nach einer Wiederherstellung auf einem anderen Rechner zeigte ein
    # absoluter Pfad auf jedes PDF ins Leere. Zurueckgegeben wird der
    # absolute, denn der Aufrufer will die Datei jetzt oeffnen.
    gespeichert = pfade.relativ(datenordner, ziel)
    conn.execute(
        "UPDATE rechnung SET pdf_pfad = ?, pdf_sha256 = ? WHERE id = ?",
        (gespeichert, hash_, rechnung_id),
    )
    db.protokolliere(conn, "PDF", "rechnung", rechnung_id, wert_neu=gespeichert, hash_=hash_)
    return str(ziel), hash_, None


def _dateiname(rechnung: sqlite3.Row) -> str:
    """Dateinamensteil aus dem Kundennamen, ohne Umlaute und Sonderzeichen."""
    try:
        name = json.loads(rechnung["kunde_snapshot_json"] or "{}").get("name", "Kunde")
    except ValueError:
        name = "Kunde"
    ersetzt = (
        name.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue").replace("ß", "ss")
    )
    sauber = "".join(z if z.isalnum() else "-" for z in ersetzt).strip("-")
    while "--" in sauber:
        sauber = sauber.replace("--", "-")
    return sauber[:48] or "Kunde"


#: Meldung, wenn jemand ein vorhandenes PDF neu erzeugen will.
PDF_VORHANDEN = (
    "Das PDF ist vorhanden; es wird nicht neu erzeugt, damit Hash und versendetes "
    "Exemplar übereinstimmen."
)


def pdf_vorhanden(pdf_pfad: str | None, belegordner: Path | None = None) -> bool:
    """Liegt die Datei zu diesem gespeicherten Pfad auf der Platte?

    Die Datenbank kennt nur den Pfad; ob die Datei noch da ist, weiss nur
    das Dateisystem. Eine Ansicht, die nur ``pdf_pfad`` prueft, zeigt nach
    einem versehentlichen Loeschen einen Knopf "PDF oeffnen", der ins Leere
    fuehrt -- und keinen Weg, die Datei wiederherzustellen.
    """
    if not pdf_pfad:
        return False
    return pfade.absolut(belegordner or db.standard_datenordner(), pdf_pfad).is_file()


def pdf_nachziehen(
    conn: sqlite3.Connection, rechnung_id: int, belegordner: Path | None = None
) -> Ergebnis:
    """Erzeugt ein fehlendes PDF nachtraeglich.

    Scheitert das Rendering beim Festschreiben, existiert die Rechnung ohne
    Datei. Das ist sichtbar und behebbar -- im Gegensatz zu einer Waise mit
    verbrauchter Nummer, die entstuende, laege das Rendering in der Transaktion.

    Nur, wenn KEINE Datei auf der Platte liegt. Ein neues Rendering ersetzte
    sonst das versendete Exemplar und seinen gespeicherten SHA-256 -- der
    Erzeugungszeitpunkt steckt im PDF, und nach einer Vorlagenaenderung saehe
    das Archivexemplar anders aus als das beim Kunden. Der dokumentierte Hash
    waere dann nicht mehr nachpruefbar.
    """
    rechnung = conn.execute(
        "SELECT nummer, status, pdf_pfad FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()
    if rechnung is None or rechnung["status"] == "ENTWURF":
        raise FestschreibenFehler("Nur eine festgeschriebene Rechnung hat ein PDF.")
    if rechnung["pdf_pfad"] and pdf_vorhanden(rechnung["pdf_pfad"], belegordner):
        raise FestschreibenFehler(PDF_VORHANDEN)
    pfad, hash_, fehler = _pdf_schreiben(conn, rechnung_id, belegordner)
    return Ergebnis(rechnung_id, rechnung["nummer"], pfad, hash_, fehler)


# ------------------------------------------------------------------- Storno

def storniere(
    conn: sqlite3.Connection,
    rechnung_id: int,
    grund: str,
    *,
    zeiten_freigeben: bool = True,
    belegordner: Path | None = None,
) -> Ergebnis:
    """Storniert eine festgeschriebene Rechnung.

    Das Original bleibt vollstaendig erhalten, sein PDF bleibt im Ordner. Das
    Storno ist ein eigenes Dokument mit eigener Nummer und negativen Betraegen
    und heisst NIE "Gutschrift" -- das bedeutet nach § 34a Satz 1 Nr. 6 UStDV
    Abrechnung durch den Leistungsempfaenger.

    Die Nummer kommt aus dem Kreis des AUSSTELLUNGSJAHRES der Korrektur, also
    von heute -- nicht aus dem Jahr des Originals. Nummer und Datum eines
    Dokuments gehoeren zusammen: genau das prueft G03 bei jeder Rechnung, und
    eine Korrektur vom 19.10.2026 mit der Nummer RE-2024-0002 laeuft
    gegeneinander, taucht in der Jahresliste 2026 mit einer 2024er Nummer auf
    und benutzt einen Kreis, der nach Jahresende abgeschlossen sein sollte.

    Storniert wird nur eine Rechnung. Eine Rechnungskorrektur selbst zu
    stornieren ergaebe ein Dokument ueber +Betrag, das die Datenbank als
    Rechnung ueber 0 € abweist -- und einen Serverfehler statt einer Meldung.
    """
    if not grund.strip():
        raise FestschreibenFehler("Ein Storno braucht eine Begründung.")
    original = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    if original is None:
        raise FestschreibenFehler("Die Rechnung existiert nicht.")
    if original["typ"] == "STORNO":
        raise FestschreibenFehler("Eine Rechnungskorrektur kann nicht storniert werden.")
    if original["typ"] != "RECHNUNG":
        raise FestschreibenFehler("Nur eine Rechnung kann storniert werden.")
    if original["status"] != "FESTGESCHRIEBEN":
        raise FestschreibenFehler("Nur eine festgeschriebene Rechnung kann storniert werden.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Den Status INNERHALB der Transaktion noch einmal lesen. Die Pruefung
        # oben lief ohne Sperre; zwei gleichzeitige Storno-Anfragen (Doppelklick)
        # sahen beide FESTGESCHRIEBEN, und die zweite haette eine zweite
        # Korrektur zu derselben Rechnung erzeugt -- mit eigener Nummer und
        # noch einmal negativem Betrag. BEGIN IMMEDIATE stellt die zweite
        # hinter die erste; hier sieht sie dann STORNIERT.
        aktuell = conn.execute(
            "SELECT status FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        if aktuell is None or aktuell["status"] != "FESTGESCHRIEBEN":
            raise FestschreibenFehler(
                "Die Rechnung ist inzwischen storniert worden — ein zweites Storno gibt es nicht."
            )
        storno_id, nummer = storno_schreiben(
            conn, original, grund, zeiten_freigeben=zeiten_freigeben
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    pfad, hash_, fehler = _pdf_schreiben(conn, storno_id, belegordner)
    return Ergebnis(storno_id, nummer.text, pfad, hash_, fehler)


def storno_schreiben(
    conn: sqlite3.Connection,
    original: sqlite3.Row,
    grund: str,
    *,
    zeiten_freigeben: bool = True,
) -> tuple[int, nummernkreis.Nummer]:
    """Schreibt das Storno-Dokument zu ``original`` -- OHNE eigene Transaktion.

    Der Aufrufer haelt die Transaktion und hat den Status darin bereits
    gelesen: :func:`storniere` fuer das reine Storno, ``rechnungsablauf``
    fuer "Korrigieren", wo Storno und Ersatzentwurf gemeinsam gelten oder
    gemeinsam verfallen. Das PDF schreibt der Aufrufer NACH dem Commit.

    Gibt die Kennung des Storno-Dokuments und seine Nummer zurueck.
    """
    rechnung_id = int(original["id"])
    heute = db.heute()
    jahr, monat = nummernkreis.jahr_monat(heute)
    # Der abgerechnete Satz steht seit dem Festschreiben an jeder Zeit
    # (festschreiben() friert ihn ein, solange die Rechnung noch Entwurf
    # ist). COALESCE laesst ihn stehen; das UPDATE erhoeht dann nur die
    # Version, die zeit_gesperrt nicht sperrt. Der Kommentar an dieser Stelle
    # behauptete frueher, die Rechnung sei hier noch nicht festgeschrieben --
    # sie ist es laengst.
    conn.execute(
        """
        UPDATE zeiteintrag
           SET satz_cent_snapshot = COALESCE(satz_cent_snapshot, (
                   SELECT COALESCE(k.satz_cent, b.standardsatz_cent, 0)
                     FROM kunde k LEFT JOIN betrieb b ON b.id = 1
                    WHERE k.id = zeiteintrag.kunde_id)),
               version = version + 1
         WHERE rechnung_id = ?
        """,
        (rechnung_id,),
    )

    nummernkreis.sichere_kreis(conn, jahr)
    nummer = nummernkreis.ziehe_nummer(conn, jahr, monat=monat)
    cur = conn.execute(
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id,
               kunde_snapshot_json, absender_snapshot_json, datum,
               leistung_von, leistung_bis, netto_cent, brutto_cent,
               leistungsort, steuerregime,
               hinweis_19_text, betreff, rundung_hinweis,
               leistungszeitraum_zeigen,
               status, storno_von_id, storno_grund,
               festgeschrieben_am, erstellt_am)
        VALUES ('STORNO', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'FESTGESCHRIEBEN', ?, ?, ?, ?)
        """,
        (
            nummer.text, nummer.seq, nummer.jahr, original["kunde_id"],
            original["kunde_snapshot_json"], original["absender_snapshot_json"],
            heute, original["leistung_von"], original["leistung_bis"],
            -original["netto_cent"], -original["brutto_cent"],
            # Der Leistungsort MUSS mit. Ohne ihn griffe die Vorbelegung
            # INLAND: ein Storno einer Oesterreich-Rechnung zoege den Betrag
            # vom inlaendischen § 19-Gesamtumsatz ab, in dem das Original
            # nie stand -- die Grenze saehe niedriger aus, als sie ist. Auf
            # dem Papier fehlten ausserdem der Reverse-Charge-Absatz und die
            # USt-IdNr. des Empfaengers.
            original["leistungsort"], original["steuerregime"],
            original["hinweis_19_text"],
            # Betreff und Rundungshinweis ebenso: die Korrektur soll
            # aussehen wie das Dokument, das sie aufhebt.
            original["betreff"], original["rundung_hinweis"],
            original["leistungszeitraum_zeigen"],
            rechnung_id, grund.strip(),
            db.jetzt_utc(), db.jetzt_utc(),
        ),
    )
    storno_id = int(cur.lastrowid)
    for position in conn.execute(
        "SELECT * FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr",
        (rechnung_id,),
    ):
        conn.execute(
            """
            INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung, untertext,
                   menge_tausendstel, einheit, einzelpreis_cent, nachlass_cent,
                   nachlass_grund, darstellung, betrag_cent, quelle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                storno_id, position["pos_nr"], position["bezeichnung"],
                position["untertext"], position["menge_tausendstel"], position["einheit"],
                position["einzelpreis_cent"], position["nachlass_cent"],
                position["nachlass_grund"], position["darstellung"],
                position["betrag_cent"], position["quelle"],
            ),
        )

    conn.execute("UPDATE rechnung SET status = 'STORNIERT' WHERE id = ?", (rechnung_id,))
    if zeiten_freigeben:
        conn.execute(
            """
            UPDATE zeiteintrag
               SET rechnung_id = NULL, freigegeben_aus_rechnung_id = ?,
                   geaendert_am = ?, version = version + 1
             WHERE rechnung_id = ?
            """,
            (rechnung_id, db.jetzt_utc(), rechnung_id),
        )
    db.protokolliere(conn, "STORNO", "rechnung", rechnung_id,
                     wert_neu=nummer.text, begruendung=grund.strip())
    return storno_id, nummer


# --------------------------------------------------------------- PDF-Daten

def aus_snapshot(
    conn: sqlite3.Connection,
    rechnung: sqlite3.Row,
    spalte: str,
    felder: tuple[str, ...],
    tabelle: str,
    schluessel: int | None,
) -> dict:
    """Die Fremddaten einer Rechnung -- aus dem Schnappschuss, sonst aus dem Stamm.

    Der Rueckfall gilt dem ENTWURF: er hat noch keinen Schnappschuss, und bis
    zum Festschreiben ist der Stamm die richtige Quelle. Sobald der
    Schnappschuss steht, ist er die einzige: er ist das, was auf dem Papier
    steht, und das Papier ist beim Kunden.
    """
    if rechnung[spalte]:
        gespeichert = json.loads(rechnung[spalte])
        return {f: gespeichert.get(f) for f in felder}
    zeile = conn.execute(f"SELECT * FROM {tabelle} WHERE id = ?", (schluessel,)).fetchone()
    return {f: (zeile[f] if zeile else None) for f in felder}


def empfaenger_daten(conn: sqlite3.Connection, rechnung: sqlite3.Row) -> dict:
    """Der Rechnungsempfaenger, so wie er auf dem Papier steht."""
    return aus_snapshot(
        conn, rechnung, "kunde_snapshot_json", KUNDE_SNAPSHOT, "kunde", rechnung["kunde_id"]
    )


def pdf_daten(conn: sqlite3.Connection, rechnung_id: int) -> dict:
    """Baut die Daten fuer die PDF-Vorlage.

    Eine festgeschriebene Rechnung liest IMMER aus ihren Snapshots, nie aus den
    Stammdaten. Sonst zeigte das PDF im Ordner einen anderen Stand als die
    Anwendung, sobald der Kunde umzieht -- die unangenehmste Art Fehler, weil
    sie sich nachtraeglich nicht reparieren laesst.
    """
    rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    if rechnung is None:
        raise FestschreibenFehler("Die Rechnung existiert nicht.")

    empfaenger = empfaenger_daten(conn, rechnung)
    absender = aus_snapshot(
        conn, rechnung, "absender_snapshot_json", BETRIEB_SNAPSHOT, "betrieb", 1
    )

    # Die Positionen liegen positiv in der Datenbank. Beim Storno dreht die
    # AUSGABE das Vorzeichen -- so steht in der Datenbank eine Wahrheit, und auf
    # dem Papier steht, was der Beleg bewirkt.
    vorzeichen = -1 if rechnung["typ"] == "STORNO" else 1
    positionen = []
    for zeile in conn.execute(
        "SELECT * FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr",
        (rechnung_id,),
    ):
        position = dict(zeile)
        position["einzelpreis_cent"] *= vorzeichen
        position["betrag_cent"] *= vorzeichen
        positionen.append(position)
    storniert = None
    if rechnung["storno_von_id"]:
        zeile = conn.execute(
            "SELECT nummer, datum FROM rechnung WHERE id = ?", (rechnung["storno_von_id"],)
        ).fetchone()
        storniert = dict(zeile) if zeile else None
    # Korrigieren: die Ersatzrechnung nennt die ersetzte und deren Storno.
    ersetzt = ersetzte_rechnung(conn, rechnung["ersetzt_rechnung_id"])

    nachweis: list[dict] = []
    if rechnung["einzelnachweis"]:
        zeiten = conn.execute(
            "SELECT * FROM zeiteintrag WHERE rechnung_id = ? ORDER BY datum, id", (rechnung_id,)
        ).fetchall()
        nachweis = gruppierung.einzelnachweis([dict(z) for z in zeiten])
        for zeile in nachweis:
            zeile.pop("dauer", None)

    return {
        "dokumenttitel": DOKUMENTTITEL.get(rechnung["typ"], "Rechnung"),
        "r": dict(rechnung),
        "absender": absender,
        "empfaenger": empfaenger,
        # Name im Kopf, Ruecksendeangabe, die EINE Kennung, Kontoinhaber --
        # abgeleitet aus dem Absender oben, also aus dem Schnappschuss.
        "druck": rechnungsdruck.druckangaben(absender, dict(rechnung)),
        "positionen": positionen,
        "storniert": storniert,
        "ersetzt": ersetzt,
        "nachweis": nachweis or None,
        "nachweis_minuten": sum(int(z["minuten"]) for z in nachweis),
        "vorschau": False,
    }


def ersetzte_rechnung(conn: sqlite3.Connection, rechnung_id: int | None) -> dict | None:
    """Nummer, Datum und Storno-Nummer der Rechnung, die eine Ersatzrechnung ersetzt.

    Der Satz auf dem PDF und die Kette auf der Rechnungsseite lesen beide
    hier: "Diese Rechnung ersetzt Rechnung Nr. X vom TT.MM.JJJJ (storniert
    mit Nr. S)."
    """
    if not rechnung_id:
        return None
    zeile = conn.execute(
        "SELECT r.id, r.nummer, r.datum, s.id AS storno_id, s.nummer AS storno_nummer"
        "  FROM rechnung r LEFT JOIN rechnung s ON s.storno_von_id = r.id"
        " WHERE r.id = ?",
        (rechnung_id,),
    ).fetchone()
    return dict(zeile) if zeile else None


# ------------------------------------------------------------- PDF-Vorschau

def vorschau_daten(conn: sqlite3.Connection, rechnung_id: int) -> dict:
    """Die Daten fuer die PDF-Vorschau eines Entwurfs -- ohne jeden Schreibzugriff.

    Dieselben Daten wie fuer das echte PDF (``pdf_daten``), damit die Vorschau
    zeigt, was beim Festschreiben herauskommt: dieselbe Vorlage, derselbe
    Renderer, dieselben gespeicherten Positionen. Gerechnet wird nichts neu --
    die Betraege sind je Position bereits gerundet gespeichert, und die
    Vorschau stimmt dadurch auf den Cent mit der spaeteren Rechnung ueberein.

    Anders als die Entwurfsseite zieht sie den Leistungsort des Kunden NUR IM
    SPEICHER nach, ohne UPDATE und ohne Journal: ein Abruf der Vorschau ist
    ein Lesezugriff. Das Festschreiben zieht ihn ohnehin selbst nach, bevor es
    prueft -- die Vorschau zeigt also schon jetzt den Hinweis, der dann
    gedruckt wird.

    Keine Nummer: die voraussichtliche kann sich noch aendern (ein anderer
    Entwurf wird zuerst festgeschrieben), und auf einem weitergeleiteten
    Entwurf waere sie irrefuehrend.
    """
    rechnung = conn.execute(
        "SELECT status, kunde_id, leistungsort FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()
    if rechnung is None:
        raise FestschreibenFehler("Die Rechnung existiert nicht.")
    if rechnung["status"] != "ENTWURF":
        raise FestschreibenFehler(
            "Eine Vorschau gibt es nur für einen Entwurf; diese Rechnung hat ihr PDF."
        )
    daten = pdf_daten(conn, rechnung_id)
    kunde = conn.execute("SELECT * FROM kunde WHERE id = ?", (rechnung["kunde_id"],)).fetchone()
    if kunde is not None and kunde["leistungsort"] != rechnung["leistungsort"]:
        betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
        daten["r"]["leistungsort"] = kunde["leistungsort"]
        daten["r"]["hinweis_19_text"] = steuerhinweis(betrieb, kunde)
        # Die Kennung haengt am Leistungsort (Reverse Charge: keine
        # Kleinunternehmer-IdNr.) -- also mit ihm neu ableiten.
        daten["druck"] = rechnungsdruck.druckangaben(daten["absender"], daten["r"])
    daten["r"]["nummer"] = None
    daten["vorschau"] = True
    return daten


# ------------------------------------------------------------ freie Positionen
#
# Eine Position ohne Zeiteintrag: die Affiliate-Provision zum Monatsende, eine
# Pauschale ohne Zeiterfassung, eine weiterberechnete Auslage. Sie haengt an
# keiner Zeit, deshalb laesst _schreibe_positionen sie bei jeder Neuberechnung
# stehen und reiht sie hinter die Zeitpositionen. Betrag = Menge x
# Einzelpreis, kaufmaennisch auf den Cent -- dieselbe Formel, die die
# Datenbank in rechnungsposition prueft.

#: Einheiten, die eine freie Position tragen darf (UN/ECE Rec 20, wie die
#: uebrigen Positionen): Stueck, pauschal, Stunden, Tage.
FREIE_EINHEITEN = ("C62", "LS", "HUR", "DAY")
#: Dieselben Einheiten fuer das Auswahlfeld, in der Reihenfolge der Haeufigkeit.
FREIE_EINHEITEN_TEXT = (("C62", "Stück"), ("LS", "pauschal"), ("HUR", "Stunden"),
                        ("DAY", "Tage"))
BEZEICHNUNG_HOECHSTENS = 200
UNTERTEXT_HOECHSTENS = 2000
#: Mengen ueber 999.999 sind kein Tippfehler mehr, sondern ein anderer Beleg.
MENGE_HOECHSTENS_TAUSENDSTEL = 999_999_000


def lies_menge(text: str | None) -> int:
    """Eine Menge als Tausendstel: "1" -> 1000, "2,5" -> 2500, "0,125" -> 125.

    Deutsche Schreibweise mit Komma; ein Punkt gilt ebenfalls als
    Dezimaltrenner (keine Tausendertrenner, so grosse Mengen gibt es hier
    nicht). Mehr als drei Dezimalen sind nicht darstellbar.

    >>> lies_menge("1"), lies_menge("2,5"), lies_menge(" 0.125 ")
    (1000, 2500, 125)
    >>> lies_menge("0")
    Traceback (most recent call last):
        ...
    app.logik.festschreiben.FestschreibenFehler: Die Menge muss größer als 0 sein.
    >>> lies_menge("1,2345")
    Traceback (most recent call last):
        ...
    app.logik.festschreiben.FestschreibenFehler: „1,2345“: höchstens drei Dezimalstellen.
    """
    roh = (text or "").strip()
    if not roh:
        raise FestschreibenFehler("Die Menge fehlt.")
    if not re.fullmatch(r"[0-9]{1,6}(?:[.,][0-9]{1,3})?", roh):
        if re.fullmatch(r"[0-9]{1,6}[.,][0-9]{4,}", roh):
            raise FestschreibenFehler(f"„{roh}“: höchstens drei Dezimalstellen.")
        raise FestschreibenFehler(f"„{roh}“ ist keine Menge. Beispiel: 1 oder 2,5")
    ganz, _, dezimal = roh.replace(".", ",").partition(",")
    tausendstel = int(ganz) * 1000 + int((dezimal + "000")[:3])
    if tausendstel <= 0:
        raise FestschreibenFehler("Die Menge muss größer als 0 sein.")
    if tausendstel > MENGE_HOECHSTENS_TAUSENDSTEL:
        raise FestschreibenFehler(f"„{roh}“ ist als Menge unplausibel groß.")
    return tausendstel


def _entwurf_fuer_position(conn: sqlite3.Connection, rechnung_id: int) -> sqlite3.Row:
    rechnung = conn.execute(
        "SELECT id, status, gruppierung FROM rechnung WHERE id = ?", (rechnung_id,)
    ).fetchone()
    if rechnung is None:
        raise FestschreibenFehler("Diese Rechnung gibt es nicht (mehr).")
    if rechnung["status"] != "ENTWURF":
        raise FestschreibenFehler("Positionen lassen sich nur an einem Entwurf ändern.")
    return rechnung


def freie_position_anlegen(
    conn: sqlite3.Connection, rechnung_id: int, *, bezeichnung: str,
    untertext: str | None, menge_tausendstel: int, einheit: str, einzelpreis_cent: int,
) -> int:
    """Haengt eine freie Position an einen Entwurf. Oeffnet die Transaktion selbst.

    Mit Einheit "LS" (pauschal) wird die Zeile wie eine Pauschale gedruckt:
    ohne Menge und Einzelpreis, denn "1,00 pausch." behauptet eine
    Mengenrechnung, die es nicht gibt. Danach rechnet _schreibe_positionen
    Summe und Nummern nach (version + 1), damit das Festschreiben nichts
    annimmt, was der Nutzer nicht gesehen hat.
    """
    bezeichnung = " ".join((bezeichnung or "").split())
    if not bezeichnung:
        raise FestschreibenFehler("Die Leistung braucht eine Bezeichnung.")
    if len(bezeichnung) > BEZEICHNUNG_HOECHSTENS:
        raise FestschreibenFehler(
            f"Die Bezeichnung ist zu lang ({len(bezeichnung)} Zeichen, höchstens "
            f"{BEZEICHNUNG_HOECHSTENS}).")
    untertext = (untertext or "").replace("\r\n", "\n").strip() or None
    if untertext and len(untertext) > UNTERTEXT_HOECHSTENS:
        raise FestschreibenFehler(
            f"Die Beschreibung ist zu lang ({len(untertext)} Zeichen, höchstens "
            f"{UNTERTEXT_HOECHSTENS}).")
    if einheit not in FREIE_EINHEITEN:
        raise FestschreibenFehler("Unbekannte Einheit.")
    if not 0 < menge_tausendstel <= MENGE_HOECHSTENS_TAUSENDSTEL:
        raise FestschreibenFehler("Die Menge muss größer als 0 sein.")
    if einzelpreis_cent <= 0:
        raise FestschreibenFehler("Der Einzelpreis muss größer als 0 sein.")
    betrag_cent = (menge_tausendstel * einzelpreis_cent + 500) // 1000
    if betrag_cent <= 0:
        raise FestschreibenFehler("Menge mal Einzelpreis ergibt keinen Cent.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        rechnung = _entwurf_fuer_position(conn, rechnung_id)
        naechste = conn.execute(
            "SELECT COALESCE(MAX(pos_nr), 0) + 1 FROM rechnungsposition WHERE rechnung_id = ?",
            (rechnung_id,),
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung, untertext,
                   menge_tausendstel, einheit, einzelpreis_cent, nachlass_cent,
                   nachlass_grund, darstellung, betrag_cent, quelle)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, 'FREI')
            """,
            (rechnung_id, int(naechste), bezeichnung, untertext, menge_tausendstel, einheit,
             einzelpreis_cent, "NUR_TEXT" if einheit == "LS" else "BETRAG", betrag_cent),
        )
        position_id = int(cur.lastrowid)
        _schreibe_positionen(conn, rechnung_id, modus=rechnung["gruppierung"])
        db.protokolliere(
            conn, "POSITION_FREI", "rechnung", rechnung_id,
            wert_neu={"position_id": position_id, "bezeichnung": bezeichnung,
                      "menge_tausendstel": menge_tausendstel, "einheit": einheit,
                      "einzelpreis_cent": einzelpreis_cent, "betrag_cent": betrag_cent},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return position_id


def freie_position_loeschen(conn: sqlite3.Connection, rechnung_id: int,
                            position_id: int) -> None:
    """Entfernt eine freie Position aus einem Entwurf; der Inhalt bleibt im Journal."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rechnung = _entwurf_fuer_position(conn, rechnung_id)
        zeile = conn.execute(
            "SELECT * FROM rechnungsposition WHERE id = ? AND rechnung_id = ?"
            " AND quelle = 'FREI'", (position_id, rechnung_id),
        ).fetchone()
        if zeile is None:
            raise FestschreibenFehler("Diese Position gibt es nicht (mehr) oder sie stammt "
                                      "aus Zeiten — die lassen sich nur über die Zeiten ändern.")
        conn.execute("DELETE FROM rechnungsposition WHERE id = ?", (position_id,))
        _schreibe_positionen(conn, rechnung_id, modus=rechnung["gruppierung"])
        db.protokolliere(
            conn, "POSITION_FREI_WEG", "rechnung", rechnung_id,
            wert_alt={k: zeile[k] for k in ("bezeichnung", "untertext", "menge_tausendstel",
                                            "einheit", "einzelpreis_cent", "betrag_cent")},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

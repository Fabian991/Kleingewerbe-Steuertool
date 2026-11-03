"""Der Ablauf einer Rechnung nach dem Festschreiben.

Eine festgeschriebene Rechnung ist ein Beleg und wird nicht geaendert
(§ 146 Abs. 4 AO; GoBD Rz. 58 f., 107 ff.). Was im Alltag trotzdem gebraucht
wird, sind Wege fuer drei Faelle -- jeder davon so, dass der urspruengliche
Inhalt und die Tatsache der Aenderung feststellbar bleiben:

1. **Korrigieren.** Etwas stimmt nicht, die Rechnung ist womoeglich schon
   beim Kunden. In EINER Transaktion: das Storno wie bisher (eigenes
   Dokument "Rechnungskorrektur" mit eigener Nummer) UND ein neuer Entwurf
   mit denselben Positionen, Texten und Zeiten, Rechnungsdatum heute, der
   auf die ersetzte Rechnung verweist. Scheitert ein Teil, gilt keiner.
2. **Zuruecknehmen vor dem Versand.** Gerade festgeschrieben, Fehler
   bemerkt, niemandem geschickt. Ausdrueckliche Entscheidung des Nutzers:
   solange sie nicht versendet ist, darf die Rechnung zurueck -- als
   Entwurf oder ganz. War ihre Nummer die zuletzt vergebene ihres Kreises,
   wird sie wieder frei; sonst bleibt sie als dokumentierte Luecke
   verbraucht, denn eine Nummer, nach der schon eine weitere vergeben wurde,
   noch einmal zu vergeben, stellte eine juengere Rechnung vor eine aeltere.
   Das PDF wird nicht geloescht, sondern unter ``ausgangsrechnungen/
   verworfen/`` aufbewahrt, und der vollstaendige Inhalt steht im Journal.
   Die Datenbank laesst die Ausnahme von ihrer Sperre nur mit einer
   Freigabe zu, die nur innerhalb der Transaktion existiert (Tabelle
   ``loeschfreigabe`` in schema.sql).
3. **Zahlung zuruecknehmen.** Eine Fehlbuchung wird durch eine Gegenbuchung
   zum SELBEN Datum aufgehoben, nie geloescht: die Zahlung hat nie
   stattgefunden, also gehoert die Korrektur in dasselbe Jahr. Beide Zeilen
   bleiben sichtbar und ergeben zusammen null -- in Zahlstatus, offenem
   Posten, EUeR und § 19-Zaehler.

Dazu der Versandvermerk (aendern und zuruecknehmen, mit Grund) und die
Nummernluecken-Liste, die jede verbrauchte Nummer ohne gueltige Rechnung mit
ihrem Grund nennt.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from app import db, pdf, pfade
from app.logik import festschreiben as fs
from app.logik import kunden, nummernkreis
from app.logik.festschreiben import FestschreibenFehler
from app.logik.rundung import euro

#: Der Satz, den der Nutzer vor dem Zuruecknehmen ausdruecklich bestaetigt.
#: Er steht woertlich im Journal: "nicht versendet" ist ein Vermerk von Hand
#: (der Mail-Link setzt ihn nicht), also muss die Bestaetigung dokumentiert
#: sein, nicht nur das Fehlen des Vermerks.
BESTAETIGUNG = "Diese Rechnung hat niemand erhalten (nicht verschickt, nicht übergeben)."

#: Vorschlaege fuer den Grund des Zuruecknehmens.
GRUND_VORSCHLAEGE = (
    "Testrechnung",
    "Fehler bemerkt, vor dem Versand",
    "Falscher Kunde",
    "Falscher Stundensatz",
    "Doppelt festgeschrieben",
)

#: Die beiden Varianten des Zuruecknehmens.
ZURUECK_ZUM_ENTWURF = "ENTWURF"
GANZ_LOESCHEN = "LOESCHEN"
VARIANTEN = (ZURUECK_ZUM_ENTWURF, GANZ_LOESCHEN)

#: Wo das PDF einer zurueckgenommenen Rechnung aufbewahrt wird, relativ zum
#: Datenordner. Unter ``ausgangsrechnungen/`` und damit in jeder Sicherung.
VERWORFEN_ORDNER = Path("ausgangsrechnungen") / "verworfen"

#: Journal-Aktionen dieses Moduls.
AKTION_KORREKTUR = "KORREKTUR"
AKTION_ZURUECKGENOMMEN = "RECHNUNG_ZURUECKGENOMMEN"
AKTION_ZAHLUNG_ZURUECK = "ZAHLUNG_ZURUECKGENOMMEN"
AKTION_VERSAND_ZURUECK = "VERSAND_ZURUECKGENOMMEN"


def _datum_de(iso: str | None) -> str:
    """>>> _datum_de("2026-03-05")
    '05.03.2026'
    """
    if not iso:
        return ""
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


# ------------------------------------------------------------- Korrigieren

@dataclass(frozen=True, slots=True)
class Korrektur:
    """Das Ergebnis von :func:`korrigieren`."""

    storno: fs.Ergebnis
    entwurf_id: int
    original_nummer: str
    #: Was auf das Original bereits eingegangen ist. Die Zahlung bleibt dort
    #: (eine Umbuchung gibt es nicht); die Oberflaeche muss das sagen.
    bezahlt_cent: int


def korrigieren(
    conn: sqlite3.Connection,
    rechnung_id: int,
    grund: str,
    *,
    belegordner: Path | None = None,
) -> Korrektur:
    """Storno plus Ersatzentwurf in einem Schritt.

    Der Entwurf uebernimmt die Positionen 1:1 -- Mengen, Saetze, Nachlaesse,
    Texte --, nicht neu gebildet aus den Zeiten: dann haengte das Ergebnis
    an einer inzwischen geaenderten Rundungsregel oder Gruppierung, und
    "dieselbe Rechnung noch einmal" waere es nicht. Wer etwas aendern will,
    aendert es am Entwurf; jede Neuberechnung dort ist eine bewusste,
    protokollierte Handlung.

    Die Zeiten wandern an den Entwurf, ihr eingefrorener Satz bleibt der der
    Originalrechnung. Kopftexte (Betreff, Einleitung, Schluss, Einzelnachweis,
    Leistungszeitraum), Gruppierung und Rundungshinweis kommen mit; das
    Rechnungsdatum ist heute -- es ist eine neu ausgestellte Rechnung. Eine
    Rechnung aus der Zeit vor Version 21 bringt keine Regel mit; der Entwurf
    bekommt dann die damalige eingefroren und den zutreffenden Satz (Altregel).

    Reihenfolge wie beim Festschreiben: Storno, Nummer, Entwurf und Journal
    in EINER Transaktion, das PDF der Rechnungskorrektur ERST DANACH.
    """
    grund = (grund or "").strip()
    if not grund:
        raise FestschreibenFehler(
            "Zum Korrigieren gehört eine Begründung — sie steht auf der Rechnungskorrektur "
            "und im Protokoll."
        )
    original = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    if original is None:
        raise FestschreibenFehler("Die Rechnung existiert nicht.")
    if original["typ"] != "RECHNUNG":
        raise FestschreibenFehler("Nur eine Rechnung kann korrigiert werden.")
    if original["status"] == "ENTWURF":
        raise FestschreibenFehler("Ein Entwurf wird einfach geändert — er hat noch keine Nummer.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Den Stand INNERHALB der Transaktion lesen: ein Doppelklick stellt
        # die zweite Anfrage hinter die erste, und die sieht hier STORNIERT
        # -- kein zweites Storno, kein zweiter Entwurf (dahinter wacht noch
        # der Index rechnung_storno_einmalig).
        aktuell = conn.execute(
            "SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        if aktuell is None or aktuell["status"] != "FESTGESCHRIEBEN":
            raise FestschreibenFehler(
                "Die Rechnung ist inzwischen storniert oder korrigiert worden — bitte die "
                "Seite neu laden."
            )
        zeit_ids = [
            z["id"] for z in conn.execute(
                "SELECT id FROM zeiteintrag WHERE rechnung_id = ? ORDER BY datum, id",
                (rechnung_id,),
            )
        ]
        positionen = conn.execute(
            "SELECT * FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr",
            (rechnung_id,),
        ).fetchall()
        zuordnung: dict[int, list[int]] = {}
        for zeile in conn.execute(
            "SELECT pz.position_id, pz.zeiteintrag_id FROM position_zeiteintrag pz"
            "  JOIN rechnungsposition p ON p.id = pz.position_id"
            " WHERE p.rechnung_id = ? ORDER BY pz.zeiteintrag_id",
            (rechnung_id,),
        ):
            zuordnung.setdefault(int(zeile["position_id"]), []).append(
                int(zeile["zeiteintrag_id"])
            )

        # Die Regel einer Altrechnung VOR dem Storno lesen: das Storno-Dokument
        # selbst behaelt den gedruckten Satz des Originals, nur der Entwurf
        # bekommt den zutreffenden.
        altregel = _regel_der_altrechnung(conn, aktuell)
        storno_id, nummer = fs.storno_schreiben(conn, aktuell, grund, zeiten_freigeben=True)
        entwurf_id = _ersatzentwurf(conn, aktuell, positionen, zuordnung, zeit_ids, altregel)
        bezahlt = int(conn.execute(
            "SELECT COALESCE(SUM(betrag_cent), 0) FROM zahlung WHERE rechnung_id = ?",
            (rechnung_id,),
        ).fetchone()[0])
        db.protokolliere(
            conn, AKTION_KORREKTUR, "rechnung", rechnung_id,
            wert_neu={"storno": nummer.text, "storno_id": storno_id,
                      "entwurf_id": entwurf_id, "bezahlt_cent": bezahlt},
            begruendung=grund,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    pfad, hash_, fehler = fs._pdf_schreiben(conn, storno_id, belegordner)
    return Korrektur(
        storno=fs.Ergebnis(storno_id, nummer.text, pfad, hash_, fehler),
        entwurf_id=entwurf_id,
        original_nummer=original["nummer"],
        bezahlt_cent=bezahlt,
    )


@dataclass(frozen=True, slots=True)
class Altregel:
    """Die Rundungsregel einer Rechnung aus der Zeit vor Version 21.

    Rechnungen aus Version 20 tragen keine eingefrorene Regel
    (``rundung_modus`` NULL) und den damaligen Satz „Abrechnung in
    angefangenen 15 Minuten.“ -- eine Zeittaktklausel je Taetigkeit, die so
    nie angewandt wurde (Version 21 hat ihn ersetzt). Wird eine solche
    Rechnung wieder zum Entwurf (Korrigieren, Zuruecknehmen), darf der
    Entwurf weder diesen Satz auf eine HEUTE ausgestellte Rechnung tragen
    noch bei der naechsten Neuberechnung still mit der heutigen Regel des
    Kunden rechnen.
    """

    modus: str
    intervall_min: int
    hinweis: str | None


def _regel_der_altrechnung(conn: sqlite3.Connection, rechnung: sqlite3.Row) -> Altregel | None:
    """Regel und Rundungssatz fuer den Entwurf aus einer Rechnung ohne eingefrorene Regel.

    None, wenn die Rechnung ihre Regel schon traegt (ab Version 21): dann
    kommen Regel und Satz unveraendert mit. Sonst die Regel, mit der die
    Positionen gerechnet wurden -- in Version 20 gab es nur die des Betriebs,
    genau wie fuer die alten Entwuerfe in Migration 21
    (db._rundungshinweis_der_entwuerfe) -- und der Satz, den das Neuberechnen
    mit dieser Regel und der Gruppierung der Rechnung schriebe. Hat jemand die
    Betriebsregel seit dem Festschreiben geaendert, laesst sich die damalige
    nicht mehr feststellen; die heutige des Betriebs ist dann die beste
    Naeherung, und die Positionen selbst kommen ohnehin 1:1 mit.
    """
    if rechnung["rundung_modus"] is not None:
        return None
    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    regel = kunden.betriebsregel(conn)
    return Altregel(
        regel.modus, regel.intervall_min,
        fs._rundungshinweis(betrieb, regel, rechnung["gruppierung"]),
    )


def _ersatzentwurf(
    conn: sqlite3.Connection,
    original: sqlite3.Row,
    positionen: list[sqlite3.Row],
    zuordnung: dict[int, list[int]],
    zeit_ids: list[int],
    altregel: Altregel | None = None,
) -> int:
    """Legt den Ersatzentwurf an -- innerhalb der Transaktion des Aufrufers."""
    heute = db.heute()
    if altregel is None:
        modus, intervall, hinweis = (original["rundung_modus"],
                                     original["rundung_intervall_min"],
                                     original["rundung_hinweis"])
    else:
        modus, intervall, hinweis = altregel.modus, altregel.intervall_min, altregel.hinweis
    # Das Zahlungsziel des Originals: es ist dieselbe Forderung, nur neu
    # ausgestellt. Faellig ab heute, weil das Rechnungsdatum heute ist.
    ziel = original["zahlungsziel_tage"]
    if ziel is None:
        ziel = kunden.zahlungsziel_tage(conn, original["kunde_id"])
    faellig = (date.fromisoformat(heute) + timedelta(days=int(ziel))).isoformat()
    cur = conn.execute(
        """
        INSERT INTO rechnung (typ, kunde_id, datum, leistung_von, leistung_bis,
                              zahlungsziel_tage, faellig_am, netto_cent, brutto_cent,
                              leistungsort, steuerregime, hinweis_19_text, betreff,
                              einleitung, schluss, rundung_hinweis, gruppierung,
                              einzelnachweis, leistungszeitraum_zeigen,
                              rundung_modus, rundung_intervall_min,
                              status, ersetzt_rechnung_id, erstellt_am)
        VALUES ('RECHNUNG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'ENTWURF', ?, ?)
        """,
        (
            original["kunde_id"], heute, original["leistung_von"], original["leistung_bis"],
            ziel, faellig, original["netto_cent"], original["brutto_cent"],
            # Leistungsort und Hinweis wie im Original. Hat sich der
            # Leistungsort am Kunden inzwischen geaendert, zieht die
            # Entwurfsseite ihn nach (festschreiben.leistungsort_nachziehen)
            # -- genau der Fall, fuer den man korrigiert.
            original["leistungsort"], original["steuerregime"], original["hinweis_19_text"],
            original["betreff"], original["einleitung"], original["schluss"],
            # Der Rundungshinweis gehoert zu den Positionen, und die kommen
            # unveraendert mit -- also auch er. Ausnahme: eine Rechnung aus
            # der Zeit vor Version 21 (Altregel), deren Satz ersetzt ist.
            hinweis, original["gruppierung"],
            original["einzelnachweis"], original["leistungszeitraum_zeigen"],
            # Die Rundungsregel, mit der die Positionen gerechnet wurden, kommt
            # eingefroren mit (Version 21): sonst rechnete der Entwurf bei der
            # naechsten Neuberechnung (Gruppierung, Text einer Zeit, Kundensatz)
            # still mit der heutigen Regel des Kunden -- und die Seite nennte
            # die als "Regel vom Anlegen". Eine Rechnung aus der Zeit vor
            # Version 21 hat keine; fuer sie friert der Entwurf hier die
            # damalige ein (_regel_der_altrechnung).
            modus, intervall,
            original["id"], db.jetzt_utc(),
        ),
    )
    entwurf_id = int(cur.lastrowid)
    if zeit_ids:
        conn.execute(
            f"UPDATE zeiteintrag SET rechnung_id = ?, geaendert_am = ?, version = version + 1"
            f" WHERE rechnung_id IS NULL AND id IN ({', '.join('?' * len(zeit_ids))})",
            (entwurf_id, db.jetzt_utc(), *zeit_ids),
        )
    for position in positionen:
        neu = conn.execute(
            """
            INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung, untertext,
                   menge_tausendstel, einheit, einzelpreis_cent, nachlass_cent,
                   nachlass_grund, darstellung, betrag_cent, steuerkategorie,
                   steuersatz_promille, quelle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entwurf_id, position["pos_nr"], position["bezeichnung"], position["untertext"],
                position["menge_tausendstel"], position["einheit"],
                position["einzelpreis_cent"], position["nachlass_cent"],
                position["nachlass_grund"], position["darstellung"], position["betrag_cent"],
                position["steuerkategorie"], position["steuersatz_promille"],
                position["quelle"],
            ),
        ).lastrowid
        # Die Zuordnung Position <-> Zeit kommt mit. pz_keine_doppelabrechnung
        # laesst das zu: das Original ist in diesem Moment schon STORNIERT.
        for zeit_id in zuordnung.get(int(position["id"]), []):
            conn.execute(
                "INSERT INTO position_zeiteintrag (position_id, zeiteintrag_id) VALUES (?, ?)",
                (neu, zeit_id),
            )
    eintrag: dict[str, object] = {
        "kunde_id": original["kunde_id"], "zeiten": len(zeit_ids),
        "modus": original["gruppierung"], "ersetzt": original["nummer"],
    }
    if altregel is not None:
        eintrag["rundung_eingefroren"] = {
            "modus": modus, "intervall_min": intervall, "hinweis": hinweis,
        }
    db.protokolliere(conn, "ENTWURF", "rechnung", entwurf_id, wert_neu=eintrag)
    return entwurf_id


# --------------------------------------------- Zuruecknehmen vor dem Versand

@dataclass(frozen=True, slots=True)
class Ruecknahmeprobe:
    """Ob und wie eine festgeschriebene Rechnung zurueckgenommen werden kann.

    Die Seite zeigt das VOR dem Bestaetigen -- vor allem, ob die Nummer
    wieder frei wird oder als Luecke stehen bleibt.
    """

    hindernisse: tuple[str, ...]
    nummer: str | None
    nummer_wird_frei: bool
    nachfolger: str | None

    @property
    def moeglich(self) -> bool:
        return not self.hindernisse


def ruecknahme_pruefen(conn: sqlite3.Connection, rechnung_id: int) -> Ruecknahmeprobe:
    """Die Vorbedingungen des Zuruecknehmens, mit Begruendung je Hindernis."""
    rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    if rechnung is None:
        return Ruecknahmeprobe(("Die Rechnung existiert nicht.",), None, False, None)
    hindernisse: list[str] = []
    if rechnung["typ"] != "RECHNUNG":
        hindernisse.append("Nur eine Rechnung lässt sich zurücknehmen.")
    if rechnung["status"] == "ENTWURF":
        hindernisse.append("Ein Entwurf hat noch keine Nummer — er wird einfach gelöscht.")
    elif rechnung["status"] != "FESTGESCHRIEBEN":
        hindernisse.append("Die Rechnung ist storniert.")
    if rechnung["versendet_am"]:
        hindernisse.append(
            f"Die Rechnung ist als versendet vermerkt (am {_datum_de(rechnung['versendet_am'])}). "
            "Wer sie erhalten hat, hält einen Beleg in der Hand — sie lässt sich nur noch "
            "korrigieren. War der Vermerk ein Irrtum, zuerst den Vermerk zurücknehmen."
        )
    if conn.execute(
        "SELECT 1 FROM zahlung WHERE rechnung_id = ? LIMIT 1", (rechnung_id,)
    ).fetchone():
        hindernisse.append(
            "Auf die Rechnung ist eine Zahlung gebucht (eine zurückgenommene Zahlung bleibt "
            "als Buchung stehen). Sie lässt sich nur noch korrigieren."
        )
    if conn.execute(
        "SELECT 1 FROM rechnung WHERE storno_von_id = ? LIMIT 1", (rechnung_id,)
    ).fetchone():
        hindernisse.append("Zu der Rechnung gibt es eine Rechnungskorrektur.")

    frei, folgende = False, None
    if rechnung["nummer_seq"] is not None and rechnung["nummer_jahr"] is not None:
        jahr, seq = int(rechnung["nummer_jahr"]), int(rechnung["nummer_seq"])
        frei = nummernkreis.ist_zuletzt_vergeben(conn, jahr, seq)
        if not frei:
            folgende = nummernkreis.nachfolger(conn, jahr, seq)
    return Ruecknahmeprobe(tuple(hindernisse), rechnung["nummer"], frei, folgende)


@dataclass(frozen=True, slots=True)
class Ruecknahme:
    """Das Ergebnis von :func:`zuruecknehmen`."""

    rechnung_id: int
    variante: str
    nummer: str
    nummer_frei: bool
    nachfolger: str | None
    #: Wo das PDF jetzt liegt (relativ zum Datenordner), oder None.
    pdf_verworfen: str | None
    zeiten: int


def zuruecknehmen(
    conn: sqlite3.Connection,
    rechnung_id: int,
    grund: str,
    *,
    bestaetigt: bool,
    variante: str = ZURUECK_ZUM_ENTWURF,
    datenordner: Path | None = None,
) -> Ruecknahme:
    """Nimmt eine festgeschriebene, nie versendete Rechnung zurueck.

    ``variante`` ist ZURUECK_ZUM_ENTWURF (die Rechnung wird wieder Entwurf
    mit denselben Positionen, Texten und Zeiten, ohne Nummer) oder
    GANZ_LOESCHEN (sie verschwindet, die Zeiten werden frei).

    Ablauf in EINER Transaktion: Bedingungen pruefen, vollen Inhalt lesen,
    Nummer freigeben (nur die zuletzt vergebene), PDF beiseitelegen,
    Freigabe setzen, Status zurueck auf ENTWURF, Freigabe entfernen,
    Journal, bei GANZ_LOESCHEN den Entwurf verwerfen. Zurueck zum Entwurf
    friert eine Rechnung aus der Zeit vor Version 21 ihre Regel ein und
    bekommt den zutreffenden Rundungssatz (Altregel). Das PDF wird hier --
    anders als beim Festschreiben -- VOR dem Commit verschoben: bliebe es
    nach dem Commit liegen und wuerde die Nummer neu vergeben, schriebe die
    naechste Rechnung ihr PDF genau dorthin. Scheitert danach etwas, kommt
    die Datei an ihren Platz zurueck.
    """
    grund = (grund or "").strip()
    if not grund:
        raise FestschreibenFehler(
            "Ein Grund ist Pflicht — er steht im Protokoll und, falls die Nummer nicht "
            "wieder frei wird, in der Liste der Nummernlücken."
        )
    if not bestaetigt:
        raise FestschreibenFehler(
            "Bitte bestätigen, dass diese Rechnung niemand erhalten hat. Sonst lässt sie sich "
            "nur korrigieren."
        )
    if variante not in VARIANTEN:
        raise FestschreibenFehler("Unbekannte Variante: zurück zum Entwurf oder ganz löschen.")
    ordner = Path(datenordner) if datenordner is not None else db.standard_datenordner()

    verschoben: tuple[Path, Path] | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        probe = ruecknahme_pruefen(conn, rechnung_id)
        if not probe.moeglich:
            raise FestschreibenFehler(probe.hindernisse[0])
        rechnung = conn.execute(
            "SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        inhalt = fs.rechnungsinhalt(conn, rechnung_id)
        jahr, seq = int(rechnung["nummer_jahr"]), int(rechnung["nummer_seq"])
        frei = nummernkreis.gib_frei(conn, jahr, seq)
        folgende = None if frei else nummernkreis.nachfolger(conn, jahr, seq)

        # Zurueck zum Entwurf: eine Rechnung aus der Zeit vor Version 21
        # bekommt ihre Regel eingefroren und den zutreffenden Rundungssatz
        # (siehe Altregel) -- sonst traege sie beim erneuten Festschreiben
        # den ersetzten Satz, und jede Neuberechnung rechnete mit der
        # heutigen Regel des Kunden.
        altregel = (_regel_der_altrechnung(conn, rechnung)
                    if variante == ZURUECK_ZUM_ENTWURF else None)

        verschoben = _pdf_beiseite(ordner, rechnung)
        pdf_neu = pfade.relativ(ordner, verschoben[1]) if verschoben else None
        pdf_hash_datei = pdf.pruefsumme(verschoben[1]) if verschoben else None

        # Die Freigabe gilt nur bis zum Ende dieser Transaktion: sie wird
        # unten in derselben Transaktion wieder entfernt. Der Trigger
        # loeschfreigabe_nur_unversendet prueft die Bedingungen ein zweites
        # Mal, in der Datenbank.
        conn.execute("INSERT INTO loeschfreigabe (rechnung_id) VALUES (?)", (rechnung_id,))
        geaendert = conn.execute(
            """
            UPDATE rechnung
               SET status = 'ENTWURF', nummer = NULL, nummer_seq = NULL, nummer_jahr = NULL,
                   kunde_snapshot_json = NULL, absender_snapshot_json = NULL,
                   festgeschrieben_am = NULL, pdf_pfad = NULL, pdf_sha256 = NULL,
                   version = version + 1
             WHERE id = ? AND status = 'FESTGESCHRIEBEN' AND versendet_am IS NULL
            """,
            (rechnung_id,),
        ).rowcount
        conn.execute("DELETE FROM loeschfreigabe WHERE rechnung_id = ?", (rechnung_id,))
        if geaendert != 1:
            raise FestschreibenFehler(
                "Die Rechnung wurde zwischenzeitlich geändert — bitte die Seite neu laden."
            )
        if altregel is not None:
            # Jetzt ein Entwurf -- die Sperre gilt nicht mehr.
            conn.execute(
                "UPDATE rechnung SET rundung_modus = ?, rundung_intervall_min = ?,"
                " rundung_hinweis = ? WHERE id = ? AND status = 'ENTWURF'",
                (altregel.modus, altregel.intervall_min, altregel.hinweis, rechnung_id),
            )
        neuer_stand: dict[str, object] = {
            "variante": "GELOESCHT" if variante == GANZ_LOESCHEN else "ENTWURF",
            "nummer": rechnung["nummer"], "nummer_seq": seq, "nummer_jahr": jahr,
            "nummer_frei": frei, "nachfolger": folgende,
            "pdf_verworfen": pdf_neu, "pdf_sha256_datei": pdf_hash_datei,
            "bestaetigt": BESTAETIGUNG,
        }
        if altregel is not None:
            neuer_stand["rundung_eingefroren"] = {
                "modus": altregel.modus, "intervall_min": altregel.intervall_min,
                "hinweis": altregel.hinweis,
            }
        db.protokolliere(
            conn, AKTION_ZURUECKGENOMMEN, "rechnung", rechnung_id, feld="nummer",
            wert_alt=inhalt,
            wert_neu=neuer_stand,
            begruendung=grund,
            hash_=rechnung["pdf_sha256"],
        )
        zeiten = len(inhalt["zeiten"]) if inhalt else 0
        if variante == GANZ_LOESCHEN:
            fs.entwurf_verwerfen(
                conn, rechnung_id,
                begruendung=f"Zurückgenommene Rechnung {rechnung['nummer']}: {grund}",
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        if verschoben is not None:
            # Die Datei an ihren Platz zurueck: die Rechnung gilt weiter.
            try:
                shutil.move(str(verschoben[1]), str(verschoben[0]))
            except OSError:
                pass
        raise

    if verschoben is not None:
        # Schreibgeschuetzt: das aufbewahrte Exemplar soll nicht versehentlich
        # bearbeitet werden. Scheitert das (fremdes Dateisystem), bleibt es
        # trotzdem aufbewahrt -- der Hash im Journal ist der Nachweis.
        try:
            os.chmod(verschoben[1], stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass
    return Ruecknahme(
        rechnung_id=rechnung_id, variante=variante, nummer=rechnung["nummer"],
        nummer_frei=frei, nachfolger=folgende, pdf_verworfen=pdf_neu, zeiten=zeiten,
    )


def _pdf_beiseite(datenordner: Path, rechnung: sqlite3.Row) -> tuple[Path, Path] | None:
    """Verschiebt das PDF nach ``ausgangsrechnungen/verworfen/``.

    Der Name traegt den Vermerk VERWORFEN und einen Zeitstempel (UTC), damit
    die Datei ausserhalb der Anwendung als das erkennbar ist, was sie ist,
    und eine spaeter neu vergebene Nummer sie nicht ueberschreibt. Der Inhalt
    bleibt Byte fuer Byte, wie er war: der Hash im Journal muss passen.

    Gibt (alter Ort, neuer Ort) zurueck, oder None, wenn es keine Datei gibt.
    """
    if not rechnung["pdf_pfad"]:
        return None
    quelle = pfade.absolut(datenordner, rechnung["pdf_pfad"])
    if not quelle.is_file():
        return None
    ziel_ordner = datenordner / VERWORFEN_ORDNER
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    stempel = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    ziel = ziel_ordner / f"{quelle.stem}_VERWORFEN_{stempel}{quelle.suffix}"
    nr = 2
    while ziel.exists():
        ziel = ziel_ordner / f"{quelle.stem}_VERWORFEN_{stempel}-{nr}{quelle.suffix}"
        nr += 1
    shutil.move(str(quelle), str(ziel))
    return quelle, ziel


# ----------------------------------------------------------- Versandvermerk

def versand_zuruecknehmen(conn: sqlite3.Connection, rechnung_id: int, grund: str) -> str:
    """Nimmt den Versandvermerk zurueck. Gibt das bisherige Datum zurueck.

    Fuer den Vermerk, der versehentlich gesetzt wurde: danach gilt die
    Rechnung wieder als nicht versendet, und -- ohne Zahlung -- greift
    wieder das Zuruecknehmen vor dem Versand. Grund Pflicht, altes Datum im
    Journal.
    """
    grund = (grund or "").strip()
    if not grund:
        raise FestschreibenFehler(
            "Ein Grund ist Pflicht — der Vermerk steht im Protokoll, seine Rücknahme auch."
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        stand = conn.execute(
            "SELECT status, versendet_am FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        if stand is None or stand["status"] != "FESTGESCHRIEBEN":
            raise FestschreibenFehler(
                "Nur bei einer festgeschriebenen Rechnung lässt sich der Versandvermerk "
                "zurücknehmen."
            )
        bisher = stand["versendet_am"]
        if bisher is None:
            raise FestschreibenFehler("Die Rechnung ist nicht als versendet vermerkt.")
        conn.execute(
            "UPDATE rechnung SET versendet_am = NULL"
            " WHERE id = ? AND status = 'FESTGESCHRIEBEN' AND versendet_am = ?",
            (rechnung_id, bisher),
        )
        db.protokolliere(conn, AKTION_VERSAND_ZURUECK, "rechnung", rechnung_id,
                         feld="versendet_am", wert_alt=bisher, wert_neu=None,
                         begruendung=grund)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return bisher


# ------------------------------------------------------- Zahlung zurueck

@dataclass(frozen=True, slots=True)
class Zahlungsruecknahme:
    """Das Ergebnis von :func:`zahlung_zuruecknehmen`."""

    zahlung_id: int
    gegenbuchung_id: int
    datum: str
    betrag_cent: int
    #: Gesetzt, wenn die Zahlung in einem Vorjahr lag: deren EUeR ist
    #: womoeglich schon abgegeben.
    warnung: str | None


def zahlung_zuruecknehmen(
    conn: sqlite3.Connection, rechnung_id: int, zahlung_id: int, grund: str
) -> Zahlungsruecknahme:
    """Hebt eine fehlerhaft erfasste Rechnungszahlung durch eine Gegenbuchung auf.

    Gleicher Betrag mit umgekehrtem Vorzeichen, GLEICHES Datum, gleiche Art:
    die Zahlung hat nie stattgefunden, also gleicht sich der Betrag im
    selben Jahr aus -- in EUeR, § 19-Zaehler, Zahlstatus und offenem Posten.
    Geloescht wird nichts; die Datenbank liesse es auch nicht zu.

    Abgewiesen wird, was danach "mehr zurueckgezahlt als eingegangen"
    hiesse: dann ist zuerst die Rueckzahlung oder Rueckerstattung
    zurueckzunehmen, die auf dieser Zahlung aufbaut.
    """
    grund = (grund or "").strip()
    if not grund:
        raise FestschreibenFehler(
            "Ein Grund ist Pflicht — er steht an der Gegenbuchung und im Protokoll."
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        zahlung = conn.execute(
            "SELECT * FROM zahlung WHERE id = ? AND rechnung_id = ?", (zahlung_id, rechnung_id)
        ).fetchone()
        if zahlung is None:
            raise FestschreibenFehler("Diese Zahlung gibt es zu dieser Rechnung nicht.")
        if zahlung["storno_von_id"] is not None:
            raise FestschreibenFehler(
                "Das ist selbst eine Rücknahme. Ist sie falsch, die Zahlung einfach neu "
                "erfassen."
            )
        if conn.execute(
            "SELECT 1 FROM zahlung WHERE storno_von_id = ?", (zahlung_id,)
        ).fetchone():
            raise FestschreibenFehler("Diese Zahlung ist bereits zurückgenommen.")
        bezahlt = int(conn.execute(
            "SELECT COALESCE(SUM(betrag_cent), 0) FROM zahlung WHERE rechnung_id = ?",
            (rechnung_id,),
        ).fetchone()[0])
        if bezahlt - int(zahlung["betrag_cent"]) < 0:
            raise FestschreibenFehler(
                "Danach stünde mehr zurückgezahlt als eingegangen. Zuerst die Rückzahlung "
                "bzw. Rückerstattung zurücknehmen, die auf dieser Zahlung aufbaut."
            )
        notiz = f"Rücknahme der Zahlung vom {_datum_de(zahlung['datum'])}: {grund}"
        gegen_id = int(conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art, notiz, storno_von_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (rechnung_id, zahlung["datum"], -int(zahlung["betrag_cent"]), zahlung["art"],
             notiz, zahlung_id),
        ).lastrowid)
        db.protokolliere(
            conn, AKTION_ZAHLUNG_ZURUECK, "rechnung", rechnung_id, feld="zahlung",
            wert_alt={"zahlung_id": zahlung_id, "datum": zahlung["datum"],
                      "cent": zahlung["betrag_cent"], "art": zahlung["art"],
                      "notiz": zahlung["notiz"]},
            wert_neu={"gegenbuchung_id": gegen_id, "datum": zahlung["datum"],
                      "cent": -int(zahlung["betrag_cent"])},
            begruendung=grund,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    jahr = int(zahlung["datum"][:4])
    warnung = None
    if jahr < int(db.heute()[:4]):
        warnung = (
            f"Die Zahlung lag im Jahr {jahr}: die EÜR für {jahr} ist evtl. schon abgegeben "
            "und müsste berichtigt werden — ebenso der § 19-Umsatz dieses Jahres."
        )
    return Zahlungsruecknahme(zahlung_id, gegen_id, zahlung["datum"],
                              int(zahlung["betrag_cent"]), warnung)


# ------------------------------------------------- Satz im Entwurf

def kundensatz_uebernehmen(conn: sqlite3.Connection, rechnung_id: int) -> tuple[list[int], int]:
    """Rechnet die Stundenpositionen eines Entwurfs mit dem heutigen Kundensatz neu.

    Fuer den Entwurf, dessen Zeiten einen eingefrorenen Satz tragen -- nach
    Korrigieren, Storno oder Zuruecknehmen ist das der Satz der alten
    Rechnung, auch wenn er gerade der Fehler war. Wie "wieder berechnen" in
    der Zeiterfassung: der Schnappschuss der STUNDEN-Zeiten wird geleert,
    und die Positionen entstehen aus den Zeiten neu (Gruppierung des
    Entwurfs, Rundungsregel des Entwurfs -- die eingefrorene, siehe
    festschreiben.regel_des_entwurfs). Kulanzzeiten behalten ihren Satz -- er bewertet den
    Verzicht. Gibt (alte Saetze, neuer Satz) zurueck.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rechnung = conn.execute(
            "SELECT status, kunde_id FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
        if rechnung is None or rechnung["status"] != "ENTWURF":
            raise FestschreibenFehler("Nur in einem Entwurf lässt sich der Satz ändern.")
        alt = sorted({
            int(z["satz_cent_snapshot"]) for z in conn.execute(
                "SELECT satz_cent_snapshot FROM zeiteintrag WHERE rechnung_id = ?"
                " AND abrechnungsmodus = 'STUNDEN' AND satz_cent_snapshot IS NOT NULL",
                (rechnung_id,),
            )
        })
        satz = kunden.satz_cent(conn, rechnung["kunde_id"])
        if not alt:
            raise FestschreibenFehler(
                "Die Zeiten dieses Entwurfs rechnen schon mit dem Kundensatz."
            )
        if satz <= 0:
            raise FestschreibenFehler(fs.KEIN_STUNDENSATZ)
        conn.execute(
            "UPDATE zeiteintrag SET satz_cent_snapshot = NULL, geaendert_am = ?,"
            " version = version + 1"
            " WHERE rechnung_id = ? AND abrechnungsmodus = 'STUNDEN'",
            (db.jetzt_utc(), rechnung_id),
        )
        fs.positionen_neu(conn, rechnung_id)
        db.protokolliere(
            conn, "AENDERN", "rechnung", rechnung_id, feld="stundensatz",
            wert_alt=alt, wert_neu=satz,
            begruendung="Kundensatz übernommen, Positionen neu berechnet",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return alt, satz


# --------------------------------------------------- Daten fuer die Seite

def seitendaten(conn: sqlite3.Connection, rechnung: sqlite3.Row) -> dict:
    """Was die Rechnungsseite fuer den Ablauf braucht -- in einem Aufruf.

    Die Kette (ersetzt / ersetzt durch), die Probe fuer das Zuruecknehmen,
    welche Zahlung zurueckgenommen ist, und der eingefrorene Satz im
    Entwurf, wenn er vom Kundensatz abweicht.
    """
    rechnung_id = int(rechnung["id"])
    ersetzt = fs.ersetzte_rechnung(conn, rechnung["ersetzt_rechnung_id"])
    ersetzt_durch = [
        dict(z) for z in conn.execute(
            "SELECT id, nummer, status FROM rechnung WHERE ersetzt_rechnung_id = ? ORDER BY id",
            (rechnung["storno_von_id"] or rechnung_id,),
        )
    ]
    zurueckgenommen = {
        int(z["storno_von_id"]): dict(z) for z in conn.execute(
            "SELECT id, storno_von_id, notiz FROM zahlung"
            " WHERE rechnung_id = ? AND storno_von_id IS NOT NULL",
            (rechnung_id,),
        )
    }
    probe = None
    if rechnung["status"] == "FESTGESCHRIEBEN" and rechnung["typ"] == "RECHNUNG":
        probe = ruecknahme_pruefen(conn, rechnung_id)

    satz = None
    if rechnung["status"] == "ENTWURF":
        eingefroren = sorted({
            int(z["satz_cent_snapshot"]) for z in conn.execute(
                "SELECT satz_cent_snapshot FROM zeiteintrag WHERE rechnung_id = ?"
                " AND abrechnungsmodus = 'STUNDEN' AND satz_cent_snapshot IS NOT NULL",
                (rechnung_id,),
            )
        })
        kundensatz = kunden.satz_cent(conn, rechnung["kunde_id"])
        if eingefroren and eingefroren != [kundensatz]:
            satz = {"eingefroren": eingefroren, "kunde": kundensatz,
                    "text": ", ".join(f"{euro(c)} €" for c in eingefroren)}
    return {
        "ersetzt": ersetzt,
        "ersetzt_durch": ersetzt_durch,
        "zahlung_zurueckgenommen": zurueckgenommen,
        "ruecknahme": probe,
        "satz_eingefroren": satz,
        "bestaetigung": BESTAETIGUNG,
        "grund_vorschlaege": GRUND_VORSCHLAEGE,
        "vorjahr": int(db.heute()[:4]) - 1,
    }


# ------------------------------------------------------- Nummernluecken

@dataclass(frozen=True, slots=True)
class Luecke:
    """Eine verbrauchte Nummer ohne gueltige Rechnung."""

    jahr: int
    seq: int
    #: Die Nummer, wie sie vergeben war; ohne Journalvermerk (Altbestand)
    #: aus dem Format nachgebildet, mit {MM} im Format nur der Zaehler.
    nummer: str
    grund: str | None
    #: Zeitpunkt des Journaleintrags (UTC), None ohne Vermerk.
    vermerkt_am: str | None
    rechnung_id: int | None
    #: "ENTWURF" (wieder Entwurf), "GELOESCHT" oder None (kein Vermerk).
    variante: str | None


def nummernluecken(conn: sqlite3.Connection, jahr: int | None = None) -> list[Luecke]:
    """Alle verbrauchten Nummern ohne gueltige Rechnung, je Jahr, mit Grund.

    Zwei Quellen. Das Journal kennt jede Nummer, die beim Zuruecknehmen
    NICHT wieder frei wurde, mit Grund und Zeitpunkt. Die Zaehlung
    (:func:`nummernkreis.luecken`) findet Luecken auch ohne Vermerk -- aus
    der Zeit vor dieser Funktion oder von Hand. Eine Luecke ohne Vermerk
    steht mit "Grund unbekannt" da: erklaerungsbeduerftig ist sie trotzdem
    (FG Koeln, 15 K 1122/16; BFH X B 79/16).
    """
    kreise: dict[int, int] = {}

    def kreis_von(j: int) -> int:
        """Der Schluessel des Kreises, aus dem das Jahr ``j`` zieht (0 = durchlaufend)."""
        if j not in kreise:
            kreise[j] = nummernkreis.KREIS_DURCHLAUFEND if nummernkreis.ist_durchlaufend(
                conn, j) else j
        return kreise[j]

    vermerke: dict[tuple[int, int], dict] = {}
    for zeile in conn.execute(
        "SELECT objekt_id, wert_neu, begruendung, zeitpunkt_utc FROM journal"
        " WHERE aktion = ? ORDER BY id",
        (AKTION_ZURUECKGENOMMEN,),
    ):
        try:
            neu = json.loads(zeile["wert_neu"] or "{}")
            j, s = int(neu["nummer_jahr"]), int(neu["nummer_seq"])
        except (ValueError, KeyError, TypeError):
            continue
        schluessel = (kreis_von(j), s)
        vermerke[schluessel] = {
            "jahr": j, "seq": s, "nummer": neu.get("nummer") or str(s),
            "frei": bool(neu.get("nummer_frei")), "variante": neu.get("variante"),
            "grund": zeile["begruendung"], "am": zeile["zeitpunkt_utc"],
            "rechnung_id": zeile["objekt_id"],
        }

    vergeben = {
        (kreis_von(int(z["nummer_jahr"])), int(z["nummer_seq"]))
        for z in conn.execute(
            "SELECT nummer_jahr, nummer_seq FROM rechnung"
            " WHERE nummer_seq IS NOT NULL AND nummer_jahr IS NOT NULL"
        )
    }
    jahre = sorted(
        {int(z[0]) for z in conn.execute(
            "SELECT DISTINCT nummer_jahr FROM rechnung WHERE nummer_jahr IS NOT NULL")}
        | {v["jahr"] for v in vermerke.values()}
    )

    gefunden: list[Luecke] = []
    gesehen: set[tuple[int, int]] = set()
    for j in jahre:
        kreis = kreis_von(j)
        for s in nummernkreis.luecken(conn, j):
            schluessel = (kreis, s)
            if schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            gefunden.append(_luecke(conn, vermerke.get(schluessel), kreis, s, j))
    # Vermerkte Luecken ausserhalb der gezaehlten Spanne: eine verbrauchte
    # Nummer, hinter der die letzte spaeter zurueckgenommen wurde -- die
    # Zaehlung reicht nur bis zur hoechsten vergebenen.
    for schluessel, vermerk in vermerke.items():
        if schluessel in gesehen or vermerk["frei"] or schluessel in vergeben:
            continue
        gesehen.add(schluessel)
        gefunden.append(_luecke(conn, vermerk, schluessel[0], schluessel[1], vermerk["jahr"]))

    if jahr is not None:
        gefunden = [luecke for luecke in gefunden if luecke.jahr == jahr]
    return sorted(gefunden, key=lambda luecke: (luecke.jahr, luecke.seq))


def _luecke(
    conn: sqlite3.Connection, vermerk: dict | None, kreis: int, seq: int, jahr: int
) -> Luecke:
    if vermerk is not None:
        return Luecke(
            jahr=vermerk["jahr"], seq=seq, nummer=vermerk["nummer"], grund=vermerk["grund"],
            vermerkt_am=vermerk["am"], rechnung_id=vermerk["rechnung_id"],
            variante=vermerk["variante"],
        )
    # Ohne Vermerk: im durchlaufenden Kreis gehoert die Luecke zum Jahr der
    # Nummer davor -- dort ist sie entstanden.
    if kreis == nummernkreis.KREIS_DURCHLAUFEND:
        davor = conn.execute(
            "SELECT nummer_jahr FROM rechnung WHERE nummer_seq < ? AND nummer_jahr IS NOT NULL"
            " ORDER BY nummer_seq DESC LIMIT 1",
            (seq,),
        ).fetchone()
        if davor is not None:
            jahr = int(davor["nummer_jahr"])
    format_zeile = conn.execute(
        "SELECT format FROM nummernkreis WHERE typ = ? AND jahr IN (?, ?)"
        " ORDER BY (jahr = ?) DESC LIMIT 1",
        (nummernkreis.TYP_RECHNUNG, kreis, jahr, kreis),
    ).fetchone()
    vorlage = format_zeile["format"] if format_zeile else nummernkreis.STANDARD_FORMAT
    nummer = (
        nummernkreis.formatiere(vorlage, jahr, seq) if "{MM}" not in vorlage
        else f"Zähler {seq}"
    )
    return Luecke(jahr=jahr, seq=seq, nummer=nummer, grund=None, vermerkt_am=None,
                  rechnung_id=None, variante=None)

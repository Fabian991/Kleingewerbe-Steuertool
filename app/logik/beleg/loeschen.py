"""Beleg loeschen -- als Loeschmarkierung, nicht als DELETE.

Was "geloescht" heisst
----------------------
Fuer den Nutzer ist der Beleg weg: er verschwindet aus der Belegliste (der
Schalter "geloeschte zeigen" holt ihn zurueck ins Bild), aus EUeR,
Uebersicht, Steuerschaetzung, den Excel-Blaettern "EÜR" und "Buchungen",
aus Anlagenverzeichnis und AfA, aus den offenen Hinweisen und aus der
Dubletten- und Aehnlichkeitspruefung. Alle Auswertungen lesen dafuer ueber
die Sicht ``v_beleg_gueltig`` (schema.sql).

Was bleibt: der Datensatz, seine Zahlungen, die Auslesung und die
Originaldatei -- schreibgeschuetzt im Archiv. Dazu das vollstaendige Abbild
im Journal.

Der rechtliche Rahmen
---------------------
§ 146 Abs. 4 AO: eine Buchung oder Aufzeichnung darf nicht so veraendert
werden, dass der urspruengliche Inhalt nicht mehr feststellbar ist. Das gilt
fuer die Aufzeichnungen eines Einnahmenueberschussrechners ebenso. Nach GoBD
Rz. 58 f. und 107 ff. darf, was einmal in die Verarbeitung eingefuehrt ist,
nicht ohne Kenntlichmachung verschwinden; GoBD Rz. 110 nennt den
"Loeschmerker" als zulaessigen Weg. Die Originaldatei ist Buchungsbeleg und
acht Jahre aufzubewahren (§ 147 Abs. 1 Nr. 4, Abs. 3 AO) -- auch die eines
falsch erfassten oder doppelt gebuchten Belegs.

Die Entscheidungen
------------------
1. **Grund ist Pflicht** (Vorschlaege: Testbeleg, doppelt erfasst, falsch
   ausgelesen). Ein Trigger verlangt ihn ebenso.
2. **Ein Anlagegut am Beleg geht mit** -- aber erst nach Rueckfrage, die
   AfA-Jahre und Betraege aufzaehlt (:class:`AnlagegutRueckfrage`); stand AfA
   in einem zurueckliegenden Jahr, warnt sie: die steht womoeglich in einer
   abgegebenen EUeR.
3. **Dieselbe Datei laesst sich danach neu hochladen und buchen.** Die
   Dublettenpruefung zaehlt nur gueltige Belege; die neue Buchung verweist
   bei gleichem Belegdatum auf dieselbe Archivdatei, sonst liegt die Datei
   ein zweites Mal im Archiv. Die Unveraenderbarkeit der Belegdatei bleibt,
   wie sie ist (siehe ``belegdatei_original_einmalig_insert`` in schema.sql).
   :func:`neu_erfassen` legt sie auf Wunsch gleich wieder in den Eingang.
4. **Wiederherstellen** nimmt die Markierung zurueck -- gegen den Fehlklick.
   Nicht, wenn die Datei inzwischen als anderer Beleg gebucht ist, und nicht
   eine zweite Verpflegungspauschale derselben Reise: sonst stuende dieselbe
   Ausgabe zweimal in der EUeR.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app import db, pfade
from app.logik.beleg import pipeline
from app.logik.beleg.eigenbeleg import ohne_steuerzeichen
from app.logik.beleg.ergebnis import BelegFehler
from app.logik.rundung import euro

#: Die Vorschlaege in der Oberflaeche.
GRUENDE = ("Testbeleg", "doppelt erfasst", "falsch ausgelesen")

#: Wie der Trigger beleg_loeschen_braucht_grund: drei Zeichen ("doppelt" geht,
#: "x" nicht).
GRUND_MINDESTLAENGE = 3


@dataclass(frozen=True, slots=True)
class Anlagegut:
    """Ein Anlagegut am Beleg mit seiner AfA-Reihe -- fuer die Rueckfrage."""

    id: int
    bezeichnung: str
    ak_brutto_cent: int
    #: (Jahr, AfA in Cent) je Jahr der Reihe, aufsteigend.
    afa: tuple[tuple[int, int], ...]

    def vorjahre(self, laufend: int) -> tuple[int, ...]:
        """Die Jahre VOR dem laufenden, in denen AfA stand."""
        return tuple(jahr for jahr, cent in self.afa if jahr < laufend and cent)

    def text(self, laufend: int) -> str:
        """Die Rueckfrage: was mit dem Gut geht, Jahr fuer Jahr."""
        reihe = ", ".join(f"{jahr}: {euro(cent)} €" for jahr, cent in self.afa)
        satz = (f"Am Beleg hängt das Anlagegut „{self.bezeichnung}“ "
                f"(Anschaffungskosten {euro(self.ak_brutto_cent)} €) mit der AfA {reihe}. "
                "Es wird mit dem Beleg gelöscht und bringt danach keine AfA mehr.")
        vorher = self.vorjahre(laufend)
        if vorher:
            satz += (f" Achtung: die AfA {jahre_text(vorher)} ist evtl. in einer abgegebenen "
                     "EÜR enthalten — dann die Erklärung berichtigen.")
        return satz


class AnlagegutRueckfrage(BelegFehler):
    """Am Beleg haengt ein Anlagegut, und das Mitloeschen ist nicht bestaetigt."""

    def __init__(self, gut: Anlagegut, laufend: int) -> None:
        super().__init__(f"{gut.text(laufend)} Zum Löschen „Anlagegut mit löschen“ anhaken.")
        self.gut = gut


@dataclass(frozen=True, slots=True)
class Geloescht:
    beleg_id: int
    #: Schon vorher geloescht (Doppelklick): nichts geschrieben.
    schon: bool = False
    grund: str = ""
    anlagegut: Anlagegut | None = None
    #: Jahre VOR dem laufenden, in denen der Beleg in der EUeR stand
    #: (Zahlung oder AfA) -- fuer die Warnung.
    vorjahre: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Wiederhergestellt:
    beleg_id: int
    schon: bool = False
    anlagegut: str | None = None
    #: Gueltige Belege, die dieselbe Rechnung sein koennten (Warnung).
    aehnliche: tuple[sqlite3.Row, ...] = ()


def jahre_text(jahre: tuple[int, ...]) -> str:
    """Jahre fuer einen Satz.

    >>> jahre_text((2024,)), jahre_text((2023, 2024, 2025))
    ('2024', '2023, 2024 und 2025')
    """
    if len(jahre) == 1:
        return str(jahre[0])
    return f"{', '.join(map(str, jahre[:-1]))} und {jahre[-1]}"


def grund_aus(auswahl: str, frei: str) -> str:
    """Der Grund aus Vorschlag und freiem Text; beides zusammen bleibt beides.

    >>> grund_aus("Testbeleg", ""), grund_aus("", " doppelt ")
    ('Testbeleg', 'doppelt')
    >>> grund_aus("doppelt erfasst", "zweites PDF")
    'doppelt erfasst: zweites PDF'
    """
    auswahl, frei = ohne_steuerzeichen(auswahl), ohne_steuerzeichen(frei)
    if auswahl and frei:
        return f"{auswahl}: {frei}"
    return auswahl or frei


def anlagegut_zum_beleg(conn: sqlite3.Connection, beleg_id: int) -> Anlagegut | None:
    """Das gueltige Anlagegut am Beleg mit seiner AfA-Reihe, sonst ``None``."""
    gut = conn.execute("SELECT * FROM v_anlagegut_gueltig WHERE beleg_id = ?",
                       (beleg_id,)).fetchone()
    if gut is None:
        return None
    reihe = conn.execute("SELECT jahr, afa_cent FROM anlagegut_afa_jahr"
                         " WHERE anlagegut_id = ? ORDER BY jahr", (gut["id"],)).fetchall()
    return Anlagegut(id=gut["id"], bezeichnung=gut["bezeichnung"],
                     ak_brutto_cent=int(gut["ak_brutto_cent"]),
                     afa=tuple((int(z["jahr"]), int(z["afa_cent"])) for z in reihe))


def anlagegueter_zu(conn: sqlite3.Connection, beleg_ids: list[int]) -> dict[int, Anlagegut]:
    """Fuer die Belegliste: je Beleg mit gueltigem Anlagegut die Rueckfrage-Daten."""
    if not beleg_ids:
        return {}
    platz = ", ".join("?" * len(beleg_ids))
    gefunden = conn.execute(f"SELECT beleg_id FROM v_anlagegut_gueltig WHERE beleg_id IN ({platz})",
                            beleg_ids).fetchall()
    return {z["beleg_id"]: gut for z in gefunden
            if (gut := anlagegut_zum_beleg(conn, z["beleg_id"])) is not None}


def _zeilen(conn: sqlite3.Connection, sql: str, *werte: object) -> list[dict[str, object]]:
    return [dict(z) for z in conn.execute(sql, werte)]


def loeschen(conn: sqlite3.Connection, beleg_id: int, *, grund: str,
             anlagegut_mit: bool = False, laufend: int | None = None) -> Geloescht:
    """Markiert den Beleg als geloescht. Oeffnet die Transaktion selbst.

    Unter EINER Sperre gelesen und geschrieben: ein Doppelklick findet den
    Beleg schon geloescht und schreibt keinen zweiten Journaleintrag.

    Das Journal (BELEG_GELOESCHT) traegt das vollstaendige Abbild: die
    Belegzeile, die Dateien (Pfad, Pruefsumme, Rolle), die Zahlungen, das
    bestaetigte Ergebnis der Auslesung und ein mitgeloeschtes Anlagegut mit
    seiner AfA-Reihe. Der Hash ist die Pruefsumme des Originals.
    """
    grund = ohne_steuerzeichen(grund)
    if len(grund) < GRUND_MINDESTLAENGE:
        raise BelegFehler("Bitte einen Grund angeben, etwa „Testbeleg“, „doppelt erfasst“ "
                          "oder „falsch ausgelesen“ — gelöscht wird nachvollziehbar.")
    laufend = laufend or int(db.heute()[:4])
    conn.execute("BEGIN IMMEDIATE")
    try:
        ergebnis = markieren(conn, beleg_id, grund=grund, anlagegut_mit=anlagegut_mit,
                             laufend=laufend)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ergebnis


def markieren(conn: sqlite3.Connection, beleg_id: int, *, grund: str,
              anlagegut_mit: bool = False, laufend: int | None = None) -> Geloescht:
    """Die Loeschmarkierung selbst -- in der offenen Transaktion des Aufrufers.

    Fuer :func:`loeschen` und fuer den WISO-Abgleich, der eine in WISO
    geaenderte oder geloeschte Buchung zusammen mit allem anderen in EINER
    Transaktion uebernimmt. Der Grund muss schon geprueft sein.
    """
    laufend = laufend or int(db.heute()[:4])
    beleg = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()
    if beleg is None:
        raise BelegFehler("Diesen Beleg gibt es nicht.")
    if beleg["geloescht_am"] is not None:
        return Geloescht(beleg_id, schon=True, grund=beleg["geloescht_grund"] or "")
    gut = anlagegut_zum_beleg(conn, beleg_id)
    if gut is not None and not anlagegut_mit:
        raise AnlagegutRueckfrage(gut, laufend)

    zeitpunkt = db.jetzt_utc()
    dateien = _zeilen(conn, "SELECT id, pfad, original_name, sha256, groesse, mime, rolle,"
                            " importiert_am FROM belegdatei WHERE beleg_id = ? ORDER BY id",
                      beleg_id)
    zahlungen = _zeilen(conn, "SELECT * FROM zahlung WHERE beleg_id = ? ORDER BY datum, id",
                        beleg_id)
    auslesung = conn.execute(
        "SELECT bestaetigt_json, verfahren, zeitpunkt FROM extraktion WHERE beleg_id = ?"
        " ORDER BY zeitpunkt DESC, id DESC LIMIT 1", (beleg_id,)).fetchone()
    abbild: dict[str, object] = {
        "beleg": dict(beleg), "dateien": dateien, "zahlungen": zahlungen,
        "auslesung": ({"bestaetigt": _json(auslesung["bestaetigt_json"]),
                       "verfahren": auslesung["verfahren"],
                       "zeitpunkt": auslesung["zeitpunkt"]} if auslesung else None),
    }

    if gut is not None:
        # Erst das Gut, dann der Beleg: der Trigger
        # beleg_loeschen_nicht_ohne_anlagegut verlangt diese Reihenfolge.
        gut_zeile = conn.execute("SELECT * FROM anlagegut WHERE id = ?",
                                 (gut.id,)).fetchone()
        conn.execute("UPDATE anlagegut SET geloescht_am = ?, geloescht_grund = ?"
                     " WHERE id = ? AND geloescht_am IS NULL",
                     (zeitpunkt, f"mit Beleg {beleg_id} gelöscht: {grund}", gut.id))
        abbild["anlagegut"] = {**dict(gut_zeile), "afa": [list(j) for j in gut.afa]}
        db.protokolliere(conn, "ANLAGEGUT_GELOESCHT", "anlagegut", gut.id,
                         wert_alt=abbild["anlagegut"],
                         wert_neu={"geloescht_am": zeitpunkt, "beleg_id": beleg_id},
                         begruendung=grund)

    getroffen = conn.execute(
        "UPDATE beleg SET geloescht_am = ?, geloescht_grund = ?, version = version + 1"
        " WHERE id = ? AND geloescht_am IS NULL",
        (zeitpunkt, grund, beleg_id)).rowcount
    if getroffen != 1:
        raise BelegFehler(f"Beleg {beleg_id} wurde gerade geändert — bitte noch einmal.")
    original = next((d["sha256"] for d in dateien if d["rolle"] == "ORIGINAL"), None)
    db.protokolliere(conn, "BELEG_GELOESCHT", "beleg", beleg_id, wert_alt=abbild,
                     wert_neu={"geloescht_am": zeitpunkt, "grund": grund},
                     begruendung=grund, hash_=original)

    vorjahre = {int(str(z["datum"])[:4]) for z in zahlungen
                if str(z["datum"])[:4].isdigit() and int(str(z["datum"])[:4]) < laufend}
    if gut is not None:
        vorjahre |= set(gut.vorjahre(laufend))
    return Geloescht(beleg_id, grund=grund, anlagegut=gut, vorjahre=tuple(sorted(vorjahre)))


def _json(text: str | None) -> object:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def wiederherstellen(conn: sqlite3.Connection, beleg_id: int) -> Wiederhergestellt:
    """Nimmt die Loeschmarkierung zurueck -- mit einem Anlagegut, das mit dem
    Beleg ging. Oeffnet die Transaktion selbst.

    Abgewiesen wird, was dieselbe Ausgabe zweimal in die EUeR braechte: die
    Datei ist inzwischen als anderer Beleg gebucht (das sichert auch der
    Trigger beleg_wiederherstellen_original_einmalig), oder der Beleg ist die
    Verpflegungspauschale einer Reise, die inzwischen neu gebucht ist. Ein
    nur inhaltlich aehnlicher Beleg (gleiche Belegnummer, gleiches Datum mit
    gleichem Betrag) haelt nicht auf -- er steht als Warnung im Ergebnis.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        beleg = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()
        if beleg is None:
            raise BelegFehler("Diesen Beleg gibt es nicht.")
        if beleg["geloescht_am"] is None:
            conn.execute("COMMIT")
            return Wiederhergestellt(beleg_id, schon=True)
        andere = conn.execute(
            "SELECT andere.beleg_id FROM belegdatei d"
            " JOIN belegdatei andere ON andere.sha256 = d.sha256 AND andere.rolle = 'ORIGINAL'"
            "  AND andere.beleg_id <> d.beleg_id"
            " JOIN v_beleg_gueltig b ON b.id = andere.beleg_id"
            " WHERE d.beleg_id = ? AND d.rolle = 'ORIGINAL' LIMIT 1", (beleg_id,)).fetchone()
        if andere is not None:
            raise BelegFehler(
                f"Die Datei von Beleg {beleg_id} ist inzwischen als Beleg "
                f"{andere['beleg_id']} gebucht. Soll der alte zurück, erst Beleg "
                f"{andere['beleg_id']} löschen — sonst stünde dieselbe Rechnung zweimal in "
                "der EÜR.")
        kategorie = conn.execute("SELECT schluessel FROM euer_kategorie WHERE id = ?",
                                 (beleg["kategorie_id"],)).fetchone()
        if (beleg["reise_id"] is not None and kategorie is not None
                and kategorie["schluessel"] == "REISE_VERPFLEGUNG"):
            pauschale = conn.execute(
                "SELECT b.id FROM v_beleg_gueltig b"
                " JOIN euer_kategorie k ON k.id = b.kategorie_id"
                " WHERE b.reise_id = ? AND k.schluessel = 'REISE_VERPFLEGUNG' LIMIT 1",
                (beleg["reise_id"],)).fetchone()
            if pauschale is not None:
                raise BelegFehler(
                    f"Für diese Reise ist die Verpflegungspauschale inzwischen als Beleg "
                    f"{pauschale['id']} gebucht — je Reise gibt es sie nur einmal.")
        conn.execute(
            "UPDATE beleg SET geloescht_am = NULL, geloescht_grund = NULL,"
            " version = version + 1 WHERE id = ?", (beleg_id,))
        db.protokolliere(conn, "BELEG_WIEDERHERGESTELLT", "beleg", beleg_id,
                         wert_alt={"geloescht_am": beleg["geloescht_am"],
                                   "grund": beleg["geloescht_grund"]})
        # Erst der Beleg, dann das Gut (Trigger
        # anlagegut_wiederherstellen_nur_mit_beleg).
        gut = conn.execute("SELECT * FROM anlagegut WHERE beleg_id = ?"
                           " AND geloescht_am IS NOT NULL", (beleg_id,)).fetchone()
        if gut is not None:
            conn.execute("UPDATE anlagegut SET geloescht_am = NULL, geloescht_grund = NULL"
                         " WHERE id = ?", (gut["id"],))
            db.protokolliere(conn, "ANLAGEGUT_WIEDERHERGESTELLT", "anlagegut", gut["id"],
                             wert_alt={"geloescht_am": gut["geloescht_am"],
                                       "grund": gut["geloescht_grund"]},
                             begruendung=f"mit Beleg {beleg_id}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return Wiederhergestellt(beleg_id, anlagegut=gut["bezeichnung"] if gut else None,
                             aehnliche=tuple(_aehnliche(conn, beleg)))


def _aehnliche(conn: sqlite3.Connection, beleg: sqlite3.Row) -> list[sqlite3.Row]:
    """Gueltige Belege, die dieselbe Rechnung sein koennten -- ohne ihn selbst."""
    fremd = beleg["waehrung"] != "EUR" and beleg["betrag_fremdwaehrung_cent"] is not None
    gleich, andere = pipeline.aehnliche_ueberall(
        conn, beleg["lieferant_id"], beleg["belegnummer_extern"], beleg["belegdatum"],
        beleg["betrag_fremdwaehrung_cent"] if fremd else beleg["betrag_brutto_cent"],
        waehrung=beleg["waehrung"], euro=beleg["betrag_brutto_cent"] if fremd else None)
    return [b for b in gleich + andere if b["id"] != beleg["id"]]


def neu_erfassen(conn: sqlite3.Connection, beleg_id: int,
                 datenordner: Path) -> pipeline.Eingang:
    """Legt die Datei eines GELOESCHTEN Belegs wieder in den Eingang -- zum
    neuen Pruefen und Buchen ("falsch ausgelesen").

    Kopiert, nicht verschoben: das Original bleibt schreibgeschuetzt im
    Archiv, es gehoert zum geloeschten Beleg. Kam die Rechnung im Anhang
    einer Mail, kommt die Mail zurueck (die Pruefmaske zeigt dann wieder
    beides).
    """
    beleg = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()
    if beleg is None:
        raise BelegFehler("Diesen Beleg gibt es nicht.")
    if beleg["geloescht_am"] is None:
        raise BelegFehler(f"Beleg {beleg_id} ist nicht gelöscht — seine Datei ist gebucht.")
    datei = conn.execute(
        "SELECT * FROM belegdatei WHERE beleg_id = ? AND rolle IN ('EMAIL', 'ORIGINAL')"
        " ORDER BY rolle = 'ORIGINAL', id LIMIT 1", (beleg_id,)).fetchone()
    if datei is None:
        raise BelegFehler(f"Beleg {beleg_id} hat keine Datei (Eigenbeleg) — bitte neu erfassen.")
    pfad = pfade.absolut(datenordner, datei["pfad"])
    try:
        inhalt = pfad.read_bytes()
    except OSError:
        raise BelegFehler(f"Die Datei von Beleg {beleg_id} fehlt im Archiv — bitte die "
                          "Belegintegrität prüfen.") from None
    if pipeline.pruefsumme(pfad) != datei["sha256"]:
        raise BelegFehler(f"Die Datei von Beleg {beleg_id} passt nicht mehr zu ihrer "
                          "Prüfsumme — bitte die Belegintegrität prüfen.")
    return pipeline.aufnehmen(inhalt, datei["original_name"], datenordner)

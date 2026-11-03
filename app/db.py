"""Datenbankzugriff und Migration.

Eine SQLite-Datei plus Belegordner ist der gesamte Datenbestand. Das macht das
Backup trivial und ist gleichzeitig der nach § 147 Abs. 1 Nr. 1 AO
aufbewahrungspflichtige Bestand.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path

SCHEMA_VERSION = 26


class DatenbankFehler(Exception):
    """Die Datei laesst sich nicht als Datenbank DIESES Programms oeffnen.

    Mit deutscher Meldung, die sagt, was zu tun ist -- die Anwendung zeigt sie
    statt jeder Seite. Drei Faelle: die Datei ist keine SQLite-Datenbank
    (Cloud-Platzhalter, defekte Kopie); sie ist eine, hat Tabellen, aber
    keinen Versionsstand (fremde Datenbank -- CREATE TABLE IF NOT EXISTS liesse
    die fremden Tabellen stehen, und erst /kunden scheiterte); oder sie ist
    neuer als der Code (nach einem Downgrade oder auf einem Zweitrechner mit
    alter Installation -- Schreibzugriffe liefen gegen ein Schema, das der
    Code nicht kennt). In allen drei Faellen wird NICHTS geschrieben.
    """


PROJEKT_WURZEL = Path(__file__).resolve().parent.parent
SCHEMA_DATEI = Path(__file__).resolve().parent / "schema.sql"


def standard_datenordner() -> Path:
    """Wo die Buchfuehrung liegt.

    Auf macOS bewusst ``~/Library/Application Support/Steuer-Tool`` und NICHT
    ``~/Documents``: Documents, Desktop und Downloads stehen unter TCC-Schutz,
    der Zugriff wird der steuernden Anwendung zugerechnet. Beim Start per
    ``.command`` waere das Terminal.app, bei einer gebuendelten App das Bundle
    -- beides erzeugt Berechtigungsdialoge, und beim Bundle bricht der Zugriff,
    sobald sich die Bundle-Identitaet aendert. Application Support faellt nicht
    unter diese Dienste, laeuft dialogfrei und wird von Time Machine
    standardmaessig erfasst.

    iCloud Drive ist als Ablage der laufenden Datenbank ausgeschlossen: defektes
    Locking auf Netzwerk-Dateisystemen, Kopieren waehrend laufender
    Transaktionen und das Einduennen von Dateien zu Platzhaltern durch
    "Mac-Speicher optimieren" sind bekannte Wege zu einer beschaedigten
    SQLite-Datei (sqlite.org/howtocorrupt.html).

    ``STEUER_TOOL_DATEN`` ueberschreibt den Ort, etwa fuer einen Testlauf oder
    ein externes Laufwerk.
    """
    aus_umgebung = os.environ.get("STEUER_TOOL_DATEN")
    if aus_umgebung:
        return Path(aus_umgebung).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Steuer-Tool"
    return PROJEKT_WURZEL / "daten"


STANDARD_DATENPFAD = standard_datenordner() / "steuer.sqlite"


def jetzt_utc() -> str:
    """Zeitstempel in UTC, ISO-8601 mit Sekundengenauigkeit."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def heute() -> str:
    return _dt.date.today().isoformat()


def sortierschluessel(text: object) -> str:
    """Deutscher Sortierschluessel: Umlaute wie ihre Grundbuchstaben, Gross wie Klein.

    SQLites ``COLLATE NOCASE`` kennt nur ASCII; "Ärzte" und "Ölmühle" landeten
    hinter "Zoo". DIN 5007-1 sortiert ä wie a, ö wie o, ü wie u und ß wie ss --
    das ist die Reihenfolge, die ein Adressbuch hat.

    >>> sorted(["Zoo", "Ärzte", "apfel", "Ölmühle", "Straße"], key=sortierschluessel)
    ['apfel', 'Ärzte', 'Ölmühle', 'Straße', 'Zoo']
    """
    if text is None:
        return ""
    zerlegt = unicodedata.normalize("NFKD", str(text).replace("ß", "ss"))
    return "".join(z for z in zerlegt if not unicodedata.combining(z)).casefold()


def _vergleich_deutsch(a: str, b: str) -> int:
    ka, kb = sortierschluessel(a), sortierschluessel(b)
    return (ka > kb) - (ka < kb) or (a > b) - (a < b)


def verbinde(
    pfad: Path | str | None = None, *, threadfrei: bool = False
) -> sqlite3.Connection:
    """Oeffnet die Datenbank und legt sie bei Bedarf an.

    ``isolation_level=None`` schaltet das implizite Transaktionsverhalten von
    Python ab. Transaktionen werden ausschliesslich explizit mit
    ``BEGIN IMMEDIATE`` geoeffnet -- genau das braucht die Festschreibung, damit
    Nummernvergabe, Positionen und PDF-Hash entweder gemeinsam gelten oder
    gemeinsam verfallen.

    ``threadfrei`` hebt die Threadbindung der Verbindung auf
    (``check_same_thread=False``). Gedacht fuer die Weboberflaeche: FastAPI
    oeffnet die Verbindung, fuehrt die Route aus und schliesst sie wieder in
    je einem beliebigen Thread des Threadpools. Die Verbindung bleibt dabei
    je Anfrage privat und ist nie gleichzeitig in zwei Threads in Benutzung
    -- nur nacheinander in verschiedenen. Genau das verbietet sqlite3 in der
    Vorbelegung, und unter Last (Doppelklick, zwei Tabs) endete jede zweite
    Anfrage mit einem ProgrammingError statt mit der vorgesehenen Meldung.
    """
    ziel = Path(pfad) if pfad is not None else STANDARD_DATENPFAD
    if ziel != Path(":memory:"):
        ziel.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ziel, isolation_level=None, check_same_thread=not threadfrei)
    conn.row_factory = sqlite3.Row
    conn.create_collation("DEUTSCH", _vergleich_deutsch)
    # Erst ein Lesezugriff zeigt, ob die Datei eine Datenbank ist: connect()
    # oeffnet auch einen Cloud-Platzhalter oder eine defekte Kopie anstandslos,
    # und der rohe "file is not a database" kaeme sonst irgendwo spaeter.
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.DatabaseError as fehler:
        conn.close()
        raise DatenbankFehler(
            f"Die Datei {ziel} ist keine lesbare Datenbank — bitte die letzte Sicherung "
            "zurückspielen."
        ) from fehler
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# Migrationsschritte fuer BESTEHENDE Datenbanken, aufsteigend nach Zielversion.
#
# schema.sql beschreibt immer den aktuellen Zielzustand und legt neue Datenbanken
# in einem Zug an. Eine bestehende Datenbank bekommt davon nichts mit: CREATE
# TABLE IF NOT EXISTS fuegt keine Spalte hinzu und benennt keine um. Deshalb
# bekommt jede Schemaaenderung hier einen Schritt. Fehlt er, laeuft die
# Anwendung gegen Spalten, die es nicht gibt -- und zwar erst dann, wenn jemand
# mit Daten arbeitet.
#
# Sichten sind davon ausgenommen: schema.sql verwirft und erzeugt sie bei jedem
# Start neu.
# Zustand der Tabelle zeiteintrag in Version 2, eingefroren. Migrationen
# bringen ihr DDL selbst mit und verlassen sich NICHT auf schema.sql: dort steht
# immer der heutige Zielzustand, der in einem Jahr anders aussieht. Eine
# Migration muss aber auch dann noch genau das tun, was sie beim Schreiben tat.
_ZEITEINTRAG_V2 = """
CREATE TABLE zeiteintrag (
    id             INTEGER PRIMARY KEY,
    kunde_id       INTEGER NOT NULL REFERENCES kunde (id),
    titel          TEXT    NOT NULL,
    beschreibung   TEXT,
    datum          TEXT    NOT NULL,
    start_zeit     TEXT,
    end_zeit       TEXT,
    dauer_minuten  INTEGER NOT NULL CHECK (dauer_minuten > 0),
    abrechnungsmodus TEXT  NOT NULL DEFAULT 'STUNDEN'
        CHECK (abrechnungsmodus IN ('STUNDEN', 'PAUSCHAL', 'KULANZ', 'INTERN')),
    grund          TEXT,
    pauschal_cent  INTEGER,
    satz_cent_snapshot INTEGER,
    rechnung_id    INTEGER REFERENCES rechnung (id),
    freigegeben_aus_rechnung_id INTEGER REFERENCES rechnung (id),
    abgeschrieben_am    TEXT,
    abgeschrieben_grund TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    erfasst_am     TEXT    NOT NULL,
    geaendert_am   TEXT,
    CHECK (abrechnungsmodus <> 'PAUSCHAL' OR pauschal_cent IS NOT NULL),
    CHECK (abrechnungsmodus NOT IN ('KULANZ', 'INTERN') OR grund IS NOT NULL),
    CHECK (abrechnungsmodus <> 'INTERN' OR rechnung_id IS NULL),
    CHECK (abgeschrieben_am IS NULL
           OR (rechnung_id IS NULL AND abgeschrieben_grund IS NOT NULL)),
    CHECK (abrechnungsmodus <> 'KULANZ' OR satz_cent_snapshot IS NOT NULL),
    CHECK (start_zeit IS NULL OR end_zeit IS NULL OR end_zeit >= start_zeit)
)
"""

# Migrationsschritte fuer BESTEHENDE Datenbanken, aufsteigend nach Zielversion.
#
# schema.sql legt NEUE Datenbanken in einem Zug an. Eine bestehende bekommt
# davon nichts mit: CREATE TABLE IF NOT EXISTS fuegt keine Spalte hinzu und
# benennt keine um. Deshalb bekommt jede Schemaaenderung hier einen Schritt.
# Fehlt er, laeuft die Anwendung gegen Spalten, die es nicht gibt -- und zwar
# erst dann, wenn jemand mit echten Daten arbeitet.
#
# Ein Schritt ist entweder eine SQL-Anweisung oder eine Funktion, die die
# Verbindung bekommt. Funktionen tragen die Schritte, die eine Bedingung
# brauchen: SQLite kennt kein "ALTER TABLE ... IF EXISTS", und RAISE() gibt es
# nur im Rumpf eines Triggers.
#
# Sichten sind ausgenommen: schema.sql verwirft und erzeugt sie bei jedem Start.
Schritt = str | Callable[[sqlite3.Connection], None]


def _eigenbeleg_spalte(conn: sqlite3.Connection) -> None:
    """Traegt ``beleg.ist_eigenbeleg`` nach, falls es die Tabelle schon gibt.

    Eine Datenbank, die noch kein ``beleg`` hat, bekommt die Tabelle gleich
    darauf von schema.sql -- mitsamt der Spalte. Dann ist hier nichts zu tun,
    und das ist kein Sonderfall: es ist der Normalfall fuer jede Datenbank, die
    aus einer Version vor den Eingangsbelegen kommt. Ein bedingungsloses ALTER
    braeche an genau dieser Stelle.
    """
    gibt_es = conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'beleg'"
    ).fetchone()["n"]
    if not gibt_es:
        return
    spalten = {z["name"] for z in conn.execute("PRAGMA table_info(beleg)")}
    if "ist_eigenbeleg" not in spalten:
        conn.execute(
            "ALTER TABLE beleg ADD COLUMN ist_eigenbeleg INTEGER NOT NULL DEFAULT 0"
        )


def _spalte_nachtragen(
    tabelle: str, spalte: str, definition: str
) -> Callable[[sqlite3.Connection], None]:
    """Ein Migrationsschritt, der eine Spalte ergaenzt -- falls sie fehlt.

    Fuer Tabellen, die eine aeltere Datenbank noch gar nicht hat, ist nichts
    zu tun: schema.sql legt sie gleich darauf an, mitsamt der Spalte. Und
    eine Datenbank, die den Schritt schon einmal durchlaufen hat, soll ihn
    ohne Fehler noch einmal ueberstehen. Ein bedingungsloses ALTER braeche in
    beiden Faellen.
    """

    def schritt(conn: sqlite3.Connection) -> None:
        gibt_es = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = ?",
            (tabelle,),
        ).fetchone()["n"]
        if not gibt_es:
            return
        spalten = {z["name"] for z in conn.execute(f"PRAGMA table_info({tabelle})")}
        if spalte not in spalten:
            conn.execute(f"ALTER TABLE {tabelle} ADD COLUMN {spalte} {definition}")

    return schritt


def _trigger_erneuern(name: str) -> Callable[[sqlite3.Connection], None]:
    """Ein Migrationsschritt, der einen Trigger durch die Fassung aus schema.sql ersetzt.

    ``CREATE TRIGGER IF NOT EXISTS`` in schema.sql laesst einen vorhandenen
    Trigger stehen -- eine verschaerfte Regel erreichte bestehende
    Datenbanken sonst nie. Die Definition wird aus schema.sql gelesen und
    nicht ein zweites Mal hier abgeschrieben: EINE Fassung, in frischen und
    migrierten Datenbanken gleich.
    """

    def schritt(conn: sqlite3.Connection) -> None:
        text = SCHEMA_DATEI.read_text(encoding="utf-8")
        kopf = f"CREATE TRIGGER IF NOT EXISTS {name}\n"
        anfang = text.index(kopf)
        ende = text.index("\nEND;", anfang) + len("\nEND;")
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        # Ohne die Tabelle (sehr alte Datenbank) legt schema.sql gleich danach
        # Tabelle und Trigger zusammen an.
        tabelle = text[anfang:ende].split(" ON ", 1)[1].split()[0]
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (tabelle,)
        ).fetchone():
            conn.execute(text[anfang:ende])

    return schritt


def _datei(conn: sqlite3.Connection) -> str:
    """Der Pfad der geoeffneten Datenbankdatei fuer Meldungen."""
    for zeile in conn.execute("PRAGMA database_list"):
        if zeile["name"] == "main" and zeile["file"]:
            return zeile["file"]
    return "(im Speicher)"


def datenordner_von(conn: sqlite3.Connection) -> Path | None:
    """Der Ordner der geoeffneten Datenbankdatei. ``None`` im Speicher."""
    for zeile in conn.execute("PRAGMA database_list"):
        if zeile["name"] == "main" and zeile["file"]:
            return Path(zeile["file"]).resolve().parent
    return None


def _pfade_relativieren(conn: sqlite3.Connection) -> None:
    """Macht absolute Dateipfade relativ zum Datenordner (siehe app/pfade.py).

    Betrifft ``belegdatei.pfad`` und ``rechnung.pdf_pfad``. Nur Pfade, die
    unter dem Ordner der Datenbank liegen, werden umgeschrieben; alle anderen
    bleiben absolut -- ein relativer Pfad, der nirgends hinfuehrt, waere
    schlimmer.

    ``belegdatei.pfad`` ist per Trigger unveraenderlich. Fuer diesen Schritt
    wird der Trigger entfernt; schema.sql legt ihn gleich nach den
    Migrationen wieder an. Der Nachweis der Unveraenderbarkeit ist die
    Pruefsumme, und die bleibt, wie sie ist.
    """
    ordner = datenordner_von(conn)
    if ordner is None:
        return
    tabellen = {
        z["name"] for z in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    conn.execute("DROP TRIGGER IF EXISTS belegdatei_unveraenderlich")
    for tabelle, spalte in (("belegdatei", "pfad"), ("rechnung", "pdf_pfad")):
        if tabelle not in tabellen:
            continue
        for zeile in conn.execute(
            f"SELECT id, {spalte} AS pfad FROM {tabelle} WHERE {spalte} IS NOT NULL"
        ).fetchall():
            pfad = Path(zeile["pfad"])
            if not pfad.is_absolute():
                continue
            try:
                relativ = pfad.resolve().relative_to(ordner)
            except ValueError:
                continue
            conn.execute(
                f"UPDATE {tabelle} SET {spalte} = ? WHERE id = ?", (str(relativ), zeile["id"])
            )


def _tabellendefinition(name: str) -> str:
    """Die CREATE-Anweisung einer Tabelle, wie schema.sql sie heute schreibt."""
    text = SCHEMA_DATEI.read_text(encoding="utf-8")
    kopf = f"CREATE TABLE IF NOT EXISTS {name} ("
    start = text.index(kopf)
    ende = text.index("\n);", start) + len("\n);")
    return text[start:ende]


def _tabelle_anlegen(name: str) -> Callable[[sqlite3.Connection], None]:
    """Ein Migrationsschritt, der eine neue Tabelle nach schema.sql anlegt.

    ``CREATE TABLE IF NOT EXISTS`` aus schema.sql selbst: der Schritt ist
    wiederholbar und schreibt die Definition nicht ein zweites Mal ab.
    """

    def schritt(conn: sqlite3.Connection) -> None:
        conn.execute(_tabellendefinition(name))

    return schritt


def _rechnung_neu(conn: sqlite3.Connection) -> None:
    """Baut rechnung aus der heutigen Definition neu, wenn sie noch die alte traegt.

    Die Tabelle aus Version 1 erzwingt ``brutto_cent >= 0``. Ein Storno ist
    aber ein Dokument mit negativem Betrag -- in einer migrierten Datenbank
    scheiterte jedes Storno am CHECK, und ein Leistungsnachweis an der
    Typliste. CHECKs lassen sich per ALTER nicht aendern, also Neubau nach
    dem dokumentierten Verfahren: neue Tabelle unter Zwischennamen, Zeilen
    uebernehmen, alte verwerfen, umbenennen. Trigger und Indizes der Tabelle
    legt schema.sql danach wieder an.

    Uebernommen werden die gemeinsamen Spalten. Ein Storno aus der alten Welt
    (dort zwangslaeufig mit positivem Betrag) bekommt das Vorzeichen, das es
    heute traegt; ein Entwurf verliert einen Versandvermerk, den es nach
    heutiger Regel nicht geben kann.
    """
    zeile = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rechnung'"
    ).fetchone()
    if zeile is None or "CHECK (brutto_cent >= 0)" not in (zeile["sql"] or ""):
        return
    conn.execute(_tabellendefinition("rechnung").replace(
        "CREATE TABLE IF NOT EXISTS rechnung (", "CREATE TABLE rechnung_neu (", 1))
    alte = [z["name"] for z in conn.execute("PRAGMA table_info(rechnung)")]
    neue = [z["name"] for z in conn.execute("PRAGMA table_info(rechnung_neu)")]
    gemeinsam = [spalte for spalte in neue if spalte in alte]
    ausdruecke = []
    for spalte in gemeinsam:
        if spalte in ("brutto_cent", "netto_cent"):
            ausdruecke.append(f"CASE typ WHEN 'STORNO' THEN -ABS({spalte}) ELSE {spalte} END")
        elif spalte == "versendet_am":
            ausdruecke.append("CASE WHEN status = 'ENTWURF' THEN NULL ELSE versendet_am END")
        else:
            ausdruecke.append(spalte)
    conn.execute(
        f"INSERT INTO rechnung_neu ({', '.join(gemeinsam)})"
        f" SELECT {', '.join(ausdruecke)} FROM rechnung"
    )
    conn.execute("DROP TABLE rechnung")
    conn.execute("ALTER TABLE rechnung_neu RENAME TO rechnung")


def _extraktion_neu(conn: sqlite3.Connection) -> None:
    """Baut extraktion mit der heutigen Definition neu, wenn ihr CHECK die
    Quelle 'email' noch nicht kennt (E-Mails als Beleg, Version 20).

    CHECKs lassen sich per ALTER nicht aendern: neue Tabelle unter
    Zwischennamen, Zeilen uebernehmen, alte verwerfen, umbenennen -- wie bei
    :func:`_rechnung_neu`. Den Index legt schema.sql danach wieder an.
    """
    zeile = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'extraktion'"
    ).fetchone()
    # Am CHECK selbst gemessen, nicht am Wort: der Kommentar in schema.sql
    # nennt 'email' auch.
    if zeile is None or "'email', 'manuell')" in (zeile["sql"] or ""):
        return
    conn.execute(_tabellendefinition("extraktion").replace(
        "CREATE TABLE IF NOT EXISTS extraktion (", "CREATE TABLE extraktion_neu (", 1))
    alte = [z["name"] for z in conn.execute("PRAGMA table_info(extraktion)")]
    gemeinsam = [z["name"] for z in conn.execute("PRAGMA table_info(extraktion_neu)")
                 if z["name"] in alte]
    conn.execute(f"INSERT INTO extraktion_neu ({', '.join(gemeinsam)})"
                 f" SELECT {', '.join(gemeinsam)} FROM extraktion")
    conn.execute("DROP TABLE extraktion")
    conn.execute("ALTER TABLE extraktion_neu RENAME TO extraktion")


def _laufenden_timer_nachtragen(conn: sqlite3.Connection) -> None:
    """Gibt einem gerade laufenden Timer seinen ersten Arbeitsabschnitt (Version 21).

    Beginn ist der gespeicherte Start: Datum und Uhrzeit in Ortszeit, nach
    UTC umgerechnet wie jeder neue Abschnitt. Nur fuer den EINEN offenen
    Timer und nur, wenn er noch keinen Abschnitt hat -- der Schritt ist
    wiederholbar. Eine Uhrzeit, die sich nicht lesen laesst, bekommt keinen
    Abschnitt: dann gilt weiter die alte Rechnung aus Datum und Uhrzeit.
    """
    from app.logik import zeiten

    for zeile in conn.execute(
        "SELECT id, datum, start_zeit FROM zeiteintrag"
        " WHERE start_zeit IS NOT NULL AND end_zeit IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM zeit_abschnitt a WHERE a.zeiteintrag_id = zeiteintrag.id)"
    ).fetchall():
        try:
            beginn = zeiten.ortszeit_als_utc(zeile["datum"], zeile["start_zeit"])
        except ValueError:
            continue
        conn.execute(
            "INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc) VALUES (?, ?)",
            (zeile["id"], beginn),
        )


def _rundungshinweis_der_entwuerfe(conn: sqlite3.Connection) -> None:
    """Gibt den Entwuerfen den Rundungssatz von Version 21 (Version 21).

    Der Satz eines Entwurfs wird bei jedem Neuberechnen der Positionen neu
    geschrieben -- ein Entwurf aus Version 20, den niemand mehr anfasst,
    behielte aber "Abrechnung in angefangenen 15 Minuten." und truege ihn
    beim Festschreiben auf die Rechnung: eine Zeittaktklausel je Taetigkeit,
    die so nie angewandt wurde, auch auf Rechnungen "pro Eintrag". Hier also
    derselbe Satz, den das Neuberechnen schriebe (mit der eben
    eingefrorenen Regel und der Gruppierung des Entwurfs). Ein eigener Satz
    des Betriebs bleibt, solange er zur Regel passt. Betraege und Positionen
    bleiben unberuehrt; festgeschriebene Rechnungen behalten ihren Satz.
    """
    from app.logik import festschreiben

    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    for rechnung in conn.execute(
        "SELECT * FROM rechnung WHERE status = 'ENTWURF'"
    ).fetchall():
        regel = festschreiben.regel_des_entwurfs(conn, rechnung)
        conn.execute(
            "UPDATE rechnung SET rundung_hinweis = ? WHERE id = ?",
            (festschreiben._rundungshinweis(betrieb, regel, rechnung["gruppierung"]),
             rechnung["id"]),
        )


def _position_zeiteintrag_neu(conn: sqlite3.Connection) -> None:
    """Baut position_zeiteintrag neu, wenn sie noch die alte Form hat.

    Zwei Altlasten, beide aus Version 1: ein UNIQUE auf zeiteintrag_id --
    damit war eine Stunde nach einem Storno nie wieder abrechenbar, denn die
    alte Zuordnung bleibt als Teil des stornierten Belegs bestehen --, und
    nach der Migration 2 ein Fremdschluessel, der auf zeiteintrag_alt zeigte,
    eine Tabelle, die es nicht mehr gab. Die Doppelabrechnung verhindert
    heute der Trigger pz_keine_doppelabrechnung, der nur FESTGESCHRIEBENE
    Rechnungen zaehlt.

    Nichts referenziert diese Tabelle, deshalb ist der Neubau ohne Umweg
    moeglich. Den Trigger legt schema.sql gleich danach wieder an.
    """
    zeile = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'position_zeiteintrag'"
    ).fetchone()
    if zeile is None:
        return
    sql = zeile["sql"] or ""
    if "zeiteintrag_alt" not in sql and "UNIQUE" not in sql.upper():
        return
    # Ohne RENAME: SQLite prueft beim Umbenennen alle Sichten der Datenbank,
    # und in einer alten Datenbank zeigen die noch auf Spalten, die es seit
    # Version 2 nicht mehr gibt. Also ueber eine Zwischentabelle kopieren.
    definition = """
        (
            position_id    INTEGER NOT NULL REFERENCES rechnungsposition (id) ON DELETE CASCADE,
            zeiteintrag_id INTEGER NOT NULL REFERENCES zeiteintrag (id),
            PRIMARY KEY (position_id, zeiteintrag_id)
        )
    """
    conn.execute("CREATE TEMP TABLE pz_zwischen AS SELECT position_id, zeiteintrag_id"
                 " FROM position_zeiteintrag")
    conn.execute("DROP TABLE position_zeiteintrag")
    conn.execute(f"CREATE TABLE position_zeiteintrag {definition}")
    conn.execute(
        "INSERT OR IGNORE INTO position_zeiteintrag (position_id, zeiteintrag_id)"
        " SELECT position_id, zeiteintrag_id FROM pz_zwischen"
    )
    conn.execute("DROP TABLE pz_zwischen")


MIGRATIONEN: dict[int, tuple[Schritt, ...]] = {
    2: (
        # abrechenbar und abrechnungsart wurden zu abrechnungsmodus
        # zusammengelegt, erledigt_am zu abgeschrieben_am, position_id entfaellt.
        # SQLite kann Spalten nicht per ALTER mit CHECK versehen, deshalb der
        # klassische Weg: umbenennen, neu anlegen, umschaufeln, verwerfen.
        #
        #
        # Reihenfolge nach dem dokumentierten Verfahren von SQLite: die NEUE
        # Tabelle unter einem Zwischennamen anlegen, umschaufeln, die alte
        # verwerfen, die neue umbenennen. Vorher wurde die ALTE umbenannt --
        # und seit SQLite 3.26 schreibt RENAME die Fremdschluessel anderer
        # Tabellen mit um: position_zeiteintrag zeigte auf zeiteintrag_alt und
        # nach deren DROP ins Leere. Jede Abrechnung einer Zeit scheiterte in
        # einer migrierten Datenbank mit "no such table: zeiteintrag_alt".
        # Beim Umbenennen von zeiteintrag_neu nach zeiteintrag wird nichts
        # umgeschrieben, denn nichts verweist auf den Zwischennamen; die
        # Verweise auf "zeiteintrag" treffen danach wieder eine Tabelle.
        # Dass das DROP der alten Tabelle nicht an den Fremdschluesseln
        # scheitert, stellt migriere() sicher: es schaltet sie fuer die
        # Dauer der Migration ab und prueft sie vor jedem COMMIT.
        _ZEITEINTRAG_V2.replace("CREATE TABLE zeiteintrag (", "CREATE TABLE zeiteintrag_neu (", 1),
        """
        INSERT INTO zeiteintrag_neu (
            id, kunde_id, titel, beschreibung, datum, start_zeit, end_zeit,
            dauer_minuten, abrechnungsmodus, grund, pauschal_cent,
            satz_cent_snapshot, rechnung_id, abgeschrieben_am, abgeschrieben_grund,
            version, erfasst_am, geaendert_am
        )
        SELECT id, kunde_id, titel, beschreibung, datum, start_zeit, end_zeit,
               dauer_minuten,
               CASE WHEN abrechenbar = 0                     THEN 'INTERN'
                    WHEN abrechnungsart = 'OHNE_BERECHNUNG'  THEN 'KULANZ'
                    ELSE abrechnungsart END,
               CASE WHEN abrechenbar = 0                     THEN 'aus Altdaten uebernommen'
                    WHEN abrechnungsart = 'OHNE_BERECHNUNG'  THEN 'aus Altdaten uebernommen'
                    END,
               pauschal_cent,
               -- KULANZ verlangt einen eingefrorenen Satz. Fehlt er in den
               -- Altdaten, wird der heutige Betriebsstandard eingesetzt: die
               -- einzige verfuegbare Zahl. Die Migration steht im Journal.
               CASE WHEN abrechnungsart = 'OHNE_BERECHNUNG' AND satz_cent_snapshot IS NULL
                    THEN COALESCE((SELECT standardsatz_cent FROM betrieb WHERE id = 1), 0)
                    ELSE satz_cent_snapshot END,
               rechnung_id, erledigt_am, erledigt_grund,
               version, erfasst_am, geaendert_am
          FROM zeiteintrag
        """,
        "DROP TABLE zeiteintrag",
        "ALTER TABLE zeiteintrag_neu RENAME TO zeiteintrag",
        "ALTER TABLE rechnung ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE betrieb ADD COLUMN nullrechnung_zulassen INTEGER NOT NULL DEFAULT 0",
    ),
    3: (
        # Der Leistungszeitraum ist nach § 34a Satz 1 UStDV keine Pflichtangabe
        # und wird abwaehlbar. Altbestaende behalten die bisherige Darstellung,
        # deshalb DEFAULT 1 -- eine Migration darf das Aussehen bereits
        # ausgestellter Rechnungen nicht veraendern.
        "ALTER TABLE rechnung ADD COLUMN leistungszeitraum_zeigen"
        " INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE betrieb ADD COLUMN leistungszeitraum_default"
        " INTEGER NOT NULL DEFAULT 1",
    ),
    4: (
        # Zielordner der Sicherung. Leer heisst "backup/ im Datenordner".
        "ALTER TABLE betrieb ADD COLUMN backup_pfad TEXT",
    ),
    5: (
        # Der Leistungskatalog wird ab jetzt auch aus der Zeiterfassung
        # beschrieben. Damit daraus kein Haufen aus 'Beratung', 'beratung' und
        # 'BERATUNG' wird, bekommt der Titel eine Eindeutigkeit ohne Ruecksicht
        # auf Gross- und Kleinschreibung.
        #
        # Doppelte Titel koennen in einer Altdatenbank nicht stehen: bis zu
        # dieser Version hat nichts in die Tabelle geschrieben. Sollte doch
        # jemand von Hand eingefuegt haben, bricht die Migration -- lauter
        # Abbruch ist besser, als still eine Zeile wegzuwerfen.
        "CREATE UNIQUE INDEX IF NOT EXISTS leistung_titel_einmalig"
        " ON leistung (titel COLLATE NOCASE)",
    ),
    # Version 6 (Eingangsbelege) braucht KEINEN Schritt: sie bringt nur neue
    # Tabellen, Indizes und Trigger, und die legt schema.sql mit
    # CREATE ... IF NOT EXISTS auch in einer bestehenden Datenbank an. Ein
    # Schritt waere eine zweite Kopie derselben Definition.
    #
    # Dass zahlung.beleg_id KEINEN Fremdschluessel bekommt, ist Absicht: SQLite
    # kann ihn nicht per ALTER ergaenzen, und die Tabelle neu zu bauen heisst,
    # die Bemessungsgrundlage der EUeR und des § 19-Grenzwerts umzuschaufeln --
    # ein Verlust dort faellt erst im Jahresabschluss auf. Dieselbe Garantie
    # gibt der Trigger zahlung_beleg_existiert in schema.sql, und der laesst
    # sich nachtragen.
    7: (
        # Eigenbelege und der Reise-Container. Die Tabelle reise legt schema.sql
        # an; die SPALTE ist_eigenbeleg auf beleg muss hier nachgetragen werden
        # -- CREATE TABLE IF NOT EXISTS fuegt keine Spalte hinzu.
        #
        # Die Begruendungspflicht fuer Eigenbelege traegt deshalb ein Trigger und
        # kein CHECK: ein CHECK laesst sich per ALTER nicht ergaenzen, und die
        # Belegtabelle dafuer neu zu bauen heisst, die Buchfuehrung
        # umzuschaufeln. Der Trigger steht in schema.sql und gilt damit fuer
        # neue und migrierte Datenbanken gleich -- EIN Mechanismus, EINE
        # Fehlermeldung.
        _eigenbeleg_spalte,
    ),
    8: (
        # Startwert der Kundennummern beim Umstieg aus Word. betrieb gibt es
        # seit Version 1, deshalb genuegt hier ein unbedingtes ALTER.
        "ALTER TABLE betrieb ADD COLUMN kundennummer_start INTEGER NOT NULL DEFAULT 1",
    ),
    9: (
        # Leistungen an Unternehmer im uebrigen Gemeinschaftsgebiet.
        #
        # Der Leistungsort wandert als Schnappschuss in die Rechnung: er
        # entscheidet, welcher Hinweis darauf gehoert, und darf sich nicht mit
        # dem Kundenstamm aendern. Altbestaende bekommen INLAND -- das war bis
        # hierher die einzige Moeglichkeit, und eine Migration darf den Inhalt
        # bereits ausgestellter Rechnungen nicht umdeuten.
        "ALTER TABLE rechnung ADD COLUMN leistungsort TEXT NOT NULL DEFAULT 'INLAND'",
        "ALTER TABLE betrieb ADD COLUMN hinweis_rc_text TEXT NOT NULL"
        " DEFAULT 'Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).'",
    ),
    # Version 10 (Anlagevermoegen) braucht keinen Schritt: nur neue Tabellen,
    # und die legt schema.sql mit CREATE ... IF NOT EXISTS auch in einer
    # bestehenden Datenbank an.
    11: (
        # Zweites Sicherungsziel, etwa eine externe Platte.
        "ALTER TABLE betrieb ADD COLUMN backup_zweitziel TEXT",
    ),
    12: (
        # Angleichung an die bisherige Word-Vorlage und an § 3a Abs. 2 UStG.
        #
        # Die Betreffzeile ist keine Pflichtangabe (§ 34a UStDV kennt sie nicht),
        # aber die Zeile, nach der der Empfaenger die Rechnung wiederfindet.
        # Altrechnungen bleiben ohne -- sie wurden ohne ausgestellt, und eine
        # Migration darf den Inhalt festgeschriebener Belege nicht ergaenzen.
        "ALTER TABLE rechnung ADD COLUMN betreff TEXT",
        # Der Nachweis, dass der Empfaenger Unternehmer ist. Ohne ihn verlagert
        # § 3a Abs. 2 UStG den Leistungsort NICHT, der Umsatz waere im Inland
        # steuerbar und zaehlte in den § 19-Gesamtumsatz. Bei EU-Kunden traegt
        # die USt-IdNr. den Nachweis, im Drittland gibt es keine.
        "ALTER TABLE kunde ADD COLUMN unternehmer_nachweis TEXT",
        # Abschaltbarer Zahlungsabsatz. Der CHECK aus schema.sql laesst sich per
        # ALTER nicht nachtragen; die Spalte haelt der Default und die
        # Oberflaeche, die nur 0 und 1 schreibt. Die Tabelle dafuer neu zu bauen
        # hiesse, die Stammdaten umzuschaufeln -- dieselbe Abwaegung wie bei
        # kundennummer_start in Version 8.
        "ALTER TABLE betrieb ADD COLUMN zahlungsziel_zeigen INTEGER NOT NULL DEFAULT 1",
        # Das Format der Kundennummer. Bis hierher war "K-0001" fest verdrahtet;
        # gewachsene Nummernkreise sehen aber anders aus, und die Nummer steht
        # auf den bisherigen Rechnungen. Altbestaende behalten das bisherige
        # Format als Vorbelegung -- eine Migration darf vergebene Nummern nicht
        # umdeuten.
        "ALTER TABLE betrieb ADD COLUMN kundennummer_format TEXT NOT NULL"
        " DEFAULT 'K-{:04d}'",
    ),
    13: (
        # Fremdwaehrung: der Nominalbetrag des Belegs, waehrend betrag_brutto_cent
        # ab jetzt immer der abgeflossene Euro-Betrag ist. Altbestaende sind
        # Euro-Belege (die Oberflaeche nahm nichts anderes sinnvoll an), die
        # Spalte bleibt dort leer.
        _spalte_nachtragen("beleg", "betrag_fremdwaehrung_cent", "INTEGER"),
        # Betrieblicher Anteil eines Anlageguts. 100 fuer Altbestaende: so
        # wurde bisher gerechnet, und eine Migration darf eine bereits
        # erklaerte AfA nicht veraendern. Der CHECK aus schema.sql laesst sich
        # per ALTER nicht nachtragen; die Oberflaeche schreibt nur 0 bis 100.
        _spalte_nachtragen("anlagegut", "aufteilungsquote", "INTEGER NOT NULL DEFAULT 100"),
        # Der UNIQUE-Index auf anlagegut.beleg_id und der auf rechnung.storno_von_id
        # kommen aus schema.sql (CREATE INDEX IF NOT EXISTS) -- kein Schritt noetig.
        #
        # Dateipfade relativ zum Datenordner: Belege und Rechnungs-PDF standen
        # absolut in der Datenbank und waren nach einer Wiederherstellung an
        # anderer Stelle alle unerreichbar.
        _pfade_relativieren,
    ),
    14: (
        # Angleich einer aus Version 1 migrierten Datenbank. Der Vergleich
        # einer solchen mit einer frischen zeigte drei Luecken, die allen
        # Migrationstests entgangen waren -- die Fixture hatte keine Rechnung.
        #
        # rechnungsposition bekam in einer spaeteren Version drei Spalten,
        # fuer die es nie einen Schritt gab. Jeder Entwurf scheiterte in einer
        # migrierten Datenbank an "no such column: nachlass_cent".
        _spalte_nachtragen("rechnungsposition", "nachlass_cent", "INTEGER NOT NULL DEFAULT 0"),
        _spalte_nachtragen("rechnungsposition", "nachlass_grund", "TEXT"),
        _spalte_nachtragen("rechnungsposition", "darstellung", "TEXT NOT NULL DEFAULT 'BETRAG'"),
        # rechnung: die CHECKs aus Version 1 liessen kein Storno zu.
        _rechnung_neu,
        # position_zeiteintrag: UNIQUE weg, Fremdschluessel wieder auf zeiteintrag.
        _position_zeiteintrag_neu,
    ),
    15: (
        # Uebernachtung ausserhalb der Wohnung an der Reise: ohne sie gibt es
        # fuer An- und Abreisetag keine Verpflegungspauschale (§ 9 Abs. 4a
        # Satz 3 Nr. 2 EStG). 1 fuer Altbestaende -- so wurde bisher
        # gerechnet, und eine Migration darf gebuchte Pauschalen nicht
        # umdeuten. Der CHECK aus schema.sql laesst sich per ALTER nicht
        # nachtragen; die Oberflaeche schreibt nur 0 und 1.
        _spalte_nachtragen("reise", "uebernachtung", "INTEGER NOT NULL DEFAULT 1"),
    ),
    16: (
        # Keine Zahlung auf ein Storno-Dokument. Bis hierher wies das nur die
        # Route ab; ein direktes INSERT nahm die Datenbank an, und der Betrag
        # zaehlte in EUeR und § 19-Umsatz. Der Trigger in schema.sql ist
        # verschaerft -- CREATE ... IF NOT EXISTS ersetzt den alten aber nicht.
        _trigger_erneuern("zahlung_nur_festgeschrieben"),
    ),
    17: (
        # Der Eingangsordner fuer Belege (Rechnungen vom Handy). NULL heisst
        # aus -- eine bestehende Datenbank holt also nichts ab, bis der Nutzer
        # einen Ordner einstellt. Die NEUE Spalte traegt ihren CHECK gleich
        # mit: ALTER TABLE ... ADD COLUMN nimmt einen CHECK fuer die
        # hinzugefuegte Spalte an (nachtragen laesst er sich nur an einer
        # vorhandenen nicht). Damit gilt dieselbe Regel wie in einer frischen
        # Datenbank.
        _spalte_nachtragen("betrieb", "belegordner_pfad", "TEXT"),
        _spalte_nachtragen("betrieb", "belegordner_automatisch",
                           "INTEGER NOT NULL DEFAULT 1"
                           " CHECK (belegordner_automatisch IN (0, 1))"),
    ),
    18: (
        # Steuerruecklage: die Angaben zur geschaetzten Nachzahlung je Jahr.
        # Nur eine neue Tabelle -- schema.sql legte sie auch ohne Schritt an.
        # Der Schritt macht die Version trotzdem nachvollziehbar (Journal
        # "MIGRATION 18") und nimmt die Definition aus schema.sql, damit es
        # EINE Fassung gibt.
        _tabelle_anlegen("steuerschaetzung"),
    ),
    19: (
        # Fremdwaehrung nachpruefbar: Kurs (mit allen Stellen der EZB),
        # Kursdatum, Quelle, Abrufzeit und SHA-256 der Kurstabelle, der
        # umgerechnete Euro-Betrag und der Betrag laut Kreditkarte. Die neuen
        # Spalten tragen ihren CHECK gleich mit (ALTER TABLE ... ADD COLUMN
        # nimmt ihn fuer die hinzugefuegte Spalte an).
        #
        # Altbestaende bleiben, wie sie sind: ein Fremdwaehrungsbeleg aus der
        # Zeit davor traegt den Euro-Betrag, den der Nutzer laut Kontoauszug
        # oder Kreditkarte eingetragen hat, und keinen Kurs. Ihn nachtraeglich
        # umzurechnen oder als "Kreditkartenbetrag" zu deuten hiesse, einen
        # gebuchten Beleg umzudeuten; die Liste zeigt ihn als das, was er ist.
        # Die Trigger fuer den stimmigen Euro-Betrag legt schema.sql an.
        _spalte_nachtragen("beleg", "kurs",
                           "TEXT CHECK (kurs IS NULL OR CAST(kurs AS REAL) > 0)"),
        _spalte_nachtragen("beleg", "kurs_datum", "TEXT"),
        _spalte_nachtragen("beleg", "kurs_quelle",
                           "TEXT CHECK (kurs_quelle IS NULL OR kurs_quelle IN ('EZB', 'MANUELL'))"),
        _spalte_nachtragen("beleg", "kurs_abruf_utc", "TEXT"),
        _spalte_nachtragen("beleg", "kurs_tabelle_sha256", "TEXT"),
        _spalte_nachtragen("beleg", "betrag_umgerechnet_cent",
                           "INTEGER CHECK (betrag_umgerechnet_cent IS NULL"
                           " OR betrag_umgerechnet_cent > 0)"),
        _spalte_nachtragen("beleg", "betrag_karte_cent",
                           "INTEGER CHECK (betrag_karte_cent IS NULL OR betrag_karte_cent > 0)"),
    ),
    20: (
        # E-Mails als Beleg: die Auslesung aus dem Text einer Mail hat ihre
        # eigene Quelle 'email'. Der CHECK laesst sich per ALTER nicht
        # erweitern, die Tabelle wird neu gebaut (Zeilen bleiben, wie sie sind).
        _extraktion_neu,
    ),
    21: (
        # Zeiterfassung mit Pause: die Arbeitsabschnitte eines Timers. Die
        # Trigger dazu legt schema.sql an.
        _tabelle_anlegen("zeit_abschnitt"),
        # Ein gerade laufender Timer bekommt seinen ersten Abschnitt, sonst
        # liesse er sich nicht pausieren. Abgeschlossene Alteintraege bekommen
        # KEINE: ihr Ende ueber Mitternacht steht nur als Tagesende da, eine
        # Rekonstruktion waere geraten. Sie gelten als ein Abschnitt.
        _laufenden_timer_nachtragen,
        # Die Rundungsregel je Kunde (NULL = wie Betrieb) samt der Stelle, an
        # der sie vereinbart ist. Die neuen Spalten tragen ihren CHECK gleich
        # mit (ALTER TABLE ... ADD COLUMN nimmt ihn fuer die neue Spalte an).
        _spalte_nachtragen("kunde", "rundung_modus",
                           "TEXT CHECK (rundung_modus IS NULL OR rundung_modus IN"
                           " ('AUFWAERTS', 'KAUFMAENNISCH', 'ABWAERTS', 'KEINE'))"),
        _spalte_nachtragen("kunde", "rundung_intervall_min",
                           "INTEGER CHECK (rundung_intervall_min IS NULL"
                           " OR rundung_intervall_min > 0)"),
        _spalte_nachtragen("kunde", "rundung_grundlage", "TEXT"),
        # Die Regel, mit der ein Entwurf rechnet, eingefroren am Entwurf.
        _spalte_nachtragen("rechnung", "rundung_modus",
                           "TEXT CHECK (rundung_modus IS NULL OR rundung_modus IN"
                           " ('AUFWAERTS', 'KAUFMAENNISCH', 'ABWAERTS', 'KEINE'))"),
        _spalte_nachtragen("rechnung", "rundung_intervall_min",
                           "INTEGER CHECK (rundung_intervall_min IS NULL"
                           " OR rundung_intervall_min > 0)"),
        # Bestehende Entwuerfe rechneten bisher mit der Regel des Betriebs --
        # eine andere gab es nicht. Genau die wird eingefroren, damit sich
        # an ihnen nichts aendert. Festgeschriebene und stornierte Rechnungen
        # bleiben NULL: ihre Positionen stehen fest, und der gedruckte
        # Rundungshinweis ist ihr Schnappschuss.
        "UPDATE rechnung SET"
        " rundung_modus = (SELECT rundung_modus FROM betrieb WHERE id = 1),"
        " rundung_intervall_min = (SELECT rundung_intervall_min FROM betrieb WHERE id = 1)"
        " WHERE status = 'ENTWURF' AND rundung_modus IS NULL",
        # Und ihr Rundungssatz wird der zutreffende -- sonst truege ein alter
        # Entwurf beim Festschreiben weiter den ersetzten Satz.
        _rundungshinweis_der_entwuerfe,
        # Titel und Beschreibung einer abgerechneten Zeit sind ab jetzt
        # gesperrt; der Einzelnachweis wird aus ihnen gebaut.
        _trigger_erneuern("zeit_gesperrt"),
    ),
    22: (
        # Rechnungsablauf: Korrigieren, Zuruecknehmen vor dem Versand,
        # Zahlung zuruecknehmen (app/logik/rechnungsablauf.py).
        #
        # Der Verweis der Ersatzrechnung auf die ersetzte und der der
        # Gegenbuchung auf die aufgehobene Zahlung. Beide Spalten sind neu und
        # leer; bestehende Rechnungen und Zahlungen bleiben, wie sie sind.
        # Die Spalten kommen ZUERST: die erneuerten Trigger darunter lesen sie.
        _spalte_nachtragen("rechnung", "ersetzt_rechnung_id",
                           "INTEGER REFERENCES rechnung (id)"),
        _spalte_nachtragen("zahlung", "storno_von_id", "INTEGER REFERENCES zahlung (id)"),
        # Die Freigabe, die nur innerhalb einer Transaktion existiert.
        _tabelle_anlegen("loeschfreigabe"),
        # rechnung_gesperrt sperrt zusaetzlich den Ersatzverweis und kennt die
        # eine Ausnahme mit Freigabe; zahlung_nur_festgeschrieben laesst die
        # Gegenbuchung einer Rueckzahlung zu. Die neuen Trigger (Zahlungen
        # unveraenderlich, Gegenbuchung stimmig, Freigabe nur unversendet)
        # legt schema.sql an -- CREATE TRIGGER IF NOT EXISTS reicht fuer neue.
        _trigger_erneuern("rechnung_gesperrt"),
        _trigger_erneuern("zahlung_nur_festgeschrieben"),
    ),
    23: (
        # Vorauszahlungen je Termin (Finanzamt und Gemeinde) mit dem Stand
        # "gezahlt". Nur eine neue Tabelle aus schema.sql. Umgedeutet wird
        # nichts: der bisherige Summenwert steuerschaetzung.vorauszahlungen_cent
        # bleibt, wo er ist, und zaehlt als "weitere gezahlte Vorauszahlungen
        # ohne Termin" -- das Ergebnis jedes Jahres aendert sich um 0 Cent.
        # Der Trigger kunde_kein_delete_mit_beleg kommt wie jeder neue Trigger
        # aus schema.sql.
        _tabelle_anlegen("steuer_vorauszahlung"),
    ),
    24: (
        # Eingangsbelege: "in der Regel schon bezahlt" als Vorgabe -- an, wie
        # in einer frischen Datenbank. Gebuchte Belege bleiben, wie sie sind:
        # die Vorgabe wirkt erst beim naechsten Buchen, eine Zahlung wird
        # nicht nachtraeglich erfunden. Die neue Spalte traegt ihren CHECK mit.
        _spalte_nachtragen("betrieb", "belege_bezahlt_vorgabe",
                           "INTEGER NOT NULL DEFAULT 1"
                           " CHECK (belege_bezahlt_vorgabe IN (0, 1))"),
        # Ein Eingangsordner je Jahr. Der bisher hinterlegte Ordner bleibt der
        # "Ordner für alle Jahre" -- eine Migration deutet keine Einstellung
        # um; die Belegseite bietet an, ihn als Ordner des Jahres zu fuehren.
        _tabelle_anlegen("belegordner_jahr"),
    ),
    25: (
        # Beleg loeschen = Loeschmarkierung (GoBD Rz. 110, "Loeschmerker"):
        # Zeitpunkt und Grund am Beleg und an einem Anlagegut, das mit ihm
        # geht. Kein Bestand wird umgedeutet -- alle Zeilen bleiben gueltig
        # (NULL). Grundpflicht und Sperren tragen Trigger aus schema.sql, die
        # Auswertungen lesen ueber die Sichten v_beleg_gueltig und
        # v_anlagegut_gueltig (ebenfalls aus schema.sql).
        _spalte_nachtragen("beleg", "geloescht_am", "TEXT"),
        _spalte_nachtragen("beleg", "geloescht_grund", "TEXT"),
        _spalte_nachtragen("anlagegut", "geloescht_am", "TEXT"),
        _spalte_nachtragen("anlagegut", "geloescht_grund", "TEXT"),
        # Der UNIQUE-Index auf der Pruefsumme der Originale haette dieselbe
        # Datei nach dem Loeschen nie wieder buchen lassen. Die Regel "ein
        # Original je GUELTIGEM Beleg" traegt jetzt der Trigger
        # belegdatei_original_einmalig_insert (Begruendung in schema.sql);
        # den gewoehnlichen Index fuer die Suche legt schema.sql an.
        "DROP INDEX IF EXISTS belegdatei_original_einmalig",
    ),
    26: (
        # Import aus WISO Steuer und Einnahmen ohne eigene Rechnung
        # (app/logik/wiso.py, app/logik/einnahmen.py, app/logik/angaben.py).
        # Nur neue Tabellen; kein Bestand wird umgedeutet. Die Schritte legen
        # sie aus schema.sql an (EINE Fassung) und machen die Version im
        # Journal nachvollziehbar. Trigger, Indizes und die Sicht
        # v_einnahme_gueltig kommen wie jede neue aus schema.sql -- auch die
        # Sperre "Beleg nur mit Ausgabenkategorie": sie greift erst beim
        # naechsten Schreiben, ein Altbestand bleibt lesbar.
        _tabelle_anlegen("einnahme"),
        _tabelle_anlegen("einnahmedatei"),
        _tabelle_anlegen("zahler"),
        _tabelle_anlegen("wiso_import"),
        _tabelle_anlegen("wiso_buchung"),
        _tabelle_anlegen("wiso_konto"),
        _tabelle_anlegen("fehlende_angabe"),
    ),
}


def migriere(conn: sqlite3.Connection) -> int:
    """Bringt die Datenbank auf SCHEMA_VERSION. Idempotent.

    Eine leere Datei bekommt das Schema in einem Zug. Eine bestehende laeuft die
    Schritte aus MIGRATIONEN hoch, die ihr noch fehlen.
    """
    # Beides in EINER Abfrage: oeffnet ein zweiter Prozess dieselbe neue Datei
    # gleichzeitig, kann er zwischen zwei getrennten Abfragen das Schema
    # anlegen -- dann saehe diese Verbindung "kein Versionsstand" und danach
    # "Tabellen vorhanden" und hielte die eigene Datenbank fuer eine fremde.
    stand = conn.execute(
        "SELECT COALESCE(SUM(name = 'schema_version'), 0) AS version,"
        " COALESCE(SUM(name NOT LIKE 'sqlite_%'), 0) AS tabellen"
        " FROM sqlite_master WHERE type = 'table'"
    ).fetchone()
    neu_angelegt = stand["version"] == 0

    vorhanden = 0
    if neu_angelegt:
        # Tabellen, aber kein Versionsstand: eine fremde SQLite-Datei. Das
        # Schema darueberzulegen liesse die fremden Tabellen stehen, die
        # Einrichtung gelaenge, und erst die Kundenliste scheiterte mit
        # "no such column". Lieber gar nicht erst anfangen.
        fremde = stand["tabellen"]
        if fremde:
            raise DatenbankFehler(
                f"Die Datei {_datei(conn)} enthält Tabellen, aber keinen Versionsstand dieses "
                "Programms — sie ist keine Datenbank dieses Werkzeugs. Bitte die letzte "
                "Sicherung zurückspielen."
            )
    else:
        vorhanden = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version"
        ).fetchone()["v"]
        if vorhanden > SCHEMA_VERSION:
            # Neuer als der Code: Spalten, die dieser Stand nicht kennt, fielen
            # erst beim Schreiben auf -- als Serverfehler, mitten in der Arbeit.
            raise DatenbankFehler(
                f"Diese Datenbank stammt aus einer neueren Programmversion (Schema "
                f"{vorhanden}, dieses Programm kennt {SCHEMA_VERSION}) — bitte das Programm "
                "aktualisieren."
            )
        if vorhanden < SCHEMA_VERSION:
            # Die Sichten weg, BEVOR die Schritte laufen. schema.sql verwirft
            # und erzeugt sie am Ende ohnehin; waehrend der Migration aber
            # zeigen alte Sichten auf Spalten, die ein Schritt gerade entfernt
            # hat -- und SQLite prueft bei jedem ALTER TABLE ... RENAME alle
            # Sichten mit. Dann scheitert Schritt 14 an einer Sicht aus
            # Version 1, die nichts mit ihm zu tun hat.
            for zeile in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            ).fetchall():
                conn.execute(f'DROP VIEW IF EXISTS "{zeile["name"]}"')
            # Fremdschluessel aus, solange umgebaut wird -- das dokumentierte
            # Verfahren fuer Schemaaenderungen in SQLite: ein DROP TABLE prueft
            # sonst jede Zeile, die noch auf die alte Tabelle zeigt, und bricht
            # ab. Die Pragma wirkt nur AUSSERHALB einer Transaktion, deshalb
            # hier und nicht im Schritt. Vor jedem COMMIT prueft
            # foreign_key_check, dass kein Verweis ins Leere zeigt; am Ende
            # geht die Pruefung wieder an.
            conn.execute("PRAGMA foreign_keys = OFF")
            # Und das Altverhalten von ALTER TABLE ... RENAME: ohne es prueft
            # SQLite beim Umbenennen jede Sicht und jeden Trigger der
            # Datenbank -- mitten im Umbau zeigen die aber auf Tabellen, die
            # gerade nicht da sind. Ebenso wenig sollen Verweise umgeschrieben
            # werden: eine Zwischentabelle, die ihren endgueltigen Namen
            # bekommt, soll von den Verweisen auf diesen Namen getroffen werden.
            conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            for ziel in sorted(MIGRATIONEN):
                if ziel <= vorhanden:
                    continue
                # Jede Anweisung einzeln mit execute(): executescript() wuerde
                # eine offene Transaktion implizit beenden und das
                # Zuruecknehmen unmoeglich machen.
                conn.execute("BEGIN IMMEDIATE")
                # Unter der Sperre noch einmal nachsehen: oeffnet ein zweiter
                # Prozess dieselbe Datei gleichzeitig (Programm doppelt
                # gestartet, ein Werkzeugskript neben der laufenden App), hat
                # er den Schritt womoeglich schon ausgefuehrt. Ein zweites
                # ALTER TABLE ... ADD COLUMN scheiterte sonst am Start.
                erreicht = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM schema_version"
                ).fetchone()["v"]
                if erreicht >= ziel:
                    conn.execute("COMMIT")
                    vorhanden = max(vorhanden, erreicht)
                    continue
                try:
                    for anweisung in MIGRATIONEN[ziel]:
                        if callable(anweisung):
                            anweisung(conn)
                        else:
                            conn.execute(anweisung)
                    verwaist = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if verwaist:
                        tabellen = sorted({z[0] for z in verwaist})
                        raise RuntimeError(
                            f"Migration {ziel} hinterliesse Verweise ins Leere in "
                            f"{', '.join(tabellen)} -- abgebrochen, nichts geaendert."
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_version (version, migriert_am)"
                        " VALUES (?, ?)",
                        (ziel, jetzt_utc()),
                    )
                    protokolliere(conn, "MIGRATION", "schema", ziel,
                                  wert_alt=vorhanden, wert_neu=ziel)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                vorhanden = ziel
        finally:
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute("PRAGMA foreign_keys = ON")

    # Zielzustand anwenden: legt fehlende Tabellen und Trigger an und erneuert
    # die Sichten. Bei einer migrierten Datenbank ist das ein No-op bis auf die
    # Sichten.
    _schema_anwenden(conn)
    if vorhanden < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, migriert_am) VALUES (?, ?)",
            (SCHEMA_VERSION, jetzt_utc()),
        )
    _stammdaten_abgleichen(conn)
    return SCHEMA_VERSION


def _schema_anwenden(conn: sqlite3.Connection) -> None:
    """schema.sql in EINER Transaktion; die PRAGMAs davor, ausserhalb.

    Das Skript verwirft die Sichten und legt sie neu an. Ohne Transaktion
    lief jede Anweisung fuer sich, und zwei Verbindungen, die dieselbe Datei
    gleichzeitig oeffneten, verschraenkten sich: beide verwarfen, die erste
    legte an, die zweite scheiterte mit "view ... already exists" -- und das
    Programm startete nicht. Mit BEGIN IMMEDIATE wartet die zweite (busy
    timeout) und findet danach den fertigen Stand vor.

    ``PRAGMA journal_mode = WAL`` darf nicht in einer Transaktion stehen;
    die PRAGMA-Zeilen laufen deshalb vorher.
    """
    text = SCHEMA_DATEI.read_text(encoding="utf-8")
    pragmas = [z for z in text.splitlines() if z.lstrip().upper().startswith("PRAGMA")]
    rumpf = "\n".join(
        z for z in text.splitlines() if not z.lstrip().upper().startswith("PRAGMA")
    )
    conn.executescript("\n".join(pragmas))
    try:
        conn.executescript(f"BEGIN IMMEDIATE;\n{rumpf}\nCOMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _stammdaten_abgleichen(conn: sqlite3.Connection) -> None:
    """Bringt den Kategoriekatalog auf den Stand der mitgelieferten Datei.

    Laeuft bei jedem Start und ist dabei still, solange sich nichts aendert.
    Der Katalog ist versionierte Referenzdatei und gehoert damit zum Schema:
    lebte er nur in der Datenbank, muesste ihn jeder neue Datenbestand von Hand
    nachbauen; lebte er nur in der Datei, waere keine Kategorie referenzierbar.

    Der Import darf den Start nicht verhindern. Eine beschaedigte
    Stammdatendatei ist aergerlich, aber kein Grund, den Zugriff auf die
    Buchfuehrung zu sperren -- die Meldung landet im Journal.
    """
    from app.logik import kategorien

    conn.execute("BEGIN IMMEDIATE")
    try:
        ergebnis = kategorien.abgleichen(conn)
        if ergebnis.angelegt or ergebnis.aktualisiert:
            protokolliere(
                conn, "STAMMDATEN", "euer_kategorie", None,
                wert_neu={"angelegt": ergebnis.angelegt,
                          "aktualisiert": ergebnis.aktualisiert},
            )
        conn.execute("COMMIT")
    except Exception as fehler:  # noqa: BLE001 -- der Start hat Vorrang
        conn.execute("ROLLBACK")
        conn.execute("BEGIN IMMEDIATE")
        protokolliere(conn, "STAMMDATEN_FEHLER", "euer_kategorie", None,
                      begruendung=str(fehler))
        conn.execute("COMMIT")


def oeffne(pfad: Path | str | None = None) -> sqlite3.Connection:
    """Verbindet und migriert in einem Schritt."""
    conn = verbinde(pfad)
    migriere(conn)
    return conn


def protokolliere(
    conn: sqlite3.Connection,
    aktion: str,
    objekttyp: str,
    objekt_id: int | None = None,
    *,
    feld: str | None = None,
    wert_alt: object = None,
    wert_neu: object = None,
    begruendung: object = None,
    hash_: str | None = None,
) -> None:
    """Schreibt einen Eintrag ins append-only Journal.

    § 146 Abs. 4 AO verlangt, dass Aenderungen den urspruenglichen Inhalt
    feststellbar lassen. Das Journal ist per Trigger gegen UPDATE und DELETE
    gesperrt; es kann also nur wachsen.
    """

    def _text(wert: object) -> str | None:
        if wert is None or isinstance(wert, str):
            return wert
        return json.dumps(wert, ensure_ascii=False, sort_keys=True)

    conn.execute(
        """
        INSERT INTO journal (zeitpunkt_utc, aktion, objekttyp, objekt_id,
                             feld, wert_alt, wert_neu, begruendung, hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            jetzt_utc(),
            aktion,
            objekttyp,
            objekt_id,
            feld,
            _text(wert_alt),
            _text(wert_neu),
            _text(begruendung),
            hash_,
        ),
    )

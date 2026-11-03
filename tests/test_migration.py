"""Migration einer bestehenden Datenbank.

schema.sql beschreibt den Zielzustand und legt neue Datenbanken in einem Zug an.
Eine bestehende Datenbank bekommt davon nichts mit: CREATE TABLE IF NOT EXISTS
fuegt keine Spalte hinzu und benennt keine um. Genau diese Luecke pruefen die
Tests hier -- sie ist der Fehlertyp, der erst auffaellt, wenn jemand mit echten
Daten arbeitet.
"""

import pathlib
import sqlite3

import pytest

from app import db

# Das ECHTE Schema in Version 1, aus dem Commit gezogen (tests/fixtures/schema_v1.sql).
# Ein nachgebautes Schema wuerde genau die Luecken nicht finden, auf die es hier
# ankommt -- etwa eine Spalte, die eine Sicht braucht und die Migration nicht anlegt.
V1 = (pathlib.Path(__file__).parent / "fixtures" / "schema_v1.sql").read_text(encoding="utf-8")


@pytest.fixture
def alte_db(tmp_path):
    """Eine Datenbank in Version 1 mit Altdaten in allen drei Grenzfaellen."""
    pfad = tmp_path / "alt.sqlite"
    conn = sqlite3.connect(pfad, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(V1)
    conn.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, standardsatz_cent)"
        " VALUES (1, 'Alt', 'Weg 1', '20095', 'Hamburg', '12/345/67890', 8500)"
    )
    conn.execute(
        "INSERT INTO kunde (id, kundennummer, name, strasse, plz, ort, satz_cent, angelegt_am)"
        " VALUES (1, 'K-0001', 'Mustermann GmbH', 'Hauptstr. 1', '10115', 'Berlin',"
        " 9000, '2026-01-01')"
    )
    conn.execute("INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00+00:00')")
    # Eine festgeschriebene Rechnung mit Position, Zeitzuordnung und Zahlung.
    # Ohne sie waren alle Migrationstests gruen -- und in einer migrierten
    # Datenbank war trotzdem kein Festschreiben moeglich.
    conn.execute(
        "INSERT INTO zeiteintrag (id, kunde_id, titel, datum, dauer_minuten, abrechnungsart,"
        " abrechenbar, erfasst_am) VALUES (99, 1, 'Abgerechnet', '2026-01-10', 60, 'STUNDEN',"
        " 1, '2026-01-10T10:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO rechnung (id, typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,"
        " netto_cent, brutto_cent, status, festgeschrieben_am, erstellt_am)"
        " VALUES (1, 'RECHNUNG', 'RE-2026-0001', 1, 2026, 1, '2026-01-20', 9000, 9000,"
        " 'FESTGESCHRIEBEN', '2026-01-20T10:00:00+00:00', '2026-01-20T10:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO rechnungsposition (id, rechnung_id, pos_nr, bezeichnung, menge_tausendstel,"
        " einheit, einzelpreis_cent, betrag_cent, quelle)"
        " VALUES (1, 1, 1, 'Abgerechnet', 1000, 'HUR', 9000, 9000, 'ZEIT')"
    )
    conn.execute("INSERT INTO position_zeiteintrag (position_id, zeiteintrag_id) VALUES (1, 99)")
    conn.execute("UPDATE zeiteintrag SET rechnung_id = 1 WHERE id = 99")
    conn.execute(
        "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (1, '2026-02-01', 9000)"
    )
    conn.execute(
        "INSERT INTO nummernkreis (typ, jahr, format, naechste_seq) VALUES"
        " ('RECHNUNG', 2026, 'RE-{JJJJ}-{NNNN}', 2)"
    )
    for titel, art, abrechenbar, erledigt in (
        ("Normale Stunde", "STUNDEN", 1, None),
        ("Akquise", "STUNDEN", 0, None),
        ("Kulanz ohne Satz", "OHNE_BERECHNUNG", 1, None),
        ("Erledigt ohne Rechnung", "STUNDEN", 1, "2026-02-01"),
    ):
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsart,"
            " abrechenbar, erledigt_am, erledigt_grund, erfasst_am)"
            " VALUES (1, ?, '2026-01-15', 60, ?, ?, ?, ?, '2026-01-15T10:00:00+00:00')",
            (titel, art, abrechenbar, erledigt, "Altgrund" if erledigt else None),
        )
    conn.commit()
    conn.close()
    return pfad


def test_alte_datenbank_wird_migriert(alte_db):
    conn = db.oeffne(alte_db)
    try:
        assert conn.execute(
            "SELECT MAX(version) v FROM schema_version"
        ).fetchone()["v"] == db.SCHEMA_VERSION
        spalten = {
            z["name"] for z in conn.execute("PRAGMA table_info(zeiteintrag)")
        }
        assert "abrechnungsmodus" in spalten
        assert "abgeschrieben_am" in spalten
        assert "abrechenbar" not in spalten
        assert "abrechnungsart" not in spalten
        assert "erledigt_am" not in spalten
        assert "position_id" not in spalten
    finally:
        conn.close()


def test_altdaten_landen_im_richtigen_modus(alte_db):
    conn = db.oeffne(alte_db)
    try:
        nach_titel = {
            z["titel"]: z for z in conn.execute("SELECT * FROM zeiteintrag")
        }
        assert nach_titel["Normale Stunde"]["abrechnungsmodus"] == "STUNDEN"
        # abrechenbar = 0 bedeutete "erscheint nie auf einer Rechnung"
        assert nach_titel["Akquise"]["abrechnungsmodus"] == "INTERN"
        # OHNE_BERECHNUNG bedeutete "erscheint mit 0,00 € auf der Rechnung"
        assert nach_titel["Kulanz ohne Satz"]["abrechnungsmodus"] == "KULANZ"
        assert nach_titel["Erledigt ohne Rechnung"]["abgeschrieben_am"] == "2026-02-01"
        assert nach_titel["Erledigt ohne Rechnung"]["abgeschrieben_grund"] == "Altgrund"
    finally:
        conn.close()


def test_kulanz_ohne_satz_bekommt_den_betriebsstandard(alte_db):
    """KULANZ verlangt einen eingefrorenen Satz. Fehlt er in den Altdaten, ist
    der heutige Betriebsstandard die einzige verfuegbare Zahl."""
    conn = db.oeffne(alte_db)
    try:
        satz = conn.execute(
            "SELECT satz_cent_snapshot FROM zeiteintrag WHERE titel = 'Kulanz ohne Satz'"
        ).fetchone()[0]
        assert satz == 8500
    finally:
        conn.close()


def test_neue_spalten_und_sichten_sind_da(alte_db):
    conn = db.oeffne(alte_db)
    try:
        assert "version" in {z["name"] for z in conn.execute("PRAGMA table_info(rechnung)")}
        betriebsspalten = {z["name"] for z in conn.execute("PRAGMA table_info(betrieb)")}
        assert "nullrechnung_zulassen" in betriebsspalten
        assert "leistungszeitraum_default" in betriebsspalten
        assert "leistungszeitraum_zeigen" in {
            z["name"] for z in conn.execute("PRAGMA table_info(rechnung)")
        }
        sichten = {
            z["name"] for z in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
        }
        assert sichten == {
            "v_rechnung_status", "v_zeiteintrag_status", "v_offene_posten", "v_verzicht",
            "v_beleg_gueltig", "v_anlagegut_gueltig", "v_einnahme_gueltig",
        }
        # Die alte Sicht las auf z.abrechenbar. Waere sie stehen geblieben,
        # schlaege diese Abfrage fehl.
        conn.execute("SELECT status FROM v_zeiteintrag_status").fetchall()
    finally:
        conn.close()


def test_jeder_schritt_wird_protokolliert(alte_db):
    """Je Schritt eine Zeile, damit nachvollziehbar bleibt, wann was lief."""
    conn = db.oeffne(alte_db)
    try:
        zeilen = list(conn.execute(
            "SELECT * FROM journal WHERE aktion = 'MIGRATION' ORDER BY objekt_id"
        ))
        gelaufen = [z["objekt_id"] for z in zeilen]
        assert gelaufen == [z for z in sorted(db.MIGRATIONEN) if z > 1]
        # Die erreichte Version wird gesondert geprueft und nicht am letzten
        # Schritt: eine Version, die nur neue Tabellen bringt, braucht keinen
        # Schritt -- schema.sql legt sie mit CREATE ... IF NOT EXISTS an.
        assert conn.execute(
            "SELECT MAX(version) v FROM schema_version"
        ).fetchone()["v"] == db.SCHEMA_VERSION
        assert max(db.MIGRATIONEN) <= db.SCHEMA_VERSION
    finally:
        conn.close()


def test_zweiter_start_macht_nichts_mehr(alte_db):
    conn = db.oeffne(alte_db)
    erste = conn.execute(
        "SELECT COUNT(*) c FROM journal WHERE aktion = 'MIGRATION'"
    ).fetchone()["c"]
    conn.close()
    conn = db.oeffne(alte_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM journal WHERE aktion = 'MIGRATION'"
        ).fetchone()["c"] == erste
        # Vier Grenzfaelle plus die abgerechnete Stunde der Fixture.
        assert conn.execute("SELECT COUNT(*) c FROM zeiteintrag").fetchone()["c"] == 5
    finally:
        conn.close()


def test_schritt_5_macht_leistungstitel_eindeutig(alte_db):
    """Die Eindeutigkeit kommt auch in eine bestehende Datenbank.

    Ohne diesen Schritt haette eine Altdatenbank weiter 'Beratung' und
    'beratung' nebeneinander erlaubt -- und genau das macht die Gruppierung
    "pro Titel" wertlos.
    """
    conn = db.oeffne(alte_db)
    try:
        conn.execute("INSERT INTO leistung (titel) VALUES ('Beratung')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO leistung (titel) VALUES ('beratung')")
    finally:
        conn.close()


class TestPfadeRelativ:
    """Schema 13: Dateipfade relativ zum Datenordner.

    Bis dahin standen Belege und Rechnungs-PDF absolut in der Datenbank. Nach
    einer Wiederherstellung an anderer Stelle zeigten alle ins Leere -- und
    belegdatei.pfad war per Trigger nicht einmal reparierbar.
    """

    def _db_mit_absoluten_pfaden(self, ordner):
        ordner.mkdir(parents=True)
        conn = db.oeffne(ordner / "steuer.sqlite")
        kunde = conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
            " VALUES ('001', 'K', 'Weg 1', '10115', 'Berlin', ?)", (db.heute(),),
        ).lastrowid
        conn.execute(
            "INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,"
            " netto_cent, brutto_cent, status, pdf_pfad, festgeschrieben_am, erstellt_am)"
            " VALUES ('RECHNUNG', 'R1', 1, 2026, ?, '2026-03-01', 100, 100, 'FESTGESCHRIEBEN',"
            " ?, ?, ?)",
            (kunde, str(ordner / "ausgangsrechnungen" / "2026" / "R1.pdf"),
             db.jetzt_utc(), db.jetzt_utc()),
        )
        beleg = conn.execute(
            "INSERT INTO beleg (art, belegdatum, betrag_brutto_cent, aufteilungsquote,"
            " betrag_betrieblich_cent, erfasst_am)"
            " VALUES ('EINGANG', '2026-03-14', 829, 100, 829, ?)", (db.jetzt_utc(),),
        ).lastrowid
        conn.execute("DROP TRIGGER IF EXISTS belegdatei_unveraenderlich")
        for sha, pfad in (("a" * 64, ordner / "belege" / "2026" / "x.pdf"),
                          ("b" * 64, pathlib.Path("/woanders") / "y.pdf")):
            conn.execute(
                "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse,"
                " rolle, importiert_am) VALUES (?, ?, 'x.pdf', ?, 1, 'ORIGINAL', ?)",
                (beleg, str(pfad), sha, db.jetzt_utc()),
            )
        # Zurueck auf Stand 12, als waere die Datenbank von vorher. Die Tabelle
        # traegt nur den aktuellen Stand -- sie leer zu machen hiesse Stand 0,
        # und dann liefe jede Migration seit Version 2 noch einmal.
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version VALUES (12, '2026-01-01T00:00:00+00:00')")
        conn.close()
        return ordner / "steuer.sqlite"

    def test_migration_macht_pfade_unter_dem_datenordner_relativ(self, tmp_path):
        pfad = self._db_mit_absoluten_pfaden(tmp_path / "Daten")
        conn = db.oeffne(pfad)
        try:
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
                == db.SCHEMA_VERSION
            belege = {z["sha256"][0]: z["pfad"] for z in conn.execute("SELECT * FROM belegdatei")}
            assert belege["a"] == "belege/2026/x.pdf"
            assert belege["b"] == "/woanders/y.pdf", "ausserhalb bleibt absolut"
            assert conn.execute("SELECT pdf_pfad FROM rechnung").fetchone()[0] \
                == "ausgangsrechnungen/2026/R1.pdf"
            # Der Trigger steht danach wieder.
            with pytest.raises(sqlite3.IntegrityError, match="unveraenderlich"):
                conn.execute("UPDATE belegdatei SET pfad = 'anders' WHERE sha256 = ?", ("a" * 64,))
        finally:
            conn.close()


class TestMigrierteDatenbankIstBenutzbar:
    """Der Massstab, an dem die Migration zu messen ist: in der migrierten
    Datenbank muss sich eine Rechnung festschreiben, stornieren und die Zeit
    danach erneut abrechnen lassen. Ohne Rechnung in der Fixture war das nie
    geprueft -- und ging nicht."""

    def test_rechnungsposition_hat_alle_spalten(self, alte_db):
        conn = db.oeffne(alte_db)
        spalten = {z["name"] for z in conn.execute("PRAGMA table_info(rechnungsposition)")}
        conn.close()
        assert {"nachlass_cent", "nachlass_grund", "darstellung"} <= spalten

    def test_position_zeiteintrag_zeigt_auf_zeiteintrag_und_ist_nicht_unique(self, alte_db):
        conn = db.oeffne(alte_db)
        fk = {z["from"]: z["table"] for z in conn.execute(
            "PRAGMA foreign_key_list(position_zeiteintrag)")}
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'position_zeiteintrag'"
                           ).fetchone()["sql"]
        altdaten = conn.execute("SELECT * FROM position_zeiteintrag").fetchall()
        conn.close()
        assert fk["zeiteintrag_id"] == "zeiteintrag", "nicht mehr zeiteintrag_alt"
        assert "UNIQUE" not in sql.upper()
        assert [(z["position_id"], z["zeiteintrag_id"]) for z in altdaten] == [(1, 99)]

    def test_festschreiben_storno_und_neuabrechnung_gehen(self, alte_db, tmp_path):
        from app.logik import festschreiben as fs

        conn = db.oeffne(alte_db)
        try:
            conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
            frei = conn.execute(
                "SELECT id FROM zeiteintrag WHERE titel = 'Normale Stunde'").fetchone()["id"]
            rid = fs.entwurf_anlegen(conn, 1, [frei])
            ergebnis = fs.festschreiben(conn, rid, belegordner=tmp_path)
            assert ergebnis.nummer == "RE-2026-0002"
            fs.storniere(conn, rid, "Probe", belegordner=tmp_path)
            # Dieselbe Zeit noch einmal: scheiterte vorher am UNIQUE.
            neu = fs.entwurf_anlegen(conn, 1, [frei])
            assert fs.festschreiben(conn, neu, belegordner=tmp_path).nummer == "RE-2026-0004"
            # Und die alte Zuordnung aus Version 1 ist noch da.
            assert conn.execute(
                "SELECT COUNT(*) FROM position_zeiteintrag WHERE zeiteintrag_id = 99"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_schema_entspricht_einer_frischen_datenbank(self, alte_db, tmp_path):
        """Spalten und Fremdschluessel -- alles, was PRAGMA sieht."""
        migriert = db.oeffne(alte_db)
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        try:
            def tabellen(conn):
                return [z["name"] for z in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%' ORDER BY name")]

            def spalten(conn, tabelle):
                return {z["name"] for z in conn.execute(f"PRAGMA table_info({tabelle})")}

            def fremdschluessel(conn, tabelle):
                return {(z["from"], z["table"])
                        for z in conn.execute(f"PRAGMA foreign_key_list({tabelle})")}

            assert tabellen(migriert) == tabellen(frisch)
            for tabelle in tabellen(frisch):
                assert spalten(migriert, tabelle) == spalten(frisch, tabelle), tabelle
                assert fremdschluessel(migriert, tabelle) == fremdschluessel(frisch, tabelle), \
                    tabelle
        finally:
            migriert.close()
            frisch.close()

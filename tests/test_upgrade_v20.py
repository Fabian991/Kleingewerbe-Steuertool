"""Upgrade der echten Lage: eine Datenbank im Stand 20 mit Daten -> heutiger Stand.

Der Nutzer arbeitet mit einer Datenbank auf Schema 20, in der echte Daten
stehen. Die sechs Wunschpakete bringen die Migrationen 21 bis 25. Dieser Test
baut einen Bestand im Stand 20 (``tests/altbestand_v20.py``: Kunden, Zeiten
mit laufendem Timer, festgeschriebene, offene und Entwurfsrechnung, Zahlungen,
Belege in Euro und Dollar, Anlagegut mit AfA, Steuerrücklage mit gezahlten
Vorauszahlungen, hinterlegter Eingangsordner), oeffnet ihn mit dem heutigen
Code und prueft:

- alle Migrationen laufen in einem Zug, jede steht im Journal;
- kein Datensatz geht verloren oder aendert sich;
- EUeR, offene Posten, § 19-Umsatz und Steuerruecklage ergeben auf den Cent
  dasselbe wie vorher -- die Referenzwerte sind mit dem Code von Version 20
  auf genau diesem Bestand berechnet;
- das migrierte Schema gleicht dem einer frischen Datenbank;
- die neuen Funktionen arbeiten auf dem migrierten Bestand.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import db
from app.logik import euer, grenzwert, steuerschaetzung, zeiten
from tests import altbestand_v20

# Mit dem Code von Version 20 (Commit acfba75) auf dem Bestand aus
# altbestand_v20.baue berechnet.
REFERENZ = {
    "einnahmen_cent": 463500,
    "ausgaben_cent": 36880,
    "gewinn_cent": 426620,
    "einnahmen": [(111, 463500, False)],
    "ausgaben": [(228, 11900, False), (228, 1842, False), (130, 23138, False)],
    "jahre": [2029, 2028, 2027, 2026],
    "offene_posten": [(3, "RE-2026-0002", 12000, 0, 12000, "OFFEN")],
    "status": [(1, "FESTGESCHRIEBEN", "BEZAHLT", 0), (2, "ENTWURF", "OFFEN", 9000),
               (3, "FESTGESCHRIEBEN", "OFFEN", 12000), (4, "FESTGESCHRIEBEN", "BEZAHLT", 0)],
    "zeitstatus": [(1, "BEZAHLT"), (2, "BEZAHLT"), (3, "IN_RECHNUNG_GESTELLT"),
                   (4, "IM_ENTWURF"), (5, "AUFGESCHRIEBEN"), (6, "AUFGESCHRIEBEN")],
    "grenzwert_cent": 463500,
    # mehrsteuer, ruecklage, offen (mindestens zuruecklegen), zurueck
    "steuer": (134506, 135000, 15000, 0),
    "buchungen": 6,
}

HEUTE = date(2026, 10, 27)

#: Tabellen mit Daten im Altbestand -- jede Zeile muss es danach noch geben,
#: mit denselben Werten in den alten Spalten.
TABELLEN = ("betrieb", "kunde", "zeiteintrag", "rechnung", "rechnungsposition",
            "position_zeiteintrag", "zahlung", "lieferant", "beleg", "belegdatei",
            "anlagegut", "anlagegut_afa_jahr", "steuerschaetzung", "nummernkreis")


@pytest.fixture
def altbestand(tmp_path) -> Path:
    pfad = tmp_path / "steuer.sqlite"
    altbestand_v20.baue(pfad, tmp_path / "Eingang")
    return pfad


def _zeilen(pfad: Path, tabelle: str) -> tuple[list[str], list[tuple]]:
    """Spalten und Zeilen einer Tabelle, direkt mit sqlite3 gelesen."""
    conn = sqlite3.connect(pfad)
    try:
        cur = conn.execute(f"SELECT * FROM {tabelle} ORDER BY rowid")
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        conn.close()


def _schema(conn: sqlite3.Connection) -> dict[str, object]:
    """Was eine Datenbank ausmacht: Spalten je Tabelle, Trigger, Indizes, Sichten."""
    def norm(sql: str | None) -> str:
        return re.sub(r"\s+", " ", sql or "").strip()

    tabellen = {
        z["name"]: {(s["name"], s["type"].upper(), s["notnull"], s["pk"])
                    for s in conn.execute(f"PRAGMA table_info({z['name']})")}
        for z in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                              " AND name NOT LIKE 'sqlite_%'")
    }
    andere = {
        (z["type"], z["name"]): norm(z["sql"])
        for z in conn.execute("SELECT type, name, sql FROM sqlite_master"
                              " WHERE type IN ('trigger', 'view', 'index')"
                              " AND name NOT LIKE 'sqlite_%'")
    }
    return {"tabellen": tabellen, "andere": andere}


def test_alle_migrationen_in_einem_zug(altbestand):
    conn = db.oeffne(altbestand)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
            == db.SCHEMA_VERSION
        migriert = [z[0] for z in conn.execute(
            "SELECT objekt_id FROM journal WHERE aktion = 'MIGRATION' ORDER BY id")]
        assert migriert == list(range(21, db.SCHEMA_VERSION + 1))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    # Ein zweites Oeffnen aendert nichts mehr.
    conn = db.oeffne(altbestand)
    try:
        assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                            ).fetchone()[0] == db.SCHEMA_VERSION - 20
    finally:
        conn.close()


#: Was die Migration an alten Spalten bewusst aendert: der Rundungssatz der
#: ENTWUERFE (Version 21, siehe db._rundungshinweis_der_entwuerfe).
GEAENDERT = {("rechnung", "rundung_hinweis")}


def test_kein_datensatz_geht_verloren(altbestand):
    vorher = {t: _zeilen(altbestand, t) for t in TABELLEN}
    db.oeffne(altbestand).close()
    for tabelle, (spalten, zeilen) in vorher.items():
        neu_spalten, neu_zeilen = _zeilen(altbestand, tabelle)
        assert len(neu_zeilen) == len(zeilen), tabelle
        gleich = [s for s in spalten if (tabelle, s) not in GEAENDERT]
        alt_index = [spalten.index(s) for s in gleich]
        neu_index = [neu_spalten.index(s) for s in gleich]
        assert [tuple(z[i] for i in neu_index) for z in neu_zeilen] \
            == [tuple(z[i] for i in alt_index) for z in zeilen], tabelle


def test_rundungssatz_nur_im_entwurf_erneuert(altbestand):
    """Festgeschriebene Rechnungen behalten ihren gedruckten Satz; der
    Entwurf bekommt den zutreffenden, den auch das Neuberechnen schriebe --
    sonst kaeme der alte Satz beim Festschreiben auf die Rechnung."""
    db.oeffne(altbestand).close()
    conn = sqlite3.connect(altbestand)
    try:
        assert dict(conn.execute("SELECT id, rundung_hinweis FROM rechnung")) == {
            1: "Abrechnung in angefangenen 15 Minuten.",
            2: "Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet.",
            3: "Abrechnung in angefangenen 15 Minuten.",
            4: "Abrechnung in angefangenen 15 Minuten.",
        }
    finally:
        conn.close()


def test_ergebnisse_auf_den_cent_wie_vorher(altbestand):
    conn = db.oeffne(altbestand)
    try:
        a = euer.auswertung(conn, altbestand_v20.JAHR)
        assert (a.einnahmen_cent, a.ausgaben_cent, a.gewinn_cent) == (
            REFERENZ["einnahmen_cent"], REFERENZ["ausgaben_cent"], REFERENZ["gewinn_cent"])
        assert [(z.kennzahl, z.betrag_cent, z.nachrichtlich) for z in a.einnahmen] \
            == REFERENZ["einnahmen"]
        assert [(z.kennzahl, z.betrag_cent, z.nachrichtlich) for z in a.ausgaben] \
            == REFERENZ["ausgaben"]
        assert euer.jahre(conn) == REFERENZ["jahre"]
        assert len(euer.buchungen(conn, altbestand_v20.JAHR)) == REFERENZ["buchungen"]
        assert [tuple(z) for z in conn.execute(
            "SELECT id, nummer, brutto_cent, bezahlt_cent, offen_cent, zahlstatus"
            " FROM v_offene_posten ORDER BY id")] == REFERENZ["offene_posten"]
        assert [tuple(z) for z in conn.execute(
            "SELECT id, dokumentstatus, zahlstatus, offen_cent FROM v_rechnung_status"
            " ORDER BY id")] == REFERENZ["status"]
        assert [tuple(z) for z in conn.execute(
            "SELECT id, status FROM v_zeiteintrag_status ORDER BY id")] == REFERENZ["zeitstatus"]
        assert grenzwert.vereinnahmt_cent(conn, altbestand_v20.JAHR) == REFERENZ["grenzwert_cent"]
        e = steuerschaetzung.berechne(conn, altbestand_v20.JAHR, heute=HEUTE)
        assert (e.mehrsteuer_cent, e.ruecklage_cent, e.offen_cent, e.zurueck_cent) \
            == REFERENZ["steuer"]
        # Der Altwert der Vorauszahlungen bleibt "weitere ohne Termin".
        assert e.eingaben.vorauszahlungen_cent == 120000
        assert conn.execute("SELECT COUNT(*) FROM steuer_vorauszahlung").fetchone()[0] == 0
    finally:
        conn.close()


def test_schema_gleicht_einer_frischen_datenbank(altbestand, tmp_path):
    migriert = db.oeffne(altbestand)
    frisch = db.oeffne(tmp_path / "frisch.sqlite")
    try:
        alt, neu = _schema(migriert), _schema(frisch)
        assert alt["tabellen"].keys() == neu["tabellen"].keys()
        for tabelle, spalten in neu["tabellen"].items():
            assert alt["tabellen"][tabelle] == spalten, tabelle
        assert alt["andere"] == neu["andere"]
    finally:
        migriert.close()
        frisch.close()


def test_laufender_timer_und_entwurf_nach_dem_upgrade(altbestand):
    conn = db.oeffne(altbestand)
    try:
        # Der laufende Timer hat seinen ersten Abschnitt, abgeschlossene
        # Alteintraege keinen.
        assert [tuple(z) for z in conn.execute(
            "SELECT zeiteintrag_id, beginn_utc, ende_utc FROM zeit_abschnitt")] == [
            (6, zeiten.ortszeit_als_utc(altbestand_v20.TIMER_DATUM,
                                        altbestand_v20.TIMER_START), None)]
        # Der Entwurf rechnet mit der Regel, die er hatte (Betrieb: 15 Min.
        # aufwaerts); festgeschriebene Rechnungen bleiben ohne.
        assert [tuple(z) for z in conn.execute(
            "SELECT id, rundung_modus, rundung_intervall_min FROM rechnung ORDER BY id")] == [
            (1, None, None), (2, "AUFWAERTS", 15), (3, None, None), (4, None, None)]
        # Der Eingangsordner bleibt "fuer alle Jahre", bezahlt ist Vorgabe.
        betrieb = conn.execute("SELECT * FROM betrieb").fetchone()
        assert betrieb["belegordner_pfad"].endswith("Eingang")
        assert betrieb["belege_bezahlt_vorgabe"] == 1
        assert conn.execute("SELECT COUNT(*) FROM belegordner_jahr").fetchone()[0] == 0
        # Kein Beleg ist geloescht, kein Zahlungsverweis gesetzt.
        assert conn.execute("SELECT COUNT(*) FROM beleg WHERE geloescht_am IS NOT NULL"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM v_beleg_gueltig").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM zahlung WHERE storno_von_id IS NOT NULL"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rechnung WHERE ersetzt_rechnung_id"
                            " IS NOT NULL").fetchone()[0] == 0
    finally:
        conn.close()


# Die Seiten, die der Nutzer nach dem Update als Erstes oeffnet -- keine darf
# am migrierten Bestand scheitern.
SEITEN = ("/", "/zeiten", "/rechnungen", "/rechnungen/luecken", "/rechnung/1", "/rechnung/2",
          "/rechnung/3", "/rechnung/2/vorschau.pdf", "/belege", "/belege?geloescht=1",
          "/belege?offen=1", "/kunden", "/kunden/1", "/kunden/2", "/steuer?jahr=2026",
          "/euer?jahr=2026", "/anlagen", "/journal", "/backup/excel")


def test_seiten_nach_dem_upgrade(altbestand):
    from fastapi.testclient import TestClient

    from app.main import erzeuge_app

    with TestClient(erzeuge_app(altbestand)) as klient:
        for pfad in SEITEN:
            antwort = klient.get(pfad, follow_redirects=False)
            assert antwort.status_code == 200, (pfad, antwort.status_code)
        # Der laufende Timer aus Version 20 laesst sich pausieren.
        antwort = klient.post("/zeiten/timer/pause", data={}, follow_redirects=False)
        assert antwort.status_code == 303
    conn = db.oeffne(altbestand)
    try:
        assert conn.execute("SELECT COUNT(*) FROM zeit_abschnitt WHERE ende_utc IS NULL"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'TIMER_PAUSE'"
                            ).fetchone()[0] == 1
    finally:
        conn.close()


def test_neue_funktionen_auf_dem_altbestand(altbestand, tmp_path, monkeypatch):
    """Korrigieren, Zuruecknehmen, Zahlung zuruecknehmen, Beleg loeschen und
    der Excel-Export arbeiten auf Rechnungen und Belegen aus Version 20 -- und
    die EUeR aendert sich um genau das, was die Handlung bedeutet."""
    import json

    from app import pdf
    from app.logik import export_excel
    from app.logik import festschreiben as fs
    from app.logik import rechnungsablauf as ablauf
    from app.logik.beleg import loeschen

    def rendere(daten: dict, ziel: Path) -> str:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"%PDF-1.7\n" + json.dumps(daten, ensure_ascii=False,
                                                     default=str).encode())
        return pdf.pruefsumme(ziel)

    monkeypatch.setattr(pdf, "rendere", rendere)
    conn = db.oeffne(altbestand)
    try:
        jahr = altbestand_v20.JAHR
        # Korrigieren einer bezahlten Altrechnung: Storno plus Ersatzentwurf;
        # die Zahlung bleibt am Original, die Einnahmen also auch.
        k = ablauf.korrigieren(conn, 1, "falscher Betreff", belegordner=tmp_path)
        entwurf = conn.execute("SELECT * FROM rechnung WHERE id = ?",
                               (k.entwurf_id,)).fetchone()
        assert (entwurf["status"], entwurf["ersetzt_rechnung_id"]) == ("ENTWURF", 1)
        # Die Altrechnung hatte keine eingefrorene Regel: der Entwurf bekommt
        # die damalige (die des Betriebs) eingefroren -- mehr dazu in
        # test_altrechnung_wird_wieder_entwurf.
        assert (entwurf["rundung_modus"], entwurf["rundung_intervall_min"]) == ("AUFWAERTS", 15)
        regel = fs.regel_des_entwurfs(conn, entwurf)
        assert (regel.modus, regel.intervall_min) == ("AUFWAERTS", 15)
        assert euer.auswertung(conn, jahr).einnahmen_cent == REFERENZ["einnahmen_cent"]

        # Zuruecknehmen der offenen, nie versendeten RE-2026-0002: danach kam
        # schon RE-2026-0003 -- die Nummer bleibt als Luecke stehen.
        r = ablauf.zuruecknehmen(conn, 3, "Testrechnung", bestaetigt=True,
                                 datenordner=tmp_path)
        assert (r.nummer, r.nummer_frei) == ("RE-2026-0002", False)
        assert [x.nummer for x in ablauf.nummernluecken(conn, jahr)] == ["RE-2026-0002"]
        assert conn.execute("SELECT COUNT(*) FROM v_offene_posten").fetchone()[0] == 0

        # Eine der beiden Raten von RE-2026-0003 war ein Fehler.
        zahlung = conn.execute("SELECT id FROM zahlung WHERE rechnung_id = 4"
                               " AND datum = '2026-08-01'").fetchone()[0]
        ablauf.zahlung_zuruecknehmen(conn, 4, zahlung, "doppelt erfasst")
        a = euer.auswertung(conn, jahr)
        assert a.einnahmen_cent == REFERENZ["einnahmen_cent"] - 250000
        assert grenzwert.vereinnahmt_cent(conn, jahr) == REFERENZ["grenzwert_cent"] - 250000

        # Der Hosting-Beleg war ein Test: geloescht zaehlt er nicht mehr.
        loeschen.loeschen(conn, 1, grund="Testbeleg")
        assert euer.auswertung(conn, jahr).ausgaben_cent == REFERENZ["ausgaben_cent"] - 11900

        assert export_excel.als_bytes(conn)[:2] == b"PK"
    finally:
        conn.close()


#: Der Satz, den Version 20 auf jede Rechnung druckte -- er beschrieb eine
#: Zeittaktklausel je Taetigkeit, die nie angewandt wurde.
ALTER_SATZ = "Abrechnung in angefangenen 15 Minuten."
NEUER_SATZ = "Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet."


def test_altrechnung_wird_wieder_entwurf(altbestand, tmp_path, monkeypatch):
    """Eine festgeschriebene Rechnung aus Version 20 wird ueber Korrigieren
    oder "Zurück zum Entwurf" wieder ein Entwurf. Der Entwurf traegt danach
    den zutreffenden Rundungssatz und die damalige Regel eingefroren: der
    ersetzte Satz kommt nicht auf eine heute ausgestellte Rechnung, und eine
    Neuberechnung (etwa nach dem Aendern eines Beschreibungstexts) rechnet
    nicht still mit der inzwischen geaenderten Regel des Kunden. Das
    Storno-Dokument behaelt den Satz des Originals."""
    import json

    from app import pdf
    from app.logik import festschreiben as fs
    from app.logik import rechnungsablauf as ablauf

    def rendere(daten: dict, ziel: Path) -> str:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"%PDF-1.7\n" + json.dumps(daten, ensure_ascii=False,
                                                     default=str).encode())
        return pdf.pruefsumme(ziel)

    monkeypatch.setattr(pdf, "rendere", rendere)
    conn = db.oeffne(altbestand)
    try:
        # Beide Kunden rechnen inzwischen anders ab: volle Stunde aufwaerts.
        # 90 Minuten waeren damit 2,00 h statt 1,50 h.
        conn.execute("UPDATE kunde SET rundung_modus = 'AUFWAERTS', rundung_intervall_min = 60")

        def neu_berechnen(rechnung_id: int) -> int:
            conn.execute("BEGIN IMMEDIATE")
            fs.positionen_neu(conn, rechnung_id)
            conn.execute("COMMIT")
            return conn.execute("SELECT brutto_cent FROM rechnung WHERE id = ?",
                                (rechnung_id,)).fetchone()[0]

        # Korrigieren: RE-2026-0001 (90 Minuten zu 90 €).
        k = ablauf.korrigieren(conn, 1, "falscher Betreff", belegordner=tmp_path)
        entwurf = conn.execute("SELECT * FROM rechnung WHERE id = ?",
                               (k.entwurf_id,)).fetchone()
        assert (entwurf["rundung_modus"], entwurf["rundung_intervall_min"],
                entwurf["rundung_hinweis"]) == ("AUFWAERTS", 15, NEUER_SATZ)
        storno = conn.execute("SELECT rundung_hinweis FROM rechnung WHERE storno_von_id = 1"
                              ).fetchone()
        assert storno["rundung_hinweis"] == ALTER_SATZ
        assert neu_berechnen(k.entwurf_id) == 13500
        eingefroren = json.loads(conn.execute(
            "SELECT wert_neu FROM journal WHERE aktion = 'ENTWURF' AND objekt_id = ?",
            (k.entwurf_id,)).fetchone()[0])["rundung_eingefroren"]
        assert eingefroren == {"modus": "AUFWAERTS", "intervall_min": 15, "hinweis": NEUER_SATZ}
        ersatz = fs.festschreiben(conn, k.entwurf_id, belegordner=tmp_path)
        assert conn.execute("SELECT rundung_hinweis FROM rechnung WHERE id = ?",
                            (ersatz.rechnung_id,)).fetchone()[0] == NEUER_SATZ

        # Zurück zum Entwurf: RE-2026-0002 (90 Minuten zu 80 €).
        ablauf.zuruecknehmen(conn, 3, "Fehler bemerkt", bestaetigt=True, datenordner=tmp_path)
        entwurf = conn.execute("SELECT * FROM rechnung WHERE id = 3").fetchone()
        assert (entwurf["status"], entwurf["rundung_modus"], entwurf["rundung_intervall_min"],
                entwurf["rundung_hinweis"]) == ("ENTWURF", "AUFWAERTS", 15, NEUER_SATZ)
        assert neu_berechnen(3) == 12000
        eintrag = json.loads(conn.execute(
            "SELECT wert_neu FROM journal WHERE aktion = 'RECHNUNG_ZURUECKGENOMMEN'"
            " AND objekt_id = 3").fetchone()[0])
        assert eintrag["rundung_eingefroren"]["hinweis"] == NEUER_SATZ
        assert fs.festschreiben(conn, 3, belegordner=tmp_path).rechnung_id == 3
        assert conn.execute("SELECT rundung_hinweis FROM rechnung WHERE id = 3"
                            ).fetchone()[0] == NEUER_SATZ
    finally:
        conn.close()

"""Regressionstests zur letzten Korrekturrunde im Bereich Rechnung.

Gegenprobe 6, 10 und Querschnitt: Eingaben, die bei einer Abweisung des
Assistenten verloren gingen; Zahlungen auf ein Storno-Dokument, die nur die
Route abwies; rohe Datenbanktexte und Python-repr in Meldungen; eine
Rechnungskorrektur, die sich nicht mehr als versendet vermerken liess;
negative Betraege in der Spalte "Offen"; mehrfache Journaleintraege beim
Verwerfen; Kennungen und Betraege, die erst SQLite sprengte.
"""

import re
import sqlite3
import threading
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import festschreiben as fs
from app.logik import validierung as v
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "inhaber": "E. Muster",
    "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
    "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
    "rundung_modus": "AUFWAERTS", "rundung_intervall_min": "15",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "email": "buchhaltung@mustermann.example", "typ": "UNTERNEHMER", "land": "DE",
    "leistungsort": "INLAND", "satz_euro": "90,00", "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def daten(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
    return tmp_path / "daten"


@pytest.fixture
def klient(daten):
    with TestClient(erzeuge_app(daten / "steuer.sqlite"), raise_server_exceptions=False) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _db(daten) -> sqlite3.Connection:
    conn = sqlite3.connect(daten / "steuer.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _abfrage(daten, sql, werte=()):
    conn = _db(daten)
    try:
        return conn.execute(sql, werte).fetchall()
    finally:
        conn.close()


def _zeit(klient, daten, datum="2026-03-02", dauer="1h", titel="Beratung",
          beschreibung="") -> int:
    antwort = klient.post("/zeiten", data={
        "kunde_id": 1, "titel": titel, "datum": datum, "dauer": dauer,
        "beschreibung": beschreibung, "modus": "STUNDEN", "grund": "",
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    return int(_abfrage(daten, "SELECT MAX(id) FROM zeiteintrag")[0][0])


def _entwurf(klient, zeit_ids, **felder):
    return klient.post("/rechnung/neu", data={
        "kunde_id": 1, "datum": "2026-03-10", "modus": "PRO_TITEL",
        "einleitung": "", "schluss": "", "zeit": zeit_ids, **felder,
    }, follow_redirects=False)


def _entwurf_id(antwort) -> int:
    assert antwort.status_code == 303, antwort.text[:400]
    ort = antwort.headers["location"]
    assert "fehler=" not in ort, ort
    return int(re.search(r"/rechnung/(\d+)", ort).group(1))


def _fehler(antwort) -> str:
    assert antwort.status_code == 303, antwort.text[:400]
    return parse_qs(urlparse(antwort.headers["location"]).query).get("fehler", [""])[0]


def _festschreiben(klient, rid):
    seite = klient.get(f"/rechnung/{rid}").text
    version = re.search(r'name="version" value="(\d+)"', seite).group(1)
    codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
    antwort = klient.post(f"/rechnung/{rid}/festschreiben",
                          data={"version": version, "bestaetigt": codes},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    assert "fehler=" not in antwort.headers["location"], _fehler(antwort)


def _festgeschrieben(klient, daten, datum="2026-03-10") -> int:
    rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)], datum=datum))
    _festschreiben(klient, rid)
    return rid


def _storno_id(klient, rid, grund="Tippfehler") -> int:
    antwort = klient.post(f"/rechnung/{rid}/storno",
                          data={"grund": grund, "zeiten_freigeben": "1"},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    assert "fehler=" not in antwort.headers["location"], _fehler(antwort)
    return int(antwort.headers["location"].rsplit("/", 1)[1])


def _parallel(anzahl, funktion):
    ergebnisse = [None] * anzahl
    sperre = threading.Barrier(anzahl)

    def lauf(i):
        try:
            sperre.wait()
            ergebnisse[i] = funktion(i)
        except Exception as fehler:  # noqa: BLE001 -- im Test sichtbar machen
            ergebnisse[i] = fehler

    faeden = [threading.Thread(target=lauf, args=(i,)) for i in range(anzahl)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join()
    return ergebnisse


# ------------------------------------------------ Entscheidung 8: Verwerfen

class TestVerwerfen:
    def test_paralleles_verwerfen_schreibt_genau_einen_journaleintrag(self, klient, daten):
        """Vier gleichzeitige Klicks auf "Verwerfen" (Doppelklick, zwei Tabs).

        Vorher lief die Statuspruefung vor der Transaktion: alle vier sahen den
        Entwurf, drei DELETEs trafen nichts, und das unveraenderliche Journal
        bekam viermal ENTWURF_VERWORFEN.
        """
        for _ in range(3):
            rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
            # Die Nummer eines verworfenen Entwurfs wird wieder vergeben;
            # gezaehlt wird deshalb nur, was diese Runde ins Journal schrieb.
            vorher = _abfrage(daten, "SELECT COALESCE(MAX(id), 0) FROM journal")[0][0]
            antworten = _parallel(4, lambda _i, rid=rid: klient.post(
                f"/rechnung/{rid}/verwerfen", follow_redirects=False))
            assert all(getattr(a, "status_code", None) == 303 for a in antworten), antworten
            anzahl = _abfrage(
                daten,
                "SELECT COUNT(*) FROM journal WHERE aktion = 'ENTWURF_VERWORFEN'"
                " AND objekt_id = ? AND id > ?", (rid, vorher),
            )[0][0]
            assert anzahl == 1
            assert not _abfrage(daten, "SELECT 1 FROM rechnung WHERE id = ?", (rid,))

    def test_zweites_verwerfen_schreibt_nichts(self, conn, betrieb, kunde_id):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, erfasst_am) VALUES (?, 'B', '2026-03-02', 60, 'STUNDEN', ?)",
            (kunde_id, db.jetzt_utc()),
        ).lastrowid
        rid = fs.entwurf_anlegen(conn, kunde_id, [zid], datum="2026-03-10")
        fs.entwurf_loeschen(conn, rid)
        fs.entwurf_loeschen(conn, rid)
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE aktion = 'ENTWURF_VERWORFEN'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT rechnung_id FROM zeiteintrag WHERE id = ?", (zid,)
        ).fetchone()[0] is None

    def test_festgeschriebene_rechnung_wird_nicht_verworfen(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/verwerfen", follow_redirects=False)
        assert "Nur ein Entwurf kann verworfen werden" in _fehler(antwort)
        assert _abfrage(daten, "SELECT status FROM rechnung WHERE id = ?",
                        (rid,))[0][0] == "FESTGESCHRIEBEN"
        assert not _abfrage(
            daten, "SELECT 1 FROM journal WHERE aktion = 'ENTWURF_VERWORFEN'")


# ------------------------------------------- Entscheidung 7: Meldungen ohne repr

class TestMeldungenOhneRepr:
    def test_entwurf_anlegen_nennt_das_datum_in_anfuehrungszeichen(
        self, conn, betrieb, kunde_id
    ):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, erfasst_am) VALUES (?, 'B', '2026-03-02', 60, 'STUNDEN', ?)",
            (kunde_id, db.jetzt_utc()),
        ).lastrowid
        with pytest.raises(fs.FestschreibenFehler) as fehler:
            fs.entwurf_anlegen(conn, kunde_id, [zid], datum="9999-12-31")
        assert str(fehler.value).startswith("„9999-12-31“")

    def test_datumsproblem_ohne_repr(self):
        assert v.datumsproblem("2026-02-30") == "„2026-02-30“ ist kein Datum."
        assert v.datumsproblem("0026-03-10").startswith("„0026-03-10“ ist kein brauchbares")
        assert v.datumsproblem(date.today().isoformat()) is None


# ------------------------------------- Entscheidung 1: Eingaben beim Anlegen

def _haken(seite: str) -> dict[str, bool]:
    """Zeit-Kennung -> angehakt? aus der Seite des Assistenten."""
    return {
        m.group(1): "checked" in m.group(0)
        for m in re.finditer(r'<input type="checkbox" name="zeit" value="(\d+)"[^>]*>', seite)
    }


class TestAnlegenBehaeltEingaben:
    @pytest.mark.parametrize("datum, meldung", [
        ("9999-12-31", "kein brauchbares Rechnungsdatum"),
        ("2026-02-30", "ist kein Datum"),
        ("", "Rechnungsdatum fehlt"),
    ])
    def test_abgewiesenes_datum_behaelt_texte_haken_und_auswahl(
        self, klient, daten, datum, meldung
    ):
        """Vorher: 303 auf den leeren Assistenten, alle Zeiten wieder angehakt."""
        ids = [_zeit(klient, daten, titel=f"Arbeit {i}") for i in range(3)]
        antwort = _entwurf(
            klient, [ids[1]], datum=datum, betreff="MEIN-BETREFF",
            einleitung="MEINE-EINLEITUNG", schluss="MEIN-SCHLUSS",
            einzelnachweis="1", modus="PRO_TAG",
        )
        assert antwort.status_code == 422
        seite = antwort.text
        assert meldung in seite
        assert 'value="MEIN-BETREFF"' in seite
        assert ">MEINE-EINLEITUNG</textarea>" in seite
        assert ">MEIN-SCHLUSS</textarea>" in seite
        assert re.search(r'name="einzelnachweis" value="1"\s+checked', seite)
        # Leistungszeitraum war nicht angehakt -- und bleibt es nicht.
        assert not re.search(r'name="leistungszeitraum" value="1"\s+checked', seite)
        assert re.search(r'value="PRO_TAG"\s+checked', seite)
        assert _haken(seite) == {str(ids[0]): False, str(ids[1]): True, str(ids[2]): False}
        assert not _abfrage(daten, "SELECT 1 FROM rechnung")

    def test_unbekannte_gruppierung_und_fehlende_zeit(self, klient, daten):
        zid = _zeit(klient, daten)
        antwort = _entwurf(klient, [zid], modus="QUATSCH", betreff="B")
        assert antwort.status_code == 422
        assert "Unbekannte Gruppierung." in antwort.text
        assert 'value="B"' in antwort.text
        ohne = _entwurf(klient, [], betreff="Nur Text")
        assert ohne.status_code == 422
        assert "Es ist keine Zeit ausgewählt." in ohne.text
        assert 'value="Nur Text"' in ohne.text

    def test_fehlender_satz_behaelt_eingaben(self, klient, daten):
        klient.post("/einrichtung", data={**EINRICHTUNG, "standardsatz_euro": "0"})
        klient.post("/kunden/1", data={**KUNDE, "satz_euro": ""})
        zid = _zeit(klient, daten)
        antwort = _entwurf(klient, [zid], einleitung="Bleibt stehen")
        assert antwort.status_code == 422
        assert "kein Stundensatz hinterlegt" in antwort.text
        assert ">Bleibt stehen</textarea>" in antwort.text

    def test_ohne_abweisung_ist_alles_vorgewaehlt(self, klient, daten):
        ids = [_zeit(klient, daten, titel=f"Arbeit {i}") for i in range(2)]
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert set(_haken(seite).values()) == {True}
        assert set(_haken(seite)) == {str(i) for i in ids}
        assert "vielen Dank für Ihren Auftrag" in seite

    @pytest.mark.parametrize("kennung", ["²", str(2**63), str(2**70), "abc", "-1"])
    def test_kaputte_kennungen_ergeben_meldung(self, klient, daten, kennung):
        zid = _zeit(klient, daten)
        kunde = klient.post("/rechnung/neu", data={
            "kunde_id": kennung, "datum": "2026-03-10", "zeit": [zid],
        }, follow_redirects=False)
        assert kunde.status_code == 422
        assert "Der Kunde existiert nicht." in kunde.text
        zeit = klient.post("/rechnung/neu", data={
            "kunde_id": "1", "datum": "2026-03-10", "zeit": [kennung],
        }, follow_redirects=False)
        assert zeit.status_code == 422
        assert "Es ist keine Zeit ausgewählt." in zeit.text

    def test_kunde_aus_der_adresse(self, klient):
        for kennung in ("²", str(2**63)):
            assert klient.get(f"/rechnung/neu?kunde={kennung}").status_code == 200
            assert klient.get(f"/rechnungen?kunde={kennung}").status_code == 200

    def test_version_beim_festschreiben(self, klient, daten):
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        for kaputt in ("²", str(2**63), str(2**70)):
            antwort = klient.post(f"/rechnung/{rid}/festschreiben",
                                  data={"version": kaputt}, follow_redirects=False)
            assert "Formular war unvollständig" in _fehler(antwort)
        assert _abfrage(daten, "SELECT status FROM rechnung WHERE id = ?",
                        (rid,))[0][0] == "ENTWURF"


# ----------------------------------- Entscheidung 2: Trigger und Migration 16

class TestZahlungAufStorno:
    def _storno_dokument(self, conn, kunde_id):
        conn.execute("UPDATE betrieb SET nullrechnung_zulassen = 0 WHERE id = 1")
        original = conn.execute(
            "INSERT INTO rechnung (typ, kunde_id, nummer, nummer_jahr, nummer_seq, datum,"
            " brutto_cent, netto_cent, status, festgeschrieben_am, erstellt_am)"
            " VALUES ('RECHNUNG', ?, 'RE-1', 2026, 1, '2026-03-10', 10000, 10000,"
            " 'FESTGESCHRIEBEN', ?, ?)",
            (kunde_id, db.jetzt_utc(), db.jetzt_utc()),
        ).lastrowid
        storno = conn.execute(
            "INSERT INTO rechnung (typ, kunde_id, nummer, nummer_jahr, nummer_seq, datum,"
            " brutto_cent, netto_cent, status, storno_von_id, storno_grund,"
            " festgeschrieben_am, erstellt_am)"
            " VALUES ('STORNO', ?, 'RE-2', 2026, 2, '2026-03-11', -10000, -10000,"
            " 'FESTGESCHRIEBEN', ?, 'Tippfehler', ?, ?)",
            (kunde_id, original, db.jetzt_utc(), db.jetzt_utc()),
        ).lastrowid
        return original, storno

    def test_trigger_weist_zahlung_auf_storno_dokument_ab(self, conn, betrieb, kunde_id):
        """Vorher nahm die Datenbank ein direktes INSERT an; nur die Route wies ab."""
        _original, storno = self._storno_dokument(conn, kunde_id)
        for betrag in (100, -100):
            with pytest.raises(sqlite3.IntegrityError, match="Rechnungskorrektur"):
                conn.execute(
                    "INSERT INTO zahlung (rechnung_id, datum, betrag_cent)"
                    " VALUES (?, '2026-03-12', ?)", (storno, betrag),
                )

    def test_zahlung_auf_festgeschriebene_rechnung_geht_weiter(
        self, conn, betrieb, kunde_id
    ):
        original, _storno = self._storno_dokument(conn, kunde_id)
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-03-12', 100)",
            (original,),
        )

    def test_migration_16_ersetzt_den_alten_trigger(self, tmp_path):
        pfad = tmp_path / "alt.sqlite"
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        soll = frisch.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'zahlung_nur_festgeschrieben'"
        ).fetchone()[0]
        frisch.close()

        conn = db.oeffne(pfad)
        # Stand von Version 15: der Trigger ohne die Pruefung auf typ = 'STORNO'.
        conn.execute("DROP TRIGGER zahlung_nur_festgeschrieben")
        conn.execute("""
            CREATE TRIGGER zahlung_nur_festgeschrieben
            BEFORE INSERT ON zahlung
            WHEN NEW.rechnung_id IS NOT NULL
             AND (   (SELECT status FROM rechnung WHERE id = NEW.rechnung_id)
                         NOT IN ('FESTGESCHRIEBEN', 'STORNIERT')
                  OR (SELECT brutto_cent FROM rechnung WHERE id = NEW.rechnung_id) = 0
                  OR (    (SELECT status FROM rechnung WHERE id = NEW.rechnung_id) = 'STORNIERT'
                      AND NEW.betrag_cent > 0 ) )
            BEGIN
                SELECT RAISE(ABORT, 'Zahlung nur zu einer festgeschriebenen Rechnung mit Betrag');
            END
        """)
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version, migriert_am) VALUES (15, ?)",
                     (db.jetzt_utc(),))
        conn.commit()
        conn.close()

        conn = db.oeffne(pfad)
        try:
            ist = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'zahlung_nur_festgeschrieben'"
            ).fetchone()[0]
            assert ist == soll
            assert "'STORNO'" in ist
            assert conn.execute(
                "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION' AND objekt_id = 16"
            ).fetchone()[0] == 1
        finally:
            conn.close()


# ------------------------------------- Entscheidung 3: keine rohen DB-Texte

class TestZahlungsmeldungen:
    def test_zahlung_auf_entwurf_mit_deutscher_meldung(self, klient, daten):
        """Vorher kam der Trigger-Text roh an: "... nur eine Rueckzahlung"."""
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        antwort = klient.post(f"/rechnung/{rid}/zahlung",
                              data={"datum": "2026-03-28", "betrag_euro": "1"},
                              follow_redirects=False)
        meldung = _fehler(antwort)
        assert meldung == "Ein Entwurf kann noch keine Zahlung erhalten — erst festschreiben."
        assert "Rueck" not in meldung
        assert not _abfrage(daten, "SELECT 1 FROM zahlung")

    def test_integrity_error_wird_uebersetzt(self):
        from app.routen import rechnungen

        roh = sqlite3.IntegrityError(
            "Zahlung nur zu einer festgeschriebenen Rechnung mit Betrag; auf eine "
            "stornierte Rechnung nur eine Rueckzahlung"
        )
        text = rechnungen._zahlung_abgewiesen(roh)
        assert "Rueck" not in text and "Rückzahlung" in text
        anderes = rechnungen._zahlung_abgewiesen(sqlite3.IntegrityError("CHECK constraint"))
        assert "CHECK" not in anderes

    def test_rueckzahlung_mit_abgewiesenem_insert(self, klient, daten, monkeypatch):
        """Weist die Datenbank trotz Vorpruefung ab, kommt ein deutscher Satz an."""
        rid = _festgeschrieben(klient, daten)
        assert "fehler=" not in klient.post(
            f"/rechnung/{rid}/zahlung", data={"datum": "2026-03-20", "betrag_euro": ""},
            follow_redirects=False).headers["location"]
        _storno_id(klient, rid)
        conn = _db(daten)
        conn.execute("""
            CREATE TRIGGER test_sperre BEFORE INSERT ON zahlung
            BEGIN SELECT RAISE(ABORT, 'Zahlung nur zu einer festgeschriebenen Rechnung mit
              Betrag; auf eine stornierte Rechnung nur eine Rueckzahlung'); END
        """)
        conn.commit()
        conn.close()
        antwort = klient.post(f"/rechnung/{rid}/rueckzahlung",
                              data={"datum": "2026-03-21", "betrag_euro": ""},
                              follow_redirects=False)
        meldung = _fehler(antwort)
        assert meldung.startswith("Diese Zahlung lässt sich nicht buchen")
        assert "Rueck" not in meldung


# -------------------------- Entscheidung 4: Rechnungskorrektur versendet

class TestKorrekturVersendet:
    def test_storno_dokument_laesst_sich_als_versendet_vermerken(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        sid = _storno_id(klient, rid)
        seite = klient.get(f"/rechnung/{sid}").text
        assert f'action="/rechnung/{sid}/versendet"' in seite
        antwort = klient.post(f"/rechnung/{sid}/versendet", data={"datum": ""},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"], _fehler(antwort)
        assert _abfrage(daten, "SELECT versendet_am FROM rechnung WHERE id = ?",
                        (sid,))[0][0] == db.heute()
        assert _abfrage(daten, "SELECT COUNT(*) FROM journal WHERE aktion = 'VERSENDET'"
                        " AND objekt_id = ?", (sid,))[0][0] == 1
        assert "Versendet am" in klient.get(f"/rechnung/{sid}").text

    def test_datumspruefung_wie_bei_rechnungen(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        sid = _storno_id(klient, rid)
        storno_datum = _abfrage(daten, "SELECT datum FROM rechnung WHERE id = ?", (sid,))[0][0]
        for kaputt, meldung in (
            ("0026-03-10", "vor dem Jahr 2000"),
            ("2099-01-01", "in der Zukunft"),
            ("2026-02-30", "ist kein Datum"),
            ("2020-01-01", "vor dem Rechnungsdatum"),
        ):
            antwort = klient.post(f"/rechnung/{sid}/versendet", data={"datum": kaputt},
                                  follow_redirects=False)
            text = _fehler(antwort)
            assert meldung in text, (kaputt, text)
            assert "'" not in text
        assert _abfrage(daten, "SELECT versendet_am FROM rechnung WHERE id = ?",
                        (sid,))[0][0] is None
        kompakt = storno_datum.replace("-", "")
        klient.post(f"/rechnung/{sid}/versendet", data={"datum": kompakt},
                    follow_redirects=False)
        assert _abfrage(daten, "SELECT versendet_am FROM rechnung WHERE id = ?",
                        (sid,))[0][0] == storno_datum

    def test_stornierte_rechnung_bleibt_ausgeschlossen(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        _storno_id(klient, rid)
        antwort = klient.post(f"/rechnung/{rid}/versendet", data={"datum": ""},
                              follow_redirects=False)
        assert "Nur eine festgeschriebene Rechnung" in _fehler(antwort)


# ------------------------------------- Entscheidung 5: Überzahlung in der Liste

class TestUeberzahlungInDerListe:
    def test_spalte_offen_zeigt_ueberzahlt(self, klient, daten):
        rid = _festgeschrieben(klient, daten)
        brutto = _abfrage(daten, "SELECT brutto_cent FROM rechnung WHERE id = ?", (rid,))[0][0]
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-20", "betrag_euro": "200"},
                    follow_redirects=False)
        klient.post(f"/rechnung/{rid}/zahlung",
                    data={"datum": "2026-03-21", "betrag_euro": "400"},
                    follow_redirects=False)
        zu_viel = 60000 - brutto
        assert zu_viel > 0
        seite = klient.get("/rechnungen").text
        betrag = f"{zu_viel // 100},{zu_viel % 100:02d}"
        assert f"überzahlt {betrag} €" in seite
        assert f"-{betrag}" not in seite

    def test_jahresfilter_mit_unsinn(self, klient, daten):
        _festgeschrieben(klient, daten)
        for jahr in ("²", "9999999999999999999", "abc", "2026"):
            assert klient.get(f"/rechnungen?jahr={jahr}").status_code == 200, jahr


# --------------------------------------- Entscheidung 6: Zeilenumbrüche sichtbar

class TestZeilenumbrueche:
    def test_untertext_und_beschreibung_mit_pre_line(self, klient, daten):
        zid = _zeit(klient, daten, beschreibung="Zeile eins\nZeile zwei")
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert re.search(r'class="gedaempft beschreibung">Zeile eins\nZeile zwei<', seite)
        rid = _entwurf_id(_entwurf(klient, [zid], modus="PRO_EINTRAG"))
        conn = _db(daten)
        conn.execute("UPDATE rechnungsposition SET untertext = 'A\nB' WHERE rechnung_id = ?",
                     (rid,))
        conn.commit()
        conn.close()
        assert re.search(r'class="gedaempft beschreibung">A\nB<',
                         klient.get(f"/rechnung/{rid}").text)

    def test_klasse_beschreibung_bricht_um(self):
        from pathlib import Path

        css = (Path(__file__).resolve().parent.parent / "app/static/stil.css").read_text()
        assert re.search(r"\.beschreibung\s*\{\s*white-space:\s*pre-line", css)


# ----------------------------- Entscheidung 7 und 9: Beträge ohne repr und 500

class TestBetraege:
    @pytest.mark.parametrize("route", ["zahlung", "rueckzahlung"])
    @pytest.mark.parametrize("betrag, meldung", [
        ("abc", "„abc“ ist kein Betrag."),
        ("inf", "„inf“ ist kein Betrag."),
        ("1e400", "„1e400“ ist kein Betrag."),
        (str(2**63), f"„{2**63}“ ist als Betrag unplausibel groß."),
    ])
    def test_betrag_als_meldung(self, klient, daten, route, betrag, meldung):
        """Angepasst (Schlussprobe, Bereich quer): "1e400" galt hier als Zahl in
        Exponentschreibweise und war deshalb "unplausibel groß". Seit
        euro_nach_cent nur noch Ziffern, Komma, Punkt, Vorzeichen und
        Leerzeichen annimmt ("1e3" waere sonst still 1.000,00 €), ist es
        schlicht kein Betrag."""
        rid = _festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/{route}",
                              data={"datum": "2026-03-20", "betrag_euro": betrag},
                              follow_redirects=False)
        assert _fehler(antwort) == meldung
        assert not _abfrage(daten, "SELECT 1 FROM zahlung")

    @pytest.mark.parametrize("route", ["zahlung", "rueckzahlung", "versendet"])
    def test_datum_ohne_repr(self, klient, daten, route):
        rid = _festgeschrieben(klient, daten)
        antwort = klient.post(f"/rechnung/{rid}/{route}",
                              data={"datum": "kaputt", "betrag_euro": "1"},
                              follow_redirects=False)
        text = _fehler(antwort)
        assert "„kaputt“ ist kein Datum" in text
        assert "'kaputt'" not in text

    def test_kopf_datum_ohne_repr(self, klient, daten):
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        antwort = klient.post(f"/rechnung/{rid}/kopf", data={"datum": "2026-02-30"},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "„2026-02-30“ ist kein Datum." in antwort.text
        assert "&#39;2026-02-30&#39;" not in antwort.text

    def test_kopf_abweisung_behaelt_alle_eingaben(self, klient, daten):
        """Ein abgewiesenes Datum unter "Kopf und Texte" leitete um; Betreff,
        Einleitung, Schlusstext und Haken desselben Absendens waren weg."""
        rid = _entwurf_id(_entwurf(klient, [_zeit(klient, daten)]))
        antwort = klient.post(f"/rechnung/{rid}/kopf", data={
            "datum": "2026-02-30", "betreff": "Mein Betreff",
            "einleitung": "Hallo Welt", "schluss": "Gruß", "einzelnachweis": "1",
        }, follow_redirects=False)
        assert antwort.status_code == 422
        assert 'value="Mein Betreff"' in antwort.text
        assert ">Hallo Welt</textarea>" in antwort.text
        assert ">Gruß</textarea>" in antwort.text
        assert re.search(r'name="einzelnachweis" value="1"\s+checked', antwort.text)
        assert not re.search(r'name="leistungszeitraum" value="1"\s+checked', antwort.text)
        seite = klient.get(f"/rechnung/{rid}").text
        assert 'value="Mein Betreff"' not in seite, "gespeichert wurde nichts"

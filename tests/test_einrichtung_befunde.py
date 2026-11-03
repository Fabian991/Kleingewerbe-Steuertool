"""Befunde der Fehleranalyse im Bereich Einrichtung und Startseite.

Jeder Test haelt EIN Fehlverhalten fest, das die Pruefung reproduziert hat --
und zwar so, wie der Nutzer es sah: ueber HTTP gegen eine frische Datenbank.
Die Reihenfolge folgt den Entscheidungen zur Befundliste.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import fristen, grenzwert
from app.logik import nummernkreis as nk
from app.main import erzeuge_app

PFLICHT = {
    "name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890",
}

KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def datenbank(tmp_path):
    return tmp_path / "daten" / "web.sqlite"


@pytest.fixture
def klient(datenbank):
    with TestClient(erzeuge_app(datenbank)) as c:
        yield c


@pytest.fixture
def eingerichtet(klient):
    antwort = klient.post("/einrichtung", data={**PFLICHT, "start_seq": "1"},
                          follow_redirects=False)
    assert antwort.status_code == 303
    return klient


def _lies(datenbank: Path, sql: str, *werte):
    conn = sqlite3.connect(datenbank)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, werte).fetchall()
    finally:
        conn.close()


def _schreibe(datenbank: Path, sql: str, *werte):
    conn = sqlite3.connect(datenbank)
    try:
        conn.execute(sql, werte)
        conn.commit()
    finally:
        conn.close()


def _kreise(datenbank: Path) -> list[tuple[int, int]]:
    return [
        (z["jahr"], z["naechste_seq"])
        for z in _lies(datenbank, "SELECT jahr, naechste_seq FROM nummernkreis"
                                  " WHERE typ = 'RECHNUNG' ORDER BY jahr")
    ]


def _feldwert(html: str, name: str) -> str:
    """Der value des Eingabefelds ``name`` im gerenderten Formular."""
    rest = html.split(f'name="{name}"', 1)[1].split(">", 1)[0]
    return rest.split('value="', 1)[1].split('"', 1)[0] if 'value="' in rest else ""


# ---------------------------------------------------------------- Nummernkreis


class TestJahreswechsel:
    """Entscheidung 1: das Formular zeigt den Stand des LAUFENDEN Jahres."""

    def test_formular_zeigt_im_neuen_jahr_die_eins(self, eingerichtet, datenbank, monkeypatch):
        _schreibe(datenbank, "UPDATE nummernkreis SET naechste_seq = 6 WHERE typ = 'RECHNUNG'")
        jahr = int(db.heute()[:4])
        monkeypatch.setattr(db, "heute", lambda: f"{jahr + 1}-01-10")
        text = eingerichtet.get("/einrichtung").text
        assert _feldwert(text, "start_seq") == "1"

    def test_speichern_ohne_aenderung_uebernimmt_keinen_vorjahresstand(
        self, eingerichtet, datenbank, monkeypatch
    ):
        """Nur die Telefonnummer geaendert -- die erste Rechnung des neuen
        Jahres muss trotzdem 0001 heissen, nicht 0006."""
        _schreibe(datenbank, "UPDATE nummernkreis SET naechste_seq = 6 WHERE typ = 'RECHNUNG'")
        jahr = int(db.heute()[:4])
        monkeypatch.setattr(db, "heute", lambda: f"{jahr + 1}-01-10")
        formular = eingerichtet.get("/einrichtung").text
        antwort = eingerichtet.post("/einrichtung", data={
            **PFLICHT, "telefon": "040 1", "start_seq": _feldwert(formular, "start_seq"),
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert _kreise(datenbank) == [(jahr, 6), (jahr + 1, 1)]
        assert f"RE-{jahr + 1}-0001" in eingerichtet.get("/").text

    def test_leeres_feld_legt_das_neue_jahr_bei_eins_an(self, eingerichtet, datenbank, monkeypatch):
        _schreibe(datenbank, "UPDATE nummernkreis SET naechste_seq = 6 WHERE typ = 'RECHNUNG'")
        jahr = int(db.heute()[:4])
        monkeypatch.setattr(db, "heute", lambda: f"{jahr + 1}-01-10")
        eingerichtet.post("/einrichtung", data={**PFLICHT, "start_seq": ""})
        assert _kreise(datenbank) == [(jahr, 6), (jahr + 1, 1)]


class TestLeeresZaehlerfeld:
    """Entscheidung 2: leer heisst unveraendert, nicht 1."""

    def test_leeres_feld_laesst_den_umstiegsstand_stehen(self, klient, datenbank):
        klient.post("/einrichtung", data={**PFLICHT, "start_seq": "118"})
        assert _kreise(datenbank) == [(int(db.heute()[:4]), 118)]
        antwort = klient.post("/einrichtung", data={**PFLICHT, "start_seq": ""},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _kreise(datenbank) == [(int(db.heute()[:4]), 118)]
        assert "0118" in klient.get("/").text

    def test_ausgefuellter_wert_stellt_um(self, klient, datenbank):
        klient.post("/einrichtung", data={**PFLICHT, "start_seq": "118"})
        klient.post("/einrichtung", data={**PFLICHT, "start_seq": "130"})
        assert _kreise(datenbank) == [(int(db.heute()[:4]), 130)]

    def test_null_wird_auf_eins_gehoben(self, klient, datenbank):
        klient.post("/einrichtung", data={**PFLICHT, "start_seq": "0"})
        assert _kreise(datenbank) == [(int(db.heute()[:4]), 1)]


class TestZaehlerObergrenze:
    """Entscheidung 6: neun Stellen, sonst 422 statt Serverfehler."""

    def test_zwanzigstellige_nummer_wird_abgewiesen(self, klient):
        antwort = klient.post("/einrichtung", data={
            **PFLICHT, "start_seq": "99999999999999999999",
        })
        assert antwort.status_code == 422
        assert "unplausibel" in antwort.text
        assert klient.get("/gesund").json()["eingerichtet"] is False

    def test_neun_stellen_gehen_noch(self, klient, datenbank):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "start_seq": "999999999"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _kreise(datenbank) == [(int(db.heute()[:4]), 999_999_999)]

    @pytest.mark.parametrize("roh", ["abc", "1.5", "1,5", "1_000", "1e3"])
    def test_keine_ganze_zahl_meldet_das_feld(self, klient, roh):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "start_seq": roh})
        assert antwort.status_code == 422
        assert "ganze Zahl" in antwort.text
        assert f'value="{roh}"' in antwort.text, "die Eingabe bleibt im Feld"


class TestNummernformat:
    """Entscheidung 10: genau ein Zaehler, ausgeglichene Klammern, klare Meldung."""

    def test_zwei_zaehler_werden_abgewiesen(self):
        with pytest.raises(ValueError, match="nur einen Zähler"):
            nk.pruefe_format("RE-{JJJJ}-{NNNN}-{NNNN}")

    @pytest.mark.parametrize(
        "vorlage", ["{JJJJ}-{N}abc}", "{JJJJ}-{N", "{{JJJJ}}-{N}", "}{JJJJ}-{N}"]
    )
    def test_unausgeglichene_klammern_werden_abgewiesen(self, vorlage):
        with pytest.raises(ValueError, match="Klammern"):
            nk.pruefe_format(vorlage)

    def test_kleinbuchstaben_nennen_die_grossschreibung(self):
        with pytest.raises(ValueError, match="großgeschrieben") as fehler:
            nk.pruefe_format("RE-{jjjj}-{nnnn}")
        assert "{JJJJ} statt {jjjj}" in str(fehler.value)
        assert "{NNNN} statt {nnnn}" in str(fehler.value)

    def test_meldungen_tragen_umlaute(self):
        with pytest.raises(ValueError, match="Zähler"):
            nk.pruefe_format("RE-{JJJJ}")
        with pytest.raises(ValueError, match="jährlicher Neustart"):
            nk.pruefe_format("{NNNN}", reset_jaehrlich=True)

    def test_gueltige_formate_bleiben_gueltig(self):
        nk.pruefe_format("RE-{JJJJ}-{MM}-{NNNN}")
        nk.pruefe_format("{JJ}{NNN}")
        nk.pruefe_format("{N}", reset_jaehrlich=False)

    def test_ueber_das_formular(self, klient):
        antwort = klient.post("/einrichtung", data={
            **PFLICHT, "nummer_format": "RE-{JJJJ}-{NNNN}-{NNNN}",
        })
        assert antwort.status_code == 422
        assert "nur einen Zähler" in antwort.text
        antwort = klient.post("/einrichtung", data={**PFLICHT, "nummer_format": "{JJJJ}-{N}abc}"})
        assert antwort.status_code == 422
        assert "Klammern" in antwort.text
        assert klient.get("/gesund").json()["eingerichtet"] is False


# ------------------------------------------------------------- Eingabepruefung


class TestStundensatz:
    """Entscheidung 3: dieselbe Lesart wie beim Kunden."""

    @pytest.mark.parametrize(("roh", "cent"), [
        ("1.234", 123400), ("1.234,56", 123456), ("85", 8500), ("85,5", 8550),
        ("12.5", 1250), ("", 0),
    ])
    def test_lesart_wie_euro_nach_cent(self, klient, datenbank, roh, cent):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "standardsatz_euro": roh},
                              follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:300]
        assert _lies(datenbank, "SELECT standardsatz_cent FROM betrieb")[0][0] == cent

    @pytest.mark.parametrize("roh", ["inf", "nan", "1e3", "-5", "abc", "1,2,3", "."])
    def test_unbrauchbare_eingabe_bleibt_im_feld(self, klient, roh):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "standardsatz_euro": roh})
        assert antwort.status_code == 422
        assert "Stundensatz" in antwort.text
        assert _feldwert(antwort.text, "standardsatz_euro") == roh, "nicht '0,00'"
        assert klient.get("/gesund").json()["eingerichtet"] is False

    def test_gespeicherter_satz_erscheint_als_text(self, klient):
        klient.post("/einrichtung", data={**PFLICHT, "standardsatz_euro": "1.234,5"})
        assert _feldwert(klient.get("/einrichtung").text, "standardsatz_euro") == "1234,50"


class TestPflichtfelder:
    """Entscheidungen 4 und 5: Leerzeichen sind nichts, und das Formular kommt zurueck."""

    @pytest.mark.parametrize(("feld", "bezeichnung"), [
        ("name", "Firmenname"), ("strasse", "Straße und Hausnummer"), ("plz", "PLZ"),
        ("ort", "Ort"),
    ])
    def test_nur_leerzeichen_werden_abgewiesen(self, klient, feld, bezeichnung):
        antwort = klient.post("/einrichtung", data={**PFLICHT, feld: "   "})
        assert antwort.status_code == 422
        assert f"„{bezeichnung}“ fehlt" in antwort.text
        assert klient.get("/gesund").json()["eingerichtet"] is False

    def test_fehlendes_feld_gibt_das_formular_statt_json(self, klient):
        daten = {k: v for k, v in PFLICHT.items() if k != "name"}
        antwort = klient.post("/einrichtung", data={**daten, "telefon": "040 1"})
        assert antwort.status_code == 422
        assert antwort.headers["content-type"].startswith("text/html")
        assert '"detail"' not in antwort.text
        assert "„Firmenname“ fehlt" in antwort.text
        assert _feldwert(antwort.text, "telefon") == "040 1"
        assert _feldwert(antwort.text, "ort") == "Hamburg"

    @pytest.mark.parametrize("plz", ["2OO95", "ABCDE", "D-20095", "2009", "200955"])
    def test_plz_braucht_fuenf_ziffern(self, klient, plz):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "plz": plz})
        assert antwort.status_code == 422
        assert "fünf Ziffern" in antwort.text
        assert _feldwert(antwort.text, "plz") == plz

    def test_ueberschrift_bleibt_bei_der_ersteinrichtung_einrichtung(self, klient):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "standardsatz_euro": "x"})
        assert antwort.status_code == 422
        assert "<h1>Einrichtung</h1>" in antwort.text

    def test_ueberschrift_heisst_danach_einstellungen(self, eingerichtet):
        assert "<h1>Einstellungen</h1>" in eingerichtet.get("/einrichtung").text
        antwort = eingerichtet.post("/einrichtung", data={**PFLICHT, "standardsatz_euro": "x"})
        assert antwort.status_code == 422
        assert "<h1>Einstellungen</h1>" in antwort.text

    @pytest.mark.parametrize(
        "feld", ["zahlungsziel_tage", "rundung_intervall_min", "kundennummer_start"]
    )
    def test_zahlenfelder_mit_text_geben_das_formular(self, klient, feld):
        antwort = klient.post("/einrichtung", data={**PFLICHT, feld: "abc"})
        assert antwort.status_code == 422
        assert "ganze Zahl" in antwort.text
        assert _feldwert(antwort.text, feld) == "abc"

    def test_leere_zahlenfelder_nehmen_die_vorbelegung(self, klient, datenbank):
        antwort = klient.post("/einrichtung", data={
            **PFLICHT, "zahlungsziel_tage": "", "rundung_intervall_min": "",
            "kundennummer_start": "",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        zeile = _lies(datenbank, "SELECT zahlungsziel_tage, rundung_intervall_min,"
                                 " kundennummer_start FROM betrieb")[0]
        assert tuple(zeile) == (14, 15, 1)


class TestBetriebsbeginn:
    """Entscheidung 7: ISO gespeichert, nichts aus der Zukunft."""

    def test_kompaktes_datum_wird_normalisiert(self, klient, datenbank):
        klient.post("/einrichtung", data={**PFLICHT, "beginn_am": "20200101"})
        assert _lies(datenbank, "SELECT beginn_am FROM betrieb")[0][0] == "2020-01-01"
        assert _feldwert(klient.get("/einrichtung").text, "beginn_am") == "2020-01-01"

    def test_zukunft_wird_abgewiesen(self, klient):
        morgen = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        antwort = klient.post("/einrichtung", data={**PFLICHT, "beginn_am": morgen})
        assert antwort.status_code == 422
        assert "Zukunft" in antwort.text
        assert _feldwert(antwort.text, "beginn_am") == morgen

    def test_heute_ist_erlaubt(self, klient):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "beginn_am": db.heute()},
                              follow_redirects=False)
        assert antwort.status_code == 303


class TestZahlungszielUndRundung:
    """Entscheidungen 8 und 9."""

    @pytest.mark.parametrize("tage", ["-3", "366", "1000"])
    def test_zahlungsziel_ausserhalb_des_bereichs(self, klient, tage):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "zahlungsziel_tage": tage})
        assert antwort.status_code == 422
        assert "zwischen 0 und 365" in antwort.text
        assert _feldwert(antwort.text, "zahlungsziel_tage") == tage

    @pytest.mark.parametrize("tage", ["0", "365"])
    def test_zahlungsziel_randwerte(self, klient, datenbank, tage):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "zahlungsziel_tage": tage},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT zahlungsziel_tage FROM betrieb")[0][0] == int(tage)

    def test_unbekannte_rundungsregel_wird_abgewiesen(self, klient):
        antwort = klient.post("/einrichtung", data={**PFLICHT, "rundung_modus": "QUATSCH"})
        assert antwort.status_code == 422
        assert "Rundungsregel" in antwort.text
        assert klient.get("/gesund").json()["eingerichtet"] is False


# ------------------------------------------------------------------ Erststart


class TestErststart:
    """Entscheidung 12: ohne Betrieb fuehrt jeder Weg zur Einrichtung."""

    @pytest.mark.parametrize("pfad", [
        "/", "/kunden", "/kunden/neu", "/zeiten", "/rechnungen", "/rechnung/neu",
        "/leistungen", "/journal", "/backup", "/backup/excel", "/belege", "/euer",
    ])
    def test_seiten_leiten_zur_einrichtung(self, klient, pfad):
        antwort = klient.get(pfad, follow_redirects=False)
        assert antwort.status_code == 303, pfad
        assert antwort.headers["location"] == "/einrichtung"

    def test_post_legt_nichts_an(self, klient, datenbank):
        antwort = klient.post("/kunden", data=KUNDE, follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"] == "/einrichtung"
        assert _lies(datenbank, "SELECT COUNT(*) FROM kunde")[0][0] == 0
        antwort = klient.post("/zeiten", data={
            "kunde_id": 1, "titel": "Beratung", "datum": "2026-03-02", "dauer": "1h",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT COUNT(*) FROM zeiteintrag")[0][0] == 0

    def test_einrichtung_gesund_und_stylesheet_bleiben_erreichbar(self, klient):
        assert klient.get("/einrichtung").status_code == 200
        assert klient.get("/gesund").status_code == 200
        assert klient.get("/static/stil.css").status_code == 200

    def test_nach_der_einrichtung_ist_alles_offen(self, eingerichtet):
        for pfad in ("/kunden", "/zeiten", "/rechnungen", "/journal"):
            assert eingerichtet.get(pfad, follow_redirects=False).status_code == 200, pfad


# ------------------------------------------------------------------ Startseite


def _alte_zeit(klient, datenbank, tage_alt: int, minuten: int = 60, kunde: dict | None = None):
    """Ein Kunde mit einem Zeiteintrag, der ``tage_alt`` Tage zurueckliegt."""
    ort = klient.post("/kunden", data=kunde or KUNDE, follow_redirects=False).headers["location"]
    kid = int(ort.rsplit("/", 1)[1])
    datum = (_dt.date.today() - _dt.timedelta(days=tage_alt)).isoformat()
    _schreibe(
        datenbank,
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
        " VALUES (?, 'Beratung', ?, ?, ?)",
        kid, datum, minuten, db.jetzt_utc(),
    )
    return kid


class TestPluralformen:
    """Entscheidung 11: "1 Tag", nicht "1 Tage"."""

    def test_fristchip_bildet_den_plural_richtig(self, eingerichtet, datenbank):
        # So alt, dass die Frist gerade EBEN abgelaufen ist. Faellt der Stichtag
        # auf einen 29., 30. oder 31., kappt die Frist auf den 28. -- dann sind
        # es ein paar Tage mehr; geprueft wird die Form zur tatsaechlichen Zahl.
        heute = _dt.date.today()
        aeltestes = fristen._monate_zurueck(heute - _dt.timedelta(days=1), fristen.FRIST_MONATE)
        _alte_zeit(eingerichtet, datenbank, (heute - aeltestes).days)
        conn = sqlite3.connect(datenbank)
        conn.row_factory = sqlite3.Row
        try:
            [fall] = fristen.offene_fristen(conn)
        finally:
            conn.close()
        ueber = -fall.tage_rest
        assert ueber >= 1
        text = eingerichtet.get("/").text
        erwartet = f"{ueber} Tag über" if ueber == 1 else f"{ueber} Tage über"
        assert erwartet in text
        assert "1 Tage" not in text

    def test_sicherung_von_gestern_ist_ein_tag(self, eingerichtet, datenbank, tmp_path):
        archiv = tmp_path / "sicherung.zip"
        archiv.write_bytes(b"x")
        gestern = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).replace(microsecond=0)
        _schreibe(datenbank, "INSERT INTO backup_log (zeitpunkt, pfad, geprueft) VALUES (?, ?, 1)",
                  gestern.isoformat(), str(archiv))
        text = eingerichtet.get("/").text
        assert "1 Tag<" in text or "1 Tag\n" in text
        assert "1 Tage" not in text

    def test_ein_bezahlter_beleg(self, eingerichtet):
        eingerichtet.post("/belege/eigenbeleg", data={
            "belegdatum": "2026-03-14", "brutto_euro": "12,34", "bezeichnung": "Parken",
            "quote": "100", "grund": "Parkautomat ohne Belegausgabe, Termin bei Mustermann GmbH",
        })
        eingerichtet.post("/belege/1/bezahlt", data={"datum": "2026-03-14"})
        text = eingerichtet.get("/").text
        assert "1 bezahlter Beleg" in text
        assert "1 bezahlte Beleg " not in text


class TestSicherungskarte:
    """Entscheidung 11: lokale Zeit, und "pruefen" statt "sichern"."""

    @pytest.fixture
    def berlin(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Berlin")
        time.tzset()
        yield
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()

    def test_sicherung_kurz_nach_mitternacht_ist_von_heute(self, berlin, eingerichtet, datenbank,
                                                           tmp_path):
        archiv = tmp_path / "sicherung.zip"
        archiv.write_bytes(b"x")
        # 00:30 Uhr Ortszeit heute -- in UTC noch der Vortag.
        lokal = _dt.datetime.combine(_dt.date.today(), _dt.time(0, 30)).astimezone()
        utc = lokal.astimezone(_dt.UTC)
        assert utc.date() < lokal.date(), "sonst prueft der Test nichts"
        _schreibe(datenbank, "INSERT INTO backup_log (zeitpunkt, pfad, geprueft) VALUES (?, ?, 1)",
                  utc.isoformat(), str(archiv))
        text = eingerichtet.get("/").text
        karte = text.split("Letzte geprüfte Sicherung", 1)[1][:200]
        assert "heute" in karte
        assert "1 Tag" not in karte

    def test_ungepruefte_sicherung_fuehrt_zum_pruefen(self, eingerichtet):
        text = eingerichtet.get("/").text
        assert "noch keine — jetzt sichern" in text
        antwort = eingerichtet.post("/backup", follow_redirects=False)
        assert antwort.status_code == 303
        text = eingerichtet.get("/").text
        assert "noch nicht geprüft — jetzt prüfen" in text
        assert "noch keine — jetzt sichern" not in text
        assert 'href="/backup"' in text


class TestNegativerJahreszaehler:
    """Entscheidung 11: Rueckzahlung im Folgejahr."""

    def test_balkenbreite_wird_gekappt(self):
        stand = grenzwert.Grenzstand(2027, -50000, 0, False, 10_000_000, True)
        assert stand.negativ
        assert stand.balken_prozent == 0.0
        voll = grenzwert.Grenzstand(2026, 12_000_000, 0, False, 10_000_000, True)
        assert voll.balken_prozent == 100.0

    def test_startseite_benennt_die_rueckzahlung(self, eingerichtet, datenbank, monkeypatch):
        kid = int(eingerichtet.post("/kunden", data=KUNDE,
                                    follow_redirects=False).headers["location"].rsplit("/", 1)[1])
        jahr = int(db.heute()[:4])
        _schreibe(
            datenbank,
            """
            INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                                  netto_cent, brutto_cent, leistungsort, status,
                                  festgeschrieben_am, erstellt_am)
            VALUES ('RECHNUNG', 'RE-1', 1, ?, ?, ?, 50000, 50000, 'INLAND',
                    'FESTGESCHRIEBEN', ?, ?)
            """,
            jahr, kid, f"{jahr}-12-01", db.jetzt_utc(), db.jetzt_utc(),
        )
        zahlung = "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (1, ?, ?)"
        _schreibe(datenbank, zahlung, f"{jahr}-12-31", 50000)
        _schreibe(datenbank, zahlung, f"{jahr + 1}-01-02", -50000)
        monkeypatch.setattr(db, "heute", lambda: f"{jahr + 1}-01-10")
        text = eingerichtet.get("/").text
        assert "-500,00 €" in text
        assert "Rückzahlungen übersteigen die Einnahmen dieses Jahres" in text
        assert "width: 0.0%" in text
        assert "width: -" not in text


class TestZeitenImEntwurf:
    """Entscheidung 13: der Weg fuehrt zum Entwurf, und die Karte zaehlt ihn mit."""

    def test_fristtabelle_und_karte_fuehren_zum_entwurf(self, eingerichtet, datenbank):
        kid = _alte_zeit(eingerichtet, datenbank, 200)
        [zid] = [z[0] for z in _lies(datenbank, "SELECT id FROM zeiteintrag")]
        antwort = eingerichtet.post("/rechnung/neu", data={
            "kunde_id": kid, "datum": db.heute(), "modus": "PRO_TITEL",
            "einleitung": "", "schluss": "", "zeit": [zid],
        }, follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:300]
        rid = int(antwort.headers["location"].rsplit("/", 1)[1])

        text = eingerichtet.get("/").text
        assert "Rechnungsfrist" in text
        assert f'href="/rechnung/{rid}">Entwurf öffnen' in text
        assert f"/rechnung/neu?kunde={kid}" not in text, "dort hiesse es: keine Zeiten offen"
        karte = text.split("Nicht abgerechnet</div>", 1)[1][:300]
        assert "1,00 h" in karte
        assert "davon 1,00 h im Entwurf" in karte
        assert "bei 1 Kunden" in karte

    def test_ohne_entwurf_bleibt_der_assistent(self, eingerichtet, datenbank):
        kid = _alte_zeit(eingerichtet, datenbank, 200)
        text = eingerichtet.get("/").text
        assert f"/rechnung/neu?kunde={kid}" in text
        assert "Entwurf öffnen" not in text
        assert "im Entwurf" not in text

    def test_offene_und_entwurfszeiten_nebeneinander(self, eingerichtet, datenbank):
        kid = _alte_zeit(eingerichtet, datenbank, 30, minuten=60)
        [zid] = [z[0] for z in _lies(datenbank, "SELECT id FROM zeiteintrag")]
        eingerichtet.post("/rechnung/neu", data={
            "kunde_id": kid, "datum": db.heute(), "modus": "PRO_TITEL",
            "einleitung": "", "schluss": "", "zeit": [zid],
        })
        _schreibe(datenbank, "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
                             " erfasst_am) VALUES (?, 'Neu', ?, 30, ?)", kid, db.heute(),
                  db.jetzt_utc())
        text = eingerichtet.get("/").text
        zeile = text.split("<h2>Nicht abgerechnete Zeiten</h2>", 1)[1]
        assert "Entwurf öffnen" in zeile
        assert f"/rechnung/neu?kunde={kid}" in zeile
        assert "1,00 h im Entwurf" in zeile
        assert "1,50 h" in text.split("Nicht abgerechnet</div>", 1)[1][:300]


class TestDrittland:
    """Entscheidung 14: keine Rechnungsfrist nach deutschem Recht."""

    def test_drittlandskunde_bekommt_keine_frist(self, conn, betrieb):
        kid = conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, typ, leistungsort, land,"
            " angelegt_am) VALUES ('K-0009', 'US Inc', '1 Main St', '10001', 'New York',"
            " 'UNTERNEHMER', 'DRITTLAND', 'US', ?)",
            (db.heute(),),
        ).lastrowid
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Arbeit', '2026-01-05', 60, ?)",
            (kid, db.jetzt_utc()),
        )
        assert fristen.offene_fristen(conn, heute=_dt.date(2026, 10, 12)) == []

    def test_inlandskunde_daneben_bekommt_sie_weiterhin(self, conn, betrieb, kunde_id):
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Arbeit', '2026-01-05', 60, ?)",
            (kunde_id, db.jetzt_utc()),
        )
        conn.execute("UPDATE kunde SET typ = 'UNTERNEHMER' WHERE id = ?", (kunde_id,))
        [fall] = fristen.offene_fristen(conn, heute=_dt.date(2026, 10, 12))
        assert fall.kunde_id == kunde_id
        assert fall.fundstelle == "§ 14 Abs. 2 Satz 2 UStG"

    def test_docstring_erklaert_es(self):
        assert "§ 3a Abs. 2 UStG" in fristen.__doc__
        assert "Drittland" in fristen.__doc__
        assert "DRITTLAND" in fristen.OHNE_FRIST_LEISTUNGSORTE

    def test_ueber_die_startseite(self, eingerichtet, datenbank):
        _alte_zeit(eingerichtet, datenbank, 300, kunde={
            **KUNDE, "name": "US Inc", "leistungsort": "DRITTLAND", "land": "US",
        })
        text = eingerichtet.get("/").text
        assert "Rechnungsfrist" not in text
        assert "US Inc" in text, "die Zeiten sind trotzdem offen"

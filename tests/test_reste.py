"""Reste der Fehleranalyse, die keinem Bereich gehoerten.

Jeder Test haelt einen Befund fest, der nach den Korrekturrunden noch offen
war: der Journal-Spiegel, das gleichzeitige Oeffnen der Datenbank, die
Meldungen der Einrichtung, die Pruefung, ob Land und USt-IdNr. eines
EU-Kunden zusammenpassen, und FastAPIs Entwicklerseiten.
"""

from __future__ import annotations

import threading

import pytest

from app import db
from app.logik import journal_export

# ------------------------------------------------------------ Journal-Spiegel

class TestJournalSpiegel:
    def test_abbruch_laesst_den_alten_spiegel_stehen(self, conn, tmp_path, monkeypatch):
        """Vorher schrieb write_text direkt in die Datei: ein Abbruch mitten im
        Schreiben hinterliess einen halben Spiegel."""
        ziel = tmp_path / "journal.txt"
        db.protokolliere(conn, "TEST", "beleg", 1, begruendung="erster")
        journal_export.schreibe(conn, ziel)
        vorher = ziel.read_text(encoding="utf-8")

        def bricht_ab(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(journal_export.os, "replace", bricht_ab)
        db.protokolliere(conn, "TEST", "beleg", 2, begruendung="zweiter")
        with pytest.raises(OSError):
            journal_export.schreibe(conn, ziel)
        assert ziel.read_text(encoding="utf-8") == vorher
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".teil")] == []

    def test_gleichzeitig_geschrieben_ist_der_spiegel_vollstaendig(self, tmp_path):
        datei = tmp_path / "db.sqlite"
        with db.oeffne(datei) as conn:
            for nummer in range(200):
                db.protokolliere(conn, "TEST", "beleg", nummer, begruendung="x" * 50)
            conn.commit()
            erwartet = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        ziel = tmp_path / "journal.txt"
        fehler: list[BaseException] = []

        def lauf():
            try:
                verbindung = db.verbinde(datei)
                try:
                    for _ in range(5):
                        journal_export.schreibe(verbindung, ziel)
                finally:
                    verbindung.close()
            except BaseException as problem:  # pragma: no cover - nur bei Fehler
                fehler.append(problem)

        laeufe = [threading.Thread(target=lauf) for _ in range(4)]
        for t in laeufe:
            t.start()
        for t in laeufe:
            t.join()
        assert fehler == []
        assert journal_export.spiegel_eintraege(ziel) == erwartet >= 200
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".teil")] == []
        assert ziel.stat().st_mode & 0o777 == 0o644


# ------------------------------------------ Datenbank gleichzeitig oeffnen

def _gleichzeitig_oeffnen(datei, anzahl=6):
    fehler: list[BaseException] = []
    start = threading.Barrier(anzahl)

    def lauf():
        try:
            start.wait()
            db.oeffne(datei).close()
        except BaseException as problem:  # pragma: no cover - nur bei Fehler
            fehler.append(problem)

    laeufe = [threading.Thread(target=lauf) for _ in range(anzahl)]
    for t in laeufe:
        t.start()
    for t in laeufe:
        t.join()
    return fehler


class TestGleichzeitigOeffnen:
    """Zwei Verbindungen, die dieselbe Datei gleichzeitig oeffneten, scheiterten
    an "view v_... already exists": schema.sql verwarf und erzeugte die Sichten
    ohne Transaktion. Passiert, wenn das Programm doppelt startet oder ein
    Werkzeugskript neben der laufenden App die Datenbank oeffnet."""

    def test_bestehende_datenbank(self, tmp_path):
        datei = tmp_path / "db.sqlite"
        db.oeffne(datei).close()
        for _ in range(5):
            assert _gleichzeitig_oeffnen(datei) == []
        with db.verbinde(datei) as conn:
            sichten = {z[0] for z in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'")}
        assert {"v_verzicht", "v_offene_posten", "v_zeiteintrag_status"} <= sichten

    def test_neue_datei(self, tmp_path):
        for runde in range(5):
            datei = tmp_path / f"neu{runde}.sqlite"
            assert _gleichzeitig_oeffnen(datei) == []
            with db.verbinde(datei) as conn:
                version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert version == db.SCHEMA_VERSION


# ------------------------------------------- USt-IdNr. und Land des Kunden

def _eu_kunde(conn, *, ust_idnr, land):
    return conn.execute(
        "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, land, typ,"
        " leistungsort, ust_idnr, satz_cent, angelegt_am)"
        " VALUES ('K-0009', 'Alpenbau GmbH', 'Ringstr. 1', '1010', 'Wien', ?,"
        " 'UNTERNEHMER', 'EU_B2B_REVERSE_CHARGE', ?, 9500, ?)",
        (land, ust_idnr, db.heute()),
    ).lastrowid


def _rc_befunde(conn, kunde_id):
    from app.logik import festschreiben, validierung

    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01', iban = 'DE02120300000000202051',"
        " ust_idnr = 'DE999999999' WHERE id = 1"
    )
    zeit = conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
        " abrechnungsmodus, erfasst_am) VALUES (?, 'Beratung', '2026-03-02', 60,"
        " 'STUNDEN', ?)", (kunde_id, db.jetzt_utc()),
    ).lastrowid
    rid = festschreiben.entwurf_anlegen(conn, kunde_id, [zeit])
    return {b.code: b for b in validierung.pruefe(conn, rid)}


class TestUstIdnrUndLand:
    def test_passende_nummer_meldet_nichts(self, conn, betrieb):
        befunde = _rc_befunde(conn, _eu_kunde(conn, ust_idnr="ATU12345678", land="AT"))
        assert "G27" not in befunde and "G26" not in befunde

    def test_deutsche_nummer_bei_reverse_charge_ist_gelb(self, conn, betrieb):
        """GELB, nicht ROT: ein auslaendisches Unternehmen kann in Deutschland
        registriert sein, ohne hier eine Niederlassung zu haben."""
        from app.logik import validierung

        befunde = _rc_befunde(conn, _eu_kunde(conn, ust_idnr="DE123456789", land="AT"))
        assert befunde["G27"].stufe == validierung.GELB
        assert "§ 19-Rechnung" in befunde["G27"].text

    def test_anderer_eu_staat_als_die_anschrift_ist_gelb(self, conn, betrieb):
        from app.logik import validierung

        befunde = _rc_befunde(conn, _eu_kunde(conn, ust_idnr="ATU12345678", land="FR"))
        assert befunde["G26"].stufe == validierung.GELB
        assert "Österreich" in befunde["G26"].text and "Frankreich" in befunde["G26"].text

    def test_griechenland_fuehrt_el(self, conn, betrieb):
        befunde = _rc_befunde(conn, _eu_kunde(conn, ust_idnr="EL123456789", land="GR"))
        assert "G26" not in befunde


# ---------------------------------------------- Meldungen der Einrichtung

@pytest.fixture
def einrichtungsklient(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import erzeuge_app

    with TestClient(erzeuge_app(tmp_path / "daten" / "web.sqlite")) as klient:
        yield klient


EINRICHTUNG = {
    "name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "start_seq": "1",
}


class TestEinrichtungsmeldungen:
    """Die Meldungen zeigten die Eingabe in Python-Schreibweise ('2OO95')."""

    @pytest.mark.parametrize("feld, wert", [
        ("plz", "2OO95"),
        ("standardsatz_euro", "zwölf"),
        ("zahlungsziel_tage", "14x"),
        ("rundung_intervall_min", "1000000000000"),
    ])
    def test_eingabe_in_deutschen_anfuehrungszeichen(self, einrichtungsklient, feld, wert):
        antwort = einrichtungsklient.post("/einrichtung", data={**EINRICHTUNG, feld: wert})
        assert antwort.status_code == 422
        assert f"„{wert}“" in antwort.text
        assert f"&#39;{wert}&#39;" not in antwort.text
        assert f"'{wert}'" not in antwort.text

    def test_riesiger_stundensatz(self, einrichtungsklient):
        antwort = einrichtungsklient.post(
            "/einrichtung", data={**EINRICHTUNG, "standardsatz_euro": "9" * 20})
        assert antwort.status_code == 422
        assert f"„{'9' * 20}“ ist unplausibel groß" in antwort.text


# ---------------------------------------------------- Entwicklerseiten

@pytest.mark.parametrize("adresse", ["/docs", "/redoc", "/openapi.json"])
def test_keine_entwicklerseiten(einrichtungsklient, adresse):
    einrichtungsklient.post("/einrichtung", data=EINRICHTUNG)
    antwort = einrichtungsklient.get(adresse, follow_redirects=False)
    assert antwort.status_code == 404
    assert "Diese Seite gibt es nicht" in antwort.text
    assert "swagger" not in antwort.text.lower()
    assert '"openapi"' not in antwort.text



# ------------------------------------- Steuerzeichen aus der Belegauslesung

class TestAusgeleseneWerte:
    """Die Textschicht eines PDFs liefert Seitenvorschuebe mit. Landeten sie in
    der Pruefmaske, wiese die zentrale Steuerzeichen-Pruefung das Buchen ab."""

    def test_fund_entfernt_steuerzeichen(self):
        from app.logik.beleg.ergebnis import Fund

        assert Fund("Hetzner\x0cOnline GmbH", "pdf_text").wert == "Hetzner Online GmbH"
        assert Fund("Zeile 1\nZeile 2", "pdf_text").wert == "Zeile 1\nZeile 2"
        assert Fund(12345, "pdf_text").wert == 12345


# ------------------------------------------------ Doppeltes Absenden

def test_jede_seite_schuetzt_vor_doppeltem_absenden(einrichtungsklient):
    """Ein Doppelklick schickte ein Formular zweimal. Der Server faengt das
    meist selbst ab, aber nicht jede zweite Anfrage ist erkennbar falsch: zwei
    gleiche Fahrten an einem Tag gibt es wirklich. Der Schutz in basis.html laesst
    je Formular nur ein Absenden durch und gibt es nach vier Sekunden wieder frei.
    Im Browser geprueft (Chromium): drei schnelle Absendevorgaenge ergeben eine
    Anfrage statt drei."""
    seite = einrichtungsklient.get("/einrichtung").text
    assert "addEventListener('submit'" in seite
    assert "dataset.abgeschickt" in seite and "ereignis.preventDefault()" in seite

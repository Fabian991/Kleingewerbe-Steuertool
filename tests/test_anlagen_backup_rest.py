"""Regressionstests zur letzten Korrekturrunde: Anlagen, Sicherung, Excel.

Die Gegenprobe nach der Fehleranalyse fand Restpunkte: Formularwerte, die
eine 500-Seite statt einer Meldung ergaben (Nutzungsdauer 2**63-1 hielt den
Rechner sogar endlos fest), ein Abgang im Jahr 2099, doppelte Journaleinträge
bei gleichzeitigen Klicks, Sicherungen und Excel-Downloads, die sich bei
Doppelklick gegenseitig zerschrieben, und Zielordner-Meldungen auf Englisch.
Jeder Test nennt den Punkt, den er festhält.
"""

from __future__ import annotations

import sqlite3
import threading
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import anlagen as a
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}

GUT = {
    "bezeichnung": "Schreibtisch", "anschaffung_am": "2026-01-05",
    "brutto_euro": "1.190,00", "ust_satz": "19", "nutzungsdauer_jahre": "3",
    "art": "BEWEGLICH", "aufteilungsquote": "100",
}


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen():
        verbindung = sqlite3.connect(tmp_path / "steuer.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung

    return oeffnen


def _meldung(antwort) -> str:
    """Die Meldung aus dem Redirect, lesbar."""
    return urllib.parse.unquote(antwort.headers["location"])


def _anlegen(klient, **felder):
    return klient.post("/anlagen", data={**GUT, **felder}, follow_redirects=False)


def _anzahl_gueter(datenbank) -> int:
    with datenbank() as conn:
        return conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0]


def _gut_id(klient, datenbank, **felder) -> int:
    antwort = _anlegen(klient, **felder)
    assert "erfolg=" in antwort.headers["location"], _meldung(antwort)
    with datenbank() as conn:
        return conn.execute("SELECT MAX(id) FROM anlagegut").fetchone()[0]


# ------------------------------------------------ 1: Nutzungsdauer 1 bis 50

class TestNutzungsdauer:
    @pytest.mark.parametrize("jahre", ["0", "51", "1000", "10000000",
                                       str(2**63 - 1), str(2**63), "²", "-3"])
    def test_ausserhalb_eins_bis_fuenfzig_wird_gemeldet(self, klient, datenbank, jahre):
        """2**63-1 hielt den Worker endlos in der AfA-Schleife, 10**7 schrieb
        Zeilen bis ins Jahr 10002025. Jetzt: Meldung, nichts gespeichert."""
        antwort = _anlegen(klient, nutzungsdauer_jahre=jahre)
        assert antwort.status_code == 303
        meldung = _meldung(antwort)
        assert "fehler=" in meldung
        assert "Nutzungsdauer" in meldung
        assert _anzahl_gueter(datenbank) == 0

    def test_fuenfzig_jahre_gehen(self, klient, datenbank):
        _gut_id(klient, datenbank, nutzungsdauer_jahre="50")
        with datenbank() as conn:
            assert conn.execute(
                "SELECT MAX(jahr) FROM anlagegut_afa_jahr").fetchone()[0] == 2075

    def test_logik_hat_dieselbe_grenze(self, conn):
        conn.execute(
            "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
            " standardsatz_cent) VALUES (1, 'B', 'S', '20095', 'Hamburg', '1', 8000)")
        with pytest.raises(a.AnlagenFehler, match="höchstens 50"):
            a.anlegen(conn, bezeichnung="Halle", anschaffung_am="2026-01-05",
                      ak_brutto_cent=10_000_000, ust_satz_promille=190,
                      nutzungsdauer_jahre=2**63 - 1)


# ------------------------------------------------ 2: Datumsfenster

class TestAnschaffungsdatum:
    def test_zweistelliges_jahr_wird_abgewiesen(self, klient, datenbank):
        antwort = _anlegen(klient, anschaffung_am="0026-02-10")
        assert "vor dem Jahr 2000" in _meldung(antwort)
        assert _anzahl_gueter(datenbank) == 0

    def test_kompakte_form_wird_als_iso_gespeichert(self, klient, datenbank):
        _gut_id(klient, datenbank, anschaffung_am="20260210")
        with datenbank() as conn:
            assert conn.execute(
                "SELECT anschaffung_am FROM anlagegut").fetchone()[0] == "2026-02-10"

    def test_zukunft_mit_deutschem_datum(self, klient, datenbank):
        meldung = _meldung(_anlegen(klient, anschaffung_am="2099-12-31"))
        assert "31.12.2099" in meldung
        assert "in der Zukunft" in meldung
        assert "2099-12-31" not in meldung
        assert _anzahl_gueter(datenbank) == 0


class TestAbgangsdatum:
    @pytest.mark.parametrize("am", ["2099-12-31", "9999-12-31", "2028-02-29"])
    def test_abgang_in_der_zukunft_wird_abgewiesen(self, klient, datenbank, am):
        """Vorher: 'Abgang erfasst' am 31.12.2099, die AfA-Reihe abgeschnitten."""
        gid = _gut_id(klient, datenbank)
        antwort = klient.post(f"/anlagen/{gid}/abgang", data={
            "am": am, "grund": "verkauft"}, follow_redirects=False)
        meldung = _meldung(antwort)
        assert "in der Zukunft" in meldung
        assert am not in meldung, "deutsches Datum statt ISO"
        with datenbank() as conn:
            assert conn.execute("SELECT abgang_am FROM anlagegut").fetchone()[0] is None

    def test_abgang_vor_2000_wird_abgewiesen(self, klient, datenbank):
        gid = _gut_id(klient, datenbank)
        meldung = _meldung(klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "0026-06-30", "grund": "verkauft"}, follow_redirects=False))
        assert "vor dem Jahr 2000" in meldung

    def test_kompakter_abgang_wird_als_iso_gespeichert(self, klient, datenbank):
        """Vorher stand '20260630' roh in der Datenbank."""
        gid = _gut_id(klient, datenbank)
        klient.post(f"/anlagen/{gid}/abgang", data={"am": "20260630", "grund": "verkauft"},
                    follow_redirects=False)
        with datenbank() as conn:
            assert conn.execute("SELECT abgang_am FROM anlagegut").fetchone()[0] == "2026-06-30"

    def test_meldungen_nennen_das_deutsche_datum(self, klient, datenbank):
        gid = _gut_id(klient, datenbank, anschaffung_am="2026-03-01")
        meldung = _meldung(klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "2026-02-01", "grund": "verkauft"}, follow_redirects=False))
        assert "01.02.2026" in meldung and "01.03.2026" in meldung
        klient.post(f"/anlagen/{gid}/abgang", data={"am": "2026-06-30", "grund": "verkauft"},
                    follow_redirects=False)
        meldung = _meldung(klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "2026-07-30", "grund": "nochmal"}, follow_redirects=False))
        assert "schon am 30.06.2026 abgegangen" in meldung


# ------------------------------------------------ 3: Abgang parallel

class TestAbgangParallel:
    def test_vier_gleichzeitige_abgaenge_ergeben_einen(self, tmp_path):
        """Vier gleichzeitige Klicks auf 'Abgang erfassen' schrieben viermal
        ANLAGEGUT_ABGANG ins unveraenderliche Journal."""
        pfad = tmp_path / "steuer.sqlite"
        conn = db.oeffne(pfad)
        conn.execute(
            "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
            " standardsatz_cent) VALUES (1, 'B', 'S', '20095', 'Hamburg', '1', 8000)")
        gid = a.anlegen(conn, bezeichnung="Monitor", anschaffung_am="2026-01-05",
                        ak_brutto_cent=119000, ust_satz_promille=190,
                        nutzungsdauer_jahre=3)
        conn.close()

        start = threading.Barrier(4)
        ergebnisse: list[str] = []

        def klick():
            eigene = db.verbinde(pfad)
            try:
                start.wait()
                a.abgang(eigene, gid, am="2026-06-30", grund="verkauft", erloes_cent=1000)
                ergebnisse.append("ok")
            except a.AnlagenFehler as fehler:
                ergebnisse.append(str(fehler))
            finally:
                eigene.close()

        faeden = [threading.Thread(target=klick) for _ in range(4)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(timeout=30)

        assert ergebnisse.count("ok") == 1, ergebnisse
        assert all("schon" in e for e in ergebnisse if e != "ok"), ergebnisse
        conn = db.verbinde(pfad)
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE aktion = 'ANLAGEGUT_ABGANG'"
        ).fetchone()[0] == 1
        conn.close()

    def test_doppelte_ruecknahme_schreibt_einen_eintrag(self, klient, datenbank):
        gid = _gut_id(klient, datenbank)
        klient.post(f"/anlagen/{gid}/abgang", data={"am": "2026-06-30", "grund": "verkauft"},
                    follow_redirects=False)
        klient.post(f"/anlagen/{gid}/abgang/zurueck", data={"grund": "vertippt"},
                    follow_redirects=False)
        zweite = klient.post(f"/anlagen/{gid}/abgang/zurueck", data={"grund": "vertippt"},
                             follow_redirects=False)
        assert "nicht abgegangen" in _meldung(zweite)
        with datenbank() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM journal WHERE aktion = 'ANLAGEGUT_ABGANG_ZURUECK'"
            ).fetchone()[0] == 1


# ------------------------------------------------ 4: Grenzwerte ohne 500

class TestGrenzwerte:
    @pytest.mark.parametrize("feld", ["brutto_euro", "netto_euro"])
    def test_riesenbetrag_wird_gemeldet(self, klient, datenbank, feld):
        antwort = _anlegen(klient, **{feld: "9223372036854775808"})
        assert antwort.status_code == 303
        assert "unplausibel groß" in _meldung(antwort)
        assert _anzahl_gueter(datenbank) == 0

    @pytest.mark.parametrize("satz", ["inf", "1e400", "nan", "-inf", "²"])
    def test_steuersatz_unendlich_ist_kein_steuersatz(self, klient, datenbank, satz):
        antwort = _anlegen(klient, ust_satz=satz)
        assert antwort.status_code == 303
        assert "kein Steuersatz" in _meldung(antwort)
        assert _anzahl_gueter(datenbank) == 0

    @pytest.mark.parametrize("beleg", [str(2**63), str(2**70), "²", "abc", "-1"])
    def test_beleg_id_ausserhalb_wird_gemeldet(self, klient, datenbank, beleg):
        antwort = _anlegen(klient, beleg_id=beleg)
        assert antwort.status_code == 303
        assert "Diesen Beleg gibt es nicht" in _meldung(antwort)

    def test_riesenerloes_wird_gemeldet(self, klient, datenbank):
        gid = _gut_id(klient, datenbank)
        antwort = klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "2026-06-30", "grund": "verkauft",
            "erloes_euro": "9223372036854775808"}, follow_redirects=False)
        assert antwort.status_code == 303
        assert "unplausibel groß" in _meldung(antwort)
        with datenbank() as conn:
            assert conn.execute("SELECT abgang_am FROM anlagegut").fetchone()[0] is None

    @pytest.mark.parametrize("pfad", ["abgang", "abgang/zurueck"])
    @pytest.mark.parametrize("gid", [2**63, 2**70])
    def test_riesige_kennung_in_der_adresse(self, klient, pfad, gid):
        """Ab 19 Stellen antwortet die zentrale Pruefung in app/main.py."""
        antwort = klient.post(f"/anlagen/{gid}/{pfad}", data={
            "am": "2026-06-30", "grund": "verkauft"}, follow_redirects=False)
        assert antwort.status_code == 404
        assert "Diese Seite gibt es nicht" in antwort.text

    def test_quote_mit_hochgestellter_ziffer(self, klient, datenbank):
        antwort = _anlegen(klient, aufteilungsquote="²")
        assert "kein Prozentsatz" in _meldung(antwort)
        assert _anzahl_gueter(datenbank) == 0


# ------------------------------------------------ 12: Druck

class TestDruck:
    def test_formulare_sind_im_druck_ausgeblendet(self, klient, datenbank):
        _gut_id(klient, datenbank)
        text = klient.get("/anlagen").text
        kopf = text.index('<h2 id="neu">')
        assert 'class="nicht-drucken"' in text[kopf - 200:kopf]
        assert 'class="zeile nicht-drucken"' in text
        assert 'class="aktionen nicht-drucken"' in text


# ================================================= Sicherung und Excel

from app.logik import backup  # noqa: E402
from app.logik import export_excel as ex  # noqa: E402


@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


@pytest.fixture
def sicherungsklient(datenordner):
    """Die Anwendung mit der Datenbank IM Datenordner, wie im Betrieb."""
    with TestClient(erzeuge_app(datenordner / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


def _einrichten(pfad) -> None:
    conn = db.oeffne(pfad)
    conn.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
        " standardsatz_cent) VALUES (1, 'B', 'S', '20095', 'Hamburg', '1', 8000)")
    conn.close()


# ------------------------------------------------ 5: Sicherung parallel

class TestSicherungParallel:
    def test_doppelklick_ergibt_zwei_heile_archive(self, datenordner):
        """Zwei gleichzeitige Sicherungen waehlten denselben Namen: eine
        scheiterte mit '[Errno 2] …', die andere meldete 'geschrieben' --
        und ihr Archiv fiel danach durch die Pruefsummenpruefung."""
        pfad = datenordner / "steuer.sqlite"
        _einrichten(pfad)
        beleg = datenordner / "belege" / "2026" / "beleg.pdf"
        beleg.parent.mkdir(parents=True)
        beleg.write_bytes(b"%PDF-1.7 " + b"x" * 200_000)

        start = threading.Barrier(4)
        archive: list[backup.Archiv] = []
        fehler: list[BaseException] = []

        def klick():
            conn = db.verbinde(pfad)
            try:
                start.wait()
                archive.append(backup.erzeuge(conn, datenordner))
            except BaseException as f:  # noqa: BLE001 -- wird unten gemeldet
                fehler.append(f)
            finally:
                conn.close()

        faeden = [threading.Thread(target=klick) for _ in range(4)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(timeout=120)

        assert not fehler, fehler
        namen = {a.pfad.name for a in archive}
        assert len(namen) == 4, namen
        conn = db.verbinde(pfad)
        for zeile in conn.execute("SELECT id FROM backup_log").fetchall():
            ergebnis = backup.pruefe(conn, zeile["id"])
            assert ergebnis.heil, ergebnis.meldung
        assert not list((datenordner / "backup").glob("*.teil"))
        conn.close()

    def test_reservierter_name_wird_uebersprungen(self, tmp_path):
        (tmp_path / "2026-10-19_steuer-backup.zip").write_bytes(b"")
        pfad = backup._reserviere(tmp_path, "2026-10-19")
        assert pfad.name == "2026-10-19_steuer-backup-2.zip"
        assert pfad.exists(), "der Name ist belegt, bevor geschrieben wird"

    def test_abbruch_gibt_den_reservierten_namen_frei(self, datenordner, monkeypatch):
        _einrichten(datenordner / "steuer.sqlite")
        conn = db.verbinde(datenordner / "steuer.sqlite")

        def kaputt(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(backup.zipfile.ZipFile, "write", kaputt)
        with pytest.raises(OSError):
            backup.erzeuge(conn, datenordner)
        assert not list((datenordner / "backup").iterdir())
        conn.close()

    def test_fehlermeldung_ohne_errno(self, sicherungsklient, monkeypatch):
        def kaputt(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "/x/2026_steuer.zip")

        monkeypatch.setattr(backup, "pruefsumme", kaputt)
        meldung = _meldung(sicherungsklient.post("/backup", follow_redirects=False))
        assert "fehlgeschlagen" in meldung
        assert "Errno" not in meldung and "No such file" not in meldung
        assert "fehlt" in meldung

    @pytest.mark.parametrize("fehler, erwartet", [
        (OSError(28, "No space left on device"), "Platte ist voll"),
        (PermissionError(13, "Permission denied", "/x"), "keine Berechtigung"),
        (OSError(30, "Read-only file system"), "schreibgeschützt"),
        (OSError(36, "File name too long"), "zu lang"),
        (OSError(9999, "Something"), "Systemfehler Nr. 9999"),
        (sqlite3.OperationalError("database is locked"), "gerade belegt"),
    ])
    def test_fehlertext_ist_deutsch(self, fehler, erwartet):
        text = backup.fehlertext(fehler)
        assert erwartet in text
        assert "Errno" not in text

    def test_riesige_kennung_beim_pruefen(self, sicherungsklient):
        """Ab 19 Stellen antwortet die zentrale Pruefung in app/main.py."""
        antwort = sicherungsklient.post(f"/backup/{2**63}/pruefen", follow_redirects=False)
        assert antwort.status_code == 404
        assert "Diese Seite gibt es nicht" in antwort.text


# ------------------------------------------------ 6: Excel parallel

class TestExcelParallel:
    def test_download_kommt_aus_dem_speicher(self, sicherungsklient, datenordner):
        """Alle Downloads schrieben in dieselbe steuer-daten.xlsx im
        Datenordner, waehrend eine fruehere Anfrage sie noch auslieferte."""
        antwort = sicherungsklient.get("/backup/excel")
        assert antwort.status_code == 200
        assert not (datenordner / ex.DATEINAME).exists()
        assert "attachment" in antwort.headers["content-disposition"]
        assert ex.DATEINAME in antwort.headers["content-disposition"]

    def test_acht_gleichzeitige_downloads_sind_alle_heil(self, sicherungsklient):
        import io

        from openpyxl import load_workbook

        start = threading.Barrier(8)
        inhalte: list[bytes] = []

        def laden():
            start.wait()
            inhalte.append(sicherungsklient.get("/backup/excel").content)

        faeden = [threading.Thread(target=laden) for _ in range(8)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(timeout=120)
        assert len(inhalte) == 8
        for inhalt in inhalte:
            assert "Lies mich" in load_workbook(io.BytesIO(inhalt)).sheetnames

    def test_schreibe_ersetzt_atomar(self, conn, betrieb, tmp_path):
        """Die Datei im Ordner wird nie halb gelesen: gleichzeitige Laeufe
        schreiben je in eine eigene Arbeitsdatei und benennen um."""
        from openpyxl import load_workbook

        ziel = tmp_path / "aus" / ex.DATEINAME
        pfad_db = tmp_path / "test.sqlite"
        start = threading.Barrier(4)
        fehler: list[BaseException] = []

        def schreiben():
            eigene = db.verbinde(pfad_db)
            try:
                start.wait()
                ex.schreibe(eigene, ziel)
            except BaseException as f:  # noqa: BLE001
                fehler.append(f)
            finally:
                eigene.close()

        faeden = [threading.Thread(target=schreiben) for _ in range(4)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(timeout=120)
        assert not fehler, fehler
        assert "Lies mich" in load_workbook(ziel).sheetnames
        assert [p.name for p in ziel.parent.iterdir()] == [ex.DATEINAME], "keine Arbeitsreste"


# ------------------------------------------------ 7, 8, 9: Zielordner

class TestZielordner:
    def test_ueberlanger_pfad_ergibt_eine_meldung(self, sicherungsklient, tmp_path):
        antwort = sicherungsklient.post(
            "/backup/ziel", data={"pfad": str(tmp_path / ("z" * 300))},
            follow_redirects=False)
        assert antwort.status_code == 303
        meldung = _meldung(antwort)
        assert "fehler=" in meldung
        assert "Errno" not in meldung and "File name" not in meldung

    def test_unbekannter_benutzer_mit_tilde(self, sicherungsklient):
        antwort = sicherungsklient.post(
            "/backup/ziel", data={"pfad": "~unbekannterbenutzer4711/Sicherung"},
            follow_redirects=False)
        assert antwort.status_code == 303
        meldung = _meldung(antwort)
        assert "Benutzer" in meldung
        assert "home directory" not in meldung

    def test_unbekannter_benutzer_als_zweitziel(self, sicherungsklient):
        antwort = sicherungsklient.post(
            "/backup/ziel", data={"pfad": "", "zweitziel": "~unbekannterbenutzer4711/x"},
            follow_redirects=False)
        assert antwort.status_code == 303
        assert "Benutzer" in _meldung(antwort)

    def test_gespeicherte_tilde_legt_die_seite_nicht_lahm(self, sicherungsklient,
                                                          datenordner):
        conn = sqlite3.connect(datenordner / "steuer.sqlite")
        conn.execute("UPDATE betrieb SET backup_pfad = '~unbekannterbenutzer4711/x'")
        conn.commit()
        conn.close()
        assert sicherungsklient.get("/backup").status_code == 200
        meldung = _meldung(sicherungsklient.post("/backup", follow_redirects=False))
        assert "fehlgeschlagen" in meldung
        assert "home directory" not in meldung

    def test_verschachtelter_ordner_auf_der_internen_platte(self, sicherungsklient, tmp_path):
        """Regression: <aussen>/Sicherungen/Steuer-Tool mit fehlendem
        'Sicherungen' wurde abgewiesen ('Hängt das Laufwerk?'), obwohl auf der
        internen Platte kein Laufwerk fehlen kann."""
        aussen = tmp_path / "aussen"
        aussen.mkdir()
        ziel = aussen / "Sicherungen" / "Steuer-Tool"
        antwort = sicherungsklient.post("/backup/ziel", data={"pfad": str(ziel)},
                                        follow_redirects=False)
        assert antwort.headers["location"] == "/backup", _meldung(antwort)
        assert ziel.is_dir()

    def test_fehlendes_laufwerk_unter_einhaengepunkt(self, sicherungsklient, tmp_path,
                                                     monkeypatch):
        volumes = tmp_path / "Volumes"
        volumes.mkdir()
        monkeypatch.setattr(backup, "EINHAENGEPUNKTE", ((volumes, 1),))
        ziel = volumes / "Backup-Platte" / "Sicherungen" / "Steuer-Tool"
        meldung = _meldung(sicherungsklient.post(
            "/backup/ziel", data={"pfad": "", "zweitziel": str(ziel)},
            follow_redirects=False))
        assert "Hängt das Laufwerk?" in meldung
        assert str(volumes / "Backup-Platte") in meldung
        assert not (volumes / "Backup-Platte").exists()

    def test_eingehaengtes_laufwerk_bekommt_die_ganze_kette(self, sicherungsklient, tmp_path,
                                                            monkeypatch):
        volumes = tmp_path / "Volumes"
        (volumes / "Backup-Platte").mkdir(parents=True)
        monkeypatch.setattr(backup, "EINHAENGEPUNKTE", ((volumes, 1),))
        ziel = volumes / "Backup-Platte" / "Sicherungen" / "Steuer-Tool"
        antwort = sicherungsklient.post("/backup/ziel", data={"pfad": "", "zweitziel": str(ziel)},
                                        follow_redirects=False)
        assert antwort.headers["location"] == "/backup", _meldung(antwort)
        assert ziel.is_dir()

    def test_einhaengeordner_der_echten_wurzeln(self):
        from pathlib import Path

        assert backup.einhaengeordner(Path("/Volumes/Platte/a/b")) == Path("/Volumes/Platte")
        assert backup.einhaengeordner(Path("/media/Platte/a")) == Path("/media/Platte")
        assert backup.einhaengeordner(Path("/mnt/usb/a")) == Path("/mnt/usb")
        assert backup.einhaengeordner(
            Path("/run/media/erika/Platte/a")) == Path("/run/media/erika/Platte")
        assert backup.einhaengeordner(Path("/home/erika/Sicherung")) is None
        assert backup.einhaengeordner(Path("/Volumes")) is None

    def test_leeres_ziel_und_standardordner_als_zweitziel(self, sicherungsklient,
                                                          datenordner):
        """Leeres erstes Ziel heisst <Daten>/backup. Dasselbe als Zweitziel
        wurde gespeichert, und jede Sicherung meldete danach rot auf Englisch
        ('… are the same file') mit falschem Hinweis aufs Laufwerk."""
        antwort = sicherungsklient.post(
            "/backup/ziel", data={"pfad": "", "zweitziel": str(datenordner / "backup")},
            follow_redirects=False)
        assert "denselben Ordner" in _meldung(antwort)
        conn = sqlite3.connect(datenordner / "steuer.sqlite")
        assert conn.execute(
            "SELECT backup_pfad, backup_zweitziel FROM betrieb").fetchone() == (None, None)
        conn.close()

    def test_abgewiesenes_paar_legt_nichts_an(self, sicherungsklient, tmp_path):
        ziel = tmp_path / "neu" / "gleich"
        sicherungsklient.post("/backup/ziel", data={"pfad": str(ziel), "zweitziel": str(ziel)},
                              follow_redirects=False)
        assert not ziel.exists()

    def test_alte_einstellung_mit_gleichem_ziel(self, sicherungsklient, datenordner):
        conn = sqlite3.connect(datenordner / "steuer.sqlite")
        conn.execute("UPDATE betrieb SET backup_zweitziel = ?", (str(datenordner / "backup"),))
        conn.commit()
        conn.close()
        meldung = _meldung(sicherungsklient.post("/backup", follow_redirects=False))
        assert "derselbe Ordner wie das erste" in meldung
        assert "same file" not in meldung
        assert "Laufwerk?" not in meldung


# ------------------------------------------------ 11: Excel-Klartexte

class TestExcelKlartext:
    def test_steuerkategorie_e_im_klartext(self, conn, betrieb, kunde_id, tmp_path):
        from openpyxl import load_workbook

        rid = conn.execute(
            "INSERT INTO rechnung (typ, kunde_id, datum, netto_cent, brutto_cent, status,"
            " erstellt_am) VALUES ('RECHNUNG', ?, '2026-03-02', 10000, 10000, 'ENTWURF', ?)",
            (kunde_id, db.jetzt_utc()),
        ).lastrowid
        conn.execute(
            "INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung,"
            " menge_tausendstel, einzelpreis_cent, betrag_cent, quelle)"
            " VALUES (?, 1, 'E', 1000, 10000, 10000, 'FREI')", (rid,))
        mappe = load_workbook(ex.schreibe(conn, tmp_path / "x.xlsx"))
        blatt = mappe["Rechnungspositionen"]
        koepfe = [z.value for z in blatt[1]]
        zeile = [z.value for z in blatt[2]]
        assert zeile[koepfe.index("Steuerkategorie")] == "steuerbefreit (Kleinunternehmer)"
        # Ein "E" in einer anderen Spalte bleibt ein E.
        assert zeile[koepfe.index("Bezeichnung")] == "E"

    def test_rolle_der_belegdatei_im_klartext(self):
        assert ex._wert("ORIGINAL", "text") == "Original"
        assert ex._wert("ABGELEITETES_XML", "text") == "abgeleitetes XML"
        assert ex._wert("INTERN", "text") == "intern (nie berechnet)"

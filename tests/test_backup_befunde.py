"""Regressionstests zu den Befunden der Fehleranalyse im Bereich Sicherung.

Sicherung, Excel-Beiblatt, Journal, Leistungskatalog-Journal und
Datenbankstart. Jeder Test nennt den Befund, den er festhaelt; die Nummern
folgen den Entscheidungen zur Befunddatei.
"""

from __future__ import annotations

import io
import os
import pathlib
import sqlite3
import time
import urllib.parse
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import db
from app.logik import backup, journal_export
from app.logik import export_excel as ex
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


@pytest.fixture
def klient(datenordner):
    """Die Anwendung mit der Datenbank IM Datenordner, wie im Betrieb."""
    with TestClient(erzeuge_app(datenordner / "web.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def einhaengepunkt(tmp_path, monkeypatch):
    """tmp_path/Volumes gilt als Einhaengepunkt wie /Volumes auf dem Mac.

    Seit verschachtelte Zielordner auf der internen Platte wieder samt
    Elternkette angelegt werden, heisst "Ordner fehlt" nur noch unter einem
    Einhaengepunkt "Laufwerk nicht da". Die Tests unten simulierten /Volumes
    mit einem gewoehnlichen Ordner und brauchen deshalb diese Einstellung.
    """
    monkeypatch.setattr(backup, "EINHAENGEPUNKTE", ((tmp_path / "Volumes", 1),))
    return tmp_path / "Volumes"


def _db(datenordner) -> sqlite3.Connection:
    conn = sqlite3.connect(datenordner / "web.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _meldung(antwort) -> str:
    """Die Meldung aus dem Redirect, lesbar."""
    return urllib.parse.unquote(antwort.headers["location"])


@pytest.fixture
def gefuellt(conn, betrieb, kunde_id, datenordner):
    """Genau EINE festgeschriebene Rechnung mit PDF, ein Zeiteintrag."""
    pdf = datenordner / "ausgangsrechnungen" / "2026" / "RE-2026-0001_Kunde.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.7 Platzhalter")
    conn.execute(
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                              netto_cent, brutto_cent, status, festgeschrieben_am,
                              erstellt_am, pdf_pfad)
        VALUES ('RECHNUNG', 'RE-2026-0001', 1, 2026, ?, '2026-03-02', 10000, 10000,
                'FESTGESCHRIEBEN', ?, ?, 'ausgangsrechnungen/2026/RE-2026-0001_Kunde.pdf')
        """,
        (kunde_id, db.jetzt_utc(), db.jetzt_utc()),
    )
    conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
        " VALUES (?, 'Beratung', '2026-03-02', 60, ?)",
        (kunde_id, db.jetzt_utc()),
    )
    return pdf


# ------------------------------------------------- 1, 3, 4, 12: Zielordner

class TestZielordner:
    def test_datenordner_selbst_wird_abgewiesen(self, klient, datenordner):
        """Befund 1: als Ziel griff der Ausschluss des Zielordners fuer JEDE
        Datei; das Archiv enthielt nur Datenbank und Tabelle, die Pruefung
        war gruen."""
        antwort = klient.post("/backup/ziel", data={"pfad": str(datenordner)},
                              follow_redirects=False)
        assert "nicht in den Datenordner oder darüber" in _meldung(antwort)
        conn = _db(datenordner)
        assert conn.execute("SELECT backup_pfad FROM betrieb").fetchone()[0] is None

    def test_elternordner_des_datenordners_wird_abgewiesen(self, klient, datenordner):
        antwort = klient.post("/backup/ziel", data={"pfad": str(datenordner.parent)},
                              follow_redirects=False)
        assert "nicht in den Datenordner oder darüber" in _meldung(antwort)

    def test_zweitziel_im_datenordner_oder_darueber_wird_abgewiesen(self, klient, datenordner):
        antwort = klient.post(
            "/backup/ziel", data={"pfad": "", "zweitziel": str(datenordner.parent)},
            follow_redirects=False,
        )
        assert "nicht in den Datenordner oder darüber" in _meldung(antwort)

    def test_unterordner_des_datenordners_bleibt_erlaubt(self, klient, datenordner):
        antwort = klient.post("/backup/ziel", data={"pfad": str(datenordner / "sicherung")},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/backup"
        klient.post("/backup", follow_redirects=False)
        klient.post("/backup", follow_redirects=False)
        conn = _db(datenordner)
        pfad = conn.execute("SELECT pfad FROM backup_log ORDER BY id DESC").fetchone()[0]
        with zipfile.ZipFile(pfad) as zip_:
            namen = zip_.namelist()
        assert not any(n.startswith("sicherung/") for n in namen), namen

    def test_erzeuge_weist_den_datenordner_selbst_ab(self, conn, gefuellt, datenordner):
        """Auch unterhalb der Oberflaeche: ein Archiv ohne Belege entsteht nicht."""
        with pytest.raises(ValueError, match="nicht in den Datenordner"):
            backup.erzeuge(conn, datenordner, datenordner)
        with pytest.raises(ValueError, match="nicht in den Datenordner"):
            backup.erzeuge(conn, datenordner, datenordner.parent)

    def test_relativer_pfad_wird_absolut_gespeichert(self, klient, datenordner, tmp_path,
                                                     monkeypatch):
        """Befund 4: 'sicherung-relativ' hing vom Arbeitsverzeichnis ab, und
        aus einem anderen gestartet 'fehlte' jede Sicherung."""
        arbeit = tmp_path / "arbeitsverzeichnis"
        arbeit.mkdir()
        monkeypatch.chdir(arbeit)
        antwort = klient.post("/backup/ziel", data={"pfad": "sicherung-relativ"},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/backup"
        conn = _db(datenordner)
        gespeichert = conn.execute("SELECT backup_pfad FROM betrieb").fetchone()[0]
        assert gespeichert == str(arbeit / "sicherung-relativ")
        klient.post("/backup", follow_redirects=False)
        pfad = conn.execute("SELECT pfad FROM backup_log").fetchone()[0]
        assert pathlib.Path(pfad).is_absolute()
        assert pathlib.Path(pfad).exists()

    def test_tilde_wird_aufgeloest_gespeichert(self, klient, datenordner, tmp_path,
                                               monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        klient.post("/backup/ziel", data={"pfad": "~/Sicherung"}, follow_redirects=False)
        conn = _db(datenordner)
        assert conn.execute("SELECT backup_pfad FROM betrieb").fetchone()[0] == str(
            tmp_path / "Sicherung"
        )

    def test_zeilenumbruch_im_pfad_wird_abgewiesen(self, klient, datenordner, tmp_path):
        antwort = klient.post("/backup/ziel", data={"pfad": f"{tmp_path}/abc\ndef"},
                              follow_redirects=False)
        assert "Zeilenumbrüche oder Steuerzeichen" in _meldung(antwort)
        assert not (tmp_path / "abc\ndef").exists()

    def test_gleicher_ordner_in_anderer_schreibweise(self, klient, tmp_path):
        """Befund 12: '/x/gleich' und '/x/gleich/' sind derselbe Ordner."""
        ziel = tmp_path / "gleich"
        for zweit in (str(ziel) + "/", str(ziel / ".." / "gleich")):
            antwort = klient.post("/backup/ziel", data={"pfad": str(ziel), "zweitziel": zweit},
                                  follow_redirects=False)
            assert "denselben Ordner" in _meldung(antwort), zweit

    def test_datei_statt_ordner_hat_eine_deutsche_meldung(self, klient, tmp_path):
        """Befund 12: statt '[Errno 17] File exists'."""
        datei = tmp_path / "datei.txt"
        datei.write_text("x")
        antwort = klient.post("/backup/ziel", data={"pfad": str(datei)},
                              follow_redirects=False)
        meldung = _meldung(antwort)
        assert "ist eine Datei, kein Ordner" in meldung
        assert "Errno" not in meldung

    def test_fehlender_elternordner_heisst_platte_nicht_da(self, klient, tmp_path,
                                                           einhaengepunkt):
        """Befund 3: /Volumes/Backup-Platte/Steuer-Tool wurde als gewoehnlicher
        Ordner auf der Systemplatte angelegt, wenn die Platte nicht hing."""
        volumes = tmp_path / "Volumes"
        volumes.mkdir()
        ziel = volumes / "Backup-Platte" / "Steuer-Tool"
        antwort = klient.post("/backup/ziel", data={"pfad": "", "zweitziel": str(ziel)},
                              follow_redirects=False)
        assert "Hängt das Laufwerk?" in _meldung(antwort)
        assert not (volumes / "Backup-Platte").exists(), "nichts auf der Systemplatte angelegt"

    def test_nur_der_letzte_ordner_wird_angelegt(self, klient, tmp_path):
        platte = tmp_path / "Platte"
        platte.mkdir()
        antwort = klient.post("/backup/ziel", data={"pfad": str(platte / "Steuer-Tool")},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/backup"
        assert (platte / "Steuer-Tool").is_dir()

    def test_schreibprobe_beim_speichern(self, klient, tmp_path, monkeypatch):
        """Befund 'vermutet': ohne Schreibrecht scheiterte erst die Sicherung
        mit errno-Text. Der Prozess laeuft hier als root, daher per Attrappe."""
        ziel = tmp_path / "nur-lesen"
        ziel.mkdir()
        echt = os.access

        def ohne_schreibrecht(pfad, modus, *rest, **kw):
            if pathlib.Path(pfad) == ziel and modus == os.W_OK:
                return False
            return echt(pfad, modus, *rest, **kw)

        monkeypatch.setattr(os, "access", ohne_schreibrecht)
        antwort = klient.post("/backup/ziel", data={"pfad": str(ziel)}, follow_redirects=False)
        assert "kann nicht geschrieben werden" in _meldung(antwort)


# ------------------------------------------------- 3, 7, 8: Archivinhalt

class TestArchivinhalt:
    def test_zweitkopie_ohne_platte_wird_rot_gemeldet(self, conn, gefuellt, datenordner,
                                                       tmp_path, einhaengepunkt):
        """Befund 3: die 'Zweitkopie' lag auf der Systemplatte und galt als Erfolg."""
        volumes = tmp_path / "Volumes"
        volumes.mkdir()
        conn.execute("UPDATE betrieb SET backup_zweitziel = ? WHERE id = 1",
                     (str(volumes / "Backup" / "Steuer-Tool"),))
        archiv = backup.erzeuge(conn, datenordner)
        assert archiv.pfad.exists()
        assert archiv.zweitkopie is None
        assert "ist nicht entstanden" in archiv.zweitkopie_fehler
        assert "Hängt das Laufwerk?" in archiv.zweitkopie_fehler
        assert not (volumes / "Backup").exists()

    def test_zweitkopie_fehlschlag_erscheint_als_fehler_auf_der_seite(self, klient, datenordner,
                                                                      tmp_path, einhaengepunkt):
        volumes = tmp_path / "Volumes"
        volumes.mkdir()
        klient.post("/backup/ziel",
                    data={"pfad": "", "zweitziel": str(volumes / "Backup" / "Steuer-Tool")},
                    follow_redirects=False)
        # Das Ziel liess sich nicht anlegen; direkt in die Datenbank, wie eine
        # Einstellung von frueher, als die Platte noch hing.
        conn = _db(datenordner)
        conn.execute("UPDATE betrieb SET backup_zweitziel = ? WHERE id = 1",
                     (str(volumes / "Backup" / "Steuer-Tool"),))
        conn.commit()
        antwort = klient.post("/backup", follow_redirects=False)
        meldung = _meldung(antwort)
        assert meldung.startswith("/backup?fehler=")
        assert "gelegt" not in meldung
        assert "Hängt das Laufwerk?" in meldung

    def test_fehlender_zielordner_der_platte_laesst_die_sicherung_scheitern(
        self, conn, gefuellt, datenordner, tmp_path, einhaengepunkt
    ):
        conn.execute("UPDATE betrieb SET backup_pfad = ? WHERE id = 1",
                     (str(tmp_path / "Volumes" / "Backup"),))
        with pytest.raises(OSError, match="Hängt das Laufwerk"):
            backup.erzeuge(conn, datenordner)
        assert not (tmp_path / "Volumes").exists()

    def test_zweitziel_im_datenordner_wandert_nicht_ins_archiv(self, klient, datenordner):
        """Befund 7: Archive wuchsen mit jedem Lauf."""
        klient.post("/backup/ziel", data={"pfad": str(datenordner / "ziel1"),
                                          "zweitziel": str(datenordner / "zweitkopie")},
                    follow_redirects=False)
        klient.post("/backup", follow_redirects=False)
        klient.post("/backup", follow_redirects=False)
        conn = _db(datenordner)
        pfad = conn.execute("SELECT pfad FROM backup_log ORDER BY id DESC").fetchone()[0]
        with zipfile.ZipFile(pfad) as zip_:
            namen = zip_.namelist()
        assert not any("zweitkopie/" in n or "ziel1/" in n for n in namen), namen

    def test_alte_archive_nach_namensmuster_bleiben_draussen(self, conn, gefuellt, datenordner):
        """Befund 7: ein frueherer Zielordner oder ein hineinkopiertes ZIP."""
        alt = datenordner / "alter-zielordner"
        alt.mkdir()
        (alt / "2026-01-01_steuer-backup.zip").write_bytes(b"PK alt")
        (alt / "2026-01-02_steuer-backup-2.zip").write_bytes(b"PK alt")
        (alt / "2026-01-03_steuer-backup.zip.teil").write_bytes(b"PK halb")
        (alt / "notiz.txt").write_text("gehoert dazu")
        archiv = backup.erzeuge(conn, datenordner)
        with zipfile.ZipFile(archiv.pfad) as zip_:
            namen = zip_.namelist()
        assert "alter-zielordner/notiz.txt" in namen
        assert not any(n.endswith((".zip", ".teil")) for n in namen), namen

    def test_laufende_datenbank_bleibt_draussen_wie_immer_sie_heisst(self, klient, datenordner):
        """Befund 8: web.sqlite samt -wal und -shm lag im Archiv."""
        klient.post("/backup", follow_redirects=False)
        conn = _db(datenordner)
        pfad = conn.execute("SELECT pfad FROM backup_log").fetchone()[0]
        with zipfile.ZipFile(pfad) as zip_:
            namen = zip_.namelist()
        assert backup.DB_IM_ARCHIV in namen
        assert not any(n.startswith("web.sqlite") for n in namen), namen

    def test_backup_log_traegt_absoluten_pfad(self, conn, gefuellt, datenordner, tmp_path,
                                               monkeypatch):
        monkeypatch.chdir(tmp_path)
        backup.erzeuge(conn, datenordner, pathlib.Path("relativ-ziel"))
        pfad = conn.execute("SELECT pfad FROM backup_log").fetchone()[0]
        assert pathlib.Path(pfad).is_absolute()
        assert pathlib.Path(pfad) == tmp_path / "relativ-ziel" / pathlib.Path(pfad).name


# ------------------------------------------------- 1, 5, 9: Pruefung

class TestPruefung:
    def test_pluralformen(self, conn, gefuellt, datenordner):
        """Befund 9: '1 festgeschriebene Rechnungen'."""
        archiv = backup.erzeuge(conn, datenordner)
        bid = conn.execute("SELECT id FROM backup_log").fetchone()[0]
        ergebnis = backup.pruefe(conn, bid)
        assert ergebnis.heil, ergebnis.meldung
        assert ergebnis.meldung == (
            "Archiv gelesen: 1 festgeschriebene Rechnung, 1 Zeiteintrag, 1 Belegdatei."
        )
        assert archiv.dateien == 4  # DB, Tabelle, Spiegel, PDF

    def test_mehrzahl_bleibt_mehrzahl(self, conn, gefuellt, datenordner, kunde_id):
        for _ in range(2):
            conn.execute(
                "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
                " VALUES (?, 'Beratung', '2026-03-03', 60, ?)", (kunde_id, db.jetzt_utc()),
            )
        backup.erzeuge(conn, datenordner)
        bid = conn.execute("SELECT id FROM backup_log").fetchone()[0]
        assert "3 Zeiteinträge" in backup.pruefe(conn, bid).meldung

    def test_verzeichnete_datei_fehlt_im_archiv(self, conn, gefuellt, datenordner):
        """Befund 1: die Datenbank nennt ein PDF, das Archiv hat es nicht --
        die Pruefung darf nicht gruen sagen."""
        gefuellt.unlink()   # das PDF ist weg, der Eintrag bleibt
        backup.erzeuge(conn, datenordner)
        bid = conn.execute("SELECT id FROM backup_log").fetchone()[0]
        ergebnis = backup.pruefe(conn, bid)
        assert not ergebnis.heil
        assert "fehlt 1 verzeichnete Belegdatei" in ergebnis.meldung
        assert "RE-2026-0001_Kunde.pdf" in ergebnis.meldung
        assert conn.execute("SELECT geprueft FROM backup_log").fetchone()[0] == 0

    def test_belegdatei_aus_der_belegtabelle_wird_verglichen(self, conn, gefuellt, datenordner):
        conn.execute(
            "INSERT INTO lieferant (id, name, angelegt_am) VALUES (1, 'Hetzner', ?)",
            (db.heute(),),
        )
        conn.execute(
            "INSERT INTO beleg (id, lieferant_id, belegdatum, betrag_brutto_cent,"
            " betrag_betrieblich_cent, erfasst_am) VALUES (1, 1, '2026-03-14', 1190, 1190, ?)",
            (db.jetzt_utc(),),
        )
        conn.execute(
            "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse, importiert_am)"
            " VALUES (1, 'belege/2026/2026-03-14_Hetzner.pdf', 'h.pdf', 'abc', 10, ?)",
            (db.jetzt_utc(),),
        )
        backup.erzeuge(conn, datenordner)
        bid = conn.execute("SELECT id FROM backup_log").fetchone()[0]
        ergebnis = backup.pruefe(conn, bid)
        assert not ergebnis.heil
        assert "2026-03-14_Hetzner.pdf" in ergebnis.meldung
        # Liegt die Datei da, ist alles gut.
        beleg = datenordner / "belege" / "2026" / "2026-03-14_Hetzner.pdf"
        beleg.parent.mkdir(parents=True)
        beleg.write_bytes(b"%PDF-1.7 Beleg")
        backup.erzeuge(conn, datenordner)
        bid = conn.execute("SELECT id FROM backup_log ORDER BY id DESC").fetchone()[0]
        ergebnis = backup.pruefe(conn, bid)
        assert ergebnis.heil, ergebnis.meldung
        assert "2 Belegdateien" in ergebnis.meldung

    def test_fehlgeschlagene_pruefung_nimmt_geprueft_zurueck(self, conn, gefuellt, datenordner):
        """Befund 5: ein frueher geprueftes, inzwischen beschaedigtes Archiv
        blieb 'geprueft'; das Dashboard beruhigte mit 'heute'."""
        archiv = backup.erzeuge(conn, datenordner)
        bid = conn.execute("SELECT id FROM backup_log").fetchone()[0]
        assert backup.pruefe(conn, bid).heil
        assert backup.letztes_geprueft(conn) is not None

        roh = bytearray(archiv.pfad.read_bytes())
        roh[7] ^= 0x55
        archiv.pfad.write_bytes(bytes(roh))
        ergebnis = backup.pruefe(conn, bid)
        assert not ergebnis.heil
        assert "Prüfsumme" in ergebnis.meldung
        assert conn.execute("SELECT geprueft FROM backup_log").fetchone()[0] == 0
        assert backup.letztes_geprueft(conn) is None
        eintrag = conn.execute(
            "SELECT * FROM journal WHERE aktion = 'BACKUP_PRUEFUNG_FEHLGESCHLAGEN'"
        ).fetchone()
        assert eintrag is not None
        assert "Prüfsumme" in eintrag["begruendung"]
        assert eintrag["objekt_id"] == bid

    def test_dashboard_beruhigt_nach_fehlschlag_nicht_mehr(self, klient, datenordner):
        klient.post("/backup", follow_redirects=False)
        conn = _db(datenordner)
        zeile = conn.execute("SELECT * FROM backup_log").fetchone()
        klient.post(f"/backup/{zeile['id']}/pruefen", follow_redirects=False)
        assert "noch keine geprüfte Sicherung" not in klient.get("/backup").text
        pfad = pathlib.Path(zeile["pfad"])
        roh = bytearray(pfad.read_bytes())
        roh[7] ^= 0x55
        pfad.write_bytes(bytes(roh))
        antwort = klient.post(f"/backup/{zeile['id']}/pruefen", follow_redirects=False)
        assert "fehler=" in antwort.headers["location"]
        seite = klient.get("/backup").text
        assert "noch keine geprüfte Sicherung" in seite
        assert ">ungeprüft<" in seite


# ------------------------------------------------- 10, 14: Ortszeit und Spiegel

@pytest.fixture
def berlin():
    """Die Anwendung in Deutschland: UTC+2 im Oktober."""
    vorher = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    yield
    if vorher is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = vorher
    time.tzset()


class TestOrtszeit:
    def test_sicherungsliste_zeigt_ortszeit(self, berlin, klient, datenordner):
        """Befund 10: 04:29 UTC stand als 04:29 unter 'Erzeugt'."""
        klient.post("/backup", follow_redirects=False)
        conn = _db(datenordner)
        conn.execute("UPDATE backup_log SET zeitpunkt = '2026-10-19T04:29:07+00:00'")
        conn.commit()
        seite = klient.get("/backup").text
        assert "<th>Erzeugt (Ortszeit)</th>" in seite
        assert "19.10.2026 06:29" in seite
        assert "2026-10-19 04:29" not in seite

    def test_journalseite_zeigt_ortszeit(self, berlin, klient, datenordner):
        conn = _db(datenordner)
        # Das Journal ist append-only; also ein eigener Eintrag mit festem Zeitpunkt.
        conn.execute(
            "INSERT INTO journal (zeitpunkt_utc, aktion, objekttyp) VALUES (?, ?, ?)",
            ("2026-10-19T04:29:07+00:00", "PROBE", "probe"),
        )
        conn.commit()
        seite = klient.get("/journal").text
        assert "<th>Zeitpunkt (Ortszeit)</th>" in seite
        assert "19.10.2026 06:29" in seite

    def test_filter_rechnet_um_und_vertraegt_unsinn(self, berlin):
        from app.filter import zeit_lokal

        assert zeit_lokal("2026-10-19T04:29:07+00:00") == "19.10.2026 06:29"
        assert zeit_lokal("2026-01-15T23:30:00Z") == "16.01.2026 00:30"
        assert zeit_lokal(None) == "—"
        assert zeit_lokal("gestern") == "gestern"


class TestJournalSpiegel:
    def test_journalseite_sagt_nach_der_sicherung_aktuell(self, klient, datenordner):
        """Befund 14: direkt nach jeder Sicherung hiess es 'veraltet' -- um den
        eigenen BACKUP-Eintrag."""
        klient.post("/backup", follow_redirects=False)
        seite = klient.get("/journal").text
        assert "ist aktuell" in seite
        assert "veraltet" not in seite.split("Vorgänge</h2>")[0]
        spiegel = (datenordner / journal_export.DATEINAME).read_text(encoding="utf-8")
        assert "| BACKUP |" in spiegel

    def test_archiv_traegt_den_spiegel_bis_vor_die_sicherung(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        with zipfile.ZipFile(archiv.pfad) as zip_:
            im_archiv = zip_.read(journal_export.DATEINAME).decode("utf-8")
        assert "| BACKUP |" not in im_archiv
        auf_platte = (datenordner / journal_export.DATEINAME).read_text(encoding="utf-8")
        assert "| BACKUP |" in auf_platte

    def test_seiten_erklaeren_den_unterschied(self, klient):
        assert "bis unmittelbar vor" in klient.get("/backup").text
        assert "bis unmittelbar vor" in klient.get("/journal").text


# ------------------------------------------------- 2, 11: Excel-Beiblatt

def _mappe(klient):
    antwort = klient.get("/backup/excel")
    assert antwort.status_code == 200, antwort.headers.get("location")
    return load_workbook(io.BytesIO(antwort.content))


def _spalte(blatt, kopf: str):
    koepfe = [z.value for z in blatt[1]]
    return koepfe.index(kopf) + 1


class TestExcelText:
    def test_fuehrendes_gleichheitszeichen_bleibt_text(self, klient, datenordner):
        """Befund 2: '=> siehe Mail' und '=HYPERLINK(...)' wurden Formeln."""
        klient.post("/kunden", data=dict(KUNDE, name='=HYPERLINK("http://x";"klick")'),
                    follow_redirects=False)
        conn = _db(datenordner)
        kid = conn.execute("SELECT id FROM kunde").fetchone()[0]
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum, dauer_minuten,"
            " erfasst_am) VALUES (?, '+Titel', '=> siehe Mail', '2026-03-02', 60, ?)",
            (kid, db.jetzt_utc()),
        )
        conn.commit()
        mappe = _mappe(klient)
        for name in mappe.sheetnames:
            for zeile in mappe[name].iter_rows():
                for zelle in zeile:
                    assert zelle.data_type != "f", (name, zelle.coordinate, zelle.value)
        kunden = mappe["Kunden"]
        werte = [z.value for z in kunden[2]]
        assert '=HYPERLINK("http://x";"klick")' in werte
        zeiten = mappe["Zeiten"]
        werte = [z.value for z in zeiten[2]]
        assert "=> siehe Mail" in werte and "+Titel" in werte

    def test_steuerzeichen_lassen_den_export_nicht_scheitern(self, klient, datenordner):
        """Befund: ein Seitenvorschub im Lieferantennamen liess den ganzen
        Notausgang mit englischer Meldung scheitern."""
        conn = _db(datenordner)
        conn.execute(
            "INSERT INTO lieferant (name, angelegt_am) VALUES (?, ?)",
            ("Hetzner\x0cOnline", db.heute()),
        )
        conn.commit()
        mappe = _mappe(klient)
        blatt = mappe["Lieferanten"]
        assert blatt.cell(row=2, column=_spalte(blatt, "Name")).value == "HetznerOnline"
        antwort = klient.post("/backup", follow_redirects=False)
        assert "ACHTUNG" not in _meldung(antwort)

    def test_fehlermeldung_der_route_ist_deutsch(self, klient, monkeypatch):
        """Die Route baut die Datei inzwischen im Speicher (als_bytes) statt
        ueber schreibe(); die Attrappe sitzt deshalb am gemeinsamen Baustein."""
        def kaputt(conn):
            raise ValueError("HetznerOnline cannot be used in worksheets.")

        monkeypatch.setattr(ex, "_mappe", kaputt)
        antwort = klient.get("/backup/excel", follow_redirects=False)
        meldung = _meldung(antwort)
        assert "ließ sich nicht erzeugen" in meldung
        assert "cannot" not in meldung and "worksheets" not in meldung


class TestExcelBeiblatt:
    def test_ueberschriften_mit_umlauten_und_bindestrich(self, klient):
        """Befund 11: 'Betrag fremdwaehrung', 'Kundennummer format',
        'Standard kategorie-ID'."""
        mappe = _mappe(klient)
        koepfe = {name: [z.value for z in mappe[name][1]] for name in mappe.sheetnames}
        assert "Betrag Fremdwährung" in koepfe["Belege"]
        assert "Kundennummer-Format" in koepfe["Betrieb"]
        assert "Standardkategorie-ID" in koepfe["Lieferanten"]
        assert "Standardkategorie" in koepfe["Lieferanten"]

    def test_zeitstempel_koepfe_tragen_utc(self, klient):
        mappe = _mappe(klient)
        koepfe = {name: [z.value for z in mappe[name][1]] for name in mappe.sheetnames}
        assert "Erfasst am (UTC)" in koepfe["Zeiten"]
        assert "Festgeschrieben am (UTC)" in koepfe["Rechnungen"]
        assert "Zeitpunkt (UTC)" in koepfe["Sicherungen"]
        assert "Zeitpunkt (UTC)" in koepfe["Journal"]
        assert "Importiert am (UTC)" in koepfe["Belegdateien"]
        assert koepfe["Journal"].count("Zeitpunkt (UTC)") == 1

    def test_angelegt_am_ist_ein_datum(self, klient):
        klient.post("/kunden", data=KUNDE, follow_redirects=False)
        mappe = _mappe(klient)
        blatt = mappe["Kunden"]
        zelle = blatt.cell(row=2, column=_spalte(blatt, "Angelegt am"))
        assert zelle.number_format == "DD.MM.YYYY"
        assert zelle.value.strftime("%Y-%m-%d") == db.heute()

    def test_aufzaehlungen_im_klartext(self, klient, datenordner):
        klient.post("/anlagen", data={
            "bezeichnung": "Monitor", "anschaffung_am": "2026-03-01", "brutto_euro": "595,00",
            "netto_euro": "500,00", "ust_satz": "19", "art": "BEWEGLICH",
        }, follow_redirects=False)
        conn = _db(datenordner)
        conn.execute(
            "INSERT INTO lieferant (id, name, angelegt_am) VALUES (1, 'Hetzner', ?)",
            (db.heute(),),
        )
        conn.execute(
            "INSERT INTO beleg (id, lieferant_id, belegdatum, betrag_brutto_cent,"
            " betrag_betrieblich_cent, erfasst_am, art)"
            " VALUES (1, 1, '2026-03-14', 1190, 1190, ?, 'AUSGANG')", (db.jetzt_utc(),),
        )
        conn.commit()
        mappe = _mappe(klient)
        anlagen = mappe["Anlagegüter"]
        assert anlagen.cell(row=2, column=_spalte(anlagen, "Art")).value == "beweglich"
        assert anlagen.cell(row=2, column=_spalte(anlagen, "Methode")).value == "GWG (Sofortabzug)"
        belege = mappe["Belege"]
        assert belege.cell(row=2, column=_spalte(belege, "Art")).value == "Ausgang"
        assert ex.KLARTEXT["EINGANG"] == "Eingang"
        assert ex.KLARTEXT["IMMATERIELL"] == "immateriell"
        assert ex.KLARTEXT["BETRAG"] == "Betrag"
        assert ex.KLARTEXT["ZEIT"] == "aus Zeiterfassung"
        assert "FREI" in ex.KLARTEXT


# ------------------------------------------------- 13: Leistungskatalog

class TestLeistungskatalogJournal:
    def test_preis_einheit_und_vorlage_werden_protokolliert(self, klient, datenordner):
        """Befund 13: nur Titel und Aktiv hinterliessen einen Eintrag."""
        klient.post("/leistungen", data={"titel": "Workshop", "einheit": "DAY",
                                         "preis_euro": "800"}, follow_redirects=False)
        conn = _db(datenordner)
        lid = conn.execute("SELECT id FROM leistung WHERE titel = 'Workshop'").fetchone()[0]
        klient.post(f"/leistungen/{lid}", data={
            "titel": "Workshop", "einheit": "HUR", "preis_euro": "950", "aktiv": "1",
            "beschreibung_vorlage": "neu",
        }, follow_redirects=False)
        eintraege = {
            z["feld"]: (z["wert_alt"], z["wert_neu"])
            for z in conn.execute(
                "SELECT * FROM journal WHERE objekttyp = 'leistung' AND aktion = 'AENDERN'"
            )
        }
        assert eintraege["preis_cent"] == ("80000", "95000")
        assert eintraege["einheit"] == ("DAY", "HUR")
        assert eintraege["beschreibung_vorlage"] == (None, "neu")
        assert "titel" not in eintraege, "unveraendert heisst kein Eintrag"

    def test_unveraendertes_speichern_hinterlaesst_nichts(self, klient, datenordner):
        klient.post("/leistungen", data={"titel": "Workshop", "einheit": "DAY",
                                         "preis_euro": "800"}, follow_redirects=False)
        conn = _db(datenordner)
        lid = conn.execute("SELECT id FROM leistung WHERE titel = 'Workshop'").fetchone()[0]
        klient.post(f"/leistungen/{lid}", data={
            "titel": "Workshop", "einheit": "DAY", "preis_euro": "800,00", "aktiv": "1",
        }, follow_redirects=False)
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE objekttyp = 'leistung' AND aktion = 'AENDERN'"
        ).fetchone()[0] == 0


# ------------------------------------------------- 6: Datenbankstart

class TestDatenbankstart:
    def test_keine_datenbank_meldet_sich_deutsch(self, tmp_path):
        """Befund 6: 'sqlite3.DatabaseError: file is not a database' beim Start."""
        datei = tmp_path / "web.sqlite"
        datei.write_bytes(b"kein sqlite " * 200)
        with TestClient(erzeuge_app(datei)) as c:
            gesund = c.get("/gesund")
            assert gesund.status_code == 503
            assert gesund.json()["status"] == "fehler"
            assert "keine lesbare Datenbank" in gesund.json()["meldung"]
            seite = c.get("/", follow_redirects=False)
            assert seite.status_code == 503
            assert "keine lesbare Datenbank" in seite.text
            assert "Sicherung zurückspielen" in seite.text
            einrichtung = c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
            assert einrichtung.status_code == 503
        assert datei.read_bytes() == b"kein sqlite " * 200, "nichts geschrieben"

    def test_fremde_sqlite_ohne_versionsstand_wird_abgewiesen(self, tmp_path):
        datei = tmp_path / "web.sqlite"
        fremd = sqlite3.connect(datei)
        fremd.execute("CREATE TABLE kunde (id INTEGER PRIMARY KEY, foo TEXT)")
        fremd.commit()
        fremd.close()
        with TestClient(erzeuge_app(datei)) as c:
            assert c.get("/gesund").status_code == 503
            assert "keinen Versionsstand" in c.get("/gesund").json()["meldung"]
            einrichtung = c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
            assert einrichtung.status_code == 503
        fremd = sqlite3.connect(datei)
        tabellen = {z[0] for z in fremd.execute("SELECT name FROM sqlite_master")}
        assert tabellen == {"kunde"}, "nichts angelegt, nichts geschrieben"

    def test_neuere_datenbank_wird_nicht_angefasst(self, tmp_path):
        datei = tmp_path / "web.sqlite"
        with TestClient(erzeuge_app(datei)) as c:
            c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        conn = sqlite3.connect(datei)
        conn.execute("INSERT INTO schema_version VALUES (?, '2027-01-01T00:00:00+00:00')",
                     (db.SCHEMA_VERSION + 1,))
        conn.commit()
        journal_vorher = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        conn.close()
        with TestClient(erzeuge_app(datei)) as c:
            gesund = c.get("/gesund")
            assert gesund.status_code == 503
            meldung = gesund.json()["meldung"]
            erwartet = f"Schema {db.SCHEMA_VERSION + 1}, dieses Programm kennt {db.SCHEMA_VERSION}"
            assert erwartet in meldung
            assert "Programm aktualisieren" in meldung
            assert c.get("/", follow_redirects=False).status_code == 503
            assert c.post("/kunden", data=KUNDE, follow_redirects=False).status_code == 503
        conn = sqlite3.connect(datei)
        assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == journal_vorher

    def test_logik_wirft_den_eigenen_fehler(self, tmp_path):
        datei = tmp_path / "web.sqlite"
        datei.write_bytes(b"kein sqlite " * 200)
        with pytest.raises(db.DatenbankFehler, match="keine lesbare Datenbank"):
            db.verbinde(datei)

    def test_normaler_start_bleibt_unveraendert(self, tmp_path):
        with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
            gesund = c.get("/gesund")
            assert gesund.status_code == 200
            assert gesund.json() == {
                "status": "ok", "schema_version": db.SCHEMA_VERSION, "eingerichtet": False,
            }
            assert c.get("/", follow_redirects=False).status_code == 303

"""Sicherung und Rückprüfung.

Der wichtigste Test ist nicht, dass ein Archiv entsteht — sondern dass die
Prüfung ein beschädigtes Archiv erkennt. Ein ungeprüftes Backup ist kein Backup,
und eine Prüfung, die alles durchwinkt, ist schlimmer als keine.
"""

import pathlib
import sqlite3
import zipfile

import pytest

from app import db
from app.logik import backup, journal_export


@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


@pytest.fixture
def gefuellt(conn, betrieb, kunde_id, datenordner):
    """Eine Datenbank mit Inhalt plus ein paar Belegdateien."""
    conn.execute(
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                              netto_cent, brutto_cent, status, festgeschrieben_am, erstellt_am)
        VALUES ('RECHNUNG', 'RE-2026-0001', 1, 2026, ?, '2026-03-02', 10000, 10000,
                'FESTGESCHRIEBEN', ?, ?)
        """,
        (kunde_id, db.jetzt_utc(), db.jetzt_utc()),
    )
    for _ in range(3):
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', '2026-03-02', 60, ?)",
            (kunde_id, db.jetzt_utc()),
        )
    rechnungen = datenordner / "ausgangsrechnungen" / "2026"
    rechnungen.mkdir(parents=True)
    (rechnungen / "RE-2026-0001_Kunde.pdf").write_bytes(b"%PDF-1.7 Platzhalter")
    belege = datenordner / "belege" / "2026"
    belege.mkdir(parents=True)
    (belege / "2026-03-14_Hetzner.pdf").write_bytes(b"%PDF-1.7 Beleg")
    return kunde_id


class TestErzeugen:
    def test_archiv_enthaelt_datenbank_und_belege(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        assert archiv.pfad.exists()
        with zipfile.ZipFile(archiv.pfad) as zip_:
            namen = zip_.namelist()
        assert backup.DB_IM_ARCHIV in namen
        # Unter ihrem Pfad IM Datenordner, ohne Zwischenordner: das Archiv in
        # einen leeren Ordner entpackt IST der Datenordner. Ein fest
        # verdrahtetes "daten/" davor passte zu keinem echten Ordnernamen.
        assert "ausgangsrechnungen/2026/RE-2026-0001_Kunde.pdf" in namen
        assert "belege/2026/2026-03-14_Hetzner.pdf" in namen
        assert not any(n.startswith("daten/") for n in namen)
        # Das Archiv traegt das Protokoll auch als Klartext: im Ernstfall fehlt
        # genau die Anwendung, mit der man die Datenbank lesen koennte.
        assert journal_export.DATEINAME in namen
        # Und aus demselben Grund die Tabellenkalkulation: eine SQLite-Datei
        # nuetzt nur, solange es ein Programm gibt, das sie oeffnet. Die
        # Aufbewahrungsfrist laeuft hier zehn Jahre -- § 147 Abs. 3 AO gibt
        # zehn fuer die Aufzeichnungen und acht fuer die Belege, und das
        # Archiv traegt beides.
        assert backup.EXCEL_IM_ARCHIV in namen
        assert archiv.excel_fehler is None
        assert archiv.dateien == 5

    def test_tabelle_im_archiv_ist_lesbar(self, conn, gefuellt, datenordner, tmp_path):
        """Enthalten heisst nicht lesbar -- also wird sie geoeffnet.

        Der ganze Zweck der Datei ist, dass sie OHNE dieses Werkzeug etwas wert
        ist. Ein beschaedigtes Blatt faellt sonst erst auf, wenn es darauf
        ankommt.
        """
        from openpyxl import load_workbook

        archiv = backup.erzeuge(conn, datenordner)
        with zipfile.ZipFile(archiv.pfad) as zip_:
            zip_.extract(backup.EXCEL_IM_ARCHIV, tmp_path)
        mappe = load_workbook(tmp_path / backup.EXCEL_IM_ARCHIV)
        assert "Rechnungen" in mappe.sheetnames
        blatt = mappe["Rechnungen"]
        kopf = [z.value for z in blatt[1]]
        spalte = kopf.index("Nummer") + 1
        nummern = [blatt.cell(row=r, column=spalte).value for r in range(2, blatt.max_row + 1)]
        assert "RE-2026-0001" in nummern

    def test_datenbank_im_archiv_ist_konsistent_und_vollstaendig(
        self, conn, gefuellt, datenordner, tmp_path
    ):
        """VACUUM INTO statt Dateikopie: eine Kopie mitten in einer Transaktion
        ergibt einen Zwischenstand."""
        archiv = backup.erzeuge(conn, datenordner)
        ziel = tmp_path / "entpackt"
        with zipfile.ZipFile(archiv.pfad) as zip_:
            zip_.extract(backup.DB_IM_ARCHIV, ziel)
        kopie = sqlite3.connect(ziel / backup.DB_IM_ARCHIV)
        assert kopie.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert kopie.execute("SELECT COUNT(*) FROM rechnung").fetchone()[0] == 1
        assert kopie.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 3
        kopie.close()

    def test_backup_ordner_wandert_nicht_ins_archiv(self, conn, gefuellt, datenordner):
        """Ohne diesen Ausschluss enthielte jedes Backup alle vorherigen."""
        erstes = backup.erzeuge(conn, datenordner)
        zweites = backup.erzeuge(conn, datenordner)
        with zipfile.ZipFile(zweites.pfad) as zip_:
            namen = zip_.namelist()
        assert not any("backup" in n for n in namen)
        assert erstes.pfad.name not in str(namen)

    def test_zweite_sicherung_am_selben_tag_ueberschreibt_nicht(
        self, conn, gefuellt, datenordner
    ):
        erstes = backup.erzeuge(conn, datenordner)
        zweites = backup.erzeuge(conn, datenordner)
        assert erstes.pfad != zweites.pfad
        assert erstes.pfad.exists() and zweites.pfad.exists()

    def test_eintrag_im_protokoll_und_im_journal(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        zeile = conn.execute("SELECT * FROM backup_log").fetchone()
        assert zeile["sha256"] == archiv.sha256
        assert zeile["geprueft"] == 0, "Frisch erzeugt heisst ungeprueft"
        assert conn.execute(
            "SELECT COUNT(*) c FROM journal WHERE aktion = 'BACKUP'"
        ).fetchone()["c"] == 1

    def test_eigener_zielordner_wird_benutzt(self, conn, gefuellt, datenordner, tmp_path):
        """Die Platte (der Elternordner) muss da sein: angelegt wird nur der
        letzte Ordner, sonst entstuende /Volumes/Backup/... auf der
        Systemplatte, wenn die externe gerade nicht haengt."""
        extern = tmp_path / "externe-platte" / "Steuer"
        extern.parent.mkdir()
        conn.execute("UPDATE betrieb SET backup_pfad = ? WHERE id = 1", (str(extern),))
        archiv = backup.erzeuge(conn, datenordner)
        assert archiv.pfad.parent == extern

    def test_standardziel_liegt_im_datenordner(self, conn, gefuellt, datenordner):
        assert backup.zielordner(conn, datenordner) == datenordner / "backup"


class TestArchivIstDerDatenordner:
    def test_entpackt_ergibt_den_datenordner(self, conn, gefuellt, datenordner, tmp_path):
        """Die Anleitung sagt: in einen leeren Ordner entpacken. Dann muss
        dort genau das liegen, was die Anwendung erwartet -- und nichts eine
        Ebene zu tief."""
        archiv = backup.erzeuge(conn, datenordner)
        ziel = tmp_path / "wiederhergestellt"
        with zipfile.ZipFile(archiv.pfad) as zip_:
            zip_.extractall(ziel)
        assert (ziel / backup.DB_IM_ARCHIV).is_file()
        assert (ziel / "ausgangsrechnungen" / "2026" / "RE-2026-0001_Kunde.pdf").is_file()
        assert (ziel / "belege" / "2026" / "2026-03-14_Hetzner.pdf").is_file()
        assert not (ziel / "daten").exists()

    def test_abgebrochene_sicherung_hinterlaesst_kein_archiv(
        self, conn, gefuellt, datenordner, monkeypatch
    ):
        """Bricht das Packen ab, darf unter dem regulaeren Namen nichts
        liegen: ein halbes ZIP ist einwandfrei zu oeffnen, steht in keinem
        Protokoll und sieht beim naechsten Blick aus wie die Sicherung von
        gestern."""
        echtes_write = zipfile.ZipFile.write

        def kaputt(self, dateiname, arcname=None, *rest, **kw):
            if arcname and str(arcname).startswith("belege/"):
                raise OSError("Platte voll")
            return echtes_write(self, dateiname, arcname, *rest, **kw)

        monkeypatch.setattr(zipfile.ZipFile, "write", kaputt)
        with pytest.raises(OSError, match="Platte voll"):
            backup.erzeuge(conn, datenordner)
        ordner = backup.zielordner(conn, datenordner)
        assert not list(ordner.glob("*.zip")), "kein halbes Archiv unter regulaerem Namen"
        assert not list(ordner.glob("*.teil")), "und auch die Arbeitsdatei ist weg"
        assert conn.execute("SELECT COUNT(*) FROM backup_log").fetchone()[0] == 0


class TestWiederherstellungAnAndererStelle:
    """Der Ernstfall: neuer Rechner, anderer Benutzername, anderer Pfad.

    Mit absoluten Pfaden in der Datenbank zeigte danach jeder Beleg und jedes
    Rechnungs-PDF ins Leere -- und belegdatei.pfad war per Trigger nicht
    einmal reparierbar.
    """

    def _datenordner_mit_inhalt(self, ordner):
        import shutil

        from app.logik import festschreiben as fs
        from app.logik.beleg import pipeline

        ordner.mkdir(parents=True)
        conn = db.oeffne(ordner / "steuer.sqlite")
        conn.execute(
            "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
            " standardsatz_cent, beginn_am)"
            " VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095', 'Hamburg',"
            " '12/345/67890', 8000, '2020-01-01')"
        )
        kunde = conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
            " VALUES ('001', 'Mustermann GmbH', 'Hauptstr. 1', '10115', 'Berlin', ?)",
            (db.heute(),),
        ).lastrowid
        zeit = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', '2026-03-02', 60, ?)", (kunde, db.jetzt_utc()),
        ).lastrowid
        rid = fs.entwurf_anlegen(conn, kunde, [zeit])
        ergebnis = fs.festschreiben(conn, rid, belegordner=ordner)
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler

        pdf = pathlib.Path(__file__).parent / "fixtures" / "lieferantenrechnung_de.pdf"
        eingang = pipeline.aufnehmen(pdf.read_bytes(), "hetzner.pdf", ordner)
        lesung = pipeline.lies(conn, eingang)
        werte = {feld: fund.wert for feld, fund in lesung.vorschlag.felder.items()}
        buchung = pipeline.buche(conn, lesung, werte, ordner)

        gespeichert = conn.execute("SELECT pfad FROM belegdatei").fetchone()["pfad"]
        pdf_pfad = conn.execute("SELECT pdf_pfad FROM rechnung WHERE id = ?",
                                (rid,)).fetchone()["pdf_pfad"]
        conn.close()
        shutil  # noqa: B018 -- Import oben, Nutzung im Aufrufer
        return rid, buchung.beleg_id, gespeichert, pdf_pfad

    def test_pfade_stehen_relativ_in_der_datenbank(self, tmp_path):
        _, _, beleg_pfad, pdf_pfad = self._datenordner_mit_inhalt(tmp_path / "alt" / "Daten")
        assert not pathlib.Path(beleg_pfad).is_absolute()
        assert beleg_pfad.startswith("belege/2026/")
        assert not pathlib.Path(pdf_pfad).is_absolute()
        assert pdf_pfad.startswith("ausgangsrechnungen/2026/")

    def test_umzug_des_datenordners_bricht_keinen_link(self, tmp_path):
        import shutil

        from fastapi.testclient import TestClient

        from app.main import erzeuge_app

        alt = tmp_path / "alt" / "Daten"
        rid, beleg_id, _, _ = self._datenordner_mit_inhalt(alt)
        neu = tmp_path / "neuer-rechner" / "anderer-name" / "Steuer-Tool"
        shutil.copytree(alt, neu)
        shutil.rmtree(alt)

        with TestClient(erzeuge_app(neu / "steuer.sqlite")) as klient:
            pdf = klient.get(f"/rechnung/{rid}/pdf")
            assert pdf.status_code == 200, pdf.headers.get("location")
            assert pdf.headers["content-type"] == "application/pdf"
            datei = klient.get(f"/belege/{beleg_id}/datei")
            assert datei.status_code == 200, datei.headers.get("location")
            assert datei.content.startswith(b"%PDF")
            # Und der Prueflauf findet jede Datei an ihrem neuen Ort.
            antwort = klient.post("/belege/integritaet", follow_redirects=False)
            assert "alle%20unver%C3%A4ndert" in antwort.headers["location"]


class TestPruefen:
    def test_heiles_archiv_wird_bestaetigt(self, conn, gefuellt, datenordner):
        backup.erzeuge(conn, datenordner)
        backup_id = conn.execute("SELECT id FROM backup_log").fetchone()["id"]
        ergebnis = backup.pruefe(conn, backup_id)
        assert ergebnis.heil
        assert ergebnis.rechnungen == 1
        assert ergebnis.zeiteintraege == 3
        assert ergebnis.belegdateien == 2
        assert conn.execute(
            "SELECT geprueft FROM backup_log WHERE id = ?", (backup_id,)
        ).fetchone()[0] == 1

    def test_veraenderte_datei_wird_erkannt(self, conn, gefuellt, datenordner):
        """Der eigentliche Zweck: ein Byte kippt, die Pruefsumme meldet es."""
        archiv = backup.erzeuge(conn, datenordner)
        backup_id = conn.execute("SELECT id FROM backup_log").fetchone()["id"]
        rohdaten = bytearray(archiv.pfad.read_bytes())
        rohdaten[len(rohdaten) // 2] ^= 0xFF
        archiv.pfad.write_bytes(bytes(rohdaten))

        ergebnis = backup.pruefe(conn, backup_id)
        assert not ergebnis.heil
        assert "Prüfsumme" in ergebnis.meldung
        assert conn.execute(
            "SELECT geprueft FROM backup_log WHERE id = ?", (backup_id,)
        ).fetchone()[0] == 0, "Ein beschaedigtes Archiv darf nicht als geprueft gelten"

    def test_fehlende_datei_wird_erkannt(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        backup_id = conn.execute("SELECT id FROM backup_log").fetchone()["id"]
        archiv.pfad.unlink()
        ergebnis = backup.pruefe(conn, backup_id)
        assert not ergebnis.heil
        assert "fehlt" in ergebnis.meldung

    def test_kein_zip_wird_erkannt(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        backup_id = conn.execute("SELECT id FROM backup_log").fetchone()["id"]
        archiv.pfad.write_bytes(b"das ist kein ZIP")
        conn.execute(
            "UPDATE backup_log SET sha256 = NULL WHERE id = ?", (backup_id,)
        )  # Pruefsumme ueberspringen, damit die ZIP-Pruefung greift
        ergebnis = backup.pruefe(conn, backup_id)
        assert not ergebnis.heil
        assert "nicht lesbar" in ergebnis.meldung

    def test_defekte_datenbank_im_archiv_wird_erkannt(self, conn, gefuellt, datenordner, tmp_path):
        """Ein Archiv, dessen Datenbank sich nicht oeffnen laesst, ist wertlos --
        und das soll man vorher wissen, nicht im Schadensfall."""
        kaputt = tmp_path / "kaputt.zip"
        with zipfile.ZipFile(kaputt, "w") as zip_:
            zip_.writestr(backup.DB_IM_ARCHIV, b"SQLite format 3\x00 aber danach Muell")
        conn.execute(
            "INSERT INTO backup_log (zeitpunkt, pfad, groesse, sha256, geprueft)"
            " VALUES (?, ?, ?, NULL, 0)",
            (db.jetzt_utc(), str(kaputt), kaputt.stat().st_size),
        )
        backup_id = conn.execute("SELECT id FROM backup_log").fetchone()["id"]
        ergebnis = backup.pruefe(conn, backup_id)
        assert not ergebnis.heil
        assert "defekt" in ergebnis.meldung or "nicht lesbar" in ergebnis.meldung

    def test_unbekanntes_backup(self, conn):
        assert not backup.pruefe(conn, 999).heil


class TestListeUndHinweise:
    def test_liste_meldet_fehlende_dateien(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        archiv.pfad.unlink()
        eintrag = backup.liste(conn)[0]
        assert eintrag["vorhanden"] is False
        assert eintrag["geprueft"] is False

    def test_letztes_geprueft_ist_zunaechst_leer(self, conn, gefuellt, datenordner):
        assert backup.letztes_geprueft(conn) is None
        backup.erzeuge(conn, datenordner)
        assert backup.letztes_geprueft(conn) is None, "Erzeugen allein genuegt nicht"
        backup.pruefe(conn, conn.execute("SELECT id FROM backup_log").fetchone()["id"])
        assert backup.letztes_geprueft(conn) is not None

    def test_blaue_warnung_verschwindet_erst_nach_der_pruefung(
        self, conn, betrieb, kunde_id, datenordner
    ):
        from app.logik import festschreiben as fs
        from app.logik import validierung as v

        conn.execute("UPDATE kunde SET satz_cent = 8000, angelegt_am = '2020-01-01'"
                     " WHERE id = ?", (kunde_id,))
        cur = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', '2026-03-02', 60, ?)",
            (kunde_id, db.jetzt_utc()),
        )
        rid = fs.entwurf_anlegen(conn, kunde_id, [int(cur.lastrowid)])

        assert "B03" in {b.code for b in v.pruefe(conn, rid)}
        backup.erzeuge(conn, datenordner)
        assert "B03" in {b.code for b in v.pruefe(conn, rid)}, "Ungeprueft zaehlt nicht"
        backup.pruefe(conn, conn.execute("SELECT id FROM backup_log").fetchone()["id"])
        assert "B03" not in {b.code for b in v.pruefe(conn, rid)}

    def test_zweitkopie_auf_externes_ziel(self, conn, gefuellt, datenordner, tmp_path):
        archiv = backup.erzeuge(conn, datenordner)
        extern = tmp_path / "platte"
        kopie = backup.kopiere_nach(archiv.pfad, extern)
        assert kopie.exists()
        assert kopie.read_bytes() == archiv.pfad.read_bytes()

    def test_wiederherstellungshinweis_nennt_die_schritte(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        text = backup.wiederherstellungshinweis(archiv.pfad)
        assert archiv.pfad.name in text
        assert "beenden" in text and "umbenennen" in text

    @pytest.mark.parametrize(
        ("bytes_", "text"),
        [(None, "—"), (0, "—"), (512, "512 B"), (2048, "2,0 KB"), (5 * 1024 * 1024, "5,0 MB"),
         (72_000, "70,3 KB")],
    )
    def test_groessenangabe(self, bytes_, text):
        assert backup.menschenlesbar(bytes_) == text


class TestUeberDieOberflaeche:
    """Der Weg, den der Nutzer tatsächlich geht."""

    @pytest.fixture
    def klient(self, tmp_path):
        from fastapi.testclient import TestClient

        from app.main import erzeuge_app

        with TestClient(erzeuge_app(tmp_path / "daten" / "steuer.sqlite")) as c:
            c.post("/einrichtung", data={
                "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095",
                "ort": "Hamburg", "steuernummer": "12/345/67890",
                "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
                "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
            }, follow_redirects=False)
            yield c

    def test_seite_mahnt_die_fehlende_sicherung_an(self, klient):
        seite = klient.get("/backup").text
        assert "noch keine geprüfte Sicherung" in seite
        assert "Noch keine Sicherung erzeugt" in seite

    def test_erzeugen_dann_pruefen(self, klient, tmp_path):
        antwort = klient.post("/backup", follow_redirects=True)
        assert "meldung erfolg" in antwort.text, antwort.text[:200]
        assert "steuer-backup.zip geschrieben" in antwort.text
        assert "ungeprüft" in antwort.text
        assert list((tmp_path / "daten" / "backup").glob("*.zip"))

        import sqlite3

        conn = sqlite3.connect(tmp_path / "daten" / "steuer.sqlite")
        backup_id = conn.execute("SELECT id FROM backup_log").fetchone()[0]
        conn.close()

        antwort = klient.post(f"/backup/{backup_id}/pruefen", follow_redirects=True)
        assert "Archiv gelesen" in antwort.text
        assert "noch keine geprüfte Sicherung" not in antwort.text

    def test_groesse_in_meldung_und_liste_mit_komma(self, klient):
        """Die Erfolgsmeldung sagte "48.9 KB" -- mit Dezimalpunkt, wie
        achtundvierzigtausendneunhundert. Meldung und Liste schreiben die Groesse
        deutsch (wie das "60,0 MB" beim Hochladen)."""
        import re

        antwort = klient.post("/backup", follow_redirects=True)
        groessen = re.findall(r"\d+[.,]\d+ [KMG]B", antwort.text)
        assert groessen, antwort.text[:300]
        assert all("," in g for g in groessen), groessen

    def test_tabelle_einzeln_herunterladen(self, klient, tmp_path):
        """Der Notausgang, den der README bewirbt — und der nie geprüft war.

        Die Logik darunter ist getestet, die Route nicht: Dateiname, Medientyp
        und dass überhaupt eine lesbare Mappe herauskommt. Genau daran scheitert
        ein Download, ohne dass ein Test der Logik es merkt.
        """
        import io

        from openpyxl import load_workbook

        antwort = klient.get("/backup/excel")
        assert antwort.status_code == 200
        assert antwort.headers["content-type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        # Mit Datum im Namen: im Downloadordner liegen sonst zehn gleich
        # heissende Dateien nebeneinander.
        verfuegung = antwort.headers["content-disposition"]
        assert "steuer-daten.xlsx" in verfuegung
        assert db.heute() in verfuegung

        mappe = load_workbook(io.BytesIO(antwort.content))
        assert "Lies mich" in mappe.sheetnames
        assert "Kunden" in mappe.sheetnames

    def test_knopf_steht_auf_der_seite(self, klient):
        assert '/backup/excel' in klient.get("/backup").text

    def test_zielordner_speichern(self, klient, tmp_path):
        """Der Elternordner ("die Platte") muss existieren; nur der letzte
        Ordner wird angelegt -- siehe test_eigener_zielordner_wird_benutzt."""
        extern = tmp_path / "platte" / "Steuer"
        extern.parent.mkdir()
        klient.post("/backup/ziel", data={"pfad": str(extern)})
        seite = klient.get("/backup").text
        assert str(extern) in seite
        klient.post("/backup", follow_redirects=True)
        assert list(extern.glob("*.zip"))

    def test_untauglicher_zielordner_wird_abgewiesen(self, klient, tmp_path):
        datei = tmp_path / "keine-mappe.txt"
        datei.write_text("x")
        antwort = klient.post("/backup/ziel", data={"pfad": str(datei)},
                              follow_redirects=True)
        assert "lässt sich nicht anlegen" in antwort.text or "kein Ordner" in antwort.text

    def test_wiederherstellung_hat_keinen_knopf(self, klient):
        """Eine Wiederherstellung überschreibt den laufenden Bestand — das soll
        niemand aus Versehen per Knopfdruck tun."""
        seite = klient.get("/backup").text
        assert "Wiederherstellen" in seite
        assert "Anwendung beenden" in seite
        assert 'action="/backup/wiederherstellen"' not in seite


class TestZweitziel:
    """Eine Sicherung auf derselben Platte wie die Daten schützt gegen
    Versehen, nicht gegen einen Plattendefekt — und die Aufbewahrungsfrist
    läuft bis zu zehn Jahre (§ 147 Abs. 3 AO), also über mehrere Rechner
    hinweg."""

    def test_ohne_zweitziel_keine_kopie(self, conn, gefuellt, datenordner):
        archiv = backup.erzeuge(conn, datenordner)
        assert archiv.zweitkopie is None
        assert archiv.zweitkopie_fehler is None

    def test_zweitkopie_entsteht(self, conn, gefuellt, datenordner, tmp_path):
        extern = tmp_path / "extern"
        conn.execute("UPDATE betrieb SET backup_zweitziel = ? WHERE id = 1",
                     (str(extern),))
        archiv = backup.erzeuge(conn, datenordner)
        assert archiv.zweitkopie is not None
        assert archiv.zweitkopie.exists()
        assert archiv.zweitkopie.read_bytes() == archiv.pfad.read_bytes()
        assert archiv.zweitkopie_fehler is None

    def test_fehlendes_laufwerk_laesst_die_sicherung_durchlaufen(
        self, conn, gefuellt, datenordner, tmp_path
    ):
        """Die externe Platte hängt meistens nicht dran. Eine Sicherung, die
        deshalb ganz ausfällt, wäre schlechter als eine, die nur einmal liegt —
        gemeldet wird es trotzdem.
        """
        blockiert = tmp_path / "datei-kein-ordner"
        blockiert.write_text("ich bin eine Datei")
        conn.execute("UPDATE betrieb SET backup_zweitziel = ? WHERE id = 1",
                     (str(blockiert / "unten"),))
        archiv = backup.erzeuge(conn, datenordner)
        assert archiv.pfad.exists(), "Die Sicherung selbst muss entstehen"
        assert archiv.zweitkopie is None
        # Frueher hiess jeder Fehlschlag "Hängt das Laufwerk?". Hier liegt
        # aber eine Datei im Weg, kein Laufwerk fehlt -- die Meldung sagt das.
        assert "ist nicht entstanden" in archiv.zweitkopie_fehler
        assert "ist eine Datei, kein Ordner" in archiv.zweitkopie_fehler

    def test_journal_haelt_die_zweitkopie(self, conn, gefuellt, datenordner, tmp_path):
        extern = tmp_path / "extern"
        conn.execute("UPDATE betrieb SET backup_zweitziel = ? WHERE id = 1",
                     (str(extern),))
        backup.erzeuge(conn, datenordner)
        zeile = conn.execute(
            "SELECT * FROM journal WHERE aktion = 'BACKUP'").fetchone()
        assert "Zweitkopie" in zeile["begruendung"]


class TestZieleUeberDieOberflaeche:
    @pytest.fixture
    def klient(self, tmp_path):
        from fastapi.testclient import TestClient

        from app.main import erzeuge_app

        with TestClient(erzeuge_app(tmp_path / "daten" / "steuer.sqlite")) as c:
            c.post("/einrichtung", data={
                "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095",
                "ort": "Hamburg", "steuernummer": "12/345/67890",
                "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
                "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
            }, follow_redirects=False)
            yield c

    def test_beide_ziele_speichern(self, klient, tmp_path):
        import sqlite3

        erstes, zweites = tmp_path / "a", tmp_path / "b"
        klient.post("/backup/ziel", data={"pfad": str(erstes), "zweitziel": str(zweites)},
                    follow_redirects=True)
        conn = sqlite3.connect(tmp_path / "daten" / "steuer.sqlite")
        conn.row_factory = sqlite3.Row
        zeile = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
        conn.close()
        assert zeile["backup_pfad"] == str(erstes)
        assert zeile["backup_zweitziel"] == str(zweites)

    def test_derselbe_ordner_zweimal_wird_abgewiesen(self, klient, tmp_path):
        ziel = str(tmp_path / "beide")
        antwort = klient.post("/backup/ziel", data={"pfad": ziel, "zweitziel": ziel},
                              follow_redirects=True)
        assert "denselben Ordner" in antwort.text
        assert "Plattendefekt" in antwort.text

    def test_seite_nennt_die_drei_ebenen(self, klient):
        text = klient.get("/backup").text
        assert "Time Machine" in text
        assert "iCloud Drive" in text
        assert "nie\n  zurückgespielt" in text or "nie zurückgespielt" in " ".join(text.split())

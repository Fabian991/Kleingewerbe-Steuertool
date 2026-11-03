"""Ein Eingangsordner je Jahr.

Geprueft wird das Hinterlegen (mit Journal, Pruefungen und Rueckfrage bei
einem schon belegten Ordner), der Vorschlag aus dem Vorjahrespfad, ein Lauf
ueber alle Ordner, das Gedaechtnis und der Journalvergleich je Ordner (die
Fehler beim Ordnerwechsel), der Hinweis auf ein Belegdatum, das nicht zum
Ordnerjahr passt, der Hinweis am Jahreswechsel, der Start und das
Sicherungsziel. Alle Pfade liegen im Testordner, alle Daten sind erfunden.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik.beleg import abholen, bezahlt, jahresordner
from app.main import erzeuge_app
from app.routen import belege as belege_routen
from tests import test_belegordner as bo

HEUTE = bo.HEUTE

datenordner = bo.datenordner
postfach = bo.postfach
conn = bo.conn
_dateien_gelten_als_fertig = bo._dateien_gelten_als_fertig
_nichts_nachzuholen = bo._nichts_nachzuholen
app_pfad = bo.app_pfad
klient = bo.klient


def anzahl(verbindung, sql: str, *werte) -> int:
    return verbindung.execute(sql, werte).fetchone()[0]


def jahresordner_anlegen(verbindung, jahr: int, pfad: Path) -> Path:
    pfad.mkdir(parents=True, exist_ok=True)
    verbindung.execute("INSERT INTO belegordner_jahr (jahr, pfad, angelegt_utc) VALUES (?, ?, ?)",
                       (jahr, str(pfad), db.jetzt_utc()))
    return pfad


def hole_alle(verbindung, datenordner, **weiter) -> abholen.Gesamtergebnis:
    weiter.setdefault("automatisch", True)
    weiter.setdefault("heute", HEUTE)
    return abholen.abholen_alle(verbindung, datenordner, abholen.ordner_liste(verbindung),
                                **weiter)


def abfrage(app_pfad, sql, *werte):
    return bo.abfrage(app_pfad, sql, *werte)


class Tag(date):
    """Ein fester "heute" fuer Routen und Logik."""

    heute = date(2026, 12, 2)

    @classmethod
    def today(cls):
        return cls.heute


@pytest.fixture
def heute_ist(monkeypatch):
    def setzen(tag: date):
        Tag.heute = tag
        monkeypatch.setattr(jahresordner, "date", Tag)
        monkeypatch.setattr(belege_routen, "date", Tag)
    return setzen


# ------------------------------------------------------------- Logik

class TestOrdnerliste:
    def test_jahre_absteigend_alle_jahre_zuletzt(self, conn, tmp_path, postfach):
        a = jahresordner_anlegen(conn, 2025, tmp_path / "Belege 2025")
        b = jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        assert abholen.ordner_liste(conn) == [
            abholen.Eingangsordner(2026, b), abholen.Eingangsordner(2025, a),
            abholen.Eingangsordner(None, postfach)]

    @pytest.mark.parametrize("alt, jahr, erwartet", [
        ("Belege 2026", 2027, "Belege 2027"),
        ("Buchhaltung/2026/Eingang", 2027, "Buchhaltung/2027/Eingang"),
    ])
    def test_vorschlag_aus_dem_vorjahr(self, conn, tmp_path, alt, jahr, erwartet):
        jahresordner_anlegen(conn, 2026, tmp_path / alt)
        assert jahresordner.vorschlag(conn, jahr) == tmp_path / erwartet

    def test_vorschlag_ohne_jahresordner(self, conn, monkeypatch):
        monkeypatch.setattr(abholen, "_ist_mac", lambda: False)
        assert jahresordner.vorschlag(conn, 2027) is None
        monkeypatch.setattr(abholen, "_ist_mac", lambda: True)
        assert jahresordner.vorschlag(conn, 2027).name == "Steuer-Tool Belege 2027"

    def test_kollisionen(self, conn, tmp_path, postfach):
        a = jahresordner_anlegen(conn, 2025, tmp_path / "Belege 2025")
        assert "schon als Ordner 2025" in jahresordner.kollision(conn, a, jahr=2026)
        assert "schon als Ordner für alle Jahre" in jahresordner.kollision(
            conn, postfach, jahr=2026)
        assert "ineinander" in jahresordner.kollision(conn, a / "2026", jahr=2026)
        assert "ineinander" in jahresordner.kollision(conn, tmp_path, jahr=2026)
        assert "„Übernommen“" in jahresordner.kollision(
            conn, postfach / abholen.UEBERNOMMEN / "x", jahr=2026)
        # Neben anderen, und ein Jahresordner im Ordner fuer alle Jahre (dessen
        # Unterordner nicht abgeholt werden): erlaubt.
        assert jahresordner.kollision(conn, tmp_path / "Belege 2026", jahr=2026) is None
        assert jahresordner.kollision(conn, postfach / "2026", jahr=2026) is None
        # Das eigene Jahr zaehlt nicht mit (der Ordner wird nur geaendert).
        assert jahresordner.kollision(conn, a, jahr=2025) is None

    def test_schon_belegt(self, tmp_path):
        ordner = tmp_path / "Jahresordner"
        (ordner / "Unterordner").mkdir(parents=True)
        (ordner / ".versteckt").write_text("x")
        assert jahresordner.schon_belegt(ordner) is None
        (ordner / "Rechnung.pdf").write_bytes(b"%PDF")
        (ordner / "Kontoauszug.xlsx").write_bytes(b"PK")
        belegt = jahresordner.schon_belegt(ordner)
        assert (belegt.belege, belegt.andere) == (1, 1)
        text = jahresordner.belegt_text(ordner, belegt)
        assert text.startswith("Im Ordner liegen schon 2 Dateien: 1 würde als Beleg abgeholt "
                               "und 1 nach „Nicht übernommen“ verschoben.")
        assert str(ordner / "Eingang") in text

    def test_schon_belegt_mit_einer_datei(self, tmp_path):
        """Eine Datei: "liegt", nicht "liegen"."""
        ordner = tmp_path / "Jahresordner"
        ordner.mkdir()
        (ordner / "Rechnung.pdf").write_bytes(b"%PDF")
        text = jahresordner.belegt_text(ordner, jahresordner.schon_belegt(ordner))
        assert text.startswith("Im Ordner liegt schon 1 Datei: 1 würde als Beleg abgeholt.")


class TestAbholen:
    def test_ein_lauf_holt_alle_ordner_ab(self, conn, datenordner, tmp_path, postfach):
        """Jeder Ordner bekommt sein "Übernommen" und seinen Journaleintrag
        (mit dem Jahr); der Satz nennt jeden Ordner."""
        bo.lieferant(conn)
        a = jahresordner_anlegen(conn, 2025, tmp_path / "Belege 2025")
        b = jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        bo.lege(a, "Dezember.xml", bo.cii("RE-2025-1", "20251103"))
        bo.lege(b, "Maerz.xml", bo.cii())
        bo.lege(postfach, "Notiz.docx", b"PK\x03\x04")
        gesamt = hole_alle(conn, datenordner)
        assert [t.jahr for t in gesamt.teile] == [2026, 2025, None]
        # Die Rechnung aus 2025 wartet: mit der Bezahlt-Vorgabe aenderte ihre
        # Zahlung unbeaufsichtigt die EUeR 2025 (Vorjahr, evtl. abgegeben).
        assert [g.originalname for g in gesamt.gebucht] == ["Maerz.xml"]
        assert [(w.originalname, w.grund) for w in gesamt.wartend] == [
            ("Dezember.xml", bezahlt.vorjahr_grund(2025))]
        assert bo.namen(a / abholen.UEBERNOMMEN) == ["Dezember.xml"]
        assert bo.namen(b / abholen.UEBERNOMMEN) == ["Maerz.xml"]
        assert bo.namen(postfach / abholen.NICHT_UEBERNOMMEN) == ["Notiz.docx"]
        eintraege = [json.loads(z[0]) for z in conn.execute(
            "SELECT wert_neu FROM journal WHERE aktion = 'BELEGORDNER_ABGEHOLT' ORDER BY id")]
        assert [(e["ordner"], e.get("jahr")) for e in eintraege] == [
            (str(b), 2026), (str(a), 2025), (str(postfach), None)]
        assert gesamt.zusammenfassung() == (
            "Aus den Eingangsordnern: Ordner 2026: 1 Rechnung automatisch gebucht. Ordner "
            "2025: 1 Rechnung wartet in der Prüfmaske. Ordner für alle Jahre: 1 Datei nicht "
            "übernommen.")

    def test_gedaechtnis_je_ordner(self, conn, datenordner, tmp_path, postfach, monkeypatch):
        """Eine nicht verschiebbare Datei in A wird nach einem Lauf fuer B
        nicht erneut gemeldet -- vorher loeschte der Lauf fuer B das
        Gedaechtnis von A, und A meldete sie samt Journaleintrag neu."""
        bo.lieferant(conn)
        a = jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        bo.lege(a, "gesperrt.docx", b"PK\x03\x04")
        echt = os.replace

        def verweigert(quelle, ziel, *rest, **optionen):
            if Path(quelle).parent == a:
                raise PermissionError(1, "Operation not permitted", str(quelle))
            return echt(quelle, ziel, *rest, **optionen)

        monkeypatch.setattr(os, "replace", verweigert)
        erster = hole_alle(conn, datenordner)
        assert [n.originalname for n in erster.nicht_verschoben] == ["gesperrt.docx"]
        # Einzeln abwechselnd: B (der Ordner fuer alle Jahre), dann A.
        abholen.abholen(conn, datenordner, postfach, automatisch=True, heute=HEUTE)
        zweiter = abholen.abholen(conn, datenordner, a, automatisch=True, heute=HEUTE, jahr=2026)
        assert zweiter.leer()
        assert anzahl(conn, "SELECT COUNT(*) FROM journal WHERE aktion = 'BELEGORDNER_ABGEHOLT'"
                            " AND json_extract(wert_neu, '$.ordner') = ?", str(a)) == 1

    def test_alte_eintraege_ohne_ordner(self, conn, datenordner, postfach):
        """Gedaechtnis aus der Zeit vor den Jahresordnern (ohne "ordner"):
        der Ordner fuer alle Jahre raeumt es auf, ein Jahresordner laesst es."""
        datei = datenordner / abholen.GEDAECHTNIS
        datei.write_text(json.dumps({"eintraege": [
            {"name": "alt.pdf", "groesse": 1, "mtime_ns": 1, "sha256": None, "ziel": "x"}]}))
        jahr = abholen._Gedaechtnis(datenordner, postfach / "2026", alte_eintraege=False)
        jahr.nur_noch(set())
        jahr.speichern()
        assert "alt.pdf" in datei.read_text()
        alle = abholen._Gedaechtnis(datenordner, postfach)
        alle.nur_noch(set())
        alle.speichern()
        assert not datei.exists()

    def test_gleicher_laufeintrag_je_ordner_nur_einmal(self, conn, datenordner, tmp_path,
                                                       postfach, monkeypatch):
        """Bleibt in zwei Ordnern je eine Datei mit demselben Fehler liegen,
        schreiben abwechselnde Laeufe keinen neuen Eintrag -- vorher verglich
        _wie_zuletzt nur mit dem letzten Eintrag irgendeines Ordners."""
        a = jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        for ordner in (a, postfach):
            bo.lege(ordner, "kaputt.pdf", b"%PDF-kaputt")

        def unlesbar(selbst):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_bytes", unlesbar)
        for _ in range(3):
            hole_alle(conn, datenordner)
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEGORDNER_ABGEHOLT'") == 2

    def test_dieselbe_datei_in_zwei_jahresordnern(self, conn, datenordner, tmp_path):
        bo.lieferant(conn)
        a = jahresordner_anlegen(conn, 2025, tmp_path / "Belege 2025")
        b = jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        bo.lege(a, "Rechnung.xml", bo.cii())
        bo.lege(b, "Rechnung.xml", bo.cii())
        gesamt = hole_alle(conn, datenordner)
        assert len(gesamt.gebucht) == 1
        assert [s.beleg_id for s in gesamt.schon_vorhanden] == [gesamt.gebucht[0].beleg_id]
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_belegdatum_passt_nicht_zum_ordnerjahr(self, conn, datenordner, tmp_path, postfach):
        """Nur ein Hinweis: gebucht wird trotzdem, im Ordner fuer alle Jahre
        gibt es ihn nicht. Auch eine wartende Datei bekommt ihn. (Ohne
        Bezahlt-Vorgabe: mit Zahlung wartete ein Beleg aus dem Vorjahr schon
        deshalb -- test_eingang_bezahlt.)"""
        conn.execute("UPDATE betrieb SET belege_bezahlt_vorgabe = 0")
        bo.lieferant(conn)
        b = jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        bo.lege(b, "Alt.xml", bo.cii("RE-2025-77", "20251103"))
        bo.lege(b, "Unbekannt.xml", bo.cii("RE-9", "20251104").replace(
            b"Hetzner Online GmbH", b"Beispiel GmbH").replace(b"DE812871812", b"DE999999998"))
        bo.lege(postfach, "AuchAlt.xml", bo.cii("RE-2025-78", "20251105"))
        gesamt = hole_alle(conn, datenordner)
        assert len(gesamt.gebucht) == 2
        [teil_2026] = [t for t in gesamt.teile if t.jahr == 2026]
        assert len(teil_2026.jahr_abweichend) == 2
        assert teil_2026.jahr_abweichend[0].startswith(
            "„Alt.xml“: Belegdatum 03.11.2025, lag im Ordner 2026 — stimmt so, wenn sie 2025 "
            "bezahlt wurde")
        [teil_alle] = [t for t in gesamt.teile if t.jahr is None]
        assert teil_alle.jahr_abweichend == []

    def test_aktuelles_ergebnis_ohne_entfernten_ordner(self, conn, datenordner, tmp_path,
                                                       postfach):
        a = jahresordner_anlegen(conn, 2025, tmp_path / "Belege 2025")
        bo.lege(a, "Notiz.docx", b"PK\x03\x04")
        stand = abholen.Laufstand()
        hole_alle(conn, datenordner, stand=stand)
        assert len(abholen.aktuelles_ergebnis(stand, conn).teile) == 2
        conn.execute("DELETE FROM belegordner_jahr")
        rest = abholen.aktuelles_ergebnis(stand, conn)
        assert [t.jahr for t in rest.teile] == [None]
        assert rest.abgewiesen == []


# ------------------------------------------------------------- Web

def ordner_jahr(app_pfad) -> dict[int, str]:
    return {z["jahr"]: z["pfad"] for z in abfrage(app_pfad, "SELECT * FROM belegordner_jahr")}


class TestHinterlegen:
    def test_speichern_aendern_entfernen_mit_journal(self, klient, app_pfad, tmp_path):
        a, b = tmp_path / "Belege 2026", tmp_path / "Belege 2026 neu"
        a.mkdir()
        b.mkdir()
        antwort = klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(a)},
                              follow_redirects=True)
        assert "Eingangsordner für 2026:" in antwort.text
        assert ordner_jahr(app_pfad) == {2026: str(a.resolve())}
        # Der Haken "automatisch" bleibt, wie er war.
        assert bo.einstellung(app_pfad)[1] == 1
        klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(b)})
        klient.post("/belege/ordner", data={"jahr": "2026", "pfad": ""})
        assert ordner_jahr(app_pfad) == {}
        journal = abfrage(app_pfad, "SELECT aktion, objekt_id, wert_alt, wert_neu FROM journal"
                                    " WHERE objekttyp = 'belegordner_jahr' ORDER BY id")
        assert [(z["aktion"], z["objekt_id"]) for z in journal] == [
            ("ANLEGEN", 2026), ("AENDERN", 2026), ("LOESCHEN", 2026)]
        assert journal[1]["wert_alt"] == str(a.resolve())

    def test_seite_zeigt_die_jahre(self, klient, tmp_path):
        a = tmp_path / "Belege 2026"
        a.mkdir()
        klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(a)})
        seite = klient.get("/belege").text
        assert "<td>2026</td>" in seite
        assert "Ja, Ordner 2026 entfernen" in seite
        assert 'action="/belege/ordner/abholen"' in seite  # "Jetzt abholen" auch ohne Allejahre

    def test_aeltere_jahre_bleiben_aenderbar(self, klient, app_pfad, tmp_path):
        """Zur Auswahl stehen Vorjahr, laufendes und naechstes Jahr -- und
        aeltere Jahre, fuer die schon ein Ordner hinterlegt ist."""
        verbindung = db.verbinde(app_pfad)
        jahresordner_anlegen(verbindung, 2020, tmp_path / "Belege 2020")
        verbindung.close()
        jahr = date.today().year
        seite = klient.get("/belege").text
        for erwartet in (f"{jahr - 1} (Vorjahr", f"{jahr} (laufendes Jahr",
                         f"{jahr + 1} (nächstes Jahr", "2020 (hinterlegt, ändern)"):
            assert erwartet in seite, erwartet

    @pytest.mark.parametrize("fall", ["doppelt", "alle_jahre", "verschachtelt", "datenordner",
                                      "sicherung", "jahr"])
    def test_abgewiesen(self, klient, app_pfad, tmp_path, fall):
        a = tmp_path / "Belege 2025"
        a.mkdir()
        klient.post("/belege/ordner", data={"jahr": "2025", "pfad": str(a)})
        alle = tmp_path / "Alle"
        alle.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(alle), "automatisch": "1"})
        sicherung = tmp_path / "Sicherungen"
        klient.post("/backup/ziel", data={"pfad": str(sicherung), "zweitziel": ""})
        pfad, jahr, erwartet = {
            "doppelt": (a, "2026", "schon als Ordner 2025"),
            "alle_jahre": (alle, "2026", "schon als Ordner für alle Jahre"),
            "verschachtelt": (a / "2026", "2026", "ineinander"),
            "datenordner": (app_pfad.parent, "2026", "Datenordner"),
            "sicherung": (sicherung, "2026", "Sicherungsordner"),
            "jahr": (tmp_path / "x", "1999", "zu klein"),
        }[fall]
        antwort = klient.post("/belege/ordner", data={"jahr": jahr, "pfad": str(pfad)})
        assert antwort.status_code == 422
        assert erwartet in antwort.text
        # Der Fehler steht beim Formular fuer ein Jahr, die Eingabe bleibt.
        assert antwort.text.index('id="ordner-fehler"') > antwort.text.index('id="jahresordner"')
        assert f'value="{pfad}"' in antwort.text
        assert set(ordner_jahr(app_pfad)) == {2025}

    def test_allejahre_ordner_nicht_gleich_einem_jahresordner(self, klient, app_pfad,
                                                              tmp_path):
        a = tmp_path / "Belege 2025"
        a.mkdir()
        klient.post("/belege/ordner", data={"jahr": "2025", "pfad": str(a)})
        antwort = klient.post("/belege/ordner", data={"pfad": str(a), "automatisch": "1"})
        assert antwort.status_code == 422
        assert "schon als Ordner 2025" in antwort.text

    def test_belegter_ordner_fragt_nach(self, klient, app_pfad, tmp_path):
        """Liegen im Ordner schon Dateien, fragt die Seite (mit Empfehlung
        eines Unterordners "Eingang"); mit dem Haken wird gespeichert. Ein
        unveraenderter Ordner fragt beim erneuten Speichern nicht."""
        a = tmp_path / "Buchhaltung 2026"
        a.mkdir()
        (a / "Kontoauszug.pdf").write_bytes(b"%PDF")
        antwort = klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(a)})
        assert antwort.status_code == 422
        assert "Im Ordner liegt schon 1 Datei" in antwort.text
        assert "Eingang" in antwort.text
        assert 'name="bestaetigt" value="1"' in antwort.text
        assert ordner_jahr(app_pfad) == {}
        antwort = klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(a),
                                                      "bestaetigt": "1"}, follow_redirects=False)
        assert antwort.status_code == 303
        assert klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(a)},
                           follow_redirects=False).status_code == 303
        # Dasselbe fuer den Ordner fuer alle Jahre.
        b = tmp_path / "Alles"
        b.mkdir()
        (b / "Bescheid.pdf").write_bytes(b"%PDF")
        antwort = klient.post("/belege/ordner", data={"pfad": str(b), "automatisch": "1"})
        assert antwort.status_code == 422
        assert "Im Ordner liegt schon 1 Datei" in antwort.text

    def test_vorschlag_fuer_ein_jahr(self, klient, app_pfad, tmp_path):
        """Aus dem Ordner des Vorjahres abgeleitet, angelegt und gespeichert."""
        a = tmp_path / "Belege 2025"
        a.mkdir()
        klient.post("/belege/ordner", data={"jahr": "2025", "pfad": str(a)})
        antwort = klient.post("/belege/ordner", data={"jahr": "2026", "pfad": "",
                                                      "vorschlag": "1"}, follow_redirects=True)
        assert "angelegt und gespeichert" in antwort.text
        assert (tmp_path / "Belege 2026").is_dir()
        assert ordner_jahr(app_pfad)[2026] == str((tmp_path / "Belege 2026").resolve())

    def test_vorschlag_uebernehmen_auch_bei_gespeichertem_ordner(self, klient, tmp_path,
                                                                 monkeypatch):
        icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        icloud.mkdir(parents=True)
        monkeypatch.setattr(abholen, "vorschlag_ordner", lambda: icloud / "Steuer-Tool Belege")
        eigener = tmp_path / "Eigener"
        eigener.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(eigener), "automatisch": "1"})
        seite = klient.get("/belege").text
        assert 'name="vorschlag" value="1" class="leise">Vorschlag übernehmen' in seite


class TestUmwidmen:
    def test_als_ordner_fuer_das_jahr_fuehren(self, klient, app_pfad, tmp_path):
        alle = tmp_path / "Steuer-Tool Belege"
        alle.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(alle), "automatisch": "1"})
        jahr = date.today().year
        seite = klient.get("/belege").text
        assert f"Als Ordner für {jahr} führen" in seite
        antwort = klient.post("/belege/ordner/umwidmen", data={"jahr": str(jahr)},
                              follow_redirects=True)
        assert f"ist jetzt der Eingangsordner für {jahr}" in antwort.text
        assert bo.einstellung(app_pfad)[0] is None
        assert ordner_jahr(app_pfad) == {jahr: str(alle.resolve())}
        assert "führen</button>" not in antwort.text
        aktionen = [z["aktion"] for z in abfrage(
            app_pfad, "SELECT aktion FROM journal WHERE begruendung LIKE '%alle Jahre%'"
                      " OR begruendung LIKE '%geführt%' ORDER BY id")]
        assert aktionen == ["AENDERN", "ANLEGEN"]

    def test_hinweis_laesst_sich_abschalten(self, klient, tmp_path):
        alle = tmp_path / "Steuer-Tool Belege"
        alle.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(alle), "automatisch": "1"})
        klient.post("/belege/ordner/hinweis-aus")
        assert "führen</button>" not in klient.get("/belege").text


class TestJahreswechsel:
    def test_ab_dezember_vorschlag_fuer_das_naechste_jahr(self, klient, app_pfad, tmp_path,
                                                          heute_ist):
        heute_ist(date(2026, 12, 2))
        a = tmp_path / "Belege 2026"
        a.mkdir()
        klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(a)})
        for seite in ("/", "/belege"):
            text = klient.get(seite).text
            assert "Für 2027 ist noch kein Eingangsordner hinterlegt." in text, seite
            assert str(tmp_path / "Belege 2027") in text, seite
        # Nichts wird ungefragt angelegt.
        assert not (tmp_path / "Belege 2027").exists()
        klient.post("/belege/ordner", data={"jahr": "2027", "vorschlag": "1"})
        assert (tmp_path / "Belege 2027").is_dir()
        assert "noch kein Eingangsordner" not in klient.get("/").text

    def test_im_neuen_jahr_ohne_ordner(self, conn, tmp_path):
        conn.execute("UPDATE betrieb SET belegordner_pfad = NULL")
        jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        hinweis = jahresordner.jahreswechsel(conn, heute=date(2027, 1, 3))
        assert (hinweis.jahr, hinweis.vorschlag, hinweis.gefunden) == (
            2027, tmp_path / "Belege 2027", False)
        (tmp_path / "Belege 2027").mkdir()
        assert jahresordner.jahreswechsel(conn, heute=date(2027, 1, 3)).gefunden
        assert jahresordner.jahreswechsel(conn, heute=date(2026, 11, 30)) is None

    def test_mit_ordner_fuer_alle_jahre_kein_hinweis(self, conn, tmp_path):
        jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        assert abholen.ordner(conn) is not None  # der Postfach-Ordner aus bo.conn
        assert jahresordner.jahreswechsel(conn, heute=date(2026, 12, 2)) is None


class TestStartUndSicherung:
    def test_start_holt_auch_nur_jahresordner_ab(self, app_pfad, tmp_path):
        bo._eingerichtet(app_pfad, None)
        verbindung = db.verbinde(app_pfad)
        a = jahresordner_anlegen(verbindung, 2026, tmp_path / "Belege 2026")
        verbindung.close()
        bo.lege(a, "Notiz.docx", b"PK\x03\x04")
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            stand = bo._warte(anwendung, c)
            assert [x.originalname for x in stand.ergebnis.abgewiesen] == ["Notiz.docx"]
            assert "1 Datei nicht übernommen" in c.get("/").text

    def test_jetzt_abholen_holt_alle(self, klient, tmp_path):
        a, b = tmp_path / "Belege 2025", tmp_path / "Belege 2026"
        for ordner in (a, b):
            ordner.mkdir()
            klient.post("/belege/ordner", data={"jahr": ordner.name[-4:], "pfad": str(ordner)})
            bo.lege(ordner, "Notiz.docx", b"PK\x03\x04")
        antwort = klient.post("/belege/ordner/abholen", follow_redirects=True)
        assert "Ordner 2026: 1 Datei nicht übernommen. Ordner 2025: 1 Datei nicht" in \
            antwort.text

    def test_sicherungsziel_darf_kein_jahresordner_sein(self, klient, app_pfad, tmp_path):
        a = tmp_path / "Belege 2026"
        a.mkdir()
        klient.post("/belege/ordner", data={"jahr": "2026", "pfad": str(a)})
        antwort = klient.post("/backup/ziel", data={"pfad": str(a), "zweitziel": ""},
                              follow_redirects=False)
        assert "Eingangsordner" in unquote(antwort.headers["location"])

    def test_excel_blatt(self, conn, datenordner, tmp_path):
        from openpyxl import load_workbook

        from app.logik import export_excel

        jahresordner_anlegen(conn, 2026, tmp_path / "Belege 2026")
        export_excel.schreibe(conn, datenordner / "daten.xlsx")
        blatt = load_workbook(datenordner / "daten.xlsx")["Eingangsordner je Jahr"]
        assert [z.value for z in blatt[2]][:2] == [2026, str(tmp_path / "Belege 2026")]


def test_check_auf_das_jahr(conn, tmp_path):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO belegordner_jahr VALUES (2101, ?, 'x')", (str(tmp_path),))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO belegordner_jahr VALUES (2026, '  ', 'x')")

"""Der Leistungszeitraum ist keine Pflichtangabe -- aber nur für Kleinunternehmer.

§ 34a Satz 1 UStDV listet sechs Angaben, und der Zeitpunkt bzw. Zeitraum der
Leistung ist nicht darunter. Bei Regelbesteuerung verlangt § 14 Abs. 4 Nr. 6
UStG ihn dagegen. Die Regel hängt deshalb am Steuerregime und nicht an einer
globalen Einstellung.
"""

import subprocess

import pytest

from app import db
from app.logik import festschreiben as fs
from app.logik import validierung as v


@pytest.fixture
def ordner(tmp_path):
    return tmp_path / "belege"


@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
    conn.execute(
        "UPDATE kunde SET satz_cent = 8000, angelegt_am = '2020-01-01' WHERE id = ?",
        (kunde_id,),
    )
    return kunde_id


def _zeit(conn, kunde_id):
    cur = conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
        " VALUES (?, 'Beratung', '2026-03-02', 60, ?)",
        (kunde_id, db.jetzt_utc()),
    )
    return int(cur.lastrowid)


def _pdf_text(pfad: str) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", pfad, "-"], capture_output=True, text=True
    ).stdout


class TestAusgabeSchalten:
    def test_standard_zeigt_den_zeitraum(self, conn, eingerichtet, ordner):
        rid = fs.entwurf_anlegen(conn, eingerichtet, [_zeit(conn, eingerichtet)])
        ergebnis = fs.festschreiben(conn, rid, belegordner=ordner)
        assert "Leistungszeitraum" in _pdf_text(ergebnis.pdf_pfad)

    def test_abgewaehlt_verschwindet_er_vom_papier(self, conn, eingerichtet, ordner):
        rid = fs.entwurf_anlegen(
            conn, eingerichtet, [_zeit(conn, eingerichtet)], leistungszeitraum_zeigen=False
        )
        ergebnis = fs.festschreiben(conn, rid, belegordner=ordner)
        text = _pdf_text(ergebnis.pdf_pfad)
        assert "Leistungszeitraum" not in text
        # Alle Pflichtangaben stehen weiter drauf.
        for pflicht in ("RE-2026-0001", "Mustermann GmbH", "12/345/67890", "§ 19", "80,00"):
            assert pflicht in text

    def test_die_daten_bleiben_gespeichert(self, conn, eingerichtet, ordner):
        """Daten wegwerfen, um eine Zeile nicht zu drucken, wäre der falsche Hebel:
        sie tragen die Plausibilitätsprüfungen und den Prüfpfad."""
        rid = fs.entwurf_anlegen(
            conn, eingerichtet, [_zeit(conn, eingerichtet)], leistungszeitraum_zeigen=False
        )
        fs.festschreiben(conn, rid, belegordner=ordner)
        zeile = conn.execute(
            "SELECT leistung_von, leistung_bis, leistungszeitraum_zeigen"
            " FROM rechnung WHERE id = ?",
            (rid,),
        ).fetchone()
        assert zeile["leistung_von"] == "2026-03-02"
        assert zeile["leistung_bis"] == "2026-03-02"
        assert zeile["leistungszeitraum_zeigen"] == 0

    def test_betriebsvorbelegung_greift(self, conn, eingerichtet):
        conn.execute("UPDATE betrieb SET leistungszeitraum_default = 0 WHERE id = 1")
        rid = fs.entwurf_anlegen(conn, eingerichtet, [_zeit(conn, eingerichtet)])
        assert conn.execute(
            "SELECT leistungszeitraum_zeigen FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()[0] == 0

    def test_ausdrueckliche_angabe_schlaegt_die_vorbelegung(self, conn, eingerichtet):
        conn.execute("UPDATE betrieb SET leistungszeitraum_default = 0 WHERE id = 1")
        rid = fs.entwurf_anlegen(
            conn, eingerichtet, [_zeit(conn, eingerichtet)], leistungszeitraum_zeigen=True
        )
        assert conn.execute(
            "SELECT leistungszeitraum_zeigen FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()[0] == 1

    def test_storno_sieht_aus_wie_das_original(self, conn, eingerichtet, ordner):
        rid = fs.entwurf_anlegen(
            conn, eingerichtet, [_zeit(conn, eingerichtet)], leistungszeitraum_zeigen=False
        )
        fs.festschreiben(conn, rid, belegordner=ordner)
        storno = fs.storniere(conn, rid, "Tippfehler", belegordner=ordner)
        assert "Leistungszeitraum" not in _pdf_text(storno.pdf_pfad)


class TestKeinePflichtFuerKleinunternehmer:
    def test_abgewaehlt_blockiert_nicht(self, conn, eingerichtet):
        rid = fs.entwurf_anlegen(
            conn, eingerichtet, [_zeit(conn, eingerichtet)], leistungszeitraum_zeigen=False
        )
        befunde = v.pruefe(conn, rid)
        assert not v.blockiert(befunde)
        assert not {b.code for b in befunde} & {"R14", "R15"}

    def test_ganz_ohne_zeitraum_blockiert_nicht(self, conn, eingerichtet):
        rid = fs.entwurf_anlegen(conn, eingerichtet, [_zeit(conn, eingerichtet)])
        conn.execute(
            "UPDATE rechnung SET leistung_von = NULL, leistung_bis = NULL WHERE id = ?", (rid,)
        )
        assert not v.blockiert(v.pruefe(conn, rid))


class TestPflichtBeiRegelbesteuerung:
    """§ 14 Abs. 4 Nr. 6 UStG verlangt den Zeitpunkt der Leistung."""

    def test_fehlender_zeitraum_blockiert(self, conn, eingerichtet):
        rid = fs.entwurf_anlegen(conn, eingerichtet, [_zeit(conn, eingerichtet)])
        conn.execute(
            "UPDATE rechnung SET steuerregime = 'REGEL', leistung_von = NULL,"
            " leistung_bis = NULL WHERE id = ?",
            (rid,),
        )
        befunde = v.pruefe(conn, rid)
        assert "R14" in {b.code for b in befunde}
        assert v.blockiert(befunde)
        assert any("14 Abs. 4 Nr. 6" in (b.fundstelle or "") for b in befunde)

    def test_ausgeblendeter_zeitraum_blockiert(self, conn, eingerichtet):
        rid = fs.entwurf_anlegen(
            conn, eingerichtet, [_zeit(conn, eingerichtet)], leistungszeitraum_zeigen=False
        )
        conn.execute("UPDATE rechnung SET steuerregime = 'REGEL' WHERE id = ?", (rid,))
        befunde = v.pruefe(conn, rid)
        assert "R15" in {b.code for b in befunde}
        assert v.blockiert(befunde)

    def test_sichtbarer_zeitraum_ist_in_ordnung(self, conn, eingerichtet):
        rid = fs.entwurf_anlegen(
            conn, eingerichtet, [_zeit(conn, eingerichtet)], leistungszeitraum_zeigen=True
        )
        conn.execute("UPDATE rechnung SET steuerregime = 'REGEL' WHERE id = ?", (rid,))
        assert not {b.code for b in v.pruefe(conn, rid)} & {"R14", "R15"}

    def test_leistungsnachweis_ist_davon_unberuehrt(self, conn, eingerichtet):
        rid = fs.entwurf_anlegen(conn, eingerichtet, [_zeit(conn, eingerichtet)])
        conn.execute(
            "UPDATE rechnung SET steuerregime = 'REGEL', typ = 'LEISTUNGSNACHWEIS',"
            " netto_cent = 0, brutto_cent = 0,"
            " leistung_von = NULL, leistungszeitraum_zeigen = 0 WHERE id = ?",
            (rid,),
        )
        conn.execute("DELETE FROM rechnungsposition WHERE rechnung_id = ?", (rid,))
        assert not {b.code for b in v.pruefe(conn, rid)} & {"R14", "R15"}

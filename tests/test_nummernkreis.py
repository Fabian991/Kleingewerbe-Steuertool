"""Nummernvergabe. Luecken entstehen, wenn die Nummer zu frueh gezogen wird."""

import pytest

from app import db
from app.logik import nummernkreis as nk


def test_formatiere_platzhalter():
    assert nk.formatiere("RE-{JJJJ}-{NNNN}", 2026, 42) == "RE-2026-0042"
    assert nk.formatiere("{JJ}-{NNN}", 2026, 7) == "26-007"
    assert nk.formatiere("{JJJJ}{MM}-{NNNNNN}", 2026, 1, monat=3) == "202603-000001"
    assert nk.formatiere("R{N}", 2026, 1234) == "R1234", "Zaehler wird nie abgeschnitten"


@pytest.mark.parametrize("vorlage", ["", "   ", "RE-2026", "RE-{XYZ}-{NNNN}"])
def test_untaugliche_formate_werden_abgewiesen(vorlage):
    with pytest.raises(ValueError):
        nk.pruefe_format(vorlage)


def test_format_ohne_jahr_mit_jaehrlichem_neustart_wird_abgewiesen():
    """Die Kombination erzeugt GARANTIERT doppelte Nummern.

    Ein Format ohne Jahresanteil ist tadellos, solange der Zaehler durchlaeuft.
    Beginnt er jedes Jahr wieder bei 1, vergibt er 2027 dieselben Nummern wie
    2026 -- und zwei Belege mit derselben Nummer sind in einer Pruefung nicht
    auseinanderzuhalten. Format und Zaehlweise sind deshalb nur GEMEINSAM
    pruefbar.
    """
    nk.pruefe_format("{NNNN}", reset_jaehrlich=False)        # in Ordnung
    with pytest.raises(ValueError, match="doppelte Nummern"):
        nk.pruefe_format("{NNNN}", reset_jaehrlich=True)


class TestVergabe:
    def test_zaehler_laeuft_hoch(self, conn):
        nk.sichere_kreis(conn, 2026)
        assert nk.ziehe_nummer(conn, 2026).text == "RE-2026-0001"
        assert nk.ziehe_nummer(conn, 2026).text == "RE-2026-0002"
        assert nk.ziehe_nummer(conn, 2026).seq == 3

    def test_startwert_fuer_den_umstieg(self, conn):
        """Wer vorher in Word nummeriert hat, setzt hier die naechste freie Nummer."""
        nk.sichere_kreis(conn, 2026, start_seq=118)
        assert nk.ziehe_nummer(conn, 2026).text == "RE-2026-0118"

    def test_sichere_kreis_ist_idempotent(self, conn):
        nk.sichere_kreis(conn, 2026, start_seq=5)
        nk.ziehe_nummer(conn, 2026)
        nk.sichere_kreis(conn, 2026, start_seq=99)
        assert nk.vorschau(conn, 2026) == "RE-2026-0006", "Ein zweiter Aufruf setzt nichts zurueck"

    def test_jahre_sind_getrennt(self, conn):
        nk.sichere_kreis(conn, 2026)
        nk.sichere_kreis(conn, 2027)
        nk.ziehe_nummer(conn, 2026)
        nk.ziehe_nummer(conn, 2026)
        assert nk.ziehe_nummer(conn, 2027).text == "RE-2027-0001"

    def test_vorschau_verbraucht_nichts(self, conn):
        nk.sichere_kreis(conn, 2026)
        assert nk.vorschau(conn, 2026) == "RE-2026-0001"
        assert nk.vorschau(conn, 2026) == "RE-2026-0001"
        assert nk.ziehe_nummer(conn, 2026).text == "RE-2026-0001"

    def test_fehlender_kreis_meldet_sich(self, conn):
        with pytest.raises(LookupError, match="Nummernkreis"):
            nk.ziehe_nummer(conn, 2026)

    def test_rollback_verbraucht_die_nummer_nicht(self, conn, kunde_id):
        """Scheitert das PDF-Rendering, darf die Nummer nicht verloren sein."""
        nk.sichere_kreis(conn, 2026)
        conn.execute("BEGIN IMMEDIATE")
        gezogen = nk.ziehe_nummer(conn, 2026)
        assert gezogen.text == "RE-2026-0001"
        conn.execute("ROLLBACK")
        assert nk.vorschau(conn, 2026) == "RE-2026-0001"
        assert nk.ziehe_nummer(conn, 2026).text == "RE-2026-0001"


class TestJahreswechsel:
    """Der Fehler, der genau einmal im Jahr auftritt -- am 1. Januar.

    ``festschreiben`` ruft ``sichere_kreis(conn, jahr)`` ohne Format auf: beim
    Festschreiben ist das einmal eingerichtete Format nicht zur Hand. Fiele die
    Funktion dort auf die Werkseinstellung zurueck, wuerde aus der Nummer 63
    still ein "RE-2027-0001" -- und bei einem jahresuebergreifenden Zaehler
    zusaetzlich eine Nummer, die es schon gibt.
    """

    def test_format_wird_ins_neue_jahr_vererbt(self, conn):
        nk.sichere_kreis(conn, 2026, vorlage="{NNN}", reset_jaehrlich=False, start_seq=62)
        nk.ziehe_nummer(conn, 2026)
        nk.sichere_kreis(conn, 2027)              # wie festschreiben() es aufruft
        assert nk.ziehe_nummer(conn, 2027).text == "063"

    def test_zaehler_laeuft_ueber_den_jahreswechsel_durch(self, conn):
        nk.sichere_kreis(conn, 2026, vorlage="{NNN}", reset_jaehrlich=False, start_seq=62)
        assert nk.ziehe_nummer(conn, 2026).text == "062"
        nk.sichere_kreis(conn, 2027)
        assert nk.ziehe_nummer(conn, 2027).text == "063"
        # Und der Nachzuegler: die Dezemberrechnung, im Januar nachgetragen.
        # Sie darf NICHT wieder bei 62 anfangen.
        assert nk.ziehe_nummer(conn, 2026).text == "064"

    def test_jaehrlicher_neustart_bleibt_jaehrlich(self, conn):
        """Die Vererbung darf die andere Zaehlweise nicht kaputtmachen."""
        nk.sichere_kreis(conn, 2026, vorlage="RE-{JJJJ}-{NNNN}", reset_jaehrlich=True)
        nk.ziehe_nummer(conn, 2026)
        nk.ziehe_nummer(conn, 2026)
        nk.sichere_kreis(conn, 2027)
        assert nk.ziehe_nummer(conn, 2027).text == "RE-2027-0001"

    def test_vorschau_zeigt_das_geerbte_format(self, conn):
        """Sonst verspricht die Vorschau etwas anderes, als dann vergeben wird."""
        nk.sichere_kreis(conn, 2026, vorlage="{NNN}", reset_jaehrlich=False, start_seq=62)
        assert nk.vorschau(conn, 2027) == "062"

    def test_umstellung_hebt_den_zaehler_nie_herunter(self, conn, kunde_id):
        """Ein Tippfehler im Formular darf keine Nummer ein zweites Mal vergeben."""
        nk.sichere_kreis(conn, 2026, vorlage="{NNN}", reset_jaehrlich=False, start_seq=62)
        for _ in range(3):
            nummer = nk.ziehe_nummer(conn, 2026)
            conn.execute(
                "INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id,"
                " datum, netto_cent, brutto_cent, status, festgeschrieben_am, erstellt_am)"
                " VALUES ('RECHNUNG', ?, ?, 2026, ?, '2026-05-01', 1000, 1000,"
                " 'FESTGESCHRIEBEN', ?, ?)",
                (nummer.text, nummer.seq, kunde_id, db.jetzt_utc(), db.jetzt_utc()),
            )
        nk.stelle_um(conn, 2026, vorlage="{NNN}", start_seq=1, reset_jaehrlich=False)
        assert nk.vorschau(conn, 2026) == "065", "62, 63, 64 sind vergeben"

    def test_umstellung_raeumt_die_andere_zaehlweise_weg(self, conn):
        """Sonst entschiede die Sortierung, aus welchem Kreis gezogen wird."""
        nk.sichere_kreis(conn, 2026, vorlage="RE-{JJJJ}-{NNNN}", reset_jaehrlich=True)
        nk.stelle_um(conn, 2026, vorlage="{NNN}", start_seq=62, reset_jaehrlich=False)
        assert nk.ziehe_nummer(conn, 2026).text == "062"
        zeilen = conn.execute(
            "SELECT jahr FROM nummernkreis WHERE typ = 'RECHNUNG'"
        ).fetchall()
        assert [z["jahr"] for z in zeilen] == [nk.KREIS_DURCHLAUFEND]


class TestLuecken:
    def _rechnung(self, conn, kunde_id, seq):
        conn.execute(
            """
            INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                                  netto_cent, brutto_cent,
                                  status, festgeschrieben_am, erstellt_am)
            VALUES ('RECHNUNG', ?, ?, 2026, ?, '2026-03-01', 10000, 10000,
                    'FESTGESCHRIEBEN', ?, ?)
            """,
            (f"RE-2026-{seq:04d}", seq, kunde_id, db.jetzt_utc(), db.jetzt_utc()),
        )

    def test_keine_luecke_bei_durchlaufender_reihe(self, conn, kunde_id):
        for seq in (1, 2, 3):
            self._rechnung(conn, kunde_id, seq)
        assert nk.luecken(conn, 2026) == []

    def test_luecken_werden_gefunden(self, conn, kunde_id):
        for seq in (1, 2, 5, 7):
            self._rechnung(conn, kunde_id, seq)
        assert nk.luecken(conn, 2026) == [3, 4, 6]

    def test_leeres_jahr_hat_keine_luecken(self, conn):
        assert nk.luecken(conn, 2026) == []


class TestAlleDokumentartenZaehlen:
    """Rechnung, Storno und Leistungsnachweis ziehen aus EINEM Kreis.

    Wer beim Anheben des Zaehlers nur Rechnungen ansieht, uebersieht die
    Nummer des letzten Stornos -- und vergibt sie noch einmal.
    """

    def _dokument(self, conn, kunde_id, typ, seq, jahr=2026, storno_von=None):
        cur = conn.execute(
            """
            INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                                  netto_cent, brutto_cent, status, storno_von_id,
                                  storno_grund, festgeschrieben_am, erstellt_am)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'FESTGESCHRIEBEN', ?, ?, ?, ?)
            """,
            (typ, f"RE-{jahr}-{seq:04d}", seq, jahr, kunde_id, f"{jahr}-05-01",
             100 if typ != "STORNO" else -100, 100 if typ != "STORNO" else -100,
             storno_von, "Probe" if typ == "STORNO" else None,
             db.jetzt_utc(), db.jetzt_utc()),
        )
        return int(cur.lastrowid)

    def test_umstellung_sieht_die_stornonummer(self, conn, kunde_id):
        original = self._dokument(conn, kunde_id, "RECHNUNG", 62)
        self._dokument(conn, kunde_id, "STORNO", 63, storno_von=original)
        nk.stelle_um(conn, 2026, vorlage="{N}", start_seq=1, reset_jaehrlich=False)
        assert nk.vorschau(conn, 2026) == "64", "63 gehoert dem Storno"

    def test_neuer_jahreskreis_beginnt_nicht_unter_vergebenen_nummern(self, conn, kunde_id):
        """Die Zeile des Kreises fehlt, die Rechnungen des Jahres nicht --
        etwa nach einer Umstellung der Zaehlweise. Bei 1 zu beginnen hiesse,
        die erste Nummer des Jahres ein zweites Mal zu vergeben."""
        self._dokument(conn, kunde_id, "RECHNUNG", 1)
        self._dokument(conn, kunde_id, "STORNO", 2, storno_von=1)
        nk.sichere_kreis(conn, 2026)
        assert nk.ziehe_nummer(conn, 2026).text == "RE-2026-0003"

    def test_storno_ist_keine_luecke(self, conn, kunde_id):
        nk.sichere_kreis(conn, 2026)
        original = self._dokument(conn, kunde_id, "RECHNUNG", 1)
        self._dokument(conn, kunde_id, "STORNO", 2, storno_von=original)
        self._dokument(conn, kunde_id, "RECHNUNG", 3)
        assert nk.luecken(conn, 2026) == []

    def test_zwei_stornos_zu_einer_rechnung_weist_die_datenbank_ab(self, conn, kunde_id):
        original = self._dokument(conn, kunde_id, "RECHNUNG", 1)
        self._dokument(conn, kunde_id, "STORNO", 2, storno_von=original)
        with pytest.raises(Exception, match="UNIQUE"):
            self._dokument(conn, kunde_id, "STORNO", 3, storno_von=original)

"""Dauereingabe. Sie muss alles schlucken, was man unterwegs tippt."""

import pytest

from app.logik import zeiten as z


class TestParseDauer:
    @pytest.mark.parametrize(
        ("eingabe", "minuten"),
        [
            ("1:45", 105), ("0:30", 30), ("12:00", 720),
            ("1h45", 105), ("1 h 45", 105), ("1h45m", 105), ("1h45min", 105),
            ("1h", 60), ("2 Std", 120), ("2std", 120), ("3 stunden", 180),
            ("1,75", 105), ("1.75", 105), ("1,75h", 105), ("0,5", 30), ("0,25", 15),
            ("105", 105), ("105m", 105), ("90min", 90), ("45 Minuten", 45),
            ("  1:45  ", 105), ("1:45".upper(), 105),
        ],
    )
    def test_varianten(self, eingabe, minuten):
        assert z.parse_dauer(eingabe) == minuten

    def test_blanke_zahl_gilt_als_minuten(self):
        """Wer Stunden meint, schreibt 2h oder 2,0 -- sonst waere jede Eingabe eine Falle."""
        assert z.parse_dauer("2") == 2
        assert z.parse_dauer("2h") == 120
        assert z.parse_dauer("2,0") == 120

    def test_dezimalstunden_werden_kaufmaennisch_gerundet(self):
        assert z.parse_dauer("1,333") == 80  # 79,98 min -> 80
        assert z.parse_dauer("0,01") == 1    # 0,6 min -> 1

    def test_zu_kleine_dezimalangabe_wird_abgewiesen(self):
        """0,008 h sind 0,48 Minuten und runden auf null -- keine Dauer.

        Die Meldung geht an den Nutzer und traegt deshalb echte Umlaute; vorher
        pruefte der Test die ASCII-Umschrift "groesser".
        """
        with pytest.raises(z.DauerUngueltig, match="größer als null"):
            z.parse_dauer("0,008")

    @pytest.mark.parametrize("eingabe", [
        "", "   ", None, "abc", "1:99", "-5", "0", "0:00", "1:2:3",
    ])
    def test_untaugliche_eingaben(self, eingabe):
        with pytest.raises(z.DauerUngueltig):
            z.parse_dauer(eingabe)

    def test_fehlermeldung_nennt_beispiele(self):
        with pytest.raises(z.DauerUngueltig, match="1:45"):
            z.parse_dauer("viertel stunde")


class TestAnzeige:
    @pytest.mark.parametrize(
        ("minuten", "text"), [(0, "0:00"), (5, "0:05"), (105, "1:45"), (720, "12:00")]
    )
    def test_formatiere(self, minuten, text):
        assert z.formatiere_dauer(minuten) == text

    @pytest.mark.parametrize(
        ("minuten", "text"), [(0, "0,00"), (45, "0,75"), (105, "1,75"), (49, "0,82")]
    )
    def test_dezimalstunden(self, minuten, text):
        assert z.dezimalstunden(minuten) == text

    def test_hin_und_zurueck(self):
        for minuten in (1, 7, 30, 49, 105, 480, 1439):
            assert z.parse_dauer(z.formatiere_dauer(minuten)) == minuten


class TestMinutenZwischen:
    def test_normalfall(self):
        assert z.minuten_zwischen("09:00", "10:45") == 105

    def test_endzeit_vor_startzeit(self):
        with pytest.raises(z.DauerUngueltig):
            z.minuten_zwischen("12:00", "09:00")


class TestLaufenderTimer:
    def test_kein_timer(self, conn):
        assert z.laufender_timer(conn) is None

    def test_timer_mit_laufzeit(self, conn, kunde_id):
        from app import db

        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, start_zeit,"
            " dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', ?, '09:00', 1, ?)",
            (kunde_id, db.heute(), db.jetzt_utc()),
        )
        timer = z.laufender_timer(conn, jetzt="10:45")
        assert timer is not None
        assert timer.minuten == 105
        assert timer.titel == "Beratung"
        assert timer.kunde == "Mustermann GmbH"
        assert timer.verdaechtig is False
        assert timer.start_anzeige == "09:00"

    def test_vergessener_timer_wird_markiert(self, conn, kunde_id):
        """Statt stillschweigend 14 Stunden zu buchen, fragt das Tool nach."""
        from app import db

        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, start_zeit,"
            " dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', ?, '08:00', 1, ?)",
            (kunde_id, db.heute(), db.jetzt_utc()),
        )
        assert z.laufender_timer(conn, jetzt="22:30").verdaechtig is True

    def test_timer_von_gestern_ist_erst_recht_verdaechtig(self, conn, kunde_id):
        """Der vergessene Timer schlechthin: gestern 08:00 gestartet, heute
        08:00 noch an. Vorher galt er mit "0 Minuten" als unauffaellig, weil
        nur Uhrzeiten verglichen wurden."""
        from app import db

        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, start_zeit,"
            " dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', '2026-10-12', '08:00', 1, ?)",
            (kunde_id, db.jetzt_utc()),
        )
        timer = z.laufender_timer(conn, jetzt="08:00", heute="2026-10-13")
        assert timer.minuten == 24 * 60
        assert timer.verdaechtig is True

    def test_timer_ueber_mitternacht_zaehlt_richtig(self, conn, kunde_id):
        from app import db

        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, start_zeit,"
            " dauer_minuten, erfasst_am)"
            " VALUES (?, 'Nachtschicht', '2026-10-12', '23:30', 1, ?)",
            (kunde_id, db.jetzt_utc()),
        )
        timer = z.laufender_timer(conn, jetzt="00:45", heute="2026-10-13")
        assert timer.minuten == 75
        assert timer.verdaechtig is False


class TestEuroNachCent:
    @pytest.mark.parametrize(
        ("eingabe", "cent"),
        [("85,00", 8500), ("85", 8500), ("1.234,56", 123456), ("0,01", 1),
         ("85,00 €", 8500), (" 85,00 ", 8500), ("", None), (None, None)],
    )
    def test_deutsche_schreibweise(self, eingabe, cent):
        from app.logik.kunden import euro_nach_cent

        assert euro_nach_cent(eingabe) == cent


class TestBetragseingabe:
    """Der deutsche Tausenderpunkt ohne Komma.

    „1.234" sind 1234 Euro. Gelesen wurde es als 1,23 — Faktor 1000 zu klein,
    und zwar lautlos: der Betrag landete so auf einer Rechnung oder als
    Stundensatz im Kundenstamm.
    """

    def test_tausenderpunkt_ohne_komma(self):
        from app.logik.kunden import euro_nach_cent

        assert euro_nach_cent("1.234") == 123400
        assert euro_nach_cent("1.000") == 100000
        assert euro_nach_cent("1.234.567") == 123456700

    def test_mit_komma_unveraendert(self):
        from app.logik.kunden import euro_nach_cent

        assert euro_nach_cent("1.234,56") == 123456
        assert euro_nach_cent("85,00") == 8500

    def test_punkt_als_dezimaltrenner_bleibt_lesbar(self):
        """Wer englische Tastaturgewohnheiten hat, tippt 12.50 für 12,50 €.

        Nur ein Punkt mit GENAU drei Ziffern dahinter kann ein Tausendertrenner
        sein — alles andere wird weiter als Dezimaltrenner gelesen.
        """
        from app.logik.kunden import euro_nach_cent

        assert euro_nach_cent("12.5") == 1250
        assert euro_nach_cent("12.50") == 1250

    def test_unlesbares_wirft_arithmeticerror(self):
        import pytest

        from app.logik.kunden import euro_nach_cent

        with pytest.raises(ArithmeticError):
            euro_nach_cent("abc")

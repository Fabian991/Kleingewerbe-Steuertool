"""Rundung ist der Baustein, der stillschweigend Geld verschiebt."""

import pytest

from app.logik import rundung as r


class TestRundungAufDieSumme:
    def test_sieben_mal_sieben_minuten_ergibt_45_nicht_105(self):
        """Der Praxisfall, der den ganzen Baustein rechtfertigt."""
        auf_summe = r.berechne_position([7] * 7, 8000)
        assert auf_summe.roh_minuten == 49
        assert auf_summe.gerundete_minuten == 45

        pro_eintrag = sum(
            r.berechne_position(7, 8000, modus=r.AUFWAERTS).gerundete_minuten for _ in range(7)
        )
        assert pro_eintrag == 105, "Rundung pro Eintrag kumuliert den Aufschlag"

    def test_menge_und_betrag_sind_nachrechenbar(self):
        p = r.berechne_position([7] * 7, 8000)
        assert p.menge_tausendstel == 750
        assert p.menge_stunden_text == "0,75"
        assert p.betrag_cent == 6000  # 0,75 x 80,00 €

    @pytest.mark.parametrize(
        ("minuten", "modus", "erwartet"),
        [
            (49, r.AUFWAERTS, 60),
            (49, r.KAUFMAENNISCH, 45),
            (49, r.ABWAERTS, 45),
            (45, r.KAUFMAENNISCH, 45),
            (8, r.KAUFMAENNISCH, 15),  # 0,53 Einheiten -> kaufmaennisch auf 1
            (7, r.KAUFMAENNISCH, 0),  # 0,47 Einheiten -> kaufmaennisch auf 0
            (7, r.AUFWAERTS, 15),
            (49, r.KEINE, 49),
        ],
    )
    def test_modi(self, minuten, modus, erwartet):
        assert r.runde_minuten(minuten, 15, modus) == erwartet

    def test_kaufmaennisch_rundet_die_haelfte_aufwaerts(self):
        assert r.runde_minuten(approx := 1125, 150, r.KAUFMAENNISCH) == 1200
        assert approx == 1125

    def test_mindestdauer_greift_nach_der_rundung(self):
        assert r.runde_minuten(7, 15, r.ABWAERTS) == 0
        assert r.runde_minuten(7, 15, r.ABWAERTS, mindest_minuten=30) == 30

    def test_abwaerts_kann_position_auf_null_bringen(self):
        """Gewollt und von der Validierung zu melden, nicht hier zu kaschieren."""
        p = r.berechne_position(49, 8000, intervall_min=60, modus=r.ABWAERTS)
        assert p.gerundete_minuten == 0
        assert p.betrag_cent == 0

    def test_unbekannter_modus_wird_abgewiesen(self):
        with pytest.raises(ValueError, match="Rundungsmodus"):
            r.runde_minuten(10, 15, "VIELLEICHT")

    def test_negative_dauer_wird_abgewiesen(self):
        with pytest.raises(ValueError):
            r.runde_minuten(-5, 15)


class TestMengeUndBetrag:
    @pytest.mark.parametrize(
        ("minuten", "tausendstel"),
        [(0, 0), (45, 750), (60, 1000), (49, 820), (90, 1500), (105, 1750), (1, 20)],
    )
    def test_dezimalstunden(self, minuten, tausendstel):
        assert r.menge_tausendstel_aus_minuten(minuten) == tausendstel

    def test_betrag_rundet_kaufmaennisch_auf_cent(self):
        # 0,82 h x 73,33 € = 60,1306 € -> 60,13 €
        assert r.betrag_cent(820, 7333) == 6013

    def test_summe_kommt_aus_den_positionen(self):
        positionen = [
            r.berechne_position(100, 8000),
            r.berechne_position(35, 8000),
            r.pauschalposition(15000),
        ]
        # 100 min -> 105 min = 1,75 h x 80,00 € = 140,00 €
        # 35 min  ->  30 min = 0,50 h x 80,00 € =  40,00 €
        assert r.rechnungssumme_cent(positionen) == 14000 + 4000 + 15000

    def test_pauschale_ignoriert_die_dauer(self):
        p = r.pauschalposition(25000)
        assert p.betrag_cent == 25000
        assert p.gerundete_minuten == 0

    def test_null_euro_position_ist_erlaubt(self):
        """Kulanzstunden: Menge groesser null, Betrag null."""
        p = r.berechne_position([60, 30], 0)
        assert p.menge_tausendstel == 1500
        assert p.betrag_cent == 0


class TestEuroFormat:
    @pytest.mark.parametrize(
        ("cent", "text"),
        [(0, "0,00"), (5, "0,05"), (6000, "60,00"), (123456, "1.234,56"),
         (100000000, "1.000.000,00"), (-6000, "-60,00")],
    )
    def test_deutsche_schreibweise(self, cent, text):
        assert r.euro(cent) == text

"""Zeiten zu Positionen. Hier entsteht die Rechnungssumme."""

import pytest

from app.logik import gruppierung as g
from app.logik import rundung


def zeit(id_, datum, titel, minuten, modus="STUNDEN", **rest):
    return {
        "id": id_, "datum": datum, "titel": titel, "dauer_minuten": minuten,
        "abrechnungsmodus": modus, "beschreibung": rest.get("beschreibung"),
        "grund": rest.get("grund"), "pauschal_cent": rest.get("pauschal_cent"),
        "satz_cent_snapshot": rest.get("satz_cent_snapshot"),
    }


SIEBEN = [zeit(i, "2026-03-02", "Beratung", 7) for i in range(1, 8)]


class TestModi:
    @pytest.mark.parametrize("modus", g.MODI)
    def test_jeder_modus_erzeugt_positionen(self, modus):
        positionen = g.erzeuge_positionen(SIEBEN, modus=modus, standard_cent=8000)
        assert positionen
        assert all(p.menge_tausendstel > 0 for p in positionen), "Keine Position ohne Menge"

    def test_sammel_ergibt_eine_position_mit_monatsname(self):
        positionen = g.erzeuge_positionen(SIEBEN, modus=g.SAMMEL, standard_cent=8000)
        assert len(positionen) == 1
        assert positionen[0].bezeichnung == "Dienstleistung März 2026"
        assert positionen[0].betrag_cent == 6000

    def test_sammel_ueber_monatsgrenze_nennt_den_zeitraum(self):
        eintraege = [zeit(1, "2026-02-25", "A", 60), zeit(2, "2026-03-03", "B", 60)]
        positionen = g.erzeuge_positionen(eintraege, modus=g.SAMMEL, standard_cent=8000)
        assert "25.02.2026 – 03.03.2026" in positionen[0].bezeichnung

    def test_pro_titel_gruppiert_und_sortiert_chronologisch(self):
        eintraege = [
            zeit(1, "2026-03-05", "Entwicklung", 60),
            zeit(2, "2026-03-02", "Beratung", 60),
            zeit(3, "2026-03-07", "Beratung", 30),
        ]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert [p.bezeichnung for p in positionen] == ["Beratung", "Entwicklung"]
        assert positionen[0].menge_tausendstel == 1500  # 90 min

    def test_pro_tag_nennt_den_titel_nur_wenn_eindeutig(self):
        eintraege = [
            zeit(1, "2026-03-02", "Beratung", 60),
            zeit(2, "2026-03-03", "Beratung", 60),
            zeit(3, "2026-03-03", "Entwicklung", 60),
        ]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TAG, standard_cent=8000)
        assert positionen[0].bezeichnung == "02.03.2026 — Beratung"
        assert positionen[1].bezeichnung == "03.03.2026 — diverse Leistungen"

    def test_pro_eintrag_rechnet_auf_die_minute(self):
        """Intervallrundung je Eintrag vervielfacht die Summe: sieben Eintraege
        a 7 Minuten sind 49 Minuten, je Eintrag aufwaerts gerundet aber 105."""
        positionen = g.erzeuge_positionen(SIEBEN, modus=g.PRO_EINTRAG, standard_cent=8000)
        assert len(positionen) == 7
        assert all(p.menge_tausendstel == 120 for p in positionen)  # 7 min = 0,12 h
        assert g.summe_cent(positionen) == 6720
        assert g.summe_cent(positionen) < 7 * 15 / 60 * 8000, "Nicht aufs Intervall gerundet"

    def test_unbekannter_modus_wird_abgewiesen(self):
        with pytest.raises(ValueError, match="Gruppierungsmodus"):
            g.erzeuge_positionen(SIEBEN, modus="IRGENDWIE")


class TestRundungAufDieSumme:
    def test_sieben_mal_sieben_minuten_ergeben_45_minuten(self):
        positionen = g.erzeuge_positionen(SIEBEN, modus=g.PRO_TITEL, standard_cent=8000)
        assert positionen[0].roh_minuten == 49
        assert positionen[0].gerundete_minuten == 45
        assert positionen[0].menge_text == "0,75"

    def test_sicherheitsnetz_verhindert_position_ohne_menge(self):
        """Die Rundung kann eine Position auf null bringen -- 49 Minuten abwaerts
        auf ein 60er-Intervall. Eine Position ohne Menge ist unzulaessig und
        ergaebe eine Rechnung ueber 0,00 EUR."""
        positionen = g.erzeuge_positionen(
            [zeit(1, "2026-03-02", "X", 49)], modus=g.PRO_TITEL, standard_cent=8000,
            intervall_min=60, modus_rundung=rundung.ABWAERTS,
        )
        assert positionen[0].menge_tausendstel == 820
        assert positionen[0].betrag_cent == 6560

    def test_summen_je_modus_sind_vergleichbar(self):
        summen = g.vorschau_alle_modi(SIEBEN, standard_cent=8000)
        assert set(summen) == set(g.MODI)
        assert summen[g.SAMMEL] == summen[g.PRO_TITEL] == 6000
        assert summen[g.PRO_EINTRAG] == 6720


class TestKulanzUndPauschale:
    def test_kulanz_wird_nie_mit_berechneter_zeit_verschmolzen(self):
        eintraege = [
            zeit(1, "2026-03-02", "Beratung", 60),
            zeit(2, "2026-03-02", "Beratung", 60, "KULANZ",
                 grund="Einarbeitung", satz_cent_snapshot=8000),
        ]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert len(positionen) == 2
        berechnet, kulanz = positionen
        assert berechnet.betrag_cent == 8000
        assert kulanz.betrag_cent == 0
        assert kulanz.nachlass_cent == 8000, "Der Wert bleibt sichtbar"
        assert kulanz.nachlass_grund == "Einarbeitung"
        assert kulanz.ist_kulanz

    def test_kulanzzeile_hat_immer_einen_text(self):
        """Sonst steht auf dem PDF eine nackte Null und sieht wie ein Fehler aus."""
        eintraege = [zeit(1, "2026-03-02", "X", 60, "KULANZ", satz_cent_snapshot=8000)]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert positionen[0].nachlass_grund == "ohne Berechnung"

    def test_pauschale_kommt_als_eigene_position_ans_ende(self):
        eintraege = [
            zeit(1, "2026-03-05", "Paket", 300, "PAUSCHAL", pauschal_cent=50000),
            zeit(2, "2026-03-02", "Beratung", 60),
        ]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert [p.quelle for p in positionen] == ["ZEIT", "PAUSCHAL"]
        assert positionen[1].betrag_cent == 50000
        assert positionen[1].einheit == "LS"

    def test_intern_gehoert_nicht_auf_die_rechnung(self):
        eintraege = [
            zeit(1, "2026-03-02", "Beratung", 60),
            zeit(2, "2026-03-02", "Akquise", 120, "INTERN", grund="Akquise"),
        ]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert len(positionen) == 1
        assert positionen[0].betrag_cent == 8000

    def test_satz_snapshot_schlaegt_den_standard(self):
        eintraege = [zeit(1, "2026-03-02", "X", 60, satz_cent_snapshot=12000)]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert positionen[0].einzelpreis_cent == 12000


class TestEinzelnachweis:
    def test_nachweis_kommt_aus_den_rohdaten(self):
        """Trennt Rechnungsklarheit von Nachweispflicht: niemand muss
        'pro Eintrag' waehlen, nur weil der Kunde Details sehen will."""
        zeilen = g.einzelnachweis(SIEBEN)
        assert len(zeilen) == 7
        assert all(z["minuten"] == 7 for z in zeilen), "Ungerundet"
        assert zeilen[0]["datum"] == "02.03.2026"

    def test_interne_zeit_steht_nicht_im_nachweis(self):
        zeilen = g.einzelnachweis([zeit(1, "2026-03-02", "A", 60, "INTERN", grund="x")])
        assert zeilen == []

    def test_kulanz_ist_im_nachweis_markiert(self):
        zeilen = g.einzelnachweis(
            [zeit(1, "2026-03-02", "A", 60, "KULANZ", grund="x", satz_cent_snapshot=8000)]
        )
        assert zeilen[0]["kulanz"] is True


class TestLeerUndUntertext:
    def test_keine_eintraege_ergeben_keine_positionen(self):
        assert g.erzeuge_positionen([], modus=g.PRO_TITEL) == []

    def test_untertext_fasst_beschreibungen_zusammen_und_entdoppelt(self):
        eintraege = [
            zeit(1, "2026-03-02", "A", 60, beschreibung="Konzept"),
            zeit(2, "2026-03-03", "A", 60, beschreibung="Konzept"),
            zeit(3, "2026-03-04", "A", 60, beschreibung="Abstimmung"),
        ]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert positionen[0].untertext == "Konzept; Abstimmung"

    def test_langer_untertext_wird_gekuerzt(self):
        eintraege = [
            zeit(i, "2026-03-02", "A", 60, beschreibung=f"Beschreibung Nummer {i} " + "x" * 40)
            for i in range(1, 12)
        ]
        positionen = g.erzeuge_positionen(eintraege, modus=g.PRO_TITEL, standard_cent=8000)
        assert len(positionen[0].untertext) <= g.UNTERTEXT_MAX
        assert positionen[0].untertext.endswith("…")

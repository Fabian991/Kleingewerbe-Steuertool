"""Steuerrücklage: Tarif, Zuschläge, Gewerbesteuer und die Schätzung der Nachzahlung.

Die Referenzwerte sind Werte des amtlichen BMF-Steuerrechners und der
BMF-Lohnsteuerschnittstelle (abgefragt am 25.10.2026, Gegenprüfung mit einer
zweiten, unabhängigen Implementierung). Sie werden **auf den Euro genau**
verglichen: eine Schätzung darf grob sein, der Tarif darunter nicht -- sonst
weiß man nicht mehr, ob eine Abweichung an den Annahmen liegt oder an einem
Rechenfehler.
"""

import math
from fractions import Fraction

import pytest

from app.logik import steuertarif as st

# --------------------------------------------------------------- Referenzwerte

#: zvE -> Einkommensteuer nach Grundtarif (BMF-ESt-Rechner).
REF_GRUND = {
    2024: {10000: 0, 12500: 105, 15000: 548, 17005: 991, 17006: 991, 25000: 3023, 33333: 5388,
           45000: 9121, 66760: 17402, 66761: 17403, 70000: 18763, 80000: 22963, 120000: 39763,
           277825: 106050, 277826: 106050, 300000: 116028},
    2025: {10000: 0, 12500: 58, 15000: 485, 17443: 1015, 17444: 1015, 25000: 2927, 33333: 5269,
           45000: 8961, 68480: 17849, 68481: 17850, 70000: 18488, 80000: 22688, 120000: 39488,
           277825: 105774, 277826: 105775, 300000: 115753},
    2026: {10000: 0, 12349: 0, 12500: 21, 15000: 435, 17799: 1034, 17800: 1035, 25000: 2850,
           33333: 5176, 37000: 6275, 37839: 6533, 45000: 8835, 60000: 14233, 69878: 18213,
           69879: 18213, 70000: 18264, 75000: 20364, 80000: 22464, 100000: 30864, 120000: 39264,
           150000: 51864, 277825: 105550, 277826: 105551, 300000: 115529, 600000: 250529},
}

#: zvE -> Einkommensteuer nach Splittingtarif (BMF-ESt-Rechner).
REF_SPLITTING = {
    2024: {50000: 6046, 100000: 21744},
    2025: {50000: 5854, 77777: 13934, 100000: 21382},
    2026: {33333: 1550, 50000: 5700, 50001: 5700, 70001: 11340, 77777: 13718, 100000: 21096,
           100001: 21096, 160000: 44928, 160001: 44928, 600000: 231058},
}

#: (Jahr, zusammen, Einkommensteuer in Euro) -> Soli in Cent (BMF-ESt-Rechner).
REF_SOLI = {
    (2024, False, 18763): 7532, (2024, False, 22963): 57512, (2024, False, 39763): 218696,
    (2024, False, 106050): 583275, (2025, False, 22688): 32582, (2025, False, 39488): 217184,
    (2026, False, 18264): 0, (2026, False, 20364): 166, (2026, False, 22464): 25156,
    (2026, False, 30864): 125116, (2026, False, 39264): 215952, (2026, False, 51864): 285252,
    (2026, False, 105550): 580525, (2026, False, 115529): 635409, (2026, False, 250529): 1377909,
    (2026, True, 44928): 50313, (2026, True, 231058): 1270819,
}

#: Grenzsteuersatz, auf zwei Nachkommastellen abgeschnitten wie beim BMF.
REF_GRENZSATZ = {(2026, 15000): "18,85", (2026, 25000): "26,46", (2026, 45000): "33,38",
                 (2026, 17799): "23,96", (2026, 17800): "23,97", (2026, 69878): "41,99",
                 (2026, 69879): "42,00", (2026, 277826): "45,00", (2026, 12349): "14,00",
                 (2026, 12500): "14,27", (2026, 33333): "29,34", (2024, 25000): "26,86",
                 (2025, 33333): "29,58", (2024, 12500): "15,36"}

#: Bruttolohn -> (zvE, ESt), kinderlos, Zusatzbeitrag im Durchschnitt (zweifach gerechnet).
REF_ZVE = {
    2025: {12000: (8148, 0), 15000: (10501, 0), 20000: (14752, 437), 30000: (22761, 2339),
           40000: (30770, 4523), 50000: (38780, 6933), 60000: (46789, 9570),
           70000: (55206, 12585), 80000: (64276, 16115), 100000: (82733, 23835),
           120000: (102733, 32235)},
    2026: {12000: (8124, 0), 15000: (10471, 0), 20000: (14714, 382), 30000: (22704, 2252),
           40000: (30694, 4413), 50000: (38684, 6796), 60000: (46674, 9399),
           70000: (54691, 12233), 80000: (63761, 15708), 100000: (81901, 23262),
           120000: (101770, 31607)},
}

#: Jahreslohnsteuer Steuerklasse I, kinderlos, ZB 2,9 % (BMF-Schnittstelle LSt2026ext).
REF_LST_2026 = {20000: 380, 30000: 2248, 40000: 4407, 50000: 6788, 60000: 9389, 80000: 15694,
                100000: 23248, 120000: 31593}


# ------------------------------------------------------------- § 32a EStG

class TestTarif:
    @pytest.mark.parametrize("jahr,zve,erwartet", [
        (jahr, zve, est) for jahr, werte in REF_GRUND.items() for zve, est in werte.items()
    ])
    def test_grundtarif_wie_der_bmf_rechner(self, jahr, zve, erwartet):
        """Jeder Referenzwert des BMF-Rechners, auf den Euro genau."""
        assert st.tarif(zve, jahr) == erwartet

    @pytest.mark.parametrize("jahr,zve,erwartet", [
        (jahr, zve, est) for jahr, werte in REF_SPLITTING.items() for zve, est in werte.items()
    ])
    def test_splitting_wie_der_bmf_rechner(self, jahr, zve, erwartet):
        """Splitting: Hälfte abrunden, Steuer abrunden, verdoppeln."""
        assert st.splitting(zve, jahr) == erwartet

    def test_splitting_rundet_die_haelfte_vor_der_steuer(self):
        """50.001 € kosten so viel wie 50.000 € -- erst die Hälfte abrunden.

        Wer 25.000,50 € in die Formel gibt und erst danach rundet, liegt bei
        manchen Beträgen einen Euro daneben; das BMF rundet die Hälfte zuerst.
        """
        t = st.tarif_fuer(2026)
        for zve in (50001, 70001, 100001, 160001):
            assert t.splitting(zve) == t.splitting(zve - 1)
            assert t.splitting(zve) % 2 == 0

    @pytest.mark.parametrize("jahr", [2024, 2025, 2026])
    def test_zonengrenzen_und_grundfreibetrag(self, jahr):
        """An jeder Zonengrenze springt die Steuer um höchstens einen Euro.

        Der Tarif ist stetig; ein Tippfehler in einem Koeffizienten zeigt sich
        als Sprung genau an einer Grenze -- dort, wo die Referenzwerte selten
        liegen.
        """
        t = st.tarif_fuer(jahr)
        assert t.steuer(t.grundfreibetrag) == 0
        assert t.steuer(t.grundfreibetrag + 1) == 0  # 14 % von einem Euro, abgerundet
        for zone in t.zonen[:-1]:
            assert 0 <= t.steuer(zone.bis + 1) - t.steuer(zone.bis) <= 1, zone

    @pytest.mark.parametrize("jahr", [2024, 2025, 2026])
    def test_steuer_steigt_nie_mit_dem_einkommen_ab(self, jahr):
        """Monotonie in 13-€-Schritten bis 320.000 €."""
        t = st.tarif_fuer(jahr)
        vorher = -1
        for zve in range(0, 320001, 13):
            steuer = t.steuer(zve)
            assert steuer >= vorher, zve
            vorher = steuer

    def test_zve_wird_abgerundet_und_negatives_kostet_nichts(self):
        """§ 32a Abs. 1: das zvE auf volle Euro abrunden; ein Verlust ist steuerfrei."""
        assert st.tarif(Fraction(4500099, 100), 2026) == st.tarif(45000, 2026)
        assert st.tarif(-20000, 2026) == 0

    @pytest.mark.parametrize("jahr,zve,erwartet", [
        (jahr, zve, satz) for (jahr, zve), satz in REF_GRENZSATZ.items()
    ])
    def test_grenzsteuersatz_wie_der_bmf_rechner(self, jahr, zve, erwartet):
        """Der Grenzsteuersatz, auf zwei Stellen abgeschnitten wie beim BMF."""
        assert st.abgeschnitten(st.grenzsteuersatz(zve, jahr)) == erwartet

    def test_grenzsteuersatz_beim_splitting_an_der_haelfte(self):
        """Bei Zusammenveranlagung gilt der Satz der Hälfte des gemeinsamen zvE."""
        assert st.grenzsteuersatz(90000, 2026, zusammen=True) == st.grenzsteuersatz(45000, 2026)

    def test_stufentabelle_2026(self):
        """Die Abstufungen für die Seite: von–bis, Satzspanne, verständlicher Name."""
        stufen = st.tarif_fuer(2026).stufen()
        assert [(s.von_euro, s.bis_euro) for s in stufen] == [
            (0, 12348), (12349, 17799), (17800, 69878), (69879, 277825), (277826, None)]
        assert [(st.prozent_ganz(s.satz_von), st.prozent_ganz(s.satz_bis)) for s in stufen] == [
            (0, 0), (14, 24), (24, 42), (42, 42), (45, 45)]
        assert [s.name for s in stufen] == [
            "Grundfreibetrag", "Eingangszone", "Progressionszone", "Spitzensteuersatz",
            "„Reichensteuer“"]

    def test_stufentabelle_beim_splitting_doppelt(self):
        """Splitting verdoppelt die Grenzen: die Hälfte 12.348 € heißt gemeinsam 24.697 €."""
        stufen = st.tarif_fuer(2026).stufen(faktor=2)
        assert [(s.von_euro, s.bis_euro) for s in stufen][:2] == [(0, 24697), (24698, 35599)]
        t = st.tarif_fuer(2026)
        assert t.splitting(24697) == 0 and t.splitting(24698) == 0
        assert t.splitting(24800) == 14  # Hälfte 12.400 €: 7 € Steuer, verdoppelt


class TestTarifjahre:
    def test_2027_gilt_der_tarif_2026_fort(self):
        """Für 2027 ist nichts verkündet: § 32a gilt „ab 2026“ weiter -- und die Seite sagt es."""
        t = st.tarif_fuer(2027)
        assert (t.status, t.aus_jahr) == ("fortgeltung", 2026)
        assert st.tarif(45000, 2027) == st.tarif(45000, 2026) == 8835
        assert "kein neuer Tarif verkündet" in t.hinweis and "2026" in t.hinweis

    def test_spaeteres_jahr_ohne_tarif_nimmt_das_neueste_und_sagt_es(self):
        """Ersatzjahr: „Tarif 2026 verwendet — für 2029 noch nicht hinterlegt“."""
        t = st.tarif_fuer(2029)
        assert (t.status, t.aus_jahr) == ("ersatz", 2026)
        assert t.hinweis == "Tarif 2026 verwendet — für 2029 noch nicht hinterlegt."

    def test_frueheres_jahr_warnt_deutlicher(self):
        """Vor 2024 waren die Werte andere: rückwärts angewendet ist das nur grob."""
        t = st.tarif_fuer(2022)
        assert t.aus_jahr == 2024
        assert "nur grob" in t.hinweis

    def test_entwurf_2027_nur_auf_wunsch_und_gekennzeichnet(self):
        """Der Regierungsentwurf ist kein Recht: nur zuschaltbar, mit Hinweis."""
        assert st.entwurf_verfuegbar(2027) and not st.entwurf_verfuegbar(2026)
        t = st.tarif_fuer(2027, entwurf=True)
        assert t.entwurf and "Entwurf, vorläufig" in t.hinweis and "21/8235" in t.hinweis
        werte = {10000: 0, 15000: 397, 25000: 2808, 45000: 8777, 70000: 18159, 120000: 39158,
                 300000: 116658}
        assert {z: t.steuer(z) for z in werte} == werte
        assert st.splitting(50000, 2027, entwurf=True) == 5616
        # 47 % ab 280.000 € gibt es nur im Entwurf.
        assert t.grenzsatz(280000) == Fraction(47, 100)
        assert st.tarif_fuer(2027).grenzsatz(280000) == Fraction(45, 100)

    def test_entwurf_2028(self):
        """Der Entwurf für 2028 aus derselben Drucksache."""
        t = st.tarif_fuer(2028, entwurf=True)
        assert [t.steuer(z) for z in (15000, 45000, 120000, 300000)] == [338, 8713, 39094, 116594]
        assert t.splitting(100000) == 20838

    def test_entwurfsschalter_ohne_entwurf_wirkt_nicht(self):
        """Für 2026 gibt es keinen Entwurf: der Schalter lässt das geltende Recht stehen."""
        assert st.tarif_fuer(2026, entwurf=True).status == "gesetz"

    def test_jede_tarifzone_hat_eine_quelle(self):
        """Ohne Fundstelle ist ein Koeffizient eine Behauptung."""
        for jahr in (2024, 2025, 2026, 2027):
            assert st.tarif_fuer(jahr).quelle
        assert "BGBl." in st.tarif_fuer(2026).quelle


# ---------------------------------------------------------- Soli, Kirche

class TestSoli:
    @pytest.mark.parametrize("jahr,zusammen,est,erwartet", [
        (j, z, e, s) for (j, z, e), s in REF_SOLI.items()
    ])
    def test_soli_wie_der_bmf_rechner(self, jahr, zusammen, est, erwartet):
        """Freigrenze, Milderungszone und voller Satz -- auf den Cent."""
        assert st.soli(est * 100, jahr, zusammen=zusammen) == erwartet

    def test_freigrenze_ist_kein_freibetrag(self):
        """Bis zur Freigrenze 0; einen Euro darüber nur 11,9 Cent, nicht 5,5 % von allem."""
        assert st.soli(2035000, 2026) == 0
        assert st.soli(2035100, 2026) == 11
        assert st.soli(4070000, 2026, zusammen=True) == 0

    def test_ende_der_milderungszone(self):
        """Ab 37.838,28 € ESt (2026) greift der volle Satz von 5,5 %."""
        assert st.soli(3783900, 2026) == 208114  # 5,5 % von 37.839 €
        assert st.soli(3700000, 2026) < 203500  # noch gemildert

    def test_freigrenzen_je_jahr(self):
        """2024 18.130 €, 2025 19.950 €, 2026 und fortgeltend 2027 20.350 €."""
        assert [st.soli_freigrenze(j).einzel_euro for j in (2024, 2025, 2026, 2027)] == [
            18130, 19950, 20350, 20350]
        assert st.soli_freigrenze(2027).status == "fortgeltung"
        assert "2027" in st.soli_freigrenze(2031).hinweis

    def test_kirchensteuer_auf_die_einkommensteuer(self):
        """8 % in Bayern und Baden-Württemberg, sonst 9 % -- auf die ESt vor § 35."""
        assert st.kirchensteuer(1423300, 9) == 128097
        assert st.kirchensteuer(1423300, 8) == 113864
        assert st.kirchensteuer(1423300, 0) == 0
        assert st.kirchensteuer(-5000, 9) == 0
        assert st.kirchensteuer_saetze() == (8, 9)


# ------------------------------------------------------ Härteausgleich

class TestHaerteausgleich:
    @pytest.mark.parametrize("gewinn,abzug", [
        (300, 300), (410, 410), (411, 409), (600, 220), (819, 1), (820, 0), (1000, 0), (-50, 0),
        (0, 0),
    ])
    def test_abzug(self, gewinn, abzug):
        """Bis 410 € voll abgezogen, bis 820 € gleitend, darüber nichts."""
        assert st.haerteausgleich(gewinn * 100) == abzug * 100

    def test_steuerpflichtiger_teil_steigt_stetig(self):
        """Gewinn minus Abzug: 0 bis 410 €, dann doppelt so schnell bis 820 €."""
        pflichtig = [g - st.haerteausgleich(g * 100) // 100 for g in (410, 411, 615, 819, 820)]
        assert pflichtig == [0, 2, 410, 818, 820]


# --------------------------------------------------------- Brutto -> zvE

class TestZveAusBrutto:
    @pytest.mark.parametrize("jahr,brutto,zve,est", [
        (j, b, z, e) for j, werte in REF_ZVE.items() for b, (z, e) in werte.items()
    ])
    def test_naeherung(self, jahr, brutto, zve, est):
        """zvE und ESt aus dem Bruttolohn, wie in der Gegenprüfung."""
        schaetzung = st.zve_aus_brutto(brutto * 100, jahr)
        assert schaetzung.zve_euro == zve
        assert st.tarif(schaetzung.zve_euro, jahr) == est

    @pytest.mark.parametrize("brutto,lohnsteuer", sorted(REF_LST_2026.items()))
    def test_liegt_knapp_ueber_der_lohnsteuer(self, brutto, lohnsteuer):
        """Die Schätzung trifft die einbehaltene Lohnsteuer (Klasse I) bis auf 0–14 €.

        Darauf beruht die ganze Rücklage: die Steuer auf das Gehalt ist durch
        den Lohnsteuerabzug schon bezahlt, offen ist nur der Zuwachs.
        """
        est = st.tarif(st.zve_aus_brutto(brutto * 100, 2026).zve_euro, 2026)
        assert 0 <= est - lohnsteuer <= 14

    def test_zerlegung_50000(self):
        """50.000 € (2026): RV 4.650 €, sonstige Vorsorge 5.400 € (ALV verdrängt)."""
        s = st.zve_aus_brutto(5000000, 2026)
        assert (s.werbungskosten_cent, s.rentenversicherung_cent, s.sonstige_vorsorge_cent,
                s.sonderausgaben_cent) == (123000, 465000, 540000, 3600)
        assert s.einkuenfte_cent == 4877000

    def test_geringes_gehalt_mit_hoechstbetrag(self):
        """15.000 € (2026): Basis 1.620 € < 1.900 €, ALV und 4 % passen noch hinein."""
        assert st.zve_aus_brutto(1500000, 2026).sonstige_vorsorge_cent == 186750

    def test_kein_gehalt(self):
        """Ohne Lohn bleibt der Sonderausgaben-Pauschbetrag: −36 €, er mindert den Gewinn."""
        assert st.zve_aus_brutto(0, 2026).zve_euro == -36

    def test_zusammenveranlagung_addiert_beide(self):
        """Zwei Löhne: beide Vorsorgeanteile, der Sonderausgaben-Pauschbetrag 72 €."""
        einzeln = [st.zve_aus_brutto(b, 2026) for b in (5000000, 3000000)]
        gemeinsam = st.zve_aus_brutto(5000000, 2026, partner_brutto_cent=3000000, zusammen=True)
        assert gemeinsam.zve_euro == sum(s.zve_euro for s in einzeln) == 61388
        nur_einer = st.zve_aus_brutto(5000000, 2026, zusammen=True)
        assert nur_einer.zve_euro == 38684 - 36

    def test_jahr_ohne_sozialversicherungswerte(self):
        """2027 rechnet mit den Werten 2026 und sagt das; 2024 mit denen von 2025."""
        s = st.zve_aus_brutto(5000000, 2027)
        assert s.aus_jahr == 2026 and s.zve_euro == 38684 and "2026" in s.hinweis
        assert st.zve_aus_brutto(5000000, 2024).aus_jahr == 2025
        assert st.zve_aus_brutto(5000000, 2026).hinweis == ""


# --------------------------------------------- Gewerbesteuer und § 35

class TestGewerbesteuer:
    @pytest.mark.parametrize("hebesatz,steuer,anrechnung,rest", [
        (280, 347900, 347900, 0), (400, 497000, 497000, 0), (410, 509425, 497000, 12425),
        (500, 621250, 497000, 124250),
    ])
    def test_60000_euro_gewinn(self, hebesatz, steuer, anrechnung, rest):
        """Messbetrag 1.242,50 €; bis 400 % voll angerechnet, darüber bleibt ein Rest."""
        g = st.gewerbesteuer(6000000, hebesatz)
        assert g.messbetrag_cent == 124250
        a35 = st.ermaessigung_35(g, 1423300, Fraction(1))
        assert (g.steuer_cent, a35, g.steuer_cent - a35) == (steuer, anrechnung, rest)

    def test_freibetrag_und_abrundung(self):
        """Erst auf volle 100 € abrunden, dann 24.500 € Freibetrag abziehen."""
        assert st.gewerbesteuer(2459999, 400).messbetrag_cent == 0
        assert st.gewerbesteuer(2460000, 400).messbetrag_cent == 350
        assert st.gewerbesteuer(6009900, 400).messbetrag_cent == 124250
        assert st.gewerbesteuer(-1000000, 400).steuer_cent == 0

    def test_anrechnung_hoechstens_anteilige_einkommensteuer(self):
        """Ermäßigungshöchstbetrag: die ESt, soweit sie auf das Gewerbe entfällt."""
        g = st.gewerbesteuer(4000000, 400)
        est90 = st.tarif(90000, 2026) * 100
        assert st.ermaessigung_35(g, est90, Fraction(40000, 90000)) == 217000
        assert st.ermaessigung_35(g, 100000, Fraction(1, 2)) == 50000
        assert st.ermaessigung_35(g, 0, Fraction(1)) == 0

    def test_mindesthebesatz(self):
        """200 % bis 2026, 280 % ab 2027 (BGBl. 2026 I Nr. 197)."""
        assert [st.mindesthebesatz(j) for j in (2024, 2026, 2027, 2028)] == [200, 200, 280, 280]


# ================================================================ Schätzung

from datetime import date  # noqa: E402

from app import db  # noqa: E402
from app.logik import steuerschaetzung as ss  # noqa: E402

HEUTE = date(2026, 10, 25)


def _schaetze(*, gewinn, einnahmen=0, heute=HEUTE, **eingaben):
    """Eine Schätzung mit Vorgaben: 2026, einzeln, Hebesatz 410 %."""
    werte = {"jahr": 2026, "hebesatz_prozent": 410} | eingaben
    return ss.schaetze(ss.Eingaben(**werte), gewinn_bisher_cent=gewinn,
                       einnahmen_bisher_cent=einnahmen, heute=heute)


class TestKernrechnung:
    @pytest.mark.parametrize("brutto,gewinn,nachzahlung", [
        (40000, 5000, 1471), (40000, 10000, 3023), (40000, 20000, 6385),
        (60000, 5000, 1752), (60000, 10000, 3580), (60000, 20000, 7496),
    ])
    def test_nachzahlung_neben_gehalt(self, brutto, gewinn, nachzahlung):
        """ESt mit Gewerbe minus einbehaltene Lohnsteuer -- wie in der Gegenprüfung.

        Die Rücklage rechnet mit ESt(zvE + Gewinn) − ESt(zvE); die Lohnsteuer
        liegt nur wenige Euro unter ESt(zvE). Beides zusammen ergibt die
        tatsächliche Nachzahlung auf den Euro.
        """
        r = _schaetze(brutto_lohn_cent=brutto * 100, gewinn=gewinn * 100)
        assert r.est_mit_cent // 100 - REF_LST_2026[brutto] == nachzahlung
        assert r.mehr_est_cent == r.est_mit_cent - r.est_ohne_cent
        assert 0 <= nachzahlung * 100 - r.mehr_est_cent <= 1400

    def test_beispiel_mit_allen_bausteinen(self):
        """50.000 € Gehalt, 30.000 € Gewinn, 9 % Kirchensteuer, Hebesatz 410 %."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=3000000, kirchensteuer_prozent=9)
        assert (r.zve_ohne_euro, r.zve_mit_euro) == (38684, 68684)
        assert (r.est_ohne_cent, r.est_mit_cent) == (679600, 100 * st.tarif(68684, 2026))
        assert r.est_mit_cent == 1771400
        # Gewerbesteuer: (30.000 − 24.500) × 3,5 % = 192,50 € Messbetrag.
        assert r.gewerbe.messbetrag_cent == 19250
        assert r.gewst_cent == 78925
        assert r.anrechnung_35_cent == 77000  # 4,0 × Messbetrag
        assert r.mehr_est_cent == 1771400 - 77000 - 679600
        assert r.mehr_soli_cent == 0  # 17.714 € − 770 € liegt unter der Freigrenze
        assert r.mehr_kist_cent == st.kirchensteuer(1771400, 9) - st.kirchensteuer(679600, 9)
        assert r.mehrsteuer_berechnet_cent == (r.mehr_est_cent + r.mehr_kist_cent + 78925)
        assert r.ruecklage_cent == ss.aufrunden(r.mehrsteuer_berechnet_cent)

    def test_soli_auf_die_differenz(self):
        """Soli(mit) − Soli(ohne), nicht 5,5 % der Mehr-ESt.

        Das Gehalt allein bleibt unter der Freigrenze, mit dem Gewinn liegt die
        ESt in der Milderungszone: dort kostet jeder Euro Mehr-ESt 11,9 Cent Soli
        -- mehr als die 5,5 %, mit denen man überschlägig rechnen würde.
        """
        r = _schaetze(brutto_lohn_cent=8000000, gewinn=4000000, hebesatz_prozent=400)
        est_nach_35 = r.est_mit_cent - r.anrechnung_35_cent
        assert r.soli_mit_cent == st.soli(est_nach_35, 2026)
        assert r.soli_ohne_cent == st.soli(r.est_ohne_cent, 2026) == 0
        assert r.mehr_est_cent * 55 // 1000 < r.mehr_soli_cent <= r.mehr_est_cent * 119 // 1000

    def test_kirchensteuer_vor_der_anrechnung(self):
        """§ 51a Abs. 2 Satz 3 EStG: die Kirchensteuer sieht die § 35-Anrechnung nicht."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=6000000, kirchensteuer_prozent=8)
        assert r.anrechnung_35_cent > 0
        assert r.kist_mit_cent == st.kirchensteuer(r.est_mit_cent, 8)

    def test_zusammenveranlagung_mit_splitting(self):
        """Gemeinsames zvE, Splittingtarif, doppelte Soli-Freigrenze."""
        r = _schaetze(brutto_lohn_cent=5000000, partner_brutto_cent=3000000,
                      veranlagung="ZUSAMMEN", gewinn=2000000)
        assert r.zve_ohne_euro == 61388
        assert r.est_ohne_cent == 100 * st.splitting(61388, 2026)
        assert r.est_mit_cent == 100 * st.splitting(81388, 2026)
        assert r.mehr_est_cent < _schaetze(brutto_lohn_cent=5000000,
                                           gewinn=2000000).mehr_est_cent
        assert [s.bis_euro for s in r.stufen][0] == 24697

    def test_zve_aus_dem_bescheid_geht_vor(self):
        """Ist das zvE eingetragen, gilt es -- das Gehalt dient nur dem Vergleich."""
        r = _schaetze(brutto_lohn_cent=5000000, zve_ohne_cent=3500000, gewinn=1000000)
        assert (r.zve_quelle, r.zve_ohne_euro, r.zve_mit_euro) == ("ZVE", 35000, 45000)
        assert r.lohn.zve_euro == 38684

    def test_verlust_spart_steuer(self):
        """Ein Verlust mindert das zvE: negative Mehrsteuer, keine Rücklage, Erstattung."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=-500000, vorauszahlungen_cent=0)
        assert r.zve_mit_euro == 38684 - 5000
        assert r.mehrsteuer_cent < 0
        assert r.ruecklage_cent == 0 and r.offen_cent == 0
        assert r.zurueck_cent == -r.mehrsteuer_cent
        assert r.gewst_cent == 0 and r.haerteausgleich_cent == 0
        assert r.satz_berechnet is None

    def test_grenzsteuersatz_beim_zve_mit_gewerbe(self):
        """Der Grenzsatz gilt am oberen Ende: dort, wo der Gewinn hinzukommt."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=1000000)
        assert r.grenzsatz == st.grenzsteuersatz(48684, 2026)
        assert r.zone_ohne == r.zone_mit == 2

    def test_zone_wechselt_mit_dem_gewinn(self):
        """Ein großer Gewinn hebt das Einkommen in die 42-%-Stufe -- die Tabelle zeigt beides."""
        r = _schaetze(brutto_lohn_cent=6000000, gewinn=3000000)
        assert (r.zone_ohne, r.zone_mit) == (2, 3)


class TestHaerteausgleichInDerSchaetzung:
    def test_bis_410_euro_keine_steuer(self):
        """Neben Arbeitslohn bleiben 410 € Gewinn steuerfrei (§ 46 Abs. 3 EStG)."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=41000)
        assert r.haerteausgleich_cent == 41000
        assert r.zve_mit_euro == r.zve_ohne_euro
        assert r.mehrsteuer_cent == 0 and r.ruecklage_cent == 0

    def test_uebergang_bis_820_euro(self):
        """600 € Gewinn: 220 € Abzug, 380 € zu versteuern (§ 70 EStDV)."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=60000)
        assert r.haerteausgleich_cent == 22000
        assert r.zve_mit_euro == r.zve_ohne_euro + 380

    def test_ab_820_euro_voll(self):
        """Ab 820 € Nebeneinkünften entfällt der Härteausgleich (§ 70 EStDV)."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=82000)
        assert r.haerteausgleich_cent == 0
        assert r.zve_mit_euro == r.zve_ohne_euro + 820

    def test_ohne_arbeitslohn_kein_haerteausgleich(self):
        """Ohne Lohn (Gehalt 0, zvE aus anderen Einkünften) gilt § 46 Abs. 3 nicht."""
        r = _schaetze(brutto_lohn_cent=0, zve_ohne_cent=5000000, gewinn=30000)
        assert not r.arbeitslohn
        assert r.haerteausgleich_cent == 0
        assert r.zve_mit_euro == 50300

    def test_pflichtveranlagung_ab_410_euro(self):
        """Über 410 € Nebeneinkünfte muss eine Erklärung abgegeben werden."""
        assert not _schaetze(brutto_lohn_cent=5000000, gewinn=41000).pflichtveranlagung
        assert _schaetze(brutto_lohn_cent=5000000, gewinn=41100).pflichtveranlagung


class TestGewerbesteuerInDerSchaetzung:
    def test_unter_dem_freibetrag_keine_gewerbesteuer(self):
        """Bis 24.599 € Gewinn kein Messbetrag -- und keine Anrechnung."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=2459900)
        assert r.gewst_cent == 0 and r.anrechnung_35_cent == 0

    def test_hebesatz_400_wird_voll_angerechnet(self):
        """Bis 400 % verschwindet die Gewerbesteuer fast ganz in der ESt-Anrechnung."""
        r = _schaetze(brutto_lohn_cent=5000000, gewinn=6000000, hebesatz_prozent=400)
        assert r.gewst_cent == 497000 == r.anrechnung_35_cent
        r410 = _schaetze(brutto_lohn_cent=5000000, gewinn=6000000, hebesatz_prozent=410)
        assert r410.gewst_cent - r410.anrechnung_35_cent == 12425
        # Der Rest von 124,25 € kommt obendrauf; die Anrechnung mindert auch den Soli.
        assert r410.mehrsteuer_berechnet_cent > r.mehrsteuer_berechnet_cent


class TestSatzUndRuecklage:
    def test_berechneter_satz_ist_mehrsteuer_durch_gewinn(self):
        """Der Satz auf den Gewinn ist ein Durchschnitt -- der Grenzsatz steht daneben."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=1000000)
        assert r.satz_berechnet == Fraction(r.mehrsteuer_berechnet_cent, 1000000)
        assert r.satz == r.satz_berechnet
        assert r.satz_berechnet < r.grenzsatz

    def test_eigener_satz_ist_der_gesamtsatz(self):
        """Mit eigenem Satz: Gewinn × Satz, ohne Soli oder Kirchensteuer obendrauf."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=2000000, satz_manuell_promille=300,
                      kirchensteuer_prozent=9)
        assert r.mit_eigenem_satz
        assert r.mehrsteuer_cent == 600000
        assert r.ruecklage_cent == 600000
        assert r.mehrsteuer_berechnet_cent != 600000  # der berechnete steht daneben

    def test_eigener_satz_null(self):
        """0 % ist eine Angabe, kein leeres Feld."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=2000000, satz_manuell_promille=0)
        assert r.mehrsteuer_cent == 0 and r.ruecklage_cent == 0

    def test_ruecklage_auf_10_euro_aufgerundet(self):
        """Die Rücklage ist eine Schätzung: volle 10 €, immer aufgerundet."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=1000001, satz_manuell_promille=333)
        assert r.mehrsteuer_cent == 333000  # 10.000,01 € × 33,3 % = 3.330,0033 €
        assert r.ruecklage_cent == 333000
        r2 = _schaetze(brutto_lohn_cent=4000000, gewinn=1000100, satz_manuell_promille=333)
        assert r2.mehrsteuer_cent == 333033 and r2.ruecklage_cent == 334000

    def test_vorauszahlungen_mindern_das_offene(self):
        """Bereits gezahlte Vorauszahlungen gehen ab; darüber hinaus gibt es Geld zurück."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=2000000, satz_manuell_promille=300,
                      vorauszahlungen_cent=250000)
        assert (r.ruecklage_cent, r.offen_cent, r.zurueck_cent) == (600000, 350000, 0)
        r2 = _schaetze(brutto_lohn_cent=4000000, gewinn=2000000, satz_manuell_promille=300,
                       vorauszahlungen_cent=700000)
        assert (r2.offen_cent, r2.zurueck_cent) == (0, 100000)

    def test_faustregel_cent_je_euro(self):
        """30 % Steuer auf den Gewinn, Gewinn = Hälfte der Einnahmen: 15 Cent je Euro."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=2000000, einnahmen=4000000,
                      satz_manuell_promille=300)
        assert r.cent_je_euro == 15
        assert _schaetze(brutto_lohn_cent=4000000, gewinn=2000000,
                         satz_manuell_promille=300).cent_je_euro is None

    def test_erwarteter_gewinn_ueberschreibt_die_euer(self):
        """Der erwartete Jahresgewinn geht vor; die Faustregel nimmt die bisherige Marge."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=1000000, einnahmen=2000000,
                      gewinn_erwartet_cent=3000000)
        assert r.gewinn_cent == 3000000 and not r.gewinn_aus_euer
        assert r.gewinn_bisher_cent == 1000000
        assert r.cent_je_euro == math.ceil(r.satz_berechnet * Fraction(1, 2) * 100)

    def test_vorauszahlungen_ab_400_euro_wahrscheinlich(self):
        """§ 37 Abs. 5 EStG: unter 400 € im Jahr setzt das Finanzamt keine fest."""
        assert _schaetze(brutto_lohn_cent=4000000, gewinn=2000000).vorauszahlungen_wahrscheinlich
        assert not _schaetze(brutto_lohn_cent=4000000,
                             gewinn=100000).vorauszahlungen_wahrscheinlich


class TestHochrechnungUndJahre:
    def test_hochrechnung_nur_im_laufenden_jahr(self):
        """7.500 € bis Oktober (10 Monate) -- gleichmäßig weiter wären es 9.000 €."""
        r = _schaetze(brutto_lohn_cent=4000000, gewinn=750000)
        assert r.hochrechnung.jahr_cent == 900000
        # ... aber nur als Hinweis: gerechnet wird mit dem bisherigen Gewinn.
        assert r.gewinn_cent == 750000
        vorjahr = _schaetze(brutto_lohn_cent=4000000, gewinn=750000, jahr=2025)
        assert vorjahr.hochrechnung is None

    def test_2027_nennt_den_fortgeltenden_tarif(self):
        """Ersatzjahr-Hinweise landen im Ergebnis: Tarif und SV-Werte für 2027."""
        r = _schaetze(jahr=2027, brutto_lohn_cent=5000000, gewinn=1000000)
        assert r.tarif.aus_jahr == 2026
        assert any("es gilt der Tarif 2026 fort" in h for h in r.hinweise)
        assert any("Sozialversicherungswerte" in h for h in r.hinweise)

    def test_2029_tarif_2026_verwendet(self):
        """Ein Jahr ohne hinterlegten Tarif rechnet mit dem neuesten und sagt das."""
        r = _schaetze(jahr=2029, brutto_lohn_cent=5000000, gewinn=1000000)
        assert "Tarif 2026 verwendet — für 2029 noch nicht hinterlegt." in r.hinweise
        assert any("Soli-Freigrenze" in h for h in r.hinweise)

    def test_entwurf_2027_rechnet_anders(self):
        """Mit dem Entwurfsschalter sinkt die Steuer 2027 (höherer Grundfreibetrag)."""
        geltend = _schaetze(jahr=2027, brutto_lohn_cent=5000000, gewinn=1000000)
        entwurf = _schaetze(jahr=2027, brutto_lohn_cent=5000000, gewinn=1000000,
                            tarif_entwurf=True)
        assert entwurf.tarif.entwurf
        assert entwurf.est_ohne_cent < geltend.est_ohne_cent
        assert any("Entwurf, vorläufig" in h for h in entwurf.hinweise)

    def test_ohne_gehalt_und_zve_keine_schaetzung(self):
        """Ohne Angabe zum übrigen Einkommen wäre jede Zahl geraten."""
        assert not ss.Eingaben(jahr=2026).rechenbar
        with pytest.raises(ValueError):
            _schaetze(gewinn=1000000)
        assert ss.Eingaben(jahr=2026, brutto_lohn_cent=0).rechenbar


# ================================================================ Eingaben

class TestEingabenPruefen:
    def test_deutsche_betraege_und_saetze(self):
        """Beträge über euro_nach_cent, Sätze über eingabe.prozent."""
        e = ss.pruefe(2026, {"brutto_lohn": "52.000,00", "partner_brutto": "",
                             "zve_ohne": "", "veranlagung": "ZUSAMMEN", "kirchensteuer": "8",
                             "gewinn_erwartet": "-1.500", "vorauszahlungen": "400",
                             "hebesatz": "435 %", "satz_manuell": "32,5 %"})
        assert (e.brutto_lohn_cent, e.partner_brutto_cent, e.gewinn_erwartet_cent) == (
            5200000, None, -150000)
        assert (e.veranlagung, e.kirchensteuer_prozent, e.hebesatz_prozent) == (
            "ZUSAMMEN", 8, 435)
        assert (e.satz_manuell_promille, e.vorauszahlungen_cent) == (325, 40000)

    def test_leere_felder_ergeben_vorgaben(self):
        """Leerer Hebesatz: der Durchschnitt (410 %); leerer Satz: der berechnete."""
        e = ss.pruefe(2026, {})
        assert e.hebesatz_prozent == 410 == ss.hebesatz_vorschlag()
        assert e.satz_manuell_promille is None and e.vorauszahlungen_cent == 0
        assert not e.rechenbar

    @pytest.mark.parametrize("feld,wert,meldung", [
        ("brutto_lohn", "abc", "Bruttogehalt: „abc“ ist kein Betrag."),
        ("brutto_lohn", "-100", "Bruttogehalt: ein negativer Betrag"),
        ("zve_ohne", "1e3", "Zu versteuerndes Einkommen: „1e3“ ist kein Betrag."),
        ("vorauszahlungen", "-5", "Vorauszahlungen: ein negativer Betrag"),
        ("satz_manuell", "120", "Eigener Steuersatz: 120 % ist zu viel"),
        ("satz_manuell", "viel", "Eigener Steuersatz: „viel“ ist kein Prozentsatz"),
        ("hebesatz", "150", "Mindesthebesatz von 200 %"),
        ("hebesatz", "1200", "Hebesatz: 1200 % ist zu hoch (höchstens 1000 %)."),
        ("hebesatz", "vier", "Hebesatz: „vier“ ist keine ganze Zahl."),
        ("kirchensteuer", "7", "Kirchensteuer: bitte keine, 8 % oder 9 % wählen."),
        ("veranlagung", "GETRENNT", "Veranlagung"),
        ("gewinn_erwartet", "9" * 30, "unplausibel groß"),
    ])
    def test_unsinn_wird_abgewiesen(self, feld, wert, meldung):
        """Jede Prüfung mit ihrer Meldung -- die Seite zeigt sie wörtlich."""
        with pytest.raises(ValueError) as fehler:
            ss.pruefe(2026, {feld: wert})
        assert meldung in str(fehler.value)

    def test_mindesthebesatz_2027(self):
        """Ab 2027 sind 280 % das Minimum (BGBl. 2026 I Nr. 197)."""
        assert ss.pruefe(2027, {"hebesatz": "280"}).hebesatz_prozent == 280
        with pytest.raises(ValueError, match="280 %"):
            ss.pruefe(2027, {"hebesatz": "270"})

    def test_entwurf_nur_wo_es_einen_gibt(self):
        """Der Schalter wird für ein Jahr ohne Entwurf stillschweigend nicht gespeichert."""
        assert ss.pruefe(2027, {"tarif_entwurf": "1"}).tarif_entwurf
        assert not ss.pruefe(2026, {"tarif_entwurf": "1"}).tarif_entwurf


# ============================================================== Speichern

class TestSpeichern:
    def test_speichern_und_laden(self, conn):
        """Was gespeichert wird, kommt unverändert zurück."""
        e = ss.Eingaben(jahr=2026, brutto_lohn_cent=5200000, veranlagung="ZUSAMMEN",
                        partner_brutto_cent=1000000, kirchensteuer_prozent=9,
                        satz_manuell_promille=325, gewinn_erwartet_cent=-50000,
                        vorauszahlungen_cent=40000, hebesatz_prozent=435)
        assert ss.speichern(conn, e) is True
        assert ss.laden(conn, 2026) == e
        assert ss.laden(conn, 2025) is None
        assert ss.gespeicherte_jahre(conn) == [2026]

    def test_jede_aenderung_steht_im_journal(self, conn):
        """Je geändertes Feld ein Eintrag „AENDERN“ mit altem und neuem Wert."""
        ss.speichern(conn, ss.Eingaben(jahr=2026, brutto_lohn_cent=5000000))
        ss.speichern(conn, ss.Eingaben(jahr=2026, brutto_lohn_cent=5200000,
                                       hebesatz_prozent=380))
        eintraege = conn.execute(
            "SELECT aktion, feld, wert_alt, wert_neu FROM journal"
            " WHERE objekttyp = 'steuerschaetzung' AND objekt_id = 2026 ORDER BY id"
        ).fetchall()
        felder = [(z["feld"], z["wert_alt"], z["wert_neu"]) for z in eintraege]
        assert all(z["aktion"] == "AENDERN" for z in eintraege)
        assert ("brutto_lohn_cent", None, "5000000") in felder
        assert ("brutto_lohn_cent", "5000000", "5200000") in felder
        assert ("hebesatz_prozent", "410", "380") in felder
        # Leere Felder einer neuen Zeile sind keine Änderung.
        assert not any(f == "zve_ohne_cent" for f, _, _ in felder)

    def test_ohne_aenderung_wird_nichts_geschrieben(self, conn):
        """Zweimal dasselbe: kein Journaleintrag, kein neuer Zeitstempel."""
        e = ss.Eingaben(jahr=2026, brutto_lohn_cent=5000000)
        ss.speichern(conn, e)
        vorher = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        stempel = conn.execute("SELECT geaendert_am FROM steuerschaetzung").fetchone()[0]
        assert ss.speichern(conn, e) is False
        assert conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == vorher
        assert conn.execute("SELECT geaendert_am FROM steuerschaetzung").fetchone()[0] == stempel

    @pytest.mark.parametrize("spalte,wert", [
        ("kirchensteuer_prozent", 7), ("hebesatz_prozent", 100), ("veranlagung", "GETRENNT"),
        ("satz_manuell_promille", 1001), ("vorauszahlungen_cent", -1), ("jahr", 26),
    ])
    def test_datenbank_weist_unsinn_ab(self, conn, spalte, wert):
        """Die CHECKs halten auch ohne Oberfläche (Kirchensteuer 7 %, Hebesatz 100 % …)."""
        import sqlite3

        zeile = {"jahr": 2026, "hebesatz_prozent": 400, "geaendert_am": "x"} | {spalte: wert}
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO steuerschaetzung ({', '.join(zeile)})"
                f" VALUES ({', '.join('?' for _ in zeile)})", tuple(zeile.values()))

    def test_berechne_nimmt_den_euer_gewinn(self, conn):
        """Ohne erwarteten Gewinn rechnet die Schätzung mit dem EÜR-Gewinn des Jahres."""
        ss.speichern(conn, ss.Eingaben(jahr=2026, brutto_lohn_cent=5000000))
        r = ss.berechne(conn, 2026, heute=HEUTE)
        assert r.gewinn_cent == 0 and r.gewinn_aus_euer
        assert r.mehrsteuer_cent == 0
        assert ss.berechne(conn, 2025) is None


class TestMigration:
    def test_version_18_legt_die_tabelle_an(self, tmp_path):
        """Eine Datenbank auf Stand 17 bekommt die Tabelle -- mit Journaleintrag."""
        pfad = tmp_path / "alt.sqlite"
        verbindung = db.oeffne(pfad)
        verbindung.execute("DROP TABLE steuerschaetzung")
        verbindung.execute("DELETE FROM schema_version WHERE version >= 18")
        verbindung.execute("INSERT OR REPLACE INTO schema_version VALUES (17, 'x')")
        verbindung.commit()
        verbindung.close()

        verbindung = db.oeffne(pfad)
        try:
            assert verbindung.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
            spalten = {z["name"] for z in verbindung.execute(
                "PRAGMA table_info(steuerschaetzung)")}
            assert {"jahr", "brutto_lohn_cent", "satz_manuell_promille",
                    "tarif_entwurf"} <= spalten
            assert verbindung.execute(
                "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION' AND objekt_id = 18"
            ).fetchone()[0] == 1
        finally:
            verbindung.close()


# ================================================================ Oberfläche

import re  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import erzeuge_app  # noqa: E402

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}


@pytest.fixture
def klient(tmp_path, monkeypatch):
    """Ein eingerichteter Betrieb; heute ist der 25.10.2026 (Rechtsstand der Werte)."""
    monkeypatch.setattr(db, "heute", lambda: "2026-10-25")
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


def _laufend() -> int:
    return 2026


def _einnahme_und_ausgabe(pfad, einnahme_cent, ausgabe_cent, datum="2026-04-01"):
    """Eine bezahlte Rechnung und ein bezahlter Beleg -- direkt in der Datenbank."""
    verbindung = db.oeffne(pfad)
    try:
        kunde = verbindung.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
            " VALUES ('K-0001', 'Kundin', 'Weg 1', '10115', 'Berlin', ?)", (datum,)).lastrowid
        rechnung = verbindung.execute(
            """INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                   netto_cent, brutto_cent, leistungsort, betreff, status,
                   festgeschrieben_am, erstellt_am)
               VALUES ('RECHNUNG', 'RE-2026-0001', 1, 2026, ?, ?, ?, ?, 'INLAND', 'Leistung',
                       'FESTGESCHRIEBEN', ?, ?)""",
            (kunde, datum, einnahme_cent, einnahme_cent, db.jetzt_utc(), db.jetzt_utc()),
        ).lastrowid
        verbindung.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, ?, ?)",
                           (rechnung, datum, einnahme_cent))
        kategorie = verbindung.execute(
            "SELECT id FROM euer_kategorie WHERE bereich = 25 ORDER BY id LIMIT 1").fetchone()[0]
        beleg = verbindung.execute(
            """INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,
                   aufteilungsquote, betrag_betrieblich_cent, erfasst_am)
               VALUES ('EINGANG', ?, ?, ?, 100, ?, ?)""",
            (kategorie, datum, ausgabe_cent, ausgabe_cent, db.jetzt_utc())).lastrowid
        verbindung.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                           (beleg, datum, -ausgabe_cent))
        verbindung.commit()
    finally:
        verbindung.close()


class TestSeite:
    def test_ohne_einrichtung_zur_einrichtung(self, tmp_path):
        """Wie jede Seite: ohne Betrieb erst die Einrichtung."""
        with TestClient(erzeuge_app(tmp_path / "leer.sqlite")) as c:
            antwort = c.get("/steuer", follow_redirects=False)
        assert antwort.status_code == 303 and antwort.headers["location"] == "/einrichtung"

    def test_leere_seite_fragt_nach_dem_gehalt(self, klient):
        """Ohne Angaben keine Zahl, sondern die Bitte ums Gehalt -- und die Abstufungen."""
        text = klient.get("/steuer").text
        assert "Trag dein Bruttogehalt ein" in text
        assert "So viel solltest du mindestens zurücklegen" not in text
        assert "Abstufungen der Einkommensteuer" in text and "Grundfreibetrag" in text
        assert "keine Steuerberatung" in text
        assert 'value="410"' in text  # Hebesatz vorbelegt

    def test_navigation_unter_buchfuehrung(self, klient):
        """Der Eintrag steht in der Registerleiste nach der EÜR und ist auf der Seite aktiv."""
        text = klient.get("/steuer").text
        assert text.index('href="/euer"') < text.index('href="/steuer"')
        assert 'href="/steuer" aria-current="page"' in text

    def test_speichern_und_ergebnis(self, klient):
        """Speichern leitet zum Ergebnis; die Rücklage steht groß da."""
        jahr = _laufend()
        antwort = klient.post("/steuer", data={
            "jahr": str(jahr), "brutto_lohn": "50.000", "gewinn_erwartet": "30.000",
            "kirchensteuer": "9", "hebesatz": "410",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith(f"/steuer?jahr={jahr}&erfolg=")
        assert antwort.headers["location"].endswith("#ergebnis")
        text = klient.get(antwort.headers["location"]).text
        assert "Angaben für" in text and "gespeichert" in text
        assert "So viel solltest du mindestens zurücklegen" in text
        assert '<div class="wert">11.920 €</div>' in text
        assert 'value="50.000,00"' in text and 'value="30.000,00"' in text
        assert "Kirchensteuer 9 %" in text
        assert "ohne Gewerbe</span>" in text and "mit Gewerbe</span>" in text

    def test_fehler_behaelt_die_eingaben(self, klient):
        """422 mit Meldung; alle Felder stehen noch da, nichts ist gespeichert."""
        jahr = _laufend()
        antwort = klient.post("/steuer", data={
            "jahr": str(jahr), "brutto_lohn": "52.000", "partner_brutto": "31.000",
            "veranlagung": "ZUSAMMEN", "kirchensteuer": "8", "hebesatz": "150",
            "satz_manuell": "33,5", "vorauszahlungen": "1.200",
        })
        assert antwort.status_code == 422
        assert "Mindesthebesatz" in antwort.text
        for wert in ('value="52.000"', 'value="31.000"', 'value="150"', 'value="33,5"',
                     'value="1.200"'):
            assert wert in antwort.text, wert
        assert re.search(r'value="ZUSAMMEN"\s+checked', antwort.text)
        assert 'value="8" selected' in antwort.text
        assert klient.get("/steuer").text.count("So viel solltest du") == 0

    def test_unsinniges_jahr(self, klient):
        """Ein Jahr wie 0026 wird nicht gespeichert; in der Adresse heißt Unsinn Standardjahr."""
        antwort = klient.post("/steuer", data={"jahr": "26", "brutto_lohn": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303 and "fehler=" in antwort.headers["location"]
        assert klient.get("/steuer?jahr=abc").status_code == 200
        assert f"Steuerrücklage {_laufend()}" in klient.get("/steuer?jahr=99999").text

    def test_eigener_satz_mit_vorschlag(self, klient):
        """Mit eigenem Satz steht der berechnete daneben -- zum Übernehmen."""
        jahr = _laufend()
        klient.post("/steuer", data={"jahr": str(jahr), "brutto_lohn": "40.000",
                                     "gewinn_erwartet": "20.000", "satz_manuell": "30"})
        text = klient.get(f"/steuer?jahr={jahr}").text
        assert "deinem Satz von 30,0 %" in text
        assert '<div class="wert">6.000 €</div>' in text
        assert 'data-fuellen="satz_manuell"' in text and "Berechneten Satz übernehmen" in text
        assert 'value="30"' in text

    def test_verlust(self, klient):
        """Ein Verlust zeigt 0 € Rücklage und die voraussichtliche Ersparnis."""
        jahr = _laufend()
        klient.post("/steuer", data={"jahr": str(jahr), "brutto_lohn": "40.000",
                                     "gewinn_erwartet": "-3.000"})
        text = klient.get(f"/steuer?jahr={jahr}").text
        assert '<div class="wert">0 €</div>' in text
        assert "Der Gewinn ist negativ" in text

    def test_entwurfsschalter_nur_fuer_2027(self, klient):
        """Der Schalter erscheint nur für ein Jahr mit Entwurf -- und rechnet dann vorläufig."""
        assert "Tarifentwurf" not in klient.get("/steuer?jahr=2026").text
        text = klient.get("/steuer?jahr=2027").text
        assert "Mit dem Tarifentwurf für 2027 rechnen" in text
        klient.post("/steuer", data={"jahr": "2027", "brutto_lohn": "40.000",
                                     "gewinn_erwartet": "10.000", "tarif_entwurf": "1"})
        text = klient.get("/steuer?jahr=2027").text
        assert "Entwurf, vorläufig" in text and "(Entwurf)" in text
        assert re.search(r'name="tarif_entwurf" value="1"\s+checked', text)

    def test_2027_ohne_entwurf_nennt_den_fortgeltenden_tarif(self, klient):
        """Ersatzjahr-Hinweis auf der Seite: es gilt der Tarif 2026 fort."""
        klient.post("/steuer", data={"jahr": "2027", "brutto_lohn": "40.000",
                                     "gewinn_erwartet": "10.000"})
        text = klient.get("/steuer?jahr=2027").text
        assert "es gilt der Tarif 2026 fort" in text
        assert "Abstufungen der Einkommensteuer 2026" in text

    def test_gewinn_aus_der_euer_mit_faustregel_und_hochrechnung(self, klient, tmp_path):
        """Ohne erwarteten Gewinn gilt die EÜR; fürs laufende Jahr steht eine Hochrechnung daneben.

        Bezahlte Rechnung 12.000 €, bezahlter Beleg 2.000 € -- Gewinn 10.000 €.
        Die Hochrechnung bis Oktober (10 Monate) ergibt 12.000 € und wird
        nur angeboten, nicht übernommen.
        """
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1200000, 200000)
        klient.post("/steuer", data={"jahr": "2026", "brutto_lohn": "40.000",
                                     "satz_manuell": "30"})
        text = klient.get("/steuer?jahr=2026").text
        assert "13.330" not in text
        assert "10.000,00 €</strong>" in text  # Gewinn laut EÜR
        assert "Gleichmäßig weitergerechnet" in text and "12.000 €" in text
        assert 'data-fuellen="gewinn_erwartet"' in text and 'data-wert="12.000,00"' in text
        assert 'name="gewinn_erwartet" inputmode="decimal" autocomplete="off"\n' \
               '               value=""' in text
        # 30 % auf den Gewinn, Gewinn = 10/12 der Einnahmen: 25 Cent je Euro.
        assert "etwa <strong>25 Cent</strong>" in text
        assert '<div class="wert">3.000 €</div>' in text
        # Im Vorjahr keine Hochrechnung.
        assert "Gleichmäßig weitergerechnet" not in klient.get("/steuer?jahr=2025").text

    def test_druckansicht_ohne_formular(self, klient):
        """Das Formular steht in einem Abschnitt, der nicht gedruckt wird; die Angaben schon."""
        jahr = _laufend()
        klient.post("/steuer", data={"jahr": str(jahr), "brutto_lohn": "40.000",
                                     "gewinn_erwartet": "10.000"})
        text = klient.get(f"/steuer?jahr={jahr}").text
        assert '<section class="steuer-eingaben nicht-drucken"' in text
        assert '<div class="nur-druck">' in text and "Bruttogehalt 40.000 €" in text

    def test_journal_und_excel(self, klient, tmp_path):
        """Die Angaben stehen im Journal und im Excel-Export."""
        jahr = _laufend()
        klient.post("/steuer", data={"jahr": str(jahr), "brutto_lohn": "40.000"})
        assert "steuerschaetzung" in klient.get("/journal").text
        antwort = klient.get("/backup/excel")
        assert antwort.status_code == 200
        from io import BytesIO

        from openpyxl import load_workbook

        mappe = load_workbook(BytesIO(antwort.content))
        assert "Steuerrücklage (Angaben)" in mappe.sheetnames
        blatt = mappe["Steuerrücklage (Angaben)"]
        kopf = [z.value for z in blatt[1]]
        assert "Bruttogehalt" in kopf and "Eigener Steuersatz" in kopf


class TestUebersicht:
    def test_ohne_angaben_ein_verweis(self, klient):
        """Vor der ersten Angabe: „Steuerrücklage schätzen“."""
        text = klient.get("/").text
        assert '<a href="/steuer">Steuerrücklage schätzen</a>' in text

    def test_mit_angaben_der_betrag(self, klient):
        """Sobald Angaben gespeichert sind, steht die Rücklage auf der Übersicht."""
        jahr = _laufend()
        klient.post("/steuer", data={"jahr": str(jahr), "brutto_lohn": "40.000",
                                     "gewinn_erwartet": "20.000", "satz_manuell": "30"})
        text = klient.get("/").text
        assert f"Steuerrücklage {jahr}" in text
        assert f'<a href="/steuer?jahr={jahr}"><strong class="mono">6.000 €</strong></a>' in text
        # Als grosse Kachel, im Stil der Zahl auf /steuer -- nicht als Textzeile.
        assert 'class="ruecklage"' in text

    def test_angaben_ohne_gehalt_ergeben_keinen_betrag(self, klient):
        """Gespeichert, aber ohne Gehalt und zvE: weiter nur der Verweis, keine geratene Zahl."""
        jahr = _laufend()
        antwort = klient.post("/steuer", data={"jahr": str(jahr), "hebesatz": "400"},
                              follow_redirects=False)
        assert "fehlt noch das" in klient.get(antwort.headers["location"]).text
        assert "Steuerrücklage schätzen" in klient.get("/").text

    def test_plan_fuers_naechste_jahr_verdraengt_das_laufende_nicht(self, klient):
        """Angaben für 2027 (Planung) und 2026: die Übersicht zeigt 2026."""
        for jahr in ("2027", "2026"):
            klient.post("/steuer", data={"jahr": jahr, "brutto_lohn": "40.000",
                                         "gewinn_erwartet": "20.000", "satz_manuell": "30"})
        text = klient.get("/").text
        assert "Steuerrücklage 2026" in text and "Steuerrücklage 2027" not in text
        # ... und die Jahresauswahl der Seite bietet das nächste Jahr an.
        assert '<option value="2027"' in klient.get("/steuer").text


class TestUnsichereWerte2027:
    def test_soli_fortgeltung_nur_wenn_soli_anfaellt(self):
        """Die fortgeltende Freigrenze 2027 wird genannt, wenn der Soli das Ergebnis berührt."""
        klein = _schaetze(jahr=2027, brutto_lohn_cent=4000000, gewinn=1000000)
        assert not any("Soli-Freigrenze" in h for h in klein.hinweise)
        gross = _schaetze(jahr=2027, brutto_lohn_cent=9000000, gewinn=3000000)
        assert gross.soli_mit_cent > 0
        assert any("gilt die Soli-Freigrenze von 2026 fort" in h for h in gross.hinweise)

    def test_kirchensteuer_2027_nicht_belegt(self):
        """Kirchensteuer 2027: gerechnet wie bisher, mit Hinweis -- ohne Kirchensteuer keiner."""
        mit = _schaetze(jahr=2027, brutto_lohn_cent=4000000, gewinn=1000000,
                        kirchensteuer_prozent=9)
        assert any("Kirchensteuersätze für 2027" in h for h in mit.hinweise)
        ohne = _schaetze(jahr=2027, brutto_lohn_cent=4000000, gewinn=1000000)
        assert not any("Kirchensteuer" in h for h in ohne.hinweise)
        assert not any("Kirchensteuer" in h for h in _schaetze(
            brutto_lohn_cent=4000000, gewinn=1000000, kirchensteuer_prozent=9).hinweise)


# ------------------------------------------------- Nachbesserungen der Abnahme

class TestAbnahmeNachtrag:
    """Drei Kleinigkeiten aus der Abnahme: Texte, die zur Lage nicht passten."""

    def test_ohne_gehalt_kein_satz_zur_lohnsteuer(self, klient):
        """Ohne Gehalt stand im Rechenweg trotzdem, die Lohnsteuer liege meist
        unter der Jahressteuer auf das Gehalt."""
        seite = klient.post("/steuer", data={"jahr": "2026", "brutto_lohn": "0",
                                             "gewinn_erwartet": "5.000"}).text
        assert "Die Lohnsteuer liegt meist" not in seite
        seite = klient.post("/steuer", data={"jahr": "2026", "brutto_lohn": "45.000",
                                             "gewinn_erwartet": "5.000"}).text
        assert "Die Lohnsteuer liegt meist" in seite

    def test_ohne_gewinn_keine_erstattung_versprochen(self, klient):
        """Leere EÜR, nichts erwartet, aber Vorauszahlungen eingetragen: die
        Aufschlüsselung endete mit „Voraussichtlich zurück 1.200,00 €“."""
        seite = klient.post("/steuer", data={"jahr": "2026", "brutto_lohn": "45.000",
                                             "vorauszahlungen": "1.200"}).text
        assert "Voraussichtlich zurück" not in seite

    def test_warnung_nur_wenn_sie_das_ergebnis_aendert(self):
        """Ein erwarteter Verlust bei leerer EÜR, oder zvE 10.000 € mit 1.000 € Gewinn
        laut EÜR und 0 erwartet: beides ergibt 0 € Rücklage. Die Warnung „danach wären
        es 0 €“ stand trotzdem daneben."""
        r = ss._mit_euer_vergleich(_schaetze(gewinn=100000, zve_ohne_cent=1000000,
                                             gewinn_erwartet_cent=0), HEUTE)
        assert r.offen_cent == 0 and not r.erwartet_unter_euer
        r = ss._mit_euer_vergleich(_schaetze(gewinn=800000, brutto_lohn_cent=4500000,
                                             gewinn_erwartet_cent=100000), HEUTE)
        assert r.erwartet_unter_euer and r.offen_laut_euer_cent > r.offen_cent

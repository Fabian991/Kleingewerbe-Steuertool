"""Anlagevermögen, Abschreibung und das GWG-Verzeichnis.

Der Kern dieser Datei ist **eine** Unterscheidung, und sie ist die häufigste
Fehlerquelle in selbstgebauten Werkzeugen:

* Die **Wertgrenze** wird nach R 9b Abs. 2 EStR **stets netto** geprüft — „Ob
  der Vorsteuerbetrag umsatzsteuerrechtlich abziehbar ist, spielt in diesem
  Fall keine Rolle." 800 € netto sind rund 952 € brutto.
* **Abgeschrieben** wird der **Bruttobetrag**, weil die nicht abziehbare
  Vorsteuer nach R 9b Abs. 1 EStR zu den Anschaffungskosten gehört.

Wer nur eine Zahl führt, prüft die Grenze am Buchungswert — und schreibt dann
Güter ab, die sofort abziehbar wären, oder zieht Güter sofort ab, die ins
Verzeichnis gehören. Beides fällt erst in der Betriebsprüfung auf.
"""

import sqlite3

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


# ------------------------------------------------------------- Einstufung

class TestWertgrenze:
    @pytest.mark.parametrize("netto,methode,verzeichnis", [
        (10000, a.GWG_SOFORT, False),    # 100 € — unter 250, kein Verzeichnis
        (25000, a.GWG_SOFORT, False),    # genau 250 € — noch ohne Verzeichnis
        (25001, a.GWG_SOFORT, True),     # einen Cent darüber — Verzeichnis
        (80000, a.GWG_SOFORT, True),     # genau 800 € — noch sofort abziehbar
        (80001, a.LINEAR, True),         # einen Cent darüber — Anlagegut
    ])
    def test_die_drei_stufen(self, netto, methode, verzeichnis):
        einstufung = a.stufe_ein(netto, jahr=2026)
        assert einstufung.methode == methode
        assert einstufung.ins_verzeichnis is verzeichnis

    def test_der_buerostuhl(self):
        """900 € brutto sind 756,30 € netto — unter der Grenze.

        Der Fall aus der Praxis: wer am Bruttobetrag prüft, schreibt diesen
        Stuhl über dreizehn Jahre ab, obwohl er sofort abziehbar ist.
        """
        from app.logik.kategorien import netto_fuer_grenzwert_cent

        netto = netto_fuer_grenzwert_cent(90000, None, 190)
        assert netto == 75630
        assert a.stufe_ein(netto, jahr=2026).methode == a.GWG_SOFORT

    def test_umgekehrt_der_monitor_ohne_umsatzsteuer(self):
        """850 € von einem Kleinunternehmer-Lieferanten sind 850 € netto.

        Dieselbe Zahl, anderes Ergebnis: ohne ausgewiesene Steuer ist Netto
        gleich Brutto, und damit liegt das Gut über der Grenze. Mit 19 % wären
        es 714,29 € netto und sofort abziehbar.
        """
        from app.logik.kategorien import netto_fuer_grenzwert_cent

        ohne_steuer = netto_fuer_grenzwert_cent(85000, None, None)
        mit_steuer = netto_fuer_grenzwert_cent(85000, None, 190)
        assert ohne_steuer == 85000
        assert mit_steuer == 71429
        assert a.stufe_ein(ohne_steuer, jahr=2026).methode == a.LINEAR
        assert a.stufe_ein(mit_steuer, jahr=2026).methode == a.GWG_SOFORT

    def test_it_darf_ein_jahr(self):
        einstufung = a.stufe_ein(240000, jahr=2026, ist_it=True)
        assert einstufung.methode == a.EIN_JAHR_IT
        assert einstufung.sofort_abziehbar
        assert einstufung.ins_verzeichnis, "Muss TROTZDEM ins Verzeichnis"

    def test_immaterielles_gut_hat_eine_eigene_kennzahl(self):
        assert a.stufe_ein(240000, jahr=2026, art="IMMATERIELL").kennzahl == a.KZ_IMMATERIELL
        assert a.stufe_ein(240000, jahr=2026).kennzahl == a.KZ_BEWEGLICH
        assert a.stufe_ein(20000, jahr=2026).kennzahl == a.KZ_GWG

    def test_begruendung_nennt_die_fundstelle(self):
        assert "§ 6 Abs. 2 EStG" in a.stufe_ein(45000, jahr=2026).begruendung
        assert "netto" in a.stufe_ein(240000, jahr=2026).begruendung

    def test_betraege_stehen_deutsch(self):
        """756.30 € wäre ein englischer Dezimaltrenner auf einem deutschen Beleg."""
        text = a.stufe_ein(75630, jahr=2026).begruendung
        assert "756,30" in text
        assert "756.30" not in text


# ------------------------------------------------------- Abschreibungsreihe

class TestAfaReihe:
    def test_sofortabzug_wirkt_voll_im_anschaffungsjahr(self):
        reihe = a.afa_reihe(ak_brutto_cent=95200, anschaffung_am="2026-10-15",
                            nutzungsdauer_jahre=1, methode=a.GWG_SOFORT, kennzahl=132)
        assert [(j.jahr, j.afa_cent) for j in reihe] == [(2026, 95200)]

    def test_it_ein_jahr_ohne_zeitanteil(self):
        """Die Ein-Jahres-Regel ist ein Sofortabzug, keine Nutzungsdauer von
        zwölf Monaten — ein im Dezember gekaufter Rechner wirkt voll."""
        reihe = a.afa_reihe(ak_brutto_cent=285600, anschaffung_am="2026-12-20",
                            nutzungsdauer_jahre=1, methode=a.EIN_JAHR_IT, kennzahl=130)
        assert [(j.jahr, j.afa_cent) for j in reihe] == [(2026, 285600)]

    def test_linear_rechnet_zeitanteilig(self):
        """§ 7 Abs. 1 Satz 4 EStG: je angefangenem Monat ein Zwölftel.

        Der häufigste stille Fehler bei selbst gerechneter AfA — ein im
        Oktober gekaufter Schreibtisch bringt drei Zwölftel, nicht ein Jahr.
        """
        reihe = a.afa_reihe(ak_brutto_cent=120000, anschaffung_am="2026-10-05",
                            nutzungsdauer_jahre=3, methode=a.LINEAR, kennzahl=130)
        assert [(j.jahr, j.afa_cent) for j in reihe] == [
            (2026, 10000), (2027, 40000), (2028, 40000), (2029, 30000),
        ]

    def test_im_januar_gekauft_bringt_das_volle_jahr(self):
        reihe = a.afa_reihe(ak_brutto_cent=120000, anschaffung_am="2026-01-10",
                            nutzungsdauer_jahre=3, methode=a.LINEAR, kennzahl=130)
        assert reihe[0].afa_cent == 40000
        assert len(reihe) == 3

    def test_summe_ist_immer_genau_die_anschaffungskosten(self):
        """Eine Reihe, die einen Cent übrig lässt, hinterlässt ein Gut mit
        Restwert 0,01 € im Verzeichnis — für immer."""
        for brutto in (178500, 100000, 99999, 333333, 1, 777):
            for monat in (1, 4, 10, 12):
                for dauer in (1, 3, 7, 13):
                    reihe = a.afa_reihe(
                        ak_brutto_cent=brutto,
                        anschaffung_am=f"2026-{monat:02d}-05",
                        nutzungsdauer_jahre=dauer, methode=a.LINEAR, kennzahl=130,
                    )
                    summe = sum(j.afa_cent for j in reihe)
                    assert summe == brutto, (brutto, monat, dauer, summe)
                    assert reihe[-1].restwert_cent == 0

    def test_restwert_laeuft_monoton_auf_null(self):
        reihe = a.afa_reihe(ak_brutto_cent=178500, anschaffung_am="2026-10-05",
                            nutzungsdauer_jahre=13, methode=a.LINEAR, kennzahl=130)
        reste = [j.restwert_cent for j in reihe]
        assert reste == sorted(reste, reverse=True)
        assert reste[-1] == 0

    def test_nutzungsdauer_null_wird_abgewiesen(self):
        with pytest.raises(a.AnlagenFehler):
            a.afa_reihe(ak_brutto_cent=100000, anschaffung_am="2026-01-01",
                        nutzungsdauer_jahre=0, methode=a.LINEAR, kennzahl=130)


# --------------------------------------------------------------- Anlegen

class TestAnlegen:
    def test_netto_wird_aus_dem_steuersatz_gerechnet(self, conn, betrieb):
        gid = a.anlegen(conn, bezeichnung="Bürostuhl", anschaffung_am="2026-03-14",
                        ak_brutto_cent=90000, ust_satz_promille=190)
        zeile = conn.execute("SELECT * FROM anlagegut WHERE id = ?", (gid,)).fetchone()
        assert zeile["ak_brutto_cent"] == 90000
        assert zeile["ak_netto_cent"] == 75630
        assert zeile["methode"] == a.GWG_SOFORT

    def test_abgeschrieben_wird_der_bruttowert(self, conn, betrieb):
        """R 9b Abs. 1 EStR: die nicht abziehbare Vorsteuer gehört zu den
        Anschaffungskosten."""
        gid = a.anlegen(conn, bezeichnung="MacBook", anschaffung_am="2026-04-01",
                        ak_brutto_cent=285600, ust_satz_promille=190, ist_it=True)
        summe = conn.execute(
            "SELECT SUM(afa_cent) AS s FROM anlagegut_afa_jahr WHERE anlagegut_id = ?",
            (gid,),
        ).fetchone()["s"]
        assert summe == 285600, "Nicht 240000 — gebucht wird brutto"

    def test_linear_ohne_nutzungsdauer_wird_abgewiesen(self, conn, betrieb):
        with pytest.raises(a.AnlagenFehler, match="Nutzungsdauer"):
            a.anlegen(conn, bezeichnung="Schreibtisch", anschaffung_am="2026-10-05",
                      ak_brutto_cent=178500, ust_satz_promille=190)

    def test_gwg_braucht_keine_nutzungsdauer(self, conn, betrieb):
        a.anlegen(conn, bezeichnung="Tastatur", anschaffung_am="2026-02-01",
                  ak_brutto_cent=11900, ust_satz_promille=190)

    def test_ohne_bezeichnung_wird_abgewiesen(self, conn, betrieb):
        with pytest.raises(a.AnlagenFehler, match="Bezeichnung"):
            a.anlegen(conn, bezeichnung="  ", anschaffung_am="2026-02-01",
                      ak_brutto_cent=11900)

    def test_nullbetrag_wird_abgewiesen(self, conn, betrieb):
        with pytest.raises(a.AnlagenFehler):
            a.anlegen(conn, bezeichnung="Nichts", anschaffung_am="2026-02-01",
                      ak_brutto_cent=0)

    def test_begruendung_steht_im_journal(self, conn, betrieb):
        a.anlegen(conn, bezeichnung="Bürostuhl", anschaffung_am="2026-03-14",
                  ak_brutto_cent=90000, ust_satz_promille=190)
        zeile = conn.execute(
            "SELECT * FROM journal WHERE aktion = 'ANLAGEGUT'").fetchone()
        assert "§ 6 Abs. 2 EStG" in zeile["begruendung"]

    def test_beleg_wird_als_anlagegut_markiert(self, conn, betrieb):
        bid = conn.execute(
            "INSERT INTO beleg (belegdatum, betrag_brutto_cent, betrag_betrieblich_cent,"
            " erfasst_am) VALUES ('2026-04-01', 285600, 285600, ?)", (db.jetzt_utc(),)
        ).lastrowid
        a.anlegen(conn, bezeichnung="MacBook", anschaffung_am="2026-04-01",
                  ak_brutto_cent=285600, ust_satz_promille=190, ist_it=True, beleg_id=bid)
        assert conn.execute(
            "SELECT ist_anlagegut FROM beleg WHERE id = ?", (bid,)
        ).fetchone()[0] == 1

    def test_schema_haelt_netto_kleiner_gleich_brutto(self, conn, betrieb):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO anlagegut (bezeichnung, anschaffung_am, ak_brutto_cent,"
                " ak_netto_cent, nutzungsdauer_jahre, erfasst_am)"
                " VALUES ('X', '2026-01-01', 10000, 20000, 3, ?)", (db.jetzt_utc(),)
            )

    def test_schema_haelt_ein_jahr_bei_der_it_regel(self, conn, betrieb):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO anlagegut (bezeichnung, anschaffung_am, ak_brutto_cent,"
                " ak_netto_cent, nutzungsdauer_jahre, methode, erfasst_am)"
                " VALUES ('X', '2026-01-01', 10000, 10000, 3, 'EIN_JAHR_IT', ?)",
                (db.jetzt_utc(),)
            )


# ---------------------------------------------------------------- Abgang

class TestAbgang:
    @pytest.fixture
    def gut(self, conn, betrieb):
        return a.anlegen(conn, bezeichnung="Schreibtisch", anschaffung_am="2026-01-05",
                         ak_brutto_cent=120000, ust_satz_promille=190,
                         nutzungsdauer_jahre=3)

    def test_abgang_kappt_nur_die_folgejahre(self, conn, gut):
        """Die Vorjahre bleiben stehen: was einmal in einer Steuererklärung
        stand, muss nachvollziehbar bleiben."""
        a.abgang(conn, gut, am="2027-06-30", grund="verkauft", erloes_cent=30000)
        jahre = [z["jahr"] for z in conn.execute(
            "SELECT jahr FROM anlagegut_afa_jahr WHERE anlagegut_id = ? ORDER BY jahr",
            (gut,))]
        assert jahre == [2026, 2027]

    def test_im_abgangsjahr_zeitanteilig_und_der_rest_ist_restbuchwert(self, conn, gut):
        """1.200 € über 3 Jahre ab Januar 2026, Verkauf am 30.06.2027 für 300 €.

        2026: 400 €. 2027: sechs Zwölftel von 400 = 200 €. Restbuchwert 600 €
        -- Betriebsausgabe im Abgangsjahr. Der Erlös 300 € Betriebseinnahme.
        Vorher stand 2027 mit vollen 400 € AfA da, und die 600 € Restbuchwert
        tauchten nirgends auf: weder abgeschrieben noch abgesetzt.
        """
        a.abgang(conn, gut, am="2027-06-30", grund="verkauft", erloes_cent=30000)
        reihe = {z["jahr"]: (z["afa_cent"], z["restwert_cent"]) for z in conn.execute(
            "SELECT * FROM anlagegut_afa_jahr WHERE anlagegut_id = ?", (gut,))}
        assert reihe == {2026: (40000, 80000), 2027: (20000, 60000)}
        assert a.afa_summe(conn, 2027) == {a.KZ_BEWEGLICH: 20000}
        assert a.afa_summe(conn, 2028) == {}
        assert a.abgaenge(conn, 2027) == (30000, 60000)
        assert a.abgaenge(conn, 2026) == (0, 0)
        eintrag = next(e for e in a.verzeichnis(conn, 2027) if e["id"] == gut)
        assert eintrag["restbuchwert_abgang_cent"] == 60000

    def test_abgang_laesst_sich_zuruecknehmen(self, conn, gut):
        """Der Tippfehler im Datum. Vorher waren die Folgejahre geloescht und
        die Reihe nicht wiederherstellbar."""
        vorher = [tuple(z) for z in conn.execute(
            "SELECT jahr, afa_cent, restwert_cent FROM anlagegut_afa_jahr"
            " WHERE anlagegut_id = ? ORDER BY jahr", (gut,))]
        a.abgang(conn, gut, am="2026-02-01", grund="vertippt")
        a.abgang_zuruecknehmen(conn, gut, grund="Datum vertippt, Gut ist noch da")
        nachher = [tuple(z) for z in conn.execute(
            "SELECT jahr, afa_cent, restwert_cent FROM anlagegut_afa_jahr"
            " WHERE anlagegut_id = ? ORDER BY jahr", (gut,))]
        assert nachher == vorher
        zeile = conn.execute("SELECT abgang_am, abgang_erloes_cent FROM anlagegut WHERE id = ?",
                             (gut,)).fetchone()
        assert zeile["abgang_am"] is None and zeile["abgang_erloes_cent"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM journal WHERE aktion = 'ANLAGEGUT_ABGANG_ZURUECK'"
        ).fetchone()[0] == 1

    def test_zweiter_abgang_braucht_erst_die_ruecknahme(self, conn, gut):
        a.abgang(conn, gut, am="2027-06-30", grund="verkauft")
        with pytest.raises(a.AnlagenFehler, match="zurücknehmen"):
            a.abgang(conn, gut, am="2027-07-30", grund="doch anders")

    def test_ruecknahme_ohne_abgang_wird_abgewiesen(self, conn, gut):
        with pytest.raises(a.AnlagenFehler, match="nicht abgegangen"):
            a.abgang_zuruecknehmen(conn, gut, grund="x")

    def test_abgang_vor_anschaffung_wird_abgewiesen(self, conn, gut):
        with pytest.raises(a.AnlagenFehler, match="vor der Anschaffung"):
            a.abgang(conn, gut, am="2025-01-01", grund="verkauft")

    def test_abgang_ohne_grund_wird_abgewiesen(self, conn, gut):
        with pytest.raises(a.AnlagenFehler, match="Grund"):
            a.abgang(conn, gut, am="2027-06-30", grund="  ")

    def test_abgang_wird_protokolliert(self, conn, gut):
        a.abgang(conn, gut, am="2027-06-30", grund="an Kollegen verkauft",
                 erloes_cent=30000)
        zeile = conn.execute(
            "SELECT * FROM journal WHERE aktion = 'ANLAGEGUT_ABGANG'").fetchone()
        assert zeile["begruendung"] == "an Kollegen verkauft"


# -------------------------------------------------------- Betrieblicher Anteil

def _beleg(conn, *, quote=70, brutto=119000, netto=100000, ust=19000, satz=190):
    return conn.execute(
        "INSERT INTO beleg (art, belegdatum, betrag_brutto_cent, betrag_netto_cent,"
        " betrag_ust_cent, ust_satz_promille, aufteilungsquote, betrag_betrieblich_cent,"
        " erfasst_am) VALUES ('EINGANG', '2026-01-10', ?, ?, ?, ?, ?, ?, ?)",
        (brutto, netto, ust, satz, quote, brutto * quote // 100, db.jetzt_utc()),
    ).lastrowid


class TestBetrieblicherAnteil:
    def test_afa_geht_nur_zum_betrieblichen_anteil_in_die_euer(self, conn, betrieb):
        """Ein zu 70 Prozent betrieblich genutzter Rechner: 70 Prozent der
        Abschreibung sind Betriebsausgabe. Vorher wurden 100 Prozent
        abgeschrieben -- die Quote des Belegs war der AfA unbekannt."""
        beleg = _beleg(conn, quote=70)
        gut = a.anlegen(conn, bezeichnung="Rechner", anschaffung_am="2026-01-10",
                        ak_brutto_cent=119000, ak_netto_cent=100000, nutzungsdauer_jahre=3,
                        beleg_id=beleg)
        zeile = conn.execute("SELECT aufteilungsquote FROM anlagegut WHERE id = ?",
                             (gut,)).fetchone()
        assert zeile["aufteilungsquote"] == 70, "vom Beleg uebernommen"
        volle_afa = 119000 // 3
        assert a.afa_summe(conn, 2026) == {a.KZ_BEWEGLICH: (volle_afa * 70 + 50) // 100}
        eintrag = a.verzeichnis(conn, 2026)[0]
        assert eintrag["afa_dieses_jahr_cent"] == volle_afa, "das Verzeichnis zeigt das Gut ganz"
        assert eintrag["afa_dieses_jahr_betrieblich_cent"] == (volle_afa * 70 + 50) // 100

    def test_quote_laesst_sich_ausdruecklich_setzen(self, conn, betrieb):
        gut = a.anlegen(conn, bezeichnung="Kamera", anschaffung_am="2026-01-10",
                        ak_brutto_cent=200000, ust_satz_promille=190, nutzungsdauer_jahre=5,
                        aufteilungsquote=50)
        assert conn.execute("SELECT aufteilungsquote FROM anlagegut WHERE id = ?",
                            (gut,)).fetchone()[0] == 50
        assert a.afa_summe(conn, 2026) == {a.KZ_BEWEGLICH: 20000}

    def test_unsinnige_quote_wird_abgewiesen(self, conn, betrieb):
        with pytest.raises(a.AnlagenFehler, match="zwischen 0 und 100"):
            a.anlegen(conn, bezeichnung="x", anschaffung_am="2026-01-10",
                      ak_brutto_cent=200000, ust_satz_promille=190, nutzungsdauer_jahre=5,
                      aufteilungsquote=120)

    def test_derselbe_beleg_wird_nur_einmal_zum_anlagegut(self, conn, betrieb):
        """Zweimal erfasst zaehlte die AfA doppelt -- jedes Jahr der Nutzungsdauer."""
        beleg = _beleg(conn, quote=100)
        a.anlegen(conn, bezeichnung="Rechner", anschaffung_am="2026-01-10",
                  ak_brutto_cent=119000, ak_netto_cent=100000, nutzungsdauer_jahre=3,
                  beleg_id=beleg)
        with pytest.raises(a.AnlagenFehler, match="bereits als Anlagegut"):
            a.anlegen(conn, bezeichnung="Rechner nochmal", anschaffung_am="2026-01-10",
                      ak_brutto_cent=119000, ak_netto_cent=100000, nutzungsdauer_jahre=3,
                      beleg_id=beleg)
        # Und die Datenbank haelt es auch ohne die Logik.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO anlagegut (beleg_id, bezeichnung, anschaffung_am, ak_brutto_cent,"
                " ak_netto_cent, nutzungsdauer_jahre, erfasst_am)"
                " VALUES (?, 'x', '2026-01-10', 1, 1, 1, ?)", (beleg, db.jetzt_utc()),
            )


# ----------------------------------------------------------- Verzeichnisse

class TestVerzeichnis:
    @pytest.fixture
    def bestand(self, conn, betrieb):
        a.anlegen(conn, bezeichnung="Bürostuhl", anschaffung_am="2026-03-14",
                  ak_brutto_cent=90000, ust_satz_promille=190)
        a.anlegen(conn, bezeichnung="MacBook", anschaffung_am="2026-04-01",
                  ak_brutto_cent=285600, ust_satz_promille=190, ist_it=True)
        a.anlegen(conn, bezeichnung="Schreibtisch", anschaffung_am="2026-10-05",
                  ak_brutto_cent=178500, ust_satz_promille=190, nutzungsdauer_jahre=13)
        a.anlegen(conn, bezeichnung="Tastatur", anschaffung_am="2026-02-01",
                  ak_brutto_cent=11900, ust_satz_promille=190)
        return conn

    def test_afa_summe_je_kennzahl(self, bestand):
        summe = a.afa_summe(bestand, 2026)
        # KZ 132: Bürostuhl 900 + Tastatur 119 = 1019 €
        assert summe[a.KZ_GWG] == 90000 + 11900
        # KZ 130: MacBook voll + Schreibtisch 3 Zwölftel
        assert summe[a.KZ_BEWEGLICH] == 285600 + (178500 // 13) * 3 // 12

    def test_folgejahr_hat_nur_noch_den_schreibtisch(self, bestand):
        summe = a.afa_summe(bestand, 2027)
        assert a.KZ_GWG not in summe
        assert summe[a.KZ_BEWEGLICH] == 178500 // 13

    def test_gwg_verzeichnis_erst_ab_250_euro_netto(self, bestand):
        """§ 6 Abs. 2 Satz 4 EStG. Die Tastatur mit 100 € netto gehört nicht
        hinein, der Bürostuhl mit 756,30 € schon."""
        namen = [z["bezeichnung"] for z in a.gwg_verzeichnis(bestand, 2026)]
        assert namen == ["Bürostuhl"]

    def test_verzeichnis_zeigt_restwerte(self, bestand):
        eintraege = {e["bezeichnung"]: e for e in a.verzeichnis(bestand, 2026)}
        assert eintraege["Bürostuhl"]["restwert_cent"] == 0
        assert eintraege["MacBook"]["restwert_cent"] == 0
        assert eintraege["Schreibtisch"]["restwert_cent"] > 0

    def test_verzeichnis_ohne_jahr_zeigt_alles(self, bestand):
        assert len(a.verzeichnis(bestand)) == 4


# ------------------------------------------------------------ Oberfläche

class TestUeberDieOberflaeche:
    def test_seite_erklaert_die_asymmetrie(self, klient):
        """Beide Fundstellen und das Beispiel, an dem man es versteht."""
        text = " ".join(klient.get("/anlagen").text.split())
        assert "R 9b Abs. 2 EStR" in text
        assert "R 9b Abs. 1 EStR" in text
        assert "stets</strong> netto</strong> geprüft" in text \
            or "stets netto" in text.replace("<strong>", "").replace("</strong>", "")
        assert "952,00 € brutto" in text
        assert "756,30 € netto" in text

    def test_sammelposten_wird_begruendet_nicht_angeboten(self, klient):
        """Die Poolabschreibung senkt die Sofortabzugsgrenze für ALLE Güter des
        Jahres von 800 auf 250 € — eine Nebenwirkung, die niemand sieht."""
        text = klient.get("/anlagen").text
        assert "§ 6 Abs. 2a EStG" in text
        assert "einheitlich" in text

    def test_anlegen_und_einstufung(self, klient, datenbank):
        antwort = klient.post("/anlagen", data={
            "bezeichnung": "Bürostuhl", "anschaffung_am": "2026-03-14",
            "brutto_euro": "900,00", "ust_satz": "19",
        }, follow_redirects=True)
        assert "756,30 € netto" in antwort.text
        assert "§ 6 Abs. 2 EStG" in antwort.text
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM anlagegut").fetchone()
        assert zeile["methode"] == "GWG_SOFORT"

    def test_it_haken_wirkt(self, klient, datenbank):
        klient.post("/anlagen", data={
            "bezeichnung": "MacBook", "anschaffung_am": "2026-04-01",
            "brutto_euro": "2856,00", "ust_satz": "19", "ist_it": "1",
        }, follow_redirects=True)
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM anlagegut").fetchone()
        assert zeile["methode"] == "EIN_JAHR_IT"
        assert zeile["nutzungsdauer_jahre"] == 1

    def test_fehlende_nutzungsdauer_wird_gemeldet(self, klient, datenbank):
        antwort = klient.post("/anlagen", data={
            "bezeichnung": "Schreibtisch", "anschaffung_am": "2026-10-05",
            "brutto_euro": "1785,00", "ust_satz": "19",
        }, follow_redirects=True)
        assert "Nutzungsdauer" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    def test_verzeichnis_zeigt_die_afa_fuer_die_euer(self, klient):
        klient.post("/anlagen", data={
            "bezeichnung": "Schreibtisch", "anschaffung_am": "2026-10-05",
            "brutto_euro": "1785,00", "ust_satz": "19", "nutzungsdauer_jahre": "13",
        }, follow_redirects=True)
        text = klient.get("/anlagen", params={"jahr": 2026}).text
        assert "AfA 2026 für die EÜR" in text
        assert "130" in text

    def test_uebernehmen_bringt_netto_steuersatz_und_quote_mit(self, klient, datenbank):
        """Vorher kamen nur Brutto und Datum; das Formular rechnete mit 19 %.
        Bei einem Reverse-Charge-Beleg (0 %) war der Nettowert damit falsch --
        und an ihm haengt die Wertgrenze."""
        with datenbank() as conn:
            conn.execute(
                "INSERT INTO beleg (art, belegdatum, betrag_brutto_cent, betrag_netto_cent,"
                " betrag_ust_cent, ust_satz_promille, reverse_charge, aufteilungsquote,"
                " betrag_betrieblich_cent, ist_anlagegut, erfasst_am)"
                " VALUES ('EINGANG', '2026-02-02', 150000, 150000, 0, 0, 1, 80, 120000, 1, ?)",
                (db.jetzt_utc(),),
            )
            conn.commit()
        seite = klient.get("/anlagen").text
        assert "[name=netto_euro]').value='1.500,00'" in seite
        assert "[name=ust_satz]').value='0'" in seite
        assert "[name=aufteilungsquote]').value='80'" in seite
        assert "(80 % betrieblich)" in seite

    def test_abgang_zuruecknehmen_ueber_die_oberflaeche(self, klient, datenbank):
        klient.post("/anlagen", data={
            "bezeichnung": "Schreibtisch", "anschaffung_am": "2026-01-05",
            "brutto_euro": "1.200,00", "ust_satz": "19", "nutzungsdauer_jahre": "3",
        })
        with datenbank() as conn:
            gut = conn.execute("SELECT id FROM anlagegut").fetchone()["id"]
        klient.post(f"/anlagen/{gut}/abgang", data={"am": "2026-03-01", "grund": "vertippt"})
        assert "Abgang zurücknehmen" in klient.get("/anlagen").text
        antwort = klient.post(f"/anlagen/{gut}/abgang/zurueck",
                              data={"grund": "Datum vertippt"}, follow_redirects=True)
        assert "zurückgenommen" in antwort.text
        with datenbank() as conn:
            jahre = [z["jahr"] for z in conn.execute(
                "SELECT jahr FROM anlagegut_afa_jahr WHERE anlagegut_id = ? ORDER BY jahr",
                (gut,))]
        assert jahre == [2026, 2027, 2028]

    def test_abgang_nennt_die_folgen(self, klient, datenbank):
        """Das Abgangsdatum lag hier im naechsten Jahr; die Oberflaeche weist
        einen Abgang in der Zukunft inzwischen ab (Gegenprobe: Datumsfenster)."""
        klient.post("/anlagen", data={
            "bezeichnung": "MacBook", "anschaffung_am": "2026-04-01",
            "brutto_euro": "2856,00", "ust_satz": "19", "ist_it": "1",
        }, follow_redirects=True)
        with datenbank() as conn:
            gid = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        antwort = klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "2026-06-30", "grund": "verkauft", "erloes_euro": "300,00",
        }, follow_redirects=True)
        assert "KZ 102" in antwort.text
        assert "außer Ansatz" in antwort.text

    def test_offene_belege_werden_angemahnt(self, klient, datenbank):
        """Ein Beleg als Anlagegut markiert, aber ohne Eintrag: dann fehlt die
        AfA in der EÜR, und das fällt erst im Jahresabschluss auf."""
        with datenbank() as conn:
            conn.execute(
                "INSERT INTO beleg (belegdatum, betrag_brutto_cent,"
                " betrag_betrieblich_cent, ist_anlagegut, erfasst_am)"
                " VALUES ('2026-04-01', 285600, 285600, 1, ?)", (db.jetzt_utc(),)
            )
            conn.commit()
        text = klient.get("/anlagen").text
        assert "noch keinen Eintrag im Verzeichnis" in text
        assert "fällt erst im Jahresabschluss auf" in text

    def test_nav_fuehrt_hin(self, klient):
        assert 'href="/anlagen"' in klient.get("/").text

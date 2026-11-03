"""Regressionstests zu den Befunden der Fehleranalyse im Bereich Anlagen, EÜR,
Reisen und Eigenbelege.

Jeder Test hier steht für einen Befund, der im Werkzeug reproduziert wurde:
eine 500-Seite, wo eine Meldung hingehört; eine Pauschale, die nie in der EÜR
ankam; eine AfA-Reihe mit einer Zeile zu viel. Die Tests prüfen das
korrigierte Verhalten über denselben Weg, auf dem der Befund auftrat — meist
über die Oberfläche.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import anlagen as a
from app.logik import euer, kategorien, pauschalen
from app.logik.beleg import eigenbeleg as e
from app.logik.beleg.ergebnis import BelegFehler
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}

GUT = {
    "bezeichnung": "Schreibtisch", "anschaffung_am": "2026-01-05",
    "brutto_euro": "1.190,00", "ust_satz": "19", "nutzungsdauer_jahre": "3",
    "art": "BEWEGLICH", "aufteilungsquote": "100",
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


def _anlegen(klient, **felder):
    return klient.post("/anlagen", data={**GUT, **felder}, follow_redirects=True)


def _eigenbeleg(klient, **felder):
    daten = {
        "belegdatum": "2026-05-06", "brutto_euro": "50,00", "bezeichnung": "Büroklammern",
        "grund": "Kassenzettel auf Thermopapier verblasst, Büromarkt Altona",
        "quote": "100", "bezahlt_am": "",
        **felder,
    }
    klient.post("/belege/eigenbeleg", data=daten, follow_redirects=False)


def _beleg_in_kategorie(conn, schluessel, brutto, *, bezahlt="2026-06-02", anlagegut=0):
    kategorie = kategorien.nach_schluessel(conn, schluessel)["id"]
    bid = conn.execute(
        "INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,"
        " aufteilungsquote, betrag_betrieblich_cent, ist_anlagegut, geprueft_am, erfasst_am)"
        " VALUES ('EINGANG', ?, '2026-06-01', ?, 100, ?, ?, ?, ?)",
        (kategorie, brutto, brutto, anlagegut, db.jetzt_utc(), db.jetzt_utc()),
    ).lastrowid
    if bezahlt:
        conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                     (bid, bezahlt, -brutto))
    return bid


# ---------------------------------------------------- Anlagegut anlegen (1, 5, 14)

class TestAnlegenMeldungen:
    """Netto über Brutto, Netto 0, Steuersatz −19: früher ein IntegrityError
    aus dem Schema-CHECK und damit die 500-Seite — ein vertauschtes Feld
    reichte."""

    def test_netto_ueber_brutto_wird_gemeldet(self, klient, datenbank):
        antwort = _anlegen(klient, brutto_euro="900,00", netto_euro="1.000,00")
        assert antwort.status_code == 200
        assert "kann nicht über dem Bruttobetrag" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    def test_negatives_netto_wird_gemeldet(self, klient, datenbank):
        antwort = _anlegen(klient, brutto_euro="900,00", netto_euro="-10,00")
        assert "Nettobetrag" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    @pytest.mark.parametrize("satz", ["-19", "101"])
    def test_steuersatz_ausserhalb_null_bis_hundert(self, klient, datenbank, satz):
        antwort = _anlegen(klient, brutto_euro="900,00", netto_euro="", ust_satz=satz)
        assert "zwischen 0 und 100 Prozent" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    def test_netto_null_komma_null_ist_nicht_leer(self, klient, datenbank):
        """Wer 0,00 tippt, meint etwas. Still den gerechneten Wert zu nehmen
        versteckte den Tippfehler."""
        antwort = _anlegen(klient, brutto_euro="900,00", netto_euro="0,00")
        assert "0,00 €" in antwort.text and "leer" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    def test_quote_null_ist_kein_betriebsvermoegen(self, klient, datenbank):
        antwort = _anlegen(klient, aufteilungsquote="0")
        assert "0 % ist kein Betriebsvermögen" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    def test_anschaffung_in_der_zukunft_wird_gemeldet(self, klient, datenbank):
        antwort = _anlegen(klient, anschaffung_am="2099-01-01")
        assert "in der Zukunft" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    def test_anschaffung_vor_betriebsbeginn_gibt_den_hinweis(self, klient, datenbank):
        """Angelegt wird es — aber mit dem Hinweis auf die Einlage zum Teilwert
        (§ 6 Abs. 1 Nr. 5 EStG), der sonst erst im Bescheid auffiele."""
        with datenbank() as conn:
            conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
            conn.commit()
        antwort = _anlegen(klient, anschaffung_am="2019-03-01")
        assert "vor dem Betriebsbeginn" in antwort.text
        assert "§ 6 Abs. 1 Nr. 5 EStG" in antwort.text
        assert "01.01.2020" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 1

    def test_nach_betriebsbeginn_kein_hinweis(self, klient, datenbank):
        with datenbank() as conn:
            conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
            conn.commit()
        assert "Betriebsbeginn" not in _anlegen(klient).text

    def test_schema_check_wird_zur_meldung(self, conn, betrieb):
        """Das Netz unter den Prüfungen: was das Schema noch abweist, ist eine
        AnlagenFehler-Meldung und keine nackte IntegrityError."""
        with pytest.raises(a.AnlagenFehler, match="über dem Bruttobetrag"):
            a.anlegen(conn, bezeichnung="X", anschaffung_am="2026-01-01",
                      ak_brutto_cent=90000, ak_netto_cent=100000)
        with pytest.raises(a.AnlagenFehler):
            a.anlegen(conn, bezeichnung="X", anschaffung_am="2026-01-01",
                      ak_brutto_cent=90000, ak_netto_cent=-1000)


class TestAnlagegutAusBeleg:
    def test_unbekannter_beleg_wird_gemeldet(self, klient, datenbank):
        antwort = _anlegen(klient, beleg_id="999")
        assert "Diesen Beleg gibt es nicht." in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0

    def test_nicht_numerische_beleg_id_wird_gemeldet(self, klient, datenbank):
        antwort = _anlegen(klient, beleg_id="abc")
        assert "Diesen Beleg gibt es nicht." in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM anlagegut").fetchone()[0] == 0


# ------------------------------------------------------------ AfA-Reihe (3)

class TestAfaReiheAbJanuar:
    def test_januar_mit_rundungsrest_hat_genau_nd_zeilen(self):
        """1.190,00 € über 3 Jahre ab Januar: 36 Monate enden im Dezember des
        dritten Jahres (§ 7 Abs. 1 Satz 4 EStG). Vorher gab es eine vierte
        Zeile über 0,02 € mit null Monaten."""
        reihe = a.afa_reihe(ak_brutto_cent=119000, anschaffung_am="2026-01-01",
                            nutzungsdauer_jahre=3, methode=a.LINEAR, kennzahl=130)
        assert [(j.jahr, j.afa_cent) for j in reihe] == [
            (2026, 39666), (2027, 39666), (2028, 39668),
        ]
        assert reihe[-1].restwert_cent == 0
        assert all(j.anteilig_monate != 0 for j in reihe)

    @pytest.mark.parametrize("brutto,dauer", [(119000, 13), (238000, 3), (119000, 51), (777, 7)])
    def test_januar_allgemein(self, brutto, dauer):
        reihe = a.afa_reihe(ak_brutto_cent=brutto, anschaffung_am="2026-01-15",
                            nutzungsdauer_jahre=dauer, methode=a.LINEAR, kennzahl=130)
        assert [j.jahr for j in reihe] == list(range(2026, 2026 + dauer))
        assert sum(j.afa_cent for j in reihe) == brutto
        assert reihe[-1].restwert_cent == 0

    def test_nicht_januar_bleibt_wie_es_war(self):
        reihe = a.afa_reihe(ak_brutto_cent=120000, anschaffung_am="2026-06-15",
                            nutzungsdauer_jahre=3, methode=a.LINEAR, kennzahl=130)
        assert [(j.jahr, j.afa_cent) for j in reihe] == [
            (2026, 23333), (2027, 40000), (2028, 40000), (2029, 16667),
        ]

    def test_abgang_bei_januar_anschaffung_bleibt_zeitanteilig(self):
        reihe = a.afa_reihe(ak_brutto_cent=120000, anschaffung_am="2026-01-05",
                            nutzungsdauer_jahre=3, methode=a.LINEAR, kennzahl=130,
                            abgang_am="2027-06-30")
        assert [(j.jahr, j.afa_cent, j.restwert_cent) for j in reihe] == [
            (2026, 40000, 80000), (2027, 20000, 60000),
        ]

    def test_ueber_die_oberflaeche_kein_viertes_jahr(self, klient, datenbank):
        _anlegen(klient)
        with datenbank() as conn:
            jahre = [z[0] for z in conn.execute(
                "SELECT jahr FROM anlagegut_afa_jahr ORDER BY jahr")]
        assert jahre == [2026, 2027, 2028]
        assert "0,02 €" not in klient.get("/anlagen?jahr=2029").text


# ------------------------------------------------- Verzeichnis (9, 11, 12, 15)

class TestVerzeichnisseite:
    def test_jahresauswahl_kennt_die_afa_zwischenjahre(self, klient):
        _anlegen(klient)
        text = klient.get("/anlagen?jahr=2027").text
        assert "<h1>Anlagenverzeichnis 2027</h1>" in text
        assert '<option value="2027" selected>' in text
        assert '<option value="2028"' in text

    def test_angefragtes_jahr_steht_im_dropdown(self, klient):
        text = klient.get("/anlagen?jahr=2031").text
        assert '<option value="2031" selected>' in text

    def test_abgangsjahr_ist_waehlbar(self, klient, datenbank):
        """Um ein Jahr vorverlegt: der Abgang lag in der Zukunft, und die
        Oberflaeche weist den inzwischen ab (Gegenprobe: Datumsfenster)."""
        _anlegen(klient, anschaffung_am="2024-04-10", brutto_euro="1.200,00",
                 ust_satz="0")
        with datenbank() as conn:
            gid = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "2025-06-30", "grund": "verkauft", "erloes_euro": "100,00",
        }, follow_redirects=False)
        # Nach dem Abgang endet die Reihe 2025; 2025 bleibt wählbar.
        assert '<option value="2025"' in klient.get("/anlagen").text

    def test_restwert_nach_abgang_ist_null(self, klient, datenbank):
        """Um ein Jahr vorverlegt: der Abgang lag in der Zukunft, und die
        Oberflaeche weist den inzwischen ab (Gegenprobe: Datumsfenster)."""
        _anlegen(klient, bezeichnung="Monitor", anschaffung_am="2025-04-10",
                 brutto_euro="1.200,00", ust_satz="0")
        with datenbank() as conn:
            gid = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "2026-06-30", "grund": "verkauft", "erloes_euro": "100,00",
        }, follow_redirects=False)
        with datenbank() as conn:
            eintrag_2026 = a.verzeichnis(conn, 2026)[0]
            eintrag_2027 = a.verzeichnis(conn, 2027)[0]
            eintrag_2025 = a.verzeichnis(conn, 2025)[0]
        assert eintrag_2026["restwert_cent"] == 0
        assert eintrag_2026["restbuchwert_abgang_cent"] == 70000
        assert eintrag_2027["restwert_cent"] == 0
        assert eintrag_2025["restwert_cent"] == 90000
        text = klient.get("/anlagen?jahr=2026").text
        assert "Restbuchwert beim Abgang\n          700,00 €" in text

    def test_abgang_meldung_warnt_vor_der_rechnung(self, klient, datenbank):
        """Der Erlös steht mit dem Abgang in KZ 102. Eine Rechnung darüber
        zählte ihn doppelt und obendrein im § 19-Zähler."""
        _anlegen(klient)
        with datenbank() as conn:
            gid = conn.execute("SELECT id FROM anlagegut").fetchone()[0]
        antwort = klient.post(f"/anlagen/{gid}/abgang", data={
            # Vorher 2027-06-30; ein Abgang in der Zukunft wird jetzt abgewiesen.
            "am": "2026-06-30", "grund": "verkauft", "erloes_euro": "700,00",
        }, follow_redirects=True)
        assert "KEINE Rechnung im Werkzeug" in antwort.text
        assert "Belegordner" in antwort.text
        assert "Ausgangsbeleg" not in antwort.text
        assert "§ 19 Abs. 2 Satz 2 UStG" in antwort.text

    def test_ein_jahr_ohne_plural(self, klient):
        _anlegen(klient, nutzungsdauer_jahre="1", brutto_euro="1.190,00")
        text = klient.get("/anlagen").text
        assert "linear über 1\n          Jahr<" in text
        assert "1 Jahre" not in text
        assert "Anwendersoftware: 1 Jahr<" in text


# ------------------------------------- Beleg in der Anlagekategorie ohne Gut (10)

class TestAnlagekategorieOhneGut:
    def test_warnung_auf_euer_und_anlagen(self, klient, datenbank):
        with datenbank() as conn:
            _beleg_in_kategorie(conn, "HARDWARE_ANLAGE", 238000)
            conn.commit()
        text = klient.get("/euer?jahr=2026").text
        assert "1 bezahlter Beleg in der\n    Anlagekategorie ist nicht als Anlagegut" in text
        assert "KZ 130" in text
        text = klient.get("/anlagen").text
        assert "nicht als Anlagegut erfasst" in text
        assert 'href="/belege?kategorie=' in text

    def test_plural_und_betrag(self, klient, datenbank):
        with datenbank() as conn:
            _beleg_in_kategorie(conn, "HARDWARE_ANLAGE", 238000)
            _beleg_in_kategorie(conn, "HARDWARE_ANLAGE", 100000)
            conn.commit()
        text = klient.get("/euer?jahr=2026").text
        assert "2 bezahlte Belege in der\n    Anlagekategorie sind nicht" in text
        assert "3.380,00 €" in text

    def test_mit_anlagegut_keine_warnung(self, klient, datenbank):
        with datenbank() as conn:
            _beleg_in_kategorie(conn, "HARDWARE_ANLAGE", 238000, anlagegut=1)
            conn.commit()
        assert "nicht als Anlagegut" not in klient.get("/euer?jahr=2026").text
        assert "nicht als Anlagegut" not in klient.get("/anlagen").text

    def test_unbezahlt_keine_warnung(self, klient, datenbank):
        with datenbank() as conn:
            _beleg_in_kategorie(conn, "HARDWARE_ANLAGE", 238000, bezahlt=None)
            conn.commit()
        assert "nicht als Anlagegut" not in klient.get("/euer?jahr=2026").text

    def test_logik_liefert_anzahl_und_betrag(self, conn, betrieb):
        _beleg_in_kategorie(conn, "HARDWARE_ANLAGE", 238000)
        luecke = euer.anlagekategorie_ohne_gut(conn, 2026)
        assert luecke.anzahl == 1 and luecke.betrag_cent == 238000
        assert not euer.anlagekategorie_ohne_gut(conn, 2027)


# ------------------------------------------------------- EÜR-Seite (8, 16, 7)

class TestEuerSeite:
    def test_nachrichtliche_ausgabenzeile_ist_markiert(self, klient, datenbank):
        with datenbank() as conn:
            _beleg_in_kategorie(conn, "BEWIRTUNG", 10000, bezahlt="2026-07-01")
            conn.commit()
        text = klient.get("/euer?jahr=2026").text
        assert "nachrichtlich, zählt nicht in die Summe" in text
        ausgaben = text.split("<h2>Betriebsausgaben</h2>")[1]
        assert '<tr class="gedaempft">' in ausgaben

    def test_singular_bezahlter_beleg(self, klient, datenbank):
        _eigenbeleg(klient, bezahlt_am="2026-05-06")
        text = klient.get("/euer?jahr=2026").text
        assert "1 bezahlter Beleg ohne Kategorie" in text
        assert "1 bezahlte Beleg " not in text

    def test_plural_bezahlte_belege(self, klient, datenbank):
        _eigenbeleg(klient, bezahlt_am="2026-05-06")
        _eigenbeleg(klient, bezahlt_am="2026-05-07")
        assert "2 bezahlte\n    Belege ohne Kategorie" in klient.get("/euer?jahr=2026").text


class TestBuchungsblatt:
    def test_eigenbeleg_traegt_belegnummer_und_gegenstelle(self, conn, betrieb):
        kategorie = kategorien.nach_schluessel(conn, "BUEROBEDARF")["id"]
        bid = e.erfasse(conn, belegdatum="2026-05-06", brutto_cent=10000,
                        kategorie_id=kategorie, grund="Kassenzettel verblasst, Büromarkt",
                        bezahlt_am="2026-05-06")
        zeile = euer.buchungen(conn, 2026)[0]
        assert zeile["beleg"] == f"Beleg {bid}"
        assert zeile["gegenstelle"] == "Eigenbeleg"

    def test_zweite_zahlung_bringt_keine_zweite_betriebsausgabe(self, conn, betrieb):
        """Buchungsblatt und EÜR müssen dieselbe Summe nennen."""
        bid = _beleg_in_kategorie(conn, "BUEROBEDARF", 5000, bezahlt="2026-05-06")
        conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                     (bid, "2026-05-07", -5000))
        zeilen = euer.buchungen(conn, 2026)
        assert [z["betriebsausgabe_cent"] for z in zeilen] == [5000, 0]
        assert sum(z["betriebsausgabe_cent"] for z in zeilen) == sum(
            z.betrag_cent for z in euer.ausgaben(conn, 2026))

    def test_raten_bleiben_raten(self, conn, betrieb):
        bid = _beleg_in_kategorie(conn, "BUEROBEDARF", 10000, bezahlt="2026-05-06")
        conn.execute("UPDATE zahlung SET betrag_cent = -5000 WHERE beleg_id = ?", (bid,))
        conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                     (bid, "2026-06-06", -5000))
        assert [z["betriebsausgabe_cent"] for z in euer.buchungen(conn, 2026)] == [5000, 5000]


# ------------------------------------------------------------ Katalog (15)

class TestKategoriename:
    def test_datei_traegt_umlaut_und_eurozeichen(self):
        eintrag = next(k for k in kategorien.lies_datei()
                       if k["schluessel"] == "HARDWARE_ANLAGE")
        assert eintrag["bezeichnung"] == "Hardware & Einrichtung über 800 € netto"

    def test_bestehende_datenbank_bekommt_den_neuen_namen(self, tmp_path):
        """Der Abgleich beim Start aktualisiert vorhandene Kategorien anhand
        ihres Schlüssels — der alte Name verschwindet ohne Schema-Migration."""
        pfad = tmp_path / "alt.sqlite"
        conn = db.oeffne(pfad)
        conn.execute("UPDATE euer_kategorie SET bezeichnung = ?"
                     " WHERE schluessel = 'HARDWARE_ANLAGE'",
                     ("Hardware & Einrichtung ueber 800 EUR netto",))
        conn.commit()
        conn.close()
        conn = db.oeffne(pfad)
        name = conn.execute("SELECT bezeichnung FROM euer_kategorie"
                            " WHERE schluessel = 'HARDWARE_ANLAGE'").fetchone()[0]
        conn.close()
        assert name == "Hardware & Einrichtung über 800 € netto"


# ----------------------------------------- Pauschalen und Eigenbelege (2, 4, 6)

class TestPauschalenInDerEuer:
    """Eine Pauschale hat keinen Geldfluss, den man später „bezahlt“ setzen
    könnte: der Abfluss ist der Reisetag. Ohne Zahlungszeile hatte die ganze
    Reisen-Funktion null EÜR-Wirkung."""

    def _reise(self, klient, datenbank, **felder):
        klient.post("/reisen", data={
            "bezeichnung": "PyCon", "ziel": "Darmstadt", "abreise_am": "2026-04-20",
            "rueckkehr_am": "2026-04-22", "anlass": "Konferenz", "inland": "1",
            "uebernachtung": "1", **felder,
        }, follow_redirects=False)
        with datenbank() as conn:
            return conn.execute("SELECT id FROM reise ORDER BY id DESC").fetchone()[0]

    def test_verpflegung_zaehlt_im_reisejahr(self, klient, datenbank):
        rid = self._reise(klient, datenbank)
        antwort = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=True)
        assert "als Betriebsausgabe 2026 erfasst" in antwort.text
        with datenbank() as conn:
            zahlung = conn.execute("SELECT * FROM zahlung").fetchone()
            assert zahlung["datum"] == "2026-04-22"
            assert zahlung["betrag_cent"] == -5600
            ausgaben = euer.auswertung(conn, 2026).ausgaben
        assert [(z.kennzahl, z.betrag_cent) for z in ausgaben] == [(171, 5600)]

    def test_kilometer_zaehlen_am_fahrtag(self, klient, datenbank):
        rid = self._reise(klient, datenbank)
        antwort = klient.post(f"/reisen/{rid}/kilometer", data={
            "datum": "2026-04-20", "km": "250", "strecke": "HH-DA", "zweck": "Hin",
        }, follow_redirects=True)
        assert "als Betriebsausgabe 2026 erfasst" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT datum FROM zahlung").fetchone()[0] == "2026-04-20"
            assert euer.auswertung(conn, 2026).ausgaben_cent == 7500
            assert not euer.fehlende_zuordnung(conn, 2026)

    def test_kilometer_auf_unbekannte_reise(self, klient, datenbank):
        antwort = klient.post("/reisen/999/kilometer", data={
            "datum": "2026-04-20", "km": "250", "strecke": "HH-DA", "zweck": "Hin",
        }, follow_redirects=True)
        assert "Diese Reise gibt es nicht." in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_eigenbeleg_mit_bezahlt_am(self, klient, datenbank):
        """Angepasst (Schlussprobe): der Beleg bekommt eine Kategorie. Ohne sie
        schrieb der Test die alte, falsche Meldung „als Betriebsausgabe 2026
        erfasst“ fest -- ein Eigenbeleg ohne Kategorie steht in keiner
        EÜR-Zeile (siehe test_schluss_belege3)."""
        with datenbank() as conn:
            kategorie = kategorien.nach_schluessel(conn, "BUEROBEDARF")["id"]
        antwort = klient.post("/belege/eigenbeleg", data={
            "belegdatum": "2026-05-06", "brutto_euro": "50,00", "quote": "100",
            "grund": "Kassenzettel auf Thermopapier verblasst", "bezahlt_am": "2026-05-06",
            "kategorie_id": str(kategorie),
        }, follow_redirects=True)
        assert "als Betriebsausgabe 2026 erfasst" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT betrag_cent FROM zahlung").fetchone()[0] == -5000

    def test_eigenbeleg_ohne_bezahlt_am_bleibt_offen_und_wird_gezaehlt(self, klient, datenbank):
        _eigenbeleg(klient, bezahlt_am="")
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zahlung").fetchone()[0] == 0
            luecke = euer.fehlende_zuordnung(conn, 2026)
        assert luecke and luecke.eigenbelege_offen == 1
        assert luecke.eigenbelege_offen_cent == 5000
        assert luecke.anzahl == 0
        text = klient.get("/euer?jahr=2026").text
        assert "1 Eigenbeleg ohne Zahlungsdatum" in text

    def test_eigenbeleg_formular_belegt_bezahlt_am_vor(self, klient):
        """Vorbelegt mit dem Belegdatum, und ein Skript zieht es nach, wenn
        der Nutzer das Belegdatum ändert (solange er „Bezahlt am“ nicht selbst
        angefasst hat)."""
        text = klient.get("/belege/eigenbeleg").text
        assert 'name="bezahlt_am"' in text
        assert f'name="bezahlt_am" value="{db.heute()}"' in text
        assert f'name="belegdatum" required\n' \
               f'                            value="{db.heute()}"' in text
        assert "querySelector('[name=belegdatum]')" in text
        assert "querySelector('[name=bezahlt_am]')" in text

    def test_eigenbeleg_bezahlt_am_kein_datum(self, klient, datenbank):
        antwort = klient.post("/belege/eigenbeleg", data={
            "belegdatum": "2026-05-06", "brutto_euro": "50,00", "quote": "100",
            "grund": "Kassenzettel auf Thermopapier verblasst", "bezahlt_am": "morgen",
        }, follow_redirects=True)
        assert "ist kein Datum" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_luecke_ohne_jahr_zaehlt_alle_offenen_eigenbelege(self, conn, betrieb):
        e.erfasse(conn, belegdatum="2025-05-06", brutto_cent=1000,
                  grund="Parkautomat ohne Belegausgabe")
        e.erfasse(conn, belegdatum="2026-05-06", brutto_cent=2000,
                  grund="Parkautomat ohne Belegausgabe")
        assert euer.fehlende_zuordnung(conn).eigenbelege_offen == 2
        assert euer.fehlende_zuordnung(conn, 2026).eigenbelege_offen == 1
        assert euer.fehlende_zuordnung(conn, 2024).eigenbelege_offen == 0


class TestErsatzjahr:
    def _reise(self, conn, von, bis, **felder):
        return e.reise_anlegen(conn, bezeichnung="Konferenz", anlass="Vortrag",
                               ziel="Köln", abreise_am=von, rueckkehr_am=bis, **felder)

    @pytest.fixture
    def ohne_2026(self, monkeypatch, tmp_path):
        """Eine Pauschalen-Datei, in der 2026 noch fehlt.

        Vorher buchte der Test die Pauschale einer Reise im Jahr 2031 -- also
        einen Beleg und eine Zahlung in der Zukunft, die seit der gemeinsamen
        Datumspruefung (nicht nach heute) zu Recht abgewiesen werden. Das
        Ersatzjahr zeigt sich ebenso an einem vergangenen Jahr ohne Eintrag.
        """
        rohdaten = json.loads(pauschalen.DATEI.read_text(encoding="utf-8"))
        rohdaten["jahre"].pop("2026")
        datei = tmp_path / "pauschalen.json"
        datei.write_text(json.dumps(rohdaten), encoding="utf-8")
        monkeypatch.setattr(pauschalen, "DATEI", datei)

    def test_kuenftiges_jahr_nennt_das_ersatzjahr(self, conn, betrieb, ohne_2026):
        rid = self._reise(conn, "2026-07-01", "2026-07-03")
        bid = e.verpflegung_buchen(conn, rid)
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert "Für 2026 sind keine Pauschbeträge hinterlegt" in zeile["notiz"]
        assert str(max(pauschalen.jahre())) in zeile["notiz"]
        extraktion = json.loads(conn.execute(
            "SELECT bestaetigt_json FROM extraktion WHERE beleg_id = ?", (bid,)
        ).fetchone()[0])
        assert extraktion["pauschalen_aus_jahr"] == max(pauschalen.jahre())

    def test_jahr_vor_dem_aeltesten_wird_abgewiesen(self, conn, betrieb):
        """2019 galten 24/12 €; rückwärts geraten würde 28/14 € buchen."""
        rid = self._reise(conn, "2019-07-01", "2019-07-03")
        with pytest.raises(BelegFehler, match="Für 2019 sind keine Pauschbeträge hinterlegt"):
            e.verpflegung_buchen(conn, rid)
        with pytest.raises(BelegFehler, match="Für 2019 sind keine Pauschbeträge hinterlegt"):
            e.kilometer_buchen(conn, datum="2019-07-01", kilometer=100, zweck="Hin",
                               strecke="HH-K", reise_id=rid)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_pauschalen_fuer_raet_nicht_rueckwaerts_wenn_verlangt(self):
        with pytest.raises(pauschalen.KeinSatz, match="2019"):
            pauschalen.fuer(2019, rueckwaerts=False)
        assert pauschalen.fuer(2019).aus_jahr == min(pauschalen.jahre())
        assert "nicht buchen" in pauschalen.fuer(2019).hinweis

    def test_oberflaeche_nennt_das_ersatzjahr(self, klient, datenbank, ohne_2026):
        """Mit einer vergangenen Reise in einem Jahr ohne Eintrag statt einer Reise
        im Jahr 2031 (siehe ``ohne_2026``)."""
        klient.post("/reisen", data={
            "bezeichnung": "Ersatzjahr", "ziel": "Köln", "abreise_am": "2026-07-01",
            "rueckkehr_am": "2026-07-03", "anlass": "Vortrag", "inland": "1",
            "uebernachtung": "1",
        }, follow_redirects=False)
        with datenbank() as conn:
            rid = conn.execute("SELECT id FROM reise").fetchone()[0]
        # Die Liste warnt für das REISEJAHR, nicht nur für das laufende Jahr.
        assert "Für 2026 sind keine Pauschbeträge hinterlegt" in klient.get("/reisen").text
        antwort = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=True)
        assert "Für 2026 sind keine Pauschbeträge hinterlegt" in antwort.text

    def test_oberflaeche_weist_2019_ab(self, klient, datenbank):
        klient.post("/reisen", data={
            "bezeichnung": "Damals", "ziel": "Köln", "abreise_am": "2019-07-01",
            "rueckkehr_am": "2019-07-03", "anlass": "Vortrag", "inland": "1",
        }, follow_redirects=False)
        with datenbank() as conn:
            rid = conn.execute("SELECT id FROM reise").fetchone()[0]
        antwort = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=True)
        assert "Für 2019 sind keine Pauschbeträge hinterlegt" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0


# ------------------------------------------------------ Beleg bezahlt (7)

class TestBelegBezahlt:
    def _beleg(self, klient, datenbank):
        _eigenbeleg(klient, bezahlt_am="")
        with datenbank() as conn:
            return conn.execute("SELECT id FROM beleg").fetchone()[0]

    # TT.MM.JJJJ wird seit der Schlussprobe gelesen ("05.05.2026" ist ein
    # Datum); ein unmoeglicher Tag in dieser Schreibweise bleibt keins.
    @pytest.mark.parametrize("datum", ["2026-13-40", "31.02.2026", "morgen"])
    def test_kein_datum_wird_gemeldet(self, klient, datenbank, datum):
        bid = self._beleg(klient, datenbank)
        antwort = klient.post(f"/belege/{bid}/bezahlt", data={"datum": datum, "art": "BAR"},
                              follow_redirects=True)
        assert "ist kein Datum" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zahlung").fetchone()[0] == 0

    def test_zukunft_wird_gemeldet(self, klient, datenbank):
        bid = self._beleg(klient, datenbank)
        antwort = klient.post(f"/belege/{bid}/bezahlt", data={"datum": "2099-01-01"},
                              follow_redirects=True)
        assert "in der Zukunft" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zahlung").fetchone()[0] == 0

    def test_zweite_zahlung_wird_abgewiesen(self, klient, datenbank):
        bid = self._beleg(klient, datenbank)
        klient.post(f"/belege/{bid}/bezahlt", data={"datum": "2026-05-06", "art": "BAR"},
                    follow_redirects=False)
        antwort = klient.post(f"/belege/{bid}/bezahlt", data={"datum": "2026-05-07"},
                              follow_redirects=True)
        assert "bereits bezahlt (am 06.05.2026)" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zahlung").fetchone()[0] == 1

    def test_gueltiges_datum_bucht(self, klient, datenbank):
        bid = self._beleg(klient, datenbank)
        klient.post(f"/belege/{bid}/bezahlt", data={"datum": "2026-05-06", "art": "BAR"},
                    follow_redirects=False)
        with datenbank() as conn:
            zahlung = conn.execute("SELECT * FROM zahlung").fetchone()
        assert zahlung["datum"] == "2026-05-06" and zahlung["betrag_cent"] == -5000


# ------------------------------------------------------- Reisen (13)

class TestVerpflegungRegeln:
    def test_eintaegig_24_stunden_ist_ein_voller_tag(self):
        rechnung = pauschalen.verpflegung("2026-05-04", "2026-05-04", abwesend_stunden=24)
        assert (rechnung.volle_tage, rechnung.teiltage) == (1, 0)
        assert rechnung.betrag_cent == 2800

    @pytest.mark.parametrize("stunden", [25, -1])
    def test_stunden_ausserhalb_werden_abgewiesen(self, stunden):
        with pytest.raises(ValueError, match="zwischen 0 und 24"):
            pauschalen.verpflegung("2026-05-04", "2026-05-04", abwesend_stunden=stunden)

    def test_zweitaegig_ohne_uebernachtung_ist_teiltagregel(self):
        """§ 9 Abs. 4a Satz 3 Nr. 3 EStG: über Mitternacht ohne Übernachtung
        gibt es 14 € nur bei mehr als acht Stunden — keine An-/Abreisepauschale."""
        kurz = pauschalen.verpflegung("2026-06-10", "2026-06-11",
                                      abwesend_stunden=3, uebernachtung=False)
        assert kurz.betrag_cent == 0
        lang = pauschalen.verpflegung("2026-06-10", "2026-06-11",
                                      abwesend_stunden=10, uebernachtung=False)
        assert (lang.volle_tage, lang.teiltage) == (0, 1)
        assert lang.betrag_cent == 1400

    def test_zweitaegig_mit_uebernachtung_bleibt_bei_28(self):
        rechnung = pauschalen.verpflegung("2026-06-10", "2026-06-11", uebernachtung=True)
        assert rechnung.betrag_cent == 2800


class TestReiseMitUebernachtung:
    def test_spalte_ist_da_und_vorbelegt(self, conn):
        spalten = {z["name"] for z in conn.execute("PRAGMA table_info(reise)")}
        assert "uebernachtung" in spalten
        rid = e.reise_anlegen(conn, bezeichnung="X", anlass="Y", ziel="Z",
                              abreise_am="2026-06-10", rueckkehr_am="2026-06-11")
        assert conn.execute("SELECT uebernachtung FROM reise WHERE id = ?",
                            (rid,)).fetchone()[0] == 1
        assert e.lies_reise(conn, rid).uebernachtung is True

    def test_migration_traegt_die_spalte_nach(self, tmp_path):
        """Eine Datenbank aus Version 14 hat die Tabelle reise ohne die Spalte."""
        pfad = tmp_path / "alt.sqlite"
        conn = db.oeffne(pfad)
        conn.close()
        roh = sqlite3.connect(pfad)
        roh.executescript("""
            PRAGMA foreign_keys = OFF;
            CREATE TABLE reise_alt (
                id INTEGER PRIMARY KEY, bezeichnung TEXT NOT NULL, anlass TEXT NOT NULL,
                ziel TEXT NOT NULL, inland INTEGER NOT NULL DEFAULT 1,
                abreise_am TEXT NOT NULL, rueckkehr_am TEXT NOT NULL,
                abwesend_stunden INTEGER, notiz TEXT, erfasst_am TEXT NOT NULL);
            DROP TABLE reise;
            ALTER TABLE reise_alt RENAME TO reise;
            DELETE FROM schema_version;
            INSERT INTO schema_version (version, migriert_am) VALUES (14, '2026-01-01');
        """)
        roh.commit()
        roh.close()
        conn = db.oeffne(pfad)
        spalten = {z["name"] for z in conn.execute("PRAGMA table_info(reise)")}
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        conn.close()
        assert "uebernachtung" in spalten
        assert version == db.SCHEMA_VERSION >= 15

    def test_ohne_uebernachtung_ueber_die_oberflaeche(self, klient, datenbank):
        klient.post("/reisen", data={
            "bezeichnung": "Nachtfahrt", "ziel": "Kiel", "abreise_am": "2026-06-10",
            "rueckkehr_am": "2026-06-11", "anlass": "Kundentermin", "inland": "1",
            "abwesend_stunden": "3", "uebernachtung": "0",
        }, follow_redirects=False)
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM reise").fetchone()
        assert zeile["uebernachtung"] == 0
        antwort = klient.post(f"/reisen/{zeile['id']}/verpflegung", follow_redirects=True)
        assert "keine Verpflegungspauschale" in antwort.text
        assert "Übernachtung" in antwort.text

    def test_eintaegig_24_stunden_ueber_die_oberflaeche(self, klient, datenbank):
        klient.post("/reisen", data={
            "bezeichnung": "Langer Tag", "ziel": "Kiel", "abreise_am": "2026-05-04",
            "rueckkehr_am": "2026-05-04", "anlass": "Kundentermin", "inland": "1",
            "abwesend_stunden": "24",
        }, follow_redirects=False)
        with datenbank() as conn:
            rid = conn.execute("SELECT id FROM reise").fetchone()[0]
        antwort = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=True)
        assert "Verpflegungspauschale 28,00 €" in antwort.text

    @pytest.mark.parametrize("stunden", ["25", "-1"])
    def test_stunden_ausserhalb_ueber_die_oberflaeche(self, klient, datenbank, stunden):
        antwort = klient.post("/reisen", data={
            "bezeichnung": "X", "ziel": "Kiel", "abreise_am": "2026-05-04",
            "rueckkehr_am": "2026-05-04", "anlass": "Kundentermin", "inland": "1",
            "abwesend_stunden": stunden,
        }, follow_redirects=True)
        assert "zwischen 0 und 24" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM reise").fetchone()[0] == 0

    def test_formular_hat_die_checkbox(self, klient):
        text = klient.get("/reisen").text
        assert 'name="uebernachtung"' in text
        assert "Übernachtung außerhalb der Wohnung" in text

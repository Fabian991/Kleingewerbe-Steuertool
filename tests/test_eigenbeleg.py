"""Eigenbelege, Pauschbeträge und die Geschäftsreise als Container.

Zwei Kategorien des eigenen Katalogs kommen **nie** mit einem Fremdbeleg: die
Verpflegungspauschale, weil abziehbar die Pauschbeträge nach § 9 Abs. 4a EStG
sind und nicht die Essensbelege, und das Kilometergeld, weil der Nachweis eine
Aufstellung ist. Ohne diesen Weg wären sie im Werkzeug nicht benutzbar.

Der wichtigste Test ist hier nicht die Rechnung, sondern dass ein Eigenbeleg
**ohne Begründung abgewiesen** wird -- auf zwei Wegen, damit eine migrierte
Datenbank sich wie eine neue verhält.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import kategorien, pauschalen
from app.logik.beleg import eigenbeleg as e
from app.logik.beleg.ergebnis import BelegFehler
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


@pytest.fixture
def reise(conn):
    return e.reise_anlegen(
        conn, bezeichnung="PyCon DE 2026", anlass="Fachkonferenz, eigener Vortrag",
        ziel="Darmstadt", abreise_am="2026-04-20", rueckkehr_am="2026-04-23",
    )


# ----------------------------------------------------------- Pauschbeträge

class TestPauschalen:
    def test_datei_ist_lesbar(self):
        assert 2026 in pauschalen.jahre()

    def test_inlandssaetze(self):
        """§ 9 Abs. 4a EStG: 28 € für 24 Stunden, 14 € für mehr als 8 Stunden."""
        satz = pauschalen.fuer(2026)
        assert satz["verpflegung_inland_vollertag_cent"] == 2800
        assert satz["verpflegung_inland_teiltag_cent"] == 1400
        assert satz["kilometergeld_pkw_cent"] == 30
        assert not satz.veraltet

    def test_unbekanntes_jahr_sagt_es(self):
        """Stillschweigend die Vorjahreswerte zu nehmen wäre der gefährlichere
        Weg: die Zahl wäre dann falsch und unauffällig."""
        satz = pauschalen.fuer(2099)
        assert satz.veraltet
        assert satz.aus_jahr == max(pauschalen.jahre())
        assert "2099" in satz.hinweis
        assert "pauschalen.json" in satz.hinweis

    def test_jahr_vor_allen_bekannten(self):
        satz = pauschalen.fuer(2000)
        assert satz.veraltet
        assert satz.aus_jahr == min(pauschalen.jahre())


class TestVerpflegung:
    def test_mehrtaegig(self):
        """20. bis 23. April sind vier Tage: An- und Abreisetag je 14 €,
        die zwei Tage dazwischen je 28 €."""
        rechnung = pauschalen.verpflegung("2026-04-20", "2026-04-23")
        assert (rechnung.volle_tage, rechnung.teiltage) == (2, 2)
        assert rechnung.betrag_cent == 2 * 2800 + 2 * 1400
        assert rechnung.betrag_cent == 8400

    def test_zweitaegig_hat_keinen_vollen_tag(self):
        rechnung = pauschalen.verpflegung("2026-04-20", "2026-04-21")
        assert (rechnung.volle_tage, rechnung.teiltage) == (0, 2)
        assert rechnung.betrag_cent == 2800

    def test_eintaegig_ueber_acht_stunden(self):
        rechnung = pauschalen.verpflegung("2026-04-20", "2026-04-20", abwesend_stunden=9)
        assert rechnung.betrag_cent == 1400

    def test_eintaegig_genau_acht_stunden_zaehlt_nicht(self):
        """Das Gesetz sagt 'mehr als acht Stunden'."""
        rechnung = pauschalen.verpflegung("2026-04-20", "2026-04-20", abwesend_stunden=8)
        assert rechnung.betrag_cent == 0

    def test_eintaegig_kurz(self):
        rechnung = pauschalen.verpflegung("2026-04-20", "2026-04-20", abwesend_stunden=4)
        assert rechnung.betrag_cent == 0

    def test_eintaegig_ohne_stundenangabe(self):
        """Ohne Angabe wird NICHTS angesetzt -- raten wäre hier eine erfundene
        Betriebsausgabe."""
        assert pauschalen.verpflegung("2026-04-20", "2026-04-20").betrag_cent == 0

    def test_rueckkehr_vor_abreise(self):
        with pytest.raises(ValueError):
            pauschalen.verpflegung("2026-04-23", "2026-04-20")

    def test_nachweis_ist_lesbar(self):
        rechnung = pauschalen.verpflegung("2026-04-20", "2026-04-23")
        assert "2 × 28,00 €" in rechnung.nachweis
        assert "2 × 14,00 €" in rechnung.nachweis

    def test_ueber_den_monatswechsel(self):
        rechnung = pauschalen.verpflegung("2026-04-29", "2026-05-02")
        assert (rechnung.volle_tage, rechnung.teiltage) == (2, 2)


class TestKilometergeld:
    def test_dreissig_cent(self):
        assert pauschalen.kilometergeld_cent(250, 2026) == 7500

    def test_null_kilometer(self):
        assert pauschalen.kilometergeld_cent(0, 2026) == 0

    def test_negative_kilometer(self):
        with pytest.raises(ValueError):
            pauschalen.kilometergeld_cent(-10, 2026)


# ------------------------------------------------------------- Eigenbeleg

class TestEigenbeleg:
    def test_erfassen(self, conn):
        kategorie = kategorien.nach_schluessel(conn, "REISE_FAHRTKOSTEN")["id"]
        bid = e.erfasse(
            conn, belegdatum="2026-03-14", brutto_cent=490, kategorie_id=kategorie,
            bezeichnung="Parkgebühr Kundentermin",
            grund="Parkautomat ohne Belegausgabe, Termin bei Mustermann GmbH",
        )
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert zeile["ist_eigenbeleg"] == 1
        assert zeile["betrag_brutto_cent"] == 490
        assert "Parkgebühr" in zeile["notiz"]
        assert "Parkautomat" in zeile["notiz"]

    def test_ohne_begruendung_wird_abgewiesen(self, conn):
        with pytest.raises(BelegFehler, match="Begründung"):
            e.erfasse(conn, belegdatum="2026-03-14", brutto_cent=490, grund="verloren")

    def test_datenbank_weist_es_ebenfalls_ab(self, conn):
        """Als Trigger und nicht als CHECK, damit eine migrierte Datenbank sich
        wie eine neue verhält -- ein CHECK lässt sich per ALTER nicht
        nachtragen."""
        with pytest.raises(sqlite3.IntegrityError, match="Eigenbeleg"):
            conn.execute(
                "INSERT INTO beleg (belegdatum, betrag_brutto_cent,"
                " betrag_betrieblich_cent, ist_eigenbeleg, notiz, erfasst_am)"
                " VALUES ('2026-03-14', 490, 490, 1, 'kurz', ?)", (db.jetzt_utc(),)
            )

    def test_begruendung_kann_nicht_nachtraeglich_entfernt_werden(self, conn):
        bid = e.erfasse(conn, belegdatum="2026-03-14", brutto_cent=490,
                        grund="Parkautomat ohne Belegausgabe")
        with pytest.raises(sqlite3.IntegrityError, match="Eigenbeleg"):
            conn.execute("UPDATE beleg SET notiz = 'weg' WHERE id = ?", (bid,))

    def test_nullbetrag_wird_abgewiesen(self, conn):
        """Angepasst (Schlussprobe): der Test hielt den alten Wortlaut „über null
        Euro ist kein Beleg“ fest, der bei negativen Beträgen schief war."""
        with pytest.raises(BelegFehler, match="braucht einen Betrag über 0 €"):
            e.erfasse(conn, belegdatum="2026-03-14", brutto_cent=0,
                      grund="Parkautomat ohne Belegausgabe")

    def test_ungueltiges_datum_wird_abgewiesen(self, conn):
        with pytest.raises(BelegFehler, match="Belegdatum"):
            e.erfasse(conn, belegdatum="irgendwann", brutto_cent=490,
                      grund="Parkautomat ohne Belegausgabe")

    def test_quote_wirkt(self, conn):
        bid = e.erfasse(conn, belegdatum="2026-03-14", brutto_cent=1000, quote=70,
                        grund="Parkautomat ohne Belegausgabe")
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert zeile["betrag_betrieblich_cent"] == 700

    def test_netto_gleich_brutto(self, conn):
        """Eine Steuer, die nicht erhoben wurde, kann man nicht herausrechnen --
        bei einer Pauschale gibt es ohnehin keine."""
        bid = e.erfasse(conn, belegdatum="2026-03-14", brutto_cent=490,
                        grund="Parkautomat ohne Belegausgabe")
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert zeile["netto_fuer_grenzwert_cent"] == 490
        assert zeile["betrag_netto_cent"] is None

    def test_berechnung_wird_archiviert(self, conn):
        """Bei einem Eigenbeleg ist die Berechnung das Einzige, was es gibt."""
        import json

        bid = e.erfasse(conn, belegdatum="2026-03-14", brutto_cent=490,
                        grund="Parkautomat ohne Belegausgabe",
                        berechnung={"fundstelle": "§ 9 Abs. 4a EStG"})
        zeile = conn.execute("SELECT * FROM extraktion WHERE beleg_id = ?",
                             (bid,)).fetchone()
        assert zeile["quelle"] == "manuell"
        bestaetigt = json.loads(zeile["bestaetigt_json"])
        assert bestaetigt["brutto"] == 490
        assert bestaetigt["fundstelle"] == "§ 9 Abs. 4a EStG"

    def test_journal_haelt_die_begruendung(self, conn):
        e.erfasse(conn, belegdatum="2026-03-14", brutto_cent=490,
                  grund="Parkautomat ohne Belegausgabe")
        zeile = conn.execute(
            "SELECT * FROM journal WHERE aktion = 'EIGENBELEG'").fetchone()
        assert zeile["begruendung"] == "Parkautomat ohne Belegausgabe"


# ----------------------------------------------------------------- Reise

class TestReise:
    def test_anlegen(self, conn, reise):
        gelesen = e.lies_reise(conn, reise)
        assert gelesen.ziel == "Darmstadt"
        assert gelesen.tage == 4

    @pytest.mark.parametrize("fehlt", ["bezeichnung", "anlass", "ziel"])
    def test_pflichtfelder(self, conn, fehlt):
        felder = {"bezeichnung": "X", "anlass": "Y", "ziel": "Z",
                  "abreise_am": "2026-04-20", "rueckkehr_am": "2026-04-23"}
        felder[fehlt] = "  "
        with pytest.raises(BelegFehler):
            e.reise_anlegen(conn, **felder)

    def test_rueckkehr_vor_abreise(self, conn):
        with pytest.raises(BelegFehler, match="Rückkehr"):
            e.reise_anlegen(conn, bezeichnung="X", anlass="Y", ziel="Z",
                            abreise_am="2026-04-23", rueckkehr_am="2026-04-20")

    def test_datenbank_haelt_die_reihenfolge(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reise (bezeichnung, anlass, ziel, abreise_am,"
                " rueckkehr_am, erfasst_am) VALUES ('X','Y','Z','2026-04-23',"
                " '2026-04-20', ?)", (db.jetzt_utc(),)
            )

    def test_verpflegung_buchen(self, conn, reise):
        bid = e.verpflegung_buchen(conn, reise)
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert zeile["betrag_brutto_cent"] == 8400
        assert zeile["ist_eigenbeleg"] == 1
        assert zeile["reise_id"] == reise
        assert "§ 9 Abs. 4a EStG" in zeile["notiz"]
        assert "2 × 28,00 €" in zeile["notiz"]
        kategorie = conn.execute(
            "SELECT schluessel FROM euer_kategorie WHERE id = ?",
            (zeile["kategorie_id"],)).fetchone()
        assert kategorie["schluessel"] == "REISE_VERPFLEGUNG"

    def test_verpflegung_nur_einmal(self, conn, reise):
        e.verpflegung_buchen(conn, reise)
        with pytest.raises(BelegFehler, match="schon als Beleg"):
            e.verpflegung_buchen(conn, reise)

    def test_verpflegung_fuer_auslandsreise_abgewiesen(self, conn):
        """Die Auslandssätze setzt das BMF jedes Jahr je Land neu fest -- ein
        veralteter Satz sieht aus wie ein geprüfter."""
        rid = e.reise_anlegen(conn, bezeichnung="Konferenz Wien", anlass="Vortrag",
                              ziel="Wien", abreise_am="2026-05-04",
                              rueckkehr_am="2026-05-06", inland=False)
        with pytest.raises(BelegFehler, match="Auslandsreisen"):
            e.verpflegung_buchen(conn, rid)

    def test_verpflegung_ohne_anspruch_abgewiesen(self, conn):
        rid = e.reise_anlegen(conn, bezeichnung="Kurzbesuch", anlass="Termin",
                              ziel="Lübeck", abreise_am="2026-05-04",
                              rueckkehr_am="2026-05-04", abwesend_stunden=5)
        with pytest.raises(BelegFehler, match="acht Stunden"):
            e.verpflegung_buchen(conn, rid)

    def test_kilometer_buchen(self, conn, reise):
        bid = e.kilometer_buchen(conn, datum="2026-04-20", kilometer=500,
                                 strecke="Hamburg – Darmstadt und zurück",
                                 zweck="Konferenzbesuch", reise_id=reise)
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?", (bid,)).fetchone()
        assert zeile["betrag_brutto_cent"] == 15000
        assert "500 km" in zeile["notiz"]
        assert "Hamburg – Darmstadt" in zeile["notiz"]

    def test_kilometer_braucht_strecke_und_zweck(self, conn):
        with pytest.raises(BelegFehler, match="Strecke und Zweck"):
            e.kilometer_buchen(conn, datum="2026-04-20", kilometer=500,
                               strecke="", zweck="Konferenz")

    def test_eine_reise_mehrere_kennzahlen(self, conn, reise):
        """Der einzige Vorgang, der mehrere Kennzahlen braucht."""
        e.verpflegung_buchen(conn, reise)
        e.kilometer_buchen(conn, datum="2026-04-20", kilometer=500,
                           strecke="Hamburg – Darmstadt", zweck="Konferenz",
                           reise_id=reise)
        hotel = kategorien.nach_schluessel(conn, "REISE_UEBERNACHTUNG")["id"]
        e.erfasse(conn, belegdatum="2026-04-23", brutto_cent=38500,
                  kategorie_id=hotel, reise_id=reise,
                  grund="Hotelrechnung auf Thermopapier verblasst, Buchungsmail liegt vor")

        uebersicht = e.reisen(conn)[0]
        assert uebersicht["belege"] == 3
        assert uebersicht["kennzahlen"] == 3
        assert uebersicht["summe_cent"] == 8400 + 15000 + 38500
        kennzahlen = [b["kennzahl"] for b in e.reisebelege(conn, reise)]
        assert kennzahlen == [147, 171, 221]


# ------------------------------------------------------------ Oberfläche

class TestUeberDieOberflaeche:
    def test_formular_nennt_die_saetze(self, klient):
        text = klient.get("/belege/eigenbeleg").text
        assert "§ 9 Abs. 4a EStG" in text
        assert "28,00" in text and "14,00" in text
        assert "30 Cent je Kilometer" in text

    def test_formular_warnt_vor_der_uebernachtungspauschale(self, klient):
        """Die 20 € gelten für den Arbeitgeberersatz an Arbeitnehmer, nicht für
        einen Selbstständigen."""
        text = klient.get("/belege/eigenbeleg").text
        assert "Übernachtungspauschale" in text
        assert "tatsächlichen Beleg" in text

    def test_eigenbeleg_erfassen(self, klient, datenbank):
        antwort = klient.post("/belege/eigenbeleg", data={
            "belegdatum": "2026-03-14", "brutto_euro": "4,90",
            "bezeichnung": "Parkgebühr", "quote": "100",
            "grund": "Parkautomat ohne Belegausgabe, Termin bei Mustermann GmbH",
        }, follow_redirects=True)
        assert "erfasst" in antwort.text
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM beleg").fetchone()
        assert zeile["ist_eigenbeleg"] == 1
        assert zeile["betrag_brutto_cent"] == 490

    def test_kurze_begruendung_wird_abgewiesen(self, klient, datenbank):
        antwort = klient.post("/belege/eigenbeleg", data={
            "belegdatum": "2026-03-14", "brutto_euro": "4,90", "grund": "weg",
        }, follow_redirects=True)
        assert "Begründung" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_reise_anlegen_und_pauschale_buchen(self, klient, datenbank):
        klient.post("/reisen", data={
            "bezeichnung": "PyCon DE 2026", "anlass": "Fachkonferenz, eigener Vortrag",
            "ziel": "Darmstadt", "abreise_am": "2026-04-20",
            "rueckkehr_am": "2026-04-23", "inland": "1",
        }, follow_redirects=True)
        with datenbank() as conn:
            rid = conn.execute("SELECT id FROM reise").fetchone()[0]

        text = klient.get("/reisen").text
        assert "PyCon DE 2026" in text
        assert "Fachkonferenz" in text

        antwort = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=True)
        assert "84,00 €" in antwort.text
        assert "Frühstück" in antwort.text
        with datenbank() as conn:
            assert conn.execute(
                "SELECT betrag_brutto_cent FROM beleg").fetchone()[0] == 8400

    def test_reise_ohne_anlass_wird_abgewiesen(self, klient, datenbank):
        antwort = klient.post("/reisen", data={
            "bezeichnung": "X", "anlass": "", "ziel": "Y",
            "abreise_am": "2026-04-20", "rueckkehr_am": "2026-04-23",
        }, follow_redirects=True)
        assert "Anlass" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM reise").fetchone()[0] == 0

    def test_kilometer_ueber_die_oberflaeche(self, klient, datenbank):
        klient.post("/reisen", data={
            "bezeichnung": "Kundentermin", "anlass": "Projektabstimmung",
            "ziel": "Berlin", "abreise_am": "2026-04-20",
            "rueckkehr_am": "2026-04-20", "abwesend_stunden": "11",
        }, follow_redirects=True)
        with datenbank() as conn:
            rid = conn.execute("SELECT id FROM reise").fetchone()[0]
        antwort = klient.post(f"/reisen/{rid}/kilometer", data={
            "datum": "2026-04-20", "km": "580",
            "strecke": "Hamburg – Berlin und zurück", "zweck": "Projektabstimmung",
        }, follow_redirects=True)
        assert "erfasst" in antwort.text
        with datenbank() as conn:
            zeile = conn.execute("SELECT * FROM beleg").fetchone()
        assert zeile["betrag_brutto_cent"] == 17400

    def test_fremdbeleg_laesst_sich_einer_reise_zuordnen(self, klient, datenbank):
        klient.post("/reisen", data={
            "bezeichnung": "PyCon DE 2026", "anlass": "Fachkonferenz",
            "ziel": "Darmstadt", "abreise_am": "2026-04-20",
            "rueckkehr_am": "2026-04-23",
        }, follow_redirects=True)
        with datenbank() as conn:
            rid = conn.execute("SELECT id FROM reise").fetchone()[0]

        pdf = Path(__file__).resolve().parent / "fixtures" / "lieferantenrechnung_de.pdf"
        ort = klient.post("/belege/hochladen", files={
            "datei": ("hotel.pdf", pdf.read_bytes(), "application/pdf")},
            follow_redirects=False).headers["location"]
        name = ort.rsplit("/", 1)[1]
        assert "Gehört zu einer Reise" in klient.get(f"/belege/pruefen/{name}").text
        klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-04-23", "brutto_euro": "385,00", "quote": "100",
            "reise_id": str(rid),
        }, follow_redirects=True)
        with datenbank() as conn:
            assert conn.execute("SELECT reise_id FROM beleg").fetchone()[0] == rid

    def test_nav_fuehrt_zu_den_reisen(self, klient):
        assert 'href="/reisen"' in klient.get("/belege").text

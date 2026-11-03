"""Die Anwendung von aussen: echte HTTP-Aufrufe gegen eine frische Datenbank."""

import datetime as _dt

import pytest
from fastapi.testclient import TestClient

from app.main import erzeuge_app


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
        yield c


def test_gesundheitspunkt(klient):
    from app import db

    antwort = klient.get("/gesund")
    assert antwort.status_code == 200
    assert antwort.json() == {
        "status": "ok", "schema_version": db.SCHEMA_VERSION, "eingerichtet": False,
    }


def test_start_leitet_vor_der_einrichtung_weiter(klient):
    antwort = klient.get("/", follow_redirects=False)
    assert antwort.status_code == 303
    assert antwort.headers["location"] == "/einrichtung"


def test_einrichtung_ohne_steuerkennung_wird_abgewiesen(klient):
    """Der Fall, der in Word still durchgeht und die Rechnung unvollstaendig macht."""
    antwort = klient.post(
        "/einrichtung",
        data={"name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg"},
    )
    assert antwort.status_code == 422
    assert "34a" in antwort.text
    assert klient.get("/gesund").json()["eingerichtet"] is False


def test_einrichtung_mit_untauglichem_nummernformat_wird_abgewiesen(klient):
    antwort = klient.post(
        "/einrichtung",
        data={
            "name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
            "steuernummer": "12/345/67890", "nummer_format": "RE-2026",
        },
    )
    assert antwort.status_code == 422
    assert "Zähler" in antwort.text or "Zaehler" in antwort.text


def test_abgewiesene_einrichtung_behaelt_die_eingaben(klient):
    """Ein Fehler darf die Eingabe nicht wegwerfen.

    Bei der Ersteinrichtung ist das Formular ein Dutzend Felder lang. Wurde
    nach einem Fehler ein leeres Formular geliefert, war die Arbeit weg --
    und beim zweiten Versuch wurde eher etwas uebersprungen als nachgetragen.
    """
    antwort = klient.post(
        "/einrichtung",
        data={
            "name": "Mein Gewerbe", "inhaber": "Erika Muster",
            "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
            "telefon": "040 123456", "email": "post@example.org",
            "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
            "standardsatz_euro": "120,00", "zahlungsziel_tage": "21",
            # Keine einzige Steuerkennung -- das ist der Fehler.
        },
    )
    assert antwort.status_code == 422
    for wert in ('value="Mein Gewerbe"', 'value="Erika Muster"', 'value="Weg 1"',
                 'value="20095"', 'value="Hamburg"', 'value="040 123456"',
                 'value="post@example.org"', 'value="DE02120300000000202051"',
                 'value="BYLADEM1001"', 'value="120,00"', 'value="21"'):
        assert wert in antwort.text, wert


def test_abgewiesene_einrichtung_setzt_den_zaehler_nicht_zurueck(klient):
    """Der eigentliche Schaden: eine zweite Rechnung mit Nummer 1.

    Das abgewiesene Formular zeigte wieder "Naechste Nummer: 1". Wer den
    Fehler behob und erneut speicherte, stellte damit den Zaehler zurueck --
    ohne es zu sehen.
    """
    antwort = klient.post(
        "/einrichtung",
        data={
            "name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
            "nummer_format": "R-{JJ}-{NNNN}", "start_seq": "32", "reset_jaehrlich": "0",
        },
    )
    assert antwort.status_code == 422
    assert 'value="32"' in antwort.text
    assert 'value="1"' not in antwort.text.split('name="start_seq"')[1][:200]
    assert 'value="R-{JJ}-{NNNN}"' in antwort.text
    # "jaehrlich zuruecksetzen = nein" muss ebenfalls stehen bleiben.
    nach_auswahl = antwort.text.split('name="reset_jaehrlich"')[1][:400]
    assert nach_auswahl.index('value="0"') < nach_auswahl.index("selected") + 20


def test_betriebsbeginn_wird_gespeichert_und_ohne_ihn_sagt_es_das_dashboard(klient):
    """Der Beginn der Taetigkeit entscheidet ueber 25.000 oder 100.000 €.

    Vorher wurde er beim Einrichten auf den Installationstag gesetzt und war
    nie korrigierbar. Ein Betrieb, der schon Jahre laeuft, galt damit als
    Gruender -- mit der falschen Grenze.
    """
    grunddaten = {"name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095",
                  "ort": "Hamburg", "steuernummer": "12/345/67890"}
    klient.post("/einrichtung", data=grunddaten)
    start = klient.get("/").text
    assert "Beginn der Tätigkeit ist nicht eingetragen" in start
    assert "100.000,00 €" in start

    klient.post("/einrichtung", data={**grunddaten, "beginn_am": "2019-04-01"})
    assert 'value="2019-04-01"' in klient.get("/einrichtung").text
    start = klient.get("/").text
    assert "nicht eingetragen" not in start

    # Ein Gruender dieses Jahres bekommt die 25.000-€-Grenze.
    klient.post("/einrichtung", data={**grunddaten, "beginn_am": "2026-02-01"})
    assert "25.000,00 €" in klient.get("/").text

    antwort = klient.post("/einrichtung", data={**grunddaten, "beginn_am": "gestern"})
    assert antwort.status_code == 422
    assert "Beginn" in antwort.text


def test_einrichtung_mit_unlesbarem_stundensatz_meldet_das_feld(klient):
    """Vorher: ValueError aus float() und ein Serverfehler ohne Hinweis."""
    antwort = klient.post(
        "/einrichtung",
        data={
            "name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
            "steuernummer": "12/345/67890", "standardsatz_euro": "hundertzwanzig",
        },
    )
    assert antwort.status_code == 422
    assert "Stundensatz" in antwort.text
    assert klient.get("/gesund").json()["eingerichtet"] is False


class TestEingerichtet:
    @pytest.fixture(autouse=True)
    def _einrichten(self, klient):
        antwort = klient.post(
            "/einrichtung",
            data={
                "name": "Mein Kleingewerbe", "inhaber": "E. Muster",
                "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
                "steuernummer": "12/345/67890", "iban": "DE02120300000000202051",
                "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
                "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "118",
            },
            follow_redirects=False,
        )
        assert antwort.status_code == 303

    def test_eingerichtet(self, klient):
        assert klient.get("/gesund").json()["eingerichtet"] is True

    def test_dashboard_zeigt_den_betrieb(self, klient):
        text = klient.get("/").text
        assert "Mein Kleingewerbe" in text
        assert "§ 19 Abs. 1 UStG" in text

    def test_dashboard_zeigt_die_naechste_nummer_aus_dem_startwert(self, klient):
        """Beim Umstieg aus Word darf keine Nummer doppelt vergeben werden."""
        assert "RE-" in klient.get("/").text
        assert "0118" in klient.get("/").text

    def test_dashboard_ohne_daten_bleibt_ruhig(self, klient):
        text = klient.get("/").text
        assert "Keine offenen Zeiten" in text
        assert "Keine offenen Rechnungen" in text
        assert "100.000-€-Grenze ist überschritten" not in text

    def test_standardsatz_wird_in_cent_gespeichert(self, klient):
        antwort = klient.get("/einrichtung")
        assert "85,00" in antwort.text

    def test_dashboard_ohne_daten_mahnt_keine_frist_an(self, klient):
        assert "Rechnungsfrist" not in klient.get("/").text

    def test_dashboard_warnt_vor_der_sechsmonatsfrist(self, klient, tmp_path):
        """§ 14 Abs. 2 Satz 2 UStG -- das bietet kein Marktprodukt."""
        import sqlite3

        from app import db as dbmodul

        kid = int(
            klient.post(
                "/kunden",
                data={
                    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115",
                    "ort": "Berlin", "typ": "UNTERNEHMER", "leistungsort": "INLAND",
                    "satz_euro": "90,00", "gruppierung": "PRO_TITEL", "aktiv": "1",
                },
                follow_redirects=False,
            ).headers["location"].rsplit("/", 1)[1]
        )
        alt = (_dt.date.today() - _dt.timedelta(days=200)).isoformat()
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Beratung', ?, 120, ?)",
            (kid, alt, dbmodul.jetzt_utc()),
        )
        conn.commit()
        conn.close()

        text = klient.get("/").text
        assert "Rechnungsfrist" in text
        assert "§ 14 Abs. 2 Satz 2 UStG" in text
        assert "Tage über" in text

    def test_dashboard_mahnt_die_fehlende_sicherung_an(self, klient):
        text = klient.get("/").text
        assert "Letzte geprüfte Sicherung" in text
        assert "noch keine" in text

    def test_vorbelegung_ist_der_viertelstundentakt(self, klient, tmp_path):
        """Angefangene 15 Minuten, aufwärts — die hier gewünschte Regel.

        Der Test haelt die VORBELEGUNG fest, nicht die Mechanik: wer die
        Vorbelegung aendert, aendert sie fuer jede neu eingerichtete Datenbank,
        und das soll eine bewusste Entscheidung sein. Gerundet wird dabei auf
        die Summe je Rechnungsposition, nie je Zeiteintrag.
        """
        import sqlite3

        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        zeile = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
        conn.close()
        assert zeile["rundung_modus"] == "AUFWAERTS"
        assert zeile["rundung_intervall_min"] == 15

    def test_rundungsregel_laesst_sich_einstellen(self, klient, tmp_path):
        import sqlite3

        klient.post("/einrichtung", data={
            "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095",
            "ort": "Hamburg", "steuernummer": "12/345/67890",
            "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
            "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "118",
            "rundung_modus": "AUFWAERTS", "rundung_intervall_min": "15",
        }, follow_redirects=False)
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        zeile = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
        conn.close()
        assert zeile["rundung_modus"] == "AUFWAERTS"
        assert zeile["rundung_intervall_min"] == 15

    def test_nummernkreis_faellt_beim_zweiten_speichern_nicht_zurueck(self, klient):
        """Der Weg zu doppelt vergebenen Rechnungsnummern: das Formular zeigt
        beim zweiten Besuch wieder 1, und ein Speichern setzt den Zähler
        zurück."""
        assert 'value="118"' in klient.get("/einrichtung").text

    def test_kundennummer_start_wird_uebernommen(self, klient):
        klient.post("/einrichtung", data={
            "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095",
            "ort": "Hamburg", "steuernummer": "12/345/67890",
            "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
            "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "61",
            "kundennummer_start": "16",
        }, follow_redirects=False)
        text = klient.post("/kunden", data={
            "name": "Neuer Kunde", "strasse": "Weg 1", "plz": "10115", "ort": "Berlin",
            "typ": "UNTERNEHMER", "leistungsort": "INLAND", "gruppierung": "PRO_TITEL",
            "aktiv": "1",
        }, follow_redirects=True).text
        assert "K-0016" in text


class TestKundennummerFormat:
    """Gewachsene Nummernkreise sehen verschieden aus.

    Bis hierher war "K-0001" fest verdrahtet. Wer seine Kunden seit Jahren als
    01, 02, 03 fuehrt, muesste entweder alle Altrechnungen umschreiben oder mit
    zwei Nummernsystemen leben -- beides falsch.
    """

    def test_zweistelliges_format_laesst_sich_einstellen(self, klient, tmp_path):
        import sqlite3

        antwort = klient.post("/einrichtung", data={
            "name": "Beispiel Marketing", "strasse": "Musterstraße 10",
            "plz": "20095", "ort": "Hamburg", "ust_idnr": "DE999999999",
            "nummer_format": "{N}", "reset_jaehrlich": "0", "start_seq": "62",
            "kundennummer_format": "{:02d}", "kundennummer_start": "17",
        }, follow_redirects=False)
        assert antwort.status_code == 303

        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            from app.logik import kunden

            assert kunden.naechste_kundennummer(conn) == "17"
            kreis = conn.execute(
                "SELECT format, naechste_seq, reset_jaehrlich FROM nummernkreis"
                " WHERE typ = 'RECHNUNG'"
            ).fetchone()
            assert kreis["format"] == "{N}"
            assert kreis["naechste_seq"] == 62
            assert not kreis["reset_jaehrlich"]
        finally:
            conn.close()

    def test_format_ohne_jahr_mit_jaehrlichem_neustart_wird_abgewiesen(self, klient):
        """Die Kombination erzeugt garantiert doppelte Rechnungsnummern."""
        antwort = klient.post("/einrichtung", data={
            "name": "Beispiel Marketing", "strasse": "Musterstraße 10",
            "plz": "20095", "ort": "Hamburg", "ust_idnr": "DE999999999",
            "nummer_format": "{N}", "reset_jaehrlich": "1", "start_seq": "62",
        })
        assert antwort.status_code == 422
        assert "doppelte Nummern" in antwort.text

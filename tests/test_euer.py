"""Die Einnahmenüberschussrechnung.

Der heikelste Punkt ist nicht das Rechnen, sondern die Abgrenzung gegen den
§ 19-Umsatzzähler: beide zählen Geld, aber nicht dasselbe. Wer das verwechselt,
erklärt entweder zu wenig Gewinn oder verliert den Kleinunternehmerstatus, ohne
es zu merken.
"""

import pytest

from app import db
from app.logik import euer, grenzwert


def _kunde(conn, nummer, name, leistungsort="INLAND"):
    return conn.execute(
        "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, leistungsort, angelegt_am)"
        " VALUES (?, ?, 'Weg 1', '10115', 'Berlin', ?, ?)",
        (nummer, name, leistungsort, db.heute()),
    ).lastrowid


def _rechnung(conn, kunde_id, nummer, brutto, leistungsort="INLAND"):
    return conn.execute(
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                              netto_cent, brutto_cent, leistungsort, betreff,
                              status, festgeschrieben_am, erstellt_am)
        VALUES ('RECHNUNG', ?, 1, 2026, ?, '2026-03-01', ?, ?, ?, 'Leistung März',
                'FESTGESCHRIEBEN', ?, ?)
        """,
        (nummer, kunde_id, brutto, brutto, leistungsort, db.jetzt_utc(), db.jetzt_utc()),
    ).lastrowid


def _zahlung_rechnung(conn, rechnung_id, datum, cent):
    conn.execute(
        "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, ?, ?)",
        (rechnung_id, datum, cent),
    )


def _beleg(conn, schluessel, brutto, *, datum="2026-05-01", bezahlt="2026-05-01"):
    kategorie = conn.execute(
        "SELECT id, quote_default FROM euer_kategorie WHERE schluessel = ?", (schluessel,)
    ).fetchone()
    assert kategorie is not None, f"Kategorie {schluessel} fehlt im Katalog"
    beleg = conn.execute(
        """
        INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,
                           aufteilungsquote, betrag_betrieblich_cent, erfasst_am)
        VALUES ('EINGANG', ?, ?, ?, 100, ?, ?)
        """,
        (kategorie["id"], datum, brutto, brutto, db.jetzt_utc()),
    ).lastrowid
    if bezahlt:
        # NEGATIV, genau wie belege._zahlung_erfassen: so bleibt eine Summe
        # ueber alle Zahlungen vorzeichenrichtig. Ein Testhelfer, der anders
        # bucht als die Anwendung, prueft eine Lage, die es nie gibt.
        conn.execute(
            "INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
            (beleg, bezahlt, -brutto),
        )
    return beleg


@pytest.fixture
def gefuellt(conn, betrieb):
    conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
    inland = _kunde(conn, "001", "Inlandskunde")
    at = _kunde(conn, "010", "Alpenbau", "EU_B2B_REVERSE_CHARGE")
    _zahlung_rechnung(conn, _rechnung(conn, inland, "61", 300000), "2026-04-01", 300000)
    _zahlung_rechnung(
        conn,
        _rechnung(conn, at, "62", 63646, "EU_B2B_REVERSE_CHARGE"),
        "2026-04-15", 63646,
    )
    return conn


class TestEinnahmen:
    def test_auslandsumsatz_ist_betriebseinnahme(self, gefuellt):
        """Der Punkt, an dem EÜR und § 19-Zähler auseinanderlaufen.

        Ertragsteuerlich zählt jeder Euro; umsatzsteuerlich zählt nur, was im
        Inland steuerbar ist. Wer das verwechselt, erklärt zu wenig Gewinn.
        """
        a = euer.auswertung(gefuellt, 2026)
        assert a.einnahmen_cent == 363646
        assert grenzwert.vereinnahmt_cent(gefuellt, 2026) == 300000

    def test_auslandsanteil_steht_nachrichtlich_da(self, gefuellt):
        a = euer.auswertung(gefuellt, 2026)
        nachricht = [z for z in a.einnahmen if z.nachrichtlich]
        assert len(nachricht) == 1
        assert nachricht[0].kennzahl == euer.KZ_NICHT_STEUERBAR
        assert nachricht[0].betrag_cent == 63646

    def test_nachrichtliche_zeile_zaehlt_nicht_doppelt(self, gefuellt):
        a = euer.auswertung(gefuellt, 2026)
        assert a.einnahmen_cent == sum(
            z.betrag_cent for z in a.einnahmen if not z.nachrichtlich
        )

    def test_gezaehlt_wird_der_zahlungseingang(self, conn, betrieb):
        """§ 11 EStG: der Tag, an dem das Geld fließt."""
        kunde = _kunde(conn, "001", "Kunde")
        rid = _rechnung(conn, kunde, "61", 100000)
        _zahlung_rechnung(conn, rid, "2027-01-10", 100000)
        assert euer.auswertung(conn, 2026).einnahmen_cent == 0
        assert euer.auswertung(conn, 2027).einnahmen_cent == 100000


class TestAusgaben:
    def test_unbezahlter_beleg_ist_noch_keine_betriebsausgabe(self, conn, betrieb):
        """§ 11 Abs. 2 EStG. Ein Beleg allein bewegt kein Geld."""
        _beleg(conn, "BUEROBEDARF", 5000, bezahlt=None)
        assert euer.auswertung(conn, 2026).ausgaben_cent == 0

    def test_bezahlter_beleg_landet_in_seiner_kennzahl(self, conn, betrieb):
        _beleg(conn, "BUEROBEDARF", 5000)
        a = euer.auswertung(conn, 2026)
        assert a.ausgaben_cent == 5000
        assert all(z.bereich == euer.BEREICH_AUSGABEN for z in a.ausgaben)

    def test_private_quote_ist_schon_abgezogen(self, conn, betrieb):
        """``betrag_betrieblich_cent`` trägt die Aufteilung, nicht der Bruttobetrag."""
        kategorie = conn.execute(
            "SELECT id FROM euer_kategorie WHERE schluessel = 'TELEFON_INTERNET'"
        ).fetchone()
        beleg = conn.execute(
            """
            INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,
                               aufteilungsquote, betrag_betrieblich_cent, erfasst_am)
            VALUES ('EINGANG', ?, '2026-05-01', 10000, 60, 6000, ?)
            """,
            (kategorie["id"], db.jetzt_utc()),
        ).lastrowid
        conn.execute(
            "INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, '2026-05-01', 10000)",
            (beleg,),
        )
        assert euer.auswertung(conn, 2026).ausgaben_cent == 6000

    def test_bewirtung_wird_aufgeteilt_statt_gekuerzt(self, conn, betrieb):
        """§ 4 Abs. 5 Satz 1 Nr. 2 EStG: 70 % abziehbar.

        Die restlichen 30 % verschwinden NICHT -- sie gehören in ihre eigene
        Kennzahl. Weggelassen wäre der Gewinn zu niedrig, und das fällt erst
        in einer Prüfung auf.
        """
        _beleg(conn, "BEWIRTUNG", 10000)
        a = euer.auswertung(conn, 2026)
        nach_kennzahl = {z.kennzahl: z.betrag_cent for z in a.ausgaben}
        assert nach_kennzahl[175] == 7000, "70 % abziehbar"
        assert nach_kennzahl[165] == 3000, "30 % in der eigenen Kennzahl"
        # Und NICHT zusammen der volle Betrag: der nicht abziehbare Teil steht
        # im Formular, mindert den Gewinn aber nicht. Diese Zusicherung stand
        # vorher andersherum da und hat den Fehler festgeschrieben.
        assert a.ausgaben_cent == 7000, "nur der abziehbare Teil mindert den Gewinn"
        assert [z.nachrichtlich for z in a.ausgaben if z.kennzahl == 165] == [True]

    def test_anlagegut_laeuft_ueber_die_afa_nicht_ueber_den_beleg(self, conn, betrieb):
        """Sonst stünde die Anschaffung zweimal im Jahr der Zahlung."""
        kategorie = conn.execute(
            "SELECT id FROM euer_kategorie WHERE bereich = 25 LIMIT 1"
        ).fetchone()
        beleg = conn.execute(
            """
            INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,
                               aufteilungsquote, betrag_betrieblich_cent,
                               ist_anlagegut, erfasst_am)
            VALUES ('EINGANG', ?, '2026-05-01', 200000, 100, 200000, 1, ?)
            """,
            (kategorie["id"], db.jetzt_utc()),
        ).lastrowid
        conn.execute(
            "INSERT INTO zahlung (beleg_id, datum, betrag_cent)"
            " VALUES (?, '2026-05-01', 200000)",
            (beleg,),
        )
        assert euer.auswertung(conn, 2026).ausgaben_cent == 0


class TestGewinn:
    def test_gewinn_ist_einnahmen_minus_ausgaben(self, gefuellt):
        _beleg(gefuellt, "BUEROBEDARF", 5000)
        a = euer.auswertung(gefuellt, 2026)
        assert a.gewinn_cent == a.einnahmen_cent - a.ausgaben_cent == 363646 - 5000


class TestBuchungen:
    def test_jede_zahlung_wird_eine_zeile(self, gefuellt):
        _beleg(gefuellt, "BUEROBEDARF", 5000)
        zeilen = euer.buchungen(gefuellt, 2026)
        assert len(zeilen) == 3
        assert {z["richtung"] for z in zeilen} == {"Einnahme", "Ausgabe"}

    def test_zeilen_tragen_belegnummer_und_gegenstelle(self, gefuellt):
        zeilen = euer.buchungen(gefuellt, 2026)
        einnahme = [z for z in zeilen if z["richtung"] == "Einnahme"]
        assert {z["beleg"] for z in einnahme} == {"61", "62"}
        assert "Alpenbau" in {z["gegenstelle"] for z in einnahme}

    def test_auslandszeile_traegt_die_richtige_kategorie(self, gefuellt):
        zeilen = euer.buchungen(gefuellt, 2026)
        alpenbau = next(z for z in zeilen if z["gegenstelle"] == "Alpenbau")
        assert "nicht steuerbar" in alpenbau["kategorie"]

    def test_nach_datum_sortiert(self, gefuellt):
        _beleg(gefuellt, "BUEROBEDARF", 5000, bezahlt="2026-01-02")
        zeilen = euer.buchungen(gefuellt, 2026)
        assert [z["datum"] for z in zeilen] == sorted(z["datum"] for z in zeilen)


class TestFehlendeZuordnung:
    """Die unangenehmste Sorte Fehler: nichts kaputt, die Zahl nur zu niedrig.

    Ein Beleg ohne Kategorie hat keine Kennzahl, also keine Zeile in der EÜR.
    Das Geld ist erfasst, zählt aber nicht mit — der Gewinn fällt um genau
    diesen Betrag zu hoch aus, und nichts sagt es.
    """

    def _ohne_kategorie(self, conn, brutto, bezahlt="2026-06-01"):
        beleg = conn.execute(
            "INSERT INTO beleg (art, belegdatum, betrag_brutto_cent, aufteilungsquote,"
            " betrag_betrieblich_cent, erfasst_am)"
            " VALUES ('EINGANG', '2026-06-01', ?, 100, ?, ?)",
            (brutto, brutto, db.jetzt_utc()),
        ).lastrowid
        if bezahlt:
            conn.execute(
                "INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                (beleg, bezahlt, -brutto),
            )
        return beleg

    def test_bezahlter_beleg_ohne_kategorie_wird_gemeldet(self, conn, betrieb):
        self._ohne_kategorie(conn, 4990)
        luecke = euer.fehlende_zuordnung(conn, 2026)
        assert luecke and luecke.anzahl == 1 and luecke.betrag_cent == 4990

    def test_er_fehlt_tatsaechlich_in_der_auswertung(self, conn, betrieb):
        """Gegenprobe — sonst prüfte der Test nur sich selbst."""
        self._ohne_kategorie(conn, 4990)
        assert euer.auswertung(conn, 2026).ausgaben_cent == 0

    def test_unbezahlter_beleg_zaehlt_nicht_als_luecke(self, conn, betrieb):
        """Ohne Zahlung fehlt nichts: § 11 Abs. 2 EStG, noch keine Ausgabe."""
        self._ohne_kategorie(conn, 4990, bezahlt=None)
        assert not euer.fehlende_zuordnung(conn, 2026)

    def test_zugeordneter_beleg_ist_keine_luecke(self, conn, betrieb):
        _beleg(conn, "BUEROBEDARF", 4990)
        assert not euer.fehlende_zuordnung(conn, 2026)

    def test_ohne_jahr_zaehlt_alles(self, conn, betrieb):
        self._ohne_kategorie(conn, 1000, bezahlt="2025-06-01")
        self._ohne_kategorie(conn, 2000, bezahlt="2026-06-01")
        assert euer.fehlende_zuordnung(conn).anzahl == 2
        assert euer.fehlende_zuordnung(conn, 2026).anzahl == 1


class TestVorzeichen:
    def test_ausgaben_stehen_in_den_buchungen_positiv(self, conn, betrieb):
        """Gespeichert sind sie negativ, gedruckt positiv.

        Die Spalte „Richtung" trägt die Richtung, und das Blatt „EÜR" zeigt
        Ausgaben ebenfalls positiv. Zwei Vorzeichenregeln in derselben Datei
        sind die sicherste Art, sich zu verrechnen.
        """
        _beleg(conn, "BUEROBEDARF", 4990)
        ausgabe = next(
            z for z in euer.buchungen(conn, 2026) if z["richtung"] == "Ausgabe"
        )
        assert ausgabe["betrag_cent"] == 4990
        # Und in der Datenbank bleibt es negativ:
        gespeichert = conn.execute(
            "SELECT betrag_cent FROM zahlung WHERE beleg_id IS NOT NULL"
        ).fetchone()["betrag_cent"]
        assert gespeichert == -4990


class TestJahre:
    def test_nur_jahre_mit_bewegung(self, gefuellt):
        assert euer.jahre(gefuellt) == [2026]

    def test_leerer_bestand_hat_keine_jahre(self, conn):
        assert euer.jahre(conn) == []


class TestSeite:
    """Die Seite /euer — dieselben Zahlen ohne Tabellenprogramm."""

    @pytest.fixture
    def klient(self, tmp_path, monkeypatch):
        import importlib

        from fastapi.testclient import TestClient

        from app import db as db_modul
        from app.main import erzeuge_app

        monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
        importlib.reload(db_modul)
        with TestClient(erzeuge_app(tmp_path / "daten" / "steuer.sqlite")) as c:
            c.post("/einrichtung", data={
                "name": "Beispiel Marketing", "strasse": "Musterstraße 10",
                "plz": "20095", "ort": "Hamburg", "ust_idnr": "DE999999999",
                "nummer_format": "{N}", "reset_jaehrlich": "0", "start_seq": "62",
            }, follow_redirects=False)
            yield c

    def _fuellen(self, tmp_path):
        import sqlite3

        conn = sqlite3.connect(tmp_path / "daten" / "steuer.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            inland = _kunde(conn, "001", "Inlandskunde")
            at = _kunde(conn, "010", "Alpenbau", "EU_B2B_REVERSE_CHARGE")
            _zahlung_rechnung(conn, _rechnung(conn, inland, "62", 300000),
                              "2026-04-01", 300000)
            _zahlung_rechnung(
                conn, _rechnung(conn, at, "63", 63646, "EU_B2B_REVERSE_CHARGE"),
                "2026-04-15", 63646,
            )
            conn.commit()
        finally:
            conn.close()

    def test_ohne_zahlungen_erklaert_die_seite_warum(self, klient):
        text = klient.get("/euer").text
        assert "noch keine Zahlung erfasst" in text
        # Die Seite muss SAGEN, warum sie leer ist: eine festgeschriebene
        # Rechnung ohne Zahlungseingang steht hier noch nicht.
        assert "ohne\n  Zahlungseingang" in text

    def test_zahlen_stehen_auf_der_seite(self, klient, tmp_path):
        self._fuellen(tmp_path)
        text = klient.get("/euer?jahr=2026").text
        assert "3.000,00" in text, "Inlandsumsatz"
        assert "636,46" in text, "Auslandsumsatz"
        assert "3.636,46" in text, "Summe und Gewinn"
        assert "nachrichtlich" in text

    def test_jahreswahl_greift(self, klient, tmp_path):
        self._fuellen(tmp_path)
        assert "3.636,46" not in klient.get("/euer?jahr=2027").text

    def test_ohne_einrichtung_zur_einrichtung(self, tmp_path, monkeypatch):
        import importlib

        from fastapi.testclient import TestClient

        from app import db as db_modul
        from app.main import erzeuge_app

        monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "leer"))
        importlib.reload(db_modul)
        with TestClient(erzeuge_app(tmp_path / "leer" / "steuer.sqlite")) as c:
            antwort = c.get("/euer", follow_redirects=False)
            assert antwort.status_code == 303
            assert antwort.headers["location"] == "/einrichtung"


class TestRatenzahlung:
    """Eine Rechnung in Raten zu bezahlen ist der Normalfall, nicht der Sonderfall.

    Die Ausgabenabfrage verband ``beleg`` mit ``zahlung`` und summierte dabei den
    BELEGBETRAG — einmal je Zahlungszeile. Zwei Raten machten aus 100 € Ausgabe
    200 €. Nichts war kaputt, der Gewinn war nur um die Differenz zu niedrig.
    """

    def _beleg(self, conn, brutto, betrieblich, raten):
        kategorie = conn.execute(
            "SELECT id FROM euer_kategorie WHERE schluessel = 'BUEROBEDARF'"
        ).fetchone()["id"]
        beleg = conn.execute(
            "INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,"
            " aufteilungsquote, betrag_betrieblich_cent, erfasst_am)"
            " VALUES ('EINGANG', ?, '2026-06-01', ?, ?, ?, ?)",
            (kategorie, brutto, betrieblich * 100 // brutto, betrieblich, db.jetzt_utc()),
        ).lastrowid
        for datum, cent in raten:
            conn.execute(
                "INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                (beleg, datum, -cent),
            )
        return beleg

    def test_zwei_raten_zaehlen_einmal(self, conn, betrieb):
        self._beleg(conn, 10000, 10000, [("2026-06-01", 6000), ("2026-07-01", 4000)])
        assert euer.auswertung(conn, 2026).ausgaben_cent == 10000

    def test_rate_ueber_den_jahreswechsel_faellt_in_ihr_jahr(self, conn, betrieb):
        """§ 11 Abs. 2 EStG: jeder Abfluss in dem Jahr, in dem er fließt."""
        self._beleg(conn, 10000, 10000, [("2026-12-20", 5000), ("2027-01-10", 5000)])
        assert euer.auswertung(conn, 2026).ausgaben_cent == 5000
        assert euer.auswertung(conn, 2027).ausgaben_cent == 5000

    def test_teilzahlung_bringt_nur_ihren_betrieblichen_anteil(self, conn, betrieb):
        """Wer von 100 € mit 60 % betrieblichem Anteil erst 50 € zahlt, hat 30 €
        Betriebsausgabe — nicht 60."""
        self._beleg(conn, 10000, 6000, [("2026-06-01", 5000)])
        assert euer.auswertung(conn, 2026).ausgaben_cent == 3000

    def test_volle_zahlung_bringt_genau_den_betrieblichen_anteil(self, conn, betrieb):
        """Gegenprobe: die Verhältnisrechnung darf den Normalfall nicht verbiegen."""
        self._beleg(conn, 10000, 6000, [("2026-06-01", 10000)])
        assert euer.auswertung(conn, 2026).ausgaben_cent == 6000

    def test_luecke_zaehlt_den_beleg_einmal(self, conn, betrieb):
        """Dieselbe Falle in fehlende_zuordnung()."""
        kategorie_los = conn.execute(
            "INSERT INTO beleg (art, belegdatum, betrag_brutto_cent, aufteilungsquote,"
            " betrag_betrieblich_cent, erfasst_am)"
            " VALUES ('EINGANG', '2026-06-01', 10000, 100, 10000, ?)",
            (db.jetzt_utc(),),
        ).lastrowid
        for datum, cent in (("2026-06-01", 6000), ("2026-07-01", 4000)):
            conn.execute(
                "INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                (kategorie_los, datum, -cent),
            )
        luecke = euer.fehlende_zuordnung(conn, 2026)
        assert luecke.anzahl == 1
        assert luecke.betrag_cent == 10000


class TestAnlagenabgang:
    def test_erloes_und_restbuchwert_stehen_in_der_euer(self, conn, betrieb):
        """Verkauf eines Anlageguts: der Erlös ist Betriebseinnahme (KZ 102),
        der Restbuchwert Betriebsausgabe (KZ 135). Vorher gab es beide Zeilen
        nicht -- der Restbuchwert blieb fuer immer in der Luft."""
        from app.logik import anlagen

        gut = anlagen.anlegen(conn, bezeichnung="Schreibtisch", anschaffung_am="2026-01-05",
                              ak_brutto_cent=120000, ust_satz_promille=190,
                              nutzungsdauer_jahre=3)
        anlagen.abgang(conn, gut, am="2027-06-30", grund="verkauft", erloes_cent=30000)

        a = euer.auswertung(conn, 2027)
        nach_kz = {(z.kennzahl, z.bezeichnung[:12]): z.betrag_cent
                   for z in a.einnahmen + a.ausgaben}
        assert nach_kz[(anlagen.KZ_ANLAGENVERKAUF, "Veräußerung ")] == 30000
        assert nach_kz[(anlagen.KZ_RESTBUCHWERT, "Restbuchwert")] == 60000
        assert nach_kz[(anlagen.KZ_BEWEGLICH, "Abschreibung")] == 20000
        assert a.gewinn_cent == 30000 - 60000 - 20000
        # Nicht im § 19-Gesamtumsatz: Anlagevermoegen bleibt ausser Ansatz.
        assert grenzwert.vereinnahmt_cent(conn, 2027) == 0

    def test_betrieblicher_anteil_gilt_auch_beim_abgang(self, conn, betrieb):
        from app.logik import anlagen

        gut = anlagen.anlegen(conn, bezeichnung="Kamera", anschaffung_am="2026-01-05",
                              ak_brutto_cent=120000, ust_satz_promille=190,
                              nutzungsdauer_jahre=3, aufteilungsquote=50)
        anlagen.abgang(conn, gut, am="2027-06-30", grund="verkauft", erloes_cent=30000)
        a = euer.auswertung(conn, 2027)
        nach_kz = {z.kennzahl: z.betrag_cent for z in a.einnahmen + a.ausgaben}
        assert nach_kz[anlagen.KZ_ANLAGENVERKAUF] == 15000
        assert nach_kz[anlagen.KZ_RESTBUCHWERT] == 30000
        assert nach_kz[anlagen.KZ_BEWEGLICH] == 10000


class TestBuchungenAlsVorstufe:
    def test_buchungszeile_traegt_den_euer_wirksamen_anteil(self, conn, betrieb):
        """Eine Bewirtung ueber 100 Euro mit 50 Prozent betrieblichem Anteil:
        geflossen sind 100, Betriebsausgabe sind 35 (50 % Quote, davon 70 %
        abziehbar). Das Blatt "Buchungen" zeigte nur die 100 auf der
        abziehbaren Kennzahl -- wer daraus die EUeR ableitete, setzte das
        Dreifache ab."""
        kategorie = conn.execute(
            "SELECT id FROM euer_kategorie WHERE schluessel = 'BEWIRTUNG'").fetchone()["id"]
        beleg = conn.execute(
            "INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,"
            " aufteilungsquote, betrag_betrieblich_cent, erfasst_am)"
            " VALUES ('EINGANG', ?, '2026-05-01', 10000, 50, 5000, ?)",
            (kategorie, db.jetzt_utc()),
        ).lastrowid
        conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
                     (beleg, "2026-05-02", -10000))
        [zeile] = euer.buchungen(conn, 2026)
        assert zeile["betrag_cent"] == 10000
        assert zeile["betriebsausgabe_cent"] == 3500

    def test_einnahmen_haben_keinen_ausgabenanteil(self, gefuellt):
        zeilen = euer.buchungen(gefuellt, 2026)
        assert all(z["betriebsausgabe_cent"] is None for z in zeilen if z["richtung"] == "Einnahme")

"""Kleinunternehmergrenzen. Zwei Fehler sind hier teuer."""

import pytest

from app import db
from app.logik import grenzwert as g


def _rechnung(conn, kunde_id, brutto, nummer, leistungsort="INLAND"):
    """Eine festgeschriebene Rechnung.

    ``leistungsort`` wird AUF DER RECHNUNG gesetzt, nicht am Kunden -- genau so,
    wie ``festschreiben.entwurf_anlegen`` es tut. Der Kundenstamm ist dafuer die
    falsche Quelle: er aendert sich, die Rechnung nicht.
    """
    cur = conn.execute(
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                              netto_cent, brutto_cent, leistungsort,
                              status, festgeschrieben_am, erstellt_am)
        VALUES ('RECHNUNG', ?, 1, 2026, ?, '2026-03-01', ?, ?, ?,
                'FESTGESCHRIEBEN', ?, ?)
        """,
        (nummer, kunde_id, brutto, brutto, leistungsort, db.jetzt_utc(), db.jetzt_utc()),
    )
    return cur.lastrowid


def _zahlung(conn, rechnung_id, datum, cent):
    conn.execute(
        "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, ?, ?)",
        (rechnung_id, datum, cent),
    )


@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
    return kunde_id


class TestBemessungsgrundlage:
    def test_gezaehlt_werden_zahlungen_nicht_rechnungen(self, conn, eingerichtet):
        """§ 19 Abs. 2 Satz 1 UStG rechnet nach vereinnahmten Entgelten.

        Ein rechnungsbasierter Zaehler zeigt zu viel, solange Kunden offen sind,
        und meldet die Grenze zum falschen Zeitpunkt.
        """
        rid = _rechnung(conn, eingerichtet, 500000, "R1")
        assert g.vereinnahmt_cent(conn, 2026) == 0, "Noch nichts vereinnahmt"
        _zahlung(conn, rid, "2026-04-01", 200000)
        assert g.vereinnahmt_cent(conn, 2026) == 200000, "Teilzahlung zaehlt anteilig"
        _zahlung(conn, rid, "2026-05-01", 300000)
        assert g.vereinnahmt_cent(conn, 2026) == 500000

    def test_zahlung_zaehlt_im_jahr_des_eingangs(self, conn, eingerichtet):
        rid = _rechnung(conn, eingerichtet, 100000, "R1")
        _zahlung(conn, rid, "2027-01-10", 100000)
        assert g.vereinnahmt_cent(conn, 2026) == 0
        assert g.vereinnahmt_cent(conn, 2027) == 100000

    def test_auslandsumsatz_zaehlt_nicht_in_die_grenze(self, conn, betrieb):
        """Nicht steuerbare Auslandsumsaetze fressen die Grenze nicht auf --
        sind aber sehr wohl EUeR-Betriebseinnahme."""
        cur = conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, leistungsort, angelegt_am)"
            " VALUES ('K-0002', 'US Agency', 'Road 1', '00000', 'NY', 'DRITTLAND', ?)",
            (db.heute(),),
        )
        rid = _rechnung(conn, cur.lastrowid, 900000, "R-US", leistungsort="DRITTLAND")
        _zahlung(conn, rid, "2026-04-01", 900000)
        assert g.vereinnahmt_cent(conn, 2026) == 0

    def test_umzug_des_kunden_aendert_abgeschlossene_jahre_nicht(self, conn, betrieb):
        """Der Zaehler liest den Schnappschuss, nicht den Kundenstamm.

        Zieht ein Kunde von Salzburg nach Hamburg, holte ein Blick in den Stamm
        rueckwirkend jahrelangen Auslandsumsatz in den § 19-Gesamtumsatz -- und
        risse womoeglich eine Grenze, die damals eingehalten war. Umgekehrt
        genauso: ein Wegzug loeschte steuerbaren Umsatz aus der Vergangenheit.
        """
        cur = conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, leistungsort,"
            " ust_idnr, angelegt_am)"
            " VALUES ('K-0003', 'Alpenbau', 'Getreidegasse 1', '5020', 'Salzburg',"
            " 'EU_B2B_REVERSE_CHARGE', 'ATU12345678', ?)",
            (db.heute(),),
        )
        kunde = cur.lastrowid
        rid = _rechnung(conn, kunde, 800000, "R-AT",
                        leistungsort="EU_B2B_REVERSE_CHARGE")
        _zahlung(conn, rid, "2026-04-01", 800000)
        assert g.vereinnahmt_cent(conn, 2026) == 0

        conn.execute(
            "UPDATE kunde SET leistungsort = 'INLAND', plz = '20095', ort = 'Hamburg'"
            " WHERE id = ?",
            (kunde,),
        )
        assert g.vereinnahmt_cent(conn, 2026) == 0, (
            "Der Umzug darf das abgeschlossene Jahr nicht umdeuten"
        )

    def test_leistungsnachweis_zaehlt_nicht(self, conn, eingerichtet):
        conn.execute("UPDATE betrieb SET nullrechnung_zulassen = 1 WHERE id = 1")
        conn.execute(
            """
            INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                                  brutto_cent, status, festgeschrieben_am, erstellt_am)
            VALUES ('LEISTUNGSNACHWEIS', 'LN1', 1, 2026, ?, '2026-03-01', 0,
                    'FESTGESCHRIEBEN', ?, ?)
            """,
            (eingerichtet, db.jetzt_utc(), db.jetzt_utc()),
        )
        assert g.vereinnahmt_cent(conn, 2026) == 0


class TestWelcheGrenzeGilt:
    def test_gruendungsjahr_hat_25000_nicht_100000(self, conn, betrieb, kunde_id):
        """Die Falle: ein Tool, das nur gegen 100.000 € prueft, lullt einen
        Gruender genau dann ein, wenn es teuer wird."""
        conn.execute("UPDATE betrieb SET beginn_am = '2026-05-01' WHERE id = 1")
        stand = g.stand(conn, 2026)
        assert stand.gruendungsjahr is True
        assert stand.grenze_cent == g.GRENZE_VORJAHR_CENT == 2_500_000

    def test_spaeteres_jahr_hat_100000(self, conn, eingerichtet):
        stand = g.stand(conn, 2026)
        assert stand.gruendungsjahr is False
        assert stand.grenze_cent == g.GRENZE_LAUFEND_CENT == 10_000_000

    def test_gruender_reisst_die_grenze_bei_25000(self, conn, betrieb, kunde_id):
        conn.execute("UPDATE betrieb SET beginn_am = '2026-05-01' WHERE id = 1")
        rid = _rechnung(conn, kunde_id, 3_000_000, "R1")
        _zahlung(conn, rid, "2026-06-01", 3_000_000)
        stand = g.stand(conn, 2026)
        assert stand.ueberschritten is True
        assert stand.stufe == "rot"

    def test_dieselbe_summe_im_zweiten_jahr_ist_unauffaellig(self, conn, eingerichtet):
        rid = _rechnung(conn, eingerichtet, 3_000_000, "R1")
        _zahlung(conn, rid, "2026-06-01", 3_000_000)
        stand = g.stand(conn, 2026)
        assert stand.ueberschritten is False
        assert stand.stufe == "gruen"


class TestStufen:
    @pytest.mark.parametrize(
        ("cent", "stufe"),
        [(0, "gruen"), (7_000_000, "gruen"), (8_000_000, "gelb"),
         (9_500_000, "orange"), (10_000_001, "rot")],
    )
    def test_ampel(self, conn, eingerichtet, cent, stufe):
        rid = _rechnung(conn, eingerichtet, cent or 1, "R1")
        if cent:
            _zahlung(conn, rid, "2026-06-01", cent)
        assert g.stand(conn, 2026).stufe == stufe

    def test_rest_bis_zur_grenze(self, conn, eingerichtet):
        rid = _rechnung(conn, eingerichtet, 1_000_000, "R1")
        _zahlung(conn, rid, "2026-06-01", 1_000_000)
        assert g.stand(conn, 2026).rest_cent == 9_000_000


class TestVorjahr:
    def test_vorjahr_ueber_25000_beendet_den_status(self, conn, eingerichtet):
        """Dann gilt ab 1. Januar die Regelbesteuerung -- auch wenn der Umsatz
        im laufenden Jahr wieder darunter liegt."""
        rid = _rechnung(conn, eingerichtet, 3_000_000, "R1")
        _zahlung(conn, rid, "2025-06-01", 3_000_000)
        stand = g.stand(conn, 2026)
        assert stand.vorjahr_cent == 3_000_000
        assert stand.vorjahr_ueberschritten is True
        assert stand.ueberschritten is False, "Das laufende Jahr ist davon unberuehrt"

    def test_fehlende_vorjahresdaten_werden_als_solche_gemeldet(self, conn, eingerichtet):
        """Ohne Zahlungen im Vorjahr ist nicht entscheidbar, ob es keinen Umsatz
        gab oder das Tool damals nicht benutzt wurde."""
        assert g.stand(conn, 2026).vorjahr_erfasst is False

    def test_im_gruendungsjahr_gibt_es_kein_vorjahr(self, conn, betrieb, kunde_id):
        conn.execute("UPDATE betrieb SET beginn_am = '2026-05-01' WHERE id = 1")
        stand = g.stand(conn, 2026)
        assert stand.vorjahr_erfasst is True
        assert stand.vorjahr_cent == 0

    def test_ohne_betriebsbeginn_gilt_100000_und_die_anzeige_weiss_es(
        self, conn, betrieb, kunde_id
    ):
        """Der Fall des Umstiegs: ein Betrieb, der seit Jahren laeuft, dessen
        Beginn aber nirgends steht.

        Vorher setzte die Einrichtung stillschweigend den Installationstag ein.
        Damit war das laufende Jahr das "Gruendungsjahr", die Grenze 25.000 €
        statt 100.000 € -- und die Anzeige haette bei 25.001 € Rot gemeldet,
        wo noch 75.000 € Luft waren. Ohne Datum gilt die Regelgrenze, und
        beginn_bekannt sagt der Oberflaeche, dass sie das dazusagen muss.
        """
        conn.execute("UPDATE betrieb SET beginn_am = NULL WHERE id = 1")
        stand = g.stand(conn, 2026)
        assert stand.beginn_bekannt is False
        assert stand.gruendungsjahr is False
        assert stand.grenze_cent == g.GRENZE_LAUFEND_CENT

    def test_mit_betriebsbeginn_ist_er_bekannt(self, conn, eingerichtet):
        assert g.stand(conn, 2026).beginn_bekannt is True

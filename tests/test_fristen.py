"""Die Sechsmonatsfrist des § 14 Abs. 2 Satz 2 UStG.

Der Test, auf den es ankommt, ist nicht "wird gewarnt", sondern **wen die Frist
gar nicht trifft**. Eine Warnung bei Privatkunden waere eine falsche Behauptung
ueber das Gesetz, und eine Warnung bei interner Arbeit ein Klickritual -- nach
zwei Wochen sieht sie niemand mehr hin, und dann ueberliest man auch die echte.
"""

from datetime import date

from app import db
from app.logik import fristen


def _kunde(conn, name, typ="UNTERNEHMER", nr="K-0002"):
    return conn.execute(
        "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, typ, angelegt_am)"
        " VALUES (?, ?, 'Weg 1', '10115', 'Berlin', ?, ?)",
        (nr, name, typ, db.heute()),
    ).lastrowid


def _zeit(conn, kunde, datum, minuten=60, modus="STUNDEN", **felder):
    spalten = {
        "kunde_id": kunde, "titel": "Arbeit", "datum": datum, "dauer_minuten": minuten,
        "abrechnungsmodus": modus, "erfasst_am": db.jetzt_utc(), **felder,
    }
    if modus in ("KULANZ", "INTERN"):
        spalten.setdefault("grund", "Test")
    if modus == "KULANZ":
        spalten.setdefault("satz_cent_snapshot", 8000)
    namen = ", ".join(spalten)
    platz = ", ".join("?" * len(spalten))
    return conn.execute(
        f"INSERT INTO zeiteintrag ({namen}) VALUES ({platz})", tuple(spalten.values())
    ).lastrowid


HEUTE = date(2026, 10, 12)


class TestMonatsrechnung:
    def test_stichtag_fuenf_monate_zurueck(self):
        assert fristen._monate_zurueck(HEUTE, 5) == date(2026, 5, 12)

    def test_ueber_den_jahreswechsel(self):
        assert fristen._monate_zurueck(date(2026, 3, 31), 5) == date(2025, 10, 31)

    def test_kurzer_monat_wird_gekappt(self):
        # 31. Maerz minus einen Monat gibt es nicht als 31. Februar.
        assert fristen._monate_zurueck(date(2026, 3, 31), 1) == date(2026, 2, 28)

    def test_schaltjahr(self):
        assert fristen._monate_zurueck(date(2024, 3, 31), 1) == date(2024, 2, 29)


class TestFristfall:
    def test_frist_ist_sechs_monate_nach_der_leistung(self):
        fall = fristen.Fristfall(1, "Kunde", "2026-01-15", 3, 180, heute="2026-05-01")
        assert fall.frist_am == "2026-07-15"
        assert fall.tage_rest == 75
        assert not fall.ueberschritten

    def test_abgelaufene_frist(self):
        fall = fristen.Fristfall(1, "Kunde", "2026-01-15", 3, 180, heute="2026-10-12")
        assert fall.ueberschritten
        assert fall.tage_rest == -89

    def test_kurzer_monat_wird_gekappt(self):
        """31. August plus sechs Monate ist kein 31. Februar."""
        fall = fristen.Fristfall(1, "Kunde", "2025-08-31", 1, 60, heute="2026-01-01")
        assert fall.frist_am == "2026-02-28"


class TestOffeneFristen:
    def test_frische_zeiten_loesen_nichts_aus(self, conn, kunde_id):
        _zeit(conn, kunde_id, "2026-10-01")
        assert fristen.offene_fristen(conn, heute=HEUTE) == []

    def test_alte_zeiten_eines_unternehmers_warnen(self, conn, kunde_id):
        _zeit(conn, kunde_id, "2026-04-02", minuten=90)
        _zeit(conn, kunde_id, "2026-04-20", minuten=30)
        faelle = fristen.offene_fristen(conn, heute=HEUTE)
        assert len(faelle) == 1
        assert faelle[0].aeltestes == "2026-04-02"
        assert faelle[0].eintraege == 2
        assert faelle[0].minuten == 120
        assert faelle[0].frist_am == "2026-10-02"

    def test_privatkunde_loest_nichts_aus(self, conn):
        """§ 14 Abs. 2 S. 2 UStG trifft nur Unternehmer und juristische Personen."""
        privat = _kunde(conn, "Frau Schmidt", typ="PRIVAT")
        _zeit(conn, privat, "2026-01-02")
        assert fristen.offene_fristen(conn, heute=HEUTE) == []

    def test_juristische_person_loest_aus(self, conn):
        verein = _kunde(conn, "Turnverein e.V.", typ="JURISTISCHE_PERSON")
        _zeit(conn, verein, "2026-01-02")
        assert [f.kunde for f in fristen.offene_fristen(conn, heute=HEUTE)] == [
            "Turnverein e.V."
        ]

    def test_festgeschriebene_zeiten_zaehlen_nicht(self, conn, kunde_id, betrieb):
        """Erledigt ist die Frist mit dem AUSSTELLEN, nicht vorher."""
        rechnung = conn.execute(
            "INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,"
            " netto_cent, brutto_cent, status, festgeschrieben_am, erstellt_am)"
            " VALUES ('RECHNUNG', 'RE-2026-0001', 1, 2026, ?, '2026-02-01',"
            " 8000, 8000, 'FESTGESCHRIEBEN', ?, ?)",
            (kunde_id, db.jetzt_utc(), db.jetzt_utc()),
        ).lastrowid
        _zeit(conn, kunde_id, "2026-01-02", rechnung_id=rechnung)
        assert fristen.offene_fristen(conn, heute=HEUTE) == []

    def test_ein_entwurf_loescht_die_warnung_nicht(self, conn, kunde_id, betrieb):
        """Der gefährlichste Fall, weil er so harmlos aussieht.

        Ein Entwurf hat keine Nummer, kein Datum und lässt sich jederzeit
        verwerfen — er stellt nichts aus. Die Frist des § 14 Abs. 2 Satz 2 UStG
        läuft weiter. Zählte er als erledigt, löschte allein das ANLEGEN eines
        Entwurfs die Warnung, und zwar dauerhaft: danach taucht die Zeit in
        keiner Liste mehr auf.
        """
        entwurf = conn.execute(
            "INSERT INTO rechnung (typ, kunde_id, erstellt_am) VALUES ('RECHNUNG', ?, ?)",
            (kunde_id, db.jetzt_utc()),
        ).lastrowid
        _zeit(conn, kunde_id, "2026-01-02", rechnung_id=entwurf)
        faelle = fristen.offene_fristen(conn, heute=HEUTE)
        assert len(faelle) == 1, "die Frist läuft weiter"
        assert faelle[0].aeltestes == "2026-01-02"

    def test_abgeschriebene_zeiten_zaehlen_nicht(self, conn, kunde_id):
        _zeit(conn, kunde_id, "2026-01-02", abgeschrieben_am="2026-02-01",
              abgeschrieben_grund="verschenkt")
        assert fristen.offene_fristen(conn, heute=HEUTE) == []

    def test_interne_arbeit_zaehlt_nicht(self, conn, kunde_id):
        """Interne Arbeit geht nie auf eine Rechnung -- es gibt keine Frist."""
        _zeit(conn, kunde_id, "2026-01-02", modus="INTERN")
        assert fristen.offene_fristen(conn, heute=HEUTE) == []

    def test_kulanz_zaehlt_mit(self, conn, kunde_id):
        """Kulanz erscheint MIT 0 € auf einer Rechnung -- die muss raus."""
        _zeit(conn, kunde_id, "2026-01-02", modus="KULANZ")
        assert len(fristen.offene_fristen(conn, heute=HEUTE)) == 1

    def test_warnung_beginnt_einen_monat_vor_ablauf(self, conn, kunde_id):
        """Genau auf dem Stichtag soll gewarnt werden, einen Tag davor nicht."""
        _zeit(conn, kunde_id, "2026-05-12")
        assert len(fristen.offene_fristen(conn, heute=HEUTE)) == 1
        conn.execute("UPDATE zeiteintrag SET datum = '2026-05-13'")
        assert fristen.offene_fristen(conn, heute=HEUTE) == []

    def test_aelteste_frist_steht_oben(self, conn, kunde_id):
        zweiter = _kunde(conn, "Andere AG")
        _zeit(conn, kunde_id, "2026-04-01")
        _zeit(conn, zweiter, "2026-01-01")
        assert [f.kunde for f in fristen.offene_fristen(conn, heute=HEUTE)] == [
            "Andere AG", "Mustermann GmbH",
        ]

    def test_stichtag_wird_in_den_fall_uebernommen(self, conn, kunde_id):
        """Ohne mitgefuehrten Stichtag haengt die Restfrist am Aufrufzeitpunkt."""
        _zeit(conn, kunde_id, "2026-04-02")
        fall = fristen.offene_fristen(conn, heute=HEUTE)[0]
        assert fall.heute == "2026-10-12"
        assert fall.frist_am == "2026-10-02"
        assert fall.tage_rest == -10

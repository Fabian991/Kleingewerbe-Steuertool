"""Leistungen an Unternehmer im EU-Ausland.

Hier steckt der Fehler, der sonst unauffällig bliebe: **der § 19-Hinweis ist
auf so einer Rechnung falsch.** § 19 Abs. 1 UStG macht einen *steuerbaren*
Umsatz steuerfrei. Eine sonstige Leistung an einen Unternehmer im übrigen
Gemeinschaftsgebiet ist nach § 3a Abs. 2 UStG am Sitz des Empfängers zu
besteuern und in Deutschland gar nicht steuerbar — es gibt also keinen Umsatz,
den man befreien könnte. Der Hinweis behauptet eine Steuerbefreiung, die es
nicht gibt, und niemandem fällt es auf, weil in beiden Fällen keine
Umsatzsteuer auf der Rechnung steht.

Die Rechtslage, gegen die hier geprüft wird:

* **§ 14a Abs. 1 Satz 1 UStG** verlangt die Angabe „Steuerschuldnerschaft des
  Leistungsempfängers". § 19 Abs. 1 Satz 2 UStG nimmt von § 14a nur die
  USt-IdNr.-Pflicht (Satz 3) aus — diesen Hinweis nicht. Er bleibt also auch
  für einen Kleinunternehmer Pflicht.
* **§ 14a Abs. 1 Satz 2 UStG:** die Rechnung ist bis zum 15. des Folgemonats
  auszustellen. Nicht sechs Monate.
* **§ 19 Abs. 2 UStG** rechnet den Gesamtumsatz aus den *steuerbaren* Umsätzen
  (§ 1 Abs. 1 Nr. 1). Die Österreich-Umsätze zählen also **nicht** gegen die
  25.000/100.000-€-Grenze — sind aber sehr wohl Betriebseinnahme in der EÜR.
* **§ 18a Abs. 4 UStG:** „Die Absätze 1 bis 3 gelten nicht für Unternehmer, die
  § 19 Absatz 1 anwenden." Also **keine** Zusammenfassende Meldung.
"""

import re
import subprocess
from datetime import date

import pytest

from app import db
from app.logik import festschreiben as f
from app.logik import grenzwert, validierung
from app.logik.fristen import Fristfall, offene_fristen

HEUTE = date(2026, 10, 12)


@pytest.fixture
def betrieb_mit_rc(conn, betrieb):
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01', iban = 'DE02120300000000202051'"
        " WHERE id = 1"
    )
    return 1


def _kunde(conn, *, leistungsort="EU_B2B_REVERSE_CHARGE", ust_idnr="ATU12345678",
           name="Alpenbau GmbH", nr="K-0002", land="AT"):
    return conn.execute(
        "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, land, typ,"
        " leistungsort, ust_idnr, satz_cent, angelegt_am)"
        " VALUES (?, ?, 'Ringstr. 1', '1010', 'Wien', ?, 'UNTERNEHMER', ?, ?, 9500, ?)",
        (nr, name, land, leistungsort, ust_idnr, db.heute()),
    ).lastrowid


def _zeiten(conn, kunde_id, datum="2026-03-02", anzahl=2, minuten=60):
    return [
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, erfasst_am) VALUES (?, 'Beratung', ?, ?, 'STUNDEN', ?)",
            (kunde_id, datum, minuten, db.jetzt_utc()),
        ).lastrowid
        for _ in range(anzahl)
    ]


class TestHinweistext:
    def test_eu_b2b_bekommt_reverse_charge_statt_paragraph_19(self, conn, betrieb_mit_rc):
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["leistungsort"] == "EU_B2B_REVERSE_CHARGE"
        assert "Steuerschuldnerschaft des Leistungsempfängers" in rechnung["hinweis_19_text"]
        assert "§ 19" not in rechnung["hinweis_19_text"]

    def test_inland_bekommt_weiter_paragraph_19(self, conn, betrieb_mit_rc, kunde_id):
        conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
        rid = f.entwurf_anlegen(conn, kunde_id, _zeiten(conn, kunde_id))
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["leistungsort"] == "INLAND"
        assert "§ 19" in rechnung["hinweis_19_text"]

    def test_drittland_behauptet_kein_reverse_charge(self, conn, betrieb_mit_rc):
        """Reverse Charge gibt es außerhalb der EU nicht — das zu behaupten wäre
        eine Aussage über einen Mechanismus, der dort nicht existiert."""
        kid = _kunde(conn, leistungsort="DRITTLAND", ust_idnr=None, name="US Agency LLC",
                     land="US")
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        hinweis = conn.execute(
            "SELECT hinweis_19_text FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()["hinweis_19_text"]
        assert "§ 19" not in hinweis
        assert "Reverse Charge" not in hinweis
        assert "nicht steuerbar" in hinweis
        assert "§ 3a Abs. 2" in hinweis

    def test_leistungsort_ist_ein_schnappschuss(self, conn, betrieb_mit_rc):
        """Zieht der Kunde von Wien nach Hamburg, bleibt die alte Rechnung eine
        nicht steuerbare Leistung ins Ausland."""
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        conn.execute("UPDATE kunde SET leistungsort = 'INLAND' WHERE id = ?", (kid,))
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["leistungsort"] == "EU_B2B_REVERSE_CHARGE"


class TestValidierung:
    def _befunde(self, conn, rechnung_id):
        return {b.code: b for b in validierung.pruefe(conn, rechnung_id)}

    def test_saubere_eu_rechnung_ist_festschreibbar(self, conn, betrieb_mit_rc):
        conn.execute("UPDATE betrieb SET ust_idnr = 'DE999999999' WHERE id = 1")
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        rot = [b for b in validierung.pruefe(conn, rid) if b.stufe == validierung.ROT]
        assert rot == [], rot

    def test_paragraph_19_hinweis_wird_abgewiesen(self, conn, betrieb_mit_rc):
        """Der eigentliche Punkt dieser Datei."""
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        conn.execute(
            "UPDATE rechnung SET hinweis_19_text = ? WHERE id = ?",
            ("Steuerfreie Leistung eines Kleinunternehmers gemäß § 19 Abs. 1 UStG.", rid),
        )
        befunde = self._befunde(conn, rid)
        assert "R16" in befunde
        assert befunde["R16"].stufe == validierung.ROT
        assert "nicht steuerbar" in befunde["R16"].text
        # Und gleichzeitig fehlt dann der Pflichthinweis.
        assert "R17" in befunde

    def test_fehlender_reverse_charge_hinweis_wird_abgewiesen(self, conn, betrieb_mit_rc):
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        conn.execute("UPDATE rechnung SET hinweis_19_text = 'Danke!' WHERE id = ?", (rid,))
        befunde = self._befunde(conn, rid)
        assert "R17" in befunde
        assert "§ 14a Abs. 1 Satz 1 UStG" == befunde["R17"].fundstelle

    def test_fehlende_ust_idnr_des_kunden_ist_rot(self, conn, betrieb_mit_rc):
        """Nicht Formalie: ohne sie greift § 3a Abs. 2 nicht, der Leistungsort
        liegt im Inland, und die ganze Einordnung ist falsch."""
        kid = _kunde(conn, ust_idnr=None)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        befunde = self._befunde(conn, rid)
        assert "R18" in befunde
        assert befunde["R18"].stufe == validierung.ROT
        assert "§ 3a Abs. 1" in befunde["R18"].text

    def test_fehlende_eigene_ust_idnr_ist_nur_gelb(self, conn, betrieb_mit_rc):
        """§ 19 Abs. 1 Satz 2 UStG nimmt § 14a Abs. 1 Satz 3 ausdrücklich aus —
        für einen Kleinunternehmer ist die eigene USt-IdNr. keine Pflicht."""
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        befunde = self._befunde(conn, rid)
        assert "G22" in befunde
        assert befunde["G22"].stufe == validierung.GELB
        assert "§ 27a" in befunde["G22"].text
        assert "kostenlos" in befunde["G22"].text

    def test_mit_eigener_ust_idnr_keine_warnung(self, conn, betrieb_mit_rc):
        conn.execute("UPDATE betrieb SET ust_idnr = 'DE999999999' WHERE id = 1")
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        assert "G22" not in self._befunde(conn, rid)

    def test_drittland_mit_paragraph_19_hinweis_ist_rot(self, conn, betrieb_mit_rc):
        kid = _kunde(conn, leistungsort="DRITTLAND", ust_idnr=None, land="US")
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        conn.execute(
            "UPDATE rechnung SET hinweis_19_text = 'Gemäß § 19 UStG steuerfrei.'"
            " WHERE id = ?", (rid,),
        )
        assert "R19" in self._befunde(conn, rid)

    def test_inlandsrechnung_bleibt_unberuehrt(self, conn, betrieb_mit_rc, kunde_id):
        conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
        rid = f.entwurf_anlegen(conn, kunde_id, _zeiten(conn, kunde_id))
        befunde = self._befunde(conn, rid)
        for code in ("R16", "R17", "R18", "R19", "G22"):
            assert code not in befunde


class TestPdf:
    @pytest.fixture
    def pdf_text(self, conn, betrieb_mit_rc, tmp_path):
        conn.execute("UPDATE betrieb SET ust_idnr = 'DE999999999' WHERE id = 1")
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
        return subprocess.run(
            ["pdftotext", "-layout", ergebnis.pdf_pfad, "-"],
            capture_output=True, text=True, check=True,
        ).stdout

    def test_pflichthinweis_steht_drauf(self, pdf_text):
        assert "Steuerschuldnerschaft des Leistungsempfängers" in pdf_text
        assert "§ 19" not in pdf_text

    def test_beide_ust_idnr_stehen_drauf(self, pdf_text):
        """Die des Kunden ist der Nachweis seiner Unternehmereigenschaft."""
        assert "DE999999999" in pdf_text
        assert "ATU12345678" in pdf_text

    def test_rechtsgrundlage_steht_drauf(self, pdf_text):
        assert "Art. 196" in pdf_text
        assert "§ 3a Abs. 2 UStG" in pdf_text

    def test_trotzdem_kein_steuersatz_und_kein_steuerbetrag(self, pdf_text):
        """„Die Umsatzsteuer schuldet der Leistungsempfänger" ist eine Aussage
        darüber, WER sie schuldet — kein Ausweis eines Betrags. Ein Satz oder
        ein Betrag wäre § 14c Abs. 1 UStG."""
        for verboten in ("19 %", "7 %", "MwSt", "zzgl"):
            assert verboten not in pdf_text
        assert not re.search(r"(?<![\d,])0,00\s*€", pdf_text)

    def test_inlandsrechnung_zeigt_die_ust_idnr_des_kunden_nicht(
        self, conn, betrieb_mit_rc, kunde_id, tmp_path
    ):
        """Auf einer Inlandsrechnung hat sie keine Funktion und wäre Rauschen."""
        conn.execute(
            "UPDATE kunde SET satz_cent = 8000, ust_idnr = 'DE111111111' WHERE id = ?",
            (kunde_id,),
        )
        rid = f.entwurf_anlegen(conn, kunde_id, _zeiten(conn, kunde_id))
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        text = subprocess.run(
            ["pdftotext", "-layout", ergebnis.pdf_pfad, "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "DE111111111" not in text


class TestGrenzwert:
    def test_oesterreich_zaehlt_nicht_gegen_die_grenze(self, conn, betrieb_mit_rc, kunde_id):
        """§ 19 Abs. 2 UStG rechnet aus den STEUERBAREN Umsätzen (§ 1 Abs. 1
        Nr. 1). Eine in Deutschland nicht steuerbare Leistung ist keiner."""
        conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
        at = _kunde(conn)
        for kid, betrag in ((kunde_id, 500000), (at, 900000)):
            rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
            conn.execute(
                "UPDATE rechnung SET nummer = ?, datum = '2026-03-02',"
                " netto_cent = ?, brutto_cent = ?, status = 'FESTGESCHRIEBEN',"
                " pdf_sha256 = 'x', festgeschrieben_am = ? WHERE id = ?",
                (f"RE-2026-{rid:04d}", betrag, betrag, db.jetzt_utc(), rid),
            )
            conn.execute(
                "INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art)"
                " VALUES (?, '2026-03-20', ?, 'UEBERWEISUNG')", (rid, betrag),
            )
        stand = grenzwert.stand(conn, 2026)
        assert stand.vereinnahmt_cent == 500000, "Nur der Inlandsumsatz zählt"

    def test_aber_eine_betriebseinnahme_bleibt_es(self, conn, betrieb_mit_rc):
        """EÜR-Bemessung und Grenzwert sind nicht deckungsgleich — der
        Österreich-Umsatz ist Betriebseinnahme und zählt nur nicht gegen § 19."""
        at = _kunde(conn)
        rid = f.entwurf_anlegen(conn, at, _zeiten(conn, at))
        conn.execute(
            "UPDATE rechnung SET nummer = 'RE-2026-0001', datum = '2026-03-02',"
            " netto_cent = 900000, brutto_cent = 900000, status = 'FESTGESCHRIEBEN',"
            " pdf_sha256 = 'x', festgeschrieben_am = ? WHERE id = ?",
            (db.jetzt_utc(), rid),
        )
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art)"
            " VALUES (?, '2026-03-20', 900000, 'UEBERWEISUNG')", (rid,),
        )
        alle = conn.execute("SELECT SUM(betrag_cent) AS s FROM zahlung").fetchone()["s"]
        assert alle == 900000


class TestFrist:
    @pytest.mark.parametrize("leistung,frist", [
        ("2026-10-02", "2026-11-15"),
        ("2026-10-31", "2026-11-15"),
        ("2026-12-31", "2027-01-15"),   # über den Jahreswechsel
        ("2026-01-01", "2026-02-15"),
    ])
    def test_eu_frist_ist_der_fuenfzehnte_des_folgemonats(self, leistung, frist):
        """§ 14a Abs. 1 Satz 2 UStG — nicht sechs Monate.

        Direkt am Fristfall geprüft und nicht über ``offene_fristen``: dort
        entscheidet zusätzlich der Warnvorlauf, und der ist eine andere Frage.
        """
        fall = Fristfall(1, "Alpenbau GmbH", leistung, 1, 60, heute=leistung,
                         leistungsort="EU_B2B_REVERSE_CHARGE")
        assert fall.ist_eu
        assert fall.frist_am == frist
        assert fall.fundstelle == "§ 14a Abs. 1 Satz 2 UStG"

    def test_eu_frist_ist_zehnmal_kuerzer_als_die_inlandsfrist(self):
        """Der Punkt, auf den es ankommt: dieselbe Leistung, zwei Fristen."""
        eu = Fristfall(1, "Wien", "2026-03-02", 1, 60, heute="2026-03-02",
                       leistungsort="EU_B2B_REVERSE_CHARGE")
        inland = Fristfall(1, "Berlin", "2026-03-02", 1, 60, heute="2026-03-02")
        assert eu.frist_am == "2026-04-15"
        assert inland.frist_am == "2026-09-02"
        assert eu.tage_rest < inland.tage_rest / 3

    def test_eu_frist_warnt_frueh_genug(self, conn, betrieb_mit_rc):
        """Eine Leistung vom 2. Oktober muss bis zum 15. November draußen sein:
        34 Tage später. Bei zwanzig Tagen Vorlauf erscheint die Warnung am
        26. Oktober — nicht erst, wenn es zu spät ist."""
        at = _kunde(conn)
        _zeiten(conn, at, datum="2026-10-02")
        assert offene_fristen(conn, heute=date(2026, 10, 20)) == []
        assert len(offene_fristen(conn, heute=date(2026, 10, 27))) == 1

    def test_abgelaufene_eu_frist(self, conn, betrieb_mit_rc):
        at = _kunde(conn)
        _zeiten(conn, at, datum="2026-08-05")
        fall = offene_fristen(conn, heute=HEUTE)[0]
        assert fall.frist_am == "2026-09-15"
        assert fall.ueberschritten

    def test_inlandsfrist_bleibt_sechs_monate(self, conn, betrieb_mit_rc, kunde_id):
        _zeiten(conn, kunde_id, datum="2026-04-02")
        fall = offene_fristen(conn, heute=HEUTE)[0]
        assert not fall.ist_eu
        assert fall.frist_am == "2026-10-02"
        assert fall.fundstelle == "§ 14 Abs. 2 Satz 2 UStG"

    def test_knappste_frist_steht_oben(self, conn, betrieb_mit_rc, kunde_id):
        """Bei gemischten Fristen ist das älteste Datum nicht das dringendste."""
        _zeiten(conn, kunde_id, datum="2026-04-02")       # Inland, Frist 02.10.
        at = _kunde(conn)
        _zeiten(conn, at, datum="2026-09-20")             # EU, Frist 15.10.
        faelle = offene_fristen(conn, heute=date(2026, 10, 1))
        assert [fall.ist_eu for fall in faelle] == [False, True]
        assert faelle[0].tage_rest < faelle[1].tage_rest

    def test_privatkunde_im_eu_ausland_loest_nichts_aus(self, conn, betrieb_mit_rc):
        """§ 14a Abs. 1 greift bei Leistungen an UNTERNEHMER. Ein Privatkunde
        in Österreich wird ohnehin im Inland besteuert (§ 3a Abs. 1)."""
        privat = conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, land, typ,"
            " leistungsort, angelegt_am) VALUES ('K-0003', 'Frau Gruber', 'Weg 1',"
            " '1010', 'Wien', 'AT', 'PRIVAT', 'INLAND', ?)", (db.heute(),),
        ).lastrowid
        _zeiten(conn, privat, datum="2026-08-05")
        assert offene_fristen(conn, heute=HEUTE) == []


class TestStornoTraegtDenLeistungsort:
    """Ein Storno ist die Korrektur SEINES Originals, nicht ein neues Dokument.

    ``storniere()`` listet die zu kopierenden Spalten einzeln auf und hatte
    ``leistungsort`` vergessen. Die Vorbelegung INLAND griff — mit zwei Folgen:
    auf dem Papier fehlten der Reverse-Charge-Absatz und die USt-IdNr. des
    Empfängers, und der § 19-Umsatzzähler zog den Betrag von einer Summe ab, in
    der das Original nie gestanden hatte.
    """

    def test_leistungsort_betreff_und_rundungshinweis_wandern_mit(
        self, conn, betrieb_mit_rc, tmp_path
    ):
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        original = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()

        f.storniere(conn, rid, "Testkorrektur")
        storno = conn.execute("SELECT * FROM rechnung WHERE typ = 'STORNO'").fetchone()

        assert storno["leistungsort"] == original["leistungsort"] == "EU_B2B_REVERSE_CHARGE"
        assert storno["betreff"] == original["betreff"]
        assert storno["rundung_hinweis"] == original["rundung_hinweis"]
        assert storno["steuerregime"] == original["steuerregime"]

    def test_storno_zieht_nichts_vom_inlandsumsatz_ab(
        self, conn, betrieb_mit_rc, tmp_path
    ):
        """Der eigentliche Schaden: eine Grenze, die niedriger aussieht als sie ist."""
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        brutto = conn.execute(
            "SELECT brutto_cent FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()["brutto_cent"]
        conn.execute(
            "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, '2026-10-20', ?)",
            (rid, brutto),
        )
        assert grenzwert.vereinnahmt_cent(conn, 2026) == 0, "Auslandsumsatz zählt nicht"

        f.storniere(conn, rid, "Testkorrektur")
        assert grenzwert.vereinnahmt_cent(conn, 2026) == 0, (
            "und das Storno darf ihn erst recht nicht ins Minus ziehen"
        )

    def test_der_reverse_charge_absatz_steht_auch_auf_der_korrektur(
        self, conn, betrieb_mit_rc, tmp_path
    ):
        """Das Papier ist der Punkt: die Korrektur geht an denselben Kunden."""
        kid = _kunde(conn)
        rid = f.entwurf_anlegen(conn, kid, _zeiten(conn, kid))
        f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        f.storniere(conn, rid, "Testkorrektur")
        storno = conn.execute("SELECT id FROM rechnung WHERE typ = 'STORNO'").fetchone()["id"]

        daten = f.pdf_daten(conn, storno)
        assert daten["r"]["leistungsort"] == "EU_B2B_REVERSE_CHARGE"
        assert daten["empfaenger"]["ust_idnr"] == "ATU12345678"

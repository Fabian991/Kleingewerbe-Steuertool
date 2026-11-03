"""Regressionstests zu den Befunden der Fehleranalyse im Bereich rechtliche Konsistenz.

Je Befund mindestens ein Test, der das alte Fehlverhalten nachstellt:
G14 zaehlte EU-B2B- und Drittlandsumsatz in die § 19-Grenze und sprach nach
ueberschrittener Grenze weiter vom "grenzreissenden" Umsatz, ein unlesbares
Rechnungsdatum fuehrte zur 500, ein kompaktes Datum ("20261019") wurde roh
gespeichert und fiel aus EUeR und § 19-Zaehler, eine Reverse-Charge-Rechnung
ging ohne Leistungszeitpunkt durch, Geschenke ueber 50 € und eine
Homeoffice-Pauschale ueber 1.260 € minderten den Gewinn voll, G23 schrieb
"Eu B2B Reverse Charge", ein Beleg mit Anlagegut-Haken ohne Verzeichniseintrag
fehlte still in der EUeR, R06 verwies auf einen Leistungsnachweis, den es nicht
gibt, und G16 war toter Code.
"""

import re
import sqlite3
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import euer, grenzwert, pauschalen
from app.logik import festschreiben as fs
from app.logik import validierung as v
from app.main import erzeuge_app

HEUTE = db.heute()
JAHR = int(HEUTE[:4])


# ----------------------------------------------------------- Logik-Helfer

@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
    conn.execute(
        "UPDATE kunde SET satz_cent = 100000, angelegt_am = '2020-01-01' WHERE id = ?",
        (kunde_id,),
    )
    return kunde_id


def _kunde(conn, leistungsort, *, land="AT", ust_idnr="ATU12345678", name="Alpenbau GmbH"):
    return conn.execute(
        """
        INSERT INTO kunde (kundennummer, name, strasse, plz, ort, land, typ, ust_idnr,
                           leistungsort, satz_cent, angelegt_am)
        VALUES (?, ?, 'Ringstr. 1', '1010', 'Wien', ?, 'UNTERNEHMER', ?, ?, 100000,
                '2020-01-01')
        """,
        (f"K-{leistungsort[:3]}-{land}", name, land, ust_idnr, leistungsort),
    ).lastrowid


def _zeit(conn, kunde_id, minuten=60, datum=None):
    return int(conn.execute(
        """
        INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,
                                 erfasst_am)
        VALUES (?, 'Beratung', ?, ?, 'STUNDEN', ?)
        """,
        (kunde_id, datum or HEUTE, minuten, db.jetzt_utc()),
    ).lastrowid)


def _entwurf(conn, kunde_id, minuten=60, **rest):
    return fs.entwurf_anlegen(
        conn, kunde_id, [_zeit(conn, kunde_id, minuten)], datum=HEUTE, **rest
    )


def _vereinnahmt(conn, kunde_id, cent, datum=HEUTE):
    """Eine festgeschriebene, bezahlte Inlandsrechnung ueber ``cent``."""
    rid = conn.execute(
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                              netto_cent, brutto_cent, leistungsort, status,
                              festgeschrieben_am, erstellt_am)
        VALUES ('RECHNUNG', ?, 900, ?, ?, ?, ?, ?, 'INLAND', 'FESTGESCHRIEBEN', ?, ?)
        """,
        (f"ALT-{cent}", JAHR, kunde_id, datum, cent, cent, db.jetzt_utc(), db.jetzt_utc()),
    ).lastrowid
    conn.execute(
        "INSERT INTO zahlung (rechnung_id, datum, betrag_cent) VALUES (?, ?, ?)",
        (rid, datum, cent),
    )
    return rid


def _befunde(conn, rid, code):
    return [b for b in v.pruefe(conn, rid) if b.code == code]


# ---------------------------------------------------- 1: G14 nur Inland

class TestGrenzeNurInland:
    @pytest.mark.parametrize("ort, land", [
        ("EU_B2B_REVERSE_CHARGE", "AT"), ("DRITTLAND", "US"),
    ])
    def test_auslandsentwurf_loest_g14_nie_aus(self, conn, eingerichtet, ort, land):
        """90.000 € an einen Oesterreicher plus 20.000 € Inland: kein G14.

        Vorher addierte G14 die Entwurfssumme ungeachtet des Leistungsorts --
        anders als grenzwert.vereinnahmt_cent, das nur INLAND zaehlt.
        """
        _vereinnahmt(conn, eingerichtet, 2_000_000)
        kunde = _kunde(conn, ort, land=land, ust_idnr="ATU12345678" if land == "AT" else None)
        rid = _entwurf(conn, kunde, minuten=90 * 60)
        assert conn.execute(
            "SELECT brutto_cent, leistungsort FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()[:] == (9_000_000, ort)
        assert _befunde(conn, rid, "G14") == []

    def test_auslandsentwurf_auch_nach_ueberschrittener_grenze_still(
        self, conn, eingerichtet
    ):
        _vereinnahmt(conn, eingerichtet, 10_100_000)
        rid = _entwurf(conn, _kunde(conn, "EU_B2B_REVERSE_CHARGE"))
        assert _befunde(conn, rid, "G14") == []

    def test_inlandsentwurf_warnt_weiter(self, conn, eingerichtet):
        _vereinnahmt(conn, eingerichtet, 9_900_000)
        rid = _entwurf(conn, eingerichtet, minuten=2 * 60)
        (befund,) = _befunde(conn, rid, "G14")
        assert befund.stufe == v.GELB
        assert "käme der Jahresumsatz auf 101.000,00 €" in befund.text


# ------------------------------------- 2: G14 nach ueberschrittener Grenze

class TestGrenzeBereitsUeberschritten:
    def test_text_sagt_bereits_ueberschritten(self, conn, eingerichtet):
        """Gruendungsjahr: 26.000 € vereinnahmt, Grenze 25.000 €."""
        conn.execute("UPDATE betrieb SET beginn_am = ? WHERE id = 1", (f"{JAHR}-01-01",))
        _vereinnahmt(conn, eingerichtet, 2_600_000)
        rid = _entwurf(conn, eingerichtet)
        (befund,) = _befunde(conn, rid, "G14")
        assert befund.stufe == v.GELB, "kein Regelbesteuerungsmodus -- GELB, nicht ROT"
        assert befund.text == (
            "Die Grenze von 25.000,00 € (Gründungsjahr) ist mit 26.000,00 € bereits "
            "überschritten — seit dem grenzreißenden Umsatz gilt die Regelbesteuerung "
            "(§ 19 Abs. 1 Satz 3 UStG); diese Rechnung darf nicht ohne Umsatzsteuer "
            "ausgestellt werden."
        )
        assert "käme" not in befund.text

    def test_genau_auf_der_grenze_ist_nicht_ueberschritten(self, conn, eingerichtet):
        _vereinnahmt(conn, eingerichtet, 10_000_000)
        rid = _entwurf(conn, eingerichtet)
        (befund,) = _befunde(conn, rid, "G14")
        assert "käme der Jahresumsatz" in befund.text


# ------------------------------------------- 3: unlesbares Rechnungsdatum

class TestUnlesbaresDatum:
    def test_r04_statt_valueerror(self, conn, eingerichtet):
        rid = _entwurf(conn, eingerichtet)
        conn.execute("UPDATE rechnung SET datum = 'kaputt' WHERE id = ?", (rid,))
        codes = {b.code for b in v.pruefe(conn, rid)}
        assert "R04" in codes and "G14" not in codes


# ----------------------------------------- 4: EU-B2B ohne Leistungszeitpunkt

class TestReverseChargeOhneLeistungszeitpunkt:
    def test_ausgeblendet_gibt_g25(self, conn, eingerichtet):
        rid = _entwurf(conn, _kunde(conn, "EU_B2B_REVERSE_CHARGE"),
                       leistungszeitraum_zeigen=False)
        (befund,) = _befunde(conn, rid, "G25")
        assert befund.stufe == v.GELB
        assert befund.text.startswith(
            "Reverse-Charge-Rechnung ohne Leistungszeitpunkt: § 14a Abs. 1 Satz 1 UStG "
            "verweist auf die Angaben nach § 14 Abs. 4 (Nr. 6 Zeitpunkt der Leistung); "
            "§ 34a UStDV erleichtert nur die § 19-Rechnung"
        )

    def test_eingeblendet_still(self, conn, eingerichtet):
        rid = _entwurf(conn, _kunde(conn, "EU_B2B_REVERSE_CHARGE"),
                       leistungszeitraum_zeigen=True)
        assert _befunde(conn, rid, "G25") == []

    def test_inland_ohne_zeitraum_bleibt_still(self, conn, eingerichtet):
        """§ 34a UStDV: fuer die § 19-Rechnung ist der Zeitraum keine Pflicht."""
        rid = _entwurf(conn, eingerichtet, leistungszeitraum_zeigen=False)
        assert _befunde(conn, rid, "G25") == []


# ------------------------------------------------- 5: G23 lesbarer Ort

class TestG23Text:
    def test_leistungsort_als_wort(self, conn, eingerichtet):
        kunde = _kunde(conn, "EU_B2B_REVERSE_CHARGE", land="DE", ust_idnr="DE123456789",
                       name="Deutsch GmbH")
        rid = _entwurf(conn, kunde)
        (befund,) = _befunde(conn, rid, "G23")
        assert "steht aber auf EU-Unternehmer (Reverse Charge)." in befund.text
        assert "Eu B2B" not in befund.text

    def test_tabelle_deckt_sich_mit_dem_filter(self):
        from app.filter import STATUS_TEXTE

        for schluessel, text in v.LEISTUNGSORT_TEXT.items():
            assert STATUS_TEXTE[schluessel] == text


# ------------------------------------------------------ 6: R06 und G16

class TestR06UndG16:
    def test_r06_nennt_keinen_leistungsnachweis(self, conn, eingerichtet):
        zid = conn.execute(
            """
            INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,
                                     abrechnungsmodus, grund, satz_cent_snapshot, erfasst_am)
            VALUES (?, 'Nachbesserung', ?, 60, 'KULANZ', 'Nachbesserung', 9000, ?)
            """,
            (eingerichtet, HEUTE, db.jetzt_utc()),
        ).lastrowid
        rid = fs.entwurf_anlegen(conn, eingerichtet, [zid], datum=HEUTE)
        (befund,) = _befunde(conn, rid, "R06")
        assert "Leistungsnachweis" not in befund.text
        assert "berechnete Rechnung" in befund.text and "abschreiben" in befund.text

    def test_g16_gibt_es_nicht_mehr(self):
        import inspect

        assert "G16" not in inspect.getsource(v._plausibilitaet)


# --------------------------------------------- 7: Geschenke, Homeoffice

def _beleg(conn, schluessel, brutto, *, datum=f"{JAHR}-03-01"):
    kategorie = conn.execute(
        "SELECT id FROM euer_kategorie WHERE schluessel = ?", (schluessel,)
    ).fetchone()[0]
    beleg = conn.execute(
        """
        INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,
                           aufteilungsquote, betrag_betrieblich_cent, erfasst_am)
        VALUES ('EINGANG', ?, ?, ?, 100, ?, ?)
        """,
        (kategorie, datum, brutto, brutto, db.jetzt_utc()),
    ).lastrowid
    conn.execute(
        "INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
        (beleg, datum, -brutto),
    )
    return beleg


class TestGeschenke:
    def test_ueber_freigrenze_nicht_abziehbar(self, conn, betrieb):
        _beleg(conn, "GESCHENKE", 6000)
        a = euer.auswertung(conn, JAHR)
        assert [(z.kennzahl, z.betrag_cent, z.nachrichtlich, z.bezeichnung)
                for z in a.ausgaben] == [
            (164, 6000, True,
             "Geschenke über 50 € — nicht abziehbar, § 4 Abs. 5 Satz 1 Nr. 1 EStG"),
        ]
        assert a.ausgaben_cent == 0 and a.gewinn_cent == 0

    def test_genau_50_euro_bleibt_abziehbar(self, conn, betrieb):
        _beleg(conn, "GESCHENKE", 5000)
        a = euer.auswertung(conn, JAHR)
        assert [(z.kennzahl, z.betrag_cent, z.nachrichtlich) for z in a.ausgaben] == [
            (174, 5000, False),
        ]

    def test_grenze_kommt_aus_der_datei(self, conn, betrieb, monkeypatch):
        echt = pauschalen.fuer

        def andere(jahr, **kw):
            satz = echt(jahr, **kw)
            return pauschalen.Satz(satz.jahr, {**satz.werte,
                                               "geschenk_freigrenze_brutto_cent": 7000})

        monkeypatch.setattr(pauschalen, "fuer", andere)
        _beleg(conn, "GESCHENKE", 6000)
        assert [z.kennzahl for z in euer.auswertung(conn, JAHR).ausgaben] == [174]

    def test_buchungsblatt_stimmt_mit_der_euer(self, conn, betrieb):
        _beleg(conn, "GESCHENKE", 6000)
        (zeile,) = euer.buchungen(conn, JAHR)
        assert zeile["betrag_cent"] == 6000 and zeile["betriebsausgabe_cent"] == 0


class TestHomeoffice:
    def test_ueber_hoechstbetrag_gekappt(self, conn, betrieb):
        _beleg(conn, "HOMEOFFICE_PAUSCHALE", 90000, datum=f"{JAHR}-06-30")
        _beleg(conn, "HOMEOFFICE_PAUSCHALE", 60000, datum=f"{JAHR}-09-30")
        a = euer.auswertung(conn, JAHR)
        (zeile,) = a.ausgaben
        assert zeile.betrag_cent == 126000 and not zeile.nachrichtlich
        assert "gekappt auf 1.260 €, § 4 Abs. 5 Satz 1 Nr. 6c EStG" in zeile.bezeichnung
        assert a.ausgaben_cent == 126000

    def test_unter_hoechstbetrag_unveraendert(self, conn, betrieb):
        _beleg(conn, "HOMEOFFICE_PAUSCHALE", 100000)
        (zeile,) = euer.auswertung(conn, JAHR).ausgaben
        assert zeile.betrag_cent == 100000 and "gekappt" not in zeile.bezeichnung

    def test_buchungsblatt_kappt_ebenso(self, conn, betrieb):
        _beleg(conn, "HOMEOFFICE_PAUSCHALE", 90000, datum=f"{JAHR}-06-30")
        _beleg(conn, "HOMEOFFICE_PAUSCHALE", 60000, datum=f"{JAHR}-09-30")
        assert [z["betriebsausgabe_cent"] for z in euer.buchungen(conn, JAHR)] == [
            90000, 36000,
        ]


# ------------------------------- 8: Anlagegut-Haken ohne Verzeichniseintrag

def _anlagebeleg(conn, brutto=119000, datum=f"{JAHR}-03-02"):
    kategorie = conn.execute(
        "SELECT id FROM euer_kategorie WHERE schluessel = 'HARDWARE_ANLAGE'"
    ).fetchone()[0]
    beleg = conn.execute(
        """
        INSERT INTO beleg (art, kategorie_id, belegdatum, betrag_brutto_cent,
                           aufteilungsquote, betrag_betrieblich_cent, ist_anlagegut,
                           erfasst_am)
        VALUES ('EINGANG', ?, ?, ?, 100, ?, 1, ?)
        """,
        (kategorie, datum, brutto, brutto, db.jetzt_utc()),
    ).lastrowid
    conn.execute(
        "INSERT INTO zahlung (beleg_id, datum, betrag_cent) VALUES (?, ?, ?)",
        (beleg, datum, -brutto),
    )
    return beleg


class TestAnlagegutOhneVerzeichnis:
    def test_wird_gezaehlt(self, conn, betrieb):
        _anlagebeleg(conn)
        assert euer.auswertung(conn, JAHR).ausgaben == []
        luecke = euer.anlagegut_ohne_verzeichnis(conn, JAHR)
        assert (luecke.anzahl, luecke.betrag_cent) == (1, 119000)
        assert euer.anlagegut_ohne_verzeichnis(conn).anzahl == 1
        assert euer.anlagegut_ohne_verzeichnis(conn, JAHR - 1).anzahl == 0, (
            "ein Gut aus diesem Jahr fehlt im Vorjahr nicht"
        )

    def test_mit_verzeichniseintrag_keine_luecke(self, conn, betrieb):
        beleg = _anlagebeleg(conn)
        conn.execute(
            """
            INSERT INTO anlagegut (bezeichnung, anschaffung_am, ak_netto_cent, ak_brutto_cent,
                                   nutzungsdauer_jahre, methode, beleg_id, erfasst_am)
            VALUES ('Notebook', ?, 100000, 119000, 3, 'LINEAR', ?, ?)
            """,
            (f"{JAHR}-03-02", beleg, db.jetzt_utc()),
        )
        assert euer.anlagegut_ohne_verzeichnis(conn, JAHR).anzahl == 0


# --------------------------------------------------------- Weboberflaeche

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "inhaber": "E. Muster",
    "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
    "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1", "reset_jaehrlich": "1",
    "rundung_modus": "AUFWAERTS", "rundung_intervall_min": "15",
    "beginn_am": "2020-01-01",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "email": "buchhaltung@mustermann.example", "typ": "UNTERNEHMER", "land": "DE",
    "leistungsort": "INLAND", "satz_euro": "100,00", "gruppierung": "PRO_TITEL",
    "aktiv": "1",
}


@pytest.fixture
def daten(tmp_path, monkeypatch):
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "daten"))
    return tmp_path / "daten"


@pytest.fixture
def klient(daten):
    with TestClient(erzeuge_app(daten / "steuer.sqlite"), raise_server_exceptions=False) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        c.post("/kunden", data=KUNDE, follow_redirects=False)
        yield c


def _db(daten) -> sqlite3.Connection:
    conn = sqlite3.connect(daten / "steuer.sqlite")
    conn.row_factory = sqlite3.Row
    return conn


def _tag(tage_zurueck: int) -> date:
    return date.fromisoformat(HEUTE) - timedelta(days=tage_zurueck)


def _web_entwurf(klient, daten) -> int:
    antwort = klient.post("/zeiten", data={
        "kunde_id": 1, "titel": "Beratung", "datum": _tag(5).isoformat(), "dauer": "1h",
        "beschreibung": "", "modus": "STUNDEN", "grund": "",
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:300]
    conn = _db(daten)
    zid = conn.execute("SELECT MAX(id) FROM zeiteintrag").fetchone()[0]
    conn.close()
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": 1, "datum": _tag(1).isoformat(), "modus": "PRO_TITEL",
        "einleitung": "", "schluss": "", "zeit": [zid],
    }, follow_redirects=False)
    assert antwort.status_code == 303 and "fehler=" not in antwort.headers["location"]
    return int(re.search(r"/rechnung/(\d+)", antwort.headers["location"]).group(1))


def _web_festschreiben(klient, rid):
    seite = klient.get(f"/rechnung/{rid}").text
    version = re.search(r'name="version" value="(\d+)"', seite).group(1)
    codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
    antwort = klient.post(f"/rechnung/{rid}/festschreiben",
                          data={"version": version, "bestaetigt": codes},
                          follow_redirects=False)
    assert antwort.status_code == 303 and "fehler=" not in antwort.headers["location"], (
        antwort.headers.get("location")
    )


def _fehler(antwort) -> str:
    return parse_qs(urlparse(antwort.headers["location"]).query).get("fehler", [""])[0]


class TestKompaktesDatum:
    def test_kopf_speichert_iso(self, klient, daten):
        rid = _web_entwurf(klient, daten)
        kompakt = _tag(2).strftime("%Y%m%d")
        antwort = klient.post(f"/rechnung/{rid}/kopf", data={"datum": kompakt, "betreff": ""},
                              follow_redirects=False)
        assert antwort.status_code == 303 and not _fehler(antwort)
        conn = _db(daten)
        datum = conn.execute("SELECT datum FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        conn.close()
        assert datum == _tag(2).isoformat()

    def test_zahlung_mit_kompaktem_datum_zaehlt_in_euer_und_grenze(self, klient, daten):
        rid = _web_entwurf(klient, daten)
        _web_festschreiben(klient, rid)
        tag = _tag(0)
        antwort = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": tag.strftime("%Y%m%d"), "betrag_euro": "", "art": "UEBERWEISUNG",
        }, follow_redirects=False)
        assert antwort.status_code == 303 and not _fehler(antwort)
        conn = _db(daten)
        try:
            assert conn.execute("SELECT datum FROM zahlung").fetchone()[0] == tag.isoformat()
            assert grenzwert.vereinnahmt_cent(conn, tag.year) == 10000
            assert euer.auswertung(conn, tag.year).einnahmen_cent == 10000
            assert tag.year in euer.jahre(conn)
        finally:
            conn.close()

    def test_rueckzahlung_mit_kompaktem_datum(self, klient, daten):
        rid = _web_entwurf(klient, daten)
        _web_festschreiben(klient, rid)
        tag = _tag(0)
        klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": tag.isoformat(), "betrag_euro": "", "art": "UEBERWEISUNG",
        }, follow_redirects=False)
        antwort = klient.post(f"/rechnung/{rid}/storno",
                              data={"grund": "Probe", "zeiten_freigeben": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303 and "fehler=" not in antwort.headers["location"]
        antwort = klient.post(f"/rechnung/{rid}/rueckzahlung", data={
            "datum": tag.strftime("%Y%m%d"), "betrag_euro": "", "art": "UEBERWEISUNG",
        }, follow_redirects=False)
        assert antwort.status_code == 303 and not _fehler(antwort)
        conn = _db(daten)
        try:
            assert conn.execute(
                "SELECT datum FROM zahlung WHERE betrag_cent < 0"
            ).fetchone()[0] == tag.isoformat()
            assert grenzwert.vereinnahmt_cent(conn, tag.year) == 0
        finally:
            conn.close()


class TestAnlagegutHinweisInDerOberflaeche:
    def _markieren(self, daten):
        conn = _db(daten)
        try:
            _anlagebeleg(conn)
            conn.commit()
        finally:
            conn.close()

    def test_euer_seite_startseite_und_anlagen_im_singular(self, klient, daten):
        self._markieren(daten)
        satz = ("1 Beleg ist als Anlagegut markiert, aber noch nicht im Verzeichnis — "
                "1.190,00 € fehlen")
        for adresse in (f"/euer?jahr={JAHR}", "/"):
            seite = re.sub(r"\s+", " ", klient.get(adresse).text)
            assert satz in seite, adresse
            assert 'href="/anlagen"' in seite
        anlagen = re.sub(r"\s+", " ", klient.get("/anlagen").text)
        assert "1 Beleg ist als Anlagegut markiert, hat aber noch keinen Eintrag" in anlagen
        assert "1 Belege sind" not in anlagen

    def test_plural(self, klient, daten):
        self._markieren(daten)
        self._markieren(daten)
        seite = re.sub(r"\s+", " ", klient.get("/").text)
        assert ("2 Belege sind als Anlagegut markiert, aber noch nicht im Verzeichnis — "
                "2.380,00 € fehlen") in seite
        assert "2 Belege sind als Anlagegut markiert, haben aber" in re.sub(
            r"\s+", " ", klient.get("/anlagen").text)

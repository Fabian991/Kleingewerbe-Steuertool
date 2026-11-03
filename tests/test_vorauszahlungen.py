"""Vorauszahlungen je Termin: Fälligkeit, Stand „gezahlt“, die große Zahl, Speichern, Seite.

Die Schätzung rechnet wie in test_steuer.py: 2026, Brutto 48.000 €, Gewinn
13.330 €, Hebesatz 410 % -- Steuer auf den Gewinn 4.393,00 €. Heute ist der
25.10.2026: die Raten vom 10.03., 10.06. und 10.09. sind fällig, die vom
10.12. nicht.
"""

import json
import re
import sqlite3
from datetime import date
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import db
from app.logik import export_excel
from app.logik import steuerschaetzung as ss
from app.main import erzeuge_app
from tests.test_steuer import _einnahme_und_ausgabe

HEUTE = date(2026, 10, 25)
R = ss.Rate


def _schaetze(raten=(), *, heute=HEUTE, gewinn=1333000, **eingaben):
    werte = {"jahr": 2026, "brutto_lohn_cent": 4800000, "hebesatz_prozent": 410} | eingaben
    return ss.schaetze(ss.Eingaben(raten=tuple(raten), **werte), gewinn_bisher_cent=gewinn,
                       heute=heute)


def _vier(betrag, empfaenger="FINANZAMT", gezahlt=None):
    """Vier gleiche Raten; ``gezahlt`` setzt einzelne Haken: {3: False}."""
    return [R(empfaenger, q, betrag, (gezahlt or {}).get(q)) for q in range(1, 5)]


# ============================================================== Fälligkeit

class TestFaelligkeit:
    def test_wochenende_verschiebt(self):
        """15.11.2026 ist ein Sonntag: fällig am Montag, 16.11.2026 (§ 108 Abs. 3 AO)."""
        assert ss.faellig_am(2026, "GEMEINDE", 4) == date(2026, 11, 16)
        assert ss.faellig_am(2028, "FINANZAMT", 2) == date(2028, 6, 12)  # Samstag

    def test_feiertag_verschiebt(self):
        """10.06.2019 war Pfingstmontag: fällig am 11.06.2019."""
        assert ss.faellig_am(2019, "FINANZAMT", 2) == date(2019, 6, 11)
        assert ss.ist_feiertag(date(2026, 12, 25)) and not ss.ist_feiertag(date(2026, 12, 24))

    def test_werktag_bleibt(self):
        assert ss.faellig_am(2026, "FINANZAMT", 4) == date(2026, 12, 10)
        assert ss.faellig_am(2026, "GEMEINDE", 1) == date(2026, 2, 16)  # 15.02. Sonntag


class TestGezahlt:
    @pytest.mark.parametrize(("heute", "erwartet"), [
        (date(2026, 12, 9), False), (date(2026, 12, 10), True), (date(2026, 12, 11), True)])
    def test_ab_dem_faelligkeitstag(self, heute, erwartet):
        """Der Fälligkeitstag selbst zählt: die Lastschrift bucht an ihm ab."""
        (termin,) = ss.termine(2026, [R("FINANZAMT", 4, 100000)], heute)
        assert termin.gezahlt is erwartet and not termin.ueberfaellig

    def test_verschobener_termin(self):
        """Am Sonntag, 15.11.2026, ist die Gewerbesteuer noch nicht abgebucht."""
        (termin,) = ss.termine(2026, [R("GEMEINDE", 4, 50000)], date(2026, 11, 15))
        assert not termin.gezahlt
        (termin,) = ss.termine(2026, [R("GEMEINDE", 4, 50000)], date(2026, 11, 16))
        assert termin.gezahlt

    def test_haken_uebersteuert(self):
        nicht, schon = ss.termine(2026, [R("FINANZAMT", 3, 100000, gezahlt=False),
                                         R("FINANZAMT", 4, 100000, gezahlt=True)], HEUTE)
        assert (nicht.gezahlt, nicht.ueberfaellig, nicht.von_hand) == (False, True, True)
        assert (schon.gezahlt, schon.ueberfaellig) == (True, False)


# ============================================================ Große Zahl

class TestRuecklage:
    def test_vier_mal_tausend(self):
        """3.000 € gezahlt, 10.12. noch offen: mindestens 1.400 €, mit dem Bescheid 393 €.

        Vorher zog das Summenfeld alle 4.000 € sofort ab: 400 € statt 1.400 €.
        """
        r = _schaetze(_vier(100000))
        assert r.mehrsteuer_cent == 439300
        assert (r.vz_gezahlt_cent, r.vz_noch_faellig_cent) == (300000, 100000)
        assert r.reststeuer_cent == 139300
        assert r.offen_cent == 140000
        assert r.bescheid_cent == 39300 and r.zurueck_cent == 0
        assert [t.faellig for t in r.vz_offen] == [date(2026, 12, 10)]
        assert r.vz_abweichungen == ()

    def test_q3_nicht_gezahlt(self):
        r = _schaetze(_vier(100000, gezahlt={3: False}))
        assert r.offen_cent == 240000
        assert [t.faellig for t in r.ueberfaellig] == [date(2026, 9, 10)]

    def test_liquiditaetsfall(self):
        """4 × 1.200 € bei 4.393 € Steuer: am 10.12. werden 1.200 € abgebucht, nicht 793 €."""
        r = _schaetze(_vier(120000))
        assert r.reststeuer_cent == 79300
        assert r.offen_cent == 120000
        assert r.bescheid_cent == -40700 and r.zurueck_cent == 40700
        (abweichung,) = r.vz_abweichungen
        assert (abweichung.empfaenger, abweichung.betrag_cent, abweichung.zu_hoch) == (
            "FINANZAMT", 40700, True)
        assert abweichung.vorschrift == "§ 37 Abs. 3 EStG"

    def test_ueberzahlt(self):
        """4 × 1.500 €: schon 107 € zu viel, trotzdem 1.500 € fällig; 1.607 € kommen zurück."""
        r = _schaetze(_vier(150000))
        assert r.reststeuer_cent == -10700
        assert r.offen_cent == 150000 and r.zurueck_cent == 160700

    def test_planjahr_alles_kuenftig(self):
        r = _schaetze(_vier(100000), jahr=2027, heute=HEUTE)
        assert r.vz_gezahlt_cent == 0 and r.vz_noch_faellig_cent == 400000
        assert r.ueberfaellig == ()

    def test_gewerbesteuer_getrennt(self):
        """GewSt-Rate 15.11. (verschoben 16.11.) vor ESt 10.12.; Überhang nennt § 19 Abs. 3."""
        r = _schaetze(_vier(110000) + _vier(30000, "GEMEINDE"), heute=date(2026, 11, 1))
        assert [(t.empfaenger, t.faellig) for t in r.vz_offen] == [
            ("GEMEINDE", date(2026, 11, 16)), ("FINANZAMT", date(2026, 12, 10))]
        gemeinde = next(a for a in r.vz_abweichungen if a.empfaenger == "GEMEINDE")
        assert gemeinde.betrag_cent == 120000 and gemeinde.vorschrift == "§ 19 Abs. 3 GewStG"

    def test_zu_niedrig_festgesetzt(self):
        """4 × 300 € bei 4.393 € Steuer: Hinweis auf Nachzahlung."""
        (abweichung,) = _schaetze(_vier(30000)).vz_abweichungen
        assert abweichung.betrag_cent == 120000 - 439300 and not abweichung.zu_hoch

    def test_unter_100_euro_je_termin_kein_hinweis(self):
        assert _schaetze(_vier(115000)).vz_abweichungen == ()   # 207 € über 4 Termine

    def test_mit_eigenem_satz(self):
        """Gewinn × 30 % = 3.999 €: die große Zahl folgt derselben Regel, der Hinweis der Summe."""
        r = _schaetze(_vier(125000), satz_manuell_promille=300)
        assert r.mehrsteuer_cent == 399900
        assert r.offen_cent == 125000
        (abweichung,) = r.vz_abweichungen
        assert abweichung.empfaenger is None and abweichung.betrag_cent == 500000 - 399900
        assert "§ 19 Abs. 3 GewStG" in abweichung.vorschrift

    def test_weitere_ohne_termin_zaehlen_als_gezahlt(self):
        r = _schaetze(_vier(100000), vorauszahlungen_cent=20000)
        assert r.vz_gezahlt_cent == 320000 and r.offen_cent == 120000

    def test_ohne_raten_wie_bisher(self):
        """Nur der Altwert: Ergebnis wie vor Version 23 (Steuer minus gezahlt)."""
        r = _schaetze(vorauszahlungen_cent=100500)
        assert r.offen_cent == 339000 and r.zurueck_cent == 0
        assert _schaetze(vorauszahlungen_cent=600000).zurueck_cent == 160700

    def test_leere_euer_vergleicht_nicht(self):
        assert _schaetze(_vier(100000), gewinn=0).vz_abweichungen == ()


# ============================================================= Eingaben

class TestPruefe:
    def test_haken_nur_bei_abweichung_von_der_regel(self):
        e = ss.pruefe(2026, {"vz_finanzamt_2": "1.000", "vz_finanzamt_3": "1.000",
                             "vz_finanzamt_4": "1.000", "vz_gemeinde_4": "250,50"},
                      gezahlt=["FINANZAMT-2", "FINANZAMT-4", "GEMEINDE-1"], stand=HEUTE)
        assert e.raten == (
            R("FINANZAMT", 2, 100000, None),     # fällig und angehakt: Regel
            R("FINANZAMT", 3, 100000, False),    # fällig, nicht angehakt
            R("FINANZAMT", 4, 100000, True),     # künftig, angehakt
            R("GEMEINDE", 4, 25050, None),       # künftig, nicht angehakt
        )

    def test_formular_ueber_mitternacht(self):
        """Am 09.12. geladen, am 10.12. abgeschickt: die Dezember-Rate bleibt bei der Regel."""
        e = ss.pruefe(2026, {"vz_finanzamt_4": "1.000"}, gezahlt=[], stand=date(2026, 12, 9))
        assert e.raten == (R("FINANZAMT", 4, 100000, None),)
        (termin,) = ss.termine(2026, e.raten, date(2026, 12, 10))
        assert termin.gezahlt

    @pytest.mark.parametrize(("feld", "wert", "meldung"), [
        ("vz_finanzamt_3", "abc", "Vorauszahlung Finanzamt 10.09.: „abc“ ist kein Betrag."),
        ("vz_gemeinde_1", "-5", "Vorauszahlung Gemeinde 15.02.: ein negativer Betrag"),
    ])
    def test_fehler_im_feld(self, feld, wert, meldung):
        with pytest.raises(ss.Eingabefehler) as fehler:
            ss.pruefe(2026, {feld: wert}, stand=HEUTE)
        assert fehler.value.feld == feld and meldung in str(fehler.value)


# ============================================================== Speichern

class TestSpeichern:
    def test_hin_und_zurueck_mit_journal(self, conn):
        e = ss.Eingaben(jahr=2026, brutto_lohn_cent=4800000,
                        raten=(R("FINANZAMT", 3, 100000, False), R("FINANZAMT", 4, 100000)))
        assert ss.speichern(conn, e) is True
        assert ss.laden(conn, 2026) == e
        neu = ss.Eingaben(jahr=2026, brutto_lohn_cent=4800000,
                          raten=(R("FINANZAMT", 4, 120000),))
        assert ss.speichern(conn, neu) is True
        assert ss.laden(conn, 2026) == neu
        eintraege = {(z["feld"], z["wert_alt"], z["wert_neu"]) for z in conn.execute(
            "SELECT feld, wert_alt, wert_neu FROM journal WHERE objekttyp = 'steuerschaetzung'"
            " AND feld LIKE 'vz_%'")}
        assert ("vz_finanzamt_4", '{"betrag_cent": 100000, "gezahlt": null}',
                '{"betrag_cent": 120000, "gezahlt": null}') in eintraege
        assert ("vz_finanzamt_3", '{"betrag_cent": 100000, "gezahlt": false}', None) in eintraege
        assert ss.speichern(conn, neu) is False

    def test_nur_raten_geaendert(self, conn):
        ss.speichern(conn, ss.Eingaben(jahr=2026, brutto_lohn_cent=4800000))
        assert ss.speichern(conn, ss.Eingaben(jahr=2026, brutto_lohn_cent=4800000,
                                              raten=(R("GEMEINDE", 1, 5000),))) is True

    def test_loeschen_nimmt_die_raten_mit(self, conn):
        ss.speichern(conn, ss.Eingaben(jahr=2026, raten=tuple(_vier(100000))))
        ss.loeschen(conn, 2026)
        assert conn.execute("SELECT COUNT(*) FROM steuer_vorauszahlung").fetchone()[0] == 0
        alt = json.loads(conn.execute(
            "SELECT wert_alt FROM journal WHERE aktion = 'LOESCHEN'"
            " AND objekttyp = 'steuerschaetzung'").fetchone()[0])
        assert len(alt["raten"]) == 4 and alt["raten"][0]["betrag_cent"] == 100000

    @pytest.mark.parametrize(("spalte", "wert"), [
        ("empfaenger", "BUND"), ("quartal", 5), ("betrag_cent", 0), ("gezahlt", 2)])
    def test_datenbank_weist_unsinn_ab(self, conn, spalte, wert):
        ss.speichern(conn, ss.Eingaben(jahr=2026))
        zeile = {"jahr": 2026, "empfaenger": "FINANZAMT", "quartal": 1, "betrag_cent": 100,
                 "geaendert_am": "x"} | {spalte: wert}
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO steuer_vorauszahlung ({', '.join(zeile)})"
                         f" VALUES ({', '.join('?' * len(zeile))})", tuple(zeile.values()))

    def test_erinnerungen(self, conn):
        """14 Tage vorher die nächste Rate, dazu jede als nicht gezahlt markierte."""
        ss.speichern(conn, ss.Eingaben(jahr=2026, raten=tuple(_vier(100000, gezahlt={3: False}))))
        am_25_10 = ss.erinnerungen(conn, HEUTE)
        assert [(t.faellig, t.ueberfaellig) for t in am_25_10] == [(date(2026, 9, 10), True)]
        am_26_11 = ss.erinnerungen(conn, date(2026, 11, 26))
        assert [t.faellig for t in am_26_11] == [date(2026, 9, 10), date(2026, 12, 10)]
        im_januar = ss.erinnerungen(conn, date(2027, 1, 5))
        assert [t.faellig for t in im_januar] == [date(2026, 9, 10)]


# =============================================================== Migration

class TestMigration:
    def test_version_23_behaelt_den_altwert(self, tmp_path):
        """Stand 20 mit 1.200 € im alten Summenfeld: danach Tabelle da, Wert und Ergebnis gleich."""
        pfad = tmp_path / "alt.sqlite"
        verbindung = db.oeffne(pfad)
        verbindung.execute("DROP TABLE steuer_vorauszahlung")
        verbindung.execute("DROP TRIGGER kunde_kein_delete_mit_beleg")
        verbindung.execute(
            "INSERT INTO steuerschaetzung (jahr, brutto_lohn_cent, vorauszahlungen_cent,"
            " hebesatz_prozent, geaendert_am) VALUES (2026, 4800000, 120000, 410, 'x')")
        # Ein Kunde mit festgeschriebener Rechnung: bleibt, und danach sperrt der Trigger.
        verbindung.execute(
            "INSERT INTO kunde (id, kundennummer, name, strasse, plz, ort, angelegt_am)"
            " VALUES (7, 'K-0007', 'Beispiel GmbH', 'Musterstraße 1', '10115', 'Berlin', 'x')")
        verbindung.execute(
            "INSERT INTO rechnung (typ, nummer, kunde_id, datum, netto_cent, brutto_cent,"
            " status, festgeschrieben_am, erstellt_am) VALUES ('RECHNUNG', 'RE-2026-0001', 7,"
            " '2026-03-31', 10000, 10000, 'FESTGESCHRIEBEN', '2026-03-31', '2026-03-31')")
        verbindung.execute("DELETE FROM schema_version")
        verbindung.execute("INSERT INTO schema_version VALUES (20, 'x')")
        verbindung.commit()
        verbindung.close()
        # Bis Version 22: offen = Steuer auf den Gewinn minus Vorauszahlungen, aufgerundet.
        vorher = ss.aufrunden(439300 - 120000)

        verbindung = db.oeffne(pfad)
        try:
            assert verbindung.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0] == db.SCHEMA_VERSION
            assert db.SCHEMA_VERSION >= 23
            spalten = {z["name"] for z in verbindung.execute(
                "PRAGMA table_info(steuer_vorauszahlung)")}
            assert {"jahr", "empfaenger", "quartal", "betrag_cent", "gezahlt"} <= spalten
            eingaben = ss.laden(verbindung, 2026)
            assert eingaben.vorauszahlungen_cent == 120000 and eingaben.raten == ()
            nachher = ss.schaetze(eingaben, gewinn_bisher_cent=1333000, heute=HEUTE).offen_cent
            assert nachher == vorher == 320000
            assert verbindung.execute(
                "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION' AND objekt_id = 23"
            ).fetchone()[0] == 1
            assert verbindung.execute(
                "SELECT name FROM kunde WHERE id = 7").fetchone()[0] == "Beispiel GmbH"
            verbindung.execute("PRAGMA foreign_keys = OFF")
            with pytest.raises(sqlite3.IntegrityError, match="stilllegen statt loeschen"):
                verbindung.execute("DELETE FROM kunde WHERE id = 7")
        finally:
            verbindung.close()


def test_excel_export(conn, betrieb):
    ss.speichern(conn, ss.Eingaben(jahr=2026, raten=(R("GEMEINDE", 4, 25050, False),)))
    blatt = load_workbook(BytesIO(export_excel.als_bytes(conn)))["Vorauszahlungen"]
    kopf = [z.value for z in blatt[1]]
    zeile = dict(zip(kopf, [z.value for z in blatt[2]], strict=True))
    assert zeile["Empfänger"] == "Gemeinde (Gewerbesteuer)"
    assert zeile["Termin laut Gesetz"] == "15.11.2026"
    assert zeile["Betrag"] == 250.5
    assert zeile["Gezahlt (von Hand gesetzt; leer = ab Fälligkeit)"] == "nein"


# =============================================================== Oberfläche

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}


@pytest.fixture
def heute():
    return {"wert": "2026-10-25"}


@pytest.fixture
def klient(tmp_path, monkeypatch, heute):
    monkeypatch.setattr(db, "heute", lambda: heute["wert"])
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1500000, 167000)
        yield c


def _seite(c, **daten):
    daten = {"jahr": "2026", "brutto_lohn": "48.000", "vz_stand": "2026-10-25"} | daten
    antwort = c.post("/steuer", data=daten, follow_redirects=False)
    if antwort.status_code == 303:
        return c.get(antwort.headers["location"]).text
    return antwort.text


def _klar(html: str) -> str:
    ohne = re.sub(r"</?(?:strong|em|b|i|a|span|small)\b[^>]*>", "", html)
    return " ".join(re.sub(r"<[^>]+>", " ", ohne).split())


VIER_MAL_TAUSEND = {f"vz_finanzamt_{q}": "1.000" for q in range(1, 5)} | {
    "vz_gezahlt": ["FINANZAMT-1", "FINANZAMT-2", "FINANZAMT-3"]}


class TestSeite:
    def test_haken_und_faelligkeit(self, klient):
        text = klient.get("/steuer?jahr=2026").text
        for q in (1, 2, 3):
            assert re.search(rf'value="FINANZAMT-{q}"\s+checked>\s*<span>gezahlt</span>', text)
        assert re.search(r'value="FINANZAMT-4">\s*<span>schon gezahlt</span>', text)
        assert "4. Quartal · 10.12.2026" in text
        assert 'name="vz_stand" value="2026-10-25"' in text

    def test_aufteilung_unter_der_grossen_zahl(self, klient):
        text = _seite(klient, **VIER_MAL_TAUSEND)
        assert re.search(r'<div class="wert">1.400 €</div>', text)
        klar = _klar(text)
        assert ("Steuer auf den Gewinn 4.393,00 € minus schon vorausgezahlt 3.000,00 € = "
                "1.393,00 €") in klar
        assert "davon fällig am 10.12.2026 (Finanzamt): 1.000,00 €" in klar
        assert "voraussichtlich mit dem Bescheid: 393,00 €" in klar
        tfoot = _klar(re.search(r"<tfoot>(.*?)</tfoot>", text, re.S).group(1))
        assert tfoot.endswith("Noch fällige Vorauszahlungen −1.000,00 € "
                              "Voraussichtlich mit dem Bescheid 393,00 €")

    def test_gespeicherte_raten_stehen_wieder_da(self, klient):
        _seite(klient, **VIER_MAL_TAUSEND)
        text = klient.get("/steuer?jahr=2026").text
        for q in range(1, 5):
            assert re.search(rf'name="vz_finanzamt_{q}"[^>]*value="1.000,00"', text)

    def test_liquiditaet_und_herabsetzung(self, klient):
        daten = {f"vz_finanzamt_{q}": "1.200" for q in range(1, 5)} | {
            "vz_gezahlt": ["FINANZAMT-1", "FINANZAMT-2", "FINANZAMT-3"]}
        text = _seite(klient, **daten)
        assert re.search(r'<div class="wert">1.200 €</div>', text)
        klar = _klar(text)
        assert "voraussichtlich mit dem Bescheid zurück: 407,00 €" in klar
        assert "Herabsetzung der Vorauszahlungen beantragen (§ 37 Abs. 3 EStG)" in klar
        assert "um etwa 407 € über der Schätzung" in klar

    def test_ueberfaellige_rate(self, klient):
        daten = VIER_MAL_TAUSEND | {"vz_gezahlt": ["FINANZAMT-1", "FINANZAMT-2"]}
        klar = _klar(_seite(klient, **daten))
        assert "Die Rate vom 10.09.2026 an das Finanzamt (1.000,00 €) ist als nicht gezahlt" in klar
        assert "§ 240 AO" in klar

    def test_fehler_markiert_genau_das_feld(self, klient):
        antwort = klient.post("/steuer", data={"jahr": "2026", "brutto_lohn": "48.000",
                                               "vz_finanzamt_3": "abc",
                                               "vz_finanzamt_4": "1.000",
                                               "vz_stand": "2026-10-25"})
        assert antwort.status_code == 422
        assert re.search(r'name="vz_finanzamt_3"[^>]*aria-invalid="true"', antwort.text)
        assert len(re.findall(r'aria-invalid="true"', antwort.text)) == 1
        # Die anderen Eingaben bleiben stehen, die Haken so, wie sie kamen.
        assert re.search(r'name="vz_finanzamt_4"[^>]*value="1.000"', antwort.text)
        assert re.search(r'value="FINANZAMT-1">', antwort.text)

    def test_gemeinde_eingeklappt_bis_es_gewerbesteuer_gibt(self, klient):
        text = klient.get("/steuer?jahr=2026").text
        assert '<details class="raten-gemeinde">' in text
        text = _seite(klient, vz_gemeinde_4="250")
        assert '<details class="raten-gemeinde" open>' in text

    def test_altwert_neben_raten(self, klient):
        text = _klar(_seite(klient, vorauszahlungen="1.200", vz_finanzamt_4="1.000"))
        assert "verteile ihn auf die Quartale und leere dieses Feld" in text
        assert "verteile ihn" not in _seite(klient, vorauszahlungen="1.200",
                                            vz_finanzamt_4="")

    def test_vorjahr_uebernehmen(self, klient):
        _seite(klient, **VIER_MAL_TAUSEND)
        text = klient.get("/steuer?jahr=2027").text
        knopf = re.search(r"<button[^>]*data-vorjahr='([^']*)'[^>]*>([^<]*)", text)
        assert knopf and "Beträge" in knopf.group(2) and "2026" in knopf.group(2)
        werte = json.loads(knopf.group(1))
        assert werte == {f"vz_finanzamt_{q}": "1.000,00" for q in range(1, 5)}
        # Übernommen ist damit noch nichts.
        assert not re.search(r'name="vz_finanzamt_1"[^>]*value="1.000,00"', text)

    def test_uebersicht_nennt_die_naechste_rate(self, klient, heute):
        _seite(klient, **VIER_MAL_TAUSEND)
        assert "nächste Vorauszahlung" not in _klar(klient.get("/").text)
        heute["wert"] = "2026-11-26"
        klar = _klar(klient.get("/").text)
        assert "nächste Vorauszahlung 10.12.: 1.000 € (Finanzamt)" in klar

    def test_uebersicht_nennt_die_ueberfaellige(self, klient):
        _seite(klient, **VIER_MAL_TAUSEND | {"vz_gezahlt": ["FINANZAMT-1", "FINANZAMT-2"]})
        klar = _klar(klient.get("/").text)
        assert "Rate vom 10.09. (Finanzamt, 1.000 €) nicht gezahlt" in klar
        # Die große Zahl der Übersicht folgt derselben Regel.
        assert "Steuerrücklage 2026 2.400 € zurücklegen" in klar

    def test_uebersicht_auch_ohne_ruecklage(self, klient):
        """Raten ohne Angabe zum Gehalt: keine Rücklage, die Erinnerung trotzdem."""
        _seite(klient, brutto_lohn="", **VIER_MAL_TAUSEND | {"vz_gezahlt": ["FINANZAMT-1"]})
        klar = _klar(klient.get("/").text)
        assert "Vorauszahlungen 2026 Rate vom 10.06. (Finanzamt, 1.000 €) nicht gezahlt · " \
               "Rate vom 10.09. (Finanzamt, 1.000 €) nicht gezahlt" in klar

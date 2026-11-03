"""Rechnungen in Fremdwaehrung: EZB-Referenzkurs, nachpruefbar, Kartenbetrag.

Der Nutzer bekommt oft Rechnungen in US-Dollar, manchmal in Pfund, und will
sie einfach hochladen. Die Entscheidungen dazu stehen in app/logik/kurse.py
(die Kurstabelle) und app/logik/beleg/umrechnung.py (was am Beleg landet).
Kein Test geht ins Netz: die Kurstabelle ist eine kleine Pruefdatei mit
echten Kursen (tests/fixtures/eurofxref-auszug.csv), und tests/conftest.py
ersetzt den Abruf durch einen, der sofort scheitert.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import anlagen, euer, export_excel, kurse
from app.logik.beleg import abholen, umrechnung
from app.logik.beleg import pipeline as p
from app.logik.beleg.ergebnis import BelegFehler
from app.main import erzeuge_app
from tests.test_belegordner import HETZNER_KOPF, cii, lege, pdf_aus_zeilen
from tests.test_belegordner import lieferant as bekannter_lieferant

FIXTURES = Path(__file__).resolve().parent / "fixtures"
KURS_CSV = FIXTURES / "eurofxref-auszug.csv"
HEUTE = date(2026, 4, 14)


def ezb_zip(text: str | None = None, name: str = "eurofxref-hist.csv") -> bytes:
    """Die Pruefdatei als ZIP, so wie die EZB sie ausliefert."""
    puffer = io.BytesIO()
    # Feste Zeit im Archiv: sonst ergaebe derselbe Inhalt je Sekunde andere Bytes.
    info = zipfile.ZipInfo(name, date_time=(2026, 4, 10, 16, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(puffer, "w") as archiv:
        archiv.writestr(info, KURS_CSV.read_text(encoding="utf-8") if text is None else text)
    return puffer.getvalue()


def csv_voll(ohne: str = "") -> str:
    """Die Pruefdatei, nach hinten verlaengert bis zum ersten Tag der
    Euro-Referenzkurse (1999-01-04) -- so lang, wie ein ERSTER Abruf sie
    mindestens bringen muss (``kurse.mindestumfang``). Ab 2002 jeder
    TARGET-Geschaeftstag, mit gleichbleibenden, plausiblen Kursen. ``ohne``:
    diese Waehrung fehlt am juengsten Tag."""
    zeilen = KURS_CSV.read_text(encoding="utf-8").splitlines()
    kopf, neu = zeilen[0], zeilen[1:]
    if ohne:
        spalte = kopf.split(",").index(ohne)
        felder = neu[0].split(",")
        felder[spalte] = "N/A"
        neu[0] = ",".join(felder)
    alt = []
    tag = date(2026, 2, 25)
    while tag >= kurse.LUECKENLOS_AB:
        if not kurse.target_geschlossen(tag):
            alt.append(f"{tag.isoformat()},1.1,130.0,0.8,1.1,N/A,N/A,")
        tag -= timedelta(days=1)
    alt.append("1999-01-04,1.1789,133.73,0.7111,1.6168,N/A,N/A,")
    return "\n".join([kopf, *neu, *alt]) + "\n"


def tabelle_ablegen(datenordner: Path, *, jetzt: datetime | None = None) -> kurse.Tabelle:
    """Legt die Pruefdatei als aktuelle Kurstabelle in den Datenordner."""
    return kurse.speichere(datenordner, ezb_zip(), jetzt=jetzt, heute=HEUTE)


@pytest.fixture
def tabelle() -> kurse.Tabelle:
    return kurse.tabelle_aus_bytes(ezb_zip(), heute=HEUTE)


# ================================================================ Kurstabelle

class TestCsv:
    def test_liest_die_echte_form_mit_na_und_abschliessendem_komma(self, tabelle):
        """Kopfzeile "Date,USD,...," mit leerer letzter Spalte, "N/A" an Tagen
        ohne Kurs: RUB hat in der ganzen Datei keinen, ISK ab dem 04.03."""
        assert tabelle.erster_tag == date(2026, 2, 26)
        assert tabelle.letzter_tag == date(2026, 4, 10)
        assert tabelle.waehrungen == ("CHF", "GBP", "ISK", "JPY", "RUB", "USD")
        assert tabelle.reihen["RUB"] == ((), ())
        assert tabelle.reihen["ISK"][0][-1] == date(2026, 3, 3)
        tage, werte = tabelle.reihen["USD"]
        # Alle Stellen, genau wie die EZB sie schreibt.
        assert werte[tage.index(date(2026, 3, 18))] == "1.15"

    @pytest.mark.parametrize("text, meldung", [
        ("Datum,USD\n2026-03-13,1.1476\n", "beginnt nicht mit „Date“"),
        ("Date,usd\n2026-03-13,1.1476\n", "keine gültigen Währungscodes"),
        ("Date,GBP\n2026-03-13,0.86\n", "keinen Kurs für US-Dollar"),
        ("Date,USD\n13.03.2026,1.1476\n", "kein Datum"),
        ("Date,USD\n2026-02-30,1.1476\n", "ist kein Datum"),
        ("Date,USD\n2026-03-13,-1.1\n", "ist kein Kurs"),
        ("Date,USD\n2026-03-13,0\n", "kein möglicher Kurs"),
        ("Date,USD\n2026-03-13,11.476\n", "unplausibel"),
        ("Date,USD\n2026-03-13,1.14\n2026-03-13,1.15\n", "zweimal"),
        ("Date,USD,GBP\n2026-03-13,1.14\n", "Felder statt"),
        ("Date,USD,GBP\n2026-03-13,N/A,0.86\n", "fehlt der US-Dollar"),
        ("Date,USD\n2031-03-13,1.14\n", "außerhalb"),
        ("", "leer"),
    ])
    def test_kaputter_inhalt_wird_abgewiesen(self, text, meldung):
        """Kopfzeile, Datumsformat, positive und plausible Kurse: was nicht
        stimmt, ist nicht die Kurstabelle der EZB und wird nicht benutzt."""
        with pytest.raises(kurse.TabelleUngueltig, match=meldung):
            kurse.lies_csv(text, heute=HEUTE)

    def test_kein_zip_und_falscher_inhalt_im_zip(self):
        """Eine HTML-Fehlerseite statt des Archivs, ein Archiv mit zwei Dateien,
        ein beschaedigtes Archiv -- alles wird abgewiesen."""
        with pytest.raises(kurse.TabelleUngueltig, match="kein ZIP"):
            kurse.entpacke(b"<html>Wartungsarbeiten</html>")
        puffer = io.BytesIO()
        with zipfile.ZipFile(puffer, "w") as archiv:
            archiv.writestr("a.csv", "Date,USD\n")
            archiv.writestr("b.csv", "Date,USD\n")
        with pytest.raises(kurse.TabelleUngueltig, match="nicht genau eine CSV"):
            kurse.entpacke(puffer.getvalue())
        with pytest.raises(kurse.TabelleUngueltig, match="beschädigt"):
            kurse.entpacke(ezb_zip()[:200])


class TestKursZumTag:
    def test_werktag_hat_seinen_eigenen_kurs(self, tabelle):
        """Am Geschaeftstag gilt sein eigener Kurs."""
        kurs = kurse.kurs_aus_tabelle(tabelle, "USD", date(2026, 3, 13))
        assert (kurs.kurs, kurs.kursdatum, kurs.quelle) == ("1.1476", date(2026, 3, 13), "EZB")
        assert kurs.sha256 == tabelle.sha256

    def test_wochenende_nimmt_den_letzten_kurs_davor(self, tabelle):
        """Samstag und Sonntag: der Kurs vom Freitag -- und das tatsaechlich
        verwendete Kursdatum steht im Ergebnis."""
        for tag in (date(2026, 3, 14), date(2026, 3, 15)):
            kurs = kurse.kurs_aus_tabelle(tabelle, "USD", tag)
            assert (kurs.kurs, kurs.kursdatum, kurs.stichtag) == ("1.1476", date(2026, 3, 13), tag)

    def test_feiertag_karfreitag_bis_ostermontag(self, tabelle):
        """Ostern 2026: Karfreitag (03.04.) und Ostermontag (06.04.) sind
        TARGET-Feiertage. Es gilt der Kurs vom Gruendonnerstag."""
        for tag in (date(2026, 4, 3), date(2026, 4, 5), date(2026, 4, 6)):
            assert kurse.kurs_aus_tabelle(tabelle, "GBP", tag).kursdatum == date(2026, 4, 2)
        assert kurse.kurs_aus_tabelle(tabelle, "GBP", date(2026, 4, 7)).kurs == "0.87258"

    def test_luecke_ueber_zehn_tage_ist_ein_fehler(self, tabelle):
        """ISK hat in der Pruefdatei ab dem 04.03. keinen Kurs: sieben Tage
        spaeter gilt noch der vom 03.03., nach mehr als zehn Tagen nicht mehr."""
        assert kurse.kurs_aus_tabelle(tabelle, "ISK", date(2026, 3, 13)).kursdatum == \
            date(2026, 3, 3)
        with pytest.raises(kurse.KeinKurs, match="seit dem 03.03.2026") as fehler:
            kurse.kurs_aus_tabelle(tabelle, "ISK", date(2026, 3, 14))
        assert fehler.value.grund == "luecke"
        with pytest.raises(kurse.KeinKurs) as fehler:
            kurse.kurs_aus_tabelle(tabelle, "RUB", date(2026, 3, 14))
        assert fehler.value.grund == "luecke"

    def test_unbekannte_waehrung_braucht_einen_kurs_von_hand(self, tabelle):
        """Eine Waehrung, die die EZB nicht fuehrt."""
        with pytest.raises(kurse.KeinKurs, match="Für AED veröffentlicht die EZB keinen") \
                as fehler:
            kurse.kurs_aus_tabelle(tabelle, "AED", date(2026, 3, 13))
        assert fehler.value.grund == "unbekannt"
        assert fehler.value.grund in umrechnung.MANUELL_ERLAUBT

    def test_nach_dem_letzten_tabellentag(self, tabelle):
        """Nach dem 10.04. (Freitag): fuer Samstag und Sonntag gilt der Freitag
        -- dazwischen liegt kein Geschaeftstag. Fuer Montag fehlt der Kurs noch."""
        assert kurse.kurs_aus_tabelle(tabelle, "USD", date(2026, 4, 12)).kursdatum == \
            date(2026, 4, 10)
        with pytest.raises(kurse.KeinKurs, match="13.04.2026 ist noch nicht veröffentlicht") \
                as fehler:
            kurse.kurs_aus_tabelle(tabelle, "USD", date(2026, 4, 14))
        assert fehler.value.grund == "noch_nicht"


class TestBesorgen:
    def test_zukunft_wird_erst_aktualisiert_dann_gemeldet(self, tmp_path, monkeypatch):
        """Ein Datum nach dem letzten Tabellentag: erst wird die Tabelle
        aktualisiert (hier: es gibt nichts Neueres), dann heisst es "noch nicht
        veröffentlicht". Ein Datum in der Zukunft hat ohnehin keinen Kurs."""
        tabelle_ablegen(tmp_path, jetzt=datetime(2026, 4, 13, 6, tzinfo=UTC))
        abrufe = []

        def abruf(url):
            abrufe.append(url)
            return ezb_zip()

        monkeypatch.setattr(kurse, "netzabruf", abruf)
        with pytest.raises(kurse.KeinKurs, match="Kurs für den 13.04.2026 ist noch nicht"):
            kurse.kurs_besorgen(tmp_path, "USD", date(2026, 4, 13), heute=HEUTE)
        assert abrufe == [kurse.QUELLE]
        with pytest.raises(kurse.KeinKurs, match="Zukunft") as fehler:
            kurse.kurs_besorgen(tmp_path, "USD", date(2026, 4, 15), heute=HEUTE)
        assert fehler.value.grund == "zukunft"

    def test_ohne_tabelle_wird_bei_bedarf_geladen(self, tmp_path, monkeypatch):
        """Die erste Rechnung in USD: noch keine Tabelle -- sie wird geladen."""
        voll = ezb_zip(csv_voll())
        monkeypatch.setattr(kurse, "netzabruf", lambda url: voll)
        kurs = kurse.kurs_besorgen(tmp_path, "USD", date(2026, 3, 14), heute=HEUTE)
        assert kurs.kurs == "1.1476"
        assert (tmp_path / "kurse" / "eurofxref-hist.zip").read_bytes() == voll

    def test_offline_ohne_tabelle_meldet_es_deutlich(self, tmp_path):
        """Kein Netz und noch keine Tabelle: eine Meldung, die das sagt. Der
        naechste Versuch wartet nicht erneut auf das Netz."""
        with pytest.raises(kurse.KeinKurs) as fehler:
            kurse.kurs_besorgen(tmp_path, "USD", date(2026, 3, 14), heute=HEUTE)
        # Ein Satz, klein weiter nach dem Doppelpunkt, ohne doppeltes
        # "Die Kurstabelle ließ sich nicht laden".
        assert str(fehler.value) == ("Die EZB-Kurstabelle ist noch nicht geladen: kein Netz "
                                     "im Test.")
        assert fehler.value.grund == "keine_tabelle"
        assert kurse.letzter_fehlschlag(tmp_path)

    def test_offline_mit_veralteter_tabelle_sagt_was_los_ist(self, tmp_path):
        """Offline, die Tabelle endet am 10.04., gefragt ist der 14.04.: nicht
        "noch nicht veröffentlicht" (der Kurs vom 13.04. ist es laengst),
        sondern dass die Tabelle sich nicht aktualisieren liess. Ein Kurs von
        Hand gilt dann wie ganz ohne Tabelle."""
        tabelle_ablegen(tmp_path, jetzt=datetime(2026, 4, 11, tzinfo=UTC))
        with pytest.raises(kurse.KeinKurs) as fehler:
            kurse.kurs_besorgen(tmp_path, "USD", date(2026, 4, 14), heute=HEUTE)
        assert str(fehler.value) == (
            "Die EZB-Kurstabelle reicht bis 10.04.2026 und ließ sich nicht aktualisieren: kein "
            "Netz im Test. Den Kurs für den 14.04.2026 kennt das Tool deshalb noch nicht.")
        assert fehler.value.grund == "nicht_aktuell"
        assert fehler.value.grund in umrechnung.MANUELL_ERLAUBT

    def test_offline_mit_tabelle_geht_es_weiter(self, tmp_path):
        """Ohne Netz, aber mit vorhandener Tabelle: der Kurs kommt aus ihr."""
        tabelle_ablegen(tmp_path)
        assert kurse.kurs_besorgen(tmp_path, "GBP", date(2026, 3, 14),
                                   heute=HEUTE).kurs == "0.86503"


class TestAbruf:
    def test_abruf_scheitert_vorhandene_tabelle_bleibt(self, tmp_path):
        """Offline: aktualisiere() meldet den Grund, die Tabelle bleibt."""
        vorher = tabelle_ablegen(tmp_path)
        with pytest.raises(kurse.AbrufFehler, match="kein Netz"):
            kurse.aktualisiere(tmp_path)
        assert kurse.tabelle(tmp_path).sha256 == vorher.sha256

    def test_kaputte_datei_wird_nicht_gespeichert(self, tmp_path):
        """Eine Fehlerseite oder eine abgeschnittene Datei ersetzt die
        vorhandene Tabelle nicht."""
        vorher = tabelle_ablegen(tmp_path)
        for kaputt in (b"<html>Wartung</html>", ezb_zip("Date,USD\n2026-04-13,7.7\n")):
            with pytest.raises(kurse.TabelleUngueltig):
                kurse.aktualisiere(tmp_path, abruf=lambda url, k=kaputt: k)
            kurse._FEHLSCHLAG.clear()
        assert kurse.tabelle(tmp_path).sha256 == vorher.sha256
        assert not list((tmp_path / "kurse").glob(".*.teil"))

    def test_aeltere_tabelle_ersetzt_keine_neuere(self, tmp_path):
        """Ein Zwischenspeicher unterwegs liefert eine alte Fassung: die
        vorhandene, neuere bleibt."""
        tabelle_ablegen(tmp_path)
        alt = "".join(z for z in KURS_CSV.read_text().splitlines(keepends=True)
                      if not z.startswith("2026-04"))
        with pytest.raises(kurse.TabelleUngueltig, match="vorhandene bleibt"):
            kurse.speichere(tmp_path, ezb_zip(alt), heute=HEUTE)

    def test_luecke_gekuerzte_geschichte_und_fehlende_waehrung(self, tmp_path):
        """Ein fehlender Geschaeftstag verschob den Kurs still auf den Vortag;
        eine gekuerzte Geschichte oder eine Datei ohne GBP ersetzte die
        vollstaendige Tabelle. Alles wird abgewiesen, die vorhandene bleibt."""
        text = KURS_CSV.read_text()
        ohne_tag = "\n".join(z for z in text.splitlines() if not z.startswith("2026-03-03"))
        with pytest.raises(kurse.TabelleUngueltig,
                           match="fehlt der 03.03.2026, ein Geschäftstag der EZB"):
            kurse.lies_csv(ohne_tag, heute=HEUTE)
        vorher = tabelle_ablegen(tmp_path)
        gekuerzt = "\n".join(z for z in text.splitlines() if not z.startswith("2026-02"))
        with pytest.raises(kurse.TabelleUngueltig, match="beginnt erst am 02.03.2026"):
            kurse.speichere(tmp_path, ezb_zip(gekuerzt), heute=HEUTE)
        ohne_gbp = "\n".join(",".join(f for i, f in enumerate(z.split(",")) if i != 3)
                             for z in text.splitlines())
        assert ohne_gbp.startswith("Date,USD,JPY,CHF,")
        with pytest.raises(kurse.TabelleUngueltig, match="fehlt GBP"):
            kurse.speichere(tmp_path, ezb_zip(ohne_gbp), heute=HEUTE)
        assert kurse.tabelle(tmp_path).sha256 == vorher.sha256

    def test_zu_gross_wird_abgebrochen(self):
        """Mehr als die Groessengrenze: abgebrochen, mit oder ohne
        Content-Length -- gelesen wird hoechstens ein Byte ueber die Grenze."""

        class Antwort(io.BytesIO):
            def __init__(self, daten, laenge):
                super().__init__(daten)
                self.status = 200
                self.headers = {"Content-Length": laenge} if laenge else {}

            def geturl(self):
                return kurse.QUELLE

        gelesen = []

        def oeffne(laenge):
            def _oeffne(anfrage, timeout):
                antwort = Antwort(b"x" * 2000, laenge)
                echt = antwort.read1
                antwort.read1 = lambda n=-1: gelesen.append(n) or echt(n)
                return antwort
            return _oeffne

        with pytest.raises(kurse.AbrufFehler, match="größer als"):
            kurse.abrufen_https(oeffne=oeffne("2000"), hoechstens=1000)
        assert gelesen == [], "bei zu grosser Content-Length gar nicht erst gelesen"
        with pytest.raises(kurse.AbrufFehler, match="größer als"):
            kurse.abrufen_https(oeffne=oeffne(None), hoechstens=1000)
        assert gelesen == [1001]

    def test_zeitgrenze_gilt_fuer_den_ganzen_abruf(self):
        """Ein Server, der tropfenweise liefert (jede Leseoperation fuer sich
        schnell): die Zeitgrenze galt nur je Leseoperation, und die Pruefmaske
        wartete minutenlang. Jetzt bricht der Abruf nach der Frist ab."""
        import time

        class Tropf(io.BytesIO):
            status = 200
            headers: dict = {}

            def geturl(self):
                return kurse.QUELLE

            def read1(self, n=-1):
                time.sleep(0.02)
                return super().read1(min(n, 10))

        beginn = time.monotonic()
        with pytest.raises(kurse.AbrufFehler, match="innerhalb von 0.2 Sekunden"):
            kurse.abrufen_https(oeffne=lambda anfrage, timeout: Tropf(b"x" * 100_000),
                                zeitlimit=0.2)
        assert time.monotonic() - beginn < 1.5

    def test_abgebrochener_download_heisst_so(self):
        """Bricht die Verbindung vor der angekuendigten Laenge ab, sagt die
        Meldung das -- statt spaeter "ZIP-Archiv beschädigt (File is not a zip
        file)", englisch und mit falscher Ursache."""

        class Kurz(io.BytesIO):
            status = 200
            headers = {"Content-Length": "1000"}

            def geturl(self):
                return kurse.QUELLE

        with pytest.raises(kurse.AbrufFehler) as fehler:
            kurse.abrufen_https(oeffne=lambda anfrage, timeout: Kurz(b"PK" + b"x" * 298))
        assert str(fehler.value) == ("Die Kurstabelle ließ sich nicht laden: der Download "
                                     "brach nach 300 von 1.000 Bytes ab.")
        with pytest.raises(kurse.TabelleUngueltig) as fehler:
            kurse.entpacke(ezb_zip()[:300])
        assert str(fehler.value) == ("Das ZIP-Archiv der Kurstabelle ist beschädigt oder "
                                     "unvollständig.")

    def test_nur_https_ohne_parameter_ohne_belegdaten(self):
        """Nur HTTPS, keine Abfrageparameter, kein Referer: die Anfrage traegt
        nichts, was vom Nutzer oder seinen Belegen abhaengt."""
        with pytest.raises(kurse.AbrufFehler, match="nur über HTTPS"):
            kurse.abrufen_https("http://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip")
        with pytest.raises(kurse.AbrufFehler, match="keine Parameter"):
            kurse.abrufen_https(kurse.QUELLE + "?beleg=7")
        anfragen = []

        class Antwort(io.BytesIO):
            status = 200
            headers: dict = {}

            def geturl(self):
                return "http://umgeleitet.example/kurse.zip"

        def oeffne(anfrage, timeout):
            anfragen.append(anfrage)
            return Antwort(b"PK")

        with pytest.raises(kurse.AbrufFehler, match="ohne HTTPS"):
            kurse.abrufen_https(oeffne=oeffne)
        [anfrage] = anfragen
        assert anfrage.full_url == kurse.QUELLE
        assert anfrage.get_method() == "GET"
        koepfe = {k.lower() for k, _ in anfrage.header_items()}
        assert "referer" not in koepfe and "cookie" not in koepfe

    def test_echter_abruf_ist_in_tests_gesperrt(self, monkeypatch):
        """Selbst wer den Ersatz umgeht, kommt nicht ins Netz."""
        monkeypatch.setattr(kurse, "netzabruf", kurse.abrufen_https)
        with pytest.raises(kurse.AbrufFehler, match="STEUER_TOOL_OHNE_NETZ"):
            kurse.aktualisiere(Path("/nicht/da"))

    def test_atomar_gespeichert(self, tmp_path, monkeypatch):
        """Scheitert das Ersetzen mitten im Speichern, liegt unter dem echten
        Namen weiter die vorige, vollstaendige Datei -- und keine halbe."""
        tabelle_ablegen(tmp_path)
        vorher = (tmp_path / "kurse" / kurse.ROHDATEI).read_bytes()
        neuer = KURS_CSV.read_text().replace(
            "Date,USD,JPY,GBP,CHF,ISK,RUB,\n",
            "Date,USD,JPY,GBP,CHF,ISK,RUB,\n2026-04-13,1.17,186.0,0.871,0.924,N/A,N/A,\n")

        def kaputt(quelle, ziel):
            raise OSError(28, "No space left on device")

        with monkeypatch.context() as m:
            m.setattr(kurse.os, "replace", kaputt)
            with pytest.raises(kurse.AbrufFehler, match="nicht speichern"):
                kurse.aktualisiere(tmp_path, abruf=lambda url: ezb_zip(neuer))
        assert (tmp_path / "kurse" / kurse.ROHDATEI).read_bytes() == vorher
        assert not [p for p in (tmp_path / "kurse").iterdir() if p.name.endswith(".teil")]
        # Und gelingt es, steht die neue Fassung mit Abrufzeit und Pruefsumme da.
        kurse._FEHLSCHLAG.clear()
        neu = kurse.aktualisiere(tmp_path, abruf=lambda url: ezb_zip(neuer))
        assert neu.letzter_tag == date(2026, 4, 13)
        assert kurse.tabelle(tmp_path).sha256 == kurse.pruefsumme(ezb_zip(neuer))
        assert kurse.tabelle(tmp_path).abgerufen_utc == neu.abgerufen_utc

    def test_verwendete_fassung_bleibt_erhalten(self, tmp_path):
        """Wird ein Kurs fuer einen Beleg benutzt, bleibt genau diese Fassung
        der Tabelle erhalten -- auch wenn spaeter eine neuere sie ersetzt."""
        alt = tabelle_ablegen(tmp_path)
        pfad = kurse.sichere_fassung(tmp_path, alt.sha256)
        assert pfad == tmp_path / "kurse" / "verwendet" / f"{alt.sha256}.zip"
        neuer = KURS_CSV.read_text().replace(
            "Date,USD,JPY,GBP,CHF,ISK,RUB,\n",
            "Date,USD,JPY,GBP,CHF,ISK,RUB,\n2026-04-13,1.17,186.0,0.871,0.924,N/A,N/A,\n")
        kurse.speichere(tmp_path, ezb_zip(neuer), heute=HEUTE)
        assert kurse.pruefsumme(pfad.read_bytes()) == alt.sha256
        tab, problem = kurse.fassung_reihe(tmp_path, alt.sha256, "USD")
        assert (tab.letzter_tag, problem) == (date(2026, 4, 10), None)

    def test_pruefung_liest_je_fassung_nur_eine_waehrung(self, tmp_path, monkeypatch):
        """Mit jeder Fassung, mit der gebucht wurde (eine je Buchungstag), wurde
        die Integritaetspruefung langsamer und behielt gut 10 MB je Fassung bis
        zum Neustart: jede wurde als ganze Tabelle gelesen. Jetzt liest sie je
        Fassung nur die Spalte der Waehrung, und der Zwischenspeicher fuer
        ganze Tabellen haelt hoechstens zwei."""
        conn = db.oeffne(tmp_path / "steuer.sqlite")
        try:
            text = KURS_CSV.read_text()
            for nr in range(4):
                # Je Durchgang eine andere Fassung (wie nach jedem Abruf).
                tab = kurse.speichere(tmp_path, ezb_zip(text + "\n" * nr), heute=HEUTE)
                kurse.sichere_fassung(tmp_path, tab.sha256)
                kurs = kurse.kurs_aus_tabelle(tab, "USD", date(2026, 3, 3))
                cent = kurse.in_euro_cent(10000, kurs.kurs)
                conn.execute(
                    "INSERT INTO beleg (belegdatum, waehrung, betrag_fremdwaehrung_cent, kurs,"
                    " kurs_datum, kurs_quelle, kurs_tabelle_sha256, betrag_umgerechnet_cent,"
                    " betrag_brutto_cent, betrag_betrieblich_cent, erfasst_am) VALUES"
                    " ('2026-03-03', 'USD', 10000, ?, '2026-03-03', 'EZB', ?, ?, ?, ?, 'x')",
                    (kurs.kurs, tab.sha256, cent, cent, cent))
            assert len(kurse._ZWISCHENSPEICHER) <= kurse.ZWISCHENSPEICHER_HOECHSTENS
            monkeypatch.setattr(kurse, "tabelle_aus_bytes",
                                lambda *a, **k: pytest.fail("ganze Tabelle gelesen"))
            pruefung = umrechnung.pruefe(conn, tmp_path)
            assert (pruefung.geprueft, pruefung.befunde) == (4, [])
        finally:
            conn.close()


class TestRundung:
    def test_kaufmaennisch_auf_cent_mit_decimal(self):
        """Euro = Fremdbetrag / Kurs, ein halber Cent rundet auf -- exakt.

        8,02 GBP zum Kurs 0,8 sind genau 10,025 EUR: kaufmaennisch 10,03.
        Mit Fliesskomma (802 / 0.8 = 1002.4999...) oder Bankers' Rounding
        kaeme 10,02 heraus."""
        assert kurse.in_euro_cent(802, "0.8") == 1003
        assert kurse.in_euro_cent(10000, "1.0856") == 9211
        assert kurse.in_euro_cent(10000, "1.15") == 8696
        assert kurse.in_euro_cent(-802, "0.8") == -1003
        assert kurse.in_euro_cent(1, "0.5") == 2
        assert round(802 / 0.8) == 1002, "Fliesskomma kaeme hier falsch heraus"

    def test_kurs_von_hand_lesen(self):
        """Komma oder Punkt als Dezimaltrenner; kein Kurs ist null oder Text."""
        assert kurse.lies_kurs("1,0856") == "1.0856"
        assert kurse.lies_kurs(" 3,6725 ") == "3.6725"
        for falsch in ("abc", "0", "-1,2", "1e3"):
            with pytest.raises(ValueError):
                kurse.lies_kurs(falsch)


def test_target_kalender_passt_zur_pruefdatei(tabelle):
    """Jeder Werktag der Pruefdatei ist ein TARGET-Geschaeftstag, und jeder
    Geschaeftstag zwischen erstem und letztem Tag hat einen Kurs."""
    tage = set(tabelle.reihen["USD"][0])
    tag = tabelle.erster_tag
    while tag <= tabelle.letzter_tag:
        assert (tag in tage) == (not kurse.target_geschlossen(tag)), tag
        tag += timedelta(days=1)


def test_umgebung_sperrt_den_abruf_auch_fuer_kindprozesse():
    """Die Sperre steht in der Umgebung -- ein Kindprozess erbt sie."""
    assert os.environ.get(kurse.OHNE_NETZ) == "1"


# ================================================================ Am Beleg

PDF_EN = FIXTURES / "lieferantenrechnung_en.pdf"
#: Die AWS-Rechnung der Pruefdatei: 1.234,56 USD vom Dienstag, 03.03.2026.
#: EZB-Kurs an dem Tag 1,1606 -- 123456 / 1,1606 = 106372,57 Cent.
AWS_EZB_CENT = 106373
#: Derselbe Betrag zum Kurs vom Freitag, 20.03.2026 (1,1555).
AWS_20_MAERZ_CENT = 106842
SHA_PRUEFDATEI = kurse.pruefsumme(ezb_zip())

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}


def aws_kopie() -> bytes:
    """Dieselbe AWS-Rechnung als zweites PDF: gleicher Inhalt, andere Bytes
    (ein zweiter Export, eine Kopie aus dem Kundenportal)."""
    return PDF_EN.read_bytes() + b"\n% zweiter Export\n"


@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


@pytest.fixture
def eingerichtet(conn):
    conn.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer)"
        " VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095', 'Hamburg', '12/345/67890')")
    return 1


def _aws(conn, datenordner, inhalt: bytes | None = None, name: str = "aws.pdf"):
    eingang = p.aufnehmen(inhalt or PDF_EN.read_bytes(), name, datenordner)
    return p.lies(conn, eingang, heute=HEUTE)


def _bestaetigt(lesung, **werte):
    felder = {feld: fund.wert for feld, fund in lesung.vorschlag.felder.items()}
    return {**felder, **werte}


def _lieferant_aws(conn) -> int:
    return conn.execute(
        "INSERT INTO lieferant (name, ust_idnr, angelegt_am, waehrung_default)"
        " VALUES ('Amazon Web Services EMEA SARL', 'LU26888617', '2026-01-01', 'USD')"
    ).lastrowid


class TestBuchen:
    def test_ohne_kartenbetrag_zum_ezb_kurs_des_belegdatums(self, conn, eingerichtet,
                                                           datenordner):
        """Ohne Kartenbetrag und ohne Zahlungstag: zum EZB-Kurs am Belegdatum.
        Gespeichert ist alles, was die Zahl erklaert -- und die verwendete
        Fassung der Kurstabelle bleibt erhalten."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        buchung = p.buche(conn, lesung, _bestaetigt(lesung), datenordner,
                          lieferant_id=_lieferant_aws(conn))
        b = conn.execute("SELECT * FROM beleg WHERE id = ?", (buchung.beleg_id,)).fetchone()
        assert (b["waehrung"], b["betrag_fremdwaehrung_cent"]) == ("USD", 123456)
        assert (b["kurs"], b["kurs_datum"], b["kurs_quelle"]) == ("1.1606", "2026-03-03", "EZB")
        assert b["betrag_umgerechnet_cent"] == b["betrag_brutto_cent"] == AWS_EZB_CENT
        assert b["betrag_betrieblich_cent"] == AWS_EZB_CENT
        assert b["betrag_karte_cent"] is None
        assert b["kurs_tabelle_sha256"] == SHA_PRUEFDATEI and b["kurs_abruf_utc"]
        assert (datenordner / "kurse" / "verwendet" / f"{SHA_PRUEFDATEI}.zip").is_file()
        assert "zum EZB-Kurs 1,1606 vom 03.03.2026 umgerechnet: 1.063,73 €" in \
            buchung.umrechnung.text()

    def test_zahlungstag_der_maske_ist_der_stichtag(self, conn, eingerichtet, datenordner):
        """Steht in der Pruefmaske "Bezahlt am", wird zum Kurs dieses Tages
        umgerechnet -- nach § 11 Abs. 2 EStG zaehlt der Abflusstag."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        buchung = p.buche(conn, lesung, _bestaetigt(lesung, umrechnung_am="2026-03-21"),
                          datenordner)
        b = conn.execute("SELECT * FROM beleg WHERE id = ?", (buchung.beleg_id,)).fetchone()
        assert (b["kurs"], b["kurs_datum"]) == ("1.1555", "2026-03-20"), "Samstag -> Freitag"
        assert b["betrag_brutto_cent"] == AWS_20_MAERZ_CENT

    def test_journal_haelt_kurs_datum_und_quelle(self, conn, eingerichtet, datenordner):
        """BELEG_ERFASSEN traegt Kurs, Kursdatum, Quelle und Tabelle."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        p.buche(conn, lesung, _bestaetigt(lesung), datenordner)
        eintrag = json.loads(conn.execute(
            "SELECT wert_neu FROM journal WHERE aktion = 'BELEG_ERFASSEN'").fetchone()[0])
        u = eintrag["umrechnung"]
        assert (u["kurs"], u["kurs_datum"], u["kurs_quelle"], u["euro_quelle"]) == \
            ("1.1606", "2026-03-03", "EZB", "EZB")
        assert (u["fremdbetrag_cent"], u["euro_cent"]) == (123456, AWS_EZB_CENT)
        assert u["kurs_tabelle_sha256"] == SHA_PRUEFDATEI

    def test_kartenbetrag_ersetzt_den_umgerechneten(self, conn, eingerichtet, datenordner):
        """Der Kartenbetrag ist genauer (mit Auslandseinsatzentgelt). Der
        EZB-Wert bleibt zum Vergleich stehen."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        buchung = p.buche(conn, lesung, _bestaetigt(lesung, brutto_eur=108000), datenordner)
        b = conn.execute("SELECT * FROM beleg WHERE id = ?", (buchung.beleg_id,)).fetchone()
        assert b["betrag_brutto_cent"] == b["betrag_karte_cent"] == 108000
        assert b["betrag_umgerechnet_cent"] == AWS_EZB_CENT
        assert buchung.umrechnung.quelle == "KARTE"

    def test_kartenbetrag_mehr_als_zehn_prozent_daneben_fragt_nach(
            self, conn, eingerichtet, datenordner):
        """Der Dollarbetrag als Euro eingetragen (16 % zu viel): Rueckfrage.
        Bestaetigt, gilt er."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        with pytest.raises(umrechnung.KartenbetragAbweichend, match="weicht um"):
            p.buche(conn, lesung, _bestaetigt(lesung, brutto_eur=123456), datenordner)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0
        assert lesung.eingang.pfad.exists()
        p.buche(conn, lesung, _bestaetigt(lesung, brutto_eur=123456, karte_bestaetigt=True),
                datenordner)
        assert conn.execute("SELECT betrag_brutto_cent FROM beleg").fetchone()[0] == 123456

    def test_ohne_kurs_und_ohne_kartenbetrag_kein_buchen(self, conn, eingerichtet,
                                                         datenordner):
        """Offline und noch keine Tabelle: die Meldung sagt, was fehlt und was
        hilft. Mit Kartenbetrag geht es auch ohne Kurs."""
        lesung = _aws(conn, datenordner)
        with pytest.raises(umrechnung.KeinEuroBetrag, match="Kreditkarte .* Kurs von Hand"):
            p.buche(conn, lesung, _bestaetigt(lesung), datenordner)
        buchung = p.buche(conn, lesung, _bestaetigt(lesung, brutto_eur=108000), datenordner)
        b = conn.execute("SELECT * FROM beleg WHERE id = ?", (buchung.beleg_id,)).fetchone()
        assert (b["betrag_brutto_cent"], b["kurs"]) == (108000, None)

    def test_unbekannte_waehrung_mit_kurs_von_hand(self, conn, eingerichtet, datenordner):
        """Die EZB fuehrt den Dirham nicht: ein Kurs von Hand, Quelle "manuell"."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        with pytest.raises(umrechnung.KeinEuroBetrag, match="AED"):
            p.buche(conn, lesung, _bestaetigt(lesung, waehrung="AED"), datenordner)
        buchung = p.buche(conn, lesung, _bestaetigt(lesung, waehrung="AED",
                                                    kurs_manuell="4.2632"), datenordner)
        b = conn.execute("SELECT * FROM beleg WHERE id = ?", (buchung.beleg_id,)).fetchone()
        assert (b["kurs"], b["kurs_quelle"], b["kurs_datum"]) == ("4.2632", "MANUELL",
                                                                  "2026-03-03")
        assert b["betrag_brutto_cent"] == kurse.in_euro_cent(123456, "4.2632")
        assert b["kurs_tabelle_sha256"] is None

    def test_kurs_von_hand_nur_ohne_ezb_kurs(self, conn, eingerichtet, datenordner):
        """Liefert die EZB einen Kurs, gilt ihrer: die Methode ist stetig."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        with pytest.raises(BelegFehler, match="stetig"):
            p.buche(conn, lesung, _bestaetigt(lesung, kurs_manuell="1.2"), datenordner)

    def test_kurs_von_hand_nicht_fuer_einen_kurs_der_noch_kommt(self, conn, eingerichtet,
                                                                 datenordner):
        """Der Kurs fuer den 13.04. ist noch nicht veroeffentlicht (die Tabelle
        reicht bis 10.04., ein Abruf eben lieferte nichts Neueres): er kommt,
        dann gilt seiner. Ein Kurs von Hand wird abgewiesen, die Meldung nennt
        "später buchen" und den Kartenbetrag -- nicht den Kurs von Hand."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        with pytest.raises(umrechnung.KeinEuroBetrag) as fehler:
            p.buche(conn, lesung, _bestaetigt(lesung, umrechnung_am="2026-04-14"), datenordner,
                    heute=HEUTE)
        assert "noch nicht veröffentlicht" in str(fehler.value)
        assert "Bitte später buchen" in str(fehler.value)
        assert "Kurs von Hand" not in str(fehler.value)
        with pytest.raises(BelegFehler, match="Einen Kurs von Hand nimmt das Tool dafür nicht"):
            p.buche(conn, lesung, _bestaetigt(lesung, umrechnung_am="2026-04-14",
                                              kurs_manuell="1.17"), datenordner, heute=HEUTE)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_ohne_kurs_steht_die_aufforderung_einmal_da(self, conn, eingerichtet,
                                                         datenordner):
        """Dirham ohne Kurs: der Grund und EINE Aufforderung, nicht zweimal
        dieselbe ("... oder den Betrag laut Kreditkarte. ... bitte den Betrag
        laut Kreditkarte eintragen oder einen Kurs von Hand")."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        with pytest.raises(umrechnung.KeinEuroBetrag) as fehler:
            p.buche(conn, lesung, _bestaetigt(lesung, waehrung="AED"), datenordner)
        assert str(fehler.value) == (
            "Für AED veröffentlicht die EZB keinen Referenzkurs. Ohne Kurs fehlt der "
            "Euro-Betrag für die EÜR. Bitte den Betrag laut Kreditkarte (in Euro) eintragen "
            "oder einen Kurs von Hand.")

    def test_euro_betrag_ist_immer_karte_oder_umgerechnet(self, conn, eingerichtet):
        """Ein dritter Euro-Betrag weist die Datenbank ab (Trigger)."""
        with pytest.raises(sqlite3.IntegrityError, match="Kartenbetrag oder der umgerechnete"):
            conn.execute(
                "INSERT INTO beleg (belegdatum, waehrung, betrag_fremdwaehrung_cent,"
                " betrag_umgerechnet_cent, betrag_brutto_cent, betrag_betrieblich_cent,"
                " erfasst_am) VALUES ('2026-03-03', 'USD', 10000, 8616, 9000, 9000, 'x')")


class TestMigration:
    def test_von_18_auf_19(self, tmp_path):
        """Eine Datenbank aus Version 18 bekommt die Spalten mit ihren CHECKs
        und die Trigger -- dieselben wie eine frische. Ein Fremdwaehrungsbeleg
        aus der Zeit davor bleibt, wie er ist."""
        pfad = tmp_path / "alt.sqlite"
        alt = db.oeffne(pfad)
        alt.execute("DROP TRIGGER beleg_euro_betrag_stimmig_insert")
        alt.execute("DROP TRIGGER beleg_euro_betrag_stimmig_update")
        # Erst seit Version 25; er liest die Spalten, die Version 18 nicht hat.
        alt.execute("DROP TRIGGER beleg_betrag_datum_fest")
        for spalte in ("kurs", "kurs_datum", "kurs_quelle", "kurs_abruf_utc",
                       "kurs_tabelle_sha256", "betrag_umgerechnet_cent", "betrag_karte_cent"):
            alt.execute(f"ALTER TABLE beleg DROP COLUMN {spalte}")
        alt.execute(
            "INSERT INTO beleg (belegdatum, waehrung, betrag_fremdwaehrung_cent,"
            " betrag_brutto_cent, betrag_betrieblich_cent, erfasst_am)"
            " VALUES ('2025-11-03', 'USD', 123456, 113000, 113000, '2025-11-03')")
        alt.execute("DELETE FROM schema_version")
        alt.execute("INSERT INTO schema_version VALUES (18, ?)", (db.jetzt_utc(),))
        alt.close()

        migriert = db.oeffne(pfad)
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        try:
            def spalten(verbindung):
                return {z["name"] for z in verbindung.execute("PRAGMA table_info(beleg)")}

            def trigger(verbindung):
                return {z[0] for z in verbindung.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    " AND tbl_name = 'beleg'")}

            assert spalten(migriert) == spalten(frisch)
            assert trigger(migriert) == trigger(frisch)
            assert migriert.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
                == db.SCHEMA_VERSION >= 19
            assert migriert.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                                    " AND objekt_id = 19").fetchone()[0] == 1
            alt_beleg = migriert.execute("SELECT * FROM beleg").fetchone()
            assert (alt_beleg["betrag_brutto_cent"], alt_beleg["betrag_karte_cent"],
                    alt_beleg["kurs"]) == (113000, None, None)
            with pytest.raises(sqlite3.IntegrityError):
                migriert.execute("UPDATE beleg SET kurs_quelle = 'GERATEN'")
            with pytest.raises(sqlite3.IntegrityError):
                migriert.execute("UPDATE beleg SET betrag_karte_cent = 0")
            with pytest.raises(sqlite3.IntegrityError, match="Kartenbetrag"):
                migriert.execute("UPDATE beleg SET betrag_karte_cent = 112000")
        finally:
            migriert.close()
            frisch.close()


class TestDubletten:
    """Die Frage des Nutzers: "Hast du auch den Fall bedacht, wenn ich aus
    Versehen zweimal dieselbe Rechnung hochlade?" -- bei Fremdwaehrung."""

    def test_byteidentische_datei_wie_bisher(self, conn, eingerichtet, datenordner):
        """Genau dieselbe Datei: "bereits als Beleg N gebucht"."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        p.buche(conn, lesung, _bestaetigt(lesung), datenordner)
        zweite = _aws(conn, datenordner)
        assert zweite.dublette_von == 1
        with pytest.raises(p.SchonGebucht, match="bereits als Beleg 1"):
            p.buche(conn, zweite, _bestaetigt(zweite), datenordner)

    def test_zweites_pdf_der_usd_rechnung_wird_erkannt(self, conn, eingerichtet,
                                                       datenordner):
        """Die erste ist mit dem Kartenbetrag gebucht (1.080,00 EUR) und ohne
        Belegnummer, die zweite (andere Bytes) wuerde zum EZB-Kurs umgerechnet
        (1.063,73 EUR). Vorher verglich der Code 1.234,56 USD aus der Auslesung
        mit dem Euro-Betrag -- gleiches Datum und gleicher Betrag trafen so
        nie. Jetzt zaehlen Waehrung und Fremdbetrag."""
        tabelle_ablegen(datenordner)
        lid = _lieferant_aws(conn)
        lesung = _aws(conn, datenordner)
        p.buche(conn, lesung, _bestaetigt(lesung, belegnummer=None, brutto_eur=108000),
                datenordner, lieferant_id=lid)

        zweite = _aws(conn, datenordner, aws_kopie(), "aws-kopie.pdf")
        assert zweite.dublette_von is None, "andere Bytes: die Pruefsumme sieht es nicht"
        assert [b["id"] for b in zweite.aehnliche] == [1]
        assert "1.234,56 USD (1.080,00 €)" in p.aehnliche_als_text(zweite.aehnliche)
        with pytest.raises(p.AehnlicherBeleg, match="trotzdem buchen"):
            p.buche(conn, zweite, _bestaetigt(zweite, belegnummer=None), datenordner,
                    lieferant_id=lid)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1
        # Ist es wirklich eine andere Rechnung, bestaetigt der Nutzer das.
        p.buche(conn, zweite, _bestaetigt(zweite, belegnummer=None), datenordner,
                lieferant_id=lid, trotzdem=True)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 2

    def test_euro_vergleich_bleibt(self, conn, eingerichtet, datenordner):
        """Zusaetzlich wie bisher: derselbe Euro-Betrag am selben Tag."""
        lid = _lieferant_aws(conn)
        conn.execute(
            "INSERT INTO beleg (lieferant_id, belegdatum, waehrung, betrag_brutto_cent,"
            " betrag_betrieblich_cent, erfasst_am) VALUES (?, '2026-03-03', 'EUR', ?, ?, 'x')",
            (lid, AWS_EZB_CENT, AWS_EZB_CENT))
        treffer = p.aehnliche_belege(conn, lid, None, "2026-03-03", 123456, waehrung="USD",
                                     euro=AWS_EZB_CENT)
        assert len(treffer) == 1
        assert p.aehnliche_belege(conn, lid, None, "2026-03-03", 123456, waehrung="USD") == []

    def test_bei_anderem_lieferanten_datensatz(self, conn, eingerichtet, datenordner):
        """Derselbe Lieferant zweimal angelegt: auch dort zaehlt der
        Fremdbetrag, nicht der Euro-Betrag."""
        tabelle_ablegen(datenordner)
        lesung = _aws(conn, datenordner)
        p.buche(conn, lesung, _bestaetigt(lesung, brutto_eur=108000), datenordner)
        andere = _lieferant_aws(conn)
        treffer = p.aehnliche_bei_anderen(conn, andere, None, "2026-03-03", 123456,
                                          waehrung="USD")
        assert [b["id"] for b in treffer] == [1]
        # Der Euro-Betrag gilt nicht als Dollarbetrag ...
        assert p.aehnliche_bei_anderen(conn, andere, None, "2026-03-03", 108000,
                                       waehrung="USD") == []
        # ... derselbe Zahlenwert in Euro dagegen trifft (R1: Waehrung verloren).
        assert [b["id"] for b in p.aehnliche_bei_anderen(
            conn, andere, None, "2026-03-03", 123456)] == [1]


# ================================================================ Oberflaeche

@pytest.fixture
def klient(tmp_path):
    """Die Anwendung mit eingerichtetem Betrieb und der Pruefdatei als
    Kurstabelle im Datenordner (tmp_path)."""
    tabelle_ablegen(tmp_path)
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


def _meldung(antwort) -> str:
    frage = parse_qs(urlparse(antwort.headers["location"]).query)
    return (frage.get("fehler") or frage.get("erfolg") or [""])[0]


def _hochladen(klient, inhalt: bytes | None = None, name: str = "aws.pdf") -> str:
    antwort = klient.post("/belege/hochladen",
                          files={"datei": (name, inhalt or PDF_EN.read_bytes(),
                                           "application/pdf")},
                          follow_redirects=False)
    return antwort.headers["location"].rsplit("/", 1)[1]


def _hosting(datenbank) -> str:
    with datenbank() as conn:
        return str(conn.execute("SELECT id FROM euer_kategorie WHERE schluessel = 'HOSTING'"
                                ).fetchone()[0])


AWS_FORMULAR = {
    "belegdatum": "2026-03-03", "brutto_euro": "1.234,56", "waehrung": "USD",
    "reverse_charge": "1", "lieferant_neu": "Amazon Web Services EMEA SARL",
    "lieferant_ust_idnr": "LU26888617", "quote": "100",
}


def _aws_buchen(klient, datenbank, inhalt: bytes | None = None, **felder):
    name = _hochladen(klient, inhalt)
    daten = {**AWS_FORMULAR, "kategorie_id": _hosting(datenbank), **felder}
    return klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)


def _beleg(datenbank, beleg_id: int = 1) -> sqlite3.Row:
    with datenbank() as conn:
        return conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()


def _zahlungen(datenbank) -> list[tuple[str, int]]:
    with datenbank() as conn:
        return [tuple(z) for z in conn.execute(
            "SELECT datum, betrag_cent FROM zahlung WHERE beleg_id IS NOT NULL ORDER BY id")]


def _ausgaben_2026(tmp_path) -> int:
    conn = db.oeffne(tmp_path / "steuer.sqlite")
    try:
        return euer.auswertung(conn, 2026).ausgaben_cent
    finally:
        conn.close()


def _zeile(klient, beleg_id: int = 1) -> str:
    return re.search(rf'<tr id="beleg-{beleg_id}">.*?</tr>', klient.get("/belege").text,
                     re.S).group(0)


class TestDublettenInDerMaske:
    """Dieselbe Rechnung als zweites PDF, beim zweiten Mal unter einem anderen
    Lieferanten-Datensatz oder ganz ohne Lieferant gebucht: Pruefmaske und
    Buchen sahen nur den gewaehlten Lieferanten, und die Rechnung stand
    zweimal in der EUeR. Der Eingangsordner prueft das schon."""

    def test_anderer_lieferanten_datensatz(self, klient, datenbank):
        """Zuerst unter "AWS" (ohne USt-IdNr.) gebucht, dann die Kopie unter
        dem neu angelegten "Amazon Web Services EMEA SARL": die Maske warnt,
        gebucht wird nur mit "trotzdem buchen"."""
        antwort = _aws_buchen(klient, datenbank, lieferant_neu="AWS", lieferant_ust_idnr="")
        assert antwort.status_code == 303
        name = _hochladen(klient, aws_kopie(), "aws-kopie.pdf")
        maske = klient.get(f"/belege/pruefen/{name}").text
        assert "Vermutlich schon gebucht." in maske
        assert "Zu Lieferant AWS gibt es bereits Beleg 1 vom 03.03.2026 über 1.234,56 USD" in maske
        assert 'name="trotzdem"' in maske
        daten = {**AWS_FORMULAR, "kategorie_id": _hosting(datenbank)}
        antwort = klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)
        assert antwort.status_code == 422
        assert "Zu Lieferant AWS gibt es bereits Beleg 1" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1
        antwort = klient.post(f"/belege/pruefen/{name}", data={**daten, "trotzdem": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303

    def test_beide_ohne_lieferant(self, klient, datenbank):
        """Beide Kopien ohne Lieferant gebucht: die zweite wird erkannt."""
        ohne = {"lieferant_neu": "", "lieferant_ust_idnr": ""}
        assert _aws_buchen(klient, datenbank, **ohne).status_code == 303
        antwort = _aws_buchen(klient, datenbank, aws_kopie(), **ohne)
        assert antwort.status_code == 422
        assert "Ohne Lieferant gibt es bereits Beleg 1 vom 03.03.2026 über 1.234,56 USD" in \
            antwort.text

    def test_verschiedene_nummern_bei_anderen_lieferanten(self, conn, eingerichtet):
        """Zwei Abos verschiedener Anbieter, am selben Tag, zum selben Betrag,
        mit eigenen Rechnungsnummern: keine Dublette. Dieselbe Nummer, oder
        eine Seite ohne Nummer, bleibt ein Treffer."""
        a = conn.execute("INSERT INTO lieferant (name, angelegt_am) VALUES ('Abo A', 'x')"
                         ).lastrowid
        b = conn.execute("INSERT INTO lieferant (name, angelegt_am) VALUES ('Abo B', 'x')"
                         ).lastrowid
        conn.execute(
            "INSERT INTO beleg (lieferant_id, belegdatum, belegnummer_extern, waehrung,"
            " betrag_brutto_cent, betrag_betrieblich_cent, erfasst_am)"
            " VALUES (?, '2026-03-01', 'A-1', 'EUR', 999, 999, 'x')", (a,))
        assert p.aehnliche_bei_anderen(conn, b, "B-7", "2026-03-01", 999) == []
        assert len(p.aehnliche_bei_anderen(conn, b, "A-1", "2026-03-09", 999)) == 1
        assert len(p.aehnliche_bei_anderen(conn, b, None, "2026-03-01", 999)) == 1
        assert len(p.aehnliche_bei_anderen(conn, None, "B-7", "2026-03-01", 999)) == 0
        assert len(p.aehnliche_bei_anderen(conn, None, None, "2026-03-01", 999)) == 1


class TestPruefmaske:
    def test_zeigt_den_ezb_vorschlag_sofort(self, klient):
        """Gleich beim Oeffnen: Kurs, Kursdatum und Euro-Betrag. Das alte
        Pflichtfeld "Betrag in Euro" ist der optionale Kartenbetrag."""
        text = klient.get(f"/belege/pruefen/{_hochladen(klient)}").text
        assert ("1.234,56 USD zum EZB-Referenzkurs 1,1606 vom 03.03.2026 = 1.063,73 €"
                in text)
        assert 'id="fremdwaehrung" hidden' not in text
        assert "Betrag laut Kreditkarte (optional)" in text
        assert "Betrag in Euro" not in text
        # Einheit und Kursrichtung: "1,1606" allein laesst offen, wie herum.
        assert "<strong>in Euro (€)</strong>: was tatsächlich abgebucht wurde" in text
        assert 'placeholder="0,00 €"' in text
        assert "= 1.063,73 € (1 € = 1,1606 USD)." in text
        assert 'id="kurs-manuell" hidden' in text, "Kurs von Hand nur, wenn noetig"

    def test_anlagegut_haken_nach_dem_euro_betrag(self, klient):
        """Das Vorhaken "Anlagegut" (ab 800 EUR netto) vergleicht bei
        Fremdwaehrung den Euro-Betrag -- Kartenbetrag, sonst EZB-Vorschlag --
        und nicht 870 USD oder 50.000 JPY. Die Maske traegt den Euro-Betrag
        des Vorschlags dafuer am Kasten; im Browser geprueft (Playwright):
        870 USD = 776,23 EUR hakt nicht mehr an, 900 EUR laut Karte schon."""
        text = klient.get(f"/belege/pruefen/{_hochladen(klient)}").text
        assert 'data-euro="1.063,73"' in text
        skript = text[text.index("function pruefe()"):]
        assert "zahl(feldKarte.value)" in skript
        assert "zahl(vorschlag.getAttribute('data-euro'))" in skript

    def test_bei_euro_bleibt_der_block_verborgen(self, klient):
        """Eine Rechnung in Euro: kein Fremdwaehrungsblock in der Maske."""
        name = _hochladen(klient, (FIXTURES / "lieferantenrechnung_de.pdf").read_bytes(),
                          "hetzner.pdf")
        assert 'id="fremdwaehrung" hidden' in klient.get(f"/belege/pruefen/{name}").text

    def test_vorschlag_beim_tippen(self, klient):
        """Die Maske fragt beim Tippen nach: Samstag nimmt den Freitag, ein
        Kartenbetrag mit mehr als 10 % Abweichung kuendigt die Rueckfrage an,
        eine Waehrung ohne EZB-Kurs bittet um einen Kurs von Hand."""
        daten = klient.get("/belege/kurs", params={
            "waehrung": "usd", "datum": "14.03.2026", "betrag": "100,00"}).json()
        assert (daten["ok"], daten["euro"], daten["kurs"], daten["kursdatum"]) == \
            (True, "87,14", "1,1476", "13.03.2026")
        assert "für den 14.03.2026" in daten["text"]
        daten = klient.get("/belege/kurs", params={
            "waehrung": "USD", "datum": "2026-03-14", "betrag": "100,00",
            "karte": "100,00"}).json()
        assert daten["rueckfrage"] and daten["abweichung"].startswith("+12,86 €")
        assert klient.get("/belege/kurs", params={"waehrung": "EUR"}).json()["waehrung"] \
            == "EUR"
        daten = klient.get("/belege/kurs", params={"waehrung": "AED",
                                                   "datum": "2026-03-14"}).json()
        assert daten["manuell"] and not daten["ok"]

    def test_buchen_ohne_kartenbetrag(self, klient, datenbank):
        """Gebucht wird ohne Kartenbetrag, zum EZB-Kurs am Belegdatum; die
        Meldung nennt Kurs, Kursdatum und Euro-Betrag."""
        antwort = _aws_buchen(klient, datenbank)
        assert antwort.status_code == 303
        assert "zum EZB-Kurs 1,1606 vom 03.03.2026 umgerechnet: 1.063,73 €" in \
            _meldung(antwort)
        b = _beleg(datenbank)
        assert (b["betrag_brutto_cent"], b["kurs"], b["kurs_quelle"]) == \
            (AWS_EZB_CENT, "1.1606", "EZB")

    def test_mit_zahlungstag_zaehlt_der_kurs_des_zahlungstags(self, klient, datenbank,
                                                              tmp_path):
        """"Bezahlt am" in der Maske: Kurs des Zahlungstags, die Zahlung
        bekommt genau den Euro-Betrag des Belegs -- und die EUeR auch."""
        _aws_buchen(klient, datenbank, bezahlt_am="2026-03-20")
        assert _beleg(datenbank)["betrag_brutto_cent"] == AWS_20_MAERZ_CENT
        assert _zahlungen(datenbank) == [("2026-03-20", -AWS_20_MAERZ_CENT)]
        assert _ausgaben_2026(tmp_path) == AWS_20_MAERZ_CENT

    def test_kartenbetrag_weit_daneben_fragt_nach(self, klient, datenbank):
        """Der Dollarbetrag als Euro eingetragen: Rueckfrage mit erhaltenen
        Eingaben (422), gebucht erst nach Bestaetigung."""
        antwort = _aws_buchen(klient, datenbank, brutto_eur="1.234,56")
        assert antwort.status_code == 422
        assert "weicht um +170,83 € / +16,06 %" in antwort.text
        assert 'name="karte_bestaetigt"' in antwort.text
        # Der Vorschlagskasten ist dann gelb wie beim Tippen, nicht gruen.
        kasten = re.search(r'<div class="meldung (\w+)"\s+id="kursvorschlag"[^>]*>(.*?)</div>',
                           antwort.text, re.S)
        assert kasten.group(1) == "warn"
        assert "mehr als 10 %; beim Buchen wird" in kasten.group(2)
        assert 'name="brutto_eur" inputmode="decimal" placeholder="0,00 €" autocomplete="off"\n' \
               '                     value="1.234,56"' in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0
        name = re.search(r'action="/belege/pruefen/([^"]+)"', antwort.text).group(1)
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **AWS_FORMULAR, "brutto_eur": "1.234,56", "karte_bestaetigt": "1"},
            follow_redirects=False)
        assert antwort.status_code == 303
        assert _beleg(datenbank)["betrag_karte_cent"] == 123456

    def test_kurs_von_hand_fuer_eine_waehrung_ohne_ezb_kurs(self, klient, datenbank):
        """Dirham: die Maske zeigt das Feld "Kurs von Hand", gebucht wird mit
        Quelle "manuell"."""
        antwort = _aws_buchen(klient, datenbank, waehrung="AED")
        assert antwort.status_code == 422
        assert "Für AED veröffentlicht die EZB keinen Referenzkurs" in antwort.text
        assert 'id="kurs-manuell" hidden' not in antwort.text
        name = re.search(r'action="/belege/pruefen/([^"]+)"', antwort.text).group(1)
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **AWS_FORMULAR, "waehrung": "AED", "kurs_manuell": "4,2632"},
            follow_redirects=False)
        assert antwort.status_code == 303
        b = _beleg(datenbank)
        assert (b["kurs"], b["kurs_quelle"]) == ("4.2632", "MANUELL")
        assert "zum Kurs von Hand 4,2632" in _meldung(antwort)


class TestKursVonHandInDerMaske:
    """Meldung und Maske passen zusammen: das Feld "Kurs von Hand" steht da,
    wo ein Kurs von Hand gilt, und die Meldung empfiehlt ihn nur dann."""

    def test_kurs_noch_nicht_veroeffentlicht(self, klient, datenbank):
        """Der Kurs fuer den 13.04. kommt noch: das Feld bleibt verborgen,
        die Meldung nennt "später buchen" und den Kartenbetrag -- und ein
        trotzdem gesendeter Kurs von Hand wird abgewiesen."""
        antwort = _aws_buchen(klient, datenbank, belegdatum="2026-04-14")
        assert antwort.status_code == 422
        assert "Bitte später buchen" in antwort.text
        assert "einen Kurs von Hand." not in antwort.text
        assert 'id="kurs-manuell" hidden' in antwort.text
        antwort = _aws_buchen(klient, datenbank, belegdatum="2026-04-14", kurs_manuell="1,17")
        assert antwort.status_code == 422
        assert "Einen Kurs von Hand nimmt das Tool dafür nicht" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_offline_mit_alter_tabelle_zeigt_das_feld(self, klient, datenbank, tmp_path):
        """Offline, die Tabelle ist zu alt: die Meldung sagt das, und das Feld
        "Kurs von Hand" steht da."""
        stand = json.loads((tmp_path / "kurse" / "stand.json").read_text())
        stand["abgerufen_utc"] = "2026-04-11T00:00:00+00:00"
        (tmp_path / "kurse" / "stand.json").write_text(json.dumps(stand))
        name = _hochladen(klient)
        text = klient.get(f"/belege/pruefen/{name}").text
        assert 'id="kurs-manuell" hidden' in text, "fuer den 03.03. gibt es den Kurs"
        daten = klient.get("/belege/kurs", params={"waehrung": "USD", "datum": "2026-04-14",
                                                   "betrag": "100,00"}).json()
        assert daten["manuell"] and not daten["ok"]
        assert "ließ sich nicht aktualisieren" in daten["text"]
        assert daten["text"].endswith("oder einen Kurs von Hand.")


class TestKursVonHandPlausibel:
    """Ein Kurs von Hand zeigt seinen Euro-Betrag schon beim Tippen, und ein
    verdrehter oder verrutschter Kurs loest eine Rueckfrage aus. Vorher
    wurde "367,00 AED zum Kurs von Hand 0,2723" ohne Rueckfrage mit
    1.347,78 EUR statt rund 90 EUR gebucht."""

    def test_vorschau_beim_tippen(self, klient):
        """Der Vorschlag rechnet mit dem Kurs von Hand und nennt die Richtung."""
        daten = klient.get("/belege/kurs", params={
            "waehrung": "AED", "datum": "2026-03-13", "betrag": "367,00", "kurs": "4,05"}).json()
        assert (daten["ok"], daten["euro"], daten["kurs_rueckfrage"]) == (True, "90,62", False)
        assert daten["text"] == "367,00 AED zum Kurs von Hand 4,05 (1 € = 4,05 AED) = 90,62 €."

    def test_verdrehter_kurs_fragt_schon_beim_tippen(self, klient):
        """0,2723 ("1 AED = 0,27 EUR") ist der Kurs andersherum."""
        daten = klient.get("/belege/kurs", params={
            "waehrung": "AED", "datum": "2026-03-13", "betrag": "367,00",
            "kurs": "0,2723"}).json()
        assert daten["kurs_rueckfrage"]
        assert "hieße: 1 AED = 3,67 € — mehr als ein Euro." in daten["text"]
        assert "(1 € = 3,6724 AED)" in daten["text"]

    def test_verdrehter_kurs_bucht_erst_nach_bestaetigung(self, klient, datenbank):
        """Beim Buchen: 422 mit Rueckfrage und Haken, gebucht erst bestaetigt."""
        antwort = _aws_buchen(klient, datenbank, waehrung="AED", brutto_euro="367,00",
                              kurs_manuell="0,2723")
        assert antwort.status_code == 422
        assert "Ist der Kurs andersherum gemeint (1 € = 3,6724 AED)?" in antwort.text
        assert 'name="kurs_bestaetigt"' in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0
        name = re.search(r'action="/belege/pruefen/([^"]+)"', antwort.text).group(1)
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **AWS_FORMULAR, "waehrung": "AED", "brutto_euro": "367,00",
            "kurs_manuell": "0,2723", "kurs_bestaetigt": "1"}, follow_redirects=False)
        assert antwort.status_code == 303
        assert _beleg(datenbank)["kurs"] == "0.2723"

    def test_ausserhalb_der_spanne_einer_hauptwaehrung(self, conn, eingerichtet, datenordner):
        """Offline ohne Tabelle, US-Dollar: 11,606 (Komma verrutscht) und
        0,011606 liegen weit ausserhalb dessen, was der Dollar seit 1999 je
        kostete -- dieselbe Spanne wie beim Einlesen der EZB-Tabelle."""
        lesung = _aws(conn, datenordner)
        for kurs in ("11.606", "0.011606"):
            with pytest.raises(umrechnung.KursVonHandUnplausibel,
                               match="weit außerhalb dessen, was 1 Euro seit 1999 in USD"):
                p.buche(conn, lesung, _bestaetigt(lesung, kurs_manuell=kurs), datenordner)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0
        buchung = p.buche(conn, lesung, _bestaetigt(lesung, kurs_manuell="1.1606"), datenordner)
        assert buchung.umrechnung.euro_cent == AWS_EZB_CENT

    def test_kurs_lesen_meldet_verstaendlich(self):
        """Zu gross heisst zu gross, nicht "größer als null"; ein sehr
        kleiner Kurs steht nie als "1E-7" da."""
        with pytest.raises(ValueError, match="unplausibel groß"):
            kurse.lies_kurs("123456789")
        assert kurse.lies_kurs("0,0000001") == "0.0000001"
        assert kurse.kurs_de("1E-7") == "0,0000001"


class TestBelegliste:
    def test_umrechnung_ist_nachpruefbar(self, klient, datenbank):
        """Unter dem Betrag Fremdbetrag, Kurs und Kursdatum; unter "Ändern"
        Quelle, Abrufzeit und die volle SHA-256 der Kurstabelle."""
        _aws_buchen(klient, datenbank)
        zeile = _zeile(klient)
        assert "1.063,73 €" in zeile
        assert "1.234,56 USD zum EZB-Kurs 1,1606 vom 03.03.2026" in zeile
        assert "<dt>Quelle</dt><dd>EZB-Referenzkurs</dd>" in zeile
        assert SHA_PRUEFDATEI in zeile
        assert "<dt>Kurstabelle</dt><dd>abgerufen " in zeile
        assert 'action="/belege/1/kartenbetrag"' in zeile
        assert 'action="/belege/1/kurs-pruefen"' in zeile

    def test_stand_der_kurstabelle_steht_auf_der_seite(self, klient):
        """Bis wann die Tabelle reicht, wann sie abgerufen wurde, ihre Pruefsumme."""
        text = klient.get("/belege").text
        assert "EZB-Kurstabelle im Datenordner: Kurse bis 10.04.2026" in text
        assert SHA_PRUEFDATEI[:16] in text

    def test_kartenbetrag_nachtragen(self, klient, datenbank, tmp_path):
        """Der Kartenbetrag ersetzt den Euro-Betrag in Beleg, Zahlung und EUeR;
        jede Aenderung steht mit altem und neuem Wert im Journal; der EZB-Wert
        bleibt zum Vergleich sichtbar (Differenz in Euro und Prozent)."""
        _aws_buchen(klient, datenbank, bezahlt_am="2026-03-03")
        assert _ausgaben_2026(tmp_path) == AWS_EZB_CENT
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.080,00"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "Betrag laut Kreditkarte 1.080,00 € übernommen" in _meldung(antwort)
        b = _beleg(datenbank)
        assert (b["betrag_brutto_cent"], b["betrag_karte_cent"],
                b["betrag_umgerechnet_cent"], b["betrag_betrieblich_cent"]) == \
            (108000, 108000, AWS_EZB_CENT, 108000)
        assert _zahlungen(datenbank) == [("2026-03-03", -108000)]
        assert _ausgaben_2026(tmp_path) == 108000
        with datenbank() as conn:
            aenderungen = {(z["objekttyp"], z["feld"]): (z["wert_alt"], z["wert_neu"])
                           for z in conn.execute("SELECT * FROM journal"
                                                 " WHERE aktion = 'AENDERN'")}
        assert aenderungen[("beleg", "betrag_karte_cent")] == (None, "108000")
        assert aenderungen[("beleg", "betrag_brutto_cent")] == (str(AWS_EZB_CENT), "108000")
        assert aenderungen[("zahlung", "betrag_cent")] == (str(-AWS_EZB_CENT), "-108000")
        zeile = _zeile(klient)
        assert "1.234,56 USD · Betrag laut Kreditkarte" in zeile
        assert "EZB-Wert 1.063,73 €, Differenz +16,27 € / +1,53 %" in zeile
        assert "<dt>Differenz</dt><dd>+16,27 € / +1,53 %</dd>" in zeile

    def test_kartenbetrag_weit_daneben_fragt_in_der_liste_nach(self, klient, datenbank):
        """Mehr als 10 % Abweichung: die Liste zeigt die Rueckfrage an der
        Zeile, mit dem getippten Betrag; erst bestaetigt gilt er."""
        _aws_buchen(klient, datenbank)
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.234,56"})
        assert antwort.status_code == 422
        zeile = re.search(r'<tr id="beleg-1">.*?</tr>', antwort.text, re.S).group(0)
        assert "<details open>" in zeile
        assert "weicht um +170,83 € / +16,06 %" in zeile
        assert 'value="1.234,56"' in zeile and 'name="bestaetigt"' in zeile
        assert _beleg(datenbank)["betrag_karte_cent"] is None
        antwort = klient.post("/belege/1/kartenbetrag",
                              data={"betrag": "1.234,56", "bestaetigt": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _beleg(datenbank)["betrag_brutto_cent"] == 123456

    def test_ohne_umgerechneten_wert_zaehlt_der_bisherige_betrag(self, klient, datenbank):
        """Offline nur mit Kartenbetrag gebucht (kein umgerechneter Wert):
        ein Kartenbetrag mal zehn ging vorher ohne Rueckfrage durch. Jetzt
        wird mit dem bisherigen Euro-Betrag verglichen."""
        _aws_buchen(klient, datenbank, brutto_eur="1.080,00")
        with datenbank() as conn:
            conn.execute("UPDATE beleg SET kurs = NULL, kurs_datum = NULL, kurs_quelle = NULL,"
                         " kurs_abruf_utc = NULL, kurs_tabelle_sha256 = NULL,"
                         " betrag_umgerechnet_cent = NULL WHERE id = 1")
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "10.800,00"})
        assert antwort.status_code == 422
        assert "vom bisherigen Euro-Betrag (1.080,00 €) ab — mehr als 10 %" in antwort.text
        assert _beleg(datenbank)["betrag_brutto_cent"] == 108000
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.081,00"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _beleg(datenbank)["betrag_brutto_cent"] == 108100

    def test_derselbe_kartenbetrag_noch_einmal(self, klient, datenbank):
        """Derselbe Betrag noch einmal: "nichts geändert" statt "übernommen
        (vorher 1.080,00 €)", und keine erneute Rueckfrage zu einem schon
        bestaetigten Betrag."""
        _aws_buchen(klient, datenbank, brutto_eur="1.234,56", karte_bestaetigt="1")
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.234,56"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _meldung(antwort) == ("Beleg 1: Betrag laut Kreditkarte 1.234,56 € war schon "
                                     "eingetragen — nichts geändert.")

    def test_teilzahlung_bleibt_eine_teilzahlung(self, klient, datenbank, tmp_path):
        """Eine Rate ueber 530,00 EUR (wie ein Altbestand): der Kartenbetrag
        aendert den Beleg, die Rate bleibt -- vorher wurde aus ihr der volle
        Betrag, und die EUeR zaehlte 1.080,00 EUR statt 530,00 EUR."""
        _aws_buchen(klient, datenbank)
        with datenbank() as conn:
            conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                         " VALUES (1, '2026-03-05', -53000, 'UEBERWEISUNG')")
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.080,00"},
                              follow_redirects=False)
        assert "Die Zahlung über 530,00 € ist eine Teilzahlung und bleibt, wie sie ist" in \
            _meldung(antwort)
        assert _beleg(datenbank)["betrag_brutto_cent"] == 108000
        assert _zahlungen(datenbank) == [("2026-03-05", -53000)]
        assert _ausgaben_2026(tmp_path) == 53000

    def test_anlagegut_im_verzeichnis_bleibt_unveraendert(self, klient, datenbank):
        """Steht der Beleg im Anlagenverzeichnis, sind die Anschaffungskosten
        der Euro-Betrag -- nachtraeglich aendert ihn kein Kartenbetrag."""
        _aws_buchen(klient, datenbank, anlagegut="1", kategorie_id="")
        conn = db.oeffne(Path(klient.app.state.datenpfad))
        try:
            anlagen.anlegen(conn, bezeichnung="Server", anschaffung_am="2026-03-03",
                            ak_brutto_cent=AWS_EZB_CENT, nutzungsdauer_jahre=3, beleg_id=1)
        finally:
            conn.close()
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.080,00"},
                              follow_redirects=False)
        assert "steht als „Server“ im Anlagenverzeichnis" in _meldung(antwort)
        assert _beleg(datenbank)["betrag_brutto_cent"] == AWS_EZB_CENT


class TestMeldungAnDerZeile:
    """Das Ergebnis einer Aktion an einem Beleg (Kurs pruefen, Kartenbetrag,
    Zahlung) erscheint dort, wo geklickt wurde. Vorher sprang die
    Weiterleitung auf die Zeile, die Meldung stand oben auf der Seite --
    ausserhalb des Fensters, und fuer den Nutzer passierte scheinbar nichts."""

    def _seite(self, klient, antwort) -> str:
        ort = antwort.headers["location"]
        assert ort.endswith("#meldung") and "beleg=1" in ort, ort
        return klient.get(ort.split("#")[0]).text

    def test_ergebnis_von_kurs_pruefen_steht_ueber_der_zeile(self, klient, datenbank):
        """Die Meldung steht direkt ueber der Zeile (id="meldung", der Sprung
        geht dorthin), die Einzelheiten sind aufgeklappt, oben steht sie
        nicht noch einmal."""
        _aws_buchen(klient, datenbank)
        text = self._seite(klient, klient.post("/belege/1/kurs-pruefen",
                                               follow_redirects=False))
        zeilenmeldung = re.search(
            r'<tr class="zeilenmeldung[^"]*">.*?id="meldung".*?</tr>\s*<tr id="beleg-1">',
            text, re.S)
        assert zeilenmeldung and "stimmt mit der gespeicherten EZB-Kurstabelle" in \
            zeilenmeldung.group(0)
        assert text.count('id="meldung"') == 1
        assert "<details open>" in _zeile_aus(text)

    def test_fehler_beim_kartenbetrag_und_bei_der_zahlung(self, klient, datenbank):
        """Ein unlesbarer Kartenbetrag und eine Zahlung, deren Kurs noch fehlt:
        beide Meldungen an der Zeile, nicht oben."""
        _aws_buchen(klient, datenbank)
        text = self._seite(klient, klient.post("/belege/1/kartenbetrag",
                                               data={"betrag": "abc"}, follow_redirects=False))
        assert re.search(r'class="meldung fehler" id="meldung"\s+role="alert">„abc“ ist kein '
                         "Betrag", text)
        text = self._seite(klient, klient.post("/belege/1/bezahlt", data={"datum": "2026-04-14"},
                                               follow_redirects=False))
        assert re.search(r'id="meldung"\s+role="alert">Zahlung nicht erfasst', text)

    def test_beleg_ausserhalb_der_liste_meldet_oben(self, klient, datenbank):
        """Steht der Beleg nicht in der Liste (gefiltert, unbekannt), steht die
        Meldung wie jede andere oben."""
        _aws_buchen(klient, datenbank)
        text = klient.get("/belege", params={"erfolg": "Erledigt.", "beleg": "99"}).text
        assert '<div class="meldung erfolg nicht-drucken">Erledigt.</div>' in text
        assert "zeilenmeldung" not in text

    def test_rueckfrage_zum_kartenbetrag_springt_in_den_kasten(self, klient, datenbank):
        """Die 422 mit der Rueckfrage: die Seite springt zum Kasten und
        fokussiert das Feld, wie beim Eingangsordner."""
        _aws_buchen(klient, datenbank)
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.234,56"})
        assert antwort.status_code == 422
        assert 'id="karte-rueckfrage"' in antwort.text
        assert 'getElementById("karte-rueckfrage")' in antwort.text
        assert "scrollIntoView" in antwort.text


def _zeile_aus(text: str, beleg_id: int = 1) -> str:
    return re.search(rf'<tr id="beleg-{beleg_id}">.*?</tr>', text, re.S).group(0)


class TestBezahlt:
    def test_anderer_zahlungstag_zieht_die_umrechnung_nach(self, klient, datenbank,
                                                           tmp_path):
        """Gebucht zum Kurs des Belegdatums, bezahlt zwei Wochen spaeter: der
        Beleg wird auf den Kurs des Zahlungstags umgerechnet, die Zahlung
        bekommt denselben Betrag -- die beiden widersprechen sich nie."""
        _aws_buchen(klient, datenbank)
        antwort = klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"},
                              follow_redirects=False)
        assert "auf den Zahlungstag umgerechnet" in _meldung(antwort)
        assert "zum EZB-Kurs 1,1555 vom 20.03.2026 = 1.068,42 €" in _meldung(antwort)
        b = _beleg(datenbank)
        assert (b["kurs"], b["kurs_datum"], b["betrag_brutto_cent"]) == \
            ("1.1555", "2026-03-20", AWS_20_MAERZ_CENT)
        assert _zahlungen(datenbank) == [("2026-03-20", -AWS_20_MAERZ_CENT)]
        assert _ausgaben_2026(tmp_path) == AWS_20_MAERZ_CENT
        with datenbank() as conn:
            kurs = conn.execute("SELECT wert_alt, wert_neu FROM journal WHERE aktion ="
                                " 'AENDERN' AND feld = 'kurs'").fetchone()
        assert tuple(kurs) == ("1.1606", "1.1555")

    def test_gleicher_kurs_aendert_nichts(self, klient, datenbank):
        """Bezahlt am Belegdatum: nichts umzurechnen, kein Journaleintrag."""
        _aws_buchen(klient, datenbank)
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-03"})
        assert _beleg(datenbank)["betrag_brutto_cent"] == AWS_EZB_CENT
        assert _zahlungen(datenbank) == [("2026-03-03", -AWS_EZB_CENT)]
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'AENDERN'"
                                ).fetchone()[0] == 0

    def test_kartenbetrag_bleibt(self, klient, datenbank):
        """Mit Kartenbetrag gibt es nichts umzurechnen: er IST der Abfluss."""
        _aws_buchen(klient, datenbank, brutto_eur="1.080,00")
        klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"})
        assert _beleg(datenbank)["betrag_brutto_cent"] == 108000
        assert _zahlungen(datenbank) == [("2026-03-20", -108000)]

    def test_anlagegut_wird_nicht_umgerechnet(self, klient, datenbank):
        """Anlagegut: im Zweifel nicht nachtraeglich aendern -- ein Hinweis."""
        _aws_buchen(klient, datenbank, anlagegut="1")
        antwort = klient.post("/belege/1/bezahlt", data={"datum": "2026-03-20"},
                              follow_redirects=False)
        assert "Anlagegut: der Euro-Betrag bleibt beim Kurs vom 03.03.2026" in \
            _meldung(antwort)
        assert _beleg(datenbank)["betrag_brutto_cent"] == AWS_EZB_CENT
        assert _zahlungen(datenbank) == [("2026-03-20", -AWS_EZB_CENT)]

    def test_ohne_kurs_des_zahlungstags_keine_zahlung(self, klient, datenbank):
        """Der Kurs des Zahlungstags fehlt (noch nicht in der Tabelle, kein
        Netz): keine Zahlung zu einem falschen Betrag, sondern eine Meldung."""
        _aws_buchen(klient, datenbank)
        antwort = klient.post("/belege/1/bezahlt", data={"datum": "2026-04-14"},
                              follow_redirects=False)
        meldung = _meldung(antwort)
        assert meldung.startswith("Zahlung nicht erfasst")
        assert "13.04.2026 ist noch nicht veröffentlicht" in meldung
        assert "Betrag laut Kreditkarte" in meldung
        assert _zahlungen(datenbank) == []


class TestKursPruefen:
    def test_stimmt(self, klient, datenbank):
        """Nachgerechnet aus der gespeicherten Tabelle: alles stimmt."""
        _aws_buchen(klient, datenbank)
        antwort = klient.post("/belege/1/kurs-pruefen", follow_redirects=False)
        assert "stimmt mit der gespeicherten EZB-Kurstabelle überein" in _meldung(antwort)

    def test_abweichung_wird_erkannt(self, klient, datenbank):
        """Ein Kurs, der nicht in der Tabelle steht und nicht zum Betrag
        passt: beides wird gemeldet -- und die Pruefung steht im Journal."""
        _aws_buchen(klient, datenbank)
        with datenbank() as conn:
            conn.execute("UPDATE beleg SET kurs = '1.1706' WHERE id = 1")
        meldung = _meldung(klient.post("/belege/1/kurs-pruefen", follow_redirects=False))
        assert "laut Tabelle 1,1606, gespeichert 1,1706" in meldung
        assert "ergibt 1.054,64 €, gespeichert sind 1.063,73 €" in meldung
        with datenbank() as conn:
            eintrag = conn.execute("SELECT * FROM journal WHERE aktion = 'KURS_PRUEFUNG'"
                                   ).fetchone()
        assert json.loads(eintrag["wert_neu"])["befunde"] == 2

    def test_geprueft_wird_gegen_die_verwendete_fassung(self, klient, datenbank, tmp_path):
        """Korrigiert die EZB spaeter einen Kurs, rechnet die Pruefung trotzdem
        aus der Fassung nach, mit der gebucht wurde -- und die meldet nichts."""
        _aws_buchen(klient, datenbank)
        korrigiert = KURS_CSV.read_text().replace("2026-03-03,1.1606,", "2026-03-03,1.1616,")
        kurse.speichere(tmp_path, ezb_zip(korrigiert), heute=HEUTE)
        meldung = _meldung(klient.post("/belege/1/kurs-pruefen", follow_redirects=False))
        assert "stimmt" in meldung
        (tmp_path / "kurse" / "verwendet" / f"{SHA_PRUEFDATEI}.zip").unlink()
        meldung = _meldung(klient.post("/belege/1/kurs-pruefen", follow_redirects=False))
        assert (f"die Fassung der Kurstabelle, mit der gebucht wurde (SHA-256 "
                f"{SHA_PRUEFDATEI[:12]}…), fehlt im Datenordner — nachgerechnet mit der "
                "aktuellen Tabelle") in meldung
        assert "laut Tabelle 1,1616, gespeichert 1,1606" in meldung

    def test_veraenderte_oder_erfundene_fassung_ist_ein_befund(self, klient, datenbank,
                                                               tmp_path):
        """Eine Fassung, deren Inhalt nicht mehr zu ihrer Pruefsumme passt, und
        eine erfundene Pruefsumme am Beleg: beides ist ein Befund, nicht
        "stimmt" -- vorher wurde still gegen die aktuelle Tabelle geprueft."""
        _aws_buchen(klient, datenbank)
        kurse.speichere(tmp_path, ezb_zip(KURS_CSV.read_text() + "\n"), heute=HEUTE)
        pfad = tmp_path / "kurse" / "verwendet" / f"{SHA_PRUEFDATEI}.zip"
        inhalt = bytearray(pfad.read_bytes())
        inhalt[-30] ^= 0x01
        pfad.unlink()  # eine harte Verknuepfung: nicht die Quelle mitaendern
        pfad.write_bytes(bytes(inhalt))
        meldung = _meldung(klient.post("/belege/1/kurs-pruefen", follow_redirects=False))
        assert "ist verändert (ihr Inhalt passt nicht mehr zu ihrer Prüfsumme)" in meldung
        assert "stimmt" not in meldung
        with datenbank() as conn:
            conn.execute("UPDATE beleg SET kurs_tabelle_sha256 = ? WHERE id = 1", ("0" * 64,))
        meldung = _meldung(klient.post("/belege/integritaet", follow_redirects=False))
        assert "(SHA-256 000000000000…), fehlt im Datenordner" in meldung
        assert "alle stimmen" not in meldung

    def test_integritaetspruefung_rechnet_mit(self, klient, datenbank):
        """Gesammelt: die Belegintegritaet prueft auch jede Umrechnung."""
        _aws_buchen(klient, datenbank)
        meldung = _meldung(klient.post("/belege/integritaet", follow_redirects=False))
        assert "1 Umrechnung in Euro nachgerechnet, alle stimmen." in meldung
        with datenbank() as conn:
            conn.execute("UPDATE beleg SET kurs_datum = '2026-03-02' WHERE id = 1")
        meldung = _meldung(klient.post("/belege/integritaet", follow_redirects=False))
        assert "Umrechnungen: Beleg 1:" in meldung
        assert "laut Tabelle 1,1698, gespeichert 1,1606" in meldung


class TestKursVonHandPruefen:
    """ "Kurs prüfen" verglich einen Kurs von Hand nie mit dem EZB-Kurs: ein
    offline mit 1,0 gebuchter Dollar-Beleg, oder eine nachtraeglich auf
    "von Hand" gestellte Quelle, galt als "stimmt"."""

    def test_offline_gebucht_dann_kommt_die_tabelle(self, conn, eingerichtet, datenordner):
        """Ohne Netz und ohne Tabelle mit Kurs von Hand 1,0 gebucht: sobald die
        Tabelle da ist, meldet die Pruefung beide Euro-Betraege."""
        lesung = _aws(conn, datenordner)
        p.buche(conn, lesung, _bestaetigt(lesung, kurs_manuell="1.0"), datenordner)
        assert umrechnung.pruefe(conn, datenordner).befunde == [], "ohne Tabelle kein Vergleich"
        tabelle_ablegen(datenordner)
        [befund] = umrechnung.pruefe(conn, datenordner).befunde
        assert befund["befund"] == (
            "für den 03.03.2026 gibt es den EZB-Kurs 1,1606 vom 03.03.2026, gebucht ist der "
            "Kurs von Hand 1,0: 1.234,56 € statt 1.063,73 € — liefert die EZB einen Kurs, "
            "gilt ihrer (unter „Ändern“: „Zum EZB-Kurs umrechnen“)")

    def test_nachtraeglich_auf_von_hand_gestellt(self, klient, datenbank):
        """Eine Faelschung "Quelle von Hand, Kurs 1,0, Betraege passend" wird
        erkannt -- je Beleg und in der Integritaetspruefung."""
        _aws_buchen(klient, datenbank)
        with datenbank() as conn:
            conn.execute("UPDATE beleg SET kurs_quelle = 'MANUELL', kurs = '1.0',"
                         " kurs_tabelle_sha256 = NULL, betrag_umgerechnet_cent = 123456,"
                         " betrag_brutto_cent = 123456, betrag_betrieblich_cent = 123456"
                         " WHERE id = 1")
        meldung = _meldung(klient.post("/belege/1/kurs-pruefen", follow_redirects=False))
        assert "gibt es den EZB-Kurs 1,1606" in meldung and "statt 1.063,73 €" in meldung
        meldung = _meldung(klient.post("/belege/integritaet", follow_redirects=False))
        assert "Umrechnungen: Beleg 1: für den 03.03.2026 gibt es den EZB-Kurs" in meldung

    def test_kurs_von_hand_ohne_ezb_kurs(self, klient, datenbank):
        """Dirham: die EZB hat keinen Kurs, der Kurs von Hand gilt. Die
        Meldung sagt, was nachgerechnet ist -- nicht "stimmt mit dem Kurs von
        Hand überein"."""
        antwort = _aws_buchen(klient, datenbank, waehrung="AED", kurs_manuell="4,2632")
        assert antwort.status_code == 303
        meldung = _meldung(klient.post("/belege/1/kurs-pruefen", follow_redirects=False))
        assert meldung == (
            "Beleg 1: 1.234,56 AED zum Kurs von Hand 4,2632 vom 03.03.2026 ergeben 289,59 € — "
            "nachgerechnet; die gespeicherte EZB-Kurstabelle hat für AED an diesem Tag keinen "
            "Kurs, der Kurs von Hand gilt.")


class TestZurueckZumEzbKurs:
    """Ein versehentlich bestaetigter Kartenbetrag liess sich nicht
    zuruecknehmen; als Ausweg blieb, den EZB-Wert als "Kreditkartenbetrag"
    einzutragen. Und ein offline gebuchter Kurs von Hand blieb, auch wenn die
    EZB fuer den Tag einen Kurs hat."""

    def test_kartenbetrag_entfernen(self, klient, datenbank, tmp_path):
        """Der umgerechnete EZB-Wert ist wieder der Euro-Betrag von Beleg und
        Zahlung, die Quelle wieder "EZB-Referenzkurs" -- mit Journal."""
        _aws_buchen(klient, datenbank, brutto_eur="1.234,56", karte_bestaetigt="1",
                    bezahlt_am="2026-03-03")
        assert _beleg(datenbank)["betrag_brutto_cent"] == 123456
        assert 'action="/belege/1/ezb-kurs"' in _zeile(klient)
        antwort = klient.post("/belege/1/ezb-kurs", follow_redirects=False)
        assert _meldung(antwort) == (
            "Beleg 1: Kartenbetrag entfernt — 1.234,56 USD zum EZB-Kurs 1,1606 vom 03.03.2026 "
            "= 1.063,73 € (vorher 1.234,56 €). Die EÜR rechnet ab jetzt damit.")
        b = _beleg(datenbank)
        assert (b["betrag_karte_cent"], b["betrag_brutto_cent"], b["kurs_quelle"]) == \
            (None, AWS_EZB_CENT, "EZB")
        assert _zahlungen(datenbank) == [("2026-03-03", -AWS_EZB_CENT)]
        assert _ausgaben_2026(tmp_path) == AWS_EZB_CENT
        with datenbank() as conn:
            karte = conn.execute("SELECT wert_alt, wert_neu FROM journal WHERE aktion ="
                                 " 'AENDERN' AND feld = 'betrag_karte_cent'").fetchall()
        assert tuple(karte[-1]) == ("123456", None)
        assert "<dt>Quelle</dt><dd>EZB-Referenzkurs</dd>" in _zeile(klient)

    def test_kurs_von_hand_durch_den_ezb_kurs_ersetzen(self, conn, eingerichtet, datenordner,
                                                       tmp_path):
        """Offline mit Kurs von Hand 1,0 gebucht; ist die Tabelle da, bietet
        die Liste "Zum EZB-Kurs umrechnen" an, und danach gilt der EZB-Kurs."""
        lesung = _aws(conn, datenordner)
        p.buche(conn, lesung, _bestaetigt(lesung, kurs_manuell="1.0"), datenordner)
        beleg = conn.execute("SELECT * FROM beleg").fetchone()
        assert not umrechnung.ezb_kurs_vorhanden(conn, datenordner, beleg)
        tabelle_ablegen(datenordner)
        assert umrechnung.ezb_kurs_vorhanden(conn, datenordner, beleg)
        kurs = umrechnung.ezb_kurs_fuer(conn, datenordner, beleg)
        umrechnung.zum_ezb_kurs(conn, beleg["id"], kurs, version=beleg["version"])
        neu = conn.execute("SELECT * FROM beleg").fetchone()
        assert (neu["kurs"], neu["kurs_quelle"], neu["betrag_brutto_cent"]) == \
            ("1.1606", "EZB", AWS_EZB_CENT)
        assert umrechnung.pruefe(conn, datenordner).befunde == []

    def test_ohne_ezb_kurs_traegt_wieder_der_kurs_von_hand(self, klient, datenbank):
        """Dirham mit Kurs von Hand und Kartenbetrag: entfernt, gilt wieder
        der Kurs von Hand."""
        _aws_buchen(klient, datenbank, waehrung="AED", kurs_manuell="4,2632",
                    brutto_eur="290,00")
        antwort = klient.post("/belege/1/ezb-kurs", follow_redirects=False)
        assert "Kartenbetrag entfernt — 1.234,56 AED zum Kurs von Hand 4,2632" in \
            _meldung(antwort)
        b = _beleg(datenbank)
        assert (b["betrag_karte_cent"], b["betrag_brutto_cent"], b["kurs_quelle"]) == \
            (None, b["betrag_umgerechnet_cent"], "MANUELL")


class TestExcel:
    def test_eigene_spalten_fuer_die_umrechnung(self, klient, datenbank):
        """Waehrung, Fremdbetrag (ohne Euro-Zeichen), Kurs als Zahl, Kursdatum,
        Quelle -- je Beleg nachvollziehbar ohne dieses Werkzeug."""
        _aws_buchen(klient, datenbank)
        antwort = _aws_buchen(klient, datenbank, aws_kopie(), brutto_eur="1.080,00",
                              belegnummer="Z-2", trotzdem="1")
        assert antwort.status_code == 303, antwort.text
        conn = db.oeffne(Path(klient.app.state.datenpfad))
        try:
            blatt = export_excel._mappe(conn)["Belege"]
        finally:
            conn.close()
        kopf = [z.value for z in blatt[1]]
        for spalte in ("Währung", "Betrag Fremdwährung", "Kurs (Einheiten je Euro)",
                       "Kursdatum", "Kursquelle", "Quelle des Euro-Betrags",
                       "Betrag umgerechnet", "Betrag laut Kreditkarte",
                       "Kurstabelle abgerufen (UTC)", "Kurstabelle SHA-256"):
            assert spalte in kopf, spalte

        def zelle(zeile, spalte):
            return blatt.cell(row=zeile, column=kopf.index(spalte) + 1)

        assert zelle(2, "Währung").value == "USD"
        assert zelle(2, "Betrag Fremdwährung").value == 1234.56
        assert "€" not in zelle(2, "Betrag Fremdwährung").number_format
        assert zelle(2, "Kurs (Einheiten je Euro)").value == 1.1606
        assert zelle(2, "Kursdatum").value == date(2026, 3, 3)
        assert zelle(2, "Kursquelle").value == "EZB-Referenzkurs"
        assert zelle(2, "Quelle des Euro-Betrags").value == "EZB-Referenzkurs"
        assert zelle(3, "Quelle des Euro-Betrags").value == "Kreditkartenbetrag"
        assert zelle(3, "Betrag laut Kreditkarte").value == 1080.0
        assert zelle(2, "Kurstabelle SHA-256").value == SHA_PRUEFDATEI


# ============================================================ Eingangsordner

@pytest.fixture
def postfach(tmp_path):
    ordner = tmp_path / "Steuer-Tool Belege"
    ordner.mkdir()
    return ordner


@pytest.fixture
def ordner_conn(datenordner, postfach):
    verbindung = db.oeffne(datenordner / "steuer.sqlite")
    verbindung.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, ust_idnr,"
        " belegordner_pfad) VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095',"
        " 'Hamburg', '12/345/67890', 'DE999999999', ?)", (str(postfach),))
    with pytest.MonkeyPatch.context() as patch:
        # Jede Datei gilt als fertig geschrieben (siehe test_belegordner).
        patch.setattr(abholen, "MINDESTALTER_SEKUNDEN", 0)
        patch.setattr(abholen, "UNLESBAR_MINDESTALTER_SEKUNDEN", 0, raising=False)
        abholen._NACHZUHOLEN.clear()
        yield verbindung
        abholen._NACHZUHOLEN.clear()
    verbindung.close()


def _abholen(conn, datenordner, postfach) -> abholen.Ergebnis:
    return abholen.abholen(conn, datenordner, postfach, automatisch=True,
                           heute=date(2026, 10, 24))


class TestEingangsordner:
    def test_usd_rechnung_eines_bekannten_lieferanten_wird_gebucht(
            self, ordner_conn, datenordner, postfach):
        """Eine Rechnung in USD von einem bekannten Lieferanten: mit EZB-Kurs
        fuer das Belegdatum wird sie gebucht wie eine in Euro."""
        tabelle_ablegen(datenordner)
        bekannter_lieferant(ordner_conn)
        lege(postfach, "Rechnung.xml", cii(waehrung="USD"))
        ergebnis = _abholen(ordner_conn, datenordner, postfach)
        assert [w.grund for w in ergebnis.wartend] == []
        [gebucht] = ergebnis.gebucht
        b = ordner_conn.execute("SELECT * FROM beleg WHERE id = ?",
                                (gebucht.beleg_id,)).fetchone()
        assert (b["waehrung"], b["betrag_fremdwaehrung_cent"]) == ("USD", 546)
        assert (b["kurs"], b["kurs_datum"], b["kurs_quelle"]) == ("1.1476", "2026-03-13", "EZB")
        assert b["betrag_brutto_cent"] == kurse.in_euro_cent(546, "1.1476") == 476
        assert b["geprueft_am"] is None, "automatisch: noch zu bestaetigen"

    def test_ohne_kurs_wartet_sie_mit_grund(self, ordner_conn, datenordner, postfach):
        """Keine Kurstabelle und kein Netz: die Rechnung wartet, der Grund sagt warum."""
        bekannter_lieferant(ordner_conn)
        lege(postfach, "Rechnung.xml", cii(waehrung="USD"))
        [wartend] = _abholen(ordner_conn, datenordner, postfach).wartend
        assert wartend.grund == (
            "Fremdwährung (USD) — die EZB-Kurstabelle ist noch nicht geladen: kein Netz im "
            "Test; in der Prüfmaske den Betrag laut Kreditkarte oder einen Kurs von Hand "
            "eintragen")

    def test_dieselbe_usd_rechnung_wird_nicht_zweimal_gebucht(
            self, ordner_conn, datenordner, postfach):
        """Dieselbe Rechnung noch einmal, als anderer Export mit anderer
        Nummer: gleicher Tag, gleicher Dollarbetrag. Die erste ist mit dem
        Kartenbetrag gebucht -- der Euro-Betrag unterscheidet sich, der
        Fremdbetrag nicht. Vorher buchte das Abholen sie ein zweites Mal."""
        tabelle_ablegen(datenordner)
        lid = bekannter_lieferant(ordner_conn)
        eingang = p.aufnehmen(cii(waehrung="USD"), "Rechnung.xml", datenordner)
        lesung = p.lies(ordner_conn, eingang, heute=date(2026, 10, 24))
        p.buche(ordner_conn, lesung, {"belegdatum": "2026-03-14", "brutto": 546,
                                      "waehrung": "USD", "brutto_eur": 489,
                                      "belegnummer": "RE-2026-0815"},
                datenordner, lieferant_id=lid)
        lege(postfach, "Rechnung-Kopie.xml", cii("RE-2026-0815-B", waehrung="USD"))
        [wartend] = _abholen(ordner_conn, datenordner, postfach).wartend
        assert wartend.grund.startswith("ähnlicher Beleg vorhanden")
        assert "546" not in wartend.grund and "5,46 USD (4,89 €)" in wartend.grund
        assert ordner_conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1


def _dollar_pdf(zeichen: str) -> bytes:
    """Eine Hetzner-Rechnung mit Textschicht, Betraege mit ``zeichen``."""
    return pdf_aus_zeilen(HETZNER_KOPF + [
        "Rechnung", "Rechnungsnummer: R0012345678", "Rechnungsdatum: 02.03.2026",
        "Kundennummer: K-99887", "Pos Bezeichnung Menge Einzelpreis Betrag",
        f"1 Cloud Server CX22 1 {zeichen}100,00 {zeichen}100,00",
        f"Nettobetrag {zeichen}100,00", f"Umsatzsteuer 19 % {zeichen}19,00",
        f"Gesamtbetrag {zeichen}119,00"])


class TestAuslesungFremdwaehrung:
    """"Einfach hochladen": bei der Pfund- und der Dirham-Rechnung blieb der
    Bruttobetrag leer ("Total" war kein Summenwort), und "AED" wurde nicht
    als Waehrung erkannt -- der Vorschlag zeigte erst nach dem Eintippen einen
    Euro-Betrag."""

    def test_total_ist_der_gesamtbetrag(self, tmp_path):
        """ "Total" als ganzes Wort traegt den Gesamtbetrag, "Subtotal" nicht."""
        from app.logik.beleg import text_leser

        pfad = tmp_path / "tf.pdf"
        pfad.write_bytes(pdf_aus_zeilen([
            "Typefoundry Ltd", "Invoice", "Invoice Number: TF-118",
            "Invoice Date: October 9, 2026", "Font licence, desktop £49.00",
            "Subtotal £49.00", "VAT £0.00", "Total £49.00 GBP"]))
        felder = text_leser.lies_datei(pfad, heute=date(2026, 10, 26)).felder
        assert (felder["brutto"].wert, felder["waehrung"].wert) == (4900, "GBP")
        from app.logik.beleg import felder as f
        assert f.finde_betrag("Subtotal 10.00\nTotal 12.00\nSubtotal 99.00",
                              f.BRUTTO_WOERTER, "EN").cent == 1200

    def test_code_am_betrag_ist_die_waehrung(self, tmp_path):
        """ "AED 367.00": Dirham, nicht Euro; CA$ und A$ sind kanadische und
        australische Dollar, kein US-Dollar."""
        from app.logik.beleg import text_leser

        pfad = tmp_path / "dh.pdf"
        pfad.write_bytes(pdf_aus_zeilen([
            "Desert Hosting FZ LLC", "Invoice", "Invoice Number: DH-77",
            "Invoice Date: October 13, 2026", "Server rental AED 367.00",
            "Total AED 367.00"]))
        felder = text_leser.lies_datei(pfad, heute=date(2026, 10, 26)).felder
        assert (felder["brutto"].wert, felder["waehrung"].wert) == (36700, "AED")
        assert not felder["waehrung"].sicher, "geraten bleibt geraten"
        assert text_leser.finde_waehrung("PHP 8.2 Hosting 10,00 EUR") == "EUR"
        assert text_leser.finde_waehrung("Total CA$119.00") == "CAD"


class TestEingangsordnerGerateneWaehrung:
    """Aus der Textschicht ist die Waehrung nur geraten: jedes "$" gilt als
    US-Dollar. Ein Betrag in kanadischen oder australischen Dollar stand so
    ohne Rueckfrage mit dem USD-Kurs in der EUeR."""

    @pytest.mark.parametrize("zeichen, geraten", [("CA$", "CAD"), ("A$", "AUD"), ("$", "USD")])
    def test_wartet_statt_mit_dem_usd_kurs_zu_buchen(self, ordner_conn, datenordner,
                                                       postfach, zeichen, geraten):
        """Wartet mit Grund in der Pruefmaske, gebucht wird nichts -- auch
        nicht beim blossen "$". Geraten wird inzwischen der richtige Dollar."""
        tabelle_ablegen(datenordner)
        bekannter_lieferant(ordner_conn)
        lege(postfach, "Rechnung.pdf", _dollar_pdf(zeichen))
        ergebnis = _abholen(ordner_conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund == (f"Währung nicht sicher erkannt (vermutlich {geraten}) — bitte "
                                 "in der Prüfmaske bestätigen")
        assert ordner_conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0

    def test_gemerkte_waehrung_des_lieferanten_gilt(self, ordner_conn, datenordner, postfach):
        """Ist US-Dollar die gemerkte Waehrung des Lieferanten, bestaetigt das
        die Auslesung -- gebucht wird zum EZB-Kurs."""
        tabelle_ablegen(datenordner)
        bekannter_lieferant(ordner_conn, waehrung="USD")
        lege(postfach, "Rechnung.pdf", _dollar_pdf("$"))
        [gebucht] = _abholen(ordner_conn, datenordner, postfach).gebucht
        b = ordner_conn.execute("SELECT * FROM beleg").fetchone()
        assert (b["id"], b["waehrung"], b["betrag_fremdwaehrung_cent"], b["kurs"]) == \
            (gebucht.beleg_id, "USD", 11900, "1.1698")


# ===================================================================== Start

class TestStart:
    def test_frischt_die_tabelle_im_hintergrund_auf(self, tmp_path, monkeypatch):
        """Ist die Tabelle aelter als ein Tag, laedt der Start sie im
        Hintergrund neu -- hier mit einem Ersatz fuer das Netz."""
        tabelle_ablegen(tmp_path, jetzt=datetime.now(UTC) - timedelta(days=2))
        neuer = KURS_CSV.read_text().replace(
            "Date,USD,JPY,GBP,CHF,ISK,RUB,\n",
            "Date,USD,JPY,GBP,CHF,ISK,RUB,\n2026-04-13,1.17,186.0,0.871,0.924,N/A,N/A,\n")
        abrufe = []
        monkeypatch.setattr(kurse, "netzabruf",
                            lambda url: abrufe.append(url) or ezb_zip(neuer))
        with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
            c.get("/gesund")
            c.app.state.kurse_faden.join(10)
            assert abrufe == [kurse.QUELLE]
            assert "Kurse bis 13.04.2026" in c.app.state.kurse_ergebnis
        assert kurse.tabelle(tmp_path).letzter_tag == date(2026, 4, 13)

    def test_offline_bleibt_die_vorhandene(self, tmp_path):
        """Ohne Netz: der Start laeuft, die vorhandene Tabelle bleibt."""
        alt = tabelle_ablegen(tmp_path, jetzt=datetime.now(UTC) - timedelta(days=2))
        with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
            c.get("/gesund")
            c.app.state.kurse_faden.join(10)
            assert "kein Netz" in c.app.state.kurse_ergebnis
        assert kurse.tabelle(tmp_path).sha256 == alt.sha256

    def test_beschaedigte_tabelle_gilt_als_veraltet(self, tmp_path, monkeypatch):
        """Die gespeicherte Tabelle ist abgeschnitten, stand.json aber frisch:
        der Start laedt sie neu (vorher erst am naechsten Tag), und ohne Netz
        sagt die Meldung, dass die Tabelle nicht lesbar ist -- nicht "noch
        nicht geladen"."""
        tabelle_ablegen(tmp_path)
        pfad = tmp_path / "kurse" / kurse.ROHDATEI
        pfad.write_bytes(pfad.read_bytes()[:400])
        assert kurse.abgerufen_vor(tmp_path) == timedelta.max
        with pytest.raises(kurse.KeinKurs) as fehler:
            kurse.kurs_besorgen(tmp_path, "USD", date(2026, 3, 13), heute=HEUTE)
        assert str(fehler.value) == (
            "Die gespeicherte EZB-Kurstabelle ist nicht lesbar (das ZIP-Archiv der Kurstabelle "
            "ist beschädigt oder unvollständig) und ließ sich nicht neu laden: kein Netz im "
            "Test.")
        kurse._FEHLSCHLAG.clear()
        abrufe = []
        # Ohne lesbare Tabelle gilt der Abruf als erster: er muss vollstaendig sein.
        voll = ezb_zip(csv_voll())
        monkeypatch.setattr(kurse, "netzabruf", lambda url: abrufe.append(url) or voll)
        with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
            c.get("/gesund")
            c.app.state.kurse_faden.join(10)
        assert abrufe == [kurse.QUELLE]
        assert kurse.tabelle(tmp_path).sha256 == kurse.pruefsumme(voll)

    def test_frische_tabelle_und_nur_euro_laden_nichts(self, tmp_path, monkeypatch):
        """Eine Tabelle von heute wird nicht neu geladen; ohne Tabelle und
        ohne Fremdwaehrung auch nicht."""
        monkeypatch.setattr(kurse, "netzabruf", lambda url: pytest.fail("geladen"))
        with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
            c.get("/gesund")
            assert c.app.state.kurse_faden is None
        tabelle_ablegen(tmp_path)
        with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
            c.get("/gesund")
            assert c.app.state.kurse_faden is None

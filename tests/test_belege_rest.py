"""Regressionstests zur letzten Korrekturrunde im Bereich Belege und Reisen.

Grundlage ist die Gegenprobe nach der Fehleranalyse (Abschnitte 5 und 7 sowie
die Beleg- und Reisepunkte des Querschnitts): Doppelklicks auf „Beleg buchen“
und „Bezahlt“, die Eigenbeleg-Route ohne Prüfung von Kategorie und Reise,
Datumsfelder ohne gemeinsames Fenster, Formularzahlen ohne Obergrenze und
Kennungen ab 2**63 in der Adresse.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.logik.beleg import pipeline as p
from app.main import erzeuge_app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}

GROSS = str(2**63)
HEUTE = dt.date.today()
MORGEN = (HEUTE + dt.timedelta(days=1)).isoformat()


def _klient(ordner: Path) -> TestClient:
    return TestClient(erzeuge_app(ordner / "steuer.sqlite"), raise_server_exceptions=False)


@pytest.fixture
def klient(tmp_path):
    with _klient(tmp_path) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


def _db(ordner: Path) -> sqlite3.Connection:
    verbindung = sqlite3.connect(ordner / "steuer.sqlite")
    verbindung.row_factory = sqlite3.Row
    return verbindung


@pytest.fixture
def datenbank(tmp_path):
    return lambda: _db(tmp_path)


def _meldung(antwort) -> str:
    frage = parse_qs(urlparse(antwort.headers.get("location", "")).query)
    return (frage.get("fehler") or frage.get("erfolg") or [""])[0]


def _erfolg(antwort) -> str:
    return parse_qs(urlparse(antwort.headers.get("location", "")).query).get("erfolg", [""])[0]


def _anzahl(datenbank, tabelle: str = "beleg") -> int:
    with datenbank() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabelle}").fetchone()[0]


def _hochladen(klient, inhalt: bytes = None, name: str = "a.pdf") -> str:
    antwort = klient.post("/belege/hochladen", files={
        "datei": (name, inhalt if inhalt is not None else PDF_DE.read_bytes(),
                  "application/pdf")}, follow_redirects=False)
    return antwort.headers["location"].rsplit("/", 1)[1]


def _kategorie(datenbank, bereich: int = 25) -> int:
    with datenbank() as conn:
        return conn.execute("SELECT id FROM euer_kategorie WHERE bereich = ? ORDER BY id"
                            " LIMIT 1", (bereich,)).fetchone()[0]


BUCHEN = {"belegdatum": "2026-02-01", "brutto_euro": "119,00", "netto_euro": "100,00",
          "ust_euro": "19,00", "ust_satz": "19", "quote": "100", "trotzdem": "1"}


def _parallel(anzahl: int, aufruf) -> list:
    """Startet ``aufruf`` in ``anzahl`` Threads gleichzeitig (wie ein Doppelklick)."""
    ergebnisse: list = [None] * anzahl
    sperre = threading.Barrier(anzahl)

    def lauf(i: int) -> None:
        try:
            sperre.wait()
            ergebnisse[i] = aufruf(i)
        except Exception as fehler:  # noqa: BLE001 -- der Test wertet ihn aus
            ergebnisse[i] = fehler

    threads = [threading.Thread(target=lauf, args=(i,)) for i in range(anzahl)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ergebnisse


def _ohne_absturz(antworten) -> None:
    for antwort in antworten:
        assert not isinstance(antwort, Exception), antwort
        assert antwort.status_code < 500, antwort.text[:400]
        assert "Da ist etwas schiefgegangen" not in antwort.text


# ------------------------------------------------- 1. Doppelklick „Beleg buchen“

class TestDoppelklickBuchen:
    """Zwei gleichzeitige POST auf dieselbe Eingangsdatei. Vorher: 500
    (FileNotFoundError) oder -- schlimmer -- die verlierende Anfrage schob beim
    Aufräumen die Archivdatei des GEBUCHTEN Belegs zurück in den Eingang."""

    def _runde(self, ordner: Path, *, lieferant_neu: bool) -> None:
        with _klient(ordner) as c:
            c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
            name = _hochladen(c)
            kategorie = _kategorie(lambda: _db(ordner))
            daten = {**BUCHEN, "kategorie_id": str(kategorie)}
            if lieferant_neu:
                daten["lieferant_neu"] = "ParLief"
            antworten = _parallel(2, lambda i: c.post(f"/belege/pruefen/{name}", data=daten,
                                                      follow_redirects=False))
            _ohne_absturz(antworten)
            meldungen = sorted(_meldung(a) for a in antworten)
            assert any(m.startswith("Beleg 1 erfasst.") for m in meldungen), meldungen
            assert any("bereits als Beleg 1 gebucht" in m for m in meldungen), meldungen

            with _db(ordner) as conn:
                assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1
                pfad = conn.execute("SELECT pfad FROM belegdatei").fetchone()[0]
                if lieferant_neu:
                    assert conn.execute("SELECT COUNT(*) FROM lieferant").fetchone()[0] == 1
            assert (ordner / pfad).is_file(), "Archivdatei des gebuchten Belegs fehlt"
            assert not (ordner / p.EINGANG / name).exists()
            integritaet = c.post("/belege/integritaet", follow_redirects=False)
            assert "alle unverändert" in _erfolg(integritaet), _meldung(integritaet)

    def test_zehn_runden_genau_ein_beleg(self, tmp_path):
        for runde in range(10):
            self._runde(tmp_path / f"r{runde}", lieferant_neu=False)

    def test_mit_neuem_lieferanten(self, tmp_path):
        for runde in range(5):
            self._runde(tmp_path / f"l{runde}", lieferant_neu=True)

    def test_zweite_buchung_derselben_lesung_laesst_das_archiv_stehen(self, klient, datenbank,
                                                                      tmp_path):
        """Logik-Ebene: dieselbe Lesung zweimal gebucht. Die zweite erkennt den
        Beleg innerhalb der Sperre und bewegt keine Datei."""
        name = _hochladen(klient)
        pfad = tmp_path / p.EINGANG / name
        eingang = p.Eingang(pfad=pfad, original_name="a.pdf", sha256=p.pruefsumme(pfad),
                            groesse=pfad.stat().st_size)
        with datenbank() as conn:
            conn.isolation_level = None
            lesung = p.lies(conn, eingang)
            bestaetigt = {"belegdatum": "2026-03-14", "brutto": 829, "waehrung": "EUR"}
            erste = p.buche(conn, lesung, bestaetigt, tmp_path)
            with pytest.raises(p.SchonGebucht) as fehler:
                p.buche(conn, lesung, bestaetigt, tmp_path)
            assert fehler.value.beleg_id == erste.beleg_id
        assert erste.pfad.is_file()
        assert _anzahl(datenbank) == 1

    def test_neuer_lieferant_den_es_schon_gibt_wird_genommen(self, klient, datenbank):
        """Ein UNIQUE-Konflikt auf dem Namen (gleich nach NOCASE) ist kein 500."""
        with datenbank() as conn:
            conn.execute("INSERT INTO lieferant (name, angelegt_am)"
                         " VALUES ('ParLief', '2026-01-01')")
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **BUCHEN, "lieferant_neu": "parlief"}, follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM lieferant").fetchone()[0] == 1
            assert conn.execute("SELECT l.name FROM beleg b JOIN lieferant l"
                                " ON l.id = b.lieferant_id").fetchone()[0] == "ParLief"


# ---------------------------------------- 7. Doppelklick bei von Hand abgelegter Datei

class TestHandabgelegt:
    def _ablegen(self, tmp_path, name: str = "Mein Beleg.pdf", inhalt: bytes = None) -> Path:
        ordner = tmp_path / p.EINGANG
        ordner.mkdir(exist_ok=True)
        ziel = ordner / name
        ziel.write_bytes(inhalt if inhalt is not None else PDF_DE.read_bytes())
        return ziel

    def test_zweiter_post_meldet_bereits_gebucht(self, klient, datenbank, tmp_path):
        self._ablegen(tmp_path)
        adresse = "/belege/pruefen/Mein%20Beleg.pdf"
        erste = klient.post(adresse, data=BUCHEN, follow_redirects=False)
        assert _erfolg(erste).startswith("Beleg 1 erfasst."), _meldung(erste)
        zweite = klient.post(adresse, data=BUCHEN, follow_redirects=False)
        assert _erfolg(zweite) == "Diese Datei wurde bereits als Beleg 1 gebucht."
        assert klient.get(adresse, follow_redirects=False).headers["location"] == \
            zweite.headers["location"]
        assert _anzahl(datenbank) == 1

    def test_gleichzeitig(self, klient, datenbank, tmp_path):
        self._ablegen(tmp_path)
        antworten = _parallel(2, lambda i: klient.post(
            "/belege/pruefen/Mein%20Beleg.pdf", data=BUCHEN, follow_redirects=False))
        _ohne_absturz(antworten)
        assert any("bereits als Beleg 1 gebucht" in _meldung(a) for a in antworten)
        assert _anzahl(datenbank) == 1


# ----------------------------------------------- 6. Inhaltsprüfung im Eingang

class TestInhaltImEingang:
    def test_programm_mit_pdf_endung_ist_nicht_buchbar(self, klient, datenbank, tmp_path):
        ordner = tmp_path / p.EINGANG
        ordner.mkdir(exist_ok=True)
        (ordner / "programm.pdf").write_bytes(b"MZ\x90\x00" + b"\x00" * 200)

        liste = klient.get("/belege").text
        assert "Inhalt passt nicht zur Endung" in liste
        assert "/belege/pruefen/programm.pdf" not in liste

        maske = klient.get("/belege/pruefen/programm.pdf", follow_redirects=False)
        assert maske.status_code == 303
        assert "ist kein PDF — die Endung passt nicht zum Inhalt" in _meldung(maske)

        antwort = klient.post("/belege/pruefen/programm.pdf", data=BUCHEN,
                              follow_redirects=False)
        assert "ist kein PDF — die Endung passt nicht zum Inhalt" in _meldung(antwort)
        assert _anzahl(datenbank) == 0
        assert (ordner / "programm.pdf").exists()
        assert not (tmp_path / p.ARCHIV).exists() or not any((tmp_path / p.ARCHIV).rglob("*.pdf"))

    def test_pipeline_bucht_keinen_fremden_inhalt(self, klient, datenbank, tmp_path):
        ordner = tmp_path / p.EINGANG
        ordner.mkdir(exist_ok=True)
        pfad = ordner / "bild.png"
        pfad.write_bytes(b"GIF89a" + b"\x00" * 50)
        eingang = p.Eingang(pfad=pfad, original_name="bild.png", sha256=p.pruefsumme(pfad),
                            groesse=pfad.stat().st_size)
        lesung = p.Lesung(eingang=eingang, vorschlag=p.Vorschlag("manuell", {}, ()))
        with datenbank() as conn:
            conn.isolation_level = None
            with pytest.raises(p.BelegFehler, match="kein PNG-Bild"):
                p.buche(conn, lesung, {"belegdatum": "2026-03-14", "brutto": 500}, tmp_path)
        assert pfad.exists()


# ------------------------------------------------- 2. Doppelklick „Bezahlt“

class TestDoppelklickBezahlt:
    def test_genau_eine_zahlung(self, klient, datenbank):
        for runde in range(5):
            klient.post("/belege/eigenbeleg", data={
                "belegdatum": "2026-02-10", "brutto_euro": "5,00", "quote": "100",
                "grund": "Ein ausreichend langer Grund", "bezahlt_am": ""})
            with datenbank() as conn:
                beleg = conn.execute("SELECT MAX(id) FROM beleg").fetchone()[0]
            antworten = _parallel(2, lambda i, beleg=beleg: klient.post(
                f"/belege/{beleg}/bezahlt", data={"datum": "2026-02-11"},
                follow_redirects=False))
            _ohne_absturz(antworten)
            assert any("bereits bezahlt (am 11.02.2026)" in _meldung(a) for a in antworten)
            with datenbank() as conn:
                anzahl, summe = conn.execute(
                    "SELECT COUNT(*), SUM(betrag_cent) FROM zahlung WHERE beleg_id = ?",
                    (beleg,)).fetchone()
            assert (anzahl, summe) == (1, -500), f"Runde {runde}"


# ---------------------------------------------------- 3. Eigenbeleg-Route

EIGEN = {"belegdatum": "2026-03-14", "brutto_euro": "4,90", "quote": "100",
         "bezeichnung": "Parkgebühr",
         "grund": "Parkautomat ohne Belegausgabe, Termin bei Mustermann GmbH",
         "bezahlt_am": "2026-03-14"}


class TestEigenbelegRoute:
    @pytest.mark.parametrize("feld, wert, meldung", [
        ("kategorie_id", "abc", "Diese Kategorie gibt es nicht."),
        ("kategorie_id", "9999", "Diese Kategorie gibt es nicht."),
        ("kategorie_id", "-1", "Diese Kategorie gibt es nicht."),
        ("kategorie_id", "²", "Diese Kategorie gibt es nicht."),
        ("kategorie_id", GROSS, "Diese Kategorie gibt es nicht."),
        ("reise_id", "abc", "Diese Reise gibt es nicht."),
        ("reise_id", "9999", "Diese Reise gibt es nicht."),
        ("reise_id", GROSS, "Diese Reise gibt es nicht."),
        ("quote", "abc", "ist keine Quote"),
        ("quote", GROSS, "ist keine Quote"),
        ("quote", "101", "zwischen 0 und 100"),
        ("brutto_euro", GROSS, "unplausibel groß"),
        ("brutto_euro", "inf", "ist kein Betrag"),
    ])
    def test_meldung_statt_absturz(self, klient, datenbank, feld, wert, meldung):
        antwort = klient.post("/belege/eigenbeleg", data={**EIGEN, feld: wert})
        assert antwort.status_code == 422
        assert meldung in antwort.text
        assert _anzahl(datenbank) == 0
        # Die Eingaben bleiben stehen, allen voran die Begründung.
        assert "Parkautomat ohne Belegausgabe, Termin bei Mustermann GmbH</textarea>" \
            in antwort.text
        assert 'value="Parkgebühr"' in antwort.text

    def test_doppelfeld_nimmt_den_letzten_wert(self, klient, datenbank):
        antwort = klient.post("/belege/eigenbeleg", content=(
            "belegdatum=2026-03-14&brutto_euro=4,90&grund=Parkautomat+ohne+Belegausgabe"
            "&kategorie_id=6&kategorie_id=x"),
            headers={"content-type": "application/x-www-form-urlencoded"})
        assert antwort.status_code == 422
        assert "Diese Kategorie gibt es nicht." in antwort.text

    def test_einnahmenkategorie_wird_abgewiesen(self, klient, datenbank):
        einnahme = _kategorie(datenbank, bereich=20)
        antwort = klient.post("/belege/eigenbeleg",
                              data={**EIGEN, "kategorie_id": str(einnahme)})
        assert antwort.status_code == 422
        assert "keine Ausgabenkategorie" in antwort.text
        assert _anzahl(datenbank) == 0

    def test_gewaehlte_kategorie_bleibt_ausgewaehlt(self, klient, datenbank):
        ausgabe = _kategorie(datenbank)
        antwort = klient.post("/belege/eigenbeleg", data={
            **EIGEN, "kategorie_id": str(ausgabe), "grund": "kurz"})
        assert antwort.status_code == 422
        assert f'<option value="{ausgabe}" selected>' in antwort.text

    @pytest.mark.parametrize("feld", ["belegdatum", "bezahlt_am"])
    @pytest.mark.parametrize("wert, meldung", [
        ("2099-01-01", "liegt in der Zukunft"),
        (MORGEN, "liegt in der Zukunft"),
        ("0026-02-10", "vor dem Jahr 2000"),
        ("2026-13-40", "kein Datum"),
    ])
    def test_datumsfenster(self, klient, datenbank, feld, wert, meldung):
        antwort = klient.post("/belege/eigenbeleg", data={**EIGEN, feld: wert})
        assert antwort.status_code == 422
        assert meldung in antwort.text
        assert _anzahl(datenbank) == 0

    def test_kompaktes_datum_wird_als_iso_gespeichert(self, klient, datenbank):
        """Angepasst (Schlussprobe): mit Kategorie. EIGEN hat keine, und ohne sie
        schrieb der Test die alte, falsche Meldung „als Betriebsausgabe 2025
        erfasst“ fest -- ein Eigenbeleg ohne Kategorie steht in keiner EÜR-Zeile."""
        antwort = klient.post("/belege/eigenbeleg", data={
            **EIGEN, "belegdatum": "20250210", "bezahlt_am": "20250212",
            "kategorie_id": str(_kategorie(datenbank))},
            follow_redirects=False)
        assert "als Betriebsausgabe 2025 erfasst" in _erfolg(antwort)
        with datenbank() as conn:
            assert conn.execute("SELECT belegdatum FROM beleg").fetchone()[0] == "2025-02-10"
            assert conn.execute("SELECT datum FROM zahlung").fetchone()[0] == "2025-02-12"

    def test_steuerzeichen_werden_abgewiesen(self, klient, datenbank):
        """Angepasst (Schlussprobe, Bereich quer): hier stand, dass der Eigenbeleg
        NUL und Escape still entfernt und speichert. Das war die Sonderbehandlung
        EINES Formulars, waehrend alle anderen das Zeichen speicherten. Seit
        main.py Steuerzeichen fuer jedes Formular zentral mit 422 abweist, kommt
        die Eingabe gar nicht erst an -- und es wird nichts gespeichert."""
        antwort = klient.post("/belege/eigenbeleg", data={
            **EIGEN, "bezeichnung": "\x00", "grund": "\x00Parkautomat ohne Beleg\x1b"},
            follow_redirects=False)
        assert antwort.status_code == 422
        assert "unsichtbares Steuerzeichen" in antwort.text
        assert _anzahl(datenbank) == 0

    def test_nur_steuerzeichen_ist_keine_begruendung(self, klient, datenbank):
        """Angepasst (Schlussprobe, Bereich quer): die Meldung kommt jetzt aus der
        zentralen Steuerzeichen-Pruefung in main.py statt aus der Route."""
        antwort = klient.post("/belege/eigenbeleg", data={**EIGEN, "grund": "\x00" * 20})
        assert antwort.status_code == 422
        assert "unsichtbares Steuerzeichen" in antwort.text
        assert _anzahl(datenbank) == 0


# ------------------------------------------ 4. Datumsfelder der Prüfmaske und Zahlung

class TestDatenPruefmaske:
    def test_kompaktes_belegdatum_wird_iso(self, klient, datenbank, tmp_path):
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            **BUCHEN, "belegdatum": "20260314", "bezahlt_am": "20260320"},
            follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        with datenbank() as conn:
            assert conn.execute("SELECT belegdatum FROM beleg").fetchone()[0] == "2026-03-14"
            assert conn.execute("SELECT datum FROM zahlung").fetchone()[0] == "2026-03-20"
            pfad = conn.execute("SELECT pfad FROM belegdatei").fetchone()[0]
        assert "2026-03-14_" in pfad and (tmp_path / pfad).is_file()

    @pytest.mark.parametrize("feld", ["belegdatum", "bezahlt_am"])
    def test_jahr_0026_wird_abgewiesen(self, klient, datenbank, feld):
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}", data={**BUCHEN, feld: "0026-02-10"})
        assert antwort.status_code == 422
        assert "vor dem Jahr 2000" in antwort.text
        assert _anzahl(datenbank) == 0

    @pytest.mark.parametrize("wert, erwartet", [
        ("20251231", "2025-12-31"), ("2025-W01-3", "2025-01-01"), ("2026-02-11", "2026-02-11"),
        ("05.03.2026", "2026-03-05"), ("5.3.2026", "2026-03-05"),
    ])
    def test_bezahlt_speichert_iso(self, klient, datenbank, wert, erwartet):
        klient.post("/belege/eigenbeleg", data={**EIGEN, "belegdatum": "2025-01-01",
                                                "bezahlt_am": ""})
        antwort = klient.post("/belege/1/bezahlt", data={"datum": wert},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/belege", _meldung(antwort)
        with datenbank() as conn:
            assert conn.execute("SELECT datum FROM zahlung").fetchone()[0] == erwartet

    @pytest.mark.parametrize("wert, meldung", [
        ("0026-02-10", "vor dem Jahr 2000"),
        (MORGEN, "liegt in der Zukunft"),
        ("31.02.2026", "ist kein Datum"),  # TT.MM.JJJJ wird gelesen, der 31.02. nicht
    ])
    def test_bezahlt_weist_ab(self, klient, datenbank, wert, meldung):
        klient.post("/belege/eigenbeleg", data={**EIGEN, "bezahlt_am": ""})
        antwort = klient.post("/belege/1/bezahlt", data={"datum": wert},
                              follow_redirects=False)
        assert meldung in _meldung(antwort)
        assert _anzahl(datenbank, "zahlung") == 0


# ------------------------------------------------------- Reisen: Datum, Zahlen

REISE = {"bezeichnung": "PyCon", "anlass": "Vortrag", "ziel": "Darmstadt",
         "abreise_am": "2026-04-20", "rueckkehr_am": "2026-04-23", "inland": "1"}


def _reise(klient, datenbank, **felder) -> int:
    klient.post("/reisen", data={**REISE, **felder}, follow_redirects=False)
    with datenbank() as conn:
        return conn.execute("SELECT MAX(id) FROM reise").fetchone()[0]


class TestReisen:
    def test_kompaktes_datum_wird_iso_und_als_datum_verglichen(self, klient, datenbank):
        """Als Text war "20260501" größer als "2026-05-03": die Prüfung
        „Rückkehr vor Abreise“ lief an der Datenbank vorbei in ein 500."""
        antwort = klient.post("/reisen", data={**REISE, "abreise_am": "20260501",
                                               "rueckkehr_am": "2026-05-03"},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/reisen", _meldung(antwort)
        with datenbank() as conn:
            zeile = conn.execute("SELECT abreise_am, rueckkehr_am FROM reise").fetchone()
        assert tuple(zeile) == ("2026-05-01", "2026-05-03")

        antwort = klient.post("/reisen", data={**REISE, "abreise_am": "2026-W01-1",
                                               "rueckkehr_am": "2025-12-28"},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "Die Rückkehr liegt vor der Abreise." in antwort.text
        # Die Eingaben bleiben im Formular stehen.
        assert 'value="2026-W01-1"' in antwort.text and 'value="PyCon"' in antwort.text

    @pytest.mark.parametrize("wert, meldung", [
        ("0026-02-10", "vor dem Jahr 2000"),
        (f"{HEUTE.year + 5}-01-01", "zu weit in der Zukunft"),
        ("gestern", "kein Datum"),
    ])
    def test_reisedatum_weist_ab(self, klient, datenbank, wert, meldung):
        antwort = klient.post("/reisen", data={**REISE, "abreise_am": wert,
                                               "rueckkehr_am": wert}, follow_redirects=False)
        assert antwort.status_code == 422
        assert meldung in antwort.text
        assert _anzahl(datenbank, "reise") == 0

    def test_geplante_reise_ist_erlaubt_ihre_pauschale_noch_nicht(self, klient, datenbank):
        naechstes = f"{HEUTE.year + 1}-01-1"
        rid = _reise(klient, datenbank, abreise_am=naechstes + "0", rueckkehr_am=naechstes + "2")
        assert rid is not None
        antwort = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=False)
        assert "Die Reise endet erst am 12.01." in _meldung(antwort)
        assert _anzahl(datenbank) == 0

    @pytest.mark.parametrize("km, meldung", [
        ("²", "sind keine Kilometer"),
        (GROSS, "sind keine Kilometer"),
        ("20000", "höchstens 10.000 km je Fahrt"),
        ("0", "Ohne Kilometer gibt es keine Fahrt."),
    ])
    def test_kilometer_obergrenze(self, klient, datenbank, km, meldung):
        rid = _reise(klient, datenbank)
        antwort = klient.post(f"/reisen/{rid}/kilometer", data={
            "datum": "2026-04-20", "km": km, "strecke": "HH-DA", "zweck": "Hin"},
            follow_redirects=False)
        assert meldung in _meldung(antwort)
        assert _anzahl(datenbank) == 0

    @pytest.mark.parametrize("datum, meldung", [
        (MORGEN, "liegt in der Zukunft"),
        ("0026-04-20", "vor dem Jahr 2000"),
    ])
    def test_kilometer_datum(self, klient, datenbank, datum, meldung):
        rid = _reise(klient, datenbank)
        antwort = klient.post(f"/reisen/{rid}/kilometer", data={
            "datum": datum, "km": "10", "strecke": "HH-DA", "zweck": "Hin"},
            follow_redirects=False)
        assert meldung in _meldung(antwort)
        assert _anzahl(datenbank) == 0

    def test_kilometer_kompaktes_datum_und_nul(self, klient, datenbank):
        """Angepasst (Schlussprobe, Bereich quer): eine Strecke mit NUL wurde hier
        still bereinigt und gespeichert. Jetzt weist main.py jedes Formular mit
        Steuerzeichen zentral ab (422); das kompakte Datum gilt weiter."""
        rid = _reise(klient, datenbank)
        antwort = klient.post(f"/reisen/{rid}/kilometer", data={
            "datum": "20260420", "km": "10", "strecke": "\x00HH-DA", "zweck": "Hin"},
            follow_redirects=False)
        assert antwort.status_code == 422
        assert "unsichtbares Steuerzeichen" in antwort.text
        assert _anzahl(datenbank) == 0
        antwort = klient.post(f"/reisen/{rid}/kilometer", data={
            "datum": "20260420", "km": "10", "strecke": "HH-DA", "zweck": "Hin"},
            follow_redirects=False)
        assert "erfasst" in _erfolg(antwort), _meldung(antwort)
        with datenbank() as conn:
            assert conn.execute("SELECT belegdatum FROM beleg").fetchone()[0] == "2026-04-20"

    def test_nur_nul_als_strecke(self, klient, datenbank):
        """Angepasst (Schlussprobe, Bereich quer): statt der Pflichtfeld-Meldung
        der Route kommt jetzt die zentrale Steuerzeichen-Meldung aus main.py."""
        rid = _reise(klient, datenbank)
        antwort = klient.post(f"/reisen/{rid}/kilometer", data={
            "datum": "2026-04-20", "km": "10", "strecke": "\x00", "zweck": "b"},
            follow_redirects=False)
        assert antwort.status_code == 422
        assert "unsichtbares Steuerzeichen" in antwort.text
        assert _anzahl(datenbank) == 0

    def test_stunden_unsinn(self, klient, datenbank):
        antwort = klient.post("/reisen", data={**REISE, "abwesend_stunden": "²",
                                               "inland": "0", "uebernachtung": ["0"]},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "sind keine Stunden" in antwort.text
        assert '<option value="0" selected>Ausland</option>' in antwort.text
        assert 'id="uebernachtung"\n           >' in antwort.text, "Kästchen abgewählt"
        assert _anzahl(datenbank, "reise") == 0

    def test_formulare_werden_nicht_gedruckt(self, klient, datenbank):
        _reise(klient, datenbank)
        text = klient.get("/reisen").text
        assert '<h2 class="nicht-drucken">Reise anlegen</h2>' in text
        assert '<div class="karten nicht-drucken">' in text


# ------------------------------------------------ 8. Formularzahlen der Prüfmaske

class TestZahlenPruefmaske:
    @pytest.mark.parametrize("wert", ["inf", "1e400", "nan", "150", "-7", "neunzehn"])
    def test_steuersatz(self, klient, datenbank, wert):
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}", data={**BUCHEN, "ust_satz": wert})
        assert antwort.status_code == 422
        assert "ist kein Steuersatz" in antwort.text
        assert _anzahl(datenbank) == 0

    def test_steuersatz_mit_prozentzeichen(self, klient, datenbank):
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}", data={**BUCHEN, "ust_satz": "19 %"},
                              follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        with datenbank() as conn:
            assert conn.execute("SELECT ust_satz_promille FROM beleg").fetchone()[0] == 190

    @pytest.mark.parametrize("feld, wert, meldung", [
        ("brutto_euro", GROSS, "unplausibel groß"),
        ("quote", GROSS, "ist keine Quote"),
        ("lieferant_id", GROSS, "Diesen Lieferanten gibt es nicht."),
        ("kategorie_id", GROSS, "Diese Kategorie gibt es nicht."),
        ("reise_id", GROSS, "Diese Reise gibt es nicht."),
        ("lieferant_id", "²", "Diesen Lieferanten gibt es nicht."),
    ])
    def test_riesenzahlen(self, klient, datenbank, feld, wert, meldung):
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}", data={**BUCHEN, feld: wert})
        assert antwort.status_code == 422
        assert meldung in antwort.text
        assert _anzahl(datenbank) == 0


# ------------------------------------------------- 5. Hinweis ohne Zahlungsdatum

class TestHinweisOhneZahlung:
    def test_buchung_ohne_zahlungsdatum_sagt_es(self, klient):
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}", data=BUCHEN, follow_redirects=False)
        assert ("Ohne Zahlungsdatum zählt er noch in keinem EÜR-Jahr — in der Liste unter "
                "„Ändern“ die Zahlung erfassen.") in _erfolg(antwort)

    def test_mit_zahlungsdatum_kein_hinweis(self, klient):
        name = _hochladen(klient)
        antwort = klient.post(f"/belege/pruefen/{name}",
                              data={**BUCHEN, "bezahlt_am": "2026-02-02"},
                              follow_redirects=False)
        assert _erfolg(antwort).startswith("Beleg 1 erfasst.")
        assert "Ohne Zahlungsdatum" not in _erfolg(antwort)


# ------------------------------------------------- Kennungen ab 2**63 in der Adresse

class TestPfadKennungen:
    @pytest.mark.parametrize("methode, adresse, daten, meldung", [
        ("get", f"/belege/{GROSS}/datei", None, "Diesen Beleg gibt es nicht."),
        ("post", f"/belege/{GROSS}/kategorie", {"kategorie_id": ""},
         "Diesen Beleg gibt es nicht."),
        ("post", f"/belege/{GROSS}/bezahlt", {"datum": "2026-03-10"},
         "Diesen Beleg gibt es nicht."),
        ("post", f"/belege/{2**70}/bezahlt", {"datum": "2026-03-10"},
         "Diesen Beleg gibt es nicht."),
        ("post", "/belege/abc/bezahlt", {"datum": "2026-03-10"},
         "Diesen Beleg gibt es nicht."),
        ("post", f"/reisen/{GROSS}/verpflegung", None, "Diese Reise gibt es nicht."),
        ("post", f"/reisen/{GROSS}/kilometer",
         {"datum": "2026-04-20", "km": "10", "strecke": "a", "zweck": "b"},
         "Diese Reise gibt es nicht."),
    ])
    def test_meldung_statt_absturz(self, klient, methode, adresse, daten, meldung):
        """Ab 19 Stellen antwortet schon die zentrale Pruefung in app/main.py
        mit der deutschen 404-Seite; sonst meldet die Route selbst."""
        if methode == "get":
            antwort = klient.get(adresse, follow_redirects=False)
        else:
            antwort = klient.post(adresse, data=daten, follow_redirects=False)
        if any(t.isascii() and t.isdigit() and len(t) > 18 for t in adresse.split("/")):
            assert antwort.status_code == 404
            assert "Diese Seite gibt es nicht" in antwort.text
            return
        assert antwort.status_code == 303, antwort.text[:300]
        assert _meldung(antwort) == meldung


class TestDoppelklickWegwerfen:
    def test_kein_absturz(self, klient, tmp_path):
        for runde in range(5):
            name = _hochladen(klient, PDF_DE.read_bytes() + f"%{runde}".encode())
            antworten = _parallel(2, lambda i, name=name: klient.post(
                f"/belege/eingang/{name}/wegwerfen", follow_redirects=False))
            _ohne_absturz(antworten)
            assert not (tmp_path / p.EINGANG / name).exists()

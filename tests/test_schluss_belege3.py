"""Regressionstests zur Schlussprobe im Bereich Belege und Reisen.

Grundlage sind die Befunde der Schlussprobe: der Doppelklick auf
„Verpflegungspauschale buchen“, die falsche Meldung „ist kein XML“ beim
gleichzeitigen Buchen einer Eingangsdatei, die Erfolgsmeldung eines
Eigenbelegs ohne Kategorie, die Betragsmeldung bei null oder negativ und der
fehlende Satzpunkt in der Meldung der Integritätsprüfung.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.logik import euer
from app.logik.beleg import pipeline as p
from app.main import erzeuge_app
from app.routen import belege as belege_routen

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}

#: Kennzahl der Verpflegungspauschale in der Anlage EÜR.
KZ_VERPFLEGUNG = 171


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


def _abfrage(antwort) -> dict[str, list[str]]:
    return parse_qs(urlparse(antwort.headers.get("location", "")).query)


def _fehler(antwort) -> str:
    return _abfrage(antwort).get("fehler", [""])[0]


def _erfolg(antwort) -> str:
    return _abfrage(antwort).get("erfolg", [""])[0]


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
        assert antwort.status_code == 303, antwort.text[:400]


# ------------------------------------- 1. Doppelklick „Verpflegungspauschale buchen“

class TestDoppelklickVerpflegung:
    """Zwei oder drei gleichzeitige POST /reisen/{id}/verpflegung. Vorher
    prüfte verpflegung_buchen „schon vorhanden“ außerhalb von BEGIN IMMEDIATE:
    beide (alle drei) Anfragen sahen „noch keine“, jede buchte eine Pauschale,
    und die EÜR-Zeile Verpflegung stieg um das Doppelte bzw. Dreifache."""

    def _reise(self, klient, datenbank, runde: int) -> int:
        antwort = klient.post("/reisen", data={
            "bezeichnung": f"Kundentermin {runde}", "anlass": "Workshop beim Kunden",
            "ziel": "Lübeck", "abreise_am": "2026-05-04", "rueckkehr_am": "2026-05-05",
            "inland": "1", "uebernachtung": "1",
        }, follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        with datenbank() as conn:
            return conn.execute("SELECT MAX(id) FROM reise").fetchone()[0]

    @pytest.mark.parametrize("threads, runden", [(2, 6), (3, 4)])
    def test_genau_eine_pauschale_je_reise(self, klient, datenbank, threads, runden):
        for runde in range(runden):
            rid = self._reise(klient, datenbank, runde)
            adresse = f"/reisen/{rid}/verpflegung"
            antworten = _parallel(threads, lambda i, adresse=adresse: klient.post(
                adresse, follow_redirects=False))
            _ohne_absturz(antworten)

            with datenbank() as conn:
                belege = conn.execute(
                    "SELECT b.id, b.betrag_brutto_cent FROM beleg b"
                    " JOIN euer_kategorie k ON k.id = b.kategorie_id"
                    " WHERE b.reise_id = ? AND k.schluessel = 'REISE_VERPFLEGUNG'",
                    (rid,)).fetchall()
                assert [z["betrag_brutto_cent"] for z in belege] == [2800], (runde, belege)
                gewinner = belege[0]["id"]
                assert conn.execute("SELECT COUNT(*) FROM zahlung z JOIN beleg b"
                                    " ON b.id = z.beleg_id WHERE b.reise_id = ?",
                                    (rid,)).fetchone()[0] == 1
                ausgaben = euer.auswertung(conn, 2026).ausgaben
            # Die EÜR-Zeile wächst je Reise um genau eine Pauschale (2 × 14 €).
            zeile = [z.betrag_cent for z in ausgaben if z.kennzahl == KZ_VERPFLEGUNG]
            assert zeile == [2800 * (runde + 1)], (runde, zeile)

            erfolge = [_erfolg(a) for a in antworten if _erfolg(a)]
            fehler = [_fehler(a) for a in antworten if _fehler(a)]
            assert len(erfolge) == 1, erfolge
            assert erfolge[0].startswith(
                f"Verpflegungspauschale 28,00 € als Eigenbeleg {gewinner} ")
            # Die verlierenden Anfragen hören dasselbe wie ein zweiter Klick danach.
            assert fehler == [f"Für diese Reise ist die Verpflegungspauschale schon als "
                              f"Beleg {gewinner} erfasst."] * (threads - 1)

    def test_nacheinander_bleibt_die_meldung(self, klient, datenbank):
        rid = self._reise(klient, datenbank, 0)
        erste = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=False)
        zweite = klient.post(f"/reisen/{rid}/verpflegung", follow_redirects=False)
        assert "als Eigenbeleg 1 " in _erfolg(erste)
        assert _fehler(zweite) == ("Für diese Reise ist die Verpflegungspauschale schon als "
                                   "Beleg 1 erfasst.")


# --------------------------- 2. Buchen im Rennen: „ist kein PDF“ statt „bereits gebucht“

def _hochladen(klient, inhalt: bytes | None = None, name: str = "rechnung.pdf") -> str:
    antwort = klient.post("/belege/hochladen", files={
        "datei": (name, inhalt if inhalt is not None else PDF_DE.read_bytes(),
                  "application/pdf")}, follow_redirects=False)
    return antwort.headers["location"].rsplit("/", 1)[1]


def _gewinner_bucht(ordner: Path, pfad: Path) -> int:
    """Die andere Anfrage: bucht dieselbe Eingangsdatei über eine eigene
    Verbindung und verschiebt sie dabei ins Archiv."""
    eingang = p.Eingang(pfad=pfad, original_name=pfad.name, sha256=p.pruefsumme(pfad),
                        groesse=pfad.stat().st_size)
    with _db(ordner) as conn:
        conn.isolation_level = None
        lesung = p.Lesung(eingang=eingang, vorschlag=p.Vorschlag("manuell", {}, ()))
        return p.buche(conn, lesung, {"belegdatum": "2026-02-01", "brutto": 11900},
                       ordner).beleg_id


class TestRennenInhaltspruefung:
    """Die Gegenanfrage verschiebt die Eingangsdatei zwischen Lesen und
    Inhaltsprüfung ins Archiv. Vorher machte pipeline.inhalt_passt() aus dem
    OSError ein False, und die verlierende Anfrage meldete „Der Inhalt von … ist
    kein PDF — die Endung passt nicht zum Inhalt … hier wegwerfen“ für eine
    Datei, die soeben gebucht worden war."""

    def test_buchen_meldet_bereits_gebucht(self, klient, datenbank, tmp_path, monkeypatch):
        name = _hochladen(klient)
        echt = belege_routen._eingang

        def eingang_und_gegenanfrage(pfad):
            ergebnis = echt(pfad)
            _gewinner_bucht(tmp_path, pfad)
            return ergebnis

        monkeypatch.setattr(belege_routen, "_eingang", eingang_und_gegenanfrage)
        antwort = klient.post(f"/belege/pruefen/{name}", data={
            "belegdatum": "2026-02-01", "brutto_euro": "119,00", "quote": "100",
            "trotzdem": "1"}, follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        assert _erfolg(antwort) == "Diese Datei wurde bereits als Beleg 1 gebucht."
        assert "Endung passt nicht" not in antwort.headers["location"]
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1

    def test_pruefmaske_meldet_bereits_gebucht(self, klient, tmp_path, monkeypatch):
        name = _hochladen(klient)
        echt = belege_routen._eingangsdatei

        def aufloesen_und_gegenanfrage(datenordner, dateiname):
            pfad = echt(datenordner, dateiname)
            if pfad is not None:
                _gewinner_bucht(tmp_path, pfad)
            return pfad

        monkeypatch.setattr(belege_routen, "_eingangsdatei", aufloesen_und_gegenanfrage)
        antwort = klient.get(f"/belege/pruefen/{name}", follow_redirects=False)
        assert antwort.status_code == 303
        assert _erfolg(antwort) == "Diese Datei wurde bereits als Beleg 1 gebucht."

    def test_liste_laesst_die_verschwundene_datei_weg(self, klient, tmp_path, monkeypatch):
        """Vorher stand die gerade gebuchte Datei als „Inhalt passt nicht zur
        Endung“ in der Eingangsliste."""
        _hochladen(klient)
        echt = p.inhalt_passt

        def verschwindet(pfad):
            pfad.unlink()
            return echt(pfad)

        monkeypatch.setattr(p, "inhalt_passt", verschwindet)
        seite = klient.get("/belege").text
        assert "Inhalt passt nicht zur Endung" not in seite

    def test_inhalt_passt_unterscheidet_weg_von_fremd(self, tmp_path):
        gut = tmp_path / "gut.pdf"
        gut.write_bytes(PDF_DE.read_bytes())
        fremd = tmp_path / "fremd.pdf"
        fremd.write_bytes(b"MZ\x90\x00" + b"\x00" * 60)
        assert p.inhalt_passt(gut) is True
        assert p.inhalt_passt(fremd) is False
        assert p.inhalt_passt(tmp_path / "weg.pdf") is None

    def test_pipeline_meldet_nicht_mehr_im_eingang(self, klient, datenbank, tmp_path,
                                                   monkeypatch):
        """Unter der Sperre: die Datei liegt noch da, ist aber beim Lesen weg
        (gleichzeitig weggeworfen). Vorher „ist kein PDF“."""
        name = _hochladen(klient)
        pfad = tmp_path / p.EINGANG / name
        eingang = p.Eingang(pfad=pfad, original_name="rechnung.pdf",
                            sha256=p.pruefsumme(pfad), groesse=pfad.stat().st_size)
        lesung = p.Lesung(eingang=eingang, vorschlag=p.Vorschlag("manuell", {}, ()))
        monkeypatch.setattr(p, "inhalt_passt", lambda _pfad: None)
        with datenbank() as conn:
            conn.isolation_level = None
            with pytest.raises(p.BelegFehler, match="liegt nicht mehr im Eingang"):
                p.buche(conn, lesung, {"belegdatum": "2026-02-01", "brutto": 500}, tmp_path)
        assert pfad.exists()


# --------------------------------------- 3. Erfolgsmeldung eines Eigenbelegs

EIGEN = {"belegdatum": "2026-02-01", "brutto_euro": "50,00", "quote": "100",
         "bezeichnung": "Parkgebühr",
         "grund": "Parkautomat ohne Belegausgabe, Termin bei Mustermann GmbH",
         "bezahlt_am": "2026-02-05"}

OHNE_KATEGORIE = ("Ohne Kategorie geht er in keine Zeile der EÜR — bitte unter „Ändern“ "
                  "eine Kategorie wählen.")
OHNE_ZAHLUNG = ("Ohne Zahlungsdatum zählt er noch in keinem EÜR-Jahr (§ 11 Abs. 2 EStG) — "
                "sobald bezahlt, in der Belegliste unter „Ändern“ die Zahlung erfassen.")


def _ausgabenkategorie(datenbank) -> int:
    with datenbank() as conn:
        return conn.execute("SELECT id FROM euer_kategorie WHERE schluessel = 'BUEROBEDARF'"
                            ).fetchone()[0]


class TestEigenbelegMeldung:
    """Vorher meldete POST /belege/eigenbeleg mit Zahlungsdatum immer „Eigenbeleg N
    als Betriebsausgabe 2026 erfasst.“ -- auch ohne Kategorie, obwohl der Beleg
    dann in keiner EÜR-Zeile steht (die Prüfmaske sagte im selben Fall richtig
    „Ohne Kategorie geht er in keine Zeile der EÜR.“)."""

    def test_ohne_kategorie(self, klient, datenbank):
        antwort = klient.post("/belege/eigenbeleg", data={**EIGEN, "kategorie_id": ""},
                              follow_redirects=False)
        assert _erfolg(antwort) == f"Eigenbeleg 1 erfasst. {OHNE_KATEGORIE}"
        with datenbank() as conn:
            assert euer.auswertung(conn, 2026).ausgaben_cent == 0

    def test_ohne_kategorie_und_ohne_zahlung(self, klient):
        antwort = klient.post("/belege/eigenbeleg",
                              data={**EIGEN, "kategorie_id": "", "bezahlt_am": ""},
                              follow_redirects=False)
        assert _erfolg(antwort) == f"Eigenbeleg 1 erfasst. {OHNE_KATEGORIE} {OHNE_ZAHLUNG}"

    def test_ohne_zahlung(self, klient, datenbank):
        """Vorher: „erfasst — noch ohne Zahlungsdatum, damit noch keine
        Betriebsausgabe“; jetzt im selben Satzbau wie die übrigen Hinweise."""
        antwort = klient.post("/belege/eigenbeleg", data={
            **EIGEN, "kategorie_id": str(_ausgabenkategorie(datenbank)), "bezahlt_am": ""},
            follow_redirects=False)
        assert _erfolg(antwort) == f"Eigenbeleg 1 erfasst. {OHNE_ZAHLUNG}"

    def test_mit_kategorie_und_zahlung(self, klient, datenbank):
        antwort = klient.post("/belege/eigenbeleg", data={
            **EIGEN, "kategorie_id": str(_ausgabenkategorie(datenbank))},
            follow_redirects=False)
        assert _erfolg(antwort) == "Eigenbeleg 1 als Betriebsausgabe 2026 erfasst."
        with datenbank() as conn:
            assert euer.auswertung(conn, 2026).ausgaben_cent == 5000


# ------------------------------------------- 4. Betrag null oder negativ

class TestEigenbelegBetrag:
    """Vorher lautete die Meldung für 0, leer und -1 „Ein Eigenbeleg über null
    Euro ist kein Beleg.“ -- für einen negativen Betrag sprachlich schief."""

    @pytest.mark.parametrize("wert", ["0", "0,00", "-1", "-24,90", ""])
    def test_meldung(self, klient, datenbank, wert):
        antwort = klient.post("/belege/eigenbeleg", data={**EIGEN, "brutto_euro": wert})
        assert antwort.status_code == 422
        assert "Ein Eigenbeleg braucht einen Betrag über 0 €." in antwort.text
        assert "über null Euro" not in antwort.text
        # Die Eingaben bleiben im Formular stehen.
        assert "Parkautomat ohne Belegausgabe, Termin bei Mustermann GmbH" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 0


# ------------------------------------- 5. Integritätsprüfung: Satzpunkt

class TestIntegritaetsmeldung:
    """Echter Befund plus zurückgesetzte Dateirechte. Vorher hingen beide Sätze
    ohne Punkt aneinander: „… die Datei wurde verändert Die Dateirechte von 1
    Belegdatei waren …“."""

    def _drei_belege(self, klient, datenbank, tmp_path) -> list[Path]:
        for i in range(3):
            name = _hochladen(klient, PDF_DE.read_bytes() + f"\n%{i}".encode(), f"r{i}.pdf")
            antwort = klient.post(f"/belege/pruefen/{name}", data={
                "belegdatum": "2026-05-02", "brutto_euro": f"{50 + i},00", "quote": "100",
                "trotzdem": "1"}, follow_redirects=False)
            assert _erfolg(antwort).startswith(f"Beleg {i + 1} erfasst."), _fehler(antwort)
        with datenbank() as conn:
            return [tmp_path / z[0] for z in conn.execute(
                "SELECT pfad FROM belegdatei ORDER BY beleg_id")]

    def test_befund_und_rechte(self, klient, datenbank, tmp_path):
        pfade = self._drei_belege(klient, datenbank, tmp_path)
        os.chmod(pfade[0], 0o644)
        pfade[0].write_bytes(pfade[0].read_bytes() + b"\n%manipuliert")
        os.chmod(pfade[0], 0o444)
        os.chmod(pfade[1], 0o644)
        antwort = klient.post("/belege/integritaet", follow_redirects=False)
        assert _fehler(antwort) == (
            "1 Befund — Beleg 1: Prüfsumme stimmt nicht — die Datei wurde verändert. "
            "Die Dateirechte von 1 Belegdatei waren nach einer Wiederherstellung oder Kopie "
            "wieder beschreibbar und sind jetzt wieder schreibgeschützt.")

    def test_befund_allein_endet_mit_punkt(self, klient, datenbank, tmp_path):
        pfade = self._drei_belege(klient, datenbank, tmp_path)
        os.chmod(pfade[2], 0o644)
        pfade[2].unlink()
        antwort = klient.post("/belege/integritaet", follow_redirects=False)
        assert _fehler(antwort) == "1 Befund — Beleg 3: Datei fehlt."

    def test_kein_doppelter_punkt(self, klient, monkeypatch):
        """Ein Befund mit Fehlertext endet schon mit Punkt -- dann kein zweiter.
        Das war vorher schon richtig; der Test sichert, dass der neue Satzpunkt
        keinen doppelten erzeugt."""

        def pruefung(_conn, _ordner, *, rechte_gesetzt):
            rechte_gesetzt.append("belege/2026/b.pdf")
            return [{"beleg_id": 1, "pfad": "belege/2026/a.pdf",
                     "befund": "Dateirechte abweichend (0644), ließen sich nicht auf 0444 "
                               "zurücksetzen: Keine Berechtigung."}]

        monkeypatch.setattr(p, "pruefe_integritaet", pruefung)
        meldung = _fehler(klient.post("/belege/integritaet", follow_redirects=False))
        assert "Keine Berechtigung. Die Dateirechte von 1 Belegdatei" in meldung
        assert ".." not in meldung

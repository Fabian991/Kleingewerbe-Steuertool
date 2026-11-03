"""Gegenprobe Stammdaten: was nach der ersten Korrekturrunde offen blieb.

Kundenstamm (USt-IdNr. je Land, Schweizer Steuernummer, doppelte Namen in
den Auswahllisten), Einrichtung (Jahreswechsel der Zaehlweise, Riesenbetraege,
Datumsfenster) und Zeiterfassung (Dauer korrigieren, Kennungen, Rueckweg,
Statuspruefung in der Transaktion). Jeder Test geht ueber HTTP gegen eine
frische Datenbank, so wie der Nutzer es sieht.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import kunden as kundenlogik
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "21",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}
PFLICHT = {
    "name": "Mein Gewerbe", "strasse": "Weg 1", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def datenbank(tmp_path) -> Path:
    return tmp_path / "web.sqlite"


@pytest.fixture
def roh(datenbank):
    """Ein Klient ohne Einrichtung."""
    with TestClient(erzeuge_app(datenbank)) as c:
        yield c


@pytest.fixture
def klient(roh):
    antwort = roh.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
    assert antwort.status_code == 303
    return roh


@pytest.fixture
def kunde(klient):
    antwort = klient.post("/kunden", data=KUNDE, follow_redirects=False)
    assert antwort.status_code == 303
    return int(antwort.headers["location"].split("?")[0].rsplit("/", 1)[1])


def _lies(datenbank: Path, sql: str, *werte):
    conn = sqlite3.connect(datenbank)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, werte).fetchall()
    finally:
        conn.close()


def _schreibe(datenbank: Path, sql: str, *werte):
    conn = sqlite3.connect(datenbank)
    try:
        conn.execute(sql, werte)
        conn.commit()
    finally:
        conn.close()


def _feldwert(html: str, name: str) -> str:
    """Der value des Eingabefelds ``name`` im gerenderten Formular."""
    rest = html.split(f'name="{name}"', 1)[1].split(">", 1)[0]
    return rest.split('value="', 1)[1].split('"', 1)[0] if 'value="' in rest else ""


def _meldung(antwort, schluessel: str = "fehler") -> str:
    ort = antwort.headers.get("location", "")
    treffer = re.search(rf"[?&]{schluessel}=([^&]*)", ort)
    return unquote(treffer.group(1)) if treffer else ""


# ============================================================ USt-IdNr.


class TestUstIdNrJeLand:
    """Entscheidung 1: bei Reverse Charge die Form des Landes, sonst frei."""

    @pytest.mark.parametrize(("falsch", "beispiel"), [
        ("ATU1234567", "ATU12345678"),
        ("DE12345678", "DE123456789"),
        ("FR1234", "FR12345678901"),
        ("NL123456789", "NL123456789B01"),
    ])
    def test_falsche_laenge_bei_reverse_charge(self, klient, falsch, beispiel):
        antwort = klient.post("/kunden", data={
            **KUNDE, "land": falsch[:2], "leistungsort": "EU_B2B_REVERSE_CHARGE",
            "ust_idnr": falsch,
        })
        assert antwort.status_code == 422
        assert f"z. B. {beispiel}" in antwort.text
        assert _feldwert(antwort.text, "ust_idnr") == falsch, "Eingabe bleibt erhalten"
        assert "Noch keine Kunden" in klient.get("/kunden").text

    def test_richtige_nummer_wird_kompakt_gespeichert(self, klient, datenbank):
        antwort = klient.post("/kunden", data={
            **KUNDE, "land": "AT", "leistungsort": "EU_B2B_REVERSE_CHARGE",
            "ust_idnr": "atu 1234 5678",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT ust_idnr FROM kunde")[0][0] == "ATU12345678"

    def test_schweizer_mwst_nummer_bleibt_beim_drittland(self, klient, datenbank):
        """Regression: "CHE-123.456.789 MWST" wurde mit 422 abgewiesen."""
        antwort = klient.post("/kunden", data={
            **KUNDE, "name": "Zürich AG", "strasse": "Bahnhofstr. 1", "plz": "8001",
            "ort": "Zürich", "land": "CH", "leistungsort": "DRITTLAND",
            "ust_idnr": "CHE-123.456.789 MWST",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT ust_idnr FROM kunde")[0][0] == "CHE-123.456.789 MWST"

    def test_inland_erlaubt_jede_steuernummer(self, klient, datenbank):
        antwort = klient.post("/kunden", data={**KUNDE, "ust_idnr": "12/345/67890"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT ust_idnr FROM kunde")[0][0] == "12/345/67890"

    def test_steuerzeichen_werden_entfernt_und_laenge_begrenzt(self, klient, datenbank):
        """Angepasst (Schlussprobe, Bereich quer): "ab\\x00c\\nd" wurde hier still
        zu "ABC D" bereinigt. Ein NUL weist main.py jetzt fuer jedes Formular
        zentral ab (422, nichts gespeichert); der Zeilenumbruch ist erlaubt und
        wird weiter zu einem Leerzeichen. Das Entfernen selbst pruefen die
        Doctests von normalisiere_ust_idnr."""
        antwort = klient.post("/kunden", data={
            **KUNDE, "leistungsort": "DRITTLAND", "ust_idnr": "ab\x00c\nd",
        }, follow_redirects=False)
        assert antwort.status_code == 422
        assert "unsichtbares Steuerzeichen" in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM kunde")[0][0] == 0
        antwort = klient.post("/kunden", data={
            **KUNDE, "leistungsort": "DRITTLAND", "ust_idnr": "abc\nd",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT ust_idnr FROM kunde")[0][0] == "ABC D"
        antwort = klient.post("/kunden", data={
            **KUNDE, "leistungsort": "DRITTLAND", "ust_idnr": "X" * 41,
        })
        assert antwort.status_code == 422
        assert "höchstens 40" in antwort.text

    def test_doctests_decken_die_geforderten_laender_ab(self):
        rc = "EU_B2B_REVERSE_CHARGE"
        for land in ("AT", "DE", "FR", "NL", "IT", "ES", "BE", "IE"):
            beispiel = kundenlogik.UST_ID_FORMEN[land][2]
            assert kundenlogik.normalisiere_ust_idnr(beispiel, rc) == beispiel
        # Alle 27 Mitgliedstaaten, Griechenland als EL, dazu Nordirland (XI).
        erwartet = (kundenlogik.EU_LAENDER - {"GR"}) | {"EL", "XI"}
        assert set(kundenlogik.UST_ID_FORMEN) == erwartet
        assert len(erwartet) == 28


# ========================================================== Einrichtung


def _rechnung(datenbank: Path, kunde_id: int, seq: int, jahr: int, nummer: str):
    _schreibe(
        datenbank,
        """
        INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,
                              netto_cent, brutto_cent, leistungsort, status,
                              festgeschrieben_am, erstellt_am)
        VALUES ('RECHNUNG', ?, ?, ?, ?, ?, 50000, 50000, 'INLAND',
                'FESTGESCHRIEBEN', ?, ?)
        """,
        nummer, seq, jahr, kunde_id, f"{jahr}-06-01", db.jetzt_utc(), db.jetzt_utc(),
    )


def _kreise(datenbank: Path) -> list[tuple[int, int]]:
    return [
        (z["jahr"], z["naechste_seq"])
        for z in _lies(datenbank, "SELECT jahr, naechste_seq FROM nummernkreis"
                                  " WHERE typ = 'RECHNUNG' ORDER BY jahr")
    ]


class TestWechselDerZaehlweise:
    """Entscheidung 3a: durchlaufend -> jaehrlich im neuen Jahr beginnt bei 1."""

    @pytest.fixture
    def durchlaufend(self, roh, datenbank, monkeypatch):
        roh.post("/einrichtung", data={**PFLICHT, "nummer_format": "{N}",
                                       "reset_jaehrlich": "0", "start_seq": "1"})
        kid = int(roh.post("/kunden", data=KUNDE, follow_redirects=False)
                  .headers["location"].split("?")[0].rsplit("/", 1)[1])
        jahr = int(db.heute()[:4])
        for seq in (1, 2, 3):
            _rechnung(datenbank, kid, seq, jahr, str(seq))
        _schreibe(datenbank, "UPDATE nummernkreis SET naechste_seq = 4")
        monkeypatch.setattr(db, "heute", lambda: f"{jahr + 1}-01-10")
        return jahr

    def _speichern(self, klient, **felder):
        return klient.post("/einrichtung", data={
            **PFLICHT, "nummer_format": "RE-{JJJJ}-{NNNN}", "reset_jaehrlich": "1",
            **felder,
        }, follow_redirects=False)

    def test_unveraendertes_feld_beginnt_bei_eins(self, roh, datenbank, durchlaufend):
        formular = roh.get("/einrichtung").text
        gezeigt = _feldwert(formular, "start_seq")
        assert gezeigt == "4"
        assert _feldwert(formular, "start_seq_angezeigt") == "4"
        antwort = self._speichern(roh, start_seq=gezeigt, start_seq_angezeigt=gezeigt)
        assert antwort.status_code == 303
        assert _kreise(datenbank) == [(durchlaufend + 1, 1)]
        assert f"RE-{durchlaufend + 1}-0001" in roh.get("/").text

    def test_leeres_feld_beginnt_bei_eins(self, roh, datenbank, durchlaufend):
        self._speichern(roh, start_seq="", start_seq_angezeigt="4")
        assert _kreise(datenbank) == [(durchlaufend + 1, 1)]

    def test_geaenderter_wert_gilt(self, roh, datenbank, durchlaufend):
        self._speichern(roh, start_seq="7", start_seq_angezeigt="4")
        assert _kreise(datenbank) == [(durchlaufend + 1, 7)]

    def test_umgekehrt_bleibt_der_stand(self, klient, datenbank, kunde):
        jahr = int(db.heute()[:4])
        for seq in (1, 2, 3, 4, 5):
            _rechnung(datenbank, kunde, seq, jahr, f"RE-{jahr}-{seq:04d}")
        _schreibe(datenbank, "UPDATE nummernkreis SET naechste_seq = 6")
        formular = klient.get("/einrichtung").text
        gezeigt = _feldwert(formular, "start_seq")
        assert gezeigt == "6"
        antwort = klient.post("/einrichtung", data={
            **PFLICHT, "nummer_format": "R{NNNN}", "reset_jaehrlich": "0",
            "start_seq": gezeigt, "start_seq_angezeigt": gezeigt,
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert _kreise(datenbank) == [(0, 6)]

    def test_fehlerseite_traegt_den_angezeigten_wert_weiter(self, roh, durchlaufend):
        antwort = self._speichern(roh, start_seq="4", start_seq_angezeigt="4", plz="1")
        assert antwort.status_code == 422
        assert _feldwert(antwort.text, "start_seq_angezeigt") == "4"


class TestRiesigerStundensatz:
    """Entscheidung 3b: nie 500, immer 422 mit Meldung."""

    @pytest.mark.parametrize("satz", ["99999999999999999999", "9223372036854775808",
                                      "92233720368547758,07"])
    def test_einrichtung(self, roh, datenbank, satz):
        antwort = roh.post("/einrichtung", data={**PFLICHT, "standardsatz_euro": satz})
        assert antwort.status_code == 422
        assert "unplausibel groß" in antwort.text
        assert _feldwert(antwort.text, "standardsatz_euro") == satz
        assert _lies(datenbank, "SELECT COUNT(*) FROM betrieb")[0][0] == 0

    @pytest.mark.parametrize("satz", ["99999999999999999999", "9223372036854775808"])
    def test_kundenformular(self, klient, kunde, satz):
        for ziel in ("/kunden", f"/kunden/{kunde}"):
            antwort = klient.post(ziel, data={**KUNDE, "satz_euro": satz})
            assert antwort.status_code == 422, ziel
            assert "unplausibel groß" in antwort.text
            assert _feldwert(antwort.text, "satz_euro") == satz


class TestBeginnDerTaetigkeit:
    """Entscheidung 3c: Jahr ab 2000, nicht in der Zukunft."""

    @pytest.mark.parametrize("eingabe", ["0026-02-10", "1999-12-31"])
    def test_jahr_vor_2000(self, roh, eingabe):
        antwort = roh.post("/einrichtung", data={**PFLICHT, "beginn_am": eingabe})
        assert antwort.status_code == 422
        assert "vor dem Jahr 2000" in antwort.text

    def test_zukunft(self, roh):
        morgen = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
        antwort = roh.post("/einrichtung", data={**PFLICHT, "beginn_am": morgen})
        assert antwort.status_code == 422
        assert "Zukunft" in antwort.text

    def test_kompakt_wird_iso(self, roh, datenbank):
        antwort = roh.post("/einrichtung", data={**PFLICHT, "beginn_am": "20200101"},
                           follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT beginn_am FROM betrieb")[0][0] == "2020-01-01"


# ============================================== Doppelte Kundennamen


class TestDoppelteNamenInAuswahllisten:
    """Entscheidung 2: bei gleichem Namen steht "(K-0001, Ort)" dahinter."""

    @pytest.fixture
    def doppel(self, klient):
        ids = []
        for name, ort in (("Doppel GmbH", "Berlin"), ("doppel gmbh", "Hamburg"),
                          ("Einzeln AG", "Köln")):
            antwort = klient.post("/kunden", data={**KUNDE, "name": name, "ort": ort},
                                  follow_redirects=False)
            assert antwort.status_code == 303
            ids.append(int(antwort.headers["location"].split("?")[0].rsplit("/", 1)[1]))
        # Eine Zeit je Kunde, damit alle im Zeitenfilter stehen.
        for kid in ids:
            klient.post("/zeiten", data={"kunde_id": str(kid), "titel": "Beratung",
                                         "datum": "2026-03-02", "dauer": "60"})
        return ids

    def test_zeiten_erfassen_timer_und_filter(self, klient, doppel):
        text = klient.get("/zeiten").text
        assert text.count("Doppel GmbH (K-0001, Berlin)") == 3, "Timer, Erfassen, Filter"
        assert text.count("doppel gmbh (K-0002, Hamburg)") == 3
        assert "Einzeln AG (" not in text

    def test_rechnungsfilter_und_assistent(self, klient, doppel):
        assert "Doppel GmbH (K-0001, Berlin)" in klient.get("/rechnungen").text
        assistent = klient.get("/rechnung/neu").text
        assert re.search(r"Doppel GmbH <span[^>]*>\(K-0001, Berlin\)</span>", assistent)
        assert re.search(r"doppel gmbh <span[^>]*>\(K-0002, Hamburg\)</span>", assistent)
        assert "Einzeln AG <span" not in assistent

    def test_umlaute_ohne_gross_klein(self, klient):
        for name, ort in (("MÜLLER KG", "Bonn"), ("Müller KG", "Ulm")):
            klient.post("/kunden", data={**KUNDE, "name": name, "ort": ort})
        text = klient.get("/zeiten").text
        assert "MÜLLER KG (K-0001, Bonn)" in text
        assert "Müller KG (K-0002, Ulm)" in text


# ===================================================== Zeiterfassung

ZEIT = {"titel": "Beratung", "datum": "2026-03-02", "dauer": "60"}


def _zeit(klient, kunde: int, datenbank: Path, **felder) -> int:
    antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, **felder},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:300]
    assert "fehler=" not in antwort.headers["location"]
    return _lies(datenbank, "SELECT MAX(id) FROM zeiteintrag")[0][0]


def _eintrag(datenbank: Path, zid: int) -> sqlite3.Row:
    return _lies(datenbank, "SELECT * FROM zeiteintrag WHERE id = ?", zid)[0]


def _journal(datenbank: Path, aktion: str, zid: int) -> list[sqlite3.Row]:
    return _lies(datenbank, "SELECT * FROM journal WHERE aktion = ?"
                            " AND objekttyp = 'zeiteintrag' AND objekt_id = ?",
                 aktion, zid)


def _entwurf(klient, kunde: int, zeit_ids: list[int]) -> int:
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": str(kunde), "datum": "2026-03-10", "modus": "PRO_TITEL",
        "zeit": [str(z) for z in zeit_ids], "leistungszeitraum": "1",
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:300]
    return int(antwort.headers["location"].rsplit("/", 1)[1])


def _festschreiben(klient, datenbank: Path, rechnung_id: int) -> None:
    version = _lies(datenbank, "SELECT version FROM rechnung WHERE id = ?", rechnung_id)[0][0]
    seite = klient.get(f"/rechnung/{rechnung_id}").text
    codes = set(re.findall(r'value="(G\d+)"', seite)) | {"G02", "G05", "G21"}
    klient.post(f"/rechnung/{rechnung_id}/festschreiben", data={
        "version": str(version), "bestaetigt": sorted(codes),
    }, follow_redirects=False)
    status = _lies(datenbank, "SELECT status FROM rechnung WHERE id = ?", rechnung_id)[0][0]
    assert status == "FESTGESCHRIEBEN"


class TestDauerKorrigieren:
    """Entscheidung 4a: POST /zeiten/{id}/dauer."""

    def test_offene_zeit(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        assert f'action="/zeiten/{zid}/dauer"' in klient.get("/zeiten").text
        assert "Dauer ändern" in klient.get("/zeiten").text
        antwort = klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "1:45"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _meldung(antwort, "erfolg") == "Dauer geändert: 1:00 → 1:45 (Beratung)."
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["dauer_minuten"] == 105
        [zeile] = _journal(datenbank, "AENDERN", zid)
        assert zeile["feld"] == "dauer_minuten"
        assert (zeile["wert_alt"], zeile["wert_neu"]) == ("60", "105")

    @pytest.mark.parametrize("dauer", ["abc", "0", "25:00", ""])
    def test_unlesbare_dauer(self, klient, kunde, datenbank, dauer):
        zid = _zeit(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/dauer", data={"dauer": dauer},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert _meldung(antwort)
        assert _eintrag(datenbank, zid)["dauer_minuten"] == 60
        assert _journal(datenbank, "AENDERN", zid) == []

    def test_im_entwurf_wird_neu_berechnet(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [zid])
        vorher = _lies(datenbank, "SELECT version, netto_cent FROM rechnung WHERE id = ?", rid)[0]
        antwort = klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "2h"},
                              follow_redirects=False)
        assert "fehler=" not in antwort.headers["location"]
        nachher = _lies(datenbank, "SELECT version, netto_cent FROM rechnung WHERE id = ?", rid)[0]
        assert nachher["version"] > vorher["version"]
        assert nachher["netto_cent"] == 2 * vorher["netto_cent"]

    def test_festgeschrieben_bleibt(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [zid])
        _festschreiben(klient, datenbank, rid)
        antwort = klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "2h"},
                              follow_redirects=False)
        assert "Storno" in _meldung(antwort)
        assert _eintrag(datenbank, zid)["dauer_minuten"] == 60
        assert _journal(datenbank, "AENDERN", zid) == []
        assert f'action="/zeiten/{zid}/dauer"' not in klient.get("/zeiten").text

    def test_laufender_timer(self, klient, kunde, datenbank):
        klient.post("/zeiten/timer/start", data={"kunde_id": str(kunde), "titel": "Lauf"})
        zid = _lies(datenbank, "SELECT id FROM zeiteintrag WHERE end_zeit IS NULL")[0][0]
        antwort = klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "1:00"},
                              follow_redirects=False)
        assert "erst stoppen" in _meldung(antwort)

    def test_nach_langem_timer_korrigierbar(self, klient, kunde, datenbank):
        klient.post("/zeiten/timer/start", data={"kunde_id": str(kunde), "titel": "Lauf"})
        zid = _lies(datenbank, "SELECT id FROM zeiteintrag WHERE end_zeit IS NULL")[0][0]
        gestern = (_dt.date.today() - _dt.timedelta(days=2)).isoformat()
        _schreibe(datenbank, "UPDATE zeiteintrag SET datum = ?, start_zeit = '08:00:00'"
                             " WHERE id = ?", gestern, zid)
        # Seit Version 21 rechnet der Timer aus seinen Arbeitsabschnitten: der
        # offene Abschnitt wandert mit dem Beginn zurueck.
        from app.logik import zeiten

        _schreibe(datenbank, "UPDATE zeit_abschnitt SET beginn_utc = ?"
                             " WHERE zeiteintrag_id = ? AND ende_utc IS NULL",
                  zeiten.ortszeit_als_utc(gestern, "08:00:00"), zid)
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert "bitte prüfen" in _meldung(antwort, "hinweis")
        assert _eintrag(datenbank, zid)["dauer_minuten"] > 24 * 60
        klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "8h"})
        assert _eintrag(datenbank, zid)["dauer_minuten"] == 480


class TestKennungen:
    """Entscheidung 4b: '²' und 2**63 ergeben eine Meldung, nie 500."""

    @pytest.mark.parametrize(("kunde_id", "erwartet"), [
        ("²", "Bitte einen Kunden wählen."),
        (str(2**63), "Diesen Kunden gibt es nicht."),
        ("99999999999999999999", "Diesen Kunden gibt es nicht."),
        ("0", "Diesen Kunden gibt es nicht."),
    ])
    def test_kunde_im_formular(self, klient, kunde, datenbank, kunde_id, erwartet):
        for ziel, daten in (("/zeiten", {**ZEIT}), ("/zeiten/timer/start", {"titel": "T"})):
            antwort = klient.post(ziel, data={"kunde_id": kunde_id, **daten})
            assert antwort.status_code == 422, ziel
            assert erwartet in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM zeiteintrag")[0][0] == 0

    @pytest.mark.parametrize("wert", ["²", str(2**63), "99999999999999999999"])
    def test_filter_und_entwurf_leer(self, klient, kunde, wert):
        assert klient.get(f"/zeiten?kunde={wert}").status_code == 200
        assert klient.get(f"/zeiten?entwurf_leer={wert}").status_code == 200

    @pytest.mark.parametrize("wert", ["²", str(2**63), "1180591620717411303424", "abc"])
    @pytest.mark.parametrize("aktion", ["modus", "abschreiben", "zuruecknehmen",
                                        "freigeben", "loeschen", "dauer"])
    def test_zeit_kennung_im_pfad(self, klient, kunde, wert, aktion):
        """Ab 19 Stellen antwortet schon die zentrale Pruefung in app/main.py
        mit der deutschen 404-Seite; kuerzere Fehleingaben meldet die Route."""
        antwort = klient.post(f"/zeiten/{wert}/{aktion}",
                              data={"modus": "STUNDEN", "grund": "g", "dauer": "1:00"},
                              follow_redirects=False)
        if wert.isascii() and wert.isdigit() and len(wert) > 18:
            assert antwort.status_code == 404
            assert "Diese Seite gibt es nicht" in antwort.text
            return
        assert antwort.status_code == 303
        assert _meldung(antwort) == "Diesen Zeiteintrag gibt es nicht mehr."


class TestRuecksprung:
    """Entscheidungen 4c und 4e."""

    def test_fremder_referer_ist_kein_ziel(self, klient, kunde):
        """Mit passendem Origin laesst die Herkunftspruefung in app/main.py die
        Anfrage durch; der fremde Referer darf trotzdem kein Ziel werden."""
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False, headers={
            "origin": "http://testserver",
            "referer": "https://angreifer.example/zeiten?kunde=1",
        })
        assert antwort.headers["location"] == "/zeiten"

    def test_eigener_referer_behaelt_die_filter(self, klient, kunde):
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False, headers={
            "referer": "http://testserver/zeiten?kunde=1&fehler=alt",
        })
        assert antwort.headers["location"] == "/zeiten?kunde=1"

    @pytest.mark.parametrize("ziel", [
        "https://angreifer.example/zeiten", "//angreifer.example/zeiten",
        "/\\angreifer.example", "/rechnungen?x=1", "/zeiten/1/loeschen",
    ])
    def test_fremdes_feld_zurueck_wird_verworfen(self, klient, kunde, ziel):
        antwort = klient.post("/zeiten/timer/stop", data={"zurueck": ziel},
                              follow_redirects=False)
        assert antwort.headers["location"] == "/zeiten"

    def test_filter_bleibt_nach_abweisung_und_korrektur(self, klient, kunde, datenbank):
        """Regression: nach 422 und Korrektur landete man in der ungefilterten Liste."""
        liste = klient.get(f"/zeiten?kunde={kunde}&status=&von=&bis=").text
        zurueck = _feldwert(liste, "zurueck")
        assert zurueck == f"/zeiten?kunde={kunde}"
        abgewiesen = klient.post("/zeiten", data={
            "kunde_id": str(kunde), **ZEIT, "dauer": "99:99", "zurueck": zurueck,
        }, headers={"referer": f"http://testserver/zeiten?kunde={kunde}&status=&von=&bis="})
        assert abgewiesen.status_code == 422
        # Die neu gezeigten Formulare tragen den Filter weiter ...
        zurueck = _feldwert(abgewiesen.text, "zurueck")
        assert zurueck == f"/zeiten?kunde={kunde}"
        # ... auch wenn der Referer der Korrektur nur noch /zeiten ist.
        antwort = klient.post("/zeiten", data={
            "kunde_id": str(kunde), **ZEIT, "dauer": "0:30", "zurueck": zurueck,
        }, headers={"referer": "http://testserver/zeiten"}, follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"] == f"/zeiten?kunde={kunde}"


class TestStatusInDerTransaktion:
    """Entscheidung 4f: zweites Abschreiben ueberschreibt nichts, Journal nur einmal."""

    def test_zweites_abschreiben(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        klient.post(f"/zeiten/{zid}/abschreiben", data={"grund": "vergessen"})
        antwort = klient.post(f"/zeiten/{zid}/abschreiben", data={"grund": "anders"},
                              follow_redirects=False)
        assert "ist bereits abgeschrieben" in _meldung(antwort)
        assert _eintrag(datenbank, zid)["abgeschrieben_grund"] == "vergessen"
        assert len(_journal(datenbank, "ABSCHREIBEN", zid)) == 1

    def test_zweites_zuruecknehmen(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        klient.post(f"/zeiten/{zid}/abschreiben", data={"grund": "vergessen"})
        klient.post(f"/zeiten/{zid}/zuruecknehmen")
        antwort = klient.post(f"/zeiten/{zid}/zuruecknehmen", follow_redirects=False)
        assert _meldung(antwort) == "Diese Zeit ist nicht abgeschrieben."
        assert len(_journal(datenbank, "ABSCHREIBEN_ZURUECK", zid)) == 1

    def test_freigeben_ohne_storno(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/freigeben", follow_redirects=False)
        assert "stornierten Rechnung" in _meldung(antwort)
        assert _journal(datenbank, "FREIGEBEN", zid) == []

    def test_modus_ohne_treffer_schreibt_kein_journal(self, klient, kunde, datenbank):
        """Aendert sich die Version zwischen Lesen und UPDATE, kein Journal."""
        zid = _zeit(klient, kunde, datenbank)
        _schreibe(datenbank, """
            CREATE TRIGGER stoer BEFORE UPDATE OF abrechnungsmodus ON zeiteintrag
            BEGIN SELECT RAISE(IGNORE); END
        """)
        antwort = klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": "g"},
                              follow_redirects=False)
        assert "inzwischen geändert" in _meldung(antwort)
        assert _journal(datenbank, "MODUS", zid) == []


class TestDatumDerZeit:
    """Entscheidung 4d: Jahr ab 2000."""

    def test_jahr_0026(self, klient, kunde, datenbank):
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT,
                                               "datum": "0026-02-10"})
        assert antwort.status_code == 422
        assert "vor dem Jahr 2000" in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM zeiteintrag")[0][0] == 0

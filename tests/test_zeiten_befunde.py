"""Befunde der Fehleranalyse zur Zeiterfassung -- je Befund eine Regression.

Der rote Faden: die Zeitenliste und ihre Aktionen antworten NIE mit einer
JSON-Fehlerseite oder einem HTTP 500. Wer vor so einer Seite steht, haelt das
Werkzeug fuer kaputt und arbeitet daran vorbei -- und dann loest es das
Ausgangsproblem nicht mehr. Jede Abweisung ist deshalb ein Redirect auf die
Liste mit einer deutschen Meldung, und jede Sackgasse (Timer auf einer
festgeschriebenen Rechnung, Zeit auf einer stornierten Rechnung) hat einen
Ausweg, den die Meldung nennt.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import zeiten as logik
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}
ZEIT = {"titel": "Beratung", "datum": "2026-03-02", "dauer": "60"}


@pytest.fixture
def klient(tmp_path):
    with TestClient(erzeuge_app(tmp_path / "web.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


@pytest.fixture
def kunde(klient):
    antwort = klient.post("/kunden", data=KUNDE, follow_redirects=False)
    assert antwort.status_code == 303
    return int(antwort.headers["location"].rsplit("/", 1)[1])


@pytest.fixture
def datenbank(tmp_path):
    def oeffnen() -> sqlite3.Connection:
        verbindung = sqlite3.connect(tmp_path / "web.sqlite")
        verbindung.row_factory = sqlite3.Row
        return verbindung

    return oeffnen


# ------------------------------------------------------------------ Helfer

def _meldung(antwort, name: str = "fehler") -> str:
    """Die Meldung aus der Redirect-URL, dekodiert.

    Erfassen und Timer-Start antworten auf eine Abweisung inzwischen mit der
    neu gezeigten Liste (422) samt Eingaben statt mit einem Redirect, der die
    Eingaben verwarf (Befund der Browserpruefung) -- dann steht die Meldung
    in der Seite.
    """
    if antwort.status_code == 422 and name == "fehler":
        treffer = re.search(r'<div class="meldung fehler">(.*?)</div>', antwort.text, re.S)
        assert treffer, antwort.text[:300]
        return html.unescape(treffer.group(1).strip())
    assert antwort.status_code == 303, antwort.text[:300]
    query = parse_qs(urlsplit(antwort.headers["location"]).query)
    return unquote(query.get(name, [""])[0])


def _zeit(klient, kunde: int, datenbank, **felder) -> int:
    """Erfasst eine Zeit ueber das Formular und gibt ihre ID zurueck."""
    daten = {"kunde_id": str(kunde), **ZEIT, **felder}
    antwort = klient.post("/zeiten", data=daten, follow_redirects=False)
    assert antwort.status_code == 303
    assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
    with datenbank() as conn:
        return conn.execute("SELECT MAX(id) FROM zeiteintrag").fetchone()[0]


def _timer(klient, kunde: int, datenbank, titel: str = "Laufend") -> int:
    antwort = klient.post("/zeiten/timer/start", data={"kunde_id": str(kunde), "titel": titel},
                          follow_redirects=False)
    assert antwort.status_code == 303
    with datenbank() as conn:
        return conn.execute(
            "SELECT id FROM zeiteintrag WHERE end_zeit IS NULL AND start_zeit IS NOT NULL"
        ).fetchone()[0]


def _entwurf(klient, kunde: int, zeit_ids: list[int]) -> int:
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": str(kunde), "datum": "2026-03-10", "modus": "PRO_TITEL",
        "zeit": [str(z) for z in zeit_ids], "leistungszeitraum": "1",
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:300]
    return int(antwort.headers["location"].rsplit("/", 1)[1])


def _festschreiben(klient, datenbank, rechnung_id: int) -> str:
    """Schreibt den Entwurf fest und gibt die Rechnungsnummer zurueck."""
    with datenbank() as conn:
        version = conn.execute(
            "SELECT version FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()["version"]
    seite = klient.get(f"/rechnung/{rechnung_id}").text
    codes = set(re.findall(r'value="(G\d+)"', seite)) | {"G02", "G05", "G21"}
    antwort = klient.post(f"/rechnung/{rechnung_id}/festschreiben", data={
        "version": str(version), "bestaetigt": sorted(codes),
    }, follow_redirects=False)
    assert antwort.status_code == 303
    with datenbank() as conn:
        zeile = conn.execute(
            "SELECT status, nummer FROM rechnung WHERE id = ?", (rechnung_id,)
        ).fetchone()
    assert zeile["status"] == "FESTGESCHRIEBEN", antwort.headers["location"]
    return zeile["nummer"]


def _storno(klient, rechnung_id: int, *, freigeben: bool) -> None:
    antwort = klient.post(f"/rechnung/{rechnung_id}/storno", data={
        "grund": "falsch abgerechnet", "zeiten_freigeben": "1" if freigeben else "0",
    }, follow_redirects=False)
    assert antwort.status_code == 303
    assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]


def _zeile(klient, zeit_id: int) -> str:
    """Der HTML-Ausschnitt der Tabellenzeile einer Zeit."""
    text = klient.get("/zeiten").text
    zeilen = re.findall(r"<tr>.*?</tr>", text, flags=re.S)
    treffer = [z for z in zeilen if f"/zeiten/{zeit_id}/" in z or f'zeit-{zeit_id}' in z]
    # Eine Zeile ohne Aktionen traegt keine URL mit der ID: dann ueber den Titel.
    return treffer[0] if treffer else ""


def _timer_zurueckdatieren(datenbank, zeit_id: int, tage: int, start_zeit: str) -> None:
    """Laesst einen gerade gestarteten Timer seit ``tage`` Tagen um ``start_zeit`` laufen.

    Seit Version 21 rechnet der Timer aus seinen Arbeitsabschnitten; der
    offene Abschnitt wandert deshalb mit dem Beginn des Eintrags zurueck.
    """
    datum = (_dt.date.today() - _dt.timedelta(days=tage)).isoformat()
    with datenbank() as conn:
        conn.execute("UPDATE zeiteintrag SET datum = ?, start_zeit = ? WHERE id = ?",
                     (datum, start_zeit, zeit_id))
        conn.execute("UPDATE zeit_abschnitt SET beginn_utc = ? WHERE zeiteintrag_id = ?"
                     " AND ende_utc IS NULL",
                     (logik.ortszeit_als_utc(datum, start_zeit), zeit_id))
        conn.commit()


def _als_altbestand_timer(datenbank, zeit_id: int, tage: int, start_zeit: str) -> None:
    """Macht eine Zeit im Entwurf nachtraeglich zum laufenden Timer.

    Ueber die Oberflaeche geht das nicht mehr: der Rechnungsassistent bietet
    laufende Timer nicht an, und entwurf_anlegen filtert sie aus. Der Zustand
    "Platzhalter von einer Minute auf einem Entwurf oder einer Rechnung" kann
    aber aus Altbestand stammen, der vor dieser Sperre entstand -- und genau
    dafuer muss das Stoppen weiter funktionieren.
    """
    datum = (_dt.date.today() - _dt.timedelta(days=tage)).isoformat()
    with datenbank() as conn:
        conn.execute(
            "UPDATE zeiteintrag SET datum = ?, start_zeit = ?, end_zeit = NULL,"
            " dauer_minuten = 1 WHERE id = ?",
            (datum, start_zeit, zeit_id),
        )
        conn.commit()


def _eintrag(datenbank, zeit_id: int) -> sqlite3.Row | None:
    with datenbank() as conn:
        return conn.execute("SELECT * FROM zeiteintrag WHERE id = ?", (zeit_id,)).fetchone()


# -------------------------------------------------------- 1 Filter der Liste

class TestFilter:
    def test_leerer_kundenfilter_ist_alle_kunden(self, klient, kunde, datenbank):
        """Das Formular sendet kunde= fuer "alle Kunden" -- vorher 422 mit JSON."""
        _zeit(klient, kunde, datenbank)
        antwort = klient.get("/zeiten?kunde=&status=&von=&bis=")
        assert antwort.status_code == 200
        assert "Beratung" in antwort.text

    @pytest.mark.parametrize("query", ["kunde=abc", "kunde=1.5", "kunde=-", "status=UNSINN"])
    def test_unsinn_im_filter_ergibt_eine_seite(self, klient, kunde, query):
        antwort = klient.get(f"/zeiten?{query}")
        assert antwort.status_code == 200
        assert "<h1>Zeiten</h1>" in antwort.text

    def test_stillgelegter_kunde_bleibt_im_filter(self, klient, kunde, datenbank):
        """Seine Zeiten sind sonst nur noch ueber "alle Kunden" zu finden."""
        _zeit(klient, kunde, datenbank)
        klient.post(f"/kunden/{kunde}", data={**KUNDE, "aktiv": "0"})
        text = klient.get("/zeiten").text
        filter_html = text.split('aria-label="Kunde filtern"')[1].split("</select>")[0]
        assert "Mustermann GmbH (stillgelegt)" in filter_html
        # Das Erfassungsformular bietet ihn dagegen nicht mehr an.
        erfassen_html = text.split('id="erfassen"')[1].split("</select>")[0]
        assert "Mustermann" not in erfassen_html

    def test_kunde_ohne_zeiten_steht_nicht_im_filter(self, klient, kunde):
        text = klient.get("/zeiten").text
        filter_html = text.split('aria-label="Kunde filtern"')[1].split("</select>")[0]
        assert "Mustermann" not in filter_html


# ---------------------------------------------------------------- 2 Timer

class TestTimerStoppen:
    def test_dauer_rechnet_ueber_das_datum(self, klient, kunde, datenbank):
        """Gestern 23:00 bis heute 10:44 sind 704 Minuten, nicht eine."""
        zid = _timer(klient, kunde, datenbank)
        _timer_zurueckdatieren(datenbank, zid, tage=1, start_zeit="23:00:00")
        vorher = _dt.datetime.now()
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"]
        eintrag = _eintrag(datenbank, zid)
        erwartet = logik.timer_minuten(eintrag["datum"], "23:00:00", vorher)
        assert abs(eintrag["dauer_minuten"] - erwartet) <= 1
        assert eintrag["dauer_minuten"] >= 60
        assert eintrag["end_zeit"] is not None
        # Kein Hinweis: der Timer lief unter 24 Stunden.
        assert "hinweis=" not in antwort.headers["location"]

    def test_endzeit_ist_jetzt_wenn_der_tag_stimmt(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        vorher = _dt.datetime.now().replace(microsecond=0)
        klient.post("/zeiten/timer/stop", follow_redirects=False)
        eintrag = _eintrag(datenbank, zid)
        ende = _dt.time.fromisoformat(eintrag["end_zeit"])
        if vorher.time() <= ende:  # nicht ueber Mitternacht gelaufen
            differenz = _dt.datetime.combine(vorher.date(), ende) - vorher
            assert differenz.total_seconds() < 5

    def test_laufzeit_ueber_24_stunden_wird_gemeldet(self, klient, kunde, datenbank):
        """Angepasst in der Schlussprobe: der Hinweis endete mit „bitte prüfen.“
        und nannte keinen Weg. Er nennt jetzt „Dauer ändern“ -- das
        Erfassen-Formular nimmt die gebuchte Dauer ueber 24 Stunden nicht an."""
        zid = _timer(klient, kunde, datenbank)
        _timer_zurueckdatieren(datenbank, zid, tage=3, start_zeit="08:00:00")
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        hinweis = _meldung(antwort, "hinweis")
        assert re.fullmatch(
            r"Der Timer lief \d+ Stunden — bitte prüfen und in der Liste unter "
            r"„Dauer ändern“ korrigieren\.", hinweis), hinweis
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["dauer_minuten"] > 24 * 60, "gebucht wird trotzdem die Laufzeit"
        assert eintrag["end_zeit"] is not None
        assert "Läuft seit" not in klient.get("/zeiten").text

    def test_timer_auf_festgeschriebener_rechnung_ist_keine_sackgasse(
        self, klient, kunde, datenbank
    ):
        """Vorher: Stoppen 500, jeder neue Timer 500, Timer lief fuer immer.

        Der Zustand entsteht heute nur noch aus Altbestand (siehe
        _als_altbestand_timer); der Trigger laesst die Dauer danach nicht mehr
        anfassen -- genau deshalb krachte das Stoppen.
        """
        timer = _zeit(klient, kunde, datenbank, titel="Laufend")
        zeit = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [timer, zeit])
        _als_altbestand_timer(datenbank, timer, tage=1, start_zeit="08:00:00")
        nummer = _festschreiben(klient, datenbank, rid)

        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        meldung = _meldung(antwort)
        assert nummer in meldung
        assert "Storno" in meldung
        eintrag = _eintrag(datenbank, timer)
        assert eintrag["end_zeit"] is not None, "der Timer laeuft nicht mehr"
        assert eintrag["dauer_minuten"] == 1, "die abgerechnete Dauer bleibt"
        assert "Läuft seit" not in klient.get("/zeiten").text

        # Und ein neuer Timer startet wieder.
        antwort = klient.post("/zeiten/timer/start",
                              data={"kunde_id": str(kunde), "titel": "Neu"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "Läuft seit" in klient.get("/zeiten").text

    def test_neuer_timer_stoppt_den_festgeschriebenen_mit_meldung(
        self, klient, kunde, datenbank
    ):
        timer = _zeit(klient, kunde, datenbank, titel="Laufend")
        rid = _entwurf(klient, kunde, [timer])
        _als_altbestand_timer(datenbank, timer, tage=1, start_zeit="08:00:00")
        _festschreiben(klient, datenbank, rid)
        antwort = klient.post("/zeiten/timer/start",
                              data={"kunde_id": str(kunde), "titel": "Neu"},
                              follow_redirects=False)
        assert "Storno" in _meldung(antwort)
        with datenbank() as conn:
            laufend = conn.execute(
                "SELECT titel FROM zeiteintrag WHERE end_zeit IS NULL AND start_zeit IS NOT NULL"
            ).fetchall()
        assert [z["titel"] for z in laufend] == ["Neu"]

    def test_timer_im_entwurf_zieht_die_position_nach(self, klient, kunde, datenbank):
        """Sonst geht die Rechnung mit dem Platzhalter von einer Minute hinaus.

        Altbestand: eine Minute im Entwurf, die sich nachtraeglich als laufender
        Timer herausstellt (siehe _als_altbestand_timer).
        """
        timer = _zeit(klient, kunde, datenbank, titel="Laufend", dauer="1")
        rid = _entwurf(klient, kunde, [timer])
        with datenbank() as conn:
            vorher = conn.execute("SELECT version FROM rechnung WHERE id = ?", (rid,)).fetchone()
            menge_vorher = conn.execute(
                "SELECT menge_tausendstel FROM rechnungsposition WHERE rechnung_id = ?", (rid,)
            ).fetchone()[0]
        assert menge_vorher == 250, "Vorbedingung: eine Minute, aufgerundet auf 0,25 h"

        _als_altbestand_timer(datenbank, timer, tage=1, start_zeit="23:00:00")
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert antwort.status_code == 303
        with datenbank() as conn:
            nachher = conn.execute("SELECT version FROM rechnung WHERE id = ?", (rid,)).fetchone()
            position = conn.execute(
                "SELECT menge_tausendstel, betrag_cent FROM rechnungsposition"
                " WHERE rechnung_id = ?", (rid,)
            ).fetchone()
        minuten = _eintrag(datenbank, timer)["dauer_minuten"]
        assert position["menge_tausendstel"] >= minuten * 1000 // 60
        assert position["menge_tausendstel"] > 250
        assert position["betrag_cent"] > 0
        assert nachher["version"] > vorher["version"], "veraltete Seiten muessen auffallen"


# ------------------------------------------- 3 Aktionen auf laufendem Timer

class TestLaufenderTimer:
    def test_abschreiben_verlangt_erst_stoppen(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/abschreiben", data={"grund": "egal"},
                              follow_redirects=False)
        assert _meldung(antwort) == "Der Timer läuft noch — erst stoppen."
        assert _eintrag(datenbank, zid)["abgeschrieben_am"] is None
        assert "Läuft seit" in klient.get("/zeiten").text

    def test_modus_verlangt_erst_stoppen(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": "x"},
                              follow_redirects=False)
        assert _meldung(antwort) == "Der Timer läuft noch — erst stoppen."
        assert _eintrag(datenbank, zid)["abrechnungsmodus"] == "STUNDEN"

    def test_loeschen_ist_der_abbruch(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/loeschen", follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"]
        assert _eintrag(datenbank, zid) is None
        assert "Läuft seit" not in klient.get("/zeiten").text

    def test_liste_zeigt_den_timer_ohne_abschreiben(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        zeile = _zeile(klient, zid)
        assert "läuft" in zeile
        assert f"/zeiten/{zid}/loeschen" in zeile
        assert f"/zeiten/{zid}/abschreiben" not in zeile
        assert f"/zeiten/{zid}/modus" not in zeile


# ----------------------------------------------- 4 Storno mit Freigabe

class TestStornoMitFreigabe:
    def test_kein_loeschen_knopf_und_verstaendliche_meldung(self, klient, kunde, datenbank):
        """Vorher: Knopf da, Klick → 500 FOREIGN KEY (position_zeiteintrag)."""
        zid = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [zid])
        nummer = _festschreiben(klient, datenbank, rid)
        _storno(klient, rid, freigeben=True)

        text = klient.get("/zeiten").text
        assert "Aufgeschrieben" in text
        assert f"/zeiten/{zid}/loeschen" not in text
        assert f"/zeiten/{zid}/abschreiben" in text, "abschreiben bleibt der Weg"

        antwort = klient.post(f"/zeiten/{zid}/loeschen", follow_redirects=False)
        meldung = _meldung(antwort)
        assert meldung == (
            f"Diese Zeit lag auf {nummer} und bleibt als Aufzeichnung erhalten "
            f"— bitte abschreiben statt löschen."
        )
        assert _eintrag(datenbank, zid) is not None


# ----------------------------------------------- 5 Storno ohne Freigabe

class TestStornoOhneFreigabe:
    def _gebunden(self, klient, kunde, datenbank) -> tuple[int, int, str]:
        zid = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [zid])
        nummer = _festschreiben(klient, datenbank, rid)
        _storno(klient, rid, freigeben=False)
        return zid, rid, nummer

    def test_eigener_status_mit_freigeben_knopf(self, klient, kunde, datenbank):
        zid, _, _ = self._gebunden(klient, kunde, datenbank)
        zeile = _zeile(klient, zid)
        assert "Storniert, nicht freigegeben" in zeile
        assert f"/zeiten/{zid}/freigeben" in zeile
        for aktion in ("loeschen", "abschreiben", "modus"):
            assert f"/zeiten/{zid}/{aktion}" not in zeile, aktion

    def test_filter_kennt_den_status(self, klient, kunde, datenbank):
        zid, _, _ = self._gebunden(klient, kunde, datenbank)
        _zeit(klient, kunde, datenbank, titel="Zweitauftrag")
        text = klient.get("/zeiten?status=STORNIERT_GEBUNDEN").text
        assert 'value="STORNIERT_GEBUNDEN" selected' in text
        assert f"/zeiten/{zid}/freigeben" in text
        liste = text.split("<h2>Erfasst")[1]
        assert "<strong>Zweitauftrag</strong>" not in liste
        assert "<strong>Zweitauftrag</strong>" in klient.get("/zeiten?status=AUFGESCHRIEBEN").text

    def test_modus_und_abschreiben_sagen_storniert(self, klient, kunde, datenbank):
        zid, _, nummer = self._gebunden(klient, kunde, datenbank)
        for pfad, felder in (
            (f"/zeiten/{zid}/modus", {"modus": "KULANZ", "grund": "x"}),
            (f"/zeiten/{zid}/abschreiben", {"grund": "x"}),
        ):
            meldung = _meldung(klient.post(pfad, data=felder, follow_redirects=False))
            assert "storniert" in meldung and "nicht freigegeben" in meldung, meldung
            assert "erst freigeben" in meldung
            assert "festgeschrieben" not in meldung, "die alte, falsche Meldung"
            assert nummer in meldung

    def test_freigeben_loest_die_zeit(self, klient, kunde, datenbank):
        zid, rid, nummer = self._gebunden(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/freigeben", follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"]
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["rechnung_id"] is None
        assert eintrag["freigegeben_aus_rechnung_id"] == rid
        with datenbank() as conn:
            journal = conn.execute(
                "SELECT * FROM journal WHERE objekttyp = 'zeiteintrag' AND objekt_id = ?"
                " AND aktion = 'FREIGEBEN'", (zid,)
            ).fetchall()
        assert len(journal) == 1
        assert nummer in journal[0]["begruendung"]
        zeile = _zeile(klient, zid)
        assert "Aufgeschrieben" in zeile
        assert f"/zeiten/{zid}/abschreiben" in zeile
        # Nach der Freigabe ist die Zeit wieder abrechenbar ...
        assert f'value="{zid}"' in klient.get(f"/rechnung/neu?kunde={kunde}").text
        # ... aber nicht loeschbar: sie lag auf einer Rechnung.
        assert f"/zeiten/{zid}/loeschen" not in zeile

    def test_freigeben_nur_bei_stornierter_rechnung(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        meldung = _meldung(klient.post(f"/zeiten/{zid}/freigeben", follow_redirects=False))
        assert "stornierten Rechnung" in meldung
        rid = _entwurf(klient, kunde, [zid])
        _festschreiben(klient, datenbank, rid)
        meldung = _meldung(klient.post(f"/zeiten/{zid}/freigeben", follow_redirects=False))
        assert "stornierten Rechnung" in meldung
        assert _eintrag(datenbank, zid)["rechnung_id"] == rid


# ------------------------------------------------------- 6 Ruecksprung

class TestRuecksprung:
    def test_alte_meldung_bleibt_nicht_stehen(self, klient, kunde):
        """Korrigiert und erneut gesendet: der Eintrag ist da, der Fehler weg."""
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT},
                              headers={"referer": "http://testserver/zeiten?kunde=1&fehler=alt"
                                                  "&hinweis=h&erfolg=e&entwurf_leer=3"},
                              follow_redirects=False)
        ziel = urlsplit(antwort.headers["location"])
        assert ziel.path == "/zeiten"
        query = parse_qs(ziel.query)
        assert query == {"kunde": ["1"]}, query

    def test_filter_des_aufrufers_bleiben(self, klient, kunde):
        """Die Abweisung zeigt die Liste mit 422 neu (statt Redirect) -- mit den
        Filtern aus dem Referer, wie vorher der Redirect sie mitnahm."""
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, "dauer": "abc"},
                              headers={"referer": "http://testserver/zeiten?status=AUFGESCHRIEBEN"
                                                  "&von=2026-01-01"},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert re.search(r'value="AUFGESCHRIEBEN" selected', antwort.text)
        assert 'name="von" type="date" value="2026-01-01"' in antwort.text
        assert "keine Dauer" in _meldung(antwort)

    @pytest.mark.parametrize("dauer", ["1&2", "1#2", "a+b", "1%2"])
    def test_sonderzeichen_zerreissen_die_meldung_nicht(self, klient, kunde, dauer):
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, "dauer": dauer},
                              follow_redirects=False)
        meldung = _meldung(antwort)
        assert f"„{dauer}“ ist keine Dauer" in meldung
        # Und das Feld traegt die Eingabe unversehrt wieder (422 statt Redirect).
        assert f'value="{html.escape(dauer)}"' in antwort.text

    def test_fremder_referer_fuehrt_auf_die_liste(self, klient, kunde):
        antwort = klient.post("/zeiten/timer/stop",
                              headers={"referer": "http://testserver/rechnungen?x=1"},
                              follow_redirects=False)
        assert urlsplit(antwort.headers["location"]).path == "/zeiten"


# ---------------------------------------------- 7 Titel, 8 Datum, 16 Kunde

class TestEingabepruefung:
    @pytest.mark.parametrize("titel", ["", "   ", "\t\n"])
    def test_titel_aus_leerzeichen_wird_abgewiesen(self, klient, kunde, datenbank, titel):
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, "titel": titel},
                              follow_redirects=False)
        assert _meldung(antwort) == "Der Titel fehlt."
        antwort = klient.post("/zeiten/timer/start",
                              data={"kunde_id": str(kunde), "titel": titel},
                              follow_redirects=False)
        assert _meldung(antwort) == "Der Titel fehlt."
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM leistung").fetchone()[0] == 0
        assert "nicht im Katalog" not in klient.get("/leistungen").text.split("<h2>")[0]

    @pytest.mark.parametrize("datum, erwartet", [
        ("abc", "kein Datum"),
        # TT.MM.JJJJ wird seit der Schlussprobe gelesen; ungültig bleibt ungültig.
        ("31.13.2026", "kein gültiges Datum"),
        ("31.12.2099", "liegt in der Zukunft"),
        ("2026-13-01", "kein gültiges Datum"),
        ("2027-02-29", "kein gültiges Datum"),
        ("", "Datum fehlt"),
        ("2099-01-01", "liegt in der Zukunft"),
    ])
    def test_datum_wird_geprueft(self, klient, kunde, datenbank, datum, erwartet):
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, "datum": datum},
                              follow_redirects=False)
        assert erwartet in _meldung(antwort)
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0
        # Der Rechnungsassistent stuerzte vorher ueber kaputte Daten ab.
        assert klient.get(f"/rechnung/neu?kunde={kunde}").status_code == 200

    def test_morgen_ist_zukunft_heute_nicht(self, klient, kunde, datenbank):
        heute = _dt.date.today()
        morgen = (heute + _dt.timedelta(days=1)).isoformat()
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, "datum": morgen},
                              follow_redirects=False)
        meldung = _meldung(antwort)
        tag = heute + _dt.timedelta(days=1)
        assert meldung == f"Das Datum {tag:%d.%m.%Y} liegt in der Zukunft."
        _zeit(klient, kunde, datenbank, datum=heute.isoformat())

    def test_datumsfeld_hat_eine_obergrenze(self, klient, kunde):
        assert f'max="{db.heute()}"' in klient.get("/zeiten").text

    def test_pruefe_datum_logik(self):
        assert logik.pruefe_datum(" 2026-03-02 ", heute="2026-03-02") == "2026-03-02"
        with pytest.raises(logik.DatumUngueltig, match="Zukunft"):
            logik.pruefe_datum("2026-03-03", heute="2026-03-02")
        # Seit der Schlussprobe auch deutsch getippt (Kalenderfeld als Textfeld).
        assert logik.pruefe_datum("2.3.2026", heute="2026-03-02") == "2026-03-02"
        with pytest.raises(logik.DatumUngueltig, match="TT.MM.JJJJ"):
            logik.pruefe_datum("2026/03/02")

    @pytest.mark.parametrize("kunde_id, erwartet", [
        ("999", "Diesen Kunden gibt es nicht."),
        ("", "Bitte einen Kunden wählen."),
        ("abc", "Bitte einen Kunden wählen."),
    ])
    def test_unbekannter_kunde_ist_kein_serverfehler(self, klient, kunde, datenbank,
                                                     kunde_id, erwartet):
        antwort = klient.post("/zeiten", data={"kunde_id": kunde_id, **ZEIT},
                              follow_redirects=False)
        assert _meldung(antwort) == erwartet
        antwort = klient.post("/zeiten/timer/start", data={"kunde_id": kunde_id, "titel": "X"},
                              follow_redirects=False)
        assert _meldung(antwort) == erwartet
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0

    def test_fehlende_felder_ergeben_eine_meldung(self, klient, kunde):
        """Form("") statt Form(...): kein generischer 422 fuer ein fehlendes Feld."""
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde)}, follow_redirects=False)
        # 422 mit der deutschen Meldung in der neu gezeigten Liste (nicht mehr
        # 303): die Eingaben bleiben so erhalten.
        assert antwort.status_code == 422
        assert _meldung(antwort) == "Der Titel fehlt."
        antwort = klient.post("/zeiten/timer/start", data={}, follow_redirects=False)
        assert antwort.status_code == 422
        assert _meldung(antwort) == "Bitte einen Kunden wählen."


# ----------------------------------------------------- 9 Leistungskatalog

class TestKatalogImFormular:
    def test_beschreibungsvorlage_wird_vorgeschlagen(self, klient, kunde):
        klient.post("/leistungen", data={
            "titel": "Workshop", "einheit": "DAY", "preis_euro": "1200",
            "beschreibung_vorlage": "Ganztagesworkshop vor Ort",
        })
        klient.post("/leistungen", data={"titel": "Ohne Vorlage", "einheit": "HUR"})
        text = klient.get("/zeiten").text
        daten = re.search(
            r'<script type="application/json" id="beschreibungsvorlagen">(.*?)</script>', text
        )
        assert daten, "die JSON-Map fehlt"
        import json

        vorlagen = json.loads(daten.group(1))
        assert vorlagen == {"Workshop": "Ganztagesworkshop vor Ort"}
        assert "addEventListener('change'" in text
        # Seit Version 21 ist die Beschreibung ein mehrzeiliges Feld, und die
        # Vorlage greift auch beim Timer-Start: der Selektor nimmt jedes Feld
        # namens "beschreibung", in beiden Formularen.
        assert "querySelector('[name=\"beschreibung\"]')" in text
        assert "['erfassen', 'timer-start']" in text

    def test_ohne_vorlagen_kein_script(self, klient, kunde):
        klient.post("/leistungen", data={"titel": "Beratung", "einheit": "HUR"})
        text = klient.get("/zeiten").text
        assert 'id="beschreibungsvorlagen"' not in text

    def test_html_in_der_vorlage_bleibt_daten(self, klient, kunde):
        klient.post("/leistungen", data={
            "titel": "Boese", "einheit": "HUR",
            "beschreibung_vorlage": "</script><script>alert(1)</script>",
        })
        text = klient.get("/zeiten").text
        assert "<script>alert(1)</script>" not in text

    def test_hinweistexte_sind_ehrlich(self, klient):
        text = klient.get("/leistungen").text
        assert "leer = Stundensatz des Kunden" not in text
        assert "wird derzeit nicht auf Rechnungen übernommen" in text
        assert "Stundensatz des Kunden" in text
        assert "auf Rechnungen steht derzeit immer die" in text

    def test_preisobergrenze(self, klient, datenbank):
        """Vorher: OverflowError beim Speichern → 500."""
        antwort = klient.post("/leistungen", data={
            "titel": "Riesig", "einheit": "HUR", "preis_euro": "99999999999999999999",
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert "1.000.000" in _meldung(antwort)
        klient.post("/leistungen", data={"titel": "Normal", "einheit": "HUR",
                                         "preis_euro": "1.000.000,00"})
        with datenbank() as conn:
            titel = [z[0] for z in conn.execute("SELECT titel FROM leistung ORDER BY titel")]
        assert titel == ["Normal"]
        with datenbank() as conn:
            lid = conn.execute("SELECT id FROM leistung").fetchone()[0]
        antwort = klient.post(f"/leistungen/{lid}", data={
            "titel": "Normal", "einheit": "HUR", "aktiv": "1", "preis_euro": "1.000.000,01",
        }, follow_redirects=False)
        assert "1.000.000" in _meldung(antwort)


# ------------------------------------------------- 10 Grenze der Liste

class TestListengrenze:
    def test_summe_zaehlt_alle_und_die_kuerzung_wird_genannt(self, klient, kunde, datenbank):
        with datenbank() as conn:
            conn.executemany(
                "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
                " VALUES (?, 'Masse', ?, 60, ?)",
                [(kunde, f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", db.jetzt_utc())
                 for i in range(405)],
            )
            conn.commit()
        text = klient.get("/zeiten").text
        assert "Die Liste zeigt die neuesten 400 von 405 Einträgen" in text
        assert "405,00 h" in text
        assert text.count("<tr>") == 400

    def test_ohne_kuerzung_kein_hinweis(self, klient, kunde, datenbank):
        _zeit(klient, kunde, datenbank)
        assert "Die Liste zeigt die neuesten" not in klient.get("/zeiten").text

    def test_summe_folgt_dem_filter(self, klient, kunde, datenbank):
        _zeit(klient, kunde, datenbank, dauer="60")
        _zeit(klient, kunde, datenbank, dauer="30", datum="2026-04-01")
        text = klient.get("/zeiten?von=2026-04-01").text
        assert "0,50 h" in text
        assert "1,50 h" not in text


# -------------------------------------------------- 11 Meldungen der Dauer

class TestDauerMeldungen:
    def test_umlaute_und_anfuehrungszeichen(self):
        with pytest.raises(logik.DauerUngueltig) as fehler:
            logik.parse_dauer("0")
        assert str(fehler.value) == "Die Dauer muss größer als null sein: „0“"
        with pytest.raises(logik.DauerUngueltig) as fehler:
            logik.parse_dauer("abc")
        assert str(fehler.value) == (
            "„abc“ ist keine Dauer. Möglich sind etwa 1:45, 1h45, 1,75, 105 oder 90min."
        )
        assert "'" not in str(fehler.value)

    @pytest.mark.parametrize("eingabe", ["25:00", "1441", "24,01", "24h01", "99999999999999999999"])
    def test_obergrenze_24_stunden(self, eingabe):
        with pytest.raises(logik.DauerUngueltig) as fehler:
            logik.parse_dauer(eingabe)
        meldung = str(fehler.value)
        assert "24 Stunden" in meldung or "keine Dauer" in meldung

    def test_genau_24_stunden_gehen(self):
        assert logik.parse_dauer("24:00") == 1440
        assert logik.parse_dauer("1440") == 1440

    def test_lange_eingabe_wird_gekuerzt(self):
        with pytest.raises(logik.DauerUngueltig) as fehler:
            logik.parse_dauer("x" * 200)
        assert len(str(fehler.value)) < 150
        assert "…" in str(fehler.value)

    def test_riesige_zahl_ist_kein_serverfehler(self, klient, kunde, datenbank):
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT,
                                               "dauer": "99999999999999999999"},
                              follow_redirects=False)
        # 422 mit Meldung und erhaltenen Eingaben (vorher Redirect, 303).
        assert antwort.status_code == 422
        assert _meldung(antwort)
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, "dauer": "25:00"},
                              follow_redirects=False)
        assert "24 Stunden" in _meldung(antwort)
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0


# ------------------------------------------------- 12 PAUSCHAL und INTERN

class TestPauschalUndIntern:
    def test_pauschal_wird_abgewiesen(self, klient, kunde, datenbank):
        """Vorher: CHECK (pauschal_cent) → 500."""
        antwort = klient.post("/zeiten", data={"kunde_id": str(kunde), **ZEIT, "modus": "PAUSCHAL"},
                              follow_redirects=False)
        assert _meldung(antwort) == "Pauschalen werden nicht über dieses Formular erfasst."
        zid = _zeit(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/modus", data={"modus": "PAUSCHAL"},
                              follow_redirects=False)
        assert _meldung(antwort) == "Pauschalen werden nicht über dieses Formular erfasst."
        assert _eintrag(datenbank, zid)["abrechnungsmodus"] == "STUNDEN"

    def test_intern_im_entwurf_loest_die_zeit(self, klient, kunde, datenbank):
        """Vorher: CHECK (INTERN ⇒ rechnung_id IS NULL) → 500."""
        a = _zeit(klient, kunde, datenbank)
        b = _zeit(klient, kunde, datenbank, datum="2026-03-03")
        rid = _entwurf(klient, kunde, [a, b])
        with datenbank() as conn:
            voll = conn.execute("SELECT netto_cent, version FROM rechnung WHERE id = ?",
                                (rid,)).fetchone()
        antwort = klient.post(f"/zeiten/{a}/modus", data={"modus": "INTERN", "grund": "intern"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"]
        eintrag = _eintrag(datenbank, a)
        assert eintrag["abrechnungsmodus"] == "INTERN"
        assert eintrag["rechnung_id"] is None
        with datenbank() as conn:
            halb = conn.execute("SELECT netto_cent, version FROM rechnung WHERE id = ?",
                                (rid,)).fetchone()
        assert halb["netto_cent"] == voll["netto_cent"] // 2
        assert halb["version"] > voll["version"]


# ------------------------------------------------------ 13 Loeschen-Knopf

class TestLoeschenKnopf:
    def test_nur_fuer_aufgeschriebene_ohne_rechnungsbezug(self, klient, kunde, datenbank):
        frei = _zeit(klient, kunde, datenbank)
        gebunden = _zeit(klient, kunde, datenbank, datum="2026-03-03")
        rid = _entwurf(klient, kunde, [gebunden])
        text = klient.get("/zeiten").text
        assert f"/zeiten/{frei}/loeschen" in text
        assert f"/zeiten/{gebunden}/loeschen" not in text

        antwort = klient.post(f"/zeiten/{gebunden}/loeschen", follow_redirects=False)
        meldung = _meldung(antwort)
        assert "Entwurf" in meldung
        assert str(rid) in meldung
        assert "erst verwerfen oder abschreiben" in meldung
        assert "auf einer Rechnung" not in meldung
        assert _eintrag(datenbank, gebunden) is not None

    def test_festgeschriebene_zeit_meldet_die_nummer(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [zid])
        nummer = _festschreiben(klient, datenbank, rid)
        meldung = _meldung(klient.post(f"/zeiten/{zid}/loeschen", follow_redirects=False))
        assert nummer in meldung
        assert "nicht gelöscht" in meldung


# ------------------------------------------------------ 14 Kulanz sichtbar

class TestKulanzSichtbar:
    def test_chip_und_rueckweg(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": "Einarbeitung"})
        zeile = _zeile(klient, zid)
        assert '<span class="chip kulanz">Kulanz</span>' in zeile
        assert "wieder berechnen" in zeile
        assert 'value="STUNDEN"' in zeile
        assert 'value="KULANZ"' not in zeile, "Kulanz nicht nochmal anbieten"
        assert f"/zeiten/{zid}/abschreiben" in zeile

        antwort = klient.post(f"/zeiten/{zid}/modus", data={"modus": "STUNDEN"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"]
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["abrechnungsmodus"] == "STUNDEN"
        assert eintrag["grund"] is None
        assert eintrag["satz_cent_snapshot"] is None, "wieder der Satz beim Abrechnen"
        zeile = _zeile(klient, zid)
        assert '<span class="chip kulanz">' not in zeile
        assert "wieder berechnen" not in zeile
        assert 'value="KULANZ"' in zeile, "der Weg zur Kulanz ist wieder da"

    def test_kulanz_im_entwurf_laesst_sich_zurueckholen(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [zid])
        with datenbank() as conn:
            voll = conn.execute("SELECT netto_cent FROM rechnung WHERE id = ?",
                                (rid,)).fetchone()[0]
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": "Kulanz"})
        with datenbank() as conn:
            assert conn.execute("SELECT netto_cent FROM rechnung WHERE id = ?",
                                (rid,)).fetchone()[0] == 0
        assert "wieder berechnen" in _zeile(klient, zid)
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "STUNDEN"})
        with datenbank() as conn:
            assert conn.execute("SELECT netto_cent FROM rechnung WHERE id = ?",
                                (rid,)).fetchone()[0] == voll

    def test_intern_hat_rueckweg_und_loeschen(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "INTERN", "grund": "Akquise"})
        zeile = _zeile(klient, zid)
        assert "Nicht abrechenbar" in zeile
        assert "wieder berechnen" in zeile
        assert f"/zeiten/{zid}/loeschen" in zeile
        antwort = klient.post(f"/zeiten/{zid}/loeschen", follow_redirects=False)
        assert "fehler=" not in antwort.headers["location"]
        assert _eintrag(datenbank, zid) is None

    def test_kulanz_auf_der_rechnung_hat_keinen_doppelten_chip(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": "Kulanz"})
        # Eine Rechnung ueber 0,00 € laesst sich nicht festschreiben: eine
        # berechnete Stunde muss dazu.
        voll = _zeit(klient, kunde, datenbank, datum="2026-03-03", titel="Umsetzung")
        rid = _entwurf(klient, kunde, [zid, voll])
        _festschreiben(klient, datenbank, rid)
        text = klient.get("/zeiten").text
        assert "Ohne Berechnung gestellt" in text
        assert '<span class="chip kulanz">' not in text


# ------------------------------------------------- 15 Entwurf ohne Position

class TestEntwurfOhnePosition:
    def test_abschreiben_der_letzten_zeit_weist_auf_den_entwurf(self, klient, kunde, datenbank):
        a = _zeit(klient, kunde, datenbank)
        b = _zeit(klient, kunde, datenbank, datum="2026-03-03")
        rid = _entwurf(klient, kunde, [a, b])
        antwort = klient.post(f"/zeiten/{a}/abschreiben", data={"grund": "x"},
                              follow_redirects=False)
        assert "entwurf_leer" not in antwort.headers["location"], "eine Zeit ist noch drin"
        antwort = klient.post(f"/zeiten/{b}/abschreiben", data={"grund": "x"},
                              follow_redirects=False)
        assert _meldung(antwort, "entwurf_leer") == str(rid)
        seite = klient.get(antwort.headers["location"]).text
        assert "hat jetzt keine" in seite and "Position mehr — verwerfen?" in seite
        assert f'href="/rechnung/{rid}"' in seite

    def test_auch_intern_leert_den_entwurf(self, klient, kunde, datenbank):
        a = _zeit(klient, kunde, datenbank)
        rid = _entwurf(klient, kunde, [a])
        antwort = klient.post(f"/zeiten/{a}/modus", data={"modus": "INTERN", "grund": "x"},
                              follow_redirects=False)
        assert _meldung(antwort, "entwurf_leer") == str(rid)


# ----------------------------------------- 16 Nicht existierende Zeiten

class TestNichtExistierendeZeit:
    @pytest.mark.parametrize("pfad, felder", [
        ("/zeiten/9999/zuruecknehmen", {}),
        ("/zeiten/9999/modus", {"modus": "KULANZ", "grund": "x"}),
        ("/zeiten/9999/abschreiben", {"grund": "x"}),
        ("/zeiten/9999/loeschen", {}),
        ("/zeiten/9999/freigeben", {}),
    ])
    def test_meldung_statt_journalmuell(self, klient, kunde, datenbank, pfad, felder):
        antwort = klient.post(pfad, data=felder, follow_redirects=False)
        assert _meldung(antwort) == "Diesen Zeiteintrag gibt es nicht mehr."
        with datenbank() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM journal WHERE objekttyp = 'zeiteintrag' AND objekt_id = 9999"
            ).fetchone()[0] == 0

    def test_zuruecknehmen_einer_nicht_abgeschriebenen_zeit(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/zuruecknehmen", follow_redirects=False)
        assert _meldung(antwort) == "Diese Zeit ist nicht abgeschrieben."
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["version"] == 1, "kein Versionssprung ohne Aenderung"


# ------------------------------------------------------- 17 Darstellung

class TestDarstellung:
    def test_zeilenumbrueche_und_lange_woerter(self, klient, kunde, datenbank):
        _zeit(klient, kunde, datenbank, beschreibung="Zeile eins\nZeile zwei")
        text = klient.get("/zeiten").text
        assert '<span class="gedaempft beschreibung">Zeile eins\nZeile zwei</span>' in text
        css = Path(__file__).resolve().parent.parent / "app" / "static" / "stil.css"
        stil = css.read_text(encoding="utf-8")
        assert re.search(r"\.beschreibung\s*\{\s*white-space:\s*pre-line", stil)
        # "break-word" statt "anywhere": lange Woerter brechen weiter um, aber
        # nur, wenn sie allein nicht in die Zeile passen -- "anywhere" zerriss
        # auch Daten und Namen ("19.10.202|6"), Befund der Browserpruefung.
        assert re.search(
            r"td:not\(\.zahl\):not\(\.mono\):not\(\.aktionen\)\s*\{\s*overflow-wrap:\s*break-word",
            stil,
        )


# ------------------------------------------------ 18 Katalog: Pluralform

class TestKatalogPlural:
    def test_singular_bei_einem_titel(self, klient, kunde, datenbank):
        _zeit(klient, kunde, datenbank)
        with datenbank() as conn:
            lid = conn.execute("SELECT id FROM leistung").fetchone()[0]
        klient.post(f"/leistungen/{lid}", data={"titel": "Consulting", "einheit": "HUR",
                                                 "aktiv": "1"})
        text = klient.get("/leistungen").text
        assert "1 benutzter Titel steht nicht im Katalog." in text
        assert "benutzte Titel stehen" not in text

    def test_plural_ab_zwei(self, klient, kunde, datenbank):
        with datenbank() as conn:
            conn.executemany(
                "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
                " VALUES (?, ?, '2026-01-02', 60, ?)",
                [(kunde, "Alt eins", db.jetzt_utc()), (kunde, "Alt zwei", db.jetzt_utc())],
            )
            conn.commit()
        assert "2 benutzte Titel stehen nicht im Katalog." in klient.get("/leistungen").text

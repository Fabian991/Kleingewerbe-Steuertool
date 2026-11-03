"""Zeiterfassung, Version 21: Pause, Beschreibung beim Timer, Rundung je Kunde.

Drei Wuensche aus dem Alltag, ein roter Faden: die Aufzeichnung soll belegen,
WANN gearbeitet wurde (Arbeitsabschnitte statt behaupteter Pausenlaenge), WAS
getan wurde (Beschreibung auch beim Timer, mehrzeilig) und WIE abgerechnet
wird (Rundung je Rechnungsposition nach der Regel des Kunden -- sichtbar,
bevor die Rechnung entsteht).
"""

from __future__ import annotations

import datetime as _dt
import html
import re
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import festschreiben as fs
from app.logik import gruppierung, kunden, rundung
from app.logik import zeiten as logik
from app.main import erzeuge_app

BERLIN = ZoneInfo("Europe/Berlin")

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}
KUNDE = {
    "name": "Beispiel GmbH", "strasse": "Musterstraße 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


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
    if antwort.status_code == 422 and name == "fehler":
        treffer = re.search(r'<div class="meldung fehler">(.*?)</div>', antwort.text, re.S)
        assert treffer, antwort.text[:300]
        return html.unescape(treffer.group(1).strip())
    assert antwort.status_code == 303, antwort.text[:300]
    query = parse_qs(urlsplit(antwort.headers["location"]).query)
    return unquote(query.get(name, [""])[0])


def _vor(minuten: float) -> str:
    """Ein UTC-Zeitpunkt ``minuten`` vor jetzt, im Format der Abschnitte."""
    return logik.utc_text(_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=minuten))


def _timer(klient, kunde: int, datenbank, titel: str = "Support", **felder) -> int:
    antwort = klient.post("/zeiten/timer/start",
                          data={"kunde_id": str(kunde), "titel": titel, **felder},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:300]
    with datenbank() as conn:
        return conn.execute(
            "SELECT id FROM zeiteintrag WHERE end_zeit IS NULL AND start_zeit IS NOT NULL"
        ).fetchone()[0]


def _abschnitte_setzen(datenbank, zeit_id: int, teile, *, start: bool = False) -> None:
    """Ersetzt die Abschnitte: je (Beginn, Ende) in Minuten vor jetzt, Ende None = offen.

    ``start``: Datum und Uhrzeit des Eintrags folgen dem ersten Beginn -- so
    wie der Timer sie beim Start geschrieben haette.
    """
    with datenbank() as conn:
        conn.execute("DELETE FROM zeit_abschnitt WHERE zeiteintrag_id = ?", (zeit_id,))
        for beginn, ende in teile:
            conn.execute(
                "INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                " VALUES (?, ?, ?)",
                (zeit_id, _vor(beginn), None if ende is None else _vor(ende)),
            )
        if start:
            erster = logik.ortszeit(_vor(teile[0][0])).replace(tzinfo=None)
            conn.execute("UPDATE zeiteintrag SET datum = ?, start_zeit = ? WHERE id = ?",
                         (erster.date().isoformat(), erster.strftime("%H:%M:%S"), zeit_id))
        conn.commit()


def _abschnitte(datenbank, zeit_id: int) -> list[sqlite3.Row]:
    with datenbank() as conn:
        return conn.execute(
            "SELECT * FROM zeit_abschnitt WHERE zeiteintrag_id = ? ORDER BY beginn_utc",
            (zeit_id,),
        ).fetchall()


def _eintrag(datenbank, zeit_id: int) -> sqlite3.Row | None:
    with datenbank() as conn:
        return conn.execute("SELECT * FROM zeiteintrag WHERE id = ?", (zeit_id,)).fetchone()


def _journal(datenbank, aktion: str, zeit_id: int | None = None) -> list[sqlite3.Row]:
    with datenbank() as conn:
        if zeit_id is None:
            return conn.execute("SELECT * FROM journal WHERE aktion = ?", (aktion,)).fetchall()
        return conn.execute(
            "SELECT * FROM journal WHERE aktion = ? AND objekt_id = ?", (aktion, zeit_id)
        ).fetchall()


def _zeit(klient, kunde: int, datenbank, **felder) -> int:
    daten = {"kunde_id": str(kunde), "titel": "Beratung", "datum": "2026-03-02",
             "dauer": "60", **felder}
    antwort = klient.post("/zeiten", data=daten, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:300]
    with datenbank() as conn:
        return conn.execute("SELECT MAX(id) FROM zeiteintrag").fetchone()[0]


def _entwurf(klient, kunde: int, zeit_ids: list[int], modus: str = "PRO_TITEL") -> int:
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": str(kunde), "datum": "2026-03-10", "modus": modus,
        "zeit": [str(z) for z in zeit_ids], "leistungszeitraum": "1",
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:300]
    return int(antwort.headers["location"].rsplit("/", 1)[1].split("?")[0])


def _festschreiben(klient, datenbank, rechnung_id: int) -> str:
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


def _kundenregel(klient, kunde: int, **felder) -> None:
    antwort = klient.post(f"/kunden/{kunde}", data={**KUNDE, **felder},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:500]


# ====================================================== Logik: Abschnitte

class TestAbschnitteRechnen:
    def test_zeitumstellung_25_oktober(self):
        """01:30 bis 03:30 am 25.10.2026 sind 180 Minuten -- die Uhr wird um
        drei auf zwei zurueckgestellt. Mit naiver Ortszeit waren es 120."""
        beginn = logik.ortszeit_als_utc("2026-10-25", "01:30:00", BERLIN)
        ende = logik.ortszeit_als_utc("2026-10-25", "03:30:00", BERLIN)
        assert (beginn, ende) == ("2026-10-24T23:30:00+00:00", "2026-10-25T02:30:00+00:00")
        sekunden = logik.netto_sekunden([logik.Abschnitt(beginn, ende)], ende)
        assert logik.minuten_aus_sekunden(sekunden) == 180

    def test_laufender_timer_ueber_die_zeitumstellung(self, conn, kunde_id):
        """Auch ein Timer aus Altbestand (ohne Abschnitte) rechnet in UTC."""
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, start_zeit, dauer_minuten,"
            " erfasst_am) VALUES (?, 'Nacht', '2026-10-25', '01:30:00', 1, ?)",
            (kunde_id, db.jetzt_utc()),
        )
        timer = logik.laufender_timer(conn, jetzt="03:30", heute="2026-10-25", zone=BERLIN)
        assert timer.minuten == 180

    def test_ueber_mitternacht(self):
        beginn = logik.ortszeit_als_utc("2026-10-12", "23:30:00", BERLIN)
        ende = logik.ortszeit_als_utc("2026-10-13", "00:45:00", BERLIN)
        assert logik.netto_sekunden([logik.Abschnitt(beginn, ende)], ende) == 75 * 60
        assert logik.abschnitte_anzeige([logik.Abschnitt(beginn, ende)], BERLIN) \
            == "23:30–00:45 (+1 Tag)"

    def test_gerundet_wird_auf_die_summe_nicht_je_abschnitt(self):
        """Vier Abschnitte zu 40 Sekunden sind 160 Sekunden: drei Minuten, nicht vier."""
        teile = [
            logik.Abschnitt(f"2026-03-02T09:0{i}:00+00:00", f"2026-03-02T09:0{i}:40+00:00")
            for i in range(4)
        ]
        sekunden = logik.netto_sekunden(teile, "2026-03-02T10:00:00+00:00")
        assert sekunden == 160
        assert logik.minuten_aus_sekunden(sekunden) == 3

    def test_pausen_sind_die_luecken(self):
        teile = [
            logik.Abschnitt("2026-03-02T09:00:00+00:00", "2026-03-02T10:00:00+00:00"),
            logik.Abschnitt("2026-03-02T10:25:00+00:00", "2026-03-02T11:00:00+00:00"),
        ]
        jetzt = "2026-03-02T11:30:00+00:00"
        assert logik.netto_sekunden(teile, jetzt) == 95 * 60
        # Gestoppt: die Pause endet mit dem letzten Abschnitt.
        assert logik.pause_sekunden(teile, jetzt, offen=False) == 25 * 60
        # Noch offen (pausiert): die laufende Pause zaehlt bis jetzt mit.
        assert logik.pause_sekunden(teile, jetzt, offen=True) == 55 * 60
        assert logik.abschnitte_anzeige(teile, _dt.UTC) == "09:00–10:00, 10:25–11:00"

    def test_offener_abschnitt_in_der_anzeige(self):
        teile = [logik.Abschnitt("2026-03-02T09:02:00+00:00")]
        assert logik.abschnitte_anzeige(teile, _dt.UTC) == "09:02–…"

    def test_pausierter_timer(self, conn, kunde_id):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum, start_zeit,"
            " dauer_minuten, erfasst_am) VALUES (?, 'Support', 'Ticket 7', ?, '09:00:00', 1, ?)",
            (kunde_id, db.heute(), db.jetzt_utc()),
        ).lastrowid
        beginn = logik.ortszeit_als_utc(db.heute(), "09:00:00")
        ende = logik.ortszeit_als_utc(db.heute(), "10:13:00")
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                     " VALUES (?, ?, ?)", (zid, beginn, ende))
        timer = logik.laufender_timer(conn, jetzt="10:38", heute=db.heute())
        assert timer.pausiert is True
        assert timer.minuten == 73
        assert timer.pause_minuten == 25
        assert timer.pause_seit == "10:13"
        assert timer.beschreibung == "Ticket 7"


class TestBeschreibungPruefen:
    def test_crlf_und_leerzeilen(self):
        assert logik.pruefe_beschreibung("Eins\r\nZwei\r\n\r\n\r\n\r\nDrei  ") \
            == "Eins\nZwei\n\nDrei"

    def test_leer_ist_none(self):
        assert logik.pruefe_beschreibung(" \r\n ") is None
        assert logik.pruefe_beschreibung(None) is None

    def test_grenze_nach_dem_normalisieren(self):
        """CRLF zaehlt als ein Zeichen -- wie maxlength im Browser."""
        text = "x\r\n" * 1000  # 3000 Zeichen roh, 1999 normalisiert
        assert len(logik.pruefe_beschreibung(text)) == 1999
        with pytest.raises(logik.BeschreibungUngueltig, match="höchstens 2.000"):
            logik.pruefe_beschreibung("x" * 2001)


# ==================================================== Schema: Abschnitte

def _laufend(conn, kunde_id) -> int:
    return conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, start_zeit, dauer_minuten, erfasst_am)"
        " VALUES (?, 'Lauf', '2026-03-02', '09:00:00', 1, ?)", (kunde_id, db.jetzt_utc()),
    ).lastrowid


class TestSchemaAbschnitte:
    def test_hoechstens_ein_offener_abschnitt(self, conn, kunde_id):
        zid = _laufend(conn, kunde_id)
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc)"
                     " VALUES (?, '2026-03-02T09:00:00+00:00')", (zid,))
        with pytest.raises(sqlite3.IntegrityError, match="bereits ein Arbeitsabschnitt"):
            conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc)"
                         " VALUES (?, '2026-03-02T10:00:00+00:00')", (zid,))

    def test_offener_abschnitt_nur_am_offenen_eintrag(self, conn, kunde_id):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Fertig', '2026-03-02', 30, ?)", (kunde_id, db.jetzt_utc()),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="laufenden Timer"):
            conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc)"
                         " VALUES (?, '2026-03-02T09:00:00+00:00')", (zid,))
        # Geschlossen darf er stehen -- so sieht ein gestoppter Timer aus.
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                     " VALUES (?, '2026-03-02T09:00:00+00:00', '2026-03-02T09:30:00+00:00')",
                     (zid,))

    def test_ende_nicht_vor_beginn(self, conn, kunde_id):
        zid = _laufend(conn, kunde_id)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                         " VALUES (?, '2026-03-02T10:00:00+00:00', '2026-03-02T09:00:00+00:00')",
                         (zid,))

    def test_pausierter_timer_blockiert_einen_zweiten(self, conn, kunde_id):
        """Pausiert heisst: kein offener Abschnitt, aber der Eintrag ist offen --
        er bleibt DER eine Timer."""
        zid = _laufend(conn, kunde_id)
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                     " VALUES (?, '2026-03-02T09:00:00+00:00', '2026-03-02T09:30:00+00:00')",
                     (zid,))
        with pytest.raises(sqlite3.IntegrityError, match="bereits ein Timer"):
            _laufend(conn, kunde_id)

    def test_auf_festgeschriebener_rechnung_gesperrt(self, conn, kunde_id):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, erfasst_am)"
            " VALUES (?, 'Abgerechnet', '2026-03-02', 30, ?)", (kunde_id, db.jetzt_utc()),
        ).lastrowid
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                     " VALUES (?, '2026-03-02T09:00:00+00:00', '2026-03-02T09:30:00+00:00')",
                     (zid,))
        rid = conn.execute(
            "INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,"
            " netto_cent, brutto_cent, status, festgeschrieben_am, erstellt_am)"
            " VALUES ('RECHNUNG', 'RE-1', 1, 2026, ?, '2026-03-10', 4500, 4500,"
            " 'FESTGESCHRIEBEN', ?, ?)", (kunde_id, db.jetzt_utc(), db.jetzt_utc()),
        ).lastrowid
        conn.execute("UPDATE zeiteintrag SET rechnung_id = ? WHERE id = ?", (rid, zid))
        with pytest.raises(sqlite3.IntegrityError, match="nur ueber Storno"):
            conn.execute("UPDATE zeit_abschnitt SET beginn_utc = '2026-03-02T08:00:00+00:00'")
        with pytest.raises(sqlite3.IntegrityError, match="nur ueber Storno"):
            conn.execute("DELETE FROM zeit_abschnitt")
        with pytest.raises(sqlite3.IntegrityError, match="nur ueber Storno"):
            conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                         " VALUES (?, '2026-03-02T11:00:00+00:00', '2026-03-02T11:30:00+00:00')",
                         (zid,))
        # Titel und Beschreibung gehoeren seit Version 21 dazu.
        with pytest.raises(sqlite3.IntegrityError, match="nur ueber Storno"):
            conn.execute("UPDATE zeiteintrag SET titel = 'Anders' WHERE id = ?", (zid,))
        with pytest.raises(sqlite3.IntegrityError, match="nur ueber Storno"):
            conn.execute("UPDATE zeiteintrag SET beschreibung = 'neu' WHERE id = ?", (zid,))

    def test_offenen_abschnitt_darf_das_stoppen_schliessen(self, conn, kunde_id):
        """Wie end_zeit: ein Timer aus Altbestand auf einer festen Rechnung muss
        sich stoppen lassen, sonst blockiert er jeden neuen."""
        zid = _laufend(conn, kunde_id)
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc)"
                     " VALUES (?, '2026-03-02T09:00:00+00:00')", (zid,))
        rid = conn.execute(
            "INSERT INTO rechnung (typ, nummer, nummer_seq, nummer_jahr, kunde_id, datum,"
            " netto_cent, brutto_cent, status, festgeschrieben_am, erstellt_am)"
            " VALUES ('RECHNUNG', 'RE-1', 1, 2026, ?, '2026-03-10', 4500, 4500,"
            " 'FESTGESCHRIEBEN', ?, ?)", (kunde_id, db.jetzt_utc(), db.jetzt_utc()),
        ).lastrowid
        conn.execute("UPDATE zeiteintrag SET rechnung_id = ? WHERE id = ?", (rid, zid))
        conn.execute("UPDATE zeit_abschnitt SET ende_utc = '2026-03-02T10:00:00+00:00'")

    def test_titel_im_entwurf_bleibt_aenderbar(self, conn, kunde_id):
        zid = _laufend(conn, kunde_id)
        conn.execute("UPDATE zeiteintrag SET end_zeit = '10:00:00' WHERE id = ?", (zid,))
        rid = conn.execute(
            "INSERT INTO rechnung (kunde_id, datum, status, erstellt_am)"
            " VALUES (?, '2026-03-10', 'ENTWURF', ?)", (kunde_id, db.jetzt_utc()),
        ).lastrowid
        conn.execute("UPDATE zeiteintrag SET rechnung_id = ? WHERE id = ?", (rid, zid))
        conn.execute("UPDATE zeiteintrag SET titel = 'Anders', beschreibung = 'x' WHERE id = ?",
                     (zid,))

    def test_loeschen_nimmt_die_abschnitte_mit(self, conn, kunde_id):
        zid = _laufend(conn, kunde_id)
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc)"
                     " VALUES (?, '2026-03-02T09:00:00+00:00')", (zid,))
        conn.execute("DELETE FROM zeiteintrag WHERE id = ?", (zid,))
        assert conn.execute("SELECT COUNT(*) FROM zeit_abschnitt").fetchone()[0] == 0


# ======================================================== Timer: Pause

class TestPause:
    def test_start_legt_den_ersten_abschnitt_an(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        [abschnitt] = _abschnitte(datenbank, zid)
        assert abschnitt["ende_utc"] is None
        beginn = _dt.datetime.fromisoformat(abschnitt["beginn_utc"])
        assert abs((_dt.datetime.now(_dt.UTC) - beginn).total_seconds()) < 10
        # Datum und Uhrzeit des Eintrags sind die Ortszeit desselben Zeitpunkts.
        eintrag = _eintrag(datenbank, zid)
        lokal = logik.ortszeit(abschnitt["beginn_utc"]).replace(tzinfo=None)
        assert (eintrag["datum"], eintrag["start_zeit"]) == (
            lokal.date().isoformat(), lokal.strftime("%H:%M:%S"))

    def test_pause_weiter_stopp_ergibt_die_arbeitszeit(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        antwort = klient.post("/zeiten/timer/pause", follow_redirects=False)
        assert _meldung(antwort, "erfolg").startswith("Pause ab ")
        assert len(_abschnitte(datenbank, zid)) == 1
        assert _abschnitte(datenbank, zid)[0]["ende_utc"] is not None
        antwort = klient.post("/zeiten/timer/weiter", follow_redirects=False)
        assert _meldung(antwort, "erfolg").startswith("Weiter ab ")
        assert [a["ende_utc"] is None for a in _abschnitte(datenbank, zid)] == [False, True]
        assert len(_journal(datenbank, "TIMER_PAUSE", zid)) == 1
        assert len(_journal(datenbank, "TIMER_WEITER", zid)) == 1

        # Zwei Stunden mit einer halben Stunde Pause: 60 + 30 Minuten Arbeit.
        _abschnitte_setzen(datenbank, zid, [(120, 60), (30, None)])
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert _meldung(antwort, "erfolg").startswith("Gestoppt: 1:30 erfasst (Support)")
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["dauer_minuten"] == 90
        assert eintrag["end_zeit"] is not None
        assert all(a["ende_utc"] is not None for a in _abschnitte(datenbank, zid))

    def test_stopp_aus_der_pause_endet_mit_dem_pausenbeginn(self, klient, kunde, datenbank):
        """Nicht der Klick auf „Stoppen“ ist das Ende, sondern der Beginn der Pause."""
        zid = _timer(klient, kunde, datenbank)
        _abschnitte_setzen(datenbank, zid, [(60, 20)], start=True)
        pause_ab = _abschnitte(datenbank, zid)[0]["ende_utc"]
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert _meldung(antwort, "erfolg").startswith("Gestoppt: 0:40 erfasst")
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["dauer_minuten"] == 40
        assert _abschnitte(datenbank, zid)[0]["ende_utc"] == pause_ab, "Abschnitt unveraendert"
        assert eintrag["end_zeit"] == logik.ende_als_endzeit(
            eintrag["datum"], eintrag["start_zeit"], pause_ab)
        [stopp] = _journal(datenbank, "TIMER_STOP", zid)
        assert "Beginn der Pause" in stopp["begruendung"]

    def test_doppelklick_auf_pause(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        klient.post("/zeiten/timer/pause", follow_redirects=False)
        antwort = klient.post("/zeiten/timer/pause", follow_redirects=False)
        assert _meldung(antwort, "hinweis").startswith("Der Timer ist bereits pausiert")
        assert _meldung(antwort) == ""
        assert len(_journal(datenbank, "TIMER_PAUSE", zid)) == 1
        assert len(_abschnitte(datenbank, zid)) == 1

    def test_fortsetzen_eines_laufenden_timers(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        antwort = klient.post("/zeiten/timer/weiter", follow_redirects=False)
        assert _meldung(antwort, "hinweis") == "Der Timer läuft bereits."
        assert len(_abschnitte(datenbank, zid)) == 1

    def test_pause_ohne_timer(self, klient, kunde):
        antwort = klient.post("/zeiten/timer/pause", follow_redirects=False)
        assert _meldung(antwort, "hinweis") == "Es läuft kein Timer."

    def test_neuer_timer_beendet_den_pausierten_mit_dem_pausenbeginn(
        self, klient, kunde, datenbank
    ):
        alt = _timer(klient, kunde, datenbank, titel="Erste Sache")
        _abschnitte_setzen(datenbank, alt, [(60, 30)], start=True)
        neu = _timer(klient, kunde, datenbank, titel="Zweite Sache")
        assert neu != alt
        assert _eintrag(datenbank, alt)["dauer_minuten"] == 30
        assert _eintrag(datenbank, alt)["end_zeit"] is not None
        with datenbank() as conn:
            offen = conn.execute(
                "SELECT zeiteintrag_id FROM zeit_abschnitt WHERE ende_utc IS NULL"
            ).fetchall()
        assert [z[0] for z in offen] == [neu]

    def test_fortsetzen_an_einem_neuen_tag_legt_einen_neuen_eintrag_an(
        self, klient, kunde, datenbank
    ):
        """Abends pausiert, morgens fortgesetzt: die Arbeit von heute steht unter heute."""
        zid = _timer(klient, kunde, datenbank, titel="Konzept", beschreibung="Gliederung")
        gestern = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
        with datenbank() as conn:
            conn.execute("DELETE FROM zeit_abschnitt WHERE zeiteintrag_id = ?", (zid,))
            conn.execute("UPDATE zeiteintrag SET datum = ?, start_zeit = '17:00:00' WHERE id = ?",
                         (gestern, zid))
            conn.execute(
                "INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                " VALUES (?, ?, ?)",
                (zid, logik.ortszeit_als_utc(gestern, "17:00:00"),
                 logik.ortszeit_als_utc(gestern, "18:30:00")),
            )
            conn.commit()
        antwort = klient.post("/zeiten/timer/weiter", follow_redirects=False)
        meldung = _meldung(antwort, "erfolg")
        assert meldung.startswith("Weiter als neuer Eintrag für heute.")
        assert "endet mit der Pause um 18:30 (1:30 erfasst)" in meldung

        alt = _eintrag(datenbank, zid)
        assert (alt["end_zeit"], alt["dauer_minuten"]) == ("18:30:00", 90)
        with datenbank() as conn:
            neu = conn.execute(
                "SELECT * FROM zeiteintrag WHERE end_zeit IS NULL AND start_zeit IS NOT NULL"
            ).fetchone()
        assert neu["id"] != zid
        assert (neu["datum"], neu["titel"], neu["beschreibung"], neu["kunde_id"]) == (
            _dt.date.today().isoformat(), "Konzept", "Gliederung", kunde)
        [abschnitt] = _abschnitte(datenbank, neu["id"])
        assert abschnitt["ende_utc"] is None
        [weiter] = _journal(datenbank, "TIMER_WEITER", neu["id"])
        assert f'"fortsetzung_von": {zid}' in weiter["wert_neu"]

    def test_pause_auf_festgeschriebenem_alt_timer_meldet_statt_500(
        self, klient, kunde, datenbank
    ):
        """Altbestand: eine Zeit, die nachtraeglich als laufender Timer dasteht,
        liegt auf einer festgeschriebenen Rechnung. Pausieren laesst sich das
        nicht -- es gibt eine Meldung, keinen Serverfehler."""
        zid = _zeit(klient, kunde, datenbank, titel="Laufend")
        rid = _entwurf(klient, kunde, [zid])
        with datenbank() as conn:
            conn.execute("UPDATE zeiteintrag SET start_zeit = '08:00:00', end_zeit = NULL,"
                         " dauer_minuten = 1 WHERE id = ?", (zid,))
            conn.commit()
        nummer = _festschreiben(klient, datenbank, rid)
        antwort = klient.post("/zeiten/timer/pause", follow_redirects=False)
        meldung = _meldung(antwort)
        assert nummer in meldung and "Storno" in meldung
        assert _abschnitte(datenbank, zid) == []
        # Stoppen geht weiter (Ende gesetzt, Dauer bleibt).
        klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert _eintrag(datenbank, zid)["end_zeit"] is not None
        assert _eintrag(datenbank, zid)["dauer_minuten"] == 1

    def test_banner_und_liste_zeigen_die_pause(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        _abschnitte_setzen(datenbank, zid, [(60, 30)])
        seite = klient.get("/zeiten").text
        assert "Pausiert seit" in seite
        assert "Läuft seit" not in seite
        assert 'formaction="/zeiten/timer/weiter"' in seite
        assert 'formaction="/zeiten/timer/pause"' not in seite
        assert '<span class="chip laufend">pausiert</span>' in seite
        assert re.search(r'netto <span class="mono">0:30</span>', seite)
        assert re.search(r'Pause\s*<span class="mono">0:30</span>', seite)
        teile = logik.abschnitte_teile(
            [logik.Abschnitt(a["beginn_utc"], a["ende_utc"]) for a in _abschnitte(datenbank, zid)]
        )
        # Im Banner als Zeile, in der Liste je Abschnitt ohne Umbruch.
        assert f"Gearbeitet: {', '.join(teile)}" in seite
        assert f'<span class="abschnitt">{teile[0]}</span>' in seite

        klient.post("/zeiten/timer/weiter", follow_redirects=False)
        seite = klient.get("/zeiten").text
        assert "Läuft seit" in seite
        assert 'formaction="/zeiten/timer/pause"' in seite
        assert '<span class="chip laufend">läuft</span>' in seite
        assert 'id="timer"' in seite and "setInterval(zeigen, 60000)" in seite

    def test_dauer_aendern_mit_pausen_laesst_das_ende_stehen(self, klient, kunde, datenbank):
        """Mit Pausen gilt „Ende = Beginn + Dauer“ nicht: das Ende bleibt, die
        Abschnitte bleiben als Aufzeichnung, und die Liste sagt, dass die Dauer
        korrigiert ist."""
        zid = _timer(klient, kunde, datenbank)
        _abschnitte_setzen(datenbank, zid, [(120, 60), (30, None)], start=True)
        klient.post("/zeiten/timer/stop", follow_redirects=False)
        vorher = _eintrag(datenbank, zid)
        assert vorher["dauer_minuten"] == 90
        antwort = klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "2:00"},
                              follow_redirects=False)
        assert "Dauer geändert" in _meldung(antwort, "erfolg")
        nachher = _eintrag(datenbank, zid)
        assert nachher["dauer_minuten"] == 120
        assert nachher["end_zeit"] == vorher["end_zeit"]
        assert len(_abschnitte(datenbank, zid)) == 2
        seite = klient.get("/zeiten").text
        assert "korrigiert, die Abschnitte ergeben\n          1:30" in seite
        assert "(Pause 0:30)" in seite

    def test_abbruch_loescht_die_abschnitte(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        klient.post("/zeiten/timer/pause", follow_redirects=False)
        antwort = klient.post(f"/zeiten/{zid}/loeschen", follow_redirects=False)
        assert "fehler=" not in antwort.headers["location"]
        assert _eintrag(datenbank, zid) is None
        assert _abschnitte(datenbank, zid) == []

    def test_pausierter_timer_ist_nicht_abrechenbar(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        klient.post("/zeiten/timer/pause", follow_redirects=False)
        seite = klient.get(f"/rechnung/neu?kunde={kunde}").text
        # Genau das Auswahlfeld der Zeit -- "value="1"" allein traefe auch
        # die versteckten Felder des Formulars "Rechnung ohne Zeiten".
        assert f'name="zeit" value="{zid}"' not in seite


# ======================================================= Beschreibung

class TestBeschreibung:
    def test_timer_start_mit_beschreibung(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank, beschreibung="Zeile 1\r\nZeile 2")
        assert _eintrag(datenbank, zid)["beschreibung"] == "Zeile 1\nZeile 2"
        seite = klient.get("/zeiten").text
        banner = seite.split('id="timer"', 1)[1].split("</form>", 1)[0]
        assert re.search(r'<textarea name="beschreibung" rows="3" maxlength="2000">'
                         r"Zeile 1\nZeile 2</textarea>", banner)

    def test_abgewiesener_start_behaelt_die_beschreibung(self, klient, kunde):
        antwort = klient.post("/zeiten/timer/start", data={
            "kunde_id": str(kunde), "titel": " ", "beschreibung": "Mein Text",
        }, follow_redirects=False)
        assert antwort.status_code == 422
        assert ">Mein Text</textarea>" in antwort.text

    def test_zu_lange_beschreibung_wird_abgewiesen(self, klient, kunde, datenbank):
        lang = "Wort " * 450
        for ziel, daten in (
            ("/zeiten", {"titel": "Beratung", "datum": "2026-03-02", "dauer": "60"}),
            ("/zeiten/timer/start", {"titel": "Beratung"}),
        ):
            antwort = klient.post(ziel, data={"kunde_id": str(kunde), "beschreibung": lang,
                                              **daten}, follow_redirects=False)
            assert antwort.status_code == 422, ziel
            assert "höchstens 2.000 sind möglich" in _meldung(antwort)
            assert f">{lang}</textarea>" in antwort.text, "die Eingabe bleibt stehen"
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0

    def test_erfassen_normalisiert_zeilenenden(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank, beschreibung="A\r\n\r\n\r\nB\r\n")
        assert _eintrag(datenbank, zid)["beschreibung"] == "A\n\nB"

    def test_mehrzeilige_felder_ueberall(self, klient, kunde, datenbank):
        _zeit(klient, kunde, datenbank)
        seite = klient.get("/zeiten").text
        erfassen = seite.split('id="erfassen"', 1)[1].split("</form>", 1)[0]
        start = seite.split('id="timer-start"', 1)[1].split("</form>", 1)[0]
        for teil in (erfassen, start):
            assert re.search(r'<textarea name="beschreibung" rows="\d" maxlength="2000"', teil)
            assert 'input name="beschreibung"' not in teil
        assert "<summary>Text ändern</summary>" in seite
        css = (db.PROJEKT_WURZEL / "app" / "static" / "stil.css").read_text(encoding="utf-8")
        assert 'form.zeile textarea[name="beschreibung"] { flex: 1 1 100%' in css
        assert re.search(r"textarea \{ resize: vertical", css)

    def test_stoppen_speichert_die_beschreibung_in_einem_zug(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank, beschreibung="Anfang")
        antwort = klient.post("/zeiten/timer/stop",
                              data={"beschreibung": "Anfang\r\nund Ende"},
                              follow_redirects=False)
        assert _meldung(antwort, "erfolg").startswith("Gestoppt:")
        eintrag = _eintrag(datenbank, zid)
        assert eintrag["beschreibung"] == "Anfang\nund Ende"
        assert eintrag["end_zeit"] is not None
        [aenderung] = _journal(datenbank, "AENDERN", zid)
        assert (aenderung["feld"], aenderung["wert_alt"], aenderung["wert_neu"]) == (
            "beschreibung", "Anfang", "Anfang\nund Ende")

    def test_stoppen_ohne_feld_laesst_die_beschreibung(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank, beschreibung="Bleibt")
        klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert _eintrag(datenbank, zid)["beschreibung"] == "Bleibt"
        assert _journal(datenbank, "AENDERN", zid) == []

    def test_pause_nimmt_die_beschreibung_mit(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        klient.post("/zeiten/timer/pause", data={"beschreibung": "Zwischenstand"},
                    follow_redirects=False)
        assert _eintrag(datenbank, zid)["beschreibung"] == "Zwischenstand"

    def test_zu_lange_beschreibung_beim_stoppen_stoppt_nicht(self, klient, kunde, datenbank):
        zid = _timer(klient, kunde, datenbank)
        antwort = klient.post("/zeiten/timer/stop", data={"beschreibung": "x" * 2001},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "höchstens 2.000" in _meldung(antwort)
        assert _eintrag(datenbank, zid)["end_zeit"] is None, "der Timer laeuft weiter"
        assert "x" * 2001 + "</textarea>" in antwort.text


class TestTextAendern:
    def test_titel_und_beschreibung(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank, beschreibung="alt")
        version = _eintrag(datenbank, zid)["version"]
        antwort = klient.post(f"/zeiten/{zid}/bearbeiten", data={
            "titel": "Workshop", "beschreibung": "neu\r\nzweite Zeile",
            "version": str(version),
        }, follow_redirects=False)
        assert _meldung(antwort, "erfolg") == "Titel und Beschreibung gespeichert (Workshop)."
        eintrag = _eintrag(datenbank, zid)
        assert (eintrag["titel"], eintrag["beschreibung"]) == ("Workshop", "neu\nzweite Zeile")
        felder = {(z["feld"], z["wert_alt"], z["wert_neu"])
                  for z in _journal(datenbank, "AENDERN", zid)}
        assert felder == {("titel", "Beratung", "Workshop"),
                          ("beschreibung", "alt", "neu\nzweite Zeile")}
        with datenbank() as conn:
            assert conn.execute("SELECT 1 FROM leistung WHERE titel = 'Workshop'").fetchone()

    def test_ohne_aenderung(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank, beschreibung="gleich")
        antwort = klient.post(f"/zeiten/{zid}/bearbeiten", data={
            "titel": "Beratung", "beschreibung": "gleich"}, follow_redirects=False)
        assert _meldung(antwort, "hinweis") == "Nichts geändert."
        assert _journal(datenbank, "AENDERN", zid) == []

    def test_im_entwurf_wird_neu_berechnet(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank, beschreibung="vorher")
        rid = _entwurf(klient, kunde, [zid])
        with datenbank() as conn:
            version = conn.execute("SELECT version FROM rechnung WHERE id = ?",
                                   (rid,)).fetchone()[0]
        antwort = klient.post(f"/zeiten/{zid}/bearbeiten", data={
            "beschreibung": "nachher"}, follow_redirects=False)
        assert f"Der Entwurf (Rechnung {rid}) ist neu berechnet." in _meldung(antwort, "erfolg")
        with datenbank() as conn:
            position = conn.execute("SELECT untertext FROM rechnungsposition"
                                    " WHERE rechnung_id = ?", (rid,)).fetchone()
            neu = conn.execute("SELECT version FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        assert position["untertext"] == "nachher"
        assert neu > version, "ein offenes Festschreib-Formular muss veraltet sein"

    def test_auf_festgeschriebener_rechnung_gesperrt(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank, beschreibung="versandt")
        rid = _entwurf(klient, kunde, [zid])
        nummer = _festschreiben(klient, datenbank, rid)
        antwort = klient.post(f"/zeiten/{zid}/bearbeiten", data={
            "titel": "Anders", "beschreibung": "anders"}, follow_redirects=False)
        meldung = _meldung(antwort)
        assert nummer in meldung and "Storno" in meldung
        assert _eintrag(datenbank, zid)["beschreibung"] == "versandt"
        assert f"/zeiten/{zid}/bearbeiten" not in klient.get("/zeiten").text

    def test_veraltete_seite(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/bearbeiten", data={
            "titel": "Anders", "version": "99"}, follow_redirects=False)
        assert "inzwischen geändert" in _meldung(antwort)
        assert _eintrag(datenbank, zid)["titel"] == "Beratung"

    def test_leerer_titel_zeigt_das_formular_wieder(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/bearbeiten", data={
            "titel": "  ", "beschreibung": "behalten"}, follow_redirects=False)
        assert antwort.status_code == 422
        assert _meldung(antwort) == "Der Titel fehlt."
        assert "<details open>" in antwort.text
        assert ">behalten</textarea>" in antwort.text

    def test_laufender_timer_ueber_das_banner(self, klient, kunde, datenbank):
        """„Nur Beschreibung speichern“ sendet keinen Titel -- er bleibt."""
        zid = _timer(klient, kunde, datenbank)
        antwort = klient.post(f"/zeiten/{zid}/bearbeiten", data={"beschreibung": "läuft"},
                              follow_redirects=False)
        assert _meldung(antwort, "erfolg") == "Beschreibung gespeichert (Support)."
        eintrag = _eintrag(datenbank, zid)
        assert (eintrag["titel"], eintrag["beschreibung"], eintrag["end_zeit"]) == (
            "Support", "läuft", None)


# =================================================== Rechnung: Untertext

def _eintraege(*beschreibungen: str, minuten: int = 60) -> list[dict]:
    return [
        {"id": i, "datum": f"2026-03-{i + 1:02d}", "titel": "A", "beschreibung": text,
         "dauer_minuten": minuten, "abrechnungsmodus": "STUNDEN"}
        for i, text in enumerate(beschreibungen, start=1)
    ]


class TestUntertext:
    def test_ein_eintrag_ungekuerzt(self):
        lang = "Konzept " * 120
        [position] = gruppierung.erzeuge_positionen(
            _eintraege(lang), modus=gruppierung.PRO_TITEL, standard_cent=8000)
        assert position.untertext == lang.strip()
        [position] = gruppierung.erzeuge_positionen(
            _eintraege(lang), modus=gruppierung.PRO_EINTRAG, standard_cent=8000)
        assert position.untertext == f"02.03.2026 · {lang.strip()}"

    def test_mehrzeilige_werden_zeilenweise_getrennt(self):
        [position] = gruppierung.erzeuge_positionen(
            _eintraege("Zeile 1\nZeile 2", "Nächster Eintrag"),
            modus=gruppierung.PRO_TITEL, standard_cent=8000)
        assert position.untertext == "Zeile 1\nZeile 2\nNächster Eintrag"

    def test_einzeilige_weiter_mit_semikolon(self):
        [position] = gruppierung.erzeuge_positionen(
            _eintraege("Konzept", "Abstimmung"), modus=gruppierung.PRO_TITEL, standard_cent=8000)
        assert position.untertext == "Konzept; Abstimmung"

    def test_gekuerzt_wird_an_der_wortgrenze(self):
        texte = [f"Abstimmung Kampagne Nummer {i}" for i in range(1, 21)]
        [position] = gruppierung.erzeuge_positionen(
            _eintraege(*texte), modus=gruppierung.PRO_TITEL, standard_cent=8000)
        zusammen = "; ".join(texte)
        assert position.untertext.endswith("…")
        assert len(position.untertext) <= gruppierung.UNTERTEXT_MAX
        rumpf = position.untertext[:-1]
        assert zusammen.startswith(rumpf)
        assert zusammen[len(rumpf)] in " ;", "nicht mitten im Wort"

    def test_einzelnachweis_bleibt_ungekuerzt(self):
        lang = "Detail " * 300
        zeilen = gruppierung.einzelnachweis(_eintraege(lang, lang + "!"))
        assert [z["beschreibung"] for z in zeilen] == [lang.strip(), (lang + "!").strip()]


# =========================================================== Rundung

class TestRundungsregel:
    def test_kunde_schlaegt_betrieb(self, klient, kunde, datenbank):
        with datenbank() as conn:
            assert kunden.rundungsregel(conn, kunde) == rundung.Regel(
                "AUFWAERTS", 15, "BETRIEB", None)
        _kundenregel(klient, kunde, rundung_modus="KAUFMAENNISCH", rundung_takt="6",
                     rundung_grundlage="Angebot vom 03.02.2026")
        with datenbank() as conn:
            assert kunden.rundungsregel(conn, kunde) == rundung.Regel(
                "KAUFMAENNISCH", 6, "KUNDE", "Angebot vom 03.02.2026")
        # Ohne eigenen Takt gilt der des Betriebs.
        _kundenregel(klient, kunde, rundung_modus="AUFWAERTS", rundung_takt="")
        with datenbank() as conn:
            assert kunden.rundungsregel(conn, kunde).intervall_min == 15

    def test_formular_zeigt_die_vorgabe_und_speichert(self, klient, kunde, datenbank):
        seite = klient.get(f"/kunden/{kunde}").text
        assert '<option value="" selected>wie Betrieb (15 Min. aufwärts)</option>' in seite
        assert "„pro Eintrag“ rechnet jede Zeit minutengenau ab" in seite
        _kundenregel(klient, kunde, rundung_modus="KEINE", rundung_takt="30",
                     rundung_grundlage="AGB Stand 01/2026")
        with datenbank() as conn:
            zeile = conn.execute("SELECT rundung_modus, rundung_intervall_min,"
                                 " rundung_grundlage FROM kunde").fetchone()
            journal = {z["feld"] for z in conn.execute(
                "SELECT feld FROM journal WHERE aktion = 'AENDERN' AND objekttyp = 'kunde'")}
        # "Nicht runden" braucht keinen Takt -- ein eingetragener wird nicht gespeichert.
        assert tuple(zeile) == ("KEINE", None, "AGB Stand 01/2026")
        assert {"rundung_modus", "rundung_grundlage"} <= journal
        seite = klient.get(f"/kunden/{kunde}").text
        assert '<option value="KEINE" selected>' in seite
        # "Wie Betrieb" setzt alles zurueck.
        _kundenregel(klient, kunde, rundung_modus="", rundung_takt="10")
        with datenbank() as conn:
            assert tuple(conn.execute("SELECT rundung_modus, rundung_intervall_min"
                                      " FROM kunde").fetchone()) == (None, None)

    @pytest.mark.parametrize(("felder", "meldung"), [
        ({"rundung_modus": "QUATSCH"}, "Unbekannte Rundungsregel"),
        ({"rundung_modus": "AUFWAERTS", "rundung_takt": "0"}, "von 1 bis 60 Minuten"),
        ({"rundung_modus": "AUFWAERTS", "rundung_takt": "61"}, "von 1 bis 60 Minuten"),
        ({"rundung_modus": "AUFWAERTS", "rundung_takt": "x"}, "von 1 bis 60 Minuten"),
        ({"rundung_grundlage": "x" * 201}, "höchstens 200 Zeichen"),
    ])
    def test_abweisungen(self, klient, kunde, datenbank, felder, meldung):
        antwort = klient.post(f"/kunden/{kunde}", data={**KUNDE, **felder})
        assert antwort.status_code == 422
        assert meldung in html.unescape(antwort.text)
        with datenbank() as conn:
            assert conn.execute("SELECT rundung_modus FROM kunde").fetchone()[0] is None

    def test_neuer_kunde_mit_regel(self, klient, datenbank):
        antwort = klient.post("/kunden", data={**KUNDE, "name": "Muster AG",
                                               "rundung_modus": "ABWAERTS",
                                               "rundung_takt": "5"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        with datenbank() as conn:
            assert tuple(conn.execute(
                "SELECT rundung_modus, rundung_intervall_min FROM kunde WHERE name = 'Muster AG'"
            ).fetchone()) == ("ABWAERTS", 5)


class TestRundungSichtbar:
    def test_stopp_meldung_nennt_die_regel_des_kunden(self, klient, kunde, datenbank):
        _timer(klient, kunde, datenbank)
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert _meldung(antwort, "erfolg").endswith(
            "— abgerechnet wird je Rechnungsposition auf volle 15 Minuten aufgerundet.")
        _kundenregel(klient, kunde, rundung_modus="KEINE")
        _timer(klient, kunde, datenbank)
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert _meldung(antwort, "erfolg").endswith("— abgerechnet wird minutengenau.")
        # "Pro Eintrag" rundet nie -- auch mit Aufrundung am Kunden.
        _kundenregel(klient, kunde, rundung_modus="AUFWAERTS", gruppierung="PRO_EINTRAG")
        _timer(klient, kunde, datenbank)
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert _meldung(antwort, "erfolg").endswith("— abgerechnet wird minutengenau.")

    def test_liste_zeigt_erfasst_und_abrechenbar_wie_die_rechnung(
        self, klient, kunde, datenbank
    ):
        _zeit(klient, kunde, datenbank, dauer="7")
        _zeit(klient, kunde, datenbank, dauer="0:45", titel="Workshop")
        seite = klient.get("/zeiten").text
        block = seite.split("<h2>Noch nicht abgerechnet</h2>", 1)[1].split("</ul>", 1)[0]
        # 7 Minuten "Beratung" -> 0:15, 45 Minuten "Workshop" bleiben 0:45.
        assert '<span class="mono">0:52</span> erfasst' in block
        assert '<span class="mono">1:00</span> abrechenbar' in block
        assert '<span class="mono">90,00 €</span>' in block
        assert ("Rundung je Position: auf volle 15 Minuten aufgerundet, Gruppierung pro Titel"
                in block)
        assert f'href="/rechnung/neu?kunde={kunde}"' in block
        # Dieselbe Rechnung wie der Entwurf.
        with datenbank() as conn:
            ids = [z[0] for z in conn.execute("SELECT id FROM zeiteintrag")]
        rid = _entwurf(klient, kunde, ids)
        with datenbank() as conn:
            summe = conn.execute("SELECT brutto_cent, rundung_hinweis FROM rechnung"
                                 " WHERE id = ?", (rid,)).fetchone()
        assert summe["brutto_cent"] == 9000
        assert summe["rundung_hinweis"] == (
            "Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet.")
        assert "Noch nicht abgerechnet" not in klient.get("/zeiten").text

    def test_liste_mit_minutengenauer_regel(self, klient, kunde, datenbank):
        _kundenregel(klient, kunde, rundung_modus="KEINE")
        _zeit(klient, kunde, datenbank, dauer="7")
        block = klient.get("/zeiten").text.split("<h2>Noch nicht abgerechnet</h2>", 1)[1]
        assert '<span class="mono">0:07</span> abrechenbar' in block
        assert "(minutengenau, Gruppierung pro Titel, Regel des Kunden)" in block

    def test_assistent_rechnet_mit_der_regel_des_kunden(self, klient, kunde, datenbank):
        _zeit(klient, kunde, datenbank, dauer="7")
        seite = klient.get(f"/rechnung/neu?kunde={kunde}").text
        assert "22,50 €" in seite, "Vorgabe des Betriebs: 0,25 h zu 90 €"
        _kundenregel(klient, kunde, rundung_modus="KEINE")
        seite = klient.get(f"/rechnung/neu?kunde={kunde}").text
        assert "10,80 €" in seite, "minutengenau: 0,12 h zu 90 €"
        assert "22,50 €" not in seite
        assert "Für diesen Kunden wird nicht gerundet" in seite


class TestEntwurfBehaeltDieRegel:
    def test_kein_stilles_umrechnen(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank, dauer="7")
        rid = _entwurf(klient, kunde, [zid])

        def stand():
            with datenbank() as conn:
                return conn.execute(
                    "SELECT r.rundung_modus, r.rundung_intervall_min, r.rundung_hinweis,"
                    " p.menge_tausendstel FROM rechnung r JOIN rechnungsposition p"
                    " ON p.rechnung_id = r.id WHERE r.id = ?", (rid,)).fetchone()

        assert tuple(stand()) == (
            "AUFWAERTS", 15, "Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet.",
            250)
        # Die Regel am Kunden aendert sich, danach wird der Entwurf neu
        # berechnet (Dauer geaendert): er rechnet weiter mit seiner Regel.
        _kundenregel(klient, kunde, rundung_modus="KEINE")
        klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "8"}, follow_redirects=False)
        assert tuple(stand())[:2] == ("AUFWAERTS", 15)
        assert stand()["menge_tausendstel"] == 250

        seite = klient.get(f"/rechnung/{rid}").text
        assert "Am Kunden gilt inzwischen: minutengenau" in seite
        assert f'action="/rechnung/{rid}/rundung"' in seite

        antwort = klient.post(f"/rechnung/{rid}/rundung", follow_redirects=False)
        assert "übernommen" in _meldung(antwort, "hinweis")
        assert tuple(stand()) == ("KEINE", 15, None, 130)
        with datenbank() as conn:
            [eintrag] = conn.execute("SELECT * FROM journal WHERE aktion = 'AENDERN'"
                                     " AND objekttyp = 'rechnung' AND feld = 'rundung'")
        assert '"modus": "AUFWAERTS"' in eintrag["wert_alt"]
        assert '"modus": "KEINE"' in eintrag["wert_neu"]
        assert "Am Kunden gilt inzwischen" not in klient.get(f"/rechnung/{rid}").text
        # Ein zweiter Klick aendert nichts.
        antwort = klient.post(f"/rechnung/{rid}/rundung", follow_redirects=False)
        assert "bereits" in _meldung(antwort, "hinweis")

    def test_hinweis_folgt_der_gruppierung(self, klient, kunde, datenbank):
        """„Pro Eintrag“ rundet nicht -- dann steht auch kein Rundungssatz da."""
        zid = _zeit(klient, kunde, datenbank, dauer="7")
        rid = _entwurf(klient, kunde, [zid])

        def hinweis():
            with datenbank() as conn:
                return conn.execute("SELECT rundung_hinweis FROM rechnung WHERE id = ?",
                                    (rid,)).fetchone()[0]

        klient.post(f"/rechnung/{rid}/gruppierung", data={"modus": "PRO_EINTRAG"})
        assert hinweis() is None
        assert "bei „pro Eintrag“ entfällt die Rundung" in klient.get(f"/rechnung/{rid}").text
        klient.post(f"/rechnung/{rid}/gruppierung", data={"modus": "SAMMEL"})
        assert hinweis() == "Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet."

    def test_festgeschriebene_rechnung_behaelt_ihren_satz(self, klient, kunde, datenbank):
        zid = _zeit(klient, kunde, datenbank, dauer="7")
        rid = _entwurf(klient, kunde, [zid])
        _festschreiben(klient, datenbank, rid)
        _kundenregel(klient, kunde, rundung_modus="KEINE")
        with datenbank() as conn:
            zeile = conn.execute("SELECT rundung_hinweis, rundung_modus, brutto_cent"
                                 " FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert tuple(zeile) == (
            "Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet.", "AUFWAERTS", 2250)
        with pytest.raises(fs.FestschreibenFehler):
            with datenbank() as conn:
                conn.row_factory = sqlite3.Row
                fs.rundung_uebernehmen(conn, rid)


class TestHinweistext:
    @pytest.mark.parametrize(("modus", "takt", "gruppe", "text"), [
        ("AUFWAERTS", 15, "PRO_TITEL",
         "Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet."),
        ("KAUFMAENNISCH", 6, "SAMMEL",
         "Die Zeitsumme je Position ist kaufmännisch auf volle 6 Minuten gerundet."),
        ("ABWAERTS", 30, "PRO_TAG",
         "Die Zeitsumme je Position ist auf volle 30 Minuten abgerundet."),
        ("KEINE", 15, "PRO_TITEL", None),
        ("AUFWAERTS", 1, "PRO_TITEL", None),
        ("AUFWAERTS", 15, "PRO_EINTRAG", None),
    ])
    def test_je_regel(self, modus, takt, gruppe, text):
        """Kein „angefangene 15 Minuten“ mehr: das beschrieb einen Takt je
        Taetigkeit, den es nie gab."""
        assert rundung.Regel(modus, takt).hinweis(gruppe) == text


# ========================================================= Migration 21

def _als_version_20(pfad) -> None:
    """Baut eine frische Datenbank auf den Stand von Version 20 zurueck."""
    conn = db.oeffne(pfad)
    for trigger in ("zeit_abschnitt_einer_offen", "zeit_abschnitt_einer_offen_update",
                    "zeit_abschnitt_nur_offener_eintrag",
                    "zeit_abschnitt_nur_offener_eintrag_update",
                    "zeit_abschnitt_gesperrt_insert", "zeit_abschnitt_gesperrt_update",
                    "zeit_abschnitt_gesperrt_delete"):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP INDEX idx_abschnitt_zeit")
    conn.execute("DROP TABLE zeit_abschnitt")
    for tabelle, spalten in (
        ("kunde", ("rundung_modus", "rundung_intervall_min", "rundung_grundlage")),
        ("rechnung", ("rundung_modus", "rundung_intervall_min")),
    ):
        for spalte in spalten:
            conn.execute(f"ALTER TABLE {tabelle} DROP COLUMN {spalte}")
    alt = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'zeit_gesperrt'"
                       ).fetchone()[0]
    conn.execute("DROP TRIGGER zeit_gesperrt")
    conn.execute(alt.replace("      OR NEW.titel              IS NOT OLD.titel\n", "")
                 .replace("      OR NEW.beschreibung       IS NOT OLD.beschreibung\n", ""))
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version VALUES (20, ?)", (db.jetzt_utc(),))
    conn.close()


class TestMigration21:
    def test_von_20_auf_21(self, tmp_path):
        pfad = tmp_path / "alt.sqlite"
        _als_version_20(pfad)
        alt = sqlite3.connect(pfad)
        alt.execute(
            "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, standardsatz_cent,"
            " rundung_modus, rundung_intervall_min)"
            " VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095', 'Hamburg', '12/345/67890',"
            " 8000, 'KAUFMAENNISCH', 10)")
        alt.execute("INSERT INTO kunde (id, kundennummer, name, strasse, plz, ort, angelegt_am)"
                    " VALUES (1, 'K-0001', 'Beispiel GmbH', 'Musterstraße 1', '10115',"
                    " 'Berlin', '2026-01-01')")
        heute = _dt.date.today().isoformat()
        alt.execute("INSERT INTO zeiteintrag (id, kunde_id, titel, beschreibung, datum,"
                    " start_zeit, dauer_minuten, erfasst_am) VALUES (1, 1, 'Läuft', 'offen', ?,"
                    " '08:00:00', 1, 'x')", (heute,))
        alt.execute("INSERT INTO zeiteintrag (id, kunde_id, titel, datum, start_zeit, end_zeit,"
                    " dauer_minuten, erfasst_am) VALUES (2, 1, 'Fertig', '2026-03-02',"
                    " '09:00:00', '10:00:00', 60, 'x')")
        alt.execute("INSERT INTO rechnung (id, kunde_id, datum, status, erstellt_am)"
                    " VALUES (1, 1, '2026-03-10', 'ENTWURF', 'x')")
        alt.execute("INSERT INTO rechnung (id, typ, nummer, nummer_seq, nummer_jahr, kunde_id,"
                    " datum, netto_cent, brutto_cent, status, festgeschrieben_am, erstellt_am,"
                    " rundung_hinweis) VALUES (2, 'RECHNUNG', 'RE-2026-0001', 1, 2026, 1,"
                    " '2026-02-01', 8000, 8000, 'FESTGESCHRIEBEN', 'x', 'x',"
                    " 'Abrechnung in angefangenen 15 Minuten.')")
        alt.execute("INSERT INTO zeiteintrag (id, kunde_id, titel, datum, dauer_minuten,"
                    " rechnung_id, erfasst_am) VALUES (3, 1, 'Abgerechnet', '2026-01-15', 60, 2,"
                    " 'x')")
        alt.commit()
        alt.close()

        conn = db.oeffne(pfad)
        try:
            assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
                == db.SCHEMA_VERSION >= 21
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                                " AND objekt_id = 21").fetchone()[0] == 1
            # Daten bleiben.
            assert [tuple(z) for z in conn.execute(
                "SELECT id, titel, dauer_minuten FROM zeiteintrag ORDER BY id")] == [
                (1, "Läuft", 1), (2, "Fertig", 60), (3, "Abgerechnet", 60)]
            # Der laufende Timer bekommt seinen offenen Abschnitt ab seinem
            # Start, der abgeschlossene Alteintrag keinen.
            abschnitte = conn.execute(
                "SELECT zeiteintrag_id, beginn_utc, ende_utc FROM zeit_abschnitt").fetchall()
            assert [tuple(a) for a in abschnitte] == [
                (1, logik.ortszeit_als_utc(heute, "08:00:00"), None)]
            timer = logik.laufender_timer(conn)
            assert timer.id == 1 and timer.pausiert is False
            # Kunden ohne eigene Regel: wie Betrieb.
            assert tuple(conn.execute("SELECT rundung_modus, rundung_intervall_min,"
                                      " rundung_grundlage FROM kunde").fetchone()) == (
                None, None, None)
            # Der Entwurf friert die Regel ein, mit der er bisher rechnete;
            # die festgeschriebene Rechnung bleibt, wie sie ist.
            rechnungen = {z["id"]: z for z in conn.execute("SELECT * FROM rechnung")}
            assert (rechnungen[1]["rundung_modus"], rechnungen[1]["rundung_intervall_min"]) \
                == ("KAUFMAENNISCH", 10)
            assert (rechnungen[2]["rundung_modus"], rechnungen[2]["rundung_hinweis"]) == (
                None, "Abrechnung in angefangenen 15 Minuten.")
            # Die Sperre deckt jetzt Titel und Beschreibung.
            with pytest.raises(sqlite3.IntegrityError, match="nur ueber Storno"):
                conn.execute("UPDATE zeiteintrag SET beschreibung = 'neu' WHERE id = 3")
            # Und die Trigger der Abschnitte stehen.
            with pytest.raises(sqlite3.IntegrityError, match="bereits ein Arbeitsabschnitt"):
                conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc)"
                             " VALUES (1, ?)", (db.jetzt_utc(),))
        finally:
            conn.close()

    def test_schema_gleicht_einer_frischen_datenbank(self, tmp_path):
        _als_version_20(tmp_path / "alt.sqlite")
        migriert = db.oeffne(tmp_path / "alt.sqlite")
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        try:
            def objekte(conn, art):
                return {z[0] for z in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (art,))}

            for art in ("table", "trigger", "index"):
                assert objekte(migriert, art) == objekte(frisch, art), art
            # Die Reihenfolge der Spalten darf abweichen (ALTER haengt hinten an).
            for tabelle in ("kunde", "rechnung", "zeit_abschnitt"):
                assert {z[1] for z in migriert.execute(f"PRAGMA table_info({tabelle})")} \
                    == {z[1] for z in frisch.execute(f"PRAGMA table_info({tabelle})")}
            assert migriert.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'zeit_gesperrt'").fetchone()[0] \
                == frisch.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'zeit_gesperrt'").fetchone()[0]
        finally:
            migriert.close()
            frisch.close()

    def test_zweiter_lauf_legt_keinen_zweiten_abschnitt_an(self, conn, kunde_id):
        """Der Schritt ist wiederholbar: ein Timer mit Abschnitt bleibt, wie er ist."""
        zid = _laufend(conn, kunde_id)
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc)"
                     " VALUES (?, '2026-03-02T08:00:00+00:00')", (zid,))
        db._laufenden_timer_nachtragen(conn)
        assert conn.execute("SELECT COUNT(*) FROM zeit_abschnitt").fetchone()[0] == 1

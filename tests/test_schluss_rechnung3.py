"""Regressionstests zur Schlussprobe im Bereich Rechnung, EÜR und Zeiten.

Versandvermerk, der bei jedem Absenden ins unveraenderliche Journal schrieb;
eine EÜR, die in einem kuenftigen AfA-Planjahr oeffnete; ein Entwurf mit
Faelligkeit und offenem Betrag in der Rechnungsliste; eine geaenderte Dauer,
die Beginn und Ende stehen liess; ein Formularhinweis auf dem Ausdruck von
/zeiten.
"""

import re
import sqlite3
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "inhaber": "E. Muster",
    "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
    "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
    "rundung_modus": "AUFWAERTS", "rundung_intervall_min": "15",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "email": "buchhaltung@mustermann.example", "typ": "UNTERNEHMER", "land": "DE",
    "leistungsort": "INLAND", "satz_euro": "90,00", "gruppierung": "PRO_TITEL", "aktiv": "1",
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


def _abfrage(daten, sql, werte=()):
    conn = _db(daten)
    try:
        return conn.execute(sql, werte).fetchall()
    finally:
        conn.close()


def _schreibe(daten, sql, werte=()):
    conn = _db(daten)
    try:
        conn.execute(sql, werte)
        conn.commit()
    finally:
        conn.close()


def _zeit(klient, daten, datum="2026-03-02", dauer="1h", titel="Beratung") -> int:
    antwort = klient.post("/zeiten", data={
        "kunde_id": 1, "titel": titel, "datum": datum, "dauer": dauer,
        "beschreibung": "", "modus": "STUNDEN", "grund": "",
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    return int(_abfrage(daten, "SELECT MAX(id) FROM zeiteintrag")[0][0])


def _entwurf(klient, daten, datum="2026-03-10") -> int:
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": 1, "datum": datum, "modus": "PRO_TITEL",
        "einleitung": "", "schluss": "", "zeit": [_zeit(klient, daten)],
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    ort = antwort.headers["location"]
    assert "fehler=" not in ort, ort
    return int(re.search(r"/rechnung/(\d+)", ort).group(1))


def _query(antwort, schluessel) -> str:
    assert antwort.status_code == 303, antwort.text[:400]
    return parse_qs(urlparse(antwort.headers["location"]).query).get(schluessel, [""])[0]


def _festgeschrieben(klient, daten, datum="2026-03-10") -> int:
    rid = _entwurf(klient, daten, datum)
    seite = klient.get(f"/rechnung/{rid}").text
    version = re.search(r'name="version" value="(\d+)"', seite).group(1)
    codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
    antwort = klient.post(f"/rechnung/{rid}/festschreiben",
                          data={"version": version, "bestaetigt": codes},
                          follow_redirects=False)
    assert "fehler=" not in antwort.headers["location"], _query(antwort, "fehler")
    return rid


def _parallel(anzahl, funktion):
    ergebnisse = [None] * anzahl
    sperre = threading.Barrier(anzahl)

    def lauf(i):
        try:
            sperre.wait()
            ergebnisse[i] = funktion(i)
        except Exception as fehler:  # noqa: BLE001 -- im Test sichtbar machen
            ergebnisse[i] = fehler

    faeden = [threading.Thread(target=lauf, args=(i,)) for i in range(anzahl)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join()
    return ergebnisse


def _versand_journal(daten, rid):
    return [tuple(z) for z in _abfrage(
        daten,
        "SELECT feld, wert_alt, wert_neu FROM journal"
        " WHERE aktion = 'VERSENDET' AND objekt_id = ? ORDER BY id",
        (rid,),
    )]


# ------------------------------------------------- 1: Versandvermerk

class TestVersandvermerk:
    def test_vier_gleichzeitige_klicks_ergeben_einen_eintrag(self, klient, daten):
        """Vorher: vier gleichzeitige POST /rechnung/{id}/versendet mit demselben
        Datum schrieben vier gleiche VERSENDET-Zeilen ins Journal, das sich
        nicht mehr bereinigen laesst."""
        rid = _festgeschrieben(klient, daten)
        antworten = _parallel(4, lambda i: klient.post(
            f"/rechnung/{rid}/versendet", data={"datum": "2026-03-15"},
            follow_redirects=False))
        for antwort in antworten:
            assert not isinstance(antwort, Exception), antwort
            assert antwort.status_code == 303
            assert "fehler=" not in antwort.headers["location"]
            assert "hinweis=" not in antwort.headers["location"]
        assert _versand_journal(daten, rid) == [(None, None, "2026-03-15")]
        assert _abfrage(daten, "SELECT versendet_am FROM rechnung WHERE id = ?",
                        (rid,))[0][0] == "2026-03-15"

    def test_gleiches_datum_nacheinander_schreibt_nichts(self, klient, daten):
        """Vorher: der zweite Klick (alter Tab, Zuruecktaste) schrieb einen
        zweiten, identischen Journaleintrag."""
        rid = _festgeschrieben(klient, daten)
        for _ in range(3):
            antwort = klient.post(f"/rechnung/{rid}/versendet",
                                  data={"datum": "2026-03-15"}, follow_redirects=False)
            assert antwort.status_code == 303
            assert antwort.headers["location"] == f"/rechnung/{rid}"
        assert _versand_journal(daten, rid) == [(None, None, "2026-03-15")]

    def test_anderes_datum_mit_altem_wert_und_meldung(self, klient, daten):
        """Vorher: ein spaeteres anderes Datum ueberschrieb versendet_am still,
        und der Journaleintrag hatte keinen wert_alt -- der erste Vermerk war
        nicht mehr feststellbar (§ 146 Abs. 4 AO)."""
        rid = _festgeschrieben(klient, daten)
        klient.post(f"/rechnung/{rid}/versendet", data={"datum": "2026-03-15"},
                    follow_redirects=False)
        # Seit dem Rechnungsablauf braucht das Aendern eines gesetzten
        # Vermerks einen Grund (der Vermerk entscheidet, ob die Rechnung noch
        # zurueckgenommen werden darf).
        ohne = klient.post(f"/rechnung/{rid}/versendet", data={"datum": "2026-03-16"},
                           follow_redirects=False)
        assert "gehört ein Grund" in _query(ohne, "fehler")
        antwort = klient.post(f"/rechnung/{rid}/versendet",
                              data={"datum": "2026-03-16", "grund": "Tippfehler"},
                              follow_redirects=False)
        assert _query(antwort, "fehler") == ""
        assert _query(antwort, "hinweis") == (
            "Versanddatum von 15.03.2026 auf 16.03.2026 geändert."
        )
        assert _versand_journal(daten, rid) == [
            (None, None, "2026-03-15"),
            ("versendet_am", "2026-03-15", "2026-03-16"),
        ]
        assert _abfrage(daten, "SELECT versendet_am FROM rechnung WHERE id = ?",
                        (rid,))[0][0] == "2026-03-16"
        seite = klient.get(antwort.headers["location"]).text
        assert "Versanddatum von 15.03.2026 auf 16.03.2026 geändert." in seite
        assert "Versendet am 16.03.2026." in seite


# ------------------------------------------------- 2: EÜR-Standardjahr

GUT = {
    "bezeichnung": "Laptop", "brutto_euro": "2.142,00", "ust_satz": "19",
    "nutzungsdauer_jahre": "3", "art": "BEWEGLICH", "aufteilungsquote": "100",
}


def _h1(text) -> str:
    return re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S).group(1).strip()


class TestEuerStandardjahr:
    @pytest.fixture
    def laufend(self):
        from app import db
        return int(db.heute()[:4])

    def _laptop(self, klient, daten, laufend):
        antwort = klient.post("/anlagen", data={**GUT, "anschaffung_am": f"{laufend}-01-15"},
                              follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        jahre = [z[0] for z in _abfrage(daten, "SELECT jahr FROM anlagegut_afa_jahr ORDER BY jahr")]
        assert jahre == [laufend, laufend + 1, laufend + 2]

    def test_ohne_jahr_das_laufende_statt_des_letzten_afa_jahres(self, klient, daten, laufend):
        """Vorher: mit einem Anlagegut ueber drei Jahre oeffnete /euer im letzten
        AfA-Planjahr (heute 2026: „Einnahmenüberschussrechnung 2028“, nur
        Abschreibung und ein Verlust)."""
        self._laptop(klient, daten, laufend)
        text = klient.get("/euer").text
        assert _h1(text) == f"Einnahmenüberschussrechnung {laufend}"
        assert f'<option value="{laufend}" selected>' in text
        # Die Planjahre bleiben waehlbar.
        assert f'<option value="{laufend + 2}" >' in text
        assert _h1(klient.get(f"/euer?jahr={laufend + 2}").text) == (
            f"Einnahmenüberschussrechnung {laufend + 2}"
        )

    @pytest.mark.parametrize("roh", ["abc", "%C2%B2", "9223372036854775808", "0", ""])
    def test_unsinn_in_der_adresse_faellt_aufs_laufende_jahr(self, klient, daten, laufend, roh):
        """Vorher: ?jahr=abc, ?jahr=² und eine riesige Zahl fielen ebenfalls auf
        das kuenftige Planjahr zurueck."""
        self._laptop(klient, daten, laufend)
        assert _h1(klient.get(f"/euer?jahr={roh}").text) == (
            f"Einnahmenüberschussrechnung {laufend}"
        )

    def test_ohne_laufendes_jahr_das_juengste_vergangene(self, klient, daten, laufend):
        """Hat das laufende Jahr nichts, aber ein vergangenes und ein kuenftiges,
        zeigt die Seite das vergangene -- nicht das kuenftige."""
        self._laptop(klient, daten, laufend)
        _schreibe(daten, "DELETE FROM anlagegut_afa_jahr WHERE jahr = ?", (laufend,))
        _schreibe(daten, "UPDATE anlagegut_afa_jahr SET jahr = ? WHERE jahr = ?",
                  (laufend - 2, laufend + 1))
        assert _h1(klient.get("/euer").text) == f"Einnahmenüberschussrechnung {laufend - 2}"

    def test_nur_kuenftige_jahre_das_frueheste(self, klient, daten, laufend):
        """Gibt es nur kuenftige Jahre, das naechstgelegene."""
        self._laptop(klient, daten, laufend)
        _schreibe(daten, "DELETE FROM anlagegut_afa_jahr WHERE jahr = ?", (laufend,))
        assert _h1(klient.get("/euer").text) == f"Einnahmenüberschussrechnung {laufend + 1}"


# ------------------------------------------------- 3: Entwurf in der Rechnungsliste

def _zellen(html, rid):
    for zeile in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        if f'href="/rechnung/{rid}"' in zeile:
            return [re.sub(r"<[^>]+>|\s+", " ", z).strip()
                    for z in re.findall(r"<td[^>]*>(.*?)</td>", zeile, re.S)]
    raise AssertionError(f"Rechnung {rid} fehlt in der Liste")


class TestEntwurfInDerListe:
    def test_entwurf_ohne_faelligkeit_und_ohne_offenen_betrag(self, klient, daten):
        """Vorher: die Zeile des Entwurfs zeigte „14.04.2026“ als Faelligkeit und
        „135,00 €“ als offen, waehrend die Uebersicht 0 offene Rechnungen
        zaehlte."""
        rid = _entwurf(klient, daten, datum="2026-03-31")
        nummer, _kunde, datum, faellig, betrag, offen, status, _pdf = _zellen(
            klient.get("/rechnungen").text, rid)
        assert nummer == "Entwurf" and status == "Entwurf"
        assert datum == "31.03.2026"
        assert betrag == "90,00 €"
        assert faellig == "—"
        assert offen == "—"

    def test_festgeschriebene_rechnung_zeigt_beides_weiter(self, klient, daten):
        """Gegenprobe: die festgeschriebene Rechnung behaelt Faelligkeit und Rest."""
        rid = _festgeschrieben(klient, daten, datum="2026-03-31")
        _n, _k, _d, faellig, _b, offen, _s, _p = _zellen(klient.get("/rechnungen").text, rid)
        assert faellig == "14.04.2026"
        assert offen == "90,00 €"


# ------------------------------------------------- 4: Dauer ändern zieht das Ende nach

def _vergessener_timer(klient, daten, start_zeit="00:00:01", tage=1) -> int:
    """Ein Timer, der seit ``tage`` Tagen um ``start_zeit`` laeuft."""
    import datetime as _dt

    antwort = klient.post("/zeiten/timer/start", data={"kunde_id": 1, "titel": "Vergessen"},
                          follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:400]
    zid = int(_abfrage(daten, "SELECT MAX(id) FROM zeiteintrag")[0][0])
    from app.logik import zeiten

    datum = (_dt.date.today() - _dt.timedelta(days=tage)).isoformat()
    _schreibe(daten, "UPDATE zeiteintrag SET datum = ?, start_zeit = ? WHERE id = ?",
              (datum, start_zeit, zid))
    # Seit Version 21 rechnet der Timer aus seinen Arbeitsabschnitten: der
    # offene Abschnitt wandert mit dem Beginn zurueck.
    _schreibe(daten, "UPDATE zeit_abschnitt SET beginn_utc = ? WHERE zeiteintrag_id = ?"
                     " AND ende_utc IS NULL",
              (zeiten.ortszeit_als_utc(datum, start_zeit), zid))
    return zid


def _zeile(daten, zid):
    return dict(_abfrage(daten, "SELECT start_zeit, end_zeit, dauer_minuten FROM zeiteintrag"
                                " WHERE id = ?", (zid,))[0])


class TestDauerUndEnde:
    def test_stopp_nach_mehr_als_24_stunden_setzt_das_tagesende(self, klient, daten):
        """Vorher: Start gestern 00:00:01, Stopp heute 20:50 ergab end_zeit
        „20:50:07“ bei 2690 Minuten -- das las sich wie ein Arbeitstag von 0 bis
        21 Uhr. Jetzt steht wie beim Lauf ueber Mitternacht das Tagesende."""
        zid = _vergessener_timer(klient, daten)
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        assert antwort.status_code == 303
        zeile = _zeile(daten, zid)
        assert zeile["dauer_minuten"] > 24 * 60, "gebucht wird weiter die Laufzeit"
        assert zeile["end_zeit"] == "23:59:59"

    def test_hinweis_nennt_den_weg(self, klient, daten):
        """Vorher: „Der Timer lief 45 Stunden — bitte prüfen.“ -- ohne zu sagen,
        wo; das Erfassen-Formular weist die gebuchte Dauer ab."""
        _vergessener_timer(klient, daten)
        antwort = klient.post("/zeiten/timer/stop", follow_redirects=False)
        hinweis = _query(antwort, "hinweis")
        assert re.fullmatch(
            r"Der Timer lief \d+ Stunden — bitte prüfen und in der Liste unter "
            r"„Dauer ändern“ korrigieren\.", hinweis), hinweis

    def test_dauer_aendern_setzt_das_ende_neu(self, klient, daten):
        """Vorher: nach „Dauer ändern“ auf 8:00 blieb das alte Ende stehen --
        Excel-Export: Beginn 00:00:01, Ende 20:50:40, Dauer 480 Minuten."""
        zid = _vergessener_timer(klient, daten)
        klient.post("/zeiten/timer/stop", follow_redirects=False)
        antwort = klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "8:00"},
                              follow_redirects=False)
        assert "Dauer geändert" in _query(antwort, "erfolg")
        assert _zeile(daten, zid) == {
            "start_zeit": "00:00:01", "end_zeit": "08:00:01", "dauer_minuten": 480,
        }
        # Journal wie bisher: genau ein Eintrag zur Dauer.
        journal = [tuple(z) for z in _abfrage(
            daten, "SELECT feld, wert_neu FROM journal WHERE aktion = 'AENDERN'"
                   " AND objekttyp = 'zeiteintrag' AND objekt_id = ?", (zid,))]
        assert journal == [("dauer_minuten", "480")]

    def test_neue_dauer_ueber_mitternacht_endet_am_tagesende(self, klient, daten):
        """Beginn 22:30 plus 2 Stunden reicht in den Folgetag; das Schema
        verlangt end_zeit >= start_zeit, also steht das Tagesende."""
        zid = _vergessener_timer(klient, daten, start_zeit="22:30:00")
        klient.post("/zeiten/timer/stop", follow_redirects=False)
        klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "2h"}, follow_redirects=False)
        assert _zeile(daten, zid) == {
            "start_zeit": "22:30:00", "end_zeit": "23:59:59", "dauer_minuten": 120,
        }
        klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "1:15"}, follow_redirects=False)
        assert _zeile(daten, zid)["end_zeit"] == "23:45:00"

    def test_nachtraeglich_erfasste_zeit_bleibt_ohne_uhrzeiten(self, klient, daten):
        """Ohne Beginn gibt es kein Ende zu rechnen."""
        zid = _zeit(klient, daten)
        klient.post(f"/zeiten/{zid}/dauer", data={"dauer": "2h"}, follow_redirects=False)
        assert _zeile(daten, zid) == {"start_zeit": None, "end_zeit": None, "dauer_minuten": 120}


# ------------------------------------------------- 5: Druck von /zeiten

class TestDruckZeiten:
    """Die Druckregel (stil.css) blendet Formulare, Aufklapper und .nicht-drucken
    aus. Geprueft wird hier, dass alles, was nur zum Formular gehoert, eine
    dieser Klassen traegt -- der Augenschein im Browser (emulate_media print)
    steht im Bericht der Schlussprobe."""

    def test_dauerhinweis_unter_dem_formular_wird_nicht_gedruckt(self, klient, daten):
        """Vorher: „Dauer: 1:45, 1h45 … Eine blanke Zahl gilt als Minuten.“ stand
        auf Papier allein unter „Zeiten“, das Formular dazu war ausgeblendet --
        mit und ohne Eintraege."""
        for _ in range(2):
            text = klient.get("/zeiten").text
            absatz = re.search(r"<p ([^>]*)>Dauer: <code>1:45</code>", text)
            assert absatz is not None
            assert "nicht-drucken" in absatz.group(1)
            _zeit(klient, daten)

    def test_spalte_der_aktionen_faellt_im_druck_weg(self, klient, daten):
        """Die Knoepfe der Zeile sind im Druck ausgeblendet; die feste Spalte
        dafuer liess die Zeilen vorher eine Handbreit vor dem Rand enden."""
        _zeit(klient, daten)
        assert '<col class="sp-aktionen nicht-drucken">' in klient.get("/zeiten").text

    def test_rat_zum_stoppen_nicht_auf_papier(self, klient, daten):
        """Im Banner eines vergessenen Timers bleibt der Befund („vermutlich
        vergessen“) im Druck stehen, der Rat zu Knopf und Formular nicht."""
        # Vorgestern statt gestern: "gestern 08:00" war vor 8 Uhr morgens noch
        # keine 24 Stunden her, und der Test hing von der Uhrzeit ab.
        _vergessener_timer(klient, daten, start_zeit="08:00:00", tage=2)
        text = klient.get("/zeiten").text
        assert "vermutlich\n    vergessen." in text
        assert ('<span class="nicht-drucken"> Nach dem Stoppen die Dauer prüfen'
                in text)


# ------------------------------------------------- Nebenbefund: Stunden im Banner

class TestTimerBanner:
    def test_stunden_ohne_dezimalpunkt(self, klient, daten):
        """Vorher: „Der Timer läuft seit über 24.0 Stunden“ -- mit englischem
        Dezimalpunkt und bei 23:59 Laufzeit auf 24 aufgerundet."""
        # Vorgestern statt gestern: "gestern 08:00" war vor 8 Uhr morgens noch
        # keine 24 Stunden her, und der Test hing von der Uhrzeit ab.
        _vergessener_timer(klient, daten, start_zeit="08:00:00", tage=2)
        text = klient.get("/zeiten").text
        treffer = re.search(r"Der Timer läuft seit (\S+) Stunden — vermutlich", text)
        assert treffer is not None, text[:2000]
        assert treffer.group(1).isdigit(), treffer.group(1)
        assert int(treffer.group(1)) >= 24

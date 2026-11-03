"""Regressionstests zu den Befunden der Fehleranalyse im Bereich Rechnung.

Je Befund mindestens ein Test, der das alte Fehlverhalten nachstellt: HTTP 500
statt deutscher Meldung, Leistungsort, der den Entwurf nicht erreicht, ein
eingefrorener Satz, der nie auf dem Papier stand, Fehlalarme in der Pruefung,
Pluralformen, ISO-Daten, PDF im falschen Ordner.
"""

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import festschreiben as fs
from app.logik import gruppierung as g
from app.logik import validierung as v
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


def _zeit(klient, datum="2026-03-02", dauer="1h", titel="Beratung", kunde_id=1, **rest) -> int:
    klient.post("/zeiten", data={
        "kunde_id": kunde_id, "titel": titel, "datum": datum, "dauer": dauer,
        "beschreibung": rest.get("beschreibung", ""),
        "modus": rest.get("modus", "STUNDEN"), "grund": rest.get("grund", ""),
    }, follow_redirects=False)
    # Die zuletzt erfasste Zeit steht am Ende der Zeitenseite nicht eindeutig;
    # die Datenbank weiss es sicher.
    with sqlite3.connect(Path(db.standard_datenordner()) / "steuer.sqlite") as conn:
        return int(conn.execute("SELECT MAX(id) FROM zeiteintrag").fetchone()[0])


def _entwurf(klient, zeit_ids, **rest):
    return klient.post("/rechnung/neu", data={
        "kunde_id": rest.get("kunde_id", 1), "datum": rest.get("datum", "2026-03-10"),
        "modus": rest.get("modus", "PRO_TITEL"), "einleitung": "", "schluss": "",
        "zeit": zeit_ids,
    }, follow_redirects=False)


def _entwurf_id(antwort) -> int:
    assert antwort.status_code == 303, antwort.text[:400]
    ort = antwort.headers["location"]
    assert "fehler=" not in ort, ort
    return int(re.search(r"/rechnung/(\d+)", ort).group(1))


#: Ein Datum im naechsten Jahr: in der Zukunft (G01), aber im plausiblen
#: Fenster. "2099-01-01" wird seit der Datumsplausibilisierung abgewiesen.
ZUKUNFT = date(date.today().year + 1, 1, 1)


def _fehler(antwort) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(antwort.headers["location"]).query).get("fehler", [""])[0]


def _version_und_codes(klient, rid) -> tuple[str, list[str], str]:
    seite = klient.get(f"/rechnung/{rid}").text
    version = re.search(r'name="version" value="(\d+)"', seite).group(1)
    codes = re.findall(r'name="bestaetigt" value="([\w-]+)"', seite)
    return version, codes, seite


def _festschreiben(klient, rid):
    version, codes, _ = _version_und_codes(klient, rid)
    return klient.post(f"/rechnung/{rid}/festschreiben",
                       data={"version": version, "bestaetigt": codes},
                       follow_redirects=False)


# ------------------------------------------------------- 1: Stundensatz null

class TestStundensatzNull:
    def test_kein_satz_ergibt_meldung_statt_serverfehler(self, klient, daten):
        """Seit der letzten Korrekturrunde 422 mit erhaltenen Eingaben statt der
        Umleitung auf den leeren Assistenten (tests/test_rechnung_rest.py)."""
        klient.post("/einrichtung", data={**EINRICHTUNG, "standardsatz_euro": "0"})
        klient.post("/kunden/1", data={**KUNDE, "satz_euro": ""})
        zid = _zeit(klient)
        antwort = _entwurf(klient, [zid])
        assert antwort.status_code == 422
        assert "kein Stundensatz hinterlegt" in antwort.text
        conn = _db(daten)
        assert conn.execute("SELECT COUNT(*) FROM rechnung").fetchone()[0] == 0
        assert conn.execute(
            "SELECT rechnung_id FROM zeiteintrag WHERE id = ?", (zid,)
        ).fetchone()[0] is None, "Die Zeit bleibt frei"
        conn.close()

    def test_assistent_zeigt_den_hinweis_statt_nullvorschau(self, klient):
        klient.post("/einrichtung", data={**EINRICHTUNG, "standardsatz_euro": "0"})
        klient.post("/kunden/1", data={**KUNDE, "satz_euro": ""})
        _zeit(klient)
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert "kein Stundensatz hinterlegt" in seite
        assert "0,00 €" not in seite

    def test_integrityerror_wird_zur_meldung(self, klient, daten):
        """Eine Pauschale ueber 0 € laesst die Datenbank die Position abweisen --
        der Nutzer bekommt den Grund, keinen Serverfehler. Seit der letzten
        Korrekturrunde als 422-Seite mit erhaltenen Eingaben statt Umleitung."""
        conn = _db(daten)
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, pauschal_cent, erfasst_am)"
            " VALUES (1, 'Provision', '2026-03-02', 30, 'PAUSCHAL', 0, ?)",
            (db.jetzt_utc(),),
        )
        conn.commit()
        zid = conn.execute("SELECT MAX(id) FROM zeiteintrag").fetchone()[0]
        conn.close()
        antwort = _entwurf(klient, [zid])
        assert antwort.status_code == 422
        assert "keinen Betrag" in antwort.text
        assert "constraint" not in antwort.text


# ------------------------------------------- 2: Privatperson im Ausland

class TestPrivatkundeImAusland:
    def _kunde(self, conn, leistungsort):
        return conn.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, land, typ,"
            " leistungsort, satz_cent, angelegt_am)"
            " VALUES ('K-0002', 'Anna Huber', 'Ring 1', '1010', 'Wien', 'AT', 'PRIVAT',"
            " ?, 9000, ?)",
            (leistungsort, db.heute()),
        ).lastrowid

    def _zeit(self, conn, kid):
        return conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
            " erfasst_am) VALUES (?, 'Beratung', '2026-03-02', 60, 'STUNDEN', ?)",
            (kid, db.jetzt_utc()),
        ).lastrowid

    def test_inland_bei_privatperson_ist_nur_gelb(self, conn, betrieb):
        kid = self._kunde(conn, "INLAND")
        rid = fs.entwurf_anlegen(conn, kid, [self._zeit(conn, kid)])
        befunde = {b.code: b for b in v.pruefe(conn, rid)}
        assert "R20" not in befunde
        assert befunde["G24"].stufe == v.GELB
        assert "§ 3a Abs. 1 UStG" in befunde["G24"].text
        assert "§ 3a Abs. 4" in befunde["G24"].text and "§ 3a Abs. 5" in befunde["G24"].text

    def test_reverse_charge_an_privatperson_ist_rot(self, conn, betrieb):
        kid = self._kunde(conn, "EU_B2B_REVERSE_CHARGE")
        rid = fs.entwurf_anlegen(conn, kid, [self._zeit(conn, kid)])
        befunde = {b.code: b for b in v.pruefe(conn, rid)}
        assert befunde["R21"].stufe == v.ROT
        assert "§ 13b UStG" in befunde["R21"].text
        assert "Art. 196 MwStSystRL" in befunde["R21"].text


# ---------------------------------------- 3: Leistungsort erreicht den Entwurf

class TestLeistungsortNachziehen:
    def test_korrektur_am_kunden_erreicht_die_ansicht(self, klient):
        """Fall A: Kunde faelschlich mit Land DE angelegt, nach dem Entwurf
        auf AT + Reverse Charge berichtigt. Vorher blieb R20 stehen."""
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        klient.post("/kunden/1", data={
            **KUNDE, "land": "AT", "plz": "5020", "ort": "Salzburg",
            "leistungsort": "EU_B2B_REVERSE_CHARGE", "ust_idnr": "ATU12345678",
        })
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Leistungsort steht aber auf Inland" not in seite
        assert "Steuerschuldnerschaft des Leistungsempfängers" in seite

    def test_festschreiben_zieht_den_leistungsort_vor_der_pruefung_nach(self, klient, daten):
        """Fall B: Land bleibt DE, Leistungsort am Kunden auf EU-B2B. Vorher
        entstand eine § 19-Rechnung, deren Kundenschnappschuss EU-B2B sagte."""
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        klient.post("/kunden/1", data={
            **KUNDE, "leistungsort": "EU_B2B_REVERSE_CHARGE", "ust_idnr": "DE999999999",
        })
        conn = db.verbinde(daten / "steuer.sqlite")
        try:
            # Direkt die Logik, ohne vorheriges GET: die Pruefung muss selbst
            # den aktuellen Kundenstand sehen.
            fs.festschreiben(conn, rid, belegordner=daten)
            r = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        finally:
            conn.close()
        assert r["status"] == "FESTGESCHRIEBEN"
        assert r["leistungsort"] == "EU_B2B_REVERSE_CHARGE"
        assert "Steuerschuldnerschaft des Leistungsempfängers" in r["hinweis_19_text"]
        assert "§ 19" not in r["hinweis_19_text"]
        assert '"leistungsort": "EU_B2B_REVERSE_CHARGE"' in r["kunde_snapshot_json"]

    def test_rueckweg_nach_inland_setzt_den_paragraph_19_hinweis(self, klient, daten):
        klient.post("/kunden/1", data={
            **KUNDE, "leistungsort": "EU_B2B_REVERSE_CHARGE", "ust_idnr": "DE999999999",
        })
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        klient.post("/kunden/1", data=KUNDE)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "§ 19" in seite
        conn = _db(daten)
        r = conn.execute("SELECT leistungsort FROM rechnung WHERE id = ?", (rid,)).fetchone()
        conn.close()
        assert r["leistungsort"] == "INLAND"


# ------------------------------------ 4: Eingefrorener Satz = abgerechneter Satz

class TestSatzSnapshot:
    def test_snapshot_ist_der_einzelpreis_der_position(self, klient, daten):
        ids = [_zeit(klient, datum="2026-03-02"), _zeit(klient, datum="2026-03-09", dauer="2h")]
        rid = _entwurf_id(_entwurf(klient, ids))
        klient.post("/kunden/1", data={**KUNDE, "satz_euro": "120,00"})
        assert _festschreiben(klient, rid).status_code == 303
        conn = _db(daten)
        snapshots = [z[0] for z in conn.execute(
            "SELECT satz_cent_snapshot FROM zeiteintrag WHERE rechnung_id = ?", (rid,))]
        preise = {p[0] for p in conn.execute(
            "SELECT einzelpreis_cent FROM rechnungsposition WHERE rechnung_id = ?", (rid,))}
        conn.close()
        assert preise == {9000}
        assert snapshots == [9000, 9000], "nicht der heutige Kundensatz von 120 €"


# ------------------------------------------- 5: Gemischte Saetze in einer Gruppe

class TestGemischteSaetze:
    def _eintraege(self, reihenfolge):
        alt = {"id": 1, "datum": "2026-03-02", "titel": "Beratung", "dauer_minuten": 60,
               "abrechnungsmodus": "STUNDEN", "satz_cent_snapshot": 8000}
        neu = {"id": 2, "datum": "2026-03-03", "titel": "Beratung", "dauer_minuten": 60,
               "abrechnungsmodus": "STUNDEN", "satz_cent_snapshot": None}
        return [alt, neu] if reihenfolge == "alt_zuerst" else [neu, alt]

    @pytest.mark.parametrize("modus", g.MODI)
    @pytest.mark.parametrize("reihenfolge", ["alt_zuerst", "neu_zuerst"])
    def test_eine_stunde_zu_80_plus_eine_zu_90_sind_170(self, modus, reihenfolge):
        positionen = g.erzeuge_positionen(
            self._eintraege(reihenfolge), modus=modus, standard_cent=9000,
            intervall_min=15, modus_rundung="AUFWAERTS",
        )
        assert g.summe_cent(positionen) == 17000
        assert {p.einzelpreis_cent for p in positionen} == {8000, 9000}
        for p in positionen:
            assert p.menge_tausendstel * p.einzelpreis_cent // 1000 == p.betrag_cent


# ------------------------------------------------- 6: G17 nur bei echter Dopplung

class TestG17:
    def _zeit(self, conn, kid, datum, modus="STUNDEN", grund=None):
        return conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
            " grund, satz_cent_snapshot, erfasst_am)"
            " VALUES (?, 'Beratung', ?, 60, ?, ?, ?, ?)",
            (kid, datum, modus, grund, 8000 if modus == "KULANZ" else None, db.jetzt_utc()),
        ).lastrowid

    @pytest.mark.parametrize("modus", g.MODI)
    def test_gruppierung_selbst_loest_nichts_aus(self, conn, betrieb, kunde_id, modus):
        ids = [
            self._zeit(conn, kunde_id, "2026-03-02"),
            self._zeit(conn, kunde_id, "2026-03-03"),
            self._zeit(conn, kunde_id, "2026-03-03", "KULANZ", "Nachbesserung"),
        ]
        rid = fs.entwurf_anlegen(conn, kunde_id, ids, modus=modus)
        assert not [c for c in (b.code for b in v.pruefe(conn, rid)) if c.startswith("G17")]

    def test_echte_dopplung_wird_gemeldet(self, conn, betrieb, kunde_id):
        rid = fs.entwurf_anlegen(conn, kunde_id, [self._zeit(conn, kunde_id, "2026-03-02")])
        conn.execute(
            "INSERT INTO rechnungsposition (rechnung_id, pos_nr, bezeichnung, untertext,"
            " menge_tausendstel, einheit, einzelpreis_cent, nachlass_cent, nachlass_grund,"
            " darstellung, betrag_cent, quelle)"
            " SELECT rechnung_id, 2, bezeichnung, untertext, menge_tausendstel, einheit,"
            " einzelpreis_cent, nachlass_cent, nachlass_grund, darstellung, betrag_cent,"
            " quelle FROM rechnungsposition WHERE rechnung_id = ?",
            (rid,),
        )
        texte = [b.text for b in v.pruefe(conn, rid) if b.code.startswith("G17")]
        assert texte and "Beratung" in texte[0]


# --------------------------------------- 7 und 13: Eingaben ohne Serverfehler

class TestEingaben:
    """Abweisungen beim Anlegen kommen seit der letzten Korrekturrunde als
    422-Seite mit erhaltenen Eingaben, nicht mehr als Umleitung, die sie
    verwarf (tests/test_rechnung_rest.py)."""

    def test_ungueltiges_datum_beim_anlegen(self, klient):
        zid = _zeit(klient)
        for kaputt in ("2026-13-45", "10.03.2026"):
            antwort = _entwurf(klient, [zid], datum=kaputt)
            assert antwort.status_code == 422
            assert "ist kein Datum" in antwort.text
        assert "Rechnungsdatum fehlt" in _entwurf(klient, [zid], datum="").text

    def test_unbekannte_gruppierung_beim_anlegen(self, klient):
        zid = _zeit(klient)
        antwort = _entwurf(klient, [zid], modus="QUATSCH")
        assert antwort.status_code == 422
        assert "Unbekannte Gruppierung" in antwort.text

    def test_gruppierung_auf_fremder_rechnung(self, klient):
        antwort = klient.post("/rechnung/99999/gruppierung", data={"modus": "PRO_TAG"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "gibt es nicht" in _fehler(antwort)

    def test_fehlende_pflichtfelder_geben_deutsche_meldungen(self, klient):
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        ohne_datum = klient.post("/rechnung/neu", data={
            "kunde_id": 1, "modus": "PRO_TITEL", "zeit": [zid]}, follow_redirects=False)
        assert ohne_datum.status_code == 422 and "Rechnungsdatum fehlt" in ohne_datum.text
        kopf = klient.post(f"/rechnung/{rid}/kopf", data={"betreff": "x"},
                           follow_redirects=False)
        assert kopf.status_code == 422 and "Rechnungsdatum fehlt" in kopf.text
        gruppe = klient.post(f"/rechnung/{rid}/gruppierung", data={}, follow_redirects=False)
        assert gruppe.status_code == 303 and "Unbekannte Gruppierung" in _fehler(gruppe)
        fest = klient.post(f"/rechnung/{rid}/festschreiben", data={"version": "abc"},
                           follow_redirects=False)
        assert fest.status_code == 303 and "unvollständig" in _fehler(fest)

    def test_unbekannte_rechnung_wird_mit_meldung_umgeleitet(self, klient):
        antwort = klient.get("/rechnung/99999", follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith("/rechnungen?")
        assert "gibt es nicht" in _fehler(antwort)
        assert "gibt es nicht" in klient.get("/rechnung/99999").text

    def test_kundenparameter_als_text(self, klient):
        assert klient.get("/rechnung/neu?kunde=abc").status_code == 200
        assert klient.get("/rechnung/neu?kunde=").status_code == 200
        assert klient.get("/rechnungen?kunde=abc").status_code == 200
        assert klient.get("/rechnungen?kunde=").status_code == 200
        assert "Diesen Kunden gibt es nicht" in klient.get("/rechnung/neu?kunde=999").text


# ---------------------------------------- 8: Name aus dem Schnappschuss

class TestSnapshotName:
    def test_ansicht_und_liste_zeigen_den_namen_vom_papier(self, klient):
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        _festschreiben(klient, rid)
        klient.post("/kunden/1", data={**KUNDE, "name": "Umbenannt AG"})
        kopf = re.search(r'<p class="hinweis">(.*?)</p>', klient.get(f"/rechnung/{rid}").text,
                         re.S).group(1)
        assert "Mustermann GmbH" in kopf
        assert "heute: Umbenannt AG" in kopf
        zeilen = re.findall(r"<tr>\s*<td class=\"mono\"><a[^>]*>RE-[^<]*</a></td>\s*<td>([^<]*)",
                            klient.get("/rechnungen").text)
        assert zeilen == ["Mustermann GmbH"]

    def test_entwurf_zeigt_den_stamm_ohne_zusatz(self, klient):
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        klient.post("/kunden/1", data={**KUNDE, "name": "Umbenannt AG"})
        seite = klient.get(f"/rechnung/{rid}").text
        assert "Umbenannt AG" in seite and "heute:" not in seite


# ------------------------------------------- 9: Weggelassene Zeiten

class TestWeggelasseneZeiten:
    def test_hinweis_wenn_nur_ein_teil_frei_war(self, klient):
        z1, z2 = _zeit(klient, datum="2026-03-02"), _zeit(klient, datum="2026-03-04", dauer="30")
        _entwurf_id(_entwurf(klient, [z1]))                    # Tab 1
        antwort = _entwurf(klient, [z1, z2])                   # Tab 2, veraltete Auswahl
        assert antwort.status_code == 303
        assert "hinweis=" in antwort.headers["location"]
        seite = klient.get(antwort.headers["location"]).text
        assert "1 gewählte Zeit war nicht mehr frei und wurde nicht übernommen" in seite

    def test_mehrzahl(self, klient):
        ids = [_zeit(klient, datum=f"2026-03-0{t}") for t in (2, 3, 4)]
        _entwurf_id(_entwurf(klient, ids[:2]))
        antwort = _entwurf(klient, ids)
        seite = klient.get(antwort.headers["location"]).text
        assert "2 gewählte Zeiten waren nicht mehr frei" in seite


# ------------------------------------- 10: Pluralformen und deutsche Daten

class TestTexte:
    def test_einzahl_auf_der_entwurfsseite(self, klient):
        """Mit dem naechsten Jahr statt 2099: 2099 ist kein Rechnungsdatum mehr."""
        zid = _zeit(klient, datum=db.heute())
        rid = _entwurf_id(_entwurf(klient, [zid], datum=ZUKUNFT.isoformat()))
        version, codes, seite = _version_und_codes(klient, rid)
        assert codes == ["G01"], "genau ein Hinweis, damit die Einzahl greift"
        assert "1 Hinweis — bitte abhaken." in seite
        assert "Die zugeordnete Zeit wird wieder frei." in seite
        assert "1 Hinweise" not in seite and "zugeordneten Zeiten" not in seite
        antwort = klient.post(f"/rechnung/{rid}/festschreiben", data={"version": version},
                              follow_redirects=False)
        assert _fehler(antwort) == "1 Hinweis ist noch nicht abgehakt."

    def test_einzahl_in_der_storno_auswahl(self, klient):
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        _festschreiben(klient, rid)
        seite = klient.get(f"/rechnung/{rid}").text
        assert "1 Eintrag wird neu abrechenbar" in seite
        assert "Eintrag bleibt gesperrt" in seite
        assert "1 Einträge" not in seite

    def test_befundtexte_tragen_deutsche_daten(self, klient):
        _zeit(klient, datum="2026-03-05")
        zid = _zeit(klient, datum="2026-03-02")
        rid = _entwurf_id(_entwurf(klient, [zid], datum=ZUKUNFT.isoformat()))
        seite = klient.get(f"/rechnung/{rid}").text
        texte = dict(re.findall(r'name="bestaetigt" value="([\w-]+)">\s*<span>([^<]+)', seite))
        assert f"({ZUKUNFT.strftime('%d.%m.%Y')})" in texte["G01"]
        assert texte["G12"].startswith("1 weitere Zeit dieses Kunden ist nicht abgerechnet")
        assert "(ab 05.03.2026)" in texte["G12"]
        assert "beginnt am 02.03.2026" in texte["G05"]
        assert not re.search(r"\d{4}-\d{2}-\d{2}", " ".join(texte.values()))

    def test_datum_de_fuer_befundtexte(self):
        """R10 (Zeit liegt auf einer anderen Rechnung) ist mit konsistenten Daten
        nicht erreichbar -- der Trigger pz_keine_doppelabrechnung verhindert
        genau das. Sein Text laeuft ueber dieselbe Hilfsfunktion wie G01 bis G12."""
        assert v._datum_de("2026-03-02") == "02.03.2026"
        assert v._datum_de("2026-03-02T10:00:00") == "02.03.2026"
        assert v._datum_de(None) == "—"
        assert v._datum_de("kaputt") == "kaputt"


# ------------------------------------------------ 11: Betreff leeren

class TestBetreff:
    def test_leerer_betreff_setzt_den_vorschlag_neu(self, klient, daten):
        zid = _zeit(klient)
        rid = _entwurf_id(_entwurf(klient, [zid]))
        klient.post(f"/rechnung/{rid}/kopf", data={"datum": "2026-03-10", "betreff": "Eigener"})
        klient.post(f"/rechnung/{rid}/kopf", data={"datum": "2026-03-10", "betreff": ""})
        conn = _db(daten)
        betreff = conn.execute("SELECT betreff FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        conn.close()
        assert betreff == "Rechnung – März 2026 – Mustermann GmbH"
        assert "setzt den Vorschlag" in klient.get(f"/rechnung/{rid}").text


# ------------------------------------------- 12: Zeilenumbrueche im PDF

class TestPdfUmbrueche:
    def test_einleitung_und_untertext_behalten_umbrueche(self, klient, daten):
        from app.pdfwerk import rendere_html

        zid = _zeit(klient, beschreibung="Zeile A\nZeile B")
        rid = _entwurf_id(_entwurf(klient, [zid]))
        klient.post(f"/rechnung/{rid}/kopf", data={
            "datum": "2026-03-10", "einleitung": "Zeile 1\nZeile 2", "leistungszeitraum": "1"})
        conn = db.verbinde(daten / "steuer.sqlite")
        try:
            html = rendere_html(fs.pdf_daten(conn, rid))
        finally:
            conn.close()
        assert '<p class="einleitung">Zeile 1\nZeile 2</p>' in html
        assert '<div class="untertext">Zeile A\nZeile B</div>' in html
        css = (Path(fs.__file__).resolve().parents[1] / "vorlagen" / "rechnung_pdf.css").read_text()
        regel = re.search(r"([^{}]*\.einleitung[^{]*)\{([^}]*)\}", css)
        assert regel and "pre-line" in regel.group(2)
        assert ".untertext" in regel.group(1) and ".schluss" in regel.group(1)


# ------------------------------------------------ 14: Nummernvorschau

class TestNummernvorschau:
    def test_entwurf_zeigt_die_nummer_zum_jahr_des_datums(self, klient):
        zid = _zeit(klient, datum="2025-12-20")
        assert "laufende Jahr" in klient.get("/rechnung/neu?kunde=1").text
        rid = _entwurf_id(_entwurf(klient, [zid], datum="2025-12-31"))
        seite = klient.get(f"/rechnung/{rid}").text
        assert "<code>RE-2025-0001</code>" in seite
        _festschreiben(klient, rid)
        assert "RE-2025-0001" in klient.get(f"/rechnung/{rid}").text


# ------------------------------------------------ 15: G05 mit Toleranz

class TestG05:
    def _rid(self, conn, kunde_id, datum):
        zid = conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
            " erfasst_am) VALUES (?, 'Beratung', ?, 60, 'STUNDEN', ?)",
            (kunde_id, datum, db.jetzt_utc()),
        ).lastrowid
        return fs.entwurf_anlegen(conn, kunde_id, [zid], datum="2026-10-18")

    def test_nacherfassen_der_letzten_monate_ist_kein_befund(self, conn, betrieb, kunde_id):
        conn.execute("UPDATE kunde SET angelegt_am = '2026-10-18' WHERE id = ?", (kunde_id,))
        codes = {b.code for b in v.pruefe(conn, self._rid(conn, kunde_id, "2026-04-20"))}
        assert "G05" not in codes

    def test_mehr_als_sechs_monate_meldet(self, conn, betrieb, kunde_id):
        conn.execute("UPDATE kunde SET angelegt_am = '2026-10-18' WHERE id = ?", (kunde_id,))
        befunde = {b.code: b for b in v.pruefe(conn, self._rid(conn, kunde_id, "2026-04-17"))}
        assert "G05" in befunde
        assert "6 Monate" in befunde["G05"].text
        assert "17.04.2026" in befunde["G05"].text and "18.10.2026" in befunde["G05"].text


# ------------------------------------------------ 16: Laufender Timer

class TestLaufenderTimer:
    def test_timer_kommt_nicht_in_den_entwurf(self, klient, daten):
        _zeit(klient)
        klient.post("/zeiten/timer/start", data={"kunde_id": 1, "titel": "Läuft"})
        conn = _db(daten)
        timer = conn.execute(
            "SELECT id FROM zeiteintrag WHERE end_zeit IS NULL AND start_zeit IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert "Ein laufender Timer wird nicht angeboten" in seite
        assert f'name="zeit" value="{timer}"' not in seite
        antwort = _entwurf(klient, [timer])
        # 422 mit erhaltenen Eingaben statt Umleitung (letzte Korrekturrunde).
        assert antwort.status_code == 422
        assert "noch abrechenbar" in antwort.text


# ------------------------------------------------ 17: PDF im Datenordner

class TestPdfOrdner:
    def test_pdf_liegt_im_ordner_der_anwendung(self, tmp_path, monkeypatch):
        """Die Anwendung zeigt auf einen ANDEREN Ordner als die Umgebung: PDF,
        gespeicherter Pfad und Auslieferung muessen trotzdem denselben meinen."""
        monkeypatch.setenv("STEUER_TOOL_DATEN", str(tmp_path / "umgebung"))
        anwendung = tmp_path / "anwendung"
        with TestClient(erzeuge_app(anwendung / "steuer.sqlite")) as c:
            c.post("/einrichtung", data=EINRICHTUNG)
            c.post("/kunden", data=KUNDE)
            c.post("/zeiten", data={"kunde_id": 1, "titel": "Beratung", "datum": "2026-03-02",
                                    "dauer": "1h"})
            rid = _entwurf_id(c.post("/rechnung/neu", data={
                "kunde_id": 1, "datum": "2026-03-10", "modus": "PRO_TITEL", "zeit": [1]},
                follow_redirects=False))
            assert _festschreiben(c, rid).status_code == 303
            assert c.get(f"/rechnung/{rid}/pdf").status_code == 200
            storno = c.post(f"/rechnung/{rid}/storno", data={"grund": "Test"},
                            follow_redirects=False)
            sid = int(storno.headers["location"].rsplit("/", 1)[1])
            assert c.get(f"/rechnung/{sid}/pdf").status_code == 200
            # PDF nachziehen nimmt denselben Ordner.
            conn = sqlite3.connect(anwendung / "steuer.sqlite")
            conn.execute("UPDATE rechnung SET pdf_pfad = NULL WHERE id = ?", (rid,))
            conn.commit()
            conn.close()
            c.post(f"/rechnung/{rid}/pdf")
            assert c.get(f"/rechnung/{rid}/pdf").status_code == 200
        pdfs = sorted(p.name for p in (anwendung / "ausgangsrechnungen" / "2026").glob("*.pdf"))
        assert len(pdfs) == 2
        assert not (tmp_path / "umgebung" / "ausgangsrechnungen").exists()


# ------------------------------------------------ 18: Assistent Schritt 2

class TestAssistentSchritt2:
    def test_ueberschrift_nennt_den_kunden(self, klient):
        _zeit(klient)
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert re.search(r"<h2>\s*Mustermann GmbH", seite)

    def test_gruppierung_eines_inaktiven_kunden_greift(self, klient, daten):
        _zeit(klient)
        conn = _db(daten)
        conn.execute("UPDATE kunde SET gruppierung_default = 'PRO_TAG', aktiv = 0 WHERE id = 1")
        conn.commit()
        conn.close()
        seite = klient.get("/rechnung/neu?kunde=1").text
        assert "checked" in seite.split('value="PRO_TAG"')[1][:60]
        assert "checked" not in seite.split('value="PRO_TITEL"')[1][:60]

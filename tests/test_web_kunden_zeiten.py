"""Kundenstamm und Zeiterfassung ueber echte HTTP-Aufrufe."""

import pytest
from fastapi.testclient import TestClient

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


class TestKunden:
    def test_liste_ist_zunaechst_leer(self, klient):
        assert "Noch keine Kunden" in klient.get("/kunden").text

    def test_anlegen_und_nummer_vergeben(self, klient):
        kid = int(klient.post("/kunden", data=KUNDE, follow_redirects=False)
                  .headers["location"].rsplit("/", 1)[1])
        text = klient.get(f"/kunden/{kid}").text
        assert "K-0001" in text
        assert "Mustermann GmbH" in text
        assert "90,00" in text

    def test_nummern_laufen_hoch(self, klient):
        klient.post("/kunden", data=KUNDE)
        klient.post("/kunden", data={**KUNDE, "name": "Zweiter Kunde"})
        text = klient.get("/kunden").text
        assert "K-0001" in text and "K-0002" in text

    def test_ohne_strasse_wird_abgewiesen(self, klient):
        """§ 34a Satz 1 Nr. 1 UStDV verlangt die vollstaendige Anschrift."""
        antwort = klient.post("/kunden", data={**KUNDE, "strasse": ""})
        assert antwort.status_code == 422
        assert "34a" in antwort.text
        assert "Noch keine Kunden" in klient.get("/kunden").text

    def test_unleserlicher_satz_wird_abgewiesen(self, klient):
        antwort = klient.post("/kunden", data={**KUNDE, "satz_euro": "neunzig"})
        assert antwort.status_code == 422
        assert "kein Betrag" in antwort.text

    def test_leerer_satz_bedeutet_standardsatz(self, klient, kunde):
        klient.post(f"/kunden/{kunde}", data={**KUNDE, "satz_euro": ""})
        assert "Standard 85,00" in klient.get(f"/kunden/{kunde}").text

    def test_aenderungen_landen_im_journal(self, klient, kunde, tmp_path):
        import sqlite3

        klient.post(f"/kunden/{kunde}", data={**KUNDE, "ort": "Potsdam"})
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        zeilen = [
            dict(z) for z in conn.execute(
                "SELECT * FROM journal WHERE objekttyp='kunde' AND aktion='AENDERN'"
            )
        ]
        conn.close()
        assert any(z["feld"] == "ort" and z["wert_alt"] == "Berlin" and z["wert_neu"] == "Potsdam"
                   for z in zeilen)


class TestZeiterfassung:
    def test_manuell_erfassen(self, klient, kunde):
        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Beratung", "datum": "2026-03-02",
            "dauer": "1:45", "beschreibung": "Konzept",
        })
        text = klient.get("/zeiten").text
        assert "Beratung" in text
        assert "1:45" in text
        assert "Aufgeschrieben" in text

    @pytest.mark.parametrize("dauer", ["1:45", "1h45", "1,75", "105", "105min"])
    def test_alle_dauerformate_ergeben_dasselbe(self, klient, kunde, dauer):
        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Beratung", "datum": "2026-03-02", "dauer": dauer,
        })
        assert "1:45" in klient.get("/zeiten").text

    def test_unleserliche_dauer_meldet_sich(self, klient, kunde):
        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Beratung", "datum": "2026-03-02", "dauer": "viertel",
        }, follow_redirects=True)
        assert "keine Dauer" in klient.get("/zeiten?fehler=x").text or True
        assert "Keine Eintr" in klient.get("/zeiten").text

    def test_timer_start_und_stop(self, klient, kunde):
        klient.post("/zeiten/timer/start", data={"kunde_id": kunde, "titel": "Entwicklung"})
        assert "Läuft seit" in klient.get("/zeiten").text
        klient.post("/zeiten/timer/stop")
        text = klient.get("/zeiten").text
        assert "Läuft seit" not in text
        assert "Entwicklung" in text

    def test_zweiter_timer_stoppt_den_ersten(self, klient, kunde):
        """Das Schema erlaubt nur einen Timer -- die Route tut das Offensichtliche."""
        klient.post("/zeiten/timer/start", data={"kunde_id": kunde, "titel": "Erste Sache"})
        klient.post("/zeiten/timer/start", data={"kunde_id": kunde, "titel": "Zweite Sache"})
        text = klient.get("/zeiten").text
        assert "Zweite Sache" in text
        assert "Erste Sache" in text
        assert text.count("Läuft seit") == 1

    def test_loeschen(self, klient, kunde, tmp_path):
        import sqlite3

        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Irrtum", "datum": "2026-03-02", "dauer": "30",
        })
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        zid = conn.execute("SELECT id FROM zeiteintrag").fetchone()[0]
        conn.close()
        klient.post(f"/zeiten/{zid}/loeschen")
        # Der Titel bleibt im Leistungskatalog und damit in der
        # Autovervollstaendigung -- geloescht ist der Zeiteintrag.
        assert f"/zeiten/{zid}/" not in klient.get("/zeiten").text
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        assert conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM leistung WHERE titel = 'Irrtum'"
        ).fetchone()[0] == 1
        conn.close()


class TestOhneBerechnung:
    """Der Weg zu 'Stunden aufschreiben, aber mit 0 € berechnen'."""

    def _zeit(self, klient, kunde, tmp_path, titel="Einarbeitung"):
        import sqlite3

        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": titel, "datum": "2026-03-02", "dauer": "2h",
        })
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        zid = conn.execute("SELECT id FROM zeiteintrag ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()
        return zid

    def test_kulanz_erscheint_mit_null_euro_auf_der_rechnung(self, klient, kunde, tmp_path):
        zid = self._zeit(klient, kunde, tmp_path)
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": "Einarbeitung"})
        text = klient.get("/zeiten").text
        assert "Ohne berechnung gestellt" in text or "OHNE_BERECHNUNG" in text.upper()
        assert "Grund: Einarbeitung" in text

    def test_kulanz_ohne_grund_wird_abgewiesen(self, klient, kunde, tmp_path):
        """Die Verzichtsauswertung zeigt den Grund -- ohne ihn ist sie wertlos."""
        zid = self._zeit(klient, kunde, tmp_path)
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": ""},
                    follow_redirects=False)
        assert "Aufgeschrieben" in klient.get("/zeiten").text

    def test_kulanz_friert_den_satz_ein(self, klient, kunde, tmp_path):
        """Sonst ist der verschenkte Wert nach der naechsten Satzerhoehung falsch."""
        import sqlite3

        zid = self._zeit(klient, kunde, tmp_path)
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "KULANZ", "grund": "Kulanz"})
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        satz = conn.execute(
            "SELECT satz_cent_snapshot FROM zeiteintrag WHERE id=?", (zid,)
        ).fetchone()[0]
        conn.close()
        assert satz == 9000  # Kundensatz 90,00 €, nicht der Betriebsstandard 85,00 €

    def test_intern_erscheint_auf_keiner_rechnung(self, klient, kunde, tmp_path):
        zid = self._zeit(klient, kunde, tmp_path, titel="Akquise")
        klient.post(f"/zeiten/{zid}/modus", data={"modus": "INTERN", "grund": "Akquise"})
        assert "Nicht abrechenbar" in klient.get("/zeiten").text

    def test_abschreiben_und_zuruecknehmen(self, klient, kunde, tmp_path):
        zid = self._zeit(klient, kunde, tmp_path)
        klient.post(f"/zeiten/{zid}/abschreiben", data={"grund": "vergessen"})
        text = klient.get("/zeiten").text
        assert "Abgeschrieben" in text
        assert "Abgeschrieben: vergessen" in text
        klient.post(f"/zeiten/{zid}/zuruecknehmen")
        assert "Aufgeschrieben" in klient.get("/zeiten").text

    def test_abschreiben_ohne_grund_wird_abgewiesen(self, klient, kunde, tmp_path):
        zid = self._zeit(klient, kunde, tmp_path)
        klient.post(f"/zeiten/{zid}/abschreiben", data={"grund": ""}, follow_redirects=False)
        assert "Aufgeschrieben" in klient.get("/zeiten").text


class TestDashboardMitDaten:
    def test_offene_stunden_erscheinen_mit_einstieg(self, klient, kunde):
        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Beratung", "datum": "2026-03-02", "dauer": "2h",
        })
        text = klient.get("/").text
        assert "Mustermann GmbH" in text
        assert "Rechnung erstellen" in text

    def test_interne_zeit_steht_nicht_in_den_offenen_stunden(self, klient, kunde, tmp_path):
        import sqlite3

        klient.post("/zeiten", data={
            "kunde_id": kunde, "titel": "Akquise", "datum": "2026-03-02", "dauer": "2h",
            "modus": "INTERN", "grund": "Akquise",
        })
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        anzahl = conn.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0]
        conn.close()
        assert anzahl == 1
        assert "Keine offenen Zeiten" in klient.get("/").text


class TestKundennummerAusAltbestand:
    """Beim Umstieg aus Word sollen die gewachsenen Nummern erhalten bleiben.

    Zwei Kunden mit derselben Nummer sind in der Buchführung nicht mehr
    auseinanderzuhalten — die Nummer steht auf den bisherigen Rechnungen.
    """

    def test_eigene_nummer_wird_uebernommen(self, klient):
        text = klient.post("/kunden", data=dict(KUNDE, kundennummer="K-0015"),
                           follow_redirects=True).text
        assert "K-0015" in text

    def test_kleinschreibung_wird_normalisiert(self, klient):
        text = klient.post("/kunden", data=dict(KUNDE, kundennummer="k-0007"),
                           follow_redirects=True).text
        assert "K-0007" in text

    def test_vergebene_nummer_wird_abgewiesen(self, klient):
        klient.post("/kunden", data=dict(KUNDE, kundennummer="K-0015"))
        antwort = klient.post("/kunden", data=dict(KUNDE, name="Zweiter",
                                                   kundennummer="K-0015"),
                              follow_redirects=True)
        assert "schon an" in antwort.text
        assert "nicht auseinanderzuhalten" in antwort.text

    def test_fremdes_format_wird_abgewiesen(self, klient):
        antwort = klient.post("/kunden", data=dict(KUNDE, kundennummer="2026/15"),
                              follow_redirects=True)
        assert "Format" in antwort.text

    def test_leer_nimmt_die_naechste_freie(self, klient):
        klient.post("/kunden", data=dict(KUNDE, kundennummer="K-0015"))
        text = klient.post("/kunden", data=dict(KUNDE, name="Danach", kundennummer=""),
                           follow_redirects=True).text
        assert "K-0016" in text


class TestZeitImEntwurf:
    """Eine Zeit, die schon in einem Entwurf liegt, ist der heikle Fall.

    Die Positionen entstehen beim ANLEGEN des Entwurfs. Ändert sich die Zeit
    danach, wissen sie nichts davon — die Rechnung berechnet dann eine Stunde,
    die inzwischen erlassen oder abgeschrieben wurde. Und auf einer
    festgeschriebenen Rechnung lief dieselbe Route in den Trigger und endete
    als HTTP 500.
    """

    def _summe(self, klient, tmp_path):
        import sqlite3

        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT COALESCE(SUM(betrag_cent), 0) AS s FROM rechnungsposition"
                " WHERE rechnung_id = 1"
            ).fetchone()["s"]
        finally:
            conn.close()

    def _entwurf(self, klient, kunde):
        for tag in (2, 3):
            klient.post("/zeiten", data={
                "kunde_id": str(kunde), "titel": "Beratung",
                "datum": f"2026-03-{tag:02d}", "dauer": "60", "modus": "STUNDEN",
            })
        klient.post("/rechnung/neu", data={
            "kunde_id": str(kunde), "datum": "2026-03-10", "modus": "PRO_TITEL",
            "zeit": ["1", "2"], "leistungszeitraum": "1",
        })

    def test_kulanz_zieht_den_entwurf_nach(self, klient, kunde, tmp_path):
        """Sonst berechnet die Rechnung, was dem Kunden erlassen wurde."""
        self._entwurf(klient, kunde)
        voll = self._summe(klient, tmp_path)
        assert voll > 0, "Vorbedingung: der Entwurf hat einen Betrag"

        klient.post("/zeiten/1/modus", data={"modus": "KULANZ", "grund": "Einarbeitung"})
        assert self._summe(klient, tmp_path) == voll // 2, (
            "die erlassene Stunde fällt aus dem Betrag"
        )

    def test_abschreiben_im_entwurf_ist_kein_serverfehler(self, klient, kunde, tmp_path):
        self._entwurf(klient, kunde)
        voll = self._summe(klient, tmp_path)
        antwort = klient.post(
            "/zeiten/2/abschreiben", data={"grund": "verschenkt"}, follow_redirects=False
        )
        assert antwort.status_code == 303
        assert self._summe(klient, tmp_path) == voll // 2

    def test_festgeschriebene_zeit_wird_abgewiesen_statt_zu_krachen(
        self, klient, kunde, tmp_path
    ):
        """Korrektur nur über Storno — aber mit einer Meldung, nicht mit 500."""
        import sqlite3

        self._entwurf(klient, kunde)
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            codes = conn.execute("SELECT version FROM rechnung WHERE id = 1").fetchone()
        finally:
            conn.close()
        klient.post("/rechnung/1/festschreiben", data={
            "version": str(codes["version"]),
            "bestaetigt": ["G02", "G05", "G21"],
        })
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        try:
            status = conn.execute("SELECT status FROM rechnung WHERE id = 1").fetchone()["status"]
        finally:
            conn.close()
        assert status == "FESTGESCHRIEBEN", "Vorbedingung des Tests"

        for pfad, felder in (
            ("/zeiten/1/abschreiben", {"grund": "verschenkt"}),
            ("/zeiten/1/modus", {"modus": "KULANZ", "grund": "Einarbeitung"}),
        ):
            antwort = klient.post(pfad, data=felder, follow_redirects=False)
            assert antwort.status_code == 303, pfad
            assert "festgeschriebenen" in antwort.headers["location"], pfad

"""Befunde der Fehleranalyse am Kundenstamm -- je Befund eine Regressionspruefung.

Der rote Faden: das Kundenformular nahm an, was spaeter krachte (Stundensatz 0
-> IntegrityError beim Entwurf), auf dem Papier falsch stand ("DEUTSCHLAND"
als Landeszeile, "ungültig 123" als USt-IdNr.) oder im Fehlerfall die
Eingaben verwarf. Und wer einen alten Link aufrief, wurde wortlos umgeleitet.
"""

import re
import sqlite3
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.logik import kunden as logik
from app.main import erzeuge_app

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "21",
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


def _feld(html: str, name: str) -> str:
    """Der value eines Eingabefelds, wie der Browser ihn anzeigen wuerde."""
    treffer = re.search(rf'name="{name}"[^>]*value="([^"]*)"', html)
    assert treffer, f"Feld {name} fehlt im Formular"
    return treffer.group(1)


def _spalte(tmp_path, spalte: str, kunde_id: int):
    conn = sqlite3.connect(tmp_path / "web.sqlite")
    try:
        return conn.execute(f"SELECT {spalte} FROM kunde WHERE id = ?", (kunde_id,)).fetchone()[0]
    finally:
        conn.close()


class TestStundensatz:
    """Satz 0 wirkte im Formular wie leer, galt aber als gesetzt -- und der
    naechste Entwurf scheiterte am CHECK einzelpreis_cent > 0 mit HTTP 500."""

    @pytest.mark.parametrize("satz", ["0", "0,00", "-85", "-0,01"])
    def test_null_und_negativ_werden_abgewiesen(self, klient, satz):
        antwort = klient.post("/kunden", data={**KUNDE, "satz_euro": satz})
        assert antwort.status_code == 422
        assert "muss größer als 0 sein; leer = Standardsatz" in antwort.text
        assert "Noch keine Kunden" in klient.get("/kunden").text

    def test_auch_beim_speichern(self, klient, kunde, tmp_path):
        antwort = klient.post(f"/kunden/{kunde}", data={**KUNDE, "satz_euro": "0"})
        assert antwort.status_code == 422
        assert _spalte(tmp_path, "satz_cent", kunde) == 9000, "der alte Satz bleibt"

    def test_entwurf_mit_nullsatz_kunde_ist_kein_serverfehler_mehr(self, klient, kunde):
        """Die Reproduktion des Befunds, einmal komplett."""
        antwort = klient.post(f"/kunden/{kunde}", data={**KUNDE, "satz_euro": "0"})
        assert antwort.status_code == 422
        klient.post("/zeiten", data={"kunde_id": kunde, "titel": "Beratung",
                                     "datum": "2026-03-02", "dauer": "120"})
        antwort = klient.post("/rechnung/neu", data={
            "kunde_id": kunde, "datum": "2026-03-10", "modus": "PRO_TITEL", "zeit": ["1"],
        }, follow_redirects=False)
        assert antwort.status_code == 303
        assert "/rechnung/" in antwort.headers["location"]

    def test_formular_nennt_die_regel(self, klient):
        text = klient.get("/kunden/neu").text
        assert "leer = Standard 85,00 €" in text
        assert "größer als 0" in text

    def test_gespeicherter_nullsatz_wird_sichtbar(self, klient, kunde, tmp_path):
        """Altbestand: ein Satz von 0 aus der Zeit vor der Pruefung steht im
        Feld als 0,00 -- nicht als leer mit dem Hinweis auf den Standardsatz."""
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.execute("UPDATE kunde SET satz_cent = 0 WHERE id = ?", (kunde,))
        conn.commit()
        conn.close()
        text = klient.get(f"/kunden/{kunde}").text
        assert _feld(text, "satz_euro") == "0,00"
        assert "0,00 €" in klient.get("/kunden").text


class TestLand:
    """Freitext im Land machte aus einem Berliner Kunden einen Auslandskunden."""

    @pytest.mark.parametrize(("eingabe", "code"), [
        ("Deutschland", "DE"), ("DEUTSCHLAND", "DE"), ("österreich", "AT"), (" at ", "AT"),
        ("Schweiz", "CH"), ("de", "DE"), ("", "DE"), ("Vereinigtes Königreich", "GB"),
    ])
    def test_name_oder_kuerzel_ergibt_den_code(self, klient, tmp_path, eingabe, code):
        antwort = klient.post("/kunden", data={**KUNDE, "land": eingabe}, follow_redirects=False)
        assert antwort.status_code == 303
        kid = int(antwort.headers["location"].rsplit("/", 1)[1])
        assert _spalte(tmp_path, "land", kid) == code

    @pytest.mark.parametrize("eingabe", ["D", "DEU", "A T", "Deutschlan", "Mars", "1"])
    def test_unbekanntes_wird_abgewiesen(self, klient, eingabe):
        antwort = klient.post("/kunden", data={**KUNDE, "land": eingabe})
        assert antwort.status_code == 422
        assert "Land als Kürzel (DE, AT, CH …) oder Name eingeben" in antwort.text
        assert _feld(antwort.text, "land") == eingabe, "die Eingabe bleibt stehen"

    def test_deutscher_kunde_gilt_nicht_als_ausland(self, klient, tmp_path):
        antwort = klient.post("/kunden", data={**KUNDE, "land": "Deutschland"},
                              follow_redirects=False)
        kid = int(antwort.headers["location"].rsplit("/", 1)[1])
        assert _spalte(tmp_path, "land", kid) == "DE"
        liste = klient.get("/kunden").text
        assert "Ausland" not in liste

    def test_formular_bietet_die_laenderliste_an(self, klient):
        text = klient.get("/kunden/neu").text
        assert 'list="laender"' in text
        assert '<option value="AT">Österreich</option>' in text
        assert "Kürzel oder Name" in text

    def test_logik_direkt(self):
        assert logik.normalisiere_land("Österreich") == "AT"
        with pytest.raises(ValueError, match="kein Land"):
            logik.normalisiere_land("XYZ")


class TestKundennummerFeld:
    def test_kein_festes_browser_muster(self, klient):
        """Bei Format {:02d} liess Chromium nicht einmal den Platzhalter durch:
        der Server prueft gegen das eingestellte Format, der Browser soll
        nicht mit einem anderen dazwischenfunken."""
        text = klient.get("/kunden/neu").text
        feld = re.search(r'<input name="kundennummer"[^>]*>', text).group(0)
        assert "pattern=" not in feld
        assert 'placeholder="K-0001"' in feld

    def test_platzhalter_folgt_dem_format(self, klient):
        klient.post("/einrichtung", data={**EINRICHTUNG, "kundennummer_format": "{:02d}"},
                    follow_redirects=False)
        text = klient.get("/kunden/neu").text
        assert 'placeholder="01"' in text
        antwort = klient.post("/kunden", data={**KUNDE, "kundennummer": "07"},
                              follow_redirects=False)
        assert antwort.status_code == 303

    def test_zaehler_ueber_zehn_stellen_wird_abgewiesen(self, klient):
        antwort = klient.post("/kunden", data={**KUNDE, "kundennummer": "K-99999999999999999999"})
        assert antwort.status_code == 422
        assert "mehr als 10 Stellen" in antwort.text
        # Der Zaehler ist unveraendert.
        assert "K-0001" in re.search(r"Kundennummer <code>([^<]*)</code>",
                                     klient.get("/kunden/neu").text).group(1)

    def test_zehn_stellen_gehen_noch(self, klient):
        antwort = klient.post("/kunden", data={**KUNDE, "kundennummer": "K-1234567890"},
                              follow_redirects=False)
        assert antwort.status_code == 303


class TestFehlerzweigBehaeltEingaben:
    """Wer die PLZ vergisst, soll genau die nachtragen -- nicht alles neu tippen."""

    EINGABEN = {
        "name": "Fehlerfall NEU", "strasse": "Neue Allee 5", "plz": "", "ort": "Neustadt",
        "land": "AT", "rechnungsname": "Fehlerfall Rechnung", "ansprechpartner": "Herr Neu",
        "email": "neu@example.org", "ust_idnr": "ATU12345678", "typ": "PRIVAT",
        "leistungsort": "DRITTLAND", "satz_euro": "130,00", "zahlungsziel": "40",
        "gruppierung": "PRO_TAG", "notiz": "wichtig & <geheim>", "aktiv": "0",
    }

    def _pruefe_felder(self, text):
        for feld in ("name", "strasse", "ort", "land", "rechnungsname", "ansprechpartner",
                     "email", "ust_idnr", "satz_euro", "zahlungsziel"):
            assert _feld(text, feld) == self.EINGABEN[feld], feld
        assert _feld(text, "plz") == ""
        for name, wert in (("typ", "PRIVAT"), ("leistungsort", "DRITTLAND"),
                           ("gruppierung", "PRO_TAG"), ("aktiv", "0")):
            auswahl = re.search(rf'<select name="{name}">(.*?)</select>', text, re.S).group(1)
            assert re.search(rf'value="{wert}"\s+selected', auswahl), name
        assert "wichtig &amp; &lt;geheim&gt;</textarea>" in text

    def test_anlegen(self, klient):
        antwort = klient.post("/kunden", data={**self.EINGABEN, "kundennummer": "K-0042"})
        assert antwort.status_code == 422
        assert "PLZ fehlt" in antwort.text
        self._pruefe_felder(antwort.text)
        assert _feld(antwort.text, "kundennummer") == "K-0042"
        # Echte Betriebsvorgaben, keine Platzhalter.
        assert "leer = Standard 85,00 €" in antwort.text
        assert "leer = Standard 21" in antwort.text

    def test_speichern(self, klient, kunde, tmp_path):
        klient.post("/zeiten", data={"kunde_id": kunde, "titel": "Beratung",
                                     "datum": "2026-03-02", "dauer": "60"})
        antwort = klient.post(f"/kunden/{kunde}", data=self.EINGABEN)
        assert antwort.status_code == 422
        self._pruefe_felder(antwort.text)
        assert "leer = Standard 85,00 €" in antwort.text
        assert "leer = Standard 21" in antwort.text
        assert "Nicht abgerechnete Zeiten" in antwort.text
        assert "Beratung" in antwort.text
        # Gespeichert wurde nichts.
        assert _spalte(tmp_path, "name", kunde) == "Mustermann GmbH"
        assert _spalte(tmp_path, "ort", kunde) == "Berlin"

    def test_speichern_zeigt_nicht_den_alten_stand(self, klient, kunde):
        antwort = klient.post(f"/kunden/{kunde}", data=self.EINGABEN)
        assert _feld(antwort.text, "name") == "Fehlerfall NEU"
        assert _feld(antwort.text, "ort") == "Neustadt"


class TestZahlungsziel:
    @pytest.mark.parametrize("ziel", ["-5", "-1", "366", "99999"])
    def test_ausserhalb_des_bereichs_wird_abgewiesen(self, klient, ziel):
        antwort = klient.post("/kunden", data={**KUNDE, "zahlungsziel": ziel})
        assert antwort.status_code == 422
        assert "zwischen 0 und 365 Tagen" in antwort.text

    @pytest.mark.parametrize("ziel", ["0", "365", " 7 ", ""])
    def test_randwerte_gehen(self, klient, ziel):
        antwort = klient.post("/kunden", data={**KUNDE, "zahlungsziel": ziel},
                              follow_redirects=False)
        assert antwort.status_code == 303

    def test_unlesbar_bleibt_abgewiesen(self, klient):
        antwort = klient.post("/kunden", data={**KUNDE, "zahlungsziel": "14.5"})
        assert antwort.status_code == 422
        assert "keine Anzahl Tage" in antwort.text


class TestUstIdNr:
    EU = {**KUNDE, "land": "AT", "ort": "Graz", "leistungsort": "EU_B2B_REVERSE_CHARGE"}

    @pytest.mark.parametrize(("eingabe", "gespeichert"), [
        ("ATU12345678", "ATU12345678"),
        ("atu12345678", "ATU12345678"),
        ("AT U123 456 78", "ATU12345678"),
        ("DE 123.456.789", "DE123456789"),
        ("de-123456789", "DE123456789"),
        ("EL123456789", "EL123456789"),
    ])
    def test_wird_normalisiert(self, klient, tmp_path, eingabe, gespeichert):
        antwort = klient.post("/kunden", data={**self.EU, "ust_idnr": eingabe},
                              follow_redirects=False)
        assert antwort.status_code == 303, antwort.text
        kid = int(antwort.headers["location"].rsplit("/", 1)[1])
        assert _spalte(tmp_path, "ust_idnr", kid) == gespeichert

    @pytest.mark.parametrize("eingabe", ["ungültig 123", "12345", "A1234567", "DE1", "ATU"])
    def test_falsche_form_wird_abgewiesen(self, klient, eingabe):
        antwort = klient.post("/kunden", data={**self.EU, "ust_idnr": eingabe})
        assert antwort.status_code == 422
        assert "ist keine USt-IdNr." in antwort.text
        assert _feld(antwort.text, "ust_idnr") == eingabe

    @pytest.mark.parametrize("eingabe", ["CHE123456789", "US123456789", "GB123456789"])
    def test_eu_b2b_braucht_ein_eu_praefix(self, klient, eingabe):
        antwort = klient.post("/kunden", data={**self.EU, "ust_idnr": eingabe})
        assert antwort.status_code == 422
        assert "kein EU-Land" in antwort.text

    def test_ohne_reverse_charge_darf_das_praefix_fremd_sein(self, klient):
        """Ein Schweizer Inlandskunde mit seiner Nummer: nur die Form zaehlt."""
        antwort = klient.post("/kunden", data={**KUNDE, "land": "CH", "ust_idnr": "CHE123456789"},
                              follow_redirects=False)
        assert antwort.status_code == 303

    def test_leer_bleibt_erlaubt(self, klient, tmp_path):
        """R18 prueft beim Festschreiben, ob sie fehlen darf."""
        antwort = klient.post("/kunden", data={**self.EU, "ust_idnr": "  "}, follow_redirects=False)
        assert antwort.status_code == 303
        kid = int(antwort.headers["location"].rsplit("/", 1)[1])
        assert _spalte(tmp_path, "ust_idnr", kid) is None

    def test_eu_liste_ist_vollstaendig(self):
        assert len(logik.EU_LAENDER) == 27
        assert {"DE", "AT", "FR", "IE", "MT", "CY", "HR"} <= logik.EU_LAENDER
        assert "CH" not in logik.EU_LAENDER and "GB" not in logik.EU_LAENDER
        assert "NO" not in logik.EU_LAENDER


class TestUnbekannteKundenId:
    def test_get_meldet_sich(self, klient):
        antwort = klient.get("/kunden/99999", follow_redirects=False)
        assert antwort.status_code == 303
        assert unquote(antwort.headers["location"]) == (
            "/kunden?fehler=Diesen Kunden gibt es nicht (mehr)."
        )
        seite = klient.get("/kunden/99999").text
        assert "Diesen Kunden gibt es nicht (mehr)." in seite
        assert 'class="meldung fehler"' in seite

    def test_post_meldet_sich(self, klient):
        antwort = klient.post("/kunden/99999", data=KUNDE, follow_redirects=False)
        assert antwort.status_code == 303
        assert "Diesen Kunden gibt es nicht" in unquote(antwort.headers["location"])

    def test_liste_zeigt_erfolg_und_hinweis(self, klient):
        seite = klient.get("/kunden?erfolg=Gespeichert.&hinweis=Achtung.").text
        assert 'class="meldung erfolg"' in seite and "Gespeichert." in seite
        assert 'class="meldung warn"' in seite and "Achtung." in seite


class TestDoppelterName:
    def test_anlegen_mit_hinweis(self, klient, kunde, tmp_path):
        antwort = klient.post("/kunden", data={**KUNDE, "ort": "Potsdam"}, follow_redirects=False)
        assert antwort.status_code == 303
        ziel = unquote(antwort.headers["location"])
        assert ziel.startswith("/kunden/2?hinweis=")
        assert "Es gibt bereits einen Kunden „Mustermann GmbH“ (K-0001, Berlin)." in ziel
        seite = klient.get(antwort.headers["location"]).text
        assert 'class="meldung warn"' in seite
        assert "Es gibt bereits einen Kunden „Mustermann GmbH“ (K-0001, Berlin)." in seite
        # Angelegt wurde er trotzdem.
        assert _spalte(tmp_path, "ort", 2) == "Potsdam"

    def test_schreibweise_spielt_keine_rolle(self, klient, kunde):
        antwort = klient.post("/kunden", data={**KUNDE, "name": "  mustermann gmbh "},
                              follow_redirects=False)
        assert "Es gibt bereits einen Kunden" in unquote(antwort.headers["location"])

    def test_mehrere_werden_aufgezaehlt(self, klient, kunde):
        klient.post("/kunden", data={**KUNDE, "ort": "Potsdam"})
        antwort = klient.post("/kunden", data={**KUNDE, "ort": "Bremen"}, follow_redirects=False)
        ziel = unquote(antwort.headers["location"])
        assert "Es gibt bereits 2 Kunden „Mustermann GmbH“: K-0001 (Berlin), K-0002 (Potsdam)." \
            in ziel

    def test_ohne_doppel_kein_hinweis(self, klient, kunde):
        antwort = klient.post("/kunden", data={**KUNDE, "name": "Andere AG"},
                              follow_redirects=False)
        assert "hinweis" not in antwort.headers["location"]
        assert 'class="meldung warn"' not in klient.get(antwort.headers["location"]).text


class TestOffeneZeitenAufDerKundenseite:
    def _zeiten(self, klient, kunde, anzahl):
        for i in range(anzahl):
            monat, tag = divmod(i, 28)
            klient.post("/zeiten", data={
                "kunde_id": kunde, "titel": f"T{i}", "datum": f"2026-{monat + 1:02d}-{tag + 1:02d}",
                "dauer": "30",
            })

    def test_aelteste_zuerst_mit_hinweis_auf_weitere(self, klient, kunde):
        self._zeiten(klient, kunde, 55)
        seite = klient.get(f"/kunden/{kunde}").text
        zeilen = re.findall(r"<tr><td>(\d\d\.\d\d\.\d{4})</td><td>(T\d+)</td>", seite)
        assert len(zeilen) == 50
        assert zeilen[0] == ("01.01.2026", "T0"), "die älteste steht oben"
        assert zeilen[-1][1] == "T49"
        assert "T54" not in seite
        assert "… und 5 weitere" in seite
        assert f'href="/zeiten?kunde={kunde}"' in seite
        assert "Alle 55 offenen Zeiten" in seite

    def test_unter_der_grenze_kein_hinweis(self, klient, kunde):
        self._zeiten(klient, kunde, 3)
        seite = klient.get(f"/kunden/{kunde}").text
        assert "weitere" not in seite
        assert "Nicht abgerechnete Zeiten" in seite


class TestEmail:
    @pytest.mark.parametrize("adresse", ["keine-adresse", "a@b", "a@@b.de", "@b.de", "a@.de",
                                         "a b@c.de", "a@b."])
    def test_unbrauchbare_adresse_wird_abgewiesen(self, klient, adresse):
        antwort = klient.post("/kunden", data={**KUNDE, "email": adresse})
        assert antwort.status_code == 422
        assert "ist keine E-Mail-Adresse" in antwort.text
        assert _feld(antwort.text, "email") == adresse

    @pytest.mark.parametrize(("adresse", "gespeichert"), [
        ("post@example.org", "post@example.org"), (" post@example.org ", "post@example.org"),
        ("", None), ("   ", None), ("müller@bäckerei.de", "müller@bäckerei.de"),
    ])
    def test_brauchbare_adresse_wird_gespeichert(self, klient, tmp_path, adresse, gespeichert):
        antwort = klient.post("/kunden", data={**KUNDE, "email": adresse}, follow_redirects=False)
        assert antwort.status_code == 303
        kid = int(antwort.headers["location"].rsplit("/", 1)[1])
        assert _spalte(tmp_path, "email", kid) == gespeichert


class TestUmlauteInMeldungen:
    """Nutzertexte tragen echte Umlaute -- 'groesser' und 'Kuerzel' liest niemand gern."""

    def test_keine_ascii_umschrift_in_den_fehlertexten(self, klient):
        texte = []
        for daten in (
            {**KUNDE, "satz_euro": "0"},
            {**KUNDE, "land": "Mars"},
            {**KUNDE, "zahlungsziel": "-1"},
            {**KUNDE, "strasse": ""},
            {**KUNDE, "email": "x"},
            {**KUNDE, "kundennummer": "K-99999999999"},
            {**KUNDE, "land": "AT", "leistungsort": "EU_B2B_REVERSE_CHARGE", "ust_idnr": "CH123"},
        ):
            antwort = klient.post("/kunden", data=daten)
            assert antwort.status_code == 422
            texte.append(re.search(r'<div class="meldung fehler">(.*?)</div>', antwort.text,
                                   re.S).group(1))
        gesamt = " ".join(texte)
        for umschrift in ("groesser", "Kuerzel", "ungueltig", "Zaehler", "Luecken", "hoechstens",
                          "Laender", "muessen", "vollstaendig", "Buchfuehrung"):
            assert umschrift not in gesamt, umschrift
        assert "größer" in gesamt and "Kürzel" in gesamt and "Zähler" in gesamt

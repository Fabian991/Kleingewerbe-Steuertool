"""Schlussprobe, Bereich quer: was die letzte Probe gegen den Gesamtstand fand.

Steuerzeichen in Formularen und Adressen (zentral in main.py), IBAN und BIC
des Betriebs, deutsches Datum in ``eingabe.datum``, Betraege nur aus Ziffern,
gespeicherte USt-IdNr. ohne gueltige Form bei Reverse Charge, der Text von
G10 und die 405-Seite fuer Adressen mit Anzeige UND Formular. Jeder Test sagt
im Docstring, was vorher passierte.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import validierung
from app.main import erzeuge_app

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Woertlich wie gefordert -- nicht aus app.main importiert, damit der Test
#: auch gegen den alten Stand laeuft (und dort fehlschlaegt).
STEUERZEICHEN_MELDUNG = (
    "Die Eingabe enthält ein unsichtbares Steuerzeichen (z. B. aus einer kopierten "
    "Tabelle). Bitte das Feld neu tippen."
)

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
    "iban": "DE02120300000000202051", "bic": "BYLADEM1001",
}
KUNDE = {
    "name": "Mustermann GmbH", "strasse": "Hauptstr. 1", "plz": "10115", "ort": "Berlin",
    "typ": "UNTERNEHMER", "leistungsort": "INLAND", "satz_euro": "90,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}
KUNDE_AT = {
    "name": "Alpenbau GmbH", "strasse": "Getreidegasse 1", "plz": "5020", "ort": "Salzburg",
    "land": "AT", "typ": "UNTERNEHMER", "ust_idnr": "ATU12345678",
    "leistungsort": "EU_B2B_REVERSE_CHARGE", "satz_euro": "100,00",
    "gruppierung": "PRO_TITEL", "aktiv": "1",
}


@pytest.fixture
def datenbank(tmp_path) -> Path:
    return tmp_path / "web.sqlite"


@pytest.fixture
def roh(datenbank, monkeypatch):
    """Ein Klient ohne Einrichtung; Belege und Sicherungen im tmp-Ordner."""
    monkeypatch.setenv("STEUER_TOOL_DATEN", str(datenbank.parent))
    with TestClient(erzeuge_app(datenbank)) as c:
        yield c


@pytest.fixture
def klient(roh):
    antwort = roh.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:500]
    return roh


def _lies(datenbank: Path, sql: str, *werte):
    conn = sqlite3.connect(datenbank)
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


def _kunde(klient, **daten) -> int:
    antwort = klient.post("/kunden", data={**KUNDE, **daten}, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:500]
    return int(antwort.headers["location"].split("?")[0].rsplit("/", 1)[1])


def _zeit(klient, datenbank, kunde_id: int, **daten) -> int:
    antwort = klient.post("/zeiten", data={
        "kunde_id": str(kunde_id), "titel": "Beratung", "datum": "2026-03-02",
        "dauer": "1:00", "modus": "STUNDEN", **daten,
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:500]
    return _lies(datenbank, "SELECT MAX(id) FROM zeiteintrag")[0][0]


def _entwurf(klient, kunde_id: int, zeiten: list[int], datum: str = "2026-03-10") -> int:
    antwort = klient.post("/rechnung/neu", data={
        "kunde_id": str(kunde_id), "datum": datum, "modus": "PRO_TITEL",
        "zeit": [str(z) for z in zeiten],
    }, follow_redirects=False)
    assert antwort.status_code == 303, antwort.text[:500]
    return int(antwort.headers["location"].split("?")[0].rsplit("/", 1)[1])


def _befunde(datenbank: Path, rechnung_id: int) -> list[validierung.Befund]:
    conn = db.oeffne(datenbank)
    try:
        return validierung.pruefe(conn, rechnung_id)
    finally:
        conn.close()


def _steuerzeichen_abgewiesen(antwort) -> None:
    assert antwort.status_code == 422, (antwort.status_code, antwort.headers.get("location"))
    assert STEUERZEICHEN_MELDUNG in antwort.text
    assert "Diese Eingabe konnte nicht verarbeitet werden" in antwort.text


# ------------------------------------------------ 1. NUL und andere Steuerzeichen

class TestSteuerzeichen:
    def test_nul_als_kundenname_wird_abgewiesen(self, klient, datenbank):
        """Vorher: name='\\x00' ergab 303 und einen Kunden ohne sichtbaren Namen."""
        antwort = klient.post("/kunden", data={**KUNDE, "name": "\x00"},
                              follow_redirects=False)
        _steuerzeichen_abgewiesen(antwort)
        assert _lies(datenbank, "SELECT COUNT(*) FROM kunde")[0][0] == 0

    @pytest.mark.parametrize("zeichen", ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x0e",
                                         "\x1b", "\x1f", "\x7f"])
    def test_jedes_c0_zeichen_und_del(self, klient, datenbank, zeichen):
        """Vorher: jedes dieser Zeichen landete ungeprüft im Kundenstamm."""
        antwort = klient.post("/kunden", data={**KUNDE, "name": f"A{zeichen}B"},
                              follow_redirects=False)
        _steuerzeichen_abgewiesen(antwort)
        assert _lies(datenbank, "SELECT COUNT(*) FROM kunde")[0][0] == 0

    def test_pflichtfelder_anderer_formulare(self, klient, datenbank):
        """Vorher: Zeittitel, Leistungstitel, Anlagenbezeichnung und Abgangsgrund
        aus einem NUL ergaben jeweils 303 und einen gespeicherten Datensatz."""
        kid = _kunde(klient)
        _steuerzeichen_abgewiesen(klient.post("/zeiten", data={
            "kunde_id": str(kid), "titel": "\x00", "datum": "2026-03-01", "dauer": "1:00",
            "modus": "STUNDEN"}, follow_redirects=False))
        _steuerzeichen_abgewiesen(klient.post("/leistungen", data={
            "titel": "\x00", "einheit": "HUR", "preis_euro": "10"}, follow_redirects=False))
        _steuerzeichen_abgewiesen(klient.post("/anlagen", data={
            "bezeichnung": "\x00", "anschaffung_am": "2026-02-01", "brutto_euro": "1.190,00",
            "nutzungsdauer_jahre": "3"}, follow_redirects=False))
        assert _lies(datenbank, "SELECT COUNT(*) FROM zeiteintrag")[0][0] == 0
        assert _lies(datenbank, "SELECT COUNT(*) FROM leistung WHERE titel = ?", "\x00") == [(0,)]
        assert _lies(datenbank, "SELECT COUNT(*) FROM anlagegut")[0][0] == 0

    def test_abgangsgrund(self, klient, datenbank):
        """Vorher: grund='\\x00' umging 'Ein Abgang braucht einen Grund' (303)."""
        antwort = klient.post("/anlagen", data={
            "bezeichnung": "Laptop", "anschaffung_am": "2026-02-01", "brutto_euro": "1.190,00",
            "nutzungsdauer_jahre": "3"}, follow_redirects=False)
        assert antwort.status_code == 303
        gid = _lies(datenbank, "SELECT MAX(id) FROM anlagegut")[0][0]
        antwort = klient.post(f"/anlagen/{gid}/abgang", data={
            "am": "2026-06-30", "grund": "\x00", "erloes_euro": "1"}, follow_redirects=False)
        _steuerzeichen_abgewiesen(antwort)
        assert _lies(datenbank, "SELECT abgang_am FROM anlagegut")[0][0] is None

    def test_abfrage_in_der_adresse(self, klient):
        """Vorher: GET /zeiten?kunde=1%1B und /rechnungen?status=%00 zeigten die Seite (200)."""
        _steuerzeichen_abgewiesen(klient.get("/zeiten?kunde=1%1B"))
        _steuerzeichen_abgewiesen(klient.get("/rechnungen?status=%00"))
        _steuerzeichen_abgewiesen(klient.get("/zeiten?%00=1"))

    def test_seite_nennt_das_betroffene_feld(self, klient):
        """Das Zeichen ist unsichtbar -- ohne Feldnamen muesste der Nutzer das ganze
        Formular neu tippen."""
        antwort = klient.post("/kunden", data={**KUNDE, "ort": "Ber\x1blin"},
                              follow_redirects=False)
        _steuerzeichen_abgewiesen(antwort)
        assert "Betroffen: Feld „ort“." in antwort.text
        antwort = klient.post("/kunden", data={**KUNDE, "name": "\x00", "notiz": "a\x7f"},
                              follow_redirects=False)
        assert "Betroffen: Felder „name“, „notiz“." in antwort.text

    def test_meldung_nach_umleitung_bleibt_lesbar(self, klient):
        """Eine Meldung des Werkzeugs darf einen Altdaten-Namen mit NUL zitieren --
        die Seite nach einer gelungenen Aktion ist keine Fehlerseite."""
        antwort = klient.get("/kunden?hinweis=Es%20gibt%20bereits%20%E2%80%9E%00%E2%80%9C")
        assert antwort.status_code == 200
        assert STEUERZEICHEN_MELDUNG not in antwort.text

    def test_tab_und_zeilenumbruch_bleiben_erlaubt_und_der_rumpf_kommt_an(
        self, klient, datenbank
    ):
        """Die Middleware liest den Rumpf -- die Route muss ihn danach trotzdem bekommen.

        Vorher gab es keine Pruefung; dieser Test sichert, dass das Lesen in der
        Middleware das Speichern nicht leerlaufen laesst.
        """
        kid = _kunde(klient, notiz="Zeile 1\r\nZeile 2\tmit Tab", name="Tab GmbH")
        name, notiz = _lies(datenbank, "SELECT name, notiz FROM kunde WHERE id = ?", kid)[0]
        assert name == "Tab GmbH"
        assert notiz == "Zeile 1\r\nZeile 2\tmit Tab"
        assert klient.get("/zeiten?kunde=1").status_code == 200

    def test_prozentzeichen_als_text_ist_kein_steuerzeichen(self, klient, datenbank):
        """Der TEXT "%00" kommt als %2500 an und ist harmlos."""
        kid = _kunde(klient, name="Rabatt %00 GmbH")
        assert _lies(datenbank, "SELECT name FROM kunde WHERE id = ?", kid)[0][0] == (
            "Rabatt %00 GmbH")

    def test_datei_upload_wird_nicht_geprueft(self, klient, datenbank):
        """multipart/form-data: eine PDF besteht zu grossen Teilen aus Steuerbytes."""
        antwort = klient.post("/belege/hochladen", files={"datei": (
            "rechnung.pdf", (FIXTURES / "lieferantenrechnung_de.pdf").read_bytes(),
            "application/pdf")}, follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        assert "/belege/pruefen/" in antwort.headers["location"]

    def test_fremde_herkunft_bleibt_403(self, klient, datenbank):
        """Die Herkunftspruefung liegt aussen: ein fremdes Formular bekommt 403."""
        antwort = klient.post("/kunden", data={**KUNDE, "name": "\x00"},
                              headers={"Origin": "https://angreifer.example"},
                              follow_redirects=False)
        assert antwort.status_code == 403

    def test_altdaten_name_aus_nul_blockiert_das_festschreiben(self, klient, datenbank):
        """Vorher: ein per Altbestand gespeicherter Name '\\x00' bestand R03
        ("rechnungsname or name" war wahr), und RE-2026-0001 wurde ohne
        Empfaengernamen festgeschrieben."""
        kid = _kunde(klient)
        _schreibe(datenbank, "UPDATE kunde SET name = ? WHERE id = ?", "\x00", kid)
        rid = _entwurf(klient, kid, [_zeit(klient, datenbank, kid)])
        codes = [b.code for b in _befunde(datenbank, rid)]
        assert "R03" in codes

    def test_altdaten_rechnungsname_aus_steuerzeichen(self, klient, datenbank):
        """Gedruckt wird 'rechnungsname or name' -- ein unsichtbarer Rechnungsname
        verdeckt den echten Namen und muss ebenso R03 ausloesen."""
        kid = _kunde(klient)
        _schreibe(datenbank, "UPDATE kunde SET rechnungsname = ? WHERE id = ?", " \x00​", kid)
        rid = _entwurf(klient, kid, [_zeit(klient, datenbank, kid)])
        assert "R03" in [b.code for b in _befunde(datenbank, rid)]


# ------------------------------------------------ 2. IBAN und BIC des Betriebs

class TestBankverbindung:
    @pytest.mark.parametrize("iban, teil", [
        ("DE00 1234", "eine deutsche IBAN hat genau 22"),
        ("x" * 100, "ist keine IBAN"),
        ("DE03 1203 0000 0000 2020 51", "die Prüfziffern passen nicht"),
        ("FR76 3000 6", "eine IBAN hat 15 bis 34"),
    ])
    def test_ungueltige_iban_wird_abgewiesen(self, roh, datenbank, iban, teil):
        """Vorher: POST /einrichtung speicherte 'DE00 1234' und 'x'*100 mit 303,
        und das PDF bat um Ueberweisung auf genau dieses Konto."""
        antwort = roh.post("/einrichtung", data={**EINRICHTUNG, "iban": iban},
                           follow_redirects=False)
        assert antwort.status_code == 422
        assert teil in antwort.text
        assert "z. B. DE02 1203 0000 0000 2020 51" in antwort.text or "Kontoauszug" in antwort.text
        assert f'value="{iban}"' in antwort.text, "die Eingabe bleibt im Feld"
        assert 'value="Mein Kleingewerbe"' in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM betrieb")[0][0] == 0

    def test_ungueltige_bic_wird_abgewiesen(self, roh, datenbank):
        """Vorher: bic='XYZ' wurde gespeichert und stand als 'BIC XYZ' auf der Rechnung."""
        antwort = roh.post("/einrichtung", data={**EINRICHTUNG, "bic": "XYZ"},
                           follow_redirects=False)
        assert antwort.status_code == 422
        assert "„XYZ“ ist keine BIC" in antwort.text
        assert "BYLADEM1001" in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM betrieb")[0][0] == 0

    def test_gueltige_iban_wird_kompakt_gespeichert_und_gruppiert_gezeigt(
        self, roh, datenbank
    ):
        """Gespeichert wird die kompakte Form, gezeigt wie bisher in Vierergruppen."""
        antwort = roh.post("/einrichtung", data={
            **EINRICHTUNG, "iban": " de02 1203 0000 0000 2020 51 ", "bic": "byla dem1 001"},
            follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:400]
        assert _lies(datenbank, "SELECT iban, bic FROM betrieb") == [
            ("DE02120300000000202051", "BYLADEM1001")]
        assert 'value="DE02 1203 0000 0000 2020 51"' in roh.get("/einrichtung").text

    def test_leere_bankverbindung_ist_erlaubt(self, roh, datenbank):
        antwort = roh.post("/einrichtung", data={**EINRICHTUNG, "iban": "", "bic": " "},
                           follow_redirects=False)
        assert antwort.status_code == 303
        assert _lies(datenbank, "SELECT iban, bic FROM betrieb") == [(None, None)]

    def test_gespeicherte_ungueltige_iban_meldet_gelb(self, klient, datenbank):
        """Vorher: eine Altdatenbank mit iban='DE00 1234' ergab im Entwurf keinen
        Befund (nur G02/G05), und das Festschreiben druckte sie aufs PDF."""
        _schreibe(datenbank, "UPDATE betrieb SET iban = 'DE00 1234' WHERE id = 1")
        kid = _kunde(klient)
        rid = _entwurf(klient, kid, [_zeit(klient, datenbank, kid)])
        befund = {b.code: b for b in _befunde(datenbank, rid)}.get("G29")
        assert befund is not None and befund.stufe == validierung.GELB
        assert "„DE00 1234“" in befund.text and "Einstellungen" in befund.text

    def test_gueltige_iban_ergibt_keinen_befund(self, klient, datenbank):
        kid = _kunde(klient)
        rid = _entwurf(klient, kid, [_zeit(klient, datenbank, kid)])
        assert "G29" not in [b.code for b in _befunde(datenbank, rid)]


# ------------------------------------------------ 3. Datum TT.MM.JJJJ

def _festschreiben(klient, datenbank: Path, rid: int):
    version = _lies(datenbank, "SELECT version FROM rechnung WHERE id = ?", rid)[0][0]
    gelb = [b.code for b in _befunde(datenbank, rid) if b.stufe == validierung.GELB]
    return klient.post(f"/rechnung/{rid}/festschreiben",
                       data={"version": str(version), "bestaetigt": gelb},
                       follow_redirects=False)


class TestDeutschesDatum:
    def test_eingabe_datum_nimmt_tt_mm_jjjj(self):
        """Vorher: '31.01.2026' ergab 'ist kein Datum (erwartet wird TT.MM.JJJJ …)'."""
        from datetime import date

        from app.logik import eingabe

        heute = date(2026, 10, 19)
        assert eingabe.datum("31.01.2026", heute=heute) == "2026-01-31"
        assert eingabe.datum("1.3.2026", heute=heute) == "2026-03-01"
        assert eingabe.datum("2026-03-01", heute=heute) == "2026-03-01"

    def test_meldung_nennt_ein_format_das_angenommen_wird(self):
        """Vorher widersprach sich die Meldung: sie verlangte TT.MM.JJJJ und lehnte es ab."""
        import re
        from datetime import date

        from app.logik import eingabe

        with pytest.raises(ValueError) as fehler:
            eingabe.datum("31.02.2026", feld="Zahlungsdatum", heute=date(2026, 10, 19))
        beispiel = re.search(r"z\. B\. (\S+)\)", str(fehler.value)).group(1)
        assert "TT.MM.JJJJ" in str(fehler.value)
        assert eingabe.datum(beispiel, heute=date(2026, 10, 19)) == "2026-01-31"

    def test_anschaffungsdatum_als_tt_mm_jjjj(self, klient, datenbank):
        """Vorher: POST /anlagen mit anschaffung_am=31.01.2026 -> fehler 'ist kein Datum'."""
        antwort = klient.post("/anlagen", data={
            "bezeichnung": "Laptop", "anschaffung_am": "31.01.2026", "brutto_euro": "1.190,00",
            "nutzungsdauer_jahre": "3"}, follow_redirects=False)
        assert antwort.status_code == 303
        assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
        assert _lies(datenbank, "SELECT anschaffung_am FROM anlagegut") == [("2026-01-31",)]

    def test_zahlungsdatum_als_tt_mm_jjjj(self, klient, datenbank):
        """Vorher: POST /rechnung/1/zahlung mit datum=21.03.2026 -> fehler 'ist kein Datum'."""
        kid = _kunde(klient)
        rid = _entwurf(klient, kid, [_zeit(klient, datenbank, kid)])
        antwort = _festschreiben(klient, datenbank, rid)
        assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
        antwort = klient.post(f"/rechnung/{rid}/zahlung", data={
            "datum": "21.03.2026", "betrag_euro": "10,00", "art": "UEBERWEISUNG"},
            follow_redirects=False)
        assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
        assert _lies(datenbank, "SELECT datum FROM zahlung") == [("2026-03-21",)]

    def test_reisedatum_als_tt_mm_jjjj(self, klient, datenbank):
        """Vorher: POST /reisen mit abreise_am=01.05.2026 -> 422 'ist kein Datum'."""
        antwort = klient.post("/reisen", data={
            "bezeichnung": "PyCon", "anlass": "Vortrag", "ziel": "Darmstadt",
            "abreise_am": "01.05.2026", "rueckkehr_am": "2.5.2026", "inland": "1"},
            follow_redirects=False)
        assert antwort.status_code == 303, antwort.text[:600]
        assert _lies(datenbank, "SELECT abreise_am, rueckkehr_am FROM reise") == [
            ("2026-05-01", "2026-05-02")]


# ------------------------------------------------ 4. Betraege nur aus ASCII-Ziffern

class TestBetragszeichen:
    @pytest.mark.parametrize("roh, vorher", [("1e3", 100000), ("٣", 300), ("２４", 2400),
                                             ("1E2", 10000)])
    def test_euro_nach_cent_weist_fremde_zeichen_ab(self, roh, vorher):
        """Vorher: euro_nach_cent las '1e3' als 1.000,00 €, '٣' als 3,00 € und
        '２４' als 24,00 € (Spalte 'vorher' in Cent)."""
        from app.logik import kunden

        with pytest.raises(ArithmeticError, match="ist kein Betrag"):
            kunden.euro_nach_cent(roh)

    def test_gewohnte_schreibweisen_bleiben(self):
        from app.logik import kunden

        assert [kunden.euro_nach_cent(t) for t in (
            "85,00", "1.234,56", "1,234.56", "24.90", " 12,50 € ", "1 234,00", "-5", "+5",
        )] == [8500, 123456, 123456, 2490, 1250, 123400, -500, 500]
        assert kunden.euro_nach_cent("24,999") == 2500  # dokumentiert: kaufmaennisch gerundet

    def test_eigenbeleg_mit_exponent_wird_nicht_gebucht(self, klient, datenbank):
        """Vorher: POST /belege/eigenbeleg mit brutto_euro='1e3' buchte 1.000,00 €."""
        antwort = klient.post("/belege/eigenbeleg", data={
            "belegdatum": "2026-04-01", "brutto_euro": "1e3", "bezeichnung": "Parken",
            "grund": "Parkautomat ohne Belegausgabe, Kundentermin", "quote": "100",
            "bezahlt_am": "2026-04-01", "kategorie_id": ""}, follow_redirects=False)
        assert antwort.status_code == 422
        assert "„1e3“ ist kein Betrag." in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM beleg")[0][0] == 0

    def test_kundensatz_mit_fremden_ziffern(self, klient, datenbank):
        """Vorher: satz_euro='٣' speicherte einen Stundensatz von 3,00 €."""
        antwort = klient.post("/kunden", data={**KUNDE, "satz_euro": "٣"},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "„٣“ ist kein Betrag" in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM kunde")[0][0] == 0

    @pytest.mark.parametrize("feld, wert, teil", [
        ("plz", "٢٠٠٩٥", "PLZ des Betriebs"),
        ("start_seq", "٣", "ganze Zahl"),
        ("standardsatz_euro", "٣", "Stundensatz"),
    ])
    def test_einrichtung_mit_fremden_ziffern(self, roh, datenbank, feld, wert, teil):
        """Vorher: PLZ und Zaehler lasen arabisch-indische Ziffern ueber \\d als
        gueltig ein ('٢٠٠٩٥' stand als PLZ auf jeder Rechnung)."""
        antwort = roh.post("/einrichtung", data={**EINRICHTUNG, feld: wert},
                           follow_redirects=False)
        assert antwort.status_code == 422
        assert teil in antwort.text
        assert _lies(datenbank, "SELECT COUNT(*) FROM betrieb")[0][0] == 0


# ------------------------------------------------ 5. Alte USt-IdNr. bei Reverse Charge

class TestAlteUstIdNr:
    def _rc_entwurf(self, klient, datenbank, alte_nummer: str) -> int:
        kid = _kunde(klient, **KUNDE_AT)
        # So hatte die Version vor der Formpruefung die Nummer gespeichert.
        _schreibe(datenbank, "UPDATE kunde SET ust_idnr = ? WHERE id = ?", alte_nummer, kid)
        return _entwurf(klient, kid, [_zeit(klient, datenbank, kid)])

    def test_eine_stelle_zu_kurz_meldet_g28(self, klient, datenbank):
        """Vorher: 'ATU1234567' ergab weder ROT noch GELB, R18 prueft nur 'nicht leer'."""
        rid = self._rc_entwurf(klient, datenbank, "ATU1234567")
        befunde = {b.code: b for b in _befunde(datenbank, rid)}
        assert befunde["G28"].stufe == validierung.GELB
        assert ("hat nicht die Form einer USt-IdNr. aus Österreich (ATU und 8 Ziffern, "
                "z. B. ATU12345678)") in befunde["G28"].text
        assert "G26" not in befunde

    @pytest.mark.parametrize("nummer", ["ungültig 123", "CHE123456789"])
    def test_unbekanntes_praefix_ohne_verstuemmelten_g26(self, klient, datenbank, nummer):
        """Vorher: nur G26 'Die USt-IdNr. (UNGÜLTIG123) stammt aus UN, die Anschrift
        liegt in Österreich' -- bzw. 'stammt aus Schweiz' fuer eine Schweizer Nummer."""
        rid = self._rc_entwurf(klient, datenbank, nummer)
        befunde = {b.code: b for b in _befunde(datenbank, rid)}
        assert "beginnt nicht mit dem Kürzel eines EU-Staats" in befunde["G28"].text
        assert "G26" not in befunde
        assert not any("stammt aus UN" in b.text for b in befunde.values())

    def test_g28_muss_abgehakt_werden(self, klient, datenbank):
        """Vorher ging das Festschreiben ohne jede Rueckfrage durch (303, RE-…)."""
        rid = self._rc_entwurf(klient, datenbank, "ATU1234567")
        befunde = _befunde(datenbank, rid)
        assert not validierung.blockiert(befunde), [b.code for b in befunde if b.blockiert]
        version = _lies(datenbank, "SELECT version FROM rechnung WHERE id = ?", rid)[0][0]
        ohne_g28 = [b.code for b in befunde if b.stufe == validierung.GELB and b.code != "G28"]
        klient.post(f"/rechnung/{rid}/festschreiben",
                    data={"version": str(version), "bestaetigt": ohne_g28},
                    follow_redirects=False)
        assert _lies(datenbank, "SELECT status FROM rechnung WHERE id = ?", rid) == [("ENTWURF",)]
        antwort = _festschreiben(klient, datenbank, rid)
        assert "fehler=" not in antwort.headers["location"], antwort.headers["location"]
        assert _lies(datenbank, "SELECT status FROM rechnung WHERE id = ?", rid) == [
            ("FESTGESCHRIEBEN",)]

    def test_gueltige_nummer_anderer_eu_staat_bleibt_g26(self, klient, datenbank):
        """Ein bekanntes EU-Praefix ungleich der Anschrift meldet weiter G26, ohne G28."""
        rid = self._rc_entwurf(klient, datenbank, "FR12345678901")
        codes = [b.code for b in _befunde(datenbank, rid)]
        assert "G26" in codes and "G28" not in codes


# ------------------------------------------------ 6. Text von G10

class TestTextG10:
    def test_ein_kalendertag_und_erfasste_dauer(self, klient, datenbank):
        """Vorher: '44 Stunden auf 1 Kalendertage ist physisch unplausibel.' --
        abgerundet (die Position zeigte 45,00 Std.) und im Plural."""
        kid = _kunde(klient)
        zeiten = [_zeit(klient, datenbank, kid, dauer="24:00"),
                  _zeit(klient, datenbank, kid, dauer="20:50")]
        rid = _entwurf(klient, kid, zeiten)
        text = {b.code: b.text for b in _befunde(datenbank, rid)}["G10"]
        assert text.startswith("Erfasst sind 44:50 Stunden auf 1 Kalendertag —"), text
        assert "Kalendertage" not in text

    def test_mehrere_kalendertage(self, klient, datenbank):
        kid = _kunde(klient)
        zeiten = [_zeit(klient, datenbank, kid, datum="2026-03-02", dauer="24:00"),
                  _zeit(klient, datenbank, kid, datum="2026-03-03", dauer="24:00")]
        rid = _entwurf(klient, kid, zeiten)
        text = {b.code: b.text for b in _befunde(datenbank, rid)}["G10"]
        assert text.startswith("Erfasst sind 48:00 Stunden auf 2 Kalendertage —"), text


# ------------------------------------------------ 7. 405-Text und HEAD

def _ueberschrift(html: str) -> str:
    import re

    return re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1).strip()


class TestFalscheMethodeUndHead:
    @pytest.mark.parametrize(("methode", "pfad"), [
        ("PUT", "/kunden/1"), ("DELETE", "/kunden/1"), ("PUT", "/kunden"),
        ("DELETE", "/kunden"),
    ])
    def test_adresse_mit_anzeige_und_formular(self, klient, methode, pfad):
        """Vorher: PUT/DELETE /kunden/1 (Allow: GET, POST) sagte 'zeigt nur etwas an
        und nimmt keine Formulare oder Änderungen entgegen' -- falsch, POST /kunden/1
        ist das Kundenformular."""
        _kunde(klient)
        antwort = klient.request(methode, pfad, follow_redirects=False)
        assert antwort.status_code == 405
        assert antwort.headers["allow"] == "GET, POST"
        assert "zeigt nur etwas an" not in antwort.text
        assert "keine Formulare" not in antwort.text
        assert _ueberschrift(antwort.text) != "Diese Seite lässt sich nur anzeigen"
        assert "nimmt die Formulare des Steuer-Tools entgegen" in antwort.text
        assert "Gespeichert wurde dabei nichts." in antwort.text

    def test_reine_leseseite_bleibt_beim_alten_text(self, klient):
        antwort = klient.post("/rechnungen", follow_redirects=False)
        assert antwort.status_code == 405
        assert _ueberschrift(antwort.text) == "Diese Seite lässt sich nur anzeigen"

    @pytest.mark.parametrize("pfad", ["/", "/kunden", "/einrichtung", "/static/stil.css"])
    def test_head_auf_eine_seite_ist_200_ohne_rumpf(self, klient, pfad):
        """Vorher: HEAD / ergab 405 (Allow: GET) mit der Seite 'lässt sich nur anzeigen'."""
        antwort = klient.head(pfad)
        assert antwort.status_code == 200
        assert antwort.content == b""
        get = klient.get(pfad)
        assert antwort.headers["content-type"] == get.headers["content-type"]
        assert antwort.headers.get("content-length") == get.headers.get("content-length")

    def test_head_auf_formularadresse_und_unbekannte_adresse(self, klient):
        antwort = klient.head("/zeiten/timer/start")
        assert antwort.status_code == 405 and antwort.content == b""
        assert antwort.headers["allow"] == "POST"
        antwort = klient.head("/gibtsnicht")
        assert antwort.status_code == 404 and antwort.content == b""

    def test_head_vor_der_einrichtung_leitet_um(self, roh):
        antwort = roh.head("/", follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"] == "/einrichtung"

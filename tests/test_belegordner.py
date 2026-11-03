"""Der Eingangsordner: Rechnungen vom Handy, beim Start abgeholt.

Geprueft wird der ganze Weg -- Einstellung, Abholen, automatisches Buchen,
Hintergrundlauf beim Start, Knoepfe und Oberflaeche -- und vor allem, was
NICHT passieren darf: dass etwas doppelt gebucht wird, dass eine unsichere
Zahl ohne Pruefmaske in die Buchfuehrung kommt (ein gebuchter Beleg laesst sich
im Werkzeug nicht aendern oder loeschen, § 146 Abs. 4 AO), dass eine Datei
verloren geht oder dass ein Fehler im Ordner den Server mitreisst.
"""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import db
from app.logik import kategorien
from app.logik.beleg import abholen, pipeline, text_leser, textdienst, vision_mac
from app.logik.beleg.ergebnis import Fund, Vorschlag
from app.main import erzeuge_app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CII = FIXTURES / "zugferd_cii.xml"
UBL = FIXTURES / "xrechnung_ubl.xml"
PDF_DE = FIXTURES / "lieferantenrechnung_de.pdf"
HEUTE = date(2026, 10, 24)

EINRICHTUNG = {
    "name": "Mein Kleingewerbe", "strasse": "Werkweg 2", "plz": "20095", "ort": "Hamburg",
    "steuernummer": "12/345/67890", "standardsatz_euro": "85,00", "zahlungsziel_tage": "14",
    "nummer_format": "RE-{JJJJ}-{NNNN}", "start_seq": "1",
}


# ------------------------------------------------------------------ Helfer

def cii(nummer: str = "RE-2026-0815", datum: str = "20260314", *, waehrung: str = "EUR",
        typ: str = "380", brutto: str | None = None, netto: str | None = None,
        ust: str | None = None, zahlbar: str | None = None, steuersumme: bool = True,
        kategorie: str = "S", satz: str = "19.00") -> bytes:
    """Die ZUGFeRD-Prueffdatei, abgewandelt: andere Nummer und anderes Datum
    ergeben einen anderen Beleg desselben Lieferanten (andere Pruefsumme).
    Betraege (als "1000.00"), Steuerkategorie und Satz lassen sich setzen;
    ``zahlbar`` ist DuePayableAmount (sonst gleich dem Brutto)."""
    text = CII.read_text(encoding="utf-8")
    text = text.replace("RE-2026-0815", nummer).replace("20260314</udt", f"{datum}</udt")
    text = text.replace("<ram:InvoiceCurrencyCode>EUR", f"<ram:InvoiceCurrencyCode>{waehrung}")
    text = text.replace("<ram:TypeCode>380</ram:TypeCode>", f"<ram:TypeCode>{typ}</ram:TypeCode>")
    if brutto:
        text = text.replace("<ram:GrandTotalAmount>5.46", f"<ram:GrandTotalAmount>{brutto}")
    text = text.replace("<ram:DuePayableAmount>5.46",
                        f"<ram:DuePayableAmount>{zahlbar or brutto or '5.46'}")
    if netto:
        text = text.replace("<ram:TaxBasisTotalAmount>4.59",
                            f"<ram:TaxBasisTotalAmount>{netto}")
        text = text.replace("<ram:BasisAmount>4.59", f"<ram:BasisAmount>{netto}")
    if ust:
        text = text.replace('currencyID="EUR">0.87', f'currencyID="EUR">{ust}')
        text = text.replace("<ram:CalculatedAmount>0.87", f"<ram:CalculatedAmount>{ust}")
    if not steuersumme:
        text = text.replace('<ram:TaxTotalAmount currencyID="EUR">0.87</ram:TaxTotalAmount>', "")
    text = text.replace("<ram:CategoryCode>S</ram:CategoryCode>",
                        f"<ram:CategoryCode>{kategorie}</ram:CategoryCode>")
    text = text.replace("<ram:RateApplicablePercent>19.00",
                        f"<ram:RateApplicablePercent>{satz}")
    return text.encode("utf-8")


def ubl_ohne_anzahlung() -> bytes:
    """Die XRechnung (Figma, Reverse Charge) ohne verrechnete Anzahlung:
    zu zahlen ist das Brutto."""
    text = UBL.read_text(encoding="utf-8")
    text = text.replace('<cbc:PrepaidAmount currencyID="EUR">30.00</cbc:PrepaidAmount>', "")
    text = text.replace('<cbc:PayableAmount currencyID="EUR">150.00',
                        '<cbc:PayableAmount currencyID="EUR">180.00')
    return text.encode("utf-8")


def lege(ordner: Path, name: str, inhalt: bytes, *, alter: float = 120) -> Path:
    """Legt eine Datei in den Ordner, ``alter`` Sekunden alt (Aenderungszeit)."""
    pfad = ordner / name
    pfad.write_bytes(inhalt)
    zeit = time.time() - alter
    os.utime(pfad, (zeit, zeit))
    return pfad


def leeres_pdf(pfad: Path) -> bytes:
    """Ein PDF ohne Textschicht -- so sieht ein Scan fuer die Auslesung aus."""
    from pypdf import PdfWriter

    schreiber = PdfWriter()
    schreiber.add_blank_page(width=595, height=842)
    with pfad.open("wb") as datei:
        schreiber.write(datei)
    return pfad.read_bytes()


def pdf_aus_zeilen(zeilen: list[str]) -> bytes:
    """Ein PDF mit Textschicht, eine Zeile je Absatz (WeasyPrint)."""
    from html import escape

    from weasyprint import HTML

    html = "<html><body style='font-family:sans-serif;font-size:11pt'>" + "".join(
        f"<p style='margin:2px 0'>{escape(z)}</p>" for z in zeilen) + "</body></html>"
    return HTML(string=html).write_pdf()


HETZNER_KOPF = ["Hetzner Online GmbH", "Industriestr. 25", "91710 Gunzenhausen",
                "USt-IdNr.: DE 812871812", "Steuernummer: 201/123/45678"]


def hetzner_pdf(titel: str = "Rechnung", nummer: str = "Rechnungsnummer: R0012345678",
                extra: str = "Vielen Dank.", *, kopf: list[str] | None = None,
                datum: list[str] | None = None, netto: str = "100,00", ust: str = "19,00",
                brutto: str = "119,00") -> bytes:
    """Ein PDF im Aufbau der Hetzner-Rechnung -- Ueberschrift, Nummer, Datum
    und aufgehende Betraege frei waehlbar."""
    return pdf_aus_zeilen(
        (HETZNER_KOPF if kopf is None else kopf)
        + [titel, nummer, *(datum or ["Rechnungsdatum: 02.10.2026"]), "Kundennummer: K-99887",
           extra, "Pos Bezeichnung Menge Einzelpreis Betrag",
           f"1 Cloud Server CX22 1 {netto} EUR {netto} EUR", f"Nettobetrag {netto} EUR",
           f"Umsatzsteuer 19 % {ust} EUR", f"Gesamtbetrag {brutto} EUR"])


def lieferant(conn, name="Hetzner Online GmbH", ust_idnr="DE812871812", *,
              kategorie: str | None = "HOSTING", waehrung="EUR") -> int:
    """Ein bekannter Lieferant, wahlweise mit gemerkter Kategorie."""
    kategorie_id = kategorien.nach_schluessel(conn, kategorie)["id"] if kategorie else None
    return conn.execute(
        "INSERT INTO lieferant (name, ust_idnr, angelegt_am, standard_kategorie_id,"
        " waehrung_default) VALUES (?, ?, ?, ?, ?)",
        (name, ust_idnr, "2026-01-01", kategorie_id, waehrung),
    ).lastrowid


def von_hand_buchen(conn, datenordner: Path, inhalt: bytes, name: str, lieferant_id: int,
                    **bestaetigt) -> int:
    """Bucht eine Datei wie die Pruefmaske (bestaetigt, mit Kategorie)."""
    eingang = pipeline.aufnehmen(inhalt, name, datenordner)
    lesung = pipeline.lies(conn, eingang, heute=HEUTE)
    werte = {"belegdatum": lesung.vorschlag.wert("belegdatum"),
             "belegnummer": lesung.vorschlag.wert("belegnummer"),
             "brutto": lesung.vorschlag.wert("brutto"), "waehrung": "EUR", **bestaetigt}
    return pipeline.buche(
        conn, lesung, werte, datenordner, lieferant_id=lieferant_id,
        kategorie_id=kategorien.nach_schluessel(conn, "HOSTING")["id"], heute=HEUTE,
    ).beleg_id


def namen(ordner: Path) -> list[str]:
    return sorted(p.name for p in ordner.iterdir()) if ordner.is_dir() else []


@pytest.fixture(autouse=True)
def _dateien_gelten_als_fertig():
    """Die Altersgrenzen messen an der Statuszeit (ctime), und die setzt das
    System beim Anlegen auf JETZT -- ``lege`` kann sie nicht zurueckdrehen.
    Fuer alle Tests ausser denen zur Altersgrenze gilt deshalb: jede Datei
    ist fertig geschrieben. Die Tests zur Altersgrenze setzen sie selbst.
    Ein eigener MonkeyPatch: ``monkeypatch.undo()`` in einem Test laesst
    diese Einstellung stehen."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(abholen, "MINDESTALTER_SEKUNDEN", 0)
        patch.setattr(abholen, "UNLESBAR_MINDESTALTER_SEKUNDEN", 0, raising=False)
        yield


@pytest.fixture
def altersgrenzen(_dateien_gelten_als_fertig):
    """Die echten Altersgrenzen (5 s fuer jede Datei, 60 s fuer eine unlesbare)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(abholen, "MINDESTALTER_SEKUNDEN", 5)
        patch.setattr(abholen, "UNLESBAR_MINDESTALTER_SEKUNDEN", 60, raising=False)
        yield


@pytest.fixture(autouse=True)
def _nichts_nachzuholen():
    """Nachzuholende Journaleintraege gelten je Prozess -- je Test frisch."""
    abholen._NACHZUHOLEN.clear()
    yield
    abholen._NACHZUHOLEN.clear()


@pytest.fixture
def datenordner(tmp_path):
    ordner = tmp_path / "daten"
    ordner.mkdir()
    return ordner


@pytest.fixture
def postfach(tmp_path):
    """Der Eingangsordner -- ausserhalb des Datenordners, wie in iCloud Drive."""
    ordner = tmp_path / "Steuer-Tool Belege"
    ordner.mkdir()
    return ordner


@pytest.fixture
def conn(datenordner, postfach):
    verbindung = db.oeffne(datenordner / "steuer.sqlite")
    verbindung.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, ust_idnr,"
        " belegordner_pfad) VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095',"
        " 'Hamburg', '12/345/67890', 'DE999999999', ?)",
        (str(postfach),),
    )
    yield verbindung
    verbindung.close()


def hole(conn, datenordner, postfach, **weiter) -> abholen.Ergebnis:
    weiter.setdefault("automatisch", True)
    weiter.setdefault("heute", HEUTE)
    return abholen.abholen(conn, datenordner, postfach, **weiter)


def anzahl(conn, sql: str, *werte) -> int:
    return conn.execute(sql, werte).fetchone()[0]


# --------------------------------------------------------------- Migration

class TestMigration:
    def test_von_16_auf_17_kommen_die_spalten_dazu(self, tmp_path):
        """Eine Datenbank aus Version 16 bekommt beide Spalten, mit Vorgabe
        "automatisch an" und ohne Ordner -- und dieselben Spalten wie eine
        frische. Der Schritt steht im Journal."""
        pfad = tmp_path / "alt.sqlite"
        alt = db.oeffne(pfad)
        alt.execute("INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer)"
                    " VALUES (1, 'Alt', 'Weg 1', '20095', 'Hamburg', '12/345/67890')")
        alt.execute("ALTER TABLE betrieb DROP COLUMN belegordner_automatisch")
        alt.execute("ALTER TABLE betrieb DROP COLUMN belegordner_pfad")
        alt.execute("DELETE FROM schema_version")
        alt.execute("INSERT INTO schema_version (version, migriert_am) VALUES (16, ?)",
                    (db.jetzt_utc(),))
        alt.close()

        migriert = db.oeffne(pfad)
        frisch = db.oeffne(tmp_path / "frisch.sqlite")
        try:
            def spalten(verbindung):
                return {z["name"] for z in verbindung.execute("PRAGMA table_info(betrieb)")}

            assert {"belegordner_pfad", "belegordner_automatisch"} <= spalten(migriert)
            assert spalten(migriert) == spalten(frisch)
            zeile = migriert.execute(
                "SELECT belegordner_pfad, belegordner_automatisch FROM betrieb").fetchone()
            assert (zeile[0], zeile[1]) == (None, 1)
            assert anzahl(migriert, "SELECT COUNT(*) FROM journal WHERE aktion = 'MIGRATION'"
                                    " AND objekt_id = 17") == 1
            assert anzahl(migriert, "SELECT MAX(version) FROM schema_version") == \
                db.SCHEMA_VERSION
            # Auch der CHECK kam mit: nur 0 und 1.
            with pytest.raises(sqlite3.IntegrityError):
                migriert.execute("UPDATE betrieb SET belegordner_automatisch = 2")
        finally:
            migriert.close()
            frisch.close()

    def test_frische_datenbank_prueft_den_schalter(self, conn):
        """In einer frischen Datenbank haelt der CHECK den Schalter auf 0 und 1."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE betrieb SET belegordner_automatisch = 5 WHERE id = 1")


# ------------------------------------------------------- Einstellung (Web)

@pytest.fixture
def app_pfad(tmp_path):
    ordner = tmp_path / "appdaten"
    ordner.mkdir()
    return ordner / "steuer.sqlite"


@pytest.fixture
def klient(app_pfad):
    with TestClient(erzeuge_app(app_pfad)) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


def einstellung(app_pfad):
    verbindung = sqlite3.connect(app_pfad)
    try:
        return verbindung.execute(
            "SELECT belegordner_pfad, belegordner_automatisch FROM betrieb WHERE id = 1"
        ).fetchone()
    finally:
        verbindung.close()


def abfrage(app_pfad, sql, *werte):
    verbindung = sqlite3.connect(app_pfad)
    verbindung.row_factory = sqlite3.Row
    try:
        return verbindung.execute(sql, werte).fetchall()
    finally:
        verbindung.close()


class TestEinstellung:
    def test_speichern_mit_journal(self, klient, app_pfad, tmp_path):
        """Ein vorhandener Ordner wird absolut gespeichert; jede Aenderung steht
        im Journal, und die Meldung sagt, wann abgeholt wird."""
        ordner = tmp_path / "Postfach"
        ordner.mkdir()
        antwort = klient.post("/belege/ordner", data={"pfad": f"  {ordner}  ", "automatisch": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "erfolg=" in antwort.headers["location"]
        assert einstellung(app_pfad) == (str(ordner.resolve()), 1)
        journal = abfrage(app_pfad, "SELECT feld, wert_neu FROM journal WHERE aktion = 'AENDERN'"
                                    " AND objekttyp = 'betrieb' AND feld LIKE 'belegordner%'")
        assert [(z["feld"], z["wert_neu"]) for z in journal] == [
            ("belegordner_pfad", str(ordner.resolve()))]

    def test_leer_schaltet_aus(self, klient, app_pfad, tmp_path):
        """Ein leeres Feld schaltet das Abholen aus; der Haken wird mitgespeichert."""
        ordner = tmp_path / "Postfach"
        ordner.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(ordner), "automatisch": "1"})
        antwort = klient.post("/belege/ordner", data={"pfad": ""}, follow_redirects=False)
        assert "ausgeschaltet" in unquote(antwort.headers["location"])
        assert einstellung(app_pfad) == (None, 0)

    def test_leeres_feld_ohne_ordner_heisst_noch_keiner_gewaehlt(self, klient, app_pfad):
        """War nie ein Ordner eingestellt, meldet "Speichern" mit leerem Feld
        nicht "ausgeschaltet" (als waere er an gewesen), sondern was zu tun ist."""
        antwort = klient.post("/belege/ordner", data={"pfad": ""}, follow_redirects=False)
        ziel = unquote(antwort.headers["location"])
        assert "Noch kein Eingangsordner gewählt" in ziel
        assert "ausgeschaltet" not in ziel

    def test_ordner_in_icloud_lesbar(self, klient, tmp_path, monkeypatch):
        """Ein Ordner in iCloud Drive heisst in Meldung und Seite so, wie ihn
        der Finder zeigt: "iCloud Drive › Steuer-Tool Belege"."""
        icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        icloud.mkdir(parents=True)
        monkeypatch.setattr(abholen, "vorschlag_ordner", lambda: icloud / "Steuer-Tool Belege")
        antwort = klient.post("/belege/ordner",
                              data={"pfad": "", "automatisch": "1", "vorschlag": "1"},
                              follow_redirects=True)
        assert "Eingangsordner iCloud Drive › Steuer-Tool Belege angelegt und gespeichert" in \
            antwort.text
        assert "In der Dateien-App und im\n    Finder: <strong>iCloud Drive › Steuer-Tool " \
            "Belege</strong>" in antwort.text

    @pytest.mark.parametrize("eingabe, erwartet", [
        ("relativ/Belege", "vollständigen Pfad"),
        ("~", "allgemeiner Ordner"),
        ("~unbekannterbenutzer/x", "Tilde"),
    ])
    def test_ungueltig_bleibt_mit_eingabe_stehen(self, klient, app_pfad, eingabe, erwartet):
        """Ein unbrauchbarer Pfad kommt als 422 mit Meldung zurueck -- und die
        Eingabe steht noch im Feld. Gespeichert wird nichts."""
        antwort = klient.post("/belege/ordner", data={"pfad": eingabe, "automatisch": "1"})
        assert antwort.status_code == 422
        assert erwartet in antwort.text
        assert f'value="{eingabe}"' in antwort.text
        assert einstellung(app_pfad) == (None, 1)

    def test_fehler_beim_speichern_ist_sofort_sichtbar(self, klient):
        """Der rote Kasten einer 422 steht weit unten im Abschnitt -- auf dem
        Handy unterhalb des Fensters. Die Seite springt deshalb dorthin, das
        Feld ist als ungueltig markiert und fokussiert."""
        antwort = klient.post("/belege/ordner", data={"pfad": "Belege"})
        assert antwort.status_code == 422
        assert 'id="ordner-fehler" role="alert"' in antwort.text
        assert 'aria-invalid="true" aria-describedby="ordner-fehler"' in antwort.text
        assert 'getElementById("ordner-fehler")' in antwort.text
        assert "scrollIntoView" in antwort.text
        assert "scrollIntoView" not in klient.get("/belege").text

    def test_datenordner_wird_abgewiesen(self, klient, app_pfad):
        """Der Datenordner selbst, ein Ordner darin und einer darueber taugen
        nicht: das Abholen verschiebt jede Datei, die es dort findet."""
        for ordner in (app_pfad.parent, app_pfad.parent / "eingang", app_pfad.parent.parent):
            antwort = klient.post("/belege/ordner", data={"pfad": str(ordner)})
            assert antwort.status_code == 422, ordner
            assert "Datenordner" in antwort.text
        assert einstellung(app_pfad) == (None, 1)

    def test_sicherungsordner_wird_abgewiesen(self, klient, app_pfad, tmp_path):
        """Ein eigenes Sicherungsziel als Eingangsordner schoebe die Archive nach
        "Nicht übernommen"."""
        ziel = tmp_path / "Sicherungen"
        klient.post("/backup/ziel", data={"pfad": str(ziel), "zweitziel": ""})
        antwort = klient.post("/belege/ordner", data={"pfad": str(ziel)})
        assert antwort.status_code == 422
        assert "Sicherungsordner" in antwort.text

    def test_sicherungsziel_darf_nicht_der_eingangsordner_werden(self, klient, app_pfad,
                                                                tmp_path):
        """Umgekehrte Reihenfolge: erst der Eingangsordner, dann dasselbe als
        Sicherungsziel (erstes oder zweites). Das wird abgewiesen -- sonst
        laege die naechste Sicherung in "Nicht übernommen"."""
        ordner = tmp_path / "Postfach"
        ordner.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(ordner), "automatisch": "1"})
        for daten in ({"pfad": str(ordner), "zweitziel": ""},
                      {"pfad": "", "zweitziel": str(ordner)}):
            antwort = klient.post("/backup/ziel", data=daten, follow_redirects=False)
            assert "fehler=" in antwort.headers["location"]
            assert "Eingangsordner" in unquote(antwort.headers["location"])
        [zeile] = abfrage(app_pfad, "SELECT backup_pfad, backup_zweitziel FROM betrieb")
        assert (zeile["backup_pfad"], zeile["backup_zweitziel"]) == (None, None)

    def test_abholen_prueft_bei_jedem_lauf_gegen_das_sicherungsziel(self, conn, datenordner,
                                                                    postfach):
        """Zeigt das Sicherungsziel (etwa aus einer alten Einstellung) auf den
        Eingangsordner, holt der Lauf nichts ab -- das Archiv bleibt liegen."""
        conn.execute("UPDATE betrieb SET backup_pfad = ? WHERE id = 1", (str(postfach),))
        lege(postfach, "2026-10-24_steuer-backup.zip", b"PK\x03\x04")
        lege(postfach, "Notiz.docx", b"PK\x03\x04")
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.fehler == abholen.SICHERUNGSORDNER
        assert namen(postfach) == ["2026-10-24_steuer-backup.zip", "Notiz.docx"]

    def test_fehlender_ordner_wird_angelegt(self, klient, app_pfad, tmp_path):
        """Fehlt nur der Ordner selbst, legt das Speichern ihn an."""
        ordner = tmp_path / "Steuer-Tool Belege"
        antwort = klient.post("/belege/ordner", data={"pfad": str(ordner), "automatisch": "1"},
                              follow_redirects=True)
        assert ordner.is_dir()
        assert "angelegt und gespeichert" in antwort.text
        assert einstellung(app_pfad)[0] == str(ordner.resolve())

    def test_fehlt_auch_der_elternordner_kommt_der_icloud_hinweis(self, klient, app_pfad,
                                                                   tmp_path):
        """Fehlt schon der Elternordner -- bei iCloud Drive heisst das meist:
        iCloud Drive ist aus --, wird nichts angelegt, und die Meldung sagt es."""
        ordner = (tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
                  / "Steuer-Tool Belege")
        antwort = klient.post("/belege/ordner", data={"pfad": str(ordner)})
        assert antwort.status_code == 422
        assert "iCloud Drive auf diesem Mac eingeschaltet" in antwort.text
        assert not ordner.parent.exists()
        assert einstellung(app_pfad) == (None, 1)

    def test_texte_mit_komma_und_ohne_bezugsloses_sonst(self, klient, tmp_path):
        """"60,0 MB" mit deutschem Komma; ausserhalb von iCloud Drive steht
        "Bitte den Pfad prüfen." ohne ein "Sonst", das sich auf einen
        iCloud-Satz bezoege, der dort fehlt."""
        assert "(60,0 MB)" in pipeline.zu_gross(60 * 1024 * 1024)
        antwort = klient.post("/belege/ordner", data={"pfad": str(tmp_path / "gibt" / "Belege")})
        assert antwort.status_code == 422
        assert "nicht anlegen. Bitte den Pfad prüfen." in antwort.text
        assert "Sonst" not in antwort.text

    def test_vorschlag_uebernehmen(self, klient, app_pfad, tmp_path, monkeypatch):
        """"Vorschlag übernehmen" nimmt den Ordner in iCloud Drive und legt ihn
        an, wenn iCloud Drive da ist."""
        icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        icloud.mkdir(parents=True)
        monkeypatch.setattr(abholen, "vorschlag_ordner", lambda: icloud / "Steuer-Tool Belege")
        text = klient.get("/belege").text
        assert "Vorschlag übernehmen" in text
        klient.post("/belege/ordner", data={"pfad": "", "automatisch": "1", "vorschlag": "1"})
        assert (icloud / "Steuer-Tool Belege").is_dir()
        assert einstellung(app_pfad) == (str((icloud / "Steuer-Tool Belege").resolve()), 1)

    def test_kein_zugriff_beim_einrichten_nennt_den_richtigen_knopf(
            self, klient, app_pfad, tmp_path, monkeypatch):
        """Verweigert macOS den Zugriff schon beim "Vorschlag übernehmen" (dort
        fragt es zum ersten Mal), gibt es "Jetzt abholen" noch gar nicht: die
        Meldung nennt "Vorschlag übernehmen" bzw. "Speichern"."""
        icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        icloud.mkdir(parents=True)
        ziel = icloud / "Steuer-Tool Belege"
        monkeypatch.setattr(abholen, "vorschlag_ordner", lambda: ziel)
        echt = Path.iterdir

        def verweigert(selbst):
            if selbst == ziel:
                raise PermissionError(1, "Operation not permitted", str(selbst))
            return echt(selbst)

        monkeypatch.setattr(Path, "iterdir", verweigert)
        antwort = klient.post("/belege/ordner",
                              data={"pfad": "", "automatisch": "1", "vorschlag": "1"})
        assert antwort.status_code == 422
        assert "erlauben, dann noch einmal „Vorschlag übernehmen“" in antwort.text
        assert "dann „Jetzt abholen“" not in antwort.text
        gespeichert = klient.post("/belege/ordner", data={"pfad": str(ziel), "automatisch": "1"})
        assert "erlauben, dann noch einmal „Speichern“" in gespeichert.text

    def test_vorschlag_gibt_es_nur_auf_dem_mac(self, monkeypatch):
        """Auf dem Mac liegt der Vorschlag in iCloud Drive, sonst gibt es keinen."""
        monkeypatch.setattr(abholen, "_ist_mac", lambda: False)
        assert abholen.vorschlag_ordner() is None
        monkeypatch.setattr(abholen, "_ist_mac", lambda: True)
        vorschlag = abholen.vorschlag_ordner()
        assert vorschlag.parts[-3:] == ("Mobile Documents", "com~apple~CloudDocs",
                                        "Steuer-Tool Belege")


# ----------------------------------------------------- Automatisch buchen

class TestAutomatischBuchen:
    def test_erste_xml_eines_neuen_lieferanten_wartet(self, conn, datenordner, postfach):
        """Der erste Beleg eines Lieferanten braucht eine Kategorie -- er wartet
        mit Grund in der Pruefmaske, die Quelle wandert nach "Übernommen"."""
        lege(postfach, "Rechnung.xml", cii())
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.originalname == "Rechnung.xml"
        assert wartend.grund.startswith("neuer Lieferant")
        assert "als Vorgabe merken" in wartend.grund
        assert (datenordner / "eingang" / wartend.eingangsname).is_file()
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]
        assert "Rechnung.xml" not in namen(postfach)
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_nach_vorgabe_merken_wird_die_zweite_xml_gebucht(self, klient, app_pfad, tmp_path):
        """Der ganze Weg: erste Rechnung abholen, in der Pruefmaske mit
        "als Vorgabe merken" buchen, zweite Rechnung desselben Lieferanten
        abholen -- die wird gebucht. Ungeprueft, mit "automatisch" im Journal
        und ohne etwas zu lernen; mit Zahlung am Belegdatum, weil "Eingangs-
        belege sind in der Regel schon bezahlt" die Vorgabe ist (in der
        neutralen Zahlungsart: die erste Rechnung hatte keine Zahlung)."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(postfach), "automatisch": "1"})
        lege(postfach, "Hetzner März.xml", cii())
        klient.post("/belege/ordner/abholen")
        [eingangsname] = [p.name for p in pipeline.liegen_geblieben(app_pfad.parent)]

        verbindung = db.verbinde(app_pfad)
        hosting = kategorien.nach_schluessel(verbindung, "HOSTING")["id"]
        verbindung.close()
        antwort = klient.post(f"/belege/pruefen/{eingangsname}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "5,46", "netto_euro": "4,59",
            "ust_euro": "0,87", "ust_satz": "19", "waehrung": "EUR",
            "belegnummer": "RE-2026-0815", "lieferant_neu": "Hetzner Online GmbH",
            "lieferant_ust_idnr": "DE812871812", "kategorie_id": str(hosting), "quote": "100",
            "vorgabe_merken": "1",
        }, follow_redirects=False)
        assert antwort.status_code == 303, antwort.text

        lege(postfach, "Hetzner April.xml", cii("RE-2026-0901", "20260414"))
        antwort = klient.post("/belege/ordner/abholen", follow_redirects=True)
        assert "1 Rechnung automatisch gebucht" in antwort.text

        [beleg] = abfrage(app_pfad, "SELECT * FROM beleg WHERE belegnummer_extern = 'RE-2026-0901'")
        assert beleg["geprueft_am"] is None
        assert beleg["kategorie_id"] == hosting
        assert beleg["betrag_brutto_cent"] == 546
        assert (beleg["betrag_netto_cent"], beleg["betrag_ust_cent"],
                beleg["ust_satz_promille"]) == (459, 87, 190)
        assert beleg["notiz"] == abholen.NOTIZ
        [zahlung] = abfrage(app_pfad, "SELECT * FROM zahlung WHERE beleg_id = ?", beleg["id"])
        assert (zahlung["datum"], zahlung["betrag_cent"], zahlung["art"]) == (
            "2026-04-14", -546, "SONSTIGES")
        [journal] = abfrage(app_pfad, "SELECT wert_neu FROM journal WHERE aktion ="
                                      " 'BELEG_ERFASSEN' AND objekt_id = ?", beleg["id"])
        assert json.loads(journal["wert_neu"])["automatisch"] is True
        [extraktion] = abfrage(app_pfad, "SELECT * FROM extraktion WHERE beleg_id = ?",
                               beleg["id"])
        assert "automatisch" in extraktion["verfahren"]
        assert json.loads(extraktion["bestaetigt_json"])["automatisch"] is True
        assert abfrage(app_pfad, "SELECT * FROM lieferant_profil") == []

    def test_pdf_eines_bekannten_lieferanten_wird_gebucht_und_lernt_nichts(
            self, conn, datenordner, postfach):
        """Ein PDF mit Textschicht: bekannter Lieferant, aufgehende
        Kreuzpruefung, bezeichnetes Datum -- alles sicher, also gebucht. Aus
        unbestaetigten Werten wird aber nichts gelernt."""
        lieferant(conn)
        lege(postfach, "Rechnung.pdf", PDF_DE.read_bytes())
        ergebnis = hole(conn, datenordner, postfach)
        [gebucht] = ergebnis.gebucht
        assert (gebucht.lieferant, gebucht.brutto_cent) == ("Hetzner Online GmbH", 829)
        assert anzahl(conn, "SELECT COUNT(*) FROM lieferant_profil") == 0
        assert conn.execute("SELECT geprueft_am FROM beleg WHERE id = ?",
                            (gebucht.beleg_id,)).fetchone()[0] is None

    def test_pdf_ohne_lernstand_wartet(self, conn, datenordner, postfach):
        """Ein PDF eines unbekannten Lieferanten: der Name ist aus der
        Textschicht nur geraten -- es wartet."""
        lege(postfach, "Rechnung.pdf", PDF_DE.read_bytes())
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert wartend.grund.startswith("neuer Lieferant")

    @pytest.mark.parametrize("titel, nummer, extra", [
        ("Gutschrift", "Gutschriftsnummer: G0000777",
         "Wir schreiben Ihnen folgenden Betrag gut."),
        ("Stornorechnung", "Rechnungsnummer: S0012349999",
         "Storno zu Rechnung R0012348888 vom 01.09.2026"),
        ("Angebot", "Angebotsnummer: A-2026-77", "Unverbindliches Angebot."),
        ("Auftragsbestätigung", "Auftragsnummer: AB-5555", "Vielen Dank."),
        ("Proforma-Rechnung", "Proforma-Nr: PF-1234",
         "Dies ist keine Rechnung im Sinne des UStG."),
        ("Zahlungserinnerung", "Rechnungsnummer: R0012345678", "Bitte zahlen."),
        ("Beleg", "Nummer: X-1", "Vielen Dank."),  # ohne "Rechnung" (Datum siehe unten)
    ])
    def test_pdf_eines_bekannten_lieferanten_ohne_lernstand_das_keine_rechnung_ist(
            self, conn, datenordner, postfach, titel, nummer, extra):
        """Bekannter Lieferant mit Vorgabe, KEIN Lernstand, Betraege gehen auf --
        aber im Kopf steht Gutschrift, Storno, Angebot, Auftragsbestaetigung,
        Proforma oder Mahnung, oder das Wort "Rechnung" fehlt ganz. Das ist
        keine gewoehnliche Ausgabe: es wartet, gebucht wird nichts."""
        lieferant(conn)
        datum = ["Datum: 02.10.2026"] if titel == "Beleg" else None
        lege(postfach, "Dokument.pdf", hetzner_pdf(titel, nummer, extra, datum=datum))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund.startswith("Dokumentart nicht eindeutig Rechnung")
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    @pytest.mark.parametrize("daten", [
        ["Datum: 10.01.2026", "Lieferdatum: 22.12.2025"],
        ["Leistungsdatum: 15.12.2025", "Datum: 05.01.2026"],
        ["Lieferdatum: 22.12.2025"],
    ])
    def test_liefer_oder_leistungsdatum_ist_kein_sicheres_belegdatum(
            self, conn, datenordner, postfach, daten):
        """Ohne "Rechnungsdatum": steht neben "Datum" ein Liefer- oder
        Leistungsdatum, ist offen, welches das Belegdatum ist -- und "datum" als
        Wortteil von "Lieferdatum" bezeichnet kein Belegdatum. Es wartet, statt
        unveraenderlich im falschen Jahr abgelegt zu werden."""
        lieferant(conn)
        lege(postfach, "Rechnung.pdf", hetzner_pdf(datum=daten))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund == "Belegdatum nicht sicher erkannt"

    def test_datum_allein_ist_sicher(self, conn, datenordner, postfach):
        """Steht nur "Datum: 15.01.2026" da, ohne ein anderes Datum, ist es das
        Belegdatum -- gebucht. (Nicht mehr der 05.01.: am Jahreswechsel wartet
        ein Beleg mit Zahlung jetzt auf die Pruefmaske, siehe
        tests/test_eingang_bezahlt.py.)"""
        lieferant(conn)
        lege(postfach, "Rechnung.pdf", hetzner_pdf(datum=["Datum: 15.01.2026"]))
        [gebucht] = hole(conn, datenordner, postfach).gebucht
        assert conn.execute("SELECT belegdatum FROM beleg WHERE id = ?",
                            (gebucht.beleg_id,)).fetchone()[0] == "2026-01-15"

    def test_pdf_rechnung_im_selben_aufbau_wird_gebucht(self, conn, datenordner, postfach):
        """Gegenprobe zum Test davor: dieselbe Vorlage mit "Rechnung" im Kopf ist
        eine Rechnung und wird gebucht."""
        lieferant(conn)
        lege(postfach, "Rechnung.pdf",
             hetzner_pdf("Rechnung", "Rechnungsnummer: R0012345678", "Vielen Dank."))
        [gebucht] = hole(conn, datenordner, postfach).gebucht
        assert gebucht.brutto_cent == 11900

    def test_name_der_bank_im_kopf_macht_die_rechnung_nicht_zur_bankgebuehr(
            self, conn, datenordner, postfach):
        """Im Kopf einer Designrechnung steht die Bankverbindung "Commerzbank
        AG" -- die erste Zeile mit Rechtsform, und ein bekannter Lieferant. Die
        einzige USt-IdNr. im Beleg gehoert aber dem Designer: der Lieferant ist
        nicht sicher, die Rechnung wartet und wird nicht als Bankgebuehr gebucht."""
        lieferant(conn, "Commerzbank AG", "DE114103379", kategorie="BANKGEBUEHREN")
        lege(postfach, "Rechnung_Webdesign.pdf", hetzner_pdf(
            kopf=["Max Schmidt Webdesign", "Musterweg 1", "12345 Berlin",
                  "USt-IdNr.: DE287654321", "Bankverbindung:", "Commerzbank AG",
                  "IBAN: DE89 3704 0044 0532 0130 00"],
            netto="1.000,00", ust="190,00", brutto="1.190,00"))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund.startswith(
            "USt-IdNr. im Beleg (DE287654321) passt nicht zu Commerzbank AG")
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_pdf_mit_idnr_bei_lieferant_ohne_idnr_wartet(self, conn, datenordner, postfach):
        """Ist beim Lieferanten keine USt-IdNr. gespeichert, laesst sich bei
        einem PDF nicht pruefen, ob die Nummer im Beleg zu ihm gehoert: es
        wartet. Ein XML (Name und Nummer aus demselben Verkaeuferdatensatz)
        wird dagegen gebucht."""
        lieferant(conn, ust_idnr=None)
        lege(postfach, "Rechnung.pdf", hetzner_pdf())
        lege(postfach, "Rechnung.xml", cii())
        ergebnis = hole(conn, datenordner, postfach)
        [wartend] = ergebnis.wartend
        assert wartend.originalname == "Rechnung.pdf"
        assert "nicht hinterlegt" in wartend.grund
        assert [g.originalname for g in ergebnis.gebucht] == ["Rechnung.xml"]

    def test_unsicherer_betrag_wartet(self, conn, datenordner, postfach, monkeypatch):
        """Bekannter Lieferant mit Kategorie, aber der Betrag geht nicht auf und
        kein Lernmuster traegt ihn: ohne Pruefmaske nicht buchen."""
        lieferant(conn)
        echt = text_leser.lies_datei

        def unsicher(*args, **kwargs):
            vorschlag = echt(*args, **kwargs)
            brutto = vorschlag.felder["brutto"]
            return Vorschlag(vorschlag.quelle,
                             {**vorschlag.felder, "brutto": Fund(brutto.wert, brutto.quelle,
                                                                 brutto.nachweis, False)},
                             vorschlag.meldungen)

        monkeypatch.setattr(text_leser, "lies_datei", unsicher)
        lege(postfach, "Rechnung.pdf", PDF_DE.read_bytes())
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert wartend.grund == "Bruttobetrag nicht sicher erkannt"
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_foto_wartet_ohne_bilderkennung(self, conn, datenordner, postfach, monkeypatch):
        """Ein Foto wartet in der Pruefmaske -- die Bilderkennung laeuft beim
        Abholen nicht, ihre Funde sind ohnehin nie sicher."""
        lieferant(conn)
        monkeypatch.setattr(vision_mac, "lies_datei",
                            lambda *a, **k: pytest.fail("Bilderkennung beim Abholen"))
        lege(postfach, "Kassenzettel.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 200)
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert wartend.grund == "Foto oder Scan — bitte in der Prüfmaske bestätigen"

    def test_scan_als_pdf_wartet_ohne_bilderkennung(self, conn, datenordner, postfach,
                                                    tmp_path, monkeypatch):
        """Ein PDF ohne Textschicht (Scan): auch wenn Apple Vision bereitstuende,
        laeuft sie beim Abholen nicht."""
        monkeypatch.setattr(vision_mac, "ist_verfuegbar",
                            lambda: pytest.fail("Bilderkennung beim Abholen"))
        lege(postfach, "Scan.pdf", leeres_pdf(tmp_path / "scan.pdf"))
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert "keine Textschicht" in wartend.grund

    def test_gutschrift_wartet(self, conn, datenordner, postfach):
        """Eine Gutschrift (TypeCode 381) ist keine Ausgabe."""
        lieferant(conn)
        lege(postfach, "Gutschrift.xml", cii(typ="381"))
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert wartend.grund == "Gutschrift — keine Ausgabe"

    def test_fremdwaehrung_wartet(self, conn, datenordner, postfach):
        """In USD: der Euro-Betrag fuer die EUeR steht erst auf dem Kontoauszug."""
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii(waehrung="USD"))
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert wartend.grund.startswith("Fremdwährung")

    def test_aehnlicher_beleg_wartet(self, conn, datenordner, postfach):
        """Gleicher Lieferant, gleiches Datum, gleicher Betrag, nur eine andere
        Nummer -- vermutlich dieselbe Rechnung als anderer Export."""
        lid = lieferant(conn)
        von_hand_buchen(conn, datenordner, cii(), "März.xml", lid)
        lege(postfach, "Rechnung.xml", cii("RE-2026-0815-B"))
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert wartend.grund.startswith("ähnlicher Beleg vorhanden")
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_dieselbe_rechnung_unter_zweitem_lieferanten_datensatz_wartet(
            self, conn, datenordner, postfach):
        """Dieselbe Rechnung ist von Hand unter "Hetzner" gebucht (ein zweiter
        Datensatz ohne USt-IdNr., in der Pruefmaske neu getippt). Das XML wird
        ueber die USt-IdNr. "Hetzner Online GmbH" zugeordnet -- die
        Aehnlichkeitspruefung desselben Lieferanten saehe den Beleg nicht. Es
        wartet mit Hinweis auf den anderen Datensatz."""
        lieferant(conn)
        zweiter = lieferant(conn, "Hetzner", None, kategorie=None)
        von_hand_buchen(conn, datenordner, cii() + b"\n", "Hetzner März.xml", zweiter)
        lege(postfach, "Rechnung.xml", cii())
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund.startswith("ähnlicher Beleg bei einem anderen Lieferanten-Datensatz")
        assert "Zu Lieferant Hetzner gibt es bereits Beleg 1" in wartend.grund
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_e_rechnung_mit_sichtdatei_bucht_das_xml(self, conn, datenordner, postfach):
        """XRechnung mit beigelegtem PDF im selben Lauf: das XML ist maßgebend
        und wird gebucht, obwohl "rechnung.pdf" alphabetisch vorher kaeme. Das
        PDF wartet, ausdruecklich als Sichtdatei zur E-Rechnung gekennzeichnet."""
        lieferant(conn)
        lege(postfach, "rechnung.pdf", hetzner_pdf(
            nummer="Rechnungsnummer: RE-2026-0815", datum=["Rechnungsdatum: 14.03.2026"],
            netto="4,59", ust="0,87", brutto="5,46"))
        lege(postfach, "rechnung.xml", cii())
        ergebnis = hole(conn, datenordner, postfach)
        [gebucht] = ergebnis.gebucht
        assert gebucht.originalname == "rechnung.xml"
        assert conn.execute("SELECT mime FROM belegdatei WHERE beleg_id = ?",
                            (gebucht.beleg_id,)).fetchone()[0] == "application/xml"
        [wartend] = ergebnis.wartend
        assert wartend.originalname == "rechnung.pdf"
        assert wartend.grund.startswith(
            f"Sichtdatei (PDF) zur E-Rechnung Beleg {gebucht.beleg_id}")

    @pytest.mark.parametrize("schluessel, erwartet", [
        (None, "keine Kategorie als Vorgabe gemerkt"),
        ("HARDWARE_ANLAGE", "Anlagegut"),
        ("BEWIRTUNG", "eigene Angaben am Beleg"),
        ("HOMEOFFICE_PAUSCHALE", "ist eine Pauschale (Eigenbeleg)"),
        ("FAHRT_PRIVATER_PKW", "ist eine Pauschale (Eigenbeleg)"),
        ("REISE_VERPFLEGUNG", "ist eine Pauschale (Eigenbeleg)"),
        ("REISE_UEBERNACHTUNG", "Reisekosten — bitte in der Prüfmaske einer Reise zuordnen"),
        ("REISE_FAHRTKOSTEN", "Reisekosten — bitte in der Prüfmaske einer Reise zuordnen"),
    ])
    def test_ohne_passende_vorgabe_wartet(self, conn, datenordner, postfach, schluessel,
                                          erwartet):
        """Ohne gemerkte Kategorie, mit der Anlagegut-Kategorie, einer
        Kategorie mit eigenen Aufzeichnungspflichten, einer Pauschale
        (Eigenbeleg) oder Reisekosten (Zuordnung zur Reise nur in der
        Pruefmaske) wartet der Beleg."""
        lieferant(conn, kategorie=schluessel)
        lege(postfach, "Rechnung.xml", cii())
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert erwartet in wartend.grund

    def test_ueber_der_wertgrenze_wartet(self, conn, datenordner, postfach):
        """In der GWG-Kategorie, aber ueber 800 EUR netto: vermutlich ein
        Anlagegut, das ins Verzeichnis gehoert."""
        lieferant(conn, kategorie="HARDWARE_GWG")
        teuer = cii().replace(b">4.59<", b">1000.00<").replace(b">0.87<", b">190.00<")
        teuer = teuer.replace(b">5.46<", b">1190.00<")
        lege(postfach, "Monitor.xml", teuer)
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert "Wertgrenze" in wartend.grund

    def test_xml_mit_nicht_aufgehenden_summen_wartet(self, conn, datenordner, postfach):
        """GrandTotal 200,00, aber netto 100,00 + 19,00 Steuer: das Dokument
        widerspricht sich. buche() verwuerfe Netto und Steuer und buchte die
        200,00 ungeprueft -- es wartet."""
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii(brutto="200.00", netto="100.00", ust="19.00"))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund.startswith("Summen gehen nicht auf: 100,00 € netto + 19,00 €")
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_wertgrenze_mit_dem_netto_das_gespeichert_wird(self, conn, datenordner, postfach):
        """GWG-Kategorie, 2.000,00 brutto bei 19 %, im XML ein Netto von 100,00
        ohne Steuersumme. buche() verwirft dieses Netto (ohne Steuer geht es
        nicht auf) und rechnet die Grenze aus dem Brutto: 1.680,67 netto, ueber
        800. Geprueft wird deshalb mit diesem Wert -- es wartet, statt als GWG
        mit 2.000 sofort abgezogen zu werden."""
        lieferant(conn, kategorie="HARDWARE_GWG")
        lege(postfach, "Laptop.xml", cii(brutto="2000.00", netto="100.00", steuersumme=False))
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert "Wertgrenze von 800,00 € netto" in wartend.grund
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    @pytest.mark.parametrize("typ, erwartet", [
        ("384", "Rechnungskorrektur (TypeCode 384)"),
        ("386", "Anzahlungsrechnung (TypeCode 386)"),
        ("326", "Teilrechnung (TypeCode 326)"),
    ])
    def test_xml_ausser_der_handelsrechnung_wartet(self, conn, datenordner, postfach, typ,
                                                   erwartet):
        """Nur die gewoehnliche Handelsrechnung (380) wird ohne Pruefmaske
        gebucht. Die Korrektur einer schon gebuchten Rechnung kaeme sonst als
        zweiter Beleg dazu (die Ausgabe doppelt), eine Anzahlungsrechnung zur
        spaeteren Schlussrechnung."""
        lieferant(conn)
        lege(postfach, "Original.xml", cii("RE-2026-1000", "20260310"))
        assert len(hole(conn, datenordner, postfach).gebucht) == 1
        lege(postfach, "Folge.xml", cii("RE-2026-1000-K", "20260320", typ=typ,
                                        brutto="2.73", netto="2.29", ust="0.44"))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund.startswith(erwartet)
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_verrechnete_anzahlung_wartet(self, conn, datenordner, postfach):
        """XRechnung ueber 180,00 mit 30,00 Anzahlung (zu zahlen 150,00): was
        als Ausgabe zaehlt, entscheidet der Mensch -- es wartet. Ebenso im CII,
        wenn DuePayableAmount vom GrandTotalAmount abweicht."""
        lieferant(conn, "Figma Ireland Limited", "IE3623748GH", kategorie="SAAS")
        lieferant(conn, "Hetzner Online GmbH", "DE812871812")
        lege(postfach, "Figma.xml", UBL.read_bytes())
        lege(postfach, "Hetzner.xml", cii(zahlbar="3.00"))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        gruende = {w.originalname: w.grund for w in ergebnis.wartend}
        assert gruende["Figma.xml"].startswith(
            "Zu zahlen sind 150,00 €, die Rechnung lautet über 180,00 €")
        assert gruende["Hetzner.xml"].startswith("Zu zahlen sind 3,00 €")

    @pytest.mark.parametrize("schluessel", ["BUEROBEDARF", "UEBRIGE", "WERBUNG",
                                            "INSTANDHALTUNG", "HOSTING"])
    def test_ueber_800_euro_netto_wartet_in_jeder_kategorie(self, conn, datenordner,
                                                         postfach, schluessel):
        """2.000,00 netto in einer Kategorie ohne eigene Wertgrenze: ein
        Versandhaendler mit Vorgabe "Bürobedarf" liefert auch einen Laptop.
        Ueber der GWG-Grenze von 800 EUR netto wartet jede Rechnung -- ein
        Anlagegut braucht die Pruefmaske."""
        lieferant(conn, kategorie=schluessel)
        lege(postfach, "Bestellung.xml", cii(brutto="2380.00", netto="2000.00", ust="380.00"))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund == ("über 800,00 € netto — möglicherweise ein Anlagegut, bitte "
                                 "in der Prüfmaske bestätigen")

    def test_bis_800_euro_netto_wird_gebucht(self, conn, datenordner, postfach):
        """Genau 800,00 netto ist noch nicht darueber."""
        lieferant(conn, kategorie="BUEROBEDARF")
        lege(postfach, "Bestellung.xml", cii(brutto="952.00", netto="800.00", ust="152.00"))
        [gebucht] = hole(conn, datenordner, postfach).gebucht
        assert gebucht.brutto_cent == 95200

    def test_belegdatum_in_der_zukunft_wartet(self, conn, datenordner, postfach):
        """buche() weist ein Datum in der Zukunft ab -- der Grund steht da."""
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii("RE-2027-1", "20270101"))
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert "Zukunft" in wartend.grund
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0

    def test_reverse_charge_aus_dem_xml(self, conn, datenordner, postfach):
        """XRechnung mit Reverse Charge: gebucht mit Steuer 0 und Netto = Brutto."""
        lieferant(conn, "Figma Ireland Limited", "IE3623748GH", kategorie="SAAS")
        lege(postfach, "Figma.xml", ubl_ohne_anzahlung())
        [gebucht] = hole(conn, datenordner, postfach).gebucht
        zeile = conn.execute("SELECT * FROM beleg WHERE id = ?", (gebucht.beleg_id,)).fetchone()
        assert (zeile["reverse_charge"], zeile["betrag_ust_cent"],
                zeile["betrag_netto_cent"]) == (1, 0, 18000)

    @pytest.mark.parametrize("kategorie, erwartet", [
        ("E", "Steuerkategorie E (steuerbefreit)"),
        ("Z", "Steuerkategorie Z (Nullsatz)"),
        ("O", "Steuerkategorie O (nicht steuerbar)"),
        ("K", "Steuerkategorie K (innergemeinschaftliche Lieferung)"),
    ])
    def test_reverse_charge_vorgabe_gegen_das_xml(self, conn, datenordner, postfach, kategorie,
                                                 erwartet):
        """Beim Lieferanten ist Reverse Charge gemerkt, das XML weist aber eine
        andere Steuerkategorie aus (steuerbefreit, nicht steuerbar ...). Beim
        XML entscheidet das Dokument: der Widerspruch wartet, statt
        unveraenderlich eine Steuerschuld nach § 13b UStG zu behaupten."""
        lid = lieferant(conn)
        conn.execute("UPDATE lieferant SET reverse_charge_default = 1 WHERE id = ?", (lid,))
        lege(postfach, "Rechnung.xml", cii(kategorie=kategorie, satz="0.00", ust="0.00",
                                           brutto="4.59"))
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.gebucht == []
        [wartend] = ergebnis.wartend
        assert wartend.grund == (f"Reverse Charge laut Vorgabe für Hetzner Online GmbH, die "
                                 f"Rechnung weist aber {erwartet} aus — bitte in der "
                                 "Prüfmaske prüfen")

    def test_reverse_charge_vorgabe_gilt_beim_pdf_ohne_vermerk(self, conn, datenordner,
                                                             postfach):
        """Ein PDF ohne Vermerk sagt nichts zur Steuerschuld -- dort greift die
        gemerkte Vorgabe weiter (hier: kein Reverse Charge gemerkt, kein Vermerk)."""
        lieferant(conn)
        lege(postfach, "Rechnung.pdf", PDF_DE.read_bytes())
        [gebucht] = hole(conn, datenordner, postfach).gebucht
        assert conn.execute("SELECT reverse_charge FROM beleg WHERE id = ?",
                            (gebucht.beleg_id,)).fetchone()[0] == 0

    def test_gruende_fuer_unlesbare_dateien_sind_deutsch(self, conn, datenordner, postfach):
        """Passwortgeschuetztes PDF, beschaedigtes PDF, abgeschnittenes XML: der
        Grund sagt auf Deutsch, was los ist und was zu tun ist -- keine
        englischen Texte aus pypdf, pdfminer oder dem XML-Parser."""
        import io

        from pypdf import PdfReader, PdfWriter

        schreiber = PdfWriter()
        schreiber.append(PdfReader(PDF_DE))
        schreiber.encrypt("geheim")
        puffer = io.BytesIO()
        schreiber.write(puffer)
        lege(postfach, "Versicherung passwortgeschuetzt.pdf", puffer.getvalue())
        lege(postfach, "kaputt.pdf", b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
        lege(postfach, "halb.xml", b"<?xml version='1.0'?><rsm:CrossIndustryInvoice")
        gruende = {w.originalname: w.grund for w in hole(conn, datenordner, postfach).wartend}
        assert "mit einem Passwort geschützt" in gruende["Versicherung passwortgeschuetzt.pdf"]
        assert "beschädigt oder unvollständig" in gruende["kaputt.pdf"]
        assert "nicht vollständig oder nicht wohlgeformt (Zeile 1" in gruende["halb.xml"]
        for grund in gruende.values():
            for englisch in ("decrypted", "startxref", "unclosed", "not found", "Root object"):
                assert englisch not in grund

    def test_automatisch_aus(self, conn, datenordner, postfach):
        """Mit ausgeschaltetem Haken wartet auch eine sicher erkannte Rechnung."""
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii())
        [wartend] = hole(conn, datenordner, postfach, automatisch=False).wartend
        assert wartend.grund == "automatisches Buchen ist ausgeschaltet"
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0


# ------------------------------------------------------- Dateien im Ordner

class TestDateien:
    def test_dublette_wird_nicht_doppelt_gebucht(self, conn, datenordner, postfach):
        """Genau diese Datei ist schon gebucht: kein zweiter Beleg, die eben
        angelegte Eingangskopie ist wieder weg, die Quelle in "Übernommen"."""
        lid = lieferant(conn)
        beleg_id = von_hand_buchen(conn, datenordner, cii(), "März.xml", lid)
        lege(postfach, "Rechnung nochmal.xml", cii())
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.schon_vorhanden == [
            abholen.SchonVorhanden("Rechnung nochmal.xml", beleg_id)]
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
        assert pipeline.liegen_geblieben(datenordner) == []
        assert namen(datenordner / "eingang") == []
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung nochmal.xml"]

    def test_datei_liegt_schon_im_eingang(self, conn, datenordner, postfach):
        """Schon hochgeladen und noch nicht gebucht: nichts Neues im Eingang,
        kein Buchungsversuch, die Quelle wandert trotzdem nach "Übernommen"."""
        lieferant(conn)
        vorher = pipeline.aufnehmen(cii(), "hochgeladen.xml", datenordner)
        lege(postfach, "Rechnung.xml", cii())
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.schon_vorhanden == [
            abholen.SchonVorhanden("Rechnung.xml", None, vorher.pfad.name)]
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0
        assert [p.name for p in pipeline.liegen_geblieben(datenordner)] == [vorher.pfad.name]
        assert pipeline.original_name_von(vorher.pfad) == "hochgeladen.xml"
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]

    def test_kein_beleg_wandert_nach_nicht_uebernommen(self, conn, datenordner, postfach):
        """.docx, leere Datei, Inhalt passt nicht zur Endung: nach "Nicht
        übernommen", mit Grund -- und beim naechsten Start nicht wieder."""
        lege(postfach, "Aufstellung.docx", b"PK\x03\x04")
        lege(postfach, "leer.pdf", b"")
        lege(postfach, "programm.pdf", b"MZ\x90\x00 kein pdf")
        ergebnis = hole(conn, datenordner, postfach)
        gruende = {a.originalname: a.grund for a in ergebnis.abgewiesen}
        assert set(gruende) == {"Aufstellung.docx", "leer.pdf", "programm.pdf"}
        assert "kein Belegformat" in gruende["Aufstellung.docx"]
        assert "leer" in gruende["leer.pdf"]
        assert "kein PDF" in gruende["programm.pdf"]
        assert all(a.verschoben for a in ergebnis.abgewiesen)
        assert namen(postfach / abholen.NICHT_UEBERNOMMEN) == [
            "Aufstellung.docx", "leer.pdf", "programm.pdf"]
        assert namen(datenordner / "eingang") == []
        nochmal = hole(conn, datenordner, postfach)
        assert nochmal.leer()

    def test_icloud_platzhalter(self, conn, datenordner, postfach, monkeypatch):
        """".Rechnung.pdf.icloud" ist noch nicht geladen: gemeldet, liegen
        gelassen, und auf dem Mac wird brctl um den Download gebeten."""
        aufrufe = []
        monkeypatch.setattr(abholen, "_ist_mac", lambda: True)
        monkeypatch.setattr(abholen.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(abholen.subprocess, "Popen",
                            lambda befehl, **kw: aufrufe.append((befehl, kw)))
        lege(postfach, ".Rechnung.pdf.icloud", b"bplist00")
        lege(postfach, ".DS_Store", b"\x00\x00\x00\x01Bud1")
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.icloud_ausstehend == ["Rechnung.pdf"]
        assert not ergebnis.veraendert()
        assert namen(postfach) == [".DS_Store", ".Rechnung.pdf.icloud"]
        [(befehl, optionen)] = aufrufe
        assert befehl == ["/usr/bin/brctl", "download", str(postfach / "Rechnung.pdf")]
        assert optionen["stdout"] == subprocess.DEVNULL

    def test_icloud_platzhalter_neben_der_echten_datei_und_fuer_nicht_belege(
            self, conn, datenordner, postfach, monkeypatch):
        """Liegt die echte Datei schon neben dem Platzhalter, zaehlt der
        Platzhalter nicht -- dieselbe Datei hiesse sonst "gebucht" und "lädt
        noch". Ein Platzhalter fuer eine .docx ist keine ausstehende Rechnung
        und stoesst keinen Download an. Ein zerlegter Name (NFD) wird richtig
        zusammengesetzt."""
        aufrufe = []
        monkeypatch.setattr(abholen, "_ist_mac", lambda: True)
        monkeypatch.setattr(abholen.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(abholen.subprocess, "Popen",
                            lambda befehl, **kw: aufrufe.append(befehl[-1]))
        lieferant(conn)
        lege(postfach, "Schon.xml", cii())
        lege(postfach, ".Schon.xml.icloud", b"bplist00")
        lege(postfach, ".Notiz.docx.icloud", b"bplist00")
        lege(postfach, ".Rechnung.pdf.icloud", b"bplist00")
        lege(postfach, "." + unicodedata.normalize("NFD", "Grüße.xml") + ".icloud", b"x")
        ergebnis = hole(conn, datenordner, postfach)
        assert [g.originalname for g in ergebnis.gebucht] == ["Schon.xml"]
        assert sorted(ergebnis.icloud_ausstehend) == ["Grüße.xml", "Rechnung.pdf"]
        assert sorted(Path(a).name for a in aufrufe) == sorted(
            [unicodedata.normalize("NFD", "Grüße.xml"), "Rechnung.pdf"])

    def test_brctl_fehler_wird_ignoriert(self, conn, datenordner, postfach, monkeypatch):
        """Haengt oder fehlt brctl, laeuft das Abholen trotzdem durch."""
        def kaputt(*a, **k):
            raise OSError(8, "Exec format error")

        monkeypatch.setattr(abholen, "_ist_mac", lambda: True)
        monkeypatch.setattr(abholen.shutil, "which", lambda name: "/usr/bin/brctl")
        monkeypatch.setattr(abholen.subprocess, "Popen", kaputt)
        lege(postfach, ".Rechnung.pdf.icloud", b"x")
        assert hole(conn, datenordner, postfach).icloud_ausstehend == ["Rechnung.pdf"]

    def test_haengendes_brctl_haelt_den_lauf_nicht_auf(self, conn, datenordner, postfach,
                                                      monkeypatch, tmp_path):
        """brctl, das haengt (hier: 30 s schlaeft), wird nur angestossen und
        nicht abgewartet: acht Platzhalter halten den Lauf -- und damit die
        Sperre und "Jetzt abholen" -- keine Minute fest."""
        brctl = tmp_path / "brctl"
        brctl.write_text("#!/bin/sh\nsleep 30\n")
        brctl.chmod(0o755)
        monkeypatch.setattr(abholen, "_ist_mac", lambda: True)
        monkeypatch.setattr(abholen.shutil, "which", lambda name: str(brctl))
        for nummer in range(8):
            lege(postfach, f".Rechnung {nummer}.pdf.icloud", b"bplist00")
        beginn = time.monotonic()
        ergebnis = hole(conn, datenordner, postfach)
        assert time.monotonic() - beginn < 5
        assert len(ergebnis.icloud_ausstehend) == 8

    def test_pdf_mit_datenmuell_haelt_den_lauf_nicht_auf(self, conn, datenordner, postfach,
                                                        monkeypatch):
        """Die Textschicht eines PDF mit Datenmuell am Ende braucht Minuten. Sie
        wird in einem eigenen Prozess mit Zeitgrenze gelesen: nach der Grenze
        wartet das PDF mit Grund, und die naechste Datei kommt trotzdem dran."""
        monkeypatch.setattr(abholen.textdienst, "ZEITGRENZE_SEKUNDEN", 2)
        lieferant(conn)
        lege(postfach, "a Muell.pdf", PDF_DE.read_bytes() + b"\x00" * (30 * 1024 * 1024))
        lege(postfach, "b Rechnung.pdf", hetzner_pdf())
        beginn = time.monotonic()
        ergebnis = hole(conn, datenordner, postfach)
        assert time.monotonic() - beginn < 20
        [wartend] = ergebnis.wartend
        assert wartend.originalname == "a Muell.pdf"
        assert "nicht in 2 Sekunden lesen" in wartend.grund
        assert [g.originalname for g in ergebnis.gebucht] == ["b Rechnung.pdf"]

    def test_unlesbare_datei_wird_einmal_gemeldet(self, conn, datenordner, postfach,
                                                 monkeypatch):
        """Dauerhaft unlesbar (chmod 000; als root hier nachgestellt): sie kam
        bei jedem Abholen wieder, samt Journaleintrag je Lauf. Jetzt wie eine
        geschuetzte Datei: einmal melden und merken. Wird sie lesbar, kommt
        sie ganz normal dran."""
        lieferant(conn)
        pfad = lege(postfach, "Rechnung.xml", cii())
        gesperrt = {pfad}
        echt = Path.read_bytes

        def lies(datei):
            if datei in gesperrt:
                raise PermissionError(13, "Permission denied", str(datei))
            return echt(datei)

        monkeypatch.setattr(Path, "read_bytes", lies)
        erster = hole(conn, datenordner, postfach)
        [abgewiesen] = erster.abgewiesen
        assert (abgewiesen.originalname, abgewiesen.verschoben) == ("Rechnung.xml", False)
        assert "keine Berechtigung" in abgewiesen.grund
        for _ in range(2):
            spaeter = hole(conn, datenordner, postfach)
            assert spaeter.leer(), spaeter.zusammenfassung()
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEGORDNER_ABGEHOLT'") == 1
        assert "nur dieses eine Mal" in abgewiesen.grund
        gesperrt.clear()
        lesbar = hole(conn, datenordner, postfach)
        assert [g.originalname for g in lesbar.gebucht] == ["Rechnung.xml"]
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]

    def test_derselbe_fehler_je_lauf_steht_einmal_im_journal(self, conn, datenordner,
                                                            postfach, monkeypatch):
        """Bleibt eine Datei bei jedem Abholen mit demselben Fehler liegen,
        sieht der Nutzer das jedes Mal -- im Journal steht es aber nur einmal,
        solange sich gegenueber dem letzten Lauf nichts geaendert hat."""
        lege(postfach, "Rechnung.xml", cii())

        def kaputt(*a, **k):
            raise OSError(5, "Eingabe-/Ausgabefehler")

        monkeypatch.setattr(pipeline, "aufnehmen", kaputt)
        for _ in range(3):
            ergebnis = hole(conn, datenordner, postfach)
            assert [a.originalname for a in ergebnis.abgewiesen] == ["Rechnung.xml"]
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEGORDNER_ABGEHOLT'") == 1
        lege(postfach, "Notiz.docx", b"PK\x03\x04")
        hole(conn, datenordner, postfach)
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEGORDNER_ABGEHOLT'") == 2

    def test_junge_datei_kommt_beim_naechsten_mal(self, conn, datenordner, postfach,
                                                  altersgrenzen):
        """Eben erst gespeichert: liegen lassen, beim naechsten Abholen dabei."""
        pfad = lege(postfach, "Rechnung.xml", cii(), alter=0)
        ergebnis = hole(conn, datenordner, postfach, jetzt=pfad.stat().st_mtime + 1)
        assert ergebnis.noch_im_schreiben == ["Rechnung.xml"]
        assert namen(postfach) == ["Rechnung.xml"]
        spaeter = hole(conn, datenordner, postfach, jetzt=pfad.stat().st_mtime + 10)
        assert [w.originalname for w in spaeter.wartend] == ["Rechnung.xml"]

    def test_kopie_mit_alter_aenderungszeit_wird_noch_geschrieben(
            self, conn, datenordner, postfach, altersgrenzen):
        """Der Finder und cp -p uebernehmen die Aenderungszeit des Originals --
        eine halb geschriebene Kopie traegt dann eine Stunde alte mtime. Die
        Statuszeit (ctime) ist aber jung: die Datei bleibt liegen, statt
        abgeschnitten in den Eingang zu kommen."""
        voll = cii()
        pfad = lege(postfach, "Kopie.xml", voll[: len(voll) // 2], alter=3600)
        ergebnis = hole(conn, datenordner, postfach, jetzt=time.time())
        assert ergebnis.noch_im_schreiben == ["Kopie.xml"]
        assert pipeline.liegen_geblieben(datenordner) == []
        assert namen(postfach) == ["Kopie.xml"]
        assert pfad.stat().st_mtime < time.time() - 3000

    def test_stockender_schreiber_haelt_keine_halbe_datei_fest(
            self, conn, datenordner, postfach, altersgrenzen):
        """Ein Kopiervorgang stockt laenger als 5 s: die halbe Datei ist alt
        genug, laesst sich aber nicht lesen. Ist sie juenger als eine Minute,
        bleibt sie liegen (kein abgeschnittenes "Original" im Eingang, das
        jemand von Hand erfassen koennte) -- die vollstaendige kommt beim
        naechsten Mal dran. Eine alte unlesbare Datei wartet dagegen wie
        bisher in der Pruefmaske."""
        lieferant(conn)
        voll = cii()
        pfad = lege(postfach, "stockt.xml", voll[: len(voll) // 2], alter=0)
        ergebnis = hole(conn, datenordner, postfach, jetzt=time.time() + 6)
        assert ergebnis.noch_im_schreiben == ["stockt.xml"]
        assert ergebnis.wartend == []
        assert pipeline.liegen_geblieben(datenordner) == []
        assert namen(postfach) == ["stockt.xml"]
        pfad.write_bytes(voll)  # der Schreiber macht weiter
        [gebucht] = hole(conn, datenordner, postfach, jetzt=time.time() + 6).gebucht
        assert gebucht.originalname == "stockt.xml"

        lege(postfach, "kaputt.xml", b"<?xml version='1.0'?><kaputt", alter=0)
        [wartend] = hole(conn, datenordner, postfach, jetzt=time.time() + 120).wartend
        assert wartend.grund.startswith(abholen.NICHT_LESBAR)

    def test_ersetzte_quelle_wird_nicht_ungelesen_verschoben(self, conn, datenordner,
                                                            postfach, monkeypatch):
        """Gleich nach dem Lesen ersetzt die Dateien-App Rechnung.xml durch eine
        andere Rechnung. Gebucht wird, was gelesen wurde; die neue Datei bleibt
        liegen (sie wanderte sonst ungelesen nach "Übernommen") und kommt beim
        naechsten Lauf dran."""
        lieferant(conn)
        quelle = lege(postfach, "Rechnung.xml", cii("E-1", "20260301"))
        echt = pipeline.aufnehmen

        def ersetzt(inhalt, name, ordner):
            eingang = echt(inhalt, name, ordner)
            neu = postfach / ".neu.xml"
            neu.write_bytes(cii("E-2", "20260302"))
            os.replace(neu, quelle)
            return eingang

        monkeypatch.setattr(pipeline, "aufnehmen", ersetzt)
        erster = hole(conn, datenordner, postfach)
        monkeypatch.undo()
        assert [g.originalname for g in erster.gebucht] == ["Rechnung.xml"]
        assert erster.noch_im_schreiben == ["Rechnung.xml"]
        assert "Rechnung.xml" in namen(postfach)
        assert namen(postfach / abholen.UEBERNOMMEN) == []
        zweiter = hole(conn, datenordner, postfach)
        assert [g.originalname for g in zweiter.gebucht] == ["Rechnung.xml"]
        assert sorted(z[0] for z in conn.execute("SELECT belegnummer_extern FROM beleg")) == [
            "E-1", "E-2"]

    def test_namenskollision_in_uebernommen(self, conn, datenordner, postfach):
        """Vom Handy heisst fast jede Rechnung gleich -- ueberschrieben wird nichts."""
        lege(postfach, "Rechnung.xml", cii())
        hole(conn, datenordner, postfach)
        lege(postfach, "Rechnung.xml", cii("RE-2026-0901", "20260414"))
        hole(conn, datenordner, postfach)
        lege(postfach, "Rechnung.xml", cii("RE-2026-0902", "20260514"))
        hole(conn, datenordner, postfach)
        assert namen(postfach / abholen.UEBERNOMMEN) == [
            "Rechnung (2).xml", "Rechnung (3).xml", "Rechnung.xml"]

    @pytest.mark.parametrize("stamm", ["R" * 250, unicodedata.normalize("NFD", "ä" * 83)],
                             ids=["ascii", "nfd"])
    def test_langer_name_mit_kollision_passt_in_255_bytes(self, conn, datenordner, postfach,
                                                         stamm):
        """Zweimal ein Name mit 254 Bytes: "… (2).xml" waere zu lang, das
        Verschieben scheiterte, und die Datei bliebe fuer immer liegen. Der
        Stamm wird gekuerzt (in NFD belegt ein Umlaut drei Bytes)."""
        name = stamm + ".xml"
        assert len(os.fsencode(name)) <= 255
        lege(postfach, name, cii())
        hole(conn, datenordner, postfach)
        lege(postfach, name, cii("RE-2026-0901", "20260414"))
        hole(conn, datenordner, postfach)
        uebernommen = namen(postfach / abholen.UEBERNOMMEN)
        assert len(uebernommen) == 2
        assert all(len(os.fsencode(n)) <= 255 for n in uebernommen)
        assert any(n.endswith(" (2).xml") for n in uebernommen)
        assert name not in namen(postfach)

    def test_name_aus_macos_wird_zusammengesetzt(self, conn, datenordner, postfach):
        """macOS liefert "ä" zerlegt (NFD). Gemerkt wird der zusammengesetzte
        Name (NFC) -- so steht er spaeter in belegdatei.original_name."""
        zerlegt = unicodedata.normalize("NFD", "Rechnung März.xml")
        assert zerlegt != "Rechnung März.xml"
        lege(postfach, zerlegt, cii())
        [wartend] = hole(conn, datenordner, postfach).wartend
        assert wartend.originalname == "Rechnung März.xml"
        eingang = datenordner / "eingang" / wartend.eingangsname
        assert pipeline.original_name_von(eingang) == "Rechnung März.xml"

    def test_dateiname_mit_ungueltigem_utf8(self, conn, datenordner, postfach):
        """Ein Name in Latin-1 (b"Rechnung_M\\xfcller.xml", Linux oder ein
        fremdes Dateisystem) bricht das Abholen nicht ab: angezeigt und
        protokolliert mit Ersatzzeichen, aufgenommen wie jede Datei."""
        roh = os.fsencode(postfach) + b"/Rechnung_M\xfcller.xml"
        try:
            with open(roh, "wb") as datei:
                datei.write(cii())
        except OSError:
            pytest.skip("Dieses Dateisystem nimmt nur gueltiges UTF-8 an (macOS)")
        alt = time.time() - 120
        os.utime(roh, (alt, alt))
        ergebnis = hole(conn, datenordner, postfach)
        [wartend] = ergebnis.wartend
        assert wartend.originalname == "Rechnung_M\ufffdller.xml"
        eingang = datenordner / "eingang" / wartend.eingangsname
        assert pipeline.original_name_von(eingang) == "Rechnung_M\ufffdller.xml"
        [zeile] = conn.execute("SELECT begruendung FROM journal"
                               " WHERE aktion = 'BELEGORDNER_ABGEHOLT'").fetchall()
        assert "Rechnung_M\ufffdller.xml" in zeile[0]
        assert namen(postfach / abholen.UEBERNOMMEN) == [os.fsdecode(b"Rechnung_M\xfcller.xml")]

    def test_unterordner_werden_uebersprungen(self, conn, datenordner, postfach):
        """Nur die oberste Ebene: was in Unterordnern liegt, bleibt unberuehrt."""
        (postfach / "Archiv 2025").mkdir()
        lege(postfach / "Archiv 2025", "Alt.xml", cii())
        assert hole(conn, datenordner, postfach).leer()
        assert namen(postfach / "Archiv 2025") == ["Alt.xml"]

    def test_ordner_fehlt(self, conn, datenordner, tmp_path):
        """Ein fehlender Ordner ist ein Fehlertext, keine Ausnahme -- in iCloud
        Drive mit dem Hinweis, ob iCloud Drive an ist."""
        ergebnis = hole(conn, datenordner, tmp_path / "gibt es nicht")
        assert "fehlt" in ergebnis.fehler
        icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "B"
        ergebnis = hole(conn, datenordner, icloud)
        assert "iCloud Drive auf diesem Mac eingeschaltet?" in ergebnis.fehler

    def test_keine_berechtigung(self, conn, datenordner, postfach, monkeypatch):
        """macOS verweigert den Zugriff (Terminal darf nicht auf iCloud Drive):
        ein deutscher Text mit dem Weg in die Systemeinstellungen."""
        lege(postfach, "Rechnung.xml", cii())
        os.chmod(postfach, 0)
        try:
            if os.access(postfach, os.R_OK):  # als root greift chmod nicht
                echt = Path.iterdir

                def verweigert(selbst):
                    if selbst == postfach:
                        raise PermissionError(1, "Operation not permitted", str(selbst))
                    return echt(selbst)

                monkeypatch.setattr(Path, "iterdir", verweigert)
            ergebnis = hole(conn, datenordner, postfach)
        finally:
            os.chmod(postfach, 0o755)
            monkeypatch.undo()
        assert "Kein Zugriff" in ergebnis.fehler
        assert "Datenschutz & Sicherheit → Dateien und Ordner → Terminal" in ergebnis.fehler
        assert "Jetzt abholen" in ergebnis.fehler
        assert namen(postfach) == ["Rechnung.xml"]

    def test_ordner_im_datenordner_wird_nicht_abgeholt(self, conn, datenordner):
        """Auch eine alte Einstellung, die in den Datenordner zeigt, verschiebt
        dort nichts."""
        ergebnis = hole(conn, datenordner, datenordner)
        assert "Datenordner" in ergebnis.fehler
        assert (datenordner / "steuer.sqlite").is_file()

    def test_verschwundene_datei_wird_still_uebersprungen(self, conn, datenordner, postfach,
                                                          monkeypatch):
        """Zwischen Auflisten und Lesen hat ein anderer Lauf die Datei
        verschoben: kein Fehler, keine Meldung."""
        lege(postfach, "Rechnung.xml", cii())
        echt = Path.read_bytes

        def weg(selbst):
            if selbst.name == "Rechnung.xml":
                raise FileNotFoundError(2, "No such file or directory", str(selbst))
            return echt(selbst)

        monkeypatch.setattr(Path, "read_bytes", weg)
        assert hole(conn, datenordner, postfach).leer()

    def test_verschieben_scheitert_und_nichts_wird_doppelt(self, conn, datenordner, postfach,
                                                          monkeypatch):
        """Laesst sich die Quelle nicht verschieben, bleibt sie liegen -- das
        naechste Abholen erkennt sie an der Pruefsumme und bucht nicht doppelt."""
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii())

        def gesperrt(*a, **k):
            raise PermissionError(13, "Permission denied")

        # Nur das Verschieben im Eingangsordner scheitert -- nicht das
        # Archivieren beim Buchen, das ebenfalls shutil.move benutzt.
        monkeypatch.setattr(abholen, "freier_name", gesperrt)
        erster = hole(conn, datenordner, postfach)
        assert len(erster.gebucht) == 1
        assert [n.originalname for n in erster.nicht_verschoben] == ["Rechnung.xml"]
        assert namen(postfach) == sorted(["Rechnung.xml", abholen.UEBERNOMMEN])
        monkeypatch.undo()
        # Gemerkt: der naechste Lauf verschiebt sie still, ohne sie noch einmal
        # aufzunehmen oder zu melden.
        assert hole(conn, datenordner, postfach).leer()
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]
        assert not (datenordner / abholen.GEDAECHTNIS).exists()


    def test_geschuetzte_datei_wird_nicht_bei_jedem_lauf_kopiert(self, conn, datenordner,
                                                                postfach, monkeypatch):
        """Laesst sich die Quelle weder umbenennen noch loeschen (Finder
        "Geschützt", chflags uchg), bleibt sie liegen -- und es entsteht KEINE
        Kopie in "Übernommen" bzw. "Nicht übernommen", auch nicht nach drei
        Laeufen."""
        lieferant(conn)
        lege(postfach, "gesperrt.xml", cii())
        lege(postfach, "gesperrt.docx", b"PK\x03\x04")

        def gesperrt(pfad) -> bool:
            pfad = Path(pfad)
            return pfad.parent == postfach and pfad.name.startswith("gesperrt")

        for name in ("replace", "rename", "unlink"):
            echt = getattr(os, name)

            def verweigert(pfad, *rest, _echt=echt, **optionen):
                if gesperrt(pfad):
                    raise PermissionError(1, "Operation not permitted", str(pfad))
                return _echt(pfad, *rest, **optionen)

            monkeypatch.setattr(os, name, verweigert)
        for _ in range(3):
            hole(conn, datenordner, postfach)
        monkeypatch.undo()
        assert namen(postfach / abholen.UEBERNOMMEN) == []
        assert namen(postfach / abholen.NICHT_UEBERNOMMEN) == []
        assert {"gesperrt.xml", "gesperrt.docx"} <= set(namen(postfach))
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1

    def test_nicht_verschiebbare_datei_wird_einmal_gemeldet(self, conn, datenordner,
                                                           postfach, monkeypatch):
        """Erledigt, aber nicht zu verschieben (geschuetzt): EINMAL gemeldet,
        mit deutschem Grund und was zu tun ist. Danach merkt sich das Werkzeug
        die Datei -- kein erneutes "schon erfasst", kein Journaleintrag je
        Start, und eine in der Pruefmaske weggeworfene Eingangskopie kommt
        nicht wieder."""
        lieferant(conn)
        lege(postfach, "a.xml", cii("RE-1", "20260301"))
        lege(postfach, "b.docx", b"PK\x03\x04")
        lege(postfach, "c.xml", cii("RE-9", "20260309").replace(b"DE812871812", b"DE999"))
        echt = os.replace

        def verweigert(quelle, ziel, *rest, **optionen):
            if Path(quelle).parent == postfach:
                raise PermissionError(1, "Operation not permitted", str(quelle))
            return echt(quelle, ziel, *rest, **optionen)

        monkeypatch.setattr(os, "replace", verweigert)
        erster = hole(conn, datenordner, postfach)
        assert len(erster.gebucht) == 1 and len(erster.wartend) == 1
        assert sorted(n.originalname for n in erster.nicht_verschoben) == [
            "a.xml", "b.docx", "c.xml"]
        assert all("Geschützt" in n.grund for n in erster.nicht_verschoben)
        assert erster.zusammenfassung().endswith(
            "; alle 3 ließen sich im Ordner nicht verschieben.")
        [wartend] = erster.wartend
        (datenordner / "eingang" / wartend.eingangsname).unlink()  # in der Pruefmaske weggeworfen
        for _ in range(2):
            assert hole(conn, datenordner, postfach).leer()
        assert pipeline.liegen_geblieben(datenordner) == []
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEGORDNER_ABGEHOLT'") == 1
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
        monkeypatch.undo()
        assert hole(conn, datenordner, postfach).leer()
        assert namen(postfach / abholen.UEBERNOMMEN) == ["a.xml", "c.xml"]
        assert namen(postfach / abholen.NICHT_UEBERNOMMEN) == ["b.docx"]

    def test_ordner_ohne_schreibrecht_holt_nichts_ab(self, conn, datenordner, postfach,
                                                     monkeypatch):
        """Ist der Eingangsordner nachtraeglich nicht mehr beschreibbar, sagt
        der Lauf das einmal als Fehler und nimmt nichts auf -- statt bei jedem
        Lauf dieselben Dateien neu aufzunehmen, zu melden und zu protokollieren."""
        lieferant(conn)
        lege(postfach, "a.xml", cii())
        lege(postfach, "b.docx", b"PK\x03\x04")
        os.chmod(postfach, 0o555)
        try:
            if os.access(postfach, os.W_OK):  # als root greift chmod nicht
                echt = os.access
                monkeypatch.setattr(os, "access", lambda pfad, modus, *a, **k: (
                    False if Path(pfad) == postfach and modus == os.W_OK
                    else echt(pfad, modus, *a, **k)))
            for _ in range(2):
                ergebnis = hole(conn, datenordner, postfach)
                assert ergebnis.fehler == abholen.nicht_beschreibbar(postfach)
                assert not ergebnis.veraendert()
        finally:
            os.chmod(postfach, 0o755)
        assert "Abgeholt wurde nichts" in ergebnis.fehler
        assert pipeline.liegen_geblieben(datenordner) == []
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEGORDNER_ABGEHOLT'") == 0

    def test_unveraenderliche_datei_echt(self, conn, datenordner, postfach):
        """Dasselbe mit einer wirklich unveraenderlichen Datei (chattr +i, nur
        als root auf ext4 & Co.)."""
        pfad = lege(postfach, "gesperrt.docx", b"PK\x03\x04")
        if subprocess.run(["chattr", "+i", str(pfad)], capture_output=True).returncode != 0:
            pytest.skip("chattr +i nicht moeglich")
        try:
            for _ in range(3):
                hole(conn, datenordner, postfach)
        finally:
            subprocess.run(["chattr", "-i", str(pfad)], check=False)
        assert namen(postfach / abholen.NICHT_UEBERNOMMEN) == []
        assert "gesperrt.docx" in namen(postfach)


    def test_abbruch_mitten_im_buchen_macht_die_rechnung_nicht_unsichtbar(
            self, conn, datenordner, postfach, monkeypatch):
        """Der Prozess endet in buche() nach dem Verschieben ins Archiv und vor
        dem Festschreiben (Terminal geschlossen, kill). Die Quelle liegt danach
        noch im Ordner -- nicht schon in "Übernommen" --, und der naechste Lauf
        bucht die Rechnung: genau ein Beleg, die Archivdatei hat ihren
        Datensatz, der Eingang ist leer."""
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii())
        echt = os.chmod

        def abbruch(pfad, *rest, **optionen):
            if pipeline.ARCHIV in Path(pfad).parts:
                raise KeyboardInterrupt  # wie ein Prozessende: kein except greift
            return echt(pfad, *rest, **optionen)

        monkeypatch.setattr(os, "chmod", abbruch)
        with pytest.raises(KeyboardInterrupt):
            hole(conn, datenordner, postfach)
        monkeypatch.undo()
        conn.rollback()
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 0
        assert "Rechnung.xml" in namen(postfach)
        assert len(list((datenordner / pipeline.ARCHIV).rglob("*.xml"))) == 1

        [gebucht] = hole(conn, datenordner, postfach).gebucht
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 1
        assert pipeline.liegen_geblieben(datenordner) == []
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]
        assert pipeline.pruefe_integritaet(conn, datenordner) == []


# ------------------------------------------------- Gleichzeitig und Journal

class TestGleichzeitig:
    def _fuellen(self, conn, postfach):
        # Der 15.01. und nicht mehr der 01.01.: am Jahreswechsel wartet ein
        # Beleg, dessen Zahlung mitgebucht wuerde, auf die Pruefmaske -- hier
        # geht es um gleichzeitige Laeufe, nicht um das Datum.
        lieferant(conn)
        lege(postfach, "a.xml", cii("RE-1", "20260115"))
        lege(postfach, "b.xml", cii("RE-2", "20260201"))
        lege(postfach, "c.xml", cii("RE-3", "20260301", waehrung="USD"))
        lege(postfach, "d.docx", b"PK\x03\x04")
        lege(postfach, "e.xml", cii("RE-1", "20260115"))  # dieselbe wie a.xml

    def _zwei_laeufe(self, datenordner, postfach):
        ergebnisse = []
        start = threading.Barrier(2)

        def lauf():
            verbindung = db.verbinde(datenordner / "steuer.sqlite")
            try:
                start.wait()
                ergebnisse.append(hole(verbindung, datenordner, postfach))
            finally:
                verbindung.close()

        faeden = [threading.Thread(target=lauf) for _ in range(2)]
        for faden in faeden:
            faden.start()
        for faden in faeden:
            faden.join(timeout=60)
        assert len(ergebnisse) == 2
        return ergebnisse

    def _nichts_doppelt(self, conn, datenordner, postfach):
        assert anzahl(conn, "SELECT COUNT(*) FROM beleg") == 2
        assert anzahl(conn, "SELECT COUNT(DISTINCT sha256) FROM belegdatei") == 2
        assert namen(postfach / abholen.UEBERNOMMEN) == ["a.xml", "b.xml", "c.xml", "e.xml"]
        assert namen(postfach / abholen.NICHT_UEBERNOMMEN) == ["d.docx"]
        # Im Eingang wartet nur die USD-Rechnung -- keine Kopie eines schon
        # gebuchten Belegs (Entscheidung f).
        usd = pipeline.aufnehmen(cii("RE-3", "20260301", waehrung="USD"), "c.xml",
                                 datenordner).pfad
        assert pipeline.liegen_geblieben(datenordner) == [usd]

    def test_zwei_laeufe_im_selben_prozess(self, conn, datenordner, postfach):
        """Start und Knopf gleichzeitig: die Sperre ordnet sie. Jede Datei hat
        ueber beide Laeufe genau ein Ergebnis, nichts ist doppelt gebucht."""
        self._fuellen(conn, postfach)
        ergebnisse = self._zwei_laeufe(datenordner, postfach)
        alle = [
            eintrag.originalname
            for e in ergebnisse
            for eintrag in e.gebucht + e.wartend + e.abgewiesen + e.schon_vorhanden
        ]
        assert sorted(alle) == ["a.xml", "b.xml", "c.xml", "d.docx", "e.xml"]
        assert all(e.fehler is None for e in ergebnisse)
        self._nichts_doppelt(conn, datenordner, postfach)

    def test_zwei_prozesse_ohne_gemeinsame_sperre(self, conn, datenordner, postfach,
                                                 monkeypatch):
        """Zwei Prozesse teilen die Sperre nicht. Dann tragen Pruefsumme,
        Eingangsdatei und die Sperre in buche(): kein Doppel, kein Absturz."""
        class KeineSperre:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        monkeypatch.setattr(abholen, "_LAUF", KeineSperre())
        self._fuellen(conn, postfach)
        ergebnisse = self._zwei_laeufe(datenordner, postfach)
        assert all(e.fehler is None for e in ergebnisse)
        self._nichts_doppelt(conn, datenordner, postfach)

    def test_zweiter_prozess_bucht_zwischen_pruefen_und_aufnehmen(self, conn, datenordner,
                                                                  postfach, monkeypatch):
        """Das Wettrennen zweier Prozesse, Schritt fuer Schritt: die Kopie des
        anderen Prozesses liegt im Eingang, als dieser Lauf die Datei liest;
        der andere bucht und archiviert sie, BEVOR dieser Lauf aufnimmt.
        aufnehmen() legt die Kopie dann neu an -- und dieser Lauf raeumt sie
        weg, weil er selbst sie angelegt hat. Im Eingang bleibt keine Kopie
        eines gebuchten Belegs."""
        lid = lieferant(conn)
        fremd = pipeline.aufnehmen(cii(), "Rechnung.xml", datenordner)
        lege(postfach, "Rechnung.xml", cii())
        echt = pipeline.aufnehmen

        def anderer_prozess_bucht_zuerst(inhalt, name, ordner):
            if pipeline.liegen_geblieben(datenordner):
                lesung = pipeline.lies(conn, fremd, heute=HEUTE)
                pipeline.buche(conn, lesung, {"belegdatum": "2026-03-14", "brutto": 546,
                                              "waehrung": "EUR"}, datenordner,
                               lieferant_id=lid, heute=HEUTE)
            return echt(inhalt, name, ordner)

        monkeypatch.setattr(pipeline, "aufnehmen", anderer_prozess_bucht_zuerst)
        ergebnis = hole(conn, datenordner, postfach)
        assert [s.beleg_id for s in ergebnis.schon_vorhanden] == [1]
        assert pipeline.liegen_geblieben(datenordner) == []
        assert namen(datenordner / "eingang") == []

    def test_zweiter_prozess_meldet_nicht_was_der_erste_gerade_bucht(
            self, conn, datenordner, postfach, monkeypatch):
        """Programm doppelt gestartet: der zweite Lauf fand die Eingangskopie,
        die der erste gerade auslas, und meldete sie als "wartet schon in der
        Prüfmaske" -- einen Augenblick spaeter war sie gebucht. Jetzt laesst er
        sie dem ersten: der bucht, verschiebt und meldet sie."""
        class KeineSperre:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        monkeypatch.setattr(abholen, "_LAUF", KeineSperre())
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii())
        liest, weiter = threading.Event(), threading.Event()
        echt = abholen.automatisch_buchen

        def langsam(*a, **k):
            liest.set()
            weiter.wait(10)
            return echt(*a, **k)

        monkeypatch.setattr(abholen, "automatisch_buchen", langsam)
        erster = []

        def lauf():
            verbindung = db.verbinde(datenordner / "steuer.sqlite")
            try:
                erster.append(hole(verbindung, datenordner, postfach))
            finally:
                verbindung.close()

        faden = threading.Thread(target=lauf)
        faden.start()
        try:
            assert liest.wait(10)
            zweiter = hole(conn, datenordner, postfach)
        finally:
            weiter.set()
            faden.join(timeout=30)
        assert zweiter.leer(), zweiter.zusammenfassung()
        assert [g.originalname for g in erster[0].gebucht] == ["Rechnung.xml"]
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]
        assert [p.name for p in (datenordner / "eingang").iterdir()] == []

    def test_ohne_flock_bleibt_keine_sperrdatei_liegen(self, conn, datenordner, postfach,
                                                      monkeypatch):
        """Kann das Laufwerk kein flock (ENOLCK), gilt die Sperre als frei -- und
        die versteckte Sperrdatei bleibt nicht im Eingang liegen."""
        import errno
        import fcntl

        def kein_flock(*a):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(fcntl, "flock", kein_flock)
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii())
        ergebnis = hole(conn, datenordner, postfach)
        assert [g.originalname for g in ergebnis.gebucht] == ["Rechnung.xml"]
        assert [p.name for p in (datenordner / "eingang").iterdir()] == []

    @pytest.mark.parametrize("dazwischen", ["gebucht", "weggeworfen"])
    def test_wartende_kopie_wird_vor_dem_melden_noch_einmal_geprueft(
            self, conn, datenordner, postfach, monkeypatch, dazwischen):
        """Die Kopie lag schon im Eingang (Upload) -- und wird gebucht oder
        weggeworfen, waehrend der Lauf die Quelle verschiebt. Gemeldet wird
        dann "schon gebucht als Beleg 1" bzw. nichts, nicht "wartet schon in
        der Prüfmaske"."""
        lid = lieferant(conn)
        hochgeladen = pipeline.aufnehmen(cii(), "Rechnung.xml", datenordner)
        lege(postfach, "Rechnung.xml", cii())
        echt = abholen._ablegen

        def maske_war_schneller(lauf, *a, **k):
            if dazwischen == "gebucht":
                pipeline.buche(conn, pipeline.lies(conn, hochgeladen, heute=HEUTE),
                               {"belegdatum": "2026-03-14", "brutto": 546, "waehrung": "EUR"},
                               datenordner, lieferant_id=lid, heute=HEUTE)
            else:
                hochgeladen.pfad.unlink()
            return echt(lauf, *a, **k)

        monkeypatch.setattr(abholen, "_ablegen", maske_war_schneller)
        ergebnis = hole(conn, datenordner, postfach)
        if dazwischen == "gebucht":
            assert [(s.originalname, s.beleg_id) for s in ergebnis.schon_vorhanden] == [
                ("Rechnung.xml", 1)]
            assert ergebnis.zusammenfassung() == (
                "Aus dem Eingangsordner: 1 Datei war schon gebucht.")
        else:
            assert ergebnis.schon_vorhanden == []
        assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]

    def test_journal_einmal_je_lauf(self, conn, datenordner, postfach):
        """Ein Eintrag BELEGORDNER_ABGEHOLT je Lauf, in dem etwas passiert ist --
        mit den Zahlen und den (gekuerzten) Namen. Ein leerer Lauf schreibt nichts."""
        lege(postfach, "Rechnung.xml", cii())
        lege(postfach, "x" * 80 + ".docx", b"PK")
        hole(conn, datenordner, postfach)
        hole(conn, datenordner, postfach)
        [zeile] = conn.execute(
            "SELECT wert_neu, begruendung FROM journal WHERE aktion = 'BELEGORDNER_ABGEHOLT'"
        ).fetchall()
        werte = json.loads(zeile["wert_neu"])
        assert (werte["wartend"], werte["abgewiesen"], werte["gebucht"]) == (1, 1, 0)
        assert "Rechnung.xml" in zeile["begruendung"]
        assert "x" * 80 not in zeile["begruendung"]

    def test_gesperrte_datenbank_beendet_den_lauf(self, conn, datenordner, postfach):
        """Haelt ein anderer Vorgang die Datenbank gesperrt, hoert der Lauf bei
        der ersten Buchung auf -- statt fuer jede Datei fuenf Sekunden zu
        warten und alles dauerhaft in die Pruefmaske zu legen. Die Quellen
        bleiben im Ordner, und nach der Freigabe bucht der naechste Lauf sie."""
        lieferant(conn)
        conn.execute("PRAGMA busy_timeout = 100")
        for nummer in (1, 2, 3):
            lege(postfach, f"r{nummer}.xml", cii(f"RE-{nummer}", f"2026030{nummer}"))
        lege(postfach, "Notiz.docx", b"PK\x03\x04")
        sperre = sqlite3.connect(datenordner / "steuer.sqlite", isolation_level=None)
        sperre.execute("BEGIN IMMEDIATE")
        try:
            ergebnis = hole(conn, datenordner, postfach)
        finally:
            sperre.execute("ROLLBACK")
            sperre.close()
        assert ergebnis.fehler == abholen.DATENBANK_BELEGT
        assert (ergebnis.gebucht, ergebnis.wartend) == ([], [])
        assert {"r1.xml", "r2.xml", "r3.xml"} <= set(namen(postfach))
        assert pipeline.liegen_geblieben(datenordner) == []
        danach = hole(conn, datenordner, postfach)
        assert sorted(g.originalname for g in danach.gebucht) == ["r1.xml", "r2.xml", "r3.xml"]
        assert danach.fehler is None

    def test_gesperrte_datenbank_holt_den_laufeintrag_nach(self, conn, datenordner, postfach,
                                                          monkeypatch):
        """Wird die Datenbank erst nach der ersten Buchung gesperrt, steht der
        Laufeintrag dieses Laufs nach der Freigabe trotzdem im Journal: der
        naechste Lauf schreibt ihn nach."""
        lieferant(conn)
        conn.execute("PRAGMA busy_timeout = 100")
        lege(postfach, "a.xml", cii("RE-1", "20260301"))
        lege(postfach, "b.xml", cii("RE-2", "20260302"))
        sperre = sqlite3.connect(datenordner / "steuer.sqlite", isolation_level=None)
        echt = pipeline.buche

        def danach_gesperrt(*a, **k):
            buchung = echt(*a, **k)
            sperre.execute("BEGIN IMMEDIATE")
            return buchung

        monkeypatch.setattr(pipeline, "buche", danach_gesperrt)
        try:
            erster = hole(conn, datenordner, postfach)
        finally:
            sperre.execute("ROLLBACK")
            sperre.close()
        monkeypatch.undo()
        assert [g.originalname for g in erster.gebucht] == ["a.xml"]
        assert erster.fehler == abholen.DATENBANK_BELEGT
        assert anzahl(conn, "SELECT COUNT(*) FROM journal"
                            " WHERE aktion = 'BELEGORDNER_ABGEHOLT'") == 0
        hole(conn, datenordner, postfach)
        werte = [json.loads(z[0]) for z in conn.execute(
            "SELECT wert_neu FROM journal WHERE aktion = 'BELEGORDNER_ABGEHOLT' ORDER BY id")]
        assert [(w["gebucht"], w["lauf"]) for w in werte] == [
            (1, erster.zeitpunkt), (1, werte[1]["lauf"])]

    def test_stoppsignal_zwischen_zwei_dateien(self, conn, datenordner, postfach,
                                               monkeypatch):
        """Beim Beenden (Strg+C) kommt das Stoppsignal: der Lauf hoert nach der
        laufenden Datei auf, schreibt sein Journal, und der Rest bleibt im
        Ordner fuer den naechsten Start."""
        lieferant(conn)
        for nummer in (1, 2, 3):
            lege(postfach, f"r{nummer}.xml", cii(f"RE-{nummer}", f"2026030{nummer}"))
        stopp = threading.Event()
        echt = abholen._eine_datei

        def eine_und_stopp(lauf, eintrag):
            echt(lauf, eintrag)
            stopp.set()

        monkeypatch.setattr(abholen, "_eine_datei", eine_und_stopp)
        ergebnis = hole(conn, datenordner, postfach, stopp=stopp)
        assert [g.originalname for g in ergebnis.gebucht] == ["r1.xml"]
        assert ergebnis.fehler == abholen.BEIM_BEENDEN
        assert {"r2.xml", "r3.xml"} <= set(namen(postfach))
        [zeile] = conn.execute("SELECT wert_neu FROM journal"
                               " WHERE aktion = 'BELEGORDNER_ABGEHOLT'").fetchall()
        assert json.loads(zeile[0])["gebucht"] == 1

    def test_stoppsignal_mitten_in_einer_langsamen_textschicht(self, conn, datenordner,
                                                              postfach):
        """Kommt das Stoppsignal, waehrend die Textschicht eines PDF mit
        Datenmuell gelesen wird, hoert der Lauf sofort auf -- die Datei bleibt
        im Ordner, im Eingang liegt keine halbe Aufnahme."""
        lieferant(conn)
        lege(postfach, "Muell.pdf", PDF_DE.read_bytes() + b"\x00" * (30 * 1024 * 1024))
        stopp = threading.Event()
        threading.Timer(1.0, stopp.set).start()
        beginn = time.monotonic()
        ergebnis = hole(conn, datenordner, postfach, stopp=stopp)
        assert time.monotonic() - beginn < 6
        assert ergebnis.fehler == abholen.BEIM_BEENDEN
        assert namen(postfach) == ["Muell.pdf"]
        assert pipeline.liegen_geblieben(datenordner) == []

    def test_laeuft_schon(self, conn, datenordner, postfach):
        """Mit warten=False wartet ein zweiter Lauf nicht, sondern meldet sich."""
        assert abholen._LAUF.acquire(blocking=False)
        try:
            with pytest.raises(abholen.LaeuftSchon):
                hole(conn, datenordner, postfach, warten=False)
        finally:
            abholen._LAUF.release()


# ----------------------------------------------------- Start und Oberflaeche

def _eingerichtet(app_pfad: Path, postfach: Path | None, *, automatisch: int = 1) -> None:
    """Eine Datenbank mit Betrieb und (wahlweise) Eingangsordner, vor dem Start."""
    verbindung = db.oeffne(app_pfad)
    verbindung.execute(
        "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, belegordner_pfad,"
        " belegordner_automatisch) VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095',"
        " 'Hamburg', '12/345/67890', ?, ?)",
        (str(postfach) if postfach else None, automatisch),
    )
    verbindung.close()


def _warte(anwendung, klient, sekunden: float = 20) -> abholen.Laufstand:
    """Wartet auf den Startlauf. Erst eine Anfrage, wie vom Browser, den
    start.command oeffnet: sie zeigt, dass der Server lauscht -- erst dann
    beginnt das Abholen."""
    klient.get("/gesund")
    ende = time.time() + sekunden
    while anwendung.state.belegordner.laeuft and time.time() < ende:
        time.sleep(0.02)
    assert not anwendung.state.belegordner.laeuft, "Startlauf wurde nicht fertig"
    return anwendung.state.belegordner


class TestStart:
    def test_start_holt_im_hintergrund_ab(self, app_pfad, tmp_path):
        """Beim Start laeuft das Abholen in einem eigenen Thread; das Ergebnis
        steht danach im Anwendungszustand und auf Uebersicht und Belegseite."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        lege(postfach, "Rechnung.xml", cii())
        _eingerichtet(app_pfad, postfach)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            stand = _warte(anwendung, c)
            assert [w.originalname for w in stand.ergebnis.wartend] == ["Rechnung.xml"]
            assert stand.zeitpunkt == stand.ergebnis.zeitpunkt
            assert "1 Rechnung wartet in der Prüfmaske" in c.get("/").text
            seite = c.get("/belege").text
            assert "Zuletzt abgeholt" in seite
            assert "neuer Lieferant" in seite

    def test_zweiter_start_auf_belegtem_port_holt_nichts_ab(self, app_pfad, tmp_path):
        """uvicorn startet die Anwendung VOR dem Binden des Ports. Scheitert das
        Binden (start.command doppelt geoeffnet), endet der Prozess gleich
        wieder -- ohne eine Anfrage. Dann darf er nichts abgeholt, gebucht oder
        verschoben haben."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        lege(postfach, "Rechnung.xml", cii())
        _eingerichtet(app_pfad, postfach)
        anwendung = erzeuge_app(app_pfad)
        beginn = time.monotonic()
        with TestClient(anwendung):
            pass  # Start und sofort wieder Ende, wie nach "address already in use"
        assert time.monotonic() - beginn < 5
        assert namen(postfach) == ["Rechnung.xml"]
        assert pipeline.liegen_geblieben(app_pfad.parent) == []
        assert anwendung.state.belegordner.laeuft is False

    def test_ohne_anfrage_beginnt_das_abholen_nach_kurzer_zeit(self, app_pfad, tmp_path,
                                                              monkeypatch):
        """Kommt keine Anfrage (kein Browser), beginnt das Abholen trotzdem --
        sobald der Server sicher lauscht."""
        import app.main as hauptmodul

        monkeypatch.setattr(hauptmodul, "ABHOLEN_NACH_SEKUNDEN", 0.2)
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        lege(postfach, "Rechnung.xml", cii())
        _eingerichtet(app_pfad, postfach)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung):
            ende = time.time() + 10
            while anwendung.state.belegordner.ergebnis is None and time.time() < ende:
                time.sleep(0.05)
            assert [w.originalname for w in anwendung.state.belegordner.ergebnis.wartend] == [
                "Rechnung.xml"]

    def test_beenden_wartet_nicht_auf_einen_langen_lauf(self, app_pfad, tmp_path,
                                                       monkeypatch):
        """Strg+C waehrend eines langen Startlaufs: das Beenden setzt das
        Stoppsignal, der Lauf hoert zwischen zwei Dateien auf und schreibt sein
        Journal -- statt 15 s zu blockieren und dann mittendrin abzubrechen."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        for nummer in range(40):
            lege(postfach, f"Notiz {nummer:02d}.docx", b"PK\x03\x04")
        _eingerichtet(app_pfad, postfach)
        echt = abholen._eine_datei

        def langsam(lauf, eintrag):
            time.sleep(0.2)
            echt(lauf, eintrag)

        monkeypatch.setattr(abholen, "_eine_datei", langsam)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            c.get("/gesund")
            time.sleep(0.6)
            beginn = time.monotonic()
        assert time.monotonic() - beginn < 3
        stand = anwendung.state.belegordner
        assert stand.laeuft is False
        assert stand.ergebnis.fehler == abholen.BEIM_BEENDEN
        assert 0 < len(stand.ergebnis.abgewiesen) < 40
        assert len(abfrage(app_pfad, "SELECT * FROM journal"
                                     " WHERE aktion = 'BELEGORDNER_ABGEHOLT'")) == 1

    def test_verwaiste_zwischendateien_werden_beim_start_weggeraeumt(self, app_pfad):
        """Brach ein Prozess mitten im Aufnehmen ab, bleibt eine versteckte
        Zwischendatei im Eingang. Der naechste Start raeumt sie weg -- eine
        junge (ein Upload, der gerade laeuft) bleibt."""
        _eingerichtet(app_pfad, None)
        eingang = app_pfad.parent / "eingang"
        eingang.mkdir()
        alt = lege(eingang, ".4300007722b9b964.pdf.9dacaab64aa0.teil", b"%PDF", alter=3600)
        jung = lege(eingang, ".5300007722b9b964.pdf.1dacaab64aa0.teil", b"%PDF", alter=5)
        with TestClient(erzeuge_app(app_pfad)):
            pass
        assert not alt.exists()
        assert jung.exists()

    def test_gesperrte_datenbank_beim_start(self, app_pfad):
        """Haelt ein anderer Vorgang die Datenbank beim Start laenger gesperrt
        als die Verbindung wartet, startet der Server trotzdem: jede Seite sagt
        auf Deutsch, was los ist (503), und nach der Freigabe geht es ohne
        Neustart weiter."""
        _eingerichtet(app_pfad, None)
        sperre = sqlite3.connect(app_pfad, isolation_level=None)
        sperre.execute("BEGIN EXCLUSIVE")
        try:
            anwendung = erzeuge_app(app_pfad)
            with TestClient(anwendung) as c:
                antwort = c.get("/")
                assert antwort.status_code == 503
                assert "gerade von einem anderen Vorgang gesperrt" in antwort.text
                sperre.execute("ROLLBACK")
                assert c.get("/gesund").json()["status"] == "ok"
                assert c.get("/").status_code == 200
        finally:
            sperre.close()

    def test_startlauf_wird_nach_der_sperre_nachgeholt(self, app_pfad, tmp_path, monkeypatch):
        """War die Datenbank beim Start gesperrt, entfiel das Abholen fuer die
        ganze Sitzung -- die Rechnung vom Handy lag bis zum naechsten Start im
        Ordner. Gelingt das Oeffnen spaeter, wird der Startlauf nachgeholt,
        und zwar einmal, auch wenn danach weitere Anfragen kommen."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        lege(postfach, "Rechnung.xml", cii())
        _eingerichtet(app_pfad, postfach)
        laeufe = []
        # Der Start holt alle hinterlegten Ordner in einem Lauf ab.
        echt = abholen.abholen_alle

        def gezaehlt(*a, **k):
            laeufe.append(1)
            return echt(*a, **k)

        monkeypatch.setattr(abholen, "abholen_alle", gezaehlt)
        sperre = sqlite3.connect(app_pfad, isolation_level=None)
        sperre.execute("BEGIN EXCLUSIVE")
        try:
            anwendung = erzeuge_app(app_pfad)
            with TestClient(anwendung) as c:
                assert c.get("/").status_code == 503
                assert laeufe == []
                sperre.execute("ROLLBACK")
                assert c.get("/belege").status_code == 200
                stand = _warte(anwendung, c)
                assert stand.ergebnis is not None, "Startlauf nicht nachgeholt"
                assert [w.originalname for w in stand.ergebnis.wartend] == ["Rechnung.xml"]
                for _ in range(3):
                    assert c.get("/").status_code == 200
                assert laeufe == [1]
                assert namen(postfach / abholen.UEBERNOMMEN) == ["Rechnung.xml"]
        finally:
            sperre.close()

    def test_ohne_ordner_passiert_nichts(self, app_pfad):
        """Kein Eingangsordner eingestellt: kein Thread, kein Ergebnis."""
        _eingerichtet(app_pfad, None)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            assert anwendung.state.belegordner.laeuft is False
            assert anwendung.state.belegordner.ergebnis is None
            assert "Eingangsordner" not in c.get("/").text

    def test_ohne_betrieb_passiert_nichts(self, app_pfad):
        """Vor der Einrichtung gibt es nichts abzuholen."""
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung):
            assert anwendung.state.belegordner.laeuft is False
            assert anwendung.state.belegordner.ergebnis is None

    def test_ausnahme_im_thread_reisst_den_server_nicht_mit(self, app_pfad, tmp_path,
                                                           monkeypatch, capsys):
        """Eine Ausnahme im Hintergrund wird zum deutschen Fehlertext; der
        Verlauf geht ins Terminal, der Server antwortet weiter."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        _eingerichtet(app_pfad, postfach)

        def kaputt(*a, **k):
            raise RuntimeError("Platte weg")

        # Der Start holt alle hinterlegten Ordner in einem Lauf ab.
        monkeypatch.setattr(abholen, "abholen_alle", kaputt)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            stand = _warte(anwendung, c)
            assert stand.ergebnis.fehler == (
                "Der Eingangsordner ließ sich beim Start nicht abholen: Platte weg.")
            assert c.get("/gesund").json()["status"] == "ok"
            assert "Platte weg" in c.get("/").text
        assert "Traceback" in capsys.readouterr().err

    def test_knopf_jetzt_abholen(self, app_pfad, tmp_path):
        """"Jetzt abholen" holt synchron ab und meldet die Zusammenfassung."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        _eingerichtet(app_pfad, postfach)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            _warte(anwendung, c)
            lege(postfach, "Aufstellung.docx", b"PK")
            antwort = c.post("/belege/ordner/abholen", follow_redirects=True)
            assert "1 Datei nicht übernommen" in antwort.text
            assert "Nicht übernommen" in antwort.text
            assert anwendung.state.belegordner.ergebnis.abgewiesen[0].originalname == \
                "Aufstellung.docx"
            leer = c.post("/belege/ordner/abholen", follow_redirects=True)
            assert "Im Eingangsordner lag nichts Neues." in leer.text

    def test_knopf_mit_ungespeichertem_haken_oder_pfad(self, app_pfad, tmp_path):
        """"Jetzt abholen" schickt das Einstellungsformular mit. Ist der Haken
        "gleich buchen" sichtbar herausgenommen oder ein anderer Ordner
        getippt, aber nicht gespeichert, wird NICHT abgeholt (sonst buchte die
        Route nach der gespeicherten Einstellung -- unloeschbar): 422 mit
        "erst speichern", und Feld und Haken bleiben, wie der Nutzer sie sah."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        anderer = tmp_path / "Anderer"
        anderer.mkdir()
        _eingerichtet(app_pfad, postfach, automatisch=1)
        verbindung = db.verbinde(app_pfad)
        lieferant(verbindung)
        verbindung.close()
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            _warte(anwendung, c)
            lege(postfach, "Rechnung.xml", cii())
            ohne_haken = c.post("/belege/ordner/abholen", data={"pfad": str(postfach)})
            assert ohne_haken.status_code == 422
            assert "noch nicht gespeichert" in ohne_haken.text
            assert re.search(r'name="automatisch" value="1"\s*>', ohne_haken.text)  # ohne Haken
            anderer_pfad = c.post("/belege/ordner/abholen",
                                  data={"pfad": str(anderer), "automatisch": "1"})
            assert anderer_pfad.status_code == 422
            assert f'value="{anderer}"' in anderer_pfad.text
            assert abfrage(app_pfad, "SELECT * FROM beleg") == []
            assert "Rechnung.xml" in namen(postfach)
            gleich = c.post("/belege/ordner/abholen",
                            data={"pfad": str(postfach), "automatisch": "1"},
                            follow_redirects=True)
            assert "1 Rechnung automatisch gebucht" in gleich.text

    def test_knopf_ohne_doppelte_und_ohne_gruene_meldung(self, app_pfad, tmp_path):
        """Nach "Jetzt abholen" steht das Ergebnis einmal, im Abschnitt
        "Eingangsordner" und in seiner Farbe (gelb bei "nicht übernommen") --
        nicht noch einmal gruen oben. Meldungen oben werden nicht gedruckt."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        _eingerichtet(app_pfad, postfach)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            _warte(anwendung, c)
            lege(postfach, "Brief.docx", b"PK\x03\x04")
            antwort = c.post("/belege/ordner/abholen", follow_redirects=False)
            assert antwort.headers["location"] == "/belege#eingangsordner"
            text = c.get("/belege").text
            satz = "Aus dem Eingangsordner: 1 Datei nicht übernommen."
            assert text.count(satz) == 1
            assert 'class="meldung erfolg' not in text
            assert re.search(r'<div class="meldung warn\s*">\s*<strong>Zuletzt abgeholt', text)
            hinweis = c.get("/belege?erfolg=x").text
            assert '<div class="meldung erfolg nicht-drucken">x</div>' in hinweis

    def test_knopf_waehrend_eines_laufs(self, app_pfad, tmp_path):
        """Laeuft gerade ein Lauf, wartet der Knopf nicht: "läuft gerade"."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        _eingerichtet(app_pfad, postfach)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            _warte(anwendung, c)
            assert abholen._LAUF.acquire(blocking=False)
            try:
                antwort = c.post("/belege/ordner/abholen", follow_redirects=True)
            finally:
                abholen._LAUF.release()
            assert "Das Abholen läuft gerade" in antwort.text

    def test_knopf_ohne_ordner(self, klient):
        """Ohne eingestellten Ordner sagt der Knopf, was zu tun ist."""
        antwort = klient.post("/belege/ordner/abholen", follow_redirects=True)
        assert "noch kein Eingangsordner eingestellt" in antwort.text

    def test_laeuft_gerade_auf_den_seiten(self, klient):
        """Solange der Startlauf arbeitet, sagen Belegseite und Uebersicht es."""
        klient.app.state.belegordner.laeuft = True
        try:
            assert "Läuft gerade" in klient.get("/belege").text
            assert "wird gerade abgeholt" in klient.get("/").text
        finally:
            klient.app.state.belegordner.laeuft = False


class TestPruefmaske:
    """Die Pruefmaske liest die Textschicht wie das Abholen: mit Zeitgrenze."""

    @pytest.fixture(autouse=True)
    def _frischer_dienst(self, monkeypatch):
        """Der Dienst der Pruefmaske und sein Gedaechtnis gelten je Prozess --
        je Test frisch, und die Prozessstarts gezaehlt."""
        beenden = getattr(textdienst, "geteilt_beenden", lambda: None)
        beenden()
        monkeypatch.setattr(textdienst, "_ZU_LANGSAM", {}, raising=False)
        self.starts = []
        echt = textdienst.Textdienst._starten

        def zaehlen(dienst):
            self.starts.append(1)
            return echt(dienst)

        monkeypatch.setattr(textdienst.Textdienst, "_starten", zaehlen)
        yield
        beenden()

    def test_pdf_mit_datenmuell_haelt_die_maske_nicht_fest(self, klient, app_pfad,
                                                          monkeypatch):
        """Ein PDF, dessen Textschicht Minuten braucht, hielt die Maske (und
        danach das Buchen) fest. Jetzt: nach der Zeitgrenze die Handeingabe mit
        Hinweis -- und weder das Neuladen noch das Buchen liest es noch einmal."""
        # Eine halbe Sekunde: 30 MB Datenmuell braucht pdfminer auf jedem
        # Rechner deutlich laenger. Mit 2 s waere der Test auf einem schnellen
        # Rechner knapp geworden.
        monkeypatch.setattr(textdienst, "ZEITGRENZE_SEKUNDEN", 0.5)
        eingang = pipeline.aufnehmen(PDF_DE.read_bytes() + b"\x00" * (30 * 1024 * 1024),
                                     "Muell.pdf", app_pfad.parent)
        beginn = time.monotonic()
        antwort = klient.get(f"/belege/pruefen/{eingang.pfad.name}")
        assert antwort.status_code == 200
        assert time.monotonic() - beginn < 15
        assert "Die Textschicht ließ sich nicht in 0.5 Sekunden lesen" in antwort.text
        assert "Bitte die Angaben von Hand aus dem Dokument eintragen." in antwort.text
        assert "aus der Textschicht" not in antwort.text
        assert klient.get(f"/belege/pruefen/{eingang.pfad.name}").status_code == 200
        antwort = klient.post(f"/belege/pruefen/{eingang.pfad.name}", data={
            "belegdatum": "2026-03-14", "brutto_euro": "8,29", "quote": "100",
            "lieferant_neu": "Muell GmbH"}, follow_redirects=False)
        assert antwort.status_code == 303
        assert "Beleg 1 erfasst" in unquote(antwort.headers["location"])
        assert len(self.starts) == 1

    def test_gewoehnliche_pdfs_teilen_sich_einen_prozess(self, klient, app_pfad):
        """Ein Prozessstart je Anfrage kostete jede Maske Zeit (Python und
        pdfplumber laden). Der Prozess bleibt zwischen den Anfragen stehen."""
        namen_ = [pipeline.aufnehmen(hetzner_pdf(nummer=f"Rechnungsnummer: R{n}"),
                                     f"R{n}.pdf", app_pfad.parent).pfad.name for n in range(3)]
        for name in namen_:
            antwort = klient.get(f"/belege/pruefen/{name}")
            assert antwort.status_code == 200
            assert "aus der Textschicht" in antwort.text
        assert len(self.starts) == 1

    def test_ohne_textdienst_liest_die_maske_selbst(self, klient, app_pfad, monkeypatch):
        """Laesst sich kein Prozess starten (OSError beim Start), liest die Maske
        wie frueher im Server -- statt "liegt nicht im Eingang" zu melden."""
        def kein_prozess(dienst):
            raise OSError(35, "Resource temporarily unavailable")

        monkeypatch.setattr(textdienst.Textdienst, "_starten", kein_prozess)
        name = pipeline.aufnehmen(hetzner_pdf(), "R.pdf", app_pfad.parent).pfad.name
        antwort = klient.get(f"/belege/pruefen/{name}")
        assert antwort.status_code == 200
        assert "aus der Textschicht" in antwort.text


class TestOberflaeche:
    def test_abschnitt_mit_anleitung_und_ohne_druck(self, klient):
        """Der Abschnitt erklaert den Weg vom iPhone und erscheint nicht im Druck."""
        text = klient.get("/belege").text
        assert '<section class="nicht-drucken" id="eingangsordner">' in text
        assert "„In Dateien sichern“ → iCloud Drive → „Steuer-Tool Belege“" in text
        assert "Sicher erkannte Rechnungen bekannter Lieferanten gleich buchen" in text
        assert 'action="/belege/ordner"' in text

    def test_platzhalter_sieht_nicht_wie_ein_wert_aus(self, klient, monkeypatch):
        """Ein grauer Pfad im leeren Feld las sich wie ein eingetragener Ordner.
        Der Platzhalter beginnt deshalb mit "z. B." -- mit und ohne Vorschlag."""
        def platzhalter():
            text = klient.get("/belege").text
            feld = re.search(r'<input name="pfad"[^>]*>', text).group(0)
            return re.search(r'placeholder="([^"]*)"', feld).group(1)

        assert platzhalter() == "z. B. /Pfad/zum/Ordner/Steuer-Tool Belege"
        monkeypatch.setattr(abholen, "_ist_mac", lambda: True)
        vorschlag = str(abholen.vorschlag_ordner())
        assert platzhalter() == f"z. B. {vorschlag}"

    def test_readme_gibt_146_ao_richtig_wieder(self):
        """§ 146 Abs. 4 AO ist das Radierverbot -- dass sich ein Beleg nicht
        loeschen laesst, ist die Umsetzung des Werkzeugs, nicht der Wortlaut."""
        text = (Path(__file__).resolve().parent.parent / "README.md").read_text()
        abschnitt = text[text.index("## Rechnungen vom Handy"):text.index("## Entwicklung")]
        assert "nach § 146 Abs. 4 AO unveränderlich" not in abschnitt
        assert ("nach § 146 Abs. 4 AO darf eine Buchung nicht so\nverändert werden, dass der "
                "ursprüngliche Inhalt nicht mehr feststellbar ist") in abschnitt

    def test_lange_namen_brechen_im_kasten_um(self):
        """Dateinamen aus Mail-Anhaengen haben oft keine Leerzeichen. Im
        Ergebniskasten (.meldung) brechen sie um, statt die Seite auf dem Handy
        waagerecht aufzuweiten (im Browser bei 390 px: 757 px breit vorher)."""
        css = (Path(__file__).resolve().parent.parent / "app" / "static" / "stil.css").read_text()
        regel = css[css.index(".meldung {"):]
        regel = regel[:regel.index("}")]
        assert "overflow-wrap: anywhere" in regel

    def test_iphone_anleitung_passt_zum_eingestellten_ordner(self, klient, tmp_path):
        """Die Anleitung nennt den tatsaechlichen Ordner in iCloud Drive und den
        letzten Schritt "Sichern". Fuer einen Ordner ausserhalb von iCloud Drive
        steht dort, dass er vom iPhone so nicht erreichbar ist."""
        icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        (icloud / "Belege 2026").mkdir(parents=True)
        klient.post("/belege/ordner", data={"pfad": str(icloud / "Belege 2026"),
                                            "automatisch": "1"})
        text = klient.get("/belege").text
        assert "„In Dateien sichern“ → iCloud Drive → „Belege 2026“ → Sichern." in text
        lokal = tmp_path / "Belege lokal"
        lokal.mkdir()
        klient.post("/belege/ordner", data={"pfad": str(lokal), "automatisch": "1"})
        text = klient.get("/belege").text
        assert "„In Dateien sichern“" not in text
        assert "Dieser Ordner liegt nicht in iCloud Drive" in text

    def test_links_auf_belege_treffen_auch_bei_filter(self, klient):
        """Die Links im Ergebnis zeigen auf die ungefilterte Liste
        ("/belege#beleg-N"): in /belege?jahr=2025 gaebe es das Element sonst
        nicht, und der Klick bewirkte nichts."""
        klient.app.state.belegordner.fertig(abholen.Ergebnis(
            gebucht=[abholen.Gebucht(13, "Hetzner Juli.xml", "Hetzner", 546)],
            schon_vorhanden=[abholen.SchonVorhanden("Doppel.xml", 13)]))
        text = klient.get("/belege?jahr=2025").text
        assert 'href="/belege#beleg-13">Hetzner Juli.xml</a>' in text
        assert 'href="/belege#beleg-13">Beleg 13</a>' in text
        assert 'href="#beleg-' not in text

    def test_stimmt_so(self, app_pfad, tmp_path):
        """Ein automatisch gebuchter Beleg traegt den Chip und den Knopf; "Stimmt
        so" setzt geprueft_am genau einmal, mit Journal."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        _eingerichtet(app_pfad, postfach)
        verbindung = db.verbinde(app_pfad)
        lieferant(verbindung)
        verbindung.close()
        lege(postfach, "Rechnung.xml", cii())
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            [gebucht] = _warte(anwendung, c).ergebnis.gebucht
            liste = c.get("/belege").text
            assert "automatisch gebucht" in liste
            assert f'action="/belege/{gebucht.beleg_id}/geprueft"' in liste
            assert f'id="beleg-{gebucht.beleg_id}"' in c.get("/belege?offen=1").text

            antwort = c.post(f"/belege/{gebucht.beleg_id}/geprueft", follow_redirects=False)
            # Die Meldung steht ueber der Zeile, der Sprung geht auf sie.
            assert antwort.headers["location"].endswith("#meldung")
            assert f"beleg={gebucht.beleg_id}" in antwort.headers["location"]
            nochmal = c.post(f"/belege/{gebucht.beleg_id}/geprueft", follow_redirects=True)
            assert "war schon bestätigt" in nochmal.text
            assert f'action="/belege/{gebucht.beleg_id}/geprueft"' not in nochmal.text
            assert f'id="beleg-{gebucht.beleg_id}"' not in c.get("/belege?offen=1").text
            unbekannt = c.post("/belege/999/geprueft", follow_redirects=True)
            assert "Diesen Beleg gibt es nicht" in unbekannt.text
        assert abfrage(app_pfad, "SELECT geprueft_am FROM beleg")[0]["geprueft_am"]
        assert len(abfrage(app_pfad, "SELECT * FROM journal WHERE aktion = 'BELEG_GEPRUEFT'")) == 1

    def test_bezahlt_aber_unbestaetigt_steht_auf_euer_und_uebersicht(self, app_pfad,
                                                                    tmp_path):
        """Ein automatisch gebuchter Beleg mit erfasster Zahlung zaehlt schon in
        der EUeR, obwohl niemand ihn bestaetigt hat. EUeR-Seite und Uebersicht
        sagen das, mit Link auf die ungeklaerten Belege -- bis "Stimmt so"."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        _eingerichtet(app_pfad, postfach)
        verbindung = db.verbinde(app_pfad)
        lieferant(verbindung)
        # Ohne die Vorgabe "in der Regel schon bezahlt": die Zahlung kommt hier
        # erst von Hand. Mit ihr steht der Hinweis gleich nach dem Abholen da
        # (tests/test_eingang_bezahlt.py).
        verbindung.execute("UPDATE betrieb SET belege_bezahlt_vorgabe = 0")
        verbindung.close()
        lege(postfach, "Rechnung.xml", cii())
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            [gebucht] = _warte(anwendung, c).ergebnis.gebucht
            satz = "1 bezahlter, automatisch gebuchter Beleg"
            assert satz not in c.get("/euer?jahr=2026").text  # noch ohne Zahlung
            verbindung = db.verbinde(app_pfad)
            verbindung.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                               " VALUES (?, '2026-03-20', -546, 'UEBERWEISUNG')",
                               (gebucht.beleg_id,))
            verbindung.close()
            for seite in ("/euer?jahr=2026", "/"):
                text = c.get(seite).text
                assert satz in text, seite
                assert "5,46 € zählen schon in der" in text
            c.post(f"/belege/{gebucht.beleg_id}/geprueft")
            assert satz not in c.get("/euer?jahr=2026").text
            assert satz not in c.get("/").text

    def test_dashboard_hinweis(self, klient):
        """Die Uebersicht zeigt die Zusammenfassung des letzten Laufs mit Link --
        aber nur, wenn etwas passiert ist."""
        stand = klient.app.state.belegordner
        stand.fertig(abholen.Ergebnis())
        assert "Zu den Belegen" not in klient.get("/").text
        stand.fertig(abholen.Ergebnis(
            gebucht=[abholen.Gebucht(1, "a.xml", "Hetzner", 546)],
            wartend=[abholen.Wartend("x.pdf", "b.pdf", "neuer Lieferant")]))
        text = klient.get("/").text
        assert ("Aus dem Eingangsordner: 1 Rechnung automatisch gebucht und 1 Rechnung "
                "wartet in der Prüfmaske.") in text
        assert 'href="/belege#eingangsordner"' in text

    def test_altes_ergebnis_verschwindet_nach_ausschalten_oder_wechsel(self, app_pfad,
                                                                     tmp_path):
        """Das Ergebnis des letzten Laufs gehoert zu seinem Ordner. Nach dem
        Ausschalten oder dem Wechsel auf einen anderen Ordner zeigen weder
        Uebersicht noch Belegseite den alten Kasten ("Kein Zugriff … dann
        „Jetzt abholen“" ohne den Knopf)."""
        postfach = tmp_path / "Postfach"
        postfach.mkdir()
        _eingerichtet(app_pfad, postfach)
        anwendung = erzeuge_app(app_pfad)
        with TestClient(anwendung) as c:
            _warte(anwendung, c)
            anwendung.state.belegordner.fertig(abholen.Ergebnis(
                fehler="Kein Zugriff auf den Eingangsordner X.", ordner=str(postfach)))
            assert "Kein Zugriff auf den Eingangsordner X." in c.get("/").text
            assert "Kein Zugriff auf den Eingangsordner X." in c.get("/belege").text
            anderer = tmp_path / "Anderer"
            anderer.mkdir()
            c.post("/belege/ordner", data={"pfad": str(anderer), "automatisch": "1"})
            assert "Kein Zugriff auf den Eingangsordner X." not in c.get("/").text
            assert "Kein Zugriff auf den Eingangsordner X." not in c.get("/belege").text
            c.post("/belege/ordner", data={"pfad": "", "automatisch": "1"})
            assert "Kein Zugriff auf den Eingangsordner X." not in c.get("/").text
            assert "Zuletzt abgeholt" not in c.get("/belege").text

    def test_wartende_datei_verlinkt_die_pruefmaske(self, klient, app_pfad):
        """Eine wartende Datei fuehrt zur Pruefmaske -- solange sie noch im
        Eingang liegt."""
        eingang = pipeline.aufnehmen(cii(), "Rechnung.xml", app_pfad.parent)
        klient.app.state.belegordner.fertig(abholen.Ergebnis(
            wartend=[abholen.Wartend(eingang.pfad.name, "Rechnung.xml", "neuer Lieferant"),
                     abholen.Wartend("0000000000000000.xml", "Weg.xml", "neuer Lieferant")]))
        text = klient.get("/belege").text
        assert f'href="/belege/pruefen/{eingang.pfad.name}">Rechnung.xml</a>' in text
        assert "Weg.xml <span class=\"gedaempft\">(inzwischen erledigt)</span>" in text


class TestOrdnerschutz:
    """Ob zwei Pfade derselbe Ordner sind, entscheidet das Dateisystem."""

    def test_andere_schreibweise_des_datenordners(self, tmp_path):
        """Auf dem Mac unterscheidet APFS weder Gross-/Kleinschreibung noch
        zerlegte Umlaute: "steuer-tool" IST der Datenordner "Steuer-Tool",
        ein NFD-"ü" derselbe Ordner wie das NFC-"ü". Beides wird abgewiesen
        (hier, wo es die Schreibweise nicht gibt, ueber den Textvergleich)."""
        daten = tmp_path / "Application Support" / "Steuer-Tool"
        daten.mkdir(parents=True)
        for ordner in (tmp_path / "application support" / "steuer-tool",
                       tmp_path / "Application Support" / "steuer-tool" / "eingang"):
            assert "Datenordner" in abholen.ordnerfehler(daten, ordner), ordner
        umlaut = tmp_path / unicodedata.normalize("NFC", "Grüße")
        umlaut.mkdir()
        zerlegt = tmp_path / unicodedata.normalize("NFD", "Grüße") / "eingang"
        assert "Datenordner" in abholen.ordnerfehler(umlaut, zerlegt)

    def test_sammelordner_in_anderer_schreibweise(self, monkeypatch, tmp_path):
        """"~/downloads" ist auf dem Mac der Ordner Downloads."""
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "heim"))
        (tmp_path / "heim" / "Downloads").mkdir(parents=True)
        for ordner in ("downloads", "DESKTOP"):
            assert "allgemeiner Ordner" in abholen.ordnerfehler(tmp_path / "daten",
                                                                tmp_path / "heim" / ordner)

    def test_datenordner_unter_zweitem_pfad_wird_nicht_ausgeraeumt(self, conn, datenordner,
                                                                 tmp_path):
        """Derselbe Datenordner unter einem zweiten Pfad (Bind-Mount; auf dem Mac
        ein Firmlink wie /System/Volumes/Data/Users/...): am Text nicht zu
        erkennen, an Geraet und Inode schon. Das Abholen verweigert, und
        steuer.sqlite samt -wal und -shm bleibt, wo es ist."""
        zweiter = tmp_path / "zweiter-weg"
        zweiter.mkdir()
        if subprocess.run(["mount", "--bind", str(datenordner), str(zweiter)],
                          capture_output=True).returncode != 0:
            pytest.skip("Bind-Mount nur als root")
        try:
            vorher = namen(datenordner)
            assert "steuer.sqlite" in vorher
            ergebnis = hole(conn, datenordner, zweiter)
            assert "Datenordner" in ergebnis.fehler
            assert namen(datenordner) == vorher
            assert abholen.ordnerfehler(datenordner, zweiter / "eingang") is not None
        finally:
            subprocess.run(["umount", str(zweiter)], check=False)

    def test_datenbankdateien_werden_nie_verschoben(self, conn, datenordner, postfach):
        """Liegen Datenbankdateien oder Sicherungsarchive doch im Eingangsordner
        (von Hand kopiert), bleiben sie unberuehrt -- keine Meldung, kein
        Verschieben."""
        for name in ("steuer.sqlite", "steuer.sqlite-wal", "steuer.sqlite-shm",
                     "2026-10-24_steuer-backup.zip"):
            lege(postfach, name, b"SQLite format 3\x00")
        assert hole(conn, datenordner, postfach).leer()
        assert namen(postfach) == ["2026-10-24_steuer-backup.zip", "steuer.sqlite",
                                   "steuer.sqlite-shm", "steuer.sqlite-wal"]


class TestExcel:
    def test_betriebsblatt_mit_gepflegten_ueberschriften(self, conn, datenordner, postfach):
        """Im Blatt "Betrieb" heissen die neuen Spalten wie alles andere
        verstaendlich, und der Schalter steht als "ja"/"nein" statt als 1.
        Seit es Ordner je Jahr gibt, heisst dieser "(alle Jahre)"."""
        from openpyxl import load_workbook

        from app.logik import export_excel

        export_excel.schreibe(conn, datenordner / "daten.xlsx")
        blatt = load_workbook(datenordner / "daten.xlsx")["Betrieb"]
        kopf = [zelle.value for zelle in blatt[1]]
        werte = dict(zip(kopf, [zelle.value for zelle in blatt[2]], strict=False))
        assert werte["Eingangsordner (alle Jahre)"] == str(postfach)
        assert werte["Sicher Erkanntes automatisch buchen"] == "ja"
        assert werte["Eingangsbelege in der Regel schon bezahlt"] == "ja"
        assert "Belegordner pfad" not in kopf


class TestZusammenfassung:
    def test_einzahl_und_mehrzahl(self):
        """Ein Satz, mit richtiger Einzahl und Mehrzahl je Teil."""
        ergebnis = abholen.Ergebnis(
            schon_vorhanden=[abholen.SchonVorhanden("a", 1)],
            icloud_ausstehend=["b", "c"], noch_im_schreiben=["d"])
        assert ergebnis.zusammenfassung() == (
            "Aus dem Eingangsordner: 1 Datei war schon gebucht, 2 Dateien laden noch aus "
            "iCloud und 1 Datei wird gerade noch gespeichert.")

    def test_geschuetzte_dateien_zaehlen_einmal(self, conn, datenordner, postfach,
                                               monkeypatch):
        """Zwei geschuetzte Dateien (Finder "Geschützt", hier nachgestellt), eine
        Rechnung und eine .docx: vorher "1 Rechnung automatisch gebucht, 1 Datei
        nicht übernommen und 2 Dateien ließen sich im Ordner nicht verschieben"
        -- vier fuer zwei Dateien. Jetzt zaehlt jede einmal."""
        import errno

        lieferant(conn)
        lege(postfach, "gesperrt.xml", cii("G-1"))
        lege(postfach, "gesperrt.docx", b"PK\x03\x04")
        lege(postfach, "frei.docx", b"PK\x03\x04")
        echt = os.replace

        def geschuetzt(quelle, ziel):
            if Path(quelle).name.startswith("gesperrt"):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(quelle))
            return echt(quelle, ziel)

        monkeypatch.setattr(abholen.os, "replace", geschuetzt)
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.zusammenfassung() == (
            "Aus dem Eingangsordner: 1 Rechnung automatisch gebucht und 2 Dateien nicht "
            "übernommen; 2 davon ließen sich im Ordner nicht verschieben.")
        nur_gesperrte = abholen.Ergebnis(
            gebucht=ergebnis.gebucht,
            abgewiesen=[a for a in ergebnis.abgewiesen if a.originalname != "frei.docx"],
            nicht_verschoben=ergebnis.nicht_verschoben, icloud_ausstehend=["neu.pdf"])
        assert nur_gesperrte.zusammenfassung() == (
            "Aus dem Eingangsordner: 1 Rechnung automatisch gebucht und 1 Datei nicht "
            "übernommen; beide ließen sich im Ordner nicht verschieben. Noch nicht "
            "abgeholt: 1 Datei lädt noch aus iCloud.")

    def test_ungebucht_im_eingang_heisst_nicht_schon_erfasst(self):
        """Lag die Datei nur ungebucht im Eingang (etwa nach einem Abbruch
        zwischen Aufnehmen und Buchen), sagt die Meldung das -- "schon
        erfasst" las sich wie "gebucht"."""
        ergebnis = abholen.Ergebnis(schon_vorhanden=[
            abholen.SchonVorhanden("a.xml", None, "1.xml"),
            abholen.SchonVorhanden("b.xml", None, "2.xml"),
            abholen.SchonVorhanden("c.xml", 7)])
        assert ergebnis.zusammenfassung() == (
            "Aus dem Eingangsordner: 1 Datei war schon gebucht und 2 Dateien warten schon "
            "in der Prüfmaske.")

    def test_abbruch_nach_dem_aufnehmen(self, conn, datenordner, postfach, monkeypatch):
        """Endet der Prozess nach dem Aufnehmen und vor dem Buchen, liegt die
        Datei ungebucht im Eingang. Der naechste Lauf meldet sie als wartend in
        der Pruefmaske (Entscheidung f: nicht noch einmal aufnehmen), nicht
        als "schon erfasst"."""
        lieferant(conn)
        lege(postfach, "Rechnung.xml", cii())
        monkeypatch.setattr(abholen, "automatisch_buchen",
                            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            hole(conn, datenordner, postfach)
        monkeypatch.undo()
        ergebnis = hole(conn, datenordner, postfach)
        assert ergebnis.zusammenfassung() == (
            "Aus dem Eingangsordner: 1 Datei wartet schon in der Prüfmaske.")
        assert len(pipeline.liegen_geblieben(datenordner)) == 1

    def test_sammelordner_werden_abgewiesen(self, tmp_path):
        """Heimordner, Downloads und iCloud Drive als Ganzes taugen nicht: dort
        liegt anderes, und das Tool verschiebe es."""
        heim = Path.home()
        for ordner in (heim, heim / "Downloads", heim / "Desktop",
                       heim / "Library" / "Mobile Documents" / "com~apple~CloudDocs"):
            assert "allgemeiner Ordner" in abholen.ordnerfehler(tmp_path / "daten", ordner)
        assert abholen.ordnerfehler(tmp_path / "daten", heim / "Downloads" / "Belege") is None

    def test_aufnehmen_hinterlaesst_keine_zwischendatei(self, tmp_path):
        """Die Eingangsdatei entsteht unter einem Zwischennamen und wird dann
        umbenannt -- uebrig bleibt nur sie selbst samt Namensdatei."""
        eingang = pipeline.aufnehmen(cii(), "Rechnung.xml", tmp_path)
        assert sorted(p.name for p in (tmp_path / "eingang").iterdir()) == [
            f".{eingang.pfad.name}.name", eingang.pfad.name]
        assert eingang.pfad.read_bytes() == cii()


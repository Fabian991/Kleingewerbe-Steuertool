"""Kundenimport: Vorlage, Lesen, Planen, Anlegen -- und die gemeinsame Pruefung mit dem Formular.

Die Testdaten sind erfunden (Musterstraße, Beispiel GmbH). Jede Datei wird
im Speicher gebaut: aus der echten Vorlage, wie ein Nutzer sie befuellt.
"""

import csv
import io
import re
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import db
from app.logik import export_excel
from app.logik import kunden as stamm
from app.logik import kundenimport as ki
from app.main import erzeuge_app

#: Spaltenreihenfolge der Vorlage.
FELDER = [s.feld for s in ki.SPALTEN]


def _xlsx(conn, *zeilen: dict, beispiel_lassen: bool = True) -> bytes:
    """Die echte Vorlage, ab Zeile 3 befuellt (Zeile 2 ist die Beispielzeile)."""
    mappe = load_workbook(io.BytesIO(ki.vorlage(conn)))
    blatt = mappe["Kunden"]
    if not beispiel_lassen:
        for nr in range(1, len(FELDER) + 1):
            blatt.cell(row=2, column=nr).value = None
    for i, zeile in enumerate(zeilen, start=3):
        for feld, wert in zeile.items():
            blatt.cell(row=i, column=FELDER.index(feld) + 1, value=wert)
    puffer = io.BytesIO()
    mappe.save(puffer)
    return puffer.getvalue()


def _csv(zeilen: list[list[str]], *, kodierung="utf-8-sig", trenner=";") -> bytes:
    puffer = io.StringIO()
    csv.writer(puffer, delimiter=trenner, lineterminator="\r\n").writerows(zeilen)
    return puffer.getvalue().encode(kodierung)


def _plan(conn, inhalt: bytes, name: str = "kunden.xlsx") -> ki.Plan:
    return ki.plane(conn, ki.lies(name, inhalt, stamm.format_aus_betrieb(conn)))


def _zeile(plan: ki.Plan, nummer: int) -> ki.Planzeile:
    return next(z for z in plan.zeilen if z.nummer == nummer)


def _kunde(conn, **werte) -> int:
    zeile = {"kundennummer": "K-0001", "name": "Müller GmbH", "strasse": "Musterstraße 1",
             "plz": "10115", "ort": "Berlin", "angelegt_am": "2026-01-01"} | werte
    return conn.execute(
        f"INSERT INTO kunde ({', '.join(zeile)}) VALUES ({', '.join('?' * len(zeile))})",
        tuple(zeile.values())).lastrowid


INLAND = {"name": "Beispiel AG", "strasse": "Musterweg 3", "plz": "01067", "ort": "Dresden"}


# ================================================================= Vorlage

class TestVorlage:
    def test_blaetter_ueberschriften_und_formate(self, conn, betrieb):
        mappe = load_workbook(io.BytesIO(ki.vorlage(conn)))
        assert mappe.sheetnames == ["Kunden", "Hilfe", "Listen"]
        assert mappe["Listen"].sheet_state == "hidden"
        blatt = mappe["Kunden"]
        kopf = [z.value for z in blatt[1]]
        assert kopf[:7] == ["Kundennummer", "Name *", "Rechnungsname", "Ansprechpartner",
                            "Straße und Hausnummer *", "PLZ *", "Ort *"]
        assert "Leistungsort" in kopf and "Stundensatz (€)" in kopf and "Notiz" in kopf
        # Kundennummer, PLZ und USt-IdNr. als Text: sonst wird 01067 zu 1067.
        for feld in ("kundennummer", "plz", "ust_idnr"):
            spalte = FELDER.index(feld) + 1
            assert blatt.cell(row=5, column=spalte).number_format == "@", feld
        # Die Beispielzeile ist als solche markiert.
        assert blatt.cell(row=2, column=1).value == "Beispiel"
        assert "nicht angelegt" in blatt.cell(row=2, column=2).value

    def test_auswahllisten(self, conn, betrieb):
        mappe = load_workbook(io.BytesIO(ki.vorlage(conn)))
        listen = mappe["Listen"]
        pruefungen = {str(p.sqref).split(":")[0][:-1]: p
                      for p in mappe["Kunden"].data_validations.dataValidation}
        for feld in ("land", "typ", "leistungsort", "gruppierung", "aktiv"):
            buchstabe = chr(ord("A") + FELDER.index(feld))
            pruefung = pruefungen[buchstabe]
            assert pruefung.type == "list", feld
            spalte = re.search(r"\$([A-Z])\$1", pruefung.formula1).group(1)
            angeboten = [z.value for z in listen[spalte] if z.value]
            assert angeboten == ki.AUSWAHL[feld], feld
        assert pruefungen[chr(ord("A") + FELDER.index("land"))].errorStyle == "warning"
        assert "EU-Unternehmer (Reverse Charge)" in ki.AUSWAHL["leistungsort"]
        assert "Deutschland" in ki.AUSWAHL["land"] and "Österreich" in ki.AUSWAHL["land"]
        assert pruefungen[chr(ord("A") + FELDER.index("zahlungsziel"))].type == "whole"

    def test_hilfeblatt(self, conn, betrieb):
        _kunde(conn, kundennummer="K-0007")
        hilfe = load_workbook(io.BytesIO(ki.vorlage(conn)))["Hilfe"]
        text = " ".join(str(z.value) for zeile in hilfe.iter_rows() for z in zeile if z.value)
        assert "nächste freie Nummer: K-0008" in text
        assert "§ 34a Satz 1 Nr. 1 UStDV" in text and "keine Formeln" not in text.lower() \
            or "Formeln" in text
        for s in ki.SPALTEN:
            assert s.ueberschrift in text

    def test_leere_vorlage_legt_nichts_an(self, conn, betrieb):
        """Nur die Beispielzeile: übersprungen, nichts anzulegen."""
        plan = _plan(conn, ki.vorlage(conn))
        assert [(z.nummer, z.status) for z in plan.zeilen] == [(2, ki.UEBERSPRUNGEN)]
        assert plan.anzulegen() == []


# =========================================================== Rundreise

class TestRundreise:
    def test_vorlage_befuellen_lesen_planen_anlegen(self, conn, betrieb):
        inhalt = _xlsx(conn, {
            "name": "Beispiel AG", "rechnungsname": "Beispiel AG Buchhaltung",
            "ansprechpartner": "Erika Mustermann", "strasse": "Musterweg 3", "plz": "01067",
            "ort": "Dresden", "land": "Deutschland", "email": "post@beispiel.example",
            "typ": "Juristische Person", "leistungsort": "Inland", "satz_euro": 92.5,
            "zahlungsziel": 21, "gruppierung": "pro Tag", "aktiv": "ja",
            "notiz": "erfunden",
        }, {
            "name": "Wien Beispiel GmbH", "strasse": "Ring 2", "plz": "1010", "ort": "Wien",
            "land": "Österreich", "ust_idnr": "atu 123 456 78", "typ": "Unternehmer",
            "leistungsort": "EU-Unternehmer (Reverse Charge)",
        })
        plan = _plan(conn, inhalt)
        assert [z.status for z in plan.zeilen] == [ki.UEBERSPRUNGEN, ki.NEU, ki.NEU]
        ergebnis = ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        assert [n for _, n, _ in ergebnis.angelegt] == ["K-0001", "K-0002"]
        erster = conn.execute("SELECT * FROM kunde WHERE kundennummer = 'K-0001'").fetchone()
        assert (erster["name"], erster["rechnungsname"], erster["ansprechpartner"],
                erster["strasse"], erster["plz"], erster["ort"], erster["land"],
                erster["email"], erster["typ"], erster["leistungsort"], erster["satz_cent"],
                erster["zahlungsziel_tage"], erster["gruppierung_default"], erster["aktiv"],
                erster["notiz"]) == (
            "Beispiel AG", "Beispiel AG Buchhaltung", "Erika Mustermann", "Musterweg 3",
            "01067", "Dresden", "DE", "post@beispiel.example", "JURISTISCHE_PERSON",
            "INLAND", 9250, 21, "PRO_TAG", 1, "erfunden")
        zweiter = conn.execute("SELECT * FROM kunde WHERE kundennummer = 'K-0002'").fetchone()
        assert (zweiter["land"], zweiter["ust_idnr"], zweiter["leistungsort"],
                zweiter["satz_cent"], zweiter["gruppierung_default"]) == (
            "AT", "ATU12345678", "EU_B2B_REVERSE_CHARGE", None, "PRO_TITEL")

    def test_journal_je_kunde_und_einmal_der_import(self, conn, betrieb):
        inhalt = _xlsx(conn, INLAND, {**INLAND, "name": "Zweite AG"})
        plan = _plan(conn, inhalt)
        ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        anlegen = conn.execute("SELECT * FROM journal WHERE aktion = 'ANLEGEN'"
                               " AND objekttyp = 'kunde'").fetchall()
        assert len(anlegen) == 2
        assert anlegen[0]["begruendung"] == "Kundenimport kunden.xlsx, Zeile 3"
        assert '"kundennummer": "K-0001"' in anlegen[0]["wert_neu"]
        importe = conn.execute("SELECT * FROM journal WHERE aktion = 'KUNDENIMPORT'").fetchall()
        assert len(importe) == 1
        assert importe[0]["hash"] == ki.pruefsumme(inhalt)
        assert '"angelegt": 2' in importe[0]["wert_neu"]

    def test_dieselbe_datei_zweimal(self, conn, betrieb):
        """Beim zweiten Mal ist alles schon vorhanden -- und nichts wird verändert."""
        inhalt = _xlsx(conn, INLAND, {**INLAND, "name": "Zweite AG", "kundennummer": "K-0010"})
        plan = _plan(conn, inhalt)
        ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        conn.execute("UPDATE kunde SET ort = 'Leipzig' WHERE kundennummer = 'K-0010'")
        # Ohne Kundennummer erkennt der Import die erste Zeile am Namen -- als
        # namensgleich; mit Nummer als vorhanden.
        zweiter = _plan(conn, inhalt)
        assert _zeile(zweiter, 4).status == ki.VORHANDEN
        assert "K-0010" in _zeile(zweiter, 4).grund
        assert _zeile(zweiter, 3).status == ki.NAMENSGLEICH
        ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=zweiter.fingerabdruck)
        assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 2
        assert conn.execute("SELECT ort FROM kunde WHERE kundennummer = 'K-0010'"
                            ).fetchone()[0] == "Leipzig"

    def test_zweiter_klick_legt_nichts_doppelt_an(self, conn, betrieb):
        inhalt = _xlsx(conn, INLAND)
        plan = _plan(conn, inhalt)
        ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        zweites = ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        assert zweites.schon_erledigt and zweites.angelegt == []
        assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1

    def test_nach_dem_loeschen_wieder_importierbar(self, conn, betrieb):
        """Probeimport, Kunden wieder geloescht: dieselbe Datei legt sie erneut an.

        Die Vorschau ist dann dieselbe wie beim ersten Mal (die Nummern sind
        wieder frei) -- der Journaleintrag des ersten Imports darf sie nicht
        als "schon angelegt" abweisen.
        """
        inhalt = _xlsx(conn, INLAND, {**INLAND, "name": "Zweite AG"})
        plan = _plan(conn, inhalt)
        erstes = ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        for kid, _, _ in erstes.angelegt:
            stamm.loeschen(conn, kid, grund="Probeimport")
        wieder = _plan(conn, inhalt)
        assert wieder.fingerabdruck == plan.fingerabdruck
        assert not ki.schon_angelegt(conn, ki.pruefsumme(inhalt), wieder.fingerabdruck)
        zweites = ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=wieder.fingerabdruck)
        assert not zweites.schon_erledigt
        assert [n for _, n, _ in zweites.angelegt] == ["K-0001", "K-0002"]
        # Und ein dritter Klick auf diese Vorschau legt wieder nichts doppelt an.
        assert ki.lege_an(conn, "kunden.xlsx", inhalt,
                          fingerabdruck=wieder.fingerabdruck).schon_erledigt
        assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 2

    def test_nichts_angelegt_dann_mit_haken(self, conn, betrieb):
        """Ohne Haken legte der Import nichts an; mit Haken auf dieselbe Vorschau legt er an."""
        _kunde(conn, kundennummer="K-0001", name="Beispiel AG")
        inhalt = _xlsx(conn, INLAND)
        plan = _plan(conn, inhalt)
        assert plan.zeilen[1].status == ki.NAMENSGLEICH
        assert ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck
                          ).angelegt == []
        # Derselbe Klick noch einmal: erledigt, kein zweiter Journaleintrag.
        assert ki.lege_an(conn, "kunden.xlsx", inhalt,
                          fingerabdruck=plan.fingerabdruck).schon_erledigt
        mit_haken = ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck,
                               trotzdem={plan.zeilen[1].nummer})
        assert [n for _, n, _ in mit_haken.angelegt] == ["K-0002"]
        assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'KUNDENIMPORT'"
                            ).fetchone()[0] == 2

    def test_veraenderter_bestand_legt_nichts_an(self, conn, betrieb):
        """Zwischen Vorschau und Bestaetigung kam ein Kunde dazu: neue Vorschau statt Import."""
        inhalt = _xlsx(conn, INLAND)
        plan = _plan(conn, inhalt)
        _kunde(conn, kundennummer="K-0001", name="Beispiel AG")
        with pytest.raises(ki.VorschauVeraltet) as veraltet:
            ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        assert veraltet.value.plan.zeilen[1].status == ki.NAMENSGLEICH
        assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1
        assert not conn.in_transaction

    def test_rollback_wenn_das_journal_scheitert(self, conn, betrieb, monkeypatch):
        """Scheitert der dritte Journaleintrag, steht danach kein einziger Kunde da."""
        inhalt = _xlsx(conn, INLAND, {**INLAND, "name": "Zweite AG"},
                       {**INLAND, "name": "Dritte AG"})
        plan = _plan(conn, inhalt)
        echt = db.protokolliere
        aufrufe = {"n": 0}

        def scheitert(*args, **kwargs):
            aufrufe["n"] += 1
            if aufrufe["n"] == 3:
                raise sqlite3.OperationalError("Platte voll")
            return echt(*args, **kwargs)

        monkeypatch.setattr(db, "protokolliere", scheitert)
        with pytest.raises(sqlite3.OperationalError):
            ki.lege_an(conn, "kunden.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'ANLEGEN'"
                            ).fetchone()[0] == 0

    def test_excel_export_laesst_sich_wieder_einlesen(self, conn, betrieb, tmp_path):
        """Das Blatt „Kunden“ des eigenen Exports in eine leere Datenbank."""
        _kunde(conn, kundennummer="K-0001", name="Beispiel GmbH", plz="01067", ort="Dresden",
               satz_cent=9050, gruppierung_default="PRO_TAG")
        _kunde(conn, kundennummer="K-0002", name="Wien Beispiel GmbH", plz="1010", ort="Wien",
               land="AT", ust_idnr="ATU12345678", leistungsort="EU_B2B_REVERSE_CHARGE",
               typ="JURISTISCHE_PERSON", aktiv=0)
        export = export_excel.als_bytes(conn)
        mappe = load_workbook(io.BytesIO(export))
        nur_kunden = Workbook()
        ziel = nur_kunden.active
        ziel.title = "Kunden"
        for zeile in mappe["Kunden"].iter_rows(values_only=True):
            ziel.append(list(zeile))
        puffer = io.BytesIO()
        nur_kunden.save(puffer)

        leer = db.oeffne(tmp_path / "leer.sqlite")
        try:
            leer.execute("INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer)"
                         " VALUES (1, 'B', 'W 1', '20095', 'Hamburg', '12/345/67890')")
            plan = _plan(leer, puffer.getvalue())
            assert [z.status for z in plan.zeilen] == [ki.NEU, ki.NEU], \
                [(z.fehler, z.grund) for z in plan.zeilen]
            assert any("Angelegt am" in h for h in plan.hinweise)
            ki.lege_an(leer, "export.xlsx", puffer.getvalue(), fingerabdruck=plan.fingerabdruck)
            spalten = ("kundennummer", "name", "plz", "land", "ust_idnr", "leistungsort", "typ",
                       "satz_cent", "gruppierung_default", "aktiv")
            alt = [tuple(z[s] for s in spalten) for z in conn.execute(
                "SELECT * FROM kunde ORDER BY kundennummer")]
            neu = [tuple(z[s] for s in spalten) for z in leer.execute(
                "SELECT * FROM kunde ORDER BY kundennummer")]
            assert neu == alt
        finally:
            leer.close()


# ======================================================== Zahlzellen

class TestZahlzellen:
    def test_geld_ueber_decimal(self, conn, betrieb):
        """0,29 € als Zahl sind 29 Cent (nicht 28); 1,234 € sind 1,23 € (nicht 1.234 €)."""
        inhalt = _xlsx(conn, {**INLAND, "satz_euro": 0.29},
                       {**INLAND, "name": "B", "satz_euro": 1.234},
                       {**INLAND, "name": "C", "satz_euro": "85,50"},
                       {**INLAND, "name": "D", "satz_euro": "1.234"})
        plan = _plan(conn, inhalt)
        assert [z.werte.get("satz_cent") for z in plan.zeilen[1:]] == [29, 123, 8550, 123400]

    def test_tage_als_zahl(self, conn, betrieb):
        inhalt = _xlsx(conn, {**INLAND, "zahlungsziel": 14.0},
                       {**INLAND, "name": "B", "zahlungsziel": 14.5})
        plan = _plan(conn, inhalt)
        assert _zeile(plan, 3).werte["zahlungsziel_tage"] == 14
        assert _zeile(plan, 4).status == ki.FEHLER
        assert "keine Anzahl Tage" in _zeile(plan, 4).fehler[0][1]

    def test_plz_als_zahl(self, conn, betrieb):
        """Excel macht aus 01067 die Zahl 1067: Fehler mit Vorschlag, bei AT richtig."""
        inhalt = _xlsx(conn, {**INLAND, "plz": 1067},
                       {**INLAND, "name": "Wien", "plz": 5020, "land": "AT", "typ": "Privatperson"})
        plan = _plan(conn, inhalt)
        spalte, grund = _zeile(plan, 3).fehler[0]
        assert spalte == "PLZ (F3)" and "01067" in grund
        assert _zeile(plan, 4).status == ki.NEU and _zeile(plan, 4).werte["plz"] == "5020"

    def test_kundennummer_als_zahl(self, conn, betrieb):
        """Format {:03d}: die Zahl 7 ist 007."""
        conn.execute("UPDATE betrieb SET kundennummer_format = '{:03d}' WHERE id = 1")
        plan = _plan(conn, _xlsx(conn, {**INLAND, "kundennummer": 7}))
        assert _zeile(plan, 3).kundennummer == "007" and _zeile(plan, 3).nummer_angegeben


class TestUnlesbareZellen:
    def test_formel_und_datum(self, conn, betrieb):
        mappe = load_workbook(io.BytesIO(_xlsx(conn, INLAND)))
        blatt = mappe["Kunden"]
        blatt.cell(row=3, column=FELDER.index("name") + 1, value="=A1&B1")
        blatt.cell(row=3, column=FELDER.index("ort") + 1).value = __import__(
            "datetime").date(2026, 1, 1)
        puffer = io.BytesIO()
        mappe.save(puffer)
        fehler = dict(_zeile(_plan(conn, puffer.getvalue()), 3).fehler)
        assert "Formel" in fehler["Name (B3)"]
        assert "Datum" in fehler["Ort (G3)"]

    def test_steuerzeichen_in_csv(self, conn, betrieb):
        inhalt = _csv([["Name", "Straße", "PLZ", "Ort"],
                       ["Beispiel\x00 AG", "Musterweg 3", "01067", "Dresden"]])
        zeile = _zeile(_plan(conn, inhalt, "kunden.csv"), 2)
        assert zeile.status == ki.FEHLER
        assert "Steuerzeichen" in zeile.fehler[0][1]

    def test_zu_lange_zelle(self, conn, betrieb):
        zeile = _zeile(_plan(conn, _xlsx(conn, {**INLAND, "name": "x" * 201})), 3)
        assert "zu lang (201 Zeichen, höchstens 200)" in zeile.fehler[0][1]


class TestCsv:
    def test_windows_1252_mit_semikolon(self, conn, betrieb):
        inhalt = _csv([["Name", "Straße und Hausnummer", "PLZ", "Ort", "Leistungsort"],
                       ["Müller & Söhne", "Musterstraße 1", "01067", "Dresden", "Inland"]],
                      kodierung="cp1252")
        plan = _plan(conn, inhalt, "kunden.csv")
        assert _zeile(plan, 2).werte["name"] == "Müller & Söhne"
        assert any("Windows-1252" in h and "„;“" in h for h in plan.hinweise)

    def test_utf8_mit_komma(self, conn, betrieb):
        inhalt = _csv([["name", "strasse", "plz", "ort", "stundensatz"],
                       ["Beispiel AG", "Musterweg 3", "01067", "Dresden", "85,50"]],
                      trenner=",")
        plan = _plan(conn, inhalt, "kunden.csv")
        assert _zeile(plan, 2).werte["satz_cent"] == 8550

    def test_fehlende_pflichtspalte(self, conn, betrieb):
        with pytest.raises(ki.ImportFehler, match="Es fehlen Spalten für die Pflichtangaben: PLZ"):
            ki.lies("k.csv", _csv([["Name", "Straße", "Ort"], ["A", "B", "C"]]), "K-{:04d}")


# ========================================================= Abgleich

class TestAbgleich:
    def test_vorhanden_ueber_nummer_in_anderer_schreibweise(self, conn, betrieb):
        _kunde(conn, kundennummer="K-0001")
        zeile = _zeile(_plan(conn, _xlsx(conn, {**INLAND, "kundennummer": "K-1"})), 3)
        assert zeile.status == ki.VORHANDEN
        assert "K-0001 Müller GmbH (gleiche Kundennummer)" in zeile.grund

    def test_vorhanden_ueber_ust_idnr(self, conn, betrieb):
        _kunde(conn, kundennummer="K-0001", land="AT", ust_idnr="ATU12345678")
        zeile = _zeile(_plan(conn, _xlsx(conn, {**INLAND, "ust_idnr": "atu 123.456-78"})), 3)
        assert zeile.status == ki.VORHANDEN and "gleiche USt-IdNr." in zeile.grund

    def test_namensgleich_ohne_ruecksicht_auf_schreibung(self, conn, betrieb):
        _kunde(conn, kundennummer="K-0001", name="Müller GmbH")
        inhalt = _xlsx(conn, {**INLAND, "name": "MÜLLER  GMBH"})
        plan = _plan(conn, inhalt)
        assert _zeile(plan, 3).status == ki.NAMENSGLEICH
        assert "K-0001 (Berlin)" in _zeile(plan, 3).grund
        # Ohne Haken wird sie nicht angelegt, mit Haken schon.
        ergebnis = ki.lege_an(conn, "k.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        assert ergebnis.angelegt == [] and ergebnis.uebersprungen == 2
        inhalt2 = _xlsx(conn, {**INLAND, "name": "MÜLLER  GMBH", "notiz": "zweite Filiale"})
        plan2 = _plan(conn, inhalt2)
        ergebnis = ki.lege_an(conn, "k.xlsx", inhalt2, fingerabdruck=plan2.fingerabdruck,
                              trotzdem={3})
        assert [n for _, n, _ in ergebnis.angelegt] == ["K-0002"]

    def test_doppelt_in_der_datei(self, conn, betrieb):
        plan = _plan(conn, _xlsx(
            conn,
            {**INLAND, "kundennummer": "K-0005"},
            {**INLAND, "name": "Andere AG", "kundennummer": "K-5"},
            {**INLAND, "name": "Dritte AG", "ust_idnr": "DE123456789"},
            {**INLAND, "name": "Vierte AG", "ust_idnr": "DE 123 456 789"},
            {**INLAND, "name": "Beispiel AG"},
        ))
        assert _zeile(plan, 4).status == ki.FEHLER
        assert "dieselbe Nummer steht schon in Zeile 3" in _zeile(plan, 4).fehler[0][1]
        assert _zeile(plan, 6).status == ki.FEHLER
        assert "dieselbe USt-IdNr. steht schon in Zeile 5" in _zeile(plan, 6).fehler[0][1]
        assert _zeile(plan, 7).status == ki.NAMENSGLEICH
        assert _zeile(plan, 7).grund == "Gleicher Name wie Zeile 3."

    @pytest.mark.parametrize(("zeile", "spalte", "text"), [
        ({"land": "AT", "plz": "1010", "leistungsort": "EU-Unternehmer (Reverse Charge)"},
         "USt-IdNr.", "Pflicht"),
        ({"land": "AT", "plz": "1010", "typ": "Privatperson",
          "leistungsort": "EU-Unternehmer (Reverse Charge)", "ust_idnr": "ATU12345678"},
         "Leistungsort", "Privatperson"),
        ({"land": "FR", "plz": "75001", "leistungsort": "Inland"}, "Leistungsort", "Frankreich"),
        ({"land": "CH", "plz": "8001"}, "Leistungsort", "fehlt; leer hieße Inland"),
    ])
    def test_querpruefungen_sind_fehler(self, conn, betrieb, zeile, spalte, text):
        """Was beim Festschreiben ROT wuerde, legt der Import gar nicht erst an."""
        p = _zeile(_plan(conn, _xlsx(conn, {**INLAND, **zeile})), 3)
        assert p.status == ki.FEHLER
        assert any(s.startswith(spalte) and text in g for s, g in p.fehler), p.fehler

    def test_unvollstaendige_anschrift(self, conn, betrieb):
        p = _zeile(_plan(conn, _xlsx(conn, {"name": "Nur Name"})), 3)
        assert p.status == ki.FEHLER
        assert [s for s, _ in p.fehler] == ["Straße und Hausnummer (E3)", "PLZ (F3)", "Ort (G3)"]
        assert all("Pflicht nach § 34a" in g for _, g in p.fehler)

    def test_beispielzeile_wird_uebersprungen(self, conn, betrieb):
        plan = _plan(conn, _xlsx(conn, {**INLAND, "kundennummer": "beispiel"}))
        assert [z.status for z in plan.zeilen] == [ki.UEBERSPRUNGEN, ki.UEBERSPRUNGEN]


class TestNummernvergabe:
    def test_vorschau_gleich_ergebnis(self, conn, betrieb):
        _kunde(conn, kundennummer="K-0003")
        inhalt = _xlsx(conn, INLAND, {**INLAND, "name": "Zweite AG"})
        plan = _plan(conn, inhalt)
        vorschau = [z.kundennummer for z in plan.anzulegen()]
        ergebnis = ki.lege_an(conn, "k.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        assert vorschau == [n for _, n, _ in ergebnis.angelegt] == ["K-0004", "K-0005"]

    def test_angegebene_nummer_vor_automatischer(self, conn, betrieb):
        plan = _plan(conn, _xlsx(conn, INLAND, {**INLAND, "name": "Alt", "kundennummer": "K-20"}))
        assert [z.kundennummer for z in plan.anzulegen()] == ["K-0021", "K-0020"]
        assert any("hebt den Zähler" in h and "K-0021" in h for h in plan.hinweise)

    def test_startwert(self, conn, betrieb):
        conn.execute("UPDATE betrieb SET kundennummer_start = 16 WHERE id = 1")
        plan = _plan(conn, _xlsx(conn, INLAND))
        assert plan.anzulegen()[0].kundennummer == "K-0016"

    def test_namensgleiche_verschieben_die_neuen_nicht(self, conn, betrieb):
        """Erst die neuen, dann die namensgleichen: ein abgewaehlter Haken aendert keine Nummer."""
        _kunde(conn, kundennummer="K-0001", name="Müller GmbH")
        inhalt = _xlsx(conn, {**INLAND, "name": "Müller GmbH"}, INLAND)
        plan = _plan(conn, inhalt)
        assert (_zeile(plan, 3).kundennummer, _zeile(plan, 4).kundennummer) == (
            "K-0003", "K-0002")
        ergebnis = ki.lege_an(conn, "k.xlsx", inhalt, fingerabdruck=plan.fingerabdruck)
        assert [n for _, n, _ in ergebnis.angelegt] == ["K-0002"]


# ============================================================ Dateien

def _billion_laughs(conn) -> bytes:
    """Eine gueltige Vorlage, deren Blatt "Kunden" eine Entitaetenbombe traegt.

    Ausgeschrieben waeren es eine Milliarde "lol" -- rund 3 GB im Speicher.
    """
    quelle = zipfile.ZipFile(io.BytesIO(_xlsx(conn, INLAND)))
    entitaeten = "".join(
        f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">'
        for i in range(1, 10))
    bombe = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">' + entitaeten + ']>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData><row r="1"><c r="B1" t="inlineStr"><is><t>Name</t></is></c></row>'
             '<row r="2"><c r="B2" t="inlineStr"><is><t>&lol9;</t></is></c></row>'
             '</sheetData></worksheet>')
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as ziel:
        for teil in quelle.infolist():
            daten = bombe.encode() if teil.filename == "xl/worksheets/sheet1.xml" \
                else quelle.read(teil.filename)
            ziel.writestr(teil.filename, daten)
    return puffer.getvalue()


def _zip(teile: dict[str, bytes]) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as ziel:
        for name, daten in teile.items():
            ziel.writestr(name, daten)
    return puffer.getvalue()


class TestDateien:
    @pytest.mark.parametrize(("name", "inhalt", "text"), [
        ("kunden.xlsm", b"PK\x03\x04", "Makros (.xlsm)"),
        ("kunden.xls", b"\xd0\xcf\x11\xe0", "alte Excel-Format"),
        ("kunden.ods", b"PK\x03\x04", "Excel-Datei (.xlsx) oder eine CSV-Datei"),
        ("kunden.xlsx", b"", "leer"),
        ("kunden.xlsx", b"\xd0\xcf\x11\xe0" + b"\0" * 100, "Kennwort"),
        ("kunden.xlsx", b"Name;Ort\n", "keine Excel-Arbeitsmappe"),
        ("kunden.csv", b"PK\x03\x04" + b"\0" * 100, "keine Textdatei"),
        ("kunden.csv", b"x" * (ki.MAX_BYTES + 1), "zu groß"),
    ])
    def test_abgewiesen_mit_deutscher_meldung(self, name, inhalt, text):
        with pytest.raises(ki.ImportFehler) as fehler:
            ki.lies(name, inhalt, "K-{:04d}")
        assert text in str(fehler.value)

    def test_zelle_ganz_unten_kostet_keine_million_zeilen(self, conn, betrieb):
        """Eine einzige Zelle in der letzten Zeile (1.048.576): openpyxl liefert
        im read_only-Modus jede Zeile davor als leere Zeile, und leere Zeilen
        zaehlen nicht gegen MAX_ZEILEN -- das Lesen dauerte gut 15 Sekunden.
        Jetzt endet es an MAX_TABELLENZEILEN mit einer Meldung."""
        import time

        mappe = load_workbook(io.BytesIO(_xlsx(conn, INLAND)))
        mappe["Kunden"].cell(row=1048576, column=FELDER.index("name") + 1,
                             value="Weit Unten GmbH")
        puffer = io.BytesIO()
        mappe.save(puffer)
        beginn = time.monotonic()
        with pytest.raises(ki.ImportFehler, match="reicht über Zeile 3.000 hinaus"):
            ki.lies("kunden.xlsx", puffer.getvalue(), "K-{:04d}")
        assert time.monotonic() - beginn < 3

    def test_leerzeilen_zwischen_den_kunden_gehen(self, conn, betrieb):
        """Unterhalb der Grenze stoeren Leerzeilen nicht."""
        mappe = load_workbook(io.BytesIO(_xlsx(conn, INLAND)))
        for feld, wert in {**INLAND, "name": "Weiter Unten AG"}.items():
            mappe["Kunden"].cell(row=ki.MAX_TABELLENZEILEN, column=FELDER.index(feld) + 1,
                                 value=wert)
        puffer = io.BytesIO()
        mappe.save(puffer)
        gelesen = ki.lies("kunden.xlsx", puffer.getvalue(), "K-{:04d}")
        assert [z.felder["name"] for z in gelesen.zeilen][-2:] == ["Beispiel AG",
                                                                   "Weiter Unten AG"]

    def test_csv_mit_endlosen_leerzeilen(self):
        inhalt = ("Name;Straße;PLZ;Ort\r\n" + "\r\n" * (ki.MAX_TABELLENZEILEN + 5)
                  + "Weit Unten GmbH;Musterweg 1;01067;Dresden\r\n").encode()
        with pytest.raises(ki.ImportFehler, match="reicht über Zeile"):
            ki.lies("kunden.csv", inhalt, "K-{:04d}")

    def test_umbenannte_makrodatei(self, conn, betrieb):
        """Eine .xlsm, die nur .xlsx heisst: am vbaProject.bin erkannt."""
        quelle = zipfile.ZipFile(io.BytesIO(_xlsx(conn, INLAND)))
        teile = {t.filename: quelle.read(t.filename) for t in quelle.infolist()}
        teile["xl/vbaProject.bin"] = b"\0" * 64
        with pytest.raises(ki.ImportFehler, match="enthält Makros"):
            ki.lies("kunden.xlsx", _zip(teile), "K-{:04d}")
        teile.pop("xl/vbaProject.bin")
        teile["[Content_Types].xml"] = teile["[Content_Types].xml"].replace(
            b"sheet.main+xml", b"sheet.macroEnabled.main+xml")
        with pytest.raises(ki.ImportFehler, match="Makros"):
            ki.lies("kunden.xlsx", _zip(teile), "K-{:04d}")

    def test_zip_bombe(self):
        """30 MB Nullen, gepackt ein paar Kilobyte: abgewiesen, bevor entpackt wird."""
        inhalt = _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<w/>",
                       "xl/worksheets/sheet1.xml": b"0" * 30_000_000})
        assert len(inhalt) < ki.MAX_BYTES
        with pytest.raises(ki.ImportFehler, match="entpackt größer als 20 MB"):
            ki.lies("kunden.xlsx", inhalt, "K-{:04d}")

    def test_stark_gepackter_teil(self):
        inhalt = _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<w/>",
                       "xl/worksheets/sheet1.xml": b"0" * 5_000_000})
        with pytest.raises(ki.ImportFehler, match="auffällig stark gepackt"):
            ki.lies("kunden.xlsx", inhalt, "K-{:04d}")

    def test_beschaedigte_datei(self, conn, betrieb):
        inhalt = bytearray(_xlsx(conn, INLAND))
        mitte = len(inhalt) // 2
        inhalt[mitte:mitte + 200] = b"\xff" * 200
        with pytest.raises(ki.ImportFehler) as fehler:
            ki.lies("kunden.xlsx", bytes(inhalt), "K-{:04d}")
        meldung = str(fehler.value)
        assert "Excel" in meldung and "/" not in meldung and "Error" not in meldung

    def test_billion_laughs(self, conn, betrieb):
        with pytest.raises(ki.ImportFehler) as fehler:
            ki.lies("kunden.xlsx", _billion_laughs(conn), "K-{:04d}")
        meldung = str(fehler.value)
        assert meldung.startswith("Die Excel-Datei ließ sich nicht")
        assert "/" not in meldung and "ntit" not in meldung

    def test_mehr_als_1000_zeilen(self, conn, betrieb):
        zeilen = [["Name", "Straße", "PLZ", "Ort"]] + [
            [f"Kunde {i}", "Musterweg 1", "01067", "Dresden"] for i in range(ki.MAX_ZEILEN + 1)]
        with pytest.raises(ki.ImportFehler, match="mehr als 1000 Kundenzeilen"):
            ki.lies("kunden.csv", _csv(zeilen), "K-{:04d}")

    def test_doppelte_ueberschrift(self):
        with pytest.raises(ki.ImportFehler, match="meinen beide"):
            ki.lies("k.csv", _csv([["Name", "Firma", "Straße", "PLZ", "Ort"]]), "K-{:04d}")


# ========================================================== Oberflaeche

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
def datenbank(tmp_path):
    def oeffnen():
        conn = sqlite3.connect(tmp_path / "web.sqlite")
        conn.row_factory = sqlite3.Row
        return conn
    return oeffnen


def _vorlage_befuellen(klient, *zeilen: dict) -> bytes:
    mappe = load_workbook(io.BytesIO(klient.get("/kunden/import/vorlage.xlsx").content))
    for i, zeile in enumerate(zeilen, start=3):
        for feld, wert in zeile.items():
            mappe["Kunden"].cell(row=i, column=FELDER.index(feld) + 1, value=wert)
    puffer = io.BytesIO()
    mappe.save(puffer)
    return puffer.getvalue()


def _hochladen(klient, inhalt: bytes, name: str = "kunden.xlsx", **kopf):
    return klient.post("/kunden/import",
                       files={"datei": (name, inhalt, "application/octet-stream")},
                       headers=kopf)


def _formular(html: str) -> dict:
    return {
        "schluessel": re.search(r'name="schluessel" value="([^"]+)"', html).group(1),
        "fingerabdruck": re.search(r'name="fingerabdruck" value="([^"]+)"', html).group(1),
    }


class TestOberflaeche:
    def test_lesen_und_abgleichen_halten_den_server_nicht_an(self, klient, monkeypatch):
        """Die Route ist ``async``; was sie direkt tut, laeuft in der
        Ereignisschleife und haelt jede andere Anfrage an. Lesen und Abgleichen
        laufen deshalb im Threadpool -- dort gibt es keine laufende Schleife."""
        import asyncio

        in_der_schleife: list[bool] = []
        echt_lies, echt_plane = ki.lies, ki.plane

        def merke() -> None:
            try:
                asyncio.get_running_loop()
                in_der_schleife.append(True)
            except RuntimeError:
                in_der_schleife.append(False)

        def lies(*argumente):
            merke()
            return echt_lies(*argumente)

        def plane(*argumente):
            merke()
            return echt_plane(*argumente)

        monkeypatch.setattr(ki, "lies", lies)
        monkeypatch.setattr(ki, "plane", plane)
        antwort = _hochladen(klient, _vorlage_befuellen(klient, INLAND))
        assert antwort.status_code == 200
        assert in_der_schleife == [False, False]

    def test_seite_und_einstieg(self, klient):
        """/kunden/import steht vor /{kunde_id}: vorher kam hier eine 422."""
        antwort = klient.get("/kunden/import")
        assert antwort.status_code == 200
        assert 'enctype="multipart/form-data"' in antwort.text
        assert 'href="/kunden/import/vorlage.xlsx"' in antwort.text
        assert 'href="/kunden/import">Kunden importieren</a>' in klient.get("/kunden").text

    def test_vorlage_zum_herunterladen(self, klient):
        antwort = klient.get("/kunden/import/vorlage.xlsx")
        assert antwort.status_code == 200
        assert antwort.headers["content-disposition"].startswith("attachment;")
        assert "Kundenvorlage.xlsx" in antwort.headers["content-disposition"]
        assert load_workbook(io.BytesIO(antwort.content)).sheetnames[0] == "Kunden"

    def test_vorschau_dann_anlegen(self, klient, datenbank):
        klient.post("/kunden", data=KUNDE)
        inhalt = _vorlage_befuellen(
            klient,
            {**INLAND, "satz_euro": 85.5},
            {**INLAND, "name": "Mustermann GmbH"},
            {"name": "Ohne Anschrift"},
            {**INLAND, "name": "Wien <Beispiel>", "land": "AT", "plz": "1010",
             "ust_idnr": "ATU12345678", "leistungsort": "EU-Unternehmer (Reverse Charge)"},
        )
        vorschau = _hochladen(klient, inhalt)
        assert vorschau.status_code == 200
        text = vorschau.text
        for erwartet in ("wird angelegt", "gleicher Name", "trotzdem anlegen", "fehlerhaft",
                         "übersprungen", "Gleicher Name wie K-0001 (Berlin)", "85,50 €",
                         "Wien &lt;Beispiel&gt;", "2 Kunden anlegen"):
            assert erwartet in text, erwartet
        # Noch nichts angelegt.
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1
        antwort = klient.post("/kunden/import/anlegen", data={**_formular(text), "trotzdem": "4"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "/kunden?erfolg=" in antwort.headers["location"]
        liste = klient.get(antwort.headers["location"]).text
        assert "3 Kunden angelegt: K-0002, K-0003, K-0004." in liste
        assert "1 übersprungen; 1 fehlerhaft, nicht angelegt." in liste
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 4

    def test_zweiter_klick(self, klient, datenbank):
        inhalt = _vorlage_befuellen(klient, INLAND)
        text = _hochladen(klient, inhalt).text
        erster = klient.post("/kunden/import/anlegen", data=_formular(text),
                             follow_redirects=False)
        zweiter = klient.post("/kunden/import/anlegen", data=_formular(text),
                              follow_redirects=False)
        assert erster.status_code == zweiter.status_code == 303
        assert "bereits%20angelegt" in zweiter.headers["location"]
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1
        # Dieselbe Datei noch einmal gewaehlt: die Vorschau sagt, dass sie schon lief.
        wieder = _hochladen(klient, inhalt).text
        assert "Diese Datei wurde am" in wieder
        assert "trotzdem anlegen" in wieder

    def test_veraenderte_vorschau_wird_nicht_angelegt(self, klient, datenbank):
        """Fingerabdruck aus einem veraenderten Formular: nichts angelegt, neue Vorschau."""
        text = _hochladen(klient, _vorlage_befuellen(klient, INLAND)).text
        formular = _formular(text)
        klient.post("/kunden", data={**KUNDE, "name": "Beispiel AG"})
        antwort = klient.post("/kunden/import/anlegen", data=formular)
        assert antwort.status_code == 409
        assert "hat sich seit der Vorschau geändert" in antwort.text
        assert "gleicher Name" in antwort.text
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 1

    def test_abgelaufene_vorschau(self, klient):
        antwort = klient.post("/kunden/import/anlegen",
                              data={"schluessel": "a" * 64, "fingerabdruck": "b" * 64})
        assert "Vorschau ist abgelaufen" in antwort.text
        antwort = klient.post("/kunden/import/anlegen", data={"schluessel": "../x"})
        assert "Vorschau ist unvollständig" in antwort.text

    def test_fehlerhafte_datei(self, klient):
        antwort = _hochladen(klient, b"\x00\x01", "kunden.xlsm")
        assert antwort.status_code == 422
        assert "Makros (.xlsm)" in antwort.text
        antwort = klient.post("/kunden/import", files={})
        assert antwort.status_code == 422 and "Bitte eine Datei wählen" in antwort.text

    def test_zu_gross_schon_am_kopf(self, klient):
        antwort = _hochladen(klient, b"x" * (ki.MAX_BYTES + 100_000), "kunden.csv")
        assert antwort.status_code == 413
        assert "zu groß" in antwort.text

    def test_fremde_herkunft(self, klient, datenbank):
        """Eine fremde Webseite kann weder hochladen noch anlegen."""
        inhalt = _vorlage_befuellen(klient, INLAND)
        fremd = {"origin": "https://angreifer.example"}
        assert _hochladen(klient, inhalt, **fremd).status_code == 403
        text = _hochladen(klient, inhalt).text
        antwort = klient.post("/kunden/import/anlegen", data=_formular(text), headers=fremd)
        assert antwort.status_code == 403
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM kunde").fetchone()[0] == 0

    def test_kundenseite_unveraendert(self, klient):
        kid = int(klient.post("/kunden", data=KUNDE, follow_redirects=False)
                  .headers["location"].rsplit("/", 1)[1])
        assert "Mustermann GmbH" in klient.get(f"/kunden/{kid}").text

    def test_formular_weist_k1_neben_k0001_ab(self, klient):
        klient.post("/kunden", data=KUNDE)
        antwort = klient.post("/kunden", data={**KUNDE, "name": "Zweiter",
                                               "kundennummer": "K-1"})
        assert antwort.status_code == 422
        assert "„K-1“ ist dieselbe Nummer wie K-0001" in antwort.text

    def test_formular_zeigt_was_beim_festschreiben_rot_wuerde(self, klient):
        """Reverse Charge ohne USt-IdNr.: angelegt, aber mit Hinweis auf der Kundenseite."""
        antwort = klient.post("/kunden", data={**KUNDE, "land": "AT", "plz": "1010",
                                               "leistungsort": "EU_B2B_REVERSE_CHARGE"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        seite = klient.get(antwort.headers["location"]).text
        assert "Bei Reverse Charge ist die USt-IdNr. des Kunden Pflicht" in seite

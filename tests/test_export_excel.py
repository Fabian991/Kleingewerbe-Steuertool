"""Der Notausgang: alle Daten lesbar, ohne dieses Werkzeug.

Geprueft wird nicht, dass eine Datei entsteht -- das waere die halbe Miete --,
sondern dass man sie ohne Erklaerung lesen kann: Betraege als Euro, Datumswerte
als Datum, Fremdschluessel im Klartext daneben, und vor allem: dass keine
Tabelle stillschweigend fehlt.
"""

import datetime as _dt

import pytest
from openpyxl import load_workbook

from app import db
from app.logik import export_excel as ex
from app.logik import festschreiben as f


@pytest.fixture
def bestand(conn, betrieb, kunde_id, tmp_path):
    """Ein kleiner, aber echter Bestand: Zeiten, Rechnung, Zahlung.

    Die Rechnung wird festgeschrieben, weil eine Zahlung sonst gar nicht
    erfasst werden darf -- das sichert ein Trigger in schema.sql ab.
    """
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01',"
        " iban = 'DE02120300000000202051' WHERE id = 1"
    )
    conn.execute("UPDATE kunde SET satz_cent = 9000 WHERE id = ?", (kunde_id,))
    ids = [
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, erfasst_am) VALUES (?, 'Beratung', ?, 60, 'STUNDEN', ?)",
            (kunde_id, f"2026-03-{tag:02d}", db.jetzt_utc()),
        ).lastrowid
        for tag in (2, 3)
    ]
    rid = f.entwurf_anlegen(conn, kunde_id, ids)
    f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
    conn.execute(
        "INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art)"
        " VALUES (?, '2026-04-01', 17850, 'UEBERWEISUNG')",
        (rid,),
    )
    return rid


@pytest.fixture
def mappe(conn, bestand, tmp_path):
    ex.schreibe(conn, tmp_path / "daten.xlsx")
    return load_workbook(tmp_path / "daten.xlsx")


class TestVollstaendigkeit:
    def test_jede_tabelle_ist_entschieden(self, conn):
        """Eine neue Tabelle darf nicht stillschweigend aus der Sicherung fallen.

        Deshalb muss jede Tabelle entweder in BLATTFOLGE stehen oder in
        NICHT_EXPORTIERT -- mit Begruendung. Die Automatik in _tabellen() faengt
        eine vergessene zwar auf und haengt sie hinten an; dieser Test sorgt
        dafuer, dass das eine BEWUSSTE Entscheidung bleibt und nicht der
        Normalfall wird.
        """
        vorhanden = {
            z[0] for z in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        }
        unentschieden = vorhanden - set(ex.BLATTFOLGE) - set(ex.NICHT_EXPORTIERT)
        assert unentschieden == set(), (
            f"Nicht entschieden: {sorted(unentschieden)}. Entweder in BLATTFOLGE "
            "aufnehmen oder in NICHT_EXPORTIERT mit Grund eintragen."
        )

    def test_jedes_blatt_hat_einen_deutschen_namen(self, conn):
        for tabelle in ex.BLATTFOLGE:
            assert tabelle in ex.BLATTNAMEN, tabelle
            assert len(ex.BLATTNAMEN[tabelle]) <= 31, "Excel erlaubt 31 Zeichen"

    def test_ausschluesse_sind_begruendet(self):
        for tabelle, grund in ex.NICHT_EXPORTIERT.items():
            assert len(grund) > 30, f"{tabelle}: eine Begruendung, kein Stichwort"

    def test_alle_blaetter_sind_da(self, mappe):
        erwartet = {ex.BLATTNAMEN[t] for t in ex.BLATTFOLGE}
        assert erwartet <= set(mappe.sheetnames)
        assert mappe.sheetnames[0] == "Lies mich", "Das Deckblatt steht vorn"
        assert mappe.sheetnames[1:3] == ["EÜR", "Buchungen"], (
            "Die Auswertung steht vor den Rohtabellen — danach sucht man zuerst"
        )


class TestEuerBlaetter:
    """Die übrigen Blätter geben die Tabellen wieder, diese beiden die Antwort."""

    def _zeilen(self, blatt):
        kopf = [z.value for z in blatt[1]]
        return kopf, [
            dict(zip(kopf, [z.value for z in zeile], strict=True))
            for zeile in blatt.iter_rows(min_row=2)
        ]

    def test_euer_blatt_traegt_kennzahl_und_eurobetrag(self, mappe):
        kopf, zeilen = self._zeilen(mappe["EÜR"])
        assert kopf == ["Jahr", "Bereich", "Kennzahl", "Bezeichnung", "Betrag", "Hinweis"]
        einnahme = next(z for z in zeilen if z["Bereich"] == "Betriebseinnahmen")
        assert einnahme["Kennzahl"] == 111
        assert einnahme["Betrag"] == 178.50, "Euro, nicht Cent"

    def test_gewinnzeile_ist_da(self, mappe):
        _, zeilen = self._zeilen(mappe["EÜR"])
        gewinn = [z for z in zeilen if z["Bereich"] == "Gewinn"]
        assert len(gewinn) == 1
        assert gewinn[0]["Betrag"] == 178.50

    def test_buchungen_tragen_beleg_und_kennzahl(self, mappe):
        kopf, zeilen = self._zeilen(mappe["Buchungen"])
        assert "Sachkonto" in kopf
        assert "davon Betriebsausgabe" in kopf
        assert len(zeilen) == 1
        assert zeilen[0]["Richtung"] == "Einnahme"
        assert zeilen[0]["Beleg"] == "RE-2026-0001"
        assert zeilen[0]["Gegenstelle"] == "Mustermann GmbH"
        assert zeilen[0]["EÜR-Kennzahl"] == 111

    def test_blaetter_entstehen_auch_ohne_zahlung(self, conn, betrieb, tmp_path):
        """Das Deckblatt nennt beide Blätter — dann müssen sie auch da sein.

        Ein versprochenes Blatt, das fehlt, lässt den Leser suchen. Eine Zeile,
        die den Grund nennt, beantwortet die Frage sofort: die EÜR rechnet nach
        Zu- und Abfluss, und ohne Zahlung gibt es nichts zu zeigen.
        """
        ex.schreibe(conn, tmp_path / "leer.xlsx")
        leer = load_workbook(tmp_path / "leer.xlsx")
        assert "EÜR" in leer.sheetnames and "Buchungen" in leer.sheetnames
        text = " ".join(
            str(z.value) for zeile in leer["EÜR"].iter_rows() for z in zeile if z.value
        )
        assert "keine Zahlung erfasst" in text
        assert "§ 11 EStG" in text

    def test_sachkonto_bleibt_leer(self, mappe):
        """Einen Kontenrahmen rät dieses Werkzeug nicht.

        Ein falsch kontierter Buchungsstapel fällt erst im Jahresabschluss auf
        — später als gar keiner.
        """
        _, zeilen = self._zeilen(mappe["Buchungen"])
        assert all(not z["Sachkonto"] for z in zeilen)


class TestLesbarkeit:
    def test_betraege_sind_euro_zahlen_keine_cent(self, mappe):
        """63646 in einer Zelle ist richtig und trotzdem unbrauchbar."""
        blatt = mappe["Zahlungen"]
        kopf = [z.value for z in blatt[1]]
        spalte = kopf.index("Betrag") + 1
        zelle = blatt.cell(row=2, column=spalte)
        assert zelle.value == 178.50
        assert "€" in zelle.number_format

    def test_datum_ist_ein_datum(self, mappe):
        blatt = mappe["Zahlungen"]
        kopf = [z.value for z in blatt[1]]
        zelle = blatt.cell(row=2, column=kopf.index("Datum") + 1)
        assert zelle.value == _dt.datetime(2026, 4, 1)
        assert zelle.number_format == "DD.MM.YYYY"

    def test_fremdschluessel_steht_im_klartext_daneben(self, mappe):
        blatt = mappe["Rechnungen"]
        kopf = [z.value for z in blatt[1]]
        assert kopf.index("Kunde") == kopf.index("Kunde-ID") + 1
        assert blatt.cell(row=2, column=kopf.index("Kunde") + 1).value == "Mustermann GmbH"

    def test_aufzaehlungen_sind_ausgeschrieben(self, mappe):
        blatt = mappe["Rechnungen"]
        kopf = [z.value for z in blatt[1]]
        assert blatt.cell(row=2, column=kopf.index("Status") + 1).value == "festgeschrieben"
        assert blatt.cell(row=2, column=kopf.index("Leistungsort") + 1).value == "Inland"

    def test_mengen_sind_stunden_keine_tausendstel(self, mappe):
        blatt = mappe["Rechnungspositionen"]
        kopf = [z.value for z in blatt[1]]
        assert blatt.cell(row=2, column=kopf.index("Menge") + 1).value == 2.0

    def test_prozentangaben_haben_den_richtigen_massstab(self):
        """Zwei Maßstäbe, zwei Umrechnungen.

        ``aufteilungsquote`` und ``quote_default`` führen GANZE Prozent (0..100),
        ``*_promille`` dagegen Promille. Beide durch 1000 zu teilen ließ 100 %
        betrieblichen Anteil als 10,0 % in der Tabelle stehen — und wer daraus
        seine Betriebsausgaben prüft, findet einen Fehler, den es nicht gibt.
        """
        assert ex._wert(100, ex._art("aufteilungsquote")) == 1.0
        assert ex._wert(60, ex._art("quote_default")) == 0.6
        assert ex._wert(190, ex._art("ust_satz_promille")) == 0.19

    def test_ja_nein_statt_null_und_eins(self, mappe):
        blatt = mappe["Kunden"]
        kopf = [z.value for z in blatt[1]]
        assert blatt.cell(row=2, column=kopf.index("Aktiv") + 1).value == "ja"

    def test_keine_ueberschrift_traegt_den_spaltennamen(self, mappe):
        """Ein Unterstrich in der Kopfzeile heisst: hier steht ein Feldname."""
        for name in mappe.sheetnames:
            if name == "Lies mich":
                continue
            for zelle in mappe[name][1]:
                assert "_" not in str(zelle.value), f"{name}: {zelle.value}"

    def test_kopfzeile_ist_fixiert(self, mappe):
        assert mappe["Rechnungen"].freeze_panes == "A2"


class TestDeckblatt:
    def test_deckblatt_nennt_die_luecken(self, mappe):
        text = "\n".join(
            str(z.value) for zeile in mappe["Lies mich"].iter_rows() for z in zeile
        )
        for tabelle in ex.NICHT_EXPORTIERT:
            assert tabelle in text
        assert "Belege" in text, "Dass die Belege NICHT drin sind, muss dastehen"

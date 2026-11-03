"""Entwurf, Festschreiben, Storno -- der Kern.

Die Reihenfolge ist hier das Pruefobjekt: Nummer und Datensatz in EINER
Transaktion, PDF danach. Laege das Rendering in der Transaktion, bliebe nach
einem Rollback ein PDF als Waise im Ordner liegen -- womoeglich mit einer
Nummer im Namen, die anschliessend an eine andere Rechnung geht.
"""

import json
import re
import sqlite3
import subprocess

import pytest

from app import db
from app.logik import festschreiben as f
from app.logik import gruppierung, nummernkreis


@pytest.fixture
def ordner(tmp_path):
    return tmp_path / "belege"


@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    # Die Rundungsregel wird hier AUSDRUECKLICH gesetzt und nicht aus der
    # Vorbelegung genommen. Die Vorbelegung ist "nicht runden", weil eine
    # Rundungsregel eine vertragliche Grundlage braucht -- diese Tests pruefen
    # aber gerade die Rundung, und ein Test, der von einer Vorbelegung lebt,
    # sagt beim naechsten Wechsel der Vorbelegung etwas anderes aus.
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01', iban = 'DE02120300000000202051',"
        " bic = 'BYLADEM1001', institut = 'DKB',"
        " rundung_modus = 'KAUFMAENNISCH', rundung_intervall_min = 15 WHERE id = 1"
    )
    conn.execute("UPDATE kunde SET satz_cent = 8000, zahlungsziel_tage = 14 WHERE id = ?",
                 (kunde_id,))
    return kunde_id


def _zeiten(conn, kunde_id, anzahl=7, minuten=7, titel="Beratung", modus="STUNDEN", **rest):
    ids = []
    for i in range(anzahl):
        cur = conn.execute(
            """
            INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum, dauer_minuten,
                                     abrechnungsmodus, grund, pauschal_cent,
                                     satz_cent_snapshot, erfasst_am)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kunde_id, titel, rest.get("beschreibung"), f"2026-03-{2 + i:02d}", minuten,
                modus, rest.get("grund"), rest.get("pauschal_cent"),
                rest.get("satz_cent_snapshot"), db.jetzt_utc(),
            ),
        )
        ids.append(int(cur.lastrowid))
    return ids


class TestEntwurf:
    def test_entwurf_belegt_die_zeiten_ohne_nummer_zu_ziehen(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["status"] == "ENTWURF"
        assert rechnung["nummer"] is None, "Die Nummer kommt erst beim Festschreiben"
        assert conn.execute(
            "SELECT COUNT(*) c FROM zeiteintrag WHERE rechnung_id = ?", (rid,)
        ).fetchone()["c"] == 7

    def test_leistungszeitraum_kommt_aus_den_zeiten(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet, anzahl=3)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["leistung_von"] == "2026-03-02"
        assert rechnung["leistung_bis"] == "2026-03-04"

    def test_faelligkeit_aus_dem_zahlungsziel(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet, anzahl=1)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids, datum="2026-03-02")
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["faellig_am"] == "2026-03-16"

    def test_rundungshinweis_steht_am_entwurf(self, conn, eingerichtet):
        """Eine Rundungsregel, die der Kunde nie gelesen hat, ist angreifbar.

        Angepasst mit Version 21: der Satz beschreibt jetzt, was geschieht --
        die Zeitsumme je Position wird gerundet. „Abrechnung im
        15-Minuten-Takt“ las sich wie ein Takt je Taetigkeit."""
        ids = _zeiten(conn, eingerichtet, anzahl=1)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        hinweis = conn.execute(
            "SELECT rundung_hinweis FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()[0]
        assert hinweis == (
            "Die Zeitsumme je Position ist kaufmännisch auf volle 15 Minuten gerundet.")

    def test_schon_belegte_zeiten_werden_uebergangen(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet, anzahl=3)
        f.entwurf_anlegen(conn, eingerichtet, ids[:2])
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        assert conn.execute(
            "SELECT COUNT(*) c FROM zeiteintrag WHERE rechnung_id = ?", (rid,)
        ).fetchone()["c"] == 1

    def test_ohne_freie_zeiten_wird_abgebrochen(self, conn, eingerichtet):
        with pytest.raises(f.FestschreibenFehler, match="abrechenbar"):
            f.entwurf_anlegen(conn, eingerichtet, [])

    def test_gruppierung_wechseln_erzeugt_positionen_neu(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids, modus=gruppierung.PRO_TITEL)
        assert conn.execute(
            "SELECT COUNT(*) c FROM rechnungsposition WHERE rechnung_id = ?", (rid,)
        ).fetchone()["c"] == 1

        f.gruppierung_wechseln(conn, rid, gruppierung.PRO_EINTRAG)
        assert conn.execute(
            "SELECT COUNT(*) c FROM rechnungsposition WHERE rechnung_id = ?", (rid,)
        ).fetchone()["c"] == 7
        # Die Verknuepfungen muessen mitgewandert sein, nicht verdoppelt.
        assert conn.execute(
            "SELECT COUNT(*) c FROM position_zeiteintrag"
        ).fetchone()["c"] == 7

    def test_kopfsumme_stimmt_mit_den_positionen(self, conn, eingerichtet):
        ids = _zeiten(conn, eingerichtet)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        summe = conn.execute(
            "SELECT SUM(betrag_cent) s FROM rechnungsposition WHERE rechnung_id = ?", (rid,)
        ).fetchone()["s"]
        assert rechnung["brutto_cent"] == summe == 6000

    def test_entwurf_verwerfen_gibt_die_zeiten_frei(self, conn, eingerichtet):
        """Der Loeschpfad muss explizit sein: solange ein Zeiteintrag auf den
        Entwurf zeigt, scheitert DELETE am Fremdschluessel."""
        ids = _zeiten(conn, eingerichtet, anzahl=3)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        f.entwurf_loeschen(conn, rid)
        assert conn.execute("SELECT COUNT(*) c FROM rechnung").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM zeiteintrag WHERE rechnung_id IS NULL"
        ).fetchone()["c"] == 3

    def test_verworfener_entwurf_hinterlaesst_keine_nummernluecke(self, conn, eingerichtet, ordner):
        ids = _zeiten(conn, eingerichtet, anzahl=2)
        f.entwurf_loeschen(conn, f.entwurf_anlegen(conn, eingerichtet, [ids[0]]))
        ergebnis = f.festschreiben(
            conn, f.entwurf_anlegen(conn, eingerichtet, [ids[1]]), belegordner=ordner
        )
        assert ergebnis.nummer.endswith("0001")
        assert nummernkreis.luecken(conn, 2026) == []


class TestFestschreiben:
    def test_vollstaendiger_durchlauf(self, conn, eingerichtet, ordner):
        ids = _zeiten(conn, eingerichtet)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)

        assert ergebnis.pdf_fehler is None
        assert ergebnis.nummer == "RE-2026-0001"
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["status"] == "FESTGESCHRIEBEN"
        assert rechnung["festgeschrieben_am"]
        assert rechnung["pdf_sha256"] == ergebnis.pdf_sha256
        from pathlib import Path

        assert Path(ergebnis.pdf_pfad).exists()

    def test_pdf_liegt_im_jahresordner_mit_sprechendem_namen(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)
        from pathlib import Path

        pfad = Path(ergebnis.pdf_pfad)
        assert pfad.parent == ordner / "ausgangsrechnungen" / "2026"
        assert pfad.name == "RE-2026-0001_Mustermann-GmbH.pdf"

    def test_snapshot_friert_die_kundendaten_ein(self, conn, eingerichtet, ordner):
        """Ohne Snapshot aenderte ein Kundenumzug rueckwirkend alle Altrechnungen,
        und das PDF im Ordner widersprueche der Datenbank."""
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        f.festschreiben(conn, rid, belegordner=ordner)
        conn.execute("UPDATE kunde SET name = 'Neuer Name GmbH', ort = 'Potsdam' WHERE id = ?",
                     (eingerichtet,))
        daten = f.pdf_daten(conn, rid)
        assert daten["empfaenger"]["name"] == "Mustermann GmbH"
        assert daten["empfaenger"]["ort"] == "Berlin"

    def test_satz_wird_an_den_zeiten_eingefroren(self, conn, eingerichtet, ordner):
        ids = _zeiten(conn, eingerichtet, anzahl=1)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        f.festschreiben(conn, rid, belegordner=ordner)
        assert conn.execute(
            "SELECT satz_cent_snapshot FROM zeiteintrag WHERE id = ?", (ids[0],)
        ).fetchone()[0] == 8000

    def test_zweimal_festschreiben_verbraucht_keine_zweite_nummer(
        self, conn, eingerichtet, ordner
    ):
        """Zwei offene Tabs oder ein Doppelklick -- der Klassiker."""
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        f.festschreiben(conn, rid, belegordner=ordner)
        with pytest.raises(f.FestschreibenFehler):
            f.festschreiben(conn, rid, belegordner=ordner)
        assert conn.execute(
            "SELECT COUNT(*) c FROM rechnung WHERE nummer IS NOT NULL"
        ).fetchone()["c"] == 1

    def test_veraltete_version_wird_abgewiesen(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        with pytest.raises(f.FestschreibenFehler, match="geändert|festgeschrieben"):
            f.festschreiben(conn, rid, version=99, belegordner=ordner)
        assert conn.execute(
            "SELECT status FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()[0] == "ENTWURF"

    def test_rote_befunde_blockieren(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        conn.execute("UPDATE kunde SET strasse = '' WHERE id = ?", (eingerichtet,))
        with pytest.raises(f.FestschreibenFehler, match="Anschrift"):
            f.festschreiben(conn, rid, belegordner=ordner)

    def test_festgeschriebene_rechnung_ist_danach_gesperrt(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        f.festschreiben(conn, rid, belegordner=ordner)
        with pytest.raises(sqlite3.IntegrityError, match="unveraenderbar"):
            conn.execute("UPDATE rechnung SET brutto_cent = 1 WHERE id = ?", (rid,))

    def test_pdf_fehler_laesst_die_rechnung_gueltig_und_nachholbar(
        self, conn, eingerichtet, ordner, monkeypatch
    ):
        """Scheitert das Rendering, existiert die Rechnung ohne Datei. Das ist
        sichtbar und behebbar -- im Gegensatz zu einer Waise mit verbrauchter
        Nummer, die entstuende, laege das Rendering in der Transaktion."""
        from app import pdf as pdfmodul

        def kaputt(*_, **__):
            raise pdfmodul.PdfFehler("Pango fehlt")

        monkeypatch.setattr(pdfmodul, "rendere", kaputt)
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)
        assert ergebnis.pdf_fehler and "Pango" in ergebnis.pdf_fehler
        assert ergebnis.nummer == "RE-2026-0001"
        rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert rechnung["status"] == "FESTGESCHRIEBEN"
        assert rechnung["pdf_pfad"] is None
        assert not list((ordner).rglob("*.pdf")), "Keine Waise im Ordner"

        monkeypatch.undo()
        nachgezogen = f.pdf_nachziehen(conn, rid, ordner)
        assert nachgezogen.pdf_fehler is None
        assert nachgezogen.pdf_sha256


class TestStorno:
    def _festgeschrieben(self, conn, kunde_id, ordner, anzahl=3):
        ids = _zeiten(conn, kunde_id, anzahl=anzahl)
        rid = f.entwurf_anlegen(conn, kunde_id, ids)
        f.festschreiben(conn, rid, belegordner=ordner)
        return rid, ids

    def test_storno_erzeugt_eigenes_dokument_mit_eigener_nummer(
        self, conn, eingerichtet, ordner
    ):
        rid, _ = self._festgeschrieben(conn, eingerichtet, ordner)
        ergebnis = f.storniere(conn, rid, "Tippfehler im Betrag", belegordner=ordner)
        assert ergebnis.nummer == "RE-2026-0002"
        storno = conn.execute(
            "SELECT * FROM rechnung WHERE id = ?", (ergebnis.rechnung_id,)
        ).fetchone()
        assert storno["typ"] == "STORNO"
        assert storno["storno_von_id"] == rid
        assert storno["brutto_cent"] < 0

    def test_storno_heisst_niemals_gutschrift(self, conn, eingerichtet, ordner):
        """Gutschrift bedeutet nach § 34a Satz 1 Nr. 6 UStDV Abrechnung durch
        den Leistungsempfaenger -- ein verbreiteter und vermeidbarer Fehler."""
        rid, _ = self._festgeschrieben(conn, eingerichtet, ordner)
        ergebnis = f.storniere(conn, rid, "Grund", belegordner=ordner)
        text = subprocess.run(
            ["pdftotext", "-layout", ergebnis.pdf_pfad, "-"],
            capture_output=True, text=True,
        ).stdout
        assert "Rechnungskorrektur" in text
        assert "Gutschrift" not in text
        assert "Storno zu Rechnung RE-2026-0001" in text

    def test_original_bleibt_erhalten(self, conn, eingerichtet, ordner):
        rid, _ = self._festgeschrieben(conn, eingerichtet, ordner)
        pfad = conn.execute("SELECT pdf_pfad FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        f.storniere(conn, rid, "Grund", belegordner=ordner)
        from pathlib import Path

        assert not Path(pfad).is_absolute(), "relativ zum Datenordner gespeichert"
        assert (ordner / pfad).exists(), "Das Original-PDF bleibt im Ordner"
        assert conn.execute(
            "SELECT status FROM rechnung WHERE id = ?", (rid,)
        ).fetchone()[0] == "STORNIERT"

    def test_zeiten_werden_freigegeben_und_sind_neu_abrechenbar(
        self, conn, eingerichtet, ordner
    ):
        rid, ids = self._festgeschrieben(conn, eingerichtet, ordner)
        f.storniere(conn, rid, "Grund", belegordner=ordner)
        frei = conn.execute(
            "SELECT * FROM v_zeiteintrag_status WHERE id = ?", (ids[0],)
        ).fetchone()
        assert frei["status"] == "AUFGESCHRIEBEN"
        assert conn.execute(
            "SELECT freigegeben_aus_rechnung_id FROM zeiteintrag WHERE id = ?", (ids[0],)
        ).fetchone()[0] == rid

        neu = f.entwurf_anlegen(conn, eingerichtet, ids)
        ergebnis = f.festschreiben(conn, neu, belegordner=ordner)
        assert ergebnis.nummer == "RE-2026-0003"

    def test_zeiten_koennen_gesperrt_bleiben(self, conn, eingerichtet, ordner):
        rid, ids = self._festgeschrieben(conn, eingerichtet, ordner)
        f.storniere(conn, rid, "Grund", zeiten_freigeben=False, belegordner=ordner)
        assert conn.execute(
            "SELECT rechnung_id FROM zeiteintrag WHERE id = ?", (ids[0],)
        ).fetchone()[0] == rid

    def test_zweites_storno_wird_abgewiesen(self, conn, eingerichtet, ordner):
        """Zwei Storno-Anfragen zur selben Rechnung -- der Doppelklick. Die
        zweite muss scheitern, sonst gibt es zwei Korrekturen mit zwei Nummern
        und doppelt negativem Betrag."""
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet))
        f.festschreiben(conn, rid, belegordner=ordner)
        f.storniere(conn, rid, "Probe", belegordner=ordner)
        with pytest.raises(f.FestschreibenFehler, match="storniert"):
            f.storniere(conn, rid, "Probe", belegordner=ordner)
        anzahl = conn.execute(
            "SELECT COUNT(*) FROM rechnung WHERE storno_von_id = ?", (rid,)
        ).fetchone()[0]
        assert anzahl == 1

    def test_neue_positionen_erhoehen_die_version(self, conn, eingerichtet):
        """Die Summe ist das betragstragende Feld: aendern sich die Positionen,
        muss das Festschreiben mit der alten Version scheitern."""
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet))
        vorher = conn.execute("SELECT version FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        f.gruppierung_wechseln(conn, rid, gruppierung.PRO_TAG)
        nachher = conn.execute("SELECT version FROM rechnung WHERE id = ?", (rid,)).fetchone()[0]
        assert nachher > vorher

    def test_storno_ohne_grund_wird_abgewiesen(self, conn, eingerichtet, ordner):
        rid, _ = self._festgeschrieben(conn, eingerichtet, ordner)
        with pytest.raises(f.FestschreibenFehler, match="Begründung"):
            f.storniere(conn, rid, "   ", belegordner=ordner)

    def test_entwurf_kann_nicht_storniert_werden(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        with pytest.raises(f.FestschreibenFehler, match="festgeschriebene"):
            f.storniere(conn, rid, "Grund", belegordner=ordner)


class TestPdfInhalt:
    def test_pflichtangaben_stehen_drauf_und_keine_umsatzsteuer(
        self, conn, eingerichtet, ordner
    ):
        ids = _zeiten(conn, eingerichtet, beschreibung="Konzept und Abstimmung")
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)
        text = subprocess.run(
            ["pdftotext", "-layout", ergebnis.pdf_pfad, "-"], capture_output=True, text=True
        ).stdout

        for pflicht in ("RE-2026-0001", "Mustermann GmbH", "Hauptstr. 1", "10115 Berlin",
                        "Mein Kleingewerbe", "Werkweg 2", "12/345/67890",
                        "§ 19", "steuerfrei", "0,75", "Std.", "60,00"):
            assert pflicht.lower() in text.lower(), f"{pflicht} fehlt auf dem PDF"

        # Kein Steuerfeld, nirgends. Die Fundstelle "UStG" im § 19-Hinweis ist
        # vorgeschrieben und daher ausgenommen.
        ohne_fundstelle = text.replace("UStG", "")
        for verboten in ("USt", "MwSt", "zzgl", "19 %", "7 %"):
            assert verboten not in ohne_fundstelle, f"{verboten} darf nicht auf dem PDF stehen"
        # "0,00 €" nur als eigener Betrag, nicht als Teil von "60,00 €".
        assert not re.search(r"(?<![\d,])0,00\s*€", text), \
            "Ein Nullbetrag darf nicht als Summe erscheinen"

    def test_einzelnachweis_als_anlage(self, conn, eingerichtet, ordner):
        ids = _zeiten(conn, eingerichtet)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids, modus=gruppierung.SAMMEL,
                                einzelnachweis=True)
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)
        text = subprocess.run(
            ["pdftotext", "-layout", ergebnis.pdf_pfad, "-"], capture_output=True, text=True
        ).stdout
        assert "Einzelnachweis" in text
        assert text.count("02.03.2026") >= 1
        assert "0:07" in text, "Der Nachweis zeigt die Rohzeiten"

    def test_kulanzzeile_sieht_nicht_wie_ein_fehler_aus(self, conn, eingerichtet, ordner):
        ids = _zeiten(conn, eingerichtet, anzahl=1, minuten=60)
        ids += _zeiten(conn, eingerichtet, anzahl=1, minuten=60, titel="Einarbeitung",
                       modus="KULANZ", grund="Kulanz", satz_cent_snapshot=8000)
        rid = f.entwurf_anlegen(conn, eingerichtet, ids)
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)
        text = subprocess.run(
            ["pdftotext", "-layout", ergebnis.pdf_pfad, "-"], capture_output=True, text=True
        ).stdout
        assert "nicht berechnet" in text
        assert "Kulanz" in text

    def test_hash_entspricht_der_datei(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        ergebnis = f.festschreiben(conn, rid, belegordner=ordner)
        aus_datei = subprocess.run(
            ["sha256sum", ergebnis.pdf_pfad], capture_output=True, text=True
        ).stdout.split()[0]
        assert aus_datei == ergebnis.pdf_sha256

    def test_journal_haelt_den_weg_fest(self, conn, eingerichtet, ordner):
        rid = f.entwurf_anlegen(conn, eingerichtet, _zeiten(conn, eingerichtet, anzahl=1))
        f.festschreiben(conn, rid, belegordner=ordner,
                        bestaetigte_warnungen=["G01", "G12"])
        aktionen = [
            z["aktion"] for z in conn.execute(
                "SELECT aktion FROM journal WHERE objekttyp = 'rechnung' ORDER BY id"
            )
        ]
        assert aktionen == ["ENTWURF", "FESTSCHREIBEN", "PDF"]
        begruendung = conn.execute(
            "SELECT begruendung FROM journal WHERE aktion = 'FESTSCHREIBEN'"
        ).fetchone()[0]
        assert json.loads(begruendung)["bestaetigte_warnungen"] == ["G01", "G12"]


class TestAbsenderSchnappschuss:
    """Was die Vorlage liest, muss im Schnappschuss stehen.

    ``rechnung_pdf.html`` fragt ``absender.zahlungsziel_zeigen``. Das Feld
    stand nicht in ``BETRIEB_SNAPSHOT`` — Jinja gibt eine unbekannte Variable
    still als leer aus, und der ganze Zahlungsabsatz (Fälligkeit, Konto,
    Verwendungszweck) verschwand von JEDER Rechnung, auch bei eingeschalteter
    Einstellung. Still, weil nichts fehlschlug.
    """

    def test_jedes_absenderfeld_der_vorlage_steht_im_schnappschuss(self):
        """Gegen genau diesen Fehler, für alle Felder auf einmal."""
        import re
        from pathlib import Path

        from app.logik.festschreiben import BETRIEB_SNAPSHOT

        vorlage = (
            Path(__file__).resolve().parent.parent / "app" / "vorlagen" / "rechnung_pdf.html"
        ).read_text(encoding="utf-8")
        benutzt = set(re.findall(r"absender\.([a-z_0-9]+)", vorlage))
        fehlt = benutzt - set(BETRIEB_SNAPSHOT)
        assert fehlt == set(), (
            f"Die Vorlage liest {sorted(fehlt)}, der Schnappschuss führt sie nicht — "
            "Jinja gibt sie still als leer aus"
        )

    def test_dasselbe_fuer_den_empfaenger(self):
        import re
        from pathlib import Path

        from app.logik.festschreiben import KUNDE_SNAPSHOT

        vorlage = (
            Path(__file__).resolve().parent.parent / "app" / "vorlagen" / "rechnung_pdf.html"
        ).read_text(encoding="utf-8")
        benutzt = set(re.findall(r"empfaenger\.([a-z_0-9]+)", vorlage))
        assert benutzt - set(KUNDE_SNAPSHOT) == set()

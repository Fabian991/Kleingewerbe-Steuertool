"""Kategoriekatalog und Belegschema.

Die Kategorie entscheidet, in welche Zeile der Anlage EUeR ein Beleg laeuft.
Ein Fehler hier ist still: die Summe stimmt, sie steht nur an der falschen
Stelle -- und das faellt frueher dem Finanzamt auf als dem Nutzer.
"""

import json
import sqlite3

import pytest

from app import db
from app.logik import kategorien as k


@pytest.fixture
def kategorie_id(conn):
    return k.nach_schluessel(conn, "SAAS")["id"]


@pytest.fixture
def lieferant_id(conn):
    return conn.execute(
        "INSERT INTO lieferant (name, land, angelegt_am) VALUES ('Hetzner', 'DE', ?)",
        (db.heute(),),
    ).lastrowid


def _beleg(conn, **felder):
    spalten = {
        "belegdatum": "2026-03-14",
        "betrag_brutto_cent": 10000,
        "betrag_betrieblich_cent": 10000,
        "erfasst_am": db.jetzt_utc(),
        **felder,
    }
    namen = ", ".join(spalten)
    platz = ", ".join("?" * len(spalten))
    return conn.execute(
        f"INSERT INTO beleg ({namen}) VALUES ({platz})", tuple(spalten.values())
    ).lastrowid


# ------------------------------------------------------------------ Katalog

class TestKatalogdatei:
    def test_datei_ist_lesbar_und_gueltig(self):
        kategorien = k.lies_datei()
        assert len(kategorien) > 25
        assert {e["bereich"] for e in kategorien} == {20, 25}

    def test_schluessel_sind_eindeutig(self):
        schluessel = [e["schluessel"] for e in k.lies_datei()]
        assert len(schluessel) == len(set(schluessel))

    def test_bewirtung_ist_zu_siebzig_prozent_abziehbar(self):
        bewirtung = next(e for e in k.lies_datei() if e["schluessel"] == "BEWIRTUNG")
        assert bewirtung["anteil_abziehbar_promille"] == 700
        assert bewirtung["kennzahl"] == 175
        assert bewirtung["kennzahl_nicht_abziehbar"] == 165

    def test_gwg_grenze_ist_netto(self):
        """R 9b Abs. 2 EStR: die Wertgrenze wird stets netto geprueft."""
        for schluessel in ("HARDWARE_GWG", "SOFTWARE_KAUF"):
            eintrag = next(e for e in k.lies_datei() if e["schluessel"] == schluessel)
            assert eintrag["grenze_netto_cent"] == 80000
            assert "netto" in eintrag["bezeichnung"].lower() or "netto" in eintrag["hinweis"]

    def test_geschenke_nennen_die_umgekehrte_regel(self):
        """Bei Geschenken zaehlt fuer die Freigrenze der BRUTTO-Betrag."""
        geschenke = next(e for e in k.lies_datei() if e["schluessel"] == "GESCHENKE")
        assert "BRUTTO" in geschenke["hinweis"]

    def test_tippfehler_im_feldnamen_wird_abgewiesen(self, tmp_path):
        """Ein ignoriertes Feld laege still mit Vorbelegung da."""
        datei = tmp_path / "k.json"
        datei.write_text(json.dumps({"kategorien": [{
            "schluessel": "X", "bezeichnung": "X", "bereich": 25, "kennzahl": 1,
            "anteil_abziehbahr_promille": 700,
        }]}), encoding="utf-8")
        with pytest.raises(ValueError, match="unbekannte Felder"):
            k.lies_datei(datei)

    def test_falscher_bereich_wird_abgewiesen(self, tmp_path):
        datei = tmp_path / "k.json"
        datei.write_text(json.dumps({"kategorien": [{
            "schluessel": "X", "bezeichnung": "X", "bereich": 30, "kennzahl": 1,
        }]}), encoding="utf-8")
        with pytest.raises(ValueError, match="bereich"):
            k.lies_datei(datei)


class TestAbgleich:
    def test_katalog_ist_beim_ersten_start_da(self, conn):
        assert len(k.katalog(conn)) > 25

    def test_zweiter_abgleich_aendert_nichts(self, conn):
        conn.execute("BEGIN IMMEDIATE")
        ergebnis = k.abgleichen(conn)
        conn.execute("COMMIT")
        assert ergebnis.angelegt == 0
        assert ergebnis.aktualisiert == 0
        assert ergebnis.unveraendert > 25

    def test_geaenderte_bezeichnung_wird_nachgezogen(self, conn, tmp_path):
        conn.execute("UPDATE euer_kategorie SET bezeichnung = 'alt' WHERE schluessel = 'SAAS'")
        conn.execute("BEGIN IMMEDIATE")
        ergebnis = k.abgleichen(conn)
        conn.execute("COMMIT")
        assert ergebnis.aktualisiert == 1
        assert k.nach_schluessel(conn, "SAAS")["bezeichnung"] != "alt"

    def test_eigene_kategorie_bleibt_stehen(self, conn):
        """Was der Nutzer selbst angelegt hat, wird nicht weggeraeumt."""
        conn.execute(
            "INSERT INTO euer_kategorie (schluessel, bezeichnung, bereich, kennzahl)"
            " VALUES ('EIGEN', 'Mein Sonderfall', 25, 183)"
        )
        conn.execute("BEGIN IMMEDIATE")
        ergebnis = k.abgleichen(conn)
        conn.execute("COMMIT")
        assert "EIGEN" in ergebnis.nur_in_der_datenbank
        assert k.nach_schluessel(conn, "EIGEN") is not None

    def test_kennzahl_darf_sich_wiederholen(self, conn):
        """Kennzahlen sind nur je Bereich eindeutig -- 111 steht mehrfach."""
        mit_111 = [z for z in k.katalog(conn, bereich=20) if z["kennzahl"] == 111]
        assert len(mit_111) >= 3


# ----------------------------------------------------------------- Rechnen

class TestBetrieblich:
    @pytest.mark.parametrize("brutto,quote,erwartet", [
        (10000, 100, 10000),
        (10000, 70, 7000),
        (9999, 50, 5000),      # 49,995 -> kaufmaennisch auf
        (10001, 50, 5001),     # 50,005 -> kaufmaennisch auf
        (10000, 0, 0),
    ])
    def test_quote(self, brutto, quote, erwartet):
        assert k.betrieblich_cent(brutto, quote) == erwartet

    def test_quote_ausserhalb_wird_abgewiesen(self):
        with pytest.raises(ValueError):
            k.betrieblich_cent(10000, 120)


class TestNettoFuerGrenzwert:
    def test_ausgewiesenes_netto_gilt(self):
        assert k.netto_fuer_grenzwert_cent(95200, 80000, 190) == 80000

    def test_ohne_netto_wird_zurueckgerechnet(self):
        """800 EUR netto sind bei 19 Prozent 952 EUR brutto."""
        assert k.netto_fuer_grenzwert_cent(95200, None, 190) == 80000

    def test_ohne_steuersatz_ist_netto_gleich_brutto(self):
        """Lieferant selbst Kleinunternehmer, Auslandsbeleg, Reverse Charge:
        eine Steuer, die nicht erhoben wurde, kann man nicht herausrechnen."""
        assert k.netto_fuer_grenzwert_cent(80000, None, None) == 80000
        assert k.netto_fuer_grenzwert_cent(80000, None, 0) == 80000

    def test_die_gwg_grenze_trennt_brutto_und_netto(self, conn):
        """Der Fall, an dem sich die Ratgeber im Netz widersprechen.

        Ein Buerostuhl fuer 900 EUR brutto liegt mit 756,30 EUR netto UNTER der
        Grenze von 800 EUR und ist damit sofort abziehbar -- abgeschrieben wird
        aber der Bruttowert.
        """
        grenze = k.nach_schluessel(conn, "HARDWARE_GWG")["grenze_netto_cent"]
        netto = k.netto_fuer_grenzwert_cent(90000, None, 190)
        assert netto == 75630
        assert netto <= grenze
        # Ein Beleg fuer 952 EUR brutto liegt genau auf der Grenze.
        assert k.netto_fuer_grenzwert_cent(95200, None, 190) == grenze
        # Einen Cent mehr, und es ist ein Anlagegut.
        assert k.netto_fuer_grenzwert_cent(95300, None, 190) > grenze


# ------------------------------------------------------------------ Schema

class TestBelegschema:
    def test_beleg_laesst_sich_anlegen(self, conn, kategorie_id, lieferant_id):
        bid = _beleg(conn, kategorie_id=kategorie_id, lieferant_id=lieferant_id)
        assert conn.execute("SELECT art FROM beleg WHERE id = ?", (bid,)).fetchone()[0] == "EINGANG"

    def test_netto_ohne_ust_wird_abgewiesen(self, conn):
        """Ein halb ausgelesener Beleg gilt spaeter als vollstaendig."""
        with pytest.raises(sqlite3.IntegrityError):
            _beleg(conn, betrag_netto_cent=8403)

    def test_netto_plus_ust_muss_brutto_ergeben(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            _beleg(conn, betrag_netto_cent=8000, betrag_ust_cent=1000)
        _beleg(conn, betrag_netto_cent=8403, betrag_ust_cent=1597)

    def test_reverse_charge_vertraegt_keine_ausgewiesene_ust(self, conn):
        """Schuldet der Empfaenger die Steuer, steht auf dem Beleg keine."""
        with pytest.raises(sqlite3.IntegrityError):
            _beleg(conn, reverse_charge=1, betrag_netto_cent=8403, betrag_ust_cent=1597)
        _beleg(conn, reverse_charge=1, betrag_netto_cent=10000, betrag_ust_cent=0)

    def test_nullbeleg_wird_abgewiesen(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            _beleg(conn, betrag_brutto_cent=0, betrag_betrieblich_cent=0)

    def test_quote_ausserhalb_wird_abgewiesen(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            _beleg(conn, aufteilungsquote=120)


class TestBelegdatei:
    def _datei(self, conn, beleg_id, sha="a" * 64, rolle="ORIGINAL", **rest):
        return conn.execute(
            "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse,"
            " rolle, importiert_am) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (beleg_id, rest.get("pfad", "belege/2026/a.pdf"),
             rest.get("original_name", "rechnung.pdf"), sha, 1234, rolle, db.jetzt_utc()),
        ).lastrowid

    def test_dieselbe_datei_zweimal_wird_abgewiesen(self, conn):
        """Der haeufigste Erfassungsfehler: ein Beleg zweimal gebucht."""
        erster, zweiter = _beleg(conn), _beleg(conn)
        self._datei(conn, erster)
        with pytest.raises(sqlite3.IntegrityError):
            self._datei(conn, zweiter)

    def test_abgeleitetes_xml_kollidiert_nicht(self, conn):
        """Nur Originale sind global eindeutig -- sonst blockierte das aus einem
        ZUGFeRD-PDF gezogene XML den naechsten Beleg desselben Inhalts."""
        bid = _beleg(conn)
        self._datei(conn, bid, sha="b" * 64)
        self._datei(conn, bid, sha="c" * 64, rolle="ABGELEITETES_XML",
                    pfad="belege/2026/a.xml")
        zweiter = _beleg(conn)
        self._datei(conn, zweiter, sha="c" * 64, rolle="ABGELEITETES_XML",
                    pfad="belege/2026/b.xml")

    def test_pruefsumme_ist_unveraenderlich(self, conn):
        """Pfad und Pruefsumme nachtraeglich zu aendern hiesse, den Beleg
        auszutauschen -- § 146 Abs. 4 AO."""
        bid = _beleg(conn)
        did = self._datei(conn, bid, sha="d" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE belegdatei SET sha256 = ? WHERE id = ?", ("e" * 64, did))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE belegdatei SET pfad = 'woanders.pdf' WHERE id = ?", (did,))

    def test_beleg_mit_datei_darf_nicht_geloescht_werden(self, conn):
        bid = _beleg(conn)
        self._datei(conn, bid, sha="f" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM beleg WHERE id = ?", (bid,))

    def test_beleg_ohne_datei_wird_markiert_nicht_geloescht(self, conn):
        """Auch ein Beleg ohne Datei (Eigenbeleg, Fehleingabe) verschwindet
        nicht per DELETE: Loeschen heisst seit Version 25 markieren, mit Grund
        (GoBD Rz. 107 ff.). Vorher sicherte dieser Test das physische Loeschen
        zu -- das ging aber nur an einem Beleg ohne Auslesung, also an keinem,
        den das Programm erzeugt (extraktion verweist per Fremdschluessel)."""
        bid = _beleg(conn)
        with pytest.raises(sqlite3.IntegrityError, match="nie geloescht"):
            conn.execute("DELETE FROM beleg WHERE id = ?", (bid,))
        conn.execute("UPDATE beleg SET geloescht_am = ?, geloescht_grund = 'Testbeleg'"
                     " WHERE id = ?", (db.jetzt_utc(), bid))
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM v_beleg_gueltig").fetchone()[0] == 0


class TestZahlungAufBeleg:
    def test_zahlung_auf_einen_echten_beleg(self, conn):
        bid = _beleg(conn)
        conn.execute(
            "INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
            " VALUES (?, '2026-03-20', -10000, 'UEBERWEISUNG')",
            (bid,),
        )

    def test_zahlung_auf_einen_erfundenen_beleg_wird_abgewiesen(self, conn):
        """Eine Betriebsausgabe ohne Beleg ist in der EUeR genau die Zahl,
        die niemand erklaeren kann."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
                " VALUES (999, '2026-03-20', -10000, 'UEBERWEISUNG')"
            )

    def test_zahlung_kann_nicht_auf_einen_erfundenen_beleg_umgehaengt_werden(self, conn):
        bid = _beleg(conn)
        zid = conn.execute(
            "INSERT INTO zahlung (beleg_id, datum, betrag_cent, art)"
            " VALUES (?, '2026-03-20', -10000, 'UEBERWEISUNG')", (bid,),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE zahlung SET beleg_id = 999 WHERE id = ?", (zid,))

    def test_zahlung_braucht_genau_eine_seite(self, conn):
        bid = _beleg(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO zahlung (datum, betrag_cent, art)"
                " VALUES ('2026-03-20', -100, 'BAR')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO zahlung (rechnung_id, beleg_id, datum, betrag_cent, art)"
                " VALUES (1, ?, '2026-03-20', -100, 'BAR')", (bid,),
            )


class TestLieferant:
    def test_name_ist_einmalig_ohne_ruecksicht_auf_schreibweise(self, conn, lieferant_id):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lieferant (name, angelegt_am) VALUES ('hetzner', ?)",
                (db.heute(),),
            )

    def test_profil_merkt_sich_das_zahlenformat(self, conn, lieferant_id):
        """AWS, Google und Figma rechnen in Euro, schreiben aber 1,234.56."""
        conn.execute(
            "INSERT INTO lieferant_profil (lieferant_id, feld, muster, zahlenformat,"
            " gelernt_am) VALUES (?, 'brutto', 'Total:\\s*([\\d.,]+)', 'EN', ?)",
            (lieferant_id, db.jetzt_utc()),
        )
        zeile = conn.execute("SELECT * FROM lieferant_profil").fetchone()
        assert zeile["zahlenformat"] == "EN"

    def test_unbekanntes_feld_im_profil_wird_abgewiesen(self, conn, lieferant_id):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO lieferant_profil (lieferant_id, feld, muster, gelernt_am)"
                " VALUES (?, 'lieblingsfarbe', 'x', ?)", (lieferant_id, db.jetzt_utc()),
            )


class TestExtraktion:
    def test_nur_die_vier_vorgesehenen_quellen(self, conn):
        bid = _beleg(conn)
        for quelle in ("xml", "pdf_text", "ocr_lokal", "manuell"):
            conn.execute(
                "INSERT INTO extraktion (beleg_id, quelle, zeitpunkt) VALUES (?, ?, ?)",
                (bid, quelle, db.jetzt_utc()),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO extraktion (beleg_id, quelle, zeitpunkt) VALUES (?, 'cloud', ?)",
                (bid, db.jetzt_utc()),
            )

"""Restpunkte der Fremdwaehrung aus der Abnahme (R1 bis R6).

Jeder Abschnitt ist ein Befund: was vorher passierte, und was jetzt gilt.
Kein Test geht ins Netz (tests/conftest.py); die Kurstabelle ist die kleine
Pruefdatei aus tests/fixtures.
"""

from __future__ import annotations

import errno
import io
import time
import urllib.error
from datetime import date

import pytest

from app.logik import kurse
from app.logik.beleg import pipeline as p
from app.logik.beleg import umrechnung
from tests import test_fremdwaehrung as fw
from tests.test_fremdwaehrung import (
    AWS_EZB_CENT,
    AWS_FORMULAR,
    HEUTE,
    _aws,
    _aws_buchen,
    _bestaetigt,
    _hochladen,
    _hosting,
    _lieferant_aws,
    aws_kopie,
    csv_voll,
    ezb_zip,
    tabelle_ablegen,
)

# Die Fixtures aus test_fremdwaehrung.py, hier unter demselben Namen.
datenordner = fw.datenordner
eingerichtet = fw.eingerichtet
klient = fw.klient
datenbank = fw.datenbank

# ======================================================= R1: verlorene Waehrung


class TestDubletteMitVerlorenerWaehrung:
    """Erste Kopie als 1.234,56 USD gebucht, die zweite Kopie derselben
    Rechnung als 1.234,56 EUR gelesen (gleiches Datum, gleicher Lieferant,
    keine Belegnummer): vorher ohne Warnung gebucht -- die Rechnung stand
    zweimal in der EUeR, einmal mit dem Dollarbetrag als Euro."""

    def _usd_gebucht(self, conn, datenordner) -> int:
        tabelle_ablegen(datenordner)
        lid = _lieferant_aws(conn)
        lesung = _aws(conn, datenordner)
        p.buche(conn, lesung, _bestaetigt(lesung, belegnummer=None), datenordner,
                lieferant_id=lid)
        return lid

    def test_euro_lesung_findet_den_dollarbeleg(self, conn, eingerichtet, datenordner):
        """Die Euro-Lesung trifft den Dollarbeleg; die Warnung nennt beide Waehrungen."""
        lid = self._usd_gebucht(conn, datenordner)
        treffer = p.aehnliche_belege(conn, lid, None, "2026-03-03", 123456, waehrung="EUR")
        assert [b["id"] for b in treffer] == [1]
        text = p.aehnliche_als_text(treffer, waehrung="EUR")
        assert "1.234,56 USD (1.063,73 €)" in text
        assert "dort in USD, hier in EUR: ähnlich, Währung prüfen" in text

    def test_buchen_als_euro_wird_abgewiesen(self, conn, eingerichtet, datenordner):
        """``buche`` weist die Kopie als Euro ab, bis "trotzdem" gesetzt ist."""
        lid = self._usd_gebucht(conn, datenordner)
        zweite = _aws(conn, datenordner, aws_kopie(), "aws-kopie.pdf")
        with pytest.raises(p.AehnlicherBeleg, match="Währung prüfen"):
            p.buche(conn, zweite,
                    _bestaetigt(zweite, belegnummer=None, waehrung="EUR", brutto=123456),
                    datenordner, lieferant_id=lid)
        assert conn.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1

    def test_auch_bei_anderem_lieferanten_datensatz_und_umgekehrt(self, conn, eingerichtet,
                                                                  datenordner):
        """Unter einem anderen Datensatz und in der Gegenrichtung: erst als
        Euro gebucht, dann als Dollar gelesen."""
        lid = self._usd_gebucht(conn, datenordner)
        andere = conn.execute("INSERT INTO lieferant (name, angelegt_am) VALUES ('AWS', 'x')"
                              ).lastrowid
        assert [b["id"] for b in p.aehnliche_bei_anderen(
            conn, andere, None, "2026-03-03", 123456, waehrung="EUR")] == [1]
        conn.execute(
            "INSERT INTO beleg (lieferant_id, belegdatum, waehrung, betrag_brutto_cent,"
            " betrag_betrieblich_cent, erfasst_am) VALUES (?, '2026-03-10', 'EUR', 5000,"
            " 5000, 'x')", (lid,))
        treffer = p.aehnliche_belege(conn, lid, None, "2026-03-10", 5000, waehrung="USD")
        assert [b["id"] for b in treffer] == [2]
        assert "dort in EUR, hier in USD" in p.aehnliche_als_text(treffer, waehrung="USD")

    def test_gleiche_waehrung_ohne_hinweis(self, conn, eingerichtet, datenordner):
        """Derselbe Fall in derselben Waehrung bleibt die gewohnte Warnung."""
        lid = self._usd_gebucht(conn, datenordner)
        treffer = p.aehnliche_belege(conn, lid, None, "2026-03-03", 123456, waehrung="USD")
        assert "Währung prüfen" not in p.aehnliche_als_text(treffer, waehrung="USD")
        # Der Euro-Betrag eines anderen Tages trifft nicht.
        assert p.aehnliche_belege(conn, lid, None, "2026-03-04", AWS_EZB_CENT) == []

    def test_buchen_in_der_maske_als_euro(self, klient, datenbank):
        """Ueber die Pruefmaske: die Kopie mit "EUR" statt "USD" bestaetigt
        ergibt die 422 mit dem Hinweis -- gebucht wird nur mit "trotzdem"."""
        assert _aws_buchen(klient, datenbank).status_code == 303
        name = _hochladen(klient, aws_kopie(), "aws-kopie.pdf")
        daten = {**AWS_FORMULAR, "kategorie_id": _hosting(datenbank), "waehrung": "EUR",
                 "reverse_charge": ""}
        antwort = klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)
        assert antwort.status_code == 422
        assert "dort in USD, hier in EUR: ähnlich, Währung prüfen" in antwort.text
        with datenbank() as verbindung:
            assert verbindung.execute("SELECT COUNT(*) FROM beleg").fetchone()[0] == 1


# ================================================ R2: Kartenbetrag = Fremdbetrag


class TestKartenbetragWieFremdbetrag:
    """100,00 EUR laut Karte bei einer Rechnung ueber 100,00 CHF: keine 10 %
    vom umgerechneten Wert (109,81 EUR) entfernt, also vorher keine
    Rueckfrage -- obwohl es meist der abgeschriebene Rechnungsbetrag ist."""

    def test_berechne_fragt_nach(self, datenordner):
        """Gleicher Zahlenwert: Rueckfrage, auch unter 10 % Abweichung."""
        tabelle_ablegen(datenordner)
        with pytest.raises(umrechnung.KartenbetragWieFremdbetrag) as fehler:
            umrechnung.berechne(datenordner, "CHF", 10000, date(2026, 3, 3), karte_cent=10000,
                                heute=HEUTE)
        assert ("Ist das wirklich der Euro-Betrag laut Kreditkarte und nicht der "
                "Rechnungsbetrag in CHF?") in str(fehler.value)
        assert isinstance(fehler.value, umrechnung.KartenbetragAbweichend)

    def test_bestaetigt_gilt_er(self, datenordner):
        """Mit dem Haken "so uebernehmen" zaehlt der Kartenbetrag."""
        tabelle_ablegen(datenordner)
        u = umrechnung.berechne(datenordner, "CHF", 10000, date(2026, 3, 3), karte_cent=10000,
                                karte_bestaetigt=True, heute=HEUTE)
        assert u.euro_cent == 10000

    def test_anderer_zahlenwert_unter_zehn_prozent_ohne_frage(self, datenordner):
        """Ein Kartenbetrag nahe am umgerechneten Wert bleibt ohne Rueckfrage."""
        tabelle_ablegen(datenordner)
        u = umrechnung.berechne(datenordner, "CHF", 10000, date(2026, 3, 3), karte_cent=11000,
                                heute=HEUTE)
        assert u.euro_cent == 11000

    def test_vorschlag_der_maske(self, datenordner):
        """Der Vorschlag beim Tippen sagt es schon, statt "mehr als 10 %"."""
        tabelle_ablegen(datenordner)
        v = umrechnung.vorschlag(datenordner, "CHF", 10000, date(2026, 3, 3), karte_cent=10000,
                                 heute=HEUTE)
        assert v["rueckfrage"] is True
        assert "nicht der Rechnungsbetrag in CHF?" in v["karte_frage"]
        ohne = umrechnung.vorschlag(datenordner, "CHF", 10000, date(2026, 3, 3),
                                    karte_cent=11000, heute=HEUTE)
        assert ohne["rueckfrage"] is False and ohne["karte_frage"] is None

    def test_pruefmaske_und_kartenbetrag_route(self, klient, datenbank):
        """Pruefmaske: 422 mit der Frage und dem Haken; mit Haken gebucht.
        Danach fragt auch "Kartenbetrag eintragen" bei demselben Zahlenwert."""
        name = _hochladen(klient)
        daten = {**AWS_FORMULAR, "kategorie_id": _hosting(datenbank), "brutto_eur": "1.234,56"}
        antwort = klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)
        assert antwort.status_code == 422
        # Ueber 10 % daneben UND gleicher Zahlenwert: beides in einem Satz.
        assert "und weicht um +170,83 € / +16,06 %" in antwort.text
        assert "nicht der Rechnungsbetrag in USD?" in antwort.text
        assert "Kartenbetrag so übernehmen" in antwort.text
        assert 'value="1.234,56"' in antwort.text
        daten["brutto_eur"] = "1.100,00"
        assert klient.post(f"/belege/pruefen/{name}", data=daten,
                           follow_redirects=False).status_code == 303
        antwort = klient.post("/belege/1/kartenbetrag", data={"betrag": "1.234,56"},
                              follow_redirects=False)
        assert antwort.status_code == 422
        assert "nicht der Rechnungsbetrag in USD?" in antwort.text
        antwort = klient.post("/belege/1/kartenbetrag",
                              data={"betrag": "1.234,56", "bestaetigt": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        with datenbank() as conn:
            assert conn.execute("SELECT betrag_karte_cent FROM beleg").fetchone()[0] == 123456


# ================================== R3: Meldungen zur Kurstabelle ohne Doppeltes


class TestMeldungenKurstabelle:
    """Doppelter Vorsatz in der Belegliste und dieselbe Aufforderung zweimal
    in der 422 der Pruefmaske."""

    def test_belegliste_nennt_die_ursache_einmal(self, klient, tmp_path):
        """"Das letzte Auffrischen scheiterte: keine Internetverbindung ..." --
        nicht ": Die Kurstabelle ließ sich nicht laden: ..."."""
        kurse._FEHLSCHLAG[kurse._schluessel(tmp_path)] = (
            time.time(), "Die Kurstabelle ließ sich nicht laden: keine Internetverbindung "
                         "(www.ecb.europa.eu nicht erreichbar).")
        seite = klient.get("/belege").text
        assert ("Das letzte Auffrischen scheiterte: keine Internetverbindung "
                "(www.ecb.europa.eu nicht erreichbar).") in seite
        assert "scheiterte: Die Kurstabelle" not in seite

    def test_kurs_noch_nicht_veroeffentlicht_aufforderung_einmal(self, klient, datenbank):
        """Belegdatum nach dem Ende der (frischen) Tabelle: die 422 nennt die
        Aufforderung "Bitte später buchen ..." genau einmal (im Kasten "In
        Euro"), die Fehlermeldung oben nur den Grund."""
        antwort = _aws_buchen(klient, datenbank, belegdatum="2026-04-13")
        assert antwort.status_code == 422
        assert "noch nicht veröffentlicht" in antwort.text
        assert "Ohne Kurs fehlt der Euro-Betrag für die EÜR." in antwort.text
        assert antwort.text.count("Bitte später buchen") == 1


# ===================================== R4: deutsche Ursache statt Systemtext


class _Antwort(io.BytesIO):
    status = 200
    headers: dict = {}

    def geturl(self):
        return kurse.QUELLE


class TestNetzgrundDeutsch:
    """Englische Texte des Betriebssystems in Klammern ("[Errno 101] Network
    is unreachable", "Tunnel connection failed: 403 Forbidden") wurden so in
    die Meldung uebernommen."""

    @pytest.mark.parametrize(("fehler", "erwartet"), [
        (OSError(errno.ENETUNREACH, "Network is unreachable"),
         "das Netzwerk ist nicht erreichbar"),
        (OSError("Tunnel connection failed: 403 Forbidden"),
         "der Proxy-Server hat die Verbindung zur EZB abgelehnt (Status 403)"),
        (OSError(errno.EACCES, "Permission denied"), "keine Berechtigung"),
        (ValueError("unknown url type"), "keine Verbindung zur EZB"),
    ])
    def test_abruf_meldet_deutsch(self, fehler, erwartet):
        """Je Fall die deutsche Ursache, kein "Errno", kein englischer Text."""
        def oeffne(anfrage, timeout):
            raise urllib.error.URLError(fehler)

        with pytest.raises(kurse.AbrufFehler) as gefangen:
            kurse.abrufen_https(oeffne=oeffne)
        text = str(gefangen.value)
        assert erwartet in text
        for englisch in ("Errno", "unreachable", "Tunnel", "Forbidden", "denied", "url type"):
            assert englisch not in text

    def test_abbruch_nach_teilantwort(self):
        """Kam schon ein Teil an, heisst es nicht "keine Antwort innerhalb von
        20 Sekunden", sondern: der Download brach nach so vielen Bytes ab."""
        def oeffne(anfrage, timeout):
            antwort = _Antwort(b"PK\x03\x04")
            stuecke = iter([b"PK\x03\x04", TimeoutError("timed out")])

            def read1(n=-1):
                stueck = next(stuecke)
                if isinstance(stueck, Exception):
                    raise stueck
                return stueck

            antwort.read1 = read1
            return antwort

        with pytest.raises(kurse.AbrufFehler) as gefangen:
            kurse.abrufen_https(oeffne=oeffne)
        assert str(gefangen.value) == (
            "Die Kurstabelle ließ sich nicht laden: der Download brach nach 4 Bytes ab "
            "(keine vollständige Antwort innerhalb von 20 Sekunden).")

    def test_verbindung_bricht_mitten_ab(self):
        """Eine unterbrochene Verbindung mitten im Download: ebenso, mit Grund."""
        def oeffne(anfrage, timeout):
            antwort = _Antwort(b"")
            stuecke = iter([b"PK" * 10, ConnectionResetError(104, "Connection reset by peer")])

            def read1(n=-1):
                stueck = next(stuecke)
                if isinstance(stueck, Exception):
                    raise stueck
                return stueck

            antwort.read1 = read1
            return antwort

        with pytest.raises(kurse.AbrufFehler) as gefangen:
            kurse.abrufen_https(oeffne=oeffne)
        assert "brach nach 20 Bytes ab (die Verbindung wurde unterbrochen)" in str(gefangen.value)
        assert "reset by peer" not in str(gefangen.value)


# ========================================== R5: Mindestumfang beim ersten Abruf


class TestErsterAbruf:
    """Ohne vorhandene Tabelle gab es nichts zu vergleichen: eine gekuerzte
    Historie oder eine ohne Pfund wurde angenommen."""

    def test_gekuerzte_historie_wird_abgewiesen(self, tmp_path):
        """Die Pruefdatei beginnt am 26.02.2026 -- als erster Abruf abgewiesen."""
        with pytest.raises(kurse.TabelleUngueltig, match="beginnt erst am 26.02.2026"):
            kurse.aktualisiere(tmp_path, abruf=lambda url: ezb_zip())
        assert kurse.tabelle(tmp_path) is None

    def test_fehlende_hauptwaehrung_wird_abgewiesen(self, tmp_path):
        """Volle Laenge, aber am juengsten Tag kein GBP: abgewiesen."""
        with pytest.raises(kurse.TabelleUngueltig, match="fehlt am 10.04.2026 GBP"):
            kurse.aktualisiere(tmp_path, abruf=lambda url: ezb_zip(csv_voll(ohne="GBP")))
        assert kurse.tabelle(tmp_path) is None

    def test_vollstaendige_tabelle_wird_gespeichert(self, tmp_path):
        """Ab 04.01.1999 mit allen Hauptwaehrungen: gespeichert."""
        tab = kurse.aktualisiere(tmp_path, abruf=lambda url: ezb_zip(csv_voll()))
        assert tab.erster_tag == date(1999, 1, 4)
        assert kurse.tabelle(tmp_path).letzter_tag == date(2026, 4, 10)

    def test_mit_vorhandener_tabelle_gilt_der_vergleich(self, tmp_path):
        """Liegt schon eine Tabelle da, entscheidet wie bisher der Vergleich
        mit ihr (nicht kuerzer, keine Waehrung weniger)."""
        tabelle_ablegen(tmp_path)
        neu = kurse.aktualisiere(tmp_path, abruf=lambda url: ezb_zip())
        assert neu.erster_tag == date(2026, 2, 26)


# ======================================= R6: neuer Lieferant erst mit der Buchung


class TestNeuerLieferantMitDerBuchung:
    """Ein in der Pruefmaske neu getippter Lieferant wurde angelegt und
    festgeschrieben, bevor ``buche`` den Beleg als Dublette abwies -- er blieb
    ohne Beleg stehen, und beim naechsten Versuch entstanden doppelte
    Lieferanten-Datensaetze."""

    def _lieferanten(self, datenbank) -> list[str]:
        with datenbank() as conn:
            return [z[0] for z in conn.execute("SELECT name FROM lieferant ORDER BY id")]

    def test_abgewiesen_bleibt_kein_lieferant_zurueck(self, klient, datenbank):
        """422 als Dublette: der neue Lieferant ist nicht angelegt, kein
        Journaleintrag; die Eingabe steht noch in der Maske."""
        assert _aws_buchen(klient, datenbank).status_code == 303
        name = _hochladen(klient, aws_kopie(), "aws-kopie.pdf")
        daten = {**AWS_FORMULAR, "kategorie_id": _hosting(datenbank),
                 "lieferant_neu": "AWS neu getippt", "lieferant_ust_idnr": ""}
        antwort = klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)
        assert antwort.status_code == 422
        assert 'value="AWS neu getippt"' in antwort.text
        assert self._lieferanten(datenbank) == ["Amazon Web Services EMEA SARL"]
        with datenbank() as conn:
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'ANLEGEN'"
                                " AND objekttyp = 'lieferant'").fetchone()[0] == 1

    def test_mit_trotzdem_entsteht_er_genau_einmal(self, klient, datenbank):
        """Erst abgewiesen, dann mit "trotzdem buchen": ein Datensatz, ein
        Journaleintrag, und der Beleg haengt an ihm."""
        _aws_buchen(klient, datenbank)
        name = _hochladen(klient, aws_kopie(), "aws-kopie.pdf")
        daten = {**AWS_FORMULAR, "kategorie_id": _hosting(datenbank),
                 "lieferant_neu": "AWS neu getippt", "lieferant_ust_idnr": ""}
        klient.post(f"/belege/pruefen/{name}", data=daten, follow_redirects=False)
        antwort = klient.post(f"/belege/pruefen/{name}", data={**daten, "trotzdem": "1"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert self._lieferanten(datenbank) == ["Amazon Web Services EMEA SARL",
                                                "AWS neu getippt"]
        with datenbank() as conn:
            assert conn.execute("SELECT l.name FROM beleg b JOIN lieferant l"
                                " ON l.id = b.lieferant_id WHERE b.id = 2").fetchone()[0] \
                == "AWS neu getippt"
            assert conn.execute("SELECT COUNT(*) FROM journal WHERE aktion = 'ANLEGEN'"
                                " AND objekttyp = 'lieferant'").fetchone()[0] == 2

    def test_buche_nimmt_vorhandenen_statt_doppelt(self, conn, eingerichtet, datenordner):
        """``buche`` mit ``lieferant_neu``: gibt es ihn (USt-IdNr.) schon,
        gilt der vorhandene -- kein zweiter Datensatz."""
        tabelle_ablegen(datenordner)
        lid = _lieferant_aws(conn)
        lesung = _aws(conn, datenordner)
        buchung = p.buche(conn, lesung, _bestaetigt(lesung), datenordner,
                          lieferant_neu=("AWS anders geschrieben", "LU26888617"))
        assert buchung.lieferant_id == lid
        assert conn.execute("SELECT COUNT(*) FROM lieferant").fetchone()[0] == 1

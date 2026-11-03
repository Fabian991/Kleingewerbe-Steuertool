"""Steuerrücklage: Befunde der Prüfrunde, je Befund ein Test.

Jeder Test schlug auf dem Stand vor der Korrektur fehl. Die Vorlagen der
Grundtests (``klient``, ``_schaetze``, ``_einnahme_und_ausgabe``) kommen aus
``test_steuer.py``.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import erzeuge_app
from tests.test_steuer import EINRICHTUNG, _einnahme_und_ausgabe, _schaetze  # noqa: F401


@pytest.fixture
def klient(tmp_path, monkeypatch):
    """Ein eingerichteter Betrieb; heute ist der 25.10.2026 (wie in test_steuer.py)."""
    monkeypatch.setattr(db, "heute", lambda: "2026-10-25")
    with TestClient(erzeuge_app(tmp_path / "steuer.sqlite")) as c:
        c.post("/einrichtung", data=EINRICHTUNG, follow_redirects=False)
        yield c


def _seite(c, **daten):
    """Speichert Angaben für 2026 (sofern nicht anders gesagt) und liefert die Seite danach."""
    daten = {"jahr": "2026"} | daten
    antwort = c.post("/steuer", data=daten, follow_redirects=False)
    if antwort.status_code == 303:
        return c.get(antwort.headers["location"]).text
    return antwort.text


def _klartext(html: str) -> str:
    """Der sichtbare Text ohne Tags, Leerraum zusammengefasst.

    Inline-Tags (strong, a, span …) verschwinden ohne Leerzeichen, wie im
    Browser; alle anderen trennen wie ein Zeilenumbruch.
    """
    ohne_inline = re.sub(r"</?(?:strong|em|b|i|a|span|small)\b[^>]*>", "", html)
    return " ".join(re.sub(r"<[^>]+>", " ", ohne_inline).split())


def _wert(html: str) -> str | None:
    treffer = re.search(r'<div class="wert">([^<]*)</div>', html)
    return treffer.group(1) if treffer else None


class TestGrenzsatzAbgeschnitten:
    def test_seite_schneidet_den_grenzsatz_ab_wie_der_bmf(self, klient):
        """An der Zonengrenze (zvE 69.878 €) zeigt der BMF 41,99 %, nicht 42,00 %.

        Ebenso bei zvE 45.000 €: 33,38 % laut Faktenbasis. Kaufmännisch
        gerundet stand auf der Seite 42,00 % und 33,39 %.
        """
        text = _seite(klient, zve_ohne="64.878", gewinn_erwartet="5.000")
        assert "Grenzsteuersatz: <strong>41,99 %</strong>" in text
        text = _seite(klient, zve_ohne="31.670", gewinn_erwartet="13.330")
        assert "Grenzsteuersatz: <strong>33,38 %</strong>" in text


class TestZveOhneKlemme:
    def test_ohne_gehalt_mindert_der_pauschbetrag_den_gewinn(self):
        """Brutto 0: zvE ohne Gewerbe −36 €, mit 25.000 € Gewinn also 24.964 €.

        Vorher wurde das zvE ohne Gewerbe auf 0 gesetzt; der Pauschbetrag fiel
        weg, und die Rücklage lag bei 2.860 € statt 2.850 €.
        """
        from app.logik import steuertarif as st

        assert st.zve_aus_brutto(0, 2026).zve_euro == -36
        r = _schaetze(brutto_lohn_cent=0, gewinn_erwartet_cent=2500000, gewinn=0)
        assert r.zve_ohne_euro == -36 and r.zve_mit_euro == 24964
        assert r.ruecklage_cent == 285000
        assert r.zve_ohne_gezeigt_euro == 0

    def test_kleines_gehalt_abzuege_ueber_dem_lohn_mindern_den_gewinn(self):
        """Brutto 1.200 €: die Vorsorge übersteigt den Lohn; zvE mit 60.000 € Gewinn 59.703 €."""
        r = _schaetze(brutto_lohn_cent=120000, gewinn_erwartet_cent=6000000, gewinn=0)
        assert r.zve_ohne_euro == -297 and r.zve_mit_euro == 59703

    def test_zusammen_ohne_gehalt_beide_pauschbetraege(self):
        """Zusammen ohne Gehälter: 72 € Sonderausgaben-Pauschbetrag mindern den Gewinn."""
        r = _schaetze(brutto_lohn_cent=0, partner_brutto_cent=0, veranlagung="ZUSAMMEN",
                      gewinn_erwartet_cent=6000000, gewinn=0)
        assert r.zve_mit_euro == 59928

    def test_rechenweg_ohne_gehalt_ehrlich(self, klient):
        """Brutto 0: keine Abzugsliste mit lauter 0 €, sondern der Hinweis auf eigene KV-Beiträge.

        Die Vergleichstabelle zeigt ohne Gewerbe 0 € (nicht −36 €) und mit
        Gewerbe 24.964 €.
        """
        text = _klartext(_seite(klient, brutto_lohn="0", gewinn_erwartet="25.000"))
        assert "Vom Bruttogehalt 0 €" not in text
        assert "Ohne Gehalt geht vom Gewinn nur der Sonderausgaben-Pauschbetrag von 36 € ab" in text
        assert "Kranken- und Pflegeversicherung senken die Steuer ebenfalls" in text
        assert "geschätzt) 0 € 24.964 €" in text
        assert '<div class="wert">2.850 €</div>' in _seite(klient, brutto_lohn="0",
                                                          gewinn_erwartet="25.000")

    def test_naeherungstext_nennt_pkv_beamte_minijob(self, klient):
        """Mit Gehalt sagt der Rechenweg, für wen die Näherung nicht passt."""
        text = _klartext(_seite(klient, brutto_lohn="45.000", gewinn_erwartet="10.000"))
        assert "privater Krankenversicherung, für Beamte und bei Mini- oder Midijob" in text
        assert "mit dem zvE" not in text


class TestGemeinsamerHoechstbetrag:
    @pytest.mark.parametrize("brutto,partner,zve", [
        (3000000, 0, 22173), (7000000, 1500000, 65410), (2000000, 0, 14348),
    ])
    def test_zusammen_ein_hoechstbetrag_fuer_beide(self, brutto, partner, zve):
        """§ 10 Abs. 4 Satz 3/4 EStG: ein gemeinsamer Höchstbetrag (2 × 1.900 €) für beide.

        Die Günstigerprüfung läuft auf die Summe beider Ehegatten. Vorher je
        Person: 22.668 € statt 22.173 € (Partner ohne Gehalt) und 65.162 €
        statt 65.410 € (die Restvorsorge des Partners zählte trotz
        ausgeschöpftem gemeinsamem Höchstbetrag).
        """
        from app.logik import steuertarif as st

        assert st.zve_aus_brutto(brutto, 2026, partner_brutto_cent=partner,
                                 zusammen=True).zve_euro == zve

    def test_ruecklagen_mit_gemeinsamem_hoechstbetrag(self):
        """Die Rücklagen aus dem Befund: 22.580 € und 5.410 € (Kirchensteuer 9 %)."""
        for brutto, partner, gewinn, ruecklage in ((7000000, 1500000, 6000000, 2258000),
                                                   (3000000, 0, 2500000, 541000)):
            r = _schaetze(brutto_lohn_cent=brutto, partner_brutto_cent=partner,
                          veranlagung="ZUSAMMEN", kirchensteuer_prozent=9, gewinn=gewinn)
            assert r.ruecklage_cent == ruecklage

    def test_einzeln_unveraendert(self):
        """Für eine Person bleibt es bei der Formel der Faktenbasis (Referenzwerte 2026)."""
        from app.logik import steuertarif as st

        assert [st.zve_aus_brutto(b * 100, 2026).zve_euro for b in (30000, 70000)] == \
            [22704, 54691]


class TestPartnergehaltFehlt:
    def test_zusammen_ohne_partnergehalt_rechnet_nicht_still_mit_null(self, klient):
        """Zusammen, Partnergehalt leer: keine Zahl, sondern die Bitte um die Angabe.

        Vorher: 6.500 € (Splitting gegen einen Partner ohne Einkommen) ohne
        jeden Hinweis, bei Einzelveranlagung 8.540 €.
        """
        text = _seite(klient, brutto_lohn="45.000", veranlagung="ZUSAMMEN",
                      partner_brutto="", gewinn_erwartet="25.000")
        assert "So viel solltest du mindestens zurücklegen" not in text
        assert "fehlt noch das Bruttogehalt deines Partners oder deiner Partnerin" in text
        assert "Splitting mit einem Partner ohne Einkommen" in text
        assert "Steuerrücklage schätzen" in klient.get("/").text

    def test_partnergehalt_0_ist_eine_angabe(self, klient):
        """Mit 0 rechnet die Seite — dann ist es eine Angabe, keine Lücke."""
        text = _seite(klient, brutto_lohn="45.000", veranlagung="ZUSAMMEN",
                      partner_brutto="0", gewinn_erwartet="25.000")
        assert "So viel solltest du mindestens zurücklegen" in text
        assert "fehlt noch" not in text

    def test_zve_aus_dem_bescheid_braucht_kein_partnergehalt(self):
        """Das zvE aus dem Bescheid ist bei Zusammenveranlagung schon das gemeinsame."""
        from app.logik import steuerschaetzung as ss

        e = ss.Eingaben(jahr=2026, zve_ohne_cent=6000000, brutto_lohn_cent=4500000,
                        veranlagung="ZUSAMMEN")
        assert e.rechenbar
        r = _schaetze(zve_ohne_cent=6000000, brutto_lohn_cent=4500000, veranlagung="ZUSAMMEN",
                      gewinn=1000000)
        # Kein Vergleich aus dem Brutto mit einem Partner von 0 €.
        assert r.lohn is None and r.zve_ohne_euro == 60000


class TestEigenerSatzOhneGehalt:
    def test_eigener_satz_reicht_fuer_die_ruecklage(self, klient):
        """Gewinn 20.000 € × eigener Satz 30 % = 6.000 € — auch ohne Gehalt und zvE.

        Vorher blieb das Ergebnis leer („Trag dein Bruttogehalt ein …“), und
        die Übersicht zeigte nur „Steuerrücklage schätzen“.
        """
        antwort = klient.post("/steuer", data={"jahr": "2026", "gewinn_erwartet": "20.000",
                                               "satz_manuell": "30"}, follow_redirects=False)
        text = klient.get(antwort.headers["location"]).text
        assert '<div class="wert">6.000 €</div>' in text
        assert "Gerechnet wird mit deinem Satz; für einen berechneten Vergleich fehlt noch" in text
        assert "Einen berechneten Vergleich gibt es mit dem Bruttogehalt" in text
        # Kein Vergleich, der mit 0 € Steuer täuschte, kein Knopf „übernehmen“.
        assert "<h2>So wird gerechnet</h2>" not in text
        assert 'data-fuellen="satz_manuell"' not in text
        assert "Grenzsteuersatz:" not in text
        assert "chip" not in text.split("Abstufungen der Einkommensteuer")[1].split("</table>")[0]
        uebersicht = klient.get("/").text
        assert '<a href="/steuer?jahr=2026"><strong class="mono">6.000 €</strong></a>' in uebersicht

    def test_ohne_satz_und_ohne_gehalt_weiter_keine_zahl(self, klient):
        """Ohne eigenen Satz bleibt es bei der Bitte ums Gehalt."""
        text = _seite(klient, gewinn_erwartet="20.000")
        assert "So viel solltest du mindestens zurücklegen" not in text
        assert "fehlt noch das Bruttogehalt" in text

    def test_modul_nur_eigener_satz(self):
        """schaetze() ohne Gehalt, mit Satz: berechnet=False, kein berechneter Satz."""
        r = _schaetze(satz_manuell_promille=300, gewinn=2000000)
        assert not r.berechnet and r.satz_berechnet is None
        assert r.ruecklage_cent == 600000 and r.zone_mit is None


class TestFehlerseiteOhneAltesErgebnis:
    def test_422_zeigt_kein_ergebnis_der_alten_angaben(self, klient, tmp_path):
        """Nach einem Tippfehler kein „4.400 €“ aus den gespeicherten Angaben neben den neuen.

        Vorher stand rechts das Ergebnis der zuletzt gespeicherten Angaben —
        ohne die gerade gewählte Kirchensteuer und ohne den eigenen Satz —,
        dazu „Berechneten Satz übernehmen (33,0 %)“.
        """
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1500000, 167000)
        assert "So viel solltest du mindestens zurücklegen" in _seite(klient,
                                                                      brutto_lohn="48.000")
        antwort = klient.post("/steuer", data={
            "jahr": "2026", "brutto_lohn": "abc", "kirchensteuer": "9",
            "gewinn_erwartet": "20.000", "satz_manuell": "30"})
        assert antwort.status_code == 422
        text = antwort.text
        assert "Bruttogehalt: „abc“ ist kein Betrag." in text
        assert "So viel solltest du mindestens zurücklegen" not in text
        assert "Bitte erst die Angabe korrigieren" in text
        assert 'data-fuellen="satz_manuell"' not in text
        assert "dann gilt der berechnete Satz (" not in text
        assert 'value="abc"' in text and 'value="30"' in text

    def test_422_stufentabelle_passt_zum_text(self, klient):
        """Zusammen gewählt, Fehler im Partnerfeld: Text „doppelt“ und doppelte Grenzen.

        Vorher: der Text sprach vom Splitting, die Tabelle zeigte die
        Einzelgrenzen der gespeicherten Einzelveranlagung (bis 12.348 €).
        """
        _seite(klient, brutto_lohn="48.000")
        antwort = klient.post("/steuer", data={
            "jahr": "2026", "brutto_lohn": "48.000", "veranlagung": "ZUSAMMEN",
            "partner_brutto": "30.000x"})
        assert antwort.status_code == 422
        tabelle = antwort.text.split('<table class="stufen">')[1].split("</table>")[0]
        assert "Bei Zusammenveranlagung gelten die Grenzen" in antwort.text
        assert "bis 24.697 €" in tabelle and "bis 12.348 €" not in tabelle


class TestFehlerfeldMarkiert:
    @pytest.mark.parametrize("feld,wert", [
        ("vorauszahlungen", "1.000,5x"), ("brutto_lohn", "abc"), ("hebesatz", "150"),
        ("satz_manuell", "120"), ("partner_brutto", "30.000x"),
    ])
    def test_422_markiert_und_fokussiert_das_feld(self, klient, feld, wert):
        """Das abgewiesene Feld trägt aria-invalid, autofocus und die Meldung direkt darunter.

        Vorher stand die Meldung nur oben; auf 390 px lag das Feld rund
        1.000 px tiefer, ohne Markierung und ohne Fokus.
        """
        daten = {"jahr": "2026", "brutto_lohn": "48.000", feld: wert}
        antwort = klient.post("/steuer", data=daten)
        assert antwort.status_code == 422
        text = antwort.text
        eingabe = re.search(rf'<input name="{feld}"[^>]*>', text).group(0)
        assert 'aria-invalid="true"' in eingabe and "autofocus" in eingabe
        assert f'aria-describedby="feldfehler-{feld}"' in eingabe
        assert f'<span class="feldfehler" id="feldfehler-{feld}">' in text
        # Nur dieses eine Feld ist markiert.
        assert text.count('aria-invalid="true"') == 1

    def test_modul_nennt_das_feld(self):
        """pruefe() nennt das Formularfeld des Fehlers (Eingabefehler.feld)."""
        from app.logik import steuerschaetzung as ss

        with pytest.raises(ss.Eingabefehler) as fehler:
            ss.pruefe(2026, {"vorauszahlungen": "1.000,5x"})
        assert fehler.value.feld == "vorauszahlungen"


def _satzzeile(html: str) -> str:
    return _klartext(re.search(r'<p class="satzzeile">(.*?)</p>', html, re.S).group(1))


class TestGrenzsatzText:
    def test_bis_410_euro_kostet_der_naechste_euro_nichts(self, klient):
        """Gewinn 300 € neben dem Gehalt: kein „so viel kostet jeder weitere Euro“.

        Bis 410 € bleibt ein weiterer Euro wegen des Härteausgleichs frei.
        """
        zeile = _satzzeile(_seite(klient, brutto_lohn="45.000", gewinn_erwartet="300"))
        assert "so viel kostet jeder weitere Euro" not in zeile
        assert "kostet ein weiterer Euro aber nichts (Härteausgleich" in zeile

    def test_zwischen_410_und_820_euro_doppelt(self, klient):
        """Gewinn 600 €: jeder Euro erhöht das zvE um 2 € — etwa 60 % statt 29,95 %."""
        zeile = _satzzeile(_seite(klient, brutto_lohn="45.000", gewinn_erwartet="600"))
        assert "um 2 € und kostet etwa 60 %" in zeile

    def test_zuschlaege_werden_genannt(self, klient):
        """Soli, Kirchen- und Gewerbesteuer: der Durchschnitt (44,3 %) liegt über 42 %.

        Die Klammer sagt jetzt, dass der Grenzsatz nur die Einkommensteuer ist
        und die Zuschläge den nächsten Euro verteuern.
        """
        zeile = _satzzeile(_seite(klient, brutto_lohn="48.000", gewinn_erwartet="60.000",
                                  kirchensteuer="9", hebesatz="450"))
        assert "Nur die Einkommensteuer auf den nächsten Euro; mit Soli, Kirchensteuer und " \
               "Gewerbesteuer kostet er mehr." in zeile

    def test_ohne_zuschlaege_nur_einkommensteuer(self, klient):
        """Ohne Soli, Kirchen- und Gewerbesteuer bleibt es beim schlichten Satz."""
        zeile = _satzzeile(_seite(klient, brutto_lohn="45.000", gewinn_erwartet="10.000"))
        assert zeile.endswith("Nur die Einkommensteuer auf den nächsten Euro.")


def _vorschlag(html: str) -> str | None:
    treffer = re.search(r'data-fuellen="satz_manuell"\s+data-wert="([^"]*)"', html)
    return treffer.group(1) if treffer else None


class TestSatzUebernehmen:
    @pytest.mark.parametrize("brutto,gewinn", [("120.000", "60.000"), ("45.000", "25.000"),
                                               ("48.000", "13.330")])
    def test_uebernommener_satz_senkt_die_ruecklage_nie(self, klient, brutto, gewinn):
        """Wer den berechneten Satz übernimmt, liegt nie unter dem berechneten „mindestens“.

        Vorher wurde aus 26.840 € mit „44,7“ (kaufmännisch gerundet) 26.820 €.
        """
        def euro(text):
            return int(_wert(text).replace(".", "").removesuffix(" €"))

        text = _seite(klient, brutto_lohn=brutto, gewinn_erwartet=gewinn)
        berechnet = euro(text)
        vorschlag = _vorschlag(text)
        assert "auf 0,1 % aufgerundet" in text
        uebernommen = euro(_seite(klient, brutto_lohn=brutto, gewinn_erwartet=gewinn,
                                  satz_manuell=vorschlag))
        # Höchstens 0,1 % des Gewinns mehr, dazu die Rundung auf 10 €.
        assert berechnet <= uebernommen <= berechnet + 70
        if brutto == "120.000":
            assert vorschlag == "44,8" and berechnet == 26840

    def test_kein_vorschlag_ueber_100_prozent(self, klient):
        """1 € Gewinn, zvE 74.980 €, Kirchensteuer: 121 % — kein Knopf, den das Feld abweist."""
        text = _seite(klient, brutto_lohn="0", zve_ohne="74.980", kirchensteuer="9",
                      gewinn_erwartet="1")
        assert "Das sind <strong>121,0 %</strong>" in text
        assert _vorschlag(text) is None


def _meldungen(html: str) -> list[str]:
    return [_klartext(m) for m in
            re.findall(r'<div class="meldung erfolg">(.*?)</div>', html, re.S)]


def _tfoot(html: str) -> str:
    return _klartext(re.search(r"<tfoot>(.*?)</tfoot>", html, re.S).group(1))


class TestVerlustUndVorauszahlungen:
    def test_verlust_ohne_ersparnis_ohne_lohnsteuertext(self, klient):
        """Brutto 0, Verlust: kein „um etwa 0 €“ und keine Lohnsteuer, die es nicht gibt."""
        meldung = " ".join(_meldungen(_seite(klient, brutto_lohn="0",
                                             gewinn_erwartet="-5.000")))
        assert "Verlust von 5.000 €" in meldung
        assert "etwa 0 €" not in meldung and "Lohnsteuer" not in meldung
        assert "auf dein übriges Einkommen fällt keine Einkommensteuer an" in meldung

    def test_verlust_mit_vorauszahlungen_nennt_die_erstattung(self, klient):
        """Verlust 2.000 € und 1.200 € Vorauszahlungen: zusammen etwa 1.790 € zurück.

        Vorher erschien nur die Ersparnis von 590 €; die Vorauszahlungen
        wurden verschwiegen, weil der Zweig für die Erstattung nie erreicht
        wurde. Die Tabelle endet jetzt mit „Voraussichtlich zurück“.
        """
        text = _seite(klient, brutto_lohn="45.000", gewinn_erwartet="-2.000",
                      vorauszahlungen="1.200")
        meldung = " ".join(_meldungen(text))
        assert "um etwa <strong>" not in meldung  # Klartext, kein HTML
        assert "senkt die Steuer auf dein übriges Einkommen um etwa 590 €" in meldung
        assert "die Lohnsteuer war dann zu hoch" in meldung
        assert "Die Vorauszahlungen von 1.200 € kommen voraussichtlich ebenfalls zurück — " \
               "zusammen etwa 1.790 €." in meldung
        assert "Voraussichtlich zurück 1.790,00 €" in _tfoot(text)
        assert "voraussichtlich etwa 1.790 € zurück" in _klartext(klient.get("/").text)

    def test_leere_euer_verspricht_keine_erstattung(self, klient, monkeypatch):
        """Jahresanfang: EÜR leer, Vorauszahlungen 1.200 € — erst der Hinweis auf den Gewinn.

        Vorher: „Die Vorauszahlungen liegen um etwa 1.200 € über der Schätzung
        — voraussichtlich gibt es das zurück“, und der Hinweis „noch kein
        Gewinn in der EÜR“ fehlte.
        """
        text = _seite(klient, brutto_lohn="45.000", vorauszahlungen="1.200")
        assert "steht noch kein Gewinn in der EÜR" in text
        assert "über der Schätzung" not in text
        assert "noch kein Gewinn in der EÜR" in _klartext(klient.get("/").text)

    def test_teilweise_vorausgezahlt_nachrechenbar(self, klient, tmp_path):
        """4.393 € Steuer, 1.005 € vorausgezahlt: die Unterzeile rechnet nach (= 3.388 €).

        Vorher: „Steuer 4.400 €, davon schon vorausgezahlt 1.005 €“ über einer
        Großzahl von 3.390 € — nachgerechnet 3.395 €. Die Tabelle hat jetzt
        eine Zeile „Noch offen“.
        """
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1500000, 167000)
        text = _seite(klient, brutto_lohn="48.000", vorauszahlungen="1.005")
        ruecklage = _klartext(re.search(r'<div class="ruecklage">(.*?)</div>\s*</div>',
                                        text, re.S).group(1))
        assert "3.390 €" in ruecklage
        assert "Steuer auf den Gewinn 4.393,00 € minus schon vorausgezahlt 1.005,00 € = " \
               "3.388,00 €, auf volle 10 € aufgerundet" in ruecklage
        assert _tfoot(text).endswith("Noch offen 3.388,00 €")
        assert "die Vorauszahlungen gehören dazu" in text

    def test_ueberzahlt_kein_davon_mehr_als_das_ganze(self, klient, tmp_path):
        """6.000 € vorausgezahlt bei 4.393 € Steuer: „decken“, nicht „davon 6.000 €“."""
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1500000, 167000)
        text = _seite(klient, brutto_lohn="48.000", vorauszahlungen="6.000")
        assert "davon schon vorausgezahlt" not in text
        assert "Die Vorauszahlungen von 6.000 € decken die geschätzte Steuer auf den Gewinn " \
               "von 4.393 €." in _klartext(text)
        assert _tfoot(text).endswith("Voraussichtlich zurück 1.607,00 €")


class TestErwartetUnterEuer:
    def test_veralteter_erwartungswert_wird_gemeldet(self, klient, tmp_path):
        """EÜR bis heute 13.330 €, erwartet 8.000 €: Warnung mit den 4.400 € laut EÜR.

        Vorher stand still „mindestens 2.570 €“ auf Seite und Übersicht,
        obwohl allein der schon erzielte Gewinn 4.400 € verlangt.
        """
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1500000, 167000)
        text = _seite(klient, brutto_lohn="48.000", gewinn_erwartet="8.000")
        assert _wert(text) == "2.570 €"  # gerechnet wird weiter mit dem erwarteten Wert
        klar = _klartext(text)
        assert "Dein erwarteter Jahresgewinn (8.000 €) liegt unter dem Gewinn laut EÜR bis " \
               "heute (13.330 €)" in klar
        assert "allein der Gewinn laut EÜR ergäbe 4.400 €" in klar
        uebersicht = _klartext(klient.get("/").text)
        assert "der erwartete Jahresgewinn liegt unter dem Gewinn laut EÜR, danach wären es " \
               "4.400 €" in uebersicht

    def test_hoeherer_erwartungswert_ohne_warnung(self, klient, tmp_path):
        """Erwartet mehr als bisher (der Normalfall im laufenden Jahr): keine Warnung."""
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1500000, 167000)
        text = _seite(klient, brutto_lohn="48.000", gewinn_erwartet="20.000")
        assert "liegt unter dem Gewinn laut EÜR" not in text
        assert _schaetze(brutto_lohn_cent=4800000, gewinn=1333000,
                         gewinn_erwartet_cent=2000000).offen_laut_euer_cent is None


class TestHochrechnungNachMonaten:
    def test_nach_monaten_wie_entscheidung_6(self):
        """Bisheriger Gewinn ÷ vergangene Monate × 12 — nicht nach Tagen.

        7.500 € bis Oktober: 9.000 € (nach Tagen waren es 9.190 €).
        """
        from datetime import date

        from app.logik import steuerschaetzung as ss

        assert ss.hochrechnung(750000, 2026, date(2026, 10, 25)).jahr_cent == 900000

    def test_am_jahresanfang_nicht_absurd(self):
        """1.500 € am 2. Januar: 18.000 €, nicht 273.750 € (am 1. Januar 547.500 €)."""
        from datetime import date

        from app.logik import steuerschaetzung as ss

        for tag in (1, 2, 15, 31):
            assert ss.hochrechnung(150000, 2027, date(2027, 1, tag)).jahr_cent == 1800000
        assert ss.hochrechnung(150000, 2027, date(2027, 12, 2)) is None

    def test_seite_nennt_die_rechnung(self, klient, tmp_path):
        """Die Seite sagt, womit hochgerechnet wurde: „(10 Monate × 12)“."""
        _einnahme_und_ausgabe(tmp_path / "steuer.sqlite", 1000000, 250000)
        text = _klartext(_seite(klient, brutto_lohn="45.000"))
        assert "aufs ganze Jahr etwa 9.000 € (10 Monate × 12)" in text


class TestUebersichtJahreswechsel:
    def test_neues_jahr_verdraengt_die_offene_ruecklage_des_vorjahres_nicht(
            self, klient, monkeypatch):
        """Angaben 2026 (4.870 €) und 2027 (nur Gehalt): am 1.1.2027 stehen beide da.

        Vorher zeigte die Übersicht am 01.01.2027 nur noch „Steuerrücklage
        2027 0 € zurücklegen“, die 4.870 € für 2026 waren verschwunden.
        """
        _seite(klient, jahr="2026", brutto_lohn="45.000", gewinn_erwartet="15.000")
        _seite(klient, jahr="2027", brutto_lohn="47.000")
        tag = {"wert": "2026-12-31"}
        monkeypatch.setattr(db, "heute", lambda: tag["wert"])
        silvester = _klartext(klient.get("/").text)
        assert "Steuerrücklage 2026 4.870 € zurücklegen" in silvester
        assert "Steuerrücklage 2027" not in silvester
        tag["wert"] = "2027-01-01"
        neujahr = _klartext(klient.get("/").text)
        assert "Steuerrücklage 2027 0 € zurücklegen" in neujahr
        assert "Steuerrücklage 2026 4.870 € zurücklegen" in neujahr

    def test_erledigtes_vorjahr_erscheint_nicht(self, klient, monkeypatch):
        """Im Vorjahr nichts mehr offen (Vorauszahlungen decken es): nur eine Zeile."""
        _seite(klient, jahr="2026", brutto_lohn="45.000", gewinn_erwartet="15.000",
               vorauszahlungen="6.000")
        _seite(klient, jahr="2027", brutto_lohn="47.000")
        monkeypatch.setattr(db, "heute", lambda: "2027-01-01")
        text = _klartext(klient.get("/").text)
        assert "Steuerrücklage 2027" in text and "Steuerrücklage 2026" not in text

    def test_neues_jahr_ohne_rechenbare_angaben(self, klient, monkeypatch):
        """2027 nur mit Hebesatz (keine Schätzung): das Vorjahr bleibt, kein bloßer Verweis."""
        _seite(klient, jahr="2026", brutto_lohn="45.000", gewinn_erwartet="15.000")
        _seite(klient, jahr="2027", hebesatz="400")
        monkeypatch.setattr(db, "heute", lambda: "2027-01-01")
        text = _klartext(klient.get("/").text)
        assert "Steuerrücklage 2026 4.870 € zurücklegen" in text
        assert "Steuerrücklage schätzen" not in text


class TestUebersichtEntwurf:
    def test_mit_tarifentwurf_gerechnet_steht_auf_der_uebersicht(self, klient, monkeypatch):
        """Mit dem Entwurf 2027 gerechnet: die Übersicht sagt „Tarifentwurf, vorläufig“.

        Vorher: „Steuerrücklage 2027 11.650 € zurücklegen“ ohne jeden Hinweis,
        mit geltendem Tarif wären es 11.690 €.
        """
        _seite(klient, jahr="2027", brutto_lohn="60.000", gewinn_erwartet="30.000",
               tarif_entwurf="1")
        monkeypatch.setattr(db, "heute", lambda: "2027-03-01")
        # Die Ruecklage ist eine Kachel (div.ruecklage) mit drei Zeilen; die
        # aeussere schliesst als einzige am Zeilenanfang.
        kachel = r'<div class="ruecklage">(.*?)\n</div>'
        zeile = _klartext(re.search(kachel, klient.get("/").text, re.S).group(1))
        assert zeile.startswith("Steuerrücklage 2027 · Tarifentwurf, vorläufig 11.650 €")
        _seite(klient, jahr="2027", brutto_lohn="60.000", gewinn_erwartet="30.000")
        zeile = _klartext(re.search(kachel, klient.get("/").text, re.S).group(1))
        assert "Entwurf" not in zeile and "11.690 €" in zeile


class TestExcelKlartext:
    def test_veranlagung_nur_in_ihrer_spalte_uebersetzt(self, klient, tmp_path):
        """Ein Kunde namens „ZUSAMMEN“ bleibt im Excel so; die Veranlagung wird Klartext.

        Vorher stand EINZEL/ZUSAMMEN im gemeinsamen KLARTEXT-Topf, der jede
        Textzelle jeder Tabelle übersetzt — aus dem Kunden wurde
        „Zusammenveranlagung“.
        """
        from io import BytesIO

        from openpyxl import load_workbook

        verbindung = db.oeffne(tmp_path / "steuer.sqlite")
        verbindung.execute(
            "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
            " VALUES ('K-0001', 'ZUSAMMEN', 'Weg 1', '10115', 'Berlin', '2026-01-01')")
        verbindung.commit()
        verbindung.close()
        _seite(klient, brutto_lohn="45.000", partner_brutto="0", veranlagung="ZUSAMMEN")
        mappe = load_workbook(BytesIO(klient.get("/backup/excel").content))
        kunden = mappe["Kunden"]
        spalte = [z.value for z in kunden[1]].index("Name")
        assert [z[spalte].value for z in kunden.iter_rows(min_row=2)] == ["ZUSAMMEN"]
        angaben = mappe["Steuerrücklage (Angaben)"]
        spalte = [z.value for z in angaben[1]].index("Veranlagung")
        assert [z[spalte].value for z in angaben.iter_rows(min_row=2)] == ["Zusammenveranlagung"]


class TestErsatzjahrOhneAngaben:
    def test_hinweis_steht_auch_ohne_gespeicherte_angaben(self, klient):
        """2023 ohne Angaben: bei den Abstufungen steht, dass der Tarif 2024 verwendet wird.

        Vorher erschien der Hinweis erst nach dem Speichern (er steckte nur
        in den Hinweisen des Ergebnisses); die Tabelle hieß „Abstufungen der
        Einkommensteuer 2024“ auf der Seite „Steuerrücklage 2023“.
        """
        text = klient.get("/steuer?jahr=2023").text
        assert "Abstufungen der Einkommensteuer 2024" in text
        assert "Für 2023 ist kein Tarif hinterlegt; gerechnet wird mit dem von 2024" in text

    def test_kuenftiges_jahr_ohne_angaben(self, klient, monkeypatch):
        """Ab 2027 lässt sich 2028 wählen; auch dort steht der Ersatzjahr-Hinweis."""
        monkeypatch.setattr(db, "heute", lambda: "2027-03-01")
        text = klient.get("/steuer?jahr=2028").text
        assert "Tarif 2026 verwendet — für 2028 noch nicht hinterlegt." in text


class TestDruck:
    def test_tabellenzeilen_brechen_nicht_ueber_den_seitenwechsel(self):
        """Im Druck: break-inside: avoid für die Zeilen der drei Steuertabellen.

        Vorher endete Seite 2 mit „70.601 – 249.999 € … 42 %“, und Seite 3
        begann unter dem wiederholten Kopf allein mit dem Chip „mit Gewerbe“.
        """
        from pathlib import Path

        css = (Path(__file__).resolve().parent.parent / "app" / "static" / "stil.css").read_text()
        druck = "".join(block.split("\n}\n")[0] for block in css.split("@media print {")[1:])
        regel = re.search(r"table\.stufen tr,[^{]*\{([^}]*)\}", druck)
        assert regel and "break-inside: avoid" in regel.group(1)


class TestTexte:
    def test_negative_betraege_mit_echtem_minus(self, klient):
        """Verlust: „−5.000 €“ und „−1.448,00 €“ mit U+2212, wie die Vorlagenwerte daneben.

        Vorher standen errechnete Beträge mit Bindestrich („-1.448,00 €“)
        neben Vorlagenwerten mit echtem Minus („−20.000,00 €“).
        """
        text = _klartext(_seite(klient, brutto_lohn="45.000", gewinn_erwartet="-5.000",
                                vorauszahlungen="1.000"))
        assert re.findall(r"(?<![\w-])-\d", text) == []
        assert "Steuer auf den Gewinn −1.448,00 €" in text
        assert "Gewinn 2026: −5.000,00 € erwartet" in text
        # Im Eingabefeld bleibt der Bindestrich: nur den liest das Formular.
        assert 'value="-5.000,00"' in _seite(klient, brutto_lohn="45.000",
                                             gewinn_erwartet="-5.000")

    def test_zve_wird_ausgeschrieben(self, klient):
        """Kein unerklärtes „zvE“ in Nutzertexten (Hinweis 2027, Rechenweg, Excel-Kopf)."""
        from app.logik import steuertarif as st

        assert "zvE" not in st.zve_aus_brutto(5000000, 2027).hinweis
        text = _klartext(_seite(klient, jahr="2027", brutto_lohn="45.000",
                                gewinn_erwartet="10.000"))
        assert not re.search(r"\bzvE\b", text)
        from app.logik.export_excel import KOPF_AUSNAHMEN

        assert KOPF_AUSNAHMEN["zve_ohne_cent"] == "Zu versteuerndes Einkommen ohne Gewerbe"

    def test_hebesatz_meldung_mit_prozent(self, klient):
        """„Hebesatz: 1500 % ist zu hoch (höchstens 1000 %).“ — wie die Gegenmeldung mit %."""
        antwort = klient.post("/steuer", data={"jahr": "2026", "brutto_lohn": "45.000",
                                               "hebesatz": "1500"})
        assert antwort.status_code == 422
        assert "Hebesatz: 1500 % ist zu hoch (höchstens 1000 %)." in antwort.text


class TestLeerzustandBeispiel:
    @pytest.mark.parametrize("jahr,neben", [("2024", "3.370"), ("2025", "3.330"),
                                            ("2026", "3.300"), ("2027", "3.300")])
    def test_beispiel_aus_der_eigenen_rechnung(self, klient, jahr, neben):
        """Das Beispiel stimmt mit der Rechnung des Tools überein, je Jahr.

        Vorher stand für alle Jahre „fast keine Steuer, neben 50.000 € Gehalt
        rund 3.500 €“; das Tool selbst rechnet 3.293 € (2026), ohne Gehalt 0 €.
        """
        text = _klartext(klient.get(f"/steuer?jahr={jahr}").text)
        assert f"10.000 € Gewinn kosten {jahr} ohne weiteres Einkommen keine " \
               f"Einkommensteuer, neben 50.000 € Gehalt rund {neben} €." in text
        assert "3.500" not in text


class TestAngabenLoeschen:
    def test_versehentliches_jahr_laesst_sich_loeschen(self, klient):
        """Angaben für 2000: gelöscht, mit Journal, und das Jahr verschwindet aus der Auswahl.

        Vorher gab es keinen Weg; 2000 blieb für immer in der Jahresauswahl.
        """
        _seite(klient, jahr="2000", brutto_lohn="1")
        assert '<option value="2000"' in klient.get("/steuer").text
        seite = klient.get("/steuer?jahr=2000").text
        assert 'action="/steuer/loeschen"' in seite
        antwort = klient.post("/steuer/loeschen", data={"jahr": "2000"},
                              follow_redirects=False)
        assert antwort.status_code == 303
        assert "jahr=2000" not in antwort.headers["location"]
        text = klient.get(antwort.headers["location"]).text
        assert "Angaben für 2000 gelöscht." in text
        assert '<option value="2000"' not in text
        journal = klient.get("/journal?objekttyp=steuerschaetzung&aktion=LOESCHEN").text
        assert "LOESCHEN" in journal and "steuerschaetzung" in journal

    def test_loeschen_im_modul_mit_journal(self, tmp_path):
        """loeschen(): die Zeile ist weg, das Journal hält die alten Werte fest."""
        from app.logik import steuerschaetzung as ss

        conn = db.oeffne(tmp_path / "s.sqlite")
        try:
            ss.speichern(conn, ss.Eingaben(jahr=2026, brutto_lohn_cent=4500000))
            assert ss.loeschen(conn, 2026) and ss.laden(conn, 2026) is None
            assert not ss.loeschen(conn, 2026)
            eintrag = conn.execute(
                "SELECT wert_alt FROM journal WHERE aktion = 'LOESCHEN'"
                " AND objekttyp = 'steuerschaetzung'").fetchone()
            assert eintrag and "4500000" in eintrag[0]
        finally:
            conn.close()

    def test_laufendes_jahr_bleibt_waehlbar(self, klient):
        """Das laufende Jahr bleibt nach dem Löschen in der Auswahl und wird gezeigt."""
        _seite(klient, brutto_lohn="45.000")
        antwort = klient.post("/steuer/loeschen", data={"jahr": "2026"},
                              follow_redirects=False)
        assert antwort.headers["location"].startswith("/steuer?jahr=2026&")
        text = klient.get(antwort.headers["location"]).text
        assert "Trag dein Bruttogehalt ein" in text and 'action="/steuer/loeschen"' not in text


class TestFehlerfeldMitFokus:
    def test_rote_markierung_auch_mit_fokus(self):
        """Das abgewiesene Feld hat sofort den Fokus — der Fokusrahmen ist dann rot, nicht grün."""
        from pathlib import Path

        css = (Path(__file__).resolve().parent.parent / "app" / "static" / "stil.css").read_text()
        regel = re.search(r'\.steuer-formular \[aria-invalid="true"\]:focus \{([^}]*)\}', css)
        assert regel and "var(--fehler)" in regel.group(1)

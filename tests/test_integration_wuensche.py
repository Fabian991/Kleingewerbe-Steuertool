"""Wechselwirkungen zwischen den sechs Wunschpaketen (Schema 21 bis 25).

Jedes Paket hat seine eigenen Tests. Hier steht, was erst beim
Zusammenfuehren auffaellt: eine Funktion aus einem Paket, die eine Spalte
oder Regel aus einem anderen nicht kennt. Jeder Test nennt die beiden
Pakete, deren Zusammenspiel er prueft.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import db, pdf
from app.logik import festschreiben as fs
from app.logik import rechnungsablauf as ablauf

HEUTE = db.heute()


@pytest.fixture
def schnelles_pdf(monkeypatch):
    """Ein PDF in Millisekunden: geprueft wird der Ablauf, nicht das Layout."""

    def rendere(daten: dict, ziel: Path) -> str:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"%PDF-1.7\n" + json.dumps(daten, ensure_ascii=False).encode())
        return pdf.pruefsumme(ziel)

    monkeypatch.setattr(pdf, "rendere", rendere)


@pytest.fixture
def kunde(conn, betrieb, kunde_id):
    conn.execute("UPDATE betrieb SET beginn_am = '2020-01-01' WHERE id = 1")
    conn.execute("UPDATE kunde SET satz_cent = 9000 WHERE id = ?", (kunde_id,))
    return kunde_id


def _zeit(conn, kunde_id, minuten=50, titel="Beratung") -> int:
    return conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten, abrechnungsmodus,"
        " erfasst_am) VALUES (?, ?, ?, ?, 'STUNDEN', ?)",
        (kunde_id, titel, HEUTE, minuten, db.jetzt_utc()),
    ).lastrowid


# ================================================ Rundung (A) x Korrigieren (C)

@pytest.mark.usefixtures("schnelles_pdf")
class TestKorrigierenBehaeltRundung:
    """Der Ersatzentwurf rechnet mit der Regel, mit der das Original gerechnet hat.

    Ohne die eingefrorene Regel fiele der Entwurf auf die heutige Regel des
    Kunden zurueck: die naechste Neuberechnung (Gruppierung, Text einer Zeit,
    Kundensatz) rechnete still anders, und die Seite nannte die heutige Regel
    "Regel vom Anlegen".
    """

    def test_ersatzentwurf_traegt_die_regel_des_originals(self, conn, kunde, tmp_path):
        zeit = _zeit(conn, kunde)
        rid = fs.entwurf_anlegen(conn, kunde, [zeit], datum=HEUTE)
        fs.festschreiben(conn, rid, belegordner=tmp_path / "daten")
        original = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert (original["rundung_modus"], original["rundung_intervall_min"]) == ("AUFWAERTS", 15)
        assert original["brutto_cent"] == 9000  # 50 Minuten, auf 1:00 aufgerundet

        # Inzwischen rechnet der Kunde minutengenau.
        conn.execute("UPDATE kunde SET rundung_modus = 'KEINE', rundung_intervall_min = 1"
                     " WHERE id = ?", (kunde,))
        k = ablauf.korrigieren(conn, rid, "Tippfehler im Betreff", belegordner=tmp_path / "daten")

        entwurf = conn.execute("SELECT * FROM rechnung WHERE id = ?", (k.entwurf_id,)).fetchone()
        assert (entwurf["rundung_modus"], entwurf["rundung_intervall_min"]) == ("AUFWAERTS", 15)
        regel = fs.regel_des_entwurfs(conn, entwurf)
        assert (regel.modus, regel.intervall_min) == ("AUFWAERTS", 15)

        # Eine Neuberechnung (gleiche Gruppierung) aendert den Betrag nicht.
        fs.gruppierung_wechseln(conn, k.entwurf_id, entwurf["gruppierung"])
        assert conn.execute("SELECT brutto_cent FROM rechnung WHERE id = ?",
                            (k.entwurf_id,)).fetchone()[0] == 9000
        # Erst die ausdrueckliche Uebernahme rechnet mit der Kundenregel.
        assert fs.rundung_uebernehmen(conn, k.entwurf_id) is True
        # 50 Minuten minutengenau: 0,83 Std. zu 90 Euro.
        assert conn.execute("SELECT brutto_cent FROM rechnung WHERE id = ?",
                            (k.entwurf_id,)).fetchone()[0] == 7470


# ======================================= Zuruecknehmen (C) und Vorschau (B)

@pytest.mark.usefixtures("schnelles_pdf")
class TestZuruecknehmenUndVorschau:
    """Eine zurueckgenommene Rechnung ist wieder ein Entwurf: sie hat wieder
    eine Vorschau (ohne Nummer), und ihre Rundungsregel bleibt. Eine ganz
    geloeschte hat keine -- die Vorschau-Route antwortet mit einer Meldung."""

    def test_zurueck_zum_entwurf_hat_vorschau_und_regel(self, conn, kunde, tmp_path):
        rid = fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde)], datum=HEUTE)
        fs.festschreiben(conn, rid, belegordner=tmp_path / "daten")
        ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                             datenordner=tmp_path / "daten")
        zeile = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rid,)).fetchone()
        assert (zeile["status"], zeile["nummer"]) == ("ENTWURF", None)
        assert (zeile["rundung_modus"], zeile["rundung_intervall_min"]) == ("AUFWAERTS", 15)
        daten = fs.vorschau_daten(conn, rid)
        assert daten["vorschau"] is True and daten["r"]["nummer"] is None
        assert daten["ersetzt"] is None

    def test_ersatzentwurf_zeigt_in_der_vorschau_den_verweis(self, conn, kunde, tmp_path):
        rid = fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde)], datum=HEUTE)
        fs.festschreiben(conn, rid, belegordner=tmp_path / "daten")
        k = ablauf.korrigieren(conn, rid, "falscher Betreff", belegordner=tmp_path / "daten")
        daten = fs.vorschau_daten(conn, k.entwurf_id)
        assert daten["ersetzt"]["nummer"] == k.original_nummer
        assert daten["ersetzt"]["storno_nummer"] == k.storno.nummer


def test_vorschau_einer_ganz_geloeschten_rechnung(tmp_path, monkeypatch):
    """Nach "Ganz löschen" zeigt ein offener Tab noch auf die Vorschau: die
    Route leitet mit einer Meldung weiter statt eines Serverfehlers."""
    from fastapi.testclient import TestClient

    from app.main import erzeuge_app

    def rendere(daten: dict, ziel: Path) -> str:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"%PDF-1.7\n")
        return pdf.pruefsumme(ziel)

    monkeypatch.setattr(pdf, "rendere", rendere)
    pfad = tmp_path / "steuer.sqlite"
    with TestClient(erzeuge_app(pfad)) as klient:
        conn = db.oeffne(pfad)
        try:
            conn.execute(
                "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
                " standardsatz_cent, beginn_am) VALUES (1, 'Beispiel Werbung',"
                " 'Musterstraße 1', '12345', 'Musterstadt', '12/345/67890', 8000,"
                " '2020-01-01')")
            kid = conn.execute(
                "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, satz_cent,"
                " angelegt_am) VALUES ('K-0001', 'Beispiel GmbH', 'Hauptstraße 5', '10115',"
                " 'Berlin', 9000, ?)", (HEUTE,)).lastrowid
            rid = fs.entwurf_anlegen(conn, kid, [_zeit(conn, kid)], datum=HEUTE)
            fs.festschreiben(conn, rid, belegordner=tmp_path)
            ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                                 variante=ablauf.GANZ_LOESCHEN, datenordner=tmp_path)
        finally:
            conn.close()
        antwort = klient.get(f"/rechnung/{rid}/vorschau.pdf", follow_redirects=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"].startswith("/rechnungen")


# ======================================= Kunde loeschen (D) und Pause (A)

def test_kunde_loeschen_nimmt_die_arbeitsabschnitte_mit(conn, kunde):
    """Kunde loeschen (D) loescht seine offenen Zeiten; deren Abschnitte (A)
    gehen per ON DELETE CASCADE mit, die Sperr-Trigger der Abschnitte lassen
    das zu (die Zeit liegt auf keiner festgeschriebenen Rechnung)."""
    from app.logik import kunden

    zid = conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, start_zeit, end_zeit, dauer_minuten,"
        " abrechnungsmodus, erfasst_am) VALUES (?, 'Kampagne', ?, '09:00', '10:30', 75,"
        " 'STUNDEN', ?)", (kunde, HEUTE, db.jetzt_utc())).lastrowid
    for beginn, ende in (("08:00:00", "08:45:00"), ("09:00:00", "09:30:00")):
        conn.execute("INSERT INTO zeit_abschnitt (zeiteintrag_id, beginn_utc, ende_utc)"
                     " VALUES (?, ?, ?)",
                     (zid, f"{HEUTE}T{beginn}+00:00", f"{HEUTE}T{ende}+00:00"))
    assert kunden.loeschen(conn, kunde) is not None
    assert conn.execute("SELECT COUNT(*) FROM zeit_abschnitt").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kunde WHERE id = ?", (kunde,)).fetchone()[0] == 0


# ================================ Journalspiegel (F) und Inhalte aus C und D

@pytest.mark.usefixtures("schnelles_pdf")
def test_journalspiegel_kuerzt_den_inhalt_einer_zurueckgenommenen_rechnung_nicht(
        conn, kunde, tmp_path):
    """Der Klartextspiegel journal.log kuerzte jeden Wert auf 300 Zeichen.
    Paket F nahm das Abbild eines geloeschten Belegs aus; der vollstaendige
    Inhalt einer zurueckgenommenen Rechnung (C), eines verworfenen Entwurfs
    (C) und eines geloeschten Kunden (D) braucht dasselbe -- sonst ist er im
    Spiegel nicht mehr feststellbar."""
    from app.logik import journal_export, kunden

    rid = fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde) for _ in range(3)], datum=HEUTE)
    fs.festschreiben(conn, rid, belegordner=tmp_path / "daten")
    ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                         variante=ablauf.GANZ_LOESCHEN, datenordner=tmp_path / "daten")
    kunden.loeschen(conn, kunde, grund="Testkunde")
    text = journal_export.schreibe(conn, tmp_path / "journal.log").read_text(encoding="utf-8")
    for aktion in ("RECHNUNG_ZURUECKGENOMMEN", "ENTWURF_VERWORFEN", "LOESCHEN"):
        zeile = next(z for z in text.splitlines() if f" | {aktion} | " in z)
        alt = zeile.split(" | ")[4].split(" -> ")[0]
        assert len(alt) > 300 and not alt.endswith("…"), aktion


@pytest.mark.usefixtures("schnelles_pdf")
def test_journalspiegel_kuerzt_auch_den_neuen_wert_nicht(conn, kunde, tmp_path):
    """Der neue Wert war weiter auf 300 Zeichen gekuerzt. Bei einer
    zurueckgenommenen Rechnung (C) stehen dort hinten die Variante (wieder
    Entwurf oder ganz geloescht) und der Pfad des aufbewahrten PDFs -- genau
    das, was ohne die Anwendung feststellbar sein muss. Beim Wegwerfen aller
    Eingangsdateien (E) stehen Originalname und SHA-256 jeder Datei im neuen
    Wert; ab der dritten Datei waren sie abgeschnitten."""
    from app.logik import journal_export
    from app.logik.beleg import sammelbuchung

    # Ein langer Kundenname -- er steht im Dateinamen des PDFs.
    conn.execute("UPDATE kunde SET name = 'Beispiel Agentur für besonders lange"
                 " Firmennamen und Kampagnen GmbH & Co. KG' WHERE id = ?", (kunde,))
    rid = fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde)], datum=HEUTE)
    fs.festschreiben(conn, rid, belegordner=tmp_path / "daten")
    r = ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                             datenordner=tmp_path / "daten")
    dateien = [{"originalname": f"rechnung-{i:02d}-beispiel-hosting.pdf", "sha256": f"{i:064x}"}
               for i in range(6)]
    sammelbuchung.verworfen_protokollieren(conn, dateien, grund="Test")
    text = journal_export.schreibe(conn, tmp_path / "journal.log").read_text(encoding="utf-8")

    def neuer_wert(aktion: str) -> dict:
        zeile = next(z for z in text.splitlines() if f" | {aktion} | " in z)
        neu = zeile.split(" | ")[4].split(" -> ", 1)[1]
        assert len(neu) > 300 and not neu.endswith("…"), aktion
        return json.loads(neu)

    zurueck = neuer_wert("RECHNUNG_ZURUECKGENOMMEN")
    assert zurueck["variante"] == "ENTWURF"
    assert zurueck["pdf_verworfen"] == r.pdf_verworfen
    assert "Beispiel-Agentur" in zurueck["pdf_verworfen"]
    assert neuer_wert("EINGANG_VERWORFEN")["dateien"] == dateien


# ========================= Zuruecknehmen (C) und Kunde loeschen / Nummern (D)

@pytest.mark.usefixtures("schnelles_pdf")
def test_kundennummer_einer_zurueckgenommenen_rechnung_bleibt_verbraucht(conn, kunde, tmp_path):
    """Ein Kunde, dessen einzige Rechnung zurueckgenommen wurde, steht auf
    keinem gueltigen Beleg und laesst sich loeschen (D). Seine Kundennummer
    steht aber auf dem aufbewahrten PDF und im Journal der Nummernluecke (C)
    -- sie wird nicht wieder vergeben: weder automatisch noch von Hand noch
    beim Import. Die Rueckfrage sagt es."""
    import csv
    import io

    from app.logik import kunden, kundenimport

    daten = tmp_path / "daten"
    test = conn.execute(
        "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, satz_cent, angelegt_am)"
        " VALUES ('K-0002', 'Testkunde GmbH', 'Musterstraße 1', '10115', 'Berlin', 9000, ?)",
        (HEUTE,)).lastrowid
    rid = fs.entwurf_anlegen(conn, test, [_zeit(conn, test)], datum=HEUTE)
    nummer = fs.festschreiben(conn, rid, belegordner=daten).nummer
    # Danach eine Rechnung an den echten Kunden: die Nummer bleibt als Luecke.
    fs.festschreiben(conn, fs.entwurf_anlegen(conn, kunde, [_zeit(conn, kunde)], datum=HEUTE),
                     belegordner=daten)
    ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                         variante=ablauf.GANZ_LOESCHEN, datenordner=daten)
    stand = kunden.loeschbarkeit(conn, test)
    assert stand.loeschbar and stand.zurueckgenommen == (nummer,)
    kunden.loeschen(conn, test, stand=stand.stand, grund="Testkunde")

    assert kunden.naechste_kundennummer(conn) == "K-0003"
    with pytest.raises(ValueError, match=f"K-0002 stand auf der zurückgenommenen Rechnung "
                                         f"{nummer}"):
        kunden.pruefe_kundennummer(conn, "K-2")
    puffer = io.StringIO()
    csv.writer(puffer, delimiter=";").writerows([
        ["Kundennummer", "Name", "Straße und Hausnummer", "PLZ", "Ort"],
        ["K-0002", "Neue Beispiel AG", "Musterweg 3", "01067", "Dresden"],
        ["", "Andere Beispiel KG", "Musterweg 4", "01067", "Dresden"],
    ])
    plan = kundenimport.plane(conn, kundenimport.lies(
        "kunden.csv", puffer.getvalue().encode(), kunden.format_aus_betrieb(conn)))
    [mit_nummer, ohne_nummer] = plan.zeilen
    assert mit_nummer.status == kundenimport.FEHLER
    assert "zurückgenommenen Rechnung" in mit_nummer.fehler[0][1]
    assert ohne_nummer.kundennummer == "K-0003"


def test_rueckfrage_nennt_die_zurueckgenommene_rechnung(tmp_path, monkeypatch):
    """Die Rueckfrage zum Loeschen sagt, dass die Nummer verbraucht bleibt --
    statt "wird wieder frei, wenn sie die höchste war"."""
    from fastapi.testclient import TestClient

    from app.main import erzeuge_app

    def rendere(daten: dict, ziel: Path) -> str:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(b"%PDF-1.7\n" + json.dumps(daten, ensure_ascii=False).encode())
        return pdf.pruefsumme(ziel)

    monkeypatch.setattr(pdf, "rendere", rendere)
    pfad = tmp_path / "web.sqlite"
    with TestClient(erzeuge_app(pfad)) as klient:
        conn = db.oeffne(pfad)
        try:
            conn.execute(
                "INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer,"
                " standardsatz_cent, beginn_am) VALUES (1, 'Beispiel Werbung',"
                " 'Werkweg 2', '20095', 'Hamburg', '12/345/67890', 8000, '2020-01-01')")
            kid = conn.execute(
                "INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)"
                " VALUES ('K-0001', 'Testkunde GmbH', 'Musterstraße 1', '10115', 'Berlin', ?)",
                (HEUTE,)).lastrowid
            rid = fs.entwurf_anlegen(conn, kid, [_zeit(conn, kid)], datum=HEUTE)
            nummer = fs.festschreiben(conn, rid, belegordner=tmp_path).nummer
            ablauf.zuruecknehmen(conn, rid, "Testrechnung", bestaetigt=True,
                                 datenordner=tmp_path)
        finally:
            conn.close()
        seite = klient.get(f"/kunden/{kid}").text
    rueckfrage = " ".join(seite[seite.index('<details class="loeschen"'):
                                seite.index("</details>", seite.index('<details class="loeschen"'))
                                ].split())
    assert (f"Seine Kundennummer K-0001 stand auf der zurückgenommenen Rechnung {nummer}"
            in rueckfrage)
    assert "wird deshalb nicht wieder vergeben" in rueckfrage
    assert "wieder frei" not in rueckfrage

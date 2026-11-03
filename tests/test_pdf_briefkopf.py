"""Briefkopf, Zahlungsblock, Fusszeile und Einzelnachweis -- am fertigen PDF.

Gemessen mit pdfplumber an der Lage der Woerter, nicht am CSS: ob eine IBAN
in EINER Zeile steht oder ein Tabellenkopf auf der Folgeseite wiederkommt,
entscheidet die Rendering-Engine.
"""

from pathlib import Path

import pytest

from app import db
from app.logik import festschreiben as f

pdfplumber = pytest.importorskip("pdfplumber")

MM = 72 / 25.4


def _woerter(pfad, seite: int = 0) -> list[dict]:
    with pdfplumber.open(pfad) as pdf:
        return pdf.pages[seite].extract_words(extra_attrs=["size"])


def _seiten(pfad) -> list[str]:
    with pdfplumber.open(pfad) as pdf:
        return [s.extract_text() or "" for s in pdf.pages]


def _zeit(conn, kunde_id, titel="Beratung", datum="2026-03-02", beschreibung=None):
    return conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum, dauer_minuten,"
        " abrechnungsmodus, erfasst_am) VALUES (?, ?, ?, ?, 60, 'STUNDEN', ?)",
        (kunde_id, titel, beschreibung, datum, db.jetzt_utc()),
    ).lastrowid


@pytest.fixture
def eingerichtet(conn, betrieb, kunde_id):
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01', iban = 'DE02120300000000202051',"
        " bic = 'BYLADEM1001', institut = 'Musterbank', inhaber = 'Erika Muster',"
        " telefon = '040 123456', email = 'post@kleingewerbe.example' WHERE id = 1"
    )
    conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
    return kunde_id


def _fest(conn, kunde_id, tmp_path, zeiten=None, **rest):
    rid = f.entwurf_anlegen(conn, kunde_id, zeiten or [_zeit(conn, kunde_id)], **rest)
    ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
    assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
    return Path(ergebnis.pdf_pfad)


class TestBriefkopf:
    def test_name_steht_gross_im_kopfbereich(self, conn, eingerichtet, tmp_path):
        """Vorher war der Kopf leer; der eigene Name stand nur in der
        abgeschnittenen Absenderzeile."""
        woerter = _woerter(_fest(conn, eingerichtet, tmp_path))
        kopf = [w for w in woerter if w["bottom"] <= 45 * MM and w["size"] >= 14]
        assert [w["text"] for w in kopf] == ["Erika", "Muster", "·", "Mein", "Kleingewerbe"]

    def test_inhaber_im_namen_wird_nicht_wiederholt(self, conn, eingerichtet, tmp_path):
        conn.execute("UPDATE betrieb SET name = 'Erika Muster Webdesign' WHERE id = 1")
        woerter = _woerter(_fest(conn, eingerichtet, tmp_path))
        kopf = [w["text"] for w in woerter if w["bottom"] <= 45 * MM and w["size"] >= 14]
        assert kopf == ["Erika", "Muster", "Webdesign"]

    @pytest.mark.parametrize("laenge", [120, 230])
    def test_langer_name_endet_vor_der_ruecksendeangabe(self, conn, eingerichtet, tmp_path,
                                                         laenge):
        """Ab etwa 114 Zeichen lief der Name in 16 pt in die Zusatz- und
        Vermerkzone, ab etwa 140 lagen Name und Ruecksendeangabe
        uebereinander. Ein langer Name wird jetzt kleiner gesetzt."""
        text = ("Online-Marketing und Beratung für Suchmaschinenoptimierung, Social Media, "
                "Webdesign und Fotografie für kleine Betriebe und Vereine in der Region "
                "Musterstadt und Umgebung sowie Seminare, Workshops und Schulungen")
        name = text[: laenge - len("Erika Muster · ")]
        conn.execute("UPDATE betrieb SET name = ? WHERE id = 1", (name,))
        woerter = _woerter(_fest(conn, eingerichtet, tmp_path))
        letztes = name.split()[-1]
        kopf = [w for w in woerter if w["top"] < 45 * MM and w["x0"] < 100 * MM
                and w["size"] >= 9.5]
        assert kopf[-1]["text"] == letztes
        assert max(w["bottom"] for w in kopf) <= 43 * MM
        klein = [w for w in woerter if w["size"] < 7.2 and 40 * MM < w["top"] < 63 * MM]
        assert min(w["top"] for w in klein) > max(w["bottom"] for w in kopf) + 2 * MM

    def test_anschrift_und_kontakt_im_kopf(self, conn, eingerichtet, tmp_path):
        woerter = _woerter(_fest(conn, eingerichtet, tmp_path))
        kopf = " ".join(w["text"] for w in woerter
                        if w["bottom"] <= 45 * MM and w["x0"] > 120 * MM)
        for teil in ("Werkweg 2", "20095 Hamburg", "040 123456", "post@kleingewerbe.example"):
            assert teil in kopf, kopf


def _zeilen(woerter) -> dict[int, list[dict]]:
    zeilen: dict[int, list[dict]] = {}
    for wort in woerter:
        zeilen.setdefault(round(wort["top"]), []).append(wort)
    return zeilen


class TestBankverbindung:
    def test_iban_und_bic_je_in_einer_zeile(self, conn, eingerichtet, tmp_path):
        """Zum Abschreiben und Kopieren: die IBAN in Vierergruppen in EINER
        Zeile -- im Zahlungsblock und in der Fusszeile. Vorher brach sie im
        Fliesstext um ("DE02 1203 0000 0000 | 2020 51")."""
        woerter = _woerter(_fest(conn, eingerichtet, tmp_path))
        gruppen = ["DE02", "1203", "0000", "0000", "2020", "51"]
        fundstellen = [
            zeile for zeile in _zeilen(woerter).values()
            if "DE02" in [w["text"] for w in zeile]
        ]
        assert len(fundstellen) == 2, "Zahlungsblock und Fusszeile"
        for zeile in fundstellen:
            texte = [w["text"] for w in sorted(zeile, key=lambda w: w["x0"])]
            start = texte.index("DE02")
            assert texte[start:start + 6] == gruppen, texte
        bic = [w for w in woerter if w["text"] == "BYLADEM1001"]
        assert len(bic) == 2

    def test_kontoinhaber_und_verwendungszweck(self, conn, eingerichtet, tmp_path):
        """Seit 10/2025 gleicht die Bank des Zahlers den Empfaengernamen mit
        der IBAN ab -- der Kontoinhaber gehoert dazu."""
        text = "\n".join(_seiten(_fest(conn, eingerichtet, tmp_path)))
        assert "Kontoinhaber Erika Muster" in text
        assert "Kontoinhaber: Erika Muster" in text
        assert "Verwendungszweck RE-2026-0001" in text
        assert "Bank Musterbank" in text
        assert "Bitte überweisen Sie den Betrag von 80,00 € bis zum" in text

    def test_ohne_inhaber_steht_der_name_als_kontoinhaber(self, conn, eingerichtet, tmp_path):
        conn.execute("UPDATE betrieb SET inhaber = NULL WHERE id = 1")
        text = "\n".join(_seiten(_fest(conn, eingerichtet, tmp_path)))
        assert "Kontoinhaber Mein Kleingewerbe" in text


class TestFusszeile:
    def test_drei_spalten_ueberlappen_nicht(self, conn, eingerichtet, tmp_path):
        woerter = _woerter(_fest(conn, eingerichtet, tmp_path))
        fuss = [w for w in woerter if w["top"] >= 262 * MM]

        def spalte(*texte):
            treffer = [w for w in fuss if w["text"] in texte]
            assert treffer, texte
            return min(w["x0"] for w in treffer), max(w["x1"] for w in treffer)

        wer = spalte("Werkweg", "20095", "Hamburg", "Kleingewerbe")
        kontakt = spalte("Tel.", "post@kleingewerbe.example", "Steuernummer", "12/345/67890")
        bank = spalte("Kontoinhaber:", "IBAN", "BIC", "BYLADEM1001", "Musterbank")
        assert wer[1] < kontakt[0] < kontakt[1] < bank[0], (wer, kontakt, bank)
        # Bezeichnung und Wert reissen nicht auseinander.
        steuer = [w for w in fuss if w["text"] in ("Steuernummer", "12/345/67890")]
        assert len({round(w["top"]) for w in steuer}) == 1


class TestEinzelnachweis:
    """Lange, mehrzeilige Beschreibungen und viele Eintraege.

    Vorher schob "page-break-inside: avoid" eine Zeile, die auf keine Seite
    passt, auf die naechste: zurueck blieb eine Seite mit nichts als der
    Ueberschrift "Einzelnachweis", und auf den Folgeseiten fehlte der
    Tabellenkopf.
    """

    @pytest.fixture
    def pdf(self, conn, eingerichtet, tmp_path):
        # 2000 Zeichen, 200 Zeilen: hoeher als eine Seite.
        zeilen = "\n".join(f"Zeile {i:03d}" for i in range(1, 201))
        lang = (zeilen + "x" * 2000)[:2000]
        assert len(lang) == 2000
        prosa = ("Kampagnen geprüft, Gebote angepasst, Anzeigentexte überarbeitet. " * 40)[:2000]
        zeiten = [_zeit(conn, eingerichtet, "Analyse", "2026-03-01", lang)]
        zeiten += [
            _zeit(conn, eingerichtet, f"Betreuung {i:02d}", f"2026-03-{2 + i % 25:02d}",
                  prosa if i % 10 == 0 else "kurz")
            for i in range(40)
        ]
        return _fest(conn, eingerichtet, tmp_path, zeiten=zeiten, einzelnachweis=True)

    def test_keine_seite_nur_mit_der_ueberschrift(self, pdf):
        seiten = _seiten(pdf)
        start = next(i for i, s in enumerate(seiten) if "Einzelnachweis" in s)
        assert "Zeile 001" in seiten[start], "die erste Zeile steht unter der Ueberschrift"

    def test_tabellenkopf_auf_jeder_folgeseite(self, pdf):
        seiten = _seiten(pdf)
        start = next(i for i, s in enumerate(seiten) if "Einzelnachweis" in s)
        assert len(seiten) - start >= 3, "der Nachweis soll mehrere Seiten fuellen"
        for nummer, seite in enumerate(seiten[start:], start=start + 1):
            assert "Datum Leistung Dauer Stunden" in seite, f"Seite {nummer} ohne Kopf"
        # Die Positionsliste davor ebenso -- jede Seite mit Positionen. Die
        # letzte Seite vor dem Nachweis kann allein den Zahlungsblock tragen.
        positionsseiten = [s for s in seiten[:start] if "Betreuung" in s or "Analyse" in s]
        assert len(positionsseiten) >= 2
        for seite in positionsseiten:
            assert "Pos. Leistung Menge Einheit Einzelpreis Betrag" in seite, seite[:200]

    def test_alles_steht_drauf(self, pdf):
        text = "\n".join(_seiten(pdf))
        for i in (1, 100, 200):
            assert f"Zeile {i:03d}" in text
        for i in (0, 39):
            assert f"Betreuung {i:02d}" in text
        assert "Summe" in text

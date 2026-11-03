"""Das Anschriftfeld muss im Fensterbereich sitzen.

Eine Rechnung, deren Anschrift nicht im Briefumschlagfenster steht, kommt
zurueck. Das merkt man erst beim Falten -- und dann ist das PDF schon
festgeschrieben und gehasht.

Gemessen wird am fertigen PDF mit ``pdftotext -bbox``, nicht am CSS: ob eine
Angabe in Millimetern dort ankommt, wo sie hingehoert, entscheidet die
Rendering-Engine, und genau die wird hier geprueft. Der Test ist zugleich die
Absicherung gegen einen Wechsel der Schrift oder der Engine.

Fensterlage nach DIN 5008 Form B (Umschlag DIN lang mit Fenster):
Fenster 90 mm breit und 45 mm hoch, linke Kante 20 mm, obere Kante 45 mm vom
Papierrand. Innerhalb davon bleibt ein Sicherheitsabstand, damit die Schrift
nicht an der Fensterkante klebt.

Das Anschriftfeld (85 x 45 mm) teilt sich in die Zusatz- und Vermerkzone
(45 bis 62,7 mm, unten darin die Ruecksendeangabe) und die Anschriftzone
(62,7 bis 90 mm, die Empfaengeranschrift).
"""

import re
import subprocess
from pathlib import Path

import pytest

from app import db
from app.logik import festschreiben as f
from tests.conftest import braucht

#: Gemessen wird am fertigen Dokument, nicht am CSS.
pytestmark = braucht("pdftotext")


MM = 72 / 25.4  # PostScript-Punkt je Millimeter

#: Fenster DIN lang, Form B.
FENSTER_LINKS, FENSTER_OBEN = 20.0, 45.0
FENSTER_BREIT, FENSTER_HOCH = 90.0, 45.0
#: Abstand zur Fensterkante, den die Schrift einhalten muss.
SICHERHEIT = 2.0
#: Grenze zwischen Zusatz- und Vermerkzone und Anschriftzone.
ANSCHRIFTZONE = 62.7


def _woerter(pfad: Path) -> list[dict[str, object]]:
    """Alle Woerter der ersten Seite mit ihrer Lage in Millimetern."""
    xml = subprocess.run(
        ["pdftotext", "-bbox", "-f", "1", "-l", "1", str(pfad), "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    gefunden = []
    for treffer in re.finditer(
        r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>',
        xml,
    ):
        xmin, ymin, xmax, ymax = (float(treffer.group(i)) / MM for i in range(1, 5))
        gefunden.append({
            "text": treffer.group(5), "links": xmin, "oben": ymin,
            "rechts": xmax, "unten": ymax,
        })
    return gefunden


@pytest.fixture
def rechnung_pdf(conn, betrieb, kunde_id, tmp_path):
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01',"
        " iban = 'DE02120300000000202051' WHERE id = 1"
    )
    conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
    ids = [
        conn.execute(
            "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
            " abrechnungsmodus, erfasst_am) VALUES (?, 'Beratung', ?, 60, 'STUNDEN', ?)",
            (kunde_id, f"2026-03-{tag:02d}", db.jetzt_utc()),
        ).lastrowid
        for tag in (2, 3)
    ]
    rid = f.entwurf_anlegen(conn, kunde_id, ids)
    ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
    assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
    return Path(ergebnis.pdf_pfad)


#: Alle Woerter der Empfaengeranschrift.
_ANSCHRIFT = ("Mustermann", "GmbH", "Hauptstr.", "10115", "Berlin")

#: Davon die, die es im Dokument nur EINMAL gibt. Der Kundenname taugt nicht als
#: Anker: er steht seit der Betreffzeile ("Rechnung – März 2026 – Mustermann
#: GmbH") absichtlich ein zweites Mal auf dem Blatt, weit unterhalb des
#: Fensters. Wer nach dem Namen sucht, findet den Betreff und misst die falsche
#: Stelle -- der Test schluege an, obwohl die Anschrift richtig sitzt.
_NUR_IN_DER_ANSCHRIFT = ("Hauptstr.", "10115", "Berlin")

#: Hoehe, die oberhalb der Anschriftzeile noch zum Block gehoert (Name,
#: Rechnungsname, Ansprechpartner -- bis zu drei Zeilen).
_NAMENSZEILEN_MM = 14.0


def _anschrift(pfad: Path) -> list[dict[str, object]]:
    """Die Woerter der Empfaengeranschrift -- ueber die Anschriftzeile gefunden."""
    woerter = _woerter(pfad)
    anker = [w for w in woerter if w["text"] in _NUR_IN_DER_ANSCHRIFT]
    if not anker:
        return []
    unten = max(w["unten"] for w in anker)
    oben = min(w["oben"] for w in anker) - _NAMENSZEILEN_MM
    return [
        w for w in woerter
        if w["text"] in _ANSCHRIFT and oben <= w["oben"] and w["unten"] <= unten + 0.5
    ]


class TestAnschriftfeld:
    def test_anschrift_wird_gefunden(self, rechnung_pdf):
        assert len(_anschrift(rechnung_pdf)) >= 4

    def test_anschrift_liegt_im_fenster(self, rechnung_pdf):
        for wort in _anschrift(rechnung_pdf):
            assert wort["links"] >= FENSTER_LINKS + SICHERHEIT, wort
            assert wort["rechts"] <= FENSTER_LINKS + FENSTER_BREIT - SICHERHEIT, wort
            assert wort["oben"] >= FENSTER_OBEN, wort
            assert wort["unten"] <= FENSTER_OBEN + FENSTER_HOCH - SICHERHEIT, wort

    def test_anschrift_beginnt_oben_in_der_anschriftzone(self, rechnung_pdf):
        """Die Anschrift beginnt bei 62,7 mm, nicht weiter unten als noetig:
        darunter stehen bis zu sechs Zeilen. Darueber liegt die Vermerkzone
        mit der Ruecksendeangabe -- vorher begann die Anschrift schon bei
        45 mm, und die Absenderzeile stand ausserhalb des Fensters."""
        oberste = min(w["oben"] for w in _anschrift(rechnung_pdf))
        assert ANSCHRIFTZONE <= oberste <= ANSCHRIFTZONE + 2

    def test_im_fenster_steht_nur_die_anschrift(self, rechnung_pdf):
        """Durch das Fenster sieht man nur den Empfaenger.

        Die Ruecksendeangabe gehoert noch dazu: sie steht nach DIN 5008 als
        kleingedruckte Zeile in der Vermerkzone ueber der Anschrift und ist
        Teil des Feldes. Alles andere -- Rechnungsnummer, Betreff, Logo-Text
        -- waere ein Layoutfehler, der dem Empfaenger auffaellt und sonst
        niemandem.
        """
        im_fenster = [
            w for w in _woerter(rechnung_pdf)
            if FENSTER_OBEN <= w["oben"] <= FENSTER_OBEN + FENSTER_HOCH
            and FENSTER_LINKS <= w["links"] <= FENSTER_LINKS + FENSTER_BREIT
        ]
        assert im_fenster, "Im Fenster steht gar nichts"
        erlaubt = {
            "Mustermann", "GmbH", "Hauptstr.", "1", "10115", "Berlin",
            # Ruecksendeangabe
            "Mein", "Kleingewerbe", "·", "Werkweg", "2", "20095", "Hamburg",
        }
        fremd = sorted({w["text"] for w in im_fenster} - erlaubt)
        assert fremd == [], f"Fremder Text im Fensterbereich: {fremd}"


class TestSeitenaufbau:
    def test_viele_positionen_paginieren(self, conn, betrieb, kunde_id, tmp_path):
        """Ein absolut positionierter Block paginiert nicht -- der Rest fiele
        lautlos aus dem Dokument. Deshalb liegt nur der Briefkopf absolut."""
        conn.execute(
            "UPDATE betrieb SET beginn_am = '2020-01-01',"
            " iban = 'DE02120300000000202051' WHERE id = 1"
        )
        conn.execute("UPDATE kunde SET satz_cent = 8000, gruppierung_default = 'PRO_EINTRAG'"
                     " WHERE id = ?", (kunde_id,))
        ids = [
            conn.execute(
                "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
                " abrechnungsmodus, erfasst_am) VALUES (?, ?, '2026-03-02', 30,"
                " 'STUNDEN', ?)",
                (kunde_id, f"Vorgang Nummer {i:02d}", db.jetzt_utc()),
            ).lastrowid
            for i in range(60)
        ]
        rid = f.entwurf_anlegen(conn, kunde_id, ids, modus="PRO_EINTRAG")
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler

        text = subprocess.run(
            ["pdftotext", "-layout", str(ergebnis.pdf_pfad), "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "Vorgang Nummer 00" in text
        assert "Vorgang Nummer 59" in text, "Positionen hinten fehlen -- Pagination kaputt"
        assert text.count("Seite ") >= 3


# ------------------------------------------------- Ruecksendeangabe, Marken


def _eine_zeit(conn, kunde_id) -> list[int]:
    conn.execute(
        "UPDATE betrieb SET beginn_am = '2020-01-01',"
        " iban = 'DE02120300000000202051' WHERE id = 1"
    )
    conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
    return [conn.execute(
        "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
        " abrechnungsmodus, erfasst_am) VALUES (?, 'Beratung', '2026-03-02', 60,"
        " 'STUNDEN', ?)",
        (kunde_id, db.jetzt_utc()),
    ).lastrowid]


class TestRuecksendeangabe:
    """Die Ruecksendeangabe steht vollstaendig in der Vermerkzone.

    Vorher stand sie bei 40 mm -- ausserhalb des Fensters -- und wurde mit
    overflow: hidden abgeschnitten: "… · 20095 Hambu".
    """

    def test_liegt_in_der_vermerkzone_und_im_fenster(self, rechnung_pdf):
        zeile = [
            w for w in _woerter(rechnung_pdf)
            if w["text"] in ("Kleingewerbe", "Werkweg", "20095", "Hamburg")
            # Nicht der Kopfbereich: dort stehen Name und Anschrift noch einmal.
            and 35 < w["oben"] < ANSCHRIFTZONE and w["links"] < FENSTER_LINKS + FENSTER_BREIT
        ]
        assert {w["text"] for w in zeile} == {"Kleingewerbe", "Werkweg", "20095", "Hamburg"}
        for wort in zeile:
            assert wort["oben"] >= FENSTER_OBEN, wort
            assert wort["unten"] <= ANSCHRIFTZONE, wort
            assert wort["links"] >= FENSTER_LINKS + SICHERHEIT, wort
            assert wort["rechts"] <= FENSTER_LINKS + FENSTER_BREIT - SICHERHEIT, wort

    def test_langer_name_wird_umbrochen_statt_abgeschnitten(
        self, conn, betrieb, kunde_id, tmp_path
    ):
        """80 Zeichen Name: alles steht da, bis "PLZ Ort", in der Vermerkzone
        und ohne die Anschrift zu beruehren."""
        name = "Vorname Nachname Webdesign Studio und Beratung fuer kleine und mittlere Betriebe"
        assert len(name) == 80
        ids = _eine_zeit(conn, kunde_id)
        conn.execute(
            "UPDATE betrieb SET name = ?, inhaber = 'Erika Muster' WHERE id = 1", (name,)
        )
        rid = f.entwurf_anlegen(conn, kunde_id, ids)
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler

        pfad = Path(ergebnis.pdf_pfad)
        zone = [w for w in _woerter(pfad)
                if FENSTER_OBEN <= w["oben"] and w["unten"] <= ANSCHRIFTZONE
                and w["links"] < FENSTER_LINKS + FENSTER_BREIT]
        erwartet = ["Erika", "Muster", *name.split(), "Werkweg", "2", "20095", "Hamburg"]
        assert [w["text"] for w in zone if w["text"] != "·"] == erwartet
        assert len({round(w["oben"]) for w in zone}) >= 2, "umbrochen, nicht abgeschnitten"
        for wort in zone:
            assert wort["rechts"] <= FENSTER_LINKS + FENSTER_BREIT - SICHERHEIT, wort
        assert max(w["unten"] for w in zone) < min(w["oben"] for w in _anschrift(pfad))


def _graustufen(pdf: Path, seite: int, tmp_path: Path, dpi: int = 72):
    """Eine Seite als Graustufenbild: (Breite, Hoehe, Bytes) aus pdftoppm -gray.

    PGM (P5) ist einfach genug, um es ohne Bildbibliothek zu lesen.
    """
    ziel = tmp_path / f"seite{seite}"
    subprocess.run(
        ["pdftoppm", "-gray", "-r", str(dpi), "-f", str(seite), "-l", str(seite),
         "-singlefile", str(pdf), str(ziel)],
        check=True,
    )
    roh = (tmp_path / f"seite{seite}.pgm").read_bytes()
    kopf = roh.split(maxsplit=4)
    assert kopf[0] == b"P5"
    breite, hoehe = int(kopf[1]), int(kopf[2])
    return breite, hoehe, roh[len(roh) - breite * hoehe:]


def _dunkel(bild, x_mm: tuple[float, float], y_mm: tuple[float, float], dpi: int = 72) -> bool:
    """Gibt es in diesem Rechteck (Millimeter) einen dunklen Bildpunkt?"""
    breite, _hoehe, pixel = bild
    px = dpi / 25.4
    return any(
        pixel[y * breite + x] < 200
        for y in range(int(y_mm[0] * px), int(y_mm[1] * px) + 1)
        for x in range(int(x_mm[0] * px), int(x_mm[1] * px) + 1)
    )


class TestFalzmarken:
    """Falz- und Lochmarken an der PAPIERkante, nur auf Seite 1.

    Vorher lagen sie mit position: fixed an der Textkante (25 mm) und auf
    Folgeseiten bei 125/230 mm mitten in der Positionsspalte; die Lochmarke
    fehlte.
    """

    def test_marken_an_der_papierkante(self, rechnung_pdf, tmp_path):
        bild = _graustufen(rechnung_pdf, 1, tmp_path)
        for hoehe in (105.0, 148.5, 210.0):
            assert _dunkel(bild, (0.5, 5.0), (hoehe - 0.8, hoehe + 0.8)), hoehe
            assert not _dunkel(bild, (10.0, 24.0), (hoehe - 0.8, hoehe + 0.8)), hoehe

    def test_folgeseiten_ohne_marken(self, conn, betrieb, kunde_id, tmp_path):
        _eine_zeit(conn, kunde_id)
        ids = [
            conn.execute(
                "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
                " abrechnungsmodus, erfasst_am) VALUES (?, ?, '2026-03-02', 30,"
                " 'STUNDEN', ?)",
                (kunde_id, f"Vorgang Nummer {i:02d}", db.jetzt_utc()),
            ).lastrowid
            for i in range(45)
        ]
        rid = f.entwurf_anlegen(conn, kunde_id, ids, modus="PRO_EINTRAG")
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
        bild = _graustufen(Path(ergebnis.pdf_pfad), 2, tmp_path)
        # Der ganze linke Rand bis zur Textkante bleibt leer.
        assert not _dunkel(bild, (0.5, 23.0), (15.0, 260.0))


class TestVorschauGeometrie:
    def test_anschrift_sitzt_wie_im_endgueltigen_pdf(
        self, conn, betrieb, kunde_id, tmp_path
    ):
        """Die Vorschau zeigt, was festgeschrieben wird -- millimetergenau."""
        from app import pdf

        rid = f.entwurf_anlegen(conn, kunde_id, _eine_zeit(conn, kunde_id))
        vorschau = tmp_path / "vorschau.pdf"
        vorschau.write_bytes(pdf.rendere_vorschau(f.vorschau_daten(conn, rid)))
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")

        def lage(pfad):
            return sorted(
                (w["text"], round(w["links"], 1), round(w["oben"], 1))
                for w in _woerter(pfad)
                if w["text"] in (*_NUR_IN_DER_ANSCHRIFT, "Werkweg", "20095", "Hamburg")
                and w["oben"] < 100 and w["links"] < FENSTER_LINKS + FENSTER_BREIT
            )

        assert len(lage(vorschau)) == 6
        assert lage(vorschau) == lage(Path(ergebnis.pdf_pfad))

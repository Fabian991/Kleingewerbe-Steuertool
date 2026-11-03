"""Die Schrift muss im Projekt liegen und im PDF ankommen.

Eine Rechnung ist acht Jahre aufbewahrungspflichtig (§ 147 Abs. 3 AO,
§ 14b Abs. 1 UStG). Verlaesst sich die Vorlage auf eine Systemschrift, haengt das
Layout eines aufbewahrungspflichtigen Belegs davon ab, welche Schriften dieser
Rechner in acht Jahren hat -- und fehlt sie,
setzt WeasyPrint still auf eine Ersatzschrift um: andere Laufweite, andere
Umbrueche, im schlimmsten Fall ein Anschriftfeld ausserhalb des
Fensterbereichs. Das faellt niemandem auf, und genau deshalb steht es hier.
"""

import re
from pathlib import Path

import pytest

from app import db
from app.logik import festschreiben as f
from tests.conftest import braucht

#: Gemessen wird am fertigen Dokument, nicht am CSS.
pytestmark = braucht("pdffonts", "pdftotext")


SCHRIFTEN = Path(__file__).resolve().parent.parent / "app" / "vorlagen" / "schriften"
CSS = Path(__file__).resolve().parent.parent / "app" / "vorlagen" / "rechnung_pdf.css"

#: Die Schnitte, die die Vorlage benutzt. Kursiv traegt den Kulanzhinweis.
ERWARTET = (
    "LiberationSans-Regular.ttf",
    "LiberationSans-Bold.ttf",
    "LiberationSans-Italic.ttf",
)


def _pdffonts(pfad: Path) -> list[dict[str, str]]:
    """Die Schriften eines PDF, so wie ein fremder Leser sie sieht."""
    import subprocess

    ausgabe = subprocess.run(
        ["pdffonts", str(pfad)], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    zeilen = []
    for zeile in ausgabe[2:]:          # Kopfzeile und Trennlinie ueberspringen
        felder = zeile.split()
        if len(felder) >= 5:
            zeilen.append({"name": felder[0], "emb": felder[-5]})
    return zeilen


class TestDateienImProjekt:
    @pytest.mark.parametrize("name", ERWARTET)
    def test_schrift_liegt_im_projekt(self, name):
        datei = SCHRIFTEN / name
        assert datei.exists(), f"{name} fehlt -- das PDF haengt dann an der Systemschrift"
        assert datei.stat().st_size > 100_000

    def test_lizenz_reist_mit(self):
        """OFL 1.1 verlangt, dass der Lizenztext bei jeder Weitergabe mitgeht."""
        text = (SCHRIFTEN / "OFL.txt").read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in text
        assert "Reserved Font Name" in text

    def test_herkunft_ist_dokumentiert(self):
        text = (SCHRIFTEN / "HERKUNFT.md").read_text(encoding="utf-8")
        assert "liberation-fonts" in text
        assert "OFL" in text

    def test_dateien_sind_unveraendert(self):
        """Unverändert uebernommen -- eine Unterteilung waere nach OFL eine
        Modified Version und muesste umbenannt werden."""
        from fontTools.ttLib import TTFont

        schrift = TTFont(SCHRIFTEN / "LiberationSans-Regular.ttf")
        namen = {r.nameID: str(r) for r in schrift["name"].names if r.platformID == 3}
        assert namen[1] == "Liberation Sans"

    def test_deutsche_rechnungszeichen_sind_vorhanden(self):
        from fontTools.ttLib import TTFont

        cmap = TTFont(SCHRIFTEN / "LiberationSans-Regular.ttf").getBestCmap()
        fehlt = [z for z in "AÄÖÜäöüß€§„“–—0123456789,." if ord(z) not in cmap]
        assert fehlt == []


class TestVorlage:
    def test_css_bindet_die_dateien_ein(self):
        css = CSS.read_text(encoding="utf-8")
        for name in ERWARTET:
            assert f"schriften/{name}" in css

    def test_mitgelieferte_schrift_steht_vor_den_systemschriften(self):
        css = CSS.read_text(encoding="utf-8")
        stapel = re.search(r"html \{[^}]*font-family:\s*([^;]+);", css, re.S)
        assert stapel, "Kein Schriftstapel in html { } gefunden"
        assert stapel.group(1).strip().startswith('"Liberation Sans"')


class TestImPdf:
    """Der eigentliche Nachweis: eingebunden heisst nicht eingebettet."""

    @pytest.fixture
    def rechnung_pdf(self, conn, betrieb, kunde_id, tmp_path):
        conn.execute(
            "UPDATE betrieb SET beginn_am = '2020-01-01',"
            " iban = 'DE02120300000000202051' WHERE id = 1"
        )
        conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
        ids = []
        for tag in (2, 3):
            ids.append(conn.execute(
                "INSERT INTO zeiteintrag (kunde_id, titel, beschreibung, datum,"
                " dauer_minuten, abrechnungsmodus, erfasst_am)"
                " VALUES (?, 'Beratung „März“', 'Umlaute: ÄÖÜäöüß, Zeichen: €§–', ?,"
                " 60, 'STUNDEN', ?)",
                (kunde_id, f"2026-03-{tag:02d}", db.jetzt_utc()),
            ).lastrowid)
        rid = f.entwurf_anlegen(conn, kunde_id, ids)
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler
        return Path(ergebnis.pdf_pfad)

    def test_schrift_ist_im_pdf_eingebettet(self, rechnung_pdf):
        """Eingebunden heisst nicht eingebettet.

        Gelesen wird mit ``pdffonts``: die Objektstroeme eines PDF sind
        komprimiert, in den Rohbytes steht kein Schriftname.
        """
        zeilen = _pdffonts(rechnung_pdf)
        benutzt = [z for z in zeilen if "Liberation" in z["name"]]
        assert benutzt, zeilen
        # Ohne Einbettung braeuchte der Leser die Schrift selbst -- genau der
        # Zustand, den dieser Block beseitigt.
        assert all(z["emb"] == "yes" for z in benutzt), benutzt

    def test_keine_ersatzschrift_im_pdf(self, rechnung_pdf):
        """Greift die Einbindung nicht, setzt WeasyPrint auf eine andere Familie um.

        Geprueft wird deshalb die Gegenrichtung: im PDF darf NUR Liberation Sans
        stehen. Ein Name aus einer anderen Familie heisst, dass die @font-face
        nicht gegriffen hat -- andere Laufweite, andere Umbrueche.
        """
        ersatz = [z["name"] for z in _pdffonts(rechnung_pdf)
                  if "Liberation" not in z["name"]]
        assert ersatz == [], f"Fremde Schrift im PDF: {ersatz}"

    def test_kursivschnitt_wird_benutzt(self, conn, betrieb, kunde_id, tmp_path):
        """Der Kulanzhinweis steht kursiv -- der Schnitt muss auch ankommen.

        Fehlt die Datei, stellt WeasyPrint die aufrechte Schrift schraeg. Das
        sieht aehnlich aus und ist deshalb der Fall, der unbemerkt bleibt.
        """
        conn.execute(
            "UPDATE betrieb SET beginn_am = '2020-01-01',"
            " iban = 'DE02120300000000202051' WHERE id = 1"
        )
        conn.execute("UPDATE kunde SET satz_cent = 8000 WHERE id = ?", (kunde_id,))
        ids = [
            conn.execute(
                "INSERT INTO zeiteintrag (kunde_id, titel, datum, dauer_minuten,"
                " abrechnungsmodus, grund, satz_cent_snapshot, erfasst_am)"
                " VALUES (?, ?, '2026-03-02', 60, ?, ?, 8000, ?)",
                (kunde_id, titel, modus, "Einarbeitung" if modus == "KULANZ" else None,
                 db.jetzt_utc()),
            ).lastrowid
            for titel, modus in (("Beratung", "STUNDEN"), ("Einarbeitung", "KULANZ"))
        ]
        rid = f.entwurf_anlegen(conn, kunde_id, ids)
        ergebnis = f.festschreiben(conn, rid, belegordner=tmp_path / "belege")
        assert ergebnis.pdf_fehler is None, ergebnis.pdf_fehler

        namen = [z["name"] for z in _pdffonts(Path(ergebnis.pdf_pfad))]
        assert any("Liberation-Sans-Italic" in n for n in namen), namen

    def test_umlaute_und_sonderzeichen_kommen_an(self, rechnung_pdf):
        """Eine Schrift einzubetten nuetzt nichts, wenn die Zeichen fehlen."""
        import subprocess

        text = subprocess.run(
            ["pdftotext", "-layout", str(rechnung_pdf), "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert "Beratung „März“" in text
        assert "ÄÖÜäöüß" in text
        assert "§ 19" in text

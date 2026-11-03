"""PDF-Erzeugung als eigener Prozess.

Bewusst ein Subprozess und nicht ein Aufruf im Server, aus drei Gruenden:

* WeasyPrint laedt native Bibliotheken (Pango). Deren Suchpfad laesst sich nur
  beim Start eines Prozesses setzen -- im Server waere es zu spaet.
* Ein Absturz im Rendering reisst den Server nicht mit.
* Der Austausch der Engine ist eine Aenderung an genau dieser Datei.

Aufruf: ``python -m app.pdfwerk ZIELDATEI`` mit den Daten als JSON auf stdin.
Geschrieben wird zuerst in eine temporaere Datei im Zielverzeichnis und dann
atomar umbenannt: ein abgebrochener Lauf hinterlaesst keine halbe Rechnung
unter dem endgueltigen Namen.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

VORLAGENVERZEICHNIS = Path(__file__).resolve().parent / "vorlagen"


def rendere_html(daten: dict) -> str:
    """Setzt die Rechnungsvorlage mit den uebergebenen Daten."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    from app.filter import registriere

    env = Environment(
        loader=FileSystemLoader(str(VORLAGENVERZEICHNIS)),
        autoescape=select_autoescape(["html"]),
    )
    registriere(env)
    return env.get_template("rechnung_pdf.html").render(**daten)


def schreibe_pdf(html: str, ziel: Path) -> None:
    from weasyprint import HTML

    ziel.parent.mkdir(parents=True, exist_ok=True)
    # Temporaere Datei im ZIELverzeichnis, damit das Umbenennen auf demselben
    # Dateisystem stattfindet und damit atomar ist.
    griff, vorlaeufig = tempfile.mkstemp(dir=ziel.parent, suffix=".pdf.tmp")
    os.close(griff)
    try:
        HTML(string=html, base_url=str(VORLAGENVERZEICHNIS)).write_pdf(vorlaeufig)
        os.replace(vorlaeufig, ziel)
    except BaseException:
        Path(vorlaeufig).unlink(missing_ok=True)
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Aufruf: python -m app.pdfwerk ZIELDATEI  (Daten als JSON auf stdin)",
              file=sys.stderr)
        return 2
    daten = json.loads(sys.stdin.read())
    schreibe_pdf(rendere_html(daten), Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Anstoss der PDF-Erzeugung aus dem Server heraus."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

#: Laenger darf ein Rendering nicht brauchen. Haengt WeasyPrint, soll die
#: Anwendung einen Fehler melden und nicht den Browser blockieren.
ZEITGRENZE_SEKUNDEN = 60

#: Wo Homebrew seine Bibliotheken ablegt -- Apple Silicon zuerst, danach der
#: Intel-Pfad. WeasyPrint laedt Pango, cairo und gdk-pixbuf ueber ctypes, und
#: ctypes sucht NICHT in den Homebrew-Pfaden: macOS durchsucht seit System
#: Integrity Protection nur die Systemverzeichnisse, und ein an den Elternprozess
#: vererbtes DYLD_* wird dabei verworfen. Der Suchpfad laesst sich deshalb nur
#: beim START eines Prozesses setzen -- genau dafuer ist pdfwerk ein eigener.
_MAC_BIBLIOTHEKEN = ("/opt/homebrew/lib", "/usr/local/lib")


def _umgebung() -> dict[str, str] | None:
    """Die Umgebung fuer den Renderprozess, oder None fuer "unveraendert".

    Nur auf macOS noetig und nur dort gesetzt. Vorhandene Eintraege bleiben
    vorn: wer den Pfad selbst gesetzt hat, meint es ernst.
    """
    if sys.platform != "darwin":
        return None
    umgebung = dict(os.environ)
    schluessel = "DYLD_FALLBACK_LIBRARY_PATH"
    # Der Standardwert von macOS, wenn die Variable gar nicht gesetzt ist. Er
    # muss mit, sonst nimmt man dem Prozess die Systembibliotheken weg.
    vorgabe = f"{os.path.expanduser('~/lib')}:/usr/local/lib:/usr/lib"
    vorhanden = umgebung.get(schluessel, vorgabe).split(":")
    pfade = [*vorhanden, *(p for p in _MAC_BIBLIOTHEKEN if p not in vorhanden)]
    umgebung[schluessel] = ":".join(p for p in pfade if p)
    return umgebung


class PdfFehler(RuntimeError):
    """Das Rendering ist fehlgeschlagen. Die Meldung enthaelt stderr."""


def rendere(daten: dict, ziel: Path) -> str:
    """Erzeugt das PDF und gibt dessen SHA-256 zurueck.

    Wird NACH dem Commit der Rechnung aufgerufen, nie innerhalb der
    Transaktion: ein Rollback wuerde eine bereits geschriebene Datei nicht
    entfernen, und sie laege als Waise im Belegordner -- womoeglich mit einer
    Nummer im Namen, die anschliessend an eine andere Rechnung geht.
    """
    lauf = subprocess.run(
        [sys.executable, "-m", "app.pdfwerk", str(ziel)],
        input=json.dumps(daten, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=ZEITGRENZE_SEKUNDEN,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=_umgebung(),
    )
    if lauf.returncode != 0:
        raise PdfFehler(
            f"PDF-Erzeugung fehlgeschlagen (Code {lauf.returncode}):\n"
            f"{lauf.stderr.strip() or lauf.stdout.strip()}"
        )
    if not ziel.exists():
        raise PdfFehler("Das Rendering meldete Erfolg, hat aber keine Datei geschrieben.")
    return pruefsumme(ziel)


def pruefsumme(pfad: Path) -> str:
    """SHA-256 einer Datei.

    Nach GoBD Rz. 138 ist ein Hash nicht vorgeschrieben. Er ist aber der
    guenstigste Weg, die Unveraenderbarkeit nach Rz. 110 nachzuweisen -- ein
    Ordner mit PDFs genuegt dafuer ausdruecklich nicht.
    """
    hasher = hashlib.sha256()
    with pfad.open("rb") as datei:
        for block in iter(lambda: datei.read(1 << 16), b""):
            hasher.update(block)
    return hasher.hexdigest()


# ------------------------------------------------------------------ Vorschau

def rendere_vorschau(daten: dict) -> bytes:
    """Erzeugt die PDF-Vorschau eines Entwurfs und gibt sie als Bytes zurueck.

    Derselbe Weg wie beim echten PDF -- Subprozess, Vorlage, Schrift --, damit
    die Vorschau zeigt, was beim Festschreiben herauskommt. Nur das Ziel ist
    ein anderes: ein temporaeres Verzeichnis im System-Temp, nie der Daten-
    oder Belegordner. Es wird nach dem Lesen geloescht, und kein Hash wird
    gespeichert -- eine Vorschau ist kein Beleg.
    """
    with tempfile.TemporaryDirectory(prefix="steuer-vorschau-") as ordner:
        ziel = Path(ordner) / "vorschau.pdf"
        rendere(daten, ziel)
        return ziel.read_bytes()


#: Ist das Rendern einmal gelungen, bleibt es dabei -- bis zum Neustart.
_RENDERER_DA = False


def renderer_fehlt() -> str | None:
    """Warum hier kein PDF entstehen kann -- oder None, wenn es geht.

    Geprueft wird im selben Rahmen wie beim Rendern: ein eigener Prozess mit
    derselben Umgebung (auf dem Mac der Bibliothekspfad fuer Homebrew). Ein
    Import im Server sagte darueber nichts, denn Pango laedt im Server
    anders als im Renderprozess.

    Gemerkt wird nur der Erfolg. Ein Fehlschlag wird beim naechsten Aufruf
    neu geprueft: wer Pango nachinstalliert, soll die Vorschau sehen, ohne
    dass ein einmal haengengebliebener Befund sie bis zum Neustart versteckt.
    """
    global _RENDERER_DA
    if _RENDERER_DA:
        return None
    try:
        lauf = subprocess.run(
            [sys.executable, "-c", "import weasyprint"],
            capture_output=True, text=True, timeout=ZEITGRENZE_SEKUNDEN,
            cwd=str(Path(__file__).resolve().parent.parent), env=_umgebung(),
        )
    except (OSError, subprocess.SubprocessError) as fehler:
        return verstaendlich(str(fehler))
    if lauf.returncode != 0:
        return verstaendlich(lauf.stderr or lauf.stdout)
    _RENDERER_DA = True
    return None


def verstaendlich(meldung: str) -> str:
    """Eine Fehlermeldung des Renderers als Satz fuer den Nutzer.

    Die beiden haeufigen Faelle haben eine klare Abhilfe; alles andere geht
    gekuerzt durch, damit es sich melden laesst.

    >>> verstaendlich("ModuleNotFoundError: No module named 'weasyprint'")[:34]
    'WeasyPrint ist nicht installiert. '
    >>> verstaendlich("OSError: cannot load library 'libpango-1.0-0'")[:44]
    'WeasyPrint findet die Bibliothek Pango nicht'
    """
    text = meldung.strip()
    if "No module named 'weasyprint'" in text:
        return (
            "WeasyPrint ist nicht installiert. Ohne WeasyPrint entsteht kein Rechnungs-PDF "
            "und keine Vorschau. Abhilfe: die Anwendung über ./start.command starten, "
            "das installiert es."
        )
    if "pango" in text.lower() or "cairo" in text.lower() or "gobject" in text.lower():
        return (
            "WeasyPrint findet die Bibliothek Pango nicht. Ohne sie entsteht kein "
            "Rechnungs-PDF und keine Vorschau. Abhilfe auf dem Mac: brew install pango, "
            "danach die Anwendung neu starten."
        )
    letzte = text.splitlines()[-1] if text else "unbekannter Fehler"
    return f"Das PDF konnte nicht erzeugt werden: {letzte[:300]}"

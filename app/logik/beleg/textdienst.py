"""Die Textschicht eines PDF in einem eigenen Prozess -- mit Zeitgrenze.

Fuer das Abholen aus dem Eingangsordner. Die Textschicht liest pdfminer, und
das braucht fuer ein PDF mit Datenmuell am Dateiende nicht Sekunden, sondern
Minuten: ein 50-MB-PDF mit angehaengtem "%" ohne Zeilenumbruch hielt einen
Lauf 336 s lang fest -- und mit ihm die Sperre, "Jetzt abholen" und das
Beenden. Ein Thread laesst sich nicht abbrechen, ein Prozess schon.

Ein Prozess je Lauf, erst beim ersten PDF gestartet und fuer alle weiteren
benutzt. Er liest je Zeile einen Pfad (JSON) und antwortet mit einer Zeile.
Ueberschreitet eine Datei die Zeitgrenze, wird er beendet und beim naechsten
PDF neu gestartet. Bewusst ``subprocess`` und nicht ``multiprocessing``: das
Startverfahren "spawn" laedt das Hauptmodul des Programms neu, und wer das
Werkzeug anders startet, bekaeme einen Pool, der seine Arbeiter endlos neu
startet.

Die Pruefmaske liest genauso (``text_mit_zeitgrenze``): ein PDF, das beim
Abholen an der Zeitgrenze scheiterte, hielt sonst die Seite drei Minuten fest
-- und das Buchen nach der Handeingabe noch einmal. Dort laeuft EIN Prozess
fuer alle Anfragen und bleibt zwischen ihnen stehen: ein Prozessstart je
Anfrage (Python und pdfplumber laden) kostete jede Maske spuerbar Zeit, ein
stehender Prozess liest ein gewoehnliches PDF so schnell wie das Lesen im
Server selbst. Nur nach Gefuehl "grosse Dateien" in den Prozess zu geben,
traefe nicht: ein echter Scan mit 42 MB liest sich in unter einer Sekunde,
und wie viel Datenmuell ein PDF traegt, sieht man ihm vorher nicht an.

Eine Datei, die die Zeitgrenze einmal gesprengt hat, merkt sich der Prozess
(``_ZU_LANGSAM``): sie wird nicht noch einmal gelesen, die Maske zeigt
sofort die Handeingabe, und das Buchen wartet nicht erneut.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.logik.beleg.ergebnis import BelegFehler

#: So lange darf die Textschicht EINES PDF dauern. Ein echter Scan mit 42 MB
#: braucht unter einer Sekunde, 500 gewoehnliche Rechnungen 25 Sekunden.
ZEITGRENZE_SEKUNDEN = 30

#: So lange darf der Start des Arbeitsprozesses dauern (Python, pdfplumber).
STARTZEIT_SEKUNDEN = 60

#: So viele zu langsame Dateien merkt sich der Prozess hoechstens.
ZU_LANGSAM_HOECHSTENS = 256


class ZuLangsam(BelegFehler):
    """Die Textschicht liess sich nicht in der Zeitgrenze lesen."""


class Abgebrochen(Exception):
    """Der Lauf soll aufhoeren (Programm wird beendet)."""


def _wurzel() -> str:
    """Der Ordner, aus dem ``app`` geladen wurde -- der Prozess soll denselben
    Code laden, auch wenn das Werkzeug aus einem anderen Ordner gestartet ist."""
    import app

    return str(Path(app.__file__).resolve().parent.parent)


class Textdienst:
    """Ein Arbeitsprozess fuer die Textschicht; ``text`` wie ``hole_text``."""

    def __init__(self, *, zeitgrenze: float | None = None,
                 stopp: threading.Event | None = None) -> None:
        self._zeitgrenze = zeitgrenze
        self.stopp = stopp
        self._prozess: subprocess.Popen[bytes] | None = None

    @property
    def zeitgrenze(self) -> float:
        """Die eigene Grenze -- sonst ``ZEITGRENZE_SEKUNDEN`` (zur Zeit des Lesens:
        der Dienst der Pruefmaske lebt laenger als eine Einstellung)."""
        return ZEITGRENZE_SEKUNDEN if self._zeitgrenze is None else self._zeitgrenze

    def _starten(self) -> subprocess.Popen[bytes]:
        umgebung = dict(os.environ)
        umgebung["PYTHONPATH"] = os.pathsep.join(
            [_wurzel(), *([umgebung["PYTHONPATH"]] if umgebung.get("PYTHONPATH") else [])])
        return subprocess.Popen(
            [sys.executable, "-m", "app.logik.beleg.textdienst"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=umgebung,
        )

    def text(self, pfad: Path):
        """Die Textschicht von ``pfad`` -- ``ZuLangsam`` nach der Zeitgrenze."""
        from app.logik.beleg.text_leser import Textschicht

        bekannt = _schon_zu_langsam(pfad)
        if bekannt is not None:
            raise ZuLangsam(bekannt)
        if self._prozess is None or self._prozess.poll() is not None:
            self._prozess = self._starten()
            # Die Zeitgrenze gilt fuer das Lesen, nicht fuer den Start des
            # Prozesses (Python und pdfplumber laden): erst auf "bereit" warten.
            self._antwort(self._prozess, zeitgrenze=STARTZEIT_SEKUNDEN)
        prozess = self._prozess
        try:
            prozess.stdin.write(json.dumps({"pfad": str(pfad)}).encode("utf-8") + b"\n")
            prozess.stdin.flush()
        except OSError as fehler:
            self.beenden()
            raise BelegFehler(f"Die Textschicht ließ sich nicht lesen: {fehler}") from fehler
        try:
            zeile = self._antwort(prozess)
        except ZuLangsam as fehler:
            _merke_zu_langsam(pfad, str(fehler))
            raise
        try:
            antwort = json.loads(zeile)
        except ValueError as fehler:
            self.beenden()
            raise BelegFehler("Die Textschicht ließ sich nicht lesen.") from fehler
        if "fehler" in antwort:
            raise BelegFehler(antwort["fehler"])
        return Textschicht(**antwort["ok"])

    def _antwort(self, prozess: subprocess.Popen[bytes], *,
                 zeitgrenze: float | None = None) -> bytes:
        grenze = self.zeitgrenze if zeitgrenze is None else zeitgrenze
        ende = time.monotonic() + grenze
        with selectors.DefaultSelector() as auswahl:
            auswahl.register(prozess.stdout, selectors.EVENT_READ)
            while True:
                if self.stopp is not None and self.stopp.is_set():
                    self.beenden()
                    raise Abgebrochen("Das Programm wird beendet.")
                rest = ende - time.monotonic()
                if rest <= 0:
                    self.beenden()
                    raise ZuLangsam(
                        f"Die Textschicht ließ sich nicht in {grenze:g} Sekunden lesen "
                        "— das PDF ist ungewöhnlich aufgebaut (etwa Datenreste am Dateiende)"
                    )
                if auswahl.select(timeout=min(rest, 0.5)):
                    zeile = prozess.stdout.readline()
                    if not zeile:
                        self.beenden()
                        raise BelegFehler("Die Textschicht ließ sich nicht lesen.")
                    return zeile

    def beenden(self) -> None:
        """Den Arbeitsprozess beenden (auch mitten in einer Datei)."""
        prozess, self._prozess = self._prozess, None
        if prozess is None:
            return
        try:
            prozess.kill()
            prozess.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
        for strom in (prozess.stdin, prozess.stdout):
            try:
                strom.close()
            except OSError:
                pass


#: Dateien, deren Textschicht die Zeitgrenze gesprengt hat: Geraet, Inode,
#: Groesse und Aenderungszeit -> Meldung. Nur im Speicher; nach einem
#: Neustart kostet dieselbe Datei die Zeitgrenze noch einmal.
_ZU_LANGSAM: dict[tuple[int, int, int, int], str] = {}
_ZU_LANGSAM_SPERRE = threading.Lock()


def _kennung(pfad: Path) -> tuple[int, int, int, int] | None:
    try:
        info = os.stat(pfad)
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _merke_zu_langsam(pfad: Path, meldung: str) -> None:
    kennung = _kennung(pfad)
    if kennung is None:
        return
    with _ZU_LANGSAM_SPERRE:
        _ZU_LANGSAM[kennung] = meldung
        while len(_ZU_LANGSAM) > ZU_LANGSAM_HOECHSTENS:
            del _ZU_LANGSAM[next(iter(_ZU_LANGSAM))]


def _schon_zu_langsam(pfad: Path) -> str | None:
    """Die Meldung, wenn genau diese Datei die Zeitgrenze schon gesprengt hat."""
    kennung = _kennung(pfad)
    if kennung is None:
        return None
    with _ZU_LANGSAM_SPERRE:
        return _ZU_LANGSAM.get(kennung)


# ------------------------------------------------- Der Dienst der Pruefmaske

#: Der Arbeitsprozess der Pruefmaske -- einer fuer alle Anfragen, erst beim
#: ersten PDF gestartet und danach stehen gelassen.
_GETEILT = Textdienst()
_GETEILT_SPERRE = threading.Lock()


def text_mit_zeitgrenze(pfad: Path):
    """Die Textschicht fuer Pruefmaske und Buchen -- ``ZuLangsam`` nach der Grenze.

    Liest im stehenden Prozess. Ist der gerade mit einer anderen Anfrage
    beschaeftigt (zweiter Tab, waehrend der erste ein langsames PDF liest),
    wartet diese Anfrage nicht bis zu dessen Zeitgrenze, sondern liest in
    einem eigenen Prozess, der danach endet. Laesst sich gar kein Prozess
    starten (``OSError`` beim Start), liest sie wie frueher im Server selbst
    -- sonst hiesse jede Maske "liegt nicht im Eingang".
    """
    try:
        if _GETEILT_SPERRE.acquire(blocking=False):
            try:
                return _GETEILT.text(pfad)
            finally:
                _GETEILT_SPERRE.release()
        einmal = Textdienst()
        try:
            return einmal.text(pfad)
        finally:
            einmal.beenden()
    except OSError:
        from app.logik.beleg.text_leser import hole_text

        return hole_text(pfad)


def geteilt_beenden(*, warten: float = 1.0) -> None:
    """Beendet den Prozess der Pruefmaske (beim Beenden des Programms).

    Liest er noch, bleibt er stehen -- endet das Programm, liest er das Ende
    seiner Eingabe und hoert von selbst auf.
    """
    if _GETEILT_SPERRE.acquire(timeout=warten):
        try:
            _GETEILT.beenden()
        finally:
            _GETEILT_SPERRE.release()


def _arbeiten() -> None:
    """Der Arbeitsprozess: je Zeile ein Pfad, je Zeile eine Antwort."""
    import pdfplumber  # noqa: F401 -- vor "bereit" geladen, nicht in der Zeitgrenze

    from app.logik.beleg.text_leser import hole_text

    sys.stdout.buffer.write(b'{"bereit": true}\n')
    sys.stdout.buffer.flush()
    for zeile in sys.stdin.buffer:
        try:
            pfad = Path(json.loads(zeile)["pfad"])
            schicht = hole_text(pfad)
            antwort = {"ok": {"text": schicht.text, "seiten": schicht.seiten,
                              "zeichen_je_seite": schicht.zeichen_je_seite,
                              "brauchbar": schicht.brauchbar,
                              "begruendung": schicht.begruendung}}
        except Exception as fehler:  # noqa: BLE001 -- jede Ursache als Antwort
            antwort = {"fehler": str(fehler) or "Die Textschicht ließ sich nicht lesen."}
        sys.stdout.buffer.write(json.dumps(antwort, ensure_ascii=False).encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    _arbeiten()

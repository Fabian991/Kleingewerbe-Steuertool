import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import db  # noqa: E402


def braucht(*programme: str) -> pytest.MarkDecorator:
    """Ueberspringt ein Testmodul, wenn ein Hilfsprogramm fehlt.

    Betrifft die PDF-Tests: sie messen am FERTIGEN Dokument mit ``pdftotext``
    und ``pdffonts`` aus poppler, statt dem CSS zu glauben. Das ist richtig so
    -- aber poppler liegt auf einem Mac nicht bei, und ohne diesen Schalter
    scheitern die Tests dort mit einem FileNotFoundError, der nach einem Fehler
    im Werkzeug aussieht und keiner ist.

    Uebersprungen heisst NICHT bestanden: die Begruendung nennt den Befehl, mit
    dem sich die Luecke schliessen laesst.
    """
    fehlt = [name for name in programme if shutil.which(name) is None]
    return pytest.mark.skipif(
        bool(fehlt),
        reason=(
            f"{', '.join(fehlt)} fehlt (poppler). Diese Pruefung misst am fertigen "
            f"PDF und kann ohne sie nicht laufen. Auf dem Mac: brew install poppler"
        ),
    )


@pytest.fixture
def conn(tmp_path):
    verbindung = db.oeffne(tmp_path / "test.sqlite")
    yield verbindung
    verbindung.close()


@pytest.fixture
def kunde_id(conn):
    cur = conn.execute(
        """
        INSERT INTO kunde (kundennummer, name, strasse, plz, ort, angelegt_am)
        VALUES ('K-0001', 'Mustermann GmbH', 'Hauptstr. 1', '10115', 'Berlin', ?)
        """,
        (db.heute(),),
    )
    return cur.lastrowid


@pytest.fixture
def betrieb(conn):
    conn.execute(
        """
        INSERT INTO betrieb (id, name, strasse, plz, ort, steuernummer, standardsatz_cent)
        VALUES (1, 'Mein Kleingewerbe', 'Werkweg 2', '20095', 'Hamburg', '12/345/67890', 8000)
        """
    )
    return 1


@pytest.fixture(autouse=True)
def _kein_netz(monkeypatch):
    """Kein Test geht ins Netz.

    Der Abruf der EZB-Kurstabelle laeuft ueber ``kurse.netzabruf``; hier wird
    er durch einen ersetzt, der sofort scheitert -- wie ohne Internet. Tests,
    die eine Tabelle brauchen, legen die kleine Pruefdatei ab oder setzen
    einen eigenen Abruf. Die Umgebungsvariable sperrt den echten Abruf auch
    dort, wo der Ersatz nicht hinreicht (ein Kindprozess).
    """
    from app.logik import kurse

    def offline(url: str) -> bytes:
        raise kurse.AbrufFehler("Die Kurstabelle ließ sich nicht laden: kein Netz im Test.")

    monkeypatch.setattr(kurse, "netzabruf", offline)
    monkeypatch.setenv(kurse.OHNE_NETZ, "1")
    kurse._FEHLSCHLAG.clear()
    kurse._ERFOLG.clear()
    yield
    kurse._FEHLSCHLAG.clear()
    kurse._ERFOLG.clear()

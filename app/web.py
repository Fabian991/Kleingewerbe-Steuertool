"""Gemeinsame Bausteine der Weboberflaeche: Vorlagen, Verbindung, Filter."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import db
from app.filter import registriere

VORLAGEN = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "vorlagen"))
registriere(VORLAGEN.env)


def hole_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Eine Verbindung pro Anfrage.

    FastAPI fuehrt synchrone Routen in einem Threadpool aus -- und zwar
    Aufbau (dieser Generator bis zum ``yield``), den Endpunkt und das
    Schliessen (nach dem ``yield``) in je einem BELIEBIGEN Thread des Pools.
    Laufen Anfragen nacheinander, ist es meist derselbe; laufen zwei
    gleichzeitig (Doppelklick, zwei Tabs), ist es das nicht. sqlite3 bindet
    eine Verbindung in der Vorbelegung an den Thread, der sie geoeffnet hat,
    und wirft sonst einen ProgrammingError -- der Nutzer sah "Da ist etwas
    schiefgegangen" statt der Meldung "bereits festgeschrieben".

    Deshalb ``threadfrei``: die Verbindung ist je Anfrage privat und wird
    nie gleichzeitig in zwei Threads benutzt, nur nacheinander in
    verschiedenen. Das ist fuer SQLite unbedenklich; ``busy_timeout`` bleibt
    und ordnet gleichzeitige Schreibzugriffe verschiedener Anfragen.
    """
    verbindung = db.verbinde(request.app.state.datenpfad, threadfrei=True)
    try:
        yield verbindung
    finally:
        verbindung.close()


def betrieb_oder_none(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()

def filterzahl(roh: str) -> int | None:
    """Ein Filterwert aus der Adresse; leer oder Unsinn heisst "nicht einschraenken".

    Das Filterformular sendet fuer "alle" einen leeren Wert (``jahr=``). Als
    ``int | None`` abgefragt, antwortete FastAPI darauf mit der 422-Seite --
    der Filter funktionierte nur, wenn Jahr UND Kategorie gewaehlt waren.

    >>> filterzahl(""), filterzahl(" 2026 "), filterzahl("abc"), filterzahl("-1")
    (None, 2026, None, None)
    >>> filterzahl("²"), filterzahl("9" * 19)
    (None, None)
    """
    roh = (roh or "").strip()
    # isascii(): "²".isdigit() ist wahr, int("²") scheitert trotzdem.
    if not (roh.isascii() and roh.isdigit()) or len(roh) > 18:
        return None
    return int(roh)


def dateikopf(name: str, *, herunterladen: bool) -> dict[str, str]:
    """Content-Disposition: im Browser anzeigen (inline) oder herunterladen.

    Der Link heisst "Datei" bzw. "PDF öffnen" -- mit ``attachment`` startete
    der Browser stattdessen einen Download. Der Name steht zweimal: als
    ASCII-Ersatz fuer alte Programme und als ``filename*`` (RFC 6266) mit
    Umlauten.

    Genutzt von der Belegdatei und der Rechnungs-PDF.

    >>> print(dateikopf("Büro März.pdf", herunterladen=False)["content-disposition"])
    inline; filename="Buro Marz.pdf"; filename*=utf-8''B%C3%BCro%20M%C3%A4rz.pdf
    >>> print(dateikopf('a"b.pdf', herunterladen=True)["content-disposition"])
    attachment; filename="a_b.pdf"; filename*=utf-8''a%22b.pdf
    """
    ersatz = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ersatz = re.sub(r'["\\\x00-\x1f\x7f]', "_", ersatz).strip() or "datei"
    art = "attachment" if herunterladen else "inline"
    return {"content-disposition":
            f"{art}; filename=\"{ersatz}\"; filename*=utf-8''{quote(name, safe='')}"}

"""Das Journal als Klartextdatei.

Ein Protokoll, das man nur mit der Anwendung lesen kann, verfehlt seinen Zweck:
im Pruefungs- oder Schadensfall ist die Anwendung genau das, was fehlt. Deshalb
ein Spiegel als Textdatei.

Bewusst **auf Abruf** geschrieben und nicht bei jedem Protokolleintrag: ein
Eintrag entsteht innerhalb einer Transaktion, und ein Rollback wuerde die Zeile
in der Datei stehen lassen. Eine Datei, die Vorgaenge behauptet, die es nicht
gab, ist schlechter als keine. Geschrieben wird daher beim Sichern und auf
Knopfdruck -- immer vollstaendig neu aus der Tabelle.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

DATEINAME = "journal.log"

_KOPF = (
    "# Aenderungsprotokoll Steuer-Tool\n"
    "#\n"
    "# Vollstaendig aus der Tabelle 'journal' erzeugt. Die Tabelle ist per Trigger\n"
    "# gegen UPDATE und DELETE gesperrt und kann nur wachsen (§ 146 Abs. 4 AO).\n"
    "# Diese Datei ist ein Spiegel zum Lesen ohne die Anwendung, nicht die Quelle.\n"
    "#\n"
    "# Spalten: Zeitpunkt (UTC) | Aktion | Objekt | Feld | alt -> neu | Begruendung | Hash\n"
    "\n"
)


#: Eintraege, deren Werte das vollstaendige Abbild eines geloeschten
#: Datensatzes sind (app/logik/beleg/loeschen.py, belegzahlung.py). Gekuerzt
#: waeren sie im Spiegel nicht mehr feststellbar -- und gerade fuer sie ist der
#: Spiegel da, falls die Anwendung fehlt. Dasselbe gilt fuer eine
#: zurueckgenommene Rechnung (alter Wert: Inhalt samt Positionen und Zeiten;
#: neuer Wert: ob sie wieder Entwurf oder ganz geloescht ist und wo ihr PDF
#: aufbewahrt liegt, app/logik/rechnungsablauf.py), einen verworfenen
#: Entwurf, jedes LOESCHEN -- etwa einen Kunden samt seinen Zeiten und
#: Entwuerfen -- und weggeworfene Eingangsdateien (Originalname und SHA-256
#: je Datei, app/logik/beleg/sammelbuchung.py). Ungekuerzt sind dann BEIDE
#: Werte, der alte und der neue.
UNGEKUERZT = frozenset({
    "BELEG_GELOESCHT", "ANLAGEGUT_GELOESCHT", "ZAHLUNG_ZURUECK",
    "RECHNUNG_ZURUECKGENOMMEN", "ENTWURF_VERWORFEN", "LOESCHEN",
    "EINGANG_VERWORFEN",
})


def _feld(wert: object, *, ganz: bool = False) -> str:
    """Ein Feld fuer die Zeile: einzeilig, ohne Spaltentrenner, sonst auf 300
    Zeichen gekuerzt -- ausser ``ganz``.

    >>> len(_feld("x" * 400)), len(_feld("x" * 400, ganz=True))
    (300, 400)
    """
    if wert is None:
        return ""
    text = str(wert).replace("\n", " ").replace("|", "/")
    return text if ganz or len(text) <= 300 else text[:299] + "…"


def schreibe(conn: sqlite3.Connection, ziel: Path) -> Path:
    """Schreibt den Spiegel neu. Gibt den Pfad zurueck.

    Ersetzt wird ATOMAR: erst eine eigene Arbeitsdatei daneben, dann
    ``os.replace``. Vorher schrieb ``write_text`` direkt in die Datei; zwei
    gleichzeitige Sicherungen schrieben dann beide hinein, und die
    Journalseite konnte zwischendurch einen halben Spiegel lesen und als
    veraltet melden.
    """
    ziel.parent.mkdir(parents=True, exist_ok=True)
    zeilen = [_KOPF]
    for z in conn.execute("SELECT * FROM journal ORDER BY id"):
        objekt = f"{z['objekttyp']}#{z['objekt_id']}" if z["objekt_id"] else z["objekttyp"]
        wechsel = ""
        if z["wert_alt"] is not None or z["wert_neu"] is not None:
            ganz = z["aktion"] in UNGEKUERZT
            wechsel = (f"{_feld(z['wert_alt'], ganz=ganz)} -> "
                       f"{_feld(z['wert_neu'], ganz=ganz)}")
        zeilen.append(
            " | ".join((
                z["zeitpunkt_utc"], z["aktion"], objekt, _feld(z["feld"]), wechsel,
                _feld(z["begruendung"]), _feld(z["hash"]),
            )) + "\n"
        )
    griff, arbeitsname = tempfile.mkstemp(
        prefix=f".{ziel.name}.", suffix=".teil", dir=ziel.parent
    )
    arbeit = Path(arbeitsname)
    try:
        with os.fdopen(griff, "w", encoding="utf-8") as datei:
            datei.write("".join(zeilen))
        # mkstemp legt 0600 an; der Spiegel ist eine gewoehnliche Textdatei.
        os.chmod(arbeit, 0o644)
        os.replace(arbeit, ziel)
    except BaseException:
        arbeit.unlink(missing_ok=True)
        raise
    return ziel


def spiegel_eintraege(ziel: Path) -> int | None:
    """Wie viele Vorgaenge im Spiegel stehen. ``None``, wenn es ihn nicht gibt.

    Die Oberflaeche vergleicht die Zahl mit der Tabelle und zeigt damit an, ob
    der Spiegel veraltet ist. Ein Spiegel, von dem man nicht weiss, wie alt er
    ist, nuetzt nichts.
    """
    if not ziel.exists():
        return None
    try:
        text = ziel.read_text(encoding="utf-8")
    except OSError:
        return None
    return sum(1 for zeile in text.splitlines() if zeile and not zeile.startswith("#"))


def zeilen(
    conn: sqlite3.Connection,
    *,
    objekttyp: str | None = None,
    aktion: str | None = None,
    grenze: int = 500,
) -> list[sqlite3.Row]:
    bedingungen: list[str] = []
    werte: list[object] = []
    if objekttyp:
        bedingungen.append("objekttyp = ?")
        werte.append(objekttyp)
    if aktion:
        bedingungen.append("aktion = ?")
        werte.append(aktion)
    wo = f"WHERE {' AND '.join(bedingungen)}" if bedingungen else ""
    werte.append(grenze)
    return conn.execute(
        f"SELECT * FROM journal {wo} ORDER BY id DESC LIMIT ?", werte
    ).fetchall()


def arten(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    typen = [z["objekttyp"] for z in conn.execute(
        "SELECT DISTINCT objekttyp FROM journal ORDER BY objekttyp")]
    aktionen = [z["aktion"] for z in conn.execute(
        "SELECT DISTINCT aktion FROM journal ORDER BY aktion")]
    return typen, aktionen

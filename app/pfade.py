"""Dateipfade in der Datenbank: relativ zum Datenordner.

Der Datenordner ist die Einheit, die gesichert, verschoben und
wiederhergestellt wird -- auf einen neuen Rechner, in ein anderes
Benutzerkonto, von einer externen Platte zurueck. Ein absoluter Pfad in der
Datenbank (``/Users/alt/Library/.../belege/2026/x.pdf``) ueberlebt keinen
dieser Schritte: nach der Wiederherstellung an anderer Stelle zeigte jeder
Beleg und jedes Rechnungs-PDF ins Leere, und weil ``belegdatei.pfad`` per
Trigger unveraenderlich ist, war das nicht einmal reparierbar.

Deshalb steht in der Datenbank der Pfad RELATIV zum Datenordner
(``belege/2026/x.pdf``), und aufgeloest wird er beim Lesen gegen den
Datenordner, der gerade gilt. Absolute Pfade bleiben lesbar -- fuer Dateien,
die ausserhalb des Datenordners liegen, und fuer Altbestaende, die eine
Migration nicht umschreiben konnte.
"""

from __future__ import annotations

from pathlib import Path


def relativ(datenordner: Path, pfad: Path) -> str:
    """Der Pfad, wie er in die Datenbank gehoert.

    Relativ zum Datenordner, wenn die Datei darin liegt; sonst absolut, denn
    ein relativer Pfad, der nirgends hinfuehrt, waere schlimmer als ein
    absoluter, der wenigstens heute stimmt.

    >>> relativ(Path("/daten"), Path("/daten/belege/2026/a.pdf"))
    'belege/2026/a.pdf'
    >>> relativ(Path("/daten"), Path("/woanders/a.pdf"))
    '/woanders/a.pdf'
    """
    try:
        return str(Path(pfad).resolve().relative_to(Path(datenordner).resolve()))
    except ValueError:
        return str(pfad)


def absolut(datenordner: Path, gespeichert: str) -> Path:
    """Der gespeicherte Pfad als Datei auf diesem Rechner.

    >>> absolut(Path("/daten"), "belege/2026/a.pdf")
    PosixPath('/daten/belege/2026/a.pdf')
    >>> absolut(Path("/daten"), "/woanders/a.pdf")
    PosixPath('/woanders/a.pdf')
    """
    pfad = Path(gespeichert)
    return pfad if pfad.is_absolute() else Path(datenordner) / pfad

"""Sicherung und Rückprüfung des gesamten Datenbestands.

Bei lokalem Betrieb ist das keine Nebensache: ein Plattendefekt kostet die
komplette Buchführung. § 147 Abs. 3 AO staffelt die Fristen — **zehn Jahre** für
Bücher und Aufzeichnungen, **acht** für Buchungsbelege, sechs für den Rest —,
und das Archiv enthält beides. Maßgeblich ist deshalb die längere: zehn Jahre.
Sie läuft zudem nicht ab, solange die Festsetzungsfrist der Steuer noch offen
ist (§ 147 Abs. 3 Satz 5 AO), kann also länger dauern.

Zwei Dinge sind hier leicht falsch zu machen:

* **Eine Dateikopie bei laufendem Programm kann eine unbrauchbare Datenbank
  ergeben.** SQLite schreibt in Seiten und führt ein WAL; wer die Datei
  mitten in einer Transaktion kopiert, erhält einen Zwischenstand. Deshalb
  ``VACUUM INTO``: das schreibt eine in sich geschlossene, konsistente Kopie.
* **Ein ungeprüftes Backup ist kein Backup.** Ob das Archiv lesbar ist, weiß
  man erst, wenn man es geöffnet hat -- und dann ist es zu spät, wenn nicht.
  ``pruefe()`` entpackt es, öffnet die Datenbank und zählt nach.

Jedes Archiv enthält deshalb drei Dinge, die sich gegenseitig stützen: die
Datenbank, die Belege und Rechnungs-PDF im Original, und eine
Tabellenkalkulation mit allen Daten im Klartext. Die SQLite-Datei nützt nur
etwas, solange es noch ein Programm gibt, das sie öffnet; die .xlsx öffnet
jeder Rechner. Die Frist oben ist lang genug, dass diese Unterscheidung zählt.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app import db
from app.logik import eingabe, export_excel, journal_export
from app.pdf import pruefsumme

#: Name der Datenbank im Archiv.
DB_IM_ARCHIV = "steuer.sqlite"

#: Name der Tabellenkalkulation im Archiv.
EXCEL_IM_ARCHIV = export_excel.DATEINAME

#: Diese Ordner wandern NICHT ins Archiv. Ohne diesen Ausschluss enthielte jedes
#: Backup alle vorherigen, und das Archiv würde mit jedem Lauf doppelt so groß.
AUSGESCHLOSSEN = {"backup"}

#: So heissen die Archive dieses Werkzeugs. Dateien mit diesem Muster wandern
#: nie ins naechste Archiv, wo immer sie im Datenordner liegen: ein frueher
#: eingestellter Zielordner, ein von Hand hineinkopiertes ZIP -- jedes davon
#: liesse die Sicherungen sonst mit jedem Lauf wachsen.
ARCHIVMUSTER = "*_steuer-backup*.zip"

#: Meldung, wenn der Datenordner selbst oder ein Ordner darueber als Ziel
#: gewaehlt wird. Dann traefe der Ausschluss des Zielordners JEDE Datei des
#: Datenordners, und das Archiv enthielte weder Belege noch Rechnungs-PDF --
#: bei gruener Pruefung.
ZIEL_IM_DATENORDNER = (
    "Die Sicherung darf nicht in den Datenordner oder darüber — bitte einen Ordner "
    "außerhalb wählen."
)

#: Ab diesem Alter mahnt die Oberfläche ein neues Backup an.
WARNUNG_AB_TAGEN = 7

#: Wo Laufwerke eingehaengt werden: (Wurzel, Tiefe des Einhaengeordners).
#: ``/Volumes/<Name>`` (macOS), ``/media/<Name>`` und ``/mnt/<Name>``,
#: ``/run/media/<Benutzer>/<Name>`` (Linux). Nur unter diesen Ordnern heisst
#: "Ordner fehlt": das Laufwerk ist nicht da. Auf der internen Platte darf die
#: ganze Kette angelegt werden.
EINHAENGEPUNKTE: tuple[tuple[Path, int], ...] = (
    (Path("/Volumes"), 1),
    (Path("/media"), 1),
    (Path("/mnt"), 1),
    (Path("/run/media"), 2),
)

#: So viele Sicherungen am selben Tag im selben Ordner, dann ist Schluss.
#: Nur ein Netz gegen eine Endlosschleife; im Betrieb sind es eine oder zwei.
LAEUFE_HOECHSTENS = 10_000


class LaufwerkFehlt(FileNotFoundError):
    """Der Zielordner liegt auf einem Laufwerk, das gerade nicht eingehaengt ist."""


#: Systemfehler in Worten. Die Meldung geht an jemanden, der kein Entwickler
#: ist; "[Errno 2] No such file or directory" hilft dem nicht.
_SYSTEMFEHLER = {
    errno.ENOENT: "die Datei oder der Ordner fehlt",
    errno.EACCES: "keine Berechtigung",
    errno.EPERM: "keine Berechtigung",
    errno.ENOSPC: "die Platte ist voll",
    errno.EDQUOT: "der Speicherplatz des Benutzerkontos ist erschöpft",
    errno.EROFS: "das Laufwerk ist schreibgeschützt",
    errno.ENAMETOOLONG: "der Pfad ist zu lang",
    errno.ENOTDIR: "ein Teil des Pfads ist eine Datei, kein Ordner",
    errno.EISDIR: "das ist ein Ordner, keine Datei",
    errno.EEXIST: "die Datei gibt es schon",
    errno.EIO: "Lesefehler auf dem Laufwerk",
    errno.EBUSY: "das Laufwerk ist gerade belegt",
    errno.ELOOP: "der Pfad verweist im Kreis",
}


def fehlertext(fehler: BaseException) -> str:
    """Ein Fehler als deutscher Satz, ohne "[Errno …]" und ohne Python-Namen.

    >>> fehlertext(FileNotFoundError(2, "No such file or directory", "/x/a.zip"))
    'die Datei oder der Ordner fehlt (/x/a.zip).'
    >>> fehlertext(OSError(28, "No space left on device"))
    'die Platte ist voll.'
    >>> fehlertext(ValueError("Bitte einen Ordner wählen."))
    'Bitte einen Ordner wählen.'
    """
    if isinstance(fehler, OSError) and fehler.errno is not None:
        text = _SYSTEMFEHLER.get(fehler.errno, f"Systemfehler Nr. {fehler.errno}")
        name = fehler.filename
        return f"{text} ({name})." if name else f"{text}."
    if isinstance(fehler, sqlite3.OperationalError) and "locked" in str(fehler):
        return "die Datenbank ist gerade belegt — bitte gleich noch einmal versuchen."
    if isinstance(fehler, sqlite3.Error):
        return "die Datenbank hat den Vorgang abgewiesen."
    text = str(fehler).strip() or "unbekannter Fehler"
    return text if text.endswith((".", "?", "!")) else f"{text}."


def _pfad(roh: str) -> Path:
    """Ein gespeicherter Pfad mit aufgeloester Tilde -- oder wie er dasteht.

    ``expanduser`` wirft bei "~unbekannt/..." einen RuntimeError. Eine
    Einstellung von frueher darf die Sicherungsseite nicht lahmlegen.
    """
    try:
        return Path(roh).expanduser()
    except RuntimeError:
        return Path(roh)


@dataclass(frozen=True, slots=True)
class Archiv:
    pfad: Path
    groesse: int
    sha256: str
    dateien: int
    #: Wohin eine Zweitkopie gelegt wurde, falls ein zweites Ziel eingestellt ist.
    zweitkopie: Path | None = None
    #: Warum die Zweitkopie nicht entstand -- meist: externe Platte nicht da.
    zweitkopie_fehler: str | None = None
    #: Warum die Tabellenkalkulation nicht im Archiv liegt, falls sie fehlt.
    excel_fehler: str | None = None


@dataclass(frozen=True, slots=True)
class Pruefergebnis:
    pfad: Path
    heil: bool
    meldung: str
    rechnungen: int = 0
    zeiteintraege: int = 0
    belegdateien: int = 0


def zielordner(conn: sqlite3.Connection, datenordner: Path) -> Path:
    """Wohin gesichert wird. Standard: ``backup/`` im Datenordner."""
    zeile = conn.execute("SELECT backup_pfad FROM betrieb WHERE id = 1").fetchone()
    if zeile and zeile["backup_pfad"]:
        return _pfad(zeile["backup_pfad"])
    return datenordner / "backup"


def zweitziel(conn: sqlite3.Connection) -> Path | None:
    """Das zweite Ziel, falls eingestellt.

    Eine Sicherung auf derselben Platte wie die Daten schuetzt gegen Versehen,
    nicht gegen einen Plattendefekt. Und die Aufbewahrungsfrist laeuft zehn
    Jahre -- also ueber mehrere Rechner hinweg.
    """
    zeile = conn.execute("SELECT backup_zweitziel FROM betrieb WHERE id = 1").fetchone()
    if zeile and zeile["backup_zweitziel"]:
        return _pfad(zeile["backup_zweitziel"])
    return None


def zielfehler(datenordner: Path, ordner: Path) -> str | None:
    """Warum ein Ordner als Sicherungsziel nicht taugt -- ``None``, wenn er taugt.

    Der Datenordner selbst und jeder seiner Vorfahren sind tabu: der Ausschluss
    des Zielordners beim Packen traefe dann jede Datei des Datenordners, und
    das Archiv enthielte nur Datenbank und Tabelle. Unterordner des
    Datenordners bleiben erlaubt (``backup/`` ist der Standard) und werden
    beim Packen wie ``backup`` ausgelassen.
    """
    wurzel = Path(datenordner).expanduser().resolve()
    ziel = Path(ordner).expanduser().resolve()
    if ziel == wurzel or ziel in wurzel.parents:
        return ZIEL_IM_DATENORDNER
    return None


def einhaengeordner(ordner: Path) -> Path | None:
    """Der Einhaengeordner des Laufwerks, auf dem ``ordner`` liegt -- oder None.

    >>> einhaengeordner(Path("/Volumes/Backup/Steuer-Tool"))
    PosixPath('/Volumes/Backup')
    >>> einhaengeordner(Path("/run/media/erika/Platte/x"))
    PosixPath('/run/media/erika/Platte')
    >>> einhaengeordner(Path("/Users/erika/Sicherungen")) is None
    True
    """
    for wurzel, tiefe in EINHAENGEPUNKTE:
        try:
            rest = ordner.relative_to(wurzel)
        except ValueError:
            continue
        if len(rest.parts) < tiefe:
            return None
        return wurzel.joinpath(*rest.parts[:tiefe])
    return None


def ordner_anlegen(ordner: Path) -> None:
    """Legt einen Zielordner an, auf der internen Platte samt Elternkette.

    Auf einem Laufwerk (``/Volumes/<Name>`` usw.) aber nur, wenn dessen
    Einhaengeordner da ist: ``mkdir(parents=True)`` legte bei nicht
    eingehaengter Platte den ganzen Pfad ``/Volumes/Backup/Steuer-Tool`` als
    gewoehnlichen Ordner auf der Systemplatte an; die "Zweitkopie" lag dann
    neben dem Original, und beim naechsten Anschliessen haengt macOS die echte
    Platte unter "Backup 1" ein. Das soll als Fehler erscheinen, nicht als
    Erfolg. Ein verschachtelter neuer Ordner auf der internen Platte
    ("~/Sicherungen/Steuer-Tool") wird dagegen angelegt -- dort gibt es kein
    Laufwerk, das fehlen koennte.
    """
    if ordner.exists():
        if not ordner.is_dir():
            raise NotADirectoryError(f"{ordner} ist eine Datei, kein Ordner.")
        return
    platte = einhaengeordner(ordner)
    if platte is not None and not platte.is_dir():
        raise LaufwerkFehlt(f"der Ordner {platte} fehlt. Hängt das Laufwerk?")
    for vorfahr in ordner.parents:
        if vorfahr.exists():
            if not vorfahr.is_dir():
                raise NotADirectoryError(f"{vorfahr} ist eine Datei, kein Ordner.")
            break
    ordner.mkdir(parents=True, exist_ok=True)


def _lebende_datenbank(conn: sqlite3.Connection) -> set[Path]:
    """Die Datei der geoeffneten Datenbank samt WAL/SHM/Journal, aufgeloest.

    Ausgeschlossen wird die TATSAECHLICHE Datei aus ``PRAGMA database_list``,
    nicht nur eine, die ``steuer.sqlite`` heisst: heisst sie anders, laege
    sonst eine inkonsistente Dateikopie samt WAL im Archiv -- genau das, wovor
    die Modulbeschreibung warnt -- und beim Entpacken zwei Datenbanken
    nebeneinander.
    """
    for zeile in conn.execute("PRAGMA database_list"):
        if zeile["name"] == "main" and zeile["file"]:
            datei = Path(zeile["file"]).resolve()
            return {datei} | {datei.with_name(datei.name + endung)
                              for endung in ("-wal", "-shm", "-journal")}
    return set()


def _sperrpfade(wurzel: Path, ordner: list[Path | None]) -> list[Path]:
    """Ziel- und Zweitzielordner als Pfade RELATIV zum Datenordner.

    Nur die, die im Datenordner liegen; was ausserhalb liegt, kann das Packen
    ohnehin nicht treffen. Der Datenordner selbst ist vorher abgewiesen, denn
    ein Sperrpfad "." traefe alles.
    """
    gefunden = []
    for eintrag in ordner:
        if eintrag is None:
            continue
        try:
            relativ = Path(eintrag).expanduser().resolve().relative_to(wurzel)
        except ValueError:
            continue
        if relativ != Path("."):
            gefunden.append(relativ)
    return gefunden


def _ausgelassen(eintrag: Path, relativ: Path, sperrpfade: list[Path],
                 lebend: set[Path]) -> bool:
    """Ob eine Datei des Datenordners NICHT ins Archiv gehoert."""
    if relativ.parts[0] in AUSGESCHLOSSEN:
        return True
    # Der TATSAECHLICHE Zielordner und das Zweitziel, wie immer sie heissen.
    # Liegen sie im Datenordner (eine naheliegende Einstellung), packte das
    # Archiv sonst alle frueheren mit ein -- und sich selbst, waehrend es
    # geschrieben wird. Jede Sicherung waere doppelt so gross wie die vorige,
    # bis die Platte voll ist.
    if any(relativ.is_relative_to(sperre) for sperre in sperrpfade):
        return True
    # Und jedes Archiv dieses Werkzeugs, wo immer es liegt: aus einem frueher
    # eingestellten Zielordner, von Hand hineinkopiert, oder als liegen
    # gebliebene Arbeitsdatei.
    if fnmatch.fnmatch(relativ.name, ARCHIVMUSTER) or fnmatch.fnmatch(
        relativ.name, ARCHIVMUSTER + ".teil"
    ):
        return True
    # Die laufende Datenbank und ihre WAL-Dateien gehoeren nicht ins Archiv --
    # im Archiv liegt die konsistente Kopie. Und eine Tabelle, die eine
    # Wiederherstellung einmal in den Datenordner gelegt hat, ebenso wenig:
    # sie entsteht neu.
    if relativ.name.startswith(DB_IM_ARCHIV) or relativ.name == EXCEL_IM_ARCHIV:
        return True
    # Der Journal-Spiegel kommt frisch aus dem Arbeitsordner (siehe erzeuge);
    # der auf der Platte koennte gerade von einer gleichzeitigen Sicherung
    # neu geschrieben werden und halb sein.
    if relativ == Path(journal_export.DATEINAME):
        return True
    if lebend and any(eintrag.name == datei.name for datei in lebend):
        return eintrag.resolve() in lebend
    return False


def erzeuge(
    conn: sqlite3.Connection,
    datenordner: Path,
    ziel: Path | None = None,
    *,
    datumsstempel: str | None = None,
) -> Archiv:
    """Schreibt ein vollständiges Archiv aus Datenbank und Belegen.

    Die Datenbank wird per ``VACUUM INTO`` konsistent herausgeschrieben, nicht
    kopiert. Danach wandern Datenbankkopie und alle Belegordner in ein ZIP.
    """
    # VACUUM laeuft nicht innerhalb einer Transaktion -- SQLite weist das ab.
    # Deshalb oeffnet DIESE Funktion die Transaktion selbst, erst nach dem
    # Schreiben des Archivs, und der Aufrufer darf keine offene haben.
    if conn.in_transaction:
        raise RuntimeError(
            "erzeuge() braucht eine Verbindung ohne offene Transaktion: "
            "VACUUM laeuft nicht innerhalb einer Transaktion."
        )

    # Absolut und aufgeloest: backup_log traegt den Pfad, und ein relativer
    # haengt vom Arbeitsverzeichnis ab -- aus einem anderen gestartet, "fehlte"
    # jede Sicherung.
    roh = ziel or zielordner(conn, datenordner)
    try:
        ordner = roh.expanduser().resolve()
    except RuntimeError as fehler:
        # expanduser: "~unbekannt/..." -- eine Einstellung von frueher.
        raise OSError(
            f"Der Zielordner {roh} lässt sich nicht auflösen: den Benutzer hinter der "
            "Tilde gibt es auf diesem Rechner nicht. Bitte unter „Zielordner“ einen "
            "vollständigen Pfad eintragen."
        ) from fehler
    except OSError as fehler:
        raise OSError(
            f"Der Zielordner {roh} lässt sich nicht auflösen: {fehlertext(fehler)} "
            "Bitte unter „Zielordner“ einen vollständigen Pfad eintragen."
        ) from fehler
    grund = zielfehler(datenordner, ordner)
    if grund:
        raise ValueError(grund)
    try:
        ordner_anlegen(ordner)
    except OSError as fehler:
        raise OSError(
            f"Der Zielordner {ordner} lässt sich nicht anlegen: {fehlertext(fehler)}"
        ) from fehler

    stempel = datumsstempel or db.heute()
    zweites = zweitziel(conn)
    wurzel = Path(datenordner).expanduser().resolve()
    sperrpfade = _sperrpfade(wurzel, [ordner, zweites])
    lebend = _lebende_datenbank(conn)

    with tempfile.TemporaryDirectory() as tmp:
        kopie = Path(tmp) / DB_IM_ARCHIV
        # VACUUM INTO verlangt, dass die Zieldatei nicht existiert.
        conn.execute("VACUUM INTO ?", (str(kopie),))

        # Die Tabellenkalkulation ist der Notausgang: sie macht den Bestand
        # lesbar, ohne dieses Werkzeug. Scheitert sie, darf die Sicherung
        # trotzdem entstehen -- die Datenbank im Archiv traegt alles, und ein
        # Archiv ohne Beiblatt ist besser als gar keines.
        tabelle: Path | None = Path(tmp) / EXCEL_IM_ARCHIV
        excel_fehler: str | None = None
        try:
            export_excel.schreibe(conn, tabelle)
        except Exception as fehler:  # noqa: BLE001 -- geht als Hinweis an den Nutzer
            tabelle, excel_fehler = None, fehlertext(fehler)

        # Der Klartext-Spiegel des Journals, damit das Archiv ein Protokoll
        # enthaelt, das ohne diese Anwendung lesbar ist -- im Ernstfall fehlt
        # genau sie. Er entsteht im Arbeitsordner dieses Laufs: der Spiegel
        # auf der Platte kann gerade von einer gleichzeitigen Sicherung
        # (Doppelklick) neu geschrieben werden.
        spiegel: Path | None = Path(tmp) / journal_export.DATEINAME
        try:
            journal_export.schreibe(conn, spiegel)
        except OSError:
            # Ein nicht schreibbarer Spiegel darf die Sicherung nicht
            # verhindern: die Datenbank im Archiv traegt das Journal.
            spiegel = None

        # Den Namen RESERVIEREN, bevor geschrieben wird. "while pfad.exists()"
        # liess zwei gleichzeitige Sicherungen (Doppelklick) denselben Namen
        # waehlen: eine scheiterte mit englischem Systemtext, die andere
        # meldete "geschrieben" -- und ihr Archiv war beschaedigt.
        pfad = _reserviere(ordner, stempel)

        # Erst in eine Datei mit Arbeitsendung, am Ende umbenennen. Bricht
        # das Packen ab (Platte voll, Rechner zu, Datei nicht lesbar), bliebe
        # sonst ein ZIP unter dem regulaeren Namen zurück: unvollstaendig,
        # aber einwandfrei zu oeffnen, in keinem Protokoll verzeichnet -- und
        # beim naechsten Blick in den Ordner die "Sicherung von gestern".
        # Der Name ist reserviert, also gehoert auch die Arbeitsdatei diesem
        # Lauf allein.
        teil = pfad.with_name(pfad.name + ".teil")
        dateien = 0
        try:
            with zipfile.ZipFile(teil, "w", zipfile.ZIP_DEFLATED) as archiv:
                archiv.write(kopie, DB_IM_ARCHIV)
                dateien += 1
                if tabelle is not None:
                    archiv.write(tabelle, EXCEL_IM_ARCHIV)
                    dateien += 1
                if spiegel is not None:
                    archiv.write(spiegel, journal_export.DATEINAME)
                    dateien += 1
                for eintrag in sorted(datenordner.rglob("*")):
                    if not eintrag.is_file():
                        continue
                    relativ = eintrag.relative_to(datenordner)
                    if _ausgelassen(eintrag, relativ, sperrpfade, lebend):
                        continue
                    # Unter ihrem Pfad IM Datenordner, ohne Zwischenordner:
                    # das Archiv in einen leeren Ordner entpackt IST der
                    # Datenordner. Ein fest verdrahtetes "daten/" davor
                    # (so war es) passte zu keinem echten Ordnernamen -- auf
                    # dem Mac heisst er "Steuer-Tool" --, und wer der Anleitung
                    # folgte, legte jeden Beleg eine Ebene zu tief ab.
                    archiv.write(eintrag, str(relativ))
                    dateien += 1
            os.replace(teil, pfad)
        except BaseException:
            teil.unlink(missing_ok=True)
            pfad.unlink(missing_ok=True)
            raise

    archiv_hash = pruefsumme(pfad)

    # Zweitkopie, falls ein zweites Ziel eingestellt ist. Ein Fehlschlag darf
    # die Sicherung NICHT scheitern lassen: die externe Platte haengt meistens
    # nicht dran, und eine Sicherung, die deshalb ganz ausfaellt, ist schlechter
    # als eine, die nur einmal liegt. Gemeldet wird es trotzdem -- als Fehler,
    # nie als Erfolg.
    kopie: Path | None = None
    kopie_fehler: str | None = None
    if zweites is not None:
        try:
            zweit_ordner = zweites.resolve()
        except (RuntimeError, OSError):
            zweit_ordner = zweites
        if zweit_ordner == ordner:
            # Etwa: erstes Ziel leer (also <Daten>/backup) und dasselbe als
            # zweites eingetragen. shutil meldete das auf Englisch, und der
            # angehaengte Hinweis aufs Laufwerk fuehrte in die Irre.
            kopie_fehler = (
                f"Das zweite Ziel {zweites} ist derselbe Ordner wie das erste — eine "
                "Kopie daneben schützt nicht gegen einen Plattendefekt. Bitte unter "
                "„Zielordner“ ein anderes Laufwerk als zweites Ziel eintragen."
            )
        else:
            try:
                kopie = kopiere_nach(pfad, zweites)
            except OSError as fehler:
                kopie_fehler = (
                    f"Die Zweitkopie nach {zweites} ist nicht entstanden: "
                    f"{fehlertext(fehler)}"
                )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO backup_log (zeitpunkt, pfad, groesse, sha256, geprueft)"
            " VALUES (?, ?, ?, ?, 0)",
            (db.jetzt_utc(), str(pfad), pfad.stat().st_size, archiv_hash),
        )
        db.protokolliere(conn, "BACKUP", "backup", None, wert_neu=str(pfad), hash_=archiv_hash,
                         begruendung=(f"Zweitkopie: {kopie}" if kopie else kopie_fehler))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # Den Spiegel auf der Platte noch einmal schreiben, jetzt MIT dem
    # Sicherungsvorgang: sonst meldete die Journalseite direkt nach jeder
    # Sicherung "veraltet". Das Archiv selbst traegt den Stand bis unmittelbar
    # vor der Sicherung -- der eigene Eintrag kann nicht darin sein.
    try:
        journal_export.schreibe(conn, datenordner / journal_export.DATEINAME)
    except OSError:
        pass
    return Archiv(pfad=pfad, groesse=pfad.stat().st_size, sha256=archiv_hash,
                  dateien=dateien, zweitkopie=kopie, zweitkopie_fehler=kopie_fehler,
                  excel_fehler=excel_fehler)


def _anzahl(n: int, einzahl: str, mehrzahl: str) -> str:
    """Zahl mit richtigem Plural: "1 Zeiteintrag", "3 Zeiteinträge"."""
    return f"{n} {einzahl if n == 1 else mehrzahl}"


def _verzeichnete_dateien(pruefling: sqlite3.Connection) -> set[str]:
    """Belegdateien und Rechnungs-PDF, die die Datenbank IM ARCHIV nennt.

    Verglichen wird mit der Datenbank aus dem Archiv, nicht mit der laufenden:
    was nach der Sicherung dazukam, kann nicht im Archiv sein und fehlt dort
    auch nicht. Nur relative Pfade zaehlen -- eine Datei ausserhalb des
    Datenordners packt die Sicherung grundsaetzlich nicht ein.
    """
    gefunden: set[str] = set()
    # Dazu die Abrechnung zu einer Einnahme ohne Rechnung und jede
    # eingespielte WISO-Datei (Migration 26): beide sind Nachweis, woher
    # eine Buchung stammt, und gehoeren ins Archiv wie ein Beleg.
    for tabelle, spalte in (("belegdatei", "pfad"), ("rechnung", "pdf_pfad"),
                            ("einnahmedatei", "pfad"), ("wiso_import", "pfad")):
        try:
            zeilen = pruefling.execute(
                f'SELECT "{spalte}" AS p FROM "{tabelle}" WHERE "{spalte}" IS NOT NULL'
            ).fetchall()
        except sqlite3.OperationalError:
            # Archiv aus einer Zeit, in der es die Tabelle oder Spalte noch
            # nicht gab.
            continue
        for zeile in zeilen:
            pfad = str(zeile["p"])
            if pfad and not Path(pfad).is_absolute():
                gefunden.add(pfad.replace(os.sep, "/"))
    return gefunden


def _pruefe_archiv(pfad: Path, sha256: str | None) -> Pruefergebnis:
    """Die eigentliche Pruefung, ohne Protokoll: Pruefsumme, ZIP, Datenbank, Dateien."""
    if not pfad.exists():
        return Pruefergebnis(pfad, False, f"Die Datei fehlt: {pfad}")

    if sha256 and pruefsumme(pfad) != sha256:
        return Pruefergebnis(
            pfad, False,
            "Die Prüfsumme stimmt nicht mehr — die Datei wurde nach dem Erzeugen verändert "
            "oder ist beschädigt.",
        )

    with tempfile.TemporaryDirectory() as tmp:
        try:
            with zipfile.ZipFile(pfad) as archiv:
                kaputt = archiv.testzip()
                if kaputt:
                    return Pruefergebnis(pfad, False, f"Beschädigter Eintrag im Archiv: {kaputt}")
                archiv.extract(DB_IM_ARCHIV, tmp)
                namen = set(archiv.namelist())
        except (zipfile.BadZipFile, KeyError) as fehler:
            return Pruefergebnis(pfad, False, f"Das Archiv ist nicht lesbar: {fehler}")

        kopie = Path(tmp) / DB_IM_ARCHIV
        try:
            pruefling = sqlite3.connect(kopie)
            pruefling.row_factory = sqlite3.Row
            try:
                schaden = pruefling.execute("PRAGMA integrity_check").fetchone()[0]
                if schaden != "ok":
                    return Pruefergebnis(
                        pfad, False, f"Die Datenbank im Archiv ist defekt: {schaden}"
                    )
                rechnungen = pruefling.execute(
                    "SELECT COUNT(*) FROM rechnung WHERE status = 'FESTGESCHRIEBEN'"
                ).fetchone()[0]
                zeiten = pruefling.execute("SELECT COUNT(*) FROM zeiteintrag").fetchone()[0]
                verzeichnet = _verzeichnete_dateien(pruefling)
            finally:
                pruefling.close()
        except sqlite3.DatabaseError as fehler:
            return Pruefergebnis(pfad, False, f"Die Datenbank im Archiv ist defekt: {fehler}")

    # Was die Datenbank im Archiv an Dateien nennt, muss auch im Archiv liegen.
    # Sonst ist das Backup fuer § 147 AO wertlos -- und das soll die Pruefung
    # sagen, nicht erst der Ernstfall. Archive aus der Zeit mit Zwischenordner
    # fuehren die Dateien unter "daten/".
    fehlend = sorted(
        p for p in verzeichnet if p not in namen and f"daten/{p}" not in namen
    )
    if fehlend:
        beispiel = fehlend[0] if len(fehlend) == 1 else f"zum Beispiel {fehlend[0]}"
        return Pruefergebnis(
            pfad, False,
            f"Im Archiv {'fehlt' if len(fehlend) == 1 else 'fehlen'} "
            f"{_anzahl(len(fehlend), 'verzeichnete Belegdatei', 'verzeichnete Belegdateien')} "
            f"({beispiel}). Das Archiv ist unvollständig.",
        )

    belege = sum(
        1 for name in namen
        if name.startswith(("belege/", "ausgangsrechnungen/",
                            # Archive aus der Zeit mit Zwischenordner.
                            "daten/belege/", "daten/ausgangsrechnungen/"))
    )
    return Pruefergebnis(
        pfad, True,
        f"Archiv gelesen: "
        f"{_anzahl(rechnungen, 'festgeschriebene Rechnung', 'festgeschriebene Rechnungen')}, "
        f"{_anzahl(zeiten, 'Zeiteintrag', 'Zeiteinträge')}, "
        f"{_anzahl(belege, 'Belegdatei', 'Belegdateien')}.",
        rechnungen=rechnungen, zeiteintraege=zeiten, belegdateien=belege,
    )


def pruefe(conn: sqlite3.Connection, backup_id: int) -> Pruefergebnis:
    """Öffnet das Archiv wirklich und zählt nach.

    Prüft zuerst die Prüfsumme -- ein verändertes Byte fällt damit auf, auch
    wenn das ZIP noch entpackbar wäre. Danach wird die Datenbank geöffnet und
    gezählt: ein Archiv, dessen Datenbank sich nicht öffnen lässt, ist wertlos,
    und das soll man vorher wissen. Zuletzt wird verglichen, ob jede Datei, die
    die Datenbank im Archiv nennt, auch im Archiv liegt.

    Ein Fehlschlag nimmt ``geprueft`` zurück: ein früher bestandenes Archiv,
    das heute durchfällt (Bitfehler, Platte), ist kein geprüftes mehr, und
    das Dashboard darf nicht weiter mit ihm beruhigen. Beides steht im Journal.
    """
    # Eine Nummer ab 2**63 aus der Adresse sprengte die Bindung an SQLite.
    zeile = conn.execute(
        "SELECT * FROM backup_log WHERE id = ?", (backup_id,)
    ).fetchone() if 1 <= backup_id <= eingabe.ID_HOECHSTENS else None
    if zeile is None:
        return Pruefergebnis(Path("."), False, "Dieses Backup ist nicht verzeichnet.")

    pfad = Path(zeile["pfad"])
    ergebnis = _pruefe_archiv(pfad, zeile["sha256"])
    if ergebnis.heil:
        conn.execute("UPDATE backup_log SET geprueft = 1 WHERE id = ?", (backup_id,))
        db.protokolliere(conn, "BACKUP_GEPRUEFT", "backup", backup_id, wert_neu=str(pfad))
    else:
        conn.execute("UPDATE backup_log SET geprueft = 0 WHERE id = ?", (backup_id,))
        db.protokolliere(conn, "BACKUP_PRUEFUNG_FEHLGESCHLAGEN", "backup", backup_id,
                         wert_neu=str(pfad), begruendung=ergebnis.meldung)
    return ergebnis


def liste(conn: sqlite3.Connection, grenze: int = 50) -> list[dict[str, object]]:
    """Die vorhandenen Sicherungen, neueste zuerst, mit Hinweis auf Fehlendes."""
    eintraege = []
    for zeile in conn.execute(
        "SELECT * FROM backup_log ORDER BY zeitpunkt DESC LIMIT ?", (grenze,)
    ):
        pfad = Path(zeile["pfad"])
        eintraege.append({
            "id": zeile["id"],
            "zeitpunkt": zeile["zeitpunkt"],
            "pfad": zeile["pfad"],
            "name": pfad.name,
            "groesse": zeile["groesse"],
            "sha256": zeile["sha256"],
            "geprueft": bool(zeile["geprueft"]),
            "vorhanden": pfad.exists(),
        })
    return eintraege


def letztes_geprueft(conn: sqlite3.Connection) -> str | None:
    """Der Zeitpunkt der letzten gepruefte Sicherung, die es noch GIBT.

    Das Protokoll weiss nichts davon, dass jemand eine Datei geloescht oder
    eine Platte abgezogen hat. Ohne den Blick aufs Dateisystem beruhigte die
    Oberflaeche mit einer Sicherung, die nicht mehr existiert -- und das ist
    die eine Zahl, auf die man sich dort verlaesst.
    """
    for zeile in conn.execute(
        "SELECT zeitpunkt, pfad FROM backup_log WHERE geprueft = 1 ORDER BY zeitpunkt DESC"
    ):
        if Path(zeile["pfad"]).exists():
            return zeile["zeitpunkt"]
    return None


def wiederherstellungshinweis(pfad: Path) -> str:
    """Was zu tun ist, wenn es ernst wird.

    Absichtlich eine Anleitung und keine Funktion: eine Wiederherstellung
    überschreibt den laufenden Bestand, und das soll niemand aus Versehen per
    Knopfdruck tun. Der Text wandert in die Verfahrensdokumentation.
    """
    return (
        f"Wiederherstellen: Anwendung beenden, Datenordner umbenennen (nicht löschen), "
        f"an seiner Stelle einen leeren Ordner gleichen Namens anlegen und {pfad.name} "
        f"dort hinein entpacken -- {DB_IM_ARCHIV}, belege/, ausgangsrechnungen/ und "
        f"journal.log liegen dann genau dort, wo die Anwendung sie erwartet. Anwendung "
        f"starten; danach im Dashboard prüfen, ob die letzte Rechnungsnummer stimmt."
    )


def menschenlesbar(bytes_: int | None) -> str:
    """Eine Dateigroesse fuer Nutzertexte -- deutsch, mit Dezimalkomma.

    Steht in der Erfolgsmeldung der Sicherung, in der Liste der Sicherungen
    und im Eingang; "70.3 KB" las sich wie siebzigtausenddreihundert.

    >>> menschenlesbar(512), menschenlesbar(72_000), menschenlesbar(5 * 1024 * 1024)
    ('512 B', '70,3 KB', '5,0 MB')
    >>> menschenlesbar(None), menschenlesbar(3 * 1024 ** 4)
    ('—', '3072,0 GB')
    """
    if not bytes_:
        return "—"
    groesse = float(bytes_)
    for einheit in ("B", "KB", "MB", "GB"):
        if groesse < 1024 or einheit == "GB":
            break
        groesse /= 1024
    if einheit == "B":
        return f"{groesse:.0f} B"
    return f"{groesse:.1f}".replace(".", ",") + f" {einheit}"


def _reserviere(ordner: Path, stempel: str) -> Path:
    """Reserviert den naechsten freien Archivnamen -- atomar.

    ``O_CREAT | O_EXCL`` legt die Datei nur an, wenn es sie noch nicht gibt;
    das entscheidet das Dateisystem, nicht ein vorheriger Blick darauf. Zwei
    gleichzeitige Sicherungen bekommen so zwei Namen. Mehrere Sicherungen am
    selben Tag sind normal und dürfen sich nicht gegenseitig überschreiben.
    """
    for lauf in range(1, LAEUFE_HOECHSTENS + 1):
        name = (f"{stempel}_steuer-backup.zip" if lauf == 1
                else f"{stempel}_steuer-backup-{lauf}.zip")
        pfad = ordner / name
        try:
            griff = os.open(pfad, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(griff)
        return pfad
    raise OSError(
        f"Im Ordner {ordner} liegen schon {LAEUFE_HOECHSTENS} Sicherungen von heute — "
        "bitte aufräumen."
    )


def kopiere_nach(archiv: Path, ziel: Path) -> Path:
    """Legt eine Zweitkopie ab, etwa auf eine externe Platte.

    Eine Sicherung auf derselben Platte wie die Daten schützt gegen
    Versehen, nicht gegen einen Plattendefekt. Fehlt der Einhaengeordner des
    Laufwerks, ist die Platte nicht da (:func:`ordner_anlegen`).

    Wie das Archiv selbst erst unter Arbeitsendung, dann umbenannt: eine
    abgezogene Platte hinterlaesst sonst ein halbes ZIP unter dem regulaeren
    Namen.
    """
    ordner_anlegen(ziel)
    kopie = ziel / archiv.name
    teil = kopie.with_name(kopie.name + ".teil")
    try:
        shutil.copy2(archiv, teil)
        os.replace(teil, kopie)
    except BaseException:
        teil.unlink(missing_ok=True)
        raise
    return kopie

"""Der Weg eines Belegs von der Datei in die Buchfuehrung.

Zwei Schritte, und die Trennung ist der Kern dieses Moduls:

1. ``aufnehmen`` + ``lies`` -- die Datei landet im **Eingang**, wird gehasht,
   gegen Dubletten geprueft und ausgelesen. In der Datenbank entsteht dabei
   **nichts**.
2. ``buche`` -- der Nutzer hat die Felder bestaetigt. Jetzt wandert das
   Original ins Archiv, die Datensaetze entstehen, und der Lernspeicher lernt.

Warum nicht in einem Schritt
----------------------------
Ein Beleg braucht Datum und Betrag, sonst ist er keiner -- das Schema verlangt
beides. Einen Datensatz vor der Bestaetigung anzulegen hiesse also, Datum und
Betrag zu **erfinden**, etwa "heute" und "ein Cent". Eine erfundene Zahl in der
Buchfuehrung ist schlimmer als eine fehlende: die fehlende faellt auf.

Umgekehrt darf eine hochgeladene Datei nicht einfach verschwinden, wenn der
Nutzer die Maske schliesst. Deshalb der Eingangsordner: dort liegt sie
sichtbar, bis sie gebucht oder weggeworfen wird. Nichts wird erfunden, nichts
geht verloren.

Die Stufenfolge
---------------
Die Reihenfolge ist durch **Recht** vorgegeben, nicht durch Bequemlichkeit:
ist bei einer E-Rechnung XML vorhanden, ist es massgebend und die Auslesung des
Bildteils unzulaessig. Scheitert das Parsen des XML, folgt ein harter Fehler --
niemals ein Rueckfall auf eine spaetere Stufe.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from app import db, pfade
from app.logik import eingabe, kategorien
from app.logik.beleg import (
    email_leser,
    profil,
    text_leser,
    umrechnung,
    vision_mac,
    xml_leser,
)
from app.logik.beleg import felder as f
from app.logik.beleg.ergebnis import BelegFehler, Fund, Vorschlag

#: Unterordner im Datenordner.
EINGANG = "eingang"
ARCHIV = "belege"

#: Dateiendungen, die als Beleg taugen. Alles andere wird abgewiesen -- ein
#: .docx oder .xlsx ist kein Beleg, sondern eine Aufstellung, und
#: veraenderliche Formate haben in einem unveraenderlichen Archiv nichts zu
#: suchen.
#:
#: Dazu E-Mails als .eml (app/logik/beleg/email_leser.py): manche Rechnung
#: gibt es nur als Mail, und manche Mail traegt die Rechnung im Anhang.
ERLAUBT = {".pdf", ".xml", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp",
           ".eml"}

#: Groessengrenze je Datei. Ein Beleg, der groesser ist, ist ein Scan in
#: unnoetiger Auflösung -- und das Archiv muss acht Jahre tragbar bleiben
#: (§ 147 Abs. 3 AO für Buchungsbelege).
MAX_BYTES = 50 * 1024 * 1024

#: Dateirechte des archivierten Originals: nur lesen, fuer niemanden
#: schreibbar. Die billigste Massnahme gegen versehentliche Aenderung; die
#: belastbare ist die Pruefsumme.
NUR_LESEN = 0o444


@dataclass(frozen=True, slots=True)
class Eingang:
    """Eine Datei im Eingangsordner, noch nicht gebucht."""

    pfad: Path
    original_name: str
    sha256: str
    groesse: int
    #: True, wenn genau DIESER Aufruf von ``aufnehmen`` die Datei angelegt hat
    #: -- False, wenn sie schon da lag (Upload, frueherer Lauf, ein
    #: gleichzeitiger Lauf eines zweiten Prozesses).
    neu: bool = False

    @property
    def endung(self) -> str:
        return Path(self.original_name).suffix.lower()


@dataclass(frozen=True, slots=True)
class Lesung:
    """Was aus einer Eingangsdatei herausgelesen wurde."""

    eingang: Eingang
    vorschlag: Vorschlag
    rohtext: str = ""
    #: Welche Stufen gelaufen sind, in der Reihenfolge ihres Laufs.
    stufen: tuple[str, ...] = ()
    #: Gesetzt, wenn genau diese Datei schon einmal gebucht wurde.
    dublette_von: int | None = None
    lieferant_id: int | None = None
    meldungen: tuple[str, ...] = field(default_factory=tuple)
    #: Gesetzt, wenn die Datei eine Gutschrift ist (CII 381, UBL CreditNote):
    #: der Text der Erkennung. Eine Gutschrift ist keine Ausgabe und wird
    #: nicht als solche gebucht.
    gutschrift: str | None = None
    #: Belege desselben Lieferanten mit derselben Belegnummer oder mit gleichem
    #: Datum und Betrag -- vermutlich dieselbe Rechnung als anderes PDF.
    aehnliche: tuple[sqlite3.Row, ...] = ()
    #: Dasselbe bei einem ANDEREN Lieferanten-Datensatz oder ohne Lieferant,
    #: enger gefasst (siehe :func:`aehnliche_bei_anderen`).
    aehnliche_andere: tuple[sqlite3.Row, ...] = ()
    #: Die Waehrung, in der die Auslesung den Betrag sieht (erkannt, sonst
    #: die gemerkte des Lieferanten, sonst Euro) -- fuer die Warnung bei
    #: einem aehnlichen Beleg in anderer Waehrung.
    waehrung: str = "EUR"
    #: Bei einer E-Mail mit Rechnung im Anhang: der Anhang (versteckt im
    #: Eingang abgelegt). ER ist dann der Beleg und wurde ausgelesen; die
    #: Mail (``eingang``) wird beim Buchen mit Rolle EMAIL archiviert.
    anhang: Eingang | None = None

    @property
    def ist_dublette(self) -> bool:
        return self.dublette_von is not None

    @property
    def ist_gutschrift(self) -> bool:
        return self.gutschrift is not None

    @property
    def geliefert(self) -> tuple[str, ...]:
        """Die Stufen, die tatsaechlich Werte geliefert haben.

        ``stufen`` nennt jede VERSUCHTE Stufe -- fuer ein gewoehnliches PDF
        also auch "xml". Als Herkunft angezeigt las der Nutzer daraus, es sei
        eine E-Rechnung oder eine Bilderkennung gelaufen. Hat keine Stufe
        etwas geliefert, ist es eine manuelle Erfassung.
        """
        if not self.vorschlag.felder:
            return ("manuell",)
        geliefert = (self.vorschlag.quelle,)
        if "profil" in self.stufen:
            geliefert += ("profil",)
        return geliefert


class AehnlicherBeleg(BelegFehler):
    """Zu diesem Lieferanten gibt es schon einen Beleg mit derselben
    Belegnummer oder demselben Datum und Betrag. Ein eigener Typ, damit die
    Maske die Bestaetigung "trotzdem buchen" anbieten kann."""


class SchonGebucht(BelegFehler):
    """Genau diese Datei (Pruefsumme) ist schon als Beleg gebucht.

    Ein eigener Typ, weil die Route zwei Faelle unterscheidet: liegt die Datei
    noch im Eingang, ist es eine zweite Kopie (wegwerfen); liegt sie dort nicht
    mehr, hat ein Doppelklick das Formular zweimal geschickt, und die erste
    Anfrage hat gebucht -- dann ist das eine Erfolgsmeldung.
    """

    def __init__(self, beleg_id: int) -> None:
        super().__init__(
            f"Diese Datei ist bereits als Beleg {beleg_id} gebucht. Sie bleibt "
            "im Eingang liegen: dort wegwerfen oder, falls es um eine weitere Zahlung "
            "geht, die Zahlung am vorhandenen Beleg erfassen."
        )
        self.beleg_id = beleg_id


def pruefsumme(pfad: Path) -> str:
    kessel = hashlib.sha256()
    with pfad.open("rb") as datei:
        for stueck in iter(lambda: datei.read(1024 * 1024), b""):
            kessel.update(stueck)
    return kessel.hexdigest()


#: Woran der Inhalt einer Datei zu erkennen ist -- unabhaengig von der Endung.
#: Die Endung behauptet nur, was die Datei ist; die ersten Bytes zeigen es.
#: Ein Programm mit der Endung .pdf landete sonst im Eingang, war dort nicht
#: lesbar, liess sich aber buchen und lag dann schreibgeschuetzt im Archiv,
#: ohne je als Beleg dienen zu koennen.
_MAGIC = {
    ".pdf": (b"%PDF",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
}

_FORMATNAMEN = {
    ".pdf": "PDF", ".png": "PNG-Bild", ".jpg": "JPEG-Bild", ".jpeg": "JPEG-Bild",
    ".tif": "TIFF-Bild", ".tiff": "TIFF-Bild", ".heic": "HEIC-Bild",
    ".webp": "WebP-Bild", ".xml": "XML", ".eml": "E-Mail (mit Kopf Von, Datum, Betreff)",
}


def inhalt_passt_zur_endung(inhalt: bytes, endung: str) -> bool:
    """Stimmen die ersten Bytes mit dem, was die Endung verspricht?

    >>> inhalt_passt_zur_endung(b"%PDF-1.7 ...", ".pdf")
    True
    >>> inhalt_passt_zur_endung(b"MZ\\x90\\x00", ".pdf")
    False
    >>> inhalt_passt_zur_endung(b"\\xef\\xbb\\xbf  <?xml version='1.0'?>", ".xml")
    True
    >>> inhalt_passt_zur_endung(b"RIFF\\x00\\x00\\x00\\x00WEBPVP8 ", ".webp")
    True
    """
    if endung in _MAGIC:
        return inhalt.startswith(_MAGIC[endung])
    if endung == ".heic":
        # ISO Base Media: die Box "ftyp" steht ab Byte 4, davor die Laenge.
        return len(inhalt) >= 12 and inhalt[4:8] == b"ftyp"
    if endung == ".webp":
        return inhalt.startswith(b"RIFF") and inhalt[8:12] == b"WEBP"
    if endung == email_leser.ENDUNG:
        return email_leser.sieht_aus_wie_mail(inhalt)
    if endung == ".xml":
        # Eine XML-Datei beginnt mit "<", nach Byte-Order-Mark und Leerraum.
        # UTF-16 (selten, aber zulaessig) wird ueber den BOM erkannt.
        anfang = inhalt[:64]
        if anfang.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                return anfang.decode("utf-16").lstrip().startswith("<")
            except UnicodeDecodeError:
                return False
        return anfang.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<")
    return False


def inhalt_passt(pfad: Path) -> bool | None:
    """Wie ``inhalt_passt_zur_endung``, fuer eine Datei, die schon im Eingang
    liegt -- etwa von Hand hineingelegt, am Upload und seiner Pruefung vorbei.

    ``None`` heisst: die Datei ist nicht (mehr) lesbar -- meist hat eine
    gleichzeitige Anfrage sie gerade gebucht und ins Archiv verschoben. Das
    ist etwas anderes als "Inhalt passt nicht": vorher wurde beides False, und
    ein Doppelklick auf "Buchen" meldete "Der Inhalt ... ist kein XML" fuer
    eine Datei, die soeben gebucht worden war. Aufrufer pruefen deshalb
    ``is None`` VOR ``not``.
    """
    endung = pfad.suffix.lower()
    try:
        with pfad.open("rb") as datei:
            # Bei einer Mail stehen From, Date und Subject oft erst nach
            # vielen Kilobyte Received- und DKIM-Zeilen.
            anfang = datei.read(email_leser.KOPF_HOECHSTENS if endung == email_leser.ENDUNG
                                else 64)
    except OSError:
        return None
    return inhalt_passt_zur_endung(anfang, endung)


def inhalt_fremd(name: str, endung: str) -> str:
    """Die Meldung fuer eine Eingangsdatei, deren Inhalt nicht zur Endung passt."""
    return (
        f"Der Inhalt von „{name}“ ist kein {_FORMATNAMEN.get(endung, endung)} — die "
        "Endung passt nicht zum Inhalt. So lässt sie sich nicht buchen; bitte am "
        "Rechner öffnen, prüfen, was sie wirklich ist, und hier wegwerfen."
    )


def aufnehmen(inhalt: bytes, original_name: str, datenordner: Path) -> Eingang:
    """Legt eine hochgeladene Datei in den Eingangsordner.

    Der Dateiname im Eingang wird aus der Pruefsumme gebildet und nicht aus dem
    Originalnamen. Zwei Gruende: der Originalname ist oft ``rechnung.pdf`` und
    kollidiert, und er kommt von aussen -- ein Name wie ``../../steuer.sqlite``
    waere ein Weg aus dem Ordner heraus.

    Der Originalname wird trotzdem gemerkt, in einer Begleitdatei neben der
    Eingangsdatei (``.<hash>.name``): beim Buchen wandert er in
    ``belegdatei.original_name``, und der Nutzer bekommt seine Datei spaeter
    unter dem Namen zurueck, unter dem er sie kennt -- nicht unter einem Hash.
    """
    # Den Namen trimmen, BEVOR die Endung geprueft wird: "rechnung.pdf " hat
    # die Endung ".pdf " und wurde mit einer Meldung abgewiesen, in der das
    # Leerzeichen unsichtbar war.
    original_name = Path(original_name.strip()).name
    endung = Path(original_name).suffix.lower()
    anderes = email_leser.anderes_mailformat(endung)
    if anderes:
        raise BelegFehler(anderes)
    if endung not in ERLAUBT:
        raise BelegFehler(
            f"{endung or 'Diese Datei'} ist kein Belegformat. Möglich sind "
            f"{', '.join(sorted(ERLAUBT))}."
        )
    if not inhalt:
        raise BelegFehler("Die Datei ist leer.")
    if len(inhalt) > MAX_BYTES:
        raise BelegFehler(zu_gross(len(inhalt)))
    if not inhalt_passt_zur_endung(inhalt, endung):
        raise BelegFehler(
            f"Der Inhalt von „{original_name}“ ist kein {_FORMATNAMEN[endung]} — die "
            "Endung passt nicht zum Inhalt. Die Datei wurde nicht aufgenommen; bitte "
            "am Rechner öffnen und prüfen, was sie wirklich ist."
        )

    ordner = datenordner / EINGANG
    ordner.mkdir(parents=True, exist_ok=True)
    hash_ = hashlib.sha256(inhalt).hexdigest()
    ziel = ordner / f"{hash_[:16]}{endung}"
    neu = False
    if not ziel.exists():
        # Erst unter einem versteckten Zwischennamen schreiben, dann in einem
        # Zug an den Hash-Namen. Bricht der Prozess mitten im Schreiben ab
        # (zweiter Start, der am belegten Port scheitert, waehrend er den
        # Eingangsordner abholt), laege sonst eine halbe Datei unter dem
        # Hash-Namen -- und jeder weitere Versuch faende "schon da" und schriebe
        # sie nie neu. Der Zwischenname ist je Aufruf eigen: zwei Threads
        # desselben Prozesses (Upload und Abholen) schrieben sonst in dieselbe
        # Datei.
        zwischen = ordner / f".{ziel.name}.{secrets.token_hex(6)}.teil"
        try:
            zwischen.write_bytes(inhalt)
            neu = _ablegen(zwischen, ziel)
        finally:
            zwischen.unlink(missing_ok=True)
    begleiter = _namensdatei(ziel)
    if not begleiter.exists():
        try:
            begleiter.write_text(original_name, encoding="utf-8")
        except (OSError, UnicodeError):
            # Dann heisst die Datei spaeter wie ihr Hash -- kein Verlust am
            # Beleg. Eine halb geschriebene Begleitdatei bleibt nicht liegen.
            try:
                begleiter.unlink(missing_ok=True)
            except OSError:
                pass
    return Eingang(pfad=ziel, original_name=original_name,
                   sha256=hash_, groesse=len(inhalt), neu=neu)


def _ablegen(zwischen: Path, ziel: Path) -> bool:
    """Legt die Zwischendatei unter dem Hash-Namen ab: True, wenn DIESER
    Aufruf sie angelegt hat.

    ``os.link`` gelingt nur, wenn der Name noch frei ist -- so weiss das
    Abholen sicher, ob die Kopie von ihm stammt. Vorher entschied ein
    "existiert sie schon?" VOR dem Aufnehmen; buchte ein zweiter Prozess
    dazwischen und raeumte seine Kopie weg, legte dieser Aufruf sie neu an,
    hielt sie aber fuer fremd und liess die Kopie eines gebuchten Belegs im
    Eingang liegen. Kann das Laufwerk keine harten Verknuepfungen, gilt das
    Umbenennen (dann wie bisher).
    """
    try:
        os.link(zwischen, ziel)
    except FileExistsError:
        return False
    except OSError:
        if ziel.exists():
            return False
        os.replace(zwischen, ziel)
    return True


#: Aelter als so viele Sekunden ist eine Zwischendatei verwaist: ihr
#: Schreiber lebt nicht mehr (Prozess beendet, waehrend er aufnahm).
ZWISCHENDATEI_VERWAIST_SEKUNDEN = 600


def alte_zwischendateien_weg(datenordner: Path, *, jetzt: float | None = None) -> int:
    """Raeumt verwaiste Zwischendateien (``.<hash>.<endung>.<zufall>.teil``)
    aus dem Eingang -- beim Start.

    Bricht der Prozess mitten im Aufnehmen ab, laeuft das ``finally`` in
    ``aufnehmen`` nicht mehr, und die versteckte Zwischendatei bliebe fuer
    immer liegen. Juengere bleiben: die koennten zu einem Upload gehoeren,
    der gerade laeuft. Gibt die Zahl der entfernten Dateien zurueck.
    """
    import time

    grenze = (time.time() if jetzt is None else jetzt) - ZWISCHENDATEI_VERWAIST_SEKUNDEN
    weg = 0
    ordner = datenordner / EINGANG
    if not ordner.is_dir():
        return 0
    for datei in ordner.glob(".*.teil"):
        try:
            if datei.is_file() and datei.stat().st_mtime < grenze:
                datei.unlink()
                weg += 1
        except OSError:
            pass
    return weg


def zu_gross(groesse: int) -> str:
    """Die Meldung fuer eine Datei ueber der Groessengrenze.

    >>> zu_gross(60 * 1024 * 1024)[:40]
    'Die Datei ist größer als 50 MB (60,0 MB)'
    """
    megabyte = f"{groesse / 1024 / 1024:.1f}".replace(".", ",")
    return (
        f"Die Datei ist größer als {MAX_BYTES // 1024 // 1024} MB "
        f"({megabyte} MB); mehr nimmt das Archiv nicht. "
        "Ein Scan in 150 dpi genügt für einen Beleg."
    )


def _namensdatei(pfad: Path) -> Path:
    """Die Begleitdatei mit dem Originalnamen. Mit Punkt davor, damit sie
    weder in der Eingangsliste noch ueber die URL als Beleg erreichbar ist."""
    return pfad.with_name(f".{pfad.name}.name")


def original_name_von(pfad: Path) -> str:
    """Der hochgeladene Name einer Eingangsdatei; ohne Begleitdatei ihr Name.

    Eine Datei, die der Nutzer selbst in den Eingangsordner gelegt hat, traegt
    ihren Namen ja schon.
    """
    try:
        name = Path(_namensdatei(pfad).read_text(encoding="utf-8").strip()).name
    except (OSError, UnicodeDecodeError, ValueError):
        return pfad.name
    # Die Endung muss zur Eingangsdatei passen -- an ihr haengt, als was die
    # Datei gelesen und archiviert wird. Eine fremde Begleitdatei zaehlt nicht.
    if not name or Path(name).suffix.lower() != pfad.suffix.lower():
        return pfad.name
    return name


def vergiss_original_name(pfad: Path) -> None:
    """Raeumt die Begleitdatei weg -- nach dem Buchen oder Wegwerfen. Bei
    einer Mail auch die versteckt abgelegten Anhaenge (:func:`_anhang_ablegen`)."""
    try:
        _namensdatei(pfad).unlink()
    except OSError:
        pass
    if pfad.suffix.lower() == email_leser.ENDUNG:
        for anhang in pfad.parent.glob(f".{pfad.name}.anhang*"):
            try:
                anhang.unlink()
            except OSError:
                pass


def eingang_wegwerfen(pfad: Path) -> bool:
    """Loescht eine Datei aus dem EINGANG samt Begleitdateien (Originalname,
    Anhaenge einer Mail). True, wenn sie noch da war.

    Nur fuer den Eingang: dort liegt noch kein Beleg, sondern eine Kopie im
    Tool. Den Pfad prueft der Aufrufer (``_eingangsdatei`` der Routen) -- ein
    gebuchter Beleg liegt im Archiv und ist per Trigger gegen das Loeschen
    gesperrt.
    """
    try:
        pfad.unlink()
    except FileNotFoundError:
        return False
    vergiss_original_name(pfad)
    return True


def dublette(conn: sqlite3.Connection, sha256: str) -> int | None:
    """Der Beleg, als dessen Original genau diese Datei (Pruefsumme) schon
    gebucht ist -- oder ``None``. Auch eine Mail, die als Begleitdatei
    (Rolle EMAIL) zu einer Rechnung im Anhang archiviert ist: dieselbe Mail
    noch einmal ist dieselbe Rechnung.

    Nur GUELTIGE Belege: ein geloeschter (etwa falsch ausgelesen) soll sich
    neu hochladen und buchen lassen. Seine Datei bleibt im Archiv; liegt die
    neue Buchung am selben Archivpfad (gleiches Belegdatum), verweist sie auf
    dieselbe Datei (siehe :func:`buche`). Wo ein geloeschter Beleg die Datei
    hatte, sagt :func:`frueher_geloescht`."""
    zeile = conn.execute(
        "SELECT d.beleg_id FROM belegdatei d JOIN v_beleg_gueltig b ON b.id = d.beleg_id"
        " WHERE d.sha256 = ? AND d.rolle IN ('ORIGINAL', 'EMAIL')"
        " ORDER BY d.rolle = 'EMAIL', d.id LIMIT 1",
        (sha256,),
    ).fetchone()
    return zeile["beleg_id"] if zeile else None


def frueher_geloescht(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    """Der juengste GELOESCHTE Beleg mit genau dieser Datei -- fuer den
    Hinweis in der Pruefmaske, dass sie neu erfasst wird."""
    return conn.execute(
        "SELECT b.id, b.geloescht_am, b.geloescht_grund FROM belegdatei d"
        " JOIN beleg b ON b.id = d.beleg_id"
        " WHERE d.sha256 = ? AND d.rolle IN ('ORIGINAL', 'EMAIL')"
        " AND b.geloescht_am IS NOT NULL ORDER BY b.geloescht_am DESC, b.id DESC LIMIT 1",
        (sha256,),
    ).fetchone()



def _waehrung_oder_eur(text: object) -> str:
    """Die Waehrung fuer den Dublettenvergleich; Unlesbares gilt als Euro."""
    try:
        return normalisiere_waehrung(text)
    except BelegFehler:
        return "EUR"


def _gleicher_betrag(waehrung: str, brutto: object, euro: object) -> tuple[list[str], list[object]]:
    """Die Bedingung "gleicher Betrag" -- in der Waehrung des Belegs.

    Bei Fremdwaehrung steht in ``betrag_brutto_cent`` der EURO-Betrag, der
    Betrag aus der Auslesung ist aber der in Dollar. Wer beides verglich, fand
    dieselbe USD-Rechnung als zweites PDF nie: 1.234,56 USD sind nicht
    1.130,00 EUR. Verglichen wird deshalb Waehrung und Fremdbetrag -- und
    zusaetzlich, wie bisher, der Euro-Betrag, sobald er feststeht (beim
    Buchen). Ob der erste Beleg mit dem Kartenbetrag und der zweite zum
    EZB-Kurs umgerechnet waere, spielt so keine Rolle.

    Und der Zahlenwert in ANDERER Waehrung: dieselbe Rechnung, einmal als
    1.234,56 USD gebucht und beim zweiten Mal ohne erkanntes Dollarzeichen als
    1.234,56 EUR gelesen (oder umgekehrt), traf sonst nichts -- gleicher
    Lieferant, gleiches Datum, keine Belegnummer, und die Rechnung stand
    zweimal in der EUeR. Verglichen wird dafuer der Betrag, wie er auf dem
    Beleg steht: bei Fremdwaehrung ``betrag_fremdwaehrung_cent``, bei Euro
    ``betrag_brutto_cent``. Die Warnung sagt dann "Währung prüfen"
    (:func:`aehnliche_als_text`).
    """
    teile: list[str] = []
    werte: list[object] = []
    if isinstance(brutto, int) and brutto > 0:
        if waehrung == "EUR":
            teile.append("b.betrag_brutto_cent = ?")
            werte.append(brutto)
            teile.append("(b.waehrung != 'EUR' AND b.betrag_fremdwaehrung_cent = ?)")
            werte.append(brutto)
        else:
            teile.append("(b.waehrung = ? AND b.betrag_fremdwaehrung_cent = ?)")
            werte += [waehrung, brutto]
            teile.append("(b.waehrung != ? AND COALESCE(b.betrag_fremdwaehrung_cent,"
                         " b.betrag_brutto_cent) = ?)")
            werte += [waehrung, brutto]
    if waehrung != "EUR" and isinstance(euro, int) and euro > 0:
        teile.append("b.betrag_brutto_cent = ?")
        werte.append(euro)
    return teile, werte


def aehnliche_belege(
    conn: sqlite3.Connection, lieferant_id: int | None, belegnummer: object,
    belegdatum: object, brutto: object, *, waehrung: str = "EUR", euro: object = None,
) -> list[sqlite3.Row]:
    """Belege desselben Lieferanten, die dieselbe Rechnung sein koennten.

    Die Pruefsumme findet nur die byteidentische Datei. Der realistische Fall
    ist ein anderer: dieselbe Rechnung als zweiter Export, als Kopie aus dem
    Kundenportal oder als Scan -- gleicher Lieferant, gleiche Belegnummer,
    gleicher Betrag, andere Bytes. Gesucht wird deshalb am Inhalt: dieselbe
    Belegnummer, oder dasselbe Datum mit demselben Betrag.

    ``brutto`` ist der Betrag in ``waehrung`` (so, wie er auf dem Beleg
    steht); ``euro`` der Euro-Betrag einer Fremdwaehrungsrechnung, falls er
    schon feststeht. Siehe :func:`_gleicher_betrag`.
    """
    if not lieferant_id:
        return []
    nummer = str(belegnummer or "").strip()
    datum = str(belegdatum or "").strip()
    bedingungen = []
    werte: list[object] = [lieferant_id]
    if nummer:
        bedingungen.append("b.belegnummer_extern = ? COLLATE NOCASE")
        werte.append(nummer)
    betrag, betragswerte = _gleicher_betrag(waehrung, brutto, euro)
    if datum and betrag:
        bedingungen.append(f"(b.belegdatum = ? AND ({' OR '.join(betrag)}))")
        werte += [datum, *betragswerte]
    if not bedingungen:
        return []
    return conn.execute(
        f"""
        SELECT b.id, b.belegnummer_extern, b.belegdatum, b.betrag_brutto_cent,
               b.waehrung, b.betrag_fremdwaehrung_cent, l.name AS lieferant
          FROM v_beleg_gueltig b JOIN lieferant l ON l.id = b.lieferant_id
         WHERE b.lieferant_id = ? AND ({' OR '.join(bedingungen)})
         ORDER BY b.id
        """,
        werte,
    ).fetchall()


def aehnliche_bei_anderen(
    conn: sqlite3.Connection, lieferant_id: int | None, belegnummer: object,
    belegdatum: object, brutto: object, *, waehrung: str = "EUR", euro: object = None,
) -> list[sqlite3.Row]:
    """Belege ANDERER Lieferanten-Datensaetze, die dieselbe Rechnung sein koennten.

    ``aehnliche_belege`` sucht nur beim selben Lieferanten. Derselbe Lieferant
    steht aber manchmal zweimal da -- "Hetzner Online GmbH" mit USt-IdNr. und
    "Hetzner", in der Pruefmaske neu getippt --, oder der erste Beleg ist ganz
    ohne Lieferant gebucht. Gesucht wird deshalb auch dort, beim Abholen wie
    in Pruefmaske und ``buche``, enger als beim selben Lieferanten: gleicher
    Bruttobetrag UND (gleiche Belegnummer oder gleiches Datum). Bei
    Fremdwaehrung ist "gleicher Betrag" gleiche Waehrung und gleicher
    Fremdbetrag (oder derselbe Euro-Betrag).

    Gleiches Datum zaehlt nicht, wenn beide Belege eine Belegnummer tragen
    und sie sich unterscheiden: zwei Abos verschiedener Anbieter zu 9,99 EUR
    am Monatsersten sind zwei Rechnungen, dieselbe Rechnung als zweites PDF
    traegt dieselbe Nummer. Ohne ``lieferant_id`` (in der Maske noch keiner
    gewaehlt) wird bei ALLEN Belegen gesucht, auch bei denen ohne Lieferant.
    """
    betrag, betragswerte = _gleicher_betrag(waehrung, brutto, euro)
    if not betrag:
        return []
    nummer = str(belegnummer or "").strip()
    datum = str(belegdatum or "").strip()
    bedingungen: list[str] = []
    werte: list[object] = [*betragswerte]
    if lieferant_id:
        ausschluss = "AND COALESCE(b.lieferant_id, 0) != ?"
        werte.append(lieferant_id)
    else:
        ausschluss = ""
    if nummer:
        bedingungen.append("b.belegnummer_extern = ? COLLATE NOCASE")
        werte.append(nummer)
    if datum:
        if nummer:
            bedingungen.append(
                "(b.belegdatum = ? AND (COALESCE(b.belegnummer_extern, '') = ''"
                " OR b.belegnummer_extern = ? COLLATE NOCASE))")
            werte += [datum, nummer]
        else:
            bedingungen.append("b.belegdatum = ?")
            werte.append(datum)
    if not bedingungen:
        return []
    return conn.execute(
        f"""
        SELECT b.id, b.belegnummer_extern, b.belegdatum, b.betrag_brutto_cent,
               b.waehrung, b.betrag_fremdwaehrung_cent,
               COALESCE(l.name, 'ohne Lieferant') AS lieferant
          FROM v_beleg_gueltig b LEFT JOIN lieferant l ON l.id = b.lieferant_id
         WHERE ({' OR '.join(betrag)}) {ausschluss}
           AND ({' OR '.join(bedingungen)})
         ORDER BY b.id
        """,
        werte,
    ).fetchall()


def aehnliche_ueberall(
    conn: sqlite3.Connection, lieferant_id: int | None, belegnummer: object,
    belegdatum: object, brutto: object, *, waehrung: str = "EUR", euro: object = None,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """Beides: aehnliche Belege desselben Lieferanten und die bei anderen
    Lieferanten-Datensaetzen bzw. ohne Lieferant (ohne Doppelte)."""
    gleich = aehnliche_belege(conn, lieferant_id, belegnummer, belegdatum, brutto,
                              waehrung=waehrung, euro=euro)
    schon = {b["id"] for b in gleich}
    andere = [b for b in aehnliche_bei_anderen(conn, lieferant_id, belegnummer, belegdatum,
                                               brutto, waehrung=waehrung, euro=euro)
              if b["id"] not in schon]
    return gleich, andere


def aehnliche_als_text(aehnliche: list[sqlite3.Row] | tuple[sqlite3.Row, ...], *,
                       waehrung: str | None = None) -> str:
    """Die Warnung zur inhaltlichen Dublette, fuer Maske und Meldung.

    ``waehrung`` ist die Waehrung des NEUEN Belegs. Ist ein Treffer in einer
    anderen gebucht, sagt die Warnung das: derselbe Zahlenwert in Dollar und
    in Euro ist meist dieselbe Rechnung, bei der einmal das Waehrungszeichen
    nicht erkannt wurde (siehe :func:`_gleicher_betrag`).
    """
    from app.logik.rundung import euro

    teile = []
    for b in aehnliche:
        dort = b["waehrung"] or "EUR"
        if dort != "EUR" and b["betrag_fremdwaehrung_cent"]:
            betrag = (f"{euro(b['betrag_fremdwaehrung_cent'])} {dort} "
                      f"({euro(b['betrag_brutto_cent'])} €)")
        else:
            betrag = f"{euro(b['betrag_brutto_cent'])} €"
        was = (f"Beleg {b['id']}"
               + (f" mit Belegnummer {b['belegnummer_extern']}" if b["belegnummer_extern"]
                  else "")
               + f" vom {b['belegdatum'][8:10]}.{b['belegdatum'][5:7]}.{b['belegdatum'][:4]}"
               + f" über {betrag}")
        if waehrung and dort != waehrung:
            was += f" — dort in {dort}, hier in {waehrung}: ähnlich, Währung prüfen"
        teile.append((b["lieferant"] or "ohne Lieferant", was))
    # Je Lieferant ein Satz: bei einem Treffer unter einem anderen Datensatz
    # oder ohne Lieferant sagt er, wo der Beleg steht.
    gruppen: dict[str, list[str]] = {}
    for lieferant, was in teile:
        gruppen.setdefault(lieferant, []).append(was)
    return " ".join(
        (f"Ohne Lieferant gibt es bereits {'; '.join(liste)}." if lieferant == "ohne Lieferant"
         else f"Zu Lieferant {lieferant} gibt es bereits {'; '.join(liste)}.")
        for lieferant, liste in gruppen.items())


#: Schreibweisen des Euro, die als EUR gelten. Alles andere muss ein
#: dreistelliger ISO-4217-Code sein -- "US-Dollar" als Waehrung gespeichert
#: hiesse, dass jede Auswertung mit einem Freitext rechnet.
_EURO_SCHREIBWEISEN = {"", "EUR", "EURO", "€", "EUR€", "€EUR"}


def normalisiere_waehrung(text: object) -> str:
    """Ein Waehrungscode nach ISO 4217, drei Buchstaben.

    >>> normalisiere_waehrung("Euro"), normalisiere_waehrung("€"), normalisiere_waehrung("EUR ")
    ('EUR', 'EUR', 'EUR')
    >>> normalisiere_waehrung("usd"), normalisiere_waehrung(None)
    ('USD', 'EUR')
    >>> normalisiere_waehrung("US-Dollar")
    Traceback (most recent call last):
        ...
    app.logik.beleg.ergebnis.BelegFehler: „US-Dollar“ ist kein Währungscode. ...
    """
    roh = str(text or "").strip()
    kompakt = roh.replace(" ", "").upper()
    if kompakt in _EURO_SCHREIBWEISEN:
        return "EUR"
    if kompakt == "$":
        return "USD"
    if kompakt == "£":
        return "GBP"
    if len(kompakt) == 3 and kompakt.isascii() and kompakt.isalpha():
        return kompakt
    raise BelegFehler(
        f"„{roh}“ ist kein Währungscode. Bitte den dreistelligen Code nach ISO 4217 "
        "eintragen — EUR, USD, CHF, GBP."
    )


def _eigene_ust_idnr(conn: sqlite3.Connection) -> str | None:
    zeile = conn.execute("SELECT ust_idnr, kleinunternehmer_idnr FROM betrieb"
                         " WHERE id = 1").fetchone()
    if zeile is None:
        return None
    return zeile["ust_idnr"] or zeile["kleinunternehmer_idnr"]


def finde_lieferant(
    conn: sqlite3.Connection, name: str | None, ust_idnr: str | None
) -> sqlite3.Row | None:
    """Sucht den Lieferanten -- zuerst an der USt-IdNr., dann am Namen.

    Die USt-IdNr. zuerst, weil sie eindeutig ist: "Amazon Web Services EMEA
    SARL" und "AWS EMEA SARL" sind derselbe Lieferant, und ein Namensvergleich
    sieht das nicht.
    """
    if ust_idnr:
        zeile = conn.execute(
            "SELECT * FROM lieferant WHERE REPLACE(UPPER(ust_idnr), ' ', '') = ?",
            (ust_idnr.replace(" ", "").upper(),),
        ).fetchone()
        if zeile:
            return zeile
    if name:
        zeile = conn.execute(
            "SELECT * FROM lieferant WHERE name = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
        if zeile:
            return zeile
    return None


def lies(
    conn: sqlite3.Connection, eingang: Eingang, *, heute: date | None = None,
    bilderkennung: bool = True, textleser=None,
) -> Lesung:
    """Liest eine Eingangsdatei durch die Stufenfolge. Schreibt nichts.

    Die Stufen laufen in dieser Reihenfolge und brechen ab, sobald eine etwas
    Verbindliches geliefert hat:

    1. **XML** (ZUGFeRD, Factur-X, XRechnung) -- exakt, und wenn vorhanden,
       massgebend.
    2. **Textschicht des PDF** -- zeichengenau, deckt digital erzeugte
       Lieferantenrechnungen ab.
    3. **Apple Vision** -- nur bei Scan oder Foto, nur auf macOS.

    Danach kommt der Lieferanten-Lernspeicher darueber: er ist spezifischer als
    die allgemeine Auswertung und gewinnt deshalb bei jedem Feld, das er kennt.

    ``bilderkennung=False`` laesst Stufe 3 aus. Das Abholen aus dem
    Eingangsordner liest so: ein Fund der Bilderkennung ist nie sicher und
    wird ohnehin nicht automatisch gebucht -- die Erkennung zu starten hiesse
    nur, den Abholvorgang zu verlangsamen. Sie laeuft, sobald der Nutzer die
    Datei in der Pruefmaske oeffnet.

    ``textleser`` ersetzt ``text_leser.hole_text`` (das Abholen liest mit
    Zeitgrenze in einem eigenen Prozess); gelesen wird die Textschicht so
    oder so nur einmal.

    Eine E-Mail (.eml) liest :func:`_lies_mail`: mit Rechnung im Anhang
    diesen Anhang wie einen Upload, sonst den Text der Mail.
    """
    if eingang.endung == email_leser.ENDUNG:
        return _lies_mail(conn, eingang, heute=heute, bilderkennung=bilderkennung,
                          textleser=textleser)
    gebucht_als = dublette(conn, eingang.sha256)
    schichten: dict[Path, text_leser.Textschicht] = {}

    def textschicht(pfad: Path) -> text_leser.Textschicht:
        if pfad not in schichten:
            schichten[pfad] = (textleser or text_leser.hole_text)(pfad)
        return schichten[pfad]

    eigene = _eigene_ust_idnr(conn)
    stufen: list[str] = []
    meldungen: list[str] = []
    rohtext = ""

    # --- Stufe 1: XML. Ein Fehler hier ist endgueltig, kein Rueckfall.
    stufen.append("xml")
    try:
        vorschlag = xml_leser.lies_datei(eingang.pfad)
    except xml_leser.Gutschrift as gutschrift:
        # Eine Gutschrift ist keine Ausgabe. Sie wird nicht ausgelesen und
        # nicht gebucht; die Maske zeigt sie als das, was sie ist.
        return Lesung(
            eingang=eingang, vorschlag=Vorschlag("xml", {}, ()), stufen=("xml",),
            dublette_von=gebucht_als, gutschrift=str(gutschrift),
            meldungen=(str(gutschrift),),
        )
    if vorschlag is not None:
        meldungen.append(
            "Aus dem eingebetteten XML gelesen. Bei einer E-Rechnung ist das XML "
            "maßgebend; der Bildteil wurde nicht ausgelesen."
        )
    else:
        # --- Stufe 2: Textschicht.
        stufen.append("pdf_text")
        vorschlag = text_leser.lies_datei(
            eingang.pfad, heute=heute, eigene_ust_idnr=eigene, textleser=textschicht
        )
        if vorschlag is not None:
            rohtext = textschicht(eingang.pfad).text
        else:
            # --- Stufe 3: Bilderkennung -- beim Abholen nicht (siehe oben).
            stand = vision_mac.ist_verfuegbar() if bilderkennung else None
            if stand is None:
                meldungen.append(
                    "Keine Textschicht — die Bilderkennung läuft, sobald die Datei in "
                    "der Prüfmaske geöffnet wird."
                )
                vorschlag = Vorschlag("manuell", {}, ())
                stufen.append("manuell")
            elif not stand:
                stufen.append("ocr_lokal")
                meldungen.append(stand.begruendung)
                vorschlag = Vorschlag("manuell", {}, ())
                stufen.append("manuell")
            else:
                stufen.append("ocr_lokal")
                vorschlag = vision_mac.lies_datei(
                    eingang.pfad, heute=heute, eigene_ust_idnr=eigene
                )
                rohtext = ""

    return _abschluss(conn, eingang, vorschlag, stufen, meldungen, rohtext, gebucht_als,
                      heute=heute)


def _abschluss(
    conn: sqlite3.Connection, eingang: Eingang, vorschlag: Vorschlag, stufen: list[str],
    meldungen: list[str], rohtext: str, gebucht_als: int | None, *, heute: date | None,
) -> Lesung:
    """Was nach den Stufen kommt: Lieferant, Lernspeicher, Dubletten."""
    # --- Lieferant und Lernspeicher.
    lieferant = finde_lieferant(
        conn, vorschlag.wert("lieferant"), vorschlag.wert("lieferant_ust_idnr")
    )
    lieferant_id = lieferant["id"] if lieferant else None
    if lieferant is not None:
        # Der bekannte Name ersetzt den geratenen: der Nutzer hat ihn einmal
        # bestaetigt, die Heuristik raet ihn jedes Mal neu.
        vorschlag = Vorschlag(
            vorschlag.quelle,
            {**vorschlag.felder, "lieferant": Fund(
                lieferant["name"], vorschlag.quelle,
                f"Bekannter Lieferant {lieferant['name']}", True)},
            vorschlag.meldungen,
            vorschlag.bedenken,
        )
        if rohtext:
            gelernt = profil.anwenden(conn, lieferant_id, rohtext,
                                      quelle=vorschlag.quelle, heute=heute)
            vorschlag = gelernt.ergaenzt_um(vorschlag)
            stufen.append("profil")

    if gebucht_als is not None:
        meldungen.insert(0, (
            f"Diese Datei ist bereits als Beleg {gebucht_als} gebucht — Byte für Byte "
            "dieselbe. Ein Beleg zweimal gebucht ist der häufigste Erfassungsfehler."
        ))
    else:
        geloescht = frueher_geloescht(conn, eingang.sha256)
        if geloescht is not None:
            # Der Normalfall nach "falsch ausgelesen": geloescht und neu
            # hochgeladen. Kein Fehler -- aber gesagt, damit niemand meint,
            # die Dublettenpruefung habe versagt.
            meldungen.insert(0, (
                f"Diese Datei war Beleg {geloescht['id']} — gelöscht "
                f"({geloescht['geloescht_grund']}). Sie wird als neuer Beleg erfasst; der "
                "gelöschte bleibt mit seiner Datei im Archiv."
            ))

    # Der Betrag aus der Auslesung steht in der Waehrung des Belegs -- bei
    # einer Dollar-Rechnung in Dollar. Erkannt oder, wenn der Beleg sie nicht
    # nennt, die gemerkte Waehrung des Lieferanten.
    waehrung = _waehrung_oder_eur(
        vorschlag.wert("waehrung") or (lieferant["waehrung_default"] if lieferant else None))
    # Auch unter einem anderen Lieferanten-Datensatz und ohne Lieferant: wer
    # dieselbe Rechnung zweimal hochlaedt, waehlt nicht unbedingt beide Male
    # denselben Lieferanten (oder beim ersten Mal gar keinen).
    aehnliche, aehnliche_andere = aehnliche_ueberall(
        conn, lieferant_id, vorschlag.wert("belegnummer"),
        vorschlag.wert("belegdatum"), vorschlag.wert("brutto"), waehrung=waehrung,
    ) if gebucht_als is None else ([], [])

    return Lesung(
        eingang=eingang,
        vorschlag=vorschlag,
        rohtext=rohtext,
        stufen=tuple(stufen),
        dublette_von=gebucht_als,
        lieferant_id=lieferant_id,
        meldungen=tuple(meldungen),
        aehnliche=tuple(aehnliche),
        aehnliche_andere=tuple(aehnliche_andere),
        waehrung=waehrung,
    )


def _anhang_ablegen(mail: Eingang, anhang: email_leser.Anhang) -> Eingang:
    """Legt einen Anhang der Mail versteckt neben sie in den Eingang
    (``.<Mail>.anhang<Nr><Endung>``) -- die Stufen lesen Dateien, und beim
    Buchen wandert er von dort ins Archiv. Versteckt, damit er weder in der
    Eingangsliste noch ueber eine URL als eigene Datei auftaucht; weggeraeumt
    wird er mit der Mail (:func:`vergiss_original_name`)."""
    endung = anhang.endung if anhang.endung in ERLAUBT else ""
    if not endung:
        endung = {"application/pdf": ".pdf", "application/xml": ".xml", "text/xml": ".xml",
                  "image/jpeg": ".jpg", "image/png": ".png"}.get(anhang.mime, ".bin")
    name = anhang.name if Path(anhang.name).suffix.lower() == endung else anhang.name + endung
    ziel = mail.pfad.with_name(f".{mail.pfad.name}.anhang{anhang.nr}{endung}")
    hash_ = hashlib.sha256(anhang.daten).hexdigest()
    if not ziel.is_file() or pruefsumme(ziel) != hash_:
        zwischen = ziel.with_name(f"{ziel.name}.{secrets.token_hex(6)}.teil")
        try:
            zwischen.write_bytes(anhang.daten)
            os.replace(zwischen, ziel)
        finally:
            zwischen.unlink(missing_ok=True)
    return Eingang(pfad=ziel, original_name=name, sha256=hash_, groesse=len(anhang.daten))


def _lies_mail(
    conn: sqlite3.Connection, eingang: Eingang, *, heute: date | None,
    bilderkennung: bool, textleser,
) -> Lesung:
    """Eine E-Mail: mit echter Rechnung im Anhang ist DER der Beleg, sonst
    der Text der Mail (Entscheidungen in app/logik/beleg/email_leser.py).

    Der Anhang wird gelesen wie ein Upload -- dieselbe Stufenfolge, XML vor
    Textschicht. Verkaufsbedingungen, AGB, Widerruf und Datenschutz sind
    keine Rechnung (am Dateinamen oder am Anfang des Textes) und werden
    uebergangen, ebenso ein XML, das keine E-Rechnung ist.
    """
    mail = email_leser.lies_datei(eingang.pfad)
    gebucht_als = dublette(conn, eingang.sha256)
    uebergangen = [f"{a.name} ({a.grund})" for a in mail.anhaenge
                   if a.art == "bedingungen" or a.grund == "XML, aber keine E-Rechnung"]
    for kandidat in (a for a in mail.anhaenge if a.art == "kandidat"):
        anhang = _anhang_ablegen(eingang, kandidat)
        teil = lies(conn, anhang, heute=heute, bilderkennung=bilderkennung,
                    textleser=textleser)
        if anhang.endung == ".xml" and teil.vorschlag.quelle != "xml" \
                and not teil.ist_gutschrift:
            uebergangen.append(f"{anhang.original_name} (XML, aber keine E-Rechnung)")
            continue
        if teil.vorschlag.quelle == "pdf_text" and email_leser.sind_bedingungen(
                anhang.original_name, teil.rohtext):
            uebergangen.append(f"{anhang.original_name} (Bedingungen oder Hinweise, "
                               "keine Rechnung)")
            continue
        hinweis = (f"Die Rechnung steckt im Anhang „{anhang.original_name}“ der E-Mail: der "
                   "Anhang ist der Beleg und wurde ausgelesen wie ein Upload; die E-Mail wird "
                   "mit ihm archiviert.")
        if uebergangen:
            hinweis += f" Übergangen: {', '.join(uebergangen)}."
        dublette_von = teil.dublette_von if teil.dublette_von is not None else gebucht_als
        meldungen = teil.meldungen
        if dublette_von is not None and teil.dublette_von is None:
            meldungen = (f"Diese Datei ist bereits als Beleg {dublette_von} gebucht — Byte "
                         "für Byte dieselbe E-Mail.", *meldungen)
        return replace(teil, eingang=eingang, anhang=anhang, dublette_von=dublette_von,
                       meldungen=(hinweis, *meldungen),
                       aehnliche=() if dublette_von is not None else teil.aehnliche,
                       aehnliche_andere=(() if dublette_von is not None
                                         else teil.aehnliche_andere))

    vorschlag, rohtext = email_leser.felder(mail, heute=heute,
                                            eigene_ust_idnr=_eigene_ust_idnr(conn))
    meldungen: list[str] = []
    if uebergangen:
        meldungen.append(f"Übergangen: {', '.join(uebergangen)}.")
    return _abschluss(conn, eingang, vorschlag, ["email"], meldungen, rohtext, gebucht_als,
                      heute=heute)


def netto_und_steuer(
    brutto: int, netto: object, ust: object, satz: object, *, reverse_charge: bool,
) -> tuple[int | None, int | None, object]:
    """Netto, Steuer und Satz eines Euro-Belegs, so wie ``buche`` sie speichert.

    Netto und Steuer gehoeren zusammen, und ihre Summe muss das Brutto
    ergeben -- das Schema verlangt beides. Passt es nicht, wird KEINES von
    beiden gespeichert: ein halb ausgelesener Beleg gilt spaeter als
    vollstaendig, und dann rechnet jemand mit einer Zahl, die nie geprueft
    wurde. Eine eigene Funktion, damit das automatische Buchen die Wertgrenze
    mit genau dem Netto prueft, das danach gespeichert wird.

    >>> netto_und_steuer(546, 459, 87, 190, reverse_charge=False)
    (459, 87, 190)
    >>> netto_und_steuer(200000, 10000, None, 190, reverse_charge=False)
    (None, None, 190)
    >>> netto_und_steuer(18000, None, None, None, reverse_charge=True)
    (18000, 0, 0)
    """
    if reverse_charge:
        # Steuerfrei: der Beleg weist keine Steuer aus, Netto ist das Brutto.
        # Die Maske belegt die Steuer mit 0,00 vor; bleibt das Feld trotzdem
        # leer, gilt dasselbe -- sonst gingen Netto und Steuer beide verloren,
        # und dasselbe Dokument ergaebe je nach Klick zwei Datensaetze.
        if isinstance(netto, int) and netto != brutto:
            raise BelegFehler(
                "Bei Reverse Charge weist der Beleg keine Umsatzsteuer aus: "
                "Netto muss gleich Brutto sein."
            )
        return brutto, 0, 0
    if isinstance(netto, int) and isinstance(ust, int) and netto + ust == brutto:
        return netto, ust, satz
    return None, None, satz


def grenzwert_netto_cent(brutto: int, netto: object, satz: object) -> int:
    """Der Nettowert fuer die Wertgrenzen -- aus den bereinigten Betraegen."""
    return kategorien.netto_fuer_grenzwert_cent(
        brutto, netto if isinstance(netto, int) else None,
        satz if isinstance(satz, int) else None,
    )


def archivpfad(datenordner: Path, belegdatum: str, sha256: str, endung: str) -> Path:
    """Wohin das Original kommt: ``belege/<Jahr>/<Datum>_<Hash>.<Endung>``.

    Nach **Belegdatum** gegliedert und nicht nach Importdatum: in der Pruefung
    sucht niemand nach dem Tag, an dem jemand etwas hochgeladen hat. Der Hash
    im Namen macht den Pfad eindeutig, ohne einen Zaehler zu brauchen.
    """
    jahr = belegdatum[:4]
    return datenordner / ARCHIV / jahr / f"{belegdatum}_{sha256[:12]}{endung}"


#: Die Meldung, wenn jemand eine erkannte Gutschrift als Ausgabe buchen will.
GUTSCHRIFT_KEINE_AUSGABE = (
    "Eine Gutschrift ist keine Ausgabe. Sie mindert die Ausgabe des ursprünglichen "
    "Belegs — dort als Erstattung erfassen."
)


@dataclass(frozen=True, slots=True)
class Buchung:
    beleg_id: int
    datei_id: int
    pfad: Path
    gelernt: tuple[str, ...] = ()
    #: Bei Fremdwaehrung: wie der Euro-Betrag zustande kam.
    umrechnung: umrechnung.Umrechnung | None = None
    #: Der Lieferant des Belegs -- auch einer, der erst beim Buchen entstand.
    lieferant_id: int | None = None


def buche(
    conn: sqlite3.Connection,
    lesung: Lesung,
    bestaetigt: dict[str, object],
    datenordner: Path,
    *,
    lieferant_id: int | None = None,
    kategorie_id: int | None = None,
    quote: int = 100,
    notiz: str | None = None,
    zahlungsart: str = "UEBERWEISUNG",
    ist_anlagegut: bool = False,
    reise_id: int | None = None,
    trotzdem: bool = False,
    heute: date | None = None,
    automatisch: bool = False,
    lieferant_neu: tuple[str, str | None] | None = None,
    bezahlt_am: str | None = None,
    zahlung_begruendung: str | None = None,
    vermerk: str | None = None,
) -> Buchung:
    """Schreibt den bestaetigten Beleg fest und archiviert das Original.

    ``bezahlt_am`` legt die Zahlung gleich mit an (Ausgabe, negativ, mit
    ``zahlungsart``), in DERSELBEN Transaktion wie den Beleg. Vorher schrieb
    die Pruefmaske sie danach in einer eigenen Transaktion -- scheiterte die,
    stand der Beleg ohne Zahlung da, und nichts sagte es (fuer die EUeR zaehlt
    nach § 11 EStG gerade der Zahltag). ``zahlung_begruendung`` steht am
    Journaleintrag ZAHLUNG, etwa die Vorgabe "Zahltag = Belegdatum"
    (app/logik/beleg/bezahlt.py). Bei Fremdwaehrung ist der Zahltag auch der
    Stichtag des Kurses, solange ``umrechnung_am`` nichts anderes sagt.

    ``vermerk`` haengt bei ``automatisch`` am Verfahren (Vorgabe:
    :data:`AUTOMATISCH_VERMERK`, das Abholen aus dem Eingangsordner); jeder
    Vermerk endet mit :data:`AUTOMATISCH_KENNUNG`.

    ``lieferant_neu`` (Name, USt-IdNr.) ist ein in der Pruefmaske neu
    getippter Lieferant. Er entsteht erst hier, in DERSELBEN Transaktion wie
    der Beleg (:func:`_lieferant_anlegen`). Vorher legte die Route ihn an und
    schrieb ihn fest, bevor ``buche`` den Beleg -- etwa als Dublette --
    abwies: der Lieferant blieb ohne Beleg stehen, der naechste Versuch legte
    womoeglich einen zweiten an, und die Duplikatpruefung je Lieferant sah
    die Belege des anderen Datensatzes nicht mehr.

    ``automatisch`` heisst: niemand hat die Felder bestaetigt -- das Abholen
    aus dem Eingangsordner bucht eine sicher erkannte Rechnung eines bekannten
    Lieferanten selbst (app/logik/beleg/abholen.py). Dann bleibt
    ``geprueft_am`` leer (der Beleg steht unter "nur ungeklaerte", bis der
    Nutzer "Stimmt so" klickt), der Lernspeicher lernt NICHT (gelernt wird nur
    aus bestaetigten Werten, sonst bestaetigte sich die Erkennung selbst), und
    Verfahren und Journal sagen "automatisch".

    ``trotzdem`` ist die ausdrueckliche Bestaetigung des Nutzers, dass ein
    inhaltlich aehnlicher Beleg (gleicher Lieferant, gleiche Belegnummer oder
    gleiches Datum mit gleichem Betrag) wirklich ein anderer ist. Ohne sie
    wird die Buchung abgewiesen -- dieselbe Rechnung als zweites PDF ist der
    haeufigste Erfassungsfehler, und die Pruefsumme sieht ihn nicht.

    Reihenfolge wie beim Festschreiben einer Rechnung: **erst** die Datei an
    ihren Platz, **dann** die Datensaetze in einer Transaktion. Scheitert das
    Verschieben, entsteht kein Datensatz. Scheitert die Transaktion, wandert
    die Datei zurueck in den Eingang -- sonst laege sie im Archiv, ohne dass
    ein Datensatz auf sie zeigt, und der Prueflauf, der nur von den
    Datensaetzen ausgeht, faende sie nie.

    Eine Dublette wird IMMER abgewiesen. Es gab einen Schalter "bewusst
    buchen"; er konnte nie gelingen, weil der UNIQUE-Index auf der Pruefsumme
    des Originals dieselbe Datei kein zweites Mal zulaesst -- und auf dem Weg
    dahin war die Datei schon ins Archiv verschoben. Zwei Buchungen zu EINER
    Datei sind auch kein Anwendungsfall: eine zweite Zahlung gehoert als
    zweite Zahlung an den vorhandenen Beleg.

    Doppelklick: zwei Anfragen fuer dieselbe Datei laufen gleichzeitig. Die
    Sperre (BEGIN IMMEDIATE) steht deshalb VOR dem Verschieben, und erst
    innerhalb der Sperre wird geprueft, ob die Pruefsumme schon gebucht ist und
    ob die Datei noch im Eingang liegt. Vorher verschoben beide, eine scheiterte
    am UNIQUE-Index -- und schob beim Aufraeumen die Archivdatei des GEBUCHTEN
    Belegs zurueck in den Eingang. Zurueckgeschoben wird nur, was diese Anfrage
    selbst verschoben hat.
    """
    if lesung.ist_dublette:
        raise SchonGebucht(lesung.dublette_von)
    if lesung.ist_gutschrift:
        raise BelegFehler(GUTSCHRIFT_KEINE_AUSGABE)
    if lesung.eingang.endung not in ERLAUBT:
        raise BelegFehler(
            f"{lesung.eingang.original_name} ist kein Belegformat — wegwerfen oder "
            f"umbenennen. Möglich sind {', '.join(sorted(ERLAUBT))}."
        )

    belegdatum = str(bestaetigt.get("belegdatum") or "").strip()
    try:
        tag = date.fromisoformat(belegdatum)
    except ValueError as fehler:
        raise BelegFehler("Ohne gültiges Belegdatum kann nicht gebucht werden.") from fehler
    stichtag_heute = heute or date.today()
    if tag > stichtag_heute:
        # Ein Tippfehler im Jahr (2062 statt 2026) wurde ohne Rueckfrage
        # gebucht, und die Datei wanderte nach belege/2062/.
        raise BelegFehler(
            f"Das Belegdatum {tag.strftime('%d.%m.%Y')} liegt in der Zukunft — "
            "vermutlich ein Tippfehler im Jahr."
        )
    # Das gemeinsame Fenster (Jahr ab 2000) und die gelesene Form: "20260314"
    # nahm fromisoformat() an, roh gespeichert fiel es aus jeder Auswertung,
    # und die Datei landete unter belege/2026/20260314_....
    try:
        belegdatum = eingabe.datum(belegdatum, feld="Belegdatum", heute=stichtag_heute)
    except ValueError as fehler:
        raise BelegFehler(str(fehler)) from None
    bestaetigt = {**bestaetigt, "belegdatum": belegdatum}
    zahltag: str | None = None
    if bezahlt_am:
        # Eine Zahlung in der Zukunft ist noch keine -- fuer die EUeR zaehlt
        # der Abfluss. Gespeichert wird die gelesene Form.
        try:
            zahltag = eingabe.datum(str(bezahlt_am), feld="Zahlungsdatum",
                                    heute=stichtag_heute)
        except ValueError as fehler:
            raise BelegFehler(str(fehler)) from None
        bestaetigt = {**bestaetigt, "bezahlt_am": zahltag}

    brutto = bestaetigt.get("brutto")
    if not isinstance(brutto, int) or brutto <= 0:
        raise BelegFehler("Ohne Bruttobetrag größer als null kann nicht gebucht werden.")

    netto = bestaetigt.get("netto")
    ust = bestaetigt.get("ust")
    satz = bestaetigt.get("ust_satz_promille")
    reverse_charge = bool(bestaetigt.get("reverse_charge"))
    if reverse_charge and (
        (isinstance(ust, int) and ust != 0) or (isinstance(satz, int) and satz > 0)
    ):
        # Vorher wurden Netto und Steuer still verworfen, der Satz 19 % blieb
        # aber am Reverse-Charge-Beleg stehen: ein widerspruechlicher Datensatz.
        raise BelegFehler("Reverse Charge und ausgewiesene Umsatzsteuer schließen sich aus.")

    # Fremdwaehrung. In die EUeR gehoert der Euro-Betrag, der tatsaechlich
    # abgeflossen ist; steht er nicht fest, der zum EZB-Referenzkurs am
    # Zahlungstag (sonst am Belegdatum) umgerechnete -- die Entscheidungen
    # stehen in app/logik/beleg/umrechnung.py. Die Umrechnung laedt bei Bedarf
    # die Kurstabelle und laeuft deshalb VOR der Sperre. Netto und Steuer
    # eines Fremdwaehrungsbelegs werden nicht uebernommen: eine Umrechnung je
    # Zeile erzeugt Rundungsdifferenzen, die das Schema (netto + ust = brutto)
    # zu Recht abweist, und die Steuer eines Drittlands ist keine deutsche
    # Umsatzsteuer.
    waehrung = normalisiere_waehrung(bestaetigt.get("waehrung"))
    fremdbetrag: int | None = None
    euro_umrechnung: umrechnung.Umrechnung | None = None
    if waehrung != "EUR":
        # "umrechnung_am" und nicht "zahlungsdatum": unter diesem Namen lernte
        # das Lieferantenprofil das Feld, und der Tag, an dem der Nutzer
        # bezahlt hat, steht nicht auf dem Beleg.
        stichtag = date.fromisoformat(zahltag) if zahltag else tag
        if bestaetigt.get("umrechnung_am"):
            try:
                stichtag = date.fromisoformat(str(bestaetigt["umrechnung_am"]))
            except ValueError:
                raise BelegFehler("Das Zahlungsdatum ist kein gültiges Datum.") from None
        karte = bestaetigt.get("brutto_eur")
        euro_umrechnung = umrechnung.berechne(
            datenordner, waehrung, brutto, stichtag,
            karte_cent=karte if isinstance(karte, int) else None,
            kurs_manuell=bestaetigt.get("kurs_manuell") or None,
            karte_bestaetigt=bool(bestaetigt.get("karte_bestaetigt")),
            kurs_bestaetigt=bool(bestaetigt.get("kurs_bestaetigt")),
            heute=stichtag_heute,
        )
        fremdbetrag, brutto = brutto, euro_umrechnung.euro_cent
        netto = ust = satz = None

    if waehrung == "EUR":
        netto, ust, satz = netto_und_steuer(brutto, netto, ust, satz,
                                            reverse_charge=reverse_charge)

    if not 0 <= quote <= 100:
        raise BelegFehler(f"Die betriebliche Quote muss zwischen 0 und 100 liegen, nicht {quote}.")

    betrieblich = kategorien.betrieblich_cent(brutto, quote)
    grenzwert_netto = grenzwert_netto_cent(brutto, netto, satz)

    # Die Dateien des Belegs: das Original -- und bei einer Rechnung im
    # Anhang einer Mail die Mail dazu (Rolle EMAIL), beide unveraendert,
    # mit Pruefsumme und schreibgeschuetzt im Archiv.
    dateien: list[tuple[Eingang, str]] = (
        [(lesung.anhang, "ORIGINAL"), (lesung.eingang, "EMAIL")] if lesung.anhang is not None
        else [(lesung.eingang, "ORIGINAL")])
    ziele = [archivpfad(datenordner, belegdatum, datei.sha256, datei.endung)
             for datei, _ in dateien]
    ziel = ziele[0]
    #: (Archivpfad, Eingangspfad) jeder Datei, die DIESE Anfrage verschoben hat.
    verschoben: list[tuple[Path, Path]] = []
    # Die Sperre VOR jeder Dateibewegung (siehe Docstring: Doppelklick).
    conn.execute("BEGIN IMMEDIATE")
    try:
        for datei, _ in dateien:
            schon = dublette(conn, datei.sha256)
            if schon is not None:
                raise SchonGebucht(schon)
        for datei, _ in dateien:
            passt = inhalt_passt(datei.pfad) if datei.pfad.is_file() else None
            if passt is None:
                raise BelegFehler(f"{datei.original_name} liegt nicht mehr im Eingang.")
            if not passt:
                # Von Hand in den Eingang gelegt, am Upload vorbei: ein Programm
                # mit der Endung .pdf laege sonst schreibgeschuetzt im Archiv.
                raise BelegFehler(inhalt_fremd(datei.original_name, datei.endung))

        if lieferant_id is None and lieferant_neu is not None:
            lieferant_id = _lieferant_anlegen(conn, *lieferant_neu)

        # Beim selben Lieferanten und -- enger -- bei anderen Datensaetzen
        # und ohne Lieferant (siehe aehnliche_bei_anderen).
        gleich, andere = aehnliche_ueberall(
            conn, lieferant_id, bestaetigt.get("belegnummer"), belegdatum,
            fremdbetrag if fremdbetrag is not None else brutto, waehrung=waehrung,
            euro=brutto if fremdbetrag is not None else None)
        aehnliche = gleich + andere
        if aehnliche and not trotzdem:
            raise AehnlicherBeleg(
                aehnliche_als_text(aehnliche, waehrung=waehrung)
                + " Ist es wirklich ein anderer Beleg, "
                "„trotzdem buchen“ anhaken."
            )

        hashes: list[str] = []
        for (datei, _), ablage in zip(dateien, ziele, strict=True):
            ablage.parent.mkdir(parents=True, exist_ok=True)
            if ablage.exists() and ablage != datei.pfad:
                # Dieselbe Datei am selben Tag: der Hash im Namen macht das
                # eindeutig, also ist es wirklich dieselbe. Nichts ueberschreiben.
                os.chmod(ablage, NUR_LESEN)
            else:
                shutil.move(str(datei.pfad), ablage)
                verschoben.append((ablage, datei.pfad))
                os.chmod(ablage, NUR_LESEN)
            # Die Pruefsumme NACH dem Verschieben neu bilden: sie ist der
            # Nachweis, dass genau diese Datei im Archiv liegt, und nicht die
            # Behauptung, dass eine andere einmal denselben Hash hatte.
            archiv_hash = pruefsumme(ablage)
            if archiv_hash != datei.sha256:
                raise BelegFehler(
                    f"Die Datei hat sich beim Archivieren verändert: erwartet "
                    f"{datei.sha256[:12]}, gefunden {archiv_hash[:12]}."
                )
            hashes.append(archiv_hash)
        archiv_hash = hashes[0]

        beleg_id = conn.execute(
            """
            INSERT INTO beleg (art, lieferant_id, kategorie_id, belegdatum,
                               belegnummer_extern, waehrung, betrag_fremdwaehrung_cent,
                               kurs, kurs_datum, kurs_quelle, kurs_abruf_utc,
                               kurs_tabelle_sha256, betrag_umgerechnet_cent,
                               betrag_karte_cent, betrag_brutto_cent,
                               betrag_netto_cent, betrag_ust_cent, ust_satz_promille,
                               reverse_charge, netto_fuer_grenzwert_cent,
                               aufteilungsquote, betrag_betrieblich_cent,
                               reise_id, ist_anlagegut, zahlungsart, notiz,
                               geprueft_am, erfasst_am)
            VALUES ('EINGANG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?)
            """,
            (
                lieferant_id, kategorie_id, belegdatum,
                (bestaetigt.get("belegnummer") or None),
                waehrung, fremdbetrag,
                *(euro_umrechnung.spalten().values() if euro_umrechnung
                  else (None,) * 7),
                brutto, netto, ust, satz, int(reverse_charge), grenzwert_netto,
                quote, betrieblich, reise_id, int(ist_anlagegut), zahlungsart, notiz,
                None if automatisch else db.jetzt_utc(), db.jetzt_utc(),
            ),
        ).lastrowid

        # Der Pfad RELATIV zum Datenordner: nur so ueberlebt er eine
        # Wiederherstellung an anderer Stelle. Siehe app/pfade.py.
        datei_ids = [
            conn.execute(
                "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse,"
                " mime, rolle, importiert_am) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (beleg_id, pfade.relativ(datenordner, ablage), datei.original_name,
                 datei_hash, ablage.stat().st_size, _mime(datei.endung), rolle,
                 db.jetzt_utc()),
            ).lastrowid
            for (datei, rolle), ablage, datei_hash in zip(dateien, ziele, hashes, strict=True)
        ]
        datei_id = datei_ids[0]

        # Das bestaetigte Ergebnis ist nach GoBD Rz. 130 selbst
        # aufbewahrungspflichtig und kein Zwischenergebnis.
        import json

        verfahren = _verfahren(lesung.stufen)
        if lesung.anhang is not None:
            verfahren += ("; Beleg aus dem Anhang einer E-Mail (.eml), die E-Mail ist als "
                          "Begleitdatei archiviert")
        conn.execute(
            "INSERT INTO extraktion (beleg_id, quelle, rohantwort_json, bestaetigt_json,"
            " verfahren, regel_version, datei_sha256, zeitpunkt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                beleg_id, lesung.vorschlag.quelle,
                json.dumps({name: {"wert": fund.wert, "quelle": fund.quelle,
                                   "nachweis": fund.nachweis, "sicher": fund.sicher}
                            for name, fund in lesung.vorschlag.felder.items()},
                           ensure_ascii=False, sort_keys=True),
                json.dumps({**bestaetigt, **({"automatisch": True} if automatisch else {})},
                           ensure_ascii=False, sort_keys=True, default=str),
                verfahren + ((vermerk or AUTOMATISCH_VERMERK) if automatisch else ""),
                f.REGEL_VERSION, archiv_hash, db.jetzt_utc(),
            ),
        )

        gelernt: list[str] = []
        if lieferant_id and lesung.rohtext and not automatisch:
            gelernt = profil.lerne(conn, lieferant_id, lesung.rohtext, bestaetigt)

        db.protokolliere(
            conn, "BELEG_ERFASSEN", "beleg", beleg_id,
            wert_neu={"brutto_cent": brutto, "datum": belegdatum,
                      "quelle": lesung.vorschlag.quelle,
                      **({"umrechnung": euro_umrechnung.journal()}
                         if euro_umrechnung else {}),
                      **({"email_sha256": hashes[1]} if lesung.anhang is not None else {}),
                      **({"automatisch": True} if automatisch else {})},
            hash_=archiv_hash,
        )
        if zahltag is not None:
            # Geflossen ist der Euro-Betrag -- bei Fremdwaehrung der
            # umgerechnete (oder der laut Kreditkarte), wie am Beleg: Beleg
            # und Zahlung widersprechen sich nicht (umrechnung.py,
            # Entscheidung 3). Negativ, weil es eine Ausgabe ist.
            conn.execute(
                "INSERT INTO zahlung (beleg_id, datum, betrag_cent, art) VALUES (?, ?, ?, ?)",
                (beleg_id, zahltag, -abs(brutto), zahlungsart),
            )
            db.protokolliere(conn, "ZAHLUNG", "beleg", beleg_id, feld="datum",
                             wert_neu=zahltag, begruendung=zahlung_begruendung)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        for ablage, zurueck in reversed(verschoben):
            # Zurueck in den Eingang. Ohne Datensatz waere die Datei im
            # Archiv eine Waise: schreibgeschuetzt, unter einem Hash-Namen,
            # und von pruefe_integritaet() nur ueber den Datenordner zu
            # finden. Im Eingang dagegen sieht sie der Nutzer beim naechsten
            # Oeffnen der Belegseite und kann sie erneut buchen.
            try:
                os.chmod(ablage, 0o644)
                shutil.move(str(ablage), zurueck)
            except OSError:
                pass
        raise
    bewegt = {zurueck for _, zurueck in verschoben}
    for (datei, _), ablage in zip(dateien, ziele, strict=True):
        if datei.pfad not in bewegt and datei.pfad != ablage:
            # Die Archivdatei lag schon da -- etwa von einem Buchen, das nach
            # dem Verschieben und vor dem Festschreiben abbrach (Prozess
            # beendet). Ihr Inhalt ist oben gegen die Pruefsumme geprueft; die
            # Kopie im Eingang ist dieselbe Datei und bliebe sonst als Doppel
            # liegen.
            try:
                datei.pfad.unlink(missing_ok=True)
            except OSError:
                pass
    vergiss_original_name(lesung.eingang.pfad)
    return Buchung(beleg_id=beleg_id, datei_id=datei_id, pfad=ziel,
                   gelernt=tuple(gelernt), umrechnung=euro_umrechnung,
                   lieferant_id=lieferant_id)


def _lieferant_anlegen(conn: sqlite3.Connection, name: str, ust_idnr: str | None) -> int:
    """Der Lieferant zu Name und USt-IdNr. -- vorhanden oder neu angelegt.

    Nur innerhalb einer offenen Transaktion (``buche``): unter der Sperre
    gesucht und angelegt, so legen zwei gleichzeitige Anfragen ihn nicht
    zweimal an; wird die Buchung abgewiesen, verschwindet er mit ihr.
    """
    vorhanden = finde_lieferant(conn, name, ust_idnr)
    if vorhanden is not None:
        return vorhanden["id"]
    neue_id = conn.execute(
        "INSERT INTO lieferant (name, ust_idnr, angelegt_am) VALUES (?, ?, ?)",
        (name, ust_idnr, db.heute()),
    ).lastrowid
    db.protokolliere(conn, "ANLEGEN", "lieferant", neue_id, wert_neu={"name": name})
    return neue_id


#: Woran Belegliste und EUeR "automatisch gebucht, noch nicht bestaetigt"
#: erkennen: jeder Vermerk eines automatischen Wegs endet so -- auch die
#: aelteren, die es schon in Datenbanken gibt.
AUTOMATISCH_KENNUNG = "gebucht, nicht bestätigt"

#: Was an ``extraktion.verfahren`` haengt, wenn das Abholen selbst gebucht hat.
AUTOMATISCH_VERMERK = f"; automatisch aus dem Eingangsordner {AUTOMATISCH_KENNUNG}"

#: Dasselbe fuer "Alle prüfen und buchen" (app/logik/beleg/sammelbuchung.py).
SAMMEL_VERMERK = f"; automatisch per „Alle prüfen und buchen“ aus dem Eingang {AUTOMATISCH_KENNUNG}"


def _verfahren(stufen: tuple[str, ...]) -> str:
    """Womit ausgelesen wurde -- fuer die Verfahrensdokumentation."""
    if "ocr_lokal" in stufen:
        return vision_mac.VERFAHREN
    if "xml" in stufen and len(stufen) == 1:
        return "Eingebettetes XML (CII oder UBL), gelesen mit xml.etree"
    if "pdf_text" in stufen:
        return "Textschicht des PDF, gelesen mit pdfplumber"
    if "email" in stufen:
        return email_leser.VERFAHREN
    return "Manuelle Erfassung"


_MIME = {
    ".pdf": "application/pdf", ".xml": "application/xml",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".heic": "image/heic",
    ".webp": "image/webp", ".eml": email_leser.MIME,
}


def _mime(endung: str) -> str | None:
    return _MIME.get(endung)


def liegen_geblieben(datenordner: Path) -> list[Path]:
    """Dateien im Eingang, die noch nicht gebucht sind.

    Sie sind nicht verloren, sondern nur nicht erfasst -- und das soll die
    Oberflaeche zeigen koennen. Ein Eingangsordner, den niemand ansieht, ist
    derselbe Zustand wie ein Schuhkarton.
    """
    ordner = datenordner / EINGANG
    if not ordner.is_dir():
        return []
    return sorted(p for p in ordner.iterdir() if p.is_file() and not p.name.startswith("."))


def pruefe_integritaet(
    conn: sqlite3.Connection,
    datenordner: Path,
    *,
    rechte_gesetzt: list[str] | None = None,
) -> list[dict[str, object]]:
    """Vergleicht jede archivierte Datei mit ihrer Pruefsumme.

    Das ist der Prueflauf, der die Unveraenderbarkeit nachweist. Ohne ihn ist
    die gespeicherte Pruefsumme eine Behauptung.

    Er laeuft auch in die andere Richtung: Dateien im Archiv, auf die kein
    Datensatz zeigt. Die entstehen, wenn etwas zwischen Verschieben und
    Buchen abbricht, und von den Datensaetzen aus sind sie unsichtbar -- dort
    fehlt ja gerade der Eintrag.

    Abweichende Dateirechte bei unveraendertem Inhalt sind kein Befund: Wer
    eine Sicherung von Hand entpackt, bekommt die Dateien mit normalen Rechten
    zurueck, und die Pruefung meldete danach jede Belegdatei rot, obwohl
    nichts veraendert war. Gelingt das Zuruecksetzen auf 0444, landet der
    Pfad in ``rechte_gesetzt`` (falls uebergeben); nur ein gescheitertes
    Zuruecksetzen bleibt ein Befund.
    """
    befunde: list[dict[str, object]] = []
    bekannte: set[str] = set()
    for zeile in conn.execute(
        "SELECT d.*, b.belegdatum, b.betrag_brutto_cent FROM belegdatei d"
        " JOIN beleg b ON b.id = d.beleg_id ORDER BY b.belegdatum, d.id"
    ):
        pfad = pfade.absolut(datenordner, zeile["pfad"])
        bekannte.add(str(pfad.resolve()) if pfad.exists() else str(pfad))
        if not pfad.exists():
            befunde.append({"beleg_id": zeile["beleg_id"], "pfad": zeile["pfad"],
                            "befund": "Datei fehlt"})
            continue
        if pruefsumme(pfad) != zeile["sha256"]:
            befunde.append({"beleg_id": zeile["beleg_id"], "pfad": zeile["pfad"],
                            "befund": "Prüfsumme stimmt nicht — die Datei wurde verändert"})
            continue
        if pfad.stat().st_size != zeile["groesse"]:
            befunde.append({"beleg_id": zeile["beleg_id"], "pfad": zeile["pfad"],
                            "befund": "Größe stimmt nicht"})
            continue
        rechte = pfad.stat().st_mode & 0o777
        if rechte != NUR_LESEN:
            # Die Seite verspricht 0444. Der billigste Schutz gegen eine
            # versehentliche Aenderung war sonst stillschweigend weg.
            try:
                os.chmod(pfad, NUR_LESEN)
            except OSError as fehler:
                from app.logik.backup import fehlertext

                grund = fehlertext(OSError(fehler.errno, fehler.strerror))
                befunde.append({
                    "beleg_id": zeile["beleg_id"], "pfad": zeile["pfad"],
                    "befund": f"Dateirechte abweichend ({rechte:04o}), ließen sich "
                              f"nicht auf {NUR_LESEN:04o} zurücksetzen: {grund}",
                })
            else:
                if rechte_gesetzt is not None:
                    rechte_gesetzt.append(zeile["pfad"])

    if (datenordner / ARCHIV).is_dir():
        for datei in sorted((datenordner / ARCHIV).rglob("*")):
            if datei.is_file() and str(datei.resolve()) not in bekannte:
                befunde.append({
                    "beleg_id": None, "pfad": str(datei),
                    "befund": "Datei im Archiv, auf die kein Beleg zeigt (Waise) — "
                              "vermutlich ein abgebrochenes Buchen; prüfen und erneut "
                              "über den Eingang buchen oder wegräumen",
                })
    return befunde

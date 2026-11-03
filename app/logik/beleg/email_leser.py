"""E-Mails als Beleg (.eml) -- strikt lokal, mit der Standardbibliothek.

Manche Rechnung gibt es nur als E-Mail: die Bestellbestaetigung eines
Shops, eine Quittung ohne PDF. Und manche Mail traegt die eigentliche
Rechnung im Anhang. Beides nimmt das Tool als ``.eml`` an, das Format, in
dem Apple Mail (Mail in den Finder ziehen) und Gmail im Browser ("Original
herunterladen") eine Mail unveraendert sichern.

Drei Entscheidungen
-------------------
1. **Eine echte Rechnung im Anhang ist der Beleg.** Sie wird ausgelesen wie
   ein Upload; die Mail wird als zugehoerige Datei (Rolle ``EMAIL``) mit
   archiviert. Verkaufsbedingungen, AGB, Widerrufsbelehrung und
   Datenschutzhinweise sind keine Rechnung und werden uebergangen -- sie
   bleiben aber in der archivierten Mail, die ja das Original ist.
2. **Ohne Rechnung im Anhang ist der Text der Mail der Beleg.** Gelesen wird
   der Textteil (``text/plain``), sonst das HTML -- umgewandelt in Text von
   :class:`_HtmlText`, ohne Netzzugriff, ohne Bilder, ohne Skripte. Die
   Felder liest derselbe Text-Leser wie bei PDFs; dazu kommen die Stellen,
   die nur Mails haben ("Bestellt bei", "Bestellnummer", der Kopf "Date").
   Alles daraus ist unsicher: eine Mail wird normalerweise nicht
   automatisch gebucht.
3. **Das HTML einer Mail wird nie im Browser gezeigt.** Es kann Zaehlpixel,
   nachgeladene Bilder und Skripte enthalten; schon das Anzeigen verriete
   dem Absender, wann der Beleg geoeffnet wurde. Gezeigt wird eine sichere
   Textansicht (:func:`textansicht`), das Original gibt es als Download.

Aufbewahrt wird die Mail unveraendert, mit Pruefsumme und schreibgeschuetzt
(GoBD: E-Mails, die Buchungsbelege sind, im Originalformat).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from email import policy
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from app.logik.beleg import felder as f
from app.logik.beleg import text_leser
from app.logik.beleg.ergebnis import BelegFehler, Fund, Vorschlag

QUELLE = "email"
ENDUNG = ".eml"
MIME = "message/rfc822"

VERFAHREN = ("E-Mail (.eml), gelesen mit dem Modul email der Python-Standardbibliothek; "
             "HTML ohne Netzzugriff in Text umgewandelt")

#: So weit wird nach dem Kopf gesucht. Vor From, Date und Subject stehen oft
#: viele Kilobyte Received- und DKIM-Zeilen.
KOPF_HOECHSTENS = 256 * 1024
#: Hoechstens so viel Text wird gelesen und gezeigt -- eine Mail mit zehn
#: Megabyte Text ist kein Beleg, sondern ein Anhang im falschen Teil.
TEXT_HOECHSTENS = 300_000
HTML_HOECHSTENS = 3_000_000

#: Die Meldung fuer Formate, die keine .eml sind, und wie man eine bekommt.
SO_GEHT_ES = (
    "So bekommst du eine .eml: am Mac in Apple Mail die Mail aus der Liste in den Finder "
    "ziehen, oder in Gmail im Browser über „⋮ → Original herunterladen“. Auf dem iPhone "
    "speichert Gmail keine .eml — dort geht „Teilen → Drucken → In Dateien sichern“ als PDF."
)
ANDERE_MAILFORMATE = {
    ".emlx": "eine Mail im internen Format von Apple Mail (.emlx)",
    ".msg": "eine Outlook-Nachricht (.msg)",
}

#: Woran Bedingungen statt einer Rechnung zu erkennen sind -- im Dateinamen
#: oder am Anfang des Inhalts.
_BEDINGUNGEN = re.compile(
    r"terms|agb|bedingungen|widerruf|datenschutz|privacy|conditions|cancellation|"
    r"gesch[äa]ftsbedingungen", re.I)
_RECHNUNG = re.compile(r"rechnung|invoice|receipt|quittung|beleg", re.I)
#: Das Wurzelelement einer E-Rechnung: CII (ZUGFeRD, Factur-X) oder UBL.
_E_RECHNUNG = re.compile(rb"<(?:[\w-]+:)?(?:CrossIndustryInvoice|Invoice|CreditNote)[\s>/]")

#: Endungen nach MIME-Typ, wenn ein Anhang keinen Dateinamen traegt.
_ENDUNG_NACH_MIME = {
    "application/pdf": ".pdf", "application/xml": ".xml", "text/xml": ".xml",
    "image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tiff",
    "image/heic": ".heic", "image/webp": ".webp",
}
#: Formate, die als Rechnung im Anhang taugen.
_BELEGENDUNGEN = {".pdf", ".xml", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"}


# ------------------------------------------------------------- Erkennen

_FELDNAME = re.compile(rb"[!-9;-~]{1,76}:")


def sieht_aus_wie_mail(inhalt: bytes) -> bool:
    """Ist das eine MIME-Mail mit den Kopfzeilen From, Date und Subject?

    Die Pruefung, die bei einem PDF die ersten Bytes uebernehmen ("%PDF"):
    eine Datei mit der Endung .eml, die keine Mail ist, kommt nicht in den
    Eingang. Eine einleitende mbox-Zeile ("From absender@... Datum") ist
    erlaubt -- so speichern manche Programme.

    >>> sieht_aus_wie_mail(b"From: a@b.de\\r\\nDate: Tue, 26 May 2026 10:00:00 +0200\\r\\n"
    ...                    b"Subject: Rechnung\\r\\n\\r\\nText")
    True
    >>> sieht_aus_wie_mail(b"%PDF-1.7"), sieht_aus_wie_mail(b"From: a@b.de\\r\\n\\r\\nText")
    (False, False)
    """
    kopf = inhalt[:KOPF_HOECHSTENS].removeprefix(b"\xef\xbb\xbf")
    if kopf.startswith(b"From "):
        kopf = kopf.split(b"\n", 1)[1] if b"\n" in kopf else b""
    if not _FELDNAME.match(kopf):
        return False
    try:
        nachricht = BytesHeaderParser(policy=policy.compat32).parsebytes(kopf)
    except Exception:  # noqa: BLE001 -- was sich nicht lesen laesst, ist keine Mail
        return False
    return all(nachricht.get(name) is not None for name in ("From", "Date", "Subject"))


def anderes_mailformat(endung: str) -> str | None:
    """Die Meldung fuer .emlx und .msg -- mit dem Weg zur .eml."""
    was = ANDERE_MAILFORMATE.get(endung.lower())
    if was is None:
        return None
    return f"Das ist {was} — das Tool liest E-Mails als .eml. {SO_GEHT_ES}"


# ------------------------------------------------------------- Lesen

@dataclass(frozen=True)
class Anhang:
    """Ein Anhang der Mail und was er (vermutlich) ist."""

    nr: int
    name: str
    mime: str
    groesse: int
    #: "kandidat" (koennte die Rechnung sein), "bedingungen", "bild" (Logo,
    #: eingebettet), "sonstig".
    art: str
    grund: str = ""
    daten: bytes = field(default=b"", repr=False, compare=False)

    @property
    def endung(self) -> str:
        return Path(self.name).suffix.lower()


@dataclass(frozen=True)
class Mail:
    """Kopf, Text und Anhaenge einer Mail."""

    von: str
    an: str
    betreff: str
    datum_kopf: str
    datum: date | None
    text: str
    aus_html: bool
    gekuerzt: bool
    anhaenge: tuple[Anhang, ...]

    @property
    def absendername(self) -> str:
        name, adresse = parseaddr(self.von)
        return name.strip().strip('"') or adresse.split("@")[-1]


def lies(inhalt: bytes) -> Mail:
    """Zerlegt eine Mail. Wirft :class:`BelegFehler`, wenn sie sich nicht lesen laesst."""
    if not sieht_aus_wie_mail(inhalt):
        raise BelegFehler("Die Datei ist keine lesbare E-Mail (es fehlen die Kopfzeilen "
                          f"Von, Datum und Betreff). {SO_GEHT_ES}")
    try:
        nachricht = BytesParser(policy=policy.default).parsebytes(inhalt)
    except Exception as fehler:  # noqa: BLE001 -- jede Ursache heisst: kaputt
        raise BelegFehler("Die E-Mail ist beschädigt und lässt sich nicht lesen.") from fehler
    datum_kopf = _kopf(nachricht, "Date")
    text, aus_html, gekuerzt = _text(nachricht)
    return Mail(
        von=_kopf(nachricht, "From"), an=_kopf(nachricht, "To"),
        betreff=_kopf(nachricht, "Subject"), datum_kopf=datum_kopf,
        datum=_datum(datum_kopf), text=text, aus_html=aus_html, gekuerzt=gekuerzt,
        anhaenge=tuple(_anhaenge(nachricht)),
    )


def lies_datei(pfad: Path) -> Mail:
    return lies(pfad.read_bytes())


def _kopf(nachricht: Message, name: str) -> str:
    """Eine Kopfzeile als Text, RFC 2047 dekodiert. Kaputte Kopfzeilen werfen
    bei policy.default gelegentlich -- dann der Rohtext."""
    try:
        wert = nachricht.get(name)
    except Exception:  # noqa: BLE001
        wert = None
        for schluessel, roh in nachricht.raw_items():
            if schluessel.lower() == name.lower():
                wert = roh
                break
    return " ".join(str(wert or "").split())


def _datum(kopf: str) -> date | None:
    """Der Tag aus dem Kopf "Date", in deutscher Zeit (Europe/Berlin)."""
    if not kopf:
        return None
    try:
        zeit = parsedate_to_datetime(kopf)
    except (TypeError, ValueError, IndexError):
        return None
    if zeit is None:
        return None
    if zeit.tzinfo is None:
        return zeit.date()
    from zoneinfo import ZoneInfo

    return zeit.astimezone(ZoneInfo("Europe/Berlin")).date()


def _inhalt(teil: Message) -> str:
    """Der Text eines Teils, dekodiert. Unbekannter Zeichensatz: UTF-8, sonst
    Latin-1 -- lesbar statt abgebrochen."""
    try:
        wert = teil.get_content()
        if isinstance(wert, str):
            return wert
    except Exception:  # noqa: BLE001 -- LookupError, UnicodeError, kaputtes Base64 ...
        pass
    roh = teil.get_payload(decode=True) or b""
    if not isinstance(roh, bytes):
        return ""
    try:
        return roh.decode("utf-8")
    except UnicodeDecodeError:
        return roh.decode("latin-1")


def _fliesstext(text: str, teil: Message) -> str:
    """format=flowed (RFC 3676): eine Zeile mit Leerzeichen am Ende geht in
    der naechsten weiter."""
    if (teil.get_param("format") or "").lower() != "flowed":
        return text
    loeschen = (teil.get_param("delsp") or "").lower() == "yes"
    zeilen = text.split("\n")
    ergebnis: list[str] = []
    puffer = ""
    for zeile in zeilen:
        if zeile.endswith(" ") and zeile.strip() and zeile.strip() != "--":
            puffer += zeile[:-1] if loeschen else zeile
            continue
        ergebnis.append(puffer + zeile)
        puffer = ""
    if puffer:
        ergebnis.append(puffer)
    return "\n".join(ergebnis)


def _text(nachricht: Message) -> tuple[str, bool, bool]:
    """Der Text der Mail: text/plain bevorzugt, sonst das HTML als Text.

    Ein Textteil mit kaum Inhalt ("Ihr Programm kann kein HTML") zaehlt
    nicht -- dann das HTML.
    """
    try:
        einfach = nachricht.get_body(preferencelist=("plain",))
        html = nachricht.get_body(preferencelist=("html",))
    except Exception:  # noqa: BLE001 -- kaputte Struktur: kein Text
        einfach = html = None
    text, aus_html = "", False
    if einfach is not None:
        text = _fliesstext(_inhalt(einfach).replace("\r\n", "\n"), einfach)
    if len("".join(text.split())) < 40 and html is not None:
        roh = _inhalt(html)[:HTML_HOECHSTENS]
        umgewandelt = html_zu_text(roh)
        if len(umgewandelt.strip()) > len(text.strip()):
            text, aus_html = umgewandelt, True
    text = _aufgeraeumt(text)
    gekuerzt = len(text) > TEXT_HOECHSTENS
    return text[:TEXT_HOECHSTENS], aus_html, gekuerzt


_UNSICHTBAR = dict.fromkeys(map(ord, "​‌‍⁠﻿͏­"), None)


def _aufgeraeumt(text: str) -> str:
    """Unsichtbare Zeichen weg, Leerraum je Zeile zusammengefasst, hoechstens
    eine Leerzeile am Stueck."""
    text = text.translate(_UNSICHTBAR).replace("\xa0", " ").replace("\r", "\n")
    zeilen = [" ".join(z.split()) for z in text.split("\n")]
    ergebnis: list[str] = []
    for zeile in zeilen:
        if not zeile and (not ergebnis or not ergebnis[-1]):
            continue
        ergebnis.append(zeile)
    return "\n".join(ergebnis).strip()


# ------------------------------------------------------------- HTML -> Text

class _HtmlText(HTMLParser):
    """HTML zu Text, ohne irgendetwas nachzuladen.

    Uebergangen werden Skripte, Stile, Kopf, eingebettete Objekte und alles,
    was ``display:none`` traegt (der unsichtbare Vorschautext vieler
    Newsletter). Bilder fallen weg -- auch ihr alt-Text, der in Mails meist
    "Logo" oder ein Zaehlpixel ist. Links werden zu ihrem Text; die Adresse
    bleibt draussen. Blockelemente und Tabellenzeilen werden Zeilen,
    Tabellenzellen stehen nebeneinander ("Summe 59,99 €").
    """

    UEBERGEHEN = frozenset({"script", "style", "head", "title", "noscript", "template",
                            "svg", "math", "object", "iframe", "embed", "applet", "xml"})
    BLOCK = frozenset({"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3",
                       "h4", "h5", "h6", "section", "article", "header", "footer",
                       "blockquote", "pre", "hr", "dl", "dt", "dd", "center", "address",
                       "tbody", "thead", "tfoot", "caption", "form", "main", "nav"})
    LEER = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                      "meta", "param", "source", "track", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.teile: list[str] = []
        self.stapel: list[tuple[str, bool]] = []
        self.verborgen = 0

    def _block(self) -> None:
        self.teile.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.BLOCK:
            self._block()
        elif tag in ("td", "th"):
            self.teile.append(" ")
        if tag in self.LEER:
            return
        stil = "".join((wert or "") for name, wert in attrs if name == "style").lower()
        aus = (tag in self.UEBERGEHEN or "display:none" in stil.replace(" ", "")
               or any(name == "hidden" for name, _ in attrs))
        self.stapel.append((tag, aus))
        if aus:
            self.verborgen += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.BLOCK:
            self._block()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.BLOCK:
            self._block()
        if tag in self.LEER:
            return
        # Bis zum passenden Anfang zurueck; ein Ende ohne Anfang zaehlt nicht.
        for stelle in range(len(self.stapel) - 1, -1, -1):
            if self.stapel[stelle][0] == tag:
                for _name, aus in self.stapel[stelle:]:
                    if aus:
                        self.verborgen -= 1
                del self.stapel[stelle:]
                break

    def handle_data(self, data: str) -> None:
        if not self.verborgen:
            self.teile.append(data)


def html_zu_text(html: str) -> str:
    """Das HTML einer Mail als Text -- ohne Tags, ohne Netz, ohne Bilder.

    >>> html_zu_text('<p>Summe</p><img src="https://x.example/p.gif">'
    ...              '<script>alert(1)</script><table><tr><td>Total</td>'
    ...              '<td>9,99&nbsp;&euro;</td></tr></table>')
    'Summe\\n\\nTotal 9,99 €'
    """
    leser = _HtmlText()
    try:
        leser.feed(html)
        leser.close()
    except Exception:  # noqa: BLE001 -- was bis dahin gelesen ist, gilt
        pass
    return _aufgeraeumt("".join(leser.teile))


# ------------------------------------------------------------- Anhaenge

def _anhaenge(nachricht: Message) -> list[Anhang]:
    """Alle Teile, die keine Textteile der Mail sind -- in ihrer Reihenfolge."""
    try:
        koerper = {id(t) for t in (nachricht.get_body(preferencelist=("plain",)),
                                   nachricht.get_body(preferencelist=("html",))) if t}
    except Exception:  # noqa: BLE001
        koerper = set()
    liste: list[Anhang] = []
    for teil in nachricht.walk():
        if teil.is_multipart() or id(teil) in koerper or teil is nachricht:
            continue
        typ = teil.get_content_type()
        name = _dateiname(teil)
        if not name and typ.startswith("text/") and teil.get_content_disposition() != "attachment":
            continue  # weitere Textfassungen (text/x-amp-html u. Ae.)
        try:
            daten = teil.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            daten = b""
        if not isinstance(daten, bytes):
            daten = b""
        nr = len(liste) + 1
        if not name:
            name = f"anhang-{nr}{_ENDUNG_NACH_MIME.get(typ, '')}"
        art, grund = _art(teil, name, typ, daten)
        liste.append(Anhang(nr=nr, name=name, mime=typ, groesse=len(daten), art=art,
                            grund=grund, daten=daten))
    return liste


def _dateiname(teil: Message) -> str:
    try:
        name = teil.get_filename() or ""
    except Exception:  # noqa: BLE001
        name = ""
    # Nur der Name, kein Pfad -- er kommt von aussen.
    name = Path(" ".join(name.replace("\\", "/").split()).split("/")[-1]).name
    return name if name not in (".", "..") else ""


def _art(teil: Message, name: str, typ: str, daten: bytes) -> tuple[str, str]:
    from app.logik.beleg.pipeline import inhalt_passt_zur_endung

    endung = Path(name).suffix.lower() or _ENDUNG_NACH_MIME.get(typ, "")
    if not daten:
        return "sonstig", "leer"
    if _BEDINGUNGEN.search(Path(name).stem) and not _RECHNUNG.search(Path(name).stem):
        return "bedingungen", "Bedingungen oder Hinweise, keine Rechnung"
    if endung not in _BELEGENDUNGEN:
        return "sonstig", "kein Belegformat"
    if not inhalt_passt_zur_endung(daten[:64], endung):
        return "sonstig", "Inhalt passt nicht zur Endung"
    if endung == ".xml" and not _E_RECHNUNG.search(daten[:4096]):
        # Nur ein XML, das als E-Rechnung beginnt, ist ein Kandidat. Eines,
        # das so beginnt und sich nicht lesen laesst, ist dagegen ein harter
        # Fehler (die Stufenfolge kennt keinen Rueckfall).
        return "sonstig", "XML, aber keine E-Rechnung"
    if endung in (".pdf", ".xml"):
        return "kandidat", ""
    # Bilder: eingebettet (Logo, Zaehlpixel) sind sie keine Rechnung; nur ein
    # ausdruecklich angehaengtes Bild kann ein Foto der Quittung sein.
    if teil.get_content_disposition() != "attachment" or teil.get("Content-ID"):
        return "bild", "eingebettetes Bild"
    return "kandidat", ""


def sind_bedingungen(name: str, text: str) -> bool:
    """Ist ein Anhang Bedingungen statt einer Rechnung -- am Namen oder am
    Anfang seines Textes (AGB, Verkaufsbedingungen, Widerruf, Datenschutz)?

    >>> sind_bedingungen("Sales-Terms-DE.pdf", "")
    True
    >>> sind_bedingungen("dok.pdf", "Allgemeine Geschäftsbedingungen\\n§ 1 Geltung")
    True
    >>> sind_bedingungen("dok.pdf", "Rechnung Nr. 4711\\nAGB unter www.x.de")
    False
    """
    stamm = Path(name).stem
    if _BEDINGUNGEN.search(stamm) and not _RECHNUNG.search(stamm):
        return True
    kopf = "\n".join([z for z in text.splitlines() if z.strip()][:12])
    return bool(_BEDINGUNGEN.search(kopf)) and not _RECHNUNG.search(kopf)


# ------------------------------------------------------------- Felder

_WERT = re.compile(
    r"^(?:[-−–]\s?)?(?:[€$£]\s?)?\d[\d.,  ]*(?:\s?(?:€|EUR|USD|CHF|GBP|\$|£))?$"
    r"|^\d{1,2}\.\s*[A-Za-zÄÖÜäöüé]+\.?\s+\d{4}\b.*$"
    r"|^\d{1,2}\.\d{1,2}\.\d{2,4}\b.*$"
    r"|^\d{4}-\d{2}-\d{2}\b.*$"
    r"|^(?=[A-Z0-9./#_-]*\d[A-Z0-9./#_-]*\d)[A-Z0-9][A-Z0-9./#_-]{3,30}$",
    re.I)


def kompakt(text: str) -> str:
    """Der Mailtext mit Bezeichnung und Wert auf EINER Zeile.

    In Mails steht der Wert oft unter seiner Bezeichnung ("Summe", Leerzeilen,
    "59,99 €"). Der Text-Leser sucht Betraege aber auf der Zeile ihres
    Wortes. Folgt einer Zeile ohne Ziffern direkt ein reiner Wert (Betrag,
    Datum, Nummer), werden beide zu "Summe: 59,99 €".

    >>> kompakt("Summe\\n\\n59,99 €\\nBestellt bei\\nGoogle Commerce Ltd")
    'Summe: 59,99 €\\nBestellt bei\\nGoogle Commerce Ltd'
    """
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    ergebnis: list[str] = []
    stelle = 0
    while stelle < len(zeilen):
        zeile = zeilen[stelle]
        folgt = zeilen[stelle + 1] if stelle + 1 < len(zeilen) else ""
        if (folgt and len(zeile) <= 60 and not re.search(r"\d", zeile)
                and not zeile.endswith((".", "!", "?")) and _WERT.match(folgt)):
            ergebnis.append(f"{zeile.rstrip(':').rstrip()}: {folgt}")
            stelle += 2
            continue
        ergebnis.append(zeile)
        stelle += 1
    return "\n".join(ergebnis)


_LIEFERANT_WOERTER = ("bestellt bei", "verkauft von", "verkauft durch", "verkäufer",
                      "sold by", "seller", "rechnungssteller", "händler", "anbieter")
_RECHNUNGSNUMMER_WOERTER = ("rechnungsnummer", "rechnungs-nr", "rechnungsnr", "rechnung nr",
                            "invoice number", "invoice no", "invoice #", "belegnummer",
                            "receipt number", "quittungsnummer")
_BESTELLNUMMER_WOERTER = ("gastbestellungsnummer", "bestellnummer", "bestell-nr", "bestellnr",
                          "bestellung nr", "auftragsnummer", "order number", "order no",
                          "order #", "order id", "bestellungsnummer")
_RECHNUNGSDATUM_WOERTER = ("rechnungsdatum", "invoice date", "belegdatum", "rechnung vom",
                           "date of issue")
_BESTELLDATUM_WOERTER = ("bestelldatum", "bestellt am", "bestellt", "order date", "ordered on",
                         "kaufdatum", "bestellung vom", "datum der bestellung", "order placed")


def _nach_wort(zeilen: list[str], woerter: tuple[str, ...]) -> tuple[str, str] | None:
    """Der Wert hinter einer Bezeichnung: auf derselben Zeile nach dem Wort,
    sonst die naechste Zeile. Gibt (Wert, Fundstelle) zurueck."""
    for stelle, zeile in enumerate(zeilen):
        klein = zeile.lower()
        for wort in woerter:
            if not klein.startswith(wort):
                continue
            rest = zeile[len(wort):]
            if rest[:1].isalpha():
                continue  # "Bestellt bei" ist nicht "Bestellt"
            rest = rest.strip(" :#\t")
            if rest:
                return rest, zeile
            if stelle + 1 < len(zeilen):
                return zeilen[stelle + 1], f"{zeile} {zeilen[stelle + 1]}"
    return None


def _nummer(zeilen: list[str]) -> tuple[str, str] | None:
    for woerter in (_RECHNUNGSNUMMER_WOERTER, _BESTELLNUMMER_WOERTER):
        for stelle, zeile in enumerate(zeilen):
            klein = zeile.lower()
            for wort in woerter:
                beginn = klein.find(wort)
                if beginn < 0 or (beginn and klein[beginn - 1].isalpha()):
                    continue
                rest = zeile[beginn + len(wort):].lstrip(" :.\t#")
                if not rest and stelle + 1 < len(zeilen):
                    rest = zeilen[stelle + 1]
                treffer = f._NUMMER.match(rest)
                if treffer and re.search(r"\d", treffer.group(1)):
                    return treffer.group(1).rstrip(".,;"), zeile
    return None


def _datum_in(zeile: str, heute: date | None) -> str | None:
    gefunden, _ = f.finde_datum(zeile, heute=heute, worte=("\x00",))
    return gefunden


def _belegdatum(zeilen: list[str], mail: Mail, heute: date | None) -> tuple[str, str] | None:
    """Rechnungsdatum, sonst Bestelldatum, sonst der Kopf "Date"."""
    for woerter in (_RECHNUNGSDATUM_WOERTER, _BESTELLDATUM_WOERTER):
        for zeile in zeilen:
            klein = zeile.lower()
            for wort in woerter:
                if not klein.startswith(wort) or klein[len(wort):len(wort) + 1].isalpha():
                    continue
                gefunden = _datum_in(zeile, heute)
                if gefunden:
                    return gefunden, zeile
    if mail.datum is not None and (heute is None or mail.datum <= heute):
        return mail.datum.isoformat(), f"Kopfzeile Date der E-Mail: {mail.datum_kopf}"
    return None


def _haeufigster_betrag(zeilen: list[str], format_: str) -> tuple[int, str] | None:
    """Ohne bezeichnete Summe: der haeufigste Betrag der Mail (bei Gleichstand
    der groesste) -- ohne Guthaben-, Versand- und Steuerzeilen. In einer
    Versandmitteilung steht der Preis je Artikel und je Bestellung da."""
    zaehler: dict[int, list[str]] = {}
    davor = ""
    for zeile in zeilen:
        betrag = f.betrag_in_zeile(zeile, format_)
        umfeld = f"{davor} {zeile}"
        davor = zeile
        if betrag is None or betrag <= 0:
            continue
        if f._traegt_wort(umfeld, f.GUTHABEN_WOERTER + ("versand", "steuer", "mwst", "tax",
                                                         "shipping", "rabatt")):
            continue
        zaehler.setdefault(betrag, []).append(zeile)
    if not zaehler:
        return None
    betrag, fundstellen = max(zaehler.items(), key=lambda paar: (len(paar[1]), paar[0]))
    return betrag, fundstellen[-1]


def _summe(zeilen: list[str], format_: str) -> tuple[int, str] | None:
    """Der bezeichnete Gesamtbetrag ("Summe", "Gesamtbetrag", "Total") --
    nicht die Zwischensumme und nicht der offene Restbetrag."""
    gefunden = None
    for zeile in zeilen:
        klein = zeile.lower()
        if (not f._traegt_wort(zeile, f.GESAMT_WOERTER)
                or any(w in klein for w in ("zwischensumme", "subtotal", "teilsumme"))
                or f._traegt_wort(zeile, f.OFFEN_WOERTER + f.GUTHABEN_WOERTER)):
            continue
        betrag = f.betrag_in_zeile(zeile, format_)
        if betrag is not None and betrag > 0:
            gefunden = (betrag, zeile)
    return gefunden


def felder(mail: Mail, *, heute: date | None = None,
           eigene_ust_idnr: str | None = None) -> tuple[Vorschlag, str]:
    """Die Belegfelder aus dem Text der Mail -- und der Text, aus dem sie
    stammen (fuer den Lernspeicher). Alles unsicher bis auf eine eindeutige
    USt-IdNr.: aus einer Mail wird normalerweise nicht automatisch gebucht."""
    text = kompakt(mail.text)
    allgemein = text_leser.lies_text(text, quelle=QUELLE, heute=heute,
                                     eigene_ust_idnr=eigene_ust_idnr)
    funde = {name: Fund(fund.wert, QUELLE, fund.nachweis,
                        fund.sicher and name == "lieferant_ust_idnr")
             for name, fund in allgemein.felder.items()}
    zeilen = text.splitlines()
    format_, _ = f.erkenne_format(text)

    lieferant = _nach_wort(zeilen, _LIEFERANT_WOERTER)
    if lieferant and 2 <= len(lieferant[0]) <= 80:
        funde["lieferant"] = Fund(lieferant[0], QUELLE, lieferant[1], False)
    elif mail.absendername:
        funde["lieferant"] = Fund(mail.absendername, QUELLE,
                                  f"Absender der E-Mail: {mail.von}", False)

    nummer = _nummer(zeilen)
    if nummer:
        funde["belegnummer"] = Fund(nummer[0], QUELLE, nummer[1], False)

    datum = _belegdatum(zeilen, mail, heute)
    if datum:
        funde["belegdatum"] = Fund(datum[0], QUELLE, datum[1], False)

    # Die Kreuzpruefung von Netto, Steuer und Brutto sagt bei einer Mail
    # selten etwas: dort steht meist nur ein Betrag je Bezeichnung.
    meldungen = [m for m in allgemein.meldungen
                 if not m.startswith(("Kein Betragstripel", "Zu wenige verschiedene Beträge"))]
    if "notiz" not in funde:
        # Kein Guthaben: der bezeichnete Gesamtbetrag, sonst der Fund des
        # Text-Lesers, sonst der haeufigste Betrag der Mail.
        summe = _summe(zeilen, format_)
        if summe is None and "brutto" not in funde:
            summe = _haeufigster_betrag(zeilen, format_)
            if summe is not None:
                summe = (summe[0], f"Häufigster Betrag der E-Mail: {summe[1]}")
        if summe is not None:
            funde["brutto"] = Fund(summe[0], QUELLE, summe[1], False)
        if "brutto" in funde:
            meldungen = [m for m in meldungen
                         if not m.startswith("Kein bezeichneter Gesamtbetrag")]
    # Netto und Steuer einer Mail sind selten aufgeschluesselt ("Geschätzte
    # Steuern") -- nur behalten, wenn sie zum Brutto passen.
    brutto = funde.get("brutto")
    netto, ust = funde.get("netto"), funde.get("ust")
    if not (brutto and netto and ust and netto.wert + ust.wert == brutto.wert):
        for name in ("netto", "ust", "ust_satz_promille"):
            funde.pop(name, None)
        meldungen = [m for m in meldungen
                     if not m.startswith("Die Beträge gehen rechnerisch nicht auf")]

    meldungen.insert(0, "Aus dem Text der E-Mail gelesen"
                        + (" (HTML in Text umgewandelt)" if mail.aus_html else "")
                        + " — keine Rechnung im Anhang. Alle Angaben bitte am Original prüfen.")
    if mail.gekuerzt:
        meldungen.append(f"Die E-Mail ist sehr lang; gelesen wurden die ersten "
                         f"{TEXT_HOECHSTENS:,} Zeichen.".replace(",", "."))
    return Vorschlag(QUELLE, funde, tuple(meldungen), allgemein.bedenken), text


# ------------------------------------------------------------- Anzeige

def textansicht(mail: Mail) -> dict[str, object]:
    """Was die sichere Textansicht zeigt: Kopf, Text, Anhaenge. Nur Text --
    die Vorlage setzt ihn mit Autoescape in ein <pre>, nichts davon wird HTML."""
    return {
        "von": mail.von, "an": mail.an, "betreff": mail.betreff,
        "datum": mail.datum_kopf, "text": mail.text, "aus_html": mail.aus_html,
        "gekuerzt": mail.gekuerzt,
        "anhaenge": [{"name": a.name, "groesse": a.groesse, "mime": a.mime,
                      "art": {"kandidat": "möglicherweise die Rechnung",
                              "bedingungen": "Bedingungen, keine Rechnung",
                              "bild": "eingebettetes Bild",
                              "sonstig": a.grund or "sonstiger Anhang"}[a.art]}
                     for a in mail.anhaenge],
    }

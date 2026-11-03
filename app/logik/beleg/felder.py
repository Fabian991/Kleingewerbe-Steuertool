"""Von Text zu Feldern. **Hier entscheidet sich die Qualitaet**, nicht an der
Erkennungsengine.

Eine Engine, die 99,9 % der Zeichen richtig liest, liefert bei einem
Dezimalpunkt im falschen Zeichen trotzdem einen zehnfachen Betrag -- und meldet
dafuer eine hohe Konfidenz. Deshalb stehen hier drei Bausteine, die alle ohne
Netz und ohne Kosten auskommen:

1. **Arithmetische Kreuzpruefung statt geschaetzter Konfidenz.** Alle
   Geldbetraege der Seite einsammeln und das Tripel suchen, fuer das
   ``netto + ust == brutto`` **und** ``ust == netto * satz`` fuer einen der
   ueblichen Saetze gilt. Ein Tripel, das aufgeht, ist fast sicher richtig.

   ⚠ Entscheidend: der **USt-Betrag muss selbst unter den erkannten Betraegen
   vorkommen**. Rechnet man ihn als ``brutto - netto`` aus, ist die erste
   Bedingung tautologisch wahr und die ganze Pruefung wertlos.

2. **Zahlenformat je Beleg entscheiden, nicht je Waehrung.** Rechnungen von
   AWS, Google, Figma oder Adobe kommen auf Euro, aber in englischem Format
   (``1,234.56``). Ein deutsches Muster zerlegt das still falsch zu ``1,23``.

3. **Reverse Charge erkennen.** § 13b Abs. 5 Satz 1 UStG macht jeden
   Unternehmer zum Steuerschuldner, Kleinunternehmer eingeschlossen. Bei einem
   IT-Dienstleister mit AWS- und Figma-Rechnungen ist das der Normalfall.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

#: Version der Regeln. Wandert in extraktion.regel_version und damit in die
#: Verfahrensdokumentation: ohne sie laesst sich spaeter nicht sagen, mit
#: welchen Regeln ein archivierter Beleg ausgelesen wurde.
REGEL_VERSION = "2026-10-1"

#: Die in Deutschland vorkommenden Steuersaetze, als Promille.
STEUERSAETZE = (190, 70, 0)

#: Marker fuer Reverse Charge. Klein verglichen.
RC_MARKER = (
    "reverse charge",
    "reverse-charge",
    "steuerschuldnerschaft des leistungsempfaengers",
    "steuerschuldnerschaft des leistungsempfängers",
    "vat reverse charged",
    "reverse charged",
    "art. 196",
    "article 196",
    "§ 13b",
    "13b ustg",
    "not subject to vat",
    "vat to be accounted for by the customer",
)

#: Begriffe, hinter denen der Gesamtbetrag steht. Reihenfolge = Verbindlichkeit.
BRUTTO_WOERTER = (
    "gesamtbetrag", "rechnungsbetrag", "zu zahlender betrag", "gesamtsumme",
    "bruttobetrag", "endbetrag", "total amount", "amount due", "grand total",
    "total (incl", "gesamt brutto", "summe brutto", "total due",
    # Zuletzt das blosse "Total" (englische Rechnungen: "Total £49.00 GBP") --
    # nur als ganzes Wort, nicht in "Subtotal".
    "total",
)

#: Woerter, die nur als ganzes Wort zaehlen, obwohl sie laenger als vier
#: Zeichen sind ("total" steckt in "subtotal").
_GANZE_WOERTER = frozenset({"total"})
NETTO_WOERTER = (
    "nettobetrag", "netto", "zwischensumme", "summe netto", "net amount",
    "subtotal", "total (excl", "net total",
)
UST_WOERTER = (
    "umsatzsteuer", "mehrwertsteuer", "ust", "mwst", "vat", "tax",
    "sales tax", "steuer",
)


class Zahlenformat:
    """Die beiden Schreibweisen, zwischen denen entschieden werden muss."""

    DE = "DE"   # 1.234,56
    EN = "EN"   # 1,234.56


@dataclass(frozen=True, slots=True)
class Betrag:
    """Ein im Text gefundener Geldbetrag mit seiner Fundstelle."""

    cent: int
    rohtext: str
    zeile: str
    format: str

    @property
    def euro(self) -> str:
        return f"{self.cent / 100:.2f}".replace(".", ",")


@dataclass(frozen=True, slots=True)
class Tripel:
    """Ein Satz Betraege, der arithmetisch aufgeht."""

    brutto: Betrag
    netto: Betrag
    ust: Betrag
    satz_promille: int

    @property
    def nachweis(self) -> str:
        return (f"{self.netto.euro} + {self.ust.euro} = {self.brutto.euro} "
                f"bei {self.satz_promille / 10:g} %")


# --------------------------------------------------------------- Zahlenformat

#: Eine Zahl mit Tausender- und Dezimaltrennern, ohne Vorzeichen und Waehrung.
#:
#: Die erste Alternative (gruppiert: 1.234,56) verlangt MINDESTENS eine
#: Dreiergruppe. Mit ``*`` statt ``+`` griff sie auch ohne Gruppe -- und
#: zerlegte jeden Betrag ab 1.000 ohne Tausendertrenner: aus "1250,00" wurden
#: "125" und "0,00", der Beleg ueber 1.250 Euro stand mit 1,25 Euro da.
_ZAHL = re.compile(r"\d{1,3}(?:[.,   ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")


def _normalisiere(text: str) -> str:
    """Macht Unicode-Eigenheiten der Texterkennung unschaedlich.

    Geschuetzte Leerzeichen als Tausendertrenner, typografische Minuszeichen,
    Ziffern in Sonderformen: alles Dinge, die eine Texterkennung liefert und an
    denen ein Muster sonst vorbeiläuft.
    """
    text = unicodedata.normalize("NFKC", text)
    for zeichen in ("−", "–", "—"):   # Minus, En-, Em-Dash
        text = text.replace(zeichen, "-")
    return text


def erkenne_format(text: str) -> tuple[str, str]:
    """Entscheidet das Zahlenformat des **ganzen Belegs**.

    Gezaehlt werden die eindeutigen Faelle: ``1.234,56`` kann nur deutsch sein,
    ``1,234.56`` nur englisch. Die Mehrheit entscheidet, und bei Gleichstand
    gewinnt Deutsch -- der haeufigere Fall bei deutschen Belegen, und ein
    deutscher Beleg mit falsch gelesenem Betrag faellt beim Vergleich mit dem
    Kontoauszug auf, ein englischer eher nicht.

    Gibt (Format, Begruendung) zurueck.

    >>> erkenne_format("Total: 1,234.56 USD")[0]
    'EN'
    >>> erkenne_format("Gesamtbetrag 1.234,56 EUR")[0]
    'DE'
    >>> erkenne_format("Betrag 45,00")[0]
    'DE'
    >>> erkenne_format("Rechnungsdatum 05.10.2026\\nLeistung 01.10.2026\\nBetrag 45,00 EUR")[0]
    'DE'
    """
    # Datumsangaben zuerst entfernen. "05.10.2026" enthaelt "5.10" -- das sah
    # aus wie ein englischer Dezimalpunkt und wurde als solcher gezaehlt. Ein
    # Beleg mit zwei Datumsangaben und einem Betrag "45,00" galt damit als
    # englisch, und 45,00 wurde zu 4.500,00 gelesen: hundertfach zu hoch, und
    # der Beleg sah trotzdem plausibel aus.
    text = _ohne_datumsangaben(_normalisiere(text))
    deutsch = len(re.findall(r"\d{1,3}(?:\.\d{3})+,\d{1,2}(?![\d,])", text))
    englisch = len(re.findall(r"\d{1,3}(?:,\d{3})+\.\d{1,2}(?![\d.])", text))
    if deutsch or englisch:
        if englisch > deutsch:
            return Zahlenformat.EN, f"{englisch}× 1,234.56 gegen {deutsch}× 1.234,56"
        return Zahlenformat.DE, f"{deutsch}× 1.234,56 gegen {englisch}× 1,234.56"

    # Keine Tausendertrenner im Beleg. Dann entscheidet der Dezimaltrenner:
    # 45,00 ist deutsch, 45.00 englisch. Beides vorhanden heisst Deutsch, weil
    # ein deutscher Beleg oft beides mischt (Mengen mit Punkt). Hinter den
    # zwei Nachstellen darf keine weitere Ziffer und kein weiterer Trenner
    # folgen -- "10.2026" ist kein Betrag.
    komma = len(re.findall(r"\d,\d{2}(?![\d,.])", text))
    punkt = len(re.findall(r"\d\.\d{2}(?![\d.,])", text))
    if punkt > komma:
        return Zahlenformat.EN, f"{punkt}× 45.00 gegen {komma}× 45,00"
    return Zahlenformat.DE, f"{komma}× 45,00 gegen {punkt}× 45.00"


_DATUMSANGABE = re.compile(
    r"\b\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}\b"     # 14.03.2026, 1.3.26, 14. 03. 2026
    r"|\b\d{4}-\d{2}-\d{2}\b"                   # 2026-03-14
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"              # 03/14/2026
)


def _ohne_datumsangaben(text: str) -> str:
    """Ersetzt Datumsangaben durch Leerraum, damit sie nicht als Zahl zaehlen.

    >>> _ohne_datumsangaben("Datum 05.10.2026, Betrag 45,00")
    'Datum  , Betrag 45,00'
    """
    return _DATUMSANGABE.sub(" ", text)


def nach_cent(rohtext: str, format_: str) -> int | None:
    """Ein Zahlentext als Cent, im angegebenen Format gelesen.

    >>> nach_cent("1.234,56", "DE"), nach_cent("1,234.56", "EN")
    (123456, 123456)
    >>> nach_cent("45,00", "DE"), nach_cent("45.00", "EN")
    (4500, 4500)
    >>> nach_cent("1.234", "DE"), nach_cent("1,234", "EN")
    (123400, 123400)
    """
    text = _normalisiere(rohtext).strip()
    text = re.sub(r"[   ]", "", text)
    if not text:
        return None

    if format_ == Zahlenformat.EN:
        tausender, dezimal = ",", "."
    else:
        tausender, dezimal = ".", ","

    # Ein einzelner Tausendertrenner mit drei Nachstellen ist genau das:
    # "1.234" deutsch sind 1234 und nicht 1,234. Mit einer oder zwei
    # Nachstellen waere es ein Dezimaltrenner, aber das ist im jeweiligen
    # Format nicht die Schreibweise -- dort steht dann das andere Zeichen.
    text = text.replace(tausender, "")
    text = text.replace(dezimal, ".")
    try:
        from decimal import ROUND_HALF_UP, Decimal

        return int((Decimal(text) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except Exception:  # noqa: BLE001 -- jeder Unfall heisst "kein Betrag"
        return None


def sammle_betraege(text: str, format_: str | None = None) -> list[Betrag]:
    """Alle Geldbetraege des Belegs, mit ihrer Zeile als Fundstelle.

    Gesucht wird nur dort, wo eine Waehrung oder ein Betragswort in der Naehe
    steht -- sonst waere jede Rechnungsnummer und jede Postleitzahl ein Betrag,
    und die Kreuzpruefung fände unter hundert Zahlen immer irgendein Tripel,
    das zufaellig aufgeht.
    """
    text = _normalisiere(text)
    if format_ is None:
        format_, _ = erkenne_format(text)

    gefunden: list[Betrag] = []
    for zeile in text.splitlines():
        if not _ist_betragszeile(zeile):
            continue
        for treffer in _ZAHL.finditer(zeile):
            roh = treffer.group(0)
            # Eine Zahl ohne Dezimalstellen ist auf einer Betragszeile meist
            # eine Menge oder ein Steuersatz, kein Betrag. Ausnahme: sie steht
            # direkt an einem Waehrungszeichen.
            if not re.search(r"[.,]\d{1,2}$", roh) and not _an_waehrung(zeile, treffer):
                continue
            cent = nach_cent(roh, format_)
            if cent is None or cent == 0:
                # 0,00 wird gesondert behandelt: bei Reverse Charge ist es die
                # richtige Steuer, sonst ist es Rauschen. Die Kreuzpruefung
                # bekommt es ueber kreuzpruefung(..., null_erlaubt=True).
                continue
            gefunden.append(Betrag(cent=cent, rohtext=roh, zeile=zeile.strip(),
                                   format=format_))
    return gefunden


_WAEHRUNG = re.compile(r"(?:€|EUR|USD|CHF|GBP|\$|£)", re.I)


def _ist_betragszeile(zeile: str) -> bool:
    klein = zeile.lower()
    if _WAEHRUNG.search(zeile):
        return True
    return any(wort in klein
               for wort in BRUTTO_WOERTER + NETTO_WOERTER + UST_WOERTER + ("betrag", "summe",
                                                                           "amount", "total",
                                                                           "price", "preis"))


def _an_waehrung(zeile: str, treffer: re.Match[str]) -> bool:
    """Steht direkt vor oder nach der Zahl ein Waehrungszeichen?"""
    davor = zeile[max(0, treffer.start() - 6):treffer.start()]
    danach = zeile[treffer.end():treffer.end() + 6]
    return bool(_WAEHRUNG.search(davor) or _WAEHRUNG.search(danach))


# ------------------------------------------------------- Arithmetische Pruefung

def _ust_passt(netto_cent: int, ust_cent: int, satz_promille: int) -> bool:
    """Ist ``ust`` die Steuer auf ``netto`` bei diesem Satz?

    Eine Toleranz von einem Cent ist noetig und nicht Schlamperei: viele
    Rechnungen runden je Position und summieren danach, und das weicht von der
    Steuer auf die Gesamtsumme um Cent ab.
    """
    if satz_promille == 0:
        return ust_cent == 0
    erwartet = (netto_cent * satz_promille + 500) // 1000
    return abs(erwartet - ust_cent) <= 1


def kreuzpruefung(betraege: list[Betrag]) -> tuple[Tripel | None, tuple[str, ...]]:
    """Sucht das Betragstripel, das arithmetisch aufgeht.

    Verlangt wird **beides**: ``netto + ust == brutto`` und ``ust`` ist die
    Steuer auf ``netto`` bei einem der ueblichen Saetze. Der USt-Betrag muss
    dabei selbst unter den gefundenen Betraegen stehen -- rechnet man ihn aus
    ``brutto - netto``, ist die erste Bedingung immer wahr und die Pruefung
    sagt nichts.

    Der **Steuersatz 0 wird hier nicht geprueft**, und das ist der Kern der
    Sache: bei 0 % ist ``netto + 0 == brutto`` fuer *jeden* Betrag wahr, also
    ginge jede einzelne Zahl des Belegs als Tripel durch und die Pruefung
    waere nicht nur wertlos, sondern irrefuehrend. Ein steuerfreier Beleg
    (Reverse Charge, Kleinunternehmer-Lieferant) hat arithmetisch nichts zu
    pruefen -- dort entscheidet das Betragswort, nicht die Rechnung. Darum
    kuemmert sich ``bewerte_betraege``.

    Gibt es **mehr als ein** gueltiges Tripel, wird **keines** gewaehlt: dann
    ist der Beleg uneindeutig, und eine Auswahl waere geraten.
    """
    nach_wert = {}
    for betrag in betraege:
        # Derselbe Betrag mehrfach im Text ist der Normalfall (Zwischensumme
        # und Gesamtbetrag). Fuer die Pruefung zaehlt der Wert, nicht die Zahl
        # der Fundstellen -- sonst gibt es scheinbar mehrere Tripel.
        nach_wert.setdefault(betrag.cent, betrag)
    werte = sorted(nach_wert)
    if len(werte) < 3:
        return None, ("Zu wenige verschiedene Beträge, um sie gegeneinander zu prüfen.",)

    treffer: list[Tripel] = []
    for brutto_cent in werte:
        for netto_cent in werte:
            if netto_cent >= brutto_cent:
                continue
            ust_cent = brutto_cent - netto_cent
            if ust_cent not in nach_wert:
                # DIE entscheidende Zeile: ohne sie ist die Pruefung wertlos.
                continue
            for satz in STEUERSAETZE:
                if satz and _ust_passt(netto_cent, ust_cent, satz):
                    treffer.append(Tripel(nach_wert[brutto_cent], nach_wert[netto_cent],
                                          nach_wert[ust_cent], satz))
                    break

    if not treffer:
        return None, (
            "Kein Betragstripel geht auf: netto + Umsatzsteuer ergibt keinen der erkannten "
            "Gesamtbeträge.",
        )
    if len(treffer) > 1:
        gelesen = "; ".join(t.nachweis for t in treffer[:3])
        return None, (
            f"Mehr als eine Lesart geht auf ({gelesen}). Eine Auswahl wäre geraten — "
            "bitte die Beträge am Beleg prüfen.",
        )
    return treffer[0], ()


def finde_betrag(text: str, woerter: tuple[str, ...], format_: str) -> Betrag | None:
    """Der Betrag auf einer Zeile, die eines dieser Woerter traegt.

    Genommen wird der **letzte** Betrag der Zeile: auf einer Rechnung stehen
    die Summen rechtsbuendig, und links davon steht gern noch der Steuersatz
    oder eine Menge. Von mehreren passenden Zeilen gewinnt die **letzte** --
    die Summenblock steht unten, und darueber kommen dieselben Woerter in den
    Positionszeilen vor.
    """
    text = _normalisiere(text)
    gefunden: Betrag | None = None
    for zeile in text.splitlines():
        if not _traegt_wort(zeile, woerter):
            continue
        letzter = None
        for treffer in _ZAHL.finditer(zeile):
            roh = treffer.group(0)
            if not re.search(r"[.,]\d{1,2}$", roh) and not _an_waehrung(zeile, treffer):
                continue
            cent = nach_cent(roh, format_)
            if cent is not None:
                letzter = Betrag(cent=cent, rohtext=roh, zeile=zeile.strip(), format=format_)
        if letzter is not None:
            gefunden = letzter
    return gefunden


@dataclass(frozen=True, slots=True)
class Betragsurteil:
    """Was von den Betraegen eines Belegs uebernommen werden darf."""

    brutto: Betrag | None = None
    netto: Betrag | None = None
    ust: Betrag | None = None
    satz_promille: int | None = None
    #: Nur wahr, wenn die Arithmetik aufgeht oder der Beleg steuerfrei ist.
    sicher: bool = False
    nachweis: str = ""
    meldungen: tuple[str, ...] = ()


def bewerte_betraege(
    text: str, format_: str | None = None, *, reverse_charge: bool | None = None
) -> Betragsurteil:
    """Entscheidet, welche Betraege uebernommen werden -- und ob ungeprueft.

    Die Reihenfolge ist die Reihenfolge der Verlaesslichkeit:

    1. **Die Arithmetik geht auf.** Ein Tripel aus drei im Text wirklich
       vorkommenden Betraegen, fuer das ``netto + ust == brutto`` und
       ``ust == netto × satz`` gilt. Das faengt genau die Dezimalfehler, die
       eine hohe gemeldete Konfidenz durchlaesst.
    2. **Der Beleg ist steuerfrei.** Reverse Charge oder ein
       Kleinunternehmer-Lieferant: dann gibt es keine Steuer, und arithmetisch
       ist nichts zu pruefen. Uebernommen wird der bezeichnete Gesamtbetrag,
       Netto = Brutto, Steuer 0.
    3. **Nichts geht auf.** Dann wird der bezeichnete Gesamtbetrag als
       *unsicher* vorgeschlagen und muss bestaetigt werden. Lieber ein gelb
       markierter Vorschlag als eine stille Zahl.
    """
    if format_ is None:
        format_, _ = erkenne_format(text)
    if reverse_charge is None:
        reverse_charge, _ = finde_reverse_charge(text)

    tripel, meldungen = kreuzpruefung(sammle_betraege(text, format_))
    if tripel is not None:
        return Betragsurteil(
            brutto=tripel.brutto, netto=tripel.netto, ust=tripel.ust,
            satz_promille=tripel.satz_promille, sicher=True,
            nachweis=f"Arithmetik geht auf: {tripel.nachweis}",
        )

    brutto = finde_betrag(text, BRUTTO_WOERTER, format_)
    if brutto is None:
        return Betragsurteil(meldungen=meldungen + (
            "Kein bezeichneter Gesamtbetrag gefunden — der Betrag muss eingegeben werden.",
        ))

    if reverse_charge:
        # Arithmetisch ist hier nichts zu pruefen -- es gibt keine Steuer. Was
        # sich pruefen laesst: steht die Summe ZWEIMAL da (Zwischensumme und
        # Gesamtbetrag), und sagen beide dasselbe? Dann ist der Betrag sicher.
        # Eine einzelne bezeichnete Zeile ist ein Fund, kein Nachweis -- sie
        # als sicher zu markieren hiesse, einen ungeprueften Betrag gruen zu
        # zeigen, und genau dort wird nicht mehr hingeschaut.
        netto = finde_betrag(text, NETTO_WOERTER, format_)
        bestaetigt = netto is not None and netto.cent == brutto.cent
        hinweis = (
            "Reverse Charge: nach § 13b Abs. 5 Satz 1 UStG schuldest du die "
            "Umsatzsteuer, auch als Kleinunternehmer. Der Beleg weist deshalb keine aus.",
        )
        if not bestaetigt:
            hinweis += (
                "Nur eine Summenzeile gefunden — der Gesamtbetrag ist nicht gegengeprüft, "
                "bitte am Beleg vergleichen.",
            )
        # Die Steuer ist bei Reverse Charge BEKANNT: null. Ohne diesen Wert
        # stand das Feld leer mit "bitte eintragen", und wer die Maske so
        # bestaetigte, verlor Netto und Steuer beide (netto + None != brutto).
        return Betragsurteil(
            brutto=brutto, netto=brutto,
            ust=Betrag(cent=0, rohtext="0,00", zeile=brutto.zeile, format=format_),
            satz_promille=0, sicher=bestaetigt,
            nachweis=(
                f"Steuerfrei (Reverse Charge), Gesamtbetrag aus: {brutto.zeile}"
                + (f"; bestätigt durch: {netto.zeile}" if bestaetigt else "")
            ),
            meldungen=hinweis,
        )

    return Betragsurteil(
        brutto=brutto,
        netto=finde_betrag(text, NETTO_WOERTER, format_),
        ust=finde_betrag(text, UST_WOERTER, format_),
        sicher=False,
        nachweis=f"Gesamtbetrag aus: {brutto.zeile}",
        meldungen=meldungen + (
            "Die Beträge gehen rechnerisch nicht auf — bitte am Beleg prüfen.",
        ),
    )


def _traegt_wort(zeile: str, woerter: tuple[str, ...]) -> bool:
    """Steht eines der Woerter in der Zeile?

    Kurze Woerter nur als ganzes Wort: ``ust`` darf nicht in "August" treffen
    und ``tax`` nicht in "Taxi".
    """
    klein = zeile.lower()
    for wort in woerter:
        if len(wort) <= 4 or wort in _GANZE_WOERTER:
            if re.search(rf"(?<![a-zäöüß]){re.escape(wort)}(?![a-zäöüß])", klein):
                return True
        elif wort in klein:
            return True
    return False


# ------------------------------------------------- Guthaben und Gutscheine

#: Woerter einer Zeile, auf der ein Guthaben, ein Gutschein oder eine
#: Geschenkkarte steht.
GUTHABEN_WOERTER = ("guthaben", "gutschein", "geschenkkarte", "geschenkgutschein",
                    "gift card", "giftcard", "voucher", "store credit", "promotional credit")
#: Und woran man sieht, dass damit BEZAHLT wurde (ohne Minuszeichen).
_EINGELOEST = ("bezahlt mit", "eingelöst", "verwendet", "angerechnet", "abzüglich",
               "abgezogen", "redeemed", "applied")
#: Ein Guthaben, das man ERHALTEN hat (ein Bonus fuer spaeter), mindert nichts.
_ERHALTEN = ("erhalten", "angebot", "bonus", "earned", "received", "gutgeschrieben")
#: Der Betrag, der nach dem Guthaben noch zu zahlen war.
OFFEN_WOERTER = ("ausstehender gesamtbetrag", "ausstehender betrag", "zu zahlen",
                 "zu zahlender betrag", "noch zu zahlen", "restbetrag", "zahlbetrag",
                 "amount due", "balance due", "amount paid", "bezahlter betrag")
#: Der Gesamtbetrag des Belegs, VOR dem Guthaben.
GESAMT_WOERTER = ("summe", "gesamtbetrag", "gesamtsumme", "rechnungsbetrag", "endbetrag",
                  "bestellsumme", "order total", "grand total", "total", "gesamt")
_KEIN_GESAMT = ("zwischensumme", "subtotal", "teilsumme")

_BETRAG_MIT_VORZEICHEN = re.compile(
    r"(-\s?)?(\d{1,3}(?:[.,   ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


def betrag_in_zeile(zeile: str, format_: str) -> int | None:
    """Der letzte Geldbetrag einer Zeile in Cent, MIT Vorzeichen.

    Wie bei :func:`finde_betrag` zaehlt nur eine Zahl mit Nachkommastellen
    oder an einem Waehrungszeichen. Ein Minus davor ("-45,00 €", "−45,00 €")
    macht den Betrag negativ -- so steht ein eingeloestes Guthaben da.

    >>> betrag_in_zeile("Google Store-Guthaben: −45,00 €", "DE")
    -4500
    >>> betrag_in_zeile("Menge: 1", "DE") is None
    True
    """
    zeile = _normalisiere(zeile)
    letzter = None
    for treffer in _BETRAG_MIT_VORZEICHEN.finditer(zeile):
        roh = treffer.group(2)
        start = treffer.start(2)
        if start and zeile[start - 1].isdigit():
            continue
        zahl = re.match(r"\d", roh)
        if not zahl:
            continue
        if not re.search(r"[.,]\d{1,2}$", roh):
            davor = zeile[max(0, start - 6):start]
            danach = zeile[treffer.end(2):treffer.end(2) + 6]
            if not (_WAEHRUNG.search(davor) or _WAEHRUNG.search(danach)):
                continue
        cent = nach_cent(roh, format_)
        if cent is not None:
            letzter = -cent if treffer.group(1) else cent
    return letzter


@dataclass(frozen=True, slots=True)
class Guthaben:
    """Ein Beleg, der teilweise mit Guthaben oder Gutschein bezahlt wurde.

    Gebucht wird nur ``bezahlt_cent`` (Entscheidung des Nutzers): das
    Guthaben war meist ein Bonus aus einer frueheren Bestellung. Zaehlte man
    beide Bestellungen voll, stuende der Guthabenbetrag zu viel in der EUeR.
    Damit nachvollziehbar bleibt, warum der Beleg mehr zeigt als gebucht
    ist, steht ``vermerk`` am Beleg (Notiz).
    """

    gesamt_cent: int
    guthaben_cent: int
    bezahlt_cent: int
    nachweis: str

    @property
    def vermerk(self) -> str:
        return (f"Beleg über {_euro_text(self.gesamt_cent)} €, davon "
                f"{_euro_text(self.guthaben_cent)} € mit Guthaben/Gutschein bezahlt; "
                f"gebucht ist nur der tatsächlich bezahlte Betrag von "
                f"{_euro_text(self.bezahlt_cent)} €.")

    @property
    def hinweis(self) -> str:
        return (f"Teilweise mit Guthaben oder Gutschein bezahlt: der Beleg zeigt "
                f"{_euro_text(self.gesamt_cent)} €, tatsächlich bezahlt wurden "
                f"{_euro_text(self.bezahlt_cent)} € ({_euro_text(self.guthaben_cent)} € "
                "Guthaben). Vorgeschlagen ist der bezahlte Betrag: ein Guthaben ist meist "
                "ein Bonus aus einer früheren Bestellung, und zählte man beide Bestellungen "
                "voll, stünde es zu viel in der EÜR. Der Vermerk steht in der Notiz.")


def _euro_text(cent: int) -> str:
    """1.234,56 -- ohne Abhaengigkeit von app.logik.rundung."""
    vorzeichen = "-" if cent < 0 else ""
    ganz, rest = divmod(abs(cent), 100)
    return f"{vorzeichen}{ganz:,}".replace(",", ".") + f",{rest:02d}"


def finde_guthaben(text: str, format_: str | None = None) -> Guthaben | None:
    r"""Wurde der Beleg teilweise mit Guthaben, Gutschein oder Geschenkkarte bezahlt?

    Gezaehlt wird eine Guthabenzeile nur, wenn sie das Guthaben ABZIEHT --
    mit Minus vor dem Betrag oder mit "bezahlt mit", "eingelöst" o. Ae. Ein
    erhaltenes Guthaben ("Store-Guthaben erhalten 45,00 €", ein Bonus fuer
    spaetere Kaeufe) mindert diesen Beleg nicht. Bezahlt ist der Betrag auf
    der Zeile "Ausstehender Gesamtbetrag"/"Zu zahlen", sonst der Gesamtbetrag
    abzueglich Guthaben.

    >>> g = finde_guthaben("Summe: 59,99 €\nGoogle Store-Guthaben: -45,00 €\n"
    ...                    "Ausstehender Gesamtbetrag: 14,99 €")
    >>> g.gesamt_cent, g.guthaben_cent, g.bezahlt_cent
    (5999, 4500, 1499)
    >>> finde_guthaben("Summe: 99,99 €\nStore-Guthaben erhalten: 45,00 €") is None
    True
    """
    if format_ is None:
        format_, _ = erkenne_format(text)
    guthaben = 0
    offen: int | None = None
    gesamt: int | None = None
    gesamtzeile = ""
    zeilen: list[str] = []
    for zeile in text.splitlines():
        klein = _normalisiere(zeile).lower()
        betrag = betrag_in_zeile(zeile, format_)
        if betrag is None:
            continue
        if _traegt_wort(zeile, GUTHABEN_WOERTER):
            if any(w in klein for w in _ERHALTEN):
                continue
            if betrag < 0 or any(w in klein for w in _EINGELOEST):
                guthaben += abs(betrag)
                zeilen.append(zeile.strip())
            continue
        if _traegt_wort(zeile, OFFEN_WOERTER):
            offen = abs(betrag)
            zeilen.append(zeile.strip())
            continue
        if _traegt_wort(zeile, GESAMT_WOERTER) and not any(w in klein for w in _KEIN_GESAMT):
            gesamt, gesamtzeile = abs(betrag), zeile.strip()
    if not guthaben:
        return None
    if offen is None and gesamt is None:
        return None
    bezahlt = offen if offen is not None else max(gesamt - guthaben, 0)
    if gesamt is None or gesamt < bezahlt:
        gesamt = bezahlt + guthaben
    else:
        zeilen.insert(0, gesamtzeile)
    return Guthaben(gesamt_cent=gesamt, guthaben_cent=guthaben, bezahlt_cent=bezahlt,
                    nachweis="Guthaben abgezogen: " + "; ".join(zeilen))


# --------------------------------------------------------------- Einzelfelder

def finde_reverse_charge(text: str) -> tuple[bool, str]:
    """Reverse Charge am Text erkennen. Gibt (Treffer, Fundstelle) zurueck."""
    klein = _normalisiere(text).lower()
    for marker in RC_MARKER:
        stelle = klein.find(marker)
        if stelle >= 0:
            anfang = max(0, stelle - 30)
            return True, "…" + _normalisiere(text)[anfang:stelle + len(marker) + 30].strip() + "…"
    return False, ""


_UST_IDNR = re.compile(
    r"\b("
    r"ATU\d{8}|BE0?\d{9,10}|BG\d{9,10}|CY\d{8}[A-Z]|CZ\d{8,10}|"
    r"DE\d{9}|DK\d{8}|EE\d{9}|EL\d{9}|ES[A-Z0-9]\d{7}[A-Z0-9]|FI\d{8}|"
    r"FR[A-Z0-9]{2}\d{9}|HR\d{11}|HU\d{8}|IE\d{7}[A-Z]{1,2}|IT\d{11}|"
    r"LT(?:\d{9}|\d{12})|LU\d{8}|LV\d{11}|MT\d{8}|NL\d{9}B\d{2}|"
    r"PL\d{10}|PT\d{9}|RO\d{2,10}|SE\d{12}|SI\d{8}|SK\d{10}|XI\d{9}"
    r")\b"
)


def finde_ust_idnr(text: str, *, ausser: str | None = None) -> str | None:
    """Die Umsatzsteuer-Identifikationsnummer eines Lieferanten.

    Auf einem Beleg stehen oft zwei: die eigene und die des Lieferanten.
    ``ausser`` nimmt die eigene heraus; wer sie nicht uebergibt, bekommt
    moeglicherweise seine eigene zurueck.

    Zwischenraeume und Punkte werden **je Treffer** entfernt und nicht im
    ganzen Text: schluckt man alle Zeilenumbrueche, haengt die Nummer am
    naechsten Wort und die Wortgrenze am Ende des Musters greift nicht mehr --
    ein Fehler, der nichts findet und dabei aussieht wie "nichts da".

    >>> finde_ust_idnr("USt-IdNr.: DE 812 871 812\\nRechnung")
    'DE812871812'
    >>> finde_ust_idnr("VAT: IE3623748GH")
    'IE3623748GH'
    """
    alle = alle_ust_idnrn(text, ausser=ausser)
    return alle[0] if alle else None


def alle_ust_idnrn(text: str, *, ausser: str | None = None) -> list[str]:
    """Alle fremden USt-IdNrn. des Belegs, in Lesereihenfolge, ohne Doppelte.

    Eine einzige ist ein belastbarer Fund; zwei verschiedene heissen, dass
    der Beleg mehr als eine Partei nennt (Lieferant, Zahlungsdienstleister,
    ein Vermittler) -- dann ist die erste nur die wahrscheinlichste.

    >>> alle_ust_idnrn("Verkäufer DE812871812\\nZahlungsabwicklung LU26888617")
    ['DE812871812', 'LU26888617']
    """
    eigene = (ausser or "").replace(" ", "").upper() or None
    gefunden: list[str] = []
    for zeile in _normalisiere(text).upper().splitlines():
        # Zwei Durchgaenge, weil keiner allein genuegt:
        #
        # Mit entfernten Trennzeichen findet man "DE 812 871 812" -- ohne sie
        # nicht, denn die Nummer ist dort dreigeteilt.
        # Ohne entfernte Trennzeichen findet man "P.IVA IT12345678901" -- mit
        # ihnen nicht, denn aus "P.IVA IT..." wird "PIVAIT...", und dann steht
        # vor dem "IT" ein Buchstabe und die Wortgrenze greift nicht.
        #
        # Nur innerhalb der Zeile, nie ueber Zeilen hinweg: sonst haengt die
        # Nummer am naechsten Wort und die Wortgrenze am Ende faellt weg.
        for fassung in (re.sub(r"[\s.  /-]", "", zeile), zeile):
            for treffer in _UST_IDNR.finditer(fassung):
                nummer = treffer.group(1)
                if nummer != eigene and nummer not in gefunden:
                    gefunden.append(nummer)
    return gefunden


_DATUM_MUSTER = (
    # Reihenfolge = Verbindlichkeit. ISO zuerst, weil eindeutig.
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("j", "m", "t")),
    (re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\b"), ("t", "m", "j")),
    (re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{2})\b"), ("t", "m", "jj")),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), ("t", "m", "j")),
)

_MONATE = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2,
    "maerz": 3, "märz": 3, "mrz": 3, "mar": 3, "march": 3, "april": 4, "apr": 4,
    "mai": 5, "may": 5, "juni": 6, "jun": 6, "june": 6, "juli": 7, "jul": 7,
    "july": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "oct": 10, "october": 10, "november": 11, "nov": 11,
    "dezember": 12, "dez": 12, "dec": 12, "december": 12,
}

_DATUM_TEXT = re.compile(
    r"\b(\d{1,2})\.?\s+([A-Za-zÄÖÜäöüé]+)\s+(\d{4})\b"
)


def _tag(teile: dict[str, str]) -> date | None:
    try:
        jahr = int(teile["j"]) if "j" in teile else 2000 + int(teile["jj"])
        return date(jahr, int(teile["m"]), int(teile["t"]))
    except (ValueError, KeyError):
        return None


def finde_datum(
    text: str, *, heute: date | None = None, worte: tuple[str, ...] = ()
) -> tuple[str | None, str]:
    """Das Belegdatum. Gibt (ISO-Datum, Fundstelle) zurueck.

    Bevorzugt werden Datumsangaben in Zeilen mit einem Wort wie
    "Rechnungsdatum" oder "Invoice date". Danach das **frueheste plausible**
    Datum: auf einem Beleg steht neben dem Belegdatum oft ein Faelligkeits-
    oder Leistungsdatum, und das Belegdatum ist das aeltere.

    Ein Datum in der Zukunft oder mehr als zehn Jahre in der Vergangenheit
    wird verworfen -- das ist dann eine Telefonnummer oder eine Kundennummer.

    >>> finde_datum("Rechnungsdatum: 14.03.2026", heute=date(2026, 10, 12))[0]
    '2026-03-14'
    >>> finde_datum("Invoice Date March 14, 2026", heute=date(2026, 10, 12))[0]
    '2026-03-14'
    """
    stichtag = heute or date.today()
    frueheste = date(stichtag.year - 10, 1, 1)
    text = _normalisiere(text)
    zeilen = text.splitlines()
    # Reihenfolge = Verbindlichkeit. Das unspezifische "datum" steht hinten:
    # es trifft auch "Lieferdatum" und "Zahlungsdatum", und dann gewaenne das
    # falsche Datum, weil es das aeltere ist.
    worte = worte or ("rechnungsdatum", "belegdatum", "invoice date", "bill date",
                      "date of issue", "rechnung vom", "issued", "datum")

    # Erst das spezifischste Wort, das im Beleg ueberhaupt vorkommt. Nur die
    # Zeilen MIT diesem Wort werden gelesen -- nicht alle bezeichneten Zeilen
    # zusammen.
    for wort in worte:
        passende = [z for z in zeilen if wort in z.lower()]
        if not passende:
            continue
        kandidaten = _datumskandidaten(passende, frueheste, stichtag)
        if kandidaten:
            tag, zeile = min(kandidaten, key=lambda paar: paar[0])
            return tag.isoformat(), zeile

    # Kein bezeichnetes Datum: das frueheste plausible nehmen. Auf einem Beleg
    # steht neben dem Belegdatum oft eine Faelligkeit, und die ist spaeter.
    kandidaten = _datumskandidaten(zeilen, frueheste, stichtag)
    if not kandidaten:
        return None, ""
    tag, zeile = min(kandidaten, key=lambda paar: paar[0])
    return tag.isoformat(), zeile


def _datumskandidaten(
    zeilen: list[str], frueheste: date, stichtag: date
) -> list[tuple[date, str]]:
    gefunden: list[tuple[date, str]] = []
    for zeile in zeilen:
        for muster, reihenfolge in _DATUM_MUSTER:
            for treffer in muster.finditer(zeile):
                tag = _tag(dict(zip(reihenfolge, treffer.groups(), strict=True)))
                if tag and frueheste <= tag <= stichtag:
                    gefunden.append((tag, zeile.strip()))
        # "3. Maerz 2026" und "3 March 2026"
        for treffer in _DATUM_TEXT.finditer(zeile):
            monat = _MONATE.get(treffer.group(2).lower().rstrip("."))
            tag = _als_tag(treffer.group(3), monat, treffer.group(1))
            if tag and frueheste <= tag <= stichtag:
                gefunden.append((tag, zeile.strip()))
        # "March 3, 2026" -- Monat vor Tag, die englische Schreibweise
        for treffer in re.finditer(r"\b([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\b", zeile):
            monat = _MONATE.get(treffer.group(1).lower())
            tag = _als_tag(treffer.group(3), monat, treffer.group(2))
            if tag and frueheste <= tag <= stichtag:
                gefunden.append((tag, zeile.strip()))
    return gefunden


def _als_tag(jahr: str, monat: int | None, tag: str) -> date | None:
    if monat is None:
        return None
    try:
        return date(int(jahr), monat, int(tag))
    except ValueError:
        return None


_NUMMER_WOERTER = (
    "rechnungsnummer", "rechnungs-nr", "rechnungsnr", "belegnummer", "beleg-nr",
    "invoice number", "invoice no", "invoice-no", "invoice #", "rechnung nr",
    "document number", "receipt number", "quittungsnummer",
)
_NUMMER = re.compile(r"([A-Z0-9][A-Z0-9/_.-]{3,30})", re.I)


def finde_belegnummer(text: str) -> tuple[str | None, str]:
    """Die Nummer, die der Lieferant dem Beleg gegeben hat.

    Gesucht wird nur in Zeilen mit einem Nummernwort. Ohne diese Einschraenkung
    ist auf jedem Beleg irgendeine Zeichenkette "die Nummer" -- eine IBAN, eine
    Kundennummer, eine Artikelnummer.

    >>> finde_belegnummer("Rechnungsnummer: RE-2026-0815")[0]
    'RE-2026-0815'
    """
    text = _normalisiere(text)
    for zeile in text.splitlines():
        klein = zeile.lower()
        for wort in _NUMMER_WOERTER:
            stelle = klein.find(wort)
            if stelle < 0:
                continue
            rest = zeile[stelle + len(wort):].lstrip(" :.\t#")
            treffer = _NUMMER.match(rest)
            if treffer:
                return treffer.group(1).rstrip(".,;"), zeile.strip()
    return None, ""

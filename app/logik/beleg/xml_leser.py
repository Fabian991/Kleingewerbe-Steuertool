"""Stufe 1: das eingebettete XML einer E-Rechnung.

**Ist XML vorhanden, ist es massgebend.** Eine Auslesung des Bildteils per
Texterkennung ist dann unzulaessig -- das Bild ist bei einer E-Rechnung nur die
menschenlesbare Beigabe, der Datensatz ist das Dokument. Scheitert das Parsen,
folgt ein harter Fehler mit Journaleintrag und **niemals** ein Rueckfall auf
Bilderkennung.

Zwei Syntaxen, ein Leser
------------------------
ZUGFeRD und Factur-X benutzen **CII** (Wurzelelement ``CrossIndustryInvoice``),
XRechnung gibt es als CII *und* als **UBL** (Wurzelelement ``Invoice`` im
UBL-Namensraum). Entschieden wird am Wurzelelement, nicht am Dateinamen.

Bewusst ohne ``drafthorse``
---------------------------
Die Bibliothek parst **keine PDFs** -- das XML muss ohnehin mit ``pypdf`` aus
den Anhaengen geholt werden -- und deckt nur CII ab, fuer UBL braucht es also
trotzdem einen eigenen Leser. Ihr Objektmodell ist zudem fuer das *Schreiben*
von Rechnungen gebaut und entsprechend streng; beim *Lesen* fremder Rechnungen
will man das Gegenteil: nachsichtig sein und genau die zehn Felder
herausziehen, die man braucht. Zwei Syntaxen mit ``xml.etree`` zu lesen ist
kuerzer als eine Abhaengigkeit zu baendigen, die nur die Haelfte abdeckt.

Namensraeume werden ignoriert und nur die lokalen Elementnamen verglichen. Das
ist hier richtig und nicht faul: die Profile von ZUGFeRD 1.0 bis 2.3 benutzen
unterschiedliche Namensraum-URIs fuer dieselben Elemente, und ein Leser, der
auf der URI besteht, bricht am naechsten Profilwechsel.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.logik.beleg.ergebnis import BelegFehler, Fund, Vorschlag, pdf_fehlertext
from app.logik.rundung import euro

QUELLE = "xml"


class Gutschrift(BelegFehler):
    """Das XML ist eine Gutschrift (CII TypeCode 381/261, UBL CreditNote).

    Ein eigener Fehlertyp, damit die Pruefmaske sie als das zeigen kann, was
    sie ist -- und nicht als leeres Buchungsformular, in das jemand die
    Gutschrift dann als Ausgabe eintraegt. Eine Gutschrift kehrt das
    Vorzeichen um: sie mindert die Ausgabe des urspruenglichen Belegs.
    """

#: Dateinamen, unter denen die Norm das XML im PDF ablegt. Gross- und
#: Kleinschreibung wechselt je Erzeuger, deshalb wird klein verglichen.
XML_NAMEN = (
    "factur-x.xml",
    "zugferd-invoice.xml",
    "xrechnung.xml",
    "order-x.xml",
    "cii.xml",
)

#: Steuerkategorien, die Reverse Charge bedeuten (UNTDID 5305).
#: AE = VAT Reverse Charge. Nicht dabei: E (exempt), Z (zero rated), G/K
#: (Ausfuhr und innergemeinschaftliche Lieferung) -- die sind steuerfrei, aber
#: machen den Empfaenger nicht zum Steuerschuldner.
REVERSE_CHARGE_CODES = {"AE"}

#: Die Steuerkategorien (UNTDID 5305) in Worten -- fuer Gruende und Nachweis.
STEUERKATEGORIEN = {
    "S": "Normalsatz", "AA": "ermäßigter Satz", "Z": "Nullsatz", "E": "steuerbefreit",
    "AE": "Reverse Charge", "K": "innergemeinschaftliche Lieferung", "G": "Ausfuhr",
    "O": "nicht steuerbar", "L": "Kanarische Inseln", "M": "Ceuta und Melilla",
}


def steuerkategorien_text(codes: object) -> str:
    """"E" -> "Steuerkategorie E (steuerbefreit)"; mehrere mit Komma.

    >>> steuerkategorien_text("E"), steuerkategorien_text("O, S")
    ('Steuerkategorie E (steuerbefreit)', 'Steuerkategorien O (nicht steuerbar), S (Normalsatz)')
    """
    teile = [c.strip() for c in str(codes or "").split(",") if c.strip()]
    if not teile:
        return "keine Steuerkategorie"
    worte = ", ".join(f"{c} ({STEUERKATEGORIEN[c]})" if c in STEUERKATEGORIEN else c
                      for c in teile)
    return f"Steuerkategorie{'n' if len(teile) > 1 else ''} {worte}"


def finde_xml(pfad: Path) -> tuple[str, bytes] | None:
    """Holt das eingebettete XML aus einem PDF. ``None``, wenn keines drin ist.

    Durchsucht die Anhaenge des PDF, nicht den Text. Ein PDF ohne Anhang ist
    keine E-Rechnung, sondern eine sonstige Rechnung in elektronischem Format
    -- fuer die ist Stufe 2 zustaendig.
    """
    from pypdf import PdfReader

    try:
        leser = PdfReader(str(pfad))
        anhaenge = leser.attachments
    except Exception as fehler:  # noqa: BLE001 -- jede Ursache fuehrt zum selben Ergebnis
        raise BelegFehler(pdf_fehlertext(fehler)) from fehler

    if not anhaenge:
        return None

    # Erst die genormten Namen, dann irgendein XML. Die Reihenfolge zaehlt:
    # ein PDF kann neben factur-x.xml noch Beiwerk tragen.
    klein = {name.lower(): name for name in anhaenge}
    for kandidat in XML_NAMEN:
        if kandidat in klein:
            inhalte = anhaenge[klein[kandidat]]
            if inhalte:
                return klein[kandidat], inhalte[0]
    for name, inhalte in anhaenge.items():
        if name.lower().endswith(".xml") and inhalte:
            return name, inhalte[0]
    return None


def _wurzel(daten: bytes) -> ET.Element:
    try:
        return ET.fromstring(daten)
    except ET.ParseError as fehler:
        zeile, spalte = getattr(fehler, "position", (None, None))
        wo = f" (Zeile {zeile}, Spalte {spalte})" if zeile else ""
        raise BelegFehler(
            f"Das eingebettete XML ist nicht lesbar: es ist nicht vollständig oder nicht "
            f"wohlgeformt{wo}. Bei einer E-Rechnung ist das XML maßgebend; eine Auslesung "
            "des Bildteils ist unzulässig. Der Beleg muss manuell erfasst werden."
        ) from fehler


def _lokal(element: ET.Element) -> str:
    """Der Elementname ohne Namensraum."""
    return element.tag.rsplit("}", 1)[-1]


def _kinder(element: ET.Element | None, name: str) -> list[ET.Element]:
    """Alle direkten Kinder mit diesem lokalen Namen.

    Nimmt ``None`` an, damit der Aufrufer nicht pruefen muss -- und nimmt es
    ausdruecklich NICHT als ``if element``: ein Element ohne Kinder ist in
    ElementTree falsch im booleschen Sinn, und ein gueltiges, leeres Element
    waere damit stillschweigend uebersprungen.
    """
    if element is None:
        return []
    return [kind for kind in element if _lokal(kind) == name]


def _pfad(element: ET.Element | None, *namen: str) -> ET.Element | None:
    """Folgt einem Pfad lokaler Elementnamen. ``None``, sobald es abbricht."""
    stelle = element
    for name in namen:
        if stelle is None:
            return None
        treffer = _kinder(stelle, name)
        stelle = treffer[0] if treffer else None
    return stelle


def _text(element: ET.Element | None, *namen: str) -> str | None:
    ziel = _pfad(element, *namen) if namen else element
    if ziel is None or ziel.text is None:
        return None
    wert = ziel.text.strip()
    return wert or None


def _cent(text: str | None) -> int | None:
    """Ein Geldbetrag aus dem XML als Cent.

    Im XML steht der Betrag immer mit Punkt als Dezimaltrenner (EN 16931), ein
    Komma kommt nicht vor. Gerechnet wird mit ``Decimal``: ``float`` macht aus
    19,99 EUR je nach Rechenweg 1998 oder 1999 Cent.
    """
    if not text:
        return None
    try:
        betrag = Decimal(text.replace(" ", ""))
    except InvalidOperation:
        return None
    return int((betrag * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def _promille(text: str | None) -> int | None:
    """Ein Steuersatz als Promille: 19 Prozent sind 190."""
    if text is None:
        return None
    try:
        return int((Decimal(text.replace(" ", "")) * 10).to_integral_value())
    except InvalidOperation:
        return None


def _datum(text: str | None) -> str | None:
    """ISO-Datum aus den drei Formen, die in E-Rechnungen vorkommen.

    CII schreibt ``20260314`` (Format 102), UBL ``2026-03-14``. Manche
    Erzeuger liefern einen vollen Zeitstempel.
    """
    if not text:
        return None
    kompakt = text.strip()
    if re.fullmatch(r"\d{8}", kompakt):
        return f"{kompakt[:4]}-{kompakt[4:6]}-{kompakt[6:]}"
    treffer = re.match(r"(\d{4})-(\d{2})-(\d{2})", kompakt)
    return "-".join(treffer.groups()) if treffer else None


def _fund(wert: object, nachweis: str) -> Fund | None:
    """Ein Fund, falls es etwas zu finden gab.

    Aus dem XML ist jeder Wert ``sicher``: er ist der Datensatz selbst und
    nicht aus einem Text geraten.
    """
    if wert in (None, ""):
        return None
    return Fund(wert=wert, quelle=QUELLE, nachweis=nachweis, sicher=True)


def _sammle(paare: dict[str, tuple[object, str]]) -> dict[str, Fund]:
    funde = {}
    for feld, (wert, nachweis) in paare.items():
        fund = _fund(wert, nachweis)
        if fund is not None:
            funde[feld] = fund
    return funde


def _steuerkategorie(felder: dict[str, Fund], kategorien: set[str], nachweis: str) -> None:
    """Die Steuerkategorien des Dokuments als Fund "steuerkategorie" ("E", "O, S").

    Kein Feld der Pruefmaske, sondern die Aussage des Dokuments, auf die sich
    das automatische Buchen stuetzt: weist das XML keine Steuerschuldnerschaft
    des Empfaengers (AE) aus, ist es KEIN Reverse-Charge-Beleg -- auch wenn
    beim Lieferanten Reverse Charge als Vorgabe gemerkt ist.
    """
    codes = sorted(k for k in kategorien if k)
    if codes:
        felder["steuerkategorie"] = Fund(", ".join(codes), QUELLE, nachweis, True)


# ------------------------------------------------------------------ CII

#: UN/CEFACT 1001: Dokumentarten, die KEINE Rechnung sind, sondern ihr
#: Gegenteil. 381 Gutschrift, 261 selbst ausgestellte Gutschrift. Eine
#: ZUGFeRD-Gutschrift traegt positive Betraege und saehe gelesen aus wie eine
#: Rechnung -- gebucht wuerde eine Ausgabe, wo Geld zurueckkam.
GUTSCHRIFT_CODES = {"381", "261"}


def _lies_cii(wurzel: ET.Element) -> Vorschlag:
    kopf = _pfad(wurzel, "ExchangedDocument")
    typ = (_text(kopf, "TypeCode") or "").strip()
    if typ in GUTSCHRIFT_CODES:
        raise Gutschrift(
            f"Das XML ist eine Gutschrift (TypeCode {typ}) und keine Rechnung. "
            "Eine Gutschrift kehrt das Vorzeichen um: sie mindert die Ausgabe des "
            "ursprünglichen Belegs und wird nicht als Ausgabe gebucht."
        )
    vorgang = _pfad(wurzel, "SupplyChainTradeTransaction")
    absprache = _pfad(vorgang, "ApplicableHeaderTradeAgreement")
    abrechnung = _pfad(vorgang, "ApplicableHeaderTradeSettlement")
    verkaeufer = _pfad(absprache, "SellerTradeParty")
    summe = _pfad(abrechnung, "SpecifiedTradeSettlementHeaderMonetarySummation")

    # Der Name steht je Profil als Name oder als SpecifiedLegalOrganization.
    name = _text(verkaeufer, "Name") or _text(
        verkaeufer, "SpecifiedLegalOrganization", "TradingBusinessName"
    )

    # Umsatzsteuer-Identifikationsnummer: die Registrierung mit schemeID "VA".
    # Ohne die Pruefung auf schemeID bekaeme man die Steuernummer (schemeID
    # "FC") -- eine andere Nummer, die nur zufaellig an derselben Stelle steht.
    ust_idnr = None
    for registrierung in _kinder(verkaeufer, "SpecifiedTaxRegistration"):
        kennung = _pfad(registrierung, "ID")
        if kennung is not None and kennung.get("schemeID") == "VA":
            ust_idnr = (kennung.text or "").strip() or None
            break

    steuern = _kinder(abrechnung, "ApplicableTradeTax")
    kategorien = {(_text(s, "CategoryCode") or "").upper() for s in steuern}
    reverse_charge = bool(kategorien & REVERSE_CHARGE_CODES)

    # Mehrere Steuersaetze auf einem Beleg: dann gibt es keinen EINEN Satz, und
    # der Nutzer muss hinschauen. Lieber kein Wert als ein falscher.
    saetze = {_promille(_text(s, "RateApplicablePercent")) for s in steuern}
    saetze.discard(None)
    satz = next(iter(saetze)) if len(saetze) == 1 else None

    brutto = _cent(_text(summe, "GrandTotalAmount"))
    netto = _cent(_text(summe, "TaxBasisTotalAmount"))
    ust = _cent(_text(summe, "TaxTotalAmount"))

    # Das Zahlungsdatum ist nicht das Belegdatum. Fuer die EUeR zaehlt nach
    # § 11 EStG der Abfluss, also der Tag der Zahlung -- im XML steht hier
    # bestenfalls das Faelligkeitsdatum, und das ist eine Ankuendigung, kein
    # Abfluss. Es wird deshalb NICHT als Zahlungsdatum uebernommen.
    felder = _sammle({
        "lieferant": (name, "CII SellerTradeParty/Name"),
        "lieferant_ust_idnr": (ust_idnr, "CII SpecifiedTaxRegistration[schemeID=VA]"),
        "belegnummer": (_text(kopf, "ID"), "CII ExchangedDocument/ID"),
        "belegdatum": (_datum(_text(kopf, "IssueDateTime", "DateTimeString")),
                       "CII ExchangedDocument/IssueDateTime"),
        "waehrung": (_text(abrechnung, "InvoiceCurrencyCode"), "CII InvoiceCurrencyCode"),
        "brutto": (brutto, "CII GrandTotalAmount"),
        "netto": (netto, "CII TaxBasisTotalAmount"),
        "ust": (ust, "CII TaxTotalAmount"),
        "ust_satz_promille": (satz, "CII RateApplicablePercent"),
    })
    if reverse_charge:
        felder["reverse_charge"] = Fund(
            True, QUELLE, "CII CategoryCode = AE (VAT Reverse Charge)", True
        )
    _steuerkategorie(felder, kategorien, "CII ApplicableTradeTax/CategoryCode")

    zahlbar = _cent(_text(summe, "DuePayableAmount"))
    return Vorschlag(QUELLE, felder, _nachpruefen(brutto, netto, ust, saetze, reverse_charge),
                     _bedenken(brutto, netto, ust, saetze, typ=typ, zahlbar=zahlbar))


# ------------------------------------------------------------------ UBL

def _lies_ubl(wurzel: ET.Element) -> Vorschlag:
    lieferpartei = _pfad(wurzel, "AccountingSupplierParty", "Party")
    summen = _pfad(wurzel, "LegalMonetaryTotal")

    # UBL fuehrt den Namen zweifach: PartyName ist der Handelsname,
    # PartyLegalEntity/RegistrationName der eingetragene. Fuer einen Beleg ist
    # der eingetragene der belastbarere, deshalb zuerst.
    name = (_text(lieferpartei, "PartyLegalEntity", "RegistrationName")
            or _text(lieferpartei, "PartyName", "Name"))

    ust_idnr = None
    for schema in _kinder(lieferpartei, "PartyTaxScheme"):
        kennung = _text(schema, "CompanyID")
        if kennung:
            ust_idnr = kennung
            break

    steuersummen = _kinder(wurzel, "TaxTotal")
    unterposten = [u for s in steuersummen for u in _kinder(s, "TaxSubtotal")]
    kategorien = {
        (_text(u, "TaxCategory", "ID") or "").upper() for u in unterposten
    }
    reverse_charge = bool(kategorien & REVERSE_CHARGE_CODES)

    saetze = {_promille(_text(u, "TaxCategory", "Percent")) for u in unterposten}
    saetze.discard(None)
    satz = next(iter(saetze)) if len(saetze) == 1 else None

    # TaxInclusiveAmount ist der Bruttobetrag, PayableAmount der zu zahlende --
    # sie weichen ab, wenn eine Anzahlung verrechnet ist. Gebucht wird als
    # Betriebsausgabe, was die Leistung gekostet hat, also das Brutto.
    brutto = _cent(_text(summen, "TaxInclusiveAmount"))
    netto = _cent(_text(summen, "TaxExclusiveAmount"))
    ust = _cent(_text(steuersummen[0], "TaxAmount")) if steuersummen else None

    felder = _sammle({
        "lieferant": (name, "UBL AccountingSupplierParty/Party"),
        "lieferant_ust_idnr": (ust_idnr, "UBL PartyTaxScheme/CompanyID"),
        "belegnummer": (_text(wurzel, "ID"), "UBL Invoice/ID"),
        "belegdatum": (_datum(_text(wurzel, "IssueDate")), "UBL IssueDate"),
        "waehrung": (_text(wurzel, "DocumentCurrencyCode"), "UBL DocumentCurrencyCode"),
        "brutto": (brutto, "UBL TaxInclusiveAmount"),
        "netto": (netto, "UBL TaxExclusiveAmount"),
        "ust": (ust, "UBL TaxTotal/TaxAmount"),
        "ust_satz_promille": (satz, "UBL TaxCategory/Percent"),
    })
    if reverse_charge:
        felder["reverse_charge"] = Fund(
            True, QUELLE, "UBL TaxCategory/ID = AE (VAT Reverse Charge)", True
        )
    _steuerkategorie(felder, kategorien, "UBL TaxSubtotal/TaxCategory/ID")

    zahlbar = _cent(_text(summen, "PayableAmount"))
    meldungen = _nachpruefen(brutto, netto, ust, saetze, reverse_charge)
    if zahlbar is not None and brutto is not None and zahlbar != brutto:
        meldungen += (
            # Deutsches Betragsformat: "150,00 €", nicht "150.00".
            f"Zu zahlen sind {euro(zahlbar)} €, die Leistung kostet aber "
            f"{euro(brutto)} € — vermutlich ist eine Anzahlung verrechnet. "
            "Betriebsausgabe ist der Bruttobetrag; die Zahlung wird gesondert erfasst.",
        )
    typ = (_text(wurzel, "InvoiceTypeCode") or "").strip()
    return Vorschlag(QUELLE, felder, meldungen,
                     _bedenken(brutto, netto, ust, saetze, typ=typ, zahlbar=zahlbar))


def _nachpruefen(
    brutto: int | None, netto: int | None, ust: int | None,
    saetze: set[int], reverse_charge: bool,
) -> tuple[str, ...]:
    """Was dem Nutzer vor dem Bestaetigen auffallen soll.

    Geprueft wird die **Summenlogik des Dokuments**, nicht die Erkennung: aus
    dem XML sind die Zahlen exakt. Ein Dokument, dessen Summen nicht aufgehen,
    ist aber trotzdem eines -- und das will man wissen, bevor man es buchst.
    """
    meldungen: list[str] = []
    if None not in (brutto, netto, ust) and netto + ust != brutto:
        meldungen.append(
            f"Die Summen des Belegs gehen nicht auf: {netto / 100:.2f} + {ust / 100:.2f} "
            f"ergibt nicht {brutto / 100:.2f}. Bitte am Dokument prüfen."
        )
    if len(saetze) > 1:
        gelesen = ", ".join(f"{s / 10:g} %" for s in sorted(saetze))
        meldungen.append(
            f"Mehrere Steuersätze auf einem Beleg ({gelesen}) — es gibt keinen einen "
            "Satz, der Satz bleibt leer."
        )
    if reverse_charge:
        meldungen.append(
            "Reverse Charge: nach § 13b Abs. 5 Satz 1 UStG schuldest du die Umsatzsteuer, "
            "auch als Kleinunternehmer. Der Beleg weist deshalb keine aus."
        )
    return tuple(meldungen)


#: UNTDID 1001: die Dokumentart, die ohne Pruefmaske gebucht werden darf --
#: die gewoehnliche Handelsrechnung. Alles andere muss jemand ansehen.
HANDELSRECHNUNG = "380"

#: Dokumentarten, die eine Rechnung sind, aber keine gewoehnliche: eine
#: Rechnungskorrektur ERSETZT eine gebuchte Rechnung (beide gebucht waere die
#: Ausgabe doppelt), eine Anzahlungsrechnung wird mit der Schlussrechnung
#: verrechnet, eine Gutschrift im Gutschriftverfahren stellt der Kunde aus.
DOKUMENTARTEN = {
    "384": "Rechnungskorrektur", "386": "Anzahlungsrechnung", "326": "Teilrechnung",
    "389": "Gutschriftverfahren (vom Empfänger ausgestellt)", "875": "Teilrechnung (Bau)",
    "876": "Teilschlussrechnung", "877": "Schlussrechnung", "751": "Rechnungsinformation",
}


def _bedenken(
    brutto: int | None, netto: int | None, ust: int | None, saetze: set[int], *,
    typ: str | None = HANDELSRECHNUNG, zahlbar: int | None = None,
) -> tuple[str, ...]:
    """Was gegen ein Buchen OHNE Pruefmaske spricht (``Vorschlag.bedenken``).

    Jeder Wert aus dem XML ist exakt -- ein Dokument, dessen Summen nicht
    aufgehen, ist aber trotzdem widerspruechlich: welcher Betrag gilt, weiss
    nur, wer es ansieht. ``buche`` verwirft ein Netto, das nicht zum Brutto
    passt; gebucht waere sonst ein Brutto, das niemand geprueft hat, und die
    Wertgrenze eines GWG mit einem Netto geprueft, das danach nicht mehr da
    ist. Bei mehreren Steuersaetzen gibt es keinen einen Satz.

    Ohne Pruefmaske gebucht wird ausserdem nur die gewoehnliche
    Handelsrechnung (TypeCode 380): eine Rechnungskorrektur (384) neben der
    gebuchten Rechnung waere die Ausgabe doppelt, eine Anzahlungsrechnung
    (386) kaeme zur Schlussrechnung hinzu. Und weicht der Zahlbetrag vom
    Brutto ab, ist eine Anzahlung oder ein Guthaben verrechnet -- dann muss
    jemand entscheiden, was diese Rechnung als Ausgabe ausmacht.

    >>> _bedenken(546, 459, 87, {190}), _bedenken(20000, 10000, 1900, {190})[0][:30]
    ((), 'Summen gehen nicht auf: 100,00')
    >>> _bedenken(546, 459, 87, {190}, typ="384")[0][:45]
    'Rechnungskorrektur (TypeCode 384) — keine gew'
    >>> _bedenken(18000, 18000, 0, {0}, zahlbar=15000)[0][:30]
    'Zu zahlen sind 150,00 €, die R'
    """
    gruende: list[str] = []
    if typ is not None and typ != HANDELSRECHNUNG:
        art = DOKUMENTARTEN.get(typ, "keine gewöhnliche Rechnung" if typ else "Dokumentart fehlt")
        gruende.append(
            f"{art} (TypeCode {typ or '–'}) — keine gewöhnliche Rechnung, bitte in der "
            "Prüfmaske prüfen" + (" (ersetzt sie eine gebuchte Rechnung?)" if typ == "384"
                                  else "")
        )
    if zahlbar is not None and brutto is not None and zahlbar != brutto:
        gruende.append(
            f"Zu zahlen sind {euro(zahlbar)} €, die Rechnung lautet über {euro(brutto)} € "
            "— vermutlich ist eine Anzahlung oder ein Guthaben verrechnet, bitte in der "
            "Prüfmaske prüfen"
        )
    if None not in (brutto, netto, ust) and netto + ust != brutto:
        gruende.append(
            f"Summen gehen nicht auf: {euro(netto)} € netto + {euro(ust)} € Umsatzsteuer "
            f"ergeben nicht {euro(brutto)} € brutto — bitte am Dokument prüfen"
        )
    if len(saetze) > 1:
        gruende.append("mehrere Steuersätze auf einem Beleg — bitte in der Prüfmaske prüfen")
    return tuple(gruende)


# ------------------------------------------------------------------ Einstieg

def lies(daten: bytes, *, herkunft: str = "XML") -> Vorschlag:
    """Liest CII oder UBL. Entschieden wird am Wurzelelement.

    >>> lies(b'<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
    ...       b'Invoice-2"><ID>R-1</ID></Invoice>').wert("belegnummer")
    'R-1'
    """
    wurzel = _wurzel(daten)
    name = _lokal(wurzel)
    if name == "CrossIndustryInvoice":
        return _lies_cii(wurzel)
    if name == "Invoice":
        return _lies_ubl(wurzel)
    if name == "CreditNote":
        raise Gutschrift(
            f"{herkunft} ist eine Gutschrift (UBL CreditNote) und keine Rechnung. "
            "Eine Gutschrift kehrt das Vorzeichen um: sie mindert die Ausgabe des "
            "ursprünglichen Belegs und wird nicht als Ausgabe gebucht."
        )
    raise BelegFehler(
        f"{herkunft}: unbekanntes Wurzelelement <{name}>. Erwartet werden "
        "CrossIndustryInvoice (ZUGFeRD, Factur-X) oder Invoice (XRechnung in UBL)."
    )


def lies_datei(pfad: Path) -> Vorschlag | None:
    """Liest eine Datei, wenn sie XML enthaelt. ``None``, wenn nicht.

    Fuer ``.xml`` direkt, fuer ``.pdf`` aus den Anhaengen. Ein ``None`` heisst:
    hier ist Stufe 2 zustaendig. Ein ``BelegFehler`` heisst: hier gibt es ein
    XML, es ist aber nicht lesbar -- und dann ist Schluss, nicht Stufe 2.
    """
    endung = pfad.suffix.lower()
    if endung == ".xml":
        return lies(pfad.read_bytes(), herkunft=pfad.name)
    if endung != ".pdf":
        return None
    gefunden = finde_xml(pfad)
    if gefunden is None:
        return None
    name, daten = gefunden
    return lies(daten, herkunft=f"{pfad.name} ({name})")

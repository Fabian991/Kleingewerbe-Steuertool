"""Stufe 2: die Textschicht des PDF.

Deckt erfahrungsgemaess die meisten Lieferantenrechnungen ab -- sie sind
digital erzeugt und tragen ihren Text mit. Kostenlos, offline, zeichengenau:
gelesen wird, was im PDF steht, nicht was ein Erkenner darin vermutet.

Die eigentliche Arbeit steckt nicht hier, sondern in ``felder.py``. Diese Stufe
holt den Text und entscheidet eine Frage: **ist das ein Scan?** Ein Scan hat
keine brauchbare Textschicht, und dann ist Stufe 3 zustaendig. Die Antwort
muss belastbar sein, denn eine fast leere Textschicht ist der unangenehmere
Fall: sie liefert ein paar Zeichen, aus denen eine Heuristik dann einen
Vorschlag baut, den niemand hinterfragt.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.logik.beleg import felder as f
from app.logik.beleg.ergebnis import BelegFehler, Fund, Vorschlag, pdf_fehlertext

QUELLE = "pdf_text"

#: Woerter, die ein Datum als Belegdatum ausweisen. Nur ein so bezeichnetes
#: Datum gilt als sicher; das frueheste Datum irgendwo im Text ist ein Fund.
DATUMSWOERTER = ("rechnungsdatum", "belegdatum", "invoice date", "bill date",
                 "date of issue", "rechnung vom", "issued", "ausstellungsdatum")

#: Datumsangaben, die NICHT das Belegdatum sind. "datum" steckt in ihnen
#: allen -- als Wortteil darf es deshalb nicht genuegen: ein Lieferdatum aus
#: dem Dezember waere sonst als "sicheres" Belegdatum gebucht worden, abgelegt
#: im falschen Jahr.
_ANDERE_DATEN = re.compile(
    r"(liefer|leistungs|f[äa]e?lligkeits|zahlungs|buchungs|bestell|auftrags|versand|"
    r"abbuchungs|valuta)\s*-?\s*datum|due\s+date|delivery\s+date|service\s+date|"
    r"payment\s+date|order\s+date|leistungszeitraum|lieferzeitraum",
    re.I,
)

#: "Datum" bzw. "Date" als eigenes Wort -- nicht als Teil von "Lieferdatum".
_DATUM_ALLEIN = re.compile(r"(?<![a-zäöüß-])(datum|date)(?![a-zäöüß])", re.I)


def datum_sicher(datum: str | None, zeile: str, text: str, *,
                 heute: date | None = None) -> bool:
    """Ist das gefundene Datum sicher das Belegdatum?

    Ja, wenn seine Zeile ein eindeutiges Wort traegt (Rechnungsdatum,
    Belegdatum, Invoice date ...). Oder wenn dort "Datum" allein steht und
    keine Liefer-, Leistungs-, Faelligkeits- oder Zahlungsdatum-Zeile ein
    anderes Datum nennt -- dann ist offen, welches der beiden gemeint ist.

    >>> datum_sicher("2026-03-14", "Rechnungsdatum: 14.03.2026", "", heute=date(2026, 10, 1))
    True
    >>> datum_sicher("2025-12-22", "Lieferdatum: 22.12.2025", "", heute=date(2026, 10, 1))
    False
    >>> datum_sicher("2026-01-10", "Datum: 10.01.2026",
    ...              "Datum: 10.01.2026\\nLieferdatum: 22.12.2025", heute=date(2026, 10, 1))
    False
    >>> datum_sicher("2026-01-10", "Datum: 10.01.2026", "Datum: 10.01.2026",
    ...              heute=date(2026, 10, 1))
    True
    """
    if not datum or not zeile:
        return False
    klein = zeile.lower()
    if any(wort in klein for wort in DATUMSWOERTER):
        return True
    if _ANDERE_DATEN.search(zeile) or not _DATUM_ALLEIN.search(zeile):
        return False
    for andere in text.splitlines():
        if _ANDERE_DATEN.search(andere):
            gefunden, _ = f.finde_datum(andere, heute=heute, worte=("\x00",))
            if gefunden and gefunden != datum:
                return False
    return True

#: Unter so vielen Zeichen je Seite gilt die Textschicht als unbrauchbar.
#: Eine Rechnung hat Hunderte Zeichen; was darunter liegt, sind die
#: Bildbeschriftungen eines Scans oder eine Erzeugersignatur.
MINDESTZEICHEN_JE_SEITE = 120


@dataclass(frozen=True, slots=True)
class Textschicht:
    text: str
    seiten: int
    zeichen_je_seite: float
    brauchbar: bool
    begruendung: str


def hole_text(pfad: Path) -> Textschicht:
    """Liest die Textschicht und beurteilt sie."""
    import pdfplumber

    try:
        with pdfplumber.open(str(pfad)) as pdf:
            seiten = [(seite.extract_text() or "") for seite in pdf.pages]
    except Exception as fehler:  # noqa: BLE001 -- jede Ursache fuehrt zum selben Ergebnis
        raise BelegFehler(pdf_fehlertext(fehler)) from fehler

    text = "\n".join(seiten)
    anzahl = len(seiten) or 1
    je_seite = len(text.strip()) / anzahl
    brauchbar = je_seite >= MINDESTZEICHEN_JE_SEITE
    return Textschicht(
        text=text,
        seiten=len(seiten),
        zeichen_je_seite=je_seite,
        brauchbar=brauchbar,
        begruendung=(
            f"{je_seite:.0f} Zeichen je Seite"
            + ("" if brauchbar else f", unter der Schwelle von {MINDESTZEICHEN_JE_SEITE}"
                                    " — vermutlich ein Scan oder ein Foto")
        ),
    )


def lies_text(
    text: str, *, quelle: str = QUELLE, heute: date | None = None,
    eigene_ust_idnr: str | None = None, format_: str | None = None,
) -> Vorschlag:
    """Zieht die Belegfelder aus einem Text.

    Nimmt den Text und nicht den Pfad, damit Stufe 3 (Bilderkennung) dieselbe
    Auswertung benutzt: der Weg von Text zu Feldern ist derselbe, nur die
    Herkunft unterscheidet sich -- und die steht in ``quelle``.
    """
    if format_ is None:
        format_, formatgrund = f.erkenne_format(text)
    else:
        formatgrund = "vorgegeben"

    reverse_charge, rc_stelle = f.finde_reverse_charge(text)
    urteil = f.bewerte_betraege(text, format_, reverse_charge=reverse_charge)
    datum, datumszeile = f.finde_datum(text, heute=heute)
    nummer, nummernzeile = f.finde_belegnummer(text)
    ust_idnr = f.finde_ust_idnr(text, ausser=eigene_ust_idnr)
    lieferant = finde_lieferant(text)

    funde: dict[str, Fund] = {}

    def setze(feld: str, wert: object, nachweis: str, sicher: bool) -> None:
        if wert not in (None, ""):
            funde[feld] = Fund(wert=wert, quelle=quelle, nachweis=nachweis, sicher=sicher)

    # Der Lieferantenname ist NIE sicher: er steht nirgends bezeichnet, die
    # erste Zeile ist nur die wahrscheinlichste Stelle. Beim zweiten Beleg
    # desselben Lieferanten entscheidet der Lernspeicher, nicht diese Zeile.
    setze("lieferant", lieferant[0], lieferant[1], False)
    # Die USt-IdNr. ist nur dann sicher, wenn der Beleg genau EINE fremde
    # nennt. Stehen zwei da (Lieferant und Zahlungsdienstleister, ein
    # Marktplatz und sein Haendler), ist die erste nur die wahrscheinlichste.
    kandidaten = f.alle_ust_idnrn(text, ausser=eigene_ust_idnr)
    setze("lieferant_ust_idnr", ust_idnr,
          "Muster einer USt-IdNr. im Text"
          + (f" — {len(kandidaten)} verschiedene gefunden" if len(kandidaten) > 1 else ""),
          len(kandidaten) == 1)
    setze("belegnummer", nummer, nummernzeile, bool(nummernzeile))
    # Das Datum ist nur sicher, wenn es BEZEICHNET ist. finde_datum faellt
    # sonst auf das frueheste plausible Datum des Belegs zurueck -- ein
    # Leistungs- oder Lieferdatum, das als Belegdatum durchging, und gruen.
    # Auch "Lieferdatum" ist bezeichnet, aber nicht als Belegdatum.
    setze("belegdatum", datum, datumszeile,
          datum_sicher(datum, datumszeile, text, heute=heute))
    if urteil.brutto is not None:
        setze("brutto", urteil.brutto.cent, urteil.nachweis, urteil.sicher)
    if urteil.netto is not None:
        setze("netto", urteil.netto.cent, urteil.nachweis, urteil.sicher)
    # Bei Reverse Charge sind Steuer und Satz nicht geraten, sondern
    # rechtlich festgelegt: null. Sie sind deshalb sicher, auch wenn der
    # Gesamtbetrag selbst noch zu pruefen ist.
    steuerfrei = reverse_charge and urteil.satz_promille == 0
    if urteil.ust is not None:
        setze("ust", urteil.ust.cent,
              "Steuerfrei (Reverse Charge)" if steuerfrei else urteil.nachweis,
              urteil.sicher or steuerfrei)
    if urteil.satz_promille is not None:
        setze("ust_satz_promille", urteil.satz_promille,
              "Steuerfrei (Reverse Charge)" if steuerfrei else urteil.nachweis,
              urteil.sicher or steuerfrei)
    if reverse_charge:
        setze("reverse_charge", True, rc_stelle, True)
    setze("waehrung", finde_waehrung(text), "Währungszeichen im Text", False)

    meldungen = urteil.meldungen
    # Teilweise mit Guthaben oder Gutschein bezahlt: vorgeschlagen wird der
    # tatsaechlich bezahlte Betrag, mit Vermerk (Entscheidung des Nutzers,
    # siehe felder.Guthaben). Netto und Steuer gehoeren zum vollen Betrag
    # und passen nicht mehr -- sie entfallen.
    guthaben = f.finde_guthaben(text, format_)
    if guthaben is not None:
        for feld in ("brutto", "netto", "ust", "ust_satz_promille"):
            funde.pop(feld, None)
        if guthaben.bezahlt_cent > 0:
            setze("brutto", guthaben.bezahlt_cent, guthaben.nachweis, False)
        setze("notiz", guthaben.vermerk, guthaben.nachweis, False)
        meldungen = tuple(m for m in meldungen
                          if not m.startswith("Die Beträge gehen rechnerisch nicht auf"))
        meldungen += (guthaben.hinweis,)
    art = dokumentart_bedenken(text)
    bedenken = (art,) if art else ()
    if format_ == f.Zahlenformat.EN:
        # Der Fall, der still falsch geht: AWS, Google, Figma und Adobe rechnen
        # in Euro und schreiben 1,234.56. Mit deutschen Regeln gelesen waeren
        # das 1,23 — und der Beleg sieht trotzdem richtig aus.
        meldungen += (
            f"Englisches Zahlenformat erkannt ({formatgrund}): 1,234.56 ist "
            "eintausendzweihundertvierunddreißig Komma sechsundfünfzig.",
        )
    return Vorschlag(quelle, funde, meldungen, bedenken)


#: So viele Zeilen gelten als Kopf des Belegs: dort stehen Ueberschrift,
#: Nummer und Datum -- und damit, was fuer ein Dokument es ist.
KOPFZEILEN = 30

#: Dokumentarten, die wie eine Rechnung aussehen, aber keine gewoehnliche
#: Ausgabe sind. Steht eines dieser Woerter im Kopf, wird nicht ohne
#: Pruefmaske gebucht: eine Gutschrift ist Geld, das zurueckkommt, eine
#: Stornorechnung hebt eine Rechnung auf, ein Angebot, eine Auftrags-
#: bestaetigung, eine Proforma-Rechnung oder ein Lieferschein sind gar keine
#: Rechnung, eine Mahnung wiederholt eine, und Anzahlungs-, Abschlags- und
#: Schlussrechnungen muessen gegeneinander verrechnet werden. Gelesen wird
#: an Wortanfaengen: "Stornorechnung", "Rechnungskorrektur" und
#: "Proforma-Rechnung" treffen, "Kostenvoranschlag" auch.
#:
#: Dazu Kreditkartenabrechnung, Kontoauszug und Umsatzuebersicht: sie listen
#: Zahlungen, die schon einzeln als Beleg gebucht sind (oder werden) -- als
#: Ausgabe gebucht stuende alles doppelt in der EUeR. Ein englisches
#: "Statement" ist dasselbe.
_KEINE_GEWOEHNLICHE_RECHNUNG = re.compile(
    r"(?<![a-zäöüß])(gutschrift|credit\s?note|storno|korrektur|korrigiert|"
    r"angebot|kostenvoranschlag|quotation|auftragsbest[äa]e?tigung|"
    r"bestellbest[äa]e?tigung|order\s+confirmation|pro[\s-]?forma|mahnung|"
    r"zahlungserinnerung|payment\s+reminder|lieferschein|delivery\s+note|"
    r"anzahlung|abschlag|vorauszahlung|schlussrechnung|teilschlussrechnung|"
    r"kreditkartenabrechnung|kartenabrechnung|kontoauszug|konto[üu]e?bersicht|"
    r"umsatz[üu]e?bersicht|umsatzaufstellung|statement)",
    re.I,
)

#: Und das Gegenstueck: ohne eines dieser Woerter ist unklar, was das
#: Dokument ist. Auch als Wortteil -- "Rechnungsnummer" zeigt eine Rechnung.
#: Aber nicht als Teil von "Abrechnung": eine "Kreditkartenabrechnung" oder
#: "Jahresabrechnung" ist dadurch noch keine Rechnung -- vorher genuegte das.
_RECHNUNG = re.compile(r"(?<!ab)rechnung|invoice|faktura|facture", re.I)


def dokumentart_bedenken(text: str) -> str | None:
    """Spricht der Kopf des Belegs dagegen, dass er eine gewoehnliche Rechnung ist?

    Die Textschicht kennt -- anders als das XML einer E-Rechnung -- keine
    Dokumentart. Eine Gutschrift, ein Angebot oder eine Stornorechnung tragen
    aber Nummer, Datum und aufgehende Betraege wie eine Rechnung und waeren
    sonst als Ausgabe gebucht. Fuer die Pruefmaske ist das nur ein Hinweis;
    das Abholen aus dem Eingangsordner bucht dann nicht selbst.

    >>> dokumentart_bedenken("Hetzner Online GmbH\\nRechnung\\nRechnungsnummer: R1") is None
    True
    >>> dokumentart_bedenken("Hetzner Online GmbH\\nGutschrift\\nNummer: G1")[:40]
    'Dokumentart nicht eindeutig Rechnung — „'
    >>> dokumentart_bedenken("Hetzner Online GmbH\\nBeleg Nr. 1")[:45]
    'Dokumentart nicht eindeutig Rechnung — das Wo'
    >>> dokumentart_bedenken("Beispielbank AG\\nKreditkartenabrechnung\\nOktober")[:42]
    'Dokumentart nicht eindeutig Rechnung — „Kr'
    >>> dokumentart_bedenken("Beispiel GmbH\\nJahresabrechnung 2026")[:45]
    'Dokumentart nicht eindeutig Rechnung — das Wo'
    >>> dokumentart_bedenken("Beispiel GmbH\\nAbrechnung\\nRechnungsnummer: 7") is None
    True
    """
    kopf = "\n".join([z for z in text.splitlines() if z.strip()][:KOPFZEILEN])
    treffer = _KEINE_GEWOEHNLICHE_RECHNUNG.search(kopf)
    if treffer:
        zeile = next((z.strip() for z in kopf.splitlines() if treffer.group(0) in z), "")
        return (f"Dokumentart nicht eindeutig Rechnung — „{zeile[:60]}“ im Kopf des Belegs "
                "(Gutschrift, Storno, Angebot, Mahnung, Anzahlung o. Ä.), bitte in der "
                "Prüfmaske prüfen")
    if not _RECHNUNG.search(kopf):
        return ("Dokumentart nicht eindeutig Rechnung — das Wort „Rechnung“ fehlt im Kopf "
                "des Belegs, bitte in der Prüfmaske prüfen")
    return None


def lies_datei(
    pfad: Path, *, heute: date | None = None, eigene_ust_idnr: str | None = None,
    textleser: Callable[[Path], Textschicht] | None = None,
) -> Vorschlag | None:
    """Liest ein PDF ueber seine Textschicht. ``None``, wenn es ein Scan ist.

    ``textleser`` ersetzt ``hole_text`` -- das Abholen liest die Textschicht
    in einem eigenen Prozess mit Zeitgrenze (``textdienst``).
    """
    if pfad.suffix.lower() != ".pdf":
        return None
    schicht = (textleser or hole_text)(pfad)
    if not schicht.brauchbar:
        return None
    vorschlag = lies_text(schicht.text, heute=heute, eigene_ust_idnr=eigene_ust_idnr)
    return Vorschlag(vorschlag.quelle, vorschlag.felder,
                     vorschlag.meldungen + (f"Textschicht gelesen: {schicht.begruendung}.",),
                     vorschlag.bedenken)


#: Rechtsformen und Firmenkennzeichen. Eine Zeile mit einem davon ist mit
#: grosser Wahrscheinlichkeit die Firmenzeile.
RECHTSFORMEN = (
    "gmbh", "ug (", "ag", "kg", "ohg", "gbr", "e.k.", "e. k.", "mbh", "se",
    "ltd", "limited", "inc.", "inc", "llc", "b.v.", "bv", "n.v.", "s.a.",
    "s.r.l.", "sarl", "plc", "e.v.", "gmbh & co",
)

#: Die Rechtsformen als EIGENSTAENDIGE Woerter. Ein reiner Textvergleich waere
#: hier falsch, und zwar unauffaellig falsch: "ag" steckt am Ende von
#: "Auftrag", "se" in "Adresse", "bv" nirgends -- und dann gilt die Zeile
#: "Vielen Dank fuer Ihren Auftrag" als Firmenzeile.
_RECHTSFORM = re.compile(
    r"(?<![\wäöüß])(?:"
    r"gmbh|mbh|ug|ag|kg|ohg|gbr|se|e\.?\s?k\.?|e\.?\s?v\.?|"
    r"ltd|limited|inc|llc|plc|b\.?v\.?|n\.?v\.?|s\.?a\.?|s\.?r\.?l\.?|sarl|"
    r"co\.?\s?kg|gmbh\s?&\s?co"
    r")(?![\wäöüß])",
    re.I,
)

#: Zeilen, die nie der Lieferantenname sind.
KEIN_NAME = (
    "rechnung", "invoice", "receipt", "beleg", "quittung", "kundennummer",
    "customer", "seite", "page", "datum", "date", "steuernummer", "ust-idnr",
    "vat", "iban", "bic", "telefon", "e-mail", "email", "www.", "http",
)


def finde_lieferant(text: str) -> tuple[str | None, str]:
    """Wer die Leistung erbracht hat. Gibt (Name, Begruendung) zurueck.

    Eine Heuristik und als solche markiert: der Name steht auf einem Beleg
    nirgends bezeichnet. Zuerst eine Zeile mit einer Rechtsform -- die ist ein
    starker Hinweis --, sonst die erste nicht-leere Zeile, die kein
    Formularwort traegt. Der Lernspeicher ersetzt das ab dem zweiten Beleg
    desselben Lieferanten.

    >>> finde_lieferant("Hetzner Online GmbH\\nIndustriestr. 25")[0]
    'Hetzner Online GmbH'
    >>> finde_lieferant("RECHNUNG\\nFigma, Inc.\\n760 Market St")[0]
    'Figma, Inc.'
    """
    zeilen = [z.strip() for z in text.splitlines()[:25] if z.strip()]
    tauglich = [z for z in zeilen
                if 3 <= len(z) <= 70 and not any(w in z.lower() for w in KEIN_NAME)]

    for zeile in tauglich:
        if _RECHTSFORM.search(zeile):
            return zeile, f"Zeile mit Rechtsform: {zeile}"
    if tauglich:
        return tauglich[0], f"Erste brauchbare Zeile: {tauglich[0]}"
    return None, ""


_WAEHRUNGEN = (("€", "EUR"), ("EUR", "EUR"), ("$", "USD"), ("USD", "USD"),
               ("CHF", "CHF"), ("£", "GBP"), ("GBP", "GBP"))

#: Dollarzeichen mit Landeskuerzel -- vor dem blossen "$" gezaehlt und dann
#: aus dem Text genommen: "CA$" ist kein US-Dollar.
_DOLLAR = (("US$", "USD"), ("CA$", "CAD"), ("AU$", "AUD"), ("NZ$", "NZD"),
           ("HK$", "HKD"), ("MX$", "MXN"), ("C$", "CAD"), ("A$", "AUD"), ("S$", "SGD"),
           ("R$", "BRL"))

#: Weitere Codes, die als Waehrung zaehlen, wenn sie direkt an einem Betrag
#: stehen ("AED 367.00", "1.250 SEK"): die der EZB-Tabelle und einige, die
#: sie nicht fuehrt. Nicht jeder Code -- "ALL" oder "TOP" stehen auch so im
#: Text.
_CODES_AN_BETRAG = frozenset({
    "AUD", "BGN", "BRL", "CAD", "CNY", "CZK", "DKK", "HKD", "HUF", "IDR", "ILS", "INR",
    "ISK", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PLN", "RON", "SEK", "SGD", "THB",
    "TRY", "ZAR", "AED", "SAR", "QAR", "KWD", "BHD", "OMR", "EGP", "UAH", "RSD", "TWD",
})
_CODE_AN_BETRAG = re.compile(r"(?<![A-Za-z])([A-Z]{3})\s?(?=\d)|(?<=\d)\s?([A-Z]{3})(?![A-Za-z])")


def finde_waehrung(text: str) -> str | None:
    """Die haeufigste Waehrungsangabe im Text.

    Geraten, nie sicher: ein "$" allein sagt nicht, welcher Dollar. "CA$" und
    "A$" zaehlen deshalb als kanadische und australische Dollar, und ein Code
    direkt am Betrag ("AED 367.00") als diese Waehrung.

    >>> finde_waehrung("Gesamtbetrag 8,29 EUR")
    'EUR'
    >>> finde_waehrung("Total due $1,234.56")
    'USD'
    >>> finde_waehrung("Total CA$119.00"), finde_waehrung("Total AED 367.00")
    ('CAD', 'AED')
    """
    zaehler: dict[str, int] = {}
    for zeichen, code in _DOLLAR:
        anzahl = text.count(zeichen)
        if anzahl:
            zaehler[code] = zaehler.get(code, 0) + anzahl
            text = text.replace(zeichen, " ")
    for zeichen, code in _WAEHRUNGEN:
        anzahl = text.count(zeichen)
        if anzahl:
            zaehler[code] = zaehler.get(code, 0) + anzahl
    for treffer in _CODE_AN_BETRAG.finditer(text):
        code = treffer.group(1) or treffer.group(2)
        if code in _CODES_AN_BETRAG:
            zaehler[code] = zaehler.get(code, 0) + 1
    if not zaehler:
        return None
    return max(zaehler.items(), key=lambda paar: paar[1])[0]

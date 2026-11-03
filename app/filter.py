"""Anzeigefilter fuer Jinja2, gemeinsam von Oberflaeche und PDF genutzt.

Ein zweiter Satz Filter fuer das PDF waere der sichere Weg zu zwei
Schreibweisen fuer denselben Betrag -- auf dem Bildschirm 1.234,56 und auf dem
Papier 1234.56.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from fractions import Fraction

from app.logik.backup import menschenlesbar
from app.logik.rundung import euro
from app.logik.steuertarif import abgeschnitten
from app.logik.zeiten import dezimalstunden, formatiere_dauer

#: UN/ECE Rec 20 Einheitencodes, wie sie das Datenmodell fuehrt.
EINHEITEN = {
    "HUR": "Std.",
    "DAY": "Tage",
    "C62": "Stück",
    "LS": "pausch.",
}


def datum_de(wert: str | date | None) -> str:
    """ISO-Datum als 02.03.2026. Leere Werte ergeben einen Gedankenstrich.

    Ein Zeitstempel wird auf seinen Datumsanteil gekuerzt, und was sich gar
    nicht lesen laesst, kommt unveraendert zurueck. Beides ist Absicht: ein
    ANZEIGEFILTER darf die Seite nicht mitreissen. ``date.fromisoformat``
    wirft bei allem, was nicht genau ein ISO-Datum ist -- und eine einzige
    krumme Spalte legte damit jede Seite lahm, auf der sie vorkommt. Der
    Rohwert stehenzulassen macht den Fehler sichtbar, ohne alles andere
    unerreichbar zu machen.

    >>> datum_de("2026-03-02")
    '02.03.2026'
    >>> datum_de("2026-03-02T14:20:52+00:00")
    '02.03.2026'
    >>> datum_de(None)
    '—'
    >>> datum_de("gestern")
    'gestern'
    """
    if not wert:
        return "—"
    if isinstance(wert, str):
        try:
            tag = date.fromisoformat(wert[:10])
        except ValueError:
            return wert
    else:
        tag = wert
    return tag.strftime("%d.%m.%Y")


def menge(tausendstel: int | None) -> str:
    """Tausendstel als deutsche Dezimalzahl.

    Zwei Stellen, solange sie reichen -- sonst drei. Die Spalte fuehrt
    TAUSENDSTEL, und mit fest zwei Stellen stimmte die Rechnung auf dem Papier
    nicht mehr: 117 Tausendstel (sieben Minuten, ungerundet abgerechnet)
    stuenden als "0,12", der Betrag waere aber aus 0,117 gerechnet. Wer die
    Rechnung nachrechnet, kommt auf eine andere Zahl. Noch schlimmer bei
    kleinen Mengen: aus einer Position mit Menge wuerde "0,00", und die Menge
    ist nach § 34a Satz 1 Nr. 4 UStDV Pflichtangabe.

    Beim ueblichen Viertelstundentakt aendert sich dadurch nichts.

    >>> menge(250), menge(1500)
    ('0,25', '1,50')
    >>> menge(117)
    '0,117'
    >>> menge(1)
    '0,001'
    >>> menge(None)
    '0,00'
    """
    roh = tausendstel or 0
    text = f"{roh / 1000:.3f}" if roh % 10 else f"{roh / 1000:.2f}"
    return text.replace(".", ",")


def einheit_de(code: str | None) -> str:
    """Einheitencode als deutsches Wort; Unbekanntes bleibt stehen.

    >>> einheit_de("HUR"), einheit_de("LS")
    ('Std.', 'pausch.')
    >>> einheit_de("KGM"), einheit_de(None)
    ('KGM', '')
    """
    return EINHEITEN.get(code or "", code or "")


def iban(wert: str | None) -> str:
    """IBAN in Vierergruppen, wie sie auf Belegen lesbar ist.

    >>> iban("de02 1203 0000 0000 2020 51")
    'DE02 1203 0000 0000 2020 51'
    >>> iban("DE02120300000000202051")
    'DE02 1203 0000 0000 2020 51'
    >>> iban(None)
    ''
    """
    if not wert:
        return ""
    kompakt = wert.replace(" ", "").upper()
    return " ".join(kompakt[i : i + 4] for i in range(0, len(kompakt), 4))


#: Laendernamen fuer die Anschrift. Auf dem Papier steht nach DIN 5008 der
#: Name des Landes, nicht das Kuerzel -- "AT" als letzte Zeile einer Anschrift
#: liest ein Empfaenger in Salzburg als Tippfehler. Unbekannte Kuerzel bleiben
#: stehen; besser ein Kuerzel als gar kein Land.
LAENDER = {
    "DE": "Deutschland", "AT": "Österreich", "CH": "Schweiz", "LI": "Liechtenstein",
    "FR": "Frankreich", "IT": "Italien", "NL": "Niederlande", "BE": "Belgien",
    "LU": "Luxemburg", "DK": "Dänemark", "SE": "Schweden", "NO": "Norwegen",
    "FI": "Finnland", "PL": "Polen", "CZ": "Tschechien", "SK": "Slowakei",
    "HU": "Ungarn", "SI": "Slowenien", "HR": "Kroatien", "ES": "Spanien",
    "PT": "Portugal", "IE": "Irland", "GR": "Griechenland", "RO": "Rumänien",
    "BG": "Bulgarien", "EE": "Estland", "LV": "Lettland", "LT": "Litauen",
    "MT": "Malta", "CY": "Zypern", "GB": "Vereinigtes Königreich",
    "UK": "Vereinigtes Königreich", "US": "USA", "CA": "Kanada", "AU": "Australien",
    "JP": "Japan", "CN": "China", "IN": "Indien", "TR": "Türkei",
}


def land_name(code: str | None) -> str:
    """Laenderkuerzel als deutscher Name; Unbekanntes bleibt stehen.

    >>> land_name("AT"), land_name(" at "), land_name("XK"), land_name(None)
    ('Österreich', 'Österreich', 'XK', '')
    """
    kuerzel = (code or "").strip().upper()
    return LAENDER.get(kuerzel, kuerzel)


#: Interne Schluessel in der Sprache des Nutzers. Jinjas ``capitalize`` taugt
#: dafuer nicht: es schreibt "Im entwurf" und "Ueberfaellig", weil es nur das
#: erste Zeichen gross und den Rest klein setzt und von Umlauten nichts weiss.
STATUS_TEXTE = {
    "AUFGESCHRIEBEN": "Aufgeschrieben", "IM_ENTWURF": "Im Entwurf",
    "IN_RECHNUNG_GESTELLT": "In Rechnung gestellt", "BEZAHLT": "Bezahlt",
    "TEILBEZAHLT": "Teilbezahlt", "OHNE_BERECHNUNG_GESTELLT": "Ohne Berechnung gestellt",
    "NICHT_ABRECHENBAR": "Nicht abrechenbar", "ABGESCHRIEBEN": "Abgeschrieben",
    "STORNIERT_GEBUNDEN": "Storniert, nicht freigegeben",
    "ENTWURF": "Entwurf", "FESTGESCHRIEBEN": "Festgeschrieben", "VERSENDET": "Versendet",
    "GESTELLT": "Gestellt", "UEBERFAELLIG": "Überfällig", "OFFEN": "Offen",
    "STORNIERT": "Storniert", "STORNO": "Storno", "OHNE_BETRAG": "Ohne Betrag",
    "LEISTUNGSNACHWEIS": "Leistungsnachweis", "RECHNUNG": "Rechnung",
    "GUTSCHRIFT_SELBSTABRECHNUNG": "Gutschrift (Selbstabrechnung)",
    "UEBERWEISUNG": "Überweisung", "KARTE": "Karte", "BAR": "Bar", "PAYPAL": "PayPal",
    "SONSTIGES": "Sonstiges",
    "STUNDEN": "Stunden", "PAUSCHAL": "Pauschal", "KULANZ": "Kulanz", "INTERN": "Intern",
    "UNTERNEHMER": "Unternehmer", "JURISTISCHE_PERSON": "Juristische Person",
    "PRIVAT": "Privatperson",
    "INLAND": "Inland", "EU_B2B_REVERSE_CHARGE": "EU-Unternehmer (Reverse Charge)",
    "DRITTLAND": "Drittland",
}


def status_text(schluessel: str | None) -> str:
    """Interner Schluessel als deutscher Text; Unbekanntes wird lesbar gemacht.

    >>> status_text("IM_ENTWURF"), status_text("UEBERFAELLIG")
    ('Im Entwurf', 'Überfällig')
    >>> status_text("NOCH_NICHT_BEKANNT"), status_text(None)
    ('Noch nicht bekannt', '')
    """
    roh = (schluessel or "").strip()
    if roh in STATUS_TEXTE:
        return STATUS_TEXTE[roh]
    worte = roh.replace("_", " ").lower()
    return worte[:1].upper() + worte[1:]


def zeit_lokal(utc_text: str | None) -> str:
    """UTC-Zeitstempel (ISO 8601) als Ortszeit: 19.10.2026 06:29.

    Die Datenbank fuehrt Zeitpunkte in UTC (``db.jetzt_utc``), der Nutzer lebt
    in seiner Zeitzone. Eine Sicherung von 06:29 Uhr stand in der Liste als
    04:29 -- ohne Hinweis, dass das UTC ist. Umgerechnet wird in die Zeitzone
    des Rechners, auf dem die Anwendung laeuft.

    Wie ``datum_de`` ein Anzeigefilter: ein Zeitstempel ohne Zeitzone gilt als
    UTC, was sich nicht lesen laesst, kommt unveraendert zurueck.

    >>> zeit_lokal(None), zeit_lokal("")
    ('—', '—')
    >>> zeit_lokal("kein Zeitpunkt")
    'kein Zeitpunkt'
    >>> len(zeit_lokal("2026-10-19T12:00:00+00:00"))
    16
    """
    if not utc_text:
        return "—"
    try:
        zeitpunkt = datetime.fromisoformat(str(utc_text).replace("Z", "+00:00"))
    except ValueError:
        return str(utc_text)
    if zeitpunkt.tzinfo is None:
        zeitpunkt = zeitpunkt.replace(tzinfo=UTC)
    return zeitpunkt.astimezone().strftime("%d.%m.%Y %H:%M")


def euro_ganz(cent: int | None) -> str:
    """Ein Betrag in vollen Euro mit Tausenderpunkt, kaufmaennisch gerundet.

    Fuer Schaetzungen und Tarifgrenzen: dort taeuschten Cent eine Genauigkeit
    vor, die es nicht gibt.

    Negativ mit echtem Minuszeichen (U+2212) wie die Vorlagen ("−4.970 €"),
    nicht mit Bindestrich. Nie in ein Eingabefeld schreiben: euro_nach_cent
    liest nur den Bindestrich.

    >>> euro_ganz(348000), euro_ganz(1234550), euro_ganz(-50050), euro_ganz(None)
    ('3.480', '12.346', '−501', '—')
    """
    if cent is None:
        return "—"
    vorzeichen = "−" if cent < 0 else ""
    ganz = (abs(cent) + 50) // 100
    return f"{vorzeichen}{ganz:,}".replace(",", ".")


def euro_text(cent: int | None) -> str:
    """Wie der Filter ``euro``, aber mit echtem Minuszeichen -- fuer Text, nicht fuer Felder.

    >>> euro_text(-148900), euro_text(500)
    ('−1.489,00', '5,00')
    """
    if cent is None:
        return "—"
    text = euro(cent)
    return "−" + text[1:] if text.startswith("-") else text


def prozent(satz: Fraction | float | None, stellen: int = 1) -> str:
    """Ein Anteil (0,324) als deutsche Prozentzahl ("32,4"), kaufmaennisch gerundet.

    >>> prozent(Fraction(324, 1000)), prozent(Fraction(1, 3), 2), prozent(Fraction(42, 100), 0)
    ('32,4', '33,33', '42')
    >>> prozent(None)
    '—'
    """
    if satz is None:
        return "—"
    faktor = 10 ** stellen
    wert = Fraction(satz) * 100 * faktor
    gerundet = int(wert + Fraction(1, 2)) if wert >= 0 else -int(-wert + Fraction(1, 2))
    ganz, rest = divmod(abs(gerundet), faktor)
    text = f"{ganz:,}".replace(",", ".") + (f",{rest:0{stellen}d}" if stellen else "")
    return ("-" if gerundet < 0 else "") + text


def prozent_abgeschnitten(satz: Fraction | None) -> str:
    """Ein Grenzsteuersatz mit zwei Nachkommastellen, ABGESCHNITTEN wie beim BMF-Rechner.

    Kaufmaennisch gerundet stuende an der Zonengrenze (zvE 69.878 € im Jahr
    2026) schon "42,00", obwohl dort noch die Progressionszone gilt; der BMF
    zeigt 41,99.

    >>> prozent_abgeschnitten(Fraction(419997, 1000000)), prozent_abgeschnitten(None)
    ('41,99', '—')
    """
    if satz is None:
        return "—"
    return abgeschnitten(Fraction(satz))


def registriere(env) -> None:
    """Haengt alle Filter an eine Jinja2-Umgebung."""
    env.filters.update(
        euro=euro,
        dauer=formatiere_dauer,
        dezimalstunden=dezimalstunden,
        datum_de=datum_de,
        menge=menge,
        einheit_de=einheit_de,
        iban=iban,
        land_name=land_name,
        status_text=status_text,
        dateigroesse=menschenlesbar,
        zeit_lokal=zeit_lokal,
        euro_ganz=euro_ganz,
        euro_text=euro_text,
        prozent=prozent,
        prozent_abgeschnitten=prozent_abgeschnitten,
    )

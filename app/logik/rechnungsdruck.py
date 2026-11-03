"""Was auf dem Rechnungspapier aus dem Absender abgeleitet wird.

Die Vorlage formatiert nur; was sie aus den Absenderangaben ABLEITET -- welcher
Name im Briefkopf steht, welche der drei Kennungen gedruckt wird, wer als
Kontoinhaber unter der IBAN steht --, entscheidet dieses Modul. Hier laesst es
sich pruefen, in der Vorlage nicht.

Alles rechnet aus dem uebergebenen Absender, und das ist bei einer
festgeschriebenen Rechnung IMMER ihr Schnappschuss
(``festschreiben.pdf_daten``), nie der Betriebsstamm von heute. Ein spaeter
nachgezogenes PDF zeigt so denselben Namen, dieselbe Kennung und dieselbe
Bankverbindung wie das versendete. Das Modul liest deshalb keine Datenbank.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Bis zu dieser Laenge passt die Ruecksendeangabe in 7 pt in eine Zeile der
#: 80 mm breiten Anschriftzone (Liberation Sans, gemessen). Darueber wird sie
#: auf 6,5 pt verkleinert und darf umbrechen -- abgeschnitten wird sie nie.
RUECKSENDE_EINZEILIG = 62

#: Ab diesen Laengen (Zeichen, "Inhaber · Name") wird der Name im Briefkopf
#: kleiner gesetzt. Er steht ab 15 mm und muss vor der Zusatz- und Vermerkzone
#: (ab 45 mm) enden; die Ruecksendeangabe darunter waechst nach oben. In
#: 16 pt fuellten gut 100 Zeichen vier Zeilen bis 42 mm, ab etwa 140 Zeichen
#: lagen Name und Ruecksendeangabe uebereinander (gemessen im Vorschau-PDF).
NAME_LANG = 100
NAME_SEHR_LANG = 150

#: Die Bezeichnungen, wie sie auf dem Papier stehen.
KENNUNG_TEXT = {
    "ust_idnr": "USt-IdNr.",
    "kleinunternehmer_idnr": "Kleinunternehmer-IdNr.",
    "steuernummer": "Steuernummer",
}

#: Vorrang bei mehreren eingetragenen Kennungen. § 34a Satz 1 Nr. 2 UStDV
#: verlangt EINE von dreien. Die USt-IdNr. zuerst: sie ist fuer die
#: Oeffentlichkeit bestimmt (Impressum, Pruefung durch Dritte beim BZSt nach
#: § 18e UStG), die Steuernummer dagegen ist der Schluessel zum Steuerkonto
#: beim Finanzamt und bleibt so von der Rechnung fern.
VORRANG = ("ust_idnr", "kleinunternehmer_idnr", "steuernummer")

#: Bei einer Reverse-Charge-Rechnung gilt nicht § 34a UStDV, sondern § 14
#: Abs. 4 Satz 1 Nr. 2 UStG (ueber § 14a Abs. 1): Steuernummer ODER USt-IdNr.
#: Die Kleinunternehmer-IdNr. genuegt dort nicht und steht deshalb zuletzt --
#: nur noch als Rueckfall fuer die Vorschau, denn festschreiben laesst die
#: Pruefung (R02) eine solche Rechnung nicht.
VORRANG_REVERSE_CHARGE = ("ust_idnr", "steuernummer", "kleinunternehmer_idnr")


def _text(wert: object) -> str:
    return " ".join(str(wert or "").split())


def anzeigename(absender: Mapping) -> str:
    """Der Name im Briefkopf und in der Ruecksendeangabe.

    Ein Personenname soll erkennbar sein: wer als nicht eingetragener
    Gewerbetreibender eine Geschaeftsbezeichnung fuehrt, darf sie nur
    ZUSAETZLICH zu Vor- und Zuname verwenden. Steht der Inhaber schon im
    Namen, wird er nicht wiederholt.

    >>> anzeigename({"name": "Webdesign Studio", "inhaber": "Erika Muster"})
    'Erika Muster · Webdesign Studio'
    >>> anzeigename({"name": "Erika Muster Webdesign Studio", "inhaber": "erika  muster"})
    'Erika Muster Webdesign Studio'
    >>> anzeigename({"name": "Mein Kleingewerbe", "inhaber": None})
    'Mein Kleingewerbe'
    """
    name = _text(absender.get("name"))
    inhaber = _text(absender.get("inhaber"))
    if inhaber and inhaber.casefold() not in name.casefold():
        return f"{inhaber} · {name}" if name else inhaber
    return name


def namensgroesse(name: str) -> str:
    """Die Schriftgroesse des Namens im Briefkopf als CSS-Klasse.

    Abgeschnitten wird nichts, und die Laenge ist nirgends begrenzt -- also
    wird ein langer Name kleiner, statt in die Ruecksendeangabe zu laufen.

    >>> namensgroesse("Erika Muster · Webdesign Studio")
    ''
    >>> namensgroesse("x" * 120), namensgroesse("x" * 200)
    ('lang', 'sehr-lang')
    """
    if len(name) > NAME_SEHR_LANG:
        return "sehr-lang"
    if len(name) > NAME_LANG:
        return "lang"
    return ""


def ruecksendeteile(absender: Mapping) -> list[str]:
    """Die Teile der Ruecksendeangabe: Name, Strasse, PLZ und Ort.

    Getrennt, damit die Vorlage nur ZWISCHEN den Teilen umbricht -- nie
    zwischen PLZ und Ort oder mitten in der Strasse.

    >>> ruecksendeteile({"name": "Mein Kleingewerbe", "strasse": "Werkweg 2",
    ...                  "plz": "20095", "ort": "Hamburg"})
    ['Mein Kleingewerbe', 'Werkweg 2', '20095 Hamburg']
    """
    ort = _text(f"{absender.get('plz') or ''} {absender.get('ort') or ''}")
    teile = (anzeigename(absender), _text(absender.get("strasse")), ort)
    return [teil for teil in teile if teil]


def ruecksendeangabe(absender: Mapping) -> str:
    """Die Absenderzeile ueber der Empfaengeranschrift, vollstaendig.

    Sie steht in der Zusatz- und Vermerkzone des Anschriftfelds (DIN 5008)
    und ist damit durch das Fenster lesbar -- der Weg, auf dem ein
    unzustellbarer Brief zurueckkommt. Deshalb wird nichts weggelassen.

    >>> ruecksendeangabe({"name": "Mein Kleingewerbe", "strasse": "Werkweg 2",
    ...                   "plz": "20095", "ort": "Hamburg"})
    'Mein Kleingewerbe · Werkweg 2 · 20095 Hamburg'
    >>> ruecksendeangabe({"name": "Webdesign Studio", "inhaber": "Erika Muster",
    ...                   "strasse": "Musterstraße 10", "plz": "20095", "ort": "Hamburg"})
    'Erika Muster · Webdesign Studio · Musterstraße 10 · 20095 Hamburg'
    """
    return " · ".join(ruecksendeteile(absender))


def steuerkennung(absender: Mapping, leistungsort: str | None = None) -> tuple[str, str] | None:
    """Die EINE Kennung, die gedruckt wird: (Bezeichnung, Wert) oder None.

    Gedruckt wird genau eine, auch wenn mehrere eingetragen sind -- vorher
    standen alle, und die Steuernummer sogar zweimal, auf jeder Rechnung.

    >>> steuerkennung({"steuernummer": "12/345/67890", "ust_idnr": "DE123456789"})
    ('USt-IdNr.', 'DE123456789')
    >>> steuerkennung({"steuernummer": "12/345/67890", "kleinunternehmer_idnr": "DE1-EX"})
    ('Kleinunternehmer-IdNr.', 'DE1-EX')
    >>> steuerkennung({"steuernummer": "12/345/67890", "kleinunternehmer_idnr": "DE1-EX"},
    ...               "EU_B2B_REVERSE_CHARGE")
    ('Steuernummer', '12/345/67890')
    >>> steuerkennung({"steuernummer": " ", "ust_idnr": None}) is None
    True
    """
    vorrang = VORRANG_REVERSE_CHARGE if leistungsort == "EU_B2B_REVERSE_CHARGE" else VORRANG
    for feld in vorrang:
        wert = _text(absender.get(feld))
        if wert:
            return KENNUNG_TEXT[feld], wert
    return None


def kontoinhaber(absender: Mapping) -> str:
    """Der Name unter der IBAN.

    Seit 9.10.2025 gleicht die Bank des Zahlers den Empfaengernamen mit der
    IBAN ab (Empfaengerueberpruefung, VO (EU) 2024/886) und warnt bei einer
    Abweichung. Ein Konto laeuft auf die Person, also der Inhaber; ohne ihn
    der Name des Betriebs.

    >>> kontoinhaber({"name": "Webdesign Studio", "inhaber": "Erika Muster"})
    'Erika Muster'
    >>> kontoinhaber({"name": "Mein Kleingewerbe", "inhaber": ""})
    'Mein Kleingewerbe'
    """
    return _text(absender.get("inhaber")) or _text(absender.get("name"))


def druckangaben(absender: Mapping, rechnung: Mapping) -> dict:
    """Alle Ableitungen fuer die Vorlage auf einmal."""
    teile = ruecksendeteile(absender)
    zeile = " · ".join(teile)
    kennung = steuerkennung(absender, rechnung.get("leistungsort"))
    name = anzeigename(absender)
    return {
        "name": name,
        "name_groesse": namensgroesse(name),
        "ruecksendeangabe": zeile,
        "ruecksendeangabe_teile": teile,
        "ruecksendeangabe_lang": len(zeile) > RUECKSENDE_EINZEILIG,
        "kennung_text": kennung[0] if kennung else None,
        "kennung_wert": kennung[1] if kennung else None,
        "kontoinhaber": kontoinhaber(absender),
    }

"""Stufe 3: Texterkennung mit Apple Vision -- lokal, kostenlos, offline.

Nur fuer Scans und Fotos. Bei E-Rechnungen und digital erzeugten
Lieferanten-PDFs ist Bilderkennung ueberfluessig, und bei E-Rechnungen sogar
unzulaessig: dort ist das XML massgebend.

Warum Apple Vision und keine eigene Pipeline
--------------------------------------------
Vision ist im Betriebssystem enthalten, kostet nichts, laeuft offline,
beherrscht Deutsch und ist bei Fotos stark, weil es dafuer gebaut wurde.
OCRmyPDF und IBMs Docling binden es als Engine ein. Eine eigene
Tesseract-Pipeline waere bei deutschen Belegen schlechter und braeuchte
dauerhafte Template-Pflege.

Datenschutzrechtlich ist die rein lokale Auslesung der einfachste Fall und
genau deshalb richtig: keine Auftragsverarbeitung, kein Vertrag, kein
Drittlandtransfer, keine Unterauftragsverarbeiter -- nichts davon in der
Verfahrensdokumentation. Die Belege verlassen den Rechner nicht, und ein
Schalter "nicht senden" eruebrigt sich, weil nie etwas gesendet wird.

⚠ Dieses Modul laeuft nur auf macOS
-----------------------------------
``ist_verfuegbar()`` sagt, ob diese Stufe hier etwas kann. Auf einem anderen
System meldet sie sich ab, **ohne den Import zu sprengen** -- sonst liesse sich
die Anwendung dort nicht einmal starten, und die uebrigen Stufen sind
plattformunabhaengig.

Die Abnahme dieser Stufe erfolgt auf dem Mac mit einem Handyfoto. Die Tests
arbeiten mit aufgezeichneten Vision-Ausgaben: sie pruefen den Weg von der
erkannten Textmenge zu den Feldern, nicht die Erkennung selbst. Das ist die
ehrliche Grenze -- eine Erkennung, die man nicht ausfuehren kann, kann man
auch nicht testen.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.logik.beleg import text_leser
from app.logik.beleg.ergebnis import BelegFehler, Vorschlag

QUELLE = "ocr_lokal"

#: Verfahren, wie es in extraktion.verfahren landet und damit in die
#: Verfahrensdokumentation. Ohne diesen Eintrag laesst sich spaeter nicht
#: sagen, womit ein archivierter Beleg gelesen wurde.
VERFAHREN = "Apple Vision (VNRecognizeTextRequest) via ocrmac"

#: Auflösung, mit der PDF-Seiten gerastert werden. 300 dpi ist der Wert, bei
#: dem Belegschrift zuverlaessig erkannt wird; darunter faellt vor allem das
#: Kleingedruckte aus, und genau dort stehen Steuersatz und USt-IdNr.
DPI = 300

#: Sprachen in der Reihenfolge, in der Vision sie versuchen soll. Deutsch
#: zuerst, Englisch dazu: die Haelfte der Belege eines IT-Dienstleisters ist
#: englisch (AWS, Figma, GitHub).
SPRACHEN = ("de-DE", "en-US")


@dataclass(frozen=True, slots=True)
class Verfuegbarkeit:
    moeglich: bool
    begruendung: str

    def __bool__(self) -> bool:
        return self.moeglich


def ist_verfuegbar() -> Verfuegbarkeit:
    """Kann diese Stufe auf diesem System etwas?

    Drei Bedingungen, einzeln gemeldet, damit die Oberflaeche sagen kann, was
    zu tun ist -- "nicht verfuegbar" allein hilft niemandem weiter.
    """
    if sys.platform != "darwin":
        return Verfuegbarkeit(
            False,
            "Apple Vision gibt es nur auf macOS. "
            "Scans und Fotos müssen hier manuell erfasst werden.",
        )
    try:
        import ocrmac  # noqa: F401
    except ImportError:
        return Verfuegbarkeit(
            False,
            "Das Paket ocrmac fehlt. Es ist die Brücke zu Apple Vision und wird mit "
            "'uv sync --extra ocr' installiert.",
        )
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return Verfuegbarkeit(
            False,
            "Das Paket pypdfium2 fehlt. Es rastert PDF-Seiten für die Erkennung und "
            "wird mit 'uv sync --extra ocr' installiert.",
        )
    return Verfuegbarkeit(True, f"Apple Vision einsatzbereit, {DPI} dpi, "
                                f"Sprachen {', '.join(SPRACHEN)}")


def _erkenne_bild(pfad: Path) -> str:
    """Text eines Bildes ueber Apple Vision."""
    from ocrmac import ocrmac as motor

    try:
        erkannt = motor.OCR(str(pfad), language_preference=list(SPRACHEN)).recognize()
    except Exception as fehler:  # noqa: BLE001 -- Vision meldet sehr verschiedene Fehler
        raise BelegFehler(f"Apple Vision konnte {pfad.name} nicht lesen: {fehler}") from fehler
    return zeilen_aus_kaesten(erkannt)


def zeilen_aus_kaesten(erkannt: list[tuple[str, float, tuple[float, float, float, float]]]) -> str:
    """Setzt Vision-Ergebnisse zu Textzeilen zusammen.

    Vision liefert **Kaesten in Lesereihenfolge des Erkenners**, nicht Zeilen:
    Betragswort und Betrag einer Rechnungszeile kommen als zwei getrennte
    Kaesten, oft weit auseinander in der Liste. Haengt man sie einfach mit
    Zeilenumbruechen zusammen, steht "Gesamtbetrag" allein auf einer Zeile und
    "8,29 EUR" auf einer anderen -- und dann findet ``finde_betrag`` nichts,
    weil es die Zeile als Einheit braucht.

    Deshalb wird nach der **vertikalen Lage** gruppiert: Kaesten, deren Mitte
    sich um weniger als die halbe Kastenhoehe unterscheidet, gehoeren auf eine
    Zeile und werden von links nach rechts sortiert.

    Der Kasten ist ``(x, y, breite, hoehe)`` in Anteilen der Bildgroesse, mit
    y von UNTEN gemessen -- so gibt Vision es heraus.

    >>> zeilen_aus_kaesten([("Gesamtbetrag", 0.9, (0.1, 0.50, 0.2, 0.02)),
    ...                     ("8,29 EUR",     0.9, (0.7, 0.502, 0.1, 0.02)),
    ...                     ("Rechnung",     0.9, (0.1, 0.80, 0.2, 0.02))])
    'Rechnung\\nGesamtbetrag 8,29 EUR'
    """
    if not erkannt:
        return ""
    kaesten = [(text, kasten) for text, _, kasten in erkannt if text.strip()]
    if not kaesten:
        return ""

    # Von oben nach unten: y wird von unten gemessen, also absteigend.
    kaesten.sort(key=lambda paar: (-(paar[1][1] + paar[1][3] / 2), paar[1][0]))

    zeilen: list[list[tuple[float, str]]] = []
    letzte_mitte: float | None = None
    for text, (x, y, _breite, hoehe) in kaesten:
        mitte = y + hoehe / 2
        toleranz = max(hoehe, 0.004) / 2
        if letzte_mitte is None or abs(mitte - letzte_mitte) > toleranz:
            zeilen.append([])
            letzte_mitte = mitte
        zeilen[-1].append((x, text.strip()))

    return "\n".join(
        " ".join(text for _, text in sorted(zeile)) for zeile in zeilen
    )


def _erkenne_pdf(pfad: Path) -> str:
    """Text eines PDF ohne Textschicht: Seiten rastern, dann erkennen."""
    import tempfile

    import pypdfium2 as pdfium

    try:
        dokument = pdfium.PdfDocument(str(pfad))
    except Exception as fehler:  # noqa: BLE001
        raise BelegFehler(f"Das PDF liess sich nicht rastern: {fehler}") from fehler

    teile = []
    with tempfile.TemporaryDirectory() as tmp:
        for nummer, seite in enumerate(dokument, start=1):
            bild = seite.render(scale=DPI / 72).to_pil()
            bildpfad = Path(tmp) / f"seite_{nummer}.png"
            bild.save(bildpfad)
            teile.append(_erkenne_bild(bildpfad))
    return "\n".join(teile)


def lies_datei(
    pfad: Path, *, heute: date | None = None, eigene_ust_idnr: str | None = None
) -> Vorschlag:
    """Erkennt einen Scan oder ein Foto und zieht die Felder heraus.

    Der Weg von Text zu Feldern ist derselbe wie bei der Textschicht -- nur
    die Herkunft unterscheidet sich. Deshalb wird ``text_leser.lies_text``
    benutzt und nichts davon hier wiederholt: zwei Auswertungen waeren zwei
    Verhaltensweisen, und die Abweichung faende niemand.

    **Alle Felder gelten hier als unsicher**, auch die, bei denen die
    Arithmetik aufgeht. Eine Bilderkennung kann ein Zeichen verlesen, und bei
    Betraegen ist ein verlesenes Zeichen ein anderer Betrag. Die aufgehende
    Arithmetik senkt das Risiko erheblich, beseitigt es aber nicht -- und die
    Pruefmaske soll das zeigen.
    """
    stand = ist_verfuegbar()
    if not stand:
        raise BelegFehler(stand.begruendung)

    endung = pfad.suffix.lower()
    if endung == ".pdf":
        text = _erkenne_pdf(pfad)
    elif endung in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp"):
        text = _erkenne_bild(pfad)
    else:
        raise BelegFehler(f"{pfad.suffix} ist kein Format, das erkannt werden kann.")

    if not text.strip():
        raise BelegFehler(
            f"In {pfad.name} wurde kein Text erkannt. Bei einem Foto hilft meist mehr "
            "Licht und ein gerader Blickwinkel; sonst manuell erfassen."
        )
    return _aus_text(text, heute=heute, eigene_ust_idnr=eigene_ust_idnr)


def _aus_text(
    text: str, *, heute: date | None = None, eigene_ust_idnr: str | None = None
) -> Vorschlag:
    """Felder aus erkanntem Text. Getrennt, damit die Tests sie fuettern koennen."""
    vorschlag = text_leser.lies_text(
        text, quelle=QUELLE, heute=heute, eigene_ust_idnr=eigene_ust_idnr
    )
    from dataclasses import replace

    unsicher = {name: replace(fund, sicher=False) for name, fund in vorschlag.felder.items()}
    return Vorschlag(
        QUELLE, unsicher,
        vorschlag.meldungen + (
            "Aus einer Bilderkennung gelesen — jedes Feld bitte am Beleg prüfen. "
            "Ein verlesenes Zeichen ist bei einem Betrag ein anderer Betrag.",
        ),
        vorschlag.bedenken,
    )

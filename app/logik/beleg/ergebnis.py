"""Was eine Auslesestufe zurueckgibt.

Gemeinsam fuer alle Stufen, damit die Pruefmaske **je Feld** zeigen kann, woher
der Wert kommt. Das ist nicht Kosmetik: nach GoBD Rz. 130 ist das bestaetigte
Ergebnis einer Auslesung selbst aufbewahrungs- und vorlagepflichtig. Wer
bestaetigt, haftet fuer die Zahl -- und kann das nur, wenn er sieht, ob sie aus
einem XML-Feld, aus einer Textzeile oder aus einer Bilderkennung kommt.

Deshalb traegt **jeder einzelne Wert** seine Herkunft, nicht der Beleg als
Ganzes: ein ZUGFeRD-PDF liefert die Betraege exakt aus dem XML, die Kategorie
aber kommt immer vom Menschen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Die Stufen der Auslesung, in der Reihenfolge ihrer Verbindlichkeit. Die
#: Reihenfolge ist durch Recht vorgegeben: ist XML vorhanden, ist es massgebend
#: und eine Bilderkennung des Bildteils unzulaessig.
QUELLEN = ("xml", "pdf_text", "ocr_lokal", "email", "manuell")

#: Die Felder, die ein Beleg braucht. Als Konstante, damit Pruefmaske,
#: Lernspeicher und Validierung dieselbe Liste benutzen.
FELDER = (
    "lieferant",
    "lieferant_ust_idnr",
    "belegnummer",
    "belegdatum",
    "waehrung",
    "brutto",
    "netto",
    "ust",
    "ust_satz_promille",
    "reverse_charge",
    "zahlungsdatum",
    # Vermerk zum Beleg, etwa "teilweise mit Guthaben bezahlt" -- wandert in
    # die Notiz.
    "notiz",
)


def pdf_fehlertext(fehler: BaseException) -> str:
    """Warum sich ein PDF nicht lesen liess -- auf Deutsch, mit einem Weg.

    pypdf und pdfminer melden auf Englisch ("File has not been decrypted",
    "startxref not found"). Ein Nicht-Entwickler erfaehrt daraus nicht, dass
    das PDF ein Passwort hat. Unterschieden werden die zwei Faelle, die etwas
    anderes verlangen: Passwort und beschaedigte Datei.

    >>> pdf_fehlertext(Exception("File has not been decrypted"))[:42]
    'Das PDF ist mit einem Passwort geschützt —'
    >>> pdf_fehlertext(Exception("startxref not found"))[:44]
    'Das PDF ist beschädigt oder unvollständig — '
    """
    kette = []
    stelle: BaseException | None = fehler
    while stelle is not None and len(kette) < 5:
        kette.append(f"{type(stelle).__name__} {stelle!r}".lower())
        stelle = stelle.__cause__ or stelle.__context__
    text = " ".join(kette)
    if any(wort in text for wort in ("decrypt", "password", "encrypt")):
        return ("Das PDF ist mit einem Passwort geschützt — ohne Passwort speichern (in der "
                "Vorschau öffnen, Ablage → Exportieren, „Verschlüsseln“ aus) oder in der "
                "Prüfmaske von Hand erfassen.")
    return ("Das PDF ist beschädigt oder unvollständig — bitte die Datei noch einmal aus der "
            "Mail sichern oder in der Prüfmaske von Hand erfassen.")


class BelegFehler(Exception):
    """Harte Fehler der Auslesung.

    Insbesondere: ein PDF mit eingebettetem XML, das sich nicht parsen laesst.
    Dann folgt ein Abbruch mit Journaleintrag und **niemals** ein Rueckfall auf
    Bilderkennung -- bei einer E-Rechnung ist das XML massgebend, und die
    Auslesung des Bildteils ist unzulaessig.
    """


@dataclass(frozen=True, slots=True)
class Fund:
    """Ein Feldwert mit seiner Herkunft.

    ``sicher`` heisst: der Wert darf ohne Rueckfrage vorbelegt werden. Nur die
    XML-Stufe und eine aufgehende arithmetische Kreuzpruefung setzen das.
    Bestaetigen muss der Nutzer trotzdem -- ``sicher`` entscheidet nur, ob das
    Feld in der Maske gelb markiert ist.
    """

    wert: object
    quelle: str
    nachweis: str = ""
    sicher: bool = True

    def __post_init__(self) -> None:
        if self.quelle not in QUELLEN:
            raise ValueError(f"Unbekannte Quelle: {self.quelle}")
        if isinstance(self.wert, str):
            object.__setattr__(self, "wert", ohne_steuerzeichen(self.wert))


def ohne_steuerzeichen(text: str) -> str:
    r"""Steuerzeichen aus einem ausgelesenen Text, ersetzt durch Leerzeichen.

    Die Textschicht eines PDFs liefert Seitenvorschub und Trenner mit
    ("Hetzner\x0cOnline"). Landete das in der Pruefmaske, wiese das Absenden
    sie ab: die App nimmt keine Formulare mit unsichtbaren Steuerzeichen an.
    Tab und Zeilenumbruch bleiben, sie sind in mehrzeiligen Feldern gewollt.

    >>> ohne_steuerzeichen("Hetzner\x0cOnline GmbH"), ohne_steuerzeichen("a\x00b")
    ('Hetzner Online GmbH', 'a b')
    >>> ohne_steuerzeichen("Zeile 1\nZeile 2\tx"), ohne_steuerzeichen("a\x0c\x0c b")
    ('Zeile 1\nZeile 2\tx', 'a b')
    """
    sauber = "".join(
        " " if (ord(z) < 32 and z not in "\t\n\r") or ord(z) == 127 else z
        for z in text
    )
    # Mehrere ersetzte Zeichen hintereinander ergeben ein Leerzeichen.
    return re.sub(r"(?<=\S) {2,}(?=\S)", " ", sauber) if sauber != text else text


@dataclass(frozen=True, slots=True)
class Vorschlag:
    """Das Ergebnis einer Stufe: gefundene Felder plus was aufgefallen ist."""

    quelle: str
    felder: dict[str, Fund] = field(default_factory=dict)
    #: Was der Nutzer wissen muss, bevor er bestaetigt. Keine Fehler -- die
    #: fliegen als BelegFehler.
    meldungen: tuple[str, ...] = ()
    #: Was gegen ein Buchen OHNE Pruefmaske spricht, obwohl die Felder sicher
    #: sind: das Dokument ist keine gewoehnliche Rechnung (Gutschrift im Text,
    #: Korrektur, Anzahlung), oder seine Summen gehen nicht auf. Die Pruefmaske
    #: zeigt die Meldungen; das Abholen aus dem Eingangsordner bucht dann nicht
    #: selbst (app/logik/beleg/abholen.py).
    bedenken: tuple[str, ...] = ()

    def wert(self, feld: str, ersatz: object = None) -> object:
        fund = self.felder.get(feld)
        return ersatz if fund is None else fund.wert

    @property
    def unsichere_felder(self) -> tuple[str, ...]:
        return tuple(name for name, fund in self.felder.items() if not fund.sicher)

    @property
    def fehlende_felder(self) -> tuple[str, ...]:
        """Was fuer einen buchbaren Beleg noch fehlt.

        Absichtlich eine kurze Liste: Lieferant, Datum und Bruttobetrag
        genuegen, um eine Betriebsausgabe zu buchen. Netto und Steuersatz sind
        bei einem Kleinunternehmer ohne Vorsteuerabzug Zusatzinformation.
        """
        pflicht = ("lieferant", "belegdatum", "brutto")
        return tuple(f for f in pflicht
                     if f not in self.felder or self.felder[f].wert in (None, ""))

    def ergaenzt_um(self, andere: Vorschlag) -> Vorschlag:
        """Nimmt Felder aus ``andere`` auf, die hier noch fehlen.

        Vorhandene Werte werden NICHT ueberschrieben: die erste Stufe, die
        etwas gefunden hat, ist die verbindlichere.
        """
        zusammen = dict(andere.felder)
        zusammen.update(self.felder)
        return Vorschlag(
            quelle=self.quelle,
            felder=zusammen,
            meldungen=self.meldungen + andere.meldungen,
            bedenken=self.bedenken + andere.bedenken,
        )

"""Abrechnungsrundung und Betragsberechnung.

Die Rechenreihenfolge ist verbindlich und der Grund, warum dieses Modul
ueberhaupt existiert. Gerundet wird auf der SUMME je Rechnungsposition, niemals
pro Zeiteintrag:

    sieben Eintraege a 7 Minuten sind exakt 49 Minuten
      pro Eintrag aufwaerts gerundet -> 7 x 15 = 105 Minuten  (+114 %)
      auf die Summe kaufmaennisch    -> 45 Minuten

Der erste Weg ist der klassische Eigenbau-Fehler und direkt Streitstoff mit
Kunden. Geld ist durchgaengig Integer-Cent, Dauer Integer-Minuten, Menge
Integer-Tausendstel. Floats kommen nicht vor.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Final

AUFWAERTS: Final = "AUFWAERTS"
KAUFMAENNISCH: Final = "KAUFMAENNISCH"
ABWAERTS: Final = "ABWAERTS"
KEINE: Final = "KEINE"

MODI: Final = (AUFWAERTS, KAUFMAENNISCH, ABWAERTS, KEINE)

# Menge wird in Tausendsteln gespeichert, aber auf zwei Dezimalen gerundet.
# So bleibt "Menge x Einzelpreis = Betrag" auf der Rechnung mit dem
# Taschenrechner nachrechenbar -- 0,75 h x 80,00 € = 60,00 €.
_TAUSEND: Final = Decimal(1000)
_ZWEI_DEZIMALEN: Final = Decimal("0.01")


def runde_minuten(
    summe_minuten: int,
    intervall_min: int,
    modus: str = KAUFMAENNISCH,
    mindest_minuten: int = 0,
) -> int:
    """Rundet eine Minutensumme auf ein Vielfaches des Intervalls.

    ``mindest_minuten`` greift nach der Rundung. Bei ``ABWAERTS`` kann das
    Ergebnis 0 werden (49 Minuten abwaerts auf 60er-Intervall); das ist
    gewollt und wird von der Validierung als Position ohne Menge gemeldet,
    nicht hier stillschweigend korrigiert.
    """
    if modus not in MODI:
        raise ValueError(f"Unbekannter Rundungsmodus: {modus!r}")
    if summe_minuten < 0:
        raise ValueError("Negative Dauer ist nicht vorgesehen")
    if summe_minuten == 0:
        return max(0, mindest_minuten)
    if modus == KEINE or intervall_min <= 1:
        gerundet = summe_minuten
    else:
        einheiten = Decimal(summe_minuten) / Decimal(intervall_min)
        rundung = {
            AUFWAERTS: ROUND_CEILING,
            ABWAERTS: ROUND_FLOOR,
            KAUFMAENNISCH: ROUND_HALF_UP,
        }[modus]
        gerundet = int(einheiten.to_integral_value(rounding=rundung)) * intervall_min
    return max(gerundet, mindest_minuten)


def menge_tausendstel_aus_minuten(minuten: int) -> int:
    """Rechnet Minuten in Dezimalstunden um, auf zwei Dezimalen, als Tausendstel.

    45 Minuten -> 0,75 h -> 750. 49 Minuten -> 0,82 h -> 820.
    """
    if minuten < 0:
        raise ValueError("Negative Dauer ist nicht vorgesehen")
    stunden = (Decimal(minuten) / Decimal(60)).quantize(
        _ZWEI_DEZIMALEN, rounding=ROUND_HALF_UP
    )
    return int(stunden * _TAUSEND)


def betrag_cent(menge_tausendstel: int, einzelpreis_cent: int) -> int:
    """Menge x Einzelpreis, kaufmaennisch auf ganze Cent."""
    produkt = Decimal(menge_tausendstel) * Decimal(einzelpreis_cent) / _TAUSEND
    return int(produkt.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class Positionsrechnung:
    """Das Ergebnis der Rundung fuer genau eine Rechnungsposition."""

    roh_minuten: int
    gerundete_minuten: int
    menge_tausendstel: int
    einzelpreis_cent: int
    betrag_cent: int

    @property
    def rundungsdifferenz_minuten(self) -> int:
        return self.gerundete_minuten - self.roh_minuten

    @property
    def menge_stunden_text(self) -> str:
        """Menge als deutsche Dezimalzahl, z. B. '0,75'."""
        stunden = Decimal(self.menge_tausendstel) / _TAUSEND
        return f"{stunden:.2f}".replace(".", ",")


def berechne_position(
    roh_minuten: int | list[int],
    einzelpreis_cent: int,
    *,
    intervall_min: int = 15,
    modus: str = KAUFMAENNISCH,
    mindest_minuten: int = 0,
) -> Positionsrechnung:
    """Fuehrt die verbindliche Rechenreihenfolge fuer eine Position aus.

    1. Rohdauern summieren
    2. Rundung auf das Intervall anwenden
    3. Mindestdauer anwenden
    4. in Dezimalstunden umrechnen, zwei Dezimalen
    5. Betrag = Menge x Einzelpreis
    """
    summe = sum(roh_minuten) if isinstance(roh_minuten, list) else roh_minuten
    gerundet = runde_minuten(summe, intervall_min, modus, mindest_minuten)
    menge = menge_tausendstel_aus_minuten(gerundet)
    return Positionsrechnung(
        roh_minuten=summe,
        gerundete_minuten=gerundet,
        menge_tausendstel=menge,
        einzelpreis_cent=einzelpreis_cent,
        betrag_cent=betrag_cent(menge, einzelpreis_cent),
    )


#: Gruppierung, bei der jede Zeit eine eigene Position ist. Dort wird auf die
#: Minute gerechnet (gruppierung.py): eine Intervallrundung je Eintrag waere
#: genau die Zeittaktklausel je Taetigkeit, die vor Gericht nicht haelt. Als
#: Text und nicht aus gruppierung importiert -- das Modul importiert dieses.
_PRO_EINTRAG: Final = "PRO_EINTRAG"


@dataclass(frozen=True, slots=True)
class Regel:
    """Eine Rundungsregel: wie die Zeitsumme JE POSITION gerundet wird.

    ``quelle`` sagt, woher sie kommt: KUNDE (am Kunden eingestellt), BETRIEB
    (Vorgabe aus den Einstellungen) oder ENTWURF (beim Anlegen eines
    Entwurfs eingefroren). ``grundlage`` ist die Stelle, an der die Rundung
    mit dem Kunden vereinbart ist, etwa „Angebot vom 03.02.2026“.
    """

    modus: str
    intervall_min: int
    quelle: str = "BETRIEB"
    grundlage: str | None = None

    @property
    def rundet(self) -> bool:
        """Veraendert die Regel ueberhaupt etwas? Bei KEINE oder Takt 1 nicht."""
        return self.modus != KEINE and self.intervall_min > 1

    def wirkt(self, gruppierung: str) -> bool:
        """Wird mit dieser Gruppierung tatsaechlich gerundet?

        "Pro Eintrag" rechnet immer auf die Minute -- die Regel greift dann nicht.
        """
        return self.rundet and gruppierung != _PRO_EINTRAG

    def gleich(self, andere: Regel) -> bool:
        """Rechnen beide gleich? Die Herkunft zaehlt dabei nicht."""
        if not self.rundet and not andere.rundet:
            return True
        return (self.modus, self.intervall_min) == (andere.modus, andere.intervall_min)

    @property
    def text(self) -> str:
        """Die Regel in Worten, z. B. „auf volle 15 Minuten aufgerundet“.

        >>> Regel(AUFWAERTS, 15).text, Regel(KEINE, 15).text
        ('auf volle 15 Minuten aufgerundet', 'minutengenau')
        """
        if not self.rundet:
            return "minutengenau"
        takt = f"volle {self.intervall_min} Minuten"
        return {
            AUFWAERTS: f"auf {takt} aufgerundet",
            KAUFMAENNISCH: f"kaufmännisch auf {takt} gerundet",
            ABWAERTS: f"auf {takt} abgerundet",
        }[self.modus]

    @property
    def kurz(self) -> str:
        """Die Regel als Stichwort fuer Auswahllisten, z. B. „15 Min. aufwärts“.

        >>> Regel(AUFWAERTS, 15).kurz, Regel(KEINE, 15).kurz
        ('15 Min. aufwärts', 'minutengenau')
        """
        if not self.rundet:
            return "minutengenau"
        art = {AUFWAERTS: "aufwärts", KAUFMAENNISCH: "kaufmännisch", ABWAERTS: "abwärts"}
        return f"{self.intervall_min} Min. {art[self.modus]}"

    def abrechnung(self, gruppierung: str) -> str:
        """Was mit erfasster Zeit auf der Rechnung geschieht -- als Satzteil.

        >>> Regel(AUFWAERTS, 15).abrechnung("PRO_TITEL")
        'abgerechnet wird je Rechnungsposition auf volle 15 Minuten aufgerundet'
        >>> Regel(AUFWAERTS, 15).abrechnung("PRO_EINTRAG")
        'abgerechnet wird minutengenau'
        """
        if not self.wirkt(gruppierung):
            return "abgerechnet wird minutengenau"
        return f"abgerechnet wird je Rechnungsposition {self.text}"

    def hinweis(self, gruppierung: str) -> str | None:
        """Der Satz fuer die Rechnung -- oder None, wenn nicht gerundet wurde.

        Er beschreibt, was tatsaechlich geschah: die SUMME je Position ist
        gerundet. „Abrechnung in angefangenen 15 Minuten“ behauptete dagegen
        eine Zeittaktklausel je Taetigkeit, die so nie stattfand -- und genau
        die halten die Gerichte fuer unwirksam. Ohne Rundung (Regel KEINE oder
        Gruppierung „pro Eintrag“) steht kein Rundungssatz auf dem Beleg.

        >>> Regel(AUFWAERTS, 15).hinweis("PRO_TITEL")
        'Die Zeitsumme je Position ist auf volle 15 Minuten aufgerundet.'
        >>> Regel(AUFWAERTS, 15).hinweis("PRO_EINTRAG") is None
        True
        """
        if not self.wirkt(gruppierung):
            return None
        return f"Die Zeitsumme je Position ist {self.text}."


def pauschalposition(betrag_in_cent: int, menge_tausendstel: int = 1000) -> Positionsrechnung:
    """Pauschale: der Festpreis gilt, die Dauer ist fuer den Betrag irrelevant.

    Uebernommen aus der Praxis etablierter Zeiterfassungen: ein Festpreis
    schlaegt immer den Stundensatz.
    """
    return Positionsrechnung(
        roh_minuten=0,
        gerundete_minuten=0,
        menge_tausendstel=menge_tausendstel,
        einzelpreis_cent=betrag_in_cent,
        betrag_cent=betrag_cent(menge_tausendstel, betrag_in_cent),
    )


def rechnungssumme_cent(positionen: list[Positionsrechnung]) -> int:
    """Summe der Positionsbetraege.

    Nie aus Gesamtstunden x Satz neu berechnen -- das weicht von der Summe der
    gerundeten Positionen ab und die Rechnung stimmt dann in sich nicht.
    """
    return sum(p.betrag_cent for p in positionen)


def euro(cent: int) -> str:
    """Cent als deutscher Betrag, z. B. 123456 -> '1.234,56'."""
    vorzeichen = "-" if cent < 0 else ""
    cent = abs(cent)
    ganz, rest = divmod(cent, 100)
    return f"{vorzeichen}{ganz:,}".replace(",", ".") + f",{rest:02d}"

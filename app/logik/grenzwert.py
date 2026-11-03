"""Ueberwachung der Kleinunternehmergrenzen nach § 19 UStG.

Zwei Dinge sind hier leicht falsch zu machen, und beide kosten Geld:

1. **Bemessungsgrundlage.** § 19 Abs. 2 Satz 1 UStG: der Gesamtumsatz ist die
   "nach vereinnahmten Entgelten berechnete Summe" der steuerbaren Umsaetze.
   Gezaehlt werden also ZAHLUNGSEINGAENGE, nicht Rechnungsausgaenge -- dieselbe
   Basis wie EUeR-Kennzahl 111. Ein rechnungsbasierter Zaehler zeigt zu viel,
   solange Kunden offen sind, und meldet die Grenze zum falschen Zeitpunkt.
   Anzahlungen zaehlen im Zeitpunkt ihrer Vereinnahmung.

2. **Welche Grenze ueberhaupt gilt.** Im Jahr der Betriebseroeffnung gilt NICHT
   die 100.000-€-Grenze, sondern 25.000 € im laufenden Jahr. Ein Tool, das nur
   gegen 100.000 € prueft, lullt einen Gruender genau dann ein, wenn es teuer
   wird.

Nicht steuerbare Auslandsumsaetze (§ 3a Abs. 2 UStG, EU-B2B mit Reverse Charge)
zaehlen NICHT in den Gesamtumsatz, sind aber sehr wohl EUeR-Betriebseinnahme.
Dasselbe gilt fuer Drittlandsumsaetze: ob USA, UK oder Oesterreich, entscheidend
ist allein, dass der Leistungsort nicht im Inland liegt (§ 19 Abs. 2 i. V. m.
§ 1 Abs. 1 Nr. 1 UStG).
Umsaetze mit Wirtschaftsguetern des Anlagevermoegens bleiben ebenfalls ausser
Ansatz (§ 19 Abs. 2 Satz 2 UStG).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

GRENZE_VORJAHR_CENT = 2_500_000      # 25.000 €
GRENZE_LAUFEND_CENT = 10_000_000     # 100.000 €

#: Ab diesem Anteil der geltenden Grenze wird gewarnt.
WARNSCHWELLE = 0.8
#: Ab diesem Anteil wird deutlich gewarnt.
WARNSCHWELLE_HOCH = 0.95


@dataclass(frozen=True, slots=True)
class Grenzstand:
    jahr: int
    vereinnahmt_cent: int
    vorjahr_cent: int
    gruendungsjahr: bool
    grenze_cent: int
    vorjahr_erfasst: bool
    #: Ist der Beginn der Taetigkeit ueberhaupt eingetragen? Ohne ihn laesst
    #: sich nicht sagen, ob das laufende Jahr das Gruendungsjahr ist -- die
    #: Anzeige rechnet dann mit 100.000 € und muss dazusagen, dass das eine
    #: Annahme ist. Stillschweigend den Installationstag als Betriebsbeginn zu
    #: nehmen (so war es), machte aus einem Altbetrieb einen Gruender und aus
    #: 100.000 € Grenze 25.000.
    beginn_bekannt: bool = True

    @property
    def anteil(self) -> float:
        return self.vereinnahmt_cent / self.grenze_cent if self.grenze_cent else 0.0

    @property
    def negativ(self) -> bool:
        """Mehr zurueckgezahlt als eingenommen.

        Das gibt es: eine Zahlung am 31. Dezember, Storno und Rueckzahlung am
        2. Januar -- im neuen Jahr steht dann ein Minus. Sachlich ist das eine
        Minderung der Bemessungsgrundlage im Jahr der Rueckzahlung (§ 17 Abs. 1
        UStG sinngemaess); die Anzeige muss es nur benennen statt eine nackte
        Minuszahl und einen Balken negativer Breite zu zeigen.
        """
        return self.vereinnahmt_cent < 0

    @property
    def balken_prozent(self) -> float:
        """Breite des Fortschrittsbalkens, auf 0 bis 100 gekappt.

        >>> Grenzstand(2027, -50000, 0, False, 10_000_000, True).balken_prozent
        0.0
        >>> Grenzstand(2026, 12_000_000, 0, False, 10_000_000, True).balken_prozent
        100.0
        >>> Grenzstand(2026, 2_500_000, 0, False, 10_000_000, True).balken_prozent
        25.0
        """
        return round(min(100.0, max(0.0, self.anteil * 100)), 1)

    @property
    def ueberschritten(self) -> bool:
        return self.vereinnahmt_cent > self.grenze_cent

    @property
    def vorjahr_ueberschritten(self) -> bool:
        return self.vorjahr_cent > GRENZE_VORJAHR_CENT

    @property
    def stufe(self) -> str:
        """gruen | gelb | orange | rot -- fuer die Anzeige."""
        if self.ueberschritten:
            return "rot"
        if self.anteil >= WARNSCHWELLE_HOCH:
            return "orange"
        if self.anteil >= WARNSCHWELLE:
            return "gelb"
        return "gruen"

    @property
    def rest_cent(self) -> int:
        return max(0, self.grenze_cent - self.vereinnahmt_cent)


def vereinnahmt_cent(conn: sqlite3.Connection, jahr: int) -> int:
    """Im Inland steuerbare, vereinnahmte Entgelte eines Kalenderjahres.

    Grundlage sind Zahlungen, nicht Rechnungen. Ausgeschlossen: Rechnungen mit
    Leistungsort ausserhalb des Inlands (nicht steuerbar) und Belege ohne
    Entgeltcharakter (Leistungsnachweise).

    Massgeblich ist ``rechnung.leistungsort``, der SCHNAPPSCHUSS, nicht der
    Kundenstamm. Der Unterschied ist nicht theoretisch: zoege ein Kunde von
    Salzburg nach Hamburg, wuerde ein Blick in den Stamm fuenf Jahre
    oesterreichischen Umsatz rueckwirkend in den § 19-Gesamtumsatz holen -- und
    damit womoeglich eine Grenze reissen, die damals eingehalten war. Was ein
    abgeschlossenes Jahr war, entscheidet der Beleg, nicht die heutige Adresse.
    """
    zeile = conn.execute(
        """
        SELECT COALESCE(SUM(z.betrag_cent), 0) AS cent
          FROM zahlung z
          JOIN rechnung r ON r.id = z.rechnung_id
         WHERE strftime('%Y', z.datum) = ?
           AND r.typ IN ('RECHNUNG', 'STORNO')
           AND r.leistungsort = 'INLAND'
        """,
        (f"{jahr:04d}",),
    ).fetchone()
    # Dazu die Einnahmen ohne eigene Rechnung, soweit ihre Kategorie im
    # Inland steuerbar ist (app/logik/einnahmen.py): eine Affiliate-Provision
    # eines inlaendischen Partnerprogramms zaehlt, die Auszahlung einer
    # auslaendischen Plattform, ein Anlagenverkauf und eine Erstattung nicht.
    from app.logik.einnahmen import zaehlt_zu_19_cent

    return int(zeile["cent"]) + zaehlt_zu_19_cent(conn, jahr)


def stand(conn: sqlite3.Connection, jahr: int) -> Grenzstand:
    """Der aktuelle Stand gegen die im Jahr ``jahr`` geltende Grenze."""
    betrieb = conn.execute("SELECT beginn_am FROM betrieb WHERE id = 1").fetchone()
    beginn = betrieb["beginn_am"] if betrieb else None
    beginn_bekannt = bool(beginn)
    gruendungsjahr = beginn_bekannt and beginn[:4] == f"{jahr:04d}"

    # Im Gruendungsjahr ist die laufende Grenze 25.000 €, nicht 100.000 €.
    grenze = GRENZE_VORJAHR_CENT if gruendungsjahr else GRENZE_LAUFEND_CENT

    vorjahr = vereinnahmt_cent(conn, jahr - 1)
    # Ohne Zahlungen im Vorjahr laesst sich nicht sagen, ob es keinen Umsatz gab
    # oder das Tool damals nicht benutzt wurde. Das muss die Anzeige sagen,
    # statt eine Null zu behaupten.
    vorjahr_erfasst = gruendungsjahr or bool(
        conn.execute(
            # Die Zahlung eines geloeschten Belegs (Testbeleg) zeigt nicht,
            # dass das Tool im Vorjahr benutzt wurde.
            "SELECT 1 FROM zahlung WHERE strftime('%Y', datum) = ?"
            " AND (beleg_id IS NULL OR beleg_id IN (SELECT id FROM v_beleg_gueltig))"
            " UNION ALL SELECT 1 FROM v_einnahme_gueltig WHERE strftime('%Y', zufluss_am) = ?"
            " LIMIT 1",
            (f"{jahr - 1:04d}", f"{jahr - 1:04d}"),
        ).fetchone()
    )

    return Grenzstand(
        jahr=jahr,
        vereinnahmt_cent=vereinnahmt_cent(conn, jahr),
        vorjahr_cent=vorjahr,
        gruendungsjahr=gruendungsjahr,
        grenze_cent=grenze,
        vorjahr_erfasst=vorjahr_erfasst,
        beginn_bekannt=beginn_bekannt,
    )

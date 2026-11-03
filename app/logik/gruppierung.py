"""Zeiteintraege in Rechnungspositionen uebersetzen.

Das ist die Stelle, an der heute manuell gearbeitet wird, und deshalb die, an
der es Auswahl geben muss. Vier Modi, dazu zwei Regeln, die nicht verhandelbar
sind:

* **Gerundet wird je erzeugter Position**, nie je Zeiteintrag. Deshalb kann die
  Rechnungssumme zwischen den Modi leicht abweichen -- die Vorschau zeigt das,
  damit es keine Ueberraschung ist.
* **Pauschalen und Kulanzzeilen werden nie mit Stundenpositionen verschmolzen.**
  Sonst stimmt "Menge x Einzelpreis = Betrag" auf dem Papier nicht mehr.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from app.logik import rundung

SAMMEL = "SAMMEL"
PRO_TITEL = "PRO_TITEL"
PRO_TAG = "PRO_TAG"
PRO_EINTRAG = "PRO_EINTRAG"

MODI: tuple[str, ...] = (SAMMEL, PRO_TITEL, PRO_TAG, PRO_EINTRAG)

MODUS_TEXT = {
    SAMMEL: "eine Sammelposition",
    PRO_TITEL: "pro Titel",
    PRO_TAG: "pro Tag",
    PRO_EINTRAG: "pro Eintrag",
}

MONATE = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)

#: Laenge, auf die ZUSAMMENGEFASSTE Beschreibungen gekuerzt werden. Eine
#: Position aus genau einem Eintrag traegt ihre Beschreibung ungekuerzt; die
#: Einzelheiten aller Eintraege stehen im Einzelnachweis.
UNTERTEXT_MAX = 240


@dataclass(slots=True)
class Position:
    """Eine Rechnungsposition, noch nicht gespeichert."""

    bezeichnung: str
    menge_tausendstel: int
    einzelpreis_cent: int
    betrag_cent: int
    quelle: str                      # ZEIT | PAUSCHAL | FREI
    untertext: str | None = None
    einheit: str = "HUR"
    nachlass_cent: int = 0
    nachlass_grund: str | None = None
    darstellung: str = "BETRAG"
    zeiteintrag_ids: list[int] = field(default_factory=list)
    roh_minuten: int = 0
    gerundete_minuten: int = 0

    @property
    def ist_kulanz(self) -> bool:
        return self.nachlass_cent > 0 and self.betrag_cent == 0

    @property
    def menge_text(self) -> str:
        return f"{self.menge_tausendstel / 1000:.2f}".replace(".", ",")


def _kuerzen(text: str, grenze: int) -> str:
    """Kuerzt an einer Wort- oder Zeilengrenze und haengt „…“ an.

    Mitten im Wort abgeschnitten las sich der Rest als Tippfehler
    („Lorem ip…“). Ein einzelnes Wort, laenger als die Grenze, wird
    notgedrungen doch geteilt.

    >>> _kuerzen("Konzept erstellt; Abstimmung mit dem Kunden", 30)
    'Konzept erstellt; Abstimmung…'
    """
    if len(text) <= grenze:
        return text
    rumpf = text[: grenze - 1]
    trenner = max(rumpf.rfind(" "), rumpf.rfind("\n"))
    if trenner > grenze // 2:
        rumpf = rumpf[:trenner]
    return rumpf.rstrip(" ;,.:\n") + "…"


def _untertext(eintraege: Sequence[dict]) -> str | None:
    """Die unterschiedlichen Beschreibungen einer Position.

    Aus genau EINEM Eintrag: die Beschreibung ungekuerzt -- bei „pro
    Eintrag“ sieht der Kunde so alles, was erfasst wurde. Aus mehreren:
    entdoppelt und an der Wortgrenze gekuerzt, damit die Sammelposition
    lesbar bleibt; die Einzelheiten stehen im Einzelnachweis. Mehrzeilige
    Beschreibungen werden zeilenweise getrennt statt mit „; “ verklebt --
    sonst stand „Zeile 2; Nächster Eintrag“ auf dem Papier.
    """
    gesehen: list[str] = []
    for eintrag in eintraege:
        text = (eintrag.get("beschreibung") or "").strip()
        if text and text not in gesehen:
            gesehen.append(text)
    if not gesehen:
        return None
    if len(eintraege) == 1:
        return gesehen[0]
    trenner = "\n" if any("\n" in text for text in gesehen) else "; "
    return _kuerzen(trenner.join(gesehen), UNTERTEXT_MAX)


def _satz(eintrag: dict, standard_cent: int) -> int:
    return int(eintrag.get("satz_cent_snapshot") or standard_cent)


def _zeitposition(
    eintraege: Sequence[dict],
    bezeichnung: str,
    *,
    standard_cent: int,
    intervall_min: int,
    modus_rundung: str,
    kulanz: bool,
) -> Position:
    """Fasst Stundeneintraege zu einer Position zusammen und rundet auf die Summe."""
    satz = _satz(eintraege[0], standard_cent)
    roh = [int(e["dauer_minuten"]) for e in eintraege]
    gerechnet = rundung.berechne_position(
        roh, satz, intervall_min=intervall_min, modus=modus_rundung
    )
    # Sicherheitsnetz: die Rundung kann eine Position auf null bringen -- sieben
    # Minuten kaufmaennisch auf ein 15er-Intervall ergeben 0. Eine Position ohne
    # Menge ist nach § 34a Satz 1 Nr. 4 UStDV unzulaessig (die Datenbank weist
    # sie auch zurueck), und eine Rechnung ueber 0,00 EUR waere das Ergebnis.
    # Dann gilt die Rohzeit, und die Pruefung meldet die Abweichung.
    if gerechnet.menge_tausendstel <= 0 and sum(roh) > 0:
        gerechnet = rundung.berechne_position(
            roh, satz, intervall_min=1, modus=rundung.KEINE
        )

    nachlass = gerechnet.betrag_cent if kulanz else 0
    gruende = [
        (e.get("grund") or "").strip() for e in eintraege if (e.get("grund") or "").strip()
    ]
    return Position(
        bezeichnung=bezeichnung,
        untertext=_untertext(eintraege),
        menge_tausendstel=gerechnet.menge_tausendstel,
        einzelpreis_cent=satz,
        nachlass_cent=nachlass,
        # Eine Zeile ohne Betrag braucht immer einen Text, sonst steht auf dem
        # PDF eine nackte Null und sieht wie ein Fehler aus.
        nachlass_grund=(gruende[0] if gruende else "ohne Berechnung") if kulanz else None,
        betrag_cent=gerechnet.betrag_cent - nachlass,
        quelle="ZEIT",
        zeiteintrag_ids=[int(e["id"]) for e in eintraege],
        roh_minuten=gerechnet.roh_minuten,
        gerundete_minuten=gerechnet.gerundete_minuten,
    )


def _pauschalposition(eintrag: dict) -> Position:
    """Ein Festpreis. Die Dauer zaehlt fuer die Auswertung, nicht fuer den Betrag."""
    betrag = int(eintrag["pauschal_cent"])
    return Position(
        bezeichnung=eintrag["titel"],
        untertext=_untertext([eintrag]),
        menge_tausendstel=1000,
        einheit="LS",            # UN/ECE Rec 20: lump sum
        einzelpreis_cent=betrag,
        betrag_cent=betrag,
        # Menge 1 und Einzelpreis gleich Betrag werden GESPEICHERT -- die Menge
        # ist nach § 34a Satz 1 Nr. 4 UStDV Pflichtangabe und die Datenbank
        # weist eine Position ohne sie zurueck. Gedruckt werden sie nicht:
        # "1,00 | pausch. | 636,46 | 636,46" behauptet eine Mengenrechnung, die
        # es bei einem Festpreis nicht gibt. Auf dem Papier steht deshalb nur
        # "Affiliate Provision | Pauschale | 636,46 €".
        darstellung="NUR_TEXT",
        quelle="PAUSCHAL",
        zeiteintrag_ids=[int(eintrag["id"])],
        roh_minuten=int(eintrag["dauer_minuten"]),
    )


def _sammelbezeichnung(eintraege: Sequence[dict]) -> str:
    """'Dienstleistung Oktober 2026' bzw. der Zeitraum, wenn er Monate umfasst."""
    tage = sorted(date.fromisoformat(e["datum"]) for e in eintraege)
    erster, letzter = tage[0], tage[-1]
    if (erster.year, erster.month) == (letzter.year, letzter.month):
        return f"Dienstleistung {MONATE[erster.month - 1]} {erster.year}"
    return (
        f"Dienstleistung {erster.strftime('%d.%m.%Y')} – {letzter.strftime('%d.%m.%Y')}"
    )


def erzeuge_positionen(
    eintraege: Iterable[dict],
    *,
    modus: str = PRO_TITEL,
    standard_cent: int = 0,
    intervall_min: int = 15,
    modus_rundung: str = rundung.KAUFMAENNISCH,
) -> list[Position]:
    """Uebersetzt Zeiteintraege in Positionen.

    ``eintraege`` sind Zeilen mit id, datum, titel, beschreibung, dauer_minuten,
    abrechnungsmodus und optional grund, pauschal_cent, satz_cent_snapshot.
    Eintraege mit Modus INTERN gehoeren nicht auf eine Rechnung und werden
    uebergangen.
    """
    if modus not in MODI:
        raise ValueError(f"Unbekannter Gruppierungsmodus: {modus!r}")

    liste = [dict(e) for e in eintraege if e["abrechnungsmodus"] != "INTERN"]
    if not liste:
        return []

    pauschal = [e for e in liste if e["abrechnungsmodus"] == "PAUSCHAL"]
    stunden = [e for e in liste if e["abrechnungsmodus"] in ("STUNDEN", "KULANZ")]

    positionen: list[Position] = []

    def baue(gruppe: list[dict], bezeichnung: str) -> None:
        # Kulanz und berechnete Zeit kommen nie in dieselbe Position: ihr
        # Nachlass ist verschieden, und auf dem Papier muessen beide sichtbar
        # bleiben.
        #
        # Ebenso wenig zwei verschiedene Stundensaetze. Der Satz gehoert zum
        # Gruppenschluessel: eine Position hat EINEN Einzelpreis, und "Menge x
        # Einzelpreis = Betrag" muss auf dem Papier aufgehen. Vorher galt der
        # Satz des ersten Eintrags fuer die ganze Gruppe -- nach einem Storno
        # mit Freigabe behalten die alten Zeiten ihren eingefrorenen Satz, neue
        # kommen zum erhoehten dazu, und eine Stunde zu 80 € plus eine zu 90 €
        # ergab je Reihenfolge 160 € oder 180 €, nie 170 €.
        for ist_kulanz in (False, True):
            teil = [
                e for e in gruppe
                if (e["abrechnungsmodus"] == "KULANZ") is ist_kulanz
            ]
            saetze: list[int] = []
            for eintrag in sorted(teil, key=lambda e: (e["datum"], e["id"])):
                satz = _satz(eintrag, standard_cent)
                if satz not in saetze:
                    saetze.append(satz)
            for satz in saetze:
                positionen.append(
                    _zeitposition(
                        [e for e in teil if _satz(e, standard_cent) == satz],
                        bezeichnung,
                        standard_cent=standard_cent,
                        intervall_min=intervall_min,
                        modus_rundung=modus_rundung,
                        kulanz=ist_kulanz,
                    )
                )

    if stunden:
        if modus == SAMMEL:
            baue(stunden, _sammelbezeichnung(stunden))
        elif modus == PRO_TITEL:
            # Nach erstem Vorkommen sortiert, damit die Rechnung chronologisch liest.
            reihenfolge: list[str] = []
            for eintrag in sorted(stunden, key=lambda e: (e["datum"], e["id"])):
                if eintrag["titel"] not in reihenfolge:
                    reihenfolge.append(eintrag["titel"])
            for titel in reihenfolge:
                baue([e for e in stunden if e["titel"] == titel], titel)
        elif modus == PRO_TAG:
            for tag in sorted({e["datum"] for e in stunden}):
                gruppe = [e for e in stunden if e["datum"] == tag]
                titel = {e["titel"] for e in gruppe}
                tagestext = date.fromisoformat(tag).strftime("%d.%m.%Y")
                bezeichnung = (
                    f"{tagestext} — {titel.pop()}" if len(titel) == 1
                    else f"{tagestext} — diverse Leistungen"
                )
                baue(gruppe, bezeichnung)
        else:  # PRO_EINTRAG
            # Hier wird auf die MINUTE gerechnet, nicht auf das Intervall.
            # Intervallrundung je Eintrag vervielfacht die Summe: sieben
            # Eintraege a 7 Minuten sind 49 Minuten, aufwaerts je Eintrag
            # gerundet aber 105 (+114 %). Das Intervall ist eine Eigenschaft der
            # Zusammenfassung, nicht des einzelnen Eintrags.
            for eintrag in sorted(stunden, key=lambda e: (e["datum"], e["id"])):
                bezeichnung = eintrag["titel"]
                datumstext = date.fromisoformat(eintrag["datum"]).strftime("%d.%m.%Y")
                position = _zeitposition(
                    [eintrag], bezeichnung,
                    standard_cent=standard_cent, intervall_min=1,
                    modus_rundung=rundung.KEINE,
                    kulanz=eintrag["abrechnungsmodus"] == "KULANZ",
                )
                position.untertext = (
                    f"{datumstext} · {position.untertext}" if position.untertext else datumstext
                )
                positionen.append(position)

    # Pauschalen immer einzeln und am Ende.
    positionen.extend(_pauschalposition(e) for e in sorted(pauschal, key=lambda e: e["datum"]))
    return positionen


def summe_cent(positionen: Sequence[Position]) -> int:
    """Rechnungssumme. Nie aus Gesamtstunden x Satz neu berechnen."""
    return sum(p.betrag_cent for p in positionen)


@dataclass(frozen=True, slots=True)
class Vorschau:
    """Was aus offenen Zeiten auf einer Rechnung wuerde.

    ``erfasst_minuten`` ist die Rohzeit der Zeitpositionen, ``abrechenbar_minuten``
    dieselbe Zeit nach der Rundung je Position -- also genau die Menge, die
    auf der Rechnung stuende. Pauschalen zaehlen nur im Betrag.
    """

    erfasst_minuten: int
    abrechenbar_minuten: int
    betrag_cent: int
    positionen: list[Position]


def vorschau(eintraege: Iterable[dict], **kwargs: object) -> Vorschau:
    """Die Rechnung im Kleinen -- mit DERSELBEN Rechnung wie der Entwurf.

    Die Zeitenliste zeigt damit „0:52 erfasst → 1:00 abrechenbar“, ohne eine
    eigene Rundung nachzubauen, die irgendwann von der Rechnung abwiche.
    Argumente wie :func:`erzeuge_positionen`.
    """
    positionen = erzeuge_positionen(eintraege, **kwargs)  # type: ignore[arg-type]
    zeit = [p for p in positionen if p.quelle == "ZEIT"]
    return Vorschau(
        erfasst_minuten=sum(p.roh_minuten for p in zeit),
        abrechenbar_minuten=sum(p.gerundete_minuten for p in zeit),
        betrag_cent=summe_cent(positionen),
        positionen=positionen,
    )


def vorschau_alle_modi(
    eintraege: Iterable[dict], **kwargs: object
) -> dict[str, int]:
    """Summe je Modus -- damit das Umschalten keine Ueberraschung ist."""
    liste = [dict(e) for e in eintraege]
    return {
        modus: summe_cent(erzeuge_positionen(liste, modus=modus, **kwargs))  # type: ignore[arg-type]
        for modus in MODI
    }


def einzelnachweis(eintraege: Iterable[dict]) -> list[dict[str, object]]:
    """Zeilen fuer die Nachweis-Anlage, immer aus den ROHdaten.

    Trennt Rechnungsklarheit von Nachweispflicht: niemand muss "pro Eintrag"
    waehlen, nur weil der Kunde Details sehen will.
    """
    zeilen = []
    for eintrag in sorted(
        (dict(e) for e in eintraege if e["abrechnungsmodus"] != "INTERN"),
        key=lambda e: (e["datum"], e["id"]),
    ):
        minuten = int(eintrag["dauer_minuten"])
        zeilen.append({
            "datum": date.fromisoformat(eintrag["datum"]).strftime("%d.%m.%Y"),
            "titel": eintrag["titel"],
            "beschreibung": (eintrag.get("beschreibung") or "").strip() or None,
            "dauer": rundung.Positionsrechnung(
                minuten, minuten, 0, 0, 0
            ),
            "minuten": minuten,
            "kulanz": eintrag["abrechnungsmodus"] == "KULANZ",
        })
    return zeilen

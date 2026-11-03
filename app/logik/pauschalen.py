"""Jahresabhaengige Pauschbetraege aus einer versionierten Datei.

Sie gehoeren **nicht in den Code**. Sonst braucht das Werkzeug jedes Jahr
Programmierarbeit und verfaellt leise: der alte Wert sieht genauso aus wie ein
gepruefter.

Ein Jahr ohne eigenen Eintrag faellt auf das neueste bekannte zurueck **und
sagt das**. Stillschweigend die Vorjahreswerte zu nehmen waere der
gefaehrlichere Weg -- die Zahl waere dann falsch und unauffaellig.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DATEI = Path(__file__).resolve().parent.parent / "stammdaten" / "pauschalen.json"


class KeinSatz(LookupError):
    """Fuer dieses Jahr gibt es keine Pauschbetraege, und raten waere falsch."""


@dataclass(frozen=True, slots=True)
class Satz:
    """Die Pauschbetraege eines Jahres, mit dem Hinweis auf eine Ersatzquelle."""

    jahr: int
    werte: dict[str, int]
    #: Gesetzt, wenn die Werte aus einem anderen Jahr stammen. Dann muss die
    #: Oberflaeche es sagen.
    aus_jahr: int | None = None

    def __getitem__(self, name: str) -> int:
        return self.werte[name]

    @property
    def veraltet(self) -> bool:
        return self.aus_jahr is not None

    @property
    def rueckwaerts(self) -> bool:
        """Die Werte stammen aus einem SPAETEREN Jahr -- geraten, nicht fortgeschrieben.

        Ein spaeteres Jahr ohne Eintrag bekommt die letzten bekannten Werte,
        das ist die Fortschreibung, die man nachtraegt. Ein frueheres Jahr
        hatte dagegen andere Saetze (2019: 24 und 12 Euro statt 28 und 14);
        die jetzigen rueckwaerts anzuwenden ist keine Fortschreibung, sondern
        falsch.
        """
        return self.aus_jahr is not None and self.aus_jahr > self.jahr

    @property
    def hinweis(self) -> str:
        if not self.veraltet:
            return ""
        if self.rueckwaerts:
            return (
                f"Für {self.jahr} sind keine Pauschbeträge hinterlegt — die ältesten "
                f"bekannten sind die von {self.aus_jahr}, und frühere Jahre hatten "
                f"andere Sätze. Pauschalen lassen sich für {self.jahr} deshalb nicht "
                f"buchen; die Beträge für {self.jahr} bitte prüfen und in "
                f"app/stammdaten/pauschalen.json nachtragen."
            )
        return (
            f"Für {self.jahr} sind keine Pauschbeträge hinterlegt; verwendet werden die "
            f"von {self.aus_jahr}. Die Beträge für {self.jahr} bitte prüfen und in "
            f"app/stammdaten/pauschalen.json nachtragen."
        )


def fuer(jahr: int, *, pfad: Path | None = None, rueckwaerts: bool = True) -> Satz:
    """Die Pauschbetraege eines Jahres.

    Ein Jahr NACH dem letzten Eintrag bekommt dessen Werte, als
    ``veraltet`` gekennzeichnet. Ein Jahr VOR dem aeltesten Eintrag bekommt
    mit ``rueckwaerts=True`` (Vorgabe) die aeltesten bekannten Werte -- fuer
    Wertgrenzen wie die GWG-Grenze, die seit Jahren stehen, ist das die
    bessere Annahme als ein Abbruch. Fuer die Pauschalen selbst ist es
    falsch (2019: 24/12 statt 28/14 Euro), deshalb verlangen
    :func:`verpflegung` und :func:`kilometergeld_cent` ``rueckwaerts=False``
    und bekommen dann :class:`KeinSatz`.

    >>> fuer(2026)["verpflegung_inland_vollertag_cent"]
    2800
    >>> fuer(2099).veraltet
    True
    >>> fuer(2019).rueckwaerts
    True
    """
    rohdaten = json.loads((pfad or DATEI).read_text(encoding="utf-8"))["jahre"]
    bekannt = {int(j): werte for j, werte in rohdaten.items()}
    if jahr in bekannt:
        return Satz(jahr=jahr, werte=bekannt[jahr])
    frueher = [j for j in bekannt if j < jahr]
    if frueher:
        # Das naechstliegende fruehere Jahr: besser als ein KeyError mitten in
        # einer Erfassung, und der Satz sagt, woher die Werte stammen.
        quelle = max(frueher)
    elif rueckwaerts:
        quelle = min(bekannt)
    else:
        raise KeinSatz(
            f"Für {jahr} sind keine Pauschbeträge hinterlegt — die ältesten bekannten "
            f"sind die von {min(bekannt)}, und frühere Jahre hatten andere Sätze. Die "
            f"Beträge für {jahr} bitte prüfen und in app/stammdaten/pauschalen.json "
            "nachtragen."
        )
    return Satz(jahr=jahr, werte=bekannt[quelle], aus_jahr=quelle)


def jahre(*, pfad: Path | None = None) -> list[int]:
    rohdaten = json.loads((pfad or DATEI).read_text(encoding="utf-8"))["jahre"]
    return sorted(int(j) for j in rohdaten)


# ------------------------------------------------- Verpflegungspauschale

@dataclass(frozen=True, slots=True)
class Verpflegung:
    """Die Verpflegungspauschale einer Reise, aufgeschluesselt."""

    volle_tage: int
    teiltage: int
    satz_voll_cent: int
    satz_teil_cent: int
    jahr: int
    veraltet: bool = False
    #: Das Jahr, aus dem die Saetze stammen, wenn es nicht ``jahr`` ist.
    aus_jahr: int | None = None

    @property
    def betrag_cent(self) -> int:
        return self.volle_tage * self.satz_voll_cent + self.teiltage * self.satz_teil_cent

    @property
    def hinweis(self) -> str:
        """Der Ersatzjahr-Hinweis, wortgleich mit :attr:`Satz.hinweis`."""
        return Satz(self.jahr, {}, self.aus_jahr).hinweis

    @property
    def nachweis(self) -> str:
        # euro() aus rundung, damit hier nicht 28.00 € steht: ein deutscher
        # Beleg mit englischem Dezimaltrenner ist genau die Verwechslung, die
        # das Werkzeug an anderer Stelle mühsam auseinanderhält.
        from app.logik.rundung import euro

        teile = []
        if self.volle_tage:
            teile.append(f"{self.volle_tage} × {euro(self.satz_voll_cent)} € (voller Tag)")
        if self.teiltage:
            teile.append(f"{self.teiltage} × {euro(self.satz_teil_cent)} € "
                         "(An- oder Abreisetag bzw. über 8 Stunden)")
        return " + ".join(teile) or "keine Pauschale"


def verpflegung(
    abreise: str | date, rueckkehr: str | date, *,
    abwesend_stunden: int | None = None, uebernachtung: bool = True,
    pfad: Path | None = None,
) -> Verpflegung:
    """Berechnet die Verpflegungspauschale fuer eine Inlandsreise.

    § 9 Abs. 4a Satz 3 EStG, Inlandssaetze:

    * Nr. 1: **28 €** fuer jeden Kalendertag mit 24 Stunden Abwesenheit -- auch
      fuer eine eintaegige Reise, wenn sie den ganzen Tag dauert.
    * Nr. 2: **14 €** fuer An- und Abreisetag einer mehrtaegigen Reise, ohne
      Mindestdauer -- aber nur, wenn an diesem, dem vorhergehenden oder dem
      folgenden Tag **ausserhalb der Wohnung uebernachtet** wird.
    * Nr. 3: **14 €** fuer jeden Tag mit mehr als acht Stunden Abwesenheit
      ohne Uebernachtung. Geht so eine Taetigkeit ueber Mitternacht, gibt es
      die 14 € einmal, fuer den Tag mit dem ueberwiegenden Teil der Abwesenheit.

    Eine Reise ueber zwei Kalendertage ohne Uebernachtung (Abfahrt 22 Uhr,
    Rueckkehr 2 Uhr) ist also KEINE Reise mit An- und Abreisetag: es gilt die
    Teiltagregel, und bei vier Stunden gibt es nichts.

    ⚠ Nur Inland. Die Auslandssaetze setzt das BMF jedes Jahr neu fest; sie sind
    bewusst nicht hinterlegt, weil ein veralteter Satz aussieht wie ein
    gepruefter.

    ⚠ Keine Kuerzung fuer gestellte Mahlzeiten. Ist das Fruehstueck im
    Hotelpreis enthalten, mindert das die Pauschale um 20 Prozent des
    Vollertagssatzes -- das muss der Nutzer selbst eintragen, und die
    Oberflaeche sagt es.

    Fuer ein Jahr vor dem aeltesten hinterlegten Satz gibt es :class:`KeinSatz`
    statt eines geratenen Betrags.

    >>> verpflegung("2026-03-02", "2026-03-04").betrag_cent
    5600
    >>> verpflegung("2026-03-02", "2026-03-02", abwesend_stunden=9).betrag_cent
    1400
    >>> verpflegung("2026-03-02", "2026-03-02", abwesend_stunden=4).betrag_cent
    0
    >>> verpflegung("2026-03-02", "2026-03-02", abwesend_stunden=24).betrag_cent
    2800
    >>> verpflegung("2026-03-02", "2026-03-03", abwesend_stunden=4,
    ...             uebernachtung=False).betrag_cent
    0
    """
    von = date.fromisoformat(abreise) if isinstance(abreise, str) else abreise
    bis = date.fromisoformat(rueckkehr) if isinstance(rueckkehr, str) else rueckkehr
    if bis < von:
        raise ValueError("Die Rückkehr liegt vor der Abreise.")
    if abwesend_stunden is not None and not 0 <= abwesend_stunden <= 24:
        # Das Formular begrenzt auf 0 bis 24, aber nur im Browser.
        raise ValueError("Die Abwesenheit muss zwischen 0 und 24 Stunden liegen.")

    satz = fuer(von.year, pfad=pfad, rueckwaerts=False)
    voll = satz["verpflegung_inland_vollertag_cent"]
    teil = satz["verpflegung_inland_teiltag_cent"]
    mindest = satz["verpflegung_mindeststunden_teiltag"]

    tage = (bis - von).days + 1
    if tage == 1 and abwesend_stunden == 24:
        # Ein ganzer Kalendertag abwesend: der volle Satz (Nr. 1), auch ohne
        # Uebernachtung. Vorher zaehlte jede eintaegige Reise als Teiltag.
        return Verpflegung(1, 0, voll, teil, von.year, satz.veraltet, satz.aus_jahr)
    if tage == 1 or not uebernachtung:
        # Eintaegig, oder ueber Mitternacht ohne Uebernachtung: kein voller
        # Tag und keine An-/Abreisepauschale, nur die Achtstundengrenze (Nr. 3).
        teiltage = 1 if (abwesend_stunden or 0) > mindest else 0
        return Verpflegung(0, teiltage, voll, teil, von.year, satz.veraltet, satz.aus_jahr)
    # Mehrtaegig mit Uebernachtung: An- und Abreisetag je 14 €, alles
    # dazwischen voll.
    return Verpflegung(tage - 2, 2, voll, teil, von.year, satz.veraltet, satz.aus_jahr)


def kilometergeld_cent(kilometer: int, jahr: int, *, pfad: Path | None = None) -> int:
    """Nutzungseinlage fuer Fahrten mit dem privaten Pkw.

    0,30 € je Kilometer. Kein Beleg eines Dritten, sondern eine Aufstellung --
    und damit ein Eigenbeleg, der die Fahrten einzeln nachweisen muss.

    >>> kilometergeld_cent(250, 2026)
    7500
    """
    if kilometer < 0:
        raise ValueError("Negative Kilometer gibt es nicht.")
    return kilometer * fuer(jahr, pfad=pfad, rueckwaerts=False)["kilometergeld_pkw_cent"]

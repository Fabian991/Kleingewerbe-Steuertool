"""Einkommensteuer, Zuschläge und Gewerbesteuer — die Bausteine der Steuerrücklage.

Reine Funktionen ohne Datenbank. Die Werte jedes Jahres stehen in
``app/stammdaten/steuertarif.json`` und nicht hier, aus demselben Grund wie bei
den Pauschalen (``pauschalen.py``): ein veralteter Wert im Code sieht genauso
aus wie ein geprüfter. Ein Jahr ohne eigenen Eintrag bekommt den
nächstliegenden **und sagt das** (``hinweis``).

Gerechnet wird exakt mit :class:`fractions.Fraction` — ohne Zwischenrundung,
wie § 32a Abs. 1 EStG es verlangt. Gerundet wird nur dort, wo das Gesetz es
sagt:

* das zu versteuernde Einkommen (zvE) auf volle Euro **abrunden**, ebenso die
  tarifliche Einkommensteuer (§ 32a Abs. 1 Satz 1 und 6 EStG);
* beim Splitting erst die Hälfte des zvE abrunden, darauf die Steuer abrunden,
  dann verdoppeln (§ 32a Abs. 5; so rechnet auch der BMF-Programmablaufplan);
* beim Solidaritätszuschlag bleiben Bruchteile eines Cents außer Ansatz
  (§ 4 Satz 3 SolZG), bei der Kirchensteuer ebenso.

Die Beispiele in den Docstrings sind Werte des amtlichen BMF-Steuerrechners
(abgefragt am 25.10.2026) und laufen als Doctest mit.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

DATEI = Path(__file__).resolve().parent.parent / "stammdaten" / "steuertarif.json"
#: Der Ort, den die Hinweise nennen: dort gehoert ein fehlendes Jahr hin.
DATEI_NAME = "app/stammdaten/steuertarif.json"


# ------------------------------------------------------------------ Laden

@lru_cache(maxsize=8)
def _gelesen(pfad: str, _stand: int) -> dict:
    return json.loads(Path(pfad).read_text(encoding="utf-8"))


def _daten(pfad: Path | None = None) -> dict:
    """Die Stammdatei, gelesen und zwischengespeichert, solange sie sich nicht aendert."""
    datei = pfad or DATEI
    return _gelesen(str(datei), datei.stat().st_mtime_ns)


def _jahre(eintraege: dict) -> dict[int, object]:
    return {int(j): wert for j, wert in eintraege.items() if not j.startswith("_")}


def _quelljahr(bekannt: dict[int, object], jahr: int) -> int:
    """Das Jahr, dessen Werte fuer ``jahr`` gelten.

    Das Jahr selbst, sonst das naechstliegende fruehere (Fortschreibung),
    sonst das frueheste bekannte (rueckwaerts geraten -- der Hinweis sagt es).

    Bewusst nicht immer das NEUESTE bekannte Jahr: fuer ein Jahr nach dem
    letzten hinterlegten ist es das (2029 rechnet mit 2026), fuer ein Jahr
    VOR dem ersten aber laege 2026 drei Tarifaenderungen weiter weg als 2024.
    Das naechstliegende Jahr ist dort naeher am damaligen Recht, und der
    Hinweis nennt es samt "nur grob".

    >>> bekannt = {2024: 1, 2026: 2}
    >>> _quelljahr(bekannt, 2025), _quelljahr(bekannt, 2030), _quelljahr(bekannt, 2020)
    (2024, 2026, 2024)
    """
    if jahr in bekannt:
        return jahr
    frueher = [j for j in bekannt if j < jahr]
    return max(frueher) if frueher else min(bekannt)


def _q(text: str | int) -> Fraction:
    """Ein Satz aus der Stammdatei, exakt. Text wie "954.80" wird ohne Gleitkomma gelesen."""
    return Fraction(text)


def ersatz_hinweis(was: str, jahr: int, aus_jahr: int | None) -> str:
    """Der Satz, den die Seite neben eine Zahl stellt, die aus einem anderen Jahr stammt.

    >>> ersatz_hinweis("Tarif", 2028, 2026)
    'Für 2028 ist noch kein Tarif hinterlegt; gerechnet wird mit dem von 2026.'
    >>> print(ersatz_hinweis("Tarif", 2023, 2024))
    Für 2023 ist kein Tarif hinterlegt; gerechnet wird mit dem von 2024 — frühere Jahre hatten \
andere Werte, die Schätzung ist für 2023 deshalb nur grob.
    """
    if aus_jahr is None or aus_jahr == jahr:
        return ""
    if aus_jahr > jahr:
        return (f"Für {jahr} ist kein {was} hinterlegt; gerechnet wird mit dem von "
                f"{aus_jahr} — frühere Jahre hatten andere Werte, die Schätzung ist für "
                f"{jahr} deshalb nur grob.")
    return f"Für {jahr} ist noch kein {was} hinterlegt; gerechnet wird mit dem von {aus_jahr}."


# ------------------------------------------------------- § 32a EStG: Tarif

@dataclass(frozen=True, slots=True)
class Zone:
    """Eine Tarifzone nach § 32a Abs. 1 EStG.

    ``art`` ist eine von vier Formen:

    * ``null``   -- Grundfreibetrag, keine Steuer;
    * ``y``      -- (a · y + 1.400) · y mit y = (x − basis) / 10.000;
    * ``z``      -- (b · z + 2.397) · z + c mit z = (x − basis) / 10.000;
    * ``linear`` -- satz · x − abzug.
    """

    bis: int | None
    art: str
    basis: int = 0
    faktor: Fraction = Fraction(0)  # a in der y-Zone, b in der z-Zone
    c: Fraction = Fraction(0)
    satz: Fraction = Fraction(0)
    abzug: Fraction = Fraction(0)

    def roh(self, x: int) -> Fraction:
        """Die Steuer in dieser Zone fuer ganzzahliges ``x``, ungerundet."""
        if self.art == "null":
            return Fraction(0)
        if self.art == "y":
            y = Fraction(x - self.basis, 10000)
            return (self.faktor * y + 1400) * y
        if self.art == "z":
            z = Fraction(x - self.basis, 10000)
            return (self.faktor * z + 2397) * z + self.c
        return self.satz * x - self.abzug

    def grenzsatz(self, x: int) -> Fraction:
        """Die Ableitung der Zonenformel: was der naechste Euro kostet."""
        if self.art == "null":
            return Fraction(0)
        if self.art == "y":
            return (2 * self.faktor * Fraction(x - self.basis, 10000) + 1400) / 10000
        if self.art == "z":
            return (2 * self.faktor * Fraction(x - self.basis, 10000) + 2397) / 10000
        return self.satz


def _zone_aus(roh: dict) -> Zone:
    art = roh["art"]
    if art == "null":
        return Zone(bis=roh["bis"], art=art)
    if art == "y":
        return Zone(bis=roh["bis"], art=art, basis=int(roh["basis"]), faktor=_q(roh["a"]))
    if art == "z":
        return Zone(bis=roh["bis"], art=art, basis=int(roh["basis"]), faktor=_q(roh["b"]),
                    c=_q(roh["c"]))
    if art == "linear":
        return Zone(bis=roh["bis"], art=art, satz=_q(roh["satz"]), abzug=_q(roh["abzug"]))
    raise ValueError(f"Unbekannte Tarifzone „{art}“ in {DATEI_NAME}.")


@dataclass(frozen=True, slots=True)
class Stufe:
    """Eine Tarifzone, aufbereitet fuer die Tabelle "Abstufungen"."""

    nr: int
    von_euro: int
    bis_euro: int | None
    satz_von: Fraction
    satz_bis: Fraction
    name: str
    erklaerung: str

    @property
    def gleichbleibend(self) -> bool:
        return self.satz_von == self.satz_bis


@dataclass(frozen=True, slots=True)
class Tarif:
    """Der Einkommensteuertarif eines Jahres.

    ``aus_jahr`` ist gesetzt, wenn die Zonen aus einem anderen Jahr stammen.
    ``status`` sagt, warum: ``fortgeltung`` (kein neues Gesetz, das alte gilt
    weiter -- 2027), ``ersatz`` (nichts hinterlegt, das naechstliegende Jahr
    verwendet) oder ``entwurf`` (Regierungsentwurf, kein Recht).
    """

    jahr: int
    zonen: tuple[Zone, ...]
    status: str
    quelle: str
    aus_jahr: int | None = None
    #: Die Fundstelle in einem Wort, fuer den Hinweis ("BT-Drs. 21/8235").
    kurzquelle: str = ""

    @property
    def entwurf(self) -> bool:
        return self.status == "entwurf"

    @property
    def grundfreibetrag(self) -> int:
        return int(self.zonen[0].bis or 0)

    @property
    def hinweis(self) -> str:
        """Was die Seite zum Tarif sagen muss; leer bei geltendem Recht des Jahres.

        >>> tarif_fuer(2026).hinweis
        ''
        >>> tarif_fuer(2027).hinweis
        'Für 2027 ist kein neuer Tarif verkündet (Stand 25.10.2026) — es gilt der Tarif 2026 fort.'
        >>> tarif_fuer(2029).hinweis
        'Tarif 2026 verwendet — für 2029 noch nicht hinterlegt.'
        >>> tarif_fuer(2027, entwurf=True).hinweis[:44]
        'Entwurf, vorläufig: Tarif 2027 nach dem Regi'
        """
        if self.status == "fortgeltung":
            stand = _datum_de(_daten().get("rechtsstand", ""))
            return (f"Für {self.jahr} ist kein neuer Tarif verkündet (Stand {stand}) — es gilt "
                    f"der Tarif {self.aus_jahr} fort.")
        if self.status == "entwurf":
            return (f"Entwurf, vorläufig: Tarif {self.jahr} nach dem Regierungsentwurf "
                    f"({self.kurzquelle}) — noch kein Gesetz. Bis zur Verkündung kann sich "
                    "jeder Wert ändern.")
        if self.status == "ersatz" and self.aus_jahr is not None:
            if self.aus_jahr > self.jahr:
                return ersatz_hinweis("Tarif", self.jahr, self.aus_jahr)
            return f"Tarif {self.aus_jahr} verwendet — für {self.jahr} noch nicht hinterlegt."
        return ""

    def zone(self, x: int) -> int:
        """Der Index der Zone, in der ein (abgerundetes) zvE liegt."""
        for nr, zone in enumerate(self.zonen):
            if zone.bis is None or x <= zone.bis:
                return nr
        raise AssertionError("Die letzte Zone ist nach oben offen.")

    def steuer(self, zve_euro: int | Fraction) -> int:
        """Grundtarif: zvE auf volle Euro abrunden, Steuer auf volle Euro abrunden.

        >>> t = tarif_fuer(2026)
        >>> [t.steuer(x) for x in (12348, 12349, 15000, 17799, 17800, 69878, 69879)]
        [0, 0, 435, 1034, 1035, 18213, 18213]
        >>> t.steuer(277825), t.steuer(277826), t.steuer(-500)
        (105550, 105551, 0)
        """
        x = math.floor(zve_euro)
        if x <= 0:
            return 0
        return math.floor(self.zonen[self.zone(x)].roh(x))

    def splitting(self, zve_euro: int | Fraction) -> int:
        """Splittingtarif (§ 32a Abs. 5): zweimal die Steuer auf die abgerundete Hälfte.

        Die Hälfte wird abgerundet, bevor die Steuer gerechnet wird -- deshalb
        ist die Splittingsteuer immer gerade, und 50.001 € kosten so viel wie
        50.000 €.

        >>> t = tarif_fuer(2026)
        >>> t.splitting(50000), t.splitting(50001), t.splitting(160000)
        (5700, 5700, 44928)
        """
        x = math.floor(zve_euro)
        return 2 * self.steuer(Fraction(x, 2))

    def grenzsatz(self, zve_euro: int | Fraction) -> Fraction:
        """Der Grenzsteuersatz: der Anteil, den der naechste Euro kostet.

        >>> t = tarif_fuer(2026)
        >>> [abgeschnitten(t.grenzsatz(x)) for x in (15000, 25000, 45000, 69878, 70000)]
        ['18,85', '26,46', '33,38', '41,99', '42,00']
        """
        x = max(0, math.floor(zve_euro))
        return self.zonen[self.zone(x)].grenzsatz(x)

    def stufen(self, *, faktor: int = 1) -> list[Stufe]:
        """Die Zonen als Tabelle; ``faktor=2`` verdoppelt die Grenzen (Splitting).

        >>> [(s.von_euro, s.bis_euro, prozent_ganz(s.satz_von), prozent_ganz(s.satz_bis))
        ...  for s in tarif_fuer(2026).stufen()]
        [(0, 12348, 0, 0), (12349, 17799, 14, 24), (17800, 69878, 24, 42), \
(69879, 277825, 42, 42), (277826, None, 45, 45)]
        """
        ergebnis: list[Stufe] = []
        von = 0
        lineare = 0
        for nr, zone in enumerate(self.zonen):
            bis = zone.bis
            if zone.art == "null":
                name = "Grundfreibetrag"
                erklaerung = "steuerfrei"
            elif zone.art == "y":
                name = "Eingangszone"
                erklaerung = "der Satz steigt schnell"
            elif zone.art == "z":
                name = "Progressionszone"
                erklaerung = "der Satz steigt langsamer"
            else:
                name = ("Spitzensteuersatz", "„Reichensteuer“")[lineare] if lineare < 2 \
                    else "Höchststufe"
                erklaerung = "gleichbleibend"
                lineare += 1
            satz_von = zone.grenzsatz(von)
            satz_bis = zone.grenzsatz(bis) if bis is not None else zone.grenzsatz(von)
            ergebnis.append(Stufe(
                nr=nr, von_euro=von * faktor if nr else 0,
                bis_euro=None if bis is None else bis * faktor + (faktor - 1),
                satz_von=satz_von, satz_bis=satz_bis, name=name, erklaerung=erklaerung,
            ))
            if bis is not None:
                von = bis + 1
        return ergebnis


def _datum_de(iso: str) -> str:
    """>>> _datum_de("2026-10-25")
    '25.10.2026'
    """
    teile = iso.split("-")
    return ".".join(reversed(teile)) if len(teile) == 3 else iso


def tarif_fuer(jahr: int, *, entwurf: bool = False, pfad: Path | None = None) -> Tarif:
    """Der Tarif eines Jahres, mit Herkunft.

    Mit ``entwurf=True`` und einem Entwurf fuer das Jahr dessen Zonen;
    sonst der geltende Tarif (der Schalter hat dann keine Wirkung).

    >>> tarif_fuer(2026).status, tarif_fuer(2027).aus_jahr, tarif_fuer(2027).status
    ('gesetz', 2026, 'fortgeltung')
    >>> tarif_fuer(2027, entwurf=True).grundfreibetrag, tarif_fuer(2026, entwurf=True).entwurf
    (12564, False)
    >>> tarif_fuer(2023).aus_jahr, tarif_fuer(2023).status
    (2024, 'ersatz')
    """
    daten = _daten(pfad)
    if entwurf:
        entwuerfe = _jahre(daten.get("tarif_entwurf", {}))
        if jahr in entwuerfe:
            roh = entwuerfe[jahr]
            return Tarif(jahr=jahr, zonen=tuple(_zone_aus(z) for z in roh["zonen"]),
                         status="entwurf", quelle=roh["quelle"],
                         kurzquelle=roh.get("kurz", roh["quelle"]))
    bekannt = _jahre(daten["tarif"])
    quelle_jahr = _quelljahr(bekannt, jahr)
    eintrag = bekannt[quelle_jahr]
    status = eintrag["status"] if quelle_jahr == jahr else "ersatz"
    quelle = eintrag["quelle"]
    # "wie": ein Jahr ohne eigenes Gesetz, in dem das alte fortgilt.
    while "wie" in eintrag:
        quelle_jahr = int(eintrag["wie"])
        eintrag = bekannt[quelle_jahr]
    if status != "fortgeltung":
        quelle = eintrag["quelle"]
    return Tarif(
        jahr=jahr, zonen=tuple(_zone_aus(z) for z in eintrag["zonen"]), status=status,
        quelle=quelle, aus_jahr=None if quelle_jahr == jahr else quelle_jahr,
    )


def entwurf_verfuegbar(jahr: int, *, pfad: Path | None = None) -> bool:
    """Gibt es fuer das Jahr einen Tarifentwurf, den die Seite anbieten kann?

    >>> entwurf_verfuegbar(2027), entwurf_verfuegbar(2026)
    (True, False)
    """
    return jahr in _jahre(_daten(pfad).get("tarif_entwurf", {}))


def tarif(zve_euro: int | Fraction, jahr: int, *, entwurf: bool = False) -> int:
    """Einkommensteuer nach dem Grundtarif, in vollen Euro.

    >>> tarif(45000, 2026), tarif(45000, 2025), tarif(45000, 2024)
    (8835, 8961, 9121)
    """
    return tarif_fuer(jahr, entwurf=entwurf).steuer(zve_euro)


def splitting(zve_euro: int | Fraction, jahr: int, *, entwurf: bool = False) -> int:
    """Einkommensteuer nach dem Splittingtarif, in vollen Euro.

    >>> splitting(100000, 2026), splitting(100001, 2026), splitting(100000, 2024)
    (21096, 21096, 21744)
    """
    return tarif_fuer(jahr, entwurf=entwurf).splitting(zve_euro)


def grenzsteuersatz(zve_euro: int | Fraction, jahr: int, *, zusammen: bool = False,
                    entwurf: bool = False) -> Fraction:
    """Grenzsteuersatz der Einkommensteuer; bei Zusammenveranlagung an der Hälfte.

    >>> abgeschnitten(grenzsteuersatz(25000, 2024))
    '26,86'
    >>> abgeschnitten(grenzsteuersatz(160001, 2026, zusammen=True))
    '42,00'
    """
    t = tarif_fuer(jahr, entwurf=entwurf)
    x = math.floor(zve_euro)
    return t.grenzsatz(x // 2 if zusammen else x)


def abgeschnitten(satz: Fraction) -> str:
    """Ein Satz in Prozent mit zwei Nachkommastellen, abgeschnitten wie beim BMF.

    >>> abgeschnitten(Fraction(1885, 10000)), abgeschnitten(Fraction(419997, 1000000))
    ('18,85', '41,99')
    """
    hundertstel = math.floor(satz * 10000)
    return f"{hundertstel // 100},{hundertstel % 100:02d}"


def prozent_ganz(satz: Fraction) -> int:
    """Ein Satz in ganzen Prozent, kaufmaennisch gerundet -- fuer die Stufentabelle.

    >>> prozent_ganz(Fraction(2397, 10000)), prozent_ganz(Fraction(14, 100))
    (24, 14)
    """
    return math.floor(satz * 100 + Fraction(1, 2))


# --------------------------------------------- Solidaritaetszuschlag (SolZG)

@dataclass(frozen=True, slots=True)
class Freigrenze:
    jahr: int
    einzel_euro: int
    zusammen_euro: int
    status: str
    quelle: str
    aus_jahr: int | None = None

    @property
    def hinweis(self) -> str:
        """>>> soli_freigrenze(2030).hinweis
        'Für 2030 ist noch keine Soli-Freigrenze hinterlegt; gerechnet wird mit der von 2027.'
        >>> soli_freigrenze(2027).hinweis
        ''
        """
        if self.aus_jahr is None:
            return ""
        if self.aus_jahr > self.jahr:
            return (f"Für {self.jahr} ist keine Soli-Freigrenze hinterlegt; gerechnet wird "
                    f"mit der von {self.aus_jahr}.")
        return (f"Für {self.jahr} ist noch keine Soli-Freigrenze hinterlegt; gerechnet wird "
                f"mit der von {self.aus_jahr}.")


    @property
    def hinweis_fortgeltung(self) -> str:
        """Fuer ein Jahr, in dem der alte Wert nur fortgilt -- sicher ist er erst im Gesetz.

        >>> soli_freigrenze(2027).hinweis_fortgeltung
        'Für 2027 gilt die Soli-Freigrenze von 2026 fort (20.350 €); geändert ist sie bisher nicht.'
        >>> soli_freigrenze(2026).hinweis_fortgeltung
        ''
        """
        if self.status != "fortgeltung":
            return ""
        grenze = f"{self.einzel_euro:,}".replace(",", ".")
        return (f"Für {self.jahr} gilt die Soli-Freigrenze von {self.jahr - 1} fort "
                f"({grenze} €); geändert ist sie bisher nicht.")


def soli_freigrenze(jahr: int, *, pfad: Path | None = None) -> Freigrenze:
    """Die Freigrenze des Solidaritaetszuschlags (§ 3 Abs. 3 SolZG).

    >>> f = soli_freigrenze(2026); (f.einzel_euro, f.zusammen_euro, f.aus_jahr)
    (20350, 40700, None)
    >>> soli_freigrenze(2027).status, soli_freigrenze(2024).einzel_euro
    ('fortgeltung', 18130)
    """
    bekannt = _jahre(_daten(pfad)["soli"]["freigrenze"])
    quelle_jahr = _quelljahr(bekannt, jahr)
    roh = bekannt[quelle_jahr]
    return Freigrenze(
        jahr=jahr, einzel_euro=int(roh["einzel"]), zusammen_euro=int(roh["zusammen"]),
        status=roh["status"] if quelle_jahr == jahr else "ersatz", quelle=roh["quelle"],
        aus_jahr=None if quelle_jahr == jahr else quelle_jahr,
    )


def soli(bemessung_cent: int, jahr: int, *, zusammen: bool = False,
         pfad: Path | None = None) -> int:
    """Solidaritaetszuschlag in Cent (§§ 3, 4 SolZG).

    Bis zur Freigrenze nichts; darueber 5,5 % der Bemessungsgrundlage, aber
    hoechstens 11,9 % des Betrags ueber der Freigrenze (Milderungszone).
    Bruchteile eines Cents bleiben ausser Ansatz. Bemessungsgrundlage ist die
    Einkommensteuer NACH der Gewerbesteuer-Anrechnung nach § 35 EStG
    (BFH, Urteil vom 14.11.2018, II R 63/15).

    >>> soli(1826400, 2026), soli(2036400, 2026), soli(2246400, 2026), soli(3926400, 2026)
    (0, 166, 25156, 215952)
    >>> soli(4492800, 2026, zusammen=True), soli(2296300, 2024), soli(2268800, 2025)
    (50313, 57512, 32582)
    """
    daten = _daten(pfad)["soli"]
    grenze = soli_freigrenze(jahr, pfad=pfad)
    frei_cent = 100 * (grenze.zusammen_euro if zusammen else grenze.einzel_euro)
    if bemessung_cent <= frei_cent:
        return 0
    voll = _q(daten["satz"]) * bemessung_cent
    gemildert = _q(daten["milderung"]) * (bemessung_cent - frei_cent)
    return math.floor(min(voll, gemildert))


# ------------------------------------------------- Kirchensteuer (§ 51a EStG)

def kirchensteuer_saetze(*, pfad: Path | None = None) -> tuple[int, ...]:
    """>>> kirchensteuer_saetze()
    (8, 9)
    """
    return tuple(int(s) for s in _daten(pfad)["kirchensteuer"]["saetze_prozent"])


def kirchensteuer_hinweis(jahr: int, prozent: int, *, pfad: Path | None = None) -> str:
    """Ein Hinweis, wenn die Kirchensteuersaetze fuer das Jahr noch nicht belegt sind.

    >>> print(kirchensteuer_hinweis(2027, 9))
    Die Kirchensteuersätze für 2027 sind noch nicht veröffentlicht; gerechnet wird wie \
bisher mit 9 %.
    >>> kirchensteuer_hinweis(2026, 9), kirchensteuer_hinweis(2027, 0)
    ('', '')
    """
    belegt_bis = int(_daten(pfad)["kirchensteuer"].get("belegt_bis", 0))
    if not prozent or jahr <= belegt_bis:
        return ""
    return (f"Die Kirchensteuersätze für {jahr} sind noch nicht veröffentlicht; gerechnet wird "
            f"wie bisher mit {prozent} %.")


def kirchensteuer(est_cent: int, prozent: int) -> int:
    """Kirchensteuer in Cent: ``prozent`` der Einkommensteuer VOR § 35 EStG.

    § 51a Abs. 2 Satz 3 EStG nimmt die Gewerbesteuer-Anrechnung ausdruecklich
    aus. Kappung und Kirchgeld sind nicht beruecksichtigt.

    >>> kirchensteuer(1423300, 9), kirchensteuer(1423300, 8), kirchensteuer(1423300, 0)
    (128097, 113864, 0)
    """
    if est_cent <= 0 or not prozent:
        return 0
    return math.floor(Fraction(est_cent * prozent, 100))


# ------------------------------------------- Haerteausgleich (§ 46 Abs. 3 EStG)

def haerteausgleich(nebeneinkuenfte_cent: int, *, pfad: Path | None = None) -> int:
    """Der Abzugsbetrag nach § 46 Abs. 3 EStG und § 70 EStDV, in Cent.

    Wer Arbeitslohn bezieht, zahlt auf Nebeneinkuenfte bis 410 € keine
    Steuer: sie werden vom Einkommen wieder abgezogen. Zwischen 410 € und
    820 € wird gleitend uebergeleitet -- abgezogen wird, was zu 820 € fehlt.
    Ab 820 € gibt es nichts mehr. Ein Verlust bekommt keinen Abzug.

    >>> [haerteausgleich(e * 100) // 100 for e in (-100, 300, 410, 411, 600, 819, 820, 1000)]
    [0, 300, 410, 409, 220, 1, 0, 0]
    """
    werte = _daten(pfad)["veranlagung"]
    voll = 100 * int(werte["haerteausgleich_voll_bis_euro"])
    gleitend = 100 * int(werte["haerteausgleich_gleitend_bis_euro"])
    if nebeneinkuenfte_cent <= 0:
        return 0
    if nebeneinkuenfte_cent <= voll:
        return nebeneinkuenfte_cent
    if nebeneinkuenfte_cent < gleitend:
        return min(gleitend - nebeneinkuenfte_cent, nebeneinkuenfte_cent)
    return 0


def veranlagungswerte(*, pfad: Path | None = None) -> dict[str, int]:
    """Grenzen fuer Pflichtveranlagung und Vorauszahlungen (§ 46, § 37 EStG, § 19 GewStG)."""
    return {k: int(v) for k, v in _daten(pfad)["veranlagung"].items() if not k.startswith("_")}


# ------------------------------------------------ Bruttolohn -> zvE (Naeherung)

@dataclass(frozen=True, slots=True)
class LohnSchaetzung:
    """Das geschaetzte zu versteuernde Einkommen aus dem Arbeitslohn.

    Alle Teilbetraege in Cent (ungerundet abgeschnitten), das zvE in vollen Euro.
    Das zvE kann negativ sein: ohne Gehalt bleiben 36 € Sonderausgaben-
    Pauschbetrag, die nichts mindern als den Gewinn, der dazukommt.
    """

    jahr: int
    brutto_cent: int
    werbungskosten_cent: int
    rentenversicherung_cent: int
    sonstige_vorsorge_cent: int
    sonderausgaben_cent: int
    #: Einkuenfte aus nichtselbstaendiger Arbeit (Brutto minus Pauschbetrag).
    einkuenfte_cent: int
    zve_euro: int
    aus_jahr: int | None = None

    @property
    def hinweis(self) -> str:
        """>>> zve_aus_brutto(5000000, 2027).hinweis[:68]
        'Für 2027 sind noch keine Sozialversicherungswerte hinterlegt; gerech'
        """
        if self.aus_jahr is None:
            return ""
        if self.aus_jahr > self.jahr:
            return (f"Für {self.jahr} sind keine Sozialversicherungswerte hinterlegt; "
                    f"gerechnet wird mit denen von {self.aus_jahr}. Das zu versteuernde "
                    "Einkommen aus dem Bruttogehalt ist dadurch etwas ungenauer.")
        return (f"Für {self.jahr} sind noch keine Sozialversicherungswerte hinterlegt; "
                f"gerechnet wird mit denen von {self.aus_jahr}. Das zu versteuernde "
                "Einkommen aus dem Bruttogehalt ist dadurch etwas ungenauer.")


def _vorsorge_einer_person(brutto: Fraction, w: dict
                           ) -> tuple[Fraction, Fraction, Fraction, Fraction]:
    """Werbungskosten, Rentenbeitrag, Basisvorsorge und alle sonstige Vorsorge EINER Person.

    Basisvorsorge: Kranken- (um 4 % fuer das Krankengeld gemindert) und
    Pflegeversicherung (§ 10 Abs. 1 Nr. 3 EStG). Alle sonstige Vorsorge: dazu
    die 4 % und die Arbeitslosenversicherung (Nr. 3a). Alles in Euro.
    """
    werbung = min(Fraction(int(w["arbeitnehmer_pauschbetrag"])), brutto)
    rv_basis = min(brutto, int(w["bbg_rv_alv"]))
    kv_basis = min(brutto, int(w["bbg_kv_pv"]))
    rente = _q(w["rv_an"]) * rv_basis
    kranken = (_q(w["kv_an"]) + _q(w["zusatzbeitrag_prozent"]) / 200) * kv_basis
    pflege = (_q(w["pv_an"]) + _q(w["pv_kinderlos"])) * kv_basis
    abschlag = _q(w["krankengeld_abschlag"])
    basis = (1 - abschlag) * kranken + pflege
    arbeitslos = _q(w["alv_an"]) * rv_basis
    return werbung, rente, basis, basis + abschlag * kranken + arbeitslos


def zve_aus_brutto(brutto_cent: int, jahr: int, *, partner_brutto_cent: int = 0,
                   zusammen: bool = False, pfad: Path | None = None) -> LohnSchaetzung:
    """Schaetzt das zvE aus dem Bruttojahreslohn (gesetzlich versicherter Arbeitnehmer).

    Die Rechnung folgt der Veranlagung, nicht der Lohnsteuer:

    * Arbeitnehmer-Pauschbetrag 1.230 € (§ 9a Satz 1 Nr. 1 Buchst. a EStG),
      hoechstens bis zum Lohn;
    * Sonderausgaben-Pauschbetrag 36 €, zusammen 72 € (§ 10c EStG);
    * Rentenbeitrag des Arbeitnehmers, 9,3 % bis zur Beitragsbemessungsgrenze
      (§ 10 Abs. 1 Nr. 2, Abs. 3 EStG -- beim Arbeitnehmer bleibt der
      Hoechstbetrag unerreicht);
    * Kranken- und Pflegeversicherung (§ 10 Abs. 1 Nr. 3 EStG): der Beitrag
      zur Krankenversicherung um 4 % gemindert, weil Anspruch auf Krankengeld
      besteht; dazu die Pflegeversicherung mit Kinderlosenzuschlag;
    * sonstige Vorsorge (§ 10 Abs. 4 EStG): diese Basisbeitraege plus die 4 %
      und die Arbeitslosenversicherung zusammen bis 1.900 € -- liegen die
      Basisbeitraege allein darueber, zaehlen sie voll und der Rest nicht.

    Bei Zusammenveranlagung gilt EIN gemeinsamer Hoechstbetrag, die Summe
    der Hoechstbetraege beider Ehegatten (§ 10 Abs. 4 Satz 3 EStG), und die
    Guenstigerpruefung vergleicht die Basisbeitraege BEIDER mit ihm (Satz 4)
    -- nicht jede Person fuer sich. Angenommen ist, dass beide gesetzlich
    versichert sind; ein Partner ohne eigenes Gehalt ist familienversichert
    und hat damit ebenfalls 1.900 € (Satz 2: Anspruch ohne eigene
    Aufwendungen).

    Nicht abgebildet: private Krankenversicherung, Minijob/Midijob, Beamte,
    hoehere Werbungskosten, Kinder, Sachsen. Das zvE wird abgerundet.

    Es wird NICHT auf 0 begrenzt: was die Abzuege ueber dem Lohn liegen,
    mindert den Gewinn, der dazukommt (zvE gesamt = zvE Gehalt + Gewinn,
    Faktenbasis). Ohne Gehalt sind das die 36 € Sonderausgaben-Pauschbetrag,
    bei 1.200 € Lohn auch die Sozialversicherung darauf. Die Steuer auf ein
    negatives zvE ist null (:meth:`Tarif.steuer`).

    >>> [zve_aus_brutto(b * 100, 2026).zve_euro for b in (20000, 50000, 80000, 120000)]
    [14714, 38684, 63761, 101770]
    >>> zve_aus_brutto(5000000, 2025).zve_euro
    38780
    >>> zve_aus_brutto(5000000, 2026, partner_brutto_cent=3000000, zusammen=True).zve_euro
    61388
    >>> zve_aus_brutto(0, 2026).zve_euro, zve_aus_brutto(0, 2026, zusammen=True).zve_euro
    (-36, -72)
    >>> zve_aus_brutto(120000, 2026).zve_euro
    -297

    Gemeinsamer Hoechstbetrag: 30.000 € und ein Partner ohne Gehalt (der
    zweite Hoechstbetrag nimmt die 4 % und die Arbeitslosenversicherung mit);
    70.000 € und 15.000 € (die Basisbeitraege beider liegen ueber 3.800 €,
    die Restvorsorge des Partners zaehlt nicht mehr):

    >>> zve_aus_brutto(3000000, 2026, partner_brutto_cent=0, zusammen=True).zve_euro
    22173
    >>> zve_aus_brutto(7000000, 2026, partner_brutto_cent=1500000, zusammen=True).zve_euro
    65410
    """
    alle = _jahre(_daten(pfad)["lohn"])
    quelle_jahr = _quelljahr(alle, jahr)
    w = alle[quelle_jahr]
    personen = [Fraction(max(0, brutto_cent), 100)]
    if zusammen:
        personen.append(Fraction(max(0, partner_brutto_cent), 100))
    werbung = rente = basis = alle = Fraction(0)
    for brutto in personen:
        if brutto <= 0:
            continue
        w_, r_, b_, a_ = _vorsorge_einer_person(brutto, w)
        werbung += w_
        rente += r_
        basis += b_
        alle += a_
    # § 10 Abs. 4 Satz 3/4: ein Hoechstbetrag je Person, bei Zusammenveranlagung
    # zusammengezaehlt; die Basisbeitraege zaehlen immer voll.
    hoechst = int(w["hoechstbetrag_sonstige_vorsorge"]) * len(personen)
    sonstige = max(basis, min(Fraction(hoechst), alle))
    pausch = int(w["sonderausgaben_pauschbetrag_zusammen" if zusammen
                   else "sonderausgaben_pauschbetrag_einzel"])
    summe = sum(personen, Fraction(0))
    einkuenfte = summe - werbung
    zve = math.floor(einkuenfte - pausch - rente - sonstige)
    return LohnSchaetzung(
        jahr=jahr, brutto_cent=math.floor(summe * 100),
        werbungskosten_cent=math.floor(werbung * 100),
        rentenversicherung_cent=math.floor(rente * 100),
        sonstige_vorsorge_cent=math.floor(sonstige * 100),
        sonderausgaben_cent=pausch * 100, einkuenfte_cent=math.floor(einkuenfte * 100),
        zve_euro=zve, aus_jahr=None if quelle_jahr == jahr else quelle_jahr,
    )


# --------------------------------------------- Gewerbesteuer und § 35 EStG

@dataclass(frozen=True, slots=True)
class Gewerbesteuer:
    gewinn_cent: int
    hebesatz_prozent: int
    #: Gewerbeertrag, auf volle 100 € abgerundet, minus Freibetrag.
    ertrag_nach_freibetrag_euro: int
    messbetrag_cent: int
    steuer_cent: int


def gewerbesteuer_werte(*, pfad: Path | None = None) -> dict:
    return _daten(pfad)["gewerbesteuer"]


def mindesthebesatz(jahr: int, *, pfad: Path | None = None) -> int:
    """Der gesetzliche Mindesthebesatz (§ 16 Abs. 4 Satz 2 GewStG).

    >>> mindesthebesatz(2026), mindesthebesatz(2027), mindesthebesatz(2030)
    (200, 280, 280)
    """
    bekannt = _jahre(gewerbesteuer_werte(pfad=pfad)["mindesthebesatz"])
    return int(bekannt[_quelljahr(bekannt, jahr)])


def gewerbesteuer(gewinn_cent: int, hebesatz_prozent: int, *,
                  pfad: Path | None = None) -> Gewerbesteuer:
    """Gewerbesteuer eines Einzelunternehmers in Cent (§§ 11, 16 GewStG).

    Gewerbeertrag = Gewinn (ohne Hinzurechnungen und Kuerzungen), auf volle
    100 € abgerundet, minus 24.500 € Freibetrag; davon 3,5 % ist der
    Messbetrag, mal Hebesatz die Steuer. Nur fuer Gewerbebetriebe --
    Freiberufler (§ 18 EStG) zahlen keine.

    >>> g = gewerbesteuer(6000000, 400); (g.messbetrag_cent, g.steuer_cent)
    (124250, 497000)
    >>> gewerbesteuer(6000000, 410).steuer_cent, gewerbesteuer(2459900, 400).steuer_cent
    (509425, 0)
    >>> gewerbesteuer(2460000, 400).messbetrag_cent, gewerbesteuer(-500000, 400).steuer_cent
    (350, 0)
    """
    w = gewerbesteuer_werte(pfad=pfad)
    stufe = int(w["abrunden_auf_euro"])
    ertrag = (math.floor(Fraction(gewinn_cent, 100)) // stufe) * stufe
    rest = max(0, ertrag - int(w["freibetrag_euro"]))
    messbetrag = _q(w["messzahl"]) * rest * 100
    steuer = messbetrag * hebesatz_prozent / 100
    return Gewerbesteuer(
        gewinn_cent=gewinn_cent, hebesatz_prozent=hebesatz_prozent,
        ertrag_nach_freibetrag_euro=rest, messbetrag_cent=math.floor(messbetrag),
        steuer_cent=math.floor(steuer),
    )


def ermaessigung_35(gewerbe: Gewerbesteuer, est_cent: int, anteil_gewerbe: Fraction, *,
                    pfad: Path | None = None) -> int:
    """Die Anrechnung der Gewerbesteuer auf die Einkommensteuer (§ 35 EStG), in Cent.

    Das Kleinste aus drei Grenzen: 4,0 × Messbetrag, die tatsaechlich zu
    zahlende Gewerbesteuer und der Ermaessigungshoechstbetrag -- die
    Einkommensteuer, soweit sie anteilig auf die gewerblichen Einkuenfte
    entfaellt. Bis 400 % Hebesatz wird die Gewerbesteuer dadurch meist voll
    verrechnet; darueber bleibt ein Rest.

    >>> g = gewerbesteuer(6000000, 410)
    >>> ermaessigung_35(g, 1423300, Fraction(1)), ermaessigung_35(g, 100000, Fraction(1, 2))
    (497000, 50000)
    """
    faktor = _q(gewerbesteuer_werte(pfad=pfad)["anrechnung_faktor"])
    anteil = min(Fraction(1), max(Fraction(0), anteil_gewerbe))
    grenze = min(faktor * gewerbe.messbetrag_cent, Fraction(gewerbe.steuer_cent),
                 Fraction(max(0, est_cent)) * anteil)
    return max(0, math.floor(grenze))

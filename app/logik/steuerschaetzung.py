"""Steuerrücklage: wie viel Einkommensteuer das Gewerbe voraussichtlich zusätzlich kostet.

Wer neben einer Anstellung ein Gewerbe betreibt, zahlt auf das Gehalt schon
Lohnsteuer -- auf den Gewinn nicht. Die Rechnung dafür kommt erst mit dem
Steuerbescheid, oft eineinhalb Jahre später und in einem Betrag. Dieses
Modul schätzt sie, damit das Geld dann da ist.

Die Kernrechnung ist die **Mehrsteuer durch das Gewerbe**:

    ESt(zvE ohne Gewerbe + Gewinn) − ESt(zvE ohne Gewerbe)

jeweils mit Solidaritätszuschlag und Kirchensteuer, bei Zusammenveranlagung
mit dem Splittingtarif auf das gemeinsame zvE. Dazu kommt oberhalb des
Freibetrags die Gewerbesteuer, abzüglich ihrer Anrechnung auf die
Einkommensteuer (§ 35 EStG). Warum die Differenz und nicht "Gewinn mal
Durchschnittssteuersatz": die Steuer auf das Gehalt ist durch den
Lohnsteuerabzug bezahlt (in Steuerklasse I/IV bis auf wenige Euro), und jeder
Euro Gewinn kommt OBEN drauf -- er kostet den Satz, der dort gilt, nicht den
Durchschnitt.

Das zvE ohne Gewerbe stammt entweder aus dem letzten Steuerbescheid
(genauer) oder ist aus dem Bruttogehalt geschätzt
(:func:`steuertarif.zve_aus_brutto`). Wer einen eigenen Satz einträgt,
bekommt Gewinn × Satz -- eine reine Schätzung, und die Seite sagt das.

Nicht berücksichtigt: andere Einkünfte (Kapital, Vermietung), Sonderausgaben
über die Pauschalen hinaus, außergewöhnliche Belastungen, Kinderfreibeträge,
Steuerklasse III/V (die Nachzahlung kann dann höher sein),
Kirchensteuer-Kappung. Keine Umsatzsteuer -- der Betrieb ist Kleinunternehmer.

**Vorauszahlungen** stehen je Termin da, wie im Bescheid: vier an das
Finanzamt (Einkommensteuer mit Soli und Kirchensteuer), vier an die Gemeinde
(Gewerbesteuer). Eine Rate zaehlt ab ihrer Faelligkeit als gezahlt -- das
Finanzamt zieht sie per Lastschrift ein --, ein Haken uebersteuert das. Die
"mindestens"-Zahl ist das Groessere aus der Reststeuer und dem, was noch
abgebucht wird: das Finanzamt bucht die festgesetzte Rate ab, egal was die
Schaetzung sagt.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from fractions import Fraction

from app import db
from app.logik import eingabe, steuertarif
from app.logik.kunden import euro_nach_cent
from app.logik.kurse import ostersonntag
from app.logik.steuertarif import Gewerbesteuer, LohnSchaetzung, Stufe, Tarif

VERANLAGUNGEN = ("EINZEL", "ZUSAMMEN")
VERANLAGUNG_TEXT = {"EINZEL": "Einzelveranlagung", "ZUSAMMEN": "Zusammenveranlagung"}

#: Die Rücklage wird auf volle 10 € aufgerundet: genauer ist die Schätzung nicht,
#: und ein krummer Betrag täuscht eine Genauigkeit vor, die sie nicht hat.
RUNDUNG_CENT = 1000


def hebesatz_vorschlag() -> int:
    """Der Hebesatz, mit dem das Formular vorbelegt ist (Durchschnitt aller Gemeinden)."""
    return int(steuertarif.gewerbesteuer_werte()["hebesatz_vorschlag"])


# ------------------------------------------------------------- Vorauszahlungen

#: Die gesetzlichen Termine je Quartal als (Monat, Tag). Finanzamt: § 37 Abs. 1
#: Satz 1 EStG; Soli und Kirchensteuer sind zugleich faellig (§ 1 Abs. 4 SolZG,
#: § 51a Abs. 4 EStG) und stehen im Bescheid unter demselben Termin -- EIN Betrag
#: je Termin, damit die Kirchensteuer nicht doppelt zurueckgelegt wird.
#: Gemeinde: § 19 Abs. 1 GewStG.
TERMINE = {
    "FINANZAMT": ((3, 10), (6, 10), (9, 10), (12, 10)),
    "GEMEINDE": ((2, 15), (5, 15), (8, 15), (11, 15)),
}
EMPFAENGER = tuple(TERMINE)
EMPFAENGER_TEXT = {"FINANZAMT": "Finanzamt", "GEMEINDE": "Gemeinde"}
#: Was je Termin dazugehoert, fuer Formular und Hinweise.
EMPFAENGER_STEUERN = {"FINANZAMT": "Einkommensteuer + Soli + Kirchensteuer",
                      "GEMEINDE": "Gewerbesteuer"}
#: Wo die Herabsetzung geregelt ist.
HERABSETZUNG = {"FINANZAMT": "§ 37 Abs. 3 EStG", "GEMEINDE": "§ 19 Abs. 3 GewStG"}

#: So viele Tage vor dem Termin nennt die Uebersicht die naechste Rate.
ERINNERUNG_TAGE = 14


def ist_feiertag(tag: date) -> bool:
    """Bundeseinheitlicher gesetzlicher Feiertag?

    Neujahr, Karfreitag, Ostermontag, 1. Mai, Christi Himmelfahrt,
    Pfingstmontag, 3. Oktober, 1. und 2. Weihnachtstag. Feiertage einzelner
    Laender (etwa 15.08. im Saarland) zaehlen nicht -- dort ist die Rate
    einen Tag spaeter faellig, die Seite nennt sie einen Tag zu frueh.

    >>> ist_feiertag(date(2019, 6, 10)), ist_feiertag(date(2026, 5, 14))
    (True, True)
    >>> ist_feiertag(date(2026, 8, 15)), ist_feiertag(date(2026, 10, 3))
    (False, True)
    """
    if (tag.month, tag.day) in ((1, 1), (5, 1), (10, 3), (12, 25), (12, 26)):
        return True
    ostern = ostersonntag(tag.year)
    return (tag - ostern).days in (-2, 1, 39, 50)


def faellig_am(jahr: int, empfaenger: str, quartal: int) -> date:
    """Der Faelligkeitstag einer Rate: der Termin, an Wochenende oder Feiertag der naechste Werktag.

    § 108 Abs. 3 AO verschiebt das Ende einer Frist, die auf einen Samstag,
    Sonntag oder Feiertag faellt; nach dem AEAO zu § 108 gilt das auch fuer
    Faelligkeitstage -- auch fuer die Gewerbesteuer der Gemeinde.

    >>> faellig_am(2026, "GEMEINDE", 4), faellig_am(2026, "FINANZAMT", 4)
    (datetime.date(2026, 11, 16), datetime.date(2026, 12, 10))
    >>> faellig_am(2028, "FINANZAMT", 2), faellig_am(2019, "FINANZAMT", 2)
    (datetime.date(2028, 6, 12), datetime.date(2019, 6, 11))
    """
    monat, tag_ = TERMINE[empfaenger][quartal - 1]
    tag = date(jahr, monat, tag_)
    while tag.weekday() >= 5 or ist_feiertag(tag):
        tag += timedelta(days=1)
    return tag


@dataclass(frozen=True, slots=True)
class Rate:
    """Eine festgesetzte Vorauszahlung, wie im Bescheid. Betrag in Cent."""

    empfaenger: str
    quartal: int
    betrag_cent: int
    #: None: nach Faelligkeit (faellig heisst gezahlt); True: schon gezahlt
    #: (vor dem Termin); False: noch nicht gezahlt (auch nach dem Termin).
    gezahlt: bool | None = None


@dataclass(frozen=True, slots=True)
class Termin:
    """Eine Rate mit Datum und Stand an einem Tag."""

    rate: Rate
    #: Der Termin laut Gesetz und der Tag, an dem die Rate faellig ist.
    termin: date
    faellig: date
    #: Wie gerechnet wird: gezahlt oder noch offen.
    gezahlt: bool
    ueberfaellig: bool

    @property
    def empfaenger(self) -> str:
        return self.rate.empfaenger

    @property
    def quartal(self) -> int:
        return self.rate.quartal

    @property
    def betrag_cent(self) -> int:
        return self.rate.betrag_cent

    @property
    def von_hand(self) -> bool:
        """Weicht der Stand von der Regel "faellig heisst gezahlt" ab?"""
        return self.rate.gezahlt is not None


def nach_faelligkeit_gezahlt(jahr: int, empfaenger: str, quartal: int, stand: date) -> bool:
    """Die Regel ohne Haken: ab dem Faelligkeitstag gilt eine Rate als gezahlt.

    Der Tag selbst zaehlt schon: die Lastschrift bucht am Faelligkeitstag ab
    (§ 224 Abs. 2 Nr. 3 AO).

    >>> nach_faelligkeit_gezahlt(2026, "FINANZAMT", 4, date(2026, 12, 9))
    False
    >>> nach_faelligkeit_gezahlt(2026, "FINANZAMT", 4, date(2026, 12, 10))
    True
    """
    return stand >= faellig_am(jahr, empfaenger, quartal)


def termine(jahr: int, raten: Collection[Rate], heute: date) -> tuple[Termin, ...]:
    """Die Raten eines Jahres mit Datum und Stand, nach Faelligkeit sortiert.

    >>> t = termine(2026, [Rate("FINANZAMT", 3, 100000), Rate("FINANZAMT", 4, 100000),
    ...                    Rate("GEMEINDE", 4, 5000, gezahlt=False)], date(2026, 11, 20))
    >>> [(x.faellig.isoformat(), x.gezahlt, x.ueberfaellig) for x in t]
    [('2026-09-10', True, False), ('2026-11-16', False, True), ('2026-12-10', False, False)]
    """
    liste = []
    for rate in raten:
        termin_ = date(jahr, *TERMINE[rate.empfaenger][rate.quartal - 1])
        faellig = faellig_am(jahr, rate.empfaenger, rate.quartal)
        gezahlt = heute >= faellig if rate.gezahlt is None else rate.gezahlt
        liste.append(Termin(rate, termin_, faellig, gezahlt,
                            ueberfaellig=not gezahlt and heute >= faellig))
    return tuple(sorted(liste, key=lambda x: (x.faellig, EMPFAENGER.index(x.empfaenger))))


@dataclass(frozen=True, slots=True)
class Abweichung:
    """Festgesetzte Vorauszahlungen gegen die Schaetzung, je Empfaenger.

    ``betrag_cent`` positiv: die Raten liegen ueber der Schaetzung
    (Herabsetzung beantragen); negativ: darunter (Nachzahlung, Zinsen).
    ``empfaenger`` ist None bei einem eigenen Satz -- dann gibt es nur die
    Summe.
    """

    empfaenger: str | None
    betrag_cent: int
    termine: int

    @property
    def zu_hoch(self) -> bool:
        return self.betrag_cent > 0

    @property
    def vorschrift(self) -> str:
        if self.empfaenger is None:
            return f"{HERABSETZUNG['FINANZAMT']} bzw. {HERABSETZUNG['GEMEINDE']}"
        return HERABSETZUNG[self.empfaenger]


@dataclass(frozen=True, slots=True)
class Eingaben:
    """Was der Nutzer für ein Jahr angibt. Beträge in Cent, Sätze in Promille."""

    jahr: int
    brutto_lohn_cent: int | None = None
    partner_brutto_cent: int | None = None
    zve_ohne_cent: int | None = None
    veranlagung: str = "EINZEL"
    kirchensteuer_prozent: int = 0
    satz_manuell_promille: int | None = None
    gewinn_erwartet_cent: int | None = None
    #: Weitere gezahlte Vorauszahlungen OHNE Termin (z. B. eine nachtraegliche
    #: Erhoehung nach § 37 Abs. 4 EStG). Bis Version 22 war das die einzige
    #: Zahl fuer alle Vorauszahlungen; ein gespeicherter Altwert steht hier.
    vorauszahlungen_cent: int = 0
    hebesatz_prozent: int = field(default_factory=hebesatz_vorschlag)
    tarif_entwurf: bool = False
    #: Die festgesetzten Raten, nach Empfaenger und Quartal sortiert.
    raten: tuple[Rate, ...] = ()

    @property
    def zusammen(self) -> bool:
        return self.veranlagung == "ZUSAMMEN"

    @property
    def fehlt(self) -> str | None:
        """Was für die berechnete Schätzung noch fehlt, als Satzteil; None, wenn nichts.

        Ohne Gehalt oder zvE gibt es keine ehrliche Schätzung: ohne Angabe
        hiesse "kein anderes Einkommen", und die Steuer auf den Gewinn waere
        viel zu niedrig. Wer kein Gehalt hat, traegt 0 ein. Dasselbe gilt fuer
        das Gehalt des Partners bei Zusammenveranlagung: leer hiesse Splitting
        gegen einen Partner ohne Einkommen -- die guenstigste Annahme, und die
        Ruecklage laege deutlich zu niedrig. Das zvE aus dem Bescheid ist bei
        Zusammenveranlagung schon das gemeinsame und braucht das nicht.

        >>> print(Eingaben(jahr=2026).fehlt)
        das Bruttogehalt (0, wenn du keines beziehst) oder das zu versteuernde Einkommen
        >>> Eingaben(jahr=2026, brutto_lohn_cent=0).fehlt is None
        True
        >>> print(Eingaben(jahr=2026, brutto_lohn_cent=0, veranlagung="ZUSAMMEN").fehlt)
        das Bruttogehalt deines Partners oder deiner Partnerin (0, wenn kein Gehalt)
        >>> Eingaben(jahr=2026, zve_ohne_cent=0, veranlagung="ZUSAMMEN").fehlt is None
        True
        """
        if self.zve_ohne_cent is not None:
            return None
        if self.brutto_lohn_cent is None:
            return ("das Bruttogehalt (0, wenn du keines beziehst) oder das zu versteuernde "
                    "Einkommen")
        if self.zusammen and self.partner_brutto_cent is None:
            return "das Bruttogehalt deines Partners oder deiner Partnerin (0, wenn kein Gehalt)"
        return None

    @property
    def berechenbar(self) -> bool:
        """Reichen die Angaben für die berechnete Schätzung? Siehe :attr:`fehlt`."""
        return self.fehlt is None

    @property
    def rechenbar(self) -> bool:
        """Gibt es eine Rücklage? Mit eigenem Satz auch ohne Gehalt: Gewinn × Satz.

        >>> Eingaben(jahr=2026, satz_manuell_promille=300).rechenbar
        True
        >>> Eingaben(jahr=2026, satz_manuell_promille=300).berechenbar
        False
        """
        return self.berechenbar or self.satz_manuell_promille is not None


def aufrunden(cent: int, auf: int = RUNDUNG_CENT) -> int:
    """Auf volle ``auf`` Cent aufrunden (Vorgabe: 10 €); negative Werte bleiben unberührt.

    >>> aufrunden(348001), aufrunden(348000), aufrunden(1), aufrunden(0), aufrunden(-500)
    (349000, 348000, 1000, 0, -500)
    """
    if cent <= 0:
        return cent
    return -(-cent // auf) * auf


def _cent(wert: Fraction) -> int:
    """Kaufmaennisch auf ganze Cent."""
    return math.floor(wert + Fraction(1, 2))


@dataclass(frozen=True, slots=True)
class Hochrechnung:
    """Der bisherige Gewinn, gleichmaessig aufs ganze Jahr weitergerechnet."""

    bisher_cent: int
    stichtag: date
    jahr_cent: int

    @property
    def monate(self) -> int:
        """Die angefangenen Monate bis zum Stichtag, durch die geteilt wurde."""
        return self.stichtag.month


def hochrechnung(gewinn_bisher_cent: int, jahr: int, heute: date) -> Hochrechnung | None:
    """Nur fürs laufende Jahr: bisheriger Gewinn ÷ vergangene Monate × 12 (Entscheidung 6).

    Vergangene Monate sind die angefangenen einschließlich des laufenden --
    im Januar also 1. Nach Tagen gerechnet wären es am 2. Januar aus 1.500 €
    über 270.000 €; nach Monaten sind es 18.000 €. Im Dezember gibt es
    nichts mehr hochzurechnen.

    Gerundet auf 10 €. Ein Hinweis, keine Vorgabe -- Einnahmen kommen selten
    gleichmäßig, und wer weiß, dass im Dezember eine große Rechnung bezahlt
    wird, trägt den erwarteten Gewinn selbst ein.

    >>> hochrechnung(750000, 2026, date(2026, 10, 25)).jahr_cent
    900000
    >>> hochrechnung(150000, 2027, date(2027, 1, 2)).jahr_cent
    1800000
    >>> hochrechnung(750000, 2025, date(2026, 10, 25)) is None
    True
    >>> hochrechnung(0, 2026, date(2026, 10, 25)) is None
    True
    >>> hochrechnung(750000, 2026, date(2026, 12, 1)) is None
    True
    """
    if jahr != heute.year or gewinn_bisher_cent == 0:
        return None
    monate = heute.month
    if monate >= 12:
        return None
    hoch = Fraction(gewinn_bisher_cent * 12, monate)
    return Hochrechnung(gewinn_bisher_cent, heute, _cent(hoch / RUNDUNG_CENT) * RUNDUNG_CENT)


@dataclass(frozen=True, slots=True)
class Ergebnis:
    """Die Schätzung eines Jahres, mit allem, was die Seite zeigt."""

    eingaben: Eingaben
    tarif: Tarif
    gewinn_cent: int
    #: True: Gewinn aus der EÜR; False: der eingetragene erwartete Gewinn.
    gewinn_aus_euer: bool
    gewinn_bisher_cent: int
    einnahmen_bisher_cent: int
    #: 'ZVE' (aus dem Bescheid) oder 'BRUTTO' (geschätzt).
    zve_quelle: str
    lohn: LohnSchaetzung | None
    zve_ohne_euro: int
    #: Gibt es Arbeitslohn? Nur dann gilt der Härteausgleich.
    arbeitslohn: bool
    haerteausgleich_cent: int
    zve_mit_euro: int
    est_ohne_cent: int
    #: Tarifliche ESt mit Gewerbe, VOR der Anrechnung nach § 35.
    est_mit_cent: int
    anrechnung_35_cent: int
    soli_ohne_cent: int
    soli_mit_cent: int
    kist_ohne_cent: int
    kist_mit_cent: int
    gewerbe: Gewerbesteuer
    hinweise: tuple[str, ...] = ()
    hochrechnung: Hochrechnung | None = None
    #: False: nur der eigene Satz, ohne Gehalt oder zvE -- dann gibt es keinen
    #: berechneten Vergleich, und alle Steuerbetraege oben sind 0.
    berechnet: bool = True
    #: Liegt der erwartete Jahresgewinn UNTER dem Gewinn laut EUeR: was allein
    #: der EUeR-Gewinn noch offen liesse. Sonst None.
    offen_laut_euer_cent: int | None = None
    #: Die Raten mit Datum und Stand am Tag der Schaetzung.
    termine: tuple[Termin, ...] = ()

    @property
    def erwartet_unter_euer(self) -> bool:
        """Ist der erwartete Jahresgewinn kleiner als der schon erzielte laut EÜR?

        Dann drückt ein früh eingetragener, inzwischen überholter Wert das
        „mindestens“ -- oder bis Jahresende kommen noch Ausgaben. Gerechnet
        wird weiter mit dem erwarteten Wert, die Seite fragt nach.
        """
        return self.offen_laut_euer_cent is not None

    # ----------------------------------------------------------- Bausteine

    @property
    def mehr_est_tariflich_cent(self) -> int:
        return self.est_mit_cent - self.est_ohne_cent

    @property
    def mehr_est_cent(self) -> int:
        """Einkommensteuer durch das Gewerbe, nach der Gewerbesteuer-Anrechnung."""
        return self.est_mit_cent - self.anrechnung_35_cent - self.est_ohne_cent

    @property
    def mehr_soli_cent(self) -> int:
        return self.soli_mit_cent - self.soli_ohne_cent

    @property
    def mehr_kist_cent(self) -> int:
        return self.kist_mit_cent - self.kist_ohne_cent

    @property
    def gewst_cent(self) -> int:
        return self.gewerbe.steuer_cent

    @property
    def mehrsteuer_berechnet_cent(self) -> int:
        return self.mehr_est_cent + self.mehr_soli_cent + self.mehr_kist_cent + self.gewst_cent

    # -------------------------------------------------------------- Sätze

    @property
    def satz_berechnet(self) -> Fraction | None:
        """Durchschnittlicher Satz auf den Gewinn: Mehrsteuer ÷ Gewinn (alles zusammen)."""
        if self.gewinn_cent <= 0 or not self.berechnet:
            return None
        return Fraction(max(0, self.mehrsteuer_berechnet_cent), self.gewinn_cent)

    @property
    def grenzsatz(self) -> Fraction:
        """Grenzsteuersatz der Einkommensteuer beim zvE mit Gewerbe (ohne Zuschläge)."""
        x = self.zve_mit_euro // 2 if self.eingaben.zusammen else self.zve_mit_euro
        return self.tarif.grenzsatz(x)

    @property
    def satz_vorschlag_promille(self) -> int | None:
        """Der berechnete Satz für „übernehmen“: auf 0,1 % AUFgerundet, höchstens 100 %.

        Aufgerundet, damit Gewinn × Satz nie unter der berechneten Steuer liegt
        -- sonst sänke das „mindestens“ durch das Übernehmen (26.840 € wurden
        mit 44,7 % zu 26.820 €). Über 100 % (Kleinstgewinn, auf volle Euro
        abgerundete Einkommensteuer) gibt es keinen Vorschlag: das Feld wiese
        ihn ab.
        """
        satz = self.satz_berechnet
        if satz is None:
            return None
        promille = math.ceil(satz * 1000)
        return promille if promille <= 1000 else None

    @property
    def satz_vorschlag(self) -> Fraction | None:
        promille = self.satz_vorschlag_promille
        return None if promille is None else Fraction(promille, 1000)

    @property
    def haertebereich(self) -> str | None:
        """Kostet der nächste Euro wegen des Härteausgleichs anders als der Grenzsatz?

        ``'frei'``: Gewinn unter 410 € neben Arbeitslohn -- der nächste Euro
        bleibt steuerfrei. ``'doppelt'``: 410 € bis unter 820 € -- der Abzug
        schmilzt ab, jeder Euro Gewinn erhöht das zvE um 2 €. Sonst ``None``.
        """
        if not self.arbeitslohn or self.gewinn_cent <= 0:
            return None
        werte = steuertarif.veranlagungswerte()
        if self.gewinn_cent < 100 * werte["haerteausgleich_voll_bis_euro"]:
            return "frei"
        if self.gewinn_cent < 100 * werte["haerteausgleich_gleitend_bis_euro"]:
            return "doppelt"
        return None

    @property
    def zuschlaege_grenze(self) -> list[str]:
        """Was den nächsten Euro über die Einkommensteuer hinaus verteuert, für den Satztext."""
        namen = []
        if self.soli_mit_cent:
            namen.append("Soli")
        if self.kist_mit_cent:
            namen.append("Kirchensteuer")
        if self.gewst_cent:
            namen.append("Gewerbesteuer")
        return namen

    @property
    def satz_manuell(self) -> Fraction | None:
        promille = self.eingaben.satz_manuell_promille
        return None if promille is None else Fraction(promille, 1000)

    @property
    def mit_eigenem_satz(self) -> bool:
        return self.satz_manuell is not None

    # ----------------------------------------------------------- Ergebnis

    @property
    def mehrsteuer_cent(self) -> int:
        """Die Steuer auf den Gewinn, mit der weitergerechnet wird.

        Mit eigenem Satz: Gewinn × Satz (der Satz ist der GESAMTsatz, Soli,
        Kirchen- und Gewerbesteuer schon drin). Sonst die berechnete
        Mehrsteuer. Negativ heißt: der Verlust spart Steuer.
        """
        if self.satz_manuell is not None:
            return _cent(self.gewinn_cent * self.satz_manuell)
        return self.mehrsteuer_berechnet_cent

    @property
    def satz(self) -> Fraction | None:
        """Der Satz, der gilt: der eigene, sonst der berechnete."""
        return self.satz_manuell if self.satz_manuell is not None else self.satz_berechnet

    @property
    def ruecklage_cent(self) -> int:
        """Steuer auf den Gewinn, auf 10 € aufgerundet; nie negativ."""
        return aufrunden(max(0, self.mehrsteuer_cent))

    # --------------------------------------------------- Vorauszahlungen

    @property
    def vz_gezahlt_cent(self) -> int:
        """Schon gezahlt: die weiteren ohne Termin und jede Rate, die als gezahlt gilt."""
        return self.eingaben.vorauszahlungen_cent + sum(
            t.betrag_cent for t in self.termine if t.gezahlt)

    @property
    def vz_offen(self) -> tuple[Termin, ...]:
        """Die Raten, die noch abgebucht werden: kuenftige und ueberfaellige."""
        return tuple(t for t in self.termine if not t.gezahlt)

    @property
    def vz_noch_faellig_cent(self) -> int:
        return sum(t.betrag_cent for t in self.vz_offen)

    @property
    def vz_gesamt_cent(self) -> int:
        """Alle Vorauszahlungen des Jahres, gezahlt oder noch faellig."""
        return self.vz_gezahlt_cent + self.vz_noch_faellig_cent

    @property
    def ueberfaellig(self) -> tuple[Termin, ...]:
        """Faellige Raten, deren Haken auf "noch nicht gezahlt" steht."""
        return tuple(t for t in self.termine if t.ueberfaellig)

    @property
    def reststeuer_cent(self) -> int:
        """Steuer auf den Gewinn minus schon gezahlt -- exakt, ungerundet."""
        return self.mehrsteuer_cent - self.vz_gezahlt_cent

    @property
    def bescheid_cent(self) -> int:
        """Was nach allen Raten mit dem Bescheid kommt: positiv Nachzahlung, negativ Erstattung."""
        return self.reststeuer_cent - self.vz_noch_faellig_cent

    @property
    def offen_cent(self) -> int:
        """Was mindestens zurueckzulegen ist, auf 10 € aufgerundet.

        Das Groessere aus der Reststeuer und den noch faelligen Raten: das
        Finanzamt bucht die festgesetzte Rate ab, auch wenn die Schaetzung
        darunter liegt (4 × 1.200 € bei 4.393 € Steuer: am 10.12. werden
        1.200 € abgebucht, obwohl nur noch 793 € Steuer offen sind).
        """
        return aufrunden(max(0, self.reststeuer_cent, self.vz_noch_faellig_cent))

    @property
    def zurueck_cent(self) -> int:
        """Voraussichtliche Erstattung mit dem Bescheid -- nach allen Raten, dazu ein Verlust."""
        return max(0, -self.bescheid_cent)

    @property
    def vz_abweichungen(self) -> tuple[Abweichung, ...]:
        """Wo die festgesetzten Raten um mindestens 100 € je Termin von der Schaetzung abweichen.

        Je Empfaenger getrennt, damit der Hinweis die richtige Stelle nennt:
        Finanzamt gegen Einkommensteuer, Soli und Kirchensteuer (dazu die
        weiteren Vorauszahlungen ohne Termin), Gemeinde gegen die
        Gewerbesteuer. Mit eigenem Satz gibt es nur die Summe. 100 € je
        Termin ist die Grenze, ab der das Finanzamt anpasst (§ 37 Abs. 5
        Satz 2 EStG). Ohne Gewinn in der EUeR gibt es noch nichts zu vergleichen.
        """
        if not self.termine or (self.gewinn_cent == 0 and self.gewinn_aus_euer):
            return ()
        grenze = 100 * steuertarif.veranlagungswerte()["vorauszahlung_est_mindestens_termin_euro"]
        if self.mit_eigenem_satz or not self.berechnet:
            vergleiche = [(None, self.vz_gesamt_cent, max(0, self.mehrsteuer_cent),
                           len(self.termine))]
        else:
            vergleiche = []
            for empfaenger in EMPFAENGER:
                eigene = [t for t in self.termine if t.empfaenger == empfaenger]
                if not eigene:
                    continue
                festgesetzt = sum(t.betrag_cent for t in eigene)
                if empfaenger == "FINANZAMT":
                    festgesetzt += self.eingaben.vorauszahlungen_cent
                    geschaetzt = self.mehr_est_cent + self.mehr_soli_cent + self.mehr_kist_cent
                else:
                    geschaetzt = self.gewst_cent
                vergleiche.append((empfaenger, festgesetzt, max(0, geschaetzt), len(eigene)))
        return tuple(
            Abweichung(empfaenger, festgesetzt - geschaetzt, anzahl)
            for empfaenger, festgesetzt, geschaetzt, anzahl in vergleiche
            if abs(festgesetzt - geschaetzt) >= grenze * anzahl
        )

    @property
    def cent_je_euro(self) -> int | None:
        """Faustregel: so viele Cent von jedem eingenommenen Euro zurücklegen.

        Steuersatz auf den Gewinn mal Gewinnanteil der bisherigen Einnahmen
        (Gewinn ÷ Betriebseinnahmen laut EÜR), aufgerundet.
        """
        satz = self.satz
        if (satz is None or satz <= 0 or self.einnahmen_bisher_cent <= 0
                or self.gewinn_bisher_cent <= 0 or self.mehrsteuer_cent <= 0):
            return None
        marge = Fraction(self.gewinn_bisher_cent, self.einnahmen_bisher_cent)
        return min(100, math.ceil(satz * marge * 100))

    # --------------------------------------------------------- Abstufungen

    @property
    def stufen(self) -> list[Stufe]:
        return self.tarif.stufen(faktor=2 if self.eingaben.zusammen else 1)

    def _zone(self, zve_euro: int) -> int | None:
        if not self.berechnet:
            return None
        x = max(0, zve_euro)
        return self.tarif.zone(x // 2 if self.eingaben.zusammen else x)

    @property
    def zone_ohne(self) -> int | None:
        return self._zone(self.zve_ohne_euro)

    @property
    def zone_mit(self) -> int | None:
        return self._zone(self.zve_mit_euro)

    @property
    def zve_ohne_gezeigt_euro(self) -> int:
        """Das zvE ohne Gewerbe, wie die Seite es zeigt: nie unter 0 €.

        Gerechnet wird mit dem ungekappten Wert -- ohne Gehalt sind das −36 €
        (Sonderausgaben-Pauschbetrag), die den Gewinn mindern. Ein negatives
        zu versteuerndes Einkommen steht in keinem Bescheid; der Rechenweg
        nennt den Abzug stattdessen.
        """
        return max(0, self.zve_ohne_euro)

    # ------------------------------------------------------------ Hinweise

    @property
    def pflichtveranlagung(self) -> bool:
        """§ 46 Abs. 2 Nr. 1 EStG: Nebeneinkünfte über 410 € neben Arbeitslohn."""
        grenze = steuertarif.veranlagungswerte()["pflicht_ueber_euro"] * 100
        return self.arbeitslohn and self.gewinn_cent > grenze

    @property
    def vorauszahlungen_wahrscheinlich(self) -> bool:
        """§ 37 Abs. 5 EStG: Vorauszahlungen ab 400 € Einkommensteuer im Jahr."""
        grenze = steuertarif.veranlagungswerte()["vorauszahlung_est_mindestens_jahr_euro"] * 100
        return self.mehr_est_cent >= grenze


def schaetze(
    eingaben: Eingaben, *, gewinn_bisher_cent: int, einnahmen_bisher_cent: int = 0,
    heute: date | None = None,
) -> Ergebnis:
    """Rechnet die Schätzung für ein Jahr.

    ``gewinn_bisher_cent`` ist der Gewinn laut EÜR; ein eingetragener
    erwarteter Jahresgewinn geht vor.

    >>> e = Eingaben(jahr=2026, brutto_lohn_cent=4000000)
    >>> r = schaetze(e, gewinn_bisher_cent=1000000)
    >>> r.zve_ohne_euro, r.zve_mit_euro, r.mehr_est_cent, r.ruecklage_cent
    (30694, 40694, 301700, 302000)
    """
    heute = heute or date.fromisoformat(db.heute())
    jahr = eingaben.jahr
    zusammen = eingaben.zusammen
    tarif = steuertarif.tarif_fuer(jahr, entwurf=eingaben.tarif_entwurf)
    steuer = tarif.splitting if zusammen else tarif.steuer
    hinweise: list[str] = []
    if tarif.hinweis:
        hinweise.append(tarif.hinweis)
    freigrenze = steuertarif.soli_freigrenze(jahr)
    if freigrenze.hinweis:
        hinweise.append(freigrenze.hinweis)

    gewinn = (eingaben.gewinn_erwartet_cent if eingaben.gewinn_erwartet_cent is not None
              else gewinn_bisher_cent)

    vz_termine = termine(jahr, eingaben.raten, heute)

    if not eingaben.berechenbar:
        if eingaben.satz_manuell_promille is None:
            raise ValueError(f"Für eine Schätzung fehlt noch {eingaben.fehlt}.")
        # Nur der eigene Satz: Gewinn × Satz braucht kein Gehalt. Der
        # berechnete Vergleich fehlt, und die Seite sagt, was ihm fehlt.
        return _mit_euer_vergleich(Ergebnis(
            eingaben=eingaben, tarif=tarif, gewinn_cent=gewinn,
            gewinn_aus_euer=eingaben.gewinn_erwartet_cent is None,
            gewinn_bisher_cent=gewinn_bisher_cent, einnahmen_bisher_cent=einnahmen_bisher_cent,
            zve_quelle="KEINE", lohn=None, zve_ohne_euro=0, arbeitslohn=False,
            haerteausgleich_cent=0, zve_mit_euro=0, est_ohne_cent=0, est_mit_cent=0,
            anrechnung_35_cent=0, soli_ohne_cent=0, soli_mit_cent=0, kist_ohne_cent=0,
            kist_mit_cent=0, gewerbe=steuertarif.gewerbesteuer(0, eingaben.hebesatz_prozent),
            hinweise=(f"Gerechnet nur mit deinem Satz. Für einen berechneten Vergleich fehlt "
                      f"noch {eingaben.fehlt}.",),
            hochrechnung=hochrechnung(gewinn_bisher_cent, jahr, heute), berechnet=False,
            termine=vz_termine,
        ), heute)

    # Brutto -> zvE immer rechnen, wenn ein Gehalt da ist: auch neben dem
    # zvE aus dem Bescheid dient es dem Vergleich und dem § 35-Anteil.
    # Bei Zusammenveranlagung ohne Partnergehalt gibt es keine Schaetzung aus
    # dem Brutto -- auch nicht zum Vergleich neben dem zvE aus dem Bescheid.
    lohn = None
    if eingaben.brutto_lohn_cent is not None and not (
            zusammen and eingaben.partner_brutto_cent is None):
        lohn = steuertarif.zve_aus_brutto(
            eingaben.brutto_lohn_cent, jahr,
            partner_brutto_cent=eingaben.partner_brutto_cent or 0, zusammen=zusammen,
        )
    if eingaben.zve_ohne_cent is not None:
        zve_quelle = "ZVE"
        zve_ohne = eingaben.zve_ohne_cent // 100
        # Wer ein zvE aus dem Bescheid eintraegt, ist hier Arbeitnehmer (die
        # Seite fragt danach); ohne Gehaltsangabe wird das angenommen.
        arbeitslohn = (zve_ohne > 0 if lohn is None else lohn.brutto_cent > 0)
        # Fuer den § 35-Anteil zaehlen die Einkuenfte; ohne Gehaltsangabe
        # steht das zvE dafuer (etwas kleiner, der Anteil also etwas groesser).
        einkuenfte_ohne = (lohn.einkuenfte_cent if lohn is not None and lohn.brutto_cent
                           else eingaben.zve_ohne_cent)
    elif lohn is not None:
        zve_quelle = "BRUTTO"
        zve_ohne = lohn.zve_euro
        arbeitslohn = lohn.brutto_cent > 0
        einkuenfte_ohne = lohn.einkuenfte_cent
        if lohn.hinweis:
            hinweise.append(lohn.hinweis)
    else:
        raise ValueError(f"Für eine Schätzung fehlt noch {eingaben.fehlt}.")

    # Haerteausgleich (§ 46 Abs. 3 EStG, § 70 EStDV): nur neben Arbeitslohn.
    haerte = steuertarif.haerteausgleich(gewinn) if arbeitslohn else 0
    zve_mit = math.floor(Fraction(zve_ohne * 100 + gewinn - haerte, 100))

    est_ohne = 100 * steuer(zve_ohne)
    est_mit = 100 * steuer(zve_mit)

    # Gewerbesteuer und Anrechnung nach § 35 EStG. Der Anteil der
    # gewerblichen an allen positiven Einkuenften begrenzt die Anrechnung.
    gewerbe = steuertarif.gewerbesteuer(gewinn, eingaben.hebesatz_prozent)
    anrechnung = 0
    if gewerbe.steuer_cent > 0:
        summe = max(0, einkuenfte_ohne) + max(0, gewinn)
        anteil = Fraction(max(0, gewinn), summe) if summe > 0 else Fraction(0)
        anrechnung = steuertarif.ermaessigung_35(gewerbe, est_mit, anteil)
        anrechnung = min(anrechnung, max(0, est_mit - est_ohne))

    soli_ohne = steuertarif.soli(est_ohne, jahr, zusammen=zusammen)
    soli_mit = steuertarif.soli(est_mit - anrechnung, jahr, zusammen=zusammen)
    kist = eingaben.kirchensteuer_prozent
    # Kirchensteuer auf die ESt VOR § 35 (§ 51a Abs. 2 Satz 3 EStG).
    kist_ohne = steuertarif.kirchensteuer(est_ohne, kist)
    kist_mit = steuertarif.kirchensteuer(est_mit, kist)

    # Unsichere Werte nur dort nennen, wo sie das Ergebnis beruehren.
    if soli_mit or soli_ohne:
        if freigrenze.hinweis_fortgeltung:
            hinweise.append(freigrenze.hinweis_fortgeltung)
    if kist_mit or kist_ohne:
        if hinweis := steuertarif.kirchensteuer_hinweis(jahr, kist):
            hinweise.append(hinweis)

    return _mit_euer_vergleich(Ergebnis(
        eingaben=eingaben, tarif=tarif, gewinn_cent=gewinn,
        gewinn_aus_euer=eingaben.gewinn_erwartet_cent is None,
        gewinn_bisher_cent=gewinn_bisher_cent, einnahmen_bisher_cent=einnahmen_bisher_cent,
        zve_quelle=zve_quelle, lohn=lohn, zve_ohne_euro=zve_ohne, arbeitslohn=arbeitslohn,
        haerteausgleich_cent=haerte, zve_mit_euro=zve_mit, est_ohne_cent=est_ohne,
        est_mit_cent=est_mit, anrechnung_35_cent=anrechnung, soli_ohne_cent=soli_ohne,
        soli_mit_cent=soli_mit, kist_ohne_cent=kist_ohne, kist_mit_cent=kist_mit,
        gewerbe=gewerbe, hinweise=tuple(hinweise),
        hochrechnung=hochrechnung(gewinn_bisher_cent, jahr, heute), termine=vz_termine,
    ), heute)


def _mit_euer_vergleich(ergebnis: Ergebnis, heute: date) -> Ergebnis:
    """Trägt :attr:`Ergebnis.offen_laut_euer_cent` ein, wenn der erwartete Gewinn darunter liegt."""
    erwartet = ergebnis.eingaben.gewinn_erwartet_cent
    if erwartet is None or ergebnis.gewinn_bisher_cent <= erwartet:
        return ergebnis
    laut_euer = schaetze(
        replace(ergebnis.eingaben, gewinn_erwartet_cent=None),
        gewinn_bisher_cent=ergebnis.gewinn_bisher_cent,
        einnahmen_bisher_cent=ergebnis.einnahmen_bisher_cent, heute=heute,
    )
    if laut_euer.offen_cent <= ergebnis.offen_cent:
        # Aendert nichts am "mindestens" (etwa beide 0 €): dann keine Warnung --
        # sie stand sonst neben einem Ergebnis, das sie gar nicht betrifft.
        return ergebnis
    return replace(ergebnis, offen_laut_euer_cent=laut_euer.offen_cent)


def beispiel(jahr: int, *, heute: date | None = None) -> tuple[int, int]:
    """Rücklage für 10.000 € Gewinn ohne Gehalt und neben 50.000 € Gehalt, in Cent.

    Für den Leerzustand der Seite: warum es ohne das Gehalt nicht geht. Aus
    der eigenen Rechnung des Jahres, damit das Beispiel nicht veraltet.

    >>> beispiel(2026, heute=date(2026, 10, 25))
    (0, 330000)
    """
    werte = []
    for brutto in (0, 5000000):
        werte.append(schaetze(Eingaben(jahr=jahr, brutto_lohn_cent=brutto),
                              gewinn_bisher_cent=1000000, heute=heute).ruecklage_cent)
    return werte[0], werte[1]


# ----------------------------------------------------------- Eingaben prüfen

#: Formularfelder in der Reihenfolge, in der die Seite sie zeigt.
FELDER = (
    "brutto_lohn", "partner_brutto", "zve_ohne", "veranlagung", "kirchensteuer",
    "gewinn_erwartet", "vorauszahlungen", "hebesatz", "satz_manuell", "tarif_entwurf",
)


def raten_feld(empfaenger: str, quartal: int) -> str:
    """Der Name des Betragsfelds einer Rate im Formular: ``vz_finanzamt_4``."""
    return f"vz_{empfaenger.lower()}_{quartal}"


def raten_schluessel(empfaenger: str, quartal: int) -> str:
    """Der Wert des Hakens "gezahlt" einer Rate: ``FINANZAMT-4``."""
    return f"{empfaenger}-{quartal}"


#: Die Betragsfelder aller acht Raten, in der Reihenfolge des Formulars.
RATEN_FELDER = tuple(raten_feld(e, q) for e in EMPFAENGER for q in range(1, 5))


class Eingabefehler(ValueError):
    """Ein Fehler in EINEM Formularfeld; ``feld`` ist dessen Name im Formular.

    Die Seite markiert damit das Feld (``aria-invalid``) und setzt den Fokus
    hinein: auf dem Handy liegt es sonst weit unter der Meldung.
    """

    def __init__(self, text: str, feld: str) -> None:
        super().__init__(text)
        self.feld = feld


@contextmanager
def _im_feld(name: str) -> Iterator[None]:
    """Macht aus jedem ``ValueError`` im Block einen :class:`Eingabefehler` des Felds."""
    try:
        yield
    except Eingabefehler:
        raise
    except ValueError as fehler:
        raise Eingabefehler(str(fehler), name) from None


def _betrag(roh: str, feld: str, *, negativ: bool = False) -> int | None:
    try:
        cent = euro_nach_cent(roh)
    except ArithmeticError as fehler:
        raise ValueError(f"{feld}: {fehler}") from None
    if cent is not None and cent < 0 and not negativ:
        raise ValueError(f"{feld}: ein negativer Betrag ergibt hier keinen Sinn.")
    return cent


def pruefe(jahr: int, roh: dict[str, str], *, gezahlt: Collection[str] = (),
           stand: date | None = None) -> Eingaben:
    """Liest die Formularwerte eines Jahres; ein Fehler ist ein :class:`Eingabefehler`.

    ``gezahlt`` sind die angehakten Raten (``FINANZAMT-3``), ``stand`` der
    Tag, an dem das Formular gezeigt wurde. Gespeichert wird ein Haken nur,
    wenn er von der Regel "faellig heisst gezahlt" AN DIESEM TAG abweicht --
    sonst wuerde ein am 09.12. geladenes und am 10.12. abgeschicktes
    Formular die Dezember-Rate als "nicht gezahlt" festschreiben.

    >>> e = pruefe(2026, {"brutto_lohn": "52.000", "hebesatz": "410", "kirchensteuer": "9",
    ...                   "satz_manuell": "32,5", "vz_finanzamt_3": "1.000",
    ...                   "vz_finanzamt_4": "1.000"},
    ...            gezahlt=["FINANZAMT-3"], stand=date(2026, 10, 25))
    >>> e.brutto_lohn_cent, e.kirchensteuer_prozent, e.satz_manuell_promille, e.hebesatz_prozent
    (5200000, 9, 325, 410)
    >>> e.raten
    (Rate(empfaenger='FINANZAMT', quartal=3, betrag_cent=100000, gezahlt=None), \
Rate(empfaenger='FINANZAMT', quartal=4, betrag_cent=100000, gezahlt=None))
    >>> try:
    ...     pruefe(2027, {"hebesatz": "250"})
    ... except ValueError as fehler:
    ...     print(fehler.feld, "|", fehler)
    hebesatz | Hebesatz: 250 % liegt unter dem gesetzlichen Mindesthebesatz von 280 % für 2027.
    """
    def wert(name: str) -> str:
        return (roh.get(name) or "").strip()

    with _im_feld("brutto_lohn"):
        brutto = _betrag(wert("brutto_lohn"), "Bruttogehalt")
    with _im_feld("partner_brutto"):
        partner = _betrag(wert("partner_brutto"), "Bruttogehalt Partner/in")
    with _im_feld("zve_ohne"):
        zve = _betrag(wert("zve_ohne"), "Zu versteuerndes Einkommen")
    with _im_feld("gewinn_erwartet"):
        gewinn = _betrag(wert("gewinn_erwartet"), "Erwarteter Jahresgewinn", negativ=True)
    with _im_feld("vorauszahlungen"):
        voraus = _betrag(wert("vorauszahlungen"), "Weitere Vorauszahlungen") or 0
    stand = stand or date.fromisoformat(db.heute())
    raten = []
    for empfaenger in EMPFAENGER:
        for quartal in range(1, 5):
            name = raten_feld(empfaenger, quartal)
            termin_text = "{:02d}.{:02d}.".format(*reversed(TERMINE[empfaenger][quartal - 1]))
            with _im_feld(name):
                betrag = _betrag(wert(name),
                                 f"Vorauszahlung {EMPFAENGER_TEXT[empfaenger]} {termin_text}")
            if not betrag:
                continue
            angehakt = raten_schluessel(empfaenger, quartal) in gezahlt
            regel = nach_faelligkeit_gezahlt(jahr, empfaenger, quartal, stand)
            raten.append(Rate(empfaenger, quartal, betrag,
                              None if angehakt == regel else angehakt))

    veranlagung = wert("veranlagung") or "EINZEL"
    if veranlagung not in VERANLAGUNGEN:
        raise Eingabefehler("Veranlagung: bitte „einzeln“ oder „zusammen“ wählen.",
                            "veranlagung")

    kirche_roh = wert("kirchensteuer") or "0"
    erlaubt = (0, *steuertarif.kirchensteuer_saetze())
    if kirche_roh not in {str(s) for s in erlaubt}:
        raise Eingabefehler("Kirchensteuer: bitte keine, 8 % oder 9 % wählen.", "kirchensteuer")
    kirche = int(kirche_roh)

    w = steuertarif.gewerbesteuer_werte()
    mindest = steuertarif.mindesthebesatz(jahr)
    hoechst = int(w["hebesatz_hoechstens"])
    hebesatz_roh = wert("hebesatz")
    if not hebesatz_roh:
        hebesatz = int(w["hebesatz_vorschlag"])
    else:
        with _im_feld("hebesatz"):
            hebesatz = eingabe.ganzzahl(hebesatz_roh.replace("%", "").strip(),
                                        feld="Hebesatz")
            if hebesatz > hoechst:
                raise ValueError(f"Hebesatz: {hebesatz} % ist zu hoch (höchstens {hoechst} %).")
            if hebesatz < mindest:
                raise ValueError(
                    f"Hebesatz: {hebesatz} % liegt unter dem gesetzlichen Mindesthebesatz von "
                    f"{mindest} % für {jahr}."
                )

    with _im_feld("satz_manuell"):
        satz = eingabe.prozent(wert("satz_manuell"), feld="Eigener Steuersatz")
    entwurf = wert("tarif_entwurf") in ("1", "on", "ja")
    if entwurf and not steuertarif.entwurf_verfuegbar(jahr):
        entwurf = False

    return Eingaben(
        jahr=jahr, brutto_lohn_cent=brutto, partner_brutto_cent=partner, zve_ohne_cent=zve,
        veranlagung=veranlagung, kirchensteuer_prozent=kirche, satz_manuell_promille=satz,
        gewinn_erwartet_cent=gewinn, vorauszahlungen_cent=voraus, hebesatz_prozent=hebesatz,
        tarif_entwurf=entwurf, raten=tuple(raten),
    )


# ----------------------------------------------------------- Speichern, Laden

_SPALTEN = (
    "brutto_lohn_cent", "partner_brutto_cent", "zve_ohne_cent", "veranlagung",
    "kirchensteuer_prozent", "satz_manuell_promille", "gewinn_erwartet_cent",
    "vorauszahlungen_cent", "hebesatz_prozent", "tarif_entwurf",
)


def _aus_zeile(zeile: sqlite3.Row, raten: tuple[Rate, ...] = ()) -> Eingaben:
    werte = {s: zeile[s] for s in _SPALTEN}
    werte["tarif_entwurf"] = bool(werte["tarif_entwurf"])
    return Eingaben(jahr=int(zeile["jahr"]), raten=raten, **werte)


def _raten_laden(conn: sqlite3.Connection, jahr: int) -> tuple[Rate, ...]:
    return tuple(
        Rate(z["empfaenger"], int(z["quartal"]), int(z["betrag_cent"]),
             None if z["gezahlt"] is None else bool(z["gezahlt"]))
        for z in conn.execute(
            "SELECT * FROM steuer_vorauszahlung WHERE jahr = ? ORDER BY empfaenger, quartal",
            (jahr,))
    )


def laden(conn: sqlite3.Connection, jahr: int) -> Eingaben | None:
    zeile = conn.execute("SELECT * FROM steuerschaetzung WHERE jahr = ?", (jahr,)).fetchone()
    return None if zeile is None else _aus_zeile(zeile, _raten_laden(conn, jahr))


def _rate_journal(rate: Rate | None) -> dict | None:
    if rate is None:
        return None
    return {"betrag_cent": rate.betrag_cent, "gezahlt": rate.gezahlt}


def gespeicherte_jahre(conn: sqlite3.Connection) -> list[int]:
    """Jahre mit gespeicherten Angaben, neueste zuerst."""
    return [int(z["jahr"]) for z in
            conn.execute("SELECT jahr FROM steuerschaetzung ORDER BY jahr DESC")]


def speichern(conn: sqlite3.Connection, eingaben: Eingaben) -> bool:
    """Speichert die Angaben eines Jahres; True, wenn sich etwas geändert hat.

    Jedes geänderte Feld steht einzeln im Journal (wie alt → wie neu), jede
    geänderte Rate ebenso (Feld ``vz_finanzamt_4``). Ohne Änderung wird nichts
    geschrieben -- auch kein neuer Zeitstempel.
    """
    neu = {s: getattr(eingaben, s) for s in _SPALTEN}
    neu["tarif_entwurf"] = int(eingaben.tarif_entwurf)
    conn.execute("BEGIN IMMEDIATE")
    try:
        alt_zeile = conn.execute(
            "SELECT * FROM steuerschaetzung WHERE jahr = ?", (eingaben.jahr,)
        ).fetchone()
        alt = {s: (alt_zeile[s] if alt_zeile is not None else None) for s in _SPALTEN}
        geaendert = [s for s in _SPALTEN if alt[s] != neu[s]]
        alte_raten = {(r.empfaenger, r.quartal): r for r in _raten_laden(conn, eingaben.jahr)}
        neue_raten = {(r.empfaenger, r.quartal): r for r in eingaben.raten}
        raten_geaendert = [
            k for k in sorted(alte_raten.keys() | neue_raten.keys())
            if alte_raten.get(k) != neue_raten.get(k)
        ]
        if alt_zeile is not None and not geaendert and not raten_geaendert:
            conn.execute("COMMIT")
            return False
        conn.execute(
            f"""
            INSERT INTO steuerschaetzung (jahr, {", ".join(_SPALTEN)}, geaendert_am)
            VALUES (?, {", ".join("?" for _ in _SPALTEN)}, ?)
            ON CONFLICT (jahr) DO UPDATE SET
                {", ".join(f"{s} = excluded.{s}" for s in _SPALTEN)},
                geaendert_am = excluded.geaendert_am
            """,
            (eingaben.jahr, *(neu[s] for s in _SPALTEN), db.jetzt_utc()),
        )
        for spalte in geaendert:
            if alt_zeile is None and neu[spalte] is None:
                continue
            db.protokolliere(conn, "AENDERN", "steuerschaetzung", eingaben.jahr, feld=spalte,
                             wert_alt=alt[spalte], wert_neu=neu[spalte])
        jetzt = db.jetzt_utc()
        for empfaenger, quartal in raten_geaendert:
            rate = neue_raten.get((empfaenger, quartal))
            if rate is None:
                conn.execute(
                    "DELETE FROM steuer_vorauszahlung WHERE jahr = ? AND empfaenger = ?"
                    " AND quartal = ?", (eingaben.jahr, empfaenger, quartal))
            else:
                conn.execute(
                    """
                    INSERT INTO steuer_vorauszahlung
                        (jahr, empfaenger, quartal, betrag_cent, gezahlt, geaendert_am)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (jahr, empfaenger, quartal) DO UPDATE SET
                        betrag_cent = excluded.betrag_cent, gezahlt = excluded.gezahlt,
                        geaendert_am = excluded.geaendert_am
                    """,
                    (eingaben.jahr, empfaenger, quartal, rate.betrag_cent,
                     None if rate.gezahlt is None else int(rate.gezahlt), jetzt))
            db.protokolliere(conn, "AENDERN", "steuerschaetzung", eingaben.jahr,
                             feld=raten_feld(empfaenger, quartal),
                             wert_alt=_rate_journal(alte_raten.get((empfaenger, quartal))),
                             wert_neu=_rate_journal(rate))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


def loeschen(conn: sqlite3.Connection, jahr: int) -> bool:
    """Löscht die Angaben eines Jahres; True, wenn es welche gab.

    Für ein versehentlich angelegtes Jahr (es bliebe sonst für immer in der
    Jahresauswahl) oder eine erledigte Rücklage. Die alten Werte stehen im
    Journal, die Raten mit.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        zeile = conn.execute("SELECT * FROM steuerschaetzung WHERE jahr = ?", (jahr,)).fetchone()
        if zeile is None:
            conn.execute("COMMIT")
            return False
        alt = {s: zeile[s] for s in _SPALTEN}
        alt["raten"] = [{"empfaenger": r.empfaenger, "quartal": r.quartal,
                         **_rate_journal(r)} for r in _raten_laden(conn, jahr)]
        db.protokolliere(conn, "LOESCHEN", "steuerschaetzung", jahr, wert_alt=alt)
        conn.execute("DELETE FROM steuer_vorauszahlung WHERE jahr = ?", (jahr,))
        conn.execute("DELETE FROM steuerschaetzung WHERE jahr = ?", (jahr,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


def berechne(conn: sqlite3.Connection, jahr: int, eingaben: Eingaben | None = None, *,
             heute: date | None = None) -> Ergebnis | None:
    """Die Schätzung für ein Jahr mit dem EÜR-Gewinn; None, solange Angaben fehlen."""
    from app.logik import euer

    eingaben = eingaben if eingaben is not None else laden(conn, jahr)
    if eingaben is None or not eingaben.rechenbar:
        return None
    auswertung = euer.auswertung(conn, jahr)
    return schaetze(
        replace(eingaben, jahr=jahr), gewinn_bisher_cent=auswertung.gewinn_cent,
        einnahmen_bisher_cent=auswertung.einnahmen_cent, heute=heute,
    )


def erinnerungen(conn: sqlite3.Connection, heute: date) -> list[Termin]:
    """Raten fuer die Uebersicht: die naechsten (ab 14 Tagen vorher) und jede ueberfaellige.

    Ueberfaellig ist nur eine Rate, deren Haken auf "noch nicht gezahlt"
    steht -- ohne Haken gilt eine faellige Rate als gezahlt. Aus dem
    laufenden und dem Vorjahr: eine im Dezember vergessene Rate soll im
    Januar nicht verschwinden.
    """
    gefunden: list[Termin] = []
    for jahr in (heute.year - 1, heute.year):
        eingaben = laden(conn, jahr)
        if eingaben is None:
            continue
        for t in termine(jahr, eingaben.raten, heute):
            if t.ueberfaellig or (
                    not t.gezahlt and (t.faellig - heute).days <= ERINNERUNG_TAGE):
                gefunden.append(t)
    return gefunden

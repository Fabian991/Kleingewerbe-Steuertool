"""Belege in Fremdwaehrung: welcher Euro-Betrag in die Buchfuehrung geht.

Der rechtliche Rahmen
---------------------
Fuer die EUeR zaehlt der tatsaechlich abgeflossene Euro-Betrag am Abflusstag
(§ 11 Abs. 2 EStG). Ist er nicht bekannt, wird zum Kurs am Zahlungstag
umgerechnet; Massstab ist der Referenzkurs der EZB (BFH zum Arbeitslohn in
Fremdwaehrung, hier entsprechend). Die Methode ist stetig anzuwenden, Kurs
und Datum sind zu dokumentieren. Der echte Kartenbetrag ist genauer und darf
den umgerechneten ersetzen -- in ihm steckt auch das Auslandseinsatzentgelt
der Kreditkarte; einen pauschalen Aufschlag dafuer gibt es bewusst nicht.
Die Umsatzsteuer nach § 13b UStG rechnet dieses Werkzeug nicht aus.

Die Entscheidungen
------------------
1. **Stichtag** ist der Zahlungstag, wenn er beim Buchen bekannt ist (Feld
   "Bezahlt am" der Pruefmaske), sonst das Belegdatum.
2. **Euro = Fremdbetrag / EZB-Kurs**, kaufmaennisch auf Cent, exakt
   (``kurse.in_euro_cent``). Gespeichert werden Kurs (mit allen Stellen der
   EZB), Kursdatum, Quelle, Abrufzeit und SHA-256 der Kurstabelle -- und die
   verwendete Fassung der Tabelle bleibt erhalten (``kurse.sichere_fassung``).
   Nur wenn die EZB keinen Kurs liefert (Waehrung nicht gefuehrt, kein Netz),
   gilt ein Kurs von Hand; liefert sie einen, gilt ihrer -- stetig.
3. **Beleg- und Zahlungsbetrag widersprechen sich nie.** Die EUeR
   (``app/logik/euer.py``, ``ausgaben``) zaehlt die ZAHLUNG, kappt sie aber am
   Belegbetrag und verteilt den betrieblichen Anteil nach ihm
   (``_betrieblicher_anteil``). Ein Beleg ueber 92,11 EUR mit einer Zahlung
   ueber 93,00 EUR zaehlte also 92,11; mit einer Zahlung ueber 91,00 EUR
   bliebe er "offen", und die Belegliste boete eine zweite Zahlung an. Jede
   Aenderung des Euro-Betrags geht deshalb in BEIDE:

   * Wird die Zahlung spaeter mit einem anderen Tag erfasst (Bezahlt-Route)
     und gibt es noch keinen Kartenbetrag, wird der BELEG auf den Kurs des
     Zahlungstags umgerechnet, und die Zahlung bekommt denselben Betrag
     (:func:`fuer_zahlungstag`).
   * Der Kartenbetrag ersetzt den Euro-Betrag des Belegs und der Zahlung
     (:func:`kartenbetrag_eintragen`). Der umgerechnete Wert bleibt zum
     Vergleich stehen.
4. **Anlagegut**: die Anschaffungskosten sind der Euro-Betrag des Belegs.
   Steht das Gut schon im Anlagenverzeichnis, aendert sich daran nachtraeglich
   nichts -- die AfA-Reihe ist daraus gerechnet und womoeglich schon erklaert.
   Ein Kartenbetrag wird dann nicht eingetragen, und die Bezahlt-Route rechnet
   einen Beleg mit Anlagegut-Haken nie um; die Meldung sagt, was zu tun ist.
5. Jede spaetere Aenderung steht mit altem und neuem Wert im Journal
   (``AENDERN``): der urspruengliche Inhalt bleibt feststellbar
   (§ 146 Abs. 4 AO).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from app import db
from app.logik import kategorien, kurse
from app.logik.beleg.ergebnis import BelegFehler
from app.logik.rundung import euro

#: Weicht der Kartenbetrag um mehr als so viele Prozent vom umgerechneten
#: Wert ab, fragt die Maske nach. Das Auslandseinsatzentgelt liegt bei 1 bis
#: 2 %, Kursschwankungen zwischen Beleg- und Buchungstag bei wenigen Prozent --
#: mehr ist meist ein Tippfehler (Dollar statt Euro, ein Komma verrutscht).
RUECKFRAGE_AB_PROZENT = 10

#: Wann ein Kurs von Hand gilt: die EZB fuehrt die Waehrung nicht, hat fuer
#: den Tag keinen (Luecke), oder ihre Tabelle ist gerade nicht zu bekommen
#: (offline, ohne oder mit veralteter Tabelle). Ist der Kurs nur noch nicht
#: veroeffentlicht (heute vor 16 Uhr, ueber das Wochenende), kommt er -- dann
#: gilt seiner, die Methode ist stetig. "Kurs pruefen" meldet spaeter jeden
#: Kurs von Hand, fuer den die EZB doch einen hat.
MANUELL_ERLAUBT = frozenset({"unbekannt", "luecke", "keine_tabelle", "nicht_aktuell"})


def hilfe(grund: str | None) -> str:
    """Was der Nutzer in der Pruefmaske tun kann, wenn der EZB-Kurs fehlt --
    passend zu dem, was die Maske anbietet (das Feld "Kurs von Hand" steht nur
    da, wo ein Kurs von Hand gilt).

    >>> hilfe("unbekannt")
    'Bitte den Betrag laut Kreditkarte (in Euro) eintragen oder einen Kurs von Hand.'
    >>> hilfe("noch_nicht").startswith("Bitte später buchen")
    True
    """
    if grund in MANUELL_ERLAUBT:
        return "Bitte den Betrag laut Kreditkarte (in Euro) eintragen oder einen Kurs von Hand."
    if grund == "noch_nicht":
        return ("Bitte später buchen, wenn der Kurs veröffentlicht ist — oder gleich den "
                "Betrag laut Kreditkarte (in Euro) eintragen.")
    return ""


#: So heissen die Quellen des Euro-Betrags in Anzeige und Tabelle.
QUELLEN = {
    "EZB": "EZB-Referenzkurs",
    "MANUELL": "Kurs von Hand",
    "KARTE": "Kreditkartenbetrag",
}


class KeinEuroBetrag(BelegFehler):
    """Kein Kurs und kein Kartenbetrag: der Euro-Betrag fehlt.

    ``ohne_hilfe`` ist die Meldung ohne die Aufforderung (:func:`hilfe`): die
    Pruefmaske zeigt die Aufforderung schon im Kasten "In Euro" -- in der
    Fehlermeldung darueber stand sie sonst ein zweites Mal.
    """

    def __init__(self, text: str, hilfe_text: str = "") -> None:
        super().__init__(f"{text} {hilfe_text}".rstrip())
        self.ohne_hilfe = text


class KursVonHandUnplausibel(BelegFehler):
    """Der Kurs von Hand ist vermutlich verdreht oder verrutscht -- Rueckfrage."""


#: Waehrungen, von denen 1 Einheit (ungefaehr) einen Euro oder mehr wert ist:
#: nur bei ihnen ist ein Kurs unter 1 (1 EUR = 0,85 GBP) gewoehnlich. Bei allen
#: anderen heisst ein Kurs unter 1 meist, dass er andersherum eingetragen ist
#: -- "1 AED = 0,27 EUR", wie Suchmaschinen ihn zeigen, statt 1 EUR = 3,67 AED.
STAERKER_ALS_EURO = frozenset({
    "BHD", "BMD", "BSD", "CHF", "CUC", "FKP", "GBP", "GGP", "GIP", "IMP", "JEP", "JOD",
    "KWD", "KYD", "OMR", "PAB", "SHP", "USD", "XDR",
})


#: Der letzte Satz der Rueckfrage -- im Vorschlag beim Tippen gibt es den
#: Haken noch nicht, er erscheint erst mit der Rueckfrage beim Buchen.
_KURS_SCHLUSS = " Stimmt der Kurs, „Kurs von Hand trotzdem übernehmen“ anhaken."


def kurs_rueckfrage(waehrung: str, kurs: str, fremd_cent: int | None) -> str | None:
    """Warum ein Kurs von Hand vermutlich nicht stimmt -- oder None.

    Zwei Faelle: ausserhalb der Spanne, die eine Hauptwaehrung seit 1999 je
    hatte (dieselbe wie beim Einlesen der EZB-Tabelle, ``kurse.PLAUSIBEL``:
    11,606 statt 1,1606 USD), und ein Kurs unter 1 bei einer Waehrung, die
    schwaecher ist als der Euro (verdreht: 0,2723 statt 3,6724 AED).

    >>> kurs_rueckfrage("USD", "1.1606", 123456) is None
    True
    >>> kurs_rueckfrage("USD", "11.606", 123456)[:40]
    'Der Kurs von Hand 11,606 liegt weit auße'
    >>> kurs_rueckfrage("AED", "0.2723", 36700)[:44]
    'Der Kurs von Hand 0,2723 hieße: 1 AED = 3,67'
    """
    wert = Decimal(kurs)
    betrag = ""
    if fremd_cent:
        betrag = (f" {euro(fremd_cent)} {waehrung} ergäben damit "
                  f"{euro(kurse.in_euro_cent(fremd_cent, kurs))} €.")
    spanne = kurse.PLAUSIBEL.get(waehrung)
    if spanne and not spanne[0] <= wert <= spanne[1]:
        return (f"Der Kurs von Hand {kurse.kurs_de(kurs)} liegt weit außerhalb dessen, was "
                f"1 Euro seit 1999 in {waehrung} kostete ({kurse.kurs_de(str(spanne[0]))} bis "
                f"{kurse.kurs_de(str(spanne[1]))}) — vermutlich ein verrutschtes Komma oder "
                f"der Kurs andersherum.{betrag}{_KURS_SCHLUSS}")
    if wert < 1 and waehrung not in STAERKER_ALS_EURO:
        einheit = (Decimal(1) / wert).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        andersherum = (Decimal(1) / wert).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return (f"Der Kurs von Hand {kurse.kurs_de(kurs)} hieße: 1 {waehrung} = "
                f"{kurse.kurs_de(str(einheit))} € — mehr als ein Euro.{betrag} Ist der Kurs "
                f"andersherum gemeint (1 € = {kurse.kurs_de(str(andersherum))} {waehrung})?"
                f"{_KURS_SCHLUSS}")
    return None


class KartenbetragAbweichend(BelegFehler):
    """Der Kartenbetrag weicht stark vom umgerechneten Wert ab -- Rueckfrage."""

    def __init__(self, karte_cent: int, umgerechnet_cent: int, *,
                 bezug: str = "vom umgerechneten Wert") -> None:
        super().__init__(
            f"Der Betrag laut Kreditkarte ({euro(karte_cent)} €) weicht um "
            f"{abweichung_text(karte_cent, umgerechnet_cent)} {bezug} "
            f"({euro(umgerechnet_cent)} €) ab — mehr als {RUECKFRAGE_AB_PROZENT} %. "
            "Ist das wirklich der abgebuchte Betrag? Dann „Kartenbetrag so übernehmen“ "
            "anhaken."
        )
        self.karte_cent = karte_cent
        self.umgerechnet_cent = umgerechnet_cent


def wie_fremdbetrag_frage(waehrung: str) -> str:
    """Die Rueckfrage, wenn der Kartenbetrag genau dem Fremdbetrag entspricht.

    >>> wie_fremdbetrag_frage("CHF")[:58]
    'Ist das wirklich der Euro-Betrag laut Kreditkarte und nich'
    """
    return (f"Ist das wirklich der Euro-Betrag laut Kreditkarte und nicht der "
            f"Rechnungsbetrag in {waehrung}?")


class KartenbetragWieFremdbetrag(KartenbetragAbweichend):
    """Der Kartenbetrag hat genau den Zahlenwert des Fremdbetrags -- Rueckfrage.

    100,00 € bei einer Rechnung ueber 100,00 CHF liegen keine 10 % vom
    umgerechneten Wert entfernt (der Franken steht nahe beim Euro), die
    Abweichungspruefung schwieg also. Meist ist es aber der Rechnungsbetrag,
    ins falsche Feld abgeschrieben -- und dann steht in der EUeR ein Betrag,
    den nie jemand abgebucht hat. Gleicher Zahlenwert heisst deshalb immer
    nachfragen, bei jeder Abweichung. Eine Unterklasse, damit Maske und
    Kartenbetrag-Route sie wie die Abweichung behandeln (Haken "so
    uebernehmen").
    """

    def __init__(self, karte_cent: int, waehrung: str, vergleich_cent: int | None = None, *,
                 bezug: str = "vom umgerechneten Wert") -> None:
        abweichung = ""
        if vergleich_cent:
            abweichung = (f" und weicht um {abweichung_text(karte_cent, vergleich_cent)} "
                          f"{bezug} ({euro(vergleich_cent)} €) ab")
        BelegFehler.__init__(
            self,
            f"Der Betrag laut Kreditkarte ({euro(karte_cent)} €) hat genau den Zahlenwert "
            f"des Rechnungsbetrags ({euro(karte_cent)} {waehrung}){abweichung}. "
            f"{wie_fremdbetrag_frage(waehrung)} Ist er es wirklich, „Kartenbetrag so "
            "übernehmen“ anhaken."
        )
        self.karte_cent = karte_cent
        self.umgerechnet_cent = vergleich_cent


def abweichung_prozent(karte_cent: int, umgerechnet_cent: int) -> Decimal:
    """Abweichung des Kartenbetrags vom umgerechneten Wert in Prozent.

    >>> abweichung_prozent(9300, 9211), abweichung_prozent(9211, 9211)
    (Decimal('0.97'), Decimal('0.00'))
    """
    if not umgerechnet_cent:
        return Decimal(0)
    return (Decimal(karte_cent - umgerechnet_cent) * 100 / Decimal(umgerechnet_cent)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def abweichung_text(karte_cent: int, umgerechnet_cent: int) -> str:
    """Die Differenz in Euro und Prozent, mit Vorzeichen.

    >>> abweichung_text(9300, 9211), abweichung_text(9100, 9211)
    ('+0,89 € / +0,97 %', '−1,11 € / −1,21 %')
    """
    differenz = karte_cent - umgerechnet_cent
    prozent = abweichung_prozent(karte_cent, umgerechnet_cent)
    zeichen = "+" if differenz >= 0 else "−"
    prozent_text = f"{abs(prozent):.2f}".replace(".", ",")
    return f"{zeichen}{euro(abs(differenz))} € / {zeichen}{prozent_text} %"


@dataclass(frozen=True)
class Umrechnung:
    """Der Euro-Betrag eines Fremdwaehrungsbelegs und woher er kommt."""

    waehrung: str
    fremd_cent: int
    #: Der Kurs (EZB oder von Hand); None, wenn es keinen gibt und der
    #: Kartenbetrag allein traegt.
    kurs: kurse.Kurs | None
    umgerechnet_cent: int | None
    karte_cent: int | None = None

    @property
    def euro_cent(self) -> int:
        betrag = self.karte_cent if self.karte_cent is not None else self.umgerechnet_cent
        assert betrag is not None
        return betrag

    @property
    def quelle(self) -> str:
        if self.karte_cent is not None:
            return "KARTE"
        assert self.kurs is not None
        return self.kurs.quelle

    def spalten(self) -> dict[str, object]:
        """Die Spalten am Beleg (Migration 19)."""
        k = self.kurs
        return {
            "kurs": k.kurs if k else None,
            "kurs_datum": k.kursdatum.isoformat() if k else None,
            "kurs_quelle": k.quelle if k else None,
            "kurs_abruf_utc": k.abgerufen_utc if k else None,
            "kurs_tabelle_sha256": k.sha256 if k else None,
            "betrag_umgerechnet_cent": self.umgerechnet_cent,
            "betrag_karte_cent": self.karte_cent,
        }

    def journal(self) -> dict[str, object]:
        """Was ins Journal gehoert: alles, was den Euro-Betrag erklaert."""
        return {"waehrung": self.waehrung, "fremdbetrag_cent": self.fremd_cent,
                "euro_cent": self.euro_cent, "euro_quelle": self.quelle,
                "stichtag": self.kurs.stichtag.isoformat() if self.kurs else None,
                **self.spalten()}

    def text(self) -> str:
        """Ein Satz fuer die Erfolgsmeldung nach dem Buchen."""
        fremd = f"{euro(self.fremd_cent)} {self.waehrung}"
        if self.karte_cent is not None:
            satz = f"{fremd} mit dem Betrag laut Kreditkarte gebucht: {euro(self.karte_cent)} €"
            if self.umgerechnet_cent is not None and self.kurs is not None:
                satz += (f" (umgerechnet wären es {euro(self.umgerechnet_cent)} €, "
                         f"{abweichung_text(self.karte_cent, self.umgerechnet_cent)})")
            return satz + "."
        assert self.kurs is not None
        art = "EZB-Kurs" if self.kurs.quelle == "EZB" else "Kurs von Hand"
        return (f"{fremd} zum {art} {kurse.kurs_de(self.kurs.kurs)} vom "
                f"{self.kurs.kursdatum:%d.%m.%Y} umgerechnet: {euro(self.euro_cent)} €.")


def berechne(
    datenordner: Path,
    waehrung: str,
    fremd_cent: int,
    stichtag: date,
    *,
    karte_cent: int | None = None,
    kurs_manuell: str | None = None,
    karte_bestaetigt: bool = False,
    kurs_bestaetigt: bool = False,
    heute: date | None = None,
) -> Umrechnung:
    """Rechnet einen Fremdbetrag zum Stichtag in Euro um.

    Wirft :class:`KeinEuroBetrag`, wenn es weder Kurs noch Kartenbetrag gibt,
    :class:`KursVonHandUnplausibel`, wenn ein Kurs von Hand vermutlich
    verdreht oder verrutscht ist (:func:`kurs_rueckfrage`), und
    :class:`KartenbetragAbweichend`, wenn der Kartenbetrag mehr als
    :data:`RUECKFRAGE_AB_PROZENT` vom umgerechneten Wert abweicht -- beides,
    solange nicht bestaetigt. Laedt die Kurstabelle bei Bedarf -- deshalb VOR
    jeder Datenbanksperre aufrufen.
    """
    if karte_cent is not None and karte_cent <= 0:
        raise BelegFehler("Der Betrag laut Kreditkarte muss größer als null sein.")
    grund, grund_code = None, None
    try:
        kurs: kurse.Kurs | None = kurse.kurs_besorgen(datenordner, waehrung, stichtag,
                                                      heute=heute)
    except kurse.KeinKurs as fehler:
        kurs, grund, grund_code = None, str(fehler), fehler.grund
    if kurs_manuell:
        if kurs is not None:
            raise BelegFehler(
                f"Für {waehrung} gibt es den EZB-Referenzkurs {kurse.kurs_de(kurs.kurs)} vom "
                f"{kurs.kursdatum:%d.%m.%Y} — er gilt, die Methode ist stetig anzuwenden. "
                "Das Feld „Kurs von Hand“ bitte leeren; weicht der abgebuchte Betrag ab, "
                "den Betrag laut Kreditkarte eintragen."
            )
        if grund_code not in MANUELL_ERLAUBT:
            # Der Kurs kommt noch (heute gegen 16 Uhr, nach dem Wochenende):
            # dann gilt seiner. Ein Kurs von Hand jetzt hiesse zwei Methoden.
            raise BelegFehler(
                f"{grund} Einen Kurs von Hand nimmt das Tool dafür nicht: liefert die EZB "
                "einen Kurs, gilt ihrer — die Methode ist stetig anzuwenden. Das Feld „Kurs "
                f"von Hand“ bitte leeren. {hilfe(grund_code)}".rstrip()
            )
        if not kurs_bestaetigt:
            frage = kurs_rueckfrage(waehrung, kurs_manuell, fremd_cent)
            if frage:
                raise KursVonHandUnplausibel(frage)
        kurs = kurse.Kurs(waehrung=waehrung, kurs=kurs_manuell, kursdatum=stichtag,
                          stichtag=stichtag, quelle="MANUELL")
    elif kurs is not None:
        kurse.sichere_fassung(datenordner, kurs.sha256)
    umgerechnet = kurse.in_euro_cent(fremd_cent, kurs.kurs) if kurs is not None else None
    if umgerechnet is not None and umgerechnet <= 0:
        raise BelegFehler(f"{euro(fremd_cent)} {waehrung} ergeben umgerechnet keinen Cent — "
                          "bitte Betrag und Währung prüfen.")
    if karte_cent is None and umgerechnet is None:
        raise KeinEuroBetrag(f"{grund or 'Kein EZB-Kurs.'} Ohne Kurs fehlt der Euro-Betrag "
                             "für die EÜR.", hilfe(grund_code))
    if karte_cent is not None and karte_cent == fremd_cent and not karte_bestaetigt:
        raise KartenbetragWieFremdbetrag(
            karte_cent, waehrung,
            umgerechnet if umgerechnet and abs(abweichung_prozent(karte_cent, umgerechnet))
            > RUECKFRAGE_AB_PROZENT else None)
    if (karte_cent is not None and umgerechnet is not None and not karte_bestaetigt
            and abs(abweichung_prozent(karte_cent, umgerechnet)) > RUECKFRAGE_AB_PROZENT):
        raise KartenbetragAbweichend(karte_cent, umgerechnet)
    return Umrechnung(waehrung=waehrung, fremd_cent=fremd_cent, kurs=kurs,
                      umgerechnet_cent=umgerechnet, karte_cent=karte_cent)


def vorschlag(
    datenordner: Path,
    waehrung: str,
    fremd_cent: int | None,
    stichtag: date | None,
    *,
    karte_cent: int | None = None,
    kurs_manuell: str | None = None,
    heute: date | None = None,
) -> dict[str, object]:
    """Was die Pruefmaske zur Umrechnung zeigt -- bevor gebucht wird.

    Ein Woerterbuch, das auch als JSON taugt: ``ok`` (es gibt einen Kurs),
    ``text`` (ein Satz fuer den Nutzer), ``euro`` und ``kurs`` als deutscher
    Text, ``rueckfrage`` (Kartenbetrag weicht stark ab oder hat den
    Zahlenwert des Fremdbetrags -- dann steht die Frage in ``karte_frage``), ``manuell`` (ein
    Kurs von Hand waere noetig), ``kurs_rueckfrage`` (der Kurs von Hand ist
    vermutlich verdreht -- beim Buchen wird nachgefragt).

    Mit ``kurs_manuell`` (schon gelesen, ``kurse.lies_kurs``) rechnet der
    Vorschlag dort, wo ein Kurs von Hand gilt, mit ihm: der Nutzer sieht den
    Euro-Betrag, bevor er bucht.
    """
    ergebnis: dict[str, object] = {"ok": False, "waehrung": waehrung, "text": "",
                                   "euro": None, "kurs": None, "kursdatum": None,
                                   "abweichung": None, "rueckfrage": False, "manuell": False,
                                   "kurs_rueckfrage": False, "karte_frage": None}
    if waehrung == "EUR":
        return ergebnis
    if stichtag is None:
        ergebnis["text"] = "Für die Umrechnung fehlt noch das Belegdatum."
        return ergebnis
    try:
        kurs = kurse.kurs_besorgen(datenordner, waehrung, stichtag, heute=heute)
    except kurse.KeinKurs as fehler:
        ergebnis["text"] = f"{fehler} {hilfe(fehler.grund)}".rstrip()
        ergebnis["manuell"] = fehler.grund in MANUELL_ERLAUBT
        if kurs_manuell and ergebnis["manuell"]:
            _mit_kurs_von_hand(ergebnis, waehrung, fremd_cent, stichtag, kurs_manuell,
                               karte_cent)
        return ergebnis
    ergebnis.update(ok=True, kurs=kurse.kurs_de(kurs.kurs),
                    kursdatum=f"{kurs.kursdatum:%d.%m.%Y}")
    tag = f"{kurs.kursdatum:%d.%m.%Y}"
    if kurs.kursdatum != stichtag:
        tag += f" (für den {stichtag:%d.%m.%Y}: keine Veröffentlichung an diesem Tag)"
    # Die Richtung des Kurses dazu, wie in der Belegliste: "1,1476" allein
    # laesst offen, ob 1 Euro 1,1476 Dollar ist oder umgekehrt.
    richtung = f"(1 € = {kurse.kurs_de(kurs.kurs)} {waehrung})"
    if fremd_cent is None or fremd_cent <= 0:
        ergebnis["text"] = f"EZB-Referenzkurs {kurse.kurs_de(kurs.kurs)} vom {tag} {richtung}."
        return ergebnis
    umgerechnet = kurse.in_euro_cent(fremd_cent, kurs.kurs)
    ergebnis["euro"] = euro(umgerechnet)
    ergebnis["text"] = (f"{euro(fremd_cent)} {waehrung} zum EZB-Referenzkurs "
                        f"{kurse.kurs_de(kurs.kurs)} vom {tag} = {euro(umgerechnet)} € "
                        f"{richtung}.")
    _karte_vergleichen(ergebnis, karte_cent, umgerechnet, fremd_cent)
    if kurs_manuell:
        ergebnis["text"] = (f"{ergebnis['text']} Für {waehrung} gilt der EZB-Referenzkurs — "
                            "das Feld „Kurs von Hand“ bitte leeren.")
    return ergebnis


def _karte_vergleichen(ergebnis: dict[str, object], karte_cent: int | None,
                       umgerechnet: int, fremd_cent: int | None) -> None:
    if karte_cent is not None and karte_cent > 0 and umgerechnet > 0:
        ergebnis["abweichung"] = abweichung_text(karte_cent, umgerechnet)
        ergebnis["rueckfrage"] = (abs(abweichung_prozent(karte_cent, umgerechnet))
                                  > RUECKFRAGE_AB_PROZENT)
        if karte_cent == fremd_cent:
            # Siehe KartenbetragWieFremdbetrag: auch unter 10 % nachfragen.
            # Der Text schliesst an "Der Kartenbetrag weicht um ... davon ab" an.
            frage = (f"Gleicher Zahlenwert wie der Rechnungsbetrag: "
                     f"{wie_fremdbetrag_frage(str(ergebnis['waehrung']))}")
            ergebnis["karte_frage"] = (
                f" — mehr als 10 %; beim Buchen wird nachgefragt. {frage}"
                if ergebnis["rueckfrage"] else f". {frage} Beim Buchen wird nachgefragt.")
            ergebnis["rueckfrage"] = True


def _mit_kurs_von_hand(ergebnis: dict[str, object], waehrung: str, fremd_cent: int | None,
                       stichtag: date, kurs: str, karte_cent: int | None) -> None:
    """Der Vorschlag mit dem Kurs von Hand: Euro-Betrag und, wenn der Kurs
    verdreht aussieht, die Rueckfrage schon beim Tippen."""
    richtung = f"(1 € = {kurse.kurs_de(kurs)} {waehrung})"
    ergebnis.update(ok=True, kurs=kurse.kurs_de(kurs), kursdatum=f"{stichtag:%d.%m.%Y}")
    if fremd_cent is None or fremd_cent <= 0:
        ergebnis["text"] = f"Kurs von Hand {kurse.kurs_de(kurs)} {richtung}."
    else:
        umgerechnet = kurse.in_euro_cent(fremd_cent, kurs)
        ergebnis["euro"] = euro(umgerechnet)
        ergebnis["text"] = (f"{euro(fremd_cent)} {waehrung} zum Kurs von Hand "
                            f"{kurse.kurs_de(kurs)} {richtung} = {euro(umgerechnet)} €.")
        _karte_vergleichen(ergebnis, karte_cent, umgerechnet, fremd_cent)
    # Ohne Betrag: den Euro-Betrag nennt der erste Satz schon.
    frage = kurs_rueckfrage(waehrung, kurs, None)
    if frage:
        ergebnis["kurs_rueckfrage"] = True
        ergebnis["text"] = f"{ergebnis['text']} {frage.removesuffix(_KURS_SCHLUSS)}"



# ----------------------------------------------------------------- Anzeige

def anzeige(beleg: Mapping) -> dict[str, object] | None:
    """Die Umrechnung eines Belegs fuer die Belegliste -- None bei Euro.

    ``zeile`` ist der Satz unter dem Betrag ("100,00 USD zum EZB-Kurs 1,0856
    vom 14.03.2026"); die uebrigen Schluessel fuellen die Einzelheiten:
    Waehrung, Fremdbetrag, Kurs, Kursdatum, Quelle, Abrufzeit und SHA-256.
    """
    waehrung = beleg["waehrung"]
    fremd = beleg["betrag_fremdwaehrung_cent"]
    if not waehrung or waehrung == "EUR" or fremd is None:
        return None
    karte = beleg["betrag_karte_cent"]
    umgerechnet = beleg["betrag_umgerechnet_cent"]
    kurs = beleg["kurs"]
    kursdatum = beleg["kurs_datum"]
    datum = (f"{kursdatum[8:10]}.{kursdatum[5:7]}.{kursdatum[:4]}" if kursdatum else "")
    fremd_text = f"{euro(fremd)} {waehrung}"
    art = "EZB-Kurs" if beleg["kurs_quelle"] == "EZB" else "Kurs von Hand"
    vergleich = None
    if karte is not None:
        quelle = "KARTE"
        zeile = f"{fremd_text} · Betrag laut Kreditkarte"
        if umgerechnet is not None and kurs:
            # Kurz, weil er unter dem Betrag in der schmalen Spalte steht; Kurs
            # und Kursdatum stehen in den Einzelheiten.
            wert = "EZB-Wert" if beleg["kurs_quelle"] == "EZB" else "umgerechnet"
            vergleich = (f"{wert} {euro(umgerechnet)} €, Differenz "
                         f"{abweichung_text(karte, umgerechnet)}")
    elif kurs and beleg["kurs_quelle"] in ("EZB", "MANUELL"):
        quelle = beleg["kurs_quelle"]
        zeile = f"{fremd_text} zum {art} {kurse.kurs_de(kurs)} vom {datum}"
    else:
        # Vor der Kursumrechnung (bis Version 18) trug die Maske den Betrag
        # laut Kontoauszug oder Kreditkarte von Hand ein.
        quelle = None
        zeile = f"{fremd_text} · Euro-Betrag laut Kontoauszug oder Kreditkarte"
    return {
        "zeile": zeile,
        "vergleich": vergleich,
        "quelle": QUELLEN.get(quelle or "", "Kontoauszug oder Kreditkarte (vor Version 19)"),
        "waehrung": waehrung,
        "fremdbetrag": fremd_text,
        "kurs": kurse.kurs_de(kurs) if kurs else None,
        "kurs_art": art if kurs else None,
        "kursdatum": datum or None,
        "abruf": beleg["kurs_abruf_utc"],
        "sha256": beleg["kurs_tabelle_sha256"],
        "umgerechnet": euro(umgerechnet) if umgerechnet is not None else None,
        "karte": euro(karte) if karte is not None else None,
        "differenz": (abweichung_text(karte, umgerechnet)
                      if karte is not None and umgerechnet is not None else None),
        "ezb": beleg["kurs_quelle"] == "EZB",
    }


# ------------------------------------------------- Aenderungen am gebuchten Beleg

def _im_verzeichnis(conn: sqlite3.Connection, beleg_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT id, bezeichnung, ak_brutto_cent FROM anlagegut"
                        " WHERE beleg_id = ?", (beleg_id,)).fetchone()


def _setze(conn: sqlite3.Connection, beleg: sqlite3.Row, neu: dict[str, object]) -> None:
    """Schreibt geaenderte Spalten des Belegs, je Spalte ein Journaleintrag
    mit altem und neuem Wert. Innerhalb einer offenen Transaktion."""
    geaendert = {spalte: wert for spalte, wert in neu.items() if beleg[spalte] != wert}
    if not geaendert:
        return
    zuweisung = ", ".join(f"{spalte} = ?" for spalte in geaendert)
    conn.execute(f"UPDATE beleg SET {zuweisung}, version = version + 1 WHERE id = ?",
                 (*geaendert.values(), beleg["id"]))
    for spalte, wert in geaendert.items():
        db.protokolliere(conn, "AENDERN", "beleg", beleg["id"], feld=spalte,
                         wert_alt=beleg[spalte], wert_neu=wert)


def _betraege(beleg: sqlite3.Row, euro_cent: int) -> dict[str, object]:
    """Euro-Betrag und alles, was daran haengt -- mit denselben Funktionen
    gerechnet wie beim Buchen (eine Rechnung, ein Ergebnis)."""
    return {
        "betrag_brutto_cent": euro_cent,
        "betrag_betrieblich_cent": kategorien.betrieblich_cent(
            euro_cent, int(beleg["aufteilungsquote"])),
        # Ein Fremdwaehrungsbeleg traegt kein Netto: die Wertgrenze wird am
        # Brutto geprueft (siehe pipeline.buche).
        "netto_fuer_grenzwert_cent": euro_cent,
    }


def _zahlung_angleichen(conn: sqlite3.Connection, beleg: sqlite3.Row,
                        euro_cent: int) -> str | None:
    """Die Zahlung des Belegs bekommt den neuen Euro-Betrag (Entscheidung 3).

    Nur eine Zahlung, die den GANZEN bisherigen Belegbetrag trug: eine
    Teilzahlung (eine Rate) bleibt, wie sie ist -- sonst wurde aus 530 EUR
    der volle Betrag, und die EUeR zaehlte, was nie abgeflossen ist. Gibt
    dann einen Hinweis fuer die Meldung zurueck.
    """
    zahlungen = conn.execute("SELECT id, betrag_cent FROM zahlung WHERE beleg_id = ?",
                             (beleg["id"],)).fetchall()
    if len(zahlungen) > 1:
        raise BelegFehler(
            f"Zu Beleg {beleg['id']} gibt es {len(zahlungen)} Zahlungen — der Euro-Betrag "
            "lässt sich nur bei einer Zahlung angleichen. Bitte von Hand prüfen."
        )
    for zahlung in zahlungen:
        if abs(zahlung["betrag_cent"]) != abs(beleg["betrag_brutto_cent"]):
            return (f"Die Zahlung über {euro(abs(zahlung['betrag_cent']))} € ist eine "
                    "Teilzahlung und bleibt, wie sie ist — bitte prüfen, ob noch etwas offen "
                    "ist.")
        neu = -abs(euro_cent)
        if zahlung["betrag_cent"] != neu:
            conn.execute("UPDATE zahlung SET betrag_cent = ? WHERE id = ?", (neu, zahlung["id"]))
            db.protokolliere(conn, "AENDERN", "zahlung", zahlung["id"], feld="betrag_cent",
                             wert_alt=zahlung["betrag_cent"], wert_neu=neu,
                             begruendung=f"Euro-Betrag von Beleg {beleg['id']} geändert")
    return None


def kartenbetrag_eintragen(conn: sqlite3.Connection, beleg_id: int, karte_cent: int, *,
                           bestaetigt: bool = False) -> tuple[sqlite3.Row, str | None]:
    """Traegt den Betrag laut Kreditkarte nach. Gibt den Beleg VOR der Aenderung
    zurueck -- und einen Hinweis, wenn die Zahlung nicht angeglichen wurde
    (eine Teilzahlung, siehe :func:`_zahlung_angleichen`).

    Der Kartenbetrag ersetzt den Euro-Betrag des Belegs und seiner Zahlung --
    und damit die Wirkung in der EUeR, Auslandseinsatzentgelt eingeschlossen.
    Der umgerechnete Wert bleibt zum Vergleich. Oeffnet die Transaktion selbst.
    """
    if karte_cent <= 0:
        raise BelegFehler("Der Betrag laut Kreditkarte muss größer als null sein.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        beleg = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()
        if beleg is None:
            raise BelegFehler("Diesen Beleg gibt es nicht.")
        if beleg["waehrung"] == "EUR" or beleg["betrag_fremdwaehrung_cent"] is None:
            raise BelegFehler(f"Beleg {beleg_id} ist in Euro — einen Kartenbetrag gibt es nur "
                              "bei Fremdwährung.")
        gut = _im_verzeichnis(conn, beleg_id)
        if gut is not None:
            raise BelegFehler(
                f"Beleg {beleg_id} steht als „{gut['bezeichnung']}“ im Anlagenverzeichnis; "
                f"die Anschaffungskosten ({euro(gut['ak_brutto_cent'])} €) und die AfA sind "
                "daraus gerechnet und werden nachträglich nicht geändert. Weicht der "
                "abgebuchte Betrag ab, bitte mit der Steuerberatung klären."
            )
        if beleg["betrag_karte_cent"] == karte_cent:
            # Derselbe Betrag noch einmal: nichts zu aendern, auch keine
            # erneute Rueckfrage zu einem schon bestaetigten Betrag.
            conn.execute("COMMIT")
            return beleg, None
        # Verglichen wird mit dem umgerechneten Wert; ohne ihn (vor Version
        # 19 gebucht, oder offline nur mit Kartenbetrag) mit dem bisherigen
        # Euro-Betrag -- sonst ging auch ein Betrag mal zehn ohne Rueckfrage.
        umgerechnet = beleg["betrag_umgerechnet_cent"]
        vergleich = umgerechnet or beleg["betrag_brutto_cent"]
        if not bestaetigt and karte_cent == beleg["betrag_fremdwaehrung_cent"]:
            raise KartenbetragWieFremdbetrag(
                karte_cent, beleg["waehrung"],
                vergleich if vergleich and abs(abweichung_prozent(karte_cent, vergleich))
                > RUECKFRAGE_AB_PROZENT else None,
                bezug="vom umgerechneten Wert" if umgerechnet else "vom bisherigen Euro-Betrag")
        if (vergleich and not bestaetigt
                and abs(abweichung_prozent(karte_cent, vergleich)) > RUECKFRAGE_AB_PROZENT):
            raise KartenbetragAbweichend(
                karte_cent, vergleich,
                bezug="vom umgerechneten Wert" if umgerechnet else "vom bisherigen Euro-Betrag")
        hinweis = _zahlung_angleichen(conn, beleg, karte_cent)
        _setze(conn, beleg, {"betrag_karte_cent": karte_cent, **_betraege(beleg, karte_cent)})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return beleg, hinweis


def _stichtag(conn: sqlite3.Connection, beleg: Mapping) -> date:
    """Der Tag, zu dem ohne Kartenbetrag umgerechnet wird: der Tag der
    Zahlung, wenn es genau eine gibt, sonst das Belegdatum (Entscheidung 3)."""
    tage = [z["datum"] for z in conn.execute("SELECT datum FROM zahlung WHERE beleg_id = ?",
                                             (beleg["id"],))]
    return date.fromisoformat(tage[0] if len(tage) == 1 else beleg["belegdatum"])


def ezb_kurs_fuer(conn: sqlite3.Connection, datenordner: Path, beleg: Mapping) -> kurse.Kurs:
    """Der EZB-Kurs fuer "Zum EZB-Kurs umrechnen" bzw. "Kartenbetrag entfernen".

    Laedt die Kurstabelle bei Bedarf -- also VOR der Sperre aufrufen. Wirft
    ``kurse.KeinKurs``, wenn es keinen gibt.
    """
    return kurse.kurs_besorgen(datenordner, beleg["waehrung"], _stichtag(conn, beleg))


def ezb_kurs_vorhanden(conn: sqlite3.Connection, datenordner: Path, beleg: Mapping, *,
                       reihen: dict[str, kurse.Tabelle | None] | None = None) -> bool:
    """Ob die GESPEICHERTE Tabelle fuer einen Beleg mit Kurs von Hand einen
    EZB-Kurs hat -- fuer die Belegliste, ohne etwas zu laden. ``reihen``
    merkt sich je Waehrung die gelesene Reihe (eine Seite, viele Belege)."""
    waehrung = beleg["waehrung"]
    if reihen is not None and waehrung in reihen:
        tab = reihen[waehrung]
    else:
        tab, _ = kurse.fassung_reihe(datenordner, None, waehrung)
        if reihen is not None:
            reihen[waehrung] = tab
    if tab is None:
        return False
    try:
        kurse.kurs_aus_tabelle(tab, beleg["waehrung"], _stichtag(conn, beleg))
    except (kurse.KeinKurs, ValueError):
        return False
    return True


def zum_ezb_kurs(conn: sqlite3.Connection, beleg_id: int, kurs: kurse.Kurs | None, *,
                 version: int) -> tuple[sqlite3.Row, str | None]:
    """Nimmt den Kartenbetrag zurueck bzw. ersetzt einen Kurs von Hand durch
    den EZB-Kurs: der Euro-Betrag von Beleg und Zahlung ist danach der zum
    EZB-Kurs ``kurs`` umgerechnete (Quelle "EZB-Referenzkurs").

    Ohne ``kurs`` (die EZB fuehrt die Waehrung nicht) wird nur der
    Kartenbetrag zurueckgenommen, und der Kurs von Hand traegt wieder. Jede
    Aenderung steht mit altem und neuem Wert im Journal. ``version`` ist die
    des Belegs, mit dem der Kurs besorgt wurde -- hat ihn inzwischen jemand
    geaendert, wird abgebrochen. Gibt den Beleg VOR der Aenderung und einen
    Hinweis zur Zahlung zurueck. Oeffnet die Transaktion selbst.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        beleg = conn.execute("SELECT * FROM beleg WHERE id = ?", (beleg_id,)).fetchone()
        if beleg is None:
            raise BelegFehler("Diesen Beleg gibt es nicht.")
        if beleg["version"] != version:
            raise BelegFehler(f"Beleg {beleg_id} wurde gerade geändert — bitte noch einmal.")
        if beleg["waehrung"] == "EUR" or beleg["betrag_fremdwaehrung_cent"] is None:
            raise BelegFehler(f"Beleg {beleg_id} ist in Euro — da gibt es nichts umzurechnen.")
        gut = _im_verzeichnis(conn, beleg_id)
        if gut is not None:
            raise BelegFehler(
                f"Beleg {beleg_id} steht als „{gut['bezeichnung']}“ im Anlagenverzeichnis; "
                f"die Anschaffungskosten ({euro(gut['ak_brutto_cent'])} €) und die AfA sind "
                "daraus gerechnet und werden nachträglich nicht geändert."
            )
        if kurs is not None:
            umgerechnet = kurse.in_euro_cent(int(beleg["betrag_fremdwaehrung_cent"]), kurs.kurs)
            neu: dict[str, object] = {
                "kurs": kurs.kurs, "kurs_datum": kurs.kursdatum.isoformat(),
                "kurs_quelle": kurs.quelle, "kurs_abruf_utc": kurs.abgerufen_utc,
                "kurs_tabelle_sha256": kurs.sha256, "betrag_umgerechnet_cent": umgerechnet,
            }
        elif beleg["betrag_umgerechnet_cent"] is not None:
            umgerechnet = int(beleg["betrag_umgerechnet_cent"])
            neu = {}
        else:
            raise BelegFehler(f"Für Beleg {beleg_id} gibt es keinen Kurs — ohne Kartenbetrag "
                              "fehlte der Euro-Betrag.")
        neu.update(betrag_karte_cent=None, **_betraege(beleg, umgerechnet))
        hinweis = _zahlung_angleichen(conn, beleg, umgerechnet)
        _setze(conn, beleg, neu)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return beleg, hinweis


@dataclass(frozen=True)
class Zahlungstag:
    """Was die Bezahlt-Route am Beleg aendern muss (Entscheidung 3)."""

    #: Der Kurs des Zahlungstags -- None: der Beleg bleibt, wie er ist.
    kurs: kurse.Kurs | None = None
    umgerechnet_cent: int | None = None
    #: Ein Satz fuer die Meldung, wenn nicht umgerechnet wird, obwohl man es
    #: erwarten koennte (Anlagegut, Kurs von Hand).
    hinweis: str | None = None


def fuer_zahlungstag(conn: sqlite3.Connection, datenordner: Path, beleg: sqlite3.Row,
                     tag: date) -> Zahlungstag:
    """Gilt fuer die Zahlung am ``tag`` ein anderer Kurs als am Beleg?

    Nur bei Fremdwaehrung ohne Kartenbetrag, mit EZB-Kurs und ohne
    Anlagegut-Haken. Laedt die Kurstabelle bei Bedarf -- also VOR der Sperre
    aufrufen. Wirft ``kurse.KeinKurs``, wenn der Kurs des Zahlungstags fehlt.
    """
    if (beleg["waehrung"] == "EUR" or beleg["betrag_fremdwaehrung_cent"] is None
            or beleg["betrag_karte_cent"] is not None):
        return Zahlungstag()
    if conn.execute("SELECT 1 FROM zahlung WHERE beleg_id = ? LIMIT 1",
                    (beleg["id"],)).fetchone() is not None:
        # Eine Rate ist schon erfasst, mit dem bisherigen Betrag: eine neue
        # Umrechnung des Belegs liesse die beiden auseinanderlaufen.
        return Zahlungstag()
    return _auf_stichtag(conn, datenordner, beleg, tag, belegdatum_gilt=True)


def fuer_stichtag(conn: sqlite3.Connection, datenordner: Path, beleg: sqlite3.Row,
                  tag: date) -> Zahlungstag:
    """Der Kurs fuer einen KORRIGIERTEN Stichtag (app/logik/beleg/belegzahlung.py):
    der Zahltag einer erfassten Zahlung wird geaendert, oder die Zahlung wird
    zurueckgenommen -- dann gilt wieder das Belegdatum (Entscheidung 1).

    Dieselben Regeln wie :func:`fuer_zahlungstag`, nur ohne dessen Abkuerzung
    "am Belegdatum gilt der Kurs des Belegs": nach einer Umrechnung auf den
    alten Zahltag stimmt sie nicht mehr. Ob es genau eine Zahlung in voller
    Hoehe gibt, prueft der Aufrufer. Laedt die Kurstabelle bei Bedarf -- also
    VOR der Sperre aufrufen. Wirft ``kurse.KeinKurs``.
    """
    if (beleg["waehrung"] == "EUR" or beleg["betrag_fremdwaehrung_cent"] is None
            or beleg["betrag_karte_cent"] is not None):
        return Zahlungstag()
    return _auf_stichtag(conn, datenordner, beleg, tag, belegdatum_gilt=False)


def _auf_stichtag(conn: sqlite3.Connection, datenordner: Path, beleg: sqlite3.Row,
                  tag: date, *, belegdatum_gilt: bool) -> Zahlungstag:
    """Gemeinsamer Teil: Anlagegut und Kurs von Hand bleiben, sonst der
    EZB-Kurs des Tages, wenn er sich vom gespeicherten unterscheidet."""
    if beleg["ist_anlagegut"] or _im_verzeichnis(conn, beleg["id"]) is not None:
        return Zahlungstag(hinweis=(
            "Anlagegut: der Euro-Betrag bleibt beim Kurs vom "
            f"{_datum_de(beleg['kurs_datum'])} — er ist die Grundlage der Anschaffungskosten "
            "und wird nachträglich nicht umgerechnet."))
    if beleg["kurs_quelle"] == "MANUELL":
        return Zahlungstag(hinweis=(
            f"Umgerechnet ist mit dem Kurs von Hand vom {_datum_de(beleg['kurs_datum'])}; "
            "steht der abgebuchte Betrag fest, bitte als Betrag laut Kreditkarte eintragen."))
    if beleg["kurs_quelle"] != "EZB":
        return Zahlungstag()
    if belegdatum_gilt and tag.isoformat() == beleg["belegdatum"]:
        # Ohne Zahlung ist zum Belegdatum umgerechnet -- bezahlt am selben
        # Tag gilt derselbe Kurs. Dafuer braucht es keine Kurstabelle (und
        # ohne Netz keine Wartezeit).
        return Zahlungstag()
    kurs = kurse.kurs_besorgen(datenordner, beleg["waehrung"], tag)
    if kurs.kurs == beleg["kurs"] and kurs.kursdatum.isoformat() == beleg["kurs_datum"]:
        return Zahlungstag()
    kurse.sichere_fassung(datenordner, kurs.sha256)
    return Zahlungstag(kurs=kurs, umgerechnet_cent=kurse.in_euro_cent(
        int(beleg["betrag_fremdwaehrung_cent"]), kurs.kurs))


def zahlungstag_uebernehmen(conn: sqlite3.Connection, beleg: sqlite3.Row,
                            neu: Zahlungstag) -> None:
    """Schreibt die Umrechnung zum Zahlungstag -- innerhalb einer offenen
    Transaktion, mit dem frisch gelesenen Beleg."""
    if neu.kurs is None or neu.umgerechnet_cent is None:
        return
    k = neu.kurs
    _setze(conn, beleg, {
        "kurs": k.kurs, "kurs_datum": k.kursdatum.isoformat(), "kurs_quelle": k.quelle,
        "kurs_abruf_utc": k.abgerufen_utc, "kurs_tabelle_sha256": k.sha256,
        "betrag_umgerechnet_cent": neu.umgerechnet_cent,
        **_betraege(beleg, neu.umgerechnet_cent),
    })


def umdatiert_uebernehmen(conn: sqlite3.Connection, beleg: sqlite3.Row,
                          neu: Zahlungstag) -> str | None:
    """Die Umrechnung auf einen korrigierten Zahltag (:func:`fuer_stichtag`)
    in Beleg UND Zahlung -- Entscheidung 3. Innerhalb einer offenen
    Transaktion, mit dem frisch gelesenen Beleg; gibt wie
    :func:`_zahlung_angleichen` einen Hinweis zu einer Teilzahlung zurueck."""
    if neu.kurs is None or neu.umgerechnet_cent is None:
        return None
    hinweis = _zahlung_angleichen(conn, beleg, neu.umgerechnet_cent)
    zahlungstag_uebernehmen(conn, beleg, neu)
    return hinweis


def _datum_de(iso: str | None) -> str:
    if not iso:
        return "?"
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


# ------------------------------------------------------------- Kurs pruefen

@dataclass(frozen=True)
class Pruefung:
    geprueft: int
    befunde: list[dict[str, object]]


def pruefe(conn: sqlite3.Connection, datenordner: Path,
           beleg_id: int | None = None) -> Pruefung:
    """Rechnet jede Umrechnung aus der gespeicherten Tabelle nach.

    Je Beleg mit Kurs: steht der Kurs genau so in der Tabelle (bevorzugt in
    der Fassung, mit der gebucht wurde -- ``kurse/verwendet/``), passt das
    Kursdatum zum Belegdatum oder zu einem Zahlungstag, ergibt Fremdbetrag /
    Kurs den gespeicherten Euro-Wert, und stimmen Beleg- und Zahlungsbetrag
    ueberein? Fehlt die Fassung, mit der gebucht wurde, oder passt sie nicht
    mehr zu ihrer Pruefsumme, ist das ein Befund; nachgerechnet wird dann
    mit der aktuellen Tabelle. Ein Kurs von Hand gilt nur, wenn die EZB
    keinen liefert (stetige Methode): hat die gespeicherte Tabelle fuer
    Waehrung und Stichtag einen EZB-Kurs -- der Beleg wurde offline gebucht,
    oder die Quelle wurde nachtraeglich auf "von Hand" gestellt --, ist das
    ein Befund mit beiden Euro-Betraegen. Geladen wird dafuer nichts --
    geprueft wird gegen das, was im Datenordner liegt.
    """
    bedingung, werte = "", []
    if beleg_id is not None:
        bedingung, werte = " AND b.id = ?", [beleg_id]
    zeilen = conn.execute(
        "SELECT b.*, (SELECT GROUP_CONCAT(z.datum) FROM zahlung z WHERE z.beleg_id = b.id)"
        " AS zahltage, (SELECT COALESCE(SUM(ABS(z.betrag_cent)), 0) FROM zahlung z"
        " WHERE z.beleg_id = b.id) AS gezahlt, (SELECT COUNT(*) FROM zahlung z"
        " WHERE z.beleg_id = b.id) AS zahlungen"
        # Geloeschte Belege nicht: an ihnen laesst sich nichts mehr richten,
        # und ein Befund dort waere ein Alarm ohne Ausweg.
        " FROM v_beleg_gueltig b WHERE b.waehrung <> 'EUR'"
        " AND b.betrag_fremdwaehrung_cent IS NOT NULL"
        f"{bedingung} ORDER BY b.id", werte,
    ).fetchall()
    befunde: list[dict[str, object]] = []

    def reihe(sha256: str | None, waehrung: str) -> tuple[kurse.Tabelle | None, str | None]:
        # Nur die Spalte der Waehrung, je Fassung einmal gelesen (siehe
        # kurse.fassung_reihe) -- nicht jede Fassung als ganze Tabelle.
        return kurse.fassung_reihe(datenordner, sha256, waehrung)

    def befund(zeile: sqlite3.Row, text: str) -> None:
        befunde.append({"beleg_id": zeile["id"], "befund": text})

    for b in zeilen:
        fremd = int(b["betrag_fremdwaehrung_cent"])
        erwartet = (b["betrag_karte_cent"] if b["betrag_karte_cent"] is not None
                    else b["betrag_umgerechnet_cent"])
        if erwartet is not None and erwartet != b["betrag_brutto_cent"]:
            befund(b, f"Euro-Betrag {euro(b['betrag_brutto_cent'])} € passt nicht zur "
                      f"Umrechnung ({euro(erwartet)} €)")
        if b["zahlungen"] == 1 and int(b["gezahlt"]) != int(b["betrag_brutto_cent"]):
            befund(b, f"Zahlung über {euro(int(b['gezahlt']))} €, Beleg über "
                      f"{euro(b['betrag_brutto_cent'])} € — die EÜR rechnet mit dem kleineren")
        if not b["kurs"]:
            continue
        if b["betrag_umgerechnet_cent"] is not None:
            nachgerechnet = kurse.in_euro_cent(fremd, b["kurs"])
            if nachgerechnet != b["betrag_umgerechnet_cent"]:
                befund(b, f"{euro(fremd)} {b['waehrung']} / {kurse.kurs_de(b['kurs'])} ergibt "
                          f"{euro(nachgerechnet)} €, gespeichert sind "
                          f"{euro(b['betrag_umgerechnet_cent'])} €")
        if b["kurs_quelle"] == "MANUELL":
            _kurs_von_hand_pruefen(b, reihe(None, b["waehrung"])[0], befund)
            continue
        if b["kurs_quelle"] != "EZB":
            continue
        sha = b["kurs_tabelle_sha256"]
        tab, problem = reihe(sha, b["waehrung"])
        if tab is None:
            # Fehlt die Fassung, mit der gebucht wurde, oder ist sie veraendert,
            # ist das selbst ein Befund -- nicht still die aktuelle Tabelle
            # nehmen und "stimmt" melden: der Nachweis ist dann nicht mehr da.
            tab, _ = reihe(None, b["waehrung"])
            fassung = ("die Fassung der Kurstabelle, mit der gebucht wurde"
                       + (f" (SHA-256 {sha[:12]}…)" if sha else ""))
            if tab is None:
                befund(b, f"{fassung}, {problem}; eine andere Kurstabelle gibt es im "
                          "Datenordner nicht — der EZB-Kurs lässt sich nicht nachprüfen")
                continue
            befund(b, f"{fassung}, {problem} — nachgerechnet mit der aktuellen Tabelle")
        kursdatum = date.fromisoformat(b["kurs_datum"])
        tage, kurswerte = tab.reihen.get(b["waehrung"], ((), ()))
        try:
            laut_tabelle = kurswerte[tage.index(kursdatum)]
        except ValueError:
            befund(b, f"die Kurstabelle nennt für den {kursdatum:%d.%m.%Y} keinen "
                      f"{b['waehrung']}-Kurs")
            continue
        if Decimal(laut_tabelle) != Decimal(b["kurs"]):
            befund(b, f"EZB-Kurs vom {kursdatum:%d.%m.%Y} laut Tabelle "
                      f"{kurse.kurs_de(laut_tabelle)}, gespeichert {kurse.kurs_de(b['kurs'])}")
        stichtage = [b["belegdatum"], *(t for t in (b["zahltage"] or "").split(",") if t)]
        passend = False
        for stichtag in stichtage:
            try:
                kurs = kurse.kurs_aus_tabelle(tab, b["waehrung"], date.fromisoformat(stichtag))
            except (kurse.KeinKurs, ValueError):
                continue
            if kurs.kursdatum == kursdatum:
                passend = True
                break
        if not passend:
            befund(b, f"Kursdatum {kursdatum:%d.%m.%Y} passt weder zum Belegdatum noch zu "
                      "einem Zahlungstag")
    return Pruefung(geprueft=len(zeilen), befunde=befunde)


def _kurs_von_hand_pruefen(b: sqlite3.Row, tab: kurse.Tabelle | None, befund) -> None:
    """Hat die EZB fuer einen Beleg mit Kurs von Hand doch einen Kurs?

    Nur, wenn der Kurs von Hand den Euro-Betrag traegt (kein Kartenbetrag):
    sonst geht er nicht in die EUeR. Stichtag ist das Kursdatum -- beim Kurs
    von Hand der Tag, fuer den umgerechnet wurde.
    """
    if tab is None or b["betrag_karte_cent"] is not None or not b["kurs_datum"]:
        return
    stichtag = date.fromisoformat(b["kurs_datum"])
    try:
        ezb = kurse.kurs_aus_tabelle(tab, b["waehrung"], stichtag)
    except kurse.KeinKurs:
        return
    ezb_cent = kurse.in_euro_cent(int(b["betrag_fremdwaehrung_cent"]), ezb.kurs)
    befund(b, f"für den {stichtag:%d.%m.%Y} gibt es den EZB-Kurs {kurse.kurs_de(ezb.kurs)} "
              f"vom {ezb.kursdatum:%d.%m.%Y}, gebucht ist der Kurs von Hand "
              f"{kurse.kurs_de(b['kurs'])}: {euro(b['betrag_brutto_cent'])} € statt "
              f"{euro(ezb_cent)} € — liefert die EZB einen Kurs, gilt ihrer (unter „Ändern“: "
              "„Zum EZB-Kurs umrechnen“)")

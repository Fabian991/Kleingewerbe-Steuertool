"""Die Einnahmenüberschussrechnung — Summen je Kennzahl der Anlage EÜR.

Der Kategoriekatalog trägt die Kennzahlen seit Anfang an
(``app/stammdaten/kategorien.json``), gerechnet wurde mit ihnen bisher nicht.
Das ist die Lücke, die dieses Modul schließt: ohne sie muss man die Zahlen für
die Anlage EÜR am Jahresende aus Listen zusammenaddieren, und genau dort
entstehen die Fehler.

Drei Dinge sind hier leicht falsch zu machen:

1. **Zu- und Abflussprinzip (§ 11 EStG).** Gezählt wird der Tag, an dem das Geld
   fließt, nicht das Rechnungs- oder Belegdatum. Eine im Dezember gestellte und
   im Januar bezahlte Rechnung gehört ins Folgejahr. Dieselbe Grundlage benutzt
   schon ``grenzwert.py`` für den § 19-Gesamtumsatz.

2. **Auslandsumsätze sind Betriebseinnahme.** Der § 19-Gesamtumsatz lässt sie
   aus, weil sie im Inland nicht steuerbar sind (§ 19 Abs. 2 i. V. m. § 1
   Abs. 1 Nr. 1 UStG). Die EÜR nicht: ertragsteuerlich zählt jeder Euro. Das
   ist der Punkt, an dem beide Auswertungen auseinanderlaufen, und wer ihn
   übersieht, erklärt zu wenig Gewinn.

3. **Nicht abziehbare Anteile.** Bewirtung ist zu 70 % abziehbar (§ 4 Abs. 5
   Satz 1 Nr. 2 EStG); der Rest gehört nicht weggelassen, sondern in eine eigene
   Kennzahl. Der Katalog trägt dafür ``anteil_abziehbar_promille`` und
   ``kennzahl_nicht_abziehbar``. Zwei Grenzen kommen aus ``pauschalen.json``:
   ein Geschenk über der Freigrenze (50 € brutto) ist ganz nicht abziehbar
   (§ 4 Abs. 5 Satz 1 Nr. 1 EStG), die Homeoffice-Pauschale ist auf den
   Jahreshöchstbetrag gekappt (§ 4 Abs. 5 Satz 1 Nr. 6c EStG).

Die **private Nutzungsquote** (``beleg.aufteilungsquote``) ist davon zu
unterscheiden und schon beim Buchen des Belegs verrechnet:
``betrag_betrieblich_cent`` ist der betriebliche Teil. Hier wird deshalb mit
dieser Spalte gerechnet, nicht mit dem Bruttobetrag.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.logik import pauschalen

#: Kennzahl der Betriebseinnahmen (Anlage EÜR, Zeile 12).
KZ_BETRIEBSEINNAHMEN = 111

#: Nachrichtliche Kennzahl für die im Inland nicht steuerbaren Umsätze.
KZ_NICHT_STEUERBAR = 119

#: Leistungsorte, deren Umsatz im Inland nicht steuerbar ist.
AUSLAND = ("EU_B2B_REVERSE_CHARGE", "DRITTLAND")

BEREICH_EINNAHMEN = 20
BEREICH_AUSGABEN = 25

#: Kategorien mit einer Grenze aus ``pauschalen.json``.
KAT_GESCHENKE = "GESCHENKE"
KAT_HOMEOFFICE = "HOMEOFFICE_PAUSCHALE"
#: Die Kategorie der Auslandseinnahmen ohne eigene Rechnung: nachrichtlich in KZ 119.
KAT_EINNAHME_AUSLAND = "EINNAHME_HONORAR_AUSLAND"
#: Erstattungen einer Betriebsausgabe: in KZ 111, aber kein Umsatz -- ebenfalls
#: nachrichtlich in KZ 119, damit KZ 111 minus KZ 119 der § 19-Umsatz bleibt.
KAT_EINNAHME_ERSTATTUNG = "EINNAHME_ERSTATTUNG"
#: Kennzahl fuer nicht abziehbare Geschenke, falls der Katalog keine nennt.
KZ_GESCHENKE_NICHT_ABZIEHBAR = 164


def _euro_kurz(cent: int) -> str:
    """Ein Grenzbetrag fuer den Zeilentext: volle Euro ohne ",00".

    >>> _euro_kurz(5000), _euro_kurz(126000), _euro_kurz(5050)
    ('50 €', '1.260 €', '50,50 €')
    """
    from app.logik.rundung import euro

    text = euro(cent)
    return (text[:-3] if text.endswith(",00") else text) + " €"


def _grenzwert(name: str, jahr: int) -> int:
    """Ein Wert aus ``pauschalen.json`` fuer ``jahr`` -- nie hart kodiert.

    Fuer ein Jahr ohne eigenen Eintrag gilt der naechstliegende (siehe
    :func:`pauschalen.fuer`); Wertgrenzen wie diese stehen seit Jahren.
    """
    return int(pauschalen.fuer(jahr)[name])


def _jahr_aus(datum: str | None, ersatz: int) -> int:
    try:
        return int(str(datum)[:4])
    except (TypeError, ValueError):
        return ersatz


def geschenk_text(freigrenze_cent: int) -> str:
    return (f"Geschenke über {_euro_kurz(freigrenze_cent)} — nicht abziehbar, "
            "§ 4 Abs. 5 Satz 1 Nr. 1 EStG")


def homeoffice_text(bezeichnung: str, hoechst_cent: int, erfasst_cent: int) -> str:
    from app.logik.rundung import euro

    return (f"{bezeichnung} — gekappt auf {_euro_kurz(hoechst_cent)}, "
            f"§ 4 Abs. 5 Satz 1 Nr. 6c EStG (erfasst: {euro(erfasst_cent)} €)")


@dataclass(frozen=True, slots=True)
class Zeile:
    kennzahl: int
    bezeichnung: str
    bereich: int
    betrag_cent: int
    #: Nur zur Anzeige: diese Zeile ist nachrichtlich und geht NICHT in den
    #: Gewinn ein. Sonst zählte man sie versehentlich doppelt.
    nachrichtlich: bool = False
    #: Woher eine Einnahmenzeile stammt (QUELLE_*), damit die Übersicht die
    #: Betriebseinnahmen ohne Blick auf die Bezeichnung aufteilen kann.
    quelle: str = ""


#: Herkunft einer Einnahmenzeile: Zahlungen auf eigene Rechnungen, Einnahmen
#: ohne eigene Rechnung (app/logik/einnahmen.py), Verkauf eines Anlageguts.
QUELLE_RECHNUNG = "RECHNUNG"
QUELLE_OHNE_RECHNUNG = "OHNE_RECHNUNG"
QUELLE_ANLAGE = "ANLAGE"
#: Ausgabenseite: die Abschreibung -- kein Geldabfluss im Jahr, deshalb weist
#: die Übersicht sie neben den Betriebsausgaben gesondert aus.
QUELLE_AFA = "AFA"


@dataclass(frozen=True, slots=True)
class Einnahmensummen:
    """Die Betriebseinnahmen eines Jahres, so wie die Übersicht sie zeigt.

    ALLE Einnahmen nach Zufluss, nicht nur die im Inland steuerbaren: der
    § 19-Zähler (app/logik/grenzwert.py) ist eine andere Zahl mit einem
    anderen Zweck, und wer beide nebeneinander sieht, muss die Differenz
    erklären können -- deshalb die Aufteilung nach Herkunft.
    """

    gesamt_cent: int
    rechnungen_cent: int
    ohne_rechnung_cent: int
    anlagenverkauf_cent: int
    #: Davon im Inland nicht steuerbar bzw. kein Umsatz (Ausland, Erstattung).
    nicht_steuerbar_cent: int


@dataclass(frozen=True, slots=True)
class Luecke:
    """Belege, deren Geld geflossen ist, die aber in keiner EUeR-Zeile stehen.

    Dazu die Eigenbelege OHNE Zahlungsdatum: ein von Hand erfasster Eigenbeleg
    ohne "Bezahlt am" ist nach § 11 Abs. 2 EStG noch keine Betriebsausgabe --
    und bei einem Eigenbeleg gibt es keinen Kontoauszug, der daran erinnert.
    """

    anzahl: int
    betrag_cent: int
    #: Eigenbelege ohne Zahlungszeile -- eigener Satz in der Meldung.
    eigenbelege_offen: int = 0
    eigenbelege_offen_cent: int = 0

    def __bool__(self) -> bool:
        return self.anzahl > 0 or self.eigenbelege_offen > 0


@dataclass(frozen=True, slots=True)
class Auswertung:
    jahr: int
    einnahmen: list[Zeile]
    ausgaben: list[Zeile]
    #: Bezahlte Belege in einer Anlagekategorie (KZ 130/131) ohne Anlagegut:
    #: sie stehen mit vollem Betrag in der AfA-Zeile -- ein Sofortabzug ueber
    #: der GWG-Grenze, den die Seite anmahnen muss.
    anlagekategorie_ohne_gut: Luecke = Luecke(0, 0)

    @property
    def einnahmen_cent(self) -> int:
        return sum(z.betrag_cent for z in self.einnahmen if not z.nachrichtlich)

    @property
    def ausgaben_cent(self) -> int:
        """Die GEWINNMINDERNDEN Ausgaben.

        Nachrichtliche Zeilen bleiben aussen vor: der nicht abziehbare Teil
        einer Bewirtung steht in der Anlage EUeR, mindert den Gewinn aber nicht
        (§ 4 Abs. 5 EStG).
        """
        return sum(z.betrag_cent for z in self.ausgaben if not z.nachrichtlich)

    @property
    def gewinn_cent(self) -> int:
        return self.einnahmen_cent - self.ausgaben_cent

    @property
    def afa_cent(self) -> int:
        """Der Teil der Ausgaben, der keine Zahlung ist: die Abschreibung."""
        return sum(z.betrag_cent for z in self.ausgaben
                   if z.quelle == QUELLE_AFA and not z.nachrichtlich)


def _betrieblicher_anteil(geflossen: int, brutto: int, betrieblich: int) -> int:
    """Der betriebliche Teil einer geflossenen Summe.

    Bei voller Zahlung ist das genau ``betrieblich``. Bei einer Teilzahlung
    waere es falsch, den vollen betrieblichen Anteil anzusetzen: wer von einer
    Rechnung ueber 100 Euro mit 60 Prozent betrieblichem Anteil erst 50 Euro
    zahlt, hat 30 Euro Betriebsausgabe, nicht 60.

    Kaufmaennisch gerundet, in ganzen Cent -- keine Fliesskommazahl.

    >>> _betrieblicher_anteil(10000, 10000, 6000)
    6000
    >>> _betrieblicher_anteil(5000, 10000, 6000)
    3000
    >>> _betrieblicher_anteil(10000, 10000, 10000)
    10000
    >>> _betrieblicher_anteil(3333, 10000, 6000)
    2000
    """
    if brutto <= 0 or betrieblich <= 0:
        return 0
    if geflossen >= brutto:
        # Voll (oder ueber-) bezahlt: der volle betriebliche Anteil, nicht mehr.
        return betrieblich
    return (geflossen * betrieblich * 2 + brutto) // (2 * brutto)


def einnahmen(conn: sqlite3.Connection, jahr: int) -> list[Zeile]:
    """Betriebseinnahmen nach Zahlungseingang, getrennt nach Leistungsort.

    Die Trennung ist keine Spielerei: Inland und Ausland landen beide in
    Kennzahl 111, aber nur das Inland zählt in den § 19-Gesamtumsatz. Wer die
    Zahlen später gegen den Umsatzzähler hält, muss die Differenz erklären
    können -- hier steht sie.
    """
    zeilen: list[Zeile] = []
    for leistungsorte, bezeichnung in (
        (("INLAND",), "Honorar & Dienstleistung (Inland)"),
        (AUSLAND, "Honorar Ausland (in DE nicht steuerbar)"),
    ):
        platzhalter = ", ".join("?" * len(leistungsorte))
        zeile = conn.execute(
            f"""
            SELECT COALESCE(SUM(z.betrag_cent), 0) AS cent
              FROM zahlung z
              JOIN rechnung r ON r.id = z.rechnung_id
             WHERE strftime('%Y', z.datum) = ?
               AND r.typ IN ('RECHNUNG', 'STORNO')
               AND r.leistungsort IN ({platzhalter})
            """,
            (f"{jahr:04d}", *leistungsorte),
        ).fetchone()
        if zeile["cent"]:
            zeilen.append(Zeile(
                KZ_BETRIEBSEINNAHMEN, bezeichnung, BEREICH_EINNAHMEN, int(zeile["cent"]),
                quelle=QUELLE_RECHNUNG,
            ))

    ausland = sum(
        z.betrag_cent for z in zeilen if "Ausland" in z.bezeichnung
    )
    erstattung = 0

    # Einnahmen ohne eigene Rechnung (app/logik/einnahmen.py): Affiliate-,
    # Werbe- und Plattformerloese, Erstattungen, Verkaeufe -- je Kategorie
    # eine Zeile, gezaehlt am Tag des Zuflusses. Nur Kategorien des
    # Einnahmenbereichs; die Tabelle laesst ohnehin keine andere zu.
    for zeile in conn.execute(
        """
        SELECT k.schluessel, k.kennzahl, k.bezeichnung, SUM(e.betrag_cent) AS cent
          FROM v_einnahme_gueltig e
          JOIN euer_kategorie k ON k.id = e.kategorie_id
         WHERE strftime('%Y', e.zufluss_am) = ?
           AND k.bereich = ?
         GROUP BY k.id
         ORDER BY k.sortierung, k.bezeichnung
        """,
        (f"{jahr:04d}", BEREICH_EINNAHMEN),
    ):
        if not zeile["cent"]:
            continue
        zeilen.append(Zeile(
            int(zeile["kennzahl"]), f"{zeile['bezeichnung']} — ohne eigene Rechnung",
            BEREICH_EINNAHMEN, int(zeile["cent"]), quelle=QUELLE_OHNE_RECHNUNG,
        ))
        if zeile["schluessel"] == KAT_EINNAHME_AUSLAND:
            ausland += int(zeile["cent"])
        elif (zeile["schluessel"] == KAT_EINNAHME_ERSTATTUNG
              and int(zeile["kennzahl"]) == KZ_BETRIEBSEINNAHMEN):
            # Die Erstattung einer Ausgabe ist Betriebseinnahme in KZ 111,
            # aber kein Umsatz. Das Finanzamt liest den Gesamtumsatz als
            # KZ 111 minus KZ 119; ohne sie hier laege er ueber dem, was das
            # Werkzeug selbst fuer § 19 zaehlt.
            erstattung += int(zeile["cent"])
    if ausland or erstattung:
        # Nachrichtlich: schon in Kennzahl 111 enthalten, hier nur, damit die
        # Zahl im Formular steht und nicht am Jahresende gesucht werden muss.
        zeilen.append(Zeile(
            KZ_NICHT_STEUERBAR,
            "davon im Inland nicht steuerbar" if not erstattung
            else "davon im Inland nicht steuerbar bzw. kein Umsatz (Erstattungen)",
            BEREICH_EINNAHMEN, ausland + erstattung, nachrichtlich=True,
        ))

    # Verkauf oder Entnahme eines Anlageguts: Betriebseinnahme in eigener
    # Kennzahl, gezaehlt im Jahr des Abgangs (dem Werkzeug ist nur dieses
    # Datum bekannt; der Zufluss liegt bei einem Verkauf meist im selben
    # Monat). Nicht im § 19-Gesamtumsatz: Umsaetze mit Wirtschaftsguetern des
    # Anlagevermoegens bleiben dort ausser Ansatz (§ 19 Abs. 2 Satz 2 UStG).
    from app.logik.anlagen import KZ_ANLAGENVERKAUF, abgaenge

    erloes, _ = abgaenge(conn, jahr)
    if erloes:
        zeilen.append(Zeile(
            KZ_ANLAGENVERKAUF, "Veräußerung oder Entnahme von Anlagevermögen",
            BEREICH_EINNAHMEN, erloes, quelle=QUELLE_ANLAGE,
        ))
    return zeilen


def einnahmen_summen(zeilen: list[Zeile]) -> Einnahmensummen:
    """Alle Betriebseinnahmen eines Jahres, aufgeteilt nach Herkunft.

    Nimmt die Zeilen der EÜR-Seite (:func:`einnahmen`, meist aus einer
    :class:`Auswertung`) und zählt sie nur zusammen -- die Übersicht darf nie
    eine andere Summe zeigen als die EÜR.
    """

    def summe(quelle: str) -> int:
        return sum(z.betrag_cent for z in zeilen if z.quelle == quelle and not z.nachrichtlich)

    return Einnahmensummen(
        gesamt_cent=sum(z.betrag_cent for z in zeilen if not z.nachrichtlich),
        rechnungen_cent=summe(QUELLE_RECHNUNG),
        ohne_rechnung_cent=summe(QUELLE_OHNE_RECHNUNG),
        anlagenverkauf_cent=summe(QUELLE_ANLAGE),
        nicht_steuerbar_cent=sum(z.betrag_cent for z in zeilen
                                 if z.nachrichtlich and z.kennzahl == KZ_NICHT_STEUERBAR),
    )


def ausgaben(conn: sqlite3.Connection, jahr: int) -> list[Zeile]:
    """Betriebsausgaben nach Zahlungsausgang, je Kennzahl.

    Grundlage ist ``zahlung`` mit gesetzter ``beleg_id`` -- ein Beleg ohne
    Zahlung ist noch keine Betriebsausgabe (§ 11 Abs. 2 EStG). Gelesen wird
    ueber ``v_beleg_gueltig``: die Zahlungen eines geloeschten Belegs bleiben
    in der Tabelle stehen (nachvollziehbar), zaehlen aber nicht -- hier so
    wenig wie in den Hinweisen und im Buchungsblatt.

    Gezaehlt wird der GEFLOSSENE Betrag, nicht der Belegbetrag. Das ist nicht
    dasselbe, sobald eine Rechnung in Raten bezahlt wird: ueber den Beleg
    summiert, zaehlte er einmal JE ZAHLUNGSZEILE -- zwei Raten machten aus
    100 Euro Ausgabe 200. Und bei einer Rate ueber den Jahreswechsel gehoert
    nach § 11 Abs. 2 EStG ohnehin jede Rate in ihr eigenes Jahr.

    Die private Nutzungsquote steckt in ``beleg.betrag_betrieblich_cent``; sie
    wird hier auf die geflossene Summe uebertragen, damit eine Teilzahlung
    nicht den vollen betrieblichen Anteil mitbringt.

    Zwei Grenzen aus ``pauschalen.json`` greifen hier:

    * **Geschenke** (Kategorie GESCHENKE): liegt der Bruttobetrag eines Belegs
      ueber ``geschenk_freigrenze_brutto_cent``, ist das GANZE Geschenk nicht
      abziehbar (§ 4 Abs. 5 Satz 1 Nr. 1 EStG; brutto nach R 9b Abs. 2 Satz 3
      EStR). Es steht nachrichtlich in Kz 164 statt abziehbar in Kz 174.
      Die Grenze gilt je Empfaenger und Jahr -- den Empfaenger kennt das
      Werkzeug nicht, geprueft wird deshalb je Beleg; mehrere kleine
      Geschenke an denselben Empfaenger muss der Nutzer selbst zusammenzaehlen.
    * **Homeoffice-Pauschale**: die Summe eines Jahres wird auf
      ``homeoffice_hoechstbetrag_cent`` gekappt (§ 4 Abs. 5 Satz 1 Nr. 6c
      EStG); die Zeile sagt das.
    """
    posten: dict[tuple[int, str], int] = {}
    nicht_abziehbar: dict[tuple[int, str], int] = {}
    homeoffice: set[tuple[int, str]] = set()

    for zeile in conn.execute(
        """
        SELECT k.kennzahl, k.bezeichnung, k.anteil_abziehbar_promille,
               k.kennzahl_nicht_abziehbar, k.schluessel,
               b.betrag_brutto_cent, b.betrag_betrieblich_cent, b.belegdatum,
               COALESCE(SUM(ABS(z.betrag_cent)), 0) AS geflossen
          FROM zahlung z
          JOIN v_beleg_gueltig b ON b.id = z.beleg_id
          JOIN euer_kategorie k  ON k.id = b.kategorie_id
         WHERE strftime('%Y', z.datum) = ?
           AND b.ist_anlagegut = 0
           -- Nur Ausgabenkategorien. Eine Einnahmenkategorie an einem Beleg
           -- (aus der Zeit vor Migration 26, als das nur die Route pruefte)
           -- zaehlte sonst als Ausgabe unter KZ 111; sie steht stattdessen
           -- in fehlende_zuordnung, wie ein Beleg ohne Kategorie.
           AND k.bereich = ?
         GROUP BY b.id
         ORDER BY k.sortierung, k.bezeichnung
        """,
        (f"{jahr:04d}", BEREICH_AUSGABEN),
    ):
        gesamt = _betrieblicher_anteil(
            int(zeile["geflossen"]),
            int(zeile["betrag_brutto_cent"]),
            int(zeile["betrag_betrieblich_cent"] or 0),
        )
        if not gesamt:
            continue
        if zeile["schluessel"] == KAT_GESCHENKE:
            freigrenze = _grenzwert(
                "geschenk_freigrenze_brutto_cent", _jahr_aus(zeile["belegdatum"], jahr)
            )
            if int(zeile["betrag_brutto_cent"]) > freigrenze:
                # Freigrenze, kein Freibetrag: drueber ist ALLES nicht
                # abziehbar, nicht nur der Teil ueber 50 €.
                schluessel = (
                    int(zeile["kennzahl_nicht_abziehbar"] or KZ_GESCHENKE_NICHT_ABZIEHBAR),
                    geschenk_text(freigrenze),
                )
                nicht_abziehbar[schluessel] = nicht_abziehbar.get(schluessel, 0) + gesamt
                continue
        anteil = int(zeile["anteil_abziehbar_promille"])
        abziehbar = gesamt * anteil // 1000
        schluessel = (int(zeile["kennzahl"]), zeile["bezeichnung"])
        posten[schluessel] = posten.get(schluessel, 0) + abziehbar
        if zeile["schluessel"] == KAT_HOMEOFFICE:
            homeoffice.add(schluessel)

        rest = gesamt - abziehbar
        if rest and zeile["kennzahl_nicht_abziehbar"]:
            # Der nicht abziehbare Teil verschwindet NICHT -- er gehoert in
            # seine eigene Kennzahl. Weggelassen waere der Gewinn zu niedrig.
            schluessel = (
                int(zeile["kennzahl_nicht_abziehbar"]),
                f"{zeile['bezeichnung']} — nicht abziehbarer Anteil",
            )
            nicht_abziehbar[schluessel] = nicht_abziehbar.get(schluessel, 0) + rest

    # Die Homeoffice-Pauschale ist ein Jahreshoechstbetrag: 6 € je Tag, aber
    # nie mehr als 1.260 € im Jahr. Was darueber erfasst ist, mindert den
    # Gewinn nicht -- die Zeile nennt Kappung und Grundlage.
    hoechst = _grenzwert("homeoffice_hoechstbetrag_cent", jahr)
    for schluessel in homeoffice:
        erfasst = posten[schluessel]
        if erfasst > hoechst:
            del posten[schluessel]
            kennzahl, bezeichnung = schluessel
            neu = (kennzahl, homeoffice_text(bezeichnung, hoechst, erfasst))
            posten[neu] = hoechst

    zeilen = [
        Zeile(kennzahl, bezeichnung, BEREICH_AUSGABEN, betrag)
        for (kennzahl, bezeichnung), betrag in sorted(posten.items())
    ]
    # NACHRICHTLICH, und das ist der entscheidende Punkt: der nicht abziehbare
    # Teil gehoert in seine eigene Kennzahl (die Anlage EUeR fragt ihn ab), darf
    # den Gewinn aber NICHT mindern -- genau das sagt § 4 Abs. 5 EStG. Zaehlte
    # er mit, waere die Aufteilung sinnlos: 100 Euro Bewirtung mit 70 Prozent
    # Abzug ergaeben wieder 100 Euro Betriebsausgabe.
    zeilen += [
        Zeile(kennzahl, bezeichnung, BEREICH_AUSGABEN, betrag, nachrichtlich=True)
        for (kennzahl, bezeichnung), betrag in sorted(nicht_abziehbar.items())
    ]

    # Die AfA folgt nicht dem Abflussprinzip: sie verteilt die
    # Anschaffungskosten ueber die Nutzungsdauer (§ 7 EStG) und steht deshalb
    # unabhaengig von einer Zahlung im Jahr.
    from app.logik.anlagen import KZ_RESTBUCHWERT, abgaenge, afa_summe

    for kennzahl, betrag in sorted(afa_summe(conn, jahr).items()):
        if betrag:
            zeilen.append(Zeile(
                int(kennzahl), "Abschreibungen (AfA)", BEREICH_AUSGABEN, int(betrag),
                quelle=QUELLE_AFA,
            ))

    # Scheidet ein Anlagegut aus, wird sein noch nicht abgeschriebener Rest
    # Betriebsausgabe -- ohne diese Zeile blieb er fuer immer in der Luft:
    # weder abgeschrieben noch abgesetzt, der Gewinn zu hoch.
    _, restbuchwert = abgaenge(conn, jahr)
    if restbuchwert:
        zeilen.append(Zeile(
            KZ_RESTBUCHWERT, "Restbuchwert ausgeschiedener Anlagegüter",
            BEREICH_AUSGABEN, restbuchwert,
        ))
    return zeilen


def fehlende_zuordnung(conn: sqlite3.Connection, jahr: int | None = None) -> Luecke:
    """Bezahlte Belege ohne Kategorie -- Ausgaben, die niemand sieht.

    Das ist die unangenehmste Sorte Fehler: nichts ist kaputt, nichts meldet
    sich, die Zahl ist nur zu niedrig. Ohne ``kategorie_id`` gibt es keine
    Kennzahl, ohne Kennzahl keine Zeile in :func:`ausgaben` -- und der Gewinn
    faellt um genau diesen Betrag zu hoch aus.

    Gezaehlt wird nur, was auch BEZAHLT ist: ein Beleg ohne Zahlung ist nach
    § 11 Abs. 2 EStG ohnehin noch keine Betriebsausgabe, da fehlt also nichts.

    Die Ausnahme sind EIGENBELEGE ohne Zahlungsdatum. Bei einem Fremdbeleg
    kommt die Zahlung noch; bei einem von Hand erfassten Eigenbeleg (Parkuhr,
    verblasster Kassenzettel) ist das Geld laengst weg, nur das Datum fehlt --
    und dann steht er in keiner EUeR, ohne dass etwas daran erinnert. Die
    Pauschalen aus den Reisen bekommen ihre Zahlung beim Buchen, sie tauchen
    hier nicht auf. Gezaehlt wird nach dem Belegdatum, ein Zahlungsdatum gibt
    es ja gerade nicht.
    """
    bedingung = "AND strftime('%Y', z.datum) = ?" if jahr else ""
    werte = (f"{jahr:04d}",) if jahr else ()
    zeile = conn.execute(
        f"""
        SELECT COUNT(*) AS anzahl, COALESCE(SUM(cent), 0) AS cent
          FROM (
            SELECT b.id,
                   MIN(b.betrag_betrieblich_cent) AS cent
              FROM v_beleg_gueltig b
              JOIN zahlung z ON z.beleg_id = b.id
             WHERE (b.kategorie_id IS NULL
                    OR NOT EXISTS (SELECT 1 FROM euer_kategorie k
                                    WHERE k.id = b.kategorie_id AND k.bereich = 25))
               AND b.ist_anlagegut = 0
               {bedingung}
             GROUP BY b.id
          )
        """,
        werte,
    ).fetchone()
    offen_bedingung = "AND strftime('%Y', b.belegdatum) = ?" if jahr else ""
    offen = conn.execute(
        f"""
        SELECT COUNT(*) AS anzahl, COALESCE(SUM(b.betrag_betrieblich_cent), 0) AS cent
          FROM v_beleg_gueltig b
         WHERE b.ist_eigenbeleg = 1
           AND b.ist_anlagegut = 0
           AND NOT EXISTS (SELECT 1 FROM zahlung z WHERE z.beleg_id = b.id)
           {offen_bedingung}
        """,
        werte,
    ).fetchone()
    return Luecke(int(zeile["anzahl"]), int(zeile["cent"]),
                  int(offen["anzahl"]), int(offen["cent"]))


def anlagegut_ohne_verzeichnis(conn: sqlite3.Connection, jahr: int | None = None) -> Luecke:
    """Belege mit Anlagegut-Haken, aber ohne Eintrag im Anlagenverzeichnis.

    Die Schwester von :func:`fehlende_zuordnung`, und die Luecke ist dieselbe
    Sorte: :func:`ausgaben` laesst einen Beleg mit ``ist_anlagegut = 1`` aus,
    weil er ueber die AfA laufen soll -- solange es das Anlagegut dazu nicht
    gibt, fehlt der Betrag ganz, weder Ausgabe noch Abschreibung. Vorher
    stand das nur auf /anlagen; die EUeR-Seite und die Startseite schwiegen.

    Gezaehlt wird unabhaengig von einer Zahlung: die AfA folgt der
    Anschaffung, nicht dem Abfluss. Mit ``jahr`` nur Belege, deren Belegdatum
    in diesem Jahr oder davor liegt -- ein Gut aus einem frueheren Jahr fehlt
    mit seiner AfA auch in den Folgejahren. Der Betrag ist der betriebliche
    Anteil, also das, was ueber die Nutzungsdauer abzuschreiben waere.
    """
    bedingung = "AND CAST(strftime('%Y', b.belegdatum) AS INTEGER) <= ?" if jahr else ""
    werte = (jahr,) if jahr else ()
    zeile = conn.execute(
        f"""
        SELECT COUNT(*) AS anzahl,
               COALESCE(SUM(COALESCE(b.betrag_betrieblich_cent, b.betrag_brutto_cent)), 0)
                   AS cent
          FROM v_beleg_gueltig b
         WHERE b.ist_anlagegut = 1
           AND NOT EXISTS (SELECT 1 FROM v_anlagegut_gueltig a WHERE a.beleg_id = b.id)
           {bedingung}
        """,
        werte,
    ).fetchone()
    return Luecke(int(zeile["anzahl"]), int(zeile["cent"]))


def anlagekategorie_ohne_gut(conn: sqlite3.Connection, jahr: int | None = None) -> Luecke:
    """Bezahlte Belege in der Anlagekategorie (KZ 130/131) ohne Anlagegut.

    Ein solcher Beleg steht in :func:`ausgaben` mit dem vollen Betrag in der
    AfA-Zeile -- ein Sofortabzug ueber der GWG-Grenze. Die Zahl steht in der
    richtigen Zeile und ist trotzdem falsch; deshalb muss die Seite es sagen.
    Die Liste selbst liefert :func:`app.logik.anlagen.belege_in_anlagekategorie_ohne_gut`.
    """
    from app.logik.anlagen import belege_in_anlagekategorie_ohne_gut

    belege = belege_in_anlagekategorie_ohne_gut(conn, jahr)
    return Luecke(len(belege), sum(int(b["betrag_betrieblich_cent"] or 0) for b in belege))


def auswertung(conn: sqlite3.Connection, jahr: int) -> Auswertung:
    """Die ganze EÜR eines Jahres."""
    return Auswertung(
        jahr=jahr, einnahmen=einnahmen(conn, jahr), ausgaben=ausgaben(conn, jahr),
        anlagekategorie_ohne_gut=anlagekategorie_ohne_gut(conn, jahr),
    )


def jahre(conn: sqlite3.Connection) -> list[int]:
    """Jahre, für die überhaupt etwas zu zeigen ist -- neueste zuerst."""
    # Ohne die Zahlungen und die AfA geloeschter Belege: ein Jahr, in dem nur
    # ein Testbeleg stand, ist kein Jahr mit etwas zu zeigen.
    gefunden = {
        int(z["jahr"])
        for z in conn.execute(
            "SELECT DISTINCT CAST(strftime('%Y', datum) AS INTEGER) AS jahr FROM zahlung"
            " WHERE beleg_id IS NULL OR beleg_id IN (SELECT id FROM v_beleg_gueltig)"
        )
        if z["jahr"] is not None
    }
    gefunden |= {
        int(z["jahr"])
        for z in conn.execute(
            "SELECT DISTINCT j.jahr FROM anlagegut_afa_jahr j"
            " JOIN v_anlagegut_gueltig g ON g.id = j.anlagegut_id")
    }
    # Und die Einnahmen ohne eigene Rechnung (Migration 26).
    from app.logik.einnahmen import jahre as einnahmejahre

    gefunden |= einnahmejahre(conn)
    return sorted(gefunden, reverse=True)


def standardjahr(jahre_liste: list[int], laufend: int) -> int:
    """Das Jahr, das die EÜR ohne (gueltige) Jahresangabe zeigt.

    Nicht einfach das juengste Jahr der Liste: :func:`jahre` fuehrt auch die
    AfA-Planjahre eines Anlageguts, und ein Laptop mit drei Jahren
    Nutzungsdauer liess die Seite in einem Jahr oeffnen, das noch nicht
    begonnen hat -- mit nichts als Abschreibung und einem Verlust. Die
    Reihenfolge: das laufende Jahr, wenn es etwas zu zeigen hat; sonst das
    juengste vergangene; sonst (nur kuenftige Jahre) das frueheste. Ohne
    jede Bewegung das laufende Jahr -- die Seite erklaert dann, was fehlt.
    Kuenftige Planjahre bleiben in der Auswahl.

    >>> standardjahr([2028, 2027, 2026], 2026)
    2026
    >>> standardjahr([2028, 2025, 2024], 2026)
    2025
    >>> standardjahr([2029, 2028], 2026)
    2028
    >>> standardjahr([], 2026)
    2026
    """
    if not jahre_liste or laufend in jahre_liste:
        return laufend
    vergangen = [j for j in jahre_liste if j <= laufend]
    return max(vergangen) if vergangen else min(jahre_liste)


def buchungen(conn: sqlite3.Connection, jahr: int | None = None) -> list[dict]:
    """Jeder Geldfluss als eine Zeile — die Vorstufe eines Buchungsstapels.

    Bewusst ohne Sachkonten: ein Kontenrahmen (SKR03 oder SKR04) und die
    Zuordnung je Kategorie gehoeren dem Steuerberater, nicht diesem Werkzeug.
    Geraten waere schlimmer als weggelassen -- ein falsch kontierter Stapel
    faellt erst im Jahresabschluss auf. Was hier steht, reicht, um die
    Zuordnung in einem Schritt zu ergaenzen, sobald der Kontenrahmen feststeht.
    """
    bedingung = "WHERE strftime('%Y', z.datum) = ?" if jahr else ""
    werte: tuple = (f"{jahr:04d}",) if jahr else ()

    eingang = conn.execute(
        f"""
        SELECT z.datum, z.betrag_cent, z.art, z.notiz, z.storno_von_id,
               r.nummer AS beleg, k.name AS gegenstelle,
               r.leistungsort, r.betreff
          FROM zahlung z
          JOIN rechnung r ON r.id = z.rechnung_id
          JOIN kunde k    ON k.id = r.kunde_id
        {bedingung}
         ORDER BY z.datum, z.id
        """,
        werte,
    ).fetchall()

    ausgang = conn.execute(
        f"""
        SELECT z.datum, z.betrag_cent, z.art,
               -- Ein Eigenbeleg hat keine Fremdnummer; "Beleg 7" wie im
               -- Excel-Blatt "Belege", sonst laesst sich die Zeile nicht
               -- zuordnen. Die Gegenstelle ist dann der Eigenbeleg selbst.
               COALESCE(b.belegnummer_extern, 'Beleg ' || b.id) AS beleg,
               COALESCE(l.name, CASE WHEN b.ist_eigenbeleg = 1 THEN 'Eigenbeleg' END)
                   AS gegenstelle,
               e.kennzahl, e.bezeichnung AS kategorie, b.notiz,
               e.schluessel AS kategorie_schluessel, b.belegdatum,
               b.betrag_brutto_cent, b.betrag_betrieblich_cent, b.ist_anlagegut,
               COALESCE(e.anteil_abziehbar_promille, 1000) AS anteil_abziehbar_promille,
               -- Was auf diesen Beleg VOR dieser Zahlung schon geflossen ist:
               -- damit "davon Betriebsausgabe" auf den noch offenen Belegbetrag
               -- gekappt wird. Eine zweite Zahlung auf einen voll bezahlten
               -- Beleg zaehlte sonst hier doppelt und in der EUeR einmal, und
               -- die beiden Blaetter widersprachen sich.
               (SELECT COALESCE(SUM(ABS(v.betrag_cent)), 0) FROM zahlung v
                 WHERE v.beleg_id = b.id
                   AND (v.datum < z.datum OR (v.datum = z.datum AND v.id < z.id)))
                   AS vorher_cent
          FROM zahlung z
          JOIN v_beleg_gueltig b ON b.id = z.beleg_id
          LEFT JOIN lieferant l  ON l.id = b.lieferant_id
          LEFT JOIN euer_kategorie e ON e.id = b.kategorie_id
        {bedingung}
         ORDER BY z.datum, z.id
        """,
        werte,
    ).fetchall()

    zeilen = [
        {
            "datum": z["datum"], "richtung": "Einnahme",
            "betrag_cent": int(z["betrag_cent"]),
            "betriebsausgabe_cent": None,
            "beleg": z["beleg"], "gegenstelle": z["gegenstelle"],
            "zahlungsart": z["art"],
            "kennzahl": KZ_BETRIEBSEINNAHMEN,
            "kategorie": (
                "Honorar Ausland (in DE nicht steuerbar)"
                if z["leistungsort"] in AUSLAND
                else "Honorar & Dienstleistung (Inland)"
            ),
            # Eine Gegenbuchung (zurueckgenommene Fehlbuchung) sagt, was sie
            # ist -- sonst stuende eine negative Einnahme mit dem Betreff der
            # Rechnung da, und niemand wuesste, warum.
            "text": (z["notiz"] if z["storno_von_id"] else z["betreff"]) or "",
        }
        for z in eingang
    ]
    # Einnahmen ohne eigene Rechnung: ein Zufluss je Zeile, wie eine Zahlung
    # auf eine Rechnung. "Einnahme 12" wie "Beleg 7" -- sonst liesse sich die
    # Zeile im Blatt "Einnahmen ohne Rechnung" nicht wiederfinden.
    einnahme_bedingung = "AND strftime('%Y', e.zufluss_am) = ?" if jahr else ""
    zeilen += [
        {
            "datum": z["zufluss_am"], "richtung": "Einnahme",
            "betrag_cent": int(z["betrag_cent"]),
            "betriebsausgabe_cent": None,
            "beleg": f"Einnahme {z['id']}", "gegenstelle": z["zahler"],
            "zahlungsart": None,
            "kennzahl": z["kennzahl"], "kategorie": z["kategorie"],
            "text": z["notiz"] or "",
        }
        for z in conn.execute(
            f"""
            SELECT e.id, e.zufluss_am, e.betrag_cent, e.zahler, e.notiz,
                   k.kennzahl, k.bezeichnung AS kategorie
              FROM v_einnahme_gueltig e
              JOIN euer_kategorie k ON k.id = e.kategorie_id
             WHERE k.bereich = {BEREICH_EINNAHMEN} {einnahme_bedingung}
             ORDER BY e.zufluss_am, e.id
            """,
            werte,
        )
    ]
    zeilen += [
        {
            "datum": z["datum"], "richtung": "Ausgabe",
            # Gespeichert sind Ausgabenzahlungen NEGATIV, damit eine Summe ueber
            # alle Zahlungen vorzeichenrichtig bleibt. Hier steht der Betrag
            # positiv: die Spalte "richtung" traegt die Richtung, und das Blatt
            # "EUeR" zeigt Ausgaben ebenfalls positiv. Zwei Vorzeichenregeln in
            # derselben Datei sind die sicherste Art, sich zu verrechnen.
            "betrag_cent": abs(int(z["betrag_cent"])),
            # Was von diesem Geldfluss tatsaechlich Betriebsausgabe ist:
            # private Nutzungsquote und Abzugsfaehigkeit (Bewirtung 70 %)
            # herausgerechnet, ein Anlagegut gar nicht (es wird abgeschrieben,
            # nicht abgesetzt). Der volle Betrag daneben bleibt stehen -- ein
            # Buchungsstapel zeigt den Geldfluss; wer die EUeR-Zeile daraus
            # ableitet, braucht diese Spalte und darf nicht die erste nehmen.
            "betriebsausgabe_cent": (
                0 if z["ist_anlagegut"] or _geschenk_ueber_freigrenze(z) else
                _betrieblicher_anteil(
                    # Nur der Teil dieser Zahlung, der den Beleg noch nicht
                    # ueberschreitet -- dieselbe Kappung wie in ausgaben().
                    min(abs(int(z["betrag_cent"])),
                        max(0, int(z["betrag_brutto_cent"]) - int(z["vorher_cent"]))),
                    int(z["betrag_brutto_cent"]),
                    int(z["betrag_betrieblich_cent"] or 0),
                ) * int(z["anteil_abziehbar_promille"]) // 1000
            ),
            "beleg": z["beleg"], "gegenstelle": z["gegenstelle"],
            "zahlungsart": z["art"],
            "kennzahl": z["kennzahl"],
            "kategorie": z["kategorie"],
            "text": z["notiz"] or "",
            "_homeoffice": z["kategorie_schluessel"] == KAT_HOMEOFFICE,
        }
        for z in ausgang
    ]
    zeilen.sort(key=lambda z: (z["datum"], z["richtung"]))

    # Dieselbe Kappung wie in ausgaben(): die Homeoffice-Pauschale mindert
    # je Jahr hoechstens den Hoechstbetrag. In zeitlicher Reihenfolge -- was
    # nach Erreichen der Grenze gezahlt ist, ist keine Betriebsausgabe mehr.
    # Ohne das zeigte das Blatt "Buchungen" mehr Betriebsausgabe als die EUeR.
    verbraucht: dict[int, int] = {}
    for zeile in zeilen:
        if not zeile.pop("_homeoffice", False) or not zeile["betriebsausgabe_cent"]:
            continue
        zjahr = _jahr_aus(zeile["datum"], 0)
        rest = max(0, _grenzwert("homeoffice_hoechstbetrag_cent", zjahr)
                   - verbraucht.get(zjahr, 0))
        zeile["betriebsausgabe_cent"] = min(zeile["betriebsausgabe_cent"], rest)
        verbraucht[zjahr] = verbraucht.get(zjahr, 0) + zeile["betriebsausgabe_cent"]
    return zeilen


def _geschenk_ueber_freigrenze(zeile) -> bool:
    """Ist dieser Buchungsbeleg ein Geschenk ueber der Freigrenze?"""
    if zeile["kategorie_schluessel"] != KAT_GESCHENKE:
        return False
    jahr = _jahr_aus(zeile["belegdatum"], _jahr_aus(zeile["datum"], 0))
    return int(zeile["betrag_brutto_cent"]) > _grenzwert("geschenk_freigrenze_brutto_cent", jahr)


def unbestaetigt_bezahlt(conn: sqlite3.Connection, jahr: int | None = None) -> Luecke:
    """Bezahlte, automatisch gebuchte Belege, die noch niemand bestaetigt hat.

    Das Abholen aus dem Eingangsordner und "Alle prüfen und buchen" buchen
    sicher erkannte Rechnungen selbst; ``geprueft_am`` bleibt leer, bis der
    Nutzer "Stimmt so" klickt.
    Ist die Zahlung erfasst, zaehlt so ein Beleg schon in der EUeR -- mit
    Werten, die niemand gesehen hat. Das ist kein Fehler, aber es soll
    dastehen, wie die Belege ohne Kategorie. Gezaehlt wird im Jahr der
    Zahlung (§ 11 EStG), wie in der EUeR.
    """
    from app.logik.beleg.pipeline import AUTOMATISCH_KENNUNG

    bedingung = "AND strftime('%Y', z.datum) = ?" if jahr else ""
    werte: tuple[object, ...] = (AUTOMATISCH_KENNUNG, *((f"{jahr:04d}",) if jahr else ()))
    zeile = conn.execute(
        f"""
        SELECT COUNT(*) AS anzahl, COALESCE(SUM(cent), 0) AS cent
          FROM (
            SELECT b.id, MIN(b.betrag_betrieblich_cent) AS cent
              FROM v_beleg_gueltig b
              JOIN zahlung z ON z.beleg_id = b.id
             WHERE b.geprueft_am IS NULL
               AND EXISTS (SELECT 1 FROM extraktion e WHERE e.beleg_id = b.id
                             AND instr(e.verfahren, ?) > 0)
               {bedingung}
             GROUP BY b.id
          )
        """,
        werte,
    ).fetchone()
    return Luecke(int(zeile["anzahl"]), int(zeile["cent"]))

"""Anlagevermoegen und Abschreibung.

Pflichtbestandteil der EUeR, sobald Anlagevermoegen existiert: die Anlage
AVEUeR. Und kein Formalismus -- die AfA-Betraege der EUeR kommen von hier.

Die eine Sache, die hier richtig sein muss
------------------------------------------
Bei den Wertgrenzen zaehlen **zwei verschiedene Bezugsgroessen**, und das ist
die haeufigste Fehlerquelle in selbstgebauten Werkzeugen:

* Die **Wertgrenze** wird nach R 9b Abs. 2 EStR **stets netto** geprueft --
  "Ob der Vorsteuerbetrag umsatzsteuerrechtlich abziehbar ist, spielt in diesem
  Fall keine Rolle." 800 EUR netto sind also rund **952 EUR brutto**.
* **Abgeschrieben** wird dagegen der **Bruttobetrag**, weil die nicht
  abziehbare Vorsteuer nach R 9b Abs. 1 EStR zu den Anschaffungskosten gehoert.

Wer nur eine Zahl fuehrt, prueft die Grenze am Buchungswert -- und schreibt
dann Gueter ab, die sofort abziehbar waeren, oder zieht Gueter sofort ab, die
ins Verzeichnis gehoeren. Beides faellt erst in der Betriebspruefung auf.

Bei **Geschenken** gilt es umgekehrt: dort zaehlt der Bruttobetrag schon fuer
die 50-EUR-Freigrenze (R 9b Abs. 2 Satz 3 EStR). Diese Asymmetrie ist der
Grund, warum sich die Ratgeber im Netz widersprechen.

Was bewusst nicht gebaut ist
----------------------------
Der **Sammelposten nach § 6 Abs. 2a EStG** (Poolabschreibung). Er ist eine
Falle: die Wahl gilt einheitlich fuer **alle** Wirtschaftsgueter des Jahres und
senkt die Sofortabzugsgrenze von 800 auf 250 EUR. Wer ihn fuer ein Gut nimmt,
bindet sich fuer das ganze Jahr. Ein Werkzeug, das beides anbietet, verleitet
zu einer Wahl, deren Nebenwirkung niemand sieht.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from app import db
from app.logik import eingabe, kategorien, pauschalen
from app.logik.rundung import euro

#: Methoden der Abschreibung.
GWG_SOFORT = "GWG_SOFORT"
EIN_JAHR_IT = "EIN_JAHR_IT"
LINEAR = "LINEAR"
METHODEN = (GWG_SOFORT, EIN_JAHR_IT, LINEAR)

#: Kennzahlen der Anlage EUeR fuer die AfA.
KZ_BEWEGLICH = 130
KZ_IMMATERIELL = 131
KZ_GWG = 132
#: Erloes aus Verkauf oder Entnahme eines Anlageguts (Betriebseinnahme) und
#: der Restbuchwert eines ausgeschiedenen Guts (Betriebsausgabe). Juengere
#: Vordrucke uebernehmen den Restbuchwert aus der Spalte "Abgaenge" der Anlage
#: AVEUeR; die Kennzahl 135 traegt ihn in der EUeR-Auswertung hier.
KZ_ANLAGENVERKAUF = 102
KZ_RESTBUCHWERT = 135

#: Betriebsgewoehnliche Nutzungsdauern, die hier vorkommen. Aus den amtlichen
#: AfA-Tabellen; sie sind Schaetzwerte und duerfen begruendet abweichen,
#: deshalb nur Vorschlaege.
NUTZUNGSDAUERN = {
    "Computerhardware (Notebook, Rechner, Monitor)": 1,
    "Anwendersoftware": 1,
    "Mobiltelefon": 5,
    "Büromöbel (Schreibtisch, Stuhl, Regal)": 13,
    "Drucker, Scanner": 3,
    "Werkzeuge, Kleingeräte": 5,
    "Pkw": 6,
}


#: Laengste Nutzungsdauer, die angenommen wird. Die AfA-Tabellen kennen
#: Gebaeude mit 50 Jahren; darueber ist es ein Tippfehler. Ohne Obergrenze
#: schrieb 10**7 AfA-Zeilen bis ins Jahr 10002025, und 2**63-1 hielt den
#: Rechner endlos in der Schleife von :func:`afa_reihe` fest.
NUTZUNGSDAUER_HOECHSTENS = 50


class AnlagenFehler(RuntimeError):
    """Ein Anlagegut, das so nicht angelegt werden kann."""


def _de(iso: str) -> str:
    """ISO-Datum als 30.06.2027 fuer Meldungen; Unlesbares bleibt, wie es ist.

    >>> _de("2027-06-30"), _de("kaputt")
    ('30.06.2027', 'kaputt')
    """
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(iso)


def _heute() -> date:
    return date.fromisoformat(db.heute())


def _gut_lesen(conn: sqlite3.Connection, anlagegut_id: int) -> sqlite3.Row:
    """Das Anlagegut oder eine Meldung -- auch fuer Nummern, die SQLite nicht bindet.

    Eine Kennung ab 2**63 aus der Adresse sprengte die Bindung mit einem
    OverflowError, und der Nutzer sah die 500-Seite.
    """
    if not 1 <= anlagegut_id <= eingabe.ID_HOECHSTENS:
        raise AnlagenFehler("Dieses Anlagegut gibt es nicht.")
    gut = conn.execute("SELECT * FROM anlagegut WHERE id = ?", (anlagegut_id,)).fetchone()
    if gut is None:
        raise AnlagenFehler("Dieses Anlagegut gibt es nicht.")
    if gut["geloescht_am"] is not None:
        # Nur ueber eine veraltete Seite erreichbar; der Trigger wiese den
        # Abgang ab -- als 500-Seite.
        raise AnlagenFehler(f"Das Anlagegut „{gut['bezeichnung']}“ ist mit seinem Beleg "
                            "gelöscht. Erst den Beleg wiederherstellen.")
    return gut


@dataclass(frozen=True, slots=True)
class Einstufung:
    """Was aus einem Wert folgt: Methode, Kennzahl und Begruendung."""

    methode: str
    kennzahl: int
    ins_verzeichnis: bool
    begruendung: str
    grenze_netto_cent: int
    verzeichnis_ab_netto_cent: int

    @property
    def sofort_abziehbar(self) -> bool:
        return self.methode in (GWG_SOFORT, EIN_JAHR_IT)


def stufe_ein(
    netto_cent: int, *, jahr: int, art: str = "BEWEGLICH", ist_it: bool = False
) -> Einstufung:
    """Entscheidet die Behandlung eines Wirtschaftsguts an seinem NETTO-Wert.

    Die drei Stufen:

    * bis 250 EUR netto: sofort abziehbar, kein Verzeichnis noetig
    * 250 bis 800 EUR netto: sofort abziehbar (§ 6 Abs. 2 EStG), aber
      **gesondert aufzuzeichnen** -- Tag der Anschaffung und
      Anschaffungskosten (§ 6 Abs. 2 Satz 4 EStG)
    * ueber 800 EUR netto: Anlagegut, gehoert in die AVEUeR. Computerhardware
      und Anwendersoftware duerfen dabei mit Nutzungsdauer 1 Jahr im
      Anschaffungsjahr voll abgezogen werden -- muessen aber **trotzdem** ins
      Verzeichnis.

    >>> stufe_ein(20000, jahr=2026).methode
    'GWG_SOFORT'
    >>> stufe_ein(20000, jahr=2026).ins_verzeichnis
    False
    >>> stufe_ein(45000, jahr=2026).ins_verzeichnis
    True
    >>> stufe_ein(240000, jahr=2026, ist_it=True).methode
    'EIN_JAHR_IT'
    >>> stufe_ein(240000, jahr=2026).methode
    'LINEAR'
    """
    satz = pauschalen.fuer(jahr)
    grenze = satz["gwg_grenze_netto_cent"]
    verzeichnis_ab = satz["gwg_verzeichnis_ab_netto_cent"]
    kz = KZ_IMMATERIELL if art == "IMMATERIELL" else KZ_BEWEGLICH

    if netto_cent <= verzeichnis_ab:
        return Einstufung(
            GWG_SOFORT, KZ_GWG, False,
            f"{euro(netto_cent)} € netto, bis {euro(verzeichnis_ab)} € netto "
            "sofort abziehbar und ohne Verzeichnispflicht.",
            grenze, verzeichnis_ab,
        )
    if netto_cent <= grenze:
        return Einstufung(
            GWG_SOFORT, KZ_GWG, True,
            f"{euro(netto_cent)} € netto, geringwertiges Wirtschaftsgut nach "
            f"§ 6 Abs. 2 EStG: sofort abziehbar, aber ab "
            f"{euro(verzeichnis_ab)} € netto gesondert aufzuzeichnen.",
            grenze, verzeichnis_ab,
        )
    if ist_it:
        return Einstufung(
            EIN_JAHR_IT, kz, True,
            f"{euro(netto_cent)} € netto, über {euro(grenze)} € — aber "
            "Computerhardware und Anwendersoftware dürfen mit Nutzungsdauer 1 Jahr "
            "im Anschaffungsjahr voll abgezogen werden. Gehört trotzdem ins "
            "Anlagenverzeichnis.",
            grenze, verzeichnis_ab,
        )
    return Einstufung(
        LINEAR, kz, True,
        f"{euro(netto_cent)} € netto, über {euro(grenze)} € netto: Anlagegut, "
        "lineare Abschreibung über die betriebsgewöhnliche Nutzungsdauer.",
        grenze, verzeichnis_ab,
    )


@dataclass(frozen=True, slots=True)
class AfaJahr:
    jahr: int
    afa_cent: int
    restwert_cent: int
    kennzahl: int
    #: Wahr, wenn in diesem Jahr nur ein Teil des Jahres abgeschrieben wird.
    anteilig_monate: int | None = None


def afa_reihe(
    *,
    ak_brutto_cent: int,
    anschaffung_am: str,
    nutzungsdauer_jahre: int,
    methode: str,
    kennzahl: int,
    abgang_am: str | None = None,
) -> list[AfaJahr]:
    """Die Abschreibungsreihe eines Wirtschaftsguts.

    Abgeschrieben wird der **Bruttowert** (R 9b Abs. 1 EStR) -- die nicht
    abziehbare Vorsteuer gehoert zu den Anschaffungskosten.

    Im Anschaffungsjahr wird **zeitanteilig** (pro rata temporis) gerechnet:
    je angefangenem Monat ein Zwoelftel (§ 7 Abs. 1 Satz 4 EStG). Ein im
    Oktober gekaufter Schreibtisch bringt also drei Zwoelftel, nicht ein ganzes
    Jahr -- der haeufigste stille Fehler bei selbst gerechneter AfA.

    Ein Sofortabzug (GWG oder die Ein-Jahres-Regel fuer IT) ist davon
    ausgenommen: er wirkt im Anschaffungsjahr in voller Hoehe.

    >>> [(j.jahr, j.afa_cent) for j in afa_reihe(
    ...     ak_brutto_cent=95200, anschaffung_am="2026-01-15",
    ...     nutzungsdauer_jahre=1, methode="EIN_JAHR_IT", kennzahl=130)]
    [(2026, 95200)]
    >>> [(j.jahr, j.afa_cent) for j in afa_reihe(
    ...     ak_brutto_cent=120000, anschaffung_am="2026-10-05",
    ...     nutzungsdauer_jahre=3, methode="LINEAR", kennzahl=130)]
    [(2026, 10000), (2027, 40000), (2028, 40000), (2029, 30000)]

    Beim **Abgang** endet die Reihe im Abgangsjahr, und dort wird zeitanteilig
    bis zum Monat des Abgangs gerechnet (R 7.4 Abs. 8 EStR; der Monat des
    Abgangs zaehlt mit). Was dann noch nicht abgeschrieben ist, ist der
    Restbuchwert -- Betriebsausgabe im Abgangsjahr, aber keine AfA. Er bleibt
    deshalb als Restwert der letzten Zeile stehen und wird NICHT auf sie
    aufgeschlagen:

    >>> [(j.jahr, j.afa_cent, j.restwert_cent) for j in afa_reihe(
    ...     ak_brutto_cent=120000, anschaffung_am="2026-01-05",
    ...     nutzungsdauer_jahre=3, methode="LINEAR", kennzahl=130,
    ...     abgang_am="2027-06-30")]
    [(2026, 40000, 80000), (2027, 20000, 60000)]
    """
    tag = date.fromisoformat(anschaffung_am)
    abgang = date.fromisoformat(abgang_am) if abgang_am else None
    if methode in (GWG_SOFORT, EIN_JAHR_IT):
        # Sofortabzug: der volle Betrag im Anschaffungsjahr, kein Zeitanteil.
        return [AfaJahr(tag.year, ak_brutto_cent, 0, kennzahl)]

    if nutzungsdauer_jahre < 1:
        raise AnlagenFehler("Die Nutzungsdauer muss mindestens ein Jahr sein.")

    jahresbetrag = ak_brutto_cent // nutzungsdauer_jahre
    monate_erstes_jahr = 13 - tag.month
    reihe: list[AfaJahr] = []
    rest = ak_brutto_cent
    # Die Nutzungsdauer laeuft in Monaten. Ab Januar endet sie mit dem Dezember
    # des letzten vollen Jahres -- es gibt dann KEIN angebrochenes Restjahr
    # (§ 7 Abs. 1 Satz 4 EStG: 36 Monate ab Januar enden im Dezember des
    # dritten Jahres). Vorher bekam der Rundungsrest in diesem Fall eine
    # eigene Zeile mit null Monaten im Jahr danach, und das Gut stand ein
    # Jahr laenger im Verzeichnis als seine Nutzungsdauer.
    januar = tag.month == 1
    ende = tag.year + nutzungsdauer_jahre - (1 if januar else 0)

    for jahr in range(tag.year, ende + 1):
        if rest <= 0:
            break
        if abgang and jahr > abgang.year:
            break
        if jahr == tag.year:
            betrag = jahresbetrag * monate_erstes_jahr // 12
            anteilig = None if januar else monate_erstes_jahr
        elif jahr == ende:
            # Letztes Jahr: der Rest, damit die Summe exakt den
            # Anschaffungskosten entspricht. Eine Abschreibungsreihe, die einen
            # Cent uebrig laesst, hinterlaesst ein Gut mit Restwert 0,01 € im
            # Verzeichnis -- fuer immer.
            betrag = rest
            anteilig = None if januar else tag.month - 1
        else:
            betrag = jahresbetrag
            anteilig = None
        if abgang and jahr == abgang.year:
            erster_monat = tag.month if jahr == tag.year else 1
            monate = max(0, abgang.month - erster_monat + 1)
            betrag = jahresbetrag * monate // 12
            anteilig = monate
        betrag = min(betrag, rest)
        rest -= betrag
        if betrag or jahr == tag.year:
            reihe.append(AfaJahr(jahr, betrag, rest, kennzahl, anteilig))

    if rest and reihe and not abgang:
        # Rundungsrest auf das letzte Jahr: siehe oben.
        letztes = reihe[-1]
        reihe[-1] = AfaJahr(letztes.jahr, letztes.afa_cent + rest, 0,
                            letztes.kennzahl, letztes.anteilig_monate)
    return reihe


def _betrieblich(cent: int, quote: int) -> int:
    """Der betriebliche Anteil eines Betrags, kaufmaennisch gerundet.

    >>> _betrieblich(10000, 70), _betrieblich(33, 50), _betrieblich(10000, 100)
    (7000, 17, 10000)
    """
    return (cent * quote + 50) // 100


def _reihe_schreiben(conn: sqlite3.Connection, gut_id: int, reihe: list[AfaJahr]) -> None:
    """Ersetzt die gespeicherte AfA-Reihe eines Guts durch ``reihe``.

    Die Reihe ist aus den Stammdaten des Guts jederzeit neu zu rechnen; was in
    der Tabelle steht, ist die Rechnung, nicht die Quelle. Deshalb darf sie
    ersetzt werden -- beim Abgang, und wenn der Abgang zurueckgenommen wird.
    """
    conn.execute("DELETE FROM anlagegut_afa_jahr WHERE anlagegut_id = ?", (gut_id,))
    for jahr in reihe:
        conn.execute(
            "INSERT INTO anlagegut_afa_jahr (anlagegut_id, jahr, afa_cent,"
            " kennzahl, restwert_cent) VALUES (?, ?, ?, ?, ?)",
            (gut_id, jahr.jahr, jahr.afa_cent, jahr.kennzahl, jahr.restwert_cent),
        )


def _reihe_fuer(gut: sqlite3.Row, kennzahl: int, abgang_am: str | None) -> list[AfaJahr]:
    return afa_reihe(
        ak_brutto_cent=int(gut["ak_brutto_cent"]), anschaffung_am=gut["anschaffung_am"],
        nutzungsdauer_jahre=int(gut["nutzungsdauer_jahre"]), methode=gut["methode"],
        kennzahl=kennzahl, abgang_am=abgang_am,
    )


def _kennzahl_von(conn: sqlite3.Connection, gut_id: int) -> int:
    zeile = conn.execute(
        "SELECT kennzahl FROM anlagegut_afa_jahr WHERE anlagegut_id = ? LIMIT 1", (gut_id,)
    ).fetchone()
    return int(zeile["kennzahl"]) if zeile else KZ_BEWEGLICH


def anlegen(
    conn: sqlite3.Connection,
    *,
    bezeichnung: str,
    anschaffung_am: str,
    ak_brutto_cent: int,
    ak_netto_cent: int | None = None,
    ust_satz_promille: int | None = None,
    nutzungsdauer_jahre: int | None = None,
    art: str = "BEWEGLICH",
    ist_it: bool = False,
    beleg_id: int | None = None,
    notiz: str | None = None,
    aufteilungsquote: int | None = None,
    transaktion: bool = True,
    heute: date | None = None,
) -> int:
    """Legt ein Anlagegut an und rechnet seine AfA-Reihe aus.

    Oeffnet die Transaktion selbst -- ausser mit ``transaktion=False``: dann
    laeuft es in der offenen Transaktion des Aufrufers (der WISO-Import legt
    Beleg, Zahlung und Gut in EINER an), und ein Fehler bricht dessen
    Transaktion ab. Gibt die ID zurueck.

    ``aufteilungsquote`` ist der betriebliche Anteil in Prozent. Ohne Angabe
    kommt er vom Beleg, ohne Beleg ist er 100. Die AfA-Reihe selbst wird auf
    den vollen Anschaffungskosten gerechnet (das Gut ist ganz oder gar nicht
    im Verzeichnis); in die EUeR geht nur der betriebliche Anteil -- siehe
    :func:`afa_summe`.
    """
    if not bezeichnung.strip():
        raise AnlagenFehler("Ohne Bezeichnung lässt sich ein Anlagegut nicht wiederfinden.")
    if beleg_id is not None and not 1 <= beleg_id <= eingabe.ID_HOECHSTENS:
        raise AnlagenFehler("Diesen Beleg gibt es nicht.")
    if beleg_id:
        # Der Beleg muss existieren: sonst scheiterte erst der Fremdschluessel
        # beim INSERT, und der Nutzer sah die 500-Seite statt einer Meldung.
        # Das versteckte Feld fuellt JavaScript -- eine veraltete Seite reicht.
        beleg = conn.execute(
            "SELECT aufteilungsquote, geloescht_am FROM beleg WHERE id = ?", (beleg_id,)
        ).fetchone()
        if beleg is None:
            raise AnlagenFehler("Diesen Beleg gibt es nicht.")
        if beleg["geloescht_am"] is not None:
            # Eine veraltete Seite reicht: der Beleg wurde inzwischen
            # geloescht. Der Trigger wiese das Gut auch ab -- hier die Meldung.
            raise AnlagenFehler(f"Beleg {beleg_id} ist gelöscht — aus ihm entsteht kein "
                                "Anlagegut. Erst den Beleg wiederherstellen.")
        # Ein Beleg wird hoechstens einmal zum Anlagegut: zweimal erfasst
        # zaehlte die AfA doppelt, und zwar jedes Jahr der Nutzungsdauer. Der
        # UNIQUE-Index im Schema faengt es auch; hier gibt es die Meldung dazu.
        vorhanden = conn.execute(
            "SELECT bezeichnung FROM anlagegut WHERE beleg_id = ?", (beleg_id,)
        ).fetchone()
        if vorhanden is not None:
            raise AnlagenFehler(
                f"Dieser Beleg ist bereits als Anlagegut „{vorhanden['bezeichnung']}“ "
                "erfasst. Zweimal erfasst zählte die Abschreibung doppelt."
            )
        if aufteilungsquote is None:
            aufteilungsquote = int(beleg["aufteilungsquote"])
    quote = 100 if aufteilungsquote is None else int(aufteilungsquote)
    if not 0 <= quote <= 100:
        raise AnlagenFehler(
            f"Der betriebliche Anteil muss zwischen 0 und 100 liegen, nicht {quote}."
        )
    if quote == 0:
        # Ein Gut ohne betrieblichen Anteil brachte nie AfA in die EUeR und
        # stuende trotzdem im Verzeichnis -- das ist kein Betriebsvermoegen.
        raise AnlagenFehler(
            "0 % ist kein Betriebsvermögen: ein Gut ohne betrieblichen Anteil gehört "
            "nicht ins Anlagenverzeichnis."
        )
    # Dasselbe Fenster wie bei Rechnung und Zahlung: Jahr ab 2000, nicht in
    # der Zukunft, gespeichert wird das GELESENE Datum. Vorher landete ein
    # "0026" (zweistellig getippt) mit AfA-Jahren 26 bis 28 im Verzeichnis,
    # und ein "20260310" stand roh in der Datenbank.
    try:
        anschaffung_am = eingabe.datum(anschaffung_am, feld="Anschaffungsdatum",
                                       heute=heute or _heute())
    except ValueError as fehler:
        text = str(fehler)
        if "Zukunft" in text:
            text += (" Ein Anlagegut wird ab dem Tag der Anschaffung abgeschrieben, "
                     "nicht vorher.")
        raise AnlagenFehler(text) from fehler
    tag = date.fromisoformat(anschaffung_am)
    if ak_brutto_cent <= 0:
        raise AnlagenFehler("Ein Anlagegut über null Euro gibt es nicht.")
    if ust_satz_promille is not None and not 0 <= ust_satz_promille <= 1000:
        raise AnlagenFehler(
            "Der Steuersatz muss zwischen 0 und 100 Prozent liegen."
        )

    # Der Nettowert allein fuer die Wertgrenze (R 9b Abs. 2 EStR). Liegt keiner
    # vor, wird aus dem Brutto zurueckgerechnet; ohne Steuersatz ist Netto
    # gleich Brutto -- eine Steuer, die nicht erhoben wurde, kann man nicht
    # herausrechnen. Ein eingetragenes "0,00" ist dagegen KEIN "leer": wer das
    # tippt, meint etwas, und still den gerechneten Wert zu nehmen versteckte
    # den Tippfehler. Die Schema-CHECKs (netto > 0, netto <= brutto) kommen
    # erst beim INSERT; hier gibt es die Meldung dazu statt der 500-Seite.
    if ak_netto_cent is None:
        netto = kategorien.netto_fuer_grenzwert_cent(
            ak_brutto_cent, None, ust_satz_promille
        )
    else:
        netto = ak_netto_cent
        if netto <= 0:
            raise AnlagenFehler(
                "Ein Nettobetrag von 0,00 € ergibt kein Anlagegut. Das Feld leer "
                "lassen, dann wird der Nettowert aus dem Steuersatz gerechnet."
            )
    if netto > ak_brutto_cent:
        raise AnlagenFehler(
            f"Der Nettobetrag ({euro(netto)} €) kann nicht über dem Bruttobetrag "
            f"({euro(ak_brutto_cent)} €) liegen — sind die beiden vertauscht?"
        )
    einstufung = stufe_ein(netto, jahr=tag.year, art=art, ist_it=ist_it)

    if einstufung.methode == EIN_JAHR_IT:
        dauer = 1
    elif einstufung.methode == GWG_SOFORT:
        dauer = 1
    else:
        dauer = nutzungsdauer_jahre or 0
        if dauer < 1:
            raise AnlagenFehler(
                f"{bezeichnung.strip()} liegt mit {euro(netto)} € netto über der "
                f"Grenze von {euro(einstufung.grenze_netto_cent)} € und wird linear "
                "abgeschrieben — dafür braucht es eine Nutzungsdauer in Jahren."
            )
        if dauer > NUTZUNGSDAUER_HOECHSTENS:
            raise AnlagenFehler(
                f"Eine Nutzungsdauer von {dauer} Jahren ist unplausibel — höchstens "
                f"{NUTZUNGSDAUER_HOECHSTENS} Jahre (Gebäude). Bitte die Jahre prüfen."
            )

    reihe = afa_reihe(
        ak_brutto_cent=ak_brutto_cent, anschaffung_am=anschaffung_am,
        nutzungsdauer_jahre=dauer, methode=einstufung.methode,
        kennzahl=einstufung.kennzahl,
    )

    if transaktion:
        conn.execute("BEGIN IMMEDIATE")
    try:
        gut_id = conn.execute(
            "INSERT INTO anlagegut (beleg_id, bezeichnung, anschaffung_am,"
            " ak_brutto_cent, ak_netto_cent, aufteilungsquote, art, nutzungsdauer_jahre,"
            " methode, notiz, erfasst_am) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (beleg_id, bezeichnung.strip(), anschaffung_am, ak_brutto_cent, netto, quote,
             art, dauer, einstufung.methode, (notiz or "").strip() or None,
             db.jetzt_utc()),
        ).lastrowid
        _reihe_schreiben(conn, int(gut_id), reihe)
        if beleg_id:
            conn.execute("UPDATE beleg SET ist_anlagegut = 1 WHERE id = ?", (beleg_id,))
        db.protokolliere(
            conn, "ANLAGEGUT", "anlagegut", gut_id,
            wert_neu={"bezeichnung": bezeichnung.strip(),
                      "brutto_cent": ak_brutto_cent, "netto_cent": netto,
                      "methode": einstufung.methode, "betrieblich_prozent": quote},
            begruendung=einstufung.begruendung,
        )
        if transaktion:
            conn.execute("COMMIT")
    except sqlite3.IntegrityError as fehler:
        # Das Netz unter den Pruefungen oben: was das Schema noch abweist,
        # wird eine Meldung und keine 500-Seite.
        if transaktion:
            conn.execute("ROLLBACK")
        raise AnlagenFehler(
            f"Die Datenbank hat das Anlagegut abgewiesen ({fehler}). Bitte die "
            "Beträge, den Steuersatz und die Nutzungsdauer prüfen."
        ) from fehler
    except Exception:
        if transaktion:
            conn.execute("ROLLBACK")
        raise
    return gut_id


def nutzungsdauer_kandidaten(ak_brutto_cent: int, anschaffung_am: str, afa_cent: int, *,
                             jahr: int | None = None) -> list[tuple[int, str]]:
    """Alle Nutzungsdauern, bei denen die AfA eines Jahres ``afa_cent`` ergibt.

    Fuer den WISO-Import: WISO zeigt die geplante Jahres-AfA, aber nicht die
    Nutzungsdauer. Gerechnet wird mit derselben Reihe wie im Werkzeug
    (monatsgenau im Anschaffungsjahr), gesucht jede Dauer, deren AfA fuer
    ``jahr`` (Vorgabe: das Anschaffungsjahr) genau passt -- als (Jahre,
    Methode); die volle AfA im Anschaffungsjahr ist die Ein-Jahres-Regel fuer
    Computer (EIN_JAHR_IT).

    Im Anschaffungsjahr ist das Ergebnis eindeutig (gleiche Monate, andere
    Dauer, andere AfA). Fuer ein spaeteres Jahr nicht: das Restjahr einer
    kurzen Dauer kann so hoch sein wie ein volles Jahr einer langen (1.500 €
    im Mai 2025, 500 € AfA 2026: ein Jahr, Rest -- oder drei Jahre, voll).

    >>> nutzungsdauer_kandidaten(150000, "2025-05-10", 50000, jahr=2026)
    [(1, 'LINEAR'), (3, 'LINEAR')]
    """
    tag = date.fromisoformat(anschaffung_am)
    jahr = jahr or tag.year
    if jahr == tag.year and afa_cent == ak_brutto_cent:
        return [(1, EIN_JAHR_IT)]
    gefunden: list[tuple[int, str]] = []
    for dauer in range(1, NUTZUNGSDAUER_HOECHSTENS + 1):
        reihe = afa_reihe(ak_brutto_cent=ak_brutto_cent, anschaffung_am=anschaffung_am,
                          nutzungsdauer_jahre=dauer, methode=LINEAR, kennzahl=KZ_BEWEGLICH)
        if any(j.jahr == jahr and j.afa_cent == afa_cent for j in reihe):
            gefunden.append((dauer, LINEAR))
    return gefunden


def nutzungsdauer_ableiten(ak_brutto_cent: int, anschaffung_am: str, afa_cent: int, *,
                           jahr: int | None = None) -> tuple[int, str] | None:
    """Die Nutzungsdauer, bei der die AfA eines Jahres ``afa_cent`` ergibt --
    nur, wenn genau EINE passt (:func:`nutzungsdauer_kandidaten`), sonst ``None``.

    Raten waere hier teuer: die kuerzeste passende Dauer war oft das Restjahr
    einer zu kurzen, die Folgejahre fehlten, und die Vorjahre sperrten die
    Korrektur.

    >>> nutzungsdauer_ableiten(120000, "2026-04-15", 30000)
    (3, 'LINEAR')
    >>> nutzungsdauer_ableiten(120000, "2026-10-05", 10000)
    (3, 'LINEAR')
    >>> nutzungsdauer_ableiten(95200, "2026-03-01", 95200)
    (1, 'EIN_JAHR_IT')
    >>> nutzungsdauer_ableiten(120000, "2026-04-15", 12345) is None
    True
    >>> nutzungsdauer_ableiten(150000, "2025-05-10", 50000, jahr=2026) is None
    True
    """
    gefunden = nutzungsdauer_kandidaten(ak_brutto_cent, anschaffung_am, afa_cent, jahr=jahr)
    return gefunden[0] if len(gefunden) == 1 else None


def nutzungsdauer_aendern(conn: sqlite3.Connection, anlagegut_id: int, jahre: int, *,
                          laufend: int | None = None) -> list[AfaJahr]:
    """Setzt die Nutzungsdauer eines linear abgeschriebenen Guts neu.

    Fuer eine abgeleitete Dauer (WISO-Import), die nicht stimmt. Die Reihe
    wird aus den Stammdaten neu gerechnet -- aber nur, solange sich dabei
    kein VORJAHR aendert: was in einer abgegebenen EUeR steht, rechnet das
    Werkzeug nicht rueckwirkend um (siehe anlagegut_afa_jahr). Ausgenommen
    sind die Jahre eines Guts aus "Angaben fehlen" VOR dem ersten Jahr seiner
    WISO-AfA: sie stehen im Werkzeug nur zur Uebersicht, erklaert sind sie in
    WISO (app/logik/angaben.py). Gibt die neue Reihe zurueck.
    """
    laufend = laufend or _heute().year
    if not 1 <= jahre <= NUTZUNGSDAUER_HOECHSTENS:
        raise AnlagenFehler(f"Die Nutzungsdauer muss zwischen 1 und {NUTZUNGSDAUER_HOECHSTENS} "
                            "Jahren liegen.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        gut = _gut_lesen(conn, anlagegut_id)
        if gut["methode"] != LINEAR:
            raise AnlagenFehler(f"„{gut['bezeichnung']}“ ist sofort abgeschrieben — eine "
                                "Nutzungsdauer gibt es dafür nicht.")
        if gut["abgang_am"]:
            raise AnlagenFehler(f"„{gut['bezeichnung']}“ ist abgegangen — erst den Abgang "
                                "zurücknehmen.")
        alt = {int(z["jahr"]): int(z["afa_cent"]) for z in conn.execute(
            "SELECT jahr, afa_cent FROM anlagegut_afa_jahr WHERE anlagegut_id = ?",
            (anlagegut_id,))}
        kennzahl = _kennzahl_von(conn, anlagegut_id)
        reihe = afa_reihe(ak_brutto_cent=int(gut["ak_brutto_cent"]),
                          anschaffung_am=gut["anschaffung_am"], nutzungsdauer_jahre=jahre,
                          methode=LINEAR, kennzahl=kennzahl)
        neu = {j.jahr: j.afa_cent for j in reihe}
        uebersicht_bis = _nur_zur_uebersicht_bis(conn, anlagegut_id)
        vorjahre = sorted(j for j in set(alt) | set(neu)
                          if j < laufend and alt.get(j, 0) != neu.get(j, 0)
                          and not (uebersicht_bis is not None and j < uebersicht_bis))
        if vorjahre:
            raise AnlagenFehler(
                f"Mit {jahre} Jahren änderte sich die AfA für "
                f"{', '.join(map(str, vorjahre))} — ein zurückliegendes Jahr rechnet das Tool "
                "nicht um. Bitte die Erklärung des Jahres prüfen und die Dauer dort ändern.")
        conn.execute("UPDATE anlagegut SET nutzungsdauer_jahre = ? WHERE id = ?",
                     (jahre, anlagegut_id))
        _reihe_schreiben(conn, anlagegut_id, reihe)
        db.protokolliere(conn, "ANLAGEGUT_NUTZUNGSDAUER", "anlagegut", anlagegut_id,
                         feld="nutzungsdauer_jahre", wert_alt=gut["nutzungsdauer_jahre"],
                         wert_neu=jahre)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return reihe


def _nur_zur_uebersicht_bis(conn: sqlite3.Connection, anlagegut_id: int) -> int | None:
    """Das erste Jahr der WISO-AfA eines Guts aus "Angaben fehlen" -- davor stehen
    seine Jahre im Werkzeug nur zur Uebersicht. ``None`` fuer jedes andere Gut."""
    zeile = conn.execute("SELECT angaben_json FROM fehlende_angabe WHERE art = 'ANLAGEGUT'"
                         " AND anlagegut_id = ? AND erledigt_am IS NOT NULL",
                         (anlagegut_id,)).fetchone()
    if zeile is None:
        return None
    try:
        jahre = [int(j) for j in (json.loads(zeile["angaben_json"] or "{}").get("afa") or {})]
    except (ValueError, TypeError, AttributeError):
        return None
    return min(jahre) if jahre else None


#: So weit darf eine Einnahme "Verkauf eines Anlageguts" vom Abgang entfernt
#: liegen, um als dessen Erloes zu gelten (Zahlung vor oder nach der Uebergabe).
TAGE_ERLOES = 60


def verkaufseinnahmen(conn: sqlite3.Connection, *, betrag_cent: int | None = None,
                      am: str | None = None) -> list[sqlite3.Row]:
    """Gueltige Einnahmen "Verkauf eines Anlageguts", die noch keinem Gut zugeordnet
    sind -- wahlweise nur mit diesem Betrag und hoechstens TAGE_ERLOES vom Abgang."""
    bedingungen, werte = ["e.anlagegut_id IS NULL", "k.schluessel = 'EINNAHME_ANLAGENVERKAUF'"], []
    if betrag_cent is not None:
        bedingungen.append("e.betrag_cent = ?")
        werte.append(betrag_cent)
    if am is not None:
        bedingungen.append("ABS(julianday(e.zufluss_am) - julianday(?)) <= ?")
        werte += [am, TAGE_ERLOES]
    return conn.execute(
        "SELECT e.* FROM v_einnahme_gueltig e JOIN euer_kategorie k ON k.id = e.kategorie_id"
        f" WHERE {' AND '.join(bedingungen)} ORDER BY e.zufluss_am, e.id", werte).fetchall()


def abgang(
    conn: sqlite3.Connection, anlagegut_id: int, *, am: str, grund: str,
    erloes_cent: int = 0, erloes_einnahme_id: int | None = None, erloes_neu: bool = False,
) -> None:
    """Verkauf, Entnahme oder Verschrottung.

    Der Erloes ist Betriebseinnahme (KZ 102), der Restbuchwert eine
    Betriebsausgabe (KZ 135) -- beides liefert :func:`abgaenge` an die EUeR.

    Steht der Erloes schon als Einnahme "Verkauf eines Anlageguts" im
    Werkzeug (etwa aus WISO), ist ``erloes_einnahme_id`` diese Einnahme: sie
    wird dem Gut zugeordnet, ihr Betrag ist der Erloes, und die EUeR zaehlt
    ihn nur einmal. Ein eingetippter Erloes, zu dem es eine solche Einnahme
    gibt (gleicher Betrag, hoechstens TAGE_ERLOES Tage entfernt), wird
    abgewiesen -- er zaehlte sonst doppelt in KZ 102 --, ausser mit
    ``erloes_neu`` (ein anderer Verkauf).
    Die AfA-Reihe wird aus den Stammdaten NEU gerechnet: die Vorjahre bleiben,
    wie sie waren, das Abgangsjahr wird zeitanteilig, danach kommt nichts
    mehr. Weil die Reihe jederzeit neu zu rechnen ist, laesst sich ein Abgang
    auch zuruecknehmen (:func:`abgang_zuruecknehmen`) -- ein Tippfehler im
    Datum war vorher nicht reparierbar, die Folgejahre waren geloescht.
    """
    _gut_lesen(conn, anlagegut_id)
    # Gelesen und als ISO gespeichert. Die Oberflaeche prueft streng "nicht in
    # der Zukunft" (routen/anlagen.py); hier steht nur das Netz darunter --
    # Jahr ab 2000 und hoechstens das naechste Kalenderjahr --, damit kein
    # 31.12.2099 und kein Jahr 0026 die Reihe abschneidet.
    try:
        am = eingabe.datum(am, feld="Abgangsdatum", zukunft=True, heute=_heute())
    except ValueError as fehler:
        raise AnlagenFehler(str(fehler)) from fehler
    if not grund.strip():
        raise AnlagenFehler("Ein Abgang braucht einen Grund: verkauft, entnommen, defekt.")
    if erloes_cent < 0:
        raise AnlagenFehler("Ein Erlös kann nicht negativ sein.")
    if erloes_cent > eingabe.ID_HOECHSTENS:
        raise AnlagenFehler("Dieser Erlös ist unplausibel groß.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        einnahme = None
        if erloes_einnahme_id is not None:
            einnahme = conn.execute(
                "SELECT e.*, k.schluessel FROM v_einnahme_gueltig e"
                " JOIN euer_kategorie k ON k.id = e.kategorie_id WHERE e.id = ?",
                (erloes_einnahme_id,)).fetchone()
            if einnahme is None or einnahme["schluessel"] != "EINNAHME_ANLAGENVERKAUF":
                raise AnlagenFehler(f"Einnahme {erloes_einnahme_id} ist keine gültige Einnahme "
                                    "„Verkauf eines Anlageguts“.")
            if einnahme["anlagegut_id"] not in (None, anlagegut_id):
                raise AnlagenFehler(f"Einnahme {erloes_einnahme_id} ist schon der Erlös eines "
                                    "anderen Anlageguts.")
            if erloes_cent and erloes_cent != int(einnahme["betrag_cent"]):
                raise AnlagenFehler(
                    f"Der Erlös ({euro(erloes_cent)} €) passt nicht zu Einnahme "
                    f"{erloes_einnahme_id} ({euro(einnahme['betrag_cent'])} €) — das Feld "
                    "„Erlös“ leer lassen, dann gilt der Betrag der Einnahme.")
            erloes_cent = int(einnahme["betrag_cent"])
        elif erloes_cent and not erloes_neu:
            schon = verkaufseinnahmen(conn, betrag_cent=erloes_cent, am=am)
            if schon:
                e = schon[0]
                raise AnlagenFehler(
                    f"Der Erlös steht vermutlich schon als Einnahme {e['id']} ({e['zahler']}, "
                    f"{euro(e['betrag_cent'])} € am {_de(e['zufluss_am'])}) in der EÜR — "
                    "bitte unter „Erlös schon als Einnahme erfasst“ wählen; sonst zählte er "
                    "doppelt (KZ 102).")
        # Die Pruefung "schon abgegangen" IN der Transaktion: vier gleichzeitige
        # Klicks auf "Abgang erfassen" lasen vorher alle "noch im Bestand",
        # und das unveraenderliche Journal bekam vier Abgaenge fuer einen.
        gut = _gut_lesen(conn, anlagegut_id)
        if gut["abgang_am"]:
            raise AnlagenFehler(
                f"Dieses Anlagegut ist schon am {_de(gut['abgang_am'])} abgegangen. Erst "
                "den Abgang zurücknehmen, dann neu erfassen."
            )
        if am < gut["anschaffung_am"]:
            raise AnlagenFehler(
                f"Der Abgang am {_de(am)} liegt vor der Anschaffung am "
                f"{_de(gut['anschaffung_am'])}."
            )
        getroffen = conn.execute(
            "UPDATE anlagegut SET abgang_am = ?, abgang_grund = ?,"
            " abgang_erloes_cent = ? WHERE id = ? AND abgang_am IS NULL",
            (am, grund.strip(), erloes_cent, anlagegut_id),
        ).rowcount
        if getroffen != 1:
            raise AnlagenFehler("Dieses Anlagegut ist schon abgegangen.")
        _reihe_schreiben(conn, anlagegut_id,
                         _reihe_fuer(gut, _kennzahl_von(conn, anlagegut_id), am))
        if einnahme is not None:
            conn.execute("UPDATE einnahme SET anlagegut_id = ? WHERE id = ?",
                         (anlagegut_id, einnahme["id"]))
        db.protokolliere(conn, "ANLAGEGUT_ABGANG", "anlagegut", anlagegut_id,
                         wert_neu={"am": am, "erloes_cent": erloes_cent,
                                   "erloes_einnahme_id": einnahme["id"] if einnahme else None},
                         begruendung=grund.strip())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def abgang_zuruecknehmen(conn: sqlite3.Connection, anlagegut_id: int, *, grund: str) -> None:
    """Macht einen Abgang rueckgaengig und stellt die volle AfA-Reihe wieder her.

    Fuer den Tippfehler im Datum und den Verkauf, der doch nicht zustande kam.
    Der urspruengliche Abgang bleibt im Journal -- nachvollziehbar, nicht
    weg.
    """
    gut = _gut_lesen(conn, anlagegut_id)
    if not gut["abgang_am"]:
        raise AnlagenFehler("Dieses Anlagegut ist nicht abgegangen.")
    if not grund.strip():
        raise AnlagenFehler("Die Rücknahme braucht einen Grund.")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Wie beim Abgang: erneut lesen IN der Transaktion, damit ein
        # Doppelklick nicht zwei Ruecknahmen ins Journal schreibt.
        gut = _gut_lesen(conn, anlagegut_id)
        if not gut["abgang_am"]:
            raise AnlagenFehler("Dieses Anlagegut ist nicht abgegangen.")
        conn.execute(
            "UPDATE anlagegut SET abgang_am = NULL, abgang_grund = NULL,"
            " abgang_erloes_cent = NULL WHERE id = ?",
            (anlagegut_id,),
        )
        # Eine als Erloes zugeordnete Einnahme bleibt Einnahme -- nur ohne Gut.
        conn.execute("UPDATE einnahme SET anlagegut_id = NULL WHERE anlagegut_id = ?",
                     (anlagegut_id,))
        _reihe_schreiben(conn, anlagegut_id,
                         _reihe_fuer(gut, _kennzahl_von(conn, anlagegut_id), None))
        db.protokolliere(conn, "ANLAGEGUT_ABGANG_ZURUECK", "anlagegut", anlagegut_id,
                         wert_alt={"am": gut["abgang_am"],
                                   "erloes_cent": gut["abgang_erloes_cent"]},
                         begruendung=grund.strip())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def abgaenge(conn: sqlite3.Connection, jahr: int) -> tuple[int, int]:
    """(Erloes, Restbuchwert) der im Jahr abgegangenen Gueter -- betrieblicher Anteil.

    Der Erloes ist Betriebseinnahme, der Restbuchwert Betriebsausgabe; beide
    im Jahr des Abgangs. Ein zu 70 Prozent betrieblich genutztes Gut bringt
    70 Prozent von beidem -- die andere Haelfte der Rechnung war nie
    Betriebsvermoegen im Sinne dieser Auswertung (siehe :func:`afa_summe`).
    """
    erloes = restbuchwert = 0
    for gut in conn.execute(
        "SELECT * FROM v_anlagegut_gueltig WHERE abgang_am IS NOT NULL AND abgang_am LIKE ?",
        (f"{jahr:04d}-%",),
    ):
        quote = int(gut["aufteilungsquote"])
        # Steht der Erloes als Einnahme im Werkzeug (zugeordnet beim Abgang),
        # zaehlt die EUeR ihn dort (KZ 102, am Tag des Zuflusses) -- hier
        # nicht noch einmal.
        if conn.execute("SELECT 1 FROM v_einnahme_gueltig WHERE anlagegut_id = ?",
                        (gut["id"],)).fetchone() is None:
            erloes += _betrieblich(int(gut["abgang_erloes_cent"] or 0), quote)
        abgeschrieben = conn.execute(
            "SELECT COALESCE(SUM(afa_cent), 0) AS s FROM anlagegut_afa_jahr"
            " WHERE anlagegut_id = ?", (gut["id"],),
        ).fetchone()["s"]
        restbuchwert += _betrieblich(int(gut["ak_brutto_cent"]) - int(abgeschrieben), quote)
    return erloes, restbuchwert


def verzeichnis(conn: sqlite3.Connection, jahr: int | None = None) -> list[dict[str, object]]:
    """Das Anlagenverzeichnis, wie es in die AVEUeR gehoert.

    Ohne ``jahr`` alle Gueter, mit ``jahr`` die AfA dieses Jahres und den
    Restwert am Jahresende.
    """
    eintraege: list[dict[str, object]] = []
    for zeile in conn.execute(
        "SELECT * FROM v_anlagegut_gueltig ORDER BY anschaffung_am, id"
    ):
        afa = conn.execute(
            "SELECT * FROM anlagegut_afa_jahr WHERE anlagegut_id = ?"
            " ORDER BY jahr", (zeile["id"],)
        ).fetchall()
        dieses_jahr = next((a for a in afa if a["jahr"] == jahr), None) if jahr else None
        bis_jahr = [a for a in afa if jahr is None or a["jahr"] <= jahr]
        quote = int(zeile["aufteilungsquote"])
        abgangsjahr = int(zeile["abgang_am"][:4]) if zeile["abgang_am"] else None
        abgang_in_jahr = abgangsjahr is not None and jahr is not None and abgangsjahr == jahr
        # Ab dem Abgang ist das Gut weg: der Restwert am Jahresende ist 0 (AVEUeR:
        # Buchwert am Ende des Jahres). Der Restbuchwert BEIM Abgang -- das, was
        # als Betriebsausgabe abgeht -- steht separat in restbuchwert_abgang_cent.
        abgegangen = abgangsjahr is not None and (jahr is None or abgangsjahr <= jahr)
        eintraege.append({
            **dict(zeile),
            "afa_jahre": [dict(a) for a in afa],
            "afa_dieses_jahr_cent": dieses_jahr["afa_cent"] if dieses_jahr else 0,
            # Was davon in die EUeR geht.
            "afa_dieses_jahr_betrieblich_cent": (
                _betrieblich(int(dieses_jahr["afa_cent"]), quote) if dieses_jahr else 0),
            # Beim Abgang in diesem Jahr: der nicht abgeschriebene Rest, der als
            # Restbuchwert Betriebsausgabe wird (betrieblicher Anteil).
            "restbuchwert_abgang_cent": (
                _betrieblich(int(zeile["ak_brutto_cent"]) - sum(a["afa_cent"] for a in afa),
                             quote) if abgang_in_jahr else 0),
            "kennzahl": dieses_jahr["kennzahl"] if dieses_jahr else (
                afa[0]["kennzahl"] if afa else KZ_BEWEGLICH),
            "afa_kumuliert_cent": sum(a["afa_cent"] for a in bis_jahr),
            "restwert_cent": (
                0 if abgegangen
                else bis_jahr[-1]["restwert_cent"] if bis_jahr
                else zeile["ak_brutto_cent"]),
            "im_bestand": jahr is None or (
                int(zeile["anschaffung_am"][:4]) <= jahr
                and (not zeile["abgang_am"] or int(zeile["abgang_am"][:4]) >= jahr)
            ),
        })
    return eintraege


def afa_summe(conn: sqlite3.Connection, jahr: int) -> dict[int, int]:
    """Die AfA eines Jahres je Kennzahl -- die Zahlen fuer die EUeR.

    Nur der BETRIEBLICHE Anteil. Ein zu 70 Prozent betrieblich genutzter
    Rechner bringt 70 Prozent seiner Abschreibung als Betriebsausgabe; die
    volle AfA anzusetzen (so war es) hiesse, den privaten Anteil abzusetzen.
    Gerechnet wird je Gut und dann summiert, damit die Rundung dort
    stattfindet, wo der Betrag entsteht.
    """
    summen: dict[int, int] = {}
    for z in conn.execute(
        "SELECT j.kennzahl, j.afa_cent, g.aufteilungsquote"
        "  FROM anlagegut_afa_jahr j JOIN v_anlagegut_gueltig g ON g.id = j.anlagegut_id"
        " WHERE j.jahr = ?", (jahr,)
    ):
        kennzahl = int(z["kennzahl"])
        summen[kennzahl] = summen.get(kennzahl, 0) + _betrieblich(
            int(z["afa_cent"]), int(z["aufteilungsquote"]))
    return {k: v for k, v in summen.items() if v}


def gwg_verzeichnis(conn: sqlite3.Connection, jahr: int | None = None) -> list[sqlite3.Row]:
    """Die gesondert aufzuzeichnenden geringwertigen Wirtschaftsgueter.

    § 6 Abs. 2 Satz 4 EStG verlangt Tag der Anschaffung und
    Anschaffungskosten -- ab 250 EUR netto, und auch wenn sofort abgezogen
    wird. Genau das ist der Teil, den man vergisst, weil der Betrag ja schon
    in der EUeR steht.
    """
    satz = pauschalen.fuer(jahr or date.today().year)
    bedingung = "WHERE methode = 'GWG_SOFORT' AND ak_netto_cent > ?"
    werte: list[object] = [satz["gwg_verzeichnis_ab_netto_cent"]]
    if jahr:
        bedingung += " AND anschaffung_am LIKE ?"
        werte.append(f"{jahr}-%")
    return conn.execute(
        f"SELECT * FROM v_anlagegut_gueltig {bedingung} ORDER BY anschaffung_am", werte
    ).fetchall()


#: Kennzahlen, die eine AfA tragen. Ein Beleg in einer Kategorie mit so einer
#: Kennzahl gehoert ins Verzeichnis -- ohne Eintrag steht er mit vollem Betrag
#: als Sofortabzug in der AfA-Zeile.
AFA_KENNZAHLEN = (KZ_BEWEGLICH, KZ_IMMATERIELL)


def belege_in_anlagekategorie_ohne_gut(
    conn: sqlite3.Connection, jahr: int | None = None
) -> list[sqlite3.Row]:
    """Bezahlte Belege einer Anlagekategorie (KZ 130/131), die kein Anlagegut sind.

    Die Kategorie "Hardware & Einrichtung ueber 800 € netto" traegt die
    Kennzahl der AfA-Zeile. Ein bezahlter Beleg dort OHNE Anlagegut-Haken
    fliesst in :func:`app.logik.euer.ausgaben` mit dem vollen Bruttobetrag in
    KZ 130 -- ein Sofortabzug ueber der GWG-Grenze, den keine Pruefung
    durchgehen laesst. Das faellt nicht auf, weil die Zahl in der richtigen
    Zeile steht; sie ist nur falsch. Deshalb der Hinweis auf /euer und
    /anlagen.

    Mit ``jahr`` nur die Belege, deren Zahlung in dieses Jahr faellt.
    """
    platzhalter = ", ".join("?" * len(AFA_KENNZAHLEN))
    bedingung = "AND strftime('%Y', z.datum) = ?" if jahr else ""
    werte: list[object] = [*AFA_KENNZAHLEN]
    if jahr:
        werte.append(f"{jahr:04d}")
    return conn.execute(
        f"""
        SELECT b.id, b.belegdatum, b.betrag_brutto_cent, b.betrag_betrieblich_cent,
               b.kategorie_id, k.bezeichnung AS kategorie, k.kennzahl,
               l.name AS lieferant, MIN(z.datum) AS bezahlt_am
          FROM v_beleg_gueltig b
          JOIN euer_kategorie k ON k.id = b.kategorie_id
          JOIN zahlung z        ON z.beleg_id = b.id
          LEFT JOIN lieferant l ON l.id = b.lieferant_id
         WHERE k.kennzahl IN ({platzhalter})
           AND b.ist_anlagegut = 0
           AND NOT EXISTS (SELECT 1 FROM v_anlagegut_gueltig a WHERE a.beleg_id = b.id)
           {bedingung}
         GROUP BY b.id
         ORDER BY b.belegdatum DESC, b.id DESC
        """,
        werte,
    ).fetchall()

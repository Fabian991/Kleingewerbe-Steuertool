"""Der Lieferanten-Lernspeicher -- der staerkste Hebel der Belegauslesung.

Beim zweiten Beleg desselben Lieferanten ist bekannt, wie er seine Felder
beschriftet. Bei zwanzig wiederkehrenden Lieferanten ist die Erfassung nach
einem Jahr ein Bestaetigungsklick.

Was genau gelernt wird
----------------------
**Die Beschriftung, nicht die Position.** Gelernt wird: "bei diesem Lieferanten
steht der Gesamtbetrag auf der Zeile, die ``TOTAL AMOUNT DUE`` enthaelt" und
"das Belegdatum auf der Zeile mit ``Invoice Date``". Das ist bewusst keine
Koordinate und kein selbst gebauter regulaerer Ausdruck:

* Eine **Koordinate** bricht, sobald eine Position mehr auf dem Beleg steht
  und sich alles nach unten schiebt -- und das passiert jeden Monat.
* Ein **gelernter regulaerer Ausdruck** ist nicht nachvollziehbar. Er trifft
  oder nicht, und wenn er falsch trifft, sieht niemand warum. Eine
  Beschriftung kann man in der Pruefmaske anzeigen und der Nutzer erkennt
  sofort, ob sie stimmt.
* Die Beschriftung ist das, was der Lieferant **stabil haelt**. Sie ist in
  seinem Vorlagensystem festgeschrieben; Betraege und Positionen wechseln.

Gelernt wird aus der ohnehin noetigen Verifikation und nicht aus einem
Trainingslauf: wenn der Nutzer bestaetigt, ist der richtige Wert bekannt, und
dann laesst sich im Text nachsehen, auf welcher Zeile er stand.

Ein Layoutwechsel faellt daran auf, dass das Profil nichts mehr findet. Deshalb
werden Treffer und Fehlschlaege gezaehlt: ein Muster mit mehr Fehlschlaegen als
Treffern wird nicht mehr angewandt, sondern zum Neulernen freigegeben.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from app import db
from app.logik.beleg import felder as f
from app.logik.beleg.ergebnis import Fund, Vorschlag

#: Felder, fuer die eine Beschriftung gelernt wird. Der Lieferantenname steht
#: nicht dabei: er ist der Schluessel, unter dem gelernt wird. Die Waehrung
#: auch nicht: "EUR" steht auf fast jeder Zeile eines Belegs, gelernt wurde
#: deshalb die erste davon -- meist eine Positionszeile, die beim naechsten
#: Beleg fehlt und dann "Layout geaendert" meldete. Die Waehrung findet die
#: allgemeine Auswertung ohnehin am Waehrungszeichen.
LERNBAR = ("belegnummer", "belegdatum", "brutto", "netto", "ust", "ust_satz",
           "zahlungsdatum")

#: Unter welchem Schluessel der Buchungsdialog ein Feld uebergibt, wenn er
#: anders heisst als das Profilfeld. Der Steuersatz kam als
#: "ust_satz_promille" an und wurde deshalb nie gelernt.
SCHLUESSEL = {"ust_satz": "ust_satz_promille"}

#: Fuer Netto und Steuer gilt nur eine Zeile als Beschriftung, die ein
#: Betragswort traegt. Bei einer Rechnung mit einer Position steht der
#: Nettobetrag zuerst in der Positionszeile ("Cloud Server CX22 6,97 EUR") und
#: erst danach in "Nettobetrag 6,97 EUR"; gelernt wurde die Positionszeile, und
#: beim naechsten Beleg mit zwei Positionen stand der Preis der ersten als
#: sicherer Nettobetrag in der Maske.
BETRAGSWOERTER = {"netto": f.NETTO_WOERTER, "ust": f.UST_WOERTER,
                  "ust_satz": f.UST_WOERTER}

#: Wie die Felder in Meldungen heissen.
FELDNAMEN = {
    "belegnummer": "Belegnummer", "belegdatum": "Belegdatum", "brutto": "Brutto",
    "netto": "Netto", "ust": "Umsatzsteuer", "ust_satz": "Steuersatz",
    "waehrung": "Währung", "zahlungsdatum": "Zahlungsdatum",
}


def feldname(feld: str) -> str:
    """Der Name eines Profilfelds fuer den Nutzer.

    >>> feldname("ust_satz"), feldname("belegdatum")
    ('Steuersatz', 'Belegdatum')
    """
    return FELDNAMEN.get(feld, feld)

#: Wie die Werte der einzelnen Felder aus einer gefundenen Zeile geholt werden.
BETRAGSFELDER = ("brutto", "netto", "ust")

#: Ab wie vielen Fehlschlaegen im Verhaeltnis zu Treffern ein Muster als
#: ueberholt gilt. Ein einzelner Fehlschlag kann ein ungewoehnlicher Beleg
#: sein; haeufen sie sich, hat der Lieferant sein Layout geaendert.
VERWORFEN_AB = 2

#: Monatsnamen, um ein bestaetigtes ISO-Datum im Belegtext wiederzufinden.
MONATSNAMEN_EN = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November",
    12: "December",
}
MONATSNAMEN_DE = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April", 5: "Mai", 6: "Juni",
    7: "Juli", 8: "August", 9: "September", 10: "Oktober", 11: "November",
    12: "Dezember",
}

#: So viel Text vor dem Wert wird als Beschriftung gelernt. Lang genug, um
#: eindeutig zu sein, kurz genug, um nicht die ganze Zeile mitzunehmen.
BESCHRIFTUNG_MAX = 40


@dataclass(frozen=True, slots=True)
class Muster:
    feld: str
    beschriftung: str
    zahlenformat: str
    treffer: int
    fehlschlaege: int

    @property
    def ueberholt(self) -> bool:
        return self.fehlschlaege >= max(VERWORFEN_AB, self.treffer + 1)


def _beschriftung(zeile: str, wert_im_text: str) -> str | None:
    """Der Text, der auf der Zeile VOR dem Wert steht.

    Das ist die Beschriftung des Feldes. Steht der Wert am Zeilenanfang, gibt
    es keine -- dann ist die Zeile als Anker unbrauchbar, und es wird nichts
    gelernt. Etwas Falsches zu lernen ist schlechter als nichts zu lernen.
    """
    stelle = zeile.find(wert_im_text)
    if stelle <= 0:
        return None
    davor = zeile[:stelle].strip(" \t:.|€$")
    # Zahlen am Ende der Beschriftung wegwerfen: "Umsatzsteuer 19 %" soll als
    # "Umsatzsteuer" gelernt werden, sonst passt das Muster nicht mehr, wenn
    # derselbe Lieferant einmal 7 % abrechnet.
    davor = re.sub(r"[\d.,%\s]+$", "", davor).strip()
    if len(davor) < 3:
        return None
    return davor[-BESCHRIFTUNG_MAX:]


def lerne(
    conn: sqlite3.Connection,
    lieferant_id: int,
    text: str,
    bestaetigt: dict[str, object],
    *,
    zahlenformat: str | None = None,
) -> list[str]:
    """Lernt aus einem bestaetigten Beleg. Gibt die gelernten Felder zurueck.

    Muss in der Transaktion des Aufrufers laufen: wird der Beleg nicht
    gespeichert, soll auch nichts gelernt werden.
    """
    if zahlenformat is None:
        zahlenformat, _ = f.erkenne_format(text)
    zeilen = text.splitlines()
    gelernt: list[str] = []

    for feld in LERNBAR:
        wert = bestaetigt.get(feld)
        if wert in (None, "") and feld in SCHLUESSEL:
            wert = bestaetigt.get(SCHLUESSEL[feld])
        if wert in (None, "") or isinstance(wert, bool):
            continue
        for darstellung in _darstellungen(feld, wert, zahlenformat):
            passende = [z for z in zeilen if darstellung in z]
            if not passende:
                continue
            # Beim Gesamtbetrag die LETZTE Fundstelle: im Summenblock laeuft es
            # von oben nach unten netto, Steuer, Gesamtbetrag -- und derselbe
            # Betrag steht bei steuerfreien Belegen auf allen drei Zeilen. Mit
            # der ersten lernte man "Zwischensumme" als Beschriftung des
            # Gesamtbetrags, und beim naechsten Beleg mit Steuer waere der
            # Betrag dann um die Steuer zu niedrig.
            if feld == "brutto":
                # ... aber NUR unter den Zeilen, die ein Summenwort tragen.
                # Unter dem Summenblock steht oft noch "Bitte ueberweisen Sie
                # 119,00 EUR bis zum ..." -- dieselbe Zahl, und als letzte
                # Fundstelle lernte man den Zahlungshinweis als Beschriftung
                # des Gesamtbetrags. Beim naechsten Beleg mit anderem Text
                # fand das Muster nichts, oder Schlimmeres: etwas anderes.
                mit_wort = [z for z in passende if f._traegt_wort(z, f.BRUTTO_WOERTER)]
                zeile = (mit_wort or passende)[-1]
            elif feld in BETRAGSWOERTER:
                # Nur eine Zeile mit Betragswort. Ohne sie lieber nichts
                # lernen als die Positionszeile.
                mit_wort = [z for z in passende if f._traegt_wort(z, BETRAGSWOERTER[feld])]
                if not mit_wort:
                    continue
                zeile = mit_wort[-1] if feld == "ust" else mit_wort[0]
            else:
                zeile = passende[0]
            beschriftung = _beschriftung(zeile, darstellung)
            if beschriftung is None:
                continue
            _merke(conn, lieferant_id, feld, beschriftung, zeile, zahlenformat)
            gelernt.append(feld)
            break
    return gelernt


def _darstellungen(feld: str, wert: object, zahlenformat: str) -> list[str]:
    """Wie der bestaetigte Wert im Belegtext aussehen kann.

    Ein Betrag liegt als Cent vor und steht im Text als ``8,29`` oder ``8.29``;
    ein Datum liegt als ISO vor und steht als ``14.03.2026``. Ohne diese
    Umsetzung findet der Lernschritt den eigenen Wert nicht wieder.
    """
    if feld in BETRAGSFELDER:
        cent = int(wert)  # type: ignore[arg-type]
        ganz, rest = divmod(abs(cent), 100)
        if zahlenformat == f.Zahlenformat.EN:
            return [f"{ganz:,}.{rest:02d}".replace(",", "\u0000").replace("\u0000", ","),
                    f"{ganz}.{rest:02d}"]
        return [f"{ganz:,}".replace(",", ".") + f",{rest:02d}", f"{ganz},{rest:02d}"]
    if feld in ("belegdatum", "zahlungsdatum"):
        try:
            tag = date.fromisoformat(str(wert))
        except ValueError:
            return []
        # Alle Schreibweisen, in denen dasselbe Datum auf einem Beleg stehen
        # kann -- auch die englischen Monatsnamen. Ohne sie findet der
        # Lernschritt bei AWS, Figma oder GitHub seinen eigenen Wert nicht
        # wieder und lernt das Datumsfeld nie.
        return [
            tag.strftime("%d.%m.%Y"),
            f"{tag.day}.{tag.month}.{tag.year}",
            tag.isoformat(),
            f"{MONATSNAMEN_EN[tag.month]} {tag.day}, {tag.year}",
            f"{MONATSNAMEN_EN[tag.month]} {tag.day} {tag.year}",
            f"{tag.day} {MONATSNAMEN_EN[tag.month]} {tag.year}",
            f"{tag.day}. {MONATSNAMEN_DE[tag.month]} {tag.year}",
        ]
    if feld == "ust_satz":
        # Mit Prozentzeichen: "19" allein steht auch im Datum (19.03.2026)
        # und in jeder Artikelnummer.
        promille = int(wert)  # type: ignore[arg-type]
        satz = f"{promille / 10:g}"
        return [f"{satz} %", f"{satz}%", f"{satz.replace('.', ',')} %",
                f"{satz.replace('.', ',')}%"]
    return [str(wert)]


def _merke(
    conn: sqlite3.Connection, lieferant_id: int, feld: str,
    beschriftung: str, zeile: str, zahlenformat: str,
) -> None:
    vorhanden = conn.execute(
        "SELECT id FROM lieferant_profil WHERE lieferant_id = ? AND feld = ?"
        " AND muster = ? COLLATE NOCASE",
        (lieferant_id, feld, beschriftung),
    ).fetchone()
    if vorhanden:
        conn.execute(
            "UPDATE lieferant_profil SET treffer = treffer + 1, bestaetigt_am = ?,"
            " kontext = ?, zahlenformat = ? WHERE id = ?",
            (db.jetzt_utc(), zeile.strip(), zahlenformat, vorhanden["id"]),
        )
        return
    conn.execute(
        "INSERT INTO lieferant_profil (lieferant_id, feld, muster, kontext,"
        " zahlenformat, treffer, gelernt_am, bestaetigt_am)"
        " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (lieferant_id, feld, beschriftung, zeile.strip(), zahlenformat,
         db.jetzt_utc(), db.jetzt_utc()),
    )


def muster(conn: sqlite3.Connection, lieferant_id: int) -> list[Muster]:
    """Was ueber diesen Lieferanten bekannt ist, bestes Muster je Feld zuerst."""
    return [
        Muster(feld=z["feld"], beschriftung=z["muster"], zahlenformat=z["zahlenformat"],
               treffer=z["treffer"], fehlschlaege=z["fehlschlaege"])
        for z in conn.execute(
            "SELECT * FROM lieferant_profil WHERE lieferant_id = ?"
            " ORDER BY feld, treffer DESC, fehlschlaege",
            (lieferant_id,),
        )
    ]


def zahlenformat(conn: sqlite3.Connection, lieferant_id: int) -> str | None:
    """Das Zahlenformat, das dieser Lieferant benutzt.

    Der Grund, warum das Format am Lieferanten haengt und nicht an der
    Waehrung: AWS, Google, Figma und Adobe rechnen in Euro und schreiben
    1,234.56. Beim zweiten Beleg muss darueber nicht mehr geraten werden.
    """
    zeile = conn.execute(
        "SELECT zahlenformat, COUNT(*) AS n FROM lieferant_profil"
        " WHERE lieferant_id = ? GROUP BY zahlenformat ORDER BY n DESC LIMIT 1",
        (lieferant_id,),
    ).fetchone()
    return zeile["zahlenformat"] if zeile else None


def anwenden(
    conn: sqlite3.Connection, lieferant_id: int, text: str, *, quelle: str,
    heute: date | None = None,
) -> Vorschlag:
    """Liest den Beleg mit dem, was ueber diesen Lieferanten bekannt ist.

    Die Funde sind ``sicher``, wenn das Muster schon einmal bestaetigt wurde
    und nicht als ueberholt gilt. Ergebnis ist absichtlich unvollstaendig: es
    deckt nur die gelernten Felder. Der Aufrufer ergaenzt es ueber
    ``Vorschlag.ergaenzt_um`` mit der allgemeinen Auswertung.
    """
    format_ = zahlenformat(conn, lieferant_id) or f.erkenne_format(text)[0]
    zeilen = text.splitlines()
    funde: dict[str, Fund] = {}
    gescheitert: list[str] = []

    for eintrag in muster(conn, lieferant_id):
        feld = SCHLUESSEL.get(eintrag.feld, eintrag.feld)
        if feld in funde or eintrag.ueberholt or not _taugt(eintrag):
            continue
        wert, zeile = _hole(eintrag, zeilen, format_, heute=heute)
        if wert is None:
            gescheitert.append(eintrag.feld)
            continue
        funde[feld] = Fund(
            wert=wert, quelle=quelle,
            nachweis=f"Gelernte Beschriftung „{eintrag.beschriftung}“: {zeile}",
            sicher=eintrag.treffer > 0,
        )

    # Ein Feld zaehlt nur als gescheitert, wenn KEINES seiner Muster gegriffen
    # hat -- ein zweites, schwaecheres Muster desselben Felds ist kein Hinweis
    # auf ein neues Layout.
    gescheitert = [g for g in gescheitert if SCHLUESSEL.get(g, g) not in funde]
    meldungen: tuple[str, ...] = ()
    if gescheitert and funde:
        meldungen = (
            f"Für {', '.join(feldname(g) for g in sorted(set(gescheitert)))} hat die "
            "gelernte Beschriftung "
            "nicht gegriffen — möglicherweise hat der Lieferant sein Layout geändert.",
        )
    elif gescheitert and not funde:
        meldungen = (
            "Keine der gelernten Beschriftungen hat gegriffen — der Lieferant hat sein "
            "Layout geändert. Die Felder werden neu gelernt, sobald du sie bestätigst.",
        )
    return Vorschlag(quelle, funde, meldungen)


def _taugt(eintrag: Muster) -> bool:
    """Ob ein gespeichertes Muster angewandt wird.

    Muster aus der Zeit vor der Korrektur des Lernschritts: eine Waehrung, an
    eine Zeile gebunden, oder eine Netto-/Steuer-Beschriftung ohne Betragswort
    (eine Positionszeile). Sie lieferten einen falschen Wert als "sicher" oder
    meldeten bei unveraendertem Layout, es habe sich geaendert.
    """
    if eintrag.feld == "waehrung":
        return False
    woerter = BETRAGSWOERTER.get(eintrag.feld)
    return woerter is None or f._traegt_wort(eintrag.beschriftung, woerter)


def _hole(
    eintrag: Muster, zeilen: list[str], format_: str, *, heute: date | None = None
) -> tuple[object, str]:
    """Holt den Wert eines Feldes aus der Zeile mit der gelernten Beschriftung."""
    klein = eintrag.beschriftung.lower()
    passende = [z for z in zeilen if klein in z.lower()]
    if not passende:
        return None, ""
    # Die LETZTE passende Zeile: der Summenblock steht unten, und die gleiche
    # Beschriftung kommt oft schon in den Positionszeilen vor.
    zeile = passende[-1]

    if eintrag.feld in BETRAGSFELDER:
        betrag = f.finde_betrag(zeile, (klein,), format_)
        return (betrag.cent if betrag else None), zeile.strip()
    if eintrag.feld in ("belegdatum", "zahlungsdatum"):
        datum, _ = f.finde_datum(zeile, heute=heute, worte=(klein,))
        return datum, zeile.strip()
    if eintrag.feld == "ust_satz":
        treffer = re.search(r"(\d{1,2})(?:[.,](\d))?\s*%", zeile)
        if not treffer:
            return None, zeile.strip()
        ganz = int(treffer.group(1))
        zehntel = int(treffer.group(2) or 0)
        return ganz * 10 + zehntel, zeile.strip()
    if eintrag.feld == "waehrung":
        from app.logik.beleg.text_leser import finde_waehrung

        return finde_waehrung(zeile), zeile.strip()
    if eintrag.feld == "belegnummer":
        stelle = zeile.lower().find(klein)
        rest = zeile[stelle + len(klein):].lstrip(" :.\t#")
        treffer = re.match(r"([A-Z0-9][A-Z0-9/_.-]{3,30})", rest, re.I)
        return (treffer.group(1).rstrip(".,;") if treffer else None), zeile.strip()
    return None, zeile.strip()


def notiere_fehlschlag(conn: sqlite3.Connection, lieferant_id: int, felder: list[str]) -> None:
    """Zaehlt die Fehlschlaege der Muster, die nicht gegriffen haben.

    Getrennt vom Anwenden, weil ein Fehlschlag erst dann einer ist, wenn der
    Beleg tatsaechlich gespeichert wurde -- ein abgebrochener Import soll kein
    Muster entwerten.
    """
    for feld in set(felder):
        conn.execute(
            "UPDATE lieferant_profil SET fehlschlaege = fehlschlaege + 1"
            " WHERE lieferant_id = ? AND feld = ?",
            (lieferant_id, feld),
        )

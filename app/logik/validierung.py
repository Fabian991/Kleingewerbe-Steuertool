"""Pflichtfeld- und Plausibilitaetspruefung einer Rechnung.

Drei Stufen, und die Trennung ist der Punkt:

* **ROT** blockiert das Festschreiben. Fast alles hier kommt aus § 34a UStDV
  oder ist eine Rechnung, die in sich nicht stimmt.
* **GELB** muss einzeln abgehakt werden, und das Abhaken landet im Journal.
  Hier sitzt die Zahlendreher-Erkennung.
* **BLAU** ist reiner Hinweis.

Der Hauptschmerz -- falsches Datum, falscher Kundenname, Zahlendreher -- ist kein
Pflichtfeldproblem. Eine Pflichtfeldpruefung faengt einen Zahlendreher nie;
dafuer braucht es den Vergleich mit dem, was bisher ueblich war.

Einen Abgleich "Entwurf gegen Kundenstamm" gibt es bewusst nicht mehr (frueher
G16): ein Entwurf hat keinen Kundenschnappschuss -- der entsteht erst beim
Festschreiben --, er druckt also immer den aktuellen Stamm. Die Regel konnte
nie anschlagen und versprach einen Schutz, den es nicht gab.

Wichtig fuer die Schwellen: im Normalbetrieb darf KEINE Warnung erscheinen.
Sonst wird das Abhaken ein Klickritual und die Pruefung ist schlechter als keine.
"""

from __future__ import annotations

import calendar
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from app.logik import eingabe, grenzwert
from app.logik.rundung import euro
from app.logik.zeiten import formatiere_dauer

ROT = "ROT"
GELB = "GELB"
BLAU = "BLAU"

#: Ab dieser Abweichung vom Median der letzten Rechnungen wird gewarnt.
AUSREISSER_FAKTOR_HOCH = 3.0
AUSREISSER_FAKTOR_NIEDRIG = 0.1
#: So viele vergangene Rechnungen bilden den Vergleichsmassstab.
MEDIAN_FENSTER = 10
#: Ab so vielen Tagen in der Vergangenheit wird das Rechnungsdatum hinterfragt.
DATUM_RUECKWIRKEND_TAGE = 14
#: Physisch unplausible Arbeitsmenge je Kalendertag.
STUNDEN_JE_TAG_MAX = 16
#: Fenster fuer ein plausibles Rechnungsdatum: ab diesem Jahr bis ein Jahr
#: nach dem laufenden. Was davor liegt, ist ein Tippfehler im Jahr ("0026"
#: statt "2026" -- im Datumsfeld des Browsers als "26" tippbar) und kein
#: Rechnungsdatum. Vorher ging so ein Entwurf durch: eigener Nummernkreis
#: "26", Nummer RE-0026-0001, PDF-Ordner "26", und das Ganze unveraenderlich.
JAHR_MIN = 2000
#: So weit darf eine Zeit vor der Anlage des Kunden liegen, ohne dass G05
#: anschlaegt. Zeiten lassen sich nur fuer vorhandene Kunden erfassen -- ein
#: Datum vor ``kunde.angelegt_am`` entsteht also genau beim Nacherfassen: wer
#: aus Word umsteigt, legt den Kunden an und traegt die Arbeit der letzten
#: Wochen nach. Das ist der Normalfall jeder ersten Rechnung, kein Befund.
#: Erst ein halbes Jahr und mehr ist die Frage wert, ob Kunde oder Datum stimmt.
KUNDENANLAGE_TOLERANZ_MONATE = 6

#: Leistungsorte als Wort fuer die Befundtexte -- dieselben Begriffe wie
#: ``app.filter.STATUS_TEXTE``. Nicht importiert, aus demselben Grund wie bei
#: :func:`_datum_de`: ``app.filter`` zieht Sicherung und Excel-Export nach.
LEISTUNGSORT_TEXT = {
    "INLAND": "Inland",
    "EU_B2B_REVERSE_CHARGE": "EU-Unternehmer (Reverse Charge)",
    "DRITTLAND": "Drittland",
}


@dataclass(frozen=True, slots=True)
class Befund:
    stufe: str
    code: str
    text: str
    fundstelle: str | None = None

    @property
    def blockiert(self) -> bool:
        return self.stufe == ROT


def _median(werte: list[int]) -> float:
    if not werte:
        return 0.0
    sortiert = sorted(werte)
    mitte = len(sortiert) // 2
    if len(sortiert) % 2:
        return float(sortiert[mitte])
    return (sortiert[mitte - 1] + sortiert[mitte]) / 2


#: Wortstaemme, die die geforderte Aussage tragen. § 34a Satz 1 Nr. 5 UStDV
#: verlangt "ein Hinweis darauf, dass fuer die Lieferung oder sonstige Leistung
#: DIE STEUERBEFREIUNG FUER KLEINUNTERNEHMER gilt" -- und das laesst sich auf
#: zwei Wegen sagen: ueber die Befreiung oder ueber die Regelung. Die IHK
#: Stuttgart empfiehlt den zweiten Weg ("Kein Steuerausweis aufgrund der
#: Anwendung der Kleinunternehmerregelung"), und ein Fehlalarm darauf waere
#: genau das Klickritual, das dieses Modul vermeiden soll.
#:
#: "steuerfrei" deckt steuerfreie/Steuerfreiheit, "befrei" deckt
#: Steuerbefreiung/befreit, "kleinunternehmer" deckt auch
#: Kleinunternehmerregelung.
_BEFREIUNG = ("steuerfrei", "befrei", "kleinunternehmer")


def _nennt_befreiung(hinweis: str) -> bool:
    """Sagt der § 19-Hinweis, dass die Steuerbefreiung fuer Kleinunternehmer gilt?

    >>> _nennt_befreiung("Steuerfreie Leistung eines Kleinunternehmers gemäß § 19 UStG.")
    True
    >>> _nennt_befreiung(
    ...     "Kein Steuerausweis aufgrund der Anwendung der "
    ...     "Kleinunternehmerregelung (§ 19 UStG)."
    ... )
    True
    >>> _nennt_befreiung("Gemäß § 19 Abs. 1 UStG wird keine Umsatzsteuer ausgewiesen.")
    False
    >>> _nennt_befreiung("Umsatzsteuer wird nach § 19 UStG nicht erhoben.")
    False
    """
    klein = hinweis.lower()
    return any(stamm in klein for stamm in _BEFREIUNG)


def _sichtbar(text: object) -> bool:
    """Steht mindestens ein sichtbares Zeichen da?

    Leerzeichen, Steuer- und Formatzeichen (NUL, Escape, Breite-null-Leerzeichen)
    zaehlen nicht: ein Name aus "\\x00" galt fuer ``all()`` als vorhanden und
    druckte doch nichts aufs Papier.

    >>> _sichtbar("Muster GmbH"), _sichtbar("\\x00"), _sichtbar(" \\t\\u200b "), _sichtbar(None)
    (True, False, False, False)
    """
    return any(
        not z.isspace() and not unicodedata.category(z).startswith("C")
        for z in str(text or "")
    )


def _datum(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def jahr_max() -> int:
    """Das spaeteste Jahr, das ein Rechnungsdatum tragen darf: das naechste."""
    return date.today().year + 1


def datum_plausibel(tag: date) -> bool:
    """Liegt das Datum im Fenster JAHR_MIN bis jahr_max()?

    >>> datum_plausibel(date(2026, 3, 10)), datum_plausibel(date(26, 3, 10))
    (True, False)
    >>> datum_plausibel(date(9999, 12, 31))
    False
    """
    return JAHR_MIN <= tag.year <= jahr_max()


def datumsproblem(text: str) -> str | None:
    """Warum dieser Text kein brauchbares Rechnungsdatum ist -- oder None.

    Fuer die Formulare (Anlegen, Kopf): dieselbe Pruefung wie die ROT-Regel
    R22, nur als Satz fuer die Meldung. Ein OverflowError (Datum 9999-12-31,
    Faelligkeit dahinter gibt es nicht) ist hier schon abgefangen, weil das
    Jahr ausserhalb des Fensters liegt.

    >>> datumsproblem("2026-03-10") is None
    True
    >>> print(datumsproblem("2026-02-30"))
    „2026-02-30“ ist kein Datum.
    >>> print(datumsproblem("0026-03-10"))  # doctest: +ELLIPSIS
    „0026-03-10“ ist kein brauchbares Rechnungsdatum: das Jahr muss zwischen 2000 und ...
    """
    tag = _datum(text)
    if tag is None:
        return f"„{text[:40]}“ ist kein Datum."
    if not datum_plausibel(tag):
        return (
            f"„{text}“ ist kein brauchbares Rechnungsdatum: das Jahr muss zwischen "
            f"{JAHR_MIN} und {jahr_max()} liegen."
        )
    return None


def _datum_de(text: str | None) -> str:
    """ISO-Datum als 02.03.2026 -- fuer die Befundtexte.

    Dasselbe wie ``app.filter.datum_de``, hier noch einmal und nicht
    importiert: ``app.filter`` zieht die Sicherung und den Excel-Export nach,
    und ``festschreiben`` haengt an diesem Modul -- der Importkreis waere
    kurz. Was sich nicht lesen laesst, bleibt roh stehen, damit der Fehler
    sichtbar ist.

    >>> _datum_de("2026-03-02"), _datum_de(None), _datum_de("kaputt")
    ('02.03.2026', '—', 'kaputt')
    """
    if not text:
        return "—"
    tag = _datum(text[:10])
    return tag.strftime("%d.%m.%Y") if tag else text


def _monate_zurueck(tag: date, monate: int) -> date:
    """Der Kalendertag ``monate`` Monate vor ``tag``; Monatsenden werden gekappt.

    >>> _monate_zurueck(date(2026, 10, 18), 6), _monate_zurueck(date(2026, 3, 31), 1)
    (datetime.date(2026, 4, 18), datetime.date(2026, 2, 28))
    """
    monat, jahr = tag.month - monate, tag.year
    while monat <= 0:
        monat += 12
        jahr -= 1
    letzter = calendar.monthrange(jahr, monat)[1]
    return tag.replace(year=jahr, month=monat, day=min(tag.day, letzter))


def pruefe(conn: sqlite3.Connection, rechnung_id: int) -> list[Befund]:
    """Prueft einen Rechnungsentwurf. Reihenfolge: ROT, dann GELB, dann BLAU."""
    rechnung = conn.execute("SELECT * FROM rechnung WHERE id = ?", (rechnung_id,)).fetchone()
    if rechnung is None:
        return [Befund(ROT, "R00", "Die Rechnung existiert nicht.")]

    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    kunde = conn.execute("SELECT * FROM kunde WHERE id = ?", (rechnung["kunde_id"],)).fetchone()
    positionen = conn.execute(
        "SELECT * FROM rechnungsposition WHERE rechnung_id = ? ORDER BY pos_nr", (rechnung_id,)
    ).fetchall()
    zeiten = conn.execute(
        "SELECT * FROM zeiteintrag WHERE rechnung_id = ?", (rechnung_id,)
    ).fetchall()

    # Zwei Sichten auf denselben Kunden: "kunde" ist der aktuelle Stamm (fuer
    # Satz, Zahlungsziel und Anlagedatum), "empfaenger" ist, was auf dem
    # Papier steht. Eine Pruefung, die etwas anderes liest als der Kunde in
    # der Hand haelt, prueft das falsche Blatt. Bei einem Entwurf sind beide
    # gleich -- er hat noch keinen Schnappschuss und druckt den Stamm.
    # Lokaler Import: festschreiben importiert dieses Modul bereits (es ruft
    # pruefe() vor dem Festschreiben auf). Ein Import auf Modulebene schlioesse
    # den Kreis und machte die Ladereihenfolge zur Bedingung.
    from app.logik.festschreiben import empfaenger_daten

    empfaenger = empfaenger_daten(conn, rechnung)

    befunde: list[Befund] = []
    befunde += _pflicht(rechnung, betrieb, kunde, empfaenger, positionen)
    befunde += _summen(conn, rechnung, positionen, zeiten)
    befunde += _datumspruefung(conn, rechnung, kunde, betrieb, zeiten)
    befunde += _plausibilitaet(conn, rechnung, kunde, betrieb, positionen, zeiten)
    befunde += _bankverbindung(betrieb)
    befunde += _hinweise(conn, rechnung, kunde, positionen)

    rang = {ROT: 0, GELB: 1, BLAU: 2}
    return sorted(befunde, key=lambda b: (rang[b.stufe], b.code))


# --------------------------------------------------------------- ROT: Pflicht

def _pflicht(rechnung, betrieb, kunde, empfaenger, positionen) -> list[Befund]:
    befunde: list[Befund] = []
    p = "§ 34a UStDV"

    if betrieb is None:
        return [Befund(ROT, "R01", "Die Betriebsdaten fehlen. Bitte zuerst einrichten.", p)]

    if not all(_sichtbar(betrieb[feld]) for feld in ("name", "strasse", "plz", "ort")):
        befunde.append(Befund(
            ROT, "R01",
            "Name oder Anschrift des Betriebs ist unvollständig.",
            f"{p} Satz 1 Nr. 1",
        ))
    if not any((betrieb["steuernummer"], betrieb["ust_idnr"],
                betrieb["kleinunternehmer_idnr"])):
        befunde.append(Befund(
            ROT, "R02",
            "Es fehlt die Steuernummer. Mindestens eine von drei Kennungen ist Pflicht: "
            "Steuernummer, Umsatzsteuer-Identifikationsnummer oder "
            "Kleinunternehmer-Identifikationsnummer.",
            f"{p} Satz 1 Nr. 2",
        ))
    elif (
        rechnung["typ"] in ("RECHNUNG", "STORNO")
        and rechnung["leistungsort"] == "EU_B2B_REVERSE_CHARGE"
        and not any(_sichtbar(betrieb[feld]) for feld in ("steuernummer", "ust_idnr"))
    ):
        # Eine Reverse-Charge-Rechnung ist keine Rechnung ueber einen nach § 19
        # befreiten Umsatz -- die Erleichterung des § 34a UStDV greift nicht.
        # § 14a Abs. 1 verweist auf § 14 Abs. 4, und dessen Satz 1 Nr. 2 kennt
        # nur Steuernummer ODER USt-IdNr.; die Kleinunternehmer-IdNr. genuegt
        # dort nicht. Gedruckt wuerde sie trotzdem (rechnungsdruck), also hier.
        befunde.append(Befund(
            ROT, "R02",
            "Für eine Reverse-Charge-Rechnung genügt die Kleinunternehmer-IdNr. nicht: "
            "nötig ist die Steuernummer oder die Umsatzsteuer-Identifikationsnummer. "
            "Bitte unter „Einstellungen“ eintragen.",
            "§ 14a Abs. 1, § 14 Abs. 4 Satz 1 Nr. 2 UStG",
        ))
    # Gedruckt wird "rechnungsname or name" (rechnung_pdf.html). Geprueft werden
    # muss dasselbe: ein Kunde mit leerem Namen, aber gesetztem Rechnungsnamen,
    # traegt eine vollstaendige Anschrift auf dem Papier -- und umgekehrt faellt
    # ein leerer Rechnungsname nicht auf, wenn nur "name" geprueft wird.
    # Sichtbar muss es sein: ein Name aus einem einzelnen NUL (Altdaten aus der
    # Zeit, bevor main.py Steuerzeichen abwies) ist auf dem Papier leer.
    anschrift = (
        (empfaenger.get("rechnungsname") or empfaenger.get("name")),
        empfaenger.get("strasse"), empfaenger.get("plz"), empfaenger.get("ort"),
    )
    if kunde is None or not all(_sichtbar(teil) for teil in anschrift):
        befunde.append(Befund(
            ROT, "R03",
            "Name oder Anschrift des Kunden ist unvollständig.",
            f"{p} Satz 1 Nr. 1",
        ))
    datum = _datum(rechnung["datum"])
    if datum is None:
        befunde.append(Befund(ROT, "R04", "Das Rechnungsdatum fehlt oder ist ungültig.",
                              f"{p} Satz 1 Nr. 3"))
    elif not datum_plausibel(datum):
        # Das Jahr ist ein Tippfehler. GELB reichte nicht: G02 ("liegt 730.000
        # Tage zurueck") laesst sich abhaken, und danach ist die Rechnung mit
        # Nummer RE-0026-0001 festgeschrieben und nur per Storno zu loesen.
        befunde.append(Befund(
            ROT, "R22",
            # Nicht ueber _datum_de: strftime("%Y") schreibt das Jahr 26 als
            # "26", und genau die fehlende Stelle soll der Nutzer sehen.
            f"Das Rechnungsdatum {datum.day:02d}.{datum.month:02d}.{datum.year:04d} "
            "liegt außerhalb des "
            f"plausiblen Zeitraums ({JAHR_MIN} bis {jahr_max()}) — vermutlich ein "
            "Tippfehler im Jahr. Bitte unter „Kopf und Texte“ berichtigen.",
            f"{p} Satz 1 Nr. 3",
        ))

    # Der Leistungszeitraum ist fuer einen Kleinunternehmer KEINE Pflichtangabe:
    # § 34a Satz 1 UStDV listet sechs Angaben, und der Zeitpunkt der Leistung ist
    # nicht darunter. Bei Regelbesteuerung dagegen verlangt § 14 Abs. 4 Nr. 6 UStG
    # ihn -- deshalb haengt die Regel am Steuerregime und nicht an einer
    # globalen Einstellung.
    if rechnung["steuerregime"] == "REGEL" and rechnung["typ"] in ("RECHNUNG", "STORNO"):
        if _datum(rechnung["leistung_von"]) is None:
            befunde.append(Befund(
                ROT, "R14",
                "Bei Regelbesteuerung ist der Zeitpunkt der Leistung Pflichtangabe. "
                "Der Leistungszeitraum fehlt.",
                "§ 14 Abs. 4 Nr. 6 UStG",
            ))
        elif not rechnung["leistungszeitraum_zeigen"]:
            befunde.append(Befund(
                ROT, "R15",
                "Bei Regelbesteuerung muss der Leistungszeitraum auf der Rechnung stehen. "
                "Er ist derzeit ausgeblendet.",
                "§ 14 Abs. 4 Nr. 6 UStG",
            ))
    if not positionen:
        befunde.append(Befund(ROT, "R05", "Die Rechnung hat keine Position.",
                              f"{p} Satz 1 Nr. 4"))
    for position in positionen:
        if not (position["bezeichnung"] or "").strip():
            befunde.append(Befund(
                ROT, f"R05-{position['pos_nr']}",
                f"Position {position['pos_nr']} hat keine Bezeichnung.",
                f"{p} Satz 1 Nr. 4"))
        if position["menge_tausendstel"] <= 0:
            befunde.append(Befund(
                ROT, f"R11-{position['pos_nr']}",
                f"Position {position['pos_nr']} hat keine Menge. Das passiert, wenn die "
                "Rundung eine Position auf null bringt.",
                f"{p} Satz 1 Nr. 4",
            ))

    hinweis = (rechnung["hinweis_19_text"] or betrieb["hinweis_19_text"] or "").strip()
    ort = rechnung["leistungsort"]

    # Land und Leistungsort muessen zusammenpassen. Die Vorbelegung des
    # Leistungsorts ist INLAND -- wer einen Kunden mit auslaendischer Anschrift
    # anlegt und das Feld uebersieht, bekommt eine § 19-Rechnung an einen
    # Oesterreicher, und zwar ohne jede Warnung. Das ist der eine Fehler, den
    # dieses Werkzeug am sichersten erkennen kann: die Anschrift steht daneben.
    land = (empfaenger.get("land") or "DE").upper()
    privat = empfaenger.get("typ") == "PRIVAT"
    if rechnung["typ"] in ("RECHNUNG", "STORNO"):
        if land != "DE" and ort == "INLAND" and privat:
            # Bei einer Privatperson ist Inland der REGELFALL: § 3a Abs. 1 UStG
            # legt den Leistungsort an den Sitz des Leistenden, § 3a Abs. 2
            # greift nur fuer Unternehmer. Deshalb kein Blocker, sondern der
            # Hinweis auf die beiden Ausnahmen, die der Nutzer selbst kennen
            # muss.
            befunde.append(Befund(
                GELB, "G24",
                f"Die Anschrift liegt in {land}, der Leistungsort steht auf Inland. Bei "
                "einer Privatperson ist das in der Regel richtig: der Leistungsort liegt "
                "nach § 3a Abs. 1 UStG am Sitz des Leistenden. Ausnahmen prüfen: § 3a "
                "Abs. 4 UStG (Katalogleistungen an Privatpersonen im Drittland) und § 3a "
                "Abs. 5 UStG (elektronische Leistungen in der EU über 10.000 €).",
                "§ 3a Abs. 1 UStG",
            ))
        elif land != "DE" and ort == "INLAND":
            befunde.append(Befund(
                ROT, "R20",
                f"Die Anschrift liegt in {land}, der Leistungsort steht aber auf Inland. "
                "Bei einem Unternehmer im Ausland liegt der Leistungsort nach § 3a Abs. 2 "
                "UStG an dessen Sitz — die Rechnung trüge dann zu Unrecht den § 19-Hinweis "
                "und bei einem EU-Kunden fehlte die Steuerschuldnerschaft des "
                "Leistungsempfängers. Bitte den Leistungsort am Kunden berichtigen.",
                "§ 3a Abs. 2 UStG",
            ))
        elif land == "DE" and ort != "INLAND":
            befunde.append(Befund(
                GELB, "G23",
                f"Die Anschrift liegt in Deutschland, der Leistungsort steht aber auf "
                f"{LEISTUNGSORT_TEXT.get(ort, ort)}. Das kommt vor — etwa bei einer "
                "Betriebsstätte im Ausland —, ist aber die Ausnahme und sollte stimmen.",
                "§ 3a Abs. 2 UStG",
            ))
    if rechnung["typ"] in ("RECHNUNG", "STORNO") and ort == "EU_B2B_REVERSE_CHARGE":
        if privat:
            # Die Steuerschuldnerschaft des Leistungsempfaengers setzt einen
            # Unternehmer als Empfaenger voraus. Eine Privatperson kann sie
            # nicht uebernehmen -- die Rechnung behauptete einen Mechanismus,
            # der fuer diesen Empfaenger nicht existiert.
            befunde.append(Befund(
                ROT, "R21",
                "Reverse Charge setzt einen Unternehmer als Empfänger voraus (§ 13b UStG, "
                "Art. 196 MwStSystRL); der Kunde ist als Privatperson angelegt. Bitte den "
                "Leistungsort am Kunden berichtigen.",
                "§ 13b UStG, Art. 196 MwStSystRL",
            ))
        befunde.extend(_eu_b2b(rechnung, betrieb, empfaenger, hinweis))
    elif rechnung["typ"] in ("RECHNUNG", "STORNO") and ort == "DRITTLAND":
        if "§ 19" in hinweis:
            befunde.append(Befund(
                ROT, "R19",
                "Die Rechnung trägt den § 19-Hinweis, der Leistungsort liegt aber im "
                "Drittland. § 19 Abs. 1 UStG macht einen steuerbaren Umsatz steuerfrei — "
                "hier ist der Umsatz im Inland gar nicht steuerbar. Der Hinweis behauptet "
                "also eine Steuerbefreiung, die es nicht gibt.",
                "§ 3a Abs. 2, § 19 Abs. 1 UStG",
            ))
    elif rechnung["typ"] in ("RECHNUNG", "STORNO"):
        if "§ 19" not in hinweis:
            befunde.append(Befund(
                ROT, "R07",
                "Der Hinweis auf die Steuerbefreiung nach § 19 UStG fehlt.",
                f"{p} Satz 1 Nr. 5",
            ))
        elif not _nennt_befreiung(hinweis):
            # Geprueft wird nicht, welche Formulierung DRINSTEHT, sondern ob die
            # geforderte Aussage drinsteht. § 34a Satz 1 Nr. 5 UStDV verlangt den
            # Hinweis, dass die STEUERBEFREIUNG GILT. Die beiden verbreiteten
            # Altformeln sagen etwas anderes: "wird nicht erhoben" beschreibt die
            # Rechtslage bis 2024, "wird keine Umsatzsteuer ausgewiesen"
            # beschreibt, was auf dem Papier fehlt -- eine Aussage ueber das
            # Dokument, nicht ueber den Umsatz. Eine Liste verbotener Woerter
            # wuerde die naechste Variante wieder durchlassen.
            befunde.append(Befund(
                GELB, "G18",
                "Der § 19-Hinweis nennt weder die Steuerbefreiung noch die "
                "Kleinunternehmerregelung. Seit 2025 ist § 19 Abs. 1 UStG eine "
                "Steuerbefreiung; Formulierungen wie „wird nicht erhoben“ (Rechtslage "
                "bis 2024) oder „wird keine Umsatzsteuer ausgewiesen“ (sagt nur, was "
                "auf der Rechnung fehlt) treffen sie nicht. Ein Wortlaut ist nicht "
                "vorgeschrieben — die Befreiung muss nur erkennbar sein. Es genügt "
                "z. B. „Steuerfreie Leistung eines Kleinunternehmers gemäß § 19 Abs. 1 "
                "UStG“ oder „Kein Steuerausweis aufgrund der Anwendung der "
                "Kleinunternehmerregelung (§ 19 UStG)“.",
                f"{p} Satz 1 Nr. 5",
            ))
    elif hinweis:
        befunde.append(Befund(
            ROT, "R12",
            "Ein Leistungsnachweis darf keinen § 19-Hinweis tragen: der Hinweis bezeichnet "
            "die Steuerfreiheit eines Umsatzes, und hier wird kein Umsatz abgerechnet.",
            "§ 19 Abs. 1 UStG",
        ))

    if rechnung["steuer_cent"]:
        befunde.append(Befund(
            ROT, "R08",
            "Die Rechnung weist Umsatzsteuer aus. Ein Kleinunternehmer schuldet "
            "ausgewiesene Umsatzsteuer, auch wenn er sie nicht hätte ausweisen dürfen.",
            "§ 14c Abs. 1 UStG",
        ))
    return befunde


#: USt-IdNr.-Praefixe, die nicht dem Laenderkuerzel der Anschrift gleichen.
_PRAEFIX_LAND = {"EL": "GR", "XI": "GB"}


def _ust_idnr_und_land(empfaenger) -> list[Befund]:
    """Passt die USt-IdNr. des Kunden zu Reverse Charge und zu seiner Anschrift?

    * **Eine deutsche USt-IdNr. (DE…)** heisst in aller Regel: der Kunde
      bezieht die Leistung fuer eine Niederlassung in Deutschland. Dann liegt
      der Leistungsort nach § 3a Abs. 2 UStG im Inland, die Steuerschuld geht
      nicht ueber (Art. 196 MwStSystRL setzt einen Leistenden voraus, der im
      Staat des Empfaengers nicht ansaessig ist), und die Rechnung ist eine
      § 19-Rechnung; der Umsatz zaehlte sonst auch nicht gegen die
      § 19-Grenze. GELB und nicht ROT: ein auslaendisches Unternehmen kann
      in Deutschland registriert sein, ohne hier eine Niederlassung zu haben
      -- dieser seltene Fall darf das Festschreiben nicht sperren.
    * **Ein anderer EU-Staat als die Anschrift** kommt vor -- eine
      Niederlassung in einem dritten Mitgliedstaat --, ist aber oft ein
      Tippfehler im Land oder in der Nummer. GELB. Griechenland fuehrt EL,
      Nordirland XI. Nur fuer ein bekanntes EU-Praefix: aus "ungültig 123"
      wurde sonst "stammt aus UN" -- was mit der Nummer nicht stimmt, sagt G28.

    >>> [b.code for b in _ust_idnr_und_land({"ust_idnr": "ATU12345678", "land": "AT"})]
    []
    >>> [b.code for b in _ust_idnr_und_land({"ust_idnr": "EL123456789", "land": "GR"})]
    []
    >>> [b.code for b in _ust_idnr_und_land({"ust_idnr": "DE 123 456 789", "land": "AT"})]
    ['G27']
    >>> [b.code for b in _ust_idnr_und_land({"ust_idnr": "ATU12345678", "land": "FR"})]
    ['G26']
    >>> [b.code for b in _ust_idnr_und_land({"ust_idnr": None, "land": "FR"})]
    []
    >>> [b.code for b in _ust_idnr_und_land({"ust_idnr": "ungültig 123", "land": "AT"})]
    []
    """
    from app.filter import land_name
    from app.logik.kunden import ist_eu_praefix

    nummer = re.sub(r"[\s.\-]", "", empfaenger.get("ust_idnr") or "").upper()
    if len(nummer) < 2:
        return []
    praefix = nummer[:2]
    if praefix == "DE":
        return [Befund(
            GELB, "G27",
            f"Die USt-IdNr. des Kunden ({nummer}) ist eine deutsche. Mit ihr bezieht "
            "der Kunde die Leistung in aller Regel für eine Niederlassung in "
            "Deutschland: der Leistungsort liegt dann nach § 3a Abs. 2 UStG im "
            "Inland, die Steuerschuld geht nicht auf ihn über, und das ist eine "
            "§ 19-Rechnung. Bitte die USt-IdNr. aus dem Sitzstaat des Kunden "
            "eintragen oder den Leistungsort am Kunden auf Inland stellen.",
            "§ 3a Abs. 2 UStG, Art. 196 MwStSystRL",
        )]
    land = (empfaenger.get("land") or "DE").strip().upper()
    if land == "DE" or not ist_eu_praefix(praefix) or _PRAEFIX_LAND.get(praefix, praefix) == land:
        return []
    return [Befund(
        GELB, "G26",
        f"Die USt-IdNr. ({nummer}) stammt aus "
        f"{land_name(_PRAEFIX_LAND.get(praefix, praefix))}, die Anschrift liegt in "
        f"{land_name(land)}. Das kommt vor, wenn der Kunde die Leistung für eine "
        "Niederlassung in einem anderen Mitgliedstaat bezieht; oft ist es aber ein "
        "Tippfehler im Land oder in der Nummer. Bitte prüfen.",
        "§ 3a Abs. 2 UStG",
    )]


def _ust_idnr_form(empfaenger) -> list[Befund]:
    """G28: die gedruckte USt-IdNr. hat nicht die Form einer EU-USt-IdNr.

    Das Kundenformular prueft die Form bei Reverse Charge seit der
    Gegenprobe. Eine Datenbank aus der Zeit davor kann aber noch
    "ATU1234567" (eine Stelle zu kurz) oder Freitext tragen -- und R18
    prueft nur "nicht leer". Geprueft wird, was gedruckt wird: der
    Schnappschuss bzw. beim Entwurf der Stamm. GELB und nicht ROT, weil das
    Werkzeug nur die Form kennt, nicht das Register; ob die Nummer vergeben
    ist, sagt erst die Bestaetigungsabfrage beim Bundeszentralamt.

    >>> [b.code for b in _ust_idnr_form({"ust_idnr": "ATU12345678"})]
    []
    >>> print(_ust_idnr_form({"ust_idnr": "ATU1234567"})[0].text)  # doctest: +ELLIPSIS
    Die USt-IdNr. des Kunden (ATU1234567) hat nicht die Form einer USt-IdNr. aus Österreich \
(ATU und 8 Ziffern, z. B. ATU12345678). ...
    >>> print(_ust_idnr_form({"ust_idnr": "ungültig 123"})[0].text)  # doctest: +ELLIPSIS
    Die USt-IdNr. des Kunden (UNGÜLTIG123) beginnt nicht mit dem Kürzel eines EU-Staats. ...
    >>> _ust_idnr_form({"ust_idnr": None}), _ust_idnr_form({"ust_idnr": " "})
    ([], [])
    """
    from app.logik.kunden import ust_idnr_formfehler

    roh = empfaenger.get("ust_idnr") or ""
    problem = ust_idnr_formfehler(roh)
    if problem is None:
        return []
    nummer = re.sub(r"[\s.\-]", "", roh).upper()
    return [Befund(
        GELB, "G28",
        f"Die USt-IdNr. des Kunden ({nummer}) {problem}. Bei Reverse Charge ist sie der "
        "Nachweis, dass der Empfänger Unternehmer ist, und steht so auf der Rechnung. "
        "Bitte die Nummer am Kunden berichtigen — das Kundenformular prüft die Form "
        "beim Speichern.",
        "§ 3a Abs. 2, § 14a Abs. 1 UStG",
    )]


def _eu_b2b(rechnung, betrieb, empfaenger, hinweis: str) -> list[Befund]:
    """Pruefungen fuer eine Leistung an einen Unternehmer im EU-Ausland.

    Die rechtliche Lage, auf die diese Regeln zurueckgehen:

    * **Der § 19-Hinweis ist hier falsch.** § 19 Abs. 1 UStG macht einen
      *steuerbaren* Umsatz steuerfrei. Eine sonstige Leistung an einen
      Unternehmer im uebrigen Gemeinschaftsgebiet ist nach § 3a Abs. 2 UStG am
      Sitz des Empfaengers zu besteuern und in Deutschland **nicht steuerbar**.
      Der Hinweis behauptete also eine Steuerbefreiung, die es nicht gibt.
    * **Pflicht ist stattdessen "Steuerschuldnerschaft des
      Leistungsempfaengers"** (§ 14a Abs. 1 Satz 1 UStG). § 19 Abs. 1 Satz 2
      nimmt von § 14a nur die USt-IdNr.-Pflicht (Satz 3) aus -- den Hinweis
      selbst nicht. Er bleibt also auch fuer einen Kleinunternehmer Pflicht.
    * **Die USt-IdNr. des Kunden ist der Nachweis**, dass der Empfaenger
      Unternehmer ist. Fehlt sie, greift § 3a Abs. 2 nicht, der Leistungsort
      liegt nach § 3a Abs. 1 im Inland, und dann ist es eine ganz normale
      § 19-Rechnung. Das ist deshalb ROT und keine Formalie: ohne sie ist die
      ganze Einordnung falsch.
    * **Die eigene USt-IdNr. ist dagegen NICHT Pflicht** -- § 19 Abs. 1 Satz 2
      nimmt § 14a Abs. 1 Satz 3 ausdruecklich aus. Praktisch verlangt der
      Kunde sie oft trotzdem, deshalb GELB.
    * **Der Zeitpunkt der Leistung gehoert drauf** (G25). § 14a Abs. 1 Satz 1
      verlangt eine Rechnung "mit den Angaben nach § 14 Absatz 4", also auch
      Nr. 6. Die Erleichterung des § 34a UStDV gilt der Rechnung ueber einen
      nach § 19 befreiten Umsatz -- dieser Umsatz ist im Inland gar nicht
      steuerbar. Weil die Reichweite des § 34a hier nicht abschliessend
      geklaert ist, GELB und nicht ROT.

    Was hier bewusst NICHT geprueft wird, und warum:

    * **Keine Zusammenfassende Meldung.** § 18a Abs. 4 UStG sagt woertlich:
      "Die Absaetze 1 bis 3 gelten nicht fuer Unternehmer, die § 19 Absatz 1
      anwenden." Solange der Kleinunternehmerstatus gilt, ist also nichts zu
      melden. ⚠ Faellt er weg -- Vorjahr ueber 25.000 € oder laufendes Jahr
      ueber 100.000 € --, wird die ZM vierteljaehrlich faellig, und dann muss
      diese Stelle nachgezogen werden. Der Grenzwertmonitor auf dem Dashboard
      ist die Vorwarnung dafuer.
    * **Keine Pruefung der USt-IdNr. des Kunden gegen das VIES-Register.** Eine
      qualifizierte Bestaetigungsabfrage ist der belastbare Nachweis, braucht
      aber einen Netzzugriff -- und das Werkzeug laeuft bewusst offline. Die
      Abfrage macht der Nutzer einmal je Kunde beim Bundeszentralamt fuer
      Steuern; das Datum gehoert in das Notizfeld des Kunden.
    """
    befunde: list[Befund] = []
    if "§ 19" in hinweis:
        befunde.append(Befund(
            ROT, "R16",
            "Die Rechnung trägt den § 19-Hinweis, die Leistung ist aber in Deutschland "
            "nicht steuerbar (§ 3a Abs. 2 UStG — Leistungsort beim Empfänger). § 19 "
            "Abs. 1 macht einen steuerbaren Umsatz steuerfrei; hier gibt es keinen "
            "steuerbaren Umsatz, den man befreien könnte.",
            "§ 3a Abs. 2, § 19 Abs. 1 UStG",
        ))
    if "steuerschuldnerschaft des leistungsempfängers" not in hinweis.lower():
        befunde.append(Befund(
            ROT, "R17",
            "Es fehlt die Angabe „Steuerschuldnerschaft des Leistungsempfängers“. Sie ist "
            "bei einer Leistung an einen Unternehmer im EU-Ausland Pflicht — auch für "
            "Kleinunternehmer: § 19 Abs. 1 Satz 2 UStG nimmt von § 14a nur die "
            "USt-IdNr.-Pflicht aus, nicht diesen Hinweis.",
            "§ 14a Abs. 1 Satz 1 UStG",
        ))
    # Der Schnappschuss, nicht der Stamm: gedruckt wird die USt-IdNr. aus dem
    # Snapshot (rechnung_pdf.html), und eine Pruefung, die eine andere Quelle
    # liest, prueft ein anderes Blatt als das beim Kunden.
    if not (empfaenger.get("ust_idnr") or "").strip():
        befunde.append(Befund(
            ROT, "R18",
            "Dem Kunden fehlt die USt-IdNr. Sie ist der Nachweis, dass der Empfänger "
            "Unternehmer ist. Ohne ihn greift § 3a Abs. 2 UStG nicht, der Leistungsort "
            "liegt nach § 3a Abs. 1 im Inland — und dann wäre das eine normale "
            "§ 19-Rechnung mit deutschem Steuerhinweis, keine Reverse-Charge-Rechnung.",
            "§ 3a Abs. 2 UStG",
        ))
    befunde.extend(_ust_idnr_form(empfaenger))
    befunde.extend(_ust_idnr_und_land(empfaenger))
    if not rechnung["leistungszeitraum_zeigen"] or _datum(rechnung["leistung_von"]) is None:
        befunde.append(Befund(
            GELB, "G25",
            "Reverse-Charge-Rechnung ohne Leistungszeitpunkt: § 14a Abs. 1 Satz 1 UStG "
            "verweist auf die Angaben nach § 14 Abs. 4 (Nr. 6 Zeitpunkt der Leistung); "
            "§ 34a UStDV erleichtert nur die § 19-Rechnung. Bitte unter „Kopf und Texte“ "
            "den Leistungszeitraum ausweisen.",
            "§ 14a Abs. 1 Satz 1, § 14 Abs. 4 Satz 1 Nr. 6 UStG",
        ))
    if not (betrieb["ust_idnr"] or "").strip():
        befunde.append(Befund(
            GELB, "G22",
            "Du hast keine eigene USt-IdNr. Rechtlich ist das in Ordnung: § 19 Abs. 1 "
            "Satz 2 UStG nimmt § 14a Abs. 1 Satz 3 ausdrücklich aus, die Angabe ist für "
            "dich also keine Pflicht. In der Praxis verlangt die Buchhaltung des Kunden "
            "sie trotzdem oft. Eine USt-IdNr. gibt es kostenlos beim Bundeszentralamt "
            "für Steuern (§ 27a UStG) und sie kostet den Kleinunternehmerstatus nicht.",
            "§ 19 Abs. 1 Satz 2, § 27a UStG",
        ))
    return befunde


# ------------------------------------------------------- ROT: Summen und Sperren

def _summen(conn, rechnung, positionen, zeiten) -> list[Befund]:
    befunde: list[Befund] = []
    # Positionen liegen immer positiv in der Datenbank; ein Storno traegt das
    # umgekehrte Vorzeichen am Kopf.
    vorzeichen = -1 if rechnung["typ"] == "STORNO" else 1
    summe = vorzeichen * sum(p["betrag_cent"] for p in positionen)

    if rechnung["netto_cent"] != summe or rechnung["brutto_cent"] != summe:
        befunde.append(Befund(
            ROT, "R09",
            f"Kopfsumme und Positionen laufen auseinander: Kopf "
            f"{euro(rechnung['brutto_cent'])} €, Positionen {euro(summe)} €.",
        ))

    if rechnung["typ"] == "RECHNUNG" and rechnung["brutto_cent"] <= 0:
        befunde.append(Befund(
            ROT, "R06",
            # Nur die Wege, die das Werkzeug auch anbietet: einen
            # Leistungsnachweis kennt das Schema, aber keine Seite erzeugt ihn.
            "Rechnung über 0,00 €: ohne Entgelt ist das keine Rechnung. Die Kulanzstunden "
            "an eine berechnete Rechnung hängen oder die Stunden abschreiben.",
            "§ 1 Abs. 1 Nr. 1, § 14 Abs. 1 UStG",
        ))
    if rechnung["typ"] == "LEISTUNGSNACHWEIS" and rechnung["brutto_cent"] != 0:
        befunde.append(Befund(
            ROT, "R13", "Ein Leistungsnachweis muss über 0,00 € lauten."))

    # Jeder zugeordnete Zeiteintrag muss noch frei sein. Die Anwendung prueft das
    # hier fuer die Anzeige; verbindlich prueft es die Festschreib-Transaktion
    # unmittelbar vor dem Commit noch einmal.
    for zeit in zeiten:
        fremd = conn.execute(
            """
            SELECT r.nummer
              FROM position_zeiteintrag pz
              JOIN rechnungsposition p ON p.id = pz.position_id
              JOIN rechnung r          ON r.id = p.rechnung_id
             WHERE pz.zeiteintrag_id = ? AND r.status <> 'STORNIERT' AND r.id <> ?
             LIMIT 1
            """,
            (zeit["id"], rechnung["id"]),
        ).fetchone()
        if fremd:
            befunde.append(Befund(
                ROT, f"R10-{zeit['id']}",
                f"Die Zeit vom {_datum_de(zeit['datum'])} liegt bereits auf Rechnung "
                f"{fremd['nummer']}.",
            ))
    return befunde


# ------------------------------------------------------------- GELB: Datumsfragen

def _datumspruefung(conn, rechnung, kunde, betrieb, zeiten) -> list[Befund]:
    befunde: list[Befund] = []
    datum = _datum(rechnung["datum"])
    if datum is None:
        return befunde
    heute = date.today()

    if datum > heute:
        befunde.append(Befund(
            GELB, "G01",
            f"Das Rechnungsdatum liegt in der Zukunft ({_datum_de(rechnung['datum'])}).",
        ))
    elif datum < heute - timedelta(days=DATUM_RUECKWIRKEND_TAGE):
        befunde.append(Befund(
            GELB, "G02",
            f"Das Rechnungsdatum liegt {(heute - datum).days} Tage zurück."))

    letzte = conn.execute(
        """
        SELECT nummer, datum FROM rechnung
         WHERE status = 'FESTGESCHRIEBEN' AND typ = 'RECHNUNG' AND id <> ?
         ORDER BY nummer_jahr DESC, nummer_seq DESC LIMIT 1
        """,
        (rechnung["id"],),
    ).fetchone()
    if letzte and _datum(letzte["datum"]) and datum < _datum(letzte["datum"]):
        befunde.append(Befund(
            GELB, "G03",
            f"Die vorige Rechnung {letzte['nummer']} ist auf {_datum_de(letzte['datum'])} "
            f"datiert, diese auf {_datum_de(rechnung['datum'])}. Nummer und Datum laufen "
            "gegeneinander — das ist in einer Prüfung erklärungsbedürftig.",
        ))

    bis = _datum(rechnung["leistung_bis"])
    von = _datum(rechnung["leistung_von"])
    if bis and bis > datum:
        befunde.append(Befund(GELB, "G04",
                              "Der Leistungszeitraum endet nach dem Rechnungsdatum."))
    if von:
        # G05 erst ab einem halben Jahr Abstand -- siehe
        # KUNDENANLAGE_TOLERANZ_MONATE: das Nacherfassen der letzten Wochen am
        # Tag der Kundenanlage ist beim Umstieg der Normalfall, und ein Befund,
        # der auf jeder ersten Rechnung erscheint, wird nur noch weggeklickt.
        angelegt = _datum(kunde["angelegt_am"][:10]) if kunde else None
        if angelegt and von < _monate_zurueck(angelegt, KUNDENANLAGE_TOLERANZ_MONATE):
            befunde.append(Befund(
                GELB, "G05",
                f"Der Leistungszeitraum beginnt am {_datum_de(rechnung['leistung_von'])}, "
                f"der Kunde ist erst seit {_datum_de(kunde['angelegt_am'])} angelegt — mehr "
                f"als {KUNDENANLAGE_TOLERANZ_MONATE} Monate später. Stimmen Kunde und Datum?",
            ))
        beginn = _datum(betrieb["beginn_am"]) if betrieb else None
        if beginn and von < beginn:
            befunde.append(Befund(
                GELB, "G21",
                "Der Leistungszeitraum beginnt vor dem Betriebsbeginn."))

    jahre = {z["datum"][:4] for z in zeiten}
    if len(jahre) > 1:
        befunde.append(Befund(
            GELB, "G06",
            f"Die Rechnung umfasst Zeiten aus {len(jahre)} Kalenderjahren "
            f"({', '.join(sorted(jahre))}). Für die EÜR zählt das Zahlungsdatum, aber die "
            "Zuordnung wird dadurch unübersichtlich.",
            "§ 11 EStG",
        ))
    return befunde


# ----------------------------------------------------- GELB: Zahlen und Mengen

def _plausibilitaet(conn, rechnung, kunde, betrieb, positionen, zeiten) -> list[Befund]:
    befunde: list[Befund] = []
    summe = rechnung["brutto_cent"]

    # Der eigentliche Zahlendreher-Detektor: Vergleich mit dem, was bisher ueblich war.
    vergleich = [
        int(z["brutto_cent"])
        for z in conn.execute(
            """
            SELECT brutto_cent FROM rechnung
             WHERE status = 'FESTGESCHRIEBEN' AND typ = 'RECHNUNG' AND brutto_cent > 0
             ORDER BY id DESC LIMIT ?
            """,
            (MEDIAN_FENSTER,),
        )
    ]
    mittel = _median(vergleich)
    if mittel and summe:
        if summe > mittel * AUSREISSER_FAKTOR_HOCH:
            befunde.append(Befund(
                GELB, "G08",
                f"Die Summe {euro(summe)} € ist mehr als dreimal so hoch wie der Mittelwert "
                f"der letzten {len(vergleich)} Rechnungen ({euro(int(mittel))} €). "
                "Bitte auf einen Zahlendreher prüfen.",
            ))
        elif summe < mittel * AUSREISSER_FAKTOR_NIEDRIG:
            befunde.append(Befund(
                GELB, "G08",
                f"Die Summe {euro(summe)} € ist auffällig niedrig gegenüber dem Mittelwert "
                f"der letzten {len(vergleich)} Rechnungen ({euro(int(mittel))} €).",
            ))

    standard = (kunde["satz_cent"] if kunde and kunde["satz_cent"] else None) or (
        betrieb["standardsatz_cent"] if betrieb else 0
    )
    abweichend = {
        p["einzelpreis_cent"] for p in positionen
        if p["quelle"] == "ZEIT" and standard and p["einzelpreis_cent"] != standard
    }
    for preis in sorted(abweichend):
        befunde.append(Befund(
            GELB, f"G09-{preis}",
            f"Eine Position rechnet mit {euro(preis)} €, hinterlegt sind {euro(standard)} €.",
        ))

    minuten = sum(int(z["dauer_minuten"]) for z in zeiten)
    von, bis = _datum(rechnung["leistung_von"]), _datum(rechnung["leistung_bis"])
    if minuten and von and bis:
        tage = (bis - von).days + 1
        if minuten > tage * STUNDEN_JE_TAG_MAX * 60:
            befunde.append(Befund(GELB, "G10", _text_g10(minuten, tage)))

    for position in positionen:
        brutto_zeile = (
            position["menge_tausendstel"] * position["einzelpreis_cent"] + 500
        ) // 1000
        if position["nachlass_cent"] > brutto_zeile:
            befunde.append(Befund(
                GELB, f"G19-{position['pos_nr']}",
                f"Position {position['pos_nr']}: der Nachlass "
                f"{euro(position['nachlass_cent'])} € ist größer als der Positionsbetrag "
                f"{euro(brutto_zeile)} €.",
            ))
        if (position["einzelpreis_cent"] == 0 or position["nachlass_cent"] > 0) and not (
            position["nachlass_grund"] or ""
        ).strip():
            befunde.append(Befund(
                GELB, f"G07-{position['pos_nr']}",
                f"Position {position['pos_nr']} hat keinen Betrag und keine Begründung. "
                "Auf dem PDF steht dann eine nackte Null.",
            ))
        if position["menge_tausendstel"] % 10:
            befunde.append(Befund(
                GELB, f"G20-{position['pos_nr']}",
                f"Position {position['pos_nr']}: die Menge hat mehr als zwei Dezimalstellen "
                "und ist auf dem Papier nicht nachrechenbar.",
            ))

    # Ein Kopierfehler ist eine Zeile, die ZWEIMAL dasteht -- nicht zwei
    # Positionen, die die Gruppierung absichtlich getrennt hat. "Pro Eintrag"
    # traegt den Titel als Bezeichnung und das Datum nur im Untertext, eine
    # Kulanzstunde steht neben der berechneten mit demselben Titel, und zwei
    # Stundensaetze ergeben zwei Zeilen. Gleich ist deshalb nur, was in
    # Bezeichnung, Untertext, Nachlass-Kennzeichen UND Einzelpreis
    # uebereinstimmt.
    schluessel = [
        (
            p["bezeichnung"], (p["untertext"] or "").strip(),
            bool(p["nachlass_cent"]), p["einzelpreis_cent"],
        )
        for p in positionen
    ]
    for nr, name in enumerate(
        sorted({s[0] for s in schluessel if schluessel.count(s) > 1}), start=1
    ):
        befunde.append(Befund(
            GELB, f"G17-{nr}", f"Die Bezeichnung „{name}“ kommt mehrfach vor — Kopierfehler?"))

    # Offene Zeiten desselben Kunden ausserhalb der Auswahl.
    offen = conn.execute(
        """
        SELECT COUNT(*) AS n, MIN(datum) AS ab FROM zeiteintrag
         WHERE kunde_id = ? AND rechnung_id IS NULL AND abgeschrieben_am IS NULL
           AND abrechnungsmodus <> 'INTERN'
        """,
        (rechnung["kunde_id"],),
    ).fetchone()
    if offen and offen["n"]:
        befunde.append(Befund(
            GELB, "G12",
            (
                "1 weitere Zeit dieses Kunden ist nicht abgerechnet"
                if offen["n"] == 1
                else f"{offen['n']} weitere Zeiten dieses Kunden sind nicht abgerechnet"
            ) + f" (ab {_datum_de(offen['ab'])}). Mitnehmen?",
        ))

    doppelt = conn.execute(
        """
        SELECT nummer FROM rechnung
         WHERE kunde_id = ? AND datum = ? AND brutto_cent = ?
           AND status = 'FESTGESCHRIEBEN' AND id <> ? LIMIT 1
        """,
        (rechnung["kunde_id"], rechnung["datum"], summe, rechnung["id"]),
    ).fetchone()
    if doppelt:
        befunde.append(Befund(
            GELB, "G13",
            f"Rechnung {doppelt['nummer']} hat denselben Kunden, dasselbe Datum und dieselbe "
            "Summe. Doppelt erfasst?",
        ))

    # Kleinunternehmergrenze: die Warnung haengt am Umsatz, nicht am Jahresende.
    befunde += _grenze(conn, rechnung, summe)
    return befunde


def _text_g10(minuten: int, tage: int) -> str:
    """Der Satz zu G10: die ERFASSTE Dauer als h:mm, Kalendertag in der richtigen Zahl.

    Vorher stand "44 Stunden auf 1 Kalendertage": die Stunden abgerundet --
    die Position derselben Rechnung zeigte 45,00 Std. -- und der Tag im
    Plural. Genannt wird jetzt die Summe der Zeiteintraege, wie sie die
    Zeitenliste zeigt; die Position kann davon nur um die Rundung abweichen.

    >>> _text_g10(2690, 1)
    'Erfasst sind 44:50 Stunden auf 1 Kalendertag — mehr als 16 Stunden je Tag sind \
physisch unplausibel.'
    >>> _text_g10(3000, 2)  # doctest: +ELLIPSIS
    'Erfasst sind 50:00 Stunden auf 2 Kalendertage — ...'
    """
    einheit = "Kalendertag" if tage == 1 else "Kalendertage"
    return (
        f"Erfasst sind {formatiere_dauer(minuten)} Stunden auf {tage} {einheit} — mehr als "
        f"{STUNDEN_JE_TAG_MAX} Stunden je Tag sind physisch unplausibel."
    )


def _bankverbindung(betrieb) -> list[Befund]:
    """G29: die gespeicherte IBAN des Betriebs besteht die Pruefung nicht.

    Die Einrichtung prueft sie seit der Schlussprobe (``eingabe.iban``); eine
    Datenbank aus der Zeit davor kann noch "DE00 1234" tragen -- und die
    Rechnung bittet dann um Ueberweisung auf ein Konto, das es nicht gibt.
    GELB, nicht ROT: die Bankverbindung ist keine Pflichtangabe, und der
    Kunde kann auch anders zahlen.

    >>> [b.code for b in _bankverbindung({"iban": "DE00 1234"})]
    ['G29']
    >>> _bankverbindung({"iban": "DE02120300000000202051"}), _bankverbindung({"iban": None})
    ([], [])
    """
    if betrieb is None:
        return []
    try:
        eingabe.iban(betrieb["iban"])
    except ValueError as fehler:
        return [Befund(
            GELB, "G29",
            f"Die gespeicherte Bankverbindung ist fehlerhaft — {fehler} Sie steht auf "
            "dieser Rechnung als Konto für die Zahlung; bitte unter „Einstellungen“ "
            "berichtigen.",
        )]
    return []


def _grenze(conn, rechnung, summe: int) -> list[Befund]:
    """G14: die Kleinunternehmergrenze des § 19 UStG.

    Gezaehlt wird wie in :func:`grenzwert.vereinnahmt_cent`: nur ein Umsatz
    mit Leistungsort INLAND gehoert in den Gesamtumsatz. Eine Leistung an
    einen EU-Unternehmer (§ 3a Abs. 2 UStG) oder ins Drittland ist im Inland
    nicht steuerbar (§ 19 Abs. 2 i. V. m. § 1 Abs. 1 Nr. 1 UStG) und loest die
    Warnung deshalb nie aus -- vorher meldete eine Reverse-Charge-Rechnung
    ueber 90.000 € "kaeme ueber 100.000 €", obwohl nichts zu klaeren war.

    Drei Lagen, drei Saetze:

    * Vorjahr ueber 25.000 €: Regelbesteuerung seit dem 1. Januar.
    * Grenze im laufenden Jahr schon ueberschritten: seit dem grenzreissenden
      Umsatz gilt die Regelbesteuerung (§ 19 Abs. 1 Satz 3 UStG) -- diese
      Rechnung ist nicht mehr "die, die die Grenze reisst".
    * Diese Rechnung brachte den Umsatz ueber die Grenze.

    Alle drei bleiben GELB, obwohl eine § 19-Rechnung nach ueberschrittener
    Grenze falsch ist: das Werkzeug kennt keinen Regelbesteuerungsmodus. Ein
    ROT liesse den Nutzer ohne Ausweg zurueck; die Rechnung muss dann ausserhalb
    erstellt werden, und genau das sagt der Text.
    """
    if rechnung["typ"] != "RECHNUNG" or rechnung["leistungsort"] != "INLAND":
        return []
    datum = _datum(rechnung["datum"])
    if datum is None:
        # Ein unlesbares Datum meldet R04; hier lief vorher int("kapu") in
        # einen ValueError und die Entwurfsseite in die 500.
        return []
    stand = grenzwert.stand(conn, datum.year)
    grenze = f"{euro(stand.grenze_cent)} €" + (" (Gründungsjahr)" if stand.gruendungsjahr else "")
    if stand.vorjahr_ueberschritten:
        return [Befund(
            GELB, "G14",
            f"Der Vorjahresumsatz lag mit {euro(stand.vorjahr_cent)} € über 25.000 €. "
            "Damit gilt seit dem 1. Januar die Regelbesteuerung — eine Rechnung ohne "
            "Umsatzsteuer wäre dann falsch.",
            "§ 19 Abs. 1 UStG",
        )]
    if stand.ueberschritten:
        return [Befund(
            GELB, "G14",
            f"Die Grenze von {grenze} ist mit {euro(stand.vereinnahmt_cent)} € bereits "
            "überschritten — seit dem grenzreißenden Umsatz gilt die Regelbesteuerung "
            "(§ 19 Abs. 1 Satz 3 UStG); diese Rechnung darf nicht ohne Umsatzsteuer "
            "ausgestellt werden.",
            "§ 19 Abs. 1 Satz 3 UStG",
        )]
    if stand.vereinnahmt_cent + summe > stand.grenze_cent:
        return [Befund(
            GELB, "G14",
            f"Mit dieser Rechnung käme der Jahresumsatz auf "
            f"{euro(stand.vereinnahmt_cent + summe)} € und damit über die Grenze von "
            f"{grenze}. Bereits der Umsatz, der die Grenze reißt, ist steuerpflichtig. "
            "Bitte vor dem Festschreiben steuerlich klären.",
            "§ 19 Abs. 1 UStG, Abschn. 19.1 Abs. 2 UStAE",
        )]
    return []


# ------------------------------------------------------------------- BLAU

def _hinweise(conn, rechnung, kunde, positionen) -> list[Befund]:
    befunde: list[Befund] = []
    if kunde and rechnung["zahlungsziel_tage"] is not None:
        erwartet = kunde["zahlungsziel_tage"]
        if erwartet is not None and rechnung["zahlungsziel_tage"] != erwartet:
            befunde.append(Befund(
                BLAU, "B01",
                f"Zahlungsziel {rechnung['zahlungsziel_tage']} Tage, beim Kunden hinterlegt "
                f"sind {erwartet}.",
            ))
    ohne_text = [p["pos_nr"] for p in positionen if not (p["untertext"] or "").strip()]
    if ohne_text and len(ohne_text) == len(positionen):
        befunde.append(Befund(BLAU, "B02", "Keine Position hat eine Beschreibung."))

    letztes = conn.execute(
        "SELECT MAX(zeitpunkt) AS z FROM backup_log WHERE geprueft = 1"
    ).fetchone()
    if letztes is None or letztes["z"] is None:
        befunde.append(Befund(BLAU, "B03", "Es gibt noch kein geprüftes Backup."))
    else:
        alter = (date.today() - date.fromisoformat(letztes["z"][:10])).days
        if alter > 7:
            befunde.append(Befund(BLAU, "B03", f"Das letzte Backup ist {alter} Tage alt."))
    return befunde


def blockiert(befunde: list[Befund]) -> bool:
    return any(b.blockiert for b in befunde)


def nach_stufe(befunde: list[Befund]) -> dict[str, list[Befund]]:
    return {stufe: [b for b in befunde if b.stufe == stufe] for stufe in (ROT, GELB, BLAU)}

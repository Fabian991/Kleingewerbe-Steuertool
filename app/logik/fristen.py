"""Fristen, die das Werkzeug im Blick behalten soll.

Es gibt **zwei**, und sie sind sehr unterschiedlich lang:

**Inland, sechs Monate.** § 14 Abs. 2 Satz 2 UStG verlangt die Rechnung binnen
sechs Monaten nach Ausfuehrung der Leistung, wenn der Empfaenger ein
Unternehmer oder eine juristische Person ist. Fuer Kleinunternehmer gilt das
unveraendert -- die Ausnahme der Norm greift nur fuer Umsaetze, die nach
§ 4 Nr. 8 bis 29 UStG steuerfrei sind, und § 19 Abs. 1 ist davon nicht erfasst.

**EU-Ausland, bis zum 15. des Folgemonats.** Bei einer sonstigen Leistung an
einen Unternehmer im uebrigen Gemeinschaftsgebiet gilt § 14a Abs. 1 Satz 2
UStG: die Rechnung ist "bis zum fuenfzehnten Tag des Monats, der auf den Monat
folgt, in dem der Umsatz ausgefuehrt worden ist, auszustellen". Das sind je
nach Leistungstag zwischen sechzehn und sechsundvierzig Tagen -- nicht sechs
Monate. § 19 Abs. 1 Satz 2 UStG nimmt von § 14a nur die USt-IdNr.-Pflicht
(Satz 3) aus, nicht diese Frist.

Diese zweite Frist ist die unangenehmere: sie ist zehnmal kuerzer, sie gilt
genau fuer die Kunden, bei denen man ohnehin unsicher ist, und kein
Marktprodukt warnt davor.

**Drittland: keine Frist.** Eine sonstige Leistung an einen Unternehmer mit
Sitz ausserhalb der EU wird an dessen Sitz ausgefuehrt (§ 3a Abs. 2 UStG) und
ist im Inland nicht steuerbar. § 14 Abs. 2 Satz 1 UStG erfasst nur Umsaetze
"nach § 1 Absatz 1 Nummer 1" -- also steuerbare --, und § 14a Abs. 1 UStG nur
Leistungen ins uebrige Gemeinschaftsgebiet. Beide Fristen greifen fuer einen
Drittlandskunden nicht; eine Warnung mit Fundstelle waere eine falsche
Rechtsbehauptung. Welche Pflichten das Recht des Empfaengerstaats aufstellt,
weiss dieses Werkzeug nicht und behauptet es deshalb auch nicht.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

#: Monate, nach denen die Rechnung ins Inland spaetestens draussen sein muss.
FRIST_MONATE = 6
#: Ab wann gewarnt wird -- einen Monat Vorlauf, damit noch Zeit zum Handeln bleibt.
WARNUNG_AB_MONATEN = 5

#: Tag des Folgemonats, bis zu dem eine EU-B2B-Rechnung draussen sein muss.
EU_FRIST_TAG = 15
#: Vorlauf fuer die EU-Frist. Kurz, weil die Frist selbst kurz ist: bei dreissig
#: Tagen Vorwarnung stuende die Warnung ab dem Tag der Leistung da und waere
#: nach zwei Wochen Tapete.
EU_WARNUNG_AB_TAGEN = 20

#: Nur diese Kundentypen loesen die Rechnungspflicht aus.
PFLICHTTYPEN = ("UNTERNEHMER", "JURISTISCHE_PERSON")

#: Leistungsorte, fuer die die kurze Frist des § 14a Abs. 1 Satz 2 UStG gilt.
EU_LEISTUNGSORTE = ("EU_B2B_REVERSE_CHARGE",)

#: Leistungsorte OHNE Rechnungsfrist nach deutschem Recht (siehe Modul-Docstring).
OHNE_FRIST_LEISTUNGSORTE = ("DRITTLAND",)


def _monate_zurueck(von: date, monate: int) -> date:
    """Datum um Monate zurueck, ohne externe Bibliothek."""
    jahr, monat = divmod((von.year * 12 + von.month - 1) - monate, 12)
    tag = min(von.day, [31, 29 if jahr % 4 == 0 and (jahr % 100 or jahr % 400 == 0) else 28,
                        31, 30, 31, 30, 31, 31, 30, 31, 30, 31][monat])
    return date(jahr, monat + 1, tag)


def _monatsende_plus(von: date, tag: int) -> date:
    """Der ``tag``. des Monats, der auf ``von`` folgt.

    >>> _monatsende_plus(date(2026, 3, 2), 15)
    datetime.date(2026, 4, 15)
    >>> _monatsende_plus(date(2026, 12, 31), 15)
    datetime.date(2027, 1, 15)
    """
    jahr, monat = divmod(von.year * 12 + von.month, 12)
    return date(jahr, monat + 1, tag)


@dataclass(frozen=True, slots=True)
class Fristfall:
    kunde_id: int
    kunde: str
    aeltestes: str
    eintraege: int
    minuten: int
    #: Stichtag, gegen den gerechnet wird. Bewusst mitgefuehrt und nicht aus
    #: ``date.today()`` geholt: sonst haengt das Ergebnis am Aufrufzeitpunkt und
    #: ist nicht pruefbar.
    heute: str = ""
    #: Leistungsort des Kunden. Er entscheidet, WELCHE Frist gilt.
    leistungsort: str = "INLAND"
    #: Der Entwurf, in dem der aelteste Eintrag steckt -- ``None``, wenn er
    #: noch auf keiner Rechnung liegt. Die Anzeige braucht das fuer den Link:
    #: eine Zeit im Entwurf ist im Assistenten nicht mehr waehlbar, und ein
    #: Link dorthin fuehrte zu "keine Zeiten offen".
    entwurf_id: int | None = None

    @property
    def ist_eu(self) -> bool:
        return self.leistungsort in EU_LEISTUNGSORTE

    @property
    def frist_am(self) -> str:
        """Letzter Tag, an dem die Rechnung draussen sein muss."""
        aelteste = date.fromisoformat(self.aeltestes)
        if self.ist_eu:
            # § 14a Abs. 1 Satz 2 UStG: 15. des Folgemonats. Gerechnet ab dem
            # AELTESTEN Eintrag -- fuer den laeuft die Frist zuerst ab.
            return _monatsende_plus(aelteste, EU_FRIST_TAG).isoformat()
        jahr, monat = divmod((aelteste.year * 12 + aelteste.month - 1) + FRIST_MONATE, 12)
        # Auf den 28. kappen: der 31. August plus sechs Monate ergaebe den
        # 31. Februar. Einen Tag zu frueh zu mahnen ist harmlos, einen Tag zu
        # spaet nicht.
        return date(jahr, monat + 1, min(aelteste.day, 28)).isoformat()

    @property
    def fundstelle(self) -> str:
        if self.ist_eu:
            return "§ 14a Abs. 1 Satz 2 UStG"
        return "§ 14 Abs. 2 Satz 2 UStG"

    @property
    def tage_rest(self) -> int:
        stichtag = date.fromisoformat(self.heute) if self.heute else date.today()
        return (date.fromisoformat(self.frist_am) - stichtag).days

    @property
    def ueberschritten(self) -> bool:
        return self.tage_rest < 0


def offene_fristen(conn: sqlite3.Connection, *, heute: date | None = None) -> list[Fristfall]:
    """Kunden mit unberechneten Zeiten, deren Frist in Sicht ist oder lief.

    Gezaehlt werden nur Zeiten, die ueberhaupt auf eine Rechnung sollen --
    interne Arbeit und Abgeschriebenes bleiben aussen vor.

    Beide Fristen werden in einem Durchlauf geprueft, aber mit
    unterschiedlichen Stichtagen: sechs Monate fuers Inland, der 15. des
    Folgemonats fuer EU-B2B. Deshalb wird nicht in SQL gefiltert, sondern erst
    am fertigen Fall -- ein Stichtag fuer beide Fristen waere entweder zu
    frueh (Inland dauernd gelb) oder zu spaet (EU-Frist schon abgelaufen,
    wenn die Warnung kommt).
    """
    tag = heute or date.today()
    inlandsgrenze = _monate_zurueck(tag, WARNUNG_AB_MONATEN).isoformat()
    platzhalter = ", ".join("?" * len(PFLICHTTYPEN))
    platzhalter_frei = ", ".join("?" * len(OHNE_FRIST_LEISTUNGSORTE))

    faelle = [
        Fristfall(
            kunde_id=z["kunde_id"], kunde=z["kunde"], aeltestes=z["aeltestes"],
            eintraege=z["eintraege"], minuten=z["minuten"], heute=tag.isoformat(),
            leistungsort=z["leistungsort"], entwurf_id=z["entwurf_id"],
        )
        for z in conn.execute(
            f"""
            SELECT k.id AS kunde_id, k.name AS kunde, k.leistungsort,
                   MIN(z.datum) AS aeltestes,
                   COUNT(*)     AS eintraege,
                   SUM(z.dauer_minuten) AS minuten,
                   -- Der Entwurf, in dem der AELTESTE offene Eintrag steckt;
                   -- NULL, wenn der aelteste noch auf keiner Rechnung liegt.
                   (SELECT CASE WHEN r2.status = 'ENTWURF' THEN z2.rechnung_id END
                      FROM zeiteintrag z2
                      LEFT JOIN rechnung r2 ON r2.id = z2.rechnung_id
                     WHERE z2.kunde_id = k.id
                       AND (z2.rechnung_id IS NULL OR r2.status = 'ENTWURF')
                       AND z2.abgeschrieben_am IS NULL
                       AND z2.abrechnungsmodus <> 'INTERN'
                     ORDER BY z2.datum, z2.id
                     LIMIT 1) AS entwurf_id
              FROM zeiteintrag z
              JOIN kunde k ON k.id = z.kunde_id
             -- Ein ENTWURF stellt nichts aus: er hat keine Nummer, kein Datum
             -- und laesst sich jederzeit verwerfen. Die Frist des § 14 Abs. 2
             -- Satz 2 UStG laeuft trotzdem weiter. Zaehlte ein Entwurf als
             -- erledigt, loeschte das Anlegen eines Entwurfs die Warnung --
             -- und zwar dauerhaft, denn danach taucht die Zeit nirgends mehr
             -- auf. Erledigt ist die Frist erst mit dem Festschreiben.
             WHERE (z.rechnung_id IS NULL OR z.rechnung_id IN (
                        SELECT id FROM rechnung WHERE status = 'ENTWURF'))
               AND z.abgeschrieben_am IS NULL
               AND z.abrechnungsmodus <> 'INTERN'
               AND k.typ IN ({platzhalter})
               -- Drittland: keine Frist nach deutschem Recht (Modul-Docstring).
               AND k.leistungsort NOT IN ({platzhalter_frei})
             GROUP BY k.id, k.name, k.leistungsort
             ORDER BY MIN(z.datum)
            """,
            (*PFLICHTTYPEN, *OHNE_FRIST_LEISTUNGSORTE),
        )
    ]

    fristnah = []
    for fall in faelle:
        if fall.ist_eu:
            if fall.tage_rest <= EU_WARNUNG_AB_TAGEN:
                fristnah.append(fall)
        elif fall.aeltestes <= inlandsgrenze:
            fristnah.append(fall)
    # Die knappste Frist zuerst, nicht das aelteste Datum: bei gemischten
    # Fristen ist das aelteste nicht das dringendste.
    return sorted(fristnah, key=lambda f: f.tage_rest)

"""Ein Datenbestand im Stand von Schema 20 -- so, wie ihn der Nutzer hat.

Gebaut aus dem echten Schema von Version 20 (``fixtures/schema_v20.sql``,
das schema.sql aus dem Commit vor den Wunschpaketen) mit reinem SQL, ohne den
heutigen Code: Kunden, Zeiten samt laufendem Timer, festgeschriebene und
offene Rechnung, ein Entwurf, Zahlungen, Belege in Euro und in Dollar, ein
Anlagegut mit AfA, die Steuerrücklage mit gezahlten Vorauszahlungen und ein
hinterlegter Eingangsordner. Erfundene Daten.

Nur die Kategorien kommen aus der mitgelieferten Stammdatendatei (über
``kategorien.abgleichen``) -- so, wie Version 20 sie beim Start anlegt.

Dieses Modul ist bewusst unabhängig vom heutigen Schema: die Referenzwerte im
Upgrade-Test (``test_upgrade_v20.py``) wurden mit dem Code von Version 20
auf GENAU diesem Bestand berechnet.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

V20 = (Path(__file__).parent / "fixtures" / "schema_v20.sql").read_text(encoding="utf-8")

#: Das Jahr des Bestands. Fest, damit die Referenzwerte nicht mit dem Kalender wandern.
JAHR = 2026

#: Der laufende Timer: Datum und Uhrzeit in Ortszeit, wie Version 20 sie speichert.
TIMER_DATUM = "2026-10-26"
TIMER_START = "09:00"


def baue(pfad: Path, belegordner: Path, *, schema: str = V20, version: int = 20) -> None:
    """Legt die Datenbank im Stand 20 unter ``pfad`` an.

    ``schema`` und ``version``: derselbe Bestand auf einem spaeteren Schema
    (``altbestand_v25.py``) -- die Spalten dieser Einfuegungen gibt es dort
    unveraendert, die spaeter hinzugekommenen haben Vorgaben.
    """
    from app.logik import kategorien

    conn = sqlite3.connect(pfad, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    conn.execute("BEGIN")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version VALUES (?, '2026-01-02T08:00:00+00:00')",
                 (version,))
    kategorien.abgleichen(conn)
    kat = {z["schluessel"]: z["id"]
           for z in conn.execute("SELECT id, schluessel FROM euer_kategorie")}

    conn.execute(
        "INSERT INTO betrieb (id, name, inhaber, strasse, plz, ort, email, telefon, steuernummer,"
        " iban, bic, standardsatz_cent, beginn_am, belegordner_pfad)"
        " VALUES (1, 'Beispiel Werbung', 'Erika Musterfrau', 'Musterstraße 1', '12345',"
        " 'Musterstadt', 'info@beispiel.invalid', '0123 456789', '12/345/67890',"
        " 'DE02120300000000202051', 'BYLADEM1001', 8000, '2020-01-01', ?)",
        (str(belegordner),),
    )
    conn.execute(
        "INSERT INTO kunde (id, kundennummer, name, strasse, plz, ort, satz_cent, angelegt_am)"
        " VALUES (1, 'K-0001', 'Beispiel GmbH', 'Hauptstraße 5', '10115', 'Berlin', 9000,"
        " '2026-01-05')"
    )
    conn.execute(
        "INSERT INTO kunde (id, kundennummer, name, strasse, plz, ort, angelegt_am)"
        " VALUES (2, 'K-0002', 'Muster AG', 'Marktplatz 2', '20095', 'Hamburg', '2026-02-01')"
    )

    absender = json.dumps({"name": "Beispiel Werbung", "inhaber": "Erika Musterfrau",
                           "strasse": "Musterstraße 1", "plz": "12345", "ort": "Musterstadt",
                           "steuernummer": "12/345/67890", "iban": "DE02120300000000202051"},
                          ensure_ascii=False)

    def kunde_json(name: str, strasse: str, plz: str, ort: str) -> str:
        return json.dumps({"name": name, "strasse": strasse, "plz": plz, "ort": ort,
                           "land": "DE"}, ensure_ascii=False)

    hinweis = "Steuerfreie Leistung eines Kleinunternehmers gemäß § 19 Abs. 1 UStG."
    # Rechnung 1: festgeschrieben und bezahlt. Rechnung 3: festgeschrieben,
    # offen. Rechnung 4: eine Pauschale, in zwei Raten bezahlt. Rechnung 2:
    # ein Entwurf mit der Betriebsregel.
    for rid, nummer, seq, kunde, datum, brutto, kopf in (
        (1, "RE-2026-0001", 1, 1, "2026-03-31", 13500,
         kunde_json("Beispiel GmbH", "Hauptstraße 5", "10115", "Berlin")),
        (3, "RE-2026-0002", 2, 2, "2026-05-31", 12000,
         kunde_json("Muster AG", "Marktplatz 2", "20095", "Hamburg")),
        (4, "RE-2026-0003", 3, 2, "2026-06-30", 450000,
         kunde_json("Muster AG", "Marktplatz 2", "20095", "Hamburg")),
    ):
        conn.execute(
            "INSERT INTO rechnung (id, nummer, nummer_seq, nummer_jahr, kunde_id,"
            " kunde_snapshot_json, absender_snapshot_json, datum, zahlungsziel_tage, faellig_am,"
            " netto_cent, brutto_cent, hinweis_19_text, betreff, rundung_hinweis, status,"
            " pdf_sha256, festgeschrieben_am, erstellt_am)"
            " VALUES (?, ?, ?, 2026, ?, ?, ?, ?, 14, date(?, '+14 days'), ?, ?, ?, ?,"
            " 'Abrechnung in angefangenen 15 Minuten.', 'FESTGESCHRIEBEN', ?, ?, ?)",
            (rid, nummer, seq, kunde, kopf, absender, datum, datum, brutto, brutto, hinweis,
             f"Rechnung {nummer}", "0" * 64, f"{datum}T10:00:00+00:00",
             f"{datum}T09:00:00+00:00"),
        )
    conn.execute(
        "INSERT INTO rechnung (id, kunde_id, datum, zahlungsziel_tage, faellig_am, netto_cent,"
        " brutto_cent, hinweis_19_text, betreff, rundung_hinweis, status, erstellt_am)"
        " VALUES (2, 1, '2026-10-25', 14, '2026-11-08', 9000, 9000, ?, 'Rechnung – Oktober 2026',"
        " 'Abrechnung in angefangenen 15 Minuten.', 'ENTWURF', '2026-10-25T09:00:00+00:00')",
        (hinweis,),
    )
    for pid, rid, menge, satz in ((1, 1, 1500, 9000), (3, 3, 1500, 8000), (2, 2, 1000, 9000)):
        conn.execute(
            "INSERT INTO rechnungsposition (id, rechnung_id, pos_nr, bezeichnung,"
            " menge_tausendstel, einzelpreis_cent, betrag_cent, quelle)"
            " VALUES (?, ?, 1, 'Beratung', ?, ?, ?, 'ZEIT')",
            (pid, rid, menge, satz, (menge * satz + 500) // 1000),
        )
    conn.execute(
        "INSERT INTO rechnungsposition (id, rechnung_id, pos_nr, bezeichnung, menge_tausendstel,"
        " einheit, einzelpreis_cent, darstellung, betrag_cent, quelle)"
        " VALUES (4, 4, 1, 'Kampagnenbetreuung Q2', 1000, 'C62', 450000, 'NUR_TEXT', 450000,"
        " 'FREI')"
    )

    # Zeiten: zwei auf Rechnung 1, eine auf Rechnung 3, eine im Entwurf, eine
    # frei, und ein Timer, der gerade laeuft (Start ohne Ende, Platzhalterminute).
    zeiten = (
        (1, 1, "Beratung", "2026-03-10", None, None, 60, 9000, 1),
        (2, 1, "Beratung", "2026-03-11", None, None, 30, 9000, 1),
        (3, 2, "Beratung", "2026-05-05", None, None, 90, 8000, 3),
        (4, 1, "Beratung", "2026-10-20", None, None, 50, None, 2),
        (5, 2, "Konzept", "2026-10-21", None, None, 45, None, None),
        (6, 1, "Kampagne", TIMER_DATUM, TIMER_START, None, 1, None, None),
    )
    for zid, kunde, titel, datum, start, ende, minuten, satz, rid in zeiten:
        conn.execute(
            "INSERT INTO zeiteintrag (id, kunde_id, titel, beschreibung, datum, start_zeit,"
            " end_zeit, dauer_minuten, satz_cent_snapshot, rechnung_id, erfasst_am)"
            " VALUES (?, ?, ?, 'Abstimmung; Entwurf', ?, ?, ?, ?, ?, ?, ?)",
            (zid, kunde, titel, datum, start, ende, minuten, satz, rid,
             f"{datum}T08:00:00+00:00"),
        )
    for pid, zid in ((1, 1), (1, 2), (3, 3), (2, 4)):
        conn.execute("INSERT INTO position_zeiteintrag VALUES (?, ?)", (pid, zid))
    conn.execute("INSERT INTO nummernkreis (typ, jahr, format, naechste_seq)"
                 " VALUES ('RECHNUNG', 2026, 'RE-{JJJJ}-{NNNN}', 4)")
    for rid, datum, betrag in ((1, "2026-04-08", 13500), (4, "2026-07-15", 200000),
                               (4, "2026-08-01", 250000)):
        conn.execute("INSERT INTO zahlung (rechnung_id, datum, betrag_cent, art)"
                     " VALUES (?, ?, ?, 'UEBERWEISUNG')", (rid, datum, betrag))

    # Belege: Hosting in Euro, ein Abo in Dollar (EZB-Kurs), ein Laptop als
    # Anlagegut mit AfA-Reihe. Alle bezahlt.
    conn.execute(
        "INSERT INTO lieferant (id, name, standard_kategorie_id, angelegt_am)"
        " VALUES (1, 'Beispiel Hosting GmbH', ?, '2026-02-01')", (kat["HOSTING"],))
    conn.execute(
        "INSERT INTO lieferant (id, name, land, standard_kategorie_id, waehrung_default,"
        " angelegt_am) VALUES (2, 'Example Software Inc.', 'US', ?, 'USD', '2026-04-10')",
        (kat["SAAS"],))
    conn.execute(
        "INSERT INTO lieferant (id, name, angelegt_am) VALUES (3, 'Muster Computer KG',"
        " '2026-06-15')")
    conn.execute(
        "INSERT INTO beleg (id, lieferant_id, kategorie_id, belegdatum, belegnummer_extern,"
        " betrag_brutto_cent, betrag_netto_cent, betrag_ust_cent, ust_satz_promille,"
        " netto_fuer_grenzwert_cent, betrag_betrieblich_cent, geprueft_am, erfasst_am)"
        " VALUES (1, 1, ?, '2026-02-01', 'H-2026-02', 11900, 10000, 1900, 190, 10000, 11900,"
        " '2026-02-02T09:00:00+00:00', '2026-02-02T09:00:00+00:00')", (kat["HOSTING"],))
    conn.execute(
        "INSERT INTO beleg (id, lieferant_id, kategorie_id, belegdatum, belegnummer_extern,"
        " waehrung, betrag_fremdwaehrung_cent, kurs, kurs_datum, kurs_quelle, kurs_abruf_utc,"
        " betrag_umgerechnet_cent, betrag_brutto_cent, netto_fuer_grenzwert_cent,"
        " betrag_betrieblich_cent, zahlungsart, geprueft_am, erfasst_am)"
        " VALUES (2, 2, ?, '2026-04-10', 'INV-0042', 'USD', 2000, '1.0856', '2026-04-10', 'EZB',"
        " '2026-04-11T07:00:00+00:00', 1842, 1842, 1842, 1842, 'KARTE',"
        " '2026-04-11T07:00:00+00:00', '2026-04-11T07:00:00+00:00')", (kat["SAAS"],))
    conn.execute(
        "INSERT INTO beleg (id, lieferant_id, kategorie_id, belegdatum, belegnummer_extern,"
        " betrag_brutto_cent, betrag_netto_cent, betrag_ust_cent, ust_satz_promille,"
        " netto_fuer_grenzwert_cent, betrag_betrieblich_cent, ist_anlagegut, geprueft_am,"
        " erfasst_am) VALUES (3, 3, ?, '2026-06-15', 'R-7781', 119000, 100000, 19000, 190,"
        " 100000, 119000, 1, '2026-06-16T09:00:00+00:00', '2026-06-16T09:00:00+00:00')",
        (kat["HARDWARE_ANLAGE"],))
    for bid, name, sha in ((1, "hosting-februar.pdf", "a" * 64),
                           (2, "invoice-0042.pdf", "b" * 64),
                           (3, "laptop.pdf", "c" * 64)):
        conn.execute(
            "INSERT INTO belegdatei (beleg_id, pfad, original_name, sha256, groesse, mime,"
            " importiert_am) VALUES (?, ?, ?, ?, 1234, 'application/pdf', ?)",
            (bid, f"belege/2026/{name}", name, sha, "2026-06-16T09:00:00+00:00"))
    for bid, datum, betrag, art in ((1, "2026-02-03", -11900, "UEBERWEISUNG"),
                                    (2, "2026-04-12", -1842, "KARTE"),
                                    (3, "2026-06-16", -119000, "UEBERWEISUNG")):
        conn.execute("INSERT INTO zahlung (beleg_id, datum, betrag_cent, art) VALUES (?, ?, ?, ?)",
                     (bid, datum, betrag, art))
    conn.execute(
        "INSERT INTO anlagegut (id, beleg_id, bezeichnung, anschaffung_am, ak_brutto_cent,"
        " ak_netto_cent, nutzungsdauer_jahre, methode, erfasst_am)"
        " VALUES (1, 3, 'Laptop', '2026-06-15', 119000, 100000, 3, 'LINEAR',"
        " '2026-06-16T09:00:00+00:00')")
    for jahr, afa, rest in ((2026, 23138, 95862), (2027, 39666, 56196),
                            (2028, 39666, 16530), (2029, 16530, 0)):
        conn.execute("INSERT INTO anlagegut_afa_jahr VALUES (1, ?, ?, 130, ?)", (jahr, afa, rest))

    # Die Steuerrücklage mit dem bisherigen Summenfeld der Vorauszahlungen.
    conn.execute(
        "INSERT INTO steuerschaetzung (jahr, zve_ohne_cent, kirchensteuer_prozent,"
        " vorauszahlungen_cent, hebesatz_prozent, geaendert_am)"
        " VALUES (2026, 3000000, 9, 120000, 440, '2026-03-01T09:00:00+00:00')")
    conn.execute("COMMIT")
    conn.close()

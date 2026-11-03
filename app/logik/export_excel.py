"""Der ganze Datenbestand als Tabellenkalkulation — der Notausgang.

Der Zweck dieses Moduls ist ausdrücklich **Unabhängigkeit von diesem
Werkzeug**. Eine Sicherung als SQLite-Datei nützt nur, solange es noch etwas
gibt, das sie öffnet. Eine Tabellenkalkulation öffnet jeder Rechner, jedes
Büro, jeder Steuerberater — heute und in zehn Jahren, wenn die
Aufbewahrungsfrist abläuft. § 147 Abs. 3 AO gibt zehn Jahre für Bücher und
Aufzeichnungen und acht für Buchungsbelege; diese Datei enthält beides.

Deshalb ist die Datei *lesbar* und nicht nur *vollständig*:

* **Beträge sind Euro-Zahlen, keine Cent-Ganzzahlen.** Intern steht überall
  Integer-Cent, weil Fließkomma in einer Buchführung nichts verloren hat.
  In der Tabelle stünde dann aber ``63646``, und niemand wüsste, dass
  636,46 € gemeint sind — summierbar wäre die Spalte trotzdem, nur eben falsch
  interpretierbar. Hier wird einmal geteilt und als Währung formatiert.
* **Datumsangaben sind echte Datumswerte**, keine Zeichenketten. Sonst
  sortiert die Tabelle ``2026-1-9`` hinter ``2026-10-01``.
* **Fremdschlüssel bekommen Klartext daneben.** ``kunde_id = 7`` ist in einer
  Tabelle ohne die Datenbank wertlos; ``7 | Alpenbau GmbH & Co KG`` nicht.
* **Aufzählungen werden ausgeschrieben.** ``EU_B2B_REVERSE_CHARGE`` ist ein
  Datenbankwert, kein deutsches Wort.

Was NICHT exportiert wird, steht in :data:`NICHT_EXPORTIERT` — jeweils mit
Begründung. Ein Test hält beide Listen gegen das Schema, damit eine neue
Tabelle nicht stillschweigend aus der Sicherung fällt.
"""

from __future__ import annotations

import datetime as _dt
import io
import os
import sqlite3
import tempfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

#: Dateiname im Sicherungsarchiv und als Vorschlag beim Herunterladen.
DATEINAME = "steuer-daten.xlsx"

#: Tabellen, die NICHT in die Datei wandern -- mit dem Grund, damit niemand sie
#: fuer vergessen haelt.
NICHT_EXPORTIERT = {
    "schema_version": "Technischer Stand der Datenbank, kein Geschaeftsvorfall.",
    "extraktion": (
        "Rohantworten der Belegauslesung (OCR/JSON). Sie sind Zwischenergebnis, "
        "nicht Buchfuehrung -- das BESTAETIGTE Ergebnis steht in beleg."
    ),
    "lieferant_profil": (
        "Gelernte Suchmuster je Lieferant. Reine Werkzeuginterna; ohne dieses "
        "Werkzeug bedeutungslos."
    ),
    "loeschfreigabe": (
        "Technische Freigabe, die nur innerhalb einer einzigen Transaktion existiert "
        "(Zuruecknehmen einer nie versendeten Rechnung) und danach immer leer ist. Was "
        "zurueckgenommen wurde, steht im Journal und im Blatt Nummernluecken."
    ),
}

#: Reihenfolge der Blaetter. Vorne, was man am haeufigsten braucht.
BLATTFOLGE = (
    "betrieb", "kunde", "rechnung", "rechnungsposition", "zahlung", "zeiteintrag",
    "zeit_abschnitt", "position_zeiteintrag", "leistung", "lieferant", "beleg", "belegdatei",
    "einnahme", "einnahmedatei", "zahler",
    "anlagegut", "anlagegut_afa_jahr", "reise", "euer_kategorie", "steuerschaetzung",
    "steuer_vorauszahlung", "belegordner_jahr", "nummernkreis", "steuerstatus_zeitraum",
    "wiso_import", "wiso_buchung", "wiso_konto", "fehlende_angabe",
    "backup_log", "journal",
)

#: Blattnamen. Excel erlaubt 31 Zeichen und kein ``[]:*?/\``.
BLATTNAMEN = {
    "betrieb": "Betrieb",
    "kunde": "Kunden",
    "rechnung": "Rechnungen",
    "rechnungsposition": "Rechnungspositionen",
    "zahlung": "Zahlungen",
    "zeiteintrag": "Zeiten",
    "zeit_abschnitt": "Zeitabschnitte",
    "position_zeiteintrag": "Position zu Zeiteintrag",
    "leistung": "Leistungskatalog",
    "lieferant": "Lieferanten",
    "beleg": "Belege",
    "belegdatei": "Belegdateien",
    "anlagegut": "Anlagegüter",
    "anlagegut_afa_jahr": "AfA je Jahr",
    "reise": "Reisen",
    "euer_kategorie": "EÜR-Kategorien",
    "steuerschaetzung": "Steuerrücklage (Angaben)",
    "steuer_vorauszahlung": "Vorauszahlungen",
    "belegordner_jahr": "Eingangsordner je Jahr",
    "nummernkreis": "Nummernkreise",
    "steuerstatus_zeitraum": "Steuerstatus",
    "backup_log": "Sicherungen",
    # Einnahmen ohne eigene Rechnung und der Import aus WISO Steuer (Migration 26).
    "einnahme": "Einnahmen ohne Rechnung",
    "einnahmedatei": "Einnahmedateien",
    "zahler": "Zahler (Einstufung)",
    "wiso_import": "WISO-Importe",
    "wiso_buchung": "WISO-Buchungen",
    "wiso_konto": "WISO-Kontozuordnung",
    "fehlende_angabe": "Angaben fehlen",
    "journal": "Journal",
}

#: Klartext neben dem Fremdschluessel: Spalte -> (Ueberschrift, SQL-Ausdruck).
#: ``t`` ist die exportierte Tabelle.
VERKNUEPFUNGEN = {
    "kunde_id": ("Kunde", "(SELECT k.name FROM kunde k WHERE k.id = t.kunde_id)"),
    "rechnung_id": (
        "Rechnungsnummer",
        "(SELECT r.nummer FROM rechnung r WHERE r.id = t.rechnung_id)",
    ),
    "storno_von_id": (
        "Storno zu",
        "(SELECT r.nummer FROM rechnung r WHERE r.id = t.storno_von_id)",
    ),
    "lieferant_id": ("Lieferant", "(SELECT l.name FROM lieferant l WHERE l.id = t.lieferant_id)"),
    "kategorie_id": (
        "Kategorie",
        "(SELECT e.bezeichnung FROM euer_kategorie e WHERE e.id = t.kategorie_id)",
    ),
    "standard_kategorie_id": (
        "Standardkategorie",
        "(SELECT e.bezeichnung FROM euer_kategorie e WHERE e.id = t.standard_kategorie_id)",
    ),
    "beleg_id": (
        "Beleg",
        "(SELECT COALESCE(b.belegnummer_extern, 'Beleg ' || b.id) FROM beleg b"
        " WHERE b.id = t.beleg_id)",
    ),
    "reise_id": ("Reise", "(SELECT re.bezeichnung FROM reise re WHERE re.id = t.reise_id)"),
    "anlagegut_id": (
        "Anlagegut",
        "(SELECT a.bezeichnung FROM anlagegut a WHERE a.id = t.anlagegut_id)",
    ),
    "position_id": (
        "Position",
        "(SELECT p.bezeichnung FROM rechnungsposition p WHERE p.id = t.position_id)",
    ),
    "zeiteintrag_id": (
        "Zeiteintrag",
        "(SELECT z.titel FROM zeiteintrag z WHERE z.id = t.zeiteintrag_id)",
    ),
    "freigegeben_aus_rechnung_id": (
        "Freigegeben aus",
        "(SELECT r.nummer FROM rechnung r WHERE r.id = t.freigegeben_aus_rechnung_id)",
    ),
    "ersetzt_rechnung_id": (
        "Ersetzt Rechnung",
        "(SELECT r.nummer FROM rechnung r WHERE r.id = t.ersetzt_rechnung_id)",
    ),
    "einnahme_id": (
        "Einnahme",
        "(SELECT e.zahler || ' vom ' || strftime('%d.%m.%Y', e.zufluss_am) FROM einnahme e"
        " WHERE e.id = t.einnahme_id)",
    ),
    "zahlung_id": (
        "Zahlung",
        "(SELECT 'Zahlung ' || v.id || ' vom ' || strftime('%d.%m.%Y', v.datum)"
        " FROM zahlung v WHERE v.id = t.zahlung_id)",
    ),
    "import_id": (
        "Import",
        "(SELECT i.dateiname || ' vom ' || strftime('%d.%m.%Y', i.importiert_am)"
        " FROM wiso_import i WHERE i.id = t.import_id)",
    ),
}

#: Wie VERKNUEPFUNGEN, aber nur fuer EINE Tabelle: derselbe Spaltenname
#: bedeutet dort etwas anderes. ``storno_von_id`` zeigt bei einer Rechnung auf
#: die stornierte Rechnung, bei einer Zahlung auf die aufgehobene Zahlung --
#: ueber den gemeinsamen Eintrag stuende neben der Gegenbuchung die Nummer
#: irgendeiner Rechnung mit derselben Kennung.
TABELLEN_VERKNUEPFUNGEN = {
    "zahlung": {
        "storno_von_id": (
            "Hebt Zahlung auf",
            "(SELECT 'Zahlung ' || v.id || ' vom ' || strftime('%d.%m.%Y', v.datum)"
            " FROM zahlung v WHERE v.id = t.storno_von_id)",
        ),
    },
}

#: Ueberschriften, die nur in EINER Tabelle gelten (siehe oben).
TABELLEN_KOPF = {
    ("zahlung", "storno_von_id"): "Gegenbuchung zu Zahlung-ID",
}

#: Datenbankwerte in deutschen Worten. Ein einziger Topf fuer alle Tabellen:
#: die Werte sind projektweit eindeutig, und eine Aufteilung je Spalte waere
#: dreimal so lang, ohne etwas zu klaeren.
KLARTEXT = {
    "INLAND": "Inland",
    "EU_B2B_REVERSE_CHARGE": "EU-B2B, Reverse Charge",
    "DRITTLAND": "Drittland",
    "ENTWURF": "Entwurf",
    "FESTGESCHRIEBEN": "festgeschrieben",
    "STORNIERT": "storniert",
    "RECHNUNG": "Rechnung",
    "STORNO": "Storno",
    "GUTSCHRIFT_SELBSTABRECHNUNG": "Gutschrift (Selbstabrechnung)",
    "ANGEBOT": "Angebot",
    "LEISTUNGSNACHWEIS": "Leistungsnachweis",
    "KU19": "Kleinunternehmer § 19",
    "REGEL": "Regelbesteuerung",
    "UNTERNEHMER": "Unternehmer",
    "JURISTISCHE_PERSON": "juristische Person",
    "PRIVAT": "Privatperson",
    "PAPIER": "Papier",
    "PDF_EMAIL": "PDF per E-Mail",
    "E_RECHNUNG": "E-Rechnung",
    "SAMMEL": "eine Sammelposition",
    "PRO_TITEL": "je Titel",
    "PRO_TAG": "je Tag",
    "PRO_EINTRAG": "je Eintrag",
    "STUNDEN": "nach Stunden",
    "PAUSCHAL": "pauschal",
    "KULANZ": "Kulanz (nicht berechnet)",
    "UEBERWEISUNG": "Überweisung",
    "KARTE": "Karte",
    "BAR": "bar",
    "PAYPAL": "PayPal",
    "SONSTIGES": "sonstiges",
    "AUFWAERTS": "aufwärts (angefangene Einheit)",
    "KAUFMAENNISCH": "kaufmännisch",
    "ABWAERTS": "abwärts",
    "KEINE": "keine Rundung",
    "NUR_TEXT": "nur Text (Pauschale)",
    "MENGE_PREIS": "Menge × Preis",
    "HUR": "Stunde",
    "DAY": "Tag",
    "C62": "Stück",
    "LS": "Pauschale",
    "EIN_JAHR_IT": "Sofortabschreibung Digitalgut",
    "GWG": "geringwertiges Wirtschaftsgut",
    "GWG_SOFORT": "GWG (Sofortabzug)",
    "LINEAR": "linear",
    "BEWEGLICH": "beweglich",
    "IMMATERIELL": "immateriell",
    "EINGANG": "Eingang",
    "AUSGANG": "Ausgang",
    "BETRAG": "Betrag",
    "ZEIT": "aus Zeiterfassung",
    "FREI": "frei erfasst",
    "INTERN": "intern (nie berechnet)",
    "ORIGINAL": "Original",
    "ABGELEITETES_XML": "abgeleitetes XML",
    "EMAIL": "E-Mail",
    # Import aus WISO Steuer (Migration 26): was aus einer Buchung wurde und
    # welche Angabe fehlt.
    "AUSGABE": "Ausgabe (Beleg)",
    "EINNAHME": "Einnahme",
    "ERSTATTUNG": "Erstattung einer Ausgabe",
    "ANLAGE": "Anschaffung eines Anlageguts",
    "AKTIV": "gilt",
    "AUSGELASSEN": "bewusst nicht übernommen",
    "ERSETZT": "durch die in WISO geänderte Fassung ersetzt",
    "GELOESCHT": "in WISO gelöscht",
    "ANLAGEGUT": "Anlagegut",
    "KONTO": "Konto ohne Kategorie",
    "ZAHLER": "Zahler ohne Einstufung",
}

#: Werte, die nur in EINER Spalte eine Bedeutung haben und deshalb nicht in
#: den gemeinsamen Topf gehoeren: ein "E" ueberall ersetzt, traefe auch einen
#: Kunden oder eine Leistung, die zufaellig so heisst. Die Steuerkategorie ist
#: der Code nach UNTDID 5305 aus der E-Rechnung; roh stand dort nur "E".
SPALTEN_KLARTEXT = {
    "steuerkategorie": {
        "E": "steuerbefreit (Kleinunternehmer)",
        "S": "Normalsatz",
        "AA": "ermäßigter Satz",
        "Z": "Nullsatz",
        "AE": "Reverse Charge (Steuerschuldnerschaft des Leistungsempfängers)",
        "K": "innergemeinschaftliche Lieferung",
        "G": "Ausfuhr (Drittland)",
        "O": "nicht steuerbar",
    },
    # Woher der Kurs eines Fremdwaehrungsbelegs kommt.
    "kurs_quelle": {
        "EZB": "EZB-Referenzkurs",
        "MANUELL": "Kurs von Hand",
    },
    # Die Rundungsregel, am Betrieb, am Kunden und eingefroren am Entwurf.
    # Ausdruecklich "je Position": gerundet wird die Zeitsumme einer
    # Rechnungsposition, nie der einzelne Eintrag.
    "rundung_modus": {
        "AUFWAERTS": "aufwärts auf den vollen Takt, je Position",
        "KAUFMAENNISCH": "kaufmännisch auf den vollen Takt, je Position",
        "ABWAERTS": "abwärts auf den vollen Takt, je Position",
        "KEINE": "minutengenau (keine Rundung)",
    },
    # Steuerruecklage: ein Kunde namens "Zusammen" bleibt so, wie er heisst.
    "veranlagung": {
        "EINZEL": "Einzelveranlagung",
        "ZUSAMMEN": "Zusammenveranlagung",
    },
    # Vorauszahlungen: an wen die Rate geht.
    "empfaenger": {
        "FINANZAMT": "Finanzamt (Einkommensteuer, Soli, Kirchensteuer)",
        "GEMEINDE": "Gemeinde (Gewerbesteuer)",
    },
    # Woher eine Einnahme ohne Rechnung kommt. "MANUELL" heisst hier etwas
    # anderes als bei der Kursquelle -- deshalb nicht im gemeinsamen Topf.
    "herkunft": {
        "MANUELL": "von Hand erfasst",
        "WISO": "aus WISO Steuer übernommen",
    },
}

#: Woerter, die die automatische Ueberschrift nicht richtig hinbekommt.
#: Der grosse Teil davon sind Umlaute: die Spaltennamen sind bewusst in ASCII
#: geschrieben, damit sie sich ueberall tippen lassen -- in der Ueberschrift
#: gehoeren sie aber zurueck.
_WORTE = {
    "ust": "USt", "idnr": "IdNr.", "afa": "AfA", "euer": "EÜR", "ak": "AK",
    "pdf": "PDF", "id": "ID", "sha256": "SHA-256", "plz": "PLZ", "iban": "IBAN",
    "bic": "BIC", "nr": "Nr.", "utc": "UTC", "gwg": "GWG", "mime": "MIME",
    "min": "Minuten", "fuer": "für", "default": "Vorbelegung", "json": "(JSON)",
    # ASCII-Schreibweisen zurueck in deutsche Schrift
    "strasse": "Straße", "faellig": "fällig", "geaendert": "geändert",
    "geprueft": "geprüft", "gueltig": "gültig", "waehrung": "Währung",
    "begruendung": "Begründung", "rueckkehr": "Rückkehr",
    "schluessel": "Schlüssel", "erloes": "Erlös", "email": "E-Mail",
    "fremdwaehrung": "Fremdwährung",
    "pruefsumme": "Prüfsumme",
}

#: Ueberschriften, bei denen die Automatik danebenliegt oder eine Einheit fehlt.
KOPF_AUSNAHMEN = {
    "dauer_minuten": "Dauer (Minuten)",
    "zahlungsziel_tage": "Zahlungsziel (Tage)",
    "kundennummer_start": "Erste neue Kundennummer",
    "hinweis_19_text": "§ 19-Hinweis",
    "hinweis_rc_text": "Reverse-Charge-Hinweis",
    "rundung_intervall_min": "Rundungstakt (Minuten)",
    "rundung_modus": "Rundungsregel",
    "rundung_hinweis": "Rundungshinweis auf der Rechnung",
    "rundung_grundlage": "Rundung vereinbart in",
    "beginn_utc": "Beginn (UTC)",
    "ende_utc": "Ende (UTC)",
    "leistungszeitraum_default": "Leistungszeitraum zeigen (Vorbelegung)",
    "nullrechnung_zulassen": "Nullrechnung zulassen",
    "backup_pfad": "Sicherungsordner",
    "backup_zweitziel": "Zweites Sicherungsziel",
    # Seit es Ordner je Jahr gibt (eigenes Blatt), ist dieser der fuer alle Jahre.
    "belegordner_pfad": "Eingangsordner (alle Jahre)",
    "belegordner_automatisch": "Sicher Erkanntes automatisch buchen",
    "belege_bezahlt_vorgabe": "Eingangsbelege in der Regel schon bezahlt",
    "logo_pfad": "Logo-Datei",
    "standardsatz_cent": "Standard-Stundensatz",
    "satz_cent": "Stundensatz",
    "preis_cent": "Preis",
    "gruppierung_default": "Gruppierung (Vorbelegung)",
    "zustimmung_am": "Zustimmung zum PDF-Versand am",
    "zustimmung_nachweis": "Nachweis der Zustimmung",
    "unternehmer_nachweis": "Nachweis der Unternehmereigenschaft",
    "beschreibung_vorlage": "Beschreibungsvorlage",
    "reverse_charge_default": "Reverse Charge (Vorbelegung)",
    "waehrung_default": "Währung (Vorbelegung)",
    "belegnummer_extern": "Belegnummer des Lieferanten",
    "ist_eigenbeleg": "Eigenbeleg",
    "ist_anlagegut": "Anlagegut",
    "original_name": "Ursprünglicher Dateiname",
    "nutzungsdauer_jahre": "Nutzungsdauer (Jahre)",
    "abgang_erloes_cent": "Veräußerungserlös",
    "abgang_grund": "Abgangsgrund",
    "anschaffung_am": "Angeschafft am",
    "storno_grund": "Stornogrund",
    "nachlass_grund": "Nachlassgrund",
    "pos_nr": "Pos.",
    "pdf_pfad": "PDF-Datei",
    "pdf_sha256": "PDF-Prüfsumme",
    "start_zeit": "Beginn",
    "end_zeit": "Ende",
    "pauschal_cent": "Pauschalbetrag",
    "abgeschrieben_grund": "Abschreibungsgrund",
    "wert_alt": "Wert vorher",
    "wert_neu": "Wert nachher",
    "kennzahl": "EÜR-Kennzahl",
    "kleinunternehmer_idnr": "Kleinunternehmer-IdNr.",
    "wirtschafts_idnr": "Wirtschafts-IdNr.",
    "storno_von_id": "Storno zu Rechnung-ID",
    "freigegeben_aus_rechnung_id": "Freigegeben aus Rechnung-ID",
    "ersetzt_rechnung_id": "Ersetzt Rechnung-ID",
    "reverse_charge": "Reverse Charge",
    "abwesend_stunden": "Abwesenheit (Stunden)",
    "menge_tausendstel": "Menge",
    "ust_satz_promille": "USt-Satz",
    "steuersatz_promille": "Steuersatz",
    "anteil_abziehbar_promille": "abziehbarer Anteil",
    "aufteilungsquote": "betrieblicher Anteil",
    "quote_default": "betrieblicher Anteil (Vorbelegung)",
    "satz_cent_snapshot": "Stundensatz (Schnappschuss)",
    "nummer_seq": "Nummer (Zähler)",
    "nummer_jahr": "Nummer (Jahr)",
    "naechste_seq": "nächster Zähler",
    "reset_jaehrlich": "Zähler jährlich zurücksetzen",
    "kunde_snapshot_json": "Kundendaten bei Festschreibung (JSON)",
    "absender_snapshot_json": "Absenderdaten bei Festschreibung (JSON)",
    "zeitpunkt_utc": "Zeitpunkt (UTC)",
    "groesse": "Größe (Bytes)",
    "netto_fuer_grenzwert_cent": "Netto für die GWG-Grenze",
    "betrag_betrieblich_cent": "Betrag betrieblich",
    "kennzahl_nicht_abziehbar": "Kennzahl (nicht abziehbar)",
    "grenze_netto_cent": "Grenze netto",
    "kundennummer_format": "Kundennummer-Format",
    "standard_kategorie_id": "Standardkategorie-ID",
    "brutto_lohn_cent": "Bruttogehalt",
    "partner_brutto_cent": "Bruttogehalt Partner/in",
    "zve_ohne_cent": "Zu versteuerndes Einkommen ohne Gewerbe",
    "kirchensteuer_prozent": "Kirchensteuer (%)",
    "satz_manuell_promille": "Eigener Steuersatz",
    "gewinn_erwartet_cent": "Erwarteter Jahresgewinn",
    "hebesatz_prozent": "Gewerbesteuer-Hebesatz (%)",
    "tarif_entwurf": "Mit Tarifentwurf gerechnet",
    "vorauszahlungen_cent": "Weitere Vorauszahlungen (ohne Termin)",
    "empfaenger": "Empfänger",
    "gezahlt": "Gezahlt (von Hand gesetzt; leer = ab Fälligkeit)",
    "kurs": "Kurs (Einheiten je Euro)",
    "kurs_datum": "Kursdatum",
    "kurs_quelle": "Kursquelle",
    "kurs_abruf_utc": "Kurstabelle abgerufen (UTC)",
    "kurs_tabelle_sha256": "Kurstabelle SHA-256",
    "betrag_umgerechnet_cent": "Betrag umgerechnet",
    "betrag_karte_cent": "Betrag laut Kreditkarte",
    # Die Loeschmarkierung (Migration 25): leer heisst gueltig.
    "geloescht_am": "Gelöscht am",
    "geloescht_grund": "Löschgrund",
    # Import aus WISO Steuer (Migration 26).
    "verknuepft": "Mit vorhandenem Eintrag verknüpft",
    "ignoriert_pruefsumme": "Ignorierte Fassung (Prüfsumme)",
    "behalten_am": "In WISO gelöscht, behalten am",
    "anzahl_json": "Anzahl je Gruppe (JSON)",
    "inhalt_json": "Inhalt der Buchung (JSON)",
    "angaben_json": "Bekannte Angaben (JSON)",
    "zufluss_am": "Zugeflossen am",
}

#: Abgeleitete Spalten, die hinter einer Spalte einer Tabelle stehen:
#: Tabelle -> {Spalte: (Ueberschrift, SQL-Ausdruck)}. Die Quelle des
#: Euro-Betrags eines Fremdwaehrungsbelegs wird nicht gespeichert (sie folgt
#: aus Kartenbetrag und Kursquelle), gehoert in der Tabelle aber daneben --
#: ohne sie muesste der Leser die Regel kennen.
ZUSATZSPALTEN = {
    # Leer heisst am Kunden "wie der Betrieb" -- das steht daneben, statt
    # dass der Leser es wissen muss.
    "kunde": {
        "rundung_modus": (
            "Rundungsregel gilt",
            "CASE WHEN t.rundung_modus IS NULL THEN 'wie Betrieb'"
            " ELSE 'eigene Regel des Kunden' END",
        ),
    },
    # Die Abschnitte in Ortszeit daneben: wer in Deutschland liest, rechnet
    # 07:02 UTC nicht im Kopf in 09:02 um.
    "zeit_abschnitt": {
        "beginn_utc": ("Beginn (Ortszeit)", "datetime(t.beginn_utc, 'localtime')"),
        "ende_utc": ("Ende (Ortszeit)", "datetime(t.ende_utc, 'localtime')"),
    },
    # Der Termin einer Rate laut Gesetz (§ 37 Abs. 1 EStG, § 19 Abs. 1 GewStG).
    # Faellt er auf ein Wochenende oder einen Feiertag, ist die Rate am
    # naechsten Werktag faellig (§ 108 Abs. 3 AO) -- das rechnet die Seite.
    "steuer_vorauszahlung": {
        "quartal": (
            "Termin laut Gesetz",
            "CASE t.empfaenger WHEN 'FINANZAMT'"
            " THEN printf('10.%02d.%d', t.quartal * 3, t.jahr)"
            " ELSE printf('15.%02d.%d', t.quartal * 3 - 1, t.jahr) END",
        ),
    },
    "beleg": {
        "betrag_karte_cent": (
            "Quelle des Euro-Betrags",
            "CASE WHEN t.waehrung = 'EUR' OR t.betrag_fremdwaehrung_cent IS NULL THEN NULL"
            " WHEN t.betrag_karte_cent IS NOT NULL THEN 'Kreditkartenbetrag'"
            " WHEN t.kurs_quelle = 'EZB' THEN 'EZB-Referenzkurs'"
            " WHEN t.kurs_quelle = 'MANUELL' THEN 'Kurs von Hand'"
            " ELSE 'Kontoauszug oder Kreditkarte (vor Version 19)' END",
        ),
    },
}

#: Spalten, die als Ja/Nein ausgegeben werden. Die Namen folgen keinem Muster,
#: das sich zuverlaessig erraten liesse -- "inland" ist ein Flag, "land" nicht.
_JA_NEIN = {
    "aktiv", "geprueft", "einzelnachweis", "leistungszeitraum_zeigen",
    "leistungszeitraum_default", "nullrechnung_zulassen", "reverse_charge",
    "reverse_charge_default", "ist_eigenbeleg", "ist_anlagegut", "inland",
    "reset_jaehrlich", "zahlungsziel_zeigen", "belegordner_automatisch", "tarif_entwurf",
    "gezahlt", "belege_bezahlt_vorgabe", "verknuepft",
}

_KOPF_FUELLUNG = PatternFill("solid", fgColor="E8E8E0")


def _ueberschrift(spalte: str) -> str:
    """Aus einem Spaltennamen eine Ueberschrift, die man ohne Erklaerung liest.

    >>> _ueberschrift("betrag_brutto_cent")
    'Betrag brutto'
    >>> _ueberschrift("ust_idnr")
    'USt-IdNr.'
    >>> _ueberschrift("dauer_minuten")
    'Dauer (Minuten)'
    """
    if spalte in KOPF_AUSNAHMEN:
        return KOPF_AUSNAHMEN[spalte]
    if spalte == "ust_idnr":
        return "USt-IdNr."
    rumpf = spalte
    for endung in ("_cent", "_tausendstel", "_promille"):
        if rumpf.endswith(endung):
            rumpf = rumpf[: -len(endung)]
    # Ein Fremdschluessel heisst "Kunde-ID", nicht "Kunde ID": der Bindestrich
    # macht sichtbar, dass die Spalte zum Nachbarn mit dem Klartext gehoert.
    anhang = ""
    if rumpf.endswith("_id"):
        rumpf, anhang = rumpf[:-3], "-ID"
    worte = [_WORTE.get(teil, teil) for teil in rumpf.split("_")]
    kopf = " ".join(worte) + anhang
    return kopf[:1].upper() + kopf[1:]


def _art(spalte: str) -> str:
    """Welche Umrechnung und Formatierung eine Spalte braucht.

    Das Schema haelt seine Konventionen ohne Ausnahme durch -- Geld immer
    ``_cent``, Dauer immer ``_minuten``, Menge immer ``_tausendstel`` --, und
    deshalb traegt der Name die Information schon. Abgeleitet wird sie hier
    statt in einer Liste von 250 Spalten, die beim naechsten Feld veraltet.

    >>> _art("betrag_brutto_cent"), _art("belegdatum"), _art("aktiv")
    ('euro', 'datum', 'ja_nein')
    """
    if spalte in _JA_NEIN:
        return "ja_nein"
    # Der Betrag in Fremdwaehrung ist kein Euro-Betrag: mit "€" formatiert
    # stuenden 1.234,56 USD als 1.234,56 € in der Tabelle.
    if spalte == "betrag_fremdwaehrung_cent":
        return "fremdbetrag"
    if spalte == "kurs":
        return "kurs"
    if spalte.endswith("_cent"):
        return "euro"
    if spalte.endswith("_promille"):
        return "prozent"
    # aufteilungsquote und quote_default fuehren GANZE Prozent (0..100), nicht
    # Promille. Durch 1000 geteilt stuenden 100 % als 10,0 % in der Tabelle.
    if spalte in ("aufteilungsquote", "quote_default"):
        return "prozent_ganz"
    if spalte.endswith("_tausendstel"):
        return "menge"
    # angelegt_am (Kunde, Lieferant) fuehrt nur ein Datum: als Zeitstempel
    # formatiert stuende dort "19.10.2026 00:00".
    if spalte.endswith("_utc") or spalte in ("zeitpunkt", "erfasst_am", "importiert_am",
                                             "erstellt_am", "geaendert_am",
                                             "festgeschrieben_am", "geprueft_am",
                                             "migriert_am", "gelernt_am", "bestaetigt_am",
                                             "geloescht_am", "erledigt_am",
                                             "behalten_am"):
        return "zeitstempel"
    if spalte.endswith("_am") or spalte.endswith("datum") or spalte in (
        "gueltig_ab", "gueltig_bis", "leistung_von", "leistung_bis", "beginn_am"
    ):
        return "datum"
    return "text"


#: Zahlenformate je Art. ``€`` als Literal, damit die Datei auch ohne
#: Gebietsschema richtig anzeigt.
FORMATE = {
    "euro": '#,##0.00\\ "€"',
    "fremdbetrag": "#,##0.00",
    # "Standard": Excel zeigt den Kurs mit genau den Stellen, die er hat.
    "kurs": "General",
    "menge": "#,##0.000",
    "prozent": "0.0 %",
    "prozent_ganz": "0 %",
    "datum": "DD.MM.YYYY",
    "zeitstempel": "DD.MM.YYYY HH:MM",
}


def _wert(roh, art: str):
    """Rechnet einen Datenbankwert in das um, was in der Zelle stehen soll."""
    if roh is None:
        return None
    if art in ("euro", "fremdbetrag"):
        return int(roh) / 100
    if art == "kurs":
        try:
            return float(roh)
        except (TypeError, ValueError):
            return roh
    if art == "menge":
        return int(roh) / 1000
    if art == "prozent":
        return int(roh) / 1000
    if art == "prozent_ganz":
        return int(roh) / 100
    if art == "ja_nein":
        return "ja" if roh else "nein"
    if art == "datum":
        try:
            return _dt.date.fromisoformat(str(roh)[:10])
        except ValueError:
            return roh
    if art == "zeitstempel":
        try:
            return _dt.datetime.fromisoformat(str(roh).replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            return roh
    if art.startswith("klartext:"):
        return SPALTEN_KLARTEXT[art.removeprefix("klartext:")].get(str(roh), roh)
    if isinstance(roh, str):
        return KLARTEXT.get(roh, roh)
    return roh


def _text(roh: str) -> str:
    """Eine Zeichenkette, wie sie in eine Zelle darf.

    openpyxl verweigert Zeichen aus dem XML-1.0-Sperrbereich (\x00-\x08,
    \x0b, \x0c, \x0e-\x1f) -- und ein Seitenvorschub aus einer
    PDF-Extraktion im Lieferantennamen liess damit den GANZEN Export
    scheitern, englische Meldung inklusive. Die Zeichen tragen keine
    Information; sie werden entfernt.

    >>> _text("Hetzner\x0cOnline")
    'HetznerOnline'
    """
    return ILLEGAL_CHARACTERS_RE.sub("", roh)


def _anhaengen(blatt, werte: list) -> None:
    """Haengt eine Zeile an -- jede Zeichenkette als TEXT, nie als Formel.

    openpyxl haelt jeden String mit fuehrendem "=" fuer eine Formel. Eine
    Beschreibung "=> siehe Mail" oder ein Kundenname "=HYPERLINK(...)" stuende
    sonst als Formel in der Datei: Excel zeigt #NAME? oder fuehrt sie aus.
    Der Zweck der Datei -- der Bestand lesbar ohne dieses Werkzeug -- waere
    fuer genau diese Zellen verfehlt. Deshalb nach dem Anhaengen den Zelltyp
    auf Text festnageln; der Wert bleibt, wie er in der Datenbank steht.
    """
    blatt.append([_text(w) if isinstance(w, str) else w for w in werte])
    for zelle in blatt[blatt.max_row]:
        if isinstance(zelle.value, str) and zelle.data_type != "s":
            zelle.data_type = "s"


def _spalten(conn: sqlite3.Connection, tabelle: str) -> list[tuple[str, str, str]]:
    """Die Spalten eines Blattes als (Ueberschrift, SQL-Ausdruck, Art).

    Hinter jedem Fremdschluessel steht der Klartext -- unmittelbar daneben und
    nicht am Zeilenende, weil man beides zusammen liest.
    """
    gefunden: list[tuple[str, str, str]] = []
    eigene = TABELLEN_VERKNUEPFUNGEN.get(tabelle, {})
    for zeile in conn.execute(f'PRAGMA table_info("{tabelle}")'):
        name = zeile[1]
        kopf = TABELLEN_KOPF.get((tabelle, name)) or _ueberschrift(name)
        art = _art(name)
        if name in SPALTEN_KLARTEXT:
            art = f"klartext:{name}"
        # Zeitstempel stehen in UTC (db.jetzt_utc). Ohne den Hinweis liest
        # jemand in Deutschland 04:29 fuer eine Sicherung von 06:29.
        if art == "zeitstempel" and "(UTC)" not in kopf:
            kopf += " (UTC)"
        gefunden.append((kopf, f't."{name}"', art))
        if name in eigene:
            kopf, ausdruck = eigene[name]
            gefunden.append((kopf, ausdruck, "text"))
        elif name in VERKNUEPFUNGEN:
            kopf, ausdruck = VERKNUEPFUNGEN[name]
            gefunden.append((kopf, ausdruck, "text"))
        if name in ZUSATZSPALTEN.get(tabelle, {}):
            kopf, ausdruck = ZUSATZSPALTEN[tabelle][name]
            gefunden.append((kopf, ausdruck, "text"))
    return gefunden


def _tabellen(conn: sqlite3.Connection) -> list[str]:
    """Die zu exportierenden Tabellen in der Reihenfolge von BLATTFOLGE."""
    vorhanden = {
        z[0]
        for z in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    geplant = [t for t in BLATTFOLGE if t in vorhanden]
    # Eine Tabelle, die weder in der Reihenfolge noch in der Ausschlussliste
    # steht, waere stillschweigend nicht gesichert. Sie kommt deshalb hinten
    # dran -- unformatiert, aber vorhanden.
    rest = sorted(vorhanden - set(BLATTFOLGE) - set(NICHT_EXPORTIERT))
    return geplant + rest


def schreibe(conn: sqlite3.Connection, ziel: Path) -> Path:
    """Schreibt den gesamten Bestand als .xlsx.

    Gibt den Pfad zurueck. Eine vorhandene Datei wird ersetzt -- der Export
    ist eine Momentaufnahme und kein Archiv; archiviert wird im ZIP. Ersetzt
    wird ATOMAR: erst eine eigene Arbeitsdatei daneben, dann ``os.replace``.
    Wer die alte Datei gerade liest (ein Download, eine Sicherung), liest sie
    zu Ende; ein zweiter Lauf schreibt nie in eine Datei, die ein erster noch
    fuellt.
    """
    mappe = _mappe(conn)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    griff, arbeitsname = tempfile.mkstemp(
        prefix=f".{ziel.name}.", suffix=".teil", dir=ziel.parent
    )
    os.close(griff)
    arbeit = Path(arbeitsname)
    try:
        mappe.save(arbeit)
        os.replace(arbeit, ziel)
    except BaseException:
        arbeit.unlink(missing_ok=True)
        raise
    return ziel


def als_bytes(conn: sqlite3.Connection) -> bytes:
    """Der gesamte Bestand als .xlsx im Speicher -- fuer den Download.

    Jede Anfrage bekommt ihre eigene Datei. Gemeinsam genutzt wurde vorher
    ``steuer-daten.xlsx`` im Datenordner, und gleichzeitige Downloads lieferten
    eine Datei aus, die ein zweiter Lauf gerade ueberschrieb.
    """
    puffer = io.BytesIO()
    _mappe(conn).save(puffer)
    return puffer.getvalue()


def _mappe(conn: sqlite3.Connection) -> Workbook:
    """Baut die Arbeitsmappe mit allen Blaettern."""
    mappe = Workbook()
    mappe.remove(mappe.active)

    for tabelle in _tabellen(conn):
        blatt = mappe.create_sheet(BLATTNAMEN.get(tabelle, tabelle)[:31])
        spalten = _spalten(conn, tabelle)
        auswahl = ", ".join(f'{ausdruck} AS "{i}"' for i, (_, ausdruck, _) in enumerate(spalten))
        _anhaengen(blatt, [kopf for kopf, _, _ in spalten])

        for zeile in conn.execute(f'SELECT {auswahl} FROM "{tabelle}" t'):
            _anhaengen(blatt, [
                _wert(zeile[i], art) for i, (_, _, art) in enumerate(spalten)
            ])

        _formatiere(blatt, spalten)

    _euer_blaetter(mappe, conn)
    _luecken_blatt(mappe, conn)
    _deckblatt(mappe, conn)
    return mappe


def _luecken_blatt(mappe: Workbook, conn: sqlite3.Connection) -> None:
    """Verbrauchte Rechnungsnummern ohne gueltige Rechnung, mit Grund.

    Eine Luecke im Nummernkreis ist in einer Betriebspruefung
    erklaerungsbeduerftig; die Erklaerung gehoert deshalb in dieselbe Datei
    wie die Rechnungen -- nicht nur in die Anwendung.
    """
    from app.logik import rechnungsablauf

    blatt = mappe.create_sheet("Nummernlücken")
    _anhaengen(blatt, ["Jahr", "Nummer", "Zähler", "Grund", "Zurückgenommen am (UTC)",
                       "Danach"])
    luecken = rechnungsablauf.nummernluecken(conn)
    if not luecken:
        _anhaengen(blatt, [None, "— keine Lücke —", None,
                           "Jede vergebene Nummer gehört zu einer gültigen Rechnung.", None, ""])
    for luecke in luecken:
        _anhaengen(blatt, [
            luecke.jahr, luecke.nummer, luecke.seq,
            luecke.grund or "ohne Vermerk im Protokoll",
            _wert(luecke.vermerkt_am, "zeitstempel"),
            {"ENTWURF": "wieder Entwurf", "GELOESCHT": "gelöscht"}.get(luecke.variante or "", ""),
        ])
    _euer_formatieren(blatt, betragsspalte="C")
    for zelle in blatt["C"][1:]:
        zelle.number_format = "0"
    for zelle in blatt["E"][1:]:
        zelle.number_format = FORMATE["zeitstempel"]


def _euer_blaetter(mappe: Workbook, conn: sqlite3.Connection) -> None:
    """Die ausgewertete EUeR und das Buchungsjournal.

    Die uebrigen Blaetter geben die Tabellen wieder; diese beiden geben die
    ANTWORT. Ohne sie muesste man die Zahlen fuer die Anlage EUeR am
    Jahresende aus Listen zusammenaddieren, und genau dort entstehen Fehler.
    """
    from app.logik import euer

    jahre = euer.jahre(conn)

    blatt = mappe.create_sheet("EÜR", 0)
    _anhaengen(blatt, ["Jahr", "Bereich", "Kennzahl", "Bezeichnung", "Betrag", "Hinweis"])
    if not jahre:
        # Die Blaetter entstehen auch leer. Das Deckblatt nennt sie, und ein
        # versprochenes Blatt, das fehlt, laesst den Leser suchen -- eine Zeile,
        # die den Grund nennt, beantwortet die Frage sofort.
        _anhaengen(blatt, [
            None, "— keine Zahlung erfasst —", None,
            "Die EÜR rechnet nach Zu- und Abfluss (§ 11 EStG). Festgeschriebene "
            "Rechnungen ohne Zahlungseingang erscheinen hier noch nicht.",
            None, "",
        ])
    for jahr in jahre:
        a = euer.auswertung(conn, jahr)
        for bereich, zeilen in (("Betriebseinnahmen", a.einnahmen),
                                ("Betriebsausgaben", a.ausgaben)):
            for zeile in zeilen:
                _anhaengen(blatt, [
                    jahr, bereich, zeile.kennzahl, zeile.bezeichnung,
                    zeile.betrag_cent / 100,
                    "nachrichtlich — nicht in der Summe" if zeile.nachrichtlich else "",
                ])
        _anhaengen(blatt, [jahr, "Summe Einnahmen", None, "", a.einnahmen_cent / 100, ""])
        _anhaengen(blatt, [jahr, "Summe Ausgaben", None, "", a.ausgaben_cent / 100, ""])
        _anhaengen(blatt, [jahr, "Gewinn", None, "", a.gewinn_cent / 100, ""])
    _euer_formatieren(blatt, betragsspalte="E")

    blatt = mappe.create_sheet("Buchungen", 1)
    _anhaengen(blatt, [
        "Datum", "Richtung", "Betrag", "davon Betriebsausgabe", "Beleg", "Gegenstelle",
        "Zahlungsart", "EÜR-Kennzahl", "Kategorie", "Text", "Sachkonto",
    ])
    if not jahre:
        _anhaengen(blatt, [
            None, "— keine Zahlung erfasst —", None, None, None, None, None, None,
            "Gezählt wird der Tag, an dem das Geld fließt.", "", "",
        ])
    for zeile in euer.buchungen(conn):
        _anhaengen(blatt, [
            _wert(zeile["datum"], "datum"), zeile["richtung"],
            zeile["betrag_cent"] / 100,
            # Der Geldfluss links, die EUeR-wirksame Zahl daneben. Ohne diese
            # Spalte landete der volle Zahlbetrag auf der abziehbaren
            # Kennzahl -- Nutzungsquote und Abzugsfaehigkeit uebergangen.
            (zeile["betriebsausgabe_cent"] / 100
             if zeile["betriebsausgabe_cent"] is not None else None),
            zeile["beleg"], zeile["gegenstelle"],
            KLARTEXT.get(zeile["zahlungsart"] or "", zeile["zahlungsart"]),
            zeile["kennzahl"], zeile["kategorie"], zeile["text"],
            "",   # bewusst leer, siehe euer.buchungen()
        ])
    _euer_formatieren(blatt, betragsspalte="C", datumsspalte="A")
    for zelle in blatt["D"][1:]:
        zelle.number_format = FORMATE["euro"]


def _euer_formatieren(blatt, *, betragsspalte: str, datumsspalte: str | None = None) -> None:
    for zelle in blatt[1]:
        zelle.font = Font(bold=True)
        zelle.fill = _KOPF_FUELLUNG
        zelle.alignment = Alignment(vertical="top", wrap_text=True)
    blatt.freeze_panes = "A2"
    for zelle in blatt[betragsspalte][1:]:
        zelle.number_format = FORMATE["euro"]
    if datumsspalte:
        for zelle in blatt[datumsspalte][1:]:
            zelle.number_format = FORMATE["datum"]
    for nr in range(1, blatt.max_column + 1):
        buchstabe = get_column_letter(nr)
        laenge = max(
            [10] + [len(str(z.value)) for z in blatt[buchstabe][:60] if z.value]
        )
        blatt.column_dimensions[buchstabe].width = min(laenge + 2, 42)


def _formatiere(blatt, spalten: list[tuple[str, str, str]]) -> None:
    """Kopfzeile fixieren, Zahlenformate setzen, Breiten schaetzen."""
    for zelle in blatt[1]:
        zelle.font = Font(bold=True)
        zelle.fill = _KOPF_FUELLUNG
        zelle.alignment = Alignment(vertical="top", wrap_text=True)
    blatt.freeze_panes = "A2"
    if blatt.max_row > 1:
        blatt.auto_filter.ref = (
            f"A1:{get_column_letter(len(spalten))}{blatt.max_row}"
        )

    for nr, (kopf, _, art) in enumerate(spalten, start=1):
        buchstabe = get_column_letter(nr)
        if art in FORMATE:
            for zelle in blatt[buchstabe][1:]:
                zelle.number_format = FORMATE[art]
        # Breite nach dem laengsten Wert, aber gedeckelt: eine JSON-Spalte
        # draengte sonst alles andere aus dem Bild.
        laenge = max(
            [len(kopf)] + [len(str(z.value)) for z in blatt[buchstabe][1:51] if z.value]
        )
        blatt.column_dimensions[buchstabe].width = min(max(10, laenge + 2), 42)


def _deckblatt(mappe: Workbook, conn: sqlite3.Connection) -> None:
    """Ein Blatt, das erklaert, was man vor sich hat.

    Ohne das steht beim Oeffnen in fuenf Jahren eine Mappe mit neunzehn
    Reitern da und niemand weiss, woher sie kommt und was fehlt.
    """
    blatt = mappe.create_sheet("Lies mich", 0)
    betrieb = conn.execute("SELECT * FROM betrieb WHERE id = 1").fetchone()
    zeilen = [
        ("Datenausgabe des Steuer-Werkzeugs", ""),
        ("", ""),
        ("Betrieb", betrieb["name"] if betrieb else "(nicht eingerichtet)"),
        ("Erzeugt am", _dt.datetime.now().strftime("%d.%m.%Y %H:%M")),
        ("", ""),
        ("Beträge", "Euro-Zahlen, summierbar. Intern wird in Cent gerechnet."),
        ("Datumsangaben", "echte Datumswerte, nicht Text."),
        ("Fremdschlüssel", "jeweils mit Klartextspalte daneben."),
        ("Fremdwährung", "Blatt „Belege\u201c: „Betrag brutto\u201c ist der Euro-Betrag der "
                         "EÜR. Daneben Währung, Betrag in Fremdwährung, Kurs (1 € = Kurs "
                         "Einheiten), Kursdatum, Kursquelle, umgerechneter Betrag, Betrag laut "
                         "Kreditkarte und die Quelle des Euro-Betrags; beim EZB-Kurs dazu "
                         "Abrufzeit und SHA-256 der Kurstabelle."),
        ("Einnahmen ohne Rechnung", "Affiliate-, Werbe- und Plattformerlöse, "
                                    "Erstattungen und Anlagenverkäufe, zu denen es keine "
                                    "eigene Rechnung gibt — Blatt „Einnahmen ohne Rechnung“. "
                                    "Sie zählen in „EÜR“ und „Buchungen“; in den "
                                    "§ 19-Umsatz nur die mit Inlandskategorie."),
        ("Import aus WISO Steuer", "Blatt „WISO-Buchungen“: was aus jeder übernommenen "
                                   "Buchung wurde (Beleg, Einnahme, verknüpfter Eintrag), "
                                   "Blatt „WISO-Importe“: Datei, Prüfsumme und Anzahl je "
                                   "Gruppe. Die Dateien selbst liegen im Datenordner unter "
                                   "importe/wiso/."),
        ("Gelöschte Belege", "stehen in den Blättern „Belege“, „Belegdateien“, "
                             "„Zahlungen“ und „Anlagegüter“ weiter — mit „Gelöscht "
                             "am“ und „Löschgrund“. In den Blättern „EÜR“ und "
                             "„Buchungen“ zählen sie nicht."),
        ("", ""),
        ("Blatt „EÜR\u201c", "Die ausgewertete Einnahmenüberschussrechnung je Jahr "
                            "und Kennzahl der Anlage EÜR."),
        ("Blatt „Buchungen\u201c", "Jeder Geldfluss als eine Zeile. Die Spalte "
                                  "Sachkonto ist leer: einen Kontenrahmen rät "
                                  "dieses Werkzeug nicht."),
        ("Blatt „Zeitabschnitte“", "Die Arbeitsphasen eines Timers, sekundengenau "
                                       "in UTC und in Ortszeit. Pausen sind die Lücken "
                                       "dazwischen; die Dauer im Blatt „Zeiten“ ist "
                                       "ihre Summe. Nachträglich erfasste Zeiten haben "
                                       "keine Abschnitte."),
        ("Blatt „Nummernlücken\u201c", "Verbrauchte Rechnungsnummern ohne gültige "
                                       "Rechnung, mit Grund und Zeitpunkt aus dem Journal "
                                       "(zurückgenommene, nie versendete Rechnungen)."),
        ("", ""),
        ("Nicht enthalten", ""),
    ]
    for tabelle, grund in NICHT_EXPORTIERT.items():
        zeilen.append((tabelle, grund))
    zeilen += [
        ("", ""),
        ("Vollständig ist nur das Sicherungsarchiv",
         "Diese Datei enthält die Daten, nicht die Belege und nicht die "
         "Rechnungs-PDF. Die liegen im ZIP der Sicherung."),
    ]
    for links, rechts in zeilen:
        _anhaengen(blatt, [links, rechts])
    for zeile in blatt.iter_rows(min_col=1, max_col=1, min_row=2):
        zeile[0].font = Font(bold=True)
    blatt["A1"].font = Font(bold=True, size=14)
    blatt.column_dimensions["A"].width = 38
    blatt.column_dimensions["B"].width = 80
    for zeile in blatt.iter_rows(min_col=2, max_col=2):
        zeile[0].alignment = Alignment(vertical="top", wrap_text=True)

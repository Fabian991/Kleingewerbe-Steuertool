-- Steuer-Tool, Schema Phase 1 (Ausgangsrechnungen)
--
-- Konventionen, die im ganzen Schema gelten:
--   Geld   immer INTEGER in Cent          (keine Floats, nie)
--   Dauer  immer INTEGER in Minuten
--   Menge  immer INTEGER in Tausendsteln  (8750 = 8,75 Stunden)
--   Datum  immer TEXT 'YYYY-MM-DD', Zeitstempel TEXT ISO-8601 UTC
--
-- Statusmodell: gespeichert wird nur, was nicht ableitbar ist. 'Aufgeschrieben',
-- 'In Rechnung gestellt' und 'Bezahlt' sind KEINE Spalten, sondern ergeben sich
-- aus rechnung_id und dem Zahlungsstand. Ein zweiter gespeicherter Status
-- liefe unweigerlich gegen den ersten.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- Stammdaten

CREATE TABLE IF NOT EXISTS betrieb (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    name                   TEXT    NOT NULL,
    inhaber                TEXT,
    strasse                TEXT    NOT NULL,
    plz                    TEXT    NOT NULL,
    ort                    TEXT    NOT NULL,
    land                   TEXT    NOT NULL DEFAULT 'DE',
    email                  TEXT,
    telefon                TEXT,
    -- § 34a Satz 1 Nr. 2 UStDV: eine dieser drei Kennungen ist Pflichtangabe
    steuernummer           TEXT,
    ust_idnr               TEXT,
    kleinunternehmer_idnr  TEXT,
    wirtschafts_idnr       TEXT,                      -- Anlage EUeR Kennzahl 101
    iban                   TEXT,
    bic                    TEXT,
    institut               TEXT,
    logo_pfad              TEXT,
    standardsatz_cent      INTEGER NOT NULL DEFAULT 0,
    zahlungsziel_tage      INTEGER NOT NULL DEFAULT 14,
    -- Startwert der Kundennummern beim Umstieg. Wer schon Kunden ausserhalb des
    -- Werkzeugs gefuehrt hat, darf nicht bei K-0001 wieder anfangen: die Nummer
    -- steht auf seinen bisherigen Rechnungen, und zwei Kunden mit derselben
    -- Nummer sind in der Buchfuehrung nicht auseinanderzuhalten.
    kundennummer_start     INTEGER NOT NULL DEFAULT 1 CHECK (kundennummer_start > 0),
    -- Das Format der Kundennummer. Gewachsene Nummernkreise sehen verschieden
    -- aus -- "K-0007", "07", "2026-007" --, und die Nummer steht auf den
    -- bisherigen Rechnungen. Sie umzuschreiben hiesse, die Altbelege unlesbar
    -- zu machen; also passt sich das Werkzeug an. Genau EIN Zaehlerplatzhalter
    -- im Python-Format, der Rest ist fester Text.
    kundennummer_format    TEXT    NOT NULL DEFAULT 'K-{:04d}',
    -- § 19 Abs. 1 UStG sagt seit 01.01.2025 "steuerfrei", nicht "wird nicht erhoben"
    hinweis_19_text        TEXT    NOT NULL
        DEFAULT 'Steuerfreie Leistung eines Kleinunternehmers gemäß § 19 Abs. 1 UStG.',
    -- Fuer Leistungen an Unternehmer im uebrigen Gemeinschaftsgebiet. § 14a
    -- Abs. 1 Satz 1 UStG verlangt GENAU diese Angabe, und § 19 Abs. 1 Satz 2
    -- nimmt davon nur die USt-IdNr.-Pflicht aus, nicht den Hinweis selbst.
    -- Der Wortlaut ist deshalb keine Formulierungsfrage.
    hinweis_rc_text        TEXT    NOT NULL
        DEFAULT 'Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).',
    -- Vorbelegung: angefangene 15 Minuten. Das ist die verbreitetste Regel bei
    -- Dienstleistungen und die hier gewuenschte. Gerundet wird auf die SUMME je
    -- Rechnungsposition, nie je Zeiteintrag -- drei Eintraege a 7 min sind
    -- 21 min -> 0,50 h, je Eintrag gerundet waeren es 0,75 h.
    -- ACHTUNG: eine Rundungsregel braucht eine vertragliche Grundlage (Angebot
    -- oder AGB). Sie steht deshalb sichtbar auf jeder Rechnung.
    rundung_intervall_min  INTEGER NOT NULL DEFAULT 15 CHECK (rundung_intervall_min > 0),
    rundung_modus          TEXT    NOT NULL DEFAULT 'AUFWAERTS'
        CHECK (rundung_modus IN ('AUFWAERTS', 'KAUFMAENNISCH', 'ABWAERTS', 'KEINE')),
    rundung_hinweis        TEXT,
    -- Nicht jede Rechnung nennt ein Faelligkeitsdatum; Kleingewerbe arbeiten oft
    -- mit "zahlbar sofort" oder gar nicht. Der Absatz ist deshalb abschaltbar,
    -- ohne die Vorlage anzufassen.
    zahlungsziel_zeigen    INTEGER NOT NULL DEFAULT 1
        CHECK (zahlungsziel_zeigen IN (0, 1)),
    beginn_am              TEXT,
    -- Die Nullrechnungs-Sperre liest dieses Flag, damit sie ohne Tabellen-Rebuild
    -- aufhebbar ist. Standard: gesperrt.
    nullrechnung_zulassen  INTEGER NOT NULL DEFAULT 0 CHECK (nullrechnung_zulassen IN (0, 1)),
    -- Vorbelegung fuer neue Rechnungen. Standard: zeigen, weil der Zeitraum dem
    -- Kunden das Pruefen erleichtert und nichts kostet.
    leistungszeitraum_default INTEGER NOT NULL DEFAULT 1
        CHECK (leistungszeitraum_default IN (0, 1)),
    -- Leer bedeutet "backup/ im Datenordner". Eine Sicherung auf derselben
    -- Platte schuetzt gegen Versehen, nicht gegen einen Plattendefekt -- hier
    -- gehoert daher ein externes Ziel hin.
    backup_pfad            TEXT,
    -- Zweites Ziel. Eine Sicherung auf derselben Platte wie die Daten schuetzt
    -- gegen Versehen, nicht gegen einen Plattendefekt -- und die
    -- Aufbewahrungsfrist laeuft bis zu zehn Jahre (§ 147 Abs. 3 AO: zehn fuer
    -- die Aufzeichnungen, acht fuer die Belege), also ueber mehrere Rechner.
    backup_zweitziel       TEXT,
    -- Der Eingangsordner fuer Belege, etwa ein Ordner in iCloud Drive, in den
    -- auf dem Handy Rechnungen aus der Mail abgelegt werden. Beim Start holt
    -- das Werkzeug neue Dateien dort ab (app/logik/beleg/abholen.py). NULL
    -- heisst: aus. Bewusst NICHT der Datenordner: der Eingangsordner ist ein
    -- Postfach, die Datei wird sofort in den lokalen Eingang kopiert.
    belegordner_pfad       TEXT,
    -- Sicher erkannte Rechnungen bekannter Lieferanten gleich buchen (1) oder
    -- alles in der Pruefmaske warten lassen (0).
    belegordner_automatisch INTEGER NOT NULL DEFAULT 1
        CHECK (belegordner_automatisch IN (0, 1)),
    CHECK (COALESCE(steuernummer, ust_idnr, kleinunternehmer_idnr) IS NOT NULL)
);

-- Traegt einen spaeteren Wechsel zur Regelbesteuerung ohne Migration.
CREATE TABLE IF NOT EXISTS steuerstatus_zeitraum (
    id          INTEGER PRIMARY KEY,
    gueltig_ab  TEXT NOT NULL,
    gueltig_bis TEXT,
    modus       TEXT NOT NULL CHECK (modus IN ('KU19', 'REGEL'))
);

CREATE TABLE IF NOT EXISTS kunde (
    id                  INTEGER PRIMARY KEY,
    kundennummer        TEXT    NOT NULL UNIQUE,
    name                TEXT    NOT NULL,
    rechnungsname       TEXT,
    ansprechpartner     TEXT,
    -- § 34a Satz 1 Nr. 1 UStDV: vollstaendiger Name UND vollstaendige Anschrift
    strasse             TEXT    NOT NULL,
    plz                 TEXT    NOT NULL,
    ort                 TEXT    NOT NULL,
    land                TEXT    NOT NULL DEFAULT 'DE',
    email               TEXT,
    ust_idnr            TEXT,
    typ                 TEXT    NOT NULL DEFAULT 'UNTERNEHMER'
        CHECK (typ IN ('UNTERNEHMER', 'JURISTISCHE_PERSON', 'PRIVAT')),
    -- Nur INLAND zaehlt in den § 19-Gesamtumsatz; alle drei sind EUeR-Betriebseinnahme.
    leistungsort        TEXT    NOT NULL DEFAULT 'INLAND'
        CHECK (leistungsort IN ('INLAND', 'EU_B2B_REVERSE_CHARGE', 'DRITTLAND')),
    satz_cent           INTEGER,
    zahlungsziel_tage   INTEGER,
    gruppierung_default TEXT    NOT NULL DEFAULT 'PRO_TITEL'
        CHECK (gruppierung_default IN ('SAMMEL', 'PRO_TITEL', 'PRO_TAG', 'PRO_EINTRAG')),
    -- § 14 Abs. 1 Satz 5 UStG: PDF-Versand bedarf der Zustimmung des Empfaengers
    versandart          TEXT    NOT NULL DEFAULT 'PDF_EMAIL'
        CHECK (versandart IN ('PAPIER', 'PDF_EMAIL', 'E_RECHNUNG')),
    zustimmung_am       TEXT,
    zustimmung_nachweis TEXT,
    -- § 3a Abs. 2 UStG verlagert den Leistungsort nur, wenn der Empfaenger
    -- UNTERNEHMER ist. Innerhalb der EU belegt das die USt-IdNr.; im Drittland
    -- gibt es keine, dort traegt die Beweislast der Leistende. Ohne Nachweis
    -- faellt der Ort nach § 3a Abs. 1 auf den eigenen Sitz zurueck -- der Umsatz
    -- waere im Inland steuerbar und zaehlte in den § 19-Gesamtumsatz.
    -- Hier gehoert hin, WO der Nachweis liegt (Vertrag, Unternehmerbescheinigung,
    -- Handelsregisterauszug), nicht der Nachweis selbst.
    unternehmer_nachweis TEXT,
    notiz               TEXT,
    angelegt_am         TEXT    NOT NULL,
    aktiv               INTEGER NOT NULL DEFAULT 1 CHECK (aktiv IN (0, 1))
);

-- Artikelstamm-light. Ohne konsistente Titel ist die Gruppierung "pro Titel" wertlos.
CREATE TABLE IF NOT EXISTS leistung (
    id                   INTEGER PRIMARY KEY,
    titel                TEXT    NOT NULL,
    beschreibung_vorlage TEXT,
    einheit              TEXT    NOT NULL DEFAULT 'HUR'   -- UN/ECE Rec 20, ZUGFeRD-faehig
        CHECK (einheit IN ('HUR', 'DAY', 'C62', 'LS')),
    preis_cent           INTEGER,
    aktiv                INTEGER NOT NULL DEFAULT 1 CHECK (aktiv IN (0, 1))
);

-- Genau der Fall, den der Katalog verhindern soll: 'Beratung' und 'beratung'
-- nebeneinander ergeben bei der Gruppierung "pro Titel" zwei Positionen fuer
-- dieselbe Arbeit. Die Eindeutigkeit gehoert deshalb in die Datenbank und nicht
-- in die Oberflaeche -- geschrieben wird der Katalog auch aus der Zeiterfassung.
CREATE UNIQUE INDEX IF NOT EXISTS leistung_titel_einmalig
    ON leistung (titel COLLATE NOCASE);

-- ------------------------------------------------------------- Zeiterfassung

CREATE TABLE IF NOT EXISTS zeiteintrag (
    id             INTEGER PRIMARY KEY,
    kunde_id       INTEGER NOT NULL REFERENCES kunde (id),
    titel          TEXT    NOT NULL,
    beschreibung   TEXT,
    datum          TEXT    NOT NULL,
    start_zeit     TEXT,
    end_zeit       TEXT,
    -- Fuehrende Groesse, immer UNGERUNDET. Gerundet wird erst je Rechnungsposition.
    dauer_minuten  INTEGER NOT NULL CHECK (dauer_minuten > 0),
    -- EINE Spalte fuer die kaufmaennische Absicht. Zwei getrennte Spalten
    -- (abrechenbar + abrechnungsart) ueberlappten und erlaubten die
    -- widerspruechliche Kombination "erscheint nie auf einer Rechnung" und
    -- "erscheint mit 0,00 € auf der Rechnung".
    --   STUNDEN  nach Zeit abrechnen
    --   PAUSCHAL Festpreis, Dauer fuer den Betrag irrelevant
    --   KULANZ   erscheint MIT 0,00 € auf der Rechnung, der Kunde sieht den Verzicht
    --   INTERN   erscheint nie auf einer Rechnung (Akquise, eigene Buchhaltung)
    abrechnungsmodus TEXT  NOT NULL DEFAULT 'STUNDEN'
        CHECK (abrechnungsmodus IN ('STUNDEN', 'PAUSCHAL', 'KULANZ', 'INTERN')),
    grund          TEXT,                 -- Pflicht bei KULANZ und INTERN
    pauschal_cent  INTEGER,
    -- Auch beim Wechsel auf KULANZ und beim Abschreiben setzen: sonst ist der
    -- verschenkte Wert nach der naechsten Satzerhoehung nicht mehr rekonstruierbar.
    satz_cent_snapshot INTEGER,
    -- Belegt ab Entwurfserstellung, damit kein zweiter Entwurf dieselbe Zeit greift.
    -- Die EINE Wahrheit darueber, ob die Zeit abgerechnet ist. Eine zusaetzliche
    -- position_id waere eine zweite: nach Storno und Neuabrechnung haette
    -- position_zeiteintrag zwei Zeilen, und welche davon gilt, muesste man
    -- wieder aus rechnung_id ableiten. Die aktuelle Position ergibt sich aus
    -- position_zeiteintrag JOIN rechnungsposition, eingeschraenkt auf rechnung_id.
    rechnung_id    INTEGER REFERENCES rechnung (id),
    -- Woher der Eintrag nach einer Storno-Freigabe kam. Die Herkunft bleibt
    -- nachvollziehbar, ohne dass der Status sie tragen muss.
    freigegeben_aus_rechnung_id INTEGER REFERENCES rechnung (id),
    -- Stillschweigend verschenkte Arbeit: der Kunde erfaehrt davon nichts.
    abgeschrieben_am    TEXT,
    abgeschrieben_grund TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    erfasst_am     TEXT    NOT NULL,
    geaendert_am   TEXT,
    CHECK (abrechnungsmodus <> 'PAUSCHAL' OR pauschal_cent IS NOT NULL),
    CHECK (abrechnungsmodus NOT IN ('KULANZ', 'INTERN') OR grund IS NOT NULL),
    -- Interne Arbeit erscheint nie auf einer Rechnung.
    CHECK (abrechnungsmodus <> 'INTERN' OR rechnung_id IS NULL),
    -- Abgeschrieben und abgerechnet schliessen sich aus, Grund ist Pflicht.
    CHECK (abgeschrieben_am IS NULL
           OR (rechnung_id IS NULL AND abgeschrieben_grund IS NOT NULL)),
    -- Kulanz braucht einen eingefrorenen Satz, sonst ist der Verzicht nicht bewertbar.
    CHECK (abrechnungsmodus <> 'KULANZ' OR satz_cent_snapshot IS NOT NULL),
    -- >= und nicht >: Start und Stop innerhalb derselben Sekunde kommt vor, und
    -- eine angefangene Minute wird als eine Minute gebucht.
    CHECK (start_zeit IS NULL OR end_zeit IS NULL OR end_zeit >= start_zeit)
);

CREATE INDEX IF NOT EXISTS idx_zeit_kunde_datum ON zeiteintrag (kunde_id, datum);
-- Die Arbeitsliste "was kann ich jetzt abrechnen": INTERN und Abgeschriebenes fliegen raus.
CREATE INDEX IF NOT EXISTS idx_zeit_offen ON zeiteintrag (kunde_id, datum)
    WHERE rechnung_id IS NULL AND abgeschrieben_am IS NULL AND abrechnungsmodus <> 'INTERN';
-- Verzichtsauswertung: die drei Toepfe verschenkter und investierter Arbeit.
CREATE INDEX IF NOT EXISTS idx_zeit_verzicht ON zeiteintrag (datum, kunde_id)
    WHERE abrechnungsmodus IN ('KULANZ', 'INTERN') OR abgeschrieben_am IS NOT NULL;
-- Rueckweg vom Beleg zur Stunde.
CREATE INDEX IF NOT EXISTS idx_zeit_rechnung ON zeiteintrag (rechnung_id)
    WHERE rechnung_id IS NOT NULL;
-- Genau ein laufender Timer. Ein UNIQUE-Index taugt dafuer nicht: SQLite behandelt
-- NULL-Werte als voneinander verschieden, ein UNIQUE auf einer durchgaengig leeren
-- Spalte wuerde also nichts verhindern. Deshalb Trigger.
CREATE TRIGGER IF NOT EXISTS zeit_timer_einmalig_insert
BEFORE INSERT ON zeiteintrag
WHEN NEW.start_zeit IS NOT NULL AND NEW.end_zeit IS NULL
 AND EXISTS (
        SELECT 1 FROM zeiteintrag
        WHERE start_zeit IS NOT NULL AND end_zeit IS NULL
 )
BEGIN
    SELECT RAISE(ABORT, 'Es laeuft bereits ein Timer');
END;

CREATE TRIGGER IF NOT EXISTS zeit_timer_einmalig_update
BEFORE UPDATE ON zeiteintrag
WHEN NEW.start_zeit IS NOT NULL AND NEW.end_zeit IS NULL
 AND EXISTS (
        SELECT 1 FROM zeiteintrag
        WHERE start_zeit IS NOT NULL AND end_zeit IS NULL AND id <> NEW.id
 )
BEGIN
    SELECT RAISE(ABORT, 'Es laeuft bereits ein Timer');
END;

-- ----------------------------------------------------------------- Rechnung

CREATE TABLE IF NOT EXISTS rechnung (
    id                  INTEGER PRIMARY KEY,
    -- "Gutschrift" bedeutet nach § 34a Satz 1 Nr. 6 UStDV Selbstabrechnung durch den
    -- Leistungsempfaenger. Ein Storno darf deshalb NIE so heissen.
    -- LEISTUNGSNACHWEIS ist der Ersatz fuer die verbotene Nullrechnung: er weist
    -- erbrachte Arbeit aus, ohne ein Entgelt abzurechnen, und hat einen eigenen
    -- Nummernkreis, damit der Rechnungskreis lueckenlos bleibt.
    typ                 TEXT    NOT NULL DEFAULT 'RECHNUNG'
        CHECK (typ IN ('RECHNUNG', 'STORNO', 'GUTSCHRIFT_SELBSTABRECHNUNG',
                       'ANGEBOT', 'LEISTUNGSNACHWEIS')),
    nummer              TEXT    UNIQUE,     -- NULL im Entwurf: Vergabe erst beim Festschreiben
    nummer_seq          INTEGER,
    nummer_jahr         INTEGER,
    kunde_id            INTEGER NOT NULL REFERENCES kunde (id),
    -- GoBD: Kopie der Fremddaten, keine Anzeige per JOIN auf die Stammdaten.
    kunde_snapshot_json    TEXT,
    absender_snapshot_json TEXT,
    datum               TEXT,
    leistung_von        TEXT,
    leistung_bis        TEXT,
    zahlungsziel_tage   INTEGER,
    faellig_am          TEXT,
    netto_cent          INTEGER NOT NULL DEFAULT 0,
    -- Kleinunternehmer: niemals Umsatzsteuer ausweisen (§ 14c Abs. 1 UStG).
    steuer_cent         INTEGER NOT NULL DEFAULT 0 CHECK (steuer_cent = 0),
    brutto_cent         INTEGER NOT NULL DEFAULT 0,
    -- Schnappschuss des Leistungsorts. Er entscheidet, WELCHER Hinweis auf die
    -- Rechnung gehoert, und darf sich deshalb nicht mit dem Kundenstamm
    -- aendern: zieht der Kunde von Wien nach Hamburg, bleibt die alte Rechnung
    -- eine nicht steuerbare Leistung ins Ausland.
    leistungsort        TEXT    NOT NULL DEFAULT 'INLAND'
        CHECK (leistungsort IN ('INLAND', 'EU_B2B_REVERSE_CHARGE', 'DRITTLAND')),
    steuerregime        TEXT    NOT NULL DEFAULT 'KU19'
        CHECK (steuerregime IN ('KU19', 'REGEL')),
    hinweis_19_text     TEXT,
    -- Die Betreffzeile. KEINE Pflichtangabe -- § 34a UStDV kennt sie nicht --,
    -- aber die Zeile, nach der der Empfaenger die Rechnung in seiner Ablage
    -- wiederfindet. Vorbelegt aus Zeitraum und Kundenname, frei ueberschreibbar.
    betreff             TEXT,
    einleitung          TEXT,
    schluss             TEXT,
    rundung_hinweis     TEXT,
    gruppierung         TEXT    NOT NULL DEFAULT 'PRO_TITEL',
    einzelnachweis      INTEGER NOT NULL DEFAULT 0 CHECK (einzelnachweis IN (0, 1)),
    -- Der Leistungszeitraum ist fuer einen Kleinunternehmer KEINE Pflichtangabe:
    -- § 34a Satz 1 UStDV listet sechs Angaben, und der Zeitpunkt bzw. Zeitraum
    -- der Leistung ist nicht darunter. Er bleibt kaufmaennisch sinnvoll und wird
    -- deshalb nur abwaehlbar, nicht entfernt.
    -- ACHTUNG: bei Regelbesteuerung verlangt § 14 Abs. 4 Nr. 6 UStG den Zeitpunkt
    -- der Leistung. Die Pruefung ist deshalb regimeabhaengig (siehe validierung.py).
    -- leistung_von/bis bleiben in jedem Fall gespeichert: sie kommen aus den
    -- Zeiteintraegen, tragen die Plausibilitaetspruefungen und den Pruefpfad.
    -- Geschaltet wird nur die AUSGABE.
    leistungszeitraum_zeigen INTEGER NOT NULL DEFAULT 1
        CHECK (leistungszeitraum_zeigen IN (0, 1)),
    status              TEXT    NOT NULL DEFAULT 'ENTWURF'
        CHECK (status IN ('ENTWURF', 'FESTGESCHRIEBEN', 'STORNIERT')),
    storno_von_id       INTEGER REFERENCES rechnung (id),
    storno_grund        TEXT,
    pdf_pfad            TEXT,
    pdf_sha256          TEXT,
    versendet_am        TEXT,
    festgeschrieben_am  TEXT,
    -- Gegen Doppelklick und zwei offene Tabs: das Festschreiben laeuft als
    -- UPDATE ... WHERE status='ENTWURF' AND version=?. Greift es nicht, war
    -- jemand schneller -- und es wird keine zweite Nummer verbraucht.
    version             INTEGER NOT NULL DEFAULT 1,
    erstellt_am         TEXT    NOT NULL,
    -- Eine festgeschriebene Rechnung ist vollstaendig oder sie existiert nicht.
    CHECK (
        status <> 'FESTGESCHRIEBEN'
        OR (nummer IS NOT NULL AND datum IS NOT NULL AND festgeschrieben_am IS NOT NULL)
    ),
    CHECK (typ <> 'STORNO' OR storno_von_id IS NOT NULL),
    CHECK (typ <> 'LEISTUNGSNACHWEIS' OR brutto_cent = 0),
    -- Ein Storno kehrt einen Beleg um und traegt deshalb einen negativen
    -- Betrag. Die POSITIONEN bleiben positiv: dort rechnet die Betragsformel,
    -- und das Vorzeichen gehoert fachlich an den Dokumenttyp, nicht an jede
    -- Zeile. Die Ausgabe dreht es beim Rendern.
    CHECK (CASE typ WHEN 'STORNO' THEN brutto_cent <= 0 ELSE brutto_cent >= 0 END),
    CHECK (CASE typ WHEN 'STORNO' THEN netto_cent  <= 0 ELSE netto_cent  >= 0 END),
    -- Ein Entwurf kann nicht versendet worden sein: er hat keine Nummer.
    CHECK (versendet_am IS NULL OR status <> 'ENTWURF')
);

CREATE INDEX IF NOT EXISTS idx_rechnung_kunde ON rechnung (kunde_id, datum);
-- Offene Posten: Storno-Dokumente, Leistungsnachweise und 0-€-Belege gehoeren nicht dazu.
CREATE INDEX IF NOT EXISTS idx_rechnung_offen ON rechnung (faellig_am, kunde_id)
    WHERE status = 'FESTGESCHRIEBEN' AND typ = 'RECHNUNG' AND brutto_cent > 0;

CREATE TABLE IF NOT EXISTS rechnungsposition (
    id                  INTEGER PRIMARY KEY,
    rechnung_id         INTEGER NOT NULL REFERENCES rechnung (id) ON DELETE CASCADE,
    pos_nr              INTEGER NOT NULL,
    bezeichnung         TEXT    NOT NULL,
    untertext           TEXT,
    menge_tausendstel   INTEGER NOT NULL,
    einheit             TEXT    NOT NULL DEFAULT 'HUR',
    einzelpreis_cent    INTEGER NOT NULL,
    -- Kulanz wird als Nachlass ausgewiesen, nicht als Preis von null. So steht auf
    -- der Rechnung, was die Arbeit wert war UND dass sie nicht berechnet wurde.
    nachlass_cent       INTEGER NOT NULL DEFAULT 0 CHECK (nachlass_cent >= 0),
    nachlass_grund      TEXT,
    darstellung         TEXT    NOT NULL DEFAULT 'BETRAG'
        CHECK (darstellung IN ('BETRAG', 'NUR_TEXT')),
    betrag_cent         INTEGER NOT NULL,
    -- ZUGFeRD-faehig vorgehalten, im KU19-Modus unbenutzt und in der Ausgabe unterdrueckt.
    steuerkategorie     TEXT    NOT NULL DEFAULT 'E',
    steuersatz_promille INTEGER NOT NULL DEFAULT 0,
    -- Die Quelle einer Kulanzzeile ist weiterhin die Zeiterfassung. Ob berechnet
    -- wird, sagt nachlass_cent, nicht die Herkunft.
    quelle              TEXT    NOT NULL CHECK (quelle IN ('ZEIT', 'PAUSCHAL', 'FREI')),
    UNIQUE (rechnung_id, pos_nr),
    -- § 34a Satz 1 Nr. 4 UStDV verlangt Menge bzw. Umfang der Leistung. Tritt real
    -- auf, wenn die Rundung mit ABWAERTS eine Position auf null bringt.
    CHECK (menge_tausendstel > 0),
    CHECK (einzelpreis_cent >= 0),
    -- Eine Zeile ohne Betrag braucht immer einen Text, sonst steht auf dem PDF
    -- eine nackte Null und sie sieht wie ein Fehler aus.
    CHECK (nachlass_cent = 0 OR nachlass_grund IS NOT NULL),
    CHECK (einzelpreis_cent > 0 OR nachlass_grund IS NOT NULL),
    -- Die Betragsformel steht in der Datenbank, damit kein Renderer sie umgehen kann.
    -- Bei nichtnegativen Integern ist (a * b + 500) / 1000 genau kaufmaennische
    -- Rundung -- dieselbe wie in app/logik/rundung.py.
    CHECK (betrag_cent = (menge_tausendstel * einzelpreis_cent + 500) / 1000 - nachlass_cent),
    CHECK (betrag_cent >= 0)
);

-- Zweite, datenbankseitige Sperre gegen Doppelabrechnung -- aber storno-tolerant.
-- Ein UNIQUE auf zeiteintrag_id waere falsch: eine Stunde liesse sich nach einem
-- Storno nie wieder abrechnen, es sei denn man loescht die Historie "diese Stunde
-- lag auf RE-2026-0012". Der Trigger zaehlt deshalb nur nicht stornierte Rechnungen.
CREATE TABLE IF NOT EXISTS position_zeiteintrag (
    position_id    INTEGER NOT NULL REFERENCES rechnungsposition (id) ON DELETE CASCADE,
    zeiteintrag_id INTEGER NOT NULL REFERENCES zeiteintrag (id),
    PRIMARY KEY (position_id, zeiteintrag_id)
);

CREATE INDEX IF NOT EXISTS idx_pz_zeit ON position_zeiteintrag (zeiteintrag_id);

CREATE TRIGGER IF NOT EXISTS pz_keine_doppelabrechnung
BEFORE INSERT ON position_zeiteintrag
WHEN EXISTS (
    SELECT 1
      FROM position_zeiteintrag pz
      JOIN rechnungsposition p ON p.id = pz.position_id
      JOIN rechnung r          ON r.id = p.rechnung_id
     WHERE pz.zeiteintrag_id = NEW.zeiteintrag_id
       AND r.status <> 'STORNIERT'
)
BEGIN
    SELECT RAISE(ABORT, 'Zeiteintrag liegt bereits auf einer nicht stornierten Rechnung');
END;

-- Bemessungsgrundlage der EUeR (§ 11 EStG) und des § 19-Grenzwerts, nicht Komfort.
CREATE TABLE IF NOT EXISTS zahlung (
    id          INTEGER PRIMARY KEY,
    rechnung_id INTEGER REFERENCES rechnung (id),
    -- Bewusst OHNE Fremdschluessel. Die Spalte gab es schon vor der
    -- Belegtabelle, und SQLite kann einen Fremdschluessel nicht per ALTER
    -- ergaenzen -- nur durch Neubau der Tabelle. Diese Tabelle neu zu bauen
    -- heisst, die Bemessungsgrundlage der EUeR umzuschaufeln, und ein Verlust
    -- dort faellt erst im Jahresabschluss auf. Dieselbe Garantie gibt der
    -- Trigger zahlung_beleg_existiert weiter unten, und der laesst sich
    -- nachtragen.
    beleg_id    INTEGER,
    datum       TEXT    NOT NULL,
    betrag_cent INTEGER NOT NULL CHECK (betrag_cent <> 0),
    art         TEXT    NOT NULL DEFAULT 'UEBERWEISUNG'
        CHECK (art IN ('UEBERWEISUNG', 'KARTE', 'BAR', 'PAYPAL', 'SONSTIGES')),
    notiz       TEXT,
    CHECK ((rechnung_id IS NULL) <> (beleg_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_zahlung_rechnung ON zahlung (rechnung_id);
CREATE INDEX IF NOT EXISTS idx_zahlung_datum ON zahlung (datum);

CREATE TABLE IF NOT EXISTS nummernkreis (
    id               INTEGER PRIMARY KEY,
    typ              TEXT    NOT NULL,
    jahr             INTEGER NOT NULL,
    format           TEXT    NOT NULL DEFAULT 'RE-{JJJJ}-{NNNN}',
    naechste_seq     INTEGER NOT NULL DEFAULT 1 CHECK (naechste_seq > 0),
    reset_jaehrlich  INTEGER NOT NULL DEFAULT 1 CHECK (reset_jaehrlich IN (0, 1)),
    UNIQUE (typ, jahr)
);

-- ----------------------------------------------------- Protokoll (append-only)

CREATE TABLE IF NOT EXISTS journal (
    id           INTEGER PRIMARY KEY,
    zeitpunkt_utc TEXT   NOT NULL,
    aktion       TEXT    NOT NULL,
    objekttyp    TEXT    NOT NULL,
    objekt_id    INTEGER,
    feld         TEXT,
    wert_alt     TEXT,
    wert_neu     TEXT,
    begruendung  TEXT,
    hash         TEXT
);

CREATE TRIGGER IF NOT EXISTS journal_kein_update
BEFORE UPDATE ON journal
BEGIN
    SELECT RAISE(ABORT, 'Journal ist append-only');
END;

CREATE TRIGGER IF NOT EXISTS journal_kein_delete
BEFORE DELETE ON journal
BEGIN
    SELECT RAISE(ABORT, 'Journal ist append-only');
END;

-- § 146 Abs. 4 AO: festgeschriebene Rechnungen sind unveraenderbar.
-- Erlaubt bleiben nur der Weg nach STORNIERT und die Felder, die den Versand
-- und die Ablage dokumentieren.
CREATE TRIGGER IF NOT EXISTS rechnung_gesperrt
BEFORE UPDATE ON rechnung
WHEN OLD.status = 'FESTGESCHRIEBEN'
 AND (
        NEW.nummer      IS NOT OLD.nummer
     OR NEW.datum       IS NOT OLD.datum
     OR NEW.kunde_id    IS NOT OLD.kunde_id
     OR NEW.netto_cent  IS NOT OLD.netto_cent
     OR NEW.brutto_cent IS NOT OLD.brutto_cent
     OR NEW.kunde_snapshot_json IS NOT OLD.kunde_snapshot_json
     OR NEW.status NOT IN ('FESTGESCHRIEBEN', 'STORNIERT')
 )
BEGIN
    SELECT RAISE(ABORT,
        'Festgeschriebene Rechnung ist unveraenderbar: Korrektur nur ueber Storno');
END;

CREATE TRIGGER IF NOT EXISTS rechnung_kein_delete
BEFORE DELETE ON rechnung
WHEN OLD.status <> 'ENTWURF'
BEGIN
    SELECT RAISE(ABORT, 'Festgeschriebene Rechnung darf nicht geloescht werden');
END;

CREATE TRIGGER IF NOT EXISTS position_gesperrt
BEFORE UPDATE ON rechnungsposition
WHEN (SELECT status FROM rechnung WHERE id = OLD.rechnung_id) = 'FESTGESCHRIEBEN'
BEGIN
    SELECT RAISE(ABORT, 'Position einer festgeschriebenen Rechnung ist unveraenderbar');
END;

-- Eine Rechnung ueber 0,00 € rechnet ueber keinen Umsatz "gegen Entgelt" nach
-- § 1 Abs. 1 Nr. 1 UStG ab, ist damit keine Rechnung im Sinne des § 14 Abs. 1 UStG,
-- bricht die Buchhaltungskette und verbrennt eine Rechnungsnummer fuer ein
-- Nicht-Dokument. Kulanzstunden reiten statt dessen mit 0,00 € auf einer
-- berechneten Rechnung mit, bekommen einen LEISTUNGSNACHWEIS oder werden
-- abgeschrieben. COALESCE sorgt dafuer, dass ohne angelegten Betrieb gesperrt wird.
CREATE TRIGGER IF NOT EXISTS rechnung_keine_nullrechnung_update
BEFORE UPDATE OF status ON rechnung
WHEN NEW.status = 'FESTGESCHRIEBEN'
 AND COALESCE((SELECT nullrechnung_zulassen FROM betrieb WHERE id = 1), 0) = 0
 AND (    (NEW.typ = 'RECHNUNG' AND NEW.brutto_cent <= 0)
       -- Ein Storno traegt korrekt einen negativen Betrag; null oder positiv
       -- waere hier der Fehler.
       OR (NEW.typ = 'STORNO'   AND NEW.brutto_cent >= 0) )
BEGIN
    -- RAISE nimmt nur ein Literal, deshalb eine lange Zeile.
    SELECT RAISE(ABORT, 'Rechnung ueber 0,00 EUR ist keine Rechnung (kein Entgelt nach § 1 Abs. 1 Nr. 1 UStG). Kulanzstunden an eine berechnete Rechnung haengen, einen Leistungsnachweis ausstellen oder die Stunden abschreiben.');
END;

CREATE TRIGGER IF NOT EXISTS rechnung_keine_nullrechnung_insert
BEFORE INSERT ON rechnung
WHEN NEW.status = 'FESTGESCHRIEBEN'
 AND COALESCE((SELECT nullrechnung_zulassen FROM betrieb WHERE id = 1), 0) = 0
 AND (    (NEW.typ = 'RECHNUNG' AND NEW.brutto_cent <= 0)
       -- Ein Storno traegt korrekt einen negativen Betrag; null oder positiv
       -- waere hier der Fehler.
       OR (NEW.typ = 'STORNO'   AND NEW.brutto_cent >= 0) )
BEGIN
    SELECT RAISE(ABORT,
        'Rechnung ueber 0,00 EUR ist keine Rechnung (kein Entgelt nach § 1 Abs. 1 Nr. 1 UStG).');
END;

-- Ein Entwurf hat keine Nummer, also auch nichts zu stornieren: er wird geloescht.
-- Zu einer Rechnung gibt es hoechstens EIN Storno. Die Logik prueft das in
-- ihrer Transaktion; der Index ist die zweite Sicherung dahinter, falls ein
-- anderer Weg in die Tabelle schreibt.
CREATE UNIQUE INDEX IF NOT EXISTS rechnung_storno_einmalig
    ON rechnung (storno_von_id) WHERE storno_von_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS rechnung_entwurf_kein_storno
BEFORE UPDATE OF status ON rechnung
WHEN OLD.status = 'ENTWURF' AND NEW.status = 'STORNIERT'
BEGIN
    SELECT RAISE(ABORT, 'Ein Entwurf wird geloescht, nicht storniert: es gibt keine Nummer');
END;

CREATE TRIGGER IF NOT EXISTS rechnung_storniert_terminal
BEFORE UPDATE ON rechnung
WHEN OLD.status = 'STORNIERT' AND NEW.status <> 'STORNIERT'
BEGIN
    SELECT RAISE(ABORT, 'STORNIERT ist ein Endzustand');
END;

-- Eine stornierte Rechnung kann Geld nur noch ZURUECK bewegen: die Rueckzahlung
-- einer bereits vereinnahmten Anzahlung ist genau der Fall, der nach dem Storno
-- noch erfasst werden muss.
--
-- Ein Storno-Dokument (Rechnungskorrektur) nimmt nie Geld an: es fordert nichts,
-- es hebt auf. Die Rueckzahlung gehoert ans Original. Bis Version 15 wies das
-- nur die Route ab; ein anderer Schreibweg haette den § 19-Zaehler erhoeht.
-- Aenderungen an diesem Trigger brauchen einen Migrationsschritt (db.py), denn
-- CREATE TRIGGER IF NOT EXISTS ersetzt einen vorhandenen nicht.
CREATE TRIGGER IF NOT EXISTS zahlung_nur_festgeschrieben
BEFORE INSERT ON zahlung
WHEN NEW.rechnung_id IS NOT NULL
 AND (   (SELECT status FROM rechnung WHERE id = NEW.rechnung_id)
             NOT IN ('FESTGESCHRIEBEN', 'STORNIERT')
      OR (SELECT typ FROM rechnung WHERE id = NEW.rechnung_id) = 'STORNO'
      OR (SELECT brutto_cent FROM rechnung WHERE id = NEW.rechnung_id) = 0
      OR (    (SELECT status FROM rechnung WHERE id = NEW.rechnung_id) = 'STORNIERT'
          AND NEW.betrag_cent > 0 ) )
BEGIN
    SELECT RAISE(ABORT, 'Zahlung nur zu einer festgeschriebenen Rechnung mit Betrag; auf eine stornierte Rechnung nur eine Rueckzahlung; keine Zahlung auf eine Rechnungskorrektur');
END;

-- Liegt die Zeit auf einer festgeschriebenen Rechnung, ist sie Teil eines
-- unveraenderbaren Belegs. Rueckwirkende Korrektur nur ueber Storno.
CREATE TRIGGER IF NOT EXISTS zeit_gesperrt
BEFORE UPDATE ON zeiteintrag
WHEN OLD.rechnung_id IS NOT NULL
 AND (SELECT status FROM rechnung WHERE id = OLD.rechnung_id) = 'FESTGESCHRIEBEN'
 AND (   NEW.abrechnungsmodus   IS NOT OLD.abrechnungsmodus
      OR NEW.dauer_minuten      IS NOT OLD.dauer_minuten
      OR NEW.datum              IS NOT OLD.datum
      OR NEW.kunde_id           IS NOT OLD.kunde_id
      OR NEW.satz_cent_snapshot IS NOT OLD.satz_cent_snapshot
      -- Ohne diese Zeile liesse sich die Zeit auf einen anderen Entwurf
      -- umhaengen, obwohl sie auf einem unveraenderbaren Beleg liegt.
      OR NEW.rechnung_id        IS NOT OLD.rechnung_id )
 -- Ausnahme: die Freigabe nach einem Storno.
 AND NOT (NEW.rechnung_id IS NULL
          AND (SELECT status FROM rechnung WHERE id = OLD.rechnung_id) = 'STORNIERT')
BEGIN
    SELECT RAISE(ABORT, 'Zeit liegt auf einer festgeschriebenen Rechnung: Korrektur nur ueber Storno');
END;

CREATE TABLE IF NOT EXISTS backup_log (
    id         INTEGER PRIMARY KEY,
    zeitpunkt  TEXT    NOT NULL,
    pfad       TEXT    NOT NULL,
    groesse    INTEGER,
    sha256     TEXT,
    geprueft   INTEGER NOT NULL DEFAULT 0 CHECK (geprueft IN (0, 1))
);

-- ---------------------------------------------------- Eingangsbelege (Phase 2)

-- Die Kategorien der Anlage EUeR.
--
-- Abgebildet wird auf (bereich, kennzahl), NICHT auf die Kennzahl allein:
-- Kennzahlen sind nur innerhalb eines Formularbereichs eindeutig. Im Vordruck
-- 2025 steht KZ 111 sowohl in Zeile 10 ("Betrieb beendet?") als auch in
-- Zeile 12 (Betriebseinnahmen Kleinunternehmer).
--
-- Zeilennummern und amtliche Bezeichnungstexte stehen NICHT hier: sie
-- verschieben sich jedes Jahr. Sie liegen in app/stammdaten/euer_<jahr>.json.
-- Hier steht nur, was stabil ist -- Bereich und Kennzahl.
CREATE TABLE IF NOT EXISTS euer_kategorie (
    id            INTEGER PRIMARY KEY,
    -- Stabiler Schluessel fuer Code und Wiedereinlesen der Stammdatendatei.
    -- Ohne ihn waere die id der einzige Bezug, und die haengt an der
    -- Einfuegereihenfolge.
    schluessel    TEXT    NOT NULL UNIQUE,
    bezeichnung   TEXT    NOT NULL,
    bereich       INTEGER NOT NULL CHECK (bereich IN (20, 25)),  -- 20 Einnahmen, 25 Ausgaben
    kennzahl      INTEGER NOT NULL,
    -- Bewirtung, Geschenke, Arbeitszimmer: der nicht abziehbare Teil geht in
    -- eine eigene Kennzahl. Ohne dieses Feld landete er stillschweigend im
    -- abziehbaren Teil und der Gewinn waere zu niedrig.
    kennzahl_nicht_abziehbar INTEGER,
    -- Anteil, der abziehbar ist: Bewirtung 70 %, alles andere 100 %.
    anteil_abziehbar_promille INTEGER NOT NULL DEFAULT 1000
        CHECK (anteil_abziehbar_promille BETWEEN 0 AND 1000),
    quote_default INTEGER NOT NULL DEFAULT 100
        CHECK (quote_default BETWEEN 0 AND 100),
    -- Wertgrenze in Cent, NETTO geprueft (R 9b Abs. 2 EStR). Darueber ist es
    -- ein Anlagegut und gehoert in die AVEUeR.
    grenze_netto_cent INTEGER,
    hinweis       TEXT,
    sortierung    INTEGER NOT NULL DEFAULT 0,
    aktiv         INTEGER NOT NULL DEFAULT 1 CHECK (aktiv IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_kategorie_kennzahl
    ON euer_kategorie (bereich, kennzahl);

-- Wer die Leistung erbracht hat. Eigene Tabelle und nicht ein Textfeld im
-- Beleg, weil daran der Lernspeicher haengt: ab dem zweiten Beleg desselben
-- Lieferanten sind Layout und Feldpositionen bekannt.
CREATE TABLE IF NOT EXISTS lieferant (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT    NOT NULL,
    strasse               TEXT,
    plz                   TEXT,
    ort                   TEXT,
    land                  TEXT    NOT NULL DEFAULT 'DE',
    ust_idnr              TEXT,
    standard_kategorie_id INTEGER REFERENCES euer_kategorie (id),
    -- Vorbelegung der betrieblichen Quote, etwa Mobilfunk 70 %. Sie gehoert
    -- NICHT an die Kategorie: sie wechselt mit dem Vertrag, nicht mit der
    -- Ausgabenart.
    quote_default         INTEGER CHECK (quote_default IS NULL
                                         OR quote_default BETWEEN 0 AND 100),
    -- § 13b Abs. 5 UStG macht JEDEN Unternehmer zum Steuerschuldner,
    -- Kleinunternehmer eingeschlossen. Bei AWS, Figma oder Google ist das der
    -- Normalfall und nicht der Rand, deshalb merkt sich der Lieferant es.
    reverse_charge_default INTEGER NOT NULL DEFAULT 0
        CHECK (reverse_charge_default IN (0, 1)),
    waehrung_default      TEXT    NOT NULL DEFAULT 'EUR',
    notiz                 TEXT,
    angelegt_am           TEXT    NOT NULL,
    aktiv                 INTEGER NOT NULL DEFAULT 1 CHECK (aktiv IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS lieferant_name_einmalig
    ON lieferant (name COLLATE NOCASE);

-- Eine Geschaeftsreise. Der EINZIGE Ort, an dem ein Vorgang mehrere Kennzahlen
-- braucht: ein Konferenzbesuch zerfaellt in Ticket (KZ 281), Hotel (221),
-- Verpflegungspauschale (171) und Fahrt (146 oder 147). Der Container haelt die
-- Belege zusammen, ohne sie zu vermischen.
--
-- Anlass und Ziel sind Pflichtfelder und nicht Komfort: eine Reise ohne
-- erkennbaren betrieblichen Anlass ist nicht abziehbar, und der Anlass muss
-- aufgezeichnet sein -- nicht erst in der Pruefung erinnert werden.
CREATE TABLE IF NOT EXISTS reise (
    id          INTEGER PRIMARY KEY,
    bezeichnung TEXT    NOT NULL,
    anlass      TEXT    NOT NULL,
    ziel        TEXT    NOT NULL,
    -- Inland oder Ausland: die Verpflegungspauschale unterscheidet sich, und
    -- die Auslandssaetze setzt das BMF jedes Jahr neu fest.
    inland      INTEGER NOT NULL DEFAULT 1 CHECK (inland IN (0, 1)),
    abreise_am  TEXT    NOT NULL,
    rueckkehr_am TEXT   NOT NULL,
    -- Nur gesetzt, wenn die Reise kuerzer als einen Tag war: dann entscheidet
    -- die Abwesenheitsdauer ueber die Pauschale.
    abwesend_stunden INTEGER,
    -- Uebernachtung ausserhalb der Wohnung. Ohne sie gibt es fuer An- und
    -- Abreisetag keine Pauschale (§ 9 Abs. 4a Satz 3 Nr. 2 EStG), nur die
    -- Teiltagregel. 1 fuer Altbestaende: so wurde bisher gerechnet, und eine
    -- Migration darf gebuchte Pauschalen nicht umdeuten. Der CHECK laesst sich
    -- per ALTER nicht nachtragen; die Oberflaeche schreibt nur 0 und 1.
    uebernachtung INTEGER NOT NULL DEFAULT 1 CHECK (uebernachtung IN (0, 1)),
    notiz       TEXT,
    erfasst_am  TEXT    NOT NULL,
    CHECK (rueckkehr_am >= abreise_am)
);

CREATE INDEX IF NOT EXISTS idx_reise_zeit ON reise (abreise_am);

-- Ein Beleg. Betraege IMMER brutto, wie sie auf dem Papier stehen: ohne
-- Vorsteuerabzug sind Betriebsausgaben "mit dem Bruttobetrag in den jeweiligen
-- Betriebsausgabenpositionen" einzutragen (amtliche Anleitung zur Anlage EUeR).
CREATE TABLE IF NOT EXISTS beleg (
    id                 INTEGER PRIMARY KEY,
    art                TEXT    NOT NULL DEFAULT 'EINGANG'
        CHECK (art IN ('EINGANG', 'AUSGANG')),
    lieferant_id       INTEGER REFERENCES lieferant (id),
    kategorie_id       INTEGER REFERENCES euer_kategorie (id),
    belegdatum         TEXT    NOT NULL,
    belegnummer_extern TEXT,
    waehrung           TEXT    NOT NULL DEFAULT 'EUR',
    -- Der Nominalbetrag in der Fremdwaehrung, falls der Beleg nicht in Euro
    -- ist. betrag_brutto_cent ist dann der Euro-Betrag -- nur der gehoert in
    -- die EUeR. Ohne diese Trennung stand ein Beleg ueber 100 USD mit 100 Euro
    -- in der EUeR.
    betrag_fremdwaehrung_cent INTEGER,
    -- Die Umrechnung, nachpruefbar (Migration 19, app/logik/beleg/umrechnung.py):
    -- der Kurs mit allen Stellen der Quelle (1 EUR = kurs Einheiten der
    -- Waehrung), der Tag, dessen Kurs gilt, die Quelle des Kurses (EZB oder von
    -- Hand) und -- beim EZB-Kurs -- Abrufzeit und SHA-256 der Kurstabelle.
    kurs               TEXT CHECK (kurs IS NULL OR CAST(kurs AS REAL) > 0),
    kurs_datum         TEXT,
    kurs_quelle        TEXT CHECK (kurs_quelle IS NULL OR kurs_quelle IN ('EZB', 'MANUELL')),
    kurs_abruf_utc     TEXT,
    kurs_tabelle_sha256 TEXT,
    -- Fremdbetrag / Kurs, kaufmaennisch auf Cent -- und der Betrag laut
    -- Kreditkarte, falls eingetragen. Der Kartenbetrag ist genauer (er traegt
    -- das Auslandseinsatzentgelt) und ersetzt den umgerechneten; der bleibt
    -- zum Vergleich. betrag_brutto_cent ist immer der eine oder der andere --
    -- das prueft der Trigger beleg_euro_betrag_stimmig.
    betrag_umgerechnet_cent INTEGER
        CHECK (betrag_umgerechnet_cent IS NULL OR betrag_umgerechnet_cent > 0),
    betrag_karte_cent  INTEGER CHECK (betrag_karte_cent IS NULL OR betrag_karte_cent > 0),
    betrag_brutto_cent INTEGER NOT NULL CHECK (betrag_brutto_cent > 0),
    betrag_netto_cent  INTEGER,
    betrag_ust_cent    INTEGER,
    ust_satz_promille  INTEGER,
    -- § 13b Abs. 5 Satz 1 UStG. Gesondert gefuehrt, weil der Beleg dann 0 %
    -- USt zeigt und trotzdem kein steuerfreier Umsatz ist.
    reverse_charge     INTEGER NOT NULL DEFAULT 0 CHECK (reverse_charge IN (0, 1)),
    -- Nur fuer die WERTGRENZE der geringwertigen Wirtschaftsgueter: die wird
    -- nach R 9b Abs. 2 EStR stets netto geprueft -- "Ob der Vorsteuerbetrag
    -- umsatzsteuerrechtlich abziehbar ist, spielt in diesem Fall keine Rolle."
    -- 800 EUR netto sind also rund 952 EUR brutto. Gebucht wird dagegen der
    -- Bruttowert (R 9b Abs. 1). Liegt kein Netto auf dem Beleg -- Lieferant ist
    -- selbst Kleinunternehmer, Auslandsbeleg --, ist Netto = Brutto.
    netto_fuer_grenzwert_cent INTEGER,
    aufteilungsquote   INTEGER NOT NULL DEFAULT 100
        CHECK (aufteilungsquote BETWEEN 0 AND 100),
    -- Was in die EUeR geht. Als Spalte und nicht als Ausdruck, damit die
    -- Rundung EINMAL stattfindet und jede Auswertung dieselbe Zahl sieht.
    betrag_betrieblich_cent INTEGER NOT NULL CHECK (betrag_betrieblich_cent >= 0),
    -- Eine Geschaeftsreise zerfaellt in vier Kennzahlen (Ticket, Hotel,
    -- Verpflegungspauschale, Fahrt). Der Container haelt sie zusammen.
    reise_id           INTEGER REFERENCES reise (id),
    -- Ein Eigenbeleg hat kein Fremddokument: Verpflegungspauschale,
    -- Kilometeraufstellung, ein verlorener Kassenzettel. Er braucht dafuer
    -- einen Grund, und der steht in notiz -- der CHECK erzwingt ihn.
    ist_eigenbeleg     INTEGER NOT NULL DEFAULT 0 CHECK (ist_eigenbeleg IN (0, 1)),
    ist_anlagegut      INTEGER NOT NULL DEFAULT 0 CHECK (ist_anlagegut IN (0, 1)),
    zahlungsart        TEXT    NOT NULL DEFAULT 'UEBERWEISUNG'
        CHECK (zahlungsart IN ('UEBERWEISUNG', 'KARTE', 'BAR', 'PAYPAL', 'SONSTIGES')),
    notiz              TEXT,
    -- Gesetzt, sobald der Nutzer die ausgelesenen Felder bestaetigt hat. Leer
    -- bleibt es bei einem Beleg, den das Abholen aus dem Eingangsordner selbst
    -- gebucht hat (nur sicher erkannte Rechnungen bekannter Lieferanten): er
    -- steht unter "nur ungeklaerte", bis der Nutzer "Stimmt so" bestaetigt.
    geprueft_am        TEXT,
    erfasst_am         TEXT    NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1,
    -- Umsatzsteuer und Netto gehoeren zusammen: eines ohne das andere ist ein
    -- halb ausgelesener Beleg, der spaeter als vollstaendig gilt.
    CHECK ((betrag_netto_cent IS NULL) = (betrag_ust_cent IS NULL)),
    CHECK (betrag_netto_cent IS NULL
           OR betrag_netto_cent + betrag_ust_cent = betrag_brutto_cent),
    -- Reverse Charge und ausgewiesene Umsatzsteuer schliessen sich aus: schuldet
    -- der Leistungsempfaenger die Steuer, steht auf dem Beleg keine.
    CHECK (reverse_charge = 0 OR COALESCE(betrag_ust_cent, 0) = 0)
);

-- Ein Eigenbeleg ohne Begruendung ist in einer Pruefung wertlos. Er ist die
-- Ausnahme von "jeder Ausgabe liegt ein Fremdbeleg zugrunde", und eine Ausnahme
-- muss man begruenden koennen -- spaeter erinnert sich niemand.
--
-- Als Trigger und nicht als CHECK, damit neue und migrierte Datenbanken sich
-- gleich verhalten: ein CHECK laesst sich per ALTER nicht nachtragen, und dann
-- gaebe es die Regel nur in einer frisch angelegten Datenbank.
CREATE TRIGGER IF NOT EXISTS beleg_eigenbeleg_braucht_grund_insert
BEFORE INSERT ON beleg
WHEN NEW.ist_eigenbeleg = 1
 AND (NEW.notiz IS NULL OR length(trim(NEW.notiz)) < 10)
BEGIN
    SELECT RAISE(ABORT, 'Eigenbeleg ohne Begruendung: ohne Fremdbeleg muss der Grund aufgezeichnet sein');
END;

CREATE TRIGGER IF NOT EXISTS beleg_eigenbeleg_braucht_grund_update
BEFORE UPDATE OF notiz, ist_eigenbeleg ON beleg
WHEN NEW.ist_eigenbeleg = 1
 AND (NEW.notiz IS NULL OR length(trim(NEW.notiz)) < 10)
BEGIN
    SELECT RAISE(ABORT, 'Eigenbeleg ohne Begruendung: ohne Fremdbeleg muss der Grund aufgezeichnet sein');
END;

-- Der Euro-Betrag eines Fremdwaehrungsbelegs ist der Kartenbetrag oder, ohne
-- ihn, der umgerechnete -- nie ein dritter. Sonst stuende in der EUeR eine
-- Zahl, die keine der gespeicherten Grundlagen erklaert. Als Trigger, damit
-- neue und migrierte Datenbanken dieselbe Regel haben.
CREATE TRIGGER IF NOT EXISTS beleg_euro_betrag_stimmig_insert
BEFORE INSERT ON beleg
WHEN COALESCE(NEW.betrag_karte_cent, NEW.betrag_umgerechnet_cent) IS NOT NULL
 AND NEW.betrag_brutto_cent <> COALESCE(NEW.betrag_karte_cent, NEW.betrag_umgerechnet_cent)
BEGIN
    SELECT RAISE(ABORT, 'Fremdwaehrung: der Euro-Betrag muss der Kartenbetrag oder der umgerechnete Betrag sein');
END;

CREATE TRIGGER IF NOT EXISTS beleg_euro_betrag_stimmig_update
BEFORE UPDATE OF betrag_brutto_cent, betrag_karte_cent, betrag_umgerechnet_cent ON beleg
WHEN COALESCE(NEW.betrag_karte_cent, NEW.betrag_umgerechnet_cent) IS NOT NULL
 AND NEW.betrag_brutto_cent <> COALESCE(NEW.betrag_karte_cent, NEW.betrag_umgerechnet_cent)
BEGIN
    SELECT RAISE(ABORT, 'Fremdwaehrung: der Euro-Betrag muss der Kartenbetrag oder der umgerechnete Betrag sein');
END;

CREATE INDEX IF NOT EXISTS idx_beleg_datum ON beleg (belegdatum);
CREATE INDEX IF NOT EXISTS idx_beleg_kategorie ON beleg (kategorie_id);
CREATE INDEX IF NOT EXISTS idx_beleg_lieferant ON beleg (lieferant_id, belegdatum);
-- Die Pruefliste "nicht zugeordnet", die spaeter den Jahresabschluss sperrt.
CREATE INDEX IF NOT EXISTS idx_beleg_offen ON beleg (erfasst_am)
    WHERE kategorie_id IS NULL OR geprueft_am IS NULL;

-- Die Datei zum Beleg. Ein Ordner mit PDFs erfuellt die Unveraenderbarkeit
-- ausdruecklich NICHT; Pruefsumme, Dateirechte 0444 und das Journal sind der
-- billigste Weg, sie doch zu erfuellen.
CREATE TABLE IF NOT EXISTS belegdatei (
    id            INTEGER PRIMARY KEY,
    beleg_id      INTEGER NOT NULL REFERENCES beleg (id),
    pfad          TEXT    NOT NULL,
    original_name TEXT    NOT NULL,
    sha256        TEXT    NOT NULL,
    groesse       INTEGER NOT NULL CHECK (groesse > 0),
    mime          TEXT,
    rolle         TEXT    NOT NULL DEFAULT 'ORIGINAL'
        CHECK (rolle IN ('ORIGINAL', 'ABGELEITETES_XML', 'EMAIL')),
    importiert_am TEXT    NOT NULL,
    UNIQUE (beleg_id, sha256)
);

-- Der Dublettencheck. Ueber ALLE Belege und nur fuer Originale: dieselbe Datei
-- zweimal zu buchen ist der haeufigste Erfassungsfehler, und das abgeleitete
-- XML eines ZUGFeRD-PDF darf nicht mit dem PDF selbst kollidieren.
CREATE UNIQUE INDEX IF NOT EXISTS belegdatei_original_einmalig
    ON belegdatei (sha256) WHERE rolle = 'ORIGINAL';

-- Die Datei ist der Beleg. Pfad und Pruefsumme nachtraeglich zu aendern hiesse,
-- den Beleg auszutauschen -- genau das, was § 146 Abs. 4 AO untersagt.
CREATE TRIGGER IF NOT EXISTS belegdatei_unveraenderlich
BEFORE UPDATE OF sha256, pfad, groesse, beleg_id ON belegdatei
BEGIN
    SELECT RAISE(ABORT, 'Belegdatei ist unveraenderlich: Pruefsumme und Pfad sind der Nachweis');
END;

CREATE TRIGGER IF NOT EXISTS beleg_kein_delete_mit_datei
BEFORE DELETE ON beleg
WHEN EXISTS (SELECT 1 FROM belegdatei WHERE beleg_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'Beleg mit Datei gehoert zur Buchfuehrung und darf nicht geloescht werden');
END;

-- Das Auslesungsergebnis. Nach GoBD Rz. 130 ist ein verifizierter
-- angereicherter Volltext SELBST aufbewahrungs- und vorlagepflichtig -- das
-- bestaetigte Ergebnis ist also ein archivpflichtiger Datensatz und kein
-- Zwischenergebnis. Deshalb eine eigene Tabelle und kein JSON-Feld am Beleg.
CREATE TABLE IF NOT EXISTS extraktion (
    id             INTEGER PRIMARY KEY,
    beleg_id       INTEGER NOT NULL REFERENCES beleg (id),
    -- Die Stufenfolge ist durch Recht vorgegeben, nicht durch Bequemlichkeit:
    -- ist XML vorhanden, ist es massgebend und eine Bilderkennung des
    -- Bildteils unzulaessig.
    -- 'email': der Text einer E-Mail (.eml) ohne Rechnung im Anhang.
    quelle         TEXT    NOT NULL
        CHECK (quelle IN ('xml', 'pdf_text', 'ocr_lokal', 'email', 'manuell')),
    rohantwort_json TEXT,
    bestaetigt_json TEXT,
    -- Womit ausgelesen wurde. Gehoert in die Verfahrensdokumentation und muss
    -- deshalb am Datensatz haengen, nicht in einer Konfigurationsdatei.
    verfahren      TEXT,
    regel_version  TEXT,
    datei_sha256   TEXT,
    zeitpunkt      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extraktion_beleg ON extraktion (beleg_id, zeitpunkt);

-- Der Lernspeicher, der staerkste Hebel der Auslesung: beim zweiten Beleg
-- desselben Lieferanten sind Layout und Feldpositionen bekannt. Bei zwanzig
-- wiederkehrenden Lieferanten ist die Erfassung nach einem Jahr ein
-- Bestaetigungsklick.
--
-- Gelernt wird aus der ohnehin noetigen Verifikation, nicht aus einem
-- Trainingslauf. Ein Layoutwechsel faellt daran auf, dass das Profil nichts
-- findet oder die Arithmetik nicht aufgeht.
CREATE TABLE IF NOT EXISTS lieferant_profil (
    id           INTEGER PRIMARY KEY,
    lieferant_id INTEGER NOT NULL REFERENCES lieferant (id),
    feld         TEXT    NOT NULL
        CHECK (feld IN ('belegnummer', 'belegdatum', 'brutto', 'netto', 'ust',
                        'ust_satz', 'waehrung', 'zahlungsdatum')),
    -- Wie der Wert gefunden wurde: ein Muster mit einer Fanggruppe, dazu der
    -- erwartete Zeilenkontext. Beides zusammen, weil ein Muster allein auf
    -- jeder zweiten Zeile zufaellig trifft.
    muster       TEXT    NOT NULL,
    kontext      TEXT,
    -- Zahlenformat je LIEFERANT, nicht je Waehrung: AWS, Google und Figma
    -- rechnen in Euro, schreiben aber 1,234.56. Ein deutsches Muster zerlegt
    -- das still falsch zu 1,23.
    zahlenformat TEXT    NOT NULL DEFAULT 'DE' CHECK (zahlenformat IN ('DE', 'EN')),
    treffer      INTEGER NOT NULL DEFAULT 0,
    fehlschlaege INTEGER NOT NULL DEFAULT 0,
    gelernt_am   TEXT    NOT NULL,
    bestaetigt_am TEXT,
    UNIQUE (lieferant_id, feld, muster)
);

CREATE INDEX IF NOT EXISTS idx_profil_lieferant ON lieferant_profil (lieferant_id, feld);

-- Ersatz fuer den Fremdschluessel, den zahlung.beleg_id nicht tragen kann
-- (Begruendung an der Spalte). Eine Zahlung auf einen Beleg, den es nicht gibt,
-- waere eine Betriebsausgabe ohne Beleg -- in der EUeR genau die Zahl, die
-- niemand erklaeren kann.
CREATE TRIGGER IF NOT EXISTS zahlung_beleg_existiert
BEFORE INSERT ON zahlung
WHEN NEW.beleg_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM beleg WHERE id = NEW.beleg_id)
BEGIN
    SELECT RAISE(ABORT, 'Zahlung verweist auf einen Beleg, den es nicht gibt');
END;

CREATE TRIGGER IF NOT EXISTS zahlung_beleg_existiert_update
BEFORE UPDATE OF beleg_id ON zahlung
WHEN NEW.beleg_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM beleg WHERE id = NEW.beleg_id)
BEGIN
    SELECT RAISE(ABORT, 'Zahlung verweist auf einen Beleg, den es nicht gibt');
END;

-- ------------------------------------------------- Anlagevermoegen (AVEUeR)

-- Das Anlagenverzeichnis. Pflichtbestandteil der EUeR, sobald Anlagevermoegen
-- existiert (§ 60 Abs. 1 EStDV i.V.m. der Anlage AVEUeR) -- und das ist kein
-- Formalismus: die AfA-Betraege der EUeR kommen von hier, nicht umgekehrt.
--
-- Die beiden Wertspalten sind der Kern und der haeufigste Fehler in
-- selbstgebauten Werkzeugen:
--
-- * ``ak_brutto_cent`` ist der BUCHUNGSWERT. Die nicht abziehbare Vorsteuer
--   gehoert nach R 9b Abs. 1 EStR zu den Anschaffungskosten, also wird der
--   Bruttobetrag abgeschrieben.
-- * ``ak_netto_cent`` dient allein der WERTGRENZE. Die wird nach
--   R 9b Abs. 2 EStR stets netto geprueft -- "Ob der Vorsteuerbetrag
--   umsatzsteuerrechtlich abziehbar ist, spielt in diesem Fall keine Rolle."
--   800 EUR netto sind rund 952 EUR brutto.
--
-- Wer nur eine Spalte fuehrt, prueft die Grenze am Buchungswert und schreibt
-- Gueter ab, die sofort abziehbar waeren -- oder umgekehrt.
CREATE TABLE IF NOT EXISTS anlagegut (
    id              INTEGER PRIMARY KEY,
    beleg_id        INTEGER REFERENCES beleg (id),
    bezeichnung     TEXT    NOT NULL,
    anschaffung_am  TEXT    NOT NULL,
    ak_brutto_cent  INTEGER NOT NULL CHECK (ak_brutto_cent > 0),
    ak_netto_cent   INTEGER NOT NULL CHECK (ak_netto_cent > 0),
    -- Betrieblicher Anteil in Prozent, aus dem Beleg uebernommen. Die AfA
    -- geht nur zu diesem Anteil in die EUeR: ein Rechner, der zu 70 Prozent
    -- betrieblich genutzt wird, bringt 70 Prozent seiner Abschreibung als
    -- Betriebsausgabe (Aufwandseinlage) -- nicht 100.
    aufteilungsquote INTEGER NOT NULL DEFAULT 100
        CHECK (aufteilungsquote BETWEEN 0 AND 100),
    art             TEXT    NOT NULL DEFAULT 'BEWEGLICH'
        CHECK (art IN ('BEWEGLICH', 'IMMATERIELL')),
    nutzungsdauer_jahre INTEGER NOT NULL CHECK (nutzungsdauer_jahre > 0),
    -- GWG_SOFORT: bis 800 EUR netto in einer Summe (§ 6 Abs. 2 EStG).
    -- EIN_JAHR_IT: Computerhardware und Anwendersoftware duerfen mit
    --   Nutzungsdauer 1 Jahr angesetzt werden -- muessen aber TROTZDEM ins
    --   Verzeichnis, und genau das wird gern vergessen.
    -- LINEAR: alles andere ueber die betriebsgewoehnliche Nutzungsdauer.
    methode         TEXT    NOT NULL DEFAULT 'LINEAR'
        CHECK (methode IN ('LINEAR', 'EIN_JAHR_IT', 'GWG_SOFORT')),
    -- Ab 250 EUR netto ist ein GWG gesondert aufzuzeichnen: Tag der
    -- Anschaffung und Anschaffungskosten (§ 6 Abs. 2 Satz 4 EStG). Genau
    -- dafuer steht es hier, auch wenn es sofort abgezogen wird.
    abgang_am       TEXT,
    abgang_grund    TEXT,
    abgang_erloes_cent INTEGER,
    notiz           TEXT,
    erfasst_am      TEXT    NOT NULL,
    CHECK (ak_netto_cent <= ak_brutto_cent),
    CHECK (abgang_am IS NULL OR abgang_am >= anschaffung_am),
    CHECK (methode <> 'EIN_JAHR_IT' OR nutzungsdauer_jahre = 1)
);

CREATE INDEX IF NOT EXISTS idx_anlagegut_jahr ON anlagegut (anschaffung_am);

-- Die AfA je Jahr, einmal gerechnet und festgehalten.
--
-- Als Tabelle und nicht als Formel: die Nutzungsdauer kann sich aendern, ein
-- Gut kann abgehen, und eine spaeter neu gerechnete Reihe wuerde die
-- Vorjahres-EUeR rueckwirkend verfaelschen. Was einmal in einer Steuererklaerung
-- stand, muss nachvollziehbar bleiben.
-- Ein Beleg wird hoechstens EINMAL zum Anlagegut. Zweimal erfasst zaehlte
-- die AfA doppelt -- und zwar jedes Jahr der Nutzungsdauer.
CREATE UNIQUE INDEX IF NOT EXISTS anlagegut_beleg_einmalig
    ON anlagegut (beleg_id) WHERE beleg_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS anlagegut_afa_jahr (
    anlagegut_id INTEGER NOT NULL REFERENCES anlagegut (id),
    jahr         INTEGER NOT NULL,
    afa_cent     INTEGER NOT NULL CHECK (afa_cent >= 0),
    -- 130 bewegliche Wirtschaftsgueter, 131 immaterielle, 132 GWG.
    kennzahl     INTEGER NOT NULL,
    restwert_cent INTEGER NOT NULL CHECK (restwert_cent >= 0),
    PRIMARY KEY (anlagegut_id, jahr)
);

-- ------------------------------------------------------ Steuerruecklage
--
-- Die Angaben zur Schaetzung der Einkommensteuer-Nachzahlung, je Jahr eine
-- Zeile. Gespeichert werden nur EINGABEN -- das Ergebnis haengt vom
-- EUeR-Gewinn ab und wird bei jedem Aufruf neu gerechnet; gespeichert liefe
-- es gegen jede spaetere Buchung.
--
-- Keine Buchfuehrung und kein Teil einer Erklaerung: private Angaben (Gehalt,
-- Veranlagung, Konfession), die das Werkzeug sonst nicht kennt.
--
-- zve_ohne_cent geht dem Bruttolohn vor, wenn beides da ist: das zvE aus dem
-- letzten Steuerbescheid ist genauer als jede Schaetzung aus dem Gehalt.
-- gewinn_erwartet_cent darf negativ sein (Verlust).
CREATE TABLE IF NOT EXISTS steuerschaetzung (
    jahr                  INTEGER PRIMARY KEY CHECK (jahr BETWEEN 2000 AND 2100),
    brutto_lohn_cent      INTEGER CHECK (brutto_lohn_cent IS NULL OR brutto_lohn_cent >= 0),
    partner_brutto_cent   INTEGER CHECK (partner_brutto_cent IS NULL OR partner_brutto_cent >= 0),
    zve_ohne_cent         INTEGER CHECK (zve_ohne_cent IS NULL OR zve_ohne_cent >= 0),
    veranlagung           TEXT    NOT NULL DEFAULT 'EINZEL'
        CHECK (veranlagung IN ('EINZEL', 'ZUSAMMEN')),
    kirchensteuer_prozent INTEGER NOT NULL DEFAULT 0
        CHECK (kirchensteuer_prozent IN (0, 8, 9)),
    -- Ein eigener Gesamtsatz in Promille (325 = 32,5 %); NULL heisst: der
    -- berechnete Satz gilt.
    satz_manuell_promille INTEGER
        CHECK (satz_manuell_promille IS NULL OR satz_manuell_promille BETWEEN 0 AND 1000),
    gewinn_erwartet_cent  INTEGER,
    vorauszahlungen_cent  INTEGER NOT NULL DEFAULT 0 CHECK (vorauszahlungen_cent >= 0),
    hebesatz_prozent      INTEGER NOT NULL CHECK (hebesatz_prozent BETWEEN 200 AND 1000),
    -- 1: mit dem Tarifentwurf des Jahres rechnen (nur, wo es einen gibt).
    tarif_entwurf         INTEGER NOT NULL DEFAULT 0 CHECK (tarif_entwurf IN (0, 1)),
    geaendert_am          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    migriert_am TEXT NOT NULL
);

-- --------------------------------------------- Abgeleitete Status als Sichten
--
-- Sichten werden bei JEDEM Start verworfen und neu angelegt. Mit
-- IF NOT EXISTS waere das Anlegen bei einer bestehenden Datenbank ein stilles
-- No-op: die alte Sicht bliebe stehen und griffe auf Spalten zu, die eine
-- Migration laengst umbenannt hat. Sichten enthalten keine Daten, das Verwerfen
-- kostet also nichts.

DROP VIEW IF EXISTS v_verzicht;
DROP VIEW IF EXISTS v_offene_posten;
DROP VIEW IF EXISTS v_zeiteintrag_status;
DROP VIEW IF EXISTS v_rechnung_status;

-- Zahlstatus und Ueberfaelligkeit sind ZWEI unabhaengige Felder, weil
-- "teilbezahlt" und "ueberfaellig" gleichzeitig wahr sein koennen. Ein einziges
-- Statusfeld muesste sich fuer eines entscheiden und verschweigt dann das andere.
-- date('now','localtime') ist nicht kosmetisch: date('now') liefert UTC und
-- liegt in Deutschland (UTC+1/+2) bis zu zwei Stunden HINTER dem lokalen Datum.
-- Ohne 'localtime' gilt eine Rechnung am Morgen nach dem Faelligkeitstag also
-- noch nicht als ueberfaellig.
CREATE VIEW v_rechnung_status AS
WITH z AS (
    SELECT rechnung_id,
           SUM(betrag_cent) AS bezahlt_cent,
           MAX(datum)       AS letzte_zahlung
      FROM zahlung
     WHERE rechnung_id IS NOT NULL
     GROUP BY rechnung_id
)
SELECT r.id, r.nummer, r.typ, r.kunde_id, r.datum, r.faellig_am, r.versendet_am,
       r.status AS dokumentstatus,
       r.brutto_cent,
       COALESCE(z.bezahlt_cent, 0)                 AS bezahlt_cent,
       r.brutto_cent - COALESCE(z.bezahlt_cent, 0) AS offen_cent,
       z.letzte_zahlung,
       CASE WHEN r.brutto_cent = 0                           THEN 'OHNE_BETRAG'
            WHEN COALESCE(z.bezahlt_cent, 0) >= r.brutto_cent THEN 'BEZAHLT'
            WHEN COALESCE(z.bezahlt_cent, 0) >  0             THEN 'TEILBEZAHLT'
            ELSE 'OFFEN' END                       AS zahlstatus,
       CASE WHEN r.status = 'FESTGESCHRIEBEN' AND r.typ = 'RECHNUNG'
                 AND r.brutto_cent > 0
                 AND COALESCE(z.bezahlt_cent, 0) < r.brutto_cent
                 AND r.faellig_am IS NOT NULL
                 AND r.faellig_am < date('now', 'localtime')
            THEN 1 ELSE 0 END                      AS ueberfaellig,
       CASE WHEN r.faellig_am IS NULL THEN NULL
            ELSE CAST(julianday('now', 'localtime') - julianday(r.faellig_am) AS INTEGER)
       END                                         AS tage_ueber_faellig,
       CASE WHEN r.versendet_am IS NOT NULL THEN 1 ELSE 0 END AS versendet,
       -- Nur die Projektion fuer die Liste. Die Reihenfolge ist Rangfolge.
       CASE WHEN r.status = 'ENTWURF'                        THEN 'ENTWURF'
            WHEN r.status = 'STORNIERT'                      THEN 'STORNIERT'
            WHEN r.typ    = 'STORNO'                         THEN 'STORNO'
            WHEN r.typ    = 'LEISTUNGSNACHWEIS'              THEN 'LEISTUNGSNACHWEIS'
            WHEN r.brutto_cent = 0                           THEN 'OHNE_BETRAG'
            WHEN COALESCE(z.bezahlt_cent, 0) >= r.brutto_cent THEN 'BEZAHLT'
            WHEN COALESCE(z.bezahlt_cent, 0) >  0             THEN 'TEILBEZAHLT'
            WHEN r.faellig_am < date('now', 'localtime')     THEN 'UEBERFAELLIG'
            WHEN r.versendet_am IS NOT NULL                  THEN 'VERSENDET'
            ELSE 'GESTELLT' END                    AS chip
  FROM rechnung r
  LEFT JOIN z ON z.rechnung_id = r.id;

-- Der Status eines Zeiteintrags in der Sprache des Nutzers, vollstaendig
-- abgeleitet. Die Reihenfolge der Zweige ist entscheidend: KULANZ wird VOR
-- BEZAHLT geprueft, sonst erschiene eine Kulanzstunde auf einer bezahlten
-- Rechnung als "Bezahlt", obwohl sie nie einen Cent erzeugt hat -- und fiele
-- aus der Verzichtsauswertung.
CREATE VIEW v_zeiteintrag_status AS
SELECT z.id, z.kunde_id, z.datum, z.titel, z.dauer_minuten, z.abrechnungsmodus, z.grund,
       z.rechnung_id, z.freigegeben_aus_rechnung_id, r.nummer AS rechnung_nummer,
       COALESCE(z.satz_cent_snapshot, k.satz_cent, b.standardsatz_cent, 0) AS satz_cent,
       -- Ungerundeter Wert, nur fuer die Auswertung. Die Rechnung rundet je
       -- Position, deshalb weicht die Summe hier bewusst leicht ab.
       (z.dauer_minuten
        * COALESCE(z.satz_cent_snapshot, k.satz_cent, b.standardsatz_cent, 0) + 30) / 60
                                                                       AS wert_cent,
       CASE
         WHEN z.abrechnungsmodus = 'INTERN'    THEN 'NICHT_ABRECHENBAR'
         WHEN z.abgeschrieben_am IS NOT NULL   THEN 'ABGESCHRIEBEN'
         WHEN z.rechnung_id IS NULL            THEN 'AUFGESCHRIEBEN'
         WHEN rs.dokumentstatus = 'ENTWURF'    THEN 'IM_ENTWURF'
         WHEN rs.dokumentstatus = 'STORNIERT'  THEN 'AUFGESCHRIEBEN'
         WHEN z.abrechnungsmodus = 'KULANZ'    THEN 'OHNE_BERECHNUNG_GESTELLT'
         WHEN rs.zahlstatus = 'BEZAHLT'        THEN 'BEZAHLT'
         ELSE 'IN_RECHNUNG_GESTELLT'
       END AS status,
       rs.zahlstatus   AS rechnung_zahlstatus,
       rs.ueberfaellig AS rechnung_ueberfaellig
  FROM zeiteintrag z
  JOIN kunde k ON k.id = z.kunde_id
  LEFT JOIN betrieb b           ON b.id  = 1
  LEFT JOIN rechnung r          ON r.id  = z.rechnung_id
  LEFT JOIN v_rechnung_status rs ON rs.id = z.rechnung_id;

-- Die Pruefliste, die spaeter den Jahresabschluss sperrt. Jede der drei
-- Bedingungen verhindert eine Falle: Storno-Dokumente und Leistungsnachweise
-- erwarten kein Geld, und eine 0-€-Rechnung wird nie bezahlt -- sie wuerde die
-- Liste ewig blockieren.
CREATE VIEW v_offene_posten AS
SELECT *
  FROM v_rechnung_status
 WHERE dokumentstatus = 'FESTGESCHRIEBEN'
   AND typ = 'RECHNUNG'
   AND brutto_cent > 0
   AND zahlstatus <> 'BEZAHLT';

-- Verschenkte und investierte Arbeit, dreigeteilt. Die Unterscheidung ist der
-- Punkt: NICHT_ABRECHENBAR ist Investition in den eigenen Betrieb und gehoert
-- nicht in die Verschenkt-Summe. OHNE_BERECHNUNG_GESTELLT ist sichtbar
-- verschenkt, der Kunde weiss davon. ABGESCHRIEBEN ist unsichtbar verschenkt --
-- erfahrungsgemaess die unangenehmste Zahl und genau deshalb die, die das Tool
-- zeigen muss.
CREATE VIEW v_verzicht AS
SELECT strftime('%Y', vz.datum) AS jahr,
       vz.status                AS art,
       vz.kunde_id,
       k.name                   AS kunde,
       COALESCE(vz.grund, z.abgeschrieben_grund, '(ohne Grund)') AS grund,
       COUNT(*)                 AS eintraege,
       SUM(vz.dauer_minuten)    AS minuten,
       SUM(vz.wert_cent)        AS wert_cent
  FROM v_zeiteintrag_status vz
  JOIN zeiteintrag z ON z.id = vz.id
  JOIN kunde k       ON k.id = vz.kunde_id
 WHERE vz.status IN ('NICHT_ABRECHENBAR', 'ABGESCHRIEBEN', 'OHNE_BERECHNUNG_GESTELLT')
  -- Gruppierung ueber den Ausdruck, nicht den Alias: 'grund' existiert auch
  -- als Spalte in den verbundenen Tabellen und waere sonst mehrdeutig.
 GROUP BY jahr, art, vz.kunde_id,
          COALESCE(vz.grund, z.abgeschrieben_grund, '(ohne Grund)');

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
    -- § 19 Abs. 1 UStG sagt seit 01.01.2025 "steuerfrei", nicht "wird nicht erhoben"
    hinweis_19_text        TEXT    NOT NULL
        DEFAULT 'Steuerfreie Leistung eines Kleinunternehmers gemäß § 19 Abs. 1 UStG.',
    rundung_intervall_min  INTEGER NOT NULL DEFAULT 15 CHECK (rundung_intervall_min > 0),
    rundung_modus          TEXT    NOT NULL DEFAULT 'KAUFMAENNISCH'
        CHECK (rundung_modus IN ('AUFWAERTS', 'KAUFMAENNISCH', 'ABWAERTS', 'KEINE')),
    rundung_hinweis        TEXT,
    beginn_am              TEXT,
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
    abrechnungsart TEXT    NOT NULL DEFAULT 'STUNDEN'
        CHECK (abrechnungsart IN ('STUNDEN', 'PAUSCHAL', 'OHNE_BERECHNUNG')),
    pauschal_cent  INTEGER,
    -- abrechenbar = 0 heisst: erscheint nie auf einer Rechnung (Akquise, interne Arbeit).
    -- Davon zu unterscheiden: abrechnungsart OHNE_BERECHNUNG erscheint MIT 0,00 € auf
    -- der Rechnung, damit der Kunde die Kulanz sieht.
    abrechenbar    INTEGER NOT NULL DEFAULT 1 CHECK (abrechenbar IN (0, 1)),
    -- Belegt ab Entwurfserstellung, damit kein zweiter Entwurf dieselbe Zeit greift.
    rechnung_id    INTEGER REFERENCES rechnung (id),
    position_id    INTEGER REFERENCES rechnungsposition (id),
    satz_cent_snapshot INTEGER,
    -- Abschluss ohne Rechnung: der Weg fuer Arbeit, die ganz entfallen soll,
    -- ohne eine 0-€-Rechnung zu erzeugen.
    erledigt_am    TEXT,
    erledigt_grund TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    erfasst_am     TEXT    NOT NULL,
    geaendert_am   TEXT,
    CHECK (abrechnungsart <> 'PAUSCHAL' OR pauschal_cent IS NOT NULL),
    CHECK (start_zeit IS NULL OR end_zeit IS NULL OR end_zeit > start_zeit),
    -- Was bereits auf einer Rechnung liegt, kann nicht gleichzeitig ohne Rechnung
    -- erledigt sein.
    CHECK (erledigt_am IS NULL OR rechnung_id IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_zeit_kunde_datum ON zeiteintrag (kunde_id, datum);
CREATE INDEX IF NOT EXISTS idx_zeit_offen ON zeiteintrag (kunde_id, datum)
    WHERE rechnung_id IS NULL AND erledigt_am IS NULL AND abrechenbar = 1;
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
    typ                 TEXT    NOT NULL DEFAULT 'RECHNUNG'
        CHECK (typ IN ('RECHNUNG', 'STORNO', 'GUTSCHRIFT_SELBSTABRECHNUNG', 'ANGEBOT')),
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
    brutto_cent         INTEGER NOT NULL DEFAULT 0 CHECK (brutto_cent >= 0),
    steuerregime        TEXT    NOT NULL DEFAULT 'KU19'
        CHECK (steuerregime IN ('KU19', 'REGEL')),
    hinweis_19_text     TEXT,
    einleitung          TEXT,
    schluss             TEXT,
    rundung_hinweis     TEXT,
    gruppierung         TEXT    NOT NULL DEFAULT 'PRO_TITEL',
    einzelnachweis      INTEGER NOT NULL DEFAULT 0 CHECK (einzelnachweis IN (0, 1)),
    status              TEXT    NOT NULL DEFAULT 'ENTWURF'
        CHECK (status IN ('ENTWURF', 'FESTGESCHRIEBEN', 'STORNIERT')),
    storno_von_id       INTEGER REFERENCES rechnung (id),
    storno_grund        TEXT,
    pdf_pfad            TEXT,
    pdf_sha256          TEXT,
    versendet_am        TEXT,
    festgeschrieben_am  TEXT,
    erstellt_am         TEXT    NOT NULL,
    -- Eine festgeschriebene Rechnung ist vollstaendig oder sie existiert nicht.
    CHECK (
        status <> 'FESTGESCHRIEBEN'
        OR (nummer IS NOT NULL AND datum IS NOT NULL AND festgeschrieben_am IS NOT NULL)
    ),
    CHECK (typ <> 'STORNO' OR storno_von_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_rechnung_kunde ON rechnung (kunde_id, datum);
CREATE INDEX IF NOT EXISTS idx_rechnung_status ON rechnung (status, faellig_am);

CREATE TABLE IF NOT EXISTS rechnungsposition (
    id                  INTEGER PRIMARY KEY,
    rechnung_id         INTEGER NOT NULL REFERENCES rechnung (id) ON DELETE CASCADE,
    pos_nr              INTEGER NOT NULL,
    bezeichnung         TEXT    NOT NULL,
    untertext           TEXT,
    menge_tausendstel   INTEGER NOT NULL,
    einheit             TEXT    NOT NULL DEFAULT 'HUR',
    einzelpreis_cent    INTEGER NOT NULL,        -- 0 ist erlaubt: Kulanzposition
    betrag_cent         INTEGER NOT NULL,
    -- ZUGFeRD-faehig vorgehalten, im KU19-Modus unbenutzt und in der Ausgabe unterdrueckt.
    steuerkategorie     TEXT    NOT NULL DEFAULT 'E',
    steuersatz_promille INTEGER NOT NULL DEFAULT 0,
    quelle              TEXT    NOT NULL
        CHECK (quelle IN ('ZEIT', 'PAUSCHAL', 'FREI', 'OHNE_BERECHNUNG')),
    UNIQUE (rechnung_id, pos_nr)
);

-- Zweite, datenbankseitige Sperre gegen Doppelabrechnung. Das UNIQUE auf
-- zeiteintrag_id greift selbst dann, wenn die Anwendungspruefung versagt.
CREATE TABLE IF NOT EXISTS position_zeiteintrag (
    position_id    INTEGER NOT NULL REFERENCES rechnungsposition (id) ON DELETE CASCADE,
    zeiteintrag_id INTEGER NOT NULL UNIQUE REFERENCES zeiteintrag (id)
);

-- Bemessungsgrundlage der EUeR (§ 11 EStG) und des § 19-Grenzwerts, nicht Komfort.
CREATE TABLE IF NOT EXISTS zahlung (
    id          INTEGER PRIMARY KEY,
    rechnung_id INTEGER REFERENCES rechnung (id),
    beleg_id    INTEGER,                         -- Phase 2
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

CREATE TABLE IF NOT EXISTS backup_log (
    id         INTEGER PRIMARY KEY,
    zeitpunkt  TEXT    NOT NULL,
    pfad       TEXT    NOT NULL,
    groesse    INTEGER,
    sha256     TEXT,
    geprueft   INTEGER NOT NULL DEFAULT 0 CHECK (geprueft IN (0, 1))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    migriert_am TEXT NOT NULL
);

-- --------------------------------------------- Abgeleitete Status als Sichten

-- Zahlungsstand je Rechnung. Eine Rechnung ueber 0 € ist mit dem Festschreiben
-- erledigt: sie erwartet keinen Zahlungseingang und darf die Pruefliste
-- "nicht bezahlt" nicht blockieren.
CREATE VIEW IF NOT EXISTS v_rechnung_status AS
SELECT
    r.id,
    r.nummer,
    r.typ,
    r.status            AS grundstatus,
    r.kunde_id,
    r.datum,
    r.faellig_am,
    r.brutto_cent,
    COALESCE(z.bezahlt_cent, 0) AS bezahlt_cent,
    r.brutto_cent - COALESCE(z.bezahlt_cent, 0) AS offen_cent,
    CASE
        WHEN r.status = 'ENTWURF'          THEN 'ENTWURF'
        WHEN r.status = 'STORNIERT'        THEN 'STORNIERT'
        WHEN r.brutto_cent = 0             THEN 'ABGESCHLOSSEN_OHNE_BETRAG'
        WHEN COALESCE(z.bezahlt_cent, 0) >= r.brutto_cent THEN 'BEZAHLT'
        WHEN COALESCE(z.bezahlt_cent, 0) > 0              THEN 'TEILBEZAHLT'
        WHEN r.faellig_am IS NOT NULL
             AND r.faellig_am < date('now')               THEN 'UEBERFAELLIG'
        ELSE 'GESTELLT'
    END AS status
FROM rechnung r
LEFT JOIN (
    SELECT rechnung_id, SUM(betrag_cent) AS bezahlt_cent
    FROM zahlung
    WHERE rechnung_id IS NOT NULL
    GROUP BY rechnung_id
) z ON z.rechnung_id = r.id;

-- Status eines Zeiteintrags, vollstaendig abgeleitet. In der Sprache des Nutzers:
-- AUFGESCHRIEBEN -> IN_RECHNUNG_GESTELLT -> BEZAHLT, plus die Zustaende, die in
-- dieser Dreierkette fehlen.
CREATE VIEW IF NOT EXISTS v_zeiteintrag_status AS
SELECT
    z.id,
    z.kunde_id,
    z.datum,
    z.titel,
    z.dauer_minuten,
    z.abrechnungsart,
    z.rechnung_id,
    CASE
        WHEN z.abrechenbar = 0        THEN 'NICHT_ABRECHENBAR'
        WHEN z.erledigt_am IS NOT NULL THEN 'ERLEDIGT_OHNE_RECHNUNG'
        WHEN z.rechnung_id IS NULL    THEN 'AUFGESCHRIEBEN'
        WHEN rs.grundstatus = 'ENTWURF'   THEN 'IM_ENTWURF'
        WHEN rs.grundstatus = 'STORNIERT' THEN 'AUFGESCHRIEBEN'
        WHEN rs.status = 'BEZAHLT'        THEN 'BEZAHLT'
        WHEN rs.status = 'ABGESCHLOSSEN_OHNE_BETRAG' THEN 'OHNE_BERECHNUNG_GESTELLT'
        ELSE 'IN_RECHNUNG_GESTELLT'
    END AS status
FROM zeiteintrag z
LEFT JOIN v_rechnung_status rs ON rs.id = z.rechnung_id;

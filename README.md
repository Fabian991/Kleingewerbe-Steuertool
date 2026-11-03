# Steuer-Tool

Rechnungen, Zeiterfassung und EÜR-Ausfüllhilfe für ein Kleingewerbe
(Kleinunternehmer nach § 19 Abs. 1 UStG). Läuft lokal, die Daten bleiben auf dem
eigenen Rechner.

## Einmalig: Voraussetzungen auf dem Mac

```bash
brew install python@3.13 pango poppler
```

Drei Dinge, und jedes aus einem eigenen Grund:

| Paket | wofür | was ohne es passiert |
|---|---|---|
| `python@3.13` | das Werkzeug verlangt **Python 3.12 oder neuer** | das Startskript bricht mit einer klaren Meldung ab |
| `pango` | WeasyPrint rendert das Rechnungs-PDF darüber; die Bibliothek kommt **nicht** über pip | alles läuft, nur das Festschreiben erzeugt kein PDF |
| `poppler` | `pdftotext` und `pdffonts` — nur für die **Tests**, die am fertigen PDF messen | die PDF-Tests werden übersprungen, mit Begründung |

Ohne Homebrew zuerst <https://brew.sh>. Ein Mac bringt zwar ein `python3` mit,
das ist aber meist die Version der Entwicklerwerkzeuge und zu alt.

## Starten

```bash
./start.command          # auf dem Mac auch per Doppelklick im Finder
```

Beim ersten Start wird eine Arbeitsumgebung unter `.venv/` angelegt, die
Abhängigkeiten werden installiert, und der Browser öffnet
`http://127.0.0.1:8737/`. Ein anderer Port geht über
`STEUER_TOOL_PORT=9000 ./start.command`.

Installiert werden dabei auch die Zusatzgruppen aus `pyproject.toml`: `pdf`
(WeasyPrint), `belege` (pypdf, pdfplumber) und auf dem Mac `ocr`
(Apple Vision über ocrmac). Ohne sie startet die Anwendung zwar, erzeugt aber
keine Rechnungs-PDF und liest keine Belege aus. Ändert sich `pyproject.toml`,
zieht der nächste Start die Abhängigkeiten nach — es gibt nichts von Hand zu
aktualisieren.

Fehlt eine Systembibliothek, sagt das Startskript es **beim Start** und nicht
erst bei der ersten Rechnung.

## Wo die Daten liegen

Auf dem Mac unter `~/Library/Application Support/Steuer-Tool/`, sonst im
Projektordner unter `daten/`. `STEUER_TOOL_DATEN` überschreibt den Ort.

```
steuer.sqlite            Datenbank: Kunden, Zeiten, Rechnungen, Zahlungen
ausgangsrechnungen/JJJJ/ die erzeugten Rechnungs-PDFs
belege/JJJJ/             Eingangsbelege im Original (Phase 2)
eingang/                 hochgeladene oder abgeholte Dateien, noch nicht gebucht
kurse/                   EZB-Kurstabelle (Rohdatei, Abrufzeit, Prüfsumme) und unter
                         verwendet/ jede Fassung, mit der ein Beleg umgerechnet wurde
backup/                  Sicherungen
```

Application Support und nicht `~/Documents`: Documents, Desktop und Downloads
stehen unter TCC-Schutz, der Zugriff wird der steuernden Anwendung zugerechnet.
Beim Start per `.command` wäre das Terminal.app, bei einer gebündelten App das
Bundle — beides erzeugt Berechtigungsdialoge, und beim Bundle bricht der Zugriff,
sobald sich die Bundle-Identität ändert. Application Support läuft dialogfrei und
ist von Time Machine standardmäßig erfasst.

**iCloud Drive ist als Ablage der laufenden Datenbank ausgeschlossen.** Defektes
Locking auf Netzwerk-Dateisystemen, Kopieren während laufender Transaktionen und
das Eindünnen von Dateien zu Platzhaltern durch „Mac-Speicher optimieren" sind
dokumentierte Wege zu einer beschädigten SQLite-Datei. Für die Sicherung die
Backup-Funktion der Anwendung benutzen, nicht den Finder: eine Dateikopie bei
laufendem Programm kann eine unbrauchbare Datenbank erzeugen.

## Viele Belege auf einmal

Unter `Belege` → „Beleg aufnehmen“ lassen sich mehrere Dateien auf einmal
auswählen. Eine einzelne öffnet gleich die Prüfmaske, mehrere landen im
Eingang („Im Eingang, noch nicht gebucht“).

**Bezahlt als Vorgabe.** Der Haken „Meine Eingangsbelege sind in der Regel
schon bezahlt“ (Standard: an) sorgt dafür, dass die Zahlung gleich mit der
Buchung entsteht, Zahltag = Belegdatum. In der Prüfmaske ist „bezahlt am“
mit dem Belegdatum vorausgewählt und änderbar; „noch offen“ muss man
ausdrücklich wählen. Die Zahlungsart ist die der letzten Zahlung an denselben
Lieferanten, sonst „Sonstiges“. Am Jahreswechsel (Belegdatum vom 22.12. bis
10.01.) zeigt die Prüfmaske einen Hinweis: für die EÜR zählt nach § 11 EStG
der Tag der Zahlung — bei Lastschrift oder Überweisung im neuen Jahr den
echten Zahltag eintragen. Die automatischen Wege (unten) buchen solche Belege
nicht selbst, sie warten auf die Prüfmaske. Der Zahltag steht im Journal mit
der Vorgabe als Begründung.

**„Alle prüfen und buchen …“** zeigt zuerst eine Vorschau: je Datei „wird
gebucht“ (Lieferant, Datum, Betrag, Kategorie zur Auswahl, Zahltag) oder
„bleibt im Eingang“ mit Grund. Gebucht wird nur, was sicher erkannt ist:
Lieferant, Belegdatum und Bruttobetrag. Ein neuer Lieferant wird mit dem
sicher erkannten Namen angelegt (aus dem XML einer E-Rechnung; aus der
Textschicht eines PDF ist ein unbekannter Name nur geraten, dann bleibt die
Datei). Die Kategorie ist die gemerkte des Lieferanten, sonst die gewählte,
sonst keine — ein Beleg ohne Kategorie steht unter „nur ungeklärte“, und die
EÜR meldet ihn, bis er zugeordnet ist. Im Eingang bleiben: unsichere Felder,
Gutschrift, Storno, Mahnung, Angebot, Kreditkartenabrechnung oder Kontoauszug,
Summen, die nicht aufgehen, ein ähnlicher schon gebuchter Beleg (auch dieselbe
Rechnung zweimal im Stapel, etwa als XML und PDF), alles über 800 € netto,
Anlagegüter, Bewirtung, Geschenke, Reisekosten und Pauschalen, Fremdwährung ohne
sicheren Kurs und — mit Zahlung — der Jahreswechsel. Ein Beleg aus einem
Vorjahr wird gebucht (das ist die Nacherfassung eines Jahres), aber die
Vorschau sagt vorher und das Ergebnis danach, dass seine Zahlung in der EÜR
dieses Jahres zählt — ist die schon abgegeben, ändert sie sich mit. Wer das
nicht will, nimmt den Haken weg oder lässt die Zahlung offen. Gebucht wird wie
beim Eingangsordner automatisch: nicht „geprüft“, bis du „Stimmt so“ klickst. Unter
„nur ungeklärte“ bestätigt „Alle N angezeigten bestätigen …“ alle auf einmal.
Dieselbe Datei, die schon gebucht ist, steht in der Vorschau zum Wegwerfen
vorgehakt.

**„Alle N Dateien wegwerfen …“** löscht nach einer Rückfrage die angezeigten
Dateien aus dem Eingang — nur die Kopie im Tool, gebuchte Belege sind nicht
betroffen. Hochgeladene Originale liegen weiter bei dir, abgeholte im
Eingangsordner unter „Übernommen“; aufbewahrungspflichtige Belege (§ 147 AO)
müssen dort weiter liegen. Das Journal hält Originalname und Prüfsumme fest.

**Nacherfassung eines Jahres.** Wann ein Beleg im Tool erfasst wurde, steht im
Journal und in der Excel-Datei; einen Hinweis „spät erfasst“ gibt es nicht.
Die GoBD nennen zehn Tage unbedenklich und lassen die Erfassung bis zum Ende
des Folgemonats zu — für eine Nacherfassung zählt, dass die Originale geordnet
abgelegt waren.

## Rechnungen vom Handy

Rechnungen, die per Mail kommen, legst du auf dem iPhone in einen Ordner in
iCloud Drive. Beim nächsten Start holt das Tool sie dort ab, liest sie aus und
legt sie an.

**Einmalig auf dem Mac:** `Belege` → „Eingangsordner“ → „Vorschlag
übernehmen“. Das legt in iCloud Drive den Ordner „Steuer-Tool Belege“ an.
Dabei — beim ersten Zugriff auf iCloud Drive — fragt macOS einmal:
*„Terminal“ möchte auf Dateien in iCloud Drive zugreifen* → **Erlauben**.
(Das Tool läuft im Terminal, deshalb fragt macOS für das Terminal.) Wurde das
abgelehnt: Systemeinstellungen → Datenschutz & Sicherheit → Dateien und
Ordner → Terminal → iCloud Drive einschalten, dann auf der Belegseite noch
einmal „Vorschlag übernehmen“ (ist der Ordner schon gespeichert: „Jetzt
abholen“).

**Auf dem iPhone, je Rechnung:**

1. In Mail den Anhang öffnen.
2. Teilen → „In Dateien sichern“.
3. iCloud Drive → „Steuer-Tool Belege“ → Sichern.

Beim nächsten Start läuft das Abholen im Hintergrund — der Start wartet nicht
darauf. Die Übersicht sagt danach in einem Satz, was passiert ist, die
Belegseite im Einzelnen. Zwischendurch geht es mit „Jetzt abholen“.

**Was automatisch gebucht wird.** Nur, was ohne Rückfrage richtig ist: eine
PDF- oder XML-Rechnung (E-Rechnung, ZUGFeRD, XRechnung oder ein PDF mit
Textschicht), bei der Lieferant, Datum und Bruttobetrag sicher erkannt sind,
von einem **bekannten Lieferanten mit gemerkter Kategorie**, in Euro oder in
einer sicher erkannten Fremdwährung (aus dem XML einer E-Rechnung) mit
EZB-Kurs für das Belegdatum, ohne ähnlichen schon
gebuchten Beleg. Für die EÜR zählt nach § 11 EStG der Tag der Zahlung: mit der
Vorgabe „Eingangsbelege sind in der Regel schon bezahlt“ (siehe oben) wird die
Zahlung am Belegdatum gleich mitgebucht — außer am Jahreswechsel (Belegdatum
vom 22.12. bis 10.01.), dort wartet die Rechnung auf die Prüfmaske, weil der
Zahltag das Jahr entscheidet. Ebenso wartet eine Rechnung mit Belegdatum in
einem Vorjahr: ihre Zahlung änderte unbeaufsichtigt eine EÜR, die womöglich
schon abgegeben ist („Alle prüfen und buchen“ bucht sie nach einer Vorschau
mit Warnung). Ohne die Vorgabe wird ohne Zahlung gebucht, und
du erfasst sie in der Liste unter „Ändern“. Ein automatisch gebuchter
Beleg trägt den Hinweis „automatisch gebucht“ und steht unter „nur
ungeklärte“, bis du ihn mit **„Stimmt so“** bestätigst.

**Was in der Prüfmaske wartet.** Alles andere liegt vorbelegt unten im
Eingang, mit dem Grund daneben: Fotos und Scans (die Bilderkennung ist nie
sicher), der erste Beleg eines neuen Lieferanten, unsicher erkannte Felder
(auch ein Datum, neben dem ein Liefer- oder Leistungsdatum steht),
Fremdwährung ohne EZB-Kurs oder nur aus Währungszeichen im Text geraten (ein
„$“ kann auch kanadische oder australische Dollar meinen), ein ähnlicher Beleg (auch unter einem zweiten Datensatz
desselben Lieferanten), alles, was keine gewöhnliche Rechnung ist
(Gutschrift, Storno, Rechnungskorrektur, Angebot, Mahnung, Anzahlungs- oder
Schlussrechnung, ein verrechnetes Guthaben), Belege, deren Summen nicht
aufgehen oder deren USt-IdNr. nicht zum Lieferanten passt, alles über 800 €
netto (möglicherweise ein Anlagegut), Anlagegüter, Reisekosten und
Pauschalen sowie Bewirtung oder Geschenke (die brauchen eigene Angaben). Der
Grund für die Strenge: nach § 146 Abs. 4 AO darf eine Buchung nicht so
verändert werden, dass der ursprüngliche Inhalt nicht mehr feststellbar ist;
das Tool lässt Betrag und Datum eines gebuchten Belegs deshalb nicht ändern —
auch die Datenbank selbst nicht. Kategorie und Quote lassen sich nachträglich
ändern; eine falsche automatische Buchung wird gelöscht und neu gebucht (siehe
„Beleg löschen“ unten). (Einzige Ausnahme: der Euro-Betrag eines Belegs in
Fremdwährung, siehe unten — mit altem und neuem Wert im Journal.)

Damit der zweite Beleg eines Lieferanten automatisch durchläuft: beim ersten
in der Prüfmaske die Kategorie wählen und **„Kategorie und Quote als Vorgabe
für diesen Lieferanten merken“** anhaken — oder später in der Belegliste unter
„Ändern“ beim Speichern der Kategorie „als Vorgabe für … merken“. Wer gar nichts automatisch buchen
lassen will, nimmt auf der Belegseite den Haken „Sicher erkannte Rechnungen
bekannter Lieferanten gleich buchen“ heraus — dann wartet alles.

**Was im Ordner passiert.** Abgeholte Dateien wandern in den Unterordner
„Übernommen“ — erst wenn entschieden ist, ob sie gebucht werden oder warten.
Was kein Beleg ist (etwa eine `.docx`, eine leere Datei), wandert nach „Nicht
übernommen“ und wird nicht bei jedem Start wieder gemeldet. Lässt sich eine
Datei nicht verschieben (im Finder „Geschützt“, keine Schreibrechte), sagt
das die Belegseite einmal mit Grund; danach merkt sich das Tool die Datei und
nimmt sie nicht bei jedem Start neu auf. Ebenso eine Datei, die sich nicht
lesen lässt (keine Leserechte): einmal gemeldet, abgeholt, sobald sie lesbar
ist. Ist der ganze Ordner nicht
beschreibbar, holt das Tool gar nichts ab und sagt, warum. Was iCloud noch nicht auf den Mac
geladen hat oder gerade erst gespeichert wird, bleibt liegen und kommt beim
nächsten Mal dran. Dieselbe Datei zweimal abgelegt wird nicht doppelt gebucht:
das Tool erkennt sie an der Prüfsumme.

**Ein Ordner je Jahr.** Wer seine Belege je Jahr in einem eigenen Ordner
sammelt, hinterlegt unter „Ordner für ein Jahr“ für jedes Jahr einen. Abgeholt
werden alle hinterlegten Ordner — auch die alter Jahre, damit verspätete
Rechnungen nicht liegen bleiben —, bis du einen entfernst; daneben bleibt der
„Ordner für alle Jahre“. Ein schon hinterlegter Ordner wird nicht umgedeutet:
die Belegseite bietet einmal an, ihn „als Ordner für <Jahr> führen“. Ab dem
1. Dezember schlägt das Tool den Ordner für das nächste Jahr vor, abgeleitet aus
dem Pfad des Vorjahres („…/Belege 2026“ → „…/Belege 2027“); angelegt und
hinterlegt wird er nur per Klick. Passt ein Belegdatum nicht zum Jahr des
Ordners, ist das nur ein Hinweis — eine Dezember-Rechnung, im Januar bezahlt
und abgelegt, ist der Normalfall. Wichtig: das Tool holt **jede** Datei der
obersten Ebene ab und verschiebt sie. Liegen in deinem Jahresordner auch
Kontoauszüge oder Bescheide, nimm einen eigenen Unterordner „Eingang“; beim
Hinterlegen eines Ordners, in dem schon Dateien liegen, fragt die Seite nach.
Derselbe Ordner lässt sich nicht zweimal hinterlegen, und Jahresordner dürfen
nicht ineinander liegen.

iCloud Drive ist hier richtig, obwohl es als Ablage der Datenbank
ausgeschlossen ist (siehe oben): der Eingangsordner ist nur ein Postfach. Das
Tool liest dort fertige Dateien und kopiert sie sofort in den lokalen
Datenordner; die Buchführung selbst liegt nie in iCloud. Der Eingangsordner
darf deshalb auch nicht im Datenordner liegen — und kein allgemeiner Ordner
wie Downloads oder der Schreibtisch sein, denn das Tool verschiebt dort jede
Datei, die es abholt.

## Rechnungen, die es nur als E-Mail gibt

Manche Rechnung ist nur eine Mail — eine Bestellbestätigung, eine Quittung —,
manche Mail trägt die Rechnung im Anhang. Beides nimmt das Tool als `.eml`
an, hochgeladen oder über den Eingangsordner. So bekommst du eine `.eml`:

- **Am Mac, Apple Mail:** die Mail aus der Liste in den Finder (oder auf den
  Schreibtisch) ziehen.
- **Gmail im Browser:** Mail öffnen → „⋮“ → „Original herunterladen“.
- **iPhone:** Gmail speichert dort keine `.eml`. „Teilen → Drucken → In
  Dateien sichern“ ergibt ein PDF, und das geht wie jede andere Rechnung.

Ist im Anhang eine echte Rechnung (PDF, XML einer E-Rechnung, Foto), ist
**der Anhang der Beleg**; die Mail wird unverändert mit archiviert.
Verkaufsbedingungen, AGB, Widerrufsbelehrung und Datenschutzhinweise im
Anhang sind keine Rechnung und werden übergangen. Ohne Rechnung im Anhang ist
der Text der Mail der Beleg: Lieferant („Bestellt bei“), Datum, Bestellnummer
und Betrag werden gelesen, aber immer als „bitte prüfen“ — eine Mail wird
deshalb normalerweise nicht automatisch gebucht. Das HTML einer Mail zeigt das
Tool nie an (Zählpixel, nachgeladene Bilder); du siehst eine Textansicht und
kannst das Original herunterladen. Alles bleibt auf dem Mac.

**Teilweise mit Guthaben oder Gutschein bezahlt** (etwa „Store-Guthaben
−45,00 €, Ausstehender Gesamtbetrag 14,99 €“): gebucht wird nur der
tatsächlich bezahlte Betrag. Das Guthaben war meist ein Bonus aus einer
früheren Bestellung; zählte man beide Bestellungen voll, stünde es zu viel in
der EÜR. Ein Vermerk mit beiden Zahlen steht in der Notiz des Belegs.

## Rechnungen in Fremdwährung

Rechnungen in US-Dollar, Pfund oder einer anderen Währung lädst du hoch wie
jede andere. Die Prüfmaske zeigt sofort, was in die EÜR geht: den
**EZB-Referenzkurs** des Tages, das Kursdatum und den Euro-Betrag — etwa
„100,00 USD zum EZB-Referenzkurs 1,1476 vom 13.03.2026 = 87,14 € (1 € = 1,1476
USD)“. Buchen geht gleich, ohne weitere Angabe.

**Welcher Tag zählt.** Für die EÜR zählt der Euro-Betrag, der am Tag der
Zahlung abgeflossen ist (§ 11 Abs. 2 EStG). Steht „Bezahlt am“ in der
Prüfmaske, wird zum Kurs dieses Tages umgerechnet, sonst zum Kurs des
Belegdatums. Erfasst du die Zahlung später mit einem anderen Tag, rechnet das
Tool den Beleg auf den Kurs des Zahlungstags um — Beleg und Zahlung tragen
immer denselben Euro-Betrag, denn die EÜR rechnet mit beiden. Zwei Ausnahmen,
die die Meldung jeweils nennt: Ein Beleg mit Haken „Anlagegut“ bleibt beim
Kurs, mit dem er gebucht ist (er ist die Grundlage der Anschaffungskosten,
auch bevor er im Anlagenverzeichnis steht), und ein Beleg mit Kurs von Hand
bleibt bei diesem Kurs — steht der abgebuchte Betrag fest, trägst du ihn als
Kartenbetrag ein. Die EZB veröffentlicht nur an Geschäftstagen (gegen 16 Uhr):
für Wochenende und Feiertag gilt der letzte Kurs davor, höchstens zehn Tage
zurück. Ist der Kurs des Zahlungstags noch nicht veröffentlicht, sagt das die
Meldung — dann später erfassen oder gleich den Kartenbetrag eintragen.

**Der Betrag laut Kreditkarte.** Er ist genauer als jede Umrechnung, denn in
ihm steckt auch das Auslandseinsatzentgelt der Karte (eine Pauschale dafür
rechnet das Tool bewusst nicht). Du kannst ihn gleich in der Prüfmaske
eintragen oder später in der Belegliste unter „Ändern“ → „Betrag laut
Kreditkarte“, sobald die Abrechnung da ist. Er ersetzt dann den Euro-Betrag
in Beleg, Zahlung und EÜR; der umgerechnete Wert bleibt zum Vergleich stehen,
mit der Differenz in Euro und Prozent. Weicht der Kartenbetrag um mehr als
10 % ab (meist ein Tippfehler, etwa der Dollarbetrag statt des Euro-Betrags),
fragt das Tool nach. „Kartenbetrag entfernen“ nimmt ihn zurück: der
Euro-Betrag von Beleg und Zahlung ist dann wieder der zum EZB-Kurs
umgerechnete. Ebenso ersetzt „Zum EZB-Kurs umrechnen“ einen offline
eingetragenen Kurs von Hand, sobald die Tabelle einen EZB-Kurs für den Tag
hat. Ein Anlagegut, das schon im Anlagenverzeichnis steht, bleibt bei seinen
Anschaffungskosten; das Tool ändert sie nachträglich nicht und sagt es.

**Nachprüfbar.** Je Beleg stehen Währung, Fremdbetrag, Kurs mit allen Stellen
der EZB, Kursdatum, Quelle (EZB-Referenzkurs, Kurs von Hand oder
Kreditkartenbetrag), Abrufzeit und SHA-256 der Kurstabelle in der Belegliste,
im Journal und in der Excel-Tabelle. „Kurs prüfen“ am Beleg — und gesammelt
die Belegintegrität — rechnet jede Umrechnung aus der gespeicherten Tabelle
nach und meldet Abweichungen; geprüft wird gegen genau die Fassung der
Tabelle, mit der gebucht wurde. Fehlt diese Fassung im Datenordner oder passt
ihr Inhalt nicht mehr zu ihrer Prüfsumme, ist das selbst ein Befund
(nachgerechnet wird dann mit der aktuellen Tabelle). Hat die gespeicherte
Tabelle für einen Beleg mit Kurs von Hand doch einen EZB-Kurs (etwa offline
gebucht), meldet die Prüfung das mit beiden Euro-Beträgen — liefert die EZB
einen Kurs, gilt ihrer. Jede Fassung, mit der gebucht wurde, bleibt unter
`kurse/verwendet/` liegen (rund 0,5 MB, eine je Tag, an dem mit einer frisch
geladenen Tabelle gebucht wurde — bei einer Dollar-Rechnung pro Woche gut 25
MB im Jahr) und geht mit in jede Sicherung; die Prüfung liest aus jeder nur
die Spalte der Währung und bleibt so auch nach Jahren schnell.

**Die Kurstabelle.** Das Tool lädt die öffentliche Kurstabelle der EZB selbst
aus dem Internet — beim Start im Hintergrund, wenn sie älter als einen Tag
ist, und wenn ein benötigter Tag fehlt. Dabei verlassen **keine Belegdaten**
den Rechner: abgerufen wird immer dieselbe Datei, ohne Parameter. Eine
geladene Datei ersetzt die vorhandene nur, wenn sie die Prüfung besteht
(Kopfzeile, Datumsformat, plausible Kurse, kein fehlender Geschäftstag) und
nicht weniger enthält als die vorhandene (kein früheres Ende, keine gekürzte
Geschichte, keine fehlende Währung). Ohne Internet geht es mit der vorhandenen
Tabelle weiter. Für eine Währung, die die EZB nicht führt (etwa den
VAE-Dirham), oder wenn ohne Internet keine oder nur eine zu alte Tabelle da
ist, nimmt die Prüfmaske einen **Kurs von Hand** (1 € = … Einheiten); liefert
die EZB einen Kurs, gilt ihrer — die Methode ist stetig anzuwenden. Ist der
Kurs des Tages nur noch nicht veröffentlicht (vor 16 Uhr, über das
Wochenende), nimmt das Tool keinen Kurs von Hand: dann später buchen oder
gleich den Kartenbetrag eintragen. Den Euro-Betrag zum Kurs von Hand zeigt die
Maske schon beim Tippen; sieht der Kurs verdreht aus (0,27 statt 3,67 Dirham
je Euro) oder liegt er weit außerhalb dessen, was Dollar, Pfund, Franken oder
Yen je kosteten, fragt sie vor dem Buchen nach.

**Doppelt hochgeladen.** Dieselbe Datei zweimal erkennt das Tool an der
Prüfsumme. Dieselbe Rechnung als zweites PDF (anderer Export, Kopie aus dem
Kundenportal) erkennt es am Inhalt: gleicher Lieferant mit gleicher
Belegnummer, oder gleiches Datum mit gleichem Betrag — bei Fremdwährung
gleiche Währung und gleicher Betrag in der Währung, auch wenn die erste mit
dem Kartenbetrag und die zweite zum EZB-Kurs umgerechnet wäre. Gesucht wird
auch unter einem anderen Lieferanten-Datensatz („AWS“ und „Amazon Web
Services EMEA SARL“) und bei Belegen ohne Lieferant, dort enger: gleicher
Betrag und dazu gleiche Belegnummer oder gleiches Datum (zwei verschiedene
Belegnummern heißen zwei Rechnungen). Die Prüfmaske warnt dann und bucht nur
mit „trotzdem buchen“; aus dem Eingangsordner wird so eine Rechnung nie
automatisch gebucht.

Die Umsatzsteuer nach § 13b UStG (Reverse Charge bei ausländischen
Dienstleistern) berechnet das Tool nicht; es merkt sich nur den Vermerk.

## Beleg löschen, Zahlung berichtigen

In der Belegliste unter **„Ändern“**:

**Beleg löschen.** Ein Testbeleg, eine doppelt erfasste oder eine falsch
ausgelesene Rechnung: „Beleg löschen …“ aufklappen, einen Grund wählen
(„Testbeleg“, „doppelt erfasst“, „falsch ausgelesen“ oder eigene Worte) und
bestätigen. Der Beleg verschwindet aus der Liste, aus EÜR, Übersicht,
Steuerrücklage, den Excel-Blättern „EÜR“ und „Buchungen“, aus
Anlagenverzeichnis und AfA, aus den offenen Hinweisen und aus der
Dublettenprüfung. Gelöscht heißt dabei **markiert**, nicht weg: Datensatz,
Zahlung und Originaldatei bleiben (schreibgeschützt im Archiv, acht Jahre
Aufbewahrung nach § 147 AO), das Journal hält den ganzen Beleg mit Zahlungen
und Auslesung fest. So verlangen es § 146 Abs. 4 AO und die GoBD (Rz. 107 ff.,
„Löschmerker“ Rz. 110): was gebucht war, darf nicht spurlos verschwinden.

- **Neu hochladen geht.** Dieselbe Datei lässt sich nach dem Löschen wieder
  hochladen und buchen — die Prüfmaske sagt dazu, welcher Beleg sie vorher
  war. Mit dem Haken „Datei gleich wieder zum Prüfen in den Eingang legen“
  öffnet sich die Prüfmaske sofort. Liegt die neue Buchung am selben
  Belegdatum, verweist sie auf dieselbe Archivdatei. Auch „Alle prüfen und
  buchen“ im Eingang bucht so eine Datei wieder, statt sie als Dublette zum
  Wegwerfen vorzuhaken.
- **Anlagegut.** Hängt am Beleg ein Anlagegut, zählt das Formular seine
  AfA-Jahre und Beträge auf; gelöscht wird nur mit dem Haken „Anlagegut mit
  löschen“. Stand AfA schon in einem zurückliegenden Jahr, steht die
  womöglich in einer abgegebenen EÜR — dann die Erklärung berichtigen.
  Dasselbe sagt das Tool, wenn die Zahlung eines gelöschten Belegs in einem
  Vorjahr lag.
- **Wiederherstellen.** Der Schalter „gelöschte zeigen“ über der Liste zeigt
  gelöschte Belege gekennzeichnet, mit Zeitpunkt und Grund; unter
  „Wiederherstellen“ kommen sie zurück (gegen den Fehlklick). Nicht, wenn
  dieselbe Datei inzwischen neu gebucht ist — sonst stünde die Rechnung
  zweimal in der EÜR.

**Zahltag ändern, Zahlung zurücknehmen.** Bei einem bezahlten Beleg mit
genau einer Zahlung: „Zahltag ändern“ setzt den Tag des Abflusses neu (die
Ausgabe wandert damit womöglich in ein anderes EÜR-Jahr), „Zahlung
zurücknehmen …“ macht den Beleg wieder offen, falls er doch noch nicht bezahlt
war — in zwei Schritten wie bei einer Rechnungszahlung: aufklappen, dann
bestätigen, mit einem freiwilligen Grund fürs Protokoll. Beides steht mit dem
alten Wert im Journal; liegt der alte oder neue Tag in einem Vorjahr, warnt
die Meldung (beim Zurücknehmen schon vor dem Bestätigen). Bei Fremdwährung
folgt der Euro-Betrag: umgerechnet wird zum EZB-Kurs des neuen Zahltags,
nach dem Zurücknehmen wieder zu dem des Belegdatums — außer bei
Kartenbetrag, Kurs von Hand und Anlagegut. Das gilt nur für Belegzahlungen
(Ausgaben): eine Zahlung auf eine Rechnung wird nie geändert, sondern per
Gegenbuchung zurückgenommen (siehe „Wenn etwas falsch ist“).

## Import aus WISO Steuer

Für den Parallelbetrieb: wer seine Buchhaltung (noch) in WISO Steuer führt,
spielt den Export immer wieder ein — jede Woche, jeden Monat, am Jahresende.
In WISO unter **Buchungen → Exportieren → „Buchungen als CSV-Datei“** die
Datei des Jahres speichern (`;`-getrennt, mit der Kopfzeile
`Buchungsnummer;Teilbuchung;Zahlungsdatum;…`), dann im Tool **„WISO-Import“**
→ Datei wählen. Angenommen werden nur `.csv` bis 2 MB, in UTF-8 (mit oder
ohne BOM) oder Windows-1252; eine als `.csv` umbenannte Excel-Datei, Steuer-
und Nullzeichen werden abgewiesen. Eine kaputte Zeile hält die anderen nicht
auf, die Vorschau nennt sie mit Zeilennummer (ein Semikolon im Buchungstext:
„19 statt 18 Felder“; ein Anführungszeichen bleibt Teil des Texts). Wird
dieselbe Vorschau in einem zweiten Fenster bestätigt, sagt das Tool „schon
übernommen“ und zeigt den Abgleich.

**Vorschau, dann übernehmen.** Die Vorschau schreibt nichts. Sie gleicht
jede Buchung über **Jahr, Buchungsnummer und Teilbuchung** gegen das ab, was
frühere Importe angelegt haben, und über eine Prüfsumme der fachlichen
Felder (Zahltag, Text, Konten, Betrag, Belegnummer), ob sie sich in WISO
seither geändert hat. Gruppen:

- **neu** — wird angelegt; Ausgaben als Beleg *ohne Datei* („aus WISO
  übernommen · Datei fehlt“, bezahlt am WISO-Zahltag), Einnahmen als
  Einnahme ohne Rechnung, eine Anschaffung auf einem Anlagenkonto als Beleg
  mit Anlagegut.
- **verknüpfen** — im Tool steht schon ein Eintrag mit demselben Betrag,
  höchstens 7 Tage entfernt und ähnlichem Namen (ein Beleg, eine Zahlung auf
  eine eigene Rechnung, eine Einnahme). Vorschlag: verknüpfen statt doppelt
  anlegen; abwählbar. Ist der Beleg im Tool noch unbezahlt, entsteht die
  Zahlung am WISO-Zahltag mit. Ein Beleg mit Zahlungsziel (ähnlicher Name,
  bis 45 Tage) wird nur angeboten, nicht vorbelegt. Wurde eine Buchung in
  WISO gelöscht und neu erfasst (neue Nummer), hängt „verknüpfen“ den
  vorhandenen Eintrag auf die neue Nummer um.
- **bekannt** — schon übernommen, unverändert: nichts zu tun. Ein zweiter
  Import derselben Datei legt **nichts** an.
- **in WISO geändert** — Wahl: „übernehmen“ (der alte Eintrag bekommt die
  Löschmarkierung mit Grund, ein neuer entsteht, eine angehängte Datei geht
  mit) oder „ignorieren“ (die Änderung wird nicht noch einmal gemeldet).
  Vorgabe: zurückstellen; „ignorieren“ bei reiner Textänderung und wenn der
  Eintrag im Tool schon gelöscht ist.
- **in WISO gelöscht** — Wahl: „löschen“ (Löschmarkierung) oder „behalten“.
- **in WISO umgebucht** — schon übernommen, in WISO aber auf Privat, AfA
  oder einen Zahltag in der Zukunft umgebucht: Wahl „im Tool löschen“ oder
  „im Tool behalten“. Vorgabe: zurückstellen — der Abgleich nennt den Grund.
- **Privat** (Konten 1800 ff.) und **AfA-Zeilen** werden nicht übernommen;
  die AfA rechnet das Tool selbst aus dem Anlagenverzeichnis und vergleicht
  sie mit WISO.

Übernommen wird in **einer** Transaktion, nach erneutem Lesen und Planen
unter der Schreibsperre: hat sich seit der Vorschau etwas geändert, wird
nichts geschrieben und die neue Vorschau gezeigt. Die Datei liegt danach
schreibgeschützt mit Prüfsumme unter `importe/wiso/<Jahr>/` im Datenordner
(und damit in jeder Sicherung); jede angelegte Buchung verweist auf sie.

**Konten und Zahler.** Die SKR03-Konten sind vorbelegt (4600 Werbung, 4673
Reisekosten, 4855 GWG — ein Software-Titel dort wird „Software-Kauf“ —,
4900/4980 Übrige, 4945 Fortbildung …); ein unbekanntes Konto bekommt in der
Vorschau eine Kategorie, die sich das Tool **merkt**. Ein Haben auf einem
Aufwandskonto ist eine **Erstattung** (Einnahme, kein Umsatz). Einnahmen
werden **je Zahler** eingestuft: Inland (zählt zur Kleinunternehmergrenze),
Ausland (bekannte Plattformen wie Amazon, YouTube/Google, Udemy, Apple:
§ 3a Abs. 2 UStG, zählt nicht), Verkauf eines Anlageguts oder Erstattung.
Auch das wird gemerkt — „YouTube Ads“ und „Youtube Ads“ sind ein Zahler —, und
zwar unter demselben Schlüssel wie bei der Erfassung von Hand: die Wahl gilt
in beide Richtungen. „Verkauf“ zählt nur als eigenes Wort am Anfang oder Ende
(„Rad Beispiel - Verkauf“, nicht „Verkaufsprovision“), auch vor einem
Plattformnamen („Amazon Echo - Verkauf“ ist ein eigener Zahler). Erstattungen
stehen in KZ 111 und nachrichtlich in KZ 119: KZ 111 minus KZ 119 ist der
§ 19-Umsatz.

**Anlagegüter.** Bei einer Anschaffung in der Datei leitet das Tool die
Nutzungsdauer aus der WISO-AfA-Zeile ab (1.200 € im April, 300 € AfA →
3 Jahre); sie lässt sich auf der Seite Anlagen ändern, solange keine AfA
eines abgeschlossenen Jahres betroffen ist. Eine Verkaufszeile wird eine
Einnahme „Verkauf eines Anlageguts“ (KZ 102); beim Abgang des Guts auf der
Seite Anlagen diese Einnahme als Erlös wählen — dann zählt er nur einmal.
Die Seite nennt Verkäufe, zu denen noch kein Abgang erfasst ist.

**Abgleich.** `WISO-Import → Abgleich` stellt je Bereich (Einnahmen,
Erstattungen, Ausgaben je Kategorie, AfA, Gewinn) die Summe laut WISO der
laut Tool gegenüber und nennt den Grund jeder Differenz — „im Tool direkt
gebucht, nicht in WISO“, „in WISO, im Tool nicht übernommen“, „Angaben
fehlen“.

### Einnahmen ohne Rechnung

Affiliate-Auszahlungen, Werbeerlöse, Plattform-Abrechnungen: **„Einnahmen“**
→ Tag des Zuflusses, Betrag, Zahler, Kategorie (oder „nach Zahler“: die
gemerkte bzw. vorgeschlagene) und, wenn es eine gibt, die Abrechnung als
Datei. Sie zählen in die EÜR (Kennzahl der Kategorie, „ohne eigene
Rechnung“), Inland auch in den § 19-Zähler. Betrag, Tag und Zahler sind fest;
falsch erfasst heißt löschen (Markierung mit Grund, wiederherstellbar) und
neu erfassen. Die Kategorie lässt sich umstellen. Steht dieselbe Einnahme
schon da (gleicher Betrag, ± 7 Tage, ähnlicher Zahler — etwa aus WISO), fragt
das Tool nach, bevor es sie ein zweites Mal zählt; ebenso eine Zahlung auf
eine eigene Rechnung („Zahlung buchen und diese Einnahme ersetzen“). Eine
Abrechnung als XML erscheint als Text, eine Mail als Textansicht.

### Dateien zuordnen, Datei nachträglich anhängen

Ein Beleg aus WISO hat keine Datei, eine Einnahme aus WISO oder von Hand
keine Abrechnung. Das Original kommt an den vorhandenen Eintrag, nicht als
zweiter Beleg — der zählte die Ausgabe in der EÜR doppelt:

- **je Eintrag:** Belegliste → „Ändern“ → „Datei anhängen“ (Filter „nur aus
  WISO ohne Datei“); Einnahmen → „Ändern“ → „Abrechnung anhängen“;
- **über den Eingang:** den ganzen Belegordner hochladen und „Alle prüfen
  und buchen“. Die Vorschau ist die Zuordnungsseite: der Abschnitt „Im
  Eingang“ auf der Belegseite nennt den Einstieg („N Dateien im Eingang ·
  M Buchungen ohne Datei · Zuordnen“). Auch die Prüfmaske einer einzelnen
  Datei fragt („Passt zu einer vorhandenen Buchung?“, Radio: an Beleg X
  anhängen / an Einnahme Y anhängen / neu buchen; die Wahl ist Pflicht).

**Die Regeln.** Der Name allein reicht nicht — WISO sagt „Claude“, die
Rechnung kommt von „Anthropic, PBC“. Deshalb zählen Betrag und Datum zuerst,
der Name nur als Bonus:

- Pflicht: **gleicher Euro-Bruttobetrag** auf den Cent, und das Datum der
  Datei (ihr Belegdatum) höchstens **45 Tage** vom Belegdatum oder Zahltag
  des Eintrags entfernt (Zahlungsziel, Kreditkartenabrechnung, Jahresabo vor
  der Verlängerung, Vorauskasse mit Rechnung nach der Lieferung). Kandidaten
  sind alle Belege ohne Datei (nicht nur aus WISO; Eigenbelege ausgenommen)
  und alle Einnahmen ohne Datei.
- Rang: der Datumsabstand, klein zuerst; ein ähnlicher Name (Lieferant,
  Buchungstext, Zahler) wiegt eine Woche Abstand auf, bei einer Gutschrift
  ebenso eine Einnahme (eine Gutschrift ist Geld, das kommt). Bei Gleichstand
  der ältere Eintrag.
- **sicher** — genau ein Kandidat, und der liegt höchstens 7 Tage entfernt
  oder sein Name passt: in der Vorschau vorausgewählt **und angehakt**.
  **zur Bestätigung** (wahrscheinlich) — genau ein Kandidat, weiter weg, Name
  passt nicht: vorausgewählt, nicht angehakt. **mehrdeutig** — mehrere
  Kandidaten (etwa Monatsabos mit gleichem Betrag): nicht angehakt, der
  nächstliegende vorausgewählt, alle zur Wahl.
- Ein ganzer Stapel wird **global** zugeordnet: alle (Datei, Eintrag)-Paare
  nach Rang, das beste zuerst, jede Datei und jeder Eintrag nur einmal. Drei
  Rechnungen eines Monatsabos fallen so auf den jeweils nächsten Eintrag;
  ein Eintrag, der schon einer anderen Datei des Stapels zugeteilt ist, zählt
  für die Stufe nicht mehr als Alternative („für „rechnung.pdf“ vorgesehen“).
- **Nie still doppelt:** hat eine Datei irgendeinen Kandidaten, bucht „Alle
  prüfen und buchen“ sie **nicht** als neuen Beleg. Sie steht in der Gruppe
  „zuordnen“ mit Dropdown (Kandidaten mit Datum, Betrag, Lieferant oder
  Zahler, Buchungstext, Abstand in Tagen, „Name passt“; dazu „neu buchen“
  und „liegen lassen“), oben die Zusammenfassung („12 sicher, 3 zur
  Bestätigung, 1 mehrdeutig, 5 ohne Treffer werden neu gebucht“) und der
  Knopf „Alle sicheren Zuordnungen übernehmen“ (nur diese, nichts Neues
  buchen). Das automatische Abholen aus dem Eingangsordner lässt eine solche
  Datei warten („passt vielleicht zu Beleg X / Einnahme Y — im Eingang
  zuordnen“).
- Beim Klick wird jede Datei neu gelesen und der gewählte Eintrag neu
  geprüft: er muss noch passen und noch ohne Datei sein, sonst bleibt die
  Datei mit Meldung im Eingang.

Betrag und Datum des Eintrags bleiben; weicht die Datei ab (bei WISO-Belegen
steht dort der Zahltag), sagt die Meldung es. Die Datei wird archiviert wie
jede: schreibgeschützt, mit Prüfsumme und Journal (`BELEG_DATEI_ANGEHAENGT`,
`EINNAHME_DATEI` mit Vermerk „aus dem Eingang“).

### Angaben fehlen

Was ein Import nicht weiß, steht als Liste auf der Übersicht („Angaben
fehlen: N“) und unter `/angaben` — je Eintrag genau die Felder, die fehlen:

- **Anlagegut aus einem Vorjahr:** WISO zeigt nur die Jahres-AfA. Bis
  Anschaffungstag und -kosten da sind, rechnet das Tool keine AfA; EÜR und
  Steuerrücklage sagen „AfA für Kamera fehlt (WISO: 400 €)“. Ausgefüllt
  rechnet es selbst und vergleicht mit WISO. Bleibt die Nutzungsdauer leer,
  leitet das Tool sie aus der WISO-AfA ab — nur wenn genau eine passt; sonst
  nennt es die passenden („1 Jahr oder 3 Jahre“) und fragt. Die Jahre vor
  der WISO-AfA stehen nur zur Übersicht, die Dauer bleibt korrigierbar.
- **Konto ohne Kategorie** und **Zahler ohne Einstufung:** ihre Buchungen
  warten; nach der Angabe entstehen sie aus der abgelegten Importdatei.

## Entwicklung

```bash
python3 -m pytest -q      # Tests
ruff check . && ruff format --check .
```

## Nicht von diesem Werkzeug abhängig sein

Jede Sicherung enthält drei Dinge, die sich gegenseitig stützen: die Datenbank,
die Belege und Rechnungs-PDF im Original, und **alle Daten als Tabelle
(`steuer-daten.xlsx`)** — Beträge als Euro-Zahlen, Datumsangaben als Datum,
Fremdschlüssel im Klartext daneben. Die SQLite-Datei nützt nur, solange es ein
Programm gibt, das sie öffnet; die Tabelle öffnet jeder Rechner. Die
Aufbewahrungsfrist läuft gestaffelt (§ 147 Abs. 3 AO): **zehn Jahre** für Bücher
und Aufzeichnungen, **acht** für Buchungsbelege wie Rechnungen und Belege, sechs
für den Rest. Das Archiv enthält beides, also gilt die längere — und sie läuft
nicht ab, solange die Festsetzungsfrist offen ist.

Die Tabelle gibt es auch einzeln: `/backup` → „Tabelle herunterladen".

## EÜR

`/euer` zeigt die Einnahmenüberschussrechnung je Jahr, aufgeschlüsselt nach den
**Kennzahlen der Anlage EÜR** — die Zahlen, die in ELSTER einzutragen sind.
Dieselben Werte stehen im Excel-Export auf den Blättern „EÜR" und „Buchungen",
letzteres mit einer Zeile je Geldfluss.

Zwei Abgrenzungen, die leicht durcheinandergehen:

- **§ 11 EStG**: gezählt wird der Tag, an dem das Geld fließt, nicht das
  Rechnungs- oder Belegdatum.
- **Auslandsumsätze sind Betriebseinnahme**, zählen aber nicht in den
  § 19-Gesamtumsatz (§ 19 Abs. 2 i. V. m. § 1 Abs. 1 Nr. 1 UStG). EÜR und
  Umsatzzähler laufen hier auseinander; die Differenz steht nachrichtlich in
  Kennzahl 119.

Die Spalte „Sachkonto" im Blatt „Buchungen" bleibt leer: einen Kontenrahmen
(SKR03/SKR04) rät dieses Werkzeug nicht. Sobald ein Steuerberater ihn nennt,
ist der DATEV-Buchungsstapel daraus eine Abbildungstabelle.

## Steuerrücklage

`/steuer` schätzt, wie viel Einkommensteuer das Gewerbe zusätzlich kostet —
und damit, wie viel vom Gewinn zur Seite gehört. Auf das Gehalt ist die Steuer
über die Lohnsteuer schon bezahlt; offen ist nur, was der Gewinn obendrauf
kostet:

    Steuer auf (Einkommen ohne Gewerbe + Gewinn) − Steuer auf (Einkommen ohne Gewerbe)

mit Solidaritätszuschlag und Kirchensteuer, bei Zusammenveranlagung mit dem
Splittingtarif, und über 24.500 € Gewinn mit Gewerbesteuer (nach Hebesatz der
Gemeinde) abzüglich ihrer Anrechnung auf die Einkommensteuer (§ 35 EStG). Der
Gewinn kommt aus der EÜR; ein erwarteter Jahresgewinn lässt sich eintragen.
Das Einkommen ohne Gewerbe ist entweder aus dem Bruttogehalt geschätzt
(gesetzlich versichert, ohne Kinder) oder — genauer — das zu versteuernde
Einkommen aus dem letzten Steuerbescheid. Bei Zusammenveranlagung gehört das
Bruttogehalt des Partners dazu (0, wenn er oder sie keines bezieht); leer
rechnet die Seite nicht, denn Splitting gegen einen Partner ohne Einkommen
wäre die günstigste Annahme und die Rücklage deutlich zu niedrig.

Die Seite zeigt den Satz auf den Gewinn (Durchschnitt) und den
Grenzsteuersatz, eine Faustregel „Cent je eingenommenem Euro“ und die
Abstufungen des Tarifs mit der Stufe, in der das Einkommen liegt. Wer lieber
mit einem eigenen Satz rechnet, trägt ihn ein — er gilt dann für alles
zusammen, und dafür braucht es kein Gehalt (Rücklage = Gewinn × Satz).
Die Rücklage wird auf volle 10 € aufgerundet. Liegt ein eingetragener
erwarteter Jahresgewinn unter dem Gewinn laut EÜR, warnen Seite und
Übersicht. Fürs laufende Jahr steht eine Hochrechnung daneben (bisheriger
Gewinn ÷ angefangene Monate × 12), nur als Angebot. Die Angaben werden je Jahr gespeichert (Tabelle `steuerschaetzung`,
jede Änderung im Journal) und lassen sich je Jahr wieder löschen; das
Ergebnis wird jedes Mal neu gerechnet. Zum Jahreswechsel zeigt die Übersicht
neben dem neuen Jahr auch die noch offene Rücklage des Vorjahres.

**Vorauszahlungen** stehen je Termin da, wie im Bescheid — auch für Termine,
die noch kommen: vier an das **Finanzamt** (10.03., 10.06., 10.09., 10.12.;
ein Betrag je Termin für Einkommensteuer, Soli und Kirchensteuer zusammen,
damit die Kirchensteuer nicht doppelt zurückgelegt wird) und vier an die
**Gemeinde** (Gewerbesteuer, 15.02., 15.05., 15.08., 15.11.; eingeklappt,
solange es keine Gewerbesteuer gibt). Fällt ein Termin auf ein Wochenende
oder einen bundesweiten Feiertag, ist die Rate am nächsten Werktag fällig
(§ 108 Abs. 3 AO). Eine fällige Rate gilt als gezahlt — sie wird am
Fälligkeitstag abgebucht —, eine künftige als offen; je Termin übersteuert
ein Haken das („noch nicht gezahlt“ bzw. „schon gezahlt“). Die große Zahl
„mindestens zurücklegen“ ist das Größere aus *Steuer auf den Gewinn minus
schon gezahlt* und *Summe der noch fälligen Raten*: das Finanzamt bucht die
festgesetzte Rate ab, auch wenn die Schätzung darunter liegt. Darunter steht
die Aufteilung — was wann noch abgebucht wird und was voraussichtlich mit dem
Bescheid kommt, exakt in Cent. Liegen die Raten um mindestens 100 € je Termin
über der Schätzung, empfiehlt die Seite die Herabsetzung (§ 37 Abs. 3 EStG
bzw. § 19 Abs. 3 GewStG); liegen sie deutlich darunter, warnt sie vor
Nachzahlung und Zinsen (§ 233a AO). „Beträge aus dem Vorjahr übernehmen“
füllt die Raten aus dem Vorjahr ins Formular (ein Bescheid gilt fort, bis ein
neuer kommt). Das frühere Summenfeld bleibt als „Weitere gezahlte
Vorauszahlungen ohne Termin“ (etwa eine nachträgliche Erhöhung); ein dort
gespeicherter Wert rechnet unverändert weiter. Die Übersicht nennt ab 14
Tagen vorher die nächste Rate und jede, die als nicht gezahlt markiert ist.
Gespeichert in der Tabelle `steuer_vorauszahlung` (Migration 23), jede
Änderung im Journal; gebucht wird nichts — Einkommen-, Kirchensteuer und Soli
sind private Steuern (§ 12 Nr. 3 EStG), die Gewerbesteuer ist keine
Betriebsausgabe (§ 4 Abs. 5b EStG).

**Eine Schätzung, keine Steuerberatung.** Nicht berücksichtigt: andere
Einkünfte, Sonderausgaben über die Pauschalen hinaus, außergewöhnliche
Belastungen, Kinderfreibeträge, Steuerklasse III/V (die Nachzahlung kann dann
höher sein), Kirchensteuer-Kappung.

Tarif, Soli-Freigrenze, Sozialversicherungswerte und Gewerbesteuer-Werte
stehen mit Fundstelle in `app/stammdaten/steuertarif.json` (Rechtsstand
25.10.2026). Für 2027 gilt der Tarif 2026 fort, solange kein neuer verkündet
ist; der Regierungsentwurf (BT-Drs. 21/8235) lässt sich als vorläufige
Variante zuschalten. Ein Jahr ohne eigene Werte rechnet mit dem
nächstliegenden und sagt das auf der Seite. Nach jeder Verkündung im
Bundesgesetzblatt gehört die Datei neu geprüft.

## Kunden

Der Rechnungsempfänger kommt immer aus dem Kundenstamm (`/kunden`), nie als
Freitext. Pflicht sind Name und vollständige Anschrift (§ 34a Satz 1 Nr. 1
UStDV). Was erst beim Festschreiben rot würde — Reverse Charge ohne USt-IdNr.,
ein Unternehmer im Ausland mit Leistungsort Inland, eine Privatperson mit
Reverse Charge —, steht schon auf der Kundenseite als Hinweis. Eine von Hand
eingegebene Kundennummer wird über ihren Zähler geprüft: „K-1“ ist „K-0001“.

**Kunden importieren** (`/kunden/import`): Vorlage herunterladen
(`Kundenvorlage.xlsx`, dieselben Felder wie das Formular, Auswahllisten,
Blatt „Hilfe“), je Kunde eine Zeile ausfüllen, hochladen. Eine CSV-Datei
(UTF-8 oder Windows-1252, `;` oder `,`) mit den Überschriften der Vorlage
geht auch, ebenso das Blatt „Kunden“ aus dem eigenen Excel-Export. Die
Vorschau zeigt je Zeile, was passiert: *wird angelegt*, *schon vorhanden*
(gleiche Kundennummer oder USt-IdNr. — vorhandene Kunden werden nie
überschrieben), *gleicher Name wie K-…* (nur mit Haken „trotzdem anlegen“)
oder *fehlerhaft* mit Spalte und Grund. Erst der Knopf „N Kunden anlegen“
schreibt — in einer Transaktion, mit Journaleintrag je Kunde und einem
Eintrag `KUNDENIMPORT` mit der Prüfsumme der Datei. Dabei wird die Datei noch
einmal gelesen und geprüft; hat sich der Kundenstamm seit der Vorschau
geändert, kommt eine neue Vorschau statt eines Imports. Die hochgeladene
Datei liegt bis dahin nur im Speicher (höchstens eine Stunde), nicht im
Datenordner. Gelesen werden nur Werte: Formeln, Makros (`.xlsm`), das alte
`.xls`, mehr als 2 MB oder mehr als 1.000 Zeilen weist der Import ab.

**Stilllegen oder löschen** (unten auf der Kundenseite): Ein Kunde, der auf
keinem festgeschriebenen Dokument steht (Rechnung, Storno,
Leistungsnachweis), lässt sich endgültig löschen. Seine offenen,
abgeschriebenen und internen Zeiten und seine Rechnungsentwürfe gehen mit —
die Rückfrage zählt sie auf, und das Protokoll hält den vollständigen
Kundendatensatz samt Zeiten und Entwürfen fest. Seine Kundennummer wird
wieder frei, wenn sie die höchste war: sie stand auf keinem Beleg. Ausnahme:
Stand sie auf einer zurückgenommenen Rechnung (deren PDF aufbewahrt ist und
deren Nummer womöglich als Lücke im Protokoll steht), bleibt sie verbraucht —
sonst verwiese diese Rechnung später auf einen anderen Kunden. Steht der
Kunde auf einem festgeschriebenen Dokument, bleibt er (§ 147 AO: die
Rechnungen werden zehn Jahre aufbewahrt, und mit ihnen der Kunde, auf den sie
verweisen) — stattdessen wird er **stillgelegt**: er verschwindet aus der
Kundenliste (`?alle=1` zeigt ihn) und aus der Auswahl für neue Zeiten und
Rechnungen, seine Rechnungen bleiben unverändert. Ein Trigger in der
Datenbank sperrt das Löschen eines solchen Kunden auch auf jedem anderen Weg.

## Der Weg von der Zeit zur Rechnung

**Beträge ohne Zeiterfassung** — die Affiliate-Provision zum Monatsende,
eine Pauschale, eine weiterberechnete Auslage — kommen als **freie
Position** auf die Rechnung: auf der Entwurfsseite unter „Freie Position
hinzufügen“ (Leistung, Menge, Einheit, Einzelpreis, optional eine
Beschreibung mit der Herleitung). Eine Rechnung, die nur aus solchen
Positionen besteht, beginnt im Assistenten mit „Rechnung ohne Zeiten“: der
Entwurf entsteht leer mit Leistungszeitraum, die Positionen folgen. Freie
Positionen bleiben bei jeder Neuberechnung der Zeitpositionen stehen und
stehen hinter ihnen; „pauschal“ druckt die Zeile ohne Menge und Einzelpreis.

1. **Zeiten** erfassen — Timer oder nachträglich. Dauer als `1:45`, `1h45`,
   `1,75` oder `105`.
2. Auf dem **Dashboard** auf „Nicht abgerechnet" klicken. Der Assistent öffnet
   mit gesetztem Kunden und allen offenen Zeiten vorgewählt.
3. **Gruppierung** wählen. Die Summe jedes Modus steht daneben, weil je Position
   gerundet wird und sich die Summen deshalb unterscheiden.
4. **Entwurf erstellen und Vorschau ansehen.** Die Entwurfsseite zeigt das PDF
   so, wie es beim Festschreiben entsteht — dieselbe Vorlage, derselbe
   Renderer —, mit dem Wasserzeichen „ENTWURF – keine Rechnung“ und ohne
   Nummer. Die Vorschau schreibt nichts: kein Journal, keine Nummer, keine
   Datei. Ein Entwurf lässt sich folgenlos verwerfen.
5. **Prüfung** lesen. Rot blockiert, Gelb wird einzeln abgehakt und landet im
   Journal, Blau ist Hinweis.
6. **Festschreiben.** Erst jetzt wird die Nummer gezogen, der Kundenstand in die
   Rechnung kopiert, das PDF erzeugt und gehasht. Danach ist die Rechnung
   unveränderbar.
7. **Versendet** und **bezahlt** vermerken — oben auf der Rechnungsseite
   (Ablaufleiste), in der Rechnungsliste oder in der Übersicht unter „Offene
   Rechnungen“, je mit „Bezahlt …“ / „Versendet …“. „Bezahlt“ ist kein Schalter:
   der Status folgt aus der erfassten Zahlung, und ihr Datum (Tag des
   Geldeingangs) ist die Grundlage der EÜR und des Umsatzzählers, nicht das
   Rechnungsdatum. Betrag leer = offener Rest.

### Timer mit Pause

Der Timer kennt **Pause** und **Fortsetzen**. Gespeichert wird jede
Arbeitsphase einzeln mit Beginn und Ende, sekundengenau in UTC (Tabelle
`zeit_abschnitt`, im Excel-Export das Blatt „Zeitabschnitte“). Pausen werden
nicht gespeichert, sie sind die Lücken dazwischen: die Aufzeichnung belegt, wann
gearbeitet wurde, statt eine Pausenlänge zu behaupten. Die Dauer ist die Summe
der Abschnitte, **einmal** auf Minuten gerundet — je Abschnitt gerundet käme mit
jeder Pause bis zu eine Minute dazu. Über Mitternacht und über die Zeitumstellung
rechnet das richtig (25.10.2026 01:30–03:30 sind 180 Minuten).

- **Stoppen aus der Pause:** der Eintrag endet mit dem Beginn der Pause, nicht
  mit dem Klick.
- **Neuer Timer, während einer pausiert ist:** der pausierte endet ebenso mit dem
  Beginn seiner Pause.
- **Fortsetzen an einem anderen Tag** (abends pausiert, morgens weiter): es
  entsteht ein neuer Eintrag für heute mit Kunde, Titel und Beschreibung; der
  alte endet mit seiner Pause. Sonst stünde Arbeit von heute unter gestern.
- Jedes Pausieren und Fortsetzen steht im Journal (`TIMER_PAUSE`,
  `TIMER_WEITER`). Wird die Dauer später von Hand geändert, bleiben die
  Abschnitte als Aufzeichnung stehen, und die Liste zeigt „korrigiert“.

Zur Einordnung (keine Rechtsberatung): für einen Selbständigen gilt kein
Arbeitszeitgesetz. Die Zeitaufzeichnung ist aber Grundlage der Rechnung — also
Unterlage nach § 147 AO, die nach § 146 Abs. 4 AO nachvollziehbar bleiben muss —
und im Streit muss der Leistende die abgerechneten Stunden darlegen. Darauf ist
das Modell zugeschnitten: Beginn, Ende und jede Phase stehen fest, gerundet
wird nie in den Rohdaten, und auf einer festgeschriebenen Rechnung sind Zeit,
Titel, Beschreibung und Abschnitte gesperrt.

### Beschreibung

Die Beschreibung ist ein mehrzeiliges, aufziehbares Feld (höchstens 2.000
Zeichen) — beim Erfassen, beim Timer-Start, im Timer-Banner (Pause, Fortsetzen
und Stoppen speichern sie mit) und unter „Text ändern“ in der Liste. Titel und
Beschreibung lassen sich ändern, solange die Zeit auf keiner festgeschriebenen
Rechnung liegt; liegt sie in einem Entwurf, wird der Entwurf neu berechnet. Auf
der Rechnung steht die Beschreibung einer Position aus **einem** Eintrag
ungekürzt; eine zusammengefasste Position zeigt die Beschreibungen gekürzt an
der Wortgrenze, die Einzelheiten stehen im Einzelnachweis.

### Rundung je Kunde

Gerundet wird die **Zeitsumme je Rechnungsposition**, nie der einzelne Eintrag.
Die Regel (aufwärts, kaufmännisch, abwärts oder minutengenau, mit Takt) steht in
den Einstellungen als Vorgabe und lässt sich je Kunde abweichend einstellen —
samt dem Vermerk, wo sie vereinbart ist („Angebot vom …“). Eine Rundung, die nur
auf der Rechnung steht, ist keine Vereinbarung.

- **Sichtbar vorher:** die Meldung nach dem Stoppen sagt, wie abgerechnet wird,
  und die Zeitenliste zeigt je Kunde „0:52 erfasst → 1:00 abrechenbar“ —
  gerechnet mit derselben Funktion wie die Rechnung.
- **Ein Entwurf behält seine Regel:** sie wird beim Anlegen eingefroren
  (`rechnung.rundung_modus`, `rundung_intervall_min`). Ein gestoppter Timer,
  eine geänderte Dauer oder eine andere Gruppierung rechnen den Entwurf mit
  dieser Regel neu, nie still mit einer inzwischen geänderten. Weicht die Regel
  am Kunden ab, sagt die Entwurfsseite es und bietet „Regel des Kunden
  übernehmen“ an.
- **Der Satz auf der Rechnung** beschreibt, was geschah: „Die Zeitsumme je
  Position ist auf volle 15 Minuten aufgerundet.“ Wird nicht gerundet (Regel
  „minutengenau“ oder Gruppierung „pro Eintrag“), steht kein Rundungssatz da.
  Festgeschriebene Rechnungen behalten ihren gedruckten Satz.

## Wenn etwas falsch ist

Eine festgeschriebene Rechnung lässt sich nicht ändern — sie ist ein Beleg, und
§ 146 Abs. 4 AO verlangt, dass ihr ursprünglicher Inhalt feststellbar bleibt.
Sie wird ersetzt. Auf der Rechnungsseite unter **„Etwas falsch?“** gibt es drei
Wege:

- **Korrigieren** (der Normalfall): in einem Schritt wird die Rechnung mit einer
  Rechnungskorrektur (eigene Nummer, nie „Gutschrift“) storniert, und es entsteht
  ein neuer Entwurf mit denselben Positionen, Texten und Zeiten, Rechnungsdatum
  heute. Positionen und Stundensatz kommen 1:1 von der alten Rechnung; weicht
  der Satz am Kunden inzwischen ab, sagt der Entwurf das und rechnet auf Klick
  mit dem neuen Satz. Das PDF der neuen Rechnung trägt den Satz „Diese Rechnung
  ersetzt Rechnung Nr. X vom TT.MM.JJJJ (storniert mit Nr. S).“, und alle drei
  Seiten verweisen aufeinander. Ist die alte Rechnung schon bezahlt, bleibt die
  Zahlung dort („Rückzahlung offen“) — eine Umbuchung gibt es nicht.
- **Zurücknehmen, solange sie niemand erhalten hat**: nur ohne Versandvermerk,
  ohne Zahlung und ohne Rechnungskorrektur, mit Grund und ausdrücklicher
  Bestätigung („Diese Rechnung hat niemand erhalten“). Danach ist sie wieder ein
  **Entwurf** (Vorbelegung) oder **ganz gelöscht** (die Zeiten werden frei). War
  ihre Nummer die zuletzt vergebene ihres Kreises, wird sie wieder frei, und die
  nächste Rechnung bekommt sie. Wurde danach schon eine weitere Nummer vergeben,
  bleibt sie als **Lücke** verbraucht — der Dialog sagt das vor dem Bestätigen,
  und die Liste der Nummernlücken (Rechnungen → Nummernlücken, auch im
  Excel-Export) nennt sie mit Grund und Datum. Nichts verschwindet spurlos: der
  vollständige Inhalt steht im Protokoll (`RECHNUNG_ZURUECKGENOMMEN`), das PDF
  wird nicht gelöscht, sondern schreibgeschützt als `…_VERWORFEN_<Zeitstempel>.pdf`
  unter `ausgangsrechnungen/verworfen/` aufbewahrt und kommt mit in jede
  Sicherung. Ein versehentlicher Versandvermerk lässt sich mit Grund
  zurücknehmen (oder umdatieren); danach greift das Zurücknehmen wieder.
- **Nur stornieren**, ohne neuen Entwurf — wie bisher.

Ein **Entwurf** hat noch keine Nummer und wird einfach gelöscht („Entwurf
löschen“ oben auf der Entwurfsseite oder in der Rechnungsliste); das hinterlässt
keine Lücke, und das Protokoll hält fest, was im Entwurf stand.

Eine **falsch erfasste Zahlung** wird in der Zahlungsliste der Rechnung
zurückgenommen: eine Gegenbuchung zum selben Datum hebt sie auf, beide Zeilen
bleiben sichtbar und ergeben zusammen null — in Zahlstatus, offenem Posten, EÜR
und § 19-Umsatz. Lag die Zahlung in einem Vorjahr, weist die Seite darauf hin,
dass die EÜR dieses Jahres evtl. schon abgegeben ist und berichtigt werden
müsste. Rechnungszahlungen selbst lassen sich (auch per Skript) weder ändern
noch löschen.

## Die Rechnung auf Papier

Das Layout folgt DIN 5008 Form B (Fensterumschlag DIN lang):

- **Briefkopf** (0–45 mm): der Name groß, rechts Anschrift, Telefon und E-Mail.
  Ist ein Inhaber eingetragen und steht er nicht schon im Namen, heißt es
  „Inhaber · Name“ — ein Personenname soll erkennbar sein.
- **Anschriftfeld** (ab 45 mm, 85 × 45 mm): oben die Zusatz- und Vermerkzone mit
  der Rücksendeangabe, ab 62,7 mm die Empfängeranschrift. Die Rücksendeangabe
  steht im Fenster und wird nie abgeschnitten — ist sie zu lang, wird sie kleiner
  und bricht zwischen ihren Teilen um.
- **Informationsblock** rechts ab 50 mm: Nummer, Datum, Kundennummer,
  Leistungszeitraum, bei Reverse Charge die USt-IdNr. des Empfängers.
- **Fußzeile** in drei Spalten: Name und Anschrift; Kontakt und Steuerkennung;
  Kontoinhaber, IBAN, BIC und Bank. IBAN und BIC stehen je in einer Zeile.
- Falz- und Lochmarken an der Papierkante, nur auf Seite 1.

**Steuerkennung:** § 34a Satz 1 Nr. 2 UStDV verlangt eine von drei — gedruckt
wird genau eine, und nur einmal: die USt-IdNr. vor der Kleinunternehmer-IdNr.
vor der Steuernummer. Wer eine USt-IdNr. hat, hat die Steuernummer also nicht
mehr auf der Rechnung. Bei Reverse Charge genügt die Kleinunternehmer-IdNr.
nicht (§ 14 Abs. 4 Satz 1 Nr. 2 UStG): die Prüfung meldet das rot, gedruckt
wird USt-IdNr. oder Steuernummer.

**Kontoinhaber:** der Inhaber, sonst der Name des Betriebs. Seit Oktober 2025
gleicht die Bank des Kunden den Empfängernamen mit der IBAN ab — der Inhaber
gehört deshalb so eingetragen, wie die Bank das Konto führt.

**Alte Rechnungen** werden nie neu gerendert: das bei der Festschreibung
abgelegte PDF ist das Original, sein SHA-256 steht in der Datenbank. Nur wenn
die Datei fehlt, erzeugt „PDF jetzt erzeugen“ sie neu — mit dem neuen Layout,
aber aus dem Schnappschuss von damals (Name, Anschrift, Kennung, Bank).

## Aufbau

```
app/schema.sql              Schema samt Sperren und abgeleiteten Status
app/db.py                   Verbindung, versionierte Migration, Journal
app/filter.py               Anzeigefilter, von Oberfläche und PDF gemeinsam genutzt
app/pdf.py / pdfwerk.py     PDF-Erzeugung im Subprozess
app/logik/rundung.py        Abrechnungsrundung, Beträge
app/logik/nummernkreis.py   Rechnungsnummern, Vergabe, Lückenerkennung
app/logik/zeiten.py         Dauereingabe, Timer
app/logik/kunden.py         Kundennummern, Satzermittlung, Prüfung der Kundenangaben
app/logik/kundenimport.py   Kundenimport: Vorlage, Lesen, Vorschau, Anlegen
app/logik/gruppierung.py    Zeiten → Rechnungspositionen, vier Modi
app/logik/validierung.py    Pflichtfeld- und Plausibilitätsprüfung (rot/gelb/blau)
app/logik/festschreiben.py  Entwurf, Festschreiben, Storno, Daten für PDF und Vorschau
app/logik/rechnungsdruck.py Was das PDF aus dem Absender ableitet: Name, Kennung, Kontoinhaber
app/logik/rechnungsablauf.py  Korrigieren, Zurücknehmen vor dem Versand,
                            Zahlung zurücknehmen, Nummernlücken
app/logik/grenzwert.py      Kleinunternehmergrenzen
app/logik/euer.py           Einnahmenüberschussrechnung je Kennzahl
app/logik/steuertarif.py    Einkommensteuertarif, Soli, Kirchen- und Gewerbesteuer je Jahr
app/logik/steuerschaetzung.py  Steuerrücklage: Mehrsteuer durch das Gewerbe
app/logik/backup.py         Sicherung, Rückprüfung, Zweitkopie
app/logik/export_excel.py   Alle Daten als .xlsx — der Notausgang
app/logik/wiso.py           Import aus WISO Steuer: Lesen, Abgleich, Übernahme, Abgleich-Summen
app/logik/einnahmen.py      Einnahmen ohne Rechnung, Einstufung je Zahler
app/logik/angaben.py        „Angaben fehlen“: Anlagegut, Konto, Zahler nachtragen
app/logik/beleg/            Belege: Auslesung (XML, Textschicht, Apple Vision, E-Mail),
                            Buchen, Eingangsordner (abholen.py, jahresordner.py),
                            „Alle prüfen und buchen“ (sammelbuchung.py), bezahlt als
                            Vorgabe (bezahlt.py), Löschen (loeschen.py),
                            Zahlung berichtigen (belegzahlung.py), Datei
                            nachträglich anhängen (anhaengen.py)
app/routen/                 HTTP-Routen je Bereich
app/vorlagen/               Oberfläche und Rechnungsvorlage (Jinja2)
```

## Grundregeln, die im Code durchgehalten werden

- **Geld ist immer Integer-Cent**, Dauer Integer-Minuten, Menge
  Integer-Tausendstel. Keine Fließkommazahlen, nirgends.
- **Gerundet wird auf der Summe je Rechnungsposition**, nie pro Zeiteintrag.
  Sieben Einträge à 7 Minuten sind 49 Minuten, auf 15 Minuten aufgerundet 60
  (kaufmännisch 45) — je Eintrag aufgerundet wären es 105.
- **Die Rechnungsnummer wird erst beim Festschreiben gezogen.** Verworfene
  Entwürfe hinterlassen dadurch keine Lücke.
- **Format und Zählweise der Nummer werden ins neue Jahr vererbt.** Ein Format
  ohne Jahresanteil zwingt zum durchlaufenden Zähler — sonst vergäbe das nächste
  Jahr dieselben Nummern noch einmal. Ein durchlaufender Kreis hält genau eine
  Zeile (`jahr = 0`), damit die im Januar nachgetragene Dezemberrechnung nicht
  in einen zweiten Zähler gerät.
- **Festgeschriebene Rechnungen sind unveränderbar** (§ 146 Abs. 4 AO),
  erzwungen durch Datenbank-Trigger. Korrektur über Storno und Neuausstellung
  („Korrigieren“). Die einzige Ausnahme ist das Zurücknehmen einer nie
  versendeten Rechnung ohne Zahlung: der Trigger lässt den Rückweg zum Entwurf
  nur mit einer Freigabe zu (Tabelle `loeschfreigabe`), die ausschließlich
  innerhalb dieser einen Transaktion existiert, deren Bedingungen die Datenbank
  selbst prüft und die den Inhalt (Datum, Kunde, Beträge) nicht freigibt. Der
  volle alte Stand steht vorher im Journal, das PDF bleibt aufbewahrt.
- **Zahlungen auf Rechnungen werden nie geändert oder gelöscht.** Eine
  Fehlbuchung hebt eine Gegenbuchung zum selben Datum auf (Trigger).
- **Gebuchte Belege werden nie physisch gelöscht**, nur als gelöscht markiert
  (mit Grund, im Journal). Betrag und Datum sind per Trigger fest,
  Belegdateien unlöschbar. Jede Auswertung liest über die Sichten
  `v_beleg_gueltig` und `v_anlagegut_gueltig`; ein Wächtertest sorgt dafür,
  dass ein neuer Zugriff den Filter nicht vergisst.
- **Fremddaten werden beim Festschreiben in die Rechnung kopiert** (Snapshot).
  Ein Kundenumzug verändert deshalb keine Altrechnung.
- **Niemals ein Umsatzsteuerfeld.** Ein Kleinunternehmer, der Umsatzsteuer
  ausweist, schuldet sie nach § 14c Abs. 1 UStG — auch bei 0,00 €.
- **Status werden abgeleitet, nicht gespeichert.** „Aufgeschrieben",
  „In Rechnung gestellt" und „Bezahlt" ergeben sich aus der
  Rechnungsverknüpfung und dem Zahlungsstand. Ein zweiter gespeicherter Status
  würde unweigerlich gegen den ersten laufen.
- **Jahresabhängige Werte gehören in `app/stammdaten/*.json`**, nie in den Code:
  EÜR-Zeilen und -Bezeichnungen, Pauschalen, Wertgrenzen.
- **Das PDF wird nach dem Commit geschrieben**, nie in der Transaktion. Ein
  Rollback entfernt keine Datei; sie läge als Waise im Ordner — womöglich mit
  einer Nummer im Namen, die danach an eine andere Rechnung geht.
- **Im PDF ist nur der Briefkopf absolut positioniert.** Ein absolut
  positionierter Block paginiert nicht; bei 60 Positionen fiele der Rest lautlos
  aus dem Dokument.
- **Jede Schemaänderung braucht einen Schritt in `db.MIGRATIONEN`**, und der
  bringt sein DDL selbst mit. `schema.sql` beschreibt den heutigen Zielzustand
  und legt nur neue Datenbanken an.

Die rechtlichen Fundstellen stehen als Kommentar an der Stelle, an der die Regel
durchgesetzt wird — meist in `app/schema.sql`.

## Lizenz

MIT, siehe `LICENSE`. Die mitgelieferten Schriften Liberation Sans stehen
unter der SIL Open Font License 1.1 (`app/vorlagen/schriften/OFL.txt`); die
Python-Abhängigkeiten werden beim Start installiert und tragen ihre eigenen,
durchweg freizügigen Lizenzen (MIT, BSD, Apache 2.0, PSF).

Das Werkzeug ersetzt keine steuerliche Beratung. Wer es einsetzt, bleibt für
seine Aufzeichnungen und Erklärungen selbst verantwortlich.

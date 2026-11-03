# Schriften im Projekt

## Warum die Datei hier liegt und nicht auf dem Rechner bleibt

Eine Rechnung ist acht Jahre aufbewahrungspflichtig (§ 147 Abs. 3 AO für
Buchungsbelege, § 14b Abs. 1 UStG für Rechnungen), und die Frist läuft nicht ab,
solange die Festsetzungsfrist offen ist. Verlässt sich die PDF-Vorlage auf eine
Systemschrift, hängt das Layout eines aufbewahrungspflichtigen Belegs davon ab,
welche Schriften dieser Mac in acht Jahren hat — und davon, ob er überhaupt noch
dieser Mac ist. Fehlt die Schrift,
setzt WeasyPrint still auf eine Ersatzschrift um: andere Laufweite, andere
Umbrüche, im schlimmsten Fall ein verschobenes Anschriftfeld, das nicht mehr im
Fensterbereich sitzt.

Eine nachträglich erzeugte Kopie einer Rechnung soll genauso aussehen wie die
versendete. Deshalb reist die Schrift im Projekt mit.

Das PDF selbst ist davon unabhängig: WeasyPrint bettet die benutzten Zeichen
ein. Diese Dateien braucht nur das *Erzeugen* eines PDF, nicht das Lesen eines
bereits erzeugten.

## Liberation Sans

* **Quelle:** <https://github.com/liberationfonts/liberation-fonts>, hier
  übernommen aus dem Debian-Paket `fonts-liberation` 1:2.1.5-3
* **Version:** 2.1.5
* **Urheber:** Steve Matteson für Ascender Corp., gepflegt von Red Hat
* **Lizenz:** SIL Open Font License 1.1 — der vollständige Text liegt in
  `OFL.txt` und muss bei jeder Weitergabe mitgehen
* **Reserved Font Name:** „Liberation"

Die Dateien sind **unverändert** übernommen. Das ist Absicht: eine Unterteilung
auf die benötigten Zeichen wäre deutlich kleiner, wäre nach OFL aber eine
*Modified Version* und müsste deshalb umbenannt werden. Eine umbenannte Schrift
unbekannter Herkunft in einem Beleg, den in acht Jahren jemand prüft, ist das
schlechtere Geschäft als 1,2 MB im Repository.

Prüfsummen der übernommenen Dateien (SHA-256, gekürzt):

| Datei | sha256 |
|---|---|
| LiberationSans-Regular.ttf | `4659bc0c58c5028d…` |
| LiberationSans-Bold.ttf | `3973aa5054fb467d…` |
| LiberationSans-Italic.ttf | `830c5fa600505fb4…` |

### Warum Liberation Sans

Liberation Sans ist **metrisch kompatibel mit Arial**: gleicher Vorschub je
Zeichen, gleiche Zeilenumbrüche. Die bisherigen Rechnungen aus Word sind in
Arial 11 pt gesetzt — die neuen sehen damit aus wie die alten. Beim Umstieg ist
das kein Schönheitsargument: der Kunde soll die Rechnung wiedererkennen und
nicht für eine Fälschung halten.

Vorher lag hier Carlito (metrisch wie Calibri). Das war eine Vermutung über die
Word-Vorlage, und die echte Rechnung hat sie widerlegt.

Dazu deckt Liberation Sans alles ab, was eine deutsche Rechnung braucht —
Umlaute, ß, €, §, deutsche Anführungszeichen, Gedankenstriche — und hat einen
Kursivschnitt, den der Kulanzhinweis benutzt.

Kein Fettkursiv-Schnitt: die Vorlage kombiniert beides nicht. Käme der Fall
dazu, gehört `LiberationSans-BoldItalic.ttf` hier hinzu, statt WeasyPrint
schräg stellen zu lassen.

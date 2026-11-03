# Prüfdateien

Keine davon ist ein echter Beleg. Alle sind nachgebaut, enthalten erfundene
Nummern und Beträge, und liegen hier, weil die Belegauslesung nur an
realistischen Dateien prüfbar ist: ein Text, den der Test selbst zusammensetzt,
beweist nur, dass der Test und die Auslesung dieselbe Vorstellung von einer
Rechnung haben.

| Datei | Was sie prüft |
|---|---|
| `schema_v1.sql` | Das echte Schema in Version 1, aus Commit `cd0eee7` gezogen. Ein nachgebautes Schema würde genau die Migrationslücken nicht finden, auf die es ankommt. |
| `schema_v25.sql` | Das echte Schema in Version 25 (Pakete A bis F), aus Commit `28b08a3` gezogen. Darauf baut `tests/altbestand_v25.py` den Bestand vor dem WISO-Import (Migration 26); `test_upgrade_v25.py` prüft, dass dabei kein Datensatz verloren geht oder sich ändert. |
| `zugferd_cii.xml` | Stufe 1, CII-Syntax (ZUGFeRD, Factur-X). Echte Namensräume und echte Verschachtelung, inklusive zweier `SpecifiedTaxRegistration` — Steuernummer und USt-IdNr. stehen dort an derselben Stelle und werden nur durch `schemeID` unterschieden. |
| `xrechnung_ubl.xml` | Stufe 1, UBL-Syntax (XRechnung). Dieselbe Norm, ganz andere Elementnamen. Trägt zusätzlich Reverse Charge (`TaxCategory/ID = AE`) und einen `PayableAmount`, der wegen einer verrechneten Anzahlung vom `TaxInclusiveAmount` abweicht. |
| `lieferantenrechnung_de.pdf` | Stufe 2, deutsches Zahlenformat. Die Beträge gehen arithmetisch auf (6,97 + 1,32 = 8,29 bei 19 %), die Kreuzprüfung muss sie also eindeutig finden. |
| `lieferantenrechnung_en.pdf` | Stufe 2, **englisches** Zahlenformat (`1,234.56`) auf einem Beleg im AWS-Stil, mit Reverse-Charge-Vermerk. Der Fall, der still falsch geht: mit deutschen Regeln gelesen wären das 1,23 €. |
| `eurofxref-auszug.csv` | Die Kurstabelle der EZB im Originalformat (`Date,USD,...`, abschließendes Komma, `N/A`), echte Referenzkurse vom 26.02. bis 10.04.2026 für sechs Währungen — darin ein Wochenende, Ostern (Karfreitag und Ostermontag ohne Kurs) und RUB ganz ohne Kurs wie in der echten Tabelle seit 2022. **Abgewandelt:** ISK ab dem 04.03.2026 auf `N/A` gesetzt, für den Fall „letzter Kurs mehr als zehn Tage alt“. Die Tests packen sie selbst in ein ZIP, wie die EZB sie ausliefert; ins Netz geht kein Test. |

Die beiden PDF sind mit WeasyPrint aus HTML erzeugt und haben deshalb eine
echte Textschicht — genau wie eine digital erzeugte Lieferantenrechnung. Ein
Scan oder Foto liegt hier **nicht**: die Bilderkennung läuft nur auf macOS und
wird dort mit einem Handyfoto abgenommen. Die Tests zu Stufe 3 arbeiten mit
aufgezeichneten Vision-Ausgaben (Textkästen mit Koordinaten) und prüfen damit
den Weg von der erkannten Textmenge zu den Feldern, nicht die Erkennung selbst.

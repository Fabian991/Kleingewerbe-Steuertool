#!/bin/bash
# Steuer-Tool starten.
#
# Auf dem Mac genuegt ein Doppelklick auf diese Datei: der Finder oeffnet das
# Terminal und fuehrt sie aus. Zum Beenden das Terminalfenster schliessen oder
# Strg+C druecken.
#
# Falls macOS den Start verweigert ("Entwickler nicht verifiziert"), einmal
# per Rechtsklick > Oeffnen starten, danach merkt sich das System die Freigabe.

set -euo pipefail
cd "$(dirname "$0")"

PORT="${STEUER_TOOL_PORT:-8737}"

# Ein frueherer Stand richtete einen Hintergrunddienst bei launchd ein
# (~/Library/LaunchAgents/de.steuer-tool.dienst.plist). Der ist wieder
# ausgebaut; eine liegen gebliebene Anmeldung wuerde sonst alle 30 Sekunden
# vergeblich starten. Einmalig aufraeumen, dann ist Ruhe.
ALTER_DIENST="$HOME/Library/LaunchAgents/de.steuer-tool.dienst.plist"
if [ -f "$ALTER_DIENST" ] && command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/de.steuer-tool.dienst" >/dev/null 2>&1 || true
  rm -f "$ALTER_DIENST"
  echo "Der fruehere Hintergrunddienst wurde abgemeldet und entfernt."
fi

laeuft_schon() {
  command -v curl >/dev/null 2>&1 \
    && curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/gesund" 2>/dev/null \
       | grep -q '"schema_version"'
}
if laeuft_schon; then
  # start.command ein zweites Mal geoeffnet: nur den Browser oeffnen. Ein
  # zweiter Server scheiterte ohnehin am belegten Port -- und haette vorher
  # womoeglich den Eingangsordner abgeholt.
  echo "Steuer-Tool laeuft schon auf http://127.0.0.1:${PORT}/ -- es wird nur der Browser geoeffnet."
  command -v open >/dev/null 2>&1 && open "http://127.0.0.1:${PORT}/"
  # Das Fenster hat seinen Zweck erfuellt. Terminal liesse es sonst mit
  # "[Prozess beendet]" stehen. nohup: Terminal schickt der endenden
  # Sitzung ein SIGHUP, das den Nachzuegler sonst mitnaehme.
  if command -v osascript >/dev/null 2>&1; then
    nohup bash -c 'sleep 0.5; osascript -e "tell application \"Terminal\" to close (every window whose name contains \"start.command\")"' >/dev/null 2>&1 &
  fi
  exit 0
fi
VENV=".venv"
# Woran die Arbeitsumgebung haengt. Aendert sich eine dieser Dateien, werden
# die Abhaengigkeiten nachgezogen -- sonst laeuft die Anwendung nach einem
# Update mit den Paketen von vorgestern weiter und scheitert an einer Stelle,
# die mit dem eigentlichen Problem nichts zu tun hat.
STEMPEL="$VENV/.stand"

# Die Zusatzgruppen aus pyproject.toml. OHNE sie startet die Anwendung zwar,
# erzeugt aber KEIN PDF (weasyprint fehlt) und liest keine Belege aus
# (pypdf/pdfplumber fehlen) -- und das faellt erst bei der ersten Rechnung auf.
EXTRAS="pdf,belege"
if [ "$(uname -s)" = "Darwin" ]; then
  # Apple Vision gibt es nur auf macOS.
  EXTRAS="$EXTRAS,ocr"
fi

fehler_fenster() {
  echo
  echo "$1"
  echo
  echo "Fenster bleibt offen, damit die Meldung lesbar ist."
  read -r -p "Mit Enter schliessen."
  exit 1
}

# pyproject.toml verlangt Python 3.12. Ein Mac bringt unter /usr/bin/python3 die
# Version der Entwicklerwerkzeuge mit, oft 3.9 -- und die ist meist die erste im
# PATH, auch wenn laengst eine neuere danebenliegt. Deshalb wird GESUCHT statt
# aufgegeben: alle ueblichen Namen und Orte durchprobiert und der erste genommen,
# der die Anforderung erfuellt.
taugt() {
  [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    >/dev/null 2>&1
}

PYTHON=""
for kandidat in \
  python3.14 python3.13 python3.12 python3 \
  /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
  /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 \
  /usr/local/bin/python3.14 /usr/local/bin/python3.13 \
  /usr/local/bin/python3.12 /usr/local/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
do
  if taugt "$kandidat"; then PYTHON="$kandidat"; break; fi
done

if [ -z "$PYTHON" ]; then
  gefunden="keines"
  if command -v python3 >/dev/null 2>&1; then
    gefunden="$(python3 --version 2>&1) unter $(command -v python3)"
  fi
  if command -v brew >/dev/null 2>&1; then
    weg1="    brew install python@3.13 pango poppler"
    weg2="Danach diese Datei erneut starten."
  else
    weg1="    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    weg2="$(printf '%s\n' \
      "Das installiert Homebrew. Danach im selben Fenster:" \
      "    brew install python@3.13 pango poppler" \
      "Danach diese Datei erneut starten." \
      "" \
      "Ohne Homebrew geht es auch: https://www.python.org/downloads/macos/ --" \
      "dann fehlt aber noch pango, ohne das keine Rechnungs-PDF entsteht.")"
  fi
  fehler_fenster "$(printf '%s\n' \
    "Dieses Werkzeug braucht Python 3.12 oder neuer." \
    "Gefunden: $gefunden" \
    "Gesucht wurde auch in /opt/homebrew, /usr/local und /Library/Frameworks." \
    "" \
    "Auf dem Mac ist das mitgelieferte python3 die Version der" \
    "Entwicklerwerkzeuge und zu alt. Einmalig im Terminal:" \
    "" \
    "$weg1" \
    "" \
    "$weg2")"
fi

# Eine vorhandene Arbeitsumgebung bleibt auf dem Python stehen, das sie
# angelegt hat -- auch wenn danebenan laengst ein neueres steht. War das ein
# altes, scheitert die Installation mit "requires a different Python", und
# zwar jedes Mal wieder. Dasselbe nach einem "brew upgrade python": die
# Bibliothek, auf die die Umgebung verweist, ist weg, ihr python startet nur
# noch mit einem dyld-Fehler. Beides laesst sich nicht reparieren. Neu
# anlegen kostet eine Minute, denn im Ordner stehen nur Pakete, keine Daten.
if [ -d "$VENV" ] && ! taugt "$VENV/bin/python"; then
  gehabt="$("$VENV/bin/python" --version 2>/dev/null || echo "startet nicht")"
  echo "Die Arbeitsumgebung laeuft auf einem zu alten oder beschaedigten"
  echo "Python ($gehabt) und wird mit $("$PYTHON" --version 2>&1) neu angelegt."
  rm -rf "$VENV"
fi

# Wurde der Programmordner verschoben (etwa aus "Dokumente" heraus, damit der
# Hintergrunddienst ihn lesen darf), zeigen die Startzeilen der Werkzeuge in
# der Arbeitsumgebung (pip, uvicorn) noch auf den alten Ort -- "bad
# interpreter" bei jedem Start. Die Umgebung wird dann neu angelegt.
if [ -f "$VENV/bin/pip" ] && ! head -3 "$VENV/bin/pip" | grep -qF "$PWD/$VENV/"; then
  echo "Der Programmordner wurde verschoben; die Arbeitsumgebung wird neu angelegt."
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  echo "Erster Start: Arbeitsumgebung wird angelegt ($("$PYTHON" --version 2>&1))."
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
fi

# Nur installieren, wenn sich seit dem letzten Lauf etwas geaendert hat.
# shasum liegt auf macOS bei, sha1sum auf Linux -- das Skript soll sich auf
# beidem pruefen lassen.
if command -v shasum >/dev/null 2>&1; then PRUEFER="shasum"; else PRUEFER="sha1sum"; fi
STAND="$($PRUEFER < pyproject.toml | cut -d' ' -f1)-$EXTRAS-$("$PYTHON" --version 2>&1)"
if [ ! -f "$STEMPEL" ] || [ "$(cat "$STEMPEL")" != "$STAND" ]; then
  echo "Abhaengigkeiten werden eingerichtet ($EXTRAS)."
  if ! "$VENV/bin/pip" install --quiet -e ".[$EXTRAS]"; then
    fehler_fenster "$(printf '%s\n' \
      "Die Einrichtung ist fehlgeschlagen. Die Meldung steht oben." \
      "" \
      "Hilft fast immer: die Arbeitsumgebung wegwerfen und neu anlegen" \
      "lassen. Darin stehen nur Pakete, keine Daten. Im Terminal:" \
      "" \
      "    rm -rf \"$PWD/$VENV\"" \
      "" \
      "Danach diese Datei erneut starten.")"
  fi
  echo "$STAND" > "$STEMPEL"
fi

# WeasyPrint laedt native Bibliotheken (Pango, cairo, gdk-pixbuf), und die
# kommen nicht aus pip. Fehlen sie, startet die Anwendung trotzdem -- Zeiten,
# Kunden und Belege funktionieren --, aber das Festschreiben erzeugt kein PDF.
# Das soll JETZT dastehen und nicht bei der ersten Rechnung auffallen.
if ! "$VENV/bin/python" -c "import weasyprint" >/dev/null 2>&1; then
  echo
  echo "ACHTUNG: WeasyPrint laesst sich nicht laden -- es fehlen die"
  echo "Systembibliotheken (Pango und cairo), die nicht ueber pip kommen."
  echo "Die Anwendung startet, erzeugt aber KEINE Rechnungs-PDF."
  echo
  echo "Behebung auf dem Mac, einmalig im Terminal:"
  echo "    brew install pango"
  echo "Ohne Homebrew zuerst: https://brew.sh"
  echo "Danach diese Datei erneut starten."
  echo
  echo "Genaue Meldung:"
  "$VENV/bin/python" -c "import weasyprint" 2>&1 | tail -3 | sed 's/^/    /'
  echo
fi

# Browser erst oeffnen, wenn der Server wirklich antwortet.
(
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${PORT}/gesund" >/dev/null 2>&1; then
      command -v open >/dev/null 2>&1 && open "http://127.0.0.1:${PORT}/"
      break
    fi
    sleep 0.25
  done
) &

echo "Steuer-Tool laeuft auf http://127.0.0.1:${PORT}/  (Strg+C beendet es)"
exec "$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT"

#!/bin/bash
# Arma el .zip distribuible del CONECTOR (versión script, requiere Python 3) en la
# raíz del repo. Estructura del zip:
#   diagramind-connector/{*.py, requirements.txt, dashboard/, iniciar.command,
#                         iniciar.bat, LEEME.txt}
# El zip lo publica el workflow release-connector.yml como asset (no se commitea).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT"
STAGE="$(mktemp -d)/diagramind-connector"

mkdir -p "$STAGE" "$OUT"
# server.py es el entry point pero importa muchos módulos hermanos → copiamos TODOS
# los .py + requirements.txt + el dashboard estático que sirve el server.
cp "$HERE"/*.py "$STAGE/"
cp "$HERE/requirements.txt" "$STAGE/"
cp -R "$HERE/dashboard" "$STAGE/dashboard"
cp "$HERE/launchers/iniciar.command" "$STAGE/"
cp "$HERE/launchers/iniciar.bat" "$STAGE/"
cp "$HERE/launchers/LEEME.txt" "$STAGE/"
chmod +x "$STAGE/iniciar.command"

rm -f "$OUT/diagramind-connector.zip"
( cd "$(dirname "$STAGE")" && zip -r -q "$OUT/diagramind-connector.zip" "diagramind-connector" )
echo "Generado: $OUT/diagramind-connector.zip"

#!/bin/bash
# Arma el .zip distribuible (versión script, requiere Python) en la raíz del repo.
# Estructura del zip: diagramind-local/{server.py, iniciar.command, iniciar.bat, LEEME.txt}
# El zip lo publica el workflow de Release como asset (no se commitea).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT"
STAGE="$(mktemp -d)/diagramind-local"

mkdir -p "$STAGE" "$OUT"
cp "$HERE/server.py" "$STAGE/"
cp "$HERE/launchers/iniciar.command" "$STAGE/"
cp "$HERE/launchers/iniciar.bat" "$STAGE/"
cp "$HERE/launchers/LEEME.txt" "$STAGE/"
chmod +x "$STAGE/iniciar.command"

rm -f "$OUT/diagramind-local.zip"
( cd "$(dirname "$STAGE")" && zip -r -q "$OUT/diagramind-local.zip" "diagramind-local" )
echo "Generado: $OUT/diagramind-local.zip"

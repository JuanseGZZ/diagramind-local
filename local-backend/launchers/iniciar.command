#!/bin/bash
# DiagraMind backend local — lanzador para macOS / Linux.
# Doble clic (macOS) o ejecutar en terminal. Requiere Python 3.
cd "$(dirname "$0")"
echo "Iniciando DiagraMind local..."
if command -v python3 >/dev/null 2>&1; then
  python3 server.py
elif command -v python >/dev/null 2>&1; then
  python server.py
else
  echo "No se encontró Python 3. Instalalo desde https://www.python.org/downloads/"
  read -r -p "Enter para cerrar..." _
fi

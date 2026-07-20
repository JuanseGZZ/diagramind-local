#!/bin/bash
# DiagraMind — Conector externo — lanzador para macOS / Linux (versión script).
# Doble clic (macOS) o ejecutar en terminal. Requiere Python 3 (y git instalado
# para el versionado). Crea un entorno virtual e instala las dependencias la 1ra vez.
cd "$(dirname "$0")"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "No se encontró Python 3. Instalalo desde https://www.python.org/downloads/"
  read -r -p "Enter para cerrar..." _
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Preparando el entorno (solo la primera vez, puede tardar un minuto)..."
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip >/dev/null
  ./.venv/bin/python -m pip install -r requirements.txt
fi

echo "Iniciando DiagraMind Conector..."
exec ./.venv/bin/python server.py

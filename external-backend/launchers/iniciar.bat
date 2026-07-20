@echo off
REM DiagraMind - Conector externo - lanzador para Windows (version script).
REM Requiere Python 3 (y git instalado para el versionado). Crea un entorno
REM virtual e instala las dependencias la primera vez.
cd /d "%~dp0"

set PY=python
where python >nul 2>&1
if %errorlevel% neq 0 (
  set PY=py
  where py >nul 2>&1
  if %errorlevel% neq 0 (
    echo No se encontro Python 3. Instalalo desde https://www.python.org/downloads/
    echo (En el instalador, marca "Add Python to PATH".)
    pause
    exit /b 1
  )
)

if not exist ".venv" (
  echo Preparando el entorno (solo la primera vez, puede tardar un minuto)...
  %PY% -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r requirements.txt
)

echo Iniciando DiagraMind Conector...
.venv\Scripts\python server.py

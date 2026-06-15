@echo off
REM DiagraMind backend local - lanzador para Windows. Requiere Python 3.
cd /d "%~dp0"
echo Iniciando DiagraMind local...
where python >nul 2>&1
if %errorlevel%==0 (
  python server.py
) else (
  where py >nul 2>&1
  if %errorlevel%==0 (
    py server.py
  ) else (
    echo No se encontro Python 3. Instalalo desde https://www.python.org/downloads/
    pause
  )
)

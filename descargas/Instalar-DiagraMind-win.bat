@echo off
REM Instalador de DiagraMind Local para Windows.
REM Baja y ejecuta el script de instalacion (PowerShell).
echo == Instalando DiagraMind Local ==
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://github.com/JuanseGZZ/diagramind-local/releases/latest/download/instalar-win.ps1' | iex"
echo.
pause

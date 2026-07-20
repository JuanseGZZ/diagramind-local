@echo off
REM Instalador del Conector externo DiagraMind para Windows.
REM Los releases del conector son "prerelease" (para no pisar el 'latest' del backend
REM local, que comparte repo), asi que resolvemos el asset del script por la API de
REM GitHub (el release mas nuevo con tag connector-v*) y lo ejecutamos.
echo == Instalando DiagraMind Conector ==
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $api='https://api.github.com/repos/JuanseGZZ/diagramind-local/releases'; $rel=Invoke-RestMethod -Uri $api -Headers @{'User-Agent'='DiagraMind'}; $r=$rel | Where-Object { $_.tag_name -like 'connector-v*' } | Select-Object -First 1; if (-not $r) { Write-Host 'No se encontro ningun release del conector.'; exit 1 }; $u=($r.assets | Where-Object { $_.name -eq 'instalar-connector-win.ps1' } | Select-Object -First 1).browser_download_url; irm $u | iex"
echo.
pause

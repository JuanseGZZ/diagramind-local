# Lógica de instalación del Conector externo DiagraMind (Windows).
# La invoca Instalar-DiagraMind-Connector-win.bat. Resuelve el .exe del release más
# nuevo del conector (tag connector-v*, marcado prerelease → no se puede usar
# releases/latest), lo instala y crea un acceso en la carpeta de Inicio para que
# arranque solo al iniciar sesión.
$ErrorActionPreference = "Stop"

$api = "https://api.github.com/repos/JuanseGZZ/diagramind-local/releases"
$rel = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "DiagraMind" }
$r = $rel | Where-Object { $_.tag_name -like "connector-v*" } | Select-Object -First 1
if (-not $r) { Write-Host "No se encontro ningun release del conector."; exit 1 }
$url = ($r.assets | Where-Object { $_.name -eq "DiagraMind-Connector-win.exe" } | Select-Object -First 1).browser_download_url
if (-not $url) { Write-Host "El release $($r.tag_name) no tiene el binario de Windows."; exit 1 }

$dir = Join-Path $env:LOCALAPPDATA "DiagraMind-Connector"
$bin = Join-Path $dir "DiagraMind-Connector.exe"

New-Item -ItemType Directory -Force -Path $dir | Out-Null

Write-Host "Descargando el conector ($($r.tag_name))..."
Invoke-WebRequest -Uri $url -OutFile $bin

Write-Host "Configurando auto-inicio..."
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup "DiagraMind Connector.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $bin
$sc.WindowStyle = 7   # minimizado
$sc.Save()

# arrancarlo ahora
Start-Process -FilePath $bin

Write-Host ""
Write-Host "Listo. El conector corre en http://127.0.0.1:8770/dashboard/ y arranca solo al iniciar Windows."
Write-Host ""
Write-Host "IMPORTANTE (primer arranque): se creo el usuario 'admin' con una password"
Write-Host "aleatoria. Como el conector arranca en segundo plano, la password quedo en:"
Write-Host "  $dir\admin_password.txt"
Write-Host "Usala para entrar al dashboard; te va a pedir cambiarla."
Write-Host ""
Write-Host "Nota: el conector usa 'git' para versionar los proyectos. Si no lo tenes,"
Write-Host "instalalo desde https://git-scm.com/download/win"

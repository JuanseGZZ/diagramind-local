# Lógica de instalación de DiagraMind Local (Windows).
# La invoca Instalar-DiagraMind-win.bat. Descarga el .exe, lo instala y
# crea un acceso en la carpeta de Inicio para que arranque solo al iniciar sesión.
$ErrorActionPreference = "Stop"

$url = "https://github.com/JuanseGZZ/diagramind-local/releases/latest/download/DiagraMind-Local-win.exe"
$dir = Join-Path $env:LOCALAPPDATA "DiagraMind"
$bin = Join-Path $dir "DiagraMind-Local.exe"

New-Item -ItemType Directory -Force -Path $dir | Out-Null

Write-Host "Descargando el programa..."
Invoke-WebRequest -Uri $url -OutFile $bin

Write-Host "Configurando auto-inicio..."
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup "DiagraMind Local.lnk"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $bin
$sc.WindowStyle = 7   # minimizado
$sc.Save()

# arrancarlo ahora
Start-Process -FilePath $bin

Write-Host ""
Write-Host "Listo. DiagraMind Local corre en http://127.0.0.1:8765 y arranca solo al iniciar Windows."
Write-Host "Volve a la web y toca 'Conectar local'."

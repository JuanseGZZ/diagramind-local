#!/bin/bash
# Instalador del Conector externo DiagraMind para macOS.
# Resuelve el binario del release más nuevo del conector (tag connector-v*, marcado
# prerelease → no se puede usar releases/latest) vía la API de GitHub, lo instala y
# lo hace arrancar solo al encender la Mac.
set -e

API="https://api.github.com/repos/JuanseGZZ/diagramind-local/releases"
echo "== Instalando DiagraMind Conector =="
echo "Buscando la última versión..."
BIN_URL="$(curl -fsSL "$API" | grep -oE '"browser_download_url": *"[^"]*DiagraMind-Connector-mac"' | head -1 | grep -oE 'https[^"]*')"
if [ -z "$BIN_URL" ]; then
  echo "No se encontró el binario de macOS en los releases del conector."
  read -r -p "Enter para cerrar..." _
  exit 1
fi

DIR="$HOME/Library/Application Support/DiagraMind-Connector"
BIN="$DIR/DiagraMind-Connector"
PLIST="$HOME/Library/LaunchAgents/com.diagramind.connector.plist"

mkdir -p "$DIR" "$HOME/Library/LaunchAgents"

echo "Descargando el conector..."
curl -fsSL "$BIN_URL" -o "$BIN"
chmod +x "$BIN"
# binario bajado por curl no lleva quarantine; por las dudas lo limpiamos
xattr -dr com.apple.quarantine "$BIN" 2>/dev/null || true

echo "Configurando auto-inicio..."
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.diagramind.connector</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo ""
echo "Listo. El conector quedó corriendo en http://127.0.0.1:8770/dashboard/"
echo "Se va a iniciar solo cada vez que prendas la Mac."
echo ""
echo "IMPORTANTE (primer arranque): se creó el usuario 'admin' con una password"
echo "aleatoria. Como el conector arranca en segundo plano, quedó guardada en:"
echo "  $DIR/admin_password.txt"
echo "Usala para entrar al dashboard; te va a pedir cambiarla."
echo ""
echo "Nota: el conector usa 'git' para versionar los proyectos (macOS suele traerlo)."
read -r -p "Enter para cerrar esta ventana..." _

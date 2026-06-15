#!/bin/bash
# Instalador de DiagraMind Local para macOS.
# Descarga el binario, lo instala y lo hace arrancar solo al encender la Mac.
set -e

REL="https://github.com/JuanseGZZ/diagramind-local/releases/latest/download"
BIN_URL="$REL/DiagraMind-Local-mac"

DIR="$HOME/Library/Application Support/DiagraMind"
BIN="$DIR/DiagraMind-Local"
PLIST="$HOME/Library/LaunchAgents/com.diagramind.local.plist"

echo "== Instalando DiagraMind Local =="
mkdir -p "$DIR" "$HOME/Library/LaunchAgents"

echo "Descargando el programa..."
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
  <key>Label</key><string>com.diagramind.local</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo ""
echo "Listo. DiagraMind Local quedó instalado y corriendo en http://127.0.0.1:8765"
echo "Se va a iniciar solo cada vez que prendas la Mac."
echo "Ahora podés volver a la web y tocar 'Conectar local'."
read -r -p "Enter para cerrar esta ventana..." _

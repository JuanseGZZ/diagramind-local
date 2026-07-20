#!/bin/bash
# Instalador del Conector externo DiagraMind para Linux.
# Resuelve el binario del release más nuevo del conector (tag connector-v*, marcado
# prerelease → no se puede usar releases/latest) vía la API de GitHub, lo instala y
# lo hace arrancar solo al iniciar sesión.
set -e

API="https://api.github.com/repos/JuanseGZZ/diagramind-local/releases"
echo "== Instalando DiagraMind Conector =="
echo "Buscando la última versión..."
BIN_URL="$(curl -fsSL "$API" | grep -oE '"browser_download_url": *"[^"]*DiagraMind-Connector-linux"' | head -1 | grep -oE 'https[^"]*')"
if [ -z "$BIN_URL" ]; then
  echo "No se encontró el binario de Linux en los releases del conector."
  exit 1
fi

BIN="$HOME/.local/bin/DiagraMind-Connector"
mkdir -p "$HOME/.local/bin"

echo "Descargando el conector..."
curl -fsSL "$BIN_URL" -o "$BIN"
chmod +x "$BIN"

echo "Configurando auto-inicio..."
if command -v systemctl >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/diagramind-connector.service" <<EOF
[Unit]
Description=DiagraMind Connector (external backend)

[Service]
ExecStart=$BIN
Restart=on-failure

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now diagramind-connector.service
  echo "Auto-inicio configurado (systemd user)."
else
  # Fallback: entrada de autostart del escritorio
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/diagramind-connector.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DiagraMind Connector
Exec=$BIN
X-GNOME-Autostart-enabled=true
EOF
  "$BIN" >/dev/null 2>&1 &
  echo "Auto-inicio configurado (autostart del escritorio)."
fi

echo ""
echo "Listo. El conector corre en http://127.0.0.1:8770/dashboard/ y arranca solo al iniciar sesión."
echo ""
echo "IMPORTANTE (primer arranque): se creó el usuario 'admin' con una password"
echo "aleatoria, guardada en:"
echo "  \$XDG_DATA_HOME/DiagraMind-Connector/admin_password.txt (default ~/.local/share/…)"
echo "Usala para entrar al dashboard; te va a pedir cambiarla."
echo ""
echo "Nota: el conector usa 'git' para versionar los proyectos. Si no lo tenés,"
echo "instalalo con el gestor de paquetes de tu distro (ej: sudo apt install git)."

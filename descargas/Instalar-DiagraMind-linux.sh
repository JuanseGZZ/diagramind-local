#!/bin/bash
# Instalador de DiagraMind Local para Linux.
# Descarga el binario, lo instala y lo hace arrancar solo al iniciar sesión.
set -e

REL="https://github.com/JuanseGZZ/diagramind-local/releases/latest/download"
BIN_URL="$REL/DiagraMind-Local-linux"

BIN="$HOME/.local/bin/DiagraMind-Local"

echo "== Instalando DiagraMind Local =="
mkdir -p "$HOME/.local/bin"

echo "Descargando el programa..."
curl -fsSL "$BIN_URL" -o "$BIN"
chmod +x "$BIN"

echo "Configurando auto-inicio..."
if command -v systemctl >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/diagramind-local.service" <<EOF
[Unit]
Description=DiagraMind Local backend

[Service]
ExecStart=$BIN
Restart=on-failure

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now diagramind-local.service
  echo "Auto-inicio configurado (systemd user)."
else
  # Fallback: entrada de autostart del escritorio
  mkdir -p "$HOME/.config/autostart"
  cat > "$HOME/.config/autostart/diagramind-local.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DiagraMind Local
Exec=$BIN
X-GNOME-Autostart-enabled=true
EOF
  "$BIN" >/dev/null 2>&1 &
  echo "Auto-inicio configurado (autostart del escritorio)."
fi

echo ""
echo "Listo. DiagraMind Local corre en http://127.0.0.1:8765 y arranca solo al iniciar sesión."
echo "Volvé a la web y tocá 'Conectar local'."

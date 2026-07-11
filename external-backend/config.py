"""Configuración y rutas del conector externo.

Todo el estado del conector vive bajo un **HOME** (dir base), configurable por env
`DMC_HOME`. Dentro:

    <HOME>/connector.db        ← sqlite (users, acl, refresh, ws_tickets)
    <HOME>/secret.key          ← secreto de firma JWT (se genera al azar la 1ra vez)
    <HOME>/admin_password.txt  ← password del admin bootstrap (se escribe al crearlo)
    <HOME>/repo/               ← ROOT del conector = repo git (working tree = copia viva)

El default del HOME es el **appdir del SO** (como el backend local), NUNCA junto al
script: si el estado vive dentro del workspace, cualquier dev-server con live-reload
(VSCode Live Server, etc.) detecta cada escritura de la sqlite y recarga la página en
loop (el bug del "F5 infinito" al conectar). Ver [[25 - Conector Externo v2]] §0/§7.

El ROOT del repo se puede mover con env `DMC_ROOT` (default `<HOME>/repo`).
"""

import os
import secrets
import shutil
import sys
from pathlib import Path

VERSION = "0.3.0"   # IA Orchestrator server-side (doc 28, fase 5)
NAME = "DiagraMind Connector"


def _default_home() -> Path:
    """Appdir del SO para el conector (fuera de cualquier workspace)."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "DiagraMind-Connector"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home)) / "DiagraMind-Connector"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "DiagraMind-Connector"


# ---- rutas base ----
HOME = Path(os.environ.get("DMC_HOME", _default_home())).resolve()

# Migración: si existe el viejo `.dmc` junto al script (default anterior) y el HOME
# nuevo no, se muda entero (conserva admin, secreto y repo). Solo sin DMC_HOME env.
_LEGACY = Path(__file__).resolve().parent / ".dmc"
if "DMC_HOME" not in os.environ and _LEGACY.is_dir() and not HOME.exists():
    HOME.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(_LEGACY), str(HOME))
    print(f"[connector] estado migrado: {_LEGACY} → {HOME}", flush=True)
DB_PATH = HOME / "connector.db"
SECRET_PATH = HOME / "secret.key"
ADMIN_PW_PATH = HOME / "admin_password.txt"
REPO_ROOT = Path(os.environ.get("DMC_ROOT", HOME / "repo")).resolve()

# ---- red ----
HOST = os.environ.get("DMC_HOST", "127.0.0.1")
PORT = int(os.environ.get("DMC_PORT", "8770"))

# ---- tiempos (segundos) ----
ACCESS_TTL = 15 * 60            # access JWT corto
REFRESH_TTL = 30 * 24 * 60 * 60  # refresh largo (con rotación)
WS_TICKET_TTL = 30             # ticket de WS: muy corto, un solo uso

JWT_ALG = "HS256"


def ensure_home() -> None:
    """Crea el HOME si no existe (idempotente)."""
    HOME.mkdir(parents=True, exist_ok=True)


def get_secret() -> str:
    """Secreto de firma JWT: lo lee de disco o lo genera y persiste la primera vez."""
    ensure_home()
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    SECRET_PATH.write_text(secret, encoding="utf-8")
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass
    return secret

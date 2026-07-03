"""Configuración y rutas del conector externo.

Todo el estado del conector vive bajo un **HOME** (dir base), configurable por env
`DMC_HOME` (default `./.dmc` junto al script). Dentro:

    <HOME>/connector.db        ← sqlite (users, acl, refresh, ws_tickets)
    <HOME>/secret.key          ← secreto de firma JWT (se genera al azar la 1ra vez)
    <HOME>/admin_password.txt  ← password del admin bootstrap (se escribe al crearlo)
    <HOME>/repo/               ← ROOT del conector = repo git (working tree = copia viva)

El ROOT del repo se puede mover con env `DMC_ROOT` (default `<HOME>/repo`).
Ver [[25 - Conector Externo v2]] §0/§7.
"""

import os
import secrets
from pathlib import Path

VERSION = "0.1.0"
NAME = "DiagraMind Connector"

# ---- rutas base ----
HOME = Path(os.environ.get("DMC_HOME", Path(__file__).resolve().parent / ".dmc")).resolve()
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

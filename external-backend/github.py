"""Conexión de la cuenta GitHub del root + push (ver [[25 - Conector Externo v2]] §7/§C).

Se conecta **solo desde el dashboard** (admin). El remoto + token se guardan en
`<HOME>/github.json` (fuera del repo git, con permisos 600). El push usa el token en
la URL (`https://<token>@github.com/...`). Sistema de snapshots propio: a futuro.
"""

import json
import os
import subprocess
from urllib.parse import urlparse, urlunparse

from config import HOME, REPO_ROOT, ensure_home

GITHUB_PATH = HOME / "github.json"


def _read() -> dict:
    try:
        return json.loads(GITHUB_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def is_connected() -> bool:
    d = _read()
    return bool(d.get("remoteUrl") and d.get("token"))


def status() -> dict:
    """Estado público (NO devuelve el token)."""
    d = _read()
    return {"connected": is_connected(), "remoteUrl": d.get("remoteUrl") or None,
            "branch": d.get("branch") or "main"}


def connect(remote_url: str, token: str, branch: str = "main") -> None:
    ensure_home()
    GITHUB_PATH.write_text(json.dumps({"remoteUrl": remote_url, "token": token, "branch": branch}),
                           encoding="utf-8")
    try:
        os.chmod(GITHUB_PATH, 0o600)
    except OSError:
        pass


def disconnect() -> None:
    try:
        GITHUB_PATH.unlink()
    except OSError:
        pass


def _auth_url(remote_url: str, token: str) -> str:
    """Inserta el token en la URL https para autenticar el push."""
    u = urlparse(remote_url)
    netloc = f"{token}@{u.hostname}" + (f":{u.port}" if u.port else "")
    return urlunparse((u.scheme, netloc, u.path, "", "", ""))


def push() -> dict:
    """Pushea HEAD a la rama configurada del remoto. {ok, detail}."""
    d = _read()
    if not (d.get("remoteUrl") and d.get("token")):
        return {"ok": False, "detail": "github not connected"}
    branch = d.get("branch") or "main"
    url = _auth_url(d["remoteUrl"], d["token"])
    r = subprocess.run(["git", "push", url, f"HEAD:{branch}"], cwd=str(REPO_ROOT),
                       capture_output=True, text=True, check=False)
    ok = r.returncode == 0
    # no filtrar el token si aparece en el mensaje de error
    detail = (r.stderr or r.stdout).replace(d["token"], "***").strip()
    return {"ok": ok, "detail": detail}

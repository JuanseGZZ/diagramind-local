"""Conector externo — server base (FastAPI).

Paso 1 del rebuild (ver [[25 - Conector Externo v2]] §13): auth (bcrypt/JWT/refresh)
+ sqlite (users/acl/refresh/ws_tickets) + bootstrap admin + root = repo git + /health.
El WebSocket realtime, el CRUD de folders/projects y el versionado llegan después.

Correr:  .venv/bin/python server.py       (o: uvicorn server:app)
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import config
from auth import router as auth_router
from content import router as content_router
from fs import router as fs_router
from sv import router as sv_router
from db import connect, init_db
from gitrepo import init_repo
from mcp import router as mcp_router
import orch
from orch import router as orch_router
from realtime import router as realtime_router
from security import hash_password, random_password
from users import router as users_router
from versions import router as versions_router


def bootstrap_admin() -> None:
    """Primer arranque: si no hay usuarios, crea el admin.

    Interactivo (default): password aleatoria → `<HOME>/admin_password.txt` + consola,
    `must_change_pw=1` (obliga a cambiarla al entrar).

    NO interactivo (SaaS/docker, doc 26 §4): la password inicial viene por env
    `DMC_ADMIN_PASSWORD` (o `DMC_ADMIN_PASSWORD_FILE`, p.ej. un Secret montado) y NO
    se escribe a disco ni se imprime. `DMC_ADMIN_MUST_CHANGE=0` desactiva el cambio
    obligatorio (instancias free: el admin ES el back central); para las pagas se
    deja el default 1 y el dueño la cambia en su primer ingreso.
    """
    with connect() as c:
        has_user = c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if has_user:
            return
        pw = os.environ.get("DMC_ADMIN_PASSWORD") or ""
        pw_file = os.environ.get("DMC_ADMIN_PASSWORD_FILE")
        if not pw and pw_file and Path(pw_file).is_file():
            pw = Path(pw_file).read_text(encoding="utf-8").strip()
        provided = bool(pw)
        if not provided:
            pw = random_password()
        must_change = 0 if (provided and os.environ.get("DMC_ADMIN_MUST_CHANGE", "1") == "0") else 1
        c.execute(
            "INSERT INTO users (username, password_hash, role, must_change_pw) VALUES (?,?,?,?)",
            ("admin", hash_password(pw), "admin", must_change),
        )
    if provided:
        print(f"[connector] admin creado con la password provista por env "
              f"(must_change_pw={must_change})", flush=True)
        return
    config.ADMIN_PW_PATH.write_text(pw, encoding="utf-8")
    print("=" * 60)
    print("  ADMIN creado (primer arranque)")
    print(f"    usuario:  admin")
    print(f"    password: {pw}")
    print(f"    (también en {config.ADMIN_PW_PATH})")
    print("    Cambiala en el primer ingreso.")
    print("=" * 60, flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_home()
    init_db()
    bootstrap_admin()
    root = init_repo()
    orch.set_loop(asyncio.get_running_loop())   # broadcasts WS desde los threads del motor
    print(f"[connector] {config.NAME} v{config.VERSION}", flush=True)
    print(f"  home:      {config.HOME}", flush=True)
    print(f"             (en Mac, ~/Library está oculta en Finder: Cmd+Shift+G para ir)", flush=True)
    print(f"  root git:  {root}", flush=True)
    print(f"  dashboard: http://{config.HOST}:{config.PORT}/dashboard/", flush=True)
    yield


app = FastAPI(title=config.NAME, version=config.VERSION, lifespan=lifespan)

# CORS abierto (dev): la web-hub le pega desde su origen. En prod, HTTPS/WSS + origen
# acotado. Usamos bearer tokens (no cookies) → no hace falta allow_credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Público: detección del conector + esquema de auth (+ flags SaaS, doc 26 §3)."""
    return {"status": "ok", "name": config.NAME, "version": config.VERSION, "auth": "jwt",
            "shared": config.SHARED, "folderQuotaMb": config.FOLDER_QUOTA_MB}


app.include_router(auth_router)
app.include_router(users_router)
app.include_router(content_router)    # REST folders/projects (namespace + permisos)
app.include_router(versions_router)   # versionado git + GitHub
app.include_router(fs_router)         # modo editor: /editor/target + /fs/* (doc 27)
app.include_router(sv_router)         # modo editor: source versions /sv/* (doc 27, fase 4)
app.include_router(orch_router)       # IA Orchestrator server-side /orch/* (doc 28, fase 5 — solo admin)
app.include_router(mcp_router)        # MCP por carpeta: /mcp/tokens (sesión) + /mcp/<token> (doc 26 §6)
app.include_router(realtime_router)   # WebSocket /ws (realtime mirror)


@app.get("/")
def root():
    return RedirectResponse(url="/dashboard/")


# Dashboard estático (admin): usuarios/ACL + repo + versiones + GitHub.
# El JS llama a los mismos endpoints REST (mismo origen). Ver dashboard/index.html.
_DASH = Path(__file__).resolve().parent / "dashboard"
app.mount("/dashboard", StaticFiles(directory=str(_DASH), html=True), name="dashboard")


def reset_password_cli(username: str) -> None:
    """Recuperación LOCAL de contraseña (p.ej. admin que se la olvidó): genera una
    nueva aleatoria, invalida todas las sesiones y re-habilita al usuario. Requiere
    shell en la máquina del server — ese es el modelo de seguridad (quien tiene
    acceso al disco ya es dueño del conector)."""
    config.ensure_home()
    init_db()
    with connect() as c:
        row = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            print(f"[connector] el usuario {username!r} no existe")
            return
        pw = random_password()
        c.execute(
            "UPDATE users SET password_hash=?, must_change_pw=1, disabled=0, "
            "token_version=token_version+1 WHERE id=?",
            (hash_password(pw), row["id"]),
        )
        c.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (row["id"],))
    if username == "admin":
        config.ADMIN_PW_PATH.write_text(pw, encoding="utf-8")
    print(f"[connector] contraseña reseteada para {username!r}: {pw}")
    print("  Al entrar va a pedir cambiarla. Todas las sesiones viejas quedaron invalidadas.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=f"{config.NAME} v{config.VERSION}")
    ap.add_argument(
        "--reset-password", metavar="USER", nargs="?", const="admin", default=None,
        help="resetea la contraseña de un usuario (sin valor: admin) y sale",
    )
    args = ap.parse_args()
    if args.reset_password:
        reset_password_cli(args.reset_password)
    else:
        uvicorn.run(app, host=config.HOST, port=config.PORT)

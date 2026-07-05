"""Conector externo — server base (FastAPI).

Paso 1 del rebuild (ver [[25 - Conector Externo v2]] §13): auth (bcrypt/JWT/refresh)
+ sqlite (users/acl/refresh/ws_tickets) + bootstrap admin + root = repo git + /health.
El WebSocket realtime, el CRUD de folders/projects y el versionado llegan después.

Correr:  .venv/bin/python server.py       (o: uvicorn server:app)
"""

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
from db import connect, init_db
from gitrepo import init_repo
from realtime import router as realtime_router
from security import hash_password, random_password
from users import router as users_router
from versions import router as versions_router


def bootstrap_admin() -> None:
    """Primer arranque: si no hay usuarios, crea un admin con password aleatoria.

    La password se escribe en `<HOME>/admin_password.txt` y se imprime en consola.
    `must_change_pw=1` → al entrar la primera vez, obliga a cambiarla.
    """
    with connect() as c:
        has_user = c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if has_user:
            return
        pw = random_password()
        c.execute(
            "INSERT INTO users (username, password_hash, role, must_change_pw) VALUES (?,?,?,1)",
            ("admin", hash_password(pw), "admin"),
        )
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
    """Público: detección del conector + esquema de auth."""
    return {"status": "ok", "name": config.NAME, "version": config.VERSION, "auth": "jwt"}


app.include_router(auth_router)
app.include_router(users_router)
app.include_router(content_router)    # REST folders/projects (namespace + permisos)
app.include_router(versions_router)   # versionado git + GitHub
app.include_router(fs_router)         # modo editor: /editor/target + /fs/* (doc 27)
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

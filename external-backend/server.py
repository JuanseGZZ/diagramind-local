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
    print(f"[connector] {config.NAME} v{config.VERSION} — root git: {root}", flush=True)
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
app.include_router(realtime_router)   # WebSocket /ws (realtime mirror)


@app.get("/")
def root():
    return RedirectResponse(url="/dashboard/")


# Dashboard estático (admin): usuarios/ACL + repo + versiones + GitHub.
# El JS llama a los mismos endpoints REST (mismo origen). Ver dashboard/index.html.
_DASH = Path(__file__).resolve().parent / "dashboard"
app.mount("/dashboard", StaticFiles(directory=str(_DASH), html=True), name="dashboard")


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)

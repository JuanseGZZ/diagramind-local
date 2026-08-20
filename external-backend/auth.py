"""Autenticación: router /auth + dependencias de sesión/rol.

Flujo (ver [[25 - Conector Externo v2]] §3):
- login → access (corto) + refresh (largo, jti persistido) + mustChangePassword.
- refresh → **rota**: revoca el jti viejo y emite par nuevo (valida token_version).
- change-password → set hash + **bump token_version** (invalida todo) + emite par nuevo.
- ws-ticket → ticket corto de un solo uso para abrir el WebSocket.
"""

import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from db import connect
from models import ChangePasswordBody, LoginBody, RefreshBody
from security import (
    decode_token,
    hash_password,
    make_access_token,
    make_refresh_token,
    new_ws_ticket,
    verify_password,
)
import config
from config import WS_TICKET_TTL

router = APIRouter(prefix="/auth", tags=["auth"])


def _now() -> int:
    return int(time.time())


# ---------------- helpers de DB ----------------

def get_user_by_id(uid: int) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None


def get_user_by_name(username: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None


def purge_refresh_tokens(conn, user_id=None) -> int:
    """Barre los refresh que ya no sirven: EXPIRADOS y revocados hace rato.

    Antes no se borraba ninguno —ni al rotar ni al salir— y la tabla solo crecía (§43).
    Las revocadas se conservan `REVOKED_KEEP` para poder detectar el reuso de un token
    robado; después no aportan nada.
    """
    now = int(time.time())
    sql = ("DELETE FROM refresh_tokens WHERE (CAST(expires_at AS INTEGER) < ? "
           "OR (revoked=1 AND CAST(strftime('%s', created_at) AS INTEGER) < ?))")
    args = [now, now - config.REVOKED_KEEP]
    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(user_id)
    return conn.execute(sql, args).rowcount


def _issue_pair(user: dict) -> dict:
    """Emite access + refresh y persiste el jti del refresh."""
    access = make_access_token(user["id"], user["role"], user["token_version"])
    refresh, jti, exp = make_refresh_token(user["id"], user["token_version"])
    with connect() as c:
        purge_refresh_tokens(c, user["id"])     # cada sesión nueva limpia lo suyo
        c.execute(
            "INSERT INTO refresh_tokens (jti, user_id, token_version, expires_at) VALUES (?,?,?,?)",
            (jti, user["id"], user["token_version"], str(exp)),
        )
    return {"access": access, "refresh": refresh}


# ---------------- dependencias ----------------

def current_user(authorization: str | None = Header(default=None)) -> dict:
    """Valida el access token (Authorization: Bearer <token>) y devuelve el usuario.

    Rechaza si: no hay token / firma o exp inválida / no es de tipo access / el
    token_version no coincide (password cambiada o logout-all) / usuario deshabilitado.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="invalid access token")
    user = get_user_by_id(int(payload["sub"]))
    if not user or user["disabled"]:
        raise HTTPException(status_code=401, detail="user unavailable")
    if payload.get("tv") != user["token_version"]:
        raise HTTPException(status_code=401, detail="token revoked")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


# ---------------- endpoints ----------------

@router.post("/login")
def login(body: LoginBody):
    user = get_user_by_name(body.username)
    if not user or user["disabled"] or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="bad credentials")
    pair = _issue_pair(user)
    return {**pair, "role": user["role"], "mustChangePassword": bool(user["must_change_pw"])}


@router.post("/refresh")
def refresh(body: RefreshBody):
    payload = decode_token(body.refresh)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="invalid refresh token")
    user = get_user_by_id(int(payload["sub"]))
    if not user or user["disabled"] or payload.get("tv") != user["token_version"]:
        raise HTTPException(status_code=401, detail="refresh revoked")
    jti = payload.get("jti")
    with connect() as c:
        row = c.execute("SELECT * FROM refresh_tokens WHERE jti=?", (jti,)).fetchone()
        if not row or row["revoked"] or int(row["expires_at"]) < _now():
            raise HTTPException(status_code=401, detail="refresh not active")
        # rotación: revoca el viejo antes de emitir el nuevo
        # BORRAR, no marcar: una sesión activa = UNA fila (§43).
        c.execute("DELETE FROM refresh_tokens WHERE jti=?", (jti,))
    pair = _issue_pair(user)
    return {**pair, "role": user["role"], "mustChangePassword": bool(user["must_change_pw"])}


@router.post("/change-password")
def change_password(body: ChangePasswordBody, user: dict = Depends(current_user)):
    # si NO está obligado a cambiar, exige el password actual y lo verifica
    if not user["must_change_pw"]:
        if not body.current or not verify_password(body.current, user["password_hash"]):
            raise HTTPException(status_code=403, detail="current password wrong")
    new_hash = hash_password(body.newPassword)
    with connect() as c:
        # bump token_version = invalida TODOS los tokens (access + refresh) existentes
        c.execute(
            "UPDATE users SET password_hash=?, must_change_pw=0, token_version=token_version+1 WHERE id=?",
            (new_hash, user["id"]),
        )
        c.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (user["id"],))
    fresh = get_user_by_id(user["id"])
    pair = _issue_pair(fresh)            # devuelve un par nuevo para no desloguear
    return {**pair, "role": fresh["role"], "mustChangePassword": False}


class LogoutBody(BaseModel):
    refresh: str | None = None
    all: bool = False


@router.post("/logout")
def logout(body: LogoutBody, authorization: str | None = Header(default=None)):
    """Cierra sesión de verdad: borra el refresh en el server (§43).

    Idempotente y sin fallar: si el token ya expiró igual queremos limpiar y contestar
    OK — cerrar sesión no puede depender de que la sesión siga viva.
    """
    borrados = 0
    uid = None
    payload = decode_token(body.refresh) if body.refresh else None
    if payload and payload.get("type") == "refresh":
        uid = int(payload["sub"])
        with connect() as c:
            borrados = c.execute("DELETE FROM refresh_tokens WHERE jti=?",
                                 (payload.get("jti"),)).rowcount
    if body.all and authorization and authorization.lower().startswith("bearer "):
        p2 = decode_token(authorization.split(" ", 1)[1].strip())
        if p2 and p2.get("type") == "access":
            uid = int(p2["sub"])
            with connect() as c:
                borrados += c.execute("DELETE FROM refresh_tokens WHERE user_id=?",
                                      (uid,)).rowcount
                c.execute("UPDATE users SET token_version = token_version + 1 WHERE id=?", (uid,))
    if uid is not None:
        with connect() as c:
            purge_refresh_tokens(c, uid)
    return {"ok": True, "closed": borrados}


@router.post("/ws-ticket")
def ws_ticket(user: dict = Depends(current_user)):
    ticket = new_ws_ticket()
    exp = _now() + WS_TICKET_TTL
    with connect() as c:
        c.execute(
            "INSERT INTO ws_tickets (ticket, user_id, expires_at) VALUES (?,?,?)",
            (ticket, user["id"], str(exp)),
        )
    return {"ticket": ticket, "ttl": WS_TICKET_TTL}


def consume_ws_ticket(ticket: str | None) -> dict | None:
    """Canjea el ticket del WS (un solo uso): valida no-usado/no-vencido, lo marca
    usado y devuelve el usuario. None si es inválido. El WS no lleva headers, por eso
    el ticket viaja por query (ver §3/§10)."""
    if not ticket:
        return None
    with connect() as c:
        row = c.execute("SELECT * FROM ws_tickets WHERE ticket=?", (ticket,)).fetchone()
        if not row or row["used"] or int(row["expires_at"]) < _now():
            return None
        c.execute("UPDATE ws_tickets SET used=1 WHERE ticket=?", (ticket,))
        uid = row["user_id"]
    user = get_user_by_id(uid)
    if not user or user["disabled"]:
        return None
    return user

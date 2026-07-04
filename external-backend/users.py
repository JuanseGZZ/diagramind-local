"""Gestión de usuarios (solo admin): alta, roles, ACL por carpeta, habilitar/borrar.

Ver [[25 - Conector Externo v2]] §3. La ACL por carpeta manda sobre el rol; el
default (sin fila) = `none` (ni ve la carpeta). El enforcement por carpeta real
llega con folders/projects (paso 4); acá queda la administración de las filas.
"""

from fastapi import APIRouter, Depends, HTTPException

from auth import require_admin
from db import connect
from models import CreateUserBody, SetAclBody, SetRoleBody
from security import hash_password, random_password
from store import get_folder

router = APIRouter(prefix="/users", tags=["users"])

ROLES = {"admin", "editor", "viewer"}
PERMS = {"none", "read", "write"}


def _user_public(row) -> dict:
    return {
        "id": row["id"], "username": row["username"], "role": row["role"],
        "mustChangePassword": bool(row["must_change_pw"]), "disabled": bool(row["disabled"]),
        "createdAt": row["created_at"],
    }


@router.get("")
def list_users(_: dict = Depends(require_admin)):
    with connect() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return {"users": [_user_public(r) for r in rows]}


@router.post("")
def create_user(body: CreateUserBody, _: dict = Depends(require_admin)):
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail="bad role")
    temp = random_password()
    with connect() as c:
        exists = c.execute("SELECT 1 FROM users WHERE username=?", (body.username,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="username taken")
        cur = c.execute(
            "INSERT INTO users (username, password_hash, role, must_change_pw) VALUES (?,?,?,1)",
            (body.username, hash_password(temp), body.role),
        )
        uid = cur.lastrowid
    # la password temporal se devuelve UNA vez (el admin se la pasa al usuario)
    return {"id": uid, "username": body.username, "role": body.role, "tempPassword": temp}


@router.post("/{uid}/reset-password")
def reset_password(uid: int, _: dict = Depends(require_admin)):
    """Resetea la contraseña de un usuario: nueva temporal (se devuelve UNA vez),
    obliga a cambiarla al entrar e invalida todas sus sesiones."""
    temp = random_password()
    with connect() as c:
        r = c.execute(
            "UPDATE users SET password_hash=?, must_change_pw=1, token_version=token_version+1 WHERE id=?",
            (hash_password(temp), uid),
        )
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="user not found")
        c.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (uid,))
    return {"tempPassword": temp}


@router.post("/{uid}/role")
def set_role(uid: int, body: SetRoleBody, _: dict = Depends(require_admin)):
    if body.role not in ROLES:
        raise HTTPException(status_code=400, detail="bad role")
    with connect() as c:
        r = c.execute("UPDATE users SET role=? WHERE id=?", (body.role, uid))
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@router.post("/{uid}/disabled")
def set_disabled(uid: int, disabled: bool = True, _: dict = Depends(require_admin)):
    with connect() as c:
        # deshabilitar también bumpea token_version → corta sus sesiones al instante
        if disabled:
            r = c.execute(
                "UPDATE users SET disabled=1, token_version=token_version+1 WHERE id=?", (uid,))
            c.execute("UPDATE refresh_tokens SET revoked=1 WHERE user_id=?", (uid,))
        else:
            r = c.execute("UPDATE users SET disabled=0 WHERE id=?", (uid,))
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True, "disabled": disabled}


@router.delete("/{uid}")
def delete_user(uid: int, admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    with connect() as c:
        r = c.execute("DELETE FROM users WHERE id=?", (uid,))
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


# ---------------- ACL por carpeta ----------------

@router.get("/{uid}/acl")
def list_acl(uid: int, _: dict = Depends(require_admin)):
    with connect() as c:
        rows = c.execute(
            "SELECT folder_id, permission FROM acl WHERE user_id=? ORDER BY folder_id", (uid,)
        ).fetchall()
    return {"acl": [{"folderId": r["folder_id"], "permission": r["permission"]} for r in rows]}


@router.post("/{uid}/acl")
def set_acl(uid: int, body: SetAclBody, _: dict = Depends(require_admin)):
    if body.permission not in PERMS:
        raise HTTPException(status_code=400, detail="bad permission")
    if not get_folder(body.folderId):
        raise HTTPException(status_code=404, detail="folder not found")
    with connect() as c:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            raise HTTPException(status_code=404, detail="user not found")
        if body.permission == "none":
            # none = sin fila (default): borrar la ACL explícita si existía
            c.execute("DELETE FROM acl WHERE user_id=? AND folder_id=?", (uid, body.folderId))
        else:
            c.execute(
                "INSERT INTO acl (user_id, folder_id, permission) VALUES (?,?,?) "
                "ON CONFLICT(user_id, folder_id) DO UPDATE SET permission=excluded.permission",
                (uid, body.folderId, body.permission),
            )
    return {"ok": True}

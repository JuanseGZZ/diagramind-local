"""Versionado (git) + GitHub — ver [[25 - Conector Externo v2]] §7.

- `GET  /versions/status?id=`  (read)  → si el proyecto tiene cambios sin guardar + HEAD.
- `POST /versions/commit`      (write) → **Guardar** = commit del tree.json (autor = usuario);
                                          si GitHub está conectado, además **pushea**.
- `GET  /versions/log?id=`     (read)  → historial del proyecto.
- `GET  /versions/diff?id=&a=&b=` (read) → diff (sin a: HEAD vs working).
- `POST /versions/rollback`    (write) → restaura a un commit y difunde el estado al room.
- `POST /github/connect` · `GET /github/status` · `POST /github/push` · `POST /github/disconnect`
                                (admin) → cuenta GitHub del root.

> Nota de implementación: el doc §7 dice "Guardar requiere GitHub conectado". Acá se
> **relaja**: el commit local siempre funciona (gated por write) y el **push es automático
> si GitHub está conectado**. Así el conector self-hosted es usable sin una cuenta GitHub.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

import git_ops
import github
import realtime
import store
from auth import current_user, require_admin
from models import CommitBody, GithubConnectBody, IdBody, RollbackBody

router = APIRouter(tags=["versions"])


def _author(user: dict) -> tuple[str, str]:
    return user["username"], f"{user['username']}@connector.local"


def _need(user: dict, pid: str, level: str) -> None:
    """Chequea permiso sobre el proyecto ('read' o 'write'); 403/404 si no."""
    perm = store.project_permission(user, pid)
    if perm == "none":
        raise HTTPException(status_code=404 if not store.get_project(pid) else 403,
                            detail="no access to project")
    if level == "write" and perm != "write":
        raise HTTPException(status_code=403, detail="need write")


# ---------------- versionado ----------------

@router.get("/versions/status")
def versions_status(id: str = Query(...), user: dict = Depends(current_user)):
    _need(user, id, "read")
    return {"hasChanges": git_ops.has_changes(id), "head": git_ops.head(),
            "github": github.status()}


@router.post("/versions/commit")
def versions_commit(body: CommitBody, user: dict = Depends(current_user)):
    _need(user, body.id, "write")
    name, email = _author(user)
    msg = body.message or f"save {store.get_project(body.id)['name']} by {name}"
    try:
        res = git_ops.commit(body.id, name, email, msg)
    except ValueError:
        raise HTTPException(status_code=404, detail="project not found")
    pushed = None
    if res.get("committed") and github.is_connected():
        pushed = github.push()
    return {**res, "pushed": pushed}


@router.get("/versions/log")
def versions_log(id: str = Query(...), user: dict = Depends(current_user)):
    _need(user, id, "read")
    return {"commits": git_ops.log(id)}


@router.get("/versions/diff")
def versions_diff(id: str = Query(...), a: str | None = None, b: str | None = None,
                  user: dict = Depends(current_user)):
    _need(user, id, "read")
    return {"diff": git_ops.diff(id, a, b)}


@router.post("/versions/rollback")
async def versions_rollback(body: RollbackBody, user: dict = Depends(current_user)):
    _need(user, body.id, "write")
    name, email = _author(user)
    try:
        res = git_ops.rollback(body.id, body.commit, name, email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await realtime.push_canonical(body.id)   # los clientes conectados ven el rollback
    return res


# ---------------- GitHub (admin) ----------------

@router.get("/github/status")
def github_status(_: dict = Depends(require_admin)):
    return github.status()


@router.post("/github/connect")
def github_connect(body: GithubConnectBody, _: dict = Depends(require_admin)):
    github.connect(body.remoteUrl, body.token, body.branch)
    return github.status()


@router.post("/github/disconnect")
def github_disconnect(_: dict = Depends(require_admin)):
    github.disconnect()
    return {"ok": True}


@router.post("/github/push")
def github_push(_: dict = Depends(require_admin)):
    return github.push()

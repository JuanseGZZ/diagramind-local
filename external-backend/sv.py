"""Source Versions del modo editor — lado EXTERNO (doc 27, fase 4).

Contrato unificado `/sv/*` con el conector local: snapshots POR PROYECTO editor de
todos los archivos del target, guardados en
`<REPO_ROOT>/<folder.dirname>/<project.dirname>/source-versions/` (viajan y caen
con el proyecto). Lógica en `sourcever.py` (módulo espejado con el local).

Permisos: list/status/diff → ACL read; save/restore → write. El AUTOR es SIEMPRE
el usuario del token; si el cliente manda `author` (p.ej. "IA"), se anota como
sufijo — la IA firma sus versiones pero no puede suplantar a nadie.
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Query

import fs as fsmod
import sourcever
import store
import svgit
from auth import current_user, require_admin
from config import REPO_ROOT
from db import connect
from models import (GhConnectBody, GhProjectBody, GhPullBody, GhPushBody,
                    SvRestoreBody, SvSaveBody)

router = APIRouter(tags=["sourceversions"])


def _need(user: dict, pid: str, level: str) -> None:
    perm = store.project_permission(user, pid)
    if perm == "none":
        raise HTTPException(status_code=404 if not store.get_project(pid) else 403,
                            detail="no access to project")
    if level == "write" and perm != "write":
        raise HTTPException(status_code=403, detail="need write")


def _ctx(pid: str) -> tuple[str, str]:
    """(sv_dir, target) del proyecto editor `pid`."""
    target = fsmod.get_target(pid)
    if not target:
        raise HTTPException(status_code=400, detail="editor target not set")
    rel = store.project_reldir(pid)
    if not rel:
        raise HTTPException(status_code=404, detail="project not found")
    return os.path.join(str(REPO_ROOT), rel, "source-versions"), target


def _author(user: dict, client_author: str | None) -> str:
    return f"{user['username']} · {client_author}" if client_author else user["username"]


def _run(fn):
    try:
        return fn()
    except sourcever.SvError as e:
        raise HTTPException(status_code=e.code, detail=e.msg)


@router.get("/sv/list")
def sv_list(projectId: str = Query(...), user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    svd, _ = _ctx(projectId)
    return {"versions": sourcever.sv_list(svd)}


@router.get("/sv/status")
def sv_status(projectId: str = Query(...), user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    svd, target = _ctx(projectId)
    return _run(lambda: sourcever.sv_status(svd, target))


@router.get("/sv/diff")
def sv_diff(projectId: str = Query(...), path: str = Query(...), id: str = "",
            user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    svd, target = _ctx(projectId)
    return _run(lambda: sourcever.sv_diff(svd, target, id or None, path))


@router.post("/sv/save")
def sv_save(body: SvSaveBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    svd, target = _ctx(body.projectId)
    return _run(lambda: sourcever.sv_save(svd, target, _author(user, body.author), body.note))


@router.post("/sv/restore")
def sv_restore(body: SvRestoreBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    svd, target = _ctx(body.projectId)
    return _run(lambda: sourcever.sv_restore(svd, target, body.id, _author(user, body.author)))


# ---------------- GitHub por proyecto editor (doc 27, fase 4) ----------------
# Conexión {remoteUrl, token, branch} en la tabla editor_github (cascade con el
# proyecto). connect/disconnect → admin (guarda credenciales y toca el server);
# push/pull → write; status/log → read. El token nunca sale del server.

def _gh_conn(pid: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT remote_url, token, branch FROM editor_github WHERE project_id=?",
                      (pid,)).fetchone()
    if not r:
        return None
    return {"remoteUrl": r["remote_url"], "token": r["token"], "branch": r["branch"]}


def _gh_run(fn):
    try:
        return fn()
    except svgit.GitError as e:
        raise HTTPException(status_code=e.code, detail=e.msg)
    except sourcever.SvError as e:
        raise HTTPException(status_code=e.code, detail=e.msg)


@router.get("/svgit/status")
def svgit_status(projectId: str = Query(...), user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    _, target = _ctx(projectId)
    return svgit.gh_status(_gh_conn(projectId), target)


@router.get("/svgit/log")
def svgit_log(projectId: str = Query(...), n: int = 20, user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    _, target = _ctx(projectId)
    return svgit.gh_log(_gh_conn(projectId), target, n)


@router.post("/svgit/connect")
def svgit_connect(body: GhConnectBody, user: dict = Depends(require_admin)):
    _, target = _ctx(body.projectId)
    with connect() as c:
        c.execute(
            "INSERT INTO editor_github (project_id, remote_url, token, branch) VALUES (?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET remote_url=excluded.remote_url, "
            "token=excluded.token, branch=excluded.branch",
            (body.projectId, body.remoteUrl.strip(), (body.token or "").strip(),
             (body.branch or "main").strip() or "main"),
        )
    return svgit.gh_status(_gh_conn(body.projectId), target)


@router.post("/svgit/disconnect")
def svgit_disconnect(body: GhProjectBody, _: dict = Depends(require_admin)):
    with connect() as c:
        c.execute("DELETE FROM editor_github WHERE project_id=?", (body.projectId,))
    return {"ok": True}


@router.post("/svgit/push")
def svgit_push(body: GhPushBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    _, target = _ctx(body.projectId)
    by_ai = (body.author or "") == "IA"
    return _gh_run(lambda: svgit.gh_push(_gh_conn(body.projectId), target,
                                         body.message, user["username"], by_ai))


@router.post("/svgit/pull")
def svgit_pull(body: GhPullBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    svd, target = _ctx(body.projectId)
    return _gh_run(lambda: svgit.gh_pull(_gh_conn(body.projectId), target, body.ref,
                                         svd, _author(user, body.author)))

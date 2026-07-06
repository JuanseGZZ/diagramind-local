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
from auth import current_user
from config import REPO_ROOT
from models import SvRestoreBody, SvSaveBody

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

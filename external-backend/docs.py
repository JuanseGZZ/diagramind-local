"""Modo documents — blobs por hash, lado EXTERNO (doc 30, fase 4).

Contrato unificado `/docs/*` con el conector local: los BYTES de los documentos
viven content-addressed en `<REPO_ROOT>/<folder.dirname>/<project.dirname>/documents/<sha256>`
(viajan y **caen en cascada** con el proyecto). Los metadatos van en el tree.json,
que ya se espeja por los canales normales. Motor en `docsfs.py` (módulo espejado
con el local).

**Permisos (doc 30 decisión L)** — el alta de este contrato en el sistema de ACL
del conector, que era el requisito para habilitar el modo contra externos:

    list / get            → ACL **read**   de la carpeta del proyecto
    put / delete / gc     → ACL **write**

Además:
- El `hash` se valida como nombre de archivo (64 hex) y el `put` **recalcula el
  sha256** y lo compara: bytes que no son lo que dicen ser → 400 sin escribir.
  Un cliente con write no puede envenenar el store de otro.
- **Cuota por carpeta** (`quota.ensure_room`) antes de escribir: en un despliegue
  compartido (SaaS, doc 26) los documentos son el contenido más pesado.
"""
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

import docsfs
import quota
import store
from auth import current_user
from config import REPO_ROOT
from models import DocsGcBody, DocsHashBody

router = APIRouter(tags=["documents"])


def _need(user: dict, pid: str, level: str) -> None:
    perm = store.project_permission(user, pid)
    if perm == "none":
        raise HTTPException(status_code=404 if not store.get_project(pid) else 403,
                            detail="no access to project")
    if level == "write" and perm != "write":
        raise HTTPException(status_code=403, detail="need write")


def _project_dir(pid: str) -> str:
    """Dir del proyecto en el repo (el cliente NUNCA manda rutas)."""
    rel = store.project_reldir(pid)
    if not rel:
        raise HTTPException(status_code=404, detail="project not found")
    return os.path.join(str(REPO_ROOT), rel)


def _out(res: tuple[int, dict]) -> dict:
    code, body = res
    if code != 200:
        raise HTTPException(status_code=code, detail=body.get("error") or "error")
    return body


# ---------------- lectura (ACL read) ----------------

@router.get("/docs/list")
def docs_list(projectId: str = Query(...), user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    return _out(docsfs.docs_list(_project_dir(projectId)))


@router.get("/docs/get")
def docs_get(projectId: str = Query(...), hash: str = Query(...),
             user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    body = _out(docsfs.docs_get(_project_dir(projectId), hash))
    # bytes CRUDOS (sin base64): un PDF de 50 MB no debe inflarse un 33%
    return Response(content=body["bytes"], media_type="application/octet-stream")


# ---------------- escritura (ACL write) ----------------

@router.post("/docs/put")
async def docs_put(request: Request, projectId: str = Query(...), hash: str = Query(...),
                   user: dict = Depends(current_user)):
    _need(user, projectId, "write")
    data = await request.body()
    pdir = _project_dir(projectId)
    proj = store.get_project(projectId)
    if proj:
        try:
            quota.ensure_room(proj["folder_id"], len(data),
                              replaces=docsfs.blob_path(pdir, hash) if docsfs.valid_hash(hash) else None)
        except quota.QuotaExceeded as e:
            raise HTTPException(status_code=413, detail=str(e))
    return _out(docsfs.docs_put(pdir, hash, data))


@router.post("/docs/delete")
def docs_delete(body: DocsHashBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    return _out(docsfs.docs_delete(_project_dir(body.projectId), body.hash))


@router.post("/docs/gc")
def docs_gc(body: DocsGcBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    return _out(docsfs.docs_gc(_project_dir(body.projectId), body.keep))

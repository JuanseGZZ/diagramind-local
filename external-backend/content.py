"""REST de folders y projects (namespace canónico + permisos por carpeta).

Ver [[25 - Conector Externo v2]] §4/§6. El **mirror en vivo** (crear/editar nodos) va
por WebSocket (§5); este REST es para: crear carpetas (admin), listar lo **visible**
según ACL, crear proyectos (write), y la **lectura inicial** del árbol (read).

- `POST /folders` — solo **admin** crea carpetas.
- `GET /folders` — carpetas **visibles** (permiso != none) + el permiso del usuario.
- `POST /projects` — crea un proyecto en una carpeta (requiere **write** en la carpeta).
- `GET /projects?folderId=` — proyectos de una carpeta (requiere **read**).
- `GET /projects/tree?id=` — lectura inicial del árbol (requiere **read**).

El borrado con autoría (§F) y el `GET /repo` del dashboard llegan en el paso 4.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import current_user, require_admin
from models import CreateFolderBody, CreateProjectBody, IdBody
from projects import read_tree
import store


def _can_delete(user: dict, created_by) -> bool:
    """§F: borrar sólo el **creador** o un **admin**."""
    return user["role"] == "admin" or created_by == user["id"]

router = APIRouter(tags=["content"])


# ---------------- folders ----------------

@router.post("/folders")
def create_folder(body: CreateFolderBody, admin: dict = Depends(require_admin)):
    f = store.create_folder(body.name, admin["id"])
    return {"id": f["id"], "name": f["name"]}


@router.get("/folders")
def list_folders(user: dict = Depends(current_user)):
    return {"folders": store.visible_folders(user)}


# ---------------- projects ----------------

@router.post("/projects")
def create_project(body: CreateProjectBody, user: dict = Depends(current_user)):
    if store.folder_permission(user, body.folderId) != "write":
        raise HTTPException(status_code=403, detail="need write on folder")
    try:
        p = store.create_project(body.folderId, body.name, user["id"])
    except ValueError:
        raise HTTPException(status_code=404, detail="folder not found")
    return {"id": p["id"], "name": p["name"], "folderId": p["folderId"]}


@router.get("/projects")
def list_projects(folderId: str = Query(...), user: dict = Depends(current_user)):
    if store.folder_permission(user, folderId) == "none":
        raise HTTPException(status_code=403, detail="no access to folder")
    projs = store.list_projects(folderId)
    return {"projects": [{"id": p["id"], "name": p["name"]} for p in projs]}


@router.get("/projects/tree")
def project_tree(id: str = Query(...), user: dict = Depends(current_user)):
    if store.project_permission(user, id) == "none":
        raise HTTPException(status_code=403, detail="no access to project")
    return {"tree": read_tree(id)}


@router.post("/projects/delete")
def delete_project(body: IdBody, user: dict = Depends(current_user)):
    proj = store.get_project(body.id)
    if not proj:
        raise HTTPException(status_code=404, detail="project not found")
    if not _can_delete(user, proj["created_by"]):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete")
    store.delete_project(body.id)
    return {"ok": True}


@router.post("/folders/delete")
def delete_folder(body: IdBody, user: dict = Depends(current_user)):
    folder = store.get_folder(body.id)
    if not folder:
        raise HTTPException(status_code=404, detail="folder not found")
    if not _can_delete(user, folder["created_by"]):
        raise HTTPException(status_code=403, detail="only the creator or an admin can delete")
    store.delete_folder(body.id)
    return {"ok": True}


@router.get("/repo")
def repo(_: dict = Depends(require_admin)):
    """Árbol completo (carpetas + proyectos) para el dashboard."""
    return {"folders": store.repo_tree()}

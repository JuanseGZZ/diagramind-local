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

from auth import current_user, get_user_by_id, require_admin
from models import (CreateFolderBody, CreateProjectBody, IdBody, RenameBody,
                    SetProjectAclBody)
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
    """Sin permiso de carpeta, devuelve SOLO los proyectos compartidos con el usuario
    (ACL por proyecto). Así el invitado ve el documento que le compartieron y ninguno
    de los otros que viven en la misma carpeta."""
    projs = store.list_projects(folderId)
    if store.folder_permission(user, folderId) == "none":
        shared = store.shared_project_ids(user)
        projs = [p for p in projs if p["id"] in shared]
        if not projs:
            raise HTTPException(status_code=403, detail="no access to folder")
    # `permission` POR PROYECTO: con ACL de proyecto, dos documentos de la MISMA carpeta
    # pueden tener permisos distintos (uno read y otro write). El cliente tiene que usar
    # este valor y NO el de la carpeta, que para un invitado siempre es `read`.
    return {"projects": [{"id": p["id"], "name": p["name"],
                          "permission": store.project_permission(user, p["id"])}
                         for p in projs]}


@router.post("/projects/rename")
def rename_project(body: RenameBody, user: dict = Depends(current_user)):
    """Renombrar un proyecto. Requiere **write** sobre él (por carpeta o por ACL).
    Solo cambia el nombre visible: el `dirname` en disco es estable a propósito, así
    que renombrar no mueve archivos ni rompe el historial de git."""
    proj = store.get_project(body.id)
    if not proj:
        raise HTTPException(status_code=404, detail="project not found")
    if store.project_permission(user, body.id) != "write":
        raise HTTPException(status_code=403, detail="need write on the project")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="empty name")
    store.rename_project(body.id, name)
    return {"ok": True, "id": body.id, "name": name}


@router.post("/projects/acl")
def set_project_acl(body: SetProjectAclBody, user: dict = Depends(current_user)):
    """Comparte (o deja de compartir) UN proyecto con otro usuario de la instancia.

    Puede hacerlo quien tenga **write sobre la carpeta** del proyecto (el dueño) o un
    admin. A propósito NO alcanza con tener write por ACL de proyecto: quien recibió un
    documento compartido no puede re-compartirlo."""
    proj = store.get_project(body.projectId)
    if not proj:
        raise HTTPException(status_code=404, detail="project not found")
    if user["role"] != "admin" and store.folder_permission(user, proj["folder_id"]) != "write":
        raise HTTPException(status_code=403, detail="need write on the project's folder")
    if body.permission not in ("none", "read", "write"):
        raise HTTPException(status_code=400, detail="permission must be none|read|write")
    target = get_user_by_id(body.userId)
    if not target or target["disabled"]:
        raise HTTPException(status_code=404, detail="user not found")
    store.set_project_acl(body.userId, body.projectId, body.permission, user["id"])
    # Avisarle EN VIVO al afectado. Sin esto el revoke no era inmediato: el que perdía
    # el acceso seguía viendo (y editando en su copia local) el documento hasta que
    # recargaba la página. El aviso es best-effort — si no llega, el próximo sync lo
    # corrige igual — pero en el caso normal el documento le desaparece al instante.
    import realtime
    realtime.manager.notify_user_soon(body.userId, {
        "t": "acl", "projectId": body.projectId, "permission": body.permission,
        "projectName": proj["name"],
    })
    return {"ok": True, "projectId": body.projectId, "userId": body.userId,
            "permission": body.permission}


@router.get("/projects/acl")
def list_project_acl(projectId: str = Query(...), user: dict = Depends(current_user)):
    """Con quién está compartido un proyecto. Mismo criterio que para compartir."""
    proj = store.get_project(projectId)
    if not proj:
        raise HTTPException(status_code=404, detail="project not found")
    if user["role"] != "admin" and store.folder_permission(user, proj["folder_id"]) != "write":
        raise HTTPException(status_code=403, detail="need write on the project's folder")
    return {"shared": [{"userId": r["user_id"], "username": r["username"],
                        "permission": r["permission"], "createdAt": r["created_at"]}
                       for r in store.project_acl_list(projectId)]}


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

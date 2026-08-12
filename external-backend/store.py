"""Namespace canónico: folders y projects con **ids estables asignados por el conector**
(ver [[25 - Conector Externo v2]] §6) + permisos por carpeta (§3).

- El **id** es la identidad (lo asigna el conector, nunca el cliente). Renombrar = cambiar
  solo `name` (metadata); el `id` y el `dirname` en disco **no cambian** → no rompe nada.
- El **dirname** en disco es legible (derivado del nombre al crear) y estable.
- Disco: `<root>/<folder.dirname>/<project.dirname>/tree.json`.

Permisos (§3): ACL por carpeta `none|read|write`. Sin fila = `none` (ni ve la carpeta).
`admin` → `write` en todo. El permiso de un proyecto = el de su carpeta.
"""

import re
import secrets
import shutil

from db import connect
from config import REPO_ROOT

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_name(name: str, fallback: str = "untitled") -> str:
    s = _SAFE.sub("_", (name or "").strip()).strip("._")
    return (s or fallback)[:80]


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(7)     # p.ej. "f1a2b3c4d5e6f7" — estable, opaco


# ---------------- folders ----------------

def create_folder(name: str, created_by: int) -> dict:
    base = safe_name(name, "folder")
    fid = _new_id("f")
    with connect() as c:
        dirname = base
        i = 2
        while c.execute("SELECT 1 FROM folders WHERE dirname=?", (dirname,)).fetchone():
            dirname = f"{base}-{i}"; i += 1
        c.execute(
            "INSERT INTO folders (id, name, dirname, created_by) VALUES (?,?,?,?)",
            (fid, name, dirname, created_by),
        )
    (REPO_ROOT / dirname).mkdir(parents=True, exist_ok=True)
    return {"id": fid, "name": name, "dirname": dirname}


def get_folder(fid: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        return dict(r) if r else None


def list_folders() -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM folders ORDER BY name").fetchall()]


def get_folder_by_dirname(dirname: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM folders WHERE dirname=?", (dirname,)).fetchone()
        return dict(r) if r else None


# ---------------- projects ----------------

def create_project(folder_id: str, name: str, created_by: int) -> dict:
    if not get_folder(folder_id):
        raise ValueError("folder not found")
    base = safe_name(name, "project")
    pid = _new_id("p")
    with connect() as c:
        dirname = base
        i = 2
        while c.execute(
            "SELECT 1 FROM projects WHERE folder_id=? AND dirname=?", (folder_id, dirname)
        ).fetchone():
            dirname = f"{base}-{i}"; i += 1
        c.execute(
            "INSERT INTO projects (id, folder_id, name, dirname, created_by) VALUES (?,?,?,?,?)",
            (pid, folder_id, name, dirname, created_by),
        )
    return {"id": pid, "folderId": folder_id, "name": name, "dirname": dirname}


def get_project(pid: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def list_projects(folder_id: str) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM projects WHERE folder_id=? ORDER BY name", (folder_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def rename_project(pid: str, name: str) -> bool:
    """Cambia SOLO el nombre visible. El `dirname` es estable a propósito: renombrar no
    mueve nada en disco ni rompe el historial de git del proyecto."""
    with connect() as c:
        r = c.execute("UPDATE projects SET name=? WHERE id=?", (name, pid))
        return r.rowcount > 0


def project_reldir(pid: str) -> str | None:
    """Ruta relativa `<folder.dirname>/<project.dirname>` de un proyecto, o None."""
    proj = get_project(pid)
    if not proj:
        return None
    folder = get_folder(proj["folder_id"])
    if not folder:
        return None
    return f"{folder['dirname']}/{proj['dirname']}"


def delete_project(pid: str) -> bool:
    """Borra el proyecto (row + dir en disco). Recuperable vía git una vez que el
    versionado esté (§8); por ahora saca el working tree del proyecto."""
    rel = project_reldir(pid)
    with connect() as c:
        r = c.execute("DELETE FROM projects WHERE id=?", (pid,))
        deleted = r.rowcount > 0
    if deleted and rel:
        shutil.rmtree(REPO_ROOT / rel, ignore_errors=True)
    return deleted


def delete_folder(fid: str) -> bool:
    """Borra la carpeta: ACL de esa carpeta + proyectos (cascade) + dir en disco."""
    f = get_folder(fid)
    if not f:
        return False
    with connect() as c:
        c.execute("DELETE FROM acl WHERE folder_id=?", (fid,))     # ACL no tiene FK cascade
        c.execute("DELETE FROM folders WHERE id=?", (fid,))        # projects caen por FK cascade
    shutil.rmtree(REPO_ROOT / f["dirname"], ignore_errors=True)
    return True


def repo_tree() -> list[dict]:
    """Árbol completo (carpetas + sus proyectos) para el dashboard (§4)."""
    out = []
    for f in list_folders():
        projs = list_projects(f["id"])
        out.append({
            "id": f["id"], "name": f["name"], "dirname": f["dirname"],
            "createdBy": f["created_by"], "createdAt": f["created_at"],
            "projects": [{
                "id": p["id"], "name": p["name"], "dirname": p["dirname"],
                "createdBy": p["created_by"], "createdAt": p["created_at"],
            } for p in projs],
        })
    return out


# ---------------- permisos ----------------

def folder_permission(user: dict, folder_id: str) -> str:
    """'none' | 'read' | 'write' para (usuario, carpeta). admin → write; sin ACL → none."""
    if user["role"] == "admin":
        return "write"
    with connect() as c:
        r = c.execute(
            "SELECT permission FROM acl WHERE user_id=? AND folder_id=?",
            (user["id"], folder_id),
        ).fetchone()
    return r["permission"] if r else "none"


_RANK = {"none": 0, "read": 1, "write": 2}


def project_acl_permission(user: dict, project_id: str) -> str:
    """Permiso otorgado DIRECTAMENTE sobre el proyecto (compartir un documento suelto),
    ignorando el de su carpeta. 'none' si no hay."""
    with connect() as c:
        r = c.execute(
            "SELECT permission FROM project_acl WHERE user_id=? AND project_id=?",
            (user["id"], project_id),
        ).fetchone()
    return r["permission"] if r else "none"


def project_permission(user: dict, project_id: str) -> str:
    """Permiso efectivo sobre un proyecto: el MAYOR entre el de su carpeta y el que le
    hayan dado sobre ese proyecto puntual. 'none' si el proyecto no existe.

    El máximo (y no un override) es a propósito: compartir un documento con permiso
    `read` NO puede degradar al dueño, que tiene `write` por la carpeta."""
    proj = get_project(project_id)
    if not proj:
        return "none"
    if user["role"] == "admin":
        return "write"
    by_folder = folder_permission(user, proj["folder_id"])
    by_project = project_acl_permission(user, project_id)
    return by_folder if _RANK[by_folder] >= _RANK[by_project] else by_project


def set_project_acl(user_id: int, project_id: str, permission: str,
                    granted_by: int | None = None) -> None:
    """Otorga (o revoca con 'none') el permiso de UN usuario sobre UN proyecto."""
    with connect() as c:
        if permission == "none":
            c.execute("DELETE FROM project_acl WHERE user_id=? AND project_id=?",
                      (user_id, project_id))
        else:
            c.execute(
                "INSERT INTO project_acl (user_id, project_id, permission, granted_by) "
                "VALUES (?,?,?,?) ON CONFLICT(user_id, project_id) DO UPDATE SET "
                "permission=excluded.permission, granted_by=excluded.granted_by",
                (user_id, project_id, permission, granted_by),
            )


def project_acl_list(project_id: str) -> list[dict]:
    """Con quién está compartido un proyecto (para mostrarlo y poder revocar)."""
    with connect() as c:
        rows = c.execute(
            "SELECT a.user_id, a.permission, a.created_at, u.username "
            "FROM project_acl a JOIN users u ON u.id = a.user_id "
            "WHERE a.project_id=? ORDER BY a.created_at", (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def shared_project_ids(user: dict) -> set[str]:
    """Proyectos a los que el usuario llega SOLO por ACL de proyecto."""
    with connect() as c:
        rows = c.execute("SELECT project_id FROM project_acl WHERE user_id=?",
                         (user["id"],)).fetchall()
    return {r["project_id"] for r in rows}


def visible_folders(user: dict) -> list[dict]:
    """Carpetas que el usuario puede ver + su permiso.

    Incluye dos casos:
    - permiso de CARPETA != none (el de siempre);
    - carpetas ajenas donde le compartieron ALGÚN proyecto suelto (`shared: True`).
      Ahí se informa `read` para que el cliente la muestre como solo-lectura, pero eso
      NO es un permiso de carpeta: `GET /projects` devuelve únicamente los proyectos
      compartidos, `create_project` sigue exigiendo write real y `fs`/versiones se
      autorizan por proyecto. La carpeta se lista solo para poder navegar hasta el
      documento compartido.
    """
    shared = shared_project_ids(user)
    shared_folders = set()
    if shared:
        with connect() as c:
            qmarks = ",".join("?" * len(shared))
            rows = c.execute(
                f"SELECT DISTINCT folder_id FROM projects WHERE id IN ({qmarks})",
                tuple(shared),
            ).fetchall()
        shared_folders = {r["folder_id"] for r in rows}

    out = []
    for f in list_folders():
        perm = folder_permission(user, f["id"])
        if perm != "none":
            out.append({"id": f["id"], "name": f["name"], "permission": perm})
        elif f["id"] in shared_folders:
            out.append({"id": f["id"], "name": f["name"], "permission": "read",
                        "shared": True})
    return out

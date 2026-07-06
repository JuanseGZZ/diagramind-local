"""Modo editor — target por proyecto + operaciones de filesystem confinadas (doc 27).

Contrato unificado `/editor/target` + `/fs/*` (el conector LOCAL implementa el mismo).
Reglas:
- El **target** se registra server-side por `projectId` (admin); las operaciones nunca
  reciben rutas absolutas del cliente: `path` es RELATIVO al target.
- **Confinamiento**: `realpath(target/path)` debe quedar dentro de `realpath(target)`
  (resuelve symlinks → un link que apunte afuera no sirve de escape).
- Permisos: lectura → ACL read de la carpeta; escritura → write; target/exec → admin.
- `exec` corre con `cwd=target`, timeout 60 s. En despliegues COMPARTIDOS (SaaS free,
  doc 26) se deshabilita con env `DMC_DISABLE_EXEC=1`.
- Límites: read ≤ 2 MB (+flag truncated / binary), listado ≤ 500 entradas por dir,
  grep ≤ 200 matches (archivos > 1 MB se saltean; `.git` se ignora).
"""

import fnmatch
import os
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query

import store
from auth import current_user, require_admin
from db import connect
from models import EditorTargetBody, FsExecBody, FsPathBody, FsRenameBody, FsWriteBody

router = APIRouter(tags=["editor"])

MAX_READ = 2 * 1024 * 1024
MAX_ENTRIES = 500
MAX_MATCHES = 200
GREP_FILE_CAP = 1 * 1024 * 1024
EXEC_TIMEOUT = 60


# ---------------- target ----------------

def get_target(project_id: str) -> str | None:
    with connect() as c:
        r = c.execute("SELECT target FROM editor_targets WHERE project_id=?", (project_id,)).fetchone()
        return r["target"] if r else None


@router.post("/editor/target")
def set_target(body: EditorTargetBody, _: dict = Depends(require_admin)):
    if not store.get_project(body.projectId):
        raise HTTPException(status_code=404, detail="project not found")
    target = os.path.realpath(body.path)
    os.makedirs(target, exist_ok=True)
    with connect() as c:
        c.execute(
            "INSERT INTO editor_targets (project_id, target) VALUES (?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET target=excluded.target",
            (body.projectId, target),
        )
    return {"path": target}


@router.get("/editor/target")
def read_target(projectId: str = Query(...), user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    return {"path": get_target(projectId)}


# ---------------- helpers ----------------

def _need(user: dict, pid: str, level: str) -> None:
    perm = store.project_permission(user, pid)
    if perm == "none":
        raise HTTPException(status_code=404 if not store.get_project(pid) else 403,
                            detail="no access to project")
    if level == "write" and perm != "write":
        raise HTTPException(status_code=403, detail="need write")


def _resolve(pid: str, rel: str | None) -> tuple[str, str]:
    """(base, abs) con confinamiento. 400 si no hay target o si la ruta escapa."""
    target = get_target(pid)
    if not target:
        raise HTTPException(status_code=400, detail="editor target not set")
    base = os.path.realpath(target)
    p = os.path.realpath(os.path.join(base, rel or "."))
    if p != base and not p.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="path escapes target")
    return base, p


# ---------------- fs ops ----------------

@router.get("/fs/tree")
def fs_tree(projectId: str = Query(...), dir: str = "", user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    _, p = _resolve(projectId, dir)
    if not os.path.isdir(p):
        raise HTTPException(status_code=404, detail="not a directory")
    out = []
    try:
        names = sorted(os.listdir(p))
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    for name in names[: MAX_ENTRIES]:
        fp = os.path.join(p, name)
        is_dir = os.path.isdir(fp)
        size = 0 if is_dir else (os.path.getsize(fp) if os.path.isfile(fp) else 0)
        out.append({"name": name, "dir": is_dir, "size": size})
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"entries": out, "truncated": len(names) > MAX_ENTRIES}


@router.get("/fs/read")
def fs_read(projectId: str = Query(...), path: str = Query(...), user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    _, p = _resolve(projectId, path)
    if not os.path.isfile(p):
        raise HTTPException(status_code=404, detail="file not found")
    with open(p, "rb") as f:
        raw = f.read(MAX_READ + 1)
    truncated = len(raw) > MAX_READ
    try:
        content = raw[:MAX_READ].decode("utf-8")
    except UnicodeDecodeError:
        return {"binary": True, "size": os.path.getsize(p)}
    return {"content": content, "truncated": truncated}


@router.post("/fs/write")
def fs_write(body: FsWriteBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    _, p = _resolve(body.projectId, body.path)
    if os.path.isdir(p):
        raise HTTPException(status_code=400, detail="path is a directory")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"ok": True}


@router.post("/fs/mkdir")
def fs_mkdir(body: FsPathBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    _, p = _resolve(body.projectId, body.path)
    os.makedirs(p, exist_ok=True)
    return {"ok": True}


@router.post("/fs/delete")
def fs_delete(body: FsPathBody, user: dict = Depends(current_user)):
    _need(user, body.projectId, "write")
    base, p = _resolve(body.projectId, body.path)
    if p == base:
        raise HTTPException(status_code=400, detail="cannot delete the target root")
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.exists(p):
        os.remove(p)
    return {"ok": True}


@router.post("/fs/rename")
def fs_rename(body: FsRenameBody, user: dict = Depends(current_user)):
    """Renombra/mueve DENTRO del target (os.rename: atómico, sirve para dirs y
    binarios). No pisa destinos existentes."""
    _need(user, body.projectId, "write")
    base, src = _resolve(body.projectId, body.from_)
    _, dst = _resolve(body.projectId, body.to)
    if src == base:
        raise HTTPException(status_code=400, detail="cannot rename the target root")
    if not os.path.exists(src):
        raise HTTPException(status_code=404, detail="source not found")
    if os.path.exists(dst):
        raise HTTPException(status_code=409, detail="destination already exists")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.get("/fs/grep")
def fs_grep(projectId: str = Query(...), q: str = Query(...), glob: str = "",
            user: dict = Depends(current_user)):
    _need(user, projectId, "read")
    base, _ = _resolve(projectId, ".")
    matches = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for name in files:
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, base)
            if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(name, glob):
                continue
            try:
                if os.path.getsize(fp) > GREP_FILE_CAP:
                    continue
                with open(fp, "r", encoding="utf-8", errors="strict") as f:
                    for i, line in enumerate(f, 1):
                        if q in line:
                            matches.append({"path": rel, "line": i, "text": line.rstrip()[:300]})
                            if len(matches) >= MAX_MATCHES:
                                return {"matches": matches, "truncated": True}
            except (OSError, UnicodeDecodeError):
                continue
    return {"matches": matches, "truncated": False}


@router.post("/fs/exec")
def fs_exec(body: FsExecBody, user: dict = Depends(require_admin)):
    if os.environ.get("DMC_DISABLE_EXEC"):
        raise HTTPException(status_code=403, detail="exec disabled on this connector")
    base, _ = _resolve(body.projectId, ".")
    try:
        r = subprocess.run(body.cmd, shell=True, cwd=base, capture_output=True,
                           text=True, timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"code": -1, "stdout": "", "stderr": f"timeout ({EXEC_TIMEOUT}s)"}
    return {"code": r.returncode, "stdout": r.stdout[-20000:], "stderr": r.stderr[-20000:]}

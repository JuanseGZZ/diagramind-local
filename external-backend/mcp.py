"""MCP por carpeta — cada carpeta del conector se puede exponer como un **server MCP
remoto** (streamable HTTP) para que un chat de Claude (u otro cliente MCP) trabaje
sobre ELLA (ver [[26 - Nube DiagraMinder]] §6).

- Un **token por (carpeta, usuario)**: `POST /mcp/<token>` autentica por el token
  propio, SIN JWT (mismo modelo que los webhooks del orquestador — en la nube estas
  rutas se excluyen del auth-gate del proxy). El permiso efectivo de cada tool es la
  **ACL de ese usuario sobre esa carpeta**, re-chequeada en cada request → revocar el
  token, la ACL o deshabilitar al usuario corta el MCP al instante.
- **Alcance = LA carpeta**: los proyectos se validan contra su `folder_id` y las tools
  de archivos quedan confinadas al dir de la carpeta (mismo realpath-check que /fs).
- Emisión/listado/revocación por REST con sesión: cada usuario para sus carpetas
  visibles; un admin para cualquier (usuario, carpeta) — así el back central de la
  nube emite los MCP de los usuarios free, y el dashboard los de la instancia.
- Protocolo: JSON-RPC 2.0 sobre POST (initialize / tools/list / tools/call),
  **stateless** (sin Mcp-Session-Id) y con respuesta JSON directa (sin SSE).
  Escribir un tree.json (write_project o fs_write) difunde el estado al room WS:
  los clientes conectados ven el cambio de la IA EN VIVO.
"""

import fnmatch
import json
import os
import secrets
import shutil
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

import config
import git_ops
import github
import quota
import realtime
import store
from auth import current_user, get_user_by_id
from config import REPO_ROOT
from db import connect
from fs import GREP_FILE_CAP, MAX_ENTRIES, MAX_MATCHES, MAX_READ
from models import McpTokenCreateBody, McpTokenRevokeBody
from projects import TypeNotAllowed, read_tree, write_tree

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-03-26"
RATE_MAX = int(os.environ.get("DMC_MCP_RATE_MAX", "120"))   # requests/min por token
_RATE: dict[int, list[float]] = {}


class ToolError(Exception):
    """Error de UNA tool: viaja como result con isError=true (no rompe el transporte)."""


# ---------------- emisión / gestión de tokens (REST, sesión) ----------------

@router.post("/mcp/tokens")
def create_token(body: McpTokenCreateBody, user: dict = Depends(current_user)):
    """Emite la URL MCP de una carpeta, ligada a un usuario (default: uno mismo).
    Emitir para OTRO usuario es de admin (el back central provisiona así los free)."""
    target = user
    if body.userId is not None and body.userId != user["id"]:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="solo un admin emite MCPs para otro usuario")
        target = get_user_by_id(body.userId)
        if not target or target["disabled"]:
            raise HTTPException(status_code=404, detail="user not found")
    folder = store.get_folder(body.folderId)
    if not folder:
        raise HTTPException(status_code=404, detail="folder not found")
    if store.folder_permission(target, folder["id"]) == "none":
        raise HTTPException(status_code=403, detail="el usuario no tiene acceso a esa carpeta")
    token = "m" + secrets.token_urlsafe(24)
    with connect() as c:
        cur = c.execute(
            "INSERT INTO mcp_tokens (token, folder_id, user_id, name) VALUES (?,?,?,?)",
            (token, folder["id"], target["id"], body.name.strip()),
        )
        tid = cur.lastrowid
    return {"id": tid, "token": token, "path": f"/mcp/{token}",
            "folderId": folder["id"], "userId": target["id"]}


@router.get("/mcp/tokens")
def list_tokens(user: dict = Depends(current_user)):
    """Tokens activos: admin ve todos; un usuario, solo los suyos."""
    q = ("SELECT t.id, t.token, t.name, t.created_at, t.last_used, "
         "       f.id AS folder_id, f.name AS folder_name, u.id AS uid, u.username "
         "FROM mcp_tokens t JOIN folders f ON f.id=t.folder_id "
         "JOIN users u ON u.id=t.user_id WHERE t.revoked=0")
    args: tuple = ()
    if user["role"] != "admin":
        q += " AND t.user_id=?"; args = (user["id"],)
    with connect() as c:
        rows = c.execute(q + " ORDER BY t.created_at DESC", args).fetchall()
    return {"tokens": [{
        "id": r["id"], "token": r["token"], "path": f"/mcp/{r['token']}",
        "name": r["name"], "folderId": r["folder_id"], "folderName": r["folder_name"],
        "userId": r["uid"], "username": r["username"],
        "createdAt": r["created_at"], "lastUsed": r["last_used"],
    } for r in rows]}


@router.post("/mcp/tokens/revoke")
def revoke_token(body: McpTokenRevokeBody, user: dict = Depends(current_user)):
    with connect() as c:
        row = c.execute("SELECT user_id FROM mcp_tokens WHERE id=?", (body.id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="token not found")
        if user["role"] != "admin" and row["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="solo el dueño o un admin revocan")
        c.execute("UPDATE mcp_tokens SET revoked=1 WHERE id=?", (body.id,))
    return {"ok": True}


# ---------------- contexto por request (token → usuario+carpeta+permiso) ----------------

def _auth(token: str) -> dict:
    """ctx {folder, user, perm} del token, o 401. Re-chequea TODO en cada request."""
    with connect() as c:
        row = c.execute(
            "SELECT * FROM mcp_tokens WHERE token=? AND revoked=0", (token,)
        ).fetchone()
        if row:
            c.execute("UPDATE mcp_tokens SET last_used=datetime('now') WHERE id=?", (row["id"],))
    if not row:
        raise HTTPException(status_code=401, detail="unknown mcp token")
    user = get_user_by_id(row["user_id"])
    folder = store.get_folder(row["folder_id"])
    if not user or user["disabled"] or not folder:
        raise HTTPException(status_code=401, detail="mcp token unavailable")
    perm = store.folder_permission(user, folder["id"])
    if perm == "none":
        raise HTTPException(status_code=401, detail="mcp token unavailable")
    now = time.time()
    stamps = [t for t in _RATE.get(row["id"], []) if now - t < 60]
    if len(stamps) >= RATE_MAX:
        _RATE[row["id"]] = stamps
        raise HTTPException(status_code=429, detail="rate limited — slow down")
    stamps.append(now)
    _RATE[row["id"]] = stamps
    return {"folder": folder, "user": user, "perm": perm, "push": [], "repo": []}


def _resolve(ctx: dict, rel: str | None) -> tuple[str, str]:
    """(base, abs) confinado al dir de la carpeta (mismo realpath-check que fs.py)."""
    base = os.path.realpath(str(REPO_ROOT / ctx["folder"]["dirname"]))
    p = os.path.realpath(os.path.join(base, rel or "."))
    if p != base and not p.startswith(base + os.sep):
        raise ToolError("path escapes folder")
    return base, p


def _project(ctx: dict, pid: str) -> dict:
    """El proyecto, validando que pertenezca a LA carpeta del token."""
    proj = store.get_project(pid)
    if not proj or proj["folder_id"] != ctx["folder"]["id"]:
        raise ToolError("project not found in this folder")
    return proj


def _mark_tree_touched(ctx: dict, rel: str) -> None:
    """Si el path relativo tocado es el tree.json de un proyecto, difundir al room."""
    parts = rel.strip("/").split("/")
    if len(parts) != 2 or parts[1] != "tree.json":
        return
    for p in store.list_projects(ctx["folder"]["id"]):
        if p["dirname"] == parts[0]:
            ctx["push"].append(p["id"])
            return



# ---------------- contexto general (para que el modelo NO adivine) ----------------
# Los esquemas NO se redactan acá: se sirven las SKILLS del dominio
# (`skills_domain.py`, espejo de local-backend/skills.py) — las mismas que leen
# Claude Code / Codex / Gemini cuando trabajan en local. Escribirlas a mano ya salió
# mal una vez: se documentó `{"nodes":[],"edges":[]}`, que no existe, y los proyectos
# quedaban vacíos. Fuente única = las skills.

import skills_domain

# nombre de la skill por tipo de proyecto (las claves son `diagramind-<tipo>`)
_SKILL_BY_TYPE = {
    "cart": "diagramind-cart",
    "freestyle": "diagramind-freestyle",
    "treeQuestionary": "diagramind-treequestionary",
    "activities": "diagramind-activities",
    "object": "diagramind-object",
    "editor": "diagramind-editor",
    "orchestrator": "diagramind-orchestrator",
    "documents": "diagramind-documents",
}


def _strip_front_matter(md: str) -> str:
    """Las skills traen front-matter YAML (name/description) para Claude Code; por MCP
    solo interesa el cuerpo."""
    if md.startswith("---"):
        end = md.find("---", 3)
        if end != -1:
            return md[end + 3:].strip()
    return md.strip()


def _skill_body(key: str) -> str:
    return _strip_front_matter(skills_domain.SKILLS.get(key, ""))


# ---------------- tools ----------------
# (name, permiso requerido, description, propiedades del inputSchema, required)
# Las descriptions van al MODELO (MCP) → en inglés, ver doc 20 §L.

_TOOLS = [
    ("general_context", "read",
     "READ THIS FIRST, before creating or editing anything. Returns the DiagraMind "
     "domain knowledge: the common format rules and the EXACT tree.json schema of each "
     "project type (cart, freestyle, treeQuestionary, activities, object, editor, "
     "orchestrator, documents), plus the recommended workflow. Pass `projectType` to "
     "get only that type's schema and save tokens. Never guess the schema: the field "
     "names are literal and a wrong shape produces an EMPTY project.",
     {"projectType": {"type": "string",
                      "description": "optional: cart | freestyle | treeQuestionary | "
                                     "activities | object | editor | orchestrator | "
                                     "documents. Omit to get them all."}}, []),
    ("list_projects", "read",
     "Lists the projects (diagrams) of the folder.",
     {}, []),
    ("read_project", "read",
     "Returns the complete tree (tree.json) of a project.",
     {"projectId": {"type": "string"}}, ["projectId"]),
    ("project_log", "read",
     "History of the saved versions (commits) of a project.",
     {"projectId": {"type": "string"}}, ["projectId"]),
    ("fs_tree", "read",
     "Lists a directory of the folder. `dir` is relative to the folder ('' = the root).",
     {"dir": {"type": "string"}}, []),
    ("fs_read", "read",
     "Reads a text file of the folder (relative path).",
     {"path": {"type": "string"}}, ["path"]),
    ("fs_grep", "read",
     "Searches literal text across the files of the folder. `glob` is optional (e.g. *.md).",
     {"q": {"type": "string"}, "glob": {"type": "string"}}, ["q"]),
    ("create_project", "write",
     "Creates a new project (diagram) in the folder and returns its id.",
     {"name": {"type": "string"}}, ["name"]),
    ("write_project", "write",
     "Replaces the tree (tree.json) of a project. Connected users see it "
     "live. `tree` is the tree JSON (a string or an object).",
     {"projectId": {"type": "string"}, "tree": {}}, ["projectId", "tree"]),
    ("save_project", "write",
     "Saves a version (a git commit) of the project's current state.",
     {"projectId": {"type": "string"}, "message": {"type": "string"}}, ["projectId"]),
    ("delete_project", "write",
     "Deletes a project from the folder (only its creator or an admin).",
     {"projectId": {"type": "string"}}, ["projectId"]),
    ("fs_write", "write",
     "Writes a text file in the folder (relative path; it creates the dirs).",
     {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    ("fs_mkdir", "write",
     "Creates a directory inside the folder.",
     {"path": {"type": "string"}}, ["path"]),
    ("fs_rename", "write",
     "Renames/moves a file or dir INSIDE the folder. It does not overwrite destinations.",
     {"from": {"type": "string"}, "to": {"type": "string"}}, ["from", "to"]),
    ("fs_delete", "write",
     "Deletes a file or directory of the folder.",
     {"path": {"type": "string"}}, ["path"]),
]


def _tool_defs(perm: str) -> list[dict]:
    """Defs MCP visibles para el permiso del token (read-only no ve las de write)."""
    return [{
        "name": name,
        "description": desc,
        "inputSchema": {"type": "object", "properties": props, "required": req},
    } for name, need, desc, props, req in _TOOLS if perm == "write" or need == "read"]


def _author(user: dict) -> tuple[str, str]:
    return user["username"], f"{user['username']}@connector.local"


def _call_tool(ctx: dict, name: str, a: dict):
    perm_needed = next((need for n, need, *_ in _TOOLS if n == name), None)
    if perm_needed is None:
        raise ToolError(f"unknown tool {name!r}")
    if perm_needed == "write" and ctx["perm"] != "write":
        raise ToolError("este MCP es de solo lectura (ACL read)")
    fid, user = ctx["folder"]["id"], ctx["user"]

    if name == "general_context":
        want = (a.get("projectType") or "").strip()
        types = {}
        for t, key in _SKILL_BY_TYPE.items():
            if want and t != want:
                continue
            types[t] = _skill_body(key)
        return {
            "folder": {"id": fid, "name": ctx["folder"]["name"], "permission": ctx["perm"]},
            "whatIsThis": "DiagraMind projects are visual diagrams. Each project is one "
                          "tree.json whose root field `type` decides the schema. This "
                          "folder is one user's workspace in the DiagraMinder cloud.",
            # Reglas comunes a TODOS los tipos (ids enteros, bumpear contadores, nunca
            # cambiar `type`, colores hex, <br> para saltos de línea, …).
            "format": _skill_body("diagramind-format"),
            # Esquema EXACTO por tipo. Con `projectType` viene solo ese (menos tokens).
            "schemas": types,
            "workflow": [
                "1. `list_projects` to see what already exists.",
                "2. If you are unsure, `read_project` on an existing project OF THE SAME "
                "TYPE: a real example never lies.",
                "3. `create_project`, then `write_project` following the schema of the "
                "type EXACTLY (never invent or rename fields).",
                "4. `read_project` again to confirm what got stored is what you meant.",
                "5. `save_project` to commit a version with a message.",
                "The user sees every change LIVE in the web; no reload needed.",
            ],
            "commonMistake": "Inventing a shape like {\"nodes\":[...],\"edges\":[...]}. "
                             "It does NOT exist in DiagraMind and produces a project "
                             "that opens completely EMPTY. Follow `schemas` literally.",
        }

    if name == "list_projects":
        return {"projects": [{"id": p["id"], "name": p["name"]}
                             for p in store.list_projects(fid)]}
    if name == "read_project":
        pid = _project(ctx, a["projectId"])["id"]
        tree = read_tree(pid)
        return tree if tree is not None else {"tree": None, "note": "el proyecto aún no tiene contenido"}
    if name == "project_log":
        return {"commits": git_ops.log(_project(ctx, a["projectId"])["id"])}
    if name == "create_project":
        p = store.create_project(fid, a["name"], user["id"])
        ctx["repo"].append({"action": "created", "projectId": p["id"], "name": p["name"]})
        return {"id": p["id"], "name": p["name"]}
    if name == "write_project":
        pid = _project(ctx, a["projectId"])["id"]
        tree = a["tree"]
        if not isinstance(tree, str):
            tree = json.dumps(tree)
        try:
            write_tree(pid, tree)
        except TypeNotAllowed as e:
            # Un asistente de IA tampoco puede meter un Editor/Orchestrator en una
            # instancia compartida: el gate vive en write_tree, así que vale igual acá.
            raise ToolError(str(e))
        except ValueError:
            raise ToolError("tree is not valid JSON")
        ctx["push"].append(pid)
        return {"ok": True}
    if name == "save_project":
        pid = _project(ctx, a["projectId"])["id"]
        nm, email = _author(user)
        msg = a.get("message") or f"save {store.get_project(pid)['name']} by {nm} (MCP)"
        res = git_ops.commit(pid, nm, email, msg)
        if res.get("committed") and github.is_connected():
            res["pushed"] = github.push()
        return res
    if name == "delete_project":
        ctx["repo"].append({"action": "deleted", "projectId": a.get("projectId", "")})
        proj = _project(ctx, a["projectId"])
        if user["role"] != "admin" and proj["created_by"] != user["id"]:
            raise ToolError("solo el creador o un admin borran un proyecto")
        store.delete_project(proj["id"])
        return {"ok": True}

    if name == "fs_tree":
        _, p = _resolve(ctx, a.get("dir") or "")
        if not os.path.isdir(p):
            raise ToolError("not a directory")
        names = sorted(os.listdir(p))
        out = []
        for n in names[:MAX_ENTRIES]:
            fp = os.path.join(p, n)
            is_dir = os.path.isdir(fp)
            out.append({"name": n, "dir": is_dir,
                        "size": 0 if is_dir else (os.path.getsize(fp) if os.path.isfile(fp) else 0)})
        out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"entries": out, "truncated": len(names) > MAX_ENTRIES}
    if name == "fs_read":
        _, p = _resolve(ctx, a["path"])
        if not os.path.isfile(p):
            raise ToolError("file not found")
        with open(p, "rb") as f:
            raw = f.read(MAX_READ + 1)
        try:
            return {"content": raw[:MAX_READ].decode("utf-8"), "truncated": len(raw) > MAX_READ}
        except UnicodeDecodeError:
            raise ToolError("binary file")
    if name == "fs_grep":
        base, _ = _resolve(ctx, ".")
        q, glob = a["q"], a.get("glob") or ""
        matches = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
            for fn in files:
                fp = os.path.join(root, fn)
                rel = os.path.relpath(fp, base)
                if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(fn, glob):
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
    if name == "fs_write":
        _, p = _resolve(ctx, a["path"])
        if os.path.isdir(p):
            raise ToolError("path is a directory")
        quota.ensure_room(fid, len(a["content"].encode("utf-8")), replaces=p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(a["content"])
        _mark_tree_touched(ctx, a["path"])
        return {"ok": True}
    if name == "fs_mkdir":
        _, p = _resolve(ctx, a["path"])
        os.makedirs(p, exist_ok=True)
        return {"ok": True}
    if name == "fs_rename":
        base, src = _resolve(ctx, a["from"])
        _, dst = _resolve(ctx, a["to"])
        if src == base:
            raise ToolError("cannot rename the folder root")
        if not os.path.exists(src):
            raise ToolError("source not found")
        if os.path.exists(dst):
            raise ToolError("destination already exists")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.rename(src, dst)
        except OSError as e:
            raise ToolError(str(e))
        _mark_tree_touched(ctx, a["from"])
        _mark_tree_touched(ctx, a["to"])
        return {"ok": True}
    if name == "fs_delete":
        base, p = _resolve(ctx, a["path"])
        if p == base:
            raise ToolError("cannot delete the folder root")
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
        _mark_tree_touched(ctx, a["path"])
        return {"ok": True}
    raise ToolError(f"unknown tool {name!r}")   # inalcanzable (perm_needed ya filtró)


# ---------------- endpoint MCP (JSON-RPC 2.0, streamable HTTP stateless) ----------------

def _rpc_error(rpc_id, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                         "error": {"code": code, "message": message}}, status_code=status)


@router.get("/mcp/{token}")
def mcp_get(token: str):
    """El transporte streamable-HTTP permite negar el stream SSE con 405 (somos
    stateless request/response); igual validamos el token para no dar oráculo."""
    _auth(token)
    raise HTTPException(status_code=405, detail="SSE stream not supported; POST JSON-RPC")


@router.post("/mcp/{token}")
async def mcp_endpoint(token: str, request: Request):
    ctx = _auth(token)
    try:
        msg = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _rpc_error(None, -32700, "parse error", status=400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "invalid request", status=400)
    method, params, rpc_id = msg.get("method") or "", msg.get("params") or {}, msg.get("id")

    if "id" not in msg or method.startswith("notifications/"):
        return Response(status_code=202)                        # notificación: sin respuesta

    if method == "initialize":
        f = ctx["folder"]
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"{config.NAME} — {f['name']}", "version": config.VERSION},
            "instructions": (f"MCP de la carpeta «{f['name']}» ({ctx['perm']}). Los proyectos "
                             "son diagramas DiagraMind (tree.json); las tools fs_* operan "
                             "sobre los archivos de la carpeta."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": _tool_defs(ctx["perm"])}}
    if method == "tools/call":
        name, args = params.get("name") or "", params.get("arguments") or {}
        try:
            # Cartelito "la IA está trabajando" en la web: se manda ANTES de ejecutar
            # para que el usuario vea la ruedita durante la operación, no después.
            await realtime.manager.notify_folder(ctx["folder"]["id"], {
                "t": "mcp", "folderId": ctx["folder"]["id"], "tool": name,
                "user": ctx["user"]["username"],
            })
            res = _call_tool(ctx, name, args)
        except (ToolError, quota.QuotaExceeded) as e:
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {
                "content": [{"type": "text", "text": str(e)}], "isError": True}}
        except KeyError as e:
            return _rpc_error(rpc_id, -32602, f"missing argument {e}")
        for pid in ctx["push"]:                                  # cambios de tree.json → EN VIVO
            await realtime.push_canonical(pid)
        # Avisos a NIVEL CARPETA: la lista de proyectos cambió (crear/borrar/renombrar).
        # Los rooms son por proyecto, así que uno recién creado no tiene a nadie
        # suscrito: sin esto había que refrescar la página para verlo.
        for ev in ctx.get("repo", []):
            await realtime.manager.notify_folder(ctx["folder"]["id"],
                                                 {"t": "repo", "folderId": ctx["folder"]["id"], **ev})
        text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {
            "content": [{"type": "text", "text": text}]}}
    return _rpc_error(rpc_id, -32601, f"method not found: {method}")

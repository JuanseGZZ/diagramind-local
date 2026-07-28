"""MCP server (stdio) del modo editor EXTERNO (doc 27, fase 4).

Cuando el chat local corre sobre un proyecto `editor` cuyo target vive en un
CONECTOR EXTERNO, los archivos NO están en esta máquina: Claude Code recibe este
server por `--mcp-config` y opera el `/fs` del conector vía tools (mcp__dmfs__*).

- Transporte: JSON-RPC 2.0 por stdio, un mensaje JSON por línea (MCP stdio).
- Credenciales por env: DMFS_URL (base del conector), DMFS_TOKEN (access token
  del usuario — cortito, 15 min; NUNCA el refresh), DMFS_PROJECT (projectId).
- El confinamiento y los permisos los aplica el CONECTOR (ACL read/write/admin);
  acá solo se traduce tool-call → HTTP. stdout es solo JSON-RPC (logs a stderr).

Se lanza re-ejecutando el propio backend con `--mcp-fs` (sirve igual para el
binario onefile, que no puede asumir un python3 del sistema).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = ""
TOKEN = ""
PROJECT = ""
# Cómo se manda el token. "bearer" (default) = conector externo, header Authorization.
# "local" = ESTE backend, que usa X-DiagraMind-Token — lo usan los agentes CLI
# CONFINADOS del orquestador (doc 28 decisión X): sus escrituras van por /fs, o sea
# por editorfs, con el mismo chequeo "path escapes target" que un agente API.
AUTH = "bearer"

_STR = {"type": "string"}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {
        "name": "fs_tree",
        "description": "Lists ONE level of the editor project directory ([{name, dir, size}], dirs first, capped at 500). Empty dir = the root; for subdirs pass their relative path.",
        "inputSchema": _schema({"dir": {"type": "string", "description": "directory relative to the target (default: the root)"}}, []),
    },
    {
        "name": "fs_read",
        "description": "Reads a file (path relative to the target). Returns {content, truncated} (2MB cap) or {binary:true}.",
        "inputSchema": _schema({"path": _STR}, ["path"]),
    },
    {
        "name": "fs_write",
        "description": "Writes a COMPLETE file (creating intermediate dirs). Use it to CREATE files or to rewrite them whole; for a small change in an existing file use fs_edit instead (much cheaper, and you don't risk mangling the rest while copying it).",
        "inputSchema": _schema({"path": _STR, "content": _STR}, ["path", "content"]),
    },
    {
        "name": "fs_edit",
        "description": "Replaces an EXACT piece of text inside a file — your equivalent of the native Edit tool. `old` must appear ONCE: copy it verbatim (indentation included) and add surrounding lines if it is ambiguous, or pass all=true to replace every occurrence. For a one-line change you do not need fs_read: the line that fs_grep returns is already a valid `old`. Returns {ok, replaced}.",
        "inputSchema": _schema({"path": _STR, "old": _STR, "new": _STR,
                                "all": {"type": "boolean", "description": "replace EVERY occurrence (default false)"}},
                               ["path", "old", "new"]),
    },
    {
        "name": "fs_mkdir",
        "description": "Creates a directory (path relative to the target).",
        "inputSchema": _schema({"path": _STR}, ["path"]),
    },
    {
        "name": "fs_rename",
        "description": "Renames or moves a file or directory INSIDE the project (it will not overwrite existing destinations).",
        "inputSchema": _schema({"from": _STR, "to": _STR}, ["from", "to"]),
    },
    {
        "name": "fs_delete",
        "description": "Deletes a file or directory (RECURSIVE — only if you were asked to).",
        "inputSchema": _schema({"path": _STR}, ["path"]),
    },
    {
        "name": "fs_grep",
        "description": "Searches text across the project files. Returns [{path, line, text}] (capped at 200 matches; `text` keeps the indentation, so it can be used as the `old` of fs_edit). Cheaper than reading a whole file when you only need to locate something.",
        "inputSchema": _schema({"q": _STR, "glob": {"type": "string", "description": "filter such as *.py (optional)"}}, ["q"]),
    },
    {
        "name": "fs_exec",
        "description": "Runs a shell command with cwd in the project target (60s timeout). Requires being an ADMIN of the connector (403 → don't insist).",
        "inputSchema": _schema({"cmd": _STR}, ["cmd"]),
    },
    {
        "name": "sv_save",
        "description": "Saves a VERSION (a snapshot of every file) of the project. Use it BEFORE a batch of changes so the user can roll back. It is signed as made by the AI.",
        "inputSchema": _schema({"note": {"type": "string", "description": "a short note (e.g. 'before refactoring X')"}}, []),
    },
    {
        "name": "sv_list",
        "description": "Lists the saved versions of the project ({id, ts, author, note, count}).",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "sv_restore",
        "description": "Takes EVERY file of the project back to a saved version (with an automatic safety snapshot first). Only if the user asks.",
        "inputSchema": _schema({"id": {"type": "string", "description": "id of the version (from sv_list)"}}, ["id"]),
    },
    {
        "name": "gh_push",
        "description": "With GitHub connected on the project: commits all changes + pushes to the repo. The commit is annotated as made by the AI. Use it if the user asks to push/upload.",
        "inputSchema": _schema({"message": {"type": "string", "description": "the commit message"}}, []),
    },
    {
        "name": "gh_pull",
        "description": "With GitHub connected: pulls from the remote (with a safety snapshot first). No `ref` = the latest; with `ref` (a sha from gh_log) = the files of that version. Only if the user asks.",
        "inputSchema": _schema({"ref": {"type": "string", "description": "an earlier sha/ref (optional)"}}, []),
    },
    {
        "name": "gh_log",
        "description": "The latest commits of the project repo ({sha, author, ts, msg}).",
        "inputSchema": _schema({}, []),
    },
]


def _http(method, path, body=None):
    """(json, err). El token va como Bearer; los errores HTTP vuelven legibles."""
    req = urllib.request.Request(BASE + path, method=method)
    if AUTH == "local":
        req.add_header("X-DiagraMind-Token", TOKEN)
    else:
        req.add_header("Authorization", "Bearer " + TOKEN)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=70) as r:
            return json.loads(r.read().decode("utf-8") or "{}"), None
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8"))
        except Exception:
            d = {}
        msg = d.get("detail") or d.get("error") or e.reason
        if e.code == 401:
            msg = f"{msg} (el token del conector expiró: pedile al usuario que mande otro mensaje para renovarlo)"
        return None, f"HTTP {e.code}: {msg}"
    except Exception as e:
        return None, str(e)


def call_tool(name, args):
    """(texto, isError) — traduce cada tool al endpoint /fs correspondiente."""
    q = urllib.parse.quote
    pid = q(PROJECT)
    if name == "fs_tree":
        out, err = _http("GET", f"/fs/tree?projectId={pid}&dir={q(args.get('dir') or '')}")
    elif name == "fs_read":
        out, err = _http("GET", f"/fs/read?projectId={pid}&path={q(args.get('path') or '')}")
    elif name == "fs_write":
        out, err = _http("POST", "/fs/write", {"projectId": PROJECT, "path": args.get("path"), "content": args.get("content") or ""})
    elif name == "fs_edit":
        out, err = _http("POST", "/fs/edit", {"projectId": PROJECT, "path": args.get("path"),
                                             "old": args.get("old") or "", "new": args.get("new") or "",
                                             "all": bool(args.get("all"))})
    elif name == "fs_mkdir":
        out, err = _http("POST", "/fs/mkdir", {"projectId": PROJECT, "path": args.get("path")})
    elif name == "fs_rename":
        out, err = _http("POST", "/fs/rename", {"projectId": PROJECT, "from": args.get("from"), "to": args.get("to")})
    elif name == "fs_delete":
        out, err = _http("POST", "/fs/delete", {"projectId": PROJECT, "path": args.get("path")})
    elif name == "fs_grep":
        out, err = _http("GET", f"/fs/grep?projectId={pid}&q={q(args.get('q') or '')}&glob={q(args.get('glob') or '')}")
    elif name == "fs_exec":
        out, err = _http("POST", "/fs/exec", {"projectId": PROJECT, "cmd": args.get("cmd")})
    elif name == "sv_save":
        out, err = _http("POST", "/sv/save", {"projectId": PROJECT, "note": args.get("note") or "", "author": "IA"})
    elif name == "sv_list":
        out, err = _http("GET", f"/sv/list?projectId={pid}")
    elif name == "sv_restore":
        out, err = _http("POST", "/sv/restore", {"projectId": PROJECT, "id": args.get("id"), "author": "IA"})
    elif name == "gh_push":
        out, err = _http("POST", "/svgit/push", {"projectId": PROJECT, "message": args.get("message") or "", "author": "IA"})
    elif name == "gh_pull":
        out, err = _http("POST", "/svgit/pull", {"projectId": PROJECT, "ref": args.get("ref") or None, "author": "IA"})
    elif name == "gh_log":
        out, err = _http("GET", f"/svgit/log?projectId={pid}")
    else:
        return f"tool desconocida: {name}", True
    if err:
        return err, True
    return json.dumps(out, ensure_ascii=False), False


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    global BASE, TOKEN, PROJECT, AUTH
    BASE = (os.environ.get("DMFS_URL") or "").rstrip("/")
    TOKEN = os.environ.get("DMFS_TOKEN") or ""
    PROJECT = os.environ.get("DMFS_PROJECT") or ""
    AUTH = (os.environ.get("DMFS_AUTH") or "bearer").strip().lower()
    if not BASE or not TOKEN or not PROJECT:
        print("faltan DMFS_URL / DMFS_TOKEN / DMFS_PROJECT", file=sys.stderr)
        sys.exit(2)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}

        if method.startswith("notifications/"):
            continue                                   # las notificaciones no se responden
        if method == "initialize":
            _reply(mid, {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dmfs", "version": "1.0.0"},
            })
        elif method == "ping":
            _reply(mid, {})
        elif method == "tools/list":
            _reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            text, is_err = call_tool(params.get("name") or "", params.get("arguments") or {})
            _reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
        elif mid is not None:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()

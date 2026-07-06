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

_STR = {"type": "string"}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {
        "name": "fs_tree",
        "description": "Lista UN nivel del directorio del proyecto editor ([{name, dir, size}], dirs primero, cap 500). dir vacío = raíz; para subdirs pasá su ruta relativa.",
        "inputSchema": _schema({"dir": {"type": "string", "description": "directorio relativo al target (default: raíz)"}}, []),
    },
    {
        "name": "fs_read",
        "description": "Lee un archivo (ruta relativa al target). Devuelve {content, truncated} (cap 2MB) o {binary:true}.",
        "inputSchema": _schema({"path": _STR}, ["path"]),
    },
    {
        "name": "fs_write",
        "description": "Escribe un archivo COMPLETO (crea dirs intermedios). Para editar: fs_read primero, modificá y mandá TODO el contenido nuevo.",
        "inputSchema": _schema({"path": _STR, "content": _STR}, ["path", "content"]),
    },
    {
        "name": "fs_mkdir",
        "description": "Crea un directorio (ruta relativa al target).",
        "inputSchema": _schema({"path": _STR}, ["path"]),
    },
    {
        "name": "fs_rename",
        "description": "Renombra o mueve un archivo o directorio DENTRO del proyecto (no pisa destinos existentes).",
        "inputSchema": _schema({"from": _STR, "to": _STR}, ["from", "to"]),
    },
    {
        "name": "fs_delete",
        "description": "Borra un archivo o directorio (RECURSIVO — solo si te lo pidieron).",
        "inputSchema": _schema({"path": _STR}, ["path"]),
    },
    {
        "name": "fs_grep",
        "description": "Busca texto en los archivos del proyecto. Devuelve [{path, line, text}] (cap 200 matches).",
        "inputSchema": _schema({"q": _STR, "glob": {"type": "string", "description": "filtro tipo *.py (opcional)"}}, ["q"]),
    },
    {
        "name": "fs_exec",
        "description": "Ejecuta un comando de shell con cwd en el target del proyecto (timeout 60s). Requiere ser ADMIN del conector (403 → no insistas).",
        "inputSchema": _schema({"cmd": _STR}, ["cmd"]),
    },
    {
        "name": "sv_save",
        "description": "Guarda una VERSIÓN (snapshot de todos los archivos) del proyecto. Usala ANTES de una tanda de cambios para que el usuario pueda volver atrás. Queda firmada como hecha por la IA.",
        "inputSchema": _schema({"note": {"type": "string", "description": "nota corta (ej. 'antes de refactorizar X')"}}, []),
    },
    {
        "name": "sv_list",
        "description": "Lista las versiones guardadas del proyecto ({id, ts, author, note, count}).",
        "inputSchema": _schema({}, []),
    },
    {
        "name": "sv_restore",
        "description": "Vuelve TODOS los archivos del proyecto a una versión guardada (con snapshot de seguridad automático previo). Solo si el usuario lo pide.",
        "inputSchema": _schema({"id": {"type": "string", "description": "id de la versión (de sv_list)"}}, ["id"]),
    },
]


def _http(method, path, body=None):
    """(json, err). El token va como Bearer; los errores HTTP vuelven legibles."""
    req = urllib.request.Request(BASE + path, method=method)
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
    global BASE, TOKEN, PROJECT
    BASE = (os.environ.get("DMFS_URL") or "").rstrip("/")
    TOKEN = os.environ.get("DMFS_TOKEN") or ""
    PROJECT = os.environ.get("DMFS_PROJECT") or ""
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

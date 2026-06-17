#!/usr/bin/env python3
"""DiagraMind — backend local (paso 2: bridge a Claude Code).

Servidor mínimo que corre en la máquina de cada persona. La web ("Conectar
local") lo detecta vía /health y lo usa como backend para hablar con **Claude
Code** instalado en esta misma máquina.

Cómo bridgea (sin webhooks: Claude corre acá al lado):
  1. La web manda un árbol con  POST /projects/sync  → se escribe a disco como
     <appdir>/projects/<id>/tree.json  y se instalan las skills del dominio en
     <proyecto>/.claude/skills/.
  2. La web dispara un turno con  POST /chat  → se spawnea  `claude -p ...`  en
     modo headless (stream-json) con cwd = la carpeta del proyecto. Claude edita
     tree.json in-place.
  3. La web escucha  GET /chat/stream?runId=...  (SSE) y va recibiendo los
     eventos (texto del asistente, herramientas, estado).
  4. Al terminar, la web pide  GET /projects/tree?id=...  y reimporta el árbol
     editado en su localStorage.

Diseño:
- Solo stdlib de Python 3 → cero dependencias, multiplataforma.
- Escucha SOLO en 127.0.0.1 (loopback) → no queda expuesto a la red.
- CORS abierto para que la web (file:// u otro origen) pueda consultarlo.

Uso:
    python3 server.py            # puerto por defecto 8765
    python3 server.py --port N   # otro puerto

Detener: Ctrl+C
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
NAME = "diagramind-local"
VERSION = "0.7.0"

# Modos del chat (web) → permission-mode de Claude Code.
PERM_MODE = {
    "auto-edit": "acceptEdits",
    "auto": "acceptEdits",
    "plan": "plan",
    "ask": "default",
}

SYSTEM_PREAMBLE = (
    "Estás trabajando en un WORKSPACE de DiagraMind que contiene VARIOS proyectos. "
    "Tu directorio de trabajo es la carpeta de proyectos: ./index.json lista TODOS "
    "los proyectos [{id, name, type}] y cuál es el foco (focusedId). Cada proyecto "
    "vive en ./<id>/tree.json. Las skills (diagramind-format y la de cada tipo) "
    "están en ./.claude/skills; leelas antes de editar.\n\n"
    "El PROYECTO FOCO es tu objetivo de ESCRITURA por defecto: editá "
    "./<focusedId>/tree.json IN-PLACE, JSON válido, respetando EXACTAMENTE el "
    "esquema de SU tipo (cada proyecto puede ser de un tipo distinto: "
    "cart/freestyle/activities). No cambies el id del árbol. Al terminar, releé el "
    "archivo y verificá que es JSON válido y cumple el esquema (sin campos de más "
    "ni de menos, ids únicos); si algo está mal, corregilo.\n\n"
    "Podés LEER cualquier otro proyecto (./<id>/tree.json, buscalo por nombre en "
    "./index.json) para basarte en él; escribí en otro proyecto SOLO si el usuario "
    "te lo pide explícitamente.\n\n"
    "IMPORTANTE: es UNA conversación continua. Recordás lo que hiciste en turnos "
    "anteriores aunque el usuario cambie el proyecto foco. Si el usuario se refiere "
    "a 'eso' o 'lo que agregaste', mirá el historial de la conversación.\n\n"
    "Respondé en español, breve."
)


# ===================== rutas / disco =====================

def app_dir():
    """Carpeta de datos del backend, por SO."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "DiagraMind")
    elif os.name == "nt":
        base = os.path.join(os.environ.get("LOCALAPPDATA", home), "DiagraMind")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME",
                            os.path.join(home, ".local", "share")), "DiagraMind")
    return base


def config_path():
    return os.path.join(app_dir(), "config.json")


def _load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# Raíz donde el conector guarda TODAS las carpetas. Configurable (config.json);
# default <appdir>/projects. Se cachea para no leer el archivo en cada poll.
_ROOT = None


def projects_dir():
    global _ROOT
    if _ROOT is None:
        _ROOT = _load_config().get("root") or os.path.join(app_dir(), "projects")
    return _ROOT


def set_root(path):
    global _ROOT
    _ROOT = path
    os.makedirs(app_dir(), exist_ok=True)
    cfg = _load_config()
    cfg["root"] = path
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass


def safe_pid(pid):
    # pid viene de la web; sanitizar para que no escape del dir
    return "".join(c for c in str(pid) if c.isalnum() or c in "-_") or "default"


def safe_name(name):
    # nombre de carpeta → dir seguro (legible). Permite espacios.
    s = "".join(c for c in str(name) if c.isalnum() or c in "-_ ").strip()
    return s or "default"


# Estructura en disco (2 niveles): <root>/<carpeta>/<arbol>/tree.json
#   <root>/folders.json              ← índice de carpetas
#   <root>/<carpeta>/index.json      ← índice por carpeta (manifiesto del chat)
#   <root>/<carpeta>/.claude/skills  ← skills (cwd del chat en esa carpeta)
def folder_dir(folder):
    return os.path.join(projects_dir(), safe_name(folder or "Local"))


def tree_dir(folder, pid):
    return os.path.join(folder_dir(folder), safe_pid(pid))


# ===================== Claude Code CLI =====================

def find_claude():
    """Resuelve el binario `claude`. OJO: cuando el backend arranca por doble
    clic / LaunchAgent, ~/.local/bin no está en el PATH, así que probamos rutas
    conocidas además de which()."""
    candidates = [shutil.which("claude")]
    home = os.path.expanduser("~")
    candidates += [
        os.path.join(home, ".local", "bin", "claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def claude_version(claude_bin):
    try:
        out = subprocess.run([claude_bin, "--version"], capture_output=True,
                             text=True, timeout=8)
        return (out.stdout or out.stderr).strip() or None
    except Exception:
        return None


def map_model(m):
    """La web manda ids tipo claude-opus-4-8; el CLI prefiere alias."""
    if not m:
        return "sonnet"
    low = m.lower()
    if "opus" in low:
        return "opus"
    if "haiku" in low:
        return "haiku"
    if "sonnet" in low:
        return "sonnet"
    return m


# ===================== runs (máquina de estados) =====================
# Un "run" = un turno disparado contra Claude. Estados:
#   queued → starting → streaming → done | error | cancelled
# Cada run guarda una lista de eventos con seq incremental para que el SSE pueda
# reconectar sin perder nada.

RUNS = {}
RUNS_LOCK = threading.Lock()
# mapeo  web_session_id → claude_session_id  (para --resume)
SESSION_MAP = {}


def new_run():
    rid = uuid.uuid4().hex
    run = {
        "id": rid,
        "status": "queued",
        "events": [],          # [{seq, kind, ...}]
        "seq": 0,
        "proc": None,
        "claude_session_id": None,
        "error": None,
    }
    with RUNS_LOCK:
        RUNS[rid] = run
    return run


def emit(run, kind, **data):
    with RUNS_LOCK:
        run["seq"] += 1
        ev = {"seq": run["seq"], "kind": kind, **data}
        run["events"].append(ev)


def set_status(run, status, error=None):
    run["status"] = status
    if error:
        run["error"] = error
    emit(run, "status", status=status, error=error,
         sessionId=run.get("claude_session_id"))


def run_claude(run, work_dir, message, mode, model, resume, focus_id, folder):
    claude_bin = find_claude()
    if not claude_bin:
        set_status(run, "error", "No se encontró el binario `claude` en esta máquina.")
        return

    # El cwd es la CARPETA del proyecto. Claude trabaja DENTRO de esa carpeta:
    # ./index.json lista sus proyectos, ./<id>/tree.json es cada árbol.
    focus_note = (
        f"\n\nESTÁS TRABAJANDO EN LA CARPETA «{folder}». Sus proyectos están en "
        f"./index.json y cada uno en ./<id>/tree.json. El proyecto en FOCO es "
        f"id={focus_id} → ./{safe_pid(focus_id)}/tree.json: escribí ahí salvo que "
        f"el usuario te indique otro proyecto de ESTA carpeta."
    )
    perm = PERM_MODE.get(mode, "acceptEdits")
    cmd = [
        claude_bin, "-p", message,
        "--output-format", "stream-json",
        "--verbose",                       # requerido por stream-json en -p
        "--model", map_model(model),
        "--permission-mode", perm,
        "--add-dir", work_dir,             # la carpeta de proyectos (acceso a todos)
        "--append-system-prompt", SYSTEM_PREAMBLE + focus_note,
    ]
    if resume:
        cmd += ["--resume", str(resume)]

    set_status(run, "starting")
    try:
        proc = subprocess.Popen(
            cmd, cwd=work_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            # Claude Code emite UTF-8; sin esto Windows usa cp1252 y rompe con
            # acentos/emojis (UnicodeDecodeError) → se muere el worker.
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        set_status(run, "error", f"No se pudo lanzar claude: {e}")
        return

    run["proc"] = proc
    set_status(run, "streaming")

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        handle_event(run, obj)

    proc.wait()
    stderr = (proc.stderr.read() or "").strip()

    if run["status"] == "cancelled":
        return
    if proc.returncode and proc.returncode != 0 and run["status"] != "done":
        set_status(run, "error", stderr or f"claude salió con código {proc.returncode}")
    elif run["status"] not in ("done", "error"):
        set_status(run, "done")


def handle_event(run, obj):
    """Traduce los eventos JSONL de Claude Code a eventos simples para la web."""
    t = obj.get("type")

    if t == "system" and obj.get("subtype") == "init":
        run["claude_session_id"] = obj.get("session_id")
        return

    if t == "assistant":
        for block in (obj.get("message", {}).get("content") or []):
            if block.get("type") == "text" and block.get("text"):
                emit(run, "assistant", text=block["text"])
            elif block.get("type") == "tool_use":
                emit(run, "tool", name=block.get("name", "tool"))
        return

    if t == "result":
        if obj.get("session_id"):
            run["claude_session_id"] = obj["session_id"]
        if obj.get("is_error"):
            set_status(run, "error", obj.get("result") or "Claude devolvió un error.")
        else:
            txt = obj.get("result")
            # algunos turnos sólo traen el texto en el result final
            if txt and not any(e["kind"] == "assistant" for e in run["events"]):
                emit(run, "assistant", text=txt)
            set_status(run, "done")
        return


# ===================== skills del dominio =====================
# Se instalan en <proyecto>/.claude/skills/<name>/SKILL.md al sincronizar.
# Claude Code las autocarga. Embebidas acá para que el binario --onefile las
# tenga sin archivos sueltos.

def _skill(name, description, body):
    return name, ("---\nname: %s\ndescription: %s\n---\n\n%s\n" %
                  (name, description, body))


SKILLS = dict([
    _skill(
        "diagramind-format",
        "Formato de un proyecto DiagraMind: tree.json, tipos, ids y contadores. "
        "Leer SIEMPRE antes de editar un diagrama.",
        "# Formato DiagraMind\n\n"
        "Un proyecto es un único archivo `tree.json` (el mismo objeto que produce "
        "`tree.toJson()` en la web). El campo raíz `type` define la estructura:\n\n"
        "- `cart` → ver `diagramind-cart`\n"
        "- `freestyle` → ver `diagramind-freestyle`\n"
        "- `activities` → ver `diagramind-activities`\n\n"
        "Común a todos:\n"
        "- `attachments`: mapa `{ \"<aid>\": { \"name\", \"mime\" } }` (adjuntos; "
        "  los bytes van aparte, NO los toques).\n"
        "- Campos `lastIdCharged` / `lastId` / `lastArrowId` / etc. son "
        "  **contadores** del último id usado.\n\n"
        "## Reglas (importantes)\n"
        "1. Editá `tree.json` IN-PLACE y dejalo como **JSON válido** (verificá que "
        "   parsea al terminar).\n"
        "2. **Respetá EXACTAMENTE los nombres de campo** del esquema del tipo. No "
        "   inventes ni renombres campos.\n"
        "3. **Los ids son números enteros.** Al agregar un nodo, usá "
        "   `<contador> + 1`, asignalo como id del nodo nuevo y **actualizá el "
        "   contador** a ese valor.\n"
        "4. No cambies el `type` ni mezcles nodos de otro tipo.\n"
        "5. Conservá los campos existentes de cada nodo (no los borres al editar).",
    ),
    _skill(
        "diagramind-cart",
        "Árbol jerárquico de cartas (tipo `cart`, layouts ltr/organigram).",
        "# Tipo cart (jerárquico)\n\n"
        "Árbol de cartas multinivel. Esquema EXACTO de `tree.json`:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"cart\",\n"
        "  \"lastIdCharged\": 3,\n"
        "  \"attachments\": {},\n"
        "  \"nodoRaiz\": {\n"
        "    \"idCarta\": 0,\n"
        "    \"idPadre\": null,\n"
        "    \"tituloCarta\": \"Raíz\",\n"
        "    \"descripcion\": \"texto del cuerpo\",\n"
        "    \"color\": null,\n"
        "    \"shape\": \"default\",\n"
        "    \"collapsed\": false,\n"
        "    \"listaHijos\": [ /* cartas con la MISMA forma */ ]\n"
        "  }\n"
        "}\n"
        "```\n\n"
        "## Campos por carta\n"
        "- `idCarta` (int, único), `idPadre` (int del padre, o null en la raíz).\n"
        "- `tituloCarta` (str), `descripcion` (str, cuerpo de texto).\n"
        "- `color` (str|null), `shape` (\"default\"), `collapsed` (bool).\n"
        "- `listaHijos` (array de cartas).\n\n"
        "## Editar\n"
        "- **Agregar hijo**: crear una carta con `idCarta = lastIdCharged + 1` y "
        "  `idPadre = idCarta del padre`; pushearla a `listaHijos` del padre; "
        "  subir `lastIdCharged`.\n"
        "- **Mover**: sacar la carta de un `listaHijos` y ponerla en otro; "
        "  actualizar su `idPadre`.\n"
        "- **Borrar**: quitar la carta (con su subárbol) de `listaHijos`.\n"
        "- OJO: es `nodoRaiz`/`listaHijos`/`idCarta`/`tituloCarta` (NO raiz/hijos/id/titulo).",
    ),
    _skill(
        "diagramind-freestyle",
        "Canvas libre (tipo `freestyle`): nodos con x/y, flechas y formas.",
        "# Tipo freestyle (canvas libre)\n\n"
        "Plano, sin layout automático. Esquema EXACTO:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"freestyle\",\n"
        "  \"lastIdCharged\": 2, \"lastArrowId\": 1, \"lastShapeId\": 0,\n"
        "  \"attachments\": {},\n"
        "  \"nodos\":  [{ \"id\":1, \"x\":100, \"y\":80, \"ancho\":160, \"alto\":90,\n"
        "             \"titulo\":\"\", \"contenido\":\"\", \"color\":null,\n"
        "             \"type\":\"basic\", \"data\":{} }],\n"
        "  \"flechas\":[{ \"id\":1, \"fromId\":1, \"toId\":2,\n"
        "             \"fromSide\":\"right\", \"toSide\":\"left\", \"label\":\"\", \"color\":null }],\n"
        "  \"formas\": [{ \"id\":1, \"x\":0,\"y\":0,\"ancho\":120,\"alto\":120,\n"
        "             \"rotation\":0, \"shape\":\"rect\", \"fill\":\"#fff\", \"stroke\":\"#000\",\n"
        "             \"strokeWidth\":2, \"label\":\"\", \"imageSrc\":\"\",\n"
        "             \"imgPosX\":50, \"imgPosY\":50, \"imgZoom\":1 }]\n"
        "}\n"
        "```\n\n"
        "## Editar\n"
        "- **Agregar nodo**: id `lastIdCharged + 1`; x/y/ancho/alto numéricos; subir "
        "  `lastIdCharged`.\n"
        "- **Conectar**: nueva flecha en `flechas` con `fromId`/`toId` de nodos "
        "  existentes; `fromSide`/`toSide` ∈ left/right/top/bottom; subir `lastArrowId`.\n"
        "- **Forma**: nueva en `formas`; subir `lastShapeId`.\n"
        "- No dupliques ids dentro de cada lista.",
    ),
    _skill(
        "diagramind-activities",
        "Diagrama de actividades (tipo `activities`): precedencias / Gantt.",
        "# Tipo activities\n\n"
        "Actividades con precedencias dirigidas. Esquema EXACTO:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"activities\",\n"
        "  \"lastId\": 3, \"seqCounter\": 3, \"timeUnit\": \"dias\",\n"
        "  \"attachments\": {},\n"
        "  \"nodes\": [{ \"id\":1, \"titulo\":\"Tarea\", \"contenido\":\"\",\n"
        "             \"color\":null, \"isStart\":true, \"seq\":1, \"duracion\":2 }],\n"
        "  \"edges\": [{ \"fromId\":1, \"toId\":2, \"color\":null }]\n"
        "}\n"
        "```\n\n"
        "## Campos\n"
        "- nodo: `id` (int), `titulo`, `contenido`, `color`, `isStart` (bool), "
        "  `seq` (orden), `duracion` (en `timeUnit`: horas/dias/semanas).\n"
        "- `edges`: precedencias dirigidas `fromId → toId`.\n\n"
        "## Editar\n"
        "- **Agregar actividad**: id `lastId + 1`, `seq = seqCounter + 1`; subir "
        "  ambos contadores.\n"
        "- **Precedencia**: nuevo `edge` con ids existentes. **No crees ciclos.**\n"
        "- Mantené `timeUnit` coherente.",
    ),
])


def install_skills(project_dir):
    skills_dir = os.path.join(project_dir, ".claude", "skills")
    for name, content in SKILLS.items():
        d = os.path.join(skills_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(content)


# ===================== selector nativo de carpeta =====================
# El navegador no expone la ruta real del sistema; el conector sí. Abrimos un
# diálogo nativo (tkinter askdirectory) en un SUBPROCESO (Tk no es thread-safe y
# el server es multi-thread) y devolvemos la ruta absoluta elegida.
# NOTA: con el binario --onefile (sys.frozen) sys.executable es el binario, no
# python, así que el subproceso -c no aplica; ahí habría que embeber un modo
# "--pick-dir". Por ahora (dev: `python server.py`) funciona.

def pick_directory(title="Elegí una carpeta"):
    script = (
        "import tkinter, tkinter.filedialog as fd\n"
        "r = tkinter.Tk(); r.withdraw()\n"
        "try: r.attributes('-topmost', True)\n"
        "except Exception: pass\n"
        "p = fd.askdirectory(title=%r)\n"
        "print(p or '')\n" % title
    )
    try:
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, timeout=300,
                             encoding="utf-8", errors="replace")
        path = (out.stdout or "").strip()
        return path or None
    except Exception:
        return None


# ===================== state mirror (watcher) =====================
# Vigila los tree.json de todos los proyectos por mtime y empuja los cambios por
# SSE (/state/stream). La web es un espejo en vivo. Cuando la web edita, manda
# write-through (/state/write) y marcamos el mtime como "ya visto" para no
# devolverle el eco. Ver doc 19 (Fase 2).

STATE_LOCK = threading.Lock()
STATE = {}            # "<folder>/<id>" -> {"mtime": float}   (último mtime visto)
STATE_LOG = []        # [{seq, folder, id, treeJson, ts}] (cambios para el SSE)
STATE_SEQ = 0
STATE_LOG_MAX = 500


def _skey(folder, pid):
    return f"{safe_name(folder)}/{safe_pid(pid)}"


def _read_tree_file(folder, pid):
    fp = os.path.join(tree_dir(folder, pid), "tree.json")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _emit_state(folder, pid, content):
    """Agrega un evento de cambio al log (asume STATE_LOCK tomado)."""
    global STATE_SEQ
    STATE_SEQ += 1
    STATE_LOG.append({"seq": STATE_SEQ, "folder": folder, "id": pid,
                      "treeJson": content, "ts": time.time()})
    if len(STATE_LOG) > STATE_LOG_MAX:
        del STATE_LOG[: len(STATE_LOG) - STATE_LOG_MAX]


def iter_disk_trees():
    """Itera (folder, treeId, fullpath_tree.json) sobre la estructura de 2 niveles."""
    root = projects_dir()
    if not os.path.isdir(root):
        return
    for fname in os.listdir(root):
        fdir = os.path.join(root, fname)
        if not os.path.isdir(fdir) or fname == ".claude":
            continue
        # saltar carpetas-legacy "planas" (un tree.json directo no es una carpeta)
        if os.path.exists(os.path.join(fdir, "tree.json")):
            continue
        for tname in os.listdir(fdir):
            fp = os.path.join(fdir, tname, "tree.json")
            if os.path.exists(fp):
                yield fname, tname, fp


def watch_state(interval=0.5):
    """Thread: detecta cambios de mtime en los tree.json (2 niveles) y los emite."""
    while True:
        try:
            for folder, tid, fp in iter_disk_trees():
                try:
                    mtime = os.path.getmtime(fp)
                except OSError:
                    continue
                key = f"{folder}/{tid}"
                with STATE_LOCK:
                    prev = STATE.get(key)
                    if prev is None or prev["mtime"] != mtime:
                        content = _read_tree_file(folder, tid)
                        STATE[key] = {"mtime": mtime}
                        if content is not None:
                            _emit_state(folder, tid, content)
        except Exception:
            pass
        time.sleep(interval)


# ===================== HTTP =====================

class Handler(BaseHTTPRequestHandler):
    server_version = f"{NAME}/{VERSION}"

    # --- helpers ---------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # --- verbos ----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        if path == "/health":
            cb = find_claude()
            self._json(200, {
                "status": "ok", "name": NAME, "version": VERSION,
                "claude": {"available": bool(cb),
                           "version": claude_version(cb) if cb else None},
            })
        elif path == "/projects/tree":
            self._get_tree(q.get("id", [None])[0], q.get("folder", [None])[0])
        elif path == "/chat/stream":
            self._stream(q.get("runId", [None])[0])
        elif path == "/state":
            self._state_full()
        elif path == "/state/stream":
            self._state_stream(q.get("since", [None])[0])
        elif path == "/folders/pick":
            self._folders_pick(q.get("title", ["Elegí una carpeta"])[0])
        elif path == "/folders/read":
            self._folders_read(q.get("path", [None])[0])
        elif path == "/config":
            self._json(200, {"root": projects_dir(), "base": app_dir()})
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/projects/sync":
            self._sync(self._read_json())
        elif path == "/config/root":
            self._config_root(self._read_json())
        elif path == "/projects/manifest":
            self._manifest(self._read_json())
        elif path == "/state/write":
            self._state_write(self._read_json())
        elif path == "/chat":
            self._chat(self._read_json())
        elif path == "/chat/cancel":
            self._cancel(parse_qs(urlparse(self.path).query).get("runId", [None])[0])
        else:
            self._json(404, {"error": "not found", "path": path})

    # --- endpoints -------------------------------------------------------
    def _sync(self, body):
        pid = body.get("id")
        folder = body.get("folder") or "Local"
        tree_json = body.get("treeJson")
        if not pid or tree_json is None:
            self._json(400, {"error": "faltan id o treeJson"})
            return
        tdir = tree_dir(folder, pid)              # <root>/<carpeta>/<arbol>
        os.makedirs(tdir, exist_ok=True)
        text = tree_json if isinstance(tree_json, str) else json.dumps(tree_json, ensure_ascii=False, indent=2)
        fp = os.path.join(tdir, "tree.json")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        install_skills(folder_dir(folder))        # skills a nivel carpeta (cwd del chat)
        try:                                       # anti-eco: mtime ya visto
            with STATE_LOCK:
                STATE[_skey(folder, pid)] = {"mtime": os.path.getmtime(fp)}
        except OSError:
            pass
        self._json(200, {"ok": True, "path": tdir})

    def _manifest(self, body):
        """Escribe el índice por carpeta (<carpeta>/index.json) + el índice de
        carpetas (<root>/folders.json), y poda lo que la web ya no tiene. La web lo
        manda con {folders:[{name, projects:[{id,name,type}]}], focusedFolder, focusedId}."""
        folders = body.get("folders")
        if folders is None:
            self._json(400, {"error": "falta folders"})
            return
        root = projects_dir()
        os.makedirs(root, exist_ok=True)
        focused_folder = body.get("focusedFolder")
        keep_folders = set()
        for f in folders:
            fname = f.get("name") or "Local"
            keep_folders.add(safe_name(fname))
            fdir = folder_dir(fname)
            os.makedirs(fdir, exist_ok=True)
            projects = f.get("projects") or []
            manifest = {
                "folder": fname,
                "projects": projects,
                "focusedId": body.get("focusedId") if fname == focused_folder else None,
                "note": "Cada proyecto de ESTA carpeta está en ./<id>/tree.json.",
            }
            with open(os.path.join(fdir, "index.json"), "w", encoding="utf-8") as fp:
                json.dump(manifest, fp, ensure_ascii=False, indent=2)
            install_skills(fdir)
            # podar árboles que la carpeta ya no tiene
            keep_t = {safe_pid(p.get("id")) for p in projects if p.get("id")}
            for tname in os.listdir(fdir):
                full = os.path.join(fdir, tname)
                if tname in (".claude", "index.json") or not os.path.isdir(full):
                    continue
                if tname not in keep_t and os.path.exists(os.path.join(full, "tree.json")):
                    try:
                        shutil.rmtree(full)
                        with STATE_LOCK:
                            STATE.pop(f"{safe_name(fname)}/{tname}", None)
                    except OSError:
                        pass

        with open(os.path.join(root, "folders.json"), "w", encoding="utf-8") as fp:
            json.dump({"folders": [{"name": f.get("name")} for f in folders],
                       "focusedFolder": focused_folder}, fp, ensure_ascii=False, indent=2)

        # podar carpetas que la web ya no tiene. SOLO tocamos dirs que "parecen
        # nuestros" (tienen index.json o un tree.json directo/legacy) para no borrar
        # nada ajeno si la raíz es un dir compartido.
        pruned = []
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if not os.path.isdir(full) or name == ".claude" or name in keep_folders:
                continue
            looks_ours = (os.path.exists(os.path.join(full, "index.json"))
                          or os.path.exists(os.path.join(full, "tree.json")))
            if looks_ours:
                try:
                    shutil.rmtree(full)
                    pruned.append(name)
                except OSError:
                    pass
        if pruned:
            print(f"[manifest] podadas {len(pruned)} carpetas huérfanas: {', '.join(pruned)}")
        self._json(200, {"ok": True, "pruned": pruned})

    def _get_tree(self, pid, folder):
        if not pid:
            self._json(400, {"error": "falta id"})
            return
        fp = os.path.join(tree_dir(folder or "Local", pid), "tree.json")
        if not os.path.exists(fp):
            self._json(404, {"error": "no hay tree.json para ese proyecto"})
            return
        with open(fp, "r", encoding="utf-8") as f:
            self._json(200, {"treeJson": f.read()})

    # --- config (ruta raíz donde el conector guarda todas las carpetas) ---
    def _config_root(self, body):
        path = (body.get("path") or "").strip()
        if not path:
            self._json(400, {"error": "falta path"})
            return
        set_root(path)
        self._json(200, {"root": projects_dir()})

    # --- carpetas (selector nativo + lectura) ---
    def _folders_pick(self, title):
        """Abre el diálogo nativo y devuelve la ruta elegida (o cancelado)."""
        path = pick_directory(title or "Elegí una carpeta")
        if not path:
            self._json(200, {"cancelled": True})
            return
        self._json(200, {"path": path})

    def _folders_read(self, path):
        """Lee los árboles (<path>/<id>/tree.json) de una carpeta del sistema."""
        if not path or not os.path.isdir(path):
            self._json(400, {"error": "ruta inválida"})
            return
        projects = []
        for name in sorted(os.listdir(path)):
            fp = os.path.join(path, name, "tree.json")
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        projects.append({"id": name, "name": name, "treeJson": f.read()})
                except Exception:
                    pass
        self._json(200, {"path": path, "projects": projects})

    # --- state mirror ---
    def _state_full(self):
        """Snapshot completo: carpetas → proyectos en disco + seq actual."""
        by_folder = {}
        for folder, tid, fp in iter_disk_trees():
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            by_folder.setdefault(folder, []).append({"id": tid, "treeJson": content})
        folders = [{"name": k, "projects": v} for k, v in sorted(by_folder.items())]
        with STATE_LOCK:
            seq = STATE_SEQ
        self._json(200, {"folders": folders, "seq": seq})

    def _state_stream(self, since):
        """SSE de larga duración: empuja los cambios de tree.json con seq > since."""
        try:
            since = int(since) if since is not None else 0
        except (TypeError, ValueError):
            since = 0
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()

        last_beat = time.time()
        try:
            while True:
                with STATE_LOCK:
                    pending = [e for e in STATE_LOG if e["seq"] > since]
                for ev in pending:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    since = ev["seq"]
                if time.time() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _state_write(self, body):
        """Write-through de la web: escribe tree.json y marca el mtime como ya
        visto para que el watcher NO devuelva el eco al que lo escribió."""
        pid = body.get("id")
        folder = body.get("folder") or "Local"
        tree_json = body.get("treeJson")
        if not pid or tree_json is None:
            self._json(400, {"error": "faltan id o treeJson"})
            return
        tdir = tree_dir(folder, pid)
        os.makedirs(tdir, exist_ok=True)
        text = tree_json if isinstance(tree_json, str) else json.dumps(tree_json, ensure_ascii=False, indent=2)
        fp = os.path.join(tdir, "tree.json")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            with STATE_LOCK:
                STATE[_skey(folder, pid)] = {"mtime": os.path.getmtime(fp)}
        except OSError:
            pass
        self._json(200, {"ok": True})

    def _chat(self, body):
        pid = body.get("projectId")
        folder = body.get("folder") or "Local"
        message = body.get("message")
        if not pid or not message:
            self._json(400, {"error": "faltan projectId o message"})
            return
        if not os.path.exists(os.path.join(tree_dir(folder, pid), "tree.json")):
            self._json(409, {"error": "el proyecto no está sincronizado (falta tree.json)"})
            return

        # cwd = la CARPETA del proyecto (estable por chat dentro de la carpeta). Así
        # --resume sobrevive el cambio de foco entre proyectos de la misma carpeta y
        # Claude "sabe" en qué carpeta labura (su cwd + el focus_note).
        work_dir = folder_dir(folder)
        web_session = body.get("sessionId")
        # la sesión de Claude está atada al cwd (la carpeta), así que el fallback
        # se cachea por (sesión web, carpeta).
        skey = f"{web_session}::{safe_name(folder)}" if web_session else None
        resume = body.get("resume") or (SESSION_MAP.get(skey) if skey else None)
        mode = body.get("mode") or "auto-edit"
        model = body.get("model")

        run = new_run()

        def worker():
            run_claude(run, work_dir, message, mode, model, resume, pid, folder)
            if run.get("claude_session_id") and skey:
                SESSION_MAP[skey] = run["claude_session_id"]

        threading.Thread(target=worker, daemon=True).start()
        self._json(200, {"runId": run["id"]})

    def _cancel(self, rid):
        with RUNS_LOCK:
            run = RUNS.get(rid)
        if not run:
            self._json(404, {"error": "run no encontrado"})
            return
        run["status"] = "cancelled"
        proc = run.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        emit(run, "status", status="cancelled")
        self._json(200, {"ok": True})

    def _stream(self, rid):
        with RUNS_LOCK:
            run = RUNS.get(rid)
        if not run:
            self._json(404, {"error": "run no encontrado"})
            return

        # cerramos el socket al terminar el run (no keep-alive): así clientes
        # como curl no quedan colgados y el thread se libera. El navegador igual
        # cierra el EventSource al recibir un estado terminal.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()

        sent = 0
        terminal = ("done", "error", "cancelled")
        last_beat = time.time()
        try:
            while True:
                with RUNS_LOCK:
                    events = run["events"][sent:]
                    sent += len(events)
                    status = run["status"]
                for ev in events:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if status in terminal and sent >= len(run["events"]):
                    break
                # heartbeat para que el socket no muera
                if time.time() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # log un poco más prolijo
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="DiagraMind backend local")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"puerto (default {DEFAULT_PORT})")
    args = parser.parse_args()

    # En Windows, si stdout es cp1252 (consola/redirección) los print con → o ·
    # crashean. Forzamos UTF-8 para la salida del propio server.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    os.makedirs(projects_dir(), exist_ok=True)
    cb = find_claude()

    # watcher del state mirror (poll de mtime → SSE)
    threading.Thread(target=watch_state, daemon=True).start()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"DiagraMind local backend v{VERSION} → http://{HOST}:{args.port}")
    print(f"Claude Code: {'OK · ' + (claude_version(cb) or '') if cb else 'NO ENCONTRADO'}")
    print(f"Proyectos en: {projects_dir()}")
    print("Endpoints: GET /health · POST /projects/sync · POST /chat · "
          "GET /chat/stream · GET /projects/tree · POST /chat/cancel · "
          "GET /state · GET /state/stream · POST /state/write")
    print("Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

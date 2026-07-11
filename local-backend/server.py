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
import base64
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# módulos desacoplados (ver claude.py / codex.py / gemini.py / cli_base.py / etc.)
from util import safe_name, safe_file_name
from runs import RUNS, RUNS_LOCK, SESSION_MAP, new_run, emit
import editorfs
import orchestrator
import sourcever
import svgit
from skills import install_skills
from claude import find_claude, claude_version
from clis import CLIS, run_cli

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
NAME = "diagramind-local"
VERSION = "0.21.0"   # IA Orchestrator fase 3: paralelismo (fork/join + locks, doc 28)

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


# ===================== token de acceso (auth local) =====================
# Aunque el server escucha SOLO en 127.0.0.1, el CORS es abierto: cualquier web
# que abras en el navegador podría pegarle al backend. Un token random corta eso.
# Se guarda en <app_dir>/token.txt: si no existe se genera al azar; si existe se
# usa. La web lo manda en cada request (?token= o header X-DiagraMind-Token).
_TOKEN = None


def token_path():
    return os.path.join(app_dir(), "token.txt")


def get_token():
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = _load_or_create_token()
    return _TOKEN


def _load_or_create_token():
    p = token_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except Exception:
        pass
    t = secrets.token_urlsafe(24)
    try:
        os.makedirs(app_dir(), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(t + "\n")
    except OSError:
        pass
    return t


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


# Estructura en disco (2 niveles): <root>/<carpeta>/<arbol>/tree.json
#   <root>/folders.json              ← índice de carpetas
#   <root>/<carpeta>/index.json      ← índice por carpeta (manifiesto del chat)
#   <root>/<carpeta>/.claude/skills  ← skills (cwd del chat en esa carpeta)
def folder_dir(folder):
    return os.path.join(projects_dir(), safe_name(folder or "Local"))


# El directorio de cada proyecto usa su NOMBRE (real), no el id. El id (interno de
# la web) se resuelve para el mirror leyendo el index.json de la carpeta.
def tree_dir(folder, name):
    return os.path.join(folder_dir(folder), safe_name(name))


# ===================== adjuntos del chat (tempFiles) =====================
# Los archivos que el usuario arrastra/sube en el chat se guardan en una carpeta
# temporal DENTRO del proyecto (<root>/<carpeta>/<proyecto>/tempFiles/), así Claude
# los lee con una ruta relativa a su cwd (la carpeta). Viven como mucho TEMP_TTL_DAYS
# días: cada subida (y el arranque) barre los vencidos. Ver doc 18 / 19 (uploads).
TEMP_DIRNAME = "tempFiles"
TEMP_TTL_DAYS = 10
TEMP_TTL_SECS = TEMP_TTL_DAYS * 24 * 3600


def temp_dir(folder, name):
    return os.path.join(tree_dir(folder, name), TEMP_DIRNAME)


def sweep_temp_files():
    """Borra adjuntos con más de TEMP_TTL_DAYS días en TODOS los tempFiles/."""
    now = time.time()
    root = projects_dir()
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) != TEMP_DIRNAME:
            continue
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                if now - os.path.getmtime(fp) > TEMP_TTL_SECS:
                    os.remove(fp)
            except OSError:
                pass


def read_folder_index(folder):
    fp = os.path.join(folder_dir(folder), "index.json")
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# Conexión GitHub POR proyecto editor (doc 27, fase 4). El token nunca entra al
# repo: vive en <app_dir>/editor_github.json (0600) y se inyecta en la URL.
def _gh_path():
    return os.path.join(app_dir(), "editor_github.json")


def gh_conn_read():
    try:
        with open(_gh_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def gh_conn_write(data):
    with open(_gh_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(_gh_path(), 0o600)
    except OSError:
        pass


def gh_conn_of(pid):
    return gh_conn_read().get(pid or "")


# Resuelve un proyecto por id escaneando los index.json de las carpetas del mirror.
def project_entry(pid):
    """(carpeta, meta {id,name,type}) del proyecto `pid`, o (None, None)."""
    try:
        folders_list = os.listdir(projects_dir())
    except OSError:
        folders_list = []
    for folder in folders_list:
        if not os.path.isdir(os.path.join(projects_dir(), folder)):
            continue
        for p in read_folder_index(folder).get("projects", []):
            if p.get("id") == pid:
                return folder, p
    return None, None


# Contexto de rutas que consume el motor del orquestador (orchestrator.py).
def orch_ctx(pid):
    folder, meta = project_entry(pid or "")
    if not meta:
        return None
    def tree_path_of(rpid):
        f2, m2 = project_entry(rpid)
        return os.path.join(tree_dir(f2, m2.get("name") or rpid), "tree.json") if m2 else None
    def sv_dir_of(rpid):
        err, svd, _t = sv_context(rpid)
        return None if err else svd
    def project_meta(rpid):
        _f, m2 = project_entry(rpid)
        return m2
    return {
        "pid": pid, "app_dir": app_dir(),
        "graph_path": os.path.join(tree_dir(folder, meta.get("name") or pid), "tree.json"),
        "work_dir": folder_dir(folder),      # cwd de los agentes CLI (fase 4)
        "tree_path_of": tree_path_of, "sv_dir_of": sv_dir_of, "project_meta": project_meta,
        # el watcher del mirror ya detecta los tree.json tocados (mtime → SSE a la web)
        "notify_edit": lambda rpid: None,
    }


# Source Versions del modo editor (doc 27): el sv_dir vive DENTRO del directorio
# del proyecto en la carpeta de proyectos (viaja/cae con el proyecto). El pid se
# resuelve a (carpeta, nombre) escaneando los index.json de las carpetas.
def sv_context(pid):
    """(err, sv_dir, target) para las operaciones /sv del proyecto editor `pid`."""
    target = editorfs.get_target(app_dir(), pid)
    if not target:
        return (400, {"error": "editor target not set"}), None, None
    try:
        folders = os.listdir(projects_dir())
    except OSError:
        folders = []
    for folder in folders:
        if not os.path.isdir(os.path.join(projects_dir(), folder)):
            continue
        for p in read_folder_index(folder).get("projects", []):
            if p.get("id") == pid:
                svd = os.path.join(tree_dir(folder, p.get("name") or pid), "source-versions")
                return None, svd, target
    return (409, {"error": "el proyecto no está sincronizado (falta en el index de su carpeta)"}), None, None


def resolve_tree_id(folder, dirname):
    """Mapea el nombre de carpeta del proyecto → id de la web (vía index.json)."""
    for p in read_folder_index(folder).get("projects", []):
        if safe_name(p.get("name", "")) == dirname:
            return p.get("id") or dirname
    return dirname


# ===================== Claude Code CLI =====================

# ===================== selector nativo de carpeta =====================
# El navegador no expone la ruta real del sistema; el conector sí. Abrimos un
# diálogo nativo (tkinter askdirectory) en un SUBPROCESO (Tk no es thread-safe y
# el server es multi-thread) y devolvemos la ruta absoluta elegida.
# NOTA: con el binario --onefile (sys.frozen) sys.executable es el binario, no
# python, así que el subproceso -c no aplica; ahí habría que embeber un modo
# "--pick-dir". Por ahora (dev: `python server.py`) funciona.

def reveal_in_explorer(path):
    """Abre el explorador del SO en `path`."""
    try:
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)                       # Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])         # macOS
        else:
            subprocess.Popen(["xdg-open", path])     # Linux
        return True
    except Exception:
        return False


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


def _skey(folder, name):
    return f"{safe_name(folder)}/{safe_name(name)}"


def _read_tree_file(folder, name):
    fp = os.path.join(tree_dir(folder, name), "tree.json")
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
            for folder, tname, fp in iter_disk_trees():
                try:
                    mtime = os.path.getmtime(fp)
                except OSError:
                    continue
                key = f"{folder}/{tname}"          # carpeta del proyecto (nombre)
                with STATE_LOCK:
                    prev = STATE.get(key)
                    if prev is None or prev["mtime"] != mtime:
                        content = _read_tree_file(folder, tname)
                        STATE[key] = {"mtime": mtime}
                        if content is not None:
                            # el evento usa el ID de la web (resuelto vía index.json)
                            _emit_state(folder, resolve_tree_id(folder, tname), content)
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DiagraMind-Token")

    # --- auth: token en ?token= o header X-DiagraMind-Token ---
    def _req_token(self):
        t = parse_qs(urlparse(self.path).query).get("token", [None])[0]
        if not t:
            t = self.headers.get("X-DiagraMind-Token")
        return t or ""

    def _auth_ok(self):
        return hmac.compare_digest(self._req_token(), get_token())

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sv(self, fn):
        """Corre una operación de sourcever y traduce SvError → HTTP."""
        try:
            self._json(200, fn())
        except sourcever.SvError as e:
            self._json(e.code, {"error": e.msg})

    def _gh(self, fn):
        """Corre una operación de svgit y traduce GitError/SvError → HTTP."""
        try:
            self._json(200, fn())
        except svgit.GitError as e:
            self._json(e.code, {"error": e.msg})
        except sourcever.SvError as e:
            self._json(e.code, {"error": e.msg})

    def _orch(self, pid, fn):
        """Resuelve el ctx del orquestador y corre `fn(ctx)` traduciendo OrchError."""
        ctx = orch_ctx(pid)
        if not ctx:
            self._json(409, {"error": "el orquestador no está sincronizado (falta en el mirror)"})
            return
        try:
            self._json(200, fn(ctx))
        except orchestrator.OrchError as e:
            self._json(e.code, {"error": e.msg})
        except sourcever.SvError as e:
            self._json(e.code, {"error": e.msg})

    def _orch_stream(self, pid, since):
        """SSE de eventos del run del orquestador (para pintar el canvas en vivo)."""
        ctx = orch_ctx(pid)
        if not ctx:
            self._json(409, {"error": "el orquestador no está sincronizado"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        sent = since
        last_beat = time.time()
        try:
            while True:
                evs, sent, status = orchestrator.events_since(ctx, sent)
                for ev in evs:
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if status in ("done", "error", "killed", "none") and not evs:
                    self.wfile.write(b"data: {\"kind\": \"end\"}\n\n")
                    self.wfile.flush()
                    break
                if time.time() - last_beat > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.25)
        except (ConnectionError, OSError):
            pass

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
            # /health es PÚBLICO (la web lo usa para detectar el server). No
            # devuelve el token; solo dice que se requiere y si el que mandaron
            # (si mandaron alguno) es válido.
            clis = []
            for a in CLIS.values():
                b = a.find()
                clis.append({"key": a.key, "label": a.label, "available": bool(b),
                             "version": a.version(b) if b else None, "resume": a.supports_resume})
            cb = find_claude()
            self._json(200, {
                "status": "ok", "name": NAME, "version": VERSION,
                "auth": True, "authOk": self._auth_ok(),
                "clis": clis,
                # compat: campo claude suelto (clientes viejos)
                "claude": {"available": bool(cb),
                           "version": claude_version(cb) if cb else None},
            })
            return

        if not self._auth_ok():
            self._json(401, {"error": "unauthorized"})
            return

        if path == "/projects/tree":
            self._get_tree(q.get("name", q.get("id", [None]))[0], q.get("folder", [None])[0])
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
        # --- modo editor (doc 27; contrato unificado con el conector externo) ---
        elif path == "/editor/target":
            self._json(200, {"path": editorfs.get_target(app_dir(), q.get("projectId", [None])[0])})
        elif path == "/fs/tree":
            self._json(*editorfs.fs_tree(app_dir(), q.get("projectId", [None])[0], q.get("dir", [""])[0]))
        elif path == "/fs/read":
            self._json(*editorfs.fs_read(app_dir(), q.get("projectId", [None])[0], q.get("path", [None])[0]))
        elif path == "/fs/grep":
            self._json(*editorfs.fs_grep(app_dir(), q.get("projectId", [None])[0],
                                         q.get("q", [None])[0], q.get("glob", [""])[0]))
        # --- source versions del modo editor (doc 27, fase 4) ---
        elif path == "/sv/list":
            err, svd, _t = sv_context(q.get("projectId", [None])[0])
            if err:
                self._json(*err)
            else:
                self._json(200, {"versions": sourcever.sv_list(svd)})
        elif path == "/sv/status":
            err, svd, target = sv_context(q.get("projectId", [None])[0])
            if err:
                self._json(*err)
            else:
                self._sv(lambda: sourcever.sv_status(svd, target))
        elif path == "/sv/diff":
            err, svd, target = sv_context(q.get("projectId", [None])[0])
            if err:
                self._json(*err)
            else:
                self._sv(lambda: sourcever.sv_diff(svd, target, q.get("id", [None])[0],
                                                   q.get("path", [None])[0]))
        # --- GitHub por proyecto editor (doc 27, fase 4) ---
        elif path == "/svgit/status":
            pid = q.get("projectId", [None])[0]
            target = editorfs.get_target(app_dir(), pid)
            if not target:
                self._json(400, {"error": "editor target not set"})
            else:
                self._gh(lambda: svgit.gh_status(gh_conn_of(pid), target))
        elif path == "/svgit/log":
            pid = q.get("projectId", [None])[0]
            target = editorfs.get_target(app_dir(), pid)
            if not target:
                self._json(400, {"error": "editor target not set"})
            else:
                self._gh(lambda: svgit.gh_log(gh_conn_of(pid), target,
                                              int(q.get("n", ["20"])[0])))
        # --- IA Orchestrator (doc 28, fase 2) ---
        elif path == "/orch/state":
            self._orch(q.get("projectId", [None])[0], lambda ctx: orchestrator.get_state(ctx))
        elif path == "/orch/stream":
            self._orch_stream(q.get("projectId", [None])[0], int(q.get("since", ["0"])[0]))
        elif path == "/orch/chatlog":
            self._orch(q.get("projectId", [None])[0],
                       lambda ctx: orchestrator.chat_read(ctx, int(q.get("nodeId", ["0"])[0])))
        elif path == "/orch/mem":
            def _mem(ctx):
                nid = int(q.get("nodeId", ["0"])[0])
                return {"entries": orchestrator.mem_read(ctx, nid),
                        "chars": orchestrator.mem_chars(ctx, nid)}
            self._orch(q.get("projectId", [None])[0], _mem)
        elif path == "/orch/keys":
            self._orch(q.get("projectId", [None])[0], lambda ctx: orchestrator.keys_status(ctx))
        elif path == "/orch/runs":
            self._orch(q.get("projectId", [None])[0], lambda ctx: orchestrator.runs_list(ctx))
        elif path == "/orch/rundetail":
            self._orch(q.get("projectId", [None])[0],
                       lambda ctx: orchestrator.run_detail(ctx, q.get("runId", [None])[0]))
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth_ok():
            self._json(401, {"error": "unauthorized"})
            return
        if path == "/projects/sync":
            self._sync(self._read_json())
        elif path == "/files/upload":
            self._files_upload(self._read_json())
        elif path == "/folders/reveal":
            self._folders_reveal(self._read_json())
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
        elif path == "/fetch":
            self._proxy_fetch(self._read_json())
        # --- modo editor (doc 27) ---
        elif path == "/editor/target":
            b = self._read_json()
            self._json(*editorfs.set_target(app_dir(), b.get("projectId"), b.get("path")))
        elif path == "/fs/write":
            b = self._read_json()
            self._json(*editorfs.fs_write(app_dir(), b.get("projectId"), b.get("path"), b.get("content")))
        elif path == "/fs/mkdir":
            b = self._read_json()
            self._json(*editorfs.fs_mkdir(app_dir(), b.get("projectId"), b.get("path")))
        elif path == "/fs/rename":
            b = self._read_json()
            self._json(*editorfs.fs_rename(app_dir(), b.get("projectId"), b.get("from"), b.get("to")))
        elif path == "/fs/delete":
            b = self._read_json()
            self._json(*editorfs.fs_delete(app_dir(), b.get("projectId"), b.get("path")))
        elif path == "/fs/exec":
            b = self._read_json()
            self._json(*editorfs.fs_exec(app_dir(), b.get("projectId"), b.get("cmd")))
        # --- source versions del modo editor (doc 27, fase 4) ---
        elif path == "/sv/save":
            b = self._read_json()
            err, svd, target = sv_context(b.get("projectId"))
            if err:
                self._json(*err)
            else:
                self._sv(lambda: sourcever.sv_save(svd, target, b.get("author"), b.get("note")))
        elif path == "/sv/restore":
            b = self._read_json()
            err, svd, target = sv_context(b.get("projectId"))
            if err:
                self._json(*err)
            else:
                self._sv(lambda: sourcever.sv_restore(svd, target, b.get("id"), b.get("author")))
        # --- GitHub por proyecto editor (doc 27, fase 4) ---
        elif path == "/svgit/connect":
            b = self._read_json()
            pid = b.get("projectId")
            if not pid or not b.get("remoteUrl"):
                self._json(400, {"error": "faltan projectId o remoteUrl"})
                return
            if not editorfs.get_target(app_dir(), pid):
                self._json(400, {"error": "editor target not set"})
                return
            data = gh_conn_read()
            data[pid] = {"remoteUrl": b["remoteUrl"].strip(),
                         "token": (b.get("token") or "").strip(),
                         "branch": (b.get("branch") or "main").strip() or "main"}
            gh_conn_write(data)
            self._json(200, svgit.gh_status(data[pid], editorfs.get_target(app_dir(), pid)))
        elif path == "/svgit/disconnect":
            b = self._read_json()
            data = gh_conn_read()
            data.pop(b.get("projectId") or "", None)
            gh_conn_write(data)
            self._json(200, {"ok": True})
        elif path == "/svgit/push":
            b = self._read_json()
            pid = b.get("projectId")
            target = editorfs.get_target(app_dir(), pid)
            if not target:
                self._json(400, {"error": "editor target not set"})
            else:
                by_ai = (b.get("author") or "") == "IA"
                self._gh(lambda: svgit.gh_push(gh_conn_of(pid), target, b.get("message"),
                                               "IA (DiagraMinder)" if by_ai else "usuario", by_ai))
        elif path == "/svgit/pull":
            b = self._read_json()
            err, svd, target = sv_context(b.get("projectId"))
            if err:
                self._json(*err)
            else:
                self._gh(lambda: svgit.gh_pull(gh_conn_of(b.get("projectId")), target,
                                               b.get("ref"), svd, b.get("author") or "usuario"))
        # --- IA Orchestrator (doc 28, fase 2) ---
        elif path == "/orch/run":
            b = self._read_json()
            def _run(ctx):
                graph = orchestrator.load_graph(ctx)
                task = graph["nodos"].get(int(b.get("taskNodeId") or 0))
                if not task or task.get("type") != "agTask":
                    raise orchestrator.OrchError(400, "taskNodeId no es un nodo tarea")
                edge = next((f for f in graph["flechas"]
                             if f.get("kind") == "task" and int(f.get("fromId", -1)) == int(task["id"])), None)
                if not edge:
                    raise orchestrator.OrchError(400, "la tarea no está conectada a un agente (flecha task)")
                enunciado = (task.get("data") or {}).get("enunciado") or ""
                texto = f"TAREA «{task.get('titulo') or ''}»: {enunciado}".strip()
                run = orchestrator.start_run(ctx, "task", int(edge["toId"]), texto,
                                             b.get("apiKeys") or {}, b.get("maxTurns"))
                return {"runId": run["id"]}
            self._orch(b.get("projectId"), _run)
        elif path == "/orch/chat":
            b = self._read_json()
            self._orch(b.get("projectId"),
                       lambda ctx: orchestrator.chat_message(ctx, int(b.get("nodeId") or 0),
                                                             b.get("message") or "",
                                                             b.get("apiKeys") or {}, b.get("maxTurns")))
        elif path == "/orch/answer":
            b = self._read_json()
            self._orch(b.get("projectId"),
                       lambda ctx: orchestrator.answer(ctx, b.get("text") or "", b.get("nodeId")))
        elif path == "/orch/pause":
            b = self._read_json()
            self._orch(b.get("projectId"), lambda ctx: orchestrator.pause(ctx))
        elif path == "/orch/resume":
            b = self._read_json()
            self._orch(b.get("projectId"), lambda ctx: orchestrator.resume(ctx))
        elif path == "/orch/kill":
            b = self._read_json()
            self._orch(b.get("projectId"), lambda ctx: orchestrator.kill(ctx))
        elif path == "/orch/keys":
            b = self._read_json()
            self._orch(b.get("projectId"),
                       lambda ctx: orchestrator.keys_write(ctx, b.get("keys") or {}))
        elif path == "/orch/memclear":
            b = self._read_json()
            def _mc(ctx):
                orchestrator.mem_clear(ctx, int(b.get("nodeId") or 0))
                return {"ok": True}
            self._orch(b.get("projectId"), _mc)
        elif path == "/orch/chatclear":
            b = self._read_json()
            self._orch(b.get("projectId"),
                       lambda ctx: orchestrator.chat_clear(ctx, int(b.get("nodeId") or 0)))
        else:
            self._json(404, {"error": "not found", "path": path})

    # --- endpoints -------------------------------------------------------
    def _sync(self, body):
        folder = body.get("folder") or "Local"
        name = body.get("name") or body.get("id")   # carpeta del proyecto = su NOMBRE
        tree_json = body.get("treeJson")
        if not name or tree_json is None:
            self._json(400, {"error": "faltan name/id o treeJson"})
            return
        tdir = tree_dir(folder, name)             # <root>/<carpeta>/<NombreProyecto>
        os.makedirs(tdir, exist_ok=True)
        text = tree_json if isinstance(tree_json, str) else json.dumps(tree_json, ensure_ascii=False, indent=2)
        fp = os.path.join(tdir, "tree.json")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        install_skills(folder_dir(folder))        # skills a nivel carpeta (cwd del chat)
        try:                                       # anti-eco: mtime ya visto
            with STATE_LOCK:
                STATE[_skey(folder, name)] = {"mtime": os.path.getmtime(fp)}
        except OSError:
            pass
        self._json(200, {"ok": True, "path": tdir})

    def _files_upload(self, body):
        """Recibe adjuntos del chat (base64) y los guarda en el tempFiles/ del
        proyecto. Devuelve las rutas RELATIVAS a la carpeta (cwd del chat) para que
        la web se las pase a Claude. Los archivos viven como mucho TEMP_TTL_DAYS días."""
        folder = body.get("folder") or "Local"
        name = body.get("name") or body.get("id")
        files = body.get("files")
        if not name or not isinstance(files, list):
            self._json(400, {"error": "faltan name o files"})
            return
        tmp = temp_dir(folder, name)
        os.makedirs(tmp, exist_ok=True)
        saved = []
        for f in files:
            try:
                data = base64.b64decode(f.get("dataB64") or "")
            except Exception:
                continue
            fname = safe_file_name(f.get("name") or "archivo")
            dest = os.path.join(tmp, fname)
            if os.path.exists(dest):                  # no pisar uno previo del mismo nombre
                stem, ext = os.path.splitext(fname)
                fname = f"{stem}-{uuid.uuid4().hex[:6]}{ext}"
                dest = os.path.join(tmp, fname)
            with open(dest, "wb") as out:
                out.write(data)
            rel = f"{safe_name(name)}/{TEMP_DIRNAME}/{fname}"   # relativo al cwd (la carpeta)
            saved.append({"name": f.get("name"), "path": rel, "bytes": len(data)})
        sweep_temp_files()                            # de paso, barrer los vencidos
        self._json(200, {"ok": True, "files": saved, "ttlDays": TEMP_TTL_DAYS})

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
                "note": "Cada proyecto de ESTA carpeta está en ./<name>/tree.json (por su NOMBRE).",
            }
            with open(os.path.join(fdir, "index.json"), "w", encoding="utf-8") as fp:
                json.dump(manifest, fp, ensure_ascii=False, indent=2)
            install_skills(fdir)
            # podar árboles que la carpeta ya no tiene (los dirs son NOMBRES)
            keep_t = {safe_name(p.get("name")) for p in projects if p.get("name")}
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

    def _get_tree(self, name, folder):
        if not name:
            self._json(400, {"error": "falta name"})
            return
        fp = os.path.join(tree_dir(folder or "Local", name), "tree.json")
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

    # --- carpetas (abrir en el explorador + selector + lectura) ---
    def _folders_reveal(self, body):
        """Abre el explorador del SO en la carpeta (o en la raíz si no se da)."""
        folder = body.get("folder")
        path = folder_dir(folder) if folder else projects_dir()
        ok = reveal_in_explorer(path)
        self._json(200, {"ok": ok, "path": path})

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
        for folder, tname, fp in iter_disk_trees():
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            rid = resolve_tree_id(folder, tname)   # id de la web (vía index.json)
            by_folder.setdefault(folder, []).append({"id": rid, "treeJson": content})
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
        except (ConnectionError, OSError):
            # el cliente cerró la conexión SSE (reconexión normal del mirror) → fin tranquilo
            pass

    def _state_write(self, body):
        """Write-through de la web: escribe tree.json y marca el mtime como ya
        visto para que el watcher NO devuelva el eco al que lo escribió."""
        folder = body.get("folder") or "Local"
        name = body.get("name") or body.get("id")
        tree_json = body.get("treeJson")
        if not name or tree_json is None:
            self._json(400, {"error": "faltan name/id o treeJson"})
            return
        tdir = tree_dir(folder, name)
        os.makedirs(tdir, exist_ok=True)
        text = tree_json if isinstance(tree_json, str) else json.dumps(tree_json, ensure_ascii=False, indent=2)
        fp = os.path.join(tdir, "tree.json")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            with STATE_LOCK:
                STATE[_skey(folder, name)] = {"mtime": os.path.getmtime(fp)}
        except OSError:
            pass
        self._json(200, {"ok": True})

    def _chat(self, body):
        pid = body.get("projectId")
        folder = body.get("folder") or "Local"
        name = body.get("name") or pid               # carpeta del proyecto = su nombre
        message = body.get("message")
        if not pid or not message:
            self._json(400, {"error": "faltan projectId o message"})
            return
        if not os.path.exists(os.path.join(tree_dir(folder, name), "tree.json")):
            self._json(409, {"error": "el proyecto no está sincronizado (falta tree.json)"})
            return

        # proyectos tipo `editor` (doc 27): con target LOCAL el CLI recibe la carpeta
        # (--add-dir); si el target vive en un conector EXTERNO, la web manda
        # `editorRelay` {url, token} y Claude opera el /fs por MCP (fase 4).
        editor_target = None
        editor_relay = None
        try:
            with open(os.path.join(tree_dir(folder, name), "tree.json"), encoding="utf-8") as f:
                is_editor = json.load(f).get("type") == "editor"
        except Exception:
            is_editor = False
        if is_editor:
            editor_target = editorfs.get_target(app_dir(), pid)
            relay = body.get("editorRelay")
            if not editor_target and isinstance(relay, dict) and relay.get("url") and relay.get("token"):
                editor_relay = {"url": relay["url"], "token": relay["token"], "projectId": pid}
            if not editor_target and not editor_relay:
                self._json(409, {"error": "el proyecto editor no tiene carpeta asignada (elegí la ubicación en la web)"})
                return

        # cwd = la CARPETA del proyecto (estable por chat dentro de la carpeta). Así
        # --resume sobrevive el cambio de foco entre proyectos de la misma carpeta y
        # Claude "sabe" en qué carpeta labura (su cwd + el focus_note).
        work_dir = folder_dir(folder)
        cli_key = body.get("cli") or "claude"
        adapter = CLIS.get(cli_key)
        if not adapter:
            self._json(400, {"error": f"CLI desconocido: {cli_key}"})
            return
        web_session = body.get("sessionId")
        # la sesión (resume) está atada al cwd (la carpeta) y al CLI; se cachea por
        # (sesión web, carpeta, cli). Solo aplica a los CLIs que soportan --resume.
        skey = f"{web_session}::{safe_name(folder)}::{cli_key}" if web_session else None
        resume = body.get("resume") or (SESSION_MAP.get(skey) if skey else None)
        mode = body.get("mode") or "auto-edit"
        model = body.get("model")
        effort = body.get("effort")

        run = new_run()

        def worker():
            run_cli(run, adapter, work_dir, message, mode, model, resume, name, folder,
                    effort, editor_target, editor_relay)
            if adapter.supports_resume and run.get("claude_session_id") and skey:
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

    # --- proxy de fetch (resuelve CORS: el request lo hace el server, no el browser) ---
    def _proxy_fetch(self, body):
        """Hace un request HTTP server-side y devuelve la respuesta. Lo usa el modo
        object para mandar fetches sin chocar con CORS (el browser no puede). Una
        respuesta HTTP (incluido 4xx/5xx) es ok=True con su status/body; un error de
        red/DNS es ok=False con el mensaje."""
        url = (body.get("url") or "").strip()
        method = (body.get("method") or "GET").upper()
        raw_headers = body.get("headers") or {}
        data = body.get("body")
        if not url:
            self._json(400, {"error": "falta url"})
            return

        hdrs = {}
        if isinstance(raw_headers, list):          # [{k,v}]
            for hh in raw_headers:
                if isinstance(hh, dict) and hh.get("k"):
                    hdrs[hh["k"]] = hh.get("v", "")
        elif isinstance(raw_headers, dict):        # {k:v}
            hdrs = {str(k): ("" if v is None else str(v)) for k, v in raw_headers.items()}

        payload = None
        if data is not None and method not in ("GET", "HEAD"):
            payload = data.encode("utf-8") if isinstance(data, str) else json.dumps(data).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=payload, method=method, headers=hdrs)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                self._json(200, {"ok": True, "status": resp.status,
                                 "statusText": getattr(resp, "reason", "") or "", "body": text})
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""
            self._json(200, {"ok": True, "status": e.code,
                             "statusText": getattr(e, "reason", "") or "", "body": text})
        except Exception as e:
            self._json(200, {"ok": False, "error": str(e)})

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
        except (ConnectionError, OSError):
            pass

    # log un poco más prolijo
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main():
    # modo MCP (doc 27, fase 4): re-ejecución de este mismo binario/script como
    # MCP server stdio de fs para editores EXTERNOS (lo lanza Claude Code).
    if "--mcp-fs" in sys.argv:
        import editor_mcp
        editor_mcp.main()
        return

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
    sweep_temp_files()                       # limpiar adjuntos vencidos al arrancar
    tok = get_token()                        # cargar/crear el token de acceso

    # watcher del state mirror (poll de mtime → SSE)
    threading.Thread(target=watch_state, daemon=True).start()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"DiagraMind local backend v{VERSION} → http://{HOST}:{args.port}")
    print(f"Claude Code: {'OK · ' + (claude_version(cb) or '') if cb else 'NO ENCONTRADO'}")
    print(f"Proyectos en: {projects_dir()}")
    print(f"Contraseña (token) de acceso: {tok}")
    print(f"  (guardada en {token_path()} — la web te la va a pedir al conectar)")
    print("Endpoints: GET /health · POST /projects/sync · POST /files/upload · "
          "POST /chat · GET /chat/stream · GET /projects/tree · POST /chat/cancel · "
          "GET /state · GET /state/stream · POST /state/write · POST /fetch")
    print("Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

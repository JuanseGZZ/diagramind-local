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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# módulos desacoplados (ver claude.py / codex.py / gemini.py / cli_base.py / etc.)
from util import safe_name, safe_file_name
from runs import RUNS, RUNS_LOCK, SESSION_MAP, new_run, emit
from skills import install_skills
from claude import find_claude, claude_version
from clis import CLIS, run_cli

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
NAME = "diagramind-local"
VERSION = "0.10.0"

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
            clis = []
            for a in CLIS.values():
                b = a.find()
                clis.append({"key": a.key, "label": a.label, "available": bool(b),
                             "version": a.version(b) if b else None, "resume": a.supports_resume})
            cb = find_claude()
            self._json(200, {
                "status": "ok", "name": NAME, "version": VERSION,
                "clis": clis,
                # compat: campo claude suelto (clientes viejos)
                "claude": {"available": bool(cb),
                           "version": claude_version(cb) if cb else None},
            })
        elif path == "/projects/tree":
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
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
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
            run_cli(run, adapter, work_dir, message, mode, model, resume, name, folder, effort)
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

    # watcher del state mirror (poll de mtime → SSE)
    threading.Thread(target=watch_state, daemon=True).start()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"DiagraMind local backend v{VERSION} → http://{HOST}:{args.port}")
    print(f"Claude Code: {'OK · ' + (claude_version(cb) or '') if cb else 'NO ENCONTRADO'}")
    print(f"Proyectos en: {projects_dir()}")
    print("Endpoints: GET /health · POST /projects/sync · POST /files/upload · "
          "POST /chat · GET /chat/stream · GET /projects/tree · POST /chat/cancel · "
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

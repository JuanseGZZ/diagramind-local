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
VERSION = "0.2.2"

# Modos del chat (web) → permission-mode de Claude Code.
PERM_MODE = {
    "auto-edit": "acceptEdits",
    "auto": "acceptEdits",
    "plan": "plan",
    "ask": "default",
}

SYSTEM_PREAMBLE = (
    "Estás trabajando dentro de un PROYECTO de DiagraMind. El diagrama vive en "
    "el archivo ./tree.json (en el directorio de trabajo). Su esquema y reglas "
    "están en las skills del proyecto (diagramind-format y la del tipo de árbol). "
    "Leé esas skills antes de editar. Si tenés que modificar el diagrama, editá "
    "./tree.json IN-PLACE manteniéndolo como JSON válido y respetando el esquema. "
    "No cambies el id del árbol. Cuando termines de editar, RELEÉ ./tree.json y "
    "verificá que es JSON válido y respeta el esquema del tipo (sin campos de más "
    "ni de menos, ids únicos); si algo está mal, corregilo antes de terminar. "
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


def projects_dir():
    return os.path.join(app_dir(), "projects")


def project_path(pid):
    # pid viene de la web; sanitizar para que no escape del dir
    safe = "".join(c for c in str(pid) if c.isalnum() or c in "-_")
    return os.path.join(projects_dir(), safe or "default")


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


def run_claude(run, project_dir, message, mode, model, resume):
    claude_bin = find_claude()
    if not claude_bin:
        set_status(run, "error", "No se encontró el binario `claude` en esta máquina.")
        return

    perm = PERM_MODE.get(mode, "acceptEdits")
    cmd = [
        claude_bin, "-p", message,
        "--output-format", "stream-json",
        "--verbose",                       # requerido por stream-json en -p
        "--model", map_model(model),
        "--permission-mode", perm,
        "--add-dir", project_dir,
        "--append-system-prompt", SYSTEM_PREAMBLE,
    ]
    if resume:
        cmd += ["--resume", str(resume)]

    set_status(run, "starting")
    try:
        proc = subprocess.Popen(
            cmd, cwd=project_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
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
            self._get_tree(q.get("id", [None])[0])
        elif path == "/chat/stream":
            self._stream(q.get("runId", [None])[0])
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/projects/sync":
            self._sync(self._read_json())
        elif path == "/chat":
            self._chat(self._read_json())
        elif path == "/chat/cancel":
            self._cancel(parse_qs(urlparse(self.path).query).get("runId", [None])[0])
        else:
            self._json(404, {"error": "not found", "path": path})

    # --- endpoints -------------------------------------------------------
    def _sync(self, body):
        pid = body.get("id")
        tree_json = body.get("treeJson")
        if not pid or tree_json is None:
            self._json(400, {"error": "faltan id o treeJson"})
            return
        pdir = project_path(pid)
        os.makedirs(pdir, exist_ok=True)
        # tree_json puede venir como string o como objeto
        if isinstance(tree_json, str):
            text = tree_json
        else:
            text = json.dumps(tree_json, ensure_ascii=False, indent=2)
        with open(os.path.join(pdir, "tree.json"), "w", encoding="utf-8") as f:
            f.write(text)
        install_skills(pdir)
        self._json(200, {"ok": True, "path": pdir})

    def _get_tree(self, pid):
        if not pid:
            self._json(400, {"error": "falta id"})
            return
        fp = os.path.join(project_path(pid), "tree.json")
        if not os.path.exists(fp):
            self._json(404, {"error": "no hay tree.json para ese proyecto"})
            return
        with open(fp, "r", encoding="utf-8") as f:
            self._json(200, {"treeJson": f.read()})

    def _chat(self, body):
        pid = body.get("projectId")
        message = body.get("message")
        if not pid or not message:
            self._json(400, {"error": "faltan projectId o message"})
            return
        pdir = project_path(pid)
        if not os.path.exists(os.path.join(pdir, "tree.json")):
            self._json(409, {"error": "el proyecto no está sincronizado (falta tree.json)"})
            return

        web_session = body.get("sessionId")
        resume = body.get("resume") or SESSION_MAP.get(web_session)
        mode = body.get("mode") or "auto-edit"
        model = body.get("model")

        run = new_run()

        def worker():
            run_claude(run, pdir, message, mode, model, resume)
            if run.get("claude_session_id") and web_session:
                SESSION_MAP[web_session] = run["claude_session_id"]

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

    os.makedirs(projects_dir(), exist_ok=True)
    cb = find_claude()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"DiagraMind local backend v{VERSION} → http://{HOST}:{args.port}")
    print(f"Claude Code: {'OK · ' + (claude_version(cb) or '') if cb else 'NO ENCONTRADO'}")
    print(f"Proyectos en: {projects_dir()}")
    print("Endpoints: GET /health · POST /projects/sync · POST /chat · "
          "GET /chat/stream · GET /projects/tree · POST /chat/cancel")
    print("Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

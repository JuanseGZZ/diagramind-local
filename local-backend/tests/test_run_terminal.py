"""Un run SIEMPRE termina: el chat no puede quedarse esperando un evento que no llega.

EL BUG QUE LO MOTIVA (bitácora §67): después del puente de permisos, un turno del chat
terminaba de responder —el texto se veía completo— y el composer quedaba ocupado, con el
botón de detener que no lo soltaba. No había ningún `claude -p` vivo y el backend
contestaba /health 200: lo que faltaba era el EVENTO TERMINAL del run.

La causa de fondo es de pipes: los servidores MCP que lanza Claude Code (editor_mcp,
permission_mcp) son NIETOS de este proceso y HEREDAN el stdout/stderr del CLI. El pipe
no da EOF hasta que muere el último que lo tiene, así que `for line in proc.stdout`
seguía esperando aunque el CLI ya hubiera terminado — y un nieto podía quedarse colgado
hasta 15 minutos esperando que alguien contestara un permiso. Como el estado terminal se
emitía DESPUÉS de ese loop, no se emitía nunca.

Se prueba sin red (salvo loopback) y sin gastar tokens: un CLI de mentira que reproduce
cada forma de dejar el turno sin final.

    python3 diagramind-local/local-backend/tests/test_run_terminal.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

# HOME propio ANTES de importar: el backend guarda su token y su config en el dir de
# datos del usuario, y este test no tiene por qué tocar el de la máquina.
_TMP = tempfile.mkdtemp(prefix="dm-run-terminal-")
os.environ["HOME"] = _TMP
os.environ["USERPROFILE"] = _TMP
os.environ["LOCALAPPDATA"] = _TMP
os.environ["XDG_DATA_HOME"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli_base                                         # noqa: E402
import runs                                             # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def estados(run):
    return [e.get("status") for e in run["events"] if e["kind"] == "status"]


def terminal(run):
    return run["status"] in ("done", "error", "cancelled")


# ---------------- un CLI de mentira ----------------
# Habla el mismo JSONL que los adaptadores reales: una línea por evento.

def script(cuerpo):
    p = os.path.join(_TMP, f"cli-{abs(hash(cuerpo))}.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(cuerpo)
    return p


class CliDeMentira:
    key = "fake"
    label = "Fake CLI"
    bin_names = ["python"]
    supports_resume = False

    def __init__(self, path, explota_en=None):
        self.path = path
        self.explota_en = explota_en      # "build" | "find" | None

    def find(self):
        if self.explota_en == "find":
            raise RuntimeError("se rompió buscando el binario")
        return sys.executable

    def version(self, b):
        return "0"

    def install_instructions(self, work_dir):
        pass

    def build_cmd(self, run, b, message, work_dir, folder, focus_name, mode, model,
                  resume, effort=None, editor_target=None, editor_relay=None):
        if self.explota_en == "build":
            raise RuntimeError("se rompió armando el comando")
        return [b, self.path], {}

    def parse_line(self, run, line):
        obj = json.loads(line)
        if obj.get("kind") == "assistant":
            runs.emit(run, "assistant", text=obj.get("text", ""))

    def finalize(self, run):
        pass


def correr(adapter, timeout=30):
    """Corre un turno en un hilo aparte: si vuelve a colgarse, el test FALLA en vez de
    dejar la suite esperando para siempre (que es justo el bug que estamos cazando)."""
    run = runs.new_run()
    t0 = time.monotonic()
    h = threading.Thread(target=lambda: cli_base.run_cli(
        run, adapter, _TMP, "hacé X", "auto-edit", "sonnet", None, "proyecto", "carpeta"),
        daemon=True)
    h.start()
    h.join(timeout)
    return run, time.monotonic() - t0, not h.is_alive()


# =================================================================================
print("\n### A. un NIETO agarrado a los pipes no deja el turno sin final")
# El CLI escribe su respuesta, deja un nieto vivo (que hereda stdout/stderr) y se muere.
# Así se ve en producción: `claude` ya terminó, pero el MCP de permisos sigue esperando.
NIETO = 25          # mucho más que PIPE_GRACE: si se espera al nieto, el test lo canta
cli_nieto = CliDeMentira(script(
    "import json, subprocess, sys, time\n"
    "print(json.dumps({'kind': 'assistant', 'text': 'listo, leí tus PDFs'}), flush=True)\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%d)'], close_fds=False)\n"
    % NIETO
))
run, tardo, termino = correr(cli_nieto, timeout=NIETO - 5)
check("el turno termina (no se queda esperando al nieto)", termino)
check("y queda en un estado TERMINAL", terminal(run), str(estados(run)))
check(f"sin esperar al nieto (tardó {tardo:.1f}s, el nieto vive {NIETO}s)", tardo < NIETO - 6)
check("no se pierde lo que el CLI alcanzó a decir",
      any(e["kind"] == "assistant" and "PDFs" in e.get("text", "") for e in run["events"]))
check("el margen para los pipes es acotado", cli_base.PIPE_GRACE <= 10)

print("\n### B. stderr se drena (si no, se llena el pipe y el que se traba es el CLI)")
cli_verborragico = CliDeMentira(script(
    "import json, sys\n"
    "sys.stderr.write('x' * 300000)\n"           # muy arriba de los ~64 KB del pipe
    "sys.stderr.flush()\n"
    "print(json.dumps({'kind': 'assistant', 'text': 'igual contesté'}), flush=True)\n"
))
run, tardo, termino = correr(cli_verborragico, timeout=25)
check("el CLI no se traba escribiendo en stderr", termino)
check("y el run termina", terminal(run), str(estados(run)))
check("con lo que dijo por stdout", any(e["kind"] == "assistant" for e in run["events"]))

print("\n### C. si algo explota, el run igual queda en estado terminal")
for donde, texto in (("find", "buscando el binario"), ("build", "armando el comando")):
    run, _, termino = correr(CliDeMentira(script("pass\n"), explota_en=donde), timeout=15)
    check(f"explotar {texto} no deja el run colgado", termino and terminal(run), str(estados(run)))
    check("  y se cuenta como error (el chat lo muestra)", run["status"] == "error",
          str(run.get("error")))

print("\n### D. un CLI que se muere sin decir nada igual cierra el turno")
run, _, termino = correr(CliDeMentira(script("import sys; sys.exit(3)\n")), timeout=15)
check("termina", termino and terminal(run), str(estados(run)))
check("y avisa que salió mal", run["status"] == "error", str(run.get("error")))

# =================================================================================
# Esto va POR LA RED contra el server real: el bug era justamente que el ENDPOINT
# cancelaba a mano (run["status"] = "cancelled") en vez de pasar por set_status, que es
# el que destraba los permisos pendientes. El test de la unidad pasaba igual.
print("\n### E. cancelar POR LA RED destraba el permiso que estaba esperando")
import server                                           # noqa: E402
from http.server import ThreadingHTTPServer             # noqa: E402

httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
TOKEN = server.get_token()


def post(path):
    req = urllib.request.Request(BASE + path, data=b"", method="POST",
                                 headers={"X-DiagraMind-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:      # 404 y compañía también son respuestas
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


run = runs.new_run()
run["status"] = "streaming"
res = {}
esperando = threading.Thread(target=lambda: res.update(
    runs.perm_ask(run, "Bash", {"command": "unzip apuntes.docx"}, "toolu_9")), daemon=True)
esperando.start()
time.sleep(0.3)
check("el pedido de permiso está esperando", esperando.is_alive())
code, body = post(f"/chat/cancel?runId={run['id']}")
check("el cancel contesta ok", code == 200 and body.get("ok") is True, str(body))
esperando.join(timeout=5)
check("y la espera se corta (no se queda los 15 minutos del timeout)", not esperando.is_alive())
check("denegando, que es el default seguro", res.get("decision") == "deny", str(res))
check("y diciendo que se canceló", "cancel" in (res.get("message") or "").lower(), str(res))
check("el run queda cancelado", run["status"] == "cancelled")
check("con el evento terminal para la web", "cancelled" in estados(run), str(estados(run)))

code, _ = post("/chat/cancel?runId=no-existe")
check("cancelar un run que no existe sigue siendo 404", code == 404)

httpd.shutdown()
print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)

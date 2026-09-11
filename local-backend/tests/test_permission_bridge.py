"""Puente de permisos: Claude Code headless pregunta, el usuario contesta en el chat.

EL BUG QUE LO MOTIVA (bitácora §66): el chat corre `claude -p`, o sea sin terminal
donde apretar "sí". Todo lo que necesitaba aprobación (unzip, pdftotext, cualquier
comando fuera de la whitelist) se auto-denegaba, y el modelo lo contaba como una
incapacidad suya: "queda bloqueado pidiendo aprobación en esta sesión no interactiva".

Lo que se prueba acá es la mecánica completa SIN red y SIN gastar tokens: el cableado
del comando, la espera que bloquea hasta la respuesta, y la traducción al contrato que
espera la CLI. El contrato (`{"behavior":"allow","updatedInput":…}`) se verificó a mano
contra el CLI real 2.1.263 — ver el docstring de permission_mcp.py.

    python3 diagramind-local/local-backend/tests/test_permission_bridge.py
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import permission_mcp                                   # noqa: E402
import runs                                             # noqa: E402
from claude import ClaudeAdapter                        # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def nuevo_run(con_backend=True):
    run = runs.new_run()
    if con_backend:
        run["local_url"] = "http://127.0.0.1:9"
        run["local_token"] = "tok"
    return run


def kinds(run):
    return [e["kind"] for e in run["events"]]


A = ClaudeAdapter()

print("\n### A. el comando le dice a la CLI a quién preguntarle")
run = nuevo_run()
cmd, _ = A.build_cmd(run, "claude", "hacé X", "/w", "carpeta", "proyecto", "auto-edit", "sonnet", None)
check("pasa --permission-prompt-tool",
      "--permission-prompt-tool" in cmd)
check("apuntando a la tool del MCP propio",
      cmd[cmd.index("--permission-prompt-tool") + 1] == "mcp__dmperm__approve")
cfg_path = cmd[cmd.index("--mcp-config") + 1]
cfg = json.load(open(cfg_path))
check("declara el server dmperm", "dmperm" in cfg["mcpServers"])
check("que es ESTE backend en modo --mcp-permission",
      "--mcp-permission" in cfg["mcpServers"]["dmperm"]["args"])
env = cfg["mcpServers"]["dmperm"]["env"]
check("con la URL, el token y el run para volver",
      env["DMPERM_URL"] == run["local_url"] and env["DMPERM_TOKEN"] == "tok"
      and env["DMPERM_RUN"] == run["id"])
check("el config es 0600 (lleva el token)", oct(os.stat(cfg_path).st_mode)[-3:] == "600")
A.finalize(run)
check("y se borra al terminar el run", not os.path.exists(cfg_path))

print("\n### B. sin backend que conteste, no se promete lo que no se puede cumplir")
run = nuevo_run(con_backend=False)
cmd, _ = A.build_cmd(run, "claude", "x", "/w", "c", "p", "auto-edit", "sonnet", None)
check("no pasa --permission-prompt-tool", "--permission-prompt-tool" not in cmd)
A.finalize(run)

print("\n### C. conviven el MCP del editor externo y el de permisos")
run = nuevo_run()
cmd, _ = A.build_cmd(run, "claude", "x", "/w", "c", "p", "auto-edit", "sonnet", None,
                     editor_relay={"url": "https://free-1.diagraminder.com",
                                   "token": "t", "projectId": "p1"})
check("UN solo --mcp-config", cmd.count("--mcp-config") == 1)
cfg = json.load(open(cmd[cmd.index("--mcp-config") + 1]))
check("con los DOS servers", sorted(cfg["mcpServers"].keys()) == ["dmfs", "dmperm"])
check("las tools del editor siguen pre-aprobadas",
      "--allowedTools" in cmd and "mcp__dmfs__fs_read" in cmd[cmd.index("--allowedTools") + 1])
A.finalize(run)

print("\n### D. la espera: bloquea hasta que el usuario contesta")
run = nuevo_run()
res = {}
t = threading.Thread(target=lambda: res.update(
    runs.perm_ask(run, "Bash", {"command": "unzip apuntes.docx"}, "toolu_1")), daemon=True)
t.start()
time.sleep(0.25)
check("sigue esperando (no se auto-deniega)", t.is_alive())
check("el pedido salió por el stream del chat", "permission" in kinds(run))
ev = [e for e in run["events"] if e["kind"] == "permission"][0]
check("con la tool y el comando que va a correr",
      ev["tool"] == "Bash" and ev["input"]["command"] == "unzip apuntes.docx")
check("y el id del tool_use de la CLI", ev["toolUseId"] == "toolu_1")
check("aceptar destraba", runs.perm_answer(run, ev["id"], "allow") is True)
t.join(timeout=3)
check("y responde allow", res.get("decision") == "allow", str(res))
check("se avisa que la tarjeta ya no espera", "permission-resolved" in kinds(run))

print("\n### E. rechazar viaja con su motivo (el modelo lo LEE)")
run = nuevo_run()
res = {}
t = threading.Thread(target=lambda: res.update(
    runs.perm_ask(run, "Bash", {"command": "rm -rf /"}, "toolu_2")), daemon=True)
t.start(); time.sleep(0.2)
pid = [e for e in run["events"] if e["kind"] == "permission"][0]["id"]
runs.perm_answer(run, pid, "deny", "ni en pedo")
t.join(timeout=3)
check("responde deny", res.get("decision") == "deny")
check("con el motivo del usuario", res.get("message") == "ni en pedo")
check("contestar dos veces el mismo pedido no rompe",
      runs.perm_answer(run, pid, "allow") is False)

print("\n### F. si el run se cancela, nadie queda colgado")
run = nuevo_run()
res = {}
t = threading.Thread(target=lambda: res.update(
    runs.perm_ask(run, "Bash", {"command": "sleep 999"}, "toolu_3")), daemon=True)
t.start(); time.sleep(0.2)
runs.set_status(run, "cancelled")
t.join(timeout=3)
check("la espera termina sola", not t.is_alive())
check("y deniega (la CLI no puede esperar para siempre)", res.get("decision") == "deny")
check("diciendo que se canceló", "cancel" in (res.get("message") or "").lower(), str(res))

print("\n### G. la traducción al contrato que espera Claude Code")
# backend de mentira: responde lo que le digamos, sin red de verdad más que loopback
RESP = {"decision": "allow", "input": {"command": "echo ok"}}
recibido = {}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        recibido.update(json.loads(self.rfile.read(n) or b"{}"))
        recibido["token"] = self.headers.get("X-DiagraMind-Token")
        body = json.dumps(RESP).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
permission_mcp.BASE = f"http://127.0.0.1:{srv.server_address[1]}"
permission_mcp.TOKEN = "tok"
permission_mcp.RUN = "run-1"

out = permission_mcp._ask_user("Bash", {"command": "echo ok"}, "toolu_9")
check("allow devuelve behavior=allow", out.get("behavior") == "allow")
check("y SIEMPRE con updatedInput (sin él, la CLI lo toma por inválido y deniega)",
      out.get("updatedInput") == {"command": "echo ok"})
check("el pedido viaja con runId, tool e input",
      recibido.get("runId") == "run-1" and recibido.get("tool") == "Bash"
      and recibido.get("input") == {"command": "echo ok"})
check("autenticado con el token del backend", recibido.get("token") == "tok")

RESP = {"decision": "deny", "message": "no quiero"}
out = permission_mcp._ask_user("Bash", {"command": "rm -rf /"}, "toolu_10")
check("deny devuelve behavior=deny", out.get("behavior") == "deny")
check("con el mensaje que escribió el usuario", out.get("message") == "no quiero")

srv.shutdown()
permission_mcp.BASE = "http://127.0.0.1:9"      # puerto muerto
permission_mcp.POLL_TIMEOUT = 2
out = permission_mcp._ask_user("Bash", {"command": "x"}, "toolu_11")
check("si el backend no está, deniega (no cuelga la CLI)", out.get("behavior") == "deny")
check("y explica por qué, para que el modelo no reintente al pedo",
      "permission" in (out.get("message") or "").lower(), str(out))

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)

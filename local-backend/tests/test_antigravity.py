"""Adaptador del Antigravity CLI (`agy`) — comando y traducción de eventos.

Los EVENTOS DE ABAJO SON REALES: se capturaron corriendo `agy -p … --output-format
stream-json` contra el binario 1.1.24 el 2026-09-02. No están inventados ni copiados
de una documentación — que es justamente la regla del CLAUDE.md que ya costó cara una
vez (un esquema documentado de memoria que produjo proyectos vacíos en producción).

Este test NO llama a la API de Google: no gasta cuota y corre sin red.

    python3 diagramind-local/local-backend/tests/test_antigravity.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from antigravity import AntigravityAdapter, handle_event   # noqa: E402
from clis import CLIS                                      # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def new_run():
    return {"id": "t", "seq": 0, "events": [], "status": "new"}


def kinds(run):
    return [e["kind"] for e in run["events"]]


def texts(run):
    return [e.get("text") for e in run["events"] if e["kind"] == "assistant"]


A = AntigravityAdapter()

print("\n### A. queda registrado como un CLI más")
check("está en CLIS", "antigravity" in CLIS)
check("soporta resume (--conversation)", A.supports_resume is True)
check("el binario que busca es `agy`", A.bin_names == ["agy"])

print("\n### B. el comando")
cmd, env = A.build_cmd(new_run(), "agy", "hacé X", "/w", "carpeta", "proyecto",
                       "auto-edit", "gemini-3.7-flash-medium", None)
check("corre headless (-p)", "-p" in cmd)
check("pide stream-json (es lo que sabe parsear parse_line)",
      "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "stream-json")
check("pasa --add-dir con el work_dir — SIN ESTO no ve sus propios archivos",
      "--add-dir" in cmd and cmd[cmd.index("--add-dir") + 1] == "/w")
check("auto-aprueba las tools (headless no tiene quién apruebe)",
      "--dangerously-skip-permissions" in cmd)
check("manda el modelo tal cual (el CLI no acepta alias)",
      cmd[cmd.index("--model") + 1] == "gemini-3.7-flash-medium")
check("no expande slash commands del texto del usuario", "--disable-slash-commands" in cmd)
check("NO manda --effort (el nivel ya va pegado al id del modelo)", "--effort" not in cmd)
check("la instrucción viaja EN el prompt (agy no tiene --append-system-prompt)",
      any("hacé X" in str(c) for c in cmd))
check("sin env extra", env == {})

cmd2, _ = A.build_cmd(new_run(), "agy", "m", "/w", "f", "p", "auto", None, "abc-123")
check("con resume agrega --conversation <id>",
      "--conversation" in cmd2 and cmd2[cmd2.index("--conversation") + 1] == "abc-123")
check("y sin resume no lo agrega", "--conversation" not in cmd)

cmd3, _ = A.build_cmd(new_run(), "agy", "m", "/w", "f", "p", "plan", None, None)
check("en modo plan usa --mode plan", "--mode" in cmd3 and cmd3[cmd3.index("--mode") + 1] == "plan")
check("y en plan NO saltea permisos (planificar no escribe)",
      "--dangerously-skip-permissions" not in cmd3)

print("\n### C. los eventos REALES del binario")

# ---- capturado tal cual de `agy -p "Reply with exactly: OK" --output-format stream-json`
INIT = {"event": "init", "conversation_id": "b0a14f6c-b95d-4280-bb03-efd5940423e0",
        "init": {"model": "gemini-3.7-flash-low", "cwd": "/tmp/x", "tools": ["view_file"],
                 "permission_mode": "request-review"}}
STEP_USER = {"event": "step_update", "step_update": {
    "conversation_id": "b0a14f6c", "step_index": 0, "state": "DONE", "step_type": "user_input"}}
STEP_TEXT = {"event": "step_update", "step_update": {
    "conversation_id": "b0a14f6c", "step_index": 1, "state": "DONE",
    "step_type": "agent_response", "text_delta": "OK\n", "duration_seconds": 1.9,
    "usage": {"input_tokens": 13776, "output_tokens": 24}}}
TOOL_ACTIVE = {"event": "step_update", "step_update": {
    "conversation_id": "b0a14f6c", "step_index": 2, "state": "ACTIVE",
    "step_type": "tool", "tool_name": "find_by_name", "tool_info": {}}}
TOOL_DONE = {"event": "step_update", "step_update": {
    "conversation_id": "b0a14f6c", "step_index": 2, "state": "DONE",
    "step_type": "tool", "tool_name": "find_by_name", "tool_info": {}, "duration_seconds": 0.4}}
SYS_MSG = {"event": "step_update", "step_update": {
    "conversation_id": "b0a14f6c", "step_index": 3, "state": "DONE",
    "step_type": "system_message", "duration_seconds": 0.1}}
RESULT = {"event": "result", "result": {
    "conversation_id": "b0a14f6c-b95d-4280-bb03-efd5940423e0", "status": "SUCCESS",
    "response": "OK\n", "duration_seconds": 1.95, "num_turns": 1,
    "usage": {"input_tokens": 13776, "output_tokens": 24}}}

run = new_run()
for ev in (INIT, STEP_USER, STEP_TEXT, TOOL_ACTIVE, TOOL_DONE, SYS_MSG, RESULT):
    handle_event(run, ev)

check("del init sale el id de conversación (es lo que resume el próximo turno)",
      run["claude_session_id"] == "b0a14f6c-b95d-4280-bb03-efd5940423e0")
check("el texto del agente llega a la web", texts(run) == ["OK\n"])
check("el texto final NO se duplica (ya salió por text_delta)",
      texts(run).count("OK\n") == 1, str(texts(run)))
check("la tool se anuncia UNA vez (el DONE repite el nombre y duplicaría la línea)",
      [e.get("name") for e in run["events"] if e["kind"] == "tool"] == ["find_by_name"])
check("`user_input` y `system_message` no ensucian la conversación",
      kinds(run).count("assistant") == 1)
check("el run termina en done", run["status"] == "done")

print("\n### D. casos que sí rompen")
run = new_run()
handle_event(run, INIT)
handle_event(run, {"event": "result", "result": {
    "conversation_id": "b0a14f6c", "status": "ERROR", "response": "quota exceeded"}})
check("un status distinto de SUCCESS deja el run en error", run["status"] == "error")
check("y el motivo se conserva", "quota" in (run.get("error") or ""))

run = new_run()
handle_event(run, INIT)
handle_event(run, RESULT)
check("si el turno no emitió deltas, el texto final SÍ se usa", texts(run) == ["OK\n"])

run = new_run()
A.parse_line(run, "esto no es json")
check("una línea que no es JSON no revienta el parseo", run["events"] == [])

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(0 if fail == 0 else 1)

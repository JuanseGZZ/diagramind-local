"""Qué CLI corre el ORQUESTADOR en un nodo agente, y cómo arma y lee su turno.

Desde 2026-09-15 el motor despacha por PERFIL (`orch_cli.py`) y no contra `claude` fijo:
un orquestador puede tener un nodo Claude Code y otro Antigravity corriendo a la vez.
Codex y Gemini quedan afuera porque no tienen `--add-dir`, y en el orquestador el agente
corre en un workspace vacío donde sus recursos se montan justamente con ese flag.

Lo que este test cuida:

- **Regresión de Claude Code**: su comando queda EXACTAMENTE como estaba antes del
  refactor. Es el único camino que ya corría en producción.
- **Antigravity**: que se le pase lo que su binario acepta (system dentro del prompt,
  `--conversation`, `--sandbox`) y NADA que no tenga (`--append-system-prompt`,
  `--allowedTools`, `--mcp-config`) — un flag inventado lo mata al arrancar.
- Que las guardas que no se pueden cumplir se rechacen ANTES de gastar un turno.

No llama a ninguna API ni ejecuta ningún CLI: corre sin red.

    python3 diagramind-local/local-backend/tests/test_orch_cli_provider.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orch_cli                                            # noqa: E402
import orchestrator                                        # noqa: E402
from clis import CLIS                                      # noqa: E402
from orch_cli import ORCH_CLIS, CliTurnError               # noqa: E402
from orchestrator import (CLI_ENGINE_OK, CLI_PROVIDERS, OrchError, _cli_for,  # noqa: E402
                          cli_session_get, cli_session_set)

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def nodo(provider, **data):
    return {"id": "n1", "titulo": "Contador", "data": {"ia": {"provider": provider}, **data}}


def spec(**kw):
    """Un spec como el que arma `_cli_cmd`, con lo mínimo de un agente real."""
    s = {"node_id": "n1", "msg": "hacé X", "system": "YOU ARE THE ACCOUNTANT",
         "model": None, "effort": None, "add_dirs": [], "confinado": False,
         "mcp": {}, "mcp_env": {"url": "http://127.0.0.1:8765", "token": "tok"},
         "exec_ok": False, "session": None}
    s.update(kw)
    return s


def flag(cmd, name):
    """El valor que sigue a `name`, o None si el flag no está."""
    return cmd[cmd.index(name) + 1] if name in cmd else None


print("\n=== A. la web ofrece cuatro CLIs; el motor EJECUTA los que tienen perfil ===")
for p in ("local", "local-codex", "local-gemini", "local-antigravity"):
    check(f"«{p}» entra por la rama CLI (no por la de API)", p in CLI_PROVIDERS)
check("hay un provider CLI por cada adaptador del chat", len(CLI_PROVIDERS) == len(CLIS),
      f"{sorted(CLI_PROVIDERS)} vs {sorted(CLIS)}")
check("el motor ejecuta Claude Code y Antigravity",
      sorted(ORCH_CLIS) == ["local", "local-antigravity"], sorted(ORCH_CLIS))
check("CLI_ENGINE_OK sale de los perfiles (una sola fuente)", CLI_ENGINE_OK == set(ORCH_CLIS))
# cada perfil del orquestador tiene su adaptador en el chat: mismo binario en los dos lados
check("el perfil de Claude apunta al mismo binario que el chat",
      ORCH_CLIS["local"].bin_names == CLIS["claude"].bin_names)
check("y el de Antigravity también (`agy`)",
      ORCH_CLIS["local-antigravity"].bin_names == CLIS["antigravity"].bin_names == ["agy"])

print("\n=== B. qué perfil le toca a cada nodo ===")
check("«local» → Claude Code", _cli_for(nodo("local")).key == "local")
check("«local-antigravity» → Antigravity", _cli_for(nodo("local-antigravity")).key == "local-antigravity")
check("un nodo sin ia definida cae en Claude Code", _cli_for({"id": "n", "titulo": "x", "data": {}}).key == "local")
for p in ("local-codex", "local-gemini"):
    try:
        _cli_for(nodo(p))
        check(f"«{p}» se rechaza", False, "no levantó OrchError")
    except OrchError as e:
        msg = str(getattr(e, "detail", e))
        check(f"«{p}» se rechaza", True)
        check("  …explicando que es por --add-dir (los recursos del nodo)", "--add-dir" in msg, msg)
        check("  …y nombrando los que SÍ corren", "Claude Code" in msg and "Antigravity" in msg, msg)
        check("  …y que en el chat andan", "chat" in msg.lower(), msg)

print("\n=== C. confinado: solo el CLI que puede cumplirlo ===")
check("Claude Code puede confinar", ORCH_CLIS["local"].can_confine is True)
check("Antigravity NO puede (no tiene MCP ni --allowedTools)",
      ORCH_CLIS["local-antigravity"].can_confine is False)
check("un nodo confinado con Claude Code se acepta",
      _cli_for(nodo("local", confinado=True)).key == "local")
try:
    _cli_for(nodo("local-antigravity", confinado=True))
    check("un nodo confinado con agy se rechaza", False, "no levantó OrchError")
except OrchError as e:
    msg = str(getattr(e, "detail", e))
    check("un nodo confinado con agy se rechaza", True)
    check("  …diciendo en qué se apoya el confinamiento",
          "--mcp-config" in msg and "--allowedTools" in msg, msg)
    check("  …y qué hacer (Claude Code, o apagarlo)", "Claude Code" in msg, msg)

print("\n=== D. Claude Code: el comando no se movió (regresión) ===")
claude = ORCH_CLIS["local"]
cmd, cfg = claude.build("/bin/claude", spec(model="claude-opus-5", add_dirs=["/tmp/proj"],
                                            session="sess-1", exec_ok=True))
check("lanza `-p` con el mensaje", cmd[1] == "-p" and cmd[2] == "hacé X", " ".join(cmd[:3]))
check("el system va en --append-system-prompt",
      flag(cmd, "--append-system-prompt") == "YOU ARE THE ACCOUNTANT")
check("el modelo se mapea al alias del CLI", flag(cmd, "--model") == "opus", flag(cmd, "--model"))
check("stream-json + verbose", "--output-format" in cmd and "--verbose" in cmd)
check("--permission-mode acceptEdits", flag(cmd, "--permission-mode") == "acceptEdits")
check("monta el recurso con --add-dir", flag(cmd, "--add-dir") == "/tmp/proj")
check("con permiso «ejecutar» pre-aprueba el shell",
      "Bash,PowerShell" in cmd, " ".join(cmd))
check("retoma con --resume", flag(cmd, "--resume") == "sess-1")
check("sin confinar no arma config MCP", cfg is None)
cmd_noexec, _ = claude.build("/bin/claude", spec())
check("sin permiso «ejecutar» le saca el shell",
      "Bash" in cmd_noexec[cmd_noexec.index("--disallowedTools"):], " ".join(cmd_noexec))
# La regresión de verdad: el comando ENTERO, no flag por flag. Este es el que corría en
# producción antes de que el motor despachara por perfil; si alguien lo mueve, se entera acá.
check("el comando completo quedó idéntico al de antes del refactor", cmd_noexec == [
    "/bin/claude", "-p", "hacé X", "--output-format", "stream-json", "--verbose",
    "--model", "sonnet", "--permission-mode", "acceptEdits",
    "--append-system-prompt", "YOU ARE THE ACCOUNTANT",
    "--disallowedTools", "WebFetch", "WebSearch", "Bash", "PowerShell"], " ".join(cmd_noexec))
cmd_conf, cfg_conf = claude.build("/bin/claude", spec(confinado=True, add_dirs=["/tmp/d"],
                                                      mcp={"dmfs7": {"projectId": "p7", "perm": 2}}))
check("confinado arma el --mcp-config", cfg_conf is not None and "--mcp-config" in cmd_conf)
check("y la whitelist nombra las tools del MCP",
      "mcp__dmfs7__fs_exec" in flag(cmd_conf, "--allowedTools"), flag(cmd_conf, "--allowedTools"))
if cfg_conf:
    os.remove(cfg_conf)
cmd_eff, _ = claude.build("/bin/claude", spec(effort="high"))
check("el esfuerzo se le suma al mensaje (keyword)", cmd_eff[2] != "hacé X", cmd_eff[2][:60])

print("\n=== E. Antigravity: solo flags que su binario tiene ===")
agy = ORCH_CLIS["local-antigravity"]
cmd, cfg = agy.build("/bin/agy", spec(add_dirs=["/tmp/a", "/tmp/b"], session="conv-9"))
check("lanza `-p`", cmd[1] == "-p")
check("el system va DENTRO del prompt (no tiene flag propio)",
      "YOU ARE THE ACCOUNTANT" in cmd[2] and "hacé X" in cmd[2], cmd[2][:80])
check("y el system va ANTES de la tarea", cmd[2].index("ACCOUNTANT") < cmd[2].index("hacé X"))
check("monta los DOS recursos con --add-dir",
      cmd.count("--add-dir") == 2 and "/tmp/a" in cmd and "/tmp/b" in cmd, " ".join(cmd))
check("retoma con --conversation (no --resume)",
      flag(cmd, "--conversation") == "conv-9" and "--resume" not in cmd)
check("modo accept-edits + saltear permisos (headless no puede aprobar)",
      flag(cmd, "--mode") == "accept-edits" and "--dangerously-skip-permissions" in cmd)
check("sin permiso «ejecutar» va con --sandbox", "--sandbox" in cmd)
check("con permiso «ejecutar» NO va --sandbox",
      "--sandbox" not in agy.build("/bin/agy", spec(exec_ok=True))[0])
check("no arma config MCP (no tiene)", cfg is None)
for f in ("--append-system-prompt", "--allowedTools", "--disallowedTools", "--mcp-config",
          "--permission-mode", "--verbose"):
    check(f"NO le pasa «{f}» (su binario no lo tiene)", f not in cmd)
check("el modelo por defecto es el real del adaptador",
      flag(agy.build("/bin/agy", spec())[0], "--model") == "gemini-3.7-flash-medium")
check("y un id elegido se manda tal cual (el CLI no acepta alias)",
      flag(agy.build("/bin/agy", spec(model="claude-opus-4-6-thinking"))[0], "--model")
      == "claude-opus-4-6-thinking")
check("el esfuerzo NO se le suma al mensaje (ya va pegado al id del modelo)",
      agy.build("/bin/agy", spec(effort="high"))[0][2] == agy.build("/bin/agy", spec())[0][2],
      "el effort cambió el prompt de agy")

print("\n=== F. leer el stream: Claude Code ===")
logs = []
st = orch_cli.new_state()
claude.feed({"type": "system", "subtype": "init", "session_id": "s7"}, st, lambda t, full=None: logs.append(t))
claude.feed({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "hola"}, {"type": "tool_use", "name": "Read"}]}},
    st, lambda t, full=None: logs.append(t))
claude.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "chau"}]}},
            st, lambda t, full=None: logs.append(t))
claude.feed({"type": "result", "session_id": "s7", "result": None, "total_cost_usd": 0.42},
            st, lambda t, full=None: logs.append(t))
text, sid, cost = orch_cli.result_of(st, claude)
check("saca la sesión", sid == "s7")
check("junta los bloques de texto con renglón en blanco", text == "hola\n\nchau", repr(text))
check("informa el costo", cost == 0.42)
check("loguea la tool usada", any("Read" in x for x in logs), str(logs))
den = []
claude.feed({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "This command requires approval"}]}},
    orch_cli.new_state(), lambda t, full=None: den.append(t))
check("avisa cuando una tool se DENIEGA headless", any("denied" in x.lower() for x in den), str(den))
try:
    claude.feed({"type": "result", "is_error": True, "result": "boom"}, orch_cli.new_state(),
                lambda t, full=None: None)
    check("un error del CLI corta el turno", False, "no levantó")
except CliTurnError as e:
    check("un error del CLI corta el turno", "boom" in str(e), str(e))

print("\n=== G. leer el stream: Antigravity (eventos REALES del binario) ===")
logs = []
st = orch_cli.new_state()
log = lambda t, full=None: logs.append(t)          # noqa: E731
agy.feed({"event": "init", "conversation_id": "c1", "init": {"model": "gemini-3.7-flash-medium"}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "agent_response",
                                                  "state": "ACTIVE", "text_delta": "Ya "}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE",
                                                  "tool_name": "view_file"}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "tool", "state": "DONE",
                                                  "tool_name": "view_file"}}, st, log)
agy.feed({"event": "step_update", "step_update": {"step_type": "agent_response",
                                                  "state": "DONE", "text_delta": "terminé."}}, st, log)
agy.feed({"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS",
                                        "response": "Ya terminé."}}, st, log)
text, sid, cost = orch_cli.result_of(st, agy)
check("saca el conversation_id (es lo que retoma el próximo turno)", sid == "c1")
check("pega los PEDAZOS sin separador", text == "Ya terminé.", repr(text))
check("loguea la tool UNA vez (el DONE no repite)",
      len([x for x in logs if "view_file" in x]) == 1, str(logs))
check("no inventa un costo (agy no lo informa)", cost == 0.0)
st2 = orch_cli.new_state()
agy.feed({"event": "result", "result": {"conversation_id": "c2", "status": "SUCCESS",
                                        "response": "respuesta directa"}}, st2, log)
check("si no hubo deltas usa el `response` final",
      orch_cli.result_of(st2, agy)[0] == "respuesta directa")
try:
    agy.feed({"event": "result", "result": {"status": "ERROR", "response": "se cayó"}},
             orch_cli.new_state(), log)
    check("un status != SUCCESS corta el turno", False, "no levantó")
except CliTurnError as e:
    check("un status != SUCCESS corta el turno", "se cayó" in str(e), str(e))

print("\n=== H. la sesión guardada es DE SU CLI ===")
with tempfile.TemporaryDirectory() as tmp:
    ctx = {"app_dir": tmp, "pid": "p1"}
    cli_session_set(ctx, "n1", "abc", "local")
    check("Claude recupera la suya", cli_session_get(ctx, "n1", "local") == "abc")
    check("agy NO recibe la sesión de Claude (sería un id que no entiende)",
          cli_session_get(ctx, "n1", "local-antigravity") is None)
    cli_session_set(ctx, "n2", "conv-1", "local-antigravity")
    check("agy recupera la suya", cli_session_get(ctx, "n2", "local-antigravity") == "conv-1")
    check("y Claude no toma la de agy", cli_session_get(ctx, "n2", "local") is None)
    # formato viejo (string suelto): era de Claude Code, que era el único que había
    orchestrator._write_json(orchestrator._cli_sessions_path(ctx), {"n3": "vieja"})
    check("una sesión del formato viejo sigue siendo de Claude",
          cli_session_get(ctx, "n3", "local") == "vieja")
    check("…y no se le pasa a otro CLI", cli_session_get(ctx, "n3", "local-antigravity") is None)
    check("sin pedir CLI, devuelve lo que haya (compat)", cli_session_get(ctx, "n3") == "vieja")

print(f"\n{'✅' if fail == 0 else '❌'} {ok}/{ok + fail}")
sys.exit(0 if fail == 0 else 1)

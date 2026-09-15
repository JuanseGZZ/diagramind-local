"""Perfiles de CLI del ORQUESTADOR: qué flags acepta cada binario y cómo se lee su stream.

⚠️ No confundir con `clis.py`, que es el registro del **CHAT**. Los dos lanzan los mismos
binarios, pero piden cosas distintas:

    chat          → un prompt armado por `_headless_prompt`, y `parse_line` EMITE eventos
                    al run de la web (runs.py).
    orquestador   → system propio del agente, `--add-dir` de SUS recursos, confinamiento,
                    y un parser que DEVUELVE (texto, session_id, costo) para el frame.

De `clis.py` se reutiliza lo que ya está resuelto (encontrar el binario, instalarle las
instrucciones); lo de acá es lo que el chat no tiene.

**Qué puede cada CLI** (verificado contra los binarios, ver `claude.py` / `antigravity.py`):

| | `--append-system-prompt` | `--add-dir` | allow/disallowTools | MCP | resume |
|---|---|---|---|---|---|
| Claude Code | sí | sí | sí | sí | `--resume` |
| Antigravity (`agy`) | **no** | sí | **no** | **no** | `--conversation` |
| Codex / Gemini | no | **NO** | no | no | no |

Codex y Gemini quedan afuera del orquestador por el `--add-dir`: el agente corre en un
workspace VACÍO (`_cli_workspace`, decisión X) y sus recursos se montan justamente con ese
flag. Sin él no alcanzarían nada — no es que anden peor, es que no llegan a su trabajo. En
el chat sí andan, porque ahí el cwd ES la carpeta del proyecto.
"""
import json
import os
import tempfile

from antigravity import AntigravityAdapter
from antigravity import map_model as agy_map_model
from claude import EFFORT_THINK, _self_cmd, find_claude
from claude import map_model as claude_map_model
from cli_base import _find_bin
from skills import install_agents_md, install_skills

# ---------- tools (viven acá porque son parte de "qué sabe hacer cada CLI") ----------
# tools que expone el MCP del editor (editor_mcp.py) según el permiso del recurso
MCP_FS_READ = ["fs_tree", "fs_read", "fs_grep", "sv_list"]
MCP_FS_WRITE = ["fs_write", "fs_edit", "fs_mkdir", "fs_rename", "fs_delete", "sv_save", "sv_restore"]
MCP_FS_EXEC = ["fs_exec"]
# nativas mínimas para tocar un diagrama-recurso cuando el agente está confinado:
# su único --add-dir es el subdirectorio de ESE diagrama, así que quedan encerradas ahí
CLI_DIAGRAM_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
# el shell de Claude Code son DOS tools según el sistema: `Bash` (POSIX) y `PowerShell`
# (Windows). Nombrar solo Bash dejaba el shell abierto en Windows — y al revés, la
# pre-aprobación del permiso `ejecutar` tiene que cubrir las dos.
CLI_SHELL_TOOLS = ["Bash", "PowerShell"]
CLI_DISALLOWED = ["WebFetch", "WebSearch"]

# frases con las que Claude Code contesta un tool_use rechazado por permisos (headless:
# la denegación es automática, no hay diálogo). Se buscan en minúsculas.
CLI_DENIED_PATH = ("requested permissions", "haven't granted", "have not granted",
                   "has not been granted", "permission denied", "not allowed to use",
                   "permission to use")
# un COMANDO denegado es otra cosa que un path denegado: `acceptEdits` auto-aprueba las
# ediciones de archivo pero NO los comandos, y para los compuestos el CLI parte la línea
# y marca la parte que necesita aprobación ("the following part requires approval: …")
CLI_DENIED_CMD = ("requires approval", "contains multiple operations")


def denied_text(block):
    """Si el bloque es un `tool_result` rechazado por permisos, devuelve
    `(motivo, texto)` con motivo `"cmd"` (un comando sin aprobar) o `"path"`."""
    if (block or {}).get("type") != "tool_result":
        return None
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    txt = (c if isinstance(c, str) else "").strip()
    low = txt.lower()
    if any(h in low for h in CLI_DENIED_CMD):
        return "cmd", txt
    if any(h in low for h in CLI_DENIED_PATH):
        return "path", txt
    return None


class CliTurnError(Exception):
    """El CLI reportó un error DENTRO de su stream (no un fallo de proceso). El loop de
    `orchestrator.py` la convierte en OrchError después de cerrar el proceso."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status
        self.message = message


def new_state():
    """Lo que se acumula mientras corre el turno. `feed()` lo va llenando."""
    return {"session_id": None, "texts": [], "result_text": None, "cost": 0.0}


# ============================ Claude Code ============================
class ClaudeOrch:
    """El camino que ya corría en producción, movido tal cual desde `orchestrator.py`.
    Es el único que puede el modo **confinado**: necesita `--mcp-config` (un server por
    editor) y `--allowedTools` (la whitelist), y es el único que los tiene."""

    key = "local"
    label = "Claude Code"
    bin_names = ["claude"]
    can_confine = True
    protocol = "stream-json"
    reports_cost = True
    resume_flag = "--resume"
    text_join = "\n\n"            # manda bloques completos

    def find(self):
        return find_claude()

    def install(self, work_dir):
        install_skills(work_dir)          # <workspace>/.claude/skills/

    def build(self, b, s):
        """(cmd, mcp_cfg_path). `s` es el spec neutral que arma `_cli_cmd`."""
        kw = EFFORT_THINK.get(s.get("effort") or "", "")
        msg = s["msg"] + (f"\n\n{kw}" if kw else "")
        cmd = [b, "-p", msg, "--output-format", "stream-json", "--verbose",
               "--model", claude_map_model(s.get("model")), "--permission-mode", "acceptEdits",
               "--append-system-prompt", s["system"]]
        for x in s["add_dirs"]:
            cmd += ["--add-dir", x]

        cfg = None
        if s["confinado"]:
            # whitelist: SOLO las tools del MCP (una por editor, según permiso) y, si tiene
            # diagramas cableados —o es un director, que alcanza su organigrama—, las nativas
            # de archivo, que solo llegan a sus add_dirs.
            servers, allowed = {}, []
            for name, info in (s["mcp"] or {}).items():
                servers[name] = {
                    **_self_cmd(),
                    "env": {"DMFS_URL": s["mcp_env"]["url"], "DMFS_TOKEN": s["mcp_env"]["token"],
                            "DMFS_PROJECT": info["projectId"], "DMFS_AUTH": "local"},
                }
                tools = list(MCP_FS_READ)
                if info["perm"] >= 1:
                    tools += MCP_FS_WRITE
                if info["perm"] >= 2:
                    tools += MCP_FS_EXEC
                allowed += [f"mcp__{name}__{t}" for t in tools]
            if s["add_dirs"]:
                allowed += CLI_DIAGRAM_TOOLS
            if servers:
                fd, cfg = tempfile.mkstemp(prefix=f"dmorch-mcp-{s['node_id']}-", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"mcpServers": servers}, f)
                os.chmod(cfg, 0o600)      # tiene el token del backend local
                cmd += ["--mcp-config", cfg]
            # sin tools permitidas el agente no puede hacer NADA con archivos: igual puede
            # razonar y responder, que es lo correcto para un nodo sin recursos cableados.
            cmd += ["--allowedTools", ",".join(allowed)]
            # `--allowedTools` es una lista de PRE-APROBACIÓN, no la lista de tools que
            # existen (eso es `--tools`): sin reglas de negación el confinado igual tiene el
            # shell nativo a mano. Se lo negamos explícitamente — su shell es `fs_exec`.
            cmd += ["--disallowedTools"] + CLI_DISALLOWED + CLI_SHELL_TOOLS
        else:
            # blacklist: conserva su toolbelt nativo, acotado por los --add-dir de arriba.
            # El shell es la vía de escape de los --add-dir, así que se lo damos SOLO si algún
            # recurso suyo tiene permiso `ejecutar` — y son DOS tools (Bash y PowerShell:
            # nombrar solo Bash dejaba el shell abierto en Windows).
            off = list(CLI_DISALLOWED) + ([] if s["exec_ok"] else list(CLI_SHELL_TOOLS))
            cmd += ["--disallowedTools"] + off
            if s["exec_ok"]:
                # tener la tool no alcanza: `acceptEdits` auto-aprueba las EDICIONES, no los
                # comandos, así que headless cada comando que no sea de solo-lectura se
                # auto-DENIEGA (un tester no podía ni levantar su server). El permiso
                # `ejecutar` es justamente "puede correr comandos" ⇒ se pre-aprueban.
                cmd += ["--allowedTools", ",".join(CLI_SHELL_TOOLS)]
        if s.get("session"):
            cmd += [self.resume_flag, str(s["session"])]
        return cmd, cfg

    def feed(self, obj, st, log):
        if obj.get("type") == "system" and obj.get("subtype") == "init":
            st["session_id"] = obj.get("session_id") or st["session_id"]
        elif obj.get("type") == "assistant":
            for b in (obj.get("message", {}).get("content") or []):
                if b.get("type") == "text" and b.get("text"):
                    st["texts"].append(b["text"])
                elif b.get("type") == "tool_use":
                    log(f"cli tool {b.get('name', '?')}")
        elif obj.get("type") == "user":
            # una tool DENEGADA por permisos no se ve en ningún lado: el CLI corre
            # headless, así que no hay diálogo, el modelo recibe el rechazo y suele
            # terminar preguntándole al humano que le "apruebe el permiso" (turnos
            # pagados a cambio de nada). Se registra en el timeline con el porqué.
            for b in (obj.get("message", {}).get("content") or []):
                hit = denied_text(b)
                if hit:
                    why, txt = hit
                    head = ("cli COMMAND denied (headless: no dialog to approve; a command needs a "
                            "resource with the «ejecutar» permission)" if why == "cmd" else
                            "cli permission DENIED (headless: nobody can approve it — the path is "
                            "outside its --add-dir)")
                    log(f"{head}: {txt[:200]}", full=txt)
        elif obj.get("type") == "result":
            st["session_id"] = obj.get("session_id") or st["session_id"]
            st["result_text"] = obj.get("result")
            st["cost"] = obj.get("total_cost_usd") or 0.0
            if obj.get("is_error"):
                raise CliTurnError(f"Claude Code returned an error: {st['result_text'] or '?'}")


# ============================ Antigravity (`agy`) ============================
class AgyOrch:
    """Antigravity CLI. Todo lo de acá sale del adaptador del chat, que está verificado
    contra el binario real (agy 1.1.24) — no contra documentación.

    Tres diferencias con Claude Code que son del BINARIO y no se pueden tapar:

    1. **No hay `--append-system-prompt`**: el rol del agente y las notas de sus recursos
       van como encabezado del propio prompt. El esquema de los diagramas viaja aparte, por
       `AGENTS.md`/`GEMINI.md` (`install_agents_md`), que agy sí lee.
    2. **No hay `--allowedTools`/`--disallowedTools` ni MCP**: no se puede confinar ni
       apagar tools sueltas. Por eso `can_confine = False` y el motor rechaza un nodo
       confinado con agy — una guarda que no se puede cumplir no es una guarda.
    3. **El nivel de razonamiento va pegado al id del modelo** (`…-high/-medium/-low`), así
       que NO se le suma el keyword de esfuerzo que se le manda a Claude: sería pedirle dos
       cosas distintas a la vez.
    """

    key = "local-antigravity"
    label = "Antigravity"
    bin_names = ["agy"]
    can_confine = False
    protocol = "stream-json (agy)"
    reports_cost = False           # el stream trae `usage`, pero no un costo en USD
    resume_flag = "--conversation"
    text_join = ""                 # manda PEDAZOS de la misma frase (text_delta)

    def find(self):
        # se instala en ~/.local/bin, que ya está en las rutas conocidas de _find_bin
        # (hace falta: con el backend arrancado por doble clic el PATH viene pelado)
        return _find_bin(self.bin_names)

    def install(self, work_dir):
        AntigravityAdapter().install_instructions(work_dir)   # AGENTS.md + GEMINI.md

    def build(self, b, s):
        # el system NO tiene flag propio: va arriba del mensaje, separado y rotulado para
        # que el modelo no lo confunda con la tarea
        prompt = f"{s['system']}\n\n---\n\n{s['msg']}"
        cmd = [b, "-p", prompt,
               "--output-format", "stream-json",
               "--model", agy_map_model(s.get("model")),
               "--mode", "accept-edits", "--dangerously-skip-permissions",
               "--disable-slash-commands"]       # el prompt es del usuario: que un "/" no expanda nada
        for x in s["add_dirs"]:
            cmd += ["--add-dir", x]
        # No se le puede quitar el shell (no hay --disallowedTools). `--sandbox` es lo más
        # parecido que ofrece el binario: restringe la terminal. Se usa cuando NINGÚN
        # recurso tiene permiso `ejecutar`, que es el caso en que a Claude se le saca Bash.
        if not s["exec_ok"]:
            cmd += ["--sandbox"]
        if s.get("session"):
            cmd += [self.resume_flag, str(s["session"])]
        return cmd, None

    def feed(self, obj, st, log):
        """Eventos REALES de agy (capturados del binario, ver tests/test_antigravity.py):

            {"event":"init","conversation_id":…,"init":{…}}
            {"event":"step_update","step_update":{step_type,state,text_delta?,tool_name?}}
            {"event":"result","result":{conversation_id,status,response,usage}}
        """
        ev = obj.get("event")
        if ev == "init":
            st["session_id"] = obj.get("conversation_id") or st["session_id"]
        elif ev == "step_update":
            s = obj.get("step_update") or {}
            t = s.get("step_type")
            if t == "agent_response":
                # el texto llega en PEDAZOS (varios eventos, cada uno con su trozo): se
                # acumulan todos y el resultado los concatena
                if s.get("text_delta"):
                    st["texts"].append(s["text_delta"])
            elif t == "tool" and s.get("state") == "ACTIVE":
                # solo el ACTIVE: el DONE repite el mismo tool_name y duplicaría la línea
                log(f"cli tool {s.get('tool_name') or 'tool'}")
        elif ev == "result":
            r = obj.get("result") or {}
            st["session_id"] = r.get("conversation_id") or st["session_id"]
            if r.get("status") and r["status"] != "SUCCESS":
                raise CliTurnError(r.get("response") or f"Antigravity returned {r['status']}.")
            # el texto final ya salió por los text_delta; el `response` se usa para los
            # turnos que contestan de una, sin deltas
            if not st["texts"]:
                st["result_text"] = r.get("response")


def result_of(st, cli):
    """(texto, session_id, costo) — mismo contrato para todos los CLIs.

    El separador lo pone el CLI y NO es un detalle: Claude manda bloques de texto
    completos (van con renglón en blanco entre uno y otro) y agy manda PEDAZOS de la misma
    frase (concatenarlos con \\n\\n partiría palabras al medio)."""
    return (st["result_text"] or cli.text_join.join(st["texts"]) or ""), st["session_id"], st["cost"]


ORCH_CLIS = {c.key: c for c in (ClaudeOrch(), AgyOrch())}

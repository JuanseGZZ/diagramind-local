"""Adaptador del Antigravity CLI (`agy`, de Google). El segundo CLI —después de
Claude Code— que da streaming fino y memoria de conversación nativa.

TODO LO DE ACÁ ESTÁ VERIFICADO CONTRA EL BINARIO REAL (agy 1.1.24, 2026-09-02), no
contra la documentación: el `--help`, el esquema de eventos de `--output-format
stream-json`, el catálogo de `agy models`, el resume y la lectura de AGENTS.md se
comprobaron corriéndolos. Lo que sigue es lo que se aprendió haciéndolo:

1. **`--add-dir` NO es opcional.** El `cwd` del proceso no alcanza: sin `--add-dir`,
   con el archivo sentado en su propio cwd, el agente no lo encuentra y se pone a
   buscarlo por todo el home con `run_command` (verificado). Con `--add-dir` lo
   resuelve en un turno (`find_by_name` + `view_file`).
2. **No hay `--append-system-prompt`.** Las instrucciones van EN el prompt
   (`_headless_prompt`, igual que Codex/Gemini) y el esquema de los árboles por
   `AGENTS.md` — verificado que lo lee: una regla puesta ahí aparece en la respuesta.
3. **No hay `--allowedTools`/`--disallowedTools`.** No se pueden apagar tools sueltas
   como en Claude Code, así que las tools de web/browser del CLI quedan disponibles.
   Es una diferencia real: en `claude.py` se desactivan WebFetch/WebSearch para que
   los fetch del modo object pasen por `runReq`. Acá no se puede — anotado en el doc
   20; lo más parecido que ofrece el binario es `--sandbox` (restringe la terminal).
4. **El esfuerzo viaja EN EL ID DEL MODELO** (`gemini-3.7-flash-high|medium|low`), y
   además existe un `--effort`. Mandar los dos es pedir un conflicto, así que NO se
   manda `--effort`: el catálogo del front expone los ids con su nivel y listo.
5. `--dangerously-skip-permissions` es el equivalente del `--yolo` de Gemini: el
   default es `request-review` y en headless no hay nadie para aprobar (la lección de
   la bitácora §... del orquestador: headless no pregunta, DENIEGA).
"""
import json

from runs import emit, set_status
from skills import install_agents_md
from cli_base import _headless_prompt, _find_bin, _bin_version

# Modos del chat (web) → --mode de agy. El default (request-review) no sirve en
# headless: nadie puede contestar el pedido de permiso.
MODE = {"plan": "plan", "auto-edit": "accept-edits", "auto": "accept-edits", "ask": "accept-edits"}

# `agy models` (2026-09-02). El nivel de razonamiento va pegado al id.
DEFAULT_MODEL = "gemini-3.7-flash-medium"


def map_model(m):
    """La web manda el id tal cual del catálogo (models.js `local-antigravity`), que
    son los ids REALES de `agy models`. Solo se cubre el vacío."""
    return m or DEFAULT_MODEL


def handle_event(run, obj):
    """Traduce los eventos NDJSON de agy a los eventos simples de la web.

    Forma real (capturada del binario):
      {"event":"init","conversation_id":"…","init":{model,cwd,tools,permission_mode}}
      {"event":"step_update","step_update":{conversation_id,step_index,state:"ACTIVE"|"DONE",
                                            step_type:"user_input"|"agent_response"|"tool"|
                                                      "system_message",
                                            text_delta?,tool_name?,tool_info?,usage?}}
      {"event":"result","result":{conversation_id,status:"SUCCESS"|…,response,num_turns,usage}}
    """
    ev = obj.get("event")

    if ev == "init":
        # el id de la conversación, para el --conversation del próximo turno. La clave
        # `claude_session_id` es el slot COMPARTIDO que server.py lee para cualquier
        # adaptador con supports_resume (el nombre quedó del primero que la usó).
        run["claude_session_id"] = obj.get("conversation_id")
        return

    if ev == "step_update":
        s = obj.get("step_update") or {}
        t = s.get("step_type")
        if t == "agent_response":
            # el texto llega en PEDAZOS (varios eventos, ACTIVE y DONE, cada uno con su
            # trozo distinto): se emiten todos y se concatenan del lado de la web.
            txt = s.get("text_delta")
            if txt:
                emit(run, "assistant", text=txt)
        elif t == "tool" and s.get("state") == "ACTIVE":
            # solo el ACTIVE: el DONE repite el mismo tool_name y duplicaría la línea.
            emit(run, "tool", name=s.get("tool_name") or "tool")
        return

    if ev == "result":
        r = obj.get("result") or {}
        if r.get("conversation_id"):
            run["claude_session_id"] = r["conversation_id"]
        if r.get("status") and r["status"] != "SUCCESS":
            set_status(run, "error", r.get("response") or f"Antigravity returned {r['status']}.")
            return
        # el texto final ya salió por los text_delta; solo se usa si no salió nada
        # (turnos que contestan de una sin deltas).
        txt = r.get("response")
        if txt and not any(e["kind"] == "assistant" for e in run["events"]):
            emit(run, "assistant", text=txt)
        set_status(run, "done")
        return


class AntigravityAdapter:
    key = "antigravity"; label = "Antigravity"; bin_names = ["agy"]; supports_resume = True

    def find(self):
        # se instala en ~/.local/bin, que ya está en las rutas conocidas de _find_bin
        # (hace falta: con el backend arrancado por doble clic el PATH viene pelado).
        return _find_bin(self.bin_names)

    def version(self, b):
        return _bin_version(b)

    def install_instructions(self, work_dir):
        install_agents_md(work_dir)          # verificado: agy lee AGENTS.md

    def build_cmd(self, run, b, message, work_dir, folder, focus_name, mode, model, resume,
                  effort=None, editor_target=None, editor_relay=None):
        # editor_target/relay no aplican todavía: los proyectos editor van con Claude
        # Code (v1), igual que en el adaptador de Gemini.
        cmd = [
            b, "-p", _headless_prompt(folder, focus_name, message),
            "--output-format", "stream-json",
            "--model", map_model(model),
            "--add-dir", work_dir,           # SIN ESTO no ve sus propios archivos (nota 1)
            "--disable-slash-commands",      # el prompt es del usuario: que un "/" no expanda nada
        ]
        m = MODE.get(mode, "accept-edits")
        if m == "plan":
            cmd += ["--mode", "plan"]        # planificar no escribe: no hace falta saltear permisos
        else:
            cmd += ["--mode", m, "--dangerously-skip-permissions"]
        if editor_target:
            cmd += ["--add-dir", editor_target]
        if resume:
            cmd += ["--conversation", str(resume)]
        return cmd, {}

    def parse_line(self, run, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        handle_event(run, obj)

    def finalize(self, run):
        pass

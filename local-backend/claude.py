"""Adaptador del CLI Claude Code. Único que da streaming fino (stream-json:
assistant/tool/result) y memoria de conversación nativa (--resume <sessionId>)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from runs import emit, set_status
from skills import install_skills, SYSTEM_PREAMBLE
from cli_base import _focus_note, _editor_note, _editor_relay_note

# tools del MCP fs de editores externos (editor_mcp.py) — para --allowedTools
MCP_FS_TOOLS = ["fs_tree", "fs_read", "fs_write", "fs_mkdir", "fs_rename",
                "fs_delete", "fs_grep", "fs_exec",
                "sv_save", "sv_list", "sv_restore"]


def _self_cmd():
    """Comando que re-ejecuta este backend (binario onefile o `python server.py`)."""
    if getattr(sys, "frozen", False):
        return {"command": sys.executable, "args": ["--mcp-fs"]}
    server_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    return {"command": sys.executable, "args": [server_py, "--mcp-fs"]}

# Modos del chat (web) → permission-mode de Claude Code.
PERM_MODE = {
    "auto-edit": "acceptEdits",
    "auto": "acceptEdits",
    "plan": "plan",
    "ask": "default",
}

# Esfuerzo (web) → palabra clave de razonamiento de Claude Code (más palabra = más
# presupuesto de thinking). low = razonamiento adaptativo por defecto (sin palabra).
EFFORT_THINK = {
    "low": "",
    "medium": "think",
    "high": "think hard",
    "xhigh": "think harder",
    "max": "ultrathink",
}


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


class ClaudeAdapter:
    key = "claude"; label = "Claude Code"; bin_names = ["claude"]; supports_resume = True

    def find(self):
        return find_claude()

    def version(self, b):
        return claude_version(b)

    def install_instructions(self, work_dir):
        install_skills(work_dir)

    def build_cmd(self, run, b, message, work_dir, folder, focus_name, mode, model, resume,
                  effort=None, editor_target=None, editor_relay=None):
        perm = PERM_MODE.get(mode, "acceptEdits")
        kw = EFFORT_THINK.get(effort or "", "")
        msg = message + (f"\n\n{kw}" if kw else "")   # palabra de thinking según esfuerzo
        # foco editor (doc 27): target LOCAL → acceso directo; target EXTERNO → MCP
        if editor_relay:
            focus = _editor_relay_note(folder, focus_name)
        elif editor_target:
            focus = _editor_note(folder, focus_name, editor_target)
        else:
            focus = _focus_note(folder, focus_name)
        cmd = [
            b, "-p", msg, "--output-format", "stream-json", "--verbose",
            "--model", map_model(model), "--permission-mode", perm,
            "--add-dir", work_dir,
            # WebFetch/WebSearch nativas quedan deshabilitadas: en el modo object los
            # fetches del diagrama se corren con el mecanismo `runReq` (ver skill),
            # NO con la tool genérica de Claude (no usaría el body/headers/proxy ni se vería).
            "--disallowedTools", "WebFetch", "WebSearch",
            "--append-system-prompt", SYSTEM_PREAMBLE + "\n\n" + focus,
        ]
        if editor_target:
            cmd += ["--add-dir", editor_target]
        if editor_relay:
            # editor EXTERNO: MCP server stdio (este mismo backend con --mcp-fs) con
            # las credenciales del conector. El config va a un temp file 0600 (tiene
            # el token) que se borra en finalize().
            mcp = {"mcpServers": {"dmfs": {
                **_self_cmd(),
                "env": {
                    "DMFS_URL": editor_relay["url"],
                    "DMFS_TOKEN": editor_relay["token"],
                    "DMFS_PROJECT": editor_relay["projectId"],
                },
            }}}
            fd, cfg = tempfile.mkstemp(prefix=f"dmfs-mcp-{run['id']}-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(mcp, f)
            os.chmod(cfg, 0o600)
            run["_mcp_cfg"] = cfg
            allowed = ",".join(f"mcp__dmfs__{t}" for t in MCP_FS_TOOLS)
            cmd += ["--mcp-config", cfg, "--allowedTools", allowed]
        if resume:
            cmd += ["--resume", str(resume)]
        return cmd, {}

    def parse_line(self, run, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        handle_event(run, obj)

    def finalize(self, run):
        cfg = run.pop("_mcp_cfg", None)     # borrar el config MCP (tiene el token)
        if cfg:
            try:
                os.remove(cfg)
            except OSError:
                pass

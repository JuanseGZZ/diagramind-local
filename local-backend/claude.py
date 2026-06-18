"""Adaptador del CLI Claude Code. Único que da streaming fino (stream-json:
assistant/tool/result) y memoria de conversación nativa (--resume <sessionId>)."""
import json
import os
import shutil
import subprocess

from runs import emit, set_status
from skills import install_skills, SYSTEM_PREAMBLE
from cli_base import _focus_note

# Modos del chat (web) → permission-mode de Claude Code.
PERM_MODE = {
    "auto-edit": "acceptEdits",
    "auto": "acceptEdits",
    "plan": "plan",
    "ask": "default",
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

    def build_cmd(self, run, b, message, work_dir, folder, focus_name, mode, model, resume):
        perm = PERM_MODE.get(mode, "acceptEdits")
        cmd = [
            b, "-p", message, "--output-format", "stream-json", "--verbose",
            "--model", map_model(model), "--permission-mode", perm,
            "--add-dir", work_dir,
            "--append-system-prompt", SYSTEM_PREAMBLE + "\n\n" + _focus_note(folder, focus_name),
        ]
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
        pass

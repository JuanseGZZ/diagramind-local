"""Base compartida de los adaptadores de CLI.
- helpers (_focus_note, _find_bin, _bin_version)
- run_cli(): el NÚCLEO reusado (Popen + loop de stdout + máquina de estados +
  cancelación + estado terminal). Cada adaptador (claude/codex/gemini) aporta lo
  propio (build_cmd / parse_line / finalize / install_instructions / find / ...)."""
import glob
import os
import shutil
import subprocess

from util import safe_name
from runs import set_status


# IDIOMA: estas notas van al MODELO (system prompt / prompt del CLI) → SIEMPRE en
# inglés, sin importar el idioma de la app. Ver doc 20 §L.

def _focus_note(folder, focus_name):
    return (
        f"YOU ARE WORKING IN THE FOLDER «{folder}». Its projects are listed in "
        f"./index.json and each one lives in ./<Name>/tree.json. The FOCUSED project is "
        f"«{focus_name}» → ./{safe_name(focus_name)}/tree.json: write there unless "
        f"the user points you at another project of THIS folder."
    )


def _editor_note(folder, focus_name, target):
    """Nota de foco cuando el proyecto es tipo `editor` (doc 27): el trabajo real es
    la carpeta target (accesible por --add-dir), NO el tree.json del mirror."""
    return (
        f"YOU ARE WORKING IN THE FOLDER «{folder}». The FOCUSED project, "
        f"«{focus_name}», is an EDITOR project: it opens the real folder «{target}» and "
        f"you have direct access to it. Work DIRECTLY on the files of that folder with "
        f"your normal tools (read/edit/bash). Do NOT touch "
        f"./{safe_name(focus_name)}/tree.json (it is only a {{type, target}} pointer) "
        f"and the diagramind-* schemas do NOT apply to this project."
    )


def _editor_relay_note(folder, focus_name):
    """Nota de foco para un editor cuyo target vive en un CONECTOR EXTERNO: los
    archivos NO están en esta máquina; se opera con las tools MCP mcp__dmfs__*."""
    return (
        f"YOU ARE WORKING IN THE FOLDER «{folder}». The FOCUSED project, "
        f"«{focus_name}», is an EDITOR project whose content lives on an EXTERNAL "
        f"CONNECTOR: its files are NOT on this disk. To explore and edit them "
        f"use EXCLUSIVELY the MCP tools of the «dmfs» server (mcp__dmfs__fs_tree, "
        f"fs_read, fs_write, fs_mkdir, fs_rename, fs_delete, fs_grep, fs_exec), with "
        f"paths RELATIVE to the project root. Typical flow: fs_tree to get your "
        f"bearings → fs_grep/fs_read to understand → fs_write with the COMPLETE file "
        f"to edit. fs_exec requires being an admin of the connector (if it returns 403, "
        f"don't insist). BEFORE a batch of changes save a VERSION with "
        f"mcp__dmfs__sv_save({{note}}) — it lets the user undo your work; sv_list shows the "
        f"history and sv_restore rolls back to a version ONLY if the user asks. "
        f"Do NOT use your local file tools for this "
        f"project: ./{safe_name(focus_name)}/tree.json is only a pointer and the "
        f"diagramind-* schemas do not apply."
    )


def _headless_prompt(folder, focus_name, message):
    """Prompt para los CLIs one-shot (Codex/Gemini): a diferencia de Claude Code, en
    modo -p son conversacionales y, si no se les ordena con MUCHA fuerza, describen o
    preguntan en vez de actuar. Por eso la orden imperativa va al principio Y al final."""
    focus_path = f"./{safe_name(focus_name)}/tree.json"
    return (
        "YOU ARE AN AGENT THAT EDITS FILES, NOT A CHAT. In THIS VERY TURN you have to "
        f"OPEN and MODIFY the file `{focus_path}` with your writing tools, "
        "to carry out the user's instruction. It is FORBIDDEN to finish without having "
        "edited the file, and FORBIDDEN to answer with questions or asking for confirmation.\n\n"
        + _focus_note(folder, focus_name) +
        "\n\nRULES:\n"
        f"1. Edit `{focus_path}` DIRECTLY (don't describe what you would do: DO IT).\n"
        "2. Respect the EXACT schema of its type (it is in AGENTS.md) and leave valid JSON.\n"
        "3. If the instruction is vague ('whichever you want', 'something', 'anything'), "
        "   DECIDE yourself and do it anyway. Do NOT ask.\n"
        "4. When you are done, answer in ONE single line what you changed, in the same "
        "   language the user wrote to you in.\n\n"
        f"USER INSTRUCTION: {message}\n\n"
        f"NOW edit `{focus_path}` and apply that change. Don't answer without having done it."
    )


# Carpetas de binarios globales de npm que NO están en el PATH del proceso cuando
# el backend arranca por doble clic / LaunchAgent, ni en las rutas fijas de abajo:
# nvm, fnm, volta o un `npm prefix -g` custom. Sin esto, un `claude` instalado con
# nvm existe pero el backend jura que no está instalado.
_NPM_BIN_CACHE = None


def _extra_bin_dirs():
    """Esas carpetas, resueltas una sola vez (el `npm prefix -g` spawnea node y
    /health consulta seguido). panel.py invalida el caché al instalar un CLI."""
    global _NPM_BIN_CACHE
    if _NPM_BIN_CACHE is not None:
        return _NPM_BIN_CACHE
    home = os.path.expanduser("~")
    dirs = [
        os.path.join(home, ".local", "bin"), "/usr/local/bin", "/opt/homebrew/bin",
        os.path.join(home, ".volta", "bin"),
        os.path.join(os.environ.get("APPDATA", ""), "npm"),
        os.path.join(os.environ.get("ProgramFiles", ""), "nodejs"),
    ]
    dirs += sorted(glob.glob(os.path.join(home, ".nvm", "versions", "node", "*", "bin")),
                   reverse=True)
    dirs += sorted(glob.glob(os.path.join(home, ".local", "share", "fnm", "node-versions",
                                          "*", "installation", "bin")), reverse=True)
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        try:
            out = subprocess.run([npm, "prefix", "-g"], capture_output=True, text=True,
                                 timeout=10)
            prefix = (out.stdout or "").strip()
            if prefix:
                dirs += [os.path.join(prefix, "bin"), prefix]   # unix: <prefix>/bin · win: <prefix>
        except Exception:
            pass
    _NPM_BIN_CACHE = [d for d in dirs if d and os.path.isdir(d)]
    return _NPM_BIN_CACHE


def _find_bin(names):
    """Resuelve un binario probando which() + rutas conocidas (PATH no siempre está
    cuando se arranca por doble clic / LaunchAgent)."""
    home = os.path.expanduser("~")
    cands = []
    for n in names:
        cands.append(shutil.which(n))
        cands += [
            os.path.join(home, ".local", "bin", n),
            f"/usr/local/bin/{n}", f"/opt/homebrew/bin/{n}",
            os.path.join(home, ".local", "bin", n + ".exe"),
            os.path.join(os.environ.get("APPDATA", ""), "npm", n + ".cmd"),
        ]
        for d in _extra_bin_dirs():
            cands += [os.path.join(d, n), os.path.join(d, n + ".cmd"),
                      os.path.join(d, n + ".exe")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def _bin_version(b):
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=8)
        return (out.stdout or out.stderr).strip() or None
    except Exception:
        return None


def run_cli(run, adapter, work_dir, message, mode, model, resume, focus_name, folder,
            effort=None, editor_target=None, editor_relay=None):
    """Núcleo compartido: lanza el CLI, lee stdout línea a línea (cada adaptador
    parsea lo suyo), maneja cancelación y estado terminal.
    `editor_target`: si el foco es un editor LOCAL, la carpeta real que abre
    (Claude Code le suma --add-dir). `editor_relay`: si el foco es un editor
    EXTERNO, {url, token, projectId} del conector (Claude Code lo opera vía MCP)."""
    bin_path = adapter.find()
    if not bin_path:
        set_status(run, "error",
                   f"The `{adapter.bin_names[0]}` binary ({adapter.label}) was not found on this machine.")
        return
    try:
        adapter.install_instructions(work_dir)
    except Exception:
        pass

    cmd, env_extra = adapter.build_cmd(
        run, bin_path, message, work_dir, folder, focus_name, mode, model,
        resume if adapter.supports_resume else None, effort, editor_target, editor_relay,
    )
    set_status(run, "starting")
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.Popen(
            cmd, cwd=work_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            # los CLIs emiten UTF-8; sin esto Windows usa cp1252 y rompe con acentos.
            encoding="utf-8", errors="replace", env=env,
        )
    except Exception as e:
        set_status(run, "error", f"Could not launch {adapter.label}: {e}")
        return

    run["proc"] = proc
    set_status(run, "streaming")

    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        try:
            adapter.parse_line(run, line)
        except Exception:
            pass

    proc.wait()
    stderr = (proc.stderr.read() or "").strip()
    try:
        adapter.finalize(run)
    except Exception:
        pass

    if run["status"] == "cancelled":
        return
    if proc.returncode and proc.returncode != 0 and run["status"] != "done":
        set_status(run, "error", stderr or f"{adapter.label} exited with code {proc.returncode}")
    elif run["status"] not in ("done", "error"):
        set_status(run, "done")

"""Base compartida de los adaptadores de CLI.
- helpers (_focus_note, _find_bin, _bin_version)
- run_cli(): el NÚCLEO reusado (Popen + loop de stdout + máquina de estados +
  cancelación + estado terminal). Cada adaptador (claude/codex/gemini) aporta lo
  propio (build_cmd / parse_line / finalize / install_instructions / find / ...)."""
import os
import shutil
import subprocess

from util import safe_name
from runs import set_status


def _focus_note(folder, focus_name):
    return (
        f"ESTÁS TRABAJANDO EN LA CARPETA «{folder}». Sus proyectos están en "
        f"./index.json y cada uno en ./<Nombre>/tree.json. El proyecto en FOCO es "
        f"«{focus_name}» → ./{safe_name(focus_name)}/tree.json: escribí ahí salvo que "
        f"el usuario te indique otro proyecto de ESTA carpeta."
    )


def _editor_note(folder, focus_name, target):
    """Nota de foco cuando el proyecto es tipo `editor` (doc 27): el trabajo real es
    la carpeta target (accesible por --add-dir), NO el tree.json del mirror."""
    return (
        f"ESTÁS TRABAJANDO EN LA CARPETA «{folder}». El proyecto en FOCO, "
        f"«{focus_name}», es un proyecto EDITOR: abre la carpeta real «{target}» y "
        f"tenés acceso directo a ella. Trabajá DIRECTAMENTE sobre los archivos de "
        f"esa carpeta con tus herramientas normales (leer/editar/bash). NO toques "
        f"./{safe_name(focus_name)}/tree.json (es solo un puntero {{type, target}}) "
        f"y los esquemas diagramind-* NO aplican a este proyecto."
    )


def _headless_prompt(folder, focus_name, message):
    """Prompt para los CLIs one-shot (Codex/Gemini): a diferencia de Claude Code, en
    modo -p son conversacionales y, si no se les ordena con MUCHA fuerza, describen o
    preguntan en vez de actuar. Por eso la orden imperativa va al principio Y al final."""
    focus_path = f"./{safe_name(focus_name)}/tree.json"
    return (
        "SOS UN AGENTE QUE EDITA ARCHIVOS, NO UN CHAT. En ESTE MISMO TURNO tenés que "
        f"ABRIR y MODIFICAR el archivo `{focus_path}` con tus herramientas de escritura, "
        "para cumplir la instrucción del usuario. Está PROHIBIDO terminar sin haber "
        "editado el archivo, y PROHIBIDO responder con preguntas o pidiendo confirmación.\n\n"
        + _focus_note(folder, focus_name) +
        "\n\nREGLAS:\n"
        f"1. Editá DIRECTAMENTE `{focus_path}` (no describas lo que harías: HACELO).\n"
        "2. Respetá EXACTAMENTE el esquema de su tipo (está en AGENTS.md) y dejá el JSON válido.\n"
        "3. Si la instrucción es vaga ('los que quieras', 'algo', 'lo que sea'), DECIDÍ "
        "   vos y hacelo igual. NO preguntes.\n"
        "4. Cuando termines, respondé en UNA sola línea qué cambiaste.\n\n"
        f"INSTRUCCIÓN DEL USUARIO: {message}\n\n"
        f"AHORA editá `{focus_path}` y aplicá ese cambio. No respondas sin haberlo hecho."
    )


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
            effort=None, editor_target=None):
    """Núcleo compartido: lanza el CLI, lee stdout línea a línea (cada adaptador
    parsea lo suyo), maneja cancelación y estado terminal.
    `editor_target`: si el foco es un proyecto tipo `editor`, la carpeta real que
    abre (el adaptador decide qué hacer; Claude Code le suma --add-dir)."""
    bin_path = adapter.find()
    if not bin_path:
        set_status(run, "error",
                   f"No se encontró el binario `{adapter.bin_names[0]}` ({adapter.label}) en esta máquina.")
        return
    try:
        adapter.install_instructions(work_dir)
    except Exception:
        pass

    cmd, env_extra = adapter.build_cmd(
        run, bin_path, message, work_dir, folder, focus_name, mode, model,
        resume if adapter.supports_resume else None, effort, editor_target,
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
        set_status(run, "error", f"No se pudo lanzar {adapter.label}: {e}")
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
        set_status(run, "error", stderr or f"{adapter.label} salió con código {proc.returncode}")
    elif run["status"] not in ("done", "error"):
        set_status(run, "done")

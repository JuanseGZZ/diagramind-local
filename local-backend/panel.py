"""Panel de control del backend local (doc 18 §Panel de control).

El backend dejó de ser "una consola con logs": al arrancar abre una VENTANA con
el panel (`panel_ui.PAGE`, servido por el propio server en GET /). Este módulo es
la lógica de esa ventana; el HTML vive en `panel_ui.py`.

Tres cosas hace:
  1. `status()`  → todo lo que el panel muestra en una sola request (CLIs con su
     versión y cómo se instalan, proyectos en disco, uptime, rutas).
  2. `install_cli()` → instala un CLI con su instalador NATIVO (ver abajo) y publica
     la salida línea por línea como un run normal (runs.py), así el panel la muestra
     en vivo por el mismo SSE que usa el chat.
  3. `open_panel()` → abre la ventana: Chrome/Edge con `--app=<url>` (sin barra de
     direcciones ni pestañas → se ve como app propia) y, si no hay ninguno, cae al
     navegador por defecto. Todo el shell de ventana está acá: cambiarlo por un
     webview nativo más adelante es tocar solo esta función.

Sin dependencias nuevas: stdlib pelada, igual que el resto del backend.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile

from cli_base import _bin_version, _extra_bin_dirs, _find_bin
from clis import CLIS
from runs import emit, new_run, set_status

START_TS = time.time()

# ===================== cómo se instala cada CLI =====================
# Nada de npm: cada CLI se instala con su vía NATIVA, que no pide Node ni nada.
#   · Claude Code y Codex publican script oficial de instalación (sh + ps1): bajan
#     el binario del SO y lo dejan en ~/.local/bin. Verificado contra los scripts.
#   · Gemini CLI NO tiene script: Google publica binario standalone SOLO para macOS
#     (gemini-darwin-<arch>-unsigned.zip en los releases de GitHub). En Linux/Windows
#     no queda otra que npm/Homebrew — ahí el panel no miente: no ofrece el botón,
#     muestra el comando a copiar. Ver install_plan().

SH_SCRIPT = {                     # macOS / Linux
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    # Antigravity CLI (`agy`). Binario compilado, sin Node ni Python: cae en
    # ~/.local/bin, que ya está en las rutas que prueba `_find_bin`. Verificado que la
    # URL sirve el script de verdad (application/x-sh con shebang), no la SPA del sitio
    # — un `curl | bash` que se traga un HTML es la clase de bug que no avisa.
    "antigravity": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
}
PS_SCRIPT = {                     # Windows (PowerShell)
    "claude": "irm https://claude.ai/install.ps1 | iex",
    "codex": "irm https://chatgpt.com/codex/install.ps1 | iex",
    "antigravity": "irm https://antigravity.google/cli/install.ps1 | iex",
}
GEMINI_REPO = "google-gemini/gemini-cli"
GEMINI_NPM = "npm install -g @google/gemini-cli"
GEMINI_BREW = "brew install gemini-cli"

DOCS = {
    "claude": "https://docs.claude.com/en/docs/claude-code/setup",
    "codex": "https://developers.openai.com/codex/cli/",
    "gemini": "https://github.com/google-gemini/gemini-cli",
    "antigravity": "https://antigravity.google/docs/cli/install/",
}


def find_npm():
    """npm, solo para el caso Gemini en Linux/Windows (ver install_plan)."""
    return _find_bin(["npm", "npm.cmd"])


def install_plan(key):
    """Cómo se instala ESTE cli en ESTE sistema.

    mode 'auto'   → hay botón: el backend lo instala solo (how: script|download|npm)
    mode 'manual' → no hay vía automática: el panel muestra `cmd` para copiar y `note`
    """
    win = os.name == "nt"
    if key in SH_SCRIPT:
        cmd = PS_SCRIPT[key] if win else SH_SCRIPT[key]
        return {"mode": "auto", "how": "script", "cmd": cmd,
                "label": "instalador oficial · sin Node"}
    if key == "gemini":
        if sys.platform == "darwin":
            return {"mode": "auto", "how": "download", "cmd": "",
                    "label": "binario oficial · sin Node"}
        npm = find_npm()
        if npm:
            return {"mode": "auto", "how": "npm", "cmd": GEMINI_NPM,
                    "label": "npm (Google no publica binario para este sistema)"}
        return {"mode": "manual", "how": "", "cmd": GEMINI_BREW,
                "label": "requiere Homebrew o Node",
                "note": "Google solo publica binario standalone de Gemini CLI para macOS. "
                        f"En este sistema instalalo con Homebrew ({GEMINI_BREW}) o, si "
                        f"tenés Node.js, con {GEMINI_NPM}."}
    return {"mode": "manual", "how": "", "cmd": "", "label": "", "note": "CLI desconocido"}


# ===================== estado que pinta el panel =====================

# El panel pide el status cada 5s y sacar la versión de cada CLI cuesta un
# subproceso (`<cli> --version`): sin caché serían 3 procesos cada 5 segundos
# mientras la ventana esté abierta. Se invalida al instalar algo.
_CLIS_CACHE = None
_CLIS_TS = 0.0
_CLIS_TTL = 20.0


def _clis():
    global _CLIS_CACHE, _CLIS_TS
    if _CLIS_CACHE is not None and time.time() - _CLIS_TS < _CLIS_TTL:
        return _CLIS_CACHE
    out = []
    for a in CLIS.values():
        b = a.find()
        out.append({
            "key": a.key, "label": a.label, "available": bool(b),
            "version": a.version(b) if b else None, "resume": a.supports_resume,
            "docs": DOCS.get(a.key, ""), "install": install_plan(a.key),
        })
    _CLIS_CACHE, _CLIS_TS = out, time.time()
    return out


def _uptime():
    s = int(time.time() - START_TS)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def list_projects(root):
    """Los proyectos que hay EN DISCO, con la estructura de 2 niveles del mirror
    (<root>/<carpeta>/<proyecto>/tree.json). Lee el tipo y la fecha del tree.json;
    un archivo corrupto no rompe el listado (queda con tipo '?')."""
    folders, count = [], 0
    if not os.path.isdir(root):
        return {"folders": [], "count": 0}
    for fname in sorted(os.listdir(root)):
        fdir = os.path.join(root, fname)
        if not os.path.isdir(fdir) or fname.startswith("."):
            continue
        projects = []
        for pname in sorted(os.listdir(fdir)):
            fp = os.path.join(fdir, pname, "tree.json")
            if not os.path.exists(fp):
                continue
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    ptype = (json.load(f) or {}).get("type") or "?"
            except Exception:
                ptype = "?"
            try:
                mod = time.strftime("%d/%m/%Y %H:%M", time.localtime(os.path.getmtime(fp)))
            except OSError:
                mod = ""
            projects.append({"name": pname, "type": ptype, "modified": mod})
        # solo carpetas nuestras (con index.json o con proyectos adentro)
        if projects or os.path.exists(os.path.join(fdir, "index.json")):
            folders.append({"name": fname, "projects": projects})
            count += len(projects)
    return {"folders": folders, "count": count}


def status(*, name, version, port, token, root, base, token_file, auto_stop):
    """Todo lo que muestra el panel, en una sola request (se repite cada 5s).
    `auto_stop`: si cerrar la ventana apaga el backend (arrancó CON panel)."""
    return {
        "name": name, "version": version, "port": port,
        "url": f"http://127.0.0.1:{port}",
        "uptime": _uptime(),
        "token": token, "tokenPath": token_file,
        "root": root, "base": base,
        "autoStop": bool(auto_stop),
        "clis": _clis(),
        "projects": list_projects(root),
    }


# ===================== instalación rápida de un CLI =====================

def install_cli(key):
    """Instala un CLI en background y devuelve el run: el panel lee su salida en
    vivo por el MISMO SSE de los chats (/panel/install/stream)."""
    run = new_run()
    threading.Thread(target=_install_worker, args=(run, key), daemon=True).start()
    return run


def _install_worker(run, key):
    adapter = CLIS.get(key)
    plan = install_plan(key)
    if not adapter or plan["mode"] != "auto":
        set_status(run, "error", plan.get("note") or f"CLI desconocido: {key}")
        return

    set_status(run, "streaming")
    try:
        if plan["how"] == "download":
            ok = _install_gemini_macos(run)
        else:
            ok = _run_install_cmd(run, plan["cmd"])
    except Exception as e:                       # que un error raro no deje el run colgado
        set_status(run, "error", f"{type(e).__name__}: {e}")
        return

    if run["status"] == "cancelled" or not ok:
        return

    # el binario recién instalado puede haber caído en una carpeta que no teníamos
    _bust_bin_cache()
    b = adapter.find()
    ver = adapter.version(b) if b else None
    emit(run, "log", line=f"✓ {adapter.label} instalado" + (f" · {ver}" if ver else ""))
    if not b:
        emit(run, "log", line="⚠ El instalador terminó OK pero no encuentro el binario. "
                              "Reiniciá el backend y volvé a mirar.")
    emit(run, "log", line=f"La primera vez que lo uses te va a pedir iniciar sesión: "
                          f"abrí una terminal y corré  {adapter.bin_names[0]}")
    set_status(run, "done")


def _run_install_cmd(run, cmd):
    """Corre el instalador oficial (sh en mac/linux, PowerShell en Windows) y va
    emitiendo su salida. Devuelve True si terminó bien."""
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    else:
        argv = ["/bin/sh", "-c", cmd]
    emit(run, "log", line=f"$ {cmd}")
    emit(run, "log", line="(baja el binario oficial; puede tardar un minuto)")

    env = dict(os.environ)
    env["CODEX_NON_INTERACTIVE"] = "true"        # el instalador de Codex pregunta si no
    env["PATH"] = os.pathsep.join(_extra_bin_dirs() + [env.get("PATH", "")])

    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, env=env)
    except Exception as e:
        set_status(run, "error", f"No se pudo ejecutar el instalador: {e}")
        return False

    run["proc"] = proc
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            emit(run, "log", line=line)
    code = proc.wait()
    if run["status"] == "cancelled":
        return False
    if code != 0:
        set_status(run, "error", f"El instalador terminó con código {code}. "
                                 f"Mirá el detalle arriba; también podés correr el comando "
                                 f"a mano en una terminal.")
        return False
    return True


# --- Gemini CLI en macOS: binario del release de GitHub (Google no da script) ---

def _install_gemini_macos(run):
    arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x64"
    asset = f"gemini-darwin-{arch}-unsigned.zip"
    dest_dir = os.path.join(os.path.expanduser("~"), ".local", "bin")
    dest = os.path.join(dest_dir, "gemini")

    emit(run, "log", line=f"Buscando el último release de {GEMINI_REPO} ({asset})…")
    url = _github_asset_url(GEMINI_REPO, asset)
    if not url:
        set_status(run, "error", f"No encontré el binario {asset} en los releases de "
                                 f"{GEMINI_REPO}. Probá con Homebrew: {GEMINI_BREW}")
        return False

    tmp = tempfile.mkdtemp(prefix="dm-gemini-")
    zpath = os.path.join(tmp, asset)
    os.makedirs(dest_dir, exist_ok=True)
    try:
        emit(run, "log", line="Descargando…")
        _download(run, url, zpath)
        emit(run, "log", line="Extrayendo…")
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist() if n.rstrip("/").endswith("gemini")]
            if not names:
                set_status(run, "error", "El zip del release no trae el binario `gemini`.")
                return False
            with z.open(names[0]) as src, open(dest + ".part", "wb") as out:
                shutil.copyfileobj(src, out)
        os.chmod(dest + ".part", 0o755)
        os.replace(dest + ".part", dest)
        # El asset es "unsigned" y en Apple Silicon el sistema mata (SIGKILL) todo
        # binario sin firma: una firma ad-hoc local alcanza para que corra.
        emit(run, "log", line="Firmando el binario (ad-hoc)…")
        r = subprocess.run(["codesign", "--force", "-s", "-", dest],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            emit(run, "log", line="⚠ No se pudo firmar: " + (r.stderr or "").strip()[:200])
            emit(run, "log", line="  Si al usarlo dice «killed», instalá las Command Line "
                                  "Tools (xcode-select --install) y reinstalá, o usá "
                                  + GEMINI_BREW)
        emit(run, "log", line=f"Instalado en {dest}")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _github_asset_url(repo, asset):
    """URL del asset en el último release ESTABLE (los nightly no cuentan)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"User-Agent": "diagramind-local", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    for a in data.get("assets", []):
        if a.get("name") == asset:
            return a.get("browser_download_url")
    return None


def _download(run, url, dest):
    """Descarga con progreso (los binarios son de decenas de MB)."""
    req = urllib.request.Request(url, headers={"User-Agent": "diagramind-local"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = last = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if got - last >= 10 << 20:            # un log cada 10 MB
                last = got
                pct = f" ({got * 100 // total}%)" if total else ""
                emit(run, "log", line=f"  {got // (1 << 20)} MB{pct}")


def _bust_bin_cache():
    """Después de instalar, el binario puede estar en una carpeta que todavía no
    conocíamos y la versión cacheada quedó vieja: se recalcula todo."""
    global _CLIS_CACHE
    import cli_base
    cli_base._NPM_BIN_CACHE = None
    _CLIS_CACHE = None


# ===================== la ventana =====================
# Chrome/Edge con --app=<url> da una ventana SIN barra de direcciones ni pestañas:
# se ve como una app propia y no cuesta ninguna dependencia. Si no hay ninguno,
# caemos a una pestaña del navegador por defecto (la UI es la misma).

def _chrome_binaries():
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        return [
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        ]
    import shutil
    names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
             "microsoft-edge", "brave-browser"]
    return [shutil.which(n) for n in names]


def open_panel(url):
    """Abre el panel. Devuelve 'app' (ventana propia), 'tab' o None."""
    for b in _chrome_binaries():
        if not b or not os.path.exists(b):
            continue
        try:
            subprocess.Popen(
                [b, f"--app={url}", "--window-size=880,900", "--no-first-run",
                 "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return "app"
        except Exception:
            continue
    try:
        return "tab" if webbrowser.open(url) else None
    except Exception:
        return None

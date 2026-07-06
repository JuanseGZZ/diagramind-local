# Modo editor (doc 27 de Diagramer) — lado LOCAL.
# Contrato UNIFICADO con el conector externo (external-backend/fs.py): target por
# projectId + operaciones fs confinadas al target (realpath-prefijo, symlinks
# resueltos → no se puede salir del target). Rutas siempre RELATIVAS al target.
# Los targets se persisten en <app_dir>/editor_targets.json.
# Cada operación devuelve (status_code, payload) para que server.py haga _json().

import fnmatch
import json
import os
import shutil
import subprocess

MAX_READ = 2 * 1024 * 1024
MAX_ENTRIES = 500
MAX_MATCHES = 200
GREP_FILE_CAP = 1 * 1024 * 1024
EXEC_TIMEOUT = 60


def _targets_path(app_dir):
    return os.path.join(app_dir, "editor_targets.json")


def _read_targets(app_dir):
    try:
        with open(_targets_path(app_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_target(app_dir, pid):
    return _read_targets(app_dir).get(pid or "")


def set_target(app_dir, pid, path):
    if not pid or not path:
        return 400, {"error": "faltan projectId o path"}
    target = os.path.realpath(os.path.expanduser(path))
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        return 400, {"error": str(e)}
    t = _read_targets(app_dir)
    t[pid] = target
    with open(_targets_path(app_dir), "w", encoding="utf-8") as f:
        json.dump(t, f)
    return 200, {"path": target}


def _resolve(app_dir, pid, rel):
    """(err, base, abs). err = (code, payload) si no hay target o la ruta escapa."""
    target = get_target(app_dir, pid)
    if not target:
        return (400, {"error": "editor target not set"}), None, None
    base = os.path.realpath(target)
    p = os.path.realpath(os.path.join(base, rel or "."))
    if p != base and not p.startswith(base + os.sep):
        return (400, {"error": "path escapes target"}), None, None
    return None, base, p


def fs_tree(app_dir, pid, rel):
    err, _, p = _resolve(app_dir, pid, rel)
    if err:
        return err
    if not os.path.isdir(p):
        return 404, {"error": "not a directory"}
    try:
        names = sorted(os.listdir(p))
    except OSError as e:
        return 400, {"error": str(e)}
    out = []
    for name in names[:MAX_ENTRIES]:
        fp = os.path.join(p, name)
        is_dir = os.path.isdir(fp)
        size = 0 if is_dir else (os.path.getsize(fp) if os.path.isfile(fp) else 0)
        out.append({"name": name, "dir": is_dir, "size": size})
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return 200, {"entries": out, "truncated": len(names) > MAX_ENTRIES}


def fs_read(app_dir, pid, rel):
    err, _, p = _resolve(app_dir, pid, rel)
    if err:
        return err
    if not os.path.isfile(p):
        return 404, {"error": "file not found"}
    with open(p, "rb") as f:
        raw = f.read(MAX_READ + 1)
    truncated = len(raw) > MAX_READ
    try:
        content = raw[:MAX_READ].decode("utf-8")
    except UnicodeDecodeError:
        return 200, {"binary": True, "size": os.path.getsize(p)}
    return 200, {"content": content, "truncated": truncated}


def fs_write(app_dir, pid, rel, content):
    err, _, p = _resolve(app_dir, pid, rel)
    if err:
        return err
    if os.path.isdir(p):
        return 400, {"error": "path is a directory"}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content if content is not None else "")
    return 200, {"ok": True}


def fs_mkdir(app_dir, pid, rel):
    err, _, p = _resolve(app_dir, pid, rel)
    if err:
        return err
    os.makedirs(p, exist_ok=True)
    return 200, {"ok": True}


def fs_delete(app_dir, pid, rel):
    err, base, p = _resolve(app_dir, pid, rel)
    if err:
        return err
    if p == base:
        return 400, {"error": "cannot delete the target root"}
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.exists(p):
        os.remove(p)
    return 200, {"ok": True}


def fs_rename(app_dir, pid, rel_from, rel_to):
    """Renombra/mueve un archivo o directorio DENTRO del target (os.rename: atómico,
    sirve para dirs y binarios). No pisa destinos existentes."""
    err, base, src = _resolve(app_dir, pid, rel_from)
    if err:
        return err
    err, _, dst = _resolve(app_dir, pid, rel_to)
    if err:
        return err
    if src == base:
        return 400, {"error": "cannot rename the target root"}
    if not os.path.exists(src):
        return 404, {"error": "source not found"}
    if os.path.exists(dst):
        return 409, {"error": "destination already exists"}
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        return 400, {"error": str(e)}
    return 200, {"ok": True}


def fs_grep(app_dir, pid, q, glob):
    err, base, _ = _resolve(app_dir, pid, ".")
    if err:
        return err
    if not q:
        return 400, {"error": "falta q"}
    matches = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for name in files:
            fp = os.path.join(root, name)
            rel = os.path.relpath(fp, base)
            if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(name, glob):
                continue
            try:
                if os.path.getsize(fp) > GREP_FILE_CAP:
                    continue
                with open(fp, "r", encoding="utf-8", errors="strict") as f:
                    for i, line in enumerate(f, 1):
                        if q in line:
                            matches.append({"path": rel, "line": i, "text": line.rstrip()[:300]})
                            if len(matches) >= MAX_MATCHES:
                                return 200, {"matches": matches, "truncated": True}
            except (OSError, UnicodeDecodeError):
                continue
    return 200, {"matches": matches, "truncated": False}


def fs_exec(app_dir, pid, cmd):
    err, base, _ = _resolve(app_dir, pid, ".")
    if err:
        return err
    if not cmd:
        return 400, {"error": "falta cmd"}
    try:
        r = subprocess.run(cmd, shell=True, cwd=base, capture_output=True,
                           text=True, timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 200, {"code": -1, "stdout": "", "stderr": "timeout (%ss)" % EXEC_TIMEOUT}
    return 200, {"code": r.returncode, "stdout": r.stdout[-20000:], "stderr": r.stderr[-20000:]}

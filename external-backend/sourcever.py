"""Source Versions del modo editor (doc 27, fase 4): un "git propio" POR PROYECTO.

Snapshots de TODOS los archivos del target, guardados en el directorio del
proyecto dentro de la carpeta de proyectos del conector
(`<root>/<Carpeta>/<Proyecto>/source-versions/`) — así viajan con el proyecto y
se borran en cascada cuando el proyecto se elimina. Cada versión guarda FECHA y
AUTOR (usuario o IA). Restaurar hace primero un snapshot de seguridad.

Módulo de LÓGICA PURA compartido: existe idéntico en local-backend/ y en
external-backend/ (mismo criterio que editorfs.py ↔ fs.py). Si tocás uno,
copiá el archivo al otro.

Errores → SvError(code, msg); cada backend lo traduce a su framework.

Layout de `sv_dir`:
    index.json          → [ {id, ts(ms), author, note, count} ]  (nuevo → viejo)
    <id>/…              → copia de los archivos del target (rutas relativas)
"""
import difflib
import json
import os
import shutil
import time

MAX_FILE = 2 * 1024 * 1024          # archivos más grandes NO entran al snapshot
MAX_FILES = 2000                    # tope de archivos por snapshot
SKIP_DIRS = {".git", "node_modules", "__pycache__", "source-versions", ".venv", "venv"}


class SvError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------------- helpers ----------------

def _index_path(sv_dir):
    return os.path.join(sv_dir, "index.json")


def _read_index(sv_dir):
    try:
        with open(_index_path(sv_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_index(sv_dir, idx):
    os.makedirs(sv_dir, exist_ok=True)
    with open(_index_path(sv_dir), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False)


def _walk(target):
    """rel → abs de los archivos versionables del target (aplica SKIP_DIRS y
    MAX_FILE). SvError 400 si el proyecto excede MAX_FILES."""
    out = {}
    base = os.path.realpath(target)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            fp = os.path.join(root, name)
            try:
                if not os.path.isfile(fp) or os.path.getsize(fp) > MAX_FILE:
                    continue
            except OSError:
                continue
            out[os.path.relpath(fp, base)] = fp
            if len(out) > MAX_FILES:
                raise SvError(400, f"el proyecto tiene más de {MAX_FILES} archivos versionables")
    return out


def _snap_files(sv_dir, vid):
    """rel → abs de los archivos guardados en un snapshot."""
    base = os.path.join(sv_dir, vid)
    out = {}
    for root, _dirs, files in os.walk(base):
        for name in files:
            fp = os.path.join(root, name)
            out[os.path.relpath(fp, base)] = fp
    return out


def _same(fp_a, fp_b):
    try:
        if os.path.getsize(fp_a) != os.path.getsize(fp_b):
            return False
        with open(fp_a, "rb") as a, open(fp_b, "rb") as b:
            while True:
                ca, cb = a.read(65536), b.read(65536)
                if ca != cb:
                    return False
                if not ca:
                    return True
    except OSError:
        return False


def _read_text(fp):
    """Contenido como texto, o None si es binario / no existe."""
    try:
        with open(fp, "rb") as f:
            raw = f.read(MAX_FILE)
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _get(sv_dir, vid):
    for m in _read_index(sv_dir):
        if m["id"] == vid:
            return m
    raise SvError(404, f"no existe la versión {vid}")


# ---------------- API ----------------

def sv_save(sv_dir, target, author, note):
    """Snapshot de todos los archivos del target. Devuelve la metadata."""
    files = _walk(target)
    vid = f"v{int(time.time() * 1000):x}"
    dest = os.path.join(sv_dir, vid)
    if os.path.exists(dest):                     # colisión de ms: sufijo
        vid += "b"
        dest = os.path.join(sv_dir, vid)
    for rel, fp in files.items():
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copyfile(fp, out)
    os.makedirs(dest, exist_ok=True)             # proyecto vacío: snapshot vacío válido
    meta = {"id": vid, "ts": int(time.time() * 1000), "author": author or "usuario",
            "note": note or "", "count": len(files)}
    idx = _read_index(sv_dir)
    idx.insert(0, meta)
    _write_index(sv_dir, idx)
    return meta


def sv_list(sv_dir):
    return _read_index(sv_dir)


def sv_status(sv_dir, target):
    """Cambios del target vs. el ÚLTIMO snapshot: [{path, state}] con
    state ∈ added|modified|deleted. Sin snapshots → todo `added` contra nada."""
    idx = _read_index(sv_dir)
    last = idx[0] if idx else None
    cur = _walk(target)
    snap = _snap_files(sv_dir, last["id"]) if last else {}
    changes = []
    for rel in sorted(set(cur) | set(snap)):
        if rel not in snap:
            changes.append({"path": rel, "state": "added"})
        elif rel not in cur:
            changes.append({"path": rel, "state": "deleted"})
        elif not _same(cur[rel], snap[rel]):
            changes.append({"path": rel, "state": "modified"})
    return {"versionId": last["id"] if last else None, "changes": changes}


def sv_diff(sv_dir, target, vid, path):
    """Diff unificado de UN archivo: snapshot (vid, default el último) vs. actual."""
    if not path:
        raise SvError(400, "falta path")
    idx = _read_index(sv_dir)
    if vid:
        _get(sv_dir, vid)
    elif idx:
        vid = idx[0]["id"]
    old_fp = os.path.join(sv_dir, vid, path) if vid else None
    new_fp = os.path.join(os.path.realpath(target), path)
    old = _read_text(old_fp) if old_fp and os.path.isfile(old_fp) else None
    new = _read_text(new_fp) if os.path.isfile(new_fp) else None
    if old is None and new is None:
        if (old_fp and os.path.isfile(old_fp)) or os.path.isfile(new_fp):
            return {"diff": "(archivo binario — sin diff)", "binary": True}
        raise SvError(404, "el archivo no existe ni en el snapshot ni en el target")
    lines = difflib.unified_diff(
        (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
        fromfile=f"{vid or '(sin versión)'}/{path}", tofile=f"actual/{path}",
    )
    return {"diff": "".join(lines) or "(sin cambios)"}


def sv_restore(sv_dir, target, vid, author):
    """Vuelve el target al snapshot `vid`. ANTES hace un snapshot de seguridad
    (para poder deshacer el propio restore). Devuelve {restored, pre}."""
    _get(sv_dir, vid)
    pre = sv_save(sv_dir, target, author, f"(auto) antes de restaurar {vid}")
    base = os.path.realpath(target)
    snap = _snap_files(sv_dir, vid)
    cur = _walk(target)
    for rel, fp in snap.items():                  # copiar lo del snapshot
        out = os.path.join(base, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copyfile(fp, out)
    for rel, fp in cur.items():                   # borrar lo que el snapshot no tiene
        if rel not in snap:
            try:
                os.remove(fp)
            except OSError:
                pass
    return {"restored": vid, "pre": pre}

"""Blobs del modo `documents` (doc 30, fase 3) — store content-addressed.

El nombre del archivo en disco ES el sha256 de su contenido:

    <projects>/<Carpeta>/<Proyecto>/documents/<hash>

Los METADATOS (nombre, mime, carpeta virtual) NO viven acá: van en el manifiesto
del tree.json, que ya viaja por los mirrors normales. Acá solo bytes, y el hash
es a la vez id, dedupe y VERIFICACIÓN de integridad: en cada `put` se recalcula
el sha256 y se compara con el que declara el cliente (si no coincide → 400, no
se escribe). Así los mirrors se mantienen sanos sin confiar en el emisor.

Lógica pura (sin HTTP) para poder espejarla en el conector externo (fase 4), igual
que editorfs.py / sourcever.py.
"""

import hashlib
import os
import shutil

DOCS_DIRNAME = "documents"
BYNAME_DIRNAME = "by-name"             # vista legible (hardlinks) para la IA y el usuario
MAX_BLOB = 200 * 1024 * 1024          # 200 MB por blob (la web corta antes, en 100)
HASH_LEN = 64                          # sha256 hex


def docs_dir(project_dir):
    return os.path.join(project_dir, DOCS_DIRNAME)


def valid_hash(h):
    """El hash viene del cliente y se usa como NOMBRE DE ARCHIVO: validarlo es lo
    que impide un path traversal (`../..`) por el nombre del blob."""
    if not isinstance(h, str) or len(h) != HASH_LEN:
        return False
    return all(c in "0123456789abcdef" for c in h.lower())


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def blob_path(project_dir, h):
    return os.path.join(docs_dir(project_dir), h.lower())


# ===================== operaciones =====================

def docs_list(project_dir):
    """Hashes que EXISTEN en el disco (lo que el mirror ya tiene) + tamaños."""
    d = docs_dir(project_dir)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for n in names:
        if not valid_hash(n):
            continue                    # ignorar cualquier cosa que no sea un blob
        try:
            out.append({"hash": n, "size": os.path.getsize(os.path.join(d, n))})
        except OSError:
            pass
    return 200, {"blobs": out}


def docs_get(project_dir, h):
    """Bytes de un blob. Devuelve (code, body_dict) con body['bytes'] en el caso OK
    para que el server lo mande crudo."""
    if not valid_hash(h):
        return 400, {"error": "hash inválido"}
    p = blob_path(project_dir, h)
    if not os.path.isfile(p):
        return 404, {"error": "blob no encontrado"}
    try:
        with open(p, "rb") as f:
            return 200, {"bytes": f.read()}
    except OSError as e:
        return 500, {"error": str(e)}


def docs_put(project_dir, h, data):
    """Guarda un blob VERIFICANDO que su sha256 sea el declarado. Idempotente: si
    ya está (mismo hash = mismo contenido), no reescribe."""
    if not valid_hash(h):
        return 400, {"error": "hash inválido"}
    if not isinstance(data, (bytes, bytearray)) or not data:
        return 400, {"error": "cuerpo vacío"}
    if len(data) > MAX_BLOB:
        return 413, {"error": f"blob demasiado grande (máx {MAX_BLOB // (1024 * 1024)} MB)"}
    real = sha256_bytes(data)
    if real != h.lower():
        # la verificación del doc 30 decisión D: el contenido no es lo que dice ser
        return 400, {"error": "el contenido no coincide con el hash", "expected": h.lower(), "got": real}
    d = docs_dir(project_dir)
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, real)
        if os.path.isfile(p) and os.path.getsize(p) == len(data):
            return 200, {"ok": True, "hash": real, "size": len(data), "deduped": True}
        tmp = p + ".part"                # escritura atómica: no dejar blobs a medias
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return 200, {"ok": True, "hash": real, "size": len(data)}
    except OSError as e:
        return 500, {"error": str(e)}


def docs_delete(project_dir, h):
    if not valid_hash(h):
        return 400, {"error": "hash inválido"}
    p = blob_path(project_dir, h)
    try:
        if os.path.isfile(p):
            os.remove(p)
        return 200, {"ok": True}
    except OSError as e:
        return 500, {"error": str(e)}


def _safe_component(part):
    """Un tramo de ruta seguro: sin separadores, sin `..`, sin vacíos."""
    part = str(part or "").replace("\\", "/").split("/")[-1].strip()
    if part in ("", ".", ".."):
        return ""
    return "".join(c for c in part if c not in '\0:*?"<>|')


def docs_link_names(project_dir, entries):
    """Reconstruye `<documents>/by-name/` : una vista LEGIBLE de la biblioteca —
    los nombres reales (con extensión) y las carpetas virtuales del manifiesto,
    apuntando a los blobs con **hardlinks** (mismo filesystem → no ocupan espacio).

    Es lo que hace usable la biblioteca para un agente que mira el disco (Claude
    Code con `--add-dir`): `documents/<hash>` no dice nada, `by-name/papers/
    informe.pdf` sí. Se REGENERA entera en cada sync, así nunca queda desfasada.
    """
    root = os.path.join(docs_dir(project_dir), BYNAME_DIRNAME)
    shutil.rmtree(root, ignore_errors=True)
    made = 0
    for e in entries or []:
        h = str((e or {}).get("hash") or "").lower()
        if not valid_hash(h):
            continue
        src = blob_path(project_dir, h)
        if not os.path.isfile(src):
            continue                      # todavía no se subió: no hay a qué linkear
        name = _safe_component((e or {}).get("name") or h[:12])
        if not name:
            continue
        parts = [p for p in (_safe_component(x) for x in str((e or {}).get("dir") or "").split("/")) if p]
        dest_dir = os.path.join(root, *parts)
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            if os.path.exists(dest):      # dos docs con el mismo nombre en la misma carpeta
                stem, ext = os.path.splitext(name)
                dest = os.path.join(dest_dir, f"{stem}-{h[:6]}{ext}")
            try:
                os.link(src, dest)        # hardlink: sin duplicar bytes
            except OSError:
                shutil.copy2(src, dest)   # otro filesystem / FS sin links: copia
            made += 1
        except OSError:
            pass
    return made


def docs_gc(project_dir, keep_hashes):
    """Borra del disco los blobs que el manifiesto ya no referencia. `keep_hashes`
    es la lista de hashes del tree.json (la web la manda al sincronizar)."""
    keep = {h.lower() for h in (keep_hashes or []) if valid_hash(h)}
    d = docs_dir(project_dir)
    removed = []
    try:
        names = os.listdir(d)
    except OSError:
        return 200, {"ok": True, "removed": []}
    for n in names:
        if valid_hash(n) and n.lower() not in keep:
            try:
                os.remove(os.path.join(d, n))
                removed.append(n)
            except OSError:
                pass
    return 200, {"ok": True, "removed": removed}

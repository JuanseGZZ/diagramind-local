"""Cuota de disco POR CARPETA (SaaS free compartido — doc 26 §3, riesgo 1).

- `DMC_FOLDER_QUOTA_MB` (0/ausente = sin cuota; en las instancias free: 30). Aplica a
  TODAS las carpetas de la instancia y a cualquier usuario (admin incluido).
- El chequeo es ANTES de escribir: uso actual del dir de la carpeta − lo que se
  reemplaza + lo que entra. `.git` no cuenta (vive en el root, no en la carpeta),
  pero los `source-versions/` de los proyectos sí (son disco real del usuario).
- Puntos de aplicación: `write_tree` (WS edit + MCP write_project), `/fs/write` y
  `fs_write` MCP, y `/sv/save`. Los borrados siempre pasan (liberan espacio).
"""

import os

import store
from config import FOLDER_QUOTA_BYTES, REPO_ROOT


class QuotaExceeded(Exception):
    def __init__(self):
        mb = FOLDER_QUOTA_BYTES // (1024 * 1024)
        super().__init__(f"folder quota exceeded ({mb} MB)")


def folder_usage(dirname: str) -> int:
    """Bytes usados por el dir de la carpeta (recursivo)."""
    total = 0
    for root, _dirs, files in os.walk(REPO_ROOT / dirname):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def ensure_room(folder_id: str, incoming: int, replaces=None) -> None:
    """Valida que escribir `incoming` bytes (pisando el archivo `replaces`, si existe)
    no pase la cuota de la carpeta. QuotaExceeded si se pasa; no-op sin cuota."""
    if not FOLDER_QUOTA_BYTES:
        return
    f = store.get_folder(folder_id)
    if not f:
        return
    old = 0
    if replaces is not None and os.path.isfile(replaces):
        old = os.path.getsize(replaces)
    if folder_usage(f["dirname"]) - old + incoming > FOLDER_QUOTA_BYTES:
        raise QuotaExceeded()


def folder_of_path(abs_path: str) -> str | None:
    """folder_id si el path cae dentro del dir de una carpeta del repo, o None
    (p.ej. un editor target fuera del root — ahí la cuota no aplica)."""
    if not FOLDER_QUOTA_BYTES:
        return None
    root = os.path.realpath(str(REPO_ROOT))
    p = os.path.realpath(abs_path)
    if not p.startswith(root + os.sep):
        return None
    first = os.path.relpath(p, root).split(os.sep)[0]
    f = store.get_folder_by_dirname(first)
    return f["id"] if f else None

"""Lectura/escritura del `tree.json` de un proyecto.

El proyecto se referencia por su **id canónico** (asignado por el conector, ver
`store.py`); acá se resuelve a la ruta en disco `<root>/<folder>/<project>/tree.json`.
El `tree.json` se persiste **verbatim** (last-write-wins). No se commitea: el working
tree es la copia viva; "Guardar" = commit explícito llega en el paso de versionado (§7).
"""

import json
from pathlib import Path

import quota
from config import REPO_ROOT
from store import get_project, project_reldir


def tree_path(project_id: str) -> Path | None:
    rel = project_reldir(project_id)
    return (REPO_ROOT / rel / "tree.json") if rel else None


def read_tree(project_id: str) -> str | None:
    """JSON del árbol (string), o None si el proyecto no existe o aún no tiene tree."""
    p = tree_path(project_id)
    if not p or not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def write_tree(project_id: str, tree_json: str) -> None:
    """Persiste el árbol verbatim. ValueError si el proyecto no existe o el JSON es
    inválido; QuotaExceeded si la carpeta se pasa de la cuota (doc 26 §3)."""
    p = tree_path(project_id)
    if not p:
        raise ValueError("project not found")
    json.loads(tree_json)                    # rechaza JSON inválido antes de escribir
    quota.ensure_room(get_project(project_id)["folder_id"],
                      len(tree_json.encode("utf-8")), replaces=p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tree_json, encoding="utf-8")

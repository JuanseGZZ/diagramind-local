"""Lectura/escritura del `tree.json` de un proyecto.

El proyecto se referencia por su **id canónico** (asignado por el conector, ver
`store.py`); acá se resuelve a la ruta en disco `<root>/<folder>/<project>/tree.json`.
El `tree.json` se persiste **verbatim** (last-write-wins). No se commitea: el working
tree es la copia viva; "Guardar" = commit explícito llega en el paso de versionado (§7).
"""

import json
from pathlib import Path

import config
import quota
from config import REPO_ROOT
from store import get_project, project_reldir

# Tipos que NO se permiten en una instancia COMPARTIDA (el free de DiagraMinder, que
# corre con `DMC_SHARED=1`). El Editor y el IA Orchestrator operan sobre archivos
# reales y necesitan EJECUTAR comandos, y `DMC_SHARED` implica `DMC_DISABLE_EXEC`:
# ahí no pueden funcionar, así que no se guardan.
#
# El chequeo va acá y no en la UI a propósito: `write_tree` es el ÚNICO punto por el
# que pasa toda escritura de árbol —el WebSocket del editor colaborativo y las tools
# MCP— así que cerrarlo acá lo cierra para todos. Una guarda en el cliente se saltea
# con un `fetch`; ésta no.
SHARED_BLOCKED_TYPES = {"editor", "orchestrator"}


class TypeNotAllowed(Exception):
    """El tipo de ese árbol no se puede alojar en esta instancia (ver arriba)."""

    def __init__(self, tree_type: str):
        self.tree_type = tree_type
        super().__init__(
            f"'{tree_type}' projects are not available on this shared connector: "
            "they need to run commands, which is disabled here. Use the local app "
            "or your own connector.")


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
    inválido; QuotaExceeded si la carpeta se pasa de la cuota (doc 26 §3);
    TypeNotAllowed si el tipo no se puede alojar en esta instancia."""
    p = tree_path(project_id)
    if not p:
        raise ValueError("project not found")
    obj = json.loads(tree_json)              # rechaza JSON inválido antes de escribir
    # El tipo vive en la RAÍZ del tree.json: {"type":"cart", ...} (verificado contra
    # los archivos reales del nodo, no de memoria).
    if config.SHARED and isinstance(obj, dict) and obj.get("type") in SHARED_BLOCKED_TYPES:
        raise TypeNotAllowed(obj["type"])
    quota.ensure_room(get_project(project_id)["folder_id"],
                      len(tree_json.encode("utf-8")), replaces=p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tree_json, encoding="utf-8")

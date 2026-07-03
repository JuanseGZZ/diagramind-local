"""Root del conector = repo git (ver [[25 - Conector Externo v2]] §7).

En este paso solo inicializamos el repo (working tree = copia viva). El commit
("Guardar"), diff, rollback y push a GitHub llegan en el paso de versionado.
"""

import subprocess
from pathlib import Path

from config import REPO_ROOT


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=False,
    )


def is_git_repo(path: Path) -> bool:
    r = _git(["rev-parse", "--is-inside-work-tree"], path)
    return r.returncode == 0 and r.stdout.strip() == "true"


def init_repo() -> Path:
    """Crea el ROOT y lo inicializa como repo git si no lo es (idempotente).

    Deja un commit vacío inicial + un `.gitkeep` para que HEAD exista desde el
    arranque (los diffs/rollback necesitan un HEAD base).
    """
    REPO_ROOT.mkdir(parents=True, exist_ok=True)
    if not is_git_repo(REPO_ROOT):
        _git(["init"], REPO_ROOT)
        # identidad local del repo para que los commits del sistema no fallen
        _git(["config", "user.name", "DiagraMind Connector"], REPO_ROOT)
        _git(["config", "user.email", "connector@diagramind.local"], REPO_ROOT)
        gitkeep = REPO_ROOT / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")
        _git(["add", "-A"], REPO_ROOT)
        _git(["commit", "-m", "init: root del conector"], REPO_ROOT)
    return REPO_ROOT

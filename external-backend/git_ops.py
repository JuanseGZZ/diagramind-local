"""Versionado = git sobre el ROOT del conector (ver [[25 - Conector Externo v2]] §7).

- El **working tree** es la copia viva (lo que el mirror sincroniza).
- **Guardar** = `git commit` del `tree.json` del proyecto que se está viendo, con
  **autor = el usuario**. Si hay una cuenta de GitHub conectada (`github.py`), se pushea.
- **log / diff / rollback** operan scopeados al `tree.json` de un proyecto.

Todas las operaciones se scopean a la ruta `<folder>/<project>/tree.json` (relativa al
root) para que el historial sea "por proyecto" aunque el repo sea único.
"""

import subprocess

from config import REPO_ROOT
from store import project_reldir


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=False,
    )


def _paths(pid: str) -> tuple[str, str] | None:
    """(reldir, path del tree.json) relativos al root, o None si el proyecto no existe."""
    rel = project_reldir(pid)
    if not rel:
        return None
    return rel, f"{rel}/tree.json"


def head() -> str | None:
    r = _git(["rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def has_changes(pid: str) -> bool:
    """¿El working tree del tree.json del proyecto difiere de lo commiteado?"""
    p = _paths(pid)
    if not p:
        return False
    r = _git(["status", "--porcelain", "--", p[1]])
    return bool(r.stdout.strip())


def commit(pid: str, author_name: str, author_email: str, message: str) -> dict:
    """Commitea el tree.json del proyecto con autor = el usuario. Si no hay cambios,
    devuelve {committed: False}. Devuelve {committed, commit}."""
    p = _paths(pid)
    if not p:
        raise ValueError("project not found")
    _, path = p
    _git(["add", "--", path])
    if not has_changes_staged(path):
        return {"committed": False, "commit": head()}
    r = _git(["commit", "-m", message, f"--author={author_name} <{author_email}>", "--", path])
    if r.returncode != 0:
        # p.ej. "nothing to commit" tras un add sin diff real
        return {"committed": False, "commit": head()}
    return {"committed": True, "commit": head()}


def has_changes_staged(path: str) -> bool:
    """¿Hay algo staged para ese path? (diff --cached no vacío)."""
    r = _git(["diff", "--cached", "--name-only", "--", path])
    return bool(r.stdout.strip())


def log(pid: str, limit: int = 50) -> list[dict]:
    p = _paths(pid)
    if not p:
        return []
    sep = "\x1f"
    fmt = sep.join(["%H", "%an", "%ae", "%ad", "%s"])
    r = _git(["log", f"-n{limit}", f"--format={fmt}", "--date=iso", "--", p[1]])
    out = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        h, an, ae, ad, s = (line.split(sep) + ["", "", "", "", ""])[:5]
        out.append({"commit": h, "author": an, "email": ae, "date": ad, "message": s})
    return out


def diff(pid: str, a: str | None = None, b: str | None = None) -> str:
    """Diff del tree.json del proyecto. Sin args: HEAD vs working. `a`: a vs working.
    `a`+`b`: a vs b."""
    p = _paths(pid)
    if not p:
        return ""
    path = p[1]
    if a and b:
        args = ["diff", a, b, "--", path]
    elif a:
        args = ["diff", a, "--", path]
    else:
        args = ["diff", "HEAD", "--", path]
    return _git(args).stdout


def rollback(pid: str, commit_hash: str, author_name: str, author_email: str) -> dict:
    """Restaura el tree.json del proyecto a un commit anterior y lo commitea como un
    cambio nuevo (rollback recuperable, estilo git revert de un path)."""
    p = _paths(pid)
    if not p:
        raise ValueError("project not found")
    _, path = p
    co = _git(["checkout", commit_hash, "--", path])
    if co.returncode != 0:
        raise ValueError(co.stderr.strip() or "checkout failed")
    _git(["add", "--", path])
    r = _git(["commit", "-m", f"rollback {path} to {commit_hash[:8]}",
              f"--author={author_name} <{author_email}>", "--", path])
    return {"committed": r.returncode == 0, "commit": head()}

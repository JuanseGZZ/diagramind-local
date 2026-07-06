"""GitHub POR proyecto editor (doc 27, fase 4): cada proyecto puede conectar SU
propio repo remoto y manejarlo desde el panel Source Control (y la IA por tools).

- El TARGET se vuelve un repo git (`git init` si no lo era; si ya es repo, se usa).
- La conexión {remoteUrl, token, branch} se guarda FUERA del repo (cada backend
  decide dónde: local → <app_dir>/editor_github.json 0600; externo → tabla DB).
- El token NUNCA se escribe en .git/config: se inyecta en la URL en cada
  push/fetch (patrón de github.py) y se redacta de todos los outputs.
- `push` = add -A + commit (autor = usuario; si lo pide la IA, el mensaje queda
  anotado) + push a la rama. `pull` = snapshot de seguridad (sourcever) + fetch +
  reset --hard FETCH_HEAD (traer lo último) o `checkout <ref> -- .` (traer una
  versión anterior sin romper la historia). `log` = últimos commits.

Módulo de LÓGICA PURA espejado local ↔ externo (como sourcever.py): si tocás uno,
copiá el archivo al otro. Errores → GitError(code, msg).
"""
import os
import subprocess
from urllib.parse import urlparse, urlunparse

import sourcever

GIT_TIMEOUT = 120


class GitError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _redact(text, token):
    return (text or "").replace(token, "***") if token else (text or "")


def _git(args, cwd, token=None):
    """Corre git y devuelve (code, salida redactada). GitError 400 si no hay git."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=GIT_TIMEOUT, check=False)
    except FileNotFoundError:
        raise GitError(400, "git no está instalado en la máquina del conector")
    except subprocess.TimeoutExpired:
        raise GitError(400, "git tardó demasiado (timeout)")
    return r.returncode, _redact((r.stdout or "") + (r.stderr or ""), token)


def _auth_url(remote_url, token):
    """Inserta el token en la URL https (para push/fetch autenticados)."""
    if not token:
        return remote_url
    u = urlparse(remote_url)
    if not u.scheme.startswith("http"):
        return remote_url                       # file:// o ssh: sin token en URL
    netloc = f"{token}@{u.hostname}" + (f":{u.port}" if u.port else "")
    return urlunparse((u.scheme, netloc, u.path, "", "", ""))


def _ident(author_name):
    email = f"{(author_name or 'diagraminder').replace(' ', '.').lower()}@diagraminder.local"
    return ["-c", f"user.name={author_name or 'DiagraMinder'}", "-c", f"user.email={email}"]


def ensure_repo(target):
    if not os.path.isdir(os.path.join(target, ".git")):
        code, out = _git(["-c", "init.defaultBranch=main", "init"], target)
        if code != 0:
            raise GitError(400, f"git init falló: {out.strip()}")


def gh_status(conn, target):
    """Estado público de la conexión (NUNCA devuelve el token)."""
    return {
        "connected": bool(conn and conn.get("remoteUrl")),
        "remoteUrl": (conn or {}).get("remoteUrl") or None,
        "branch": (conn or {}).get("branch") or "main",
        "isRepo": os.path.isdir(os.path.join(target, ".git")),
    }


def gh_push(conn, target, message, author_name, by_ai=False):
    """add -A + commit (si hay cambios) + push a la rama del remoto."""
    if not conn or not conn.get("remoteUrl"):
        raise GitError(400, "GitHub no está conectado en este proyecto")
    ensure_repo(target)
    token = conn.get("token") or ""
    branch = conn.get("branch") or "main"
    msg = (message or "").strip() or "Guardado desde DiagraMinder"
    if by_ai:
        msg += "\n\n[commit hecho por la IA vía DiagraMinder]"
    _git(["add", "-A"], target)
    committed = False
    code, _ = _git(["diff", "--cached", "--quiet"], target)
    if code != 0:                                # hay cambios staged
        email = f"{(author_name or 'diagraminder').replace(' ', '.').lower()}@diagraminder.local"
        code, out = _git([*_ident(author_name), "commit", "-m", msg,
                          f"--author={author_name or 'DiagraMinder'} <{email}>"], target, token)
        if code != 0:
            raise GitError(400, f"commit falló: {out.strip()}")
        committed = True
    code, out = _git(["push", _auth_url(conn["remoteUrl"], token), f"HEAD:{branch}"], target, token)
    if code != 0:
        raise GitError(400, f"push falló: {out.strip()}")
    return {"ok": True, "committed": committed, "branch": branch}


def gh_pull(conn, target, ref, sv_dir, author_name):
    """Trae del remoto. Sin `ref`: lo último de la rama (reset --hard FETCH_HEAD).
    Con `ref` (sha/tag): deja los ARCHIVOS de esa versión en el working tree
    (checkout <ref> -- . — la historia no se toca; un push posterior lo commitea).
    SIEMPRE guarda antes un snapshot de seguridad en las source versions."""
    if not conn or not conn.get("remoteUrl"):
        raise GitError(400, "GitHub no está conectado en este proyecto")
    ensure_repo(target)
    token = conn.get("token") or ""
    branch = conn.get("branch") or "main"
    pre = sourcever.sv_save(sv_dir, target, author_name,
                            f"(auto) antes de pull {ref or branch}")
    code, out = _git(["fetch", _auth_url(conn["remoteUrl"], token), branch], target, token)
    if code != 0:
        raise GitError(400, f"fetch falló: {out.strip()}")
    if ref:
        code, out = _git(["checkout", ref, "--", "."], target, token)
        if code != 0:
            raise GitError(400, f"no pude traer la versión {ref}: {out.strip()}")
        return {"ok": True, "ref": ref, "pre": pre}
    code, out = _git(["reset", "--hard", "FETCH_HEAD"], target, token)
    if code != 0:
        raise GitError(400, f"pull falló: {out.strip()}")
    return {"ok": True, "ref": branch, "pre": pre}


def gh_log(conn, target, n=20):
    """Últimos commits del repo del target: [{sha, author, ts(ms), msg}]."""
    if not os.path.isdir(os.path.join(target, ".git")):
        return {"commits": []}
    code, out = _git(["log", "-n", str(int(n)), "--format=%H%x1f%an%x1f%at%x1f%s"],
                     target, (conn or {}).get("token"))
    if code != 0:
        return {"commits": []}                    # repo sin commits todavía
    commits = []
    for line in out.strip().splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "author": parts[1],
                            "ts": int(parts[2]) * 1000, "msg": parts[3]})
    return {"commits": commits}

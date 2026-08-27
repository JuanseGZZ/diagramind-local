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


# Sin esto, un repo privado sin token deja a git ESPERANDO usuario/contraseña en
# una consola que no existe: el pedido se cuelga hasta el timeout en vez de fallar
# con un error claro. Vale para todo (verify, push, fetch), no solo para verify.
_NO_PROMPT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",       # no pedir user/pass por terminal
    "GIT_ASKPASS": "echo",            # ni por askpass gráfico
    "SSH_ASKPASS": "echo",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
    "GCM_INTERACTIVE": "never",       # credential manager de Windows
}


def _git(args, cwd, token=None, timeout=None):
    """Corre git y devuelve (code, salida redactada). GitError 400 si no hay git."""
    env = {**os.environ, **_NO_PROMPT_ENV}
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout or GIT_TIMEOUT, check=False,
                           env=env)
    except FileNotFoundError:
        raise GitError(400, "git is not installed on the connector machine")
    except subprocess.TimeoutExpired:
        raise GitError(400, "git took too long (timeout)")
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


# --------------------------- verificación del remoto -------------------------
# Conectar un proyecto a un repo que NO EXISTE dejaba la UI diciendo "conectado" y
# el error recién aparecía al primer push. La conexión se VERIFICA contra el
# remoto de verdad antes de guardarse, y esto vive en el SERVIDOR: una guarda en
# el cliente no es una regla (se saltea llamando la API directo).

VERIFY_TIMEOUT = 25

BAD_URL = ("That doesn't look like a repository URL. "
           "Use something like https://github.com/user/repo.git")
NOT_FOUND = ("That repo doesn't exist, or it's private and the token doesn't "
             "have access to it.")
BAD_AUTH = ("The remote rejected the token (check that it's valid and has "
            "access to the repo).")
NO_HOST = ("Couldn't reach the remote host: check the connection and that the "
           "URL is right.")


def check_remote_url(remote_url):
    """Valida la FORMA de la URL. GitError 400 si no puede ser un remoto git."""
    url = (remote_url or "").strip()
    if not url:
        raise GitError(400, BAD_URL)
    # scp-like de ssh: git@host:owner/repo.git
    if "://" not in url:
        if "@" in url and ":" in url.split("@", 1)[1]:
            return url
        if os.path.isabs(url):                  # ruta local: remoto en disco
            return url
        raise GitError(400, BAD_URL)
    u = urlparse(url)
    if u.scheme in ("http", "https"):
        if not u.hostname or len([p for p in u.path.split("/") if p]) < 2:
            raise GitError(400, BAD_URL)        # falta host o owner/repo
        return url
    if u.scheme in ("ssh", "git", "file"):
        return url
    raise GitError(400, BAD_URL)


def _explain(out):
    """Traduce la queja de git a algo que se pueda leer."""
    low = (out or "").lower()
    if ("could not read username" in low or "terminal prompts disabled" in low
            or "authentication failed" in low or "invalid username or password" in low
            or "permission denied" in low or "403" in low):
        # sin credenciales no se distingue "privado" de "no existe": GitHub
        # contesta lo mismo a propósito, y decir "no existe" sería mentir
        return BAD_AUTH if "authentication failed" in low or "403" in low else NOT_FOUND
    if ("not found" in low or "does not exist" in low or "repository not found" in low
            or "no such" in low):
        return NOT_FOUND
    if ("could not resolve host" in low or "unable to access" in low
            or "connection refused" in low or "timed out" in low
            or "network is unreachable" in low):
        return NO_HOST
    return NOT_FOUND


def gh_verify(remote_url, token=None, branch=None, cwd=None):
    """¿Existe el remoto y se puede acceder? Devuelve {branches, branchExists}.

    Levanta GitError(400, <mensaje legible>) si no. `ls-remote` no clona ni
    escribe nada: es la forma barata de preguntarle al remoto si está ahí.
    """
    url = check_remote_url(remote_url)
    code, out = _git(["ls-remote", "--heads", _auth_url(url, token)],
                     cwd or os.getcwd(), token, timeout=VERIFY_TIMEOUT)
    if code != 0:
        raise GitError(400, _explain(out))
    branches = []
    for line in (out or "").splitlines():
        if "refs/heads/" in line:
            branches.append(line.split("refs/heads/", 1)[1].strip())
    want = (branch or "main").strip() or "main"
    return {"branches": branches, "branchExists": want in branches}


def _ident(author_name):
    email = f"{(author_name or 'diagraminder').replace(' ', '.').lower()}@diagraminder.local"
    return ["-c", f"user.name={author_name or 'DiagraMinder'}", "-c", f"user.email={email}"]


def ensure_repo(target):
    if not os.path.isdir(os.path.join(target, ".git")):
        code, out = _git(["-c", "init.defaultBranch=main", "init"], target)
        if code != 0:
            raise GitError(400, f"git init failed: {out.strip()}")


def strip_userinfo(url):
    """Saca el user:pass de una URL https. La URL del remoto YA configurado puede
    traer un token adentro (`https://ghp_xxx@github.com/...`): eso no puede salir
    del server ni aparecer en pantalla."""
    u = (url or "").strip()
    if "://" not in u:
        return u
    parsed = urlparse(u)
    if not parsed.scheme.startswith("http") or "@" not in (parsed.netloc or ""):
        return u
    host = parsed.netloc.split("@", 1)[1]
    return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))


def detect_repo(target):
    """Lo que el target YA tiene de git, sin que nadie lo configure acá.

    Un editor abierto sobre una carpeta que ya era un repo (lo normal) no debería
    tener que reescribir a mano la URL que git ya conoce: se detecta el `origin` y
    la rama actual para poder ofrecerlos de un click.
    """
    if not os.path.isdir(os.path.join(target, ".git")):
        return {"isRepo": False, "detectedRemote": None, "currentBranch": None,
                "hasCommits": False}
    code, out = _git(["remote", "get-url", "origin"], target, timeout=15)
    remote = strip_userinfo(out.strip()) if code == 0 and out.strip() else None
    # --show-current funciona aunque el repo no tenga NINGÚN commit todavía
    code, out = _git(["branch", "--show-current"], target, timeout=15)
    branch = out.strip() if code == 0 and out.strip() else None
    code, _ = _git(["rev-parse", "--verify", "HEAD"], target, timeout=15)
    return {"isRepo": True, "detectedRemote": remote, "currentBranch": branch,
            "hasCommits": code == 0}


def gh_status(conn, target):
    """Estado público de la conexión (NUNCA devuelve el token)."""
    info = detect_repo(target)
    return {
        "connected": bool(conn and conn.get("remoteUrl")),
        "remoteUrl": (conn or {}).get("remoteUrl") or None,
        "branch": (conn or {}).get("branch") or info.get("currentBranch") or "main",
        **info,
    }


def gh_push(conn, target, message, author_name, by_ai=False):
    """add -A + commit (si hay cambios) + push a la rama del remoto."""
    if not conn or not conn.get("remoteUrl"):
        raise GitError(400, "GitHub is not connected on this project")
    ensure_repo(target)
    token = conn.get("token") or ""
    branch = conn.get("branch") or "main"
    msg = (message or "").strip() or "Guardado desde DiagraMinder"
    if by_ai:
        msg += "\n\n[commit made by the AI via DiagraMinder]"
    _git(["add", "-A"], target)
    committed = False
    code, _ = _git(["diff", "--cached", "--quiet"], target)
    if code != 0:                                # hay cambios staged
        email = f"{(author_name or 'diagraminder').replace(' ', '.').lower()}@diagraminder.local"
        code, out = _git([*_ident(author_name), "commit", "-m", msg,
                          f"--author={author_name or 'DiagraMinder'} <{email}>"], target, token)
        if code != 0:
            raise GitError(400, f"commit failed: {out.strip()}")
        committed = True
    code, out = _git(["push", _auth_url(conn["remoteUrl"], token), f"HEAD:{branch}"], target, token)
    if code != 0:
        raise GitError(400, f"push failed: {out.strip()}")
    return {"ok": True, "committed": committed, "branch": branch}


def gh_pull(conn, target, ref, sv_dir, author_name):
    """Trae del remoto. Sin `ref`: lo último de la rama (reset --hard FETCH_HEAD).
    Con `ref` (sha/tag): deja los ARCHIVOS de esa versión en el working tree
    (checkout <ref> -- . — la historia no se toca; un push posterior lo commitea).
    SIEMPRE guarda antes un snapshot de seguridad en las source versions."""
    if not conn or not conn.get("remoteUrl"):
        raise GitError(400, "GitHub is not connected on this project")
    ensure_repo(target)
    token = conn.get("token") or ""
    branch = conn.get("branch") or "main"
    # force: es la red para deshacer un pull (que hace reset --hard). Tiene que
    # existir aunque no haya cambios locales — sin force, un pull "limpio" moriría
    # con "nada que guardar" antes de traer nada.
    pre = sourcever.sv_save(sv_dir, target, author_name,
                            f"(auto) before pull {ref or branch}", force=True)
    code, out = _git(["fetch", _auth_url(conn["remoteUrl"], token), branch], target, token)
    if code != 0:
        raise GitError(400, f"fetch failed: {out.strip()}")
    if ref:
        code, out = _git(["checkout", ref, "--", "."], target, token)
        if code != 0:
            raise GitError(400, f"could not fetch version {ref}: {out.strip()}")
        return {"ok": True, "ref": ref, "pre": pre}
    code, out = _git(["reset", "--hard", "FETCH_HEAD"], target, token)
    if code != 0:
        raise GitError(400, f"pull failed: {out.strip()}")
    return {"ok": True, "ref": branch, "pre": pre}


LOG_FMT = "%H%x1f%an%x1f%at%x1f%s"
FETCH_TIMEOUT = 20


def _log(target, rango, n, token=None):
    """`git log` de un rango → [{sha, author, ts(ms), msg}]."""
    code, out = _git(["log", "-n", str(int(n)), f"--format={LOG_FMT}", rango],
                     target, token, timeout=30)
    if code != 0:
        return []
    commits = []
    for line in out.strip().splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "author": parts[1],
                            "ts": int(parts[2]) * 1000, "msg": parts[3]})
    return commits


def _count(target, rango):
    code, out = _git(["rev-list", "--count", rango], target, timeout=20)
    try:
        return int(out.strip()) if code == 0 else 0
    except ValueError:
        return 0


def gh_log(conn, target, n=20, fetch=True):
    """El historial que se MUESTRA en el panel.

    Antes esto era `git log` del HEAD local a secas, y eso ENGAÑA: si la carpeta se
    *conectó* a un repo (`git init` + remote) en vez de clonarlo, el local no tiene
    nada de lo que hay arriba — el panel mostraba dos commits propios y el usuario
    veía un historial que no era el de su repo.

    Ahora, con conexión, se hace `fetch` y se listan los commits DEL REMOTO; los
    locales que todavía no están arriba van primero y marcados (`unpushed`), que es
    como los muestra VSC. Sin conexión —o si el remoto no responde— se cae al log
    local y se avisa con `fetched:false`, en vez de mentir con una lista a medias.
    """
    if not os.path.isdir(os.path.join(target, ".git")):
        return {"commits": [], "ahead": 0, "behind": 0, "fetched": False}

    token = (conn or {}).get("token") or ""
    remote = (conn or {}).get("remoteUrl")
    branch = (conn or {}).get("branch") or "main"
    tiene_head = _git(["rev-parse", "--verify", "HEAD"], target, timeout=15)[0] == 0

    if not (fetch and remote):
        return {"commits": _log(target, "HEAD", n, token) if tiene_head else [],
                "ahead": 0, "behind": 0, "fetched": False}

    code, out = _git(["fetch", _auth_url(remote, token), branch], target, token,
                     timeout=FETCH_TIMEOUT)
    if code != 0:
        # el remoto no contesta (o la rama no existe todavía): se muestra lo local
        return {"commits": _log(target, "HEAD", n, token) if tiene_head else [],
                "ahead": 0, "behind": 0, "fetched": False,
                "error": _explain(out)}

    remotos = _log(target, "FETCH_HEAD", n, token)
    sin_subir = _log(target, "FETCH_HEAD..HEAD", n, token) if tiene_head else []
    for c in sin_subir:
        c["unpushed"] = True
    return {
        "commits": sin_subir + remotos,
        "ahead": len(sin_subir),
        "behind": _count(target, "HEAD..FETCH_HEAD") if tiene_head else len(remotos),
        "fetched": True,
    }

"""El rate limit de los endpoints caros vive en el SERVER — probado POR LA RED.

Por qué existe: el botón "Guardar" de la web ahora tiene cooldown y no commitea sin
cambios, pero eso es UX. Cualquiera manda la misma request con `curl` en un bucle, y
cada commit es trabajo de disco y de git. La regla tiene que estar acá, y este test la
prueba como la probaría un atacante: mandando el POST a mano, sin pasar por la web.

Cubre: que corte al pasarse (429 + Retry-After), que ANTES del límite no moleste, que
sea POR USUARIO (uno frenado no frena al otro) y que sea POR BUCKET (frenar los commits
no frena la subida de documentos).

    diagramind-local/external-backend/.venv/bin/python tests/test_ratelimit.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

CB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PW = "pw-admin-test"

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _hdrs(h):
    """Headers en minúscula: HTTP es case-insensitive y uvicorn los manda así."""
    return {k.lower(): v for k, v in dict(h).items()}


def req(base, path, method="GET", body=None, token=None, raw=None):
    r = urllib.request.Request(base + path, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if raw is not None:
        r.add_header("Content-Type", "application/octet-stream")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, data, timeout=20) as resp:
            t = resp.read()
            return resp.status, (json.loads(t) if t.strip() else {}), _hdrs(resp.headers)
    except urllib.error.HTTPError as e:
        t = e.read()
        try:
            return e.code, json.loads(t or b"{}"), _hdrs(e.headers)
        except Exception:
            return e.code, {"raw": t.decode(errors="replace")}, _hdrs(e.headers)


def start(home):
    port = free_port()
    env = {**os.environ, "DMC_HOME": home, "DMC_PORT": str(port), "DMC_HOST": "127.0.0.1",
           "DMC_ADMIN_PASSWORD": ADMIN_PW, "DMC_ADMIN_MUST_CHANGE": "0"}
    p = subprocess.Popen([CB + "/.venv/bin/python", "server.py"], cwd=CB, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            return p, base
        except Exception:
            if p.poll() is not None:
                print(p.stdout.read())
                sys.exit("el conector murió al arrancar")
            time.sleep(0.25)
    sys.exit("el conector no levantó")


import tempfile

home = tempfile.mkdtemp(prefix="dmc-ratelimit-")
proc, base = start(home)
print(f"\n### rate limit (conector real en {base})")

try:
    _, d, _ = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    adm = d["access"]

    def mkuser(name):
        _, u, _ = req(base, "/users", "POST", {"username": name, "role": "editor"}, token=adm)
        _, lg, _ = req(base, "/auth/login", "POST", {"username": name, "password": u["tempPassword"]})
        _, ch, _ = req(base, "/auth/change-password", "POST", {"newPassword": name + "-pw-123"},
                       token=lg["access"])
        return u["id"], ch["access"]

    ana_id, ana = mkuser("ana")
    beto_id, beto = mkuser("beto")

    # las carpetas las crea el ADMIN (un editor no puede) y le da write a cada uno;
    # después cada usuario crea SU proyecto, para que el dueño sea él.
    def project_for(uid, tok, tag):
        _, f, _ = req(base, "/folders", "POST", {"name": "f-" + tag}, token=adm)
        req(base, f"/users/{uid}/acl", "POST", {"folderId": f["id"], "permission": "write"}, token=adm)
        _, p, _ = req(base, "/projects", "POST", {"folderId": f["id"], "name": "p-" + tag}, tok)
        assert p.get("id"), p
        return p["id"]

    pid_ana = project_for(ana_id, ana, "ana")
    pid_beto = project_for(beto_id, beto, "beto")

    print("\n=== A. commits: corta al pasarse (límite 20/min) ===")
    codes = []
    _ultimo_429_headers = {}
    for _ in range(25):
        s, _d, h = req(base, "/versions/commit", "POST", {"id": pid_ana}, ana)
        if s == 429: _ultimo_429_headers = h
        codes.append((s, h.get("retry-after")))
    exitos = [c for c, _ in codes if c == 200]
    frenados = [(c, ra) for c, ra in codes if c == 429]
    check("los primeros pasan", len(exitos) >= 15, f"200s={len(exitos)}")
    check("y al pasarse devuelve 429", len(frenados) > 0, f"429s={len(frenados)}")
    check("nunca más de 20 aceptados en la ventana", len(exitos) <= 20, f"200s={len(exitos)}")
    check("el 429 trae Retry-After", all(ra and int(ra) > 0 for _c, ra in frenados),
          "headers del 429: " + json.dumps(_ultimo_429_headers))
    _s, d, _h = req(base, "/versions/commit", "POST", {"id": pid_ana}, ana)
    check("el mensaje dice qué pasa", "too many" in json.dumps(d).lower(), json.dumps(d))

    print("\n=== B. el límite es POR USUARIO ===")
    s, _d, _h = req(base, "/versions/commit", "POST", {"id": pid_beto}, beto)
    check("beto puede guardar aunque ana esté frenada", s == 200, str(s))

    print("\n=== C. el límite es POR ENDPOINT (bucket) ===")
    # ana está frenada para commits; subir un documento usa otro bucket
    s, _d, _h = req(base, f"/docs/put?projectId={pid_ana}&hash=" + "a" * 64, "POST",
                    raw=b"hola", token=ana)
    check("subir un documento NO está frenado por los commits", s in (200, 400, 409, 413),
          f"status={s}")
    s2, _d2, _h2 = req(base, "/versions/commit", "POST", {"id": pid_ana}, ana)
    check("y los commits siguen frenados", s2 == 429, str(s2))

    print("\n=== D2. el WebSocket también tiene freno (los `edit` escriben disco) ===")
    # El socket queda abierto: sin límite, un cliente puede mandar `edit` en bucle y
    # cada uno escribe el árbol y lo difunde a la sala.
    from websockets.sync.client import connect as ws_connect
    st, td, _ = req(base, "/auth/ws-ticket", "POST", {}, beto)
    ws = ws_connect(base.replace("http", "ws") + f"/ws?ticket={td['ticket']}", open_timeout=15)
    try:
        ws.send(json.dumps({"t": "open", "projectId": pid_beto}))
        deadline = time.time() + 10
        while time.time() < deadline:
            if json.loads(ws.recv(timeout=10)).get("t") in ("state", "error"):
                break
        # 130 ediciones seguidas (el límite es 120/min)
        limitado = None
        for i in range(130):
            ws.send(json.dumps({"t": "edit", "projectId": pid_beto,
                                "tree": {"type": "cart", "lastIdCharged": i}}))
        # los rechazos llegan como frames de error; se leen los que haya
        try:
            while True:
                m = json.loads(ws.recv(timeout=3))
                if m.get("t") == "error" and m.get("code") == "rate_limited":
                    limitado = m
                    break
        except TimeoutError:
            pass
        check("el WS corta las ediciones de más", limitado is not None,
              "no llegó ningún frame rate_limited")
        if limitado:
            check("y dice cuánto esperar", int(limitado.get("retryAfter") or 0) > 0, str(limitado))
    finally:
        ws.close()

    print("\n=== D. sin token no se pasa igual (auth primero) ===")
    s, _d, _h = req(base, "/versions/commit", "POST", {"id": pid_ana})
    check("sin token: 401/403, no 429", s in (401, 403), str(s))

finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(0 if fail == 0 else 1)

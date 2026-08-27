"""La presencia lista PERSONAS, no sockets (doc 25 §5).

Por qué existe: con dos pestañas abiertas del mismo usuario, la barra de
colaboración mostraba el nombre repetido y decía "2 en línea" — parecía que había
dos personas mirando el proyecto cuando había una sola con dos ventanas.

Se prueba con dos WebSockets REALES de la misma cuenta contra el conector de
verdad, que es la única forma de reproducirlo.

    diagramind-local/external-backend/.venv/bin/python tests/test_presence.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
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


def req(base, path, method="GET", body=None, token=None):
    r = urllib.request.Request(base + path, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(r, data, timeout=20) as resp:
            t = resp.read()
            return resp.status, (json.loads(t) if t.strip() else {})
    except urllib.error.HTTPError as e:
        t = e.read()
        return e.code, (json.loads(t or b"{}") if t.strip() else {})


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


def ws_open(base, tok, pid):
    from websockets.sync.client import connect
    st, td = req(base, "/auth/ws-ticket", "POST", {}, tok)
    assert st == 200, (st, td)
    ws = connect(base.replace("http", "ws") + f"/ws?ticket={td['ticket']}", open_timeout=15)
    ws.send(json.dumps({"t": "open", "projectId": pid}))
    return ws


def last_presence(ws, timeout=6):
    """Última lista de presencia que llegó por este socket."""
    fin = time.time() + timeout
    ultima = None
    while time.time() < fin:
        try:
            m = json.loads(ws.recv(timeout=max(0.2, fin - time.time())))
        except (TimeoutError, Exception):
            break
        if m.get("t") == "presence":
            ultima = m.get("members", [])
    return ultima


home = tempfile.mkdtemp(prefix="dmc-pres-")
proc, base = start(home)
try:
    s, d = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    tok = d["access"]
    s, d = req(base, "/folders", "POST", {"name": "f-pres"}, tok)
    fid = d["id"]
    s, d = req(base, "/projects", "POST", {"folderId": fid, "name": "p-pres"}, tok)
    pid = d["id"]

    print("\n=== una persona, DOS pestañas ===")
    a = ws_open(base, tok, pid)
    last_presence(a, 3)
    b = ws_open(base, tok, pid)          # la MISMA cuenta, otra ventana
    miembros = last_presence(a, 6) or last_presence(b, 3) or []

    check("la lista trae UNA sola entrada (una persona)", len(miembros) == 1, miembros)
    check("y dice que tiene dos ventanas abiertas",
          (miembros[0].get("sessions") if miembros else None) == 2, miembros)
    check("con el nombre una sola vez",
          [m["username"] for m in miembros] == ["admin"], miembros)

    print("\n=== se cierra una pestaña ===")
    b.close()
    time.sleep(1.0)
    miembros = last_presence(a, 6)
    if miembros is None:                  # sin cambios nuevos, se re-consulta
        miembros = []
    check("sigue habiendo una persona", len(miembros) == 1, miembros)
    check("y ya con una sola ventana",
          (miembros[0].get("sessions") if miembros else None) == 1, miembros)
    a.close()
finally:
    proc.terminate()
    proc.wait(timeout=10)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)

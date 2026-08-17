"""El conector COMPARTIDO rechaza alojar Editor / IA Orchestrator (doc 12).

Por qué existe este test: la primera versión del portero era **solo de UI** — la web
escondía las opciones y listo. Eso no es seguridad: un `fetch` a mano, o un asistente
por MCP, creaban el proyecto igual. La regla vive ahora en `projects.write_tree`, que
es el ÚNICO punto por el que pasa toda escritura de árbol (WebSocket del editor
colaborativo y tools MCP), y esto lo verifica **por la red**, sin pasar por la web.

En una instancia NO compartida (local, o un conector propio) tiene que seguir
permitiéndose: es donde esos modos funcionan.

    diagramind-local/external-backend/.venv/bin/python tests/test_shared_types.py
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
        try:
            return e.code, json.loads(t or b"{}")
        except Exception:
            return e.code, {"raw": t.decode(errors="replace")}


def start(home, shared):
    port = free_port()
    env = {**os.environ, "DMC_HOME": home, "DMC_PORT": str(port), "DMC_HOST": "127.0.0.1",
           "DMC_ADMIN_PASSWORD": ADMIN_PW, "DMC_ADMIN_MUST_CHANGE": "0"}
    if shared:
        env["DMC_SHARED"] = "1"
    else:
        env.pop("DMC_SHARED", None)
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


def login(base):
    s, d = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    assert s == 200, (s, d)
    return d["access"]


def make_project(base, tok, name):
    s, d = req(base, "/folders", "POST", {"name": "f-" + name}, tok)
    assert s == 200, (s, d)
    fid = d["id"]
    s, d = req(base, "/projects", "POST", {"folderId": fid, "name": name}, tok)
    assert s == 200, (s, d)
    return d["id"]


def write_tree_ws(base, tok, pid, tree_type):
    """Escribe el árbol por el MISMO camino que usa la web: el WebSocket /ws.
    Devuelve (codigoDeError|None, detalle)."""
    # Cliente WS de `websockets`, que YA viene con `uvicorn[standard]` (dep del
    # server): no hace falta instalar nada para correr este test.
    # El /ws NO acepta el Bearer: pide un TICKET de un solo uso (auth.py:135), que es
    # exactamente lo que hace la web antes de abrir el socket.
    from websockets.sync.client import connect
    st, td = req(base, "/auth/ws-ticket", "POST", {}, tok)
    assert st == 200, (st, td)
    ticket = td.get("ticket")
    ws = connect(base.replace("http", "ws") + f"/ws?ticket={ticket}", open_timeout=15)
    try:
        ws.send(json.dumps({"t": "open", "projectId": pid}))
        # respuesta al open (state) — se descarta
        deadline = time.time() + 10
        while time.time() < deadline:
            m = json.loads(ws.recv(timeout=10))
            if m.get("t") in ("state", "error"):
                break
        ws.send(json.dumps({"t": "edit", "projectId": pid,
                            "tree": {"type": tree_type, "lastIdCharged": 0}}))
        # Un `edit` ACEPTADO no le contesta nada al emisor (el broadcast va a los
        # OTROS clientes de la sala). O sea: silencio = se guardó. Solo el rechazo
        # manda un frame. Por eso se espera un rato corto y el timeout es el éxito
        # — y de todas formas el test confirma después contra /projects/tree, que es
        # el efecto que de verdad importa.
        try:
            while True:
                m = json.loads(ws.recv(timeout=3))
                if m.get("t") == "error":
                    return m.get("code"), m.get("detail", "")
        except TimeoutError:
            return None, ""
    finally:
        ws.close()


print("\n=== A. instancia COMPARTIDA (el free): rechaza los dos tipos ===")
home = tempfile.mkdtemp(prefix="dmc-shared-")
proc, base = start(home, shared=True)
try:
    s, d = req(base, "/health")
    check("el /health se declara shared", d.get("shared") is True, d)
    tok = login(base)

    for t in ("editor", "orchestrator"):
        pid = make_project(base, tok, "p-" + t)
        code, detail = write_tree_ws(base, tok, pid, t)
        check(f"guardar un árbol '{t}' es RECHAZADO", code == "type_not_allowed",
              f"code={code} detail={detail}")
        check(f"y el error explica por qué ('{t}')",
              "shared connector" in detail or "not available" in detail, detail)
        # lo que importa de verdad: NO quedó nada guardado
        s, d = req(base, f"/projects/tree?id={pid}", token=tok)
        tree = (d or {}).get("tree")
        check(f"el árbol '{t}' NO se persistió", not tree or f'"{t}"' not in tree,
              str(tree)[:80])

    print("\n--- los tipos normales siguen andando ---")
    for t in ("cart", "freestyle", "documents"):
        pid = make_project(base, tok, "ok-" + t)
        code, detail = write_tree_ws(base, tok, pid, t)
        check(f"'{t}' se guarda sin problema", code is None, f"code={code} {detail}")
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("\n=== B. instancia NO compartida (local / conector propio): se permite ===")
home2 = tempfile.mkdtemp(prefix="dmc-own-")
proc, base = start(home2, shared=False)
try:
    s, d = req(base, "/health")
    check("el /health NO se declara shared", d.get("shared") is False, d)
    tok = login(base)
    for t in ("editor", "orchestrator"):
        pid = make_project(base, tok, "p-" + t)
        code, detail = write_tree_ws(base, tok, pid, t)
        check(f"'{t}' se guarda en una instancia propia", code is None,
              f"code={code} {detail}")
        s, d = req(base, f"/projects/tree?id={pid}", token=tok)
        check(f"y el árbol '{t}' quedó persistido", f'"{t}"' in (d.get("tree") or ""),
              str(d.get("tree"))[:80])
finally:
    proc.terminate()
    proc.wait(timeout=10)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)

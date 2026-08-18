"""El revoke de un documento compartido avisa EN VIVO por el WebSocket.

Bug que arregla: al revocarle el acceso a alguien, el conector quitaba la ACL pero no
se lo decía a nadie. El invitado seguía viendo (y editando su copia local) el
documento hasta que recargaba la página — "el revoke no es inmediato".

Ahora `POST /projects/acl` le manda al AFECTADO `{"t":"acl", ...}` por su socket. Se
verifica por la red: Beto abre un WS de verdad y tiene que recibir el aviso sin haber
pedido nada.

    diagramind-local/external-backend/.venv/bin/python tests/test_acl_live.py
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
ADMIN_PW = "adminpw123"
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
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


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


def start(home):
    port = free_port()
    env = {**os.environ, "DMC_HOME": home, "DMC_PORT": str(port), "DMC_HOST": "127.0.0.1",
           "DMC_ADMIN_PASSWORD": ADMIN_PW, "DMC_ADMIN_MUST_CHANGE": "0"}
    p = subprocess.Popen([CB + "/.venv/bin/python", "server.py"], cwd=CB, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/health", timeout=1); return p, base
        except Exception:
            if p.poll() is not None:
                print(p.stdout.read()); sys.exit("el conector murió al arrancar")
            time.sleep(0.25)
    sys.exit("el conector no levantó")


def open_ws(base, tok):
    from websockets.sync.client import connect
    st, td = req(base, "/auth/ws-ticket", "POST", {}, tok)
    assert st == 200, (st, td)
    return connect(base.replace("http", "ws") + f"/ws?ticket={td['ticket']}", open_timeout=15)


def wait_for(ws, kind, timeout=8):
    """Primer mensaje de tipo `kind`; None si no llegó en `timeout`."""
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            m = json.loads(ws.recv(timeout=max(0.2, fin - time.time())))
        except TimeoutError:
            return None
        if m.get("t") == kind:
            return m
    return None


home = tempfile.mkdtemp(prefix="dmc-acl-live-")
proc, base = start(home)
try:
    _, d = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    adm = d["access"]
    _, folder = req(base, "/folders", "POST", {"name": "carpeta-de-ana"}, token=adm)
    fid = folder["id"]

    def mkuser(name):
        _, u = req(base, "/users", "POST", {"username": name, "role": "editor"}, token=adm)
        _, lg = req(base, "/auth/login", "POST", {"username": name, "password": u["tempPassword"]})
        _, ch = req(base, "/auth/change-password", "POST", {"newPassword": name + "-pw-123"},
                    token=lg["access"])
        return u["id"], ch["access"]

    ana_id, ana = mkuser("ana")
    beto_id, beto = mkuser("beto")
    req(base, f"/users/{ana_id}/acl", "POST", {"folderId": fid, "permission": "write"}, token=adm)
    _, proj = req(base, "/projects", "POST", {"folderId": fid, "name": "compartido"}, token=ana)
    pid = proj["id"]

    print("\n=== A. Ana comparte: Beto lo recibe EN VIVO, sin pedir nada ===")
    ws = open_ws(base, beto)
    try:
        s, _ = req(base, "/projects/acl", "POST",
                   {"projectId": pid, "userId": beto_id, "permission": "read"}, token=ana)
        check("la ACL se otorgó", s == 200, s)
        m = wait_for(ws, "acl")
        check("a Beto le llega el aviso por el socket", m is not None, "no llegó nada")
        check("con el proyecto y el permiso nuevo",
              m and m.get("projectId") == pid and m.get("permission") == "read", m)
        check("y el nombre del documento, para poder mostrarlo",
              m and m.get("projectName") == "compartido", m)

        print("\n=== B. Ana REVOCA: el aviso llega al instante (el bug) ===")
        t0 = time.time()
        s, _ = req(base, "/projects/acl", "POST",
                   {"projectId": pid, "userId": beto_id, "permission": "none"}, token=ana)
        check("la ACL se revocó", s == 200, s)
        m = wait_for(ws, "acl")
        dt = time.time() - t0
        check("Beto se entera SIN recargar", m is not None, "no llegó nada")
        check("y el aviso dice permission=none", m and m.get("permission") == "none", m)
        check(f"es inmediato (llegó en {dt:.2f}s)", m is not None and dt < 3, f"{dt:.2f}s")
    finally:
        ws.close()

    print("\n=== C. el aviso es SOLO para el afectado ===")
    ws_ana = open_ws(base, ana)
    try:
        req(base, "/projects/acl", "POST",
            {"projectId": pid, "userId": beto_id, "permission": "read"}, token=ana)
        # Ana es quien comparte: no tiene por qué recibir el aviso de permiso de otro.
        check("Ana NO recibe el aviso de Beto", wait_for(ws_ana, "acl", timeout=2) is None)
    finally:
        ws_ana.close()

    print("\n=== D. y el acceso real quedó cortado (no solo el aviso) ===")
    req(base, "/projects/acl", "POST",
        {"projectId": pid, "userId": beto_id, "permission": "none"}, token=ana)
    s, d = req(base, f"/projects/tree?id={pid}", token=beto)
    check("Beto ya no puede leer el árbol", s == 403, (s, d))
    s, d = req(base, "/folders", token=beto)
    check("ni ve la carpeta", d.get("folders") == [], d)
finally:
    proc.terminate(); proc.wait(timeout=10)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)

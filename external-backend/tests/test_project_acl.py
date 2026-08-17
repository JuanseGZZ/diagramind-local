"""e2e de la ACL por PROYECTO (compartir un documento suelto) — doc 25 §3.1.

Escenario: Ana tiene una carpeta con 2 proyectos; comparte SOLO uno con Beto; Beto
tiene que ver ese y NADA más. Después Ana revoca y Beto lo pierde.

Patrón de la casa: server REAL con HOME temporal + puerto libre. No necesita cluster
ni docker.

    diagramind-local/external-backend/.venv/bin/python tests/test_project_acl.py
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


home = tempfile.mkdtemp(prefix="dmc-acl-")
port = free_port()
env = {**os.environ, "DMC_HOME": home, "DMC_PORT": str(port), "DMC_HOST": "127.0.0.1",
       "DMC_ADMIN_PASSWORD": "adminpw123", "DMC_ADMIN_MUST_CHANGE": "0"}
proc = subprocess.Popen([sys.executable, "server.py"], cwd=CB, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f"http://127.0.0.1:{port}"
for _ in range(100):
    try:
        urllib.request.urlopen(base + "/health", timeout=1)
        break
    except Exception:
        if proc.poll() is not None:
            print(proc.stdout.read())
            sys.exit("el conector murió al arrancar")
        time.sleep(0.25)

try:
    print("\n=== preparar escenario ===")
    _, d = req(base, "/auth/login", "POST", {"username": "admin", "password": "adminpw123"})
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

    _, p1 = req(base, "/projects", "POST", {"folderId": fid, "name": "compartido"}, token=ana)
    _, p2 = req(base, "/projects", "POST", {"folderId": fid, "name": "privado"}, token=ana)
    check("Ana creó 2 proyectos", bool(p1.get("id") and p2.get("id")), (p1, p2))

    print("\n=== ANTES de compartir: Beto no ve nada ===")
    s, d = req(base, "/folders", token=beto)
    check("Beto no ve la carpeta", d.get("folders") == [], d)
    s, d = req(base, "/projects?folderId=" + fid, token=beto)
    check("listar proyectos → 403", s == 403, (s, d))
    s, d = req(base, f"/projects/tree?id={p1['id']}", token=beto)
    check("abrir el proyecto → 403", s == 403, (s, d))

    print("\n=== Ana comparte SOLO 'compartido' con Beto (read) ===")
    s, d = req(base, "/projects/acl", "POST",
               {"projectId": p1["id"], "userId": beto_id, "permission": "read"}, token=ana)
    check("compartir 200", s == 200, (s, d))

    s, d = req(base, "/folders", token=beto)
    fs = d.get("folders", [])
    check("Beto ahora VE la carpeta", len(fs) == 1 and fs[0]["id"] == fid, d)
    check("marcada como shared", fs and fs[0].get("shared") is True, d)
    check("informa el owner", fs and fs[0].get("owner") == "ana", d)

    s, d = req(base, "/projects?folderId=" + fid, token=beto)
    names = [p["name"] for p in d.get("projects", [])]
    check("Beto ve SOLO el compartido", names == ["compartido"], d)
    check("el privado NO aparece", "privado" not in names, d)
    check("permiso POR PROYECTO en la respuesta",
          d["projects"][0].get("permission") == "read", d)

    s, d = req(base, f"/projects/tree?id={p1['id']}", token=beto)
    check("puede abrir el compartido", s == 200, (s, d))
    s, d = req(base, f"/projects/tree?id={p2['id']}", token=beto)
    check("NO puede abrir el privado", s == 403, (s, d))

    print("\n=== read no alcanza para escribir ===")
    s, d = req(base, "/fs/write", "POST",
               {"projectId": p1["id"], "path": "x.md", "content": "hola"}, token=beto)
    check("fs/write con read → 403", s == 403, (s, d))
    s, d = req(base, "/projects/rename", "POST", {"id": p1["id"], "name": "otro"}, token=beto)
    check("renombrar con read → 403", s == 403, (s, d))

    print("\n=== el DUEÑO no pierde nada (el permiso es el MÁXIMO) ===")
    s, d = req(base, "/projects?folderId=" + fid, token=ana)
    check("Ana sigue viendo sus 2 proyectos", len(d.get("projects", [])) == 2, d)
    check("y con write en el compartido",
          all(p["permission"] == "write" for p in d["projects"]), d)
    s, d = req(base, f"/projects/tree?id={p2['id']}", token=ana)
    check("Ana sigue entrando al privado", s == 200, (s, d))

    print("\n=== subir a write ===")
    req(base, "/projects/acl", "POST",
        {"projectId": p1["id"], "userId": beto_id, "permission": "write"}, token=ana)
    s, d = req(base, "/fs/write", "POST",
               {"projectId": p1["id"], "path": "x.md", "content": "hola"}, token=beto)
    check("fs/write con write ya no da 403", s != 403, (s, d))
    s, d = req(base, "/projects/rename", "POST", {"id": p1["id"], "name": "compartido"},
               token=beto)
    check("renombrar con write ya se puede", s == 200, (s, d))

    print("\n=== Beto NO puede re-compartir lo que le compartieron ===")
    s, d = req(base, "/projects/acl", "POST",
               {"projectId": p1["id"], "userId": ana_id, "permission": "write"}, token=beto)
    check("re-compartir → 403", s == 403, (s, d))

    print("\n=== quién tiene acceso / revocar ===")
    s, d = req(base, f"/projects/acl?projectId={p1['id']}", token=ana)
    check("Ana ve con quién compartió", s == 200 and len(d.get("shared", [])) == 1, d)
    req(base, "/projects/acl", "POST",
        {"projectId": p1["id"], "userId": beto_id, "permission": "none"}, token=ana)
    s, d = req(base, f"/projects/tree?id={p1['id']}", token=beto)
    check("tras revocar, Beto pierde el acceso", s == 403, (s, d))
    s, d = req(base, "/folders", token=beto)
    check("y deja de ver la carpeta", d.get("folders") == [], d)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print(f"\n{'=' * 44}\nRESULTADO: {ok} ok / {fail} fallados\n{'=' * 44}")
sys.exit(1 if fail else 0)

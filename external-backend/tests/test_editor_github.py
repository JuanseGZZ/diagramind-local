"""Conectar un proyecto editor a GitHub VERIFICA que el repo exista (doc 27 fase 4).

Por qué existe este test: la primera versión guardaba la URL sin preguntarle nada al
remoto. Escribías cualquier cosa —`jhnkljnls`— y la UI decía "conectado": el error
recién aparecía en el primer push, lejos de donde se cometió. Peor: `git` se quedaba
esperando usuario y contraseña en una consola que no existe, así que el pedido se
COLGABA en vez de fallar.

Se verifica POR LA RED contra el conector real, no llamando a la función: el chequeo
tiene que estar en el servidor, porque una guarda en el cliente se saltea con un
`fetch` a mano (o por MCP).

Sin internet: los remotos de prueba son repos `file://` en un temporal — uno que
existe de verdad y otro que no. Es la misma pregunta que le hace git a GitHub.

    diagramind-local/external-backend/.venv/bin/python tests/test_editor_github.py
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


def req(base, path, method="GET", body=None, token=None, timeout=60):
    r = urllib.request.Request(base + path, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(r, data, timeout=timeout) as resp:
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
    env.pop("DMC_SHARED", None)          # el compartido no aloja editor (doc 12)
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


def err_of(d):
    return str(d.get("detail") or d.get("error") or d)


tmp = tempfile.mkdtemp(prefix="dmc-gh-")
home = os.path.join(tmp, "home")
os.makedirs(home)

# un remoto que EXISTE (repo bare de verdad) y uno que no
real_remote = os.path.join(tmp, "remoto.git")
subprocess.run(["git", "init", "--bare", "-b", "main", real_remote],
               capture_output=True, check=True)
ghost_remote = os.path.join(tmp, "no-existe.git")

proc, base = start(home)
try:
    s, d = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    tok = d["access"]
    s, d = req(base, "/folders", "POST", {"name": "f-gh"}, tok)
    fid = d["id"]
    s, d = req(base, "/projects", "POST", {"folderId": fid, "name": "editorr"}, tok)
    pid = d["id"]
    target = os.path.join(tmp, "trabajo")
    s, d = req(base, "/editor/target", "POST", {"projectId": pid, "path": target}, tok)
    assert s == 200, (s, d)

    print("\n=== A. lo que NO existe se rechaza (y no queda guardado) ===")

    # el caso del reporte: se escribió cualquier cosa y quedó "conectado"
    t0 = time.time()
    s, d = req(base, "/svgit/connect", "POST",
               {"projectId": pid, "remoteUrl": "jhnkljnls"}, tok)
    check("una URL que no es una URL se rechaza", s == 400, f"{s} {err_of(d)}")
    check("y el error dice qué se esperaba",
          "repository URL" in err_of(d), err_of(d))
    check("falla RÁPIDO (no se cuelga pidiendo credenciales)", time.time() - t0 < 30,
          f"{time.time() - t0:.1f}s")

    s, d = req(base, "/svgit/status?projectId=" + pid, token=tok)
    check("después del rechazo el proyecto sigue SIN conectar",
          d.get("connected") is False, d)

    for url in (f"file://{ghost_remote}",
                "https://github.com/usuario-inexistente-dm/repo-que-no-existe.git"):
        s, d = req(base, "/svgit/connect", "POST",
                   {"projectId": pid, "remoteUrl": url}, tok)
        etiqueta = "un repo local que no existe" if url.startswith("file") else "un repo remoto que no existe"
        check(f"{etiqueta} se rechaza", s == 400, f"{s} {err_of(d)}")
        check("  y el mensaje se entiende (no es la queja cruda de git)",
              "doesn't exist" in err_of(d) or "Couldn't reach" in err_of(d)
              or "rejected the token" in err_of(d), err_of(d))
        s2, d2 = req(base, "/svgit/status?projectId=" + pid, token=tok)
        check("  y NO quedó guardado", d2.get("connected") is False, d2)

    print("\n=== B. un repo que existe SÍ se conecta ===")
    s, d = req(base, "/svgit/connect", "POST",
               {"projectId": pid, "remoteUrl": f"file://{real_remote}", "branch": "main"}, tok)
    check("conectar a un repo real devuelve 200", s == 200, f"{s} {err_of(d)}")
    check("y queda conectado", d.get("connected") is True, d)
    check("el token NUNCA vuelve en la respuesta", "token" not in d, d)

    s, d = req(base, "/svgit/status?projectId=" + pid, token=tok)
    check("el status confirma la conexión", d.get("connected") is True, d)
    check("con la rama elegida", d.get("branch") == "main", d)

    print("\n=== C. la rama que todavía no existe se avisa, no se rechaza ===")
    s, d = req(base, "/svgit/connect", "POST",
               {"projectId": pid, "remoteUrl": f"file://{real_remote}",
                "branch": "una-rama-nueva"}, tok)
    check("conectar a una rama inexistente se permite", s == 200, f"{s} {err_of(d)}")
    check("pero el server avisa que la rama todavía no está",
          d.get("branchExists") is False, d)

    print("\n=== D. el chequeo es del SERVIDOR, no de la web ===")
    # (el pedido de arriba fue un urllib pelado: nunca pasó por la UI)
    s, d = req(base, "/svgit/connect", "POST",
               {"projectId": pid, "remoteUrl": "  "}, tok)
    check("una URL vacía también se rechaza", s in (400, 422), f"{s} {err_of(d)}")
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("\n=== E. no se guardan versiones vacías ===")
proc, base = start(os.path.join(tmp, "home2"))
try:
    s, d = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    tok = d["access"]
    s, d = req(base, "/folders", "POST", {"name": "f-sv"}, tok)
    fid = d["id"]
    s, d = req(base, "/projects", "POST", {"folderId": fid, "name": "ed2"}, tok)
    pid = d["id"]
    work = os.path.join(tmp, "trabajo2")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "a.txt"), "w") as f:
        f.write("uno")
    req(base, "/editor/target", "POST", {"projectId": pid, "path": work}, tok)

    s, d = req(base, "/sv/save", "POST", {"projectId": pid, "note": "primera"}, tok)
    check("la primera versión se guarda", s == 200, f"{s} {err_of(d)}")

    s, d = req(base, "/sv/save", "POST", {"projectId": pid, "note": "otra igual"}, tok)
    check("guardar OTRA VEZ sin tocar nada se rechaza", s == 400, f"{s} {err_of(d)}")
    check("y el motivo lo dice con todas las letras",
          "hasn't changed" in err_of(d), err_of(d))

    s, d = req(base, "/sv/list?projectId=" + pid, token=tok)
    check("el historial NO se llenó de copias", len(d.get("versions", [])) == 1,
          str(len(d.get("versions", []))))

    with open(os.path.join(work, "a.txt"), "w") as f:
        f.write("dos")
    s, d = req(base, "/sv/save", "POST", {"projectId": pid, "note": "con cambios"}, tok)
    check("con un cambio real sí guarda", s == 200, f"{s} {err_of(d)}")

    # el restore hace un snapshot de seguridad ANTES: ese tiene que salir igual
    # aunque no haya cambios, o no habría con qué deshacer el propio restore
    s, d = req(base, "/sv/list?projectId=" + pid, token=tok)
    vieja = d["versions"][-1]["id"]
    s, d = req(base, "/sv/restore", "POST", {"projectId": pid, "id": vieja}, tok)
    check("restaurar funciona aunque no haya cambios pendientes", s == 200, f"{s} {err_of(d)}")
    check("y deja su snapshot de seguridad para poder volver",
          bool((d or {}).get("pre")), d)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("\n=== F. el repo que la carpeta YA tiene se detecta ===")
home3 = os.path.join(tmp, "home3")
proc, base = start(home3)
try:
    s, d = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    tok = d["access"]
    s, d = req(base, "/folders", "POST", {"name": "f-det"}, tok)
    fid = d["id"]
    s, d = req(base, "/projects", "POST", {"folderId": fid, "name": "ed3"}, tok)
    pid = d["id"]

    # una carpeta que YA es un repo, con origin y un commit (el caso del usuario)
    work = os.path.join(tmp, "ya-es-repo")
    os.makedirs(work, exist_ok=True)
    run = lambda *a: subprocess.run(list(a), cwd=work, capture_output=True, check=True)
    run("git", "init", "-b", "trabajo")
    run("git", "config", "user.email", "t@t.t")
    run("git", "config", "user.name", "Test")
    with open(os.path.join(work, "hola.txt"), "w") as f:
        f.write("hola")
    run("git", "add", "-A")
    run("git", "commit", "-m", "commit que ya estaba")
    # con el token METIDO en la URL: no puede salir del server
    run("git", "remote", "add", "origin", "https://ghp_secretoquenodebesalir@github.com/user/repo.git")
    req(base, "/editor/target", "POST", {"projectId": pid, "path": work}, tok)

    s, d = req(base, "/svgit/status?projectId=" + pid, token=tok)
    check("se reconoce que la carpeta ya es un repo", d.get("isRepo") is True, d)
    check("se detecta el remoto que ya tenía configurado",
          d.get("detectedRemote") == "https://github.com/user/repo.git", d)
    check("el token embebido en esa URL NO sale del server",
          "ghp_secreto" not in json.dumps(d), d)
    check("se detecta la rama en la que está", d.get("currentBranch") == "trabajo", d)
    check("y que ya tiene commits", d.get("hasCommits") is True, d)
    check("pero sigue figurando como NO conectado", d.get("connected") is False, d)

    s, d = req(base, "/svgit/log?projectId=" + pid, token=tok)
    check("el historial de git se puede leer sin conectar nada",
          len(d.get("commits", [])) == 1, d)
    check("con el commit que ya estaba",
          (d.get("commits") or [{}])[0].get("msg") == "commit que ya estaba", d)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("\n=== G. el historial que se muestra es el DEL REMOTO ===")
# El caso del reporte: la carpeta se CONECTÓ a un repo (git init + remote) en vez
# de clonarlo. El local no tiene nada de lo que hay arriba, así que un `git log`
# pelado mostraba dos commits propios y parecía ser "el repo".
home4 = os.path.join(tmp, "home4")
proc, base = start(home4)
try:
    s, d = req(base, "/auth/login", "POST", {"username": "admin", "password": ADMIN_PW})
    tok = d["access"]
    s, d = req(base, "/folders", "POST", {"name": "f-log"}, tok)
    fid = d["id"]
    s, d = req(base, "/projects", "POST", {"folderId": fid, "name": "ed4"}, tok)
    pid = d["id"]

    # un remoto CON historia propia (lo que el usuario ve en GitHub)
    remoto = os.path.join(tmp, "conhistoria.git")
    subprocess.run(["git", "init", "--bare", "-b", "main", remoto], capture_output=True, check=True)
    sembrado = os.path.join(tmp, "sembrado")
    os.makedirs(sembrado, exist_ok=True)
    run_s = lambda *a: subprocess.run(list(a), cwd=sembrado, capture_output=True, check=True)
    run_s("git", "init", "-b", "main")
    run_s("git", "config", "user.email", "r@r.r")
    run_s("git", "config", "user.name", "OtraPersona")
    for i in (1, 2):
        with open(os.path.join(sembrado, f"f{i}.txt"), "w") as f:
            f.write(str(i))
        run_s("git", "add", "-A")
        run_s("git", "commit", "-m", f"commit REMOTO {i}")
    run_s("git", "push", remoto, "main")

    # el target: repo aparte, con UN commit propio que el remoto no tiene
    work = os.path.join(tmp, "trabajo4")
    os.makedirs(work, exist_ok=True)
    run_w = lambda *a: subprocess.run(list(a), cwd=work, capture_output=True, check=True)
    run_w("git", "init", "-b", "main")
    run_w("git", "config", "user.email", "y@y.y")
    run_w("git", "config", "user.name", "Yo")
    with open(os.path.join(work, "mio.txt"), "w") as f:
        f.write("mio")
    run_w("git", "add", "-A")
    run_w("git", "commit", "-m", "commit MIO sin subir")
    req(base, "/editor/target", "POST", {"projectId": pid, "path": work}, tok)

    s, d = req(base, "/svgit/connect", "POST",
               {"projectId": pid, "remoteUrl": f"file://{remoto}", "branch": "main"}, tok)
    assert s == 200, (s, d)

    s, d = req(base, "/svgit/log?projectId=" + pid, token=tok)
    msgs = [c["msg"] for c in d.get("commits", [])]
    check("se leyó el remoto de verdad", d.get("fetched") is True, d)
    check("aparecen los commits que están EN EL REMOTO",
          "commit REMOTO 2" in msgs and "commit REMOTO 1" in msgs, msgs)
    check("y también el propio que todavía no se subió",
          "commit MIO sin subir" in msgs, msgs)
    check("el propio va marcado como no subido",
          any(c.get("unpushed") for c in d["commits"] if c["msg"] == "commit MIO sin subir"), d)
    check("los del remoto NO se marcan como no subidos",
          not any(c.get("unpushed") for c in d["commits"] if c["msg"].startswith("commit REMOTO")), d)
    check("se cuenta lo que falta subir", d.get("ahead") == 1, d)
    check("y lo que falta bajar", d.get("behind") == 2, d)

    # remoto inalcanzable: se avisa en vez de mostrar lo local como si fuera el repo
    s, d = req(base, "/svgit/connect", "POST",
               {"projectId": pid, "remoteUrl": f"file://{remoto}", "branch": "main"}, tok)
    shutil_ok = True
    import shutil as _sh
    _sh.rmtree(remoto)
    s, d = req(base, "/svgit/log?projectId=" + pid, token=tok)
    check("si el remoto no responde se avisa", d.get("fetched") is False, d)
    check("y NO se hace pasar lo local por el historial del repo",
          bool(d.get("error")), d)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)

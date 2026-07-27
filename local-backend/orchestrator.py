"""Motor del IA Orchestrator (doc 28, Fase 3 — PARALELO).

- UN run por proyecto a la vez. El trabajo entra por una TAREA (`agTask` → agente
  raíz por flecha `task`) o por el MINI-CHAT de un nodo (decisión S: hablarle a un
  agente — típicamente el PM — es otro entry point; la charla entra a su memoria y
  se puede borrar entera).
- TOKENS = ÁRBOL DE FRAMES (decisiones C y D): cada frame es un agente trabajando
  con su transcript nativo. `delegar` suspende al caller; con `agentes: [..]` el
  token se FORKEA (varios hijos en paralelo) y el `join` elige cómo despertarlo:
  "todos" (default: una sola vuelta con todas las respuestas) o "cada_una" (una
  vuelta por respuesta). Un SCHEDULER (thread por run) lanza un worker por frame
  listo; el estado compartido se muta siempre bajo el LOCK global y las llamadas
  LLM/CLI corren afuera (ahí vive el paralelismo).
- ANTI-PISADAS (decisión E): lock por RECURSO DE ESCRITURA (projectId de cada
  `usa` con permiso ≥ editar) + lock POR AGENTE (un empleado hace UNA cosa a la
  vez). Un frame adquiere TODOS sus locks antes de girar y los suelta al terminar
  o suspenderse (delegar / preguntar): el que no puede queda `queued` (en cola).
  Adquisición todo-o-nada ⇒ sin deadlocks.
- TOOLS DE RECURSOS: por cada `agResource` conectado por `usa`, el agente recibe
  tools con prefijo `r<idNodo>_` según permiso (editor → fs_*/sv_* vía
  editorfs/sourcever; diagramas → view_tree/set_tree sobre el tree.json del mirror).
- MEMORIA por agente (decisión N): entradas {id, kind: task|chat|delegado, chatId?,
  ts, texto} en <orch>/<pid>/memory/<nodeId>.json; se inyecta al system si está
  habilitada; `memHeavy` (decisión R) si supera MEM_HEAVY_CHARS. `limpiar_memoria`
  como tool (la propia o la de un subordinado conectado por `delega`).
- HUMANO EN EL LOOP: `preguntar_al_usuario` suspende SOLO esa rama; las demás
  siguen. `run.pendings` acumula las preguntas abiertas (run.pending = la primera,
  compat) y el run recién pasa a `waiting_human` cuando NADA más puede avanzar.
- SNAPSHOT pre-ejecución (decisión I): al crear el frame de un agente con recursos
  de escritura → sv_save en los editores + copia del tree.json en los diagramas.
- PRESUPUESTO (decisión J): maxTurns (llamadas LLM) por run; pause/resume/kill.
- Cabezas: APIs (Anthropic + Google + OpenAI-compatible) y Claude Code CLI (fase 4),
  mixto. Las credenciales son del PROYECTO (keys.json del conector, decisión T) y
  son VARIAS con nombre: cada nodo elige cuál usa (`data.ia.credId`).

El server (server.py) provee el contexto de rutas: dónde está el tree.json del
orquestador y cómo resolver los de los proyectos-recurso (mirror de la carpeta).
"""
import hmac
import json
import os
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import subprocess
import tempfile

import editorfs
import sourcever
from claude import EFFORT_THINK, find_claude, map_model, _self_cmd
from skills import SKILLS as TYPE_SKILLS, install_skills
from util import safe_name

MEM_HEAVY_CHARS = 8000
MAX_TURNS_DEFAULT = 30
MAX_TOOL_ITERS = 12          # iteraciones LLM dentro de UN turno de agente
HTTP_TIMEOUT = 180

RUNS = {}                     # pid -> run dict vivo
KEYS = {}                     # pid -> apiKeys (SOLO RAM)
LOCK = threading.Lock()       # protege RUNS/run dicts; los workers lo sueltan para llamar al LLM
RUNTIME = {}                  # pid -> {cv, procs, alive} (NUNCA se serializa)

CONTROL_TOOLS = {"delegar", "responder", "preguntar_al_usuario"}
CLI_PROVIDERS = {"local", "local-codex", "local-gemini"}
CLI_TIMEOUT = 15 * 60        # tope de un turno CLI


def _rt(pid):
    """Runtime NO serializable del run (condition variable + procesos CLI vivos)."""
    rt = RUNTIME.get(pid)
    if rt is None:
        rt = RUNTIME.setdefault(pid, {"cv": threading.Condition(LOCK), "procs": {}, "alive": False})
    return rt


# ===================== storage =====================

def orch_dir(app_dir, pid):
    d = os.path.join(app_dir, "orchestrator", pid)
    os.makedirs(d, exist_ok=True)
    return d


def _run_path(ctx):
    return os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "run.json")


def _runs_dir(ctx):
    d = os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "runs")
    os.makedirs(d, exist_ok=True)
    return d


def _runs_index_path(ctx):
    return os.path.join(_runs_dir(ctx), "index.json")


def _run_summary(run):
    """Fila del historial: lo justo para la lista (sin events)."""
    return {"id": run["id"], "entry": run["entry"], "rootNodeId": run.get("rootNodeId"),
            "status": run["status"], "final": run.get("final"), "error": run.get("error"),
            "createdAt": run.get("createdAt"), "endedAt": run.get("endedAt"),
            "turns": run.get("turns", 0), "spend": (run.get("spend") or {}).get("total", {})}


def _archive_run(ctx, run):
    """Guarda un run TERMINADO en runs/<id>.json + lo prepend al index (una vez)."""
    if run.get("_archived") or run["status"] not in ("done", "error", "killed"):
        return
    run["_archived"] = True
    run["endedAt"] = run.get("endedAt") or int(time.time() * 1000)
    full = {k: v for k, v in run.items()
            if not str(k).startswith("_") and k not in ("stack", "frames", "locks")}
    _write_json(os.path.join(_runs_dir(ctx), f"{run['id']}.json"), full)
    idx = _read_json(_runs_index_path(ctx), [])
    idx = [x for x in idx if x.get("id") != run["id"]]
    idx.insert(0, _run_summary(run))
    _write_json(_runs_index_path(ctx), idx[:200])


def _mem_path(ctx, node_id):
    d = os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "memory")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{node_id}.json")


def _chat_path(ctx, node_id):
    d = os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "chats")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{node_id}.json")


def _read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _save(ctx, run):
    """Persiste el run SIN las keys. Llamar con el LOCK tomado."""
    _write_json(_run_path(ctx), {k: v for k, v in run.items() if not str(k).startswith("_")})


# ===================== CREDENCIALES DEL ORQUESTADOR (decisión T) =====================
# Las credenciales viven EN EL CONECTOR del proyecto (decisión 2026-07-11): el core
# tiene que poder correr sin la web conectada, y no son las keys del chat del
# usuario. Se guardan en <orch>/<pid>/keys.json (0600) — NUNCA en el tree.json ni
# en el mirror (eso las metería en localStorage/git).
#
# Desde 2026-07-25 son VARIAS y con NOMBRE (las que el usuario quiera: 5 de
# Anthropic, 3 de OpenAI, n de Google, f de "otras") y cada nodo agente elige CUÁL
# usa (`data.ia.credId`). Forma del archivo:
#     {"creds": [{"id","nombre","provider","key","url"?}], "mcp:<idNodo>": {...}}
# Las claves legacy por proveedor ({"anthropic": "sk-…"}) se MIGRAN a `creds` la
# primera vez que se leen.

CRED_PROVIDERS = ("anthropic", "google", "openai", "other")
PROV_LABEL = {"anthropic": "Anthropic", "google": "Google", "openai": "OpenAI", "other": "Otra API"}


def _keys_path(ctx):
    return os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "keys.json")


def _write_keys(ctx, keys):
    _write_json(_keys_path(ctx), keys)
    try:
        os.chmod(_keys_path(ctx), 0o600)
    except OSError:
        pass


def _new_cred_id(creds):
    used = {c.get("id") for c in creds}
    n = 1
    while f"k{n}" in used:
        n += 1
    return f"k{n}"


def _migrate_keys(keys):
    """Legacy {proveedor: key} → lista `creds` con nombre. Devuelve (keys, cambió)."""
    creds = keys.get("creds")
    changed = not isinstance(creds, list)
    creds = creds if isinstance(creds, list) else []
    for prov in ("anthropic", "openai", "other"):
        if prov not in keys:
            continue
        val = keys.pop(prov)
        changed = True
        key = val if isinstance(val, str) else (val or {}).get("key")
        url = (val or {}).get("url") if isinstance(val, dict) else None
        if not key:
            continue
        creds.append({"id": _new_cred_id(creds), "nombre": PROV_LABEL[prov],
                      "provider": prov, "key": key, **({"url": url} if url else {})})
    keys["creds"] = creds
    return keys, changed


def keys_read(ctx):
    keys, changed = _migrate_keys(_read_json(_keys_path(ctx), {}))
    if changed:
        _write_keys(ctx, keys)
    return keys


def creds_of(keys, provider=None):
    """Credenciales utilizables (con key), opcionalmente filtradas por proveedor."""
    out = [c for c in (keys.get("creds") or []) if isinstance(c, dict) and c.get("key")]
    return [c for c in out if c.get("provider") == provider] if provider else out


def cred_write(ctx, cred):
    """Alta o edición de una credencial con nombre. En una EDICIÓN sin `key` se
    conserva la que ya estaba (la UI nunca ve el secreto, solo el hint)."""
    cred = cred or {}
    prov = cred.get("provider")
    if prov not in CRED_PROVIDERS:
        raise OrchError(400, f"proveedor inválido: {prov!r}")
    key = (cred.get("key") or "").strip()
    url = (cred.get("url") or "").strip()
    nombre = (cred.get("nombre") or "").strip()
    if prov == "other" and not url:
        raise OrchError(400, "la credencial de 'Otra API' necesita la URL del endpoint")
    keys = keys_read(ctx)
    creds = keys.setdefault("creds", [])
    cur = next((c for c in creds if c.get("id") == cred.get("id")), None) if cred.get("id") else None
    if cur is None:
        if not key:
            raise OrchError(400, "falta la API key")
        cur = {"id": _new_cred_id(creds)}
        creds.append(cur)
    cur["provider"] = prov
    if key:
        cur["key"] = key
    cur["nombre"] = nombre or PROV_LABEL[prov]
    if prov == "other":
        cur["url"] = url
    else:
        cur.pop("url", None)
    _write_keys(ctx, keys)
    return keys_status(ctx)


def cred_delete(ctx, cred_id):
    keys = keys_read(ctx)
    keys["creds"] = [c for c in (keys.get("creds") or []) if c.get("id") != cred_id]
    _write_keys(ctx, keys)
    return keys_status(ctx)


def keys_write(ctx, patch):
    """Credenciales de nodos MCP/API (`mcp:<idNodo>`): setea/borra (vacío = borrar).
    Las de los modelos van por cred_write/cred_delete."""
    keys = keys_read(ctx)
    for prov, val in (patch or {}).items():
        if not str(prov).startswith("mcp:"):
            continue
        empty = not val or (isinstance(val, dict) and not (val.get("key") or val.get("url")))
        if empty:
            keys.pop(prov, None)
        elif isinstance(val, dict):
            # merge parcial (p.ej. actualizar solo el header sin pisar la key)
            keys[prov] = {**(keys.get(prov) or {}), **{k: v for k, v in val.items() if v}}
        else:
            keys[prov] = val
    _write_keys(ctx, keys)
    return keys_status(ctx)


def _key_hint(k):
    return ("…" + k[-4:]) if isinstance(k, str) and len(k) >= 8 else ""


def keys_status(ctx):
    """Estado para la UI: las credenciales con su nombre/proveedor/hint (NUNCA el
    secreto) + las credenciales de los nodos MCP."""
    keys = keys_read(ctx)
    creds = [{"id": c.get("id"), "nombre": c.get("nombre") or "", "provider": c.get("provider"),
              "url": c.get("url") or "", "hint": _key_hint(c.get("key") or "")}
             for c in creds_of(keys)]
    mcp = {}
    for k, v in keys.items():
        if str(k).startswith("mcp:"):
            mcp[str(k).split(":", 1)[1]] = {"set": bool((v or {}).get("key")),
                                            "hint": _key_hint((v or {}).get("key") or "")}
    return {"creds": creds, "keys": {"mcp": mcp}}


# ===================== MCP / API EXTERNAS (decisión V — salida, fase 6c) =====================
# Nodos agMcp conectados por `usa`: el agente recibe las tools de ese servicio con
# prefijo m<idNodo>_ (conocimiento LOCAL: solo quien está cableado las ve).
# - tipo "api": endpoints definidos a mano en el nodo → una tool por endpoint.
# - tipo "mcp": cliente MCP streamable-HTTP mínimo (initialize → tools/list →
#   tools/call), tools remotas descubiertas (cache 5 min por nodo).
# Credenciales: keys.json sección `mcp:<idNodo>` = {key, header?} (decisión T) —
# van como `Authorization: Bearer <key>` salvo header custom. SIN lock por default
# (servicios externos con su propia consistencia).

MCP_HTTP_TIMEOUT = 60
_MCP_CACHE = {}               # (pid, nodeId, url) -> {ts, session, tools}


def mcps_of(graph, node_id):
    out = []
    for f in graph["flechas"]:
        if f.get("kind") == "usa" and int(f.get("fromId", -1)) == int(node_id):
            r = graph["nodos"].get(int(f["toId"]))
            if r and r.get("type") == "agMcp":
                out.append(r)
    return out


def _mcp_headers(ctx, node_id):
    cred = keys_read(ctx).get(f"mcp:{node_id}") or {}
    key = cred.get("key")
    if not key:
        return {}
    name = (cred.get("header") or "").strip() or "Authorization"
    if name.lower() == "authorization" and not key.lower().startswith("bearer "):
        key = f"Bearer {key}"
    return {name: key}


def _http_raw(url, method, headers, body_bytes):
    req = urllib.request.Request(url, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, body_bytes, timeout=MCP_HTTP_TIMEOUT) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"").decode("utf-8", "replace")
    except Exception as e:
        raise OrchError(502, f"no pude hablar con el servicio externo: {e}")


def _mcp_rpc(url, headers, session, method, params=None, rpc_id=1, notify=False):
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not notify:
        body["id"] = rpc_id
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", **headers}
    if session:
        hdrs["Mcp-Session-Id"] = session
    status, rhdrs, text = _http_raw(url, "POST", hdrs, json.dumps(body).encode("utf-8"))
    session = rhdrs.get("Mcp-Session-Id") or rhdrs.get("mcp-session-id") or session
    if notify or not (text or "").strip():
        return session, None
    data = text
    if "text/event-stream" in (rhdrs.get("Content-Type") or ""):
        datas = [l[5:].strip() for l in text.splitlines() if l.startswith("data:")]
        data = datas[-1] if datas else "{}"
    try:
        obj = json.loads(data)
    except Exception:
        raise OrchError(502, f"respuesta MCP no-JSON (HTTP {status})")
    if isinstance(obj, dict) and obj.get("error"):
        raise OrchError(502, f"error MCP: {(obj['error'] or {}).get('message')}")
    return session, (obj or {}).get("result")


def _mcp_connect(ctx, node):
    """(url, session, tools) del server MCP del nodo (cache 5 min)."""
    node_id = int(node["id"])
    url = ((node.get("data") or {}).get("config") or {}).get("url") or ""
    if not url:
        raise OrchError(400, f"el nodo MCP «{node.get('titulo')}» no tiene URL configurada")
    ck = (ctx["pid"], node_id, url)
    c = _MCP_CACHE.get(ck)
    if c and time.time() - c["ts"] < 300:
        return url, c["session"], c["tools"]
    headers = _mcp_headers(ctx, node_id)
    session, _ = _mcp_rpc(url, headers, None, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "diagramind-orchestrator", "version": "1.0"}})
    _mcp_rpc(url, headers, session, "notifications/initialized", {}, notify=True)
    session2, res = _mcp_rpc(url, headers, session, "tools/list", {}, rpc_id=2)
    tools = (res or {}).get("tools") or []
    _MCP_CACHE[ck] = {"ts": time.time(), "session": session2 or session, "tools": tools}
    return url, session2 or session, tools


def _tool_name_safe(s):
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(s or "ep"))[:48]


def _add_api_endpoint_tool(ctx, node, mid, ep, tools, execs):
    tname = f"{mid}_{_tool_name_safe(ep.get('name'))}"
    def call(i, _ep=ep, _nid=int(node["id"])):
        headers = {"Content-Type": "application/json", **_mcp_headers(ctx, _nid)}
        url = _ep.get("url") or ""
        q = i.get("query")
        if isinstance(q, dict) and q:
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode({k: str(v) for k, v in q.items()})
        body = (i.get("body") or "").encode("utf-8") if i.get("body") else None
        status, _h, text = _http_raw(url, (_ep.get("method") or "GET").upper(), headers, body)
        return json.dumps({"status": status, "body": text[:20000]}, ensure_ascii=False), status >= 400
    tools.append(dict(name=tname, **_s(
        f"[{node.get('titulo') or mid}] {_ep_desc(ep)}",
        {"body": {"type": "string", "description": "cuerpo JSON (opcional)"},
         "query": {"type": "object", "description": "query params (opcional)"}})))
    execs[tname] = call


def _ep_desc(ep):
    return (ep.get("description") or f"{(ep.get('method') or 'GET').upper()} {ep.get('url') or ''}")[:400]


def _add_mcp_remote_tool(ctx, node, mid, url, rt, tools, execs):
    tname = f"{mid}_{_tool_name_safe(rt.get('name'))}"
    schema = rt.get("inputSchema") or {"type": "object", "properties": {}}
    tools.append({"name": tname,
                  "description": (f"[{node.get('titulo') or mid}] "
                                  f"{rt.get('description') or rt.get('name')}")[:800],
                  "schema": schema})
    def call(i, _rt=rt, _nid=int(node["id"]), _url=url):
        headers = _mcp_headers(ctx, _nid)
        session = (_MCP_CACHE.get((ctx["pid"], _nid, _url)) or {}).get("session")
        _s2, res = _mcp_rpc(_url, headers, session, "tools/call",
                            {"name": _rt.get("name"), "arguments": i or {}}, rpc_id=3)
        parts = [c.get("text", "") for c in (res or {}).get("content", []) if c.get("type") == "text"]
        out = "\n".join(p for p in parts if p) or json.dumps(res or {}, ensure_ascii=False)
        return out[:60000], bool((res or {}).get("isError"))
    execs[tname] = call


def mcp_tools(ctx, graph, node_id):
    """(tools, executors, notas) de los nodos agMcp conectados por `usa`."""
    tools, execs, notes = [], {}, []
    for m in mcps_of(graph, node_id):
        mid = f"m{m['id']}"
        d = m.get("data") or {}
        cfg = d.get("config") or {}
        label = f"{m.get('titulo') or mid} ({d.get('tipo')}, preset {d.get('preset')})"
        if d.get("tipo") == "api":
            eps = cfg.get("endpoints") or []
            notes.append(f"- {mid}: {label} — {len(eps)} endpoints")
            for ep in eps:
                _add_api_endpoint_tool(ctx, m, mid, ep, tools, execs)
        else:
            try:
                url, _sess, remote = _mcp_connect(ctx, m)
            except OrchError as e:
                notes.append(f"- {mid}: {label} — NO DISPONIBLE ({e.msg})")
                continue
            notes.append(f"- {mid}: {label} — {len(remote)} tools MCP")
            for rt in remote:
                _add_mcp_remote_tool(ctx, m, mid, url, rt, tools, execs)
    return tools, execs, notes


# ===================== WEBHOOKS (decisión V — entrada reactiva) =====================
# El exterior dispara trabajo: cada nodo agWebhook tiene URI + TOKEN PROPIO
# (generados por el conector, guardados en <orch>/<pid>/hooks.json — nunca en el
# tree.json). El disparo responde AL INSTANTE (arrancó o se encoló); si el que
# llama quiere el resultado pasa `callback` (una URL suya) y al terminar el run
# se le POSTea {hookId, runId, status, final|error}. Cola FIFO con tope por nodo
# (queueMax, default 50) — NO hay runs concurrentes — y rate-limit por hook.

HOOK_RATE_MAX = 30            # disparos por minuto por hook
HOOK_QUEUE_DEFAULT = 50
_HOOK_RATE = {}               # hookId -> [timestamps] (RAM)


def _hooks_path(ctx):
    return os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "hooks.json")


def _hooks_index_path(app_dir):
    d = os.path.join(app_dir, "orchestrator")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "hooks-index.json")


def _triggers_path(ctx):
    return os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "triggers.json")


def hook_register(ctx, node_id):
    """Crea (o REGENERA: invalida el anterior) la URI + token de un nodo webhook."""
    graph = load_graph(ctx)
    n = graph["nodos"].get(int(node_id))
    if not n or n.get("type") != "agWebhook":
        raise OrchError(400, "el nodo no es un webhook")
    hooks = _read_json(_hooks_path(ctx), {})
    old = (hooks.get(str(int(node_id))) or {}).get("hookId")
    hook = {"hookId": "h" + uuid.uuid4().hex[:12], "token": secrets.token_urlsafe(24)}
    hooks[str(int(node_id))] = hook
    _write_json(_hooks_path(ctx), hooks)
    try:
        os.chmod(_hooks_path(ctx), 0o600)
    except OSError:
        pass
    idx = _read_json(_hooks_index_path(ctx["app_dir"]), {})
    if old:
        idx.pop(old, None)
    idx[hook["hookId"]] = ctx["pid"]
    _write_json(_hooks_index_path(ctx["app_dir"]), idx)
    return dict(hook)


def hook_info(ctx, node_id):
    return dict(_read_json(_hooks_path(ctx), {}).get(str(int(node_id))) or
                {"hookId": None, "token": None})


def hook_resolve(app_dir, hook_id):
    """hookId → projectId del orquestador dueño (o None)."""
    return _read_json(_hooks_index_path(app_dir), {}).get(str(hook_id))


def _trigger_text(node, payload):
    d = (node.get("data") or {}) if node else {}
    base = (f"WEBHOOK «{(node or {}).get('titulo') or 'webhook'}» ({d.get('tipo') or 'otro'}): "
            f"{str(payload)[:6000]}")
    plantilla = (d.get("plantilla") or "").strip()
    return f"{plantilla}\n\n{base}" if plantilla else base


def _enqueue_trigger(ctx, trig, qmax):
    with LOCK:
        q = _read_json(_triggers_path(ctx), [])
        if len(q) >= qmax:
            raise OrchError(429, "trigger queue full — try again in a moment")
        q.append(trig)
        _write_json(_triggers_path(ctx), q)
        return {"ok": True, "queued": len(q)}


def _start_trigger_run(ctx, trig):
    graph = load_graph(ctx)
    node = graph["nodos"].get(int(trig["nodeId"]))
    texto = _trigger_text(node, trig["payload"])
    return start_run(ctx, "trigger", trig["rootId"], texto, {},
                     trigger={"hookId": trig["hookId"], "callback": trig.get("callback")})


def hook_fire(ctx, hook_id, token, payload, callback=None):
    """Disparo EXTERNO del hook. Valida el token propio del hook + rate-limit, y
    responde al instante: arrancó (runId) o quedó encolado (posición)."""
    hooks = _read_json(_hooks_path(ctx), {})
    node_id = next((int(k) for k, v in hooks.items() if v.get("hookId") == hook_id), None)
    if node_id is None:
        raise OrchError(404, "unknown hook")
    if not token or not hmac.compare_digest(str(token), hooks[str(node_id)].get("token") or ""):
        raise OrchError(401, "bad hook token")
    now = time.time()
    stamps = [t for t in _HOOK_RATE.get(hook_id, []) if now - t < 60]
    if len(stamps) >= HOOK_RATE_MAX:
        _HOOK_RATE[hook_id] = stamps
        raise OrchError(429, "rate limited — slow down")
    stamps.append(now)
    _HOOK_RATE[hook_id] = stamps

    graph = load_graph(ctx)
    node = graph["nodos"].get(node_id)
    if not node or node.get("type") != "agWebhook":
        raise OrchError(404, "unknown hook")
    if (node.get("data") or {}).get("enabled") is False:
        raise OrchError(409, "this webhook is disabled")
    edge = next((f for f in graph["flechas"]
                 if f.get("kind") == "trigger" and int(f.get("fromId", -1)) == node_id), None)
    if not edge:
        raise OrchError(409, "the webhook is not connected to an agent (trigger arrow)")
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)
    trig = {"nodeId": node_id, "rootId": int(edge["toId"]), "payload": payload,
            "callback": (str(callback or "").strip() or None), "hookId": hook_id,
            "ts": int(now * 1000)}
    qmax = int((node.get("data") or {}).get("queueMax") or HOOK_QUEUE_DEFAULT)
    with LOCK:
        run = RUNS.get(ctx["pid"])
        busy = bool(run and run["status"] in ("running", "waiting_human", "paused")) \
            or bool(_read_json(_triggers_path(ctx), []))
    if busy:
        return _enqueue_trigger(ctx, trig, qmax)
    try:
        return {"ok": True, "runId": _start_trigger_run(ctx, trig)["id"]}
    except OrchError as e:
        if e.code == 409:              # carrera: otro run arrancó justo antes
            return _enqueue_trigger(ctx, trig, qmax)
        raise


def _post_callback(trig, run_id, status, final, error):
    cb = (trig or {}).get("callback")
    if not cb:
        return
    try:
        body = json.dumps({"hookId": trig.get("hookId"), "runId": run_id, "status": status,
                           "final": final, "error": error}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(cb, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass                            # best-effort: el callback caído no frena nada


def _after_run(ctx, run):
    """Post-run (SIN el lock global): callback del webhook + drenar la cola."""
    _post_callback(run.get("trigger"), run["id"], run["status"], run.get("final"), run.get("error"))
    while True:
        with LOCK:
            live = RUNS.get(ctx["pid"])
            if live and live["status"] in ("running", "waiting_human", "paused"):
                return
            q = _read_json(_triggers_path(ctx), [])
            if not q:
                return
            trig = q.pop(0)
            _write_json(_triggers_path(ctx), q)
        try:
            _start_trigger_run(ctx, trig)
            return
        except OrchError as e:          # trigger inválido (agente borrado, key faltante)
            _post_callback(trig, None, "error", None, e.msg)
            continue


# ===================== grafo =====================

NODE_TYPES = {"agAgent", "agResource", "agTask", "agDept", "agWebhook", "agMcp"}
ARROW_OK = {("delega", "agAgent", "agAgent"), ("usa", "agAgent", "agResource"),
            ("usa", "agAgent", "agMcp"), ("task", "agTask", "agAgent"),
            ("trigger", "agWebhook", "agAgent")}


def validate_graph(ctx, obj):
    """Valida un organigrama entero (para org_edit del director, decisión U).
    Devuelve None si es válido, o el texto del error."""
    if not isinstance(obj, dict) or obj.get("type") != "orchestrator":
        return "el JSON debe ser un objeto con type='orchestrator'"
    nodos, flechas = obj.get("nodos"), obj.get("flechas")
    if not isinstance(nodos, list) or not isinstance(flechas, list):
        return "faltan las listas nodos/flechas"
    seen = {}
    for n in nodos:
        try:
            nid = int(n.get("id"))
        except (TypeError, ValueError):
            return "cada nodo necesita un id entero"
        if nid in seen:
            return f"id de nodo repetido: {nid}"
        if n.get("type") not in NODE_TYPES:
            return f"tipo de nodo inválido: {n.get('type')}"
        seen[nid] = n
    for f in flechas:
        try:
            a, b = int(f.get("fromId")), int(f.get("toId"))
        except (TypeError, ValueError):
            return "cada flecha necesita fromId/toId enteros"
        if a not in seen or b not in seen:
            return f"flecha con punta inexistente ({a}→{b})"
        combo = (f.get("kind"), seen[a].get("type"), seen[b].get("type"))
        if combo not in ARROW_OK:
            return f"flecha inválida: {combo[0]} {combo[1]}→{combo[2]}"
    for n in nodos:
        if n.get("type") == "agResource":
            rpid = (n.get("data") or {}).get("projectId")
            if rpid == ctx["pid"]:
                return "un recurso no puede ser el propio orquestador"
            if rpid and not ctx["project_meta"](rpid):
                return f"el recurso «{n.get('titulo')}» apunta a un proyecto que no es de esta carpeta"
    return None


def load_graph(ctx):
    tree = _read_json(ctx["graph_path"], None)
    if not tree or tree.get("type") != "orchestrator":
        raise OrchError(400, "el proyecto no es un orquestador o no está sincronizado")
    nodos = {int(n["id"]): n for n in tree.get("nodos", [])}
    flechas = tree.get("flechas", [])
    return {"nodos": nodos, "flechas": flechas}


class OrchError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _agent(graph, node_id):
    n = graph["nodos"].get(int(node_id))
    if not n or n.get("type") != "agAgent":
        raise OrchError(400, f"el nodo {node_id} no es un agente")
    return n


def delega_targets(graph, node_id):
    """Agentes a los que `node_id` puede delegar (flechas delega salientes)."""
    out = []
    for f in graph["flechas"]:
        if f.get("kind") == "delega" and int(f.get("fromId", -1)) == int(node_id):
            t = graph["nodos"].get(int(f["toId"]))
            if t and t.get("type") == "agAgent":
                out.append(t)
    return out


def resources_of(graph, node_id):
    """Recursos conectados por `usa` desde el agente."""
    out = []
    for f in graph["flechas"]:
        if f.get("kind") == "usa" and int(f.get("fromId", -1)) == int(node_id):
            r = graph["nodos"].get(int(f["toId"]))
            if r and r.get("type") == "agResource" and (r.get("data") or {}).get("projectId"):
                out.append(r)
    return out


def _resolve_target(graph, node_id, name_or_id):
    """Resuelve el destino de delegar por nombre (case-insensitive) o id."""
    wanted = str(name_or_id or "").strip().lower()
    for t in delega_targets(graph, node_id):
        if str(t["id"]) == wanted or (t.get("titulo") or "").strip().lower() == wanted:
            return t
    return None


# ===================== memoria (N/R/S) =====================

def mem_read(ctx, node_id):
    return _read_json(_mem_path(ctx, node_id), [])


def mem_chars(ctx, node_id):
    try:
        return os.path.getsize(_mem_path(ctx, node_id))
    except OSError:
        return 0


def mem_append(ctx, node_id, kind, texto, chat_id=None):
    mem = mem_read(ctx, node_id)
    mem.append({"id": "m" + uuid.uuid4().hex[:10], "kind": kind, "chatId": chat_id,
                "ts": int(time.time() * 1000), "texto": texto})
    _write_json(_mem_path(ctx, node_id), mem)


def mem_clear(ctx, node_id):
    try:
        os.remove(_mem_path(ctx, node_id))
    except OSError:
        pass


def chat_read(ctx, node_id):
    return _read_json(_chat_path(ctx, node_id), {"chatId": None, "messages": []})


def chat_append(ctx, node_id, role, text, chat_id):
    c = chat_read(ctx, node_id)
    c["chatId"] = chat_id
    c["messages"].append({"role": role, "text": text, "ts": int(time.time() * 1000)})
    _write_json(_chat_path(ctx, node_id), c)


def chat_clear(ctx, node_id):
    """Borra la charla del nodo Y sus entradas de memoria (decisión S)."""
    c = chat_read(ctx, node_id)
    chat_id = c.get("chatId")
    try:
        os.remove(_chat_path(ctx, node_id))
    except OSError:
        pass
    if chat_id:
        mem = [m for m in mem_read(ctx, node_id) if m.get("chatId") != chat_id]
        _write_json(_mem_path(ctx, node_id), mem)
    return {"ok": True, "removedChatId": chat_id}


# ===================== adapters LLM (Anthropic / OpenAI-compat) =====================

def _http_json(url, headers, body):
    req = urllib.request.Request(url, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {}
        msg = detail.get("error", {}).get("message") if isinstance(detail.get("error"), dict) else None
        raise OrchError(502, f"la API respondió {e.code}: {msg or e.reason}")
    except Exception as e:
        raise OrchError(502, f"no pude hablar con la API: {e}")


class AnthropicChat:
    provider = "anthropic"

    def __init__(self, key, model, effort):
        self.key, self.model, self.effort = key, model, effort
        self.base = os.environ.get("DMO_ANTHROPIC_BASE", "https://api.anthropic.com")

    def tools_spec(self, tools):
        return [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
                for t in tools]

    def user_msg(self, text):
        return {"role": "user", "content": [{"type": "text", "text": text}]}

    def call(self, system, messages, tools):
        body = {"model": self.model, "max_tokens": 8192, "system": system,
                "messages": messages, "tools": self.tools_spec(tools)}
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        r = _http_json(self.base + "/v1/messages",
                       {"x-api-key": self.key, "anthropic-version": "2023-06-01"}, body)
        text, calls = "", []
        for b in r.get("content", []):
            if b.get("type") == "text":
                text += b.get("text", "")
            elif b.get("type") == "tool_use":
                calls.append({"id": b["id"], "name": b["name"], "input": b.get("input") or {}})
        usage = r.get("usage", {})
        return {"text": text, "tool_calls": calls,
                "usage": {"in": usage.get("input_tokens", 0), "out": usage.get("output_tokens", 0)},
                "assistant_msg": {"role": "assistant", "content": r.get("content", [])}}

    def tool_results_msg(self, results):
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": x["id"], "content": x["content"],
             **({"is_error": True} if x.get("is_error") else {})} for x in results]}


class GeminiChat:
    """Google Gemini (generateContent). Transcript nativo: contents con role
    user/model y parts. Function calling nativo; con thinking, el `thoughtSignature`
    del functionCall TIENE que volver en el turno siguiente o la API tira 400."""
    provider = "google"
    # esfuerzo → thinkingBudget (tokens); "dinámico" = -1 (lo decide el modelo)
    BUDGET = {"off": 0, "dinámico": -1, "dinamico": -1, "low": 1024, "medium": 8192, "high": 24576}

    def __init__(self, key, model, effort=None):
        self.key, self.model, self.effort = key, model, effort
        self.base = os.environ.get("DMO_GOOGLE_BASE",
                                   "https://generativelanguage.googleapis.com/v1beta/models")

    def tools_spec(self, tools):
        return [{"functionDeclarations": [
            {"name": t["name"], "description": t["description"], "parameters": t["schema"]}
            for t in tools]}]

    def user_msg(self, text):
        return {"role": "user", "parts": [{"text": text}]}

    def call(self, system, messages, tools):
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": messages, "tools": self.tools_spec(tools)}
        budget = self.BUDGET.get(self.effort) if self.effort else None
        if budget is not None:
            body["generationConfig"] = {"thinkingConfig": {"thinkingBudget": budget}}
        url = f"{self.base}/{self.model}:generateContent?key={urllib.parse.quote(self.key)}"
        r = _http_json(url, {}, body)
        parts = ((r.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text, calls, keep = "", [], []
        for i, p in enumerate(parts):
            if p.get("thought"):
                continue                      # parte de "pensamiento": no es la respuesta
            if p.get("text"):
                text += p["text"]
                keep.append({"text": p["text"]})
            elif p.get("functionCall"):
                fc = p["functionCall"] or {}
                name = fc.get("name") or ""
                # el id lleva el NOMBRE adelante: Gemini identifica los resultados por
                # nombre de función, no por id (ver tool_results_msg)
                calls.append({"id": f"{name}#{i}", "name": name, "input": fc.get("args") or {}})
                part = {"functionCall": {"name": name, "args": fc.get("args") or {}}}
                if p.get("thoughtSignature"):
                    part["thoughtSignature"] = p["thoughtSignature"]
                keep.append(part)
        u = r.get("usageMetadata") or {}
        return {"text": text, "tool_calls": calls,
                "usage": {"in": u.get("promptTokenCount", 0), "out": u.get("candidatesTokenCount", 0)},
                "assistant_msg": {"role": "model", "parts": keep or [{"text": text or ""}]}}

    def tool_results_msg(self, results):
        # el name sale del id ("<name>#<i>") para que un resultado de CONTROL (que
        # el motor devuelve con name="control") igual matchee la función llamada
        return [{"role": "user", "parts": [
            {"functionResponse": {"name": str(x["id"]).split("#")[0],
                                  "response": {"result": x["content"]}}} for x in results]}]


class OpenAIChat:
    provider = "openai"

    def __init__(self, key, model, effort=None, base=None):
        self.key, self.model = key, model
        self.base = base or os.environ.get("DMO_OPENAI_BASE", "https://api.openai.com/v1/chat/completions")

    def tools_spec(self, tools):
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                  "parameters": t["schema"]}} for t in tools]

    def user_msg(self, text):
        return {"role": "user", "content": text}

    def call(self, system, messages, tools):
        msgs = [{"role": "system", "content": system}] + messages
        body = {"model": self.model, "messages": msgs, "tools": self.tools_spec(tools)}
        r = _http_json(self.base, {"Authorization": "Bearer " + self.key}, body)
        m = (r.get("choices") or [{}])[0].get("message") or {}
        calls = [{"id": tc["id"], "name": tc["function"]["name"],
                  "input": json.loads(tc["function"].get("arguments") or "{}")}
                 for tc in (m.get("tool_calls") or [])]
        usage = r.get("usage", {})
        return {"text": m.get("content") or "", "tool_calls": calls,
                "usage": {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0)},
                "assistant_msg": m}

    def tool_results_msg(self, results):
        return [{"role": "tool", "tool_call_id": x["id"], "content": x["content"]} for x in results]


def pick_cred(keys, node, provider, cred_id):
    """Resuelve QUÉ credencial usa el nodo: la elegida por id (`ia.credId`) o, si no
    eligió ninguna (nodos viejos), la primera cargada de ese proveedor."""
    quien = f"el nodo «{node.get('titulo') or node.get('id')}»"
    if cred_id:
        c = next((x for x in creds_of(keys) if x.get("id") == cred_id), None)
        if c:
            return c
        raise OrchError(400, f"{quien} tiene elegida una credencial que ya no existe en este "
                             "orquestador — elegí otra en el nodo (o cargala con el botón Keys)")
    disp = creds_of(keys, provider)
    if not disp:
        raise OrchError(400, f"{quien} usa {PROV_LABEL.get(provider, provider)} y no hay ninguna "
                             "credencial de ese proveedor cargada — agregala con el botón Keys y "
                             "elegila en el nodo")
    return disp[0]


def make_adapter(ctx, node):
    ia = (node.get("data") or {}).get("ia") or {}
    keys = KEYS.get(ctx["pid"]) or keys_read(ctx)
    cred = pick_cred(keys, node, ia.get("provider") or "anthropic", ia.get("credId"))
    provider = cred.get("provider") or ia.get("provider")
    key = cred.get("key")
    if provider == "anthropic":
        return AnthropicChat(key, ia.get("model") or "claude-sonnet-5", ia.get("effort"))
    if provider == "google":
        return GeminiChat(key, ia.get("model") or "gemini-2.5-flash", ia.get("effort"))
    if provider == "openai":
        return OpenAIChat(key, ia.get("model") or "gpt-4o")
    if provider == "other":
        if not cred.get("url"):
            raise OrchError(400, f"la credencial «{cred.get('nombre')}» es de 'Otra API' y no tiene URL")
        return OpenAIChat(key, ia.get("model") or "gpt-4o", None, base=cred["url"])
    raise OrchError(400, f"el nodo «{node.get('titulo')}» usa el proveedor '{provider}', que el motor "
                         "todavía no soporta (APIs: Anthropic, Google y OpenAI-compatible; CLI: Claude Code)")


# ===================== tools =====================

def _s(desc, props=None, req=None):
    return {"description": desc,
            "schema": {"type": "object", "properties": props or {}, "required": req or []}}


def control_tools(graph, node_id):
    names = ", ".join(f"«{t.get('titulo') or t['id']}»" for t in delega_targets(graph, node_id)) or "(nadie)"
    tools = [
        dict(name="responder", **_s(
            "Terminá tu trabajo respondiéndole a quien te llamó (o al usuario si sos la raíz). SIEMPRE cerrá tu turno con esta tool.",
            {"mensaje": {"type": "string", "description": "tu respuesta/resultado, concreto"}}, ["mensaje"])),
        dict(name="preguntar_al_usuario", **_s(
            "Pausa el trabajo y le pregunta al USUARIO humano (validación, decisión, contexto que falta). Usala ante la duda.",
            {"pregunta": {"type": "string"}}, ["pregunta"])),
        dict(name="limpiar_memoria", **_s(
            "Borra la memoria persistente: la tuya (sin argumento) o la de un subordinado directo (nombre). Usala cuando una tarea se cierra.",
            {"agente": {"type": "string", "description": "nombre del subordinado (opcional; default: vos)"}})),
    ]
    if delega_targets(graph, node_id):
        node = graph["nodos"].get(int(node_id)) or {}
        if (node.get("data") or {}).get("secuencial"):
            # agente SECUENCIAL (decisión W): delega de a UNO, nunca forkea
            tools.insert(0, dict(name="delegar", **_s(
                f"Delegá trabajo a UN subordinado directo y ESPERÁ su respuesta (podés delegar a: {names}). "
                "Sos un agente SECUENCIAL: delegás de a UNO por vez, nunca en paralelo — si necesitás a "
                "varios, andá uno por uno esperando cada respuesta. El mensaje debe ser concreto y verificable.",
                {"agente": {"type": "string", "description": "nombre del agente destino"},
                 "mensaje": {"type": "string", "description": "qué tiene que hacer, con el contexto necesario"}},
                ["agente", "mensaje"])))
        else:
            tools.insert(0, dict(name="delegar", **_s(
                f"Delegá trabajo a subordinados directos y ESPERÁ su(s) respuesta(s) (podés delegar a: {names}). "
                "Para UNO usá `agente`; para VARIOS EN PARALELO usá `agentes` y elegí `join`: \"todos\" te despierta "
                "UNA vez con todas las respuestas juntas (default, para validar en conjunto) o \"cada_una\" te "
                "despierta con CADA respuesta a medida que llega. El mensaje debe ser concreto y verificable.",
                {"agente": {"type": "string", "description": "nombre del agente destino (delegación simple)"},
                 "agentes": {"type": "array", "items": {"type": "string"},
                             "description": "varios destinos: trabajan EN PARALELO"},
                 "mensaje": {"type": "string", "description": "qué tienen que hacer, con el contexto necesario"},
                 "join": {"type": "string", "enum": ["todos", "cada_una"],
                          "description": "cómo te despierto si delegás a varios (default: todos)"}},
                ["mensaje"])))
    return tools


PERM_LEVEL = {"leer": 0, "editar": 1, "ejecutar": 2}


def resource_tools(ctx, graph, node_id, author):
    """(tools, executors, notas para el system) de los recursos `usa` del agente."""
    tools, execs, notes = [], {}, []
    for r in resources_of(graph, node_id):
        rid = f"r{r['id']}"
        rpid = r["data"]["projectId"]
        perm = PERM_LEVEL.get((r["data"] or {}).get("permiso") or "editar", 1)
        meta = ctx["project_meta"](rpid)          # {name, type} o None
        if not meta:
            notes.append(f"- {rid}: (proyecto borrado — no usar)")
            continue
        rtype = meta.get("type")
        label = f"{meta.get('name')} ({rtype}, permiso {r['data'].get('permiso')})"
        notes.append(f"- {rid}: {label}")
        if rtype == "editor":
            _editor_tools(ctx, rid, rpid, perm, tools, execs, author)
        else:
            _diagram_tools(ctx, rid, rpid, rtype, perm, tools, execs)
    return tools, execs, notes


def _fs(fn, *args):
    code, payload = fn(*args)
    return json.dumps(payload, ensure_ascii=False), code >= 400


def _editor_tools(ctx, rid, rpid, perm, tools, execs, author):
    app = ctx["app_dir"]
    def add(name, spec, fn):
        tools.append(dict(name=f"{rid}_{name}", **spec))
        execs[f"{rid}_{name}"] = fn
    add("fs_tree", _s("Lista UN nivel del proyecto editor (dirs primero).",
                      {"dir": {"type": "string"}}),
        lambda i: _fs(editorfs.fs_tree, app, rpid, i.get("dir") or ""))
    add("fs_read", _s("Lee un archivo (ruta relativa).", {"path": {"type": "string"}}, ["path"]),
        lambda i: _fs(editorfs.fs_read, app, rpid, i.get("path")))
    add("fs_grep", _s("Busca texto en los archivos.", {"q": {"type": "string"}, "glob": {"type": "string"}}, ["q"]),
        lambda i: _fs(editorfs.fs_grep, app, rpid, i.get("q"), i.get("glob") or ""))
    def sv_ctx():
        return ctx["sv_dir_of"](rpid), editorfs.get_target(app, rpid)
    def sv_list(i):
        svd, _t = sv_ctx()
        return json.dumps(sourcever.sv_list(svd), ensure_ascii=False), False
    add("sv_list", _s("Historial de versiones del proyecto."), sv_list)
    if perm >= 1:
        add("fs_write", _s("Escribe un archivo COMPLETO (crea dirs).",
                           {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
            lambda i: _fs(editorfs.fs_write, app, rpid, i.get("path"), i.get("content") or ""))
        add("fs_mkdir", _s("Crea un directorio.", {"path": {"type": "string"}}, ["path"]),
            lambda i: _fs(editorfs.fs_mkdir, app, rpid, i.get("path")))
        add("fs_rename", _s("Renombra/mueve dentro del proyecto.",
                            {"from": {"type": "string"}, "to": {"type": "string"}}, ["from", "to"]),
            lambda i: _fs(editorfs.fs_rename, app, rpid, i.get("from"), i.get("to")))
        add("fs_delete", _s("Borra archivo o dir (recursivo).", {"path": {"type": "string"}}, ["path"]),
            lambda i: _fs(editorfs.fs_delete, app, rpid, i.get("path")))
        def sv_save(i):
            svd, t = sv_ctx()
            return json.dumps(sourcever.sv_save(svd, t, author, i.get("note") or ""), ensure_ascii=False), False
        add("sv_save", _s("Guarda una VERSIÓN (snapshot) del proyecto. Usala ANTES de una tanda de cambios.",
                          {"note": {"type": "string"}}), sv_save)
        def sv_restore(i):
            svd, t = sv_ctx()
            return json.dumps(sourcever.sv_restore(svd, t, i.get("id"), author), ensure_ascii=False), False
        add("sv_restore", _s("Vuelve el proyecto a una versión (con snapshot de seguridad previo). Solo si te lo piden.",
                             {"id": {"type": "string"}}, ["id"]), sv_restore)
    if perm >= 2:
        add("fs_exec", _s("Ejecuta un comando de shell en el proyecto (timeout 60s).",
                          {"cmd": {"type": "string"}}, ["cmd"]),
            lambda i: _fs(editorfs.fs_exec, app, rpid, i.get("cmd")))


def org_tools(ctx, graph, run, node):
    """Tools del DIRECTOR (decisión U): auto-edición del organigrama en el que vive.
    org_edit valida + snapshotea + refresca el grafo del run EN VIVO (los frames
    nuevos y los próximos turnos lo ven) y NUNCA dispara runs."""
    tools, execs = [], {}
    def org_view(i):
        tree = _read_json(ctx["graph_path"], None)
        if tree is None:
            return "el organigrama no está sincronizado", True
        return json.dumps(tree, ensure_ascii=False), False
    tools.append(dict(name="org_view", **_s(
        "Devuelve el JSON completo del organigrama de TU empresa (este orquestador).")))
    execs["org_view"] = org_view

    def org_edit(i):
        raw = i.get("json")
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            return f"JSON inválido: {e}", True
        err = validate_graph(ctx, obj)
        if err:
            return f"organigrama inválido: {err}", True
        try:                                     # snapshot pre-edición (decisión I)
            d = os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "snapshots")
            os.makedirs(d, exist_ok=True)
            if os.path.isfile(ctx["graph_path"]):
                shutil.copyfile(ctx["graph_path"],
                                os.path.join(d, f"{run['id']}-org-{int(time.time() * 1000)}.json"))
        except Exception:
            pass
        _write_json(ctx["graph_path"], obj)
        ctx["notify_edit"](ctx["pid"])
        with LOCK:                               # refresh en vivo del grafo del run
            graph["nodos"] = {int(n["id"]): n for n in obj.get("nodos", [])}
            graph["flechas"] = obj.get("flechas", [])
            emit(run, "log", nodeId=node["id"], text="👑 editó el organigrama (org_edit)")
        return "OK: organigrama actualizado. NO se disparó ningún run.", False
    tools.append(dict(name="org_edit", **_s(
        "Reemplaza el organigrama ENTERO de tu empresa con un JSON válido de tipo orchestrator "
        "(usá org_view primero y respetá su esquema EXACTO; conservá lo que no te pidieron tocar). "
        "Editar NUNCA ejecuta nada: hacé SOLO los cambios que te pidieron.",
        {"json": {"type": "string", "description": "el tree.json completo del orquestador"}}, ["json"])))
    execs["org_edit"] = org_edit
    return tools, execs


def _diagram_tools(ctx, rid, rpid, rtype, perm, tools, execs):
    def view(i):
        tree = _read_json(ctx["tree_path_of"](rpid), None)
        if tree is None:
            return "el proyecto no está sincronizado", True
        return json.dumps(tree, ensure_ascii=False), False
    tools.append(dict(name=f"{rid}_view_tree", **_s(f"Devuelve el JSON del diagrama ({rtype}).")))
    execs[f"{rid}_view_tree"] = view
    if perm >= 1:
        def set_tree(i):
            raw = i.get("json")
            try:
                obj = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as e:
                return f"JSON inválido: {e}", True
            if not isinstance(obj, dict) or obj.get("type") != rtype:
                return f"el JSON debe ser un objeto con type='{rtype}'", True
            _write_json(ctx["tree_path_of"](rpid), obj)
            ctx["notify_edit"](rpid)
            return "OK: diagrama actualizado.", False
        tools.append(dict(name=f"{rid}_set_tree", **_s(
            f"Reemplaza el diagrama ENTERO con un JSON válido de tipo {rtype} (respetá su esquema EXACTO).",
            {"json": {"type": "string", "description": "el tree.json completo"}}, ["json"])))
        execs[f"{rid}_set_tree"] = set_tree


# ===================== system prompt =====================

def _skill_body(rtype):
    content = TYPE_SKILLS.get(f"diagramind-{rtype.lower()}") or ""
    return content.split("---\n", 2)[-1].strip() if content else ""


def build_system(ctx, graph, node, notes):
    d = node.get("data") or {}
    nid = node["id"]
    partes = [
        f"Sos «{node.get('titulo') or 'agente'}», un empleado IA de la empresa (IA Orchestrator de DiagraMinder).",
        f"TU ROL: {d.get('rol') or '(sin rol definido — trabajá con criterio)'}",
    ]
    targets = delega_targets(graph, nid)
    if targets:
        if d.get("secuencial"):
            partes.append("SUBORDINADOS (sos SECUENCIAL: delegás de a UNO por vez con `delegar` y esperás "
                          "cada respuesta — nunca en paralelo): " +
                          "; ".join(f"«{t.get('titulo') or t['id']}» ({(t.get('data') or {}).get('rol', '')[:80]})" for t in targets))
        else:
            partes.append("SUBORDINADOS (podés delegarles con la tool `delegar` — a varios EN PARALELO con "
                          "`agentes` — y esperás su(s) respuesta(s)): " +
                          "; ".join(f"«{t.get('titulo') or t['id']}» ({(t.get('data') or {}).get('rol', '')[:80]})" for t in targets))
    if notes:
        partes.append("TUS RECURSOS (tools con el prefijo indicado):\n" + "\n".join(notes))
    if (d.get("memoria") or {}).get("enabled", True):
        mem = mem_read(ctx, nid)
        if mem:
            lines = [f"- [{time.strftime('%Y-%m-%d %H:%M', time.localtime(m['ts'] / 1000))}] {m['texto']}"
                     for m in mem[-12:]]
            partes.append("TU MEMORIA (trabajos y charlas anteriores):\n" + "\n".join(lines))
    tipos = {ctx["project_meta"](r["data"]["projectId"]).get("type")
             for r in resources_of(graph, nid)
             if ctx["project_meta"](r["data"].get("projectId"))}
    for t in sorted(x for x in tipos if x and x != "editor"):
        body = _skill_body(t)
        if body:
            partes.append(f"ESQUEMA del tipo {t} (para view/set_tree):\n{body[:3500]}")
    if d.get("director"):
        partes.append("👑 SOS DIRECTOR de esta empresa (decisión U): podés gestionar el organigrama "
                      "con `org_view` y `org_edit` — crear/editar/borrar agentes, recursos y flechas, "
                      "incluso modificarte a vos mismo. REGLAS del director: editar el grafo NUNCA "
                      "dispara runs; hacé SOLO los cambios que te pidieron y conservá el resto.")
        body = _skill_body("orchestrator")
        if body:
            partes.append(f"ESQUEMA del organigrama (para org_view/org_edit):\n{body[:3500]}")
    # sin recursos no tiene DÓNDE trabajar: que pregunte en vez de improvisar
    if not notes:
        partes.append(
            "NO TENÉS NINGÚN RECURSO ASIGNADO: no hay ningún archivo ni diagrama que puedas "
            "tocar. Si la tarea implica leer o escribir algo, NO la intentes — usá "
            "`preguntar_al_usuario` y pedí que te cableen un recurso. Si es solo pensar o "
            "responder, hacela normal.")
    elif not _has_editor(ctx, graph, nid):
        partes.append(
            "OJO: no tenés ningún recurso EDITOR asignado, así que no tenés dónde escribir "
            "código ni archivos sueltos — solo los diagramas de arriba. Si lo que te pidieron "
            "necesita un proyecto de código, preguntá con `preguntar_al_usuario` para que te "
            "asignen uno en vez de improvisar.")
    partes.append(
        "REGLAS: 1) Trabajá SOLO en lo que te pidieron. 2) Todo el trabajo sobre archivos va DENTRO "
        "de tus recursos, con SUS tools: son el único lugar donde podés escribir y donde el usuario "
        "puede revisar y deshacer lo que hiciste. 3) Usá `preguntar_al_usuario` ante decisiones "
        "importantes o contexto faltante. 4) Cerrá SIEMPRE tu turno con `responder` (resumen concreto y "
        "verificable). 5) En proyectos editor, guardá una versión (sv_save) antes de una tanda de cambios. "
        "6) Respondé en español."
    )
    return "\n\n".join(partes)


# ===================== eventos / estado =====================
# emit / set_node_state / add_spend asumen el LOCK tomado (mutan run).

def emit(run, kind, **data):
    run["events"].append({"kind": kind, "ts": int(time.time() * 1000), **data})


def set_node_state(ctx, run, node_id, status):
    st = run["nodeStates"].setdefault(str(node_id), {})
    st["status"] = status
    chars = mem_chars(ctx, node_id)
    st["memChars"] = chars
    st["memHeavy"] = chars > MEM_HEAVY_CHARS
    emit(run, "node", nodeId=node_id, status=status, memHeavy=st["memHeavy"])


def add_spend(run, node_id, usage):
    for key in (str(node_id), "total"):
        s = run["spend"].setdefault(key, {"turns": 0, "in": 0, "out": 0})
        s["turns"] += 1
        s["in"] += usage.get("in", 0)
        s["out"] += usage.get("out", 0)
    emit(run, "spend", total=run["spend"]["total"])


# ===================== snapshots pre-ejecución (decisión I) =====================

def snapshot_resources(ctx, run, graph, node):
    name = node.get("titulo") or f"nodo {node['id']}"
    for r in resources_of(graph, node["id"]):
        if PERM_LEVEL.get((r["data"] or {}).get("permiso") or "editar", 1) < 1:
            continue
        rpid = r["data"]["projectId"]
        meta = ctx["project_meta"](rpid)
        if not meta:
            continue
        try:
            if meta.get("type") == "editor":
                svd = ctx["sv_dir_of"](rpid)
                target = editorfs.get_target(ctx["app_dir"], rpid)
                if svd and target:
                    sourcever.sv_save(svd, target, f"IA ({name})", f"(auto) run {run['id']}: turno de {name}")
            else:
                src = ctx["tree_path_of"](rpid)
                if src and os.path.isfile(src):
                    d = os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "snapshots")
                    os.makedirs(d, exist_ok=True)
                    shutil.copyfile(src, os.path.join(d, f"{run['id']}-{node['id']}-{rpid}.json"))
            emit(run, "log", nodeId=node["id"], text=f"snapshot pre-turno de {meta.get('name')}")
        except Exception as e:
            emit(run, "log", nodeId=node["id"], text=f"snapshot falló ({meta.get('name')}): {e}")


# ===================== locks por recurso/agente (decisión E) =====================
# run["locks"]: key -> frameId. Keys: "res:<projectId>" (recurso con permiso de
# escritura) y "node:<nodeId>" (un empleado hace UNA cosa a la vez). Un frame toma
# TODOS sus locks o ninguno (sin deadlock posible) y los mantiene entre iteraciones
# de tools; los suelta al responder, delegar o preguntar.

def _lock_keys(graph, frame):
    keys = [f"node:{frame['nodeId']}"]
    for r in resources_of(graph, frame["nodeId"]):
        if PERM_LEVEL.get((r["data"] or {}).get("permiso") or "editar", 1) >= 1:
            keys.append(f"res:{r['data']['projectId']}")
    return keys


def _try_locks(graph, run, frame):
    keys = _lock_keys(graph, frame)
    for k in keys:
        holder = run["locks"].get(k)
        if holder and holder != frame["id"]:
            return False
    for k in keys:
        run["locks"][k] = frame["id"]
    return True


def _release_locks(run, frame_id):
    for k in [k for k, v in run["locks"].items() if v == frame_id]:
        del run["locks"][k]


# ===================== frames =====================

def _new_frame(ctx, graph, run, node, entry_kind, initial_text, parent_id=None):
    """Crea un frame listo para correr (snapshot pre-ejecución incluido)."""
    run["_fseq"] = run.get("_fseq", 0) + 1
    fid = f"f{run['_fseq']}"
    provider = ((node.get("data") or {}).get("ia") or {}).get("provider") or "anthropic"
    base = {"id": fid, "nodeId": node["id"], "parentId": parent_id, "provider": provider,
            "entry": entry_kind, "status": "ready", "iters": 0, "firstText": initial_text,
            "inbox": [{"text": initial_text}], "join": None, "waiting": {}, "collected": []}
    if provider in CLI_PROVIDERS:
        if provider != "local":
            raise OrchError(400, f"el nodo «{node.get('titulo')}» usa '{provider}': como cabeza CLI "
                                 "por ahora solo está soportado Claude Code (fase 4 v1)")
        frame = {**base, "kind": "cli", "sessionId": None}
    else:
        make_adapter(ctx, node)   # valida ya mismo que la key del proveedor esté
        frame = {**base, "kind": "api", "messages": [], "pendingToolId": None, "stash": []}
    run["frames"][fid] = frame
    snapshot_resources(ctx, run, graph, node)
    set_node_state(ctx, run, node["id"], "running")
    emit(run, "log", nodeId=node["id"], text=f"→ entra trabajo: {initial_text[:200]}")
    return frame


def _finish_node(ctx, run, graph, frame, mensaje):
    """responder: registra memoria y marca el nodo como terminado."""
    node = _agent(graph, frame["nodeId"])
    d = node.get("data") or {}
    if (d.get("memoria") or {}).get("enabled", True):
        chat_id = run.get("chatId") if frame["entry"] == "chat" else None
        mem_append(ctx, node["id"], frame["entry"],
                   f"Tarea: {frame['firstText'][:400]} → Resultado: {mensaje[:700]}", chat_id)
    set_node_state(ctx, run, node["id"], "done")
    emit(run, "log", nodeId=node["id"], text=f"← responde: {mensaje[:200]}")


def _do_responder(ctx, graph, run, frame, mensaje):
    """Cierra el frame y entrega la respuesta al padre (o cierra el run si es la raíz)."""
    _finish_node(ctx, run, graph, frame, mensaje)
    frame["status"] = "done"
    _release_locks(run, frame["id"])
    parent_id = frame.get("parentId")
    if not parent_id:
        run["final"] = mensaje
        if run["entry"] == "chat":
            chat_append(ctx, run["rootNodeId"], "assistant", mensaje, run["chatId"])
        emit(run, "final", text=mensaje)
        return
    parent = run["frames"][parent_id]
    child = _agent(graph, frame["nodeId"])
    texto = f"Respuesta de «{child.get('titulo') or child['id']}»: {mensaje}"
    parent["waiting"].pop(frame["id"], None)
    if parent.get("join") == "cada_una":
        quedan = len(parent["waiting"])
        if quedan:
            texto += f"\n(seguís esperando {quedan} respuesta(s) más)"
        parent["inbox"].append({"text": texto})
        if parent["status"] == "waiting_children":
            parent["status"] = "ready"
    else:                                  # join "todos": una sola vuelta con todo
        parent["collected"].append(texto)
        if not parent["waiting"]:
            parent["inbox"].append({"text": "\n\n".join(parent["collected"])})
            parent["collected"] = []
            parent["status"] = "ready"


def _implicit_end(ctx, graph, run, frame, texto):
    """Turno que terminó sin acción de control: si espera hijos, sigue esperando;
    si no, es un responder implícito con el texto."""
    if frame["waiting"]:
        frame["status"] = "waiting_children"
        set_node_state(ctx, run, frame["nodeId"], "waiting")
        _release_locks(run, frame["id"])
        emit(run, "log", nodeId=frame["nodeId"], text="sigue esperando las respuestas pendientes")
        return
    _do_responder(ctx, graph, run, frame, texto)


def _do_delegar(ctx, graph, run, frame, node, inp):
    """Resuelve destino(s) y forkea el token (decisión D). Devuelve un texto de
    error (sin tocar nada) o None si delegó y el frame quedó esperando hijos."""
    if frame["waiting"]:
        faltan = ", ".join(f"«{v}»" for v in frame["waiting"].values())
        return f"ya tenés delegaciones en curso ({faltan}): esperá esas respuestas antes de volver a delegar"
    wanted = [w for w in (inp.get("agentes") if isinstance(inp.get("agentes"), list) else []) if str(w or "").strip()]
    if str(inp.get("agente") or "").strip():
        wanted.insert(0, inp["agente"])
    seen, names = set(), []
    for w in wanted:
        k = str(w).strip().lower()
        if k not in seen:
            seen.add(k)
            names.append(str(w))
    if not names:
        return "decí a quién delegás: `agente` (uno) o `agentes` (varios en paralelo)"
    targets, ids = [], set()
    for w in names:
        t = _resolve_target(graph, node["id"], w)
        if not t:
            return f"no podés delegar a «{w}»: no está conectado por una flecha delega"
        if t["id"] not in ids:
            ids.add(t["id"])
            targets.append(t)
    if len(targets) > 1 and (node.get("data") or {}).get("secuencial"):
        return ("sos un agente SECUENCIAL (lo definió el humano): delegá de a UNO con `agente` "
                "y esperá cada respuesta antes de la siguiente")
    join = "cada_una" if str(inp.get("join") or "").strip().lower() in ("cada_una", "cada una") else "todos"
    msg = str(inp.get("mensaje") or "")
    frame["join"], frame["collected"] = join, []
    frame["status"] = "waiting_children"
    set_node_state(ctx, run, node["id"], "waiting")
    _release_locks(run, frame["id"])
    texto = f"«{node.get('titulo') or node['id']}» te delega: {msg}"
    for t in targets:
        child = _new_frame(ctx, graph, run, t, "delegado", texto, parent_id=frame["id"])
        frame["waiting"][child["id"]] = t.get("titulo") or str(t["id"])
    if len(targets) > 1:
        emit(run, "log", nodeId=node["id"],
             text=f"fork: delegó en paralelo a {len(targets)} agentes (join: {join})")
    return None


# ===================== scheduler + workers =====================

def start_run(ctx, entry_kind, root_node_id, initial_text, api_keys, max_turns=None, trigger=None):
    """Crea y lanza un run (entry task, chat o trigger). Devuelve el run dict."""
    with LOCK:
        prev = RUNS.get(ctx["pid"])
        if prev and prev["status"] in ("running", "waiting_human", "paused"):
            raise OrchError(409, "ya hay un run en curso en este orquestador: "
                                 "esperá, respondé lo pendiente o matalo")
        graph = load_graph(ctx)
        root = _agent(graph, root_node_id)
        # las credenciales son SIEMPRE las del proyecto (decisión T); `api_keys` del
        # request se ignora — queda en la firma por compat con webs viejas
        KEYS[ctx["pid"]] = keys_read(ctx)
        run = {
            "id": "run" + uuid.uuid4().hex[:8], "projectId": ctx["pid"], "entry": entry_kind,
            "status": "running", "rootNodeId": root["id"], "final": None, "error": None,
            "turns": 0, "maxTurns": max_turns or MAX_TURNS_DEFAULT,
            "chatId": None, "pending": None, "pendings": [],
            "trigger": trigger, "frames": {}, "locks": {}, "nodeStates": {}, "spend": {},
            "events": [], "createdAt": int(time.time() * 1000),
            "_fseq": 0, "_workers": 0,
        }
        if entry_kind == "chat":
            c = chat_read(ctx, root["id"])
            run["chatId"] = c.get("chatId") or ("c" + uuid.uuid4().hex[:8])
            chat_append(ctx, root["id"], "user", initial_text, run["chatId"])
        _new_frame(ctx, graph, run, root, entry_kind, initial_text)
        RUNS[ctx["pid"]] = run
        _save(ctx, run)
    _spawn(ctx)
    return run


def _spawn(ctx):
    """Arranca el scheduler del run si no está vivo; si está, lo despierta."""
    rt = _rt(ctx["pid"])
    with LOCK:
        if rt["alive"]:
            rt["cv"].notify_all()
            return
        rt["alive"] = True
    threading.Thread(target=_loop, args=(ctx,), daemon=True).start()


def _loop(ctx):
    """Scheduler: lanza un worker por frame listo (si consigue sus locks), decide
    queued/waiting_human/done y corta por presupuesto, pausa o kill."""
    run = RUNS.get(ctx["pid"])
    rt = _rt(ctx["pid"])
    cv = rt["cv"]
    with cv:
        try:
            graph = load_graph(ctx)
            while run["status"] == "running":
                if run.get("_kill"):
                    run["status"] = "killed"
                    break
                active = [f for f in run["frames"].values() if f["status"] != "done"]
                if not active:
                    run["status"] = "done"
                    break
                if run.get("_pause"):
                    if run["_workers"] == 0:
                        run.pop("_pause", None)
                        run["status"] = "paused"
                        break
                elif run["turns"] >= run["maxTurns"]:
                    if run["_workers"] == 0:
                        raise OrchError(400, f"presupuesto agotado ({run['maxTurns']} turnos). "
                                             "Subí maxTurns o dividí la tarea")
                else:
                    for f in sorted((x for x in run["frames"].values() if x["status"] in ("ready", "queued")),
                                    key=lambda x: int(x["id"][1:])):
                        if run["turns"] + run["_workers"] >= run["maxTurns"]:
                            break
                        if _try_locks(graph, run, f):
                            f["status"] = "running"
                            run["_workers"] += 1
                            threading.Thread(target=_worker, args=(ctx, graph, run, f), daemon=True).start()
                        elif f["status"] != "queued":
                            f["status"] = "queued"
                            set_node_state(ctx, run, f["nodeId"], "queued")
                            emit(run, "log", nodeId=f["nodeId"],
                                 text="en cola: espera un recurso/agente ocupado por otra rama")
                    if run["_workers"] == 0:
                        blocked = {x["status"] for x in active}
                        if blocked <= {"waiting_human", "waiting_children"} and "waiting_human" in blocked:
                            run["status"] = "waiting_human"
                            break
                        if blocked == {"waiting_children"}:
                            raise OrchError(500, "el run quedó trabado (agentes esperando sin hijos activos)")
                cv.wait(timeout=0.25)
        except OrchError as e:
            run["status"], run["error"] = "error", e.msg
        except Exception as e:
            run["status"], run["error"] = "error", f"error interno del motor: {e}"
        if run["status"] == "error":
            emit(run, "status", status="error", error=run["error"])
        else:
            emit(run, "status", status=run["status"])
        _save(ctx, run)
        _archive_run(ctx, run)
        rt["alive"] = False
        cv.notify_all()
    _after_run(ctx, run)                # callback del webhook + drenar la cola (V)


def _worker(ctx, graph, run, frame):
    """Un turno de agente (una rama). El LLM/CLI corre SIN el lock global."""
    cv = _rt(ctx["pid"])["cv"]
    try:
        if frame["kind"] == "cli":
            _turn_cli(ctx, graph, run, frame)
        else:
            _turn_api(ctx, graph, run, frame)
    except OrchError as e:
        with cv:
            if run["status"] == "running":
                run["status"], run["error"] = "error", e.msg
    except Exception as e:
        with cv:
            if run["status"] == "running":
                run["status"], run["error"] = "error", f"error interno del motor: {e}"
    finally:
        with cv:
            run["_workers"] -= 1
            _save(ctx, run)
            cv.notify_all()


def _deliver_inbox(adapter, frame):
    """Vuelca el inbox del frame a su transcript (con el LOCK tomado). El primer
    ítem resuelve el tool_result pendiente (delegar/preguntar); el resto entra
    como mensajes de usuario."""
    items, frame["inbox"] = frame["inbox"], []
    if frame.get("pendingToolId"):
        first = items.pop(0) if items else {"text": "(continuá)"}
        results = frame["stash"] + [{"id": frame["pendingToolId"], "name": "control",
                                     "content": first["text"],
                                     **({"is_error": True} if first.get("is_error") else {})}]
        if adapter.provider == "anthropic":
            frame["messages"].append(adapter.tool_results_msg(results))
        else:
            frame["messages"].extend(adapter.tool_results_msg(results))
        frame["pendingToolId"] = None
        frame["stash"] = []
    for it in items:
        frame["messages"].append(adapter.user_msg(it["text"]))


def _append_results(adapter, frame, results):
    if adapter.provider == "anthropic":
        frame["messages"].append(adapter.tool_results_msg(results))
    else:
        frame["messages"].extend(adapter.tool_results_msg(results))


def _reject_control(frame, control, results, texto):
    """Devuelve un error a la acción de control sin suspender el frame: el próximo
    turno entrega stash + el error como tool_result."""
    frame["pendingToolId"] = control["id"]
    frame["stash"] = results
    frame["inbox"].insert(0, {"text": texto, "is_error": True})
    frame["status"] = "ready"


def _turn_api(ctx, graph, run, frame):
    node = _agent(graph, frame["nodeId"])
    adapter = make_adapter(ctx, node)
    author = f"IA ({node.get('titulo') or node['id']})"
    ctrl = control_tools(graph, node["id"])
    rtools, rexecs, rnotes = resource_tools(ctx, graph, node["id"], author)
    mtools, mexecs, mnotes = mcp_tools(ctx, graph, node["id"])
    rtools, rnotes = rtools + mtools, rnotes + mnotes
    rexecs = {**rexecs, **mexecs}
    if (node.get("data") or {}).get("director"):
        otools, oexecs = org_tools(ctx, graph, run, node)
        rtools = otools + rtools
        rexecs = {**rexecs, **oexecs}
    system = build_system(ctx, graph, node, rnotes)
    tools = ctrl + rtools
    cv = _rt(ctx["pid"])["cv"]

    with cv:
        _deliver_inbox(adapter, frame)
        set_node_state(ctx, run, node["id"], "running")
    res = adapter.call(system, frame["messages"], tools)      # ← paralelismo real

    with cv:
        if run["status"] != "running" or run.get("_kill"):
            return
        run["turns"] += 1
        frame["iters"] += 1
        add_spend(run, node["id"], res["usage"])
        frame["messages"].append(res["assistant_msg"])
        if not res["tool_calls"]:
            _implicit_end(ctx, graph, run, frame, res["text"] or "(sin respuesta)")
            _save(ctx, run)
            return
        if frame["iters"] > MAX_TOOL_ITERS:
            _implicit_end(ctx, graph, run, frame,
                          (res["text"] or "") + "\n(corté: demasiadas iteraciones en este turno)")
            _save(ctx, run)
            return
        control, plain, ignored = None, [], []
        for tc in res["tool_calls"]:
            if control is not None:
                ignored.append({"id": tc["id"], "name": tc["name"], "is_error": True,
                                "content": "ignorada: primero se resuelve la acción de control anterior"})
            elif tc["name"] in CONTROL_TOOLS:
                control = tc
            else:
                plain.append(tc)

    # tools de recursos FUERA del lock global (el frame ya tiene sus locks de recurso)
    results = [_exec_tool(ctx, graph, run, node, rexecs, tc) for tc in plain] + ignored

    with cv:
        if run["status"] != "running" or run.get("_kill"):
            return
        if control is None:
            _append_results(adapter, frame, results)
            frame["status"] = "ready"
            _save(ctx, run)
            return
        inp = control["input"] or {}
        if control["name"] == "responder":
            if frame["waiting"]:
                faltan = ", ".join(f"«{v}»" for v in frame["waiting"].values())
                _reject_control(frame, control, results,
                                f"todavía esperás las respuestas de: {faltan} — no podés responder hasta que lleguen")
            else:
                _do_responder(ctx, graph, run, frame, str(inp.get("mensaje") or ""))
        elif control["name"] == "delegar":
            err = _do_delegar(ctx, graph, run, frame, node, inp)
            if err:
                _reject_control(frame, control, results, err)
            else:
                frame["pendingToolId"] = control["id"]
                frame["stash"] = results
        else:                                   # preguntar_al_usuario
            pregunta = str(inp.get("pregunta") or "")
            frame["pendingToolId"] = control["id"]
            frame["stash"] = results
            frame["status"] = "waiting_human"
            _release_locks(run, frame["id"])
            run["pendings"].append({"frameId": frame["id"], "nodeId": node["id"], "question": pregunta})
            run["pending"] = run["pendings"][0]
            set_node_state(ctx, run, node["id"], "asking")
            emit(run, "ask", nodeId=node["id"], question=pregunta)
        _save(ctx, run)


def _exec_tool(ctx, graph, run, node, rexecs, tc):
    """Ejecuta una tool de recurso/memoria. Corre SIN el lock global (toma el LOCK
    solo para emitir eventos)."""
    name, inp = tc["name"], tc["input"]
    with LOCK:
        emit(run, "log", nodeId=node["id"], text=f"tool {name}({json.dumps(inp, ensure_ascii=False)[:160]})")
    try:
        if name == "limpiar_memoria":
            who = (inp.get("agente") or "").strip()
            if not who:
                mem_clear(ctx, node["id"])
                return {"id": tc["id"], "name": name, "content": "OK: tu memoria quedó limpia."}
            target = _resolve_target(graph, node["id"], who)
            if not target:
                return {"id": tc["id"], "name": name, "is_error": True,
                        "content": f"«{who}» no es un subordinado directo tuyo"}
            mem_clear(ctx, target["id"])
            with LOCK:
                set_node_state(ctx, run, target["id"],
                               run["nodeStates"].get(str(target["id"]), {}).get("status", "idle"))
            return {"id": tc["id"], "name": name, "content": f"OK: memoria de «{target.get('titulo')}» limpia."}
        fn = rexecs.get(name)
        if not fn:
            return {"id": tc["id"], "name": name, "is_error": True, "content": f"tool desconocida: {name}"}
        content, is_err = fn(inp)
        out = {"id": tc["id"], "name": name, "content": content[:60000]}
        if is_err:
            out["is_error"] = True
        return out
    except sourcever.SvError as e:
        return {"id": tc["id"], "name": name, "is_error": True, "content": e.msg}
    except Exception as e:
        return {"id": tc["id"], "name": name, "is_error": True, "content": f"error ejecutando {name}: {e}"}


# ===================== turnos CLI (Claude Code — fase 4) =====================
# La cabeza del nodo es el CLI: acceso DIRECTO a los targets de sus recursos editor
# (--add-dir) y a los tree.json de los diagramas (cwd = la carpeta del mirror, con
# las skills instaladas). Las acciones de control van por PROTOCOLO DE TEXTO: la
# última línea del turno debe ser `CONTROL: {json}`. La continuidad entre
# delegaciones/preguntas usa --resume (sesión por frame).

CLI_PROTOCOL = (
    "PROTOCOLO DE CONTROL (OBLIGATORIO — sos un empleado del orquestador): tu respuesta "
    "DEBE terminar con UNA línea exacta `CONTROL: {json}` con una de estas acciones:\n"
    'CONTROL: {"accion":"responder","mensaje":"<tu resultado, concreto y verificable>"}\n'
    'CONTROL: {"accion":"delegar","agente":"<nombre subordinado>","mensaje":"<qué tiene que hacer>"}\n'
    'CONTROL: {"accion":"delegar","agentes":["<nombre A>","<nombre B>"],"join":"todos","mensaje":"<qué tienen que hacer>"} '
    '— delega a VARIOS EN PARALELO; join "todos" = te despierto una vez con todas las respuestas juntas, '
    '"cada_una" = te despierto con cada respuesta a medida que llega\n'
    'CONTROL: {"accion":"preguntar_al_usuario","pregunta":"<qué necesitás que decida el humano>"}\n'
    "Además podés emitir líneas `CONTROL: {\"accion\":\"limpiar_memoria\",\"agente\":\"<opcional>\"}` "
    "ANTES de la línea final. Si delegás o preguntás, vas a recibir la(s) respuesta(s) en el próximo turno "
    "de esta misma conversación. NUNCA termines sin la línea CONTROL."
)


def _cli_workspace(ctx, node_id):
    """cwd propio del agente CLI: `<orch>/<pid>/cli/<nodeId>`.

    ANTES el cwd era la carpeta ENTERA del mirror, así que un agente CLI podía leer y
    escribir CUALQUIER proyecto de la carpeta aunque no fuera recurso suyo (decisión X,
    2026-07-26). Ahora arranca en un directorio propio y vacío —con las skills
    instaladas— y lo único que alcanza es lo que se le agrega por `--add-dir`, que sale
    de SU cableado."""
    d = os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "cli", str(node_id))
    os.makedirs(d, exist_ok=True)
    return d


def _cli_resource_notes(ctx, graph, node):
    """Cómo llega un agente CLI a sus recursos (decisión X). Devuelve
    (notas, add_dirs, mcp, exec_ok):

    - `confinado` OFF → **tools nativas con los --add-dir acotados**: solo la carpeta
      real de SUS editores y el subdirectorio de SUS diagramas. Conserva Read/Write/
      Bash (codea bien), pero no ve el resto de la carpeta.
    - `confinado` ON → **todo por el MCP del editor**: un server `dmfs<id>` por editor
      contra ESTE backend, así cada escritura pasa por `editorfs` con su chequeo
      "path escapes target", igual que un agente API. Sin --add-dir para editores.

    `exec_ok` es True si ALGÚN recurso tiene permiso `ejecutar`: si no, se le saca Bash.
    """
    confinado = bool((node.get("data") or {}).get("confinado"))
    notes, add_dirs, mcp, exec_ok = [], [], {}, False
    for r in resources_of(graph, node["id"]):
        rpid = r["data"]["projectId"]
        meta = ctx["project_meta"](rpid)
        if not meta:
            continue
        perm = (r["data"] or {}).get("permiso") or "editar"
        lvl = PERM_LEVEL.get(perm, 1)
        exec_ok = exec_ok or lvl >= 2
        if meta.get("type") == "editor":
            target = editorfs.get_target(ctx["app_dir"], rpid)
            if not target:
                notes.append(f"- «{meta.get('name')}» (editor): sin carpeta configurada — no usar")
                continue
            if confinado:
                name = f"dmfs{r['id']}"
                mcp[name] = {"projectId": rpid, "perm": lvl}
                tools = "leer" if lvl < 1 else ("leer/escribir/ejecutar" if lvl >= 2 else "leer/escribir")
                notes.append(f"- «{meta.get('name')}» (editor, permiso {perm}): usá SOLO las tools "
                             f"`mcp__{name}__*` ({tools}). NO tenés la carpeta montada: no la busques "
                             "en el disco, todo va por esas tools.")
            elif lvl >= 1:
                add_dirs.append(target)
                notes.append(f"- «{meta.get('name')}» (editor, permiso {perm}): la carpeta real "
                             f"{target} — trabajá DIRECTO ahí con tus herramientas de archivos")
            else:
                notes.append(f"- «{meta.get('name')}» (editor, permiso leer): NO lo tenés montado "
                             "(las tools nativas no distinguen lectura de escritura). Si necesitás "
                             "leerlo, pedile al humano que lo ponga en modo confinado.")
        else:
            # diagrama: es UN archivo dentro del mirror. Se monta su subdirectorio, no
            # la carpeta entera (en confinado también: no hay MCP de diagramas todavía).
            sub = os.path.join(ctx.get("work_dir") or ctx["app_dir"], safe_name(meta.get("name") or rpid))
            if lvl >= 1:
                add_dirs.append(sub)
            rel = os.path.join(sub, "tree.json")
            notes.append(f"- «{meta.get('name')}» ({meta.get('type')}, permiso {perm}): el diagrama "
                         f"{rel} — editalo respetando el esquema EXACTO de su tipo "
                         f"(skill diagramind-{str(meta.get('type')).lower()})")
    return notes, add_dirs, mcp, exec_ok


def _has_editor(ctx, graph, node_id):
    """¿tiene algún recurso EDITOR cableado? Es el único lugar donde un agente puede
    escribir archivos de código, así que sin uno hay que mandarlo a preguntar."""
    for r in resources_of(graph, node_id):
        meta = ctx["project_meta"]((r.get("data") or {}).get("projectId"))
        if meta and meta.get("type") == "editor":
            return True
    return False


def _cli_system(ctx, graph, node, notes):
    d = node.get("data") or {}
    any_editor = _has_editor(ctx, graph, node["id"])
    partes = [
        f"Sos «{node.get('titulo') or 'agente'}», un empleado IA de la empresa (IA Orchestrator de DiagraMinder).",
        f"TU ROL: {d.get('rol') or '(sin rol definido — trabajá con criterio)'}",
    ]
    targets = delega_targets(graph, node["id"])
    if targets:
        if d.get("secuencial"):
            partes.append("SUBORDINADOS (sos SECUENCIAL: delegá de a UNO — accion delegar con `agente` — y "
                          "esperá cada respuesta; NUNCA uses `agentes` múltiple): " +
                          "; ".join(f"«{t.get('titulo') or t['id']}» ({(t.get('data') or {}).get('rol', '')[:80]})" for t in targets))
        else:
            partes.append("SUBORDINADOS (podés delegarles — a varios EN PARALELO — y esperás su(s) respuesta(s)): " +
                          "; ".join(f"«{t.get('titulo') or t['id']}» ({(t.get('data') or {}).get('rol', '')[:80]})" for t in targets))
    if notes:
        partes.append("TUS RECURSOS:\n" + "\n".join(notes))
    if (d.get("memoria") or {}).get("enabled", True):
        mem = mem_read(ctx, node["id"])
        if mem:
            lines = [f"- [{time.strftime('%Y-%m-%d %H:%M', time.localtime(m['ts'] / 1000))}] {m['texto']}"
                     for m in mem[-12:]]
            partes.append("TU MEMORIA (trabajos y charlas anteriores):\n" + "\n".join(lines))
    if d.get("director"):
        partes.append("👑 SOS DIRECTOR de esta empresa (decisión U): podés gestionar el organigrama "
                      f"editando DIRECTO el archivo {ctx['graph_path']} (seguí la skill "
                      "diagramind-orchestrator y respetá su esquema EXACTO — org completo, ids únicos, "
                      "contadores). Podés crear/editar/borrar agentes, recursos y flechas, incluso a "
                      "vos mismo. REGLAS: editar el grafo NUNCA dispara runs; hacé SOLO los cambios "
                      "que te pidieron y conservá el resto.")
    if d.get("confinado"):
        # Decirle QUÉ tiene, no solo qué le falta: si no, gasta turnos buscando Read/Bash
        # y "descubriendo" que no están. Los nombres son los del server de cada recurso.
        partes.append(
            "SOS UN AGENTE CONFINADO (decisión X): NO tenés la carpeta de proyectos montada y "
            "tus tools nativas de archivos (Read/Write/Edit/Bash/Glob/Grep) están DESHABILITADAS. "
            "No las busques ni intentes `cd`: no existen para vos.\n"
            "TRABAJÁS CON ESTAS, una tanda por recurso editor (`<id>` es el del recurso, mirá "
            "TUS RECURSOS más arriba):\n"
            "- `mcp__dmfs<id>__fs_tree` — listar UN nivel (el equivalente a `ls`; pasá `dir` "
            "para bajar a un subdirectorio)\n"
            "- `mcp__dmfs<id>__fs_read` — leer un archivo · `mcp__dmfs<id>__fs_grep` — buscar "
            "texto (acepta `glob`, es tu `grep -r`)\n"
            "- `mcp__dmfs<id>__fs_write` — escribir un archivo COMPLETO (leé primero, mandá todo "
            "el contenido nuevo) · `fs_mkdir` / `fs_rename` / `fs_delete`\n"
            "- `mcp__dmfs<id>__sv_save` — guardá una VERSIÓN antes de una tanda de cambios · "
            "`sv_list` / `sv_restore`\n"
            "- `mcp__dmfs<id>__fs_exec` — SOLO si el recurso tiene permiso `ejecutar`: es una "
            "shell REAL con el cwd en la carpeta del editor, ahí corrés `ls`, `find`, tests, "
            "build, git, lo que necesites.\n"
            "Es el mismo juego de herramientas que usan los agentes de API: alcanza de sobra "
            "para codear. Si te falta algo, pedilo con preguntar_al_usuario en vez de inventar "
            "un camino por afuera.")
    reglas = ["Trabajá SOLO en lo que te pidieron.",
              "Tocá ÚNICAMENTE tus recursos (no otros proyectos de la carpeta).",
              "Respondé en español, concreto."]
    if not notes:
        # sin recursos cableados no tiene DÓNDE trabajar: que pregunte en vez de
        # inventar una ruta (antes tenía el mirror entero montado y "algo" hacía).
        partes.append(
            "NO TENÉS NINGÚN RECURSO ASIGNADO. No hay ninguna carpeta ni archivo que puedas "
            "tocar, y no tenés que buscarte uno: si la tarea implica leer o escribir código o "
            "archivos, NO la intentes — usá `CONTROL: {\"accion\":\"preguntar_al_usuario\", "
            "\"pregunta\":\"...\"}` y pedí que te cableen un recurso editor (o preguntá dónde "
            "querés que trabaje). Si la tarea es solo pensar o responder, hacela normal.")
    elif not any_editor:
        partes.append(
            "OJO: no tenés ningún recurso EDITOR asignado, así que no tenés dónde escribir "
            "código ni archivos sueltos — solo los diagramas de arriba. Si lo que te pidieron "
            "necesita un proyecto de código, NO improvises una ruta: preguntá con "
            "`CONTROL: {\"accion\":\"preguntar_al_usuario\",\"pregunta\":\"...\"}` para que te "
            "asignen un recurso editor.")
    else:
        reglas.insert(1, "Todo el trabajo sobre archivos va DENTRO de tus recursos editor: son "
                         "el único lugar donde podés escribir y donde el usuario puede revisar y "
                         "deshacer lo que hiciste. No crees archivos en ningún otro lado.")
    partes.append("REGLAS: " + " ".join(f"{i}) {r}" for i, r in enumerate(reglas, 1)))
    partes.append(CLI_PROTOCOL)
    return "\n\n".join(partes)


def _parse_control(text):
    """(acciones_limpiar, accion_final|None, texto_sin_lineas_control)."""
    limpiar, final, visibles = [], None, []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("CONTROL:"):
            try:
                obj = json.loads(stripped[len("CONTROL:"):].strip())
            except Exception:
                visibles.append(line)
                continue
            if obj.get("accion") == "limpiar_memoria":
                limpiar.append(obj)
            else:
                final = obj
        else:
            visibles.append(line)
    return limpiar, final, "\n".join(visibles).strip()


# tools que expone el MCP del editor (editor_mcp.py) según el permiso del recurso
MCP_FS_READ = ["fs_tree", "fs_read", "fs_grep", "sv_list"]
MCP_FS_WRITE = ["fs_write", "fs_mkdir", "fs_rename", "fs_delete", "sv_save", "sv_restore"]
MCP_FS_EXEC = ["fs_exec"]
# nativas mínimas para tocar un diagrama-recurso cuando el agente está confinado:
# su único --add-dir es el subdirectorio de ESE diagrama, así que quedan encerradas ahí
CLI_DIAGRAM_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]


def _rm(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _cli_cmd(ctx, graph, node, frame, message, cli_bin):
    """(cmd, cwd, mcp_cfg_path) del turno CLI. Acá vive la decisión X: qué alcanza a
    tocar el agente. El cwd ya NO es la carpeta del mirror (ver `_cli_workspace`)."""
    d = node.get("data") or {}
    confinado = bool(d.get("confinado"))
    notes, add_dirs, mcp, exec_ok = _cli_resource_notes(ctx, graph, node)
    system = _cli_system(ctx, graph, node, notes)
    ia = d.get("ia") or {}
    kw = EFFORT_THINK.get(ia.get("effort") or "", "")
    msg = message + (f"\n\n{kw}" if kw else "")
    cwd = _cli_workspace(ctx, node["id"])
    try:
        install_skills(cwd)               # las skills viven en SU workspace, no en el mirror
    except Exception:
        pass
    cmd = [cli_bin, "-p", msg, "--output-format", "stream-json", "--verbose",
           "--model", map_model(ia.get("model")), "--permission-mode", "acceptEdits",
           "--append-system-prompt", system]
    for x in add_dirs:
        cmd += ["--add-dir", x]

    cfg = None
    if confinado:
        # whitelist: SOLO las tools del MCP (una por editor, según permiso) y, si tiene
        # diagramas cableados, las nativas de archivo — que solo alcanzan sus add_dirs.
        servers, allowed = {}, []
        for name, info in mcp.items():
            servers[name] = {
                **_self_cmd(),
                "env": {"DMFS_URL": ctx.get("local_url") or "http://127.0.0.1:8765",
                        "DMFS_TOKEN": ctx.get("local_token") or "",
                        "DMFS_PROJECT": info["projectId"], "DMFS_AUTH": "local"},
            }
            tools = list(MCP_FS_READ)
            if info["perm"] >= 1:
                tools += MCP_FS_WRITE
            if info["perm"] >= 2:
                tools += MCP_FS_EXEC
            allowed += [f"mcp__{name}__{t}" for t in tools]
        if add_dirs:
            allowed += CLI_DIAGRAM_TOOLS
        if servers:
            fd, cfg = tempfile.mkstemp(prefix=f"dmorch-mcp-{node['id']}-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": servers}, f)
            os.chmod(cfg, 0o600)          # tiene el token del backend local
            cmd += ["--mcp-config", cfg]
        # sin tools permitidas el agente no puede hacer NADA con archivos: igual puede
        # razonar y responder, que es lo correcto para un nodo sin recursos cableados.
        cmd += ["--allowedTools", ",".join(allowed)]
    else:
        # blacklist: conserva su toolbelt nativo, acotado por los --add-dir de arriba.
        # Bash es la vía de escape de los --add-dir, así que se la damos SOLO si algún
        # recurso suyo tiene permiso `ejecutar`.
        off = ["WebFetch", "WebSearch"] + ([] if exec_ok else ["Bash"])
        cmd += ["--disallowedTools"] + off
    if frame.get("sessionId"):
        cmd += ["--resume", str(frame["sessionId"])]
    return cmd, cwd, cfg


def _run_cli_turn(ctx, graph, run, node, frame, message):
    """Lanza `claude -p` para un turno del agente y devuelve (texto, session_id, costo)."""
    cli_bin = find_claude()
    if not cli_bin:
        raise OrchError(400, f"el nodo «{node.get('titulo')}» usa Claude Code y el binario `claude` "
                             "no está en esta máquina")
    cmd, cwd, cfg = _cli_cmd(ctx, graph, node, frame, message, cli_bin)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, encoding="utf-8", errors="replace")
    except Exception as e:
        _rm(cfg)
        raise OrchError(400, f"no pude lanzar Claude Code: {e}")
    frame["_mcpCfg"] = cfg
    rt = _rt(ctx["pid"])
    rt["procs"][frame["id"]] = proc
    session_id, result_text, texts, cost, deadline = None, None, [], 0.0, time.time() + CLI_TIMEOUT
    try:
        for line in proc.stdout:
            if time.time() > deadline:
                proc.terminate()
                raise OrchError(400, f"turno CLI de «{node.get('titulo')}» superó el tope de {CLI_TIMEOUT // 60} min")
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                session_id = obj.get("session_id") or session_id
            elif obj.get("type") == "assistant":
                for b in (obj.get("message", {}).get("content") or []):
                    if b.get("type") == "text" and b.get("text"):
                        texts.append(b["text"])
                    elif b.get("type") == "tool_use":
                        with LOCK:
                            emit(run, "log", nodeId=node["id"], text=f"cli tool {b.get('name', '?')}")
            elif obj.get("type") == "result":
                session_id = obj.get("session_id") or session_id
                result_text = obj.get("result")
                cost = obj.get("total_cost_usd") or 0.0
                if obj.get("is_error"):
                    proc.wait()
                    raise OrchError(502, f"Claude Code devolvió un error: {result_text or '?'}")
        proc.wait()
    finally:
        rt["procs"].pop(frame["id"], None)
        _rm(frame.pop("_mcpCfg", None))   # el config MCP lleva el token del backend local
    if run.get("_kill"):
        raise OrchError(400, "turno CLI cancelado")
    if proc.returncode not in (0, None) and result_text is None:
        err = (proc.stderr.read() or "").strip()[:400]
        raise OrchError(502, f"Claude Code salió con código {proc.returncode}: {err}")
    return (result_text or "\n\n".join(texts) or ""), session_id, cost


def _turn_cli(ctx, graph, run, frame):
    node = _agent(graph, frame["nodeId"])
    cv = _rt(ctx["pid"])["cv"]
    with cv:
        items, frame["inbox"] = frame["inbox"], []
        message = "\n\n".join(("⚠ " if it.get("is_error") else "") + it["text"] for it in items) or "(continuá)"
        frame["iters"] += 1
        set_node_state(ctx, run, node["id"], "running")
    text, session_id, cost = _run_cli_turn(ctx, graph, run, node, frame, message)   # ← sin lock
    with cv:
        if run["status"] != "running" or run.get("_kill"):
            return
        run["turns"] += 1
        if session_id:
            frame["sessionId"] = session_id
        add_spend(run, node["id"], {"in": 0, "out": 0})
        if cost:
            for key in (str(node["id"]), "total"):
                sp = run["spend"].setdefault(key, {"turns": 0, "in": 0, "out": 0})
                sp["usd"] = round(sp.get("usd", 0.0) + cost, 6)
        limpiar, accion, visible = _parse_control(text)

        for lm in limpiar:
            who = (lm.get("agente") or "").strip()
            if not who:
                mem_clear(ctx, node["id"])
                emit(run, "log", nodeId=node["id"], text="limpió su memoria")
            else:
                target = _resolve_target(graph, node["id"], who)
                if target:
                    mem_clear(ctx, target["id"])
                    emit(run, "log", nodeId=node["id"], text=f"limpió la memoria de «{target.get('titulo')}»")

        if accion is None:
            _implicit_end(ctx, graph, run, frame, visible or "(sin respuesta)")
        elif accion.get("accion") == "responder":
            if frame["waiting"]:
                faltan = ", ".join(f"«{v}»" for v in frame["waiting"].values())
                frame["inbox"].append({"text": f"todavía esperás las respuestas de: {faltan} — "
                                               "no podés responder hasta que lleguen", "is_error": True})
                frame["status"] = "ready"
            else:
                _do_responder(ctx, graph, run, frame, str(accion.get("mensaje") or visible or "(sin respuesta)"))
        elif frame["iters"] > MAX_TOOL_ITERS:
            _implicit_end(ctx, graph, run, frame, visible + "\n(corté: demasiadas iteraciones)")
        elif accion.get("accion") == "delegar":
            err = _do_delegar(ctx, graph, run, frame, node, accion)
            if err:
                frame["inbox"].append({"text": err + ". Elegí un subordinado válido o respondé.", "is_error": True})
                frame["status"] = "ready"
        elif accion.get("accion") == "preguntar_al_usuario":
            frame["status"] = "waiting_human"
            _release_locks(run, frame["id"])
            p = {"frameId": frame["id"], "nodeId": node["id"], "question": str(accion.get("pregunta") or "")}
            run["pendings"].append(p)
            run["pending"] = run["pendings"][0]
            set_node_state(ctx, run, node["id"], "asking")
            emit(run, "ask", nodeId=node["id"], question=p["question"])
        else:
            frame["inbox"].append({"text": f"acción CONTROL desconocida: {accion.get('accion')}. "
                                           "Usá responder/delegar/preguntar_al_usuario.", "is_error": True})
            frame["status"] = "ready"
        _save(ctx, run)


# ===================== API de alto nivel (la usa server.py) =====================

def _active_nodes(run):
    frames = run.get("frames") or {}
    return [f["nodeId"] for f in sorted(frames.values(), key=lambda x: int(x["id"][1:]))
            if f["status"] != "done"]


def get_state(ctx):
    run = RUNS.get(ctx["pid"])
    if not run:
        run = _read_json(_run_path(ctx), None)
        if run and run.get("status") in ("running", "waiting_human", "paused"):
            run["status"] = "error"
            run["error"] = "el backend se reinició durante el run — relanzalo"
            _write_json(_run_path(ctx), run)
    if not run:
        return {"run": None}
    slim = {k: v for k, v in run.items()
            if not str(k).startswith("_") and k not in ("stack", "frames", "locks", "events")}
    slim["stackNodes"] = _active_nodes(run)
    return {"run": slim}


def answer(ctx, text, node_id=None):
    """Respuesta del humano a UNA pregunta pendiente (por nodeId si hay varias)."""
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] not in ("running", "waiting_human"):
        raise OrchError(409, "no hay ninguna pregunta pendiente (¿se reinició el backend?)")
    with LOCK:
        pendings = run.get("pendings") or []
        if not pendings:
            raise OrchError(409, "no hay ninguna pregunta pendiente")
        if node_id is not None:
            match = [p for p in pendings if str(p["nodeId"]) == str(node_id)]
            if not match:
                raise OrchError(404, "ese nodo no tiene una pregunta pendiente")
            p = match[0]
        elif len(pendings) == 1:
            p = pendings[0]
        else:
            raise OrchError(400, "hay varias preguntas pendientes: indicá nodeId")
        frame = run["frames"][p["frameId"]]
        emit(run, "log", nodeId=p["nodeId"], text=f"usuario responde: {text[:200]}")
        # la respuesta del humano va PRIMERA (resuelve el tool_result de la pregunta)
        frame["inbox"].insert(0, {"text": f"Respuesta del usuario: {text}"})
        frame["status"] = "ready"
        pendings.remove(p)
        run["pending"] = pendings[0] if pendings else None
        if run["status"] == "waiting_human":
            run["status"] = "running"
        _save(ctx, run)
    _spawn(ctx)
    return {"ok": True}


def chat_message(ctx, node_id, text, api_keys, max_turns=None):
    """Mini-chat (decisión S): un mensaje al nodo = un run con root en ese nodo."""
    run = start_run(ctx, "chat", node_id, text, api_keys, max_turns)
    return {"runId": run["id"], "chatId": run["chatId"]}


def pause(ctx):
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] != "running":
        raise OrchError(409, "no hay un run corriendo")
    with LOCK:
        run["_pause"] = True
        _rt(ctx["pid"])["cv"].notify_all()
    return {"ok": True}


def resume(ctx):
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] != "paused":
        raise OrchError(409, "no hay un run pausado")
    if not KEYS.get(ctx["pid"]):
        KEYS[ctx["pid"]] = keys_read(ctx)   # las del proyecto persisten en disco
    with LOCK:
        run["status"] = "running"
        _save(ctx, run)
    _spawn(ctx)
    return {"ok": True}


def kill(ctx):
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] not in ("running", "waiting_human", "paused"):
        raise OrchError(409, "no hay un run activo")
    rt = _rt(ctx["pid"])
    direct = False
    with LOCK:
        if run["status"] == "running":
            run["_kill"] = True
            for proc in list(rt["procs"].values()):
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            rt["cv"].notify_all()
        else:                       # waiting_human / paused: el scheduler no está vivo
            run["status"] = "killed"
            emit(run, "status", status="killed")
            _save(ctx, run)
            _archive_run(ctx, run)
            direct = True
    if direct:
        _after_run(ctx, run)        # también acá se drena la cola de triggers (V)
    return {"ok": True}


def runs_list(ctx):
    """Historial de runs (nuevo → viejo). El run VIVO (si lo hay) va primero."""
    idx = _read_json(_runs_index_path(ctx), [])
    live = RUNS.get(ctx["pid"])
    if live and not live.get("_archived"):
        idx = [x for x in idx if x.get("id") != live["id"]]
        idx.insert(0, {**_run_summary(live), "live": True})
    return {"runs": idx}


def run_detail(ctx, run_id):
    """Un run completo (con sus events). Del vivo en RAM o del archivo."""
    live = RUNS.get(ctx["pid"])
    if live and live["id"] == run_id:
        d = {k: v for k, v in live.items()
             if not str(k).startswith("_") and k not in ("stack", "frames", "locks")}
        d["live"] = live["status"] in ("running", "waiting_human", "paused")
        d["stackNodes"] = _active_nodes(live)
        return {"run": d}
    data = _read_json(os.path.join(_runs_dir(ctx), f"{run_id}.json"), None)
    if not data:
        raise OrchError(404, "no existe ese run en el historial")
    return {"run": data}


def events_since(ctx, since):
    run = RUNS.get(ctx["pid"])
    if not run:
        return [], 0, "none"
    evs = run["events"][since:]
    return evs, since + len(evs), run["status"]


# ===================== RADIOGRAFÍA DE UN AGENTE (botones Context / Tools) =====================
# Lo que la web muestra tiene que ser lo que el motor MANDA — no una reconstrucción
# paralela que se desincroniza al primer cambio. Por eso `inspect_node` llama a las
# MISMAS funciones que usa el turno (build_system / control_tools / resource_tools /
# mcp_tools / org_tools) sobre el grafo del MIRROR. Responde dos preguntas:
#   1) "si gira AHORA, ¿qué recibe?" → system + tools calculados en vivo (el caso
#      «cablié un recurso, ¿lo ve?»: si no aparece acá, el agente NO lo tiene).
#   2) "¿qué se le mandó de verdad?" → el transcript del run vivo o, si no hay, el
#      del último terminado (`run.json`). Los runs ARCHIVADOS no guardan frames
#      (ver _archive_run), así que de ahí para atrás solo quedan los events/logs.
# Recordá que system y tools se REARMAN en cada turno: esto no es una foto del
# arranque, es lo que se manda una y otra vez.

# Toolbelt nativo de Claude Code. NO lo declara el motor (por eso no está en
# `control_tools`/`resource_tools`): lo trae el CLI y puede variar con su versión —
# se lista como REFERENCIA para que el usuario vea con qué trabaja un nodo CLI.
# Lo que sí controlamos son los flags: `--disallowedTools` y `--permission-mode`.
CLI_NATIVE_TOOLS = [
    ("Read", "Lee un archivo del disco."),
    ("Write", "Escribe un archivo completo."),
    ("Edit", "Reemplaza texto exacto dentro de un archivo."),
    ("Bash", "Ejecuta comandos de shell."),
    ("Glob", "Busca archivos por patrón."),
    ("Grep", "Busca texto dentro de los archivos."),
    ("Task", "Lanza subagentes propios del CLI."),
    ("TodoWrite", "Lista de tareas interna del turno."),
    ("NotebookEdit", "Edita notebooks Jupyter."),
    ("WebFetch", "Trae una URL."),
    ("WebSearch", "Busca en la web."),
]
CLI_DISALLOWED = ["WebFetch", "WebSearch"]


def _skill_catalog():
    """Las skills que `install_skills` deja en <work_dir>/.claude/skills/: el agente CLI
    las tiene disponibles aunque no sean tools. Nombre + description del frontmatter."""
    out = []
    for name, content in TYPE_SKILLS.items():
        desc = ""
        for line in content.splitlines():
            if line.startswith("description:"):
                desc = line[len("description:"):].strip()
                break
        out.append({"name": name, "description": desc})
    return out


def _norm_blocks(msg):
    """Normaliza un mensaje de CUALQUIERA de los 3 adapters (Anthropic `content` /
    Gemini `parts` / OpenAI plano) a {role, blocks:[{kind,name,text}]} para que la
    web pinte los tres transcripts igual."""
    role = msg.get("role") or "user"
    content, parts, out = msg.get("content"), msg.get("parts"), []
    if isinstance(parts, list):                                    # Gemini
        for p in parts:
            if p.get("text") is not None:
                out.append({"kind": "text", "text": p.get("text") or ""})
            elif p.get("functionCall"):
                fc = p.get("functionCall") or {}
                out.append({"kind": "tool_use", "name": fc.get("name") or "",
                            "text": json.dumps(fc.get("args") or {}, ensure_ascii=False)})
            elif p.get("functionResponse"):
                fr = p.get("functionResponse") or {}
                out.append({"kind": "tool_result", "name": fr.get("name") or "",
                            "text": str((fr.get("response") or {}).get("result", ""))})
    elif isinstance(content, list):                                # Anthropic
        for b in content:
            kind = b.get("type")
            if kind == "text":
                out.append({"kind": "text", "text": b.get("text") or ""})
            elif kind == "tool_use":
                out.append({"kind": "tool_use", "name": b.get("name") or "",
                            "text": json.dumps(b.get("input") or {}, ensure_ascii=False)})
            elif kind == "tool_result":
                out.append({"kind": "tool_result", "name": b.get("tool_use_id") or "",
                            "text": str(b.get("content") or ""), "error": bool(b.get("is_error"))})
    else:                                                          # OpenAI (content plano)
        if content:
            out.append({"kind": "tool_result" if role == "tool" else "text",
                        "name": msg.get("tool_call_id") or "", "text": str(content)})
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            out.append({"kind": "tool_use", "name": fn.get("name") or "",
                        "text": fn.get("arguments") or "{}"})
    if role in ("assistant", "model"):
        role = "assistant"
    elif any(b["kind"] == "tool_result" for b in out):
        role = "tool"                                              # Anthropic los manda como user
    else:
        role = "user"
    return {"role": role, "blocks": out, "chars": sum(len(b.get("text") or "") for b in out)}


def _transcript_of(ctx, node_id):
    """Lo que EFECTIVAMENTE se le mandó a este nodo: los frames del run vivo (RAM,
    bajo LOCK) o, si no hay ninguno corriendo, los del último run terminado que
    quedó en run.json. Un frame = una delegación: cada uno arranca con messages
    vacío, así que acá se ve por qué un agente 'no se acuerda' de la anterior."""
    with LOCK:
        live = RUNS.get(ctx["pid"])
        src = json.loads(json.dumps({k: v for k, v in live.items()
                                     if not str(k).startswith("_")})) if live else None
    is_live = src is not None
    if src is None:
        src = _read_json(_run_path(ctx), None)
    if not src:
        return None
    frames = [f for f in (src.get("frames") or {}).values() if int(f["nodeId"]) == int(node_id)]
    frames.sort(key=lambda f: int(str(f["id"])[1:]))
    out = []
    for f in frames:
        out.append({"frameId": f["id"], "entry": f.get("entry"), "kind": f.get("kind"),
                    "status": f.get("status"), "parentId": f.get("parentId"),
                    "firstText": f.get("firstText") or "", "iters": f.get("iters", 0),
                    "sessionId": f.get("sessionId"),
                    "messages": [_norm_blocks(m) for m in (f.get("messages") or [])]})
    return {"runId": src.get("id"), "status": src.get("status"), "live": is_live,
            "createdAt": src.get("createdAt"), "frames": out}


def _stats_of(ctx, node_id):
    """Pestaña Info: en cuántos runs laburó este nodo y cuánto gastó. El acumulado
    sale de los runs ARCHIVADOS (runs/<id>.json guarda el spend por nodo, aunque no
    los frames) + el vivo/último. El estado actual sale del run en curso."""
    key = str(node_id)
    zero = {"turns": 0, "in": 0, "out": 0, "usd": 0.0}
    acc, runs = dict(zero), 0

    def add(dst, sp):
        for k in ("turns", "in", "out"):
            dst[k] += sp.get(k, 0) or 0
        dst["usd"] = round(dst["usd"] + (sp.get("usd", 0.0) or 0.0), 6)

    idx = _read_json(_runs_index_path(ctx), [])
    for row in idx:
        data = _read_json(os.path.join(_runs_dir(ctx), f"{row.get('id')}.json"), None)
        sp = ((data or {}).get("spend") or {}).get(key)
        if not sp:
            continue
        runs += 1
        add(acc, sp)

    with LOCK:
        live = RUNS.get(ctx["pid"])
        cur = json.loads(json.dumps({k: v for k, v in live.items()
                                     if not str(k).startswith("_")})) if live else None
    is_live = cur is not None
    if cur is None:
        cur = _read_json(_run_path(ctx), None)
    last = None
    if cur and not (is_live and cur.get("_archived")):
        sp = (cur.get("spend") or {}).get(key)
        archived = any(r.get("id") == cur.get("id") for r in idx)
        if sp:
            if not archived:                     # el vivo todavía no está en el índice
                runs += 1
                add(acc, sp)
            last = {"runId": cur.get("id"), "status": cur.get("status"), "live": is_live,
                    "createdAt": cur.get("createdAt"), **sp}
    estado = ((cur or {}).get("nodeStates") or {}).get(key, {}) if is_live else {}
    return {"runs": runs, "acumulado": acc, "ultimo": last,
            "estado": estado.get("status") or ("idle" if is_live else None)}


def _wiring_of(graph, node_id):
    """El cableado del nodo tal como lo lee el motor: a quién delega, quién le
    delega, qué recursos y qué MCPs tiene. Es la fuente de TODO lo que recibe."""
    nid = int(node_id)
    inbound = [graph["nodos"].get(int(f["fromId"])) for f in graph["flechas"]
               if f.get("kind") == "delega" and int(f.get("toId", -1)) == nid]
    entradas = [f.get("kind") for f in graph["flechas"]
                if int(f.get("toId", -1)) == nid and f.get("kind") in ("task", "trigger")]
    return {
        "delegaA": [{"id": t["id"], "titulo": t.get("titulo")} for t in delega_targets(graph, nid)],
        "leDelegan": [{"id": n["id"], "titulo": n.get("titulo")} for n in inbound if n],
        "recursos": [{"id": r["id"], "titulo": r.get("titulo"),
                      "projectId": (r.get("data") or {}).get("projectId"),
                      "permiso": (r.get("data") or {}).get("permiso") or "editar"}
                     for r in resources_of(graph, nid)],
        "mcps": [{"id": m["id"], "titulo": m.get("titulo"),
                  "tipo": (m.get("data") or {}).get("tipo")} for m in mcps_of(graph, nid)],
        "entradas": entradas,
    }


def _tool_sources(ctx, graph, node_id):
    """prefijo (`r5`/`m8`) → de qué nodo del canvas viene. El prefijo lo define el
    propio motor al armar las tools, así que agrupar por él no duplica lógica."""
    src = {}
    for r in resources_of(graph, node_id):
        rpid = (r.get("data") or {}).get("projectId")
        meta = ctx["project_meta"](rpid)
        src[f"r{r['id']}"] = {"origin": "resource", "nodeId": r["id"],
                              "titulo": r.get("titulo"),
                              "label": (meta or {}).get("name") or "(proyecto borrado)",
                              "tipo": (meta or {}).get("type"),
                              "permiso": (r.get("data") or {}).get("permiso") or "editar",
                              "missing": meta is None}
    for m in mcps_of(graph, node_id):
        d = m.get("data") or {}
        src[f"m{m['id']}"] = {"origin": "mcp", "nodeId": m["id"], "titulo": m.get("titulo"),
                              "label": m.get("titulo") or f"m{m['id']}",
                              "tipo": d.get("tipo"), "preset": d.get("preset")}
    return src


def inspect_node(ctx, node_id):
    """System prompt y tools EXACTOS que recibiría este agente si girara ahora,
    más el transcript de lo que se le mandó. Todo calculado con las funciones del
    motor: si algo no aparece acá, el agente NO lo tiene."""
    graph = load_graph(ctx)
    node = _agent(graph, node_id)
    d = node.get("data") or {}
    ia = d.get("ia") or {}
    provider = ia.get("provider") or "anthropic"
    is_cli = provider in CLI_PROVIDERS
    author = f"IA ({node.get('titulo') or node['id']})"
    mem = mem_read(ctx, node["id"])
    mchars = mem_chars(ctx, node["id"])
    base = {
        "nodeId": node["id"], "titulo": node.get("titulo"),
        "kind": "cli" if is_cli else "api",
        "ia": {"provider": provider, "model": ia.get("model"), "effort": ia.get("effort"),
               "credId": ia.get("credId")},
        "director": bool(d.get("director")), "secuencial": bool(d.get("secuencial")),
        "confinado": bool(d.get("confinado")),
        # el system inyecta SOLO las últimas 12 entradas (build_system): el resto
        # está en disco pero el agente no lo ve.
        "memoria": {"enabled": (d.get("memoria") or {}).get("enabled", True),
                    "total": len(mem), "enviadas": min(len(mem), 12),
                    "chars": mchars, "heavy": mchars > MEM_HEAVY_CHARS, "entries": mem},
        "wiring": _wiring_of(graph, node["id"]),
        "stats": _stats_of(ctx, node["id"]),
        "graph": {"nodos": len(graph["nodos"]), "flechas": len(graph["flechas"])},
        "transcript": _transcript_of(ctx, node["id"]),
        "rearmed": True,          # system y tools se recalculan EN CADA TURNO
    }
    if is_cli:
        # Cabeza CLI: no le mandamos tools JSON — usa las NATIVAS de Claude Code y
        # el control va por protocolo de texto. El transcript vive en la sesión del
        # CLI (--resume), no acá: por eso los frames CLI no tienen `messages`.
        confinado = bool(d.get("confinado"))
        notes, add_dirs, mcp, exec_ok = _cli_resource_notes(ctx, graph, node)
        cwd = _cli_workspace(ctx, node["id"])
        refs = []
        if confinado:
            # el whitelist real: una entrada por editor cableado, según su permiso
            for name, info in mcp.items():
                tools = list(MCP_FS_READ)
                if info["perm"] >= 1:
                    tools += MCP_FS_WRITE
                if info["perm"] >= 2:
                    tools += MCP_FS_EXEC
                refs.append({"origin": "mcp", "label": f"Editor por MCP · {name}",
                             "note": ("Cada llamada va a /fs de este backend, o sea a editorfs: "
                                      "rechaza cualquier ruta fuera de la carpeta del editor, "
                                      "igual que para un agente API."),
                             "tools": [{"name": f"mcp__{name}__{t}", "schema": {},
                                        "description": f"{t} sobre ese proyecto editor."}
                                       for t in tools]})
            if add_dirs:
                refs.append({"origin": "cli", "label": "Nativas de archivos (solo sus diagramas)",
                             "note": ("Habilitadas porque tiene diagramas cableados; sus únicos "
                                      "--add-dir son esos subdirectorios, así que no alcanzan "
                                      "nada más."),
                             "tools": [{"name": n, "schema": {}, "description": "",
                                        "disabled": False} for n in CLI_DIAGRAM_TOOLS]})
        else:
            refs.append({"origin": "cli", "label": "Herramientas nativas de Claude Code",
                         "note": ("Las declara el CLI, no el motor, así que pueden variar con su "
                                  "versión — esto es una referencia. Lo que sí fija el motor son "
                                  "los flags: --permission-mode acceptEdits y --disallowedTools."),
                         "tools": [{"name": n, "description": de, "schema": {},
                                    "disabled": n in CLI_DISALLOWED or (n == "Bash" and not exec_ok)}
                                   for n, de in CLI_NATIVE_TOOLS]})
        refs.append({"origin": "skills", "label": "Skills instaladas en su workspace",
                     "note": ("`install_skills` las escribe en <workspace>/.claude/skills/ antes "
                              "de cada turno: el agente las lee cuando le hacen falta. No son "
                              "tools — son conocimiento (el esquema de cada tipo de diagrama)."),
                     "tools": [{"name": s["name"], "description": s["description"], "schema": {}}
                               for s in _skill_catalog()]})
        off = list(CLI_DISALLOWED) + ([] if exec_ok else ["Bash"])
        base.update({
            "system": _cli_system(ctx, graph, node, notes),
            "systemNote": ("Se pasa como --append-system-prompt EN CADA TURNO, entero. "
                           "El transcript NO se reenvía: lo guarda Claude Code y se "
                           "recupera con --resume <sessionId>."),
            # `toolGroups` es SOLO lo que el motor declara (para una cabeza CLI, nada).
            # Lo demás va en `refGroups`: existe y el agente lo usa, pero no lo manda
            # el motor — mantener la distinción es lo que hace confiable a este modal.
            "toolGroups": [],
            "refGroups": refs,
            "cli": {"addDirs": add_dirs, "workDir": cwd, "protocol": CLI_PROTOCOL,
                    "confinado": confinado, "permissionMode": "acceptEdits",
                    "disallowed": [] if confinado else off,
                    "execOk": exec_ok,
                    "warning": (
                        "Confinado: no tiene la carpeta montada. Toda escritura sobre un editor "
                        "pasa por editorfs (mismo confinamiento que un agente API); las nativas "
                        "de archivos solo existen si tiene diagramas cableados, y llegan nada más "
                        "que a esos subdirectorios."
                        if confinado else
                        "No confinado: usa sus tools nativas, acotadas a los --add-dir de abajo — "
                        "solo los recursos que le cableaste. Ojo: --add-dir no distingue lectura "
                        "de escritura (un recurso 'leer' no se monta) y Bash puede salirse de "
                        "ellos, por eso solo la tiene si algún recurso suyo es 'ejecutar'.")},
        })
        return base

    ctrl = control_tools(graph, node["id"])
    rtools, _rexecs, rnotes = resource_tools(ctx, graph, node["id"], author)
    mtools, _mexecs, mnotes = mcp_tools(ctx, graph, node["id"])
    otools = []
    if d.get("director"):
        otools, _oexecs = org_tools(ctx, graph, {"id": "inspect"}, node)
    system = build_system(ctx, graph, node, rnotes + mnotes)
    src = _tool_sources(ctx, graph, node["id"])
    groups, buckets = [], {}
    for t in rtools + mtools:
        buckets.setdefault(str(t["name"]).split("_")[0], []).append(t)
    groups.append({"origin": "control", "label": "Control del orquestador",
                   "note": ("Siempre presentes. `delegar` aparece solo si el nodo tiene "
                            "flechas `delega` salientes."), "tools": ctrl})
    if otools:
        groups.append({"origin": "director", "label": "Director (tick 👑)",
                       "note": "Gestiona ESTE organigrama. Editar nunca dispara runs.",
                       "tools": otools})
    for prefix, tools in buckets.items():
        info = src.get(prefix) or {"origin": "resource", "label": prefix}
        groups.append({**info, "prefix": prefix, "tools": tools})
    # los ESQUEMAS de los tipos de diagrama no son tools: build_system los inyecta
    # como texto en el system (por eso un agente API sabe escribir un tree.json sin
    # tener una tool para eso). Van en refGroups para que no parezca que "le faltan
    # tools", sin ensuciar la lista de lo que el motor declara de verdad.
    tipos = sorted({(ctx["project_meta"]((r["data"] or {}).get("projectId")) or {}).get("type")
                    for r in resources_of(graph, node["id"])} - {None, "editor"})
    refs = []
    if tipos:
        refs.append({"origin": "skills", "label": "Esquemas inyectados en el system",
                     "note": ("No son tools: el motor mete el esquema de estos tipos como "
                              "TEXTO en el system prompt (pestaña Context), así el agente "
                              "sabe cómo escribir su tree.json con set_tree."),
                     "tools": [{"name": f"diagramind-{t.lower()}", "schema": {},
                                "description": f"Esquema del tipo {t}."} for t in tipos]})
    base.update({
        "refGroups": refs,
        "system": system,
        "systemNote": ("Se REARMA y se manda entero en cada turno (rol + subordinados + "
                       "recursos + memoria + esquemas). Las tools también se recalculan."),
        "toolGroups": [{**g, "tools": [{"name": t["name"], "description": t["description"],
                                        "schema": t["schema"]} for t in g["tools"]]}
                       for g in groups],
        "notes": rnotes + mnotes,
    })
    return base

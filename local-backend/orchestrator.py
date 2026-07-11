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
- Cabezas: APIs (Anthropic + OpenAI-compatible) y Claude Code CLI (fase 4), mixto.
  Las API keys viven SOLO en RAM (nunca se persisten): si el backend se reinicia a
  mitad de un run, el run queda en error y se relanza.

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
import urllib.request
import uuid

import subprocess

import editorfs
import sourcever
from claude import EFFORT_THINK, find_claude, map_model
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


# ===================== API keys DEL ORQUESTADOR (por proyecto) =====================
# Las credenciales viven EN EL CONECTOR del proyecto (decisión 2026-07-11): el core
# tiene que poder correr sin la web conectada, y no son las keys del chat del
# usuario. Se guardan en <orch>/<pid>/keys.json (0600) — NUNCA en el tree.json ni
# en el mirror (eso las metería en localStorage/git). Al correr, las del proyecto
# MANDAN; las que lleguen en el request quedan solo como fallback (compat).

def _keys_path(ctx):
    return os.path.join(orch_dir(ctx["app_dir"], ctx["pid"]), "keys.json")


def keys_read(ctx):
    return _read_json(_keys_path(ctx), {})


def keys_write(ctx, patch):
    """Setea/borra credenciales por proveedor (valor vacío/None = borrar)."""
    keys = keys_read(ctx)
    for prov, val in (patch or {}).items():
        if prov not in ("anthropic", "openai", "other") and not str(prov).startswith("mcp:"):
            continue
        empty = not val or (isinstance(val, dict) and not (val.get("key") or val.get("url")))
        if empty:
            keys.pop(prov, None)
        elif isinstance(val, dict):
            # merge parcial (p.ej. actualizar solo la URL de "other" sin pisar la key)
            keys[prov] = {**(keys.get(prov) or {}), **{k: v for k, v in val.items() if v}}
        else:
            keys[prov] = val
    _write_json(_keys_path(ctx), keys)
    try:
        os.chmod(_keys_path(ctx), 0o600)
    except OSError:
        pass
    return keys_status(ctx)


def _key_hint(k):
    return ("…" + k[-4:]) if isinstance(k, str) and len(k) >= 8 else ""


def keys_status(ctx):
    """Estado para la UI: qué proveedores están configurados. NUNCA devuelve los secretos."""
    keys = keys_read(ctx)
    out = {}
    for prov in ("anthropic", "openai"):
        v = keys.get(prov)
        out[prov] = {"set": bool(v), "hint": _key_hint(v or "")}
    o = keys.get("other") or {}
    out["other"] = {"set": bool(o.get("key") and o.get("url")), "url": o.get("url") or "",
                    "hint": _key_hint(o.get("key") or "")}
    mcp = {}
    for k, v in keys.items():
        if str(k).startswith("mcp:"):
            mcp[str(k).split(":", 1)[1]] = {"set": bool((v or {}).get("key")),
                                            "hint": _key_hint((v or {}).get("key") or "")}
    out["mcp"] = mcp
    return {"keys": out}


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


class OpenAIChat:
    provider = "openai"

    def __init__(self, key, model, effort, base=None):
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


def make_adapter(ctx, node):
    ia = (node.get("data") or {}).get("ia") or {}
    provider = ia.get("provider") or "anthropic"
    keys = KEYS.get(ctx["pid"]) or {}
    if provider == "anthropic":
        if not keys.get("anthropic"):
            raise OrchError(400, f"el nodo «{node.get('titulo')}» usa Anthropic y no llegó esa API key")
        return AnthropicChat(keys["anthropic"], ia.get("model") or "claude-sonnet-4-6", ia.get("effort"))
    if provider == "openai":
        if not keys.get("openai"):
            raise OrchError(400, f"el nodo «{node.get('titulo')}» usa OpenAI y no llegó esa API key")
        return OpenAIChat(keys["openai"], ia.get("model") or "gpt-4o")
    if provider == "other":
        o = keys.get("other") or {}
        if not o.get("key") or not o.get("url"):
            raise OrchError(400, f"el nodo «{node.get('titulo')}» usa 'Otra API' y no llegó su key/URL")
        return OpenAIChat(o["key"], ia.get("model") or "gpt-4o", None, base=o["url"])
    raise OrchError(400, f"el nodo «{node.get('titulo')}» usa el proveedor '{provider}', que el motor "
                         "todavía no soporta (APIs: Anthropic y OpenAI-compatible; CLI: Claude Code)")


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
    partes.append(
        "REGLAS: 1) Trabajá SOLO en lo que te pidieron. 2) Usá `preguntar_al_usuario` ante decisiones "
        "importantes o contexto faltante. 3) Cerrá SIEMPRE tu turno con `responder` (resumen concreto y "
        "verificable). 4) En proyectos editor, guardá una versión (sv_save) antes de una tanda de cambios. "
        "5) Respondé en español."
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
        # las keys DEL PROYECTO mandan; las del request quedan como fallback (compat)
        KEYS[ctx["pid"]] = {**(api_keys or {}), **keys_read(ctx)}
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


def _cli_resource_notes(ctx, graph, node):
    """Notas de recursos para un agente CLI (rutas reales, no tools) + add_dirs."""
    notes, add_dirs = [], []
    for r in resources_of(graph, node["id"]):
        rpid = r["data"]["projectId"]
        meta = ctx["project_meta"](rpid)
        if not meta:
            continue
        perm = (r["data"] or {}).get("permiso") or "editar"
        if meta.get("type") == "editor":
            target = editorfs.get_target(ctx["app_dir"], rpid)
            if target:
                notes.append(f"- «{meta.get('name')}» (editor, permiso {perm}): la carpeta real "
                             f"{target} — trabajá DIRECTO ahí con tus herramientas de archivos")
                if PERM_LEVEL.get(perm, 1) >= 1:
                    add_dirs.append(target)
        else:
            rel = f"./{safe_name(meta.get('name') or rpid)}/tree.json"
            notes.append(f"- «{meta.get('name')}» ({meta.get('type')}, permiso {perm}): el diagrama "
                         f"{rel} — editalo respetando el esquema EXACTO de su tipo "
                         f"(skill diagramind-{str(meta.get('type')).lower()})")
    return notes, add_dirs


def _cli_system(ctx, graph, node, notes):
    d = node.get("data") or {}
    partes = [
        f"Sos «{node.get('titulo') or 'agente'}», un empleado IA de la empresa (IA Orchestrator de DiagraMinder).",
        f"TU ROL: {d.get('rol') or '(sin rol definido — trabajá con criterio)'}",
    ]
    targets = delega_targets(graph, node["id"])
    if targets:
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
    partes.append("REGLAS: 1) Trabajá SOLO en lo que te pidieron. 2) Tocá ÚNICAMENTE tus recursos "
                  "(no otros proyectos de la carpeta). 3) Respondé en español, concreto.")
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


def _run_cli_turn(ctx, graph, run, node, frame, message):
    """Lanza `claude -p` para un turno del agente y devuelve (texto, session_id, costo)."""
    cli_bin = find_claude()
    if not cli_bin:
        raise OrchError(400, f"el nodo «{node.get('titulo')}» usa Claude Code y el binario `claude` "
                             "no está en esta máquina")
    notes, add_dirs = _cli_resource_notes(ctx, graph, node)
    system = _cli_system(ctx, graph, node, notes)
    ia = (node.get("data") or {}).get("ia") or {}
    kw = EFFORT_THINK.get(ia.get("effort") or "", "")
    msg = message + (f"\n\n{kw}" if kw else "")
    work_dir = ctx.get("work_dir") or ctx["app_dir"]
    try:
        install_skills(work_dir)
    except Exception:
        pass
    cmd = [cli_bin, "-p", msg, "--output-format", "stream-json", "--verbose",
           "--model", map_model(ia.get("model")), "--permission-mode", "acceptEdits",
           "--add-dir", work_dir,
           "--disallowedTools", "WebFetch", "WebSearch",
           "--append-system-prompt", system]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    if frame.get("sessionId"):
        cmd += ["--resume", str(frame["sessionId"])]
    try:
        proc = subprocess.Popen(cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, encoding="utf-8", errors="replace")
    except Exception as e:
        raise OrchError(400, f"no pude lanzar Claude Code: {e}")
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

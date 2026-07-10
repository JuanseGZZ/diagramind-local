"""Motor del IA Orchestrator (doc 28, Fase 2 — SECUENCIAL).

- UN run por proyecto a la vez. El trabajo entra por una TAREA (`agTask` → agente
  raíz por flecha `task`) o por el MINI-CHAT de un nodo (decisión S: hablarle a un
  agente — típicamente el PM — es otro entry point; la charla entra a su memoria y
  se puede borrar entera).
- TOKEN de ejecución con PILA de llamadas (decisión C: delegar = llamar y esperar).
  Cada frame es un agente con su transcript nativo del proveedor. `delegar` /
  `responder` / `preguntar_al_usuario` son TOOLS que mueven/suspenden el token.
- TOOLS DE RECURSOS: por cada `agResource` conectado por `usa`, el agente recibe
  tools con prefijo `r<idNodo>_` según permiso (editor → fs_*/sv_* vía
  editorfs/sourcever; diagramas → view_tree/set_tree sobre el tree.json del mirror,
  que la web refleja en vivo).
- MEMORIA por agente (decisión N): entradas {id, kind: task|chat|delegado, chatId?,
  ts, texto} en <orch>/<pid>/memory/<nodeId>.json; se inyecta al system si está
  habilitada; `memHeavy` (decisión R) si supera MEM_HEAVY_CHARS. `limpiar_memoria`
  como tool (la propia o la de un subordinado conectado por `delega`).
- SNAPSHOT pre-turno (decisión I): al activar el frame de un agente con recursos de
  escritura → sv_save en los editores + copia del tree.json en los diagramas.
- PRESUPUESTO (decisión J): maxTurns (llamadas LLM) por run; pause/resume/kill.
- Proveedores v1: Anthropic + OpenAI-compatible (openai/other). Google y CLIs → más
  adelante (error claro). Las API keys viven SOLO en RAM (nunca se persisten): si el
  backend se reinicia a mitad de un run, el run queda en error y se relanza.

El server (server.py) provee el contexto de rutas: dónde está el tree.json del
orquestador y cómo resolver los de los proyectos-recurso (mirror de la carpeta).
"""
import json
import os
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
LOCK = threading.Lock()

CONTROL_TOOLS = {"delegar", "responder", "preguntar_al_usuario"}
CLI_PROVIDERS = {"local", "local-codex", "local-gemini"}
CLI_TIMEOUT = 15 * 60        # tope de un turno CLI


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
    full = {k: v for k, v in run.items() if not str(k).startswith("_") and k not in ("stack",)}
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
    """Persiste el run SIN las keys (viven solo en RAM)."""
    _write_json(_run_path(ctx), run)


# ===================== grafo =====================

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
            f"Delegá trabajo a un subordinado directo y ESPERÁ su respuesta (podés delegar a: {names}). El mensaje debe ser concreto y verificable.",
            {"agente": {"type": "string", "description": "nombre del agente destino"},
             "mensaje": {"type": "string", "description": "qué tiene que hacer, con el contexto necesario"}},
            ["agente", "mensaje"])))
    return tools


PERM_LEVEL = {"leer": 0, "editar": 1, "ejecutar": 2}


def resource_tools(ctx, graph, node_id):
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
            _editor_tools(ctx, rid, rpid, perm, tools, execs)
        else:
            _diagram_tools(ctx, rid, rpid, rtype, perm, tools, execs)
    return tools, execs, notes


def _fs(fn, *args):
    code, payload = fn(*args)
    return json.dumps(payload, ensure_ascii=False), code >= 400


def _editor_tools(ctx, rid, rpid, perm, tools, execs):
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
            return json.dumps(sourcever.sv_save(svd, t, ctx["author"], i.get("note") or ""), ensure_ascii=False), False
        add("sv_save", _s("Guarda una VERSIÓN (snapshot) del proyecto. Usala ANTES de una tanda de cambios.",
                          {"note": {"type": "string"}}), sv_save)
        def sv_restore(i):
            svd, t = sv_ctx()
            return json.dumps(sourcever.sv_restore(svd, t, i.get("id"), ctx["author"]), ensure_ascii=False), False
        add("sv_restore", _s("Vuelve el proyecto a una versión (con snapshot de seguridad previo). Solo si te lo piden.",
                             {"id": {"type": "string"}}, ["id"]), sv_restore)
    if perm >= 2:
        add("fs_exec", _s("Ejecuta un comando de shell en el proyecto (timeout 60s).",
                          {"cmd": {"type": "string"}}, ["cmd"]),
            lambda i: _fs(editorfs.fs_exec, app, rpid, i.get("cmd")))


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


def build_system(ctx, graph, node):
    d = node.get("data") or {}
    nid = node["id"]
    partes = [
        f"Sos «{node.get('titulo') or 'agente'}», un empleado IA de la empresa (IA Orchestrator de DiagraMinder).",
        f"TU ROL: {d.get('rol') or '(sin rol definido — trabajá con criterio)'}",
    ]
    targets = delega_targets(graph, nid)
    if targets:
        partes.append("SUBORDINADOS (podés delegarles con la tool `delegar` y esperás su respuesta): " +
                      "; ".join(f"«{t.get('titulo') or t['id']}» ({(t.get('data') or {}).get('rol', '')[:80]})" for t in targets))
    _tools, _execs, notes = ctx["_res_cache"]
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
    partes.append(
        "REGLAS: 1) Trabajá SOLO en lo que te pidieron. 2) Usá `preguntar_al_usuario` ante decisiones "
        "importantes o contexto faltante. 3) Cerrá SIEMPRE tu turno con `responder` (resumen concreto y "
        "verificable). 4) En proyectos editor, guardá una versión (sv_save) antes de una tanda de cambios. "
        "5) Respondé en español."
    )
    return "\n\n".join(partes)


# ===================== eventos / estado =====================

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


# ===================== snapshots pre-turno (decisión I) =====================

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


# ===================== frames / motor =====================

def _new_frame(ctx, graph, run, node, entry_kind, initial_text):
    provider = ((node.get("data") or {}).get("ia") or {}).get("provider") or "anthropic"
    if provider in CLI_PROVIDERS:
        if provider != "local":
            raise OrchError(400, f"el nodo «{node.get('titulo')}» usa '{provider}': como cabeza CLI "
                                 "por ahora solo está soportado Claude Code (fase 4 v1)")
        frame = {"kind": "cli", "nodeId": node["id"], "provider": provider, "entry": entry_kind,
                 "sessionId": None, "pendingInput": initial_text, "iters": 0,
                 "firstText": initial_text}
    else:
        adapter = make_adapter(ctx, node)
        frame = {"kind": "api", "nodeId": node["id"], "provider": adapter.provider, "entry": entry_kind,
                 "messages": [adapter.user_msg(initial_text)], "pendingToolId": None,
                 "stash": [], "iters": 0, "firstText": initial_text}
    snapshot_resources(ctx, run, graph, node)
    set_node_state(ctx, run, node["id"], "running")
    emit(run, "log", nodeId=node["id"], text=f"→ entra trabajo: {initial_text[:200]}")
    return frame


def _adapter_for_frame(ctx, graph, frame):
    return make_adapter(ctx, _agent(graph, frame["nodeId"]))


def _finish_node(ctx, run, graph, frame, mensaje):
    """responder: registra memoria y devuelve el mensaje al caller (o cierra el run)."""
    node = _agent(graph, frame["nodeId"])
    d = node.get("data") or {}
    if (d.get("memoria") or {}).get("enabled", True):
        chat_id = run.get("chatId") if frame["entry"] == "chat" else None
        mem_append(ctx, node["id"], frame["entry"],
                   f"Tarea: {frame['firstText'][:400]} → Resultado: {mensaje[:700]}", chat_id)
    set_node_state(ctx, run, node["id"], "done")
    emit(run, "log", nodeId=node["id"], text=f"← responde: {mensaje[:200]}")


def start_run(ctx, entry_kind, root_node_id, initial_text, api_keys, max_turns=None):
    """Crea y lanza un run (entry task o chat). Devuelve el run dict."""
    with LOCK:
        prev = RUNS.get(ctx["pid"])
        if prev and prev["status"] in ("running", "waiting_human", "paused"):
            raise OrchError(409, "ya hay un run en curso en este orquestador (v1 secuencial): "
                                 "esperá, respondé lo pendiente o matalo")
        graph = load_graph(ctx)
        root = _agent(graph, root_node_id)
        KEYS[ctx["pid"]] = api_keys or {}
        run = {
            "id": "run" + uuid.uuid4().hex[:8], "projectId": ctx["pid"], "entry": entry_kind,
            "status": "running", "rootNodeId": root["id"], "final": None, "error": None,
            "turns": 0, "maxTurns": max_turns or MAX_TURNS_DEFAULT,
            "chatId": None, "pending": None, "stack": [], "nodeStates": {}, "spend": {},
            "events": [], "createdAt": int(time.time() * 1000),
        }
        if entry_kind == "chat":
            c = chat_read(ctx, root["id"])
            run["chatId"] = c.get("chatId") or ("c" + uuid.uuid4().hex[:8])
            chat_append(ctx, root["id"], "user", initial_text, run["chatId"])
        run["stack"].append(_new_frame(ctx, graph, run, root, entry_kind, initial_text))
        RUNS[ctx["pid"]] = run
        _save(ctx, run)
    _spawn(ctx)
    return run


def _spawn(ctx):
    threading.Thread(target=_loop, args=(ctx,), daemon=True).start()


def _loop(ctx):
    run = RUNS.get(ctx["pid"])
    try:
        graph = load_graph(ctx)
        while run["status"] == "running":
            if run.get("_kill"):
                run["status"] = "killed"
                break
            if run.get("_pause"):
                run["status"] = "paused"
                run.pop("_pause", None)
                break
            if not run["stack"]:
                run["status"] = "done"
                break
            if run["turns"] >= run["maxTurns"]:
                raise OrchError(400, f"presupuesto agotado ({run['maxTurns']} turnos). Subí maxTurns o dividí la tarea")
            _step(ctx, graph, run)
        emit(run, "status", status=run["status"])
    except OrchError as e:
        run["status"] = "error"
        run["error"] = e.msg
        emit(run, "status", status="error", error=e.msg)
    except Exception as e:
        run["status"] = "error"
        run["error"] = f"error interno del motor: {e}"
        emit(run, "status", status="error", error=run["error"])
    _save(ctx, run)
    _archive_run(ctx, run)


def _step(ctx, graph, run):
    frame = run["stack"][-1]
    if frame.get("kind") == "cli":
        _step_cli(ctx, graph, run, frame)
        return
    node = _agent(graph, frame["nodeId"])
    adapter = _adapter_for_frame(ctx, graph, frame)
    ctrl = control_tools(graph, node["id"])
    rtools, rexecs, rnotes = resource_tools(ctx, graph, node["id"])
    ctx["_res_cache"] = (rtools, rexecs, rnotes)
    ctx["author"] = f"IA ({node.get('titulo') or node['id']})"
    system = build_system(ctx, graph, node)
    tools = ctrl + rtools

    set_node_state(ctx, run, node["id"], "running")
    res = adapter.call(system, frame["messages"], tools)
    run["turns"] += 1
    frame["iters"] += 1
    add_spend(run, node["id"], res["usage"])
    frame["messages"].append(res["assistant_msg"])

    if not res["tool_calls"]:
        # sin tools → responder implícito con el texto
        _do_responder(ctx, graph, run, frame, res["text"] or "(sin respuesta)")
        _save(ctx, run)
        return

    if frame["iters"] > MAX_TOOL_ITERS:
        _do_responder(ctx, graph, run, frame,
                      (res["text"] or "") + "\n(corté: demasiadas iteraciones en este turno)")
        _save(ctx, run)
        return

    results, control = [], None
    for tc in res["tool_calls"]:
        if control is not None:
            results.append({"id": tc["id"], "name": tc["name"], "is_error": True,
                            "content": "ignorada: primero se resuelve la acción de control anterior"})
            continue
        if tc["name"] in CONTROL_TOOLS:
            control = tc
            continue
        results.append(_exec_tool(ctx, graph, run, node, rexecs, tc))

    if control is None:
        frame["messages"].append(adapter.tool_results_msg(results)) if adapter.provider == "anthropic" \
            else frame["messages"].extend(adapter.tool_results_msg(results))
        _save(ctx, run)
        return

    if control["name"] == "responder":
        _do_responder(ctx, graph, run, frame, str(control["input"].get("mensaje") or ""))
        _save(ctx, run)
        return

    # delegar / preguntar_al_usuario: dejan el frame esperando el tool_result
    frame["pendingToolId"] = control["id"]
    frame["stash"] = results
    if control["name"] == "delegar":
        target = _resolve_target(graph, node["id"], control["input"].get("agente"))
        if not target:
            _resume_frame(ctx, graph, run, frame,
                          f"no podés delegar a «{control['input'].get('agente')}»: no está conectado por una flecha delega",
                          is_error=True)
            _save(ctx, run)
            return
        set_node_state(ctx, run, node["id"], "waiting")
        msg = str(control["input"].get("mensaje") or "")
        texto = f"«{node.get('titulo') or node['id']}» te delega: {msg}"
        run["stack"].append(_new_frame(ctx, graph, run, target, "delegado", texto))
        _save(ctx, run)
        return

    # preguntar_al_usuario
    pregunta = str(control["input"].get("pregunta") or "")
    run["pending"] = {"nodeId": node["id"], "question": pregunta}
    run["status"] = "waiting_human"
    set_node_state(ctx, run, node["id"], "asking")
    emit(run, "ask", nodeId=node["id"], question=pregunta)
    _save(ctx, run)


def _exec_tool(ctx, graph, run, node, rexecs, tc):
    name, inp = tc["name"], tc["input"]
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
            set_node_state(ctx, run, target["id"], run["nodeStates"].get(str(target["id"]), {}).get("status", "idle"))
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


def _resume_frame(ctx, graph, run, frame, content, is_error=False):
    """Entrega el tool_result pendiente (respuesta de delegado / humano / error)."""
    if frame.get("kind") == "cli":
        frame["pendingInput"] = ("⚠ " if is_error else "") + content
        set_node_state(ctx, run, frame["nodeId"], "running")
        return
    adapter = _adapter_for_frame(ctx, graph, frame)
    results = frame["stash"] + [{"id": frame["pendingToolId"], "name": "control",
                                 "content": content, **({"is_error": True} if is_error else {})}]
    if adapter.provider == "anthropic":
        frame["messages"].append(adapter.tool_results_msg(results))
    else:
        frame["messages"].extend(adapter.tool_results_msg(results))
    frame["pendingToolId"] = None
    frame["stash"] = []
    set_node_state(ctx, run, frame["nodeId"], "running")


def _do_responder(ctx, graph, run, frame, mensaje):
    _finish_node(ctx, run, graph, frame, mensaje)
    run["stack"].pop()
    if not run["stack"]:
        run["final"] = mensaje
        run["status"] = "done"
        if run["entry"] == "chat":
            chat_append(ctx, run["rootNodeId"], "assistant", mensaje, run["chatId"])
        emit(run, "final", text=mensaje)
        return
    parent = run["stack"][-1]
    child = _agent(graph, frame["nodeId"])
    _resume_frame(ctx, graph, run, parent,
                  f"Respuesta de «{child.get('titulo') or child['id']}»: {mensaje}")


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
    'CONTROL: {"accion":"preguntar_al_usuario","pregunta":"<qué necesitás que decida el humano>"}\n'
    "Además podés emitir líneas `CONTROL: {\"accion\":\"limpiar_memoria\",\"agente\":\"<opcional>\"}` "
    "ANTES de la línea final. Si delegás o preguntás, vas a recibir la respuesta en el próximo turno "
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
        partes.append("SUBORDINADOS (podés delegarles y esperás su respuesta): " +
                      "; ".join(f"«{t.get('titulo') or t['id']}» ({(t.get('data') or {}).get('rol', '')[:80]})" for t in targets))
    if notes:
        partes.append("TUS RECURSOS:\n" + "\n".join(notes))
    if (d.get("memoria") or {}).get("enabled", True):
        mem = mem_read(ctx, node["id"])
        if mem:
            lines = [f"- [{time.strftime('%Y-%m-%d %H:%M', time.localtime(m['ts'] / 1000))}] {m['texto']}"
                     for m in mem[-12:]]
            partes.append("TU MEMORIA (trabajos y charlas anteriores):\n" + "\n".join(lines))
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


def _run_cli_turn(ctx, run, node, frame, message):
    """Lanza `claude -p` para un turno del agente y devuelve (texto, session_id, costo)."""
    cli_bin = find_claude()
    if not cli_bin:
        raise OrchError(400, f"el nodo «{node.get('titulo')}» usa Claude Code y el binario `claude` "
                             "no está en esta máquina")
    graph = load_graph(ctx)
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
    run["_cliProc"] = proc
    session_id, result_text, texts, cost, deadline = None, None, [], 0.0, time.time() + CLI_TIMEOUT
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
                    emit(run, "log", nodeId=node["id"], text=f"cli tool {b.get('name', '?')}")
        elif obj.get("type") == "result":
            session_id = obj.get("session_id") or session_id
            result_text = obj.get("result")
            cost = obj.get("total_cost_usd") or 0.0
            if obj.get("is_error"):
                proc.wait()
                run["_cliProc"] = None
                raise OrchError(502, f"Claude Code devolvió un error: {result_text or '?'}")
    proc.wait()
    run["_cliProc"] = None
    if run.get("_kill"):
        raise OrchError(400, "turno CLI cancelado")
    if proc.returncode not in (0, None) and result_text is None:
        err = (proc.stderr.read() or "").strip()[:400]
        raise OrchError(502, f"Claude Code salió con código {proc.returncode}: {err}")
    return (result_text or "\n\n".join(texts) or ""), session_id, cost


def _step_cli(ctx, graph, run, frame):
    node = _agent(graph, frame["nodeId"])
    message = frame.get("pendingInput") or "(continuá)"
    frame["pendingInput"] = None
    frame["iters"] += 1
    set_node_state(ctx, run, node["id"], "running")
    text, session_id, cost = _run_cli_turn(ctx, run, node, frame, message)
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

    if accion is None or accion.get("accion") == "responder":
        mensaje = (accion or {}).get("mensaje") or visible or "(sin respuesta)"
        _do_responder(ctx, graph, run, frame, str(mensaje))
        _save(ctx, run)
        return
    if frame["iters"] > MAX_TOOL_ITERS:
        _do_responder(ctx, graph, run, frame, visible + "\n(corté: demasiadas iteraciones)")
        _save(ctx, run)
        return
    if accion.get("accion") == "delegar":
        target = _resolve_target(graph, node["id"], accion.get("agente"))
        if not target:
            frame["pendingInput"] = (f"⚠ no podés delegar a «{accion.get('agente')}»: no está conectado "
                                     "por una flecha delega. Elegí un subordinado válido o respondé.")
            _save(ctx, run)
            return
        set_node_state(ctx, run, node["id"], "waiting")
        texto = f"«{node.get('titulo') or node['id']}» te delega: {accion.get('mensaje') or ''}"
        run["stack"].append(_new_frame(ctx, graph, run, target, "delegado", texto))
        _save(ctx, run)
        return
    if accion.get("accion") == "preguntar_al_usuario":
        run["pending"] = {"nodeId": node["id"], "question": str(accion.get("pregunta") or "")}
        run["status"] = "waiting_human"
        set_node_state(ctx, run, node["id"], "asking")
        emit(run, "ask", nodeId=node["id"], question=run["pending"]["question"])
        _save(ctx, run)
        return
    # acción desconocida → pedirle que corrija
    frame["pendingInput"] = f"⚠ acción CONTROL desconocida: {accion.get('accion')}. Usá responder/delegar/preguntar_al_usuario."
    _save(ctx, run)


# ===================== API de alto nivel (la usa server.py) =====================

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
    slim = {k: v for k, v in run.items() if k not in ("stack", "events", "_kill", "_pause")}
    slim["stackNodes"] = [f["nodeId"] for f in run.get("stack", [])]
    return {"run": slim}


def answer(ctx, text):
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] != "waiting_human" or not run.get("pending"):
        raise OrchError(409, "no hay ninguna pregunta pendiente (¿se reinició el backend?)")
    graph = load_graph(ctx)
    frame = run["stack"][-1]
    node = _agent(graph, frame["nodeId"])
    emit(run, "log", nodeId=node["id"], text=f"usuario responde: {text[:200]}")
    _resume_frame(ctx, graph, run, frame, f"Respuesta del usuario: {text}")
    run["pending"] = None
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
    run["_pause"] = True
    return {"ok": True}


def resume(ctx):
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] != "paused":
        raise OrchError(409, "no hay un run pausado")
    if ctx["pid"] not in KEYS:
        raise OrchError(409, "el backend se reinició: relanzá el run")
    run["status"] = "running"
    _save(ctx, run)
    _spawn(ctx)
    return {"ok": True}


def kill(ctx):
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] not in ("running", "waiting_human", "paused"):
        raise OrchError(409, "no hay un run activo")
    if run["status"] == "running":
        run["_kill"] = True
        proc = run.get("_cliProc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    else:
        run["status"] = "killed"
        emit(run, "status", status="killed")
        _save(ctx, run)
        _archive_run(ctx, run)
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
        d = {k: v for k, v in live.items() if not str(k).startswith("_") and k != "stack"}
        d["live"] = live["status"] in ("running", "waiting_human", "paused")
        d["stackNodes"] = [f["nodeId"] for f in live.get("stack", [])]
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

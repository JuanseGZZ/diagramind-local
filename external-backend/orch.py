"""IA Orchestrator — motor ESPEJADO en el conector externo (doc 28, Fase 5).

Es la segunda implementación del MISMO diseño que corre en el backend local
(`local-backend/orchestrator.py`, fase 3: árbol de frames + scheduler + locks +
fork/join). El orquestador de una carpeta EXTERNA vive y corre ACÁ, server-side
(decisión M). Divergencias respecto del local, todas por diseño:

- **Cabezas = solo APIs** (Anthropic / OpenAI-compatible). Un nodo con CLI local
  (Claude Code/Codex/Gemini) → error claro: los CLIs no corren en el server.
- **Solo admin** (decisión Q, enforcement real): TODOS los endpoints /orch/*
  exigen `require_admin`. El run ejecuta las tools de recursos "como" ese admin.
- **Recursos por namespace canónico** (doc 25 §6): un `agResource` referencia el
  id canónico del proyecto; debe ser de la MISMA carpeta que el orquestador
  (decisión B). Diagramas → read/write del tree.json del repo (+ broadcast al
  room WS para que la web lo vea en vivo); editores → las MISMAS ops confinadas
  de fs.py/sv (doc 27) reusadas como funciones.
- **Snapshot pre-ejecución (decisión I)** con lo que versiona ESTE conector:
  diagramas → `git commit` del tree.json (autor "IA (<nodo>)", solo si hay
  cambios sin commitear: HEAD ya es el estado previo si no); editores →
  `sv_save` (source versions).
- **Eventos por POLLING REST** (`GET /orch/events?since=`): acá el transporte
  autenticado es JWT por header y EventSource no manda headers (doc 25 §10.5).

Storage de runs/memorias/charlas: `<HOME>/orchestrator/<pid>/` (mismo layout que
el local). Las API keys viven SOLO en RAM (reinicio ⇒ run en error, se relanza).
"""
import asyncio
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import config
import fs as fsmod
import git_ops
import projects
import realtime
import sourcever
import store
from auth import require_admin
from models import FsExecBody, FsPathBody, FsRenameBody, FsWriteBody

MEM_HEAVY_CHARS = 8000
MAX_TURNS_DEFAULT = 30
MAX_TOOL_ITERS = 12          # iteraciones LLM dentro de UN turno de agente
HTTP_TIMEOUT = 180
GIT_EMAIL = "orchestrator@diagramind.local"

RUNS = {}                     # pid -> run dict vivo
KEYS = {}                     # pid -> apiKeys (SOLO RAM)
LOCK = threading.Lock()       # protege RUNS/run dicts; los workers lo sueltan para llamar al LLM
RUNTIME = {}                  # pid -> {cv, alive} (NUNCA se serializa)

CONTROL_TOOLS = {"delegar", "responder", "preguntar_al_usuario"}
CLI_PROVIDERS = {"local", "local-codex", "local-gemini"}

LOOP = None                   # event loop de FastAPI (para push_canonical desde threads)


def set_loop(loop) -> None:
    global LOOP
    LOOP = loop


def _rt(pid):
    rt = RUNTIME.get(pid)
    if rt is None:
        rt = RUNTIME.setdefault(pid, {"cv": threading.Condition(LOCK), "alive": False})
    return rt


class OrchError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ===================== contexto (rutas/permisos de ESTE conector) =====================

def make_ctx(pid, user):
    proj = store.get_project(pid)
    if not proj:
        raise OrchError(404, "el orquestador no existe en este conector")
    return {"pid": pid, "user": user, "folder_id": proj["folder_id"]}


def project_meta(ctx, rpid):
    """{name, type} de un proyecto-recurso, o None si no existe o es de OTRA carpeta
    (decisión B: el alcance del orquestador es SU carpeta)."""
    proj = store.get_project(rpid)
    if not proj or proj["folder_id"] != ctx["folder_id"]:
        return None
    ptype = None
    tj = projects.read_tree(rpid)
    if tj:
        try:
            ptype = json.loads(tj).get("type")
        except Exception:
            pass
    return {"name": proj["name"], "type": ptype or "?"}


def _notify_edit(rpid):
    """Difunde el tree.json canónico al room WS (la web lo ve en vivo). Desde threads."""
    if LOOP is not None:
        try:
            asyncio.run_coroutine_threadsafe(realtime.push_canonical(rpid), LOOP)
        except Exception:
            pass


def _sv_ctx(rpid):
    """(sv_dir, target) de un proyecto editor. OrchError si no tiene target."""
    target = fsmod.get_target(rpid)
    if not target:
        raise OrchError(400, "el proyecto editor no tiene target configurado")
    rel = store.project_reldir(rpid)
    if not rel:
        raise OrchError(404, "proyecto no encontrado")
    return os.path.join(str(config.REPO_ROOT), rel, "source-versions"), target


# ===================== storage =====================

def orch_dir(pid):
    d = Path(config.HOME) / "orchestrator" / store.safe_name(pid, "orch")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_path(ctx):
    return orch_dir(ctx["pid"]) / "run.json"


def _runs_dir(ctx):
    d = orch_dir(ctx["pid"]) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _runs_index_path(ctx):
    return _runs_dir(ctx) / "index.json"


def _run_summary(run):
    return {"id": run["id"], "entry": run["entry"], "rootNodeId": run.get("rootNodeId"),
            "status": run["status"], "final": run.get("final"), "error": run.get("error"),
            "createdAt": run.get("createdAt"), "endedAt": run.get("endedAt"),
            "turns": run.get("turns", 0), "spend": (run.get("spend") or {}).get("total", {})}


def _archive_run(ctx, run):
    if run.get("_archived") or run["status"] not in ("done", "error", "killed"):
        return
    run["_archived"] = True
    run["endedAt"] = run.get("endedAt") or int(time.time() * 1000)
    full = {k: v for k, v in run.items()
            if not str(k).startswith("_") and k not in ("frames", "locks")}
    _write_json(_runs_dir(ctx) / f"{run['id']}.json", full)
    idx = _read_json(_runs_index_path(ctx), [])
    idx = [x for x in idx if x.get("id") != run["id"]]
    idx.insert(0, _run_summary(run))
    _write_json(_runs_index_path(ctx), idx[:200])


def _mem_path(ctx, node_id):
    d = orch_dir(ctx["pid"]) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{node_id}.json"


def _chat_path(ctx, node_id):
    d = orch_dir(ctx["pid"]) / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{node_id}.json"


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


# ===================== grafo =====================

def load_graph(ctx):
    tj = projects.read_tree(ctx["pid"])
    tree = None
    if tj:
        try:
            tree = json.loads(tj)
        except Exception:
            tree = None
    if not tree or tree.get("type") != "orchestrator":
        raise OrchError(400, "el proyecto no es un orquestador o no está sincronizado")
    nodos = {int(n["id"]): n for n in tree.get("nodos", [])}
    flechas = tree.get("flechas", [])
    return {"nodos": nodos, "flechas": flechas}


def _agent(graph, node_id):
    n = graph["nodos"].get(int(node_id))
    if not n or n.get("type") != "agAgent":
        raise OrchError(400, f"el nodo {node_id} no es un agente")
    return n


def delega_targets(graph, node_id):
    out = []
    for f in graph["flechas"]:
        if f.get("kind") == "delega" and int(f.get("fromId", -1)) == int(node_id):
            t = graph["nodos"].get(int(f["toId"]))
            if t and t.get("type") == "agAgent":
                out.append(t)
    return out


def resources_of(graph, node_id):
    out = []
    for f in graph["flechas"]:
        if f.get("kind") == "usa" and int(f.get("fromId", -1)) == int(node_id):
            r = graph["nodos"].get(int(f["toId"]))
            if r and r.get("type") == "agResource" and (r.get("data") or {}).get("projectId"):
                out.append(r)
    return out


def _resolve_target(graph, node_id, name_or_id):
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
    if provider in CLI_PROVIDERS:
        raise OrchError(400, f"el nodo «{node.get('titulo')}» usa un CLI local ('{provider}'): en "
                             "conectores EXTERNOS las cabezas son solo APIs (decisión M)")
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
    raise OrchError(400, f"el nodo «{node.get('titulo')}» usa el proveedor '{provider}', que este "
                         "conector no soporta (APIs: Anthropic y OpenAI-compatible)")


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
    tools, execs, notes = [], {}, []
    for r in resources_of(graph, node_id):
        rid = f"r{r['id']}"
        rpid = r["data"]["projectId"]
        perm = PERM_LEVEL.get((r["data"] or {}).get("permiso") or "editar", 1)
        meta = project_meta(ctx, rpid)
        if not meta:
            notes.append(f"- {rid}: (proyecto borrado o de otra carpeta — no usar)")
            continue
        rtype = meta.get("type")
        label = f"{meta.get('name')} ({rtype}, permiso {r['data'].get('permiso')})"
        notes.append(f"- {rid}: {label}")
        if rtype == "editor":
            _editor_tools(ctx, rid, rpid, perm, tools, execs, author)
        else:
            _diagram_tools(ctx, rid, rpid, rtype, perm, tools, execs)
    return tools, execs, notes


def _fs_call(fn, **kw):
    """Reusa los endpoints de fs.py como funciones (mismas ops confinadas, doc 27)."""
    try:
        return json.dumps(fn(**kw), ensure_ascii=False), False
    except HTTPException as e:
        return str(e.detail), True


def _editor_tools(ctx, rid, rpid, perm, tools, execs, author):
    user = ctx["user"]
    def add(name, spec, fn):
        tools.append(dict(name=f"{rid}_{name}", **spec))
        execs[f"{rid}_{name}"] = fn
    add("fs_tree", _s("Lista UN nivel del proyecto editor (dirs primero).",
                      {"dir": {"type": "string"}}),
        lambda i: _fs_call(fsmod.fs_tree, projectId=rpid, dir=i.get("dir") or "", user=user))
    add("fs_read", _s("Lee un archivo (ruta relativa).", {"path": {"type": "string"}}, ["path"]),
        lambda i: _fs_call(fsmod.fs_read, projectId=rpid, path=i.get("path"), user=user))
    add("fs_grep", _s("Busca texto en los archivos.", {"q": {"type": "string"}, "glob": {"type": "string"}}, ["q"]),
        lambda i: _fs_call(fsmod.fs_grep, projectId=rpid, q=i.get("q"), glob=i.get("glob") or "", user=user))
    def sv_list_fn(i):
        try:
            svd, _t = _sv_ctx(rpid)
            return json.dumps(sourcever.sv_list(svd), ensure_ascii=False), False
        except OrchError as e:
            return e.msg, True
    add("sv_list", _s("Historial de versiones del proyecto."), sv_list_fn)
    if perm >= 1:
        add("fs_write", _s("Escribe un archivo COMPLETO (crea dirs).",
                           {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
            lambda i: _fs_call(fsmod.fs_write, body=FsWriteBody(projectId=rpid, path=i.get("path") or "",
                                                                content=i.get("content") or ""), user=user))
        add("fs_mkdir", _s("Crea un directorio.", {"path": {"type": "string"}}, ["path"]),
            lambda i: _fs_call(fsmod.fs_mkdir, body=FsPathBody(projectId=rpid, path=i.get("path") or ""), user=user))
        add("fs_rename", _s("Renombra/mueve dentro del proyecto.",
                            {"from": {"type": "string"}, "to": {"type": "string"}}, ["from", "to"]),
            lambda i: _fs_call(fsmod.fs_rename,
                               body=FsRenameBody(**{"projectId": rpid, "from": i.get("from") or "",
                                                    "to": i.get("to") or ""}), user=user))
        add("fs_delete", _s("Borra archivo o dir (recursivo).", {"path": {"type": "string"}}, ["path"]),
            lambda i: _fs_call(fsmod.fs_delete, body=FsPathBody(projectId=rpid, path=i.get("path") or ""), user=user))
        def sv_save_fn(i):
            try:
                svd, target = _sv_ctx(rpid)
                return json.dumps(sourcever.sv_save(svd, target, author, i.get("note") or ""),
                                  ensure_ascii=False), False
            except OrchError as e:
                return e.msg, True
        add("sv_save", _s("Guarda una VERSIÓN (snapshot) del proyecto. Usala ANTES de una tanda de cambios.",
                          {"note": {"type": "string"}}), sv_save_fn)
        def sv_restore_fn(i):
            try:
                svd, target = _sv_ctx(rpid)
                return json.dumps(sourcever.sv_restore(svd, target, i.get("id"), author),
                                  ensure_ascii=False), False
            except OrchError as e:
                return e.msg, True
        add("sv_restore", _s("Vuelve el proyecto a una versión (con snapshot de seguridad previo). Solo si te lo piden.",
                             {"id": {"type": "string"}}, ["id"]), sv_restore_fn)
    if perm >= 2:
        add("fs_exec", _s("Ejecuta un comando de shell en el proyecto (timeout 60s).",
                          {"cmd": {"type": "string"}}, ["cmd"]),
            lambda i: _fs_call(fsmod.fs_exec, body=FsExecBody(projectId=rpid, cmd=i.get("cmd") or ""), user=user))


def _diagram_tools(ctx, rid, rpid, rtype, perm, tools, execs):
    def view(i):
        tj = projects.read_tree(rpid)
        if tj is None:
            return "el proyecto no está sincronizado", True
        return tj, False
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
            projects.write_tree(rpid, json.dumps(obj, ensure_ascii=False))
            _notify_edit(rpid)
            return "OK: diagrama actualizado.", False
        tools.append(dict(name=f"{rid}_set_tree", **_s(
            f"Reemplaza el diagrama ENTERO con un JSON válido de tipo {rtype} (respetá su esquema EXACTO).",
            {"json": {"type": "string", "description": "el tree.json completo"}}, ["json"])))
        execs[f"{rid}_set_tree"] = set_tree


# ===================== system prompt =====================

def _load_type_skills():
    """Esquemas de los tipos de diagrama: se toman de local-backend/skills.py si el
    repo está entero (deploy normal desde diagramind-local/). Si no, se sigue sin
    ellos (el system igual exige respetar el esquema del view_tree)."""
    try:
        import importlib.util
        p = Path(__file__).resolve().parent.parent / "local-backend" / "skills.py"
        if not p.exists():
            return {}
        spec = importlib.util.spec_from_file_location("dm_local_skills", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return dict(mod.SKILLS)
    except Exception:
        return {}


TYPE_SKILLS = _load_type_skills()


def _skill_body(rtype):
    content = TYPE_SKILLS.get(f"diagramind-{str(rtype).lower()}") or ""
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
    tipos = set()
    for r in resources_of(graph, nid):
        meta = project_meta(ctx, r["data"].get("projectId"))
        if meta and meta.get("type") and meta["type"] != "editor":
            tipos.add(meta["type"])
    for t in sorted(tipos):
        body = _skill_body(t)
        if body:
            partes.append(f"ESQUEMA del tipo {t} (para view/set_tree):\n{body[:3500]}")
    partes.append(
        "REGLAS: 1) Trabajá SOLO en lo que te pidieron. 2) Usá `preguntar_al_usuario` ante decisiones "
        "importantes o contexto faltante. 3) Cerrá SIEMPRE tu turno con `responder` (resumen concreto y "
        "verificable). 4) Antes de editar un diagrama mirá su JSON actual (view_tree) y respetá su esquema "
        "EXACTO. 5) Respondé en español."
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


# ===================== snapshots pre-ejecución (decisión I, versionado de ESTE conector) =====================

def snapshot_resources(ctx, run, graph, node):
    name = node.get("titulo") or f"nodo {node['id']}"
    for r in resources_of(graph, node["id"]):
        if PERM_LEVEL.get((r["data"] or {}).get("permiso") or "editar", 1) < 1:
            continue
        rpid = r["data"]["projectId"]
        meta = project_meta(ctx, rpid)
        if not meta:
            continue
        try:
            if meta.get("type") == "editor":
                svd, target = _sv_ctx(rpid)
                sourcever.sv_save(svd, target, f"IA ({name})", f"(auto) run {run['id']}: turno de {name}")
            elif git_ops.has_changes(rpid):
                # commit del estado SIN guardar: eso ES el snapshot previo (si no hay
                # cambios, HEAD ya es el estado previo y no hace falta nada)
                git_ops.commit(rpid, f"IA ({name})", GIT_EMAIL,
                               f"(auto) run {run['id']}: snapshot pre-turno de {name}")
            emit(run, "log", nodeId=node["id"], text=f"snapshot pre-turno de {meta.get('name')}")
        except Exception as e:
            emit(run, "log", nodeId=node["id"], text=f"snapshot falló ({meta.get('name')}): {e}")


# ===================== locks por recurso/agente (decisión E) =====================

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
    run["_fseq"] = run.get("_fseq", 0) + 1
    fid = f"f{run['_fseq']}"
    make_adapter(ctx, node)   # valida provider (API-only) y que la key esté
    frame = {"id": fid, "nodeId": node["id"], "parentId": parent_id,
             "provider": ((node.get("data") or {}).get("ia") or {}).get("provider") or "anthropic",
             "entry": entry_kind, "status": "ready", "iters": 0, "firstText": initial_text,
             "inbox": [{"text": initial_text}], "join": None, "waiting": {}, "collected": [],
             "kind": "api", "messages": [], "pendingToolId": None, "stash": []}
    run["frames"][fid] = frame
    snapshot_resources(ctx, run, graph, node)
    set_node_state(ctx, run, node["id"], "running")
    emit(run, "log", nodeId=node["id"], text=f"→ entra trabajo: {initial_text[:200]}")
    return frame


def _finish_node(ctx, run, graph, frame, mensaje):
    node = _agent(graph, frame["nodeId"])
    d = node.get("data") or {}
    if (d.get("memoria") or {}).get("enabled", True):
        chat_id = run.get("chatId") if frame["entry"] == "chat" else None
        mem_append(ctx, node["id"], frame["entry"],
                   f"Tarea: {frame['firstText'][:400]} → Resultado: {mensaje[:700]}", chat_id)
    set_node_state(ctx, run, node["id"], "done")
    emit(run, "log", nodeId=node["id"], text=f"← responde: {mensaje[:200]}")


def _do_responder(ctx, graph, run, frame, mensaje):
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
    else:
        parent["collected"].append(texto)
        if not parent["waiting"]:
            parent["inbox"].append({"text": "\n\n".join(parent["collected"])})
            parent["collected"] = []
            parent["status"] = "ready"


def _implicit_end(ctx, graph, run, frame, texto):
    if frame["waiting"]:
        frame["status"] = "waiting_children"
        set_node_state(ctx, run, frame["nodeId"], "waiting")
        _release_locks(run, frame["id"])
        emit(run, "log", nodeId=frame["nodeId"], text="sigue esperando las respuestas pendientes")
        return
    _do_responder(ctx, graph, run, frame, texto)


def _do_delegar(ctx, graph, run, frame, node, inp):
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

def start_run(ctx, entry_kind, root_node_id, initial_text, api_keys, max_turns=None):
    with LOCK:
        prev = RUNS.get(ctx["pid"])
        if prev and prev["status"] in ("running", "waiting_human", "paused"):
            raise OrchError(409, "ya hay un run en curso en este orquestador: "
                                 "esperá, respondé lo pendiente o matalo")
        graph = load_graph(ctx)
        root = _agent(graph, root_node_id)
        KEYS[ctx["pid"]] = api_keys or {}
        run = {
            "id": "run" + uuid.uuid4().hex[:8], "projectId": ctx["pid"], "entry": entry_kind,
            "status": "running", "rootNodeId": root["id"], "final": None, "error": None,
            "turns": 0, "maxTurns": max_turns or MAX_TURNS_DEFAULT,
            "chatId": None, "pending": None, "pendings": [],
            "frames": {}, "locks": {}, "nodeStates": {}, "spend": {},
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
    rt = _rt(ctx["pid"])
    with LOCK:
        if rt["alive"]:
            rt["cv"].notify_all()
            return
        rt["alive"] = True
    threading.Thread(target=_loop, args=(ctx,), daemon=True).start()


def _loop(ctx):
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


def _worker(ctx, graph, run, frame):
    cv = _rt(ctx["pid"])["cv"]
    try:
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


# ===================== API de alto nivel =====================

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
            run["error"] = "el conector se reinició durante el run — relanzalo"
            _write_json(_run_path(ctx), run)
    if not run:
        return {"run": None}
    slim = {k: v for k, v in run.items()
            if not str(k).startswith("_") and k not in ("frames", "locks", "events")}
    slim["stackNodes"] = _active_nodes(run)
    return {"run": slim}


def answer(ctx, text, node_id=None):
    run = RUNS.get(ctx["pid"])
    if not run or run["status"] not in ("running", "waiting_human"):
        raise OrchError(409, "no hay ninguna pregunta pendiente (¿se reinició el conector?)")
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
    if ctx["pid"] not in KEYS:
        raise OrchError(409, "el conector se reinició: relanzá el run")
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
    with LOCK:
        if run["status"] == "running":
            run["_kill"] = True
            rt["cv"].notify_all()
        else:
            run["status"] = "killed"
            emit(run, "status", status="killed")
            _save(ctx, run)
            _archive_run(ctx, run)
    return {"ok": True}


def runs_list(ctx):
    idx = _read_json(_runs_index_path(ctx), [])
    live = RUNS.get(ctx["pid"])
    if live and not live.get("_archived"):
        idx = [x for x in idx if x.get("id") != live["id"]]
        idx.insert(0, {**_run_summary(live), "live": True})
    return {"runs": idx}


def run_detail(ctx, run_id):
    live = RUNS.get(ctx["pid"])
    if live and live["id"] == run_id:
        d = {k: v for k, v in live.items()
             if not str(k).startswith("_") and k not in ("frames", "locks")}
        d["live"] = live["status"] in ("running", "waiting_human", "paused")
        d["stackNodes"] = _active_nodes(live)
        return {"run": d}
    data = _read_json(_runs_dir(ctx) / f"{run_id}.json", None)
    if not data:
        raise OrchError(404, "no existe ese run en el historial")
    return {"run": data}


def events_since(ctx, since):
    run = RUNS.get(ctx["pid"])
    if not run:
        return [], since, "none"
    evs = run["events"][since:]
    return evs, since + len(evs), run["status"]


# ===================== endpoints (TODOS solo-admin: decisión Q) =====================

router = APIRouter(tags=["orchestrator"])


class OrchRunBody(BaseModel):
    projectId: str
    taskNodeId: int
    apiKeys: dict | None = None
    maxTurns: int | None = None


class OrchChatBody(BaseModel):
    projectId: str
    nodeId: int
    message: str
    apiKeys: dict | None = None
    maxTurns: int | None = None


class OrchAnswerBody(BaseModel):
    projectId: str
    text: str
    nodeId: int | None = None


class OrchPidBody(BaseModel):
    projectId: str


class OrchNodeBody(BaseModel):
    projectId: str
    nodeId: int


def _orch(pid, user, fn):
    try:
        return fn(make_ctx(pid, user))
    except OrchError as e:
        raise HTTPException(status_code=e.code, detail=e.msg)


@router.post("/orch/run")
def orch_run(body: OrchRunBody, user: dict = Depends(require_admin)):
    def go(ctx):
        graph = load_graph(ctx)
        task = graph["nodos"].get(int(body.taskNodeId))
        if not task or task.get("type") != "agTask":
            raise OrchError(400, "taskNodeId no es un nodo tarea")
        edge = next((f for f in graph["flechas"]
                     if f.get("kind") == "task" and int(f.get("fromId", -1)) == int(body.taskNodeId)), None)
        if not edge:
            raise OrchError(400, "la tarea no está conectada a un agente (flecha task)")
        texto = f"TAREA «{task.get('titulo') or 'tarea'}»: {(task.get('data') or {}).get('enunciado') or ''}"
        run = start_run(ctx, "task", int(edge["toId"]), texto, body.apiKeys or {}, body.maxTurns)
        return {"runId": run["id"]}
    return _orch(body.projectId, user, go)


@router.post("/orch/chat")
def orch_chat(body: OrchChatBody, user: dict = Depends(require_admin)):
    return _orch(body.projectId, user,
                 lambda ctx: chat_message(ctx, body.nodeId, body.message, body.apiKeys or {}, body.maxTurns))


@router.get("/orch/state")
def orch_state(projectId: str = Query(...), user: dict = Depends(require_admin)):
    return _orch(projectId, user, get_state)


@router.get("/orch/events")
def orch_events(projectId: str = Query(...), since: int = 0, user: dict = Depends(require_admin)):
    def go(ctx):
        evs, nxt, status = events_since(ctx, since)
        return {"events": evs, "next": nxt, "status": status}
    return _orch(projectId, user, go)


@router.post("/orch/answer")
def orch_answer(body: OrchAnswerBody, user: dict = Depends(require_admin)):
    return _orch(body.projectId, user, lambda ctx: answer(ctx, body.text or "", body.nodeId))


@router.post("/orch/pause")
def orch_pause(body: OrchPidBody, user: dict = Depends(require_admin)):
    return _orch(body.projectId, user, pause)


@router.post("/orch/resume")
def orch_resume(body: OrchPidBody, user: dict = Depends(require_admin)):
    return _orch(body.projectId, user, resume)


@router.post("/orch/kill")
def orch_kill(body: OrchPidBody, user: dict = Depends(require_admin)):
    return _orch(body.projectId, user, kill)


@router.get("/orch/chatlog")
def orch_chatlog(projectId: str = Query(...), nodeId: int = Query(...),
                 user: dict = Depends(require_admin)):
    return _orch(projectId, user, lambda ctx: chat_read(ctx, nodeId))


@router.get("/orch/mem")
def orch_mem(projectId: str = Query(...), nodeId: int = Query(...),
             user: dict = Depends(require_admin)):
    def go(ctx):
        return {"entries": mem_read(ctx, nodeId), "chars": mem_chars(ctx, nodeId)}
    return _orch(projectId, user, go)


@router.post("/orch/memclear")
def orch_memclear(body: OrchNodeBody, user: dict = Depends(require_admin)):
    def go(ctx):
        mem_clear(ctx, body.nodeId)
        return {"ok": True}
    return _orch(body.projectId, user, go)


@router.post("/orch/chatclear")
def orch_chatclear(body: OrchNodeBody, user: dict = Depends(require_admin)):
    return _orch(body.projectId, user, lambda ctx: chat_clear(ctx, body.nodeId))


@router.get("/orch/runs")
def orch_runs(projectId: str = Query(...), user: dict = Depends(require_admin)):
    return _orch(projectId, user, runs_list)


@router.get("/orch/rundetail")
def orch_rundetail(projectId: str = Query(...), runId: str = Query(...),
                   user: dict = Depends(require_admin)):
    return _orch(projectId, user, lambda ctx: run_detail(ctx, runId))

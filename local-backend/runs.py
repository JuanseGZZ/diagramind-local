"""Máquina de estados de los 'runs' (un turno disparado contra un CLI).
Estados: queued → starting → streaming → done | error | cancelled.
Cada run guarda eventos con seq incremental para que el SSE reconecte sin perder.
Compartido por server.py (crea/lee runs) y los adaptadores de CLI (emiten eventos)."""
import threading
import uuid

RUNS = {}
RUNS_LOCK = threading.Lock()
# mapeo (sesión web, carpeta, cli) → session id del CLI (solo para los que resumen)
SESSION_MAP = {}


def new_run():
    rid = uuid.uuid4().hex
    run = {
        "id": rid,
        "status": "queued",
        "events": [],          # [{seq, kind, ...}]
        "seq": 0,
        "proc": None,
        "claude_session_id": None,   # lo setea el adaptador que soporte resume
        "error": None,
    }
    with RUNS_LOCK:
        RUNS[rid] = run
    return run


def emit(run, kind, **data):
    with RUNS_LOCK:
        run["seq"] += 1
        run["events"].append({"seq": run["seq"], "kind": kind, **data})


def set_status(run, status, error=None):
    run["status"] = status
    if error:
        run["error"] = error
    emit(run, "status", status=status, error=error,
         sessionId=run.get("claude_session_id"))
    if status in ("done", "error", "cancelled"):
        perm_release(run)          # nadie va a contestar ya: destrabar lo que espere


# ===================== permisos en vivo (permission_mcp.py) =====================
# Claude Code corre headless: lo que necesita aprobación se auto-deniega. Con
# `--permission-prompt-tool` la CLI nos pregunta, y la pregunta viaja al chat de la
# web por el MISMO stream de eventos del run. Acá vive la espera: un Event por
# pedido, que destraba la respuesta del usuario (o el fin del run).

PERM_TIMEOUT = 900      # 15 min sin respuesta = denegado (la CLI no puede esperar para siempre)


def perm_ask(run, tool, tool_input, tool_use_id):
    """Emite el pedido al chat y BLOQUEA hasta que el usuario conteste.
    Devuelve {"decision": "allow"|"deny", "input": {...}, "message": str}."""
    with RUNS_LOCK:
        run["_perm_n"] = run.get("_perm_n", 0) + 1
        pid = f"p{run['_perm_n']}"
        pend = run.setdefault("perms", {})
        pend[pid] = {"event": threading.Event(), "answer": None}
    emit(run, "permission", id=pid, tool=tool, input=tool_input, toolUseId=tool_use_id)

    ok = pend[pid]["event"].wait(PERM_TIMEOUT)
    with RUNS_LOCK:
        entry = run.get("perms", {}).pop(pid, None)
    ans = (entry or {}).get("answer")
    if not ok or not ans:
        motivo = "The run was cancelled." if run.get("status") == "cancelled" else \
                 "Nobody answered the permission request in time."
        # el chat ya no espera: se avisa para poder sacar la tarjeta de la pantalla
        emit(run, "permission-resolved", id=pid, decision="deny")
        return {"decision": "deny", "message": motivo}
    emit(run, "permission-resolved", id=pid, decision=ans.get("decision") or "deny")
    return ans


def perm_answer(run, pid, decision, message=None, tool_input=None):
    """Lo que contestó el usuario en la web. True si había alguien esperando."""
    with RUNS_LOCK:
        entry = run.get("perms", {}).get(pid)
        if not entry:
            return False
        entry["answer"] = {"decision": "allow" if decision == "allow" else "deny",
                           "message": message, "input": tool_input}
    entry["event"].set()
    return True


def perm_release(run):
    """Destraba todo pedido pendiente (el run terminó o se canceló)."""
    with RUNS_LOCK:
        pend = list(run.get("perms", {}).values())
    for e in pend:
        e["event"].set()

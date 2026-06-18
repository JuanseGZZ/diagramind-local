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

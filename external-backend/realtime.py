"""WebSocket realtime — el corazón del mirror (ver [[25 - Conector Externo v2]] §5).

Modelo: **last-write-wins por documento, server autoridad + broadcaster.**
- Un **room por proyecto** (`projectId`). Todos los que abren ese proyecto entran.
- El server **persiste** cada `edit` en el working tree y lo **retransmite a TODOS**
  (incluido el emisor). Cada cliente **ignora su propio eco** por `originId`. El server
  **NUNCA** suprime el broadcast (fue *el* bug del §10.1).
- **Presencia + cursores** por room. **Read-only**: el cliente `read` no debería mandar
  `edit`; si igual se cuela, el server lo **rechaza** y le reenvía el estado canónico
  (su cambio optimista se revierte).

Auth: el WS no lleva headers → se canjea un **ws-ticket** (query `?ticket=`) de un solo
uso al abrir (§3/§10).

Protocolo JSON (campo `t` = tipo):
  cliente→server: open{projectId} · edit{projectId,tree,originId} ·
                  cursor{projectId,x,y,editing} · close{projectId}
  server→cliente: state{projectId,tree,seq} · edit{projectId,tree,originId,by,seq} ·
                  presence{projectId,members[]} · cursor{projectId,...} ·
                  readonly{projectId} · error{code,detail}
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from auth import consume_ws_ticket
from projects import TypeNotAllowed, read_tree, write_tree
import ratelimit
from quota import QuotaExceeded
from store import project_permission

router = APIRouter()

# paleta estable de colores de cursor/presencia (por id de usuario)
_COLORS = ["#ef4444", "#f59e0b", "#22c55e", "#3b82f6", "#a855f7", "#ec4899", "#14b8a6", "#eab308"]


def _color_for(uid: int) -> str:
    return _COLORS[uid % len(_COLORS)]


class Room:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.members: dict[WebSocket, dict] = {}   # ws -> user info {id, username, color, perm}
        self.seq = 0
        self.lock = asyncio.Lock()                 # serializa persist+broadcast del edit


class RoomManager:
    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.loop: asyncio.AbstractEventLoop | None = None   # ver register()
        # Registro GLOBAL de sockets (ws -> user). Los rooms son POR PROYECTO, así que
        # sin esto no había forma de avisar cosas de CARPETA: "se creó un proyecto"
        # (nadie está suscrito a uno que no existía) ni "el MCP está trabajando".
        self.sockets: dict[WebSocket, dict] = {}

    def register(self, ws: WebSocket, user: dict) -> None:
        self.sockets[ws] = user
        # El loop se captura acá porque `ws_endpoint` SÍ corre en él. Los endpoints
        # REST de content.py son `def` (threadpool) y no pueden await: para avisarles
        # algo a los clientes necesitan `notify_user_soon`, que usa esta referencia.
        # Si nunca se conectó nadie, tampoco hay a quién avisar.
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    async def notify_user(self, user_id: int, msg: dict) -> None:
        """Manda `msg` a TODOS los sockets de ese usuario (puede tener varias pestañas)."""
        data = json.dumps(msg)
        dead = []
        for ws, user in list(self.sockets.items()):
            if user.get("id") != user_id:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.pop(ws, None)

    def notify_user_soon(self, user_id: int, msg: dict) -> None:
        """Versión llamable desde código SÍNCRONO (los endpoints REST). Best-effort:
        si el aviso no sale, el cliente igual se entera en el próximo sync."""
        loop = getattr(self, "loop", None)
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.notify_user(user_id, msg), loop)
        except Exception:
            pass

    def unregister(self, ws: WebSocket) -> None:
        self.sockets.pop(ws, None)

    async def notify_folder(self, folder_id: str, msg: dict) -> None:
        """Manda `msg` a todos los sockets cuyo usuario VE esa carpeta. Se re-chequea
        el permiso en el momento (no se cachea): si alguien perdió el acceso, no le
        llega."""
        import store as _store
        dead = []
        for ws, user in list(self.sockets.items()):
            try:
                if _store.folder_permission(user, folder_id) == "none":
                    continue
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.pop(ws, None)

    def _room(self, project_id: str) -> Room:
        r = self.rooms.get(project_id)
        if r is None:
            r = self.rooms[project_id] = Room(project_id)
        return r

    async def join(self, ws: WebSocket, project_id: str, member: dict) -> Room:
        room = self._room(project_id)
        room.members[ws] = member
        return room

    def leave(self, ws: WebSocket, project_id: str) -> Room | None:
        room = self.rooms.get(project_id)
        if not room:
            return None
        room.members.pop(ws, None)
        if not room.members:
            self.rooms.pop(project_id, None)   # room vacío se descarta (seq se reinicia)
        return room

    def presence(self, project_id: str) -> list[dict]:
        """Quiénes están en la sala: PERSONAS, no sockets.

        Una persona con dos pestañas abiertas son dos WebSockets, y la lista salía
        con el mismo nombre repetido ("juan · juan · 2 en línea"). Se agrupa por
        usuario y se informa cuántas sesiones tiene (`sessions`), que es lo que de
        verdad quiere saber quien mira: cuánta gente hay.
        """
        room = self.rooms.get(project_id)
        if not room:
            return []
        por_usuario: dict = {}
        for m in room.members.values():
            uid = m["id"]
            if uid in por_usuario:
                por_usuario[uid]["sessions"] += 1
            else:
                por_usuario[uid] = {"id": uid, "username": m["username"],
                                    "color": m["color"], "sessions": 1}
        return list(por_usuario.values())

    async def broadcast(self, project_id: str, msg: dict, exclude: WebSocket | None = None) -> None:
        room = self.rooms.get(project_id)
        if not room:
            return
        data = json.dumps(msg)
        dead = []
        for ws in list(room.members.keys()):
            if ws is exclude:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            room.members.pop(ws, None)


manager = RoomManager()


async def push_canonical(pid: str) -> None:
    """Difunde el estado canónico del proyecto a su room (tras un cambio hecho fuera
    del WS, p.ej. un rollback de versionado). Bumpea el seq. No-op si no hay room."""
    room = manager.rooms.get(pid)
    if not room:
        return
    async with room.lock:
        room.seq += 1
        await manager.broadcast(pid, {"t": "state", "projectId": pid,
                                      "tree": read_tree(pid), "seq": room.seq})


async def _send(ws: WebSocket, msg: dict) -> None:
    await ws.send_text(json.dumps(msg))


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    user = consume_ws_ticket(ws.query_params.get("ticket"))
    if not user:
        await ws.close(code=4401)               # ticket inválido/vencido/usado
        return
    await ws.accept()

    member = {"id": user["id"], "username": user["username"],
              "color": _color_for(user["id"])}
    manager.register(ws, user)          # canal de carpeta (proyecto nuevo / actividad MCP)
    joined: dict[str, str] = {}             # pid -> permiso efectivo ('read' | 'write')

    async def do_presence(pid: str):
        await manager.broadcast(pid, {"t": "presence", "projectId": pid,
                                      "members": manager.presence(pid)})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(ws, {"t": "error", "code": "bad_json", "detail": "invalid JSON"})
                continue
            t = msg.get("t")
            pid = msg.get("projectId")

            if t == "open":
                if not pid:
                    await _send(ws, {"t": "error", "code": "no_project", "detail": "projectId required"})
                    continue
                perm = project_permission(user, pid)
                if perm == "none":
                    # sin acceso (o proyecto inexistente): ni siquiera lo ve
                    await _send(ws, {"t": "error", "code": "forbidden", "detail": "no access to project"})
                    continue
                await manager.join(ws, pid, member)
                joined[pid] = perm
                tree = read_tree(pid)
                room = manager.rooms[pid]
                await _send(ws, {"t": "state", "projectId": pid, "tree": tree, "seq": room.seq})
                if perm != "write":
                    await _send(ws, {"t": "readonly", "projectId": pid})
                await do_presence(pid)

            elif t == "edit":
                if not pid or pid not in joined:
                    await _send(ws, {"t": "error", "code": "not_open", "detail": "open the project first"})
                    continue
                # Cada `edit` ESCRIBE el árbol en disco y lo difunde a la sala: es la
                # operación más cara del socket. La web manda con debounce (unas pocas
                # por minuto), así que 120/min es holgado — y corta a quien tenga el
                # socket abierto mandando en bucle. Los `cursor` NO se limitan: son
                # baratos, no tocan disco, y limitarlos rompería la colaboración.
                try:
                    ratelimit.check("ws_edit", user["id"])
                except HTTPException as e:
                    await _send(ws, {"t": "error", "code": "rate_limited",
                                     "detail": e.detail,
                                     "retryAfter": int((e.headers or {}).get("Retry-After", 0) or 0)})
                    continue
                if joined[pid] != "write":
                    # read-only: rechazar y reenviar el estado canónico → revierte lo optimista
                    await _send(ws, {"t": "readonly", "projectId": pid})
                    await _send(ws, {"t": "state", "projectId": pid,
                                     "tree": read_tree(pid), "seq": manager.rooms[pid].seq})
                    continue
                tree = msg.get("tree")
                if not isinstance(tree, str):
                    tree = json.dumps(tree)      # aceptar objeto o string
                room = manager.rooms[pid]
                async with room.lock:
                    try:
                        write_tree(pid, tree)
                    except QuotaExceeded as e:
                        # cuota llena: rechazar + reenviar el canónico → revierte lo optimista
                        await _send(ws, {"t": "error", "code": "quota_exceeded", "detail": str(e)})
                        await _send(ws, {"t": "state", "projectId": pid,
                                         "tree": read_tree(pid), "seq": room.seq})
                        continue
                    except TypeNotAllowed as e:
                        # Tipo no alojable acá (Editor/Orchestrator en el free). Mismo
                        # patrón que la cuota: se rechaza Y se reenvía el canónico,
                        # para que el cliente revierta lo que puso optimista.
                        await _send(ws, {"t": "error", "code": "type_not_allowed",
                                         "detail": str(e), "treeType": e.tree_type})
                        await _send(ws, {"t": "state", "projectId": pid,
                                         "tree": read_tree(pid), "seq": room.seq})
                        continue
                    except ValueError:
                        await _send(ws, {"t": "error", "code": "bad_tree", "detail": "tree is not valid JSON"})
                        continue
                    room.seq += 1
                    # broadcast a TODOS (incluido el emisor); el emisor ignora por originId
                    await manager.broadcast(pid, {
                        "t": "edit", "projectId": pid, "tree": tree,
                        "originId": msg.get("originId"),
                        "by": {"id": user["id"], "username": user["username"]},
                        "seq": room.seq,
                    })

            elif t == "cursor":
                if not pid or pid not in joined:
                    continue
                await manager.broadcast(pid, {
                    "t": "cursor", "projectId": pid,
                    "userId": user["id"], "username": user["username"],
                    "color": member["color"],
                    "x": msg.get("x"), "y": msg.get("y"), "editing": bool(msg.get("editing")),
                }, exclude=ws)

            elif t == "close":
                if pid and pid in joined:
                    manager.leave(ws, pid)
                    joined.pop(pid, None)
                    await do_presence(pid)

            else:
                await _send(ws, {"t": "error", "code": "unknown", "detail": f"unknown type {t!r}"})

    except WebSocketDisconnect:
        pass
    finally:
        manager.unregister(ws)
        for pid in list(joined):
            manager.leave(ws, pid)
            await do_presence(pid)

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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth import consume_ws_ticket
from projects import read_tree, write_tree
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
        room = self.rooms.get(project_id)
        if not room:
            return []
        return [{"id": m["id"], "username": m["username"], "color": m["color"]}
                for m in room.members.values()]

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
        for pid in list(joined):
            manager.leave(ws, pid)
            await do_presence(pid)

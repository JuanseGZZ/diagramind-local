"""MCP server (stdio) que le PREGUNTA AL USUARIO, en el chat de la web, si deja
correr una tool que Claude Code no puede auto-aprobar.

EL PROBLEMA QUE RESUELVE. El chat corre `claude -p` (headless): no hay terminal
donde apretar "sí". Cualquier cosa que necesite aprobación —un `unzip`, un
`pdftotext`, un comando fuera de la whitelist— se auto-deniega y el modelo lo
cuenta como si no pudiera hacerlo ("queda bloqueado pidiendo aprobación en esta
sesión no interactiva"). El usuario ve una capacidad perdida, no un permiso.

CÓMO. Claude Code acepta `--permission-prompt-tool mcp__<server>__<tool>`: cuando
una llamada llega al paso de "preguntar", en vez de morir invoca ESA tool. Acá
esa tool reenvía la pregunta al backend local, que la emite por el MISMO SSE del
chat (`kind: "permission"`), la web dibuja la tarjeta con Permitir / Rechazar y la
respuesta vuelve por HTTP. La CLI queda esperando mientras tanto: es un bloqueo a
propósito (así lo espera `--permission-prompt-tool`).

CONTRATO, VERIFICADO CONTRA EL CLI REAL (2.1.263, 2026-09-11) — no copiado de una
doc, que es la regla del CLAUDE.md:

    tools/call → {"name": "approve", "arguments": {
        "tool_name": "Bash",
        "input": {"command": "echo hola > salida.txt", "description": "..."},
        "tool_use_id": "toolu_011onk…"}}

    respuesta (el TEXTO del content es un JSON):
        {"behavior": "allow", "updatedInput": {...}}   ← corre con ese input
        {"behavior": "deny",  "message": "..."}        ← no corre; el modelo LEE el mensaje

Se comprobó de punta a punta: con `allow`, el comando se ejecutó de verdad.

Env: DMPERM_URL (base del backend local), DMPERM_TOKEN (su token), DMPERM_RUN (el
run del chat, para que la pregunta caiga en la conversación correcta).

Se lanza re-ejecutando el propio backend con `--mcp-permission` (igual que
`--mcp-fs`), así también funciona en el binario onefile.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = ""
TOKEN = ""
RUN = ""

# Sin tope propio: el usuario puede tardar lo que quiera. Quien corta es el backend
# (si el run se cancela) o el usuario con el botón Detener del chat. El timeout del
# socket es alto y se reintenta, para que una siesta larga no se lea como "denegado".
POLL_TIMEOUT = 600

TOOLS = [{
    "name": "approve",
    # Descripción en inglés (la lee el MODELO) — regla del CLAUDE.md.
    "description": "Asks the human for permission to run a tool call. Claude Code calls "
                   "this automatically through --permission-prompt-tool; never call it yourself.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}]


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is None:
        msg["result"] = result or {}
    else:
        msg["error"] = error
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _ask_user(tool_name, tool_input, tool_use_id):
    """Le pregunta al usuario por el chat. Devuelve el dict que espera la CLI.

    Si el backend no contesta (se cerró, el run se canceló, la web no está), se
    DENIEGA con un motivo entendible: dejar la CLI colgada para siempre sería peor,
    y el modelo puede leer el mensaje y seguir por otro lado."""
    body = json.dumps({
        "runId": RUN, "tool": tool_name, "input": tool_input, "toolUseId": tool_use_id,
    }).encode("utf-8")
    req = urllib.request.Request(BASE + "/chat/permission/ask", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-DiagraMind-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"behavior": "deny",
                "message": f"The app could not ask the user for permission (HTTP {e.code}). "
                           f"Do not retry this call; tell the user what you need."}
    except Exception as e:
        return {"behavior": "deny",
                "message": f"The app could not ask the user for permission ({e}). "
                           f"Do not retry this call; tell the user what you need."}

    if data.get("decision") == "allow":
        # `updatedInput` es OBLIGATORIO en un allow: sin él la CLI lo trata como
        # inválido y termina denegando (ver doc del SDK, canUseTool).
        return {"behavior": "allow", "updatedInput": data.get("input") or tool_input}
    return {"behavior": "deny",
            "message": data.get("message") or "The user denied this action."}


def main():
    global BASE, TOKEN, RUN
    BASE = (os.environ.get("DMPERM_URL") or "").rstrip("/")
    TOKEN = os.environ.get("DMPERM_TOKEN") or ""
    RUN = os.environ.get("DMPERM_RUN") or ""
    if not BASE or not TOKEN or not RUN:
        print("faltan DMPERM_URL / DMPERM_TOKEN / DMPERM_RUN", file=sys.stderr)
        sys.exit(2)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}

        if method.startswith("notifications/"):
            continue
        if method == "initialize":
            _reply(mid, {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dmperm", "version": "1.0.0"},
            })
        elif method == "ping":
            _reply(mid, {})
        elif method == "tools/list":
            _reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            args = params.get("arguments") or {}
            out = _ask_user(args.get("tool_name") or "", args.get("input") or {},
                            args.get("tool_use_id") or "")
            _reply(mid, {"content": [{"type": "text", "text": json.dumps(out)}]})
        elif mid is not None:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()

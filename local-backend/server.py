#!/usr/bin/env python3
"""DiagraMind — backend local (paso 1).

Servidor mínimo que corre en la máquina de cada persona. La web ("Conectar
local") lo detecta vía /health y lo usa como backend. Más adelante se le van a
agregar endpoints que hablan con Claude Code (chat, generar soft, inyectar en
la web, etc.) y las skills correspondientes.

Diseño:
- Solo stdlib de Python 3 → cero dependencias, multiplataforma.
- Escucha SOLO en 127.0.0.1 (loopback) → no queda expuesto a la red.
- CORS abierto para que la web (sea file:// u otro origen) pueda consultarlo.

Uso:
    python3 server.py            # puerto por defecto 8765
    python3 server.py --port N   # otro puerto

Detener: Ctrl+C
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
NAME = "diagramind-local"
VERSION = "0.1.0"


class Handler(BaseHTTPRequestHandler):
    server_version = f"{NAME}/{VERSION}"

    # --- helpers ---------------------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- verbos ----------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "name": NAME, "version": VERSION})
        else:
            self._json(404, {"error": "not found", "path": self.path})

    # log un poco más prolijo
    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="DiagraMind backend local")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"puerto (default {DEFAULT_PORT})")
    args = parser.parse_args()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"DiagraMind local backend → http://{HOST}:{args.port}")
    print("Endpoints: GET /health")
    print("Ctrl+C para detener.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDeteniendo…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

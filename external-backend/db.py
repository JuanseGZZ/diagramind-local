"""Capa sqlite del conector externo.

sqlite3 de la stdlib (sync). Se abre una conexión por operación (barato) con
`row_factory` para leer por nombre de columna. El schema se crea idempotente en
`init_db()` al arrancar.

Tablas (ver [[25 - Conector Externo v2]] §3/§6):
  users          — cuentas + rol + must_change_pw + token_version + disabled
  acl            — permiso por (usuario, carpeta): none|read|write (default: sin fila = none)
  refresh_tokens — un row por refresh emitido (jti); rotación = revocar el viejo
  ws_tickets     — ticket corto de un solo uso para abrir el WebSocket
  folders        — carpeta con id ESTABLE asignado por el conector (namespace canónico)
  projects       — proyecto con id estable, dentro de una carpeta (id = identidad; dirname = disco)
"""

import sqlite3
from contextlib import contextmanager

from config import DB_PATH, ensure_home

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  username       TEXT    NOT NULL UNIQUE,
  password_hash  TEXT    NOT NULL,
  role           TEXT    NOT NULL DEFAULT 'viewer',   -- admin | editor | viewer
  must_change_pw INTEGER NOT NULL DEFAULT 0,
  token_version  INTEGER NOT NULL DEFAULT 0,          -- bump = invalida todos los tokens
  disabled       INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS acl (
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  folder_id  TEXT    NOT NULL,
  permission TEXT    NOT NULL,                        -- read | write (ausencia = none)
  PRIMARY KEY (user_id, folder_id)
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
  jti        TEXT    PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_version INTEGER NOT NULL,                     -- snapshot del tv al emitir
  expires_at TEXT    NOT NULL,
  revoked    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ws_tickets (
  ticket     TEXT    PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT    NOT NULL,
  used       INTEGER NOT NULL DEFAULT 0,
  created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS folders (
  id         TEXT    PRIMARY KEY,                    -- id estable asignado por el conector
  name       TEXT    NOT NULL,                       -- nombre visible (rename = solo esto)
  dirname    TEXT    NOT NULL UNIQUE,                -- nombre de carpeta en disco (estable)
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
  id         TEXT    PRIMARY KEY,                    -- id estable asignado por el conector
  folder_id  TEXT    NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
  name       TEXT    NOT NULL,
  dirname    TEXT    NOT NULL,                       -- nombre de dir en disco (estable, único por carpeta)
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (folder_id, dirname)
);
"""


@contextmanager
def connect():
    """Abre una conexión sqlite con FK on y row_factory por nombre. Commit al salir OK."""
    ensure_home()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Crea el schema si no existe (idempotente)."""
    with connect() as conn:
        conn.executescript(SCHEMA)

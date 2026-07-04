"""Seguridad: hashing de passwords (bcrypt directo) y tokens (PyJWT).

Trampas ya conocidas (ver [[25 - Conector Externo v2]] §3/§10):
- **bcrypt directo** (no passlib, que rompe con bcrypt 4.x+/Py3.14) + **truncar a 72 bytes**
  (bcrypt ignora lo que pasa de 72; truncamos explícito para no depender del backend).
- **PyJWT ≥ 2.10** exige `sub` **string** → guardamos `str(user_id)`.
- Access corto + **refresh largo con rotación** + `token_version` (invalida todo al cambiar
  password / deshabilitar / logout-all).
- **ws-ticket** stateful corto (un solo uso), porque el WS no lleva headers.
"""

import secrets
import time

import bcrypt
import jwt

# Trampa conocida: existe OTRO paquete PyPI llamado "jwt" (GehirnInc) que NO tiene
# jwt.encode(). Si el server corre con un Python que lo tenga (p.ej. el del sistema
# en vez del .venv), el login revienta con un 500 críptico. Cortar acá con un
# mensaje claro.
if not hasattr(jwt, "encode"):
    raise SystemExit(
        "[connector] El módulo 'jwt' importado NO es PyJWT "
        f"(vino de {getattr(jwt, '__file__', '?')}).\n"
        "  Probablemente estés usando el Python del sistema en vez del venv.\n"
        "  Corré:  .venv/bin/python server.py\n"
        "  (o en ese Python: pip uninstall jwt && pip install PyJWT)"
    )

from config import ACCESS_TTL, JWT_ALG, REFRESH_TTL, get_secret


# ---------------- passwords (bcrypt) ----------------

def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:72]          # bcrypt: máx 72 bytes
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        pw = password.encode("utf-8")[:72]
        return bcrypt.checkpw(pw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def random_password(nbytes: int = 12) -> str:
    """Password temporal legible (para bootstrap admin / alta de usuario)."""
    return secrets.token_urlsafe(nbytes)


# ---------------- JWT (access / refresh) ----------------

def _now() -> int:
    return int(time.time())


def make_access_token(user_id: int, role: str, token_version: int) -> str:
    payload = {
        "sub": str(user_id),        # PyJWT exige sub string
        "role": role,
        "tv": token_version,
        "type": "access",
        "iat": _now(),
        "exp": _now() + ACCESS_TTL,
    }
    return jwt.encode(payload, get_secret(), algorithm=JWT_ALG)


def make_refresh_token(user_id: int, token_version: int) -> tuple[str, str, int]:
    """Devuelve (token, jti, expires_at_epoch). El jti se persiste en refresh_tokens."""
    jti = secrets.token_urlsafe(24)
    exp = _now() + REFRESH_TTL
    payload = {
        "sub": str(user_id),
        "tv": token_version,
        "type": "refresh",
        "jti": jti,
        "iat": _now(),
        "exp": exp,
    }
    token = jwt.encode(payload, get_secret(), algorithm=JWT_ALG)
    return token, jti, exp


def decode_token(token: str) -> dict | None:
    """Decodifica y valida firma + exp. None si es inválido/expirado."""
    try:
        return jwt.decode(token, get_secret(), algorithms=[JWT_ALG])
    except jwt.PyJWTError:
        return None


# ---------------- ws-ticket ----------------

def new_ws_ticket() -> str:
    return secrets.token_urlsafe(24)

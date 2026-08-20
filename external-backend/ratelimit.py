"""Rate limit POR USUARIO para los endpoints caros (SaaS compartido — doc 26 riesgo 3).

Por qué en el SERVER y no en el botón: el botón "Guardar" ya no dispara commits de más,
pero eso es UX. Cualquiera puede mandar la misma request con `curl` en un bucle, y cada
commit es trabajo real de disco y de git. La regla vive acá.

Ventana deslizante simple en memoria: para cada (bucket, usuario) se guardan los
timestamps de la ventana y se cuenta. Alcanza porque el conector es UN proceso
(StatefulSet de 1 réplica); si algún día hay varias, esto se muda a la base o a Redis.

Al pasarse devuelve **429** con `Retry-After` en segundos, que es lo que el cliente
necesita para esperar en vez de reintentar a ciegas.
"""

import os
import threading
import time

from fastapi import HTTPException

PREFIX = "DMC_RL_"

# bucket -> (cantidad máxima, ventana en segundos)
LIMITS = {
    # Guardar = commit de git: caro y spameable desde el botón. 20/min deja trabajar
    # tranquilo (un save cada 3 s sostenido) y corta el bucle.
    "commit": (20, 60),
    # Subir blobs de documentos: pesado en disco. 120/min permite cargar una tanda
    # grande de archivos sin frenar a nadie que trabaje de verdad.
    "docs_put": (120, 60),
    # Rollback y restore tocan el árbol entero: son excepcionales.
    "restore": (10, 60),
    # Ediciones por WebSocket: cada una escribe el árbol y lo difunde a la sala. La web
    # manda con debounce (unas pocas por minuto); 120 deja lugar de sobra para trabajar
    # rápido y para varias pestañas del mismo usuario.
    "ws_edit": (120, 60),
}

def _env_override():
    """`DMN_RL_<BUCKET>` / `DMC_RL_<BUCKET>` = "cantidad/segundos" (ej. "5/3600").
    Poder ajustar un límite sin rebuildear la imagen importa el día que uno esté mal
    calibrado y haya gente trabajando del otro lado."""
    for name in list(LIMITS):
        raw = os.environ.get(PREFIX + name.upper())
        if not raw:
            continue
        try:
            n, w = raw.split("/")
            LIMITS[name] = (int(n), int(w))
        except ValueError:
            pass


_env_override()

_hits: dict[tuple[str, str], list[float]] = {}
_lock = threading.Lock()


def check(bucket: str, user_id) -> None:
    """Cuenta un uso de `bucket` para `user_id`. HTTPException 429 si se pasó."""
    limit, window = LIMITS.get(bucket, (0, 0))
    if not limit:
        return
    key = (bucket, str(user_id))
    now = time.monotonic()
    with _lock:
        hits = [t for t in _hits.get(key, []) if now - t < window]
        if len(hits) >= limit:
            retry = max(1, int(window - (now - hits[0])) + 1)
            _hits[key] = hits
            raise HTTPException(
                status_code=429,
                detail=f"too many requests — retry in {retry}s",
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)
        _hits[key] = hits


def reset() -> None:
    """Solo para los tests."""
    with _lock:
        _hits.clear()

# DiagraMind — Conector Externo (server base)

Server multiusuario que hace de **fuente de verdad** de un repo de proyectos
compartido. Es el rebuild del doc `25 - Conector Externo v2`. Este paso cubre la
**base**: auth (bcrypt/JWT/refresh + ws-ticket), sqlite, bootstrap de admin y
root = repo git. Falta: WebSocket realtime, CRUD folders/projects, versionado git,
dashboard, cliente web-hub.

## Correr (dev)

```bash
cd external-backend
.venv/bin/python server.py            # o: .venv/bin/uvicorn server:app --port 8770
```

> ⚠️ **Siempre con el Python del venv.** Con `python3 server.py` (Python del sistema) podés
> pisar la trampa del paquete `jwt` equivocado (existe otro `jwt` en PyPI sin `jwt.encode`;
> daba `AttributeError: module 'jwt' has no attribute 'encode'` en el login). El server ahora
> corta al arrancar con un mensaje claro si detecta ese caso.

Al primer arranque crea el usuario **admin** con una password aleatoria (la imprime
en consola y la deja en `<HOME>/admin_password.txt`). El primer ingreso obliga a
cambiarla.

## Config (env)

| Env | Default | Qué |
|---|---|---|
| `DMC_HOME` | appdir del SO (mac: `~/Library/Application Support/DiagraMind-Connector`) | dir base (db, secreto, repo). **Nunca dentro del workspace**: si el estado vive en la carpeta del proyecto, un dev-server con live-reload (VSCode Live Server) recarga la página con cada escritura de la sqlite → loop de "F5 infinito" al conectar. Un `.dmc` legacy junto al script se migra solo. |
| `DMC_ROOT` | `<HOME>/repo` | root del conector = repo git |
| `DMC_HOST` | `127.0.0.1` | host |
| `DMC_PORT` | `8770` | puerto |

## Endpoints (pasos 1–5)

| Método | Ruta | Quién | Qué |
|---|---|---|---|
| GET | `/health` | público | detección + `auth:"jwt"` |
| POST | `/auth/login` | público | → access + refresh + `mustChangePassword` + role |
| POST | `/auth/refresh` | público | rota el refresh (revoca el viejo) + par nuevo |
| POST | `/auth/change-password` | sesión | set hash + bump `token_version` + par nuevo |
| POST | `/auth/ws-ticket` | sesión | ticket corto (un solo uso) para abrir el WS |
| GET/POST | `/users…` | admin | listar/crear + rol + `disabled` + ACL por carpeta |
| DELETE | `/users/{id}` | admin | borrar usuario |
| POST | `/folders` | admin | crear carpeta (id canónico) |
| GET | `/folders` | sesión | carpetas **visibles** (permiso != none) + permiso |
| POST | `/projects` | write | crear proyecto en una carpeta |
| GET | `/projects?folderId=` | read | listar proyectos de una carpeta |
| GET | `/projects/tree?id=` | read | lectura inicial del `tree.json` |
| POST | `/projects/delete` | creador/admin | borrar proyecto (§F) |
| POST | `/folders/delete` | creador/admin | borrar carpeta (+ sus proyectos) |
| GET | `/repo` | admin | árbol completo (carpetas + proyectos) para el dashboard |
| GET | `/versions/status?id=` | read | ¿cambios sin guardar? + HEAD + estado GitHub |
| POST | `/versions/commit` | write | **Guardar** (commit; auto-push si GitHub conectado) |
| GET | `/versions/log?id=` | read | historial del proyecto |
| GET | `/versions/diff?id=&a=&b=` | read | diff (sin `a`: HEAD vs working) |
| POST | `/versions/rollback` | write | restaura a un commit + difunde al room |
| GET/POST | `/github/*` | admin | connect / status / push / disconnect |
| WS | `/ws?ticket=` | sesión | mirror realtime: `open`/`edit`/`cursor`/`close` (§5) |
| GET | `/dashboard/` | admin (en la UI) | dashboard estático: usuarios/ACL + repo + versiones + GitHub |

El **dashboard** de administración está en `http://<host>:<port>/dashboard/` (`/` redirige ahí).

Auth: `Authorization: Bearer <access>`. Access corto (15 min), refresh largo (30 d)
con **rotación**; cambiar password o deshabilitar **invalida todo** (`token_version`).

**Permisos** (§3): ACL por carpeta `none|read|write` (default sin fila = `none` = ni ve la
carpeta). El permiso de un proyecto = el de su carpeta. `admin` → write en todo. El **mirror
en vivo** (crear/editar) va por el **WebSocket**, no por REST.

---

## ¿Olvidaste una contraseña?

| Caso | Solución |
|---|---|
| **Admin, nunca la cambiaste** | La inicial sigue en `<HOME>/admin_password.txt` (mac: `cat "$HOME/Library/Application Support/DiagraMind-Connector/admin_password.txt"`) |
| **Admin, la cambiaste y la olvidaste** | En la máquina del server: `.venv/bin/python server.py --reset-password` → imprime una nueva (y la deja en `admin_password.txt`). Funciona con el server prendido o apagado; invalida todas las sesiones y pide cambiarla al entrar. |
| **Un usuario normal** | Un admin, desde el **dashboard → Usuarios → "Reset pw"** → muestra la temporal una vez (o `POST /users/{id}/reset-password`). |

> El reset por CLI requiere **shell en la máquina del server** — ese es el modelo de
> seguridad: quien tiene acceso al disco ya es dueño del conector. No hay reset remoto
> sin sesión de admin.

## Deploy en un servidor (detrás de nginx)

El modelo recomendado: **nginx termina TLS** y proxyea al conector, que escucha **solo en
loopback** (default `DMC_HOST=127.0.0.1` — NO lo pongas en `0.0.0.0` si nginx corre en la
misma máquina). El cliente web deriva `wss://` solo: al agregar el conector con URL
`https://conector.midominio.com`, el WebSocket sale por `wss://…/ws` automáticamente.

### Config de referencia

```nginx
server {
    listen 80;
    server_name conector.midominio.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name conector.midominio.com;

    ssl_certificate     /etc/letsencrypt/live/conector.midominio.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/conector.midominio.com/privkey.pem;

    # los `edit` mandan el tree.json entero (LWW): dejar margen para diagramas grandes
    client_max_body_size 25m;

    # WebSocket del mirror (/ws): upgrade + timeouts largos (conexión de larga vida)
    location /ws {
        proxy_pass http://127.0.0.1:8770;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }

    # REST + dashboard (mismo origen)
    location / {
        proxy_pass http://127.0.0.1:8770;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### Observaciones

1. **El `location /ws` es obligatorio**: sin los headers `Upgrade`/`Connection` el handshake
   del WebSocket falla (la web conecta pero el mirror nunca arranca). Y sin
   `proxy_read_timeout` largo, nginx corta la conexión idle a los 60 s default.
2. **Si nginx corta el WS** (idle muy largo, reload de nginx, etc.) la web hoy **no
   reconecta sola**: marca el conector desconectado y hay que tocar "Connect" de nuevo
   (auto-reconexión del WS = mejora futura). Con actividad normal (cursores/presencia)
   la conexión no queda idle.
3. **Tokens**: viajan por HTTPS en headers/query (el ws-ticket va en la query del WS y es
   de un solo uso + 30 s de vida, así que no es sensible en logs). Igual conviene no
   loguear la query del `/ws` en el `access_log` si se quiere ser estricto.
4. **CORS**: el server responde `Access-Control-Allow-Origin: *` — correcto para este
   modelo (la auth es por token, no por cookie), no hace falta tocar nada en nginx.
5. **Servicio**: en el server conviene un unit de systemd:
   ```ini
   [Unit]
   Description=DiagraMind Connector
   After=network.target
   [Service]
   ExecStart=/opt/diagramind/external-backend/.venv/bin/python /opt/diagramind/external-backend/server.py
   Restart=on-failure
   User=diagramind
   [Install]
   WantedBy=multi-user.target
   ```
   El estado queda en el appdir del usuario del servicio (o fijalo con `DMC_HOME=/var/lib/diagramind`).
6. **HTTPS nativo sin nginx** (futuro): uvicorn soporta `--ssl-keyfile/--ssl-certfile`;
   cuando se quiera, es agregar esos args en `server.py` y listo.

### ¿Dónde guarda el estado? (appdir por SO)

| SO | Ruta |
|---|---|
| macOS | `~/Library/Application Support/DiagraMind-Connector/` (⚠️ `~/Library` está **oculta** en Finder: Cmd+Shift+G) |
| Windows | `%LOCALAPPDATA%\DiagraMind-Connector\` |
| Linux | `$XDG_DATA_HOME/DiagraMind-Connector/` (default `~/.local/share/…`) |

Adentro: `admin_password.txt` (password inicial del admin), `connector.db`, `secret.key`,
`github.json` (si conectaste GitHub) y `repo/` (el root git con las carpetas/proyectos).

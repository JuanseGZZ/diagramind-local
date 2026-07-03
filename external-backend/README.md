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

Al primer arranque crea el usuario **admin** con una password aleatoria (la imprime
en consola y la deja en `<HOME>/admin_password.txt`). El primer ingreso obliga a
cambiarla.

## Config (env)

| Env | Default | Qué |
|---|---|---|
| `DMC_HOME` | `./.dmc` | dir base (db, secreto, repo) |
| `DMC_ROOT` | `<HOME>/repo` | root del conector = repo git |
| `DMC_HOST` | `127.0.0.1` | host |
| `DMC_PORT` | `8770` | puerto |

## Endpoints (pasos 1–4)

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
| WS | `/ws?ticket=` | sesión | mirror realtime: `open`/`edit`/`cursor`/`close` (§5) |

Auth: `Authorization: Bearer <access>`. Access corto (15 min), refresh largo (30 d)
con **rotación**; cambiar password o deshabilitar **invalida todo** (`token_version`).

**Permisos** (§3): ACL por carpeta `none|read|write` (default sin fila = `none` = ni ve la
carpeta). El permiso de un proyecto = el de su carpeta. `admin` → write en todo. El **mirror
en vivo** (crear/editar) va por el **WebSocket**, no por REST.

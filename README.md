# DiagraMind Local — repo de los backends

Este repo **público** (`JuanseGZZ/diagramind-local`) tiene las **dos** piezas que
la app web necesita del lado de la máquina/servidor, cada una con su **versionado
y su CI independientes**:

| Pieza | Carpeta | Qué es | Versión en | Tag que compila |
|---|---|---|---|---|
| **Backend local** | `local-backend/` | el programita de escritorio (un usuario, su PC) | `server.py` → `VERSION` | `v*` (ej. `v0.32.0`) |
| **Conector externo** | `external-backend/` | server multiusuario (carpetas, proyectos, WS en vivo, MCP) | `config.py` → `VERSION` | `connector-v*` (ej. `connector-v0.18.6`) |

Los dos números **no** van juntos: el local puede ir en 0.32 y el conector en
0.18. Cada tag dispara **su** workflow y publica **sus** binarios.

> **Por qué separado.** La app web (repo `Diagramer`, privado) **ignora** esta
> carpeta (`diagramind-local/` en su `.gitignore`): acá adentro hay otro `.git`
> independiente — por eso los cambios de acá **no aparecen** en el `git status`
> de Diagramer. El versionado del backend va **en este repo**, no en Diagramer.
> Y tiene que ser **público** para que: (a) GitHub Actions compile gratis, y
> (b) los instaladores puedan bajar los binarios de los Releases.

## Estructura

```
.                                       ← raíz del repo (diagramind-local)
├── .github/workflows/
│   ├── release.yml                     ← CI del LOCAL      (tags v*)
│   └── release-connector.yml           ← CI del CONECTOR   (tags connector-v*)
├── local-backend/
│   ├── server.py                       ← el backend local (acá vive su VERSION)
│   ├── svgit.py, sourcever.py          ← ESPEJOS del conector (ver abajo)
│   ├── launchers/                      ← lanzadores del modo script
│   └── build_zip.sh, build_binary.sh
├── external-backend/
│   ├── server.py, config.py            ← el conector (su VERSION está en config.py)
│   ├── dashboard/                      ← panel del operador (estático)
│   └── tests/                          ← se corren con su .venv (ver más abajo)
└── descargas/
    ├── Instalar-DiagraMind-<os>            ← instaladores del local
    ├── Instalar-DiagraMind-Connector-<os>  ← instaladores del conector
    └── instalar-win.ps1
```

> **Archivos espejados**: `svgit.py` y `sourcever.py` existen **idénticos** en las
> dos carpetas (lógica pura, sin framework). Si tocás uno, **copiá el archivo al
> otro** — ya divergieron una vez (mensajes en español en uno y en inglés en el
> otro) y nadie lo notó hasta que un error salió sin traducir.

Los **binarios no se commitean**: los genera el CI y viven en los
[Releases](https://github.com/JuanseGZZ/diagramind-local/releases).

## Trabajar en otra máquina

```bash
git clone https://github.com/JuanseGZZ/diagramind-local.git
cd diagramind-local
```

Eso es todo: este repo es autocontenido. (Dentro de Diagramer aparece como
`diagramind-local/`, ignorada; podés laburar desde cualquiera de las dos.)

## Ciclo de desarrollo

1. **Editar** `local-backend/server.py`.
2. **Probar local** (sin compilar nada):
   ```bash
   python3 local-backend/server.py        # http://127.0.0.1:8765
   ```
   En la web → **IA → Conectar local**: si responde, estado *Conectado*.
3. **Subir la versión**: en `server.py`, subí `VERSION = "0.1.x"` (la web la
   muestra en *Conectado · diagramind-local vX*, así sabés que agarró la nueva).
4. **Commit + push** del código:
   ```bash
   git add -A
   git commit -m "backend: <qué cambió>"
   git push
   ```
5. **Generar los exe nuevos** (dispara el build): pushear un **tag**.
   ```bash
   git tag v0.1.1            # backend LOCAL
   git push origin v0.1.1

   git tag connector-v0.6.1  # CONECTOR externo
   git push origin connector-v0.6.1
   ```

   > En PowerShell corré los comandos **en líneas separadas** (`&&` no anda).
   >
   > El tag tiene que coincidir con la `VERSION` del código de esa pieza: es lo
   > que la web muestra en *Conectado · …* y lo único que permite saber qué
   > binario está corriendo alguien.

## Tests del conector

Corren **sin docker ni cluster** (levantan servers reales con HOME temporal y
puerto libre):

```bash
cd external-backend
.venv/bin/python tests/test_project_acl.py      # 24 · permisos por proyecto
.venv/bin/python tests/test_shared_types.py     # 15 · el compartido no aloja editor/orch
.venv/bin/python tests/test_acl_live.py         # 11 · revoke en vivo por WS
.venv/bin/python tests/test_editor_github.py    # 42 · versiones + GitHub del editor
.venv/bin/python tests/test_presence.py         #  5 · presencia por persona, no por socket
.venv/bin/python tests/pentest_connector.py     # 32 · ataques (JWT, IDOR, traversal, WS)
```

**Corrélos antes de taggear**: el tag publica binarios y un Release público.

## Qué pasa al pushear el tag

`git push origin v0.1.1` dispara
[`.github/workflows/release.yml`](.github/workflows/release.yml):

1. Compila con PyInstaller en Windows, macOS y Linux (Python 3.12).
2. Crea el Release `v0.1.1` y adjunta los 3 binarios + los 3 instaladores +
   `instalar-win.ps1` + `diagramind-local.zip`.

A los ~3 min, todo queda en
`https://github.com/JuanseGZZ/diagramind-local/releases/latest/download/<archivo>`.
Esa URL apunta siempre al Release más nuevo, así que **la web y los instaladores
no se tocan**: al sacar una versión nueva, empiezan a servir los binarios nuevos
solos.

> Sin tag, un `git push` normal **no** compila nada. El build lo dispara el tag.
> Para probar el build sin publicar: pestaña **Actions** → **Release** →
> **Run workflow** (`workflow_dispatch`), que compila sin crear Release.

Más detalle de compilación en [local-backend/COMPILAR.md](local-backend/COMPILAR.md).

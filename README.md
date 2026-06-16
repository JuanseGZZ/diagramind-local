# DiagraMind Local — repo del backend

Este es el **repo del backend local** de DiagraMind (`server.py` + instaladores +
CI). Es un repo **aparte y público**: `JuanseGZZ/diagramind-local`.

> **Por qué separado.** La app web (repo `Diagramer`, privado) **ignora** esta
> carpeta (`externos/` en su `.gitignore`): acá adentro hay otro `.git`
> independiente. El versionado del backend va **en este repo**, no en Diagramer.
> Y tiene que ser **público** para que: (a) GitHub Actions compile gratis, y
> (b) los instaladores puedan bajar los binarios de los Releases.

## Estructura

```
.                                  ← raíz del repo (diagramind-local)
├── .github/workflows/release.yml  ← CI: compila los 3 binarios y publica el Release
├── local-backend/
│   ├── server.py        ← el backend (lo que vas a editar)
│   ├── launchers/       ← lanzadores del modo script
│   └── build_zip.sh, build_binary.sh
└── descargas/
    ├── Instalar-DiagraMind-<os>   ← instaladores (bajan el binario + auto-inicio)
    └── instalar-win.ps1
```

Los **binarios no se commitean**: los genera el CI y viven en los
[Releases](https://github.com/JuanseGZZ/diagramind-local/releases).

## Trabajar en otra máquina

```bash
git clone https://github.com/JuanseGZZ/diagramind-local.git
cd diagramind-local
```

Eso es todo: este repo es autocontenido. (En Diagramer esta carpeta aparece como
`externos/`, ignorada; podés laburar desde cualquiera de las dos.)

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
5. **Generar los exe nuevos** (dispara el build): pushear un **tag** `v*`.
   ```bash
   git tag v0.1.1
   git push origin v0.1.1
   ```

   > En PowerShell corré los comandos **en líneas separadas** (`&&` no anda).

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

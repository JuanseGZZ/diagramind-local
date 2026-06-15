# DiagraMind — Backend local

App de Python que corre en tu máquina y hace de **backend local** para la web de
DiagraMind. La web lo detecta con el botón **IA → Conectar local**.

> Estado: paso 1. Por ahora solo expone `/health` para que la web confirme que
> está vivo. Más adelante va a exponer endpoints para chatear con Claude Code,
> generar soft e inyectarlo en la web.

## Dos formas de usarlo

La web (botón **IA → Descargar local**) ofrece dos descargas:

1. **Ejecutable (sin Python)** — binario standalone. Se baja, se abre con doble
   clic y listo. No requiere Python. Generado con PyInstaller.
2. **Script .py (requiere Python)** — `diagramind-local.zip` con `server.py` +
   lanzadores (`iniciar.command` / `iniciar.bat`). Descarga mínima, pero
   necesita Python 3 instalado.

Los archivos se sirven desde `descargas/` del repo.

## Correr (modo desarrollo / script)

```bash
cd local-backend
python3 server.py            # http://127.0.0.1:8765
python3 server.py --port 9000
```

Dejalo corriendo en una terminal. Después, en la web, abrí **IA → Conectar
local**: si el server está vivo, el estado pasa a *Conectado*.

## Empaquetar

Los binarios (Win/Mac/Linux) y el `.zip` los compila y publica el **CI** como
assets de un **GitHub Release**, al pushear un tag `v*`. No se commitean.

```bash
git tag v0.1.1 && git push origin v0.1.1   # dispara el workflow Release
```

Detalle en [COMPILAR.md](COMPILAR.md). Para debug local podés generar el binario
del SO actual con `pip install pyinstaller && bash build_binary.sh` (queda en
`descargas/`, gitignored).

## Endpoints

| Método | Ruta      | Respuesta |
|--------|-----------|-----------|
| GET    | `/health` | `{ "status": "ok", "name": "diagramind-local", "version": "0.1.0" }` |

## Seguridad

- Escucha **solo en `127.0.0.1`** (loopback): no es accesible desde la red.
- CORS abierto (`*`) para que la web pueda consultarlo desde cualquier origen
  local (incluido `file://`).

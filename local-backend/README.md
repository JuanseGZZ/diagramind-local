# DiagraMind — Backend local (server.py)

App de Python que corre en tu máquina y hace de **backend local** para la web de
DiagraMind. La web lo detecta con el botón **IA → Conectar local**.

> Estado: paso 1. Por ahora solo expone `/health` para que la web confirme que
> está vivo. Más adelante va a exponer endpoints para chatear con Claude Code,
> generar soft e inyectarlo en la web.

> El **ciclo de desarrollo y cómo publicar** (cambio → push → build) está en el
> [README de la raíz](../README.md). Acá van los detalles del server.

## Tres formas de usarlo (las baja la web)

La web (**IA → Descargar local**) ofrece, todas desde los **Releases** del repo:

1. **Ejecutable** (sin Python) — binario standalone (PyInstaller). Doble clic.
2. **Instalador** — baja el ejecutable y lo deja arrancando solo (auto-inicio).
3. **Script .py** — `diagramind-local.zip` con `server.py` + lanzadores; liviano
   pero requiere Python 3.

## Correr local (para probar mientras desarrollás)

```bash
python3 server.py            # http://127.0.0.1:8765
python3 server.py --port 9000
```

Dejalo corriendo en una terminal. Después, en la web, **IA → Conectar local**: si
el server está vivo, el estado pasa a *Conectado*. Al cambiar algo, subí
`VERSION` (la web la muestra) para confirmar que agarró la versión nueva.

## Compilar local (opcional, debug)

```bash
pip install pyinstaller
bash build_binary.sh   # binario del SO actual → descargas/ (gitignored)
```

Los binarios oficiales los hace el CI; ver [COMPILAR.md](COMPILAR.md).

## Endpoints

| Método | Ruta      | Respuesta |
|--------|-----------|-----------|
| GET    | `/health` | `{ "status": "ok", "name": "diagramind-local", "version": "0.1.0" }` |

## Seguridad

- Escucha **solo en `127.0.0.1`** (loopback): no es accesible desde la red.
- CORS abierto (`*`) para que la web pueda consultarlo desde cualquier origen
  local (incluido `file://`).

# Cómo se generan los ejecutables (CI → GitHub Releases)

Los binarios standalone (Windows / macOS / Linux) los compila **GitHub Actions**
y los publica como **assets de un Release**. No se compilan ni se commitean a
mano. El repo es `JuanseGZZ/diagramind-local` (público).

## Sacar una versión nueva

```bash
# Parado en externos/ (el repo interno):
git add -A
git commit -m "backend: <lo que cambió>"
git push

git tag v0.1.1          # subí el número en cada release
git push origin v0.1.1  # esto dispara el workflow Release
```

El workflow [.github/workflows/release.yml](../.github/workflows/release.yml):

1. Compila con PyInstaller `--onefile` en `windows-latest`, `macos-latest` y
   `ubuntu-latest` (Python 3.12).
2. Genera el `.zip` (versión script) con `build_zip.sh`.
3. Crea el Release del tag y adjunta como assets: los 3 binarios, los 3
   instaladores, `instalar-win.ps1` y `diagramind-local.zip`.

A los ~2 min, todo queda en:
`https://github.com/JuanseGZZ/diagramind-local/releases/latest/download/<archivo>`

Esa URL `releases/latest/download/` siempre apunta al Release más nuevo, así que
los instaladores y la web **no hay que tocarlos** al sacar versiones.

## Probar el build sin publicar

En la pestaña **Actions** del repo → workflow **Release** → **Run workflow**
(`workflow_dispatch`). Compila los 3 binarios y los deja como *artifacts*
descargables, **sin** crear Release (solo los tags publican).

## Compilar local (opcional, para debug)

```bash
pip install pyinstaller
bash local-backend/build_binary.sh   # deja el binario del SO actual en descargas/ (gitignored)
```

## Notas

- **macOS es arm64** (el runner `macos-latest` es Apple Silicon). Intel
  necesitaría un build aparte.
- Los binarios **no están firmados**: Windows puede mostrar "Windows protegió tu
  PC" → "Más información" → "Ejecutar de todas formas"; macOS → clic derecho →
  Abrir. Firmar/notarizar es un paso posterior (requiere certificados de pago).

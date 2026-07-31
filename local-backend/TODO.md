# TODO — Backend local & agentes IA

Estado y pendientes de la integración IA (API agents + CLIs locales). Se va
tachando a medida que se prueba/implementa.

## Estado de los backends

| Backend | Cableado | Verificado de verdad |
|---|---|---|
| Local · Claude Code | ✅ | ✅ edita, colores, `--resume` |
| Google API (Gemini) | ✅ | ✅ (diagrama de café con colores) |
| Local · Gemini CLI | ✅ | 🟡 editó pero murió por cuota free; `-m` sin confirmar |
| Local · Codex | ✅ | ⬜ sin instalar |
| Anthropic API | ✅ | ⬜ key sin créditos |
| OpenAI API | ✅ | ⬜ sin key |
| Otra (URL custom) | ✅ | ⬜ sin endpoint |

## Pendientes (features)

- [ ] **Memoria conversacional para Codex y Gemini CLI** (gap más importante).
      Hoy van one-shot; solo Claude tiene `--resume`. Plan: que el FRONT reinyecte
      el transcript en el prompt cada turno (es dueño de la conversación). Así:
      (a) Codex/Gemini recuerdan turnos anteriores como Claude, y (b) el **swap por
      tokens** queda sin costura (el CLI nuevo arranca con toda la charla, no solo
      con el `tree.json`).
- [ ] **Verificar el `-m` de Gemini CLI**: corrió `gemini-3.5-flash` en vez del
      elegido. Reordené `-m` antes de `-p`; falta confirmar con cuota.
- [ ] **Eventos finos para Codex/Gemini**: hoy "Trabajando…" + texto final. El
      Gemini CLI tiene `--output-format stream-json` → daría eventos como Claude
      (assistant/tool) y texto limpio sin el filtrado de ruido actual.
- [ ] **Confirmar el thinking de Claude Code**: el esfuerzo se mapea a palabras
      (`think`/`ultrathink`); falta verificar que el CLI las respeta en modo `-p`.
- [ ] **Auto-fallback** (v2): al recibir rate-limit/quota, reintentar solo con el
      siguiente CLI disponible, sin que el usuario tenga que swapear a mano.
- [ ] **Catálogos de modelos locales** de Codex (`gpt-5-codex`…) son tentativos —
      verificar contra los reales cuando se instale Codex.
- [ ] Probar el round-trip real de **Anthropic / OpenAI / Otra** (faltan
      créditos / key / endpoint).

## Panel de control (v0.31.0)

- [x] Ventana propia al arrancar (`--app` de Chrome/Edge, fallback pestaña) + `--no-ui`
      en los auto-inicios; segundo doble clic = abrir el panel de la instancia viva.
- [x] Los 3 CLIs con estado (verde/versión o "no instalado") + **Instalar** con el
      instalador NATIVO de cada uno (sin Node) y la salida en vivo por SSE:
      `claude.ai/install.sh`, `chatgpt.com/codex/install.sh` (`CODEX_NON_INTERACTIVE`),
      y para Gemini el binario del release de GitHub + `codesign` ad-hoc (macOS).
- [x] Contraseña (ver/copiar/regenerar), proyectos (raíz + árbol del disco), pasos para
      conectar la web. **Cerrar la ventana apaga el backend** (SSE `/panel/alive`).
- [x] `_extra_bin_dirs()`: detectar CLIs instalados con nvm/fnm/volta/`npm prefix -g`.
- [ ] **Probar una instalación real desde el botón** (no se probó: no quisimos instalar
      CLIs en la máquina). Falta confirmar sobre todo el camino de Codex headless.
- [ ] Probar el panel en **Windows y Linux**: rutas de Chrome/Edge, los `install.ps1`
      por PowerShell, y el acceso directo con `--no-ui`. Verificado solo en macOS.
- [ ] **Gemini en Linux/Windows** no tiene binario oficial: hoy cae a npm (si existe) o
      a mostrar el comando. Revisar si Google publica binarios para esos SO.
- [ ] Traducir el panel (hoy es solo español; la web tiene i18n, doc 22).
- [ ] Agregar al panel la lista de **runs** en curso (con Cancelar) — quedó afuera de v1.

## Hecho (referencia)

- [x] Patrón adaptador-por-CLI desacoplado (claude/codex/gemini.py + cli_base + clis).
- [x] `AGENTS.md` compartido (mismo esquema que las skills de Claude).
- [x] Gemini CLI: trust (`GEMINI_CLI_TRUST_WORKSPACE`) + filtro de ruido + prompt
      imperativo (`_headless_prompt`) + manejo de cuota agotada.
- [x] `/health` reporta los CLIs; la web muestra los 3 locales (filtra al conectar).
- [x] Esfuerzo solo en Claude (mapeado a thinking); oculto en Gemini/Codex.
- [x] SSE robusto ante desconexión del cliente (ConnectionError/OSError).

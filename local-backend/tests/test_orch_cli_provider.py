"""Qué CLI acepta el ORQUESTADOR en un nodo agente, y qué hace con los que no ejecuta.

El bug (2026-09-14): el selector del nodo agente ofrecía tres CLIs de los cuatro que la
app maneja —faltaba Antigravity (`agy`)—, y los dos que no son Claude estaban ROTOS de
una forma imposible de diagnosticar: el motor corre `claude` para cualquier provider de
`CLI_PROVIDERS`, y `map_model()` deja pasar tal cual un id que no sea opus/haiku/sonnet.
O sea que un nodo "Codex" lanzaba `claude --model gpt-5-codex` y el binario contestaba
`unrecognized_model` (verificado contra claude 2.1.263). El run moría con "There's an
issue with the selected model", que no le dice a nadie que el problema es el CLI elegido.

Este test NO llama a ninguna API ni lanza ningún CLI: corre sin red.

    python3 diagramind-local/local-backend/tests/test_orch_cli_provider.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator                                        # noqa: E402
from orchestrator import CLI_PROVIDERS, CLI_ENGINE_OK, OrchError, _run_cli_turn   # noqa: E402
from claude import map_model                               # noqa: E402
from clis import CLIS                                      # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def nodo(provider, model=None):
    return {"id": "n1", "titulo": "Contador",
            "data": {"ia": {"provider": provider, "model": model}}}


print("\n=== los CUATRO CLIs de la app entran por la rama CLI ===")
# Si un provider CLI no está acá, el motor lo manda a la rama API y termina buscando una
# credencial que no existe: el error habla de keys y no del CLI. Por eso están los cuatro.
for p in ("local", "local-codex", "local-gemini", "local-antigravity"):
    check(f"«{p}» es un provider CLI para el motor", p in CLI_PROVIDERS)

# el set del motor y el registro de adaptadores del chat cuentan los MISMOS CLIs
check("hay un CLI del motor por cada adaptador del chat (más no, menos tampoco)",
      len(CLI_PROVIDERS) == len(CLIS), f"{sorted(CLI_PROVIDERS)} vs {sorted(CLIS)}")
check("y Antigravity está en los dos lados",
      "local-antigravity" in CLI_PROVIDERS and "antigravity" in CLIS)

print("\n=== pero el motor EJECUTA solo Claude Code ===")
check("CLI_ENGINE_OK es únicamente «local»", CLI_ENGINE_OK == {"local"}, str(CLI_ENGINE_OK))

print("\n=== …y lo dice, en vez de morir con «unrecognized_model» ===")
# El chequeo va ANTES de find_claude(), así que este test no depende de que el binario
# esté instalado ni lanza ningún proceso.
for p, cli in (("local-codex", "codex"), ("local-gemini", "gemini"),
               ("local-antigravity", "antigravity")):
    try:
        _run_cli_turn(None, None, None, nodo(p), None, "hola")
        check(f"«{p}» es rechazado antes de lanzar nada", False, "no levantó OrchError")
    except OrchError as e:
        msg = str(e.detail if hasattr(e, "detail") else e)
        check(f"«{p}» es rechazado antes de lanzar nada", True)
        check(f"  …el error NOMBRA el provider elegido ({p})", p in msg, msg)
        check("  …y dice con qué SÍ corre (Claude Code)", "Claude Code" in msg, msg)
        check("  …y que en el chat sí andan", "chat" in msg.lower(), msg)
    except Exception as e:                       # ni TypeError por los args en None
        check(f"«{p}» es rechazado antes de lanzar nada", False, f"{type(e).__name__}: {e}")

print("\n=== el porqué del bug viejo: map_model deja pasar ids ajenos ===")
# Esto NO se arregló (map_model es de Claude y está bien así): lo que se arregló es que
# un id ajeno ya no llegue hasta el binario. Si algún día map_model normalizara, este
# check falla y hay que revisar si el chequeo de provider sigue haciendo falta.
check("un id de Codex pasaría tal cual a `claude --model`", map_model("gpt-5-codex") == "gpt-5-codex")
check("y uno de Antigravity también",
      map_model("gemini-3.7-flash-medium") == "gemini-3.7-flash-medium")
check("(los de Claude sí se mapean a su alias)", map_model("claude-sonnet-5") == "sonnet")

print(f"\n{'✅' if fail == 0 else '❌'} {ok}/{ok + fail}")
sys.exit(0 if fail == 0 else 1)

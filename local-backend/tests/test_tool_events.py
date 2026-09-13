"""Lo que la web ve MIENTRAS el turno corre (bitácora §68).

EL BUG QUE LO MOTIVA: el chat mostraba "Sigue trabajando… 279s" y nada más. Un turno que
pasa minutos corriendo herramientas sin escribir una línea era indistinguible de un
cuelgue — el usuario no sabía si se había bugeado. La razón: de cada `tool_use` el
backend emitía SOLO el nombre de la tool y tiraba el `input`, y del resultado no emitía
nada.

Las formas de los eventos NO están copiadas de la documentación: salen de una corrida
REAL de `claude -p --output-format stream-json --verbose` (CLI 2.x) capturada a mano.
Los fixtures de abajo son esas líneas, recortadas. Es la misma regla que se siguió con
el contrato del puente de permisos (§66).

    python3 diagramind-local/local-backend/tests/test_tool_events.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runs                                             # noqa: E402
from claude import handle_event, tool_detail            # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name} {extra}")


def eventos(run, kind):
    return [e for e in run["events"] if e["kind"] == kind]


# ---- líneas REALES de una corrida de la CLI (recortadas) ----
LEE = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "toolu_013ZPmo7QkoN8Ea4f98JyCxD", "name": "Read",
     "input": {"file_path": "C:\\Users\\juans\\scratchpad\\pruebacli\\datos.txt"}}]}}
LEE_OK = {"type": "user", "message": {"content": [
    {"tool_use_id": "toolu_013ZPmo7QkoN8Ea4f98JyCxD", "type": "tool_result",
     "content": "1\thola mundo\n2\t"}]}}
BASH = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "toolu_01RR6FrRxvEyRSdZXKYsjD9P", "name": "Bash",
     "input": {"command": "wc -l \"datos.txt\"", "description": "Count lines in datos.txt"}}]}}
BASH_MAL = {"type": "user", "message": {"content": [
    {"tool_use_id": "toolu_01RR6FrRxvEyRSdZXKYsjD9P", "type": "tool_result",
     "content": "wc: datos.txt: No such file or directory", "is_error": True}]}}
PIENSA = {"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "The user wants me to read the file first."}]}}
DICE = {"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Listo, tiene 1 línea."}]}}


print("\n### A. cada herramienta cuenta QUÉ va a hacer, no solo su nombre")
run = runs.new_run()
handle_event(run, LEE)
handle_event(run, BASH)
tools = eventos(run, "tool")
check("sale un evento por herramienta", len(tools) == 2, str(tools))
check("con el nombre", [t["name"] for t in tools] == ["Read", "Bash"], str(tools))
check("Read dice QUÉ archivo lee", tools[0]["detail"].endswith("datos.txt"), tools[0]["detail"])
check("Bash dice EL COMANDO (no 'Running commands…')",
      tools[1]["detail"] == 'wc -l "datos.txt"', tools[1]["detail"])
check("y viaja el id para poder parear el resultado",
      tools[0]["useId"] == "toolu_013ZPmo7QkoN8Ea4f98JyCxD", str(tools[0]))

print("\n### B. y cuando termina, se sabe si salió bien o mal")
handle_event(run, LEE_OK)
handle_event(run, BASH_MAL)
fin = eventos(run, "tool-done")
check("sale un evento por resultado", len(fin) == 2, str(fin))
check("pareado por el id del tool_use",
      [f["useId"] for f in fin] == [t["useId"] for t in tools], str(fin))
check("sin is_error (que es como viene el caso bueno) cuenta como OK", fin[0]["ok"] is True, str(fin[0]))
check("y el que falló cuenta como error", fin[1]["ok"] is False, str(fin[1]))
check("con el motivo, para poder mirarlo", "No such file" in fin[1]["error"], str(fin[1]))
check("el caso bueno no arrastra el output entero", fin[0]["error"] == "", str(fin[0]))

print("\n### C. lo que NO tiene que cambiar")
run = runs.new_run()
handle_event(run, PIENSA)
check("el razonamiento no se publica en el chat", run["events"] == [], str(run["events"]))
handle_event(run, DICE)
check("el texto del asistente sí", [e["kind"] for e in run["events"]] == ["assistant"])
check("y con su contenido", run["events"][0]["text"] == "Listo, tiene 1 línea.")

print("\n### D. el detalle: lo importante primero y acotado")
check("el comando le gana a la descripción que escribe el modelo",
      tool_detail({"description": "Count lines", "command": "wc -l x"}) == "wc -l x")
check("si no hay comando, sirve el archivo",
      tool_detail({"file_path": "/tmp/x.md"}) == "/tmp/x.md")
check("una URL también", tool_detail({"url": "https://x.com"}) == "https://x.com")
check("y como último recurso, la descripción del modelo",
      tool_detail({"description": "Unpack the notes"}) == "Unpack the notes")
largo = tool_detail({"command": "x" * 900})
check("un comando gigante se recorta (no se manda un chorizo a la UI)",
      len(largo) <= 301 and largo.endswith("…"), str(len(largo)))
check("una tool sin nada legible no rompe", tool_detail({"raro": 3}) == "")
check("ni una sin input", tool_detail(None) == "")

print(f"\n=== RESULTADO: {ok} ok, {fail} fallidos ===")
sys.exit(1 if fail else 0)

"""Conocimiento del dominio (formato de los diagramas) para los CLIs locales.
- Claude Code lo lee de <carpeta>/.claude/skills (una skill por archivo).
- Codex y Gemini CLI leen un único AGENTS.md / GEMINI.md armado con el MISMO
  contenido (estándar compartido).
Embebido acá para que el binario --onefile lo tenga sin archivos sueltos."""
import os

SYSTEM_PREAMBLE = (
    "Estás trabajando en un WORKSPACE de DiagraMind que contiene VARIOS proyectos. "
    "Tu directorio de trabajo es la carpeta de proyectos: ./index.json lista TODOS "
    "los proyectos [{id, name, type}] y cuál es el foco (focusedId). Cada proyecto "
    "vive en ./<id>/tree.json. Las skills (diagramind-format y la de cada tipo) "
    "están en ./.claude/skills; leelas antes de editar.\n\n"
    "El PROYECTO FOCO es tu objetivo de ESCRITURA por defecto: editá "
    "./<focusedId>/tree.json IN-PLACE, JSON válido, respetando EXACTAMENTE el "
    "esquema de SU tipo (cada proyecto puede ser de un tipo distinto: "
    "cart/freestyle/activities). No cambies el id del árbol. Al terminar, releé el "
    "archivo y verificá que es JSON válido y cumple el esquema (sin campos de más "
    "ni de menos, ids únicos); si algo está mal, corregilo.\n\n"
    "Podés LEER cualquier otro proyecto (./<id>/tree.json, buscalo por nombre en "
    "./index.json) para basarte en él; escribí en otro proyecto SOLO si el usuario "
    "te lo pide explícitamente.\n\n"
    "IMPORTANTE: es UNA conversación continua. Recordás lo que hiciste en turnos "
    "anteriores aunque el usuario cambie el proyecto foco. Si el usuario se refiere "
    "a 'eso' o 'lo que agregaste', mirá el historial de la conversación.\n\n"
    "Respondé en español, breve."
)


def _skill(name, description, body):
    return name, ("---\nname: %s\ndescription: %s\n---\n\n%s\n" %
                  (name, description, body))


SKILLS = dict([
    _skill(
        "diagramind-format",
        "Formato de un proyecto DiagraMind: tree.json, tipos, ids y contadores. "
        "Leer SIEMPRE antes de editar un diagrama.",
        "# Formato DiagraMind\n\n"
        "Un proyecto es un único archivo `tree.json` (el mismo objeto que produce "
        "`tree.toJson()` en la web). El campo raíz `type` define la estructura:\n\n"
        "- `cart` → ver `diagramind-cart`\n"
        "- `freestyle` → ver `diagramind-freestyle`\n"
        "- `activities` → ver `diagramind-activities`\n\n"
        "Común a todos:\n"
        "- `attachments`: mapa `{ \"<aid>\": { \"name\", \"mime\" } }` (adjuntos; "
        "  los bytes van aparte, NO los toques).\n"
        "- Campos `lastIdCharged` / `lastId` / `lastArrowId` / etc. son "
        "  **contadores** del último id usado.\n\n"
        "## Reglas (importantes)\n"
        "1. Editá `tree.json` IN-PLACE y dejalo como **JSON válido** (verificá que "
        "   parsea al terminar).\n"
        "2. **Respetá EXACTAMENTE los nombres de campo** del esquema del tipo. No "
        "   inventes ni renombres campos.\n"
        "3. **Los ids son números enteros.** Al agregar un nodo, usá "
        "   `<contador> + 1`, asignalo como id del nodo nuevo y **actualizá el "
        "   contador** a ese valor.\n"
        "4. No cambies el `type` ni mezcles nodos de otro tipo.\n"
        "5. Conservá los campos existentes de cada nodo (no los borres al editar).\n"
        "6. **Colores**: el campo `color` (en nodos, flechas y cartas) acepta un "
        "   **string hex** como `\"#e53935\"`, o `null` = color por defecto. SÍ se "
        "   pueden cambiar: poné el hex en `color`. En freestyle las formas usan "
        "   `fill`/`stroke` (también hex).",
    ),
    _skill(
        "diagramind-cart",
        "Árbol jerárquico de cartas (tipo `cart`, layouts ltr/organigram).",
        "# Tipo cart (jerárquico)\n\n"
        "Árbol de cartas multinivel. Esquema EXACTO de `tree.json`:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"cart\",\n"
        "  \"lastIdCharged\": 3,\n"
        "  \"attachments\": {},\n"
        "  \"nodoRaiz\": {\n"
        "    \"idCarta\": 0,\n"
        "    \"idPadre\": null,\n"
        "    \"tituloCarta\": \"Raíz\",\n"
        "    \"descripcion\": \"texto del cuerpo\",\n"
        "    \"color\": null,\n"
        "    \"shape\": \"default\",\n"
        "    \"collapsed\": false,\n"
        "    \"listaHijos\": [ /* cartas con la MISMA forma */ ]\n"
        "  }\n"
        "}\n"
        "```\n\n"
        "## Campos por carta\n"
        "- `idCarta` (int, único), `idPadre` (int del padre, o null en la raíz).\n"
        "- `tituloCarta` (str), `descripcion` (str, cuerpo de texto).\n"
        "- `color` (str|null), `shape` (\"default\"), `collapsed` (bool).\n"
        "- `listaHijos` (array de cartas).\n\n"
        "## Editar\n"
        "- **Agregar hijo**: crear una carta con `idCarta = lastIdCharged + 1` y "
        "  `idPadre = idCarta del padre`; pushearla a `listaHijos` del padre; "
        "  subir `lastIdCharged`.\n"
        "- **Mover**: sacar la carta de un `listaHijos` y ponerla en otro; "
        "  actualizar su `idPadre`.\n"
        "- **Borrar**: quitar la carta (con su subárbol) de `listaHijos`.\n"
        "- OJO: es `nodoRaiz`/`listaHijos`/`idCarta`/`tituloCarta` (NO raiz/hijos/id/titulo).",
    ),
    _skill(
        "diagramind-freestyle",
        "Canvas libre (tipo `freestyle`): nodos con x/y, flechas y formas.",
        "# Tipo freestyle (canvas libre)\n\n"
        "Plano, sin layout automático. Esquema EXACTO:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"freestyle\",\n"
        "  \"lastIdCharged\": 2, \"lastArrowId\": 1, \"lastShapeId\": 0,\n"
        "  \"attachments\": {},\n"
        "  \"nodos\":  [{ \"id\":1, \"x\":100, \"y\":80, \"ancho\":160, \"alto\":90,\n"
        "             \"titulo\":\"\", \"contenido\":\"\", \"color\":null,\n"
        "             \"type\":\"basic\", \"data\":{} }],\n"
        "  \"flechas\":[{ \"id\":1, \"fromId\":1, \"toId\":2,\n"
        "             \"fromSide\":\"right\", \"toSide\":\"left\", \"label\":\"\", \"color\":null }],\n"
        "  \"formas\": [{ \"id\":1, \"x\":0,\"y\":0,\"ancho\":120,\"alto\":120,\n"
        "             \"rotation\":0, \"shape\":\"rect\", \"fill\":\"#fff\", \"stroke\":\"#000\",\n"
        "             \"strokeWidth\":2, \"label\":\"\", \"imageSrc\":\"\",\n"
        "             \"imgPosX\":50, \"imgPosY\":50, \"imgZoom\":1 }]\n"
        "}\n"
        "```\n\n"
        "## Editar\n"
        "- **Agregar nodo**: id `lastIdCharged + 1`; x/y/ancho/alto numéricos; subir "
        "  `lastIdCharged`.\n"
        "- **Conectar**: nueva flecha en `flechas` con `fromId`/`toId` de nodos "
        "  existentes; `fromSide`/`toSide` ∈ left/right/top/bottom; subir `lastArrowId`.\n"
        "- **Editar nodo**: cambiá `titulo`/`contenido`/`color` (hex) o `x`/`y`/`ancho`/`alto`.\n"
        "- **Forma**: nueva en `formas`; subir `lastShapeId` (`fill`/`stroke` son hex).\n"
        "- **Borrar nodo**: quitalo de `nodos` Y borrá las flechas que lo referencian.\n"
        "- No dupliques ids dentro de cada lista.",
    ),
    _skill(
        "diagramind-activities",
        "Diagrama de actividades (tipo `activities`): precedencias / Gantt.",
        "# Tipo activities\n\n"
        "Actividades con precedencias dirigidas. Esquema EXACTO:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"activities\",\n"
        "  \"lastId\": 3, \"seqCounter\": 3, \"timeUnit\": \"dias\",\n"
        "  \"attachments\": {},\n"
        "  \"nodes\": [{ \"id\":1, \"titulo\":\"Tarea\", \"contenido\":\"\",\n"
        "             \"color\":null, \"isStart\":true, \"seq\":1, \"duracion\":2 }],\n"
        "  \"edges\": [{ \"fromId\":1, \"toId\":2, \"color\":null }]\n"
        "}\n"
        "```\n\n"
        "## Campos\n"
        "- nodo: `id` (int), `titulo`, `contenido`, `color`, `isStart` (bool), "
        "  `seq` (orden), `duracion` (en `timeUnit`: horas/dias/semanas).\n"
        "- `edges`: precedencias dirigidas `fromId → toId`.\n\n"
        "## Editar\n"
        "- **Agregar actividad**: id `lastId + 1`, `seq = seqCounter + 1`; subir "
        "  ambos contadores.\n"
        "- **Precedencia**: nuevo `edge` con ids existentes. **No crees ciclos.**\n"
        "- **Editar**: cambiá `titulo`/`contenido`/`color` (hex)/`duracion`; `isStart` "
        "  marca el nodo de arranque.\n"
        "- **Borrar actividad**: quitala de `nodes` Y borrá los `edges` que la referencian.\n"
        "- Mantené `timeUnit` coherente (horas/dias/semanas).",
    ),
])


def install_skills(project_dir):
    """Claude Code: una skill por carpeta en <project_dir>/.claude/skills/<name>/SKILL.md."""
    skills_dir = os.path.join(project_dir, ".claude", "skills")
    for name, content in SKILLS.items():
        d = os.path.join(skills_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(content)


def _agents_md():
    """Mismo conocimiento que las skills de Claude, pero en un solo documento que
    leen Codex y Gemini CLI (estándar compartido). Se arma de SKILLS + el preámbulo."""
    parts = ["# DiagraMind — instrucciones para el agente\n", SYSTEM_PREAMBLE, "\n"]
    for _name, content in SKILLS.items():
        body = content.split("---\n", 2)[-1]   # saco el frontmatter YAML
        parts.append("\n---\n\n" + body.strip() + "\n")
    return "\n".join(parts)


def install_agents_md(work_dir):
    """Codex lee AGENTS.md; Gemini CLI lee GEMINI.md (y también AGENTS.md). Escribimos
    ambos en la carpeta (cwd del agente) con el esquema de los diagramas."""
    os.makedirs(work_dir, exist_ok=True)
    md = _agents_md()
    for fname in ("AGENTS.md", "GEMINI.md"):
        with open(os.path.join(work_dir, fname), "w", encoding="utf-8") as f:
            f.write(md)

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
    "cart/freestyle/treeQuestionary/activities/object/editor). No cambies el id del árbol. Al terminar, releé el "
    "archivo y verificá que es JSON válido y cumple el esquema (sin campos de más "
    "ni de menos, ids únicos); si algo está mal, corregilo.\n\n"
    "EXCEPCIÓN — proyectos tipo `editor`: NO son diagramas; su tree.json es un "
    "puntero {type, target} a una CARPETA REAL (ver diagramind-editor). Si el foco "
    "es un editor, trabajá directo sobre esa carpeta y NO toques su tree.json.\n\n"
    "Podés LEER cualquier otro proyecto (./<id>/tree.json, buscalo por nombre en "
    "./index.json) para basarte en él; escribí en otro proyecto SOLO si el usuario "
    "te lo pide explícitamente.\n\n"
    "EJECUTAR FETCHES (proyectos tipo object): para 'correr/probar' un nodo Fetch o "
    "Ráfaga del diagrama NO uses WebFetch ni curl/Bash (están deshabilitadas a propósito): "
    "editá el `tree.json` poniendo un `runReq` nuevo en el `data` del nodo, guardá y terminá "
    "tu turno; la app lo ejecuta y te devuelve el resultado en el próximo turno. Ver la "
    "skill diagramind-object.\n\n"
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
        "- `treeQuestionary` → ver `diagramind-treequestionary`\n"
        "- `activities` → ver `diagramind-activities`\n"
        "- `object` → ver `diagramind-object`\n"
        "- `editor` → ver `diagramind-editor` (NO es un diagrama: apunta a una carpeta real)\n\n"
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
        "diagramind-treequestionary",
        "Cuestionario de estudio (tipo `treeQuestionary`): mismo canvas que freestyle "
        "pero con nodos de estudio (qa/mc/test/asking) en `type` + `data`.",
        "# Tipo treeQuestionary (cuestionario de estudio)\n\n"
        "Mismo canvas plano que `freestyle` (nodos con x/y + flechas), pero los nodos "
        "son de estudio: lo propio de cada uno va en `type` y en el objeto `data`. "
        "Esquema EXACTO:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"treeQuestionary\",\n"
        "  \"lastIdCharged\": 4, \"lastArrowId\": 0, \"lastShapeId\": 0,\n"
        "  \"attachments\": {},\n"
        "  \"nodos\": [\n"
        "    { \"id\":1, \"x\":80, \"y\":80, \"ancho\":250, \"alto\":150,\n"
        "      \"titulo\":\"\", \"contenido\":\"\", \"color\":null,\n"
        "      \"type\":\"qa\", \"data\":{ \"pregunta\":\"¿Capital de Francia?\", \"respuesta\":\"París\" } },\n"
        "    { \"id\":2, \"x\":360, \"y\":80, \"ancho\":270, \"alto\":200,\n"
        "      \"titulo\":\"\", \"contenido\":\"\", \"color\":null,\n"
        "      \"type\":\"mc\", \"data\":{ \"consigna\":\"¿Cuáles son pares?\",\n"
        "        \"opciones\":[ {\"texto\":\"2\",\"correcta\":true}, {\"texto\":\"3\",\"correcta\":false}, {\"texto\":\"4\",\"correcta\":true} ] } },\n"
        "    { \"id\":3, \"x\":80, \"y\":300, \"ancho\":220, \"alto\":130,\n"
        "      \"titulo\":\"Mi test\", \"contenido\":\"\", \"color\":null,\n"
        "      \"type\":\"test\", \"data\":{ \"seleccion\":[1,2] } },\n"
        "    { \"id\":4, \"x\":360, \"y\":300, \"ancho\":220, \"alto\":130,\n"
        "      \"titulo\":\"Repaso\", \"contenido\":\"\", \"color\":null,\n"
        "      \"type\":\"asking\", \"data\":{ \"seleccion\":[1] } }\n"
        "  ],\n"
        "  \"flechas\": [], \"formas\": []\n"
        "}\n"
        "```\n\n"
        "## Tipos de nodo (`type` + lo que va en `data`)\n"
        "- `qa` → pregunta/respuesta: `data = { pregunta, respuesta }` (strings).\n"
        "- `mc` → multiple choice: `data = { consigna, opciones:[{ texto, correcta }] }`. "
        "  `correcta` es bool; **puede haber VARIAS correctas**.\n"
        "- `test` → quiz con puntaje final: `data = { seleccion:[ids de nodos qa/mc en ORDEN] }`.\n"
        "- `asking` → repaso estilo Anki: `data = { seleccion:[ids de nodos qa SOLAMENTE, en orden] }`.\n\n"
        "## Editar\n"
        "- **Agregar nodo**: id `lastIdCharged + 1`; poné `type` y su `data`; x/y/ancho/alto "
        "  numéricos; subir `lastIdCharged`.\n"
        "- **`seleccion`** (test/asking) son ids de OTROS nodos del mismo árbol (qa/mc para "
        "  test; solo qa para asking). No te referencies a vos mismo.\n"
        "- El `titulo` del nodo es su nombre visible; el contenido real va en `data`.\n"
        "- **Conectar** con flechas funciona igual que freestyle (opcional acá).\n"
        "- **Borrar nodo**: quitalo de `nodos` Y sacá su id de cualquier `seleccion` que lo use.\n"
        "- No inventes campos en `data`; respetá las claves de cada tipo. No dupliques ids.",
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
    _skill(
        "diagramind-object",
        "Metamodelo (tipo `object`): clases y objetos dinámicos. El GRAFO es el JSON; "
        "las referencias/arrays se modelan con FLECHAS tipadas.",
        "# Tipo object (clases y objetos dinámicos)\n\n"
        "Mismo contenedor que `freestyle` (nodos + flechas + formas + contadores), pero "
        "los nodos son del metamodelo. **CLAVE: el grafo ES el JSON** — los valores "
        "referencia/array NO van dentro del nodo, son **flechas tipadas**. Esquema EXACTO:\n\n"
        "```json\n"
        "{\n"
        "  \"type\": \"object\",\n"
        "  \"lastIdCharged\": 7, \"lastArrowId\": 3, \"lastShapeId\": 0, \"attachments\": {},\n"
        "  \"nodos\": [\n"
        "    { \"id\":1, \"x\":80,\"y\":40,\"ancho\":240,\"alto\":200, \"titulo\":\"Person\",\"contenido\":\"\",\"color\":null, \"type\":\"objclass\",\n"
        "      \"data\":{ \"padre\":null, \"atributos\":[ {\"id\":\"a1\",\"nombre\":\"name\",\"tipo\":\"string\",\"esArray\":false},\n"
        "                                              {\"id\":\"a2\",\"nombre\":\"car\",\"tipo\":{\"ref\":2},\"esArray\":false} ] } },\n"
        "    { \"id\":2, \"x\":400,\"y\":40, \"titulo\":\"Car\", \"type\":\"objclass\",\n"
        "      \"data\":{ \"padre\":null, \"atributos\":[ {\"id\":\"a3\",\"nombre\":\"brand\",\"tipo\":\"string\",\"esArray\":false} ] } },\n"
        "    { \"id\":3, \"x\":80,\"y\":320, \"titulo\":\"juan\", \"type\":\"objinstance\", \"data\":{ \"classId\":1, \"valores\":{ \"a1\":\"Juan\" } } },\n"
        "    { \"id\":4, \"x\":400,\"y\":320, \"titulo\":\"bmw\", \"type\":\"objinstance\", \"data\":{ \"classId\":2, \"valores\":{ \"a3\":\"BMW\" } } },\n"
        "    { \"id\":5, \"x\":700,\"y\":320, \"titulo\":\"cars\", \"type\":\"objcollection\", \"data\":{ \"kind\":\"list\", \"elementClass\":2 } },\n"
        "    { \"id\":6, \"x\":80,\"y\":560, \"titulo\":\"send\", \"type\":\"objfetch\",\n"
        "      \"data\":{ \"method\":\"POST\",\"url\":\"https://api/x\",\"headers\":[{\"k\":\"Authorization\",\"v\":\"Bearer ...\"}],\n"
        "               \"webhook\":{ \"enabled\":false,\"expectStatus\":\"2xx\",\"expectContains\":\"\",\"trigger\":\"onMismatch\",\"action\":\"notify\",\"notifyMethod\":\"POST\",\"notifyUrl\":\"\",\"notifyBody\":\"\" } } },\n"
        "    { \"id\":7, \"x\":400,\"y\":560, \"titulo\":\"sendAll\", \"type\":\"objburst\", \"data\":{ /* igual que objfetch */ } }\n"
        "  ],\n"
        "  \"flechas\": [\n"
        "    { \"id\":1, \"fromId\":1,\"toId\":2, \"fromSide\":\"right\",\"toSide\":\"left\", \"kind\":\"tiene\",\"attrId\":\"a2\",\"label\":\"car\",\"color\":null },\n"
        "    { \"id\":2, \"fromId\":3,\"toId\":4, \"fromSide\":\"right\",\"toSide\":\"left\", \"kind\":\"value\",\"attrId\":\"a2\",\"label\":\"car\",\"color\":null },\n"
        "    { \"id\":3, \"fromId\":5,\"toId\":4, \"fromSide\":\"right\",\"toSide\":\"left\", \"kind\":\"elem\",\"label\":\"\",\"color\":null }\n"
        "  ], \"formas\": []\n"
        "}\n"
        "```\n\n"
        "## Tipos de nodo (`type` + `data`)\n"
        "- `objclass` → CLASE. `data = { padre: idDeOtraClase|null, atributos:[{id(str único), nombre, tipo, esArray(bool)}] }`. "
        "  `tipo` = primitivo `\"string\"|\"number\"|\"bool\"|\"date\"` o referencia `{\"ref\": idDeClase}`. El `titulo` es el NOMBRE de la clase.\n"
        "- `objinstance` → OBJETO. `data = { classId: idDeClase|null, valores:{ [attrId]:valor } }`. En `valores` van SOLO los atributos PRIMITIVOS (por attrId). El `titulo` es solo etiqueta (NO sale en el JSON).\n"
        "- `objcollection` → `data = { kind:\"list\"|\"queue\"|\"stack\"|\"tree\", elementClass: idDeClase|null, "
        "order:[ids] (orden de miembros en list/queue/stack), edges:[{from,to}] (jerarquía padre→hijo en tree), layout:{} (posiciones, ignorable) }`. "
        "Miembros = objetos que la colección apunta por `elem`. Serializa: list/queue/stack → ARRAY (orden por `order`; stack en reverse = LIFO); "
        "tree → ANIDADO desde root(s) (root = miembro sin padre), cada nodo con `children:[...]`.\n"
        "- `objfetch` / `objburst` → `data = { method, url, headers:[{k,v}], webhook:{...} }`. `objburst` además: `data.sendOrder` para tree "
        "(`\"depth-ltr\"|\"depth-rtl\"|\"breadth-ltr\"|\"breadth-rtl\"`) y manda un request por cada elemento.\n"
        "  `webhook = { enabled, expectStatus, expectContains, trigger(onMismatch|onMatch|always), action(ignore|notify), notifyMethod, notifyUrl, notifyBody, stopOnTrigger(solo ráfaga) }`.\n\n"
        "## Flechas (`kind` + `attrId`)\n"
        "- `hereda` → herencia hija→padre (objclass→objclass); además seteá `padre` en la data de la hija. `attrId` null.\n"
        "- `tiene`  → atributo referencia entre clases (objclass→objclass); `attrId` = id del atributo en la clase origen.\n"
        "- `value`  → VALOR de un atributo referencia: objeto→objeto (ref simple) u objeto→colección (array); `attrId` = id del atributo.\n"
        "- `elem`   → elemento de una colección (objcollection→objinstance); `attrId` null.\n"
        "- `body`   → cuerpo de un fetch/ráfaga (objfetch|objburst → objinstance|objcollection); `attrId` null.\n\n"
        "## Reglas\n"
        "- ids de nodo/flecha = enteros (subí contadores). `attrId` = string único dentro de la clase (ej `\"a1\"`).\n"
        "- Atributo referencia LLENO en un objeto = una flecha `value` (NO va en `valores`).\n"
        "- Si una clase tiene un atributo `{ref:X}`, agregá también la flecha `tiene` clase→X con ese `attrId`.\n"
        "- Herencia: seteá `padre` Y agregá la flecha `hereda`. El objeto hereda los atributos efectivos (cadena de padres): usá esos `attrId`.\n"
        "- Colección: los miembros se definen con flechas `elem` (colección→objeto). El orden de envío sale de `order` (list/queue/stack) o de `edges` (tree).\n"
        "- El `titulo` NO es dato del JSON: el JSON sale de los atributos + las flechas.\n\n"
        "## Ejecutar un Fetch / Ráfaga (testear endpoints)\n"
        "**MUY IMPORTANTE — NO uses tu herramienta `WebFetch` ni `Bash`/`curl` para "
        "ejecutar un nodo Fetch del diagrama.** Esas herramientas NO arman el body desde "
        "el grafo de objetos, NO respetan el method/headers del nodo, NO pasan por el "
        "proxy del conector (CORS) y NO se ven en el canvas. La ÚNICA forma correcta de "
        "correr un nodo `objfetch`/`objburst` es por su estado:\n"
        "1. Poné en el `data` del nodo un campo `runReq` con una marca NUEVA (ej. un "
        "timestamp o un número distinto al anterior).\n"
        "2. GUARDÁ el `tree.json` y **terminá tu turno** (NO uses sleeps ni loops de espera).\n"
        "3. La app ejecuta el nodo y te devuelve el resultado (status + body + webhook) en "
        "el PRÓXIMO turno AUTOMÁTICAMENTE. También queda en el nodo como `data.lastResponse`.\n"
        "Para re-ejecutar, cambiá de nuevo `runReq`. Podés pedir varios nodos a la vez "
        "(varios `runReq`). Si te piden 'ejecutar/probar un fetch' de un proyecto object, "
        "esto es lo que tenés que hacer — nunca WebFetch.",
    ),
    _skill(
        "diagramind-editor",
        "Tipo `editor`: el proyecto abre una CARPETA REAL (no es un diagrama). "
        "Leer si el proyecto en foco es tipo editor.",
        "# Tipo editor (carpeta real)\n\n"
        "Un proyecto `editor` (doc 27) abre una **carpeta real** de la máquina. Su "
        "`tree.json` es solo un puntero:\n\n"
        "```json\n"
        "{ \"type\": \"editor\", \"target\": \"/ruta/a/la/carpeta\" }\n"
        "```\n\n"
        "## Reglas\n"
        "1. **NO edites su `tree.json`** (ni metas nodos ahí): los archivos del "
        "   proyecto NO viven en el workspace, viven en el `target`.\n"
        "2. Editor **LOCAL**: el chat te da **acceso directo al target** (la ruta "
        "   exacta viene en tu system prompt): trabajá sobre esos archivos con tus "
        "   herramientas normales (leer / editar / bash).\n"
        "3. Editor **EXTERNO** (el target vive en un conector): los archivos NO "
        "   están en este disco — usá las tools MCP del server «dmfs» "
        "   (mcp__dmfs__fs_tree / fs_read / fs_write / fs_mkdir / fs_rename / "
        "   fs_delete / fs_grep / fs_exec), con rutas relativas a la raíz del "
        "   proyecto. Para editar: fs_read → modificá → fs_write con el archivo "
        "   COMPLETO. Tu system prompt te dice cuál de los dos casos es. Además "
        "   tenés VERSIONES del proyecto: mcp__dmfs__sv_save({note}) ANTES de una "
        "   tanda de cambios (queda firmado como IA), sv_list para el historial y "
        "   sv_restore({id}) para volver atrás SOLO si el usuario lo pide.\n"
        "4. Los esquemas diagramind-* (ids, contadores, tipos de nodo) **no aplican** "
        "   a estos proyectos: es código/archivos comunes.\n"
        "5. Los demás proyectos del workspace siguen las reglas de siempre.",
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

"""HTML del panel de control del backend local (doc 18 §Panel de control).

Es UNA sola página autocontenida (HTML + CSS + JS inline, sin CDN ni assets):
así entra tal cual en el binario --onefile del CI y funciona sin internet.
La sirve `server.py` en GET / y la abre `panel.open_panel()` en una ventana
`--app` de Chrome/Edge (sin barra de direcciones → se ve como app propia).

El token de acceso se INYECTA en la página (`__TOKEN__`). Por eso estas rutas se
sirven SIN cabeceras CORS: aunque otra web le pegue a 127.0.0.1:8765/, el
navegador no la deja leer la respuesta y el token no se filtra.

Paleta: la del design system de DiagraMinder (styles.css), con variante clara.
"""

PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DiagraMind Local</title>
<style>
:root {
  --bg:#1f2229; --surface:#2b2e37; --surface-el:#313543; --surface2:#383d4a;
  --border:#454b59; --border-strong:#5b6272;
  --text:#f2f4f7; --text-secondary:#b8bfcc; --text-muted:#838b9b;
  --accent:#3f6bc9; --accent-dim:rgba(63,107,201,.18); --accent-hover:#35599e; --accent-text:#fff;
  --success:#3ba55d; --success-dim:rgba(59,165,93,.16);
  --warning:#d9a441; --warning-dim:rgba(217,164,65,.14);
  --danger:#c94444; --danger-dim:rgba(201,68,68,.18);
  --radius:10px;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg:#f4f5f8; --surface:#ffffff; --surface-el:#f7f8fa; --surface2:#eceef3;
    --border:#dcdfe7; --border-strong:#b9bfcc;
    --text:#1b1e25; --text-secondary:#4a5160; --text-muted:#7b8394;
    --accent:#3560bd; --accent-dim:rgba(53,96,189,.12); --accent-hover:#2b4f9e;
    --success:#2f8b4c; --success-dim:rgba(47,139,76,.12);
    --warning:#a9761d; --warning-dim:rgba(169,118,29,.12);
    --danger:#c33f3f; --danger-dim:rgba(195,63,63,.12);
  }
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,Inter,sans-serif;
  font-size:14px; line-height:1.5;
}
code, .mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12.5px; }
.wrap { max-width:820px; margin:0 auto; padding:22px 20px 56px; }

/* ---- header ---- */
header { display:flex; align-items:center; gap:12px; margin-bottom:20px; }
.logo {
  width:34px; height:34px; border-radius:9px; flex:none;
  background:linear-gradient(140deg,var(--accent),#7c6ceb);
  display:grid; place-items:center; color:#fff;
}
h1 { font-size:17px; margin:0; font-weight:650; letter-spacing:-.01em; }
.sub { color:var(--text-muted); font-size:12px; }
header .spacer { flex:1; }

/* ---- cards ---- */
.card {
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:16px 18px; margin-bottom:14px;
}
.card h2 {
  font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--text-muted); margin:0 0 12px; font-weight:600;
  display:flex; align-items:center; gap:8px;
}
.card h2 .spacer { flex:1; }

/* ---- filas ---- */
.row {
  display:flex; align-items:center; gap:12px; padding:10px 0;
  border-top:1px solid var(--border);
}
.row:first-of-type { border-top:none; }
.row .grow { flex:1; min-width:0; }
.row .name { font-weight:550; }
.row .meta { color:var(--text-muted); font-size:12px; word-break:break-all; }

/* ---- puntitos de estado ---- */
.dot { width:10px; height:10px; border-radius:50%; flex:none; background:var(--text-muted); }
.dot.ok   { background:var(--success); box-shadow:0 0 0 3px var(--success-dim); }
.dot.warn { background:var(--warning); box-shadow:0 0 0 3px var(--warning-dim); }
.dot.off  { background:var(--border-strong); }
.dot.err  { background:var(--danger); box-shadow:0 0 0 3px var(--danger-dim); }

/* ---- botones ---- */
button {
  font:inherit; font-size:13px; cursor:pointer; border-radius:8px; padding:6px 12px;
  border:1px solid var(--border-strong); background:transparent; color:var(--text);
  transition:background .12s, border-color .12s, color .12s;
}
button:hover:not(:disabled) { background:var(--surface2); }
button:disabled { opacity:.5; cursor:default; }
button.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-text); font-weight:550; }
button.primary:hover:not(:disabled) { background:var(--accent-hover); border-color:var(--accent-hover); }
button.ghost { border-color:transparent; color:var(--text-secondary); padding:6px 9px; }
button.ghost:hover:not(:disabled) { background:var(--surface2); color:var(--text); }
button.danger { color:var(--danger); border-color:color-mix(in srgb,var(--danger) 35%,transparent); }
button.danger:hover:not(:disabled) { background:var(--danger-dim); border-color:var(--danger); }
.btns { display:flex; gap:8px; flex-wrap:wrap; }

/* ---- avisos ---- */
.note {
  border-radius:8px; padding:10px 12px; font-size:13px; margin-bottom:12px;
  border:1px solid transparent; color:var(--text-secondary);
}
.note strong { color:var(--text); font-weight:600; }
.note.warn { background:var(--warning-dim); border-color:color-mix(in srgb,var(--warning) 40%,transparent); }
.note.info { background:var(--accent-dim); border-color:color-mix(in srgb,var(--accent) 35%,transparent); }
.note.err  { background:var(--danger-dim);  border-color:color-mix(in srgb,var(--danger) 40%,transparent); }
.note a { color:var(--accent); }
@media (prefers-color-scheme: light) { .note.warn, .note.info, .note.err { color:var(--text); } }

/* ---- misc ---- */
.pill {
  font-size:11.5px; padding:2px 8px; border-radius:999px; background:var(--surface2);
  color:var(--text-muted); border:1px solid var(--border);
}
.tokenbox {
  display:flex; align-items:center; gap:8px; background:var(--surface-el);
  border:1px solid var(--border); border-radius:8px; padding:8px 10px; margin-bottom:10px;
}
.tokenbox .val { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; letter-spacing:.02em; }
pre.console {
  background:var(--surface-el); border:1px solid var(--border); border-radius:8px;
  padding:10px 12px; margin:10px 0 0; max-height:220px; overflow:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px;
  color:var(--text-secondary); white-space:pre-wrap; word-break:break-word;
}
.steps { margin:0; padding-left:20px; color:var(--text-secondary); }
.steps li { margin:5px 0; }
.steps b { color:var(--text); font-weight:600; }
.tree { margin:0; max-height:300px; overflow:auto; }
.tree .folder { font-weight:600; margin-top:10px; display:flex; align-items:center; gap:7px; }
.tree .folder:first-child { margin-top:0; }
.tree .folder .spacer { flex:1; }
.tree .proj {
  display:flex; align-items:center; gap:8px; padding:3px 0 3px 21px;
  color:var(--text-secondary); font-size:13px;
}
.tree .proj .type { color:var(--text-muted); font-size:11.5px; }
.empty { color:var(--text-muted); font-size:13px; }
.spin { animation:spin 1s linear infinite; transform-origin:50% 50%; }
@keyframes spin { to { transform:rotate(360deg); } }
#toast {
  position:fixed; left:50%; bottom:22px; transform:translateX(-50%) translateY(20px);
  background:var(--surface2); color:var(--text); border:1px solid var(--border-strong);
  border-radius:8px; padding:9px 16px; font-size:13px; opacity:0; pointer-events:none;
  transition:opacity .18s, transform .18s; z-index:50;
}
#toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="logo">
      <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
           stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>
        <path d="M10 6.5h2.5a2 2 0 0 1 2 2V14"/>
      </svg>
    </div>
    <div>
      <h1>DiagraMind Local</h1>
      <div class="sub" id="hdr-sub">cargando…</div>
    </div>
    <div class="spacer"></div>
    <span class="dot ok" id="hdr-dot"></span>
    <span class="sub" id="hdr-state">servidor activo</span>
  </header>

  <!-- ============ CLIs de IA ============ -->
  <section class="card">
    <h2>Motores de IA (CLI)<span class="spacer"></span><span class="pill" id="cli-count"></span></h2>
    <div id="cli-note"></div>
    <div id="cli-list"></div>
    <pre class="console" id="install-log" hidden></pre>
  </section>

  <!-- ============ contraseña ============ -->
  <section class="card">
    <h2>Contraseña de acceso</h2>
    <div class="tokenbox">
      <span class="mono val" id="tok-val">••••••••••••••••••••</span>
      <button class="ghost" id="tok-eye">Ver</button>
      <button class="ghost" id="tok-copy">Copiar</button>
    </div>
    <div class="note info">
      La web te la pide una sola vez al tocar <strong>Conectar local</strong>. Sirve para que
      ninguna otra página de tu navegador pueda usar este backend.
    </div>
    <div class="row">
      <div class="grow"><div class="meta" id="tok-path"></div></div>
      <button class="danger" id="tok-regen">Regenerar</button>
    </div>
  </section>

  <!-- ============ proyectos ============ -->
  <section class="card">
    <h2>Proyectos<span class="spacer"></span><span class="pill" id="proj-count"></span></h2>
    <div class="row">
      <div class="grow">
        <div class="name">Carpeta raíz</div>
        <div class="meta mono" id="root-path"></div>
      </div>
      <div class="btns">
        <button id="root-open">Abrir</button>
        <button id="root-change">Cambiar…</button>
      </div>
    </div>
    <div class="row" style="display:block">
      <div class="tree" id="proj-tree"></div>
    </div>
  </section>

  <!-- ============ conectar la web ============ -->
  <section class="card">
    <h2>Conectar la web</h2>
    <ol class="steps">
      <li>Abrí DiagraMind en el navegador y entrá a <b>IA → Conectar local</b>.</li>
      <li>Pegá la <b>contraseña</b> de arriba cuando te la pida.</li>
      <li>El estado del panel IA pasa a <b>Conectado</b> y ya podés chatear con los CLIs.</li>
    </ol>
    <div class="row">
      <div class="grow"><div class="meta">Este backend escucha en <span class="mono" id="url-val"></span></div></div>
      <button id="url-copy">Copiar URL</button>
    </div>
  </section>

  <!-- ============ servidor ============ -->
  <section class="card">
    <h2>Servidor</h2>
    <div class="row">
      <div class="grow">
        <div class="name" id="srv-name">diagramind-local</div>
        <div class="meta" id="srv-meta"></div>
      </div>
      <button class="danger" id="srv-stop" hidden>Detener</button>
    </div>
    <div class="note info" style="margin:12px 0 0" id="srv-note"></div>
  </section>

</div>
<div id="toast"></div>

<script>
const TOKEN = "__TOKEN__";
const $ = (id) => document.getElementById(id);

function url(path) {
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  };
  const r = await fetch(url(path), opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}
let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg; el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}
function copy(text, msg) {
  navigator.clipboard.writeText(text).then(() => toast(msg || "Copiado"))
    .catch(() => toast("No se pudo copiar"));
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

let STATE = null;
let installing = null;   // key del CLI que se está instalando

/* ---------------- render ---------------- */
function render(s) {
  STATE = s;

  $("hdr-sub").textContent = "v" + s.version + " · puerto " + s.port;
  $("hdr-state").textContent = "servidor activo · " + s.uptime;

  renderClis(s);
  renderProjects(s);

  $("tok-path").textContent = "Guardada en " + s.tokenPath;
  $("url-val").textContent = s.url;
  $("srv-meta").textContent = s.url + " · datos en " + s.base;
  $("proj-count").textContent = s.projects.count + " proyecto" + (s.projects.count === 1 ? "" : "s");

  // Con la ventana abierta por el propio backend, cerrarla lo apaga. Si en cambio
  // es una instancia residente (arrancó con --no-ui, p.ej. el auto-inicio), sigue
  // viva al cerrar y por eso ahí sí mostramos el botón de detener.
  $("srv-stop").hidden = s.autoStop;
  $("srv-note").innerHTML = s.autoStop
    ? "Cerrar esta ventana <strong>detiene el servidor</strong> y la web pierde la conexión. " +
      "Para volver a levantarlo, abrí DiagraMind Local de nuevo."
    : "Este backend arrancó solo con el sistema, así que <strong>sigue corriendo</strong> " +
      "aunque cierres esta ventana.";
}

function renderClis(s) {
  const ok = s.clis.filter((c) => c.available).length;
  $("cli-count").textContent = ok + " de " + s.clis.length + " instalados";

  let note = "";
  if (ok === 0) {
    note = '<div class="note warn"><strong>Necesitás al menos uno de estos CLIs.</strong> ' +
      'Son los que realmente escriben tus diagramas cuando chateás con la IA desde la web: ' +
      'sin ninguno instalado, el chat con backend <strong>Local</strong> no va a poder responder. ' +
      'Tocá <strong>Instalar</strong> en el que uses — se baja el instalador oficial, no hace ' +
      'falta nada más. Cada uno te va a pedir su cuenta la primera vez que lo corras.</div>';
  } else if (ok < s.clis.length) {
    note = '<div class="note info">Ya podés usar la IA local. Instalar los otros te sirve para ' +
      '<strong>swapear de motor</strong> si te quedás sin tokens en uno.</div>';
  }
  $("cli-note").innerHTML = note;

  $("cli-list").innerHTML = s.clis.map((c) => {
    const busy = installing === c.key;
    const auto = c.install.mode === "auto";
    const dot = c.available ? "ok" : (busy ? "warn" : "off");
    const meta = c.available
      ? esc(c.version || "instalado") + (c.resume ? " · memoria de conversación" : "")
      : (busy ? "instalando…" : "no instalado · " + esc(c.install.label));
    const action = auto
      ? '<button class="' + (c.available ? "" : "primary") + '" data-install="' + c.key + '"' +
        (busy ? " disabled" : "") + ">" +
        (busy ? "Instalando…" : (c.available ? "Reinstalar" : "Instalar")) + "</button>"
      : '<button data-how="' + c.key + '">Cómo instalar</button>';
    return '' +
      '<div class="row">' +
        '<span class="dot ' + dot + '"></span>' +
        '<div class="grow"><div class="name">' + esc(c.label) + '</div>' +
        '<div class="meta">' + meta + '</div></div>' +
        '<div class="btns">' +
          '<button class="ghost" data-docs="' + esc(c.docs) + '">Docs</button>' + action +
        '</div>' +
      '</div>';
  }).join("");
}

function renderProjects(s) {
  $("root-path").textContent = s.root;
  const folders = s.projects.folders;
  if (!folders.length) {
    $("proj-tree").innerHTML = '<div class="empty">Todavía no hay proyectos en disco. ' +
      'Aparecen acá cuando conectás la web y se sincronizan.</div>';
    return;
  }
  $("proj-tree").innerHTML = folders.map((f) =>
    '<div class="folder">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>' +
      esc(f.name) + '<span class="spacer"></span>' +
      '<button class="ghost" data-reveal="' + esc(f.name) + '">Abrir</button>' +
    '</div>' +
    (f.projects.length
      ? f.projects.map((p) => '<div class="proj">· ' + esc(p.name) +
          ' <span class="type">' + esc(p.type) + " · " + esc(p.modified) + "</span></div>").join("")
      : '<div class="proj empty">(vacía)</div>')
  ).join("");
}

/* ---------------- acciones ---------------- */
async function refresh() {
  try {
    render(await api("/panel/status"));
  } catch (e) {
    $("hdr-dot").className = "dot err";
    $("hdr-state").textContent = "sin respuesta";
  }
}

function install(key) {
  const log = $("install-log");
  installing = key;
  log.hidden = false;
  log.textContent = "";
  renderClis(STATE);
  api("/panel/install", {cli: key}).then((r) => {
    const es = new EventSource(url("/panel/install/stream?runId=" + r.runId));
    es.onmessage = (ev) => {
      const d = JSON.parse(ev.data);
      if (d.kind === "log") {
        log.textContent += d.line + "\n";
        log.scrollTop = log.scrollHeight;
      } else if (d.kind === "status" && ["done", "error", "cancelled"].includes(d.status)) {
        es.close();
        installing = null;
        if (d.status === "done") toast("Instalado");
        else { log.textContent += "\n✗ " + (d.error || "falló la instalación") + "\n"; toast("Falló la instalación"); }
        refresh();
      }
    };
    es.onerror = () => { es.close(); installing = null; refresh(); };
  }).catch((e) => {
    installing = null;
    log.textContent += "✗ " + e.message + "\n";
    refresh();
  });
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;

  if (t.dataset.install) return install(t.dataset.install);
  if (t.dataset.docs) return window.open(t.dataset.docs, "_blank", "noreferrer");
  if (t.dataset.how) {
    // sin vía automática en este sistema: mostramos el comando y lo copiamos
    const c = STATE.clis.find((x) => x.key === t.dataset.how);
    $("cli-note").innerHTML = '<div class="note warn">' + esc(c.install.note) +
      (c.install.cmd ? ' <span class="mono">' + esc(c.install.cmd) + "</span>" : "") + "</div>";
    if (c.install.cmd) copy(c.install.cmd, "Comando copiado");
    return;
  }
  if (t.dataset.reveal !== undefined) {
    try { await api("/folders/reveal", {folder: t.dataset.reveal}); } catch (e) { toast(e.message); }
    return;
  }

  switch (t.id) {
    case "tok-eye": {
      const shown = $("tok-val").dataset.shown === "1";
      $("tok-val").dataset.shown = shown ? "0" : "1";
      $("tok-val").textContent = shown ? "••••••••••••••••••••" : STATE.token;
      t.textContent = shown ? "Ver" : "Ocultar";
      break;
    }
    case "tok-copy": copy(STATE.token, "Contraseña copiada"); break;
    case "tok-regen":
      if (!confirm("¿Generar una contraseña nueva?\n\nLa web va a perder la conexión hasta que " +
                   "pegues la nueva en «Conectar local».")) break;
      try {
        await api("/panel/token/regenerate", {});
        location.reload();          // la página se re-sirve ya con el token nuevo inyectado
      } catch (e) { toast(e.message); }
      break;
    case "url-copy": copy(STATE.url, "URL copiada"); break;
    case "root-open":
      try { await api("/folders/reveal", {}); } catch (e) { toast(e.message); }
      break;
    case "root-change":
      toast("Elegí la carpeta en el diálogo del sistema…");
      try {
        const p = await api("/folders/pick?title=Carpeta%20de%20proyectos%20de%20DiagraMind");
        if (p.path) { await api("/config/root", {path: p.path}); toast("Carpeta cambiada"); refresh(); }
      } catch (e) { toast(e.message); }
      break;
    case "srv-stop":
      if (!confirm("¿Detener el servidor?\n\nLa web va a quedar desconectada hasta que lo " +
                   "vuelvas a abrir.")) break;
      try { await api("/panel/shutdown", {}); } catch (e) { /* el server se muere sin responder */ }
      $("hdr-dot").className = "dot off";
      $("hdr-state").textContent = "detenido";
      document.querySelectorAll("button").forEach((b) => (b.disabled = true));
      break;
  }
});

/* ---------------- presencia: cerrar la ventana apaga el backend ----------------
   Un SSE de larga duración que vive mientras la página viva. Cuando se cierra la
   ventana, el socket muere y el backend se apaga (solo si él mismo abrió el panel;
   una instancia residente —auto-inicio, --no-ui— sigue corriendo). Es un socket y
   no un temporizador a propósito: el navegador estrangula los timers de pestañas
   en segundo plano, y eso apagaría el backend de mentira. */
const alive = new EventSource(url("/panel/alive"));

refresh();
setInterval(() => { if (!installing) refresh(); }, 5000);
</script>
</body>
</html>
"""


def page(token):
    """La página con el token inyectado (se sirve SIN CORS: ver docstring)."""
    return PAGE.replace("__TOKEN__", token)

"""
panel.py — Panel de administración local (Fase 1.5 del roadmap).

Un panel web para no tener que abrir la terminal cada vez ni tener que
saber Python/SQL para operar el pipeline:
- Ves las cadenas Shopify configuradas (config_cadenas.json).
- Por cadena, un botón "Descubrir categorías" te trae TODAS las categorías
  reales del sitio (vía collections.json) con su conteo de productos, para
  que elijas cuáles scrapear sin copiar URLs a mano.
- Marcas las que quieres, apretás "Scrapear seleccionadas" y el panel
  corre scraping -> carga -> matchear -> export_json solo, en el orden
  correcto, y te muestra el log en vivo.
- Las categorías que elijas se guardan en config_cadenas.json, así la
  próxima corrida de run_all.py (o del cron) ya las incluye sin que
  vuelvas a tocar el panel.
- Estado del matching en vivo: barra de % de avance, en qué categoría va
  (el matching ahora procesa categoría por categoría, ver nota en
  etl.matchear), cuántas fusiones lleva, cuántas consultas al LLM, ETA --
  viene de agent_runs (etl.py ahora commitea seguido y guarda el progreso
  ahí en vez de recién al final). Podés correr SOLO el matching (sin
  re-scrapear) con un límite de consultas LLM configurable desde acá, en
  vez de un env var.
- Historial de las últimas corridas, cada una con su cadena y categoría/
  ruta bien claras (antes quedaban genéricas, ver fix en
  etl.cargar_resultado) y filtro para ver solo las que fallaron -- con
  un botón para reintentar esa cadena sin volver a la consola.

Esto corre SOLO en tu máquina (no es para exponer a internet tal cual —
no tiene login). Es la base sobre la que se puede construir un dashboard
real más adelante si el negocio lo justifica.

Uso:
    pip install flask
    python panel.py
    abre http://localhost:5050
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

import psycopg2.extras
from flask import Flask, jsonify, render_template_string, request

import etl
import scraper_shopify

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config_cadenas.json"

app = Flask(__name__)

# Estado del último run, en memoria (simple a propósito: un solo usuario, un solo proceso)
estado = {"corriendo": False, "log": []}


def cargar_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def guardar_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def log(msg: str):
    print(msg, flush=True)
    estado["log"].append(msg)
    estado["log"] = estado["log"][-300:]  # no crecer sin límite


def correr_comando(cmd: list[str]):
    """Corre cualquier comando (run_all.py o etl.py matchear) como
    subproceso y va agregando su output al log en vivo. Subproceso (no
    import directo) para que un error de un sitio no tumbe el panel
    entero."""
    estado["corriendo"] = True
    estado["log"] = []
    try:
        log(f"Ejecutando: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            log(line.rstrip())
        proc.wait()
        log(f"--- Terminado (código {proc.returncode}) ---")
    except Exception as e:
        log(f"ERROR fatal: {e}")
    finally:
        estado["corriendo"] = False


@app.route("/")
def home():
    return render_template_string(HTML, cfg=cargar_config())


@app.route("/api/descubrir")
def api_descubrir():
    sitio_base = request.args.get("sitio_base")
    if not sitio_base:
        return jsonify({"error": "falta sitio_base"}), 400
    try:
        cols = scraper_shopify.listar_colecciones(sitio_base)
        cols.sort(key=lambda c: -c["productos"])
        return jsonify(cols)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guardar_colecciones", methods=["POST"])
def api_guardar_colecciones():
    data = request.get_json()
    cadena = data["cadena"]
    handles = data["handles"]
    cfg = cargar_config()
    for chain_cfg in cfg["shopify"]:
        if chain_cfg["cadena"] == cadena:
            chain_cfg["colecciones"] = handles
    guardar_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/correr", methods=["POST"])
def api_correr():
    if estado["corriendo"]:
        return jsonify({"error": "ya hay una corrida en curso"}), 409
    data = request.get_json(silent=True) or {}
    cadenas = data.get("cadenas")  # None = todas
    cmd = [sys.executable, "run_all.py"]
    if cadenas and len(cadenas) == 1:
        cmd += ["--solo", cadenas[0]]
    threading.Thread(target=correr_comando, args=(cmd,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/estado")
def api_estado():
    return jsonify(estado)


@app.route("/api/matching/correr", methods=["POST"])
def api_matching_correr():
    """Corre SOLO etl.py matchear (sin re-scrapear nada) -- para cuando
    solo hace falta procesar el backlog de matching con un límite de LLM
    distinto, sin esperar un scraping completo de nuevo."""
    if estado["corriendo"]:
        return jsonify({"error": "ya hay una corrida en curso"}), 409
    data = request.get_json(silent=True) or {}
    max_llm = data.get("max_llm")
    cmd = [sys.executable, "etl.py", "matchear"]
    if max_llm not in (None, ""):
        try:
            cmd += ["--max-llm", str(int(max_llm))]
        except (TypeError, ValueError):
            return jsonify({"error": "max_llm tiene que ser un número"}), 400
    threading.Thread(target=correr_comando, args=(cmd,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/matching/estado")
def api_matching_estado():
    """Última corrida de matching (en progreso, o la más reciente
    terminada) -- el detalle lo escribe etl.matchear() cada ~15
    candidatos o cada 20s, no solo al final, así esto refleja avance
    real mientras corre. Incluye en qué categoría va (matchear() ahora
    procesa categoría por categoría, ver etl.py)."""
    try:
        conn = etl.get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, estado, iniciado_en, finalizado_en, registros_procesados, detalle "
            "FROM agent_runs WHERE agente_tipo = 'matching' "
            "ORDER BY iniciado_en DESC LIMIT 1"
        )
        fila = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not fila:
        return jsonify({"existe": False})
    return jsonify({
        "existe": True,
        "id": str(fila["id"]),
        "estado": fila["estado"],
        "iniciado_en": fila["iniciado_en"].isoformat() if fila["iniciado_en"] else None,
        "finalizado_en": fila["finalizado_en"].isoformat() if fila["finalizado_en"] else None,
        "procesados": fila["registros_procesados"],
        "detalle": fila["detalle"] or {},
    })


@app.route("/api/historial")
def api_historial():
    """Últimas corridas de cualquier tipo (scraper, matching, carga
    manual) -- de acá sale tanto el historial general como el filtro de
    "solo errores" (mismo dato, filtrado en el frontend)."""
    try:
        conn = etl.get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT ar.id, ar.agente_tipo, ar.fuente_metodo, ar.estado,
                   ar.iniciado_en, ar.finalizado_en, ar.registros_procesados,
                   ar.registros_error, ar.detalle, c.nombre AS cadena
            FROM agent_runs ar
            LEFT JOIN cadenas c ON c.id = ar.cadena_id
            ORDER BY ar.iniciado_en DESC
            LIMIT 50
            """
        )
        filas = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    def fmt(f):
        detalle = f["detalle"] or {}
        # Para matching, la "ruta" más útil es en qué categoría iba (o
        # terminó) -- el fuente_metodo crudo ("max_llm=200") es config,
        # no dice qué se estaba procesando.
        if f["agente_tipo"] == "matching":
            cat = detalle.get("categoria_actual")
            bucket = detalle.get("bucket_actual")
            bt = detalle.get("buckets_totales")
            ruta = f"{cat} (categoría {bucket}/{bt})" if cat and bucket else (f["fuente_metodo"] or "—")
        else:
            ruta = f["fuente_metodo"]
        return {
            "id": str(f["id"]), "tipo": f["agente_tipo"], "cadena": f["cadena"],
            "ruta": ruta, "estado": f["estado"],
            "iniciado_en": f["iniciado_en"].isoformat() if f["iniciado_en"] else None,
            "finalizado_en": f["finalizado_en"].isoformat() if f["finalizado_en"] else None,
            "procesados": f["registros_procesados"], "errores": f["registros_error"],
            "error_detalle": detalle.get("error"),
        }
    return jsonify([fmt(f) for f in filas])


@app.route("/api/reintentar", methods=["POST"])
def api_reintentar():
    """Reintenta la cadena ENTERA de una ruta que falló -- run_all.py no
    tiene forma de re-scrapear una sola categoría suelta, así que esto
    corre esa cadena de punta a punta de nuevo (rápido para Shopify, que
    descubre categorías solo; para Hipermaxi/Chavez recorre todas sus
    URLs configuradas)."""
    if estado["corriendo"]:
        return jsonify({"error": "ya hay una corrida en curso"}), 409
    data = request.get_json(silent=True) or {}
    cadena = data.get("cadena")
    if not cadena:
        return jsonify({"error": "falta cadena"}), 400
    cmd = [sys.executable, "run_all.py", "--solo", cadena]
    threading.Thread(target=correr_comando, args=(cmd,), daemon=True).start()
    return jsonify({"ok": True})


HTML = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price Intelligence — Panel</title>
<style>
  :root {
    --bg: #0d0f14; --panel: #161a22; --panel-2: #1d212b; --border: #2a2f3a;
    --texto: #eef0f4; --muted: #8d93a3; --primary: #5b7cfa; --primary-2: #4364e0;
    --green: #34c77b; --green-bg: rgba(52,199,123,.12);
    --red: #ff6161; --red-bg: rgba(255,97,97,.12);
    --amber: #f5a623; --amber-bg: rgba(245,166,35,.12);
    --radius: 14px;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif; max-width: 1040px; margin: 0 auto;
    padding: 28px 20px 60px; background: var(--bg); color: var(--texto); line-height: 1.45;
  }
  h1 { font-size: 1.5rem; margin: 0 0 4px; display: flex; align-items: center; gap: 10px; }
  .subtitulo { color: var(--muted); font-size: .92rem; margin: 0 0 22px; }

  .franja-estado {
    display: flex; align-items: center; gap: 10px; padding: 10px 16px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--border); font-size: .85rem; font-weight: 600;
    width: fit-content; margin-bottom: 26px;
  }
  .franja-estado .punto { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); }
  .franja-estado.activa .punto { background: var(--green); box-shadow: 0 0 0 4px var(--green-bg); animation: latido 1.4s infinite; }
  .franja-estado.activa { color: var(--green); }
  @keyframes latido { 0%,100% { opacity: 1 } 50% { opacity: .4 } }

  section.bloque { margin-top: 34px; }
  .bloque-titulo { display: flex; align-items: center; gap: 9px; font-size: 1.08rem; font-weight: 700; margin-bottom: 4px; }
  .bloque-ayuda { color: var(--muted); font-size: .85rem; margin: 0 0 14px; max-width: 720px; }

  .caja { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px 20px; }
  .caja + .caja { margin-top: 12px; }

  .cadena-caja { border-left: 3px solid var(--primary); }
  .cadena-cabeza { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .cadena-nombre { font-weight: 700; font-size: 1.02rem; }
  .cadena-sitio { color: var(--muted); font-size: .8rem; font-weight: 400; }
  .cadena-cats { color: var(--muted); font-size: .82rem; margin: 6px 0 10px; }
  .colecciones { max-height: 220px; overflow-y: auto; margin: .5rem 0; }
  .colecciones label { display: flex; align-items: center; gap: 8px; padding: .3rem 0; font-size: .87rem; }
  .colecciones .b { color: var(--muted); font-size: .78rem; }

  .fila-botones { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  button {
    background: var(--primary); color: white; border: none; padding: .55rem 1rem; border-radius: 8px;
    cursor: pointer; font-size: .86rem; font-weight: 600; transition: background .15s, opacity .15s;
  }
  button:hover { background: var(--primary-2); }
  button:disabled { opacity: .45; cursor: default; }
  button.secundario { background: var(--panel-2); border: 1px solid var(--border); color: var(--texto); }
  button.secundario:hover { background: #262b37; }
  button.chico { padding: .32rem .7rem; font-size: .78rem; }
  button.principal { font-size: .95rem; padding: .75rem 1.3rem; }

  input[type=number] {
    background: var(--bg); border: 1px solid var(--border); color: var(--texto); border-radius: 8px;
    padding: .5rem .7rem; width: 100px; font-size: .86rem;
  }
  input[type=number]:focus { outline: none; border-color: var(--primary); }
  label.campo { font-size: .85rem; color: var(--muted); }

  .barra-fondo { background: var(--bg); border-radius: 999px; height: 20px; overflow: hidden; margin: 10px 0 12px; border: 1px solid var(--border); }
  .barra-relleno {
    background: linear-gradient(90deg, var(--primary), #7b93ff); height: 100%; transition: width .4s ease;
    display: flex; align-items: center; justify-content: flex-end; padding-right: 8px;
    font-size: .72rem; font-weight: 700; color: #fff; min-width: 30px;
  }
  .barra-relleno.ok { background: var(--green); }
  .barra-relleno.error { background: var(--red); }

  .categoria-actual {
    display: inline-flex; align-items: center; gap: 6px; background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 999px; padding: 4px 12px; font-size: .8rem; color: var(--texto); margin-bottom: 10px;
  }

  .stats-grid { display: flex; gap: 22px; flex-wrap: wrap; margin-top: 6px; }
  .stat { min-width: 100px; }
  .stat .n { font-size: 1.25rem; font-weight: 700; display: block; }
  .stat .l { font-size: .74rem; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; }

  .tabs { display: flex; gap: 6px; margin-bottom: 12px; }
  .tab { padding: .4rem .9rem; border-radius: 999px; font-size: .82rem; font-weight: 600; cursor: pointer;
         background: var(--panel-2); border: 1px solid var(--border); color: var(--muted); }
  .tab.activo { background: var(--primary); color: #fff; border-color: var(--primary); }

  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  th, td { text-align: left; padding: .6rem .5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 600; font-size: .74rem; text-transform: uppercase; letter-spacing: .03em; }
  tr:last-child td { border-bottom: none; }
  .celda-ruta { color: var(--muted); font-size: .78rem; margin-top: 2px; }
  .celda-cadena { font-weight: 600; }

  .pill {
    display: inline-flex; align-items: center; gap: 5px; padding: .2rem .6rem; border-radius: 999px;
    font-size: .74rem; font-weight: 700;
  }
  .pill.ok, .pill.ok_con_errores { background: var(--green-bg); color: var(--green); }
  .pill.error { background: var(--red-bg); color: var(--red); }
  .pill.en_progreso { background: rgba(91,124,250,.15); color: var(--primary); }

  #log {
    background: #000; color: #7fffa0; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .78rem;
    padding: 16px; border-radius: var(--radius); height: 260px; overflow-y: auto; white-space: pre-wrap;
    margin-top: 14px; border: 1px solid var(--border);
  }
  .vacio { color: var(--muted); font-size: .85rem; padding: 6px 0; }
</style>
</head>
<body>
  <h1>🧭 Price Intelligence — Panel</h1>
  <p class="subtitulo">Scraping, matching y monitoreo del pipeline completo, sin pasar por la terminal.</p>

  <div class="franja-estado" id="franja-estado">
    <span class="punto"></span><span id="franja-texto">Verificando estado...</span>
  </div>

  <section class="bloque">
    <div class="bloque-titulo">🛒 Cadenas Shopify</div>
    <p class="bloque-ayuda">Estas cadenas descubren sus propias categorías solas. Elegí cuáles scrapear por cadena, o corré todo el pipeline de una.</p>

    {% for chain in cfg.shopify %}
    <div class="caja cadena-caja" data-cadena="{{ chain.cadena }}" data-sitio="{{ chain.sitio_base }}">
      <div class="cadena-cabeza">
        <span class="cadena-nombre">{{ chain.cadena }} <span class="cadena-sitio">{{ chain.sitio_base }}</span></span>
      </div>
      <div class="cadena-cats">Categorías activas hoy: {{ chain.colecciones|join(', ') if chain.colecciones else 'ninguna configurada' }}</div>
      <div class="colecciones" data-loaded="false"><span class="b">Sin descubrir aún — tocá "Descubrir categorías".</span></div>
      <div class="fila-botones">
        <button class="secundario chico" onclick="descubrir('{{ chain.cadena }}', '{{ chain.sitio_base }}')">Descubrir categorías</button>
        <button class="secundario chico" onclick="guardarSeleccion('{{ chain.cadena }}')">Guardar selección</button>
        <button class="chico" onclick="correr(['{{ chain.cadena }}'])">▶ Scrapear esta cadena</button>
      </div>
    </div>
    {% endfor %}
  </section>

  <section class="bloque">
    <div class="bloque-titulo">🧭 Cadenas con navegador</div>
    <p class="bloque-ayuda">Más lentas (Playwright, sin descubrimiento automático). Para agregar una categoría nueva, editá config_cadenas.json a mano (sección "navegador") y agregá la URL al array "urls".</p>
    {% for chain in cfg.navegador %}
    <div class="caja cadena-caja">
      <div class="cadena-nombre">{{ chain.cadena }}</div>
      <div class="cadena-cats">URLs configuradas: {{ chain.urls|join(', ') }}</div>
      <div class="fila-botones">
        <button class="chico" onclick="correr(['{{ chain.cadena }}'])">▶ Scrapear esta cadena</button>
      </div>
    </div>
    {% endfor %}
  </section>

  <button id="btn-todo" class="principal" style="margin-top:22px" onclick="correr(null)">▶ Correr TODO el pipeline (scraping + carga + match + export)</button>

  <section class="bloque">
    <div class="bloque-titulo">⚙️ Estado del matching</div>
    <p class="bloque-ayuda">El matching compara productos categoría por categoría (no todo el catálogo contra todo), así que cuando el catálogo crezca sigue siendo manejable.</p>
    <div class="caja">
      <div id="matching-vacio" class="vacio">Todavía no corrió ningún matching desde que existe este panel.</div>
      <div id="matching-info" style="display:none">
        <div class="barra-fondo"><div class="barra-relleno" id="matching-barra" style="width:0%"></div></div>
        <div class="categoria-actual" id="m-categoria-wrap" style="display:none">📂 Procesando: <b id="m-categoria">—</b></div>
        <div class="stats-grid">
          <div class="stat"><span class="n" id="m-estado">—</span><span class="l">Estado</span></div>
          <div class="stat"><span class="n" id="m-candidatos">—</span><span class="l">Candidatos</span></div>
          <div class="stat"><span class="n" id="m-fusiones">—</span><span class="l">Fusiones</span></div>
          <div class="stat"><span class="n" id="m-llm">—</span><span class="l">Consultas LLM</span></div>
          <div class="stat"><span class="n" id="m-eta">—</span><span class="l">ETA</span></div>
        </div>
      </div>
      <div class="fila-botones" style="margin-top:16px; align-items:center;">
        <label class="campo" for="max-llm-input">Límite de consultas LLM esta corrida:</label>
        <input type="number" id="max-llm-input" min="0" placeholder="default: 50">
        <button id="btn-matching" class="secundario" onclick="correrMatching()">Correr solo matching</button>
      </div>
      <p class="bloque-ayuda" style="margin:10px 0 0">No re-scrapea nada — usa lo que ya está cargado en Postgres. Dejá el campo vacío para usar el límite de siempre.</p>
    </div>
  </section>

  <section class="bloque">
    <div class="bloque-titulo">📋 Historial de corridas</div>
    <p class="bloque-ayuda">Cada scraping, carga y matching queda acá con su cadena y categoría/ruta. Las que fallaron se pueden reintentar con un click.</p>
    <div class="tabs">
      <div class="tab activo" data-filtro="todo" onclick="cambiarFiltro('todo')">Todo</div>
      <div class="tab" data-filtro="error" onclick="cambiarFiltro('error')">Solo errores</div>
    </div>
    <div class="caja">
      <table id="tabla-historial">
        <thead><tr><th>Cuándo</th><th>Tipo</th><th>Cadena</th><th>Categoría / ruta</th><th>Estado</th><th>Procesados</th><th></th></tr></thead>
        <tbody><tr><td colspan="7" class="vacio">Cargando...</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="bloque">
    <div class="bloque-titulo">🖥️ Log en vivo</div>
    <div id="log">Esperando...</div>
  </section>

<script>
let filtroHistorial = 'todo';
let ultimoHistorial = [];

async function descubrir(cadena, sitio) {
  const div = document.querySelector(`.cadena-caja[data-cadena="${cadena}"] .colecciones`);
  div.innerHTML = '<span class="b">Cargando...</span>';
  const r = await fetch(`/api/descubrir?sitio_base=${encodeURIComponent(sitio)}`);
  const cols = await r.json();
  if (cols.error) { div.innerHTML = `<span class="b">Error: ${cols.error}</span>`; return; }
  const activasTexto = document.querySelector(`.cadena-caja[data-cadena="${cadena}"] .cadena-cats`).textContent.split(': ')[1] || '';
  const activas = new Set(activasTexto.split(', ').map(s => s.trim()));
  div.innerHTML = cols.map(c => `
    <label><input type="checkbox" value="${c.handle}" ${activas.has(c.handle) ? 'checked' : ''}> ${c.titulo} <span class="b">(${c.productos} productos, /${c.handle})</span></label>
  `).join('');
  div.dataset.loaded = 'true';
}

async function guardarSeleccion(cadena) {
  const div = document.querySelector(`.cadena-caja[data-cadena="${cadena}"] .colecciones`);
  const handles = Array.from(div.querySelectorAll('input:checked')).map(i => i.value);
  await fetch('/api/guardar_colecciones', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cadena, handles}),
  });
  alert(`Guardado: ${cadena} ahora scrapea [${handles.join(', ')}]`);
  location.reload();
}

async function correr(cadenas) {
  const r = await fetch('/api/correr', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cadenas}),
  });
  if (!r.ok) { alert((await r.json()).error); return; }
  poll();
}

async function correrMatching() {
  const val = document.getElementById('max-llm-input').value.trim();
  const r = await fetch('/api/matching/correr', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({max_llm: val || null}),
  });
  if (!r.ok) { alert((await r.json()).error); return; }
  poll();
}

async function reintentar(cadena) {
  const r = await fetch('/api/reintentar', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cadena}),
  });
  if (!r.ok) { alert((await r.json()).error); return; }
  poll();
}

function cambiarFiltro(f) {
  filtroHistorial = f;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('activo', t.dataset.filtro === f));
  renderHistorial();
}

async function poll() {
  const r = await fetch('/api/estado');
  const e = await r.json();
  document.getElementById('log').textContent = e.log.join('\\n') || 'Esperando...';
  document.getElementById('log').scrollTop = 999999;
  document.getElementById('btn-todo').disabled = e.corriendo;
  document.getElementById('btn-matching').disabled = e.corriendo;
  const franja = document.getElementById('franja-estado');
  franja.classList.toggle('activa', e.corriendo);
  document.getElementById('franja-texto').textContent = e.corriendo ? 'Corriendo ahora...' : 'Inactivo — listo para correr';
  if (e.corriendo) setTimeout(poll, 1000);
}

function fmtFecha(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('es-BO', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'});
}

const ICONO_ESTADO = {ok: '✅', ok_con_errores: '⚠️', error: '❌', en_progreso: '⏳'};

async function pollMatching() {
  try {
    const r = await fetch('/api/matching/estado');
    const e = await r.json();
    if (!e.existe) {
      document.getElementById('matching-vacio').style.display = 'block';
      document.getElementById('matching-info').style.display = 'none';
    } else {
      document.getElementById('matching-vacio').style.display = 'none';
      document.getElementById('matching-info').style.display = 'block';
      const d = e.detalle || {};
      const pct = d.porcentaje != null ? d.porcentaje : (e.estado === 'ok' ? 100 : 0);
      const barra = document.getElementById('matching-barra');
      barra.style.width = pct + '%';
      barra.textContent = pct + '%';
      barra.className = 'barra-relleno' + (e.estado === 'ok' ? ' ok' : e.estado === 'error' ? ' error' : '');
      document.getElementById('m-estado').textContent = (ICONO_ESTADO[e.estado] || '') + ' ' + (e.estado === 'en_progreso' ? 'corriendo' : e.estado);
      document.getElementById('m-candidatos').textContent = (d.candidatos_procesados ?? '—') + ' / ' + (d.candidatos_totales ?? '—');
      document.getElementById('m-fusiones').textContent = d.fusiones ?? '—';
      document.getElementById('m-llm').textContent = d.llm_usados ?? '—';
      document.getElementById('m-eta').textContent = d.eta_segundos ? Math.round(d.eta_segundos / 60) + ' min' : (e.estado === 'ok' ? '—' : 'calculando...');
      const catWrap = document.getElementById('m-categoria-wrap');
      if (d.categoria_actual) {
        catWrap.style.display = 'inline-flex';
        document.getElementById('m-categoria').textContent = `${d.categoria_actual} (categoría ${d.bucket_actual}/${d.buckets_totales})`;
      } else {
        catWrap.style.display = 'none';
      }
    }
  } catch (e) { /* silencioso -- se reintenta solo en el próximo poll */ }
  setTimeout(pollMatching, 4000);
}

function renderHistorial() {
  const tbody = document.querySelector('#tabla-historial tbody');
  const filas = filtroHistorial === 'error' ? ultimoHistorial.filter(f => f.estado === 'error') : ultimoHistorial;
  if (!filas.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="vacio">${filtroHistorial === 'error' ? 'Ninguna corrida falló — todo en orden.' : 'Todavía no hay corridas registradas.'}</td></tr>`;
    return;
  }
  tbody.innerHTML = filas.map(f => `
    <tr>
      <td>${fmtFecha(f.iniciado_en)}</td>
      <td>${f.tipo}</td>
      <td class="celda-cadena">${f.cadena || '—'}</td>
      <td>${f.ruta || '—'}${f.error_detalle ? `<div class="celda-ruta">⚠ ${f.error_detalle.slice(0,140)}</div>` : ''}</td>
      <td><span class="pill ${f.estado}">${ICONO_ESTADO[f.estado] || ''} ${f.estado}</span></td>
      <td>${f.procesados ?? '—'}${f.errores ? ` (${f.errores} err)` : ''}</td>
      <td>${f.tipo === 'scraper' && f.estado === 'error' && f.cadena ? `<button class="chico secundario" onclick="reintentar('${f.cadena}')">↻ Reintentar</button>` : ''}</td>
    </tr>
  `).join('');
}

async function pollHistorial() {
  try {
    const r = await fetch('/api/historial');
    ultimoHistorial = await r.json();
    renderHistorial();
  } catch (e) { /* silencioso */ }
  setTimeout(pollHistorial, 8000);
}

poll();
pollMatching();
pollHistorial();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(port=5050, debug=False)

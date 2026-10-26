"""
panel.py — Panel de operación de Precios Claros (local).

Para operar el pipeline sin abrir la terminal ni saber Python/SQL:

- Nueva corrida: elegís QUÉ correr y SOBRE QUÉ cadenas.
    · Pipeline completo   scraping + carga + clasificar + matching + export + fotos + publicar
    · Solo scraping       trae precios y los guarda en la base (no matchea ni publica)
    · Solo matching       clasifica y matchea lo que ya está en la base
    · Solo publicar       copia fotos y sube a la web lo que ya está matcheado
  Todo lo corre run_all.py (con --pasos y --solo), igual que desde la terminal.
- Corrida en curso: paso actual, cadena y categoría (i/n), tiempo, ETA,
  productos cargados, errores, log en vivo y botón para detener.
- Frescura por cadena: cuántos productos activos tiene y hace cuánto se
  actualizaron sus precios.
- Categorías por cadena: descubrir las colecciones Shopify y elegir cuáles
  scrapear (se guardan en config_cadenas.json para las próximas corridas).
- Historial de corridas (agent_runs) con filtro y reintento de las fallidas.

Corre SOLO en tu máquina (no tiene login, no exponerlo a internet).

Uso (desde postgres_migration/etl, con la venv activada):
    pip install flask
    python panel.py
    abrí http://localhost:5050
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path

import psycopg2.extras
from flask import Flask, Response, jsonify, request

import candados
import etl
import scraper_shopify

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config_cadenas.json"
# Mientras corre una corrida con solo algunas categorías, config_cadenas.json
# queda filtrado (run_all.py no recibe categorías sueltas) y el original vive acá.
CONFIG_RESPALDO = BASE_DIR / "config_cadenas.respaldo.json"
ZONA_HORARIA = os.environ.get("PRECIOS_TZ", "America/La_Paz")  # para "esta semana" (desde el lunes)

app = Flask(__name__)

# Qué pasos de run_all.py corre cada modo del panel (None = todos).
MODOS = {
    "completo": {"pasos": None, "usa_cadenas": True},
    "scraping": {"pasos": ["scraping"], "usa_cadenas": True},
    "matching": {"pasos": ["clasificar", "matching"], "usa_cadenas": False},
    "publicar": {"pasos": ["fotos", "publicar"], "usa_cadenas": False},
    # "Publicar ahora" después de unir/separar a mano: solo rearma la web, sin
    # copiar fotos pendientes (eso puede tardar mucho y no hace falta para el cambio).
    "web": {"pasos": ["publicar"], "usa_cadenas": False},
}
PASOS = ["scraping", "clasificar", "matching", "export", "fotos", "publicar"]
NOMBRE_PASO = {"Scraping y carga": "scraping", "Clasificar": "clasificar", "Matching": "matching",
               "Exportar precios.json": "export", "Fotos": "fotos", "Publicar web": "publicar"}


def _progreso_vacio() -> dict:
    return {"modo": None, "plan": None, "fase": None, "cadena": None, "etiqueta": None, "i": 0, "n": 0,
            "inicio": None, "fin": None, "codigo": None, "detenido": False,
            "cargas": 0, "productos": 0, "errores": 0, "errores_productos": 0,
            "seg_por_categoria": None, "t_categoria": None}


# Estado de la corrida, en memoria (un solo usuario, un solo proceso).
estado = {"corriendo": False, "log": [], "progreso": _progreso_vacio()}
_proc = None
_duraciones: list[float] = []
# Sin este lock, dos clics casi simultáneos podían lanzar dos run_all.py a la
# vez (el chequeo y el marcado de "corriendo" no eran una sola operación).
_estado_lock = threading.Lock()


def intentar_iniciar_corrida() -> bool:
    with _estado_lock:
        if estado["corriendo"]:
            return False
        estado["corriendo"] = True
        return True


def cargar_config() -> dict:
    # Si hay una corrida con categorías elegidas, la config real es el respaldo.
    ruta = CONFIG_RESPALDO if CONFIG_RESPALDO.exists() else CONFIG_PATH
    return json.loads(ruta.read_text(encoding="utf-8"))


def guardar_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _handle(x) -> str:
    return x["handle"] if isinstance(x, dict) else x


def _nombre_url(url: str) -> str:
    """'…/categoria/abarrotes/aderezos' -> 'abarrotes / aderezos'."""
    partes = [p for p in url.split("?")[0].rstrip("/").split("/")[3:] if p and p != "categoria"]
    return " / ".join(partes[-2:]) or url


def categorias_de(c: dict, tipo: str) -> list[dict] | None:
    """Categorías (Shopify) o URLs (navegador) de una cadena, para elegirlas una
    por una. None = 'todas' (auto-descubrimiento: no hay lista fija)."""
    if tipo == "shopify":
        cols = c.get("colecciones")
        if not isinstance(cols, list):
            return None
        return [{"id": _handle(x), "nombre": (x.get("subcategoria") or x["handle"]) if isinstance(x, dict) else x,
                 "grupo": (x.get("categoria") or "") if isinstance(x, dict) else ""} for x in cols]
    return [{"id": u, "nombre": _nombre_url(u), "grupo": ""} for u in c.get("urls", [])]


def cadenas_config(cfg: dict) -> list[dict]:
    """Cadenas que corre run_all.py, con cuántas categorías/URLs tiene cada una."""
    out = []
    for c in cfg.get("shopify", []):
        cols = c.get("colecciones")
        out.append({"cadena": c["cadena"], "tipo": "shopify", "sitio_base": c.get("sitio_base"),
                    "n": len(cols) if isinstance(cols, list) else None,
                    "colecciones": [_handle(x) for x in cols] if isinstance(cols, list) else cols,
                    "categorias": categorias_de(c, "shopify")})
    for c in cfg.get("navegador", []):
        out.append({"cadena": c["cadena"], "tipo": "navegador", "n": len(c.get("urls", [])),
                    "categorias": categorias_de(c, "navegador")})
    return out


def config_filtrada(cfg: dict, categorias: dict[str, list[str]]) -> dict:
    """Copia de la config dejando, en las cadenas de `categorias`, solo esas
    categorías/URLs (en el mismo orden que la config)."""
    cfg = json.loads(json.dumps(cfg))
    for c in cfg.get("shopify", []):
        if c["cadena"] in categorias and isinstance(c.get("colecciones"), list):
            ids = set(categorias[c["cadena"]])
            c["colecciones"] = [x for x in c["colecciones"] if _handle(x) in ids]
    for c in cfg.get("navegador", []):
        if c["cadena"] in categorias:
            ids = set(categorias[c["cadena"]])
            c["urls"] = [u for u in c.get("urls", []) if u in ids]
    return cfg


def restaurar_config():
    """Vuelve a poner la config original si quedó filtrada por una corrida con
    categorías elegidas (al terminar, o al abrir el panel si se cortó de golpe)."""
    if CONFIG_RESPALDO.exists():
        CONFIG_PATH.write_text(CONFIG_RESPALDO.read_text(encoding="utf-8"), encoding="utf-8")
        CONFIG_RESPALDO.unlink()


restaurar_config()


# ---------------------------------------------------------------------------
# Log en vivo + lectura del avance a partir de las líneas de run_all.py
# ---------------------------------------------------------------------------
RE_PASO = re.compile(r"== Paso: (.+?) ==")
RE_SCRAP = re.compile(r"Scrapeando (.+?)/(.+?) \[(\d+)/(\d+)\]")
RE_CARGA = re.compile(r"cargar-resultado \((.+?)\): (\d+) productos cargados, (\d+) errores")


def _leer_avance(msg: str):
    p = estado["progreso"]
    m = RE_PASO.search(msg)
    if m:
        p["fase"] = NOMBRE_PASO.get(m.group(1), p["fase"])
        return
    m = RE_SCRAP.search(msg)
    if m:
        ahora = time.time()
        if p["t_categoria"] is not None:
            _duraciones.append(ahora - p["t_categoria"])
            del _duraciones[:-40]
            p["seg_por_categoria"] = round(sum(_duraciones) / len(_duraciones), 1)
        p.update(fase="scraping", cadena=m.group(1), etiqueta=m.group(2), i=int(m.group(3)), n=int(m.group(4)),
                 t_categoria=ahora)
        return
    m = RE_CARGA.search(msg)
    if m:
        p["cargas"] += 1
        p["productos"] += int(m.group(2))
        p["errores_productos"] += int(m.group(3))
        return
    if re.search(r"\bERROR\b|Traceback", msg):
        p["errores"] += 1


def log(msg: str):
    print(msg, flush=True)
    estado["log"].append(msg)
    estado["log"] = estado["log"][-600:]  # no crecer sin límite
    try:
        _leer_avance(msg)
    except Exception:
        pass


def correr_comando(cmd: list[str], token: str):
    """Corre run_all.py como subproceso (un error de un sitio no tumba el
    panel) y va pasando su salida al log en vivo. `token` identifica los
    candados compartidos de esta corrida (para poder liberarlos al detenerla)."""
    global _proc
    try:
        log(f"$ {' '.join(cmd[1:])}")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PRECIOS_CANDADO_TOKEN=token)
        _proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        for line in _proc.stdout:
            log(line.rstrip())
        _proc.wait()
        estado["progreso"]["codigo"] = _proc.returncode
        if estado["progreso"]["detenido"]:
            log("--- Detenida por el usuario ---")
        else:
            log(f"--- Terminado (código {_proc.returncode}) ---")
    except Exception as e:
        log(f"ERROR fatal: {e}")
        estado["progreso"]["codigo"] = -1
    finally:
        try:
            restaurar_config()
        except Exception as e:
            log(f"ERROR al restaurar config_cadenas.json (el original está en {CONFIG_RESPALDO.name}): {e}")
        estado["progreso"]["fin"] = time.time()
        _proc = None
        estado["corriendo"] = False


def lanzar(modo: str, cadenas: list[str] | None = None, max_llm=None, limite=None, sin_export=False, sin_fotos=False,
           categorias: dict | None = None):
    """Arma el comando de run_all.py para el modo pedido y lo lanza en un
    hilo. Quien llama ya ganó el turno con intentar_iniciar_corrida().
    `categorias` = {cadena: [handles o URLs]} para scrapear solo esas."""
    cfg_modo = MODOS[modo]
    cfg = cargar_config()
    disponibles = cadenas_config(cfg)
    cmd = [sys.executable, "-u", "run_all.py"]
    pasos = list(cfg_modo["pasos"] or PASOS)
    if sin_export and "export" in pasos:
        pasos.remove("export")
    if sin_fotos and "fotos" in pasos:
        pasos.remove("fotos")
    if cfg_modo["pasos"] is not None or sin_export or sin_fotos:
        cmd += ["--pasos", ",".join(pasos)]
    elegidas = []
    if cfg_modo["usa_cadenas"]:
        nombres = {c["cadena"] for c in disponibles}
        elegidas = [c for c in (cadenas or []) if c in nombres]
        if elegidas and len(elegidas) < len(nombres):
            cmd += ["--solo", ",".join(elegidas)]
        if not elegidas:
            elegidas = sorted(nombres, key=[c["cadena"] for c in disponibles].index)
    if max_llm is not None and "matching" in pasos:
        cmd += ["--max-llm", str(max_llm)]
    if limite is not None and "scraping" in pasos:
        cmd += ["--limite", str(limite)]

    # Solo algunas categorías: run_all.py lee config_cadenas.json, así que se le
    # deja una copia filtrada durante la corrida (la original queda en el respaldo).
    parciales = {}
    if categorias and "scraping" in pasos:
        for c in disponibles:
            if c["cadena"] not in elegidas or c["cadena"] not in categorias or c["categorias"] is None:
                continue
            todas = [x["id"] for x in c["categorias"]]
            ids = [i for i in categorias[c["cadena"]] if i in set(todas)]
            if not ids:
                raise ValueError(f"{c['cadena']}: elegí al menos una categoría")
            if len(ids) < len(todas):
                parciales[c["cadena"]] = ids
    if parciales:
        if not CONFIG_RESPALDO.exists():
            CONFIG_RESPALDO.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        guardar_config(config_filtrada(cfg, parciales))

    plan_cadenas = []
    for c in disponibles:
        if c["cadena"] in elegidas and "scraping" in pasos:
            n = len(parciales[c["cadena"]]) if c["cadena"] in parciales else c["n"]
            if n is not None and limite is not None:
                n = min(n, limite)
            plan_cadenas.append({"cadena": c["cadena"], "tipo": c["tipo"], "n": n})
    _duraciones.clear()
    estado["log"] = []
    estado["progreso"] = _progreso_vacio()
    estado["progreso"].update(modo=modo, inicio=time.time(), plan={"pasos": pasos, "cadenas": plan_cadenas,
                                                                  "categorias_parciales": {k: len(v) for k, v in parciales.items()}})
    estado["token"] = uuid.uuid4().hex
    threading.Thread(target=correr_comando, args=(cmd, estado["token"]), daemon=True).start()


def _entero_opcional(v, nombre):
    if v in (None, ""):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{nombre} tiene que ser un número")
    if n < 0:
        raise ValueError(f"{nombre} no puede ser negativo")
    return n


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    datos = {"cadenas": cadenas_config(cargar_config()), "pasos": PASOS}
    html = HTML.replace("__DATOS__", json.dumps(datos, ensure_ascii=False).replace("</", "<\\/"))
    return Response(html, mimetype="text/html")


@app.route("/api/correr", methods=["POST"])
def api_correr():
    data = request.get_json(silent=True) or {}
    modo = data.get("modo") or "completo"
    if modo not in MODOS:
        return jsonify({"error": f"modo desconocido: {modo}"}), 400
    try:
        max_llm = _entero_opcional(data.get("max_llm"), "El límite de consultas IA")
        limite = _entero_opcional(data.get("limite"), "La prueba rápida")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if limite == 0:
        return jsonify({"error": "La prueba rápida tiene que ser de 1 categoría o más"}), 400
    categorias = data.get("categorias")
    if categorias is not None and not (isinstance(categorias, dict) and
                                       all(isinstance(v, list) for v in categorias.values())):
        return jsonify({"error": "categorias tiene que ser {cadena: [categorías]}"}), 400
    if not intentar_iniciar_corrida():
        return jsonify({"error": "Ya hay una corrida en curso"}), 409
    try:
        lanzar(modo, data.get("cadenas"), max_llm, limite, bool(data.get("sin_export")), bool(data.get("sin_fotos")),
               categorias)
    except ValueError as e:
        restaurar_config()
        estado["corriendo"] = False
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        restaurar_config()
        estado["corriendo"] = False
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/detener", methods=["POST"])
def api_detener():
    """Detiene la corrida. Lo ya guardado queda guardado; la categoría que se
    estaba cargando no queda a medias (se confirma toda junta al final)."""
    proc = _proc
    if not estado["corriendo"] or proc is None:
        return jsonify({"error": "No hay ninguna corrida en curso"}), 409
    estado["progreso"]["detenido"] = True
    try:
        proc.terminate()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Un proceso cortado de golpe no alcanza a soltar sus candados: se sueltan acá
    # (si no, vencerían solos a los 3 minutos).
    if estado.get("token"):
        threading.Thread(target=candados.soltar_de, args=(estado["token"],), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/candados")
def api_candados():
    """Qué está corriendo en este momento en cualquier computadora (tabla candados)."""
    try:
        filas = candados.activos()
    except Exception as e:
        return jsonify({"equipo": candados.equipo(), "candados": [], "error": str(e)})
    mio = estado.get("token") if estado["corriendo"] else None
    for f in filas:
        f["mi_corrida"] = bool(mio) and f["dueno"] == mio
        f.pop("dueno", None)
    return jsonify({"equipo": candados.equipo(), "candados": filas})


def _matching_en_curso():
    """Si alguna computadora está en el paso de matching, devuelve su candado."""
    try:
        return next((c for c in candados.activos() if c.get("paso") == "matching"), None)
    except Exception:
        return None


def _bloqueo_matching():
    c = _matching_en_curso()
    if not c:
        return None
    quien = "esta computadora" if c.get("este_equipo") else c["equipo"]
    return jsonify({"error": f"Hay un matching corriendo en {quien}. Esperá a que termine para unir o separar a mano "
                             f"(si no, el matching podría deshacer tu cambio)."}), 409


@app.route("/api/estado")
def api_estado():
    desde = request.args.get("desde", type=int) or 0
    total = len(estado["log"])
    return jsonify({"corriendo": estado["corriendo"], "progreso": estado["progreso"],
                    "ahora": time.time(), "log_total": total, "log": estado["log"][desde:] if desde <= total else estado["log"]})


LIMITE_DB_MB = float(os.environ.get("SUPABASE_LIMITE_DB_MB", 500))         # plan gratis
LIMITE_STORAGE_MB = float(os.environ.get("SUPABASE_LIMITE_STORAGE_MB", 1024))
_uso_cache = {"t": 0, "datos": None}


@app.route("/api/uso")
def api_uso():
    """Cuánto de los límites del plan gratis de Supabase se está usando
    (base de datos y Storage). La transferencia (egress) no se puede leer
    desde SQL: se ve en el panel de Supabase -> Usage. Cache de 5 min."""
    if _uso_cache["datos"] and time.time() - _uso_cache["t"] < 300 and not request.args.get("forzar"):
        return jsonify(_uso_cache["datos"])
    datos = {"db_mb": None, "storage_mb": None, "limite_db_mb": LIMITE_DB_MB,
             "limite_storage_mb": LIMITE_STORAGE_MB, "tablas": []}
    try:
        conn = etl.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT pg_database_size(current_database())")
        datos["db_mb"] = round(float(cur.fetchone()[0]) / 1048576, 1)
        cur.execute("""
            SELECT c.relname, pg_total_relation_size(c.oid)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY 2 DESC LIMIT 4
        """)
        datos["tablas"] = [{"tabla": t, "mb": round(b / 1048576, 1)} for t, b in cur.fetchall()]
        try:
            cur.execute("SELECT coalesce(sum((metadata->>'size')::bigint), 0) FROM storage.objects")
            datos["storage_mb"] = round(float(cur.fetchone()[0]) / 1048576, 1)
        except Exception:
            conn.rollback()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    _uso_cache.update(t=time.time(), datos=datos)
    return jsonify(datos)


_resumen_cache = {"t": 0, "datos": None}


def _sin_esquema(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower()).split("?")[0].split("#")[0].rstrip("/")


def _categoria_hecha(tipo: str, cid: str, rutas: list[str]) -> bool:
    """¿Alguna ruta de agent_runs (fuente_metodo) corresponde a esta categoría?
    Shopify: el handle es un tramo de la ruta (…/collections/<handle>/…, 'shopify:<handle>'
    o el handle solo). Navegador: la misma URL (sin http/www, query ni / final)."""
    if tipo == "shopify":
        h = cid.lower()
        return any(h in re.split(r"[/:?#\s]+", r.lower()) for r in rutas)
    u = _sin_esquema(cid)
    return any(_sin_esquema(r) == u for r in rutas)


def _categorias_semana(cur) -> dict:
    """Por cadena: qué categorías de config_cadenas.json ya tienen un scraping
    exitoso esta semana (desde el lunes 00:00 hora de Bolivia) y cuáles faltan."""
    cur.execute("""
        SELECT c.nombre AS cadena, ar.fuente_metodo AS ruta
        FROM agent_runs ar JOIN cadenas c ON c.id = ar.cadena_id
        WHERE ar.agente_tipo = 'scraper' AND ar.estado IN ('ok', 'ok_con_errores') AND ar.fuente_metodo IS NOT NULL
          AND ar.iniciado_en >= date_trunc('week', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s
    """, {"tz": ZONA_HORARIA})
    rutas = {}
    for r in cur.fetchall():
        rutas.setdefault(r["cadena"], []).append(r["ruta"])
    out = {}
    for c in cadenas_config(cargar_config()):
        if c["categorias"] is None:  # 'todas' (auto): no hay lista fija contra la cual comparar
            continue
        rs = rutas.get(c["cadena"], [])
        hechas = [x["id"] for x in c["categorias"] if _categoria_hecha(c["tipo"], x["id"], rs)]
        faltan = [x["id"] for x in c["categorias"] if x["id"] not in set(hechas)]
        # Si hubo corridas esta semana pero ninguna ruta coincide con la config,
        # no se puede afirmar que falta todo (puede ser otro formato de ruta).
        out[c["cadena"]] = {"total": len(c["categorias"]), "hechas": len(hechas), "faltan": faltan,
                            "corridas": len(rs), "reconocidas": bool(hechas) or not rs}
    return out


def _iso(v):
    return v.isoformat() if v else None


@app.route("/api/resumen")
def api_resumen():
    """Estado de cada cadena y del catálogo, para la parte de arriba del panel:
    - por cadena: productos activos, último scraping exitoso, última captura de
      precio, errores recientes, categorías que faltan scrapear esta semana, productos NUEVOS desde el último matching (lo que
      falta matchear), cambios de precio y productos nuevos desde la última
      publicación (lo que falta publicar) y su portafolio según en cuántas cadenas
      está cada producto;
    - catálogo: productos por cantidad de cadenas (1, 2, 3…), última publicación
      de la web y último matching. Cache de 60 s."""
    if _resumen_cache["datos"] and time.time() - _resumen_cache["t"] < 60 and not request.args.get("forzar"):
        return jsonify(_resumen_cache["datos"])
    datos = {"cadenas": [], "cobertura": {}, "total_productos": 0, "web_actualizada": None,
             "ultimo_matching": None, "total_cadenas": 0}
    try:
        conn = etl.get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        web = match = None
        try:
            cur.execute("SELECT actualizado_en FROM web_resumen WHERE id = 1")
            fila = cur.fetchone()
            web = fila["actualizado_en"] if fila else None
        except Exception:
            conn.rollback()
        cur.execute("SELECT max(finalizado_en) AS f FROM agent_runs WHERE agente_tipo = 'matching' AND estado = 'ok'")
        match = cur.fetchone()["f"]
        datos["web_actualizada"], datos["ultimo_matching"] = _iso(web), _iso(match)

        cadenas = {}
        cur.execute("""
            SELECT c.nombre AS cadena, count(l.id) AS productos, max(p.capturado_en) AS ultima_captura,
                   count(l.id) FILTER (WHERE l.creado_en > coalesce(%(match)s::timestamptz, '-infinity')) AS nuevos_sin_matchear,
                   count(l.id) FILTER (WHERE l.creado_en > coalesce(%(web)s::timestamptz, '-infinity')) AS nuevos_sin_publicar
            FROM cadenas c
            JOIN listados l ON l.cadena_id = c.id AND l.activo
            LEFT JOIN precios p ON p.listado_id = l.id
            GROUP BY c.nombre
        """, {"match": match, "web": web})
        for r in cur.fetchall():
            cadenas[r["cadena"]] = {"cadena": r["cadena"], "productos": r["productos"],
                                    "ultima_captura": _iso(r["ultima_captura"]),
                                    "nuevos_sin_matchear": r["nuevos_sin_matchear"] if match else r["productos"],
                                    "cambios_sin_publicar": r["nuevos_sin_publicar"], "cobertura": {},
                                    "ultimo_scraping": None, "errores_recientes": 0}
        # precios que CAMBIARON después de la última publicación (historial solo guarda cambios)
        cur.execute("""
            SELECT c.nombre AS cadena, count(*) AS n
            FROM historial_precios h JOIN listados l ON l.id = h.listado_id AND l.activo
            JOIN cadenas c ON c.id = l.cadena_id
            WHERE h.capturado_en > coalesce(%s::timestamptz, '-infinity') AND l.creado_en <= h.capturado_en - interval '1 minute'
            GROUP BY c.nombre
        """, (web,))
        for r in cur.fetchall():
            if r["cadena"] in cadenas:
                cadenas[r["cadena"]]["cambios_sin_publicar"] += r["n"]
        cur.execute("""
            SELECT c.nombre AS cadena,
                   max(ar.finalizado_en) FILTER (WHERE ar.estado IN ('ok', 'ok_con_errores')) AS ultimo,
                   count(*) FILTER (WHERE ar.estado = 'error' AND ar.iniciado_en > now() - interval '2 days') AS errores
            FROM agent_runs ar JOIN cadenas c ON c.id = ar.cadena_id
            WHERE ar.agente_tipo = 'scraper'
            GROUP BY c.nombre
        """)
        for r in cur.fetchall():
            if r["cadena"] in cadenas:
                cadenas[r["cadena"]].update(ultimo_scraping=_iso(r["ultimo"]), errores_recientes=r["errores"])
        for nombre, sem in _categorias_semana(cur).items():
            if nombre in cadenas:
                cadenas[nombre]["semana"] = sem
        # portafolio: cada listado activo, según en cuántas cadenas está su producto
        cur.execute("""
            WITH prod AS (SELECT producto_id, count(DISTINCT cadena_id) AS n FROM listados WHERE activo GROUP BY producto_id)
            SELECT c.nombre AS cadena, prod.n, count(*) AS cant
            FROM listados l JOIN prod ON prod.producto_id = l.producto_id JOIN cadenas c ON c.id = l.cadena_id
            WHERE l.activo GROUP BY c.nombre, prod.n
        """)
        for r in cur.fetchall():
            if r["cadena"] in cadenas:
                cadenas[r["cadena"]]["cobertura"][str(r["n"])] = r["cant"]
        cur.execute("""
            WITH prod AS (SELECT producto_id, count(DISTINCT cadena_id) AS n FROM listados WHERE activo GROUP BY producto_id)
            SELECT n, count(*) AS cant FROM prod GROUP BY n ORDER BY n
        """)
        datos["cobertura"] = {str(r["n"]): r["cant"] for r in cur.fetchall()}
        datos["total_productos"] = sum(datos["cobertura"].values())
        datos["total_cadenas"] = len(cadenas)
        datos["cadenas"] = sorted(cadenas.values(), key=lambda c: c["cadena"])
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    _resumen_cache.update(t=time.time(), datos=datos)
    return jsonify(datos)


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
    if CONFIG_RESPALDO.exists():
        return jsonify({"error": "Hay una corrida con categorías elegidas en curso. Guardá la selección cuando termine."}), 409
    cfg = cargar_config()
    # Si "cadena" no coincide con ninguna entrada real, avisar en vez de
    # devolver ok sin haber guardado nada.
    encontrada = False
    for chain_cfg in cfg["shopify"]:
        if chain_cfg["cadena"] == cadena:
            viejas = chain_cfg.get("colecciones")
            # Conservar categoria/subcategoria de las que ya estaban configuradas
            por_handle = {x["handle"]: x for x in viejas if isinstance(x, dict)} if isinstance(viejas, list) else {}
            chain_cfg["colecciones"] = [por_handle.get(h, h) for h in handles]
            encontrada = True
    if not encontrada:
        return jsonify({"error": f"cadena '{cadena}' no encontrada en config_cadenas.json"}), 404
    guardar_config(cfg)
    return jsonify({"ok": True, "n": len(handles)})


@app.route("/api/matching/estado")
def api_matching_estado():
    """Última corrida de matching (en progreso o terminada). etl.matchear()
    guarda su avance en agent_runs mientras corre."""
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
    """Últimas corridas de cualquier tipo (scraper, matching, carga manual)."""
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
            LIMIT 80
            """
        )
        filas = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    def fmt(f):
        detalle = f["detalle"] or {}
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
    """Reintenta la cadena ENTERA de una ruta que falló (run_all.py no puede
    re-scrapear una sola categoría suelta)."""
    data = request.get_json(silent=True) or {}
    cadena = data.get("cadena")
    if not cadena:
        return jsonify({"error": "falta cadena"}), 400
    if not intentar_iniciar_corrida():
        return jsonify({"error": "Ya hay una corrida en curso"}), 409
    lanzar("completo", [cadena])
    return jsonify({"ok": True})


@app.route("/api/matching/correr", methods=["POST"])
def api_matching_correr():
    """Compatibilidad con la versión anterior del panel: equivale a modo 'matching'."""
    data = request.get_json(silent=True) or {}
    try:
        max_llm = _entero_opcional(data.get("max_llm"), "El límite de consultas IA")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not intentar_iniciar_corrida():
        return jsonify({"error": "Ya hay una corrida en curso"}), 409
    lanzar("matching", None, max_llm)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Unir / separar productos a mano
# ---------------------------------------------------------------------------
# Un "producto" (tabla productos) es lo que la web muestra como UNA tarjeta;
# cada cadena aporta un "listado" que apunta a ese producto. Unir = hacer que
# los listados de varios productos apunten a uno solo. Separar = sacar un
# listado a un producto propio y dejar anotado que el matching automático no
# los vuelva a unir (match_revisiones_llm, decision=false, razon 'manual…').
SIN_ACENTO = "translate(lower({}), 'áéíóúüñ', 'aeiouun')"


def _sin_acentos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", t.lower()) if not unicodedata.combining(c))


def _num(v):
    return float(v) if v is not None else None


def _fotos_para_panel(cur, imagenes) -> dict:
    """Misma lógica de fotos que la web (publicar_web): Hipermaxi guarda sus fotos
    detrás de su proxy (/_next/image?url=…), que fuera de su sitio no funciona.
    Se usa la copia propia en Supabase Storage si existe; si no, la URL real."""
    import publicar_web
    base = getattr(publicar_web, "SUPABASE_URL", None) or os.environ.get("SUPABASE_URL", "https://dxdmstnkgqpndbbqkwct.supabase.co")
    origen = {img: publicar_web.origen_img(img) for img in set(i for i in imagenes if i)}
    copias = {}
    urls = [u for u in origen.values() if u]
    if urls:
        cur.execute("SAVEPOINT fotos")
        try:
            cur.execute("SELECT url_origen, ruta FROM foto_espejo WHERE ok AND url_origen = ANY(%s)", (urls,))
            copias = {r["url_origen"]: r["ruta"] for r in cur.fetchall()}
            cur.execute("RELEASE SAVEPOINT fotos")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT fotos")  # todavía no existe foto_espejo
    salida = {}
    for img, u in origen.items():
        if u and u in copias:
            salida[img] = f"{base.rstrip('/')}/storage/v1/object/public/fotos/{copias[u]}_s.webp"
        elif u and u.lower().startswith("http"):
            salida[img] = u
    return salida


@app.route("/api/match/buscar")
def api_match_buscar():
    q = (request.args.get("q") or "").strip()
    palabras = [w for w in re.split(r"\s+", _sin_acentos(q)) if len(w) >= 2][:6]
    if not palabras:
        return jsonify([])
    try:
        conn = etl.get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cond = " AND ".join([SIN_ACENTO.format("l.nombre_original") + " LIKE %s"] * len(palabras))
        cur.execute(f"SELECT DISTINCT l.producto_id::text AS id FROM listados l WHERE l.activo AND {cond} LIMIT 40",
                    [f"%{w}%" for w in palabras])
        ids = [r["id"] for r in cur.fetchall()]
        grupos = {}
        if ids:
            cur.execute("""
                SELECT l.id::text AS listado_id, l.producto_id::text AS producto_id, l.nombre_original, l.imagen,
                       l.url, l.codigo_cadena, l.match_metodo, c.nombre AS cadena,
                       pr.precio_oferta, pr.precio_regular, p.nombre AS producto, p.producto_clave
                FROM listados l
                JOIN cadenas c ON c.id = l.cadena_id
                JOIN productos p ON p.id = l.producto_id
                LEFT JOIN precios pr ON pr.listado_id = l.id
                WHERE l.activo AND l.producto_id = ANY(%s::uuid[])
                ORDER BY c.nombre, l.nombre_original
            """, (ids,))
            filas = cur.fetchall()
            fotos = _fotos_para_panel(cur, [r["imagen"] for r in filas])
            for r in filas:
                g = grupos.setdefault(r["producto_id"], {"producto_id": r["producto_id"], "nombre": r["producto"],
                                                         "clave": r["producto_clave"], "listados": []})
                g["listados"].append({
                    "id": r["listado_id"], "cadena": r["cadena"], "nombre": r["nombre_original"],
                    "imagen": fotos.get(r["imagen"]), "url": r["url"], "codigo": r["codigo_cadena"], "metodo": r["match_metodo"],
                    "precio": _num(r["precio_oferta"]), "precio_regular": _num(r["precio_regular"]),
                })
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    def relevancia(g):
        texto = _sin_acentos(" ".join(x["nombre"] for x in g["listados"]))
        return (-sum(w in texto for w in palabras), -len({x["cadena"] for x in g["listados"]}), g["nombre"])
    return jsonify(sorted(grupos.values(), key=relevancia)[:24])


@app.route("/api/match/unir", methods=["POST"])
def api_match_unir():
    """Une 2+ productos en uno: queda el que ya tiene más cadenas (o el más
    antiguo) y los listados de los demás pasan a apuntar a él."""
    ids = (request.get_json(silent=True) or {}).get("productos") or []
    ids = list(dict.fromkeys(str(i) for i in ids))
    if len(ids) < 2:
        return jsonify({"error": "Elegí al menos dos productos para unir"}), 400
    bloqueo = _bloqueo_matching()
    if bloqueo:
        return bloqueo
    try:
        conn = etl.get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.id::text AS id, p.producto_clave, p.creado_en,
                   count(DISTINCT l.cadena_id) FILTER (WHERE l.activo) AS cadenas
            FROM productos p LEFT JOIN listados l ON l.producto_id = p.id
            WHERE p.id = ANY(%s::uuid[]) GROUP BY p.id
        """, (ids,))
        prods = cur.fetchall()
        if len(prods) < 2:
            return jsonify({"error": "Esos productos ya no existen (¿alguien los unió recién?). Volvé a buscar."}), 409
        prods.sort(key=lambda p: (-p["cadenas"], p["creado_en"]))
        canon, otros = prods[0], prods[1:]
        for o in otros:
            cur.execute("UPDATE listados SET producto_id = %s, match_metodo = 'manual', match_confidence = 1.0 "
                        "WHERE producto_id = %s", (canon["id"], o["id"]))
            # si antes alguien los había separado a mano, ese veto ya no aplica
            cur.execute("DELETE FROM match_revisiones_llm WHERE razon LIKE 'manual%%' AND "
                        "((producto_clave_a = %s AND producto_clave_b = %s) OR (producto_clave_a = %s AND producto_clave_b = %s))",
                        (canon["producto_clave"], o["producto_clave"], o["producto_clave"], canon["producto_clave"]))
            cur.execute("DELETE FROM productos WHERE id = %s", (o["id"],))
        cur.execute("UPDATE listados SET match_metodo = 'manual', match_confidence = 1.0 WHERE producto_id = %s",
                    (canon["id"],))
        quitados = _dejar_uno_por_cadena(cur, canon["id"])
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "producto_id": canon["id"], "unidos": len(prods), "duplicados_quitados": quitados})


def _dejar_uno_por_cadena(cur, producto_id) -> int:
    """Si un producto quedó con dos (o más) listados ACTIVOS de la misma cadena,
    deja activo solo el más reciente y desactiva los otros (no se borran, quedan
    para auditar). Mismo criterio que etl.desactivar_duplicados_por_cadena(),
    que además lo repite al final de cada matching por si un scraping los reactiva."""
    cur.execute("""
        UPDATE listados SET activo = FALSE, actualizado_en = now()
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY cadena_id ORDER BY actualizado_en DESC, id DESC) AS rn
                FROM listados WHERE producto_id = %s::uuid AND activo
            ) x WHERE rn > 1
        )
    """, (producto_id,))
    return cur.rowcount


@app.route("/api/match/dejar_uno", methods=["POST"])
def api_match_dejar_uno():
    """Botón 'Dejar uno' de una tarjeta que tiene la misma cadena repetida."""
    producto_id = (request.get_json(silent=True) or {}).get("producto_id")
    if not producto_id:
        return jsonify({"error": "falta producto_id"}), 400
    try:
        conn = etl.get_conn()
        cur = conn.cursor()
        n = _dejar_uno_por_cadena(cur, producto_id)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "quitados": n})


@app.route("/api/match/separar", methods=["POST"])
def api_match_separar():
    """Saca un listado de su grupo a un producto propio y deja anotado que el
    matching automático no los vuelva a unir."""
    listado_id = (request.get_json(silent=True) or {}).get("listado_id")
    if not listado_id:
        return jsonify({"error": "falta listado_id"}), 400
    bloqueo = _bloqueo_matching()
    if bloqueo:
        return bloqueo
    try:
        conn = etl.get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT l.id::text AS id, l.producto_id::text AS producto_id, l.codigo_cadena, l.nombre_original,
                   c.nombre AS cadena, p.producto_clave, p.nombre AS producto, p.marca, p.categoria, p.subcategoria,
                   (SELECT count(*) FROM listados x WHERE x.producto_id = l.producto_id AND x.activo) AS n
            FROM listados l JOIN cadenas c ON c.id = l.cadena_id JOIN productos p ON p.id = l.producto_id
            WHERE l.id = %s::uuid
        """, (listado_id,))
        l = cur.fetchone()
        if not l:
            return jsonify({"error": "Ese producto ya no existe. Volvé a buscar."}), 404
        if l["n"] < 2:
            return jsonify({"error": "Ese producto ya está solo, no hay nada que separar."}), 409
        clave = f"{etl.slugify(l['cadena'])}-{l['codigo_cadena']}"
        cur.execute("SELECT 1 FROM productos WHERE producto_clave = %s", (clave,))
        if cur.fetchone():
            clave = f"{clave}-sep-{uuid.uuid4().hex[:6]}"
        cur.execute("INSERT INTO productos (producto_clave, nombre, marca, categoria, subcategoria) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id::text AS id",
                    (clave, l["nombre_original"], l["marca"], l["categoria"], l["subcategoria"]))
        nuevo = cur.fetchone()["id"]
        cur.execute("UPDATE listados SET producto_id = %s, match_metodo = 'manual', match_confidence = 1.0 WHERE id = %s",
                    (nuevo, l["id"]))
        cur.execute("""
            INSERT INTO match_revisiones_llm (producto_clave_a, producto_clave_b, nombre_a, nombre_b,
                                              decision, confianza, razon)
            VALUES (%s, %s, %s, %s, FALSE, 1.0, 'manual: separado desde el panel')
            ON CONFLICT (producto_clave_a, producto_clave_b)
            DO UPDATE SET decision = FALSE, confianza = 1.0, razon = EXCLUDED.razon, revisado_en = now()
        """, (clave, l["producto_clave"], l["nombre_original"], l["producto"]))
        # Que el producto nuevo quede en la misma categoría propia (si existe la tabla)
        cur.execute("SAVEPOINT cat")
        try:
            cur.execute("INSERT INTO producto_categoria (k, nombre, cat, sub, fuente) "
                        "SELECT %s, %s, cat, sub, fuente FROM producto_categoria WHERE k = %s ON CONFLICT (k) DO NOTHING",
                        (clave, l["nombre_original"], l["producto_clave"]))
            cur.execute("RELEASE SAVEPOINT cat")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT cat")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "producto_id": nuevo})


HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Precios Claros — Panel de operación</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cg transform='translate(32 32) rotate(-18) scale(.9) translate(-32 -32)'%3E%3Cpath d='M4 32 L19 13 H55 a5 5 0 0 1 5 5 V46 a5 5 0 0 1 -5 5 H19 Z M14.5 32 m-4 0 a4 4 0 1 0 8 0 a4 4 0 1 0 -8 0 Z' fill='%2312B886' fill-rule='evenodd'/%3E%3Ccircle cx='14.5' cy='32' r='5.6' fill='none' stroke='%23FFA94D' stroke-width='2.2'/%3E%3C/g%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Sora:wght@600;700;800&display=swap" rel="stylesheet">
<style>
/* =====================================================================
   Tokens — sistema visual de Precios Claros (by prometeo)
   ===================================================================== */
:root {
  /* Color: superficie (de fondo a primer plano) */
  --bg: #03110D;
  --surface-1: #08201A;
  --surface-2: #0D2A22;
  --surface-3: #13362C;
  --line: #1B3D34;
  --line-strong: #29564A;
  /* Color: texto */
  --text: #E9F4EF;
  --text-2: #A3BBB3;
  --text-3: #6E8981;
  /* Color: marca */
  --brand: #12B886;
  --brand-strong: #0B8A65;
  --brand-ink: #63E6BE;
  --brand-soft: rgba(18, 184, 134, .14);
  --spark: #FFA94D;
  --spark-soft: rgba(255, 169, 77, .14);
  /* Color: semántico */
  --ok: #3DD68C;    --ok-soft: rgba(61, 214, 140, .13);
  --warn: #F5B841;  --warn-soft: rgba(245, 184, 65, .13);
  --danger: #FF6B6B; --danger-soft: rgba(255, 107, 107, .13);
  --info: #74C0FC;  --info-soft: rgba(116, 192, 252, .13);
  /* Tipografía */
  --font-display: Sora, Inter, system-ui, sans-serif;
  --font-body: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, Consolas, monospace;
  --fs-xs: 12px; --fs-sm: 13px; --fs-md: 14px; --fs-lg: 16px; --fs-xl: 20px; --fs-2xl: 26px;
  /* Espaciado (base 4) */
  --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 20px; --s6: 24px; --s8: 32px; --s10: 40px;
  /* Bordes y elevación */
  --r-sm: 8px; --r-md: 12px; --r-lg: 16px; --r-pill: 999px;
  --shadow-1: 0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px -12px rgba(0,0,0,.6);
  --shadow-2: 0 24px 48px -20px rgba(0,0,0,.75);
  --focus: 0 0 0 3px rgba(99, 230, 190, .45);
  /* Movimiento */
  --ease: cubic-bezier(.2, .8, .2, 1);
  --t-fast: 140ms; --t-med: 240ms;
  color-scheme: dark;
}
@media (prefers-reduced-motion: reduce) { :root { --t-fast: 0ms; --t-med: 0ms; } }

*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 400 var(--fs-md)/1.5 var(--font-body); -webkit-font-smoothing: antialiased;
  background-image: radial-gradient(1200px 500px at 85% -10%, rgba(18,184,134,.10), transparent 60%),
                    radial-gradient(800px 400px at -10% 10%, rgba(255,169,77,.05), transparent 60%);
  background-attachment: fixed;
}
h1, h2, h3 { font-family: var(--font-display); margin: 0; letter-spacing: -.02em; }
p { margin: 0; }
button, input { font: inherit; color: inherit; }
:focus-visible { outline: none; box-shadow: var(--focus); }
.num, .mono { font-variant-numeric: tabular-nums; }
.mono { font-family: var(--font-mono); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
[hidden] { display: none !important; }

/* =====================================================================
   Layout
   ===================================================================== */
.wrap { max-width: 1280px; margin: 0 auto; padding: 0 var(--s6); }
.topbar {
  position: sticky; top: 0; z-index: 20; backdrop-filter: blur(14px);
  background: rgba(3, 17, 13, .78); border-bottom: 1px solid var(--line);
}
.topbar .wrap { display: flex; align-items: center; gap: var(--s4); height: 64px; }
.brand { display: flex; align-items: center; gap: var(--s3); text-decoration: none; color: inherit; }
.brand svg { width: 34px; height: 34px; flex: none; }
.brand-name { white-space: nowrap; font-family: var(--font-display); font-weight: 800; font-size: 17px; letter-spacing: -.03em; line-height: 1; }
.brand-name small { display: block; font: 600 11px/1 var(--font-body); color: var(--brand-ink); letter-spacing: 0; margin-top: 4px; }
.topbar-sep { width: 1px; height: 26px; background: var(--line-strong); }
.topbar-title { font-weight: 600; color: var(--text-2); }
.topbar-right { margin-left: auto; display: flex; align-items: center; gap: var(--s3); }

main.wrap { padding-top: var(--s6); padding-bottom: var(--s10); display: grid; gap: var(--s6); }
main > *, .grid-main > * { min-width: 0; }
.grid-main { display: grid; gap: var(--s6); grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); align-items: start; }
@media (max-width: 1080px) { .grid-main { grid-template-columns: 1fr; } }
@media (max-width: 640px) {
  .wrap { padding: 0 var(--s4); } .topbar-title, .topbar-sep { display: none; }
  .topbar-right .btn span { display: none; }
  .topbar-right { min-width: 0; } .topbar-right .badge { min-width: 0; max-width: 52vw; }
  .topbar-right .badge > span:last-child { overflow: hidden; text-overflow: ellipsis; }
  .card-head { flex-wrap: wrap; } .card-head .acciones { margin-left: 0; width: 100%; }
  .tabs { max-width: 100%; overflow-x: auto; }
  .card-head, .card-body { padding-left: var(--s4); padding-right: var(--s4); } .lanzar { padding: var(--s4); }
  .lanzar .btn { width: 100%; }
  .acc > summary .s { display: none; }
}

/* =====================================================================
   Componentes
   ===================================================================== */
/* Card */
.card { background: linear-gradient(180deg, var(--surface-1), rgba(8,32,26,.82)); border: 1px solid var(--line);
  border-radius: var(--r-lg); box-shadow: var(--shadow-1); }
.card-head { display: flex; align-items: center; gap: var(--s3); padding: var(--s5) var(--s6) 0; }
.card-head h2 { font-size: var(--fs-lg); font-weight: 700; }
.card-head .sub { color: var(--text-3); font-size: var(--fs-sm); }
.card-head .acciones { margin-left: auto; display: flex; gap: var(--s2); flex-wrap: wrap; }
.card-body { padding: var(--s5) var(--s6) var(--s6); }
.eyebrow { font: 600 11px/1 var(--font-body); letter-spacing: .12em; text-transform: uppercase; color: var(--text-3); margin-bottom: var(--s3); display: flex; align-items: center; gap: var(--s2); }
.eyebrow .paso-n { width: 20px; height: 20px; border-radius: 50%; background: var(--surface-3); color: var(--brand-ink); display: grid; place-items: center; font-size: 11px; letter-spacing: 0; }
.bloque + .bloque { margin-top: var(--s6); }

/* Botón */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: var(--s2); min-height: 40px;
  padding: 0 var(--s4); border-radius: var(--r-md); border: 1px solid transparent; cursor: pointer;
  font-weight: 600; font-size: var(--fs-md); line-height: 1; white-space: nowrap; text-decoration: none;
  transition: background var(--t-fast) var(--ease), border-color var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), opacity var(--t-fast); }
.btn svg { width: 16px; height: 16px; flex: none; }
.btn:active:not(:disabled) { transform: translateY(1px); }
.btn:disabled { opacity: .45; cursor: not-allowed; }
.btn-primary { background: var(--brand); color: #03201A; box-shadow: 0 8px 22px -10px rgba(18,184,134,.8); }
.btn-primary:hover:not(:disabled) { background: #20C997; }
.btn-secondary { background: var(--surface-3); border-color: var(--line-strong); color: var(--text); }
.btn-secondary:hover:not(:disabled) { border-color: var(--brand-strong); background: #17402F; }
.btn-ghost { background: transparent; color: var(--text-2); }
.btn-ghost:hover:not(:disabled) { background: var(--surface-2); color: var(--text); }
.btn-danger { background: var(--danger-soft); color: var(--danger); border-color: rgba(255,107,107,.35); }
.btn-danger:hover:not(:disabled) { background: rgba(255,107,107,.22); }
.btn-sm { min-height: 32px; padding: 0 var(--s3); font-size: var(--fs-sm); border-radius: var(--r-sm); }
.btn-lg { min-height: 48px; padding: 0 var(--s6); font-size: var(--fs-lg); }
.btn .spinner { width: 14px; height: 14px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: giro .8s linear infinite; }
@keyframes giro { to { transform: rotate(360deg); } }

/* Badge de estado */
.badge { display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 10px; border-radius: var(--r-pill);
  font-size: var(--fs-xs); font-weight: 600; white-space: nowrap; background: var(--surface-3); color: var(--text-2); border: 1px solid var(--line-strong); }
.badge .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.badge--ok { background: var(--ok-soft); color: var(--ok); border-color: rgba(61,214,140,.3); }
.badge--warn { background: var(--warn-soft); color: var(--warn); border-color: rgba(245,184,65,.3); }
.badge--danger { background: var(--danger-soft); color: var(--danger); border-color: rgba(255,107,107,.3); }
.badge--run { background: var(--brand-soft); color: var(--brand-ink); border-color: rgba(18,184,134,.35); }
.badge--run .dot { animation: latido 1.2s ease-in-out infinite; box-shadow: 0 0 0 0 rgba(99,230,190,.6); }
@keyframes latido { 0% { box-shadow: 0 0 0 0 rgba(99,230,190,.55); } 70% { box-shadow: 0 0 0 7px rgba(99,230,190,0); } 100% { box-shadow: 0 0 0 0 rgba(99,230,190,0); } }

/* Selector de modo (radio cards) */
.modos { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3); }
@media (max-width: 520px) { .modos { grid-template-columns: 1fr; } }
.modo { position: relative; display: flex; gap: var(--s3); padding: var(--s4); border-radius: var(--r-md); cursor: pointer;
  background: var(--surface-2); border: 1px solid var(--line); transition: border-color var(--t-fast), background var(--t-fast); }
.modo:hover { border-color: var(--line-strong); }
.modo input { position: absolute; opacity: 0; pointer-events: none; }
.modo-ico { width: 36px; height: 36px; flex: none; border-radius: 10px; background: var(--surface-3); color: var(--text-2); display: grid; place-items: center; transition: background var(--t-fast), color var(--t-fast); }
.modo-ico svg { width: 18px; height: 18px; }
.modo-t { font-weight: 600; display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; }
.modo-d { color: var(--text-3); font-size: var(--fs-sm); line-height: 1.4; margin-top: 2px; }
.modo-tag { font-size: 10.5px; font-weight: 700; letter-spacing: .04em; color: var(--spark); background: var(--spark-soft); border-radius: var(--r-pill); padding: 2px 7px; }
.modo:has(input:checked) { border-color: var(--brand); background: linear-gradient(180deg, rgba(18,184,134,.12), rgba(18,184,134,.04)); }
.modo:has(input:checked) .modo-ico { background: var(--brand); color: #03201A; }
.modo:has(input:focus-visible) { box-shadow: var(--focus); }

/* Stepper del pipeline */
.pipeline { display: flex; align-items: center; flex-wrap: wrap; row-gap: var(--s2); padding: 2px; }
.pl-paso { display: flex; align-items: center; gap: 7px; flex: none; padding: 6px 10px; border-radius: var(--r-pill);
  font-size: var(--fs-xs); font-weight: 600; color: var(--text-3); border: 1px dashed var(--line-strong); transition: all var(--t-med) var(--ease); }
.pl-paso .pl-i { width: 18px; height: 18px; border-radius: 50%; display: grid; place-items: center; font-size: 10px; background: var(--surface-3); color: var(--text-3); }
.pl-paso.incluido { color: var(--text); border: 1px solid var(--line-strong); background: var(--surface-2); }
.pl-paso.incluido .pl-i { background: var(--brand-soft); color: var(--brand-ink); }
.pl-paso.activo { border-color: var(--brand); background: var(--brand-soft); color: var(--brand-ink); }
.pl-paso.activo .pl-i { background: var(--brand); color: #03201A; }
.pl-paso.hecho { color: var(--text-2); border-style: solid; border-color: rgba(61,214,140,.3); }
.pl-paso.hecho .pl-i { background: var(--ok); color: #03201A; }
.pl-flecha { width: 14px; height: 1px; background: var(--line-strong); flex: none; }

/* Chips de cadenas */
.chips { display: flex; flex-wrap: wrap; gap: var(--s2); }
.chip { display: inline-flex; align-items: center; gap: var(--s2); min-height: 36px; padding: 0 var(--s3) 0 var(--s2);
  border-radius: var(--r-pill); background: var(--surface-2); border: 1px solid var(--line-strong); cursor: pointer; font-weight: 600; font-size: var(--fs-sm);
  transition: all var(--t-fast) var(--ease); }
.chip .ck { width: 20px; height: 20px; border-radius: 50%; border: 1.5px solid var(--line-strong); display: grid; place-items: center; transition: all var(--t-fast); }
.chip .ck svg { width: 12px; height: 12px; opacity: 0; color: #03201A; }
.chip .meta { color: var(--text-3); font-weight: 500; }
.chip[aria-pressed="true"] { border-color: var(--brand); background: var(--brand-soft); }
.chip[aria-pressed="true"] .ck { background: var(--brand); border-color: var(--brand); }
.chip[aria-pressed="true"] .ck svg { opacity: 1; }
.chip:disabled { opacity: .4; cursor: not-allowed; }
.nota { display: flex; gap: var(--s2); align-items: flex-start; color: var(--text-3); font-size: var(--fs-sm); margin-top: var(--s3); }
.nota svg { width: 15px; height: 15px; flex: none; margin-top: 2px; }

/* Campos */
.opciones summary { cursor: pointer; list-style: none; display: inline-flex; align-items: center; gap: var(--s2); color: var(--text-2); font-weight: 600; font-size: var(--fs-sm); }
.opciones summary::-webkit-details-marker { display: none; }
.opciones summary svg { width: 14px; height: 14px; transition: transform var(--t-fast); }
.opciones[open] summary svg { transform: rotate(90deg); }
.campos { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: var(--s4); margin-top: var(--s4); }
.campo label { display: block; font-size: var(--fs-sm); font-weight: 600; margin-bottom: 6px; }
.campo .ayuda { color: var(--text-3); font-size: var(--fs-xs); margin-top: 6px; }
.input { width: 100%; height: 40px; padding: 0 var(--s3); border-radius: var(--r-sm); background: var(--bg); border: 1px solid var(--line-strong); }
.input:hover { border-color: #3A6B5D; }
.input:focus { border-color: var(--brand); box-shadow: var(--focus); outline: none; }
.check { display: flex; gap: var(--s2); align-items: center; font-size: var(--fs-sm); cursor: pointer; }
.check input { width: 16px; height: 16px; accent-color: var(--brand); }

/* Resumen de lanzamiento */
.lanzar { display: flex; align-items: center; gap: var(--s4); flex-wrap: wrap; padding: var(--s4) var(--s6); border-top: 1px solid var(--line);
  background: rgba(3, 17, 13, .35); border-radius: 0 0 var(--r-lg) var(--r-lg); }
.lanzar-txt { flex: 1 1 260px; min-width: 0; }
.lanzar-txt b { font-weight: 600; }
.lanzar-txt .est { color: var(--text-3); font-size: var(--fs-sm); margin-top: 2px; }

/* Progreso */
.progress { height: 10px; border-radius: var(--r-pill); background: var(--surface-3); overflow: hidden; position: relative; }
.progress > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--brand-strong), var(--brand), var(--brand-ink)); width: 0; transition: width var(--t-med) var(--ease); }
.progress.warn > span { background: var(--warn); }
.progress.danger > span { background: var(--danger); }
.progress.indet > span { width: 35% !important; animation: indet 1.4s var(--ease) infinite; }
@keyframes indet { 0% { transform: translateX(-100%); } 100% { transform: translateX(300%); } }
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--s3); }
@media (max-width: 560px) { .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.stat { background: var(--surface-2); border: 1px solid var(--line); border-radius: var(--r-md); padding: var(--s3) var(--s4); }
.stat .v { font: 700 var(--fs-xl)/1.1 var(--font-display); letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.stat .l { color: var(--text-3); font-size: var(--fs-xs); font-weight: 600; margin-top: 4px; }
.stat.danger .v { color: var(--danger); }
.ahora { display: flex; align-items: baseline; gap: var(--s3); flex-wrap: wrap; margin: var(--s4) 0 var(--s3); }
.ahora .cad { font: 700 var(--fs-2xl)/1.1 var(--font-display); letter-spacing: -.03em; }
.ahora .cnt { color: var(--text-2); font-weight: 600; }
.ahora .cat { color: var(--text-3); font-family: var(--font-mono); font-size: var(--fs-sm); width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Consola de log */
.consola { background: #010806; border: 1px solid var(--line); border-radius: var(--r-md); overflow: hidden; }
.consola-head { display: flex; align-items: center; gap: var(--s2); padding: var(--s2) var(--s3); border-bottom: 1px solid var(--line); background: rgba(255,255,255,.015); }
.consola-head .t { font-size: var(--fs-xs); color: var(--text-3); font-weight: 600; margin-right: auto; }
.consola pre { margin: 0; height: 300px; overflow: auto; padding: var(--s3) var(--s4); font: 12px/1.6 var(--font-mono); color: #B8D3CA; white-space: pre-wrap; word-break: break-word; }
.consola pre .l-err { color: var(--danger); }
.consola pre .l-warn { color: var(--warn); }
.consola pre .l-ok { color: var(--ok); }
.consola pre .l-paso { color: var(--brand-ink); font-weight: 500; }
.consola pre .l-cmd { color: var(--text-3); }

/* Frescura por cadena */
.fresh { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: var(--s3); }
.fresh-item { background: var(--surface-1); border: 1px solid var(--line); border-radius: var(--r-md); padding: var(--s4); display: grid; gap: 4px; }
.fresh-item .top { display: flex; align-items: center; justify-content: space-between; gap: var(--s2); }
.fresh-item .n { font-weight: 700; }
.fresh-item .v { font: 700 22px/1.1 var(--font-display); font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
.fresh-item .s { color: var(--text-3); font-size: var(--fs-xs); }
.fresh-item.kpi { background: linear-gradient(180deg, rgba(255,169,77,.08), transparent), var(--surface-1); }

/* Tabs */
.tabs { display: inline-flex; gap: 2px; padding: 3px; background: var(--surface-2); border: 1px solid var(--line); border-radius: var(--r-md); }
.tab { border: 0; background: transparent; padding: 6px 12px; border-radius: 9px; cursor: pointer; font-size: var(--fs-sm); font-weight: 600; color: var(--text-2); }
.tab[aria-selected="true"] { background: var(--surface-3); color: var(--text); box-shadow: 0 1px 0 rgba(255,255,255,.05) inset; }
.tab .cnt { color: var(--text-3); font-weight: 500; margin-left: 4px; }

/* Tabla */
.tabla-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: var(--fs-sm); }
th { text-align: left; color: var(--text-3); font-weight: 600; font-size: var(--fs-xs); text-transform: uppercase; letter-spacing: .06em; padding: var(--s2) var(--s3); border-bottom: 1px solid var(--line); white-space: nowrap; }
td { padding: var(--s3); border-bottom: 1px solid rgba(27,61,52,.6); vertical-align: top; }
tr:hover td { background: rgba(255,255,255,.015); }
td .ruta { color: var(--text-2); max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td .err { color: var(--danger); font-size: var(--fs-xs); margin-top: 4px; max-width: 420px; }
.vacio { padding: var(--s8) var(--s4); text-align: center; color: var(--text-3); }
.vacio svg { width: 28px; height: 28px; display: block; margin: 0 auto var(--s2); color: var(--line-strong); }

/* Categorías por cadena */
.acc { border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface-1); }
.acc + .acc { margin-top: var(--s3); }
.acc > summary { list-style: none; cursor: pointer; display: flex; align-items: center; gap: var(--s3); padding: var(--s4); }
.acc > summary::-webkit-details-marker { display: none; }
.acc > summary .chev { width: 16px; height: 16px; color: var(--text-3); transition: transform var(--t-fast); }
.acc[open] > summary .chev { transform: rotate(90deg); }
.acc > summary .n { font-weight: 700; }
.acc > summary .s { color: var(--text-3); font-size: var(--fs-sm); }
.acc > summary .badge { margin-left: auto; }
.acc-body { padding: 0 var(--s4) var(--s4); border-top: 1px solid var(--line); }
.acc-tools { display: flex; gap: var(--s2); flex-wrap: wrap; align-items: center; padding: var(--s3) 0; }
.acc-tools .input { flex: 1 1 200px; height: 34px; }
.cols { max-height: 360px; overflow: auto; border: 1px solid var(--line); border-radius: var(--r-sm); }
.col { display: flex; align-items: center; gap: var(--s3); padding: 9px var(--s3); border-bottom: 1px solid rgba(27,61,52,.55); cursor: pointer; font-size: var(--fs-sm); }
.col:last-child { border-bottom: 0; }
.col:hover { background: var(--surface-2); }
.col input { width: 16px; height: 16px; accent-color: var(--brand); flex: none; }
.col .h { color: var(--text-3); font-family: var(--font-mono); font-size: 11.5px; }
.col .c { margin-left: auto; color: var(--text-2); font-variant-numeric: tabular-nums; }
.acc-foot { display: flex; align-items: center; gap: var(--s3); margin-top: var(--s3); flex-wrap: wrap; }
.acc-foot .sel { color: var(--text-2); font-size: var(--fs-sm); margin-right: auto; }

/* Categorías de esta corrida (desplegable por cadena en "Nueva corrida") */
.sel-cats { display: grid; gap: var(--s2); margin-top: var(--s3); }
.sel-cats .acc + .acc { margin-top: 0; }
.sel-cats .acc > summary { padding: var(--s2) var(--s3); flex-wrap: wrap; }
.sel-cats .cols { max-height: 300px; }
.col .g { color: var(--text-3); font-size: 11.5px; }
.col .falta { margin-left: auto; flex: none; font-size: 10.5px; font-weight: 700; color: var(--warn); background: var(--warn-soft); border-radius: var(--r-pill); padding: 2px 7px; }

/* Toasts */
.toasts { position: fixed; right: var(--s5); bottom: var(--s5); display: grid; gap: var(--s2); z-index: 50; max-width: min(420px, calc(100vw - 32px)); }
.toast { display: flex; gap: var(--s3); align-items: flex-start; padding: var(--s3) var(--s4); border-radius: var(--r-md); background: var(--surface-3);
  border: 1px solid var(--line-strong); box-shadow: var(--shadow-2); animation: entra var(--t-med) var(--ease); font-size: var(--fs-sm); }
.toast svg { width: 18px; height: 18px; flex: none; margin-top: 1px; }
.toast.ok svg { color: var(--ok); } .toast.error svg { color: var(--danger); } .toast.info svg { color: var(--info); }
@keyframes entra { from { opacity: 0; transform: translateY(8px); } }

/* Confirmación inline */
.confirmar { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; padding: var(--s3); border-radius: var(--r-md); background: var(--danger-soft); border: 1px solid rgba(255,107,107,.3); font-size: var(--fs-sm); }
.confirmar span { margin-right: auto; }

/* Estado por cadena / cobertura */
.fresh-item.cad { gap: 6px; }
.fresh-item .v .u { font: 500 var(--fs-xs) var(--font-body); color: var(--text-3); letter-spacing: 0; }
.cob { display: flex; height: 8px; border-radius: var(--r-pill); overflow: hidden; background: var(--surface-3); margin: 4px 0 2px; }
.cob > span { height: 100%; }
.cob-1 { background: #3A5A51; } .cob-2 { background: #0B8A65; } .cob-3 { background: #12B886; } .cob-4 { background: #63E6BE; } .cob-5 { background: var(--spark); }
.acciones-cad { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 4px; }
.accion { border: 1px solid rgba(245,184,65,.35); background: var(--warn-soft); color: var(--warn); border-radius: var(--r-pill); padding: 3px 9px; font: 600 11.5px var(--font-body); cursor: pointer; }
.accion:hover { background: rgba(245,184,65,.22); }
.accion.err { border-color: rgba(255,107,107,.35); background: var(--danger-soft); color: var(--danger); }
.ok-txt { display: inline-flex; align-items: center; gap: 5px; color: var(--ok); font-size: var(--fs-xs); font-weight: 600; }
.ok-txt svg { width: 14px; height: 14px; }
.fresh-item.catalogo { grid-column: span 2; background: linear-gradient(180deg, rgba(18,184,134,.08), transparent), var(--surface-1); }
.leyenda { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: var(--fs-xs); color: var(--text-2); }
.ley { display: inline-flex; align-items: center; gap: 5px; }
.ley i { width: 9px; height: 9px; border-radius: 3px; display: inline-block; }
@media (max-width: 640px) { .fresh-item.catalogo { grid-column: auto; } }
.uso-fila { display: grid; grid-template-columns: 42px 1fr auto; align-items: center; gap: var(--s2); font-size: var(--fs-xs); color: var(--text-2); margin-top: 4px; }
.progress.mini { height: 6px; }
.fresh-item.uso { grid-column: span 2; }
@media (max-width: 640px) { .fresh-item.uso { grid-column: auto; } }

/* Otros equipos corriendo (candados compartidos) */
.equipo { font-size: var(--fs-xs); color: var(--text-3); font-weight: 600; white-space: nowrap; }
.equipo b { color: var(--text-2); }
.otros { display: grid; gap: var(--s2); }
.otro { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; padding: var(--s3) var(--s4); border-radius: var(--r-md);
  background: linear-gradient(90deg, rgba(18,184,134,.10), rgba(18,184,134,.03)); border: 1px solid rgba(18,184,134,.3); font-size: var(--fs-sm); }
.otro .t { margin-left: auto; color: var(--text-3); font-size: var(--fs-xs); }
.chip .uso { font-size: 10.5px; font-weight: 700; color: var(--warn); background: var(--warn-soft); border-radius: var(--r-pill); padding: 2px 7px; }
.aviso-plan { color: var(--warn); font-size: var(--fs-sm); margin-top: 4px; }
@media (max-width: 640px) { .equipo { display: none; } }

/* Unir a mano */
.buscador { display: flex; gap: var(--s2); }
.buscador .input { height: 44px; font-size: var(--fs-lg); padding-left: 40px; background: var(--bg) no-repeat 12px 50% / 18px url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%236E8981' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cpath d='m20 20-3.5-3.5'/%3E%3C/svg%3E"); }
.buscador .btn { height: 44px; }
.grupos { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: var(--s3); margin-top: var(--s4); }
.grupo { position: relative; display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface-2); transition: border-color var(--t-fast), box-shadow var(--t-fast); }
.grupo.sel { border-color: var(--spark); box-shadow: 0 0 0 1px var(--spark) inset; }
.grupo-head { display: flex; gap: var(--s3); padding: var(--s3); cursor: pointer; align-items: flex-start; }
.grupo-head img, .grupo-head .ph { width: 64px; height: 64px; flex: none; border-radius: 10px; object-fit: contain; background: #fff; }
.grupo-head .ph { background: var(--surface-3); display: grid; place-items: center; color: var(--text-3); font-size: 11px; }
.grupo-t { font-weight: 600; font-size: var(--fs-sm); line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.grupo-meta { margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.grupo-ck { position: absolute; top: 10px; right: 10px; width: 22px; height: 22px; border-radius: 6px; border: 1.5px solid var(--line-strong); background: var(--bg); display: grid; place-items: center; }
.grupo-ck svg { width: 13px; height: 13px; color: #1F0C04; opacity: 0; }
.grupo.sel .grupo-ck { background: var(--spark); border-color: var(--spark); }
.grupo.sel .grupo-ck svg { opacity: 1; }
.grupo-head .info { min-width: 0; padding-right: 26px; }
.listados { border-top: 1px solid var(--line); }
.lst { display: flex; align-items: center; gap: var(--s2); padding: 8px var(--s3); font-size: var(--fs-sm); border-bottom: 1px solid rgba(27,61,52,.5); }
.lst:last-child { border-bottom: 0; }
.lst .cad { font-weight: 600; flex: none; min-width: 92px; }
.lst .nom { color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.lst .pre { font-variant-numeric: tabular-nums; font-weight: 600; flex: none; }
.lst .btn { flex: none; }
.barra-union { position: sticky; bottom: var(--s4); margin-top: var(--s4); display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap;
  padding: var(--s3) var(--s4); border-radius: var(--r-md); background: #2A1D0E; border: 1px solid rgba(255,169,77,.45); box-shadow: var(--shadow-2); }
.barra-union .t { margin-right: auto; font-size: var(--fs-sm); }
.barra-union .t b { color: var(--spark); }
.btn-spark { background: var(--spark); color: #1F0C04; }
.btn-spark:hover:not(:disabled) { background: #FFB866; }
.aviso-publicar { display: flex; align-items: center; gap: var(--s3); flex-wrap: wrap; padding: var(--s3) var(--s4); border-radius: var(--r-md); background: var(--info-soft); border: 1px solid rgba(116,192,252,.3); font-size: var(--fs-sm); margin-top: var(--s4); }
.aviso-publicar span { margin-right: auto; }
.ver-mas { display: flex; justify-content: center; padding-top: var(--s4); }

.skel { display: inline-block; height: 1em; width: 60px; border-radius: 6px; background: linear-gradient(90deg, var(--surface-2), var(--surface-3), var(--surface-2)); background-size: 200% 100%; animation: skel 1.4s infinite; }
@keyframes skel { to { background-position: -200% 0; } }
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-play" viewBox="0 0 24 24"><path fill="currentColor" d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 11v5M12 8h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></symbol>
  <symbol id="i-alert" viewBox="0 0 24 24"><path d="M12 3 2.5 20h19L12 3Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M12 10v4M12 17h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></symbol>
  <symbol id="i-ok" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="m8 12.5 3 3 5-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-full" viewBox="0 0 24 24"><path d="M4 7h11M4 12h16M4 17h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="18.5" cy="7" r="2" fill="currentColor"/></symbol>
  <symbol id="i-scrape" viewBox="0 0 24 24"><path d="M12 3v12m0 0-4.5-4.5M12 15l4.5-4.5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-match" viewBox="0 0 24 24"><circle cx="8" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="16" cy="12" r="4.5" fill="none" stroke="currentColor" stroke-width="2"/></symbol>
  <symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 14a4 4 0 0 0 0 7h14a4 4 0 0 0 0-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-ext" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-retry" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></symbol>
  <symbol id="i-copy" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" fill="none" stroke="currentColor" stroke-width="2"/></symbol>
  <symbol id="i-empty" viewBox="0 0 24 24"><path d="M4 13h4l2 3h4l2-3h4M5.5 6h13L20 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-5l1.5-7Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></symbol>
</svg>

<header class="topbar">
  <div class="wrap">
    <a class="brand" href="/" aria-label="Precios Claros, panel de operación">
      <svg viewBox="0 0 64 64" aria-hidden="true"><g transform="translate(32 32) rotate(-18) scale(.9) translate(-32 -32)"><path d="M4 32 L19 13 H55 a5 5 0 0 1 5 5 V46 a5 5 0 0 1 -5 5 H19 Z M14.5 32 m-4 0 a4 4 0 1 0 8 0 a4 4 0 1 0 -8 0 Z" fill="#12B886" fill-rule="evenodd"/><rect x="25" y="23" width="3" height="18" rx="1.5" fill="#fff"/><rect x="30" y="23" width="1.6" height="18" rx=".8" fill="#fff"/><rect x="33.6" y="23" width="4" height="18" rx="2" fill="#fff"/><rect x="39.6" y="23" width="2" height="18" rx="1" fill="#fff"/><rect x="43.6" y="23" width="3.4" height="18" rx="1.7" fill="#fff"/><rect x="49" y="23" width="1.8" height="18" rx=".9" fill="#fff"/><circle cx="14.5" cy="32" r="5.6" fill="none" stroke="#FFA94D" stroke-width="2.2"/></g></svg>
      <span class="brand-name">Precios Claros<small>by prometeo</small></span>
    </a>
    <span class="topbar-sep" aria-hidden="true"></span>
    <span class="topbar-title">Panel de operación</span>
    <div class="topbar-right">
      <span class="equipo" title="Nombre de esta computadora para el otro equipo. Se cambia con PRECIOS_EQUIPO en el .env">Equipo: <b id="equipo">—</b></span>
      <span id="badge-estado" class="badge" role="status" aria-live="polite"><span class="dot"></span><span>Conectando…</span></span>
      <a class="btn btn-ghost btn-sm" href="https://preciosclaros.techprometeo.com" target="_blank" rel="noopener"><span>Ver sitio</span><svg><use href="#i-ext"/></svg></a>
    </div>
  </div>
</header>

<main class="wrap">
  <div class="otros" id="otros" hidden aria-live="polite"></div>
  <section aria-labelledby="h-fresh">
    <h2 id="h-fresh" class="sr-only">Estado de los datos</h2>
    <div class="fresh" id="fresh"><div class="fresh-item"><span class="skel" style="width:90px"></span><span class="skel" style="width:60px;height:22px"></span><span class="skel" style="width:120px"></span></div></div>
  </section>

  <div class="grid-main">
    <!-- ============ Nueva corrida ============ -->
    <section class="card" aria-labelledby="h-nueva">
      <div class="card-head"><h2 id="h-nueva">Nueva corrida</h2></div>
      <div class="card-body">
        <div class="bloque">
          <div class="eyebrow"><span class="paso-n">1</span>Qué querés hacer</div>
          <div class="modos" role="radiogroup" aria-label="Modo de corrida" id="modos"></div>
        </div>
        <div class="bloque">
          <div class="pipeline" id="pipeline-plan" aria-label="Pasos que va a correr"></div>
        </div>
        <div class="bloque" id="bloque-cadenas">
          <div class="eyebrow"><span class="paso-n">2</span>Cadenas <button class="btn btn-ghost btn-sm" style="margin-left:auto;letter-spacing:0;text-transform:none" id="btn-todas" type="button">Todas</button></div>
          <div class="chips" id="chips" role="group" aria-label="Cadenas a scrapear"></div>
          <div class="sel-cats" id="sel-cats"></div>
          <p class="nota" id="nota-cadenas" hidden><svg><use href="#i-info"/></svg><span>Este modo trabaja sobre todo lo que ya está en la base, de todas las cadenas.</span></p>
        </div>
        <div class="bloque">
          <details class="opciones" id="opciones">
            <summary><svg><use href="#i-chev"/></svg>Opciones avanzadas</summary>
            <div class="campos">
              <div class="campo" id="campo-limite">
                <label for="in-limite">Prueba rápida</label>
                <input class="input" id="in-limite" type="number" min="1" inputmode="numeric" placeholder="Todas las categorías">
                <p class="ayuda">Solo las primeras N categorías de cada cadena.</p>
              </div>
              <div class="campo" id="campo-llm">
                <label for="in-llm">Consultas a la IA en el matching</label>
                <input class="input" id="in-llm" type="number" min="0" inputmode="numeric" placeholder="50 (por defecto)">
                <p class="ayuda">Tope de pares dudosos que se le preguntan a la IA.</p>
              </div>
              <div class="campo" id="campo-fotos">
                <label>Fotos nuevas de Hipermaxi y Chávez</label>
                <label class="check"><input type="checkbox" id="in-fotos" checked> Copiarlas a Supabase</label>
                <p class="ayuda">Puede tardar si hay muchas. Las ya copiadas no se vuelven a subir. Si lo desmarcás, la web usa la foto original del sitio. Se frena sola al llegar al tope de espacio.</p>
              </div>
              <div class="campo" id="campo-export">
                <label>Archivo precios.json</label>
                <label class="check"><input type="checkbox" id="in-export" checked> Regenerarlo (~37 MB)</label>
                <p class="ayuda">Copia de todos los precios en un archivo. La web no lo usa; lo usan la API para clientes y el dashboard de Farmacorp. Si no los vas a usar hoy, desmarcalo y la corrida tarda ~3 min menos.</p>
              </div>
            </div>
          </details>
        </div>
      </div>
      <div class="lanzar">
        <div class="lanzar-txt"><div id="resumen-plan">—</div><div class="est" id="estimado">—</div></div>
        <button class="btn btn-primary btn-lg" id="btn-correr" type="button"><svg><use href="#i-play"/></svg><span>Iniciar corrida</span></button>
      </div>
    </section>

    <!-- ============ Corrida en curso ============ -->
    <section class="card" aria-labelledby="h-curso">
      <div class="card-head">
        <h2 id="h-curso">Corrida</h2>
        <span class="sub" id="curso-sub"></span>
        <div class="acciones"><button class="btn btn-danger btn-sm" id="btn-detener" type="button" hidden><svg><use href="#i-stop"/></svg>Detener</button></div>
      </div>
      <div class="card-body">
        <div id="confirmar" class="confirmar" hidden>
          <span>¿Detener la corrida? Lo ya guardado queda guardado; la categoría en curso no queda a medias.</span>
          <button class="btn btn-ghost btn-sm" id="conf-no" type="button">Seguir corriendo</button>
          <button class="btn btn-danger btn-sm" id="conf-si" type="button">Sí, detener</button>
        </div>
        <div class="pipeline" id="pipeline-curso" aria-label="Avance por paso"></div>
        <div id="curso-vacio" class="vacio"><svg><use href="#i-empty"/></svg>No hay ninguna corrida en curso. Elegí qué correr y tocá <b>Iniciar corrida</b>.</div>
        <div id="curso-info" hidden>
          <div class="ahora"><span class="cad" id="c-cadena">—</span><span class="cnt num" id="c-cnt"></span><span class="cat" id="c-cat"></span></div>
          <div class="progress" id="c-barra" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-label="Avance"><span></span></div>
          <div class="stats" style="margin-top:var(--s4)">
            <div class="stat"><div class="v num" id="c-tiempo">—</div><div class="l">Transcurrido</div></div>
            <div class="stat"><div class="v num" id="c-eta">—</div><div class="l">Falta aprox.</div></div>
            <div class="stat"><div class="v num" id="c-prod">0</div><div class="l">Productos guardados</div></div>
            <div class="stat" id="c-err-box"><div class="v num" id="c-err">0</div><div class="l">Errores</div></div>
          </div>
        </div>
        <div class="consola" style="margin-top:var(--s5)">
          <div class="consola-head">
            <span class="t">Log en vivo</span>
            <label class="check" style="font-size:12px"><input type="checkbox" id="log-errores"> Solo errores</label>
            <button class="btn btn-ghost btn-sm" id="btn-copiar" type="button" aria-label="Copiar log"><svg><use href="#i-copy"/></svg></button>
          </div>
          <pre id="log" tabindex="0" aria-label="Log de la corrida">Esperando…</pre>
        </div>
      </div>
    </section>
  </div>

  <!-- ============ Matching ============ -->
  <section class="card" aria-labelledby="h-match">
    <div class="card-head"><h2 id="h-match">Último matching</h2><span class="sub" id="m-sub"></span></div>
    <div class="card-body">
      <div id="m-vacio" class="vacio" style="padding:var(--s4)">Todavía no corrió ningún matching.</div>
      <div id="m-info" hidden>
        <div class="progress" id="m-barra"><span></span></div>
        <p class="nota" id="m-cat" hidden></p>
        <div class="stats" style="margin-top:var(--s4)">
          <div class="stat"><div class="v num" id="m-cand">—</div><div class="l">Candidatos revisados</div></div>
          <div class="stat"><div class="v num" id="m-fus">—</div><div class="l">Fusiones</div></div>
          <div class="stat"><div class="v num" id="m-llm">—</div><div class="l">Consultas IA</div></div>
          <div class="stat"><div class="v num" id="m-eta">—</div><div class="l">Falta aprox.</div></div>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ Unir a mano ============ -->
  <section class="card" aria-labelledby="h-unir">
    <div class="card-head"><h2 id="h-unir">Unir o separar productos a mano</h2><span class="sub">Cuando el matching no se da cuenta de que son el mismo producto (o une dos que no lo son)</span></div>
    <div class="card-body">
      <form class="buscador" id="form-buscar" role="search">
        <label class="sr-only" for="in-buscar">Buscar productos</label>
        <input class="input" id="in-buscar" type="search" placeholder="Buscá por nombre, ej.: zanna frutos" autocomplete="off">
        <button class="btn btn-secondary" type="submit">Buscar</button>
      </form>
      <p class="nota" id="ayuda-unir"><svg><use href="#i-info"/></svg><span>Cada tarjeta es un producto tal como sale en la web, con las cadenas que lo venden. <b>Unir:</b> tocá las tarjetas que son el mismo producto y después "Unir". <b>Separar:</b> en una tarjeta con varias cadenas, tocá "Separar" en la que no corresponde; el matching automático no la vuelve a unir.</span></p>
      <p class="nota" id="nota-bloq-match" hidden style="color:var(--warn)"><svg><use href="#i-alert"/></svg><span id="nota-bloq-txt"></span></p>
      <div class="grupos" id="grupos"></div>
      <div class="barra-union" id="barra-union" hidden>
        <span class="t" id="union-txt"></span>
        <button class="btn btn-ghost btn-sm" id="union-cancelar" type="button">Cancelar</button>
        <button class="btn btn-spark btn-sm" id="union-ok" type="button">Unir como un solo producto</button>
      </div>
      <div class="aviso-publicar" id="aviso-publicar" hidden>
        <svg style="width:18px;height:18px;color:var(--info)"><use href="#i-info"/></svg>
        <span>Ya está guardado en la base. <b>Publicar ahora</b> solo rearma la web (1–2 min): no scrapea, no matchea ni copia fotos. Ojo: sube todo lo que hay en la base en este momento, así que si había precios cargados sin publicar, también salen.</span>
        <button class="btn btn-secondary btn-sm" id="btn-publicar-ya" type="button"><svg><use href="#i-upload"/></svg>Publicar ahora</button>
      </div>
    </div>
  </section>

  <!-- ============ Historial ============ -->
  <section class="card" aria-labelledby="h-hist">
    <div class="card-head">
      <h2 id="h-hist">Historial</h2>
      <div class="acciones">
        <div class="tabs" role="tablist" aria-label="Filtrar historial" id="tabs-hist">
          <button class="tab" role="tab" data-f="todo" aria-selected="true">Todo</button>
          <button class="tab" role="tab" data-f="error" aria-selected="false">Errores<span class="cnt" id="cnt-err"></span></button>
          <button class="tab" role="tab" data-f="scraper" aria-selected="false">Scraping</button>
          <button class="tab" role="tab" data-f="matching" aria-selected="false">Matching</button>
        </div>
      </div>
    </div>
    <div class="card-body">
      <div class="tabla-wrap">
        <table>
          <thead><tr><th>Cuándo</th><th>Tipo</th><th>Cadena</th><th>Categoría / ruta</th><th>Estado</th><th style="text-align:right">Productos</th><th></th></tr></thead>
          <tbody id="hist"><tr><td colspan="7" class="vacio">Cargando…</td></tr></tbody>
        </table>
      </div>
      <div class="ver-mas" id="hist-mas" hidden><button class="btn btn-secondary btn-sm" type="button" id="btn-hist-mas">Ver más</button></div>
    </div>
  </section>

  <!-- ============ Categorías ============ -->
  <section class="card" aria-labelledby="h-cats">
    <div class="card-head"><h2 id="h-cats">Categorías por cadena</h2><span class="sub">Qué categorías scrapea cada cadena en las próximas corridas</span></div>
    <div class="card-body" id="cats"></div>
  </section>
</main>

<div class="toasts" id="toasts" aria-live="polite"></div>

<script>
const DATOS = __DATOS__;
const PASOS = DATOS.pasos;
const NOMBRE = {scraping: 'Scraping y carga', clasificar: 'Clasificar', matching: 'Matching', export: 'precios.json', fotos: 'Fotos', publicar: 'Publicar web'};
const MODOS = [
  {id: 'completo', t: 'Pipeline completo', d: 'Scrapea, guarda, matchea, copia fotos y publica en la web.', ico: 'i-full', tag: 'Recomendado', pasos: PASOS, cadenas: true},
  {id: 'scraping', t: 'Solo scraping', d: 'Trae precios nuevos y los guarda en la base. No publica.', ico: 'i-scrape', pasos: ['scraping'], cadenas: true},
  {id: 'matching', t: 'Solo matching', d: 'Clasifica y empareja lo que ya está en la base.', ico: 'i-match', pasos: ['clasificar', 'matching'], cadenas: false},
  {id: 'publicar', t: 'Solo publicar', d: 'Copia fotos nuevas (opcional) y rearma la web con todo lo que hay en la base.', ico: 'i-upload', pasos: ['fotos', 'publicar'], cadenas: false},
];
// Estimaciones (segundos) medidas con la carga por lotes; se ajustan solas con la corrida real.
const SEG_CATEGORIA = {shopify: 9, navegador: 35};
const SEG_PASO = {clasificar: 120, matching: 420, export: 180, fotos: 120, publicar: 180};

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = n => (n ?? 0).toLocaleString('es-BO');
function fmtDur(s) {
  if (s == null || !isFinite(s)) return '—';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + ' s';
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min`;
}
function haceCuanto(iso) {
  if (!iso) return 'nunca';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return 'recién';
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400); return `hace ${d} día${d > 1 ? 's' : ''}`;
}
function fmtFecha(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('es-BO', {day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit'});
}
function toast(msg, tipo = 'info') {
  const ico = {ok: 'i-ok', error: 'i-alert', info: 'i-info'}[tipo];
  const el = document.createElement('div');
  el.className = `toast ${tipo}`; el.setAttribute('role', tipo === 'error' ? 'alert' : 'status');
  el.innerHTML = `<svg><use href="#${ico}"/></svg><div>${esc(msg)}</div>`;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), tipo === 'error' ? 7000 : 4000);
}
async function api(url, body) {
  const r = await fetch(url, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (!r.ok || j.error) throw new Error(j.error || `Error ${r.status}`);
  return j;
}

/* ---------------- Nueva corrida ---------------- */
let modo = 'completo';
const elegidas = new Set(DATOS.cadenas.map(c => c.cadena));
// Categorías elegidas para ESTA corrida, por cadena (por defecto todas). No cambia
// config_cadenas.json: eso se hace abajo, en "Categorías por cadena".
const catsElegidas = {};
DATOS.cadenas.forEach(c => { if (c.categorias) catsElegidas[c.cadena] = new Set(c.categorias.map(x => x.id)); });
const nCats = c => c.categorias ? catsElegidas[c.cadena].size : c.n;
const faltanSemana = cadena => new Set((((ultimoResumen && ultimoResumen.cadenas) || []).find(x => x.cadena === cadena)?.semana?.faltan) || []);
const abiertas = new Set();
let firmaSel = '';

function renderModos() {
  $('#modos').innerHTML = MODOS.map(m => `
    <label class="modo">
      <input type="radio" name="modo" value="${m.id}" ${m.id === modo ? 'checked' : ''}>
      <span class="modo-ico"><svg><use href="#${m.ico}"/></svg></span>
      <span><span class="modo-t">${m.t}${m.tag ? `<span class="modo-tag">${m.tag}</span>` : ''}</span><span class="modo-d">${m.d}</span></span>
    </label>`).join('');
  document.querySelectorAll('input[name=modo]').forEach(i => i.addEventListener('change', () => { modo = i.value; actualizarPlan(); }));
}
/* Candados compartidos: qué corre en otras computadoras */
let candadosTodos = [], candadosOtros = [], firmaCandados = '';
const cadenaOcupada = c => candadosOtros.find(x => x.nombre === 'cadena:' + c);
const procesoOcupado = () => candadosOtros.find(x => x.nombre === 'procesar');
function renderOtros() {
  const porEquipo = {};
  candadosOtros.forEach(c => (porEquipo[c.equipo] = porEquipo[c.equipo] || []).push(c));
  const html = Object.entries(porEquipo).map(([eq, cs]) => {
    const proc = cs.find(c => c.nombre === 'procesar');
    const que = proc ? (proc.detalle || 'Procesando') : cs.map(c => c.detalle || c.nombre.replace('cadena:', 'Scraping ')).join(' · ');
    const min = Math.max(...cs.map(c => c.minutos || 0));
    const quien = cs[0].este_equipo ? `${eq} (esta computadora, desde otra ventana o la terminal)` : eq;
    return `<div class="otro"><span class="badge badge--run"><span class="dot"></span>En curso</span>
      <span><b>${esc(quien)}</b> está corriendo: ${esc(que)}</span><span class="t">desde hace ${min} min</span></div>`;
  }).join('');
  $('#otros').innerHTML = html; $('#otros').hidden = !html;
  const m = candadosTodos.find(c => c.paso === 'matching');
  $('#nota-bloq-match').hidden = !m;
  if (m) $('#nota-bloq-txt').textContent = `Hay un matching corriendo en ${m.mi_corrida ? 'esta corrida' : m.equipo}. Uní o separá cuando termine; si no, el matching podría deshacer tu cambio.`;
}
async function cargarCandados() {
  try {
    const r = await api('/api/candados');
    $('#equipo').textContent = r.equipo;
    candadosTodos = r.candados || [];
    candadosOtros = candadosTodos.filter(c => !c.mi_corrida);
    const firma = JSON.stringify(candadosTodos.map(c => [c.nombre, c.equipo, c.detalle, c.minutos]));
    if (firma !== firmaCandados) { firmaCandados = firma; renderOtros(); renderChips(); actualizarPlan(); }
  } catch (e) { /* se reintenta solo */ }
  setTimeout(cargarCandados, 10000);
}

function renderChips() {
  $('#chips').innerHTML = DATOS.cadenas.map(c => { const o = cadenaOcupada(c.cadena); return `
    <button type="button" class="chip" data-c="${esc(c.cadena)}" aria-pressed="${elegidas.has(c.cadena)}" ${o ? `title="La está scrapeando ${esc(o.equipo)}: si la elegís, se saltea"` : ''}>
      <span class="ck"><svg><use href="#i-check"/></svg></span>${esc(c.cadena)}
      <span class="meta">${c.n == null ? 'auto' : (nCats(c) < c.n ? `${nCats(c)} de ${c.n}` : c.n)} ${c.tipo === 'navegador' ? 'URLs' : 'cat.'}</span>
      ${o ? `<span class="uso">en uso: ${esc(o.equipo)}</span>` : ''}
    </button>`; }).join('');
  document.querySelectorAll('.chip').forEach(b => b.addEventListener('click', () => {
    const c = b.dataset.c; elegidas.has(c) ? elegidas.delete(c) : elegidas.add(c);
    b.setAttribute('aria-pressed', elegidas.has(c)); actualizarPlan();
  }));
}
$('#btn-todas').addEventListener('click', () => {
  const todas = elegidas.size === DATOS.cadenas.length;
  DATOS.cadenas.forEach(c => todas ? elegidas.delete(c.cadena) : elegidas.add(c.cadena));
  renderChips(); actualizarPlan();
});

function resumenSel(c) {
  const sel = catsElegidas[c.cadena], tot = c.categorias.length, u = c.tipo === 'navegador' ? 'URLs' : 'categorías';
  return sel.size === tot ? `Todas las ${tot} ${u}` : `${sel.size} de ${tot} ${u}`;
}
function renderSelCats() {
  const m = MODOS.find(x => x.id === modo);
  const cads = m.cadenas && pasosDelModo().includes('scraping') ? DATOS.cadenas.filter(c => elegidas.has(c.cadena) && c.categorias) : [];
  const firma = JSON.stringify(cads.map(c => [c.cadena, [...faltanSemana(c.cadena)].length]));
  if (firma === firmaSel) {  // mismo contenido: solo se actualizan los contadores (no se pierde el filtro ni el scroll)
    cads.forEach(c => { const el = document.querySelector(`#sel-cats [data-sc="${CSS.escape(c.cadena)}"]`); if (el) el.textContent = resumenSel(c); });
    return;
  }
  firmaSel = firma;
  $('#sel-cats').innerHTML = cads.map(c => {
    const sel = catsElegidas[c.cadena], falt = faltanSemana(c.cadena), k = DATOS.cadenas.indexOf(c);
    return `<details class="acc" data-sk="${k}" ${abiertas.has(c.cadena) ? 'open' : ''}>
      <summary><svg class="chev"><use href="#i-chev"/></svg><span class="n">${esc(c.cadena)}</span>
        <span class="s" data-sc="${esc(c.cadena)}">${resumenSel(c)}</span>
        ${falt.size ? `<span class="badge badge--warn">${falt.size} sin scrapear esta semana</span>` : ''}</summary>
      <div class="acc-body">
        <div class="acc-tools">
          <input class="input" type="search" placeholder="Filtrar…" data-sfiltro aria-label="Filtrar categorías de ${esc(c.cadena)}">
          <button class="btn btn-ghost btn-sm" data-sacc="todas" type="button">Todas</button>
          <button class="btn btn-ghost btn-sm" data-sacc="ninguna" type="button">Ninguna</button>
          ${falt.size ? '<button class="btn btn-ghost btn-sm" data-sacc="faltan" type="button">Solo las que faltan</button>' : ''}
        </div>
        <div class="cols">${c.categorias.map(x => `<label class="col"><input type="checkbox" value="${esc(x.id)}" ${sel.has(x.id) ? 'checked' : ''}>
          <span><span>${esc(x.nombre)}</span>${x.grupo ? ` <span class="g">· ${esc(x.grupo)}</span>` : ''}</span>${falt.has(x.id) ? '<span class="falta">falta esta semana</span>' : ''}</label>`).join('')}</div>
      </div>
    </details>`;
  }).join('');
  document.querySelectorAll('#sel-cats .acc').forEach(d => {
    const c = DATOS.cadenas[d.dataset.sk], sel = catsElegidas[c.cadena];
    d.addEventListener('toggle', () => d.open ? abiertas.add(c.cadena) : abiertas.delete(c.cadena));
    d.querySelector('.cols').addEventListener('change', e => {
      e.target.checked ? sel.add(e.target.value) : sel.delete(e.target.value);
      renderChips(); actualizarPlan();
    });
    d.querySelector('[data-sfiltro]').addEventListener('input', e => {
      const q = e.target.value.toLowerCase();
      d.querySelectorAll('.col').forEach(l => l.hidden = q && !l.textContent.toLowerCase().includes(q));
    });
    d.querySelectorAll('[data-sacc]').forEach(b => b.addEventListener('click', () => {
      const falt = faltanSemana(c.cadena);
      d.querySelectorAll('.col').forEach(l => {
        const i = l.querySelector('input');
        if (b.dataset.sacc === 'faltan') i.checked = falt.has(i.value);
        else if (!l.hidden) i.checked = b.dataset.sacc === 'todas';  // con filtro: solo las que se ven
        i.checked ? sel.add(i.value) : sel.delete(i.value);
      });
      renderChips(); actualizarPlan();
    }));
  });
}

function pasosDelModo() {
  let p = [...MODOS.find(m => m.id === modo).pasos];
  if (!$('#in-export').checked) p = p.filter(x => x !== 'export');
  if (!$('#in-fotos').checked) p = p.filter(x => x !== 'fotos');
  return p;
}
function pipelineHTML(incluidos, activo, hechos) {
  return PASOS.map((p, i) => {
    let cls = 'pl-paso';
    if (incluidos.includes(p)) cls += ' incluido';
    if (hechos && hechos.includes(p)) cls += ' hecho';
    if (p === activo) cls += ' activo';
    const marca = hechos && hechos.includes(p) ? '<svg style="width:10px;height:10px"><use href="#i-check"/></svg>' : (i + 1);
    return (i ? '<span class="pl-flecha" aria-hidden="true"></span>' : '') +
      `<span class="${cls}"><span class="pl-i">${marca}</span>${NOMBRE[p]}</span>`;
  }).join('');
}
function estimar(pasos, cadenas, limite) {
  let s = 0, cats = 0;
  if (pasos.includes('scraping')) cadenas.forEach(c => {
    const n0 = nCats(c), n = n0 == null ? 100 : (limite ? Math.min(n0, limite) : n0);
    cats += n; s += n * SEG_CATEGORIA[c.tipo];
  });
  pasos.forEach(p => { if (SEG_PASO[p]) s += SEG_PASO[p]; });
  return {s, cats};
}
function actualizarPlan() {
  const m = MODOS.find(x => x.id === modo);
  const pasos = pasosDelModo();
  $('#pipeline-plan').innerHTML = pipelineHTML(pasos);
  $('#bloque-cadenas').style.opacity = m.cadenas ? 1 : .55;
  document.querySelectorAll('.chip, #btn-todas').forEach(b => b.disabled = !m.cadenas);
  $('#nota-cadenas').hidden = m.cadenas;
  $('#campo-limite').hidden = !pasos.includes('scraping');
  $('#campo-llm').hidden = !pasos.includes('matching');
  $('#campo-export').hidden = modo !== 'completo';
  $('#campo-fotos').hidden = !MODOS.find(x => x.id === modo).pasos.includes('fotos');
  const limite = parseInt($('#in-limite').value) || null;
  const cads = DATOS.cadenas.filter(c => elegidas.has(c.cadena));
  const {s, cats} = estimar(pasos, m.cadenas ? cads : [], limite);
  renderSelCats();
  const sinCats = m.cadenas && pasos.includes('scraping') ? cads.filter(c => c.categorias && !catsElegidas[c.cadena].size) : [];
  let txt;
  if (m.cadenas && !cads.length) txt = '<b>Elegí al menos una cadena.</b>';
  else if (sinCats.length) txt = `<b>Elegí al menos una categoría de ${sinCats.map(c => esc(c.cadena)).join(', ')}</b> (o sacá la cadena).`;
  else if (modo === 'completo') txt = `Scrapear <b>${cads.length === DATOS.cadenas.length ? 'todas las cadenas' : cads.map(c => esc(c.cadena)).join(', ')}</b> (${fmtNum(cats)} categorías) y publicar en la web.`;
  else if (modo === 'scraping') txt = `Traer precios de <b>${cads.map(c => esc(c.cadena)).join(', ')}</b> (${fmtNum(cats)} categorías) y guardarlos. <span style="color:var(--text-3)">La web no cambia.</span>`;
  else if (modo === 'matching') txt = 'Clasificar y emparejar los productos pendientes de <b>todas las cadenas</b>.';
  else txt = (pasos.includes('fotos') ? 'Copiar fotos nuevas y <b>rearmar la web</b>' : '<b>Rearmar la web</b>') + ' con todo lo que hay en la base (precios ya cargados y cambios hechos a mano).';
  const avisos = [];
  if (pasos.includes('scraping') && m.cadenas) {
    const ocupadas = cads.filter(c => cadenaOcupada(c.cadena));
    if (ocupadas.length) avisos.push(`Se van a saltear ${ocupadas.map(c => `${esc(c.cadena)} (la corre ${esc(cadenaOcupada(c.cadena).equipo)})`).join(', ')}.`);
  }
  const proc = procesoOcupado();
  if (proc && pasos.some(p => p !== 'scraping')) avisos.push(`${esc(proc.equipo)} está en “${esc(proc.detalle || 'procesando')}”: tu corrida va a esperar a que termine antes de matchear o publicar.`);
  $('#resumen-plan').innerHTML = txt + avisos.map(a => `<div class="aviso-plan">${a}</div>`).join('');
  $('#estimado').textContent = `Tiempo estimado: ≈ ${fmtDur(s)}${limite && pasos.includes('scraping') ? ' · prueba rápida' : ''}`;
  $('#btn-correr').disabled = corriendo || (m.cadenas && !cads.length) || sinCats.length > 0;
}
['#in-limite', '#in-llm', '#in-export', '#in-fotos'].forEach(s => $(s).addEventListener('input', actualizarPlan));

$('#btn-correr').addEventListener('click', async () => {
  const m = MODOS.find(x => x.id === modo);
  const btn = $('#btn-correr'); btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span><span>Iniciando…</span>';
  try {
    await api('/api/correr', {
      modo, cadenas: m.cadenas ? DATOS.cadenas.filter(c => elegidas.has(c.cadena)).map(c => c.cadena) : null,
      categorias: m.cadenas ? Object.fromEntries(DATOS.cadenas
        .filter(c => elegidas.has(c.cadena) && c.categorias && nCats(c) < c.categorias.length)
        .map(c => [c.cadena, [...catsElegidas[c.cadena]]])) : null,
      limite: $('#in-limite').value || null, max_llm: $('#in-llm').value || null, sin_export: !$('#in-export').checked,
      sin_fotos: !$('#in-fotos').checked,
    });
    toast(`Corrida iniciada: ${m.t.toLowerCase()}.`, 'ok');
    logDesde = 0; $('#log').textContent = '';
    pollEstado();
  } catch (e) { toast(e.message, 'error'); }
  btn.innerHTML = '<svg><use href="#i-play"/></svg><span>Iniciar corrida</span>';
  actualizarPlan();
});

/* ---------------- Corrida en curso ---------------- */
let corriendo = false, logDesde = 0, logLineas = [], pollTimer = null, ultimoProgreso = null;

function claseLinea(l) {
  if (/\bERROR\b|Traceback|\[error\]|fall[oó]/i.test(l)) return 'l-err';
  if (/ADVERTENCIA|Reintento|Intento \d/i.test(l)) return 'l-warn';
  if (/^== Paso/.test(l.replace('[run_all] ', ''))) return 'l-paso';
  if (/^\$ |^--- /.test(l)) return 'l-cmd';
  if (/productos cargados, 0 errores|Listo\./.test(l)) return 'l-ok';
  return '';
}
function renderLog() {
  const pre = $('#log');
  const pegado = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  const solo = $('#log-errores').checked;
  const lineas = solo ? logLineas.filter(l => claseLinea(l) === 'l-err') : logLineas.slice(-500);
  pre.innerHTML = lineas.length ? lineas.map(l => { const c = claseLinea(l); return c ? `<span class="${c}">${esc(l)}</span>` : esc(l); }).join('\n')
                                : (solo ? 'Sin errores en esta corrida.' : 'Esperando…');
  if (pegado || !solo) pre.scrollTop = pre.scrollHeight;
}
$('#log-errores').addEventListener('change', renderLog);
$('#btn-copiar').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(logLineas.join('\n')); toast('Log copiado.', 'ok'); } catch (e) { toast('No se pudo copiar.', 'error'); }
});

function setBadge(tipo, texto) {
  const b = $('#badge-estado');
  b.className = 'badge' + (tipo ? ` badge--${tipo}` : '');
  b.querySelector('span:last-child').textContent = texto;
}

function renderProgreso(e) {
  const p = e.progreso || {};
  ultimoProgreso = p;
  const plan = p.plan;
  $('#btn-detener').hidden = !e.corriendo;
  if (!e.corriendo) $('#confirmar').hidden = true;
  if (!plan) { $('#curso-vacio').hidden = false; $('#curso-info').hidden = true; $('#pipeline-curso').innerHTML = ''; $('#curso-sub').textContent = ''; return; }
  $('#curso-vacio').hidden = true; $('#curso-info').hidden = false;

  const pasos = plan.pasos;
  const idx = pasos.indexOf(p.fase);
  const terminado = !e.corriendo;
  const hechos = terminado && p.codigo === 0 ? pasos : pasos.slice(0, Math.max(0, idx));
  $('#pipeline-curso').innerHTML = pipelineHTML(pasos, terminado ? null : p.fase, hechos);

  const fin = p.fin || e.ahora;
  const trans = p.inicio ? fin - p.inicio : null;
  $('#c-tiempo').textContent = fmtDur(trans);
  $('#c-prod').textContent = fmtNum(p.productos);
  const nErr = (p.errores || 0) + (p.errores_productos || 0);
  $('#c-err').textContent = fmtNum(nErr);
  $('#c-err-box').classList.toggle('danger', nErr > 0);

  // Avance y ETA
  const segCat = p.seg_por_categoria;
  let restante = 0, totalCats = 0, hechasCats = 0;
  const cads = plan.cadenas || [];
  const iCad = cads.findIndex(c => c.cadena === p.cadena);
  cads.forEach((c, k) => {
    const n = c.n ?? (k === iCad ? p.n : 100);
    totalCats += n;
    const seg = segCat && c.tipo === (cads[iCad] || {}).tipo ? segCat : SEG_CATEGORIA[c.tipo];
    if (p.fase !== 'scraping' && idx > 0) { hechasCats += n; return; }
    if (iCad < 0 || k > iCad) restante += n * seg;
    else if (k === iCad) { hechasCats += Math.max(0, p.i - 1); restante += Math.max(0, (p.n || n) - p.i + 1) * seg; }
    else hechasCats += n;
  });
  pasos.forEach((ps, k) => { if (SEG_PASO[ps] && (idx < 0 || k > idx || (k === idx && ps !== 'scraping'))) restante += k === idx ? SEG_PASO[ps] / 2 : SEG_PASO[ps]; });

  const barra = $('#c-barra');
  let pct;
  if (terminado) pct = 100;
  else if (p.fase === 'scraping' && totalCats) pct = Math.min(99, hechasCats / totalCats * 100);
  else if (idx >= 0) pct = Math.min(99, (idx + .5) / pasos.length * 100);
  else pct = 2;
  barra.classList.toggle('indet', !terminado && p.fase !== 'scraping' && p.fase != null);
  barra.classList.toggle('warn', terminado && !!p.detenido);
  barra.classList.toggle('danger', terminado && !p.detenido && p.codigo !== 0);
  barra.firstElementChild.style.width = pct + '%';
  barra.setAttribute('aria-valuenow', Math.round(pct));
  $('#c-eta').textContent = terminado ? '—' : ('≈ ' + fmtDur(restante));

  const modoInfo = MODOS.find(m => m.id === p.modo) || {t: p.modo === 'web' ? 'Publicar cambios en la web' : 'Corrida'};
  if (terminado) {
    const ok = p.codigo === 0, det = p.detenido;
    $('#c-cadena').textContent = det ? 'Detenida' : ok ? 'Terminó bien' : 'Terminó con errores';
    $('#c-cnt').textContent = `${modoInfo.t} · ${fmtDur(trans)}`;
    $('#c-cat').textContent = p.fin ? `Finalizó ${fmtFecha(new Date(p.fin * 1000).toISOString())}` : '';
    $('#curso-sub').textContent = '';
  } else if (p.fase === 'scraping' && p.cadena) {
    $('#c-cadena').textContent = p.cadena;
    $('#c-cnt').textContent = `categoría ${p.i} de ${p.n}` + (cads.length > 1 ? ` · cadena ${iCad + 1} de ${cads.length}` : '');
    $('#c-cat').textContent = p.etiqueta || '';
    $('#curso-sub').textContent = modoInfo.t;
  } else {
    $('#c-cadena').textContent = p.fase ? NOMBRE[p.fase] : 'Preparando…';
    $('#c-cnt').textContent = p.fase ? `paso ${idx + 1} de ${pasos.length}` : '';
    $('#c-cat').textContent = '';
    $('#curso-sub').textContent = modoInfo.t;
  }
}

async function pollEstado() {
  clearTimeout(pollTimer);
  try {
    const e = await api(`/api/estado?desde=${logDesde}`);
    if (e.log_total < logDesde) { logLineas = []; logDesde = 0; }
    logLineas = logLineas.concat(e.log); logDesde = e.log_total;
    if (logLineas.length > 3000) logLineas = logLineas.slice(-2000);
    const antes = corriendo; corriendo = e.corriendo;
    renderLog(); renderProgreso(e);
    if (corriendo) {
      const p = e.progreso;
      setBadge('run', p.fase === 'scraping' && p.cadena ? `Corriendo · ${p.cadena} ${p.i}/${p.n}` : `Corriendo · ${NOMBRE[p.fase] || 'iniciando'}`);
    } else if (e.progreso && e.progreso.plan) {
      const p = e.progreso;
      setBadge(p.detenido ? 'warn' : p.codigo === 0 ? 'ok' : 'danger', p.detenido ? 'Detenida' : p.codigo === 0 ? 'Última corrida OK' : 'Última corrida con errores');
    } else setBadge('', 'Inactivo · listo para correr');
    if (antes && !corriendo) {
      const p = e.progreso;
      toast(p.detenido ? 'Corrida detenida.' : p.codigo === 0 ? 'La corrida terminó bien.' : 'La corrida terminó con errores. Revisá el log.', p.detenido ? 'info' : p.codigo === 0 ? 'ok' : 'error');
      cargarResumen(true); cargarHistorial();
    }
    actualizarPlan();
  } catch (err) { setBadge('danger', 'Sin conexión con el panel'); }
  pollTimer = setTimeout(pollEstado, corriendo ? 1000 : 5000);
}

$('#btn-detener').addEventListener('click', () => { $('#confirmar').hidden = false; $('#conf-si').focus(); });
$('#conf-no').addEventListener('click', () => { $('#confirmar').hidden = true; });
$('#conf-si').addEventListener('click', async () => {
  $('#confirmar').hidden = true;
  try { await api('/api/detener', {}); toast('Deteniendo…', 'info'); } catch (e) { toast(e.message, 'error'); }
});

/* ---------------- Estado por cadena y catálogo ---------------- */
let ultimoResumen = null;
// Los botones "Falta: …" dejan armada la corrida (no la lanzan): se revisa y se toca Iniciar.
function prepararCorrida(acc, cadena) {
  if (acc === 'errores') {
    filtro = 'error'; mostrarHist = 10;
    document.querySelectorAll('#tabs-hist .tab').forEach(x => x.setAttribute('aria-selected', x.dataset.f === 'error'));
    renderHistorial(); document.getElementById('h-hist').scrollIntoView({behavior: 'smooth'}); return;
  }
  if (acc === 'faltan') {
    const sem = (ultimoResumen.cadenas.find(x => x.cadena === cadena) || {}).semana || {faltan: []};
    modo = 'scraping';
    document.querySelectorAll('input[name=modo]').forEach(i => i.checked = i.value === modo);
    elegidas.clear(); elegidas.add(cadena);
    catsElegidas[cadena] = new Set(sem.faltan); abiertas.add(cadena); firmaSel = '';
    renderChips(); actualizarPlan();
    document.getElementById('h-nueva').scrollIntoView({behavior: 'smooth'});
    toast(`Listo para scrapear las ${sem.faltan.length} categorías de ${cadena} que faltan esta semana. Revisá y tocá "Iniciar corrida".`, 'info');
    return;
  }
  modo = acc;
  document.querySelectorAll('input[name=modo]').forEach(i => i.checked = i.value === acc);
  if (acc === 'scraping' && cadena) { elegidas.clear(); elegidas.add(cadena); renderChips(); }
  if (acc === 'publicar') $('#in-fotos').checked = false;
  actualizarPlan();
  document.getElementById('h-nueva').scrollIntoView({behavior: 'smooth'});
  toast(acc === 'scraping' ? `Listo para scrapear ${cadena}. Revisá y tocá "Iniciar corrida".` :
        acc === 'matching' ? 'Listo para matchear. Revisá y tocá "Iniciar corrida".' :
        'Listo para publicar (sin copiar fotos). Revisá y tocá "Iniciar corrida".', 'info');
}
async function cargarResumen(forzar) {
  try {
    const r = await api('/api/resumen' + (forzar ? '?forzar=1' : ''));
    ultimoResumen = r;
    const fresco = iso => { if (!iso) return ['danger', 'Sin datos']; const h = (Date.now() - new Date(iso)) / 36e5; return h < 30 ? ['ok', 'Al día'] : h < 80 ? ['warn', 'Atrasada'] : ['danger', 'Desactualizada']; };
    const maxN = Math.max(2, r.total_cadenas || 5);
    const barra = (cob, total, etiqueta) => {
      const segs = []; for (let n = 1; n <= maxN; n++) { const v = cob[n] || 0; if (v) segs.push(`<span class="cob-${Math.min(n, 5)}" style="width:${v / total * 100}%" title="${fmtNum(v)} ${etiqueta} en ${n} cadena${n > 1 ? 's' : ''} (${Math.round(v / total * 100)}%)"></span>`); }
      return `<div class="cob" role="img" aria-label="Distribución por cantidad de cadenas">${segs.join('')}</div>`;
    };
    let html = r.cadenas.map(c => {
      const ref = c.ultimo_scraping || c.ultima_captura;
      let [t, l] = fresco(ref);
      const sem = c.semana, faltan = sem && sem.reconocidas ? sem.faltan.length : 0;
      if (faltan && t === 'ok') [t, l] = ['warn', 'Faltan categorías'];
      else if (sem && !sem.reconocidas && t === 'ok') [t, l] = ['warn', 'Revisar categorías'];
      const total = c.productos || 1, solos = c.cobertura['1'] || 0;
      const comp = Math.round((total - solos) / total * 100);
      const acciones = [];
      if (!ref || (Date.now() - new Date(ref)) / 36e5 >= 24) acciones.push(`<button class="accion" data-acc="scraping" data-c="${esc(c.cadena)}">Scrapear</button>`);
      if (c.nuevos_sin_matchear > 0) acciones.push(`<button class="accion" data-acc="matching">Matchear ${fmtNum(c.nuevos_sin_matchear)} nuevos</button>`);
      if (c.cambios_sin_publicar > 0) acciones.push(`<button class="accion" data-acc="publicar">Publicar ${fmtNum(c.cambios_sin_publicar)} cambios</button>`);
      if (faltan) acciones.push(`<button class="accion" data-acc="faltan" data-c="${esc(c.cadena)}">Scrapear ${fmtNum(faltan)} categorías de esta semana</button>`);
      if (c.errores_recientes) acciones.push(`<button class="accion err" data-acc="errores">${c.errores_recientes} rutas con error</button>`);
      const semTxt = !sem ? '' : !sem.reconocidas
        ? `<div class="s" style="color:var(--warn)">Esta semana hubo ${fmtNum(sem.corridas)} scrapings, pero sus rutas no coinciden con las categorías de la config: no puedo decir cuáles faltan.</div>`
        : `<div class="s">Esta semana: <b class="num" style="color:var(--text)">${fmtNum(sem.hechas)} de ${fmtNum(sem.total)}</b> categorías scrapeadas</div>`;
      return `<div class="fresh-item cad">
        <div class="top"><span class="n">${esc(c.cadena)}</span><span class="badge badge--${t}"><span class="dot"></span>${l}</span></div>
        <div class="v">${fmtNum(c.productos)} <span class="u">productos</span></div>
        <div class="s" title="${ref ? new Date(ref).toLocaleString('es-BO') : ''}">Scrapeado ${ref ? `${fmtFecha(ref)} · ${haceCuanto(ref)}` : 'nunca'}</div>
        ${semTxt}
        ${barra(c.cobertura, total, 'productos')}
        <div class="s"><b class="num" style="color:var(--text)">${comp}%</b> se compara con otra cadena</div>
        <div class="acciones-cad">${acciones.length ? `<span class="s">Falta:</span>${acciones.join('')}` : '<span class="ok-txt"><svg><use href="#i-ok"/></svg>Todo al día</span>'}</div>
      </div>`;
    }).join('');

    // Catálogo completo
    const tot = r.total_productos || 1;
    const comparables = tot - (r.cobertura['1'] || 0);
    const leyenda = []; for (let n = 1; n <= maxN; n++) { const v = r.cobertura[n] || 0; leyenda.push(`<span class="ley"><i class="cob-${Math.min(n, 5)}"></i>${n} cadena${n > 1 ? 's' : ''} <b class="num">${Math.round(v / tot * 100)}%</b> <span class="s num">${fmtNum(v)}</span></span>`); }
    const pubPend = r.cadenas.reduce((s, c) => s + (c.cambios_sin_publicar || 0), 0) || (r.ultimo_matching && r.web_actualizada && new Date(r.ultimo_matching) > new Date(r.web_actualizada));
    html += `<div class="fresh-item catalogo">
      <div class="top"><span class="n">Catálogo en la web</span>${pubPend ? '<button class="accion" data-acc="publicar">Hay cambios sin publicar · Publicar</button>' : '<span class="badge badge--ok"><span class="dot"></span>Web al día</span>'}</div>
      <div class="v">${fmtNum(r.total_productos)} <span class="u">productos · <b style="color:var(--brand-ink)">${Math.round(comparables / tot * 100)}%</b> en 2 o más cadenas</span></div>
      ${barra(r.cobertura, tot, 'productos')}
      <div class="leyenda">${leyenda.join('')}</div>
      <div class="s">Web publicada ${haceCuanto(r.web_actualizada)}${r.web_actualizada ? ` (${fmtFecha(r.web_actualizada)})` : ''} · último matching ${haceCuanto(r.ultimo_matching)}</div>
    </div>`;
    try {
      const u = await api('/api/uso' + (forzar ? '?forzar=1' : ''));
      const fila = (nombre, usado, limite) => {
        if (usado == null) return `<div class="uso-fila"><span>${nombre}</span><span class="s" style="grid-column:2/4">no se pudo leer</span></div>`;
        const pct = Math.min(100, usado / limite * 100);
        return `<div class="uso-fila"><span>${nombre}</span><div class="progress mini ${pct >= 90 ? 'danger' : pct >= 75 ? 'warn' : ''}"><span style="width:${pct}%"></span></div>
          <span class="num">${fmtNum(Math.round(usado))} / ${fmtNum(limite)} MB</span></div>`;
      };
      const peor = Math.max((u.db_mb || 0) / u.limite_db_mb, (u.storage_mb || 0) / u.limite_storage_mb);
      const [t, l] = peor >= .9 ? ['danger', 'Casi lleno'] : peor >= .75 ? ['warn', 'Ojo con el espacio'] : ['ok', 'Con espacio'];
      const grande = (u.tablas || [])[0];
      html += `<div class="fresh-item uso"><div class="top"><span class="n">Supabase (plan gratis)</span><span class="badge badge--${t}"><span class="dot"></span>${l}</span></div>
        ${fila('Base', u.db_mb, u.limite_db_mb)}${fila('Fotos', u.storage_mb, u.limite_storage_mb)}
        <div class="s">${grande ? `Tabla más grande: ${esc(grande.tabla)} (${fmtNum(Math.round(grande.mb))} MB) · ` : ''}la transferencia se ve en Supabase → Usage</div></div>`;
    } catch (e) { /* sin datos de uso */ }
    $('#fresh').innerHTML = html;
    actualizarPlan();  // marca en el selector de categorías las que faltan esta semana
    document.querySelectorAll('#fresh [data-acc]').forEach(b => b.addEventListener('click', () => prepararCorrida(b.dataset.acc, b.dataset.c)));
  } catch (e) {
    $('#fresh').innerHTML = `<div class="fresh-item"><span class="n">No se pudo leer la base</span><span class="s">${esc(e.message)}</span></div>`;
  }
}

/* ---------------- Matching ---------------- */
async function cargarMatching() {
  try {
    const e = await api('/api/matching/estado');
    if (!e.existe) { $('#m-vacio').hidden = false; $('#m-info').hidden = true; }
    else {
      $('#m-vacio').hidden = true; $('#m-info').hidden = false;
      const d = e.detalle || {};
      const enCurso = e.estado === 'en_progreso';
      const pct = d.porcentaje != null ? d.porcentaje : (e.estado === 'ok' ? 100 : 0);
      $('#m-barra').firstElementChild.style.width = pct + '%';
      $('#m-sub').innerHTML = `<span class="badge ${enCurso ? 'badge--run' : e.estado === 'ok' ? 'badge--ok' : 'badge--danger'}"><span class="dot"></span>${enCurso ? `Corriendo · ${pct}%` : e.estado === 'ok' ? 'Terminado' : esc(e.estado)}</span> <span style="margin-left:6px">${fmtFecha(e.finalizado_en || e.iniciado_en)}</span>`;
      $('#m-cand').textContent = `${fmtNum(d.candidatos_procesados)} / ${fmtNum(d.candidatos_totales)}`;
      $('#m-fus').textContent = fmtNum(d.fusiones);
      $('#m-llm').textContent = fmtNum(d.llm_usados);
      $('#m-eta').textContent = enCurso && d.eta_segundos ? '≈ ' + fmtDur(d.eta_segundos) : '—';
      const cat = $('#m-cat');
      cat.hidden = !(enCurso && d.categoria_actual);
      if (d.categoria_actual) cat.innerHTML = `<svg><use href="#i-info"/></svg><span>Procesando <b>${esc(d.categoria_actual)}</b> (categoría ${d.bucket_actual} de ${d.buckets_totales})</span>`;
    }
  } catch (e) { /* se reintenta solo */ }
  setTimeout(cargarMatching, corriendo ? 4000 : 15000);
}

/* ---------------- Historial ---------------- */
let hist = [], filtro = 'todo', mostrarHist = 10;
$('#btn-hist-mas').addEventListener('click', () => { mostrarHist += 10; renderHistorial(); });
const ESTADO = {ok: ['ok', 'OK'], ok_con_errores: ['warn', 'Con errores'], error: ['danger', 'Error'], en_progreso: ['run', 'En curso']};
const TIPO = {scraper: 'Scraping', matching: 'Matching', carga_manual: 'Carga manual'};
function renderHistorial() {
  const filas = hist.filter(f => filtro === 'todo' || (filtro === 'error' ? f.estado === 'error' : f.tipo === filtro));
  const nErr = hist.filter(f => f.estado === 'error').length;
  $('#cnt-err').textContent = nErr ? nErr : '';
  const restantes = filas.length - mostrarHist;
  $('#hist-mas').hidden = restantes <= 0;
  $('#btn-hist-mas').textContent = `Ver ${Math.min(10, restantes)} más · quedan ${restantes}`;
  if (!filas.length) {
    $('#hist').innerHTML = `<tr><td colspan="7" class="vacio"><svg><use href="#i-ok"/></svg>${filtro === 'error' ? 'Ninguna corrida falló. Todo en orden.' : 'Todavía no hay corridas.'}</td></tr>`;
    return;
  }
  $('#hist').innerHTML = filas.slice(0, mostrarHist).map(f => {
    const [t, l] = ESTADO[f.estado] || ['', f.estado];
    const ruta = (f.ruta || '—').replace(/^https?:\/\/[^/]+/, '');
    return `<tr>
      <td class="num" style="white-space:nowrap">${fmtFecha(f.iniciado_en)}</td>
      <td>${TIPO[f.tipo] || esc(f.tipo)}</td>
      <td>${esc(f.cadena || '—')}</td>
      <td><div class="ruta" title="${esc(f.ruta)}">${esc(ruta)}</div>${f.error_detalle ? `<div class="err">${esc(String(f.error_detalle).slice(0, 160))}</div>` : ''}</td>
      <td><span class="badge badge--${t}"><span class="dot"></span>${l}</span></td>
      <td class="num" style="text-align:right">${f.procesados != null ? fmtNum(f.procesados) : '—'}${f.errores ? `<div class="err">${f.errores} con error</div>` : ''}</td>
      <td>${f.tipo === 'scraper' && f.estado === 'error' && f.cadena ? `<button class="btn btn-secondary btn-sm" data-retry="${esc(f.cadena)}"><svg><use href="#i-retry"/></svg>Reintentar</button>` : ''}</td>
    </tr>`;
  }).join('');
  document.querySelectorAll('[data-retry]').forEach(b => b.addEventListener('click', async () => {
    try { await api('/api/reintentar', {cadena: b.dataset.retry}); toast(`Reintentando ${b.dataset.retry} completa.`, 'ok'); logDesde = 0; logLineas = []; pollEstado(); }
    catch (e) { toast(e.message, 'error'); }
  }));
}
document.querySelectorAll('#tabs-hist .tab').forEach(t => t.addEventListener('click', () => {
  filtro = t.dataset.f; mostrarHist = 10;
  document.querySelectorAll('#tabs-hist .tab').forEach(x => x.setAttribute('aria-selected', x === t));
  renderHistorial();
}));
async function cargarHistorial() {
  try { hist = await api('/api/historial'); renderHistorial(); }
  catch (e) { $('#hist').innerHTML = `<tr><td colspan="7" class="vacio">No se pudo leer el historial: ${esc(e.message)}</td></tr>`; }
}

/* ---------------- Categorías por cadena ---------------- */
function renderCategorias() {
  $('#cats').innerHTML = DATOS.cadenas.map((c, k) => c.tipo === 'shopify' ? `
    <details class="acc" data-k="${k}">
      <summary><svg class="chev"><use href="#i-chev"/></svg><span class="n">${esc(c.cadena)}</span><span class="s">${esc(c.sitio_base || '')}</span>
        <span class="badge">${c.n == null ? 'Todas (auto)' : `${c.n} categorías activas`}</span></summary>
      <div class="acc-body">
        <div class="acc-tools">
          <button class="btn btn-secondary btn-sm" data-desc="${k}" type="button">Descubrir categorías del sitio</button>
          <input class="input" type="search" placeholder="Filtrar…" data-filtro="${k}" hidden aria-label="Filtrar categorías de ${esc(c.cadena)}">
          <button class="btn btn-ghost btn-sm" data-marcar="${k}" type="button" hidden>Marcar todas</button>
          <button class="btn btn-ghost btn-sm" data-desmarcar="${k}" type="button" hidden>Ninguna</button>
        </div>
        <div class="cols" data-cols="${k}" hidden></div>
        <p class="nota" data-ayuda="${k}"><svg><use href="#i-info"/></svg><span>Trae todas las categorías reales del sitio con su cantidad de productos, para elegir cuáles scrapear.</span></p>
        <div class="acc-foot" data-pie="${k}" hidden><span class="sel" data-sel="${k}"></span><button class="btn btn-primary btn-sm" data-guardar="${k}" type="button">Guardar selección</button></div>
      </div>
    </details>` : `
    <details class="acc">
      <summary><svg class="chev"><use href="#i-chev"/></svg><span class="n">${esc(c.cadena)}</span><span class="s">navegador (Playwright)</span><span class="badge">${c.n} URLs</span></summary>
      <div class="acc-body"><p class="nota"><svg><use href="#i-info"/></svg><span>Las URLs se editan a mano en <span class="mono">config_cadenas.json</span> (sección "navegador"). Es la cadena más lenta: ~${SEG_CATEGORIA.navegador} s por URL.</span></p></div>
    </details>`).join('');

  const contar = k => {
    const box = document.querySelector(`[data-cols="${k}"]`);
    const n = box.querySelectorAll('input:checked').length;
    document.querySelector(`[data-sel="${k}"]`).textContent = `${n} de ${box.querySelectorAll('input').length} seleccionadas`;
  };
  document.querySelectorAll('[data-desc]').forEach(b => b.addEventListener('click', async () => {
    const k = b.dataset.desc, c = DATOS.cadenas[k];
    b.disabled = true; b.innerHTML = '<span class="spinner"></span>Buscando…';
    try {
      const cols = await api(`/api/descubrir?sitio_base=${encodeURIComponent(c.sitio_base)}`);
      const activas = new Set(Array.isArray(c.colecciones) ? c.colecciones : []);
      const todas = c.colecciones === 'todas';
      const box = document.querySelector(`[data-cols="${k}"]`);
      box.innerHTML = cols.map(x => `<label class="col"><input type="checkbox" value="${esc(x.handle)}" ${todas || activas.has(x.handle) ? 'checked' : ''}>
        <span><span>${esc(x.titulo)}</span> <span class="h">/${esc(x.handle)}</span></span><span class="c">${fmtNum(x.productos)}</span></label>`).join('');
      box.hidden = false;
      ['filtro', 'marcar', 'desmarcar'].forEach(a => document.querySelector(`[data-${a}="${k}"]`).hidden = false);
      document.querySelector(`[data-pie="${k}"]`).hidden = false;
      document.querySelector(`[data-ayuda="${k}"]`).hidden = true;
      box.addEventListener('change', () => contar(k));
      contar(k);
      b.textContent = 'Volver a buscar';
    } catch (e) { toast(`No se pudieron traer las categorías: ${e.message}`, 'error'); b.textContent = 'Descubrir categorías del sitio'; }
    b.disabled = false;
  }));
  document.querySelectorAll('[data-filtro]').forEach(i => i.addEventListener('input', () => {
    const q = i.value.toLowerCase();
    document.querySelectorAll(`[data-cols="${i.dataset.filtro}"] .col`).forEach(l => l.hidden = q && !l.textContent.toLowerCase().includes(q));
  }));
  const marcar = (k, v) => { document.querySelectorAll(`[data-cols="${k}"] .col:not([hidden]) input`).forEach(i => i.checked = v); contar(k); };
  document.querySelectorAll('[data-marcar]').forEach(b => b.addEventListener('click', () => marcar(b.dataset.marcar, true)));
  document.querySelectorAll('[data-desmarcar]').forEach(b => b.addEventListener('click', () => marcar(b.dataset.desmarcar, false)));
  document.querySelectorAll('[data-guardar]').forEach(b => b.addEventListener('click', async () => {
    const k = b.dataset.guardar, c = DATOS.cadenas[k];
    const handles = [...document.querySelectorAll(`[data-cols="${k}"] input:checked`)].map(i => i.value);
    if (!handles.length) { toast('Elegí al menos una categoría.', 'error'); return; }
    try {
      await api('/api/guardar_colecciones', {cadena: c.cadena, handles});
      const previas = Object.fromEntries((c.categorias || []).map(x => [x.id, x]));
      c.categorias = handles.map(h => previas[h] || {id: h, grupo: '',
        nombre: document.querySelector(`[data-cols="${k}"] input[value="${CSS.escape(h)}"]`)?.closest('.col').querySelector('span span').textContent || h});
      catsElegidas[c.cadena] = new Set(handles); firmaSel = '';
      c.colecciones = handles; c.n = handles.length;
      document.querySelector(`.acc[data-k="${k}"] summary .badge`).textContent = `${handles.length} categorías activas`;
      renderChips(); actualizarPlan();
      toast(`${c.cadena}: ${handles.length} categorías guardadas para las próximas corridas.`, 'ok');
    } catch (e) { toast(e.message, 'error'); }
  }));
}

/* ---------------- Unir / separar a mano ---------------- */
let grupos = [], seleccion = new Set(), ultimaBusqueda = '';
const fmtBs = n => n == null ? '—' : 'Bs ' + n.toLocaleString('es-BO', {minimumFractionDigits: 2, maximumFractionDigits: 2});
function renderGrupos() {
  const box = $('#grupos');
  if (!ultimaBusqueda) { box.innerHTML = ''; }
  else if (!grupos.length) box.innerHTML = `<div class="vacio" style="grid-column:1/-1"><svg><use href="#i-empty"/></svg>No encontré productos con "${esc(ultimaBusqueda)}". Probá con menos palabras.</div>`;
  else box.innerHTML = grupos.map(g => {
    const img = (g.listados.find(l => l.imagen) || {}).imagen;
    const cads = [...new Set(g.listados.map(l => l.cadena))];
    const manual = g.listados.some(l => l.metodo === 'manual');
    const repetidas = cads.filter(c => g.listados.filter(l => l.cadena === c).length > 1);
    return `<article class="grupo ${seleccion.has(g.producto_id) ? 'sel' : ''}" data-g="${g.producto_id}">
      <div class="grupo-head" role="checkbox" tabindex="0" aria-checked="${seleccion.has(g.producto_id)}" aria-label="Seleccionar ${esc(g.nombre)}">
        ${img ? `<img src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.outerHTML='<span class=ph>sin foto</span>'">` : '<span class="ph">sin foto</span>'}
        <div class="info"><div class="grupo-t">${esc(g.nombre)}</div>
          <div class="grupo-meta"><span class="badge ${cads.length > 1 ? 'badge--ok' : ''}">${cads.length > 1 ? `En ${cads.length} cadenas` : `Solo en ${esc(cads[0])}`}</span>${manual ? '<span class="badge">Revisado a mano</span>' : ''}</div></div>
        <span class="grupo-ck" aria-hidden="true"><svg><use href="#i-check"/></svg></span>
      </div>
      ${repetidas.length ? `<div class="lst" style="background:var(--warn-soft);color:var(--warn)">
        <span style="flex:1">${repetidas.map(esc).join(', ')}: el mismo producto aparece dos veces.</span>
        <button class="btn btn-secondary btn-sm" type="button" data-dejaruno="${g.producto_id}">Dejar uno solo</button></div>` : ''}
      <div class="listados">${g.listados.map(l => `<div class="lst">
        <span class="cad">${esc(l.cadena)}</span><span class="nom" title="${esc(l.nombre)} · código ${esc(l.codigo)}">${esc(l.nombre)}</span><span class="pre">${fmtBs(l.precio)}</span>
        ${g.listados.length > 1 ? `<button class="btn btn-ghost btn-sm" type="button" data-separar="${l.id}" title="Sacar ${esc(l.cadena)} de este producto">Separar</button>` : ''}
      </div>`).join('')}</div>
    </article>`;
  }).join('');
  const n = seleccion.size;
  $('#barra-union').hidden = n < 1;
  $('#union-ok').disabled = n < 2;
  $('#union-txt').innerHTML = n < 2 ? 'Elegí <b>otro</b> producto para unir con este.' : `<b>${n} productos</b> seleccionados · en la web se van a ver como uno solo.`;
  document.querySelectorAll('.grupo-head').forEach(h => {
    const toggle = () => { const id = h.parentElement.dataset.g; seleccion.has(id) ? seleccion.delete(id) : seleccion.add(id); renderGrupos(); };
    h.addEventListener('click', toggle);
    h.addEventListener('keydown', ev => { if (ev.key === ' ' || ev.key === 'Enter') { ev.preventDefault(); toggle(); } });
  });
  document.querySelectorAll('[data-dejaruno]').forEach(b => b.addEventListener('click', async () => {
    b.disabled = true;
    try { const r = await api('/api/match/dejar_uno', {producto_id: b.dataset.dejaruno}); toast(`Listo: se quitó ${r.quitados} repetido${r.quitados === 1 ? '' : 's'} (queda el más reciente).`, 'ok'); $('#aviso-publicar').hidden = false; await buscar(ultimaBusqueda); }
    catch (e) { toast(e.message, 'error'); b.disabled = false; }
  }));
  document.querySelectorAll('[data-separar]').forEach(b => b.addEventListener('click', async () => {
    b.disabled = true;
    try { await api('/api/match/separar', {listado_id: b.dataset.separar}); toast('Separado. El matching automático no los va a volver a unir.', 'ok'); $('#aviso-publicar').hidden = false; await buscar(ultimaBusqueda); }
    catch (e) { toast(e.message, 'error'); b.disabled = false; }
  }));
}
async function buscar(q) {
  ultimaBusqueda = q.trim();
  if (ultimaBusqueda.length < 2) { grupos = []; renderGrupos(); return; }
  $('#grupos').innerHTML = '<div class="vacio" style="grid-column:1/-1"><span class="spinner" style="display:inline-block;width:18px;height:18px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:giro .8s linear infinite"></span></div>';
  try {
    grupos = await api('/api/match/buscar?q=' + encodeURIComponent(ultimaBusqueda));
    const ids = new Set(grupos.map(g => g.producto_id));
    seleccion = new Set([...seleccion].filter(id => ids.has(id)));
  } catch (e) { grupos = []; toast(e.message, 'error'); }
  renderGrupos();
}
$('#form-buscar').addEventListener('submit', ev => { ev.preventDefault(); seleccion.clear(); buscar($('#in-buscar').value); });
$('#union-cancelar').addEventListener('click', () => { seleccion.clear(); renderGrupos(); });
$('#union-ok').addEventListener('click', async () => {
  const btn = $('#union-ok'); btn.disabled = true;
  try {
    const r = await api('/api/match/unir', {productos: [...seleccion]});
    toast(`Listo: ${r.unidos} productos unidos en uno.` + (r.duplicados_quitados ? ` Se quitó ${r.duplicados_quitados} repetido de la misma cadena.` : ''), 'ok');
    seleccion.clear(); $('#aviso-publicar').hidden = false; await buscar(ultimaBusqueda);
  } catch (e) { toast(e.message, 'error'); btn.disabled = false; }
});
$('#btn-publicar-ya').addEventListener('click', async () => {
  try {
    await api('/api/correr', {modo: 'web'});
    toast('Publicando en la web… lo ves en "Corrida".', 'ok'); $('#aviso-publicar').hidden = true;
    logDesde = 0; logLineas = []; pollEstado(); document.getElementById('h-curso').scrollIntoView({behavior: 'smooth'});
  } catch (e) { toast(e.message, 'error'); }
});

renderModos(); renderChips(); actualizarPlan(); renderCategorias();
pollEstado(); cargarResumen(); cargarMatching(); cargarHistorial(); cargarCandados();
setInterval(cargarHistorial, 20000);
setInterval(() => cargarResumen(), 60000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Panel en http://localhost:5050")
    app.run(port=5050, debug=False, threaded=True)

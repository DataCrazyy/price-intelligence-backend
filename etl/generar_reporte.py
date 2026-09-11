"""
generar_reporte.py — Genera un reporte PDF ejecutivo tipo "revista"
(estilo editorial, a la The Economist), brandeado con Farmacorp, con
foto real de producto, comparación de precios y un análisis corto
generado por IA (OpenAI) sobre los resultados -- filtrable por
categoría y/o por las cadenas competidoras que se quieran incluir.

Pensado para correrse a mano cuando querés un entregable ejecutivo para
mandar/mostrar, no para automatizarse todavía (a diferencia de
run_all.py). Usa el mismo data/precios.json que ya genera el pipeline
-- no vuelve a scrapear ni toca Postgres.

Sale en 2 páginas fijas: la página 1 es el "pantallazo" -- todo lo que
un gerente necesita ver de una mirada (KPIs, panorama + recomendación
de la IA, y dispersión por categoría) -- y la página 2 es el detalle
(productos puntuales a revisar y comparación por cadena).

El PDF se renderiza con Chromium headless (vía Playwright) en vez de
wkhtmltopdf -- wkhtmltopdf usa un motor viejo que no soporta bien
flexbox/grid y desarma cualquier layout con columnas. Chromium es el
mismo motor real de Chrome, así que el PDF sale con el layout tal cual
se diseña acá.

Requiere:
    pip install requests playwright
    playwright install chromium
    Variable de entorno OPENAI_API_KEY con tu API key (nunca hardcodeada
    acá -- si no está seteada, el reporte se genera igual con un
    resumen automático simple en vez del análisis de IA).

Uso:
    python generar_reporte.py
    python generar_reporte.py --categoria "Cuidado Personal"
    python generar_reporte.py --cadenas Fidalga,Hipermaxi
    python generar_reporte.py --categoria "Leches Y Formulas" --cadenas "Farmacias Chavez" --salida reporte_leches.pdf
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import requests

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Falta playwright -- corré: pip install playwright && playwright install chromium", file=sys.stderr)
    raise

ROOT = Path(__file__).parent.parent.parent
DATA_PATH = ROOT / "data" / "precios.json"
LOGOS_DIR = ROOT / "assets" / "logos"
REPORTS_DIR = ROOT / "reportes"

CLIENTE = "Farmacorp"

# Mismo mapa de marca que dashboard.html (CHAIN_META) -- único lugar a
# tocar acá si cambia un logo o color, igual que en el dashboard.
CHAIN_META = {
    "Farmacorp": {"color": "#0B3D63", "logo": LOGOS_DIR / "farmacorp.jpg"},
    "Fidalga": {"color": "#39A935", "logo": LOGOS_DIR / "fidalga.jpg"},
    "Amarket": {"color": "#E31E24", "logo": LOGOS_DIR / "amarket.jpg"},
    "Hipermaxi": {"color": "#F26522", "logo": LOGOS_DIR / "hipermaxi.jpg"},
    "Farmacias Chavez": {"color": "#00AEEF", "logo": LOGOS_DIR / "chavez.png"},
}

OPENAI_MODEL = os.environ.get("OPENAI_REPORT_MODEL", "gpt-4o-mini")

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def log(msg):
    print(f"[reporte] {msg}", flush=True)


def meta_for(cadena):
    return CHAIN_META.get(cadena, {"color": "#6B7280", "logo": None})


# ---------------------------------------------------------------------
# 1. Datos + comparaciones (misma lógica que construirComparaciones() en
#    dashboard.html, portada a Python para no depender del navegador).
# ---------------------------------------------------------------------

def cargar_datos():
    if not DATA_PATH.exists():
        sys.exit(f"No existe {DATA_PATH} -- corré export_json.py / run_all.py primero.")
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def construir_comparaciones(productos, cadenas_incluidas):
    grupos = {}
    for p in productos:
        grupos.setdefault(p["producto_clave"], []).append(p)

    filas = []
    for grupo in grupos.values():
        propio = next((p for p in grupo if p["cadena"] == CLIENTE), None)
        rivales = [p for p in grupo if p["cadena"] in cadenas_incluidas]
        if not propio or not rivales:
            continue
        mejor_rival = min(rivales, key=lambda r: r["precio_oferta"])
        precio_propio = propio["precio_oferta"]
        gap = (precio_propio - mejor_rival["precio_oferta"]) / mejor_rival["precio_oferta"] * 100 if mejor_rival["precio_oferta"] else 0
        por_cadena = {r["cadena"]: r["precio_oferta"] for r in rivales}
        filas.append({
            "clave": propio["producto_clave"],
            "nombre": propio["nombre"],
            "imagen": propio.get("imagen") or mejor_rival.get("imagen"),
            "categoria": propio.get("categoria") or propio.get("subcategoria") or "Sin categoría",
            "precio_propio": precio_propio,
            "precio_rival": mejor_rival["precio_oferta"],
            "rival_cadena": mejor_rival["cadena"],
            "por_cadena": por_cadena,
            "gap": gap,
        })
    return filas


def filtrar_por_categoria(filas, categoria):
    if not categoria or categoria.lower() == "todas":
        return filas
    cat_norm = categoria.strip().lower()
    return [f for f in filas if f["categoria"].strip().lower() == cat_norm]


# ---------------------------------------------------------------------
# 2. Resumen numérico -- lo que se manda a la IA y lo que arma las
#    tarjetas de destacados/categorías/cadenas del PDF.
# ---------------------------------------------------------------------

def calcular_resumen(filas, cadenas_incluidas):
    total = len(filas)
    if total == 0:
        return None

    mas_baratos = [f for f in filas if f["gap"] < -1]
    mas_caros = [f for f in filas if f["gap"] > 1]
    gap_prom = sum(f["gap"] for f in filas) / total

    ordenados = sorted(filas, key=lambda f: f["gap"])
    n_each = min(4, total // 2) or (1 if total == 1 else 0)
    top_ventaja = ordenados[:n_each] if n_each else (ordenados[:1] if total == 1 and ordenados[0]["gap"] <= 0 else [])
    top_riesgo = list(reversed(ordenados[total - n_each:])) if n_each else (ordenados[:1] if total == 1 and ordenados[0]["gap"] > 0 else [])

    por_cat = {}
    for f in filas:
        por_cat.setdefault(f["categoria"], []).append(f["gap"])
    categorias = sorted(
        [{"categoria": c, "gap_prom": sum(g) / len(g), "n": len(g)} for c, g in por_cat.items()],
        key=lambda x: -abs(x["gap_prom"])
    )

    por_cadena = []
    for cadena in sorted(cadenas_incluidas):
        gaps = [
            (f["precio_propio"] - f["por_cadena"][cadena]) / f["por_cadena"][cadena] * 100
            for f in filas if cadena in f["por_cadena"] and f["por_cadena"][cadena]
        ]
        if gaps:
            por_cadena.append({"cadena": cadena, "gap_prom": sum(gaps) / len(gaps), "n": len(gaps)})

    return {
        "total": total,
        "mas_baratos": len(mas_baratos),
        "mas_caros": len(mas_caros),
        "gap_prom": gap_prom,
        "top_ventaja": top_ventaja,
        "top_riesgo": top_riesgo,
        "categorias": categorias,
        "por_cadena": por_cadena,
    }


# ---------------------------------------------------------------------
# 3. Análisis con IA (OpenAI) -- resumen ejecutivo tipo "insight de
#    gerencia". Si no hay API key o falla la llamada, cae a un texto
#    generado con reglas simples para que el reporte siga saliendo.
# ---------------------------------------------------------------------

def _resumen_fallback(resumen, categoria, cadenas):
    cats = ", ".join(c["categoria"] for c in resumen["categorias"][:3])
    return {
        "titulo": "Panorama de precios" + (f" — {categoria}" if categoria and categoria.lower() != "todas" else ""),
        "bajada": f"Comparación de Farmacorp contra {', '.join(sorted(cadenas))}.",
        "resumen_ejecutivo": (
            f"Sobre {resumen['total']} SKUs comparados, Farmacorp está más barato en "
            f"{resumen['mas_baratos']} y más caro en {resumen['mas_caros']}, con una brecha "
            f"promedio de {resumen['gap_prom']:+.1f}%. Las categorías con mayor peso en esta "
            f"comparación son {cats}."
        ),
        "hallazgos": [
            f"{c['categoria']}: brecha promedio de {c['gap_prom']:+.1f}% sobre {c['n']} SKUs."
            for c in resumen["categorias"][:4]
        ],
        "riesgos": [
            f"{f['nombre']} está {f['gap']:+.1f}% más caro que {f['rival_cadena']}."
            for f in resumen["top_riesgo"][:3]
        ],
        "recomendacion": "Revisar primero los SKUs con mayor brecha en contra antes de ajustar precio o promoción.",
    }


def generar_insight_ia(resumen, categoria, cadenas):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log("OPENAI_API_KEY no está seteada -- se genera el reporte con un resumen automático simple, sin IA.")
        return _resumen_fallback(resumen, categoria, cadenas)

    payload_datos = {
        "cliente": CLIENTE,
        "competidores_incluidos": sorted(cadenas),
        "categoria_filtrada": categoria or "Todas",
        "total_skus_comparados": resumen["total"],
        "mas_baratos_que_competencia": resumen["mas_baratos"],
        "mas_caros_que_competencia": resumen["mas_caros"],
        "brecha_promedio_pct": round(resumen["gap_prom"], 1),
        "categorias": [
            {"categoria": c["categoria"], "brecha_promedio_pct": round(c["gap_prom"], 1), "skus": c["n"]}
            for c in resumen["categorias"][:8]
        ],
        "por_cadena": [
            {"cadena": c["cadena"], "brecha_promedio_pct": round(c["gap_prom"], 1), "skus": c["n"]}
            for c in resumen["por_cadena"]
        ],
        "productos_mayor_riesgo": [
            {"nombre": f["nombre"], "categoria": f["categoria"], "vs": f["rival_cadena"], "brecha_pct": round(f["gap"], 1)}
            for f in resumen["top_riesgo"][:5]
        ],
        "productos_mejor_posicionados": [
            {"nombre": f["nombre"], "categoria": f["categoria"], "vs": f["rival_cadena"], "brecha_pct": round(f["gap"], 1)}
            for f in resumen["top_ventaja"][:5]
        ],
    }

    system_prompt = (
        "Sos un analista senior de pricing retail. Te paso datos agregados de una comparación de "
        "precios entre Farmacorp (farmacia) y su competencia en Bolivia. Escribí un análisis corto, "
        "ejecutivo, en español boliviano neutro, para gerencia comercial -- directo, sin relleno, "
        "basado SOLO en los números que te paso (no inventes cifras). Respondé ÚNICAMENTE con un "
        "objeto JSON con estas claves exactas: "
        "titulo (string, corto, tipo titular de revista), bajada (string, una línea, tipo bajada editorial), "
        "resumen_ejecutivo (string, 2-3 frases), "
        "hallazgos (array de 3-4 strings cortos), "
        "riesgos (array de 2-3 strings cortos), "
        "recomendacion (string, 1-2 frases, accionable)."
    )

    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_MODEL,
                "response_format": {"type": "json_object"},
                "temperature": 0.4,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload_datos, ensure_ascii=False)},
                ],
            },
            timeout=45,
        )
        r.raise_for_status()
        contenido = r.json()["choices"][0]["message"]["content"]
        insight = json.loads(contenido)
        for clave in ("titulo", "bajada", "resumen_ejecutivo", "hallazgos", "riesgos", "recomendacion"):
            insight.setdefault(clave, _resumen_fallback(resumen, categoria, cadenas)[clave])
        return insight
    except Exception as e:
        log(f"Falló la llamada a OpenAI ({e}) -- se usa el resumen automático simple en su lugar.")
        return _resumen_fallback(resumen, categoria, cadenas)


# ---------------------------------------------------------------------
# 4. Fotos reales de producto -- se bajan a una carpeta temporal para
#    que Chromium las pueda cargar como file:// sin depender de que la
#    red siga arriba al momento de imprimir.
# ---------------------------------------------------------------------

def descargar_imagen(url, destino):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        destino.write_bytes(r.content)
        return True
    except Exception:
        return False


def preparar_imagenes(filas_destacadas, carpeta_temp):
    rutas = {}
    for f in filas_destacadas:
        if not f.get("imagen"):
            continue
        destino = carpeta_temp / f"{f['clave'][:40].replace('/', '_')}.jpg"
        if descargar_imagen(f["imagen"], destino):
            rutas[f["clave"]] = destino
    return rutas


# ---------------------------------------------------------------------
# 5. Armado del HTML "revista" (layout editorial, a la The Economist:
#    masthead con doble filete, cuerpo a dos columnas con capitular,
#    grillas de producto/cadena con CSS Grid real) y export a PDF con
#    Chromium headless.
# ---------------------------------------------------------------------

def esc(x):
    return (str(x or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def money(x):
    return f"Bs {x:,.2f}"


def fecha_larga(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{d.day} de {MESES[d.month - 1]} de {d.year}"
    except Exception:
        return iso


def tarjeta_producto(f, imagenes_locales):
    img_path = imagenes_locales.get(f["clave"])
    img_html = (
        f'<img src="file:///{img_path.as_posix()}" class="prod-img">'
        if img_path else '<div class="prod-img prod-img--vacio">Sin imagen</div>'
    )
    cls = "win" if f["gap"] < -1 else ("lose" if f["gap"] > 1 else "flat")
    signo = "▲" if f["gap"] > 1 else ("▼" if f["gap"] < -1 else "■")
    return f"""
    <figure class="prod-card card">
        <div class="prod-img-wrap">
            {img_html}
            <span class="prod-pill {cls}">{signo} {f['gap']:+.1f}%</span>
        </div>
        <figcaption>
            <div class="prod-nombre">{esc(f['nombre'])}</div>
            <div class="prod-cat">{esc(f['categoria'])}</div>
            <div class="prod-precios">{money(f['precio_propio'])} <span class="vs">vs</span> {money(f['precio_rival'])}
                <span class="tag-cadena">{esc(f['rival_cadena'])}</span></div>
        </figcaption>
    </figure>"""


CAT_ESCALA = 30  # % de brecha al que se satura la barra (igual que el dashboard)


def fila_categoria(c):
    pct = max(-CAT_ESCALA, min(CAT_ESCALA, c["gap_prom"]))
    ancho = abs(pct) / CAT_ESCALA * 50  # mitad de la pista por lado, desde el centro
    cls = "win" if c["gap_prom"] < -1 else ("lose" if c["gap_prom"] > 1 else "flat")
    lado = "left" if c["gap_prom"] < 0 else "right"
    return f"""
    <div class="cat-row">
        <div class="cat-info">
            <div class="cat-name">{esc(c['categoria'])}</div>
            <div class="cat-n">{c['n']} SKUs</div>
        </div>
        <div class="cat-bar-track">
            <div class="cat-bar-mid"></div>
            <div class="cat-bar-fill {cls} {lado}" style="width:{ancho:.1f}%"></div>
        </div>
        <div class="cat-val {cls}">{c['gap_prom']:+.1f}%</div>
    </div>"""


def tarjeta_cadena(c):
    m = meta_for(c["cadena"])
    logo_html = (
        f'<img src="file:///{m["logo"].as_posix()}" class="cadena-logo">'
        if m["logo"] and m["logo"].exists() else f'<span class="cadena-nombre">{esc(c["cadena"])}</span>'
    )
    cls = "win" if c["gap_prom"] < -1 else ("lose" if c["gap_prom"] > 1 else "flat")
    return f"""
    <div class="cadena-card card">
        <div class="cadena-logo-wrap">{logo_html}</div>
        <div class="cadena-stat {cls}">{c['gap_prom']:+.1f}%</div>
        <div class="cadena-sub">{c['n']} SKUs comparados</div>
    </div>"""


CSS = """
    @page { size: A4; margin: 9mm 10mm 7mm }
    * { box-sizing: border-box }
    html { background: #F5F5F7 }
    body {
        margin: 0; padding: 0; background: #F5F5F7;
        font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif;
        color: #1D1D1F; font-size: 9.4pt; line-height: 1.42;
        -webkit-font-smoothing: antialiased;
    }
    .card { background: #fff; border-radius: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }

    .kicker {
        font-size: 7.6pt; font-weight: 600; letter-spacing: .07em; text-transform: uppercase;
        color: #86868B; margin-bottom: 3mm; display: flex; align-items: center; gap: 7px;
    }
    .kicker .isologo { height: 13px; width: auto }
    .masthead { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 4.5mm 6mm }
    .masthead h1 {
        font-size: 25pt; font-weight: 700; line-height: 1.08; margin: 0; letter-spacing: -.02em;
        color: #1D1D1F; max-width: 460px;
    }
    .masthead .bajada {
        color: #6E6E73; font-weight: 400; font-size: 10.5pt; margin-top: 2mm; max-width: 440px; line-height: 1.4;
    }
    .masthead .meta {
        text-align: right; font-size: 7.6pt; color: #86868B; line-height: 1.7; white-space: nowrap; padding-top: 1mm;
    }
    .meta b { color: #1D1D1F; font-weight: 600 }

    .kpi-strip {
        display: grid; grid-template-columns: repeat(4, 1fr); gap: 3mm; margin: 4mm 0 3.5mm;
    }
    .kpi { padding: 4.5mm 4mm; text-align: center }
    .kpi .label { font-size: 7.2pt; font-weight: 500; text-transform: uppercase; letter-spacing: .05em; color: #86868B }
    .kpi .val { font-size: 21pt; font-weight: 700; margin-top: 1.5mm; letter-spacing: -.02em; color: #1D1D1F }
    .kpi .val.win { color: #1EA672 }
    .kpi .val.lose { color: #E0383E }

    .body-grid { display: grid; grid-template-columns: 62fr 36fr; gap: 3.5mm; margin-bottom: 3mm }
    .analisis { padding: 3.8mm 5mm }
    .analisis h2 { font-size: 12.5pt; font-weight: 700; margin: 0 0 2.5mm; letter-spacing: -.01em }
    .analisis p.lead { margin: 0 0 3mm; font-size: 9.4pt; color: #3A3A3C; line-height: 1.5 }
    .analisis h3 {
        font-size: 7.4pt; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: #86868B;
        margin: 3mm 0 1.5mm;
    }
    .analisis ul { margin: 0; padding-left: 0; list-style: none; font-size: 8.9pt }
    .analisis li {
        margin-bottom: 1.6mm; padding-left: 12px; position: relative; color: #3A3A3C; line-height: 1.4;
    }
    .analisis li::before {
        content: ""; position: absolute; left: 0; top: 5.5px; width: 5px; height: 5px;
        border-radius: 50%; background: #0B3D63;
    }

    .sidebar {
        background: linear-gradient(155deg, #0B3D63, #123C61 60%, #1a4a75);
        color: #fff; padding: 3.8mm 5mm; display: flex; flex-direction: column; justify-content: center;
    }
    .sidebar h3 {
        font-size: 7.4pt; font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
        color: rgba(255,255,255,.62); margin: 0 0 2.5mm;
    }
    .sidebar .pullquote {
        font-size: 12pt; font-weight: 600; line-height: 1.35; margin: 0; letter-spacing: -.01em;
    }

    .section-head { margin: 0 0 2.5mm; padding: 0 1mm }
    .section-head h2 { font-size: 13.5pt; font-weight: 700; margin: 0; letter-spacing: -.01em }
    .section-head .sub { font-size: 8pt; color: #86868B; margin-top: .5mm; display: block }

    .prod-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 3mm; margin-bottom: 5mm }
    .prod-card { margin: 0; overflow: hidden; break-inside: avoid }
    .prod-img-wrap { position: relative; background: #F5F5F7; border-radius: 16px 16px 0 0; overflow: hidden }
    .prod-img { display: block; width: 100%; height: 21mm; object-fit: contain; padding: 2mm }
    .prod-img--vacio {
        width: 100%; height: 21mm; display: flex; align-items: center; justify-content: center;
        color: #C7C7CC; font-size: 7.4pt; font-weight: 500; text-transform: uppercase; letter-spacing: .04em;
    }
    .prod-pill {
        position: absolute; top: 6px; right: 6px; font-size: 7.6pt; font-weight: 700;
        padding: 3px 8px; border-radius: 999px; color: #fff; letter-spacing: 0;
    }
    .prod-pill.win { background: #1EA672 }
    .prod-pill.lose { background: #E0383E }
    .prod-pill.flat { background: #0B3D63 }
    .prod-card figcaption { padding: 3mm 3.2mm 3.5mm }
    .prod-nombre { font-size: 8.6pt; font-weight: 600; line-height: 1.25; min-height: 18pt; color: #1D1D1F }
    .prod-cat { font-size: 7pt; color: #86868B; margin-top: .8mm }
    .prod-precios { font-size: 8pt; margin-top: 2mm; color: #3A3A3C }
    .prod-precios .vs { color: #C7C7CC; font-weight: 400 }
    .tag-cadena { color: #86868B; font-size: 7pt }

    .cadena-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 3mm; margin-bottom: 3mm }
    .cadena-card { padding: 3mm 3mm; text-align: center }
    .cadena-logo-wrap { height: 20px; display: flex; align-items: center; justify-content: center; margin-bottom: 3mm }
    .cadena-logo { max-height: 20px; max-width: 100%; object-fit: contain }
    .cadena-nombre { font-weight: 600; font-size: 8.6pt }
    .cadena-stat { font-size: 16pt; font-weight: 700; letter-spacing: -.01em }
    .cadena-stat.win { color: #1EA672 }
    .cadena-stat.lose { color: #E0383E }
    .cadena-stat.flat { color: #0B3D63 }
    .cadena-sub { font-size: 7pt; color: #86868B; margin-top: 1mm }

    .cat-list { padding: 1mm 5mm 3mm }
    .cat-row { display: grid; grid-template-columns: 33fr 44fr 14fr; align-items: center; gap: 4mm; padding: 2.6mm 0; border-top: 1px solid #EDEDED }
    .cat-row:first-child { border-top: none }
    .cat-name { font-size: 8.6pt; font-weight: 600; color: #1D1D1F }
    .cat-n { font-size: 7pt; color: #86868B; margin-top: .3mm }
    .cat-bar-track { position: relative; height: 6px; background: #F0F0F2; border-radius: 999px }
    .cat-bar-mid { position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: #D5D5D7 }
    .cat-bar-fill { position: absolute; top: 0; bottom: 0; border-radius: 999px }
    .cat-bar-fill.left { right: 50% }
    .cat-bar-fill.right { left: 50% }
    .cat-bar-fill.win { background: #1EA672 }
    .cat-bar-fill.lose { background: #E0383E }
    .cat-bar-fill.flat { background: #0B3D63 }
    .cat-val { font-size: 8.6pt; font-weight: 700; text-align: right }
    .cat-val.win { color: #1EA672 }
    .cat-val.lose { color: #E0383E }
    .cat-val.flat { color: #0B3D63 }

    .page-break { break-before: page }
    .page2-head {
        display: flex; align-items: center; justify-content: space-between;
        margin-bottom: 4mm; padding: 0 1mm;
    }
    .page2-head .kicker { margin-bottom: 0 }
    .page2-head .page-tag { font-size: 7.6pt; color: #86868B; font-weight: 600 }

    .foot {
        margin-top: 2mm; padding: 0 1mm;
        display: flex; justify-content: space-between; font-size: 7pt; color: #AEAEB2; font-weight: 500;
    }
"""


def construir_html(resumen, insight, categoria, cadenas, imagenes_locales, fecha_datos):
    isologo = LOGOS_DIR / "farmacorp-isologo.png"
    isologo_html = f'<img src="file:///{isologo.as_posix()}" class="isologo">' if isologo.exists() else ""

    destacados_html = "".join(
        tarjeta_producto(f, imagenes_locales) for f in (resumen["top_riesgo"] + resumen["top_ventaja"])
    )
    cadenas_html = "".join(tarjeta_cadena(c) for c in resumen["por_cadena"])
    hallazgos_html = "".join(f"<li>{esc(h)}</li>" for h in insight["hallazgos"])
    riesgos_html = "".join(f"<li>{esc(r)}</li>" for r in insight["riesgos"])
    categorias_html = "".join(fila_categoria(c) for c in resumen["categorias"][:10])
    categoria_label = categoria if categoria and categoria.lower() != "todas" else "Todo el catálogo"
    footer_gen = f"Generado automáticamente por el panel de Prometeo Tech para Farmacorp · {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    footer_nota = "Solo productos con match confirmado entre cadenas."

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<style>{CSS}</style></head>
<body>
    <!-- Página 1 -- lo que un gerente necesita ver de un pantallazo: KPIs,
         panorama + recomendación, y dónde está parada cada categoría. -->
    <div class="kicker">{isologo_html} Prometeo Tech · Inteligencia de precios</div>
    <div class="masthead card">
        <div>
            <h1>{esc(insight['titulo'])}</h1>
            <div class="bajada">{esc(insight['bajada'])}</div>
        </div>
        <div class="meta">
            <b>Farmacorp</b> vs. {esc(', '.join(sorted(cadenas)))}<br>
            Categoría: <b>{esc(categoria_label)}</b><br>
            Datos al {esc(fecha_larga(fecha_datos))}
        </div>
    </div>

    <div class="kpi-strip">
        <div class="kpi card"><div class="label">SKUs comparados</div><div class="val">{resumen['total']}</div></div>
        <div class="kpi card"><div class="label">Más baratos</div><div class="val win">{resumen['mas_baratos']}</div></div>
        <div class="kpi card"><div class="label">Más caros</div><div class="val lose">{resumen['mas_caros']}</div></div>
        <div class="kpi card"><div class="label">Brecha promedio</div><div class="val">{resumen['gap_prom']:+.1f}%</div></div>
    </div>

    <div class="body-grid">
        <div class="analisis card">
            <h2>Panorama</h2>
            <p class="lead">{esc(insight['resumen_ejecutivo'])}</p>
            <h3>Hallazgos por categoría</h3>
            <ul>{hallazgos_html}</ul>
            <h3>Riesgos a revisar primero</h3>
            <ul>{riesgos_html}</ul>
        </div>
        <div class="sidebar card">
            <h3>Recomendación</h3>
            <p class="pullquote">{esc(insight['recomendacion'])}</p>
        </div>
    </div>

    <div class="section-head">
        <h2>Dispersión por categoría</h2>
        <span class="sub">Brecha promedio de Farmacorp frente al mejor precio de la competencia, por categoría</span>
    </div>
    <div class="cat-list card">{categorias_html}</div>

    <div class="foot">
        <span>{footer_gen}</span>
        <span>Página 1 de 2</span>
    </div>

    <!-- Página 2 -- el detalle: qué SKUs puntuales revisar y cómo estamos
         parados frente a cada cadena competidora. -->
    <div class="page-break"></div>
    <div class="page2-head">
        <div class="kicker">{isologo_html} Prometeo Tech · Inteligencia de precios</div>
        <span class="page-tag">{esc(insight['titulo'])} · Detalle</span>
    </div>

    <div class="section-head">
        <h2>Productos en la mira</h2>
        <span class="sub">Mayor riesgo y mayor ventaja frente a la competencia</span>
    </div>
    <div class="prod-grid">{destacados_html}</div>

    <div class="section-head">
        <h2>Por cadena</h2>
        <span class="sub">Brecha promedio de Farmacorp frente a cada competidor incluido</span>
    </div>
    <div class="cadena-grid">{cadenas_html}</div>

    <div class="foot">
        <span>{footer_nota}</span>
        <span>Página 2 de 2</span>
    </div>
</body></html>"""


def render_pdf(html, salida):
    # Layout fijo a 2 páginas (ver el <div class="page-break"> en el HTML):
    # página 1 = el pantallazo ejecutivo, página 2 = el detalle. Los
    # márgenes van en @page (en el CSS) para que se repitan igual en cada
    # página impresa, no solo al principio del documento.
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(html)
        html_path = Path(tmp.name)
    try:
        with sync_playwright() as pw:
            navegador_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
            b = pw.chromium.launch(executable_path=navegador_path) if navegador_path else pw.chromium.launch()
            page = b.new_page()
            page.goto(f"file:///{html_path.as_posix()}")
            page.wait_for_timeout(400)  # deja que carguen las fuentes/imágenes locales
            page.pdf(path=str(salida), format="A4", print_background=True)
            b.close()
    finally:
        html_path.unlink(missing_ok=True)


def generar(categoria, cadenas_arg, salida):
    data = cargar_datos()
    todas_cadenas = sorted({p["cadena"] for p in data["productos"] if p["cadena"] != CLIENTE})
    cadenas = [c for c in cadenas_arg if c in todas_cadenas] if cadenas_arg else todas_cadenas
    if not cadenas:
        sys.exit(f"Ninguna de las cadenas pedidas está en los datos. Disponibles: {', '.join(todas_cadenas)}")

    log(f"Comparando Farmacorp vs {', '.join(cadenas)} (categoría: {categoria or 'Todas'}) ...")
    filas = filtrar_por_categoria(construir_comparaciones(data["productos"], set(cadenas)), categoria)
    resumen = calcular_resumen(filas, cadenas)
    if resumen is None:
        sys.exit("No hay SKUs con match confirmado para esa combinación de categoría/cadenas.")

    log("Generando análisis ejecutivo ...")
    insight = generar_insight_ia(resumen, categoria, cadenas)

    log("Descargando fotos reales de producto ...")
    with tempfile.TemporaryDirectory() as tmp:
        carpeta = Path(tmp)
        destacadas = resumen["top_riesgo"][:3] + resumen["top_ventaja"][:3]
        imagenes = preparar_imagenes(destacadas, carpeta)

        html = construir_html(resumen, insight, categoria, cadenas, imagenes, data.get("fecha_actualizacion", "sin fecha"))

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        salida_path = REPORTS_DIR / salida
        log(f"Armando PDF -> {salida_path} ...")
        render_pdf(html, salida_path)

    log(f"Listo: {salida_path}")
    return salida_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--categoria", help='Filtrar por categoría exacta (ej: "Cuidado Personal"). Default: todas.')
    ap.add_argument("--cadenas", help='Cadenas a incluir, separadas por coma (ej: "Fidalga,Hipermaxi"). Default: todas.')
    ap.add_argument("--salida", default=None, help="Nombre del PDF de salida (se guarda en reportes/). Default: automático con fecha.")
    args = ap.parse_args()

    cadenas_arg = [c.strip() for c in args.cadenas.split(",")] if args.cadenas else None
    salida = args.salida or f"reporte_{date.today().isoformat()}{'_' + args.categoria.replace(' ', '-') if args.categoria else ''}.pdf"
    generar(args.categoria, cadenas_arg, salida)

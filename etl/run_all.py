"""
run_all.py — El "botón único" del flujo diario.

Lee config_cadenas.json, scrapea todas las cadenas/categorías
configuradas usando el contrato único (contrato.ScrapeRequest ->
contrato.ScrapeResult, ver contrato.py), carga cada resultado directo a
Postgres (etl.cargar_resultado — sin pasar por Excel), corre matchear()
una sola vez al final, y exporta precios.json. Cero intervención manual,
cero pasos sueltos.

Uso:
    python run_all.py                            # corre todo lo que está en config_cadenas.json
    python run_all.py --solo Fidalga             # corre solo una cadena
    python run_all.py --sin-export               # no regenera data/precios.json (para pruebas)
    python run_all.py --solo Fidalga --limite 3  # prueba de humo: solo 3 colecciones/URLs de Fidalga

Pensado para programarse en Task Scheduler / cron y correr solo, sin que
nadie esté mirando.
"""
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

import etl
import scraper_chavez
import scraper_hipermaxi
import scraper_shopify
from contrato import ScrapeRequest

CONFIG_PATH = Path(__file__).parent / "config_cadenas.json"
RAW_DIR = Path(__file__).parent.parent / "scrapes_raw"
EXPORT_PATH = Path(__file__).parent.parent / "data" / "precios.json"

# script configurado en config_cadenas.json -> módulo con ejecutar(request)
NAVEGADOR_MODULOS = {
    "scraper_hipermaxi.py": scraper_hipermaxi,
    "scraper_chavez.py": scraper_chavez,
}

MAX_INTENTOS = 3


def log(msg: str):
    print(f"[run_all] {msg}", flush=True)


def _registrar_ruta_fallida(cadena: str, etiqueta: str, url: str, error: str):
    """Antes, una ruta que fallaba tras los 3 reintentos sólo quedaba en
    el `errores.append(...)` de esta corrida -- se imprimía al final y se
    perdía apenas se cerraba la terminal. Ahora queda en `agent_runs`
    (misma tabla que ya se usa para auditar cargas exitosas), para que el
    panel pueda listar "rutas con error" entre corridas y ofrecer
    reintentarlas con un click, sin tener que ir a buscar en logs viejos."""
    try:
        conn = etl.get_conn()
        cur = conn.cursor()
        cadena_id = etl.get_or_create_cadena(cur, cadena)
        run_id = etl.start_agent_run(cur, "scraper", cadena_id=cadena_id, fuente_metodo=etiqueta)
        etl.finish_agent_run(cur, run_id, "error", 0, 1, {"etiqueta": etiqueta, "url": url, "error": error})
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log(f"  (no se pudo registrar la ruta fallida en agent_runs: {e})")


def _archivar(resultado, carpeta_hoy: Path, sufijo: str):
    """Guarda TODO lo scrapeado en un único .xlsx -- validado y
    observado, con columnas Observado (Y/N) y Motivo. Postgres sigue
    cargándose solo desde resultado.productos (los validados); esto es
    el respaldo completo para auditoría.

    Fallback: si el scraper todavía no arma 'raw_data' (migración en
    curso -- scraper_shopify.py/scraper_chavez.py por ahora), usa
    resultado.productos como antes, para no perder el respaldo mientras
    se termina de migrar cada scraper."""
    datos = resultado.raw_data or [p.model_dump() for p in resultado.productos]
    if not datos:
        return
    out_path = carpeta_hoy / f"{resultado.cadena.lower().replace(' ', '-')}_{sufijo}.xlsx"
    pd.DataFrame(datos).to_excel(out_path, index=False)
    return out_path


def _con_reintentos(cadena: str, etiqueta: str, fn):
    """Corre fn() con reintentos y backoff -- misma lógica de siempre,
    ahora envolviendo una llamada directa a ejecutar() en vez de un
    subprocess. Devuelve el ScrapeResult, o (None, error) si falló
    definitivo tras MAX_INTENTOS.

    Antes, si fn() devolvía None SIN tirar excepción (un scraper con un
    bug que hace 'return' en vez de 'raise' ante un error), ese None se
    devolvía tal cual como si fuera un ScrapeResult exitoso -- el
    caller hace `resultado.productos` sin chequear, y como None no es
    una tupla, no entraba al branch de "falló definitivo": crasheaba
    todo run_all.py con un AttributeError y ninguna otra cadena llegaba
    a correr. Ahora un fn() que devuelve None se trata igual que una
    excepción: cuenta como intento fallido, entra al mismo backoff, y
    si se agotan los intentos devuelve el mismo (None, error) que ya
    manejan los callers."""
    ultimo_error = None
    for intento in range(1, MAX_INTENTOS + 1):
        try:
            resultado = fn()
        except Exception as e:
            resultado = None
            ultimo_error = str(e)
        else:
            if resultado is not None:
                return resultado
            ultimo_error = "el scraper devolvió None sin lanzar una excepción (revisar su ejecutar())"
        log(f"  Intento {intento}/{MAX_INTENTOS} falló ({etiqueta}): {ultimo_error}")
        if intento < MAX_INTENTOS:
            espera = 30 * intento
            log(f"  Esperando {espera}s antes de reintentar (por si es un límite temporal de solicitudes)...")
            time.sleep(espera)
    log(f"  ERROR definitivo tras {MAX_INTENTOS} intentos en {etiqueta}: {ultimo_error}")
    return None, ultimo_error


def correr(solo_cadena: str | None, hacer_export: bool, max_llm: int | None = None, limite: int | None = None):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    hoy = date.today().isoformat()
    carpeta_hoy = RAW_DIR / hoy
    carpeta_hoy.mkdir(parents=True, exist_ok=True)

    archivos_cargados = 0
    errores = []

    for chain_cfg in config.get("shopify", []):
        cadena = chain_cfg["cadena"]
        if solo_cadena and cadena.lower() != solo_cadena.lower():
            continue

        sitio_base = chain_cfg["sitio_base"]
        colecciones = chain_cfg["colecciones"]
        
        if colecciones == "todas":
            log(f"Descubriendo categorías de {cadena} ({sitio_base}) ...")
            descubiertas = scraper_shopify.listar_colecciones(sitio_base)
            colecciones = [{"categoria": c.get("categoria", "General"), "subcategoria": c["titulo"], "handle": c["handle"]} for c in descubiertas if c["productos"] > 0]
            log(f"  {len(colecciones)} categorías con productos encontradas")

        if limite is not None:
            colecciones = colecciones[:limite]
            log(f"  --limite {limite}: solo se corren {len(colecciones)} colección(es) de {cadena} en esta corrida")

        for col in colecciones:
            # Compatibilidad: maneja diccionarios estructurados o strings directos
            if isinstance(col, dict):
                handle_coleccion = col["handle"]
                cat_def = col.get("categoria") or None
                subcat_def = col.get("subcategoria") or None
            else:
                handle_coleccion = col
                cat_def = handle_coleccion.replace("-", " ").title()
                subcat_def = None

            url_coleccion = f"{sitio_base}/collections/{handle_coleccion}"
            etiqueta = f"{cadena}/{handle_coleccion}"
            log(f"Scrapeando {etiqueta} ...")
            
            request = ScrapeRequest(
                cadena=cadena, 
                url=url_coleccion, 
                categoria_default=cat_def,
                subcategoria_default=subcat_def
            )

            resultado = _con_reintentos(cadena, etiqueta, lambda: scraper_shopify.ejecutar(request))
            if isinstance(resultado, tuple):  # falló definitivo -> (None, error)
                errores.append(f"{etiqueta}: {resultado[1]}")
                _registrar_ruta_fallida(cadena, etiqueta, url_coleccion, resultado[1])
                continue
            if not resultado.productos:
                log(f"  ADVERTENCIA: 0 productos en {url_coleccion}, se salta.")
                continue

            _archivar(resultado, carpeta_hoy, f"{handle_coleccion}_{hoy}")
            log(f"  {len(resultado.productos)} productos ({len(resultado.errores)} descartados por validación)")
            etl.cargar_resultado(resultado)
            archivos_cargados += 1

    for chain_cfg in config.get("navegador", []):
        cadena = chain_cfg["cadena"]
        if solo_cadena and cadena.lower() != solo_cadena.lower():
            continue

        modulo = NAVEGADOR_MODULOS.get(chain_cfg["script"])
        if modulo is None:
            log(f"  ERROR: no sé qué módulo usar para script '{chain_cfg['script']}' (¿falta agregarlo a NAVEGADOR_MODULOS?)")
            errores.append(f"{cadena}: script '{chain_cfg['script']}' no registrado")
            continue

        urls_cadena = chain_cfg["urls"]
        if limite is not None:
            urls_cadena = urls_cadena[:limite]
            log(f"  --limite {limite}: solo se corren {len(urls_cadena)} URL(s) de {cadena} en esta corrida")

        for url in urls_cadena:
            etiqueta = f"{cadena}/{url}"
            log(f"Scrapeando {etiqueta} (navegador) ...")
            request = ScrapeRequest(cadena=cadena, url=url)

            resultado = _con_reintentos(cadena, etiqueta, lambda: modulo.ejecutar(request))
            if isinstance(resultado, tuple):
                errores.append(f"{etiqueta}: {resultado[1]}")
                _registrar_ruta_fallida(cadena, etiqueta, url, resultado[1])
                continue
            if not resultado.productos:
                log(f"  ADVERTENCIA: 0 productos en {url}, se salta.")
                continue

            _archivar(resultado, carpeta_hoy, f"{hoy}_{abs(hash(url)) % 10000}")
            log(f"  {len(resultado.productos)} productos ({len(resultado.errores)} descartados por validación)")
            etl.cargar_resultado(resultado)
            archivos_cargados += 1

    if archivos_cargados == 0:
        log("Nada se cargó, se salta matchear/export.")
    else:
        # Primero se clasifican los productos nuevos: el matcheo agrupa por esas
        # categorías propias (más parejas entre cadenas que las de cada cadena).
        # La IA clasifica solo lo nuevo (centavos por corrida).
        log("Clasificando productos nuevos en categorías ...")
        try:
            import clasificar
            r = clasificar.clasificar()
            log(f"  {r['clasificados']} clasificados, {r['pendientes']} pendientes")
        except SystemExit as e:
            log(f"  ADVERTENCIA: no se clasificó ({e}).")
        except Exception as e:
            log(f"  ADVERTENCIA: no se pudo clasificar ({e}). ¿Corriste db/migracion_categorias.sql en Supabase?")

        log("Corriendo matchear() ...")
        etl.matchear(max_llm=max_llm if max_llm is not None else etl.MAX_LLM_POR_CORRIDA)

        if hacer_export:
            log(f"Exportando a {EXPORT_PATH} ...")
            import export_json
            export_json.export_json(str(EXPORT_PATH))

        # La página web lee de Supabase (tabla web_productos), no del JSON.
        # Si todavía no se corrió db/migracion_web.sql, avisa y sigue.
        log("Publicando web_productos para la página ...")
        try:
            import publicar_web
            publicar_web.publicar()
        except Exception as e:
            log(f"  ADVERTENCIA: no se pudo publicar la web ({e}). ¿Corriste db/migracion_web.sql en Supabase?")

    log(f"Listo. Cargas: {archivos_cargados}. Errores: {len(errores)}")
    for e in errores:
        log(f"  - {e}")

    if errores:
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solo", help="Correr solo esta cadena (ej: Fidalga)")
    ap.add_argument("--sin-export", action="store_true", help="No regenerar data/precios.json")
    ap.add_argument("--max-llm", type=int, default=None,
                     help="Tope de consultas NUEVAS al LLM en el matchear() de esta corrida "
                          "(default: env MATCH_LLM_MAX_POR_CORRIDA, o 50)")
    ap.add_argument("--limite", type=int, default=None,
                     help="Solo corre las primeras N colecciones/URLs de cada cadena "
                          "(para pruebas rapidas de humo -- ej: --solo Fidalga --limite 3)")
    args = ap.parse_args()
    correr(args.solo, hacer_export=not args.sin_export, max_llm=args.max_llm, limite=args.limite)
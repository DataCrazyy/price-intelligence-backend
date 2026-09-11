"""
run_all.py — El "botón único" del flujo diario.

Lee config_cadenas.json, scrapea todas las cadenas/categorías
configuradas usando el contrato único (contrato.ScrapeRequest ->
contrato.ScrapeResult, ver contrato.py), carga cada resultado directo a
Postgres (etl.cargar_resultado — sin pasar por Excel), corre matchear()
una sola vez al final, y exporta precios.json. Cero intervención manual,
cero pasos sueltos.

Uso:
    python run_all.py                 # corre todo lo que está en config_cadenas.json
    python run_all.py --solo Fidalga  # corre solo una cadena
    python run_all.py --sin-export    # no regenera data/precios.json (para pruebas)

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
EXPORT_PATH = Path(__file__).parent.parent.parent / "data" / "precios.json"

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
    """Guarda una copia .xlsx del resultado ya validado, solo para
    auditoría (scrapes_raw/) -- Postgres se carga directo desde el
    ScrapeResult, esto es un respaldo, no el camino de carga."""
    if not resultado.productos:
        return
    out_path = carpeta_hoy / f"{resultado.cadena.lower().replace(' ', '-')}_{sufijo}.xlsx"
    pd.DataFrame([p.model_dump() for p in resultado.productos]).to_excel(out_path, index=False)
    return out_path


def _con_reintentos(cadena: str, etiqueta: str, fn):
    """Corre fn() con reintentos y backoff -- misma lógica de siempre,
    ahora envolviendo una llamada directa a ejecutar() en vez de un
    subprocess. Devuelve el resultado o None si falló definitivo."""
    ultimo_error = None
    for intento in range(1, MAX_INTENTOS + 1):
        try:
            return fn()
        except Exception as e:
            ultimo_error = str(e)
            log(f"  Intento {intento}/{MAX_INTENTOS} falló ({etiqueta}): {e}")
            if intento < MAX_INTENTOS:
                espera = 30 * intento
                log(f"  Esperando {espera}s antes de reintentar (por si es un límite temporal de solicitudes)...")
                time.sleep(espera)
    log(f"  ERROR definitivo tras {MAX_INTENTOS} intentos en {etiqueta}: {ultimo_error}")
    return None, ultimo_error


def correr(solo_cadena: str | None, hacer_export: bool, max_llm: int | None = None):
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
            colecciones = [c["handle"] for c in descubiertas if c["productos"] > 0]
            log(f"  {len(colecciones)} categorías con productos encontradas")

        for handle_coleccion in colecciones:
            url_coleccion = f"{sitio_base}/collections/{handle_coleccion}"
            etiqueta = f"{cadena}/{handle_coleccion}"
            log(f"Scrapeando {etiqueta} ...")
            categoria_default = handle_coleccion.replace("-", " ").title()
            request = ScrapeRequest(cadena=cadena, url=url_coleccion, categoria_default=categoria_default)

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

        for url in chain_cfg["urls"]:
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
        log("Corriendo matchear() ...")
        etl.matchear(max_llm=max_llm if max_llm is not None else etl.MAX_LLM_POR_CORRIDA)

        if hacer_export:
            log(f"Exportando a {EXPORT_PATH} ...")
            import export_json
            export_json.export_json(str(EXPORT_PATH))

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
    args = ap.parse_args()
    correr(args.solo, hacer_export=not args.sin_export, max_llm=args.max_llm)

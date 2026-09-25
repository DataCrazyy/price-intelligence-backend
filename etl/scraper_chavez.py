"""
scraper_chavez.py — Scraper para Farmacias Chávez (Next.js, sin JSON público).

A diferencia de Hipermaxi (scroll infinito), Chávez pagina de verdad con
un parámetro de URL: agregando ?currentPage=N a la URL de la categoría se
navega directo a esa página, sin necesidad de hacer scroll ni clickear el
botón "siguiente". El script detecta cuántas páginas hay (lo muestra el
paginador "1/21") y las recorre todas.

El código de artículo es el ID numérico al final de la URL del producto
(/p/nutrilon-premium-2-x-400-gr-27136 -> 27136), el identificador interno
real de Chávez, estable entre scrapeos.

Requiere:
    pip install playwright requests
    playwright install chromium    (solo una vez)

Uso:
    python scraper_chavez.py <url_categoria> [--out archivo.xlsx]
    python scraper_chavez.py --descubrir [url_base]
"""
import argparse
import json
import os
import re
import sys
from datetime import date

import pandas as pd
import requests
from playwright.sync_api import sync_playwright

from contrato import ProductoScrapeado, ScrapeRequest, ScrapeResult

# ==========================================
# CONFIGURACIÓN E INSTALEAP API (CATEGORÍAS)
# ==========================================
INSTALEAP_API_URL = "https://nextgentheadless.instaleap.io/api/v3"
INSTALEAP_HEADERS = {
    "content-type": "application/json",
    "dpl-api-key": "d3c650d5-4fa8-4356-a9e2-6fbedec25335",
    "apollographql-client-name": "e-commerce Moira Engine client FARMACIAS_CHAVEZ",
    "apollographql-client-version": "0.19.341",
    "client-name": "e-commerce Moira Engine FARMACIAS_CHAVEZ",
    "client-version": "0.19.341",
    "user-agent": "Mozilla/5.0 (compatible; PriceIntelBot/1.0)",
}

CATEGORY_TREE_QUERY = """
fragment CategoryFields on CategoryModel {
    active
    boost
    hasChildren
    categoryNamesPath
    isAvailableInHome
    level
    name
    path
    reference
    slug
    photoUrl
    imageUrl
    shortName
    isFeatured
    isAssociatedToCatalog
    __typename
}

fragment CategoriesRecursive on CategoryModel {
    subCategories {
        ...CategoryFields
        subCategories {
            ...CategoryFields
            subCategories {
                ...CategoryFields
                __typename
            }
            __typename
        }
        __typename
    }
    __typename
}

fragment CategoryModel on CategoryModel {
    ...CategoryFields
    ...CategoriesRecursive
    __typename
}

query GetCategoryTree($getCategoryInput: GetCategoryInput!) {
    getCategory(getCategoryInput: $getCategoryInput) {
        ...CategoryModel
        __typename
    }
}
"""

EXTRAER_JS = """
() => {
  const cards = Array.from(document.querySelectorAll('a.containerCard'));
  return cards.map(a => {
    const href = a.getAttribute('href');
    const m = href ? href.match(/\\/p\\/(.+)-(\\d+)$/) : null;
    const priceEl = a.querySelector('.base__price, [class*="CardBasePrice"]');
    const img = a.querySelector('img');
    return {
      codigo: m ? m[2] : null,
      nombre: img ? img.getAttribute('alt') : null,
      precio: priceEl ? priceEl.textContent.trim() : null,
      url: href,
      imagen: img ? img.src : null,
    };
  }).filter(p => p.codigo);
}
"""


def extraer_categoria_y_subcategoria(url: str) -> tuple[str, str]:
    """
    Analiza la URL de Farmacias Chávez (ej: /ca/suplementos-y-vitaminas/vitaminas/112/11202)
    o URLs absolutas, descartando dominios, 'ca' y códigos numéricos, 
    para retornar una Categoría limpia y su Subcategoría jerárquica.
    """
    if "://" in url:
        partes_url = url.split("://")[1].split("/")[1:]
    else:
        partes_url = [p for p in url.split("/") if p]
        
    partes = [p for p in partes_url if p and not p.isdigit() and p.lower() != "ca" and "." not in p]
    
    if not partes:
        return "General", ""
        
    categoria = partes[0].replace("-", " ").title()
    
    if len(partes) > 1:
        subpartes = [p.replace("-", " ").title() for p in partes[1:]]
        subcategoria = " > ".join(subpartes)
        return categoria, subcategoria
        
    return categoria, ""


def total_paginas(page) -> int:
    """Lee el paginador Ant Design (formato 'actual/total', ej. '1/21').
    Si no hay paginador (categoría chica, cabe en una sola página), es 1."""
    titulo = page.evaluate(
        "document.querySelector('.ant-pagination-simple-pager')?.getAttribute('title') || null"
    )
    if not titulo:
        return 1
    m = re.match(r"\d+/(\d+)", titulo)
    return int(m.group(1)) if m else 1


def scrapear(url_base: str) -> list[dict]:
    url_base = url_base.split("?")[0]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")

        print(f"Abriendo {url_base} ...")
        page.goto(url_base, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("a.containerCard", timeout=30000)
        except Exception:
            print("  No aparecieron productos en la página 1 -- puede que la categoría esté vacía o cambió la estructura.")
            browser.close()
            return []
        page.wait_for_timeout(800)

        n_paginas = total_paginas(page)
        print(f"  {n_paginas} página(s) detectada(s) en el paginador")

        productos = list(page.evaluate(EXTRAER_JS))
        print(f"  página 1/{n_paginas}: {len(productos)} productos (total: {len(productos)})")

        vistos = {p["codigo"] for p in productos}
        for n in range(2, n_paginas + 1):
            url_pagina = f"{url_base}?currentPage={n}"
            page.goto(url_pagina, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("a.containerCard", timeout=20000)
            except Exception:
                print(f"  página {n}: no cargaron productos, se salta")
                continue
            page.wait_for_timeout(600)
            nuevos = page.evaluate(EXTRAER_JS)
            agregados = [p for p in nuevos if p["codigo"] not in vistos]
            for p in agregados:
                vistos.add(p["codigo"])
            productos.extend(agregados)
            print(f"  página {n}/{n_paginas}: {len(nuevos)} productos ({len(agregados)} nuevos, total: {len(productos)})")

        browser.close()

    print(f"  {len(productos)} productos encontrados en total")
    return productos


def _slug_partes(slug: str) -> tuple[str, str]:
    """
    Extrae limpiamente el nombre y el ID numérico del slug sin acumular rutas padres.
    """
    if not slug:
        return "", ""
    partes = [p for p in slug.split("/") if p]
    textos = [p for p in partes if not p.isdigit()]
    numeros = [p for p in partes if p.isdigit()]
    
    nombre = textos[-1] if textos else ""
    id_ = numeros[-1] if numeros else ""
    return nombre, id_


def descubrir_categorias(sitio_base: str = "https://www.farmaciaschavez.com.bo") -> list[dict]:
    print(f"Descubriendo categorías y subcategorías vía API Instaleap en {sitio_base}...")
    
    payload = [{
        "operationName": "GetCategoryTree",
        "variables": {"getCategoryInput": {"clientId": "FARMACIAS_CHAVEZ", "storeReference": "5406"}},
        "query": CATEGORY_TREE_QUERY,
    }]
    
    try:
        resp = requests.post(INSTALEAP_API_URL, json=payload, headers=INSTALEAP_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        categorias_nivel1 = data[0]["data"]["getCategory"]
    except Exception as e:
        print(f"  Error al consultar la API de Instaleap: {e}")
        return []

    resultados = []
    for cat in categorias_nivel1:
        cat_nombre, cat_id = _slug_partes(cat["slug"])
        subcats = cat.get("subCategories") or []

        if not subcats:
            resultados.append({
                "categoria": cat["name"], 
                "subcategoria": None,
                "url": f"{sitio_base}/ca/{cat_nombre}/{cat_id}",
            })
            continue

        # Límite estricto: categoría principal y su primera subcategoría limpia
        for sub in subcats:
            sub_nombre, sub_id = _slug_partes(sub["slug"])
            resultados.append({
                "categoria": cat["name"], 
                "subcategoria": sub["name"],
                "url": f"{sitio_base}/ca/{cat_nombre}/{sub_nombre}/{cat_id}/{sub_id}",
            })

    print(f"¡Se descubrieron {len(resultados)} registros limpios y estructurados!")
    return resultados


def cargar_a_config_cadenas(json_path: str, config_path: str = "config_cadenas.json"):
    print(f"Actualizando {config_path} con las URLs de '{json_path}'...")
    
    if not os.path.exists(json_path):
        print(f"  Error: No se encontró el archivo {json_path}")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        categorias_descubiertas = json.load(f)

    # Extraer únicamente las URLs válidas y limpias
    urls_extraidas = [cat["url"] for cat in categorias_descubiertas if "url" in cat]

    config_data = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            config_data = {}

    if "navegador_chavez" in config_data:
        del config_data["navegador_chavez"]

    if "_comentario" not in config_data:
        config_data["_comentario"] = "Cadenas que se scrapean automaticamente..."

    if "cadenas" not in config_data or not isinstance(config_data["cadenas"], list):
        config_data["cadenas"] = []

    encontrado = False
    for cadena_cfg in config_data["cadenas"]:
        if cadena_cfg.get("cadena") == "Farmacias Chavez":
            cadena_cfg["script"] = "scraper_chavez.py"
            cadena_cfg["urls"] = urls_extraidas
            encontrado = True
            break
    
    if not encontrado:
        config_data["cadenas"].append({
            "cadena": "Farmacias Chavez",
            "script": "scraper_chavez.py",
            "urls": urls_extraidas
        })

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=4)

    print(f"¡Configuración sincronizada! {len(urls_extraidas)} URLs limpias guardadas en {config_path}.")


def parse_precio_chavez(txt) -> float | None:
    if not txt:
        return None
    limpio = re.sub(r"[^\d.,]", "", str(txt))
    if not limpio:
        return None
    if "," in limpio and "." in limpio:
        limpio = limpio.replace(".", "").replace(",", ".")
    elif "," in limpio:
        limpio = limpio.replace(",", ".")
    try:
        return float(limpio)
    except ValueError:
        return None


def productos_a_filas(
    productos: list[dict], 
    categoria_default: str = "", 
    subcategoria_default: str = "", 
    cadena: str = "Farmacias Chavez",
    url_fallback: str = ""
) -> list[dict]:
    filas = []
    
    cat_auto, subcat_auto = extraer_categoria_y_subcategoria(url_fallback)
    
    categoria_final = categoria_default or cat_auto
    subcategoria_final = subcategoria_default if subcategoria_default != "" else subcat_auto

    for p in productos:
        precio = parse_precio_chavez(p["precio"])
        if precio is None:
            continue
            
        filas.append({
            "Articulo": p["nombre"] or "",
            "Precio_Oferta": precio,
            "Precio_Regular": precio,
            "CADENA": cadena,
            "COD_ARTICULO": p["codigo"],
            "Categoria": categoria_final,
            "Subcategoria": subcategoria_final,
            "Ciudad": "Santa Cruz",
            "Sucursal": "",
            "URL": p["url"] if str(p["url"]).startswith("http") else f"https://www.farmaciaschavez.com.bo{p['url']}",
            "Imagen": p["imagen"] or "",
        })
    return filas


def ejecutar(request: ScrapeRequest) -> ScrapeResult:
    productos_raw = scrapear(request.url)
    
    filas = productos_a_filas(
        productos_raw, 
        categoria_default=request.categoria_default or "",
        subcategoria_default=request.subcategoria_default or "",
        cadena=request.cadena,
        url_fallback=request.url
    )

    productos, errores, raw_data = [], [], []
    for f in filas:
        fila = dict(f)
        try:
            productos.append(ProductoScrapeado(
                nombre=f["Articulo"], precio_oferta=f["Precio_Oferta"], precio_regular=f["Precio_Regular"],
                cadena=f["CADENA"], codigo_articulo=f["COD_ARTICULO"], categoria=f["Categoria"],
                subcategoria=f["Subcategoria"] or None, ciudad=f["Ciudad"] or request.ciudad,
                sucursal=f["Sucursal"] or None, url=f["URL"], imagen=f["Imagen"] or None,
            ))
            fila["Observado"] = "N"
            fila["Motivo"] = ""
        except Exception as e:
            errores.append(f"{f.get('COD_ARTICULO', '?')}: {e}")
            fila["Observado"] = "Y"
            fila["Motivo"] = str(e)
        raw_data.append(fila)

    return ScrapeResult(
        cadena=request.cadena, fuente="playwright", productos=productos, 
        errores=errores, raw_data=raw_data
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="URL de categoría de Farmacias Chávez")
    ap.add_argument("--descubrir", nargs="?", const="https://www.farmaciaschavez.com.bo", help="Descubre categorías y genera un JSON")
    ap.add_argument("--out", help="Nombre del archivo de salida (.xlsx o .json)")
    args = ap.parse_args()

    if args.descubrir is not None:
        categorias = descubrir_categorias(args.descubrir)
        out = args.out or f"categorias_chavez_{date.today().isoformat()}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(categorias, f, ensure_ascii=False, indent=4)
        print(f"¡JSON guardado con éxito en: {out}!")
        cargar_a_config_cadenas(out, "config_cadenas.json")
        return

    if not args.url:
        ap.print_help()
        sys.exit(1)

    cat_auto, _ = extraer_categoria_y_subcategoria(args.url)
    productos = scrapear(args.url)
    filas = productos_a_filas(productos, url_fallback=args.url)
    if not filas:
        print("No se encontraron productos. Revisa la URL o si Chávez cambió su estructura.")
        sys.exit(1)

    df = pd.DataFrame(filas)
    out = args.out or f"chavez_{cat_auto.lower().replace(' ', '_')}_{date.today().isoformat()}.xlsx"
    df.to_excel(out, index=False)
    print(f"\nListo: {len(df)} filas -> {out}")
    print(f"Siguiente paso: python etl.py cargar-excel {out}")


if __name__ == "__main__":
    main()
"""
scraper_shopify.py — Scraper genérico para CUALQUIER tienda Shopify.

No usa IA ni tokens: pega directo al endpoint público /products.json que
Shopify expone en todas las tiendas, pagina automáticamente, y devuelve un
.xlsx en el formato estándar (ver ESTANDAR_SCRAPING.md) listo para
`etl.py cargar-excel`.

Uso:
    python scraper_shopify.py <url_coleccion> <nombre_cadena> [--out archivo.xlsx]
    python scraper_shopify.py --descubrir <sitio_base>

Ejemplos:
    python scraper_shopify.py https://www.fidalga.com/collections/lacteos Fidalga
    python scraper_shopify.py --descubrir https://www.amarket.com.bo

Regla de oro (ver ESTANDAR_SCRAPING.md): el código de artículo SIEMPRE es
el `handle` (la parte de la URL después de /products/), nunca el SKU.
El SKU puede venir vacío o repetirse entre variantes; el handle es estable
y es el que ya usa todo tu histórico en Postgres.
"""
import argparse
import json
import sys
import time
from datetime import date

import pandas as pd
import requests
from bs4 import BeautifulSoup

from contrato import ProductoScrapeado, ScrapeRequest, ScrapeResult

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PriceIntelBot/1.0)"}


def listar_colecciones(sitio_base: str) -> list[dict]:
    """Descubre TODAS las categorías (colecciones) publicadas de una tienda
    Shopify, sin distinguir padre/hijo. Útil para explorar, pero para la
    corrida diaria conviene usar descubrir_categorias_shopify() en su
    lugar (ver nota en config_cadenas.json sobre por qué)."""
    sitio_base = sitio_base.rstrip("/")
    colecciones = []
    page = 1
    while True:
        resp = requests.get(
            f"{sitio_base}/collections.json",
            params={"limit": 250, "page": page},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("collections", [])
        if not batch:
            break
        for c in batch:
            colecciones.append({
                "handle": c["handle"],
                "titulo": c.get("title", c["handle"]),
                "url": f"{sitio_base}/collections/{c['handle']}",
                "productos": c.get("products_count", 0),
            })
        if len(batch) < 250:
            break
        page += 1
    return colecciones


def _url_absoluta(sitio_base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    return sitio_base.rstrip("/") + href


def _descubrir_navmenu(sitio_base: str, soup: BeautifulSoup) -> list[dict]:
    """Tema 'navmenu' de Shopify (Amarket, Farmacorp): cada categoría
    padre es un <li class="navmenu-item-parent"> con su <a> apuntando a
    la colección padre, y un <ul class="navmenu-submenu"> anidado adentro
    con las subcategorías como <li><a>."""
    resultados = []
    for li_padre in soup.select("li.navmenu-item-parent"):
        a_padre = li_padre.find("a", class_="navmenu-link-parent")
        if not a_padre:
            continue
        categoria = a_padre.get_text(strip=True)
        href_cat = a_padre.get("href", "")

        submenu = li_padre.find("ul", class_="navmenu-submenu")
        hijos = submenu.find_all("a", class_="navmenu-link") if submenu else []

        if not hijos:
            resultados.append({
                "categoria": categoria, "subcategoria": None,
                "url": _url_absoluta(sitio_base, href_cat),
            })
            continue

        for a_hijo in hijos:
            subcategoria = a_hijo.get_text(strip=True)
            href_sub = a_hijo.get("href", "")
            resultados.append({
                "categoria": categoria, "subcategoria": subcategoria,
                "url": _url_absoluta(sitio_base, href_sub),
            })
    return resultados


def _descubrir_fidalga(sitio_base: str, soup: BeautifulSoup) -> list[dict]:
    """Tema de Fidalga: cada categoría es un <div class="menu-lv-2"> con
    su <a class="menu__moblie"> (categoría PADRE, ej. 'Lacteos'), y un
    submenu <ul class="site-nav-dropdown"> con las subcategorías HIJAS
    (ej. 'Leche', 'Yogurt') como <li class="menu-lv-3"><a>. La primera
    entrada del submenu suele ser un "Todas <categoría>" que repite la
    URL del padre -- se descarta."""
    resultados = []
    for div_padre in soup.select("div.menu-lv-2"):
        a_padre = div_padre.find("a", class_="menu__moblie")
        if not a_padre:
            continue
        span_padre = a_padre.find("span")
        categoria = span_padre.get_text(strip=True) if span_padre else a_padre.get_text(strip=True)
        href_cat = a_padre.get("href", "")

        submenu = div_padre.find("ul", class_="site-nav-dropdown")
        hijos = submenu.find_all("li", class_="menu-lv-3") if submenu else []

        entradas_hijas = 0
        for li_hijo in hijos:
            a_hijo = li_hijo.find("a")
            if not a_hijo:
                continue
            href_sub = a_hijo.get("href", "")
            if href_sub == href_cat:
                continue  # "Todas <categoría>" -- misma URL que el padre
            span_hijo = a_hijo.find("span")
            subcategoria = span_hijo.get_text(strip=True) if span_hijo else a_hijo.get_text(strip=True)
            resultados.append({
                "categoria": categoria,
                "subcategoria": subcategoria,
                "url": _url_absoluta(sitio_base, href_sub),
            })
            entradas_hijas += 1

        if entradas_hijas == 0:
            resultados.append({
                "categoria": categoria, "subcategoria": None,
                "url": _url_absoluta(sitio_base, href_cat),
            })
    return resultados

def descubrir_categorias_shopify(sitio_base: str) -> list[dict]:
    """Detecta automáticamente qué tema de menú usa el sitio (navmenu de
    Amarket/Farmacorp, o el de Fidalga) y aplica el parser correcto."""
    resp = requests.get(sitio_base.rstrip("/") + "/", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    if soup.select("li.navmenu-item-parent"):
        return _descubrir_navmenu(sitio_base, soup)
    if soup.select("div.menu-lv-2"):
        return _descubrir_fidalga(sitio_base, soup)

    print(f"  ADVERTENCIA: no reconozco el tema de menú en {sitio_base} -- devolviendo lista vacía.")
    return []


def fetch_collection(base_url: str) -> list[dict]:
    """Descarga TODOS los productos de una colección Shopify, paginando."""
    base_url = base_url.rstrip("/")
    if not base_url.endswith((".json",)):
        json_url = f"{base_url}/products.json"
    else:
        json_url = base_url

    productos = []
    page = 1
    while True:
        resp = requests.get(
            json_url, params={"limit": 250, "page": page}, headers=HEADERS, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("products", [])
        if not batch:
            break
        productos.extend(batch)
        print(f"  página {page}: {len(batch)} productos (total: {len(productos)})")
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.5)

    return productos


def productos_a_filas(
    productos: list[dict], 
    sitio_base: str, 
    cadena: str, 
    categoria_default: str = "", 
    subcategoria_default: str = ""
) -> list[dict]:
    """Mapea los productos de Shopify a filas estándar asegurando
    que la categoría principal y la subcategoria se mantengan limpias y en su lugar."""
    filas = []
    for p in productos:
        if not p.get("variants"):
            continue
        v = p["variants"][0]
        precio_oferta = float(v.get("price") or 0)
        compare = float(v.get("compare_at_price") or 0)
        precio_regular = compare if compare > precio_oferta else precio_oferta
        img = p["images"][0]["src"] if p.get("images") else ""

        product_type = (p.get("product_type") or "").strip()
        
        # Asignación corregida y robusta:
        categoria = categoria_default or product_type or "Sin categoria"
        subcategoria = subcategoria_default if subcategoria_default else ""

        filas.append({
            "Articulo": p.get("title", "").strip(),
            "Precio_Oferta": precio_oferta,
            "Precio_Regular": precio_regular,
            "CADENA": cadena,
            "COD_ARTICULO": p["handle"],  # Regla de oro: handle, nunca SKU
            "Categoria": categoria,
            "Subcategoria": subcategoria,
            "Ciudad": "Santa Cruz",
            "Sucursal": "",
            "URL": f"{sitio_base}/products/{p['handle']}",
            "Imagen": img,
        })
    return filas


def ejecutar(request: ScrapeRequest) -> ScrapeResult:
    sitio_base = "/".join(request.url.split("/")[:3])
    productos_raw = fetch_collection(request.url)
    
    filas = productos_a_filas(
        productos_raw, 
        sitio_base, 
        request.cadena, 
        categoria_default=request.categoria_default or "",
        subcategoria_default=request.subcategoria_default or ""
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
        cadena=request.cadena, fuente="shopify_json", productos=productos,
        errores=errores, raw_data=raw_data,
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--descubrir":
        sitio_base = sys.argv[2] if len(sys.argv) > 2 else None
        if not sitio_base:
            print("Uso: python scraper_shopify.py --descubrir <sitio_base>")
            sys.exit(1)
        resultados = descubrir_categorias_shopify(sitio_base)
        out = f"shopify_categorias_{sitio_base.split('//')[-1].split('/')[0].replace('.', '_')}.json"
        with open(out, "w", encoding="utf-8") as fp:
            json.dump(resultados, fp, indent=2, ensure_ascii=False)
        print(f"\n{len(resultados)} entradas guardadas en {out}")
        con_sub = [r for r in resultados if r["subcategoria"]]
        print(f"({len(con_sub)} con subcategoría -- esas son las URLs que conviene poner en config_cadenas.json)")
        return

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="URL de la colección Shopify")
    ap.add_argument("cadena", nargs="?", help="Nombre de la cadena")
    ap.add_argument("--out", help="Nombre del archivo .xlsx de salida")
    ap.add_argument("--listar", action="store_true", help="Lista todas las categorías disponibles")
    args = ap.parse_args()

    if args.listar:
        sitio_base = "/".join(args.url.split("/")[:3])
        cols = listar_colecciones(sitio_base)
        print(f"{len(cols)} categorías encontradas en {sitio_base}:\n")
        for c in cols:
            print(f"  {c['handle']:<40} {c['titulo']}")
        return

    if not args.cadena:
        print("Falta el nombre de la cadena (ej: Fidalga).")
        sys.exit(1)

    sitio_base = "/".join(args.url.split("/")[:3])
    slug_coleccion = args.url.rstrip("/").split("/")[-1].split("?")[0]
    categoria_default = slug_coleccion.replace("-", " ").title()

    print(f"Scrapeando {args.url} ...")
    productos = fetch_collection(args.url)
    print(f"Total productos encontrados: {len(productos)}")

    filas = productos_a_filas(productos, sitio_base, args.cadena, categoria_default)
    df = pd.DataFrame(filas)

    if not args.out:
        args.out = f"{args.cadena.lower()}_{slug_coleccion}_{date.today().isoformat()}.xlsx"

    df.to_excel(args.out, index=False)
    print(f"\nListo: {len(df)} filas -> {args.out}")
    print(f"Siguiente paso: python etl.py cargar-excel {args.out}")


if __name__ == "__main__":
    main()
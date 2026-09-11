"""
scraper_shopify.py — Scraper genérico para CUALQUIER tienda Shopify.

No usa IA ni tokens: pega directo al endpoint público /products.json que
Shopify expone en todas las tiendas, pagina automáticamente, y devuelve un
.xlsx en el formato estándar (ver ESTANDAR_SCRAPING.md) listo para
`etl.py cargar-excel`.

Uso:
    python scraper_shopify.py <url_coleccion> <nombre_cadena> [--out archivo.xlsx]

Ejemplos:
    python scraper_shopify.py https://www.fidalga.com/collections/lacteos Fidalga
    python scraper_shopify.py https://farmacorp.com/collections/leches-y-formulas Farmacorp --out farmacorp_leches.xlsx

Regla de oro (ver ESTANDAR_SCRAPING.md): el código de artículo SIEMPRE es
el `handle` (la parte de la URL después de /products/), nunca el SKU.
El SKU puede venir vacío o repetirse entre variantes; el handle es estable
y es el que ya usa todo tu histórico en Postgres.
"""
import argparse
import sys
import time
from datetime import date

import pandas as pd
import requests

from contrato import ProductoScrapeado, ScrapeRequest, ScrapeResult

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PriceIntelBot/1.0)"}


def listar_colecciones(sitio_base: str) -> list[dict]:
    """Descubre TODAS las categorías (colecciones) publicadas de una tienda
    Shopify. Esto es lo que permite que el panel diga 'dame la cadena y te
    muestro qué categorías hay para elegir', sin que tengas que ir manual
    al sitio a copiar URLs."""
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
        time.sleep(0.5)  # no martillar el servidor

    return productos


def productos_a_filas(productos: list[dict], sitio_base: str, cadena: str, categoria_default: str = "") -> list[dict]:
    """categoria_default: se usa cuando el producto no trae 'product_type'
    propio (muchas tiendas Shopify lo dejan vacío -- le pasó a Farmacorp).
    Sin esto la categoría queda vacía, y una celda vacía en el Excel se
    lee como NULL en Postgres al recargarla, así que el producto termina
    invisible en los filtros de categoría del frontend. Normalmente se le
    pasa el nombre de la colección que se está scrapeando."""
    filas = []
    for p in productos:
        if not p.get("variants"):
            continue
        v = p["variants"][0]
        precio_oferta = float(v.get("price") or 0)
        compare = float(v.get("compare_at_price") or 0)
        # compare_at_price es el precio "tachado" (regular) cuando hay oferta
        precio_regular = compare if compare > precio_oferta else precio_oferta
        img = p["images"][0]["src"] if p.get("images") else ""

        filas.append({
            "Articulo": p.get("title", "").strip(),
            "Precio_Oferta": precio_oferta,
            "Precio_Regular": precio_regular,
            "CADENA": cadena,
            "COD_ARTICULO": p["handle"],  # regla de oro: handle, nunca SKU
            "Categoria": p.get("product_type") or categoria_default or "Sin categoria",
            "Subcategoria": "",
            "Ciudad": "Santa Cruz",
            "Sucursal": "",
            "URL": f"{sitio_base}/products/{p['handle']}",
            "Imagen": img,
        })
    return filas


def ejecutar(request: ScrapeRequest) -> ScrapeResult:
    """El "agente único": misma firma que scraper_hipermaxi.ejecutar() y
    scraper_chavez.ejecutar(), aunque por dentro esto pega al JSON público
    de Shopify en vez de abrir un navegador. request.url es la URL de la
    colección (ej. https://www.fidalga.com/collections/lacteos)."""
    sitio_base = "/".join(request.url.split("/")[:3])
    productos_raw = fetch_collection(request.url)
    filas = productos_a_filas(productos_raw, sitio_base, request.cadena, request.categoria_default or "")

    productos, errores = [], []
    for f in filas:
        try:
            productos.append(ProductoScrapeado(
                nombre=f["Articulo"], precio_oferta=f["Precio_Oferta"], precio_regular=f["Precio_Regular"],
                cadena=f["CADENA"], codigo_articulo=f["COD_ARTICULO"], categoria=f["Categoria"],
                subcategoria=f["Subcategoria"] or None, ciudad=f["Ciudad"] or request.ciudad,
                sucursal=f["Sucursal"] or None, url=f["URL"], imagen=f["Imagen"] or None,
            ))
        except Exception as e:
            errores.append(f"{f.get('COD_ARTICULO', '?')}: {e}")

    return ScrapeResult(cadena=request.cadena, fuente="shopify_json", productos=productos, errores=errores)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="URL de la colección Shopify, ej: https://www.fidalga.com/collections/lacteos (o el dominio solo, con --listar)")
    ap.add_argument("cadena", nargs="?", help="Nombre de la cadena, ej: Fidalga (no aplica con --listar)")
    ap.add_argument("--out", help="Nombre del archivo .xlsx de salida (default: auto)")
    ap.add_argument("--listar", action="store_true", help="En vez de scrapear, lista todas las categorías disponibles del sitio")
    args = ap.parse_args()

    if args.listar:
        sitio_base = "/".join(args.url.split("/")[:3])
        cols = listar_colecciones(sitio_base)
        print(f"{len(cols)} categorías encontradas en {sitio_base}:\n")
        for c in cols:
            print(f"  {c['handle']:<40} {c['titulo']}")
        return

    if not args.cadena:
        print("Falta el nombre de la cadena (ej: Fidalga). Usa --listar si solo quieres ver categorías.")
        sys.exit(1)

    sitio_base = "/".join(args.url.split("/")[:3])  # https://www.fidalga.com
    slug_coleccion = args.url.rstrip("/").split("/")[-1].split("?")[0]
    categoria_default = slug_coleccion.replace("-", " ").title()

    print(f"Scrapeando {args.url} ...")
    productos = fetch_collection(args.url)
    print(f"Total productos encontrados: {len(productos)}")

    filas = productos_a_filas(productos, sitio_base, args.cadena, categoria_default)
    df = pd.DataFrame(filas)

    if not args.out:
        slug_coleccion = args.url.rstrip("/").split("/")[-1]
        args.out = f"{args.cadena.lower()}_{slug_coleccion}_{date.today().isoformat()}.xlsx"

    df.to_excel(args.out, index=False)
    print(f"\nListo: {len(df)} filas -> {args.out}")
    print(f"Siguiente paso: python etl.py cargar-excel {args.out}")


if __name__ == "__main__":
    main()

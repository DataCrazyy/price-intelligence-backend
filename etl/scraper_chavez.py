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
    pip install playwright
    playwright install chromium      (solo una vez)

Uso:
    python scraper_chavez.py <url_categoria> [--out archivo.xlsx]

Ejemplo:
    python scraper_chavez.py "https://www.farmaciaschavez.com.bo/ca/mamas-y-bebes/nutricion-infantil/leches-y-formulas/108/10806/1080603"
"""
import argparse
import re
import sys
from datetime import date

import pandas as pd
from playwright.sync_api import sync_playwright

from contrato import ProductoScrapeado, ScrapeRequest, ScrapeResult

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


def extraer_categoria(url: str) -> str:
    partes = [p for p in url.split("?")[0].rstrip("/").split("/") if p and not p.isdigit()]
    return partes[-1] if partes else "categoria"


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
    url_base = url_base.split("?")[0]  # por si ya venía con ?currentPage=N
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


def parse_precio_chavez(txt) -> float | None:
    """'Bs 207' -> 207.0 ; 'Bs 1.234,50' -> 1234.50"""
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


def productos_a_filas(productos: list[dict], categoria: str, cadena: str = "Farmacias Chavez") -> list[dict]:
    filas = []
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
            "Categoria": categoria.replace("-", " ").title(),
            "Subcategoria": "",
            "Ciudad": "Santa Cruz",
            "Sucursal": "",
            "URL": p["url"] if str(p["url"]).startswith("http") else f"https://www.farmaciaschavez.com.bo{p['url']}",
            "Imagen": p["imagen"] or "",
        })
    return filas


def ejecutar(request: ScrapeRequest) -> ScrapeResult:
    """El "agente único": misma firma que scraper_shopify.ejecutar() y
    scraper_hipermaxi.ejecutar(), aunque por dentro esto pagina por URL
    con Playwright en vez de pegarle a un JSON. request.url es la URL
    de categoría de Farmacias Chávez."""
    categoria = extraer_categoria(request.url)
    productos_raw = scrapear(request.url)
    filas = productos_a_filas(productos_raw, request.categoria_default or categoria, request.cadena)

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

    return ScrapeResult(cadena=request.cadena, fuente="playwright", productos=productos, errores=errores)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="URL de categoría de Farmacias Chávez")
    ap.add_argument("--out", help="Nombre del archivo .xlsx de salida")
    args = ap.parse_args()

    categoria = extraer_categoria(args.url)
    productos = scrapear(args.url)
    filas = productos_a_filas(productos, categoria)
    if not filas:
        print("No se encontraron productos. Revisa la URL o si Chávez cambió su estructura.")
        sys.exit(1)

    df = pd.DataFrame(filas)
    out = args.out or f"chavez_{categoria}_{date.today().isoformat()}.xlsx"
    df.to_excel(out, index=False)
    print(f"\nListo: {len(df)} filas -> {out}")
    print(f"Siguiente paso: python etl.py cargar-excel {out}")


if __name__ == "__main__":
    main()

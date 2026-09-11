"""
scraper_hipermaxi.py — Scraper para Hipermaxi (Next.js, sin JSON público).

No usa Claude ni tokens: usa Playwright (navegador Chromium headless
controlado por código) para renderizar la página como lo haría un
usuario real, hacer scroll hasta cargar todo el catálogo, y extraer del
DOM: nombre, precio, código interno (viene en la URL /producto/<codigo>/),
imagen y URL. Devuelve un .xlsx en el formato estándar.

Requiere:
    pip install playwright
    playwright install chromium      (solo una vez, descarga el navegador)

Uso:
    python scraper_hipermaxi.py <url_categoria> [--out archivo.xlsx]

Ejemplo:
    python scraper_hipermaxi.py https://www.hipermaxi.com/santa-cruz/hipermaxi-equipetrol/categoria/bebidas

La sucursal (ej. "Hipermaxi Equipetrol") se detecta sola de la URL.
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

from contrato import ProductoScrapeado, ScrapeRequest, ScrapeResult

EXTRAER_JS = """
() => {
  const cards = Array.from(document.querySelectorAll('article.card'));
  return cards.map(card => {
    const a = card.querySelector('a[href*="/producto/"]');
    const href = a ? a.getAttribute('href') : null;
    const m = href ? href.match(/\\/producto\\/(\\d+)\\/([^/?]+)/) : null;
    const img = card.querySelector('img');
    const priceEl = Array.from(card.querySelectorAll('*')).find(el =>
      el.children.length === 0 && /Bs\\s*[\\d.,]+/.test(el.textContent)
    );
    const precioTxt = priceEl ? priceEl.textContent.trim().replace('Bs','').trim() : null;
    let imgSrc = null;
    if (img && img.src) {
      const mm = img.src.match(/url=([^&]+)/);
      imgSrc = mm ? decodeURIComponent(decodeURIComponent(mm[1])) : img.src;
    }
    return {
      codigo: m ? m[1] : null,
      nombre: img ? img.getAttribute('title') : null,
      precio: precioTxt,
      url: href,
      imagen: imgSrc,
    };
  }).filter(p => p.codigo);
}
"""


def extraer_sucursal(url: str) -> str:
    # https://www.hipermaxi.com/santa-cruz/hipermaxi-equipetrol/categoria/bebidas
    m = re.search(r"hipermaxi\.com/[^/]+/([^/]+)/categoria/", url)
    if not m:
        return "Hipermaxi"
    slug = m.group(1)  # "hipermaxi-equipetrol"
    return slug.replace("-", " ").title()


def extraer_categoria(url: str) -> str:
    m = re.search(r"/categoria/([^/?]+)", url)
    return m.group(1) if m else "categoria"


def scrapear(url: str, headed: bool = False) -> list[dict]:
    sucursal = extraer_sucursal(url)
    with sync_playwright() as pw:
        # --disable-blink-features=AutomationControlled + tapar
        # navigator.webdriver: reduce las chances de que un sitio con
        # protección anti-bot detecte "esto es un navegador automatizado"
        # y lo trate distinto (bloqueos/timeouts tras varias corridas
        # seguidas, como nos pasó). No es invisibilidad total, pero ayuda.
        browser = pw.chromium.launch(
            headless=not headed,
            slow_mo=200 if headed else 0,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
            locale="es-BO",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        print(f"Abriendo {url} ...")
        # No usamos wait_until="networkidle": este sitio tiene tráfico de
        # fondo constante (monitoreo/analytics) que nunca queda inactivo,
        # así que networkidle siempre agota el timeout. Esperamos en cambio
        # a que el HTML base cargue y luego a que aparezcan los productos.
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("article.card", timeout=30000)
        except Exception:
            print("  ADVERTENCIA: no aparecieron productos en 30s.")
            print(f"  URL actual (revisa si hubo redirect): {page.url}")
            print(f"  Título de la página: {page.title()}")
            debug_png = Path("debug_hipermaxi.png")
            debug_html = Path("debug_hipermaxi.html")
            page.screenshot(path=str(debug_png), full_page=True)
            debug_html.write_text(page.content(), encoding="utf-8")
            print(f"  Guardé una captura en {debug_png} y el HTML en {debug_html} -- ábrelos para ver qué se cargó de verdad.")
            texto_visible = page.evaluate("document.body.innerText.slice(0, 400)")
            print(f"  Primeros 400 caracteres de texto visible en la página:\n---\n{texto_visible}\n---")
        page.wait_for_timeout(1500)

        # Scroll infinito real: el error anterior era considerar "estable"
        # apenas 3 scrolls seguidos sin cambio, sin haber llegado al fondo
        # de la página todavía -- por eso cortaba antes de darle chance a
        # cargar más. Ahora: 1) baja de a incrementos hasta tocar fondo,
        # 2) UNA VEZ en el fondo, espera a ver si entra contenido nuevo,
        # 3) si entra, sigue bajando (el fondo se movió); si no entra
        # después de varios intentos en el fondo, asume que ya cargó todo.
        sin_cambio_en_fondo = 0
        MAX_SIN_CAMBIO_EN_FONDO = 5
        anterior = -1
        for intento in range(200):
            en_fondo = page.evaluate(
                "(window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 150)"
            )
            if not en_fondo:
                page.mouse.wheel(0, 900)
                page.wait_for_timeout(500)
                continue

            page.wait_for_timeout(1500)  # dar tiempo a que la carga XHR entre
            actual = page.evaluate("document.querySelectorAll('article.card').length")
            if actual == anterior:
                sin_cambio_en_fondo += 1
                if sin_cambio_en_fondo >= MAX_SIN_CAMBIO_EN_FONDO:
                    break
            else:
                sin_cambio_en_fondo = 0
                print(f"  ... {actual} productos cargados hasta ahora")
            anterior = actual
            page.mouse.wheel(0, 900)  # por si el fondo se corrió, seguir bajando
            page.wait_for_timeout(500)

        productos = page.evaluate(EXTRAER_JS)
        browser.close()

    for p in productos:
        p["sucursal"] = sucursal
    print(f"  {len(productos)} productos encontrados en {sucursal}")
    return productos


def productos_a_filas(productos: list[dict], categoria: str, cadena: str = "Hipermaxi") -> list[dict]:
    filas = []
    for p in productos:
        try:
            precio = float(str(p["precio"]).replace(",", "."))
        except (TypeError, ValueError):
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
            "Sucursal": p["sucursal"],
            "URL": p["url"] if str(p["url"]).startswith("http") else f"https://hipermaxi.com{p['url']}",
            "Imagen": p["imagen"] or "",
        })
    return filas


def ejecutar(request: ScrapeRequest) -> ScrapeResult:
    """El "agente único": misma firma que scraper_shopify.ejecutar() y
    scraper_chavez.ejecutar(), aunque por dentro esto abre un Chromium
    headless con Playwright en vez de pegarle a un JSON. request.url es
    la URL de categoría de Hipermaxi."""
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
    ap.add_argument("url", help="URL de categoría de Hipermaxi")
    ap.add_argument("--out", help="Nombre del archivo .xlsx de salida")
    ap.add_argument("--headed", action="store_true",
                     help="Abre el navegador visible (no headless) para ver qué está pasando en vivo -- útil solo para depurar")
    args = ap.parse_args()

    categoria = extraer_categoria(args.url)
    productos = scrapear(args.url, headed=args.headed)
    filas = productos_a_filas(productos, categoria)
    if not filas:
        print("No se encontraron productos. Revisa la URL o si Hipermaxi cambió su estructura.")
        sys.exit(1)

    df = pd.DataFrame(filas)
    out = args.out or f"hipermaxi_{categoria}_{date.today().isoformat()}.xlsx"
    df.to_excel(out, index=False)
    print(f"\nListo: {len(df)} filas -> {out}")
    print(f"Siguiente paso: python etl.py cargar-excel {out}")


if __name__ == "__main__":
    main()

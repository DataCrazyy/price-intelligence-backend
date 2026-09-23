"""
scraper_hipermaxi.py — Scraper para Hipermaxi (Next.js, sin JSON público).

No usa Claude ni tokens: usa Playwright (navegador Chromium headless
controlado por código) para renderizar la página como lo haría un
usuario real, hacer scroll hasta cargar todo el catálogo, y extraer del
DOM: nombre, precio, código interno (viene en la URL /producto/<codigo>/),
imagen y URL. Devuelve un .xlsx en el formato estándar.

Requiere:
    pip install playwright
    playwright install chromium    (solo una vez, descarga el navegador)

Uso:
    python scraper_hipermaxi.py <url_categoria> [--out archivo.xlsx]
    python scraper_hipermaxi.py --descubrir <url_sucursal>
"""
import argparse
import json
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

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _nuevo_contexto(pw, headed: bool = False):
    """Navegador + contexto con las protecciones anti-detección: sin
    esto, Hipermaxi (que tiene un WAF/anti-bot -- Perfdrive/hCaptcha
    detectado en el tráfico de la página) puede tratar distinto una
    sesión que "huele" a automatizada."""
    browser = pw.chromium.launch(
        headless=not headed,
        slow_mo=200 if headed else 0,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="es-BO",
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


def extraer_sucursal(url: str) -> str:
    # https://www.hipermaxi.com/santa-cruz/hipermaxi-equipetrol/categoria/bebidas
    m = re.search(r"hipermaxi\.com/[^/]+/([^/]+)/categoria/", url)
    if not m:
        return "Hipermaxi"
    slug = m.group(1)  # "hipermaxi-equipetrol"
    return slug.replace("-", " ").title()


def extraer_categoria(url: str) -> tuple[str, str]:
    """Categoría y subcategoría desde el path de la URL: todo lo que
    viene después de '/categoria/' se separa por '/' -- el primer
    segmento es la categoría, el segundo (si existe) es la subcategoría."""
    partes = url.split("/categoria/")[-1].split("/")
    categoria = partes[0] if partes and partes[0] else "categoria"
    subcategoria = partes[1] if len(partes) > 1 and partes[1] else ""
    return categoria, subcategoria


def extraer_nombre_de_url(url: str) -> str:
    """Fallback cuando la página no trae el nombre en el 'title' de la
    imagen (Hipermaxi dejó de incluirlo en algún momento). El slug de la
    URL de producto (/producto/<codigo>/<slug-del-nombre>) sigue trayendo
    el nombre real, solo que con guiones en vez de espacios."""
    m = re.search(r"/producto/\d+/([^/?]+)", str(url or ""))
    return m.group(1).replace("-", " ").title() if m else ""


def descubrir_categorias_y_subcategorias(
    url_sucursal: str, headed: bool = False, out_path: str = "hipermaxi_categorias_descubiertas.json"
) -> list[dict]:
    resultados = []
    with sync_playwright() as pw:
        # --- Fase 1: categorías principales, desde la home ---
        browser, context = _nuevo_contexto(pw, headed=headed)
        page_home = context.new_page()
        print(f"Abriendo {url_sucursal} para descubrir categorías y subcategorías...")
        page_home.goto(url_sucursal, wait_until="domcontentloaded", timeout=60000)
        page_home.wait_for_timeout(3000)

        hrefs_iniciales = page_home.eval_on_selector_all(
            'a[href*="/categoria/"]',
            'els => [...new Set(els.map(e => e.getAttribute("href")))]',
        )
        categorias = {}
        for href in hrefs_iniciales:
            if not href:
                continue
            m = re.search(r"/categoria/([^/?]+)/?$", href)
            if m:
                slug = m.group(1)
                url_completa = href if href.startswith("http") else f"https://www.hipermaxi.com{href}"
                categorias[slug] = url_completa.split("?")[0].rstrip("/")
        context.close()
        browser.close()

        print(f"  {len(categorias)} categorías principales detectadas. Analizando subcategorías...")

        # --- Fase 2: por cada categoría, BROWSER Y CONTEXT NUEVOS ---
        # No es solo la página -- el contexto (cookies/sesión) compartido
        # entre categorías es lo que hace que el WAF de Hipermaxi vaya
        # "desconfiando" a medida que avanza la secuencia (confirmado:
        # aislada con contexto propio, "bebidas" trae sus 14 subcategorías
        # perfectas; en secuencia con contexto compartido, solo 1).
        for slug_cat, url_cat in categorias.items():
            print(f" Explorando categoría: '{slug_cat}' ...")
            subslugs = set()

            for intento in range(2):
                browser_cat, context_cat = _nuevo_contexto(pw, headed=headed)
                pagina_cat = context_cat.new_page()
                try:
                    pagina_cat.goto(url_cat, wait_until="domcontentloaded", timeout=30000)
                    pagina_cat.wait_for_timeout(3000)
                    hrefs_sub = pagina_cat.eval_on_selector_all(
                        f'a[href*="/categoria/{slug_cat}/"]',
                        'els => [...new Set(els.map(e => e.getAttribute("href")))]',
                    )
                    patron = re.compile(rf"/categoria/{re.escape(slug_cat)}/([^/?]+)/?")
                    for href in hrefs_sub:
                        if not href:
                            continue
                        m = patron.search(href)
                        if m and m.group(1) != slug_cat:
                            subslugs.add(m.group(1))
                    browser_cat.close()
                    break  # navegación OK, no hace falta reintentar
                except Exception as e:
                    browser_cat.close()
                    if intento == 0:
                        print(f"    (reintentando '{slug_cat}' tras error: {e})")
                    else:
                        print(f"    ⚠️ Aviso en '{slug_cat}' tras reintentar: {e}. Usando URL base.")

            if not subslugs:
                resultados.append({"categoria": slug_cat.replace("-", " ").title(), "subcategoria": None, "url": url_cat})
            else:
                for subslug in sorted(subslugs):
                    resultados.append({
                        "categoria": slug_cat.replace("-", " ").title(),
                        "subcategoria": subslug.replace("-", " ").title(),
                        "url": f"{url_cat}/{subslug}",
                    })
            print(f"    -> {len(subslugs) or 1} entrada(s) para '{slug_cat}'")

            Path(out_path).write_text(json.dumps(resultados, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{len(resultados)} entradas totales guardadas en {out_path}")
    return resultados


def main_descubrir():
    url_sucursal = sys.argv[2] if len(sys.argv) > 2 else "https://www.hipermaxi.com/santa-cruz/hipermaxi-equipetrol"
    descubrir_categorias_y_subcategorias(url_sucursal)


def scrapear(url: str, headed: bool = False) -> list[dict]:
    sucursal = extraer_sucursal(url)
    with sync_playwright() as pw:
        browser, context = _nuevo_contexto(pw, headed=headed)
        page = context.new_page()
        print(f"Abriendo {url} ...")
        # No usamos wait_until="networkidle": este sitio tiene tráfico de
        # fondo constante (monitoreo/analytics/anti-bot) que nunca queda
        # inactivo, así que networkidle siempre agota el timeout. Esperamos
        # en cambio a que el HTML base cargue y luego a que aparezcan los
        # productos.
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

        # Scroll infinito real: baja de a incrementos hasta tocar fondo, y
        # UNA VEZ en el fondo, espera a ver si entra contenido nuevo antes
        # de darse por vencido (evita cortar antes de que cargue todo).
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
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(500)

        productos = page.evaluate(EXTRAER_JS)
        browser.close()

    for p in productos:
        p["sucursal"] = sucursal
    print(f"  {len(productos)} productos encontrados en {sucursal}")
    return productos


def productos_a_filas(productos: list[dict], categoria: str, cadena: str = "Hipermaxi", subcategoria: str = "") -> list[dict]:
    filas = []
    for p in productos:
        try:
            precio = float(str(p["precio"]).replace(",", "."))
        except (TypeError, ValueError):
            continue

        # Fallback: si el 'title' de la imagen no trae el nombre (pasó
        # con Hipermaxi en algún momento -- dejaron de incluirlo), lo
        # sacamos del slug de la URL de producto en su lugar.
        nombre = p["nombre"] or extraer_nombre_de_url(p.get("url"))

        filas.append({
            "Articulo": nombre,
            "Precio_Oferta": precio,
            "Precio_Regular": precio,
            "CADENA": cadena,
            "COD_ARTICULO": p["codigo"],
            "Categoria": categoria.replace("-", " ").title(),
            "Subcategoria": subcategoria.replace("-", " ").title() if subcategoria else "",
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
    categoria, subcategoria = extraer_categoria(request.url)
    productos_raw = scrapear(request.url)
    filas = productos_a_filas(
        productos_raw, request.categoria_default or categoria, request.cadena, subcategoria
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
        errores=errores, raw_data=raw_data,
    )


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--descubrir":
        main_descubrir()
        return

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="URL de categoría de Hipermaxi")
    ap.add_argument("--out", help="Nombre del archivo .xlsx de salida")
    ap.add_argument("--headed", action="store_true",
                     help="Abre el navegador visible (no headless) para ver qué está pasando en vivo -- útil solo para depurar")
    args = ap.parse_args()

    categoria, subcategoria = extraer_categoria(args.url)
    productos = scrapear(args.url, headed=args.headed)
    filas = productos_a_filas(productos, categoria, subcategoria=subcategoria)
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
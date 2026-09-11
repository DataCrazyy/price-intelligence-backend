"""
etl.py — Carga precios.json y los Excel del scraping a Postgres.
==================================================================

Uso:
    # 1) Cargar tu precios.json actual (ya trae producto_clave "canónico"
    #    porque tu matching manual ya decidió qué es lo mismo entre cadenas)
    python etl.py cargar-json ../../data/precios.json

    # 2) Cargar un Excel nuevo que te generó un agente scraper (una cadena
    #    a la vez, todavía SIN matching entre cadenas — cada fila se crea
    #    como su propio producto "pendiente")
    python etl.py cargar-excel Farmacorp_Leches_Formulas.xlsx --cadena Farmacorp
    python etl.py cargar-excel Chavez_Leches_Formulas.xlsx --cadena "Farmacias Chavez"

    # 3) Correr el agente de matching sobre lo "pendiente" para fusionar
    #    productos iguales entre cadenas (usa la misma lógica de
    #    match_productos.py que ya tienes)
    python etl.py matchear

Todas las operaciones son IDEMPOTENTES: correr el mismo archivo dos veces
no duplica nada — actualiza lo que cambió e inserta historial sólo si el
precio es distinto al de la última captura.
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from rapidfuzz import fuzz

import matching_llm

load_dotenv()
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://price_intel:change_me_local_only@localhost:5432/price_intel",
)


def get_conn():
    return psycopg2.connect(DATABASE_URL)


# =========================================================================
# Helpers de normalización (mismos que match_productos.py, resumidos)
# =========================================================================
BRANDS = ["NAN", "NUTRILON", "BEBELAC", "PRIMALAC", "SANCOR", "PRIMAPLUS", "NEOCATE", "NIDO"]


def slugify(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in nfkd if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text


# Palabras que distintos catálogos escriben distinto para lo mismo
# (género, singular/plural, idioma). El matching ya reordena palabras
# (token_sort_ratio), así que el problema real no es el ORDEN -- es la
# variante exacta de la palabra. Esto se aplica ANTES de comparar, para
# que "Women" y "Woman" (o "Men"/"Mens") cuenten como la misma palabra.
# Agrega acá cualquier par nuevo que veas fallar en el frontend.
SINONIMOS_PALABRA = {
    "WOMEN": "WOMAN", "MENS": "MAN", "MEN": "MAN",
    "KIDS": "KID", "NINOS": "KID", "NINAS": "KID",
    "DIENTES": "DENTAL",
}


def normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in nfkd if not unicodedata.combining(c)).upper()
    text = re.sub(r"[^A-Z0-9\s.]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    palabras = [SINONIMOS_PALABRA.get(w, w) for w in text.split(" ")]
    return " ".join(palabras)


def extract_brand(name_norm: str) -> str:
    for b in BRANDS:
        if re.search(rf"\b{b}\b", name_norm):
            return b
    return ""


STAGE_WORDS = {
    "AR": "AR", "AE": "AE", "LF": "LF", "COMFORT": "COMFORT", "PRE": "PRE",
    "SOYA": "SOYA", "PREMATUROS": "PREMATURO", "PREMATURE": "PREMATURO",
    "SUPREME": "SUPREME", "OPTIPRO": "OPTIPRO",
}


def extract_size_grams(name_norm: str):
    """Tamaño normalizado en gramos (ml se marca negativo para no
    confundirse con gramos). None si no se detecta presentación."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*KG", name_norm)
    if m:
        return float(m.group(1)) * 1000
    m = re.search(r"(\d+(?:\.\d+)?)\s*G(?:R)?\b", name_norm)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*ML", name_norm)
    if m:
        return -float(m.group(1))
    return None


def extract_stage(name_norm: str):
    m = re.search(r"\b([123])\b", name_norm)
    stage_num = m.group(1) if m else None
    stage_word = None
    for w, canon in STAGE_WORDS.items():
        if re.search(rf"\b{w}\b", name_norm):
            stage_word = canon
            break
    return stage_num, stage_word


# =========================================================================
# Get-or-create helpers (así se hace un upsert legible en psycopg2)
# =========================================================================

# Distintos scrapers escriben el nombre de la misma cadena distinto
# (mayúsculas, errores de tipeo como "FILDAGA"). Sin esto, cada variante
# crea una cadena nueva y tu dashboard muestra "competidores fantasma"
# duplicados. Agrega aquí cualquier variante nueva que encuentres.
CADENA_CANONICA = {
    "HIPERMAXI": "Hipermaxi",
    "FIDALGA": "Fidalga",
    "FILDAGA": "Fidalga",       # errata real encontrada en df_fidalga_28_08_2026.xlsx
    "AMARKET": "Amarket",
    "FARMACORP": "Farmacorp",
    "FARMACIAS CHAVEZ": "Farmacias Chavez",
    "CHAVEZ": "Farmacias Chavez",
}


def canonicalizar_cadena(nombre: str) -> str:
    return CADENA_CANONICA.get(nombre.strip().upper(), nombre.strip())


# Mismo problema, mismo remedio: cada sitio le pone un nombre distinto a la
# misma categoría real ("Pasta Dental" vs "Pasta de Dientes", "Cuidado E
# Higiene" vs "Cuidado Personal"). Sin esto, cada variante aparece como un
# chip de filtro separado en el frontend aunque sean la misma cosa. Agrega
# acá cualquier variante nueva que encuentres -- la clave es como aparece
# en el sitio de origen (en mayúsculas), el valor es el nombre "oficial"
# que va a mostrar el frontend.
CATEGORIA_CANONICA = {
    "PASTA DENTAL": "Pasta de Dientes",
    "PASTA DE DIENTE": "Pasta de Dientes",
    "CUIDADO E HIGIENE": "Cuidado Personal",
    "HIGIENE PERSONAL": "Cuidado Personal",
    "SELF CARE": "Cuidado Personal",
    "LECHES": "Leches y Formulas",
    "LECHES Y FORMULA": "Leches y Formulas",
    "NUTRICION INFANTIL": "Leches y Formulas",
    "BEBIDAS Y LICORES": "Bebidas",
    "OTROS LACTEOS": "Lacteos",
    # A medida que se agreguen más cadenas/categorías (ej. al escalar a
    # ~90.000 SKUs), sumar acá cualquier variante nueva que aparezca --
    # esto es lo que evita que matchear() (que ahora agrupa por categoría,
    # ver más abajo) deje de comparar dos productos iguales solo porque
    # cada sitio les puso un nombre de categoría distinto.
}


def _clave_categoria(nombre: str) -> str:
    """Clave de búsqueda insensible a acentos/mayúsculas/espacios extra --
    antes el lookup era solo .strip().upper(), así que 'Pastá Dental' (con
    acento, típico de un sitio mal codificado) NO encontraba la entrada
    'PASTA DENTAL' del diccionario y quedaba como categoría suelta,
    aunque fuera la misma. Esto reduce cuánto hay que mantener el
    diccionario a mano sin cambiar el nombre "oficial" que se muestra."""
    nfkd = unicodedata.normalize("NFKD", nombre or "")
    sin_acentos = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sin_acentos.strip().upper())


def canonicalizar_categoria(nombre: str) -> str:
    if not nombre:
        return nombre
    return CATEGORIA_CANONICA.get(_clave_categoria(nombre), nombre.strip())


def get_or_create_cadena(cur, nombre: str, pais: str = "BO", sitio_web: str = None) -> int:
    nombre = canonicalizar_cadena(nombre)
    # comparación case-insensitive: "hipermaxi" y "Hipermaxi" son la misma cadena
    cur.execute("SELECT id FROM cadenas WHERE lower(nombre) = lower(%s)", (nombre,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO cadenas (nombre, pais, sitio_web) VALUES (%s, %s, %s) RETURNING id",
        (nombre, pais, sitio_web),
    )
    return cur.fetchone()[0]


def get_or_create_sucursal(cur, cadena_id: int, nombre: str, ciudad: str = "Santa Cruz"):
    if not nombre:
        return None
    cur.execute(
        "SELECT id FROM sucursales WHERE cadena_id = %s AND nombre = %s",
        (cadena_id, nombre),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO sucursales (cadena_id, nombre, ciudad) VALUES (%s, %s, %s) RETURNING id",
        (cadena_id, nombre, ciudad),
    )
    return cur.fetchone()[0]


def get_or_create_producto(cur, producto_clave: str, nombre: str, marca: str,
                            categoria: str, subcategoria: str) -> str:
    cur.execute("SELECT id FROM productos WHERE producto_clave = %s", (producto_clave,))
    row = cur.fetchone()
    if row:
        cur.execute(
            "UPDATE productos SET nombre=%s, marca=%s, categoria=%s, subcategoria=%s, "
            "actualizado_en=now() WHERE id=%s",
            (nombre, marca, categoria, subcategoria, row[0]),
        )
        return row[0]
    cur.execute(
        "INSERT INTO productos (producto_clave, nombre, marca, categoria, subcategoria) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (producto_clave, nombre, marca, categoria, subcategoria),
    )
    return cur.fetchone()[0]


def find_listado(cur, cadena_id: int, codigo_cadena: str):
    """Devuelve (listado_id, producto_id) si el listado ya existe, o None."""
    cur.execute(
        "SELECT id, producto_id FROM listados WHERE cadena_id = %s AND codigo_cadena = %s",
        (cadena_id, codigo_cadena),
    )
    return cur.fetchone()


def upsert_listado(cur, producto_id: str, cadena_id: int, sucursal_id, codigo_cadena: str,
                    nombre_original: str, url: str, imagen: str, match_metodo: str = "manual") -> str:
    """OJO: si el listado YA EXISTE, este helper NO toca su producto_id.
    Reasignar producto_id en cada carga deshace cualquier fusión que haya
    hecho el agente de matching (bug real que encontramos probando esto:
    volver a cargar el mismo Excel "resucitaba" el producto huérfano y
    desconectaba el listado del producto canónico ya fusionado). El único
    que tiene permiso de cambiar producto_id es matchear()."""
    existente = find_listado(cur, cadena_id, codigo_cadena)
    if existente:
        listado_id, producto_id_actual = existente
        cur.execute(
            "UPDATE listados SET sucursal_id=%s, nombre_original=%s, "
            "url=%s, imagen=%s, activo=TRUE, actualizado_en=now() WHERE id=%s",
            (sucursal_id, nombre_original, url, imagen, listado_id),
        )
        return listado_id, producto_id_actual
    cur.execute(
        "INSERT INTO listados (producto_id, cadena_id, sucursal_id, codigo_cadena, "
        "nombre_original, url, imagen, match_metodo) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (producto_id, cadena_id, sucursal_id, codigo_cadena, nombre_original, url, imagen, match_metodo),
    )
    return cur.fetchone()[0], producto_id


def upsert_precio_actual(cur, listado_id: str, precio_regular: float, precio_oferta: float,
                          agente_run_id: str = None):
    cur.execute(
        """
        INSERT INTO precios (listado_id, precio_regular, precio_oferta, agente_run_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (listado_id) DO UPDATE
        SET precio_regular = EXCLUDED.precio_regular,
            precio_oferta  = EXCLUDED.precio_oferta,
            capturado_en   = now(),
            agente_run_id  = EXCLUDED.agente_run_id
        """,
        (listado_id, precio_regular, precio_oferta, agente_run_id),
    )


def insert_historial_si_cambio(cur, listado_id: str, fecha: date, precio_regular: float,
                                precio_oferta: float, agente_run_id: str = None):
    """Sólo escribe una fila nueva en historial_precios si el precio de
    oferta cambió respecto a la última captura — así el histórico no crece
    con 365 filas idénticas al año por producto."""
    cur.execute(
        "SELECT precio_oferta FROM historial_precios WHERE listado_id = %s "
        "ORDER BY fecha DESC LIMIT 1",
        (listado_id,),
    )
    ultimo = cur.fetchone()
    if ultimo is not None and float(ultimo[0]) == float(precio_oferta):
        return  # sin cambios, no duplicar
    cur.execute(
        """
        INSERT INTO historial_precios (listado_id, fecha, precio_regular, precio_oferta, agente_run_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (listado_id, fecha) DO UPDATE
        SET precio_regular = EXCLUDED.precio_regular,
            precio_oferta  = EXCLUDED.precio_oferta
        """,
        (listado_id, fecha, precio_regular, precio_oferta, agente_run_id),
    )


def start_agent_run(cur, agente_tipo: str, cadena_id=None, fuente_metodo: str = None) -> str:
    """Se llama con dos tipos de cursor distintos según quién lo use:
    run_all.py pasa un cursor normal (fila = tupla, se lee con [0]), pero
    matchear() pasa un RealDictCursor (fila = dict, [0] tira KeyError --
    hay que leer con ["id"]). Detectar el tipo de fila y leer como
    corresponda evita duplicar esta función para cada caso."""
    cur.execute(
        "INSERT INTO agent_runs (agente_tipo, cadena_id, fuente_metodo) "
        "VALUES (%s, %s, %s) RETURNING id",
        (agente_tipo, cadena_id, fuente_metodo),
    )
    fila = cur.fetchone()
    return fila["id"] if isinstance(fila, dict) else fila[0]


def finish_agent_run(cur, run_id: str, estado: str, procesados: int, errores: int, detalle: dict = None):
    cur.execute(
        "UPDATE agent_runs SET estado=%s, registros_procesados=%s, registros_error=%s, "
        "finalizado_en=now(), detalle=%s WHERE id=%s",
        (estado, procesados, errores, json.dumps(detalle or {}), run_id),
    )


# =========================================================================
# Carga 1: precios.json (formato que ya usa tu frontend)
# =========================================================================

def cargar_json(path: str):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    conn = get_conn()
    cur = conn.cursor()
    run_id = start_agent_run(cur, "carga_manual", fuente_metodo="json_frontend")
    procesados, errores = 0, 0

    for p in data.get("productos", []):
        try:
            cadena_id = get_or_create_cadena(cur, p.get("cadena") or "Sin cadena")
            sucursal_id = get_or_create_sucursal(cur, cadena_id, p.get("sucursal"), p.get("ciudad") or "Santa Cruz")
            codigo_cadena = str(p.get("codigo_cadena") or p.get("id") or slugify(p["nombre"]))

            existente = find_listado(cur, cadena_id, codigo_cadena)
            if existente:
                # el listado ya existe: NO tocamos su producto_id (podría
                # ya estar fusionado por matchear()), sólo refrescamos datos.
                producto_id = existente[1]
            else:
                producto_clave = p.get("producto_clave") or p.get("sku_global") or slugify(p["nombre"])
                producto_id = get_or_create_producto(
                    cur, producto_clave, p["nombre"], p.get("marca"),
                    p.get("categoria"), p.get("subcategoria"),
                )

            listado_id, producto_id = upsert_listado(
                cur, producto_id, cadena_id, sucursal_id, codigo_cadena,
                p["nombre"], p.get("url"), p.get("imagen"),
            )

            upsert_precio_actual(cur, listado_id, p["precio_regular"], p["precio_oferta"], run_id)

            hoy = data.get("fecha_actualizacion") or date.today().isoformat()
            insert_historial_si_cambio(cur, listado_id, hoy, p["precio_regular"], p["precio_oferta"], run_id)
            for h in p.get("historial", []):
                precio_h = h.get("precio_oferta", h.get("precio"))
                insert_historial_si_cambio(cur, listado_id, h["fecha"], precio_h, precio_h, run_id)

            procesados += 1
        except Exception as e:
            errores += 1
            print(f"[error] {p.get('nombre')}: {e}", file=sys.stderr)

    finish_agent_run(cur, run_id, "ok" if errores == 0 else "ok_con_errores", procesados, errores)
    conn.commit()
    cur.close()
    conn.close()
    print(f"cargar-json: {procesados} productos cargados, {errores} errores")


# =========================================================================
# Carga 2: Excel crudo de un agente scraper (una cadena, sin matching aún)
# =========================================================================

COLUMN_ALIASES = {
    "nombre":            ["Nombre", "nombre", "Articulo", "Artículo"],
    "marca":              ["Marca", "marca"],
    "categoria":          ["Categoria", "Categoría", "categoria"],
    "subcategoria":       ["Subcategoria", "Subcategoría", "subcategoria"],
    "precio_oferta":      ["Precio_Oferta", "Precio (Bs)", "Precio", "precio"],
    "precio_regular":     ["Precio_Regular", "Precio Regular (Bs)", "precio_regular"],
    "descripcion":        ["Descripcion", "Descripción", "descripcion"],
    "imagen":             ["Imagen", "imagen"],
    "url":                ["URL", "Url", "url"],
    "codigo_cadena":      ["COD_ARTICULO", "codigo_cadena", "Codigo"],
    "cadena_col":         ["CADENA", "Cadena"],
    "ciudad":             ["Ciudad", "ciudad"],
    "sucursal":           ["Sucursal", "sucursal"],
}


def _pick_col(df, field):
    for alias in COLUMN_ALIASES[field]:
        if alias in df.columns:
            return alias
    return None


def parse_precio(valor):
    """Algunos scrapers guardan el precio ya numérico (9.7), otros como
    texto con formato boliviano: 'Bs9,70' (prefijo 'Bs' + coma decimal).
    Esta función entiende ambos."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    s = str(valor).strip()
    s = re.sub(r"[^0-9,.\-]", "", s)  # saca "Bs", espacios, etc.
    if not s:
        return None
    if "," in s and "." in s:
        # el separador que aparece último es el decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def cargar_excel(path: str, cadena_nombre: str = None, pais: str = "BO"):
    """cadena_nombre es OPCIONAL si el Excel ya trae una columna CADENA
    (el formato real de tu scraper) — cada fila usa entonces su propio
    valor de esa columna; --cadena queda como respaldo/override."""
    df = pd.read_excel(path)
    col = {f: _pick_col(df, f) for f in COLUMN_ALIASES}

    conn = get_conn()
    cur = conn.cursor()
    cadena_ids_cache = {}
    run_id = start_agent_run(cur, "scraper", fuente_metodo="excel_scraper")
    procesados, errores = 0, 0

    def cadena_id_para(nombre_cadena: str) -> int:
        if nombre_cadena not in cadena_ids_cache:
            cadena_ids_cache[nombre_cadena] = get_or_create_cadena(cur, nombre_cadena, pais)
        return cadena_ids_cache[nombre_cadena]

    for idx, row in df.iterrows():
        try:
            nombre = str(row[col["nombre"]]).strip()

            cadena_fila = (
                str(row[col["cadena_col"]]).strip()
                if col["cadena_col"] and pd.notna(row[col["cadena_col"]])
                else cadena_nombre
            )
            if not cadena_fila:
                raise ValueError("no se pudo determinar la cadena (ni columna CADENA ni --cadena)")
            cadena_id = cadena_id_para(cadena_fila)

            precio_oferta = parse_precio(row[col["precio_oferta"]]) if col["precio_oferta"] else None
            precio_oferta = precio_oferta if precio_oferta is not None else 0.0
            precio_regular = parse_precio(row[col["precio_regular"]]) if col["precio_regular"] else None
            precio_regular = precio_regular if precio_regular is not None else precio_oferta
            if precio_regular < precio_oferta:  # dato sucio: el "regular" nunca debería ser menor
                precio_regular = precio_oferta

            marca = str(row[col["marca"]]).strip() if col["marca"] and pd.notna(row[col["marca"]]) else extract_brand(normalize(nombre))
            categoria = str(row[col["categoria"]]).strip() if col["categoria"] and pd.notna(row[col["categoria"]]) else None
            categoria = canonicalizar_categoria(categoria)
            subcategoria = str(row[col["subcategoria"]]).strip() if col["subcategoria"] and pd.notna(row[col["subcategoria"]]) else None
            subcategoria = canonicalizar_categoria(subcategoria)
            if not subcategoria:
                # El frontend arma el filtro de categorías con "subcategoria",
                # no con "categoria" -- si el scraper no trae una subcategoría
                # real (la mayoría no la tiene), usamos la categoría como
                # respaldo para que el producto siempre aparezca en algún
                # chip filtrable, en vez de quedar invisible en la lista.
                subcategoria = categoria
            imagen = str(row[col["imagen"]]).strip() if col["imagen"] and pd.notna(row[col["imagen"]]) else None
            url = str(row[col["url"]]).strip() if col["url"] and pd.notna(row[col["url"]]) else None
            ciudad = str(row[col["ciudad"]]).strip() if col["ciudad"] and pd.notna(row[col["ciudad"]]) else "Santa Cruz"
            sucursal_nombre = str(row[col["sucursal"]]).strip() if col["sucursal"] and pd.notna(row[col["sucursal"]]) else None
            sucursal_id = get_or_create_sucursal(cur, cadena_id, sucursal_nombre, ciudad) if sucursal_nombre else None

            # el código real de la tienda (COD_ARTICULO) es mucho más
            # confiable que inventar uno con el nombre — úsalo si el
            # archivo lo trae.
            if col["codigo_cadena"] and pd.notna(row[col["codigo_cadena"]]):
                codigo_cadena = str(row[col["codigo_cadena"]]).strip()
            else:
                codigo_cadena = slugify(nombre)[:60]

            existente = find_listado(cur, cadena_id, codigo_cadena)
            if existente:
                # ya lo conocíamos (venga de un excel anterior o ya fusionado
                # por matchear): conservamos su producto_id tal cual está,
                # PERO refrescamos categoria/subcategoria/marca -- si no lo
                # hiciéramos, una categoría mal puesta la primera vez (ej.
                # el caso real de Farmacorp con categoria=NULL que quedó
                # parchada a mano como "Leches y Formulas" para TODOS sus
                # productos, aunque muchos son de otras categorías) nunca se
                # corregiría con un re-scrape normal -- habría que borrar
                # datos a mano cada vez. COALESCE: si este Excel en particular
                # no trae categoria (columna ausente), no pisamos la que ya
                # había con NULL.
                listado_id, producto_id = existente
                cur.execute(
                    "UPDATE productos SET marca=COALESCE(%s, marca), "
                    "categoria=COALESCE(%s, categoria), subcategoria=COALESCE(%s, subcategoria), "
                    "actualizado_en=now() WHERE id=%s",
                    (marca, categoria, subcategoria, producto_id),
                )
            else:
                # primera vez que vemos este listado: se crea como su propio
                # "producto pendiente" — el paso de matching lo fusiona si
                # corresponde, más abajo o en una corrida posterior.
                producto_clave = f"{slugify(cadena_fila)}-{codigo_cadena}"
                producto_id = get_or_create_producto(cur, producto_clave, nombre, marca, categoria, subcategoria)

            listado_id, producto_id = upsert_listado(
                cur, producto_id, cadena_id, sucursal_id, codigo_cadena, nombre, url, imagen,
                match_metodo="pendiente",
            )
            upsert_precio_actual(cur, listado_id, precio_regular, precio_oferta, run_id)
            insert_historial_si_cambio(cur, listado_id, date.today().isoformat(), precio_regular, precio_oferta, run_id)
            procesados += 1
        except Exception as e:
            errores += 1
            print(f"[error] fila {idx}: {e}", file=sys.stderr)

    finish_agent_run(cur, run_id, "ok" if errores == 0 else "ok_con_errores", procesados, errores)
    conn.commit()
    cur.close()
    conn.close()
    print(f"cargar-excel ({path}): {procesados} filas cargadas, {errores} errores, cadenas detectadas: {list(cadena_ids_cache)}")


def cargar_resultado(resultado, pais: str = "BO", etiqueta: str = None) -> dict:
    """Carga un ScrapeResult (el 'contrato único' de contrato.py) directo
    a Postgres -- mismo camino que cargar_excel (get_or_create_cadena ->
    find_listado -> upsert_listado -> upsert_precio_actual -> historial),
    pero sin pasar por Excel ni por el adivinador de columnas
    (COLUMN_ALIASES). Como los productos ya vienen tipados y validados
    por Pydantic antes de llegar acá, esto elimina de raíz el problema
    del redondeo Excel -> NaN que dejó la categoría de Farmacorp en NULL
    (ver ESTANDAR_SCRAPING.md).

    `etiqueta`: identificador legible de QUÉ se scrapeó (ej.
    "Fidalga/lacteos", o la URL para las cadenas de navegador) -- lo pasa
    run_all.py, que ya lo tiene armado. Antes de este fix el `agent_runs`
    de cada carga quedaba con fuente_metodo genérico ("shopify_json" para
    TODAS las categorías de una cadena Shopify, indistinguibles entre sí)
    y sin cadena_id (se calculaba DESPUÉS de crear la fila, así que nunca
    se guardaba) -- el panel no podía mostrar qué cadena/categoría fue
    cada corrida exitosa, solo las que fallaban."""
    conn = get_conn()
    cur = conn.cursor()
    cadena_id = get_or_create_cadena(cur, resultado.cadena, pais)
    run_id = start_agent_run(cur, "scraper", cadena_id=cadena_id, fuente_metodo=etiqueta or resultado.fuente)
    procesados, errores = 0, len(resultado.errores)

    for p in resultado.productos:
        try:
            categoria = canonicalizar_categoria(p.categoria)
            subcategoria = canonicalizar_categoria(p.subcategoria) or categoria
            marca = extract_brand(normalize(p.nombre))
            sucursal_id = get_or_create_sucursal(cur, cadena_id, p.sucursal, p.ciudad) if p.sucursal else None
            codigo_cadena = p.codigo_articulo.strip()

            precio_oferta = p.precio_oferta
            precio_regular = p.precio_regular if p.precio_regular >= precio_oferta else precio_oferta

            existente = find_listado(cur, cadena_id, codigo_cadena)
            if existente:
                # ya lo conocíamos: conservamos su producto_id, pero
                # refrescamos categoria/subcategoria/marca -- mismo
                # criterio que cargar_excel (ver nota ahí).
                listado_id, producto_id = existente
                cur.execute(
                    "UPDATE productos SET marca=COALESCE(%s, marca), "
                    "categoria=COALESCE(%s, categoria), subcategoria=COALESCE(%s, subcategoria), "
                    "actualizado_en=now() WHERE id=%s",
                    (marca, categoria, subcategoria, producto_id),
                )
            else:
                producto_clave = f"{slugify(resultado.cadena)}-{codigo_cadena}"
                producto_id = get_or_create_producto(cur, producto_clave, p.nombre, marca, categoria, subcategoria)

            listado_id, producto_id = upsert_listado(
                cur, producto_id, cadena_id, sucursal_id, codigo_cadena, p.nombre, p.url, p.imagen,
                match_metodo="pendiente",
            )
            upsert_precio_actual(cur, listado_id, precio_regular, precio_oferta, run_id)
            insert_historial_si_cambio(cur, listado_id, date.today().isoformat(), precio_regular, precio_oferta, run_id)
            procesados += 1
        except Exception as e:
            errores += 1
            print(f"[error] {p.nombre}: {e}", file=sys.stderr)

    finish_agent_run(cur, run_id, "ok" if errores == 0 else "ok_con_errores", procesados, errores)
    conn.commit()
    cur.close()
    conn.close()
    print(f"cargar-resultado ({resultado.cadena}): {procesados} productos cargados, {errores} errores")
    return {"cadena": resultado.cadena, "procesados": procesados, "errores": errores}


# =========================================================================
# Paso 3: Agente de matching — fusiona productos "pendientes" entre cadenas
# =========================================================================

MIN_SCORE = 78

# Fase 2 -- zona gris: por debajo de MIN_SCORE pero por encima de esto,
# no es lo bastante claro para fusionar solo ni lo bastante bajo para
# descartar sin mirar -- se consulta al LLM (con cache, ver matching_llm.py
# y match_revisiones_llm). Por debajo de AMBIGUO_MIN se asume que son
# productos distintos y ni se pregunta (ahorra costo en pares obviamente
# no relacionados).
AMBIGUO_MIN = int(os.environ.get("MATCH_AMBIGUO_MIN", 60))
# Tope de consultas NUEVAS al LLM por corrida de matchear() -- controla el
# costo de la primera corrida cuando hay muchos pares ambiguos de golpe.
# Los que no alcanzan a revisarse quedan pendientes para la próxima
# corrida (no se pierden, sólo se posponen).
MAX_LLM_POR_CORRIDA = int(os.environ.get("MATCH_LLM_MAX_POR_CORRIDA", 50))
# Filtro barato de precio (ver matchear()): si el par tiene precios muy
# dispares se descarta antes de llamar a _score(), sin gastar el fuzzy-text.
# 4x es generoso a propósito -- mejor de más que arriesgar descartar un
# match real por una oferta agresiva.
PRECIO_RATIO_MAX = float(os.environ.get("MATCH_PRECIO_RATIO_MAX", 4.0))


def _score(a, b):
    """Replica la lógica de match_productos.py: texto + marca + PRESENTACIÓN
    + etapa. Sin el chequeo de tamaño, 'Bebelac Gold 1 X 400g' y
    'Bebelac Gold 1 X 900g' matchean por texto solo — y son productos
    DISTINTOS. Este es el detalle que hizo fallar la primera versión."""
    if a["_brand"] and b["_brand"] and a["_brand"] != b["_brand"]:
        return 0.0

    text_score = fuzz.token_sort_ratio(a["_norm"], b["_norm"])

    size_bonus = 0
    if a["_size"] is not None and b["_size"] is not None:
        size_bonus = 15 if abs(a["_size"] - b["_size"]) < 0.5 else -30

    # "variante" = la palabra clave de la fórmula (AR, PRE, Comfort, LF...)
    # si NINGUNA de las dos la tiene, o si ambas tienen la MISMA, no penaliza.
    # Si sólo una la tiene, o tienen distinta, es una variante diferente del
    # mismo producto base (ej. "Ultimate 1" normal vs "Ultimate AR" -
    # anti-reflujo) y NO deben fusionarse aunque el texto sea 95% idéntico.
    variante_a = a["_stage_word"] or a["_stage_num"]
    variante_b = b["_stage_word"] or b["_stage_num"]
    if variante_a == variante_b:
        stage_bonus = 8 if variante_a else 0
    else:
        stage_bonus = -35

    brand_bonus = 10 if (a["_brand"] and a["_brand"] == b["_brand"]) else 0

    return max(0.0, min(100.0, text_score * 0.7 + size_bonus + stage_bonus + brand_bonus))


def _clave_par(clave_a: str, clave_b: str):
    """Orden estable: (A,B) y (B,A) tienen que caer en la MISMA fila de
    cache, sea cual sea el orden en que matchear() los compare."""
    return (clave_a, clave_b) if clave_a <= clave_b else (clave_b, clave_a)


def _buscar_revision_cacheada(cur, clave_a: str, clave_b: str):
    a, b = _clave_par(clave_a, clave_b)
    cur.execute(
        "SELECT decision, confianza FROM match_revisiones_llm "
        "WHERE producto_clave_a=%s AND producto_clave_b=%s",
        (a, b),
    )
    return cur.fetchone()


def _guardar_revision_llm(cur, clave_a, nombre_a, clave_b, nombre_b, score_fuzzy, decision, confianza, razon):
    if clave_a <= clave_b:
        a_clave, a_nombre, b_clave, b_nombre = clave_a, nombre_a, clave_b, nombre_b
    else:
        a_clave, a_nombre, b_clave, b_nombre = clave_b, nombre_b, clave_a, nombre_a
    cur.execute(
        """
        INSERT INTO match_revisiones_llm
            (producto_clave_a, producto_clave_b, nombre_a, nombre_b, score_fuzzy, decision, confianza, razon)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (producto_clave_a, producto_clave_b) DO UPDATE
        SET decision=EXCLUDED.decision, confianza=EXCLUDED.confianza,
            razon=EXCLUDED.razon, revisado_en=now()
        """,
        (a_clave, b_clave, a_nombre, b_nombre, score_fuzzy, decision, confianza, razon),
    )


def _registrar_progreso_matching(cur, conn, run_id, procesados, total, fusiones, llm_usados, llm_pospuestos,
                                  inicio, categoria_actual=None, bucket_idx=None, bucket_total=None):
    """Guarda el avance en `agent_runs.detalle` y COMMITEA -- a diferencia
    de la versión anterior de matchear() (un solo commit al final de TODO
    el proceso), esto hace que: (1) el panel/consola puedan mostrar % real
    mientras corre, y (2) si la corrida se corta a mitad de camino, todo
    lo fusionado hasta ese punto queda guardado, no se pierde."""
    transcurrido = time.time() - inicio
    ritmo = procesados / transcurrido if transcurrido > 0 else 0
    restantes = max(total - procesados, 0)
    eta_seg = int(restantes / ritmo) if ritmo > 0 else None
    detalle = {
        "candidatos_totales": total,
        "candidatos_procesados": procesados,
        "porcentaje": round(procesados / total * 100, 1) if total else 100.0,
        "fusiones": fusiones,
        "llm_usados": llm_usados,
        "llm_pospuestos": llm_pospuestos,
        "segundos_transcurridos": int(transcurrido),
        "eta_segundos": eta_seg,
        "categoria_actual": categoria_actual,
        "bucket_actual": bucket_idx,
        "buckets_totales": bucket_total,
    }
    cur.execute(
        "UPDATE agent_runs SET registros_procesados=%s, detalle=%s WHERE id=%s",
        (procesados, json.dumps(detalle), run_id),
    )
    conn.commit()
    eta_txt = f", ETA ~{eta_seg // 60}min" if eta_seg else ""
    cat_txt = f" [{categoria_actual}]" if categoria_actual else ""
    print(f"[matchear] {procesados}/{total} candidatos ({detalle['porcentaje']}%){cat_txt} -- "
          f"{fusiones} fusiones, {llm_usados} consultas LLM{eta_txt}", flush=True)
    return detalle


def matchear(min_score: int = MIN_SCORE, ambiguo_min: int = AMBIGUO_MIN, max_llm: int = MAX_LLM_POR_CORRIDA):
    """Recorre los PRODUCTOS que hoy sólo tienen listados de UNA sola
    cadena (todavía no fueron cruzados con ninguna otra), y fusiona los
    que parecen el mismo producto real, sin importar de qué cadena venga
    cada uno.

    OJO -- versión anterior de este agente: filtraba por
    `match_metodo = 'pendiente'` a nivel de LISTADO. Eso se rompía apenas
    un producto pasaba por CUALQUIER fusión (incluso una interna, de la
    misma cadena, hecha por desactivar_duplicados_por_cadena o por un
    matchear() previo): su match_metodo quedaba en 'fuzzy' para siempre,
    y ya no volvía a ser candidato -- ni siquiera para el cruce entre
    cadenas que todavía le faltaba. Caso real que lo destapó: "Nan 1
    Optipro X 400G" (Farmacorp) y "Nan 1 Optipro X 400 Gr" (Farmacias
    Chavez) cada uno ya había sido "limpiado" internamente en su propia
    cadena, marcándolos 'fuzzy', y por eso nunca se comparaban entre sí
    aunque el score entre ambos daba ~90 (bien arriba de MIN_SCORE=78).

    La corrección: el criterio para ser candidato ya no es el
    match_metodo de un listado individual, sino si el PRODUCTO como
    entidad todavía vive en una sola cadena (HAVING count(DISTINCT
    cadena_id) = 1). Eso es correcto sin importar cuántas veces ya se
    haya tocado ese producto antes -- en cuanto tiene listados de 2+
    cadenas, sale solo del pool de candidatos porque ya está cruzado.

    IMPORTANTE (encontrado en producción, 2026-09-09): dos productos de
    la MISMA cadena nunca deben compararse entre sí acá -- son dos
    artículos distintos de ese catálogo (ej. Fidalga "Leche de Frutilla"
    vs Fidalga "Leche de Vainilla"), no el mismo producto visto en dos
    cadenas. Compararlos daba miles de pares "ambiguos" falsos (ruido
    intra-cadena, no matching real) que inflaban el costo del fallback
    LLM sin sentido. Por eso el candidato ahora también trae su
    cadena_id, y el loop se salta cualquier par con la misma."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Fila de seguimiento visible desde el arranque (no recién al final) --
    # esto es lo que el panel/consola consultan para mostrar "en progreso".
    run_id = start_agent_run(cur, "matching", fuente_metodo=f"max_llm={max_llm}")
    conn.commit()

    cur.execute(
        """
        SELECT p.id AS producto_id, p.producto_clave, p.nombre, p.marca, p.categoria,
               p.subcategoria, p.creado_en,
               MIN(l.cadena_id) AS cadena_id,
               AVG(pr.precio_oferta) AS precio_oferta
        FROM productos p
        JOIN listados l ON l.producto_id = p.id AND l.activo = TRUE
        LEFT JOIN precios pr ON pr.listado_id = l.id
        GROUP BY p.id, p.producto_clave, p.nombre, p.marca, p.categoria, p.subcategoria, p.creado_en
        HAVING count(DISTINCT l.cadena_id) = 1
        ORDER BY p.creado_en ASC
        """
    )
    candidatos = cur.fetchall()
    for r in candidatos:
        r["_norm"] = normalize(r["nombre"])
        r["_brand"] = r["marca"] or extract_brand(r["_norm"])
        r["_size"] = extract_size_grams(r["_norm"])
        r["_stage_num"], r["_stage_word"] = extract_stage(r["_norm"])
        r["_precio"] = float(r["precio_oferta"]) if r["precio_oferta"] else None

    # Bucketing por categoría -- ANTES de esto, matchear() comparaba TODOS
    # los candidatos contra TODOS (O(n²) real: con 8000 candidatos ya son
    # ~32 millones de pares; con 90.000 serían ~4.000 MILLONES -- 127 veces
    # más, no 11 -- inviable). La categoría de cada producto ya viene
    # canonicalizada desde la carga (canonicalizar_categoria en
    # cargar_resultado/cargar_excel, misma función), así que agrupar por
    # ese valor es seguro: dos productos de la misma categoría real caen
    # siempre en el mismo bucket sin importar de qué cadena vinieron. Dos
    # productos que SÍ son el mismo artículo pero quedaron con categorías
    # mal canonicalizadas y distintas entre cadenas no se van a comparar
    # entre sí -- es el trade-off consciente que se pidió (menos pares
    # candidatos, muchísimo más rápido) a cambio de una fuga chica y
    # infrecuente en vez de recorrer todo el catálogo cada vez.
    buckets = {}
    for r in candidatos:
        clave_bucket = r["categoria"] or "Sin categoría"
        buckets.setdefault(clave_bucket, []).append(r)

    # Sub-bucketing por subcategoría: SOLO para categorías que quedaron
    # muy grandes (arriba de SUBBUCKET_UMBRAL) -- dentro de un bucket el
    # trabajo sigue siendo O(k²), así que una categoría como "Abarrotes"
    # con 15.000 candidatos pesa muchísimo más que diez categorías de
    # 1.500. Partirla por subcategoría (si el dato existe) reduce eso.
    # Los candidatos SIN subcategoría en una categoría grande van a su
    # propio grupo ("Sin subcategoría") -- mismo trade-off consciente que
    # con la categoría: si dos SKUs son el mismo producto pero una cadena
    # le puso subcategoría y la otra no, no se van a comparar. Por eso el
    # split solo se aplica cuando el bucket ya es grande (donde el ahorro
    # de tiempo importa más que ese riesgo chico), no en toda categoría.
    SUBBUCKET_UMBRAL = int(os.environ.get("MATCH_SUBBUCKET_UMBRAL", 1200))
    buckets_finales = {}
    for categoria_bucket, grupo in buckets.items():
        if len(grupo) <= SUBBUCKET_UMBRAL:
            buckets_finales[categoria_bucket] = grupo
            continue
        subgrupos = {}
        for r in grupo:
            sub = (r["subcategoria"] or "Sin subcategoría").strip() or "Sin subcategoría"
            subgrupos.setdefault(sub, []).append(r)
        for sub, sgrupo in subgrupos.items():
            buckets_finales[f"{categoria_bucket} / {sub}"] = sgrupo

    # buckets más chicos primero -- ven resultados (fusiones, ETA real)
    # temprano en la corrida en vez de recién al final si el primer bucket
    # que tocara fuera justo el más grande.
    orden_buckets = sorted(buckets_finales.items(), key=lambda kv: len(kv[1]))

    total = len(candidatos)
    fusiones = 0
    resueltos = set()
    llm_usados = 0
    llm_pospuestos = 0
    inicio = time.time()
    ultimo_commit = inicio
    procesados_global = 0
    categoria_en_curso = None

    try:
        for bucket_idx, (categoria_bucket, grupo) in enumerate(orden_buckets, start=1):
            categoria_en_curso = categoria_bucket
            for i, a in enumerate(grupo):
                if a["producto_id"] in resueltos:
                    procesados_global += 1
                    continue
                for b in grupo[i + 1:]:
                    if b["producto_id"] in resueltos:
                        continue
                    if a["cadena_id"] == b["cadena_id"]:
                        continue  # nunca comparar dos productos de la MISMA cadena
                    # Chequeo barato de precio ANTES del fuzzy-text (que es
                    # lo más caro de _score): si un producto cuesta varias
                    # veces más que el otro, casi seguro no es el mismo
                    # artículo (ni con oferta/promoción normal se explica
                    # una diferencia de 4x+). Generoso a propósito -- la
                    # idea es descartar rápido los pares absurdos, no
                    # reemplazar el chequeo de marca/texto que ya hace
                    # _score(). Si a alguno le falta precio, no se filtra.
                    if a["_precio"] and b["_precio"]:
                        ratio = max(a["_precio"], b["_precio"]) / min(a["_precio"], b["_precio"])
                        if ratio > PRECIO_RATIO_MAX:
                            continue
                    score = _score(a, b)

                    decision, confianza, metodo = None, None, "fuzzy"
                    if score >= min_score:
                        decision, confianza = True, round(score / 100, 3)
                    elif score >= ambiguo_min:
                        # Zona gris (Fase 2): antes esto se dejaba sin matchear
                        # para siempre. Ahora primero se busca si este PAR ya se
                        # le preguntó al LLM alguna vez (cache en Postgres); si
                        # no, y todavía queda cupo en esta corrida, se le
                        # pregunta y se guarda la respuesta para siempre -- un
                        # mismo par nunca se vuelve a pagar dos veces.
                        cacheada = _buscar_revision_cacheada(cur, a["producto_clave"], b["producto_clave"])
                        if cacheada:
                            decision = cacheada["decision"]
                            confianza = float(cacheada["confianza"]) if cacheada["confianza"] is not None else None
                            metodo = "llm"
                        elif llm_usados < max_llm:
                            try:
                                resp = matching_llm.revisar_par(a["nombre"], b["nombre"], a.get("categoria"), b.get("categoria"))
                                llm_usados += 1
                                _guardar_revision_llm(
                                    cur, a["producto_clave"], a["nombre"], b["producto_clave"], b["nombre"],
                                    round(score, 1), resp["mismo_producto"], resp["confianza"], resp["razon"],
                                )
                                decision, confianza, metodo = resp["mismo_producto"], resp["confianza"], "llm"
                            except Exception as e:
                                print(f"[warn] fallback LLM falló para '{a['nombre']}' vs '{b['nombre']}': {e}", file=sys.stderr)
                                continue  # no se pudo decidir; se reintenta en la próxima corrida, sin cachear un fallo
                        else:
                            llm_pospuestos += 1
                            continue  # tope de esta corrida alcanzado -- se retoma en la próxima

                    if not decision:
                        continue

                    # "a" es el canónico (el más antiguo): re-apunta TODOS los
                    # listados activos de "b" a producto_id de "a" (no sólo uno --
                    # "b" puede traer varios, de una limpieza interna previa).
                    cur.execute(
                        "UPDATE listados SET producto_id=%s, match_metodo=%s, "
                        "match_confidence=%s WHERE producto_id=%s AND activo=TRUE",
                        (a["producto_id"], metodo, confianza, b["producto_id"]),
                    )
                    cur.execute(
                        "UPDATE listados SET match_metodo=%s, match_confidence=1.0 "
                        "WHERE producto_id=%s AND match_metodo <> %s",
                        (metodo, a["producto_id"], metodo),
                    )
                    # Nota: como ahora se compara SOLO dentro del bucket de "a",
                    # "b" se resuelve acá dentro de la misma pasada del bucket
                    # -- no vuelve a aparecer como candidato en ningún otro
                    # bucket porque un producto pertenece a un único bucket
                    # (su propia categoría), así que no hace falta "sacarlo de
                    # la lista" a mano: el chequeo `resueltos` de arriba ya
                    # lo cubre para el resto de ESTE bucket.
                    cur.execute("SELECT count(*) AS n FROM listados WHERE producto_id=%s", (b["producto_id"],))
                    if cur.fetchone()["n"] == 0:
                        cur.execute("DELETE FROM productos WHERE id=%s", (b["producto_id"],))
                    resueltos.add(b["producto_id"])
                    fusiones += 1

                procesados_global += 1
                # Progreso: cada "a" resuelto contra el resto de SU bucket es
                # una unidad de trabajo completa -- mejor medida de avance real
                # que contar pares (la mayoría se descartan al toque, unos
                # pocos disparan LLM y tardan mucho más). Cada tanto, o si
                # pasaron 20s desde el último aviso (por si un solo candidato
                # tarda por las consultas LLM que dispara), se guarda el
                # avance y se commitea -- así un corte a mitad de camino no
                # pierde lo ya fusionado.
                ahora = time.time()
                if procesados_global % 15 == 0 or procesados_global == total or (ahora - ultimo_commit) > 20:
                    _registrar_progreso_matching(
                        cur, conn, run_id, procesados_global, total, fusiones, llm_usados, llm_pospuestos,
                        inicio, categoria_actual=categoria_en_curso, bucket_idx=bucket_idx, bucket_total=len(orden_buckets),
                    )
                    ultimo_commit = ahora

        detalle_final = _registrar_progreso_matching(
            cur, conn, run_id, total, total, fusiones, llm_usados, llm_pospuestos, inicio,
            bucket_idx=len(orden_buckets), bucket_total=len(orden_buckets),
        )
        finish_agent_run(cur, run_id, "ok", total, 0, detalle_final)
        conn.commit()
    except Exception as e:
        conn.rollback()
        try:
            finish_agent_run(cur, run_id, "error", procesados_global, 1,
                              {"error": str(e), "categoria_actual": categoria_en_curso})
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        cur.close()
        conn.close()

    print(f"matchear: {len(orden_buckets)} categorías procesadas por separado (en vez de comparar todo el catálogo entre sí)")

    print(f"matchear: {fusiones} productos fusionados entre cadenas ({llm_usados} consultas nuevas al LLM"
          f"{f', {llm_pospuestos} pares pospuestos por el tope de esta corrida' if llm_pospuestos else ''})")

    desactivados = desactivar_duplicados_por_cadena()
    print(f"matchear: {desactivados} listados duplicados (misma cadena, mismo producto) desactivados")


def desactivar_duplicados_por_cadena():
    """Salvaguarda: si dos (o más) listados ACTIVOS de la MISMA cadena
    terminan apuntando al MISMO producto_id -- sin importar la causa
    (un re-scrape con otro esquema de código, un duplicado real en el
    sitio de origen, etc.) -- eso nunca es información correcta para el
    frontend: un mismo producto no puede tener dos precios "actuales" de
    la misma cadena a la vez. Se queda el más reciente (el que se
    actualizó por última vez) y se desactivan los demás, sin borrar nada
    (quedan en la tabla por si hace falta auditar)."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT id, cadena_id, producto_id, actualizado_en,
               ROW_NUMBER() OVER (
                   PARTITION BY cadena_id, producto_id
                   ORDER BY actualizado_en DESC, id DESC
               ) AS rn
        FROM listados
        WHERE activo = TRUE
        """
    )
    filas = cur.fetchall()
    a_desactivar = [f["id"] for f in filas if f["rn"] > 1]
    if a_desactivar:
        cur.execute(
            "UPDATE listados SET activo = FALSE, actualizado_en = now() WHERE id = ANY(%s::uuid[])",
            ([str(x) for x in a_desactivar],),
        )
    conn.commit()
    cur.close()
    conn.close()
    return len(a_desactivar)


# =========================================================================
# CLI
# =========================================================================

def main():
    ap = argparse.ArgumentParser(description="ETL Price Intelligence → Postgres")
    sub = ap.add_subparsers(dest="comando", required=True)

    p1 = sub.add_parser("cargar-json")
    p1.add_argument("path")

    p2 = sub.add_parser("cargar-excel")
    p2.add_argument("path")
    p2.add_argument("--cadena", required=False, default=None,
                     help="Opcional si el Excel ya trae columna CADENA")
    p2.add_argument("--pais", default="BO")

    p3 = sub.add_parser("matchear")
    p3.add_argument("--max-llm", type=int, default=None,
                     help="Tope de consultas NUEVAS al LLM en esta corrida "
                          "(default: env MATCH_LLM_MAX_POR_CORRIDA, o 50)")

    args = ap.parse_args()
    if args.comando == "cargar-json":
        cargar_json(args.path)
    elif args.comando == "cargar-excel":
        cargar_excel(args.path, args.cadena, args.pais)
    elif args.comando == "matchear":
        matchear(max_llm=args.max_llm if args.max_llm is not None else MAX_LLM_POR_CORRIDA)


if __name__ == "__main__":
    main()

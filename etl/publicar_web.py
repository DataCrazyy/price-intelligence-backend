"""
publicar_web.py — Publica en Supabase lo que muestra la página web.

Reemplaza a export_json.py PARA LA WEB: en vez de escribir un precios.json de
~37 MB que el navegador baja entero, llena la tabla `web_productos` (una fila
por producto canónico, ya calculada) y `web_resumen`. La página le pide a
Supabase solo lo que muestra (24 productos por pantalla).

Requisito (una sola vez): correr db/migracion_web.sql en Supabase.

Uso (desde postgres_migration/etl, con la venv activada):
    python publicar_web.py              # publica
    python publicar_web.py --dry-run    # calcula y muestra totales, no escribe nada

Se corre al final de cada actualización, después de run_all.py y matchear.
La categoría de la web sale de docs/categorias.js (la misma lista que usa la
página), así no hay dos listas de categorías que mantener.
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
CATEGORIAS_JS = Path(__file__).resolve().parent.parent / "docs" / "categorias.js"


# ------------------------------------------------------------------ categorías
def _normalizar(texto):
    """Igual que normalizar() de categorias.js: sin acentos, MAYÚSCULAS, espacios simples."""
    t = unicodedata.normalize("NFD", str(texto or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t.upper()).strip()


def cargar_categorias(ruta=CATEGORIAS_JS):
    """Lee MAPA_EXACTO, PALABRAS_CLAVE y CATEGORIA_OTROS directo de categorias.js."""
    js = ruta.read_text(encoding="utf-8")
    bloque_mapa = re.search(r"var MAPA_EXACTO\s*=\s*\{(.*?)\n\s*\};", js, re.S).group(1)
    mapa = dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", bloque_mapa))
    bloque_pal = re.search(r"var PALABRAS_CLAVE\s*=\s*\[(.*?)\n\s*\];", js, re.S).group(1)
    palabras = [(re.compile(rx), cat) for rx, cat in re.findall(r"\[/(.+?)/,\s*'([^']+)'\]", bloque_pal)]
    otros = re.search(r"var CATEGORIA_OTROS\s*=\s*'([^']+)'", js).group(1)
    if not mapa or not palabras:
        sys.exit(f"No pude leer las categorías de {ruta}")
    return mapa, palabras, otros


def crear_macro(mapa, palabras, otros):
    def macro(etiqueta):
        if not etiqueta:
            return otros
        clave = _normalizar(etiqueta)
        if clave in mapa:
            return mapa[clave]
        for rx, cat in palabras:
            if rx.search(clave):
                return cat
        return otros
    return macro


def norm_busqueda(texto):
    """Igual que norm() de la página: minúsculas, sin acentos, espacios simples."""
    t = unicodedata.normalize("NFD", str(texto or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t.lower()).strip()


# ------------------------------------------------------------------ cálculo
def leer(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT l.id AS listado_id, p.producto_clave AS k, p.nombre, p.categoria, p.subcategoria,
               c.nombre AS cadena, l.imagen, l.url,
               pr.precio_oferta::float AS precio, pr.precio_regular::float AS regular
        FROM listados l
        JOIN productos p ON p.id = l.producto_id
        JOIN cadenas c   ON c.id = l.cadena_id
        JOIN precios pr  ON pr.listado_id = l.id
        WHERE l.activo = TRUE AND pr.precio_oferta > 0
    """)
    filas = cur.fetchall()
    cur.execute("SELECT listado_id, fecha, precio_oferta::float AS p FROM historial_precios ORDER BY fecha")
    hist = defaultdict(list)
    for h in cur.fetchall():
        hist[h["listado_id"]].append((h["fecha"].isoformat(), h["p"]))
    cur.execute("SELECT max(capturado_en)::date AS f FROM precios")
    fecha = cur.fetchone()["f"]
    cur.execute("SELECT coalesce(array_agg(nombre ORDER BY nombre), '{}') AS c FROM cadenas WHERE activo")
    cadenas = list(cur.fetchone()["c"])
    return filas, hist, (fecha.isoformat() if fecha else None), cadenas


def calcular(filas, hist, fecha, macro_de, otros):
    grupos = defaultdict(list)
    for f in filas:
        grupos[f["k"]].append(f)

    filas_web, cat_count = [], defaultdict(int)
    for k, lst in grupos.items():
        # una fila por cadena (la más barata si la cadena lo lista dos veces)
        por_cadena = {}
        for x in lst:
            y = por_cadena.get(x["cadena"])
            if y is None or x["precio"] < y["precio"]:
                por_cadena[x["cadena"]] = x
        lst = sorted(por_cadena.values(), key=lambda x: x["precio"])
        best, worst = lst[0], lst[-1]

        macro = otros
        for x in lst:
            m = macro_de(x["subcategoria"])
            if m != otros:
                macro = m
                break
        else:
            for x in lst:
                m = macro_de(x["categoria"])
                if m != otros:
                    macro = m
                    break

        def off(x):
            return (1 - x["precio"] / x["regular"]) * 100 if x["regular"] and x["regular"] > x["precio"] else 0.0

        best_off = off(best)
        ahorro = round(worst["precio"] - best["precio"], 2)
        n = len(lst)
        nombre = (best["nombre"] or "").strip()
        img = next((x["imagen"] for x in lst if x["imagen"]), None)

        historial = []
        for x in lst:
            vistas = set()
            for f_, p_ in hist.get(x["listado_id"], []):
                if f_ not in vistas and p_ > 0:
                    historial.append({"f": f_, "c": x["cadena"], "p": round(p_, 2)})
                    vistas.add(f_)
            if fecha and fecha not in vistas:
                historial.append({"f": fecha, "c": x["cadena"], "p": round(x["precio"], 2)})
        historial.sort(key=lambda h: (h["f"], h["c"]))

        filas_web.append({
            "k": k,
            "nombre": nombre,
            "macro": macro,
            "img": img,
            "n": n,
            "precio": round(best["precio"], 2),
            "precio_regular": round(max(best["regular"] or 0, best["precio"]), 2),
            "cadena": best["cadena"],
            "url": best["url"],
            "ahorro": ahorro,
            "cadena_cara": worst["cadena"],
            "best_off": round(best_off, 2),
            "puntaje_oferta": round(min(best_off, 40) + (15 if n >= 2 and ahorro >= 0.5 else 0), 2),
            "cadenas": [x["cadena"] for x in lst],
            "st": norm_busqueda(f"{nombre} {' '.join(x['cadena'] for x in lst)} {best['subcategoria'] or ''} {macro}"),
            "listados": [{"c": x["cadena"], "p": round(x["precio"], 2), "r": round(max(x["regular"] or 0, x["precio"]), 2), "u": x["url"]} for x in lst],
            "historial": historial,
        })
        cat_count[macro] += 1
    return filas_web, dict(cat_count)


# ------------------------------------------------------------------ escritura
COLUMNAS = ["k", "nombre", "macro", "img", "n", "precio", "precio_regular", "cadena", "url", "ahorro",
            "cadena_cara", "best_off", "puntaje_oferta", "cadenas", "st", "listados", "historial"]


def escribir(conn, filas_web, resumen):
    """Todo en UNA transacción: quien esté mirando la página ve los datos
    viejos hasta el commit, y después los nuevos -- nunca una mezcla."""
    cur = conn.cursor()
    cur.execute("DELETE FROM web_productos")
    valores = [tuple(psycopg2.extras.Json(f[c]) if c in ("listados", "historial") else f[c] for c in COLUMNAS) for f in filas_web]
    psycopg2.extras.execute_values(
        cur, f"INSERT INTO web_productos ({', '.join(COLUMNAS)}) VALUES %s", valores, page_size=1000)
    cur.execute("""
        INSERT INTO web_resumen (id, datos, actualizado_en) VALUES (1, %s, now())
        ON CONFLICT (id) DO UPDATE SET datos = EXCLUDED.datos, actualizado_en = now()
    """, (psycopg2.extras.Json(resumen),))
    conn.commit()


def publicar(dry_run=False):
    """Calcula y (salvo dry_run) escribe web_productos / web_resumen. Devuelve el resumen."""
    if not DATABASE_URL:
        raise RuntimeError("Falta DATABASE_URL en el .env")
    t0 = time.time()
    mapa, palabras, otros = cargar_categorias()
    macro_de = crear_macro(mapa, palabras, otros)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        filas, hist, fecha, cadenas = leer(conn)
        filas_web, cat_count = calcular(filas, hist, fecha, macro_de, otros)
        resumen = {
            "fecha": fecha,
            "cadenas": cadenas,
            "total": len(filas_web),
            "ofertas": sum(1 for f in filas_web if f["best_off"] >= 1),
            "cats": cat_count,
        }
        print(f"[publicar_web] {len(filas)} listados -> {len(filas_web)} productos · "
              f"{sum(1 for f in filas_web if f['n'] >= 2)} en 2+ cadenas · {resumen['ofertas']} con descuento · datos del {fecha}")
        for c, n in sorted(cat_count.items(), key=lambda x: -x[1]):
            print(f"    {n:>6}  {c}")
        if dry_run:
            print("[publicar_web] --dry-run: no se escribió nada.")
            return resumen
        escribir(conn, filas_web, resumen)
        print(f"[publicar_web] listo en {time.time() - t0:.1f}s")
        return resumen
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Publica web_productos / web_resumen en Supabase")
    ap.add_argument("--dry-run", action="store_true", help="calcula y muestra totales sin escribir")
    args = ap.parse_args()
    try:
        publicar(dry_run=args.dry_run)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()

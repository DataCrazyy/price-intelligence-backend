"""
Clasifica cada producto en las categorías de la página (etl/taxonomia.py)
usando la IA de OpenAI y lo guarda en la tabla producto_categoria.

Es incremental: solo clasifica lo que falta (productos nuevos, nombres que
cambiaron o categorías que ya no existen en la taxonomía). Lo corregido a mano
(fuente = 'manual') no se toca nunca.

Uso (desde postgres_migration/etl, con la venv activada):
    python clasificar.py --dry-run              # cuántos faltan y costo estimado, sin llamar a la IA
    python clasificar.py --limite 300 --muestra # prueba con 300 y deja muestra_categorias.csv para revisar
    python clasificar.py --limite 300 --muestra --rehacer   # repetir la prueba con los mismos 300 tras cambiar reglas
    python clasificar.py                        # clasifica todo lo que falta
    python clasificar.py --rehacer-cat "Farmacia"   # vuelve a clasificar una categoría (p. ej. tras cambiar sus subcategorías)

Necesita en el .env: DATABASE_URL y OPENAI_API_KEY (opcional OPENAI_MODEL,
por defecto gpt-4o-mini).
"""
import argparse
import csv
import json
import os
import sys
import re
import threading
import unicodedata
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from taxonomia import TAXONOMIA, ETIQUETAS, PARES, OTROS, REGLAS, valida

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
MODELO = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
POR_LLAMADA = 80          # productos por consulta a la IA
HILOS = 4                 # consultas en paralelo (sube a 6-8 si tu cuenta de OpenAI lo permite)
# Precio de gpt-4o-mini por millón de tokens (entrada / salida), solo para el estimado
PRECIO_ENTRADA, PRECIO_SALIDA = 0.15, 0.60

SISTEMA = """Clasificás productos de supermercados y farmacias de Bolivia en categorías para un comparador de precios.
Para cada producto elegí UNA etiqueta de la lista, exactamente como está escrita: donde lo buscaría una persona en el súper.
La pista de la cadena es solo orientativa: muchas veces es una promoción ("Lo Nuevo", "Ofertas", "Zona Papa") y no sirve.

Reglas:
""" + "\n".join(f"- {r}" for r in REGLAS) + """

Etiquetas permitidas (con aclaraciones):
""" + "\n".join(
    f"- {c} > {s}" + (f"  ({pista})" if pista else "")
    for c, subs in TAXONOMIA.items() for s, pista in subs.items()
)

ESQUEMA = {
    "type": "object",
    "properties": {
        "r": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer", "description": "número del producto"},
                    "p": {"type": "string", "description": "las 2 primeras palabras del nombre del producto, copiadas"},
                    "e": {"type": "string", "enum": ETIQUETAS},
                },
                "required": ["i", "p", "e"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["r"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ base de datos
def conectar():
    return psycopg2.connect(DATABASE_URL, connect_timeout=20, keepalives=1,
                            keepalives_idle=30, keepalives_interval=10, keepalives_count=5)


def pendientes(conn, rehacer_cat=None, rehacer=False):
    """Productos con un listado activo que todavía no tienen una categoría válida."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT ON (p.producto_clave)
               p.producto_clave AS k, p.nombre, p.marca,
               concat_ws(' / ', nullif(p.categoria, ''), nullif(p.subcategoria, '')) AS pista,
               pc.cat, pc.sub, pc.fuente, pc.nombre AS nombre_clasificado
        FROM productos p
        JOIN listados l ON l.producto_id = p.id AND l.activo
        LEFT JOIN producto_categoria pc ON pc.k = p.producto_clave
        ORDER BY p.producto_clave
    """)
    falta = []
    for r in cur.fetchall():
        if r["fuente"] == "manual":
            continue
        if (r["cat"] is None or not valida(r["cat"], r["sub"]) or r["nombre_clasificado"] != r["nombre"]
                or rehacer or (rehacer_cat and r["cat"] == rehacer_cat)):
            falta.append(r)
    return falta


def guardar(conn, filas):
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        INSERT INTO producto_categoria (k, nombre, cat, sub, fuente, modelo, actualizado_en)
        VALUES %s
        ON CONFLICT (k) DO UPDATE SET nombre = EXCLUDED.nombre, cat = EXCLUDED.cat, sub = EXCLUDED.sub,
                                      modelo = EXCLUDED.modelo, actualizado_en = now()
        WHERE producto_categoria.fuente <> 'manual'
    """, [(f["k"], f["nombre"], f["cat"], f["sub"], "ia", f["modelo"]) for f in filas],
        template="(%s, %s, %s, %s, %s, %s, now())")
    conn.commit()


# ------------------------------------------------------------------ IA
def _palabras(t):
    t = unicodedata.normalize("NFD", str(t or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9 ]", " ", t).split()


_cliente = None
def cliente():
    global _cliente
    if _cliente is None:
        from openai import OpenAI
        if not os.environ.get("OPENAI_API_KEY"):
            sys.exit("Falta OPENAI_API_KEY en el .env (la misma clave de ChatGPT que usa matching_llm.py).")
        _cliente = OpenAI(max_retries=3, timeout=90)
    return _cliente


def clasificar_lote(lote):
    """Devuelve ([{k, nombre, cat, sub, modelo}], tokens_entrada, tokens_salida)."""
    lineas = []
    for i, p in enumerate(lote):
        extra = f" [marca: {p['marca']}]" if p.get("marca") else ""
        pista = f" [cadena: {p['pista']}]" if p.get("pista") else ""
        lineas.append(f"{i}. {p['nombre']}{extra}{pista}")
    r = cliente().chat.completions.create(
        model=MODELO,
        temperature=0,
        messages=[{"role": "system", "content": SISTEMA},
                  {"role": "user", "content": "Productos:\n" + "\n".join(lineas)}],
        response_format={"type": "json_schema", "json_schema": {"name": "clasificacion", "strict": True, "schema": ESQUEMA}},
    )
    datos = json.loads(r.choices[0].message.content)
    # Solo se acepta la respuesta si el eco del nombre coincide con el producto de ese número
    # (si la IA "se corre" de fila, se descarta y queda pendiente para la próxima corrida)
    def coincide(i, eco):
        a, b = _palabras(lote[i]["nombre"]), _palabras(eco)
        return bool(b) and bool(a) and b[0] == a[0]
    elegidas = {x["i"]: x["e"] for x in datos.get("r", []) if 0 <= x["i"] < len(lote) and coincide(x["i"], x.get("p", ""))}
    salida = []
    for i, p in enumerate(lote):
        e = elegidas.get(i)
        if not e or " > " not in e:
            continue                                  # sin respuesta: queda pendiente para la próxima corrida
        cat, sub = e.split(" > ", 1)
        if valida(cat, sub):
            salida.append({"k": p["k"], "nombre": p["nombre"], "cat": cat, "sub": sub, "modelo": r.model})
    u = r.usage
    return salida, (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)


# ------------------------------------------------------------------ principal
def clasificar(limite=None, muestra=False, dry_run=False, rehacer_cat=None, hilos=HILOS, rehacer=False):
    if not DATABASE_URL:
        sys.exit("Falta DATABASE_URL en el .env")
    t0 = time.time()
    conn = conectar()
    try:
        falta = pendientes(conn, rehacer_cat, rehacer)
        if limite:
            falta = falta[:limite]
        lotes = [falta[i:i + POR_LLAMADA] for i in range(0, len(falta), POR_LLAMADA)]
        # Estimado: el prompt de sistema y la lista (~3.300 tokens) va en cada consulta + ~25 tokens por producto de ida y ~14 de vuelta
        est_in, est_out = len(lotes) * 3300 + len(falta) * 25, len(falta) * 22
        costo = (est_in * PRECIO_ENTRADA + est_out * PRECIO_SALIDA) / 1e6
        print(f"[clasificar] {len(falta)} productos por clasificar en {len(lotes)} consultas · costo estimado US$ {costo:.2f} ({MODELO})")
        if dry_run or not falta:
            return {"clasificados": 0, "pendientes": len(falta)}

        hechos, t_in, t_out, errores, listas = [], 0, 0, 0, 0
        ex = ThreadPoolExecutor(max_workers=hilos)
        try:
            futuros = {ex.submit(clasificar_lote, lote): n for n, lote in enumerate(lotes)}
            for fut in as_completed(futuros):
                listas += 1
                try:
                    filas, a, b = fut.result()
                except Exception as e:
                    errores += 1
                    print(f"  consulta {futuros[fut] + 1} falló: {str(e)[:120]}")
                    continue
                t_in += a; t_out += b
                if filas:
                    # Se guarda de a poco: si algo se corta, la próxima corrida sigue donde quedó.
                    # Si Supabase cortó la conexión, se reconecta y se reintenta una vez.
                    try:
                        guardar(conn, filas)
                    except psycopg2.OperationalError:
                        print("  se cortó la conexión con la base; reconectando…")
                        try: conn.close()
                        except Exception: pass
                        conn = conectar()
                        guardar(conn, filas)
                    hechos.extend(filas)
                if listas % 10 == 0 or listas == len(lotes):
                    costo = (t_in * PRECIO_ENTRADA + t_out * PRECIO_SALIDA) / 1e6
                    print(f"  {listas}/{len(lotes)} consultas · {len(hechos)}/{len(falta)} clasificados · ~US$ {costo:.3f}", flush=True)
        except BaseException:
            # Error o Ctrl+C: se cancelan las consultas que faltan para no seguir gastando
            ex.shutdown(wait=False, cancel_futures=True)
            print(f"\n[clasificar] detenido: quedaron guardados {len(hechos)}. Volvé a correrlo y sigue desde ahí.")
            raise
        ex.shutdown(wait=True)
        costo = (t_in * PRECIO_ENTRADA + t_out * PRECIO_SALIDA) / 1e6
        print(f"\n[clasificar] listo: {len(hechos)} clasificados · {errores} consultas con error · "
              f"{t_in + t_out:,} tokens · ~US$ {costo:.3f} · {time.time() - t0:.0f}s")

        if muestra and hechos:
            nombres = {p["k"]: p for p in falta}
            ruta = Path(__file__).with_name("muestra_categorias.csv")
            with open(ruta, "w", newline="", encoding="utf-8-sig") as f:      # utf-8-sig: Excel lo abre con tildes
                w = csv.writer(f, delimiter=";")
                w.writerow(["categoria", "subcategoria", "producto", "pista de la cadena", "producto_clave"])
                for h in sorted(hechos, key=lambda x: (list(TAXONOMIA).index(x["cat"]), x["sub"], x["nombre"])):
                    w.writerow([h["cat"], h["sub"], h["nombre"], nombres[h["k"]]["pista"], h["k"]])
            print(f"[clasificar] muestra para revisar: {ruta}")
        return {"clasificados": len(hechos), "pendientes": len(falta) - len(hechos)}
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="solo cuenta y estima el costo")
    ap.add_argument("--limite", type=int, help="clasificar como máximo N productos (para probar)")
    ap.add_argument("--muestra", action="store_true", help="dejar muestra_categorias.csv con lo clasificado")
    ap.add_argument("--rehacer-cat", help="volver a clasificar los productos de esta categoría")
    ap.add_argument("--rehacer", action="store_true", help="volver a clasificar también lo que ya clasificó la IA (lo manual no se toca)")
    ap.add_argument("--hilos", type=int, default=HILOS)
    a = ap.parse_args()
    if a.rehacer_cat and a.rehacer_cat not in TAXONOMIA:
        sys.exit(f"No existe la categoría {a.rehacer_cat!r}. Opciones: {', '.join(TAXONOMIA)}")
    clasificar(limite=a.limite, muestra=a.muestra, dry_run=a.dry_run, rehacer_cat=a.rehacer_cat, hilos=a.hilos, rehacer=a.rehacer)


if __name__ == "__main__":
    main()

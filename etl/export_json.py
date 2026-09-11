"""
export_json.py — Cierra el ciclo: Postgres -> data/precios.json

Genera un archivo IDÉNTICO en forma al precios.json que ya consume tu
index.html, pero ahora sacado de la base normalizada en vez de escrito a
mano. Corre esto después de cada carga (cargar-json / cargar-excel /
matchear) y sobrescribe data/precios.json en tu repo.

Uso:
    python export_json.py ../../data/precios.json
"""
import json
import os
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://price_intel:change_me_local_only@localhost:5432/price_intel",
)


def export_json(out_path: str):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            l.id AS listado_id,
            l.cadena_id::text || '-' || l.codigo_cadena AS id,
            p.producto_clave, l.codigo_cadena,
            p.nombre, p.categoria, p.subcategoria,
            c.nombre AS cadena, s.nombre AS sucursal,
            COALESCE(s.ciudad, 'Santa Cruz') AS ciudad,
            pr.precio_oferta, pr.precio_regular,
            pr.descuento_bs, pr.descuento_pct, pr.tiene_descuento,
            l.imagen, l.url
        FROM listados l
        JOIN productos p ON p.id = l.producto_id
        JOIN cadenas c   ON c.id = l.cadena_id
        LEFT JOIN sucursales s ON s.id = l.sucursal_id
        JOIN precios pr  ON pr.listado_id = l.id
        WHERE l.activo = TRUE
        ORDER BY p.nombre
    """)
    filas = cur.fetchall()

    cur.execute("""
        SELECT h.listado_id, h.fecha, h.precio_oferta
        FROM historial_precios h
        ORDER BY h.fecha
    """)
    historial_por_listado = defaultdict(list)
    for h in cur.fetchall():
        historial_por_listado[h["listado_id"]].append(
            {"fecha": h["fecha"].isoformat(), "precio": float(h["precio_oferta"])}
        )

    cur.execute("SELECT array_agg(DISTINCT nombre) AS fuentes FROM cadenas WHERE activo")
    fuentes = cur.fetchone()["fuentes"] or []

    productos = []
    for f in filas:
        cadena = f["cadena"]
        hist = [{**h, "cadena": cadena} for h in historial_por_listado.get(f["listado_id"], [])]
        productos.append({
            "id": f["id"],
            "producto_clave": f["producto_clave"],
            "codigo_cadena": f["codigo_cadena"],
            "nombre": f["nombre"],
            "categoria": f["categoria"],
            "subcategoria": f["subcategoria"],
            "cadena": cadena,
            "sucursal": f["sucursal"],
            "ciudad": f["ciudad"],
            "precio_oferta": float(f["precio_oferta"]),
            "precio_regular": float(f["precio_regular"]),
            "descuento_bs": float(f["descuento_bs"]),
            "descuento_pct": float(f["descuento_pct"]),
            "tiene_descuento": f["tiene_descuento"],
            "imagen": f["imagen"],
            "url": f["url"],
            "historial": hist,
        })

    payload = {
        "fecha_actualizacion": __import__("datetime").date.today().isoformat(),
        "fuentes": fuentes,
        "total_registros": len(productos),
        "total_productos_unicos": len({p["producto_clave"] for p in productos}),
        "reglas": {
            "oferta_actual": "precio_oferta < precio_regular",
            "minimo_maximo": "calculados por el HTML sobre historial",
            "matching": "cantidad equivalente y similitud alta de nombre; no coincidentes conservan clave separada",
        },
        "productos": productos,
    }

    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)

    cur.close()
    conn.close()
    print(f"export_json: {len(productos)} listados exportados -> {out_path}")
    return len(productos)


if __name__ == "__main__":
    export_json(sys.argv[1] if len(sys.argv) > 1 else "precios_export.json")

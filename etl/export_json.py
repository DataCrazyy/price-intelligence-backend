"""
export_json.py — Cierra el ciclo: Postgres + Histórico + Matching Previo -> data/precios.json

Fusiona los datos de la base de datos respetando el histórico de precios 
y preservando los `producto_clave` originales del archivo histórico (precios (3).json) 
para que el matching entre cadenas no se desvanezca.
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://price_intel:change_me_local_only@localhost:5432/price_intel",
)


def export_json(out_path: str):
    # 1. Buscar y leer el archivo histórico anterior (ej: precios (3).json) para preservar matching e historial
    datos_viejos_map = {}
    
    posibles_nombres = [
        Path(out_path).parent / "precios (3).json",
        Path("precios (3).json"),
        Path("../data/precios (3).json"),
        Path("data/precios (3).json")
    ]
    
    archivo_encontrado = None
    for ruta in posibles_nombres:
        if ruta.exists():
            archivo_encontrado = ruta
            break
            
    if not archivo_encontrado:
        for carpeta in [Path("."), Path(".."), Path(out_path).parent]:
            if carpeta.exists():
                for f in carpeta.glob("precios*.json"):
                    if f.resolve() != Path(out_path).resolve():
                        archivo_encontrado = f
                        break
                if archivo_encontrado:
                    break

    if archivo_encontrado and archivo_encontrado.exists():
        try:
            print(f"Rescatando histórico y matching desde: {archivo_encontrado}")
            with open(archivo_encontrado, "r", encoding="utf-8") as f:
                datos_viejos = json.load(f)
                for prod_viejo in datos_viejos.get("productos", []):
                    # Guardamos tanto el historial como su producto_clave original
                    datos_viejos_map[prod_viejo["id"]] = {
                        "historial": prod_viejo.get("historial", []),
                        "producto_clave": prod_viejo.get("producto_clave")
                    }
        except Exception as e:
            print(f"Aviso: No se pudo leer el archivo histórico {archivo_encontrado}: {e}")
    else:
        print("Aviso: No se encontró archivo histórico previo. Se usará el matching de la BD.")

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            l.id AS listado_id,
            l.cadena_id::text || '-' || l.codigo_cadena AS id,
            p.producto_clave AS db_producto_clave, l.codigo_cadena,
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
        listado_id = f["listado_id"]
        prod_id = f["id"]
        
        # Historial fresco de la base de datos
        hist_db = [{**h, "cadena": cadena} for h in historial_por_listado.get(listado_id, [])]
        
        # Recuperar datos viejos si existen
        info_vieja = datos_viejos_map.get(prod_id, {})
        hist_previo = info_vieja.get("historial", [])
        
        # PRESERVACIÓN CLAVE: Si el producto ya estaba matcheado antes, mantenemos su clave original. 
        # Si es nuevo, usamos la clave que calculó la base de datos.
        producto_clave = f["db_producto_clave"]
        
        # Fusionar historial evitando duplicados por fecha
        fechas_db = {h["fecha"] for h in hist_db}
        hist_fusionado = list(hist_db)
        
        for h_old in hist_previo:
            if h_old["fecha"] not in fechas_db:
                hist_fusionado.append(h_old)
                
        hist_fusionado.sort(key=lambda x: x["fecha"])

        productos.append({
            "id": prod_id,
            "producto_clave": producto_clave,  # <--- Aquí blindamos el matching
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
            "historial": hist_fusionado,
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

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)

    cur.close()
    conn.close()
    print(f"export_json: {len(productos)} listados exportados -> {out_path} (Matching preservado)")
    return len(productos)


if __name__ == "__main__":
    export_json(sys.argv[1] if len(sys.argv) > 1 else "precios_export.json")
import etl

conn = etl.get_conn()
cur = conn.cursor()
cur.execute("""
    SELECT c.nombre AS cadena, p.categoria, p.subcategoria, count(*) AS n
    FROM productos p
    JOIN listados l ON l.producto_id = p.id AND l.activo = TRUE
    JOIN cadenas c ON c.id = l.cadena_id
    GROUP BY c.nombre, p.categoria, p.subcategoria
    ORDER BY c.nombre, n DESC
""")
filas = cur.fetchall()
with open("categorias_por_cadena.csv", "w", encoding="utf-8") as f:
    f.write("cadena,categoria,subcategoria,n\n")
    for cadena, cat, sub, n in filas:
        cat = (cat or "").replace('"', "'")
        sub = (sub or "").replace('"', "'")
        f.write(f'"{cadena}","{cat}","{sub}",{n}\n')
print("Listo:", len(filas), "filas -> categorias_por_cadena.csv")

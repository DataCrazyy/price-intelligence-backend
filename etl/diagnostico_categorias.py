# Diagnostico: top categorias SIN match canonico, por cantidad de productos.
# Solo lee Postgres, no escribe nada. Correr desde postgres_migration/etl:
#   python diagnostico_categorias.py
import sys
sys.path.insert(0, ".")
from etl import get_conn, categoria_madre_canonica
import psycopg2.extras
from collections import Counter

conn = get_conn()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT categoria, subcategoria, count(*) as n FROM productos GROUP BY categoria, subcategoria ORDER BY n DESC")
filas = cur.fetchall()
cur.close()
conn.close()

sin_match = Counter()
total_productos = 0
total_sin_match = 0
for f in filas:
    total_productos += f["n"]
    nueva = categoria_madre_canonica(f["categoria"], f["subcategoria"])
    if not nueva:
        sin_match[(f["categoria"], f["subcategoria"])] += f["n"]
        total_sin_match += f["n"]

print(f"Total productos: {total_productos}")
print(f"Sin match canonico: {total_sin_match} productos ({100*total_sin_match/total_productos:.1f}%)")
print()
print("Top 60 (categoria, subcategoria) SIN match, por cantidad de productos:")
for (cat, sub), n in sin_match.most_common(60):
    print(f"  {n:6d}  categoria={cat!r:35s} subcategoria={sub!r}")

# Diagnostico v2: separa "sin match" (None) de "pasa sin consolidar"
# (la funcion devuelve la categoria tal cual, sin agruparla en ninguno
# de los ~19 buckets canonicos). Solo lee Postgres, no escribe nada.
import sys
sys.path.insert(0, ".")
from etl import get_conn, categoria_madre_canonica, CATEGORIA_MADRE_EXACTA
import psycopg2.extras
from collections import Counter

BUCKETS_CANONICOS = set(CATEGORIA_MADRE_EXACTA.values())

conn = get_conn()
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SELECT categoria, subcategoria, count(*) as n FROM productos GROUP BY categoria, subcategoria ORDER BY n DESC")
filas = cur.fetchall()
cur.close()
conn.close()

sin_match = Counter()
no_consolida = Counter()
total = 0
n_sin_match = 0
n_no_consolida = 0
n_ok = 0

for f in filas:
    total += f["n"]
    nueva = categoria_madre_canonica(f["categoria"], f["subcategoria"])
    if not nueva:
        sin_match[(f["categoria"], f["subcategoria"])] += f["n"]
        n_sin_match += f["n"]
    elif nueva not in BUCKETS_CANONICOS:
        # devolvio algo, pero no es ninguno de los ~19 buckets -> paso sin consolidar
        no_consolida[(f["categoria"], f["subcategoria"], nueva)] += f["n"]
        n_no_consolida += f["n"]
    else:
        n_ok += f["n"]

print(f"Total productos: {total}")
print(f"OK (cayeron en uno de los buckets canonicos): {n_ok} ({100*n_ok/total:.1f}%)")
print(f"Sin match (None): {n_sin_match} ({100*n_sin_match/total:.1f}%)")
print(f"Pasan SIN consolidar (bucket propio, no compartido): {n_no_consolida} ({100*n_no_consolida/total:.1f}%)")
print()
print("Top 50 que PASAN SIN CONSOLIDAR, por cantidad de productos:")
for (cat, sub, nueva), n in no_consolida.most_common(50):
    print(f"  {n:6d}  categoria={cat!r:30s} subcategoria={sub!r:35s} -> {nueva!r}")

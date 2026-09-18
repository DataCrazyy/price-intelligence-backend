"""
verificar_conexion.py — Chequeo rápido para confirmar que dos máquinas
distintas (David / Diego) están mirando exactamente la misma base de
datos compartida (Supabase) y el mismo commit de código.

Uso:
    python verificar_conexion.py

Corré esto en tu máquina y compará la salida con la de la otra persona --
"Conteo por tabla" y "Ultimo commit" deberían coincidir exacto.
"""
import os
import subprocess

import psycopg2
from dotenv import load_dotenv

load_dotenv()
url = os.environ.get("DATABASE_URL")
if not url:
    print("ERROR: no se encontro DATABASE_URL en .env")
    raise SystemExit(1)

print("=" * 60)
print("1. Conexion a la base compartida")
print("=" * 60)
conn = psycopg2.connect(url, connect_timeout=10)
cur = conn.cursor()
cur.execute("select current_database(), inet_server_addr();")
print("Conectado OK a:", cur.fetchone())

print()
print("=" * 60)
print("2. Conteo de filas por tabla (deberia ser IGUAL para los dos)")
print("=" * 60)
cur.execute("""
    select table_name
    from information_schema.tables
    where table_schema = 'public'
    order by table_name;
""")
tablas = [r[0] for r in cur.fetchall()]
for t in tablas:
    cur.execute(f'select count(*) from "{t}";')
    n = cur.fetchone()[0]
    print(f"  {t:25s} {n:>8} filas")

conn.close()

print()
print("=" * 60)
print("3. Ultimo commit de este repo (deberia ser IGUAL para los dos)")
print("=" * 60)
try:
    commit = subprocess.check_output(
        ["git", "log", "--oneline", "-1"], text=True
    ).strip()
    print(" ", commit)
except Exception as e:
    print("  (no se pudo leer git:", e, ")")

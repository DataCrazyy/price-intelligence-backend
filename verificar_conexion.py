import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
url = os.environ.get("DATABASE_URL")
if not url:
    print("ERROR: no se encontro DATABASE_URL en .env")
    raise SystemExit(1)

conn = psycopg2.connect(url, connect_timeout=10)
cur = conn.cursor()
cur.execute("select table_name from information_schema.tables where table_schema='public' order by table_name;")
tablas = [r[0] for r in cur.fetchall()]
print(f"Conexion OK. {len(tablas)} tablas encontradas:")
for t in tablas:
    print(" -", t)
conn.close()

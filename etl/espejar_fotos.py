"""
Copia propia de las fotos lentas (Hipermaxi y Farmacias Chávez) en Supabase Storage.

Para cada producto activo:
  - si alguna cadena tiene la foto en Shopify (Amarket, Fidalga, Farmacorp), se usa
    esa y no se copia nada (ya carga rápido);
  - si no, se baja la foto de Hipermaxi/Chávez UNA vez, se hacen dos versiones WebP
    (160 px para listas y 480 px para la ficha) y se suben al bucket público `fotos`.

Es incremental: solo procesa fotos nuevas. Si una falla, se reintenta en las próximas
corridas (hasta 3 veces) y mientras tanto la página usa la original.

Uso (desde postgres_migration/etl, con la venv activada):
    python espejar_fotos.py --dry-run        # cuántas faltan, sin bajar nada
    python espejar_fotos.py --limite 50      # prueba con 50
    python espejar_fotos.py                  # todas las que faltan

Necesita en el .env: DATABASE_URL y SUPABASE_SERVICE_ROLE_KEY
(opcional SUPABASE_URL). Y: pip install pillow requests
"""
import argparse
import hashlib
import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from publicar_web import origen_img

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://dxdmstnkgqpndbbqkwct.supabase.co").rstrip("/")
SERVICIO = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
BUCKET = "fotos"
TAMANOS = {"s": 160, "m": 480}          # sufijo -> ancho/alto máximo
HILOS = 8
MAX_INTENTOS = 3
NAVEGADOR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/128.0 Safari/537.36", "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}


def es_rapida(url):
    """Fotos que ya cargan rápido (CDN de Shopify): no hace falta copiarlas."""
    return "cdn.shopify.com" in (url or "")


def rol_de_la_clave(clave):
    """'service_role', 'anon', 'secret' (claves nuevas sb_secret_…), 'publishable' u '?'."""
    import base64, json as _json
    clave = (clave or "").strip()
    if clave.startswith("sb_secret_"):
        return "secret"
    if clave.startswith("sb_publishable_"):
        return "publishable"
    try:
        datos = clave.split(".")[1]
        datos += "=" * (-len(datos) % 4)
        return _json.loads(base64.urlsafe_b64decode(datos)).get("role", "?")
    except Exception:
        return "?"


def cabeceras_storage():
    # Las claves nuevas (sb_secret_…) no son JWT: van solo en "apikey".
    # Las clásicas (eyJ…) van en "apikey" y en "Authorization".
    h = {"apikey": SERVICIO}
    if not SERVICIO.startswith("sb_secret_"):
        h["Authorization"] = f"Bearer {SERVICIO}"
    return h


def url_publica(ruta, tam="m"):
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{ruta}_{tam}.webp"


# ------------------------------------------------------------------ base de datos
def conectar():
    return psycopg2.connect(DATABASE_URL, connect_timeout=20, keepalives=1,
                            keepalives_idle=30, keepalives_interval=10, keepalives_count=5)


def fotos_a_copiar(conn):
    """Una foto por producto activo que NO tiene ninguna foto de Shopify."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT p.producto_clave AS k, l.imagen
        FROM productos p
        JOIN listados l ON l.producto_id = p.id AND l.activo
        WHERE coalesce(l.imagen, '') <> ''
    """)
    por_producto = {}
    for r in cur.fetchall():
        u = origen_img(r["imagen"])
        if u and u.lower().startswith("http"):
            por_producto.setdefault(r["k"], []).append(u)
    elegidas = set()
    for urls in por_producto.values():
        if any(es_rapida(u) for u in urls):
            continue
        elegidas.add(sorted(urls)[0])           # orden estable: siempre la misma foto para el mismo producto
    cur.execute("SELECT url_origen, ok, intentos FROM foto_espejo")
    hechas = {r["url_origen"]: r for r in cur.fetchall()}
    return [u for u in sorted(elegidas)
            if u not in hechas or (not hechas[u]["ok"] and hechas[u]["intentos"] < MAX_INTENTOS)]


def anotar(conn, filas):
    cur = conn.cursor()
    psycopg2.extras.execute_values(cur, """
        INSERT INTO foto_espejo (clave, url_origen, ruta, bytes, ok, intentos, error, actualizado_en)
        VALUES %s
        ON CONFLICT (clave) DO UPDATE SET ruta = EXCLUDED.ruta, bytes = EXCLUDED.bytes, ok = EXCLUDED.ok,
            intentos = foto_espejo.intentos + 1, error = EXCLUDED.error, actualizado_en = now()
    """, [(clave(f["url"]), f["url"], f["ruta"], f["bytes"], f["ok"], 1, f["error"]) for f in filas],
        template="(%s, %s, %s, %s, %s, %s, %s, now())")
    conn.commit()


# ------------------------------------------------------------------ fotos
_local = threading.local()
def sesion():
    import requests
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        _local.s.headers.update(NAVEGADOR)
    return _local.s


def a_webp(contenido, lado):
    from PIL import Image
    im = Image.open(io.BytesIO(contenido))
    im.load()
    if im.mode in ("RGBA", "LA", "P"):                 # transparencia -> fondo blanco
        im = im.convert("RGBA")
        fondo = Image.new("RGB", im.size, (255, 255, 255))
        fondo.paste(im, mask=im.split()[-1])
        im = fondo
    else:
        im = im.convert("RGB")
    im.thumbnail((lado, lado), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=78, method=4)
    return out.getvalue()


def clave(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def copiar(url):
    h = clave(url)
    ruta = f"{h[:2]}/{h}"
    try:
        r = sesion().get(url, timeout=30)
        r.raise_for_status()
        if len(r.content) < 200:
            raise ValueError("la cadena devolvió una imagen vacía")
        total = 0
        for suf, lado in TAMANOS.items():
            datos = a_webp(r.content, lado)
            total += len(datos)
            sub = sesion().post(
                f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{ruta}_{suf}.webp",
                data=datos, timeout=30,
                headers={**cabeceras_storage(),
                         "Content-Type": "image/webp", "x-upsert": "true",
                         "Cache-Control": "max-age=31536000"},
            )
            if sub.status_code >= 300:
                raise RuntimeError(f"Storage {sub.status_code}: {sub.text[:120]}")
        return {"url": url, "ruta": ruta, "bytes": total, "ok": True, "error": None}
    except Exception as e:
        return {"url": url, "ruta": None, "bytes": None, "ok": False, "error": str(e)[:300]}


# ------------------------------------------------------------------ principal
def espejar(limite=None, dry_run=False, hilos=HILOS):
    if not DATABASE_URL:
        sys.exit("Falta DATABASE_URL en el .env")
    t0 = time.time()
    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('foto_espejo') IS NOT NULL")
        if not cur.fetchone()[0]:
            sys.exit("Falta la tabla foto_espejo: corré db/migracion_fotos.sql en Supabase.")
        falta = fotos_a_copiar(conn)
        if limite:
            falta = falta[:limite]
        print(f"[fotos] {len(falta)} fotos de Hipermaxi/Chávez por copiar"
              f" (~{len(falta) * 35 / 1024:.0f} MB en Storage)")
        if dry_run or not falta:
            return {"copiadas": 0, "pendientes": len(falta)}
        if not SERVICIO:
            sys.exit("Falta SUPABASE_SERVICE_ROLE_KEY en el .env (Supabase -> Project Settings -> API -> service_role).")
        rol = rol_de_la_clave(SERVICIO)
        if rol not in ("service_role", "secret"):
            sys.exit(f"La clave SUPABASE_SERVICE_ROLE_KEY del .env es de tipo '{rol}', no de servicio: Storage no deja subir con ella.\n"
                     "En Supabase -> Project Settings -> API Keys copiá la 'service_role' (pestaña Legacy, empieza con eyJ…) "
                     "o una 'Secret key' (empieza con sb_secret_…).")
        try:
            import PIL, requests  # noqa: F401
        except ImportError:
            sys.exit("Falta instalar: pip install pillow requests")

        ok = err = 0
        lote = []
        errores = {}
        ex = ThreadPoolExecutor(max_workers=hilos)
        try:
            futuros = [ex.submit(copiar, u) for u in falta]
            for i, fut in enumerate(as_completed(futuros), 1):
                f = fut.result()
                lote.append(f)
                ok += f["ok"]; err += not f["ok"]
                if not f["ok"]:
                    tipo = f["error"].split(":")[0][:60]
                    ej = errores.setdefault(tipo, [0, f["error"][:200], f["url"][:120]])
                    ej[0] += 1
                if len(lote) >= 50 or i == len(falta):
                    try:
                        anotar(conn, lote)
                    except psycopg2.OperationalError:
                        print("  se cortó la conexión con la base; reconectando…")
                        try: conn.close()
                        except Exception: pass
                        conn = conectar()
                        anotar(conn, lote)
                    lote = []
                    print(f"  {i}/{len(falta)} · {ok} copiadas · {err} con error", flush=True)
        except BaseException:
            ex.shutdown(wait=False, cancel_futures=True)
            print("\n[fotos] detenido: lo copiado quedó guardado. Volvé a correrlo y sigue desde ahí.")
            raise
        ex.shutdown(wait=True)
        print(f"[fotos] listo: {ok} copiadas, {err} con error, {time.time() - t0:.0f}s")
        if errores:
            print("[fotos] errores más comunes:")
            for tipo, (n, ejemplo, url) in sorted(errores.items(), key=lambda kv: -kv[1][0])[:5]:
                print(f"  {n:>5} × {ejemplo}\n          ej.: {url}")
        return {"copiadas": ok, "pendientes": err}
    finally:
        try: conn.close()
        except Exception: pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="solo cuenta, no baja ni sube nada")
    ap.add_argument("--limite", type=int, help="copiar como máximo N fotos (para probar)")
    ap.add_argument("--hilos", type=int, default=HILOS)
    a = ap.parse_args()
    espejar(limite=a.limite, dry_run=a.dry_run, hilos=a.hilos)


if __name__ == "__main__":
    main()

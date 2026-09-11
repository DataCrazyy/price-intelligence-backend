"""
alertas.py — Alertas de precio por correo, para un SKU puntual contra una
cadena puntual.

Guarda reglas ("avisame si el precio de <producto> en <cadena> sube / baja
/ cambia") en Postgres (tabla alertas_precio, ver db/migracion_alertas.sql)
y, al correrse, revisa cada regla activa contra el precio VIGENTE (tabla
precios, vía listados) y manda un correo por Gmail si se cumple la
condición.

Pensado para programarse APARTE del scraping (Task Scheduler de Windows,
en su propio horario) -- así avisa un día aunque ese día no haya corrido
run_all.py, con el precio que ya está guardado.

Uso (CLI, para probar a mano desde la terminal):
    python alertas.py crear <producto_clave> <cadena> <condicion> [--umbral 5] [--email correo@dominio.com]
    python alertas.py listar
    python alertas.py eliminar <alerta_id>
    python alertas.py pausar <alerta_id>
    python alertas.py reanudar <alerta_id>
    python alertas.py revisar          # <- la corrida real, esto es lo que va en Task Scheduler

El dashboard (dashboard.html, pestaña Alertas) usa estas mismas funciones
a través de los endpoints nuevos en api.py -- no hace falta tocar la
consola para el uso normal, esto es solo para pruebas o mantenimiento manual.

Variables de entorno nuevas (agregar a postgres_migration/.env, una por
línea, sin comillas):
    GMAIL_USER              tu.correo@gmail.com
    GMAIL_APP_PASSWORD      contraseña de aplicación de 16 caracteres -- NO tu contraseña normal de Gmail.
                             Se genera en https://myaccount.google.com/apppasswords
                             (requiere tener la verificación en 2 pasos activada en esa cuenta de Google).
    ALERTAS_EMAIL_DEFAULT   correo al que llegan las alertas cuando no se especifica uno al crearlas
                             (si no se define, cae de vuelta a GMAIL_USER).
"""
import argparse
import os
import smtplib
import sys
from email.mime.text import MIMEText

import psycopg2.extras
from dotenv import load_dotenv

from etl import get_conn

load_dotenv()
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_DEFAULT = os.environ.get("ALERTAS_EMAIL_DEFAULT") or GMAIL_USER

CONDICIONES = ("sube", "baja", "cualquier_cambio")


def obtener_precio_actual(conn, producto_clave: str, cadena: str):
    """Precio vigente (tabla `precios`) de ese producto en ESA cadena
    puntual -- None si esa cadena todavía no matcheó ese producto, o dejó
    de trackearlo."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.precio_oferta
        FROM precios p
        JOIN listados l  ON l.id = p.listado_id
        JOIN productos pr ON pr.id = l.producto_id
        JOIN cadenas c   ON c.id = l.cadena_id
        WHERE pr.producto_clave = %s AND c.nombre = %s
        """,
        (producto_clave, cadena),
    )
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row else None


def crear_alerta(producto_clave: str, cadena: str, condicion: str, umbral_pct: float | None, email: str | None):
    if condicion not in CONDICIONES:
        raise ValueError(f"condicion debe ser una de {CONDICIONES}")
    if not email and not EMAIL_DEFAULT:
        raise ValueError("Sin correo: pasá --email o definí ALERTAS_EMAIL_DEFAULT/GMAIL_USER en .env")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT nombre FROM productos WHERE producto_clave = %s", (producto_clave,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise ValueError(f"No existe ningún producto con producto_clave='{producto_clave}'")
    nombre = row[0]

    precio_actual = obtener_precio_actual(conn, producto_clave, cadena)

    cur.execute(
        """
        INSERT INTO alertas_precio
            (producto_clave, producto_nombre, cadena, condicion, umbral_pct, email, precio_base, ultimo_precio_visto)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (producto_clave, nombre, cadena, condicion, umbral_pct, email or EMAIL_DEFAULT, precio_actual, precio_actual),
    )
    alerta_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return alerta_id


def listar_alertas(solo_activas: bool = False):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    q = "SELECT * FROM alertas_precio"
    if solo_activas:
        q += " WHERE activo = TRUE"
    q += " ORDER BY creado_en DESC"
    cur.execute(q)
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return filas


def set_activo(alerta_id: str, activo: bool) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE alertas_precio SET activo = %s WHERE id = %s", (activo, alerta_id))
    afectadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afectadas > 0


def eliminar_alerta(alerta_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM alertas_precio WHERE id = %s", (alerta_id,))
    afectadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afectadas > 0


def enviar_email(destino: str, asunto: str, cuerpo: str):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "Faltan GMAIL_USER / GMAIL_APP_PASSWORD en .env -- ver instrucciones en el encabezado de este archivo."
        )
    msg = MIMEText(cuerpo)
    msg["Subject"] = asunto
    msg["From"] = GMAIL_USER
    msg["To"] = destino
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [destino], msg.as_string())


def revisar():
    """La corrida real -- esto es lo que va en Task Scheduler. Por cada
    alerta activa, compara el precio vigente contra `precio_base` (el
    precio al momento del último disparo, o al crearla si nunca disparó)
    y manda un correo si la condición se cumple. No dispara dos veces por
    el mismo movimiento: tras mandar el correo, `precio_base` pasa a ser
    el precio que lo disparó, así la próxima alerta es por el SIGUIENTE
    cambio, no por el mismo de nuevo."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM alertas_precio WHERE activo = TRUE")
    activas = cur.fetchall()
    disparadas, errores = 0, 0

    for a in activas:
        precio_actual = obtener_precio_actual(conn, a["producto_clave"], a["cadena"])
        if precio_actual is None:
            continue  # esa cadena todavía no tiene match para este producto

        base = float(a["precio_base"]) if a["precio_base"] is not None else precio_actual
        variacion_pct = 0.0 if base == 0 else (precio_actual - base) / base * 100
        umbral = float(a["umbral_pct"]) if a["umbral_pct"] is not None else 0.0

        cumple = False
        if a["condicion"] == "sube" and variacion_pct > 0 and variacion_pct >= umbral:
            cumple = True
        elif a["condicion"] == "baja" and variacion_pct < 0 and -variacion_pct >= umbral:
            cumple = True
        elif a["condicion"] == "cualquier_cambio" and precio_actual != base and abs(variacion_pct) >= umbral:
            cumple = True

        cur.execute("UPDATE alertas_precio SET ultimo_precio_visto = %s WHERE id = %s", (precio_actual, a["id"]))

        if cumple:
            direccion = "subió" if variacion_pct > 0 else "bajó"
            asunto = f"[Price Intelligence] {a['producto_nombre']} {direccion} en {a['cadena']}"
            cuerpo = (
                f"{a['producto_nombre']}\n"
                f"Cadena: {a['cadena']}\n"
                f"Precio anterior: Bs {base:.2f}\n"
                f"Precio actual:   Bs {precio_actual:.2f}\n"
                f"Variación: {variacion_pct:+.1f}%\n"
            )
            try:
                enviar_email(a["email"], asunto, cuerpo)
                cur.execute(
                    "UPDATE alertas_precio SET precio_base = %s, ultimo_disparo_en = now() WHERE id = %s",
                    (precio_actual, a["id"]),
                )
                disparadas += 1
                print(f"[alertas] Correo enviado: {asunto} -> {a['email']}")
            except Exception as e:
                errores += 1
                print(f"[alertas] ERROR mandando correo para alerta {a['id']}: {e}", file=sys.stderr)

    conn.commit()
    cur.close()
    conn.close()
    print(f"[alertas] Revisión terminada. {len(activas)} activas, {disparadas} correos enviados, {errores} errores.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)

    p1 = sub.add_parser("crear")
    p1.add_argument("producto_clave")
    p1.add_argument("cadena")
    p1.add_argument("condicion", choices=CONDICIONES)
    p1.add_argument("--umbral", type=float, default=None, help="%% mínimo de variación para disparar (vacío = cualquier cambio)")
    p1.add_argument("--email", default=None)

    sub.add_parser("listar")
    sub.add_parser("revisar")

    p2 = sub.add_parser("eliminar")
    p2.add_argument("alerta_id")

    p3 = sub.add_parser("pausar")
    p3.add_argument("alerta_id")

    p4 = sub.add_parser("reanudar")
    p4.add_argument("alerta_id")

    args = ap.parse_args()
    if args.comando == "crear":
        alerta_id = crear_alerta(args.producto_clave, args.cadena, args.condicion, args.umbral, args.email)
        print(f"Alerta creada: {alerta_id}")
    elif args.comando == "listar":
        filas = listar_alertas()
        if not filas:
            print("Todavía no hay alertas creadas.")
        for a in filas:
            estado = "activa" if a["activo"] else "pausada"
            umbral = f"{a['umbral_pct']}%" if a["umbral_pct"] is not None else "cualquiera"
            print(f"[{estado}] {a['producto_nombre']} en {a['cadena']} -- {a['condicion']} (umbral {umbral}) -- {a['email']} -- id: {a['id']}")
    elif args.comando == "revisar":
        revisar()
    elif args.comando == "eliminar":
        print("Eliminada." if eliminar_alerta(args.alerta_id) else "No se encontró esa alerta.")
    elif args.comando == "pausar":
        print("Pausada." if set_activo(args.alerta_id, False) else "No se encontró esa alerta.")
    elif args.comando == "reanudar":
        print("Reanudada." if set_activo(args.alerta_id, True) else "No se encontró esa alerta.")


if __name__ == "__main__":
    main()

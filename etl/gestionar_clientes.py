"""
gestionar_clientes.py — Alta de clientes y API keys (Fase 3, uso interno
de David/Diego -- esto NO es un endpoint público, es un script que corrés
vos a mano cuando sumás un cliente nuevo).

Uso:
    # 1) Crear el cliente
    python gestionar_clientes.py crear-cliente "Farmacia XYZ" --plan starter --rate-limit 120

    # 2) Generarle una API key (esto es lo único que se le manda al cliente
    #    -- la key en texto plano se muestra UNA sola vez, acá, en la
    #    terminal. Postgres sólo guarda su hash, así que si la perdés hay
    #    que generar una nueva, no se puede "recuperar" la vieja)
    python gestionar_clientes.py crear-key <cliente_id>

    # 3) Ver todo lo que existe
    python gestionar_clientes.py listar

    # 4) Desactivar una key (ej. el cliente canceló, o se filtró la key)
    python gestionar_clientes.py revocar-key <api_key_id>
"""
import argparse
import secrets
import sys

import psycopg2.extras

from auth import hash_key
from etl import get_conn


def crear_cliente(nombre: str, plan: str, rate_limit: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO clientes (nombre, plan, rate_limit_por_hora) VALUES (%s, %s, %s) RETURNING id",
        (nombre, plan, rate_limit),
    )
    cliente_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    print(f"Cliente creado: {nombre} (plan={plan}, {rate_limit} req/hora)")
    print(f"cliente_id: {cliente_id}")
    print(f"\nSiguiente paso: python gestionar_clientes.py crear-key {cliente_id}")


def crear_key(cliente_id: str, scopes: list[str]):
    key_plana = "pi_live_" + secrets.token_urlsafe(32)
    key_prefix = key_plana[:12]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT nombre FROM clientes WHERE id = %s", (cliente_id,))
    row = cur.fetchone()
    if not row:
        print(f"ERROR: no existe un cliente con id {cliente_id}", file=sys.stderr)
        sys.exit(1)

    cur.execute(
        "INSERT INTO api_keys (cliente_id, key_hash, key_prefix, scopes) VALUES (%s, %s, %s, %s) RETURNING id",
        (cliente_id, hash_key(key_plana), key_prefix, scopes),
    )
    api_key_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    print(f"API key creada para '{row[0]}'.")
    print()
    print("=" * 70)
    print(f"  {key_plana}")
    print("=" * 70)
    print()
    print("Copiá esto AHORA y mandaselo al cliente por un canal seguro -- no se")
    print("puede volver a mostrar (Postgres sólo guarda el hash). Si se pierde,")
    print(f"generá una key nueva y revocá esta (api_key_id: {api_key_id}).")
    print()
    print("El cliente la usa así, en cada request:")
    print(f'    curl -H "X-API-Key: {key_plana}" http://tu-servidor/v1/precios')


def listar():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT c.nombre AS cliente, c.plan, c.rate_limit_por_hora, c.activo AS cliente_activo,
               k.id AS api_key_id, k.key_prefix, k.activa AS key_activa, k.ultimo_uso_en
        FROM clientes c
        LEFT JOIN api_keys k ON k.cliente_id = c.id
        ORDER BY c.creado_en, k.creado_en
        """
    )
    filas = cur.fetchall()
    cur.close()
    conn.close()
    if not filas:
        print("Todavía no hay clientes.")
        return
    for f in filas:
        estado_cliente = "activo" if f["cliente_activo"] else "INACTIVO"
        print(f"{f['cliente']} [{f['plan']}, {f['rate_limit_por_hora']} req/hora, {estado_cliente}]")
        if f["api_key_id"]:
            estado_key = "activa" if f["key_activa"] else "REVOCADA"
            uso = f["ultimo_uso_en"].isoformat() if f["ultimo_uso_en"] else "nunca usada"
            print(f"    key {f['key_prefix']}... [{estado_key}] -- último uso: {uso} -- id: {f['api_key_id']}")
        else:
            print("    (sin API key todavía)")


def revocar_key(api_key_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE api_keys SET activa = FALSE WHERE id = %s", (api_key_id,))
    afectadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if afectadas:
        print(f"Key {api_key_id} revocada.")
    else:
        print(f"No se encontró ninguna key con id {api_key_id}", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)

    p1 = sub.add_parser("crear-cliente")
    p1.add_argument("nombre")
    p1.add_argument("--plan", default="trial", choices=["trial", "starter", "pro"])
    p1.add_argument("--rate-limit", type=int, default=60, help="requests por hora permitidos")

    p2 = sub.add_parser("crear-key")
    p2.add_argument("cliente_id")
    p2.add_argument("--scopes", nargs="+", default=["read:precios"])

    sub.add_parser("listar")

    p4 = sub.add_parser("revocar-key")
    p4.add_argument("api_key_id")

    args = ap.parse_args()
    if args.comando == "crear-cliente":
        crear_cliente(args.nombre, args.plan, args.rate_limit)
    elif args.comando == "crear-key":
        crear_key(args.cliente_id, args.scopes)
    elif args.comando == "listar":
        listar()
    elif args.comando == "revocar-key":
        revocar_key(args.api_key_id)


if __name__ == "__main__":
    main()

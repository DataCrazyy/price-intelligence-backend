"""
auth.py — API keys + rate limiting por cliente (Fase 3 del blueprint).

Cómo funciona una request autenticada:
    1. El cliente manda su key en el header `X-API-Key`.
    2. Se hashea (SHA-256) y se busca en `api_keys` -- la key en texto
       plano NUNCA se guarda en Postgres, sólo el hash. Si no matchea
       ninguna fila, o la key/cliente está inactivo o expirado -> 401.
    3. Se cuenta cuántas requests hizo esa key en la última hora
       (tabla `api_key_uso`) y se compara contra `clientes.rate_limit_por_hora`.
       Si ya se pasó -> 429.
    4. Se registra esta request en `api_key_uso` y se sigue.

Generar una key nueva es trabajo de administración (ver
gestionar_clientes.py) -- este módulo sólo valida, no crea.
"""
import hashlib
from datetime import datetime, timezone

import psycopg2.extras
from fastapi import Header, HTTPException

import etl


def hash_key(key_plana: str) -> str:
    return hashlib.sha256(key_plana.encode("utf-8")).hexdigest()


def requerir_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> dict:
    """Dependency de FastAPI: usar como `Depends(requerir_api_key)` en
    cualquier endpoint que deba quedar detrás de autenticación. Devuelve
    un dict con el cliente autenticado, disponible en el endpoint para
    filtrar la respuesta según su plan/scopes más adelante."""
    conn = etl.get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        key_hash = hash_key(x_api_key)
        cur.execute(
            """
            SELECT k.id AS api_key_id, k.activa, k.expira_en, k.scopes,
                   c.id AS cliente_id, c.nombre AS cliente_nombre, c.plan,
                   c.activo AS cliente_activo, c.rate_limit_por_hora
            FROM api_keys k
            JOIN clientes c ON c.id = k.cliente_id
            WHERE k.key_hash = %s
            """,
            (key_hash,),
        )
        row = cur.fetchone()
        if not row or not row["activa"] or not row["cliente_activo"]:
            raise HTTPException(401, "API key inválida o inactiva")
        if row["expira_en"] and row["expira_en"] < datetime.now(timezone.utc):
            raise HTTPException(401, "API key expirada")

        cur.execute(
            "SELECT count(*) AS n FROM api_key_uso "
            "WHERE api_key_id = %s AND solicitado_en > now() - interval '1 hour'",
            (row["api_key_id"],),
        )
        usados = cur.fetchone()["n"]
        if usados >= row["rate_limit_por_hora"]:
            raise HTTPException(
                429,
                f"Límite de {row['rate_limit_por_hora']} requests/hora alcanzado para este plan ({row['plan']}). "
                f"Probá de nuevo más tarde.",
            )

        cur.execute("INSERT INTO api_key_uso (api_key_id) VALUES (%s)", (row["api_key_id"],))
        cur.execute("UPDATE api_keys SET ultimo_uso_en = now() WHERE id = %s", (row["api_key_id"],))
        conn.commit()

        return {
            "cliente_id": row["cliente_id"],
            "cliente_nombre": row["cliente_nombre"],
            "plan": row["plan"],
            "scopes": row["scopes"],
            "api_key_id": row["api_key_id"],
        }
    finally:
        cur.close()
        conn.close()

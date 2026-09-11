"""
matching_llm.py — Fallback a LLM para casos ambiguos de matching (Fase 2
del blueprint).

Cuando dos productos de distinta cadena tienen un score de RapidFuzz en
"zona gris" (ni tan alto como para fusionar solos con confianza, ni tan
bajo como para descartar sin mirar), en vez de dejarlos sin matchear para
siempre, se le pregunta a un LLM "¿son el mismo producto?" con los
nombres reales.

Usa la API de OpenAI -- el blueprint original decía "Claude API", pero
David ya tiene créditos cargados en OpenAI, así que se usa esa. El
proveedor queda aislado en este único archivo (la función revisar_par()
es el contrato); cambiarlo a Claude/Anthropic más adelante es tocar sólo
este módulo, no etl.py.

Requiere:
    pip install openai
    OPENAI_API_KEY en tu .env (opcional OPENAI_MODEL, default gpt-4o-mini)

Todas las consultas quedan cacheadas en Postgres (tabla
match_revisiones_llm, ver etl.py / db/migracion_fase2.sql) -- un mismo
par de productos nunca se le vuelve a preguntar al modelo, así que el
costo real es "una vez por par ambiguo que existió alguna vez", no "una
vez por corrida de matchear()".
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI  # import perezoso: si no se usa el fallback, no hace falta tener el paquete
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Falta OPENAI_API_KEY en el .env -- necesaria para el fallback de matching ambiguo (Fase 2)."
            )
        _client = OpenAI(api_key=api_key)
    return _client


SYSTEM_PROMPT = (
    "Sos un experto en catálogos de supermercado/farmacia en Bolivia. "
    "Te doy el nombre de dos productos, cada uno publicado por una cadena "
    "distinta, y tenés que decidir si son EXACTAMENTE el mismo producto "
    "(misma marca, misma presentación/tamaño, misma variante o etapa) "
    "aunque estén escritos distinto (orden de palabras, singular/plural, "
    "abreviaturas). Diferente tamaño, diferente etapa/variante (ej. "
    "'Nan 1' vs 'Nan 1 AR', 'Original' vs 'Sin Azúcar') o diferente marca "
    "NO son el mismo producto, aunque el nombre se parezca mucho. "
    "Respondé ÚNICAMENTE un JSON con este formato exacto, sin texto extra: "
    '{"mismo_producto": true|false, "confianza": 0.0-1.0, "razon": "..."}'
)


def revisar_par(nombre_a: str, nombre_b: str, categoria_a: str = None, categoria_b: str = None) -> dict:
    """Le pregunta al modelo si nombre_a y nombre_b son el mismo producto.
    Devuelve {"mismo_producto": bool, "confianza": float, "razon": str}.
    Deja que cualquier excepción (red, API, JSON inválido) suba tal cual
    -- el llamador (etl.matchear) decide qué hacer, normalmente: dejar el
    par pendiente y seguir, no tumbar la corrida entera por un fallo de
    la API."""
    client = _get_client()
    contexto = f'Producto A: "{nombre_a}"'
    if categoria_a:
        contexto += f" (categoría: {categoria_a})"
    contexto += f'\nProducto B: "{nombre_b}"'
    if categoria_b:
        contexto += f" (categoría: {categoria_b})"

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": contexto},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    data = json.loads(resp.choices[0].message.content)
    return {
        "mismo_producto": bool(data["mismo_producto"]),
        "confianza": max(0.0, min(1.0, float(data.get("confianza", 0.5)))),
        "razon": str(data.get("razon", ""))[:500],
    }

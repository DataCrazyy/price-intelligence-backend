-- Migración Fase 2 -- agregá esto a tu Postgres existente (no hace falta
-- recrear nada, sólo suma la tabla de cache para el fallback LLM de
-- matching ambiguo). Idempotente: correrlo dos veces no rompe nada.

CREATE TABLE IF NOT EXISTS match_revisiones_llm (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    producto_clave_a   TEXT NOT NULL,
    producto_clave_b   TEXT NOT NULL,
    nombre_a           TEXT,
    nombre_b           TEXT,
    score_fuzzy        NUMERIC(5,2),
    decision           BOOLEAN NOT NULL,
    confianza          NUMERIC(4,3),
    razon              TEXT,
    revisado_en        TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (producto_clave_a, producto_clave_b)
);

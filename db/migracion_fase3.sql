-- Migración Fase 3 -- clientes, API keys y rate limiting. Idempotente:
-- correrlo dos veces no rompe nada. No toca ninguna tabla existente.

CREATE TABLE IF NOT EXISTS clientes (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre                TEXT NOT NULL,
    plan                  TEXT NOT NULL DEFAULT 'trial',   -- 'trial' | 'starter' | 'pro'
    rate_limit_por_hora   INTEGER NOT NULL DEFAULT 60,
    activo                BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- La key en texto plano NUNCA se guarda -- sólo su hash SHA-256
-- (key_hash). key_prefix son los primeros caracteres visibles, para que
-- el cliente reconozca cuál key es cuál en un listado sin exponer la key
-- completa (mismo patrón que usan Stripe/GitHub).
CREATE TABLE IF NOT EXISTS api_keys (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cliente_id        UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    key_hash          TEXT NOT NULL UNIQUE,
    key_prefix        TEXT NOT NULL,
    scopes            TEXT[] NOT NULL DEFAULT '{read:precios}',
    activa            BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_uso_en     TIMESTAMPTZ,
    expira_en         TIMESTAMPTZ
);

-- Un registro por request autenticado -- de acá se calcula el rate limit
-- (requests en la última hora) sin depender de memoria del proceso, así
-- sobrevive a un restart de uvicorn y funciona igual con varios workers.
CREATE TABLE IF NOT EXISTS api_key_uso (
    id             BIGSERIAL PRIMARY KEY,
    api_key_id     UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    solicitado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_api_key_uso_key_fecha ON api_key_uso (api_key_id, solicitado_en DESC);
-- Nota para "después": cuando esta tabla crezca, un cron que borre filas
-- de más de 24-48hs es suficiente (el rate limit sólo mira la última hora).

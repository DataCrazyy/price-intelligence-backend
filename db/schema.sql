-- =========================================================================
-- Price Intelligence — esquema Postgres (Fase 0)
-- =========================================================================
-- Filosofía:
--   1) "productos"   = el producto CANÓNICO (el mismo Yogurt Kefir sin
--                       importar en qué cadena lo veas). Es lo que el
--                       agente de matching decide.
--   2) "listados"    = la fila que cada cadena/tienda expone de ese
--                       producto (su SKU, su URL, su imagen). Un producto
--                       canónico puede tener 1 listado por cadena.
--   3) "precios"     = el precio VIGENTE de cada listado (una fila por
--                       listado, se actualiza en el lugar — es lo que
--                       lees para "el precio de hoy").
--   4) "historial_precios" = serie de tiempo append-only. Nunca se
--                       actualiza, sólo se inserta. Aquí vive el histórico
--                       para las gráficas.
--   5) "agent_runs"  = auditoría de cada corrida de cada agente (para
--                       saber qué generó qué dato y cuándo).
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "pg_trgm";    -- búsqueda difusa de texto en SQL (útil para el agente de matching)

-- -------------------------------------------------------------------------
-- 1. Cadenas y sucursales
-- -------------------------------------------------------------------------
CREATE TABLE cadenas (
    id            SERIAL PRIMARY KEY,
    nombre        TEXT NOT NULL UNIQUE,         -- 'Hipermaxi', 'Fidalga', 'Farmacorp'...
    pais          CHAR(2) NOT NULL DEFAULT 'BO',
    sitio_web     TEXT,
    activo        BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sucursales (
    id            SERIAL PRIMARY KEY,
    cadena_id     INTEGER NOT NULL REFERENCES cadenas(id) ON DELETE CASCADE,
    nombre        TEXT NOT NULL,                 -- 'Hipermaxi Equipetrol'
    ciudad        TEXT NOT NULL DEFAULT 'Santa Cruz',
    UNIQUE (cadena_id, nombre)
);

-- -------------------------------------------------------------------------
-- 2. Productos canónicos (lo que el agente de MATCHING decide)
-- -------------------------------------------------------------------------
CREATE TABLE productos (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    producto_clave      TEXT NOT NULL UNIQUE,     -- el "sku-786024" / clave estable que ya usa el frontend
    nombre              TEXT NOT NULL,
    marca               TEXT,
    categoria           TEXT,
    subcategoria        TEXT,
    presentacion_valor  NUMERIC(10,2),             -- 900
    presentacion_unidad TEXT,                       -- 'ml' | 'g' | 'kg' | 'l' | 'un'
    gtin_ean            TEXT,                        -- cuando el sitio lo expone (código de barras)
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_productos_nombre_trgm ON productos USING gin (nombre gin_trgm_ops);
CREATE INDEX idx_productos_marca ON productos (marca);
CREATE INDEX idx_productos_categoria ON productos (categoria, subcategoria);

-- -------------------------------------------------------------------------
-- 3. Listados: la fila que CADA cadena expone de un producto canónico
-- -------------------------------------------------------------------------
CREATE TABLE listados (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    producto_id       UUID NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    cadena_id         INTEGER NOT NULL REFERENCES cadenas(id) ON DELETE CASCADE,
    sucursal_id       INTEGER REFERENCES sucursales(id),
    codigo_cadena     TEXT NOT NULL,               -- SKU/ID interno del sitio de origen (ej. '786024')
    nombre_original   TEXT NOT NULL,               -- nombre tal cual lo publica la cadena (sin normalizar)
    url               TEXT,
    imagen            TEXT,
    match_confidence  NUMERIC(4,3),                -- 0.000–1.000, qué tan seguro está el matching
    match_metodo      TEXT DEFAULT 'fuzzy',          -- 'fuzzy' | 'llm' | 'manual'
    activo            BOOLEAN NOT NULL DEFAULT TRUE, -- false si dejó de aparecer en el sitio (descontinuado)
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- la MISMA cadena nunca debe tener el mismo código de producto dos veces
    UNIQUE (cadena_id, codigo_cadena)
);

CREATE INDEX idx_listados_producto ON listados (producto_id);
CREATE INDEX idx_listados_cadena ON listados (cadena_id);

-- -------------------------------------------------------------------------
-- 3.5. Cache de revisiones LLM (Fase 2 — fallback para matching ambiguo)
-- -------------------------------------------------------------------------
-- Cuando RapidFuzz da un score en "zona gris" (ni fusiona solo ni se
-- descarta solo), se le pregunta a un LLM. La respuesta se guarda acá
-- para siempre -- un mismo par de productos nunca se le vuelve a
-- preguntar al modelo (ver etl/matching_llm.py).
CREATE TABLE match_revisiones_llm (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    producto_clave_a   TEXT NOT NULL,
    producto_clave_b   TEXT NOT NULL,
    nombre_a           TEXT,
    nombre_b           TEXT,
    score_fuzzy        NUMERIC(5,2),
    decision           BOOLEAN NOT NULL,        -- true = mismo producto
    confianza          NUMERIC(4,3),
    razon              TEXT,
    revisado_en        TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (producto_clave_a, producto_clave_b)
);

-- -------------------------------------------------------------------------
-- 4. Auditoría de agentes (se crea antes que "precios" e "historial_precios"
--    porque ambas la referencian por FK)
-- -------------------------------------------------------------------------
CREATE TABLE agent_runs (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agente_tipo           TEXT NOT NULL,   -- 'scraper' | 'normalizador' | 'matching' | 'validador'
    cadena_id             INTEGER REFERENCES cadenas(id),
    fuente_metodo         TEXT,             -- 'html' | 'api_interna' | 'excel_manual'
    iniciado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalizado_en         TIMESTAMPTZ,
    estado                TEXT NOT NULL DEFAULT 'en_progreso', -- 'ok' | 'error' | 'en_progreso'
    registros_procesados  INTEGER DEFAULT 0,
    registros_error       INTEGER DEFAULT 0,
    detalle               JSONB             -- log libre: qué falló, qué cambió, etc.
);

-- -------------------------------------------------------------------------
-- 5. Precios VIGENTES (una fila por listado — se actualiza en el lugar)
-- -------------------------------------------------------------------------
CREATE TABLE precios (
    listado_id        UUID PRIMARY KEY REFERENCES listados(id) ON DELETE CASCADE,
    precio_regular    NUMERIC(12,2) NOT NULL,
    precio_oferta     NUMERIC(12,2) NOT NULL,
    moneda            CHAR(3) NOT NULL DEFAULT 'BOB',
    descuento_bs      NUMERIC(12,2) GENERATED ALWAYS AS (precio_regular - precio_oferta) STORED,
    descuento_pct     NUMERIC(5,2)  GENERATED ALWAYS AS (
                          CASE WHEN precio_regular > 0
                               THEN round(((precio_regular - precio_oferta) / precio_regular) * 100, 2)
                               ELSE 0 END
                      ) STORED,
    tiene_descuento   BOOLEAN GENERATED ALWAYS AS (precio_oferta < precio_regular) STORED,
    capturado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    agente_run_id     UUID REFERENCES agent_runs(id)
);

-- -------------------------------------------------------------------------
-- 6. Historial de precios — serie de tiempo, SOLO INSERT
-- -------------------------------------------------------------------------
CREATE TABLE historial_precios (
    id                BIGSERIAL PRIMARY KEY,
    listado_id        UUID NOT NULL REFERENCES listados(id) ON DELETE CASCADE,
    fecha             DATE NOT NULL,
    precio_regular    NUMERIC(12,2) NOT NULL,
    precio_oferta     NUMERIC(12,2) NOT NULL,
    capturado_en      TIMESTAMPTZ NOT NULL DEFAULT now(),
    agente_run_id     UUID REFERENCES agent_runs(id),

    -- un listado no debería tener dos capturas "oficiales" el mismo día
    UNIQUE (listado_id, fecha)
);

CREATE INDEX idx_historial_listado_fecha ON historial_precios (listado_id, fecha DESC);
-- Nota para "después": cuando esta tabla crezca a millones de filas,
-- particionarla por mes (PARTITION BY RANGE (fecha)) o migrarla a
-- TimescaleDB (hypertable) sin cambiar una sola query de tu app.

-- -------------------------------------------------------------------------
-- 7. Multi-tenant / SaaS (Fase 3) -- clientes, API keys, rate limiting.
--    Ver también db/migracion_fase3.sql (misma definición, para bases ya
--    creadas sin recrear todo).
-- -------------------------------------------------------------------------
CREATE TABLE clientes (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre                TEXT NOT NULL,
    plan                  TEXT NOT NULL DEFAULT 'trial',   -- 'trial' | 'starter' | 'pro'
    rate_limit_por_hora   INTEGER NOT NULL DEFAULT 60,
    activo                BOOLEAN NOT NULL DEFAULT TRUE,
    creado_en             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- La key en texto plano NUNCA se guarda -- sólo su hash SHA-256 (key_hash).
-- key_prefix son los primeros caracteres visibles (mismo patrón que
-- Stripe/GitHub), para que el cliente reconozca cuál key es cuál sin ver
-- la key completa de nuevo.
CREATE TABLE api_keys (
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
-- (requests en la última hora) sin depender de memoria del proceso.
CREATE TABLE api_key_uso (
    id             BIGSERIAL PRIMARY KEY,
    api_key_id     UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    solicitado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_api_key_uso_key_fecha ON api_key_uso (api_key_id, solicitado_en DESC);

-- =========================================================================
-- Tablas para "un después" (dashboard/alertas, Fase 3 avanzada) —
-- comentadas a propósito, activar cuando haya un cliente real esperando.
-- =========================================================================
--
-- CREATE TABLE alertas (
--     id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
--     cliente_id      UUID NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
--     producto_id     UUID REFERENCES productos(id),
--     categoria       TEXT,
--     condicion       TEXT NOT NULL,   -- 'baja_precio' | 'nueva_oferta' | 'cambio_pct'
--     umbral_pct      NUMERIC(5,2),
--     canal           TEXT NOT NULL DEFAULT 'whatsapp',  -- 'whatsapp' | 'email' | 'webhook'
--     destino         TEXT NOT NULL,   -- número, correo o URL de webhook
--     activa          BOOLEAN NOT NULL DEFAULT TRUE
-- );
--
-- -- Row Level Security: cuando tengas clientes reales viendo datos filtrados,
-- -- esto asegura que un cliente NUNCA vea filas de otro aunque haya un bug
-- -- en el código del API:
-- ALTER TABLE alertas ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY alertas_por_cliente ON alertas
--     USING (cliente_id = current_setting('app.cliente_id')::uuid);

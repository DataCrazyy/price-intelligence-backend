-- =========================================================================
-- Lista de compras con IA — control de uso y registro
-- =========================================================================
-- La función supabase/functions/armar-lista llama a la API de OpenAI, que cuesta
-- dinero por uso. Esta tabla cuenta cada lista armada para aplicar topes
-- (por persona por hora y total por día) y saber cuánto se gasta.
--
-- `ip_hash` es un resumen irreversible de la IP (no se guarda la IP).
-- Solo la función (con la clave de servicio) escribe y lee acá: la clave
-- pública no tiene ningún acceso.
--
-- Correr UNA vez en Supabase -> SQL Editor. Es idempotente.
-- =========================================================================

CREATE TABLE IF NOT EXISTS lista_ia_uso (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    creado_en       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip_hash         TEXT NOT NULL,
    items           INTEGER NOT NULL DEFAULT 0,
    encontrados     INTEGER NOT NULL DEFAULT 0,
    tokens_entrada  INTEGER NOT NULL DEFAULT 0,
    tokens_salida   INTEGER NOT NULL DEFAULT 0,
    ms              INTEGER,
    error           TEXT
);
ALTER TABLE lista_ia_uso ADD COLUMN IF NOT EXISTS modelo TEXT;   -- qué modelo de OpenAI respondió
CREATE INDEX IF NOT EXISTS idx_lista_ia_uso_ip_fecha ON lista_ia_uso (ip_hash, creado_en DESC);
CREATE INDEX IF NOT EXISTS idx_lista_ia_uso_fecha    ON lista_ia_uso (creado_en DESC);

ALTER TABLE lista_ia_uso ENABLE ROW LEVEL SECURITY;          -- sin políticas: la clave pública no ve nada
REVOKE ALL ON lista_ia_uso FROM anon, authenticated;

-- El registro de uso de la página acepta ahora el tipo 'lista'
ALTER TABLE web_eventos DROP CONSTRAINT IF EXISTS web_eventos_tipo_check;
ALTER TABLE web_eventos ADD CONSTRAINT web_eventos_tipo_check
    CHECK (tipo IN ('inicio', 'busqueda', 'categoria', 'listado', 'producto', 'tienda', 'compartir', 'lista'));

-- Reporte: listas armadas y costo aproximado por día. El costo usa precios
-- de gpt-4o-mini (US$0,15 por millón de tokens de entrada y US$0,60 de
-- salida); si cambiás de modelo, ajustá esos dos números.
CREATE SCHEMA IF NOT EXISTS analitica;
DROP VIEW IF EXISTS analitica.listas_ia;
CREATE VIEW analitica.listas_ia AS
SELECT creado_en::date AS dia,
       count(*)                                  AS listas,
       count(DISTINCT ip_hash)                   AS personas,
       round(avg(items), 1)                      AS items_promedio,
       round(100.0 * sum(encontrados) / nullif(sum(items), 0)) AS pct_items_encontrados,
       count(*) FILTER (WHERE error IS NOT NULL) AS con_error,
       sum(tokens_entrada)                       AS tokens_entrada,
       sum(tokens_salida)                        AS tokens_salida,
       round((sum(tokens_entrada) * 0.15 + sum(tokens_salida) * 0.60) / 1e6, 4) AS costo_usd_aprox,
       string_agg(DISTINCT modelo, ', ')         AS modelos
FROM lista_ia_uso
GROUP BY 1 ORDER BY 1 DESC;
REVOKE ALL ON ALL TABLES IN SCHEMA analitica FROM anon, authenticated, public;

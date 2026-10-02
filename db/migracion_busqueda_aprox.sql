-- =========================================================================
-- Búsqueda tolerante a errores de escritura ("meremelada" -> mermelada)
-- =========================================================================
-- Cuando la búsqueda normal no encuentra nada, la página y la lista con IA
-- llaman a esta función, que busca nombres PARECIDOS (por trigramas, con el
-- índice idx_web_nn_trgm de migracion_busqueda.sql).
--
-- Es de solo lectura y corre con los permisos de quien la llama
-- (SECURITY INVOKER): la clave pública solo ve lo mismo que ya ve en
-- web_productos.
--
-- Uso desde la API:  /rest/v1/rpc/buscar_aprox?q=meremelada&select=k,nombre&limit=20
--
-- Correr UNA vez en Supabase -> SQL Editor (después de migracion_busqueda.sql).
-- Es idempotente.
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION buscar_aprox(q TEXT, lim INT DEFAULT 60)
RETURNS SETOF web_productos
LANGUAGE sql STABLE SECURITY INVOKER
SET pg_trgm.word_similarity_threshold = 0.5
AS $$
    WITH b AS (SELECT regexp_replace(lower(unaccent(left(coalesce(q, ''), 80))), '\s+', ' ', 'g') AS t)
    SELECT w.*
    FROM web_productos w, b
    WHERE length(b.t) >= 3 AND b.t <% w.nn
    ORDER BY word_similarity(b.t, w.nn) DESC, w.n DESC, w.best_off DESC
    LIMIT least(greatest(coalesce(lim, 60), 1), 100)
$$;

REVOKE ALL ON FUNCTION buscar_aprox(TEXT, INT) FROM public;
GRANT EXECUTE ON FUNCTION buscar_aprox(TEXT, INT) TO anon, authenticated, service_role;

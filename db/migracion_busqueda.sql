-- =========================================================================
-- Búsqueda por NOMBRE del producto (no por la categoría de la cadena)
-- =========================================================================
-- Hasta ahora la web buscaba en `st`, que mezcla nombre + cadenas +
-- categoría de la cadena. Por eso "pañal" traía Bio-Oil (la cadena lo
-- tiene en "Pañales y toallas húmedas") y "pan" traía "Tulipán".
-- `nn` es solo el nombre, en minúsculas y sin acentos. La página busca
-- palabras completas en `nn` y usa `st` solo como respaldo.
--
-- Correr UNA vez en Supabase -> SQL Editor, ANTES de la próxima corrida de
-- publicar_web.py (que desde ahora también llena esta columna).
-- Es idempotente.
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE web_productos ADD COLUMN IF NOT EXISTS nn TEXT;

-- Llenarla ya con lo que hay (publicar_web.py la va a recalcular igual)
UPDATE web_productos
SET nn = regexp_replace(lower(unaccent(nombre)), '\s+', ' ', 'g')
WHERE nn IS NULL;

-- Índice para búsquedas por palabra (expresiones regulares e ilike)
CREATE INDEX IF NOT EXISTS idx_web_nn_trgm ON web_productos USING gin (nn gin_trgm_ops);

-- =========================================================================
-- Copia propia de las fotos lentas (Hipermaxi y Farmacias Chávez)
-- =========================================================================
-- Las fotos de Amarket, Fidalga y Farmacorp vienen del CDN de Shopify y
-- cargan rápido. Las de Hipermaxi y Chávez salen de servidores lentos: el
-- script etl/espejar_fotos.py las baja UNA vez, las achica (WebP) y las
-- guarda en Supabase Storage, en el bucket público `fotos`. La página las
-- carga de ahí.
--
--   foto_espejo: una fila por foto de origen (qué se copió, adónde, si falló)
--
-- Correr UNA vez en Supabase -> SQL Editor. Es idempotente.
-- =========================================================================

-- Bucket público (solo lectura para todos; solo la clave de servicio sube)
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('fotos', 'fotos', true, 2097152, ARRAY['image/webp'])
ON CONFLICT (id) DO UPDATE SET public = true;

CREATE TABLE IF NOT EXISTS foto_espejo (
    clave           TEXT PRIMARY KEY,            -- sha1 de la URL original (hay URLs muy largas)
    url_origen      TEXT NOT NULL,               -- la foto original de la cadena
    ruta            TEXT,                        -- carpeta/nombre en el bucket (sin el sufijo de tamaño)
    bytes           INTEGER,                     -- peso de las dos versiones juntas
    ok              BOOLEAN NOT NULL DEFAULT false,
    intentos        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_foto_espejo_ok ON foto_espejo (ok);

ALTER TABLE foto_espejo ENABLE ROW LEVEL SECURITY;      -- sin políticas: la clave pública no ve nada
REVOKE ALL ON foto_espejo FROM anon, authenticated;

-- Reporte: cuántas fotos hay copiadas y cuánto ocupan
CREATE SCHEMA IF NOT EXISTS analitica;
CREATE OR REPLACE VIEW analitica.fotos_espejo AS
SELECT count(*) FILTER (WHERE ok)                          AS copiadas,
       count(*) FILTER (WHERE NOT ok)                      AS con_error,
       round(sum(bytes) FILTER (WHERE ok) / 1048576.0, 1)  AS mb_usados
FROM foto_espejo;
REVOKE ALL ON ALL TABLES IN SCHEMA analitica FROM anon, authenticated, public;

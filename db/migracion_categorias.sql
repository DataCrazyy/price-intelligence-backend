-- =========================================================================
-- Categorías propias (categoría > subcategoría) clasificadas por nombre
-- =========================================================================
-- Cada cadena clasifica a su manera ("Lo Nuevo", "Zona Papa", "Supermercado"),
-- así que la categoría de la página ya no sale de ahí: etl/clasificar.py le
-- pide a la IA que ubique cada producto, por su nombre, en la lista fija de
-- etl/taxonomia.py. El resultado se guarda acá una sola vez por producto.
--
--   fuente = 'ia'      lo clasificó la IA (se puede rehacer)
--   fuente = 'manual'  lo corregiste a mano: la IA nunca lo pisa
--
-- Para corregir uno a mano:
--   UPDATE producto_categoria SET cat = 'Lácteos y Huevos', sub = 'Leche', fuente = 'manual'
--   WHERE k = '<producto_clave>';
--
-- Correr UNA vez en Supabase -> SQL Editor. Es idempotente.
-- =========================================================================

CREATE TABLE IF NOT EXISTS producto_categoria (
    k               TEXT PRIMARY KEY,             -- productos.producto_clave
    nombre          TEXT NOT NULL,                -- el nombre que se clasificó (si cambia, se reclasifica)
    cat             TEXT NOT NULL,
    sub             TEXT NOT NULL,
    fuente          TEXT NOT NULL DEFAULT 'ia' CHECK (fuente IN ('ia', 'manual')),
    modelo          TEXT,
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_producto_categoria_cat ON producto_categoria (cat, sub);

ALTER TABLE producto_categoria ENABLE ROW LEVEL SECURITY;    -- sin políticas: la clave pública no ve nada
REVOKE ALL ON producto_categoria FROM anon, authenticated;

-- La página filtra por subcategoría
ALTER TABLE web_productos ADD COLUMN IF NOT EXISTS sub TEXT;
CREATE INDEX IF NOT EXISTS idx_web_macro_sub ON web_productos (macro, sub);

-- Reporte: cuántos productos hay en cada categoría y cuántos corregiste a mano
CREATE SCHEMA IF NOT EXISTS analitica;
CREATE OR REPLACE VIEW analitica.categorias_clasificadas AS
SELECT cat, sub,
       count(*)                                   AS productos,
       count(*) FILTER (WHERE fuente = 'manual')  AS corregidos_a_mano
FROM producto_categoria
GROUP BY 1, 2 ORDER BY 1, 3 DESC;
REVOKE ALL ON ALL TABLES IN SCHEMA analitica FROM anon, authenticated, public;

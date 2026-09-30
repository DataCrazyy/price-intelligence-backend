-- =========================================================================
-- Superficie PÚBLICA de solo lectura para la página web (docs/index.html)
-- =========================================================================
-- La página (GitHub Pages) lee Supabase con la clave "anon", que es pública.
-- Hoy todas las tablas tienen RLS activo y sin políticas, así que la clave
-- anon no ve nada -- y así se quedan. Esta migración agrega DOS tablas
-- nuevas pensadas solo para la web, y abre únicamente esas, en modo lectura:
--
--   web_productos  una fila por producto canónico, ya calculada: mejor
--                  precio, cadenas, ahorro, categoría para la web, y los
--                  precios por cadena + historial en JSON.
--   web_resumen    una sola fila con los totales y la fecha de los datos.
--
-- Las llena etl/publicar_web.py al final de cada corrida (reemplaza a
-- export_json.py para la web). Nada de clientes, api_keys, alertas ni
-- listados crudos queda expuesto.
--
-- Correr UNA vez en Supabase -> SQL Editor (o con psql). Es idempotente.
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS web_productos (
    k               TEXT PRIMARY KEY,               -- producto_clave (es el enlace de la ficha: #/p/<k>)
    nombre          TEXT NOT NULL,
    macro           TEXT NOT NULL,                  -- categoría de la web (docs/categorias.js)
    img             TEXT,
    n               SMALLINT NOT NULL,              -- en cuántas cadenas está
    precio          NUMERIC(12,2) NOT NULL,         -- mejor precio de hoy
    precio_regular  NUMERIC(12,2) NOT NULL,         -- precio regular de esa misma oferta
    cadena          TEXT NOT NULL,                  -- cadena con el mejor precio
    url             TEXT,                           -- enlace a esa cadena
    ahorro          NUMERIC(12,2) NOT NULL DEFAULT 0,  -- más cara - más barata
    cadena_cara     TEXT,
    best_off        NUMERIC(5,2) NOT NULL DEFAULT 0,   -- % de descuento de la mejor oferta
    puntaje_oferta  NUMERIC(6,2) NOT NULL DEFAULT 0,   -- orden de "Ofertas de hoy"
    cadenas         TEXT[] NOT NULL,
    st              TEXT NOT NULL,                  -- texto de búsqueda: minúsculas, sin acentos
    listados        JSONB NOT NULL,                 -- [{c, p, r, u}] cadena, precio, regular, url
    historial       JSONB NOT NULL DEFAULT '[]',    -- [{f, c, p}] fecha, cadena, precio
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_web_st_trgm      ON web_productos USING gin (st gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_web_cadenas      ON web_productos USING gin (cadenas);
CREATE INDEX IF NOT EXISTS idx_web_macro        ON web_productos (macro, n DESC, ahorro DESC);
CREATE INDEX IF NOT EXISTS idx_web_n            ON web_productos (n DESC, ahorro DESC);
CREATE INDEX IF NOT EXISTS idx_web_oferta       ON web_productos (puntaje_oferta DESC) WHERE best_off >= 1;
CREATE INDEX IF NOT EXISTS idx_web_precio       ON web_productos (precio);

CREATE TABLE IF NOT EXISTS web_resumen (
    id              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    datos           JSONB NOT NULL,
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- Acceso: SOLO lectura, SOLO estas dos tablas -------------------------
ALTER TABLE web_productos ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_resumen   ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS web_productos_lectura ON web_productos;
CREATE POLICY web_productos_lectura ON web_productos FOR SELECT TO anon, authenticated USING (true);

DROP POLICY IF EXISTS web_resumen_lectura ON web_resumen;
CREATE POLICY web_resumen_lectura ON web_resumen FOR SELECT TO anon, authenticated USING (true);

-- Aunque RLS ya lo impide (no hay política de escritura), quitamos también
-- el permiso: defensa en profundidad.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON web_productos, web_resumen FROM anon, authenticated;
GRANT SELECT ON web_productos, web_resumen TO anon, authenticated;

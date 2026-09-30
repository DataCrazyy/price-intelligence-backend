-- =========================================================================
-- Registro de uso ANÓNIMO de la página web
-- =========================================================================
-- Qué busca la gente, qué búsquedas no encuentran nada, qué productos y
-- categorías abren, y cuándo tocan "Ir a la tienda". Sin datos personales:
-- `sesion` es un código al azar que genera el navegador (no es la IP, ni
-- un nombre, ni un correo).
--
-- Acceso con la clave pública (anon): SOLO puede AGREGAR filas. No puede
-- leer, cambiar ni borrar nada. Los reportes están en el esquema
-- `analitica`, que Supabase no publica: se consultan desde el SQL Editor.
--
-- Correr UNA vez en Supabase -> SQL Editor. Es idempotente.
-- =========================================================================

CREATE TABLE IF NOT EXISTS web_eventos (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    creado_en   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sesion      TEXT NOT NULL CHECK (length(sesion) BETWEEN 8 AND 64),
    tipo        TEXT NOT NULL CHECK (tipo IN ('inicio', 'busqueda', 'categoria', 'listado', 'producto', 'tienda', 'compartir')),
    q           TEXT CHECK (length(q) <= 120),           -- lo que escribió en el buscador
    k           TEXT CHECK (length(k) <= 200),           -- producto (producto_clave)
    cat         TEXT CHECK (length(cat) <= 80),
    cadena      TEXT CHECK (length(cadena) <= 80),       -- filtro de cadena, o la tienda a la que fue
    resultados  INTEGER CHECK (resultados >= 0),         -- cuántos productos encontró (0 = búsqueda sin resultado)
    precio      NUMERIC(12,2) CHECK (precio >= 0),       -- precio que vio al ir a la tienda
    extra       JSONB CHECK (pg_column_size(extra) <= 1000),  -- orden, oferta, origen... (chico, sin datos personales)
    movil       BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_web_eventos_fecha ON web_eventos (creado_en DESC);
CREATE INDEX IF NOT EXISTS idx_web_eventos_tipo  ON web_eventos (tipo, creado_en DESC);

ALTER TABLE web_eventos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS web_eventos_agregar ON web_eventos;
CREATE POLICY web_eventos_agregar ON web_eventos FOR INSERT TO anon, authenticated
    WITH CHECK (creado_en > now() - interval '5 minutes' AND creado_en < now() + interval '5 minutes');
-- Sin política de SELECT/UPDATE/DELETE: la clave pública no ve ni toca lo registrado.
REVOKE ALL ON web_eventos FROM anon, authenticated;
GRANT INSERT ON web_eventos TO anon, authenticated;

-- ---- Reportes (esquema privado: NO expuesto por la API de Supabase) -----
CREATE SCHEMA IF NOT EXISTS analitica;
REVOKE ALL ON SCHEMA analitica FROM anon, authenticated, public;

-- Uso por día
CREATE OR REPLACE VIEW analitica.uso_diario AS
SELECT creado_en::date AS dia,
       count(DISTINCT sesion)                                   AS visitantes,
       count(*) FILTER (WHERE tipo = 'busqueda')                AS busquedas,
       count(*) FILTER (WHERE tipo = 'busqueda' AND resultados = 0) AS busquedas_sin_resultado,
       count(*) FILTER (WHERE tipo = 'producto')                AS productos_vistos,
       count(*) FILTER (WHERE tipo = 'tienda')                  AS clics_a_tiendas,
       round(100.0 * count(*) FILTER (WHERE movil) / nullif(count(*), 0)) AS pct_movil
FROM web_eventos
GROUP BY 1 ORDER BY 1 DESC;

-- Qué busca la gente (últimos 30 días)
CREATE OR REPLACE VIEW analitica.busquedas_top AS
SELECT lower(trim(q)) AS busqueda, count(*) AS veces, count(DISTINCT sesion) AS personas,
       round(avg(resultados)) AS resultados_promedio
FROM web_eventos
WHERE tipo = 'busqueda' AND q IS NOT NULL AND creado_en > now() - interval '30 days'
GROUP BY 1 ORDER BY veces DESC;

-- Lo que buscan y NO encuentran: productos o cadenas a sumar
CREATE OR REPLACE VIEW analitica.busquedas_sin_resultado AS
SELECT lower(trim(q)) AS busqueda, count(*) AS veces, count(DISTINCT sesion) AS personas, max(creado_en) AS ultima_vez
FROM web_eventos
WHERE tipo = 'busqueda' AND resultados = 0 AND q IS NOT NULL
GROUP BY 1 ORDER BY veces DESC;

-- Productos más vistos y cuántas veces terminaron en la tienda
CREATE OR REPLACE VIEW analitica.productos_top AS
SELECT e.k,
       coalesce(w.nombre, e.k) AS nombre,
       count(*) FILTER (WHERE e.tipo = 'producto') AS vistas,
       count(*) FILTER (WHERE e.tipo = 'tienda')   AS clics_a_tienda,
       count(DISTINCT e.sesion)                    AS personas
FROM web_eventos e
LEFT JOIN web_productos w ON w.k = e.k
WHERE e.k IS NOT NULL AND e.creado_en > now() - interval '30 days'
GROUP BY e.k, w.nombre ORDER BY vistas DESC;

-- A qué cadena manda la página a la gente
CREATE OR REPLACE VIEW analitica.clics_por_cadena AS
SELECT cadena, count(*) AS clics, count(DISTINCT sesion) AS personas
FROM web_eventos
WHERE tipo = 'tienda' AND creado_en > now() - interval '30 days'
GROUP BY cadena ORDER BY clics DESC;

-- Categorías más visitadas
CREATE OR REPLACE VIEW analitica.categorias_top AS
SELECT cat, count(*) AS visitas, count(DISTINCT sesion) AS personas
FROM web_eventos
WHERE tipo = 'categoria' AND creado_en > now() - interval '30 days'
GROUP BY cat ORDER BY visitas DESC;

REVOKE ALL ON ALL TABLES IN SCHEMA analitica FROM anon, authenticated, public;

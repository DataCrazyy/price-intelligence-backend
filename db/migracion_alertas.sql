-- =========================================================================
-- Alertas de precio por correo (SKU puntual)
-- =========================================================================
-- Agregar a una base ya creada con db/schema.sql. Correr UNA sola vez:
--   psql "%DATABASE_URL%" -f db/migracion_alertas.sql
-- (o el equivalente con el cliente de Postgres que uses -- pgAdmin, DBeaver, etc.)
-- =========================================================================

CREATE TABLE IF NOT EXISTS alertas_precio (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    producto_clave        TEXT NOT NULL,
    producto_nombre       TEXT NOT NULL,          -- snapshot del nombre al crearla -- listarlas no requiere join
    cadena                TEXT NOT NULL,           -- nombre exacto de la cadena a vigilar (cadenas.nombre, ej. 'Fidalga')
    condicion             TEXT NOT NULL CHECK (condicion IN ('sube', 'baja', 'cualquier_cambio')),
    umbral_pct            NUMERIC(5,2),            -- NULL = cualquier variación, por chica que sea, dispara
    email                 TEXT NOT NULL,
    activo                BOOLEAN NOT NULL DEFAULT TRUE,
    precio_base           NUMERIC(12,2),           -- precio contra el que se compara la próxima revisión
                                                    -- (se actualiza cada vez que la alerta dispara, para no
                                                    -- reenviar el mismo movimiento de precio una y otra vez)
    ultimo_precio_visto   NUMERIC(12,2),           -- último precio leído, dispare o no -- lo muestra la UI
    ultimo_disparo_en     TIMESTAMPTZ,
    creado_en             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_alertas_precio_activo ON alertas_precio (activo);
CREATE INDEX IF NOT EXISTS idx_alertas_precio_clave ON alertas_precio (producto_clave);

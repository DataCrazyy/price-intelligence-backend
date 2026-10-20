-- Candados compartidos entre computadoras (David / Diego) -- ver etl/candados.py.
-- No hace falta correrlo a mano: candados.py crea la tabla sola la primera vez.
-- Queda acá como referencia. Idempotente.
CREATE TABLE IF NOT EXISTS candados (
    nombre     TEXT PRIMARY KEY,          -- 'cadena:<Nombre>' o 'procesar'
    dueno      TEXT NOT NULL,             -- token de la corrida que lo tiene
    equipo     TEXT NOT NULL,             -- PRECIOS_EQUIPO del .env (o usuario@computadora)
    detalle    TEXT,                      -- qué está haciendo, ej. 'Scraping Hipermaxi 40/149'
    paso       TEXT,                      -- scraping | clasificar | matching | export | fotos | publicar
    tomado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    latido_en  TIMESTAMPTZ NOT NULL DEFAULT now()   -- se renueva cada 30 s; sin latido 3 min = vencido
);
ALTER TABLE candados ENABLE ROW LEVEL SECURITY;      -- sin políticas: la clave pública no ve nada
REVOKE ALL ON candados FROM anon, authenticated;

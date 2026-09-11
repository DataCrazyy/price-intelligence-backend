-- =========================================================================
-- Vista puente: reconstruye EXACTAMENTE el shape de tu precios.json actual
-- a partir de las tablas normalizadas. Tu index.html no cambia ni una línea
-- — sólo cambia de dónde sale el archivo (de "lo escribo a mano" a
-- "lo exporta esta vista").
-- =========================================================================
CREATE OR REPLACE VIEW vista_precios_frontend AS
SELECT
    l.id                                                         AS listado_id,
    l.cadena_id || '-' || l.codigo_cadena                       AS id,
    p.producto_clave                                            AS producto_clave,
    l.codigo_cadena                                             AS codigo_cadena,
    p.nombre                                                    AS nombre,
    p.categoria                                                 AS categoria,
    p.subcategoria                                              AS subcategoria,
    c.nombre                                                    AS cadena,
    s.nombre                                                    AS sucursal,
    COALESCE(s.ciudad, 'Santa Cruz')                            AS ciudad,
    pr.precio_oferta                                            AS precio_oferta,
    pr.precio_regular                                           AS precio_regular,
    pr.descuento_bs                                             AS descuento_bs,
    pr.descuento_pct                                            AS descuento_pct,
    pr.tiene_descuento                                          AS tiene_descuento,
    l.imagen                                                    AS imagen,
    l.url                                                       AS url
FROM listados l
JOIN productos p  ON p.id = l.producto_id
JOIN cadenas c    ON c.id = l.cadena_id
LEFT JOIN sucursales s ON s.id = l.sucursal_id
JOIN precios pr   ON pr.listado_id = l.id
WHERE l.activo = TRUE;

-- Exportar como el JSON completo que ya consume tu frontend
-- (ejecuta esto y guarda el resultado como data/precios.json):
--
-- SELECT json_build_object(
--   'fecha_actualizacion', to_char(now(), 'YYYY-MM-DD'),
--   'fuentes', (SELECT array_agg(DISTINCT nombre) FROM cadenas WHERE activo),
--   'total_registros', (SELECT count(*) FROM vista_precios_frontend),
--   'total_productos_unicos', (SELECT count(DISTINCT producto_clave) FROM vista_precios_frontend),
--   'productos', (
--     SELECT json_agg(json_build_object(
--       'id', id, 'producto_clave', producto_clave, 'codigo_cadena', codigo_cadena,
--       'nombre', nombre, 'categoria', categoria, 'subcategoria', subcategoria,
--       'cadena', cadena, 'sucursal', sucursal, 'ciudad', ciudad,
--       'precio_oferta', precio_oferta, 'precio_regular', precio_regular,
--       'descuento_bs', descuento_bs, 'descuento_pct', descuento_pct,
--       'tiene_descuento', tiene_descuento, 'imagen', imagen, 'url', url,
--       'historial', (
--         SELECT json_agg(json_build_object('fecha', h.fecha, 'cadena', v.cadena, 'precio', h.precio_oferta) ORDER BY h.fecha)
--         FROM historial_precios h
--         WHERE h.listado_id = v.listado_id
--       )
--     ))
--     FROM vista_precios_frontend v
--   )
-- ) AS precios_json;

-- =========================================================================
-- Consultas que vas a usar seguido
-- =========================================================================

-- Precio más barato por producto, ahora mismo (para "mejor precio" del card)
SELECT p.nombre, c.nombre AS cadena, pr.precio_oferta
FROM precios pr
JOIN listados l ON l.id = pr.listado_id
JOIN productos p ON p.id = l.producto_id
JOIN cadenas c ON c.id = l.cadena_id
WHERE p.producto_clave = 'sku-786024'
ORDER BY pr.precio_oferta ASC
LIMIT 1;

-- Evolución de precio de un producto en una cadena (para la gráfica del modal)
SELECT h.fecha, h.precio_oferta
FROM historial_precios h
JOIN listados l ON l.id = h.listado_id
WHERE l.producto_id = (SELECT id FROM productos WHERE producto_clave = 'sku-786024')
ORDER BY h.fecha;

-- Productos con mayor descuento activo AHORA (para "mayor descuento" del hero)
SELECT p.nombre, c.nombre AS cadena, pr.descuento_pct
FROM precios pr
JOIN listados l ON l.id = pr.listado_id
JOIN productos p ON p.id = l.producto_id
JOIN cadenas c ON c.id = l.cadena_id
WHERE pr.tiene_descuento
ORDER BY pr.descuento_pct DESC
LIMIT 10;

-- Productos que un scraper dejó de ver en los últimos 3 días (posible descontinuado
-- o el scraper se rompió — esto lo consume el Agente Validador)
SELECT p.nombre, c.nombre, l.actualizado_en
FROM listados l
JOIN productos p ON p.id = l.producto_id
JOIN cadenas c ON c.id = l.cadena_id
WHERE l.activo AND l.actualizado_en < now() - interval '3 days';

-- Corridas de agentes que fallaron (para tu alerta interna de Slack/email)
SELECT agente_tipo, cadena_id, estado, registros_error, iniciado_en
FROM agent_runs
WHERE estado = 'error'
ORDER BY iniciado_en DESC
LIMIT 20;

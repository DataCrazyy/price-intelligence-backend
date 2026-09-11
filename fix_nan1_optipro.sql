-- Fix retroactivo: fusiona a mano el caso puntual que encontramos
-- ("Nan 1 Optipro X 400G" de Farmacorp y "Nan 1 Optipro X 400 Gr" de
-- Farmacias Chavez), que quedó atrapado por el bug de matchear() ya
-- corregido en etl.py. Corré esto UNA vez; matchear() de ahora en más
-- ya encuentra estos casos solo.

BEGIN;

UPDATE listados
SET producto_id = '53a3ec3f-818b-454f-b622-b96b54bb12d9',  -- Farmacorp: canónico (más antiguo)
    match_metodo = 'fuzzy',
    match_confidence = 0.90
WHERE producto_id = '08ef8179-ff87-4cea-a1cb-71f86a05f39e'  -- Farmacias Chavez
  AND activo = TRUE;

-- si el producto de Chavez quedó sin listados activos, se borra
DELETE FROM productos
WHERE id = '08ef8179-ff87-4cea-a1cb-71f86a05f39e'
  AND NOT EXISTS (SELECT 1 FROM listados WHERE producto_id = '08ef8179-ff87-4cea-a1cb-71f86a05f39e');

COMMIT;

-- Verificación: debería devolver una sola fila con match_metodo='fuzzy'
-- para los 4 listados originales, todos bajo el mismo producto_id.
SELECT l.id, c.nombre AS cadena, l.match_metodo, l.producto_id, p.nombre
FROM listados l
JOIN cadenas c ON c.id = l.cadena_id
JOIN productos p ON p.id = l.producto_id
WHERE l.producto_id = '53a3ec3f-818b-454f-b622-b96b54bb12d9';

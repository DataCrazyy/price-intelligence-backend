# Estándar de salida para cualquier Agente Scraper

Todo scraping nuevo (lo que pidas hoy, en 6 meses, o lo arme otro dev)
tiene que devolver un `.xlsx` con **estas columnas exactas**. Si las
respeta, `etl.py cargar-excel` lo carga sin tocar una línea de código.

| Columna         | Obligatoria | Formato                          | Ejemplo                        |
|-----------------|:-----------:|-----------------------------------|---------------------------------|
| `Articulo`      | Sí          | texto                             | `Leche Pil Natural 900 ml`      |
| `Precio_Oferta` | Sí          | número (punto decimal, sin "Bs")  | `9.7`                           |
| `Precio_Regular`| No          | número — si no hay oferta, igual a Precio_Oferta | `9.7`     |
| `CADENA`        | Sí          | nombre de la cadena, cualquier mayúscula/minúscula | `Hipermaxi` |
| `COD_ARTICULO`  | Sí          | el SKU/ID que usa el sitio de origen (NO inventar uno) | `786024` |
| `Categoria`     | No          | texto                             | `Lacteos Y Derivados`           |
| `Subcategoria`  | No          | texto                             | `Leches`                        |
| `Ciudad`        | No          | texto (default "Santa Cruz")      | `Santa Cruz`                    |
| `Sucursal`      | No          | texto                             | `Hipermaxi Equipetrol`          |
| `URL`           | No          | link al producto                  | `https://...`                   |
| `Imagen`        | No          | link a la foto                    | `https://...`                   |

**Reglas:**
- `Precio_Oferta` como **número**, no texto — evita `"Bs9,70"` si el sitio
  lo permite (el loader lo tolera igual con `parse_precio()`, pero número
  limpio es menos propenso a errores).
- `COD_ARTICULO` es la clave más importante de toda la tabla — es lo que
  usa Postgres para saber "esto ya lo vi" vs "esto es nuevo". Si el sitio
  no expone un ID visible, usa la URL completa del producto como
  `COD_ARTICULO` (nunca inventes un código a partir del nombre — el nombre
  puede repetirse, la URL no).
- **Para sitios Shopify: `COD_ARTICULO` = el `handle` del producto (la
  parte de la URL después de `/products/`), NUNCA el SKU.** El SKU puede
  venir vacío o cambiar entre variantes; el handle es estable y es lo que
  ya se usó para cargar los datos históricos — mezclar los dos esquemas en
  la misma cadena crea productos duplicados (nos pasó con Fidalga: un
  scraping usó handle, otro usó SKU, y el mismo producto entró dos veces).
  **Esta regla aplica siempre, sin excepción, para cualquier cadena Shopify
  (Fidalga, Farmacorp, y las que se agreguen después).**
- Nombre de cadena: escribe el que quieras (mayúsculas, minúsculas, con
  errores hasta cierto punto) — `get_or_create_cadena()` ya normaliza
  `HIPERMAXI`/`hipermaxi`/`Hipermaxi` al mismo registro. Si aparece una
  cadena nueva con nombre raro, agrégala a `CADENA_CANONICA` en `etl.py`.

## Prompt para pedir un scraping nuevo (copia y pega, cambia la URL)

```
Necesito que scrapees [nombre de la cadena] en [URL de la categoría].
Extrae: nombre, precio de oferta, precio regular (si existe), categoría,
subcategoría, imagen, URL del producto, y el código/SKU interno que use
el sitio.

Entrégame un Excel con EXACTAMENTE estas columnas (mismos nombres,
mismo orden no importa):
Articulo, Precio_Oferta, Precio_Regular, CADENA, COD_ARTICULO,
Categoria, Subcategoria, Ciudad, Sucursal, URL, Imagen

CADENA = "[nombre de la cadena]" en todas las filas.
Precio_Oferta y Precio_Regular como número (9.7), no texto ("Bs9,70").
COD_ARTICULO = el ID/SKU real del sitio, nunca inventado.
```

Con esto, el flujo completo para una cadena nueva queda en 4 pasos:
1. Pegas el prompt de arriba con la URL de la cadena nueva.
2. Te llega el `.xlsx`.
3. `python etl.py cargar-excel el_archivo.xlsx`
4. `python etl.py matchear` (para que se cruce con las cadenas que ya tienes).

import json
import scraper_shopify

sitio_base = "https://www.fidalga.com"
print(f"Conectando al endpoint de Shopify de {sitio_base}...")

# Llamamos a la función del scraper que descubre las colecciones públicas
try:
    descubiertas = scraper_shopify.listar_colecciones(sitio_base)
    
    if descubiertas:
        print(f"¡Se encontraron {len(descubiertas)} colecciones/categorías!")
        
        # Guardamos el resultado estructurado en el JSON de Fidalga
        archivo_salida = "shopify_categorias_www_fidalga_com.json"
        with open(archivo_salida, "w", encoding="utf-8") as f:
            json.dump(descubiertas, f, indent=2, ensure_ascii=False)
            
        print(f"Guardado exitosamente en '{archivo_salida}'.")
        
        # Mostramos un par de ejemplos para verificar
        print("\nEjemplos extraídos:")
        for col in descubiertas[:3]:
            print(f" - Título/Subcategoría: {col.get('titulo')} | Handle: {col.get('handle')} | Productos: {col.get('productos')}")
    else:
        print("La consulta devolvió una lista vacía. Revisa la conexión o el endpoint de Fidalga.")

except Exception as e:
    print(f"Ocurrió un error al extraer las colecciones: {e}")
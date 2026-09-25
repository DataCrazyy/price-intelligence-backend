from pathlib import Path
from contrato import ScrapeRequest
import scraper_chavez
import pandas as pd

def probar_chavez():
    cadena = "Farmacias Chavez"
    url_categoria = "https://www.farmaciaschavez.com.bo/ca/alimentos-y-bebidas/alimentos-y-bebidas/alimentos/101/alimentos-y-bebidas/alimentos/embutidos/101/10101/101/10101/1010105"
    
    print(f"Iniciando prueba para [{cadena}]")
    print(f"URL objetivo: {url_categoria}")
    
    # Crear el ScrapeRequest respetando el contrato de Chávez
    request = ScrapeRequest(
        cadena=cadena,
        url=url_categoria,
        categoria_default="Alimentos y Bebidas",
        subcategoria_default="Alimentos > Embutidos"
    )
    
    # Ejecutar el scraper de Chávez
    resultado = scraper_chavez.ejecutar(request)
    
    print(f"\nResultados del scraping:")
    print(f" - Productos válidos: {len(resultado.productos)}")
    print(f" - Errores descartados: {len(resultado.errores)}")
    
    if resultado.productos:
        # Extraer los datos al formato de diccionario para el DataFrame
        datos = resultado.raw_data or [p.model_dump() for p in resultado.productos]
        
        out_path = Path(__file__).parent / "prueba_chavez_embutidos.xlsx"
        pd.DataFrame(datos).to_excel(out_path, index=False)
        
        print(f"\n¡Archivo Excel generado con éxito en: {out_path}!")
        
        # Mostrar una muestra de los productos procesados
        print("\nMuestra de productos procesados:")
        for p in resultado.productos[:3]:
            print(f"   * {p.nombre} | Precio: {p.precio_oferta} | Cat: {p.categoria} | Subcat: {p.subcategoria}")
    else:
        print("\nLa prueba no arrojó productos válidos para esta URL.")

if __name__ == "__main__":
    probar_chavez()
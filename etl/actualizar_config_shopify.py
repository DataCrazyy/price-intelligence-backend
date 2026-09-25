import json
import os

config_path = "config_cadenas.json"

with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

mapeos_shopify = [
    {
        "cadena": "Fidalga",
        "sitio_base": "https://www.fidalga.com",
        "archivo": "shopify_categorias_www_fidalga_com.json",
    },
    {
        "cadena": "Farmacorp",
        "sitio_base": "https://farmacorp.com",
        "archivo": "shopify_categorias_farmacorp_com.json",
    },
    {
        "cadena": "Amarket",
        "sitio_base": "https://amarket.com.bo",
        "archivo": "shopify_categorias_www_amarket_com_bo.json",
    }
]

nuevas_entradas_shopify = []

for item in mapeos_shopify:
    if not os.path.exists(item["archivo"]):
        print(f"Aviso: No se encontró el archivo {item['archivo']}, se omite.")
        continue
        
    with open(item["archivo"], "r", encoding="utf-8") as af:
        datos = json.load(af)
    
    colecciones_config = []
    for d in datos:
        # Extraer handle o slug de la URL
        url = d.get("url", "")
        handle = d.get("handle") or (url.rstrip("/").split("/")[-1] if url else "")
        if not handle:
            continue
            
        colecciones_config.append({
            "categoria": d.get("categoria", ""),
            "subcategoria": d.get("subcategoria", d.get("titulo", "")),
            "handle": handle
        })

    nuevas_entradas_shopify.append({
        "cadena": item["cadena"],
        "sitio_base": item["sitio_base"],
        "colecciones": colecciones_config if colecciones_config else "todas"
    })

config["shopify"] = nuevas_entradas_shopify

with open(config_path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print("¡config_cadenas.json actualizado con jerarquía de categoría y subcategoría!")
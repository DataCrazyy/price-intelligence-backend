import json

# Cargar el config actual
with open("config_cadenas.json", "r", encoding="utf-8") as f:
    config = json.load(f)

# Cargar las URLs descubiertas
with open("hipermaxi_categorias_descubiertas.json", "r", encoding="utf-8") as f:
    descubiertas = json.load(f)

urls_hipermaxi = [item["url"] for item in descubiertas if "url" in item]

# Actualizar o insertar la sección de Hipermaxi dentro de 'navegador'
if "navegador" not in config:
    config["navegador"] = []

# Buscar si ya existe Hipermaxi para actualizarlo, o crearlo si no está
encontrado = False
for item in config["navegador"]:
    if item.get("cadena") == "Hipermaxi":
        item["urls"] = urls_hipermaxi
        encontrado = True
        break

if not encontrado:
    config["navegador"].append({
        "cadena": "Hipermaxi",
        "script": "scraper_hipermaxi.py",
        "urls": urls_hipermaxi
    })

# Guardar los cambios limpios en config_cadenas.json
with open("config_cadenas.json", "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print("¡config_cadenas.json actualizado con las URLs de Hipermaxi exitosamente!")
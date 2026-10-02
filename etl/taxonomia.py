"""
Categorías de la página: una lista fija de dos niveles (categoría > subcategoría).

Es la ÚNICA fuente de verdad: clasificar.py le pide a la IA que elija solo
de esta lista, publicar_web.py la usa para ordenar y contar, y la página
muestra las categorías en este orden.

Para cambiarla:
  - Renombrar o agregar una subcategoría: editá acá y corré
    `python clasificar.py --rehacer-cat "<Categoría>"` para reclasificar
    solo esa categoría.
  - Las correcciones a mano van en la tabla producto_categoria con
    fuente = 'manual' (la IA nunca las pisa).
"""

OTROS = "Otros"

# Orden = orden en la página. Cada subcategoría lleva una pista corta que
# se le pasa a la IA para que no dude (no se muestra en la página).
TAXONOMIA = {
    "Lácteos y Huevos": {
        "Leche": "leche líquida entera, descremada, deslactosada, saborizada (no en polvo)",
        "Leche en polvo": "leche en polvo para adultos o toda la familia (no fórmula infantil)",
        "Leche condensada y evaporada": "",
        "Yogurt": "yogurt, kefir, bebible o batido",
        "Quesos": "",
        "Mantequilla y margarina": "",
        "Crema de leche": "crema para cocinar o batir, dulce de leche, manjar",
        "Helados y postres": "helados, gelatinas listas, flanes listos",
        "Huevos": "",
        "Bebidas vegetales": "leche de soya, almendra, avena, coco",
    },
    "Despensa": {
        "Arroz, granos y legumbres": "arroz, porotos, lentejas, quinua, maíz",
        "Fideos y pastas": "",
        "Aceites": "aceite de cocina, oliva",
        "Azúcar y endulzantes": "azúcar, edulcorantes, stevia",
        "Harinas y repostería": "harina, polvo de hornear, mezclas para torta, gelatina en polvo, chispas",
        "Enlatados y conservas": "atún, sardinas, choclo, duraznos en lata, aceitunas",
        "Salsas y condimentos": "mayonesa, ketchup, mostaza, sal, especias, vinagre, puré de tomate",
        "Sopas y caldos": "caldos en cubo, sopas instantáneas",
        "Cereales y avena": "cereales de desayuno, avena, granola",
        "Café": "café molido, instantáneo, cápsulas",
        "Té e infusiones": "té, mate, trimate, manzanilla, infusiones en sobres o saquitos (aunque la cadena los ponga en Bebidas)",
        "Untables y mermeladas": "mermelada, miel, mantequilla de maní, cremas de cacao",
    },
    "Snacks y Dulces": {
        "Galletas": "",
        "Chocolates": "",
        "Golosinas": "caramelos, gomitas, chicles, chupetines",
        "Snacks salados": "papas fritas, nachos, palitos, pipocas",
        "Frutos secos": "maní, almendras, castañas, pasas",
    },
    "Frescos y Congelados": {
        "Carnes": "res, cerdo, cordero",
        "Pollo": "",
        "Pescados y mariscos": "",
        "Fiambres y embutidos": "jamón, salchichas, chorizo, mortadela",
        "Frutas y verduras": "",
        "Panadería": "pan, pan de molde, tortas y queques listos, cuñapé",
        "Congelados y listos": "nuggets, papas congeladas, pizzas, comida lista",
    },
    "Bebidas": {
        "Agua": "",
        "Gaseosas": "",
        "Jugos y refrescos": "jugos, néctares, refrescos listos",
        "Energizantes e isotónicas": "",
        "Polvos para preparar": "jugos en polvo, chocolate en polvo para leche",
        "Cerveza": "",
        "Vinos y espumantes": "solo vino, sangría, espumante y sidra (whisky, ron o singani NO van acá)",
        "Licores y destilados": "whisky, singani, ron, vodka, gin, tequila, fernet, licores, aperitivos",
    },
    "Limpieza": {
        "Ropa": "detergente, suavizante, quitamanchas, jabón de lavar",
        "Lavavajillas": "",
        "Limpiadores y desinfectantes": "lavandina, limpiapisos, limpiavidrios, desinfectantes, destapacañerías",
        "Papel y desechables": "papel higiénico, servilletas, toallas de papel, vasos y platos descartables, film, papel aluminio",
        "Bolsas de basura": "",
        "Insecticidas y aromatizantes": "",
        "Esponjas, paños y escobas": "esponjas, guantes de limpieza, trapeadores, escobas",
    },
    "Hogar y Bazar": {
        "Cocina y mesa": "ollas, sartenes, vajilla, vasos, cubiertos, táperes (no descartables)",
        "Organización": "cajas, canastos, organizadores plásticos",
        "Electro y tecnología": "pequeños electrodomésticos, pilas, cables, focos",
        "Ferretería y auto": "",
        "Fiestas y regalos": "globos, velas, decoración, regalos",
        "Ropa y textiles": "",
        "Útiles escolares y oficina": "cuadernos, lápices, colores, mochilas, papel bond, archivadores",
        "Juguetes": "juguetes, juegos, masas para modelar, globos de juego",
    },
    "Cuidado Personal": {
        "Shampoo y acondicionador": "",
        "Tintes y peinado": "tintes, gel, cera, cremas para peinar, tratamientos capilares",
        "Jabón y gel de ducha": "",
        "Desodorante": "",
        "Cuidado bucal": "pasta dental, cepillos, enjuague, hilo dental",
        "Afeitado y depilación": "",
        "Protección femenina": "toallas higiénicas, tampones, protectores diarios",
        "Cremas corporales": "cremas y lociones para el cuerpo, las manos y los pies",
        "Protector solar": "",
        "Algodón e hisopos": "",
        "Incontinencia adulto": "pañales y protectores para adultos",
    },
    "Belleza": {
        "Maquillaje": "base, polvo, sombras, máscara de pestañas, delineador",
        "Labios": "labiales, brillos, tintes de labios",
        "Uñas": "esmaltes, quitaesmalte",
        "Cuidado facial": "limpiadores, agua micelar, sérums, cremas faciales, mascarillas",
        "Perfumes": "perfumes, colonias, body splash",
        "Accesorios de belleza": "brochas, pinzas, peines, ligas, secadores, planchas",
    },
    "Bebé": {
        "Pañales": "pañales de bebé",
        "Toallitas húmedas": "",
        "Leche infantil": "fórmulas infantiles y leches de crecimiento",
        "Alimentos para bebé": "papillas, compotas, cereales infantiles",
        "Higiene del bebé": "shampoo, colonia, cremas y talco de bebé",
        "Mamaderas y accesorios": "mamaderas, tetinas, chupones, vasos",
    },
    "Farmacia": {
        "Dolor y fiebre": "analgésicos, antiinflamatorios, relajantes musculares",
        "Gripe, tos y alergia": "antigripales, jarabes para la tos, antialérgicos, descongestionantes",
        "Digestivo": "antiácidos, laxantes, antidiarreicos, antiparasitarios",
        "Antibióticos": "antibióticos, antivirales, antimicóticos",
        "Corazón, presión y diabetes": "",
        "Piel": "cremas y ungüentos medicados, cicatrizantes, antimicóticos de piel",
        "Ojos y oídos": "colirios, gotas para oídos, lágrimas artificiales",
        "Salud sexual": "preservativos, anticonceptivos, lubricantes, pruebas de embarazo",
        "Sistema nervioso": "antidepresivos, ansiolíticos, para dormir, anticonvulsivos",
        "Otros medicamentos": "medicamentos que no entran en las anteriores",
        "Primeros auxilios": "alcohol, agua oxigenada, gasas, curitas, vendas, termómetros",
        "Insumos médicos y ortopedia": "jeringas, suturas, sondas, guantes, fajas, tensiómetros",
    },
    "Vitaminas y Suplementos": {
        "Multivitamínicos": "",
        "Vitaminas y minerales": "vitamina C, D, B, magnesio, hierro, zinc, colágeno",
        "Proteínas y deporte": "proteínas, creatina, pre entreno",
        "Naturales": "productos naturales y de hierbas, infusiones medicinales",
        "Nutrición especial": "Ensure, PediaSure, complementos nutricionales",
    },
    "Mascotas": {
        "Alimento para perros": "",
        "Alimento para gatos": "",
        "Accesorios e higiene": "arena, juguetes, collares, shampoo, antipulgas",
    },
    OTROS: {
        "Otros": "tabaco, vapes, tarjetas telefónicas y todo lo que no entra en otra categoría",
    },
}

# Reglas de frontera: cada producto tiene UN solo lugar. Van tal cual a la IA.
REGLAS = [
    "Clasificá por lo que ES el producto, no por su sabor, aroma o ingrediente: \"Leche Corporal de Pepino\" es Cuidado Personal > Cremas corporales; \"Shampoo con leche de coco\" es Shampoo; \"Galleta de leche\" es Galletas.",
    "\"Pan\" es Frescos y Congelados > Panadería; \"Tulipán\" es una marca. Leé el nombre completo antes de decidir.",
    "Perfumes, colonias y body splash: Belleza > Perfumes. Cremas para el cuerpo: Cuidado Personal > Cremas corporales.",
    "Todo lo de la cara (agua micelar, sérums, cremas faciales, limpiadores, mascarillas): Belleza > Cuidado facial. Protector solar (cara o cuerpo): Cuidado Personal > Protector solar.",
    "Productos para bebé (shampoo, colonia, crema, talco, fórmulas, papillas): siempre Bebé, nunca Cuidado Personal ni Lácteos. Leches de crecimiento 1+ y fórmulas: Bebé > Leche infantil.",
    "Pañales y protectores para adultos: Cuidado Personal > Incontinencia adulto. Pañales de bebé: Bebé > Pañales.",
    "Alcohol, agua oxigenada, gasas, curitas, vendas, termómetros: Farmacia > Primeros auxilios. Jeringas, suturas, sondas, guantes médicos, fajas, ortopedia: Farmacia > Insumos médicos y ortopedia.",
    "Cualquier medicamento (con dosis en mg, jarabe, ampolla, comprimido, tableta) va en Farmacia aunque la cadena lo ponga en una promoción. Vitaminas, minerales, colágeno, proteínas y Ensure/PediaSure van en Vitaminas y Suplementos aunque tengan dosis en mg.",
    "Papel higiénico, servilletas, toallas de papel, descartables, film y papel aluminio: Limpieza > Papel y desechables. Ollas, vajilla y táperes: Hogar y Bazar > Cocina y mesa.",
    "Té, mate, café e infusiones en sobres o para preparar: Despensa (Té e infusiones / Café), aunque la cadena los ponga en Bebidas. En Bebidas solo va lo que se toma listo o se mezcla para jugo.",
    "Bebidas alcohólicas por tipo: cerveza en Cerveza; vino, sangría, espumante y sidra en Vinos y espumantes; whisky, singani, ron, vodka, gin, tequila, fernet y licores en Licores y destilados.",
    "Útiles escolares, oficina y juguetes: Hogar y Bazar. Accesorios y alimento de mascotas: Mascotas.",
    "Packs o combos: clasificalos por el producto principal (el primero que nombra).",
    "Si dudás entre dos subcategorías de la misma categoría, elegí la más específica. \"Otros > Otros\" solo si de verdad no entra en ninguna (tabaco, vapes, tarjetas telefónicas).",
]

CATEGORIAS = list(TAXONOMIA)
PARES = [(c, s) for c, subs in TAXONOMIA.items() for s in subs]          # todas las combinaciones válidas
ETIQUETAS = [f"{c} > {s}" for c, s in PARES]                             # lo que elige la IA


def valida(cat, sub):
    return cat in TAXONOMIA and sub in TAXONOMIA[cat]


# Mientras un producto no esté clasificado por la IA se usa la regla vieja
# (categorias.js) traducida a las categorías nuevas, sin subcategoría.
DESDE_REGLA = {
    "Salud y Medicamentos": "Farmacia",
    "Vitaminas y Suplementos": "Vitaminas y Suplementos",
    "Cuidado Personal": "Cuidado Personal",
    "Belleza": "Belleza",
    "Alimentos y Abarrotes": "Despensa",
    "Supermercado": "Despensa",
    "Lácteos": "Lácteos y Huevos",
    "Bebidas": "Bebidas",
    "Hogar y Limpieza": "Limpieza",
    "Bebé y Familia": "Bebé",
    "Escolar e Infantil": "Hogar y Bazar",
    "Mascotas": "Mascotas",
}

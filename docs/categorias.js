// categorias.js — Agrupa las ~630 etiquetas de categoría/subcategoría
// crudas (muchas son nombres de campaña de marketing tipo "Tiktok Sale
// Hasta 60 Off" o "Zona Papa", no categorías de producto reales) en un
// puñado de categorías "macro" para mostrar en los filtros.
//
// UNA SOLA fuente de verdad para index.html y dashboard.html -- las dos
// páginas cargan este archivo (<script src="categorias.js">) en vez de
// tener cada una su propia lógica de agrupación, así nunca se desincronizan.
//
// Basado en datos reales: 986 combinaciones cadena/categoría exportadas de
// Postgres (11/09/2026). Con 73 etiquetas ya se cubre el 90% del catálogo;
// el resto (418 etiquetas, <2% del catálogo, casi todas de Farmacorp/
// Amarket) son campañas puntuales -- van al cajón "Otros / Promociones".
//
// Si el scraper de Shopify llega a capturar el campo real `product_type`
// de cada producto en vez de caer al nombre de la colección cuando falta
// categoría, esto se vuelve casi innecesario para Farmacorp/Amarket (que
// es de donde sale casi todo el ruido) -- pendiente de revisar.

(function (global) {
    'use strict';

    // clave EXACTA (normalizada: sin acentos, mayúsculas, espacios simples)
    // -> categoría macro. Cubre las ~75 etiquetas reales de mayor volumen.
    var MAPA_EXACTO = {
        // Salud y Medicamentos
        'SALUD Y MEDICAMENTOS': 'Salud y Medicamentos',
        'INSUMOS MEDICOS': 'Salud y Medicamentos',
        'SALUD Y BIENESTAR': 'Salud y Medicamentos',
        'SALUD Y PROTECCION PARA TODOS': 'Salud y Medicamentos',
        'SCHMIDTS PHARMA SRL': 'Salud y Medicamentos',
        'SEMANA DEL CORAZON': 'Salud y Medicamentos',
        'SEMANA SALUD Y BIENESTAR': 'Salud y Medicamentos',
        'VACUNATE Y CUIDATE': 'Salud y Medicamentos',

        // Vitaminas y Suplementos
        'SUPLEMENTOS Y VITAMINAS': 'Vitaminas y Suplementos',
        'VITAMINAS Y SUPLEMENTOS': 'Vitaminas y Suplementos',
        'VITAMINAS SUPLEMENTOS Y MINERALES': 'Vitaminas y Suplementos',
        'VITAMINAZO CBBA': 'Vitaminas y Suplementos',
        'NUTRICION SALUDABLE': 'Vitaminas y Suplementos',
        'NUTRICION INFANTIL': 'Vitaminas y Suplementos',

        // Cuidado Personal (higiene/skincare) -- separado de Belleza (maquillaje)
        'CUIDADO PERSONAL': 'Cuidado Personal',
        'SALUD Y BELLEZA': 'Cuidado Personal',
        'SKINCARE': 'Cuidado Personal',
        'CUIDADO DE LA PIEL': 'Cuidado Personal',
        'HIGIENE PERSONAL': 'Cuidado Personal',
        'CUIDADO E HIGIENE': 'Cuidado Personal',
        'SELF CARE': 'Cuidado Personal',
        'SALUD SEXUAL': 'Cuidado Personal',
        // Confirmado con el cliente: campaña de verano (protector solar,
        // cuidado de piel), no vitaminas.
        'VERANOL': 'Cuidado Personal',
        'VERANANGO': 'Cuidado Personal',

        'BELLEZA': 'Belleza',
        'MI SALON DE BELLEZA': 'Belleza',
        'BELLEZA FACIAL': 'Belleza',
        'MIRADA PERFECTA': 'Belleza',
        'BEAUTY': 'Belleza',
        'VOGUE': 'Belleza',
        'SEMANA BELLEZA Y CUIDADO PERSONAL': 'Belleza',
        'RENOVA TU RUTINA': 'Belleza',
        'HAIR CARE VERANO': 'Belleza',
        'SHAMPOO': 'Belleza',
        'TOCOBO': 'Belleza',

        // Alimentos y Abarrotes
        'ABARROTES': 'Alimentos y Abarrotes',
        'DESPENSA': 'Alimentos y Abarrotes',
        'SALSAS SAZONADORES Y ADEREZOS': 'Alimentos y Abarrotes',
        'PANADERIA Y REPOSTERIA': 'Alimentos y Abarrotes',
        'REPOSTERIA': 'Alimentos y Abarrotes',
        'SNACK ATTACK': 'Alimentos y Abarrotes',
        'SAN REMO': 'Alimentos y Abarrotes',
        'FOODIES': 'Alimentos y Abarrotes',
        'ACEITES Y VINAGRES': 'Alimentos y Abarrotes',
        'ACEITE': 'Alimentos y Abarrotes',
        'ACEITE DE SOYA': 'Alimentos y Abarrotes',
        'ENLATADOS Y ENVASADOS': 'Alimentos y Abarrotes',
        'ACEITUNAS': 'Alimentos y Abarrotes',
        'ACEITUNA': 'Alimentos y Abarrotes',
        'VERDURAS': 'Alimentos y Abarrotes',
        'VEGANIS': 'Alimentos y Abarrotes',

        // Lácteos
        'LACTEOS Y DERIVADOS': 'Lácteos',
        'OTROS LACTEOS': 'Lácteos',
        'YOGURT': 'Lácteos',
        'HELADOS': 'Lácteos',

        // Bebidas
        'BEBIDAS': 'Bebidas',
        'BEBIDAS Y LICORES': 'Bebidas',
        'VINOS Y ESPUMANTES': 'Bebidas',
        'ENERGIZANTE': 'Bebidas',
        'BEBIDA ISOTONICA': 'Bebidas',

        // Hogar y Limpieza
        'CUIDADO DEL HOGAR': 'Hogar y Limpieza',
        'LIMPIEZA DEL HOGAR': 'Hogar y Limpieza',
        'MI HOGAR': 'Hogar y Limpieza',
        'HOGAR': 'Hogar y Limpieza',
        'ELECTRO HOGAR': 'Hogar y Limpieza',
        'PLASTICOS Y SIMILARES': 'Hogar y Limpieza',
        'DETERGENTES Y JABONES': 'Hogar y Limpieza',
        'ACCESORIOS DE LIMPIEZA': 'Hogar y Limpieza',
        'MENAJE Y COCINA': 'Hogar y Limpieza',

        // Bebé y Familia (incluye "Zona Papa": confirmado con el cliente
        // que es la sección de Día del Padre / regalos, no verdulería)
        'CUIDADO DEL BEBE': 'Bebé y Familia',
        'TOALLAS HUMEDAS Y PANALES': 'Bebé y Familia',
        'PANALES Y TOALLAS HUMEDAS': 'Bebé y Familia',
        'LECHES Y FORMULA': 'Bebé y Familia',
        'LECHES Y FORMULAS': 'Bebé y Familia',
        'LECHES': 'Bebé y Familia',
        'TIEMPO EN FAMILIA': 'Bebé y Familia',
        'ZONA MAMA': 'Bebé y Familia',
        'ZONA PAPA': 'Bebé y Familia',

        // Escolar e Infantil
        'ZONA ESCOLAR': 'Escolar e Infantil',
        'VOLVAMOS A CLASES': 'Escolar e Infantil',
        'SEMANA INFANTIL': 'Escolar e Infantil',

        // Supermercado / Marca Propia
        'SUPERMERCADO': 'Supermercado',
        'NUESTRAS MARCAS': 'Supermercado',

        'MASCOTAS': 'Mascotas',
    };

    // Palabras clave (substring, sobre el texto ya normalizado) para
    // clasificar automáticamente etiquetas de la cola larga que no están
    // en MAPA_EXACTO pero sí traen una señal clara en el nombre -- cubre
    // buena parte de las categorías finitas de Fidalga (Gaseosa, Nectar de
    // Frutas, Jugo de Fruta, Lavandina, etc.) sin tener que listarlas todas
    // a mano. Se evalúa en orden -- la primera que matchea gana.
    var PALABRAS_CLAVE = [
        [/SALUD|MEDIC|FARMA/, 'Salud y Medicamentos'],
        [/VITAMIN|SUPLEMENT/, 'Vitaminas y Suplementos'],
        [/SKIN|MAQUILLA|PERFUM|BELLEZA FACIAL|COSMETIC/, 'Belleza'],
        [/HIGIENE|CUIDADO PERSONAL|DESODORANTE/, 'Cuidado Personal'],
        [/LACTEO|YOGURT|QUESO|LECHE/, 'Lácteos'],
        [/BEBIDA|GASEOSA|JUGO|NECTAR|VINO|CERVEZA|ENERGIZANTE|ISOTONIC/, 'Bebidas'],
        [/LIMPIEZA|DETERGENTE|LAVANDINA|JABON DE ROPA|DESINFECTANTE/, 'Hogar y Limpieza'],
        [/BEBE\b|PANAL|FORMULA INFANTIL/, 'Bebé y Familia'],
        [/ESCOLAR|UTILES|VOLVAMOS A CLASES/, 'Escolar e Infantil'],
        [/MASCOTA|PERRO|GATO/, 'Mascotas'],
        [/ABARROTE|DESPENSA|SNACK|REPOSTERIA|PANADERIA|SALSA|CONDIMENTO|MAYONESA|NACHO|GELATINA|ENLATADO|ACEITE|ACEITUNA|VERDURA/, 'Alimentos y Abarrotes'],
    ];

    var CATEGORIA_OTROS = 'Otros / Promociones';

    function normalizar(texto) {
        return String(texto || '')
            .normalize('NFD').replace(/[̀-ͯ]/g, '') // sin acentos
            .toUpperCase().replace(/\s+/g, ' ').trim();
    }

    function macroCategoria(etiquetaCruda) {
        if (!etiquetaCruda) return CATEGORIA_OTROS;
        var clave = normalizar(etiquetaCruda);
        if (MAPA_EXACTO[clave]) return MAPA_EXACTO[clave];
        for (var i = 0; i < PALABRAS_CLAVE.length; i++) {
            if (PALABRAS_CLAVE[i][0].test(clave)) return PALABRAS_CLAVE[i][1];
        }
        return CATEGORIA_OTROS;
    }

    global.CategoriasMacro = { macroCategoria: macroCategoria, CATEGORIA_OTROS: CATEGORIA_OTROS };
})(window);

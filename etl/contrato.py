"""
contrato.py — El "contrato único" del agente de scraping (Fase 1 del blueprint).

Esto NO cambia CÓMO se scrapea cada cadena: Hipermaxi y Farmacias Chávez
siguen usando Playwright (sitios sin JSON público, con scroll infinito o
paginación real); Fidalga, Amarket y Farmacorp siguen pegando directo al
endpoint público /products.json de Shopify (gratis, sin navegador, sin
tokens). Nada de eso cambia.

Lo que unifica es el ENVOLTORIO de entrada/salida. Antes cada scraper
escribía un .xlsx con columnas sueltas (Articulo, CADENA, COD_ARTICULO...)
que etl.py adivinaba por nombre con COLUMN_ALIASES -- funcionaba, pero
dependía de un vaivén por Excel/pandas que ya nos mordió una vez (el bug
real: escribir "" en una celda y releerla con pd.read_excel() la
convierte en NaN, lo que dejó la categoría de Farmacorp en NULL).

Ahora cada scraper expone una única función `ejecutar(request) -> ScrapeResult`
que devuelve productos ya tipados y validados por Pydantic -- si falta un
campo obligatorio o un precio viene negativo, se rechaza ahí mismo, antes
de llegar a Postgres. Esto es lo que te deja enchufar después, sin
reescribir cada scraper: un endpoint FastAPI (Fase 1), un agente de
matching con fallback a Claude (Fase 2), o un dashboard multi-tenant
(Fase 3) -- todos hablan el mismo idioma.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ScrapeRequest(BaseModel):
    """Entrada: qué scrapear. Misma forma para Shopify o Playwright --
    la URL puede ser una colección Shopify o una URL de categoría de un
    sitio con navegador; cada scraper sabe qué hacer con la suya."""
    cadena: str
    url: str
    categoria_default: Optional[str] = None  # fallback si el sitio no trae categoría propia
    ciudad: str = "Santa Cruz"


class ProductoScrapeado(BaseModel):
    """Salida: una fila de producto, ya validada y tipada -- este es el
    contrato que reemplaza a las columnas sueltas del Excel."""
    nombre: str
    precio_oferta: float
    precio_regular: float
    cadena: str
    codigo_articulo: str
    categoria: str
    subcategoria: Optional[str] = None
    ciudad: str = "Santa Cruz"
    sucursal: Optional[str] = None
    url: Optional[str] = None
    imagen: Optional[str] = None

    @field_validator("precio_oferta", "precio_regular")
    @classmethod
    def precio_no_negativo(cls, v):
        if v < 0:
            raise ValueError("el precio no puede ser negativo")
        return v

    @field_validator("nombre", "codigo_articulo", "cadena")
    @classmethod
    def no_vacio(cls, v):
        if not v or not str(v).strip():
            raise ValueError("no puede estar vacío")
        return v


class ScrapeResult(BaseModel):
    """Lo que devuelve CUALQUIER scraper, sin importar si por dentro usa
    Playwright o el JSON de Shopify -- este es el 'contrato de agente
    único' del blueprint. errores: filas que no pasaron la validación
    (se saltean, no tumban todo el scrape)."""
    cadena: str
    fuente: str  # "shopify_json" | "playwright"
    productos: list[ProductoScrapeado]
    scrapeado_en: datetime = Field(default_factory=datetime.now)
    errores: list[str] = Field(default_factory=list)

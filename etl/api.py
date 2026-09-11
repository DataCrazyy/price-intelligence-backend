"""
api.py — FastAPI mínimo (Fase 1 del blueprint: "Postgres + FastAPI
mínimo, sin agentes LLM todavía").

Reemplaza el paso manual "corré export_json.py cada vez que querés
refrescar el frontend" por un endpoint. Deliberadamente mínimo: no
dispara scraping (eso lo sigue haciendo run_all.py / panel.py) -- Fase 1
es solo Postgres + un endpoint que sirve/regenera precios.json.

Uso:
    pip install fastapi "uvicorn[standard]"
    uvicorn api:app --reload --port 8000

Endpoints internos (sin autenticar -- para tu propio frontend/herramientas,
pensados para correr sólo en tu máquina o tu red):
    GET  /health          -> chequeo simple
    GET  /precios.json    -> sirve el archivo actual tal cual está en disco
    POST /export          -> lo regenera desde Postgres (mismo export_json.py de siempre) y lo sirve

Endpoints de cliente (Fase 3 -- requieren header `X-API-Key`, ver
gestionar_clientes.py para crear clientes/keys):
    GET  /v1/precios       -> mismo contenido que precios.json, pero autenticado y con rate limit por cliente
"""
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import export_json
import generar_reporte
from auth import requerir_api_key

EXPORT_PATH = Path(__file__).parent.parent.parent / "data" / "precios.json"

app = FastAPI(
    title="Price Intelligence API",
    version="0.2.0",
    description="Fase 1: sirve/regenera precios.json desde Postgres. Fase 3: acceso autenticado por cliente, con rate limit.",
)

# El dashboard (dashboard.html, abierto con Live Server en otro puerto) le
# pega a esta API desde el navegador -- sin CORS el botón "Generar reporte"
# se rompe silenciosamente. Esto corre solo en la máquina de David, así que
# abrir a cualquier origen local no es un riesgo real.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/reporte")
def generar_reporte_pdf(categoria: str | None = None, cadenas: str | None = None):
    """Genera (al toque, no async) el reporte PDF ejecutivo y lo devuelve
    para que el dashboard lo muestre en un iframe -- mismo generar_reporte.py
    que se corre a mano por consola, acá solo se lo envuelve en un endpoint.
    `categoria`: nombre exacto de categoría, o vacío para todas.
    `cadenas`: cadenas separadas por coma, o vacío para todas las disponibles.
    """
    cadenas_arg = [c.strip() for c in cadenas.split(",") if c.strip()] if cadenas else None
    salida_nombre = f"reporte_dashboard_{date.today().isoformat()}.pdf"
    try:
        ruta = generar_reporte.generar(categoria, cadenas_arg, salida_nombre)
    except SystemExit as e:
        # generar_reporte.generar() usa sys.exit() para errores de datos
        # (ej. sin match para esa combinación) -- acá se traducen a 400.
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Error generando el reporte: {e}")
    return FileResponse(ruta, media_type="application/pdf", filename=ruta.name)


@app.get("/precios.json")
def obtener_precios():
    """Sirve el archivo tal cual está en disco -- no toca Postgres. Para
    refrescarlo primero, usar POST /export."""
    if not EXPORT_PATH.exists():
        raise HTTPException(404, "Todavía no existe precios.json -- corré POST /export primero.")
    return FileResponse(EXPORT_PATH, media_type="application/json")


@app.post("/export")
def regenerar_precios():
    """Regenera data/precios.json desde Postgres (equivalente a correr
    `python export_json.py` a mano) y devuelve cuántos listados salieron."""
    try:
        n = export_json.export_json(str(EXPORT_PATH))
    except Exception as e:
        raise HTTPException(500, f"Error exportando: {e}")
    return {"exportado": True, "listados_exportados": n, "archivo": str(EXPORT_PATH)}


@app.get("/v1/precios")
def obtener_precios_autenticado(cliente: dict = Depends(requerir_api_key)):
    """Igual que GET /precios.json, pero para clientes externos: exige
    `X-API-Key` válida y cuenta contra el rate limit de ese cliente
    (ver auth.py). Por ahora devuelve el dataset completo -- filtrar por
    plan/scopes (ej. sólo ciertas cadenas o categorías) es la próxima
    vuelta de tuerca de Fase 3, cuando haga falta para un cliente real."""
    if not EXPORT_PATH.exists():
        raise HTTPException(404, "Todavía no existe precios.json -- corré POST /export primero.")
    return FileResponse(EXPORT_PATH, media_type="application/json")

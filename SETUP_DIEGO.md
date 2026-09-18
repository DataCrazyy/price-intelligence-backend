# Price Intelligence — Setup desde cero (Diego)

Guía para levantar el backend en tu máquina, partiendo del zip que te compartió David.

**Actualización importante:** la base de datos ya NO es local — ahora es una base compartida en Supabase (proyecto `price-intelligence`, org PrometeoTech). Vos y David trabajan sobre los mismos datos en tiempo real. Esto simplifica bastante el setup: **no hace falta instalar Docker ni Postgres**, ni correr ninguna migración — ya están aplicadas en la base compartida.

## 0. Antes de arrancar

**Importante sobre el `.env`:** si el zip incluyó el archivo `.env` (no debería, está en `.gitignore`, pero un zip de carpeta completa sí lo agarra), ese archivo trae:
- El `DATABASE_URL` de Supabase — es la base **compartida real**, cualquier cosa que escribas ahí la ve David también (y viceversa). Tené cuidado con scripts que borren datos.
- Una **API key real de OpenAI**.

No compartas ese `.env` fuera de este proyecto. Si tenés dudas sobre si venía en el zip, preguntale a David.

## 1. Requisitos previos

Solo necesitás:
1. **Python 3.10 o 3.11** — https://www.python.org/downloads/ (en el instalador de Windows, tildá "Add python.exe to PATH").
2. **Git** (opcional si ya tenés el zip) — https://git-scm.com/downloads

Verificá:
```powershell
python --version
```

(Docker es **opcional** — solo si en algún momento querés un cliente de base de datos local en vez del dashboard de Supabase. Ver nota al final.)

## 2. Descomprimir y configurar variables de entorno

Descomprimí el zip donde prefieras, por ejemplo `C:\Proyectos\Price_Intelligence\`. Abrí la carpeta en VS Code.

Si tu `.env` **no** vino en el zip, pedile a David el `DATABASE_URL` de Supabase (no lo inventes, tiene que ser el mismo para que compartan datos) y armá `postgres_migration/.env`:
```
DATABASE_URL=postgresql://postgres:<password-real>@db.dxdmstnkgqpndbbqkwct.supabase.co:5432/postgres

OPENAI_API_KEY=tu-propia-api-key-de-openai
```
La `OPENAI_API_KEY` la necesita `matching_llm.py` para el matching ambiguo entre cadenas. Si no tenés una, generá una en https://platform.openai.com/api-keys — el proyecto funciona igual sin ella, pero ese fallback no se activa.

## 3. Entorno virtual de Python

Desde `postgres_migration/`:
```powershell
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```
(`playwright install chromium` es necesario porque el scraper de Hipermaxi navega con un navegador real.)

## 4. Correr los servicios

Con la venv activada, dos terminales separadas:

**Panel de administración** (arma corridas de scraping, ves categorías y logs en vivo):
```powershell
cd etl
python panel.py
```
→ http://localhost:5050

**API** (sirve/regenera `precios.json` desde la base compartida):
```powershell
cd etl
uvicorn api:app --reload --port 8000
```
→ http://localhost:8000/health

**Scrapers manuales**:
```powershell
cd etl
python run_all.py
python run_all.py --solo Chavez   # una sola cadena
```

## 5. Ver los datos

Ya no hace falta Adminer local — entrá directo al dashboard de Supabase (te pasa David el link de invitación al proyecto si no la tenés) → **Table Editor** para ver filas tipo Excel, o **SQL Editor** para consultas.

## 6. Verificación rápida

1. Con la venv activada, `python -c "import psycopg2, os, dotenv; dotenv.load_dotenv(); import psycopg2; c=psycopg2.connect(os.environ['DATABASE_URL']); print('conexión OK')"` desde `postgres_migration/` — si imprime "conexión OK", estás conectado a la base compartida.
2. Panel (`:5050`) carga y muestra las cadenas configuradas en `etl/config_cadenas.json`.
3. `python run_all.py --solo Chavez` corre sin errores y deja precios nuevos — si después lo ve David en Supabase, están compartiendo datos correctamente.

## 7. Dudas / problemas comunes

- **`ModuleNotFoundError`** → la venv no está activada, o faltó `pip install -r requirements.txt`.
- **Error de conexión a la base** → revisá que el `DATABASE_URL` en tu `.env` sea exactamente el mismo que el de David (mismo host, mismo password), y que no tengas espacios de más al copiarlo.
- **(Opcional) Adminer local**: si igual querés un cliente de DB local además del dashboard de Supabase, `docker compose up -d` levanta Adminer en `localhost:8080` — necesitás Docker Desktop instalado solo para esto, y como login usás el mismo `DATABASE_URL` (servidor = lo que sigue a la `@`, ej. `db.dxdmstnkgqpndbbqkwct.supabase.co`).

Cualquier cosa, David tiene el resto del contexto (arquitectura completa, pendientes técnicos, roadmap a producción) en el documento de traspaso que ya le compartió.

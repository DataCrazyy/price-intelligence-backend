"""
visibilidad.py — Cadenas que NO se muestran en lo público (web, precios.json).

La lista vive en config_cadenas.json, clave "ocultas_en_web", por ejemplo:
    "ocultas_en_web": ["Farmacias Chavez"]

Una cadena oculta se puede seguir scrapeando y matcheando internamente (queda en
la base y en el panel), pero no sale en:
  - la web (publicar_web.py: sus precios, fotos y nombre no se publican),
  - data/precios.json (export_json.py),
y por lo tanto tampoco en la lista de compras con IA, que lee lo publicado.
Para volver a mostrarla: sacarla de la lista y publicar de nuevo.
"""
import json
import unicodedata
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config_cadenas.json"


def _norm(nombre: str) -> str:
    t = unicodedata.normalize("NFKD", str(nombre or "")).lower().strip()
    return "".join(c for c in t if not unicodedata.combining(c))


def cadenas_ocultas() -> set[str]:
    """Nombres normalizados (minúsculas, sin acentos) de las cadenas ocultas."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {_norm(n) for n in cfg.get("ocultas_en_web", []) if str(n).strip()}


def oculta(nombre: str, ocultas: set[str] | None = None) -> bool:
    ocultas = cadenas_ocultas() if ocultas is None else ocultas
    n = _norm(nombre)
    # "Chavez" y "Farmacias Chavez" son la misma cadena
    return any(n == o or n.endswith(" " + o) or o.endswith(" " + n) for o in ocultas)


def clave_publica(k: str, ocultas: set[str] | None = None) -> str:
    """Los grupos llevan como clave el nombre de la cadena que los originó
    (p. ej. "chavez-27459"). Si esa cadena está oculta, la clave pública se
    neutraliza ("g-27459") para que su nombre no aparezca en lo publicado."""
    ocultas = cadenas_ocultas() if ocultas is None else ocultas
    k = str(k or "")
    pref, sep, resto = k.partition("-")
    if sep and any(pref and (pref == o or o.endswith(" " + pref)) for o in ocultas):
        return "g-" + resto
    return k

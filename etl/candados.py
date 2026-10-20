"""
candados.py — Candados compartidos entre computadoras (tabla `candados` en Supabase).

Para que David y Diego puedan correr el pipeline cada uno en su equipo sin
pisarse. Los candados viven en la base (no en la memoria de un proceso), así
que los ven todas las computadoras:

  cadena:<Nombre>   una cadena se está scrapeando. Si otro equipo la tiene,
                    run_all.py la saltea con un aviso (no la scrapea dos veces).
  procesar          clasificar / matching / export / fotos / publicar. Son pasos
                    que tocan TODA la base: si otro equipo los está corriendo,
                    run_all.py espera a que termine y recién ahí sigue.

Cada candado guarda quién lo tiene (equipo), qué está haciendo (detalle, paso)
y un "latido" que se renueva cada 30 s mientras el proceso vive. Si una
computadora se apaga, se cuelga o la corrida se mata, el candado vence solo a
los 3 minutos sin latido y otro lo puede tomar.

El nombre del equipo sale de la variable PRECIOS_EQUIPO (ponela en el .env,
ej. PRECIOS_EQUIPO=Diego) o, si no está, de usuario@computadora.

Si la base no responde o no se puede crear la tabla, los candados no bloquean
nada: se avisa una vez y se sigue (mejor correr sin candado que no correr).
"""
import atexit
import getpass
import os
import socket
import sys
import threading
import time
import uuid

VENCE_SEG = int(os.environ.get("CANDADO_VENCE_SEG", 180))
LATIDO_SEG = 30

TOKEN = os.environ.get("PRECIOS_CANDADO_TOKEN") or uuid.uuid4().hex

_mios: dict[str, dict] = {}       # nombre -> {"detalle", "paso"}
_lock = threading.Lock()
_hilo = None
_tabla_ok = False
_avisado = False

DDL = [
    """CREATE TABLE IF NOT EXISTS candados (
        nombre     TEXT PRIMARY KEY,
        dueno      TEXT NOT NULL,
        equipo     TEXT NOT NULL,
        detalle    TEXT,
        paso       TEXT,
        tomado_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
        latido_en  TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    "ALTER TABLE candados ENABLE ROW LEVEL SECURITY",
    "REVOKE ALL ON candados FROM anon, authenticated",
]


def equipo() -> str:
    nombre = os.environ.get("PRECIOS_EQUIPO", "").strip()
    if nombre:
        return nombre
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return socket.gethostname()


def _conectar():
    import etl  # import tardío: etl carga el .env y la URL de la base
    conn = etl.get_conn()
    conn.autocommit = True
    return conn


def _avisar_una_vez(e):
    global _avisado
    if not _avisado:
        _avisado = True
        print(f"[candados] AVISO: no se pudieron usar los candados compartidos ({str(e).strip().splitlines()[0]}). "
              "Se sigue sin candado: coordiná a mano con el otro equipo.", file=sys.stderr, flush=True)


def _asegurar_tabla(cur):
    global _tabla_ok
    if _tabla_ok:
        return
    for sql in DDL:
        try:
            cur.execute(sql)
        except Exception:
            pass  # ej. REVOKE en una base sin esos roles: no importa
    _tabla_ok = True


def _iniciar_latido():
    global _hilo
    if _hilo is None:
        _hilo = threading.Thread(target=_latir, daemon=True)
        _hilo.start()


def _latir():
    while True:
        time.sleep(LATIDO_SEG)
        with _lock:
            mios = {k: dict(v) for k, v in _mios.items()}
        if not mios:
            continue
        try:
            conn = _conectar()
            cur = conn.cursor()
            for nombre, d in mios.items():
                cur.execute("UPDATE candados SET latido_en = now(), detalle = %s, paso = %s "
                            "WHERE nombre = %s AND dueno = %s", (d["detalle"], d["paso"], nombre, TOKEN))
            conn.close()
        except Exception:
            pass  # el próximo latido lo reintenta; si se corta 3 min, el candado vence


def _fila_a_info(r) -> dict:
    nombre, eq, detalle, paso, tomado, latido, dueno = r
    return {"nombre": nombre, "equipo": eq, "detalle": detalle, "paso": paso,
            "tomado_en": tomado.isoformat() if tomado else None,
            "latido_en": latido.isoformat() if latido else None,
            "minutos": round((time.time() - tomado.timestamp()) / 60) if tomado else None,
            "dueno": dueno, "propio": dueno == TOKEN, "este_equipo": eq == equipo()}


def tomar(nombre: str, detalle: str = "", paso: str = None):
    """Intenta tomar el candado. Devuelve (True, None) si ahora es nuestro, o
    (False, info) con quién lo tiene."""
    try:
        conn = _conectar()
        cur = conn.cursor()
        _asegurar_tabla(cur)
        cur.execute(
            """
            INSERT INTO candados (nombre, dueno, equipo, detalle, paso) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (nombre) DO UPDATE
               SET dueno = EXCLUDED.dueno, equipo = EXCLUDED.equipo, detalle = EXCLUDED.detalle,
                   paso = EXCLUDED.paso, tomado_en = now(), latido_en = now()
             WHERE candados.dueno = EXCLUDED.dueno
                OR candados.latido_en < now() - make_interval(secs => %s)
            RETURNING dueno
            """,
            (nombre, TOKEN, equipo(), detalle, paso, VENCE_SEG),
        )
        if cur.fetchone():
            conn.close()
            with _lock:
                _mios[nombre] = {"detalle": detalle, "paso": paso}
            _iniciar_latido()
            return True, None
        cur.execute("SELECT nombre, equipo, detalle, paso, tomado_en, latido_en, dueno FROM candados WHERE nombre = %s",
                    (nombre,))
        r = cur.fetchone()
        conn.close()
        return False, (_fila_a_info(r) if r else {"equipo": "otro equipo", "detalle": "", "minutos": None})
    except Exception as e:
        _avisar_una_vez(e)
        return True, None


def esperar(nombre: str, detalle: str = "", paso: str = None, max_seg: int = 90 * 60, avisar=print) -> bool:
    """Toma el candado; si lo tiene otro equipo, espera (avisando cada ~minuto)
    hasta que se libere o venza. Devuelve False si se agotó max_seg."""
    inicio = time.time()
    ultimo_aviso = 0
    while True:
        ok, otro = tomar(nombre, detalle, paso)
        if ok:
            if ultimo_aviso:
                avisar(f"Listo: {otro_txt} terminó. Sigo.")
            return True
        otro_txt = f"{otro['equipo']} ({otro.get('detalle') or 'trabajando'})"
        if time.time() - inicio > max_seg:
            return False
        if time.time() - ultimo_aviso > 55:
            lleva = f" · lleva {otro['minutos']} min" if otro.get("minutos") is not None else ""
            avisar(f"Esperando a que {otro_txt} termine para seguir{lleva} ...")
            ultimo_aviso = time.time()
        time.sleep(15)


def actualizar(detalle: str = None, paso: str = None):
    """Cambia lo que muestran TODOS nuestros candados (se escribe en el próximo latido)."""
    with _lock:
        for d in _mios.values():
            if detalle is not None:
                d["detalle"] = detalle
            if paso is not None:
                d["paso"] = paso


def soltar(nombre: str = None):
    """Libera un candado nuestro (o todos, si nombre es None)."""
    with _lock:
        nombres = [nombre] if nombre else list(_mios)
        for n in nombres:
            _mios.pop(n, None)
    if not nombres:
        return
    try:
        conn = _conectar()
        conn.cursor().execute("DELETE FROM candados WHERE dueno = %s AND nombre = ANY(%s)", (TOKEN, nombres))
        conn.close()
    except Exception:
        pass  # vence solo en VENCE_SEG


def soltar_de(token: str):
    """Libera todos los candados de otro proceso (el panel lo usa al detener una corrida)."""
    try:
        conn = _conectar()
        conn.cursor().execute("DELETE FROM candados WHERE dueno = %s", (token,))
        conn.close()
    except Exception:
        pass


def activos() -> list[dict]:
    """Candados vigentes (con latido reciente) de todos los equipos."""
    conn = _conectar()
    cur = conn.cursor()
    _asegurar_tabla(cur)
    cur.execute("SELECT nombre, equipo, detalle, paso, tomado_en, latido_en, dueno FROM candados "
                "WHERE latido_en >= now() - make_interval(secs => %s) ORDER BY tomado_en", (VENCE_SEG,))
    filas = [_fila_a_info(r) for r in cur.fetchall()]
    conn.close()
    return filas


atexit.register(soltar)

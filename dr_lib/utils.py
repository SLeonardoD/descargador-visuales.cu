"""Utilidades compartidas por el resto de dr_lib: logging con colores,
formateo de tamaños/nombres, y el estado global (sesión HTTP compartida,
bandera de cancelación, nivel de verbosidad)."""
import os
import re
import sys
import threading
import unicodedata

import requests
from tqdm import tqdm

try:
    import colorama
    colorama.just_fix_windows_console()
    _COLORAMA_OK = True
except ImportError:  # colorama es liviano, pero por las dudas no rompemos si falta
    _COLORAMA_OK = False

# ---------------------------------------------------------------------------
# Estado global compartido entre módulos e hilos
# ---------------------------------------------------------------------------
SESSION = requests.Session()

# Bandera de cancelación compartida entre hilos. threading.Event es seguro
# de usar entre hilos por diseño (importante en Python "free-threaded",
# donde ya no hay protección implícita del GIL sobre estructuras compartidas).
CANCELAR = threading.Event()

_nivel_actual = 0  # -1 silencioso, 0 normal, 1+ detallado
_slot_lock = threading.Lock()
_slot_contador = 0
_slot_local = threading.local()

_COLOR_HABILITADO = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

_COLORES = {}
if _COLORAMA_OK:
    _COLORES = {
        "red": colorama.Fore.RED,
        "green": colorama.Fore.GREEN,
        "yellow": colorama.Fore.YELLOW,
        "cyan": colorama.Fore.CYAN,
    }
_RESET = colorama.Style.RESET_ALL if _COLORAMA_OK else ""


def configurar_nivel(v):
    """Fija el nivel de detalle global. Se llama una sola vez en main(),
    ANTES de arrancar los hilos trabajadores (por eso no necesita lock)."""
    global _nivel_actual
    _nivel_actual = v


def log(msg, min_nivel=0, color=None):
    if _nivel_actual >= min_nivel:
        if color and _COLOR_HABILITADO and color in _COLORES:
            msg = f"{_COLORES[color]}{msg}{_RESET}"
        tqdm.write(msg)


def obtener_slot_de_este_hilo(total_slots):
    """Asigna a cada hilo trabajador una fila fija de la terminal para su
    barra de progreso (para que no se pisen entre sí). Cada hilo del pool
    conserva siempre el mismo slot durante toda la ejecución."""
    global _slot_contador
    if not hasattr(_slot_local, "slot"):
        with _slot_lock:
            _slot_local.slot = _slot_contador % max(total_slots, 1)
            _slot_contador += 1
    return _slot_local.slot


def formatear_bytes(n):
    if n is None:
        return "?"
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unidad}" if unidad != "B" else f"{int(n)} {unidad}"
        n /= 1024
    return f"{n:.1f} PB"


def parsear_tamano(texto):
    """Convierte el tamaño tal como lo muestra el listado ('703M', '18K',
    '2344', '-') a bytes aproximados. Devuelve None si no se puede
    interpretar (ej. '-' para carpetas)."""
    if not texto:
        return None
    texto = texto.strip()
    if not texto or texto == "-":
        return None
    m = re.match(r"^([\d.]+)\s*([KMGT])?B?$", texto, re.IGNORECASE)
    if not m:
        return None
    numero = float(m.group(1))
    unidad = (m.group(2) or "").upper()
    factor = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[unidad]
    return int(numero * factor)


def normalizar_nombre(nombre):
    """Evita problemas con caracteres raros en nombres de archivo/carpeta locales."""
    nombre = unicodedata.normalize("NFC", nombre)
    for ch in '<>:"/\\|?*':
        nombre = nombre.replace(ch, "_")
    return nombre.rstrip(" .")


def directorio_base():
    """Carpeta donde vive el ejecutable/script (funciona tanto corriendo el
    .py normal como compilado con PyInstaller, donde sys.frozen=True)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # __file__ acá es dr_lib/utils.py -> subimos un nivel para la raíz del proyecto
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

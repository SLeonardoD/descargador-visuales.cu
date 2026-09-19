#!/usr/bin/env python3
"""
descargador_repositorio.py
---------------------------
Herramienta para navegar recursivamente un repositorio audiovisual tipo
"Index of /..." (listados de directorio estilo Apache/nginx/lighttpd),
recrear su árbol de carpetas en local y descargar únicamente los archivos
que nos interesan (por defecto: videos y subtítulos).

USO
---
    descargador_repositorio [opciones] URL

    (Las opciones van antes de la URL, ej:)
    descargador_repositorio --workers 2 --dry-run "https://sitio/Series/Mi Serie/"

CARACTERÍSTICAS
----------------
    - Reanudación REAL de descargas cortadas (usa HTTP Range, sigue exactamente
      donde quedó un archivo ".part" en vez de reiniciar desde cero).
    - Reintentos automáticos con espera progresiva ante fallos de red.
    - Detección de archivos ya completos SIN pedirle nada al servidor: usa el
      tamaño aproximado que ya viene en el propio listado (ej. "703M").
    - Empieza a descargar mientras todavía sigue explorando subcarpetas
      (no espera a terminar de recorrer todo el árbol para arrancar).
    - Límite de velocidad opcional para no saturar la conexión.
    - Cancelación limpia con Ctrl+C: no deja el proceso a mitad de camino sin
      avisar, y lo que ya se bajó (incluyendo archivos ".part" parciales) se
      puede retomar simplemente corriendo el mismo comando de nuevo.
    - Barra de progreso por archivo y un total agregado (con tqdm).
    - Resumen final: cuántos se descargaron, cuántos ya existían, cuántos
      fallaron, y cuánto se transfirió en total.

OPCIONES ÚTILES
----------------
    -o, --output DIR        Carpeta base donde guardar todo (por defecto: carpeta actual)
    -e, --ext ".e1,.e2"     Extensiones extra a descargar, separadas por coma
    --solo-ext ".e1,.e2"    Ignora las extensiones por defecto y usa SOLO estas
    --dry-run               No descarga nada, solo muestra el árbol y tamaños estimados
    --max-depth N           Límite de profundidad de recursión (por defecto: sin límite)
    --workers N             Descargas simultáneas (por defecto: 2)
    --retries N             Reintentos por archivo ante error de red (por defecto: 3)
    --rate-limit KBps       Límite de velocidad agregado, en KB/s (por defecto: sin límite)
    --user-agent "..."      User-Agent personalizado
    --no-resume             Ignora archivos/.part existentes y descarga todo desde cero
    -q, --quiet             Solo errores y el resumen final (sin barras de progreso)
    -v, --verbose           Más detalle (muestra cada carpeta explorada). Repetible: -vv
    --use-proxy             Activa el uso de proxy (si no se pasa, NUNCA se usa proxy)
    --proxy-file archivo.csv  Archivo CSV con los datos del proxy (requiere --use-proxy)

PROXY (opcional, requiere --use-proxy)
----------------------------------------
El proxy está DESACTIVADO por defecto. Solo se usa si pasas --use-proxy.

Cuando usas --use-proxy, la herramienta busca un archivo .csv con los datos
del proxy (por defecto "proxy.csv" junto al ejecutable/script; puedes indicar
otra ruta con --proxy-file):

  - Si el archivo NO existe: se genera automáticamente una plantilla de
    ejemplo en esa ruta y el programa se detiene, para que la completes
    con tus datos reales y lo vuelvas a ejecutar.
  - Si el archivo existe pero está mal formado: el programa se detiene con
    un error de "proxy incorrecto".
  - Si el archivo es válido: se usa ese proxy para todas las peticiones.

Formato del .csv (fila de encabezado + una fila de datos; usuario y
contrasena pueden ir vacíos si el proxy no pide autenticación):

    host,puerto,usuario,contrasena,esquema
    10.0.0.5,8080,miusuario,miclave,http

La columna "esquema" es opcional (por defecto "http"; también acepta
"https" o "socks5" — para socks5 hace falta instalar `pip install "requests[socks]"`).

EJEMPLOS
--------
    descargador_repositorio --dry-run "https://sitio/Series/Mi Serie/"
    descargador_repositorio --solo-ext .srt "https://sitio/Series/Mi Serie/Temporada 1/"
    descargador_repositorio -o "/mnt/d/Videos" --workers 5 "https://sitio/Peliculas/"
    descargador_repositorio --rate-limit 500 "https://sitio/Peliculas/"   # máx ~500 KB/s
    descargador_repositorio --use-proxy "https://sitio/Peliculas/"
"""

import argparse
import csv
import os
import queue
import re
import signal
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, unquote, quote

import requests
from tqdm import tqdm

__version__ = "0.2.0"

DEFAULT_VIDEO_EXT = {".avi", ".mp4", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
DEFAULT_SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

PROXY_FILE_POR_DEFECTO = "proxy.csv"
PROXY_CSV_COLUMNAS = ["host", "puerto", "usuario", "contrasena", "esquema"]
PROXY_CSV_EJEMPLO = ["10.0.0.5", "8080", "miusuario", "miclave", "http"]

CHUNK_SIZE = 512 * 1024  # 512 KiB
TOLERANCIA_MINIMA_BYTES = 1024 * 1024  # 1 MiB

SESSION = requests.Session()

# Bandera de cancelación compartida entre hilos. En Python "free-threaded"
# (3.13t/3.14t sin GIL) las operaciones sobre objetos compartidos ya NO están
# protegidas implícitamente, así que usamos threading.Event (que es seguro
# por diseño) en vez de, por ejemplo, un simple booleano global.
CANCELAR = threading.Event()

# Nivel de detalle en consola: -1 silencioso, 0 normal, 1+ detallado.
# Se fija una sola vez en main() ANTES de arrancar los hilos, así que no
# necesita lock (no hay escritura concurrente).
NIVEL = 0

_slot_lock = threading.Lock()
_slot_contador = 0
_slot_local = threading.local()


class ProxyIncorrectoError(Exception):
    """Se lanza cuando el archivo de proxy existe pero está mal formado."""


class ProxyPlantillaGeneradaError(Exception):
    """Se lanza cuando no existía el archivo de proxy y se acaba de generar uno de ejemplo."""


def log(msg, min_nivel=0):
    if NIVEL >= min_nivel:
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
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Parseo del listado "Index of /" con un parser HTML real (no regex), que es
# tolerante a variaciones de formato entre servidores y decodifica entidades
# HTML automáticamente.
# ---------------------------------------------------------------------------
class ListadoParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entradas = []  # [(href, texto_después_del_link), ...]
        self._href_actual = None
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._cerrar_entrada_actual()
            self._href_actual = dict(attrs).get("href")

    def handle_data(self, data):
        if self._href_actual is not None:
            self._buffer.append(data)

    def _cerrar_entrada_actual(self):
        if self._href_actual is not None:
            self.entradas.append((self._href_actual, "".join(self._buffer)))
        self._href_actual = None
        self._buffer = []

    def close(self):
        self._cerrar_entrada_actual()
        super().close()


def parsear_listado_html(html_text, url):
    """Función PURA (sin red): recibe el HTML de una página 'Index of /' y
    la url de esa carpeta, y devuelve una lista de
    (nombre, url_absoluta, es_dir, tamano_aprox_bytes).
    Separada de listar_directorio() para poder testearla sin un servidor."""
    parser = ListadoParser()
    parser.feed(html_text)
    parser.close()

    entradas = []
    for href, texto_extra in parser.entradas:
        if not href or href in ("../", ".."):
            continue
        if href.startswith("?") or href.startswith("#"):
            continue  # links de ordenamiento tipo ?C=N;O=D, o anclas internas
        abs_url = urljoin(url, href)
        if not abs_url.startswith(url):
            continue  # enlace fuera de esta carpeta (breadcrumbs, sort links, etc.)

        es_dir = abs_url.endswith("/")
        nombre = unquote(abs_url[len(url):].rstrip("/") if es_dir else abs_url[len(url):])
        if not nombre:
            continue

        tamano_aprox = None
        if not es_dir:
            tokens = texto_extra.split()
            if tokens:
                tamano_aprox = parsear_tamano(tokens[-1])

        entradas.append((nombre, abs_url, es_dir, tamano_aprox))
    return entradas


def listar_directorio(url, user_agent):
    """Descarga una página 'Index of /' y devuelve lista de
    (nombre, url_absoluta, es_dir, tamano_aprox_bytes)."""
    resp = SESSION.get(url, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    return parsear_listado_html(resp.text, url)


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------
def generar_proxy_plantilla(ruta_csv):
    with open(ruta_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(PROXY_CSV_COLUMNAS)
        w.writerow(PROXY_CSV_EJEMPLO)


def leer_proxy_csv(ruta_csv):
    try:
        with open(ruta_csv, newline="", encoding="utf-8-sig") as f:
            lector = csv.DictReader(f)
            if not lector.fieldnames:
                raise ProxyIncorrectoError(f"El archivo de proxy '{ruta_csv}' está vacío o no tiene encabezado.")
            lector.fieldnames = [c.strip().lower() for c in lector.fieldnames]
            fila = next(lector, None)
    except OSError as e:
        raise ProxyIncorrectoError(f"No se pudo abrir el archivo de proxy '{ruta_csv}': {e}") from e
    except csv.Error as e:
        raise ProxyIncorrectoError(f"El archivo de proxy '{ruta_csv}' no es un csv válido: {e}") from e

    if fila is None:
        raise ProxyIncorrectoError(f"El archivo de proxy '{ruta_csv}' no tiene ninguna fila de datos.")

    host = (fila.get("host") or "").strip()
    puerto_txt = (fila.get("puerto") or fila.get("port") or "").strip()
    usuario = (fila.get("usuario") or fila.get("user") or "").strip()
    contrasena = (fila.get("contrasena") or fila.get("contraseña") or fila.get("password") or "").strip()
    esquema = (fila.get("esquema") or fila.get("scheme") or "http").strip().lower()

    if not host or not puerto_txt:
        raise ProxyIncorrectoError(f"El archivo de proxy '{ruta_csv}' debe incluir al menos 'host' y 'puerto'.")
    if not puerto_txt.isdigit():
        raise ProxyIncorrectoError(f"El puerto '{puerto_txt}' en '{ruta_csv}' no es un número válido.")
    puerto = int(puerto_txt)
    if not (1 <= puerto <= 65535):
        raise ProxyIncorrectoError(f"El puerto '{puerto}' en '{ruta_csv}' está fuera de rango (1-65535).")
    if esquema not in ("http", "https", "socks5", "socks5h"):
        raise ProxyIncorrectoError(f"Esquema de proxy no soportado en '{ruta_csv}': '{esquema}'.")

    credenciales = f"{quote(usuario, safe='')}:{quote(contrasena, safe='')}@" if usuario else ""
    proxy_url = f"{esquema}://{credenciales}{host}:{puerto}"
    return {"http": proxy_url, "https": proxy_url}


def resolver_proxy(ruta_csv):
    if not os.path.exists(ruta_csv):
        generar_proxy_plantilla(ruta_csv)
        raise ProxyPlantillaGeneradaError(
            f"No se encontró '{ruta_csv}'. Se generó una plantilla de ejemplo en esa ruta.\n"
            f"Complétala con los datos reales de tu proxy y vuelve a ejecutar el comando."
        )
    return leer_proxy_csv(ruta_csv)


# ---------------------------------------------------------------------------
# Límite de velocidad (compartido entre todos los hilos de descarga)
# ---------------------------------------------------------------------------
class LimitadorVelocidad:
    def __init__(self, bytes_por_segundo=None):
        self.bytes_por_segundo = bytes_por_segundo
        self.lock = threading.Lock()
        self.inicio_ventana = time.monotonic()
        self.acumulado = 0

    def consumir(self, n):
        if not self.bytes_por_segundo:
            return
        with self.lock:
            self.acumulado += n
            transcurrido = time.monotonic() - self.inicio_ventana
            esperado = self.acumulado / self.bytes_por_segundo
            espera = esperado - transcurrido
            if transcurrido >= 1.0:
                self.inicio_ventana = time.monotonic()
                self.acumulado = 0
        if espera > 0:
            time.sleep(espera)


# ---------------------------------------------------------------------------
# Estadísticas finales (acceso desde varios hilos -> con lock)
# ---------------------------------------------------------------------------
class Estadisticas:
    def __init__(self):
        self.lock = threading.Lock()
        self.completados = 0
        self.omitidos = 0
        self.fallidos = 0
        self.bytes_descargados = 0
        self.nombres_fallidos = []

    def sumar_completado(self, bytes_nuevos):
        with self.lock:
            self.completados += 1
            self.bytes_descargados += bytes_nuevos

    def sumar_omitido(self):
        with self.lock:
            self.omitidos += 1

    def sumar_fallido(self, nombre):
        with self.lock:
            self.fallidos += 1
            self.nombres_fallidos.append(nombre)


# ---------------------------------------------------------------------------
# Descarga de un archivo: resume real (Range), reintentos, límite de
# velocidad, barra de progreso individual.
# ---------------------------------------------------------------------------
def descargar_archivo(url, destino, tamano_aprox, args, stats, limitador, barra_total):
    nombre = os.path.basename(destino)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    tmp = destino + ".part"

    # 1) ¿Ya está completo? Lo resolvemos SIN red, usando el tamaño
    #    aproximado que ya trae el listado (evita un HEAD extra por archivo).
    if not args.no_resume and os.path.exists(destino) and tamano_aprox:
        tolerancia = max(TOLERANCIA_MINIMA_BYTES, int(tamano_aprox * 0.02))
        if abs(os.path.getsize(destino) - tamano_aprox) <= tolerancia:
            log(f"[ya existe] {nombre}", min_nivel=1)
            stats.sumar_omitido()
            return

    if args.no_resume:
        for ruta in (destino, tmp):
            if os.path.exists(ruta):
                os.remove(ruta)

    intentos = 0
    while True:
        if CANCELAR.is_set():
            return
        intentos += 1
        offset = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        headers = {"User-Agent": args.user_agent}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"

        barra = None
        try:
            with SESSION.get(url, headers=headers, stream=True, timeout=60) as r:
                if offset > 0 and r.status_code == 416:
                    # El servidor dice "ese rango no existe": probablemente ya
                    # está completo. Confirmamos con una petición normal.
                    r.close()
                    with SESSION.get(url, headers={"User-Agent": args.user_agent}, stream=True, timeout=30) as r2:
                        total_real = int(r2.headers.get("Content-Length") or 0)
                    if total_real and offset >= total_real:
                        os.replace(tmp, destino)
                        log(f"[completado] {nombre}", min_nivel=1)
                        stats.sumar_completado(total_real)
                        return
                    # Tamaño no cuadra -> el .part quedó corrupto, reiniciar
                    os.remove(tmp)
                    continue

                modo = "ab" if (offset > 0 and r.status_code == 206) else "wb"
                if offset > 0 and r.status_code == 200:
                    # El servidor ignoró el Range (no soporta resume): reiniciar.
                    offset = 0

                r.raise_for_status()

                if r.status_code == 206:
                    content_range = r.headers.get("Content-Range", "")
                    total = int(content_range.split("/")[-1]) if "/" in content_range else None
                else:
                    cl = r.headers.get("Content-Length")
                    total = int(cl) if cl is not None else None

                if not args.quiet:
                    slot = obtener_slot_de_este_hilo(max(args.workers, 1))
                    barra = tqdm(
                        total=total, initial=offset, unit="B", unit_scale=True,
                        desc=nombre[:40], leave=False, position=slot, dynamic_ncols=True,
                    )

                with open(tmp, modo) as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if CANCELAR.is_set():
                            return
                        if not chunk:
                            continue
                        f.write(chunk)
                        limitador.consumir(len(chunk))
                        if barra:
                            barra.update(len(chunk))
                        if barra_total:
                            barra_total.update(len(chunk))

            os.replace(tmp, destino)
            log(f"[descargado] {nombre}", min_nivel=1)
            stats.sumar_completado(os.path.getsize(destino))
            return

        except requests.RequestException as e:
            if intentos > args.retries:
                log(f"[ERROR] {nombre}: {e} (después de {intentos} intento(s), se conserva el .part para reanudar)")
                stats.sumar_fallido(nombre)
                return
            espera = min(30, 2**intentos)
            log(f"[reintentando] {nombre}: {e} -> intento {intentos + 1}/{args.retries + 1} en {espera}s", min_nivel=1)
            time.sleep(espera)
        finally:
            if barra:
                barra.close()


# ---------------------------------------------------------------------------
# Exploración recursiva del árbol remoto (generador: permite empezar a
# descargar mientras todavía se sigue explorando).
# ---------------------------------------------------------------------------
def explorar(url, ruta_local, extensiones, user_agent, max_depth, depth=0):
    """Generador que produce eventos:
        ("carpeta", url)
        ("archivo", url_absoluta, ruta_local_destino, tamano_aprox_o_None)
    """
    if CANCELAR.is_set():
        return
    yield ("carpeta", url)
    try:
        entradas = listar_directorio(url, user_agent)
    except requests.RequestException as e:
        yield ("error", url, str(e))
        return

    for nombre, abs_url, es_dir, tamano_aprox in entradas:
        if CANCELAR.is_set():
            return
        nombre_local = normalizar_nombre(nombre)
        if es_dir:
            if max_depth is not None and depth >= max_depth:
                continue
            yield from explorar(abs_url, os.path.join(ruta_local, nombre_local), extensiones, user_agent, max_depth, depth + 1)
        else:
            _, ext = os.path.splitext(nombre_local)
            if ext.lower() in extensiones:
                yield ("archivo", abs_url, os.path.join(ruta_local, nombre_local), tamano_aprox)


def modo_dry_run(url, ruta_local, extensiones, user_agent, max_depth):
    total_bytes = 0
    total_archivos = 0
    for evento in explorar(url, ruta_local, extensiones, user_agent, max_depth):
        if evento[0] == "carpeta":
            log(f"[Carpeta] {evento[1]}")
        elif evento[0] == "error":
            log(f"  [ERROR listando] {evento[2]}")
        else:
            _, abs_url, destino, tamano_aprox = evento
            total_archivos += 1
            if tamano_aprox:
                total_bytes += tamano_aprox
            log(f"  [Se descargaría] {os.path.basename(destino)} ({formatear_bytes(tamano_aprox)})")
    log(f"\nTotal: {total_archivos} archivo(s), ~{formatear_bytes(total_bytes)} (estimado a partir del listado).")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    global NIVEL

    ap = argparse.ArgumentParser(
        prog="descargador_repositorio",
        description="Descarga videos/subtítulos de un repositorio tipo 'Index of /'.",
        usage="%(prog)s [opciones] URL",
    )
    ap.add_argument("-o", "--output", default=".", help="Carpeta local donde recrear el árbol.")
    ap.add_argument("-e", "--ext", default="", help="Extensiones extra, separadas por coma. Ej: -e '.nfo,.jpg'")
    ap.add_argument("--solo-ext", default=None, help="Usar SOLO estas extensiones (coma-separadas).")
    ap.add_argument("--dry-run", action="store_true", help="No descarga nada, solo muestra el árbol y tamaños.")
    ap.add_argument("--max-depth", type=int, default=None, help="Profundidad máxima de recursión.")
    ap.add_argument("--workers", type=int, default=2, help="Descargas simultáneas (por defecto: 2).")
    ap.add_argument("--retries", type=int, default=3, help="Reintentos por archivo ante error de red (por defecto: 3).")
    ap.add_argument("--rate-limit", type=float, default=None, help="Límite de velocidad agregado, en KB/s.")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (compatible; DescargadorRepositorio/1.0)")
    ap.add_argument("--no-resume", action="store_true", help="Ignora archivos/.part existentes, descarga todo desde cero.")
    ap.add_argument("-q", "--quiet", action="store_true", help="Solo errores y el resumen final.")
    ap.add_argument("-v", "--verbose", action="count", default=0, help="Más detalle (repetible: -vv).")
    ap.add_argument("--use-proxy", action="store_true", help="Activa el uso de proxy (desactivado por defecto).")
    ap.add_argument("--proxy-file", default=None, help=f"Archivo .csv del proxy (por defecto: '{PROXY_FILE_POR_DEFECTO}' junto al ejecutable).")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("url", help="URL de la carpeta raíz a descargar. Va al final, después de las opciones.")
    args = ap.parse_args()

    if args.workers < 1:
        ap.error("--workers debe ser 1 o mayor.")
    if args.retries < 0:
        ap.error("--retries debe ser 0 o mayor.")
    if args.quiet and args.verbose:
        ap.error("--quiet y --verbose son mutuamente excluyentes.")

    NIVEL = -1 if args.quiet else args.verbose

    def manejar_sigint(signum, frame):
        if CANCELAR.is_set():
            log("\nSegunda interrupción: forzando salida inmediata.")
            os._exit(130)
        CANCELAR.set()
        log("\nCancelando... esperando a que terminen las descargas en curso (Ctrl+C de nuevo para forzar).")

    signal.signal(signal.SIGINT, manejar_sigint)

    if args.use_proxy:
        ruta_proxy = args.proxy_file or os.path.join(directorio_base(), PROXY_FILE_POR_DEFECTO)
        try:
            proxies = resolver_proxy(ruta_proxy)
        except ProxyPlantillaGeneradaError as e:
            log(str(e))
            sys.exit(1)
        except ProxyIncorrectoError as e:
            log(f"[ERROR] Proxy incorrecto: {e}")
            sys.exit(1)
        SESSION.proxies.update(proxies)
        log(f"Usando proxy: {proxies['http'].split('@')[-1]}")
    else:
        log("Proxy desactivado (usa --use-proxy para activarlo).", min_nivel=1)

    url = args.url if args.url.endswith("/") else args.url + "/"

    def _parsear_ext(txt):
        return {
            (e.strip() if e.strip().startswith(".") else "." + e.strip()).lower()
            for e in txt.split(",") if e.strip()
        }

    if args.solo_ext is not None:
        extensiones = _parsear_ext(args.solo_ext)
        if not extensiones:
            ap.error("--solo-ext no puede estar vacío.")
    else:
        extensiones = set(DEFAULT_VIDEO_EXT) | set(DEFAULT_SUB_EXT) | _parsear_ext(args.ext)

    ultimo_segmento = unquote(urlparse(url).path.rstrip("/").split("/")[-1]) or "descarga"
    raiz_local = os.path.join(args.output, normalizar_nombre(ultimo_segmento))

    log(f"URL raíz: {url}")
    log(f"Carpeta local: {raiz_local}")
    log(f"Extensiones a descargar: {sorted(extensiones)}", min_nivel=1)

    if args.dry_run:
        modo_dry_run(url, raiz_local, extensiones, args.user_agent, args.max_depth)
        return

    # --- Descarga real: cola productor (explorador) / consumidores (workers) ---
    cola = queue.Queue(maxsize=500)
    stats = Estadisticas()
    limitador = LimitadorVelocidad(args.rate_limit * 1024 if args.rate_limit else None)
    barra_total = None if args.quiet else tqdm(
        total=0, unit="B", unit_scale=True, desc="TOTAL", position=args.workers, dynamic_ncols=True,
    )
    lock_total = threading.Lock()
    inicio = time.monotonic()

    def hilo_explorador():
        try:
            for evento in explorar(url, raiz_local, extensiones, args.user_agent, args.max_depth):
                if CANCELAR.is_set():
                    break
                if evento[0] == "carpeta":
                    log(f"[Carpeta] {evento[1]}", min_nivel=1)
                elif evento[0] == "error":
                    log(f"  [ERROR listando] {evento[2]}")
                else:
                    _, abs_url, destino, tamano_aprox = evento
                    if barra_total is not None and tamano_aprox:
                        with lock_total:
                            barra_total.total = (barra_total.total or 0) + tamano_aprox
                            barra_total.refresh()
                    cola.put((abs_url, destino, tamano_aprox))
        finally:
            for _ in range(args.workers):
                cola.put(None)  # centinela: uno por cada worker

    def hilo_worker():
        while True:
            item = cola.get()
            try:
                if item is None or CANCELAR.is_set():
                    return
                abs_url, destino, tamano_aprox = item
                descargar_archivo(abs_url, destino, tamano_aprox, args, stats, limitador, barra_total)
            finally:
                cola.task_done()

    explorador = threading.Thread(target=hilo_explorador, daemon=True)
    explorador.start()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futuros = [executor.submit(hilo_worker) for _ in range(args.workers)]
        explorador.join()
        for f in futuros:
            f.result()

    if barra_total is not None:
        barra_total.close()

    duracion = time.monotonic() - inicio
    log("")
    if CANCELAR.is_set():
        log("Descarga CANCELADA por el usuario. Podés retomarla corriendo el mismo comando de nuevo.")
    log(
        f"Resumen: {stats.completados} descargado(s), {stats.omitidos} ya existían, "
        f"{stats.fallidos} fallaron. Transferidos {formatear_bytes(stats.bytes_descargados)} "
        f"en {duracion:.1f}s."
    )
    if stats.nombres_fallidos:
        log("Archivos que fallaron (podés reintentar corriendo el mismo comando de nuevo):")
        for n in stats.nombres_fallidos:
            log(f"  - {n}")

    if CANCELAR.is_set():
        sys.exit(130)
    sys.exit(1 if stats.fallidos else 0)


if __name__ == "__main__":
    main()

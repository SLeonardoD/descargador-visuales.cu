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
    descargador_repositorio [opciones] [URL]

    (Las opciones van antes de la URL. La URL es opcional SI la definiste
    dentro de un config-file.json y usás --use-config-file.)

    descargador_repositorio --workers 2 --dry-run "https://sitio/Series/Mi Serie/"
    descargador_repositorio --use-config-file

CARACTERÍSTICAS
----------------
    - Reanudación REAL de descargas cortadas (usa HTTP Range).
    - Reintentos automáticos con espera progresiva ante fallos de red.
    - Detección de archivos ya completos SIN pedirle nada al servidor.
    - Empieza a descargar mientras todavía sigue explorando subcarpetas, y
      muestra un resumen de tamaño estimado apenas termina de explorar.
    - Límite de velocidad opcional.
    - Cancelación limpia con Ctrl+C, retomable corriendo el mismo comando.
    - Barra de progreso por archivo y un total agregado (con tqdm), y salida
      con colores (desactivable con NO_COLOR=1).
    - config-file.json opcional: puede fijar CUALQUIERA de las flags de
      abajo (y la URL, y un proxy), para no tener que repetirlas cada vez.
      Lo que pases a mano por línea de comandos siempre tiene prioridad
      sobre lo que diga el archivo.

OPCIONES ÚTILES
----------------
    -o, --output DIR        Carpeta base donde guardar todo (por defecto: carpeta actual)
    -e, --ext ".e1,.e2"     Extensiones extra a descargar, separadas por coma
    --solo-ext ".e1,.e2"    Ignora las extensiones por defecto y usa SOLO estas
    --dry-run               No descarga nada, solo muestra el árbol y tamaños estimados
    --max-depth N           Límite de profundidad de recursión
    --workers N             Descargas simultáneas (por defecto: 2)
    --retries N             Reintentos por archivo ante error de red (por defecto: 3)
    --rate-limit KBps       Límite de velocidad agregado, en KB/s
    --user-agent "..."      User-Agent personalizado
    --no-resume             Ignora archivos/.part existentes, descarga todo desde cero
    -q, --quiet             Solo errores y el resumen final (sin barras de progreso)
    -v, --verbose           Más detalle (repetible: -vv)
    --use-config-file       Activa la lectura de config-file.json (ver abajo)
    --config-file archivo   Ruta del config-file.json (por defecto: junto al ejecutable)
    --config-example        Genera example.config-file.json con TODAS las flags
                             documentadas y sale (no requiere URL)

CONFIG-FILE.JSON (opcional, requiere --use-config-file)
---------------------------------------------------------
El config-file está DESACTIVADO por defecto. Solo se usa si pasás
--use-config-file, y solo entonces la herramienta busca (por defecto)
"config-file.json" junto al ejecutable/script.

  - Si el archivo NO existe: se genera automáticamente una plantilla
    completa (con todas las flags documentadas) en esa ruta, y el
    programa se detiene para que la completes.
  - Si el archivo existe pero es inválido (JSON roto, un campo con el
    tipo equivocado, un puerto de proxy fuera de rango, etc.): el
    programa se detiene con un error de "config incorrecto".
  - Si es válido: cada campo que el archivo defina se usa como valor por
    defecto de esa flag. Cualquier flag que pases A MANO en la línea de
    comandos siempre gana por sobre lo que diga el archivo. Un campo que
    NI el archivo ni la línea de comandos definen usa el valor por
    defecto normal del programa.

Corré `descargador_repositorio --config-example` para generar un archivo de
ejemplo con los 14 campos posibles (incluyendo "url" y "proxy") explicados.

EJEMPLOS
--------
    descargador_repositorio --dry-run "https://sitio/Series/Mi Serie/"
    descargador_repositorio --solo-ext .srt "https://sitio/Series/Mi Serie/Temporada 1/"
    descargador_repositorio -o "/mnt/d/Videos" --workers 5 "https://sitio/Peliculas/"
    descargador_repositorio --rate-limit 500 "https://sitio/Peliculas/"
    descargador_repositorio --config-example
    descargador_repositorio --use-config-file
    descargador_repositorio --use-config-file --workers 5 "https://otro-sitio/Otra Serie/"
"""

import argparse
import os
import queue
import signal
import sys
import threading
import time
import types
from urllib.parse import unquote, urlparse

from tqdm import tqdm

from dr_lib import config as cfgmod
from dr_lib import utils as u
from dr_lib.utils import (
    CANCELAR, SESSION, directorio_base, formatear_bytes, log, normalizar_nombre,
)
from dr_lib.listado import explorar, modo_dry_run
from dr_lib.descarga import Estadisticas, LimitadorVelocidad, descargar_archivo
from dr_lib.config import (
    DEFAULTS, ConfigIncorrectoError, ConfigPlantillaGeneradaError,
    generar_config_ejemplo, resolver_config,
)

# --- re-exports para que los tests (y cualquier script viejo) puedan seguir
# --- haciendo `import descargador_repositorio as dr; dr.parsear_tamano(...)`
from dr_lib.utils import parsear_tamano, obtener_slot_de_este_hilo  # noqa: F401
from dr_lib.listado import ListadoParser, parsear_listado_html, listar_directorio  # noqa: F401
from dr_lib.descarga import CHUNK_SIZE, TOLERANCIA_MINIMA_BYTES  # noqa: F401

__version__ = "0.3.0"

DEFAULT_VIDEO_EXT = {".avi", ".mp4", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
DEFAULT_SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

CONFIG_FILE_POR_DEFECTO = "config-file.json"


def _parsear_ext(txt):
    return {
        (e.strip() if e.strip().startswith(".") else "." + e.strip()).lower()
        for e in (txt or "").split(",") if e.strip()
    }


def main():
    ap = argparse.ArgumentParser(
        prog="descargador_repositorio",
        description="Descarga videos/subtítulos de un repositorio tipo 'Index of /'.",
        usage="%(prog)s [opciones] [URL]",
    )
    # Todos los defaults acá son None a propósito: es el "sentinel" que nos
    # permite distinguir "el usuario no pasó esta flag" de "el usuario la
    # pasó con tal valor", para poder mezclarlo con el config-file.json.
    ap.add_argument("-o", "--output", default=None, help="Carpeta local donde recrear el árbol.")
    ap.add_argument("-e", "--ext", default=None, help="Extensiones extra, separadas por coma. Ej: -e '.nfo,.jpg'")
    ap.add_argument("--solo-ext", default=None, help="Usar SOLO estas extensiones (coma-separadas).")
    ap.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None, help="No descarga nada, solo muestra el árbol y tamaños.")
    ap.add_argument("--max-depth", type=int, default=None, help="Profundidad máxima de recursión.")
    ap.add_argument("--workers", type=int, default=None, help="Descargas simultáneas (por defecto: 2).")
    ap.add_argument("--retries", type=int, default=None, help="Reintentos por archivo ante error de red (por defecto: 3).")
    ap.add_argument("--rate-limit", type=float, default=None, help="Límite de velocidad agregado, en KB/s.")
    ap.add_argument("--user-agent", default=None)
    ap.add_argument("--no-resume", action=argparse.BooleanOptionalAction, default=None, help="Ignora archivos/.part existentes, descarga todo desde cero.")
    ap.add_argument("-q", "--quiet", action=argparse.BooleanOptionalAction, default=None, help="Solo errores y el resumen final.")
    ap.add_argument("-v", "--verbose", action="count", default=None, help="Más detalle (repetible: -vv).")
    ap.add_argument("--use-config-file", action="store_true", help="Activa la lectura de config-file.json (desactivado por defecto).")
    ap.add_argument("--config-file", default=None, help=f"Ruta del config-file.json (por defecto: '{CONFIG_FILE_POR_DEFECTO}' junto al ejecutable).")
    ap.add_argument("--config-example", action="store_true", help="Genera example.config-file.json con todas las flags documentadas y sale.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("url", nargs="?", default=None, help="URL de la carpeta raíz a descargar. Opcional si la definiste en el config-file.")
    args = ap.parse_args()

    # --config-example no necesita URL ni nada más: genera y sale.
    if args.config_example:
        ruta_ejemplo = os.path.join(directorio_base(), "example.config-file.json")
        generar_config_ejemplo(ruta_ejemplo)
        print(f"Se generó '{ruta_ejemplo}' con las {len(DEFAULTS) + 1} opciones documentadas (incluye 'proxy').")
        return

    def manejar_sigint(signum, frame):
        if CANCELAR.is_set():
            log("\nSegunda interrupción: forzando salida inmediata.")
            os._exit(130)
        CANCELAR.set()
        log("\nCancelando... esperando a que terminen las descargas en curso (Ctrl+C de nuevo para forzar).", color="yellow")

    signal.signal(signal.SIGINT, manejar_sigint)

    # --- Config-file: leer (si corresponde) ANTES de fusionar flags ---
    config_data = {}
    proxies_de_config = None
    if args.use_config_file:
        ruta_cfg = args.config_file or os.path.join(directorio_base(), CONFIG_FILE_POR_DEFECTO)
        try:
            config_data, proxies_de_config, advertencias = resolver_config(ruta_cfg)
        except ConfigPlantillaGeneradaError as e:
            log(str(e), color="yellow")
            sys.exit(1)
        except ConfigIncorrectoError as e:
            log(f"[ERROR] Config incorrecto: {e}", color="red")
            sys.exit(1)
    else:
        advertencias = []

    # --- Fusión: CLI explícito > config-file > default real del programa ---
    def resolver(campo):
        cli_valor = getattr(args, campo)
        if cli_valor is not None:
            return cli_valor
        if campo in config_data:
            return config_data[campo]
        return DEFAULTS[campo]

    output = resolver("output")
    ext_txt = resolver("ext")
    solo_ext_txt = resolver("solo_ext")
    dry_run = resolver("dry_run")
    max_depth = resolver("max_depth")
    workers = resolver("workers")
    retries = resolver("retries")
    rate_limit = resolver("rate_limit")
    user_agent = resolver("user_agent")
    no_resume = resolver("no_resume")
    quiet = resolver("quiet")
    verbose = resolver("verbose")
    url_arg = args.url or config_data.get("url")

    if quiet and verbose:
        ap.error("--quiet y --verbose son mutuamente excluyentes.")
    u.configurar_nivel(-1 if quiet else verbose)

    for clave in advertencias:
        log(f"[aviso] clave desconocida en el config-file: '{clave}' (¿typo?)", min_nivel=1, color="yellow")

    if workers < 1:
        ap.error("--workers debe ser 1 o mayor (revisá también el config-file si usás uno).")
    if retries < 0:
        ap.error("--retries debe ser 0 o mayor (revisá también el config-file si usás uno).")
    if not url_arg:
        ap.error("Falta la URL: pasala por línea de comandos, o definila en el campo 'url' del config-file.json (con --use-config-file).")

    if args.use_config_file:
        if proxies_de_config:
            SESSION.proxies.update(proxies_de_config)
            log(f"Usando proxy: {proxies_de_config['http'].split('@')[-1]}")
        log(f"Usando config-file: {args.config_file or os.path.join(directorio_base(), CONFIG_FILE_POR_DEFECTO)}", min_nivel=1)
    else:
        log("Config-file desactivado (usa --use-config-file para activarlo).", min_nivel=1)

    url = url_arg if url_arg.endswith("/") else url_arg + "/"

    if solo_ext_txt:
        extensiones = _parsear_ext(solo_ext_txt)
        if not extensiones:
            ap.error("--solo-ext no puede estar vacío.")
    else:
        extensiones = set(DEFAULT_VIDEO_EXT) | set(DEFAULT_SUB_EXT) | _parsear_ext(ext_txt)

    ultimo_segmento = unquote(urlparse(url).path.rstrip("/").split("/")[-1]) or "descarga"
    raiz_local = os.path.join(output, normalizar_nombre(ultimo_segmento))

    log(f"URL raíz: {url}")
    log(f"Carpeta local: {raiz_local}")
    log(f"Extensiones a descargar: {sorted(extensiones)}", min_nivel=1)
    log(
        f"Config efectiva: workers={workers} retries={retries} rate_limit={rate_limit} "
        f"no_resume={no_resume} max_depth={max_depth}",
        min_nivel=1,
    )

    if dry_run:
        modo_dry_run(url, raiz_local, extensiones, user_agent, max_depth)
        return

    # --- Descarga real: cola productor (explorador) / consumidores (workers) ---
    cfg = types.SimpleNamespace(
        workers=workers, retries=retries, user_agent=user_agent, no_resume=no_resume, quiet=quiet,
    )
    cola = queue.Queue(maxsize=500)
    stats = Estadisticas()
    limitador = LimitadorVelocidad(rate_limit * 1024 if rate_limit else None)
    barra_total = None if quiet else tqdm(
        total=0, unit="B", unit_scale=True, desc="TOTAL", position=workers, dynamic_ncols=True,
    )
    lock_total = threading.Lock()
    inicio = time.monotonic()
    contador_descubiertos = {"archivos": 0, "bytes": 0, "bytes_desconocidos": 0}

    def hilo_explorador():
        try:
            for evento in explorar(url, raiz_local, extensiones, user_agent, max_depth):
                if CANCELAR.is_set():
                    break
                if evento[0] == "carpeta":
                    log(f"[Carpeta] {evento[1]}", min_nivel=1)
                elif evento[0] == "error":
                    log(f"  [ERROR listando] {evento[2]}", color="red")
                else:
                    _, abs_url, destino, tamano_aprox = evento
                    contador_descubiertos["archivos"] += 1
                    if tamano_aprox:
                        contador_descubiertos["bytes"] += tamano_aprox
                    else:
                        contador_descubiertos["bytes_desconocidos"] += 1
                    if barra_total is not None and tamano_aprox:
                        with lock_total:
                            barra_total.total = (barra_total.total or 0) + tamano_aprox
                            barra_total.refresh()
                    cola.put((abs_url, destino, tamano_aprox))
        finally:
            extra = ""
            if contador_descubiertos["bytes_desconocidos"]:
                extra = f" (+{contador_descubiertos['bytes_desconocidos']} de tamaño desconocido)"
            log(
                f"Exploración completa: {contador_descubiertos['archivos']} archivo(s) encontrados, "
                f"~{formatear_bytes(contador_descubiertos['bytes'])} estimado{extra}.",
                color="cyan",
            )
            for _ in range(workers):
                cola.put(None)  # centinela: uno por cada worker

    def hilo_worker():
        while True:
            item = cola.get()
            try:
                if item is None or CANCELAR.is_set():
                    return
                abs_url, destino, tamano_aprox = item
                descargar_archivo(abs_url, destino, tamano_aprox, cfg, stats, limitador, barra_total)
            finally:
                cola.task_done()

    explorador = threading.Thread(target=hilo_explorador, daemon=True)
    explorador.start()

    hilos_worker = [threading.Thread(target=hilo_worker, daemon=True) for _ in range(workers)]
    for h in hilos_worker:
        h.start()
    explorador.join()
    for h in hilos_worker:
        h.join()

    if barra_total is not None:
        barra_total.close()

    duracion = time.monotonic() - inicio
    log("")
    if CANCELAR.is_set():
        log("Descarga CANCELADA por el usuario. Podés retomarla corriendo el mismo comando de nuevo.", color="yellow")
    log(
        f"Resumen: {stats.completados} descargado(s), {stats.omitidos} ya existían, "
        f"{stats.fallidos} fallaron. Transferidos {formatear_bytes(stats.bytes_descargados)} "
        f"en {duracion:.1f}s.",
        color=("red" if stats.fallidos else "green"),
    )
    if stats.nombres_fallidos:
        log("Archivos que fallaron (podés reintentar corriendo el mismo comando de nuevo):", color="red")
        for n in stats.nombres_fallidos:
            log(f"  - {n}", color="red")

    if CANCELAR.is_set():
        sys.exit(130)
    sys.exit(1 if stats.fallidos else 0)


if __name__ == "__main__":
    main()

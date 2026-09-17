#!/usr/bin/env python3
"""
descargador_repositorio.py
---------------------------
Herramienta para navegar recursivamente un repositorio audiovisual tipo
"Index of /..." (listados de directorio estilo Apache/nginx/lighttpd),
recrear su árbol de carpetas en local y descargar únicamente los archivos
que nos interesan (por defecto: videos y subtítulos).

USO BÁSICO
----------
    python3 descargador_repositorio.py "https://visuales.uclv.cu/Series/Espanol/Los%20Hombres%20de%20Paco/"

Esto crea, dentro de la carpeta actual, una carpeta "Los Hombres de Paco"
con la misma estructura de subcarpetas del sitio, y descarga adentro
todos los .avi/.mp4/.mkv/... y .srt/.ass/... que encuentre.

USO
---
    descargador_repositorio [opciones] URL

    (Las opciones van antes de la URL, ej:)
    descargador_repositorio --workers 2 --dry-run "https://sitio/Series/Mi Serie/"

OPCIONES ÚTILES
----------------
    -o, --output DIR        Carpeta base donde guardar todo (por defecto: carpeta actual)
    -e, --ext ".e1,.e2"     Extensiones extra a descargar, separadas por coma, ej: -e ".nfo,.jpg"
    --solo-ext ".e1,.e2"    Ignora las extensiones por defecto y usa SOLO estas
    --dry-run               No descarga nada, solo muestra qué haría (recomendado la primera vez)
    --max-depth N           Límite de profundidad de recursión (por defecto: sin límite)
    --workers N             Descargas simultáneas (por defecto: 2, para no saturar la conexión)
    --user-agent "..."      User-Agent personalizado
    --no-resume             Vuelve a descargar todo aunque el archivo ya exista
    --use-proxy             Activa el uso de proxy (si no se pasa, NUNCA se usa proxy)
    --proxy-file archivo.csv  Archivo CSV con los datos del proxy (solo aplica si --use-proxy está presente)

PROXY (opcional, requiere --use-proxy)
----------------------------------------
El proxy está DESACTIVADO por defecto. Solo se usa si pasas --use-proxy.

Cuando usas --use-proxy, la herramienta busca un archivo .csv con los datos
del proxy (por defecto "proxy.csv" en el directorio actual; puedes indicar
otro con --proxy-file):

  - Si el archivo NO existe: se genera automáticamente una plantilla de
    ejemplo en esa ruta y el programa se detiene, para que la completes
    con tus datos reales y lo vuelvas a ejecutar.
  - Si el archivo existe pero está mal formado (le faltan columnas, el
    puerto no es un número, el esquema no es válido, etc.): el programa
    se detiene con un error de "proxy incorrecto".
  - Si el archivo es válido: se usa ese proxy para todas las peticiones.

Formato del .csv (fila de encabezado + una fila de datos; usuario y
contrasena pueden ir vacíos si el proxy no pide autenticación):

    host,puerto,usuario,contrasena,esquema
    10.0.0.5,8080,miusuario,miclave,http

La columna "esquema" es opcional (por defecto "http"; también acepta
"https" o "socks5" — para socks5 hace falta instalar `pip install "requests[socks]"`).

EJEMPLOS
--------
    # Ver qué descargaría, sin bajar nada todavía
    descargador_repositorio --dry-run "https://sitio/Series/Mi Serie/"

    # Descargar solo subtítulos de una temporada puntual
    descargador_repositorio --solo-ext .srt "https://sitio/Series/Mi Serie/Temporada 1/"

    # Guardar en una carpeta específica y con 5 descargas a la vez
    descargador_repositorio -o "/mnt/d/Videos" --workers 5 "https://sitio/Peliculas/"

    # Descargar a través de un proxy (usa/crea proxy.csv en el directorio actual)
    descargador_repositorio --use-proxy "https://sitio/Peliculas/"

    # Descargar a través de un proxy definido en un csv con otro nombre
    descargador_repositorio --use-proxy --proxy-file mi_proxy.csv "https://sitio/Peliculas/"
"""

import argparse
import csv
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, unquote, quote

import requests

__version__ = "0.1.0"

DEFAULT_VIDEO_EXT = {".avi", ".mp4", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
DEFAULT_SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

PROXY_FILE_POR_DEFECTO = "proxy.csv"
PROXY_CSV_COLUMNAS = ["host", "puerto", "usuario", "contrasena", "esquema"]
PROXY_CSV_EJEMPLO = ["10.0.0.5", "8080", "miusuario", "miclave", "http"]

HREF_RE = re.compile(r'<a\s+[^>]*href="([^"]+)"', re.IGNORECASE)

SESSION = requests.Session()


class ProxyIncorrectoError(Exception):
    """Se lanza cuando el archivo de proxy existe pero está mal formado."""


class ProxyPlantillaGeneradaError(Exception):
    """Se lanza cuando no existía el archivo de proxy y se acaba de generar uno de ejemplo."""


def log(msg):
    print(msg, flush=True)


def normalizar_nombre(nombre):
    """Evita problemas con caracteres raros en nombres de archivo/carpeta locales."""
    nombre = unicodedata.normalize("NFC", nombre)
    # Windows no permite estos caracteres en nombres de archivo/carpeta
    for ch in '<>:"/\\|?*':
        nombre = nombre.replace(ch, "_")
    return nombre.rstrip(" .")


def listar_directorio(url, user_agent):
    """Descarga una página 'Index of /' y devuelve lista de (nombre, url_absoluta, es_dir)."""
    resp = SESSION.get(url, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    html = resp.text

    entradas = []
    for href in HREF_RE.findall(html):
        if href in ("../", ".."):
            continue
        # Ignorar enlaces absolutos a otras partes del sitio (breadcrumbs, etc.)
        # que no cuelguen de esta misma carpeta.
        abs_url = urljoin(url, href)
        if not abs_url.startswith(url):
            continue

        es_dir = abs_url.endswith("/")
        nombre = unquote(abs_url[len(url):].rstrip("/") if es_dir else abs_url[len(url):])
        if not nombre:
            continue
        entradas.append((nombre, abs_url, es_dir))
    return entradas


def generar_proxy_plantilla(ruta_csv):
    """Crea un .csv de ejemplo con la estructura esperada del proxy."""
    with open(ruta_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(PROXY_CSV_COLUMNAS)
        w.writerow(PROXY_CSV_EJEMPLO)


def leer_proxy_csv(ruta_csv):
    """Lee un .csv con columnas host,puerto,usuario,contrasena[,esquema] y
    devuelve un dict {"http": url, "https": url} listo para requests.
    Lanza ProxyIncorrectoError si el archivo existe pero está mal formado."""
    try:
        with open(ruta_csv, newline="", encoding="utf-8-sig") as f:
            lector = csv.DictReader(f)
            if not lector.fieldnames:
                raise ProxyIncorrectoError(f"El archivo de proxy '{ruta_csv}' está vacío o no tiene encabezado.")
            # Normalizamos los nombres de columna (minúsculas, sin espacios) para
            # aceptar variaciones como "Host", " Puerto ", etc.
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
        raise ProxyIncorrectoError(f"Esquema de proxy no soportado en '{ruta_csv}': '{esquema}' (usa http, https, socks5 o socks5h).")

    if usuario:
        credenciales = f"{quote(usuario, safe='')}:{quote(contrasena, safe='')}@"
    else:
        credenciales = ""

    proxy_url = f"{esquema}://{credenciales}{host}:{puerto}"
    return {"http": proxy_url, "https": proxy_url}


def resolver_proxy(ruta_csv):
    """Punto de entrada para --use-proxy: si el archivo no existe, genera una
    plantilla y avisa (ProxyPlantillaGeneradaError); si existe pero está mal
    formado, lanza ProxyIncorrectoError; si es válido, devuelve el dict de
    proxies listo para requests."""
    if not os.path.exists(ruta_csv):
        generar_proxy_plantilla(ruta_csv)
        raise ProxyPlantillaGeneradaError(
            f"No se encontró '{ruta_csv}'. Se generó una plantilla de ejemplo en esa ruta.\n"
            f"Complétala con los datos reales de tu proxy y vuelve a ejecutar el comando."
        )
    return leer_proxy_csv(ruta_csv)


def tamano_remoto(url, user_agent):
    """Intenta obtener el tamaño remoto vía HEAD (para poder saltar descargas completas)."""
    try:
        r = SESSION.head(url, headers={"User-Agent": user_agent}, timeout=20, allow_redirects=True)
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        return int(cl) if cl is not None else None
    except requests.RequestException:
        return None


def descargar_archivo(url, destino, user_agent, resume=True):
    """Descarga un archivo con barra de progreso simple. Salta si ya está completo."""
    os.makedirs(os.path.dirname(destino), exist_ok=True)

    remoto = tamano_remoto(url, user_agent)
    if resume and remoto is not None and os.path.exists(destino):
        local = os.path.getsize(destino)
        if local == remoto:
            log(f"  [OK, ya existe] {os.path.basename(destino)}")
            return
        elif local > remoto:
            log(f"  [Tamaño local mayor que el remoto, se vuelve a descargar] {os.path.basename(destino)}")

    try:
        with SESSION.get(url, headers={"User-Agent": user_agent}, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0))
            descargado = 0
            tmp = destino + ".part"
            with open(tmp, "wb") as f:
                ultimo_reporte = time.time()
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    f.write(chunk)
                    descargado += len(chunk)
                    if time.time() - ultimo_reporte > 2:
                        pct = f"{descargado * 100 / total:5.1f}%" if total else "?"
                        log(f"  ... {os.path.basename(destino)}: {pct} ({descargado/1_048_576:.1f} MB)")
                        ultimo_reporte = time.time()
            os.replace(tmp, destino)
            log(f"  [Descargado] {os.path.basename(destino)}")
    except requests.RequestException as e:
        log(f"  [ERROR] {os.path.basename(destino)}: {e}")


def recorrer(url, ruta_local, extensiones, user_agent, dry_run, max_depth, depth=0):
    """Recorre recursivamente el árbol remoto y devuelve la lista de (url, destino) a descargar."""
    tareas = []
    log(f"{'  ' * depth}[Carpeta] {url}")

    try:
        entradas = listar_directorio(url, user_agent)
    except requests.RequestException as e:
        log(f"{'  ' * depth}  [ERROR listando] {e}")
        return tareas

    for nombre, abs_url, es_dir in entradas:
        nombre_local = normalizar_nombre(nombre)
        if es_dir:
            if max_depth is not None and depth >= max_depth:
                continue
            subcarpeta = os.path.join(ruta_local, nombre_local)
            tareas.extend(
                recorrer(abs_url, subcarpeta, extensiones, user_agent, dry_run, max_depth, depth + 1)
            )
        else:
            _, ext = os.path.splitext(nombre_local)
            if ext.lower() in extensiones:
                destino = os.path.join(ruta_local, nombre_local)
                if dry_run:
                    log(f"{'  ' * (depth+1)}[Se descargaría] {nombre_local}")
                else:
                    tareas.append((abs_url, destino))
    return tareas


def main():
    ap = argparse.ArgumentParser(
        prog="descargador_repositorio",
        description="Descarga videos/subtítulos de un repositorio tipo 'Index of /'.",
        usage="%(prog)s [opciones] URL",
    )
    ap.add_argument("-o", "--output", default=".", help="Carpeta local donde recrear el árbol (por defecto: carpeta actual).")
    ap.add_argument("-e", "--ext", default="", help="Extensiones extra a incluir, separadas por coma. Ej: -e '.nfo,.jpg'")
    ap.add_argument("--solo-ext", default=None, help="Usar SOLO estas extensiones (coma-separadas), ignora las de por defecto.")
    ap.add_argument("--dry-run", action="store_true", help="No descarga nada, solo muestra qué haría.")
    ap.add_argument("--max-depth", type=int, default=None, help="Profundidad máxima de recursión.")
    ap.add_argument("--workers", type=int, default=2, help="Descargas simultáneas (por defecto: 2).")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (compatible; DescargadorRepositorio/1.0)")
    ap.add_argument("--no-resume", action="store_true", help="Vuelve a descargar todo aunque el archivo ya exista.")
    ap.add_argument("--use-proxy", action="store_true", help="Activa el uso de proxy (desactivado por defecto).")
    ap.add_argument("--proxy-file", default=PROXY_FILE_POR_DEFECTO,
                     help=f"Archivo .csv con los datos del proxy (por defecto: '{PROXY_FILE_POR_DEFECTO}'). Solo aplica junto con --use-proxy.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("url", help="URL de la carpeta raíz a descargar (serie, temporada o película). Va al final, después de las opciones.")
    args = ap.parse_args()

    if args.workers < 1:
        ap.error("--workers debe ser 1 o mayor.")

    if args.use_proxy:
        try:
            proxies = resolver_proxy(args.proxy_file)
        except ProxyPlantillaGeneradaError as e:
            log(str(e))
            sys.exit(1)
        except ProxyIncorrectoError as e:
            log(f"[ERROR] Proxy incorrecto: {e}")
            sys.exit(1)
        SESSION.proxies.update(proxies)
        log(f"Usando proxy: {proxies['http'].split('@')[-1]}")  # no mostramos usuario/contraseña en el log
    else:
        log("Proxy desactivado (usa --use-proxy para activarlo).")

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
        extensiones = set(DEFAULT_VIDEO_EXT) | set(DEFAULT_SUB_EXT)
        extensiones |= _parsear_ext(args.ext)

    # Nombre de la carpeta raíz local = último segmento no vacío de la URL
    ultimo_segmento = unquote(urlparse(url).path.rstrip("/").split("/")[-1]) or "descarga"
    raiz_local = os.path.join(args.output, normalizar_nombre(ultimo_segmento))

    log(f"URL raíz: {url}")
    log(f"Carpeta local: {raiz_local}")
    log(f"Extensiones a descargar: {sorted(extensiones)}")
    if args.dry_run:
        log("Modo DRY-RUN: no se descargará nada, solo se listará.\n")

    tareas = recorrer(url, raiz_local, extensiones, args.user_agent, args.dry_run, args.max_depth)

    if args.dry_run:
        return

    if not tareas:
        log("\nNo se encontraron archivos nuevos para descargar.")
        return

    log(f"\nIniciando descarga de {len(tareas)} archivo(s) con {args.workers} hilo(s)...\n")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futuros = {
            executor.submit(descargar_archivo, u, d, args.user_agent, not args.no_resume): (u, d)
            for u, d in tareas
        }
        for fut in as_completed(futuros):
            fut.result()  # propaga cualquier excepción no capturada

    log("\nListo.")


if __name__ == "__main__":
    main()

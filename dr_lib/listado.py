"""Parseo de páginas 'Index of /' (con un parser HTML real, no regex) y
exploración recursiva del árbol remoto."""
import os
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin

import requests

from .utils import SESSION, CANCELAR, log, parsear_tamano, formatear_bytes, normalizar_nombre


class ListadoParser(HTMLParser):
    """Extrae de cada <a href="..."> el propio href y el texto que aparece
    después de él en la misma línea (fecha + tamaño en los listados estilo
    Apache), hasta el siguiente <a>."""

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
    (nombre, url_absoluta, es_dir, tamano_aprox_bytes)."""
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


def explorar(url, ruta_local, extensiones, user_agent, max_depth, depth=0):
    """Generador que produce eventos:
        ("carpeta", url)
        ("archivo", url_absoluta, ruta_local_destino, tamano_aprox_o_None)
        ("error", url, mensaje)
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
            log(f"  [ERROR listando] {evento[2]}", color="red")
        else:
            _, abs_url, destino, tamano_aprox = evento
            total_archivos += 1
            if tamano_aprox:
                total_bytes += tamano_aprox
            log(f"  [Se descargaría] {os.path.basename(destino)} ({formatear_bytes(tamano_aprox)})")
    log(f"\nTotal: {total_archivos} archivo(s), ~{formatear_bytes(total_bytes)} (estimado a partir del listado).", color="cyan")

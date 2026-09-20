"""Descarga de archivos individuales: resume real con HTTP Range, reintentos
automáticos, límite de velocidad opcional, barra de progreso y colores."""
import os
import threading
import time

import requests
from tqdm import tqdm

from .utils import SESSION, CANCELAR, log, obtener_slot_de_este_hilo

CHUNK_SIZE = 512 * 1024  # 512 KiB
TOLERANCIA_MINIMA_BYTES = 1024 * 1024  # 1 MiB


class LimitadorVelocidad:
    """Límite de velocidad agregado, compartido entre todos los hilos de
    descarga (no es un límite exacto por hilo, sino un total aproximado)."""

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


class Estadisticas:
    """Contadores del resumen final. Con lock porque varios hilos escriben
    a la vez (y en Python free-threaded ya no hay GIL que lo proteja solo)."""

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


def descargar_archivo(url, destino, tamano_aprox, cfg, stats, limitador, barra_total):
    """cfg: objeto con .workers, .retries, .user_agent, .no_resume, .quiet
    (ver types.SimpleNamespace armado en main())."""
    nombre = os.path.basename(destino)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    tmp = destino + ".part"

    # 1) ¿Ya está completo? Lo resolvemos SIN red, usando el tamaño
    #    aproximado que ya trae el listado (evita un HEAD extra por archivo).
    if not cfg.no_resume and os.path.exists(destino) and tamano_aprox:
        tolerancia = max(TOLERANCIA_MINIMA_BYTES, int(tamano_aprox * 0.02))
        if abs(os.path.getsize(destino) - tamano_aprox) <= tolerancia:
            log(f"[ya existe] {nombre}", min_nivel=1, color="cyan")
            stats.sumar_omitido()
            return

    if cfg.no_resume:
        for ruta in (destino, tmp):
            if os.path.exists(ruta):
                os.remove(ruta)

    intentos = 0
    while True:
        if CANCELAR.is_set():
            return
        intentos += 1
        offset = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        headers = {"User-Agent": cfg.user_agent}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"

        barra = None
        try:
            with SESSION.get(url, headers=headers, stream=True, timeout=60) as r:
                if offset > 0 and r.status_code == 416:
                    # El servidor dice "ese rango no existe": probablemente ya
                    # está completo. Confirmamos con una petición normal.
                    r.close()
                    with SESSION.get(url, headers={"User-Agent": cfg.user_agent}, stream=True, timeout=30) as r2:
                        total_real = int(r2.headers.get("Content-Length") or 0)
                    if total_real and offset >= total_real:
                        os.replace(tmp, destino)
                        log(f"[completado] {nombre}", min_nivel=1, color="green")
                        stats.sumar_completado(total_real)
                        return
                    os.remove(tmp)  # el .part quedó corrupto, reiniciar
                    continue

                modo = "ab" if (offset > 0 and r.status_code == 206) else "wb"
                if offset > 0 and r.status_code == 200:
                    offset = 0  # el servidor ignoró el Range: reiniciar

                r.raise_for_status()

                if r.status_code == 206:
                    content_range = r.headers.get("Content-Range", "")
                    total = int(content_range.split("/")[-1]) if "/" in content_range else None
                else:
                    cl = r.headers.get("Content-Length")
                    total = int(cl) if cl is not None else None

                if not cfg.quiet:
                    slot = obtener_slot_de_este_hilo(max(cfg.workers, 1))
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
            log(f"[descargado] {nombre}", min_nivel=1, color="green")
            stats.sumar_completado(os.path.getsize(destino))
            return

        except requests.RequestException as e:
            if intentos > cfg.retries:
                log(f"[ERROR] {nombre}: {e} (después de {intentos} intento(s), se conserva el .part para reanudar)", color="red")
                stats.sumar_fallido(nombre)
                return
            espera = min(30, 2**intentos)
            log(f"[reintentando] {nombre}: {e} -> intento {intentos + 1}/{cfg.retries + 1} en {espera}s", min_nivel=1, color="yellow")
            time.sleep(espera)
        finally:
            if barra:
                barra.close()

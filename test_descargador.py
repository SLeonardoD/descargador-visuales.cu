"""
Tests unitarios de descargador_repositorio.py.
No requieren red: prueban únicamente las funciones puras (parseo de HTML,
parseo de tamaños, normalización de nombres, parseo del csv de proxy).

Correr con: pytest tests/
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import descargador_repositorio as dr  # noqa: E402


# ---------------------------------------------------------------------------
# parsear_tamano
# ---------------------------------------------------------------------------
def test_parsear_tamano_con_unidades():
    assert dr.parsear_tamano("703M") == 703 * 1024**2
    assert dr.parsear_tamano("18K") == 18 * 1024
    assert dr.parsear_tamano("2G") == 2 * 1024**3


def test_parsear_tamano_bytes_planos():
    assert dr.parsear_tamano("2344") == 2344


def test_parsear_tamano_guion_es_none():
    assert dr.parsear_tamano("-") is None


def test_parsear_tamano_vacio_es_none():
    assert dr.parsear_tamano("") is None
    assert dr.parsear_tamano(None) is None


def test_parsear_tamano_basura_es_none():
    assert dr.parsear_tamano("no-es-un-tamano") is None


# ---------------------------------------------------------------------------
# normalizar_nombre
# ---------------------------------------------------------------------------
def test_normalizar_nombre_quita_caracteres_prohibidos():
    assert dr.normalizar_nombre('nombre:raro?"con<caracteres>') == "nombre_raro__con_caracteres_"


def test_normalizar_nombre_quita_puntos_finales():
    assert dr.normalizar_nombre("nombre.") == "nombre"
    assert dr.normalizar_nombre("nombre ") == "nombre"


# ---------------------------------------------------------------------------
# parsear_listado_html (parseo real contra HTML estilo Apache)
# ---------------------------------------------------------------------------
HTML_SERIE = """
<html><body><h1>Index of /Series/Espanol/Los Hombres de Paco/</h1><hr><pre>
<a href="https://sitio.test/Series/Espanol/">../</a>
<a href="https://sitio.test/Series/Espanol/Los%20Hombres%20de%20Paco/Los%20Hombres%20de%20Paco%20x%201/">Los Hombres de Paco x 1/</a>   16-Jun-2021 11:33       -
<a href="https://sitio.test/Series/Espanol/Los%20Hombres%20de%20Paco/Disco-1.jpg">Disco-1.jpg</a>   20-Feb-2014 00:56    221K
</pre><hr></body></html>
"""

URL_SERIE = "https://sitio.test/Series/Espanol/Los%20Hombres%20de%20Paco/"


def test_parsear_listado_ignora_parent_dir():
    entradas = dr.parsear_listado_html(HTML_SERIE, URL_SERIE)
    nombres = [n for n, _, _, _ in entradas]
    assert "../" not in nombres and ".." not in nombres


def test_parsear_listado_detecta_subcarpeta():
    entradas = dr.parsear_listado_html(HTML_SERIE, URL_SERIE)
    dirs = [(n, es_dir) for n, _, es_dir, _ in entradas if es_dir]
    assert ("Los Hombres de Paco x 1", True) in dirs


def test_parsear_listado_detecta_archivo_y_tamano():
    entradas = dr.parsear_listado_html(HTML_SERIE, URL_SERIE)
    archivos = {n: (es_dir, tam) for n, _, es_dir, tam in entradas if not es_dir}
    assert archivos["Disco-1.jpg"] == (False, 221 * 1024)


def test_parsear_listado_decodifica_percent_encoding():
    entradas = dr.parsear_listado_html(HTML_SERIE, URL_SERIE)
    nombres = [n for n, _, _, _ in entradas]
    assert "Los Hombres de Paco x 1" in nombres  # sin %20


HTML_TEMPORADA = """
<html><body><pre>
<a href="../">../</a>
<a href="Los%20hombres%20de%20Paco%2001%20La%20Suerte.avi">Los hombres de Paco 01 La Suerte.avi</a>   04-Jun-2007 17:46    703M
<a href="Los%20hombres%20de%20Paco%2001%20La%20Suerte-thumb.jpg">Los hombres de Paco 01 La Suerte-thumb.jpg</a>   07-May-2021 11:14     18K
<a href="sinopsis.txt">sinopsis.txt</a>   27-Jan-2014 06:19     625
</pre></body></html>
"""
URL_TEMPORADA = "https://sitio.test/Series/Mi%20Serie/Temporada%201/"


def test_parsear_listado_temporada_tamanos_correctos():
    entradas = dr.parsear_listado_html(HTML_TEMPORADA, URL_TEMPORADA)
    tam = {n: t for n, _, _, t in entradas}
    assert tam["Los hombres de Paco 01 La Suerte.avi"] == 703 * 1024**2
    assert tam["sinopsis.txt"] == 625


def test_filtro_extensiones_simula_solo_avi():
    entradas = dr.parsear_listado_html(HTML_TEMPORADA, URL_TEMPORADA)
    seleccionados = [n for n, _, es_dir, _ in entradas if not es_dir and n.lower().endswith(".avi")]
    assert seleccionados == ["Los hombres de Paco 01 La Suerte.avi"]


def test_parsear_listado_ignora_enlaces_fuera_de_la_carpeta():
    # Un link de "sort" tipo ?C=N;O=D no debería colarse como entrada real
    html = '<pre><a href="?C=N;O=D">Name</a>\n<a href="archivo.mp4">archivo.mp4</a>   01-Jan-2024 00:00   10M</pre>'
    entradas = dr.parsear_listado_html(html, "https://sitio.test/Carpeta/")
    nombres = [n for n, _, _, _ in entradas]
    assert nombres == ["archivo.mp4"]


# ---------------------------------------------------------------------------
# Proxy: generar plantilla y leerla
# ---------------------------------------------------------------------------
def test_generar_y_leer_proxy_valido(tmp_path):
    ruta = tmp_path / "proxy.csv"
    dr.generar_proxy_plantilla(str(ruta))
    proxies = dr.leer_proxy_csv(str(ruta))
    assert proxies["http"].startswith("http://miusuario:miclave@10.0.0.5:8080")
    assert proxies["http"] == proxies["https"]


def test_proxy_sin_usuario_ni_contrasena(tmp_path):
    ruta = tmp_path / "proxy.csv"
    ruta.write_text("host,puerto\n192.168.1.10,3128\n")
    proxies = dr.leer_proxy_csv(str(ruta))
    assert proxies["http"] == "http://192.168.1.10:3128"


def test_proxy_con_caracteres_especiales_en_contrasena(tmp_path):
    ruta = tmp_path / "proxy.csv"
    ruta.write_text("host,puerto,usuario,contrasena\n10.0.0.5,8080,user,mi:clave@rara\n")
    proxies = dr.leer_proxy_csv(str(ruta))
    assert "mi%3Aclave%40rara" in proxies["http"]


def test_proxy_puerto_invalido_lanza_error(tmp_path):
    ruta = tmp_path / "proxy.csv"
    ruta.write_text("host,puerto\n10.0.0.5,no-es-un-puerto\n")
    try:
        dr.leer_proxy_csv(str(ruta))
        assert False, "Debería haber lanzado ProxyIncorrectoError"
    except dr.ProxyIncorrectoError:
        pass


def test_proxy_esquema_invalido_lanza_error(tmp_path):
    ruta = tmp_path / "proxy.csv"
    ruta.write_text("host,puerto,usuario,contrasena,esquema\n10.0.0.5,8080,,,ftp\n")
    try:
        dr.leer_proxy_csv(str(ruta))
        assert False, "Debería haber lanzado ProxyIncorrectoError"
    except dr.ProxyIncorrectoError:
        pass


def test_proxy_sin_filas_de_datos_lanza_error(tmp_path):
    ruta = tmp_path / "proxy.csv"
    ruta.write_text("host,puerto,usuario,contrasena\n")
    try:
        dr.leer_proxy_csv(str(ruta))
        assert False, "Debería haber lanzado ProxyIncorrectoError"
    except dr.ProxyIncorrectoError:
        pass


def test_resolver_proxy_genera_plantilla_si_no_existe(tmp_path):
    ruta = tmp_path / "no_existe_todavia.csv"
    try:
        dr.resolver_proxy(str(ruta))
        assert False, "Debería haber lanzado ProxyPlantillaGeneradaError"
    except dr.ProxyPlantillaGeneradaError:
        pass
    assert ruta.exists()


# ---------------------------------------------------------------------------
# LimitadorVelocidad y Estadisticas: comportamiento básico, sin red
# ---------------------------------------------------------------------------
def test_limitador_sin_limite_no_espera():
    limitador = dr.LimitadorVelocidad(None)
    import time
    inicio = time.monotonic()
    limitador.consumir(10_000_000)
    assert time.monotonic() - inicio < 0.1


def test_estadisticas_conteos():
    stats = dr.Estadisticas()
    stats.sumar_completado(1000)
    stats.sumar_completado(500)
    stats.sumar_omitido()
    stats.sumar_fallido("archivo.avi")
    assert stats.completados == 2
    assert stats.bytes_descargados == 1500
    assert stats.omitidos == 1
    assert stats.fallidos == 1
    assert stats.nombres_fallidos == ["archivo.avi"]


def test_formatear_bytes():
    assert dr.formatear_bytes(500) == "500 B"
    assert dr.formatear_bytes(1536) == "1.5 KB"
    assert dr.formatear_bytes(None) == "?"

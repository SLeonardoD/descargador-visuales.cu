"""Manejo de config-file.json: un único archivo opcional que puede fijar
CUALQUIERA de las flags de línea de comandos (y opcionalmente la URL y los
datos de un proxy), sin necesidad de repetirlas cada vez.

Precedencia: valor pasado explícitamente por línea de comandos > valor en
el config-file.json > valor por defecto del programa.
"""
import json
import os
from urllib.parse import quote

# Valores por defecto "reales" del programa (no confundir con los defaults
# de argparse, que se ponen en None para poder detectar "no se pasó por CLI").
DEFAULTS = {
    "output": ".",
    "ext": "",
    "solo_ext": None,
    "dry_run": False,
    "max_depth": None,
    "workers": 2,
    "retries": 3,
    "rate_limit": None,
    "user_agent": "Mozilla/5.0 (compatible; DescargadorRepositorio/1.0)",
    "no_resume": False,
    "quiet": False,
    "verbose": 0,
    "url": None,
}

# Descripción de cada campo, usada para generar el archivo de ejemplo.
AYUDA = {
    "output": "Carpeta local donde recrear el árbol de carpetas.",
    "ext": "Extensiones extra a incluir además de las de por defecto, separadas por coma. Ej: '.nfo,.jpg'",
    "solo_ext": "Si se define (string), IGNORA las extensiones por defecto y usa SOLO estas (coma-separadas). null = no usar esta opción.",
    "dry_run": "true = no descarga nada, solo muestra el árbol y tamaños estimados.",
    "max_depth": "Profundidad máxima de recursión (número entero). null = sin límite.",
    "workers": "Cantidad de descargas simultáneas (número entero >= 1).",
    "retries": "Reintentos por archivo ante error de red (número entero >= 0).",
    "rate_limit": "Límite de velocidad agregado, en KB/s (número). null = sin límite.",
    "user_agent": "User-Agent a enviar en las peticiones HTTP (texto).",
    "no_resume": "true = ignora archivos/.part existentes, descarga todo desde cero.",
    "quiet": "true = solo errores y el resumen final, sin barras de progreso.",
    "verbose": "Nivel de detalle: 0 normal, 1 detallado, 2 muy detallado.",
    "url": "URL de la carpeta raíz a descargar. Si se define acá, no hace falta pasarla por línea de comandos.",
    "proxy": "Objeto OPCIONAL: host, puerto, usuario, contrasena, esquema (http/https/socks5/socks5h). Se puede omitir este campo (o todo el archivo) para no usar proxy.",
}

CAMPOS_SIMPLES = list(DEFAULTS.keys())
PROXY_ESQUEMAS_VALIDOS = ("http", "https", "socks5", "socks5h")

_TIPOS_ESPERADOS = {
    "output": str, "ext": str, "solo_ext": str, "dry_run": bool,
    "max_depth": int, "workers": int, "retries": int, "rate_limit": (int, float),
    "user_agent": str, "no_resume": bool, "quiet": bool, "verbose": int, "url": str,
}


class ConfigIncorrectoError(Exception):
    """El archivo de configuración existe pero tiene datos inválidos."""


class ConfigPlantillaGeneradaError(Exception):
    """No existía el archivo de configuración y se acaba de generar una plantilla."""


def generar_config_ejemplo(ruta):
    """Escribe un config-file.json de ejemplo con TODAS las flags
    documentadas y un valor de muestra para cada una (incluyendo proxy)."""
    contenido = {
        "_info": (
            "Ejemplo generado por --config-example. Copiá/renombrá este archivo a "
            "'config-file.json' (o al nombre que le pases a --config-file), editá los "
            "valores que te interesen fijar y borrá (o dejá en su default) los que no. "
            "Las claves que empiezan con '_' son solo documentación y se ignoran al leer "
            "el archivo, así que podés incluso dejar este mismo archivo tal cual."
        ),
        "_ayuda": AYUDA,
        **DEFAULTS,
        "url": "https://sitio/Series/Mi Serie/",
        "proxy": {
            "host": "10.0.0.5",
            "puerto": 8080,
            "usuario": "miusuario",
            "contrasena": "miclave",
            "esquema": "http",
        },
    }
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(contenido, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _validar_tipo(nombre, valor, tipos, ruta):
    if valor is not None and not isinstance(valor, tipos):
        raise ConfigIncorrectoError(
            f"El campo '{nombre}' en '{ruta}' tiene un tipo inválido "
            f"(se esperaba {tipos}, se recibió {type(valor).__name__})."
        )


def _validar_proxy(proxy_dict, ruta):
    if proxy_dict is None:
        return None
    if not isinstance(proxy_dict, dict):
        raise ConfigIncorrectoError(f"El campo 'proxy' en '{ruta}' debe ser un objeto JSON.")

    host = proxy_dict.get("host")
    puerto = proxy_dict.get("puerto")
    usuario = str(proxy_dict.get("usuario") or "")
    contrasena = str(proxy_dict.get("contrasena") or "")
    esquema = str(proxy_dict.get("esquema") or "http").lower()

    if not host or not isinstance(host, str):
        raise ConfigIncorrectoError(f"El campo 'proxy.host' en '{ruta}' es obligatorio y debe ser texto.")
    if not isinstance(puerto, int) or isinstance(puerto, bool):
        raise ConfigIncorrectoError(f"El campo 'proxy.puerto' en '{ruta}' debe ser un número entero.")
    if not (1 <= puerto <= 65535):
        raise ConfigIncorrectoError(f"El campo 'proxy.puerto' en '{ruta}' está fuera de rango (1-65535).")
    if esquema not in PROXY_ESQUEMAS_VALIDOS:
        raise ConfigIncorrectoError(f"El campo 'proxy.esquema' en '{ruta}' no es válido: '{esquema}'.")

    credenciales = f"{quote(usuario, safe='')}:{quote(contrasena, safe='')}@" if usuario else ""
    proxy_url = f"{esquema}://{credenciales}{host}:{puerto}"
    return {"http": proxy_url, "https": proxy_url}


def leer_config_json(ruta):
    """Lee y valida config-file.json. Devuelve (datos, proxies_o_None, advertencias)."""
    try:
        with open(ruta, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ConfigIncorrectoError(f"No se pudo abrir '{ruta}': {e}") from e
    except json.JSONDecodeError as e:
        raise ConfigIncorrectoError(f"'{ruta}' no es un JSON válido: {e}") from e

    if not isinstance(data, dict):
        raise ConfigIncorrectoError(f"'{ruta}' debe contener un objeto JSON en la raíz.")

    claves_conocidas = set(CAMPOS_SIMPLES) | {"proxy"}
    advertencias = [c for c in data if not c.startswith("_") and c not in claves_conocidas]

    for campo, tipos in _TIPOS_ESPERADOS.items():
        if campo in data:
            _validar_tipo(campo, data[campo], tipos, ruta)

    if data.get("workers") is not None and data["workers"] < 1:
        raise ConfigIncorrectoError(f"El campo 'workers' en '{ruta}' debe ser 1 o mayor.")
    if data.get("retries") is not None and data["retries"] < 0:
        raise ConfigIncorrectoError(f"El campo 'retries' en '{ruta}' debe ser 0 o mayor.")

    proxies = _validar_proxy(data.get("proxy"), ruta)
    return data, proxies, advertencias


def resolver_config(ruta):
    """Si el archivo no existe, genera una plantilla completa ahí mismo y
    avisa (ConfigPlantillaGeneradaError); si existe pero es inválido, lanza
    ConfigIncorrectoError; si es válido, devuelve (datos, proxies, advertencias)."""
    if not os.path.exists(ruta):
        generar_config_ejemplo(ruta)
        raise ConfigPlantillaGeneradaError(
            f"No se encontró '{ruta}'. Se generó una plantilla completa en esa ruta.\n"
            f"Editala con los valores que quieras fijar y volvé a ejecutar el comando."
        )
    return leer_config_json(ruta)

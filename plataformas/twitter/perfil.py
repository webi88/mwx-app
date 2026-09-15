"""Lectura y sincronizacion del perfil real de cuentas de X (Twitter).

Este modulo permite, SIN abrir Chrome:
1. Parsear el HTML publico de x.com para extraer nombre visible + @handle
   (`extraer_datos_perfil`).
2. Leer el perfil real de una cuenta usando sus cookies guardadas
   (`auth_token` + `ct0`) por HTTP (`obtener_perfil_http`): la pagina HTML se
   pide SIEMPRE con headers de navegador (`_HEADERS_BASICOS`) y, cuando hay
   sesion, se agrega el header `cookie: auth_token=...; ct0=...`. NO se usan
   los headers de API de `session_validator.construir_headers`
   (`authorization`/`x-twitter-auth-type`): X responde 401 a la pagina HTML
   cuando se envian.
3. Sincronizar en base de datos los campos `nombre_mostrado` y `handle_actual`
   (`sincronizar_cuenta`, `sincronizar_todas`), porque otras herramientas
   pueden haber renombrado las cuentas en X.

La interfaz de este modulo esta CONGELADA: la consume el dashboard web.
Todas las funciones publicas devuelven `None`/diccionario y NUNCA lanzan
excepciones (registran en `logger` y devuelven un resultado vacio).
"""
import html as _html
import json
import re
from typing import Optional

import httpx
from loguru import logger

from core.config import settings
from core.database import get_db_session
from core.models import Cuenta
from plataformas.twitter.session_validator import (
    _session_id_por_cuenta,
    extraer_ct0,
    obtener_ct0,
)

X_BASE = "https://x.com"

_UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Headers de navegador para las lecturas anonimas (sin sesion).
_HEADERS_BASICOS = {
    "user-agent": _UA_CHROME,
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "accept-language": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "upgrade-insecure-requests": "1",
}

_RE_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# <meta property="og:title" content="NOMBRE (@handle) / X"> (o name=, o comillas simples)
_RE_OG_TITLE = re.compile(
    r"<meta[^>]+(?:property|name)\s*=\s*[\"']og:title[\"'][^>]*>",
    re.IGNORECASE,
)
_RE_META_CONTENT = re.compile(r"content\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_RE_TITULO_PERFIL = re.compile(
    r"^(?P<nombre>.*?)\s*\(@(?P<handle>[A-Za-z0-9_]{1,15})\)",
    re.DOTALL,
)

# Fallback: JSON embebido de la pagina de perfil.
_RE_JSON_SCREEN_NAME = re.compile(r'"screen_name"\s*:\s*"([A-Za-z0-9_]{1,15})"')
_RE_JSON_NAME = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.){1,200})"')

# Paginas de suspendida/bloqueada que no deben confundirse con un perfil.
_FRASES_BLOQUEO = (
    "account suspended",
    "cuenta suspendida",
    "something went wrong",
    "this account doesn't exist",
    "this account doesn’t exist",
    "esta cuenta no existe",
)


def _armar_perfil(nombre: str, handle: str) -> Optional[dict]:
    """Valida y normaliza el par (nombre, handle); devuelve None si es invalido."""
    handle = (handle or "").strip().lstrip("@")
    nombre = (nombre or "").strip()
    if not handle or not _RE_HANDLE.match(handle):
        return None
    return {"nombre": nombre, "handle": handle}


def _decodificar_json_texto(texto: str) -> str:
    """Decodifica los escapes JSON de un string embebido (\\u00e9, \\", etc.)."""
    try:
        return json.loads(f'"{texto}"')
    except Exception:
        return _html.unescape(texto)


def extraer_datos_perfil(html: str) -> Optional[dict]:
    """Extrae {"nombre": str, "handle": str} del HTML de un perfil de x.com.

    Estrategia (en orden):
    1. `<meta property="og:title" content="NOMBRE (@handle) / X">`
       (tambien `name=og:title` y comillas simples).
    2. Regex sobre el JSON embebido: `"screen_name":"handle"` y `"name":"NOMBRE"`.

    Usa `html.unescape` para las entidades HTML. Devuelve None si no hay datos
    validos (handle vacio o con formato invalido).
    """
    if not html:
        return None

    # 1) og:title
    try:
        for meta in _RE_OG_TITLE.finditer(html):
            m = _RE_META_CONTENT.search(meta.group(0))
            if not m:
                continue
            titulo = _html.unescape(m.group(1)).strip()
            pm = _RE_TITULO_PERFIL.match(titulo)
            if pm:
                perfil = _armar_perfil(pm.group("nombre"), pm.group("handle"))
                if perfil:
                    return perfil
    except Exception as e:
        logger.debug(f"og:title de perfil no parseable: {e}")

    # 2) JSON embebido
    try:
        sm = _RE_JSON_SCREEN_NAME.search(html)
        if sm:
            pos_handle = sm.start()
            inicio = max(0, pos_handle - 4000)
            fin = min(len(html), sm.end() + 500)
            ventana = html[inicio:fin]
            pos_handle_ventana = pos_handle - inicio

            candidato = None
            candidato_antes = None
            for nm in _RE_JSON_NAME.finditer(ventana):
                if nm.start() <= pos_handle_ventana:
                    candidato_antes = nm
                else:
                    candidato = nm
                    break
            elegido = candidato_antes or candidato

            nombre = _decodificar_json_texto(elegido.group(1)) if elegido else ""
            perfil = _armar_perfil(nombre, sm.group(1))
            if perfil:
                return perfil
    except Exception as e:
        logger.debug(f"JSON de perfil no parseable: {e}")

    return None


def _proxy_de_cuenta(cuenta) -> str:
    """Proxy para la lectura HTTP: el de la cuenta o el sticky MX determinista.

    NO valida `x_accesible` a proposito: la sincronizacion es de bajo impacto y
    la validacion completa la hace `session_validator._proxy_operativo`.
    """
    proxy = (getattr(cuenta, "proxy", "") or "").strip()
    if proxy:
        return proxy
    usuario = (getattr(cuenta, "usuario", "") or "").strip()
    try:
        return settings.proxy_sticky_mx(session_id=_session_id_por_cuenta(usuario))
    except Exception as e:
        logger.debug(f"No se pudo construir el proxy sticky de {usuario}: {e}")
        return ""


def _candidatos_perfil(
    usuario: str,
    handle_actual: str = "",
    handle_propuesto: str = "",
) -> list:
    """@handle a consultar en orden, sin repetidos (case-insensitive).

    Incluye el `usuario` interno de BD y, como fallback, el `handle_actual` y
    el `handle_propuesto`: cuando una cuenta fue renombrada en X, el handle
    viejo puede devolver 404 y solo el nuevo resuelve la pagina.
    """
    candidatos = []
    vistos = set()
    for valor in (usuario, handle_actual, handle_propuesto):
        valor = (valor or "").strip().lstrip("@")
        if not valor or valor.lower() in vistos:
            continue
        vistos.add(valor.lower())
        candidatos.append(valor)
    return candidatos


def _leer_html_perfil(
    candidato: str,
    headers: dict,
    proxy,
    timeout: int,
) -> Optional[dict]:
    """GET de https://x.com/{candidato} y extraccion de nombre/@ del HTML.

    Devuelve el dict de `extraer_datos_perfil` o None (HTTP >= 400, pagina de
    suspendida/bloqueada o HTML sin datos parseables). No decide candidatos:
    eso es responsabilidad de `obtener_perfil_http`.
    """
    url = f"{X_BASE}/{candidato}"
    try:
        resp = httpx.get(
            url,
            headers=headers,
            proxy=proxy,
            timeout=timeout,
            follow_redirects=True,
        )
    except TypeError:
        # httpx viejo no acepta `proxy=` (usaba `proxies=`): se reintenta sin proxy.
        logger.warning("httpx sin soporte para 'proxy='; se consulta sin proxy")
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)

    if resp.status_code >= 400:
        logger.info(f"Perfil de {candidato}: HTTP {resp.status_code}")
        return None

    texto = resp.text or ""
    datos = extraer_datos_perfil(texto)
    if datos:
        return datos

    # Sin datos validos: comprobar si es una pagina de suspendida/bloqueada.
    bajo = texto[:200000].lower()
    for frase in _FRASES_BLOQUEO:
        if frase in bajo:
            logger.warning(
                f"Perfil de {candidato}: pagina bloqueada/suspendida ('{frase}')"
            )
            return None

    logger.info(f"Perfil de {candidato}: sin datos parseables en el HTML")
    return None


def obtener_perfil_http(
    usuario: str,
    auth_token: str = "",
    ct0: str = "",
    proxy: str = "",
    timeout: int = 20,
    handle_actual: str = "",
    handle_propuesto: str = "",
) -> Optional[dict]:
    """Lee el perfil real de `usuario` en x.com por HTTP (sin Chrome).

    - GET https://x.com/{candidato} con `follow_redirects=True`.
    - SIEMPRE usa headers de navegador (`_HEADERS_BASICOS`); si hay
      `auth_token`, agrega el header `cookie: auth_token=...; ct0=...`. Los
      headers de API de `session_validator.construir_headers`
      (`authorization`/`x-twitter-auth-type`) NO se usan aqui: X responde 401
      a la pagina HTML con ellos.
    - Si hay `auth_token` pero falta `ct0`, lo pide a x.com con `obtener_ct0`.
    - `handle_actual`/`handle_propuesto` (opcionales) se prueban como
      candidatos cuando la URL del `usuario` no devuelve datos.
    - Detecta paginas de suspendida/bloqueada y devuelve None.

    Devuelve {"nombre", "handle", "consultado"} o None.
    Nunca lanza excepcion: loguea y devuelve None.
    """
    usuario = (usuario or "").strip().lstrip("@")
    if not usuario:
        logger.warning("obtener_perfil_http: usuario vacio")
        return None

    auth_token = (auth_token or "").strip()
    ct0 = (ct0 or "").strip()
    proxy = (proxy or "").strip() or None
    candidatos = _candidatos_perfil(usuario, handle_actual, handle_propuesto)

    try:
        # Sin ct0 la pagina autenticada de X no responde: se pide uno fresco.
        if auth_token and not ct0:
            ct0 = obtener_ct0(proxy=proxy, timeout=timeout) or ""

        # Headers de NAVEGADOR (pagina HTML). Con sesion se agrega la cookie;
        # jamás los headers de API (authorization/x-twitter-auth-type -> 401).
        headers = dict(_HEADERS_BASICOS)
        if auth_token:
            cookie = f"auth_token={auth_token}"
            if ct0:
                cookie += f"; ct0={ct0}"
            headers["cookie"] = cookie

        for candidato in candidatos:
            datos = _leer_html_perfil(candidato, headers, proxy, timeout)
            if not datos:
                continue
            datos["consultado"] = candidato
            if candidato.lower() != usuario.lower():
                logger.info(
                    f"Perfil de {usuario}: resuelto por el candidato @{candidato}"
                )
            return datos

        logger.info(
            f"Perfil de {usuario}: sin datos parseables en el HTML "
            f"(candidatos probados: {', '.join(candidatos)})"
        )
        return None

    except Exception as e:
        logger.warning(f"Error leyendo el perfil HTTP de {usuario}: {str(e)[:200]}")
        return None


def sincronizar_cuenta(cuenta, timeout: int = 20) -> dict:
    """Sincroniza `nombre_mostrado` y `handle_actual` de una cuenta en BD.

    `cuenta` es un objeto `core.models.Cuenta`. Ademas del `usuario` interno,
    se consultan como fallback sus `handle_actual` y `handle_propuesto` (por
    si la cuenta fue renombrada en X y el handle viejo da 404). Devuelve:
    {"usuario", "nombre", "handle", "consultado", "cambio_nombre",
    "cambio_handle", "error"} donde `error` es "" si todo ok, "sin_datos" si
    no se pudo leer el perfil, o el detalle del fallo. NUNCA lanza excepcion.
    """
    resultado = {
        "usuario": "",
        "nombre": "",
        "handle": "",
        "consultado": "",
        "cambio_nombre": False,
        "cambio_handle": False,
        "error": "",
    }
    try:
        usuario = (getattr(cuenta, "usuario", "") or "").strip()
        resultado["usuario"] = usuario
        if not usuario:
            resultado["error"] = "cuenta sin usuario"
            return resultado

        auth_token = (getattr(cuenta, "auth_token", "") or "").strip()
        ct0 = extraer_ct0(getattr(cuenta, "cookies_json", None))
        proxy = _proxy_de_cuenta(cuenta)
        handle_actual = (getattr(cuenta, "handle_actual", "") or "").strip()
        handle_propuesto = (getattr(cuenta, "handle_propuesto", "") or "").strip()

        datos = obtener_perfil_http(
            usuario,
            auth_token=auth_token,
            ct0=ct0,
            proxy=proxy,
            timeout=timeout,
            handle_actual=handle_actual,
            handle_propuesto=handle_propuesto,
        )
        if not datos:
            resultado["error"] = "sin_datos"
            return resultado

        nombre = (datos.get("nombre") or "").strip()
        handle = (datos.get("handle") or "").strip().lstrip("@")
        resultado["nombre"] = nombre
        resultado["handle"] = handle
        resultado["consultado"] = (datos.get("consultado") or "").strip()

        # Comparacion antes/despues con lo guardado en BD.
        antes_nombre = (getattr(cuenta, "nombre_mostrado", "") or "").strip()
        antes_handle = (getattr(cuenta, "handle_actual", "") or "").strip().lstrip("@")
        resultado["cambio_nombre"] = bool(nombre) and nombre != antes_nombre
        resultado["cambio_handle"] = bool(handle) and handle != antes_handle

        actualizar = {}
        if nombre:
            actualizar["nombre_mostrado"] = nombre
        if handle:
            actualizar["handle_actual"] = handle

        if actualizar:
            try:
                with get_db_session() as db:
                    reg = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
                    if reg is not None:
                        for campo, valor in actualizar.items():
                            if hasattr(Cuenta, campo):
                                setattr(reg, campo, valor)
            except Exception as e:
                logger.error(f"Error actualizando perfil de {usuario} en BD: {e}")
                resultado["error"] = f"bd: {str(e)[:150]}"

            # Refleja los valores nuevos en el objeto en memoria.
            for campo, valor in actualizar.items():
                try:
                    setattr(cuenta, campo, valor)
                except Exception:
                    pass

        return resultado

    except Exception as e:
        logger.exception(
            f"Error sincronizando perfil de {getattr(cuenta, 'usuario', '?')}: {e}"
        )
        resultado["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        return resultado


def sincronizar_todas(solo_status=None, seccion=None, callback=None) -> dict:
    """Sincroniza los perfiles de TODAS las cuentas de twitter en BD.

    `solo_status`: filtra por `Cuenta.status` (ej. "active").
    `seccion`: filtra por `Cuenta.seccion` (solo si el modelo ya la tiene).
    `callback(actual, total, usuario, resultado)` se invoca por cuenta.

    Devuelve {"total", "ok", "errores", "sin_datos", "cambios_nombre",
    "cambios_handle", "detalle": [resultado, ...]}. Nunca lanza excepcion.
    """
    resumen = {
        "total": 0,
        "ok": 0,
        "errores": 0,
        "sin_datos": 0,
        "cambios_nombre": 0,
        "cambios_handle": 0,
        "detalle": [],
    }

    try:
        with get_db_session() as db:
            q = db.query(Cuenta).filter(Cuenta.plataforma == "twitter")
            if solo_status:
                q = q.filter(Cuenta.status == solo_status)
            if seccion:
                if hasattr(Cuenta, "seccion"):
                    q = q.filter(Cuenta.seccion == seccion)
                else:
                    logger.warning(
                        "El modelo Cuenta aun no tiene 'seccion'; se ignora el filtro"
                    )
            cuentas = q.all()
    except Exception as e:
        logger.error(f"Error consultando cuentas twitter para sincronizar: {e}")
        return resumen

    total = len(cuentas)
    resumen["total"] = total

    for idx, cuenta in enumerate(cuentas, start=1):
        resultado = sincronizar_cuenta(cuenta)
        resumen["detalle"].append(resultado)

        error = resultado.get("error", "")
        if error == "":
            resumen["ok"] += 1
        elif error == "sin_datos":
            resumen["sin_datos"] += 1
        else:
            resumen["errores"] += 1

        if resultado.get("cambio_nombre"):
            resumen["cambios_nombre"] += 1
        if resultado.get("cambio_handle"):
            resumen["cambios_handle"] += 1

        if callback:
            try:
                callback(idx, total, resultado.get("usuario", ""), resultado)
            except Exception as e:
                logger.error(f"Error en callback de sincronizacion: {e}")

    return resumen

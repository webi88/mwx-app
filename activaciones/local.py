# -*- coding: utf-8 -*-
"""Campana LOCAL secuencial (Chrome visible en la computadora del dueño).

Objetivo: una campana en la computadora del dueño con las cuentas Tier 1 con
sesion (SIN tope de cuentas por defecto: ``LOCAL_MAX_CUENTAS=0``) que alterna
POSTS CON IMAGEN (rol ``hashtags``) y RETWEETS CON CITA (rol ``cita``) del
tweet ancla. Usa UN solo Chrome visible, cambia de cuenta en caliente con las
cookies guardadas y reparte las acciones dentro de una ventana (default 1
hora) con un tope diario de acciones exitosas por cuenta.

Proxy opcional (``usar_proxy=True``): pre-vuelo que asigna/persiste UN proxy
sticky VALIDO por cuenta (reutiliza el de la BD si sigue alcanzando x.com) y
abre Chrome con forwarder dinamico; sin ningun proxy valido la campana aborta
SIN abrir Chrome. Con ``esperar_captcha_seg>0`` un fallo anti-bot (Cloudflare)
espera a que el humano resuelva el reto en el Chrome visible y reintenta la
accion UNA vez. El default (``usar_proxy=False``, ``esperar_captcha_seg=0``)
es EXACTAMENTE el comportamiento historico (conexion directa).

Velocidad: en local NO se refresca la pagina (ni al entrar ni al cambiar de
cuenta); si una sesion esta invalida se omite la cuenta en vez de pagar un
refresh. ``_ENV_LOCAL`` fuerza ``REFRESH_AL_ENTRAR=0`` y
``REFRESH_ENTRADA_MAX=0`` como cinturon de seguridad.

API PUBLICA (firmas congeladas; kwargs nuevos siempre al final):

    ejecutar_campana_local(
        usuarios=None, urls=None, hashtags="", texto_base="", contexto="",
        narrativa="", menciones="", imagenes=None, probabilidad_imagen=100,
        acciones_por_cuenta=6, duracion_min=60, limite_dia=None,
        dar_like=False, permitir_pausadas=False, dejar_abierto=False,
        cancelar=None, callback=None, pausa_entre_acciones_seg=2,
        capturar_url=False, usar_proxy=False, proxies_pais="mexico",
        sin_imagenes=None, esperar_captcha_seg=0,
    ) -> dict

    cerrar_navegadores_locales() -> int

Reglas:
  * Solo cuentas ``plataforma == "twitter"``, activas, Tier 1 y con sesion
    (auth_token / cookies_json / cookies_path existente). Las pausadas se
    omiten salvo ``permitir_pausadas=True`` y las pendientes del cajon de
    verificacion humana SIEMPRE quedan fuera (``omitidas_verificacion``).
  * ``LOCAL_MAX_CUENTAS=0`` (default) NO recorta cuentas; un valor ``> 0``
    aplica el tope. El limite diario de 49 acciones por cuenta SE CONSERVA.
  * Roles permitidos: ``hashtags`` (post, con imagen opcional) y ``cita``
    (quote-RT del ancla). Nada de RT simple ni likes.
  * Todo texto final pasa por ``garantizar_texto_hashtag(..., limite=100)``.
  * Las acciones se intercalan ROUND-ROBIN entre cuentas y el orden de las
    cuentas se baraja en CADA ciclo/ronda (no un orden fijo por campana).
  * Nunca lanza: cualquier error se devuelve en ``{"ok": False, "error": ...}``.
"""
from __future__ import annotations

import contextlib
import inspect
import os
import random
import re
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from loguru import logger

try:
    from core.config import settings
except Exception:  # pragma: no cover - settings siempre existe en el repo
    settings = None

__all__ = ["ejecutar_campana_local", "cerrar_navegadores_locales"]

_ENV_LOCAL = {
    "TWITTER_SIN_PROXY": "1",
    "API_PRIMERO": "0",
    "RT_POR_API": "0",
    "CHROME_SIN_IMAGENES": "0",
    "MODO_PESTANA": "1",
    # La campana local NO refresca la pagina: maxima velocidad. Se fuerzan a
    # 0 como cinturon de seguridad (se restauran en el `finally`).
    "REFRESH_AL_ENTRAR": "0",
    "REFRESH_ENTRADA_MAX": "0",
}

_RE_URL_PUBLICACION = re.compile(
    r"^https?://(?:www\.)?(?:twitter\.com|x\.com)/"
    r"(?:[^/\s]+/status|i/web/status)/\d+",
    re.IGNORECASE,
)

_FALLBACK_POST = "Comparto esta reflexión con ustedes."
_FALLBACK_CITA = "Lo que está pasando merece atención."

# La campaña LOCAL puede usar TODO el limite de X (280). Las campanas masivas
# siguen con su limite de 100 en `ia/generador_contenido.py`.
_LIMITE_TEXTO_LOCAL = 280

# Topes de IA para no pagar una llamada gigante cuando el usuario elige el
# cupo diario (49/accion): los textos que falten se rellenan con variaciones
# locales unicas (mismo flujo que ya usa la campana). Con el default de 10
# (5 posts + 5 citas) estos topes no recortan nada.
_MAX_POSTS_IA_LOCAL = 6
_MAX_CITAS_IA_LOCAL = 25

# Hashtags escritos por el usuario dentro del contexto/texto base (fallback
# cuando deja vacio el campo de hashtags).
_RE_HASHTAG_TEXTO = re.compile(
    r"#[0-9A-Za-z_ÁÉÍÓÚÜÑáéíóúüñ]+", re.UNICODE
)

# Plantillas locales de ultimo recurso: SOLO se usan si la IA y el fallback
# de `generar_textos_hashtags_por_cuenta` fallan por completo. Se combinan
# para que dos cuentas no publiquen el mismo texto.
_PLANTILLAS_FALLBACK_POST = (
    "Hoy vale la pena hablar de esto.",
    "Comparto esta reflexión con ustedes.",
    "Es un tema que merece atención.",
    "Me parece importante decirlo.",
    "Hay cosas que conviene comentar.",
    "Esto es algo de lo que todos deberíamos hablar.",
    "Una idea que vale la pena compartir.",
    "Así lo veo hoy.",
    "Vale la pena detenerse un momento en esto.",
    "Quiero dejar mi punto de vista.",
    "Este tema nos toca a todos.",
    "Conviene seguir hablando de esto.",
)

_NAVEGADORES_ABIERTOS: list = []
_LOCK_NAVEGADORES = threading.Lock()

TwitterBot = None

# Hooks opcionales para tests/monkeypatch (None = usar el modulo real).
registrar_accion = None
contar_acciones_dia_por_usuario = None
tipo_registro_rol = None
generar_textos_hashtags_por_cuenta = None
generar_pool_variaciones_openai = None
garantizar_texto_hashtag = None


def _acepta_kwarg(func, nombre: str) -> bool:
    """True si `func` acepta el kwarg `nombre` (o **kwargs). Nunca lanza."""
    try:
        firma = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for parametro in firma.parameters.values():
        if parametro.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return nombre in firma.parameters


def _kwargs_perfil_rapido(metodo, capturar_url: bool) -> dict:
    """Kwargs extra para NO visitar el perfil cuando se captura el link.

    Solo con `capturar_url=True` y si el metodo del bot acepta
    `buscar_url_perfil` (kwarg nuevo): la captura se queda en la ruta rapida
    (toast/timeline) y nunca visita el perfil (~5-10s por accion). Con
    `capturar_url=False` o un bot/fake viejo devuelve `{}`: el comportamiento
    es exactamente el de antes. Nunca lanza.
    """
    if not capturar_url:
        return {}
    try:
        if _acepta_kwarg(metodo, "buscar_url_perfil"):
            return {"buscar_url_perfil": False}
    except Exception:
        return {}
    return {}


def _env_activo(nombre: str, default: bool = False) -> bool:
    try:
        valor = os.environ.get(nombre)
    except Exception:
        return bool(default)
    if valor is None:
        return bool(default)
    return str(valor).strip().lower() not in ("", "0", "false", "no", "off")


def _a_int(valor, default: int) -> int:
    try:
        return int(float(str(valor).strip()))
    except (TypeError, ValueError):
        return int(default)


def _a_float(valor, default: float) -> float:
    try:
        numero = float(str(valor).strip())
    except (TypeError, ValueError):
        return float(default)
    if numero != numero:
        return float(default)
    return numero


def _clamp(valor: int, minimo: int, maximo: int) -> int:
    return max(int(minimo), min(int(maximo), int(valor)))


def _limpiar_texto(valor) -> str:
    try:
        return " ".join(str(valor or "").split())
    except Exception:
        return ""


def _normalizar_usuarios(usuarios) -> list:
    """Lista de usuarios sin '@', deduplicada case-insensitive, en su orden."""
    if usuarios is None:
        return []
    if isinstance(usuarios, str):
        crudos = re.split(r"[\s,;|]+", usuarios)
    else:
        try:
            crudos = list(usuarios)
        except TypeError:
            crudos = [usuarios]
    vistos, salida = set(), []
    for crudo in crudos:
        usuario = str(crudo or "").strip().lstrip("@").strip()
        if not usuario:
            continue
        clave = usuario.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(usuario)
    return salida


def _normalizar_urls(urls) -> list:
    """Lista de URLs http(s) deduplicadas (lista o texto). Nunca lanza."""
    if urls is None:
        return []
    if isinstance(urls, str):
        crudos = re.split(r"[\r\n;|]+", urls)
    else:
        try:
            crudos = list(urls)
        except TypeError:
            crudos = [urls]
    vistos, salida = set(), []
    for crudo in crudos:
        url = str(crudo or "").strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            continue
        if url in vistos:
            continue
        vistos.add(url)
        salida.append(url)
        if len(salida) >= 50:
            break
    return salida


def _normalizar_imagenes(imagenes) -> list:
    """Rutas de imagen que EXISTEN en disco, deduplicadas. Nunca lanza."""
    if imagenes is None:
        return []
    if isinstance(imagenes, str):
        crudos = re.split(r"[\r\n;|]+", imagenes)
    else:
        try:
            crudos = list(imagenes)
        except TypeError:
            crudos = [imagenes]
    vistos, salida = set(), []
    for crudo in crudos:
        ruta = str(crudo or "").strip()
        if not ruta or ruta in vistos:
            continue
        vistos.add(ruta)
        try:
            if os.path.isfile(ruta):
                salida.append(ruta)
        except Exception:
            continue
        if len(salida) >= 50:
            break
    return salida


def _normalizar_hashtags_texto(hashtags) -> str:
    if hashtags is None:
        return ""
    if isinstance(hashtags, str):
        return hashtags.strip()
    try:
        return " ".join(str(tag or "").strip() for tag in hashtags if str(tag or "").strip())
    except TypeError:
        return str(hashtags).strip()


def _lista_hashtags(hashtags) -> list:
    """Hashtags normalizados ("#Tag") sin duplicados. Nunca lanza."""
    texto = _normalizar_hashtags_texto(hashtags)
    if not texto:
        return []
    crudos = [token for token in re.split(r"[\s,]+", texto) if token]
    vistos, salida = set(), []
    for token in crudos:
        tag = token.strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = "#" + tag.lstrip("@")
        clave = tag.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(tag)
    return salida


def _normalizar_tier(valor) -> str:
    try:
        from core.tiers import normalizar_tier

        return normalizar_tier(valor)
    except Exception:
        return ""


def _normalizar_registro(valor) -> str:
    try:
        from core.registros import normalizar_tipo_cuenta

        return normalizar_tipo_cuenta(valor)
    except Exception:
        return ""


def _normalizar_perfil(valor) -> str:
    try:
        from core.perfiles import normalizar_perfil

        return normalizar_perfil(valor)
    except Exception:
        return ""


def _cookies_json_con_contenido(cookies_json) -> bool:
    try:
        if cookies_json is None:
            return False
        if isinstance(cookies_json, str):
            crudo = cookies_json.strip()
            if not crudo or crudo.lower() in ("[]", "{}", "null", "none"):
                return False
            return True
        if isinstance(cookies_json, (list, tuple, set, dict)):
            return bool(cookies_json)
        return bool(cookies_json)
    except Exception:
        return False


def _tiene_sesion(cuenta: dict) -> bool:
    """Mismo criterio que `scheduler/calentamiento.py::_tiene_sesion`."""
    try:
        auth_token = str(cuenta.get("auth_token") or "").strip()
        if auth_token:
            return True
        if _cookies_json_con_contenido(cuenta.get("cookies_json")):
            return True
        cookies_path = str(cuenta.get("cookies_path") or "").strip()
        if not cookies_path:
            return False
        from core.config import resolver_ruta

        return os.path.isfile(resolver_ruta(cookies_path))
    except Exception:
        return False


def _esta_pausada(cuenta: dict) -> bool:
    try:
        from core.pausas import esta_pausada

        if isinstance(cuenta, dict):
            return bool(esta_pausada(types.SimpleNamespace(**cuenta)))
        return bool(esta_pausada(cuenta))
    except Exception:
        return bool(cuenta.get("pausada_activacion")) if isinstance(cuenta, dict) else False


def _esta_pendiente_verificacion(cuenta) -> bool:
    """True si la cuenta esta en el cajon de verificacion humana.

    Import PEREZOSO de `core.verificacion` (tolerante: si el modulo no existe
    o falla, la cuenta NO se considera pendiente para no frenar la campana).
    Nunca lanza."""
    try:
        from core.verificacion import esta_pendiente

        return bool(esta_pendiente(cuenta))
    except Exception:
        try:
            return bool(cuenta.get("verificacion_pendiente"))
        except Exception:
            return False


def _cuenta_a_dict(cuenta) -> dict:
    return {
        "usuario": str(getattr(cuenta, "usuario", "") or ""),
        "tier_calidad": getattr(cuenta, "tier_calidad", "") or "",
        "tipo_cuenta": getattr(cuenta, "tipo_cuenta", "") or "",
        "perfil_personalidad": getattr(cuenta, "perfil_personalidad", "") or "",
        "personalidad": getattr(cuenta, "personalidad", "") or "",
        "seccion": getattr(cuenta, "seccion", "") or "",
        "nombre_mostrado": getattr(cuenta, "nombre_mostrado", "") or "",
        "handle_actual": getattr(cuenta, "handle_actual", "") or "",
        "pausada_activacion": bool(getattr(cuenta, "pausada_activacion", False)),
        "verificacion_pendiente": bool(
            getattr(cuenta, "verificacion_pendiente", False)
        ),
        "verificacion_motivo": str(
            getattr(cuenta, "verificacion_motivo", "") or ""
        ),
        "auth_token": getattr(cuenta, "auth_token", "") or "",
        "cookies_json": getattr(cuenta, "cookies_json", None),
        "cookies_path": getattr(cuenta, "cookies_path", "") or "",
    }


def _leer_cuentas(usuarios=None) -> tuple:
    """Lee cuentas twitter activas y devuelve (dicts, no_encontradas). Nunca lanza."""
    try:
        from core.database import get_db_session
        from core.models import Cuenta
    except Exception as e:
        return [], [], f"no se pudo importar la BD: {e}"

    solicitados = _normalizar_usuarios(usuarios)
    try:
        with get_db_session() as db:
            filas = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter", Cuenta.activa == True)  # noqa: E712
                .all()
            )
            cuentas = [_cuenta_a_dict(fila) for fila in filas]
    except Exception as e:
        return [], [], f"error leyendo cuentas: {e}"

    if usuarios is None:
        return cuentas, [], ""
    if not solicitados:
        return [], [], ""

    indice = {cuenta["usuario"].lower(): cuenta for cuenta in cuentas}
    orden, no_encontradas = [], []
    for usuario in solicitados:
        cuenta = indice.get(usuario.lower())
        if cuenta is None:
            no_encontradas.append(usuario)
        else:
            orden.append(cuenta)
    return orden, no_encontradas, ""


def _seleccionar_cuentas(cuentas, usuarios, permitir_pausadas, max_cuentas) -> dict:
    """Filtra Tier 1 + sesion + pausa, respeta `usuarios` y aplica el tope.

    Contrato del tope: ``max_cuentas <= 0`` = SIN TOPE (no recorta y
    ``recortadas`` queda 0); solo un valor ``> 0`` recorta la seleccion.
    """
    solicitados = _normalizar_usuarios(usuarios)
    orden = list(cuentas or [])
    if usuarios is not None:
        indice = {cuenta.get("usuario", "").lower(): cuenta for cuenta in orden}
        orden = [
            indice[usuario.lower()]
            for usuario in solicitados
            if usuario.lower() in indice
        ]

    seleccion, omitidas_tier, omitidas_sin_sesion, omitidas_pausadas = [], [], [], []
    omitidas_verificacion = []
    for cuenta in orden:
        usuario = str(cuenta.get("usuario") or "").strip()
        if not usuario:
            continue
        if _normalizar_tier(cuenta.get("tier_calidad")) != "tier1":
            omitidas_tier.append(usuario)
            continue
        if not _tiene_sesion(cuenta):
            omitidas_sin_sesion.append(usuario)
            continue
        if _esta_pendiente_verificacion(cuenta):
            # Cajon de verificacion humana: fuera de TODAS las campanas de
            # activacion (se reporta aparte, nunca se mezcla con pausadas).
            omitidas_verificacion.append(usuario)
            continue
        if not permitir_pausadas and _esta_pausada(cuenta):
            omitidas_pausadas.append(usuario)
            continue
        seleccion.append(cuenta)

    recortadas_usuarios = []
    try:
        tope = int(max_cuentas)
    except (TypeError, ValueError):
        tope = 0
    if tope > 0 and len(seleccion) > tope:
        recortadas_usuarios = [
            str(cuenta.get("usuario") or "") for cuenta in seleccion[tope:]
        ]
        seleccion = seleccion[:tope]

    return {
        "selected": seleccion,
        "omitidas_tier": omitidas_tier,
        "omitidas_sin_sesion": omitidas_sin_sesion,
        "omitidas_pausadas": omitidas_pausadas,
        "omitidas_verificacion": omitidas_verificacion,
        "recortadas": len(recortadas_usuarios),
        "recortadas_usuarios": recortadas_usuarios,
    }


def _contar_usados(usuarios) -> dict:
    try:
        funcion = globals().get("contar_acciones_dia_por_usuario")
        if funcion is None:
            from core import registro

            funcion = registro.contar_acciones_dia_por_usuario
        resultado = funcion(list(usuarios or []))
        if isinstance(resultado, dict):
            return {str(clave): int(valor or 0) for clave, valor in resultado.items()}
        return {}
    except Exception:
        return {}


def _construir_plan(selected, roles, acciones_por_cuenta, usados, limite, urls) -> dict:
    """Construye el plan round-robin y aplica la cuota diaria por cuenta.

    En CADA ronda se baraja el orden de las cuentas que aun tienen accion en
    esa ronda (no se fija un orden unico para toda la campana): el conjunto de
    cuentas por ronda es el mismo, pero el orden varia. La alternancia de roles
    de cada cuenta (hashtags/cita) NO cambia.
    """
    plan_por_cuenta, cupos, omitidas_cuota = {}, {}, []
    for cuenta in selected:
        usuario = str(cuenta.get("usuario") or "")
        usado = int((usados or {}).get(usuario, 0) or 0)
        restante = None
        if int(limite) > 0:
            restante = max(0, int(limite) - usado)
        cupos[usuario] = restante
        if restante is not None and restante <= 0:
            omitidas_cuota.append(usuario)
            plan_por_cuenta[usuario] = []
            continue
        cantidad = int(acciones_por_cuenta)
        if restante is not None:
            cantidad = min(cantidad, restante)
        acciones = []
        for indice in range(max(0, cantidad)):
            rol = roles[indice % len(roles)]
            accion = {
                "usuario": usuario,
                "cuenta": cuenta,
                "rol": rol,
                "url": random.choice(urls) if rol == "cita" and urls else "",
                "texto": "",
                "imagen": None,
            }
            acciones.append(accion)
        plan_por_cuenta[usuario] = acciones

    elegibles = [
        str(cuenta.get("usuario") or "")
        for cuenta in selected
        if plan_por_cuenta.get(str(cuenta.get("usuario") or ""))
    ]
    plan = []
    maximo = max((len(plan_por_cuenta[usuario]) for usuario in elegibles), default=0)
    for ronda in range(maximo):
        # Solo las cuentas que aun tienen accion en esta ronda, barajadas en
        # CADA ciclo (el orden no se repite de una ronda a otra).
        orden_ronda = [
            usuario for usuario in elegibles
            if ronda < len(plan_por_cuenta[usuario])
        ]
        random.shuffle(orden_ronda)
        for usuario in orden_ronda:
            plan.append(plan_por_cuenta[usuario][ronda])
    return {
        "plan": plan,
        "por_cuenta": plan_por_cuenta,
        "cupos": cupos,
        "omitidas_cuota": omitidas_cuota,
    }


def _partir_plan_por_navegador(plan, navegadores) -> list:
    """Reparte el plan en N cubetas SIN partir cuentas entre navegadores.

    - Cada `usuario` queda ENTERO en una sola cubeta (se agrupan sus acciones
      conservando el orden interno del plan).
    - Los grupos se barajan y se reparten con greedy al navegador con menos
      acciones para equilibrar la carga.
    - Devuelve SIEMPRE N listas; la union es exactamente `plan` (sin perder ni
      duplicar acciones). `navegadores <= 1` devuelve `[list(plan)]`.
    Nunca lanza.
    """
    try:
        acciones_plan = list(plan or [])
    except Exception:
        acciones_plan = []
    try:
        n = _clamp(_a_int(navegadores, 1), 1, 3)
    except Exception:
        n = 1
    if n <= 1:
        return [list(acciones_plan)]
    if not acciones_plan:
        return [[] for _ in range(n)]

    # 1) Agrupar por cuenta conservando el orden interno de cada una.
    grupos: list = []
    indice: dict = {}
    try:
        for accion in acciones_plan:
            usuario = str(accion.get("usuario") or "")
            posicion = indice.get(usuario)
            if posicion is None:
                indice[usuario] = len(grupos)
                grupos.append([accion])
            else:
                grupos[posicion].append(accion)
    except Exception:
        return [list(acciones_plan)] + [[] for _ in range(n - 1)]

    # 2) Barajar los grupos (no ser determinista) y repartir greedy.
    try:
        random.shuffle(grupos)
    except Exception:
        pass
    cubetas: list = [[] for _ in range(n)]
    cargas = [0] * n
    for grupo in grupos:
        minimo = min(cargas)
        candidatos = [i for i, carga in enumerate(cargas) if carga == minimo]
        destino = random.choice(candidatos) if len(candidatos) > 1 else candidatos[0]
        cubetas[destino].extend(grupo)
        cargas[destino] += len(grupo)
    return cubetas


def _hashtags_desde_texto(*textos) -> list:
    """Hashtags escritos dentro de textos del usuario (contexto/texto base).

    Se usa SOLO cuando el campo de hashtags quedo vacio: el dueño suele
    escribir "#Muuki2026" dentro del contexto. Devuelve una lista normalizada
    (con '#' y sin duplicados) en el orden en que aparecen. Nunca lanza.
    """
    encontrados = []
    for texto in textos:
        try:
            encontrados.extend(_RE_HASHTAG_TEXTO.findall(str(texto or "")))
        except Exception:
            continue
    return _lista_hashtags(" ".join(encontrados))


def _garantizar_texto(texto, hashtags, limite: int = _LIMITE_TEXTO_LOCAL) -> str:
    """Pasa el texto por `garantizar_texto_hashtag`; fallback local si falla.

    La campaña LOCAL usa 280 (límite de X) para que el texto salga completo.
    """
    try:
        funcion = globals().get("garantizar_texto_hashtag")
        if funcion is None:
            from ia import generador_contenido

            funcion = generador_contenido.garantizar_texto_hashtag
        resultado = funcion(texto, hashtags, limite=limite)
        if str(resultado or "").strip():
            return str(resultado).strip()
    except Exception as e:
        logger.debug(f"Campana local: garantizar_texto_hashtag fallo: {e}")

    base = _limpiar_texto(texto) or _FALLBACK_POST
    tags = _lista_hashtags(hashtags)
    if tags:
        grupo = " ".join(tags)
        mitad = max(1, len(base) // 2)
        texto_final = f"{base[:mitad]} {grupo} {base[mitad:]}".strip()
    else:
        texto_final = base
    texto_final = " ".join(texto_final.split())
    if len(texto_final) > limite:
        corte = texto_final[:limite]
        espacio = corte.rfind(" ")
        if espacio >= int(limite * 0.55):
            corte = corte[:espacio]
        texto_final = corte.rstrip(" ,;:")
    return texto_final


def _variar_post_local(base: str, hashtags, evitar: set) -> str:
    """Ultimo recurso de post unico: variacion local + hashtags garantizados.

    Usa `activaciones.variaciones.variar_texto` (sinonimos/aperturas/cierres)
    y evita repetir un texto ya usado en la campana. Nunca lanza."""
    tags = _lista_hashtags(hashtags)
    candidatos = []
    try:
        from activaciones import variaciones

        candidatos.append(variaciones.variar_texto(base, hashtags=tags))
    except Exception:
        pass
    candidatos.extend(
        f"{plantilla} {base}".strip()
        for plantilla in random.sample(
            _PLANTILLAS_FALLBACK_POST,
            k=min(4, len(_PLANTILLAS_FALLBACK_POST)),
        )
    )
    candidatos.append(base)
    for candidato in candidatos:
        final = _garantizar_texto(candidato, hashtags)
        if final and final not in evitar:
            return final
    return _garantizar_texto(
        f"{random.choice(_PLANTILLAS_FALLBACK_POST)} {_FALLBACK_POST}",
        hashtags,
    )


def _generar_posts(selected, hashtags, contexto, narrativa, max_posts) -> dict:
    """Genera `max_posts` textos por cuenta (IA + fallback local garantizado).

    Los textos son UNICOS dentro de la campana: si la IA devuelve repetidos o
    el fallback coincide, se varian localmente. Nunca lanza.
    """
    resultado = {str(cuenta.get("usuario") or ""): [] for cuenta in selected}
    if max_posts <= 0 or not selected:
        return resultado

    info = []
    for cuenta in selected:
        info.append(
            {
                "usuario": str(cuenta.get("usuario") or ""),
                "registro": _normalizar_registro(cuenta.get("tipo_cuenta")),
                "personalidad": cuenta.get("personalidad") or "",
                "seccion": cuenta.get("seccion") or "",
                "nombre": (
                    cuenta.get("nombre_mostrado")
                    or cuenta.get("usuario")
                    or ""
                ),
                "perfil": _normalizar_perfil(cuenta.get("perfil_personalidad")),
            }
        )

    crudos = {}
    try:
        funcion = globals().get("generar_textos_hashtags_por_cuenta")
        if funcion is None:
            from ia import generador_contenido
            funcion = generador_contenido.generar_textos_hashtags_por_cuenta

        kwargs_ia = {
            "hashtags": hashtags,
            "contexto": contexto,
            "n_por_cuenta": min(int(max_posts), _MAX_POSTS_IA_LOCAL),
            "narrativa": narrativa,
            "entrenamiento": "",
        }
        try:
            if _acepta_kwarg(funcion, "limite"):
                kwargs_ia["limite"] = _LIMITE_TEXTO_LOCAL
        except Exception:
            pass
        crudos = funcion(info, **kwargs_ia)
        if not isinstance(crudos, dict):
            crudos = {}
    except Exception as e:
        logger.warning(f"Campana local: IA de posts fallo, uso fallback: {e}")
        crudos = {}

    vistos: set = set()
    vistos_lista: list = []
    for cuenta in selected:
        usuario = str(cuenta.get("usuario") or "")
        textos_crudos = crudos.get(usuario) or []
        if isinstance(textos_crudos, str):
            textos_crudos = [textos_crudos]
        textos = []
        try:
            for texto in list(textos_crudos)[:max_posts]:
                final = _garantizar_texto(texto, hashtags)
                if (
                    final
                    and final not in vistos
                    and _es_texto_diverso_local(
                        final, vistos_lista,
                        _umbral_post_local(),
                    )
                ):
                    vistos.add(final)
                    vistos_lista.append(final)
                    textos.append(final)
        except Exception:
            textos = []
        while len(textos) < max_posts:
            base = contexto or _FALLBACK_POST
            final = _variar_post_local(base, hashtags, vistos)
            if (
                not final
                or not _es_texto_diverso_local(
                    final, vistos_lista,
                    _umbral_post_local(),
                )
            ):
                # Ultimo recurso: generador local diverso (cambia angulo,
                # apertura, estructura y cierre) con el registro de la cuenta.
                try:
                    from activaciones.variaciones import (
                        generar_pool_variaciones_diversas,
                    )

                    alternos = generar_pool_variaciones_diversas(
                        base,
                        1,
                        registro=_normalizar_registro(
                            cuenta.get("tipo_cuenta")
                        ),
                        perfil=_normalizar_perfil(
                            cuenta.get("perfil_personalidad")
                        ),
                        hashtags=_lista_hashtags(hashtags),
                        evitar=list(vistos_lista),
                        limite=_LIMITE_TEXTO_LOCAL,
                        semilla=len(vistos_lista) + 1,
                    )
                    if alternos:
                        final = _garantizar_texto(alternos[0], hashtags)
                except Exception as e:
                    logger.debug(
                        f"Campana local: post diverso fallo: {e}"
                    )
            if not final:
                final = _garantizar_texto(_FALLBACK_POST, hashtags)
            vistos.add(final)
            vistos_lista.append(final)
            textos.append(final)
        resultado[usuario] = textos[:max_posts]
    return resultado



def _generar_citas(plan, texto_base, narrativa, hashtags) -> None:
    """Asigna texto de cita por cuenta (agrupado por registro/perfil).

    Garantiza que ninguna cita quede vacia ni repita el mismo texto: si el
    pool de IA/fallback viene corto, se generan variaciones locales unicas.
    Nunca lanza.
    """
    grupos = {}
    for accion in plan:
        if accion.get("rol") != "cita":
            continue
        cuenta = accion.get("cuenta") or {}
        clave = (
            _normalizar_registro(cuenta.get("tipo_cuenta")),
            _normalizar_perfil(cuenta.get("perfil_personalidad")),
        )
        grupos.setdefault(clave, []).append(accion)

    tags = _lista_hashtags(hashtags)
    vistos: set = set()
    vistos_lista: list = []
    for (registro, perfil), acciones in grupos.items():
        pool = []
        try:
            funcion = globals().get("generar_pool_variaciones_openai")
            if funcion is None:
                from activaciones import variaciones
                funcion = variaciones.generar_pool_variaciones_openai

            kwargs_pool = {
                "narrativa": narrativa,
                "entrenamiento": "",
                "registro": registro,
                "perfil": perfil,
                "hashtags": tags,
            }
            try:
                # Memoria anti-repeticion de TODA la campana: la IA recibe los
                # textos ya usados y no debe reformularlos.
                if _acepta_kwarg(funcion, "evitar"):
                    kwargs_pool["evitar"] = list(vistos_lista)
                if _acepta_kwarg(funcion, "limite"):
                    kwargs_pool["limite"] = _LIMITE_TEXTO_LOCAL
            except Exception:
                pass
            pool = funcion(
                texto_base,
                min(len(acciones), _MAX_CITAS_IA_LOCAL),
                **kwargs_pool,
            )
        except Exception as e:
            logger.warning(f"Campana local: variaciones de cita fallaron: {e}")
            pool = []
        try:
            pool = [str(texto or "") for texto in list(pool or [])]
        except Exception:
            pool = []
        random.shuffle(pool)
        indice_pool = 0
        for accion in acciones:
            base = ""
            while indice_pool < len(pool) and not base:
                candidato = pool[indice_pool]
                indice_pool += 1
                final = _garantizar_texto(candidato, hashtags)
                if (
                    final
                    and final not in vistos
                    and _es_texto_diverso_local(final, vistos_lista)
                ):
                    base = final
            if not base:
                cuenta = accion.get("cuenta") or {}
                base = _cita_local_diversa_local(
                    cuenta,
                    texto_base or _FALLBACK_CITA,
                    hashtags,
                    vistos_lista,
                    semilla=len(vistos_lista) + 1,
                )
            if not base:
                base = _variar_post_local(
                    texto_base or _FALLBACK_CITA, hashtags, vistos
                )
            if not base:
                base = _garantizar_texto(_FALLBACK_CITA, hashtags)
            vistos.add(base)
            vistos_lista.append(base)
            accion["texto"] = base



def _resumen_inicial(limite_dia, acciones_por_cuenta, duracion_min,
                     imagenes_disponibles, probabilidad_imagen) -> dict:
    ahora = datetime.now()
    return {
        "ok": True,
        "error": "",
        "motivo": "",
        "cancelada": False,
        "inicio": ahora.isoformat(timespec="seconds"),
        "fin": "",
        "segundos": 0.0,
        "modo_local": True,
        "navegador_visible": True,
        "sin_proxy": True,
        "usar_proxy": False,
        "proxies_asignados": 0,
        "proxies_invalidos": 0,
        "captchas_resueltos": 0,
        "captchas_agotados": 0,
        "datos_proxy_subida": 0,
        "datos_proxy_bajada": 0,
        "datos_proxy_total": 0,
        "cuentas": [],
        "cuentas_total": 0,
        "cuentas_seleccionadas": 0,
        "cuentas_usadas": 0,
        "limite_dia": int(limite_dia),
        "acciones_por_cuenta": int(acciones_por_cuenta),
        "duracion_min": int(duracion_min),
        "planificadas": 0,
        "hechas": 0,
        "exitosas": 0,
        "fallidas": 0,
        "omitidas": 0,
        "omitidas_tier": 0,
        "omitidas_sin_sesion": 0,
        "omitidas_pausadas": 0,
        "omitidas_verificacion": 0,
        "verificaciones_marcadas": 0,
        "verificaciones_usuarios": [],
        "omitidas_por_cuota": 0,
        "omitidas_por_tiempo": 0,
        "omitidas_usuarios": {
            "tier": [],
            "sin_sesion": [],
            "pausadas": [],
            "verificacion": [],
            "cuota": [],
            "tiempo": [],
            "no_encontradas": [],
            "no_elegibles": [],
        },
        "recortadas": 0,
        "por_cuenta": {},
        "eventos": [],
        "imagenes_disponibles": int(imagenes_disponibles),
        "probabilidad_imagen": probabilidad_imagen,
        "sin_imagenes": int(imagenes_disponibles) <= 0,
        "navegadores_creados": 0,
        "navegador_abierto": False,
    }


@contextlib.contextmanager
def _entorno_local(env=None):
    """Aplica `env` (default `_ENV_LOCAL`) y lo restaura SIEMPRE al salir.

    `ejecutar_campana_local` pasa una COPIA runtime de `_ENV_LOCAL` con los
    valores de proxy/imagenes de la campaña; `_ENV_LOCAL` queda intacto (los
    tests lo leen).
    """
    valores = dict(_ENV_LOCAL if env is None else env)
    previos = {clave: os.environ.get(clave) for clave in valores}
    try:
        for clave, valor in valores.items():
            os.environ[clave] = valor
        yield
    finally:
        for clave, previo in previos.items():
            try:
                if previo is None:
                    os.environ.pop(clave, None)
                else:
                    os.environ[clave] = previo
            except Exception:
                continue


def _url_publicacion(url, ancla: str = "") -> str:
    """URL de post valida (`/status/`), limpia y distinta del ancla."""
    try:
        texto = str(url or "").strip()
        if not texto or not _RE_URL_PUBLICACION.match(texto):
            return ""
        limpia = texto.split("?")[0].split("#")[0].rstrip("/")
        if ancla:
            ancla_limpia = str(ancla).strip().split("?")[0].split("#")[0].rstrip("/")
            if limpia == ancla_limpia:
                return ""
        return limpia
    except Exception:
        return ""


def _tipo_registro(rol: str) -> str:
    try:
        funcion = globals().get("tipo_registro_rol")
        if funcion is None:
            from core import registro

            funcion = registro.tipo_registro_rol
        return funcion(rol)
    except Exception:
        return "post" if rol == "hashtags" else "cita"


def _registrar_accion(usuario, tipo, estado, url, detalle) -> None:
    try:
        funcion = globals().get("registrar_accion")
        if funcion is None:
            from core import registro

            funcion = registro.registrar_accion
        funcion(usuario, tipo, estado, url or "", (detalle or "")[:200])
    except Exception as e:
        logger.debug(f"Campana local: no se pudo registrar la accion: {e}")


def _crear_bot(usuario: str):
    """Crea el TwitterBot local (import perezoso; monkeypatcheable)."""
    clase = globals().get("TwitterBot")
    if clase is None:
        from plataformas.twitter.selenium_bot import TwitterBot as clase
    try:
        if _acepta_kwarg(clase.__init__, "modo_local"):
            return clase(usuario, modo_local=True)
    except TypeError:
        pass
    return clase(usuario)


def _asignar_proxies_cuentas(usuarios, pais: str = "mexico"):
    """Asigna (y persiste) UN proxy sticky valido por cuenta. Nunca lanza.

    Pre-vuelo de la campana con proxy, ANTES de abrir Chrome:

    * Candidatos: ``ProxyManager().cargar_por_pais(pais)`` y, si viene vacio,
      ``cargar_proxies()``.
    * Reutiliza ``Cuenta.proxy`` si sigue alcanzando x.com (stickiness entre
      corridas); si no (credenciales vencidas => 407) toma el siguiente
      candidato NO usado y lo valida con ``x_accesible`` (12s). El valido se
      persiste en ``Cuenta.proxy`` (commit por cuenta).
    * Si un candidato falla se prueba el siguiente (50 por pais).

    Devuelve ``(proxy_por_usuario, invalidos, error)``. ``error`` solo viene
    con texto si NO se pudo asignar NINGUN proxy (la campaña debe abortar sin
    abrir Chrome: el objetivo es evitar el reto de Cloudflare de la IP
    directa). No imprime credenciales: los logs solo llevan conteos.
    """
    proxy_por_usuario: dict = {}
    invalidos = 0
    lista = _normalizar_usuarios(usuarios)
    if not lista:
        return {}, 0, ""
    try:
        from core.database import get_db_session
        from core.models import Cuenta
        from utils.proxies import ProxyManager
    except Exception as e:
        return {}, 0, f"no se pudo importar la BD/proxies: {e}"
    try:
        pm = ProxyManager()
        candidatos = list(pm.cargar_por_pais(pais) or [])
        if not candidatos:
            candidatos = list(pm.cargar_proxies() or [])
    except Exception as e:
        return {}, 0, f"no se pudieron cargar los proxies: {e}"
    if not candidatos:
        return {}, 0, "No hay proxies válidos: la lista de proxies esta vacia"

    def _clave(proxy) -> str:
        return str(proxy or "").strip().lower()

    usados: set = set()
    indice_candidato = 0
    try:
        with get_db_session() as db:
            for usuario in lista:
                cuenta = (
                    db.query(Cuenta)
                    .filter(
                        Cuenta.usuario == usuario,
                        Cuenta.plataforma == "twitter",
                    )
                    .first()
                )
                actual_bruto = (
                    (getattr(cuenta, "proxy", "") or "").strip()
                    if cuenta is not None else ""
                )
                # Limpia credenciales con segmentos repetidos (`_area-MX_life-15`
                # duplicado) ANTES de validar: el proxy funciona igual, pero el
                # log lo sanea y la BD queda normalizada al persistirlo.
                actual = actual_bruto
                try:
                    normalizar_guardado = getattr(pm, "normalizar_guardado", None)
                    if callable(normalizar_guardado):
                        actual = normalizar_guardado(actual_bruto) or actual_bruto
                except Exception:
                    actual = actual_bruto
                if actual and pm.x_accesible(actual, timeout=12):
                    proxy_por_usuario[usuario] = actual
                    usados.add(_clave(actual))
                    if cuenta is not None and (cuenta.proxy or "") != actual:
                        cuenta.proxy = actual
                        try:
                            db.commit()
                        except Exception as e:
                            logger.warning(
                                f"Campana local: no se pudo normalizar el proxy "
                                f"de {usuario}: {e}"
                            )
                    continue
                asignado = ""
                while indice_candidato < len(candidatos):
                    candidato = candidatos[indice_candidato]
                    indice_candidato += 1
                    clave = _clave(candidato)
                    if not clave or clave in usados:
                        continue
                    if pm.x_accesible(candidato, timeout=12):
                        asignado = candidato
                        break
                    invalidos += 1
                if not asignado:
                    invalidos += 1
                    continue
                usados.add(_clave(asignado))
                proxy_por_usuario[usuario] = asignado
                if cuenta is not None and (cuenta.proxy or "") != asignado:
                    cuenta.proxy = asignado
                    try:
                        # Commit por cuenta: si una asignacion posterior falla,
                        # las anteriores ya quedaron persistidas.
                        db.commit()
                    except Exception as e:
                        logger.warning(
                            f"Campana local: no se pudo persistir el proxy de "
                            f"{usuario}: {e}"
                        )
    except Exception as e:
        logger.warning(f"Campana local: error asignando proxies: {e}")
        error = (
            f"error asignando proxies: {type(e).__name__}: {e}"
            if not proxy_por_usuario else ""
        )
        return proxy_por_usuario, invalidos, error

    logger.info(
        f"Campana local: proxies validos {len(proxy_por_usuario)}/"
        f"{len(lista)} (pais={pais}, invalidos={invalidos})"
    )
    error = ""
    if not proxy_por_usuario:
        error = (
            "No hay proxies válidos: x.com no respondio por ningun candidato "
            f"(pais={pais}); no se abre Chrome sin proxy"
        )
    return proxy_por_usuario, invalidos, error


def _iniciar_driver(bot, usar_proxy: bool = False) -> bool:
    iniciar = getattr(bot, "iniciar_driver", None)
    if not callable(iniciar):
        return True
    dinamico = bool(usar_proxy)
    try:
        if _acepta_kwarg(iniciar, "modo_local"):
            try:
                resultado = iniciar(proxy_dinamico=dinamico, modo_local=True)
            except TypeError:
                try:
                    resultado = iniciar(proxy_dinamico=dinamico)
                except TypeError:
                    resultado = iniciar()
        elif _acepta_kwarg(iniciar, "proxy_dinamico"):
            resultado = iniciar(proxy_dinamico=dinamico)
        else:
            resultado = iniciar()
        return resultado is not False
    except Exception as e:
        try:
            bot.ultimo_error = f"{type(e).__name__}: {e}"
        except Exception:
            pass
        return False


def _preparar_sesion(bot) -> bool:
    """Deja la sesion lista para el PRIMER uso del navegador.

    Antes de inyectar cookies se limpian las del perfil de Chrome: la campaña
    reutiliza `data/perfiles_chrome/<cuenta>` entre ejecuciones y, si la
    corrida anterior dejo abierto el navegador con OTRA cuenta, quedarian
    cookies mezcladas (auth_token de una, ct0/twid de otra). `cambiar_cuenta`
    ya limpia en cada cambio; esto cubre el arranque. Nunca lanza.
    """
    driver = getattr(bot, "driver", None)
    if driver is not None:
        try:
            driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        except Exception:
            try:
                driver.delete_all_cookies()
            except Exception:
                pass
    preparar = getattr(bot, "preparar_sesion_cdp", None)
    login = getattr(bot, "login_con_cookies", None)
    if not callable(preparar) and not callable(login):
        return True
    if callable(preparar):
        try:
            if preparar():
                return True
        except Exception:
            pass
    if callable(login):
        try:
            return bool(login())
        except Exception:
            pass
    return False


def _refrescar_entrada(bot) -> None:
    """Refresh rapido best-effort (helper historico, YA NO se usa en local).

    La campana local NO refresca la pagina: la entrada y el cambio de cuenta
    van directos para no pagar un refresh por accion. Se conserva por
    compatibilidad/tests; nunca lanza.
    """
    if bot is None:
        return
    refrescar = getattr(bot, "refrescar_pagina_rapido", None)
    if not callable(refrescar):
        return
    try:
        estado = refrescar()
        logger.debug(f"Campana local: refresh de entrada -> {estado}")
    except Exception as e:
        logger.debug(f"Campana local: refresh de entrada fallo: {e}")


def _mod_ia():
    """Import perezoso del modulo de IA (para leer umbrales sin acoplarse)."""
    from ia import generador_contenido

    return generador_contenido


def _umbral_post_local():
    """Umbral de similitud de posts (None si el backend es viejo/falla)."""
    try:
        return getattr(_mod_ia(), "DIVERSIDAD_UMBRAL_POST", None)
    except Exception:
        return None


def _es_texto_diverso_local(texto, otros, umbral=None) -> bool:
    """True si `texto` no es igual ni similar a `otros` (motor de `ia`)."""
    t = str(texto or "").strip()
    if not t:
        return False
    try:
        from ia.generador_contenido import (
            DIVERSIDAD_UMBRAL_CITA,
            texto_es_suficientemente_distinto,
        )

        limite = DIVERSIDAD_UMBRAL_CITA if umbral is None else umbral
        return bool(texto_es_suficientemente_distinto(
            t, list(otros or []), limite
        ))
    except Exception:
        try:
            normalizar = " ".join(t.lower().split())
            return normalizar not in {
                " ".join(str(x or "").lower().split()) for x in (otros or [])
            }
        except Exception:
            return True


def _cita_local_diversa_local(cuenta, base, hashtags, evitar, semilla=None) -> str:
    """Cita local diversa (sin IA) para una cuenta de la campana LOCAL."""
    try:
        from activaciones.variaciones import generar_pool_variaciones_diversas

        textos = generar_pool_variaciones_diversas(
            str(base or "").strip() or _FALLBACK_CITA,
            1,
            registro=_normalizar_registro(cuenta.get("tipo_cuenta")),
            perfil=_normalizar_perfil(cuenta.get("perfil_personalidad")),
            hashtags=_lista_hashtags(hashtags),
            evitar=list(evitar or []),
            limite=_LIMITE_TEXTO_LOCAL,
            semilla=semilla,
        )
        if isinstance(textos, list) and textos:
            return str(textos[0] or "").strip()
    except Exception as e:
        logger.debug(f"Campana local: cita diversa fallo: {e}")
    return ""


def _esta_vivo(bot) -> bool:
    comprobar = getattr(bot, "esta_vivo", None)
    if not callable(comprobar):
        return True
    try:
        return bool(comprobar())
    except Exception:
        return False


# Mensajes REALES de X cuando la cuenta quedo bloqueada / pide verificacion de
# identidad. La cuenta debe salir de la campana y quedar en el cajon de
# verificacion humana (no es un fallo transitorio de red).
_SENALES_BLOQUEO_CUENTA = (
    "your account has locked",
    "unusual activity",
    "confirm your identity",
    "pass a challenge",
    "account unlocked",
    "cuenta bloqueada",
    "actividad inusual",
    "confirma tu identidad",
    "verifica tu identidad",
)

_SENALES_SESION_INVALIDA = (
    "sesión de x expirada",
    "sesion de x expirada",
    "sesion expirada",
    "se pidió login",
    "se pidio login",
    "onboarding",
    "i/jf/onboarding",
    "introduce tu número de teléfono",
    "introduce tu numero de telefono",
    "add phone number",
    "crea tu cuenta",
    "sign up",
    "muro de login",
    "anti-bot",
    "anti bot",
    "cloudflare",
) + _SENALES_BLOQUEO_CUENTA

_REFRESH_SESION_INVALIDA = ("login", "anti_bot", "driver")


def _normalizar_detalle(detalle) -> str:
    try:
        texto = str(detalle or "")
    except Exception:
        return ""
    try:
        import unicodedata

        texto = unicodedata.normalize("NFKD", texto)
        texto = "".join(
            caracter for caracter in texto
            if not unicodedata.combining(caracter)
        )
    except Exception:
        pass
    try:
        return " ".join(texto.lower().split())
    except Exception:
        return ""


def _error_sesion_invalida(detalle) -> bool:
    """True si el detalle indica login/onboarding/anti-bot (nunca lanza)."""
    texto = _normalizar_detalle(detalle)
    if not texto:
        return False
    if "compositor de x no cargo" in texto:
        return any(
            senal in texto for senal in ("login", "onboarding", "sesion")
        )
    for senal in _SENALES_SESION_INVALIDA:
        normalizada = _normalizar_detalle(senal)
        if normalizada and normalizada in texto:
            return True
    return False


_SENALES_ANTI_BOT = (
    "anti-bot",
    "anti bot",
    "cloudflare",
    "just a moment",
    "verificación anti-bot",
    "verificacion anti-bot",
    "verifying you are human",
    "challenge",
    "access denied",
)


def _error_anti_bot(detalle) -> bool:
    """True si el detalle del fallo apunta a un reto anti-bot (Cloudflare)."""
    texto = _normalizar_detalle(detalle)
    if not texto:
        return False
    for senal in _SENALES_ANTI_BOT:
        normalizada = _normalizar_detalle(senal)
        if normalizada and normalizada in texto:
            return True
    return False


def _error_bloqueo_cuenta(detalle) -> bool:
    """True si el detalle es un bloqueo/verificacion de identidad de X.

    Mismo estilo defensivo que `_error_anti_bot`: normaliza tildes y
    mayusculas y nunca lanza. Un bloqueo NO es un fallo transitorio de red: la
    cuenta debe omitirse y quedar en el cajon de verificacion humana.
    """
    texto = _normalizar_detalle(detalle)
    if not texto:
        return False
    try:
        for senal in _SENALES_BLOQUEO_CUENTA:
            normalizada = _normalizar_detalle(senal)
            if normalizada and normalizada in texto:
                return True
    except Exception:
        return False
    return False


def _motivo_verificacion(detalle, bot=None) -> str:
    """Motivo real para el cajon de verificacion; "" si NO aplica.

    Revisa el detalle del fallo y, si viene vacio, `bot.ultimo_error` /
    `bot.ultimo_refresh`. Un fallo SOLO de driver/red transitoria devuelve ""
    (no se manda la cuenta al cajon). Nunca lanza.
    """
    try:
        texto = str(detalle or "").strip()
    except Exception:
        texto = ""
    if not texto:
        try:
            texto = str(getattr(bot, "ultimo_error", "") or "").strip()
        except Exception:
            texto = ""
    if texto and (
        _error_sesion_invalida(texto)
        or _error_anti_bot(texto)
        or _error_bloqueo_cuenta(texto)
    ):
        return texto
    try:
        estado = str(getattr(bot, "ultimo_refresh", "") or "").strip().lower()
    except Exception:
        estado = ""
    # "driver" a proposito NO cuenta: es un fallo transitorio de navegador.
    if estado in ("login", "anti_bot"):
        return f"refresh={estado}: {texto}" if texto else f"refresh={estado}"
    return ""


def _esperar_resolucion_anti_bot(bot, segundos, cancelar) -> bool:
    """Espera (poll 2s, cancelable) a que el reto anti-bot desaparezca.

    Duck-typed: usa `bot.es_pagina_anti_bot()`. Si el bot no expone el metodo
    NO se espera (devuelve False) para no frenar bots/fakes viejos. Devuelve
    True si el humano lo resolvio antes del tope; nunca lanza.
    """
    comprobar = getattr(bot, "es_pagina_anti_bot", None) if bot is not None else None
    if not callable(comprobar):
        return False
    tope = _a_int(segundos, 0)
    if tope <= 0:
        return False
    fin = time.monotonic() + float(tope)
    while True:
        if _cancelado(cancelar):
            return False
        try:
            if not comprobar():
                return True
        except Exception as e:
            logger.debug(f"Campana local: sondeo anti-bot fallo: {e}")
            return False
        restante = fin - time.monotonic()
        if restante <= 0:
            return False
        time.sleep(min(2.0, restante))


def _guardar_sesion_actual(bot) -> bool:
    """`bot.guardar_sesion_actual()` duck-typed (conserva cf_clearance)."""
    guardar = getattr(bot, "guardar_sesion_actual", None) if bot is not None else None
    if not callable(guardar):
        return False
    try:
        return bool(guardar())
    except Exception as e:
        logger.debug(f"Campana local: no se pudo guardar la sesion: {e}")
        return False


def _acumular_estadisticas_proxy(bot, resumen: dict) -> None:
    """Suma `bot._fwd_proxy.estadisticas()` al resumen (bytes). Nunca lanza."""
    if bot is None or not isinstance(resumen, dict):
        return
    fwd = getattr(bot, "_fwd_proxy", None)
    if fwd is None:
        return
    estadisticas = getattr(fwd, "estadisticas", None)
    if not callable(estadisticas):
        return
    try:
        datos = estadisticas()
    except Exception:
        return
    if not isinstance(datos, dict):
        return
    for clave_resumen, clave_datos in (
        ("datos_proxy_subida", "subida"),
        ("datos_proxy_bajada", "bajada"),
        ("datos_proxy_total", "total"),
    ):
        try:
            resumen[clave_resumen] = int(resumen.get(clave_resumen, 0) or 0) + int(
                datos.get(clave_datos, 0) or 0
            )
        except Exception:
            continue


def _sesion_invalida_bot(bot) -> bool:
    """True si el ultimo refresh/error del bot indica sesion invalida."""
    if bot is None:
        return False
    try:
        estado = str(getattr(bot, "ultimo_refresh", "") or "").strip().lower()
    except Exception:
        estado = ""
    if estado in _REFRESH_SESION_INVALIDA:
        return True
    try:
        return _error_sesion_invalida(getattr(bot, "ultimo_error", ""))
    except Exception:
        return False


def _cerrar_bot(bot, resumen=None) -> None:
    """Cierra el bot; si se pasa `resumen`, suma antes los bytes del proxy."""
    if bot is None:
        return
    if isinstance(resumen, dict):
        _acumular_estadisticas_proxy(bot, resumen)
    cerrar = getattr(bot, "cerrar", None)
    if callable(cerrar):
        try:
            cerrar()
        except Exception as e:
            logger.debug(f"Campana local: error cerrando navegador: {e}")


def _elegir_imagen(imagenes, probabilidad) -> str:
    if not imagenes or probabilidad <= 0:
        return ""
    try:
        if random.random() * 100.0 >= probabilidad:
            return ""
        return random.choice(imagenes)
    except Exception:
        return ""


def _ejecutar_accion_local(bot, accion, usuario, rol, urls_lista,
                           capturar_url, dar_like, imagenes_lista,
                           probabilidad):
    """Ejecuta UNA accion (post con hashtag o cita) y devuelve `(ok, url, detalle)`.

    Extraido del bucle de `ejecutar_campana_local` SIN cambiar el
    comportamiento: lo reusa el modo asistido de Cloudflare para reintentar la
    misma accion una vez. Nunca lanza.
    """
    texto = accion.get("texto") or ""
    url_publicada = ""
    detalle = ""
    ok = False
    try:
        if rol == "hashtags":
            imagen = _elegir_imagen(imagenes_lista, probabilidad)
            accion["imagen"] = imagen or None
            extra_perfil = _kwargs_perfil_rapido(
                bot.publicar_tweet, capturar_url
            )
            try:
                respuesta = bot.publicar_tweet(
                    texto,
                    imagen_path=imagen or None,
                    buscar_url=capturar_url,
                    **extra_perfil,
                )
            except TypeError:
                respuesta = bot.publicar_tweet(
                    texto,
                    buscar_url=capturar_url,
                    **extra_perfil,
                )
            ok = bool(respuesta)
            if isinstance(respuesta, str):
                url_publicada = _url_publicacion(respuesta)
            if ok and not url_publicada:
                url_publicada = _url_publicacion(
                    getattr(bot, "ultima_url_publicada", "")
                )
            detalle = "ok" if ok else (
                getattr(bot, "ultimo_error", "") or "sin exito"
            )
        else:
            ancla = accion.get("url") or (urls_lista[0] if urls_lista else "")
            extra_perfil = _kwargs_perfil_rapido(
                bot.solo_retwittear, capturar_url
            )
            try:
                respuesta = bot.solo_retwittear(
                    [ancla],
                    usuario,
                    mensaje_cita=texto,
                    dar_like=dar_like,
                    buscar_url=capturar_url,
                    **extra_perfil,
                )
            except TypeError:
                # Bot/fake viejo sin el kwarg: se mantiene el
                # comportamiento anterior (captura de link).
                respuesta = bot.solo_retwittear(
                    [ancla],
                    usuario,
                    mensaje_cita=texto,
                    dar_like=dar_like,
                )
            if not isinstance(respuesta, dict):
                respuesta = {}
            ok = int(respuesta.get("exitos", 0) or 0) > 0
            for candidata in respuesta.get("urls") or []:
                validada = _url_publicacion(candidata, ancla)
                if validada:
                    url_publicada = validada
                    break
            if ok and not url_publicada:
                url_publicada = _url_publicacion(
                    getattr(bot, "ultima_url_publicada", ""), ancla
                )
            detalle = "ok" if ok else (
                getattr(bot, "ultimo_error", "") or "sin exito"
            )
    except Exception as e:
        ok = False
        detalle = f"{type(e).__name__}: {e}"
        logger.warning(f"Campana local @{usuario} ({rol}): {detalle}")
    return ok, url_publicada, detalle


def _cancelado(cancelar) -> bool:
    if cancelar is None:
        return False
    try:
        return bool(cancelar.is_set())
    except AttributeError:
        try:
            return bool(cancelar())
        except Exception:
            return False
    except Exception:
        return False


def _esperar_turno(objetivo: float, cancelar) -> bool:
    """Duerme por tramos <=0.5s hasta `objetivo`; False si se cancelo."""
    while True:
        if _cancelado(cancelar):
            return False
        restante = objetivo - time.monotonic()
        if restante <= 0:
            return True
        time.sleep(min(0.5, restante))


def _objetivos(total: int, ventana_seg: float, t0: float,
               pausa_entre_seg: float = 0.0) -> list:
    """Instantes de cada accion: la primera inmediata, tope `ventana_seg`.

    `pausa_entre_seg` (nuevo, default 0 = comportamiento clasico de repartir
    en TODA la ventana): si es > 0, la separacion entre acciones es
    ``min(ventana/total, pausa)``. La ventana queda como MAXIMO y la campana
    termina antes (ritmo rapido), que es lo que pide el modo local.
    """
    if total <= 0:
        return []
    objetivos = [t0]
    if ventana_seg and ventana_seg > 0:
        gap_ventana = float(ventana_seg) / max(1, total)
        try:
            pausa = max(0.0, float(pausa_entre_seg or 0))
        except (TypeError, ValueError):
            pausa = 0.0
        gap = min(gap_ventana, pausa) if pausa > 0 else gap_ventana
        if gap <= 0:
            gap = gap_ventana
        for indice in range(1, total):
            jitter = random.uniform(0.0, min(5.0, gap / 4.0))
            objetivos.append(t0 + min(float(ventana_seg), indice * gap + jitter))
    else:
        instante = t0
        for _indice in range(1, total):
            instante += random.uniform(5.0, 10.0)
            objetivos.append(instante)
    return objetivos


def ejecutar_campana_local(
    usuarios=None,
    urls=None,
    hashtags="",
    texto_base="",
    contexto="",
    narrativa="",
    menciones="",
    imagenes=None,
    probabilidad_imagen=100,
    acciones_por_cuenta=6,
    duracion_min=60,
    limite_dia=None,
    dar_like=False,
    permitir_pausadas=False,
    dejar_abierto=False,
    cancelar=None,
    callback=None,
    pausa_entre_acciones_seg=2,
    capturar_url=False,
    usar_proxy: bool = False,
    proxies_pais: str = "mexico",
    sin_imagenes: bool | None = None,
    esperar_captcha_seg: int = 0,
    navegadores: int = 1,
) -> dict:
    """Ejecuta la campana LOCAL secuencial (ver docstring del modulo).

    Nunca lanza: ante cualquier error devuelve ``{"ok": False, "error": ...}``
    con el resto del resumen en ceros.

    ``LOCAL_MAX_CUENTAS`` = 0 (default) significa SIN TOPE: entran todas las
    cuentas Tier 1 con sesion. Solo un valor ``> 0`` recorta. El tope diario
    de 49 acciones por cuenta de X SE CONSERVA.

    No se refresca la pagina en local (ni al entrar ni al cambiar de cuenta):
    maxima velocidad; si una sesion falla se omite la cuenta. `pausa_entre_
    acciones_seg` (default 2): separacion minima entre acciones; la ventana es
    un MAXIMO y la campana termina antes. `capturar_url` (default False): si se
    activa, busca el link de cada post/cita por la ruta rapida
    (toast/timeline), SIN visitar el perfil; apagado prioriza velocidad y
    Reportes muestra "sin link".

    Kwargs NUEVOS al final (defaults = comportamiento historico):

    * ``usar_proxy`` (False): pre-vuelo que asigna/persiste UN proxy sticky
      valido por cuenta, abre Chrome con forwarder dinamico y lo cambia en
      caliente. Si NO hay ningun proxy valido la campana aborta SIN abrir
      Chrome (no hay fallback silencioso a IP directa).
    * ``proxies_pais`` ("mexico"): pais de ``data/proxies/<pais>.txt``.
    * ``sin_imagenes`` (None): con proxy, True/"auto" pide
      ``CHROME_SIN_IMAGENES=1``; False prioriza calidad (``0``).
    * ``esperar_captcha_seg`` (0): modo asistido de Cloudflare; si una accion
      falla por anti-bot se avisa por callback (``tipo="espera_captcha"``) y se
      espera a que el humano lo resuelva en el Chrome visible para reintentar
      UNA vez. ``0`` = comportamiento exacto al historico.
    * ``navegadores`` (1): 1..3 Chrome visibles en paralelo. Cada cuenta queda
      ENTERA en un solo navegador (nunca se parte entre dos); el plan se
      reparte por grupos con greedy y los workers arrancan escalonados.
      ``1`` = comportamiento historico EXACTO.
    """
    usar_proxy = bool(usar_proxy)
    captcha_seg = max(0, _a_int(esperar_captcha_seg, 0))
    navegadores_n = _clamp(_a_int(navegadores, 1), 1, 3)
    pais_proxies = str(proxies_pais or "mexico").strip() or "mexico"
    t_inicio = time.monotonic()

    limite = limite_dia
    if limite is None:
        limite = getattr(settings, "limite_local_dia", 49) if settings else 49
    limite = _a_int(limite, 49)

    max_cuentas = _a_int(
        getattr(settings, "local_max_cuentas", 0) if settings else 0, 0
    )
    # 0 (o negativo) = SIN TOPE: no se fuerza a 1. Solo `_seleccionar_cuentas`
    # recorta cuando `max_cuentas > 0`.

    acciones = _clamp(_a_int(acciones_por_cuenta, 6), 1, 49)
    duracion_original = _a_int(duracion_min, 60)
    if duracion_original <= 0:
        duracion_efectiva = 0
        ventana_seg = 0.0
    else:
        duracion_efectiva = _clamp(duracion_original, 1, 180)
        ventana_seg = float(duracion_efectiva * 60)

    probabilidad = _a_float(probabilidad_imagen, 100.0)
    probabilidad = max(0.0, min(100.0, probabilidad))
    pausa_entre = max(
        0.0, min(300.0, _a_float(pausa_entre_acciones_seg, 2.0))
    )
    capturar_url = bool(capturar_url)

    hashtags_texto = _normalizar_hashtags_texto(hashtags)
    urls_lista = _normalizar_urls(urls)
    imagenes_lista = _normalizar_imagenes(imagenes)
    contexto_texto = _limpiar_texto(contexto)
    narrativa_texto = _limpiar_texto(narrativa)
    texto_base_texto = _limpiar_texto(texto_base)

    # Si el campo de hashtags quedo vacio, el dueño suele escribir el # dentro
    # del contexto o del texto base: se detectan y se usan (el texto SIEMPRE
    # lleva el # pedido). La narrativa de noticias NO se escanea (es trasfondo).
    hashtags_auto: list = []
    if not hashtags_texto:
        hashtags_auto = _hashtags_desde_texto(
            contexto_texto, texto_base_texto
        )
        if hashtags_auto:
            hashtags_texto = " ".join(hashtags_auto)

    resumen = _resumen_inicial(
        limite, acciones, duracion_efectiva, len(imagenes_lista), probabilidad
    )
    resumen["hashtags"] = hashtags_texto
    resumen["hashtags_auto"] = list(hashtags_auto)
    resumen["limite_texto"] = _LIMITE_TEXTO_LOCAL
    resumen["pausa_entre_acciones_seg"] = pausa_entre
    resumen["capturar_url"] = capturar_url
    resumen["usar_proxy"] = usar_proxy
    resumen["sin_proxy"] = not usar_proxy
    resumen["esperar_captcha_seg"] = captcha_seg
    resumen["navegadores"] = navegadores_n

    # Copia runtime de `_ENV_LOCAL` (el dict original queda intacto: los tests
    # lo leen). Sin proxy = valores historicos exactos.
    env_campana = dict(_ENV_LOCAL)
    if usar_proxy:
        env_campana["TWITTER_SIN_PROXY"] = "0"
        env_campana["CHROME_SIN_IMAGENES"] = (
            "0" if sin_imagenes is False else "1"
        )
    else:
        env_campana["TWITTER_SIN_PROXY"] = "1"
        env_campana["CHROME_SIN_IMAGENES"] = "0"

    if not urls_lista and not (hashtags_texto or contexto_texto or narrativa_texto):
        resumen["ok"] = False
        resumen["error"] = (
            "sin URL de tweet ancla y sin material para posts: envia urls y/o "
            "hashtags/contexto/narrativa"
        )
        resumen["motivo"] = "sin material ni URLs"
        return resumen

    tiene_urls = bool(urls_lista)
    tiene_posts = bool(hashtags_texto or contexto_texto or narrativa_texto)
    if tiene_urls and tiene_posts:
        roles = ["hashtags", "cita"]
    elif tiene_urls:
        roles = ["cita"]
    else:
        roles = ["hashtags"]

    # Una corrida anterior pudo dejar Chrome abierto (`dejar_abierto=True`).
    # Cerrarlo ANTES de crear el navegador nuevo evita el bloqueo del perfil
    # (`user data directory is already in use`) y sesiones mezcladas.
    try:
        cerrar_navegadores_locales()
    except Exception:
        pass

    estado = {"bot": None, "bot_cerrado": False, "bots": []}
    try:
        with _entorno_local(env_campana):
            headless_previo = getattr(settings, "headless", None) if settings else None
            try:
                if settings is not None and headless_previo:
                    try:
                        settings.headless = False
                    except Exception:
                        pass

                cuentas, no_encontradas, error_lectura = _leer_cuentas(usuarios)
                resumen["cuentas_total"] = len(cuentas)
                resumen["omitidas_usuarios"]["no_encontradas"] = list(no_encontradas or [])
                if error_lectura:
                    resumen["ok"] = False
                    resumen["error"] = error_lectura
                    resumen["motivo"] = "error de BD"
                    return resumen

                seleccion = _seleccionar_cuentas(
                    cuentas, usuarios, permitir_pausadas, max_cuentas
                )
                selected = seleccion["selected"]
                resumen["omitidas_tier"] = len(seleccion["omitidas_tier"])
                resumen["omitidas_sin_sesion"] = len(seleccion["omitidas_sin_sesion"])
                resumen["omitidas_pausadas"] = len(seleccion["omitidas_pausadas"])
                verificacion_omitidas = list(
                    seleccion.get("omitidas_verificacion") or []
                )
                resumen["omitidas_verificacion"] = len(verificacion_omitidas)
                resumen["recortadas"] = seleccion["recortadas"]
                resumen["omitidas_usuarios"]["tier"] = seleccion["omitidas_tier"]
                resumen["omitidas_usuarios"]["sin_sesion"] = seleccion["omitidas_sin_sesion"]
                resumen["omitidas_usuarios"]["pausadas"] = seleccion["omitidas_pausadas"]
                resumen["omitidas_usuarios"]["verificacion"] = verificacion_omitidas
                resumen["cuentas"] = [
                    str(cuenta.get("usuario") or "") for cuenta in selected
                ]
                resumen["cuentas_seleccionadas"] = len(selected)

                if not selected:
                    resumen["ok"] = False
                    resumen["error"] = (
                        "sin cuentas Tier 1 elegibles (tier/sesion/pausa)"
                    )
                    resumen["motivo"] = "sin cuentas elegibles"
                    return resumen

                # Pre-vuelo de proxies (SOLO con `usar_proxy`), ANTES de crear
                # Chrome: asigna/persiste UN proxy sticky valido por cuenta.
                # Sin proxies validos se aborta sin navegador ni acciones (el
                # objetivo es evitar el reto de Cloudflare de la IP directa).
                proxy_por_usuario: dict = {}
                if usar_proxy:
                    proxy_por_usuario, proxies_invalidos, error_proxies = (
                        _asignar_proxies_cuentas(
                            [cuenta.get("usuario") for cuenta in selected],
                            pais_proxies,
                        )
                    )
                    resumen["proxies_asignados"] = len(proxy_por_usuario)
                    resumen["proxies_invalidos"] = proxies_invalidos
                    if not proxy_por_usuario:
                        resumen["ok"] = False
                        resumen["error"] = (
                            error_proxies
                            or "No hay proxies válidos: no se abre Chrome sin proxy"
                        )
                        resumen["motivo"] = "sin proxies"
                        return resumen

                usados = _contar_usados(resumen["cuentas"])
                construido = _construir_plan(
                    selected, roles, acciones, usados, limite, urls_lista
                )
                plan = construido["plan"]
                resumen["omitidas_por_cuota"] = len(construido["omitidas_cuota"])
                resumen["omitidas_usuarios"]["cuota"] = construido["omitidas_cuota"]
                resumen["planificadas"] = len(plan)
                resumen["por_cuenta"] = {
                    str(cuenta.get("usuario") or ""): {
                        "planificadas": 0,
                        "hechas": 0,
                        "exitosas": 0,
                        "fallidas": 0,
                    }
                    for cuenta in selected
                }
                for accion in plan:
                    fila = resumen["por_cuenta"].get(accion["usuario"])
                    if fila is not None:
                        fila["planificadas"] += 1

                if not plan:
                    resumen["ok"] = False
                    resumen["error"] = (
                        "sin acciones para ejecutar (cuota diaria agotada)"
                    )
                    resumen["motivo"] = "sin acciones"
                    return resumen

                if _cancelado(cancelar):
                    resumen["cancelada"] = True
                    resumen["motivo"] = "cancelada"
                    resumen["omitidas_por_tiempo"] = len(plan)
                    resumen["omitidas_usuarios"]["tiempo"] = [
                        accion["usuario"] for accion in plan
                    ]
                    return resumen

                # Abre Chrome ANTES de generar los textos: el usuario ve la
                # ventana aparecer apenas pulsa Run, mientras la IA trabaja.
                hechas = exitosas = fallidas = 0
                sesiones_caidas = set()
                marcadas_verificacion = set()
                usados_intento = set()
                # Lock compartido por los workers multi-navegador: protege los
                # contadores, eventos, sets y el registro en BD. RLock porque
                # helpers como `_marcar_sin_sesion` pueden anidarse.
                _lock_resumen = threading.RLock()
                navegadores_creados = 0
                recreaciones = 0
                bot = None
                primer_usuario = plan[0]["usuario"]

                def _crear(usuario: str, inicial: bool = False):
                    nonlocal navegadores_creados, recreaciones
                    if not inicial:
                        recreaciones += 1
                    try:
                        nuevo = _crear_bot(usuario)
                    except Exception as e:
                        logger.warning(f"Campana local: no se pudo crear el bot: {e}")
                        nuevo = None
                    if nuevo is None:
                        return None
                    if not _iniciar_driver(nuevo, usar_proxy=usar_proxy):
                        _cerrar_bot(nuevo, resumen)
                        return None
                    navegadores_creados += 1
                    resumen["navegadores_creados"] = navegadores_creados
                    return nuevo

                def _marcar_sin_sesion(usuario: str, motivo: str = "",
                                       bot_actual=None,
                                       hechas_actual=None,
                                       total_actual=None) -> None:
                    """Omite la cuenta y la manda al cajon de verificacion (1 vez).

                    Solo marca pendiente si el motivo (o `bot.ultimo_error` /
                    `bot.ultimo_refresh`) es login/sesion invalida, anti-bot o
                    bloqueo REAL de X. Un fallo SOLO de driver/red transitoria
                    NO se manda al cajon. `bot_actual`/`hechas_actual`/
                    `total_actual` los usan los workers multi-navegador para
                    reportar SU bot y SU avance; en el camino de 1 navegador se
                    omiten (comportamiento historico).
                    """
                    with _lock_resumen:
                        if usuario in sesiones_caidas:
                            return
                        sesiones_caidas.add(usuario)
                        resumen["omitidas_sin_sesion"] += 1
                        resumen["omitidas_usuarios"]["sin_sesion"].append(usuario)

                        bot_ctx = bot if bot_actual is None else bot_actual
                        hechas_ctx = (
                            hechas if hechas_actual is None else hechas_actual
                        )
                        total_ctx = (
                            len(plan) if total_actual is None else total_actual
                        )
                        detalle = _motivo_verificacion(motivo, bot_ctx)
                        if not detalle or usuario in marcadas_verificacion:
                            return
                        try:
                            from core.verificacion import marcar_pendiente
                        except Exception as e:
                            logger.debug(
                                f"Campana local: core.verificacion no "
                                f"disponible: {e}"
                            )
                            return
                        marcada = False
                        try:
                            marcada = bool(
                                marcar_pendiente(
                                    usuario,
                                    motivo=detalle[:300],
                                    origen="local",
                                )
                            )
                        except Exception as e:
                            logger.debug(
                                f"Campana local: no se pudo marcar "
                                f"verificacion de @{usuario}: {e}"
                            )
                        if not marcada:
                            return
                        marcadas_verificacion.add(usuario)
                        resumen["verificaciones_marcadas"] = int(
                            resumen.get("verificaciones_marcadas", 0) or 0
                        ) + 1
                        usuarios_marcados = resumen.setdefault(
                            "verificaciones_usuarios", []
                        )
                        if usuario not in usuarios_marcados:
                            usuarios_marcados.append(usuario)
                        logger.warning(
                            f"Campana local: @{usuario} quedo en el cajon de "
                            f"verificacion humana ({detalle[:100]})"
                        )
                        if callback is not None:
                            try:
                                callback(
                                    {
                                        "tipo": "verificacion_requerida",
                                        "usuario": usuario,
                                        "motivo": detalle[:200],
                                        "hechas": int(hechas_ctx or 0),
                                        "total": int(total_ctx or 0),
                                    }
                                )
                            except Exception:
                                pass

                if navegadores_n <= 1:
                    bot = _crear(primer_usuario, inicial=True)
                    estado["bot"] = bot
                    if bot is None:
                        _marcar_sin_sesion(primer_usuario)
                    elif not _preparar_sesion(bot):
                        _marcar_sin_sesion(
                            primer_usuario,
                            str(getattr(bot, "ultimo_error", "") or ""),
                        )
                    else:
                        # Local NO refresca: maxima velocidad. Si la sesion
                        # esta invalida, se omite la cuenta (sin refresh).
                        if _sesion_invalida_bot(bot):
                            _marcar_sin_sesion(
                                primer_usuario,
                                str(getattr(bot, "ultimo_error", "") or ""),
                            )

                max_posts = 0
                for acciones_cuenta in construido["por_cuenta"].values():
                    max_posts = max(
                        max_posts,
                        sum(1 for accion in acciones_cuenta if accion["rol"] == "hashtags"),
                    )
                posts_por_cuenta = {}
                # Posts y citas son independientes: se generan EN PARALELO para
                # que el primer post no espere dos llamadas de IA en serie.
                with ThreadPoolExecutor(max_workers=2) as _gen_pool:
                    fut_posts = _gen_pool.submit(
                        _generar_posts,
                        selected,
                        hashtags_texto,
                        contexto_texto,
                        narrativa_texto,
                        max_posts,
                    )
                    fut_citas = _gen_pool.submit(
                        _generar_citas,
                        plan,
                        texto_base_texto,
                        narrativa_texto,
                        hashtags_texto,
                    )
                    try:
                        posts_por_cuenta = fut_posts.result()
                    except Exception as e:
                        logger.warning(
                            f"Campana local: generacion de posts fallo: {e}"
                        )
                        posts_por_cuenta = {}
                    try:
                        fut_citas.result()
                    except Exception as e:
                        logger.warning(
                            f"Campana local: generacion de citas fallo: {e}"
                        )
                if not isinstance(posts_por_cuenta, dict):
                    posts_por_cuenta = {}
                for usuario, textos in posts_por_cuenta.items():
                    indice = 0
                    for accion in construido["por_cuenta"].get(usuario, []):
                        if accion["rol"] != "hashtags":
                            continue
                        accion["texto"] = textos[indice] if indice < len(textos) else ""
                        indice += 1

                if _cancelado(cancelar):
                    resumen["cancelada"] = True
                    resumen["motivo"] = "cancelada"
                    resumen["omitidas_por_tiempo"] = len(plan)
                    resumen["omitidas_usuarios"]["tiempo"] = [
                        accion["usuario"] for accion in plan
                    ]
                    return resumen

                if navegadores_n > 1:
                    # ------------------ MULTI-NAVEGADOR (1..3) ------------------
                    cubetas = _partir_plan_por_navegador(plan, navegadores_n)
                    total = len(plan)
                    resumen["navegadores"] = navegadores_n
                    resumen["navegadores_usados"] = 0
                    resumen["por_navegador"] = [
                        {
                            "navegador": indice_nav + 1,
                            "acciones": len(cubeta),
                            "hechas": 0,
                            "exitosas": 0,
                            "fallidas": 0,
                        }
                        for indice_nav, cubeta in enumerate(cubetas)
                    ]
                    resumen["cancelada"] = False
                    resumen["motivo"] = ""
                    eventos = resumen["eventos"]
                    por_cuenta = resumen["por_cuenta"]

                    def _crear_worker(usuario):
                        """Crea/abre UN Chrome para un worker (nunca lanza)."""
                        nonlocal navegadores_creados
                        try:
                            nuevo = _crear_bot(usuario)
                        except Exception as e:
                            logger.warning(
                                f"Campana local (multi): no se pudo crear el "
                                f"bot: {e}"
                            )
                            nuevo = None
                        if nuevo is None:
                            return None
                        if not _iniciar_driver(nuevo, usar_proxy=usar_proxy):
                            _cerrar_bot(nuevo)
                            return None
                        with _lock_resumen:
                            navegadores_creados += 1
                            resumen["navegadores_creados"] = navegadores_creados
                            estado["bots"].append(nuevo)
                        return nuevo

                    def _soltar_bot(bot_w) -> None:
                        """Cierra el bot de un worker y lo saca de `estado`."""
                        if bot_w is None:
                            return
                        try:
                            _cerrar_bot(bot_w)
                        finally:
                            with _lock_resumen:
                                _acumular_estadisticas_proxy(bot_w, resumen)
                                try:
                                    estado["bots"].remove(bot_w)
                                except ValueError:
                                    pass

                    def _omitir_restantes(cubeta, desde, motivo) -> None:
                        """Suma la cola no procesada de una cubeta al resumen."""
                        try:
                            resto = list(cubeta[desde:])
                        except Exception:
                            resto = []
                        if not resto:
                            return
                        with _lock_resumen:
                            resumen["omitidas_por_tiempo"] = int(
                                resumen.get("omitidas_por_tiempo", 0) or 0
                            ) + len(resto)
                            tiempo = resumen["omitidas_usuarios"]["tiempo"]
                            for accion_restante in resto:
                                usuario_restante = str(
                                    accion_restante.get("usuario") or ""
                                )
                                if (
                                    usuario_restante
                                    and usuario_restante not in tiempo
                                ):
                                    tiempo.append(usuario_restante)
                            if motivo == "cancelada":
                                resumen["cancelada"] = True
                                resumen["motivo"] = "cancelada"
                            elif (
                                not resumen.get("cancelada")
                                and not resumen.get("motivo")
                            ):
                                resumen["motivo"] = "navegador no disponible"

                    def _worker_multi(indice_nav, cubeta) -> None:
                        """Procesa SECUENCIALMENTE la cubeta de un navegador."""
                        if not cubeta:
                            return
                        bot_w = None
                        creaciones = 0
                        usados_bot = False
                        detenido = False
                        hechas_w = 0
                        # Arranque escalonado: los workers 1+ no abren Chrome
                        # al mismo instante que el primero.
                        t0 = time.monotonic() + (
                            indice_nav * random.uniform(1.0, 3.0)
                        )
                        objetivos_w = _objetivos(
                            len(cubeta), ventana_seg, t0, pausa_entre
                        )
                        idx_w = 0
                        for idx_w, accion in enumerate(cubeta):
                            if _cancelado(cancelar):
                                _omitir_restantes(cubeta, idx_w, "cancelada")
                                return
                            if not _esperar_turno(objetivos_w[idx_w], cancelar):
                                _omitir_restantes(cubeta, idx_w, "cancelada")
                                return
                            usuario = accion["usuario"]
                            rol = accion["rol"]
                            with _lock_resumen:
                                caida = usuario in sesiones_caidas
                            if caida:
                                continue

                            if bot_w is None:
                                # 1 creacion inicial + 2 recreaciones maximo.
                                if creaciones >= 3:
                                    detenido = True
                                    break
                                bot_w = _crear_worker(usuario)
                                creaciones += 1
                                if bot_w is None:
                                    _marcar_sin_sesion(
                                        usuario,
                                        "",
                                        bot_actual=None,
                                        hechas_actual=hechas_w,
                                        total_actual=total,
                                    )
                                    if creaciones >= 3:
                                        detenido = True
                                        break
                                    continue
                                if not usados_bot:
                                    usados_bot = True
                                    with _lock_resumen:
                                        resumen["navegadores_usados"] = int(
                                            resumen.get(
                                                "navegadores_usados", 0
                                            ) or 0
                                        ) + 1
                                if not _preparar_sesion(bot_w):
                                    _marcar_sin_sesion(
                                        usuario,
                                        str(
                                            getattr(
                                                bot_w, "ultimo_error", ""
                                            ) or ""
                                        ),
                                        bot_actual=bot_w,
                                        hechas_actual=hechas_w,
                                        total_actual=total,
                                    )
                                elif _sesion_invalida_bot(bot_w):
                                    _marcar_sin_sesion(
                                        usuario,
                                        str(
                                            getattr(
                                                bot_w, "ultimo_error", ""
                                            ) or ""
                                        ),
                                        bot_actual=bot_w,
                                        hechas_actual=hechas_w,
                                        total_actual=total,
                                    )
                            elif getattr(bot_w, "usuario", usuario) != usuario:
                                cambiar = getattr(bot_w, "cambiar_cuenta", None)
                                cambiado = False
                                if callable(cambiar):
                                    kwargs_cambio = {}
                                    try:
                                        if _acepta_kwarg(cambiar, "proxy"):
                                            kwargs_cambio["proxy"] = (
                                                proxy_por_usuario.get(
                                                    usuario, ""
                                                )
                                            )
                                        if _acepta_kwarg(cambiar, "refrescar"):
                                            kwargs_cambio["refrescar"] = False
                                    except Exception:
                                        kwargs_cambio = {}
                                    try:
                                        if kwargs_cambio:
                                            try:
                                                cambiado = bool(
                                                    cambiar(
                                                        usuario,
                                                        **kwargs_cambio,
                                                    )
                                                )
                                            except TypeError:
                                                cambiado = bool(
                                                    cambiar(usuario)
                                                )
                                        else:
                                            cambiado = bool(cambiar(usuario))
                                    except Exception:
                                        cambiado = False
                                if not cambiado:
                                    login = getattr(
                                        bot_w, "login_con_cookies", None
                                    )
                                    if callable(login):
                                        try:
                                            cambiado = bool(login())
                                        except Exception:
                                            cambiado = False
                                if not cambiado:
                                    _marcar_sin_sesion(
                                        usuario,
                                        str(
                                            getattr(
                                                bot_w, "ultimo_error", ""
                                            ) or ""
                                        ),
                                        bot_actual=bot_w,
                                        hechas_actual=hechas_w,
                                        total_actual=total,
                                    )
                                    continue
                                if _sesion_invalida_bot(bot_w):
                                    _marcar_sin_sesion(
                                        usuario,
                                        str(
                                            getattr(
                                                bot_w, "ultimo_error", ""
                                            ) or ""
                                        ),
                                        bot_actual=bot_w,
                                        hechas_actual=hechas_w,
                                        total_actual=total,
                                    )
                                    continue

                            ok, url_publicada, detalle = _ejecutar_accion_local(
                                bot_w,
                                accion,
                                usuario,
                                rol,
                                urls_lista,
                                capturar_url,
                                dar_like,
                                imagenes_lista,
                                probabilidad,
                            )

                            # Modo asistido Cloudflare: por accion/worker.
                            if (
                                not ok
                                and captcha_seg > 0
                                and _error_anti_bot(detalle)
                                and _esta_vivo(bot_w)
                                and not _cancelado(cancelar)
                            ):
                                if callback is not None:
                                    with _lock_resumen:
                                        try:
                                            callback(
                                                {
                                                    "tipo": "espera_captcha",
                                                    "usuario": usuario,
                                                    "segundos": captcha_seg,
                                                    "hechas": int(
                                                        resumen.get(
                                                            "hechas", 0
                                                        ) or 0
                                                    ),
                                                    "total": total,
                                                }
                                            )
                                        except Exception:
                                            pass
                                if _esperar_resolucion_anti_bot(
                                    bot_w, captcha_seg, cancelar
                                ):
                                    _guardar_sesion_actual(bot_w)
                                    ok, url_publicada, detalle = (
                                        _ejecutar_accion_local(
                                            bot_w,
                                            accion,
                                            usuario,
                                            rol,
                                            urls_lista,
                                            capturar_url,
                                            dar_like,
                                            imagenes_lista,
                                            probabilidad,
                                        )
                                    )
                                    with _lock_resumen:
                                        clave_captcha = (
                                            "captchas_resueltos"
                                            if ok
                                            else "captchas_agotados"
                                        )
                                        resumen[clave_captcha] = int(
                                            resumen.get(clave_captcha, 0) or 0
                                        ) + 1
                                    if not ok:
                                        _marcar_sin_sesion(
                                            usuario,
                                            detalle
                                            or str(
                                                getattr(
                                                    bot_w, "ultimo_error", ""
                                                ) or ""
                                            ),
                                            bot_actual=bot_w,
                                            hechas_actual=hechas_w,
                                            total_actual=total,
                                        )
                                else:
                                    with _lock_resumen:
                                        resumen["captchas_agotados"] = int(
                                            resumen.get(
                                                "captchas_agotados", 0
                                            ) or 0
                                        ) + 1

                            if ok and usar_proxy:
                                _guardar_sesion_actual(bot_w)

                            hechas_w += 1
                            with _lock_resumen:
                                resumen["hechas"] = int(
                                    resumen.get("hechas", 0) or 0
                                ) + 1
                                if ok:
                                    resumen["exitosas"] = int(
                                        resumen.get("exitosas", 0) or 0
                                    ) + 1
                                else:
                                    resumen["fallidas"] = int(
                                        resumen.get("fallidas", 0) or 0
                                    ) + 1
                                usados_intento.add(usuario)
                                fila = por_cuenta.get(usuario)
                                if fila is not None:
                                    fila["hechas"] += 1
                                    if ok:
                                        fila["exitosas"] += 1
                                    else:
                                        fila["fallidas"] += 1
                                nav_fila = resumen["por_navegador"][indice_nav]
                                nav_fila["hechas"] += 1
                                if ok:
                                    nav_fila["exitosas"] += 1
                                else:
                                    nav_fila["fallidas"] += 1
                                if len(eventos) < 200:
                                    eventos.append(
                                        {
                                            "usuario": usuario,
                                            "rol": rol,
                                            "ok": bool(ok),
                                            "detalle": (detalle or "")[:200],
                                            "url": url_publicada,
                                            "hora": datetime.now().isoformat(
                                                timespec="seconds"
                                            ),
                                        }
                                    )
                                _registrar_accion(
                                    usuario,
                                    _tipo_registro(rol),
                                    "exito" if ok else "fallido",
                                    url_publicada,
                                    detalle,
                                )

                            if not ok and (
                                _error_sesion_invalida(detalle)
                                or _error_anti_bot(detalle)
                                or _error_bloqueo_cuenta(detalle)
                            ):
                                _marcar_sin_sesion(
                                    usuario,
                                    detalle,
                                    bot_actual=bot_w,
                                    hechas_actual=hechas_w,
                                    total_actual=total,
                                )

                            if callback is not None:
                                with _lock_resumen:
                                    try:
                                        callback(
                                            {
                                                "hechas": int(
                                                    resumen.get("hechas", 0)
                                                    or 0
                                                ),
                                                "total": total,
                                                "usuario": usuario,
                                                "rol": rol,
                                                "ok": bool(ok),
                                                "detalle": (detalle or "")[:200],
                                                "url": url_publicada,
                                            }
                                        )
                                    except Exception:
                                        pass

                            if not _esta_vivo(bot_w):
                                _soltar_bot(bot_w)
                                bot_w = None

                        if detenido:
                            _omitir_restantes(cubeta, idx_w, "navegador")

                    activos = [
                        indice_nav
                        for indice_nav, cubeta in enumerate(cubetas)
                        if cubeta
                    ]
                    if activos:
                        with ThreadPoolExecutor(
                            max_workers=len(activos)
                        ) as _pool_nav:
                            futuros_nav = [
                                _pool_nav.submit(
                                    _worker_multi,
                                    indice_nav,
                                    cubetas[indice_nav],
                                )
                                for indice_nav in activos
                            ]
                            for futuro_nav in futuros_nav:
                                try:
                                    futuro_nav.result()
                                except Exception as e:
                                    logger.error(
                                        f"Campana local (multi): worker fallo: "
                                        f"{e}"
                                    )
                    resumen["cuentas_usadas"] = len(usados_intento)
                    resumen["cancelada"] = bool(
                        resumen.get("cancelada", False)
                    )
                    if not resumen.get("motivo"):
                        resumen["motivo"] = "completada"
                    estado["bot"] = None
                    return resumen

                total = len(plan)
                # El reloj del reparto arranca AQUI: la generacion de textos
                # (OpenAI puede tardar) no debe dejar acciones "vencidas" que
                # se ejecuten en rafaga al terminar de generar.
                objetivos = _objetivos(
                    total, ventana_seg, time.monotonic(), pausa_entre
                )

                hechas = exitosas = fallidas = 0
                eventos = resumen["eventos"]
                por_cuenta = resumen["por_cuenta"]
                cancelada = False
                corte_forzado = False

                for indice, accion in enumerate(plan):
                    if _cancelado(cancelar):
                        cancelada = True
                        break
                    if not _esperar_turno(objetivos[indice], cancelar):
                        cancelada = True
                        break
                    if _cancelado(cancelar):
                        cancelada = True
                        break

                    usuario = accion["usuario"]
                    rol = accion["rol"]
                    if usuario in sesiones_caidas:
                        continue

                    if bot is None:
                        if recreaciones >= 2:
                            corte_forzado = True
                            break
                        bot = _crear(usuario)
                        estado["bot"] = bot
                        if bot is None:
                            _marcar_sin_sesion(usuario)
                            continue
                        if not _preparar_sesion(bot):
                            _marcar_sin_sesion(
                                usuario,
                                str(getattr(bot, "ultimo_error", "") or ""),
                            )
                            continue
                        # Local NO refresca: si la sesion esta invalida, se
                        # omite la cuenta en vez de pagar un refresh.
                        if _sesion_invalida_bot(bot):
                            _marcar_sin_sesion(
                                usuario,
                                str(getattr(bot, "ultimo_error", "") or ""),
                            )
                            continue
                    elif getattr(bot, "usuario", usuario) != usuario:
                        cambiar = getattr(bot, "cambiar_cuenta", None)
                        cambiado = False
                        if callable(cambiar):
                            kwargs_cambio = {}
                            try:
                                if _acepta_kwarg(cambiar, "proxy"):
                                    kwargs_cambio["proxy"] = (
                                        proxy_por_usuario.get(usuario, "")
                                    )
                                if _acepta_kwarg(cambiar, "refrescar"):
                                    kwargs_cambio["refrescar"] = False
                            except Exception:
                                kwargs_cambio = {}
                            try:
                                if kwargs_cambio:
                                    try:
                                        cambiado = bool(
                                            cambiar(usuario, **kwargs_cambio)
                                        )
                                    except TypeError:
                                        # Firma vieja que no acepta los kwargs.
                                        cambiado = bool(cambiar(usuario))
                                else:
                                    cambiado = bool(cambiar(usuario))
                            except Exception:
                                cambiado = False
                        if not cambiado:
                            login = getattr(bot, "login_con_cookies", None)
                            if callable(login):
                                try:
                                    cambiado = bool(login())
                                except Exception:
                                    cambiado = False
                        if not cambiado:
                            _marcar_sin_sesion(
                                usuario,
                                str(getattr(bot, "ultimo_error", "") or ""),
                            )
                            continue
                        if _sesion_invalida_bot(bot):
                            _marcar_sin_sesion(
                                usuario,
                                str(getattr(bot, "ultimo_error", "") or ""),
                            )
                            continue

                    ok, url_publicada, detalle = _ejecutar_accion_local(
                        bot,
                        accion,
                        usuario,
                        rol,
                        urls_lista,
                        capturar_url,
                        dar_like,
                        imagenes_lista,
                        probabilidad,
                    )

                    # Modo asistido Cloudflare (opt-in): si fallo por anti-bot,
                    # avisa por callback y espera (cancelable) a que el humano
                    # resuelva el reto en el Chrome visible; luego guarda la
                    # sesion (cf_clearance) y reintenta la MISMA accion UNA vez
                    # (anti-bot no publico nada: el reintento es seguro).
                    if (
                        not ok
                        and captcha_seg > 0
                        and _error_anti_bot(detalle)
                        and _esta_vivo(bot)
                        and not _cancelado(cancelar)
                    ):
                        if callback is not None:
                            try:
                                callback(
                                    {
                                        "tipo": "espera_captcha",
                                        "usuario": usuario,
                                        "segundos": captcha_seg,
                                        "hechas": hechas,
                                        "total": total,
                                    }
                                )
                            except Exception:
                                pass
                        if _esperar_resolucion_anti_bot(
                            bot, captcha_seg, cancelar
                        ):
                            _guardar_sesion_actual(bot)
                            ok, url_publicada, detalle = _ejecutar_accion_local(
                                bot,
                                accion,
                                usuario,
                                rol,
                                urls_lista,
                                capturar_url,
                                dar_like,
                                imagenes_lista,
                                probabilidad,
                            )
                            if ok:
                                resumen["captchas_resueltos"] += 1
                            else:
                                resumen["captchas_agotados"] += 1
                                # El reto se resolvio pero la accion sigue
                                # fallando: la sesion de esa cuenta no sirve.
                                _marcar_sin_sesion(
                                    usuario,
                                    detalle
                                    or str(getattr(bot, "ultimo_error", "") or ""),
                                )
                        else:
                            resumen["captchas_agotados"] += 1

                    # Con proxy: persiste las cookies vivas (cf_clearance/ct0)
                    # tras cada accion exitosa para no repetir el reto.
                    if ok and usar_proxy:
                        _guardar_sesion_actual(bot)

                    hechas += 1
                    usados_intento.add(usuario)
                    if ok:
                        exitosas += 1
                    else:
                        fallidas += 1
                    fila = por_cuenta.get(usuario)
                    if fila is not None:
                        fila["hechas"] += 1
                        if ok:
                            fila["exitosas"] += 1
                        else:
                            fila["fallidas"] += 1

                    evento = {
                        "usuario": usuario,
                        "rol": rol,
                        "ok": bool(ok),
                        "detalle": (detalle or "")[:200],
                        "url": url_publicada,
                        "hora": datetime.now().isoformat(timespec="seconds"),
                    }
                    if len(eventos) < 200:
                        eventos.append(evento)

                    _registrar_accion(
                        usuario,
                        _tipo_registro(rol),
                        "exito" if ok else "fallido",
                        url_publicada,
                        detalle,
                    )

                    if not ok and (
                        _error_sesion_invalida(detalle)
                        or _error_anti_bot(detalle)
                        or _error_bloqueo_cuenta(detalle)
                    ):
                        _marcar_sin_sesion(usuario, detalle)

                    if callback is not None:
                        try:
                            callback(
                                {
                                    "hechas": hechas,
                                    "total": total,
                                    "usuario": usuario,
                                    "rol": rol,
                                    "ok": bool(ok),
                                    "detalle": (detalle or "")[:200],
                                    "url": url_publicada,
                                }
                            )
                        except Exception:
                            pass

                    if not _esta_vivo(bot):
                        _cerrar_bot(bot, resumen)
                        bot = None
                        estado["bot"] = None

                resumen["hechas"] = hechas
                resumen["exitosas"] = exitosas
                resumen["fallidas"] = fallidas
                resumen["cuentas_usadas"] = len(usados_intento)
                resumen["cancelada"] = bool(cancelada)
                if cancelada:
                    pendientes = max(0, total - indice)
                    resumen["omitidas_por_tiempo"] = pendientes
                    resumen["omitidas_usuarios"]["tiempo"] = list(
                        dict.fromkeys(accion["usuario"] for accion in plan[indice:])
                    )
                    resumen["motivo"] = "cancelada"
                elif corte_forzado:
                    pendientes = max(0, total - indice)
                    resumen["omitidas_por_tiempo"] = pendientes
                    resumen["omitidas_usuarios"]["tiempo"] = list(
                        dict.fromkeys(accion["usuario"] for accion in plan[indice:])
                    )
                    resumen["motivo"] = "navegador no disponible"
                else:
                    resumen["motivo"] = "completada"

                estado["bot"] = bot
            finally:
                if settings is not None and headless_previo is not None:
                    try:
                        settings.headless = headless_previo
                    except Exception:
                        pass
    except Exception as e:
        logger.exception(f"Campana local: error inesperado: {e}")
        resumen["ok"] = False
        resumen["error"] = f"{type(e).__name__}: {e}"
        resumen["motivo"] = "error inesperado"
    finally:
        # Cierre unificado: el camino de 1 navegador usa `estado["bot"]`; el
        # multi-navegador deja los vivos en `estado["bots"]` (lista).
        bots_finales: list = []
        bot_simple = estado.get("bot")
        if bot_simple is not None:
            bots_finales.append(bot_simple)
        for bot_extra in list(estado.get("bots") or []):
            if bot_extra is not None and all(
                bot_extra is not registrado for registrado in bots_finales
            ):
                bots_finales.append(bot_extra)
        if bots_finales:
            if dejar_abierto:
                # Se acumulan los bytes consumidos hasta el final de la campana
                # (los navegadores quedan vivos; `cerrar_navegadores_locales`
                # los cierra luego sin tocar el resumen).
                for bot_abierto in bots_finales:
                    _acumular_estadisticas_proxy(bot_abierto, resumen)
                    with _LOCK_NAVEGADORES:
                        _NAVEGADORES_ABIERTOS.append(bot_abierto)
                resumen["navegador_abierto"] = True
            else:
                for bot_cerrado in bots_finales:
                    _cerrar_bot(bot_cerrado, resumen)
            estado["bot"] = None
            estado["bots"] = []
            estado["bot_cerrado"] = True
        resumen["omitidas"] = max(
            0, int(resumen.get("planificadas", 0)) - int(resumen.get("hechas", 0))
        )
        resumen["fin"] = datetime.now().isoformat(timespec="seconds")
        try:
            resumen["segundos"] = round(time.monotonic() - t_inicio, 2)
        except Exception:
            resumen["segundos"] = 0.0

    return resumen


def cerrar_navegadores_locales() -> int:
    """Cierra los Chrome guardados por `dejar_abierto=True`. Devuelve cuantos."""
    cerrados = 0
    with _LOCK_NAVEGADORES:
        pendientes = list(_NAVEGADORES_ABIERTOS)
        _NAVEGADORES_ABIERTOS.clear()
    for bot in pendientes:
        try:
            _cerrar_bot(bot)
            cerrados += 1
        except Exception:
            continue
    return cerrados

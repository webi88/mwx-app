# -*- coding: utf-8 -*-
"""Orquestador de la campana local del ejecutable MWX APP.

Reutiliza `activaciones.local.ejecutar_campana_local` (import perezoso) y se
encarga de lo propio del ejecutable: inyectar el token de sesion como clave
del proxy OpenAI, descargar las imagenes del administrador, aplicar los
ajustes permitidos y enviar los eventos de inicio/fin.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from mwx_app import store
from mwx_app.api_client import MWXClient
from mwx_app.config import dir_datos, normalizar_url_api

ejecutar_campana_local = None

_EXTENSIONES_IMAGEN = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _cargar_ejecutor():
    """Resuelve el motor local una sola vez (monkeypatcheable en tests)."""
    global ejecutar_campana_local
    if ejecutar_campana_local is None:
        from activaciones.local import ejecutar_campana_local as funcion

        ejecutar_campana_local = funcion
    return ejecutar_campana_local


def configurar_token(token: str, api_url: str) -> dict:
    """Apunta el SDK de OpenAI al proxy de la API usando el token de sesion."""
    token = str(token or "").strip()
    base_api = normalizar_url_api(api_url)
    base_openai = f"{base_api}/v1" if base_api else ""

    os.environ["OPENAI_API_KEY"] = token or "mwx-session"
    if base_openai:
        os.environ["OPENAI_API_BASE"] = base_openai
        os.environ["OPENAI_BASE_URL"] = base_openai
    else:
        os.environ.pop("OPENAI_API_BASE", None)
        os.environ.pop("OPENAI_BASE_URL", None)

    clave_ok = False
    base_ok = False
    try:
        from core.config import settings

        settings.openai_api_key = token or "mwx-session"
        settings.openai_api_base = base_openai
        clave_ok = str(getattr(settings, "openai_api_key", "")) == (
            token or "mwx-session"
        )
        base_ok = str(getattr(settings, "openai_api_base", "")) == base_openai
    except Exception as error:
        logger.debug(f"MWX APP: no se pudo ajustar settings de OpenAI: {error}")

    return {
        "api_key": token,
        "api_base": base_openai,
        "settings_api_key": clave_ok,
        "settings_api_base": base_ok,
    }


def limpiar_token() -> None:
    """Quita el token de sesion del proceso (al cerrar sesion)."""
    configurar_token("", "")


def _entero(valor: Any, defecto: int, minimo: int, maximo: int) -> int:
    try:
        numero = int(float(str(valor).strip()))
    except (TypeError, ValueError):
        numero = defecto
    return max(minimo, min(maximo, numero))


def _booleano(valor: Any, defecto: bool = True) -> bool:
    """bool tolerante (None -> `defecto`; acepta bool, 0/1 y si/no/true/false)."""
    if valor is None:
        return bool(defecto)
    if isinstance(valor, str):
        texto = valor.strip().lower()
        if texto in ("1", "true", "si", "sí", "yes", "on"):
            return True
        if texto in ("0", "false", "no", "off", ""):
            return False
        return bool(defecto)
    return bool(valor)


def _entero_resumen(resumen: dict, clave: str) -> Optional[int]:
    """Entero de un campo del resumen del motor (None si no viene)."""
    if not isinstance(resumen, dict) or clave not in resumen:
        return None
    try:
        return int(resumen.get(clave))
    except (TypeError, ValueError):
        return None


def _lista_usuarios(usuarios: Any) -> list:
    if usuarios is None:
        return []
    if isinstance(usuarios, str):
        crudos = [usuarios]
    else:
        try:
            crudos = list(usuarios)
        except TypeError:
            crudos = [usuarios]
    salida, vistos = [], set()
    for crudo in crudos:
        if isinstance(crudo, dict):
            crudo = crudo.get("usuario") or crudo.get("username") or ""
        usuario = str(crudo or "").strip().lstrip("@")
        if not usuario:
            continue
        clave = usuario.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(usuario)
    return salida


def _lista_imagenes(valor: Any) -> list:
    if valor is None:
        return []
    if isinstance(valor, (list, tuple)):
        return list(valor)
    return [valor]


def _carpeta_imagenes() -> Path:
    """Carpeta temporal de la app (``DATA_DIR/temp``)."""
    return Path(dir_datos()) / "temp"


def _extension(nombre: str) -> str:
    sufijo = Path(str(nombre or "")).suffix.lower()
    return sufijo if sufijo in _EXTENSIONES_IMAGEN else ".jpg"


def _descargar_imagenes(
    cliente: Any, token: str, imagenes: Any
) -> list:
    """Descarga las imagenes de la campana; las que fallen se omiten."""
    rutas: list = []
    carpeta = _carpeta_imagenes()
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
    except OSError:
        return rutas

    for indice, item in enumerate(_lista_imagenes(imagenes)[:20], start=1):
        if isinstance(item, dict):
            nombre = item.get("nombre") or item.get("name") or ""
        else:
            nombre = item
        nombre = str(nombre or "").strip()
        if not nombre:
            continue
        try:
            contenido = cliente.descargar_imagen(token, nombre)
        except Exception as error:
            logger.warning(f"MWX APP: no se pudo descargar la imagen {nombre}: {error}")
            continue
        if not contenido:
            continue
        destino = carpeta / f"mwx_img_{indice}{_extension(nombre)}"
        try:
            destino.write_bytes(contenido)
            rutas.append(str(destino))
        except OSError as error:
            logger.warning(f"MWX APP: no se pudo guardar {destino}: {error}")
    return rutas


def _imagenes_locales(imagenes: Any) -> list:
    rutas = []
    for item in _lista_imagenes(imagenes):
        ruta = str(item or "").strip()
        try:
            if ruta and os.path.isfile(ruta):
                rutas.append(ruta)
        except OSError:
            continue
    return rutas


def _enviar_evento(
    cliente: Any, token: str, tipo: str, detalle: str = ""
) -> bool:
    if cliente is None or not token:
        return False
    try:
        cliente.evento(token, tipo, detalle)
        return True
    except Exception:
        return False


def _evento_async(
    cliente: Any, token: str, tipo: str, detalle: str = ""
) -> None:
    hilo = threading.Thread(
        target=_enviar_evento,
        args=(cliente, token, tipo, detalle),
        daemon=True,
    )
    hilo.start()


def _diagnostico_navegadores(config: dict, resumen: dict, usuarios: list) -> dict:
    """Explica por que se usaron menos navegadores que los solicitados."""
    solicitados = int(config.get("navegadores") or 1)
    panel = int(config.get("navegadores_panel") or solicitados)
    origen = str(config.get("navegadores_origen") or "panel")
    usados = _entero_resumen(resumen, "navegadores_usados")
    salida: dict = {
        "navegadores_solicitados": solicitados,
        "navegadores_aviso": "",
    }
    if usados is not None:
        salida["navegadores_usados"] = usados
    if solicitados <= 1 and not (origen == "ajustes" and panel > 1):
        return salida
    if usados == 0:
        aviso = (
            "No se pudo abrir ningún Chrome: revisa Chrome instalado / "
            "chromedriver / RAM."
        )
    elif origen == "ajustes" and solicitados != panel:
        aviso = (
            f"El equipo tiene ajustes locales guardados ({solicitados} "
            f"navegador(es)); el panel dice {panel}. Usa «Usar configuración "
            "del panel» en Ajustes."
        )
    elif usados is None:
        if str(resumen.get("error") or "").strip():
            aviso = ""
        else:
            aviso = (
                "La versión del motor no reporta navegadores; actualiza el "
                "ejecutable."
            )
    elif usados < solicitados and len(usuarios) < solicitados:
        aviso = (
            f"Solo hay {len(usuarios)} cuenta(s) activa(s) asignada(s); se "
            "abrirá 1 Chrome por cuenta disponible. Asigna más cuentas al "
            "usuario."
        )
    elif usados < solicitados:
        aviso = (
            f"Se solicitaron {solicitados} navegadores pero solo se usaron "
            f"{usados}. Revisa RAM/CPU o baja los navegadores."
        )
    else:
        aviso = ""
    if aviso:
        salida["navegadores_aviso"] = aviso
    return salida


def config_efectiva(perfil: dict, ajustes: Optional[dict]) -> dict:
    campana = (perfil or {}).get("campana") or {}
    permitir = bool(campana.get("permitir_ajustes"))
    ajustes = ajustes if (permitir and isinstance(ajustes, dict)) else {}

    acciones = _entero(
        ajustes.get("acciones_por_cuenta", campana.get("acciones_por_cuenta", 10)),
        10,
        1,
        49,
    )
    pausa = _entero(
        ajustes.get("pausa_seg", campana.get("pausa_seg", 2)), 2, 0, 300
    )
    duracion = _entero(
        ajustes.get("duracion_min", campana.get("duracion_min", 60)),
        60,
        1,
        180,
    )
    limite = _entero(
        ajustes.get("limite_dia", campana.get("limite_dia", 49)), 49, 1, 49
    )
    navegadores_panel = _entero(campana.get("navegadores", 1), 1, 1, 3)
    if "navegadores" in ajustes:
        navegadores = _entero(
            ajustes.get("navegadores"), navegadores_panel, 1, 3
        )
        navegadores_origen = "ajustes"
    else:
        navegadores = navegadores_panel
        navegadores_origen = "panel"

    sin_imagenes_panel = _booleano(campana.get("sin_imagenes"), True)
    if "sin_imagenes" in ajustes:
        sin_imagenes = _booleano(
            ajustes.get("sin_imagenes"), sin_imagenes_panel
        )
        sin_imagenes_origen = "ajustes"
    else:
        sin_imagenes = sin_imagenes_panel
        sin_imagenes_origen = "panel"

    hashtags = campana.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags_texto = hashtags.strip()
    else:
        try:
            hashtags_texto = " ".join(
                str(tag or "").strip() for tag in hashtags if str(tag or "").strip()
            )
        except TypeError:
            hashtags_texto = str(hashtags).strip()

    urls = campana.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]

    return {
        "permitir_ajustes": permitir,
        "acciones_por_cuenta": acciones,
        "pausa_seg": pausa,
        "duracion_min": duracion,
        "limite_dia": limite,
        "hashtags": hashtags_texto,
        "contexto": str(campana.get("contexto") or ""),
        "narrativa": str(campana.get("narrativa") or ""),
        "texto_base": str(campana.get("texto_base") or ""),
        "urls": [str(url).strip() for url in urls if str(url or "").strip()],
        "imagenes": campana.get("imagenes") or [],
        "navegadores": navegadores,
        "navegadores_origen": navegadores_origen,
        "navegadores_panel": navegadores_panel,
        "sin_imagenes": sin_imagenes,
        "sin_imagenes_origen": sin_imagenes_origen,
    }


def ejecutar_campana(
    perfil: dict,
    token: str,
    api_url: str,
    ajustes: Optional[dict] = None,
    imagenes_extra: Optional[list] = None,
    cancelar: Optional[threading.Event] = None,
    callback: Optional[Any] = None,
    cliente: Optional[Any] = None,
    usuarios: Optional[list] = None,
) -> dict:
    """Ejecuta la campana local y devuelve el resumen del motor.

    Los ajustes solo se aplican cuando el servidor los permite
    (``permitir_ajustes``). Las imagenes del administrador se descargan a la
    carpeta temporal; si una falla, la campana sigue sin esa imagen.
    """
    perfil = perfil if isinstance(perfil, dict) else {}
    token = str(token or "").strip()
    api_url = normalizar_url_api(api_url)
    config = config_efectiva(perfil, ajustes)

    if usuarios is None:
        usuarios = store.sincronizar_perfil(perfil)
    usuarios = _lista_usuarios(usuarios)

    configurar_token(token, api_url)

    if cliente is None:
        cliente = MWXClient(api_url) if api_url else None

    imagenes = _descargar_imagenes(cliente, token, config["imagenes"]) if cliente else []
    if config["permitir_ajustes"]:
        for ruta in _imagenes_locales(imagenes_extra):
            if ruta not in imagenes:
                imagenes.append(ruta)

    if cancelar is None:
        cancelar = threading.Event()

    detalle_inicio = (
        f"cuentas={len(usuarios)} acciones_por_cuenta={config['acciones_por_cuenta']} "
        f"ventana={config['duracion_min']}min imagenes={len(imagenes)} "
        f"navegadores={config['navegadores']} "
        f"navegadores_origen={config['navegadores_origen']} "
        f"sin_imagenes={config['sin_imagenes']}"
    )
    _evento_async(cliente, token, "campana_inicio", detalle_inicio)

    resumen: dict
    try:
        ejecutor = _cargar_ejecutor()
        resumen = ejecutor(
            usuarios=usuarios,
            urls=config["urls"],
            hashtags=config["hashtags"],
            texto_base=config["texto_base"],
            contexto=config["contexto"],
            narrativa=config["narrativa"],
            imagenes=imagenes,
            probabilidad_imagen=100,
            acciones_por_cuenta=config["acciones_por_cuenta"],
            duracion_min=config["duracion_min"],
            limite_dia=config["limite_dia"],
            cancelar=cancelar,
            callback=callback,
            pausa_entre_acciones_seg=config["pausa_seg"],
            capturar_url=True,
            navegadores=config["navegadores"],
            sin_imagenes=config["sin_imagenes"],
        )
        if not isinstance(resumen, dict):
            resumen = {
                "ok": False,
                "error": "El motor de la campaña devolvió una respuesta inesperada.",
            }
    except Exception as error:
        logger.exception(f"MWX APP: error ejecutando la campana: {error}")
        resumen = {
            "ok": False,
            "error": "No se pudo ejecutar la campaña. Inténtalo de nuevo.",
            "exitosas": 0,
            "fallidas": 0,
            "omitidas": 0,
            "planificadas": 0,
            "hechas": 0,
            "cancelada": False,
            "eventos": [],
        }

    diagnostico = _diagnostico_navegadores(config, resumen, usuarios)
    resumen["navegadores_solicitados"] = diagnostico["navegadores_solicitados"]
    if "navegadores_usados" in diagnostico:
        resumen["navegadores_usados"] = diagnostico["navegadores_usados"]
    if diagnostico.get("navegadores_aviso"):
        resumen["navegadores_aviso"] = diagnostico["navegadores_aviso"]

    detalle_fin = (
        f"exitosas={resumen.get('exitosas', 0)} "
        f"fallidas={resumen.get('fallidas', 0)} "
        f"omitidas={resumen.get('omitidas', 0)} "
        f"cancelada={bool(resumen.get('cancelada'))}"
    )
    _evento_async(cliente, token, "campana_fin", detalle_fin)
    return resumen

# -*- coding: utf-8 -*-
"""Google Sheets/Drive para entregar al cliente un reporte con link.

Crea una hoja de calculo con las pestanas **Resumen** y **Detalle** con las
acciones EXITOSAS de un lote y la comparte para que el cliente pueda abrir el
link sin descargar Excel. El contenido es de cara al cliente: no incluye
proveedor, costos, saldo, API, IDs de servicio ni "pedidos" (se habla de
"acciones"). La autenticacion usa una **cuenta de servicio** de Google.

Credenciales (en orden de prioridad):
    1. ``GOOGLE_SERVICE_ACCOUNT_B64``  JSON en base64 (recomendado Railway).
    2. ``GOOGLE_SERVICE_ACCOUNT_JSON`` JSON crudo.
    3. ``GOOGLE_SERVICE_ACCOUNT_FILE`` ruta al JSON.
    4. ``data/google_service_account.json`` (archivo local, via resolver_ruta).
    5. ``GOOGLE_APPLICATION_CREDENTIALS`` ruta estandar (ultimo fallback).

La private key JAMAS se imprime, registra ni devuelve en ``estado()``. Todas
las funciones publicas son defensivas: ante cualquier error devuelven un dict
con ``ok=False``/``disponible=False`` y un mensaje en espanol; nunca lanzan.

Contrato publico (lo consume el dashboard de JAP):
    estado() -> {"disponible","motivo","fuente","email"}
    disponible() -> bool
    guardar_credenciales_json(contenido) -> {"ok","email","error"}
    crear_sheet_reporte(pedidos, cliente="", campana="", fecha="", folio="",
                        notas="", compartir_publico=True, email="") ->
        {"ok","spreadsheet_id","url","titulo","compartido","error"}

Helpers puros (sin red, testeables):
    construir_datos_sheet(pedidos, ...) -> dict con las filas de ambas hojas.
    resumen_pedidos(pedidos) -> dict con los totales de acciones exitosas.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.config import resolver_ruta, settings

__all__ = [
    "estado",
    "disponible",
    "guardar_credenciales_json",
    "crear_sheet_reporte",
    "construir_datos_sheet",
    "resumen_pedidos",
    "TIPOS",
    "TIPOS_POR_ENLACE",
    "ETIQUETAS_TIPO",
]

TIPOS = ("likes", "retweets", "comments", "views", "followers")
TIPOS_POR_ENLACE = ("likes", "retweets", "comments", "views")

ETIQUETAS_TIPO = {
    "likes": "Likes",
    "retweets": "Retweets",
    "comments": "Comentarios",
    "views": "Vistas",
    "followers": "Seguidores",
}

# Nombres exactos de las pestanas del documento.
HOJA_RESUMEN = "Resumen"
HOJA_DETALLE = "Detalle"

# Archivo local que escribe `guardar_credenciales_json` (gitignored).
ARCHIVO_LOCAL = "data/google_service_account.json"

# Scopes: crear/escribir hojas y compartir el archivo en Drive.
_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

# Alias de tipos que aceptan los pedidos (historial o captura manual).
_ALIAS_TIPO = {
    "like": "likes",
    "likes": "likes",
    "me gusta": "likes",
    "me_gusta": "likes",
    "retweet": "retweets",
    "retweets": "retweets",
    "rt": "retweets",
    "repost": "retweets",
    "reposts": "retweets",
    "retuiteo": "retweets",
    "retuiteos": "retweets",
    "comment": "comments",
    "comments": "comments",
    "comentario": "comments",
    "comentarios": "comments",
    "view": "views",
    "views": "views",
    "vista": "views",
    "vistas": "views",
    "visualizacion": "views",
    "visualizaciones": "views",
    "reproduccion": "views",
    "reproducciones": "views",
    "follower": "followers",
    "followers": "followers",
    "seguidor": "followers",
    "seguidores": "followers",
}

_ETIQUETAS_FUENTE = {
    "env_b64": "GOOGLE_SERVICE_ACCOUNT_B64",
    "env_json": "GOOGLE_SERVICE_ACCOUNT_JSON",
    "env_file": "GOOGLE_SERVICE_ACCOUNT_FILE",
    "archivo": ARCHIVO_LOCAL,
    "application_credentials": "GOOGLE_APPLICATION_CREDENTIALS",
}

_VALORES_ERROR = ("error", "fallido", "fallo", "failed", "cancelado", "cancel")


# --------------------------------------------------------------------------- #
# Utilidades defensivas
# --------------------------------------------------------------------------- #
def _sin_acentos(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def _texto(valor: Any, limite: int = 500) -> str:
    """str limpio y recortado; nunca lanza."""
    if valor is None:
        return ""
    try:
        texto = str(valor).strip()
    except Exception:
        return ""
    if limite and len(texto) > limite:
        return texto[:limite]
    return texto


def _entero(valor: Any) -> int:
    """Entero no negativo; acepta '50', 50.0, None o basura."""
    if isinstance(valor, bool):
        return int(valor)
    try:
        if valor is None or valor == "":
            return 0
        numero = int(float(str(valor).strip()))
    except Exception:
        return 0
    return max(0, numero)


def _booleano(valor: Any, default: bool = False) -> bool:
    """Normaliza banderas de formularios/JSON/entorno."""
    if isinstance(valor, bool):
        return valor
    if valor is None:
        return default
    if isinstance(valor, (int, float)):
        return bool(valor)
    texto = _sin_acentos(_texto(valor, 40)).lower()
    if texto in ("1", "true", "yes", "y", "si", "on", "verdadero"):
        return True
    if texto in ("0", "false", "no", "n", "off", "", "falso"):
        return False
    return default


def _es_exito(pedido: Dict[str, Any]) -> bool:
    """Lee ``exito`` (BD) o ``ok`` (panel); estado como respaldo."""
    if not isinstance(pedido, dict):
        return False
    if "exito" in pedido:
        return _booleano(pedido.get("exito"), default=True)
    if "ok" in pedido:
        return _booleano(pedido.get("ok"), default=True)
    estado = _sin_acentos(_texto(pedido.get("estado"), 40)).lower()
    if estado:
        return not any(clave in estado for clave in _VALORES_ERROR)
    return True


def _normalizar_tipo(valor: Any) -> str:
    """Mapea alias ES/EN a los 5 tipos canonicos; desconocidos se conservan."""
    texto = _sin_acentos(_texto(valor, 60)).lower().replace("-", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    if texto in _ALIAS_TIPO:
        return _ALIAS_TIPO[texto]
    return texto.replace(" ", "_")[:30]


def _celda(valor: Any, limite: int = 4000) -> str:
    """Texto seguro para una celda: evita que se interprete como formula."""
    texto = _texto(valor, limite)
    if texto[:1] in ("=", "+", "-", "@"):
        return "'" + texto
    return texto


def _fecha_legible(valor: Any) -> str:
    """dd/mm/aaaa a partir de ISO/datetime; hoy si falta o es invalido."""
    if isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y")
    texto = _texto(valor, 60)
    if not texto:
        return datetime.now().strftime("%d/%m/%Y")
    try:
        return datetime.fromisoformat(
            texto.replace("Z", "+00:00")
        ).strftime("%d/%m/%Y")
    except Exception:
        pass
    try:
        return datetime.strptime(texto[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        pass
    return texto[:40]


def _mensaje_error(error: Any, limite: int = 400) -> str:
    """Mensaje corto, en espanol y sin material sensible."""
    if isinstance(error, FileNotFoundError):
        return "No se encontro el archivo de credenciales de Google."
    if isinstance(error, PermissionError):
        return "No hay permisos para leer/escribir las credenciales de Google."
    if isinstance(error, json.JSONDecodeError):
        return "El JSON de credenciales no es valido."
    texto = _texto(error, 1200)
    # Solo se oculta material de clave real; los mensajes de validacion (p.ej.
    # "falta el campo private_key") deben llegar intactos al usuario.
    if "BEGIN PRIVATE KEY" in texto or "PRIVATE KEY-----" in texto:
        return "La private key de la cuenta de servicio no es valida."
    texto = re.sub(r"(://[^:/@\s]+):([^@\s]+)@", r"\1:***@", texto)
    return texto[:limite] or "error inesperado"


def _mensaje_google(error: Any) -> str:
    """Traduce errores tipicos de Google a un mensaje util en espanol."""
    detalle = _texto(error, 1200)
    bajo = _sin_acentos(detalle).lower()
    if "PRIVATE KEY" in detalle or "private_key" in bajo:
        return "La private key de la cuenta de servicio no es valida."
    if (
        "has not been used in project" in bajo
        or "accessnotconfigured" in bajo
        or "service_disabled" in bajo
        or "api is not enabled" in bajo
        or "is disabled" in bajo
    ):
        return (
            "La API de Google Sheets o Google Drive no esta habilitada en el "
            "proyecto de la cuenta de servicio."
        )
    if (
        "invalid_grant" in bajo
        or "invalid credentials" in bajo
        or "invalid_client" in bajo
        or "unauthorized" in bajo
        or "401" in bajo
    ):
        return (
            "Las credenciales de la cuenta de servicio no son validas o fueron "
            "revocadas."
        )
    if (
        "permission" in bajo
        or "forbidden" in bajo
        or "insufficient" in bajo
        or "403" in bajo
    ):
        return (
            "La cuenta de servicio no tiene permiso para crear o compartir el "
            "archivo en Google Drive."
        )
    if "quota" in bajo or "rate limit" in bajo or "429" in bajo:
        return (
            "Google limito temporalmente las solicitudes (cuota). Intenta de "
            "nuevo en unos minutos."
        )
    if "invalid key" in bajo or "decoding" in bajo or "signature" in bajo:
        return "La private key de la cuenta de servicio es invalida."
    return f"No se pudo completar la operacion con Google: {detalle[:300]}"


# --------------------------------------------------------------------------- #
# Resumen (puro, sin red)
# --------------------------------------------------------------------------- #
def resumen_pedidos(pedidos: Any) -> Dict[str, Any]:
    """Agrega SOLO las acciones exitosas de una lista de pedidos.

    Devuelve los totales por tipo, el total de engagement (likes + retweets +
    comentarios + vistas), los seguidores (aparte), las publicaciones unicas y
    el detalle ``por_link`` que usa la pestana Detalle.
    """
    base: Dict[str, Any] = {
        "pedidos_totales": 0,
        "pedidos_exitosos": 0,
        "pedidos_fallidos": 0,
        "por_tipo": {tipo: 0 for tipo in TIPOS},
        "acciones_total": 0,
        "seguidores": 0,
        "total_con_seguidores": 0,
        "publicaciones": 0,
        "links": [],
        "por_link": {},
        "perfil": "",
        "tipos_con_acciones": [],
        "hay_exitos": False,
    }
    try:
        lista = list(pedidos) if isinstance(pedidos, (list, tuple)) else []
    except Exception:
        lista = []

    por_link: Dict[str, Dict[str, int]] = {}
    links: List[str] = []
    perfil = ""
    for pedido in lista:
        if not isinstance(pedido, dict):
            continue
        base["pedidos_totales"] += 1
        if not _es_exito(pedido):
            base["pedidos_fallidos"] += 1
            continue
        base["pedidos_exitosos"] += 1
        tipo = _normalizar_tipo(pedido.get("tipo"))
        cantidad = _entero(pedido.get("cantidad"))
        if tipo not in TIPOS or cantidad <= 0:
            continue
        base["por_tipo"][tipo] += cantidad
        if tipo == "followers":
            if not perfil:
                perfil = _texto(pedido.get("link"), 4000)
            continue
        base["acciones_total"] += cantidad
        enlace = _texto(pedido.get("link"), 4000)
        if not enlace:
            # Sin enlace no se puede atribuir la accion a una publicacion.
            continue
        fila = por_link.get(enlace)
        if fila is None:
            fila = {campo: 0 for campo in TIPOS_POR_ENLACE}
            por_link[enlace] = fila
            links.append(enlace)
        fila[tipo] += cantidad

    base["seguidores"] = base["por_tipo"]["followers"]
    base["total_con_seguidores"] = base["acciones_total"] + base["seguidores"]
    base["publicaciones"] = len(links)
    base["links"] = links
    base["por_link"] = por_link
    base["perfil"] = perfil
    base["tipos_con_acciones"] = [
        tipo for tipo in TIPOS if base["por_tipo"].get(tipo, 0) > 0
    ]
    base["hay_exitos"] = base["pedidos_exitosos"] > 0 and (
        base["total_con_seguidores"] > 0
    )
    return base


def _titulo_documento(cliente: str, campana: str, folio: str) -> str:
    """Titulo del documento (el que vera el cliente en Drive)."""
    partes = ["Reporte de acciones realizadas"]
    vistos = {partes[0].lower()}
    for valor in (cliente, campana, folio):
        texto = _texto(valor, 80)
        if texto and texto.lower() not in vistos:
            partes.append(texto)
            vistos.add(texto.lower())
    return _texto(" · ".join(partes), 200)


def construir_datos_sheet(
    pedidos: Any,
    cliente: str = "",
    campana: str = "",
    fecha: str = "",
    folio: str = "",
    notas: str = "",
) -> Dict[str, Any]:
    """Construye las filas de las hojas Resumen y Detalle (sin llamar a Google).

    Solo incluye acciones exitosas. Los seguidores aparecen UNA vez y aparte
    (van al perfil, no a una publicacion). Las columnas del detalle son
    dinamicas: solo se muestran los tipos con cantidad > 0.
    """
    resumen = resumen_pedidos(pedidos)
    cliente_txt = _texto(cliente, 120)
    campana_txt = _texto(campana, 160)
    folio_txt = _texto(folio, 40)
    notas_txt = _texto(notas, 4000)
    fecha_txt = _fecha_legible(fecha)

    # ----------------------------- Resumen ----------------------------- #
    resumen_filas: List[List[Any]] = [["Reporte de acciones realizadas"], [""]]
    if cliente_txt:
        resumen_filas.append(["Cliente", _celda(cliente_txt, 120)])
    if campana_txt:
        resumen_filas.append(["Campaña", _celda(campana_txt, 160)])
    resumen_filas.append(["Fecha", _celda(fecha_txt, 40)])
    if folio_txt:
        resumen_filas.append(["Folio", _celda(folio_txt, 40)])
    resumen_filas.append([""])

    seccion_fila = len(resumen_filas)
    resumen_filas.append(["Resumen de acciones"])
    tabla_fila = len(resumen_filas)
    resumen_filas.append(["Acción", "Cantidad"])

    tipos = [tipo for tipo in TIPOS if resumen["por_tipo"].get(tipo, 0) > 0]
    for tipo in tipos:
        resumen_filas.append(
            [ETIQUETAS_TIPO[tipo], int(resumen["por_tipo"][tipo])]
        )

    total_fila: Optional[int] = None
    numeros_resumen: Optional[Tuple[int, int]] = None
    if tipos:
        total_fila = len(resumen_filas)
        resumen_filas.append(
            ["Total de acciones", int(resumen["acciones_total"])]
        )
        resumen_filas.append(["Publicaciones", int(resumen["publicaciones"])])
        numeros_resumen = (tabla_fila + 1, len(resumen_filas))
    else:
        resumen_filas.append(["Sin acciones registradas", ""])

    if notas_txt:
        resumen_filas.append([""])
        resumen_filas.append(["Notas"])
        resumen_filas.append([_celda(notas_txt, 4000)])

    # ----------------------------- Detalle ----------------------------- #
    totales_detalle = {
        tipo: sum(
            int((fila or {}).get(tipo) or 0)
            for fila in resumen["por_link"].values()
        )
        for tipo in TIPOS_POR_ENLACE
    }
    columnas = [
        tipo for tipo in TIPOS_POR_ENLACE if totales_detalle[tipo] > 0
    ]
    detalle_filas: List[List[Any]] = []
    numeros_detalle: Optional[Tuple[int, int]] = None
    if columnas and resumen["links"]:
        detalle_filas.append(
            ["Publicación"] + [ETIQUETAS_TIPO[tipo] for tipo in columnas]
        )
        for enlace in resumen["links"]:
            fila = resumen["por_link"].get(enlace) or {}
            detalle_filas.append(
                [_celda(enlace, 4000)]
                + [int(fila.get(tipo) or 0) for tipo in columnas]
            )
        numeros_detalle = (1, len(detalle_filas))
    else:
        detalle_filas.append(["Publicación"])
        detalle_filas.append(["Sin publicaciones con acciones"])

    seguidores_fila: Optional[int] = None
    if resumen["seguidores"] > 0:
        detalle_filas.append([""])
        seguidores_fila = len(detalle_filas)
        fila_seguidores: List[Any] = [
            "Seguidores",
            int(resumen["seguidores"]),
        ]
        if resumen["perfil"]:
            fila_seguidores.append(_celda(resumen["perfil"], 4000))
        detalle_filas.append(fila_seguidores)

    return {
        "titulo": "Reporte de acciones realizadas",
        "titulo_documento": _titulo_documento(
            cliente_txt, campana_txt, folio_txt
        ),
        "nombre_hoja_resumen": HOJA_RESUMEN,
        "nombre_hoja_detalle": HOJA_DETALLE,
        "resumen": {"filas": resumen_filas},
        "detalle": {"filas": detalle_filas},
        "columnas_detalle": columnas,
        "por_tipo": dict(resumen["por_tipo"]),
        "acciones_total": resumen["acciones_total"],
        "seguidores": resumen["seguidores"],
        "publicaciones": resumen["publicaciones"],
        "hay_exitos": resumen["hay_exitos"],
        "metadata": {
            "cliente": cliente_txt,
            "campana": campana_txt,
            "fecha": fecha_txt,
            "folio": folio_txt,
            "notas": notas_txt,
        },
        "formato": {
            "resumen": {
                "titulo_fila": 0,
                "seccion_fila": seccion_fila,
                "tabla_fila": tabla_fila,
                "total_fila": total_fila,
                "numeros": list(numeros_resumen) if numeros_resumen else None,
            },
            "detalle": {
                "tabla_fila": 0,
                "numeros": list(numeros_detalle) if numeros_detalle else None,
                "columnas": len(columnas),
                "seguidores_fila": seguidores_fila,
            },
        },
    }


# --------------------------------------------------------------------------- #
# Credenciales
# --------------------------------------------------------------------------- #
def _valor_config(nombre: str) -> str:
    """Lee una variable del entorno; si no esta, del .env cargado por settings."""
    valor = os.environ.get(nombre)
    if valor is not None and str(valor).strip():
        return str(valor).strip()
    try:
        valor = getattr(settings, nombre.lower(), "")
    except Exception:
        return ""
    return str(valor or "").strip()


def _decodificar_b64(texto: str) -> str:
    """Decodifica un JSON en base64 (tolera espacios/saltos)."""
    limpio = re.sub(r"\s+", "", texto)
    try:
        datos = base64.b64decode(limpio.encode("ascii"), validate=False)
        return datos.decode("utf-8-sig")
    except Exception:
        raise ValueError("El base64 de las credenciales no es valido.")


def _validar_credenciales(datos: Any) -> Dict[str, Any]:
    """Valida client_email/private_key/token_uri de una cuenta de servicio."""
    if not isinstance(datos, dict):
        raise ValueError("El JSON de credenciales debe ser un objeto.")
    tipo = _texto(datos.get("type"), 60).lower()
    if tipo and tipo != "service_account":
        raise ValueError(
            "El JSON no corresponde a una cuenta de servicio "
            "(type distinto de service_account)."
        )
    email = _texto(datos.get("client_email"), 320)
    clave = datos.get("private_key")
    clave = clave if isinstance(clave, str) else ""
    token_uri = _texto(datos.get("token_uri"), 500)
    if not email or "@" not in email:
        raise ValueError("Al JSON le falta el campo client_email.")
    if "PRIVATE KEY" not in clave:
        raise ValueError("Al JSON le falta el campo private_key.")
    if not token_uri or not token_uri.lower().startswith("https://"):
        raise ValueError("Al JSON le falta el campo token_uri.")
    limpio = dict(datos)
    limpio["client_email"] = email
    limpio["private_key"] = clave
    limpio["token_uri"] = token_uri
    return limpio


def _parsear_credenciales(contenido: Any) -> Dict[str, Any]:
    """Acepta dict, JSON crudo o base64 de JSON; devuelve el dict validado."""
    if isinstance(contenido, dict):
        datos: Any = contenido
    else:
        if isinstance(contenido, (bytes, bytearray)):
            try:
                texto = bytes(contenido).decode("utf-8-sig")
            except Exception:
                raise ValueError(
                    "El contenido de las credenciales no es UTF-8 valido."
                )
        else:
            texto = str(contenido or "").strip()
        if not texto:
            raise ValueError("El contenido de las credenciales esta vacio.")
        datos = None
        try:
            datos = json.loads(texto)
        except Exception:
            datos = None
        if datos is None:
            try:
                datos = json.loads(_decodificar_b64(texto))
            except Exception:
                raise ValueError(
                    "El contenido no es un JSON valido de cuenta de servicio."
                )
    if not isinstance(datos, dict):
        raise ValueError("El JSON de credenciales debe ser un objeto.")
    return _validar_credenciales(datos)


def _leer_archivo_credenciales(ruta: str) -> Dict[str, Any]:
    """Lee y valida un JSON de service account desde disco."""
    ruta = os.path.expanduser(_texto(ruta, 2000))
    if not ruta:
        raise ValueError("La ruta del archivo de credenciales esta vacia.")
    ruta_abs = resolver_ruta(ruta)
    if not os.path.isfile(ruta_abs):
        raise FileNotFoundError(
            f"No existe el archivo de credenciales: {ruta}."
        )
    try:
        if os.path.getsize(ruta_abs) > 2_000_000:
            raise ValueError("El archivo de credenciales es demasiado grande.")
        with open(ruta_abs, "r", encoding="utf-8-sig") as handle:
            contenido = handle.read()
    except OSError as error:
        raise ValueError(f"No se pudo leer el archivo de credenciales: {error}")
    return _parsear_credenciales(contenido)


def _cargar_credenciales() -> Tuple[
    Optional[Dict[str, Any]], str, str, str
]:
    """Devuelve ``(datos, fuente, email, motivo)``; datos None si no hay."""
    errores: List[Tuple[str, str]] = []

    b64 = _valor_config("GOOGLE_SERVICE_ACCOUNT_B64")
    if b64:
        try:
            datos = _parsear_credenciales(_decodificar_b64(b64))
            return datos, "env_b64", datos.get("client_email", ""), ""
        except Exception as error:
            errores.append(("env_b64", _mensaje_error(error)))

    crudo = _valor_config("GOOGLE_SERVICE_ACCOUNT_JSON")
    if crudo:
        try:
            datos = _parsear_credenciales(crudo)
            return datos, "env_json", datos.get("client_email", ""), ""
        except Exception as error:
            errores.append(("env_json", _mensaje_error(error)))

    ruta_env = _valor_config("GOOGLE_SERVICE_ACCOUNT_FILE")
    if ruta_env:
        try:
            datos = _leer_archivo_credenciales(ruta_env)
            return datos, "env_file", datos.get("client_email", ""), ""
        except Exception as error:
            errores.append(("env_file", _mensaje_error(error)))

    try:
        ruta_local = resolver_ruta(ARCHIVO_LOCAL)
    except Exception:
        ruta_local = ""
    if ruta_local and os.path.isfile(ruta_local):
        try:
            datos = _leer_archivo_credenciales(ruta_local)
            return datos, "archivo", datos.get("client_email", ""), ""
        except Exception as error:
            errores.append(("archivo", _mensaje_error(error)))

    ruta_estandar = _texto(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), 2000
    )
    if ruta_estandar:
        try:
            datos = _leer_archivo_credenciales(ruta_estandar)
            return (
                datos,
                "application_credentials",
                datos.get("client_email", ""),
                "",
            )
        except Exception as error:
            errores.append(("application_credentials", _mensaje_error(error)))

    if errores:
        fuente, motivo = errores[0]
        etiqueta = _ETIQUETAS_FUENTE.get(fuente, fuente)
        return (
            None,
            fuente,
            "",
            f"La credencial {etiqueta} no es valida: {motivo}",
        )
    return (
        None,
        "",
        "",
        "Sin credenciales de Google. Pega el JSON de la cuenta de servicio o "
        "define GOOGLE_SERVICE_ACCOUNT_B64.",
    )


def estado() -> Dict[str, Any]:
    """Estado de la conexion (nunca lanza ni expone la private key)."""
    base: Dict[str, Any] = {
        "disponible": False,
        "motivo": "",
        "fuente": "",
        "email": "",
    }
    try:
        datos, fuente, email, motivo = _cargar_credenciales()
        if datos:
            return {
                "disponible": True,
                "motivo": f"Credenciales de Google listas para {email}.",
                "fuente": fuente,
                "email": email,
            }
        return {
            **base,
            "motivo": motivo or "Sin credenciales de Google configuradas.",
            "fuente": fuente,
        }
    except Exception as error:
        return {
            **base,
            "motivo": "No se pudo leer la configuracion de Google: "
            + _mensaje_error(error),
        }


def disponible() -> bool:
    """True solo si hay credenciales validas de cuenta de servicio."""
    try:
        return bool(estado().get("disponible"))
    except Exception:
        return False


def guardar_credenciales_json(contenido: Any) -> Dict[str, Any]:
    """Valida y guarda el JSON de la cuenta de servicio en el archivo local.

    Escribe de forma atomica (temp + ``os.replace``), crea la carpeta y en
    POSIX deja permisos 0600. Nunca imprime el JSON ni la private key.
    """
    resultado: Dict[str, Any] = {"ok": False, "email": "", "error": ""}
    temporal = ""
    try:
        datos = _parsear_credenciales(contenido)
        ruta = resolver_ruta(ARCHIVO_LOCAL)
        carpeta = os.path.dirname(ruta) or "."
        os.makedirs(carpeta, exist_ok=True)
        descriptor, temporal = tempfile.mkstemp(
            prefix=".google_sa_", suffix=".tmp", dir=carpeta
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(datos, handle, ensure_ascii=False, indent=2)
        if os.name != "nt":
            os.chmod(temporal, 0o600)
        os.replace(temporal, ruta)
        temporal = ""
        if os.name != "nt":
            os.chmod(ruta, 0o600)
        resultado["ok"] = True
        resultado["email"] = _texto(datos.get("client_email"), 320)
    except Exception as error:
        resultado["error"] = _mensaje_error(error)
    finally:
        if temporal:
            try:
                os.unlink(temporal)
            except OSError:
                pass
    return resultado


# --------------------------------------------------------------------------- #
# Google API
# --------------------------------------------------------------------------- #
def _crear_credenciales_google(datos: Dict[str, Any]) -> Any:
    """Crea ``service_account.Credentials`` con los scopes necesarios."""
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_info(
        datos, scopes=list(_SCOPES)
    )


def _construir_cliente(
    servicio: str, version: str, credenciales: Any
) -> Any:
    """Construye un cliente de Google (sheets/drive) sin cache en disco."""
    from googleapiclient.discovery import build

    return build(
        servicio,
        version,
        credentials=credenciales,
        cache_discovery=False,
    )


def _ids_hojas(creado: Dict[str, Any]) -> Dict[str, int]:
    """sheetId de Resumen/Detalle a partir de la respuesta de create."""
    ids = {HOJA_RESUMEN: 0, HOJA_DETALLE: 1}
    hojas = (creado or {}).get("sheets") or []
    for hoja in hojas:
        propiedades = (hoja or {}).get("properties") or {}
        titulo = _texto(propiedades.get("title"), 40)
        identificador = propiedades.get("sheetId")
        if titulo in ids and identificador is not None:
            try:
                ids[titulo] = int(identificador)
            except (TypeError, ValueError):
                pass
    return ids


def _crear_documento(sheets: Any, titulo: str) -> Dict[str, Any]:
    """Crea el Google Sheet con las dos pestanas y devuelve la respuesta."""
    cuerpo = {
        "properties": {"title": titulo},
        "sheets": [
            {"properties": {"title": HOJA_RESUMEN, "index": 0}},
            {"properties": {"title": HOJA_DETALLE, "index": 1}},
        ],
    }
    creado = (
        sheets.spreadsheets()
        .create(
            body=cuerpo,
            fields="spreadsheetId,spreadsheetUrl,sheets(properties(sheetId,title))",
        )
        .execute()
    )
    return creado if isinstance(creado, dict) else {}


def _escribir_valores(
    sheets: Any, spreadsheet_id: str, datos: Dict[str, Any]
) -> None:
    """Escribe las filas de Resumen y Detalle en UNA sola llamada."""
    payload = {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {
                "range": f"'{HOJA_RESUMEN}'!A1",
                "majorDimension": "ROWS",
                "values": datos["resumen"]["filas"],
            },
            {
                "range": f"'{HOJA_DETALLE}'!A1",
                "majorDimension": "ROWS",
                "values": datos["detalle"]["filas"],
            },
        ],
    }
    (
        sheets.spreadsheets()
        .values()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=payload)
        .execute()
    )


def _rango(
    sheet_id: int,
    fila_inicio: int,
    fila_fin: int,
    columna_inicio: int,
    columna_fin: int,
) -> Dict[str, int]:
    """GridRange 0-based con fin exclusivo."""
    return {
        "sheetId": int(sheet_id),
        "startRowIndex": int(fila_inicio),
        "endRowIndex": int(fila_fin),
        "startColumnIndex": int(columna_inicio),
        "endColumnIndex": int(columna_fin),
    }


def _estilo_encabezado() -> Dict[str, Any]:
    """Fondo y texto en negritas para los encabezados de tabla."""
    return {
        "userEnteredFormat": {
            "backgroundColor": {
                "red": 0.89,
                "green": 0.92,
                "blue": 0.97,
            },
            "textFormat": {
                "bold": True,
                "foregroundColor": {
                    "red": 0.06,
                    "green": 0.18,
                    "blue": 0.36,
                },
            },
        }
    }


def _solicitudes_formato(
    datos: Dict[str, Any], ids: Dict[str, int]
) -> List[Dict[str, Any]]:
    """Requests de ``spreadsheets.batchUpdate`` para dar formato a la hoja."""
    formato = datos["formato"]
    resumen = formato["resumen"]
    detalle = formato["detalle"]
    id_resumen = ids.get(HOJA_RESUMEN, 0)
    id_detalle = ids.get(HOJA_DETALLE, 1)
    azul = {"red": 0.06, "green": 0.18, "blue": 0.36}
    ancho_detalle = max(1, 1 + int(detalle.get("columnas") or 0))
    solicitudes: List[Dict[str, Any]] = []

    # Titulo grande y en negritas.
    solicitudes.append(
        {
            "repeatCell": {
                "range": _rango(id_resumen, 0, 1, 0, 6),
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "bold": True,
                            "fontSize": 16,
                            "foregroundColor": azul,
                        }
                    }
                },
                "fields": "userEnteredFormat.textFormat",
            }
        }
    )
    # Encabezado de la seccion "Resumen de acciones".
    solicitudes.append(
        {
            "repeatCell": {
                "range": _rango(
                    id_resumen,
                    resumen["seccion_fila"],
                    resumen["seccion_fila"] + 1,
                    0,
                    2,
                ),
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "fontSize": 12}
                    }
                },
                "fields": "userEnteredFormat.textFormat",
            }
        }
    )
    # Encabezados de tabla con fondo.
    solicitudes.append(
        {
            "repeatCell": {
                "range": _rango(
                    id_resumen,
                    resumen["tabla_fila"],
                    resumen["tabla_fila"] + 1,
                    0,
                    2,
                ),
                "cell": _estilo_encabezado(),
                "fields": "userEnteredFormat",
            }
        }
    )
    solicitudes.append(
        {
            "repeatCell": {
                "range": _rango(
                    id_detalle,
                    detalle["tabla_fila"],
                    detalle["tabla_fila"] + 1,
                    0,
                    ancho_detalle,
                ),
                "cell": _estilo_encabezado(),
                "fields": "userEnteredFormat",
            }
        }
    )
    # Fila de totales en negritas.
    if resumen.get("total_fila") is not None:
        solicitudes.append(
            {
                "repeatCell": {
                    "range": _rango(
                        id_resumen,
                        resumen["total_fila"],
                        resumen["total_fila"] + 1,
                        0,
                        2,
                    ),
                    "cell": {
                        "userEnteredFormat": {"textFormat": {"bold": True}}
                    },
                    "fields": "userEnteredFormat.textFormat",
                }
            }
        )
    # Filas congeladas (encabezado visible al desplazarse).
    solicitudes.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": id_resumen,
                    "gridProperties": {
                        "frozenRowCount": int(resumen["tabla_fila"]) + 1
                    },
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )
    solicitudes.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": id_detalle,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }
    )
    # Autoajuste de columnas.
    solicitudes.append(
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": id_resumen,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 2,
                }
            }
        }
    )
    solicitudes.append(
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": id_detalle,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": ancho_detalle,
                }
            }
        }
    )
    # Cantidades como enteros (sin decimales) alineadas a la derecha.
    formato_numero = {
        "numberFormat": {"type": "NUMBER", "pattern": "0"},
        "horizontalAlignment": "RIGHT",
    }
    if resumen.get("numeros"):
        inicio, fin = resumen["numeros"]
        solicitudes.append(
            {
                "repeatCell": {
                    "range": _rango(
                        id_resumen, int(inicio), int(fin), 1, 2
                    ),
                    "cell": {
                        "userEnteredFormat": dict(formato_numero)
                    },
                    "fields": (
                        "userEnteredFormat.numberFormat,"
                        "userEnteredFormat.horizontalAlignment"
                    ),
                }
            }
        )
    if detalle.get("numeros") and detalle.get("columnas"):
        inicio, fin = detalle["numeros"]
        solicitudes.append(
            {
                "repeatCell": {
                    "range": _rango(
                        id_detalle, int(inicio), int(fin), 1, ancho_detalle
                    ),
                    "cell": {
                        "userEnteredFormat": dict(formato_numero)
                    },
                    "fields": (
                        "userEnteredFormat.numberFormat,"
                        "userEnteredFormat.horizontalAlignment"
                    ),
                }
            }
        )
    return solicitudes


def _aplicar_formato(
    sheets: Any,
    spreadsheet_id: str,
    datos: Dict[str, Any],
    ids: Dict[str, int],
) -> None:
    """Aplica todo el formato en UNA llamada a ``spreadsheets.batchUpdate``."""
    solicitudes = _solicitudes_formato(datos, ids)
    if not solicitudes:
        return
    (
        sheets.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": solicitudes}
        )
        .execute()
    )


def _compartir(
    drive: Any, spreadsheet_id: str, publico: bool, email: str
) -> Dict[str, Any]:
    """Comparte el documento (cualquiera con el link y/o un correo lector)."""
    resultado: Dict[str, Any] = {
        "ok": False,
        "publico": False,
        "email": "",
        "permisos": [],
        "aviso": "",
    }
    if publico:
        (
            drive.permissions()
            .create(
                fileId=spreadsheet_id,
                body={"type": "anyone", "role": "reader"},
                fields="id",
            )
            .execute()
        )
        resultado["publico"] = True
        resultado["permisos"].append({"tipo": "anyone", "rol": "reader"})
    if email:
        try:
            (
                drive.permissions()
                .create(
                    fileId=spreadsheet_id,
                    body={
                        "type": "user",
                        "role": "reader",
                        "emailAddress": email,
                    },
                    fields="id",
                    sendNotificationEmail=False,
                )
                .execute()
            )
            resultado["email"] = email
            resultado["permisos"].append(
                {"tipo": "user", "rol": "reader", "email": email}
            )
        except Exception as error:
            # Si el link publico ya funciona, no se pierde el reporte por el
            # correo adicional; solo se deja el aviso.
            if not resultado["publico"]:
                raise
            resultado["aviso"] = (
                "El enlace publico quedo listo, pero no se pudo compartir con "
                "el correo indicado: " + _mensaje_error(error)
            )
    resultado["ok"] = bool(resultado["publico"] or resultado["email"])
    return resultado


def _email_valido(email: str) -> bool:
    """Validacion basica (Google hara la comprobacion final)."""
    return bool(
        re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or "")
    )


def crear_sheet_reporte(
    pedidos: Any,
    cliente: str = "",
    campana: str = "",
    fecha: str = "",
    folio: str = "",
    notas: str = "",
    compartir_publico: bool = True,
    email: str = "",
) -> Dict[str, Any]:
    """Crea el Google Sheet del reporte y devuelve el link compartido.

    Solo incluye acciones exitosas. Por defecto comparte como lector para
    cualquiera con el enlace; si ``email`` viene (o esta configurado
    ``GOOGLE_SHEETS_SHARE_EMAIL``) tambien lo comparte con ese correo. Con
    ``compartir_publico=None`` se usa ``GOOGLE_SHEETS_PUBLICO``. Nunca lanza:
    los errores de credenciales/API vuelven en ``error``.
    """
    resultado: Dict[str, Any] = {
        "ok": False,
        "spreadsheet_id": "",
        "url": "",
        "titulo": "",
        "compartido": {
            "ok": False,
            "publico": False,
            "email": "",
            "permisos": [],
            "aviso": "",
        },
        "error": "",
    }
    try:
        if compartir_publico is None:
            publico = _booleano(
                _valor_config("GOOGLE_SHEETS_PUBLICO"), default=True
            )
        else:
            publico = _booleano(compartir_publico, default=True)
        email = _texto(email, 320)
        if not email:
            email = _texto(_valor_config("GOOGLE_SHEETS_SHARE_EMAIL"), 320)
        if email and not _email_valido(email):
            resultado["error"] = (
                "El correo para compartir no parece valido: " + email
            )
            return resultado
        if not publico and not email:
            resultado["error"] = (
                "Indica un correo para compartir o activa el enlace publico "
                "(si no, el cliente no podra abrir el Sheet)."
            )
            return resultado

        datos = construir_datos_sheet(
            pedidos,
            cliente=cliente,
            campana=campana,
            fecha=fecha,
            folio=folio,
            notas=notas,
        )
        resultado["titulo"] = datos["titulo_documento"]
        if not datos["hay_exitos"]:
            resultado["error"] = (
                "No hay acciones exitosas para incluir en el reporte."
            )
            return resultado

        credenciales_info, _fuente, _email_cuenta, motivo = (
            _cargar_credenciales()
        )
        if not credenciales_info:
            resultado["error"] = motivo or (
                "Sin credenciales de Google configuradas."
            )
            return resultado

        credenciales = _crear_credenciales_google(credenciales_info)
        sheets = _construir_cliente("sheets", "v4", credenciales)
        drive = _construir_cliente("drive", "v3", credenciales)

        creado = _crear_documento(sheets, datos["titulo_documento"])
        spreadsheet_id = _texto(creado.get("spreadsheetId"), 200)
        if not spreadsheet_id:
            raise RuntimeError(
                "Google no devolvio el identificador de la hoja de calculo."
            )
        ids = _ids_hojas(creado)

        _escribir_valores(sheets, spreadsheet_id, datos)
        _aplicar_formato(sheets, spreadsheet_id, datos, ids)
        compartido = _compartir(
            drive, spreadsheet_id, publico, email
        )

        resultado.update(
            {
                "ok": True,
                "spreadsheet_id": spreadsheet_id,
                "url": (
                    "https://docs.google.com/spreadsheets/d/"
                    f"{spreadsheet_id}/edit"
                ),
                "compartido": compartido,
                "error": "",
            }
        )
        return resultado
    except Exception as error:
        resultado["error"] = _mensaje_google(error)
        return resultado

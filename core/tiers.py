# -*- coding: utf-8 -*-
"""Clasificacion de cuentas por tier de calidad.

Tres tiers operativos:
    - tier1: "Tier 1 (Líder/Boosted)" — cuentas fuertes/maduras; SI pueden
      publicar posts originales con hashtags.
    - tier2: "Tier 2 (Volumen/Aged)" — cuentas de volumen; tienen PROHIBIDO el
      rol "hashtags" (texto original) y solo deben hacer RT, Cita o Comentario.
    - tier3: "Tier 3 (Métricas / Soporte)" — cuentas de relleno/metricas;
      SOLO pueden hacer RT y like (volumen ciego). Cualquier otro rol
      ("hashtags"/post, "cita", "comentario", ...) esta PROHIBIDO.

Reglas de negocio (las aplican activaciones y el dashboard):
    `rol_permitido_tier(cuenta, rol)` -> False si la cuenta es Tier 2 y el rol
    normalizado es "hashtags" (ojo: "post"/"publicacion"/"mantenimiento"/
    "calentamiento"/"hilo" TAMBIEN normalizan a "hashtags" con
    `core.registro.normalizar_rol_cuota`). Si la cuenta es Tier 3 -> True SOLO
    con rol efectivo "rt"/"like" (ROLES_PERMITIDOS_TIER3) o vacio ("" = sin rol
    no ejecuta nada). Tier 1, tier vacio ("") o un tier no reconocido = sin
    restriccion.

Interfaz congelada: otros modulos (web, activaciones, cuentas, exportacion)
importan TIERS, normalizar_tier(), etiqueta_tier(), tier_de_cuenta(),
es_tier2(), es_tier3(), ROL_PROHIBIDO_TIER2, ROLES_PROHIBIDOS_TIER3,
ROLES_PERMITIDOS_TIER3, rol_permitido_tier() y error_rol_tier().
Sin dependencias externas; la normalizacion de roles se importa de forma
PEREZOSA (dentro de la funcion) para no crear ciclos con core.registro.
"""
from core.secciones import _normalizar_texto

# Codigo canonico -> nombre legible del tier.
TIERS = {
    "tier1": "Tier 1 (Líder/Boosted)",
    "tier2": "Tier 2 (Volumen/Aged)",
    "tier3": "Tier 3 (Métricas / Soporte)",
}

# Rol de activacion PROHIBIDO para las cuentas Tier 2 (texto original).
ROL_PROHIBIDO_TIER2 = "hashtags"

# Tier 3 (Métricas/Soporte): SOLO rt y like (volumen ciego). Cualquier otro rol
# efectivo (hashtags/post/publicacion/mantenimiento/calentamiento/hilo, cita,
# comentario, respuesta, reply, ...) esta prohibido.
ROLES_PROHIBIDOS_TIER3 = ("hashtags", "cita", "comentario")
ROLES_PERMITIDOS_TIER3 = ("rt", "like")

# Sinonimos EXACTOS (ya normalizados: minusculas, sin acentos ni separadores).
_TIER1_EXACTOS = (
    "tier1",
    "1",
    "lider",
    "lideres",
    "boosted",
    "boost",
    "principal",
    "principales",
)
_TIER2_EXACTOS = (
    "tier2",
    "2",
    "volumen",
    "volumenes",
    "aged",
    "masivo",
    "masiva",
    "masivos",
    "masivas",
)
_TIER3_EXACTOS = (
    "tier3",
    "3",
    "granja",
    "granjas",
    "farm",
    "metricas",
    "soporte",
    "bajacalidad",
)


def _claves_tier(codigo: str) -> tuple:
    """Claves normalizadas que identifican un tier.

    Incluye el codigo ("tier1"), el nombre ("Tier 1 (Líder/Boosted)", cuyos
    parentesis se conservan al normalizar) y ambas formas juntas, para que el
    dashboard pueda pasar indistintamente la etiqueta completa."""
    nombre = TIERS[codigo]
    return (
        _normalizar_texto(codigo),
        _normalizar_texto(nombre),
        _normalizar_texto(f"{codigo} {nombre}"),
    )


def normalizar_tier(valor) -> str:
    """Devuelve el codigo canonico del tier ("tier1", "tier2", "tier3" o "").

    Acepta variantes con/sin acentos y mayusculas/minusculas:
        - "tier1", "tier 1", "Tier1", "1", "lider", "líder", "boosted",
          "principal" (-> "tier1").
        - "tier2", "tier 2", "Tier2", "2", "volumen", "aged", "masivo"
          (-> "tier2").
        - "tier3", "tier 3", "Tier3", "3", "granja", "farm", "metricas",
          "métricas", "soporte", "baja calidad" (-> "tier3").
        - las etiquetas completas de TIERS ("Tier 1 (Líder/Boosted)", etc.).
    Cualquier otro valor, vacio o None devuelve "" (sin clasificar). Nunca
    lanza."""
    try:
        clave = _normalizar_texto(valor)
        if not clave:
            return ""
        if clave in _TIER1_EXACTOS or clave in _claves_tier("tier1"):
            return "tier1"
        if clave in _TIER2_EXACTOS or clave in _claves_tier("tier2"):
            return "tier2"
        if clave in _TIER3_EXACTOS or clave in _claves_tier("tier3"):
            return "tier3"
        # Prefijo del codigo: "tier1lider", "tier 1 (lider)" -> tier1.
        if clave.startswith("tier1"):
            return "tier1"
        if clave.startswith("tier2"):
            return "tier2"
        if clave.startswith("tier3"):
            return "tier3"
        return ""
    except Exception:
        return ""


def etiqueta_tier(valor) -> str:
    """Devuelve la etiqueta legible de un tier.

    "tier1" -> "Tier 1 (Líder/Boosted)"; "tier2" -> "Tier 2 (Volumen/Aged)";
    "tier3" -> "Tier 3 (Métricas / Soporte)"; vacio/None/no reconocido -> ""
    (sin tier). Nunca lanza."""
    try:
        clave = normalizar_tier(valor)
        return TIERS.get(clave, "") if clave else ""
    except Exception:
        return ""


def tier_de_cuenta(cuenta) -> str:
    """Tier normalizado de una cuenta ("tier1"/"tier2"/"tier3"/"").

    Tolerante: usa `getattr(cuenta, "tier_calidad", "")` y nunca lanza (un
    objeto sin ese atributo o con un valor raro devuelve "")."""
    try:
        return normalizar_tier(getattr(cuenta, "tier_calidad", ""))
    except Exception:
        return ""


def es_tier2(cuenta) -> bool:
    """True si la cuenta es Tier 2 (Volumen/Aged). Nunca lanza."""
    try:
        return tier_de_cuenta(cuenta) == "tier2"
    except Exception:
        return False


def es_tier3(cuenta) -> bool:
    """True si la cuenta es Tier 3 (Métricas/Soporte). Nunca lanza."""
    try:
        return tier_de_cuenta(cuenta) == "tier3"
    except Exception:
        return False


def _rol_efectivo(rol) -> str:
    """Rol normalizado con `core.registro.normalizar_rol_cuota` (import perezoso).

    Si el import fallara (no deberia), cae a minusculas sin acentos basico para
    no propagar la excepcion."""
    try:
        from core.registro import normalizar_rol_cuota

        return normalizar_rol_cuota(rol)
    except Exception:
        return str(rol or "").strip().lower()


def rol_permitido_tier(cuenta, rol) -> bool:
    """True si la cuenta puede ejecutar ese rol segun su tier.

    False SOLO si:
        - la cuenta es Tier 3 y el rol efectivo NO esta en
          `ROLES_PERMITIDOS_TIER3` ("rt"/"like"). Rol efectivo vacio ("") ->
          True (sin rol no ejecuta nada).
        - la cuenta es Tier 2 y el rol normalizado es "hashtags"
          (`ROL_PROHIBIDO_TIER2`); "post", "publicacion", "mantenimiento",
          "calentamiento" e "hilo" TAMBIEN cuentan como "hashtags" porque asi
          los normaliza `core.registro.normalizar_rol_cuota`.
    Tier 1, tier vacio y cualquier otra combinacion -> True. Nunca lanza."""
    try:
        tier = tier_de_cuenta(cuenta)
        if tier == "tier3":
            efectivo = _rol_efectivo(rol)
            if not efectivo:
                return True
            return efectivo in ROLES_PERMITIDOS_TIER3
        if tier != "tier2":
            return True
        return _rol_efectivo(rol) != ROL_PROHIBIDO_TIER2
    except Exception:
        return True


def error_rol_tier(cuenta, rol) -> str:
    """Mensaje bloqueante si el tier de la cuenta prohibe ese rol.

    Devuelve "" cuando el rol esta permitido; si no, un mensaje claro en
    español listo para mostrar en el dashboard/campaña. Nunca lanza."""
    try:
        if rol_permitido_tier(cuenta, rol):
            return ""
        usuario = getattr(cuenta, "usuario", "") or getattr(cuenta, "handle_actual", "")
        if es_tier3(cuenta):
            return (
                f"La cuenta @{usuario} (Tier 3 - Métricas/Soporte) solo puede ejecutar "
                f"RT y likes (volumen ciego): el rol '{_rol_efectivo(rol)}' está "
                f"PROHIBIDO. Usa Tier 1/Tier 2 para ese rol o asígnale RT."
            )
        return (
            f"La cuenta @{usuario} (Tier 2 - Volumen/Aged) tiene PROHIBIDO el rol "
            f"'{ROL_PROHIBIDO_TIER2}'. Usa Tier 1 para posts originales o "
            f"asígnale RT/Cita/Comentario."
        )
    except Exception:
        return ""

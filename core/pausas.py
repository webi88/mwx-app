"""Pausas de cuentas para activacion masiva (soporte de datos en `core/`).

REGLA DE NEGOCIO
================
`Cuenta.pausada_activacion` marca las cuentas que se entregan a CLIENTES y que
NO deben usarse en campanas masivas, pero que SI deben mantenerse vivas.

* PAUSADA = EXCLUIDA de la activacion masiva:
  - `activaciones/motor.py` (campanas por seccion, por roles, cita masiva,
    curva de aceleracion y cuentas de respaldo).
  - `web/operaciones/activacion_masiva.py`, `rts.py`, `likes.py` y
    `reparto_hora.py`.
  - Tareas de ACTIVACION programadas en el `scheduler`.
* PERMITIDA en mantenimiento (sigue disponible aunque este pausada):
  - Mantenimiento programado de `web/operaciones/posts.py`.
  - Calentamiento continuo (`scheduler/calentamiento.py`).
  - Publicaciones manuales y el bot de clientes.

API PUBLICA (nombres y firmas CONGELADOS; la usan `activaciones`, `web` y
`scheduler`):

    esta_pausada(cuenta) -> bool
    filtrar_para_activacion(cuentas, incluir_pausadas=False) -> list
    pausadas_usuarios(cuentas) -> list[str]

Es un modulo generico y sin dependencias nuevas: NO importa `core.models` (asi
evita importaciones circulares con `core.database`/`core.models`) y lee el
atributo con `getattr` tolerante. Ninguna funcion lanza jamas: ante cualquier
dato raro devuelve el valor conservador (False o lista vacia).
"""
from __future__ import annotations

__all__ = ["esta_pausada", "filtrar_para_activacion", "pausadas_usuarios"]

# Cadenas que cuentan como pausa (se comparan en minusculas y sin espacios).
_VALORES_PAUSA = frozenset(
    {"1", "true", "si", "sí", "yes", "on", "verdadero"}
)


def esta_pausada(cuenta) -> bool:
    """True si la cuenta esta marcada como pausada para activacion masiva.

    Acepta cualquier entrada: bool, numeros (0/1), cadenas ("1", "true",
    "si", ...) y objetos con o sin el atributo `pausada_activacion`
    (None/""/0 -> False). Nunca lanza."""
    try:
        valor = getattr(cuenta, "pausada_activacion", False)
    except Exception:
        return False
    if valor is None:
        return False
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return valor != 0
    if isinstance(valor, (bytes, bytearray)):
        try:
            valor = valor.decode("utf-8", "ignore")
        except Exception:
            return False
    if isinstance(valor, str):
        return valor.strip().lower() in _VALORES_PAUSA
    try:
        return bool(valor)
    except Exception:
        return False


def filtrar_para_activacion(cuentas, incluir_pausadas: bool = False) -> list:
    """Lista NUEVA de cuentas elegibles para activacion masiva.

    Con `incluir_pausadas=False` (default) quita las pausadas; con
    `incluir_pausadas=True` devuelve la lista completa. No muta la lista
    original y nunca lanza (entrada no iterable -> [])."""
    try:
        lista = list(cuentas)
    except Exception:
        return []
    if incluir_pausadas:
        return lista
    return [cuenta for cuenta in lista if not esta_pausada(cuenta)]


def pausadas_usuarios(cuentas) -> list:
    """`.usuario` (texto no vacio) de las cuentas pausadas de la lista.

    Tolerante a objetos sin `.usuario`, con `.usuario = None` o con valores
    que fallen al convertirse: esos se omiten. Nunca lanza."""
    try:
        lista = list(cuentas)
    except Exception:
        return []
    usuarios = []
    for cuenta in lista:
        try:
            if not esta_pausada(cuenta):
                continue
            usuario = getattr(cuenta, "usuario", "")
            if usuario is None:
                continue
            usuario = str(usuario).strip()
            if usuario:
                usuarios.append(usuario)
        except Exception:
            continue
    return usuarios

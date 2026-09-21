# -*- coding: utf-8 -*-
"""Sistema de Cuotas Inteligente por Hora (capa del motor de activaciones).

Cuenta, por par (usuario, rol), las acciones EXITOSAS dentro de una ventana de
minutos para que el motor no supere los limites por hora de X:

    usado = base_bd + exitos_campana + reservas_en_vuelo

- ``base_bd``: acciones exitosas ya registradas en ``RegistroAccion`` al
  preparar la campana (una sola consulta agrupada via
  ``contar_acciones_por_usuario``).
- ``exitos_campana``: acciones confirmadas por ESTA campana; se suman al
  liberar la reserva con exito.
- ``reservas_en_vuelo``: acciones reservadas por workers en paralelo que aun
  no terminan. Se cuentan desde la RESERVA (no al terminar) para que dos
  workers jamas pasen el limite a la vez: ``reservar`` y ``liberar`` son
  atomicos bajo un ``threading.RLock``.

Todas las funciones son tolerantes a fallos: envuelven su cuerpo en
``try/except`` y devuelven el default (``True`` en ``rol_permitido``,
``False`` en ``reservar``, listas/dicts vacios en el resto) sin lanzar jamas.
"""
import threading
import time

from core.config import settings
from core.registro import (
    contar_acciones_por_usuario,
    limite_por_rol,
    normalizar_rol_cuota,
)

# Roles del sistema de cuotas (los 4 que entiende core.registro).
ROLES_CUOTA = ("hashtags", "cita", "rt", "comentario")


class CuotasHorarias:
    """Contador thread-safe de acciones exitosas por (usuario, rol) y ventana.

    Uso tipico desde el motor de activaciones::

        cuotas = CuotasHorarias()          # limites de settings (5/5/7/3)
        cuotas.preparar(usuarios)          # carga la base desde la BD
        if cuotas.reservar(usuario, "rt"): # atomico: reserva un cupo
            try:
                exito = ejecutar_accion()
            finally:
                cuotas.liberar(usuario, "rt", exito)

    ``reservar`` devuelve ``False`` cuando el uso actual
    (``base_bd + exitos_campana + reservas_en_vuelo``) ya alcanzo el limite;
    ``liberar(..., exito=False)`` devuelve el cupo sin consumirlo y
    ``liberar(..., exito=True)`` lo consume (suma a ``exitos_campana``).
    Un limite ``<= 0`` significa ILIMITADO (siempre permite y siempre reserva).
    """

    def __init__(self, limites=None, minutos=None, max_aviso_seg=60):
        """Crea el contador.

        Args:
            limites: mapa opcional ``{rol: int}`` (util en tests); por default
                usa ``limite_por_rol`` de los 4 roles (0 = ilimitado). Las
                claves se normalizan con ``normalizar_rol_cuota``.
            minutos: ventana en minutos; ``None``/invalido/``<= 0`` usa
                ``settings.limite_ventana_min`` (y 60 si tampoco es valido).
            max_aviso_seg: segundos minimos entre dos avisos de cuota agotada
                de la MISMA cuenta (default 60); ``0`` avisa siempre.
        """
        self._lock = threading.RLock()
        self._limites = self._construir_limites(limites)
        self._minutos = self._normalizar_minutos(minutos)
        self._max_aviso_seg = self._normalizar_aviso(max_aviso_seg)
        self._base: dict = {}
        self._exitos: dict = {}
        self._reservas: dict = {}
        self._ultimo_aviso: dict = {}

    # ------------------------------------------------------------------ #
    # Construccion / normalizacion
    # ------------------------------------------------------------------ #
    @staticmethod
    def _construir_limites(limites) -> dict:
        """Mapa {rol_normalizado: limite entero}; nunca lanza."""
        resultado: dict = {}
        if limites is None:
            for rol in ROLES_CUOTA:
                try:
                    resultado[rol] = int(limite_por_rol(rol))
                except Exception:
                    resultado[rol] = 0
            return resultado
        try:
            for rol, valor in dict(limites).items():
                clave = CuotasHorarias._clave_rol(rol)
                if not clave:
                    continue
                try:
                    resultado[clave] = int(valor)
                except (TypeError, ValueError):
                    resultado[clave] = 0
        except Exception:
            return {}
        return resultado

    @staticmethod
    def _clave_rol(rol) -> str:
        """Rol canonico (hashtags/cita/rt/comentario) o "" si viene vacio."""
        try:
            return normalizar_rol_cuota(rol)
        except Exception:
            return ""

    @staticmethod
    def _normalizar_minutos(minutos) -> int:
        """Ventana efectiva: ``minutos`` > 0 o la de settings (default 60)."""
        try:
            valor = int(minutos) if minutos is not None else 0
        except (TypeError, ValueError):
            valor = 0
        if valor > 0:
            return valor
        try:
            por_defecto = int(getattr(settings, "limite_ventana_min", 60))
        except (TypeError, ValueError):
            por_defecto = 60
        return por_defecto if por_defecto > 0 else 60

    @staticmethod
    def _normalizar_aviso(max_aviso_seg) -> float:
        """Segundos de throttle del aviso (>= 0; default 60 si es invalido)."""
        try:
            valor = float(max_aviso_seg)
        except (TypeError, ValueError):
            return 60.0
        if valor != valor:  # NaN
            return 60.0
        return valor if valor >= 0 else 60.0

    @staticmethod
    def _clave(usuario, rol):
        """Par ``(usuario, rol_canonico)`` o ``None`` si falta alguno."""
        texto = str(usuario or "").strip()
        normalizado = CuotasHorarias._clave_rol(rol)
        if not texto or not normalizado:
            return None
        return (texto, normalizado)

    def _usado_locked(self, clave) -> int:
        """``base + exitos + reservas`` de un par; REQUIERE el lock tomado."""
        return (
            int(self._base.get(clave, 0))
            + int(self._exitos.get(clave, 0))
            + int(self._reservas.get(clave, 0))
        )

    def _usado(self, usuario, rol) -> int:
        """Uso actual de un par (thread-safe; 0 si el par es invalido)."""
        try:
            with self._lock:
                clave = self._clave(usuario, rol)
                if clave is None:
                    return 0
                return self._usado_locked(clave)
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # API publica
    # ------------------------------------------------------------------ #
    def preparar(self, usuarios) -> None:
        """Carga la base desde la BD para ``usuarios`` (re-llamable).

        Reemplaza SOLO ``_base`` con ``contar_acciones_por_usuario`` (una
        consulta agrupada); ``exitos_campana`` y ``reservas_en_vuelo`` se
        conservan. Ante cualquier error deja ``_base`` como estaba (nunca
        lanza).
        """
        try:
            if usuarios is None:
                lista = []
            elif isinstance(usuarios, str):
                lista = [usuarios]
            else:
                lista = [u for u in usuarios]
            base = contar_acciones_por_usuario(lista, self._minutos)
            if not isinstance(base, dict):
                base = {}
            normalizada: dict = {}
            for usuario, por_rol in base.items():
                if not isinstance(por_rol, dict):
                    continue
                for rol, conteo in por_rol.items():
                    normalizado = self._clave_rol(rol)
                    if not normalizado:
                        continue
                    try:
                        total = int(conteo or 0)
                    except (TypeError, ValueError):
                        total = 0
                    if total > 0:
                        normalizada[(str(usuario), normalizado)] = total
            with self._lock:
                self._base = normalizada
        except Exception:
            pass

    def rol_permitido(self, usuario, rol) -> bool:
        """True si la cuenta aun tiene cupo para ese rol (limite <= 0 = libre)."""
        try:
            with self._lock:
                clave = self._clave(usuario, rol)
                if clave is None:
                    return True
                limite = int(self._limites.get(clave[1], 0))
                if limite <= 0:
                    return True
                return self._usado_locked(clave) < limite
        except Exception:
            return True

    def viables(self, usuario, candidatos) -> list:
        """Subconjunto de ``candidatos`` con cupo, normalizado y sin repetir.

        Conserva el orden de entrada y deduplica por rol canonico (p. ej.
        ``["post", "hashtags"]`` cuenta una sola vez). Ante cualquier fallo
        devuelve la lista original tal cual.
        """
        try:
            try:
                lista = list(candidatos or [])
            except TypeError:
                lista = [candidatos]
            resultado, vistos = [], set()
            for rol in lista:
                normalizado = self._clave_rol(rol)
                if not normalizado or normalizado in vistos:
                    continue
                vistos.add(normalizado)
                if self.rol_permitido(usuario, normalizado):
                    resultado.append(normalizado)
            return resultado
        except Exception:
            try:
                return list(candidatos or [])
            except Exception:
                return []

    def reservar(self, usuario, rol) -> bool:
        """Reserva ATOMICA de un cupo; False si ya no hay (o par invalido).

        Con limite ``<= 0`` (ilimitado) siempre reserva; con limite > 0 exige
        ``usado < limite``. Contar la reserva como uso es lo que evita que dos
        workers pasen el limite en paralelo.
        """
        try:
            with self._lock:
                clave = self._clave(usuario, rol)
                if clave is None:
                    return False
                limite = int(self._limites.get(clave[1], 0))
                if limite > 0 and self._usado_locked(clave) >= limite:
                    return False
                self._reservas[clave] = int(self._reservas.get(clave, 0)) + 1
                return True
        except Exception:
            return False

    def liberar(self, usuario, rol, exito: bool) -> None:
        """Cierra una reserva: la devuelve o la convierte en exito.

        Siempre descuenta la reserva (nunca por debajo de 0) y, solo con
        ``exito=True``, suma la accion a ``exitos_campana`` (consume cupo).
        """
        try:
            with self._lock:
                clave = self._clave(usuario, rol)
                if clave is None:
                    return
                en_vuelo = int(self._reservas.get(clave, 0))
                if en_vuelo > 1:
                    self._reservas[clave] = en_vuelo - 1
                else:
                    self._reservas.pop(clave, None)
                if exito:
                    self._exitos[clave] = int(self._exitos.get(clave, 0)) + 1
        except Exception:
            pass

    def agotado(self, usuario, candidatos) -> bool:
        """True si hay candidatos y NINGUNO tiene cupo; sin candidatos False."""
        try:
            lista = list(candidatos or [])
        except TypeError:
            return False
        if not lista:
            return False
        return not self.viables(usuario, lista)

    def avisar_agotada(self, usuario) -> bool:
        """True si toca avisar que la cuenta agoto sus cuotas (throttle).

        Como maximo un aviso cada ``max_aviso_seg`` por cuenta (medido con
        ``time.monotonic``); con ``max_aviso_seg=0`` avisa siempre.
        """
        try:
            clave = str(usuario or "")
            ahora = time.monotonic()
            with self._lock:
                anterior = self._ultimo_aviso.get(clave)
                if (
                    anterior is not None
                    and (ahora - float(anterior)) < self._max_aviso_seg
                ):
                    return False
                self._ultimo_aviso[clave] = ahora
                return True
        except Exception:
            return True

    def uso(self, usuario) -> dict:
        """Estado por rol de una cuenta: ``{rol: {"usado": n, "limite": m}}``."""
        try:
            resultado = {}
            for rol in ROLES_CUOTA:
                resultado[rol] = {
                    "usado": self._usado(usuario, rol),
                    "limite": self._limite(rol),
                }
            return resultado
        except Exception:
            return {}

    def _limite(self, rol) -> int:
        """Limite del rol (0 = ilimitado; rol desconocido = 0)."""
        try:
            normalizado = self._clave_rol(rol)
            return int(self._limites.get(normalizado, 0))
        except Exception:
            return 0

    def resumen(self) -> dict:
        """Resumen agregado de la campana (nunca lanza)."""
        try:
            with self._lock:
                return {
                    "limites": dict(self._limites),
                    "ventana_min": int(self._minutos),
                    "acciones_exitosas": sum(
                        int(valor) for valor in self._exitos.values()
                    ),
                    "reservas_activas": sum(
                        int(valor) for valor in self._reservas.values()
                    ),
                }
        except Exception:
            return {
                "limites": {},
                "ventana_min": 60,
                "acciones_exitosas": 0,
                "reservas_activas": 0,
            }

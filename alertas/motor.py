"""Motor de alertas (coordinador).

Pipeline por cliente:
  1. `GoogleNewsSource.buscar` (web + site: redes; split principales/genéricas,
     variantes de acento, `when:` por ventana, fecha fail-open).
  2. Filtros locales: `exclude_terms` + blacklist global (solo genéricas) y
     geo por regiones; rescate por cuerpo de artículo (opt-out por env).
  3. Deduplicación persistente por cliente (`Deduplicador`, tabla
     `alertas_historial`).
  4. Filtro IA tolerante: usa `FiltrosAlertas.clasificar_menciones` si existe
     (fail-open; nunca rompe la corrida).
  5. Envío por Telegram: `enviadas` cuenta SOLO si el notificador confirma.
  6. Persistencia para dashboards: `MencionDia` (hoy) y `ReporteDiario`
     (por_fuente + por_temas).

`TwitterSource` queda APAGADO por default (`ALERTAS_TWITTER_ACTIVO=0`): su
implementacion actual manda variables GraphQL invalidas (400) y no registra
errores; mientras no se repare, X/Facebook/Instagram se cubren con Google News
`site:`. Si se activa, sus fallos se loguean y no tumban la corrida.
"""

import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from loguru import logger

from alertas.deduplicacion import Deduplicador
from alertas.filtros import (
    FiltrosGeograficos,
    FiltrosTematicos,
    texto_normalizado,
    verificacion_cuerpo_activa,
    verificar_cuerpo_batch,
)
from alertas.fuentes.google_news import GoogleNewsSource
from alertas.fuentes.twitter_api import TwitterSource
from alertas.notificador import NotificadorTelegram
from core.database import get_db_session
from core.models import MencionDia, ReporteDiario
from ia.celulas import CelulasManager
from ia.filtros_alertas import FiltrosAlertas

_TWITTER_AVISADO = False


def _twitter_activo() -> bool:
    """TwitterSource solo si se pide explicitamente (esta roto, ver AGENTS)."""
    return (os.getenv("ALERTAS_TWITTER_ACTIVO", "0") or "").strip().lower() in (
        "1", "true", "si", "sí", "yes", "on"
    )


def _match_kw(kw: str, texto_norm: str) -> bool:
    """True si la keyword aparece en el texto normalizado.

    Tokens simples (palabra/handle) usan match por PALABRA COMPLETA para que
    "SEMA" no matchee "semana"; frases y tokens con signos (ej. "M.I.A.") usan
    la regla historica (todas sus palabras presentes / substring).
    """
    from alertas.filtros import _contiene_palabra_completa, _normalizar_texto

    kw_norm = _normalizar_texto(kw)
    if not kw_norm or len(kw_norm) < 2:
        return False
    if " " not in kw_norm:
        if re.match(r"^[\w@#]+$", kw_norm):
            return _contiene_palabra_completa(texto_norm, kw_norm)
        return kw_norm in texto_norm
    return all(p in texto_norm for p in kw_norm.split())


def _envio_habilitado(notificador) -> bool:
    """True si el notificador enviara a Telegram (tolerante a notificadores fake).

    Default True cuando el objeto no expone `envio_habilitado` (comportamiento
    historico: si no hay pausa declarada, se envia).
    """
    return bool(getattr(notificador, "envio_habilitado", True))


def _avisar_pausa(notificador, total: int) -> None:
    """Avisa UNA vez por corrida si el notificador real esta en pausa."""
    avisar = getattr(notificador, "preparar_pausa", None)
    if callable(avisar):
        avisar(total)


class MotorAlertas:
    def __init__(self):
        self.google_news = GoogleNewsSource()
        self.twitter_source = TwitterSource()
        self.filtros_geo = FiltrosGeograficos()
        self.filtros_tematicos = FiltrosTematicos()
        self.deduplicador = Deduplicador()
        self.notificador = NotificadorTelegram()
        self.filtros_ai = FiltrosAlertas()
        self.celulas_manager = CelulasManager()

    # ------------------------------------------------------------------ #
    # API publica
    # ------------------------------------------------------------------ #
    def ejecutar_alertas(
        self,
        cliente_id: int = None,
        horas: int = 24,
        resumen_diario: bool = False,
        clientes_filtro: list = None,
    ) -> dict:
        """Ejecuta alertas para todos los clientes (o uno / lista por nombre).

        `resumen_diario=True` envia ademas el resumen diario por cliente al
        terminar (mismo efecto que llamar `enviar_resumen_diario()`).
        """
        clientes = self.celulas_manager.obtener_clientes()

        if cliente_id:
            clientes = [c for c in clientes if c.id == cliente_id]
        if clientes_filtro:
            clientes = [c for c in clientes if c.nombre in clientes_filtro]

        resultados = {
            "total": 0,
            "enviadas": 0,
            "filtradas": 0,
            "duplicadas": 0,
            "clientes procesados": len(clientes),
            "envio_pausado": not _envio_habilitado(self.notificador),
        }

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                (executor.submit(self._procesar_cliente, cliente, horas), cliente)
                for cliente in clientes
            ]

            for future, cliente in futures:
                try:
                    resultado = future.result()
                    resultados["total"] += resultado["total"]
                    resultados["enviadas"] += resultado["enviadas"]
                    resultados["filtradas"] += resultado["filtradas"]
                    resultados["duplicadas"] += resultado["duplicadas"]
                except Exception as e:
                    logger.error(f"Error procesando cliente {cliente.nombre}: {e}")

        if resumen_diario:
            try:
                self.enviar_resumen_diario()
            except Exception as e:
                logger.error(f"Error enviando resumen diario: {e}")

        return resultados

    def enviar_resumen_diario(self) -> dict:
        """Envia el resumen diario (menciones de HOY) por cliente y marca
        `ReporteDiario.enviado`. Pensado para el job del scheduler.

        Con la pausa activa (`ALERTAS_ENVIAR_TELEGRAM=0`) no se envia nada: el
        reporte queda `enviado=False` y se cuentan en `pausados`.
        """
        pausado = not _envio_habilitado(self.notificador)
        resumen = {
            "clientes": 0,
            "enviados": 0,
            "sin_menciones": 0,
            "errores": 0,
            "pausados": 0,
            "envio_pausado": pausado,
        }

        try:
            clientes = self.celulas_manager.obtener_clientes()
        except Exception as e:
            logger.error(f"Resumen diario: no se pudieron obtener clientes: {e}")
            return resumen

        hoy = date.today()
        for cliente in clientes:
            try:
                menciones = self._menciones_de_hoy(cliente.id, hoy)
                if not menciones:
                    resumen["sin_menciones"] += 1
                    continue

                chats = [c.strip() for c in (cliente.telegram_chat_ids or "").split(",") if c.strip()]
                if not chats:
                    logger.warning(f"Resumen diario {cliente.nombre}: sin telegram_chat_ids")
                    resumen["errores"] += 1
                    continue

                if pausado:
                    # Una sola llamada al notificador: avisa y no toca la red.
                    _avisar_pausa(self.notificador, len(menciones))
                    ok = False
                    resumen["pausados"] += 1
                else:
                    ok = True
                    for chat_id in chats:
                        try:
                            if not self.notificador.enviar_resumen(chat_id, cliente.nombre, menciones):
                                ok = False
                        except Exception as e:
                            logger.error(f"Resumen diario {cliente.nombre} chat {chat_id}: {e}")
                            ok = False

                self._marcar_reporte_enviado(cliente, hoy, menciones, ok)
                resumen["clientes"] += 1
                if ok:
                    resumen["enviados"] += 1
            except Exception as e:
                logger.error(f"Resumen diario {cliente.nombre}: {e}")
                resumen["errores"] += 1

        return resumen

    # ------------------------------------------------------------------ #
    # Procesamiento por cliente
    # ------------------------------------------------------------------ #
    def _procesar_cliente(self, cliente, horas: int) -> dict:
        resultado = {"total": 0, "enviadas": 0, "filtradas": 0, "duplicadas": 0}

        try:
            keywords = json.loads(cliente.keywords) if cliente.keywords else []
            exclude_terms = json.loads(cliente.exclude_terms) if cliente.exclude_terms else []
            keywords = [str(k).strip() for k in keywords if str(k).strip()]

            try:
                split = int(cliente.num_principales or 0)
            except (TypeError, ValueError):
                split = 0
            if split <= 0:
                split = 10
            kw_especificas = keywords[:split]
            kw_genericas = keywords[split:]

            # 1) Fuentes
            menciones = list(self.google_news.buscar(
                keywords, cliente.localidad or "", horas, num_principales=split
            ))
            menciones.extend(self._buscar_twitter(cliente, keywords, horas))

            resultado["total"] = len(menciones)

            # 2) Marcar principal / generica (para filtros correctos)
            sin_match = []
            for mencion in menciones:
                mencion.setdefault("cliente", cliente.nombre)

                titulo = (mencion.get("titulo") or "").strip()
                resumen = (mencion.get("resumen") or "").strip()
                # Descarta entradas sin titulo/resumen utilizables (ej. "Canal 10").
                if len(titulo) < 10 and len(resumen) < 40:
                    continue

                texto = texto_normalizado(mencion)

                principal = None
                for kw in kw_especificas:
                    if _match_kw(kw, texto):
                        principal = kw
                        break
                if principal:
                    mencion["es_principal"] = True
                    mencion["kw_principal"] = principal
                    continue

                mencion["es_principal"] = False
                generica = None
                for kw in kw_genericas:
                    if _match_kw(kw, texto):
                        generica = kw
                        break
                if generica:
                    mencion["kw_principal"] = generica
                else:
                    sin_match.append(mencion)

            ids_sin_match = {id(m) for m in sin_match}

            # 3) Filtros locales (tema + geo)
            filtradas = []
            for mencion in menciones:
                if id(mencion) in ids_sin_match:
                    continue
                if self.filtros_tematicos.excluir(
                    mencion, exclude_terms, es_principal=mencion.get("es_principal", False)
                ):
                    continue
                if cliente.localidad:
                    if not self.filtros_geo.verificar_region(
                        mencion, cliente.localidad,
                        tiene_kw_especifica=mencion.get("es_principal", False),
                    ):
                        continue
                filtradas.append(mencion)

            # 4) Rescate por cuerpo del articulo (solo si hay kw principales)
            if sin_match and kw_especificas and verificacion_cuerpo_activa():
                try:
                    rescatadas = verificar_cuerpo_batch(sin_match, kw_especificas)
                except Exception as e:
                    logger.warning(f"Cuerpo del articulo fallo (se omite rescate): {e}")
                    rescatadas = []
                for mencion in rescatadas:
                    if cliente.localidad and not self.filtros_geo.verificar_region(
                        mencion, cliente.localidad, tiene_kw_especifica=True
                    ):
                        continue
                    filtradas.append(mencion)

            resultado["filtradas"] = resultado["total"] - len(filtradas)

            # 5) Deduplicacion persistente por cliente
            filtradas = self.deduplicador.filtrar_duplicados(filtradas, cliente.id)
            nuevas = [m for m in filtradas if not m.get("duplicada")]
            resultado["duplicadas"] = len(filtradas) - len(nuevas)

            # 6) Filtro IA (fail-open; clasificador del otro agente si existe)
            nuevas = self._clasificar_con_ia(nuevas, kw_especificas, cliente.localidad or "")

            # 7) Envio (cuenta solo lo confirmado por el notificador).
            #    Con la pausa activa (ALERTAS_ENVIAR_TELEGRAM=0) NO se llama a
            #    Telegram: se avisa una vez y las menciones finales se siguen
            #    registrando abajo (MencionDia + historial) para el dashboard.
            chats = [c.strip() for c in (cliente.telegram_chat_ids or "").split(",") if c.strip()]
            enviadas = 0
            if not _envio_habilitado(self.notificador):
                _avisar_pausa(self.notificador, len(nuevas))
            else:
                for mencion in nuevas:
                    try:
                        if self.notificador.enviar_alerta(mencion, chats):
                            enviadas += 1
                    except Exception as e:
                        logger.error(f"Error enviando alerta de {cliente.nombre}: {e}")
            resultado["enviadas"] = enviadas

            # 8) Persistencia para dashboards/reportes (SIEMPRE, tambien en pausa)
            self.deduplicador.guardar_historial(nuevas, cliente.id)
            self._guardar_menciones_dia(cliente, nuevas)
            self._actualizar_reporte(cliente)

        except Exception as e:
            logger.error(f"Error en _procesar_cliente ({getattr(cliente, 'nombre', '?')}): {e}")

        return resultado

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _buscar_twitter(self, cliente, keywords: list, horas: int) -> list:
        """TwitterSource solo si ALERTAS_TWITTER_ACTIVO=1; fallos se loguean."""
        global _TWITTER_AVISADO

        if not _twitter_activo():
            if not _TWITTER_AVISADO:
                logger.info(
                    "TwitterSource desactivado (ALERTAS_TWITTER_ACTIVO=0): "
                    "X/FB/IG se cubren con Google News site:"
                )
                _TWITTER_AVISADO = True
            return []

        try:
            return self.twitter_source.buscar(keywords, horas) or []
        except Exception as e:
            logger.warning(f"TwitterSource fallo para {cliente.nombre} (se continua): {e}")
            return []

    def _clasificar_con_ia(self, menciones: list, kw_especificas: list, localidad: str) -> list:
        """Usa `FiltrosAlertas.clasificar_menciones` si existe (fail-open).

        - Si no existe o lanza: devuelve las menciones SIN tocar.
        - Si devuelve una lista valida: se respeta (marca lo que traiga).
        """
        if not menciones:
            return menciones

        clasificador = getattr(self.filtros_ai, "clasificar_menciones", None)
        if not callable(clasificador):
            return menciones

        try:
            resultado = clasificador(
                menciones,
                keywords_principales=kw_especificas,
                localidad=localidad,
            )
        except TypeError:
            # Firma vieja/alterna sin kwargs.
            try:
                resultado = clasificador(menciones)
            except Exception as e:
                logger.warning(f"Filtro IA fallo (fail-open, se conservan todas): {e}")
                return menciones
        except Exception as e:
            logger.warning(f"Filtro IA fallo (fail-open, se conservan todas): {e}")
            return menciones

        if resultado is None or not isinstance(resultado, list):
            logger.warning("Filtro IA devolvio un valor no-lista (fail-open, se conservan todas)")
            return menciones

        validas = [m for m in resultado if isinstance(m, dict)]
        if len(validas) != len(resultado):
            logger.warning("Filtro IA devolvio elementos no-dict; se conservan originales")
            return menciones
        return validas

    def _guardar_menciones_dia(self, cliente, menciones: list):
        """Registra en `menciones_dia` las menciones finales del día (sin repetir URL)."""
        if not menciones:
            return
        hoy = date.today()
        try:
            with get_db_session() as db:
                existentes = {
                    url for (url,) in db.query(MencionDia.enlace).filter(
                        MencionDia.cliente_id == cliente.id,
                        MencionDia.fecha == hoy,
                    ).all()
                    if url
                }

                agregadas = 0
                for mencion in menciones:
                    enlace = mencion.get("enlace", "")
                    if not enlace or enlace in existentes:
                        continue

                    db.add(MencionDia(
                        cliente_id=cliente.id,
                        titulo=mencion.get("titulo", "") or "",
                        resumen=(mencion.get("resumen_ai") or mencion.get("resumen") or "")[:2000],
                        enlace=enlace,
                        fuente=(mencion.get("fuente") or "")[:50],
                        es_principal=bool(mencion.get("es_principal")),
                        kw_principal=(mencion.get("kw_principal") or "")[:100],
                        fecha=hoy,
                    ))
                    existentes.add(enlace)
                    agregadas += 1

                db.commit()
                if agregadas:
                    logger.info(f"Menciones del día {cliente.nombre}: {agregadas} agregadas")
        except Exception as e:
            logger.error(f"Error guardando menciones del día ({cliente.nombre}): {e}")

    def _menciones_de_hoy(self, cliente_id: int, hoy: date) -> list:
        menciones = []
        try:
            with get_db_session() as db:
                filas = db.query(MencionDia).filter(
                    MencionDia.cliente_id == cliente_id,
                    MencionDia.fecha == hoy,
                ).all()
                menciones = [{
                    "titulo": f.titulo,
                    "enlace": f.enlace,
                    "fuente": f.fuente,
                    "es_principal": f.es_principal,
                    "resumen": f.resumen,
                } for f in filas]
        except Exception as e:
            logger.error(f"Error leyendo menciones del día ({cliente_id}): {e}")
        return menciones

    def _actualizar_reporte(self, cliente):
        """Crea/actualiza el `ReporteDiario` de hoy (por_fuente + por_temas)."""
        hoy = date.today()
        try:
            with get_db_session() as db:
                filas = db.query(MencionDia).filter(
                    MencionDia.cliente_id == cliente.id,
                    MencionDia.fecha == hoy,
                ).all()

                por_fuente: dict = {}
                titulares = []
                for fila in filas:
                    fuente = fila.fuente or "Otra"
                    por_fuente[fuente] = por_fuente.get(fuente, 0) + 1
                    if fila.titulo:
                        titulares.append(fila.titulo)

                por_temas = self._agrupar_temas(titulares)

                reporte = db.query(ReporteDiario).filter(
                    ReporteDiario.cliente_id == cliente.id,
                    ReporteDiario.fecha == hoy,
                ).first()

                if reporte is None:
                    reporte = ReporteDiario(
                        fecha=hoy,
                        cliente_id=cliente.id,
                        total_alertas=len(filas),
                        por_fuente=por_fuente,
                        por_temas=por_temas,
                        enviado=False,
                    )
                    db.add(reporte)
                else:
                    reporte.total_alertas = len(filas)
                    reporte.por_fuente = por_fuente
                    reporte.por_temas = por_temas

                db.commit()
        except Exception as e:
            logger.error(f"Error actualizando ReporteDiario ({cliente.nombre}): {e}")

    def _agrupar_temas(self, titulares: list) -> dict:
        """{tema: cantidad} usando `FiltrosAlertas.agrupar_temas` si existe."""
        temas = []
        agrupador = getattr(self.filtros_ai, "agrupar_temas", None)
        if callable(agrupador):
            try:
                temas = agrupador(titulares) or []
            except Exception as e:
                logger.warning(f"Filtro IA agrupar_temas fallo (fallback local): {e}")
                temas = []

        if not temas:
            temas = self._agrupar_temas_local(titulares)

        por_temas: dict = {}
        for tema in temas:
            if not isinstance(tema, dict):
                continue
            nombre = tema.get("tema") or tema.get("titulo") or ""
            cantidad = tema.get("articulos", tema.get("cantidad", 1))
            if not nombre:
                continue
            try:
                cantidad = int(cantidad)
            except (TypeError, ValueError):
                cantidad = 1
            por_temas[str(nombre)[:80]] = cantidad
        return por_temas

    @staticmethod
    def _agrupar_temas_local(titulares: list) -> list:
        """Fallback sin IA: palabras frecuentes (como el viejo agrupar_por_temas)."""
        palabras = []
        for titulo in titulares:
            for palabra in (titulo or "").lower().split():
                limpio = "".join(c for c in palabra if c.isalnum())
                if len(limpio) > 4:
                    palabras.append(limpio)
        conteo = Counter(palabras).most_common(5)
        return [{"titulo": palabra, "cantidad": cantidad} for palabra, cantidad in conteo]

    def _marcar_reporte_enviado(self, cliente, hoy: date, menciones: list, ok: bool):
        """Marca `enviado` en el reporte de hoy (creandolo si no existe)."""
        try:
            with get_db_session() as db:
                reporte = db.query(ReporteDiario).filter(
                    ReporteDiario.cliente_id == cliente.id,
                    ReporteDiario.fecha == hoy,
                ).first()

                if reporte is None:
                    por_fuente: dict = {}
                    for mencion in menciones:
                        fuente = mencion.get("fuente") or "Otra"
                        por_fuente[fuente] = por_fuente.get(fuente, 0) + 1
                    reporte = ReporteDiario(
                        fecha=hoy,
                        cliente_id=cliente.id,
                        total_alertas=len(menciones),
                        por_fuente=por_fuente,
                        por_temas={},
                        enviado=bool(ok),
                    )
                    db.add(reporte)
                else:
                    reporte.enviado = bool(ok)

                db.commit()
        except Exception as e:
            logger.error(f"Error marcando reporte enviado ({cliente.nombre}): {e}")

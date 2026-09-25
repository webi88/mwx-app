"""Deduplicacion persistente por cliente (tabla `alertas_historial`).

Se conserva el mecanismo del proyecto nuevo (BD, ventana de dias) y se agrega
normalizacion de URLs para que parametros de tracking (`utm_*`, `fbclid`, `#`)
no generen falsos "nuevos".
"""

import os
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from loguru import logger

from core.database import get_db_session
from core.models import AlertaHistorial

_PARAMS_IGNORADOS = ("fbclid", "gclid", "igshid", "mc_cid", "mc_eid")


def normalizar_url(url: str) -> str:
    """URL canonica para comparar: sin utm/tracking, sin fragmento, sin '/' final."""
    if not url:
        return ""
    try:
        partes = urlsplit(str(url).strip())
        query = [
            (k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _PARAMS_IGNORADOS
        ]
        path = partes.path.rstrip("/") or "/"
        return urlunsplit((partes.scheme.lower(), partes.netloc.lower(), path, urlencode(query), ""))
    except Exception:
        return str(url).strip()


class Deduplicador:
    def __init__(self):
        try:
            self.dias_historial = int(os.getenv("ALERTAS_DIAS_HISTORIAL", "7"))
        except (TypeError, ValueError):
            self.dias_historial = 7
        self.dias_historial = max(1, self.dias_historial)

    def filtrar_duplicados(self, menciones: list[dict], cliente_id: int) -> list[dict]:
        """Marca `duplicada=True/False` y elimina repetidas DENTRO de la corrida."""
        urls_existentes = self._obtener_urls_existentes(cliente_id)

        resultado = []
        urls_vistas = set()

        for mencion in menciones:
            url = normalizar_url(mencion.get("enlace", ""))

            if not url:
                continue

            if url in urls_vistas:
                continue

            mencion["duplicada"] = url in urls_existentes

            urls_vistas.add(url)
            resultado.append(mencion)

        return resultado

    def _obtener_urls_existentes(self, cliente_id: int) -> set:
        urls = set()

        try:
            fecha_limite = datetime.now() - timedelta(days=self.dias_historial)

            with get_db_session() as db:
                historial = db.query(AlertaHistorial).filter(
                    AlertaHistorial.cliente_id == cliente_id,
                    AlertaHistorial.fecha_envio >= fecha_limite
                ).all()

                for registro in historial:
                    urls.add(normalizar_url(registro.url))
        except Exception as e:
            logger.error(f"Error obteniendo historial: {e}")

        return urls

    def guardar_historial(self, menciones: list[dict], cliente_id: int):
        """Guarda las URLs de las menciones (normalizadas) sin repetir registros."""
        try:
            guardadas = 0
            with get_db_session() as db:
                for mencion in menciones:
                    url = normalizar_url(mencion.get("enlace", ""))

                    if not url:
                        continue

                    existente = db.query(AlertaHistorial).filter(
                        AlertaHistorial.cliente_id == cliente_id,
                        AlertaHistorial.url == url
                    ).first()

                    if not existente:
                        registro = AlertaHistorial(
                            cliente_id=cliente_id,
                            url=url,
                            fuente=mencion.get("fuente", ""),
                            fecha_envio=datetime.now()
                        )
                        db.add(registro)
                        guardadas += 1

                db.commit()
                logger.info(f"Historial guardado: {guardadas} nuevas de {len(menciones)} menciones")
        except Exception as e:
            logger.error(f"Error guardando historial: {e}")

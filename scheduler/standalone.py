"""Proceso standalone del scheduler (para Railway/supervisord).

Mantiene vivo el SchedulerManager en segundo plano para que las tareas
programadas (post/RT/like/follow/visualización) se ejecuten 24/7.

Arranque tolerante: si la base de datos no está lista todavía (Supabase fría,
red transitoria o un bloqueo de SQLite al arrancar a la vez que el dashboard),
reintenta en vez de morir con exit 1 y que supervisord lo marque como FATAL.
"""
import time

from loguru import logger

from core.database import init_db


def _inicializar_bd():
    """Inicializa la BD con reintentos para tolerar arranques transitorios."""
    while True:
        try:
            init_db()
            logger.info("Base de datos inicializada correctamente")
            return
        except Exception:
            logger.exception("No se pudo inicializar la BD. Reintentando en 5s...")
            time.sleep(5)


def main():
    _inicializar_bd()

    try:
        from scheduler.manager import SchedulerManager

        # Unico proceso que corre el calentamiento continuo y las alertas 24/7;
        # el dashboard instancia SchedulerManager() sin argumentos y NO los
        # registra (si lo hiciera, cada pagina buscaria y enviaria las MISMAS
        # alertas a Telegram: envios duplicados).
        SchedulerManager(con_calentamiento=True, con_alertas=True)
    except Exception:
        logger.exception("Error iniciando SchedulerManager")
        raise

    logger.info(
        "Scheduler standalone en ejecución (tareas cada 30s + calentamiento y "
        "alertas 24/7 según ALERTAS_*)"
    )

    # Mantener vivo el hilo principal. El BackgroundScheduler de APScheduler
    # corre en un hilo aparte. Usamos sleep(1) para responder rápido a SIGTERM.
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()

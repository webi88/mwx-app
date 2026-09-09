"""Proceso standalone del scheduler (para Railway/supervisord).

Mantiene vivo el SchedulerManager en segundo plano para que las tareas
programadas (post/RT/like/follow/visualización) se ejecuten 24/7.
"""
import time

from loguru import logger

from core.database import init_db
from scheduler.manager import SchedulerManager


def main():
    init_db()
    SchedulerManager()
    logger.info("Scheduler standalone en ejecución (revisando tareas cada 30s)")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()

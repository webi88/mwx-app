#!/usr/bin/env python3
"""Script autonomo de alertas (wrapper delgado de `MotorAlertas`).

Uso:
    python auto_alertas.py                        # Todos los clientes, 24h
    python auto_alertas.py --clientes ORA,ISLA    # Solo esos clientes
    python auto_alertas.py --horas 6              # Ventana de 6 horas
    python auto_alertas.py --resumen-diario       # Envia ademas el resumen del dia

No mantiene estado propio: la deduplicacion vive en la tabla
`alertas_historial` (via `Deduplicador`) y las menciones/reportes del dia en
`menciones_dia` / `reportes_diarios`. Pensado para cron/supervisord 24/7.
"""

import argparse
import sys

from loguru import logger

from alertas.motor import MotorAlertas


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sistema de alertas automaticas (MotorAlertas)"
    )
    parser.add_argument(
        "--clientes",
        type=str,
        help="Clientes a procesar (separados por coma). Ej: ORA,ISLA",
    )
    parser.add_argument(
        "--horas",
        type=int,
        default=24,
        help="Ventana de tiempo en horas (default: 24)",
    )
    parser.add_argument(
        "--resumen-diario",
        action="store_true",
        help="Enviar tambien el resumen diario por cliente",
    )

    args = parser.parse_args()
    clientes_filtro = (
        [c.strip() for c in args.clientes.split(",") if c.strip()]
        if args.clientes
        else None
    )

    try:
        motor = MotorAlertas()
        resultados = motor.ejecutar_alertas(
            horas=args.horas,
            resumen_diario=args.resumen_diario,
            clientes_filtro=clientes_filtro,
        )
    except Exception as e:
        logger.error(f"Error fatal en alertas: {e}")
        return 1

    logger.info(f"Alertas completadas: {resultados}")
    print(
        f"Alertas: {resultados.get('enviadas', 0)} enviadas de "
        f"{resultados.get('total', 0)} menciones "
        f"({resultados.get('filtradas', 0)} filtradas, "
        f"{resultados.get('duplicadas', 0)} duplicadas, "
        f"{resultados.get('clientes procesados', 0)} clientes)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Exportacion de cuentas a Excel (.xlsx)."""
from datetime import datetime

from loguru import logger

from core.config import resolver_ruta
from core.database import get_db_session
from core.models import Cuenta


# (atributo_del_modelo, titulo_de_columna) en el orden en que se mostraran.
# Todos los campos necesarios para un login manual de la cuenta.
COLUMNAS_CUENTAS = (
    ("usuario", "Usuario_X"),
    ("password", "Password_X"),
    ("totp_secret", "Semilla_2FA_TOTP"),
    ("email", "Email_Respaldo"),
    ("email_password", "Password_Email"),
    ("auth_token", "Auth_Token"),
    ("status", "Estado"),
)

INSTRUCCIONES_LOGIN = [
    "GUIA DE ACCESO MANUAL A X (LOGIN EXTERNO)",
    "",
    "Paso 1 (Acceso directo): Ve a https://x.com/i/flow/login en un navegador limpio o perfil aislado.",
    "Paso 2 (Credenciales): Ingresa el Usuario_X y la Password_X correspondientes.",
    "Paso 3 (Resolucion de 2FA / OTP):",
    "  * Si X solicita el codigo de seguridad de 2 factores, NO busques SMS ni correos.",
    "  * Abre cualquier generador TOTP (ej. https://2fa.live, una extension de navegador o Google Authenticator).",
    "  * Pega la cadena de la columna Semilla_2FA_TOTP.",
    "  * Copia el codigo temporal de 6 digitos resultante y pegalo en X para completar el login.",
    "Paso 4 (Alternativa sin login): Para extensiones de inyeccion de cookies (como Cookie-Editor), basta con inyectar el valor de Auth_Token en el dominio x.com.",
]


def exportar_cuentas_excel(ruta: str = None, solo_activas: bool = False) -> dict:
    """Genera un archivo Excel con las cuentas (solo usuario y contraseña).

    Lee todas las cuentas de la base de datos y escribe un .xlsx con una
    cabecera con estilo. Si 'solo_activas' es True, solo exporta las cuentas
    activas. Si no se pasa 'ruta', guarda en data/reportes/ con un nombre con
    marca de tiempo.

    Devuelve {'ruta': str, 'total': int} en caso de exito, o
    {'ruta': '', 'total': 0, 'error': str} en caso de error.
    """
    import os

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    try:
        with get_db_session() as db:
            query = db.query(Cuenta)
            if solo_activas:
                query = query.filter(Cuenta.activa.is_(True))
            cuentas = query.order_by(Cuenta.fecha_creacion.desc()).all()

        if not ruta:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ruta = resolver_ruta(f"data/reportes/cuentas_{stamp}.xlsx")

        os.makedirs(os.path.dirname(ruta), exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Cuentas"

        cabeceras = [titulo for _, titulo in COLUMNAS_CUENTAS]
        ws.append(cabeceras)

        # Estilo de la cabecera.
        for col_idx in range(1, len(cabeceras) + 1):
            celda = ws.cell(row=1, column=col_idx)
            celda.font = Font(bold=True, color="FFFFFF")
            celda.fill = PatternFill("solid", fgColor="1DA1F2")
            celda.alignment = Alignment(horizontal="center")

        for cuenta in cuentas:
            fila = []
            for atributo, _ in COLUMNAS_CUENTAS:
                valor = getattr(cuenta, atributo, "")
                fila.append("" if valor is None else valor)
            ws.append(fila)

        # Ancho de columnas para que se lea bien.
        for col_idx in range(1, len(cabeceras) + 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 22

        # Hoja con la guia de acceso manual.
        ws_inst = wb.create_sheet("Instrucciones_Login")
        for idx, linea in enumerate(INSTRUCCIONES_LOGIN, start=1):
            celda = ws_inst.cell(row=idx, column=1, value=linea)
            if idx == 1:
                celda.font = Font(bold=True)
        ws_inst.column_dimensions["A"].width = 95

        wb.save(ruta)
        logger.info(f"Excel de cuentas generado: {ruta} ({len(cuentas)} cuentas)")
        return {"ruta": ruta, "total": len(cuentas)}

    except Exception as e:
        logger.error(f"Error exportando cuentas a Excel: {e}")
        return {"ruta": "", "total": 0, "error": str(e)}


def exportar_cuentas_csv(ruta: str = None, solo_activas: bool = False) -> dict:
    """Genera un archivo CSV con las cuentas (todos los campos de login manual).

    Lee todas las cuentas de la base de datos y escribe un .csv con cabecera
    igual a los titulos de COLUMNAS_CUENTAS y una fila por cuenta, ordenadas
    por fecha de creacion descendente. Si 'solo_activas' es True, solo exporta
    las cuentas activas. Si no se pasa 'ruta', guarda en data/reportes/ con un
    nombre con marca de tiempo.

    Devuelve {'ruta': str, 'total': int} en caso de exito, o
    {'ruta': '', 'total': 0, 'error': str} en caso de error.
    """
    import csv
    import os

    try:
        with get_db_session() as db:
            query = db.query(Cuenta)
            if solo_activas:
                query = query.filter(Cuenta.activa.is_(True))
            cuentas = query.order_by(Cuenta.fecha_creacion.desc()).all()

        if not ruta:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ruta = resolver_ruta(f"data/reportes/cuentas_{stamp}.csv")

        os.makedirs(os.path.dirname(ruta), exist_ok=True)

        cabeceras = [titulo for _, titulo in COLUMNAS_CUENTAS]
        with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(cabeceras)
            for cuenta in cuentas:
                fila = []
                for atributo, _ in COLUMNAS_CUENTAS:
                    valor = getattr(cuenta, atributo, "")
                    fila.append("" if valor is None else valor)
                writer.writerow(fila)

        logger.info(f"CSV de cuentas generado: {ruta} ({len(cuentas)} cuentas)")
        return {"ruta": ruta, "total": len(cuentas)}

    except Exception as e:
        logger.error(f"Error exportando cuentas a CSV: {e}")
        return {"ruta": "", "total": 0, "error": str(e)}

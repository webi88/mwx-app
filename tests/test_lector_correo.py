"""Tests rapidos de `utils/lector_correo.py` (SIN red, SIN BD y SIN credenciales).

Cubre el contrato congelado que consume `bot_clientes/handlers.py`:

    obtener_codigo_verificacion(email, password, timeout=25) -> dict
        {"ok", "codigo", "remitente", "asunto", "fecha", "error"}  (claves EXACTAS)

Se prueba:
  (1) `extraer_codigo_de_texto`: 6 digitos en espanol/ingles, HTML, entidades,
      asunto vs cuerpo, prioridad por cercania a "codigo/verification",
      descarte de telefonos/ids/colores hex y tolerancia total a basura.
  (2) `obtener_codigo_verificacion` con un IMAP FALSO (monkeypatch de
      `imaplib.IMAP4_SSL`): happy path, preferencia de remitente X/Twitter,
      INBOX -> Spam como respaldo, contrato exacto, errores claros (credenciales,
      timeout, conexion) y NUNCA la contrasena en la respuesta.
  (3) `diagnosticar_conexion`: login OK con carpetas/conteo y errores sin lanzar.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_lector_correo.py   (solo este archivo)
"""
from __future__ import annotations

import imaplib
import os
import socket
import sys
from email.message import EmailMessage
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from utils.lector_correo import (  # noqa: E402
    diagnosticar_conexion,
    extraer_codigo_de_texto,
    obtener_codigo_verificacion,
)

CLAVES_CONTRATO = {"ok", "codigo", "remitente", "asunto", "fecha", "error"}
CLAVES_DIAGNOSTICO = {"ok", "host", "carpetas", "mensajes", "tiempo_seg", "error"}

USUARIO = "cliente@yahoo.com"
CLAVE = "clave-secreta-123"


# --------------------------------------------------------------------------- #
# IMAP falso (nunca toca la red)
# --------------------------------------------------------------------------- #
class _Mensaje:
    """Correo crudo (RFC822) con su UID."""

    def __init__(self, uid: int, crudo: bytes):
        self.uid = uid
        self.crudo = crudo


class _Config:
    """Configuracion del IMAP falso."""

    def __init__(
        self,
        usuario=USUARIO,
        clave=CLAVE,
        carpetas=None,
        mensajes=None,
        error_login=None,
        error_constructor=None,
    ):
        self.usuario = usuario
        self.clave = clave
        self.carpetas = list(carpetas or ["INBOX"])
        self.mensajes = dict(mensajes or {})
        self.error_login = error_login
        self.error_constructor = error_constructor


class _ClienteFalso:
    """Cliente IMAP minimo con la superficie que usa el lector."""

    def __init__(self, config: _Config, host, port, timeout):
        self.config = config
        self.host = host
        self.port = port
        self.timeout = timeout
        self.carpetas_vistas = []
        self.logout_llamado = False
        self.shutdown_llamado = False
        self.usuario = ""
        self.password = ""

    def login(self, usuario, password):
        self.usuario = usuario
        self.password = password
        if self.config.error_login is not None:
            raise self.config.error_login
        if (usuario, password) != (self.config.usuario, self.config.clave):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return ("OK", [b"LOGIN completed"])

    def list(self, *args, **kwargs):
        return (
            "OK",
            [f'(\\HasNoChildren) "/" "{nombre}"'.encode("utf-8") for nombre in self.config.carpetas],
        )

    def select(self, carpeta, readonly=False):
        self.carpetas_vistas.append(carpeta)
        if carpeta not in self.config.mensajes:
            return ("NO", [b"No such mailbox"])
        return ("OK", [str(len(self.config.mensajes[carpeta])).encode()])

    def uid(self, comando, *args):
        carpeta = self.carpetas_vistas[-1] if self.carpetas_vistas else "INBOX"
        if comando == "search":
            mensajes = self.config.mensajes.get(carpeta, [])
            return ("OK", [b" ".join(str(m.uid).encode() for m in mensajes)])
        if comando == "fetch":
            uid = int(args[0])
            for mensaje in self.config.mensajes.get(carpeta, []):
                if mensaje.uid == uid:
                    meta = b"%d (UID %d BODY[] {%d}" % (uid, uid, len(mensaje.crudo))
                    return ("OK", [(meta, mensaje.crudo), b")"])
            return ("OK", [None])
        return ("NO", [b"no soportado"])

    def logout(self):
        self.logout_llamado = True
        return ("BYE", [b"logout"])

    def shutdown(self):
        self.shutdown_llamado = True


class _IMAPFalso:
    """Context manager que reemplaza `imaplib.IMAP4_SSL` por el IMAP falso."""

    def __init__(self, config: _Config):
        self.config = config
        self.instancias = []
        self._original = None

    def __enter__(self):
        self._original = imaplib.IMAP4_SSL
        contenedor = self

        def fabrica(host, port=993, timeout=None):
            if contenedor.config.error_constructor is not None:
                raise contenedor.config.error_constructor
            instancia = _ClienteFalso(contenedor.config, host, port, timeout)
            contenedor.instancias.append(instancia)
            return instancia

        imaplib.IMAP4_SSL = fabrica
        return self

    def __exit__(self, *exc):
        imaplib.IMAP4_SSL = self._original
        return False

    @property
    def ultima(self):
        return self.instancias[-1] if self.instancias else None


def _correo(remitente, asunto, cuerpo, fecha="Tue, 23 Sep 2026 10:00:00 +0000", html=False):
    """Construye un correo RFC822 real (EmailMessage) para el IMAP falso."""
    mensaje = EmailMessage()
    mensaje["From"] = remitente
    mensaje["Subject"] = asunto
    mensaje["Date"] = fecha
    if html:
        mensaje.set_content(cuerpo, subtype="html")
    else:
        mensaje.set_content(cuerpo)
    return mensaje.as_bytes()


def _config_con_inbox(mensajes_inbox, carpetas=None, mensajes_extra=None):
    """Config con los mensajes dados en INBOX (+ carpetas adicionales)."""
    mensajes = {"INBOX": list(mensajes_inbox)}
    if mensajes_extra:
        mensajes.update(mensajes_extra)
    return _Config(carpetas=carpetas or ["INBOX"], mensajes=mensajes)


# --------------------------------------------------------------------------- #
# (1) Extraccion pura
# --------------------------------------------------------------------------- #
def _test_extraccion(check):
    check(
        "extraccion: codigo espanol junto a 'codigo de verificacion'",
        extraer_codigo_de_texto("Tu código de verificación es 123456") == "123456",
    )
    check(
        "extraccion: codigo ingles 'confirmation code'",
        extraer_codigo_de_texto("Your confirmation code: 987654") == "987654",
    )
    check(
        "extraccion: codigo sin palabras clave tambien se encuentra",
        extraer_codigo_de_texto("Ref interna 345678") == "345678",
    )
    check(
        "extraccion: HTML con etiquetas alrededor del codigo",
        extraer_codigo_de_texto(
            "<html><body><p>Tu <b>código</b> de verificación es "
            "<span>112233</span></p></body></html>"
        )
        == "112233",
    )
    check(
        "extraccion: entidades HTML numericas se desescapan",
        extraer_codigo_de_texto("C&#243;digo de verificaci&#243;n: 445566") == "445566",
    )
    check(
        "extraccion: &nbsp; entre palabras no rompe la deteccion",
        extraer_codigo_de_texto("Código&nbsp;de&nbsp;verificación:&nbsp;778899") == "778899",
    )
    check(
        "extraccion: prioriza el codigo pegado a 'codigo de verificacion'",
        extraer_codigo_de_texto(
            "Número de pedido: 111111. Más abajo: tu código de verificación "
            "es 222222, úsalo pronto."
        )
        == "222222",
    )
    check(
        "extraccion: un id lejano a las palabras clave pierde contra el codigo",
        extraer_codigo_de_texto(
            "Gracias por tu compra. Reporte de envío 111111 disponible en tu "
            "cuenta para consultarlo. Tu código de verificación es 222222."
        )
        == "222222",
    )
    check(
        "extraccion: telefono con guiones NO se toma como codigo",
        extraer_codigo_de_texto("Llama al (555) 123-4567 para soporte") == "",
    )
    check(
        "extraccion: telefono separado por espacios NO se toma como codigo",
        extraer_codigo_de_texto("Llama al 555 123 4567 para soporte") == "",
    )
    check(
        "extraccion: id largo (10 digitos) NO se toma como codigo",
        extraer_codigo_de_texto("ID 1234567890") == "",
    )
    check(
        "extraccion: color hex #123456 NO se toma como codigo",
        extraer_codigo_de_texto("color #123456 de fondo") == "",
    )
    check(
        "extraccion: precio $123456 NO se toma como codigo",
        extraer_codigo_de_texto("pago $123456 hoy") == "",
    )
    check(
        "extraccion: 5 digitos NO alcanzan",
        extraer_codigo_de_texto("pedido 12345") == "",
    )
    check(
        "extraccion: '123 456' con contexto de verificacion se une",
        extraer_codigo_de_texto("Tu código es 123 456") == "123456",
    )
    check(
        "extraccion: '123 456' sin contexto NO se toma como codigo",
        extraer_codigo_de_texto("llegada 123 456") == "",
    )
    check(
        "extraccion: bytes utf-8",
        extraer_codigo_de_texto(b"code 654321") == "654321",
    )
    check(
        "extraccion: vacio/None/objeto raro devuelven '' sin lanzar",
        extraer_codigo_de_texto("") == ""
        and extraer_codigo_de_texto(None) == ""
        and extraer_codigo_de_texto(object()) == "",
    )
    check(
        "extraccion: texto sin numeros devuelve ''",
        extraer_codigo_de_texto("Hola, ¿cómo estás?") == "",
    )
    check(
        "extraccion: no parte un codigo de 8 digitos en sub-cadenas",
        extraer_codigo_de_texto("cuenta 12345678") == "",
    )


# --------------------------------------------------------------------------- #
# (2) obtener_codigo_verificacion con IMAP falso
# --------------------------------------------------------------------------- #
def _test_obtener(check):
    # -- Happy path simple -------------------------------------------------- #
    inbox = [
        _Mensaje(
            1,
            _correo(
                "noreply@ejemplo.com",
                "Hola",
                "Tu código de verificación es 123456",
            ),
        )
    ]
    with _IMAPFalso(_config_con_inbox(inbox)) as falso:
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: happy path devuelve ok y el codigo", resultado["ok"] and resultado["codigo"] == "123456")
    check(
        "obtener: incluye remitente/asunto/fecha del correo elegido",
        resultado["remitente"] == "noreply@ejemplo.com"
        and resultado["asunto"] == "Hola"
        and bool(resultado["fecha"]),
    )
    check("obtener: error vacio cuando encuentra codigo", resultado["error"] == "")
    check("obtener: claves EXACTAS del contrato", set(resultado.keys()) == CLAVES_CONTRATO == set(resultado))
    check(
        "obtener: timeout de socket pasado al IMAP (default 25)",
        falso.ultima is not None and falso.ultima.timeout == 25,
    )
    check(
        "obtener: usa el host por defecto de Yahoo",
        falso.ultima is not None and falso.ultima.host == "imap.mail.yahoo.com",
    )
    check("obtener: cierra la conexion (logout)", falso.ultima is not None and falso.ultima.logout_llamado)

    # -- Preferencia de remitente X/Twitter --------------------------------- #
    inbox = [
        _Mensaje(1, _correo("info@x.com", "Your X confirmation code", "Code: 222222")),
        _Mensaje(2, _correo("novedades@tienda.com", "Pedido", "Tu código es 111111")),
    ]
    with _IMAPFalso(_config_con_inbox(inbox)) as falso:
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check(
        "obtener: prefiere el correo de X aunque sea mas antiguo",
        resultado["ok"] and resultado["codigo"] == "222222",
    )
    check(
        "obtener: reporta el remitente elegido",
        resultado["remitente"] == "info@x.com",
    )

    # -- Empate: gana el mas reciente (UID mayor) --------------------------- #
    inbox = [
        _Mensaje(1, _correo("noreply@ejemplo.com", "Hola", "Tu código es 111111")),
        _Mensaje(2, _correo("noreply@ejemplo.com", "Hola", "Tu código es 222222")),
    ]
    with _IMAPFalso(_config_con_inbox(inbox)):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: con empate gana el correo mas reciente", resultado["codigo"] == "222222")

    # -- Asunto con el codigo vs cuerpo con otro numero --------------------- #
    inbox = [
        _Mensaje(
            1,
            _correo("noreply@tienda.com", "Tu código de confirmación: 555555", "Pedido 444444"),
        )
    ]
    with _IMAPFalso(_config_con_inbox(inbox)):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: el asunto compite con el cuerpo y gana el de la clave", resultado["codigo"] == "555555")

    # -- Sin codigos -------------------------------------------------------- #
    inbox = [_Mensaje(1, _correo("amigo@ejemplo.com", "Saludos", "Nos vemos mañana, saludos 12"))]
    with _IMAPFalso(_config_con_inbox(inbox)):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: sin codigos devuelve ok=False", resultado["ok"] is False)
    check(
        "obtener: error claro de 'no encontre ningun codigo'",
        "no encontré ningún código" in resultado["error"],
    )
    check("obtener: contrato exacto tambien al fallar", set(resultado.keys()) == CLAVES_CONTRATO)

    # -- INBOX sin codigo -> revisa Spam/Bulk ------------------------------- #
    inbox = [_Mensaje(5, _correo("amigo@ejemplo.com", "Saludos", "Sin novedad"))]
    bulk = [_Mensaje(9, _correo("verify@x.com", "Tu codigo", "Tu código de verificación: 999888"))]
    config = _config_con_inbox(inbox, carpetas=["INBOX", "Bulk Mail"], mensajes_extra={"Bulk Mail": bulk})
    with _IMAPFalso(config) as falso:
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: si INBOX no tiene codigo, busca en Bulk/Spam", resultado["ok"] and resultado["codigo"] == "999888")
    check("obtener: la carpeta de spam fue visitada despues de INBOX", "Bulk Mail" in falso.ultima.carpetas_vistas)

    # -- Si INBOX ya tiene codigo, no visita Spam --------------------------- #
    inbox = [_Mensaje(5, _correo("noreply@ejemplo.com", "Tu codigo", "Tu código es 101010"))]
    bulk = [_Mensaje(9, _correo("verify@x.com", "Otro", "Tu código es 202020"))]
    config = _config_con_inbox(inbox, carpetas=["INBOX", "Bulk Mail"], mensajes_extra={"Bulk Mail": bulk})
    with _IMAPFalso(config) as falso:
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: con codigo en INBOX no visita Spam", resultado["codigo"] == "101010" and "Bulk Mail" not in falso.ultima.carpetas_vistas)

    # -- HTML real via email module ----------------------------------------- #
    inbox = [
        _Mensaje(
            1,
            _correo(
                "info@x.com",
                "Confirma tu correo",
                "<html><body><p>Tu código de verificación es <b>556677</b></p></body></html>",
                html=True,
            ),
        )
    ]
    with _IMAPFalso(_config_con_inbox(inbox)):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: correo HTML real (parsed por email) encuentra el codigo", resultado["codigo"] == "556677")

    # -- Asunto RFC2047 con acentos ----------------------------------------- #
    inbox = [
        _Mensaje(
            1,
            _correo("info@x.com", "Tu código de verificación", "El código es 667788"),
        )
    ]
    with _IMAPFalso(_config_con_inbox(inbox)):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check(
        "obtener: asunto RFC2047 se decodifica a texto legible",
        resultado["codigo"] == "667788" and "código" in resultado["asunto"],
    )

    # -- Credenciales malas -------------------------------------------------- #
    config = _Config(usuario=USUARIO, clave=CLAVE)
    with _IMAPFalso(config):
        resultado = obtener_codigo_verificacion(USUARIO, "clave-incorrecta-xyz")
    check("obtener: credenciales malas devuelven ok=False", resultado["ok"] is False)
    check(
        "obtener: error explica credenciales y contrasena de aplicacion",
        "contraseña" in resultado["error"] and "aplicación" in resultado["error"],
    )
    check(
        "obtener: la contrasena NUNCA aparece en la respuesta",
        "clave-incorrecta-xyz" not in str(resultado),
    )
    check("obtener: contrato en credenciales malas", set(resultado.keys()) == CLAVES_CONTRATO)

    # -- Excepciones raras: nunca lanza ------------------------------------- #
    config = _Config(error_login=RuntimeError(f"fallo interno con {CLAVE}"))
    with _IMAPFalso(config):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check("obtener: excepcion inesperada no lanza y devuelve contrato", set(resultado.keys()) == CLAVES_CONTRATO)
    check("obtener: mensaje de excepcion saneado (sin contrasena)", CLAVE not in str(resultado))

    config = _Config(error_constructor=OSError("connection refused"))
    with _IMAPFalso(config):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check(
        "obtener: fallo de conexion da error descriptivo",
        resultado["ok"] is False and "no pude conectar" in resultado["error"],
    )

    config = _Config(error_login=socket.timeout("timed out"))
    with _IMAPFalso(config):
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check(
        "obtener: timeout da error de tiempo agotado",
        resultado["ok"] is False and "tiempo" in resultado["error"],
    )

    # -- email/clave faltantes ----------------------------------------------
    resultado = obtener_codigo_verificacion("", "")
    check(
        "obtener: sin correo/clave devuelve error claro sin lanzar",
        resultado["ok"] is False and "Falta el correo" in resultado["error"],
    )

    # -- Host configurable por entorno --------------------------------------
    original = os.environ.get("LECTOR_CORREO_HOST")
    try:
        os.environ["LECTOR_CORREO_HOST"] = "imap.ejemplo.test"
        with _IMAPFalso(_config_con_inbox([])) as falso:
            obtener_codigo_verificacion(USUARIO, CLAVE)
        check(
            "obtener: LECTOR_CORREO_HOST cambia el servidor",
            falso.ultima is not None and falso.ultima.host == "imap.ejemplo.test",
        )
    finally:
        if original is None:
            os.environ.pop("LECTOR_CORREO_HOST", None)
        else:
            os.environ["LECTOR_CORREO_HOST"] = original

    # -- Timeout personalizado ----------------------------------------------
    with _IMAPFalso(_config_con_inbox([])) as falso:
        obtener_codigo_verificacion(USUARIO, CLAVE, timeout=7)
    check(
        "obtener: timeout personalizado se pasa al socket IMAP",
        falso.ultima is not None and falso.ultima.timeout == 7,
    )

    # -- Carpetas de spam por alias cuando LIST no las muestra --------------
    inbox = [_Mensaje(1, _correo("amigo@ejemplo.com", "Hola", "Sin codigos"))]
    spam = [_Mensaje(2, _correo("verify@x.com", "Codigo", "Tu código es 333444"))]
    config = _Config(
        carpetas=["INBOX", "ARCHIVO"],
        mensajes={"INBOX": inbox, "Spam": spam},
    )
    with _IMAPFalso(config) as falso:
        resultado = obtener_codigo_verificacion(USUARIO, CLAVE)
    check(
        "obtener: si LIST no muestra Spam, prueba el alias directamente",
        resultado["ok"] and resultado["codigo"] == "333444",
    )
    check("obtener: el alias 'Spam' quedo en las carpetas revisadas", "Spam" in falso.ultima.carpetas_vistas)


# --------------------------------------------------------------------------- #
# (3) diagnosticar_conexion
# --------------------------------------------------------------------------- #
def _test_diagnostico(check):
    inbox = [
        _Mensaje(1, _correo("a@b.com", "Uno", "hola")),
        _Mensaje(2, _correo("a@b.com", "Dos", "adios")),
    ]
    config = _Config(carpetas=["INBOX", "Bulk Mail"], mensajes={"INBOX": inbox})
    with _IMAPFalso(config):
        resultado = diagnosticar_conexion(USUARIO, CLAVE)
    check("diagnostico: login OK con ok=True", resultado["ok"] is True)
    check("diagnostico: claves exactas del helper", set(resultado.keys()) == CLAVES_DIAGNOSTICO)
    check(
        "diagnostico: lista carpetas del servidor",
        "INBOX" in resultado["carpetas"] and "Bulk Mail" in resultado["carpetas"],
    )
    check("diagnostico: cuenta los mensajes de INBOX", resultado["mensajes"] == 2)
    check("diagnostico: tiempo_seg es numerico", isinstance(resultado["tiempo_seg"], float))
    check("diagnostico: error vacio cuando todo va bien", resultado["error"] == "")

    with _IMAPFalso(_Config(usuario=USUARIO, clave=CLAVE)):
        resultado = diagnosticar_conexion(USUARIO, "otra-clave-zzz")
    check("diagnostico: credenciales malas -> ok=False", resultado["ok"] is False)
    check(
        "diagnostico: error de credenciales sin contrasena",
        "contraseña" in resultado["error"] and "otra-clave-zzz" not in str(resultado),
    )

    with _IMAPFalso(_Config(error_login=RuntimeError(f"boom {CLAVE}"))):
        resultado = diagnosticar_conexion(USUARIO, CLAVE)
    check("diagnostico: excepcion inesperada no lanza", set(resultado.keys()) == CLAVES_DIAGNOSTICO)
    check("diagnostico: error saneado sin contrasena", CLAVE not in str(resultado))

    with _IMAPFalso(_Config(error_constructor=socket.gaierror("dns fail"))):
        resultado = diagnosticar_conexion(USUARIO, CLAVE)
    check(
        "diagnostico: fallo DNS da error de conexion",
        resultado["ok"] is False and "no pude conectar" in resultado["error"],
    )

    resultado = diagnosticar_conexion("", "")
    check(
        "diagnostico: sin credenciales devuelve error claro",
        resultado["ok"] is False and "Falta el correo" in resultado["error"],
    )


def run(check):
    """Ejecuta todos los checks del lector de correo."""
    _test_extraccion(check)
    _test_obtener(check)
    _test_diagnostico(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_lector_correo.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

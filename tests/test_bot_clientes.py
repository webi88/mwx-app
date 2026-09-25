# -*- coding: utf-8 -*-
"""Tests del bot de clientes (`bot_clientes/`) sin Telegram real ni Chrome.

Modelo actual: el bot funciona SOLO en el grupo de clientes
(`TELEGRAM_CLIENTES_CHAT_ID`, por defecto -1005538610567) y las cuentas son
GLOBALES del grupo (`data/clientes_bot.json`).

Cubre:
  - `clientes_store`: estructura nueva `{"chat_id", "cuentas"}`, migracion del
    formato viejo `{"clientes": {...}}` (cuentas unicas + IDs descartados),
    chat_id por JSON/env/default, `es_chat_permitido`, normalizacion y
    escritura atomica (siempre en archivo temporal).
  - `keyboards`: botones/callbacks esperados del menu (`cli_cuentas`) y
    confirmaciones.
  - Validacion del nombre nuevo y formateo del codigo 2FA (SOLO TOTP: semilla
    fake -> 6 digitos; sin semilla -> mensaje amable) y que el flujo YA NO
    depende de `utils.lector_correo`.
  - Guard por CHAT: privado -> mensaje corto; otro grupo -> silencio; grupo
    correcto -> sigue. Flujos con fakes PTB (codigo, nombre, foto, grupo,
    aislamiento por usuario) y admin `/nombre` (BD fake).
  - `import bot_clientes.main` no arranca nada y `main()` exige token.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_bot_clientes.py
"""
from __future__ import annotations

import asyncio
import ast
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from bot_clientes import clientes_store  # noqa: E402
from bot_clientes import handlers as h  # noqa: E402
from bot_clientes import keyboards as k  # noqa: E402

CHAT_GRUPO = -1005538610567
CHAT_OTRO = -1009999999999
SECRETO_FAKE = "JBSWY3DPEHPK3PXP"
CUENTAS_15 = [
    "3ranuii97",
    "SoLikeShady",
    "Samia_lovesyou",
    "LuxuryMachine",
    "PinkLipStick16",
    "BAMsugar96",
    "HardisonRichard",
    "yungbillyz",
    "sir_portugal",
    "kristy63411720",
    "gadams_gene",
    "estybaby8",
    "_Campos_7",
    "BuchholzLacey",
    "Kaitlyn26014743",
]


# --------------------------------------------------------------------------- #
# Helpers de entorno y fakes PTB
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def _store_temporal():
    """store -> archivo JSON temporal; restaura la ruta real al salir."""
    with tempfile.TemporaryDirectory() as carpeta:
        ruta = os.path.join(carpeta, "clientes_bot.json")
        anterior = getattr(clientes_store, "_RUTA_OVERRIDE", "")
        clientes_store.usar_ruta(ruta)
        try:
            yield ruta
        finally:
            clientes_store.usar_ruta(anterior)


@contextlib.contextmanager
def _env(nombre, valor):
    """Fija/quita una variable de entorno y la restaura al salir."""
    previo = os.environ.get(nombre)
    if valor is None:
        os.environ.pop(nombre, None)
    else:
        os.environ[nombre] = str(valor)
    try:
        yield
    finally:
        if previo is None:
            os.environ.pop(nombre, None)
        else:
            os.environ[nombre] = previo


@contextlib.contextmanager
def _parches(*cambios):
    """Aplica `(objeto, nombre, valor)` y restaura SIEMPRE al salir."""
    originales = []
    try:
        for objeto, nombre, valor in cambios:
            originales.append((objeto, nombre, getattr(objeto, nombre)))
            setattr(objeto, nombre, valor)
        yield
    finally:
        for objeto, nombre, valor in reversed(originales):
            setattr(objeto, nombre, valor)


class _UsuarioFake:
    def __init__(self, uid, username=None, first_name="Persona"):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.full_name = first_name


class _ChatFake:
    def __init__(self, chat_id=1, tipo="private"):
        self.id = chat_id
        self.type = tipo


def _chat_grupo(chat_id=CHAT_GRUPO):
    return _ChatFake(chat_id, "supergroup")


class _BotFake:
    """`context.bot` minimo (id del bot + envio directo al chat)."""

    def __init__(self, bot_id=424242):
        self.id = bot_id
        self.enviados = []

    async def send_message(self, chat_id, text):
        self.enviados.append((chat_id, text))
        return None


class _MiembroFake:
    def __init__(self, status):
        self.status = status


class _CambioMiembroFake:
    """Fake de `ChatMemberUpdated` (old/new member) para my_chat_member."""

    def __init__(self, viejo, nuevo):
        self.old_chat_member = _MiembroFake(viejo)
        self.new_chat_member = _MiembroFake(nuevo)


class _FotoFake:
    def __init__(self, file_id="file_id_foto"):
        self.file_id = file_id

    async def get_file(self):
        return _ArchivoFake()


class _ArchivoFake:
    async def download_to_drive(self, ruta):
        with open(ruta, "wb") as fh:
            fh.write(b"jpeg-fake")


class _MensajeFake:
    def __init__(self, texto="", fotos=None):
        self.text = texto
        self.photo = list(fotos or [])
        self.new_chat_members = []
        self.replies = []
        self.edits = []
        self.photos = []

    async def reply_text(self, texto, **kwargs):
        self.replies.append((texto, kwargs))
        return self

    async def edit_text(self, texto, **kwargs):
        self.edits.append((texto, kwargs))
        return self

    async def reply_photo(self, photo, caption="", **kwargs):
        self.photos.append((photo, caption, kwargs))
        return self


class _QueryFake:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or _MensajeFake()
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, bool(show_alert)))

    async def edit_message_text(self, texto, **kwargs):
        self.message.edits.append((texto, kwargs))
        return self.message


class _UpdateFake:
    def __init__(self, uid=None, texto="", fotos=None, query=None, chat=None, username=None):
        self.effective_user = (
            _UsuarioFake(uid, username=username) if uid is not None else None
        )
        self.effective_chat = chat or _ChatFake(1, "private")
        self.effective_message = _MensajeFake(texto, fotos)
        self.callback_query = query


class _ContextoFake:
    def __init__(self, args=None):
        self.args = list(args or [])
        self.user_data = {}
        self.bot = _BotFake()


def _correr(coro):
    return asyncio.run(coro)


def _callbacks(markup) -> list:
    """Lista plana de callback_data de un InlineKeyboardMarkup."""
    if markup is None:
        return []
    filas = getattr(markup, "inline_keyboard", None) or []
    return [boton.callback_data for fila in filas for boton in fila]


class _CuentaBDFake:
    """Fila minima de `Cuenta` para la sesion fake de /nombre."""

    def __init__(self, usuario="MiCuenta", nombre_mostrado=""):
        self.usuario = usuario
        self.nombre_mostrado = nombre_mostrado


class _QueryBDFake:
    def __init__(self, cuenta):
        self._cuenta = cuenta

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._cuenta


class _SesionBDFake:
    def __init__(self, cuenta):
        self._cuenta = cuenta

    def query(self, modelo):
        return _QueryBDFake(self._cuenta)


@contextlib.contextmanager
def _db_fake(cuenta):
    """Sustituye `get_db_session()` por una sesion fake con `cuenta`."""
    import core.database as core_database

    with _parches((core_database, "get_db_session", lambda: _db_fake_ctx(cuenta))):
        yield


@contextlib.contextmanager
def _db_fake_ctx(cuenta):
    yield _SesionBDFake(cuenta)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def run(check) -> None:  # noqa: C901 - seccionado por bloques tematicos
    # ---------------------------------------------------------------- STORE --
    with _store_temporal() as ruta, _env("TELEGRAM_CLIENTES_CHAT_ID", None):
        carpeta = os.path.dirname(ruta)
        datos = clientes_store.cargar(ruta)
        check(
            "store: cargar crea el archivo con chat + 15 cuentas por defecto",
            os.path.isfile(ruta)
            and datos["chat_id"] == CHAT_GRUPO
            and datos["cuentas"] == CUENTAS_15,
        )
        check(
            "store: cuentas() devuelve las 15 globales",
            clientes_store.cuentas(ruta) == CUENTAS_15,
        )
        check(
            "store: chat_id() usa el JSON (default del grupo)",
            clientes_store.chat_id(ruta) == CHAT_GRUPO,
        )
        check(
            "store: es_chat_permitido solo con el grupo configurado",
            clientes_store.es_chat_permitido(CHAT_GRUPO, ruta)
            and clientes_store.es_chat_permitido(str(CHAT_GRUPO), ruta)
            and not clientes_store.es_chat_permitido(CHAT_OTRO, ruta)
            and not clientes_store.es_chat_permitido(123, ruta)
            and not clientes_store.es_chat_permitido(None, ruta),
        )
        error = clientes_store.guardar(
            {"chat_id": CHAT_OTRO, "cuentas": ["@Uno", " dos ", "DOS", ""]}, ruta
        )
        check(
            "store: guardar normaliza chat y cuentas (sin '@', sin duplicados)",
            error == ""
            and clientes_store.chat_id(ruta) == CHAT_OTRO
            and clientes_store.cuentas(ruta) == ["Uno", "dos"],
        )
        clientes_store.guardar(
            {"chat_id": CHAT_GRUPO, "cuentas": "CuentaSuelta"}, ruta
        )
        check(
            "store: un string suelto se trata como UNA cuenta",
            clientes_store.cuentas(ruta) == ["CuentaSuelta"],
        )

        # Migracion del formato viejo (registro por Telegram ID).
        viejo = {
            "clientes": {
                "111": {"nombre": "Cliente A", "cuentas": ["uno", "dos"]},
                "222": {"nombre": "Cliente B", "cuentas": ["DOS", "tres"]},
                "333": {"nombre": "Cliente C", "cuentas": ["@cuatro"]},
            }
        }
        with open(ruta, "w", encoding="utf-8") as fh:
            json.dump(viejo, fh)
        migrado = clientes_store.cargar(ruta)
        check(
            "store: migra el formato viejo a cuentas unicas + chat",
            migrado["chat_id"] == CHAT_GRUPO
            and migrado["cuentas"] == ["uno", "dos", "tres", "cuatro"],
        )
        with open(ruta, "r", encoding="utf-8") as fh:
            en_disco = json.load(fh)
        check(
            "store: la migracion se reescribe en disco (sin 'clientes' ni IDs)",
            "clientes" not in en_disco
            and en_disco["chat_id"] == CHAT_GRUPO
            and en_disco["cuentas"] == ["uno", "dos", "tres", "cuatro"],
        )

        # JSON corrupto: nunca lanza, devuelve estructura valida.
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write("{esto no es json!!")
        recuperado = clientes_store.cargar(ruta)
        check(
            "store: JSON corrupto no lanza y devuelve estructura valida",
            recuperado["chat_id"] == CHAT_GRUPO
            and recuperado["cuentas"] == CUENTAS_15,
        )
        check(
            "store: guardar con datos invalidos normaliza (nunca lanza)",
            clientes_store.guardar("no-dict", ruta) == ""
            and clientes_store.cargar(ruta)["chat_id"] == CHAT_GRUPO,
        )
        sobrantes = [n for n in os.listdir(carpeta) if n.endswith(".tmp")]
        with open(ruta, encoding="utf-8") as fh:
            contenido = json.load(fh)
        check(
            "store: guardar es atomico (sin .tmp y JSON valido)",
            not sobrantes and "cuentas" in contenido,
        )
        with _env("TELEGRAM_CLIENTES_CHAT_ID", "-1005555555555"):
            check(
                "store: el env TELEGRAM_CLIENTES_CHAT_ID manda sobre el JSON",
                clientes_store.chat_id(ruta) == -1005555555555
                and clientes_store.es_chat_permitido(-1005555555555, ruta)
                and not clientes_store.es_chat_permitido(CHAT_GRUPO, ruta),
            )

    # ------------------------------------------------------------ KEYBOARDS --
    menu_completo = k.menu_principal(completo=True)
    callbacks_menu = _callbacks(menu_completo)
    check(
        "teclados: el menu completo (privado admin) tiene las 6 opciones",
        callbacks_menu
        == [
            "cli_codigo",
            "cli_nombre",
            "cli_foto_perfil",
            "cli_foto_portada",
            "cli_cuentas",
            "cli_ayuda",
        ],
    )
    check(
        "teclados: el menu del GRUPO es reducido (solo codigo + ayuda)",
        _callbacks(k.menu_principal(completo=False)) == ["cli_codigo", "cli_ayuda"]
        and _callbacks(k.menu_principal()) == callbacks_menu,  # default = completo
    )
    check(
        "teclados: selector de cuenta tiene un boton por cuenta",
        _callbacks(k.teclado_cuentas(["Uno", "Dos"]))
        == ["cuenta_Uno", "cuenta_Dos", "cli_menu"],
    )
    check(
        "teclados: acciones por cuenta incluye los 4 flujos",
        set(
            ["codigo_X", "nombre_X", "foto_perfil_X", "foto_portada_X"]
        ).issubset(set(_callbacks(k.teclado_acciones_cuenta("X")))),
    )
    check(
        "teclados: la lista de cuentas usa cli_cuenta_<usuario>",
        _callbacks(k.teclado_lista_cuentas(["Uno"]))
        == ["cli_cuenta_Uno", "cli_menu"],
    )
    check(
        "teclados: confirmacion de nombre con si/no",
        _callbacks(k.teclado_confirmar_nombre("X")) == ["nombre_si_X", "nombre_no_X"],
    )
    check(
        "teclados: confirmacion de foto con si/otra/no",
        _callbacks(k.teclado_confirmar_foto("portada", "X"))
        == ["foto_si_portada_X", "foto_otra_portada_X", "foto_no_portada_X"],
    )
    check(
        "teclados: codigo ofrece 'otro codigo' y menu",
        _callbacks(k.teclado_codigo("X")) == ["codigo_X", "cli_menu"],
    )
    check(
        "teclados: cancelar usa cli_cancelar",
        _callbacks(k.teclado_cancelar()) == ["cli_cancelar"],
    )

    # ------------------------------------------------------ VALIDAR NOMBRE --
    ok, nombre, error = h.validar_nombre("  María   López ")
    check(
        "nombre: acepta nombre normal y normaliza espacios",
        ok and nombre == "María López" and error == "",
    )
    ok, nombre, _ = h.validar_nombre("José Luis Pérez-Hernández")
    check("nombre: acepta acentos y guiones", ok and nombre == "José Luis Pérez-Hernández")
    for malo, motivo in (
        ("", "vacio"),
        ("A", "1 letra"),
        ("x" * 51, "51 chars"),
        ("Visita http://spam.com", "URL"),
        ("@mari", "@"),
        ("María 😀", "emoji"),
        ("12345", "solo digitos"),
    ):
        ok, _, mensaje = h.validar_nombre(malo)
        check(f"nombre: rechaza {motivo}", not ok and bool(mensaje))

    # ----------------------------------------------------------------- TOTP --
    codigo, error = h.generar_codigo_totp(SECRETO_FAKE)
    try:
        import pyotp

        verifica = pyotp.TOTP(SECRETO_FAKE).verify(codigo, valid_window=1)
    except Exception:
        verifica = False
    check(
        "codigo: TOTP de 6 digitos con semilla fake",
        len(codigo) == 6 and codigo.isdigit() and error == "" and verifica,
    )
    codigo_espacios, _ = h.generar_codigo_totp("  JBSW Y3DP EHPK 3PXP ")
    check(
        "codigo: TOTP limpia espacios de la semilla",
        len(codigo_espacios) == 6 and codigo_espacios.isdigit(),
    )
    codigo_malo, error_malo = h.generar_codigo_totp("no-es-base32!!")
    check(
        "codigo: semilla invalida devuelve error (nunca lanza)",
        codigo_malo == "" and bool(error_malo),
    )
    check(
        "codigo: segundos restantes de la ventana en 1..30",
        1 <= h.segundos_restantes_ventana() <= 30,
    )
    texto_totp = h.texto_codigo("123456", 12)
    check(
        "codigo: texto TOTP trae codigo, vencimiento y aviso de no compartir",
        "123456" in texto_totp
        and "Vence en ~12" in texto_totp
        and "compartas" in texto_totp
        and "usuario" in texto_totp,
    )
    check(
        "codigo: el mensaje ya NO menciona correo (solo TOTP)",
        "correo" not in texto_totp.lower()
        and "correo" not in h.TEXTO_SIN_TOTP.lower(),
    )

    # --------------------------------- CODIGO SOLO TOTP (SIN LECTOR CORREO) --
    fuente_handlers = (RAIZ / "bot_clientes" / "handlers.py").read_text(encoding="utf-8")
    arbol_handlers = ast.parse(fuente_handlers)
    modulos_importados = set()
    funciones_definidas = set()
    for nodo in ast.walk(arbol_handlers):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            modulos_importados.add(nodo.module)
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                modulos_importados.add(alias.name)
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funciones_definidas.add(nodo.name)
    check(
        "codigo: el flujo YA NO importa ni define el lector de correo",
        "utils.lector_correo" not in modulos_importados
        and "codigo_desde_correo" not in funciones_definidas
        and "_importar_lector_correo" not in funciones_definidas
        and "_llamar_lector" not in funciones_definidas,
    )
    check(
        "codigo: el bot ya no lee email_password para dar codigos",
        "email_password" not in fuente_handlers
        and not hasattr(h, "codigo_desde_correo")
        and not hasattr(h, "TIEMPO_CORREO"),
    )

    # ------------------------------------- MATRIZ DE ACCESO / START ------
    with _store_temporal() as ruta, _env("TELEGRAM_CLIENTES_CHAT_ID", None), _env(
        "TELEGRAM_ADMIN_IDS", "555"
    ):
        # Privado NO admin: mensaje claro y sin menu.
        update = _UpdateFake(uid=777, texto="/start")
        _correr(h.start(update, _ContextoFake()))
        check(
            "guard: privado ajeno recibe el mensaje claro (sin menu)",
            update.effective_message.replies
            and h.TEXTO_SOLO_ADMIN_PRIVADO in update.effective_message.replies[0][0]
            and not update.effective_message.replies[0][1],
        )
        # Callback privado ajeno: alerta corta, sin editar nada.
        query = _QueryFake("cli_menu")
        _correr(h.cli_callback(_UpdateFake(uid=777, query=query), _ContextoFake()))
        check(
            "guard: callback privado ajeno responde alerta corta",
            query.answers
            and query.answers[-1][1]
            and h.TEXTO_SOLO_ADMIN_PRIVADO in query.answers[-1][0]
            and not query.message.edits,
        )
        # Privado del ADMIN: menu COMPLETO.
        update = _UpdateFake(uid=555, texto="/start")
        _correr(h.start(update, _ContextoFake()))
        texto, kwargs = update.effective_message.replies[0]
        check(
            "guard: privado del ADMIN recibe el menu completo",
            set(
                [
                    "cli_codigo",
                    "cli_nombre",
                    "cli_foto_perfil",
                    "cli_foto_portada",
                    "cli_cuentas",
                    "cli_ayuda",
                ]
            ).issubset(set(_callbacks(kwargs["reply_markup"])))
            and "Opciones:" in texto,
        )
        # Otro grupo: silencio absoluto (mensaje y callback).
        update = _UpdateFake(uid=500, texto="/start", chat=_chat_grupo(CHAT_OTRO))
        _correr(h.start(update, _ContextoFake()))
        check(
            "guard: otro grupo se ignora en silencio (nada de nada)",
            update.effective_message.replies == [],
        )
        query = _QueryFake("cli_menu")
        _correr(
            h.cli_callback(
                _UpdateFake(uid=500, query=query, chat=_chat_grupo(CHAT_OTRO)),
                _ContextoFake(),
            )
        )
        check(
            "guard: callback de otro grupo solo quita el spinner",
            not query.message.edits
            and query.answers
            and query.answers[-1] == ("", False),
        )
        # Grupo correcto: cualquier miembro, menu REDUCIDO (solo 2FA + ayuda).
        update = _UpdateFake(uid=500, texto="/start", chat=_chat_grupo(), username="fulano")
        _correr(h.start(update, _ContextoFake()))
        texto, kwargs = update.effective_message.replies[0]
        callbacks = _callbacks(kwargs["reply_markup"])
        check(
            "guard: el grupo recibe SOLO el menu 2FA (codigo + ayuda)",
            "15 cuentas disponibles" in texto
            and "código de verificación" in texto
            and "👤 @fulano," in texto
            and callbacks == ["cli_codigo", "cli_ayuda"],
        )
        check(
            "guard: en el grupo NO aparecen nombre/fotos/cuentas",
            not any(
                boton in callbacks
                for boton in ("cli_nombre", "cli_foto_perfil", "cli_foto_portada", "cli_cuentas")
            ),
        )
        # Defensa: boton viejo de cambiar nombre en el grupo -> rechazado.
        query = _QueryFake("cli_nombre")
        _correr(
            h.cli_callback(
                _UpdateFake(uid=500, query=query, chat=_chat_grupo(), username="fulano"),
                _ContextoFake(),
            )
        )
        check(
            "guard: boton de cambiar nombre en el grupo se rechaza sin ejecutar",
            not query.message.edits
            and query.answers
            and query.answers[-1][1]
            and h.TEXTO_FLUJO_SOLO_ADMIN in query.answers[-1][0],
        )
        # Defensa: flujo de nombre pendiente + seleccion de cuenta en grupo.
        contexto = _ContextoFake()
        contexto.user_data["cli_flujo"] = "nombre"
        query = _QueryFake("cuenta_3ranuii97")
        with _parches((h, "_datos_cuenta", lambda u: {"totp_secret": SECRETO_FAKE})):
            _correr(
                h.cuenta_callback(
                    _UpdateFake(uid=500, query=query, chat=_chat_grupo(), username="fulano"),
                    contexto,
                )
            )
        check(
            "guard: flujo de nombre pendiente no corre en el grupo (defensa)",
            contexto.user_data.get("cli_flujo") is None
            and not query.message.edits
            and query.answers[-1][1]
            and h.TEXTO_FLUJO_SOLO_ADMIN in query.answers[-1][0],
        )

    # ------------------------------------------------- FLUJO DE CODIGO ----
    with _store_temporal() as ruta, _env("TELEGRAM_CLIENTES_CHAT_ID", None), _env(
        "TELEGRAM_ADMIN_IDS", None
    ):
        def _datos_totp(usuario):
            return {
                "usuario": usuario,
                "handle_actual": "HandleReal",
                "nombre_mostrado": "",
                "totp_secret": SECRETO_FAKE,
            }

        # Con varias cuentas pregunta con cual (default: 15).
        query = _QueryFake("cli_codigo", _MensajeFake())
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(
                h.cli_callback(
                    _UpdateFake(uid=700, query=query, chat=_chat_grupo(), username="fulano"),
                    _ContextoFake(),
                )
            )
        ultimo, kwargs = query.message.edits[-1]
        check(
            "codigo: con varias cuentas pregunta con cual (15 botones)",
            "¿Con cuál cuenta" in ultimo
            and "cuenta_3ranuii97" in _callbacks(kwargs["reply_markup"])
            and "cuenta_Kaitlyn26014743" in _callbacks(kwargs["reply_markup"]),
        )

        # Una sola cuenta: va directo al TOTP.
        clientes_store.guardar({"chat_id": CHAT_GRUPO, "cuentas": ["SoloUno"]}, ruta)
        query = _QueryFake("cli_codigo", _MensajeFake())
        contexto = _ContextoFake()
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(
                h.cli_callback(
                    _UpdateFake(uid=700, query=query, chat=_chat_grupo(), username="fulano"),
                    contexto,
                )
            )
        ultimo, kwargs = query.message.edits[-1]
        seis = re.search(r"\b\d{6}\b", ultimo)
        check(
            "codigo: una sola cuenta entrega el TOTP directo (con HTML y boton)",
            seis is not None
            and "Este es tu código" in ultimo
            and kwargs.get("parse_mode") == "HTML"
            and "codigo_SoloUno" in _callbacks(kwargs["reply_markup"]),
        )
        check(
            "codigo: la semilla TOTP nunca se muestra (solo el codigo)",
            SECRETO_FAKE not in ultimo and "totp_secret" not in ultimo,
        )

        # Sin semilla: mensaje amable + boton de reintento (sin correo).
        def _datos_vacios(usuario):
            return {"totp_secret": "", "handle_actual": "", "nombre_mostrado": ""}

        query = _QueryFake("cli_codigo", _MensajeFake())
        with _parches((h, "_datos_cuenta", _datos_vacios)):
            _correr(
                h.cli_callback(
                    _UpdateFake(uid=700, query=query, chat=_chat_grupo(), username="fulano"),
                    _ContextoFake(),
                )
            )
        ultimo, kwargs = query.message.edits[-1]
        check(
            "codigo: sin semilla muestra el mensaje amable (sin correo)",
            "no tiene configurado el código 2FA" in ultimo
            and "Pide ayuda" in ultimo
            and "correo" not in ultimo.lower()
            and "codigo_SoloUno" in _callbacks(kwargs["reply_markup"]),
        )

        def _datos_malos(usuario):
            return {"totp_secret": "no-es-base32!!"}

        query = _QueryFake("cli_codigo", _MensajeFake())
        with _parches((h, "_datos_cuenta", _datos_malos)):
            _correr(
                h.cli_callback(
                    _UpdateFake(uid=700, query=query, chat=_chat_grupo(), username="fulano"),
                    _ContextoFake(),
                )
            )
        check(
            "codigo: semilla invalida tambien muestra el mensaje amable",
            "no tiene configurado el código 2FA" in query.message.edits[-1][0]
            and "no-es-base32" not in query.message.edits[-1][0],
        )

        # Flujo multi: elegir por callback y regenerar con "otro codigo".
        clientes_store.guardar(
            {"chat_id": CHAT_GRUPO, "cuentas": ["Multi1", "Multi2"]}, ruta
        )
        query = _QueryFake("cli_codigo", _MensajeFake())
        contexto = _ContextoFake()
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(
                h.cli_callback(
                    _UpdateFake(uid=700, query=query, chat=_chat_grupo(), username="fulano"),
                    contexto,
                )
            )
            check(
                "codigo: el flujo pendiente queda guardado",
                contexto.user_data.get("cli_flujo") == "codigo",
            )
            query2 = _QueryFake("cuenta_Multi2", _MensajeFake())
            _correr(
                h.cuenta_callback(
                    _UpdateFake(uid=700, query=query2, chat=_chat_grupo(), username="fulano"),
                    contexto,
                )
            )
        check(
            "codigo: al elegir cuenta se entrega su TOTP",
            any("Este es tu código" in texto for texto, _ in query2.message.edits)
            and contexto.user_data.get("cli_flujo") is None,
        )

        # Un lector de correo que explota NO se usa: el flujo es SOLO TOTP.
        clientes_store.guardar(
            {"chat_id": CHAT_GRUPO, "cuentas": ["SoloTres"]}, ruta
        )
        modulo_falso = types.ModuleType("utils.lector_correo")

        def _lector_explota(*args, **kwargs):
            raise AssertionError(
                "el bot de clientes YA NO debe usar el lector de correo"
            )

        modulo_falso.obtener_codigo_verificacion = _lector_explota
        previo = sys.modules.get("utils.lector_correo")
        sys.modules["utils.lector_correo"] = modulo_falso
        try:
            query = _QueryFake("cli_codigo", _MensajeFake())
            with _parches((h, "_datos_cuenta", _datos_vacios)):
                _correr(
                    h.cli_callback(
                        _UpdateFake(uid=700, query=query, chat=_chat_grupo(), username="fulano"),
                        _ContextoFake(),
                    )
                )
        finally:
            if previo is None:
                sys.modules.pop("utils.lector_correo", None)
            else:
                sys.modules["utils.lector_correo"] = previo
        check(
            "codigo: un lector de correo que explota no afecta el flujo (no se usa)",
            bool(query.message.edits)
            and "no tiene configurado el código 2FA" in query.message.edits[-1][0],
        )

    # -------------------------------------- FLUJO DE NOMBRE (admin privado) --
    with _store_temporal() as ruta, _env("TELEGRAM_CLIENTES_CHAT_ID", None), _env(
        "TELEGRAM_ADMIN_IDS", "555"
    ):
        clientes_store.guardar({"chat_id": CHAT_GRUPO, "cuentas": ["CuentaNombre"]}, ruta)
        query = _QueryFake("cli_nombre", _MensajeFake())
        contexto = _ContextoFake()
        _correr(h.cli_callback(_UpdateFake(uid=555, query=query), contexto))
        check(
            "nombre (admin privado): pide el nombre por texto y deja flujo pendiente",
            "Escríbeme el nombre" in query.message.edits[-1][0]
            and contexto.user_data.get("cli_espera", {}).get("tipo") == "nombre",
        )
        update = _UpdateFake(uid=555, texto="http://spam.com")
        _correr(h.texto_recibido(update, contexto))
        check(
            "nombre (admin privado): texto invalido pide escribirlo otra vez",
            "No uses links" in update.effective_message.replies[-1][0]
            and contexto.user_data.get("cli_espera", {}).get("tipo") == "nombre",
        )
        update = _UpdateFake(uid=555, texto="María López")
        _correr(h.texto_recibido(update, contexto))
        texto, kwargs = update.effective_message.replies[-1]
        check(
            "nombre (admin privado): pide confirmacion con el nombre y botones si/no",
            "«María López»" in texto
            and "¿Lo hago?" in texto
            and _callbacks(kwargs["reply_markup"])
            == ["nombre_si_CuentaNombre", "nombre_no_CuentaNombre"]
            and contexto.user_data["cli_nombre_pend"]["nombre"] == "María López",
        )
        # ❌ No.
        query = _QueryFake("nombre_no_CuentaNombre")
        _correr(h.nombre_callback(_UpdateFake(uid=555, query=query), contexto))
        check(
            "nombre (admin privado): el boton No no cambia nada y limpia el pendiente",
            "no cambié nada" in query.message.edits[-1][0]
            and "cli_nombre_pend" not in contexto.user_data,
        )
        # ✅ Sí (con Selenium mockeado).
        update = _UpdateFake(uid=555, texto="Nuevo Nombre")
        contexto.user_data["cli_espera"] = {"tipo": "nombre", "usuario": "CuentaNombre"}
        _correr(h.texto_recibido(update, contexto))
        llamado = {}

        async def _cambiar_ok(usuario, nombre):
            llamado["usuario"] = usuario
            llamado["nombre"] = nombre
            return True, ""

        query = _QueryFake("nombre_si_CuentaNombre")
        with _parches((h, "ejecutar_cambiar_nombre", _cambiar_ok)):
            _correr(h.nombre_callback(_UpdateFake(uid=555, query=query), contexto))
        check(
            "nombre (admin privado): el boton Si ejecuta el cambio y avisa Listo",
            llamado == {"usuario": "CuentaNombre", "nombre": "Nuevo Nombre"}
            and "Cambiando el nombre" in query.message.edits[-1][0]
            and any("¡Listo!" in t for t, _ in query.message.replies),
        )
        # ✅ Sí pero falla.
        contexto.user_data["cli_nombre_pend"] = {"usuario": "CuentaNombre", "nombre": "Otro"}
        query = _QueryFake("nombre_si_CuentaNombre")

        async def _cambiar_falla(usuario, nombre):
            return False, "sin sesion"

        with _parches((h, "ejecutar_cambiar_nombre", _cambiar_falla)):
            _correr(h.nombre_callback(_UpdateFake(uid=555, query=query), contexto))
        check(
            "nombre (admin privado): fallo muestra mensaje simple y reintento",
            any("No se pudo cambiar" in t for t, _ in query.message.replies)
            and "nombre_CuentaNombre" in _callbacks(query.message.replies[-1][1]["reply_markup"]),
        )
        # Boton vencido.
        query = _QueryFake("nombre_si_CuentaNombre")
        _correr(h.nombre_callback(_UpdateFake(uid=555, query=query), contexto))
        check(
            "nombre (admin privado): confirmacion vencida avisa sin ejecutar",
            query.answers and query.answers[-1][1],
        )
        # DEFENSA: en el grupo el flujo de nombre no arranca.
        query = _QueryFake("nombre_CuentaNombre", _MensajeFake())
        _correr(
            h.nombre_callback(
                _UpdateFake(uid=500, query=query, chat=_chat_grupo(), username="alguien"),
                _ContextoFake(),
            )
        )
        check(
            "nombre: en el grupo se rechaza (solo admin privado)",
            not query.message.edits
            and query.answers[-1][1]
            and h.TEXTO_FLUJO_SOLO_ADMIN in query.answers[-1][0],
        )

    # ---------------------------------------- FLUJO DE FOTO (admin privado) --
    with _store_temporal() as ruta, _env("TELEGRAM_CLIENTES_CHAT_ID", None), _env(
        "TELEGRAM_ADMIN_IDS", "555"
    ):
        clientes_store.guardar({"chat_id": CHAT_GRUPO, "cuentas": ["CuentaFoto"]}, ruta)
        with tempfile.TemporaryDirectory() as temporal:
            query = _QueryFake("cli_foto_perfil", _MensajeFake())
            contexto = _ContextoFake()
            _correr(h.cli_callback(_UpdateFake(uid=555, query=query), contexto))
            check(
                "foto (admin privado): pide la imagen por Telegram",
                "Envíame la foto de perfil" in query.message.edits[-1][0]
                and contexto.user_data.get("cli_espera", {}).get("tipo") == "foto",
            )
            update = _UpdateFake(uid=555, fotos=[_FotoFake()])
            with _parches((h, "_data_temp", lambda: temporal)):
                _correr(h.foto_recibida(update, contexto))
            caption = update.effective_message.photos[-1][1]
            kwargs = update.effective_message.photos[-1][2]
            pendiente = contexto.user_data.get("cli_foto_pend") or {}
            check(
                "foto (admin privado): vista previa pide confirmacion con botones",
                "¿Uso esta foto como tu nueva foto de perfil de @CuentaFoto?"
                in caption
                and "foto_si_perfil_CuentaFoto" in _callbacks(kwargs["reply_markup"])
                and os.path.isfile(pendiente.get("ruta", "")),
            )
            # ❌ No.
            query = _QueryFake("foto_no_perfil_CuentaFoto")
            _correr(h.foto_callback(_UpdateFake(uid=555, query=query), contexto))
            check(
                "foto (admin privado): el boton No limpia la pendiente",
                "no cambié nada" in query.message.edits[-1][0]
                and "cli_foto_pend" not in contexto.user_data,
            )
            # 🔁 Otra foto.
            query = _QueryFake("cli_foto_perfil", _MensajeFake())
            _correr(h.cli_callback(_UpdateFake(uid=555, query=query), contexto))
            update = _UpdateFake(uid=555, fotos=[_FotoFake()])
            with _parches((h, "_data_temp", lambda: temporal)):
                _correr(h.foto_recibida(update, contexto))
            query = _QueryFake("foto_otra_perfil_CuentaFoto")
            _correr(h.foto_callback(_UpdateFake(uid=555, query=query), contexto))
            check(
                "foto (admin privado): el boton Otra foto vuelve a pedirla",
                "Envíame la foto de perfil" in query.message.edits[-1][0]
                and contexto.user_data.get("cli_espera", {}).get("tipo") == "foto",
            )
            # ✅ Sí (Selenium mockeado + limpieza del archivo temporal).
            ruta_temporal = os.path.join(temporal, "prueba.jpg")
            with open(ruta_temporal, "wb") as fh:
                fh.write(b"foto")
            contexto.user_data["cli_foto_pend"] = {
                "usuario": "CuentaFoto",
                "tipo": "perfil",
                "ruta": ruta_temporal,
            }
            llamado = {}

            async def _foto_ok(usuario, tipo, ruta):
                llamado.update({"usuario": usuario, "tipo": tipo, "ruta": ruta})
                return True, ""

            query = _QueryFake("foto_si_perfil_CuentaFoto")
            with _parches((h, "ejecutar_cambiar_foto", _foto_ok)):
                _correr(h.foto_callback(_UpdateFake(uid=555, query=query), contexto))
            check(
                "foto (admin privado): el boton Si sube la foto y limpia la pendiente",
                llamado["usuario"] == "CuentaFoto"
                and llamado["tipo"] == "perfil"
                and "Subiendo" in query.message.edits[-1][0]
                and any("¡Listo!" in t for t, _ in query.message.replies),
            )
            # Foto sin flujo pendiente (admin privado): aviso.
            contexto.user_data.pop("cli_espera", None)
            update = _UpdateFake(uid=555, fotos=[_FotoFake()])
            _correr(h.foto_recibida(update, contexto))
            check(
                "foto (admin privado): sin flujo pendiente avisa que no la esperaba",
                "No esperaba ninguna foto"
                in update.effective_message.replies[-1][0],
            )
            # En el GRUPO no hay fotos: silencio (ni aviso ni ejecucion).
            update = _UpdateFake(uid=500, fotos=[_FotoFake()], chat=_chat_grupo())
            _correr(h.foto_recibida(update, _ContextoFake()))
            check(
                "foto: en el grupo se ignora en silencio (solo 2FA)",
                update.effective_message.replies == []
                and update.effective_message.photos == [],
            )
            # DEFENSA: boton viejo de foto en el grupo -> rechazado.
            query = _QueryFake("foto_perfil_CuentaFoto")
            _correr(
                h.foto_callback(
                    _UpdateFake(uid=500, query=query, chat=_chat_grupo(), username="alguien"),
                    _ContextoFake(),
                )
            )
            check(
                "foto: en el grupo el boton se rechaza (solo admin privado)",
                not query.message.edits
                and query.answers[-1][1]
                and h.TEXTO_FLUJO_SOLO_ADMIN in query.answers[-1][0],
            )

        # 📋 Cuentas muestra el @ y el nombre REGISTRADO (BD fake).
        def _datos_registrado(u):
            return {
                "usuario": u,
                "handle_actual": "HandleReal",
                "nombre_mostrado": "Nombre Registrado",
                "totp_secret": "",
            }

        query = _QueryFake("cli_cuentas", _MensajeFake())
        with _parches((h, "_datos_cuenta", _datos_registrado)):
            _correr(h.cli_callback(_UpdateFake(uid=555, query=query), _ContextoFake()))
        check(
            "cuentas (admin privado): lista las cuentas con su nombre registrado",
            "@HandleReal" in query.message.edits[-1][0]
            and "«Nombre Registrado»" in query.message.edits[-1][0]
            and "cli_cuenta_CuentaFoto" in _callbacks(query.message.edits[-1][1]["reply_markup"]),
        )

        # /cancelar limpia cualquier flujo.
        contexto = _ContextoFake()
        contexto.user_data.update(
            {
                "cli_flujo": "codigo",
                "cli_espera": {"tipo": "foto"},
                "cli_nombre_pend": {"usuario": "x"},
                "cli_foto_pend": {"usuario": "x"},
            }
        )
        update = _UpdateFake(uid=555, texto="/cancelar")
        _correr(h.cancelar(update, contexto))
        check(
            "/cancelar (admin privado): limpia flujos y vuelve al menu",
            "cancelado" in update.effective_message.replies[-1][0]
            and not any(clave in contexto.user_data for clave in (
                "cli_flujo",
                "cli_espera",
                "cli_nombre_pend",
                "cli_foto_pend",
            )),
        )

    # -------------------------------------------------------------- GRUPOS --
    check(
        "grupo: helpers de mencion (grupo si, privado no)",
        h.mencion_grupo(_UpdateFake(uid=1, chat=_chat_grupo(), username="fulano"))
        == "@fulano"
        and h.mencion_grupo(
            _UpdateFake(uid=1, chat=_ChatFake(1, "private"), username="fulano")
        )
        == ""
        and h.con_mencion("hola", "@fulano") == "👤 @fulano, hola"
        and h.con_mencion("hola", "") == "hola",
    )
    check(
        "grupo: sin @username usa el nombre visible",
        h.mencion_grupo(_UpdateFake(uid=1, chat=_chat_grupo(), username=None))
        == "Persona",
    )

    with _store_temporal() as ruta, _env("TELEGRAM_CLIENTES_CHAT_ID", None), _env(
        "TELEGRAM_ADMIN_IDS", None
    ):
        grupo = _chat_grupo()
        # Bienvenida al agregar el bot (new_chat_members) + anti-duplicado.
        h._BIENVENIDAS.clear()
        update = _UpdateFake(uid=500, chat=grupo)
        update.effective_message.new_chat_members = [_UsuarioFake(424242)]
        ctx = _ContextoFake()
        _correr(h.bienvenida_grupo(update, ctx))
        check(
            "grupo: bienvenida al agregar el bot (flujo 2FA)",
            bool(update.effective_message.replies)
            and "Escribe /start" in update.effective_message.replies[-1][0]
            and "código 2FA" in update.effective_message.replies[-1][0],
        )
        _correr(h.bienvenida_grupo(update, ctx))
        check(
            "grupo: la bienvenida NO se duplica",
            len(update.effective_message.replies) == 1,
        )
        # En OTRO grupo: silencio (no saluda).
        update_otro = _UpdateFake(uid=500, chat=_chat_grupo(CHAT_OTRO))
        update_otro.effective_message.new_chat_members = [_UsuarioFake(424242)]
        _correr(h.bienvenida_grupo(update_otro, _ContextoFake()))
        check(
            "grupo: la bienvenida NO se manda en otros grupos",
            update_otro.effective_message.replies == [],
        )
        # my_chat_member: entra al chat permitido -> saluda; otro -> silencio.
        update2 = _UpdateFake(uid=None, chat=_chat_grupo(-1009999999998))
        update2.effective_message = None  # un chat_member update no trae mensaje
        update2.my_chat_member = _CambioMiembroFake("left", "member")
        ctx2 = _ContextoFake()
        _correr(h.bienvenida_miembro(update2, ctx2))
        check(
            "grupo: my_chat_member no saluda si el chat NO es el permitido",
            not ctx2.bot.enviados,
        )
        update3 = _UpdateFake(uid=None, chat=grupo)
        update3.effective_message = None
        update3.my_chat_member = _CambioMiembroFake("left", "member")
        ctx3 = _ContextoFake()
        h._BIENVENIDAS.clear()  # el grupo ya se marco con new_chat_members
        _correr(h.bienvenida_miembro(update3, ctx3))
        check(
            "grupo: my_chat_member saluda al entrar al grupo permitido",
            bool(ctx3.bot.enviados) and "Escribe /start" in ctx3.bot.enviados[-1][1],
        )
        update4 = _UpdateFake(uid=None, chat=_chat_grupo(-1008888888888))
        update4.effective_message = None
        update4.my_chat_member = _CambioMiembroFake("administrator", "administrator")
        ctx4 = _ContextoFake()
        _correr(h.bienvenida_miembro(update4, ctx4))
        check(
            "grupo: sin cambio de entrada no hay bienvenida",
            not ctx4.bot.enviados,
        )
        h._BIENVENIDAS.clear()

        # Un miembro CUALQUIERA obtiene el TOTP de una cuenta global.
        def _datos_totp(usuario):
            return {"totp_secret": SECRETO_FAKE, "handle_actual": "", "nombre_mostrado": ""}

        query = _QueryFake("codigo_3ranuii97", _MensajeFake())
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(
                h.codigo_callback(
                    _UpdateFake(uid=999, query=query, chat=grupo, username="nuevo"),
                    _ContextoFake(),
                )
            )
        check(
            "grupo: cualquier miembro obtiene el TOTP (cuentas globales)",
            any("Este es tu código" in t for t, _ in query.message.edits)
            and re.search(r"\b\d{6}\b", query.message.edits[-1][0]) is not None,
        )

        # Texto SIN flujo de otro miembro: ignorado en silencio.
        update = _UpdateFake(
            uid=801, texto="hola a todos", chat=grupo, username="otro"
        )
        _correr(h.texto_recibido(update, _ContextoFake()))
        check(
            "grupo: texto de otro miembro SIN flujo se ignora (nada de spam)",
            update.effective_message.replies == [],
        )

        # Aislamiento: el flujo de A no lo captura B (user_data por usuario).
        ctx_a = _ContextoFake()
        ctx_b = _ContextoFake()
        ctx_a.user_data["cli_espera"] = {"tipo": "nombre", "usuario": "CuentaFoto"}
        update_b = _UpdateFake(uid=801, texto="Nombre de B", chat=grupo, username="otro")
        _correr(h.texto_recibido(update_b, ctx_b))
        check(
            "grupo: el flujo de A no lo captura B (user_data por usuario)",
            update_b.effective_message.replies == []
            and "cli_espera" in ctx_a.user_data,
        )

        # En el GRUPO no hay flujo de texto (solo 2FA): aunque haya un pendiente
        # viejo, el texto se descarta en silencio (y se limpia).
        ctx_a.user_data["cli_espera"] = {"tipo": "nombre", "usuario": "CuentaFoto"}
        update = _UpdateFake(uid=800, texto="Otro Nombre", chat=grupo, username="fulano")
        update.effective_message.reply_to_message = _MensajeFake(
            "✏️ Escríbeme el nombre nuevo para @CuentaFoto."
        )
        _correr(h.texto_recibido(update, ctx_a))
        check(
            "grupo: el texto NO arranca el flujo de nombre (se descarta)",
            update.effective_message.replies == []
            and "cli_espera" not in ctx_a.user_data,
        )

        # Boton de confirmacion en el grupo: se rechaza (solo admin privado).
        query = _QueryFake("nombre_si_CuentaFoto")
        _correr(
            h.nombre_callback(
                _UpdateFake(uid=801, query=query, chat=grupo, username="otro"),
                _ContextoFake(),
            )
        )
        check(
            "grupo: la confirmacion de nombre se rechaza (solo admin privado)",
            not query.message.edits
            and bool(query.answers)
            and query.answers[-1][1]
            and h.TEXTO_FLUJO_SOLO_ADMIN in query.answers[-1][0],
        )

    # ---------------------------------- ADMIN /nombre (solo privado admin) --
    with _store_temporal() as ruta, _env("TELEGRAM_CLIENTES_CHAT_ID", None), _env(
        "TELEGRAM_ADMIN_IDS", "555"
    ):
        # BD fake: la cuenta existe y se actualiza nombre_mostrado.
        cuenta_fake = _CuentaBDFake("MiCuenta", "")
        with _db_fake(cuenta_fake):
            ok, error = h._actualizar_nombre_registrado("MiCuenta", "María  López")
        check(
            "admin /nombre: actualiza nombre_mostrado en la BD (sesion fake)",
            ok and error == "" and cuenta_fake.nombre_mostrado == "María López",
        )
        with _db_fake(None):
            ok, error = h._actualizar_nombre_registrado("NoExiste", "X")
        check(
            "admin /nombre: cuenta no encontrada devuelve error claro",
            not ok and "No encontré la cuenta" in error,
        )
        # Handler completo en el PRIVADO del admin, con la BD fake.
        update = _UpdateFake(uid=555, texto="/nombre")
        with _db_fake(cuenta_fake):
            _correr(h.comando_nombre(update, _ContextoFake(args=["MiCuenta", "Nuevo"])))
        check(
            "admin /nombre: en privado confirma el cambio con el nombre completo",
            "Nuevo" in cuenta_fake.nombre_mostrado
            and "ahora es «Nuevo»" in update.effective_message.replies[-1][0],
        )
        # Sin args -> uso.
        update = _UpdateFake(uid=555, texto="/nombre")
        _correr(h.comando_nombre(update, _ContextoFake(args=[])))
        check(
            "admin /nombre: sin args muestra el uso",
            "Uso: /nombre" in update.effective_message.replies[-1][0],
        )
        # Privado NO admin -> mensaje claro (guard por contexto).
        update = _UpdateFake(uid=666, texto="/nombre")
        _correr(h.comando_nombre(update, _ContextoFake(args=["MiCuenta", "Nuevo"])))
        check(
            "admin /nombre: privado ajeno recibe el mensaje claro",
            h.TEXTO_SOLO_ADMIN_PRIVADO in update.effective_message.replies[-1][0],
        )
        # En el GRUPO -> "solo por privado del administrador".
        update = _UpdateFake(uid=555, texto="/nombre", chat=_chat_grupo(), username="jefe")
        _correr(h.comando_nombre(update, _ContextoFake(args=["MiCuenta", "Nuevo"])))
        check(
            "admin /nombre: en el grupo avisa que es solo por privado",
            h.TEXTO_NOMBRE_SOLO_PRIVADO in update.effective_message.replies[-1][0]
            and "privado" in update.effective_message.replies[-1][0],
        )
        # Otro grupo -> silencio.
        update = _UpdateFake(uid=555, texto="/nombre", chat=_chat_grupo(CHAT_OTRO))
        _correr(h.comando_nombre(update, _ContextoFake(args=["MiCuenta", "Nuevo"])))
        check(
            "admin /nombre: en otro grupo se ignora en silencio",
            update.effective_message.replies == [],
        )
        # Los comandos viejos por cliente YA NO existen.
        check(
            "admin: /asignar, /quitar y /clientes se eliminaron",
            not hasattr(h, "comando_asignar")
            and not hasattr(h, "comando_quitar")
            and not hasattr(h, "comando_clientes"),
        )

    # ------------------------------------------------------- ENTRYPOINT ---
    fuente = (RAIZ / "bot_clientes" / "main.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    comandos = set()
    patrones = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name):
            if nodo.func.id == "CommandHandler" and nodo.args:
                comandos.add(ast.literal_eval(nodo.args[0]))
            if nodo.func.id == "CallbackQueryHandler":
                for kw in nodo.keywords:
                    if kw.arg == "pattern":
                        patrones.add(ast.literal_eval(kw.value))
    check(
        "main: registra los comandos del cliente y el /nombre de admin",
        {"start", "ayuda", "cancelar", "nombre"}.issubset(comandos)
        and not ({"asignar", "quitar", "clientes"} & comandos),
    )
    check(
        "main: registra los 5 prefijos de callbacks del bot de clientes",
        patrones == {r"^cli_", r"^cuenta_", r"^codigo_", r"^nombre_", r"^foto_"},
    )
    # --- Soporte de GRUPOS en el entrypoint ---
    allowed_updates = set()
    registros_bienvenida = set()
    usa_new_chat_members = False
    usa_my_chat_member = False
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Attribute):
            if nodo.attr == "NEW_CHAT_MEMBERS":
                usa_new_chat_members = True
            if nodo.attr == "MY_CHAT_MEMBER":
                usa_my_chat_member = True
            if nodo.attr in ("bienvenida_grupo", "bienvenida_miembro"):
                registros_bienvenida.add(nodo.attr)
        if isinstance(nodo, ast.Call):
            nombre_func = getattr(nodo.func, "attr", getattr(nodo.func, "id", ""))
            if nombre_func == "run_polling":
                for kw in nodo.keywords:
                    if kw.arg == "allowed_updates":
                        try:
                            allowed_updates = set(ast.literal_eval(kw.value))
                        except Exception:
                            allowed_updates = set()
    check(
        "main: allowed_updates incluye message, callback_query y my_chat_member",
        {"message", "callback_query", "my_chat_member"}.issubset(allowed_updates),
    )
    check(
        "main: registra la bienvenida al grupo (new_chat_members + my_chat_member)",
        {"bienvenida_grupo", "bienvenida_miembro"}.issubset(registros_bienvenida)
        and usa_new_chat_members
        and usa_my_chat_member,
    )
    check(
        "main: importar no arranca nada y `construir_app` arma la app",
        (
            "'if __name__' in fuente"
            " and fuente.count('main()') >= 1"
            " and _app_ok()"
        ),
    )
    check(
        "main: obtener_token lee TELEGRAM_CLIENTES_BOT_TOKEN del entorno",
        _token_env(),
    )
    check(
        "main: sin token no arranca (SystemExit 1)",
        _sin_token_exit(),
    )
    check(
        "main: `import bot_clientes.main` sin token no imprime el arranque",
        _import_sin_token_ok(),
    )

    # ------------------------------------- TOKEN DUPLICADO (BOT INTERNO) --
    from bot_clientes import main as mod  # noqa: E402

    check(
        "token duplicado: helper detecta el mismo token (ignora espacios)",
        mod.es_token_del_bot_interno(" 111:AAA ", "111:AAA")
        and not mod.es_token_del_bot_interno("111:AAA", "222:BBB"),
    )
    check(
        "token duplicado: helper nunca marca tokens vacios",
        not mod.es_token_del_bot_interno("", "111:AAA")
        and not mod.es_token_del_bot_interno("111:AAA", "")
        and not mod.es_token_del_bot_interno("", ""),
    )
    check(
        "token duplicado: obtener_token_interno lee TELEGRAM_BOT_TOKEN del entorno",
        _token_interno_env(),
    )
    check(
        "token duplicado: obtener_token_interno cae a settings si no hay env",
        _token_interno_settings(),
    )
    resultado = _main_con_token("111:AAA", " 111:AAA ")
    check(
        "token duplicado: main() NO construye la app y sale con SystemExit(1)",
        resultado[0] == "exit"
        and resultado[1] == 1
        and not resultado[2]["construir"]
        and not resultado[2]["polling"],
    )
    check(
        "token duplicado: el error explica que Telegram rompe ambos bots",
        any("MISMO que el del bot interno" in m for m in resultado[3])
        and any("@BotFather" in m for m in resultado[3]),
    )
    resultado = _main_con_token("222:BBB", "111:AAA")
    check(
        "token distinto: main() arranca normal (app y polling fake)",
        resultado[0] == "polling"
        and resultado[2]["construir"]
        and resultado[2]["polling"]
        and not any("MISMO que el del bot interno" in m for m in resultado[3]),
    )
    resultado = _main_con_token("", "111:AAA")
    check(
        "token vacio: sigue el guard actual de falta de token",
        resultado[0] == "exit"
        and resultado[1] == 1
        and not resultado[2]["construir"]
        and any("falta TELEGRAM_CLIENTES_BOT_TOKEN" in m for m in resultado[3]),
    )


# --------------------------------------------------------------------------- #
# Helpers de los checks de entrypoint (definidos abajo para no ensuciar)
# --------------------------------------------------------------------------- #

def _app_ok() -> bool:
    """`construir_app` con token fake registra los handlers esperados."""
    from telegram.ext import (
        CallbackQueryHandler,
        ChatMemberHandler,
        CommandHandler,
        MessageHandler,
    )

    from bot_clientes.main import construir_app

    try:
        app = construir_app("123456:FAKE-TOKEN")
    except Exception:
        return False
    handlers = [handler for grupo in app.handlers.values() for handler in grupo]
    return (
        sum(isinstance(x, CommandHandler) for x in handlers) == 5
        and sum(isinstance(x, CallbackQueryHandler) for x in handlers) == 5
        # texto + fotos + new_chat_members
        and sum(isinstance(x, MessageHandler) for x in handlers) == 3
        and sum(isinstance(x, ChatMemberHandler) for x in handlers) == 1
    )


def _token_env() -> bool:
    import bot_clientes.main as mod

    with _env("TELEGRAM_CLIENTES_BOT_TOKEN", " 123:TOKEN  "):
        return mod.obtener_token() == "123:TOKEN"


def _token_interno_env() -> bool:
    import bot_clientes.main as mod

    with _env("TELEGRAM_BOT_TOKEN", " 999:INTERNO  "):
        return mod.obtener_token_interno() == "999:INTERNO"


def _token_interno_settings() -> bool:
    """Sin env, `obtener_token_interno` cae a `settings.telegram_bot_token`."""
    import core.config as core_config

    import bot_clientes.main as mod

    class _SettingsFake:
        telegram_bot_token = " 777:DESDE-SETTINGS "

    with _env("TELEGRAM_BOT_TOKEN", None), _parches(
        (core_config, "settings", _SettingsFake())
    ):
        return mod.obtener_token_interno() == "777:DESDE-SETTINGS"


class _CapturaLog(logging.Handler):
    """Handler que guarda los mensajes de log (para verificar el error)."""

    def __init__(self):
        super().__init__()
        self.mensajes = []

    def emit(self, record):
        self.mensajes.append(record.getMessage())


class _FinPolling(Exception):
    """Centinela: main() llego a `run_polling` sin arrancar Telegram de verdad."""


def _main_con_token(cliente_token, interno_env):
    """Ejecuta `mod.main()` con `construir_app` falso (nunca toca Telegram).

    Devuelve `(resultado, codigo, llamado, mensajes)`:
      resultado: "exit" | "polling" | "retorno" | nombre de excepcion rara.
      llamado:   {"construir": bool, "polling": bool}.
      mensajes:  mensajes de log capturados.
    """
    import bot_clientes.main as mod

    llamado = {"construir": False, "polling": False}

    class _AppFake:
        def run_polling(self, **kwargs):
            llamado["polling"] = True
            raise _FinPolling()

    def _construir_fake(token):
        llamado["construir"] = True
        return _AppFake()

    captura = _CapturaLog()
    logger = logging.getLogger("bot_clientes.main")
    logger.addHandler(captura)
    try:
        with _env("TELEGRAM_BOT_TOKEN", interno_env), _parches(
            (mod, "obtener_token", lambda: cliente_token),
            (mod, "construir_app", _construir_fake),
        ):
            try:
                mod.main()
                return ("retorno", 0, llamado, captura.mensajes)
            except SystemExit as e:
                return ("exit", int(e.code or 0), llamado, captura.mensajes)
            except _FinPolling:
                return ("polling", 0, llamado, captura.mensajes)
            except Exception as e:  # nunca deberia pasar
                return (type(e).__name__, 0, llamado, captura.mensajes)
    finally:
        logger.removeHandler(captura)


def _sin_token_exit() -> bool:
    import bot_clientes.main as mod

    with _parches((mod, "obtener_token", lambda: "")):
        try:
            mod.main()
        except SystemExit as e:
            return int(e.code or 0) == 1
        except Exception:
            return False
    return False


def _import_sin_token_ok() -> bool:
    """Subproceso: importar no arranca polling y termina con 0."""
    entorno = os.environ.copy()
    entorno.pop("TELEGRAM_CLIENTES_BOT_TOKEN", None)
    try:
        salida = subprocess.run(
            [
                sys.executable,
                "-c",
                "import bot_clientes.main; print('IMPORT-OK')",
            ],
            cwd=str(RAIZ),
            env=entorno,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception:
        return False
    return (
        salida.returncode == 0
        and "IMPORT-OK" in (salida.stdout or "")
        and "Bot de clientes iniciado" not in (salida.stdout or "")
    )


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_bot_clientes.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

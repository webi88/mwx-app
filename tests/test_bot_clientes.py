# -*- coding: utf-8 -*-
"""Tests del bot de clientes (`bot_clientes/`) sin Telegram real ni Chrome.

Cubre:
  - `clientes_store`: crear/leer/asignar/quitar, duplicados, '@', IDs
    invalidos, JSON corrupto y escritura atomica (siempre en archivo temporal).
  - `keyboards`: los botones/callbacks esperados del menu y confirmaciones.
  - Validacion del nombre nuevo y formateo del codigo 2FA, que es SOLO TOTP
    (semilla fake -> 6 digitos; sin semilla -> mensaje amable) y que el flujo
    YA NO depende de `utils.lector_correo` (ni import, ni llamada; un lector
    falso que explota no afecta).
  - Guard de no autorizado, admin detectado y flujos completos con fakes PTB
    (start/menu, codigo TOTP, confirmacion de nombre, foto y comandos de admin
    `/asignar`/`/quitar` escribiendo un JSON temporal).
  - Grupos: bienvenida, menciones, aislamiento por usuario y callbacks.
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

SECRETO_FAKE = "JBSWY3DPEHPK3PXP"


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


def _chat_grupo(chat_id=-1005538610567):
    return _ChatFake(chat_id, "supergroup")


def _correr(coro):
    return asyncio.run(coro)


def _callbacks(markup) -> list:
    """Lista plana de callback_data de un InlineKeyboardMarkup."""
    if markup is None:
        return []
    filas = getattr(markup, "inline_keyboard", None) or []
    return [boton.callback_data for fila in filas for boton in fila]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def run(check) -> None:  # noqa: C901 - seccionado por bloques tematicos
    # ---------------------------------------------------------------- STORE --
    with _store_temporal() as ruta:
        carpeta = os.path.dirname(ruta)
        datos = clientes_store.cargar(ruta)
        check(
            "store: cargar crea el archivo vacio si no existe",
            os.path.isfile(ruta) and datos == {"clientes": {}},
        )
        check(
            "store: asignar devuelve ok y registra nombre + cuentas",
            clientes_store.asignar("123", "Cliente A", ["uno", "dos"], ruta) == ""
            and clientes_store.cliente_de("123", ruta) == {
                "nombre": "Cliente A",
                "cuentas": ["uno", "dos"],
            },
        )
        check(
            "store: cuentas_de devuelve la lista del cliente",
            clientes_store.cuentas_de("123", ruta) == ["uno", "dos"],
        )
        clientes_store.asignar("123", "", ["DOS", "tres"], ruta)
        check(
            "store: asignar AGREGA y deduplica case-insensitive",
            clientes_store.cuentas_de("123", ruta) == ["uno", "dos", "tres"],
        )
        clientes_store.asignar("123", "Cliente A2", [], ruta)
        check(
            "store: asignar sin cuentas solo actualiza el nombre",
            clientes_store.cliente_de("123", ruta)
            == {"nombre": "Cliente A2", "cuentas": ["uno", "dos", "tres"]},
        )
        clientes_store.quitar("123", ["uno"], ruta)
        check(
            "store: quitar elimina la cuenta pedida",
            clientes_store.cuentas_de("123", ruta) == ["dos", "tres"],
        )
        clientes_store.quitar("123", ["no-existe"], ruta)
        check(
            "store: quitar una cuenta inexistente no rompe",
            clientes_store.cuentas_de("123", ruta) == ["dos", "tres"],
        )
        error = clientes_store.quitar("999", ["x"], ruta)
        check(
            "store: quitar de un cliente inexistente devuelve error string",
            isinstance(error, str) and "no está registrado" in error,
        )
        for malo in ("abc", "12x", None, "", "0", "-5"):
            check(
                f"store: ID invalido {malo!r} no se registra",
                bool(clientes_store.asignar(malo, "", ["u"], ruta))
                and clientes_store.cliente_de(malo, ruta) is None,
            )
        check(
            "store: asignar sin usuarios ni nombre devuelve error",
            "No hay usuarios válidos" in clientes_store.asignar("77", "", [], ruta),
        )
        clientes_store.asignar("321", "", ["@ConArroba", " ConEspacios "], ruta)
        check(
            "store: los '@' y espacios se normalizan",
            clientes_store.cuentas_de("321", ruta) == ["ConArroba", "ConEspacios"],
        )
        clientes_store.asignar("322", "", "CuentaSuelta", ruta)
        check(
            "store: un string suelto se trata como UNA cuenta (no letra por letra)",
            clientes_store.cuentas_de("322", ruta) == ["CuentaSuelta"],
        )
        check(
            "store: usuario_permitido es case-insensitive",
            clientes_store.usuario_permitido("321", "conarroba", ruta)
            and not clientes_store.usuario_permitido("321", "otra", ruta),
        )
        check(
            "store: es_cliente distingue registrados",
            clientes_store.es_cliente("321", ruta)
            and not clientes_store.es_cliente("999", ruta),
        )
        clientes_store.asignar("000321", "", ["OtraMas"], ruta)
        check(
            "store: los IDs se normalizan (000321 == 321)",
            clientes_store.cuentas_de("321", ruta) == ["ConArroba", "ConEspacios", "OtraMas"],
        )
        check(
            "store: todos_los_clientes devuelve el registro completo",
            set(clientes_store.todos_los_clientes(ruta)) == {"123", "321", "322"},
        )
        # Atomicidad: tras varias escrituras no quedan temporales y el JSON vale.
        sobrantes = [n for n in os.listdir(carpeta) if n.endswith(".tmp")]
        with open(ruta, encoding="utf-8") as fh:
            contenido = json.load(fh)
        check(
            "store: guardar es atomico (sin .tmp y JSON valido)",
            not sobrantes and "clientes" in contenido,
        )
        # JSON corrupto: no lanza y devuelve estructura vacia.
        with open(ruta, "w", encoding="utf-8") as fh:
            fh.write("{esto no es json!!")
        recuperado = clientes_store.cargar(ruta)
        check(
            "store: JSON corrupto no lanza y devuelve estructura vacia",
            recuperado == {"clientes": {}},
        )
        check(
            "store: guardar con datos invalidos normaliza (nunca lanza)",
            clientes_store.guardar("no-dict", ruta) == ""
            and clientes_store.cargar(ruta) == {"clientes": {}},
        )

    # ------------------------------------------------------------ KEYBOARDS --
    menu = k.menu_principal()
    callbacks_menu = _callbacks(menu)
    check(
        "teclados: el menu tiene los 6 botones del cliente",
        callbacks_menu
        == [
            "cli_codigo",
            "cli_nombre",
            "cli_foto_perfil",
            "cli_foto_portada",
            "cli_mis_cuentas",
            "cli_ayuda",
        ],
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

    # -------------------------------------------------- GUARD / START/MENU --
    with _store_temporal() as ruta, _env("TELEGRAM_ADMIN_IDS", None):
        update = _UpdateFake(uid=999, texto="/start")
        _correr(h.start(update, _ContextoFake()))
        check(
            "guard: no registrado recibe UN mensaje claro y sin menu",
            update.effective_message.replies
            and "No tengo tu cuenta registrada" in update.effective_message.replies[0][0]
            and not update.effective_message.replies[0][1],
        )
        query = _QueryFake("cli_menu")
        update = _UpdateFake(uid=999, query=query)
        _correr(h.cli_callback(update, _ContextoFake()))
        check(
            "guard: callback de no registrado responde alerta",
            query.answers and query.answers[-1][1] and not query.message.edits,
        )

    with _store_temporal() as ruta, _env("TELEGRAM_ADMIN_IDS", "555"):
        check(
            "admin detectado solo con su ID en TELEGRAM_ADMIN_IDS",
            h.es_admin(555) and not h.es_admin(556) and not h.es_admin("x"),
        )
        update = _UpdateFake(uid=555, texto="/start")
        _correr(h.start(update, _ContextoFake()))
        check(
            "admin no registrado igual entra al menu",
            update.effective_message.replies
            and "Hola" in update.effective_message.replies[0][0]
            and "cli_codigo" in _callbacks(update.effective_message.replies[0][1]["reply_markup"]),
        )
        error = clientes_store.asignar("700", "Cliente A", ["Uno", "Dos"], ruta)
        update = _UpdateFake(uid=700, texto="/start")
        _correr(h.start(update, _ContextoFake()))
        texto, kwargs = update.effective_message.replies[0]
        check(
            "start cliente: saluda con su nombre y avisa cuantas cuentas tiene",
            error == ""
            and "Cliente A" in texto
            and "2 cuentas" in texto
            and "cli_codigo" in _callbacks(kwargs["reply_markup"]),
        )

    # ------------------------------------------------- FLUJO DE CODIGO ----
    with _store_temporal() as ruta, _env("TELEGRAM_ADMIN_IDS", None):
        clientes_store.asignar("701", "Cliente Uno", ["SoloUno"], ruta)
        clientes_store.asignar("702", "Cliente Dos", ["SoloDos"], ruta)
        clientes_store.asignar("703", "Cliente Tres", ["Multi1", "Multi2"], ruta)

        def _datos_totp(usuario):
            return {
                "usuario": usuario,
                "handle_actual": "HandleReal",
                "nombre_mostrado": "",
                "totp_secret": SECRETO_FAKE,
                "activa": True,
            }

        # Multiples cuentas: pregunta con cual.
        query = _QueryFake("cli_codigo")
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(h.cli_callback(_UpdateFake(uid=703, query=query), _ContextoFake()))
        ultimo, kwargs = query.message.edits[-1]
        check(
            "codigo: con varias cuentas pregunta con cual",
            "¿Con cuál cuenta" in ultimo
            and {"cuenta_Multi1", "cuenta_Multi2"}.issubset(set(_callbacks(kwargs["reply_markup"]))),
        )
        # Una sola cuenta: va directo al codigo TOTP.
        query = _QueryFake("cli_codigo")
        contexto = _ContextoFake()
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(h.cli_callback(_UpdateFake(uid=701, query=query), contexto))
        ultimo, kwargs = query.message.edits[-1]
        seis = re.search(r"\b\d{6}\b", ultimo)
        check(
            "codigo: una sola cuenta entrega el TOTP directo (con HTML y boton)",
            seis is not None
            and "Este es tu código" in ultimo
            and kwargs.get("parse_mode") == "HTML"
            and "codigo_SoloUno" in _callbacks(kwargs["reply_markup"]),
        )

        # Flujo multi: elegir cuenta por callback.
        query = _QueryFake("cli_codigo")
        contexto = _ContextoFake()
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(h.cli_callback(_UpdateFake(uid=703, query=query), contexto))
            check(
                "codigo: el flujo pendiente queda guardado",
                contexto.user_data.get("cli_flujo") == "codigo",
            )
            query2 = _QueryFake("cuenta_Multi2", _MensajeFake())
            _correr(h.cuenta_callback(_UpdateFake(uid=703, query=query2), contexto))
        check(
            "codigo: al elegir cuenta se entrega su TOTP",
            any("Este es tu código" in texto for texto, _ in query2.message.edits)
            and contexto.user_data.get("cli_flujo") is None,
        )

        # Sin semilla: mensaje amable + boton de reintento (sin correo).
        def _datos_vacios(usuario):
            return {"totp_secret": "", "handle_actual": "", "nombre_mostrado": ""}

        query = _QueryFake("cli_codigo")
        with _parches((h, "_datos_cuenta", _datos_vacios)):
            _correr(h.cli_callback(_UpdateFake(uid=702, query=query), _ContextoFake()))
        ultimo, kwargs = query.message.edits[-1]
        check(
            "codigo: sin semilla muestra el mensaje amable (sin correo)",
            "no tiene configurado el código 2FA" in ultimo
            and "Pide ayuda" in ultimo
            and "correo" not in ultimo.lower()
            and "codigo_SoloDos" in _callbacks(kwargs["reply_markup"]),
        )

        # Semilla invalida: mismo mensaje amable (el detalle va al log).
        def _datos_malos(usuario):
            return {"totp_secret": "no-es-base32!!"}

        query = _QueryFake("cli_codigo")
        with _parches((h, "_datos_cuenta", _datos_malos)):
            _correr(h.cli_callback(_UpdateFake(uid=702, query=query), _ContextoFake()))
        check(
            "codigo: semilla invalida tambien muestra el mensaje amable",
            "no tiene configurado el código 2FA" in query.message.edits[-1][0]
            and "no-es-base32" not in query.message.edits[-1][0],
        )

        # La semilla NUNCA se muestra.
        query = _QueryFake("cli_codigo")
        with _parches((h, "_datos_cuenta", _datos_totp)):
            _correr(h.cli_callback(_UpdateFake(uid=701, query=query), _ContextoFake()))
        texto_seguro = query.message.edits[-1][0]
        check(
            "codigo: la semilla TOTP nunca se muestra (solo el codigo)",
            SECRETO_FAKE not in texto_seguro
            and "totp_secret" not in texto_seguro
            and re.search(r"\b\d{6}\b", texto_seguro) is not None,
        )

        # Un lector de correo que explota NO se usa: el flujo es SOLO TOTP.
        modulo_falso = types.ModuleType("utils.lector_correo")

        def _lector_explota(*args, **kwargs):
            raise AssertionError(
                "el bot de clientes YA NO debe usar el lector de correo"
            )

        modulo_falso.obtener_codigo_verificacion = _lector_explota
        previo = sys.modules.get("utils.lector_correo")
        sys.modules["utils.lector_correo"] = modulo_falso
        try:
            query = _QueryFake("cli_codigo")
            with _parches((h, "_datos_cuenta", _datos_vacios)):
                _correr(
                    h.cli_callback(_UpdateFake(uid=702, query=query), _ContextoFake())
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

    # ------------------------------------------------ FLUJO DE NOMBRE -----
    with _store_temporal() as ruta, _env("TELEGRAM_ADMIN_IDS", None):
        clientes_store.asignar("704", "Cliente Nombre", ["CuentaNombre"], ruta)
        query = _QueryFake("cli_nombre")
        contexto = _ContextoFake()
        _correr(h.cli_callback(_UpdateFake(uid=704, query=query), contexto))
        check(
            "nombre: pide el nombre por texto y deja flujo pendiente",
            "Escríbeme el nombre" in query.message.edits[-1][0]
            and contexto.user_data.get("cli_espera", {}).get("tipo") == "nombre",
        )
        update = _UpdateFake(uid=704, texto="http://spam.com")
        _correr(h.texto_recibido(update, contexto))
        check(
            "nombre: texto invalido pide escribirlo otra vez",
            "No uses links" in update.effective_message.replies[-1][0]
            and contexto.user_data.get("cli_espera", {}).get("tipo") == "nombre",
        )
        update = _UpdateFake(uid=704, texto="María López")
        _correr(h.texto_recibido(update, contexto))
        texto, kwargs = update.effective_message.replies[-1]
        check(
            "nombre: pide confirmacion con el nombre y botones si/no",
            "«María López»" in texto
            and "¿Lo hago?" in texto
            and _callbacks(kwargs["reply_markup"]) == ["nombre_si_CuentaNombre", "nombre_no_CuentaNombre"]
            and contexto.user_data["cli_nombre_pend"]["nombre"] == "María López",
        )
        # ❌ No.
        query = _QueryFake("nombre_no_CuentaNombre")
        _correr(h.nombre_callback(_UpdateFake(uid=704, query=query), contexto))
        check(
            "nombre: el boton No no cambia nada y limpia el pendiente",
            "no cambié nada" in query.message.edits[-1][0]
            and "cli_nombre_pend" not in contexto.user_data,
        )
        # ✅ Sí (con Selenium mockeado).
        update = _UpdateFake(uid=704, texto="Nuevo Nombre")
        contexto.user_data["cli_espera"] = {"tipo": "nombre", "usuario": "CuentaNombre"}
        _correr(h.texto_recibido(update, contexto))
        llamado = {}

        async def _cambiar_ok(usuario, nombre):
            llamado["usuario"] = usuario
            llamado["nombre"] = nombre
            return True, ""

        query = _QueryFake("nombre_si_CuentaNombre")
        with _parches((h, "ejecutar_cambiar_nombre", _cambiar_ok)):
            _correr(h.nombre_callback(_UpdateFake(uid=704, query=query), contexto))
        check(
            "nombre: el boton Si ejecuta el cambio y avisa Listo",
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
            _correr(h.nombre_callback(_UpdateFake(uid=704, query=query), contexto))
        check(
            "nombre: fallo muestra mensaje simple y reintento",
            any("No se pudo cambiar" in t for t, _ in query.message.replies)
            and "nombre_CuentaNombre" in _callbacks(query.message.replies[-1][1]["reply_markup"]),
        )
        # Boton vencido.
        query = _QueryFake("nombre_si_CuentaNombre")
        _correr(h.nombre_callback(_UpdateFake(uid=704, query=query), contexto))
        check(
            "nombre: confirmacion vencida avisa sin ejecutar",
            query.answers and query.answers[-1][1],
        )

    # -------------------------------------------------- FLUJO DE FOTO -----
    with _store_temporal() as ruta, _env("TELEGRAM_ADMIN_IDS", None):
        clientes_store.asignar("705", "Cliente Foto", ["CuentaFoto"], ruta)
        with tempfile.TemporaryDirectory() as temporal:
            query = _QueryFake("cli_foto_perfil")
            contexto = _ContextoFake()
            _correr(h.cli_callback(_UpdateFake(uid=705, query=query), contexto))
            check(
                "foto: pide la imagen por Telegram",
                "Envíame la foto de perfil" in query.message.edits[-1][0]
                and contexto.user_data.get("cli_espera", {}).get("tipo") == "foto",
            )
            update = _UpdateFake(uid=705, fotos=[_FotoFake()])
            with _parches((h, "_data_temp", lambda: temporal)):
                _correr(h.foto_recibida(update, contexto))
            caption = update.effective_message.photos[-1][1]
            kwargs = update.effective_message.photos[-1][2]
            pendiente = contexto.user_data.get("cli_foto_pend") or {}
            check(
                "foto: vista previa pide confirmacion con botones",
                "¿Uso esta foto como tu nueva foto de perfil de @CuentaFoto?"
                in caption
                and "foto_si_perfil_CuentaFoto" in _callbacks(kwargs["reply_markup"])
                and os.path.isfile(pendiente.get("ruta", "")),
            )
            # ❌ No.
            query = _QueryFake("foto_no_perfil_CuentaFoto")
            _correr(h.foto_callback(_UpdateFake(uid=705, query=query), contexto))
            check(
                "foto: el boton No limpia la pendiente",
                "no cambié nada" in query.message.edits[-1][0]
                and "cli_foto_pend" not in contexto.user_data,
            )
            # 🔁 Otra foto.
            query = _QueryFake("cli_foto_perfil")
            _correr(h.cli_callback(_UpdateFake(uid=705, query=query), contexto))
            update = _UpdateFake(uid=705, fotos=[_FotoFake()])
            with _parches((h, "_data_temp", lambda: temporal)):
                _correr(h.foto_recibida(update, contexto))
            query = _QueryFake("foto_otra_perfil_CuentaFoto")
            _correr(h.foto_callback(_UpdateFake(uid=705, query=query), contexto))
            check(
                "foto: el boton Otra foto vuelve a pedirla",
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
                _correr(h.foto_callback(_UpdateFake(uid=705, query=query), contexto))
            check(
                "foto: el boton Si sube la foto y limpia la pendiente",
                llamado["usuario"] == "CuentaFoto"
                and llamado["tipo"] == "perfil"
                and "Subiendo" in query.message.edits[-1][0]
                and any("¡Listo!" in t for t, _ in query.message.replies),
            )
            # Foto sin flujo pendiente.
            contexto.user_data.pop("cli_espera", None)
            update = _UpdateFake(uid=705, fotos=[_FotoFake()])
            _correr(h.foto_recibida(update, contexto))
            check(
                "foto: sin flujo pendiente avisa que no la esperaba",
                "No esperaba ninguna foto"
                in update.effective_message.replies[-1][0],
            )

        # 📋 Mis cuentas muestra el @ real y el nombre actual.
        query = _QueryFake("cli_mis_cuentas")
        with _parches(
            (
                h,
                "_datos_cuenta",
                lambda u: {
                    "usuario": u,
                    "handle_actual": "HandleReal",
                    "nombre_mostrado": "Nombre Real",
                    "totp_secret": "",
                },
            )
        ):
            _correr(h.cli_callback(_UpdateFake(uid=705, query=query), _ContextoFake()))
        check(
            "mis cuentas: lista @handle y nombre sin credenciales",
            "@HandleReal" in query.message.edits[-1][0]
            and "«Nombre Real»" in query.message.edits[-1][0],
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
        update = _UpdateFake(uid=705, texto="/cancelar")
        _correr(h.cancelar(update, contexto))
        check(
            "/cancelar: limpia flujos y vuelve al menu",
            "cancelado" in update.effective_message.replies[-1][0]
            and not any(clave in contexto.user_data for clave in (
                "cli_flujo",
                "cli_espera",
                "cli_nombre_pend",
                "cli_foto_pend",
            )),
        )

    # ---------------------------------------------------- COMANDOS ADMIN --
    with _store_temporal() as ruta, _env("TELEGRAM_ADMIN_IDS", "555"):
        update = _UpdateFake(uid=666, texto="/asignar 1 u")
        contexto = _ContextoFake(args=["1", "u"])
        _correr(h.comando_asignar(update, contexto))
        check(
            "admin: /asignar bloqueado para quien no es admin",
            "solo para el equipo" in update.effective_message.replies[-1][0],
        )

        update = _UpdateFake(uid=555, texto="/asignar")
        _correr(h.comando_asignar(update, _ContextoFake(args=[])))
        check(
            "admin: /asignar sin datos muestra el uso",
            "Uso: /asignar" in update.effective_message.replies[-1][0],
        )

        contexto = _ContextoFake(args=["999", "Juan", "CuentaUno", "CuentaDos"])
        update = _UpdateFake(uid=555, texto="/asignar")
        with _parches((h, "_usuarios_en_bd", lambda usuarios: {"cuentauno", "cuentados"})):
            _correr(h.comando_asignar(update, contexto))
        info = clientes_store.cliente_de(999, ruta)
        check(
            "admin: /asignar <id> <nombre> <usuario...> escribe el JSON",
            info == {"nombre": "Juan", "cuentas": ["CuentaUno", "CuentaDos"]},
        )
        check(
            "admin: /asignar confirma con las cuentas agregadas",
            "@CuentaUno" in update.effective_message.replies[-1][0]
            and "Cuentas agregadas" in update.effective_message.replies[-1][0],
        )

        contexto = _ContextoFake(args=["1000", "nombre=Cliente_Piloto", "OtraCuenta"])
        with _parches((h, "_usuarios_en_bd", lambda usuarios: None)):
            _correr(h.comando_asignar(_UpdateFake(uid=555, texto="/asignar"), contexto))
        check(
            "admin: /asignar nombre=Cliente_Piloto (con '_' -> espacio)",
            clientes_store.cliente_de(1000, ruta) == {
                "nombre": "Cliente Piloto",
                "cuentas": ["OtraCuenta"],
            },
        )

        update = _UpdateFake(uid=555, texto="/clientes")
        with _parches((h, "_usuarios_en_bd", lambda usuarios: None)):
            _correr(h.comando_clientes(update, _ContextoFake()))
        check(
            "admin: /clientes lista clientes y sus cuentas",
            "999" in update.effective_message.replies[-1][0]
            and "@CuentaUno" in update.effective_message.replies[-1][0]
            and "1000" in update.effective_message.replies[-1][0],
        )

        contexto = _ContextoFake(args=["999", "CuentaUno"])
        _correr(h.comando_quitar(_UpdateFake(uid=555, texto="/quitar"), contexto))
        check(
            "admin: /quitar quita solo la cuenta pedida",
            clientes_store.cuentas_de(999, ruta) == ["CuentaDos"],
        )

        contexto = _ContextoFake(args=["999", "CuentaDos"])
        update = _UpdateFake(uid=555, texto="/quitar")
        _correr(h.comando_quitar(update, contexto))
        check(
            "admin: /quitar avisa cuando ya no le quedan cuentas",
            "Ya no tiene cuentas" in update.effective_message.replies[-1][0],
        )

        query = _QueryFake("cli_menu")
        update = _UpdateFake(uid=999, query=query)
        _correr(h.cli_callback(update, _ContextoFake()))
        check(
            "admin: cliente sin cuentas sigue pudiendo usar el menu",
            query.message.edits and "cli_ayuda" in _callbacks(query.message.edits[-1][1]["reply_markup"]),
        )

    # -------------------------------------------------------------- GRUPOS --
    check(
        "grupo: helpers de mencion (grupo si, privado no)",
        h.mencion_grupo(
            _UpdateFake(uid=1, chat=_chat_grupo(), username="fulano")
        )
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

    with _store_temporal() as ruta, _env("TELEGRAM_ADMIN_IDS", None):
        clientes_store.asignar("800", "Cliente Grupo", ["CuentaGrupo"], ruta)
        clientes_store.asignar("801", "Otro Cliente", ["CuentaOtra"], ruta)
        grupo = _chat_grupo()

        # Bienvenida al agregar el bot (new_chat_members) + anti-duplicado.
        h._BIENVENIDAS.clear()
        update = _UpdateFake(uid=500, chat=grupo)
        update.effective_message.new_chat_members = [_UsuarioFake(424242)]
        ctx = _ContextoFake()
        _correr(h.bienvenida_grupo(update, ctx))
        check(
            "grupo: bienvenida al agregar el bot con instrucciones",
            bool(update.effective_message.replies)
            and "Escribe /start" in update.effective_message.replies[-1][0],
        )
        _correr(h.bienvenida_grupo(update, ctx))
        check(
            "grupo: la bienvenida NO se duplica",
            len(update.effective_message.replies) == 1,
        )
        # Agregaron a otra persona (no al bot): silencio.
        update_x = _UpdateFake(uid=500, chat=_chat_grupo(-1007777777777))
        update_x.effective_message.new_chat_members = [_UsuarioFake(999999)]
        _correr(h.bienvenida_grupo(update_x, _ContextoFake()))
        check(
            "grupo: no saluda cuando agregan a otra persona",
            update_x.effective_message.replies == [],
        )
        # my_chat_member: entra al chat -> saluda; sigue admin -> silencio.
        update2 = _UpdateFake(uid=None, chat=_chat_grupo(-1009999999999))
        update2.effective_message = None  # un chat_member update no trae mensaje
        update2.my_chat_member = _CambioMiembroFake("left", "member")
        ctx2 = _ContextoFake()
        _correr(h.bienvenida_miembro(update2, ctx2))
        check(
            "grupo: my_chat_member saluda cuando el bot recien entra",
            bool(ctx2.bot.enviados)
            and "Escribe /start" in ctx2.bot.enviados[-1][1],
        )
        update3 = _UpdateFake(uid=None, chat=_chat_grupo(-1008888888888))
        update3.effective_message = None
        update3.my_chat_member = _CambioMiembroFake("administrator", "administrator")
        ctx3 = _ContextoFake()
        _correr(h.bienvenida_miembro(update3, ctx3))
        check(
            "grupo: sin cambio de entrada no hay bienvenida",
            not ctx3.bot.enviados,
        )
        h._BIENVENIDAS.clear()

        # /start de cliente registrado: menu en el grupo, con mencion.
        update = _UpdateFake(uid=800, texto="/start", chat=grupo, username="fulano")
        _correr(h.start(update, _ContextoFake()))
        texto = update.effective_message.replies[0][0]
        check(
            "grupo: /start responde EN el grupo y con mencion",
            "👤 @fulano," in texto and "Toca un botón" in texto,
        )

        # /start de no registrado: UN mensaje claro (sin spam).
        update = _UpdateFake(uid=999, texto="/start", chat=grupo, username="desconocido")
        _correr(h.start(update, _ContextoFake()))
        check(
            "grupo: /start de no registrado recibe UN mensaje claro con mencion",
            len(update.effective_message.replies) == 1
            and "No tengo tu cuenta registrada"
            in update.effective_message.replies[0][0]
            and "👤 @desconocido," in update.effective_message.replies[0][0],
        )

        # /cancelar en grupo.
        update = _UpdateFake(uid=800, texto="/cancelar", chat=grupo, username="fulano")
        _correr(h.cancelar(update, _ContextoFake()))
        check(
            "grupo: /cancelar responde en el grupo con mencion",
            "cancelado" in update.effective_message.replies[-1][0]
            and "👤 @fulano," in update.effective_message.replies[-1][0],
        )

        # Texto SIN flujo de otro miembro: ignorado en silencio.
        update = _UpdateFake(uid=801, texto="hola a todos", chat=grupo, username="otro")
        _correr(h.texto_recibido(update, _ContextoFake()))
        check(
            "grupo: texto de otro miembro SIN flujo se ignora (nada de spam)",
            update.effective_message.replies == [],
        )

        # Aislamiento: el flujo de A no lo captura B (user_data por usuario).
        ctx_a = _ContextoFake()
        ctx_b = _ContextoFake()
        ctx_a.user_data["cli_espera"] = {"tipo": "nombre", "usuario": "CuentaGrupo"}
        update_b = _UpdateFake(uid=801, texto="Nombre de B", chat=grupo, username="otro")
        _correr(h.texto_recibido(update_b, ctx_b))
        check(
            "grupo: el flujo de A no lo captura B (user_data por usuario)",
            update_b.effective_message.replies == []
            and "cli_espera" in ctx_a.user_data,
        )

        # Texto CON flujo (mensaje suelto) -> confirmacion con mencion.
        update = _UpdateFake(uid=800, texto="María López", chat=grupo, username="fulano")
        _correr(h.texto_recibido(update, ctx_a))
        texto, kwargs = update.effective_message.replies[-1]
        check(
            "grupo: texto CON flujo se captura y pide confirmacion con mencion",
            "«María López»" in texto
            and "👤 @fulano," in texto
            and _callbacks(kwargs["reply_markup"])
            == ["nombre_si_CuentaGrupo", "nombre_no_CuentaGrupo"],
        )

        # Texto como RESPUESTA al mensaje del bot -> tambien se captura.
        ctx_a.user_data["cli_espera"] = {"tipo": "nombre", "usuario": "CuentaGrupo"}
        update = _UpdateFake(uid=800, texto="Otro Nombre", chat=grupo, username="fulano")
        update.effective_message.reply_to_message = _MensajeFake(
            "✏️ Escríbeme el nombre nuevo para @CuentaGrupo."
        )
        _correr(h.texto_recibido(update, ctx_a))
        check(
            "grupo: la RESPUESTA al mensaje del bot tambien se captura",
            any("Otro Nombre" in t for t, _ in update.effective_message.replies),
        )

        # Callback valido: codigo en el grupo, con mencion.
        def _datos_totp_grupo(usuario):
            return {
                "usuario": usuario,
                "handle_actual": "",
                "nombre_mostrado": "",
                "totp_secret": SECRETO_FAKE,
            }

        query = _QueryFake("cli_codigo", _MensajeFake())
        with _parches((h, "_datos_cuenta", _datos_totp_grupo)):
            _correr(
                h.cli_callback(
                    _UpdateFake(uid=800, query=query, chat=grupo, username="fulano"),
                    _ContextoFake(),
                )
            )
        ultimo = query.message.edits[-1][0]
        check(
            "grupo: callback valido responde con mencion y el codigo",
            "👤 @fulano," in ultimo
            and "Este es tu código" in ultimo
            and re.search(r"\b\d{6}\b", ultimo) is not None,
        )

        # Botones de otra persona: la cuenta se valida contra quien pulsa.
        query = _QueryFake("cuenta_CuentaGrupo")
        with _parches((h, "_datos_cuenta", _datos_totp_grupo)):
            _correr(
                h.cuenta_callback(
                    _UpdateFake(uid=801, query=query, chat=grupo, username="otro"),
                    _ContextoFake(),
                )
            )
        check(
            "grupo: el boton de una cuenta ajena se rechaza (valida al que pulsa)",
            not query.message.edits
            and bool(query.answers)
            and query.answers[-1][1]
            and "no está disponible" in query.answers[-1][0],
        )

        # Otro miembro no puede confirmar el cambio de nombre ajeno.
        query = _QueryFake("nombre_si_CuentaGrupo")
        _correr(
            h.nombre_callback(
                _UpdateFake(uid=801, query=query, chat=grupo, username="otro"),
                _ContextoFake(),
            )
        )
        check(
            "grupo: otro miembro no confirma el nombre ajeno (boton vencido)",
            not query.message.edits
            and bool(query.answers)
            and query.answers[-1][1]
            and "venció" in query.answers[-1][0],
        )

        # Botones del menu: piden la foto con mencion.
        query = _QueryFake("cli_foto_perfil", _MensajeFake())
        _correr(
            h.cli_callback(
                _UpdateFake(uid=800, query=query, chat=grupo, username="fulano"),
                _ContextoFake(),
            )
        )
        check(
            "grupo: los botones del menu piden la foto con mencion",
            "Envíame la foto de perfil" in query.message.edits[-1][0]
            and "👤 @fulano," in query.message.edits[-1][0],
        )

        # Vista previa de la foto en el grupo.
        with tempfile.TemporaryDirectory() as temporal:
            ctx = _ContextoFake()
            ctx.user_data["cli_espera"] = {
                "tipo": "foto",
                "foto_tipo": "perfil",
                "usuario": "CuentaGrupo",
            }
            update = _UpdateFake(
                uid=800, fotos=[_FotoFake()], chat=grupo, username="fulano"
            )
            with _parches((h, "_data_temp", lambda: temporal)):
                _correr(h.foto_recibida(update, ctx))
            caption = update.effective_message.photos[-1][1]
            check(
                "grupo: la vista previa de la foto lleva mencion",
                "👤 @fulano," in caption and "¿Uso esta foto" in caption,
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
        "main: registra los comandos del cliente y de admin",
        {"start", "ayuda", "cancelar", "clientes", "asignar", "quitar"}.issubset(comandos),
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
        "'if __name__' in fuente"
        and fuente.count("main()") >= 1
        and _app_ok(),
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
        sum(isinstance(x, CommandHandler) for x in handlers) == 7
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

"""Tests del proxy OpenAI compatible (MWX APP) en `ia/generador_contenido.py`.

Objetivo: que el generador pueda apuntar a un endpoint compatible con OpenAI
(``/v1/chat/completions``) mediante ``OPENAI_API_BASE``/``OPENAI_BASE_URL`` o
``settings.openai_api_base`` sin cambiar el comportamiento historico cuando no
hay base configurada (``api.openai.com``).

Cubre, SIN red real (todo `openai` monkeypatcheado):
  (a) ``_config_openai`` normaliza la key/base y respeta settings/env.
  (b) openai 0.28: usa ``openai.api_base`` SOLO si hay base; sin base deja el
      default historico. La key/token se asigna a ``openai.api_key``.
  (c) openai >= 1.0: crea ``OpenAI(api_key=..., base_url=...)``; sin base NO
      pasa ``base_url`` (contrato identico al actual).
  (d) ``generar_contenido`` sigue devolviendo textos con el fake en ambos modos.
  (e) El log DEBUG de configuracion se emite UNA vez y nunca incluye la clave.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_openai_proxy.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import types
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from loguru import logger  # noqa: E402

import ia.generador_contenido as gc  # noqa: E402
from ia.generador_contenido import GeneradorContenido  # noqa: E402

# Token/claves FALSAS de test: nunca se imprimen ni se usan contra la red.
_TOKEN = "sk-test-mwx-token"
_BASE = "https://mwx.example/openai/v1"
_BASE_ENV = "https://proxy.example/v1"


def _respuesta(texto: str):
    """Respuesta minima con la forma `response.choices[0].message.content`."""
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=texto))]
    )


@contextlib.contextmanager
def _config(
    api_key: str = _TOKEN,
    settings_base: str = "",
    env_key: str | None = None,
    env_base: str | None = None,
    env_base_url: str | None = None,
):
    """Aisla settings/env de OpenAI: no filtra la config real del proyecto."""
    entorno = {
        k: v
        for k, v in os.environ.items()
        if k not in ("OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL")
    }
    if env_key is not None:
        entorno["OPENAI_API_KEY"] = env_key
    if env_base is not None:
        entorno["OPENAI_API_BASE"] = env_base
    if env_base_url is not None:
        entorno["OPENAI_BASE_URL"] = env_base_url

    settings_falsos = types.SimpleNamespace(
        openai_api_key=api_key,
        openai_api_base=settings_base,
    )
    with mock.patch.dict(os.environ, entorno, clear=True):
        with mock.patch.object(gc, "settings", settings_falsos):
            yield settings_falsos


def _fake_legacy(contenido: str = "Texto falso"):
    """Simula openai 0.28: modulo con `api_key`/`api_base` y ChatCompletion."""
    llamadas: list = []

    def _create(**kwargs):
        llamadas.append(dict(kwargs))
        return _respuesta(contenido)

    fake = types.SimpleNamespace(
        api_key="",
        api_base="https://api.openai.com/v1",
        ChatCompletion=types.SimpleNamespace(create=_create),
    )
    return fake, llamadas


def _fake_moderno(contenido: str = "Texto falso"):
    """Simula openai >= 1.0: `OpenAI(**kwargs)` + chat.completions.create."""
    clientes: list = []
    llamadas: list = []

    def _create(**kwargs):
        llamadas.append(dict(kwargs))
        return _respuesta(contenido)

    cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_create)
        ),
        images=types.SimpleNamespace(generate=lambda **kwargs: None),
    )

    def _factory(**kwargs):
        clientes.append(dict(kwargs))
        return cliente

    fake = types.SimpleNamespace(api_key="", OpenAI=_factory)
    return fake, clientes, llamadas


# --------------------------------------------------------------------------- #
# (a) Helper de configuracion
# --------------------------------------------------------------------------- #
def test_config_helper(check):
    print("(a) _config_openai: settings/env, recorte y normalizacion")

    with _config(api_key=f"  {_TOKEN}  ", settings_base=f"  {_BASE}/  "):
        api_key, api_base = gc._config_openai()
    check("helper: existe y devuelve una tupla de 2", isinstance((api_key, api_base), tuple))
    check("helper: recorta espacios de la key", api_key == _TOKEN)
    check("helper: normaliza base de settings (strip + sin / final)", api_base == _BASE)

    with _config(api_key=_TOKEN, settings_base="", env_base=f"  {_BASE_ENV}/  "):
        _, api_base = gc._config_openai()
    check("helper: usa OPENAI_API_BASE del entorno", api_base == _BASE_ENV)

    with _config(api_key=_TOKEN, settings_base="", env_base_url=f" {_BASE_ENV} "):
        _, api_base = gc._config_openai()
    check("helper: cae a OPENAI_BASE_URL si falta OPENAI_API_BASE", api_base == _BASE_ENV)

    with _config(api_key=_TOKEN, settings_base=""):
        _, api_base = gc._config_openai()
    check("helper: sin base -> cadena vacia (default historico)", api_base == "")

    with _config(api_key="", settings_base="", env_key=f"  {_TOKEN}  "):
        api_key, _ = gc._config_openai()
    check("helper: usa OPENAI_API_KEY del entorno si settings esta vacio", api_key == _TOKEN)

    with _config(api_key="sk-settings", settings_base=_BASE, env_base="https://otro.example/v1"):
        api_key, api_base = gc._config_openai()
    check("helper: settings tiene prioridad sobre el entorno", (api_key, api_base) == ("sk-settings", _BASE))


# --------------------------------------------------------------------------- #
# (b) openai 0.28 (ruta legacy)
# --------------------------------------------------------------------------- #
def test_legacy_api_base(check):
    print("(b) openai 0.28: api_base solo si hay base, key en api_key")

    # Sin base: NO se toca api_base (default historico de la libreria).
    with _config(api_key=_TOKEN, settings_base=""):
        fake, llamadas = _fake_legacy("Hola desde el fake legacy")
        with mock.patch.object(gc, "openai", fake):
            texto = GeneradorContenido()._chat("prompt de prueba", temperature=0.3)
    check("0.28 sin base: devuelve el texto del fake", texto == "Hola desde el fake legacy")
    check("0.28 sin base: api_base conserva el default historico",
          fake.api_base == "https://api.openai.com/v1", fake.api_base)
    check("0.28: api_key recibe el token", fake.api_key == _TOKEN)
    check("0.28: una sola llamada de chat", len(llamadas) == 1)
    check("0.28: modelo sin cambios", llamadas and llamadas[0].get("model") == "gpt-4o-mini")
    check("0.28: temperatura sin cambios", llamadas and llamadas[0].get("temperature") == 0.3)
    check("0.28: mensajes sin cambios", llamadas and llamadas[0].get("messages") == [{"role": "user", "content": "prompt de prueba"}])

    # Con base: se fija openai.api_base al proxy.
    with _config(api_key=_TOKEN, settings_base=_BASE):
        fake2, _ = _fake_legacy("Hola por el proxy")
        with mock.patch.object(gc, "openai", fake2):
            texto2 = GeneradorContenido()._chat("otro prompt")
    check("0.28 con base: devuelve el texto del proxy", texto2 == "Hola por el proxy")
    check("0.28 con base: api_base apunta al proxy", fake2.api_base == _BASE, fake2.api_base)
    check("0.28 con base: api_key recibe el token", fake2.api_key == _TOKEN)


# --------------------------------------------------------------------------- #
# (c) openai >= 1.0 (cliente)
# --------------------------------------------------------------------------- #
def test_modern_base_url(check):
    print("(c) openai >= 1.0: base_url solo si hay base; api_key siempre")

    # Sin base: el cliente se crea EXACTAMENTE como antes (solo api_key).
    with _config(api_key=_TOKEN, settings_base=""):
        fake, clientes, llamadas = _fake_moderno("Hola moderno sin base")
        with mock.patch.object(gc, "openai", fake):
            texto = GeneradorContenido()._chat("prompt moderno", temperature=0.5)
    check(">=1.0 sin base: devuelve el texto del fake", texto == "Hola moderno sin base")
    check(">=1.0 sin base: un solo cliente creado", len(clientes) == 1)
    check(">=1.0 sin base: NO se pasa base_url", clientes and "base_url" not in clientes[0])
    check(">=1.0 sin base: kwargs identicos al contrato actual", clientes and clientes[0] == {"api_key": _TOKEN})
    check(">=1.0: modelo sin cambios", llamadas and llamadas[0].get("model") == "gpt-4o-mini")
    check(">=1.0: temperatura sin cambios", llamadas and llamadas[0].get("temperature") == 0.5)

    # Con base: base_url exacto, key como api_key y sin kwargs extra.
    with _config(api_key=_TOKEN, settings_base=_BASE):
        fake2, clientes2, _ = _fake_moderno("Hola moderno con proxy")
        with mock.patch.object(gc, "openai", fake2):
            texto2 = GeneradorContenido()._chat("prompt proxy")
    check(">=1.0 con base: devuelve el texto del fake", texto2 == "Hola moderno con proxy")
    check(">=1.0 con base: kwargs exactos (api_key + base_url)",
          clientes2 and clientes2[0] == {"api_key": _TOKEN, "base_url": _BASE})
    check(">=1.0 con base: base_url normalizado sin / final",
          clientes2 and clientes2[0].get("base_url") == _BASE)


# --------------------------------------------------------------------------- #
# (d) generar_contenido sigue funcionando en ambos modos
# --------------------------------------------------------------------------- #
def test_generar_contenido(check):
    print("(d) generar_contenido devuelve textos con el fake (0.28 y >=1.0)")
    textos = [
        "Texto de prueba uno sobre el tema de hoy",
        "Texto de prueba dos distinto para la cuenta",
    ]
    contenido = json.dumps(textos, ensure_ascii=False)

    with _config(api_key=_TOKEN, settings_base=_BASE):
        fake, clientes, _ = _fake_moderno(contenido)
        with mock.patch.object(gc, "openai", fake):
            resultado = GeneradorContenido().generar_contenido(
                "mantenimiento", "narrativa de prueba", cantidad=2
            )
    check("generar_contenido >=1.0: devuelve 2 textos", len(resultado) == 2, len(resultado))
    check("generar_contenido >=1.0: conserva el primer texto", textos[0] in resultado)
    check("generar_contenido >=1.0: conserva el segundo texto", textos[1] in resultado)
    check("generar_contenido >=1.0: uso base_url del proxy",
          clientes and clientes[0].get("base_url") == _BASE)

    with _config(api_key=_TOKEN, settings_base=""):
        fake2, _ = _fake_legacy(contenido)
        with mock.patch.object(gc, "openai", fake2):
            resultado2 = GeneradorContenido().generar_contenido(
                "mantenimiento", "narrativa de prueba", cantidad=2
            )
    check("generar_contenido 0.28: devuelve 2 textos", len(resultado2) == 2, len(resultado2))
    check("generar_contenido 0.28: conserva los textos", textos[0] in resultado2 and textos[1] in resultado2)
    check("generar_contenido 0.28: api_base sigue en el default",
          fake2.api_base == "https://api.openai.com/v1")


# --------------------------------------------------------------------------- #
# (e) Log DEBUG unico y sin clave
# --------------------------------------------------------------------------- #
def test_log_unico_sin_clave(check):
    print("(e) log DEBUG: una vez por proceso y sin la key")
    mensajes: list = []

    def _sink(mensaje):
        # Con `format="{message}"` loguru entrega el texto ya formateado.
        mensajes.append(str(mensaje))

    flag_original = gc._OPENAI_DEBUG_LOGUEADO
    sink_id = logger.add(_sink, level="DEBUG", format="{message}")
    try:
        with _config(api_key=_TOKEN, settings_base=_BASE):
            gc._OPENAI_DEBUG_LOGUEADO = False
            gc._log_config_openai("gpt-4o-mini")
            gc._log_config_openai("gpt-4o-mini")
    finally:
        logger.remove(sink_id)
        gc._OPENAI_DEBUG_LOGUEADO = flag_original

    check("log: se emite UNA sola vez", len(mensajes) == 1, len(mensajes))
    if mensajes:
        mensaje = mensajes[0]
        check("log: incluye base", f"base={_BASE}" in mensaje)
        check("log: incluye modelo", "modelo=gpt-4o-mini" in mensaje)
        check("log: NO incluye la key/token", _TOKEN not in mensaje)
    else:
        check("log: incluye base", False)
        check("log: incluye modelo", False)
        check("log: NO incluye la key/token", False)


# --------------------------------------------------------------------------- #
# (f) Guardas estaticas: no quedan construcciones directas de OpenAI
# --------------------------------------------------------------------------- #
def test_sin_construcciones_directas(check):
    print("(f) generador_contenido usa el helper en todas las rutas")
    fuente = Path(gc.__file__).read_text(encoding="utf-8")
    check("fuente: existe _config_openai", "def _config_openai(" in fuente)
    check("fuente: no queda openai.OpenAI(api_key=...)", "openai.OpenAI(api_key=" not in fuente)
    check("fuente: no queda api_key tomada directo de settings",
          "openai.api_key = settings" not in fuente)
    check("fuente: construye clientes >=1.0 via helper",
          fuente.count("_opciones_cliente_openai(api_key, api_base)") >= 2)
    check("fuente: lee la config en init, chat e imagen",
          fuente.count("_config_openai()") >= 3)


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_config_helper(check)
    test_legacy_api_base(check)
    test_modern_base_url(check)
    test_generar_contenido(check)
    test_log_unico_sin_clave(check)
    test_sin_construcciones_directas(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

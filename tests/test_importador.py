"""Tests rapidos de `cuentas/importador.py` (sin red, sin Chrome y sin BD real).

Cubre el parseo de lineas "Aged" que traen el User-Agent y el JSON de cookies
en la MISMA linea: la version anterior hacia `split(':')` ANTES de extraer el
JSON, asi que los `:` de los valores rompian los campos base. Ahora ambos
bloques se extraen de forma segura (escaner balanceado + `json.loads` + patron
`Mozilla/...`) y despues se parsean los 6 campos base.

  (1-2)  6 campos clasicos y 7 con cookies base64 (compatibilidad antigua).
  (3-4)  7 campos con JSON crudo `[{...}]` y `{"cookies":[...]}` (con espacios).
  (5-6)  8 campos (JSON/base64 + UA posicional).
  (7-8)  7 campos con SOLO UA y UA etiquetado (`ua=`/`user_agent=`) en cualquier
         posicion.
  (9)    Caso Aged realista y su variante con el UA ANTES del JSON.
  (10)   JSON en posicion intermedia sin desalinear los 6 campos base.
  (11)   JSON con `:` dentro de los valores (value/expiry/domain).
  (12)   Campos faltantes se rellenan con "".
  (13)   Linea vacia / comentario / basura sin campos -> None.
  (14)   Campo extra >8 no vacio -> None (y 7º/8º vacios se ignoran).
  (15)   Persistencia en `importar_una` con sesion falsa (monkeypatch): inyecta
         `cookies_json`/`user_agent` al crear y NO pisa los de una existente
         con una linea de 6 campos.
  (16)   `decodificar_cookies` (JSON crudo lista/envuelto + base64 + basura).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_importador.py   (solo este archivo)
"""
from __future__ import annotations

import base64
import contextlib
import json
import sys
import types
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from cuentas import importador  # noqa: E402
from cuentas.importador import decodificar_cookies, parsear_linea  # noqa: E402

# --------------------------------------------------------------------------- #
# Datos de prueba
# --------------------------------------------------------------------------- #
BASE = "aged_user:clave:ABCDEF:correo@x.com:mailpass:tok123"
BASE_CAMPOS = ("aged_user", "clave", "ABCDEF", "correo@x.com", "mailpass", "tok123")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
COOKIES = [
    {
        "name": "auth_token",
        "value": "a:b:c",
        "domain": ".x.com",
        "path": "/",
        "secure": True,
        "expiry": 1800000000,
    },
    {
        "name": "ct0",
        "value": "token:con:dos:puntos",
        "domain": ".x.com",
        "path": "/",
        "expiry": "2026-01-01T00:00:00Z",
    },
]
JSON_COOKIES = json.dumps(COOKIES, ensure_ascii=False)
JSON_ENVUELTO = json.dumps({"cookies": COOKIES, "origen": "aged"}, ensure_ascii=False)
B64 = base64.b64encode(JSON_COOKIES.encode("utf-8")).decode("ascii")
CLAVES_CONTRATO = {
    "username",
    "password",
    "totp_secret",
    "email",
    "email_password",
    "auth_token",
    "cookies",
    "user_agent",
}


def _campos_base(fields: dict) -> tuple:
    """Los 6 campos base en orden, para comparar de una sola vez."""
    return (
        fields.get("username"),
        fields.get("password"),
        fields.get("totp_secret"),
        fields.get("email"),
        fields.get("email_password"),
        fields.get("auth_token"),
    )


# --------------------------------------------------------------------------- #
# (1-2) Formatos clasicos: 6 campos y 7 con cookies base64
# --------------------------------------------------------------------------- #
def test_clasicos(check):
    print("(1-2) 6 campos clasicos y 7 con cookies base64")
    fields = parsear_linea(BASE)
    check("6 campos: no devuelve None", fields is not None)
    check("6 campos: los 6 campos base", _campos_base(fields) == BASE_CAMPOS)
    check("6 campos: cookies None", fields.get("cookies") is None)
    check("6 campos: user_agent vacio", fields.get("user_agent") == "")
    check("6 campos: contrato exacto de claves", set(fields) == CLAVES_CONTRATO)

    b64 = parsear_linea(f"{BASE}:{B64}")  # compatibilidad antigua
    check("7 base64: no devuelve None", b64 is not None)
    check("7 base64: cookies decodificadas a la lista original", b64.get("cookies") == COOKIES)
    check("7 base64: user_agent vacio", b64.get("user_agent") == "")
    check("7 base64: campos base intactos", _campos_base(b64) == BASE_CAMPOS)


# --------------------------------------------------------------------------- #
# (3-4) JSON crudo y envuelto en el 7º campo
# --------------------------------------------------------------------------- #
def test_json_crudo(check):
    print("(3-4) JSON crudo `[{...}]` y `{\"cookies\":[...]}` en el 7º campo")
    crudo = parsear_linea(f"{BASE}:{JSON_COOKIES}")
    check("7 JSON: no devuelve None", crudo is not None)
    check("7 JSON: cookies == lista del JSON", crudo.get("cookies") == COOKIES)
    check("7 JSON: campos base intactos", _campos_base(crudo) == BASE_CAMPOS)

    envuelto = parsear_linea(f"{BASE}:{JSON_ENVUELTO}")
    check("7 JSON envuelto (con espacios): no devuelve None", envuelto is not None)
    check("7 JSON envuelto: extrae la lista de 'cookies'", envuelto.get("cookies") == COOKIES)
    check("7 JSON envuelto: campos base intactos", _campos_base(envuelto) == BASE_CAMPOS)


# --------------------------------------------------------------------------- #
# (5-6) 8 campos: cookies + UA posicional
# --------------------------------------------------------------------------- #
def test_ocho_campos(check):
    print("(5-6) 8 campos con cookies (JSON/base64) + UA posicional")
    f5 = parsear_linea(f"{BASE}:{JSON_COOKIES}:{UA}")
    check("8 JSON+UA: no devuelve None", f5 is not None)
    check("8 JSON+UA: cookies == lista", f5.get("cookies") == COOKIES)
    check("8 JSON+UA: user_agent exacto", f5.get("user_agent") == UA)
    check("8 JSON+UA: campos base intactos", _campos_base(f5) == BASE_CAMPOS)

    f6 = parsear_linea(f"{BASE}:{B64}:{UA}")
    check("8 base64+UA: no devuelve None", f6 is not None)
    check("8 base64+UA: cookies decodificadas", f6.get("cookies") == COOKIES)
    check("8 base64+UA: user_agent exacto", f6.get("user_agent") == UA)
    check("8 base64+UA: campos base intactos", _campos_base(f6) == BASE_CAMPOS)


# --------------------------------------------------------------------------- #
# (7-8) UA posicional solo y UA etiquetado en cualquier posicion
# --------------------------------------------------------------------------- #
def test_user_agent(check):
    print("(7-8) 7 campos solo UA y UA etiquetado ua=/user_agent=")
    solo_ua = parsear_linea(f"{BASE}:{UA}")
    check("7 solo UA: no devuelve None", solo_ua is not None)
    check("7 solo UA: cookies None", solo_ua.get("cookies") is None)
    check("7 solo UA: user_agent exacto", solo_ua.get("user_agent") == UA)
    check("7 solo UA: campos base intactos", _campos_base(solo_ua) == BASE_CAMPOS)

    al_inicio = parsear_linea(f"ua={UA}:{BASE}")
    check("ua= al inicio: user_agent exacto", al_inicio.get("user_agent") == UA)
    check("ua= al inicio: campos base intactos", _campos_base(al_inicio) == BASE_CAMPOS)

    al_final = parsear_linea(f"{BASE}:user_agent={UA}")
    check("user_agent= al final: user_agent exacto", al_final.get("user_agent") == UA)
    check("user_agent= al final: campos base intactos", _campos_base(al_final) == BASE_CAMPOS)

    en_medio = parsear_linea(
        f"aged_user:clave:useragent={UA}:ABCDEF:correo@x.com:mailpass:tok123"
    )
    check("useragent= en medio: user_agent exacto", en_medio.get("user_agent") == UA)
    check("useragent= en medio: campos base intactos", _campos_base(en_medio) == BASE_CAMPOS)

    con_json = parsear_linea(f"{BASE}:{JSON_COOKIES}:ua={UA}")
    check("ua= junto al JSON: cookies y UA", con_json.get("cookies") == COOKIES and con_json.get("user_agent") == UA)
    check("ua= junto al JSON: campos base intactos", _campos_base(con_json) == BASE_CAMPOS)


# --------------------------------------------------------------------------- #
# (9-11) Caso Aged realista, orden invertido, posicion intermedia y `:` internos
# --------------------------------------------------------------------------- #
def test_aged_realista(check):
    print("(9-11) lote Aged realista (JSON y UA en la misma linea)")
    linea = f"{BASE}:{JSON_COOKIES}:{UA}"
    fields = parsear_linea(linea)
    check("Aged: no devuelve None", fields is not None)
    check("Aged: 6 campos base intactos", _campos_base(fields) == BASE_CAMPOS)
    check("Aged: cookies con ':' en los valores", fields.get("cookies") == COOKIES)
    check(
        "Aged: valor ':' preservado tal cual",
        fields["cookies"][0]["value"] == "a:b:c" and fields["cookies"][1]["value"] == "token:con:dos:puntos",
    )
    check("Aged: expiry con ':' preservado", fields["cookies"][1]["expiry"] == "2026-01-01T00:00:00Z")
    check("Aged: user_agent exacto", fields.get("user_agent") == UA)

    invertida = parsear_linea(f"{BASE}:{UA}:{JSON_COOKIES}")
    check("Aged invertida (UA antes del JSON): no devuelve None", invertida is not None)
    check("Aged invertida: campos base intactos", _campos_base(invertida) == BASE_CAMPOS)
    check("Aged invertida: cookies == lista", invertida.get("cookies") == COOKIES)
    check("Aged invertida: user_agent exacto", invertida.get("user_agent") == UA)

    intermedia = parsear_linea(
        f"aged_user:clave:ABCDEF:{JSON_COOKIES}:correo@x.com:mailpass:tok123"
    )
    check("JSON tras el totp: no devuelve None", intermedia is not None)
    check("JSON tras el totp: campos base intactos", _campos_base(intermedia) == BASE_CAMPOS)
    check("JSON tras el totp: cookies == lista", intermedia.get("cookies") == COOKIES)


# --------------------------------------------------------------------------- #
# (12) Campos faltantes
# --------------------------------------------------------------------------- #
def test_campos_faltantes(check):
    print("(12) campos faltantes se rellenan con \"\"")
    fields = parsear_linea("solo_user:solo_pass")
    check("faltantes: no devuelve None", fields is not None)
    check(
        "faltantes: totp/email/email_password/auth_token en \"\"",
        fields.get("totp_secret") == ""
        and fields.get("email") == ""
        and fields.get("email_password") == ""
        and fields.get("auth_token") == "",
    )
    check(
        "faltantes: conserva username/password",
        (fields.get("username"), fields.get("password")) == ("solo_user", "solo_pass"),
    )

    tres = parsear_linea("solo_user:solo_pass:ABC123")
    check("faltantes (3 campos): totp conservado", tres.get("totp_secret") == "ABC123")
    check("faltantes (3 campos): resto en \"\"", tres.get("email") == "" and tres.get("auth_token") == "")


# --------------------------------------------------------------------------- #
# (13-14) Lineas invalidas y extras
# --------------------------------------------------------------------------- #
def test_invalidas(check):
    print("(13-14) vacia/comentario/basura -> None; extra >8 no vacio -> None")
    check("vacia: None", parsear_linea("") is None)
    check("solo espacios: None", parsear_linea("    ") is None)
    check("comentario '#' : None", parsear_linea("# lote del vendedor") is None)
    check("solo separadores: None", parsear_linea(":::::") is None)
    check("solo JSON (sin credenciales): None", parsear_linea(JSON_COOKIES) is None)
    check("solo UA (sin credenciales): None", parsear_linea(UA) is None)

    check("9 campos con valor no vacio: None", parsear_linea("a:b:c:d:e:f:Z2c=:h:i") is None)
    check("10 campos con valores no vacios: None", parsear_linea("a:b:c:d:e:f:g:h:i:j") is None)
    check(
        "campo extra tras el 8º no vacio: None",
        parsear_linea(f"{BASE}:{B64}:{UA}:extra1:extra2") is None,
    )

    vacios = parsear_linea(f"{BASE}:{B64}:{UA}:")
    check("7º/8º vacios (':' final): se ignoran sin error", vacios is not None)
    check(
        "7º/8º vacios: cookies y UA correctos",
        vacios.get("cookies") == COOKIES and vacios.get("user_agent") == UA,
    )
    doble = parsear_linea(f"{BASE}::")
    check("solo '::' al final (6 campos): se ignoran", doble is not None and doble.get("cookies") is None)


# --------------------------------------------------------------------------- #
# (15) Persistencia en importar_una (sesion falsa)
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, resultado):
        self._resultado = resultado

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._resultado


class _FakeDB:
    def __init__(self, cuenta=None):
        self.cuenta = cuenta
        self.agregadas = []

    def query(self, modelo):
        return _FakeQuery(self.cuenta)

    def add(self, objeto):
        self.agregadas.append(objeto)


@contextlib.contextmanager
def _sesion_con(db):
    yield db


def _cuenta_existente(**extra):
    """Objeto fake con los atributos que `importar_una` toca."""
    datos = dict(
        usuario="aged_user",
        password="vieja",
        totp_secret="",
        email="",
        email_password="",
        auth_token="tok_viejo",
        cookies_json=[{"name": "vieja", "value": "1"}],
        user_agent="UA viejo",
        status="imported",
        last_checked=None,
        seccion="",
        tipo_cuenta="",
    )
    datos.update(extra)
    return types.SimpleNamespace(**datos)


def test_persistencia(check):
    print("(15) importar_una inyecta cookies_json/user_agent y no pisa con 6 campos")
    fields = parsear_linea(f"{BASE}:{JSON_COOKIES}:{UA}")
    sesion_nueva = _FakeDB(None)
    with mock.patch.object(
        importador, "get_db_session", lambda: _sesion_con(sesion_nueva)
    ):
        resultado = importador.importar_una(fields)

    check("nueva: resultado 'nueva'", resultado == "nueva")
    check("nueva: se agrego 1 cuenta", len(sesion_nueva.agregadas) == 1)
    agregada = sesion_nueva.agregadas[0]
    check("nueva: cookies_json == lista extraida del JSON", agregada.cookies_json == COOKIES)
    check("nueva: user_agent == UA extraido", agregada.user_agent == UA)
    check(
        "nueva: usuario y plataforma",
        agregada.usuario == "aged_user" and agregada.plataforma == "twitter",
    )
    check("nueva: auth_token persistido", agregada.auth_token == "tok123")

    cuenta = _cuenta_existente()
    sesion_existente = _FakeDB(cuenta)
    with mock.patch.object(
        importador, "get_db_session", lambda: _sesion_con(sesion_existente)
    ):
        resultado2 = importador.importar_una(parsear_linea(BASE))

    check("existente: resultado 'actualizada'", resultado2 == "actualizada")
    check(
        "existente 6 campos: NO pisa cookies_json",
        cuenta.cookies_json == [{"name": "vieja", "value": "1"}],
    )
    check("existente 6 campos: NO pisa user_agent", cuenta.user_agent == "UA viejo")
    check(
        "existente 6 campos: SI actualiza credenciales no vacias",
        cuenta.password == "clave" and cuenta.auth_token == "tok123",
    )

    cuenta2 = _cuenta_existente()
    sesion2 = _FakeDB(cuenta2)
    with mock.patch.object(importador, "get_db_session", lambda: _sesion_con(sesion2)):
        importador.importar_una(parsear_linea(f"{BASE}:{JSON_COOKIES}:{UA}"))

    check(
        "existente con linea Aged: SI pisa cookies_json",
        cuenta2.cookies_json == COOKIES,
    )
    check("existente con linea Aged: SI pisa user_agent", cuenta2.user_agent == UA)


# --------------------------------------------------------------------------- #
# (16) decodificar_cookies intacta
# --------------------------------------------------------------------------- #
def test_decodificar_cookies(check):
    print("(16) decodificar_cookies acepta JSON crudo/base64 y basura -> None")
    check("decodificar JSON lista", decodificar_cookies(JSON_COOKIES) == COOKIES)
    check("decodificar JSON envuelto", decodificar_cookies(JSON_ENVUELTO) == COOKIES)
    check("decodificar base64 de lista", decodificar_cookies(B64) == COOKIES)

    envuelto_b64 = base64.b64encode(JSON_ENVUELTO.encode("utf-8")).decode("ascii")
    check("decodificar base64 de objeto envuelto", decodificar_cookies(envuelto_b64) == COOKIES)
    check("decodificar vacio -> None", decodificar_cookies("") is None)
    check("decodificar basura -> None", decodificar_cookies("!!!esto no es base64!!!") is None)
    check("decodificar JSON malformado -> None", decodificar_cookies('[{"name": "x"') is None)
    check("decodificar numero JSON -> None", decodificar_cookies("123") is None)


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_clasicos(check)
    test_json_crudo(check)
    test_ocho_campos(check)
    test_user_agent(check)
    test_aged_realista(check)
    test_campos_faltantes(check)
    test_invalidas(check)
    test_persistencia(check)
    test_decodificar_cookies(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_importador.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

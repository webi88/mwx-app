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
  (17-18) Formato vendedor de 7 campos (`usuario:pass:totp:email:mailpass:ct0:auth_token`)
         con y sin User-Agent (posicional y etiquetado).
  (19-21) Desambiguacion: el formato clasico de cookies gana; 7º campo no plano
         -> None (con warning) y vendedor con ct0 vacio.
  (22)   Persistencia del formato vendedor en `importar_una` (sesion falsa).
  (23-26) Heuristica por FORMA (columnas del proveedor en CUALQUIER orden):
         email/totp intercambiados, email en la primera columna, token en medio,
         token al final con UA posicional y con JSON de cookies + persistencia
         de una linea desordenada en `importar_una`.
  (27-28) Conflictos de la heuristica: prioridad de la posicion canonica (una
         password con forma de token NO pisa al token real), token-shaped sin
         auth real (comportamiento documentado, el parseo nunca rompe) y email
         sin TLD valido no detectado.

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

# --------------------------------------------------------------------------- #
# Formato vendedor de 7 campos (`...:ct0:auth_token`). TODOS los datos son
# SINTETICOS (nunca credenciales reales del vendedor).
# --------------------------------------------------------------------------- #
CT0_VENDEDOR = "0f1e2d3c4b5a69788796a5b4c3d2e1f0" * 2  # 64 chars hex
TOKEN_VENDEDOR = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"  # 40 chars hex
VENDEDOR_BASE = "vend_user:vpass:VTOTP:vcorreo@x.com:vmailpass"
VENDEDOR = f"{VENDEDOR_BASE}:{CT0_VENDEDOR}:{TOKEN_VENDEDOR}"
VENDEDOR_CAMPOS = (
    "vend_user",
    "vpass",
    "VTOTP",
    "vcorreo@x.com",
    "vmailpass",
    TOKEN_VENDEDOR,
)
COOKIES_VENDEDOR = [
    {"name": "ct0", "value": CT0_VENDEDOR, "domain": ".x.com", "path": "/"},
    {"name": "auth_token", "value": TOKEN_VENDEDOR, "domain": ".x.com", "path": "/"},
]
# base64 100% alfanumerico (sin `+`, `/` ni `=`) de una lista de cookies valida:
# es "plano" para el regex del formato vendedor, pero decodifica a cookies, asi
# que gana la interpretacion clasica (el auth_token sale del 6º campo).
JSON_PLANO_B64 = (
    "W3sibmFtZSI6ICJjdDAiLCAidmFsdWUiOiAiNjUxMzI3MjY5ZTBkMzdmMmE3NGRlNDUyZTZiNDM4In1d"
)
COOKIES_PLANO = [{"name": "ct0", "value": "651327269e0d37f2a74de452e6b438"}]


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


# --------------------------------------------------------------------------- #
# (17-18) Formato vendedor de 7 campos: ct0 + auth_token, con y sin UA
# --------------------------------------------------------------------------- #
def test_formato_vendedor(check):
    print("(17-18) formato vendedor 7 campos (ct0 + auth_token) con y sin UA")
    fields = parsear_linea(VENDEDOR)
    check("vendedor: no devuelve None", fields is not None)
    check("vendedor: campos base (auth_token == 7º campo)", _campos_base(fields) == VENDEDOR_CAMPOS)
    check("vendedor: email == 4º campo", fields.get("email") == "vcorreo@x.com")
    check("vendedor: email_password == 5º campo", fields.get("email_password") == "vmailpass")
    check("vendedor: totp_secret == 3er campo", fields.get("totp_secret") == "VTOTP")
    check("vendedor: cookies == 2 entradas exactas (orden y claves)", fields.get("cookies") == COOKIES_VENDEDOR)
    check(
        "vendedor: cookies[0]=ct0 (6º campo) y cookies[1]=auth_token (7º campo)",
        fields["cookies"][0]["value"] == CT0_VENDEDOR
        and fields["cookies"][1]["value"] == TOKEN_VENDEDOR,
    )
    check(
        "vendedor: cada cookie trae SOLO name/value/domain/path",
        all(set(cookie) == {"name", "value", "domain", "path"} for cookie in fields["cookies"]),
    )
    check("vendedor: user_agent vacio", fields.get("user_agent") == "")
    check("vendedor: contrato exacto de claves", set(fields) == CLAVES_CONTRATO)

    con_ua = parsear_linea(f"{VENDEDOR}:{UA}")
    check("vendedor+UA posicional: no devuelve None", con_ua is not None)
    check("vendedor+UA posicional: campos base intactos", _campos_base(con_ua) == VENDEDOR_CAMPOS)
    check("vendedor+UA posicional: cookies == vendedor", con_ua.get("cookies") == COOKIES_VENDEDOR)
    check("vendedor+UA posicional: user_agent extraido antes", con_ua.get("user_agent") == UA)

    con_ua_eq = parsear_linea(f"{VENDEDOR}:ua={UA}")
    check("vendedor+ua=: campos base intactos", _campos_base(con_ua_eq) == VENDEDOR_CAMPOS)
    check(
        "vendedor+ua=: cookies y user_agent",
        con_ua_eq.get("cookies") == COOKIES_VENDEDOR and con_ua_eq.get("user_agent") == UA,
    )

    ua_antes = parsear_linea(f"user_agent={UA}:{VENDEDOR}")
    check(
        "user_agent= antes del vendedor: cookies/auth_token/UA",
        ua_antes.get("cookies") == COOKIES_VENDEDOR
        and ua_antes.get("auth_token") == TOKEN_VENDEDOR
        and ua_antes.get("user_agent") == UA,
    )


# --------------------------------------------------------------------------- #
# (19-21) Desambiguacion: el clasico gana; invalidos y ct0 vacio
# --------------------------------------------------------------------------- #
def test_vendedor_desambiguacion(check):
    print("(19-21) clasico gana, 7º no plano -> None y vendedor con ct0 vacio")
    b64 = parsear_linea(f"{BASE}:{B64}")
    check("7 base64 clasico (no regresion): cookies", b64.get("cookies") == COOKIES)
    check("7 base64 clasico (no regresion): auth_token = 6º campo", b64.get("auth_token") == "tok123")

    plano = parsear_linea(f"{BASE}:{JSON_PLANO_B64}")
    check(
        "7 plano que decodifica a cookies: gana el clasico",
        plano is not None and plano.get("cookies") == COOKIES_PLANO,
    )
    check("7 plano que decodifica a cookies: auth_token = 6º campo", plano.get("auth_token") == "tok123")

    check("7º no plano ('!!!no-valido!!!'): None", parsear_linea(f"{BASE}:!!!no-valido!!!") is None)
    check("7º no plano ('a=b=c'): None", parsear_linea(f"{BASE}:a=b=c") is None)
    check("7º no plano ('YWJj+ZGVm'): None", parsear_linea(f"{BASE}:YWJj+ZGVm") is None)

    vacio = parsear_linea(f"{VENDEDOR_BASE}::{TOKEN_VENDEDOR}")
    check("vendedor sin ct0: no devuelve None", vacio is not None)
    check("vendedor sin ct0: auth_token == 7º campo", vacio.get("auth_token") == TOKEN_VENDEDOR)
    check(
        "vendedor sin ct0: cookies solo con auth_token",
        vacio.get("cookies")
        == [{"name": "auth_token", "value": TOKEN_VENDEDOR, "domain": ".x.com", "path": "/"}],
    )


# --------------------------------------------------------------------------- #
# (22) Persistencia del formato vendedor en importar_una (sesion falsa)
# --------------------------------------------------------------------------- #
def test_vendedor_persistencia(check):
    print("(22) persistencia del formato vendedor en importar_una")
    fields = parsear_linea(VENDEDOR)
    sesion = _FakeDB(None)
    with mock.patch.object(importador, "get_db_session", lambda: _sesion_con(sesion)):
        resultado = importador.importar_una(fields)

    check("vendedor nueva: resultado 'nueva'", resultado == "nueva")
    check("vendedor nueva: se agrego 1 cuenta", len(sesion.agregadas) == 1)
    agregada = sesion.agregadas[0]
    check("vendedor nueva: cookies_json == 2 entradas", agregada.cookies_json == COOKIES_VENDEDOR)
    check("vendedor nueva: auth_token == 7º campo", agregada.auth_token == TOKEN_VENDEDOR)
    check(
        "vendedor nueva: email/email_password persistidos",
        agregada.email == "vcorreo@x.com" and agregada.email_password == "vmailpass",
    )

    cuenta = _cuenta_existente(
        usuario="vend_user",
        auth_token="tok_viejo",
        cookies_json=[{"name": "vieja", "value": "1"}],
    )
    with mock.patch.object(importador, "get_db_session", lambda: _sesion_con(_FakeDB(cuenta))):
        resultado2 = importador.importar_una(fields)

    check("vendedor existente: resultado 'actualizada'", resultado2 == "actualizada")
    check("vendedor existente: pisa cookies_json", cuenta.cookies_json == COOKIES_VENDEDOR)
    check("vendedor existente: pisa auth_token", cuenta.auth_token == TOKEN_VENDEDOR)

    # Re-importar 6 campos con auth_token vacio NO pisa la sesion (semantica actual).
    with mock.patch.object(importador, "get_db_session", lambda: _sesion_con(_FakeDB(cuenta))):
        importador.importar_una(parsear_linea(f"{VENDEDOR_BASE}:"))

    check(
        "vendedor + 6 campos con auth_token vacio: NO pisa auth_token",
        cuenta.auth_token == TOKEN_VENDEDOR,
    )
    check(
        "vendedor + 6 campos con auth_token vacio: NO pisa cookies_json",
        cuenta.cookies_json == COOKIES_VENDEDOR,
    )


# --------------------------------------------------------------------------- #
# (23-28) Heuristica por FORMA: columnas del proveedor en cualquier orden.
# Todos los datos son SINTETICOS (nunca credenciales reales).
# --------------------------------------------------------------------------- #
TOTP_B32 = "JBSWY3DPEHPK3PXP"  # 16 chars base32 mayusculas (regla 2)
OTRO_TOKEN = "9f8e7d6c5b4a39281706f5e4d3c2b1a099887766"  # otros 40 hex (regla 1)


def test_heuristica_desorden(check):
    print("(23-26) heuristica: email/totp/token reasignados sin importar la columna")
    # (23) email y totp intercambiados (cada uno en la posicion canonica del otro).
    campos = parsear_linea(
        f"u_des:pass_des:mail@yahoo.com:{TOTP_B32}:mails_des:{TOKEN_VENDEDOR}"
    )
    check("intercambiados: no devuelve None", campos is not None)
    check("intercambiados: contrato exacto de claves", set(campos) == CLAVES_CONTRATO)
    check(
        "intercambiados: los 6 campos reasignados",
        _campos_base(campos)
        == ("u_des", "pass_des", TOTP_B32, "mail@yahoo.com", "mails_des", TOKEN_VENDEDOR),
    )
    check(
        "intercambiados: cookies/UA vacios",
        campos.get("cookies") is None and campos.get("user_agent") == "",
    )

    # (24) email en la PRIMERA columna (usuario/password corren una posicion).
    primera = parsear_linea(
        f"mail@example.com:usuario_des:clave_des:{TOTP_B32}:{TOKEN_VENDEDOR}:correo_pass"
    )
    check(
        "email primero: los 6 campos correctos",
        _campos_base(primera)
        == ("usuario_des", "clave_des", TOTP_B32, "mail@example.com", "correo_pass", TOKEN_VENDEDOR),
    )

    # (25) token en medio (columna de email_pass) con "mails" en la canonica.
    medio = parsear_linea(
        f"u_mid:pass_mid:{TOTP_B32}:mail@example.com:{TOKEN_VENDEDOR}:mails_mid"
    )
    check(
        "token en medio: los 6 campos correctos",
        _campos_base(medio)
        == ("u_mid", "pass_mid", TOTP_B32, "mail@example.com", "mails_mid", TOKEN_VENDEDOR),
    )

    # (26) token al final con UA posicional y con JSON de cookies.
    con_ua = parsear_linea(
        f"u_ua:pass_ua:{TOTP_B32}:mail@example.com:mails_ua:{TOKEN_VENDEDOR}:{UA}"
    )
    check(
        "token final + UA: 6 campos + user_agent",
        _campos_base(con_ua)
        == ("u_ua", "pass_ua", TOTP_B32, "mail@example.com", "mails_ua", TOKEN_VENDEDOR)
        and con_ua.get("user_agent") == UA
        and con_ua.get("cookies") is None,
    )

    con_json = parsear_linea(
        f"u_js:pass_js:mail@example.com:{TOTP_B32}:{TOKEN_VENDEDOR}:mails_js:{JSON_COOKIES}"
    )
    check(
        "token final + JSON: 6 campos + cookies",
        _campos_base(con_json)
        == ("u_js", "pass_js", TOTP_B32, "mail@example.com", "mails_js", TOKEN_VENDEDOR)
        and con_json.get("cookies") == COOKIES,
    )

    # Persistencia de una linea desordenada (sesion falsa): llega bien a la BD.
    sesion = _FakeDB(None)
    with mock.patch.object(importador, "get_db_session", lambda: _sesion_con(sesion)):
        resultado = importador.importar_una(campos)
    check("desorden persistido: resultado 'nueva'", resultado == "nueva")
    agregada = sesion.agregadas[0]
    check(
        "desorden persistido: auth_token/totp/email correctos",
        agregada.auth_token == TOKEN_VENDEDOR
        and agregada.totp_secret == TOTP_B32
        and agregada.email == "mail@yahoo.com",
    )


def test_heuristica_conflictos(check):
    print("(27-28) heuristica: prioridad canonica y password con forma de token")
    # (27) Varios candidatos a auth_token: gana la posicion canonica (6º campo)
    # y la password con forma de token NO se toca (nunca se pisan valores).
    con_auth = parsear_linea(
        f"u_conf:{TOKEN_VENDEDOR}:{TOTP_B32}:mail@example.com:mails_conf:{OTRO_TOKEN}"
    )
    check("conflicto: no devuelve None", con_auth is not None)
    check(
        "conflicto: los 6 campos (auth canonico, password conservada)",
        _campos_base(con_auth)
        == ("u_conf", TOKEN_VENDEDOR, TOTP_B32, "mail@example.com", "mails_conf", OTRO_TOKEN),
    )

    # (28) Password con forma de token SIN auth real: comportamiento DOCUMENTADO
    # (el valor con forma de token se interpreta como auth_token, la senal mas
    # fuerte; el parseo NUNCA rompe y devuelve el contrato completo).
    sin_auth = parsear_linea(
        f"u_conf2:{TOKEN_VENDEDOR}:{TOTP_B32}:mail@example.com:mails_conf2"
    )
    check("sin auth real: no devuelve None", sin_auth is not None)
    check("sin auth real: contrato exacto de claves", set(sin_auth) == CLAVES_CONTRATO)
    check(
        "sin auth real: el token-shaped se reclama como auth_token",
        sin_auth.get("auth_token") == TOKEN_VENDEDOR,
    )
    check(
        "sin auth real: no detectados conservan su orden (user/pass/mailpass)",
        (sin_auth.get("username"), sin_auth.get("password"), sin_auth.get("email_password"))
        == ("u_conf2", "mails_conf2", ""),
    )
    check(
        "sin auth real: totp/email intactos",
        sin_auth.get("totp_secret") == TOTP_B32
        and sin_auth.get("email") == "mail@example.com",
    )

    # (28b) Un email SIN dominio valido no se detecta (queda posicional).
    sin_tld = parsear_linea(
        f"user_nt:pass_nt:correo@localhost:{TOTP_B32}:{TOKEN_VENDEDOR}:mails_nt"
    )
    check(
        "email sin TLD: no se detecta pero conserva su lugar (email posicional)",
        sin_tld is not None
        and _campos_base(sin_tld)
        == ("user_nt", "pass_nt", TOTP_B32, "correo@localhost", "mails_nt", TOKEN_VENDEDOR),
    )


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
    test_formato_vendedor(check)
    test_vendedor_desambiguacion(check)
    test_vendedor_persistencia(check)
    test_heuristica_desorden(check)
    test_heuristica_conflictos(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_importador.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

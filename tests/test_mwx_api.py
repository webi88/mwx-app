# -*- coding: utf-8 -*-
"""Tests de la API HTTP del ejecutable MWX APP (`api/main.py`).

Sin red real y SIN tocar `data/` ni la BD del proyecto: se monta una SQLite
temporal re-apuntando `core.database.SessionLocal` durante la prueba (equivale
a exportar `DATABASE_URL` antes de importar; se hace asi para ser robusto
dentro del runner compartido, que ya importo el modulo). Al terminar se
restaura la sesion original y se borra el directorio temporal.

Cubre:
  - login OK (solo cuentas asignadas/activas/twitter), defaults de campaña,
    configuracion guardada, 401, 403 y rate limit 429;
  - perfil con token valido, sin `password`, y 401 tras `revocar_sesiones`;
  - estado (200 y 401 tras revocar);
  - imagenes: prioridad de la BD (`AppImagen`), fallback al archivo local,
    404 uniforme (inexistente/traversal/invalido) y 401 sin token;
  - `/health` publico (200 sin auth) y que no falla con la BD caida;
  - proxy OpenAI: 503 sin key, 400 modelo no permitido, reenvio correcto
    (URL/headers/modelo) con `httpx` monkeypatcheado;
  - evento persistido con detalle truncado a 2000.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_api.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

_ENV_RATE = ("MWX_RATE_LIMIT_INTENTOS", "MWX_RATE_LIMIT_MINUTOS")

# Claves FALSAS de prueba: nunca se imprimen ni se usan contra la red.
_CLAVE_FAKE = "sk-test-mwx-api-proxy"

# Bytes PNG falsos (solo para verificar que el contenido viaja intacto).
_PNG_BD = b"\x89PNG\r\n\x1a\n" + b"imagen-en-base-de-datos-mwx"


class _RespuestaHTTPFalsa:
    """Respuesta minima con la interfaz usada por el endpoint proxy."""

    def __init__(self, status_code: int = 200, datos=None):
        self.status_code = status_code
        self._datos = datos if datos is not None else {"id": "chatcmpl-test"}

    def json(self):
        return self._datos


class _ClienteHTTPFalso:
    """Reemplazo de `httpx.AsyncClient` que captura la peticion saliente."""

    llamadas: list = []
    respuesta: _RespuestaHTTPFalsa = _RespuestaHTTPFalsa()

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, headers=None, json=None):
        _ClienteHTTPFalso.llamadas.append(
            {"url": url, "headers": dict(headers or {}), "json": json}
        )
        return _ClienteHTTPFalso.respuesta


def _login(cliente, username="operador", password="clave-segura", machine_id="maquina-test-1"):
    return cliente.post(
        "/v1/login",
        json={
            "username": username,
            "password": password,
            "machine_id": machine_id,
        },
    )


def _con_bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def run(check) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, select

    import core.database as cdb
    from core.app_auth import hash_password, revocar_sesiones
    from core.models import (
        AppAsignacion,
        AppConfig,
        AppEvento,
        AppImagen,
        AppUsuario,
        Cuenta,
    )
    from api.deps import limpiar_rate_limit

    tmp = Path(tempfile.mkdtemp(prefix="mwx_api_test_"))
    db_path = tmp / "mwx_api_test.db"
    url_temp = f"sqlite:///{db_path.as_posix()}"

    env_previo = {clave: os.environ.get(clave) for clave in _ENV_RATE}
    url_previo = os.environ.get("DATABASE_URL")
    openai_previo = os.environ.get("OPENAI_API_KEY")
    os.environ["DATABASE_URL"] = url_temp
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-mwx-api-import")

    engine_previo = cdb.engine
    es_sqlite_previo = cdb._ES_SQLITE
    engine_temp = create_engine(
        url_temp, connect_args={"check_same_thread": False}
    )
    cdb.engine = engine_temp
    cdb._ES_SQLITE = True
    cdb.SessionLocal.configure(bind=engine_temp)
    limpiar_rate_limit()

    try:
        cdb.init_db()

        ids = {}
        with cdb.get_db_session() as db:
            operador = AppUsuario(
                username="operador",
                password_hash=hash_password("clave-segura"),
                nombre="Operador Uno",
                activo=True,
                token_version=0,
            )
            apagado = AppUsuario(
                username="apagado",
                password_hash=hash_password("clave-segura"),
                nombre="Apagado",
                activo=False,
                token_version=0,
            )
            sinconfig = AppUsuario(
                username="sinconfig",
                password_hash=hash_password("otra-clave"),
                nombre="Sin Config",
                activo=True,
                token_version=0,
            )
            db.add_all([operador, apagado, sinconfig])
            db.flush()
            ids = {
                "operador": operador.id,
                "apagado": apagado.id,
                "sinconfig": sinconfig.id,
            }

            def _cuenta(usuario, **extra):
                datos = {
                    "usuario": usuario,
                    "password": f"pw-{usuario}",
                    "plataforma": "twitter",
                    "activa": True,
                    "nombre_mostrado": f"Nombre {usuario}",
                    "handle_actual": f"{usuario}_x",
                    "seccion": "LIB",
                    "tipo_cuenta": "activista",
                    "perfil_personalidad": "popular",
                    "personalidad": "Directa y casual",
                    "tier_calidad": "tier1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "auth_token": f"auth-{usuario}",
                    "cookies_json": [{"name": "auth_token", "value": usuario}],
                    "user_agent": f"UA-{usuario}",
                }
                datos.update(extra)
                return Cuenta(**datos)

            cuenta_uno = _cuenta("cuenta_uno")
            cuenta_dos = _cuenta("cuenta_dos")
            cuenta_fb = _cuenta("cuenta_fb", plataforma="facebook")
            cuenta_inactiva = _cuenta("cuenta_inactiva", activa=False)
            cuenta_libre = _cuenta("cuenta_libre")
            cuenta_tres = _cuenta("cuenta_tres")
            db.add_all(
                [
                    cuenta_uno,
                    cuenta_dos,
                    cuenta_fb,
                    cuenta_inactiva,
                    cuenta_libre,
                    cuenta_tres,
                ]
            )
            db.flush()
            db.add_all(
                [
                    AppAsignacion(usuario_id=operador.id, cuenta_id=cuenta_uno.id),
                    AppAsignacion(usuario_id=operador.id, cuenta_id=cuenta_dos.id),
                    AppAsignacion(usuario_id=operador.id, cuenta_id=cuenta_fb.id),
                    AppAsignacion(
                        usuario_id=operador.id, cuenta_id=cuenta_inactiva.id
                    ),
                    AppAsignacion(
                        usuario_id=sinconfig.id, cuenta_id=cuenta_tres.id
                    ),
                    AppConfig(
                        usuario_id=operador.id,
                        hashtags=json.dumps(["#Mexico", "#Unidad"]),
                        contexto="Contexto de campaña",
                        narrativa="Trasfondo invisible",
                        urls=json.dumps(
                            ["https://x.com/ancla/status/1234567890"]
                        ),
                        texto_base="Texto base para citas",
                        acciones_por_cuenta=7,
                        pausa_seg=3,
                        duracion_min=45,
                        limite_dia=20,
                        imagenes=json.dumps(["foto.jpg"]),
                        permitir_ajustes=True,
                    ),
                ]
            )

        import api.main as api_main

        with TestClient(api_main.app) as cliente:
            # --- Healthcheck (Railway, sin auth) --------------------------
            r = cliente.get("/health")
            check("health: 200 sin auth", r.status_code == 200)
            check(
                "health: payload exacto",
                r.json()
                == {"ok": True, "servicio": "mwx-api", "version": "1.0"},
                str(r.json()),
            )
            with mock.patch.object(
                cdb, "SessionLocal", side_effect=RuntimeError("bd caida")
            ):
                r = cliente.get("/health")
            check(
                "health: no falla con la BD caida",
                r.status_code == 200 and r.json().get("ok") is True,
            )

            # --- Login: errores -------------------------------------------
            limpiar_rate_limit()
            r = _login(cliente, password="incorrecta")
            check("login: contraseña incorrecta -> 401", r.status_code == 401)
            check(
                "login: mensaje de credenciales exacto",
                r.json() == {
                    "ok": False,
                    "error": "usuario o contraseña incorrectos",
                },
            )

            limpiar_rate_limit()
            r = _login(cliente, username="apagado")
            check("login: usuario inactivo -> 403", r.status_code == 403)
            check(
                "login: mensaje de cuenta desactivada",
                r.json() == {"ok": False, "error": "cuenta desactivada"},
            )

            limpiar_rate_limit()
            r = _login(cliente, username="no_existe")
            check("login: usuario inexistente -> 401", r.status_code == 401)

            # --- Login OK -------------------------------------------------
            limpiar_rate_limit()
            r = _login(cliente)
            check("login: credenciales válidas -> 200", r.status_code == 200)
            cuerpo = r.json() if r.status_code == 200 else {}
            token = cuerpo.get("token") or ""
            check("login: devuelve token HMAC", isinstance(token, str) and "." in token)
            check(
                "login: expira en UTC con Z",
                str(cuerpo.get("expira") or "").endswith("Z"),
            )
            check(
                "login: payload con claves exactas",
                set(cuerpo) == {"ok", "token", "expira", "usuario", "campana", "cuentas"},
            )
            check(
                "login: usuario sin secretos",
                cuerpo.get("usuario")
                == {"username": "operador", "nombre": "Operador Uno"},
            )

            usuarios = [c.get("usuario") for c in cuerpo.get("cuentas", [])]
            check(
                "login: solo cuentas asignadas, activas y twitter",
                usuarios == ["cuenta_dos", "cuenta_uno"],
                str(usuarios),
            )
            cuenta_login = cuerpo.get("cuentas", [{}])[0]
            check(
                "login: cuenta con los 13 campos del contrato",
                set(cuenta_login)
                == {
                    "usuario",
                    "nombre_mostrado",
                    "handle_actual",
                    "seccion",
                    "tipo_cuenta",
                    "perfil_personalidad",
                    "personalidad",
                    "tier_calidad",
                    "password",
                    "totp_secret",
                    "auth_token",
                    "cookies_json",
                    "user_agent",
                },
            )
            check(
                "login: password/cookies presentes (contrato del exe)",
                cuenta_login.get("password") == "pw-cuenta_dos"
                and isinstance(cuenta_login.get("cookies_json"), str)
                and cuenta_login.get("totp_secret") == "JBSWY3DPEHPK3PXP",
            )

            campana = cuerpo.get("campana") or {}
            check(
                "login: campaña con claves exactas",
                set(campana)
                == {
                    "hashtags",
                    "contexto",
                    "narrativa",
                    "urls",
                    "texto_base",
                    "acciones_por_cuenta",
                    "pausa_seg",
                    "duracion_min",
                    "limite_dia",
                    "imagenes",
                    "permitir_ajustes",
                },
            )
            check(
                "login: campaña lee AppConfig",
                campana.get("hashtags") == ["#Mexico", "#Unidad"]
                and campana.get("acciones_por_cuenta") == 7
                and campana.get("pausa_seg") == 3
                and campana.get("duracion_min") == 45
                and campana.get("limite_dia") == 20
                and campana.get("permitir_ajustes") is True,
            )
            check(
                "login: imagenes con url del contrato",
                campana.get("imagenes") == [
                    {"nombre": "foto.jpg", "url": "/v1/imagenes/foto.jpg"}
                ],
            )

            r = _login(cliente, username="sinconfig", password="otra-clave")
            campana_default = (r.json() if r.status_code == 200 else {}).get(
                "campana", {}
            )
            check(
                "login: sin AppConfig usa defaults del modelo",
                r.status_code == 200
                and campana_default.get("hashtags") == []
                and campana_default.get("acciones_por_cuenta") == 10
                and campana_default.get("pausa_seg") == 2
                and campana_default.get("duracion_min") == 60
                and campana_default.get("limite_dia") == 49
                and campana_default.get("imagenes") == []
                and campana_default.get("permitir_ajustes") is False,
            )

            with cdb.get_db_session() as db:
                evento_login = (
                    db.execute(
                        select(AppEvento)
                        .where(
                            AppEvento.usuario_id == ids["operador"],
                            AppEvento.tipo == "login",
                        )
                        .order_by(AppEvento.id.desc())
                    )
                    .scalars()
                    .first()
                )
                check(
                    "login: AppEvento registra machine_id sin credenciales",
                    evento_login is not None
                    and "maquina-test-1" in (evento_login.detalle or "")
                    and "clave-segura" not in (evento_login.detalle or "")
                    and token not in (evento_login.detalle or ""),
                )

            # --- Perfil / estado -----------------------------------------
            r = cliente.get("/v1/perfil")
            check("perfil: sin token -> 401", r.status_code == 401)

            r = cliente.get("/v1/perfil", headers=_con_bearer("token-invalido"))
            check("perfil: token inválido -> 401", r.status_code == 401)

            r = cliente.get("/v1/perfil", headers=_con_bearer(token))
            check("perfil: token válido -> 200", r.status_code == 200)
            perfil = r.json() if r.status_code == 200 else {}
            check(
                "perfil: mismo payload sin password",
                perfil.get("usuario") == cuerpo.get("usuario")
                and perfil.get("campana") == campana
                and [c.get("usuario") for c in perfil.get("cuentas", [])]
                == ["cuenta_dos", "cuenta_uno"],
            )
            cuentas_perfil = perfil.get("cuentas", [])
            check(
                "perfil: NUNCA expone password",
                all("password" not in c for c in cuentas_perfil),
            )
            check(
                "perfil: conserva secretos de operación",
                bool(cuentas_perfil)
                and all(
                    c.get("auth_token") and c.get("cookies_json") and c.get("user_agent")
                    for c in cuentas_perfil
                ),
            )

            r = cliente.post("/v1/estado", headers=_con_bearer(token))
            check(
                "estado: token vigente -> 200",
                r.status_code == 200
                and r.json() == {"ok": True, "activo": True, "nombre": "Operador Uno"},
            )

            with cdb.get_db_session() as db:
                nuevo_tv = revocar_sesiones(db, ids["operador"])
            check("revocación: token_version incrementado", nuevo_tv == 1)

            r = cliente.get("/v1/perfil", headers=_con_bearer(token))
            check("perfil: token revocado -> 401", r.status_code == 401)
            r = cliente.post("/v1/estado", headers=_con_bearer(token))
            check("estado: token revocado -> 401", r.status_code == 401)

            limpiar_rate_limit()
            r = _login(cliente)
            token = (r.json() if r.status_code == 200 else {}).get("token") or ""
            check("login: nuevo token tras revocación", r.status_code == 200 and bool(token))

            # --- Imagenes: BD primero, archivo como fallback ---------------
            carpeta_imagenes = tmp / "data" / "mwx_app" / "operador" / "imagenes"
            carpeta_imagenes.mkdir(parents=True, exist_ok=True)

            with cdb.get_db_session() as db:
                db.add(
                    AppImagen(
                        usuario_id=ids["operador"],
                        nombre="desde_bd.png",
                        contenido=_PNG_BD,
                        content_type="image/png",
                        tamano=len(_PNG_BD),
                    )
                )

            # Archivo local con el MISMO nombre y OTROS bytes: si se sirviera
            # el archivo en vez de la BD, el contenido no coincidiria.
            (carpeta_imagenes / "desde_bd.png").write_bytes(b"archivo-local")

            r = cliente.get("/v1/imagenes/desde_bd.png")
            check("imagen: sin token -> 401", r.status_code == 401)

            with mock.patch.object(
                api_main,
                "resolver_ruta",
                side_effect=AssertionError("la BD debe resolver sin tocar disco"),
            ):
                r = cliente.get(
                    "/v1/imagenes/desde_bd.png", headers=_con_bearer(token)
                )
            check("imagen BD: existe -> 200", r.status_code == 200)
            check(
                "imagen BD: contenido exacto y content-type",
                r.content == _PNG_BD
                and (r.headers.get("content-type") or "").startswith("image/png"),
            )
            check(
                "imagen BD: Content-Disposition inline",
                r.headers.get("content-disposition")
                == 'inline; filename="desde_bd.png"',
                str(r.headers.get("content-disposition")),
            )
            check(
                "imagen BD: Cache-Control privado",
                r.headers.get("cache-control") == "private, max-age=300",
                str(r.headers.get("cache-control")),
            )

            imagen = carpeta_imagenes / "foto.jpg"
            contenido = b"\xff\xd8\xff\xe0contenido-de-prueba"
            imagen.write_bytes(contenido)

            with mock.patch.object(
                api_main, "resolver_ruta", side_effect=lambda rel: str(tmp / rel)
            ):
                r = cliente.get("/v1/imagenes/foto.jpg", headers=_con_bearer(token))
                check("imagen archivo: fallback -> 200", r.status_code == 200)
                check(
                    "imagen archivo: contenido y content-type",
                    r.content == contenido
                    and (r.headers.get("content-type") or "").startswith("image/jpeg"),
                )
                for nombre, etiqueta in [
                    ("no_existe.jpg", "inexistente"),
                    ("a..b.jpg", "con '..'"),
                    ("sub%2Ffoto.jpg", "con '/'"),
                    ("%2E%2E%2Fx", "traversal '../x'"),
                    ("..%2Fx", "traversal '..%2Fx'"),
                    ("foto%20mal.jpg", "con espacio"),
                    ("foto%C3%B1.jpg", "con acento"),
                ]:
                    r = cliente.get(
                        f"/v1/imagenes/{nombre}", headers=_con_bearer(token)
                    )
                    check(
                        f"imagen: {etiqueta} -> 404 uniforme",
                        r.status_code == 404
                        and r.json() == {"ok": False, "error": "imagen no encontrada"},
                        f"{r.status_code} {r.text[:80]}",
                    )

            # --- Proxy OpenAI ---------------------------------------------
            cuerpo_chat = {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hola"}],
                "max_tokens": 128,
            }

            with mock.patch.object(
                api_main, "settings", SimpleNamespace(openai_api_key="")
            ):
                r = cliente.post(
                    "/v1/chat/completions",
                    headers=_con_bearer(token),
                    json=cuerpo_chat,
                )
                check(
                    "proxy: sin OPENAI_API_KEY -> 503",
                    r.status_code == 503
                    and r.json() == {
                        "ok": False,
                        "error": "servicio de IA no configurado",
                    },
                )

            r = cliente.post(
                "/v1/chat/completions",
                headers=_con_bearer(token),
                json={
                    "model": "gpt-5-prohibido",
                    "messages": [{"role": "user", "content": "hola"}],
                },
            )
            check("proxy: modelo no permitido -> 400", r.status_code == 400)

            r = cliente.post(
                "/v1/chat/completions",
                headers=_con_bearer(token),
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hola"}],
                    "max_tokens": 5000,
                },
            )
            check("proxy: max_tokens > 4096 -> 400", r.status_code == 400)

            _ClienteHTTPFalso.llamadas = []
            _ClienteHTTPFalso.respuesta = _RespuestaHTTPFalsa(
                200, {"id": "chatcmpl-test", "choices": []}
            )
            with mock.patch.object(
                api_main, "settings", SimpleNamespace(openai_api_key=_CLAVE_FAKE)
            ), mock.patch.object(
                api_main.httpx, "AsyncClient", _ClienteHTTPFalso
            ):
                r = cliente.post(
                    "/v1/chat/completions",
                    headers=_con_bearer(token),
                    json=cuerpo_chat,
                )
            check("proxy: respuesta upstream -> 200/JSON tal cual", r.status_code == 200 and r.json() == {"id": "chatcmpl-test", "choices": []})
            enviada = _ClienteHTTPFalso.llamadas[0] if _ClienteHTTPFalso.llamadas else {}
            check(
                "proxy: URL/headers/modelo correctos",
                enviada.get("url")
                == "https://api.openai.com/v1/chat/completions"
                and enviada.get("headers", {}).get("Authorization")
                == f"Bearer {_CLAVE_FAKE}"
                and enviada.get("json", {}).get("model") == "gpt-4o-mini"
                and enviada.get("json", {}).get("max_tokens") == 128,
            )
            check(
                "proxy: no filtra la key en la respuesta",
                _CLAVE_FAKE not in str(r.json()),
            )

            _ClienteHTTPFalso.llamadas = []
            _ClienteHTTPFalso.respuesta = _RespuestaHTTPFalsa(
                429, {"error": {"message": "rate limit"}}
            )
            with mock.patch.object(
                api_main, "settings", SimpleNamespace(openai_api_key=_CLAVE_FAKE)
            ), mock.patch.object(
                api_main.httpx, "AsyncClient", _ClienteHTTPFalso
            ):
                r = cliente.post(
                    "/v1/chat/completions",
                    headers=_con_bearer(token),
                    json=cuerpo_chat,
                )
            check(
                "proxy: conserva el status del upstream (429)",
                r.status_code == 429
                and r.json() == {"error": {"message": "rate limit"}},
            )

            # --- Evento ---------------------------------------------------
            r = cliente.post(
                "/v1/evento",
                headers=_con_bearer(token),
                json={"tipo": "campana", "detalle": "x" * 2500},
            )
            check("evento: guarda y responde ok", r.status_code == 200 and r.json() == {"ok": True})

            with cdb.get_db_session() as db:
                evento = (
                    db.execute(
                        select(AppEvento)
                        .where(
                            AppEvento.usuario_id == ids["operador"],
                            AppEvento.tipo == "campana",
                        )
                        .order_by(AppEvento.id.desc())
                    )
                    .scalars()
                    .first()
                )
                check(
                    "evento: detalle truncado a 2000",
                    evento is not None and len(evento.detalle or "") == 2000,
                )

            # --- Rate limit ----------------------------------------------
            for clave, valor in zip(_ENV_RATE, ("3", "5")):
                os.environ[clave] = valor
            limpiar_rate_limit()
            estados = [
                _login(cliente, password="mala").status_code for _ in range(3)
            ]
            r = _login(cliente, password="mala")
            check(
                "login: rate limit 429 tras 3 fallos",
                estados == [401, 401, 401] and r.status_code == 429,
                str(estados),
            )
            check(
                "login: mensaje de rate limit",
                r.json().get("ok") is False and "demasiados intentos" in r.json().get("error", ""),
            )
            limpiar_rate_limit()

    finally:
        for clave, valor in env_previo.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor
        if url_previo is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_previo
        if openai_previo is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = openai_previo
        cdb.SessionLocal.configure(bind=engine_previo)
        cdb.engine = engine_previo
        cdb._ES_SQLITE = es_sqlite_previo
        engine_temp.dispose()
        shutil.rmtree(tmp, ignore_errors=True)
        limpiar_rate_limit()


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

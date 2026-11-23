# -*- coding: utf-8 -*-
"""Tests deterministas de `migrar_desde_remoto.py` (sin red, sin Chrome).

Usa SOLO SQLite temporales en `tempfile.mkdtemp()`: nunca toca la BD local real
(`data/gestor_redes.db`) ni `data/`. Las sesiones de SQLAlchemy se redirigen con
monkeypatch a la base temporal (mismo patron que `tests/test_cuotas_core.py`).

Cubre:
    (1) serializacion datetime/JSON/bool/Decimal/bytes/UUID y formato del JSON
        de exportacion (fecha, origen, keep, cuentas).
    (2) deteccion de duplicados por token, por email, ambiguo (2 matches),
        destino ya existente en local y local-only genuino.
    (3) E2E: BD remota temporal -> exportar -> renombrar -> restaurar ->
        verificar (0 mismatches) + segunda corrida idempotente.
    (4) seguridad: URLs/credenciales enmascaradas y salida sin tokens/emails.
    (5) CLI: prioridad de URL, banderas, --json y dry-run forzado.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import date, datetime
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import core.database as core_database  # noqa: E402
import core.renombrar as core_renombrar  # noqa: E402
import cuentas.restaurador as restaurador  # noqa: E402
import migrar_desde_remoto as migrar  # noqa: E402
from core.models import Base, Cuenta, RegistroAccion  # noqa: E402


# --------------------------------------------------------------------------- #
# Bases SQLite temporales (nunca la BD real)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _base_temporal(nombre: str):
    """Crea una SQLite temporal y devuelve `(maker, ruta, contexto_sesion)`."""
    carpeta = tempfile.mkdtemp(prefix=f"migra_remota_{nombre}_")
    ruta = Path(carpeta) / f"{nombre}.db"
    engine = create_engine(
        f"sqlite:///{ruta}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextlib.contextmanager
    def sesion():
        db = maker()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    try:
        yield maker, ruta, sesion
    finally:
        engine.dispose()
        shutil.rmtree(carpeta, ignore_errors=True)


def _cuenta(usuario: str, **extra) -> Cuenta:
    """Cuenta sintetica con columnas explicitas (sin credenciales reales)."""
    datos = {
        "usuario": usuario,
        "password": "pass_sintetica",
        "plataforma": "twitter",
        "tags": "",
        "grupo": "",
        "activa": True,
        "cookies_path": "",
        "email": "",
        "phone": "",
        "proxy": "",
        "pais": "",
        "sector": "",
        "avatar_path": "",
        "banner_path": "",
        "seccion": "",
        "nombre_mostrado": "",
        "handle_actual": "",
        "tipo_cuenta": "",
        "nombre_propuesto": "",
        "handle_propuesto": "",
        "personalidad": "",
        "rol_activacion": "",
        "perfil_personalidad": "",
        "tier_calidad": "",
        "pausada_activacion": False,
        "totp_secret": "",
        "email_password": "",
        "auth_token": "",
        "cookies_json": None,
        "user_agent": "",
        "status": "imported",
        "last_checked": None,
    }
    datos.update(extra)
    return Cuenta(**datos)


def _insertar(maker, filas) -> None:
    with maker() as db:
        for fila in filas:
            db.add(fila)
        db.commit()


def _filas(maker):
    with maker() as db:
        return db.query(Cuenta).all()


def _dict_filas(maker):
    return [
        {columna.name: getattr(fila, columna.name) for columna in Cuenta.__table__.columns}
        for fila in _filas(maker)
    ]


# --------------------------------------------------------------------------- #
# (1) Serializacion y formato del backup
# --------------------------------------------------------------------------- #
def test_serializacion_y_formato(check):
    print("(1) serializacion de tipos y formato del JSON de exportacion")

    fecha = datetime(2026, 1, 2, 3, 4, 5, 123456)
    check(
        "datetime -> ISO",
        migrar._serializar_valor(fecha) == "2026-01-02T03:04:05.123456",
    )
    check(
        "date -> ISO",
        migrar._serializar_valor(date(2026, 1, 2)) == "2026-01-02",
    )
    check("bool True sigue siendo bool", migrar._serializar_valor(True) is True)
    check("bool False sigue siendo bool", migrar._serializar_valor(False) is False)
    check(
        "dict/lista anidados se serializan",
        migrar._serializar_valor({"a": [fecha, True], "b": None})
        == {"a": ["2026-01-02T03:04:05.123456", True], "b": None},
    )
    check("Decimal -> str", migrar._serializar_valor(migrar.Decimal("1.25")) == "1.25")
    check(
        "bytes -> base64",
        migrar._serializar_valor(b"abc") == "YWJj",
    )
    import uuid

    valor_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    check("UUID -> str", migrar._serializar_valor(valor_uuid) == str(valor_uuid))
    check(
        "objeto desconocido -> str (no lanza)",
        isinstance(migrar._serializar_valor(object()), str),
    )

    cookies = [
        {
            "name": "auth_token",
            "value": "TOKEN_SINTETICO_QA",
            "domain": ".x.com",
            "path": "/",
            "httpOnly": True,
            "expiry": 1826747192,
        }
    ]
    with _base_temporal("remota_serializa") as (maker, _ruta, sesion):
        _insertar(
            maker,
            [
                _cuenta(
                    "remota_serializa_qa",
                    password="pass_qa",
                    activa=True,
                    pausada_activacion=False,
                    cookies_json=cookies,
                    fecha_creacion=fecha,
                    last_checked=fecha,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "export.json"
            resumen = migrar.exportar_cuentas_remotas(f"sqlite:///{_ruta}", destino)
            check("export: ok", resumen.get("ok") is True, str(resumen.get("error")))
            check("export: 1 exportada", resumen.get("exportadas") == 1)
            check("export: destino existe", Path(resumen["destino"]).is_file())
            with Path(resumen["destino"]).open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            check("formato: dict con fecha", isinstance(data.get("fecha"), str))
            check("formato: origen mwx.app", data.get("origen") == "mwx.app")
            check("formato: keep=[]", data.get("keep") == [])
            check("formato: lista cuentas", isinstance(data.get("cuentas"), list))
            fila = data["cuentas"][0]
            check(
                "formato: fecha_creacion serializada ISO",
                fila.get("fecha_creacion") == "2026-01-02T03:04:05.123456",
            )
            check("formato: activa es bool", fila.get("activa") is True)
            check("formato: pausada es bool", fila.get("pausada_activacion") is False)
            check(
                "formato: cookies_json es objeto (no string)",
                isinstance(fila.get("cookies_json"), list)
                and fila["cookies_json"][0]["name"] == "auth_token",
            )
            check(
                "export: URL enmascarada en el resumen",
                "pass_qa" not in json.dumps(resumen, ensure_ascii=False),
            )


# --------------------------------------------------------------------------- #
# (2) Deteccion de duplicados
# --------------------------------------------------------------------------- #
def _remota(usuario, auth_token="", email=""):
    return {"usuario": usuario, "auth_token": auth_token, "email": email}


def _local(usuario, auth_token="", email=""):
    return {"usuario": usuario, "auth_token": auth_token, "email": email}


def test_duplicados(check):
    print("(2) deteccion de duplicados por token/email/ambiguo/destino existente")
    remotas = [
        _remota("rem_token", "TOKEN_UNO", "token@example.test"),
        _remota("rem_email", "", "correo@example.test"),
        _remota("rem_ambiguo_a", "TOKEN_DOS", "amb@example.test"),
        _remota("rem_ambiguo_b", "TOKEN_DOS", "amb@example.test"),
        _remota("rem_destino_existente", "TOKEN_TRES", "destino@example.test"),
    ]
    locales = [
        _local("local_token", "TOKEN_UNO", ""),
        _local("local_email", "", "CORREO@Example.Test"),
        _local("local_ambiguo", "TOKEN_DOS", ""),
        _local("local_destino", "TOKEN_TRES", ""),
        _local("rem_destino_existente", "", "otro@example.test"),
        _local("local_genuino", "TOKEN_GENUINO", "genuino@example.test"),
    ]
    dup = migrar.detectar_duplicados_locales(remotas, cuentas_locales=locales)
    check("duplicados: ok", dup.get("ok") is True)
    renombres = {item["anterior"]: item["nuevo"] for item in dup.get("renombres") or []}
    motivos = {item["anterior"]: item["motivo"] for item in dup.get("renombres") or []}
    check("token: local_token -> rem_token", renombres.get("local_token") == "rem_token")
    check("token: motivo token", motivos.get("local_token") == "token")
    check("email: local_email -> rem_email", renombres.get("local_email") == "rem_email")
    check("email: comparacion case-insensitive", motivos.get("local_email") == "email")
    check(
        "ambiguo: 2 matches NO se renombra",
        "local_ambiguo" not in renombres
        and any(item["usuario"] == "local_ambiguo" for item in dup.get("ambiguos") or []),
    )
    check(
        "destino existente: local_destino NO se renombra",
        "local_destino" not in renombres
        and any(item["usuario"] == "local_destino" for item in dup.get("omitidos") or []),
    )
    check(
        "local genuino: solo local_genuino (rem_destino_existente esta en local)",
        dup.get("local_only_reales") == ["local_genuino"],
    )
    check("nunca lanza con entradas raras", migrar.detectar_duplicados_locales([], [])["ok"])

    dup_vacio = migrar.detectar_duplicados_locales(None, cuentas_locales=None)
    check("remotas None: ok sin lanzar", dup_vacio.get("ok") is True)


# --------------------------------------------------------------------------- #
# (3) E2E exportar -> renombrar -> restaurar -> verificar
# --------------------------------------------------------------------------- #
def _preparar_e2e(tmp: Path):
    """Crea las BD remota/local temporales y el JSON exportado."""
    remota_ctx = _base_temporal("remota_e2e")
    local_ctx = _base_temporal("local_e2e")
    maker_remota, ruta_remota, _sesion_remota = remota_ctx.__enter__()
    maker_local, ruta_local, sesion_local = local_ctx.__enter__()

    cookies = [
        {"name": "auth_token", "value": "TOKEN_SINTETICO_E2E", "domain": ".x.com", "path": "/"}
    ]
    _insertar(
        maker_remota,
        [
            _cuenta(
                "existente_qa",
                password="pass_remota_existente",
                activa=True,
                tier_calidad="tier1",
                seccion="CI",
                tipo_cuenta="politica",
                rol_activacion="cita",
                pausada_activacion=False,
                auth_token="TOK_EXIST_QA",
                email="exist@example.test",
                cookies_json=cookies,
                last_checked=datetime(2026, 2, 3, 4, 5, 6),
            ),
            _cuenta(
                "dup_token_nuevo_qa",
                password="pass_remota_dup",
                activa=False,
                tier_calidad="tier2",
                seccion="LIB",
                tipo_cuenta="activista",
                rol_activacion="rt",
                pausada_activacion=True,
                auth_token="TOK_DUP_QA",
                email="dup@example.test",
                cookies_json=cookies,
            ),
            _cuenta(
                "dup_email_nuevo_qa",
                password="pass_remota_email",
                activa=True,
                tier_calidad="tier3",
                seccion="JUS",
                tipo_cuenta="ciudadana",
                rol_activacion="comentario",
                pausada_activacion=False,
                auth_token="",
                email="DupEmail@Example.Test",
            ),
            _cuenta(
                "nueva_remota_qa",
                password="pass_remota_nueva",
                activa=True,
                tier_calidad="",
                seccion="CI",
                tipo_cuenta="activista",
                rol_activacion="hashtags",
                pausada_activacion=False,
                auth_token="TOK_NUEVA_QA",
                email="nueva@example.test",
            ),
        ],
    )
    _insertar(
        maker_local,
        [
            _cuenta(
                "existente_qa",
                password="pass_local_vieja",
                activa=False,
                tier_calidad="",
                seccion="",
                tipo_cuenta="",
                rol_activacion="",
                pausada_activacion=False,
                auth_token="TOK_EXIST_QA",
                email="exist@example.test",
            ),
            _cuenta(
                "dup_token_viejo_qa",
                password="pass_local_dup_token",
                activa=True,
                tier_calidad="",
                auth_token="TOK_DUP_QA",
                email="viejo_token@example.test",
            ),
            _cuenta(
                "dup_email_viejo_qa",
                password="pass_local_dup_email",
                activa=True,
                tier_calidad="",
                auth_token="",
                email="dupemail@example.test",
            ),
            _cuenta(
                "solo_local_qa",
                password="pass_local_solo",
                activa=False,
                tier_calidad="tier2",
                seccion="LIB",
                tipo_cuenta="politica",
                rol_activacion="cita",
                pausada_activacion=True,
                auth_token="TOK_SOLO_QA",
                email="solo@example.test",
            ),
        ],
    )

    destino = tmp / "remoto.json"
    exportacion = migrar.exportar_cuentas_remotas(f"sqlite:///{ruta_remota}", destino)
    remotas = migrar._cargar_remotas_de_json(exportacion["destino"])
    locales = migrar.leer_cuentas_locales(sesion_local)
    dup = migrar.detectar_duplicados_locales(remotas, cuentas_locales=locales)

    contexto = {
        "remota_ctx": remota_ctx,
        "local_ctx": local_ctx,
        "maker_remota": maker_remota,
        "maker_local": maker_local,
        "ruta_local": ruta_local,
        "sesion_local": sesion_local,
        "export_json": exportacion["destino"],
        "remotas": remotas,
        "dup": dup,
    }
    return contexto


def _cerrar_e2e(contexto) -> None:
    contexto["local_ctx"].__exit__(None, None, None)
    contexto["remota_ctx"].__exit__(None, None, None)


def test_e2e(check):
    print("(3) E2E exportar -> renombrar -> restaurar -> verificar (2 corridas)")
    with tempfile.TemporaryDirectory() as tmp_texto:
        tmp = Path(tmp_texto)
        contexto = _preparar_e2e(tmp)
        try:
            dup = contexto["dup"]
            check("E2E: exporta 4 remotas", len(contexto["remotas"]) == 4)
            check("E2E: detecta 2 renombres", len(dup["renombres"]) == 2)
            check(
                "E2E: local-only genuino = solo_local_qa",
                dup["local_only_reales"] == ["solo_local_qa"],
            )

            cambio_dir = tmp / "change"
            cambio_dir.mkdir()
            (cambio_dir / "dup_token_viejo_qa.json").write_text(
                '{"usuario":"dup_token_viejo_qa","cookies":[]}', encoding="utf-8"
            )
            backups = tmp / "backups"

            # `core.renombrar` importa get_db_session DENTRO de sus funciones
            # desde `core.database`, por eso se parchea ahi; el restaurador lo
            # tiene importado a nivel de modulo. Nunca se tocan archivos reales:
            # los nombres QA no existen en data/ y las rutas son "".
            with contextlib.ExitStack() as pila:
                pila.enter_context(
                    mock.patch.object(core_database, "get_db_session", contexto["sesion_local"])
                )
                pila.enter_context(
                    mock.patch.object(restaurador, "get_db_session", contexto["sesion_local"])
                )
                pila.enter_context(
                    mock.patch.object(migrar, "get_db_session", contexto["sesion_local"])
                )
                pila.enter_context(
                    mock.patch.object(core_renombrar, "_planes_archivos", lambda *a, **k: [])
                )
                resultado = migrar.aplicar(
                    contexto["export_json"],
                    renombres=dup["renombres"],
                    session_factory=contexto["sesion_local"],
                    sqlite_path=contexto["ruta_local"],
                    destino_dir=backups,
                    change_dir=cambio_dir,
                )

            check("E2E apply: ok", resultado.get("ok") is True, str(resultado.get("error")))
            respaldo = resultado.get("respaldo") or {}
            check("E2E apply: respaldo SQLite creado", Path(respaldo.get("db", "")).is_file())
            check(
                "E2E apply: respaldo JSON de cuentas creado",
                Path(respaldo.get("cuentas_json", "")).is_file(),
            )
            check("E2E apply: respaldo de Change creado", bool(respaldo.get("change")))
            check(
                "E2E apply: sesion Change migrada y respaldada",
                (cambio_dir / "dup_token_nuevo_qa.json").is_file()
                and (Path(respaldo["change"]) / "dup_token_viejo_qa.json").is_file(),
            )
            restauracion = resultado.get("restauracion") or {}
            check("E2E restore: ok", restauracion.get("ok") is True)
            check("E2E restore: 1 creada", restauracion.get("restauradas") == 1)
            check("E2E restore: 3 actualizadas", restauracion.get("actualizadas") == 3)

            locales = {fila["usuario"]: fila for fila in _dict_filas(contexto["maker_local"])}
            check(
                "E2E: claves finales = remotas + solo_local",
                set(locales) == {r["usuario"] for r in contexto["remotas"]} | {"solo_local_qa"},
            )
            check("E2E: sin clave vieja por token", "dup_token_viejo_qa" not in locales)
            check("E2E: sin clave vieja por email", "dup_email_viejo_qa" not in locales)
            tokens = [fila.get("auth_token") for fila in locales.values()]
            check("E2E: sin token duplicado", tokens.count("TOK_DUP_QA") == 1)

            existente = locales["existente_qa"]
            check("E2E: tier remoto aplicado", existente["tier_calidad"] == "tier1")
            check("E2E: seccion remota aplicada", existente["seccion"] == "CI")
            check("E2E: rol remoto aplicado", existente["rol_activacion"] == "cita")
            check("E2E: activa remota aplicada", bool(existente["activa"]) is True)
            check("E2E: password remoto aplicado", existente["password"] == "pass_remota_existente")
            check("E2E: cookies_json remoto aplicado", isinstance(existente["cookies_json"], list))

            duplicada = locales["dup_token_nuevo_qa"]
            check("E2E: duplicada renombrada conserva cookies", isinstance(duplicada["cookies_json"], list))
            check("E2E: duplicada tier remoto", duplicada["tier_calidad"] == "tier2")
            check("E2E: duplicada pausada remota", duplicada["pausada_activacion"] is True)
            check("E2E: duplicada activa=False remota", duplicada["activa"] is False)
            check(
                "E2E: duplicada por email se renombro",
                locales["dup_email_nuevo_qa"]["tier_calidad"] == "tier3",
            )

            solo_local = locales["solo_local_qa"]
            check(
                "E2E: local-only genuina intacta (password)",
                solo_local["password"] == "pass_local_solo",
            )
            check(
                "E2E: local-only genuina intacta (tier/seccion/rol)",
                solo_local["tier_calidad"] == "tier2"
                and solo_local["seccion"] == "LIB"
                and solo_local["rol_activacion"] == "cita",
            )
            check(
                "E2E: local-only genuina intacta (pausada/activa)",
                solo_local["pausada_activacion"] is True and solo_local["activa"] is False,
            )

            verificacion = resultado.get("verificacion") or {}
            check("E2E verificar: ok", verificacion.get("ok") is True)
            check("E2E verificar: 0 mismatches", not verificacion.get("mismatches"))
            check("E2E verificar: 0 faltantes", not verificacion.get("faltantes"))
            check(
                "E2E verificar: solo la local genuina queda local-only",
                verificacion.get("local_only") == ["solo_local_qa"],
            )
            check(
                "E2E verificar: stats remoto (4) y local (5 con la genuina)",
                verificacion.get("stats_remoto", {}).get("total") == 4
                and verificacion.get("stats_local", {}).get("total") == 5,
            )
            check(
                "E2E verificar: tiers/conteos coherentes",
                verificacion.get("stats_local", {}).get("por_tier")
                == {"(vacio)": 1, "tier1": 1, "tier2": 2, "tier3": 1},
            )

            # ---- segunda corrida: idempotente -------------------------------- #
            with contextlib.ExitStack() as pila:
                pila.enter_context(
                    mock.patch.object(core_database, "get_db_session", contexto["sesion_local"])
                )
                pila.enter_context(
                    mock.patch.object(restaurador, "get_db_session", contexto["sesion_local"])
                )
                pila.enter_context(
                    mock.patch.object(migrar, "get_db_session", contexto["sesion_local"])
                )
                pila.enter_context(
                    mock.patch.object(core_renombrar, "_planes_archivos", lambda *a, **k: [])
                )
                segunda = migrar.aplicar(
                    contexto["export_json"],
                    renombres=None,
                    session_factory=contexto["sesion_local"],
                    sqlite_path=contexto["ruta_local"],
                    destino_dir=backups,
                    change_dir=cambio_dir,
                )
            verificacion2 = segunda.get("verificacion") or {}
            check("E2E 2a corrida: ok", segunda.get("ok") is True, str(segunda.get("error")))
            check("E2E 2a corrida: 0 mismatches", not verificacion2.get("mismatches"))
            check("E2E 2a corrida: 0 faltantes", not verificacion2.get("faltantes"))
            check(
                "E2E 2a corrida: ninguna cuenta nueva/borrada",
                len(_filas(contexto["maker_local"])) == 5,
            )
            check(
                "E2E 2a corrida: sin renombres nuevos",
                segunda.get("renombres") == [],
            )
            check(
                "E2E 2a corrida: no se filtran secretos en el resumen",
                "TOK_DUP_QA" not in json.dumps(segunda, ensure_ascii=False, default=str)
                and "pass_local_solo" not in json.dumps(segunda, ensure_ascii=False, default=str),
            )
        finally:
            _cerrar_e2e(contexto)


# --------------------------------------------------------------------------- #
# (4) Seguridad / salida sin secretos
# --------------------------------------------------------------------------- #
def test_seguridad(check):
    print("(4) URLs/credenciales enmascaradas y salida sin secretos")
    url = "postgresql://usuario_secreto:password_secreto@host.ejemplo:5432/base?sslmode=require"
    oculta = migrar.enmascarar_url(url)
    check("enmascarar: conserva host/base", "host.ejemplo" in oculta and "base" in oculta)
    check("enmascarar: oculta password", "password_secreto" not in oculta)
    check("enmascarar: oculta usuario", "usuario_secreto" not in oculta)
    check("enmascarar: oculta query", "sslmode" not in oculta)
    check(
        "sanear_texto: oculta URL con credenciales en un error",
        "password_secreto" not in migrar.sanear_texto(f"fallo al conectar a {url}"),
    )
    check(
        "sanear_texto: oculta token=...",
        "abc123" not in migrar.sanear_texto("token=abc123 no valido"),
    )
    check("_valor_seguro: auth_token oculto", migrar._valor_seguro("auth_token", "SECRETO") == "<oculto>")
    check("_valor_seguro: password oculto", migrar._valor_seguro("password", "SECRETO") == "<oculto>")
    check("_valor_seguro: email oculto", migrar._valor_seguro("email", "a@b.c") == "<oculto>")
    check("_valor_seguro: cookies ocultas", migrar._valor_seguro("cookies_json", [{"x": 1}]) == "<oculto>")
    check(
        "_valor_seguro: columna normal se trunca",
        migrar._valor_seguro("status", "x" * 200).endswith("..."),
    )

    remotas = [
        {
            "usuario": "user_visible",
            "auth_token": "TOKEN_SECRETO_QA",
            "password": "PASS_SECRETA_QA",
            "email": "secreto@example.test",
            "tier_calidad": "tier1",
            "seccion": "CI",
            "tipo_cuenta": "politica",
            "rol_activacion": "cita",
            "pausada_activacion": False,
            "activa": True,
        }
    ]
    locales = [{"usuario": "user_visible"}]
    resumen = migrar.construir_resumen(
        {"ok": True, "destino": "C:/tmp/export.json", "cuentas": remotas},
        locales,
        {"renombres": [], "local_only_reales": [], "ambiguos": [], "omitidos": []},
        True,
    )
    texto = json.dumps(resumen, ensure_ascii=False, default=str)
    check(
        "resumen: no filtra token/password/email",
        all(
            secreto not in texto
            for secreto in ("TOKEN_SECRETO_QA", "PASS_SECRETA_QA", "secreto@example.test")
        ),
    )
    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        migrar._imprimir_resumen(resumen)
    check(
        "texto legible: no filtra token/password/email",
        all(
            secreto not in salida.getvalue()
            for secreto in ("TOKEN_SECRETO_QA", "PASS_SECRETA_QA", "secreto@example.test")
        ),
    )
    check("texto legible: muestra el conteo de exportadas", "Exportadas" in salida.getvalue())


# --------------------------------------------------------------------------- #
# (5) URL remota + CLI
# --------------------------------------------------------------------------- #
def test_url_remota(check):
    print("(5a) prioridad de la URL remota y Railway CLI")
    check(
        "arg: postgres:// se normaliza",
        migrar.obtener_url_remota("postgres://u:p@h/db") == "postgresql://u:p@h/db",
    )
    with mock.patch.dict(os.environ, {"DATABASE_URL_REMOTE": "postgresql://env:env@h/db"}, clear=False):
        with mock.patch.object(migrar, "_url_desde_railway_cli", side_effect=AssertionError("no CLI")):
            check(
                "arg gana a la variable de entorno",
                migrar.obtener_url_remota("postgresql://arg:arg@h/db")
                == "postgresql://arg:arg@h/db",
            )
            check(
                "env gana al CLI",
                migrar.obtener_url_remota(None) == "postgresql://env:env@h/db",
            )

    with mock.patch.object(migrar, "_leer_env_remota", return_value=""):
        with mock.patch.object(
            migrar, "_url_desde_railway_cli", return_value="postgres://cli:cli@h/db"
        ):
            check(
                "CLI como ultimo recurso (normalizado)",
                migrar.obtener_url_remota(None) == "postgresql://cli:cli@h/db",
            )

    proceso = types.SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"DATABASE_URL": "postgres://u:p@h:5432/db"}),
        stderr="",
    )
    with mock.patch.object(migrar.shutil, "which", return_value="railway.cmd"):
        with mock.patch.object(migrar.subprocess, "run", return_value=proceso) as ejecutado:
            url = migrar._url_desde_railway_cli()
            check("Railway CLI: parser de variables", url == "postgres://u:p@h:5432/db")
            check(
                "Railway CLI: invoca 'variables --json'",
                ejecutado.call_args.args[0][1:] == ["variables", "--json"],
            )

    fallo = types.SimpleNamespace(returncode=7, stdout="", stderr="password=SECRETO")
    with mock.patch.object(migrar.shutil, "which", return_value="railway.cmd"):
        with mock.patch.object(migrar.subprocess, "run", return_value=fallo):
            try:
                migrar._url_desde_railway_cli()
                mensaje = ""
            except Exception as exc:
                mensaje = str(exc)
            check("Railway CLI: fallo reportado", "fallo" in mensaje or "codigo" in mensaje)
            check("Railway CLI: el error no filtra credenciales", "SECRETO" not in mensaje)


def _json_cli(tmp: Path, remotas) -> Path:
    ruta = tmp / "cli_export.json"
    ruta.write_text(
        json.dumps(
            {
                "fecha": "2026-01-01T00:00:00",
                "origen": "mwx.app",
                "keep": [],
                "cuentas": remotas,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ruta


def test_cli(check):
    print("(5b) CLI: defaults, banderas, --json y dry-run forzado")
    parser = migrar._construir_parser()
    args = parser.parse_args([])
    check("CLI parser: sin --apply", args.apply is False)
    check("CLI parser: sin --json", args.como_json is False)
    check("CLI parser: sin --sin-renombrar", args.sin_renombrar is False)
    check("CLI parser: sin --dry-run", args.dry_run is False)

    remotas = [
        {"usuario": "remota_uno", "auth_token": "TOKEN_CLI_QA", "password": "PASS_CLI_QA",
         "email": "cli@example.test", "tier_calidad": "tier1", "seccion": "CI",
         "tipo_cuenta": "politica", "rol_activacion": "cita",
         "pausada_activacion": False, "activa": True},
    ]
    locales = [{"usuario": "local_solo_cli", "auth_token": "", "email": ""}]

    with tempfile.TemporaryDirectory() as tmp_texto:
        tmp = Path(tmp_texto)
        export_json = _json_cli(tmp, remotas)
        llamadas = {"export": [], "detectar": 0, "aplicar": 0, "url": []}

        def _fake_url(url=None):
            llamadas["url"].append(url)
            return "postgresql://usuario:secreto@host/db"

        def _fake_export(url, destino=None):
            llamadas["export"].append({"url": url, "destino": destino})
            return {
                "ok": True,
                "exportadas": len(remotas),
                "total": len(remotas),
                "destino": str(export_json),
                "origen": "mwx.app",
                "error": "",
            }

        def _fake_detectar(cuentas_remotas, cuentas_locales=None, session_factory=None):
            llamadas["detectar"] += 1
            return {
                "ok": True,
                "renombres": [{"anterior": "local_solo_cli", "nuevo": "remota_uno", "motivo": "token"}],
                "ambiguos": [],
                "omitidos": [],
                "local_only_reales": [],
                "error": "",
            }

        def _fake_aplicar(export_json_arg, renombres=None, **kwargs):
            llamadas["aplicar"] += 1
            return {
                "ok": True,
                "respaldo": {"db": "x.db", "cuentas_json": "x.json", "change": ""},
                "renombres": list(renombres or []),
                "restauracion": {"ok": True, "restauradas": 1, "actualizadas": 0},
                "verificacion": {
                    "ok": True,
                    "mismatches": [],
                    "faltantes": [],
                    "local_only": ["local_solo_cli"],
                },
                "error": "",
            }

        with mock.patch.object(migrar, "obtener_url_remota", _fake_url), \
                mock.patch.object(migrar, "exportar_cuentas_remotas", _fake_export), \
                mock.patch.object(migrar, "leer_cuentas_locales", lambda *a, **k: locales), \
                mock.patch.object(migrar, "detectar_duplicados_locales", _fake_detectar), \
                mock.patch.object(migrar, "aplicar", _fake_aplicar):
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                codigo = migrar.main(["--json", "--url", "postgres://arg:arg@h/db"])
            datos = json.loads(salida.getvalue() or "{}")
            check("CLI dry-run: codigo 0", codigo == 0)
            check("CLI dry-run: dry_run=True", datos.get("dry_run") is True)
            check("CLI dry-run: exportadas 1", datos.get("exportadas") == 1)
            check("CLI dry-run: 1 renombre", datos.get("total_renombres") == 1)
            check("CLI dry-run: no llama aplicar", llamadas["aplicar"] == 0)
            check("CLI dry-run: pasa --url", llamadas["url"][-1] == "postgres://arg:arg@h/db")
            check("CLI dry-run: usa el JSON exportado", datos.get("destino") == str(export_json))
            check(
                "CLI dry-run: no filtra secretos",
                "TOKEN_CLI_QA" not in salida.getvalue()
                and "PASS_CLI_QA" not in salida.getvalue()
                and "cli@example.test" not in salida.getvalue(),
            )

            llamadas["aplicar"] = 0
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                codigo = migrar.main(["--apply", "--json"])
            datos = json.loads(salida.getvalue() or "{}")
            check("CLI --apply: codigo 0", codigo == 0)
            check("CLI --apply: llama aplicar", llamadas["aplicar"] == 1)
            check("CLI --apply: dry_run=False", datos.get("dry_run") is False)

            llamadas["aplicar"] = 0
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                codigo = migrar.main(["--apply", "--dry-run", "--json"])
            datos = json.loads(salida.getvalue() or "{}")
            check("CLI --apply --dry-run: fuerzan simulacion", codigo == 0 and datos.get("dry_run") is True)
            check("CLI --apply --dry-run: NO llama aplicar", llamadas["aplicar"] == 0)

            llamadas["detectar"] = 0
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                codigo = migrar.main(["--sin-renombrar", "--json"])
            datos = json.loads(salida.getvalue() or "{}")
            check("CLI --sin-renombrar: no detecta duplicados", llamadas["detectar"] == 0)
            check(
                "CLI --sin-renombrar: local-only real = 1",
                datos.get("total_local_only_reales") == 1,
            )
            check("CLI --sin-renombrar: codigo 0", codigo == 0)

            # Fallo de exportacion: sale 1 y sin secretos.
            def _fake_export_falla(url, destino=None):
                return {"ok": False, "exportadas": 0, "error": "fallo sintetico"}

            with mock.patch.object(migrar, "exportar_cuentas_remotas", _fake_export_falla):
                salida = io.StringIO()
                with contextlib.redirect_stdout(salida):
                    codigo = migrar.main(["--json"])
                check("CLI export falla: codigo 1", codigo == 1)
                check("CLI export falla: mensaje claro", "fallo sintetico" in salida.getvalue())


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_serializacion_y_formato(check)
    test_duplicados(check)
    test_e2e(check)
    test_seguridad(check)
    test_url_remota(check)
    test_cli(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

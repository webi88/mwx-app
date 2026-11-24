# -*- coding: utf-8 -*-
"""Tests del panel de administracion del ejecutable MWX APP (dashboard web).

Cubren, SIN Streamlit runtime para la mayoria y SIN tocar `data/` real:

  (1) Usuarios del APP: creacion con PBKDF2, validacion de username/password,
      duplicado case-insensitive, login con `autenticar_app` y bloqueo de
      usuarios inactivos.
  (2) Asignaciones: `guardar_asignaciones` REEMPLAZA (delete+insert), dedupe,
      unique(usuario, cuenta) y el atajo `asignar_todas_seccion`.
  (3) Configuracion: normalizacion de hashtags/URLs, upsert (una sola fila),
      acotado de numeros e imagenes; guardado/borrado de archivos en una
      carpeta temporal (NUNCA en `data/` real).
  (4) Revocar sesiones (individual y masivo), desactivar impide login y
      eliminar borra usuario + asignaciones + config + eventos.
  (5) Listado/filtros de cuentas (seccion, tier, sesion, pausadas, busqueda).
  (6) `web/app.py`: la operacion esta en OPCIONES/CATEGORIAS y tiene su rama
      de despacho (modulo `web.operaciones.mwx_app`).
  (7) AppTest de la pagina: 0 excepciones en las 5 pestanas, widgets clave
      presentes y botones destructivos deshabilitados sin confirmacion; el
      rol operador recibe "Acceso restringido" sin renderizar pestanas.

Aislamiento: se crea una SQLite temporal, se monkeypatchea
`web.operaciones.mwx_app.obtener_sesion` y `resolver_ruta` (imagenes) y se
restaura todo al terminar. `DATABASE_URL` se apunta a un temporal ANTES de
importar (si el entorno no la traia definida).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_admin_web.py
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# Aislamiento temprano: si este modulo se ejecuta solo, la BD global jamas
# apunta a `data/gestor_redes.db`. En el runner compartido se mantiene la que
# ya exista (nuestras pruebas SIEMPRE parchean la sesion de la pagina).
_TMP_IMPORT = Path(tempfile.mkdtemp(prefix="mwx_admin_web_import_"))
os.environ.setdefault(
    "DATABASE_URL", f"sqlite:///{(_TMP_IMPORT / 'bootstrap.db').as_posix()}"
)

import atexit  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import core.models  # noqa: F401,E402
from core.app_auth import autenticar_app, verify_password  # noqa: E402
from core.database import Base  # noqa: E402
from core.models import (  # noqa: E402
    AppAsignacion,
    AppConfig,
    AppEvento,
    AppUsuario,
    Cuenta,
)
from web.operaciones import mwx_app  # noqa: E402

atexit.register(shutil.rmtree, _TMP_IMPORT, True)


# ---------------------------------------------------------------------------
# Infraestructura de aislamiento
# ---------------------------------------------------------------------------


@contextmanager
def _bd_temporal():
    """SQLite temporal + patch de `obtener_sesion` y `resolver_ruta`."""
    tmp = Path(tempfile.mkdtemp(prefix="mwx_admin_web_db_"))
    ruta = tmp / "app.db"
    engine = create_engine(
        f"sqlite:///{ruta.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Sesion = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _sesion():
        db = Sesion()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    original_sesion = mwx_app.obtener_sesion
    original_resolver = mwx_app.resolver_ruta
    mwx_app.obtener_sesion = _sesion
    mwx_app.resolver_ruta = lambda relativa: str(
        (tmp / str(relativa).replace("\\", "/")).resolve()
    )
    mwx_app.limpiar_cache_app()
    try:
        yield Sesion, tmp
    finally:
        mwx_app.obtener_sesion = original_sesion
        mwx_app.resolver_ruta = original_resolver
        mwx_app.limpiar_cache_app()
        engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def _crear_cuenta(
    Sesion,
    usuario: str,
    seccion: str = "",
    tier: str = "",
    auth_token: str = "",
    cookies=None,
    pausada: bool = False,
    activa: bool = True,
) -> int:
    """Inserta una Cuenta de Twitter de prueba y devuelve su id."""
    db = Sesion()
    try:
        cuenta = Cuenta(
            usuario=usuario,
            plataforma="twitter",
            activa=activa,
            seccion=seccion,
            tier_calidad=tier,
            auth_token=auth_token,
            cookies_json=cookies,
            pausada_activacion=pausada,
        )
        db.add(cuenta)
        db.commit()
        db.refresh(cuenta)
        return int(cuenta.id)
    finally:
        db.close()


def _crear_usuario_directo(Sesion, username="operador", password="clave1234"):
    """Crea un AppUsuario directo en la BD (para pruebas de asignaciones)."""
    from core.app_auth import hash_password

    db = Sesion()
    try:
        usuario = AppUsuario(
            username=username,
            password_hash=hash_password(password),
            nombre="Operador",
            activo=True,
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)
        return int(usuario.id)
    finally:
        db.close()


def _contar(Sesion, modelo) -> int:
    db = Sesion()
    try:
        return int(db.query(modelo).count())
    finally:
        db.close()


# ---------------------------------------------------------------------------
# (1) Usuarios
# ---------------------------------------------------------------------------


def _probar_usuarios(check) -> None:
    with _bd_temporal() as (Sesion, _tmp):
        resultado = mwx_app.crear_usuario_app(
            "Ana.Admin", "clave-segura", "Ana Admin", activo=True
        )
        check(
            "usuarios: se crea con username en minusculas",
            resultado.get("ok") is True and resultado.get("usuario_id"),
            str(resultado.get("error") or ""),
        )
        uid = int(resultado["usuario_id"])

        db = Sesion()
        try:
            usuario = db.get(AppUsuario, uid)
            check(
                "usuarios: guarda PBKDF2 (nunca la contrasena en claro)",
                usuario is not None
                and usuario.username == "ana.admin"
                and usuario.password_hash
                and usuario.password_hash != "clave-segura"
                and verify_password("clave-segura", usuario.password_hash)
                and not verify_password("otra-clave", usuario.password_hash),
            )
            check(
                "usuarios: activo por defecto y token_version=0",
                usuario.activo is True
                and int(usuario.token_version or 0) == 0
                and usuario.nombre == "Ana Admin",
            )
        finally:
            db.close()

        duplicado = mwx_app.crear_usuario_app(
            "ANA.ADMIN", "otra-clave-1", "", activo=True
        )
        check(
            "usuarios: duplicado case-insensitive bloqueado",
            duplicado.get("ok") is False
            and "existe" in str(duplicado.get("error", "")).lower(),
            str(duplicado.get("error")),
        )
        check(
            "usuarios: no se creo una segunda fila",
            _contar(Sesion, AppUsuario) == 1,
        )

        for invalido in ("ab", "con espacio", "a" * 21, ""):
            res = mwx_app.crear_usuario_app(invalido, "clave-segura", "")
            check(
                f"usuarios: username invalido rechazado ({invalido!r})",
                res.get("ok") is False and bool(res.get("error")),
            )
        check(
            "usuarios: mayusculas se normalizan a minusculas",
            mwx_app._normalizar_username("MAYUSCULAS") == "mayusculas"
            and mwx_app.validar_username("MAYUSCULAS") == "",
        )

        corta = mwx_app.crear_usuario_app("valido.uno", "corta", "")
        check(
            "usuarios: password de menos de 8 rechazada",
            corta.get("ok") is False and "8" in str(corta.get("error")),
        )

        # Login real (case-insensitive) contra la BD temporal.
        db = Sesion()
        try:
            autenticado = autenticar_app(db, "ANA.ADMIN", "clave-segura")
            check(
                "usuarios: login con verify_password funciona (case-insensitive)",
                autenticado is not None and int(autenticado.id) == uid,
            )
            check(
                "usuarios: login actualiza ultimo_acceso",
                autenticado is not None and autenticado.ultimo_acceso is not None,
            )
            check(
                "usuarios: password incorrecta no autentica",
                autenticar_app(db, "ana.admin", "clave-mala") is None,
            )
        finally:
            db.close()

        inactivo = mwx_app.crear_usuario_app(
            "inactivo.uno", "clave-segura", "Inactivo", activo=False
        )
        db = Sesion()
        try:
            check(
                "usuarios: inactivo se guarda con activo=False",
                inactivo.get("ok") is True
                and db.get(AppUsuario, int(inactivo["usuario_id"])).activo is False,
            )
            check(
                "usuarios: inactivo no puede iniciar sesion",
                autenticar_app(db, "inactivo.uno", "clave-segura") is None,
            )
        finally:
            db.close()

        filas = mwx_app.listar_usuarios_app()
        claves = set(filas[0]) if filas else set()
        check(
            "usuarios: listar devuelve metadata y NUNCA password/hash",
            len(filas) == 2
            and all(
                "password" not in clave.lower() and "hash" not in clave.lower()
                for clave in claves
            )
            and {"id", "username", "nombre", "activo", "cuentas"} <= claves,
            str(sorted(claves)),
        )


# ---------------------------------------------------------------------------
# (2) Asignaciones
# ---------------------------------------------------------------------------


def _probar_asignaciones(check) -> None:
    with _bd_temporal() as (Sesion, _tmp):
        uid = _crear_usuario_directo(Sesion)
        c1 = _crear_cuenta(Sesion, "cuenta_lib_1", "LIB", "tier1", auth_token="tok1")
        c2 = _crear_cuenta(Sesion, "cuenta_ci_2", "CI", "tier2", cookies=[{"name": "a"}])
        c3 = _crear_cuenta(Sesion, "cuenta_lib_3", "LIB", "tier3", pausada=True)

        res = mwx_app.guardar_asignaciones(uid, [c1, c2, c2, "basura"])
        check(
            "asignaciones: guarda IDs validos y deduplica",
            res.get("ok") is True and res.get("total") == 2,
            str(res),
        )
        check(
            "asignaciones: listar devuelve exactamente las asignadas",
            mwx_app.listar_asignaciones(uid) == sorted([c1, c2]),
        )

        res = mwx_app.guardar_asignaciones(uid, [c3])
        check(
            "asignaciones: REEMPLAZA la seleccion (una sola transaccion)",
            res.get("ok") is True
            and res.get("total") == 1
            and mwx_app.listar_asignaciones(uid) == [c3]
            and _contar(Sesion, AppAsignacion) == 1,
        )

        res = mwx_app.guardar_asignaciones(uid, [])
        check(
            "asignaciones: lista vacia borra todas",
            res.get("ok") is True
            and res.get("total") == 0
            and mwx_app.listar_asignaciones(uid) == []
            and _contar(Sesion, AppAsignacion) == 0,
        )

        res = mwx_app.guardar_asignaciones(uid, [c1])
        res2 = mwx_app.asignar_todas_seccion(uid, "LIB")
        check(
            "asignaciones: atajo por seccion AGREGA sin borrar las previas",
            res2.get("ok") is True
            and res2.get("seccion") == "LIB"
            and res2.get("agregadas") == 1
            and mwx_app.listar_asignaciones(uid) == sorted([c1, c3]),
            str(res2),
        )
        check(
            "asignaciones: atajo no duplica el unique(usuario, cuenta)",
            _contar(Sesion, AppAsignacion) == 2,
        )
        check(
            "asignaciones: seccion invalida no escribe",
            mwx_app.asignar_todas_seccion(uid, "NO-EXISTE").get("ok") is False,
        )
        check(
            "asignaciones: usuario inexistente no escribe",
            mwx_app.guardar_asignaciones(999999, [c1]).get("ok") is False,
        )


# ---------------------------------------------------------------------------
# (3) Configuracion + imagenes
# ---------------------------------------------------------------------------


class _ArchivoFalso:
    """UploadedFile minimo (name + getbuffer) para probar las imagenes."""

    def __init__(self, nombre: str, contenido: bytes):
        self.name = nombre
        self._contenido = contenido

    def getbuffer(self):
        return memoryview(self._contenido)


def _probar_configuracion(check) -> None:
    with _bd_temporal() as (Sesion, tmp):
        uid = _crear_usuario_directo(Sesion)

        check(
            "config: hashtags normalizados con # y maximo 6 sin duplicados",
            mwx_app.normalizar_hashtags(
                "mexico, #Futbol  #MEXICO #uno #dos #tres #cuatro #cinco"
            )
            == ["#mexico", "#Futbol", "#uno", "#dos", "#tres", "#cuatro"],
        )
        check(
            "config: URLs solo http(s), sin duplicados",
            mwx_app.normalizar_urls(
                "https://x.com/a/status/1\nno-es-url\n"
                "https://x.com/a/status/1\nhttp://b.example/2"
            )
            == ["https://x.com/a/status/1", "http://b.example/2"],
        )
        check(
            "config: CONFIG_DEFECTO incluye navegadores=1",
            mwx_app.CONFIG_DEFECTO.get("navegadores") == 1,
            str(mwx_app.CONFIG_DEFECTO.get("navegadores")),
        )

        res = mwx_app.guardar_config_app(
            uid,
            {
                "hashtags": "mexico #futbol",
                "contexto": "tema manual",
                "narrativa": "noticia de fondo",
                "urls": "https://x.com/a/status/1\nbasura",
                "texto_base": "base de la cita",
                "acciones_por_cuenta": 999,
                "pausa_seg": 0,
                "duracion_min": 9999,
                "limite_dia": 0,
                "navegadores": 2,
                "imagenes": ["../evil.png", "foto.png", "foto.png"],
                "permitir_ajustes": True,
            },
        )
        check(
            "config: upsert guarda y acota los numeros",
            res.get("ok") is True
            and res["config"]["acciones_por_cuenta"] == 49
            and res["config"]["pausa_seg"] == 1
            and res["config"]["duracion_min"] == 240
            and res["config"]["limite_dia"] == 1
            and res["config"]["navegadores"] == 2,
            str(res.get("error") or res.get("config")),
        )
        check(
            "config: imagenes invalidas descartadas (sin traversal)",
            res["config"]["imagenes"] == ["foto.png"],
            str(res["config"]["imagenes"]),
        )
        check(
            "config: una sola fila por usuario",
            _contar(Sesion, AppConfig) == 1,
        )

        leida = mwx_app.obtener_config_app(uid)
        check(
            "config: leer decodifica listas y respeta valores",
            leida["hashtags"] == ["#mexico", "#futbol"]
            and leida["urls"] == ["https://x.com/a/status/1"]
            and leida["contexto"] == "tema manual"
            and leida["narrativa"] == "noticia de fondo"
            and leida["texto_base"] == "base de la cita"
            and leida["imagenes"] == ["foto.png"]
            and leida["permitir_ajustes"] is True
            and leida["navegadores"] == 2,
            str(leida),
        )

        res2 = mwx_app.guardar_config_app(
            uid,
            {"hashtags": "otro", "contexto": "nuevo tema", "permitir_ajustes": False},
        )
        check(
            "config: segundo guardado ACTUALIZA la misma fila",
            res2.get("ok") is True
            and _contar(Sesion, AppConfig) == 1
            and mwx_app.obtener_config_app(uid)["contexto"] == "nuevo tema"
            and mwx_app.obtener_config_app(uid)["permitir_ajustes"] is False,
        )
        check(
            "config: usuario inexistente no crea fila",
            mwx_app.guardar_config_app(999999, {"hashtags": "x"}).get("ok") is False
            and _contar(Sesion, AppConfig) == 1,
        )

        # ---- Navegadores: guardado acotado y lectura tolerante ----
        for valor, esperado in ((2, 2), (0, 1), (9, 3), (None, 1), ("basura", 1)):
            guardado = mwx_app.guardar_config_app(
                uid, {"navegadores": valor}
            )
            check(
                f"config: navegadores {valor!r} se guarda como {esperado}",
                guardado.get("ok") is True
                and guardado["config"]["navegadores"] == esperado
                and mwx_app.obtener_config_app(uid)["navegadores"] == esperado,
                str(guardado.get("error") or guardado.get("config")),
            )

        class _FilaFake:
            """Fila AppConfig minima (sin `navegadores` = BD vieja)."""

            hashtags = "[]"
            contexto = ""
            narrativa = ""
            urls = "[]"
            texto_base = ""
            acciones_por_cuenta = 10
            pausa_seg = 2
            duracion_min = 60
            limite_dia = 49
            imagenes = "[]"
            permitir_ajustes = False

        class _DbFake:
            def __init__(self, fila):
                self._fila = fila

            def query(self, *_a, **_k):
                return self

            def filter(self, *_a, **_k):
                return self

            def first(self):
                return self._fila

        @contextmanager
        def _sesion_con(fila):
            yield _DbFake(fila)

        original_sesion = mwx_app.obtener_sesion
        try:
            fila_con = type(
                "_FilaConNavegadores", (_FilaFake,), {"navegadores": 2}
            )()
            mwx_app.obtener_sesion = lambda: _sesion_con(fila_con)
            check(
                "config: obtener_config_app lee navegadores=2",
                mwx_app.obtener_config_app(uid)["navegadores"] == 2,
            )

            mwx_app.obtener_sesion = lambda: _sesion_con(_FilaFake())
            check(
                "config: fila sin atributo navegadores -> 1 (BD vieja)",
                mwx_app.obtener_config_app(uid)["navegadores"] == 1,
            )

            for valor, esperado in (
                (0, 1),
                (9, 3),
                (None, 1),
                ("x", 1),
                (-5, 1),
                (2, 2),
            ):
                fila = type("_FilaNav", (_FilaFake,), {"navegadores": valor})()
                mwx_app.obtener_sesion = lambda f=fila: _sesion_con(f)
                check(
                    f"config: navegadores leidos {valor!r} -> {esperado}",
                    mwx_app.obtener_config_app(uid)["navegadores"] == esperado,
                )
        finally:
            mwx_app.obtener_sesion = original_sesion
            mwx_app.limpiar_cache_app()

        # ---- Imagenes (carpeta temporal) ----
        archivo = _ArchivoFalso("Foto Verano.PNG", b"\x89PNG-falso")
        nombres, errores = mwx_app.guardar_imagenes_mwx("ana.admin", [archivo], [])
        check(
            "imagenes: guarda con nombre seguro y extension normalizada",
            nombres == ["Foto_Verano.png"] and errores == [],
            str((nombres, errores)),
        )
        ruta = Path(tmp) / "data" / "mwx_app" / "ana.admin" / "imagenes" / nombres[0]
        check(
            "imagenes: el archivo existe fuera de data/ real",
            ruta.is_file() and ruta.read_bytes() == b"\x89PNG-falso",
            str(ruta),
        )
        check(
            "imagenes: no se escribe en el data/ del proyecto",
            not (RAIZ / "data" / "mwx_app" / "ana.admin" / "imagenes").exists(),
        )

        nombres2, _ = mwx_app.guardar_imagenes_mwx(
            "ana.admin", [_ArchivoFalso("Foto Verano.PNG", b"x")], nombres
        )
        check(
            "imagenes: mismo nombre se guarda como unico (_1)",
            len(nombres2) == 2
            and nombres2[0] == "Foto_Verano.png"
            and nombres2[1] == "Foto_Verano_1.png",
            str(nombres2),
        )
        nombres3, _ = mwx_app.guardar_imagenes_mwx("ana.admin", [], nombres2)
        check(
            "imagenes: sin subida nueva conserva las existentes",
            nombres3 == nombres2,
        )

        mwx_app.guardar_config_app(
            uid, {"hashtags": "x", "imagenes": nombres2}
        )
        borrado = mwx_app.eliminar_imagen_mwx(uid, "ana.admin", nombres2[0])
        restantes = mwx_app.obtener_config_app(uid)["imagenes"]
        check(
            "imagenes: eliminar la quita de la config y del disco",
            borrado.get("ok") is True
            and restantes == [nombres2[1]]
            and not (
                Path(tmp)
                / "data"
                / "mwx_app"
                / "ana.admin"
                / "imagenes"
                / nombres2[0]
            ).exists(),
            str(restantes),
        )
        check(
            "imagenes: nombres con traversal no se aceptan",
            mwx_app._nombre_imagen_permitido("../evil.png") is False
            and mwx_app._nombre_imagen_permitido("ok.png") is True
            and mwx_app.eliminar_imagen_mwx(uid, "ana.admin", "../x.png")[
                "ok"
            ]
            is False,
        )


# ---------------------------------------------------------------------------
# (4) Revocar / desactivar / eliminar
# ---------------------------------------------------------------------------


def _probar_sesiones_y_borrado(check) -> None:
    with _bd_temporal() as (Sesion, _tmp):
        uid1 = _crear_usuario_directo(Sesion, "operador.uno")
        uid2 = _crear_usuario_directo(Sesion, "operador.dos")
        c1 = _crear_cuenta(Sesion, "cuenta_uno", "CI", "tier1", auth_token="t")
        c2 = _crear_cuenta(Sesion, "cuenta_dos", "LIB", "tier1", auth_token="t")

        check(
            "sesiones: revocar sube token_version del usuario",
            mwx_app.revocar_sesiones_usuario(uid1).get("version") == 1
            and mwx_app.revocar_sesiones_usuario(uid1).get("version") == 2,
        )
        db = Sesion()
        try:
            check(
                "sesiones: el nuevo token_version quedo persistido",
                int(db.get(AppUsuario, uid1).token_version) == 2
                and int(db.get(AppUsuario, uid2).token_version) == 0,
            )
        finally:
            db.close()
        check(
            "sesiones: usuario inexistente -> error claro",
            mwx_app.revocar_sesiones_usuario(999999).get("ok") is False,
        )

        res = mwx_app.revocar_todas_las_sesiones()
        db = Sesion()
        try:
            check(
                "sesiones: revocar TODAS incrementa a cada usuario",
                res.get("ok") is True
                and res.get("total") == 2
                and int(db.get(AppUsuario, uid1).token_version) == 3
                and int(db.get(AppUsuario, uid2).token_version) == 1,
                str(res),
            )
        finally:
            db.close()

        # Desactivar: impide login y revoca sesiones.
        res = mwx_app.cambiar_estado_usuario(uid2, False)
        db = Sesion()
        try:
            check(
                "usuarios: desactivar impide login y revoca sesiones",
                res.get("ok") is True
                and db.get(AppUsuario, uid2).activo is False
                and int(db.get(AppUsuario, uid2).token_version) == 2
                and autenticar_app(db, "operador.dos", "clave1234") is None,
            )
        finally:
            db.close()
        check(
            "usuarios: reactivar no rompe",
            mwx_app.cambiar_estado_usuario(uid2, True).get("ok") is True,
        )

        # Eliminar: borra dependencias y no toca al otro usuario.
        mwx_app.guardar_asignaciones(uid1, [c1, c2])
        mwx_app.guardar_config_app(uid1, {"hashtags": "x"})
        mwx_app.registrar_evento_app(uid1, "prueba", "detalle")
        mwx_app.guardar_asignaciones(uid2, [c1])
        mwx_app.guardar_config_app(uid2, {"hashtags": "y"})

        res = mwx_app.eliminar_usuario_app(uid1)
        db = Sesion()
        try:
            check(
                "eliminar: borra usuario + asignaciones + config + eventos",
                res.get("ok") is True
                and db.get(AppUsuario, uid1) is None
                and db.query(AppAsignacion)
                .filter(AppAsignacion.usuario_id == uid1)
                .count()
                == 0
                and db.query(AppConfig)
                .filter(AppConfig.usuario_id == uid1)
                .count()
                == 0
                and db.query(AppEvento)
                .filter(AppEvento.usuario_id == uid1)
                .count()
                == 0
                and res.get("asignaciones") == 2
                and res.get("config") == 1
                and res.get("eventos") >= 1,
                str(res),
            )
            check(
                "eliminar: no toca al otro usuario ni sus dependencias",
                db.get(AppUsuario, uid2) is not None
                and db.query(AppAsignacion)
                .filter(AppAsignacion.usuario_id == uid2)
                .count()
                == 1
                and db.query(AppConfig)
                .filter(AppConfig.usuario_id == uid2)
                .count()
                == 1,
            )
        finally:
            db.close()
        check(
            "eliminar: usuario inexistente -> error claro",
            mwx_app.eliminar_usuario_app(999999).get("ok") is False,
        )


# ---------------------------------------------------------------------------
# (5) Listado / filtros de cuentas
# ---------------------------------------------------------------------------


def _probar_listado_cuentas(check) -> None:
    with _bd_temporal() as (Sesion, _tmp):
        c_lib1 = _crear_cuenta(
            Sesion, "ana_lib", "LIB", "tier1", auth_token="tok"
        )
        c_ci2 = _crear_cuenta(
            Sesion, "beto_ci", "CI", "tier2", cookies={"cookies": [{"name": "x"}]}
        )
        c_sin = _crear_cuenta(Sesion, "sin_tier", "", "", pausada=True)
        c_otra_plataforma = _crear_cuenta(Sesion, "en_facebook", "LIB", "tier1")
        db = Sesion()
        try:
            db.get(Cuenta, c_otra_plataforma).plataforma = "facebook"
            db.commit()
        finally:
            db.close()
        mwx_app.limpiar_cache_app()

        todas = mwx_app.listar_cuentas_app(mostrar_pausadas=True)
        ids = {f["id"] for f in todas}
        check(
            "cuentas: lista solo Twitter y arma etiqueta con seccion/tier",
            ids == {c_lib1, c_ci2, c_sin}
            and any(
                f["id"] == c_lib1
                and f["label"].startswith("@ana_lib ·")
                and "LIB" in f["label"]
                and "Tier 1" in f["label"]
                for f in todas
            ),
            str(sorted(ids)),
        )
        check(
            "cuentas: filtro por seccion",
            {f["id"] for f in mwx_app.listar_cuentas_app(seccion="LIB", mostrar_pausadas=True)}
            == {c_lib1},
        )
        check(
            "cuentas: filtro por tier y 'sin tier'",
            {f["id"] for f in mwx_app.listar_cuentas_app(tier="tier2", mostrar_pausadas=True)}
            == {c_ci2}
            and {
                f["id"]
                for f in mwx_app.listar_cuentas_app(
                    tier="sin", mostrar_pausadas=True
                )
            }
            == {c_sin},
        )
        check(
            "cuentas: filtro 'solo con sesion'",
            {f["id"] for f in mwx_app.listar_cuentas_app(solo_con_sesion=True, mostrar_pausadas=True)}
            == {c_lib1, c_ci2},
        )
        check(
            "cuentas: pausadas ocultas salvo incluir_ids",
            {f["id"] for f in mwx_app.listar_cuentas_app()}
            == {c_lib1, c_ci2}
            and {f["id"] for f in mwx_app.listar_cuentas_app(incluir_ids=(c_sin,))}
            == {c_lib1, c_ci2, c_sin},
        )
        check(
            "cuentas: busqueda por usuario/handle/nombre",
            {f["id"] for f in mwx_app.listar_cuentas_app(busqueda="beto", mostrar_pausadas=True)}
            == {c_ci2}
            and not mwx_app.listar_cuentas_app(busqueda="inexistente", mostrar_pausadas=True),
        )
        check(
            "cuentas: deteccion de sesion tolera str JSON, lista y dict",
            mwx_app._cuenta_tiene_sesion(
                type("C", (), {"auth_token": "", "cookies_json": '[{"name":"a"}]'})()
            )
            and mwx_app._cuenta_tiene_sesion(
                type("C", (), {"auth_token": "", "cookies_json": [{"name": "a"}]})()
            )
            and mwx_app._cuenta_tiene_sesion(
                type("C", (), {"auth_token": "tok", "cookies_json": None})()
            )
            and not mwx_app._cuenta_tiene_sesion(
                type("C", (), {"auth_token": "", "cookies_json": "[]"})()
            ),
        )


# ---------------------------------------------------------------------------
# (6) web/app.py: registro + dispatch
# ---------------------------------------------------------------------------


def _constantes_app() -> dict:
    fuente = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    constantes = {}
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign):
            for objetivo in nodo.targets:
                if isinstance(objetivo, ast.Name):
                    try:
                        constantes[objetivo.id] = ast.literal_eval(nodo.value)
                    except Exception:
                        pass
    return constantes


def _modulos_operaciones_app() -> set:
    fuente = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    modulos = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            if nodo.module.startswith("web.operaciones."):
                modulos.add(nodo.module.rsplit(".", 1)[-1])
    return modulos


def _prefijos_despacho_app() -> set:
    fuente = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    prefijos = set()
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "startswith"
            and nodo.args
            and isinstance(nodo.args[0], ast.Constant)
            and isinstance(nodo.args[0].value, str)
        ):
            prefijos.add(nodo.args[0].value)
    return prefijos


def _probar_navegacion(check) -> None:
    constantes = _constantes_app()
    opciones = constantes.get("OPCIONES") or []
    admin = constantes.get("OPCIONES_ADMIN") or []
    categorias = constantes.get("CATEGORIAS") or []
    op = "📲 MWX APP: Usuarios y Acceso"

    check(
        "navegacion: la operacion MWX APP es SOLO para admin",
        op in admin and op not in opciones,
        f"(opciones={len(opciones)}, admin={len(admin)})",
    )
    check(
        "navegacion: aparece en UNA sola categoria y es Datos y cuentas",
        [nombre for nombre, ops in categorias if op in ops]
        == ["📊 Datos y cuentas"],
        str([nombre for nombre, ops in categorias if op in ops]),
    )
    check(
        "navegacion: el dispatch importa web.operaciones.mwx_app",
        "mwx_app" in _modulos_operaciones_app()
        and (RAIZ / "web" / "operaciones" / "mwx_app.py").is_file(),
    )
    check(
        "navegacion: existe la rama startswith del prefijo 📲",
        "📲" in _prefijos_despacho_app(),
        str(sorted(_prefijos_despacho_app())),
    )


# ---------------------------------------------------------------------------
# (7) AppTest de la pagina
# ---------------------------------------------------------------------------


def _app_mwx_admin():
    """Script de AppTest (SOLO ASCII): render completo como admin."""
    from web.operaciones import mwx_app

    mwx_app.render({"rol": "admin", "username": "admin"})


def _app_mwx_operador():
    """Script de AppTest (SOLO ASCII): rol sin permisos."""
    from web.operaciones import mwx_app

    mwx_app.render({"rol": "operador", "username": "operador"})


def _probar_apptest(check) -> None:
    from streamlit.testing.v1 import AppTest

    # BD vacia (primer arranque): las 5 pestanas deben renderizar igual.
    with _bd_temporal():
        try:
            vacio = AppTest.from_function(_app_mwx_admin, default_timeout=60)
            vacio.run()
            error_vacio = (
                str(vacio.exception[0].value)[:200] if vacio.exception else ""
            )
        except Exception as exc:  # pragma: no cover
            vacio = None
            error_vacio = f"{type(exc).__name__}: {exc}"
        check(
            "apptest: con BD vacia renderiza sin excepciones y pide crear usuario",
            vacio is not None
            and not error_vacio
            and len(vacio.tabs) == 5
            and any("Crea" in str(i.value) for i in vacio.info),
            error_vacio,
        )

    with _bd_temporal() as (Sesion, _tmp):
        uid = _crear_usuario_directo(Sesion, "ana.admin")
        c1 = _crear_cuenta(Sesion, "cuenta_lib", "LIB", "tier1", auth_token="tok")
        c2 = _crear_cuenta(Sesion, "cuenta_ci", "CI", "tier2", auth_token="tok")
        mwx_app.guardar_asignaciones(uid, [c1])
        mwx_app.guardar_config_app(
            uid,
            {
                "hashtags": "mexico futbol",
                "contexto": "tema",
                "urls": "https://x.com/a/status/1",
                "acciones_por_cuenta": 7,
                "navegadores": 2,
                "imagenes": [],
            },
        )
        mwx_app.registrar_evento_app(uid, "login", "evento de prueba")
        mwx_app.limpiar_cache_app()

        try:
            at = AppTest.from_function(_app_mwx_admin, default_timeout=90)
            at.run()
            error = str(at.exception[0].value)[:300] if at.exception else ""
        except Exception as exc:  # pragma: no cover - fallo de infraestructura
            error = f"{type(exc).__name__}: {exc}"
            at = None

        check(
            "apptest: la pagina MWX APP renderiza sin excepciones",
            at is not None and not error,
            error,
        )
        if at is None or error:
            return

        check(
            "apptest: tiene las 5 pestanas",
            len(at.tabs) == 5,
            f"(tabs={len(at.tabs)})",
        )

        claves_texto = {w.key for w in at.text_input}
        claves_num = {w.key for w in at.number_input}
        claves_sel = {w.key for w in at.selectbox}
        claves_multi = {w.key for w in at.multiselect}
        claves_botones = {w.key for w in at.button}
        claves_check = {w.key for w in at.checkbox}

        check(
            "apptest: pestana Ejecutable con version, obligatoria y publicar",
            "mwx_release_version" in claves_texto
            and "mwx_release_obligatorio" in claves_check
            and "mwx_release_publicar" in claves_botones,
            str(sorted(claves_texto)),
        )

        check(
            "apptest: formulario de creacion presente",
            {"mwx_new_username", "mwx_new_password", "mwx_new_password2"}
            <= claves_texto
            and "mwx_new_activo" in claves_check
            and any(b.label == "Crear usuario" for b in at.button),
            str(sorted(claves_texto)),
        )
        check(
            "apptest: tabla/gestion de usuarios y confirmaciones presentes",
            "mwx_user_sel" in claves_sel
            and f"mwx_user_conf_revocar_{uid}" in claves_check
            and f"mwx_user_conf_eliminar_{uid}" in claves_check,
            str(sorted(claves_sel)),
        )
        check(
            "apptest: el borrado de usuario esta deshabilitado sin confirmar",
            at.button(key="mwx_user_btn_eliminar").disabled is True
            and at.button(key="mwx_user_btn_revocar").disabled is True,
        )

        check(
            "apptest: asignacion con multiselect y guardado",
            "mwx_asig_usuario" in claves_sel
            and "mwx_asig_cuentas" in claves_multi
            and "mwx_asig_btn_guardar" in claves_botones
            and "mwx_asig_btn_todas" in claves_botones,
            str(sorted(claves_multi)),
        )
        widget_multi = at.multiselect(key="mwx_asig_cuentas")
        opciones = [str(o) for o in widget_multi.options]
        seleccion = list(widget_multi.value)
        check(
            "apptest: preselecciona las cuentas ya asignadas",
            len(opciones) == 2
            and any("@cuenta_ci" in o and "CI" in o and "Tier 2" in o for o in opciones)
            and any("@cuenta_lib" in o and "LIB" in o and "Tier 1" in o for o in opciones)
            and seleccion == [c1],
            f"(options={opciones}, value={seleccion})",
        )

        # E2E del boton: cambia la seleccion y guarda (delete+insert real).
        try:
            at.multiselect(key="mwx_asig_cuentas").set_value([c2]).run()
            at.button(key="mwx_asig_btn_guardar").click().run()
            error_guardar = (
                str(at.exception[0].value)[:200] if at.exception else ""
            )
        except Exception as exc:  # pragma: no cover
            error_guardar = f"{type(exc).__name__}: {exc}"
        check(
            "apptest: guardar asignaciones actualiza la BD y muestra exito",
            not error_guardar
            and mwx_app.listar_asignaciones(uid) == [c2]
            and any(
                "Asignaciones guardadas" in str(s.value)
                for s in at.success
            ),
            error_guardar,
        )

        check(
            "apptest: campana con hashtags, numeros e imagenes",
            f"mwx_cfg_{uid}_hashtags" in claves_texto
            and f"mwx_cfg_{uid}_urls" in {w.key for w in at.text_area}
            and f"mwx_cfg_{uid}_acciones" in claves_num
            and f"mwx_cfg_{uid}_pausa" in claves_num
            and f"mwx_cfg_{uid}_duracion" in claves_num
            and f"mwx_cfg_{uid}_limite" in claves_num
            and f"mwx_cfg_{uid}_navegadores" in claves_num
            and f"mwx_cfg_{uid}_guardar" in claves_botones,
            str(sorted(claves_num)),
        )
        check(
            "apptest: navegadores parte de la config guardada (2)",
            at.number_input(key=f"mwx_cfg_{uid}_navegadores").value == 2,
            str(at.number_input(key=f"mwx_cfg_{uid}_navegadores").value),
        )

        # E2E del boton: cambia navegadores y guarda la config (real).
        try:
            at.number_input(key=f"mwx_cfg_{uid}_navegadores").set_value(3).run()
            at.button(key=f"mwx_cfg_{uid}_guardar").click().run()
            error_nav = (
                str(at.exception[0].value)[:200] if at.exception else ""
            )
        except Exception as exc:  # pragma: no cover
            error_nav = f"{type(exc).__name__}: {exc}"
        check(
            "apptest: guardar configuracion persiste navegadores=3",
            not error_nav
            and mwx_app.obtener_config_app(uid)["navegadores"] == 3
            and any(
                "Configuraci" in str(s.value) for s in at.success
            ),
            error_nav,
        )

        # BD vieja / sin fila: el number_input vuelve al default 1.
        db = Sesion()
        try:
            db.query(AppConfig).filter(AppConfig.usuario_id == uid).delete()
            db.commit()
        finally:
            db.close()
        try:
            at3 = AppTest.from_function(_app_mwx_admin, default_timeout=60)
            at3.run()
            error_at3 = (
                str(at3.exception[0].value)[:200] if at3.exception else ""
            )
            valor_default = (
                None
                if error_at3
                else at3.number_input(
                    key=f"mwx_cfg_{uid}_navegadores"
                ).value
            )
        except Exception as exc:  # pragma: no cover
            error_at3 = f"{type(exc).__name__}: {exc}"
            valor_default = None
        check(
            "apptest: sin fila de config, navegadores inicia en 1",
            not error_at3 and valor_default == 1,
            error_at3 or str(valor_default),
        )
        check(
            "apptest: actividad/seguridad con revocacion global y URL del API",
            "mwx_seg_usuario" in claves_sel
            and "mwx_seg_btn_revocar" in claves_botones
            and "mwx_seg_btn_desactivar" in claves_botones
            and "mwx_seg_api_url" in claves_texto
            and at.button(key="mwx_seg_btn_revocar").disabled is True,
            str(sorted(claves_botones)),
        )
        check(
            "apptest: muestra el evento sembrado en la tabla",
            any(
                "login" in (
                    tabla.value.to_string()
                    if hasattr(tabla.value, "to_string")
                    else str(tabla.value)
                )
                for tabla in at.dataframe
            ),
        )

        # Rol sin permisos: error claro, sin pestanas ni excepciones.
        try:
            at2 = AppTest.from_function(_app_mwx_operador, default_timeout=60)
            at2.run()
            error2 = str(at2.exception[0].value)[:200] if at2.exception else ""
        except Exception as exc:  # pragma: no cover
            at2 = None
            error2 = f"{type(exc).__name__}: {exc}"
        check(
            "apptest: operador recibe Acceso restringido sin excepciones",
            at2 is not None
            and not error2
            and any(
                "Acceso restringido" in str(e.value) for e in at2.error
            )
            and len(at2.tabs) == 0,
            error2,
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(check) -> None:
    _probar_usuarios(check)
    _probar_asignaciones(check)
    _probar_configuracion(check)
    _probar_sesiones_y_borrado(check)
    _probar_listado_cuentas(check)
    _probar_navegacion(check)
    _probar_apptest(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

"""Tests rapidos de `cuentas/eliminador.py` y del CLI `eliminar_cuentas.py`.

Sin red, sin Chrome y SIN base de datos real: `cuentas.eliminador.get_db_session`
se reemplaza por una sesion falsa (mismo patron que `tests/test_restaurador.py`)
y las carpetas de datos se apuntan a un directorio temporal. TODOS los datos son
sinteticos; jamas se toca una cuenta real ni la BD del proyecto.

Cubre:
    (1) parser: linea del vendedor, `@`, comentarios, vacias, duplicados
        case-insensitive (conserva la primera forma).
    (2) busqueda case-insensitive + no encontradas + error de BD sin lanzar.
    (3) eliminacion completa: respaldo JSON en tmp con su contenido, pkl de
        cookies borradas de tmp, avatar/portada SOLO dentro de data/, tareas
        canceladas solo si TODOS sus ids pertenecen a las eliminadas (las
        mixtas quedan intactas), commit y lista de eliminadas.
    (4) opciones: sin respaldo, sin cookies, sin tareas y ruta_respaldo custom.
    (5) errores: respaldo fallido NO borra nada, error de BD en query/delete
        sin lanzar y con rollback; lista vacia.
    (6) CLI `main()`: dry-run por defecto (solo buscar), `--apply` llama a
        `eliminar_usuarios`, banderas, `--json`, nunca imprime credenciales.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_eliminador.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import eliminar_cuentas  # noqa: E402
from core.models import Cuenta, Tarea  # noqa: E402
from cuentas import eliminador  # noqa: E402
from cuentas.eliminador import (  # noqa: E402
    buscar_cuentas,
    eliminar_usuarios,
    parsear_lista_usuarios,
)

# --------------------------------------------------------------------------- #
# Fakes de base de datos (nunca se conecta a una BD real)
# --------------------------------------------------------------------------- #
def _cumple(objeto, criterio) -> bool:
    """Evalua un criterio SQLAlchemy simple contra un objeto fake.

    Soporta los criterios que usa `eliminador`:
        Cuenta.plataforma == "twitter"      -> left.name == "plataforma"
        Tarea.estado == "pendiente"         -> left.name == "estado"
        Cuenta.usuario.in_([...])           -> left.name == "usuario"
        func.lower(Cuenta.usuario).in_([..])-> left.name == "lower"
    """
    izquierda = getattr(criterio, "left", None)
    derecha = getattr(criterio, "right", None)
    valor = getattr(derecha, "value", derecha)
    nombre = getattr(izquierda, "name", "")
    if nombre == "lower":
        lista = valor if isinstance(valor, (list, tuple, set)) else []
        actual = str(getattr(objeto, "usuario", "") or "").lower()
        return actual in {str(v).lower() for v in lista}
    if nombre == "usuario":
        lista = valor if isinstance(valor, (list, tuple, set)) else [valor]
        return getattr(objeto, "usuario", None) in set(lista)
    if nombre in ("plataforma", "estado", "tipo"):
        return getattr(objeto, nombre, None) == valor
    return True


class _FakeQuery:
    """Emula `db.query(Modelo).filter(...).all()/.delete()` sobre listas vivas."""

    def __init__(self, db, items):
        self._db = db
        self._items = items
        self._criterios = []

    def filter(self, *criterios, **kwargs):
        self._criterios.extend(criterios)
        return self

    def all(self):
        return [x for x in self._items if all(_cumple(x, c) for c in self._criterios)]

    def delete(self, synchronize_session=False):
        if self._db.fallo_en == "delete":
            raise RuntimeError("DELETE fallo (fake)")
        objetivos = self.all()
        for objetivo in objetivos:
            self._items.remove(objetivo)
        return len(objetivos)


class _FakeDB:
    """Sesion falsa: registra commit/rollback/close y muta sus listas."""

    def __init__(self, cuentas=None, tareas=None, explosivo=False, fallo_en=""):
        self.cuentas = list(cuentas or [])
        self.tareas = list(tareas or [])
        self.explosivo = explosivo
        self.fallo_en = fallo_en
        self.commits = 0
        self.rollbacks = 0
        self.cerrada = False

    def query(self, modelo):
        if self.explosivo:
            raise RuntimeError("BD caida (fake)")
        if modelo is Cuenta:
            return _FakeQuery(self, self.cuentas)
        if modelo is Tarea:
            return _FakeQuery(self, self.tareas)
        raise AssertionError(f"modelo inesperado en query: {modelo!r}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.cerrada = True


@contextlib.contextmanager
def _sesion_fake(db):
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextlib.contextmanager
def _con_sesion(db):
    with mock.patch.object(eliminador, "get_db_session", lambda: _sesion_fake(db)):
        yield db


@contextlib.contextmanager
def _carpetas(tmp):
    """Apunta las carpetas del modulo a subcarpetas temporales."""
    rutas = {
        "cookies": Path(tmp) / "cookies",
        "avatares": Path(tmp) / "avatares",
        "portadas": Path(tmp) / "portadas",
        "backups": Path(tmp) / "backups",
    }
    for ruta in rutas.values():
        ruta.mkdir(parents=True, exist_ok=True)
    with (
        mock.patch.object(eliminador, "CARPETA_COOKIES", str(rutas["cookies"])),
        mock.patch.object(eliminador, "CARPETA_AVATARES", str(rutas["avatares"])),
        mock.patch.object(eliminador, "CARPETA_PORTADAS", str(rutas["portadas"])),
        mock.patch.object(eliminador, "CARPETA_BACKUPS", str(rutas["backups"])),
    ):
        yield rutas


# --------------------------------------------------------------------------- #
# Datos sinteticos
# --------------------------------------------------------------------------- #
def _cuenta(usuario, id, plataforma="twitter", **extra):
    datos = types.SimpleNamespace(
        id=id,
        usuario=usuario,
        plataforma=plataforma,
        activa=True,
        status="imported",
        nombre_mostrado="",
        handle_actual="",
        avatar_path="",
        banner_path="",
        fecha_creacion=datetime(2026, 1, 2, 3, 4, 5),
        cookies_json=None,
    )
    for clave, valor in extra.items():
        setattr(datos, clave, valor)
    return datos


def _tarea(id, cuentas_ids, estado="pendiente", resultado="", tipo="post"):
    if isinstance(cuentas_ids, str):
        crudo = cuentas_ids
    else:
        crudo = json.dumps(cuentas_ids)
    return types.SimpleNamespace(
        id=id,
        tipo=tipo,
        plataforma="twitter",
        cuentas_ids=crudo,
        estado=estado,
        resultado=resultado,
    )


def _archivo(ruta: Path) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text("contenido sintetico", encoding="utf-8")
    return ruta


# --------------------------------------------------------------------------- #
# (1) parser
# --------------------------------------------------------------------------- #
def test_parsear(check):
    print("(1) parsear_lista_usuarios: vendedor, @, comentarios, duplicados")
    texto = (
        "juan\n"
        "@Maria\n"
        "PEDRO:pass_sintetica:totp_sintetico:correo@example.com:mailpass:ct0:x\n"
        "# esto es un comentario\n"
        "\n"
        "   espaciado   \n"
        "juan\n"
        "@JUAN\n"
        ":\n"
        "@\n"
    )
    lista = parsear_lista_usuarios(texto)
    check("parser: 4 usuarios", len(lista) == 4, repr(lista))
    check("parser: conserva el orden", lista[:3] == ["juan", "Maria", "PEDRO"])
    check("parser: linea del vendedor toma el primer campo", "PEDRO" in lista)
    check("parser: quita '@' inicial", "Maria" in lista and "@Maria" not in lista)
    check("parser: ignora comentarios", all("comentario" not in u for u in lista))
    check("parser: ignora vacias y solo ':':", ":" not in lista and "" not in lista)
    check("parser: quita espacios", "espaciado" in lista)
    check(
        "parser: dedupe case-insensitive conserva la primera forma",
        lista.count("juan") == 1 and len([u for u in lista if u.lower() == "juan"]) == 1,
    )
    check("parser: sin lanzar con texto raro", parsear_lista_usuarios(None) == [])
    check("parser: numero -> []", parsear_lista_usuarios(12345) == [])
    check("parser: vacio -> []", parsear_lista_usuarios("") == [])
    check("parser: CRLF/tabs", parsear_lista_usuarios("a\r\n\tb\t\r\n") == ["a", "b"])
    check(
        "parser: linea vendedora con password con ':' raro",
        parsear_lista_usuarios("solo_user:pass:mas:cosas") == ["solo_user"],
    )


# --------------------------------------------------------------------------- #
# (2) busqueda
# --------------------------------------------------------------------------- #
def test_buscar(check):
    print("(2) buscar_cuentas: case-insensitive, no encontradas y error")
    cuentas = [
        _cuenta("uno", id=1, nombre_mostrado="Uno Display", handle_actual="uno_real"),
        _cuenta("dos", id=2, status="activa"),
        _cuenta("fb_user", id=3, plataforma="facebook"),
    ]
    db = _FakeDB(cuentas=cuentas)
    with _con_sesion(db):
        resultado = buscar_cuentas(["UNO", "dos", "fantasma", "@uno", "fb_user"])

    check("buscar: sin error", resultado["error"] == "")
    encontradas = [c["usuario"] for c in resultado["encontradas"]]
    check("buscar: case-insensitive encuentra las 2 de twitter", encontradas == ["uno", "dos"])
    check("buscar: dedupe ('@uno' no repite)", len(resultado["encontradas"]) == 2)
    check("buscar: no_encontradas respeta el orden", resultado["no_encontradas"] == ["fantasma", "fb_user"])
    claves = set(resultado["encontradas"][0])
    check(
        "buscar: claves exactas de la interfaz",
        claves == {"usuario", "activa", "status", "nombre_mostrado", "handle_actual"},
        repr(claves),
    )
    check(
        "buscar: conserva nombre/handle reales",
        resultado["encontradas"][0]["nombre_mostrado"] == "Uno Display"
        and resultado["encontradas"][0]["handle_actual"] == "uno_real",
    )
    check("buscar: no muta la BD", len(db.cuentas) == 3)
    check("buscar: lista vacia -> vacio", buscar_cuentas([])["encontradas"] == [])

    db_roto = _FakeDB(explosivo=True)
    with _con_sesion(db_roto):
        fallo = buscar_cuentas(["x", "y"])
    check("buscar: error de BD no lanza", bool(fallo["error"]))
    check("buscar: error de BD -> encontradas=[]", fallo["encontradas"] == [])
    check("buscar: error de BD -> no_encontradas=usuarios", fallo["no_encontradas"] == ["x", "y"])
    check("buscar: error de BD -> retorno con las claves", set(fallo) == {"encontradas", "no_encontradas", "error"})


# --------------------------------------------------------------------------- #
# (3) eliminacion completa
# --------------------------------------------------------------------------- #
def test_eliminar_completo(check):
    print("(3) eliminar_usuarios: respaldo, archivos, tareas y commit")
    with tempfile.TemporaryDirectory() as tmp:
        with _carpetas(tmp) as rutas:
            # Archivos en las carpetas temporales.
            pkl_uno = _archivo(rutas["cookies"] / "twitter" / "uno.pkl")
            pkl_dos = _archivo(rutas["cookies"] / "twitter" / "dos.pkl")
            pkl_tres = _archivo(rutas["cookies"] / "twitter" / "tres.pkl")
            avatar_uno = _archivo(rutas["avatares"] / "avatar_uno.png")
            banner_uno = _archivo(rutas["portadas"] / "banner_uno.png")
            avatar_afuera = _archivo(Path(tmp) / "otro" / "afuera.png")

            cuentas = [
                _cuenta(
                    "uno",
                    id=1,
                    avatar_path=str(avatar_uno),
                    banner_path=str(banner_uno),
                    cookies_json=[{"name": "auth_token", "value": "sintetica"}],
                ),
                _cuenta("dos", id=2),
                _cuenta("tres", id=3, avatar_path=str(avatar_afuera)),
            ]
            tareas = [
                _tarea(1, [1]),
                _tarea(2, [1, 99]),
                _tarea(3, []),
                _tarea(4, "no-es-json"),
                _tarea(5, [2], estado="completada"),
                _tarea(6, [1, 2], resultado="previo"),
            ]
            db = _FakeDB(cuentas=cuentas, tareas=tareas)
            with _con_sesion(db):
                resultado = eliminar_usuarios(["UNO", "dos", "fantasma"])

            check("eliminar: sin error", resultado["error"] == "")
            check("eliminar: eliminadas exactas", resultado["eliminadas"] == ["uno", "dos"])
            check("eliminar: no_encontradas", resultado["no_encontradas"] == ["fantasma"])
            check("eliminar: quedan solo las vivas", [c.usuario for c in db.cuentas] == ["tres"])
            check("eliminar: 1 commit", db.commits == 1 and db.cerrada)
            check("eliminar: 4 archivos borrados", resultado["archivos_borrados"] == 4, str(resultado["archivos_borrados"]))
            check("eliminar: pkl de eliminadas borrado", not pkl_uno.exists() and not pkl_dos.exists())
            check("eliminar: pkl de NO eliminada intacta", pkl_tres.exists())
            check("eliminar: avatar dentro de data/ borrado", not avatar_uno.exists())
            check("eliminar: portada dentro de data/ borrada", not banner_uno.exists())
            check("eliminar: avatar FUERA de data/ intacto", avatar_afuera.exists())
            check("eliminar: 2 tareas canceladas", resultado["tareas_canceladas"] == 2)
            check("eliminar: tarea solo de eliminada -> cancelada", tareas[0].estado == "cancelada")
            check(
                "eliminar: motivo agregado al resultado",
                "cuentas eliminadas" in (tareas[0].resultado or ""),
            )
            check(
                "eliminar: resultado previo conservado",
                "previo" in (tareas[5].resultado or "")
                and "cuentas eliminadas" in (tareas[5].resultado or ""),
            )
            check("eliminar: tarea MIXTA intacta", tareas[1].estado == "pendiente")
            check("eliminar: tarea sin ids intacta", tareas[2].estado == "pendiente")
            check("eliminar: tarea con JSON invalido intacta", tareas[3].estado == "pendiente")
            check("eliminar: tarea completada intacta", tareas[4].estado == "completada")

            ruta_respaldo = Path(resultado["respaldo"])
            check("eliminar: respaldo creado", bool(resultado["respaldo"]) and ruta_respaldo.is_file())
            check(
                "eliminar: nombre del respaldo",
                ruta_respaldo.name.startswith("eliminacion_cuentas_")
                and ruta_respaldo.name.endswith(".json"),
            )
            check("eliminar: respaldo dentro de backups", ruta_respaldo.parent == rutas["backups"])

            data = json.loads(ruta_respaldo.read_text(encoding="utf-8"))
            check("respaldo: claves esperadas", set(data) >= {"fecha", "host", "keep", "cuentas", "tareas"})
            check("respaldo: 2 cuentas", len(data["cuentas"]) == 2)
            backup_por_usuario = {fila["usuario"]: fila for fila in data["cuentas"]}
            check("respaldo: usuarios correctos", set(backup_por_usuario) == {"uno", "dos"})
            fila_uno = backup_por_usuario["uno"]
            check("respaldo: todas las columnas de Cuenta", "status" in fila_uno and "activa" in fila_uno)
            check(
                "respaldo: fecha ISO",
                fila_uno["fecha_creacion"] == "2026-01-02T03:04:05",
                repr(fila_uno["fecha_creacion"]),
            )
            check(
                "respaldo: cookies_json tal cual",
                fila_uno["cookies_json"] == [{"name": "auth_token", "value": "sintetica"}],
            )
            ids_backup = sorted(fila["id"] for fila in data["tareas"])
            check("respaldo: solo tareas relacionadas", ids_backup == [1, 2, 5, 6], repr(ids_backup))
            check("respaldo: keep vacio", data["keep"] == [])


# --------------------------------------------------------------------------- #
# (4) opciones
# --------------------------------------------------------------------------- #
def test_opciones(check):
    print("(4) opciones: sin respaldo, sin cookies, sin tareas y ruta custom")
    with tempfile.TemporaryDirectory() as tmp:
        with _carpetas(tmp) as rutas:
            _archivo(rutas["cookies"] / "twitter" / "uno.pkl")
            db = _FakeDB(cuentas=[_cuenta("uno", id=1)])
            with _con_sesion(db):
                sin_respaldo = eliminar_usuarios(["uno"], respaldar=False)
            check("opciones: respaldar=False -> respaldo vacio", sin_respaldo["respaldo"] == "")
            check("opciones: respaldar=False -> nada escrito", list(rutas["backups"].iterdir()) == [])
            check("opciones: respaldar=False si elimina", sin_respaldo["eliminadas"] == ["uno"])

    with tempfile.TemporaryDirectory() as tmp:
        with _carpetas(tmp) as rutas:
            pkl = _archivo(rutas["cookies"] / "twitter" / "uno.pkl")
            db = _FakeDB(cuentas=[_cuenta("uno", id=1)])
            with _con_sesion(db):
                sin_cookies = eliminar_usuarios(["uno"], borrar_cookies=False)
            check("opciones: borrar_cookies=False -> 0 archivos", sin_cookies["archivos_borrados"] == 0)
            check("opciones: borrar_cookies=False -> pkl intacta", pkl.exists())
            check("opciones: borrar_cookies=False igual respalda", bool(sin_cookies["respaldo"]))

    with tempfile.TemporaryDirectory() as tmp:
        with _carpetas(tmp) as rutas:
            tarea = _tarea(1, [1])
            avatar = _archivo(rutas["avatares"] / "x.png")
            db = _FakeDB(cuentas=[_cuenta("uno", id=1, avatar_path=str(avatar))], tareas=[tarea])
            with _con_sesion(db):
                sin_tareas = eliminar_usuarios(["uno"], cancelar_tareas=False)
            check("opciones: cancelar_tareas=False -> 0", sin_tareas["tareas_canceladas"] == 0)
            check("opciones: tarea sigue pendiente", tarea.estado == "pendiente")
            check("opciones: avatar borrado igual (1)", sin_tareas["archivos_borrados"] == 1)

    with tempfile.TemporaryDirectory() as tmp:
        with _carpetas(tmp):
            destino = Path(tmp) / "custom" / "respaldo_custom.json"
            db = _FakeDB(cuentas=[_cuenta("uno", id=1)])
            with _con_sesion(db):
                custom = eliminar_usuarios(["uno"], ruta_respaldo=str(destino))
            check("opciones: ruta_respaldo custom usada", Path(custom["respaldo"]) == destino)
            check("opciones: ruta_respaldo custom creada", destino.is_file())

    db = _FakeDB(cuentas=[_cuenta("uno", id=1)])
    with _con_sesion(db):
        vacio = eliminar_usuarios([])
    check("opciones: lista vacia -> error sin lanzar", bool(vacio["error"]))
    check("opciones: lista vacia no elimina", vacio["eliminadas"] == [] and len(db.cuentas) == 1)


# --------------------------------------------------------------------------- #
# (5) errores
# --------------------------------------------------------------------------- #
def test_errores(check):
    print("(5) errores: respaldo fallido, BD caida y delete fallido")
    with tempfile.TemporaryDirectory() as tmp:
        with _carpetas(tmp) as rutas:
            pkl = _archivo(rutas["cookies"] / "twitter" / "uno.pkl")
            bloqueada = Path(tmp) / "backups_bloqueado"
            bloqueada.write_text("no soy una carpeta", encoding="utf-8")
            db = _FakeDB(cuentas=[_cuenta("uno", id=1)])
            with mock.patch.object(eliminador, "CARPETA_BACKUPS", str(bloqueada)):
                with _con_sesion(db):
                    fallo = eliminar_usuarios(["uno"])
            check("error respaldo: reportado", bool(fallo["error"]))
            check("error respaldo: NO elimina", fallo["eliminadas"] == [] and len(db.cuentas) == 1)
            check("error respaldo: pkl intacta", pkl.exists())
            check("error respaldo: rollback", db.rollbacks == 1)

        db_query = _FakeDB(cuentas=[_cuenta("uno", id=1)], explosivo=True)
        with _con_sesion(db_query):
            fallo_query = eliminar_usuarios(["uno"])
        check("error de BD en query: error reportado", bool(fallo_query["error"]))
        check("error de BD en query: no elimina", fallo_query["eliminadas"] == [])
        check("error de BD en query: rollback", db_query.rollbacks == 1)
        check(
            "error de BD en query: interfaz completa",
            set(fallo_query) == {
                "eliminadas",
                "no_encontradas",
                "archivos_borrados",
                "tareas_canceladas",
                "respaldo",
                "error",
            },
        )

        db_delete = _FakeDB(cuentas=[_cuenta("uno", id=1)], fallo_en="delete")
        with _con_sesion(db_delete):
            fallo_delete = eliminar_usuarios(["uno"], respaldar=False)
        check("error en DELETE: error reportado", bool(fallo_delete["error"]))
        check("error en DELETE: cuenta sigue viva", [c.usuario for c in db_delete.cuentas] == ["uno"])
        check("error en DELETE: rollback", db_delete.rollbacks == 1)
        check("error en DELETE: pkl no se toco (sin carpetas)", fallo_delete["archivos_borrados"] == 0)


# --------------------------------------------------------------------------- #
# (6) CLI main()
# --------------------------------------------------------------------------- #
def test_main_cli(check):
    print("(6) main(): dry-run por defecto, --apply, banderas y --json")
    llamadas_buscar = []
    llamadas_eliminar = []

    def _fake_buscar(usuarios, plataforma="twitter"):
        llamadas_buscar.append({"usuarios": list(usuarios), "plataforma": plataforma})
        encontradas = [
            {
                "usuario": u,
                "activa": True,
                "status": "imported",
                "nombre_mostrado": "",
                "handle_actual": "",
            }
            for u in usuarios
            if u.lower() != "fantasma"
        ]
        no_encontradas = [u for u in usuarios if u.lower() == "fantasma"]
        return {"encontradas": encontradas, "no_encontradas": no_encontradas, "error": ""}

    def _fake_eliminar(usuarios, **kwargs):
        llamadas_eliminar.append({"usuarios": list(usuarios), "kwargs": dict(kwargs)})
        return {
            "eliminadas": list(usuarios),
            "no_encontradas": [],
            "archivos_borrados": 3,
            "tareas_canceladas": 1,
            "respaldo": "data/backups/eliminacion_cuentas_fake.json",
            "error": "",
        }

    with tempfile.TemporaryDirectory() as tmp:
        lista = Path(tmp) / "lista.txt"
        lista.write_text(
            " usuario_uno \n"
            "@usuario_dos\n"
            "usuario_tres:password_sintetica:totp:x:y:z:w\n"
            "# comentario\n"
            "fantasma\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(eliminador, "buscar_cuentas", _fake_buscar),
            mock.patch.object(eliminador, "eliminar_usuarios", _fake_eliminar),
        ):
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                codigo = eliminar_cuentas.main([str(lista)])
            texto = salida.getvalue()
            check("main dry-run: devuelve 0", codigo == 0)
            check("main dry-run: usa buscar_cuentas", len(llamadas_buscar) == 1)
            check(
                "main dry-run: parsea la lista (vendedor y @)",
                llamadas_buscar[-1]["usuarios"] == ["usuario_uno", "usuario_dos", "usuario_tres", "fantasma"],
                repr(llamadas_buscar[-1]["usuarios"]),
            )
            check("main dry-run: NO llama a eliminar_usuarios", llamadas_eliminar == [])
            check(
                "main dry-run: avisa simulacion y --apply",
                "SIMULACION" in texto and "--apply" in texto,
            )
            check("main dry-run: no imprime credenciales", "password_sintetica" not in texto)

            salida_apply = io.StringIO()
            with contextlib.redirect_stdout(salida_apply):
                codigo_apply = eliminar_cuentas.main([str(lista), "--apply"])
            check("main --apply: devuelve 0", codigo_apply == 0)
            check("main --apply: llama a eliminar_usuarios", len(llamadas_eliminar) == 1)
            check(
                "main --apply: defaults respaldo/tareas/cookies en True",
                llamadas_eliminar[-1]["kwargs"]
                == {"borrar_cookies": True, "cancelar_tareas": True, "respaldar": True},
                repr(llamadas_eliminar[-1]["kwargs"]),
            )
            check("main --apply: imprime eliminadas y respaldo", "Respaldo" in salida_apply.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                eliminar_cuentas.main(
                    [str(lista), "--apply", "--sin-cookies", "--sin-tareas", "--sin-respaldo"]
                )
            check(
                "main --apply: banderas --sin-* pasan False",
                llamadas_eliminar[-1]["kwargs"]
                == {"borrar_cookies": False, "cancelar_tareas": False, "respaldar": False},
                repr(llamadas_eliminar[-1]["kwargs"]),
            )

            with contextlib.redirect_stdout(io.StringIO()):
                eliminar_cuentas.main(["--usuarios", "pepe, PEPE , luis", "--apply"])
            check(
                "main --usuarios: parsea y deduplica comas",
                llamadas_eliminar[-1]["usuarios"] == ["pepe", "luis"],
                repr(llamadas_eliminar[-1]["usuarios"]),
            )

            salida_json = io.StringIO()
            with contextlib.redirect_stdout(salida_json):
                codigo_json = eliminar_cuentas.main([str(lista), "--json"])
            try:
                datos_json = json.loads(salida_json.getvalue())
            except Exception:
                datos_json = None
            check("main --json: devuelve 0", codigo_json == 0)
            check(
                "main --json: JSON parseable con lo encontrado",
                isinstance(datos_json, dict)
                and datos_json.get("dry_run") is True
                and len(datos_json.get("encontradas") or []) == 3,
            )
            check("main --json: no incluye credenciales", "password_sintetica" not in (salida_json.getvalue() or ""))

            salida_json_apply = io.StringIO()
            with contextlib.redirect_stdout(salida_json_apply):
                eliminar_cuentas.main([str(lista), "--apply", "--json"])
            try:
                datos_apply = json.loads(salida_json_apply.getvalue())
            except Exception:
                datos_apply = None
            check(
                "main --json --apply: resumen real en JSON",
                isinstance(datos_apply, dict)
                and datos_apply.get("dry_run") is False
                and datos_apply.get("ok") is True
                and datos_apply.get("archivos_borrados") == 3,
            )

            salida_error = io.StringIO()
            with contextlib.redirect_stdout(salida_error):
                codigo_error = eliminar_cuentas.main([])
            check("main sin lista: devuelve 1", codigo_error == 1)
            check("main sin lista: mensaje claro", "ERROR" in salida_error.getvalue())

            salida_inexistente = io.StringIO()
            with contextlib.redirect_stdout(salida_inexistente):
                codigo_inexistente = eliminar_cuentas.main([str(Path(tmp) / "no_existe.txt")])
            check("main archivo inexistente: devuelve 1", codigo_inexistente == 1)


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_parsear(check)
    test_buscar(check)
    test_eliminar_completo(check)
    test_opciones(check)
    test_errores(check)
    test_main_cli(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_eliminador.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

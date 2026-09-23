# -*- coding: utf-8 -*-
"""Tests rapidos de `cuentas/generador_identidades.py` (sin red, sin Chrome,
sin BD real).

Cubre el contrato congelado del cambio masivo de nombre/@:

  (a) `_normalizar_tipo`: "partido" (y sinonimos: similitud, guino, espectro,
      color...) y "mixto" (mezcla/mitad), ademas de la regresion de
      persona/movimiento.
  (b) Tipo "partido" 100% local (monkeypatch de `_openai_disponible`→False):
      nombres 2-4 palabras <= 50, handles `HANDLE_RE` 4-15 unicos, sin tokens
      de partidos ni "movimientociudadan"; presets del dueno ("Movimiento
      Naranja", "Los Bolillos", "Amarillo de Luz") validos y "Pan de Luz"
      rechazado por `_nombre_prohibido`.
  (c) `asignar_propuestas(tipo="mixto", dry_run=True)` con 24 cuentas fake:
      recuentos persona/partido entre 30% y 70%, handles/nombres unicos,
      `origen_ia` presente, nada escrito en la BD; y el camino de escritura
      real (dry_run=False) con una sesion fake.
  (d) `asignar_propuestas` con IA simulada (monkeypatch de `_pedir_openai_lote`):
      usa los items validos, respeta handles ocupados/duplicados y llama a la
      IA en lotes <= 30.
  (e) `aplicar_propuestas_en_lote` con `aplicar_propuesta` fake: exito/fallo
      mixto, callback con `hechas` creciente desde el hilo recolector, cap de
      `max_workers` en 4, cancelacion con `threading.Event` (no lanza nuevas),
      renombrado opcional (`renombrado`/`error_renombrado`) y blindaje ante
      excepciones.
  (f) `ia_disponible()`: False con keys placeholder y True con una key real.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_identidades.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import random
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from cuentas import generador_identidades as gi  # noqa: E402


# --------------------------------------------------------------------------- #
# Fakes de BD (la suite no toca la base real)
# --------------------------------------------------------------------------- #
class _FakeQuery:
    """Query minima: los filtros se ignoran y `.all()` devuelve las filas."""

    def __init__(self, filas):
        self._filas = list(filas)

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._filas)

    def first(self):
        return self._filas[0] if self._filas else None


class _FakeDB:
    """Sesion minima con `query(modelo).filter(...).all()`."""

    def __init__(self, cuentas=()):
        self.cuentas = list(cuentas)
        self.consultas = 0

    def query(self, entidad, *resto):
        self.consultas += 1
        return _FakeQuery(self.cuentas)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeColumna:
    """Columna ORM falsa: solo necesita soportar `in_`/comparaciones."""

    def in_(self, valores):
        return ("in", list(valores))

    def __eq__(self, otro):
        return ("eq", otro)

    def __ne__(self, otro):
        return ("ne", otro)

    def __hash__(self):
        return id(self)


class _FakeCuentaModelo:
    """Modelo Cuenta falso (patchea `core.models.Cuenta`)."""

    usuario = _FakeColumna()
    handle_actual = _FakeColumna()
    handle_propuesto = _FakeColumna()
    nombre_mostrado = _FakeColumna()
    nombre_propuesto = _FakeColumna()
    plataforma = _FakeColumna()


@contextlib.contextmanager
def _sesion_falsa(db):
    yield db


@contextlib.contextmanager
def _bd_falsa(db):
    """Parchea la BD tal como la importa `asignar_propuestas` en tiempo de uso."""
    with mock.patch(
        "core.database.get_db_session", lambda: _sesion_falsa(db)
    ), mock.patch("core.models.Cuenta", _FakeCuentaModelo):
        yield


def _cuenta(usuario, **cambios):
    """Fila fake de Cuenta con los campos que usa asignar_propuestas."""
    datos = dict(
        usuario=usuario,
        handle_actual=f"{usuario}_x",
        handle_propuesto="",
        nombre_mostrado="",
        nombre_propuesto="",
        tipo_cuenta="",
        seccion="",
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _tokens_de(texto):
    """Tokens normalizados (minusculas, sin acentos) de un nombre."""
    return {t for t in re.split(r"[^a-z0-9]+", gi._normalizar_texto(texto)) if t}


# --------------------------------------------------------------------------- #
# (a) _normalizar_tipo
# --------------------------------------------------------------------------- #
def test_normalizar_tipo(check):
    print("(a) _normalizar_tipo: partido, mixto y regresion persona/movimiento")
    for variante in ("partido", "partidos", "similitud", "similitudes"):
        check(
            f"tipo: {variante!r} -> partido",
            gi._normalizar_tipo(variante) == "partido",
            repr(gi._normalizar_tipo(variante)),
        )
    for variante in ("guiño", "guino", "espectro", "color", "colores", "Color"):
        check(
            f"tipo: {variante!r} -> partido",
            gi._normalizar_tipo(variante) == "partido",
            repr(gi._normalizar_tipo(variante)),
        )
    for variante in ("mixto", "mixta", "mixtos", "mezcla", "mezclas", "mitad"):
        check(
            f"tipo: {variante!r} -> mixto",
            gi._normalizar_tipo(variante) == "mixto",
            repr(gi._normalizar_tipo(variante)),
        )
    check(
        "tipo: regresion movimiento/persona/politica",
        gi._normalizar_tipo("movimiento") == "movimiento"
        and gi._normalizar_tipo("persona") == "persona"
        and gi._normalizar_tipo("politica") == "movimiento"
        and gi._normalizar_tipo("") == "persona",
        f"{gi._normalizar_tipo('movimiento')}/{gi._normalizar_tipo('persona')}/"
        f"{gi._normalizar_tipo('politica')}/{gi._normalizar_tipo('')}",
    )


# --------------------------------------------------------------------------- #
# (b) Tipo "partido" local
# --------------------------------------------------------------------------- #
def test_partido_local(check):
    print("(b) partido local: nombres/handles validos, unicidad y prohibiciones")

    # Lista negra vigente: "bolillos" pasa; "Pan de Luz" NO.
    check(
        "partido: 'Los Bolillos' permitido",
        gi._nombre_prohibido("Los Bolillos") is False,
        repr(gi._nombre_prohibido("Los Bolillos")),
    )
    check(
        "partido: 'Movimiento Naranja' permitido",
        gi._nombre_prohibido("Movimiento Naranja") is False,
        repr(gi._nombre_prohibido("Movimiento Naranja")),
    )
    check(
        "partido: 'Amarillo de Luz' valido y sin prohibiciones",
        gi._validar_nombre("Amarillo de Luz") == "Amarillo de Luz"
        and gi._nombre_prohibido("Amarillo de Luz") is False,
        repr(gi._validar_nombre("Amarillo de Luz")),
    )
    check(
        "partido: 'Pan de Luz' rechazado (token 'pan')",
        gi._nombre_prohibido("Pan de Luz") is True,
        repr(gi._nombre_prohibido("Pan de Luz")),
    )
    handle_pan = gi._handle_desde_nombre("Pan de Luz", set(), random)
    check(
        "partido: el handle derivado de 'Pan de Luz' no lleva 'pan'",
        "pan" not in _tokens_de(handle_pan.replace("_", " "))
        and gi.HANDLE_RE.match(handle_pan) is not None,
        repr(handle_pan),
    )

    # Presets exactos pedidos por el dueno.
    for preset in (
        "Movimiento Naranja",
        "Fuerza Naranja",
        "Marea Naranja",
        "Corriente Naranja",
        "Amarillo de Luz",
        "Los Bolillos",
        "Bolillos de la Colonia",
        "Corazón Naranja",
        "Sol Naranja",
        "Faro Azul",
        "Bandera Guinda",
        "Rosa en Movimiento",
        "Aurora Naranja",
        "Barrio Naranja",
        "Naranja con Causa",
    ):
        check(
            f"partido: preset {preset!r} existe y es valido",
            preset in gi._PRESETS_PARTIDO
            and gi._validar_nombre(preset) == preset,
            repr(gi._validar_nombre(preset)),
        )

    random.seed(2026)
    with mock.patch.object(gi, "_openai_disponible", lambda: False):
        una = gi.generar_identidad("partido")
        identidades = gi.generar_identidades(40, "partido")

    check(
        "partido: generar_identidad devuelve tipo=partido",
        una.get("tipo") == "partido"
        and gi.HANDLE_RE.match(una.get("handle", "")) is not None
        and 2 <= len(str(una.get("nombre", "")).split()) <= 4,
        repr(una),
    )
    check(
        "partido: generar_identidades(40) devuelve las 40",
        len(identidades) == 40,
        f"n={len(identidades)}",
    )
    nombres = [i["nombre"] for i in identidades]
    handles = [i["handle"] for i in identidades]
    check(
        "partido: nombres unicos (40)",
        len({n.lower() for n in nombres}) == 40,
        f"unicos={len({n.lower() for n in nombres})}",
    )
    check(
        "partido: handles unicos (40) con HANDLE_RE 4-15",
        len({h.lower() for h in handles}) == 40
        and all(gi.HANDLE_RE.match(h) for h in handles),
        repr(handles[:5]),
    )
    check(
        "partido: 2-4 palabras y <= 50 caracteres",
        all(2 <= len(n.split()) <= 4 and len(n) <= gi._LARGO_MAX_NOMBRE for n in nombres),
        repr([n for n in nombres if not (2 <= len(n.split()) <= 4 and len(n) <= 50)][:3]),
    )
    check(
        "partido: ningun nombre prohibido ni 'movimientociudadan'",
        all(not gi._nombre_prohibido(n) for n in nombres)
        and all("movimientociudadan" not in n.lower().replace(" ", "") for n in nombres),
        repr([n for n in nombres if gi._nombre_prohibido(n)][:3]),
    )
    check(
        "partido: ningun handle tocado por _handle_prohibido",
        all(not gi._handle_prohibido(h) for h in handles),
        repr([h for h in handles if gi._handle_prohibido(h)][:3]),
    )
    tokens = set()
    for n in nombres:
        tokens |= _tokens_de(n)
    check(
        "partido: sin siglas/nombres literales de partido",
        not (tokens & gi._TOKENS_PARTIDO),
        repr(sorted(tokens & gi._TOKENS_PARTIDO)),
    )
    colores = {c.lower() for c in gi._COLORES_PARTIDO}
    simbolos = {s.lower() for s in gi._SIMBOLOS_PARTIDO}
    check(
        "partido: al menos un preset o estructura esperada (color/simbolo)",
        any(n in gi._PRESETS_PARTIDO for n in nombres)
        or any(
            any(c in n.lower() for c in colores) and any(s in n.lower() for s in simbolos)
            for n in nombres
        ),
        repr(nombres[:8]),
    )


# --------------------------------------------------------------------------- #
# (c) asignar_propuestas: mixto (dry_run y escritura)
# --------------------------------------------------------------------------- #
def test_asignar_mixto(check):
    print("(c) asignar_propuestas mixto: ~50/50, unicos, dry_run y escritura")
    usuarios = [f"u{i}" for i in range(24)]
    cuentas = [_cuenta(u) for u in usuarios]
    db = _FakeDB(cuentas)
    random.seed(7)

    with _bd_falsa(db), mock.patch.object(gi, "_openai_disponible", lambda: False):
        res = gi.asignar_propuestas(usuarios, tipo="mixto", dry_run=True)

    check(
        "mixto: total/ok completos",
        res.get("total") == 24 and res.get("ok") == 24,
        f"total={res.get('total')} ok={res.get('ok')} errores={res.get('errores')}",
    )
    check(
        "mixto: origen_ia presente y False sin OpenAI",
        "origen_ia" in res and res.get("origen_ia") is False,
        repr(res.get("origen_ia")),
    )
    n_persona = int(res.get("persona", 0))
    n_partido = int(res.get("partido", 0))
    check(
        "mixto: recuento por tipo (persona+partido == ok, movimiento=0)",
        n_persona + n_partido == 24 and int(res.get("movimiento", 0)) == 0,
        f"persona={n_persona} partido={n_partido} movimiento={res.get('movimiento')}",
    )
    check(
        "mixto: ambos tipos entre 30% y 70%",
        0.3 * 24 <= n_persona <= 0.7 * 24 and 0.3 * 24 <= n_partido <= 0.7 * 24,
        f"persona={n_persona} partido={n_partido}",
    )
    propuestas = res.get("propuestas") or []
    tipos = [p.get("tipo") for p in propuestas]
    check(
        "mixto: cada propuesta es persona o partido y hay de los dos",
        all(t in ("persona", "partido") for t in tipos)
        and "persona" in tipos
        and "partido" in tipos,
        repr(sorted(set(tipos))),
    )
    claves_handles = [gi._clave_handle(p.get("handle")) for p in propuestas]
    nombres = [gi._normalizar_texto(p.get("nombre")) for p in propuestas]
    check(
        "mixto: handles unicos y validos",
        len(set(claves_handles)) == 24
        and all(gi.HANDLE_RE.match(str(p.get("handle") or "")) for p in propuestas),
        repr([h for h in claves_handles if not gi.HANDLE_RE.match(h)][:3]),
    )
    check(
        "mixto: nombres unicos",
        len(set(nombres)) == 24,
        f"unicos={len(set(nombres))}",
    )
    check(
        "mixto: ningun handle choca con los ocupados de la BD",
        not (set(claves_handles) & {gi._clave_handle(c.usuario) for c in cuentas})
        and not (set(claves_handles) & {gi._clave_handle(c.handle_actual) for c in cuentas}),
        "",
    )
    check(
        "mixto: dry_run NO escribe en la BD",
        all(not c.nombre_propuesto and not c.handle_propuesto for c in cuentas),
        repr([(c.usuario, c.nombre_propuesto) for c in cuentas if c.nombre_propuesto][:2]),
    )

    # Camino de escritura real (dry_run=False): guarda las propuestas.
    cuentas2 = [_cuenta(f"w{i}") for i in range(3)]
    db2 = _FakeDB(cuentas2)
    with _bd_falsa(db2), mock.patch.object(gi, "_openai_disponible", lambda: False):
        res2 = gi.asignar_propuestas(
            [c.usuario for c in cuentas2], tipo="partido", dry_run=False
        )
    check(
        "mixto: dry_run=False guarda nombre/handle propuestos",
        res2.get("ok") == 3
        and all(c.nombre_propuesto and c.handle_propuesto for c in cuentas2),
        repr([(c.usuario, c.nombre_propuesto, c.handle_propuesto) for c in cuentas2]),
    )
    check(
        "mixto: tipo explicito 'partido' se respeta en la propuesta",
        res2.get("partido") == 3 and res2.get("persona") == 0,
        f"partido={res2.get('partido')} persona={res2.get('persona')}",
    )


# --------------------------------------------------------------------------- #
# (d) asignar_propuestas con IA simulada
# --------------------------------------------------------------------------- #
def test_asignar_ia(check):
    print("(d) asignar_propuestas con IA simulada: items validos y lotes <= 30")

    llamadas = []

    def _lote_valido(cantidad, tipo, seccion, contexto, evitar, rng=random):
        llamadas.append((int(cantidad), tipo, seccion, contexto, set(evitar)))
        base = len(llamadas) * 100
        return [
            {
                "nombre": f"Ia Numero {base + i} Prueba",
                "handle": f"ia{base}_{i}",
            }
            for i in range(int(cantidad))
        ]

    usuarios = [f"ia{i}" for i in range(4)]
    cuentas = [_cuenta(u) for u in usuarios]
    with _bd_falsa(_FakeDB(cuentas)), mock.patch.object(
        gi, "_openai_disponible", lambda: True
    ), mock.patch.object(gi, "_pedir_openai_lote", _lote_valido):
        res = gi.asignar_propuestas(usuarios, tipo="partido", dry_run=True)

    check(
        "ia: usa los items validos de OpenAI (origen_ia=True)",
        res.get("ok") == 4 and res.get("origen_ia") is True,
        f"ok={res.get('ok')} origen_ia={res.get('origen_ia')} errores={res.get('errores')}",
    )
    nombres = [p.get("nombre") for p in res.get("propuestas") or []]
    check(
        "ia: los nombres vienen del lote simulado",
        all(str(n).startswith("Ia Numero") for n in nombres),
        repr(nombres),
    )
    check(
        "ia: respeta HANDLE_RE y unicidad",
        all(gi.HANDLE_RE.match(str(p.get("handle") or "")) for p in res.get("propuestas") or [])
        and len({gi._clave_handle(p.get("handle")) for p in res.get("propuestas") or []}) == 4,
        repr([p.get("handle") for p in res.get("propuestas") or []]),
    )
    check(
        "ia: los handles ocupados viajan al prompt (evitar)",
        llamadas
        and all(gi._clave_handle(c.usuario) in llamadas[0][4] for c in cuentas),
        repr(llamadas[0][4] if llamadas else None),
    )

    # Items invalidos (nombre de 1 palabra) y handles duplicados/ocupados.
    def _lote_mixto(cantidad, tipo, seccion, contexto, evitar, rng=random):
        return [
            {"nombre": "Ia Valido Uno", "handle": "ia_valido_1"},
            {"nombre": "X", "handle": "ia_invalido"},
            {"nombre": "Ia Valido Dos", "handle": "ia_valido_1"},
            {"nombre": "Ia Valido Tres", "handle": "ia_valido_3"},
        ][: int(cantidad)]

    usuarios2 = [f"v{i}" for i in range(3)]
    cuentas2 = [
        _cuenta("v0", handle_actual="ia_valido_1"),  # handle ya ocupado en la BD
        _cuenta("v1"),
        _cuenta("v2"),
    ]
    with _bd_falsa(_FakeDB(cuentas2)), mock.patch.object(
        gi, "_openai_disponible", lambda: True
    ), mock.patch.object(gi, "_pedir_openai_lote", _lote_mixto):
        res2 = gi.asignar_propuestas(usuarios2, tipo="partido", dry_run=True)

    propuestas2 = res2.get("propuestas") or []
    claves2 = [gi._clave_handle(p.get("handle")) for p in propuestas2]
    check(
        "ia: completa el lote aunque haya items invalidos",
        res2.get("ok") == 3,
        f"ok={res2.get('ok')} errores={res2.get('errores')}",
    )
    check(
        "ia: no usa el nombre invalido y evita el handle ocupado",
        all(p.get("nombre") != "X" for p in propuestas2)
        and "ia_valido_1" not in claves2
        and len(set(claves2)) == 3,
        repr(propuestas2),
    )

    # Lotes <= 30: con 35 cuentas el generador debe pedir 30 y luego 5.
    llamadas.clear()
    usuarios3 = [f"g{i}" for i in range(35)]
    with _bd_falsa(_FakeDB([_cuenta(u) for u in usuarios3])), mock.patch.object(
        gi, "_openai_disponible", lambda: True
    ), mock.patch.object(gi, "_pedir_openai_lote", _lote_valido):
        res3 = gi.asignar_propuestas(usuarios3, tipo="partido", dry_run=True)
    tamanos = [c[0] for c in llamadas]
    check(
        "ia: con 35 cuentas el primer lote es de 30 y todos los lotes <= 30",
        tamanos
        and tamanos[0] == 30
        and all(tamano <= 30 for tamano in tamanos)
        and res3.get("ok") == 35,
        f"tamanos={tamanos} ok={res3.get('ok')}",
    )
    check(
        "ia: ningun lote supera 30 items",
        all(tamano <= 30 for tamano in tamanos) and len(tamanos) >= 1,
        repr(tamanos),
    )


# --------------------------------------------------------------------------- #
# (d2) es_handle_generico + proteccion anti-sobrescritura
# --------------------------------------------------------------------------- #
def test_handle_generico(check):
    print("(d2) es_handle_generico: basura de proveedor vs identidad humana")
    for handle in (
        "Katiaforbx7m",
        "GoodWinsnvn",
        "khawajaGjdgi",
        "Sajtiagosal21",
        "qwrtyps12x",
    ):
        check(
            f"handle {handle!r} -> generico",
            gi.es_handle_generico(handle) is True,
            repr(gi.es_handle_generico(handle)),
        )
    for handle in (
        "UnidosMovCDMX",
        "Barrio_naranja",
        "VozLibertad",
        "JusticiaYa",
        "AnalisisIP",
        "ConsultoriaDatos",
        "MariaLopez",
        "lupita_hdz",
        "4T_puntodos",
        "NeraFarner",
        "Unidos en Movimiento",
        "Katia",
        "",
        None,
    ):
        check(
            f"handle {handle!r} -> humano/legitimo (no generico)",
            gi.es_handle_generico(handle) is False,
            repr(gi.es_handle_generico(handle)),
        )
    check(
        "handle: entrada rara (objeto) no lanza y devuelve False",
        gi.es_handle_generico(object()) is False,
        repr(gi.es_handle_generico(object())),
    )

    check(
        "_cuenta_protegida: handle_actual humano protege",
        gi._cuenta_protegida(
            _cuenta("u1", handle_actual="UnidosMovCDMX")
        )[0]
        is True,
    )
    check(
        "_cuenta_protegida: nombre_mostrado humano protege aunque el handle sea basura",
        gi._cuenta_protegida(
            _cuenta(
                "u2",
                handle_actual="Katiaforbx7m",
                nombre_mostrado="Lupita Hernández",
            )
        )
        == (True, "nombre_mostrado", "Lupita Hernández"),
    )
    check(
        "_cuenta_protegida: propuesta previa tambien protege",
        gi._cuenta_protegida(
            _cuenta("u3", handle_actual="GoodWinsnvn", handle_propuesto="VozLibertad")
        )
        == (True, "propuesta", "VozLibertad"),
    )
    check(
        "_cuenta_protegida: handle basura sin nombre no protege",
        gi._cuenta_protegida(
            _cuenta("u4", handle_actual="GoodWinsnvn", nombre_mostrado="GoodWinsnvn")
        )
        == (False, "", ""),
    )
    check(
        "_cuenta_protegida: cuenta sin datos no protege",
        gi._cuenta_protegida(_cuenta("u5", handle_actual="")) == (False, "", ""),
    )


def test_proteccion_asignar(check):
    print("(d3) asignar_propuestas: protege brandeadas y omite su generacion")

    cuentas = [
        _cuenta(
            "humana_handle",
            handle_actual="UnidosMovCDMX",
            nombre_mostrado="Unidos en Movimiento",
            tipo_cuenta="politica",
            seccion="IP",
        ),
        _cuenta(
            "humana_nombre",
            handle_actual="Katiaforbx7m",
            nombre_mostrado="Lupita Hernández",
            tipo_cuenta="activista",
            seccion="LIB",
        ),
        _cuenta(
            "con_propuesta",
            handle_actual="",
            nombre_mostrado="",
            handle_propuesto="VozLibertad",
        ),
        _cuenta(
            "generica_ip",
            handle_actual="GoodWinsnvn",
            nombre_mostrado="GoodWinsnvn",
            tipo_cuenta="politica",
            seccion="IP",
        ),
        _cuenta(
            "generica_lib",
            handle_actual="khawajaGjdgi",
            tipo_cuenta="activista",
            seccion="LIB",
        ),
        _cuenta(
            "generica_ciud",
            handle_actual="",
            tipo_cuenta="ciudadana",
            seccion="JUS",
        ),
    ]
    usuarios = [c.usuario for c in cuentas]
    with _bd_falsa(_FakeDB(cuentas)), mock.patch.object(
        gi, "_openai_disponible", lambda: False
    ):
        res = gi.asignar_propuestas(usuarios, dry_run=True, proteger_brandeadas=True)
        res_sin = gi.asignar_propuestas(usuarios, dry_run=True)

    check(
        "proteccion: total es el de la lista recibida",
        res.get("total") == 6,
        f"total={res.get('total')}",
    )
    check(
        "proteccion: 3 omitidas y 3 propuestas",
        res.get("omitidas_protegidas") == 3 and res.get("ok") == 3,
        f"omitidas={res.get('omitidas_protegidas')} ok={res.get('ok')} "
        f"errores={res.get('errores')}",
    )
    check(
        "proteccion: protegidas_usuarios exacto",
        set(res.get("protegidas_usuarios") or [])
        == {"humana_handle", "humana_nombre", "con_propuesta"},
        repr(res.get("protegidas_usuarios")),
    )
    detalle = {
        (d.get("usuario"), d.get("campo")): d.get("valor")
        for d in res.get("protegidas_detalle") or []
        if isinstance(d, dict)
    }
    check(
        "proteccion: detalle con campo/valor por cuenta",
        detalle.get(("humana_handle", "handle_actual")) == "UnidosMovCDMX"
        and detalle.get(("humana_nombre", "nombre_mostrado")) == "Lupita Hernández"
        and detalle.get(("con_propuesta", "propuesta")) == "VozLibertad",
        repr(res.get("protegidas_detalle")),
    )
    check(
        "proteccion: las genericas SI reciben propuesta",
        {p.get("usuario") for p in res.get("propuestas") or []}
        == {"generica_ip", "generica_lib", "generica_ciud"},
        repr([p.get("usuario") for p in res.get("propuestas") or []]),
    )
    check(
        "proteccion: sin el flag todas se procesan (retrocompatible)",
        res_sin.get("ok") == 6
        and res_sin.get("omitidas_protegidas") == 0
        and not res_sin.get("protegidas_usuarios"),
        f"ok={res_sin.get('ok')} omitidas={res_sin.get('omitidas_protegidas')}",
    )


# --------------------------------------------------------------------------- #
# (d4) cruce registro + seccion (grupos y prompt)
# --------------------------------------------------------------------------- #
def test_cruce_registro_seccion(check):
    print("(d4) cruce registro+seccion: grupos por (tipo, seccion, registro) y prompt")

    capturas = []

    def _lote_captura(cantidad, tipo, seccion, contexto, evitar, rng=random, registro=""):
        capturas.append((tipo, seccion, registro))
        return []

    cuentas = [
        _cuenta(
            "politica_ip",
            handle_actual="GoodWinsnvn",
            tipo_cuenta="politica",
            seccion="IP",
        ),
        _cuenta(
            "activista_lib",
            handle_actual="khawajaGjdgi",
            tipo_cuenta="activista",
            seccion="LIB",
        ),
        _cuenta(
            "ciudadana_jus",
            handle_actual="",
            tipo_cuenta="ciudadana",
            seccion="JUS",
        ),
    ]
    with _bd_falsa(_FakeDB(cuentas)), mock.patch.object(
        gi, "_openai_disponible", lambda: True
    ), mock.patch.object(gi, "_pedir_openai_lote", _lote_captura):
        res = gi.asignar_propuestas([c.usuario for c in cuentas], dry_run=True)

    check(
        "cruce: politica+IP -> (movimiento, IP, politica)",
        ("movimiento", "IP", "politica") in capturas,
        repr(capturas),
    )
    check(
        "cruce: activista+LIB -> (persona, LIB, activista)",
        ("persona", "LIB", "activista") in capturas,
        repr(capturas),
    )
    check(
        "cruce: ciudadana IGNORA su seccion -> (persona, '', ciudadana)",
        ("persona", "", "ciudadana") in capturas,
        repr(capturas),
    )
    check(
        "cruce: las 3 cuentas reciben propuesta",
        res.get("ok") == 3,
        f"ok={res.get('ok')} errores={res.get('errores')}",
    )

    p_ip = gi._construir_prompt(5, "persona", "IP", "", set(), registro="politica")
    p_lib = gi._construir_prompt(5, "persona", "LIB", "", set(), registro="activista")
    p_ciud = gi._construir_prompt(5, "persona", "JUS", "", set(), registro="ciudadana")
    check(
        "prompt IP+politica: AnalisisIP/ConsultoriaDatos/MANDAN",
        all(k in p_ip for k in ("AnalisisIP", "ConsultoriaDatos", "MANDAN")),
        repr([k for k in ("AnalisisIP", "ConsultoriaDatos", "MANDAN") if k not in p_ip]),
    )
    check(
        "prompt LIB+activista: VozLibertad/JusticiaYa",
        all(k in p_lib for k in ("VozLibertad", "JusticiaYa")),
        repr([k for k in ("VozLibertad", "JusticiaYa") if k not in p_lib]),
    )
    check(
        "prompt ciudadana: no menciona la seccion de la cuenta",
        "Contexto de seccion" not in p_ciud and "JUS" not in p_ciud.replace(
            "LIB/JUS", ""
        ),
        f"seccion_en_prompt={'Contexto de seccion' in p_ciud}",
    )
    check(
        "prompt ciudadana: regla explicita de ignorar seccion",
        "ciudadana" in p_ciud.lower() and "IGNORA" in p_ciud,
        repr(p_ciud[-200:]),
    )


# --------------------------------------------------------------------------- #
# (e) aplicar_propuestas_en_lote
# --------------------------------------------------------------------------- #
def test_aplicar_lote(check):
    print("(e) aplicar_propuestas_en_lote: paralelo, callback, cancelar y renombrar")

    # ---- Exito/fallo mixto + callback + cap de workers ----
    candado = threading.Lock()
    activos = {"ahora": 0, "max": 0}
    llamadas = []

    def _fake_aplicar(usuario, password=""):
        with candado:
            llamadas.append((usuario, password))
            activos["ahora"] += 1
            activos["max"] = max(activos["max"], activos["ahora"])
        time.sleep(0.03)
        with candado:
            activos["ahora"] -= 1
        if usuario.startswith("ko"):
            return {"ok": False, "nombre": False, "handle": False, "error": "fallo simulado"}
        if usuario == "explota":
            raise RuntimeError("boom del worker")
        return {"ok": True, "nombre": True, "handle": usuario.startswith("ren")}

    progreso = []
    usuarios = ["ok1", "ok2", "ok3", "ko1", "ko2", "ok4", "ok5", "explota"]
    with mock.patch.object(gi, "aplicar_propuesta", _fake_aplicar):
        res = gi.aplicar_propuestas_en_lote(
            usuarios + ["ok1", "", None],  # duplicados/vacios se descartan
            max_workers=10,  # se acota a 4
            password="clave",
            callback=lambda p: progreso.append(dict(p)),
        )

    check(
        "lote: deduplica usuarios y descarta vacios",
        res.get("total") == 8,
        f"total={res.get('total')}",
    )
    check(
        "lote: recuento ok/fallidos correcto",
        res.get("ok") == 5 and res.get("fallidos") == 3,
        f"ok={res.get('ok')} fallidos={res.get('fallidos')} errores={res.get('errores')}",
    )
    check(
        "lote: exito y excepcion del worker quedan como fallidos con error",
        any("fallo simulado" in e for e in res.get("errores") or [])
        and any("boom del worker" in e for e in res.get("errores") or []),
        repr(res.get("errores")),
    )
    claves_resultado = {"usuario", "ok", "nombre", "handle", "error", "renombrado", "error_renombrado"}
    check(
        "lote: cada resultado trae el contrato completo",
        len(res.get("resultados") or []) == 8
        and all(claves_resultado <= set(entrada) for entrada in res["resultados"]),
        repr(sorted((res.get("resultados") or [{}])[0])),
    )
    check(
        "lote: el password se pasa tal cual a aplicar_propuesta",
        llamadas and all(clave == "clave" for _u, clave in llamadas),
        repr(llamadas[:3]),
    )
    check(
        "lote: callback por cada cuenta terminada, hechas creciente y total correcto",
        [p.get("hechas") for p in progreso] == list(range(1, 9))
        and all(p.get("total") == 8 for p in progreso)
        and all({"total", "hechas", "usuario", "ok", "error"} <= set(p) for p in progreso),
        repr([p.get("hechas") for p in progreso]),
    )
    check(
        "lote: max_workers se acota a 4 en paralelo",
        activos["max"] <= 4,
        f"max_concurrentes={activos['max']}",
    )
    check(
        "lote: sin cancelacion -> cancelado=False",
        res.get("cancelado") is False,
        repr(res.get("cancelado")),
    )

    # ---- Un callback que revienta no escapa ----
    def _callback_roto(_progreso):
        raise RuntimeError("callback roto")

    with mock.patch.object(
        gi, "aplicar_propuesta", lambda usuario, password="": {"ok": True, "nombre": True, "handle": False}
    ):
        res_ok = gi.aplicar_propuestas_en_lote(["a", "b"], callback=_callback_roto)
    check(
        "lote: las excepciones del callback NO escapan",
        res_ok.get("ok") == 2 and res_ok.get("fallidos") == 0,
        f"ok={res_ok.get('ok')} fallidos={res_ok.get('fallidos')}",
    )

    # ---- Cancelacion: no se lanzan nuevas cuentas ----
    eventos = []
    candado2 = threading.Lock()
    evento = threading.Event()

    def _fake_cancelar(usuario, password=""):
        with candado2:
            eventos.append(usuario)
        time.sleep(0.02)
        evento.set()  # la primera en terminar pide cancelar
        return {"ok": True, "nombre": True, "handle": False}

    with mock.patch.object(gi, "aplicar_propuesta", _fake_cancelar):
        res_cancel = gi.aplicar_propuestas_en_lote(
            [f"c{i}" for i in range(8)], max_workers=1, cancelar=evento
        )
    check(
        "lote: cancelar evita lanzar nuevas cuentas (cancelado=True)",
        res_cancel.get("cancelado") is True
        and len(eventos) < 8
        and res_cancel.get("ok") == len(eventos)
        and res_cancel.get("total") == 8,
        f"lanzadas={len(eventos)} cancelado={res_cancel.get('cancelado')} ok={res_cancel.get('ok')}",
    )

    # ---- Cancelado desde el inicio: no lanza ninguna ----
    evento2 = threading.Event()
    evento2.set()
    with mock.patch.object(
        gi, "aplicar_propuesta", lambda usuario, password="": {"ok": True}
    ):
        res_pre = gi.aplicar_propuestas_en_lote(["x1", "x2"], cancelar=evento2)
    check(
        "lote: evento ya set -> no lanza nada y cancelado=True",
        res_pre.get("cancelado") is True
        and res_pre.get("ok") == 0
        and res_pre.get("total") == 2
        and not res_pre.get("resultados"),
        repr(res_pre),
    )

    # ---- max_workers invalido no rompe ----
    with mock.patch.object(
        gi, "aplicar_propuesta", lambda usuario, password="": {"ok": True, "handle": False}
    ):
        res_inv = gi.aplicar_propuestas_en_lote(["z1"], max_workers="dos")
    check(
        "lote: max_workers invalido cae al default y termina",
        res_inv.get("ok") == 1,
        repr(res_inv.get("ok")),
    )

    # ---- renombrar=True: llama al fake solo con ok+handle ----
    renombrados_llamados = []

    def _fake_renombrar(usuario, dry_run=False):
        renombrados_llamados.append(usuario)
        if usuario == "ren1":
            return {"ok": True, "renombrado": True, "error": ""}
        raise RuntimeError("colision de clave")

    def _fake_aplicar_rename(usuario, password=""):
        if usuario == "ren2":
            return {"ok": True, "nombre": True, "handle": False}  # sin handle: no renombra
        if usuario == "ren_falla":
            return {"ok": False, "error": "no aplico"}
        return {"ok": True, "nombre": True, "handle": True}

    with mock.patch.object(gi, "aplicar_propuesta", _fake_aplicar_rename), mock.patch(
        "core.renombrar.renombrar_al_handle_actual", _fake_renombrar
    ):
        res_ren = gi.aplicar_propuestas_en_lote(
            ["ren1", "ren2", "ren3", "ren_falla"], max_workers=2, renombrar=True
        )

    por_usuario = {e.get("usuario"): e for e in res_ren.get("resultados") or []}
    check(
        "renombrar: solo renombra las cuentas con handle aplicado",
        sorted(renombrados_llamados) == ["ren1", "ren3"],
        repr(renombrados_llamados),
    )
    check(
        "renombrar: exito marca renombrado=True y cuenta en renombrados",
        por_usuario.get("ren1", {}).get("renombrado") is True
        and res_ren.get("renombrados") == 1,
        f"renombrados={res_ren.get('renombrados')}",
    )
    check(
        "renombrar: si falla guarda error_renombrado y ok sigue True",
        por_usuario.get("ren3", {}).get("ok") is True
        and "colision" in str(por_usuario.get("ren3", {}).get("error_renombrado", "")),
        repr(por_usuario.get("ren3")),
    )
    check(
        "renombrar: handle no aplicado no intenta renombrar",
        por_usuario.get("ren2", {}).get("renombrado") is False
        and por_usuario.get("ren2", {}).get("error_renombrado") == "",
        repr(por_usuario.get("ren2")),
    )

    # ---- Nunca lanza aunque aplicar_propuesta explote siempre ----
    with mock.patch.object(
        gi, "aplicar_propuesta", side_effect=RuntimeError("siempre explota")
    ):
        res_explota = gi.aplicar_propuestas_en_lote(["q1", "q2"])
    check(
        "lote: si aplicar_propuesta lanza siempre, no propaga y reporta fallidos",
        res_explota.get("ok") == 0
        and res_explota.get("fallidos") == 2
        and len(res_explota.get("errores") or []) == 2,
        repr(res_explota),
    )


# --------------------------------------------------------------------------- #
# (f) ia_disponible
# --------------------------------------------------------------------------- #
def test_ia_disponible(check):
    print("(f) ia_disponible: keys placeholder vs key real")
    from core.config import settings

    for key in ("", "test_123", "placeholder_key", "tu_api_key", "your_openai_key", "algo_placeholder_x"):
        with mock.patch.object(settings, "openai_api_key", key):
            check(
                f"ia: key {key!r} -> False",
                gi.ia_disponible() is False,
                repr(gi.ia_disponible()),
            )
    with mock.patch.object(settings, "openai_api_key", "sk-real-1234567890"):
        check(
            "ia: key real -> True",
            gi.ia_disponible() is True,
            repr(gi.ia_disponible()),
        )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_normalizar_tipo(check)
    test_partido_local(check)
    test_asignar_mixto(check)
    test_asignar_ia(check)
    test_handle_generico(check)
    test_proteccion_asignar(check)
    test_cruce_registro_seccion(check)
    test_aplicar_lote(check)
    test_ia_disponible(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_identidades.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())

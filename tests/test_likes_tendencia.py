"""Tests minimos del preset "tendencia en X" (pausa humana + Like+RT).

Cubren, sin Chrome y sin BD real (fakes + monkeypatch):

  - `web/operaciones/_helpers._pausa_entre_cuentas`: float fijo, tupla
    (min, max), None (no duerme) y valor invalido (nunca lanza).
  - `_helpers.ejecutar_en_cuentas(..., pausa_entre=...)`: la pausa duerme
    ENTRE cuenta y cuenta (n-1 veces para n cuentas; nunca antes de la
    primera) y NO rompe el flujo (exitos/fallidos/registro intactos).
  - Fuente de `web/operaciones/likes.py`: el modo "Like + RT" usa
    `solo_retwittear(..., dar_like=True)` y `tipo="rt"`; el modo "Solo Like"
    conserva `tipo="like"` y la pausa humana `pausa_entre=(2.0, 6.0)`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent


class _CuentaFake(SimpleNamespace):
    pass


def _cuenta(usuario: str) -> _CuentaFake:
    return _CuentaFake(usuario=usuario, tier_calidad="", activa=True)


class _BotFake:
    cuenta_suspendida = False
    ultima_url_publicada = ""

    def __init__(self, usuario: str):
        self.usuario = usuario

    def accion_ok(self, *args, **kwargs):
        return True

    def cerrar(self):
        pass


class _FactoryFake:
    llamadas: list = []

    @classmethod
    def crear_bot(cls, plataforma, usuario):
        cls.llamadas.append((plataforma, usuario))
        return _BotFake(usuario)


def run(check):
    import plataformas.base as base_mod

    from web.operaciones import _helpers as helpers

    # ---------------- 1) helper `_pausa_entre_cuentas` ----------------
    durmio: list = []

    def _sleep(segundos):
        durmio.append(segundos)

    original_sleep = helpers.time.sleep
    original_uniform = helpers.random.uniform
    helpers.time.sleep = _sleep
    helpers.random.uniform = lambda a, b: 2.5
    try:
        durmio.clear()
        helpers._pausa_entre_cuentas(None)
        check("pausa: None no duerme", durmio == [], str(durmio))

        durmio.clear()
        helpers._pausa_entre_cuentas(3.0)
        check("pausa: float fijo duerme esa cantidad", durmio == [3.0], str(durmio))

        durmio.clear()
        helpers._pausa_entre_cuentas((1.0, 4.0))
        check("pausa: tupla sortea con random.uniform", durmio == [2.5], str(durmio))

        durmio.clear()
        helpers._pausa_entre_cuentas("basura")
        check("pausa: valor invalido no duerme ni lanza", durmio == [], str(durmio))

        durmio.clear()
        helpers._pausa_entre_cuentas((5.0, 1.0))  # min > max: uniform lo resuelve
        check("pausa: tupla invertida no lanza", durmio == [2.5], str(durmio))
    finally:
        helpers.time.sleep = original_sleep
        helpers.random.uniform = original_uniform

    # ---------------- 2) ejecutar_en_cuentas con pausa_entre ----------------
    original_factory = base_mod.PlataformaFactory
    original_registrar = helpers.registrar_accion
    registros: list = []

    def _registrar(usuario, tipo, estado, url="", detalle=""):
        registros.append((usuario, tipo, estado))

    base_mod.PlataformaFactory = _FactoryFake
    helpers.registrar_accion = _registrar
    _FactoryFake.llamadas = []
    durmio.clear()
    helpers.time.sleep = _sleep
    helpers.random.uniform = lambda a, b: 2.5
    try:
        cuentas = [_cuenta("a"), _cuenta("b"), _cuenta("c")]
        res = helpers.ejecutar_en_cuentas(
            cuentas,
            lambda bot: bot.accion_ok(),
            "twitter",
            None,
            None,
            tipo="like",
            pausa_entre=(2.0, 6.0),
        )
        check(
            "ejecutar pausa: duerme ENTRE cuentas (n-1 veces, no antes de la 1a)",
            durmio == [2.5, 2.5] and len(durmio) == len(cuentas) - 1,
            str(durmio),
        )
        check(
            "ejecutar pausa: flujo intacto (3 exitos, 0 omitidas, 0 fallidos)",
            res["exitos"] == 3
            and res["fallidos"] == 0
            and res["omitidas"] == 0,
            ascii(res),
        )
        check(
            "ejecutar pausa: registro intacto (3 exitos)",
            [u for u, _, _ in registros] == ["a", "b", "c"],
            str(registros),
        )

        # Sin pausa_entre (retrocompatibilidad): comportamiento EXACTO actual.
        _FactoryFake.llamadas = []
        registros.clear()
        durmio.clear()
        res_sin = helpers.ejecutar_en_cuentas(
            [_cuenta("sola")],
            lambda bot: bot.accion_ok(),
            "twitter",
            None,
            None,
            tipo="like",
        )
        check(
            "ejecutar sin pausa: no duerme (retrocompatible)",
            durmio == []
            and res_sin["exitos"] == 1
            and [u for u, _, _ in registros] == ["sola"],
            f"durmio={durmio} registros={registros}",
        )
    finally:
        base_mod.PlataformaFactory = original_factory
        helpers.registrar_accion = original_registrar
        helpers.time.sleep = original_sleep
        helpers.random.uniform = original_uniform

    # ---------------- 3) Fuente de likes.py: modos + pausa humana ----------------
    fuente_likes = (RAIZ / "web" / "operaciones" / "likes.py").read_text(
        encoding="utf-8"
    )
    check(
        "fuente likes: modo Like+RT llama solo_retwittear con dar_like=True",
        "solo_retwittear([url], bot.usuario, dar_like=True)" in fuente_likes,
    )
    check(
        "fuente likes: el modo Like+RT registra tipo='rt'",
        'tipo="rt"' in fuente_likes,
    )
    check(
        "fuente likes: el modo Solo Like conserva tipo='like'",
        'tipo="like"' in fuente_likes,
    )
    check(
        "fuente likes: baraja las cuentas antes de ejecutar",
        "random.shuffle(cuentas_sel)" in fuente_likes,
    )
    check(
        "fuente likes: pasa pausa humana (2.0, 6.0) a ejecutar_en_cuentas",
        "pausa_entre=(2.0, 6.0)" in fuente_likes,
    )

    # ---------------- 4) Fuente de activacion_masiva.py: preset ----------------
    fuente_am = (
        RAIZ / "web" / "operaciones" / "activacion_masiva.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente activacion: preset de rafaga setea curva + fase1 + porcentajes",
        'st.session_state["act_roles_curva"] = True' in fuente_am
        and 'st.session_state["act_roles_curva_fase1"] = 3' in fuente_am
        and 'st.session_state["act_roles_pct_hashtags"] = 15' in fuente_am
        and 'st.session_state["act_roles_pct_cita"] = 30' in fuente_am
        and 'st.session_state["act_roles_pct_rt"] = 35' in fuente_am
        and 'st.session_state["act_roles_pct_comentario"] = 20' in fuente_am,
    )
    check(
        "fuente activacion: el preset usa key unica btn_tendencia_preset",
        'key="btn_tendencia_preset"' in fuente_am,
    )

"""Operacion CHANGE.ORG: REPORTES DE POLITICAS + REGISTRO + FIRMAS.

La pagina tiene TRES pestanas:

1. "🚩 Ataque de reportes": reporta una peticion de Change.org por violar sus
   normas. Dos modos:
     - "🧾 Con cuentas registradas (recomendado)": cada reporte sale con una
       cuenta de X (reparto round-robin); el backend inicia sesion o registra
       la cuenta en Change.org antes de reportar (asi los avisos de Change
       llegan al correo de esa cuenta).
     - "🎭 Anonimo (identidades IA)": comportamiento clasico con identidades
       generadas por IA que quedan en la granja `CuentaChange`.
2. "🧾 Cuentas Change.org": registra o inicia sesion en Change.org con el
   correo y la contrasena de las cuentas de X (los avisos de Change llegan a
   esos correos; si el correo ya tiene cuenta, solo inicia sesion). Al
   registrar/entrar con exito, la sesion (cookies) se guarda en
   `data/cookies/change/` y en la BD `sesion_change`, y se REUTILIZA en las
   corridas siguientes (incluso en Railway) para no volver a pedir captcha.
3. "✍️ Firmar peticiones": firma masivamente una peticion con el flujo de 8
   pasos (Firmar la peticion -> ¡Ya firmaste! -> No, prefiero compartirla ->
   Copiar enlace -> Continuar). Con cuentas registradas usa la sesion
   persistida (sin captcha); en anonimo genera identidades y las marca como
   firmadas (`CuentaChange.usada_firma`).

Backend congelado (`cuentas/change_org.py`), importado PEREZOSAMENTE dentro
del handler del boton para que la pagina cargue sin Chrome:

    ejecutar_campana_reportes(
        url_peticion=..., contexto=..., cantidad=..., max_workers=...,
        usar_proxies=..., pais_proxy=..., guardar_identidades=...,
        headless=..., cancelar=<threading.Event>, callback=cb,
        cuentas=[{usuario,email,email_password,nombre_mostrado}] | None,
        esperar_captcha_seg=None,   # modo ASISTIDO (respaldo; solo si la firma lo acepta)
        resolver_captcha="auto",    # CapSolver (solo si la firma lo acepta)
    )

    ejecutar_campana_registros(
        cuentas=[...], max_workers=..., usar_proxies=..., pais_proxy=...,
        headless=..., cancelar=<threading.Event>, callback=cb,
        esperar_captcha_seg=None,   # modo ASISTIDO (respaldo; solo si la firma lo acepta)
        resolver_captcha="auto",    # CapSolver (solo si la firma lo acepta)
    )

Callbacks (llegan en los hilos worker del backend):
  - "inicio": ``{"tipo":"inicio","total":N}``.
  - "reporte": ``{"tipo":"reporte","hechas":i,"total":N,"ok":bool,
    "email":str,"usuario":str (solo con cuentas),"con_cuenta":bool,
    "identidad":{...} (solo anonimo),"detalle":str}``.
  - "registro": ``{"tipo":"registro","hechas":i,"total":N,"ok":bool,
    "usuario":str,"email":str,"estado":"nueva"|"existente"|"fallo"|"omitida",
    "detalle":str}``.
  - "espera_captcha": ``{"tipo":"espera_captcha","hechas":i,"total":N,
    "usuario":str,"email":str,"detalle":"esperando captcha (Ns)"}``: el modo
    asistido espera a que el humano resuelva el reto de Cloudflare; solo
    agrega una linea al log (NO cuenta como exito/fallo).
  - "captcha_api": ``{"tipo":"captcha_api","estado":"iniciando"|"resuelto"|
    "fallo","metodo":"capsolver","usuario":str,"email":str,"segundos":N,
    "detalle":str}``: avisos del solucionador AUTOMATICO CapSolver (kwarg
    `resolver_captcha`); solo agregan una linea al log (NO cuentan como
    exito/fallo). En "fallo", si el modo asistido esta activo
    (`esperar_captcha_seg` > 0 en los parametros de la campana), la linea
    recuerda que se esperara a que el humano lo resuelva.

Captcha automatico (CapSolver) + modo asistido de RESPALDO: en
"⚙️ Opciones avanzadas" de AMBAS pestanas estan el estado del solucionador
(caption) y el checkbox "🤖 Resolver captcha automáticamente (CapSolver)"
(default True SOLO si hay `CAPSOLVER_API_KEY`; con el solucionador inactivo
queda deshabilitado y la campana no puede pasar `"auto"`). El checkbox emite
`"auto"` marcado u `"off"` desmarcado y se pasa como `resolver_captcha` al
backend SOLO si su firma lo acepta (`_acepta_kwarg`, retrocompatible).
Cuando CapSolver no esta disponible o falla, el modo asistido sigue siendo el
respaldo: el checkbox "🧠 Esperar a que resuelvas el captcha a mano (respaldo
si CapSolver no está disponible o falla)". Con el marcado, la pagina fuerza
Chrome visible (el checkbox de visible queda deshabilitado), muestra los
segundos maximos de espera y pasa `esperar_captcha_seg=N` al backend SOLO si
su firma lo acepta: con un backend viejo se lanza sin el kwarg y se avisa con
`st.warning`. El backend espera N segundos a que un humano resuelva el reto y
luego continua solo.

El callback SOLO muta el registro de campanas a nivel modulo (`_CAMPANAS` +
`_CAMPANAS_LOCK`): jamas toca `st.*`. El panel `_render_proceso_activo()` se
pinta SIEMPRE al inicio de `render()` (no en `st.session_state`), asi el
avance/log sobrevive a un rerun o a navegar a otra operacion, con
`st.fragment(run_every=1s)` cuando existe y fallback `time.sleep(1)+st.rerun()`
solo mientras la campana corre. El guard de campana unica es COMPARTIDO entre
reportes y registros.

La granja de identidades (`CuentaChange`) se muestra al final en un expander
con el total de filas y las ultimas 20, SIEMPRE envuelta en try/except: si la
tabla o la BD no existen, la pagina sigue funcionando con un caption.

Las tablas del panel final muestran SOLO los exitos (reportes y registros):
los fallidos y las omitidas no se listan, y un caption debajo resume sus
totales. En el panel final de reportes, un expander
"🖼️ Imagen del éxito (N)" muestra hasta 8 capturas de los exitos
(`resultado["captura"]`, clave OPCIONAL: si falta, la ruta no existe o el
archivo no se puede mostrar, se omite).
"""
from __future__ import annotations

import inspect
import os
import threading
import time
import uuid

import streamlit as st

from web.ui import cabecera

# ============================ CONSTANTES ============================

# Tipos de campana del registro/panel (etiquetan el panel en vivo).
TIPO_REPORTES = "reportes"
TIPO_REGISTROS = "registros"
TIPO_FIRMAS = "firmas"

# Modos del ataque de reportes (radio de la pestana 1).
MODO_CON_CUENTAS = "🧾 Con cuentas registradas (recomendado)"
MODO_ANONIMO = "🎭 Anónimo (identidades IA)"

# ============================ REGISTRO DE CAMPANAS ============================
# Las campanas de Change.org de ESTE proceso (en curso y terminadas), tanto de
# reportes como de registros. La UI lee el estado desde aqui y no desde
# `st.session_state`, por lo que el panel reaparece aunque el usuario recargue
# o cambie de operacion. Los callbacks del backend corren en hilos worker:
# SIEMPRE mutan el registro bajo lock y NUNCA llaman a `st.*` (Streamlit no es
# thread-safe).
_CAMPANAS: dict = {}
_CAMPANAS_LOCK = threading.RLock()

# Estados que cuentan como "campana en curso" (bloquean lanzar otra).
_ESTADOS_ACTIVOS = ("en_curso", "deteniendo")

# Maximo de lineas del log en memoria (anti-fuga en campanas de 200 cuentas).
LOG_MAX = 200

# Etiqueta del pais "sin filtro" del selector de proxies.
PAIS_TODAS = "Todas"

# Modo asistido anti-bot (Cloudflare): textos compartidos por ambas pestanas.
CAPTCHA_LABEL = (
    "🧠 Esperar a que resuelvas el captcha a mano "
    "(respaldo si CapSolver no está disponible o falla)"
)
CAPTCHA_CAPTION = (
    "Se abrirá una ventana de Chrome: resuelve el reto de Cloudflare cuando "
    "aparezca y el bot continuará solo. Usa 1 navegador para el modo asistido."
)
CAPTCHA_AVISO_BACKEND = (
    "⚠️ El modo asistido no está disponible en este backend "
    "(`esperar_captcha_seg`): la campaña se lanzó sin esperar a que resuelvas "
    "el captcha."
)

# Captcha automatico (CapSolver): estado visible y checkbox por pestana. Con
# el solucionador activo (env `CAPSOLVER_API_KEY`) el backend resuelve el reto
# de Cloudflare sin humano; el modo asistido queda como RESPALDO.
CAPTCHA_SOLVER_LABEL = "🤖 Resolver captcha automáticamente (CapSolver)"
CAPTCHA_SOLVER_ACTIVO = (
    "🤖 Captcha automático: CapSolver activo — el reto se resuelve solo; "
    "el modo asistido queda como respaldo."
)
CAPTCHA_SOLVER_INACTIVO = (
    "⚠️ Captcha automático: CapSolver no configurado (falta "
    "CAPSOLVER_API_KEY en el .env). El reto se resolverá a mano si activas "
    "el modo asistido."
)


def _entero(valor, default: int = 0) -> int:
    """int() tolerante a None/str/basura (nunca lanza)."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


def _en_contexto_streamlit() -> bool:
    """True solo dentro de un script de Streamlit (AppTest incluido).

    En modo bare (tests que llaman helpers sin runtime) es False, de modo que
    el lanzamiento no intenta `st.rerun()`."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def _campana_actual():
    """Entrada de la campana mas reciente (en curso o terminada); None si no hay."""
    with _CAMPANAS_LOCK:
        if not _CAMPANAS:
            return None
        return next(reversed(_CAMPANAS.values()))


def _campana_en_curso():
    """Entrada activa (en_curso/deteniendo) o None. Guard de campana unica."""
    with _CAMPANAS_LOCK:
        for entrada in reversed(list(_CAMPANAS.values())):
            if str(entrada.get("estado") or "") in _ESTADOS_ACTIVOS:
                return entrada
    return None


def _registrar_campana(hilo=None, evento=None, parametros=None,
                       tipo: str = TIPO_REPORTES) -> str:
    """Registra una campana nueva en estado `en_curso` y devuelve su id.

    Contrato de la entrada: id, tipo ("reportes"/"registros"), hilo, evento
    (threading.Event de paro), estado, hechas, total, enviados (ok), fallidos,
    nuevas, existentes, omitidas, identidades, log (lista), resumen y error
    (mas inicio/fin/parametros internos)."""
    id_campana = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    with _CAMPANAS_LOCK:
        _CAMPANAS[id_campana] = {
            "id": id_campana,
            "tipo": str(tipo or TIPO_REPORTES),
            "hilo": hilo,
            "evento": evento if evento is not None else threading.Event(),
            "estado": "en_curso",
            "hechas": 0,
            "total": 0,
            "enviados": 0,
            "fallidos": 0,
            "nuevas": 0,
            "existentes": 0,
            "omitidas": 0,
            "identidades": 0,
            "log": [],
            "resumen": None,
            "error": "",
            "inicio": time.time(),
            "fin": None,
            "parametros": dict(parametros or {}),
        }
    return id_campana


def _agregar_linea(entrada: dict, linea: str) -> None:
    """Agrega una linea al log de la entrada y lo recorta a LOG_MAX."""
    log = entrada.setdefault("log", [])
    log.append(str(linea))
    if len(log) > LOG_MAX:
        del log[:-LOG_MAX]


def _anotar_evento(id_campana, evento) -> None:
    """Procesa UN evento del callback del backend (puro: jamas toca `st.*`).

    - `{"tipo":"inicio","total":N}`: fija el total planificado.
    - `{"tipo":"reporte",...}`: incrementa hechas/enviados/fallidos, agrega la
      linea al log (`✅ Reporte enviado por {email|@usuario}` si ok; `❌ ... —
      {detalle}` si falla) y cuenta las identidades recibidas (solo modo
      anonimo). El log se recorta a `LOG_MAX` lineas.
    - `{"tipo":"registro",...}`: cuenta ok (nuevas/existentes) y fallidos, y
      loggea `✅ @usuario — cuenta nueva`, `✅ @usuario — ya tenía cuenta (solo
      inició sesión)` o `❌ @usuario — detalle`.
    - `{"tipo":"espera_captcha",...}`: modo asistido; agrega al log
      `🧠 {email|@usuario} — esperando a que resuelvas el captcha en Chrome
      (Ns)…` y NO toca ningun contador (no es exito ni fallo).
    - `{"tipo":"captcha_api",...}`: solucionador automatico CapSolver; agrega
      al log la linea de "iniciando"/"resuelto"/"fallo" y NO toca ningun
      contador (no es exito ni fallo). En "fallo", si la campana se lanzo con
      el modo asistido (`parametros["esperar_captcha_seg"]` > 0), la linea
      termina con "; se esperará a que lo resuelvas a mano".
    """
    if not isinstance(evento, dict) or not id_campana:
        return
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is None:
            return
        tipo = str(evento.get("tipo") or "")
        if tipo == "inicio":
            entrada["total"] = _entero(
                evento.get("total"), _entero(entrada.get("total"))
            )
            return
        if tipo == "registro":
            usuario = str(evento.get("usuario") or "").strip().lstrip("@") or "?"
            ok = bool(evento.get("ok"))
            estado = str(evento.get("estado") or "").strip().lower()
            detalle = str(evento.get("detalle") or "").strip()
            entrada["hechas"] = _entero(
                evento.get("hechas"), _entero(entrada.get("hechas")) + 1
            )
            total = _entero(evento.get("total"))
            if total:
                entrada["total"] = total
            if ok:
                entrada["enviados"] = _entero(entrada.get("enviados")) + 1
                if estado == "nueva":
                    entrada["nuevas"] = _entero(entrada.get("nuevas")) + 1
                    linea = f"✅ @{usuario} — cuenta nueva"
                elif estado == "existente":
                    entrada["existentes"] = (
                        _entero(entrada.get("existentes")) + 1
                    )
                    linea = f"✅ @{usuario} — ya tenía cuenta (solo inició sesión)"
                else:
                    linea = f"✅ @{usuario} — {detalle or 'ok'}"
            elif estado == "omitida":
                # Cuenta sin correo/contrasena: no abrio navegador (el backend
                # la reporta aparte, no es un fallo).
                entrada["omitidas"] = _entero(entrada.get("omitidas")) + 1
                linea = f"➖ @{usuario} — {detalle or 'omitida'}"
            else:
                entrada["fallidos"] = _entero(entrada.get("fallidos")) + 1
                linea = f"❌ @{usuario} — {detalle or 'fallo sin detalle'}"
            _agregar_linea(entrada, linea)
            return
        if tipo == "espera_captcha":
            # Modo asistido: el humano debe resolver el reto de Cloudflare en
            # la ventana de Chrome. NO es exito ni fallo: solo deja la linea.
            email = str(evento.get("email") or "").strip()
            usuario = str(evento.get("usuario") or "").strip().lstrip("@")
            identificador = email or (f"@{usuario}" if usuario else "?")
            detalle = str(evento.get("detalle") or "").strip()
            sufijo = ""
            if detalle:
                # El backend manda "esperando captcha (Ns)": se conserva "(Ns)".
                inicio, fin = detalle.rfind("("), detalle.rfind(")")
                sufijo = (
                    f" ({detalle[inicio + 1:fin].strip()})"
                    if 0 <= inicio < fin
                    else f" ({detalle})"
                )
            _agregar_linea(
                entrada,
                f"🧠 {identificador} — esperando a que resuelvas el captcha "
                f"en Chrome{sufijo}…",
            )
            return
        if tipo == "captcha_api":
            # Solucionador AUTOMATICO CapSolver: solo informa en el feed (no
            # es exito ni fallo). En "fallo" recuerda el respaldo humano si el
            # modo asistido esta activo en esta campana.
            email = str(evento.get("email") or "").strip()
            usuario = str(evento.get("usuario") or "").strip().lstrip("@")
            identificador = email or (f"@{usuario}" if usuario else "?")
            estado = str(evento.get("estado") or "").strip().lower()
            detalle = str(evento.get("detalle") or "").strip()
            if estado == "iniciando":
                linea = (
                    f"🤖 {identificador} — resolviendo captcha automáticamente "
                    "con CapSolver..."
                )
            elif estado == "resuelto":
                segundos = _entero(evento.get("segundos"))
                linea = (
                    f"✅ {identificador} — captcha resuelto automáticamente "
                    f"(~{segundos}s)"
                )
            elif estado == "fallo":
                linea = (
                    f"⚠️ {identificador} — CapSolver no pudo resolver el captcha"
                )
                if detalle:
                    linea += f" · {detalle}"
                parametros = entrada.get("parametros") or {}
                if _entero(parametros.get("esperar_captcha_seg")):
                    linea += "; se esperará a que lo resuelvas a mano"
            else:
                return
            _agregar_linea(entrada, linea)
            return
        if tipo == "firma":
            # Firma de peticiones: mismo contador que los reportes (enviados =
            # firmadas), con su etiqueta de log propia.
            email = str(evento.get("email") or "").strip()
            usuario = str(evento.get("usuario") or "").strip().lstrip("@")
            identificador = email or (f"@{usuario}" if usuario else "?")
            ok = bool(evento.get("ok"))
            detalle = str(evento.get("detalle") or "").strip()
            entrada["hechas"] = _entero(
                evento.get("hechas"), _entero(entrada.get("hechas")) + 1
            )
            total = _entero(evento.get("total"))
            if total:
                entrada["total"] = total
            if ok:
                entrada["enviados"] = _entero(entrada.get("enviados")) + 1
                linea = f"✍️ Firma registrada por {identificador}"
            else:
                entrada["fallidos"] = _entero(entrada.get("fallidos")) + 1
                linea = f"❌ {identificador} — {detalle}"
            identidad = evento.get("identidad")
            if isinstance(identidad, dict) and identidad:
                entrada["identidades"] = _entero(entrada.get("identidades")) + 1
            _agregar_linea(entrada, linea)
            return
        if tipo != "reporte":
            return

        email = str(evento.get("email") or "").strip()
        usuario = str(evento.get("usuario") or "").strip().lstrip("@")
        identificador = email or (f"@{usuario}" if usuario else "?")
        ok = bool(evento.get("ok"))
        detalle = str(evento.get("detalle") or "").strip()
        entrada["hechas"] = _entero(
            evento.get("hechas"), _entero(entrada.get("hechas")) + 1
        )
        total = _entero(evento.get("total"))
        if total:
            entrada["total"] = total
        if ok:
            entrada["enviados"] = _entero(entrada.get("enviados")) + 1
            linea = f"✅ Reporte enviado por {identificador}"
        else:
            entrada["fallidos"] = _entero(entrada.get("fallidos")) + 1
            linea = f"❌ {identificador} — {detalle}"
        identidad = evento.get("identidad")
        if isinstance(identidad, dict) and identidad:
            entrada["identidades"] = _entero(entrada.get("identidades")) + 1
        _agregar_linea(entrada, linea)


def _finalizar_campana(id_campana, resumen=None, error=None) -> None:
    """Cierra la campana: estado final, resumen/error y fin (nunca lanza).

    Acepta resumenes de reportes (`enviados`) y de registros (`exitosos`)."""
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is None:
            return
        entrada["fin"] = time.time()
        resumen_dict = resumen if isinstance(resumen, dict) else {}
        # Sin excepcion, el backend puede reportar un error interno en su
        # resumen ("error"): se guarda para que el panel lo muestre como error.
        entrada["error"] = (
            str(error)
            if error is not None
            else str(resumen_dict.get("error") or "")
        )
        if error is not None:
            entrada["estado"] = "error"
        elif resumen_dict.get("cancelada"):
            entrada["estado"] = "cancelada"
        else:
            entrada["estado"] = "terminada"
        if resumen_dict:
            entrada["resumen"] = resumen_dict
            entrada["enviados"] = _entero(
                resumen_dict.get(
                    "enviados",
                    resumen_dict.get("exitosos", resumen_dict.get("firmadas")),
                ),
                _entero(entrada.get("enviados")),
            )
            entrada["fallidos"] = _entero(
                resumen_dict.get("fallidos"), _entero(entrada.get("fallidos"))
            )
            entrada["total"] = _entero(
                resumen_dict.get("total"), _entero(entrada.get("total"))
            )
            entrada["nuevas"] = _entero(
                resumen_dict.get("nuevas"), _entero(entrada.get("nuevas"))
            )
            entrada["existentes"] = _entero(
                resumen_dict.get("existentes"), _entero(entrada.get("existentes"))
            )
            entrada["omitidas"] = _entero(
                resumen_dict.get("omitidas"), _entero(entrada.get("omitidas"))
            )


def _solicitar_paro() -> bool:
    """Setea el `threading.Event` de la campana activa. True si habia una.

    Pasa el estado a `deteniendo` (la campana sigue viva hasta que el backend
    termina el reporte/registro en curso)."""
    with _CAMPANAS_LOCK:
        entrada = None
        for candidata in reversed(list(_CAMPANAS.values())):
            if str(candidata.get("estado") or "") in _ESTADOS_ACTIVOS:
                entrada = candidata
                break
        if entrada is None:
            return False
        if entrada.get("estado") == "en_curso":
            entrada["estado"] = "deteniendo"
        try:
            evento = entrada.get("evento")
            if evento is not None:
                evento.set()
        except Exception:
            pass
    return True


def _limpiar_registro(id_campana=None) -> None:
    """Borra la entrada indicada (o TODAS) del registro. Limpieza/tests."""
    with _CAMPANAS_LOCK:
        if id_campana is None:
            _CAMPANAS.clear()
        else:
            _CAMPANAS.pop(id_campana, None)


def _snapshot(id_campana=None) -> dict | None:
    """Copia inmutable de la entrada para pintar el panel sin retener el lock."""
    with _CAMPANAS_LOCK:
        if id_campana:
            entrada = _CAMPANAS.get(id_campana)
        elif _CAMPANAS:
            entrada = next(reversed(_CAMPANAS.values()))
        else:
            entrada = None
        if entrada is None:
            return None
        estado = str(entrada.get("estado") or "")
        return {
            "id": str(entrada.get("id") or ""),
            "tipo": str(entrada.get("tipo") or TIPO_REPORTES),
            "estado": estado,
            "en_curso": estado in _ESTADOS_ACTIVOS,
            "deteniendo": estado == "deteniendo",
            "hechas": _entero(entrada.get("hechas")),
            "total": _entero(entrada.get("total")),
            "enviados": _entero(entrada.get("enviados")),
            "fallidos": _entero(entrada.get("fallidos")),
            "nuevas": _entero(entrada.get("nuevas")),
            "existentes": _entero(entrada.get("existentes")),
            "omitidas": _entero(entrada.get("omitidas")),
            "identidades": _entero(entrada.get("identidades")),
            "log": list(entrada.get("log") or []),
            "resumen": (
                entrada.get("resumen")
                if isinstance(entrada.get("resumen"), dict)
                else {}
            ),
            "error": str(entrada.get("error") or ""),
        }


def _estado_resultado(resultado: dict) -> str:
    """`estado` normalizado de un resultado de registros ('' si falta)."""
    return str(resultado.get("estado") or "").strip().lower()


def _filas_resultados(resumen: dict) -> list:
    """Filas (Usuario/Email/Detalle) de SOLO los reportes exitosos.

    Los fallidos NO se muestran (el caption del panel da su total). `Usuario`
    usa `resultado["usuario"]` y, si no hay, cae al correo. `captura` (nueva,
    opcional) no forma parte de la tabla: la usa el expander de imagenes."""
    filas = []
    for resultado in (resumen or {}).get("resultados") or []:
        if not isinstance(resultado, dict) or not resultado.get("ok"):
            continue
        email = str(resultado.get("email") or "").strip()
        usuario = str(resultado.get("usuario") or "").strip().lstrip("@")
        filas.append(
            {
                "Usuario": f"@{usuario}" if usuario else (email or "?"),
                "Email": email,
                "Detalle": str(resultado.get("detalle") or "").strip(),
            }
        )
    return filas


def _conteos_resultados(resumen: dict) -> dict:
    """Conteos {exitos, nuevas, existentes, fallidos, omitidas} del resumen.

    Prefiere los contadores del backend (`enviados`/`exitosos`, `nuevas`,
    `existentes`, `fallidos`, `omitidas`) y cae a contar la lista `resultados`
    cuando el contador falta (backend viejo con lista pero sin totales).
    Nunca lanza."""
    resumen = resumen or {}
    resultados = [
        resultado
        for resultado in resumen.get("resultados") or []
        if isinstance(resultado, dict)
    ]
    exitos_filas = sum(1 for resultado in resultados if resultado.get("ok"))
    nuevas_filas = sum(
        1
        for resultado in resultados
        if resultado.get("ok") and _estado_resultado(resultado) == "nueva"
    )
    existentes_filas = sum(
        1
        for resultado in resultados
        if resultado.get("ok")
        and _estado_resultado(resultado) == "existente"
    )
    omitidas_filas = sum(
        1
        for resultado in resultados
        if not resultado.get("ok")
        and _estado_resultado(resultado) == "omitida"
    )
    fallidos_filas = max(0, len(resultados) - exitos_filas - omitidas_filas)
    enviados = resumen.get("enviados")
    if enviados is None:
        enviados = resumen.get("exitosos")
    return {
        "exitos": max(_entero(enviados, exitos_filas), exitos_filas),
        "nuevas": max(_entero(resumen.get("nuevas"), nuevas_filas), nuevas_filas),
        "existentes": max(
            _entero(resumen.get("existentes"), existentes_filas),
            existentes_filas,
        ),
        "fallidos": max(
            _entero(resumen.get("fallidos"), fallidos_filas), fallidos_filas
        ),
        "omitidas": max(
            _entero(resumen.get("omitidas"), omitidas_filas), omitidas_filas
        ),
    }


def _filas_resultados_registros(resumen: dict) -> list:
    """Filas (Usuario/Email/Estado/Detalle) de SOLO los registros exitosos.

    Exito = `ok=True` con estado "nueva" | "existente"; los fallos y las
    omitidas NO se muestran (el caption del panel da sus totales)."""
    filas = []
    for resultado in (resumen or {}).get("resultados") or []:
        if not isinstance(resultado, dict) or not resultado.get("ok"):
            continue
        estado = _estado_resultado(resultado)
        if estado == "nueva":
            etiqueta = "🆕 Nueva"
        elif estado == "existente":
            etiqueta = "👤 Existente"
        else:
            continue
        usuario = str(resultado.get("usuario") or "").strip().lstrip("@")
        filas.append(
            {
                "Usuario": f"@{usuario}" if usuario else "",
                "Email": str(resultado.get("email") or ""),
                "Estado": etiqueta,
                "Detalle": str(resultado.get("detalle") or ""),
            }
        )
    return filas


def _resolver_captura(ruta) -> str:
    """Ruta de archivo existente de una captura ('' si falta o no es archivo).

    Acepta rutas absolutas y relativas a la raiz del proyecto (`resolver_ruta`
    cuando esta disponible; si no, `os.path.abspath`). Nunca lanza."""
    candidata = str(ruta or "").strip()
    if not candidata:
        return ""
    if not os.path.isabs(candidata):
        try:
            from core.config import resolver_ruta

            candidata = str(resolver_ruta(candidata))
        except Exception:
            candidata = os.path.abspath(candidata)
    return candidata if os.path.isfile(candidata) else ""


def _detalle_corto(detalle, limite: int = 80) -> str:
    """Detalle en una sola linea, recortado a `limite` caracteres (con …)."""
    texto = " ".join(str(detalle or "").split())
    if len(texto) <= limite:
        return texto
    return texto[: max(1, limite - 1)].rstrip() + "…"


def _capturas_exitos(resumen: dict, limite: int = 8) -> list:
    """[(ruta, quien, detalle)] de los exitos CON captura existente.

    La clave `captura` es OPCIONAL (`resultado.get("captura") or ""`): con un
    backend viejo no hay capturas y el expander no se pinta. Se conserva el
    orden de `resultados` y se corta en `limite` (default 8). Nunca lanza."""
    capturas = []
    for resultado in (resumen or {}).get("resultados") or []:
        if not isinstance(resultado, dict) or not resultado.get("ok"):
            continue
        ruta = _resolver_captura(resultado.get("captura") or "")
        if not ruta:
            continue
        email = str(resultado.get("email") or "").strip()
        usuario = str(resultado.get("usuario") or "").strip().lstrip("@")
        capturas.append(
            (
                ruta,
                usuario or email,
                str(resultado.get("detalle") or "").strip(),
            )
        )
        if len(capturas) >= limite:
            break
    return capturas


def _boton_detener(snap: dict) -> None:
    """Boton "⛔ Detener" compartido por los paneles en vivo de ambos tipos."""
    if st.button(
        "⛔ Detener",
        type="primary",
        key="change_btn_detener",
        disabled=bool(snap.get("deteniendo")),
        help=(
            "Pide al backend que pare: no lanza acciones nuevas y cierra los "
            "navegadores al terminar la acción en curso."
        ),
    ):
        _solicitar_paro()
        st.rerun()


def _pintar_reportes_en_vivo(snap: dict) -> None:
    """Panel en vivo del ataque de reportes (modo cuentas o anonimo)."""
    st.markdown("#### 🚩 Ataque de reportes en vivo")
    total = max(1, snap["total"])
    hechas = min(snap["hechas"], total) if snap["total"] else snap["hechas"]
    st.progress(min(1.0, hechas / total))
    st.markdown(
        f"✅ **{snap['enviados']} enviados** · ❌ {snap['fallidos']} "
        f"fallidos · 🧾 {snap['identidades']} identidades"
    )
    lineas = snap["log"][-12:]
    if lineas:
        st.markdown("  \n".join(lineas))
    else:
        st.markdown("⏳ Esperando los primeros reportes…")
    if snap["deteniendo"]:
        st.warning("⛔ Detención solicitada: terminando el reporte en curso…")
    _boton_detener(snap)


def _pintar_reportes_final(snap: dict) -> None:
    """Resumen final del ultimo ataque de reportes (terminado/cancelado/error)."""
    st.markdown("#### 🧾 Último ataque de reportes")
    resumen = snap["resumen"] or {}
    if snap["estado"] == "error" or snap["error"]:
        st.error(
            "❌ Ataque de reportes interrumpido: "
            + (snap["error"] or "error inesperado del backend")
        )
    elif snap["estado"] == "cancelada" or resumen.get("cancelada"):
        st.warning(
            f"⛔ Ataque de reportes detenido: {snap['enviados']} enviados / "
            f"{snap['fallidos']} fallidos"
        )
    else:
        st.success(
            f"✅ Ataque de reportes terminado: {snap['enviados']} enviados / "
            f"{snap['fallidos']} fallidos"
        )
    filas = _filas_resultados(resumen)
    totales = _conteos_resultados(resumen)
    if filas:
        st.dataframe(filas, use_container_width=True, hide_index=True)
        st.caption(
            f"✅ {totales['exitos']} reportes exitosos · "
            f"❌ {totales['fallidos']} fallidos (no se muestran)"
        )
    else:
        st.caption("Sin reportes exitosos.")
    capturas = _capturas_exitos(resumen)
    if capturas:
        with st.expander(
            f"🖼️ Imagen del éxito ({len(capturas)})", expanded=False
        ):
            for ruta, quien, detalle in capturas:
                etiqueta = f"@{quien}" if quien else "?"
                if detalle:
                    etiqueta += f" — {_detalle_corto(detalle)}"
                try:
                    st.image(ruta, caption=etiqueta)
                except Exception:
                    continue
    guardadas = _entero(resumen.get("identidades_guardadas"))
    if guardadas > 0:
        st.caption(f"🧾 Identidades guardadas: {guardadas}")


def _pintar_registros_en_vivo(snap: dict) -> None:
    """Panel en vivo del registro de cuentas en Change.org."""
    st.markdown("#### 🧾 Registro de cuentas en vivo")
    total = max(1, snap["total"])
    hechas = min(snap["hechas"], total) if snap["total"] else snap["hechas"]
    st.progress(min(1.0, hechas / total))
    contador = (
        f"✅ **{snap['enviados']} ok** (🆕 {snap['nuevas']} nuevas · "
        f"👤 {snap['existentes']} existentes) · ❌ {snap['fallidos']} fallidos"
    )
    if snap["omitidas"]:
        contador += f" · ➖ {snap['omitidas']} omitidas"
    st.markdown(contador)
    lineas = snap["log"][-12:]
    if lineas:
        st.markdown("  \n".join(lineas))
    else:
        st.markdown("⏳ Esperando los primeros registros…")
    if snap["deteniendo"]:
        st.warning("⛔ Detención solicitada: terminando el registro en curso…")
    _boton_detener(snap)


def _pintar_registros_final(snap: dict) -> None:
    """Resumen final del ultimo registro de cuentas (terminado/cancelado/error)."""
    st.markdown("#### 🧾 Último registro de cuentas")
    resumen = snap["resumen"] or {}
    conteos = (
        f"{snap['enviados']} ok (🆕 {snap['nuevas']} nuevas · "
        f"👤 {snap['existentes']} existentes) / {snap['fallidos']} fallidos"
    )
    if snap["omitidas"]:
        conteos += f" · ➖ {snap['omitidas']} omitidas"
    if snap["estado"] == "error" or snap["error"]:
        st.error(
            "❌ Registro de cuentas interrumpido: "
            + (snap["error"] or "error inesperado del backend")
        )
    elif snap["estado"] == "cancelada" or resumen.get("cancelada"):
        st.warning(f"⛔ Registro detenido: {conteos}")
    else:
        st.success(f"✅ Registro terminado: {conteos}")
    filas = _filas_resultados_registros(resumen)
    totales = _conteos_resultados(resumen)
    if filas:
        st.dataframe(filas, use_container_width=True, hide_index=True)
    st.caption(
        f"✅ {totales['exitos']} ok (🆕 {totales['nuevas']} nuevas · "
        f"👤 {totales['existentes']} existentes) · "
        f"❌ {totales['fallidos']} fallidos · "
        f"➖ {totales['omitidas']} omitidas (no se muestran)"
    )


def _pintar_firmas_en_vivo(snap: dict) -> None:
    """Panel en vivo de la firma de peticiones (modo cuentas o anonimo)."""
    st.markdown("#### ✍️ Firma de peticiones en vivo")
    total = max(1, snap["total"])
    hechas = min(snap["hechas"], total) if snap["total"] else snap["hechas"]
    st.progress(min(1.0, hechas / total))
    st.markdown(
        f"✍️ **{snap['enviados']} firmadas** · ❌ {snap['fallidos']} "
        f"fallidas · 🧾 {snap['identidades']} identidades"
    )
    lineas = snap["log"][-12:]
    if lineas:
        st.markdown("  \n".join(lineas))
    else:
        st.markdown("⏳ Esperando las primeras firmas…")
    if snap["deteniendo"]:
        st.warning("⛔ Detención solicitada: terminando la firma en curso…")
    _boton_detener(snap)


def _pintar_firmas_final(snap: dict) -> None:
    """Resumen final de la ultima firma de peticiones (terminada/cancelada/error)."""
    st.markdown("#### 🧾 Última firma de peticiones")
    resumen = snap["resumen"] or {}
    if snap["estado"] == "error" or snap["error"]:
        st.error(
            "❌ Firma de peticiones interrumpida: "
            + (snap["error"] or "error inesperado del backend")
        )
    elif snap["estado"] == "cancelada" or resumen.get("cancelada"):
        st.warning(
            f"⛔ Firma de peticiones detenida: {snap['enviados']} firmadas / "
            f"{snap['fallidos']} fallidas"
        )
    else:
        st.success(
            f"✅ Firma de peticiones terminada: {snap['enviados']} firmadas / "
            f"{snap['fallidos']} fallidas"
        )
    filas = _filas_resultados(resumen)
    if filas:
        st.dataframe(filas, use_container_width=True, hide_index=True)
        st.caption(
            f"✍️ {snap['enviados']} firmadas · "
            f"❌ {snap['fallidos']} fallidas (no se muestran)"
        )
    else:
        st.caption("Sin firmas exitosas.")
    capturas = _capturas_exitos(resumen)
    if capturas:
        with st.expander(
            f"🖼️ Imagen del éxito ({len(capturas)})", expanded=False
        ):
            for ruta, quien, detalle in capturas:
                etiqueta = f"@{quien}" if quien else "?"
                if detalle:
                    etiqueta += f" — {_detalle_corto(detalle)}"
                try:
                    st.image(ruta, caption=etiqueta)
                except Exception:
                    continue
    guardadas = _entero(resumen.get("identidades_guardadas"))
    if guardadas > 0:
        st.caption(f"🧾 Identidades guardadas: {guardadas}")


def _pintar_proceso(snap=None) -> None:
    """Pinta UNA pasada del panel: en vivo o el resumen final (`st.*` aqui).

    Etiqueta el panel segun el tipo de campana ("reportes", "registros" o
    "firmas")."""
    if snap is None:
        snap = _snapshot()
    if not snap:
        return
    tipo = str(snap.get("tipo") or TIPO_REPORTES)
    if tipo == TIPO_REGISTROS:
        if snap["en_curso"]:
            _pintar_registros_en_vivo(snap)
        else:
            _pintar_registros_final(snap)
        return
    if tipo == TIPO_FIRMAS:
        if snap["en_curso"]:
            _pintar_firmas_en_vivo(snap)
        else:
            _pintar_firmas_final(snap)
        return
    if snap["en_curso"]:
        _pintar_reportes_en_vivo(snap)
    else:
        _pintar_reportes_final(snap)


def _refrescar_con_fragmento() -> bool:
    """`st.fragment(run_every=1s)` que repinta el proceso en vivo.

    Mismo patron que `activacion_masiva._refrescar_con_fragmento`: devuelve
    False si esta version de Streamlit no soporta fragmentos (el llamador cae
    al bucle `time.sleep(1); st.rerun()`). Cuando la campana termina, el
    fragmento dispara UN rerun completo y deja de refrescar."""
    fabrica = getattr(st, "fragment", None)
    if not callable(fabrica):
        return False

    def _pasada():
        if _campana_en_curso() is None:
            st.rerun(scope="app")
            return
        _pintar_proceso()

    try:
        decorada = fabrica(run_every=1.0)(_pasada)
    except Exception:
        return False
    decorada()
    return True


def _render_proceso_activo(max_pasos=None) -> None:
    """Panel de proceso persistente (auto-refresco SOLO mientras corre).

    - Con campana en curso: `st.fragment(run_every=1s)` si esta disponible; si
      no, bucle `time.sleep(1); st.rerun()`.
    - Sin campana en curso: UNA pasada (resumen final en memoria o nada).

    `max_pasos` es un flag interno para tests/llamadas acotadas: con un valor
    (p. ej. 1) se pinta UNA sola pasada, sin fragmentos ni bucles."""
    if max_pasos is not None:
        _pintar_proceso()
        return
    if _campana_en_curso() is None:
        _pintar_proceso()
        return
    if _refrescar_con_fragmento():
        return
    _pintar_proceso()
    time.sleep(1)
    st.rerun()


# ============================ PROXIES ============================

def _paises_proxy() -> list:
    """Paises detectados en `data/proxies/*.txt` (sin `quemados.txt`).

    Nunca lanza: sin carpeta o sin archivos devuelve []."""
    try:
        from core.config import resolver_ruta

        carpeta = resolver_ruta("data/proxies")
        if not os.path.isdir(carpeta):
            return []
        paises = set()
        for nombre in os.listdir(carpeta):
            if not str(nombre).lower().endswith(".txt"):
                continue
            base = os.path.splitext(str(nombre))[0].strip()
            if not base or base.lower() == "quemados":
                continue
            paises.add(base.lower())
        return sorted(paises)
    except Exception:
        return []


def _proxies_disponibles(pais: str = "") -> int:
    """Cuantos proxies usaria la campana (tolerante a fallos: 0)."""
    try:
        from utils.proxies import ProxyManager

        gestor = ProxyManager()
        if pais:
            return len(gestor.cargar_por_pais(pais) or [])
        return len(gestor.cargar_proxies() or [])
    except Exception:
        return 0


def _aviso_proxies(pais_proxy: str, pais_sel: str) -> None:
    """Caption con los proxies disponibles o warning si el filtro esta vacio."""
    disponibles = _proxies_disponibles(pais_proxy)
    if disponibles:
        etiqueta_pais = f" para {pais_sel}" if pais_proxy else ""
        st.caption(f"🌐 {disponibles} proxies disponibles{etiqueta_pais}.")
    else:
        st.warning(
            "No hay proxies disponibles con ese filtro: la campaña "
            "continuará sin proxy."
        )


# ============================ CUENTAS DE X ============================

def _cargar_cuentas_change() -> list:
    """Cuentas twitter activas CON correo y contraseña (para Change.org).

    Devuelve dicts compatibles con `_selector_masivo` (mismo patron que
    `activacion_masiva._cargar_cuentas_con_roles`) mas `email`,
    `email_password` y `nombre_mostrado` para pasarlos al backend. Nunca
    lanza: ante error muestra el detalle y devuelve [] (la pagina sigue)."""
    from core.database import get_db_session
    from core.models import Cuenta
    from core.registros import normalizar_tipo_cuenta
    from core.roles import normalizar_rol_activacion
    from core.secciones import normalizar_seccion

    try:
        from core.tiers import normalizar_tier
    except Exception:
        def normalizar_tier(valor):
            return ""

    try:
        with get_db_session() as db:
            cuentas = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter", Cuenta.activa == True)
                .order_by(Cuenta.usuario)
                .all()
            )
            filas = []
            for cuenta in cuentas:
                email = str(getattr(cuenta, "email", "") or "").strip()
                email_password = str(
                    getattr(cuenta, "email_password", "") or ""
                ).strip()
                if not email or not email_password:
                    continue
                filas.append(
                    {
                        "usuario": cuenta.usuario,
                        "status": cuenta.status or "",
                        "seccion": normalizar_seccion(
                            getattr(cuenta, "seccion", "")
                        ),
                        "tipo_cuenta": normalizar_tipo_cuenta(
                            getattr(cuenta, "tipo_cuenta", "")
                        ),
                        "handle_actual": (
                            getattr(cuenta, "handle_actual", "") or ""
                        ).strip(),
                        "grupo": cuenta.grupo or "",
                        "rol_activacion": normalizar_rol_activacion(
                            getattr(cuenta, "rol_activacion", "")
                        ),
                        "tier_calidad": normalizar_tier(
                            getattr(cuenta, "tier_calidad", "")
                        ),
                        "pausada_activacion": bool(
                            getattr(cuenta, "pausada_activacion", False)
                        ),
                        "email": email,
                        "email_password": email_password,
                        "nombre_mostrado": str(
                            getattr(cuenta, "nombre_mostrado", "") or ""
                        ).strip(),
                    }
                )
            return filas
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudieron cargar las cuentas de Change: {e}")
        return []


def _cuentas_para_backend(filas) -> list:
    """Dicts `{"usuario","email","email_password","nombre_mostrado"}` limpios.

    Solo incluye cuentas con usuario, correo y contraseña del correo no
    vacios (lo que exige el backend). Nunca lanza."""
    cuentas = []
    for fila in filas or []:
        if not isinstance(fila, dict):
            continue
        usuario = str(fila.get("usuario") or "").strip().lstrip("@")
        email = str(fila.get("email") or "").strip()
        email_password = str(fila.get("email_password") or "").strip()
        if not usuario or not email or not email_password:
            continue
        cuentas.append(
            {
                "usuario": usuario,
                "email": email,
                "email_password": email_password,
                "nombre_mostrado": str(
                    fila.get("nombre_mostrado") or ""
                ).strip(),
            }
        )
    return cuentas


def _acepta_kwarg(func, nombre: str) -> bool:
    """True si `func` acepta el kwarg `nombre` (o tiene **kwargs). Nunca lanza."""
    try:
        parametros = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if nombre in parametros:
        return True
    return any(
        parametro.kind == inspect.Parameter.VAR_KEYWORD
        for parametro in parametros.values()
    )


# ============================ GRANJA DE IDENTIDADES ============================

def _cargar_granja(limite: int = 20):
    """(total, filas) de la granja `CuentaChange` (ultimas `limite`).

    Puede lanzar si la tabla/BD no existe: `_visor_granja` lo envuelve."""
    from core.database import obtener_sesion
    from core.models import CuentaChange

    with obtener_sesion() as db:
        total = int(db.query(CuentaChange).count() or 0)
        registros = (
            db.query(CuentaChange)
            .order_by(CuentaChange.fecha_creacion.desc())
            .limit(max(0, int(limite)))
            .all()
        )
    filas = []
    for registro in registros:
        nombre = (
            f"{registro.nombre or ''} {registro.apellido or ''}".strip()
        )
        fecha = getattr(registro, "fecha_creacion", None)
        filas.append(
            {
                "Nombre": nombre,
                "Email": str(registro.email or ""),
                "CP": str(registro.codigo_postal or ""),
                "Petición": str(registro.url_peticion or ""),
                "Fecha": (
                    fecha.strftime("%Y-%m-%d %H:%M") if fecha else ""
                ),
            }
        )
    return total, filas


def _visor_granja() -> None:
    """Expander con el total y las ultimas 20 identidades de la granja.

    Envuelto en try/except: si la tabla/BD no existe solo pinta un caption
    informativo y la pagina sigue funcionando."""
    try:
        total, filas = _cargar_granja(limite=20)
    except Exception:
        st.caption(
            "ℹ️ La granja de identidades todavía no está disponible (sin "
            "tabla o sin conexión a la BD); el ataque de reportes sigue "
            "funcionando."
        )
        return
    with st.expander(
        f"🧾 Identidades creadas para firmar ({total})", expanded=False
    ):
        if filas:
            st.dataframe(filas, use_container_width=True, hide_index=True)
        else:
            st.caption("Aún no hay identidades guardadas en la granja.")


# ============================ LANZAMIENTO ============================

def _iniciar_hilo(id_campana, runner, nombre: str) -> bool:
    """Arranca el hilo daemon de la campana (cierra la entrada si falla).

    Nunca lanza: ante error marca la campana con la excepcion y avisa en la
    UI. Devuelve True si el hilo arranco."""
    hilo = threading.Thread(target=runner, daemon=True, name=nombre)
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is not None:
            entrada["hilo"] = hilo
    try:
        hilo.start()
    except BaseException as e:  # noqa: BLE001
        _finalizar_campana(id_campana, error=e)
        st.error(f"No se pudo iniciar la campaña de Change.org: {e}")
        return False
    return True


def _lanzar_ataque(url, contexto, cantidad, workers, usar_proxies, pais_proxy,
                   guardar_identidades, headless, cuentas=None,
                   esperar_captcha_seg=0, resolver_captcha="auto") -> None:
    """Valida y lanza el ataque de reportes en un hilo daemon (nunca lanza).

    `cuentas` (opcional) = filas seleccionadas del selector; None/[] = modo
    anonimo con identidades IA. `esperar_captcha_seg` > 0 pide el modo asistido
    anti-bot: se pasa al backend SOLO si su firma lo acepta (`_acepta_kwarg`);
    con un backend viejo se lanza sin el kwarg y se deja un `st.warning` para
    el siguiente render. `resolver_captcha` ("auto"|"off") activa/desactiva el
    captcha automatico CapSolver y se pasa tambien SOLO si la firma lo acepta
    (retrocompatible). El backend se importa AQUI (perezoso): la pagina
    carga sin Chrome. El callback solo muta `_CAMPANAS` bajo lock."""
    direccion = str(url or "").strip()
    if not direccion or "change.org" not in direccion.lower():
        st.warning(
            "La URL debe ser una petición de Change.org "
            "(https://www.change.org/p/...)."
        )
        return
    motivo = str(contexto or "").strip()
    if not motivo:
        st.warning(
            "Escribe el motivo general de la queja: la IA lo necesita para "
            "redactar los reportes."
        )
        return
    # `cuentas is None` = modo anónimo; `[]` = modo con cuentas sin selección.
    es_modo_cuentas = cuentas is not None
    cuentas_envio = _cuentas_para_backend(cuentas) if cuentas else []
    if es_modo_cuentas and not cuentas_envio:
        st.warning(
            "Selecciona al menos una cuenta con correo y contraseña para "
            "reportar (o cambia al modo anónimo)."
        )
        return
    if _campana_en_curso() is not None:
        st.warning(
            "⛔ Ya hay un ataque de reportes en curso: espera a que termine o "
            "pulsa «⛔ Detener» en el panel de arriba."
        )
        return

    try:
        from cuentas.change_org import ejecutar_campana_reportes
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo importar el backend de reportes: {e}")
        return
    if cuentas_envio and not _acepta_kwarg(
        ejecutar_campana_reportes, "cuentas"
    ):
        st.error(
            "El backend de reportes todavía no soporta el modo con cuentas "
            "registradas (`cuentas=`): actualiza `cuentas/change_org.py` o "
            "usa el modo anónimo."
        )
        return

    # Modo asistido anti-bot: el kwarg se pasa SOLO si la firma lo acepta.
    asistido = bool(esperar_captcha_seg)
    soporta_captcha = _acepta_kwarg(
        ejecutar_campana_reportes, "esperar_captcha_seg"
    )
    if asistido and not soporta_captcha:
        # Doble aviso: el `st.warning` inmediato y la bandera en
        # `session_state` (el `st.rerun()` del lanzamiento descarta los
        # elementos previos; `_formulario_reportes` repinta la bandera).
        if _en_contexto_streamlit():
            st.session_state["change_rep_aviso_asistido"] = CAPTCHA_AVISO_BACKEND
        st.warning(CAPTCHA_AVISO_BACKEND)
    # Captcha automatico (CapSolver): mismo patron retrocompatible.
    soporta_solver = _acepta_kwarg(
        ejecutar_campana_reportes, "resolver_captcha"
    )

    evento = threading.Event()
    id_campana = _registrar_campana(
        tipo=TIPO_REPORTES,
        evento=evento,
        parametros={
            "url": direccion,
            "cantidad": int(cantidad),
            "workers": int(workers),
            "usar_proxies": bool(usar_proxies),
            "pais_proxy": str(pais_proxy or ""),
            "guardar_identidades": bool(guardar_identidades),
            "headless": bool(headless),
            "esperar_captcha_seg": _entero(esperar_captcha_seg),
            "resolver_captcha": str(resolver_captcha or "auto"),
            "modo": MODO_CON_CUENTAS if cuentas_envio else MODO_ANONIMO,
            "cuentas_total": len(cuentas_envio),
        },
    )

    def _cb(evento_cb):
        # Corre en los hilos worker del backend: SOLO registro, jamas `st.*`.
        _anotar_evento(id_campana, evento_cb)

    def _runner():
        resumen = None
        error = None
        try:
            kwargs = dict(
                url_peticion=direccion,
                contexto=motivo,
                cantidad=int(cantidad),
                max_workers=int(workers),
                usar_proxies=bool(usar_proxies),
                pais_proxy=str(pais_proxy or ""),
                guardar_identidades=bool(guardar_identidades),
                headless=bool(headless),
                cancelar=evento,
                callback=_cb,
            )
            if _acepta_kwarg(ejecutar_campana_reportes, "cuentas"):
                # Modo anonimo: None (comportamiento clasico intacto).
                kwargs["cuentas"] = cuentas_envio or None
            if soporta_captcha:
                # 0 = sin modo asistido (falla rapido como siempre).
                kwargs["esperar_captcha_seg"] = _entero(esperar_captcha_seg)
            if soporta_solver:
                # "auto" = CapSolver si hay API key; "off" lo desactiva.
                kwargs["resolver_captcha"] = str(resolver_captcha or "auto")
            resumen = ejecutar_campana_reportes(**kwargs)

        except BaseException as e:  # noqa: BLE001
            error = e
        finally:
            _finalizar_campana(id_campana, resumen=resumen, error=error)

    if not _iniciar_hilo(id_campana, _runner, "change-reportes"):
        return
    if _en_contexto_streamlit():
        # El panel persistente (arriba) toma el relevo con el progreso en vivo.
        st.rerun()


def _lanzar_registros(cuentas, workers, usar_proxies, pais_proxy,
                      headless, esperar_captcha_seg=0,
                      resolver_captcha="auto") -> None:
    """Valida y lanza el registro de cuentas en Change.org (nunca lanza).

    Importa `ejecutar_campana_registros` PEREZOSAMENTE; si el backend aun no
    la tiene, muestra un `st.error` legible y no rompe la pagina. Con
    `esperar_captcha_seg` > 0 (modo asistido) se pasa el kwarg SOLO si la
    firma del backend lo acepta y, si no, se avisa para el siguiente render.
    `resolver_captcha` ("auto"|"off") se pasa con el mismo criterio
    retrocompatible. El callback solo muta `_CAMPANAS` bajo lock."""
    cuentas_envio = _cuentas_para_backend(cuentas)
    if not cuentas_envio:
        st.warning(
            "Selecciona al menos una cuenta con correo y contraseña para "
            "registrar en Change.org."
        )
        return
    if _campana_en_curso() is not None:
        st.warning(
            "⛔ Ya hay una campaña de Change.org en curso: espera a que "
            "termine o pulsa «⛔ Detener» en el panel de arriba."
        )
        return

    try:
        from cuentas import change_org as backend
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo importar el backend de Change.org: {e}")
        return
    funcion = getattr(backend, "ejecutar_campana_registros", None)
    if not callable(funcion):
        st.error(
            "El backend todavía no incluye «ejecutar_campana_registros» "
            "(cuentas/change_org.py): actualiza el backend para registrar "
            "cuentas en Change.org."
        )
        return

    # Modo asistido anti-bot: el kwarg se pasa SOLO si la firma lo acepta.
    asistido = bool(esperar_captcha_seg)
    soporta_captcha = _acepta_kwarg(funcion, "esperar_captcha_seg")
    if asistido and not soporta_captcha:
        # Doble aviso: el `st.warning` inmediato y la bandera en
        # `session_state` (el `st.rerun()` del lanzamiento descarta los
        # elementos previos; `_formulario_registros` repinta la bandera).
        if _en_contexto_streamlit():
            st.session_state["change_reg_aviso_asistido"] = CAPTCHA_AVISO_BACKEND
        st.warning(CAPTCHA_AVISO_BACKEND)
    # Captcha automatico (CapSolver): mismo patron retrocompatible.
    soporta_solver = _acepta_kwarg(funcion, "resolver_captcha")

    evento = threading.Event()
    id_campana = _registrar_campana(
        tipo=TIPO_REGISTROS,
        evento=evento,
        parametros={
            "workers": int(workers),
            "usar_proxies": bool(usar_proxies),
            "pais_proxy": str(pais_proxy or ""),
            "headless": bool(headless),
            "esperar_captcha_seg": _entero(esperar_captcha_seg),
            "resolver_captcha": str(resolver_captcha or "auto"),
            "cuentas_total": len(cuentas_envio),
        },
    )

    def _cb(evento_cb):
        # Corre en los hilos worker del backend: SOLO registro, jamas `st.*`.
        _anotar_evento(id_campana, evento_cb)

    def _runner():
        resumen = None
        error = None
        try:
            kwargs = dict(
                cuentas=list(cuentas_envio),
                max_workers=int(workers),
                usar_proxies=bool(usar_proxies),
                pais_proxy=str(pais_proxy or ""),
                headless=bool(headless),
                cancelar=evento,
                callback=_cb,
            )
            if soporta_captcha:
                # 0 = sin modo asistido (falla rapido como siempre).
                kwargs["esperar_captcha_seg"] = _entero(esperar_captcha_seg)
            if soporta_solver:
                # "auto" = CapSolver si hay API key; "off" lo desactiva.
                kwargs["resolver_captcha"] = str(resolver_captcha or "auto")
            resumen = funcion(**kwargs)
        except BaseException as e:  # noqa: BLE001
            error = e
        finally:
            _finalizar_campana(id_campana, resumen=resumen, error=error)

    if not _iniciar_hilo(id_campana, _runner, "change-registros"):
        return
    if _en_contexto_streamlit():
        # El panel persistente (arriba) toma el relevo con el progreso en vivo.
        st.rerun()


def _lanzar_firmas(url, cantidad, workers, usar_proxies, pais_proxy,
                   guardar_identidades, headless, cuentas=None,
                   esperar_captcha_seg=0, resolver_captcha="auto") -> None:
    """Valida y lanza la firma de peticiones en un hilo daemon (nunca lanza).

    `cuentas` (opcional) = filas seleccionadas del selector; None/[] = modo
    anonimo con identidades IA. El backend se importa AQUI (perezoso): la
    pagina carga sin Chrome. El callback solo muta `_CAMPANAS` bajo lock."""
    direccion = str(url or "").strip()
    if not direccion or "change.org" not in direccion.lower():
        st.warning(
            "La URL debe ser una petición de Change.org "
            "(https://www.change.org/p/...)."
        )
        return
    # `cuentas is None` = modo anónimo; `[]` = modo con cuentas sin selección.
    es_modo_cuentas = cuentas is not None
    cuentas_envio = _cuentas_para_backend(cuentas) if cuentas else []
    if es_modo_cuentas and not cuentas_envio:
        st.warning(
            "Selecciona al menos una cuenta con correo y contraseña para "
            "firmar (o cambia al modo anónimo)."
        )
        return
    if _campana_en_curso() is not None:
        st.warning(
            "⛔ Ya hay una campaña de Change.org en curso: espera a que "
            "termine o pulsa «⛔ Detener» en el panel de arriba."
        )
        return

    try:
        from cuentas.change_org import ejecutar_campana_firmas
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo importar el backend de firmas: {e}")
        return
    if cuentas_envio and not _acepta_kwarg(ejecutar_campana_firmas, "cuentas"):
        st.error(
            "El backend de firmas todavía no soporta el modo con cuentas "
            "registradas (`cuentas=`): actualiza `cuentas/change_org.py` o "
            "usa el modo anónimo."
        )
        return

    # Modo asistido anti-bot: el kwarg se pasa SOLO si la firma lo acepta.
    asistido = bool(esperar_captcha_seg)
    soporta_captcha = _acepta_kwarg(
        ejecutar_campana_firmas, "esperar_captcha_seg"
    )
    if asistido and not soporta_captcha:
        if _en_contexto_streamlit():
            st.session_state["change_fir_aviso_asistido"] = CAPTCHA_AVISO_BACKEND
        st.warning(CAPTCHA_AVISO_BACKEND)
    soporta_solver = _acepta_kwarg(ejecutar_campana_firmas, "resolver_captcha")

    evento = threading.Event()
    id_campana = _registrar_campana(
        tipo=TIPO_FIRMAS,
        evento=evento,
        parametros={
            "url": direccion,
            "cantidad": int(cantidad),
            "workers": int(workers),
            "usar_proxies": bool(usar_proxies),
            "pais_proxy": str(pais_proxy or ""),
            "guardar_identidades": bool(guardar_identidades),
            "headless": bool(headless),
            "esperar_captcha_seg": _entero(esperar_captcha_seg),
            "resolver_captcha": str(resolver_captcha or "auto"),
            "modo": MODO_CON_CUENTAS if cuentas_envio else MODO_ANONIMO,
            "cuentas_total": len(cuentas_envio),
        },
    )

    def _cb(evento_cb):
        _anotar_evento(id_campana, evento_cb)

    def _runner():
        resumen = None
        error = None
        try:
            kwargs = dict(
                url_peticion=direccion,
                cantidad=int(cantidad),
                max_workers=int(workers),
                usar_proxies=bool(usar_proxies),
                pais_proxy=str(pais_proxy or ""),
                guardar_identidades=bool(guardar_identidades),
                headless=bool(headless),
                cancelar=evento,
                callback=_cb,
            )
            if _acepta_kwarg(ejecutar_campana_firmas, "cuentas"):
                kwargs["cuentas"] = cuentas_envio or None
            if soporta_captcha:
                kwargs["esperar_captcha_seg"] = _entero(esperar_captcha_seg)
            if soporta_solver:
                kwargs["resolver_captcha"] = str(resolver_captcha or "auto")
            resumen = ejecutar_campana_firmas(**kwargs)
        except BaseException as e:  # noqa: BLE001
            error = e
        finally:
            _finalizar_campana(id_campana, resumen=resumen, error=error)

    if not _iniciar_hilo(id_campana, _runner, "change-firmas"):
        return
    if _en_contexto_streamlit():
        st.rerun()


# ============================ FORMULARIOS ============================

def _estado_solver_captcha() -> dict:
    """Estado del solucionador automatico CapSolver (import perezoso).

    Importa `utils.captcha_solver.estado` dentro de try/except: si el modulo
    no existe o falla, devuelve el estado inactivo con el motivo y la pagina
    sigue funcionando. NUNCA lanza."""
    try:
        from utils.captcha_solver import estado as captcha_solver_estado

        estado = captcha_solver_estado()
        if isinstance(estado, dict):
            return {
                "activo": bool(estado.get("activo")),
                "proveedor": str(estado.get("proveedor") or "capsolver"),
                "motivo": str(estado.get("motivo") or ""),
            }
    except Exception:
        pass
    return {
        "activo": False,
        "proveedor": "capsolver",
        "motivo": "solucionador no disponible (utils.captcha_solver)",
    }


def _texto_estado_solver(estado=None) -> str:
    """Caption del estado del solucionador (activo o no configurado).

    Con el solucionador inactivo se agrega el `motivo` cuando aporta detalle
    (el motivo estandar ya describe la falta de `CAPSOLVER_API_KEY`)."""
    estado = estado if isinstance(estado, dict) else _estado_solver_captcha()
    if bool(estado.get("activo")):
        return CAPTCHA_SOLVER_ACTIVO
    motivo = str(estado.get("motivo") or "").strip()
    texto = CAPTCHA_SOLVER_INACTIVO
    if motivo and "CAPSOLVER_API_KEY" not in motivo.upper():
        texto += f" ({motivo})"
    return texto


def _controles_solver_captcha(clave: str) -> str:
    """Estado + checkbox del captcha automatico de UNA pestana.

    Devuelve "auto" si el backend debe intentar CapSolver o "off" si no. El
    checkbox queda deshabilitado (y sin marcar) cuando CapSolver no esta
    configurado, asi que con el solucionador inactivo JAMAS se emite "auto"."""
    estado = _estado_solver_captcha()
    activo = bool(estado.get("activo"))
    st.caption(_texto_estado_solver(estado))
    marcado = st.checkbox(
        CAPTCHA_SOLVER_LABEL,
        value=activo,
        key=clave,
        disabled=not activo,
        help=(
            "Define `CAPSOLVER_API_KEY` en el .env para activarlo. Con el "
            "solucionador activo el reto de Cloudflare se resuelve solo; el "
            "modo asistido de abajo queda como respaldo si falla."
        ),
    )
    return "auto" if (activo and marcado) else "off"


def _controles_modo_asistido(clave_captcha: str, clave_segundos: str,
                             clave_visible: str) -> tuple:
    """Controles del modo asistido anti-bot de una pestana.

    Devuelve `(esperar_captcha_seg, chrome_visible)`. Con el checkbox marcado
    se muestra el number_input de segundos (30-900, default 180) y se FUERZA
    Chrome visible aunque el checkbox de visible venga apagado (queda
    deshabilitado): el reto de Cloudflare solo se resuelve con la ventana a la
    vista. `esperar_captcha_seg` = 0 con el modo apagado (clasico)."""
    asistido = st.checkbox(
        CAPTCHA_LABEL,
        value=False,
        key=clave_captcha,
        help=(
            "Change.org a veces pide un reto de Cloudflare («no eres un "
            "bot»). Con esto marcado, el bot abre Chrome visible y espera a "
            "que lo resuelvas antes de continuar."
        ),
    )
    esperar_captcha_seg = 0
    if asistido:
        esperar_captcha_seg = int(
            st.number_input(
                "Segundos máximos de espera",
                min_value=30,
                max_value=900,
                value=180,
                step=30,
                key=clave_segundos,
                help=(
                    "Tiempo máximo que el bot espera a que resuelvas el "
                    "reto anti-bot antes de fallar."
                ),
            )
        )
        st.caption(CAPTCHA_CAPTION)
    chrome_visible = st.checkbox(
        "🖥️ Chrome visible (debug)",
        value=bool(asistido),
        key=clave_visible,
        disabled=bool(asistido),
        help=(
            "Forzado por el modo asistido: el reto de Cloudflare solo se "
            "puede resolver con la ventana de Chrome a la vista."
            if asistido
            else "Solo para depurar: en producción la campaña corre headless."
        ),
    )
    if asistido:
        # Forzado DESPUES de leer el checkbox (puede venir apagado).
        chrome_visible = True
    return esperar_captcha_seg, bool(chrome_visible)


def _formulario_reportes(cuentas: list) -> None:
    """Pestana "🚩 Ataque de reportes": objetivo, modo y lanzamiento."""
    aviso = st.session_state.pop("change_rep_aviso_asistido", "")
    if aviso:
        st.warning(aviso)
    st.markdown("### 🎯 Objetivo del ataque")
    url = st.text_input(
        "URL de la petición objetivo",
        key="change_rep_url",
        placeholder="https://www.change.org/p/...",
        help="Petición de Change.org que se va a reportar por violar sus normas.",
    )
    contexto = st.text_area(
        "Motivo general de la queja (contexto para la IA)",
        key="change_rep_contexto",
        height=120,
        placeholder=(
            "Ej. La petición difunde información falsa y acosa a personas "
            "concretas para incumplir las normas de la comunidad."
        ),
        help=(
            "La IA redacta cada queja a partir de este motivo; no se copia "
            "literalmente en los reportes."
        ),
    )

    modo = st.radio(
        "Modo de reporte",
        [MODO_CON_CUENTAS, MODO_ANONIMO],
        index=0,
        key="change_rep_modo",
        help=(
            "Con cuentas registradas cada reporte sale con una cuenta de X "
            "(round-robin) que inicia sesión en Change.org; en anónimo se "
            "generan identidades con IA."
        ),
    )
    es_cuentas = modo == MODO_CON_CUENTAS

    seleccion: list = []
    if es_cuentas:
        if not cuentas:
            st.info(
                "No hay cuentas de X con correo y contraseña cargados. "
                "Importa o edita cuentas con su email y la contraseña del "
                "correo en «🗂️ Cuentas: Perfiles, Secciones & Nombres» para "
                "reportar con cuentas registradas."
            )
        else:
            from web.operaciones.cuentas import _selector_masivo

            seleccion = _selector_masivo(cuentas, "change_rep_selector")
            st.caption(
                f"🧾 {len(seleccion)} cuenta(s) seleccionada(s): cada reporte "
                "usa una cuenta en round-robin."
            )

    col_cantidad, col_workers = st.columns(2)
    with col_cantidad:
        cantidad = st.number_input(
            "Cantidad de reportes a enviar",
            min_value=1,
            max_value=100,
            value=5,
            step=1,
            key="change_rep_cantidad",
            help=(
                "Total de reportes; las cuentas se reparten en round-robin "
                "(una cuenta por reporte) en el modo con cuentas."
            ),
        )
    with col_workers:
        workers = st.number_input(
            "Navegadores simultáneos",
            min_value=1,
            max_value=5,
            value=2,
            step=1,
            key="change_rep_workers",
            help=(
                "Reportes en paralelo (1-5). En Railway no conviene pasar de "
                "2-3: cada Chrome consume RAM/CPU/hilos."
            ),
        )

    with st.expander("⚙️ Opciones avanzadas", expanded=False):
        rotar_proxies = st.checkbox(
            "🌐 Rotar proxy residencial por reporte",
            value=True,
            key="change_rep_proxies",
            help=(
                "Cada reporte sale con una IP distinta (menos bloqueos de "
                "Change.org). Si no hay proxies, el ataque continúa sin proxy."
            ),
        )
        paises = _paises_proxy()
        pais_sel = st.selectbox(
            "País del proxy",
            [PAIS_TODAS] + paises,
            index=0,
            key="change_rep_pais",
            help=(
                "Filtra los proxies por país (archivos de `data/proxies/`). "
                "«Todas» usa cualquier proxy disponible."
            ),
        )
        pais_proxy = "" if pais_sel == PAIS_TODAS else str(pais_sel)
        if rotar_proxies:
            _aviso_proxies(pais_proxy, pais_sel)
        if es_cuentas:
            # En modo con cuentas no se generan identidades: el checkbox de la
            # granja solo aplica al modo anónimo.
            guardar_identidades = False
        else:
            guardar_identidades = st.checkbox(
                "🧾 Guardar identidades en la base de datos",
                value=True,
                key="change_rep_guardar",
                help=(
                    "Las identidades usadas quedan en la granja "
                    "(`CuentaChange`) para reutilizarlas más adelante."
                ),
            )
        resolver_captcha = _controles_solver_captcha("change_rep_solver")
        esperar_captcha_seg, chrome_visible = _controles_modo_asistido(
            "change_rep_captcha", "change_rep_captcha_seg", "change_rep_visible"
        )
        headless = not bool(chrome_visible)

    if st.button(
        "🚩 Lanzar ataque de reportes",
        type="primary",
        key="btn_change_reportes",
        disabled=bool(es_cuentas and not cuentas),
        help=(
            "Con cuentas registradas: cada reporte inicia sesión en "
            "Change.org con una cuenta de X. En anónimo: genera identidades "
            "con IA. El progreso se muestra arriba y sobrevive a las recargas."
        ),
    ):
        _lanzar_ataque(
            url=url,
            contexto=contexto,
            cantidad=cantidad,
            workers=workers,
            usar_proxies=bool(rotar_proxies),
            pais_proxy=pais_proxy,
            guardar_identidades=bool(guardar_identidades),
            headless=headless,
            cuentas=seleccion if es_cuentas else None,
            esperar_captcha_seg=int(esperar_captcha_seg),
            resolver_captcha=resolver_captcha,
        )


def _formulario_registros(cuentas: list) -> None:
    """Pestana "🧾 Cuentas Change.org": registra/entra cuentas de X."""
    aviso = st.session_state.pop("change_reg_aviso_asistido", "")
    if aviso:
        st.warning(aviso)
    st.caption(
        "Registra o inicia sesión en Change.org con el correo y la contraseña "
        "de las cuentas de X (así los avisos de Change llegan a esos correos). "
        "Si el correo ya tiene cuenta, solo inicia sesión."
    )
    st.info(
        "🔐 **Sesión persistente:** al registrar/entrar con éxito, el bot guarda "
        "las cookies de la sesión (en `data/cookies/change/` y en la BD "
        "`sesion_change`). En las siguientes corridas —incluida Railway— esas "
        "cookies se reutilizan y **ya no se vuelve a pedir captcha**. Crea las "
        "cuentas en tu máquina LOCAL (Chrome visible + modo asistido) para "
        "resolver el captcha rápido y reutiliza la sesión después."
    )
    if not cuentas:
        st.info(
            "No hay cuentas de X con correo y contraseña cargados. Importa o "
            "edita cuentas con su email y la contraseña del correo en "
            "«🗂️ Cuentas: Perfiles, Secciones & Nombres»."
        )
        return

    from web.operaciones.cuentas import _selector_masivo

    seleccion = _selector_masivo(cuentas, "change_reg_selector")
    st.caption(f"🧾 Se registrarán **{len(seleccion)}** cuentas (las que ya "
               "tengan cuenta en Change.org solo inician sesión).")

    workers = st.number_input(
        "Navegadores simultáneos",
        min_value=1,
        max_value=5,
        value=2,
        step=1,
        key="change_reg_workers",
        help=(
            "Registros en paralelo (1-5). En Railway no conviene pasar de "
            "2-3: cada Chrome consume RAM/CPU/hilos."
        ),
    )

    with st.expander("⚙️ Opciones avanzadas", expanded=False):
        rotar_proxies = st.checkbox(
            "🌐 Rotar proxy residencial por cuenta",
            value=True,
            key="change_reg_proxies",
            help=(
                "Cada cuenta sale con una IP distinta (menos bloqueos de "
                "Change.org). Si no hay proxies, la campaña sigue sin proxy."
            ),
        )
        paises = _paises_proxy()
        pais_sel = st.selectbox(
            "País del proxy",
            [PAIS_TODAS] + paises,
            index=0,
            key="change_reg_pais",
            help=(
                "Filtra los proxies por país (archivos de `data/proxies/`). "
                "«Todas» usa cualquier proxy disponible."
            ),
        )
        pais_proxy = "" if pais_sel == PAIS_TODAS else str(pais_sel)
        if rotar_proxies:
            _aviso_proxies(pais_proxy, pais_sel)
        resolver_captcha = _controles_solver_captcha("change_reg_solver")
        esperar_captcha_seg, chrome_visible = _controles_modo_asistido(
            "change_reg_captcha", "change_reg_captcha_seg", "change_reg_visible"
        )
        headless = not bool(chrome_visible)

    if st.button(
        "🧾 Registrar en Change.org",
        type="primary",
        key="btn_change_registros",
        help=(
            "Crea la cuenta en Change.org (o inicia sesión si el correo ya "
            "tiene una) con el correo y la contraseña de cada cuenta de X."
        ),
    ):
        _lanzar_registros(
            cuentas=seleccion,
            workers=workers,
            usar_proxies=bool(rotar_proxies),
            pais_proxy=pais_proxy,
            headless=headless,
            esperar_captcha_seg=int(esperar_captcha_seg),
            resolver_captcha=resolver_captcha,
        )


def _formulario_firmas(cuentas: list) -> None:
    """Pestana "✍️ Firmar peticiones": firma masiva de una peticion."""
    aviso = st.session_state.pop("change_fir_aviso_asistido", "")
    if aviso:
        st.warning(aviso)
    st.markdown("### 🎯 Objetivo de la firma")
    url = st.text_input(
        "URL de la petición a firmar",
        key="change_fir_url",
        placeholder="https://www.change.org/p/...",
        help="Petición de Change.org que se va a firmar masivamente.",
    )

    modo = st.radio(
        "Modo de firma",
        [MODO_CON_CUENTAS, MODO_ANONIMO],
        index=0,
        key="change_fir_modo",
        help=(
            "Con cuentas registradas cada firma sale con una cuenta de X "
            "(round-robin) que inicia sesión o restaura su sesión en "
            "Change.org; en anónimo se generan identidades con IA."
        ),
    )
    es_cuentas = modo == MODO_CON_CUENTAS

    seleccion: list = []
    if es_cuentas:
        if not cuentas:
            st.info(
                "No hay cuentas de X con correo y contraseña cargados. Importa "
                "o edita cuentas con su email y la contraseña del correo en "
                "«🗂️ Cuentas: Perfiles, Secciones & Nombres» para firmar con "
                "cuentas registradas."
            )
        else:
            from web.operaciones.cuentas import _selector_masivo

            seleccion = _selector_masivo(cuentas, "change_fir_selector")
            st.caption(
                f"🧾 {len(seleccion)} cuenta(s) seleccionada(s): cada firma "
                "usa una cuenta en round-robin."
            )

    col_cantidad, col_workers = st.columns(2)
    with col_cantidad:
        cantidad = st.number_input(
            "Cantidad de firmas a enviar",
            min_value=1,
            max_value=100,
            value=5,
            step=1,
            key="change_fir_cantidad",
            help=(
                "Total de firmas; las cuentas se reparten en round-robin "
                "(una cuenta por firma) en el modo con cuentas."
            ),
        )
    with col_workers:
        workers = st.number_input(
            "Navegadores simultáneos",
            min_value=1,
            max_value=5,
            value=2,
            step=1,
            key="change_fir_workers",
            help=(
                "Firmas en paralelo (1-5). En Railway no conviene pasar de "
                "2-3: cada Chrome consume RAM/CPU/hilos."
            ),
        )

    with st.expander("⚙️ Opciones avanzadas", expanded=False):
        rotar_proxies = st.checkbox(
            "🌐 Rotar proxy residencial por firma",
            value=True,
            key="change_fir_proxies",
            help=(
                "Cada firma sale con una IP distinta (menos bloqueos de "
                "Change.org). Si no hay proxies, la campaña sigue sin proxy."
            ),
        )
        paises = _paises_proxy()
        pais_sel = st.selectbox(
            "País del proxy",
            [PAIS_TODAS] + paises,
            index=0,
            key="change_fir_pais",
            help=(
                "Filtra los proxies por país (archivos de `data/proxies/`). "
                "«Todas» usa cualquier proxy disponible."
            ),
        )
        pais_proxy = "" if pais_sel == PAIS_TODAS else str(pais_sel)
        if rotar_proxies:
            _aviso_proxies(pais_proxy, pais_sel)
        if es_cuentas:
            guardar_identidades = False
        else:
            guardar_identidades = st.checkbox(
                "🧾 Guardar identidades en la base de datos",
                value=True,
                key="change_fir_guardar",
                help=(
                    "Las identidades usadas quedan en la granja "
                    "(`CuentaChange`) y se marcan como firmadas "
                    "(`usada_firma`)."
                ),
            )
        resolver_captcha = _controles_solver_captcha("change_fir_solver")
        esperar_captcha_seg, chrome_visible = _controles_modo_asistido(
            "change_fir_captcha", "change_fir_captcha_seg", "change_fir_visible"
        )
        headless = not bool(chrome_visible)

    if st.button(
        "✍️ Lanzar firma de peticiones",
        type="primary",
        key="btn_change_firmas",
        disabled=bool(es_cuentas and not cuentas),
        help=(
            "Con cuentas registradas: cada firma inicia sesión o restaura su "
            "sesión en Change.org. En anónimo: genera identidades con IA. El "
            "progreso se muestra arriba y sobrevive a las recargas."
        ),
    ):
        _lanzar_firmas(
            url=url,
            cantidad=cantidad,
            workers=workers,
            usar_proxies=bool(rotar_proxies),
            pais_proxy=pais_proxy,
            guardar_identidades=bool(guardar_identidades),
            headless=headless,
            cuentas=seleccion if es_cuentas else None,
            esperar_captcha_seg=int(esperar_captcha_seg),
            resolver_captcha=resolver_captcha,
        )


def render(usuario: dict):
    """Pagina "✍️ Change.org: Reportes": reportes, registro y firmas."""
    cabecera(
        "✍️ CHANGE.ORG: REPORTES, REGISTRO Y FIRMAS",
        "Reportes masivos, registro de cuentas y firmas de peticiones",
    )

    # Panel persistente: SIEMPRE al inicio, para que el avance/log siga visible
    # aunque el usuario recargue o navegue a otra operacion durante la campana.
    _render_proceso_activo()

    cuentas = _cargar_cuentas_change()

    pestana_reportes, pestana_registros, pestana_firmas = st.tabs(
        ["🚩 Ataque de reportes", "🧾 Cuentas Change.org", "✍️ Firmar peticiones"]
    )
    with pestana_reportes:
        _formulario_reportes(cuentas)
    with pestana_registros:
        _formulario_registros(cuentas)
    with pestana_firmas:
        _formulario_firmas(cuentas)

    _visor_granja()

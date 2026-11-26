# -*- coding: utf-8 -*-
"""Autoactualizador del ejecutable MWX APP (sin dependencias nuevas).

El cliente consulta el release publicado en la API y, si hay una version mas
nueva, descarga el ZIP, lo extrae a una carpeta temporal y deja un script que
reemplaza los archivos cuando el proceso actual termina: un ``.bat`` en
Windows y un ``.sh`` en macOS.

Contrato CONGELADO (todas las funciones son defensivas y NUNCA lanzan):

    version_actual() -> str
    comparar_versiones(a, b) -> int          # -1 / 0 / 1
    puede_actualizarse() -> bool             # ejecutable congelado Win/macOS
    consultar(api_url, version_actual="", timeout=6, plataforma="") -> dict
    descargar(api_url, version, destino="", sha256="", progreso=None,
              cancelar=None, plataforma="") -> dict
    preparar(ruta_zip, app_dir="", staging_dir="") -> dict
    lanzar(preparado) -> dict
    actualizar(api_url, version="", progreso=None) -> dict

Los diccionarios devueltos siempre incluyen TODAS sus claves, aunque la
operacion falle. Nunca se ejecuta el script desde aqui: solo ``lanzar`` lo
inicia en segundo plano.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

import requests

from mwx_app import config

__all__ = [
    "version_actual",
    "comparar_versiones",
    "puede_actualizarse",
    "consultar",
    "descargar",
    "preparar",
    "lanzar",
    "actualizar",
]

_CHUNK = 256 * 1024
_TIMEOUT_DESCARGA = (10, 120)
_NOMBRE_EXE = "mwx app.exe"
_NOMBRE_APP_MAC = "mwx app.app"


# ---------------------------------------------------------------------------
# Utilidades tolerantes
# ---------------------------------------------------------------------------
def _es_macos() -> bool:
    """True si el proceso corre en macOS (nunca lanza)."""
    try:
        return sys.platform == "darwin"
    except Exception:
        return False


def _plataforma_actual() -> str:
    """Plataforma del ejecutable actual (``""`` si no esta soportada).

    Devuelve ``windows`` en Windows y, en macOS, ``mac-arm64`` o ``mac-intel``
    segun la arquitectura. Nunca lanza.
    """
    try:
        if os.name == "nt":
            return "windows"
        if sys.platform != "darwin":
            return ""
        maquina = str(platform.machine() or "").lower()
        if maquina in ("arm64", "aarch64"):
            return "mac-arm64"
        if maquina in ("x86_64", "amd64"):
            return "mac-intel"
        return ""
    except Exception:
        return ""


def _texto(valor) -> str:
    """Convierte a texto recortado; "" para None o errores."""
    try:
        if valor is None:
            return ""
        if isinstance(valor, (bytes, bytearray)):
            valor = valor.decode("utf-8", "ignore")
        return str(valor).strip()
    except Exception:
        return ""


def _entero(valor, defecto: int = 0) -> int:
    """Entero seguro (bool incluido) con valor por defecto."""
    try:
        if valor is None or valor == "":
            return defecto
        return int(valor)
    except (TypeError, ValueError, OverflowError):
        return defecto


def _timeout(valor, defecto: float) -> float:
    """Timeout positivo (admite tuplas de requests sin tocarlas)."""
    if isinstance(valor, (tuple, list)):
        return valor
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return defecto
    return numero if numero > 0 else defecto


def _base_url(api_url) -> str:
    """URL base normalizada (sin /v1 ni barra final); "" si falta."""
    texto = _texto(api_url)
    if not texto:
        return ""
    try:
        return config.normalizar_url_api(texto)
    except Exception:
        return texto.rstrip("/")


def _slug_version(valor) -> str:
    """Nombre de archivo seguro a partir de una version."""
    texto = _texto(valor)
    limpio = re.sub(r"[^A-Za-z0-9._-]+", "_", texto).strip("._-")
    return limpio[:60] or "latest"


def _cancelado(cancelar) -> bool:
    """True si el evento de cancelacion esta activo (nunca lanza)."""
    try:
        if cancelar is None:
            return False
        if isinstance(cancelar, bool):
            return cancelar
        metodo = getattr(cancelar, "is_set", None)
        if callable(metodo):
            return bool(metodo())
        if callable(cancelar):
            return bool(cancelar())
        return False
    except Exception:
        return False


def _avisar(progreso, fraccion, mensaje: str) -> None:
    """Invoca el callback de progreso sin romper la descarga."""
    if not callable(progreso):
        return
    try:
        valor = min(1.0, max(0.0, float(fraccion)))
    except (TypeError, ValueError):
        valor = 0.0
    try:
        progreso(valor, str(mensaje or ""))
    except Exception:
        pass


def _cabeceras(version: str, plataforma: str = "") -> dict:
    """Headers del cliente (UA con la version y la plataforma actuales)."""
    actual = _texto(version) or version_actual() or "0.0.0"
    tipo = (_texto(plataforma) or _plataforma_actual()).lower()
    etiqueta = "macOS" if tipo.startswith("mac") else "Windows"
    return {
        "Accept": "application/json",
        "User-Agent": f"MWX-APP/{actual} ({etiqueta})",
    }


def _borrar(ruta) -> None:
    """Elimina un archivo parcial/temporal (best-effort)."""
    try:
        Path(ruta).unlink(missing_ok=True)
    except Exception:
        pass


def _version_de_zip(nombre: str) -> str:
    """Version a partir de ``MWX-APP-<version>.zip`` (o del stem)."""
    texto = _texto(nombre)
    if texto.lower().endswith(".zip"):
        texto = texto[:-4]
    if texto.lower().startswith("mwx-app-"):
        texto = texto[len("mwx-app-") :]
    return _slug_version(texto)


# ---------------------------------------------------------------------------
# Version y comparacion
# ---------------------------------------------------------------------------
def version_actual() -> str:
    """Version del ejecutable (``mwx_app.__version__``); ``0.0.0`` si falla."""
    try:
        from mwx_app import __version__

        return _texto(__version__) or "0.0.0"
    except Exception:
        return "0.0.0"


def _version_del_paquete() -> str:
    """Alias interno para no chocar con el parametro homonimo de `consultar`."""
    return version_actual()


def comparar_versiones(a, b) -> int:
    """Compara dos versiones numericas: -1 (a<b), 0 (igual) o 1 (a>b).

    Tolera prefijos ``v``, sufijos (``-beta``), segmentos faltantes y texto
    no numerico: ``1.10.0`` es mayor que ``1.9.0``.
    """
    try:
        def partes(valor):
            texto = _texto(valor)
            numeros = [int(numero) for numero in re.findall(r"\d+", texto)]
            while len(numeros) > 1 and numeros[-1] == 0:
                numeros.pop()
            return numeros or [0]

        izquierda = partes(a)
        derecha = partes(b)
        largo = max(len(izquierda), len(derecha))
        izquierda += [0] * (largo - len(izquierda))
        derecha += [0] * (largo - len(derecha))
        if izquierda < derecha:
            return -1
        if izquierda > derecha:
            return 1
        return 0
    except Exception:
        return 0


def _version_base_plataforma(valor) -> str:
    """Quita el sufijo de plataforma para comparar versiones.

    Los releases Mac se publican como ``1.1.4-mac-arm64``/``1.1.4-mac-intel``
    mientras la app reporta ``1.1.4``; sin esto el cliente creeria que su
    propia version es mas vieja y se reinstalaria en bucle. Nunca lanza.
    """
    texto = _texto(valor)
    if not texto:
        return ""
    try:
        limpio = re.sub(
            r"[-_ ]+mac(?:[-_ ]*(?:arm64|intel))?$",
            "",
            texto,
            flags=re.IGNORECASE,
        ).strip("-_ .")
        return limpio or texto
    except Exception:
        return texto


def puede_actualizarse() -> bool:
    """True en el ejecutable congelado de Windows o de macOS.

    En macOS ya existe autoactualizador (ZIP por arquitectura + script
    ``.sh``); en desarrollo y en plataformas no soportadas el gate queda
    cerrado.
    """
    try:
        if not getattr(sys, "frozen", False):
            return False
        return _plataforma_actual() in ("windows", "mac-arm64", "mac-intel")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Consulta del release
# ---------------------------------------------------------------------------
def _resultado_consulta() -> dict:
    return {
        "ok": False,
        "disponible": False,
        "requerida": False,
        "obligatoria": False,
        "version": "",
        "notas": "",
        "sha256": "",
        "tamano": 0,
        "url_descarga": "",
        "error": "",
    }


def consultar(
    api_url, version_actual: str = "", timeout=6, plataforma: str = ""
) -> dict:
    """Pregunta a la API por el ultimo release (sin descargar el contenido).

    ``requerida`` solo es True si el release existe y su version es mayor que
    la actual. Sin red devuelve ``ok=False`` y ``requerida=False`` para no
    bloquear el arranque. La consulta incluye ``plataforma`` (windows,
    mac-arm64 o mac-intel) para recibir el ZIP correcto.
    """
    resultado = _resultado_consulta()
    base = _base_url(api_url)
    actual = _texto(version_actual) or _version_del_paquete()
    explicita = _texto(plataforma)
    plataforma_txt = explicita or _plataforma_actual()
    con_plataforma = bool(
        plataforma_txt and (explicita or plataforma_txt != "windows")
    )
    if not base:
        resultado["error"] = "falta configurar la direccion del servidor"
        return resultado

    consulta = f"{base}/v1/app/version"
    if con_plataforma:
        consulta = f"{consulta}?plataforma={quote(plataforma_txt, safe='')}"

    respuesta = None
    try:
        respuesta = requests.get(
            consulta,
            headers=_cabeceras(actual, plataforma_txt),
            timeout=_timeout(timeout, 6),
        )
        codigo = _entero(getattr(respuesta, "status_code", 0), 0)
        if codigo and (codigo < 200 or codigo >= 300):
            resultado["error"] = f"el servidor respondio {codigo}"
            return resultado

        try:
            datos = respuesta.json()
        except (ValueError, TypeError):
            resultado["error"] = "respuesta invalida del servidor"
            return resultado
        if not isinstance(datos, dict):
            resultado["error"] = "respuesta invalida del servidor"
            return resultado
        if datos.get("ok") is False:
            resultado["error"] = _texto(datos.get("error")) or (
                "no se pudo consultar la actualizacion"
            )
            return resultado

        disponible = bool(datos.get("disponible"))
        remota = _texto(datos.get("version"))
        resultado["disponible"] = disponible
        resultado["version"] = remota if disponible else ""
        resultado["notas"] = _texto(datos.get("notas")) if disponible else ""
        resultado["sha256"] = _texto(datos.get("sha256")) if disponible else ""
        resultado["tamano"] = (
            _entero(datos.get("tamano"), 0) if disponible else 0
        )
        resultado["requerida"] = bool(
            disponible
            and remota
            and comparar_versiones(
                _version_base_plataforma(remota),
                _version_base_plataforma(actual),
            )
            > 0
        )
        resultado["obligatoria"] = bool(
            disponible and datos.get("obligatorio")
        )
        if disponible and remota:
            descarga = (
                f"{base}/v1/app/descargar?version={quote(remota, safe='')}"
            )
            if con_plataforma:
                descarga = (
                    f"{descarga}&plataforma={quote(plataforma_txt, safe='')}"
                )
            resultado["url_descarga"] = descarga
        resultado["ok"] = True
        return resultado
    except requests.Timeout:
        resultado["error"] = "el servidor tardo demasiado en responder"
        return resultado
    except requests.RequestException:
        resultado["error"] = "no hay conexion con el servidor"
        return resultado
    except Exception:
        resultado["error"] = "no se pudo consultar la actualizacion"
        return resultado
    finally:
        if respuesta is not None:
            cerrar = getattr(respuesta, "close", None)
            if callable(cerrar):
                try:
                    cerrar()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Descarga del ZIP
# ---------------------------------------------------------------------------
def _ruta_descarga(destino, version: str) -> Path:
    """Ruta final del ZIP (destino explicito o DATA_DIR/updates)."""
    texto = _texto(destino)
    if texto:
        return Path(texto).expanduser()
    try:
        base = config.dir_datos()
    except Exception:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MWX APP"
    return Path(base) / "updates" / f"MWX-APP-{_slug_version(version)}.zip"


def descargar(
    api_url,
    version,
    destino="",
    sha256="",
    progreso=None,
    cancelar=None,
    plataforma="",
) -> dict:
    """Descarga el ZIP del release a ``destino`` y verifica sha256/tamano.

    ``progreso(frac, texto)`` se invoca durante la descarga; ``cancelar`` es un
    ``threading.Event`` opcional. ``plataforma`` viaja en los parametros para
    recibir el ZIP de Windows o el de macOS. Nunca lanza.
    """
    resultado = {"ok": False, "ruta": "", "bytes": 0, "error": ""}
    base = _base_url(api_url)
    if not base:
        resultado["error"] = "falta configurar la direccion del servidor"
        return resultado
    if _cancelado(cancelar):
        resultado["error"] = "descarga cancelada"
        return resultado

    version_txt = _texto(version)
    explicita = _texto(plataforma)
    plataforma_txt = explicita or _plataforma_actual()
    parametros = {"version": version_txt}
    if plataforma_txt and (explicita or plataforma_txt != "windows"):
        parametros["plataforma"] = plataforma_txt
    ruta = _ruta_descarga(destino, version_txt)
    respuesta = None
    try:
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            resultado["error"] = "no se pudo crear la carpeta de actualizaciones"
            return resultado

        respuesta = requests.get(
            f"{base}/v1/app/descargar",
            params=parametros,
            headers=_cabeceras(version_txt, plataforma_txt),
            stream=True,
            timeout=_TIMEOUT_DESCARGA,
        )
        codigo = _entero(getattr(respuesta, "status_code", 0), 0)
        if codigo == 404:
            resultado["error"] = "actualizacion no disponible"
            return resultado
        if codigo and (codigo < 200 or codigo >= 300):
            resultado["error"] = f"no se pudo descargar la actualizacion ({codigo})"
            return resultado

        cabeceras = getattr(respuesta, "headers", {}) or {}
        try:
            total = _entero(cabeceras.get("Content-Length"), 0)
        except AttributeError:
            total = 0
        if not total:
            total = _entero(cabeceras.get("content-length"), 0)

        leidos = 0
        digest = hashlib.sha256()
        cancelada = False
        _avisar(progreso, 0.0, "Descargando la actualizacion...")

        iterador = getattr(respuesta, "iter_content", None)
        if callable(iterador):
            try:
                trozos = iterador(chunk_size=_CHUNK)
            except TypeError:
                trozos = iterador(_CHUNK)
        else:
            trozos = [getattr(respuesta, "content", b"") or b""]

        with open(ruta, "wb") as archivo:
            for trozo in trozos:
                if _cancelado(cancelar):
                    cancelada = True
                    break
                if not trozo:
                    continue
                if not isinstance(trozo, (bytes, bytearray)):
                    trozo = bytes(trozo)
                archivo.write(trozo)
                digest.update(trozo)
                leidos += len(trozo)
                fraccion = (leidos / total) if total > 0 else 0.0
                if total > 0:
                    texto = f"{leidos} de {total} bytes"
                else:
                    texto = f"{leidos} bytes"
                _avisar(progreso, fraccion, texto)

        if cancelada or _cancelado(cancelar):
            _borrar(ruta)
            resultado["error"] = "descarga cancelada"
            return resultado
        if leidos <= 0:
            _borrar(ruta)
            resultado["error"] = "la descarga llego vacia"
            return resultado
        if total > 0 and leidos != total:
            _borrar(ruta)
            resultado["error"] = (
                f"la descarga quedo incompleta ({leidos} de {total} bytes)"
            )
            return resultado

        esperado = _texto(sha256).lower()
        calculado = digest.hexdigest()
        if esperado and calculado != esperado:
            _borrar(ruta)
            resultado["error"] = (
                "el archivo descargado no coincide con la actualizacion (sha256)"
            )
            return resultado

        _avisar(progreso, 1.0, "Descarga completa")
        resultado["ok"] = True
        resultado["ruta"] = str(ruta)
        resultado["bytes"] = leidos
        return resultado
    except requests.Timeout:
        _borrar(ruta)
        resultado["error"] = "el servidor tardo demasiado en responder"
        return resultado
    except requests.RequestException:
        _borrar(ruta)
        resultado["error"] = "no hay conexion con el servidor"
        return resultado
    except Exception as error:
        _borrar(ruta)
        resultado["error"] = f"no se pudo descargar la actualizacion: {error}"
        return resultado
    finally:
        if respuesta is not None:
            cerrar = getattr(respuesta, "close", None)
            if callable(cerrar):
                try:
                    cerrar()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Extraccion y scripts de actualizacion
# ---------------------------------------------------------------------------
def _app_dir_por_defecto() -> Path:
    """Carpeta del ejecutable actual (en macOS, la raiz del ``.app``)."""
    try:
        ejecutable = Path(sys.executable).resolve()
        if _es_macos():
            return ejecutable.parent.parent.parent
        return ejecutable.parent
    except Exception:
        return Path.cwd()


def _staging_por_defecto(version: str) -> Path:
    """Carpeta temporal ``DATA_DIR/updates/<version>``."""
    try:
        base = config.dir_datos()
    except Exception:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MWX APP"
    return Path(base) / "updates" / _slug_version(version)


def _miembro_seguro(nombre: str) -> str:
    """Normaliza una ruta del ZIP o devuelve "" si intenta salir de staging."""
    texto = _texto(nombre).replace("\\", "/")
    if not texto or texto.startswith("/"):
        return ""
    if re.match(r"^[A-Za-z]:", texto):
        return ""
    partes = [parte for parte in texto.split("/") if parte not in ("", ".")]
    if not partes or any(parte == ".." for parte in partes):
        return ""
    return "/".join(partes)


def _buscar_archivo(staging: Path, nombre: str) -> Path:
    """Primer archivo cuyo nombre coincide (case-insensitive); None si no hay."""
    try:
        for candidato in sorted(staging.rglob("*"), key=lambda p: str(p).lower()):
            try:
                if candidato.is_file() and candidato.name.lower() == nombre:
                    return candidato.resolve()
            except OSError:
                continue
    except OSError:
        pass
    return None


def _buscar_app_mac(staging: Path):
    """Primer directorio ``MWX APP.app`` (case-insensitive); None si no hay."""
    try:
        for candidato in sorted(staging.rglob("*"), key=lambda p: str(p).lower()):
            try:
                if candidato.is_dir() and candidato.name.lower() == _NOMBRE_APP_MAC:
                    return candidato.resolve()
            except OSError:
                continue
    except OSError:
        pass
    return None


def _extraer_zip(ruta: Path, staging: Path) -> str:
    """Extrae el ZIP validando traversal; devuelve "" o el mensaje de error."""
    try:
        with zipfile.ZipFile(str(ruta)) as zf:
            for info in zf.infolist():
                seguro = _miembro_seguro(info.filename)
                if not seguro:
                    return (
                        "el ZIP contiene una ruta no permitida "
                        f"({info.filename})"
                    )
                destino = staging.joinpath(*seguro.split("/")).resolve()
                try:
                    destino.relative_to(staging)
                except ValueError:
                    return (
                        "el ZIP contiene una ruta fuera de la carpeta temporal "
                        f"({info.filename})"
                    )
                if info.is_dir() or info.filename.endswith(("/", "\\")):
                    destino.mkdir(parents=True, exist_ok=True)
                    continue
                destino.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as origen_zip, open(destino, "wb") as salida:
                    shutil.copyfileobj(origen_zip, salida, _CHUNK)
    except (zipfile.BadZipFile, OSError, RuntimeError) as error:
        return f"no se pudo extraer la actualizacion: {error}"
    return ""


def _bat_escape(valor) -> str:
    """Escapa ``%`` para que cmd no expanda variables dentro de una ruta."""
    return str(valor).replace("%", "%%")


def _contenido_bat(source: Path, app_dir: Path, staging: Path) -> str:
    """Genera el .bat que espera, reemplaza y relanza la aplicacion."""
    origen = _bat_escape(source)
    destino = _bat_escape(app_dir)
    temporal = _bat_escape(staging)
    ejecutable = f"{destino}\\MWX APP.exe"
    pid_actual = int(os.getpid())
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal EnableExtensions\r\n"
        f'set "MWX_PID={pid_actual}"\r\n'
        "set /a MWX_INTENTOS=0\r\n"
        ":MWX_ESPERAR\r\n"
        'tasklist /FI "PID eq %MWX_PID%" 2>nul | find /I "MWX APP.exe" >nul\r\n'
        "if errorlevel 1 goto MWX_COPIAR\r\n"
        "set /a MWX_INTENTOS+=1\r\n"
        "if %MWX_INTENTOS% GEQ 60 goto MWX_COPIAR\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        "goto MWX_ESPERAR\r\n"
        ":MWX_COPIAR\r\n"
        f'robocopy "{origen}" "{destino}" /E /IS /IT /R:5 /W:2 '
        "/XF config.json /NFL /NDL /NJH /NJS /NP\r\n"
        'set "MWX_RC=%ERRORLEVEL%"\r\n'
        "if %MWX_RC% GEQ 8 goto MWX_ERROR\r\n"
        f'start "" "{ejecutable}"\r\n'
        f'rmdir /S /Q "{temporal}" >nul 2>nul\r\n'
        'del "%~f0" >nul 2>nul\r\n'
        "endlocal\r\n"
        "exit /b 0\r\n"
        ":MWX_ERROR\r\n"
        "echo [%DATE% %TIME%] La actualizacion fallo. robocopy=%MWX_RC%.>>"
        '"%TEMP%\\mwx_update.log"\r\n'
        "echo Abre MWX APP.exe manualmente.>>"
        '"%TEMP%\\mwx_update.log"\r\n'
        "endlocal\r\n"
        "exit /b 1\r\n"
    )


def _contenido_sh() -> str:
    """Genera el ``.sh`` que espera, reemplaza y relanza el ``.app`` en macOS.

    Recibe ``PID``, ``ORIGEN`` y ``DESTINO`` como argumentos. Si la carpeta del
    destino no es escribible (p. ej. ``/Applications`` sin permisos) instala en
    ``$HOME/Applications``. Nunca borra la app actual sin respaldo.
    """
    return (
        "#!/bin/sh\n"
        "# Actualizador de MWX APP para macOS.\n"
        "set -e\n"
        "\n"
        'PID="$1"\n'
        'ORIGEN="$2"\n'
        'DESTINO="$3"\n'
        'LOG_DIR="$HOME/Library/Logs/MWX APP"\n'
        'LOG="$LOG_DIR/actualizacion.log"\n'
        'FALLBACK="$HOME/Applications/MWX APP.app"\n'
        "\n"
        'mkdir -p "$LOG_DIR" 2>/dev/null || true\n'
        "registrar() {\n"
        '    printf \'[%s] %s\\n\' "$(date \'+%Y-%m-%d %H:%M:%S\')" "$1" '
        '>>"$LOG" 2>/dev/null || true\n'
        "}\n"
        "\n"
        'if [ -z "$ORIGEN" ] || [ ! -d "$ORIGEN" ]; then\n'
        '    registrar "No se encontro la aplicacion nueva; se conserva la '
        'version actual."\n'
        '    if [ -n "$DESTINO" ] && [ -d "$DESTINO" ]; then\n'
        '        open "$DESTINO" 2>/dev/null || true\n'
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        "\n"
        "ESPERA=120\n"
        'while [ "$ESPERA" -gt 0 ]; do\n'
        '    if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then\n'
        "        break\n"
        "    fi\n"
        "    sleep 1\n"
        "    ESPERA=$((ESPERA - 1))\n"
        "done\n"
        "\n"
        'if [ -n "$DESTINO" ] && [ -w "$(dirname "$DESTINO")" ]; then\n'
        '    FINAL="$DESTINO"\n'
        "else\n"
        '    mkdir -p "$HOME/Applications" 2>/dev/null || true\n'
        '    FINAL="$FALLBACK"\n'
        "fi\n"
        "\n"
        'if [ ! -w "$(dirname "$FINAL")" ]; then\n'
        '    registrar "No hay una carpeta escribible para instalar; se abre '
        'la version actual."\n'
        '    if [ -n "$DESTINO" ] && [ -d "$DESTINO" ]; then\n'
        '        open "$DESTINO" 2>/dev/null || true\n'
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        "\n"
        'TEMP_NUEVO="${FINAL}.nuevo.$$"\n'
        'BACKUP="${FINAL}.backup.$$"\n'
        "\n"
        'if ! ditto "$ORIGEN" "$TEMP_NUEVO" 2>>"$LOG"; then\n'
        '    registrar "Fallo la copia de la version nueva; se abre la version '
        'actual."\n'
        '    rm -rf "$TEMP_NUEVO" 2>/dev/null || true\n'
        '    if [ -n "$DESTINO" ] && [ -d "$DESTINO" ]; then\n'
        '        open "$DESTINO" 2>/dev/null || true\n'
        "    fi\n"
        "    exit 1\n"
        "fi\n"
        "\n"
        'if [ -e "$FINAL" ]; then\n'
        '    if ! mv "$FINAL" "$BACKUP" 2>>"$LOG"; then\n'
        '        registrar "Fallo el respaldo de la version actual; se conserva '
        'la actual."\n'
        '        rm -rf "$TEMP_NUEVO" 2>/dev/null || true\n'
        '        open "$FINAL" 2>/dev/null || true\n'
        "        exit 1\n"
        "    fi\n"
        "fi\n"
        "\n"
        'if ! mv "$TEMP_NUEVO" "$FINAL" 2>>"$LOG"; then\n'
        '    registrar "Fallo la instalacion; se restaura la version actual."\n'
        '    rm -rf "$FINAL" 2>/dev/null || true\n'
        '    if [ -d "$BACKUP" ]; then\n'
        '        mv "$BACKUP" "$FINAL" 2>>"$LOG" || true\n'
        "    fi\n"
        '    open "$FINAL" 2>/dev/null || true\n'
        "    exit 1\n"
        "fi\n"
        "\n"
        'xattr -dr com.apple.quarantine "$FINAL" 2>/dev/null || true\n'
        'if [ -d "$BACKUP" ]; then\n'
        '    rm -rf "$BACKUP" 2>/dev/null || true\n'
        "fi\n"
        'if [ "$FINAL" != "$DESTINO" ]; then\n'
        '    registrar "La carpeta original no era escribible; se instalo en '
        '$FINAL."\n'
        "fi\n"
        'registrar "Actualizacion instalada en $FINAL."\n'
        'if [ "$FINAL" = "$DESTINO" ]; then\n'
        '    open "$DESTINO" 2>/dev/null || true\n'
        "else\n"
        '    open "$FINAL" 2>/dev/null || true\n'
        "fi\n"
        "exit 0\n"
    )


def _resultado_preparar() -> dict:
    return {
        "ok": False,
        "staging": "",
        "source": "",
        "bat": "",
        "app_dir": "",
        "error": "",
    }


def preparar(ruta_zip, app_dir="", staging_dir="") -> dict:
    """Extrae el ZIP, valida traversal y deja listo el script de actualizacion.

    En Windows busca ``MWX APP.exe`` en cualquier subcarpeta extraida y deja un
    ``.bat``. En macOS busca el directorio ``MWX APP.app`` (case-insensitive) y
    deja un ``actualizar_mwx.sh``. ``app_dir`` por defecto es la carpeta del
    ejecutable (en macOS, la raiz del ``.app``) y ``staging_dir`` por defecto
    ``DATA_DIR/updates/<version>``.
    """
    resultado = _resultado_preparar()
    try:
        ruta = Path(_texto(ruta_zip)).expanduser()
        if not _texto(ruta_zip) or not ruta.is_file():
            resultado["error"] = "no existe el archivo de actualizacion"
            return resultado
        if not zipfile.is_zipfile(str(ruta)):
            resultado["error"] = "el archivo de actualizacion no es un ZIP valido"
            return resultado

        if _texto(staging_dir):
            staging = Path(_texto(staging_dir)).expanduser()
        else:
            staging = _staging_por_defecto(_version_de_zip(ruta.name))
        staging = staging.resolve()

        destino_app = (
            Path(_texto(app_dir)).expanduser()
            if _texto(app_dir)
            else _app_dir_por_defecto()
        )
        destino_app = destino_app.resolve()

        try:
            if staging.exists():
                if staging.is_dir():
                    shutil.rmtree(staging, ignore_errors=True)
                else:
                    staging.unlink(missing_ok=True)
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            resultado["error"] = f"no se pudo preparar la carpeta temporal: {error}"
            return resultado

        error_zip = _extraer_zip(ruta, staging)
        if error_zip:
            resultado["error"] = error_zip
            return resultado

        if _es_macos():
            source = _buscar_app_mac(staging)
            if source is None:
                if _buscar_archivo(staging, _NOMBRE_EXE) is not None:
                    resultado["error"] = (
                        "la actualizacion es para Windows (MWX APP.exe); en "
                        "macOS se esperaba el directorio 'MWX APP.app'"
                    )
                else:
                    resultado["error"] = (
                        "no se encontro 'MWX APP.app' dentro de la actualizacion"
                    )
                return resultado

            guion = staging.parent / "actualizar_mwx.sh"
            try:
                guion.write_bytes(_contenido_sh().encode("utf-8"))
            except OSError as error:
                resultado["error"] = (
                    f"no se pudo escribir el actualizador: {error}"
                )
                return resultado

            resultado["ok"] = True
            resultado["staging"] = str(staging)
            resultado["source"] = str(source)
            resultado["bat"] = str(guion)
            resultado["app_dir"] = str(destino_app)
            return resultado

        ejecutable = _buscar_archivo(staging, _NOMBRE_EXE)
        if ejecutable is None:
            resultado["error"] = (
                "no se encontro 'MWX APP.exe' dentro de la actualizacion"
            )
            return resultado

        source = ejecutable.parent.resolve()
        bat = staging.parent / "actualizar_mwx.bat"
        try:
            bat.write_bytes(
                _contenido_bat(source, destino_app, staging).encode("utf-8")
            )
        except OSError as error:
            resultado["error"] = f"no se pudo escribir el actualizador: {error}"
            return resultado

        resultado["ok"] = True
        resultado["staging"] = str(staging)
        resultado["source"] = str(source)
        resultado["bat"] = str(bat)
        resultado["app_dir"] = str(destino_app)
        return resultado
    except Exception as error:
        resultado["error"] = f"no se pudo preparar la actualizacion: {error}"
        return resultado


def _origen_mac(preparado: dict) -> str:
    """Carpeta del ``.app`` nuevo (``source`` o lo que haya en ``staging``)."""
    origen = _texto(preparado.get("source"))
    if origen and Path(origen).is_dir():
        return origen
    staging = _texto(preparado.get("staging"))
    if staging and Path(staging).is_dir():
        app = _buscar_app_mac(Path(staging))
        if app is not None:
            return str(app)
    return ""


def lanzar(preparado) -> dict:
    """Inicia el actualizador sin consola visible y vuelve enseguida.

    Windows: ``cmd /c <archivo>.bat`` sin ventana. macOS: ``/bin/sh`` en una
    sesion nueva y desacoplada, pasando PID, ORIGEN y DESTINO.
    """
    try:
        if not isinstance(preparado, dict):
            return {"ok": False, "error": "datos de actualizacion invalidos"}
        if preparado.get("ok") is False:
            return {"ok": False, "error": "la actualizacion no esta lista"}
        bat = _texto(preparado.get("bat"))
        if not bat or not Path(bat).is_file():
            return {"ok": False, "error": "no se encontro el actualizador"}

        if _es_macos():
            origen = _origen_mac(preparado)
            if not origen:
                return {
                    "ok": False,
                    "error": "no se encontro la aplicacion nueva",
                }
            destino = _texto(preparado.get("app_dir")) or str(
                _app_dir_por_defecto()
            )
            subprocess.Popen(
                ["/bin/sh", bat, str(os.getpid()), origen, destino],
                cwd=str(Path(bat).parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            return {"ok": True, "error": ""}

        banderas = 0
        if os.name == "nt":
            # CREATE_NO_WINDOW oculta la consola del .bat; el proceso queda
            # desacoplado del arbol de la app para poder reemplazar el .exe.
            banderas = int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            ) | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        comando = ["cmd.exe", "/c", bat] if os.name == "nt" else [bat]
        subprocess.Popen(
            comando,
            cwd=str(Path(bat).parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=banderas,
            close_fds=True,
        )
        return {"ok": True, "error": ""}
    except Exception as error:
        return {"ok": False, "error": f"no se pudo iniciar el actualizador: {error}"}


def actualizar(api_url, version="", progreso=None) -> dict:
    """Consulta, descarga y prepara el release (no lo lanza). Nunca lanza."""
    resultado = {
        "ok": False,
        "version": "",
        "bat": "",
        "staging": "",
        "app_dir": "",
        "error": "",
    }
    try:
        info = consultar(api_url)
        if not info.get("ok"):
            resultado["error"] = info.get("error") or (
                "no se pudo consultar la actualizacion"
            )
            return resultado

        pedida = _texto(version)
        if pedida:
            version_txt = pedida
        elif info.get("disponible") and info.get("requerida"):
            version_txt = _texto(info.get("version"))
        elif info.get("disponible"):
            resultado["error"] = "ya tienes la ultima version"
            return resultado
        else:
            resultado["error"] = "no hay actualizacion disponible"
            return resultado

        if not version_txt:
            resultado["error"] = "no hay actualizacion disponible"
            return resultado
        resultado["version"] = version_txt

        sha_esperado = ""
        if not pedida or version_txt == _texto(info.get("version")):
            sha_esperado = _texto(info.get("sha256"))

        descarga = descargar(
            api_url,
            version_txt,
            sha256=sha_esperado,
            progreso=progreso,
        )
        if not descarga.get("ok"):
            resultado["error"] = descarga.get("error") or (
                "no se pudo descargar la actualizacion"
            )
            return resultado

        preparado = preparar(descarga.get("ruta", ""))
        if not preparado.get("ok"):
            resultado["error"] = preparado.get("error") or (
                "no se pudo preparar la actualizacion"
            )
            return resultado

        resultado["ok"] = True
        resultado["version"] = version_txt
        resultado["bat"] = preparado.get("bat", "")
        resultado["staging"] = preparado.get("staging", "")
        resultado["app_dir"] = preparado.get("app_dir", "")
        return resultado
    except Exception as error:
        resultado["error"] = f"no se pudo actualizar: {error}"
        return resultado

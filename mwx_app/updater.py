# -*- coding: utf-8 -*-
"""Autoactualizador del ejecutable MWX APP (sin dependencias nuevas).

El cliente consulta el release publicado en la API y, si hay una version mas
nueva, descarga el ZIP, lo extrae a una carpeta temporal y deja un ``.bat`` que
reemplaza los archivos cuando el proceso actual termina.

Contrato CONGELADO (todas las funciones son defensivas y NUNCA lanzan):

    version_actual() -> str
    comparar_versiones(a, b) -> int          # -1 / 0 / 1
    puede_actualizarse() -> bool             # solo en el ejecutable congelado
    consultar(api_url, version_actual="", timeout=6) -> dict
    descargar(api_url, version, destino="", sha256="", progreso=None,
              cancelar=None) -> dict
    preparar(ruta_zip, app_dir="", staging_dir="") -> dict
    lanzar(preparado) -> dict
    actualizar(api_url, version="", progreso=None) -> dict

Los diccionarios devueltos siempre incluyen TODAS sus claves, aunque la
operacion falle. Nunca se ejecuta el ``.bat`` desde aqui: solo ``lanzar`` lo
inicia en segundo plano.
"""
from __future__ import annotations

import hashlib
import os
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


# ---------------------------------------------------------------------------
# Utilidades tolerantes
# ---------------------------------------------------------------------------
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


def _cabeceras(version: str) -> dict:
    """Headers del cliente (UA con la version actual)."""
    actual = _texto(version) or version_actual() or "0.0.0"
    return {
        "Accept": "application/json",
        "User-Agent": f"MWX-APP/{actual} (Windows)",
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


def puede_actualizarse() -> bool:
    """True solo dentro del ejecutable congelado (en desarrollo no aplica)."""
    try:
        return bool(getattr(sys, "frozen", False))
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


def consultar(api_url, version_actual: str = "", timeout=6) -> dict:
    """Pregunta a la API por el ultimo release (sin descargar el contenido).

    ``requerida`` solo es True si el release existe y su version es mayor que
    la actual. Sin red devuelve ``ok=False`` y ``requerida=False`` para no
    bloquear el arranque.
    """
    resultado = _resultado_consulta()
    base = _base_url(api_url)
    actual = _texto(version_actual) or _version_del_paquete()
    if not base:
        resultado["error"] = "falta configurar la direccion del servidor"
        return resultado

    respuesta = None
    try:
        respuesta = requests.get(
            f"{base}/v1/app/version",
            headers=_cabeceras(actual),
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
            and comparar_versiones(remota, actual) > 0
        )
        resultado["obligatoria"] = bool(
            disponible and datos.get("obligatorio")
        )
        if disponible and remota:
            resultado["url_descarga"] = (
                f"{base}/v1/app/descargar?version={quote(remota, safe='')}"
            )
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
) -> dict:
    """Descarga el ZIP del release a ``destino`` y verifica sha256/tamano.

    ``progreso(frac, texto)`` se invoca durante la descarga; ``cancelar`` es un
    ``threading.Event`` opcional. Nunca lanza.
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
            params={"version": version_txt},
            headers=_cabeceras(version_txt),
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
# Extraccion y .bat de actualizacion
# ---------------------------------------------------------------------------
def _app_dir_por_defecto() -> Path:
    """Carpeta del ejecutable actual (o el CWD si no se puede resolver)."""
    try:
        return Path(sys.executable).resolve().parent
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
    """Extrae el ZIP, valida traversal y deja listo el ``.bat``.

    Busca ``MWX APP.exe`` en cualquier subcarpeta extraida; ``source`` es su
    carpeta padre. ``app_dir`` por defecto es la carpeta del ejecutable y
    ``staging_dir`` por defecto ``DATA_DIR/updates/<version>``.
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

        try:
            with zipfile.ZipFile(str(ruta)) as zf:
                for info in zf.infolist():
                    seguro = _miembro_seguro(info.filename)
                    if not seguro:
                        resultado["error"] = (
                            "el ZIP contiene una ruta no permitida "
                            f"({info.filename})"
                        )
                        return resultado
                    destino = staging.joinpath(*seguro.split("/")).resolve()
                    try:
                        destino.relative_to(staging)
                    except ValueError:
                        resultado["error"] = (
                            "el ZIP contiene una ruta fuera de la carpeta temporal "
                            f"({info.filename})"
                        )
                        return resultado
                    if info.is_dir() or info.filename.endswith(("/", "\\")):
                        destino.mkdir(parents=True, exist_ok=True)
                        continue
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as origen_zip, open(destino, "wb") as salida:
                        shutil.copyfileobj(origen_zip, salida, _CHUNK)
        except (zipfile.BadZipFile, OSError, RuntimeError) as error:
            resultado["error"] = f"no se pudo extraer la actualizacion: {error}"
            return resultado

        ejecutable = None
        for candidato in sorted(staging.rglob("*"), key=lambda p: str(p).lower()):
            try:
                if candidato.is_file() and candidato.name.lower() == _NOMBRE_EXE:
                    ejecutable = candidato
                    break
            except OSError:
                continue
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


def lanzar(preparado) -> dict:
    """Inicia el ``.bat`` sin ventana de consola y vuelve enseguida."""
    try:
        if not isinstance(preparado, dict):
            return {"ok": False, "error": "datos de actualizacion invalidos"}
        if preparado.get("ok") is False:
            return {"ok": False, "error": "la actualizacion no esta lista"}
        bat = _texto(preparado.get("bat"))
        if not bat or not Path(bat).is_file():
            return {"ok": False, "error": "no se encontro el actualizador"}

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

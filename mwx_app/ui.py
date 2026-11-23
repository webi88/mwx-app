# -*- coding: utf-8 -*-
"""Interfaz de escritorio de MWX APP (Tkinter + ttk).

Pantallas: login, "Tu campaña", campaña en curso y resumen final. Toda la
configuracion tecnica llega del servidor; esta capa solo muestra botones
grandes, confirmaciones claras y mensajes sin jerga.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

from mwx_app import config, runner, store, theme, updater
from mwx_app.api_client import (
    CuentaDesactivadaError,
    MWXClient,
    SesionExpiradaError,
    mensaje_amigable,
)

ANCHO = 900
ALTO = 700
MIN_ANCHO = 820
MIN_ALTO = 650

# Cada cuánto se vuelve a pedir `/v1/perfil` mientras se ve "Tu campaña".
# El botón "Actualizar" fuerza el mismo refresco al instante.
SYNC_CAMPANA_MS = 10_000


def _firma_perfil(perfil) -> str:
    """Firma estable de campaña + cuentas asignadas (sin secretos).

    Sirve para detectar si el panel cambió algo y decidir si hay que
    repintar la pantalla. Solo incluye datos visibles; nunca cookies,
    tokens ni contraseñas. Nunca lanza.
    """
    try:
        datos = perfil if isinstance(perfil, dict) else {}
        campana = datos.get("campana") or {}
        cuentas = sorted(
            (
                str(cuenta.get("usuario") or ""),
                str(cuenta.get("seccion") or ""),
            )
            for cuenta in (datos.get("cuentas") or [])
            if isinstance(cuenta, dict)
        )
        return json.dumps(
            {"campana": campana, "cuentas": cuentas},
            sort_keys=True,
            ensure_ascii=False,
        )
    except Exception:
        return ""



def _formato_tiempo(segundos: float) -> str:
    try:
        total = max(0, int(segundos))
    except (TypeError, ValueError):
        total = 0
    horas, resto = divmod(total, 3600)
    minutos, segundos = divmod(resto, 60)
    if horas:
        return f"{horas}:{minutos:02d}:{segundos:02d}"
    return f"{minutos:02d}:{segundos:02d}"


def _corto(texto: Any, limite: int = 70) -> str:
    limpio = " ".join(str(texto or "").split())
    if len(limpio) <= limite:
        return limpio
    return limpio[: limite - 1].rstrip() + "…"


def _etiqueta_seccion(codigo: str) -> str:
    try:
        from core.secciones import SECCIONES

        return SECCIONES.get(codigo, codigo or "Sin sección")
    except Exception:
        return {
            "CI": "Ciudadanía",
            "IP": "Institución Privada",
            "LIB": "Libertad",
            "JUS": "Justicia",
        }.get(codigo, "Sin sección")


def _normalizar_seccion(valor: Any) -> str:
    try:
        from core.secciones import normalizar_seccion

        return normalizar_seccion(valor) or ""
    except Exception:
        return str(valor or "").strip()


def _etiqueta_rol(rol: str) -> str:
    return {
        "hashtags": "Publicación",
        "post": "Publicación",
        "cita": "Cita",
        "rt": "Retweet",
        "comentario": "Comentario",
        "like": "Me gusta",
    }.get(str(rol or "").lower(), "Publicación")


def _detalle_amigable(evento: dict) -> str:
    detalle = str(evento.get("detalle") or "").strip()
    if evento.get("ok"):
        if not detalle or detalle.lower() == "ok":
            return "Publicado"
        if "con imagen" in detalle.lower():
            return "Publicado con imagen"
        if "cita" in str(evento.get("rol") or "").lower():
            return "Cita publicada"
        return "Publicado"
    texto = detalle.lower()
    if any(clave in texto for clave in ("sesión", "sesion", "login", "onboarding", "anti-bot")):
        return "La sesión de esta cuenta no está disponible"
    if any(clave in texto for clave in ("compositor", "editor", "botón", "boton", "post")):
        return "No se pudo publicar en esta cuenta"
    if "confirmó" in texto or "confirmo" in texto:
        return "No se pudo confirmar la publicación"
    if "cuota" in texto or "límite" in texto or "limite" in texto:
        return "Límite diario alcanzado"
    if "tier" in texto or "pausa" in texto:
        return "La cuenta no está disponible para esta acción"
    return "No se pudo publicar"


def _mensaje_error_campana(resumen: dict) -> str:
    error = str(resumen.get("error") or "").lower()
    if "tier 1" in error or "sin cuentas" in error:
        return (
            "Tus cuentas no están listas para publicar en este momento. "
            "Contacta a tu administrador."
        )
    if "sin acciones" in error or "cuota" in error:
        return "No hay publicaciones pendientes por ahora. Inténtalo más tarde."
    if "sin url" in error or "sin material" in error:
        return "La campaña no tiene contenido configurado. Contacta a tu administrador."
    if "base de datos" in error or "b d" in error:
        return "No se pudieron leer tus cuentas. Inténtalo de nuevo."
    return "No se pudo completar la campaña. Inténtalo de nuevo."


class MWXApp:
    """Controlador y vistas de la aplicacion."""

    def __init__(self, root: tk.Tk, auto_reconectar: bool = True):
        self.root = root
        self.auto_reconectar = bool(auto_reconectar)
        self.familia = theme.aplicar_tema(root)

        self._configurar_ventana()
        self._cargar_icono()
        self.root.report_callback_exception = self._reportar_error_tk

        self.api_url = config.api_url()
        self.cliente: Optional[MWXClient] = (
            MWXClient(self.api_url) if self.api_url else None
        )
        self.token = ""
        self.perfil: dict = {}
        self.usuario = ""
        self.nombre = ""
        self.ajustes: dict = {}
        self.imagenes_extra: list = []

        self.cancelar: Optional[threading.Event] = None
        self._campana_activa = False
        self._cerrando = False
        self._login_en_curso = False
        self._eventos: list = []
        self._resumen: Optional[dict] = None
        self._inicio_ts = 0.0
        self._after_cola = None
        self._after_tiempo = None
        self._after_actualizacion = None
        self._after_sync = None
        self._sincronizando = False
        self._sync_firma = ""
        self._sync_hora = ""
        self._inicio_pendiente = False
        self._lbl_sync = None
        self._ventana_actualizacion = None
        self._terminado = False
        self._ventana_ajustes = None
        self._links: list = []
        self._icono = None

        self._cola: queue.Queue = queue.Queue()
        self.contenedor = tk.Frame(root, bg=theme.FONDO)
        self.contenedor.pack(fill="both", expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        self._programar_cola()

        if not self._gate_actualizacion():
            return

        sesion = config.cargar_sesion() if self.auto_reconectar else {}
        if sesion and self.api_url:
            self._reconectar(sesion)
        else:
            self.mostrar_login()

    def _configurar_ventana(self) -> None:
        self.root.title("MWX APP")
        try:
            self.root.configure(bg=theme.FONDO)
        except tk.TclError:
            pass
        self.root.minsize(MIN_ANCHO, MIN_ALTO)
        self.root.update_idletasks()
        ancho_pantalla = self.root.winfo_screenwidth()
        alto_pantalla = self.root.winfo_screenheight()
        x = max(0, (ancho_pantalla - ANCHO) // 2)
        y = max(0, (alto_pantalla - ALTO) // 2)
        self.root.geometry(f"{ANCHO}x{ALTO}+{x}+{y}")
        self._barra_titulo_oscura()

    def _barra_titulo_oscura(self) -> None:
        """Barra de titulo oscura en Windows 10/11 (best-effort)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            valor = ctypes.c_int(1)
            for atributo in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
                resultado = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    atributo,
                    ctypes.byref(valor),
                    ctypes.sizeof(valor),
                )
                if resultado == 0:
                    break
        except Exception:
            pass

    def _cargar_icono(self) -> None:
        carpetas = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            carpetas.append(Path(meipass) / "assets")
        carpetas.append(Path(__file__).resolve().parent.parent / "assets")
        try:
            carpetas.append(Path(sys.executable).resolve().parent / "assets")
        except Exception:
            pass

        vistas = set()
        for carpeta in carpetas:
            clave = str(carpeta)
            if clave in vistas:
                continue
            vistas.add(clave)
            ico = carpeta / "mwx_app.ico"
            if ico.is_file():
                try:
                    self.root.iconbitmap(default=str(ico))
                    return
                except tk.TclError:
                    pass
            png = carpeta / "mwx_logo.png"
            if png.is_file():
                try:
                    imagen = tk.PhotoImage(file=str(png))
                    self.root.iconphoto(True, imagen)
                    self._icono = imagen
                    return
                except tk.TclError:
                    pass

    def _limpiar(self) -> None:
        self._cerrar_ajustes()
        if self._after_tiempo is not None:
            try:
                self.root.after_cancel(self._after_tiempo)
            except tk.TclError:
                pass
            self._after_tiempo = None
        if self._after_sync is not None:
            try:
                self.root.after_cancel(self._after_sync)
            except tk.TclError:
                pass
            self._after_sync = None
        self._lbl_sync = None
        for hijo in list(self.contenedor.winfo_children()):
            try:
                hijo.destroy()
            except tk.TclError:
                pass

    def _programar_cola(self) -> None:
        if self._cerrando:
            return
        try:
            self._after_cola = self.root.after(120, self._procesar_cola)
        except tk.TclError:
            self._after_cola = None

    def _procesar_cola(self) -> None:
        try:
            for _ in range(40):
                tipo, carga = self._cola.get_nowait()
                self._despachar(tipo, carga)
        except queue.Empty:
            pass
        except Exception as error:
            self._registrar_error(error)
        finally:
            self._programar_cola()

    def _despachar(self, tipo: str, carga: Any) -> None:
        if tipo == "login_ok":
            self._login_en_curso = False
            self._aplicar_sesion(carga, reconexion=False)
        elif tipo == "reconectar_ok":
            self._login_en_curso = False
            self._aplicar_sesion(carga, reconexion=True)
        elif tipo == "login_error":
            self._login_en_curso = False
            if self.pantalla_actual == "login":
                self._habilitar_login()
                self._mensaje_login(str(carga or mensaje_amigable(None)))
        elif tipo == "reconectar_error":
            self._login_en_curso = False
            if self.pantalla_actual == "login":
                self._habilitar_login()
                self._mensaje_login(str(carga or mensaje_amigable(None)))
        elif tipo == "campana_evento":
            if self.pantalla_actual == "en_curso":
                self._aplicar_evento(carga)
        elif tipo == "campana_fin":
            self._campana_activa = False
            if self._after_tiempo is not None:
                try:
                    self.root.after_cancel(self._after_tiempo)
                except tk.TclError:
                    pass
                self._after_tiempo = None
            self.mostrar_fin(carga if isinstance(carga, dict) else {})
        elif tipo == "perfil_sincronizado":
            self._sincronizando = False
            datos = carga if isinstance(carga, dict) else {}
            perfil = datos.get("perfil")
            if isinstance(perfil, dict) and perfil:
                self._aplicar_perfil_actualizado(
                    perfil,
                    iniciar=bool(datos.get("iniciar")),
                    manual=bool(datos.get("manual")),
                )
            else:
                self._inicio_pendiente = False
                self._programar_sincronizacion()
        elif tipo == "sync_error":
            self._sincronizando = False
            datos = carga if isinstance(carga, dict) else {}
            self._inicio_pendiente = False
            mensaje = str(
                datos.get("error")
                or "No se pudo actualizar la configuración desde el panel."
            )
            if datos.get("manual") or datos.get("iniciar"):
                self._mensaje_sync(mensaje, error=True)
            if datos.get("iniciar"):
                messagebox.showerror("MWX APP", mensaje, parent=self.root)
            self._programar_sincronizacion()

    def _hilo(self, funcion, *args) -> None:
        threading.Thread(target=funcion, args=args, daemon=True).start()

    def _aplicar_sesion(self, carga: dict, reconexion: bool) -> None:
        respuesta = (carga or {}).get("respuesta") or {}
        usuario = respuesta.get("usuario") or {}
        self.token = str(respuesta.get("token") or "").strip()
        self.usuario = str(usuario.get("username") or "").strip()
        self.nombre = str(usuario.get("nombre") or self.usuario).strip()
        self.perfil = respuesta
        self.ajustes = {}
        self.imagenes_extra = []
        self._sync_firma = _firma_perfil(respuesta)
        self._sync_hora = time.strftime("%H:%M:%S")
        self._sincronizando = False
        self._inicio_pendiente = False
        try:
            self._var_password.set("")
        except Exception:
            pass
        config.guardar_sesion(self.token, self.usuario, self.nombre)
        if reconexion:
            self._mensaje_login("")
        self.mostrar_campana()

    def _mensaje_login(self, texto: str, error: bool = True) -> None:
        etiqueta = getattr(self, "_lbl_error", None)
        if etiqueta is None:
            return
        try:
            etiqueta.configure(
                text=str(texto or ""),
                fg=theme.ERROR if error else theme.TEXTO_SEC,
            )
        except tk.TclError:
            pass

    def _habilitar_login(self) -> None:
        boton = getattr(self, "_boton_login", None)
        if boton is None:
            return
        try:
            boton.configure(state="normal", text="Entrar")
        except tk.TclError:
            pass

    def mostrar_login(self, mensaje: str = "") -> None:
        self._limpiar()
        self.pantalla_actual = "login"
        self._login_en_curso = False

        centro = tk.Frame(self.contenedor, bg=theme.FONDO)
        centro.place(relx=0.5, rely=0.5, anchor="center")

        self._crear_logo(centro).pack(pady=(0, 10))
        tk.Label(
            centro,
            text="Acceso a tu campaña",
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 19, "bold"),
        ).pack()
        tk.Label(
            centro,
            text="Ingresa con los datos que te dio tu administrador.",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
        ).pack(pady=(4, 14))

        formulario = tk.Frame(centro, bg=theme.FONDO)
        formulario.pack()

        tk.Label(
            formulario,
            text="Usuario",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self._var_usuario = tk.StringVar(value=config.cargar_usuario_recordado())
        self._entrada_usuario = tk.Entry(
            formulario,
            textvariable=self._var_usuario,
            width=36,
            bg=theme.SUPERFICIE_ALT,
            fg=theme.TEXTO,
            insertbackground=theme.ACENTO,
            relief="flat",
            font=(self.familia, 12),
            highlightthickness=1,
            highlightbackground=theme.BORDE,
            highlightcolor=theme.ACENTO,
        )
        self._entrada_usuario.pack(ipady=7, pady=(0, 12))

        tk.Label(
            formulario,
            text="Contraseña",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        self._var_password = tk.StringVar()
        self._entrada_password = tk.Entry(
            formulario,
            textvariable=self._var_password,
            show="•",
            width=36,
            bg=theme.SUPERFICIE_ALT,
            fg=theme.TEXTO,
            insertbackground=theme.ACENTO,
            relief="flat",
            font=(self.familia, 12),
            highlightthickness=1,
            highlightbackground=theme.BORDE,
            highlightcolor=theme.ACENTO,
        )
        self._entrada_password.pack(ipady=7, pady=(0, 10))

        self._entrada_usuario.bind("<Return>", lambda _e: self._hacer_login())
        self._entrada_password.bind("<Return>", lambda _e: self._hacer_login())

        self._var_recordar = tk.BooleanVar(value=True)
        tk.Checkbutton(
            formulario,
            text="Recordar mi usuario",
            variable=self._var_recordar,
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            activebackground=theme.FONDO,
            activeforeground=theme.TEXTO,
            selectcolor=theme.SUPERFICIE_ALT,
            font=(self.familia, 10),
        ).pack(anchor="w", pady=(0, 10))

        self._boton_login = theme.boton(
            formulario,
            "Entrar",
            self._hacer_login,
            color=theme.ACENTO,
            color_hover=theme.ACENTO_HOVER,
            familia=self.familia,
            tamano=13,
        )
        self._boton_login.pack(fill="x")

        self._lbl_error = tk.Label(
            centro,
            text="",
            bg=theme.FONDO,
            fg=theme.ERROR,
            font=(self.familia, 10),
            wraplength=380,
            justify="center",
        )
        self._lbl_error.pack(pady=(10, 0))
        if mensaje:
            self._mensaje_login(mensaje)

        if not self.api_url:
            tk.Label(
                centro,
                text="Falta configurar el servidor.",
                bg=theme.FONDO,
                fg=theme.ACENTO,
                font=(self.familia, 10, "bold"),
            ).pack(pady=(10, 0))

        # Engranaje discreto arriba a la derecha para el administrador
        # (cambiar la URL del servidor sin editar archivos a mano).
        tk.Button(
            self.contenedor,
            text="⚙️",
            command=self._abrir_config_servidor,
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            activebackground=theme.FONDO,
            activeforeground=theme.ACENTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 11),
        ).place(relx=0.985, rely=0.015, anchor="ne")

        self._entrada_usuario.focus_set()

    def _crear_logo(self, padre: tk.Misc) -> tk.Canvas:
        canvas = tk.Canvas(
            padre, width=112, height=112, bg=theme.FONDO, highlightthickness=0
        )
        canvas.create_polygon(
            14,
            0,
            98,
            0,
            112,
            14,
            112,
            98,
            98,
            112,
            14,
            112,
            0,
            98,
            0,
            14,
            smooth=True,
            fill=theme.SUPERFICIE,
            outline=theme.BORDE,
            width=2,
        )
        canvas.create_rectangle(16, 88, 74, 96, fill=theme.ACENTO, width=0)
        canvas.create_rectangle(62, 88, 96, 96, fill=theme.SECUNDARIO, width=0)
        canvas.create_text(
            56,
            52,
            text="MWX",
            fill=theme.TEXTO,
            font=(self.familia, 26, "bold"),
        )
        return canvas

    def _gate_actualizacion(self) -> bool:
        """Consulta la actualización antes del login; False = cerrar la app."""
        try:
            if not updater.puede_actualizarse():
                return True
            api = str(self.api_url or "").strip()
            if not api:
                return True
            datos = updater.consultar(api, updater.version_actual(), timeout=6)
            if not datos.get("ok") or not datos.get("requerida"):
                return True
            return self._dialogo_actualizacion(datos)
        except Exception as error:
            self._registrar_error(error)
            return True

    def _verificar_actualizacion_obligatoria(self) -> bool:
        """Gate rápido antes de una campaña; False = no continuar."""
        try:
            if not updater.puede_actualizarse():
                return True
            api = str(self.api_url or "").strip()
            if not api:
                return True
            datos = updater.consultar(api, updater.version_actual(), timeout=4)
            if (
                datos.get("ok")
                and datos.get("requerida")
                and datos.get("obligatoria")
            ):
                return self._dialogo_actualizacion(datos)
        except Exception as error:
            self._registrar_error(error)
        return True

    def _mensaje_actualizacion(self, resultado) -> str:
        error = ""
        if isinstance(resultado, dict):
            error = str(resultado.get("error") or "").strip()
        if error:
            return f"No se pudo completar la actualización. {error}"
        return "No se pudo completar la actualización. Inténtalo de nuevo."

    def _worker_actualizacion(self, datos: dict, cola: queue.Queue) -> None:
        """Descarga+prepara fuera del hilo de Tk (solo escribe en la cola)."""
        try:
            def progreso(frac, texto):
                cola.put(("progreso", frac, texto))

            resultado = updater.actualizar(
                self.api_url,
                version=str(datos.get("version") or ""),
                progreso=progreso,
            )
        except Exception as error:
            self._registrar_error(error)
            resultado = {"ok": False, "error": mensaje_amigable(error)}
        cola.put(("listo", resultado))

    def _cerrar_ventana_actualizacion(self, ventana: tk.Toplevel) -> None:
        if self._after_actualizacion is not None:
            try:
                self.root.after_cancel(self._after_actualizacion)
            except tk.TclError:
                pass
            self._after_actualizacion = None
        try:
            ventana.grab_release()
        except tk.TclError:
            pass
        try:
            ventana.destroy()
        except tk.TclError:
            pass

    def _salir_aplicacion(self) -> None:
        self._terminado = True
        if self._after_actualizacion is not None:
            try:
                self.root.after_cancel(self._after_actualizacion)
            except tk.TclError:
                pass
            self._after_actualizacion = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        if updater.puede_actualizarse():
            try:
                os._exit(0)
            except Exception:
                pass

    def _dialogo_actualizacion(self, datos: dict) -> bool:
        """Diálogo de actualización; True = seguir, False = cerrar la app."""
        obligatoria = bool(datos.get("obligatoria"))
        version = str(datos.get("version") or "").strip() or "nueva"
        notas = str(datos.get("notas") or "").strip()
        if not notas:
            notas = "Hay una versión nueva de MWX APP lista para instalarse."

        ventana = tk.Toplevel(self.root)
        ventana.title("Actualización de MWX APP")
        ventana.configure(bg=theme.FONDO)
        ventana.transient(self.root)
        ventana.resizable(False, False)
        ventana.grab_set()
        self._ventana_actualizacion = ventana

        resultado = {"continuar": True}
        cola: queue.Queue = queue.Queue()
        estado = {"descargando": False}

        marco = tk.Frame(ventana, bg=theme.FONDO, padx=30, pady=24)
        marco.pack(fill="both", expand=True)

        tk.Label(
            marco,
            text="Actualización disponible",
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            marco,
            text=f"Versión {version}",
            bg=theme.FONDO,
            fg=theme.ACENTO,
            font=(self.familia, 12, "bold"),
        ).pack(anchor="w", pady=(6, 0))
        tk.Label(
            marco,
            text=(
                "Esta actualización es obligatoria. Necesitas instalarla "
                "para seguir usando la aplicación."
                if obligatoria
                else "Puedes actualizar ahora o continuar con la versión actual."
            ),
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(10, 10))
        tk.Label(
            marco,
            text=notas[:1500],
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO,
            font=(self.familia, 10),
            wraplength=540,
            justify="left",
            padx=14,
            pady=12,
        ).pack(fill="x")

        barra = ttk.Progressbar(
            marco, orient="horizontal", mode="determinate", maximum=100, value=0
        )
        barra.pack(fill="x", pady=(16, 8))
        lbl_estado = tk.Label(
            marco,
            text="",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
            wraplength=540,
            justify="left",
        )
        lbl_estado.pack(anchor="w")

        botones = tk.Frame(marco, bg=theme.FONDO)
        botones.pack(fill="x", pady=(16, 0))

        def procesar_listo(res) -> None:
            if not isinstance(res, dict) or not res.get("ok"):
                estado["descargando"] = False
                barra.configure(value=0)
                lbl_estado.configure(
                    text=self._mensaje_actualizacion(res), fg=theme.ERROR
                )
                btn_actualizar.configure(state="normal", text="Reintentar")
                if btn_ahora_no is not None:
                    btn_ahora_no.configure(state="normal")
                return
            lbl_estado.configure(
                text="La aplicación se reiniciará para aplicar la actualización...",
                fg=theme.EXITO,
            )
            barra.configure(value=100)
            lanzado = updater.lanzar(res)
            if not lanzado.get("ok"):
                estado["descargando"] = False
                lbl_estado.configure(
                    text=self._mensaje_actualizacion(lanzado), fg=theme.ERROR
                )
                btn_actualizar.configure(state="normal", text="Reintentar")
                if btn_ahora_no is not None:
                    btn_ahora_no.configure(state="normal")
                return
            btn_actualizar.configure(state="disabled", text="Reiniciando...")
            if btn_ahora_no is not None:
                btn_ahora_no.configure(state="disabled")
            try:
                self.root.after(700, self._salir_aplicacion)
            except tk.TclError:
                self._salir_aplicacion()

        def revisar() -> None:
            if not ventana.winfo_exists():
                return
            listo = False
            try:
                while True:
                    evento = cola.get_nowait()
                    tipo = evento[0] if evento else ""
                    if tipo == "progreso":
                        try:
                            frac = float(evento[1])
                        except (TypeError, ValueError, IndexError):
                            frac = 0.0
                        barra.configure(
                            value=max(0.0, min(1.0, frac)) * 100
                        )
                        lbl_estado.configure(
                            text=str(evento[2] or "Descargando la actualización...")
                        )
                    elif tipo == "listo":
                        listo = True
                        procesar_listo(evento[1] if len(evento) > 1 else {})
            except queue.Empty:
                pass
            if listo:
                return
            try:
                self._after_actualizacion = self.root.after(120, revisar)
            except tk.TclError:
                self._after_actualizacion = None

        def iniciar() -> None:
            if estado["descargando"]:
                return
            estado["descargando"] = True
            btn_actualizar.configure(state="disabled", text="Actualizando...")
            if btn_ahora_no is not None:
                btn_ahora_no.configure(state="disabled")
            barra.configure(value=0)
            lbl_estado.configure(
                text="Descargando la actualización...", fg=theme.TEXTO_SEC
            )
            self._hilo(self._worker_actualizacion, datos, cola)
            revisar()

        def cerrar() -> None:
            if obligatoria:
                resultado["continuar"] = False
                self._salir_aplicacion()
                return
            resultado["continuar"] = True
            self._cerrar_ventana_actualizacion(ventana)

        btn_actualizar = theme.boton(
            botones,
            "Actualizar ahora",
            iniciar,
            color=theme.ACENTO,
            color_hover=theme.ACENTO_HOVER,
            familia=self.familia,
            tamano=11,
            padx=18,
            pady=8,
        )
        btn_actualizar.pack(side="right")

        btn_ahora_no = None
        if not obligatoria:
            btn_ahora_no = tk.Button(
                botones,
                text="Ahora no",
                command=cerrar,
                bg=theme.SUPERFICIE,
                fg=theme.TEXTO_SEC,
                activebackground=theme.SUPERFICIE_ALT,
                activeforeground=theme.TEXTO,
                relief="flat",
                bd=0,
                cursor="hand2",
                font=(self.familia, 10),
                padx=16,
                pady=8,
            )
            btn_ahora_no.pack(side="right", padx=(0, 8))

        ventana.protocol("WM_DELETE_WINDOW", cerrar)
        try:
            self.root.wait_window(ventana)
        except tk.TclError:
            pass
        finally:
            self._cerrar_ventana_actualizacion(ventana)
            self._ventana_actualizacion = None
        return bool(resultado["continuar"])

    def _abrir_config_servidor(self) -> None:
        ventana = tk.Toplevel(self.root)
        ventana.title("Configurar servidor")
        ventana.configure(bg=theme.FONDO)
        ventana.transient(self.root)
        ventana.grab_set()
        ventana.resizable(False, False)

        marco = tk.Frame(ventana, bg=theme.FONDO, padx=26, pady=22)
        marco.pack(fill="both", expand=True)
        tk.Label(
            marco,
            text="Dirección del servidor",
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            marco,
            text="Escríbela tal como te la dio tu administrador.",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
        ).pack(anchor="w", pady=(4, 10))

        variable = tk.StringVar(value=self.api_url)
        entrada = tk.Entry(
            marco,
            textvariable=variable,
            width=46,
            bg=theme.SUPERFICIE_ALT,
            fg=theme.TEXTO,
            insertbackground=theme.ACENTO,
            relief="flat",
            font=(self.familia, 11),
            highlightthickness=1,
            highlightbackground=theme.BORDE,
            highlightcolor=theme.ACENTO,
        )
        entrada.pack(ipady=7, pady=(0, 14))
        entrada.focus_set()

        def guardar():
            guardada = config.guardar_api_url(variable.get())
            if not guardada:
                messagebox.showerror(
                    "MWX APP",
                    "No se pudo guardar la dirección. Revisa el texto e inténtalo de nuevo.",
                    parent=ventana,
                )
                return
            self.api_url = guardada
            self.cliente = MWXClient(guardada)
            ventana.destroy()
            self.mostrar_login()

        botones = tk.Frame(marco, bg=theme.FONDO)
        botones.pack(fill="x")
        theme.boton(
            botones,
            "Guardar",
            guardar,
            color=theme.ACENTO,
            color_hover=theme.ACENTO_HOVER,
            familia=self.familia,
            tamano=11,
            padx=18,
            pady=7,
        ).pack(side="right")
        tk.Button(
            botones,
            text="Cancelar",
            command=ventana.destroy,
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO_SEC,
            activebackground=theme.SUPERFICIE_ALT,
            activeforeground=theme.TEXTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 10),
            padx=16,
            pady=7,
        ).pack(side="right", padx=(0, 8))
        ventana.bind("<Return>", lambda _e: guardar())
        ventana.bind("<Escape>", lambda _e: ventana.destroy())

    def _hacer_login(self) -> None:
        if self._login_en_curso:
            return
        usuario = self._var_usuario.get().strip()
        password = self._var_password.get()
        if not usuario or not password:
            self._mensaje_login("Escribe tu usuario y tu contraseña.")
            return

        self.api_url = config.api_url()
        if not self.api_url:
            self._mensaje_login("Primero configura la dirección del servidor.")
            self._abrir_config_servidor()
            return

        if self._var_recordar.get():
            config.guardar_usuario_recordado(usuario)
        else:
            config.guardar_usuario_recordado("")

        self._login_en_curso = True
        self._boton_login.configure(state="disabled", text="Entrando...")
        self._mensaje_login("Conectando con el servidor...", error=False)
        self.cliente = MWXClient(self.api_url)
        self._hilo(self._worker_login, usuario, password)

    def _worker_login(self, usuario: str, password: str) -> None:
        try:
            respuesta = self.cliente.login(
                usuario, password, config.machine_id()
            )
        except Exception as error:
            self._cola.put(("login_error", mensaje_amigable(error)))
            return

        try:
            usuarios = store.sincronizar_perfil(respuesta)
        except Exception as error:
            self._registrar_error(error)
            self._cola.put(
                (
                    "login_error",
                    "No se pudieron preparar tus cuentas en este equipo. "
                    "Inténtalo de nuevo.",
                )
            )
            return

        self._cola.put(
            (
                "login_ok",
                {"respuesta": respuesta, "usuarios": usuarios},
            )
        )

    def _reconectar(self, sesion: dict) -> None:
        self.token = sesion.get("token", "")
        self.usuario = sesion.get("usuario", "")
        self.nombre = sesion.get("nombre", self.usuario)
        self.mostrar_login()
        self._login_en_curso = True
        self._var_usuario.set(self.usuario)
        self._boton_login.configure(state="disabled", text="Conectando...")
        self._mensaje_login("Reconectando tu sesión...", error=False)
        self._hilo(self._worker_reconectar, self.token)

    def _worker_reconectar(self, token: str) -> None:
        try:
            self.cliente.estado(token)
            perfil = self.cliente.perfil(token)
            usuarios = store.sincronizar_perfil(perfil)
        except (SesionExpiradaError, CuentaDesactivadaError) as error:
            config.borrar_sesion()
            self._cola.put(("reconectar_error", error.mensaje))
            return
        except Exception as error:
            self._registrar_error(error)
            self._cola.put(("reconectar_error", mensaje_amigable(error)))
            return

        datos = dict(perfil)
        datos["token"] = token
        self._cola.put(
            (
                "reconectar_ok",
                {"respuesta": datos, "usuarios": usuarios},
            )
        )

    def mostrar_campana(self, aviso: str = "") -> None:
        if not self.perfil:
            self.mostrar_login()
            return
        self._limpiar()
        self.pantalla_actual = "campana"
        self._campana_activa = False

        campana = runner.config_efectiva(self.perfil, self.ajustes)
        cuentas = self.perfil.get("cuentas") or []

        cabecera = tk.Frame(self.contenedor, bg=theme.FONDO)
        cabecera.pack(fill="x", padx=44, pady=(34, 0))
        tk.Label(
            cabecera,
            text=f"Hola, {self.nombre or self.usuario}",
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 22, "bold"),
        ).pack(side="left")
        tk.Button(
            cabecera,
            text="Cerrar sesión",
            command=self._cerrar_sesion,
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            activebackground=theme.FONDO,
            activeforeground=theme.TEXTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 9),
        ).pack(side="right")

        tk.Label(
            self.contenedor,
            text="Tu campaña",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 12),
        ).pack(anchor="w", padx=44, pady=(0, 4))

        fila_sync = tk.Frame(self.contenedor, bg=theme.FONDO)
        fila_sync.pack(fill="x", padx=44, pady=(0, 12))
        texto_sync = str(aviso or "").strip()
        if texto_sync:
            if self._sync_hora:
                texto_sync += f" · {self._sync_hora}"
        else:
            texto_sync = (
                f"Configuración sincronizada con el panel · "
                f"{self._sync_hora or 'ahora'}"
            )
        if self.ajustes and campana.get("permitir_ajustes"):
            texto_sync += " · Ajustes locales activos"
        self._lbl_sync = tk.Label(
            fila_sync,
            text=texto_sync,
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 9),
            anchor="w",
        )
        self._lbl_sync.pack(side="left", fill="x", expand=True)
        tk.Button(
            fila_sync,
            text="🔄 Actualizar",
            command=self._sincronizar_manual,
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            activebackground=theme.FONDO,
            activeforeground=theme.TEXTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 9),
        ).pack(side="right")

        tarjeta_cuentas = self._tarjeta(self.contenedor)
        tarjeta_cuentas.pack(fill="x", padx=44, pady=(0, 12))
        tk.Label(
            tarjeta_cuentas,
            text="Tus cuentas",
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO,
            font=(self.familia, 13, "bold"),
        ).pack(anchor="w")
        chips = tk.Frame(tarjeta_cuentas, bg=theme.SUPERFICIE)
        chips.pack(fill="x", pady=(10, 0))
        grupos = self._agrupar_secciones(cuentas)
        if grupos:
            for codigo, cantidad in grupos:
                tk.Label(
                    chips,
                    text=f"{_etiqueta_seccion(codigo)} · {cantidad}",
                    bg=theme.SUPERFICIE_ALT,
                    fg=theme.TEXTO,
                    font=(self.familia, 10),
                    padx=12,
                    pady=5,
                ).pack(side="left", padx=(0, 8))
        else:
            tk.Label(
                chips,
                text="No tienes cuentas asignadas.",
                bg=theme.SUPERFICIE,
                fg=theme.TEXTO_SEC,
                font=(self.familia, 10),
            ).pack(anchor="w")

        tarjeta_campana = self._tarjeta(self.contenedor)
        tarjeta_campana.pack(fill="x", padx=44, pady=(0, 14))
        tk.Label(
            tarjeta_campana,
            text="Hashtags",
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO,
            font=(self.familia, 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            tarjeta_campana,
            text=campana["hashtags"] or "Sin hashtags configurados.",
            bg=theme.SUPERFICIE,
            fg=theme.ACENTO,
            font=(self.familia, 12, "bold"),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        self._lbl_resumen = tk.Label(
            self.contenedor,
            text="",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
        )
        self._lbl_resumen.pack(anchor="w", padx=44)
        self._actualizar_resumen_campana()

        self._lbl_estado = tk.Label(
            self.contenedor,
            text="",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
        )
        self._lbl_estado.pack(anchor="w", padx=44, pady=(8, 16))

        self._puede_iniciar = bool(cuentas) and bool(
            campana["hashtags"]
            or campana["contexto"]
            or campana["narrativa"]
            or campana["urls"]
        )
        if not cuentas:
            self._lbl_estado.configure(
                text="No tienes cuentas asignadas. Contacta a tu administrador."
            )
        elif not self._puede_iniciar:
            self._lbl_estado.configure(
                text="La campaña aún no está configurada. Contacta a tu administrador."
            )
        else:
            self._lbl_estado.configure(text="Todo listo para comenzar.")

        acciones = tk.Frame(self.contenedor, bg=theme.FONDO)
        acciones.pack(pady=(6, 0))
        self._boton_iniciar = theme.boton(
            acciones,
            "▶  Iniciar campaña",
            self._iniciar_campana,
            color=theme.EXITO,
            color_hover=theme.EXITO_HOVER,
            familia=self.familia,
            tamano=16,
            padx=44,
            pady=14,
        )
        self._boton_iniciar.pack()
        if not self._puede_iniciar:
            self._boton_iniciar.configure(state="disabled", bg=theme.SUPERFICIE_ALT)

        if campana["permitir_ajustes"]:
            tk.Button(
                self.contenedor,
                text="⚙️ Ajustes",
                command=self._abrir_ajustes,
                bg=theme.FONDO,
                fg=theme.TEXTO_SEC,
                activebackground=theme.FONDO,
                activeforeground=theme.TEXTO,
                relief="flat",
                bd=0,
                cursor="hand2",
                font=(self.familia, 10),
            ).pack(pady=(12, 0))

        self._programar_sincronizacion()

    def _tarjeta(self, padre: tk.Misc) -> tk.Frame:
        marco = tk.Frame(
            padre,
            bg=theme.SUPERFICIE,
            highlightbackground=theme.BORDE,
            highlightthickness=1,
            padx=20,
            pady=16,
        )
        return marco

    def _agrupar_secciones(self, cuentas: list) -> list:
        conteo: dict = {}
        for cuenta in cuentas or []:
            if not isinstance(cuenta, dict):
                continue
            codigo = _normalizar_seccion(cuenta.get("seccion"))
            conteo[codigo] = conteo.get(codigo, 0) + 1
        orden = ["CI", "IP", "LIB", "JUS", ""]
        salida = []
        for codigo in orden:
            if conteo.get(codigo):
                salida.append((codigo, conteo[codigo]))
        for codigo, cantidad in conteo.items():
            if codigo not in orden and cantidad:
                salida.append((codigo, cantidad))
        return salida

    def _actualizar_resumen_campana(self) -> None:
        etiqueta = getattr(self, "_lbl_resumen", None)
        if etiqueta is None:
            return
        campana = runner.config_efectiva(self.perfil, self.ajustes)
        imagenes = len(campana.get("imagenes") or []) + len(self.imagenes_extra)
        texto = (
            f"{campana['acciones_por_cuenta']} publicaciones por cuenta · "
            f"ventana {campana['duracion_min']} min"
        )
        if imagenes:
            texto += f" · {imagenes} imagen(es)"
        etiqueta.configure(text=texto)

    def _mensaje_sync(self, texto: str, error: bool = False) -> None:
        etiqueta = getattr(self, "_lbl_sync", None)
        if etiqueta is None:
            return
        try:
            etiqueta.configure(
                text=str(texto or ""),
                fg=theme.ERROR if error else theme.TEXTO_SEC,
            )
        except tk.TclError:
            pass

    def _programar_sincronizacion(self) -> None:
        if self._after_sync is not None:
            try:
                self.root.after_cancel(self._after_sync)
            except tk.TclError:
                pass
            self._after_sync = None
        if self._cerrando or self.pantalla_actual != "campana" or self._campana_activa:
            return
        try:
            self._after_sync = self.root.after(
                SYNC_CAMPANA_MS, self._sincronizacion_automatica
            )
        except tk.TclError:
            self._after_sync = None

    def _sincronizacion_automatica(self) -> None:
        self._after_sync = None
        if self._cerrando or self.pantalla_actual != "campana" or self._campana_activa:
            return
        if self._sincronizando or self._inicio_pendiente:
            self._programar_sincronizacion()
            return
        if self._ventana_ajustes is not None:
            self._programar_sincronizacion()
            return
        self._sincronizar_perfil(manual=False, iniciar=False)

    def _sincronizar_manual(self) -> None:
        if self._sincronizando:
            self._mensaje_sync("Actualizando configuración...")
            return
        self._sincronizar_perfil(manual=True, iniciar=False)

    def _sincronizar_perfil(
        self, manual: bool = False, iniciar: bool = False
    ) -> bool:
        if self._sincronizando:
            return False
        if self.cliente is None or not self.token:
            mensaje = (
                "Falta configurar el servidor. Pídele la dirección a tu "
                "administrador."
            )
            self._mensaje_sync(mensaje, error=True)
            self._inicio_pendiente = False
            self._programar_sincronizacion()
            return False
        self._sincronizando = True
        if manual or iniciar:
            self._mensaje_sync("Actualizando configuración desde el panel...")
        self._hilo(self._worker_sincronizar_perfil, bool(manual), bool(iniciar))
        return True

    def _worker_sincronizar_perfil(self, manual: bool, iniciar: bool) -> None:
        """Pide `/v1/perfil` fuera del hilo de Tk y encola el resultado."""
        token = str(self.token or "")
        try:
            perfil = self.cliente.perfil(token) if self.cliente else None
        except Exception as error:
            if token and self.token == token:
                self._cola.put(
                    (
                        "sync_error",
                        {
                            "manual": bool(manual),
                            "iniciar": bool(iniciar),
                            "error": mensaje_amigable(error),
                        },
                    )
                )
            return
        if not token or self.token != token:
            return
        if not isinstance(perfil, dict) or not perfil:
            self._cola.put(
                (
                    "sync_error",
                    {
                        "manual": bool(manual),
                        "iniciar": bool(iniciar),
                        "error": (
                            "El servidor no devolvió la configuración de tu "
                            "campaña."
                        ),
                    },
                )
            )
            return
        try:
            store.sincronizar_perfil(perfil)
        except Exception as error:
            if self.token == token:
                self._cola.put(
                    (
                        "sync_error",
                        {
                            "manual": bool(manual),
                            "iniciar": bool(iniciar),
                            "error": (
                                "No se pudieron actualizar las cuentas locales. "
                                + mensaje_amigable(error)
                            ),
                        },
                    )
                )
            return
        if self.token != token:
            return
        self._cola.put(
            (
                "perfil_sincronizado",
                {
                    "perfil": perfil,
                    "manual": bool(manual),
                    "iniciar": bool(iniciar),
                },
            )
        )

    def _aplicar_perfil_actualizado(
        self, perfil: dict, iniciar: bool = False, manual: bool = False
    ) -> None:
        """Reemplaza el perfil local por el del panel y repinta si cambió."""
        campana = (perfil or {}).get("campana") or {}
        if not campana.get("permitir_ajustes"):
            self.ajustes = {}
        firma_anterior = self._sync_firma
        self.perfil = perfil
        self._sync_firma = _firma_perfil(perfil)
        self._sync_hora = time.strftime("%H:%M:%S")
        cambio = bool(firma_anterior) and self._sync_firma != firma_anterior
        if iniciar:
            self._inicio_pendiente = False
            if self.pantalla_actual == "campana":
                self._iniciar_campana_ahora()
            return
        if self.pantalla_actual == "campana":
            if cambio:
                self.mostrar_campana(
                    aviso="Configuración actualizada desde el panel."
                )
            else:
                self._mensaje_sync(
                    f"Sin cambios · {self._sync_hora}"
                    + (
                        " · Ajustes locales activos"
                        if self.ajustes and campana.get("permitir_ajustes")
                        else ""
                    )
                )
        self._programar_sincronizacion()

    def _abrir_ajustes(self) -> None:
        if self._ventana_ajustes is not None:
            try:
                self._ventana_ajustes.lift()
                return
            except tk.TclError:
                self._ventana_ajustes = None

        ventana = tk.Toplevel(self.root)
        self._ventana_ajustes = ventana
        ventana.title("Ajustes de la campaña")
        ventana.configure(bg=theme.FONDO)
        ventana.transient(self.root)
        ventana.grab_set()
        ventana.resizable(False, False)

        campana = runner.config_efectiva(self.perfil, self.ajustes)
        marco = tk.Frame(ventana, bg=theme.FONDO, padx=26, pady=22)
        marco.pack(fill="both", expand=True)
        tk.Label(
            marco,
            text="Ajustes de la campaña",
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 15, "bold"),
        ).pack(anchor="w")
        tk.Label(
            marco,
            text="Solo cambia estos valores si tu administrador te lo indicó.",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 9),
        ).pack(anchor="w", pady=(4, 14))

        variables = {}

        def campo(texto: str, clave: str, desde: int, hasta: int) -> None:
            fila = tk.Frame(marco, bg=theme.FONDO)
            fila.pack(fill="x", pady=5)
            tk.Label(
                fila,
                text=texto,
                bg=theme.FONDO,
                fg=theme.TEXTO,
                font=(self.familia, 10),
                width=32,
                anchor="w",
            ).pack(side="left")
            variable = tk.StringVar(value=str(campana.get(clave, desde)))
            variables[clave] = variable
            tk.Spinbox(
                fila,
                from_=desde,
                to=hasta,
                textvariable=variable,
                width=8,
                bg=theme.SUPERFICIE_ALT,
                fg=theme.TEXTO,
                buttonbackground=theme.SUPERFICIE,
                insertbackground=theme.ACENTO,
                relief="flat",
                font=(self.familia, 10),
                highlightthickness=1,
                highlightbackground=theme.BORDE,
            ).pack(side="right")

        campo("Publicaciones por cuenta", "acciones_por_cuenta", 1, 49)
        campo("Pausa entre publicaciones (seg)", "pausa_seg", 1, 120)
        campo("Duración máxima (minutos)", "duracion_min", 1, 180)
        campo("Tope diario por cuenta", "limite_dia", 1, 49)

        imagenes = list(self.imagenes_extra)
        lbl_imagenes = tk.Label(
            marco,
            text=self._texto_imagenes(imagenes),
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 9),
        )
        lbl_imagenes.pack(anchor="w", pady=(10, 4))

        def agregar_imagenes() -> None:
            rutas = filedialog.askopenfilenames(
                parent=ventana,
                title="Agregar imágenes",
                filetypes=[
                    ("Imágenes", "*.png *.jpg *.jpeg *.gif *.webp"),
                    ("Todos los archivos", "*.*"),
                ],
            )
            for ruta in rutas or []:
                if ruta not in imagenes:
                    imagenes.append(ruta)
            lbl_imagenes.configure(text=self._texto_imagenes(imagenes))

        tk.Button(
            marco,
            text="Agregar imágenes",
            command=agregar_imagenes,
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO,
            activebackground=theme.SUPERFICIE_ALT,
            activeforeground=theme.TEXTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 10),
            padx=14,
            pady=6,
        ).pack(anchor="w")

        def guardar() -> None:
            valores = {}
            for clave, variable in variables.items():
                try:
                    valores[clave] = int(float(variable.get().strip()))
                except (TypeError, ValueError):
                    messagebox.showerror(
                        "MWX APP",
                        "Revisa los números: deben ser valores válidos.",
                        parent=ventana,
                    )
                    return
            self.ajustes = valores
            self.imagenes_extra = list(imagenes)
            self._cerrar_ajustes()
            self._actualizar_resumen_campana()

        botones = tk.Frame(marco, bg=theme.FONDO)
        botones.pack(fill="x", pady=(18, 0))

        def usar_config_panel() -> None:
            self.ajustes = {}
            self.imagenes_extra = []
            self._cerrar_ajustes()
            self.mostrar_campana(
                aviso="Se aplicó la configuración del panel."
            )

        tk.Button(
            botones,
            text="Usar configuración del panel",
            command=usar_config_panel,
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO_SEC,
            activebackground=theme.SUPERFICIE_ALT,
            activeforeground=theme.TEXTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 9),
            padx=12,
            pady=7,
        ).pack(side="left")
        theme.boton(
            botones,
            "Guardar",
            guardar,
            color=theme.ACENTO,
            color_hover=theme.ACENTO_HOVER,
            familia=self.familia,
            tamano=11,
            padx=18,
            pady=7,
        ).pack(side="right")
        tk.Button(
            botones,
            text="Cancelar",
            command=self._cerrar_ajustes,
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO_SEC,
            activebackground=theme.SUPERFICIE_ALT,
            activeforeground=theme.TEXTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 10),
            padx=16,
            pady=7,
        ).pack(side="right", padx=(0, 8))
        ventana.protocol("WM_DELETE_WINDOW", self._cerrar_ajustes)
        ventana.bind("<Escape>", lambda _e: self._cerrar_ajustes())

    def _texto_imagenes(self, imagenes: list) -> str:
        if not imagenes:
            return "Sin imágenes adicionales."
        return f"{len(imagenes)} imagen(es) adicionales."

    def _cerrar_ajustes(self) -> None:
        ventana = self._ventana_ajustes
        self._ventana_ajustes = None
        if ventana is not None:
            try:
                ventana.grab_release()
            except tk.TclError:
                pass
            try:
                ventana.destroy()
            except tk.TclError:
                pass

    def _iniciar_campana(self) -> None:
        if self._campana_activa or not self.perfil:
            return
        if self._inicio_pendiente or self._sincronizando:
            messagebox.showinfo(
                "MWX APP",
                "Espera un momento: se está actualizando la configuración.",
            )
            return
        if not getattr(self, "_puede_iniciar", True):
            messagebox.showinfo(
                "MWX APP",
                "La campaña todavía no está lista. Contacta a tu administrador.",
            )
            return
        self._inicio_pendiente = True
        if not self._sincronizar_perfil(manual=True, iniciar=True):
            self._inicio_pendiente = False
            messagebox.showinfo(
                "MWX APP",
                "No se pudo actualizar la configuración. Revisa tu conexión "
                "e inténtalo de nuevo.",
            )

    def _iniciar_campana_ahora(self) -> None:
        if self._campana_activa or not self.perfil:
            self._inicio_pendiente = False
            return
        campana = runner.config_efectiva(self.perfil, self.ajustes)
        cuentas = self.perfil.get("cuentas") or []
        puede = bool(cuentas) and bool(
            campana["hashtags"]
            or campana["contexto"]
            or campana["narrativa"]
            or campana["urls"]
        )
        if not puede:
            self._inicio_pendiente = False
            messagebox.showinfo(
                "MWX APP",
                "La campaña todavía no está lista. Contacta a tu administrador.",
            )
            return
        if not self._verificar_actualizacion_obligatoria():
            self._inicio_pendiente = False
            return

        self._inicio_pendiente = False
        self.cancelar = threading.Event()
        self._eventos = []
        self._resumen = None
        self._inicio_ts = time.monotonic()
        self._campana_activa = True
        self.mostrar_en_curso()
        self._hilo(self._worker_campana)

    def mostrar_en_curso(self) -> None:
        self._limpiar()
        self.pantalla_actual = "en_curso"

        campana = runner.config_efectiva(self.perfil, self.ajustes)
        cuentas = self.perfil.get("cuentas") or []
        total_estimado = len(cuentas) * int(
            campana.get("acciones_por_cuenta") or 10
        )

        centro = tk.Frame(self.contenedor, bg=theme.FONDO)
        centro.pack(fill="both", expand=True, padx=60, pady=40)

        tk.Label(
            centro,
            text="Campaña en curso",
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            centro,
            text="No cierres esta ventana. Puedes seguir el avance aquí.",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
        ).pack(anchor="w", pady=(4, 20))

        self._barra_total = total_estimado
        if total_estimado > 0:
            self._barra_modo = "determinate"
            self._barra = ttk.Progressbar(
                centro,
                orient="horizontal",
                mode="determinate",
                maximum=total_estimado,
                value=0,
            )
        else:
            self._barra_modo = "indeterminate"
            self._barra = ttk.Progressbar(
                centro, orient="horizontal", mode="indeterminate"
            )
        self._barra.pack(fill="x")
        if self._barra_modo == "indeterminate":
            self._barra.start(15)

        fila = tk.Frame(centro, bg=theme.FONDO)
        fila.pack(fill="x", pady=(10, 0))
        self._lbl_progreso = tk.Label(
            fila,
            text="Preparando la campaña...",
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 12, "bold"),
        )
        self._lbl_progreso.pack(side="left")
        self._lbl_tiempo = tk.Label(
            fila,
            text="⏱ 00:00",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 11),
        )
        self._lbl_tiempo.pack(side="right")

        tarjeta = self._tarjeta(centro)
        tarjeta.pack(fill="x", pady=(18, 18))
        tk.Label(
            tarjeta,
            text="Últimas publicaciones",
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO,
            font=(self.familia, 11, "bold"),
        ).pack(anchor="w")
        self._lbl_eventos = []
        for _ in range(5):
            etiqueta = tk.Label(
                tarjeta,
                text="",
                bg=theme.SUPERFICIE,
                fg=theme.TEXTO_SEC,
                font=(self.familia, 10),
                anchor="w",
            )
            etiqueta.pack(fill="x", pady=2)
            self._lbl_eventos.append(etiqueta)

        self._lbl_estado_corrida = tk.Label(
            centro,
            text="La campaña se está ejecutando.",
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
        )
        self._lbl_estado_corrida.pack(anchor="w", pady=(0, 14))

        self._boton_detener = theme.boton(
            centro,
            "⛔  Detener",
            self._detener_campana,
            color=theme.PELIGRO,
            color_hover=theme.PELIGRO_HOVER,
            color_texto="#FFFFFF",
            familia=self.familia,
            tamano=13,
            padx=28,
            pady=10,
        )
        self._boton_detener.pack(anchor="w")

        self._tick_tiempo()

    def _tick_tiempo(self) -> None:
        if not self._campana_activa:
            return
        etiqueta = getattr(self, "_lbl_tiempo", None)
        if etiqueta is not None:
            try:
                etiqueta.configure(
                    text=f"⏱ {_formato_tiempo(time.monotonic() - self._inicio_ts)}"
                )
            except tk.TclError:
                return
        try:
            self._after_tiempo = self.root.after(1000, self._tick_tiempo)
        except tk.TclError:
            self._after_tiempo = None

    def _aplicar_evento(self, evento: dict) -> None:
        if not isinstance(evento, dict):
            return
        self._eventos.append(dict(evento))
        if len(self._eventos) > 20:
            self._eventos = self._eventos[-20:]

        try:
            hechas = int(evento.get("hechas") or 0)
            total = int(evento.get("total") or 0)
        except (TypeError, ValueError):
            hechas, total = 0, 0

        if total > 0:
            if self._barra_modo != "determinate":
                self._barra.stop()
                self._barra.configure(mode="determinate")
                self._barra_modo = "determinate"
            self._barra.configure(maximum=total, value=min(hechas, total))
            self._barra_total = total
            self._lbl_progreso.configure(
                text=f"Publicando: {hechas} de {total}"
            )
        elif self._barra_modo == "determinate":
            self._barra.configure(value=hechas)

        self._actualizar_eventos()

    def _actualizar_eventos(self) -> None:
        recientes = list(reversed(self._eventos[-5:]))
        for indice, etiqueta in enumerate(getattr(self, "_lbl_eventos", [])):
            if indice >= len(recientes):
                etiqueta.configure(text="")
                continue
            evento = recientes[indice]
            icono = "✅" if evento.get("ok") else "❌"
            usuario = str(evento.get("usuario") or "Cuenta")
            detalle = _detalle_amigable(evento)
            etiqueta.configure(
                text=f"{icono} @{usuario} · {_corto(detalle, 64)}",
                fg=theme.EXITO if evento.get("ok") else theme.ERROR,
            )

    def _detener_campana(self) -> None:
        if not self._campana_activa or self.cancelar is None:
            return
        confirmar = messagebox.askyesno(
            "Detener campaña",
            "¿Quieres detener la campaña?\n\n"
            "Se conservarán las publicaciones que ya se hicieron.",
            parent=self.root,
        )
        if not confirmar:
            return
        self.cancelar.set()
        try:
            self._boton_detener.configure(state="disabled", text="Deteniendo...")
            self._lbl_estado_corrida.configure(
                text="Deteniendo la campaña. Espera unos segundos."
            )
        except tk.TclError:
            pass

    def _worker_campana(self) -> None:
        try:
            resumen = runner.ejecutar_campana(
                self.perfil,
                self.token,
                self.api_url,
                ajustes=dict(self.ajustes),
                imagenes_extra=list(self.imagenes_extra),
                cancelar=self.cancelar,
                callback=self._callback_campana,
                cliente=self.cliente,
            )
        except Exception as error:
            self._registrar_error(error)
            resumen = {
                "ok": False,
                "error": mensaje_amigable(error),
                "exitosas": 0,
                "fallidas": 0,
                "omitidas": 0,
                "planificadas": 0,
                "hechas": 0,
                "cancelada": False,
                "eventos": [],
            }
        self._cola.put(("campana_fin", resumen))

    def _callback_campana(self, evento: dict) -> None:
        try:
            self._cola.put(("campana_evento", dict(evento or {})))
        except Exception:
            pass

    def mostrar_fin(self, resumen: dict) -> None:
        self._limpiar()
        self.pantalla_actual = "fin"
        self._resumen = resumen
        self._links = []

        exitosas = int(resumen.get("exitosas") or 0)
        fallidas = int(resumen.get("fallidas") or 0)
        omitidas = int(
            resumen.get("omitidas")
            or max(
                0,
                int(resumen.get("planificadas") or 0)
                - int(resumen.get("hechas") or 0),
            )
        )
        cancelada = bool(resumen.get("cancelada"))
        error = (
            resumen.get("ok") is False
            and int(resumen.get("hechas") or 0) == 0
        )

        if error:
            titulo = "⚠️ No se pudo completar la campaña"
            subtitulo = _mensaje_error_campana(resumen)
        elif cancelada:
            titulo = "⛔ Campaña detenida"
            subtitulo = "Se conservaron las publicaciones que ya se hicieron."
        else:
            titulo = "✅ Campaña terminada"
            subtitulo = "Todo salió bien. Aquí está el resumen."

        centro = tk.Frame(self.contenedor, bg=theme.FONDO)
        centro.pack(fill="both", expand=True, padx=44, pady=(28, 18))
        tk.Label(
            centro,
            text=titulo,
            bg=theme.FONDO,
            fg=theme.TEXTO,
            font=(self.familia, 21, "bold"),
        ).pack(anchor="w")
        tk.Label(
            centro,
            text=subtitulo,
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
            wraplength=780,
            justify="left",
        ).pack(anchor="w", pady=(4, 16))

        metricas = tk.Frame(centro, bg=theme.FONDO)
        metricas.pack(fill="x", pady=(0, 14))
        self._tarjeta_metrica(metricas, "Exitosas", exitosas, theme.EXITO)
        self._tarjeta_metrica(metricas, "Fallidas", fallidas, theme.PELIGRO)
        self._tarjeta_metrica(metricas, "Omitidas", omitidas, theme.SECUNDARIO)

        enlaces = []
        for evento in resumen.get("eventos") or []:
            if not isinstance(evento, dict) or not evento.get("ok"):
                continue
            url = str(evento.get("url") or "").strip()
            if not url:
                continue
            enlaces.append(
                {
                    "usuario": str(evento.get("usuario") or ""),
                    "tipo": _etiqueta_rol(evento.get("rol")),
                    "link": url,
                }
            )
        self._links = [fila["link"] for fila in enlaces]

        tarjeta = self._tarjeta(centro)
        tarjeta.pack(fill="both", expand=True, pady=(0, 14))
        tk.Label(
            tarjeta,
            text="Publicaciones con enlace",
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO,
            font=(self.familia, 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        if enlaces:
            contenedor_tabla = tk.Frame(tarjeta, bg=theme.SUPERFICIE)
            contenedor_tabla.pack(fill="both", expand=True)
            tabla = ttk.Treeview(
                contenedor_tabla,
                columns=("usuario", "tipo", "link"),
                show="headings",
                height=6,
            )
            tabla.heading("usuario", text="Cuenta")
            tabla.heading("tipo", text="Tipo")
            tabla.heading("link", text="Enlace")
            tabla.column("usuario", width=150, anchor="w")
            tabla.column("tipo", width=120, anchor="w")
            tabla.column("link", width=430, anchor="w")
            desplazamiento = ttk.Scrollbar(
                contenedor_tabla, orient="vertical", command=tabla.yview
            )
            tabla.configure(yscrollcommand=desplazamiento.set)
            tabla.pack(side="left", fill="both", expand=True)
            desplazamiento.pack(side="right", fill="y")
            for fila in enlaces:
                tabla.insert(
                    "",
                    "end",
                    values=(fila["usuario"], fila["tipo"], fila["link"]),
                )
        else:
            tk.Label(
                tarjeta,
                text="No se capturaron enlaces.",
                bg=theme.SUPERFICIE,
                fg=theme.TEXTO_SEC,
                font=(self.familia, 10),
            ).pack(anchor="w", pady=(6, 0))

        self._lbl_copiado = tk.Label(
            centro,
            text="",
            bg=theme.FONDO,
            fg=theme.EXITO,
            font=(self.familia, 10),
        )
        self._lbl_copiado.pack(anchor="w")

        botones = tk.Frame(centro, bg=theme.FONDO)
        botones.pack(fill="x", pady=(4, 0))
        self._boton_copiar = theme.boton(
            botones,
            "📋  Copiar enlaces",
            self._copiar_enlaces,
            color=theme.SECUNDARIO,
            color_hover=theme.SECUNDARIO_HOVER,
            color_texto="#FFFFFF",
            familia=self.familia,
            tamano=11,
            padx=18,
            pady=9,
        )
        self._boton_copiar.pack(side="left")
        if not self._links:
            self._boton_copiar.configure(state="disabled", bg=theme.SUPERFICIE_ALT)

        theme.boton(
            botones,
            "▶  Volver a iniciar",
            self.mostrar_campana,
            color=theme.EXITO,
            color_hover=theme.EXITO_HOVER,
            familia=self.familia,
            tamano=11,
            padx=18,
            pady=9,
        ).pack(side="left", padx=10)

        tk.Button(
            botones,
            text="Cerrar sesión",
            command=self._cerrar_sesion,
            bg=theme.FONDO,
            fg=theme.TEXTO_SEC,
            activebackground=theme.FONDO,
            activeforeground=theme.TEXTO,
            relief="flat",
            bd=0,
            cursor="hand2",
            font=(self.familia, 10),
        ).pack(side="right")

        if error:
            messagebox.showerror(
                "MWX APP",
                f"{subtitulo}\n\nSugerencia: revisa tu conexión a internet "
                "y vuelve a intentarlo. Si el problema continúa, contacta a tu administrador.",
                parent=self.root,
            )

    def _tarjeta_metrica(
        self, padre: tk.Misc, titulo: str, valor: int, color: str
    ) -> None:
        marco = tk.Frame(
            padre,
            bg=theme.SUPERFICIE,
            highlightbackground=theme.BORDE,
            highlightthickness=1,
            padx=18,
            pady=12,
        )
        marco.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tk.Label(
            marco,
            text=str(int(valor)),
            bg=theme.SUPERFICIE,
            fg=color,
            font=(self.familia, 24, "bold"),
        ).pack(anchor="w")
        tk.Label(
            marco,
            text=titulo,
            bg=theme.SUPERFICIE,
            fg=theme.TEXTO_SEC,
            font=(self.familia, 10),
        ).pack(anchor="w")

    def _copiar_enlaces(self) -> None:
        if not self._links:
            messagebox.showinfo(
                "MWX APP", "No se capturaron enlaces en esta campaña.", parent=self.root
            )
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(self._links))
            self.root.update_idletasks()
            self._lbl_copiado.configure(text="Enlaces copiados.")
        except tk.TclError:
            messagebox.showerror(
                "MWX APP",
                "No se pudieron copiar los enlaces. Inténtalo de nuevo.",
                parent=self.root,
            )

    def _cerrar_sesion(self) -> None:
        if self._campana_activa:
            messagebox.showinfo(
                "MWX APP", "Primero detén la campaña en curso.", parent=self.root
            )
            return
        self.token = ""
        self.perfil = {}
        self.usuario = ""
        self.nombre = ""
        self.ajustes = {}
        self.imagenes_extra = []
        self.cancelar = None
        self._sincronizando = False
        self._inicio_pendiente = False
        self._sync_firma = ""
        self._sync_hora = ""
        config.borrar_sesion()
        runner.limpiar_token()
        self.mostrar_login()

    def _al_cerrar(self) -> None:
        if self._campana_activa:
            confirmar = messagebox.askyesno(
                "Cerrar MWX APP",
                "Hay una campaña en curso. ¿Quieres cerrar de todos modos?",
                parent=self.root,
            )
            if not confirmar:
                return
            if self.cancelar is not None:
                self.cancelar.set()
        self.detener()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def detener(self) -> None:
        """Detiene los temporizadores internos (util en tests y al cerrar)."""
        self._cerrando = True
        for identificador in (
            self._after_cola,
            self._after_tiempo,
            self._after_sync,
        ):
            if identificador is None:
                continue
            try:
                self.root.after_cancel(identificador)
            except tk.TclError:
                pass
        self._after_cola = None
        self._after_tiempo = None
        self._after_sync = None

    def _registrar_error(self, error: Any = None) -> None:
        try:
            if error is not None and getattr(error, "__traceback__", None) is not None:
                texto = "".join(
                    traceback.format_exception(
                        type(error), error, error.__traceback__
                    )
                )
            else:
                texto = traceback.format_exc()
            if self.token:
                texto = texto.replace(self.token, "***")
            ruta = config.ruta_datos("logs/mwx_app.log")
            ruta.parent.mkdir(parents=True, exist_ok=True)
            with ruta.open("a", encoding="utf-8") as archivo:
                archivo.write(
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {texto}\n"
                )
        except Exception:
            pass

    def _reportar_error_tk(self, tipo, valor, rastro) -> None:
        self._registrar_error(valor)
        try:
            messagebox.showerror(
                "MWX APP",
                "Ocurrió un problema inesperado.\n\n"
                "Inténtalo de nuevo. Si sigue pasando, contacta a tu administrador.",
                parent=self.root,
            )
        except tk.TclError:
            pass


def ejecutar_app() -> None:
    """Arranca la ventana principal (bloqueante)."""
    root = tk.Tk()
    app = MWXApp(root)
    if getattr(app, "_terminado", False):
        return
    try:
        root.mainloop()
    finally:
        try:
            app.detener()
        except Exception:
            pass

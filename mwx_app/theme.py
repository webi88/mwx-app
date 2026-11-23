# -*- coding: utf-8 -*-
"""Tema visual oscuro de MWX APP (Tkinter + ttk)."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

FONDO = "#0A0E13"
SUPERFICIE = "#11161D"
SUPERFICIE_ALT = "#17202A"
BORDE = "#22303C"
TEXTO = "#E6EDF3"
TEXTO_SEC = "#9AA7B4"
ACENTO = "#00B4C4"
ACENTO_HOVER = "#00C8DB"
SECUNDARIO = "#7C5CFF"
SECUNDARIO_HOVER = "#9178FF"
EXITO = "#2DD4A7"
EXITO_HOVER = "#3EE5B8"
PELIGRO = "#EF4444"
PELIGRO_HOVER = "#F86B6B"
ERROR = "#F87171"

FAMILIA_PREFERIDA = "Segoe UI"


def elegir_familia(root: tk.Misc) -> str:
    """Devuelve Segoe UI si existe; si no, una alternativa legible."""
    try:
        disponibles = set(tkfont.families(root))
    except tk.TclError:
        disponibles = set()
    for candidata in (FAMILIA_PREFERIDA, "DejaVu Sans", "Arial", "Helvetica"):
        if candidata in disponibles:
            return candidata
    return "TkDefaultFont"


def aplicar_tema(root: tk.Misc) -> str:
    """Configura ttk y devuelve la familia de fuente elegida."""
    familia = elegir_familia(root)
    try:
        root.configure(bg=FONDO)
    except tk.TclError:
        pass

    estilo = ttk.Style(root)
    try:
        estilo.theme_use("clam")
    except tk.TclError:
        pass

    estilo.configure(
        ".", background=FONDO, foreground=TEXTO, font=(familia, 10)
    )
    estilo.configure("TFrame", background=FONDO)
    estilo.configure("Card.TFrame", background=SUPERFICIE)
    estilo.configure(
        "TLabel", background=FONDO, foreground=TEXTO, font=(familia, 10)
    )
    estilo.configure(
        "Card.TLabel", background=SUPERFICIE, foreground=TEXTO, font=(familia, 10)
    )
    estilo.configure(
        "Muted.TLabel", background=FONDO, foreground=TEXTO_SEC, font=(familia, 10)
    )
    estilo.configure(
        "MutedCard.TLabel",
        background=SUPERFICIE,
        foreground=TEXTO_SEC,
        font=(familia, 10),
    )
    estilo.configure(
        "Titulo.TLabel",
        background=FONDO,
        foreground=TEXTO,
        font=(familia, 26, "bold"),
    )
    estilo.configure(
        "Subtitulo.TLabel",
        background=FONDO,
        foreground=TEXTO,
        font=(familia, 16, "bold"),
    )
    estilo.configure(
        "TEntry",
        fieldbackground=SUPERFICIE_ALT,
        foreground=TEXTO,
        insertcolor=ACENTO,
        bordercolor=BORDE,
        lightcolor=BORDE,
        darkcolor=BORDE,
        padding=8,
    )
    estilo.map(
        "TEntry",
        fieldbackground=[("disabled", SUPERFICIE)],
        foreground=[("disabled", TEXTO_SEC)],
    )
    estilo.configure(
        "TCheckbutton",
        background=FONDO,
        foreground=TEXTO_SEC,
        font=(familia, 10),
    )
    estilo.map(
        "TCheckbutton",
        background=[("active", FONDO)],
        foreground=[("active", TEXTO)],
    )
    estilo.configure(
        "Treeview",
        background=SUPERFICIE,
        fieldbackground=SUPERFICIE,
        foreground=TEXTO,
        rowheight=28,
        borderwidth=0,
        font=(familia, 10),
    )
    estilo.configure(
        "Treeview.Heading",
        background=SUPERFICIE_ALT,
        foreground=TEXTO_SEC,
        relief="flat",
        font=(familia, 10, "bold"),
    )
    estilo.map(
        "Treeview",
        background=[("selected", "#1D3A46")],
        foreground=[("selected", TEXTO)],
    )
    estilo.configure(
        "Horizontal.TProgressbar",
        troughcolor=SUPERFICIE_ALT,
        background=ACENTO,
        bordercolor=SUPERFICIE_ALT,
        lightcolor=ACENTO,
        darkcolor=ACENTO,
        thickness=14,
    )
    return familia


def boton(
    padre: tk.Misc,
    texto: str,
    comando,
    color: str = ACENTO,
    color_hover: str = ACENTO_HOVER,
    color_texto: str = "#04161A",
    familia: str = FAMILIA_PREFERIDA,
    tamano: int = 13,
    padx: int = 24,
    pady: int = 10,
    **opciones,
) -> tk.Button:
    """Crea un boton plano con colores explicitos (fiable en Windows)."""
    widget = tk.Button(
        padre,
        text=texto,
        command=comando,
        bg=color,
        fg=color_texto,
        activebackground=color_hover,
        activeforeground=color_texto,
        disabledforeground="#6B7B88",
        relief="flat",
        bd=0,
        cursor="hand2",
        font=(familia, tamano, "bold"),
        padx=padx,
        pady=pady,
        **opciones,
    )

    def _entrar(_evento=None):
        if str(widget["state"]) != "disabled":
            widget.configure(bg=color_hover)

    def _salir(_evento=None):
        if str(widget["state"]) != "disabled":
            widget.configure(bg=color)

    widget.bind("<Enter>", _entrar, add="+")
    widget.bind("<Leave>", _salir, add="+")
    return widget

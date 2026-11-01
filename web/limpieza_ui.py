"""Limpieza visual de iconitos (emojis) en TODO el dashboard.

Envuelve una vez las funciones de Streamlit para que los emojis NO se muestren,
sin cambiar los valores internos (claves de navegación, opciones de radio/select,
rutas `?op=`/`?tab=`): el emoji se quita solo del TEXTO que ve el usuario.

- Widgets que devuelven la opción elegida (radio/selectbox/multiselect/
  segmented_control/pills): se les inyecta `format_func` para mostrar sin emoji
  y se limpia su etiqueta; el VALOR devuelto conserva el emoji (no rompe rutas).
- Widgets de solo texto (tabs/button/subheader/markdown/caption/…): se limpia el
  texto mostrado.
- Se respetan los bloques HTML (`unsafe_allow_html`) para no alterar el CSS ni
  las tarjetas.

Se instala UNA sola vez desde `web/app.py` (idempotente).
"""
import re
import streamlit as st

_RE_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF←-⇿⬅️⃣]"
)


def _limpiar(texto):
    """Quita emojis de un texto de display. No toca '-'/'·' (markdown)."""
    if not isinstance(texto, str):
        return texto
    t = _RE_EMOJI.sub("", texto)
    t = re.sub(r"[ \t]{2,}", " ", t)
    # Espacios huérfanos antes de signos tras quitar el emoji.
    t = t.replace(" :", ":").replace(" ·", " ·").replace("· ", "· ")
    # Quita un "·"/espacio inicial que haya quedado suelto.
    t = re.sub(r"^[ ·]+", "", t)
    return t.strip(" ")


def _es_html(args, kwargs) -> bool:
    if kwargs.get("unsafe_allow_html"):
        return True
    if args and isinstance(args[0], str) and "<" in args[0] and ">" in args[0]:
        return True
    return False


def _limpiar_primer_arg(func):
    """Envuelve una func cuyo primer arg posicional es texto de display."""
    def wrapper(*args, **kwargs):
        if args and isinstance(args[0], str) and not _es_html(args, kwargs):
            args = (_limpiar(args[0]),) + args[1:]
        return func(*args, **kwargs)
    return wrapper


def _limpiar_tabs(func):
    def wrapper(labels, *args, **kwargs):
        try:
            labels = [_limpiar(x) if isinstance(x, str) else x for x in labels]
        except Exception:
            pass
        return func(labels, *args, **kwargs)
    return wrapper


def _limpiar_opciones(func):
    """Widgets que devuelven la opción: display limpio, valor intacto."""
    def wrapper(*args, **kwargs):
        # Etiqueta del widget (primer arg) también sin emoji.
        if args and isinstance(args[0], str):
            args = (_limpiar(args[0]),) + args[1:]
        prev = kwargs.get("format_func")
        if prev is None:
            kwargs["format_func"] = lambda v: _limpiar(v) if isinstance(v, str) else v
        else:
            kwargs["format_func"] = lambda v, _p=prev: _limpiar(_p(v))
        return func(*args, **kwargs)
    return wrapper


_INSTALADO = False


def instalar_limpieza_emojis():
    """Instala los wrappers una sola vez (idempotente, tolerante a fallos)."""
    global _INSTALADO
    if _INSTALADO:
        return
    try:
        # Solo texto de display (primer arg).
        for nombre in (
            "button", "download_button", "checkbox", "toggle",
            "subheader", "header", "title", "markdown", "caption",
            "info", "success", "warning", "error", "expander",
        ):
            if hasattr(st, nombre):
                setattr(st, nombre, _limpiar_primer_arg(getattr(st, nombre)))

        # Pestañas: lista de etiquetas.
        if hasattr(st, "tabs"):
            st.tabs = _limpiar_tabs(st.tabs)

        # Widgets que devuelven la opción elegida (valor intacto).
        for nombre in (
            "radio", "selectbox", "multiselect", "segmented_control",
            "pills", "form_submit_button",
        ):
            if hasattr(st, nombre):
                if nombre == "form_submit_button":
                    setattr(st, nombre, _limpiar_primer_arg(getattr(st, nombre)))
                else:
                    setattr(st, nombre, _limpiar_opciones(getattr(st, nombre)))

        _INSTALADO = True
    except Exception:
        # Nunca tumbar el dashboard por la limpieza visual.
        _INSTALADO = True

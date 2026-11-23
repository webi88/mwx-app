# -*- coding: utf-8 -*-
"""Genera el icono multirresolucion y el logo de MWX APP.

Uso:
    python assets/generar_icono.py

Produce (en esta misma carpeta):
    mwx_app.ico   -> 16, 24, 32, 48, 64, 128 y 256 px
    mwx_logo.png  -> 512 x 512 px

Requiere Pillow (solo para compilar; el .ico generado ya queda en el repo).
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONDO = "#0A0E13"
BORDE = "#22303C"
TEXTO = "#FFFFFF"
ACENTO = "#00B4C4"
SECUNDARIO = "#7C5CFF"

TAMANO_BASE = 1024
TAMANO_PNG = 512
TAMANOS_ICO = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

DIRECTORIO = Path(__file__).resolve().parent


def _hex_rgb(color: str) -> tuple:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))


def _mezclar(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _fuente(tamano: int) -> ImageFont.FreeTypeFont:
    candidatas = [
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/seguisb.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "DejaVuSans-Bold.ttf",
        "LiberationSans-Bold.ttf",
    ]
    for ruta in candidatas:
        try:
            return ImageFont.truetype(ruta, size=tamano)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tamano)
    except TypeError:
        return ImageFont.load_default()


def _ajustar_fuente(dibujo: ImageDraw.ImageDraw, texto: str) -> ImageFont.FreeTypeFont:
    ancho_max = int(TAMANO_BASE * 0.62)
    alto_max = int(TAMANO_BASE * 0.34)
    tamano = int(TAMANO_BASE * 0.42)
    while tamano > 24:
        fuente = _fuente(tamano)
        caja = dibujo.textbbox((0, 0), texto, font=fuente)
        ancho = caja[2] - caja[0]
        alto = caja[3] - caja[1]
        if ancho <= ancho_max and alto <= alto_max:
            return fuente
        tamano -= 12
    return _fuente(24)


def _gradiente_diagonal(ancho: int, alto: int) -> Image.Image:
    imagen = Image.new("RGB", (ancho, alto))
    pixeles = imagen.load()
    inicio = _hex_rgb(ACENTO)
    fin = _hex_rgb(SECUNDARIO)
    denominador = max(1, ancho + alto - 2)
    for y in range(alto):
        for x in range(ancho):
            pixeles[x, y] = _mezclar(inicio, fin, (x + y) / denominador)
    return imagen


def crear_logo() -> Image.Image:
    base = Image.new("RGBA", (TAMANO_BASE, TAMANO_BASE), (0, 0, 0, 0))
    dibujo = ImageDraw.Draw(base)

    margen = int(TAMANO_BASE * 0.045)
    radio = int(TAMANO_BASE * 0.20)
    dibujo.rounded_rectangle(
        (margen, margen, TAMANO_BASE - margen, TAMANO_BASE - margen),
        radius=radio,
        fill=_hex_rgb(FONDO),
    )

    banda = Image.new("L", (TAMANO_BASE, TAMANO_BASE), 0)
    dibujo_banda = ImageDraw.Draw(banda)
    dibujo_banda.polygon(
        [
            (int(TAMANO_BASE * 0.08), int(TAMANO_BASE * 0.87)),
            (int(TAMANO_BASE * 0.22), int(TAMANO_BASE * 0.87)),
            (int(TAMANO_BASE * 0.92), int(TAMANO_BASE * 0.60)),
            (int(TAMANO_BASE * 0.78), int(TAMANO_BASE * 0.60)),
        ],
        fill=255,
    )

    mascara_tarjeta = Image.new("L", (TAMANO_BASE, TAMANO_BASE), 0)
    ImageDraw.Draw(mascara_tarjeta).rounded_rectangle(
        (margen, margen, TAMANO_BASE - margen, TAMANO_BASE - margen),
        radius=radio,
        fill=255,
    )
    mascara = Image.composite(banda, Image.new("L", banda.size, 0), mascara_tarjeta)
    gradiente = _gradiente_diagonal(TAMANO_BASE, TAMANO_BASE).convert("RGBA")
    gradiente.putalpha(mascara)
    base.alpha_composite(gradiente)

    dibujo = ImageDraw.Draw(base)
    dibujo.rounded_rectangle(
        (margen, margen, TAMANO_BASE - margen, TAMANO_BASE - margen),
        radius=radio,
        outline=_hex_rgb(BORDE),
        width=max(2, int(TAMANO_BASE * 0.006)),
    )

    fuente = _ajustar_fuente(dibujo, "MWX")
    dibujo.text(
        (TAMANO_BASE / 2, TAMANO_BASE * 0.46),
        "MWX",
        font=fuente,
        fill=_hex_rgb(TEXTO),
        anchor="mm",
    )

    return base


def main() -> int:
    logo = crear_logo()
    png = DIRECTORIO / "mwx_logo.png"
    ico = DIRECTORIO / "mwx_app.ico"
    logo.resize((TAMANO_PNG, TAMANO_PNG), Image.LANCZOS).save(png, format="PNG")
    logo.save(ico, format="ICO", sizes=TAMANOS_ICO)
    print(f"Generado: {png}")
    print(f"Generado: {ico} ({', '.join(str(s[0]) for s in TAMANOS_ICO)} px)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ImportError:
        print("Falta Pillow. Instala con: pip install pillow", file=sys.stderr)
        sys.exit(1)

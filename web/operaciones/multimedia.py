import streamlit as st
import os
from web.ui import cabecera


def render(usuario: dict):
    cabecera("🖼️ MULTIMEDIA", "Generación de imágenes y edición")
    
    tabs = st.tabs([
        "🎨 Generar Imagen con IA",
        "✏️ Texto sobre Imagen",
    ])
    
    with tabs[0]:
        _generar_imagen()
    with tabs[1]:
        _texto_sobre_imagen()


def _generar_imagen():
    st.markdown("### 🎨 Generar Imagen con IA")
    
    if "web_generar_imagen" in st.session_state:
        st.info(f"Prompt programado desde el sidebar: {st.session_state['web_generar_imagen']}")
    
    prompt = st.text_area(
        "Describe la imagen",
        height=100,
        key="mult_prompt",
        placeholder="Un paisaje urbano al amanecer con los colores de la bandera mexicana...",
    )
    
    if st.button("🎨 Generar Imagen", type="primary", key="btn_mult_generar"):
        if not prompt:
            st.warning("Escribe un prompt.")
            return
        
        from ia.generador_contenido import GeneradorContenido
        
        with st.spinner("Generando imagen con OpenAI..."):
            generador = GeneradorContenido()
            ruta = generador.generar_imagen(prompt)
        
        if "web_generar_imagen" in st.session_state:
            del st.session_state["web_generar_imagen"]
        
        if ruta and os.path.exists(ruta):
            st.image(ruta, caption="Imagen generada")
            st.success(f"Imagen guardada en: {ruta}")
            st.session_state["web_imagen_generada"] = ruta
        else:
            st.error("No se pudo generar la imagen.")


def _texto_sobre_imagen():
    st.markdown("### ✏️ Texto sobre Imagen")
    
    st.info("Aplica texto sobre una imagen base usando PIL.")
    
    ruta_imagen = st.text_input(
        "Ruta de la imagen base",
        key="txtov_ruta",
        placeholder="data/temp/imagen_generada.png",
    )
    
    texto = st.text_input("Texto a aplicar", key="txtov_texto")
    
    posiciones = ["Centro", "Arriba", "Abajo"]
    posicion = st.selectbox("Posición", posiciones, key="txtov_pos")
    
    tamaño = st.slider("Tamaño del texto", 30, 200, 90, key="txtov_size")
    
    if st.button("✏️ Aplicar Texto", type="primary", key="btn_txtov"):
        if not ruta_imagen or not texto:
            st.warning("Ruta y texto son obligatorios.")
            return
        if not os.path.exists(ruta_imagen):
            st.error("La imagen no existe en esa ruta.")
            return
        
        from PIL import Image, ImageDraw, ImageFont
        
        try:
            img = Image.open(ruta_imagen).convert("RGBA")
            draw = ImageDraw.Draw(img)
            w, h = img.size
            
            try:
                font = ImageFont.truetype("arial.ttf", tamaño)
            except Exception:
                font = ImageFont.load_default()
            
            bbox = draw.textbbox((0, 0), texto, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            
            if posicion == "Centro":
                pos = ((w - tw) // 2, (h - th) // 2)
            elif posicion == "Arriba":
                pos = ((w - tw) // 2, 20)
            else:
                pos = ((w - tw) // 2, h - th - 20)
            
            draw.text(pos, texto, font=font, fill=(255, 255, 255, 255),
                      stroke_width=3, stroke_fill=(0, 0, 0, 255))
            
            salida = "data/temp/imagen_con_texto.png"
            img.convert("RGB").save(salida)
            
            st.image(salida, caption="Imagen con texto")
            st.success(f"Guardada en: {salida}")
            st.session_state["web_imagen_generada"] = salida
        except Exception as e:
            st.error(f"Error: {e}")
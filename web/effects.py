"""Efectos JS aislados para el dashboard (reloj en vivo, indicador en línea).

Streamlit ELIMINA los `<script>` que se pasan por `st.markdown`, así que estos
micro-efectos se renderizan en un iframe propio con `st.components.v1.html`.
El componente es 100% autónomo: vive en su propio iframe, no toca el DOM padre
de Streamlit, no puede romper la navegación ni capturar estado del dashboard.

Se inyecta UNA sola vez desde `web/app.py` (barra de estado superior).
"""

import streamlit as st

# HTML/CSS/JS autocontenido. No depende de fuentes externas (fuente del sistema)
# para no añadir latencia ni peticiones de red en cada render.
_HTML = r"""
<div style="
    box-sizing:border-box;
    display:flex; align-items:center; gap:16px; flex-wrap:wrap;
    width:100%;
    padding:8px 4px;
    border-bottom:1px solid #e6ecee;
    background:transparent;
    font-family: 'Poppins', -apple-system, 'Segoe UI', sans-serif;
    color:#1d2a30;
">
    <div style="display:flex; align-items:center; gap:8px;">
        <span style="
            display:inline-block; width:8px; height:8px; border-radius:50%;
            background:linear-gradient(135deg,#1d8f97,#3cc8b4);
        "></span>
        <span style="font-weight:600; font-size:13px; letter-spacing:.2px;">MWX.app</span>
    </div>

    <div style="display:flex; align-items:center; gap:7px; margin-left:auto;">
        <span style="
            display:inline-block; width:7px; height:7px; border-radius:50%;
            background:#3cc8b4; box-shadow:0 0 0 0 rgba(60,200,180,.6);
            animation:mwPulse 1.8s ease-out infinite;
        "></span>
        <span style="font-size:12px; color:#6b7a80;">En línea</span>
    </div>

    <div style="display:flex; align-items:baseline; gap:10px;">
        <span id="mw-date" style="font-size:12px; color:#6b7a80;"></span>
        <span id="mw-clock" style="
            font-size:13px; font-weight:600;
            font-variant-numeric:tabular-nums; color:#1d2a30;
        ">--:--:--</span>
    </div>
</div>

<style>
    @keyframes mwPulse {
        0%   { box-shadow: 0 0 0 0 rgba(60,200,180,.6); }
        70%  { box-shadow: 0 0 0 8px rgba(60,200,180,0); }
        100% { box-shadow: 0 0 0 0 rgba(60,200,180,0); }
    }
</style>

<script>
(function () {
    var clock = document.getElementById('mw-clock');
    var date = document.getElementById('mw-date');
    if (!clock || !date) return;

    function pad(n) { return (n < 10 ? '0' : '') + n; }

    function tick() {
        var now = new Date();
        clock.textContent = pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds());
        // Pequeño "flash" sutil al cambiar de segundo = sensación de vida.
    }

    function fecha() {
        var now = new Date();
        var dias = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];
        var meses = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
        date.textContent = dias[now.getDay()] + ' · ' + pad(now.getDate()) + ' ' + meses[now.getMonth()] + ' · ' + now.getFullYear();
    }

    clock.style.transition = 'transform .18s ease';
    tick();
    fecha();
    window.setInterval(tick, 1000);
})();
</script>
"""


def inyectar_efectos(altura: int = 66):
    """Renderiza la barra de estado en vivo (reloj + indicador en línea).

    Envolvemos la inyección en try/except para que ningún entorno (AppTest,
    proxies, sandboxes) pueda tumbar el dashboard por culpa del efecto visual.
    """
    try:
        st.components.v1.html(_HTML, height=altura, scrolling=False)
    except Exception:
        pass

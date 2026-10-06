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
    padding:10px 18px;
    border-radius:14px;
    border:1px solid rgba(255,255,255,0.08);
    background:
        radial-gradient(120% 200% at 0% 50%, rgba(0,180,196,0.12) 0%, transparent 50%),
        linear-gradient(90deg, rgba(17,22,29,0.92), rgba(17,22,29,0.92));
    box-shadow: 0 6px 24px rgba(0,0,0,0.35);
    font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
    color:#e6edf3;
">
    <div style="display:flex; align-items:center; gap:10px;">
        <span style="font-size:18px;">📡</span>
        <span style="font-weight:700; letter-spacing:.4px; font-size:14px;">MWX.app</span>
    </div>

    <div style="display:flex; align-items:center; gap:8px; margin-left:auto;">
        <span style="
            display:inline-block; width:9px; height:9px; border-radius:50%;
            background:#2dd4a7; box-shadow:0 0 0 0 rgba(45,212,167,.7);
            animation:mwPulse 1.8s ease-out infinite;
        "></span>
        <span style="font-size:12px; letter-spacing:1px; text-transform:uppercase; color:rgba(230,237,243,.7);">
            En línea
        </span>
    </div>

    <div style="display:flex; flex-direction:column; align-items:flex-end; gap:1px;">
        <span id="mw-clock" style="
            font-size:20px; font-weight:800; letter-spacing:1.5px;
            font-variant-numeric:tabular-nums; color:#ffffff;
        ">--:--:--</span>
        <span id="mw-date" style="
            font-size:11px; letter-spacing:1px; text-transform:uppercase;
            color:rgba(0,180,196,.85);
        "></span>
    </div>
</div>

<style>
    @keyframes mwPulse {
        0%   { box-shadow: 0 0 0 0 rgba(45,212,167,.7); }
        70%  { box-shadow: 0 0 0 8px rgba(45,212,167,0); }
        100% { box-shadow: 0 0 0 0 rgba(45,212,167,0); }
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
        clock.style.transform = 'scale(1.06)';
        window.setTimeout(function () { clock.style.transform = 'scale(1)'; }, 120);
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

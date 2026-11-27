from pydantic_settings import BaseSettings
from pydantic import AliasChoices, Field
from typing import Optional
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Rutas del proyecto vs rutas de datos (MWX APP / PyInstaller)
# ---------------------------------------------------------------------------
# En desarrollo (sin congelar) se conserva EXACTAMENTE el comportamiento de
# siempre: la raiz del proyecto es la carpeta que contiene `core/` y todo
# (`.env`, SQLite, cookies, perfiles de Chrome, reportes) cuelga de ahi.
#
# En el ejecutable (PyInstaller: `sys.frozen`), el codigo y los recursos
# empaquetados son de SOLO LECTURA (`_MEIPASS` / carpeta del .exe), asi que los
# datos que se escriben (DB, cookies, perfiles, reportes, logs, .env del
# usuario) se guardan en `DATA_DIR`: `%LOCALAPPDATA%/MWX APP` en Windows,
# `~/.mwx_app` en el resto, o lo que diga la variable `MWX_DATA_DIR`.
APP_CONGELADA = bool(getattr(sys, "frozen", False))
# Alias publico (documentado) por si otro modulo necesita consultarlo.
CONGELADO = APP_CONGELADA


def _resolver_dir_datos() -> Path:
    """Carpeta de ESCRITURA del ejecutable (solo se usa si esta congelado)."""
    personalizado = (os.environ.get("MWX_DATA_DIR") or "").strip()
    if personalizado:
        return Path(personalizado).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "MWX APP"
        return Path.home() / "AppData" / "Local" / "MWX APP"
    return Path.home() / ".mwx_app"


if APP_CONGELADA:
    # Carpeta del ejecutable: recursos NO empaquetados (.env, assets propios).
    APP_DIR = Path(sys.executable).resolve().parent
    # Recursos empaquetados por PyInstaller (`--add-data`). En onefile es la
    # carpeta temporal `_MEIPASS`; en onedir puede no existir.
    _meipass = getattr(sys, "_MEIPASS", None)
    RESOURCE_DIR = Path(_meipass).resolve() if _meipass else APP_DIR
    DATA_DIR = _resolver_dir_datos().resolve()
    # Compatibilidad: los modulos que importan PROJECT_ROOT siguen recibiendo
    # una raiz valida (la del ejecutable, que es de solo lectura).
    PROJECT_ROOT = APP_DIR
    # Crea la raiz de escritura y las carpetas conocidas antes que cualquier
    # import de la app intente abrir la DB o escribir logs.
    for _carpeta in (DATA_DIR, DATA_DIR / "data", DATA_DIR / "config", DATA_DIR / "logs"):
        try:
            _carpeta.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Si una subcarpeta falla no se aborta el arranque: `settings`
            # volvera a intentar crearla y el error sera visible alli.
            pass
    # pydantic-settings lee los .env en orden; si un valor esta en ambos gana
    # el ULTIMO (el del ejecutable) para permitir overrides junto al .exe.
    _ENV_FILES = (DATA_DIR / ".env", APP_DIR / ".env")
else:
    PROJECT_ROOT = Path(__file__).parent.parent
    APP_DIR = PROJECT_ROOT
    RESOURCE_DIR = PROJECT_ROOT
    # Raiz de ESCRITURA en desarrollo = raiz del proyecto (misma semantica que
    # `PROJECT_ROOT/data` de siempre).
    DATA_DIR = PROJECT_ROOT
    _ENV_FILES = PROJECT_ROOT / ".env"


# Prefijos que en el ejecutable viven bajo DATA_DIR (escritura); el resto de
# rutas son codigo/recursos (solo lectura).
_PREFIJOS_DATOS = ("data", "config", "logs")


def _tiene_prefijo(texto: str, prefijo: str) -> bool:
    return texto == prefijo or texto.startswith(prefijo + "/")


def resolver_ruta(relativa: str) -> str:
    """Resuelve una ruta relativa a una ruta ABSOLUTA.

    Sin congelar: se resuelve contra la raiz del proyecto (comportamiento
    historico, identico byte a byte).

    Congelado (PyInstaller): `data/`, `config/` y `logs/` se resuelven contra
    ``DATA_DIR`` (escritura); el resto contra ``RESOURCE_DIR`` (recursos
    empaquetados) y, si no existe ahi, contra la carpeta del ejecutable.
    Las rutas absolutas se devuelven tal cual (resueltas).
    """
    if not APP_CONGELADA:
        return str((PROJECT_ROOT / relativa).resolve())

    ruta = Path(relativa)
    if ruta.is_absolute():
        return str(ruta.resolve())
    texto = ruta.as_posix()
    if any(_tiene_prefijo(texto, prefijo) for prefijo in _PREFIJOS_DATOS):
        return str((DATA_DIR / relativa).resolve())
    candidata = RESOURCE_DIR / relativa
    if not candidata.exists() and APP_DIR != RESOURCE_DIR:
        candidata_app = APP_DIR / relativa
        if candidata_app.exists():
            candidata = candidata_app
    return str(candidata.resolve())


def obtener_chromedriver(version_mayor: Optional[int] = None) -> str:
    """Localiza o descarga chromedriver.exe para la version de Chrome instalada
    y lo deja fijo en data/bin/chromedriver.exe. Devuelve la ruta absoluta.

    Usa webdriver-manager (Chrome for Testing para versiones >= 115).
    Si el binario ya existe en data/bin/ no vuelve a descargar nada."""
    import shutil
    nombre = "chromedriver.exe" if os.name == "nt" else "chromedriver"
    # Ruta absoluta: en el .exe `data/` vive bajo DATA_DIR (escritura).
    destino = Path(resolver_ruta(f"data/bin/{nombre}"))
    if destino.exists():
        return str(destino)

    try:
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        raise RuntimeError(
            "webdriver-manager no esta instalado. Ejecuta: pip install webdriver-manager"
        )

    if version_mayor is None:
        version_mayor = detectar_chrome_version()
    print(f"   [chromedriver] Buscando driver para Chrome {version_mayor}...")
    origen = ChromeDriverManager().install()
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origen, destino)
    print(f"   [chromedriver] Guardado en: {destino}")
    return str(destino)


def _headless_por_defecto() -> bool:
    """Decide el modo de Chrome por defecto segun el entorno.

    Regla (solo aplica si el usuario NO definio HEADLESS explicitamente; el
    campo Settings.headless con env="HEADLESS" siempre manda):

    1. Windows de escritorio => Chrome visible (False), como siempre.
    2. Dentro de un contenedor (Railway/Docker/Kubernetes: RAILWAY_ENVIRONMENT,
       /.dockerenv o KUBERNETES_SERVICE_HOST) => headless (True), AUNQUE exista
       DISPLAY: el Xvfb de supervisord no es necesario para las campanas y
       headless rinde mas y consume menos CPU/RAM.
    3. Linux/macOS sin DISPLAY (servidor sin pantalla) => headless (True).
    4. Cualquier otro caso (Linux de escritorio con DISPLAY) => visible (False).

    Para forzar Chrome visible bajo Xvfb define HEADLESS=false en el entorno.
    """
    if os.name == "nt":
        return False
    if (
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.path.exists("/.dockerenv")
        or os.environ.get("KUBERNETES_SERVICE_HOST")
    ):
        return True
    return not os.environ.get("DISPLAY")


def detectar_chrome_version() -> Optional[int]:
    """Devuelve la version mayor de Chrome instalada (ej. 151 -> 151).

    Windows: registro. macOS: primero los binarios dentro de las .app
    (`/Applications/Google Chrome.app/...` y Chromium) y despues los comandos
    del PATH. Linux: comandos del PATH. Nunca lanza: ante cualquier error
    devuelve None.
    """
    try:
        if os.name == "nt" and sys.platform != "darwin":
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon")
                val, _ = winreg.QueryValueEx(key, "version")
                winreg.CloseKey(key)
                return int(val.split(".")[0])
            except Exception:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon")
                val, _ = winreg.QueryValueEx(key, "version")
                winreg.CloseKey(key)
                return int(val.split(".")[0])
        import subprocess
        comandos = []
        if sys.platform == "darwin":
            # En macOS Chrome no esta en el PATH: vive dentro de la .app.
            comandos.extend([
                ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
                ["/Applications/Chromium.app/Contents/MacOS/Chromium", "--version"],
            ])
        # Linux (y respaldo en macOS): binarios del PATH.
        comandos.extend([
            ["google-chrome", "--version"],
            ["chromium", "--version"],
            ["chromium-browser", "--version"],
        ])
        for cmd in comandos:
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                salida = (out.stdout or out.stderr or "").strip()
                # Salidas reales: "Google Chrome 153.0.0.0" (3 tokens) o
                # "Chromium 140.0.6742.0" (2 tokens); se toma el primer token
                # numerico con puntos en ambos casos.
                for parte in salida.split():
                    if "." in parte and parte.split(".")[0].isdigit():
                        return int(parte.split(".")[0])
            except Exception:
                continue
    except Exception:
        pass
    return None


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str = Field(default="", env="TELEGRAM_BOT_TOKEN")
    
    # OpenAI
    openai_api_key: str = Field(..., env="OPENAI_API_KEY")
    # Endpoint alternativo (proxy/gateway compatible con OpenAI). pydantic-
    # settings v2 ignora `env=` en Field: los nombres efectivos son los
    # `validation_alias` (se aceptan `OPENAI_API_BASE` y `OPENAI_BASE_URL`).
    openai_api_base: str = Field(
        default="",
        env="OPENAI_API_BASE",
        validation_alias=AliasChoices(
            "OPENAI_API_BASE", "OPENAI_BASE_URL", "openai_api_base"
        ),
    )
    # URL del API local/remoto del MWX APP (el .exe la recibe por entorno).
    mwx_api_url: str = Field(
        default="",
        env="MWX_API_URL",
        validation_alias=AliasChoices("MWX_API_URL", "mwx_api_url"),
    )
    
    # Just Another Panel (JAP) API v2: panel de servicios (likes, RT, views,
    # comentarios, seguidores) para X/Twitter. La key tambien puede vivir en
    # data/jap_config.json (la escribe integraciones.jap.guardar_api_key).
    jap_api_key: str = Field(
        default="",
        env="JAP_API_KEY",
        validation_alias=AliasChoices("JAP_API_KEY", "jap_api_key"),
    )

    # Google Sheets / Drive: reporte con link para el cliente usando una
    # cuenta de servicio. La credencial se toma en este orden:
    #   GOOGLE_SERVICE_ACCOUNT_B64  -> JSON en base64 (recomendado Railway)
    #   GOOGLE_SERVICE_ACCOUNT_JSON -> JSON crudo (una linea)
    #   GOOGLE_SERVICE_ACCOUNT_FILE -> ruta al JSON
    #   data/google_service_account.json -> archivo local (lo guarda el panel)
    #   GOOGLE_APPLICATION_CREDENTIALS   -> ruta estandar (ultimo fallback)
    # pydantic-settings v2 ignora el kwarg `env=` de Field: el alias efectivo
    # es `validation_alias` (se aceptan MAYUSCULAS y el nombre del campo).
    google_service_account_b64: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GOOGLE_SERVICE_ACCOUNT_B64", "google_service_account_b64"
        ),
    )
    google_service_account_json: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GOOGLE_SERVICE_ACCOUNT_JSON", "google_service_account_json"
        ),
    )
    google_service_account_file: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GOOGLE_SERVICE_ACCOUNT_FILE", "google_service_account_file"
        ),
    )
    google_sheets_share_email: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GOOGLE_SHEETS_SHARE_EMAIL", "google_sheets_share_email"
        ),
    )
    google_sheets_publico: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "GOOGLE_SHEETS_PUBLICO", "google_sheets_publico"
        ),
    )

    # Database
    database_url: str = Field(default="sqlite:///data/gestor_redes.db", env="DATABASE_URL")
    
    # Security
    secret_key: str = Field(default="change-me-in-production", env="SECRET_KEY")
    
    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    log_file: str = Field(default="data/logs/gestor_redes.log", env="LOG_FILE")
    
    # Paths. `base_dir` es la raiz de ESCRITURA: raiz del proyecto en
    # desarrollo y DATA_DIR en el ejecutable congelado. `data_dir`/`app_dir`
    # se exponen como str para otros modulos (API / MWX APP).
    base_dir: Path = DATA_DIR
    data_dir: str = str(DATA_DIR)
    app_dir: str = str(APP_DIR)
    cookies_dir: Path = base_dir / "data" / "cookies"
    profiles_dir: Path = base_dir / "data" / "perfiles_chrome"
    reports_dir: Path = base_dir / "data" / "reportes"
    temp_dir: Path = base_dir / "data" / "temp"
    
    # Selenium
    chrome_driver_path: Optional[str] = None
    headless: bool = Field(default_factory=_headless_por_defecto, env="HEADLESS")
    # 3 navegadores permiten ~200+ publicaciones/hora (cada navegador ejecuta
    # una cuenta a la vez). Bajalo a 1 si el proxy va justo de GB/RAM; subelo
    # solo con margen. Tambien se ajusta desde el campo "Navegadores
    # simultaneos" de la operacion Activacion Masiva.
    max_browsers: int = Field(default=3, env="MAX_BROWSERS")
    
    # Rate Limiting
    twitter_delay_min: float = 2.5
    twitter_delay_max: float = 6.0

    # Cuotas inteligentes por hora (cuentas no premium): maximo de acciones
    # EXITOSAS por cuenta en la ventana de minutos configurada, por rol de
    # activacion. 0 = sin limite para ese rol. Se ajustan por .env.
    limite_posts_hora: int = Field(default=5, env="LIMITE_POSTS_HORA")
    limite_citas_hora: int = Field(default=5, env="LIMITE_CITAS_HORA")
    limite_rts_hora: int = Field(default=7, env="LIMITE_RTS_HORA")
    limite_comentarios_hora: int = Field(default=3, env="LIMITE_COMENTARIOS_HORA")
    limite_ventana_min: int = Field(default=60, env="LIMITE_VENTANA_MIN")
    # pydantic-settings v2 ignora el kwarg `env=` de Field: el nombre efectivo
    # de la variable de entorno es `validation_alias` (por eso aqui va explicito).
    limite_cuotas_activo: bool = Field(
        default=True,
        env="CUOTAS_HORARIAS_ACTIVO",
        validation_alias="CUOTAS_HORARIAS_ACTIVO",
    )

    # Tope DIARIO total por cuenta (anti-banderas de X): maximo de acciones
    # OPERATIVAS EXITOSAS por cuenta (posts, citas, RTs y comentarios, sin
    # importar el rol) dentro de la ventana diaria configurada. 0 = ilimitado.
    # `LIMITE_DIARIO_POR_CUENTA` es el nombre PRINCIPAL (pedido del dueño);
    # `LIMITE_ACCIONES_DIA` sigue aceptandose como alias legado (y el nombre
    # del campo en minusculas). pydantic-settings v2 ignora `env=` en Field:
    # el alias efectivo es `validation_alias`.
    limite_acciones_dia: int = Field(
        default=12,
        env="LIMITE_ACCIONES_DIA",
        validation_alias=AliasChoices(
            "LIMITE_DIARIO_POR_CUENTA",
            "LIMITE_ACCIONES_DIA",
            "limite_acciones_dia",
        ),
    )
    limite_dia_ventana_min: int = Field(default=1440, env="LIMITE_DIA_VENTANA_MIN")
    # Mismo patron que limite_cuotas_activo: `validation_alias` es el nombre
    # efectivo de la variable de entorno en pydantic-settings v2.
    limite_diario_activo: bool = Field(
        default=True,
        env="CUOTAS_DIARIAS_ACTIVO",
        validation_alias="CUOTAS_DIARIAS_ACTIVO",
    )

    # Campana LOCAL (Chrome visible en la computadora del dueño, sin proxies,
    # cuentas Tier 1 con sesion): reparte `local_acciones_cuenta` acciones por
    # cuenta (10 = 5 posts + 5 citas, el limite por hora; se puede subir hasta
    # 49 = tope diario de X) dentro de una ventana de `local_duracion_min`
    # minutos, con un tope diario de X propio
    # (`limite_local_dia`) para no mezclarlo con el tope general de arriba.
    # 0 = ilimitado. pydantic-settings v2 ignora `env=` en Field: el alias
    # efectivo es `validation_alias` (se aceptan MAYUSCULAS y el nombre del
    # campo en minusculas).
    limite_local_dia: int = Field(
        default=49,
        env="LIMITE_LOCAL_DIA",
        validation_alias=AliasChoices("LIMITE_LOCAL_DIA", "limite_local_dia"),
    )
    # 0 = sin límite (solo cuentas Tier 1 con sesión)
    local_max_cuentas: int = Field(
        default=0,
        env="LOCAL_MAX_CUENTAS",
        validation_alias=AliasChoices("LOCAL_MAX_CUENTAS", "local_max_cuentas"),
    )
    local_acciones_cuenta: int = Field(
        default=10,
        env="LOCAL_ACCIONES_CUENTA",
        validation_alias=AliasChoices(
            "LOCAL_ACCIONES_CUENTA", "local_acciones_cuenta"
        ),
    )
    local_duracion_min: int = Field(
        default=60,
        env="LOCAL_DURACION_MIN",
        validation_alias=AliasChoices("LOCAL_DURACION_MIN", "local_duracion_min"),
    )

    # Alertas
    alertas_max_results: int = 50
    alertas_timeout: int = 120
    alertas_dedup_days: int = 7

    # Alertas 24/7 (job del scheduler standalone; ver scheduler/manager.py y
    # scheduler/standalone.py). El job SOLO se registra con con_alertas=True,
    # que pasa UNICAMENTE el proceso standalone: el dashboard instancia
    # SchedulerManager() en varias paginas y registraria el job en cada una
    # (envios duplicados a Telegram). Los int se leen como 1/0 (tolerante a
    # "abc"/None: el manager los normaliza a su default).
    # pydantic-settings v2 ignora el kwarg `env=` de Field: el alias efectivo
    # es `validation_alias`; se aceptan MAYUSCULAS (Railway) y el nombre del
    # campo en minusculas.
    alertas_activo: int = Field(
        default=1,
        env="ALERTAS_ACTIVO",
        validation_alias=AliasChoices("ALERTAS_ACTIVO", "alertas_activo"),
    )
    alertas_intervalo_min: int = Field(
        default=60,
        env="ALERTAS_INTERVALO_MIN",
        validation_alias=AliasChoices(
            "ALERTAS_INTERVALO_MIN", "alertas_intervalo_min"
        ),
    )
    alertas_ventana_horas: int = Field(
        default=6,
        env="ALERTAS_VENTANA_HORAS",
        validation_alias=AliasChoices("ALERTAS_VENTANA_HORAS", "alertas_ventana_horas"),
    )
    alertas_resumen_diario: int = Field(
        default=1,
        env="ALERTAS_RESUMEN_DIARIO",
        validation_alias=AliasChoices(
            "ALERTAS_RESUMEN_DIARIO", "alertas_resumen_diario"
        ),
    )
    alertas_resumen_diario_hora: int = Field(
        default=22,
        env="ALERTAS_RESUMEN_DIARIO_HORA",
        validation_alias=AliasChoices(
            "ALERTAS_RESUMEN_DIARIO_HORA", "alertas_resumen_diario_hora"
        ),
    )
    # Pausa de envio a Telegram: 0 (default) = solo detectar y registrar en el
    # dashboard (MencionDia + historial de dedup); 1 = enviar tambien a los
    # grupos. Lo consulta `alertas/notificador.py`.
    alertas_enviar_telegram: int = Field(
        default=0,
        env="ALERTAS_ENVIAR_TELEGRAM",
        validation_alias=AliasChoices(
            "ALERTAS_ENVIAR_TELEGRAM", "alertas_enviar_telegram"
        ),
    )

    # Smartproxy residencial (México, sticky 15 min)
    smartproxy_host: str = Field(default="proxy.smartproxy.net", env="SMARTPROXY_HOST")
    smartproxy_port: int = Field(default=3120, env="SMARTPROXY_PORT")
    smartproxy_user: str = Field(default="smart-za4h7grdmqnw", env="SMARTPROXY_USER")
    smartproxy_password: str = Field(default="iZtVkRKjeNGqkgIh", env="SMARTPROXY_PASSWORD")
    smartproxy_country: str = Field(default="MX", env="SMARTPROXY_COUNTRY")
    smartproxy_life: int = Field(default=15, env="SMARTPROXY_LIFE")
    smartproxy_session_length: int = Field(default=8, env="SMARTPROXY_SESSION_LENGTH")

    class Config:
        # Puede ser un Path o una tupla de Paths (pydantic-settings v2 los
        # lee en orden; si una clave esta en varios gana el ultimo archivo).
        env_file = _ENV_FILES
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    def model_post_init(self, __context) -> None:
        # Create directories if they don't exist (siempre bajo DATA_DIR)
        self.cookies_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "data" / "logs").mkdir(parents=True, exist_ok=True)
        if APP_CONGELADA:
            # Carpetas de escritura conocidas del ejecutable.
            (self.base_dir / "config").mkdir(parents=True, exist_ok=True)
            (self.base_dir / "logs").mkdir(parents=True, exist_ok=True)
            try:
                Path(resolver_ruta(self.log_file)).parent.mkdir(
                    parents=True, exist_ok=True
                )
            except OSError:
                pass

    def proxy_sticky_mx(self, session_id: Optional[str] = None) -> str:
        import string, random
        if not session_id:
            session_id = "".join(random.choices(string.ascii_letters + string.digits, k=self.smartproxy_session_length))
        user = f"{self.smartproxy_user}_area-{self.smartproxy_country}_life-{self.smartproxy_life}_session-{session_id}"
        return f"http://{user}:{self.smartproxy_password}@{self.smartproxy_host}:{self.smartproxy_port}"


settings = Settings()

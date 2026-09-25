from pydantic_settings import BaseSettings
from pydantic import AliasChoices, Field
from typing import Optional
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def resolver_ruta(relativa: str) -> str:
    """Resuelve una ruta relativa al directorio raiz del proyecto,
    funcione cual sea el directorio de trabajo actual."""
    return str((PROJECT_ROOT / relativa).resolve())


def obtener_chromedriver(version_mayor: Optional[int] = None) -> str:
    """Localiza o descarga chromedriver.exe para la version de Chrome instalada
    y lo deja fijo en data/bin/chromedriver.exe. Devuelve la ruta absoluta.

    Usa webdriver-manager (Chrome for Testing para versiones >= 115).
    Si el binario ya existe en data/bin/ no vuelve a descargar nada."""
    import shutil
    nombre = "chromedriver.exe" if os.name == "nt" else "chromedriver"
    destino = PROJECT_ROOT / "data" / "bin" / nombre
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
    Usa el registro de Windows en Windows, y el binario en Linux/macOS."""
    try:
        if os.name == "nt":
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
        else:
            import subprocess
            for cmd in (["google-chrome", "--version"], ["chromium", "--version"], ["chromium-browser", "--version"]):
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    version = (out.stdout or out.stderr).strip()
                    return int(version.split()[2].split(".")[0])
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
    
    # Database
    database_url: str = Field(default="sqlite:///data/gestor_redes.db", env="DATABASE_URL")
    
    # Security
    secret_key: str = Field(default="change-me-in-production", env="SECRET_KEY")
    
    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    log_file: str = Field(default="data/logs/gestor_redes.log", env="LOG_FILE")
    
    # Paths
    base_dir: Path = Path(__file__).parent.parent
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
        env_file = PROJECT_ROOT / ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    def model_post_init(self, __context) -> None:
        # Create directories if they don't exist
        self.cookies_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "data" / "logs").mkdir(parents=True, exist_ok=True)

    def proxy_sticky_mx(self, session_id: Optional[str] = None) -> str:
        import string, random
        if not session_id:
            session_id = "".join(random.choices(string.ascii_letters + string.digits, k=self.smartproxy_session_length))
        user = f"{self.smartproxy_user}_area-{self.smartproxy_country}_life-{self.smartproxy_life}_session-{session_id}"
        return f"http://{user}:{self.smartproxy_password}@{self.smartproxy_host}:{self.smartproxy_port}"


settings = Settings()

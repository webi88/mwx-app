from pydantic_settings import BaseSettings
from pydantic import Field
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
    headless: bool = Field(default=False, env="HEADLESS")
    max_browsers: int = Field(default=3, env="MAX_BROWSERS")
    
    # Rate Limiting
    twitter_delay_min: float = 2.5
    twitter_delay_max: float = 6.0
    
    # Alertas
    alertas_max_results: int = 50
    alertas_timeout: int = 120
    alertas_dedup_days: int = 7

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

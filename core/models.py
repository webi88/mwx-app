from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    JSON,
    LargeBinary,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime, date

from core.database import Base


class Celula(Base):
    __tablename__ = "celulas"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(100), unique=True, nullable=False)
    narrativa = Column(Text, nullable=False)
    activa = Column(Boolean, default=True)
    
    clientes = relationship("Cliente", back_populates="celula", cascade="all, delete-orphan")
    cuentas = relationship("Cuenta", back_populates="celula")


class Cliente(Base):
    __tablename__ = "clientes"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(100), nullable=False)
    celula_id = Column(Integer, ForeignKey("celulas.id"), nullable=False)
    entrenamiento = Column(Text, default="")
    keywords = Column(Text, default="[]")
    localidad = Column(String(100), default="")
    telegram_chat_ids = Column(String(500), default="")
    exclude_terms = Column(Text, default="[]")
    num_principales = Column(Integer, default=5)
    activo = Column(Boolean, default=True)
    
    celula = relationship("Celula", back_populates="clientes")
    cuentas = relationship("Cuenta", back_populates="cliente")
    alertas_historial = relationship("AlertaHistorial", back_populates="cliente")
    menciones = relationship("MencionDia", back_populates="cliente")
    reportes = relationship("ReporteDiario", back_populates="cliente")


class Cuenta(Base):
    __tablename__ = "cuentas"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario = Column(String(100), unique=True, nullable=False)
    password = Column(String(200), default="")
    plataforma = Column(String(20), nullable=False)
    tags = Column(String(500), default="")
    # Grupo operativo libre (sin valor por defecto): ""/NULL = sin grupo.
    # Ya NO se usa "A" como default; la migracion en core/database.py
    # convierte los grupo="A" historicos a "" sin tocar otros grupos reales.
    grupo = Column(String(10), default="")
    activa = Column(Boolean, default=True)
    celery_id = Column(Integer, ForeignKey("celulas.id"), nullable=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True)
    cookies_path = Column(String(200), default="")
    email = Column(String(200), default="")
    phone = Column(String(50), default="")
    proxy = Column(String(300), default="")
    pais = Column(String(50), default="")
    sector = Column(String(30), default="")
    avatar_path = Column(String(200), default="")
    # Ruta local de la foto de portada (banner) de la cuenta.
    banner_path = Column(String(200), default="")
    # Clasificacion operativa: CI=Ciudadanía, IP=Institución Privada,
    # LIB=Libertad, JUS=Justicia (ver core/secciones.py). 'nombre_mostrado' y
    # 'handle_actual' guardan como se llama la cuenta REALMENTE en X, porque
    # otros clientes pueden renombrarla. 'usuario' sigue siendo la clave
    # interna de login y no se modifica.
    seccion = Column(String(20), default="")
    nombre_mostrado = Column(String(120), default="")
    handle_actual = Column(String(100), default="")
    # Tipo de voz de la cuenta: "politica" (lenguaje cuidado/institucional),
    # "activista" (tecnico-coloquial, ciudadania politica) o "ciudadana"
    # (lenguaje coloquial, persona real); "" = sin definir.
    # 'nombre_propuesto'/'handle_propuesto' guardan la propuesta generada con
    # IA ANTES de aplicarla en X (ver core/registros.py).
    tipo_cuenta = Column(String(20), default="")
    nombre_propuesto = Column(String(120), default="")
    handle_propuesto = Column(String(100), default="")
    # Personalidad: descripcion breve de tono/intereses/gustos de la cuenta,
    # usada por el mantenimiento programado para generar tuits acordes.
    personalidad = Column(Text, default="")
    # Rol de activacion en campana: "cita" (retweet con cita), "hashtags"
    # (publicar hashtags y menciones), "rt" (retweet simple) o "" (sin rol).
    # Ver core/roles.py para normalizacion y etiquetas.
    rol_activacion = Column(String(20), default="")
    # Perfil de redaccion para el contenido: "formal" (estructurado en 3
    # partes), "ciudadano" (par de renglones, analisis intermedio) o "popular"
    # (casual de un renglon con faltas intencionales). Se reparte equitativo
    # entre las cuentas; ver core/perfiles.py.
    perfil_personalidad = Column(String(20), default="")
    # Tier de calidad de la cuenta: "tier1" (Líder/Boosted; puede publicar
    # posts originales con hashtags), "tier2" (Volumen/Aged; PROHIBIDO el rol
    # "hashtags", solo RT/Cita/Comentario) o "tier3" (Métricas/Soporte; SOLO
    # rt/likes, el resto de roles prohibido). "" = sin clasificar (sin
    # restriccion). Ver core/tiers.py.
    tier_calidad = Column(String(20), default="")
    # Cuentas de CLIENTE: True = pausada para activación masiva (campañas,
    # RT/likes, reparto por hora y tareas de activación del scheduler) pero
    # PERMITIDA en mantenimiento (mantenimiento programado, calentamiento
    # continuo, publicaciones manuales y el bot de clientes). Ver core/pausas.py.
    pausada_activacion = Column(Boolean, default=False)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    totp_secret = Column(String(100), default="")
    email_password = Column(String(200), default="")
    auth_token = Column(String(200), default="")
    cookies_json = Column(JSON, nullable=True)
    # User-Agent original de la cuenta (viene en el lote) para que Chrome use
    # el mismo agente que cuando se creo.
    user_agent = Column(String(300), default="")
    status = Column(String(20), default="imported")
    last_checked = Column(DateTime, nullable=True)
    # "Cajon" de verificacion humana: True = la cuenta necesita que una persona
    # la revise (login, captcha, verificacion de identidad, etc.). Se excluye de
    # las campanas de activacion hasta que se marque como verificada.
    # 'verificacion_motivo' explica por que se mando al cajon,
    # 'verificacion_origen' de donde vino la deteccion (p. ej. "campana",
    # "scheduler", "manual") y 'verificacion_fecha' cuando se marco.
    # Ver core/verificacion.py.
    verificacion_pendiente = Column(Boolean, default=False)
    verificacion_motivo = Column(String(300), default="")
    verificacion_origen = Column(String(30), default="")
    verificacion_fecha = Column(DateTime, nullable=True)
    
    celula = relationship("Celula", back_populates="cuentas")
    cliente = relationship("Cliente", back_populates="cuentas")


class Tarea(Base):
    __tablename__ = "tareas"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    tipo = Column(String(50), nullable=False)
    plataforma = Column(String(20), nullable=False)
    contenido = Column(Text, default="")
    imagen_path = Column(String(200), default="")
    video_path = Column(String(200), default="")
    cuentas_ids = Column(Text, default="[]")
    fecha_hora = Column(DateTime, nullable=False)
    opciones = Column(JSON, default=dict)
    estado = Column(String(20), default="pendiente")
    resultado = Column(Text, default="")
    creada_por = Column(Integer, nullable=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)


class AlertaHistorial(Base):
    __tablename__ = "alertas_historial"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False)
    url = Column(Text, nullable=False)
    fuente = Column(String(50), default="")
    fecha_envio = Column(DateTime, default=datetime.utcnow)
    
    cliente = relationship("Cliente", back_populates="alertas_historial")


class MencionDia(Base):
    __tablename__ = "menciones_dia"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False)
    titulo = Column(Text, default="")
    resumen = Column(Text, default="")
    enlace = Column(Text, default="")
    fuente = Column(String(50), default="")
    es_principal = Column(Boolean, default=False)
    kw_principal = Column(String(100), default="")
    fecha = Column(Date, default=date.today)
    
    cliente = relationship("Cliente", back_populates="menciones")


class ReporteDiario(Base):
    __tablename__ = "reportes_diarios"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    fecha = Column(Date, nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False)
    total_alertas = Column(Integer, default=0)
    por_fuente = Column(JSON, default=dict)
    por_temas = Column(JSON, default=dict)
    enviado = Column(Boolean, default=False)
    
    cliente = relationship("Cliente", back_populates="reportes")


class RegistroAccion(Base):
    __tablename__ = "registro_acciones"

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario = Column(String(100), default="")
    tipo = Column(String(30), default="")      # mantenimiento / activacion / post / rt / hilo / like / calentamiento
    estado = Column(String(20), default="")     # exito / fallido
    url_publicacion = Column(Text, default="")
    detalle = Column(Text, default="")
    fecha = Column(DateTime, default=datetime.utcnow)


class CuentaChange(Base):
    """Granja de identidades de Change.org (Nombre / Apellido / Correo).

    Cada fila es una identidad lista para usarse en reportes de violacion de
    politicas (y, mas adelante, en firmas masivas) de Change.org. 'contexto' es
    el motivo general de la queja que captura el operador y 'queja' es el texto
    final que la IA genero y se envio en el reporte. 'usada_firma' es el flag
    pensado para el futuro modulo de firmas masivas (True = la identidad ya se
    uso para firmar). 'origen' registra de donde salio la identidad: "reporte"
    ahora; mas adelante "firma"/"manual".

    El email es UNIQUE a proposito: la granja no debe duplicar identidades.

    Nota: la tabla se crea sola con `Base.metadata.create_all` (via `init_db`),
    por lo que no requiere migracion ALTER en core/database.py."""

    __tablename__ = "cuenta_change"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(120), default="")
    apellido = Column(String(120), default="")
    email = Column(String(200), default="", unique=True)  # unico: la granja no debe duplicar identidades
    codigo_postal = Column(String(20), default="")
    url_peticion = Column(Text, default="")
    contexto = Column(Text, default="")   # motivo general de la queja (input del operador)
    queja = Column(Text, default="")      # texto que la IA genero y se envio en el reporte
    origen = Column(String(30), default="reporte")  # "reporte" ahora; futuro "firma"/"manual"
    usada_firma = Column(Boolean, default=False)    # flag para el futuro modulo de firmas masivas
    fecha_creacion = Column(DateTime, default=datetime.utcnow)


class SesionChange(Base):
    """Sesion persistida (cookies) de una cuenta de Change.org en la BD.

    Una fila por cuenta (`usuario` UNIQUE): `cookies_json` guarda el MISMO dict
    que el archivo `data/cookies/change/{usuario}.json`
    (``{"usuario","email","guardada","proxy","cookies":[...]}``). Sirve para que
    la sesion sobreviva a los deploys cuando el volumen no es persistente
    (Railway/Supabase): si el archivo no existe o esta corrupto,
    `cargar_sesion_change` la recupera de aqui y la vuelve a cachear en disco.
    `actualizada` se refresca en cada UPSERT.

    Nota: la tabla se crea sola con `Base.metadata.create_all` (via `init_db`),
    por lo que no requiere migracion ALTER en core/database.py."""

    __tablename__ = "sesion_change"

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario = Column(String(100), unique=True, index=True, nullable=False)
    cookies_json = Column(JSON, nullable=True)
    actualizada = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# Tablas del ejecutable MWX APP (login multi-usuario + configuracion local)
# ---------------------------------------------------------------------------
# Son tablas NUEVAS: `Cuenta` no se toca. `Base.metadata.create_all(bind=engine)`
# dentro de `init_db` (core/database.py, que ya importa `core.models`) las crea
# solas en SQLite/PostgreSQL, sin migraciones ALTER.


class AppUsuario(Base):
    """Usuario local del MWX APP (login del ejecutable, no Telegram)."""

    __tablename__ = "app_usuario"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Se guarda en minusculas desde `core.app_auth` (case-insensitive).
    username = Column(String(100), unique=True, index=True, nullable=False)
    # Formato PBKDF2: pbkdf2_sha256$iteraciones$salt_hex$hash_hex
    password_hash = Column(String(256), default="", nullable=False)
    nombre = Column(String(150), default="")
    activo = Column(Boolean, default=True, nullable=False)
    # Se incrementa al revocar sesiones; el token firmado lleva `tv` y la API
    # lo compara contra este valor para invalidar tokens anteriores.
    token_version = Column(Integer, default=0, nullable=False)
    creado = Column(DateTime, default=datetime.utcnow)
    ultimo_acceso = Column(DateTime, nullable=True)


class AppAsignacion(Base):
    """Cuentas de la flota asignadas a un usuario del MWX APP."""

    __tablename__ = "app_asignacion"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id", "cuenta_id", name="uq_app_asignacion_usuario_cuenta"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(
        Integer, ForeignKey("app_usuario.id"), nullable=False, index=True
    )
    cuenta_id = Column(Integer, ForeignKey("cuentas.id"), nullable=False, index=True)


class AppConfig(Base):
    """Configuracion de campaña LOCAL guardada por usuario (una fila c/u).

    Las listas (`hashtags`, `urls`, `imagenes`) se guardan como Text con JSON
    serializado; `imagenes` son nombres de archivo (no rutas absolutas)."""

    __tablename__ = "app_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(
        Integer,
        ForeignKey("app_usuario.id"),
        unique=True,
        index=True,
        nullable=False,
    )
    hashtags = Column(Text, default="[]")
    contexto = Column(Text, default="")
    narrativa = Column(Text, default="")
    urls = Column(Text, default="[]")
    texto_base = Column(Text, default="")
    acciones_por_cuenta = Column(Integer, default=10)
    pausa_seg = Column(Integer, default=2)
    duracion_min = Column(Integer, default=60)
    limite_dia = Column(Integer, default=49)
    # Navegadores Chrome simultaneos de la campana LOCAL (1-3).
    navegadores = Column(Integer, default=1)
    # No cargar imagenes de la pagina en el navegador (ahorra datos).
    sin_imagenes = Column(Boolean, default=True)
    imagenes = Column(Text, default="[]")
    permitir_ajustes = Column(Boolean, default=False)
    actualizado = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppImagen(Base):
    """Imagen de campaña del MWX APP guardada EN LA BASE DE DATOS.

    La API del ejecutable corre en otro contenedor y NO comparte el disco del
    dashboard, por eso el binario viaja con la fila (`contenido`) en vez de
    depender de una ruta local. `nombre` es el nombre de archivo saneado (ver
    `core/app_imagenes.py`); es UNICO por usuario, de modo que volver a subir
    la misma imagen REEMPLAZA la anterior (UPSERT) sin duplicarla.

    `AppConfig.imagenes` sigue guardando la lista de nombres; esta tabla es la
    que resuelve el contenido cuando el cliente pide `/v1/imagenes/{nombre}`.

    Nota: la tabla se crea sola con `Base.metadata.create_all` (via `init_db`),
    por lo que no requiere migracion ALTER en core/database.py."""

    __tablename__ = "app_imagen"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id", "nombre", name="uq_app_imagen_usuario_nombre"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(
        Integer, ForeignKey("app_usuario.id"), nullable=False, index=True
    )
    # Nombre de archivo saneado (solo [A-Za-z0-9._-], sin rutas).
    nombre = Column(String(160), nullable=False)
    # Bytes de la imagen: LargeBinary -> BLOB en SQLite, BYTEA en PostgreSQL.
    contenido = Column(LargeBinary, nullable=False)
    content_type = Column(String(80), default="application/octet-stream")
    tamano = Column(Integer, default=0)
    creado = Column(DateTime, default=datetime.utcnow)

    # Relacion informativa (sin cascade destructivo): borrar el usuario NO
    # borra imagenes automaticamente; la FK lo impide o quedan huerfanas segun
    # el motor, igual que el resto de tablas App*.
    usuario = relationship("AppUsuario")


class AppEvento(Base):
    """Auditoria opcional de eventos del MWX APP (login, campaña, errores)."""

    __tablename__ = "app_evento"

    id = Column(Integer, primary_key=True, autoincrement=True)
    usuario_id = Column(
        Integer, ForeignKey("app_usuario.id"), nullable=True, index=True
    )
    tipo = Column(String(50), default="")
    detalle = Column(Text, default="")
    fecha = Column(DateTime, default=datetime.utcnow, index=True)


class AppRelease(Base):
    """Paquete ZIP publicado del ejecutable MWX APP (descarga + actualización).

    `contenido` guarda el ZIP completo (LargeBinary -> BLOB en SQLite, BYTEA en
    PostgreSQL) para que el ejecutable y el dashboard puedan descargarlo aunque
    no compartan disco. `version` es UNIQUE: publicar la misma versión otra vez
    la REEMPLAZA (UPSERT) en vez de duplicarla. Las notas se recortan a 4000
    caracteres al publicar (ver core/app_releases.py).

    Nota: la tabla se crea sola con `Base.metadata.create_all` (via `init_db`),
    por lo que no requiere migracion ALTER en core/database.py."""

    __tablename__ = "app_releases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(50), unique=True, nullable=False, index=True)
    obligatorio = Column(Boolean, default=True)
    notas = Column(Text, default="")
    filename = Column(String(200), default="")
    sha256 = Column(String(64), default="")
    tamano = Column(Integer, default=0)
    contenido = Column(LargeBinary, nullable=True)
    fecha = Column(DateTime, default=datetime.utcnow)

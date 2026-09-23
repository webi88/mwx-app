from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Date, ForeignKey, JSON
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

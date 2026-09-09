from core.database import get_db_session
from core.models import Celula, Cliente
from loguru import logger


class CelulasManager:
    def crear_celula(self, nombre: str, narrativa: str) -> bool:
        try:
            with get_db_session() as db:
                existing = db.query(Celula).filter(Celula.nombre == nombre).first()
                if existing:
                    logger.warning(f"Celula '{nombre}' ya existe")
                    return False
                
                celula = Celula(nombre=nombre, narrativa=narrativa)
                db.add(celula)
                db.commit()
                
                logger.info(f"Celula '{nombre}' creada")
                return True
        except Exception as e:
            logger.error(f"Error creando celula: {e}")
            return False
    
    def eliminar_celula(self, celula_id: int) -> bool:
        try:
            with get_db_session() as db:
                celula = db.query(Celula).filter(Celula.id == celula_id).first()
                if not celula:
                    return False
                
                db.delete(celula)
                db.commit()
                
                logger.info(f"Celula '{celula.nombre}' eliminada")
                return True
        except Exception as e:
            logger.error(f"Error eliminando celula: {e}")
            return False
    
    def crear_cliente(self, nombre: str, celula_id: int) -> bool:
        try:
            with get_db_session() as db:
                celula = db.query(Celula).filter(Celula.id == celula_id).first()
                if not celula:
                    logger.warning(f"Celula {celula_id} no encontrada")
                    return False
                
                cliente = Cliente(nombre=nombre, celula_id=celula_id)
                db.add(cliente)
                db.commit()
                
                logger.info(f"Cliente '{nombre}' creado en celula '{celula.nombre}'")
                return True
        except Exception as e:
            logger.error(f"Error creando cliente: {e}")
            return False
    
    def entrenar_cliente(self, cliente_id: int, instrucciones: str) -> bool:
        try:
            with get_db_session() as db:
                cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
                if not cliente:
                    return False
                
                cliente.entrenamiento = instrucciones
                db.commit()
                
                logger.info(f"Cliente '{cliente.nombre}' entrenado")
                return True
        except Exception as e:
            logger.error(f"Error entrenando cliente: {e}")
            return False
    
    def obtener_celulas(self) -> list[Celula]:
        with get_db_session() as db:
            return db.query(Celula).filter(Celula.activa == True).all()
    
    def obtener_clientes(self, celula_id: int = None) -> list[Cliente]:
        with get_db_session() as db:
            query = db.query(Cliente).filter(Cliente.activo == True)
            if celula_id:
                query = query.filter(Cliente.celula_id == celula_id)
            return query.all()
    
    def obtener_cliente(self, cliente_id: int) -> Cliente:
        with get_db_session() as db:
            return db.query(Cliente).filter(Cliente.id == cliente_id).first()

import hashlib
import json
import os
from pathlib import Path

WEB_USERS_FILE = Path(__file__).parent.parent / "data" / "web_users.json"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _cargar_usuarios() -> dict:
    if os.path.exists(WEB_USERS_FILE):
        try:
            with open(WEB_USERS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _guardar_usuarios(usuarios: dict):
    WEB_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEB_USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(usuarios, f, indent=4, ensure_ascii=False)


def crear_usuario_web(username: str, password: str, rol: str = "operador", nombre: str = ""):
    usuarios = _cargar_usuarios()
    if username in usuarios:
        return False
    usuarios[username] = {
        "password_hash": _hash_password(password),
        "role": rol,
        "nombre": nombre or username,
        "activo": True,
        "creado": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    _guardar_usuarios(usuarios)
    return True


def verificar_login_web(username: str, password: str):
    usuarios = _cargar_usuarios()
    usuario = usuarios.get(username)
    if not usuario:
        return None
    if not usuario.get("activo", True):
        return None
    if usuario["password_hash"] != _hash_password(password):
        return None
    return {
        "username": username,
        "nombre": usuario.get("nombre", username),
        "rol": usuario.get("role", "operador")
    }


def listar_usuarios_web() -> list[dict]:
    usuarios = _cargar_usuarios()
    return [
        {"username": u, **datos}
        for u, datos in usuarios.items()
    ]


def actualizar_usuario_web(username: str, **campos):
    usuarios = _cargar_usuarios()
    if username not in usuarios:
        return False
    if "password" in campos:
        usuarios[username]["password_hash"] = _hash_password(campos.pop("password"))
    if "rol" in campos:
        usuarios[username]["role"] = campos.pop("rol")
    if "nombre" in campos:
        usuarios[username]["nombre"] = campos.pop("nombre")
    if "activo" in campos:
        usuarios[username]["activo"] = campos.pop("activo")
    for k, v in campos.items():
        usuarios[username][k] = v
    _guardar_usuarios(usuarios)
    return True


def eliminar_usuario_web(username: str) -> bool:
    usuarios = _cargar_usuarios()
    if username not in usuarios:
        return False
    del usuarios[username]
    _guardar_usuarios(usuarios)
    return True


def asegurar_admin_por_defecto():
    usuarios = _cargar_usuarios()
    if not usuarios:
        crear_usuario_web("admin", "admin", rol="admin", nombre="Administrador")


def es_admin_web(rol: str) -> bool:
    return rol == "admin"
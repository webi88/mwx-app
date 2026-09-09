import json
import hashlib
import os
import random
import re
import string
from typing import Optional
from loguru import logger
from core.config import resolver_ruta
from utils.forward_proxy import LocalForwardProxy


# Mapeo de codigo ISO de 2 letras (IPRoyal usa _country-XX_) a los paises que
# manejamos en Grizzly. Permite asignar un proxy del mismo pais que el numero
# de telefono para no disparar el anti-fraude de X.
PAIS_ISO = {
    "CA": "canada",
    "GB": "uk",
    "MX": "mexico",
    "BR": "brasil",
    "GH": "ghana",
    "RO": "rumania",
    "PH": "filipinas",
    "KE": "kenia",
    "MA": "marruecos",
    "PY": "paraguay",
    "PL": "polonia",
    "TH": "tailandia",
    "NG": "nigeria",
    "IE": "irlanda",
    "BO": "bolivia",
}

# Duracion (minutos) de la sesion sticky. En Smartproxy va como _life-XX en el
# USUARIO; en IPRoyal Mobile va como _lifetime-XXm en el PASSWORD. El plan es de
# 15 min; con esto la IP se mantiene fija durante todo el registro (2-5 min) sin
# rotar a mitad del flujo.
LIFETIME_MINUTOS = 15

PAIS_ALIAS = {
    "canada": ["canada", "canadá", "ca"],
    "uk": ["uk", "gb", "reino unido", "united kingdom", "inglaterra"],
    "mexico": ["mexico", "mx", "méxico"],
    "brasil": ["brasil", "br", "brazil"],
    "ghana": ["ghana", "gh"],
    "rumania": ["rumania", "romania", "ro"],
    "filipinas": ["filipinas", "philippines", "ph"],
    "kenia": ["kenia", "kenya", "ke"],
    "marruecos": ["marruecos", "morocco", "ma"],
    "paraguay": ["paraguay", "py"],
    "polonia": ["polonia", "poland", "pl"],
    "tailandia": ["tailandia", "thailand", "th"],
    "nigeria": ["nigeria", "ng"],
    "irlanda": ["irlanda", "ireland", "ie"],
    "bolivia": ["bolivia", "bo"],
}


def pais_normalizado(pais: str) -> Optional[str]:
    """Devuelve la clave canonica del pais (ej. 'mexico') a partir de alias."""
    if not pais:
        return None
    p = pais.strip().lower()
    for canon, aliases in PAIS_ALIAS.items():
        if p in aliases:
            return canon
    return p


def codigo_a_pais(codigo: str) -> Optional[str]:
    """Convierte un codigo ISO de 2 letras (ej. 'DE', 'FR') a la clave canonica
    del pais ('alemania', 'francia'). Devuelve None si no hay codigo."""
    if not codigo:
        return None
    c = codigo.strip().upper()
    return PAIS_ISO.get(c, c.lower())


def _sesion_aleatoria(longitud: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=longitud))


class ProxyManager:
    """Gestiona proxies residenciales/moviles (Smartproxy e IPRoyal).

    Lee un archivo .txt con un proxy por linea y permite asignar
    uno distinto a cada cuenta. Soporta los formatos:
      - http://user:pass@host:port
      - user:pass@host:port
      - host:port
      - host:port:user:pass
      - socks5://user:pass@host:port

    Ademas detecta el pais del proxy (Smartproxy codifica `_area-XX_` en el
    USUARIO; IPRoyal codifica `_country-XX_` en el PASSWORD) para poder
    filtrar proxies por pais.
    """

    FORMATOS = ("http://", "https://", "socks4://", "socks5://")
    RE_COUNTRY = re.compile(r"_(?:area|country)-([A-Za-z]{2})(?:_|:|$)", re.IGNORECASE)
    # Bright Data (brd.superproxy.io) usa GUION para el pais: -country-XX.
    RE_COUNTRY_DASH = re.compile(r"-country-([A-Za-z]{2})(?:-|:|$)", re.IGNORECASE)

    # Numero de sesiones (_session-XXX) quemadas de UN mismo gateway (misma
    # combinacion host:port:usuario_base) a partir del cual se considera que el
    # gateway completo esta flaggeado por X y se excluyen TODAS sus sesiones.
    UMBRAL_GATEWAY_QUEMADO = 5

    def __init__(self, archivo: str = None):
        self.archivo = archivo or resolver_ruta("data/proxies.txt")
        self.dir_paises = resolver_ruta("data/proxies")
        self.quemados_path = resolver_ruta("data/proxies/quemados.txt")
        self._fwd_proxy = None
        # Gateways ya reportados por logger.error para no repetir el aviso.
        self._gateways_logueados = set()

    def _archivos_por_pais(self) -> dict[str, list[str]]:
        """Devuelve un dict {pais_canonico: [rutas de archivo]} escaneando la
        carpeta data/proxies/ (un archivo .txt por pais)."""
        resultado = {}
        if not os.path.isdir(self.dir_paises):
            return resultado
        for nombre in os.listdir(self.dir_paises):
            if not nombre.lower().endswith(".txt"):
                continue
            if nombre.lower() == "quemados.txt":
                continue
            canon = pais_normalizado(os.path.splitext(nombre)[0])
            if canon:
                resultado.setdefault(canon, []).append(os.path.join(self.dir_paises, nombre))
        return resultado

    def _leer_archivo(self, ruta: str) -> list[str]:
        proxies = []
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                for linea in f:
                    linea = linea.strip()
                    if not linea or linea.startswith("#"):
                        continue
                    normalizado = self.normalizar(linea)
                    if normalizado:
                        proxies.append(normalizado)
        except Exception as e:
            logger.warning(f"Error leyendo {ruta}: {e}")
        return proxies

    def detectar_pais(self, proxy: str) -> Optional[str]:
        """Detecta el pais del proxy leyendo `_area-XX` (Smartproxy, en el
        usuario), `_country-XX` (IPRoyal, en el password) o `-country-XX`
        (Bright Data, guion, en el usuario). Devuelve la clave canonica
        (ej. 'uk') o None si no lo sabe."""
        if not proxy:
            return None
        m = self.RE_COUNTRY.search(proxy) or self.RE_COUNTRY_DASH.search(proxy)
        if not m:
            return None
        codigo = m.group(1).upper()
        return PAIS_ISO.get(codigo, codigo.lower())

    def cargar_quemados(self) -> set:
        """Devuelve el conjunto de proxies marcados como quemados (bloqueados)."""
        quemados = set()
        if not os.path.exists(self.quemados_path):
            return quemados
        try:
            with open(self.quemados_path, "r", encoding="utf-8") as f:
                for linea in f:
                    linea = linea.strip()
                    if not linea or linea.startswith("#"):
                        continue
                    normalizado = self.normalizar(linea)
                    if normalizado:
                        quemados.add(normalizado)
        except Exception as e:
            logger.warning(f"Error leyendo quemados: {e}")
        return quemados

    def _clave(self, proxy: str) -> str:
        """Clave canonica de un proxy para comparar (host:port:user:session).

        Incluye la sesion sticky (_session-XXX): en Smartproxy va en el USUARIO
        (junto con _area-XX y _life-XX) y en IPRoyal va en el PASSWORD (junto
        con _country-XX y _lifetime-XXm). Sin incluir la sesion, todos los
        proxies de un pais compartirian la misma clave y quemar uno quemaria
        todos.
        """
        info = self.analizar(proxy)
        if not info:
            return proxy or ""
        base = f"{info['host']}:{info['port']}:{info.get('user') or ''}"
        m = re.search(r"[-_]session-([A-Za-z0-9]+)", proxy)
        if m:
            base += f":{m.group(1)}"
        return base

    def _clave_gateway(self, proxy: str) -> str:
        """Clave del GATEWAY al que pertenece el proxy, ignorando la sesion y
        el pais.

        Todos los proxies de un mismo proveedor comparten un unico gateway
        (misma combinacion host:port:usuario_base) y solo se diferencian por
        modificadores (`_area-XX`, `_life-XX`, `_session-XXX` en Smartproxy;
        `_country-XX`, `_lifetime-XXm`, `_session-XXX` en IPRoyal). Esta clave
        agrupa todas esas variantes para detectar cuando X ha quemado el
        gateway COMPLETO (no solo una sesion). NO sustituye a _clave(), que
        sigue usandose para quemar sesiones individuales.
        """
        info = self.analizar(proxy)
        if not info:
            return proxy or ""
        user = info.get("user") or ""
        # Quitar todos los modificadores del usuario (Smartproxy los lleva en
        # el usuario con guion bajo; Bright Data con guion; IPRoyal ya va limpio).
        user = re.sub(r"_(?:area|country)-[A-Za-z]{2}(?=_|:|$)", "", user)
        user = re.sub(r"-country-[A-Za-z]{2}(?=-|:|$)", "", user)
        user = re.sub(r"_life-[A-Za-z0-9]+(?=_|:|$)", "", user)
        user = re.sub(r"_lifetime-[A-Za-z0-9]+(?=_|:|$)", "", user)
        user = re.sub(r"[-_]session-[A-Za-z0-9]+(?=[-_]|:|$)", "", user)
        return f"{info['host']}:{info['port']}:{user}"

    def _reportar_gateway_quemado(self, clave: str, n: int) -> None:
        """Registra (una sola vez) el aviso de gateway completo quemado."""
        if clave in self._gateways_logueados:
            return
        self._gateways_logueados.add(clave)
        logger.error(
            f"GATEWAY DE PROXY QUEMADO: {clave} acumula {n} sesiones quemadas "
            f"(umbral {self.UMBRAL_GATEWAY_QUEMADO}). X ha bloqueado el gateway "
            f"completo: CAMBIA DE PROVEEDOR de proxies y NO gastes mas numeros "
            f"Grizzly en un pool muerto (quemar sesiones una a una es inutil)."
        )

    def _gateways_quemados(self, quemados: set = None) -> set:
        """Devuelve el conjunto de gateways con >= UMBRAL sesiones quemadas.

        Un gateway quemado se detecta contando cuantas sesiones distintas de la
        MISMA clave de gateway (host:port:usuario_base, sin _session-XXX ni
        _area-XX) estan en quemados.txt. Al superar el umbral, se reporta el
        ERROR y su clave queda para que cargar_proxies() excluya TODAS sus
        sesiones restantes.
        """
        if quemados is None:
            quemados = self.cargar_quemados()
        contador = {}
        for q in quemados:
            clave = self._clave_gateway(q)
            contador[clave] = contador.get(clave, 0) + 1
        gateways = set()
        for clave, n in contador.items():
            if n >= self.UMBRAL_GATEWAY_QUEMADO:
                gateways.add(clave)
                self._reportar_gateway_quemado(clave, n)
        return gateways

    def gateway_quemado(self, proxy: str) -> bool:
        """True si el GATEWAY al que pertenece 'proxy' ya se considera quemado
        (>= UMBRAL_GATEWAY_QUEMADO sesiones bloqueadas del mismo gateway).

        Lo usa el creador para abortar la tanda en cuanto X quema el gateway
        completo, en vez de seguir quemando sesiones una a una y gastando
        numeros Grizzly en un pool de IP muerto.
        """
        if not proxy:
            return False
        return self._clave_gateway(proxy) in self._gateways_quemados()

    def marcar_quemado(self, proxy: str) -> bool:
        """Marca un proxy como quemado: lo anade a data/proxies/quemados.txt y
        lo elimina de los archivos de pais. Devuelve True si se guardo."""
        if not proxy:
            return False
        try:
            os.makedirs(os.path.dirname(self.quemados_path), exist_ok=True)
            clave = self._clave(proxy)
            if not os.path.exists(self.quemados_path):
                with open(self.quemados_path, "w", encoding="utf-8") as f:
                    f.write("# Proxies quemados (bloqueados por X)\n")

            # No duplicar
            if any(self._clave(q) == clave for q in self.cargar_quemados()):
                logger.info(f"Proxy ya estaba marcado como quemado: {clave}")
            else:
                with open(self.quemados_path, "a", encoding="utf-8") as f:
                    f.write(proxy + "\n")

            # Eliminarlo de los archivos de pais para que no se recargue
            for rutas in self._archivos_por_pais().values():
                for ruta in rutas:
                    self._eliminar_de_archivo(ruta, clave)

            logger.info(f"Proxy marcado como quemado y removido: {clave}")

            # Deteccion de gateway quemado: si con esta sesion el gateway ya
            # supera el umbral, se reporta (y cargar_proxies() lo excluira
            # entero). Se recarga el archivo para incluir la sesion recien
            # anadida.
            self._gateways_quemados()
            return True
        except Exception as e:
            logger.error(f"Error marcando proxy quemado: {e}")
            return False

    def _eliminar_de_archivo(self, ruta: str, clave: str) -> None:
        """Elimina las lineas cuyo proxy coincide con 'clave' del archivo."""
        try:
            if not os.path.exists(ruta):
                return
            with open(ruta, "r", encoding="utf-8") as f:
                lineas = f.readlines()
            nuevas = []
            removidas = 0
            for linea in lineas:
                s = linea.strip()
                if not s or s.startswith("#"):
                    nuevas.append(linea)
                    continue
                norm = self.normalizar(s)
                if norm and self._clave(norm) == clave:
                    removidas += 1
                    continue
                nuevas.append(linea)
            if removidas:
                with open(ruta, "w", encoding="utf-8") as f:
                    f.writelines(nuevas)
                logger.info(f"Removidas {removidas} linea(s) de {os.path.basename(ruta)}")
        except Exception as e:
            logger.warning(f"Error eliminando proxy de {ruta}: {e}")

    def cargar_proxies(self) -> list[str]:
        # 1) Si existe la carpeta data/proxies/ con archivos por pais, usar esos.
        por_pais = self._archivos_por_pais()
        if por_pais:
            proxies = []
            for rutas in por_pais.values():
                for ruta in rutas:
                    proxies.extend(self._leer_archivo(ruta))
            if proxies:
                quemados = self.cargar_quemados()
                claves_quemadas = {self._clave(q) for q in quemados}
                gateways_quemados = self._gateways_quemados(quemados)
                proxies = [
                    p for p in proxies
                    if self._clave(p) not in claves_quemadas
                    and self._clave_gateway(p) not in gateways_quemados
                ]
                logger.info(f"Proxies cargados: {len(proxies)} desde data/proxies/ (excluyendo quemados)")
                return proxies

        # 2) Fallback: archivo unico data/proxies.txt
        if not os.path.exists(self.archivo):
            logger.warning(f"No existe el archivo de proxies: {self.archivo}")
            return []

        proxies = self._leer_archivo(self.archivo)
        quemados = self.cargar_quemados()
        claves_quemadas = {self._clave(q) for q in quemados}
        gateways_quemados = self._gateways_quemados(quemados)
        proxies = [
            p for p in proxies
            if self._clave(p) not in claves_quemadas
            and self._clave_gateway(p) not in gateways_quemados
        ]
        logger.info(f"Proxies cargados: {len(proxies)} desde {self.archivo}")
        return proxies

    def cargar_por_pais(self, pais: str) -> list[str]:
        """Carga solo los proxies de un pais (ej. 'mexico', 'usa', 'uk').

        Busca primero un archivo data/proxies/{pais}.txt; si no existe, filtra
        por deteccion de codigo _country-XX_ en el proxy. Si no hay del pais,
        devuelve la lista completa como fallback. Excluye proxies quemados."""
        canon = pais_normalizado(pais)
        if canon:
            por_pais = self._archivos_por_pais()
            rutas = por_pais.get(canon)
            if rutas:
                proxies = []
                for ruta in rutas:
                    proxies.extend(self._leer_archivo(ruta))
                quemados = self.cargar_quemados()
                claves_quemadas = {self._clave(q) for q in quemados}
                gateways_quemados = self._gateways_quemados(quemados)
                proxies = [
                    p for p in proxies
                    if self._clave(p) not in claves_quemadas
                    and self._clave_gateway(p) not in gateways_quemados
                ]
                logger.info(f"Proxies del pais '{canon}': {len(proxies)} (archivo dedicado)")
                return proxies

        todos = self.cargar_proxies()
        if not canon:
            return todos

        filtrados = [p for p in todos if self.detectar_pais(p) == canon]

        if not filtrados:
            logger.warning(
                f"No hay proxies del pais '{canon}'. Se usaran los {len(todos)} "
                f"proxies disponibles (o se creara sin proxy si no hay)."
            )
            return todos

        logger.info(f"Proxies del pais '{canon}': {len(filtrados)}/{len(todos)}")
        return filtrados

    def regenerar_desde_base(self, proxy_base: str, sesiones_por_pais: int = 50, paises: list = None) -> dict:
        """Regenera data/proxies/{pais}.txt desde UN solo proxy base.

        Soporta dos proveedores, detectados por el host:

        * Smartproxy (proxy.smartproxy.net): los modificadores van en el
          USUARIO (`_area-XX`, `_life-XX`, `_session-XXX`) y el password queda
          intacto. `_life` NO lleva 'm' al final (`_life-15`).
        * IPRoyal (geo.iproyal.com): los modificadores van en el PASSWORD
          (`_country-XX`, `_session-XXX`, `_lifetime-XXm`).
        * Bright Data (brd.superproxy.io): los modificadores van en el USUARIO
          con GUION (`-country-XX`, `-session-XXX`); el password queda intacto.

        Desde un proxy base se generan los paises indicados (por defecto todos)
        con 'sesiones_por_pais' sesiones sticky distintas. Devuelve {pais: n}.
        """
        proxy_base = self.normalizar(proxy_base) or proxy_base
        info = self.analizar(proxy_base)
        if not info:
            logger.error(f"Proxy base invalido: {proxy_base}")
            return {}
        os.makedirs(self.dir_paises, exist_ok=True)
        resultado = {}

        host = (info.get("host") or "").lower()
        es_iproyal = "iproyal" in host
        es_brightdata = "superproxy.io" in host or "brightdata" in host

        # Base limpia: usuario/password sin modificadores previos.
        user_base = info.get("user") or ""
        pass_base = info.get("password") or ""
        if es_iproyal:
            # IPRoyal Mobile: los 3 parametros van en el PASSWORD (poner
            # _country-XX en el usuario devuelve 407 Proxy Auth Required).
            user_base = re.sub(r"_country-[A-Za-z]{2}(?=_|:|$)", "", user_base)
            pass_base = re.sub(r"_country-[A-Za-z]{2}(?=_|:|$)", "", pass_base)
            pass_base = re.sub(r"_session-[A-Za-z0-9]+(?=_|:|$)", "", pass_base)
            pass_base = re.sub(r"_lifetime-[A-Za-z0-9]+(?=_|:|$)", "", pass_base)
        elif es_brightdata:
            # Bright Data: country y sesion van en el USUARIO con GUION
            # (-country-XX, -session-XXX); el password queda intacto.
            user_base = re.sub(r"-country-[A-Za-z]{2}(?=-|:|$)", "", user_base)
            user_base = re.sub(r"[-_]session-[A-Za-z0-9]+(?=[-_]|:|$)", "", user_base)
        else:
            # Smartproxy: los modificadores van en el USUARIO; el password queda
            # intacto (sin modificadores).
            user_base = re.sub(r"_area-[A-Za-z]{2}(?=_|:|$)", "", user_base)
            user_base = re.sub(r"_life-[A-Za-z0-9]+(?=_|:|$)", "", user_base)
            user_base = re.sub(r"_session-[A-Za-z0-9]+(?=_|:|$)", "", user_base)

        codigos = (
            list(PAIS_ISO.keys())
            if paises is None
            else [c.upper() for c in paises if c.upper() in PAIS_ISO]
        )
        for codigo in codigos:
            pais = PAIS_ISO[codigo]
            proxies_pais = []
            for _ in range(max(1, sesiones_por_pais)):
                if es_iproyal:
                    user = user_base
                    pwd = f"{pass_base}_country-{codigo}_session-{_sesion_aleatoria()}_lifetime-{LIFETIME_MINUTOS}m"
                elif es_brightdata:
                    user = f"{user_base}-country-{codigo}-session-{_sesion_aleatoria()}"
                    pwd = pass_base
                else:
                    user = f"{user_base}_area-{codigo}_life-{LIFETIME_MINUTOS}_session-{_sesion_aleatoria()}"
                    pwd = pass_base
                p = f"{info['scheme']}://{user}:{pwd}@{info['host']}:{info['port']}"
                proxies_pais.append(p)
            proxies_pais = list(dict.fromkeys(proxies_pais))
            ruta = os.path.join(self.dir_paises, f"{pais}.txt")
            proveedor = "BrightData" if es_brightdata else ("IPRoyal" if es_iproyal else "Smartproxy")
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(f"# Proxies {pais} (generados desde proxy base {proveedor})\n")
                for p in proxies_pais:
                    f.write(p + "\n")
            resultado[pais] = len(proxies_pais)
            logger.info(f"Proxies {pais}: {len(proxies_pais)} -> {ruta}")
        return resultado

    def regenerar_si_base(self) -> bool:
        """Si existe data/proxy_base.txt, regenera los proxies por pais desde
        la primera linea valida. Devuelve True si regenero.

        SOLO regenera en el PRIMER arranque (cuando los archivos por pais no
        existen o estan vacios). Regenerar en cada arranque crearia sesiones
        _session-XXX nuevas sobre el MISMO gateway flaggeado, dejaria huerfanas
        las sesiones quemadas de quemados.txt y anularia marcar_quemado() entre
        ejecuciones (whack-a-mole). Por eso, si ya hay proxies por pais, se
        conservan tal cual (con sus quemados acumulados).
        """
        base_path = resolver_ruta("data/proxy_base.txt")
        if not os.path.exists(base_path):
            return False
        if self._hay_proxies_por_pais():
            logger.info(
                "Ya existen proxies por pais; se omite la regeneracion desde "
                "base para conservar las sesiones quemadas entre ejecuciones."
            )
            return False
        try:
            with open(base_path, "r", encoding="utf-8") as f:
                for linea in f:
                    linea = linea.strip()
                    if not linea or linea.startswith("#"):
                        continue
                    proxy = self.normalizar(linea)
                    if proxy:
                        self.regenerar_desde_base(proxy)
                        logger.info(f"Proxies por pais actualizados desde {base_path}")
                        return True
        except Exception as e:
            logger.warning(f"Error regenerando proxies desde base: {e}")
        return False

    def _hay_proxies_por_pais(self) -> bool:
        """True si existe al menos un proxy valido en data/proxies/{pais}.txt.

        Se usa para saber si los archivos por pais ya fueron generados (primer
        arranque) y evitar regenerarlos de nuevo sobre el mismo gateway.
        """
        for rutas in self._archivos_por_pais().values():
            for ruta in rutas:
                if self._leer_archivo(ruta):
                    return True
        return False

    def verificar_proxy(self, proxy: str, timeout: int = 25) -> dict:
        """Comprueba que el proxy responde y devuelve su IP de salida real.

        Sirve para (1) descartar proxies muertos/congelados antes de usarlos y
        (2) confirmar que el codigo _country-XX_ realmente sale por el pais
        esperado (si el plan de IPRoyal no soporta ese pais, aqui se detecta
        porque la IP no coincide con el codigo).

        Devuelve {'ok': bool, 'ip': str, 'pais': str, 'error': str}.
        """
        import requests

        resultado = {"ok": False, "ip": "", "pais": "", "error": "", "definitivo": False}
        info = self.analizar(proxy)
        if not info:
            resultado["error"] = "proxy invalido"
            return resultado

        user = info.get("user")
        pwd = info.get("password") or ""
        creds = f"{user}:{pwd}@" if user else ""
        url_proxy = f"{info['scheme']}://{creds}{info['host']}:{info['port']}"
        proxies = {"http": url_proxy, "https": url_proxy}

        # Orden de endpoints pensado para proxies residenciales (IPRoyal): los
        # servicios de geolocalizacion (ipwho.is/ipapi.co/ipinfo.io) a menudo
        # BLOQUEAN las IPs residenciales, por eso se anteponen los mas amigables
        # y se usa ipify como confirmacion minima de conexion (sin pais).
        # ipinfo.io es el mas restrictivo y se deja al final como ultimo recurso.
        # Cada tupla es (url, {clave_ip, claves_pais}).
        endpoints = (
            ("https://ipwho.is/", {"ip": "ip", "pais": ("country_code", "country")}),
            ("https://ipapi.co/json/", {"ip": "ip", "pais": ("country_code", "country")}),
            ("https://api.ipify.org?format=json", {"ip": "ip", "pais": ()}),
            ("https://ipinfo.io/json", {"ip": "ip", "pais": ("country", "country_code")}),
        )

        for endpoint, campos in endpoints:
            try:
                r = requests.get(endpoint, proxies=proxies, timeout=timeout)
                if r.status_code != 200:
                    resultado["error"] = f"{endpoint}: HTTP {r.status_code}"
                    continue
                data = r.json()
                ip = data.get(campos["ip"]) or ""
                pais = ""
                for k in campos["pais"]:
                    v = data.get(k)
                    if v:
                        pais = str(v).strip().upper()
                        break
                if ip:
                    # Un endpoint que responde 200 con IP pero sin pais (ej.
                    # ipify) basta para confirmar que el proxy CONECTA: se
                    # devuelve ok=True con pais="" para que el caller no
                    # penalice por no conocer el pais de salida.
                    resultado.update(ok=True, ip=ip, pais=pais)
                    return resultado
                resultado["error"] = f"{endpoint}: sin IP en respuesta"
            except Exception as e:
                error_completo = str(e)
                resultado["error"] = f"{endpoint}: {error_completo[:100]}"
                # Fallo DEFINITIVO de pago: IPRoyal sin saldo responde en el tunel
                # CONNECT con HTTP 402 Payment Required. El 402 aparece pasados
                # los 100 chars del truncado, por eso se mira el string completo.
                # El 407 (Proxy Authentication Required) NO es definitivo.
                if "Payment Required" in error_completo or " 402 " in error_completo:
                    resultado["definitivo"] = True

        return resultado

    def verificar_ip_x(self, proxy: str, timeout: int = 20) -> dict:
        """Comprueba si el proxy puede cargar X sin bloqueo HTTP evidente.

        Devuelve {'ok': bool, 'status': int|None, 'error': str}. Solo marca ok=False
        ante señales CLARAS de bloqueo (HTTP 403/429 o marcadores de bloqueo en el
        body). Los errores de red/timeout NO se marcan como bloqueo (de eso ya se
        encarga verificar_proxy); en ese caso se devuelve ok=True para no quemar
        proxies por falsos negativos.
        """
        import requests

        resultado = {"ok": True, "status": None, "error": ""}
        info = self.analizar(proxy)
        if not info:
            resultado["ok"] = False
            resultado["error"] = "proxy invalido"
            return resultado

        user = info.get("user")
        pwd = info.get("password") or ""
        creds = f"{user}:{pwd}@" if user else ""
        url_proxy = f"{info['scheme']}://{creds}{info['host']}:{info['port']}"
        proxies = {"http": url_proxy, "https": url_proxy}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

        try:
            r = requests.get(
                "https://x.com/",
                proxies=proxies,
                timeout=timeout,
                allow_redirects=True,
                headers=headers,
            )
            resultado["status"] = r.status_code

            if r.status_code in (403, 429):
                resultado["ok"] = False
                resultado["error"] = f"HTTP {r.status_code} (posible bloqueo de X)"
                return resultado

            texto = (r.text or "")[:3000].lower()
            marcadores = (
                "something went wrong",
                "algo salio mal",
                "access denied",
                "privacy related extensions",
                "enable javascript",
            )
            for m in marcadores:
                if m in texto:
                    resultado["ok"] = False
                    resultado["error"] = f"X devolvió bloqueo ({m})"
                    return resultado

            return resultado
        except Exception as e:
            msg = str(e)
            if "Tunnel connection failed" in msg:
                # El proveedor rechazó el túnel CONNECT hacia x.com (bloqueo real).
                resultado["ok"] = False
                resultado["error"] = f"proveedor bloquea x.com: {msg[:120]}"
                return resultado
            # Errores de red/timeout NO queman el proxy: devolver ok=True.
            resultado["ok"] = True
            resultado["error"] = msg[:120]
            return resultado

    def verificar_acceso_x(self, proxy: str, timeout: int = 20, reintentos: int = 1) -> dict:
        """Verificación PREVIA de que el proxy puede acceder a x.com.

        Debe llamarse ANTES de solicitar un número Grizzly: si el proxy no llega
        a x.com, no tiene sentido gastar un SMS. Devuelve:
          {'ok': bool, 'status': int|None, 'error': str, 'definitivo': bool}
        'definitivo' distingue un bloqueo real (proveedor o X) de un fallo
        transitorio de red (que no debe quemar el proxy, pero tampoco debe
        gastar un número). Se reintenta 'reintentos' veces ante fallo transitorio.
        """
        import time as _time

        import requests

        resultado = {"ok": False, "status": None, "error": "", "definitivo": False}
        info = self.analizar(proxy)
        if not info:
            resultado["error"] = "proxy invalido"
            resultado["definitivo"] = True
            return resultado

        user = info.get("user")
        pwd = info.get("password") or ""
        creds = f"{user}:{pwd}@" if user else ""
        url_proxy = f"{info['scheme']}://{creds}{info['host']}:{info['port']}"
        proxies = {"http": url_proxy, "https": url_proxy}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }

        ultimo_error = ""
        for _ in range(reintentos + 1):
            try:
                r = requests.get(
                    "https://x.com/",
                    proxies=proxies,
                    timeout=timeout,
                    allow_redirects=True,
                    headers=headers,
                )
                resultado["status"] = r.status_code
                if r.status_code in (403, 429):
                    resultado["error"] = f"X devuelve HTTP {r.status_code} (IP bloqueada)"
                    resultado["definitivo"] = True
                    return resultado
                # 200 u otra respuesta no-bloqueante -> accesible.
                resultado["ok"] = True
                return resultado
            except Exception as e:
                msg = str(e)
                if "Tunnel connection failed" in msg:
                    # El proveedor rechazó el túnel CONNECT hacia x.com (bloqueo real).
                    resultado["error"] = f"proveedor bloquea x.com: {msg[:160]}"
                    resultado["definitivo"] = True
                    return resultado
                ultimo_error = msg
                _time.sleep(2)  # reintentar ante fallo transitorio

        resultado["error"] = f"sin acceso a x.com (timeout/red): {ultimo_error[:160]}"
        return resultado

    def refrescar_sesion(self, proxy: str) -> str:
        """Devuelve el MISMO proxy con una sesion _session-XXX nueva.

        Regenerar la sesion sticky fuerza una IP de salida distinta. La sesion
        puede ir en el usuario (Smartproxy) o en el password (IPRoyal): se
        reemplaza _session-XXX y se conserva el resto intacto (incluido
        _life-XX/_lifetime-XXm).
        """
        if not proxy or "_session-" not in proxy:
            return proxy
        return re.sub(r"_session-[A-Za-z0-9]+", f"_session-{_sesion_aleatoria()}", proxy)

    def normalizar(self, linea: str) -> Optional[str]:
        s = linea.strip()
        if not s:
            return None

        if not s.startswith(self.FORMATOS):
            if "@" in s:
                s = f"http://{s}"
            elif s.count(":") == 3:
                host, port, user, pwd = s.rsplit(":", 3)
                s = f"http://{user}:{pwd}@{host}:{port}"
            else:
                s = f"http://{s}"

        if not self.analizar(s):
            logger.warning(f"Proxy ignorado (formato invalido): {linea}")
            return None

        return s

    def analizar(self, proxy: str) -> Optional[dict]:
        """Devuelve {scheme, host, port, user, password} o None."""
        try:
            s = proxy.strip()
            scheme = "http"
            resto = s
            for prefijo in self.FORMATOS:
                if s.startswith(prefijo):
                    scheme = prefijo.replace("://", "")
                    resto = s[len(prefijo):]
                    break

            user = None
            password = None
            if "@" in resto:
                creds, host_port = resto.rsplit("@", 1)
                if ":" in creds:
                    user, password = creds.split(":", 1)
                else:
                    user = creds
            else:
                host_port = resto

            if ":" not in host_port:
                return None

            host, port = host_port.rsplit(":", 1)
            if not host or not port or not port.isdigit():
                return None

            return {
                "scheme": scheme,
                "host": host,
                "port": int(port),
                "user": user,
                "password": password,
            }
        except Exception:
            return None

    def host_port(self, proxy: str) -> Optional[str]:
        info = self.analizar(proxy)
        if not info:
            return None
        return f"{info['host']}:{info['port']}"

    def construir_extension_auth(self, proxy: str, base_dir: str = None) -> Optional[str]:
        """Crea una extension de Chrome que autentica el proxy (407) y la carga.

        Chrome no soporta credenciales dentro de --proxy-server de forma fiable,
        asi que usamos una extension con chrome.proxy + onAuthRequired.
        Devuelve la ruta de la extension o None si no requiere autenticacion.
        """
        info = self.analizar(proxy)
        if not info or not info.get("user"):
            return None

        base_dir = base_dir or resolver_ruta("data/temp/proxy_extensions")
        tag = hashlib.md5(proxy.encode()).hexdigest()[:12]
        ext_dir = os.path.join(base_dir, tag)
        os.makedirs(ext_dir, exist_ok=True)

        manifest_json = """{
            "version": "1.0.0",
            "manifest_version": 3,
            "name": "Chrome Proxy",
            "permissions": ["proxy", "webRequest", "webRequestBlocking", "webRequestAuthProvider"],
            "host_permissions": ["<all_urls>"],
            "background": {"service_worker": "background.js"}
        }
        """

        user_js = json.dumps(info["user"])
        pass_js = json.dumps(info.get("password", ""))
        scheme_js = json.dumps(info["scheme"])
        host_js = json.dumps(info["host"])
        port_js = json.dumps(str(info["port"]))

        background_js = (
            "var config = {\n"
            '    mode: "fixed_servers",\n'
            "    rules: {\n"
            "      singleProxy: {\n"
            f"        scheme: {scheme_js},\n"
            f"        host: {host_js},\n"
            f"        port: parseInt({port_js})\n"
            "      },\n"
            '      bypassList: ["localhost"]\n'
            "    }\n"
            "  };\n\n"
            'chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});\n\n'
            "function callbackFn(details) {\n"
            "    return {\n"
            "        authCredentials: {\n"
            f"            username: {user_js},\n"
            f"            password: {pass_js}\n"
            "        }\n"
            "    };\n"
            "}\n\n"
            "chrome.webRequest.onAuthRequired.addListener(\n"
            "    callbackFn,\n"
            '    {urls: ["<all_urls>"]},\n'
            '    ["blocking"]\n'
            ");\n"
        )

        with open(os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(manifest_json)
        with open(os.path.join(ext_dir, "background.js"), "w", encoding="utf-8") as f:
            f.write(background_js)

        return ext_dir

    def aplicar_a_options(self, options, proxy: str, tag: str = "perfil") -> None:
        """Aplica el proxy a un objeto ChromeOptions de undetected_chromedriver.

        Chrome 137+ ya no carga --load-extension ni acepta credenciales en
        --proxy-server, asi que se levanta un proxy local (127.0.0.1) que
        inyecta Proxy-Authorization y reenvia al proxy real.
        """
        if not proxy:
            return

        info = self.analizar(proxy)
        if not info:
            logger.warning(f"Proxy invalido, no se aplicara: {proxy}")
            return

        if info.get("user"):
            # Cerrar un proxy local previo antes de levantar uno nuevo.
            self.cerrar_fwd_proxy()
            fwd = LocalForwardProxy(
                info["host"], info["port"], info["user"], info.get("password", "")
            )
            puerto_local = fwd.start()
            self._fwd_proxy = fwd
            options.add_argument(f"--proxy-server=127.0.0.1:{puerto_local}")
        else:
            options.add_argument(f"--proxy-server={info['host']}:{info['port']}")

    def cerrar_fwd_proxy(self) -> None:
        """Cierra el proxy local de autenticacion activo (si hay)."""
        if getattr(self, "_fwd_proxy", None):
            try:
                self._fwd_proxy.close()
            except Exception:
                pass
            self._fwd_proxy = None
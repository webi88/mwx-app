import base64
import os
import select
import socket
import threading
import time
from loguru import logger


_DOMINIOS_BLOQUEADOS = (
    ".google.com",
    ".googleapis.com",
    ".gstatic.com",
    ".doubleclick.net",
    ".google-analytics.com",
    ".googletagmanager.com",
    ".googlesyndication.com",
    ".googleusercontent.com",
    ".googlevideo.com",
)


def _env_activo(nombre: str, por_defecto: bool) -> bool:
    """Lee un booleano de entorno: 0/false/no/off desactivan."""
    valor = os.environ.get(nombre)
    if valor is None or not valor.strip():
        return por_defecto
    return valor.strip().lower() not in ("0", "false", "no", "off")


def _bloqueo_google_activo() -> bool:
    """True si el bloqueo de dominios Google esta activo (default si)."""
    try:
        return _env_activo("CHROME_BLOQUEAR_GOOGLE", True)
    except Exception:
        return True


def _host_bloqueado(host) -> bool:
    """True si `host` (con o sin puerto) es de un dominio bloqueado.

    Normaliza a minusculas, quita el puerto final y compara por sufijo
    exacto: `notgoogle.com` NO se bloquea. Nunca lanza.
    """
    try:
        h = str(host or "").strip().lower()
        if h.startswith("["):
            corte = h.find("]")
            if corte != -1:
                h = h[1:corte]
        elif ":" in h:
            h = h.rsplit(":", 1)[0]
        h = h.rstrip(".")
        if not h:
            return False
        for dominio in _DOMINIOS_BLOQUEADOS:
            if h == dominio[1:] or h.endswith(dominio):
                return True
        return False
    except Exception:
        return False


class LocalForwardProxy:
    """Proxy local que inyecta `Proxy-Authorization` y reenvia al proxy real.

    Chrome 137+ ignora `--load-extension` y no acepta credenciales en
    `--proxy-server`, asi que la forma fiable de usar un proxy con
    usuario/contraseña es apuntar Chrome a un proxy local (127.0.0.1) que
    agregue el header de autenticacion y reenvie el trafico al proxy real.
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = int(port)
        self.auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._server = None
        self._conns = set()
        self._lock = threading.Lock()
        self._running = False
        self.port_local = 0

    def start(self) -> int:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self._running = True
        self.port_local = self._server.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        logger.info(
            f"Proxy local de auth en 127.0.0.1:{self.port_local} "
            f"-> {self.host}:{self.port}"
        )
        return self.port_local

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._conns.add(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _upstream(self):
        return socket.create_connection((self.host, self.port), timeout=25)

    def _handle(self, client):
        try:
            client.settimeout(30)
            data = client.recv(65536)
            if not data:
                return
            first_line = data.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
            if first_line.startswith("CONNECT"):
                self._tunnel(client, first_line, data)
            else:
                self._forward_plain(client, data)
        except Exception as e:
            logger.error(f"Forward proxy error: {type(e).__name__}: {e}")
        finally:
            self._discard(client)

    def _tunnel(self, client, first_line, data):
        trozos = first_line.split(" ")
        hostport = trozos[1] if len(trozos) > 1 else ""
        if _bloqueo_google_activo() and _host_bloqueado(hostport):
            logger.debug(
                f"Forward proxy: {hostport} bloqueado (dominio Google)"
            )
            try:
                client.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
                )
            except Exception:
                pass
            return

        upstream = None
        buf = b""
        status_line_bytes = b""
        for intento in (1, 2):
            try:
                upstream = self._upstream()
                req = (
                    f"CONNECT {hostport} HTTP/1.1\r\n"
                    f"Host: {hostport}\r\n"
                    f"Proxy-Authorization: Basic {self.auth}\r\n"
                    "Proxy-Connection: keep-alive\r\n\r\n"
                ).encode("latin-1")
                upstream.sendall(req)

                buf = b""
                upstream.settimeout(25)
                while b"\r\n\r\n" not in buf:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                status_line_bytes = buf.split(b"\r\n", 1)[0]
            except Exception as e:
                if upstream is not None:
                    try:
                        upstream.close()
                    except Exception:
                        pass
                    upstream = None
                if intento == 1:
                    logger.debug(
                        f"Forward proxy: fallo de upstream a {hostport} "
                        f"({type(e).__name__}: {e}); reintento en 0.3s"
                    )
                    time.sleep(0.3)
                    continue
                raise
            if b" 200" in status_line_bytes:
                break
            try:
                upstream.close()
            except Exception:
                pass
            upstream = None
            if intento == 1:
                logger.debug(
                    f"Forward proxy: respuesta sin 200 de {hostport} "
                    f"({status_line_bytes.decode('latin-1', 'ignore')!r}); "
                    "reintento en 0.3s"
                )
                time.sleep(0.3)
                continue
            client.sendall(buf)
            logger.error(
                f"El proxy rechazo el tunel a {hostport}: "
                f"{status_line_bytes.decode('latin-1', 'ignore')} | {buf[:500]!r}"
            )
            return

        client.sendall(buf)
        logger.debug(
            f"Tunel establecido -> {hostport} "
            f"({status_line_bytes.decode('latin-1', 'ignore')})"
        )

        # bytes extra que el cliente pudo enviar junto al CONNECT (TLS hello)
        partes = data.split(b"\r\n\r\n", 1)
        if len(partes) > 1 and partes[1]:
            upstream.sendall(partes[1])

        self._relay(client, upstream)

    def _forward_plain(self, client, data):
        upstream = self._upstream()
        # Insertar Proxy-Authorization tras la linea de request.
        header, sep, rest = data.partition(b"\r\n")
        if sep:
            data = header + b"\r\nProxy-Authorization: Basic " + self.auth.encode() + b"\r\n" + rest
        upstream.sendall(data)
        self._relay(client, upstream)

    def _relay(self, a, b):
        sockets = [a, b]
        try:
            while self._running:
                readable, _, _ = select.select(sockets, [], [], 30)
                if not readable:
                    break
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except Exception:
            pass
        finally:
            for s in sockets:
                try:
                    s.close()
                except Exception:
                    pass

    def _discard(self, conn):
        with self._lock:
            self._conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass

    def close(self):
        self._running = False
        with self._lock:
            for c in list(self._conns):
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
        try:
            self._server.close()
        except Exception:
            pass

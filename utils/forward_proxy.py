import base64
import select
import socket
import threading
from loguru import logger


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
            logger.debug(f"Forward proxy handle error: {e}")
        finally:
            self._discard(client)

    def _tunnel(self, client, first_line, data):
        hostport = first_line.split(" ")[1]
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
        client.sendall(buf)

        status_line = buf.split(b"\r\n", 1)[0]
        if b" 200" not in status_line:
            return

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

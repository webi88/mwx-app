import base64
import os
import queue
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


def _env_int(nombre: str, por_defecto: int, minimo: int = 1) -> int:
    """Lee un entero de entorno con valor minimo; nunca lanza."""
    try:
        valor = int(str(os.environ.get(nombre, "")).strip())
        return max(minimo, valor)
    except Exception:
        return por_defecto


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

    Usa un POOL ACOTADO de workers fijos (NO un hilo por conexion): Chrome
    mantiene keep-alive y con muchos navegadores concurrentes los hilos por
    conexion agotaban el contenedor (`RuntimeError: can't start new thread`).
    `_accept_loop` solo acepta y encola; los N workers (env
    `FORWARD_PROXY_WORKERS`, default 6) atienden la cola. Si la cola esta
    llena, la conexion se cierra sin crear nada.

    Cada navegador crea su PROPIO `LocalForwardProxy`: con default 16 y 7
    navegadores eran 112 hilos solo de proxys (parte del agotamiento de
    hilos/PIDs). Chrome usa ~6 conexiones por host a la vez, por lo que 6
    workers por navegador alcanzan.
    """

    # Tamano maximo de la cola de conexiones pendientes: si se llena, la
    # conexion se rechaza (evita acumular trabajo/memoria sin limite).
    COLA_MAX = 256

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = int(port)
        self.auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._server = None
        self._conns = set()
        self._lock = threading.Lock()
        self._running = False
        self.port_local = 0
        # Pool acotado de workers (tope real de hilos, independiente del
        # numero de conexiones de Chrome).
        self._num_workers = _env_int("FORWARD_PROXY_WORKERS", 6)
        self._queue = None
        self._workers = []
        # Modo defensivo si el SO no deja crear NI UN worker (contenedor
        # agotado): el aceptador atiende la conexion el mismo, sin encolar.
        self._inline = False

    def start(self) -> int:
        if self._running and self.port_local:
            return self.port_local
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(64)
        self._server.settimeout(1.0)
        self._running = True
        self.port_local = self._server.getsockname()[1]
        self._queue = queue.Queue(maxsize=self.COLA_MAX)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        self._workers = []
        for indice in range(self._num_workers):
            try:
                hilo = threading.Thread(
                    target=self._worker_loop, name=f"fwd-proxy-{indice}", daemon=True
                )
                hilo.start()
                self._workers.append(hilo)
            except Exception as e:
                logger.error(
                    f"Forward proxy: no se pudo crear el worker {indice + 1}/"
                    f"{self._num_workers} ({type(e).__name__}: {e})"
                )
                break
        if not self._workers:
            # Nunca lanzar: al menos se atiende en el hilo aceptador.
            self._inline = True
            logger.warning(
                "Forward proxy: sin workers disponibles; modo inline defensivo "
                "(se atiende de a una conexion)"
            )
        logger.info(
            f"Proxy local de auth en 127.0.0.1:{self.port_local} "
            f"-> {self.host}:{self.port} "
            f"({len(self._workers)} worker(s), cola {self.COLA_MAX})"
        )
        return self.port_local

    def _accept_loop(self):
        while self._running:
            servidor = self._server
            if servidor is None:
                break
            try:
                conn, _ = servidor.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                logger.error(f"Forward proxy accept error: {type(e).__name__}: {e}")
                break
            if not self._running:
                self._discard(conn)
                break
            with self._lock:
                self._conns.add(conn)
            if self._inline or not self._workers:
                # Defensivo: sin workers, atender en el aceptador.
                self._handle(conn)
                continue
            try:
                self._queue.put_nowait(conn)
            except queue.Full:
                logger.debug("Forward proxy: cola llena; se rechaza la conexion")
                self._discard(conn)

    def _worker_loop(self):
        while True:
            try:
                conn = self._queue.get(timeout=0.5)
            except queue.Empty:
                if not self._running:
                    return
                continue
            except Exception:
                return
            try:
                if not self._running:
                    self._discard(conn)
                    continue
                self._handle(conn)
            except Exception as e:
                logger.error(
                    f"Forward proxy worker error: {type(e).__name__}: {e}"
                )
                self._discard(conn)
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass

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
        """Detiene el proxy por completo: server, cola, conexiones y workers.

        Nunca lanza. Tras el close ya no se aceptan conexiones nuevas y los
        workers terminan en <=2s (join con timeout corto).
        """
        self._running = False
        self._inline = False
        servidor, self._server = self._server, None
        if servidor is not None:
            try:
                servidor.close()
            except Exception:
                pass
        cola, self._queue = self._queue, None
        if cola is not None:
            while True:
                try:
                    pendiente = cola.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
                self._discard(pendiente)
        with self._lock:
            activas = list(self._conns)
            self._conns.clear()
        for c in activas:
            try:
                c.close()
            except Exception:
                pass
        workers, self._workers = self._workers, []
        limite = time.time() + 2.0
        for worker in workers:
            restante = limite - time.time()
            if restante <= 0:
                break
            try:
                worker.join(timeout=min(0.5, restante))
            except Exception:
                pass
        # Los workers restantes son daemon y saldran solos al ver `_running`
        # en False y la cola vacia.

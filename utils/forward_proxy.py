import base64
import os
import queue
import select
import socket
import threading
import time
from urllib.parse import urlsplit

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


def _es_socket_cerrado(error) -> bool:
    """True si el error indica que el socket ya estaba cerrado LOCALMENTE.

    Ocurre cuando `cambiar_upstream()` (o `close()`) cierra una conexion
    mientras un worker recien la toma: en Windows da `WinError 10038` y en
    POSIX `EBADF` (9). No es un fallo del proxy: se registra a DEBUG.
    """
    try:
        if getattr(error, "winerror", None) == 10038:
            return True
        if getattr(error, "errno", None) == 9:  # EBADF
            return True
    except Exception:
        pass
    return False


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

    CASO DE USO "PESTAÑA PERSISTENTE": un SOLO Chrome apunta a
    `127.0.0.1:{port_local}` durante toda la campana y, al cambiar de cuenta
    (`TwitterBot.cambiar_cuenta`), se llama a `cambiar_upstream(proxy_nuevo)`
    para que la IP de salida real (el upstream sticky del pais de la cuenta
    nueva) cambie EN CALIENTE sin abrir un navegador nuevo. `cambiar_upstream("")`
    deja el proxy en modo DIRECTO (sin upstream), para cuentas sin proxy
    (`TWITTER_SIN_PROXY`), donde el proxy local conecta directo al destino.

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

    El constructor es TOLERANTE: sin `host`/`port` validos arranca en modo
    directo (sin upstream), pensado para el modo dinamico de `aplicar_a_options`.
    """

    # Tamano maximo de la cola de conexiones pendientes: si se llena, la
    # conexion se rechaza (evita acumular trabajo/memoria sin limite).
    COLA_MAX = 256

    def __init__(self, host: str = None, port: int = 0, username: str = "", password: str = ""):
        # Tolerante: si no hay host/port validos, modo DIRECTO (sin upstream:
        # el proxy local conecta directo al destino). Lo usa el modo dinamico
        # de `ProxyManager.aplicar_a_options` para cuentas sin proxy.
        self.host = (host or "").strip()
        try:
            self.port = int(port or 0)
        except (TypeError, ValueError):
            self.port = 0
        if username or password:
            self.auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        else:
            # Sin credenciales no se manda Proxy-Authorization.
            self.auth = ""
        self._directo = not (self.host and self.port > 0)
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
        destino = "DIRECTO (sin upstream)" if self._directo else f"{self.host}:{self.port}"
        logger.info(
            f"Proxy local de auth en 127.0.0.1:{self.port_local} "
            f"-> {destino} "
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

    def cambiar_upstream(self, proxy: str = "") -> bool:
        """Cambia el upstream EN CALIENTE (pestaña persistente).

        `proxy` vacio => modo DIRECTO (`_directo=True`, sin upstream). Si viene
        `http://user:pass@host:port`, `user:pass@host:port` o `host:port`
        (parseado con `urlsplit`; sin credenciales NO se manda
        `Proxy-Authorization`). Nunca lanza; devuelve True si aplico el cambio.

        Cierra las conexiones cliente ACTIVAS (recogidas bajo lock y cerradas
        FUERA del lock: `_discard` toma el mismo lock) para que Chrome recree
        los tuneles por el upstream nuevo. Los workers del pool quedan intactos.
        """
        try:
            texto = (proxy or "").strip()
            host, port, username, password = "", 0, "", ""
            if texto:
                s = texto if "://" in texto else f"http://{texto}"
                partes = urlsplit(s)
                host = (partes.hostname or "").strip()
                try:
                    port = int(partes.port or 0)
                except (TypeError, ValueError):
                    port = 0
                username = partes.username or ""
                password = partes.password or ""
                if not host or port <= 0:
                    logger.warning(
                        f"Forward proxy: upstream invalido {texto!r}; se "
                        "mantiene el upstream actual"
                    )
                    return False

            with self._lock:
                self.host = host
                self.port = port
                if username or password:
                    self.auth = base64.b64encode(
                        f"{username}:{password}".encode()
                    ).decode()
                else:
                    self.auth = ""
                self._directo = not (host and port > 0)
                # Recoger bajo lock; cerrar FUERA (evita deadlock con _discard).
                activas = list(self._conns)
                self._conns.clear()

            for conn in activas:
                # shutdown primero: despierta a los workers que esten en
                # `recv`/`select` con un cierre limpio (en Windows cerrar el
                # socket bajo un recv ajeno da WinError 10038).
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.debug(
                f"Forward proxy: no se pudo cambiar el upstream "
                f"({type(e).__name__}: {e})"
            )
            return False

    @staticmethod
    def _partir_hostport(hostport):
        """Convierte `host:port` (o `[ipv6]:port`) a tupla `(host, port)`."""
        try:
            h = str(hostport or "").strip()
            if h.startswith("["):
                corte = h.find("]")
                host = h[1:corte]
                resto = h[corte + 1:]
                return host, int(resto.lstrip(":") or 0)
            host, _, puerto = h.rpartition(":")
            return host, int(puerto or 0)
        except Exception:
            return "", 0

    def _upstream(self, hostport=None):
        """Conecta al destino: DIRECTO al host o al proxy configurado.

        `hostport` (str `host:port` o tupla) solo aplica en modo directo; en
        modo proxy se conecta siempre al upstream configurado, como antes.
        """
        if self._directo:
            destino = hostport
            if destino is None:
                destino = (self.host, self.port)
            elif isinstance(destino, str):
                destino = self._partir_hostport(destino)
            if not destino or not destino[0] or int(destino[1]) <= 0:
                raise OSError("host de destino invalido")
            return socket.create_connection(tuple(destino), timeout=25)
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
            if _es_socket_cerrado(e):
                # Cambio de upstream / cierre del proxy: no es un error real.
                logger.debug(
                    f"Forward proxy: conexion cerrada desde otro hilo "
                    f"({type(e).__name__}: {e})"
                )
            else:
                logger.error(f"Forward proxy error: {type(e).__name__}: {e}")
        finally:
            self._discard(client)

    def _tunnel(self, client, first_line, data):
        trozos = first_line.split(" ")
        hostport = trozos[1] if len(trozos) > 1 else ""

        if self._directo:
            # Modo directo: NO se habla con ningun upstream; se responde el 200
            # al cliente y se hace relay DIRECTO a `hostport`.
            self._tunnel_directo(client, hostport, data)
            return

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
                cabeceras = [
                    f"CONNECT {hostport} HTTP/1.1",
                    f"Host: {hostport}",
                ]
                if self.auth:
                    cabeceras.append(f"Proxy-Authorization: Basic {self.auth}")
                cabeceras.append("Proxy-Connection: keep-alive")
                req = ("\r\n".join(cabeceras) + "\r\n\r\n").encode("latin-1")
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

    def _tunnel_directo(self, client, hostport, data):
        """Relay DIRECTO del tunel (modo sin upstream). Nunca lanza."""
        upstream = None
        try:
            upstream = self._upstream(hostport)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            # bytes extra que el cliente pudo enviar junto al CONNECT (TLS hello)
            partes = data.split(b"\r\n\r\n", 1)
            if len(partes) > 1 and partes[1]:
                upstream.sendall(partes[1])
        except Exception as e:
            logger.debug(
                f"Forward proxy (directo): fallo el tunel a {hostport}: "
                f"{type(e).__name__}: {e}"
            )
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass
            return
        self._relay(client, upstream)

    def _forward_plain(self, client, data):
        if self._directo:
            self._forward_plain_directo(client, data)
            return
        upstream = self._upstream()
        # Insertar Proxy-Authorization tras la linea de request (solo si hay
        # credenciales; sin ellas no se manda el header).
        if self.auth:
            header, sep, rest = data.partition(b"\r\n")
            if sep:
                data = header + b"\r\nProxy-Authorization: Basic " + self.auth.encode() + b"\r\n" + rest
        upstream.sendall(data)
        self._relay(client, upstream)

    @staticmethod
    def _destino_plain(data):
        """Extrae `(host, port)` del request-line/Host de un request HTTP plano."""
        try:
            linea = data.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
            trozos = linea.split(" ")
            objetivo = trozos[1] if len(trozos) > 1 else ""
            if "://" in objetivo:
                partes = urlsplit(objetivo)
                host = (partes.hostname or "").strip()
                puerto = partes.port or (443 if partes.scheme == "https" else 80)
                return host, int(puerto)
            if objetivo.startswith("/"):
                for cabecera in data.split(b"\r\n"):
                    if cabecera.lower().startswith(b"host:"):
                        valor = cabecera.split(b":", 1)[1].decode("latin-1", "ignore").strip()
                        return LocalForwardProxy._partir_hostport(valor)
            return LocalForwardProxy._partir_hostport(objetivo)
        except Exception:
            return "", 0

    def _forward_plain_directo(self, client, data):
        """HTTP plano sin upstream: conecta al host del request-line."""
        upstream = None
        try:
            host, puerto = self._destino_plain(data)
            if not host or puerto <= 0:
                return
            upstream = socket.create_connection((host, puerto), timeout=25)
            upstream.sendall(data)
        except Exception as e:
            logger.debug(
                f"Forward proxy (directo): fallo HTTP plano: "
                f"{type(e).__name__}: {e}"
            )
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass
            return
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

"""A local HTTP CONNECT proxy that forwards over the SSH SOCKS5 tunnel.

mpv hands --http-proxy straight to FFmpeg, whose HTTP protocol only speaks HTTP
CONNECT -- it ignores a socks5:// value and streams direct, which is a real leak
on exactly the traffic that matters. So mpv is pointed at this instead.
"""
import select
import socket
import socketserver
import struct
import threading
from urllib.parse import urlsplit

# Why this exists: mpv hands --http-proxy straight to FFmpeg, and FFmpeg's HTTP
# protocol only speaks HTTP CONNECT — it silently ignores a socks5:// value and
# streams direct. That is a real IP leak on the exact traffic we care about.
# So we expose a local HTTP CONNECT proxy that forwards over the SSH SOCKS5
# tunnel, and point mpv at that instead.
# --------------------------------------------------------------------------- #

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("socks: connection closed early")
        buf += chunk
    return buf


def socks5_connect(dest_host, dest_port, proxy_port, timeout=15):
    """Open a TCP stream to dest via a no-auth SOCKS5 proxy on localhost.

    Hostnames are sent to the proxy verbatim (socks5h semantics), so DNS is
    resolved on the far side and never leaks locally.
    """
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    ver, method = _recv_exact(s, 2)
    if ver != 5 or method != 0:
        s.close()
        raise OSError("socks: handshake refused")

    host_b = dest_host.encode("idna") if not _is_ip(dest_host) else dest_host.encode()
    if len(host_b) > 255:
        s.close()
        raise OSError("socks: hostname too long")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + struct.pack(">H", dest_port))

    head = _recv_exact(s, 4)
    if head[1] != 0:
        s.close()
        raise OSError("socks: server replied error %d" % head[1])
    atyp = head[3]
    if atyp == 1:
        _recv_exact(s, 4)
    elif atyp == 3:
        _recv_exact(s, _recv_exact(s, 1)[0])
    elif atyp == 4:
        _recv_exact(s, 16)
    _recv_exact(s, 2)
    s.settimeout(None)
    return s


def _is_ip(value):
    try:
        socket.inet_aton(value)
        return True
    except OSError:
        return ":" in value


def _relay(a, b):
    """Pump bytes both ways until either side hangs up."""
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x or not r:
                break
            for src in r:
                dst = b if src is a else a
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


class _BridgeHandler(socketserver.StreamRequestHandler):
    timeout = 60

    def handle(self):
        try:
            line = self.rfile.readline(65536).decode("latin-1").strip()
        except Exception:
            return
        if not line:
            return

        parts = line.split()
        if len(parts) < 3:
            self._fail(400, "Malformed request line")
            return
        method, target, version = parts[0], parts[1], parts[2]

        headers = []
        while True:
            raw = self.rfile.readline(65536)
            if not raw or raw in (b"\r\n", b"\n"):
                break
            headers.append(raw.decode("latin-1").rstrip("\r\n"))

        port = self.server.socks_port
        if method.upper() == "CONNECT":
            host, _, p = target.rpartition(":")
            try:
                upstream = socks5_connect(host, int(p or 443), port)
            except Exception as exc:
                self._fail(502, "Upstream tunnel refused: %s" % exc)
                return
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            _relay(self.connection, upstream)
            return

        # Absolute-form request (plain HTTP through a proxy)
        u = urlsplit(target)
        if not u.hostname:
            self._fail(400, "Proxy needs an absolute URL")
            return
        try:
            upstream = socks5_connect(u.hostname, u.port or 80, port)
        except Exception as exc:
            self._fail(502, "Upstream tunnel refused: %s" % exc)
            return

        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        kept = [h for h in headers if not h.lower().startswith("proxy-")]
        req = "%s %s %s\r\n%s\r\n\r\n" % (method, path, version, "\r\n".join(kept))
        try:
            upstream.sendall(req.encode("latin-1"))
        except OSError:
            upstream.close()
            return
        _relay(self.connection, upstream)

    def _fail(self, code, msg):
        try:
            body = msg.encode()
            self.wfile.write(
                ("HTTP/1.1 %d Proxy Error\r\nContent-Length: %d\r\n"
                 "Connection: close\r\n\r\n" % (code, len(body))).encode()
            )
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *_a, **_k):
        pass


class _BridgeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class HttpBridge:
    """Local HTTP CONNECT proxy -> SSH SOCKS5 tunnel."""

    def __init__(self, listen_port, socks_port):
        self.listen_port = listen_port
        self.socks_port = socks_port
        self.server = None
        self.thread = None

    def start(self):
        self.server = _BridgeServer(("127.0.0.1", self.listen_port), _BridgeHandler)
        self.server.socks_port = self.socks_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None

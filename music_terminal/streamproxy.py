# -*- coding: utf-8 -*-
"""Proxy local de rangos para streams «bounded-range» de googlevideo.

YouTube limita las URLs emitidas por clientes móviles (ANDROID_VR) a
peticiones de rango acotado: un GET completo / open-ended responde 403.
Este servidor HTTP local (127.0.0.1) recibe las peticiones de mpv y las
re-sirve a googlevideo en ventanas finitas (igual que hace ExoPlayer en
la app), escribiendo la respuesta completa y continua para el cliente.
"""
import http.server
import threading
import time
import urllib.error
import urllib.request

CHUNK = 128 * 1024                 # ventana upstream (googlevideo admite
                                   # rangos acotados de ≲256 KiB por petición)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Atiende /audio desde mpv; delega en StreamProxy.serve_range()."""
    protocol_version = "HTTP/1.1"
    proxy = None                   # lo asigna StreamProxy.start()

    def log_message(self, *a):     # silencio en la TUI
        pass

    def do_GET(self):
        if self.path.rstrip("/") != "/audio":
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        if not self.proxy.serve_range(self, rng):
            try:
                self.send_error(502)
            except Exception:
                pass

    def do_HEAD(self):
        if self.path.rstrip("/") != "/audio":
            self.send_error(404)
            return
        clen, mime = self.proxy.describe()
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        if clen:
            self.send_header("Content-Length", str(clen))
        self.send_header("Content-Type", mime)
        self.end_headers()


class StreamProxy:
    """Servidor local: sirve /audio troceando la URL actual en rangos."""

    def __init__(self):
        self.current_url = None
        self.current_clen = None
        self.current_mime = "audio/webm"
        self.current_ua = "com.google.android.apps.youtube.vr.oculus/1.65.10"
        self.port = None
        self.server = None
        self.thread = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- #
    def start(self):
        if self.server:
            return self.port
        handler = _Handler
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        handler.proxy = self
        self.server = server
        self.port = server.server_address[1]
        self.thread = threading.Thread(
            target=server.serve_forever, name="streamproxy", daemon=True)
        self.thread.start()
        return self.port

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except OSError:
                pass
        self.server = None
        self.port = None

    # ---------------------------------------------------------------- #
    def describe(self):
        with self._lock:
            return self.current_clen, self.current_mime

    def set_stream(self, url, clen=None, mime=None, ua=None):
        """Registra la URL que /audio servirá (al siguiente play)."""
        with self._lock:
            self.current_url = url
            self.current_clen = int(clen) if clen else None
            self.current_mime = mime or "audio/webm"
            self.current_ua = ua or self.current_ua
        if not self.port:
            self.start()
        return f"http://127.0.0.1:{self.port}/audio"

    # ---------------------------------------------------------------- #
    def _fetch(self, url, start, end, ua):
        """Una petición upstream de rango acotado → bytes (falla → raise)."""
        req = urllib.request.Request(
            url, headers={"Range": f"bytes={start}-{end}", "User-Agent": ua})
        last = None
        for i in range(3):              # dos reintentos ante fallos leves
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    if r.status not in (200, 206):
                        raise OSError(f"upstream {r.status}")
                    return r.read()
            except urllib.error.HTTPError:
                raise                   # 4xx: no insistir (URL caducada)
            except (urllib.error.URLError, OSError) as e:
                last = e
                if i < 2:
                    time.sleep(0.6)
        raise last or OSError("upstream sin respuesta")

    def _probe_len(self, url):
        """Content-Length total vía rango bytes=0-0 (Content-Range)."""
        try:
            req = urllib.request.Request(
                url, headers={"Range": "bytes=0-0",
                              "User-Agent": self.current_ua})
            with urllib.request.urlopen(req, timeout=30) as r:
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    return int(cr.rsplit("/", 1)[1])
        except (Exception,):
            pass
        return None

    # ---------------------------------------------------------------- #
    def serve_range(self, handler, range_hdr):
        """Contesta la Range de mpv escribiendo en su wfile.

        Siempre contesta 206; la petición del cliente se sirve de una pieza
        mientras el upstream se hace en ventanas acotadas (CHUNK)."""
        with self._lock:
            url = self.current_url
            clen = self.current_clen
            mime = self.current_mime
            ua = self.current_ua
        if not url:
            return None
        if clen is None:
            clen = self._probe_len(url)
            if clen:
                with self._lock:
                    if self.current_url == url:
                        self.current_clen = clen
            else:
                return None

        # --- Range del cliente ------------------------------------- #
        start, open_end = 0, True
        if range_hdr and range_hdr.lower().startswith("bytes="):
            try:
                spec = range_hdr.split("=", 1)[1].strip()
                first, last = (spec.split("-", 1) + [""])[:2]
                start = int(first) if first else 0
                open_end = not last
                if last:
                    end = int(last)
                else:
                    end = clen - 1
            except (ValueError, IndexError):
                start, end, open_end = 0, clen - 1, True
        else:
            end = clen - 1
        if start >= clen or start < 0:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{clen}")
            handler.end_headers()
            return True
        start = min(start, clen - 1)
        end = min(end, clen - 1)
        if end < start:
            end = start

        # --- respuesta 206 y ventana acotada contra googlevideo ----- #
        handler.send_response(206)
        handler.send_header("Content-Type", mime)
        n = end - start + 1
        handler.send_header("Content-Length", str(n))
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Range", f"bytes {start}-{end}/{clen}")
        handler.end_headers()

        pos = start
        try:
            while pos <= end:
                wend = min(pos + CHUNK - 1, end)
                # googlevideo puede devolver menos de lo pedido: se sigue
                data = self._fetch(url, pos, wend, ua)
                handler.wfile.write(data)
                pos += len(data)
        except (BrokenPipeError, ConnectionResetError):
            return True                 # mpv cortó la conexión: sin drama
        except Exception:
            handler.close_connection = True  # EOF le avisa a mpv
        return True


# ----------------------------------------------------------------------
# prueba rápida: python3 streamproxy.py
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    proxy = StreamProxy()
    proxy.set_stream(sys.argv[1] if len(sys.argv) > 1 else
                     "https://example.invalid/videoplayback")
    print(f"puerto {proxy.port}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        proxy.stop()

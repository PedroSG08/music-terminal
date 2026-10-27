# -*- coding: utf-8 -*-
"""Copias locales para oír sin conexión (la carpeta «offline»).

Guarda el propio flujo de audio que mpv reproduce (la URL directa del
extractor) por id de vídeo:  DATA_DIR/offline/<vid>.<webm|m4a|…>

Al reproducir, `App.play_stream` intenta primero la copia: si está en
la carpeta no se toca ni el extractor ni la red — la playlist guardada
se oye con elWiFi caído.  Las descargas van en cola serial (un hilo)
para no machacar el CDN.
"""
import os
import queue as _queue
import threading
import time
import urllib.error
import urllib.request

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_UA_SONDA = "com.google.ios.youtube/20.10.4"   # la UA de la sonda que
#             valida la URL del extractor: con ella la ventana responde

_VENT = 128 * 1024            # ventana upstream; el CDN acota el rango
#                              completo a 403 y raciona cada URL a ~1 MiB

_EXT_MIN = 32 * 1024          # un audio que pese menos no es un audio


def human_bytes(n):
    n = float(n or 0)
    for unit in ("o", "Kio", "Mio", "Gio"):
        if n < 1024 or unit == "Gio":
            return (f"{n:.1f} {unit}" if unit != "o"
                    else f"{int(n)} {unit}")
        n /= 1024
    return f"{n:.1f} Gio"


def _ext_for(mime):
    """Extensión coherente con el mime del stream."""
    mime = (mime or "").lower()
    if "webm" in mime or "opus" in mime:
        return "webm"
    if "mp4" in mime or "m4a" in mime or "aac" in mime:
        return "m4a"
    if "mp3" in mime:
        return "mp3"
    if "mp4a" in mime:
        return "m4a"
    if "ogg" in mime:
        return "ogg"
    if "wav" in mime:
        return "wav"
    return "webm"


class LocalStore:
    """La carpeta de copias y las consultas (memoizadas, baratas)."""

    EXTS = ("webm", "m4a", "mp3", "mka", "opus", "ogg", "wav", "mp4")

    def __init__(self, root):
        self.root = root                  # …/offline
        self._memo = {}                   # vid → ruta | False
        self._lock = threading.Lock()
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass

    def path_for(self, vid, ext=None):
        if not vid:
            return None
        if ext:
            return os.path.join(self.root, f"{vid}.{ext}")
        with self._lock:
            if vid in self._memo:
                return self._memo[vid] or None
        for e in self.EXTS:
            p = os.path.join(self.root, f"{vid}.{e}")
            try:
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    with self._lock:
                        self._memo[vid] = p
                    return p
            except OSError:
                continue
        with self._lock:
            self._memo[vid] = False
        return None

    def register(self, vid, path):
        """Anota una copia nueva (tras escribir el fichero)."""
        with self._lock:
            self._memo[vid] = path

    def forget(self, vid):
        with self._lock:
            self._memo.pop(vid, None)

    def all(self):
        """[(vid, ruta), …] de las copias presentes."""
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        out = []
        for n in names:
            for e in self.EXTS:
                if n.endswith("." + e):
                    out.append((n[: -len(e) - 1], os.path.join(self.root, n)))
                    break
        return out

    def stats(self):
        items = self.all()
        total = 0
        for _, p in items:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return len(items), total

    def wipe(self, vid=None):
        """Borra una copia (o todas) y limpia la memoria."""
        if vid is None:
            for _, p in self.all():
                try:
                    os.remove(p)
                except OSError:
                    pass
            with self._lock:
                self._memo.clear()
            return
        for _, p in self.all():
            if p.startswith(os.path.join(self.root, vid + ".")):
                try:
                    os.remove(p)
                except OSError:
                    pass
        self.forget(vid)


class Downloader:
    """Cola serial de descargas (un hilo duro, sin machacar el CDN)."""

    CHUNK = 256 * 1024

    def __init__(self, extractor, store, notify=None):
        self.extractor = extractor
        self.store = store
        self.notify = notify or (lambda msg, tone="info": None)
        self._q = _queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def has(self, vid):
        return self.store.path_for(vid)

    def submit(self, vid, title=""):
        """Encola una copia (nada si ya está local o pende)."""
        if not vid or self.has(vid):
            return False
        with self._lock:
            if vid in self._pending:
                return False
            self._pending.add(vid)
        self._q.put((vid, title))
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker,
                                            daemon=True, name="descargas")
            self._thread.start()
        return True

    def _worker(self):
        while not self._stop.is_set():
            try:
                vid, title = self._q.get(timeout=0.6)
            except _queue.Empty:
                with self._lock:
                    quiet = not self._pending and self._q.empty()
                if quiet:
                    break
                continue
            err = ""
            ok = False
            for intento in (1, 2):          # un reintento: los baches de
                try:                        # red no deben tumbar la copia
                    ok = bool(self.download(vid))
                    err = ""
                    break
                except Exception as e:
                    ok = False
                    err = (f" ({type(e).__name__}: {str(e)[:110]})"
                           if str(e) or type(e).__name__ != "Exception"
                           else "")
                    if intento == 1:
                        time.sleep(4)
            with self._lock:
                self._pending.discard(vid)
            if ok:
                self.notify(f"⤓ guardada en local: {title or vid}", "info")
            else:
                self.notify(f"la copia local no pudo guardar: "
                            f"{title or vid}{err}", "warn")

    @staticmethod
    def _avisar(progress, vid, got, total):
        try:
            progress(vid, got, total)
        except Exception:
            pass

    @staticmethod
    def _total_de(url, clen, ua):
        """Tamaño completo vía rango bytes=0-0 (Content-Range) si falta."""
        if int(clen or 0):
            return int(clen)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": ua, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    resto = cr.rsplit("/", 1)[-1]
                    if resto.isdigit():
                        return int(resto)
        except Exception:
            pass
        return 0

    def _renovar(self, vid, total):
        """URL nueva para la misma copia (el CDN cerró la anterior)."""
        try:
            self.extractor.invalidate(vid)
        except Exception:
            pass
        url, desc, clen, mime, _pdata = self.extractor.get_stream(vid)
        nt = int(clen or 0)
        if nt and total and nt != total:
            raise OSError("el tamaño del stream cambió al renovar la URL")
        return url, (nt or total)

    def download(self, vid, progress=None):
        """Baja el stream directo a la carpeta (sincrónico).

        El CDN a veces rechaza el pedido completo (`bytes=0-`) con 403
        (fuentes de rango acotado — por eso existe el StreamProxy para
        mpv) y además raciona cada URL a ~1 MiB.  Secuencia: pedido
        abierto a caudal completo; si da 403, ventanas de _VENT con la
        UA de la sonda, renovando la URL cuando se agota el cupo."""
        got_path = self.has(vid)
        if got_path:
            return got_path
        url, desc, clen, mime, _pdata = self.extractor.get_stream(vid)
        ext = _ext_for(mime)
        final = self.store.path_for(vid, ext)
        tmp = final + ".parte"
        total = self._total_de(url, clen, _UA_SONDA)
        got = 0
        renovando = 0            # renovaciones seguidas sin bytes nuevos
        peticiones = 0
        try:
            with open(tmp, "wb") as f:
                modo = "abierto"
                while True:
                    if total and got >= total:
                        break
                    if modo == "abierto":
                        req = urllib.request.Request(
                            url, headers={"User-Agent": _UA,
                                          "Range": "bytes=0-"})
                        try:
                            with urllib.request.urlopen(req, timeout=20) as r:
                                cr = r.headers.get("Content-Range") or ""
                                resto = cr.rsplit("/", 1)[-1]
                                if resto.isdigit() and not total:
                                    total = int(resto)
                                while True:
                                    ch = r.read(self.CHUNK)
                                    if not ch:
                                        break
                                    f.write(ch)
                                    got += len(ch)
                                    if progress:
                                        self._avisar(progress, vid, got,
                                                     total)
                            break       # el pedido abierto va hasta el EOF
                        except urllib.error.HTTPError as e:
                            if e.code != 403:
                                raise
                            modo = "ventanas"
                            continue
                        # ---- fin modo abierto (sale por break/raise) ----
                    modo = "ventanas"
                    peticiones += 1
                    if peticiones > 4000:
                        raise OSError("demasiadas ventanas para esta copia")
                    fin = (min(got + _VENT, total - 1) if total
                           else got + _VENT - 1)
                    pend = fin + 1 - got
                    req = urllib.request.Request(
                        url, headers={"User-Agent": _UA_SONDA,
                                      "Range": f"bytes={got}-{fin}"})
                    caido = ""
                    try:
                        with urllib.request.urlopen(req, timeout=45) as r:
                            while pend > 0:
                                ch = r.read(pend)
                                if not ch:
                                    break
                                f.write(ch)
                                got += len(ch)
                                pend -= len(ch)
                                if progress:
                                    self._avisar(progress, vid, got, total)
                    except urllib.error.HTTPError as e:
                        if e.code == 416 and not total:
                            break     # la ventana ya tocó el último byte
                        if e.code != 403:
                            raise
                        caido = f"HTTP {e.code}"
                    except (urllib.error.URLError, OSError) as e:
                        # bache de red a mitad de la ventana: otra URL
                        caido = (str(e) or type(e).__name__)[:60]
                    if caido:
                        if renovando >= 2:
                            raise OSError("el CDN no da más bytes "
                                          "de esta fuente")
                        url, total = self._renovar(vid, total)
                        renovando += 1
                        continue
                    if pend > 0:
                        # ventana servida corta sin total: se acabó de verdad
                        if not total:
                            break
                        if renovando >= 2:
                            raise OSError("el CDN va corto para esta fuente")
                        url, total = self._renovar(vid, total)
                        renovando += 1
                        continue
                    renovando = 0      # ventana servida entera: todo en orden
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        if got < _EXT_MIN:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise OSError("descarga sospechosamente corta")
        os.replace(tmp, final)
        self.store.register(vid, final)
        return final

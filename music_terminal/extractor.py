# -*- coding: utf-8 -*-
"""Extracción de streams de audio (equivalente a InnerTubeXPlayer de la app).

Ruta principal: endpoint `player` con el cliente VISIONOS_0_1 (la ruta
automática del catálogo de la app), que entrega URLs de audio (opus/m4a)
de rango abierto — mpv las reproduce directamente.
Rutas de respaldo: proxy local de rangos acotados (streamproxy) y
yt-dlp (binario opcional descargado a DATA_DIR/bin).
"""
import json
import os
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request

STREAM_TTL_SECONDS = 5 * 60          # igual que StreamUrlCache.kt de la app
YTDLP_URL = ("https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
             "yt-dlp_linux")

# bitrate máx. por calidad (kbps): high toma el mejor disponible (~160),
# medium ~128, low ~80
QUALITY_LIMIT = {"high": 192, "medium": 128, "low": 80}

# escalera client del endpoint `player`, de preferencia a respaldo.
# anon=True → la petición se hace SIN cookies (los clientes privados
# session-bound — VISIONOS/ANDROID_VR — devuelven HTTP 400 con sesión
# desde que YouTube los liga a la sesión; IOS es la primera opción
# funcional actual sin cookies).
NATIVE_LADDER = (("IOS", True), ("VISIONOS_0_1", False), ("ANDROID_VR", False))

STREAM_TTL = 30 * 60                 # las URLs del player duran ~6 h

# bytes que se leen de la URL para validar que responde (403 → cliente caído)
PROBE_BYTES = 40 * 1024


class ExtractionError(Exception):
    pass


class Extractor:
    def __init__(self, api, settings, data_dir, log=None):
        self.api = api
        self.settings = settings
        self.data_dir = data_dir
        self._log = log or (lambda *a: None)
        self._cache = {}     # videoId → (ts, (url, desc, clen, mime, pdata))
        self._anon = None    # InnerTube sin cookies para clientes anónimos
        self._inflight = set()   # prefetch en curso
        self._lock = threading.Lock()
        self._streams_file = os.path.join(data_dir, "streams.json")

    # ------------------------------------------------------------------ #
    # caché de streams en disco (rejugados instantáneos)
    # ------------------------------------------------------------------ #
    def _disk_get(self, video_id):
        if not os.path.isfile(self._streams_file):
            return None
        try:
            with open(self._streams_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        entry = data.get(video_id)
        if not entry or time.time() - entry.get("ts", 0) > STREAM_TTL:
            return None
        return (entry["url"], entry["desc"], entry.get("clen"),
                entry["mime"], None)

    def _disk_put(self, video_id, url, desc, clen, mime, ts=None):
        data = {}
        if os.path.isfile(self._streams_file):
            try:
                with open(self._streams_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        # purga: elimina expiradas y cap a 120 (deja las más recientes)
        now = time.time()
        data = {k: v for k, v in data.items()
                if now - v.get("ts", 0) < STREAM_TTL}
        if len(data) >= 120:
            recent = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0),
                            reverse=True)[:119]
            data = dict(recent)
        data[video_id] = {"ts": ts or now, "url": url, "desc": desc,
                          "clen": clen, "mime": mime}
        try:
            tmp = self._streams_file + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._streams_file)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # api anónima (sin cookies) para la escalera de clientes
    # ------------------------------------------------------------------ #
    def _anon_api(self):
        if self._anon is None:
            from .innertube import InnerTube
            session = getattr(self.api, "session_path", None)
            self._anon = InnerTube(region=getattr(self.api, "region", "ES"),
                                   language=getattr(self.api, "language", "es"),
                                   session_path=(session + ".anon"
                                                 if session else None),
                                   auth_path=None)
        return self._anon

    @staticmethod
    def _url_alive(url):
        """Sonda de la URL: ¿responde de verdad con el rango ABIERTO?

        Con un rango acotado (ventana) casi todos los clientes pasan,
        pero luego mpv/el pedido completo topa con un 403 a mitad de
        flujo (fuente de rango acotado).  La sonda abre con `bytes=0-`
        para casarse SOLO con URLs que dejan bajar el fichero entero.
        Devuelve None si la URL vale; si no, el motivo («HTTP 403» o
        «sin respuesta») para no confundir una fuente acotada con un
        bache pasajero de la red."""
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "com.google.ios.youtube/20.10.4",
                              "Range": "bytes=0-"})
            with urllib.request.urlopen(req, timeout=6) as r:
                if r.read(PROBE_BYTES):
                    return None
            return "respuesta vacía"
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code}"
        except Exception:
            return "sin respuesta"

    # ------------------------------------------------------------------ #
    # helper externo (opcional)
    # ------------------------------------------------------------------ #
    def helper_path(self):
        return os.path.join(self.data_dir, "bin", "yt-dlp")

    def helper_ready(self):
        p = self.helper_path()
        return os.path.isfile(p) and os.access(p, os.X_OK)

    def download_helper(self):
        os.makedirs(os.path.dirname(self.helper_path()), exist_ok=True)
        tmp = self.helper_path() + ".part"
        try:
            with urllib.request.urlopen(YTDLP_URL, timeout=300) as r, \
                    open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    self._log(f"descargando yt-dlp… {done // (1<<20)} MB"
                              + (f"/{total // (1<<20)} MB" if total else ""))
            mode = os.stat(tmp).st_mode | stat.S_IRWXU | stat.S_IEXEC
            os.chmod(tmp, mode)
            os.replace(tmp, self.helper_path())
            return True
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # entrada
    # ------------------------------------------------------------------ #
    def get_stream(self, video_id):
        """Devuelve (url, descripción, len_bytes, mime, player_data)
        o lanza Error.  player_data es el `playerResponse` (None en yt-dlp)."""
        mode = self.settings.get("extractor", "auto")
        cached = self._cache.get(video_id)
        if cached and time.time() - cached[0] < STREAM_TTL:
            return cached[1]
        disk = self._disk_get(video_id)
        if disk:
            self._cache[video_id] = (time.time(), disk)
            return disk
        errors = []
        if mode in ("auto", "native"):
            try:
                stream = self._native(video_id)
                self._cache[video_id] = (time.time(), stream)
                self._disk_put(video_id, *stream[:4])
                return stream
            except Exception as e:
                errors.append(f"nativo: {str(e)[:160]}")
        if mode in ("auto", "ytdlp"):
            if self.helper_ready():
                try:
                    stream = self._ytdlp(video_id)
                    self._cache[video_id] = (time.time(), stream)
                    self._disk_put(video_id, *stream[:4])
                    return stream
                except Exception as e:
                    errors.append(f"yt-dlp: {str(e)[:130]}")
            else:
                errors.append("helper yt-dlp: no instalado (Ajustes)")
        raise ExtractionError("; ".join(errors) or "sin extractor disponible")

    def invalidate(self, video_id):
        """Olvida un stream (URL que mpv no pudo abrir)."""
        with self._lock:
            self._cache.pop(video_id, None)
        try:
            if os.path.isfile(self._streams_file):
                with open(self._streams_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.pop(video_id, None)
                tmp = self._streams_file + ".part"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, self._streams_file)
        except OSError:
            pass

    # ---- prefetch (siguiente canción en segundo plano) ---- #
    def prefetch(self, video_id):
        """Resuelve el stream en segundo plano y lo deja cacheado."""
        if not video_id:
            return
        with self._lock:
            if video_id in self._inflight:
                return
            self._inflight.add(video_id)

        def work():
            try:
                self.get_stream(video_id)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._inflight.discard(video_id)

        threading.Thread(target=work, name=f"pref-{video_id[:8]}",
                         daemon=True).start()

    # ------------------------------------------------------------------ #
    def _native(self, video_id):
        """Endpoint `player` con escalera de clientes (IOS anónimo →
        VISIONOS → ANDROID_VR).  Los session-bound con cookie dan HTTP 400
        desde que YouTube los ligó a la sesión, así que las peticiones
        privadas van siempre sin cookies; la primera URL que responde
        (sonda de rango abierto —see `_url_alive`—, mpv la reproduce
        directo) gana."""
        body = {"videoId": video_id, "contentCheckOk": True,
                "racyCheckOk": True}
        errors = []
        deadline = time.time() + 10.0       # techo: si la escalera no da
        # URL viva en ~10 s, mejor saltar a yt-dlp que seguir esperando
        for client_key, anon in NATIVE_LADDER:
            if time.time() > deadline:
                errors.append("escalera: techo de 5 s alcanzado")
                break
            try:
                api = self._anon_api() if anon else self.api
                api.ensure_visitor()
                data = api._post("player", body,
                                 client_key=client_key, path="youtubei/v1")
            except Exception as e:
                errors.append(f"{client_key}: {str(e)[:90]}")
                continue
            ps = (data.get("playabilityStatus") or {})
            if ps.get("status") != "OK":
                reason = ps.get("reason") or ps.get("status") or "?"
                errors.append(f"{client_key}: {reason[:90]}")
                continue
            fmts = (data.get("streamingData") or {}).get("adaptiveFormats") or []
            audio = [f for f in fmts
                     if f.get("url") and str(f.get("mimeType", "")).startswith("audio/")]
            if not audio:
                errors.append(f"{client_key}: sin formatos de audio")
                continue
            quality = self.settings.get("audio_quality", "high")
            limit = QUALITY_LIMIT.get(quality, 192)

            def br(f):
                try:
                    return int(f.get("bitrate", 0) or 0) // 1000
                except (TypeError, ValueError):
                    return 0

            under = [f for f in audio if br(f) <= limit]
            for cand in sorted(audio if not under else under,
                               key=br, reverse=True):
                url = cand["url"]
                motivo = self._url_alive(url)
                if motivo:
                    errors.append(f"{client_key}: URL no vale ({motivo})")
                    continue
                mime = str(cand.get("mimeType", "audio")).split(";")[0]
                desc = f"{mime} @{br(cand)}K ({client_key})"
                clen = None
                try:
                    clen = int(cand.get("contentLength") or 0) or None
                except (TypeError, ValueError):
                    clen = None
                return url, desc, clen, mime, data
            errors.append(f"{client_key}: todas las URLs fallan la sonda")
        raise ExtractionError(" · ".join(errors) or "sin clientes viables")

    def _ytdlp(self, video_id):
        url_page = f"https://music.youtube.com/watch?v={video_id}"
        out = subprocess.run(
            [self.helper_path(), "--format", "bestaudio", "--no-playlist",
             "--quiet", "--print", "url", "--print", "filesize",
             "--print", "ext", url_page],
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            msg = (out.stderr or out.stdout or "").strip()
            raise ExtractionError(msg[:160])
        lines = [ln.strip() for ln in (out.stdout or "").splitlines()
                 if ln.strip()]
        if not lines:
            raise ExtractionError("yt-dlp sin salida")
        url = lines[0]
        clen = None
        mime = "audio/webm"
        for ln in lines[1:]:
            low = ln.lower()
            if clen is None and low.isdigit():
                clen = int(low)
            ext = low if low.isalnum() else ""
            mime = {"webm": "audio/webm", "opus": "audio/opus",
                    "m4a": "audio/mp4", "aac": "audio/aac",
                    "mp3": "audio/mpeg", "flac": "audio/flac",
                    "wav": "audio/wav", "ogg": "audio/ogg",
                    "oga": "audio/ogg"}.get(ext, mime)
        return url, "yt-dlp bestaudio", clen, mime, None


# ---------------------------------------------------------------------- #
# copiar URL al portapapeles (como la app para compartir)
# ---------------------------------------------------------------------- #
def copy_to_clipboard(text):
    cmds = (["wl-copy"], ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"])
    for cmd in cmds:
        try:
            p = subprocess.run(cmd, input=text.strip().encode(), timeout=5,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if p.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False

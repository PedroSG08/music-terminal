# -*- coding: utf-8 -*-
"""Reproductor basado en mpv con IPC (equivalente al MusicService de la app).

Controla mpv por socket unix JSON: play/pausa/seek/volumen y detección de
fin de pista para avanzar la cola.  Sin mpv reproduce el enlace en otras
vías (navegador) como hace AniLinux Terminal.
"""
import itertools
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time


class MpvPlayer:
    WITHOUT_MPV_MSG = "mpv no está instalado (sudo apt install mpv)"

    def __init__(self, volume=80, on_ended=None):
        self.proc = None
        self.addr = None
        self.sock = None            # socket de eventos
        self.on_ended = on_ended
        self.on_error = None        # pista rota a mitad (para rescatarla)
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread = None
        self.volume = volume
        self.pending_url = None     # se carga al estar listo (idle)
        self.pending_notify = False
        self.last_error = None
        self._rids = itertools.count(1)     # id de cada petición IPC
        self._file_consumed = False  # fin de la pista ya contabilizado

    # ------------------------------------------------------------------ #
    def start(self):
        if self.proc and self.proc.poll() is None:
            return True
        if not shutil.which("mpv"):
            self.last_error = self.WITHOUT_MPV_MSG
            return False
        d = tempfile.mkdtemp(prefix="music-terminal-mpv-")
        self.addr = os.path.join(d, "ipc.sock")
        self.last_error = None
        try:
            self._log_path = os.path.join(d, "mpv.log")
            self._logf = open(self._log_path, "w")
            self.proc = subprocess.Popen(
                ["mpv", "--no-video", "--no-terminal", "--really-quiet",
                 "--idle=yes", "--keep-open=no", "--audio-display=no",
                 f"--input-ipc-server={self.addr}",
                 f"--volume={int(self.volume)}"],
                stdout=subprocess.DEVNULL, stderr=self._logf)
        except OSError as e:
            self.last_error = str(e)
            return False
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()
        # esperar a que aparezca el socket
        import time
        for _ in range(100):
            if os.path.exists(self.addr):
                return True
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        if not os.path.exists(self.addr):
            try:
                self._logf.flush()
                with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-400:].strip()
            except OSError:
                tail = ""
            self.last_error = ("mpv no abrió el socket IPC" + (f": {tail}" if tail else ""))
            return False
        return True

    def issue(self, command, wait=False):
        """Envía un comando JSON por el socket (conexión efímera).

        Con wait=True espera la respuesta identificada por «request_id»,
        descartando los eventos que mpv difunde a todos los clientes
        (antes un evento podía mascarar la respuesta: duración 0
        → «?:??» y controles erráticos)."""
        if not self.proc or self.proc.poll() is not None or not self.addr \
                or not os.path.exists(self.addr):
            return False
        payload = dict(command)
        rid = 0
        if wait:
            rid = next(self._rids)
            payload["request_id"] = rid
        for _ in range(3):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(2.0 if wait else 0.5)
                s.connect(self.addr)
                s.sendall((json.dumps(payload) + "\n").encode())
                if not wait:
                    s.close()
                    return True
                data = b""
                s.settimeout(0.4)
                fin = time.time() + 2.0
                while time.time() < fin:
                    try:
                        chunk = s.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    data += chunk
                    resp = self._match_reply(data, rid)
                    if resp is not None:
                        s.close()
                        return resp
                s.close()
                return self._match_reply(data, rid) or {}
            except OSError:
                return False
        return False

    @staticmethod
    def _match_reply(data, rid):
        """Localiza la respuesta del rid pedido entre líneas-JSON."""
        for line in (data or b"").decode("utf-8", "replace").split("\n"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("request_id") == rid:
                return msg
        return None

    # ------------------------------------------------------------------ #
    def play(self, url, title=None):
        if not self.start():
            return False
        self.pending_url = None
        self._file_consumed = False   # la pista nueva tendrá su propio fin
        cmd = {"command": ["loadfile", url, "replace"]}
        if title:
            cmd["command"].append("0")
        ok = self.issue(cmd)
        if ok and title:
            self.issue({"command": ["set", "force-media-title", title]})
        return ok

    def queue_load(self, url):
        self.issue({"command": ["loadfile", url, "append-play"]})

    def stop(self):
        self.issue({"command": ["stop"]})

    def pause(self):
        self.issue({"command": ["set", "pause", "yes"]})

    def resume(self):
        self.issue({"command": ["set", "pause", "no"]})

    def toggle(self):
        self.issue({"command": ["cycle", "pause"]})

    def set_volume(self, vol):
        self.volume = max(0, min(100, int(vol)))
        self.issue({"command": ["set", "volume", self.volume]})

    def seek(self, secs, absolute=False):
        cmd = ["seek", secs, "absolute"] if absolute else ["seek", secs]
        self.issue({"command": cmd})

    def get(self, prop, default=None):
        resp = self.issue({"command": ["get_property", prop]}, wait=True)
        if isinstance(resp, dict):
            return resp.get("data", default)
        return default

    def position(self):
        try:
            return float(self.get("time-pos") or 0)
        except (TypeError, ValueError):
            return 0.0

    def duration(self):
        try:
            return float(self.get("duration") or 0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------ #
    def _on_end_file(self, evt):
        """Clasifica el evento end-file de mpv.

        El fin natural de pista llega como reason="eof" (las variantes
        "eos"/"reach-end" eran la suposición antigua y se aceptan por
        compatibilidad); "error"/"no-more-media" es pista rota y se
        avisa por on_error para que la app intente rescatarla; y
        "stop"/"quit"/"redirect"/"unknown" son mudanzas internas que
        se ignoran.  Cada pista notifica su fin una sola vez."""
        reason = str(evt.get("reason") or
                     evt.get("file_error") or "unknown")
        if reason in ("eof", "eos", "reach-end"):
            if self.on_ended and not self._file_consumed:
                self._file_consumed = True
                self.on_ended()
        elif reason in ("stop", "quit", "redirect") or \
                (reason == "unknown" and not evt.get("file_error")):
            pass
        else:
            if evt.get("file_error"):
                self.last_error = str(evt["file_error"])
            elif reason in ("error", "no-more-media"):
                self.last_error = reason
            if not self._file_consumed:
                self._file_consumed = True
                if self.on_error:
                    self.on_error()
                elif self.on_ended:
                    self.on_ended()

    def _event_loop(self):
        """ lee los eventos persistidos del socket del mpv."""
        import select
        evt_sock = None
        while not self._stop_evt.is_set():
            if not evt_sock or self.proc is None or self.proc.poll() is not None:
                try:
                    if evt_sock:
                        evt_sock.close()
                except OSError:
                    pass
                evt_sock = None
                if self.proc is None or self.proc.poll() is not None:
                    break
                if self.addr and os.path.exists(self.addr):
                    try:
                        evt_sock = socket.socket(socket.AF_UNIX,
                                                 socket.SOCK_STREAM)
                        evt_sock.settimeout(0.2)
                        evt_sock.connect(self.addr)
                        evt_sock.sendall(b'["observe_property",1,"idle-active"]\n')
                    except OSError:
                        evt_sock = None
            if not evt_sock:
                self._stop_evt.wait(0.3)
                continue
            try:
                r, _, _ = select.select([evt_sock], [], [], 0.3)
                if not r:
                    continue
                data = evt_sock.recv(65536)
                if not data:
                    try:
                        evt_sock.close()
                    except OSError:
                        pass
                    evt_sock = None
                    continue
                for line in data.decode("utf-8", "replace").split("\n"):
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    if evt.get("event") == "end-file":
                        self._on_end_file(evt)
            except OSError:
                try:
                    evt_sock.close()
                except OSError:
                    pass
                evt_sock = None
        if evt_sock:
            try:
                evt_sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    def shutdown(self):
        self._stop_evt.set()
        self.issue({"command": ["quit", "no"]})
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
            except OSError:
                pass
        self.proc = None

    def playing(self):
        return self.get("idle-active") is False and not self.position() == 0.0

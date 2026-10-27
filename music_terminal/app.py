# -*- coding: utf-8 -*-
"""Aplicación TUI de Music Terminal.

Navegación estilo asistente (calca la interfaz de AniLinux Terminal):
rail lateral de secciones, listas con flechas, diálogos centrados y barra
de estado.  Añade la barra de reproductor y las pantallas de música
(búsqueda con pestañas, explorar, biblioteca, cola, letras, etc.).
"""
import json
import os
import queue as _queue
import random
import re
import shutil
import signal
import subprocess
import sys
import _thread
import threading
import time

from . import config
from . import innertube as api_mod
from . import tui
from . import browser_cookies
from .cover import CoverStore
from .downloader import Downloader, LocalStore, human_bytes
from .innertube import ApiError, NetworkError
from .player import MpvPlayer
from .queue import Queue
from .extractor import Extractor, ExtractionError, copy_to_clipboard
from .streamproxy import StreamProxy
from .tui import C, draw_box, fit, pad, visible_len, wrap

HINT_BACK = "↑↓ mover   ·   Enter/→ seleccionar   ·   Esc/← volver   ·   q salir"
HINT_LIST = ("↑↓/PgUp/PgDn navegar · Enter reproducir · a encolar · s aleatoria "
             "· d copia local (D lista entera) · f/K favorito (local/cuenta) "
             "· r radio · l letra · c copiar · Esc volver")

REGIONS = ("ES", "US", "MX", "AR", "GB", "DE", "FR", "IT", "BR", "JP")
LANGS = ("es", "en", "pt", "fr", "de", "it", "ja", "ko")

TYPE_LABEL = {"song": "canción", "album": "álbum", "artist": "artista",
              "playlist": "playlist"}


def _visible_window(text, cursor, width):
    """Ventana visible de un campo de texto que puede desbordar el ancho."""
    text = text or ""
    if visible_len(text) <= width:
        return text, 0
    start = max(0, min(cursor - width, len(text) - width))
    return text[start:start + width], start


class App:
    SECTIONS = ("inicio", "explorar", "buscar", "biblioteca", "cola", "ajustes")
    SECTION_LABELS = (
        "Inicio", "Explorar", "Buscar", "Biblioteca", "Cola", "Ajustes",
    )
    TABS = (("songs", "Canciones"), ("videos", "Vídeos"),
            ("albums", "Álbumes"), ("artists", "Artistas"),
            ("community_playlists", "Listas"))

    def __init__(self, api, db, settings):
        self.api = api
        self.db = db
        self.settings = settings
        self.scr = tui.Screen()
        self.flash_msg = None
        self.flash_ts = 0.0
        self._restore = None
        self._stopping = False
        self._keyq = _queue.Queue()
        self._auto_next = False
        self._stream_broke = False  # mpv marcó error: pendiente de rescatar
        self._rescued_id = None     # pista ya rescatada (un intento)
        self._last_pos = 0.0        # último segundo conocido de la pista
        self.player = MpvPlayer(volume=int(settings.get("volume", 80)))
        self.extractor = Extractor(api, settings, config.DATA_DIR)
        self.proxy = StreamProxy()
        self.dl = Downloader(self.extractor,
                             LocalStore(os.path.join(config.DATA_DIR,
                                                     "offline")),
                             notify=self._dl_flash)
        self.q = Queue()
        self.q.mode = settings.get("repeat", "none")
        self.q.shuffle = bool(settings.get("shuffle", False))
        self.player.on_ended = lambda: setattr(self, "_auto_next", True)
        self.player.on_error = lambda: setattr(self, "_stream_broke", True)
        # panel de reproductor (columna derecha) y extras de sesión
        self.panel = {"page": "cover", "qsel": 0, "qtop": 0,
                      "ltop": 0}
        self.covers = CoverStore(config.COVER_DIR)
        self._lyr = {}            # videoId → texto o None (sin letra)
        self._lyr_wait = set()    # videoId en descarga
        self._typing = False      # mientras se escribe, sin atajos globales
        self._likes = {}          # videoId → bool (Me gusta de la cuenta)
        self._sug_cache = {}      # query → sugerencias (sobrevive al diálogo)
        self._bib_acct = None     # caché de la biblioteca de la cuenta
        self._prov = ""           # etiqueta de la fuente del stream actual
        self._kg_ok = tui.kitty_ok()   # protocolo gráfico de kitty
        self._kg_id = 0           # id de imagen kitty colocada (0 = ninguna)
        self._kg_state = None     # (videoId, cols, rows) ya colocada
        self._kg_next = 0         # contador de ids de imagen
        self._kg_px = None        # (w, h) físico de UNA celda (arranque)
        self._restore_queue()

    # ================================================================= #
    # cola persistente ("continuar última sesión")
    # ================================================================= #
    def _restore_queue(self):
        saved = self.db.get_meta("queue_state")
        if isinstance(saved, dict) and saved.get("items"):
            try:
                self.q.restore(saved)
            except Exception:
                pass

    def _persist_state(self):
        plain = []
        for it in self.q.items[:100]:
            d = {k: it.get(k) for k in
                 ("type", "id", "title", "album", "album_id", "duration",
                  "thumb", "sub")}
            d["artists"] = list(it.get("artists") or [])
            plain.append(d)
        self.db.set_meta("queue_state", {
            "items": plain,
            "pos": max(0, self.q.index),
            "mode": self.q.mode,
            "shuffle": self.q.shuffle,
            "context": self.q.context,
        })
        s = dict(self.settings)
        s["volume"] = int(self.player.volume)
        s["repeat"] = self.q.mode
        s["shuffle"] = self.q.shuffle
        config.save_settings(s)

    def _q_fingerprint(self):
        """Huella del estado de la cola para el guardado periódico."""
        cur = self.q.current()
        return (len(self.q.items), self.q.index, self.q.mode,
                bool(self.q.shuffle), self.q.context,
                cur.get("id") if cur else None)

    def _persist_watch(self, interval=2.0):
        """Guarda la cola aunque el proceso muera sin cierre limpio
        (ventana de kitty cerrada, logout, kill -TERM, apagón…)."""
        last = None
        while not self._stopping:
            try:
                time.sleep(interval)
            except Exception:
                break
            if self._stopping:
                break
            try:
                fp = self._q_fingerprint()
            except Exception:
                continue
            try:  # por dónde va la pista (para rescatarla si el CDN muere)
                pos = self.player.position()
                if pos > 0.5:
                    self._last_pos = pos
            except Exception:
                pass
            if fp == last:
                continue
            last = fp
            try:
                self._persist_state()
            except Exception:
                pass

    def _install_sig_save(self):
        """SIGTERM/SIGHUP: guardar la cola antes de morir."""
        def on_sig(_s, _f):
            self._sig_saved = True
            try:
                self._persist_state()
            except Exception:
                pass
            _thread.interrupt_main()
        self._sig_handler = on_sig
        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, on_sig)
            except (OSError, ValueError):
                pass

    # ================================================================= #
    # ciclo principal
    # ================================================================= #
    def run(self):
        if not tui.can_raw():
            print("Necesitas una terminal interactiva (TTY) para ejecutar la TUI.")
            sys.exit(1)
        self._restore = tui.make_raw()
        self.scr.enter()
        if self._kg_ok:
            # confirmación positiva antes de fiarnos del entorno
            self._kg_ok = tui.kg_probe()
            if self._kg_ok:
                self._kg_px = tui.kg_cell_px()   # resolución física exacta
        reader = threading.Thread(target=self._key_reader, daemon=True)
        reader.start()
        if threading.current_thread() is threading.main_thread():
            self._install_sig_save()     # muere → guarda igualmente
        watcher = threading.Thread(target=self._persist_watch, daemon=True)
        watcher.start()                  # guarda cada ~2 s si cambió
        try:
            self.rail_loop()
        except KeyboardInterrupt:
            pass     # SIGTERM/SIGHUP ya guardaron; salida sin traceback
        finally:
            self._stopping = True
            try:
                self._persist_state()
            except Exception:
                pass
            self.player.shutdown()
            self.proxy.stop()
            if getattr(self, "_kg_ok", False):
                try:
                    tui.kitty_delete(self.scr)          # elimina TODOS los gráficos
                except Exception:
                    pass
            self.scr.leave()
            self._restore()

    def _key_reader(self):
        while not self._stopping:
            try:
                key = tui.read_key(0)
            except (KeyboardInterrupt, EOFError):
                key = "ctrl+c"
            self._keyq.put(key)

    PANEL_HOTKEYS = ("space", "n", "v", "o", "y")

    def get_key(self, timeout=0.35):
        # volca el frame recién dibujado antes de bloquearnos (stdout a
        # terminal va con buffering de línea y los marcos no llevan '\n')
        self.scr.flush()
        playing = self.q.current() is not None
        if playing and timeout > 1.0:
            timeout = 1.0      # redibuja ~1 Hz para el panel/barra de tiempo
        try:
            key = self._keyq.get(timeout=timeout)
        except _queue.Empty:
            return None
        if playing and key and not self._typing:
            if key == "space":
                if self.player.proc:
                    self.player.toggle()
                else:
                    cur = self.q.current()
                    if cur:
                        self.play_stream(cur)   # reanudar tras reinicio
                return None
            if key == "n":
                self.auto_next()
                return None
            if key == "v":
                self.play_prev()
                return None
            if key == "o":
                self.panel_cycle_page()
                return None
            if key == "y":
                self.panel_open_lyrics()
                return None
            if key in ("+", "-"):
                self.panel_scroll(key == "+")
                return None
            if key == "tab" and self.panel["page"] == "cola":
                self.panel_jump()
                return None
        return key

    def flash(self, msg, tone="info"):
        if msg is None:
            self.flash_msg = None
            return
        self.flash_msg = (tone, msg)
        self.flash_ts = time.time()

    def _consume_auto(self):
        if self._stream_broke:
            self._stream_broke = False
            self._rescue_stream()
            return
        if self._auto_next:
            self._auto_next = False
            self.auto_next()

    def rail_loop(self):
        sel = 0
        while True:
            self.draw_rail(sel, 0, "")
            key = self.get_key()
            if key is None:
                self._consume_auto()
                continue
            self._consume_auto()
            if key in ("up", "k"):
                sel = (sel - 1) % len(self.SECTIONS)
            elif key in ("down", "j"):
                sel = (sel + 1) % len(self.SECTIONS)
            elif key in ("enter", "right"):
                self.flash_msg = None
                self.run_section(self.SECTIONS[sel])
            elif key == "q":
                if self.confirm("¿Salir de Music Terminal?", default=True):
                    return
            elif key == "ctrl+c":
                return
            elif key == "space" and self.player.proc:
                self.player.toggle()

    # ================================================================= #
    # encuadre base
    # ================================================================= #
    def _terminal(self):
        return self.scr.size

    def _stats_line(self):
        return (f"{self.db.count_songs()} canciones · "
                f"{self.db.count_favorites()} favoritos · "
                f"{self.db.count_playlists()} playlists")

    def _panel_width(self, W=None):
        """Ancho del panel del reproductor (0 si no hay pista o es muy justa)."""
        cur_w = self._terminal()[0] if W is None else W
        if not self.q.current():
            return 0
        return min(64, max(32, cur_w // 3)) if cur_w >= 96 else 0

    def draw_base(self, active, title, hint, rail_sel=-1):
        """Dibuja marco completo.  Si hay pista activa reserva la derecha
        para el panel del reproductor (1/4 del ancho, toda la altura)."""
        W, H = self._terminal()
        pw = self._panel_width(W)
        zw = W - pw                       # ancho de la zona izquierda
        self.scr.clear()
        if self._kg_ok:
            # ESC 2J borra las imágenes gráficas colocadas: hay que
            # volver a mostrarlas en el siguiente repintado del panel.
            self._kg_state = None
        self.scr.move(0, 0)
        self.scr.write(C.TITLE_BG + C.BOLD +
                       pad(" MUSIC TERMINAL", zw) + C.RESET)
        stats = self._stats_line()
        left = f" Biblioteca local · {stats}"[:max(2, zw - 2)]
        self.scr.move(1, 0)
        self.scr.write(C.FG_DIM + pad(left, zw) + C.RESET)

        rail_w = 18
        body_top = 2
        body_h = H - 4 - body_top          # deja barra mpv y pie
        for yy in range(body_top, body_top + body_h + 1):
            if yy > H - 3:
                break
            self.scr.move(yy, 0)
            self.scr.write(C.RAIL_BG + " " * rail_w + C.RESET)
        self.scr.move(body_top, 0)
        self.scr.write(C.RAIL_BG + C.BOLD +
                       pad(fit(" SECCIONES", rail_w - 1), rail_w - 1) + C.RESET)
        for i, name in enumerate(self.SECTIONS):
            row_label = self.SECTION_LABELS[i]
            yy = body_top + 2 + i
            if yy >= body_top + body_h:
                break
            self.scr.move(yy, 0)
            if name == active:
                item = C.BOLD + C.OK + "▌ " + row_label
            elif i == rail_sel:
                item = C.BOLD + C.ACCENT + "▌ " + row_label
            else:
                item = C.FG + "  " + row_label
            self.scr.write(C.RAIL_BG + pad(item, rail_w - 1) + C.RESET)

        y = body_top + 1
        x = rail_w + 1
        h = body_h - 2
        w = zw - rail_w - 2
        draw_box(self.scr, y - 1, x - 1, h + 2, w + 2,
                 title if title else "", C.BORDER)
        self._draw_player_bar(H - 2, zw, rail_w)
        display = fit(hint, max(0, zw - 2))
        self.scr.move(H - 1, 0)
        self.scr.write(C.STATUS_BG + pad(" " + display, zw) + C.RESET)
        if pw:
            self.draw_panel(W, H)
        self._paint_toast(W)
        return y, x, h, w

    def _paint_toast(self, W):
        """Aviso flotante: rectangulo con borde en la esquina superior
        derecha.  Vive en las filas 0-2 (el contenido de las pantallas
        empieza en la fila 3, y aqui ya se pintaron titulo, estadisticas
        y panel), asi ninguna pantalla lo borra hasta el proximo
        repintado, y la barra de abajo (el hint) queda intacta."""
        if not (self.flash_msg and time.time() - self.flash_ts < 8):
            return
        tone, texto = self.flash_msg
        texto = " · ".join(p.strip() for p in str(texto or "").split("\n")
                           if p.strip()).strip()
        if not texto:
            return
        col = {"info": C.OK, "ok": C.OK, "warn": C.WARN,
               "err": C.ERR}.get(tone, C.FG)
        etiq = {"err": " error ", "ok": " listo ",
                "warn": " aviso "}.get(tone, " aviso ")
        maxw = min(W - 8, 64)
        if maxw < 8:
            return                       # terminal demasiado estrecha
        texto = C.strip(texto)           # sin codigos ANSI heredados
        if visible_len(texto) > maxw:
            texto = fit(texto, maxw - 1) + "…"
        bw = max(14, min(W - 4, visible_len(texto) + 4))
        if visible_len(texto) + 4 > bw:  # terminal estrecha: recorta
            texto = fit(texto, bw - 5) + "…"
        left = max(0, W - 1 - bw)
        draw_box(self.scr, 0, left, 3, bw, etiq, col, None, col)
        self.scr.move(1, left + 1)
        self.scr.write(C.STATUS_BG + col + C.BOLD +
                       pad(" " + texto + " ", bw - 2) + C.RESET)

    def _draw_player_bar(self, row, W, rail_w):
        cur = self.q.current()
        bar_col = C.SEL_BG
        if not cur:
            hint = ("sin reproducción — busca y pulsa Enter sobre una canción")
            line = C.ACCENT + " ♪ " + hint
            self.scr.move(row, 0)
            self.scr.write(bar_col + pad(line, W) + C.RESET)
            return
        paused = self.player.get("pause") if self.player.proc else False
        icon = "⏸" if paused else "▶"
        pos = int(self.player.position() or 0)
        if pos > 1:
            self._last_pos = pos
        dur = int(self.player.duration() or cur.get("duration") or 0)
        if dur and not cur.get("duration"):   # mpv la conoció: se rellena
            cur["duration"] = dur
        ps, ds = config.fmt_secs(pos), (config.fmt_secs(dur) if dur else "?:??")
        title = cur.get("title", "?")
        artists = cur.get("artists") or []
        art = artists[0][0] if artists else ""
        mode = ""
        if self.q.shuffle:
            mode += " ⤨mz"
        if self.q.mode == "all":
            mode += " ⟲"
        elif self.q.mode == "one":
            mode += " ⟲1"
        extra = f"  {self.q.index + 1}/{len(self.q.items)}" if self.q.items else ""
        prov = f" · {self._prov}" if self._prov else ""
        right = f"{ps}/{ds}{prov}{mode}{extra} "
        reserved = visible_len(right) + 4
        tw = max(8, W - reserved - rail_w - 2)
        main = fit(f"{title} — {art}", tw)
        line = (" " + icon + " " + main + " " * max(1, tw - visible_len(main))
                + C.FG_DIM + right)
        self.scr.move(row, 0)
        self.scr.write(bar_col + pad(line, W) + C.RESET)

    # ================================================================= #
    # panel del reproductor (columna derecha · 1/4 del ancho · todo el alto)
    # ================================================================= #
    PANEL_BTNS = (            # (tecla, etiqueta)
        ("espacio", "Pausa/Continuar"),
        ("n", "Siguiente"),
        ("v", "Anterior"),
        ("o", "Ver cola"),
        ("y", "Ver letra"),
    )
    PANEL_PAGES = ("cover", "cola", "letra")

    def panel_cycle_page(self):
        p = self.panel["page"] if self.panel["page"] in self.PANEL_PAGES \
            else "cover"
        self.panel["page"] = self.PANEL_PAGES[
            (self.PANEL_PAGES.index(p) + 1) % len(self.PANEL_PAGES)]
        if self.panel["page"] == "letra":
            cur = self.q.current()
            if cur:
                self._ensure_lyrics(cur.get("id"))
        elif self.panel["page"] == "cola":
            self.panel["qsel"] = max(0, self.q.index)
            self.panel["qtop"] = 0

    def panel_open_lyrics(self):
        self.panel["page"] = "letra"
        cur = self.q.current()
        if cur:
            self._ensure_lyrics(cur.get("id"))

    def _ensure_lyrics(self, vid):
        """Lanza la descarga de la letra en un hilo (caché por videoId)."""
        if not vid or vid in self._lyr or vid in self._lyr_wait:
            return
        self._lyr_wait.add(vid)

        def work():
            try:
                txt = None
                res = self.api.next(vid)
                ep = res.get("lyrics_endpoint")
                if ep and ep.get("browseId"):
                    txt = self.api.lyrics(ep["browseId"], ep.get("params"))
                if not isinstance(txt, str):
                    txt = ""
            except (ApiError, NetworkError, Exception):
                txt = ""
            self._lyr[vid] = txt
            self._lyr_wait.discard(vid)

        threading.Thread(target=work, name=f"letra-{vid}", daemon=True).start()

    def panel_scroll(self, up):
        """Mueve la selección/página activa del panel (+/-)."""
        if self.panel["page"] == "cola":
            n = len(self.q.items)
            if n:
                self.panel["qsel"] = min(
                    n - 1, max(0, self.panel["qsel"] + (1 if up else -1)))
        elif self.panel["page"] == "letra":
            self.panel["ltop"] = max(0, self.panel["ltop"] + (3 if up else -3))

    def panel_jump(self):
        """Salta a la canción seleccionada de la página Cola (Tab)."""
        i = self.panel["qsel"]
        if 0 <= i < len(self.q.items) and i != self.q.index:
            self.q.index = i
            self.play_stream(self.q.items[i])

    def draw_panel(self, W, H):
        pw = self._panel_width(W)
        if not pw:
            if self._kg_ok and self._kg_id:
                tui.kitty_delete(self.scr, self._kg_id)
                self._kg_id = 0
                self._kg_state = None
            return
        x = W - pw
        cur = self.q.current()
        draw_box(self.scr, 0, x, H, pw, "♪  Reproductor", C.BORDER)
        ix, iw = x + 1, pw - 2
        top, bot = 2, H - 2
        page = self.panel.get("page", "cover")
        if page != "cover" and self._kg_ok and self._kg_id:
            # otra página del panel: la portada gráfica debe desaparecer
            tui.kitty_delete(self.scr, self._kg_id)
            self._kg_id = 0
            self._kg_state = None
        if page == "cola":
            self._panel_queue(ix, iw, top, bot)
        elif page == "letra":
            self._panel_lyrics(ix, iw, top, bot)
        elif cur:
            self._panel_cover(cur, ix, iw, top, bot)
        elif self._kg_ok and self._kg_id:
            # cola vacía en portada: la imagen gráfica sobraría
            tui.kitty_delete(self.scr, self._kg_id)
            self._kg_id = 0
            self._kg_state = None

    def _place_kg(self, cur, ix, iw, top, art_h):
        """Portada vía protocolo gráfico de kitty (imagen real)."""
        vid = cur.get("id", "")
        state = (vid, iw, art_h)
        if self._kg_state == state and self._kg_id:
            return                     # ya colocada: no tocar
        px = self._kg_px or (12, 24)   # píxeles por celda; generoso si el
                                       # arranque no los midió: kitty
                                       # reescala a la caja y va nítido
        png = self.covers.get_png(vid, iw, art_h, cur.get("title", ""),
                                  cur.get("thumb"),
                                  px_w=iw * px[0], px_h=art_h * px[1])
        if png is None:
            for r in range(art_h):     # aún descargando la portada
                self.scr.move(top + r, ix)
                self.scr.write(C.FG_DIM + ("░" * iw) + C.RESET)
            return
        if self._kg_id:
            tui.kitty_delete(self.scr, self._kg_id)
        img_id = self._kg_next + 1
        self._kg_next = img_id        # ids únicos por imagen (1…)
        # limpiar las celdas del área una vez (borra texto/glifos previos)
        for r in range(art_h):
            self.scr.move(top + r, ix)
            self.scr.write("\x1b[0m" + (" " * iw))
        if tui.kitty_show(self.scr, png, img_id, 1, iw, art_h,
                          at=(top, ix)):
            self._kg_id = img_id
            self._kg_state = state

    def _panel_cover(self, cur, ix, iw, top, bot):
        started = bool(self.player.proc)
        paused = self.player.get("pause") if started else False
        art_h = min(22, max(4, bot - top - 10))
        vid = cur.get("id", "")
        if self._kg_ok:
            self._place_kg(cur, ix, iw, top, art_h)
        else:
            art = self.covers.get(vid, iw, art_h, cur.get("title", ""),
                                  cur.get("thumb"))
            if art:
                for r in range(art_h):
                    self.scr.move(top + r, ix)
                    self.scr.write(art[r])
            else:
                for r in range(art_h):
                    self.scr.move(top + r, ix)
                    self.scr.write(C.FG_DIM + ("░" * iw) + C.RESET)
                self.scr.move(top + art_h // 2, ix)
                self.scr.write(pad(fit(C.FG_DIM + "obteniendo portada…", iw),
                                   iw) + C.RESET)
        yy = top + art_h
        title = cur.get("title", "?")
        for line in wrap(title, iw)[:2]:
            if yy >= bot - 6:
                break
            self.scr.move(yy, ix)
            self.scr.write(C.FG + C.BOLD + fit(line, iw) + C.RESET)
            yy += 1
        artists = cur.get("artists") or []
        grp = " · ".join(a[0] for a in artists[:2])
        if yy < bot - 6:
            self.scr.move(yy, ix)
            self.scr.write(C.FG_DIM + fit(grp or "—", iw) + C.RESET)
        # barra de progreso e índice
        pos = int(self.player.position() or 0)
        if pos > 1:
            self._last_pos = pos
        dur = int(self.player.duration() or cur.get("duration") or 0)
        if dur and not cur.get("duration"):
            cur["duration"] = dur
        bary = max(yy + 2, bot - 6)
        fill = int(iw * pos / dur) if dur and dur > 0 else 0
        bar = (C.OK + "━" * fill + C.FG_DIM + "─" * (iw - fill)) if fill else \
              (C.FG_DIM + "─" * iw)
        self.scr.move(bary, ix)
        self.scr.write(bar + C.RESET)
        trow = (config.fmt_secs(pos) if dur else "?:??")
        line = f"{trow} de {config.fmt_secs(dur) if dur else '?:??'}"
        self.scr.move(bary + 1, ix)
        self.scr.write(C.FG_DIM + pad(fit(line, iw), iw) + C.RESET)
        # botones (5 filas fijas al fondo)
        for i, (k, lab) in enumerate(self.PANEL_BTNS):
            r = bot - 4 + i
            if r <= bary + 1 or r > bot:
                continue
            if i == 0:
                lab = ("Iniciar" if not started
                       else "Continuar" if paused else "Pausa")
            self.scr.move(r, ix)
            self.scr.write(C.ACCENT + f"[{k:^7}] " + C.FG +
                           pad(fit(lab, iw - 10), iw - 10) + C.RESET)

    def _panel_queue(self, ix, iw, top, bot):
        cur = self.q.current()
        n = len(self.q.items)
        self.scr.move(top, ix)
        self.scr.write(C.ACCENT + C.BOLD +
                       pad(fit(f"Cola · {n} canciones", iw), iw) + C.RESET)
        self.scr.move(top + 1, ix)
        self.scr.write(C.FG_DIM +
                       pad(fit("[+/-] elegir · [Tab] saltar", iw), iw) + C.RESET)
        if not n:
            self.scr.move(top + 3, ix)
            self.scr.write(C.FG_DIM + fit("(la cola está vacía)", iw) + C.RESET)
            return
        if self.panel["qsel"] >= n:
            self.panel["qsel"] = n - 1
        ls_top = top + 3
        h = bot - ls_top + 1
        if h <= 0:
            return
        off = self._fix_offset(self.panel["qsel"], self.panel["qtop"], h, n)
        self.panel["qtop"] = off
        for r in range(h):
            i = off + r
            if i >= n:
                break
            s = self.q.items[i]
            artists = s.get("artists") or []
            txt = f"{s.get('title', '?')} — {artists[0][0] if artists else '?'}"
            pre = f"{i + 1:>3} {'▶' if i == self.q.index else ' '}"
            if i == self.q.index:
                self.scr.move(ls_top + r, ix)
                self.scr.write(C.OK + C.BOLD + pad(fit(pre + txt, iw - 1),
                                                   iw) + C.RESET)
            elif i == self.panel["qsel"]:
                self.scr.move(ls_top + r, ix)
                self.scr.write(C.SEL_BG + pad(fit(pre + txt, iw - 1),
                                              iw) + C.RESET)
            else:
                self.scr.move(ls_top + r, ix)
                self.scr.write(C.FG + pad(fit(pre + txt, iw - 1), iw) + C.RESET)

    def _panel_lyrics(self, ix, iw, top, bot):
        cur = self.q.current()
        vid = cur.get("id") if cur else None
        self.scr.move(top, ix)
        t = (cur.get("title", "?") if cur else "sin canción")
        self.scr.write(C.ACCENT + C.BOLD +
                       pad(fit(f"Letra · {t}", iw), iw) + C.RESET)
        self.scr.move(top + 1, ix)
        self.scr.write(C.FG_DIM +
                       pad(fit("[+/-] scroll · [o] atrás", iw), iw) + C.RESET)
        if not cur:
            return
        if vid in self._lyr_wait and vid not in self._lyr:
            txt = None
        else:
            txt = self._lyr.get(vid)
        if txt is None:
            self.scr.move(top + 3, ix)
            self.scr.write(C.FG_DIM + pad(fit("buscando letra…", iw),
                                          iw) + C.RESET)
            return
        body = txt or ""
        lines = []
        for ln in body.split("\n"):
            lines.extend(wrap(ln, iw) or [""])
        if not body.strip():
            lines = ["(no hay letra disponible para esta canción)"]
        ls_top = top + 3
        h = bot - ls_top + 1
        if h <= 0:
            return
        self.panel["ltop"] = min(max(0, self.panel["ltop"]),
                                 max(0, len(lines) - h))
        for r in range(h):
            i = self.panel["ltop"] + r
            if i >= len(lines):
                break
            self.scr.move(ls_top + r, ix)
            self.scr.write(C.FG + pad(fit(lines[i], iw), iw) + C.RESET)

    def draw_rail(self, sel, off_unused=0, extra=""):
        hint = (f"Enter/→ abre {self.SECTION_LABELS[sel]}   ·   "
                "↑↓ mover   ·   q salir   ·   espacio pausa")
        y, x, h, w = self.draw_base(None, "Music Terminal", hint, rail_sel=sel)
        cur = self.q.current()
        playing = tit = cur.get("title", "?") if cur else "nada todavía"
        lines = [
            ("Cliente de YouTube Music para Linux", C.ACCENT + C.BOLD),
            ("", 0),
            (f"Sonando: {playing}", C.FG),
            (f"Biblioteca: {self._stats_line()}", C.FG),
            (f"Región {self.settings.get('region', 'ES')} · "
             f"calidad {self.settings.get('audio_quality', 'high')}", C.FG_DIM),
            ("", 0),
            ("Elige la sección con ↑↓ y ábrela con Enter o →", C.ACCENT2),
            ("Los controles del reproductor están en la Cola.", C.FG_DIM),
        ]
        yy = y
        for text, col in lines:
            if yy >= y + h - 1:
                break
            self.scr.move(yy, x + 2)
            self.scr.write((col or "") + fit(str(text), w - 4) + C.RESET)
            yy += 1

    @staticmethod
    def _fix_offset(sel, offset, h, n):
        if sel < offset:
            offset = sel
        elif sel >= offset + h:
            offset = sel - h + 1
        return max(0, min(offset, max(0, n - h)))

    def menu(self, y, x, h, w, items, sel, offset):
        """Menú vertical desplazable.  items: [(texto, color|None)]"""
        n = len(items)
        offset = self._fix_offset(sel, offset, h, n)
        show = min(h, n)
        for row_i in range(show):
            idx = offset + row_i
            if idx >= n:
                break
            item = items[idx]
            if isinstance(item, str):       # list_screen: filas sin color
                text, col = item, None
            else:
                text, col = item
            self.scr.move(y + row_i, x)
            prefix = "› " if idx == sel else "  "
            if idx == sel:
                self.scr.write(C.SEL_BG + C.BOLD +
                               pad(fit(prefix + text, w), w) + C.RESET)
            else:
                body = (col if col is not None else C.FG) + prefix + text
                self.scr.write(pad(fit(body, w), w) + C.RESET)
        if n > h:
            self.scr.move(y, x + w - 2)
            if offset > 0:
                self.scr.write(C.FG_DIM + "▲" + C.RESET)
            else:
                self.scr.write(" " + C.RESET)
            self.scr.move(y + show - 1, x + w - 2)
            if offset + show < n:
                self.scr.write(C.FG_DIM + "▼" + C.RESET)
            else:
                self.scr.write(" " + C.RESET)

    def centered(self, W, H, height, width):
        pw = self._panel_width(W)
        zw = W - pw
        y0 = max(3, min(H - height - 3, (H - height) // 2 + 1))
        x0 = 19 + max(1, ((zw - 20) - width) // 2)
        if x0 + width > zw - 1:
            x0 = max(20, zw - width - 2)
        return y0, x0, height, width

    # ================================================================= #
    # diálogos
    # ================================================================= #
    def alert(self, text, title="Aviso"):
        W, H = self._terminal()
        tw = max(40, min((W - self._panel_width(W)) - 24, 68))
        lines = wrap(str(text), tw - 4) or [""]
        h = min(len(lines) + 3, H - 5)
        w = tw
        y0, x0, _, _ = self.centered(W, H, height=h, width=w)
        while True:
            self.draw_base(None, title, HINT_BACK)
            draw_box(self.scr, y0 - 1, x0 - 1, h + 1, w + 2, "", C.WARN)
            for i in range(min(len(lines), h - 1)):
                self.scr.move(y0 + i, x0)
                self.scr.write(C.WARN + fit(lines[i], w) + C.RESET)
            self.scr.move(y0 + min(len(lines), h - 1), x0)
            self.scr.write(C.FG_DIM + fit("Enter / Esc para continuar", w)
                           + C.RESET)
            key = self.get_key(120.0)
            if key in ("enter", "esc", "q", "left"):
                return

    def confirm(self, text, title="Confirmar", default=True):
        W, H = self._terminal()
        h, w = 7, max(50, min((W - self._panel_width(W)) - 24, 66))
        y0, x0, _, _ = self.centered(W, H, height=h, width=w)
        lines = wrap(str(text), w - 4)
        sel = 0 if default else 1
        items = ["Sí, continuar", "No, cancelar"]
        while True:
            self.draw_base(None, title, "↑↓ mover · Enter confirmar · Esc cancela")
            draw_box(self.scr, y0 - 1, x0 - 1, h + 2, w + 2, "", C.WARN)
            for i, ln in enumerate(lines[:3]):
                self.scr.move(y0 + i, x0 + 1)
                self.scr.write(C.FG + fit(ln, w - 2) + C.RESET)
            for i, it in enumerate(items):
                self.scr.move(y0 + 4 + i, x0 + 2)
                prefix = "› " if i == sel else "  "
                row = prefix + it
                self.scr.write((C.SEL_BG + C.BOLD if i == sel else C.FG) +
                               pad(fit(row, w - 4), w - 4) + C.RESET)
            key = self.get_key(120.0)
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key == "enter":
                return sel == 0
            elif key in ("esc", "left", "q"):
                return False

    def input_box(self, title, label, initial="", suggestion_fn=None):
        """Entrada de texto con sugerencias en vivo (como el buscador)."""
        self._typing = True
        try:
            return self._input_box(title, label, initial, suggestion_fn)
        finally:
            self._typing = False

    def _input_box(self, title, label, initial="", suggestion_fn=None):
        W, H = self._terminal()
        w = max(40, min((W - self._panel_width(W)) - 28, 62))
        sug = []
        sug_sel = -1
        y0, x0, _, _ = self.centered(W, H, height=6, width=w)
        value = initial
        cursor = len(value)
        sug_cache = self._sug_cache   # query → sugerencias (persiste)
        pending_q = None      # query being fetched in the background
        shown_q = None        # query whose suggestions are painted

        def _fetch(q):
            try:
                res = suggestion_fn(q)
            except Exception:
                res = []
            sug_cache[q] = res or []

        while True:
            base_h = 6
            extra_h = min(len(sug), 4) if sug else 0
            self.draw_base(None, title, "escribe · ↑↓ sugerencia · Enter aceptar · "
                                         "Esc cancela")
            draw_box(self.scr, y0 - 1, x0 - 1, base_h + extra_h + 1, w + 2, "",
                     C.ACCENT2)
            self.scr.move(y0, x0 + 1)
            self.scr.write(C.FG_DIM + fit(label, w - 1) + C.RESET)
            line, start = _visible_window(value, cursor, w - 1)
            self.scr.move(y0 + 2, x0)
            self.scr.write(C.SEL_BG + pad(" " + line, w) + C.RESET)
            for i, srow in enumerate(sug[:4]):   # sugerencias visibles
                self.scr.move(y0 + 3 + i, x0)
                self.scr.write(C.FG_DIM + ("→ " if i == sug_sel else "  ")
                               + fit(srow, w - 2) + C.RESET)
            self.scr.move(y0 + 2, x0 + 1 + visible_len(line[:cursor - start]))
            self.scr.write("\x1b[?25h")
            # sugerencias ASÍNCRONAS: el eco del teclado nunca espera la red
            q = value.strip()
            if suggestion_fn and len(q) >= 2:
                hit = sug_cache.get(q)
                if hit is not None and shown_q != q:
                    sug = hit
                    sug_sel = -1            # nueva lista: selección a cero
                    shown_q = q
                    pending_q = None
                elif pending_q != q:
                    pending_q = q
                    threading.Thread(target=_fetch, args=(q,), name="sug",
                                     daemon=True).start()
            else:
                sug, sug_sel = [], -1
                if suggestion_fn:
                    pending_q, shown_q = None, None
            key = self.get_key(0.25 if pending_q else 60.0)
            self.scr.write("\x1b[?25l")
            if key == "esc":
                return None
            if key == "enter":
                if 0 <= sug_sel < len(sug):
                    return sug[sug_sel]
                return value.strip()
            elif key == "down" and sug:
                sug_sel = (sug_sel + 1) % min(len(sug), 4)
            elif key == "up" and sug:
                sug_sel = (sug_sel - 1) % min(len(sug), 4)
            elif key == "backspace":
                if cursor > 0:
                    value = value[:cursor - 1] + value[cursor:]
                    cursor -= 1
            elif key == "delete":
                value = value[:cursor] + value[cursor + 1:]
            elif key == "ctrl+u":
                value, cursor = "", 0
            elif key == "home":
                cursor = 0
            elif key == "end":
                cursor = len(value)
            elif key == "left":
                cursor = max(0, cursor - 1)
            elif key == "right":
                cursor = min(len(value), cursor + 1)
            elif isinstance(key, str) and key and len(key) == 1 and key >= " ":
                value = value[:cursor] + key + value[cursor:]
                cursor += 1
                sug_sel = -1
            # los resultados caen en el siguiente pase de este mismo bucle;
            # si el usuario cambió de consulta, el hilo anterior queda en
            # caché para cuando vuelva (sin bloquear jamás el eco)

    # ================================================================= #
    # enrutamiento
    # ================================================================= #
    def run_section(self, name):
        handlers = {
            "inicio": self.scr_inicio,
            "explorar": self.scr_explorar,
            "buscar": self.scr_buscar,
            "biblioteca": self.scr_biblioteca,
            "cola": self.scr_cola,
            "ajustes": self.scr_ajustes,
        }
        try:
            handlers[name]()
        except NetworkError as e:
            self.alert(f"Sin conexión: {e}", title="Red")
        except ApiError as e:
            self.alert(f"Fallo del API: {e}", title="API")
        except ExtractionError as e:
            self.alert(f"Extracción: {e}", title="Stream")
        except Exception as e:  # noqa: BLE001
            self.alert(f"Error inesperado ({type(e).__name__}): {e}",
                       title="Error")

    # ================================================================= #
    # reproducción
    # ================================================================= #
    def _mpv_started(self, wait=3.0):
        """Comprueba que mpv arrancó la pista (no volvió a idle vacío)."""
        fin = time.time() + wait
        while time.time() < fin:
            if self.player.get("idle-active") is True:
                return False
            try:
                if float(self.player.get("time-pos") or 0) > 0.05:
                    return True
            except (TypeError, ValueError):
                pass
            time.sleep(0.2)
        return self.player.get("idle-active") is False

    @staticmethod
    def _is_gvideo(url):
        return "googlevideo.com/" in url

    def play_stream(self, song, resume=None):
        """Pide el stream de la canción (ya en cola) y le da a mpv.

        Orden de intentos: URL directa (VISIONOS, rango abierto) → misma
        URL por el proxy local (si la fuente fuera de rango acotado) →
        yt-dlp (según modo del extractor).  Con resume=segundos retoma
        la pista ahí (rescate tras un error del CDN).  Devuelve True si
        la pista quedó sonando."""
        vid = song.get("id")
        if not vid:
            self.alert("Ese elemento no tiene id de vídeo", title="Aviso")
            return False
        if resume is None:
            self._last_pos = 0.0
        self.flash("extrayendo stream…")
        self.draw_rail(0)
        pdata = None
        local = self.dl.has(vid) if self.dl else None
        if local:
            # hay copia en local → ni el extractor ni la red se tocan
            self.flash("cargando la copia local…")
            url, desc, clen, mime = local, "local (copia)", None, None
            intents = [(url, desc, clen, mime)]
        else:
            try:
                url, desc, clen, mime, pdata = self.extractor.get_stream(vid)
            except ExtractionError as e:
                self.flash(None)
                self.alert(f"No se pudo extraer el audio:\n{e}\n\n"
                           f"(cópiela en local con «D» para oírla sin red)",
                           title="Stream")
                return False
            intents = [(url, desc, clen, mime)]
        if self._is_gvideo(url):
            try:  # variante a través del proxy de rangos acotados
                intents.append((self.proxy.set_stream(url, clen=clen,
                                                      mime=mime),
                                desc + " (proxy)", clen, mime))
            except Exception:
                pass
        ok_msg = None
        for u, d, _cl, _m in intents:
            _tg = re.search(r"\(([^)]+)\)", d)
            self.flash(f"iniciando ({_tg.group(1) if _tg else 'stream'})…")
            self.draw_rail(0)
            if not self.player.play(u, self._media_title(song)):
                continue
            if self._mpv_started():
                ok_msg = d
                if resume:
                    self.player.seek(resume, absolute=True)
                break
            self.player.stop()
        # la URL nativa muere en mpv (CDN 403) → reintento perezoso con
        # yt-dlp y se invalida la URL mala de las cachés
        if ok_msg is None and pdata is not None and \
                self.settings.get("extractor", "auto") in ("auto", "ytdlp")\
                and self.extractor.helper_ready():
            try:
                self.extractor.invalidate(vid)
                yurl, ydesc, yclen, ymime, _yp = self.extractor._ytdlp(vid)
                intents = [(yurl, ydesc, yclen, ymime)]
                if self._is_gvideo(yurl):
                    try:
                        intents.append((self.proxy.set_stream(
                            yurl, clen=yclen, mime=ymime),
                            ydesc + " (proxy)", yclen, ymime))
                    except Exception:
                        pass
                for u, d, _cl, _m in intents:
                    _tg2 = re.search(r"\(([^)]+)\)", d)
                    self.flash(f"reintentando ({_tg2.group(1) if _tg2 else 'stream'})…")
                    self.draw_rail(0)
                    if not self.player.play(u, self._media_title(song)):
                        continue
                    if self._mpv_started():
                        ok_msg = d
                        if resume:
                            self.player.seek(resume, absolute=True)
                        self.extractor._cache[vid] = (
                            time.time(),
                            (yurl, ydesc, yclen, ymime, None))
                        self.extractor._disk_put(vid, yurl, ydesc, yclen,
                                                 ymime)
                        break
                    self.player.stop()
            except Exception:
                pass
        if ok_msg is None:
            self.flash(None)
            msg = self.player.last_error or "mpv no pudo abrir el audio"
            self.alert(f"Reproducción fallida:\n{msg}", title="Reproductor")
            return False
        # prefetch de la siguiente cola (hace que «siguiente» no espere a
        # que se resuelva el stream)
        try:
            i = self.q.index
            for j in (i + 1, i + 2):
                if 0 <= j < len(self.q.items) and self.q.items[j].get("id"):
                    self.extractor.prefetch(self.q.items[j]["id"])
        except Exception:
            pass
        cur = self.q.current() or song
        artists = song.get("artists") or []
        if artists:
            cur["artists"] = artists
        if song.get("album"):
            cur["album"] = song.get("album")
        self.db.upsert_songs([song] if song.get("title") else [cur])
        self.db.add_history(vid,
                            max_keep=int(self.settings.get("max_history", 500)))
        self.panel["page"] = "cover"
        if "yt-dlp" in ok_msg:
            tag = "yt-dlp"
        else:
            _m = re.search(r"\(([^)]+)\)", ok_msg)
            tag = _m.group(1) if _m else "stream"
        if "(proxy)" in ok_msg:
            tag += " +proxy"
        self._prov = tag
        if pdata and self.api.signed_in:
            threading.Thread(target=self.api.ping_played,
                             args=(pdata, vid), daemon=True).start()
        self.flash(f"▶ {self._media_title(song)}", "info")
        if self.settings.get("offline_auto") and self._ok_song(song):
            # la opción de copia automática: todo lo que suena, queda
            self.dl.submit(vid, song.get("title", ""))
        return True

    def _media_title(self, song):
        artists = song.get("artists") or []
        return (f"{song.get('title', '')} — {artists[0][0]}"
                if artists else song.get("title", ""))

    def play_song(self, song, queue_items=None, start=None, context=None,
                  fill_radio=False):
        """Coloca canción+lista de contexto en la cola y la reproduce."""
        queue_items = queue_items or ([song] if song else [])
        if start is None and song:
            for i, it in enumerate(queue_items):
                if it.get("id") == song.get("id"):
                    start = i
                    break
            else:
                start = 0
        self.q.set_items(queue_items, start=start or 0, context=context)
        if song:
            self.play_stream(song)
            if self._ok_song(song) and fill_radio:
                self.fill_radio(song)

    def fill_radio(self, song):
        try:
            res = self.api.next(song["id"],
                                playlist_id=self.api.radio_id(song["id"]))
        except (ApiError, NetworkError) as e:
            self.flash(f"sin radio: {e}", "warn")
            return
        extra = [s for s in res.get("items", [])
                 if s.get("id") != song.get("id")][:49]
        if extra:
            self.db.upsert_songs(extra)
        keep = [self.q.current()] if self.q.current() else []
        self.q.set_items(keep + extra, start=0, context=self.q.context)
        self.flash(f"radio activada (+{len(extra)} canciones)", "info")

    def _rescue_stream(self):
        """mpv declaró la pista rota a mitad (CDN corta, URL muerta).

        Un intento de reextracción y siga donde iba (se retoma en el
        último segundo conocido); si vuelve a romper, a la siguiente.
        Si ya rueda otra pista, el aviso era viejo y se descarta."""
        cur = self.q.current()
        if not cur or not cur.get("id"):
            return
        try:
            if self.player.duration() > 0:
                return
        except Exception:
            return
        if self._rescued_id == cur.get("id"):
            self._rescued_id = None
            self.auto_next()
            return
        self._rescued_id = cur.get("id")
        pos = self._last_pos if self._last_pos > 3.0 else 0.0
        self.player.stop()
        self.flash("reconectando la pista…", "warn")
        try:
            ok = self.play_stream(cur, resume=pos)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            self.flash(None)
            self.auto_next()

    def auto_next(self):
        nxt = self.q.next_index()
        if nxt is None:
            self.flash("fin de la cola", "info")
            return False
        self.q.index = nxt
        self._rescued_id = None
        self.play_stream(self.q.items[nxt])
        return True

    def play_prev(self):
        prv = self.q.prev_index()
        if prv is None:
            self.flash("no hay pista anterior", "warn")
            return False
        self.q.index = prv
        self._rescued_id = None
        self.play_stream(self.q.items[prv])
        return True

    def enqueue(self, song):
        if not song or not song.get("id"):
            return
        self.db.upsert_songs([song])
        self.q.items.append(song)
        self.q._rebuild_order()
        self.flash(f"encolada: {song.get('title', '?')} "
                   f"({len(self.q.items)})", "info")

    def _drain_list(self, used, loader):
        """Toda la lista: lo ya cargado + el resto vía loader (hasta 800).

        Las playlists con paginación (búsqueda/cuenta) llegan por trozos;
        la cola aleatoria se merece la lista entera."""
        all_it = list(used)
        if loader and len(all_it) < 800:
            try:
                while len(all_it) < 800:
                    more = loader(len(all_it))
                    if not more:
                        break
                    all_it.extend(more)
            except (ApiError, NetworkError) as e:
                self.flash(f"sin más paginas: {e}", "warn")
        return all_it

    def shuffle_play(self, songs, title=None, context=None):
        """Reemplaza la cola por TODA la lista barajada y reproduce."""
        songs = [s for s in songs if self._ok_song(s)]
        if not songs:
            self.flash("no hay canciones que mezclar", "warn")
            return
        random.shuffle(songs)
        self.db.upsert_songs(songs)
        self.q.set_items(songs, start=0,
                         context=(context or title or ""))
        self.play_stream(songs[0])
        self.flash(f"cola aleatoria: {len(songs)} canciones", "info")

    def _dl_flash(self, msg, tone="info"):
        """El hilo de descargas avanza el flash (hilente, por atributo)."""
        self.flash(msg, tone)

    def _dl_rows(self, rows, whole=False, flash=True):
        """Encola copias locales para las canciones de la lista.

        Devuelve cuántas entraron en la cola (flash=False lo silencia)."""
        n = 0
        for s in rows:
            if self._ok_song(s) and self.dl.submit(s["id"],
                                                   s.get("title", "")):
                n += 1
        if flash:
            if n:
                self.flash(f"⤓ {n} canciones hacia la copia local…", "info")
            elif rows and any(self._ok_song(s) for s in rows):
                self.flash("todo ya está guardado en local", "info")
            elif whole:
                self.flash("lista sin canciones que guardar", "warn")
            else:
                self.flash("nada que guardar (no es una canción)", "info")
        return n

    def _mirror_pl(self, name, songs):
        """Espeja una playlist remota en las playlists locales.

        La copia local lleva el mismo nombre y el mismo orden; si ya
        existía una playlist con ese nombre se actualiza su contenido.
        Devuelve el texto del aviso (««X» creada/actualizada») o «»."""
        canciones = [s for s in songs if self._ok_song(s)]
        if not canciones or not name:
            return ""
        self.db.upsert_songs(canciones)
        pid = None
        for p in (self.db.playlists() or []):
            if p["name"] == name:
                pid = p["pid"]
                break
        nueva = pid is None
        if nueva:
            pid = self.db.create_playlist(name)
        if not pid:
            self.flash("la playlist local no pudo guardarse", "warn")
            return ""
        self.db.mirror_playlist(pid, [s["id"] for s in canciones])
        return f"«{name}» {'creada' if nueva else 'actualizada'}"

    # ================================================================= #
    # helpers de listas
    # ================================================================= #
    def _song_row(self, s):
        artists = s.get("artists") or []
        art = artists[0][0] if artists else (s.get("sub") or "")
        bits = []
        if art:
            bits.append(art)
        alb = s.get("album")
        if alb and alb != art:
            bits.append(alb)
        dur = s.get("duration")
        txt = s.get("title", "?")
        if bits:
            txt += "  —  " + " · ".join(bits)
        if dur:
            txt += f"  [{config.fmt_secs(dur)}]"
        return txt

    def _ok_song(self, s):
        return bool(s) and s.get("type", "song") == "song" and s.get("id")

    def list_screen(self, title, items, hint=HINT_LIST, context="",
                    loader=None, on_enter=None, header_draw=None,
                    on_key=None, start_sel=0, yt_pl=None):
        """Lista de navegación con acciones (calca list_screen de anilinux).

        loader(offset) → más items al llegar al final; on_enter(item, idx)
        para personalizar Enter; devuelve None/True/"refresh" según flujo.
        """
        sel = max(0, min(start_sel, max(0, len(items) - 1)))
        offset = 0
        used = list(items)
        fav_cache = {it["id"]: self.db.is_favorite(it["id"]) for it in used
                     if it.get("id")}
        fetching = False
        while True:
            y, x, h, w = self.draw_base(None, title, hint)
            if header_draw:
                header_draw(y - 1, x + 2, w - 4)
            rows = []
            for i, it in enumerate(used):
                star = "★ " if fav_cache.get(it.get("id")) else ""
                mark = "⤓ " if it.get("id") and self.dl.has(it["id"]) else ""
                rows.append(mark + star + self._song_row(it))
            self.menu(y + 1, x + 2, h - 2, w - 4, rows, sel, offset)
            lbl = ""
            if 0 <= sel < len(used):
                lbl = TYPE_LABEL.get(used[sel].get("type", "song"), "")
            self.scr.move(y + h - 1, x + 2)
            self.scr.write(C.FG_DIM + fit(
                f"{sel + 1}/{len(used)}"
                + (f"   ({context})" if context else "")
                + (f"    «{lbl}»" if lbl else ""), w - 4) + C.RESET)
            key = self.get_key(60.0)
            self._consume_auto()
            if key is None:
                continue
            if on_key and key:
                res = on_key(key)
                if res:
                    return res
            if key in ("up", "k"):
                sel = (sel - 1) % len(used) if used else 0
            elif key in ("down", "j"):
                sel = (sel + 1) % len(used) if used else 0
            elif key == "pgup":
                sel = max(0, sel - max(1, h - 4))
            elif key == "pgdn":
                sel = min(len(used) - 1, sel + max(1, h - 4)) if used else 0
            elif key == "home":
                sel = 0
            elif key == "end":
                sel = max(0, len(used) - 1)
            elif key in ("enter", "right") and used:
                if on_enter:
                    on_enter(used[sel], sel)
                else:
                    self._list_enter(used, sel, title)
                if not used:
                    continue
            elif key == "a" and used and self._ok_song(used[sel]):
                self.enqueue(used[sel])
            elif key == "s" and used:
                # cola aleatoria con la lista ENTEIRA (ordena las
                # continuaciones vía loader si hay paginación)
                self.flash("mezclando…")
                songs = self._drain_list(used, loader)
                self.shuffle_play(songs, title, context)
            elif key == "d" and used and self._ok_song(used[sel]):
                self.dl.submit(used[sel]["id"], used[sel].get("title", ""))
                self.flash("⤓ copia local en cola: "
                           f"{used[sel].get('title', '?')}", "info")
            elif key == "D" and used:
                # la lista ENTEIRA a la carpeta de copias (con paginación);
                # en una playlist remota también la espeja en las
                # playlists locales de la biblioteca
                self.flash("bajando la lista a la copia local…")
                songs = self._drain_list(used, loader)
                n = self._dl_rows(songs, whole=True)
                if yt_pl:
                    nota = self._mirror_pl(title, songs)
                    if nota:
                        if n:
                            self.flash(f"⤓ {n} copias en cola"
                                       f" · playlist local {nota}", "info")
                        else:
                            self.flash("todo ya está en la copia local"
                                       f" · playlist local {nota}", "info")
            elif key == "f" and used and used[sel].get("id"):
                cur = used[sel]
                if cur.get("title"):
                    self.db.upsert_songs([cur])
                added = self.db.toggle_favorite(cur["id"])
                fav_cache[cur["id"]] = added
                self.flash("★ añadido a favoritos" if added
                           else "quitado de favoritos", "info")
            elif key == "r" and used and self._ok_song(used[sel]):
                self.play_song(used[sel], fill_radio=True, context=context)
            elif key == "c" and used and used[sel].get("id"):
                ok = copy_to_clipboard(config.yt_watch_url(used[sel]["id"]))
                self.flash("URL copiada al portapapeles" if ok
                           else "sin xclip/wl-copy instalado",
                           "info" if ok else "warn")
            elif key == "l" and used and self._ok_song(used[sel]):
                self.lyrics_screen(used[sel])
            elif key == "p" and used and used[sel].get("id"):
                self.add_to_playlist_menu(used[sel])
            elif key == "K" and used and used[sel].get("type", "song") == "song":
                self.toggle_account_like(used[sel])
            elif key == "space" and self.player.proc:
                self.player.toggle()
            elif key == "n":
                self.auto_next()
            elif key == "v":
                self.play_prev()
            elif key in ("esc", "left", "backspace"):
                return None
            elif key == "q":
                return None
            if loader and used and sel >= len(used) - 3 and not fetching:
                fetching = True
                try:
                    more = loader(len(used))
                    if more:
                        have = {i.get("id") for i in used}
                        for it in more:
                            if it.get("id") and it["id"] not in have:
                                used.append(it)
                                if it.get("id"):
                                    fav_cache[it["id"]] = \
                                        self.db.is_favorite(it["id"])
                        self.db.upsert_songs(more)
                    else:
                        loader = None
                except (ApiError, NetworkError):
                    loader = None
                fetching = False

    def _list_enter(self, items, sel, title):
        it = items[sel]
        t = it.get("type", "song")
        if t == "song":
            self.play_song(it, queue_items=items, start=sel, context=title)
        elif t == "album":
            self.browse_screen(it["id"], it.get("title", "Álbum"))
        elif t == "artist":
            self.artist_screen(it["id"], it.get("title", "Artista"))
        elif t == "playlist":
            self.playlist_screen(it["id"], it.get("title"))
        else:
            self.play_song(it)

    # ================================================================= #
    # pantallas de detalle (álbum/artista/playlist)
    # ================================================================= #
    def browse_screen(self, browse_id, fallback_title=None, is_playlist=False):
        try:
            res = self.api.generic_browse(browse_id)
        except (ApiError, NetworkError):
            raise
        title = res.get("title") or fallback_title or "Detalle"
        subs = res.get("subtitle") or ""
        sections = res.get("sections", [])
        if not sections:
            if res.get("continuation") is None:
                self.alert(f"{title}: sin contenido", title="Vacío")
                return
        songs = []
        for sec in sections:
            for it in sec.get("items", []):
                if it.get("type") == "song" and not songs:
                    songs, kind = list(sec["items"]), "list"
                if sec.get("kind") == "shelf" and it.get("type") == "song":
                    songs.append(it)
        token = res.get("continuation")

        def more(off):
            if not token_container[0]:
                return []
            try:
                r2 = self.api.continuation(token_container[0], browse_id)
                token_container[0] = r2.get("continuation")
                self.db.upsert_songs(r2.get("items", []))
                return r2.get("items", [])
            except (ApiError, NetworkError) as e:
                self.flash(f"sin más: {e}", "warn")
                return []

        token_container = [token]
        if len(sections) == 1 and all(
                it.get("type", "song") == "song" for it in
                sections[0].get("items", [])[:5] or []):
            songs = list(sections[0].get("items", []))
            self.db.upsert_songs(songs)
            self.list_screen(title, songs, context=subs, loader=more,
                             yt_pl=(browse_id if is_playlist else None))
            return
        self.shelf_screen(title, sections, subs)

    def shelf_screen(self, title, sections, subs=""):
        items = [(f"{s.get('title') or 'Selección'}  "
                  f"({len(s.get('items', []))})", None)
                 for s in sections if s.get("items")]
        if not items:
            self.alert(f"{title}: sin contenido", title="Vacío")
            return
        items.append(("Volver", C.FG_DIM))
        sel, offset = 0, 0
        while True:
            y, x, h, w = self.draw_base(None, title, HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4, items,
                      sel, offset)
            key = self.get_key(60.0)
            self._consume_auto()
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key in ("enter", "right"):
                if sel == len(items) - 1:
                    return
                sec = [s for s in sections if s.get("items")][sel]
                self.db.upsert_songs(sec.get("items", []))
                self.list_screen(sec.get("title") or title,
                                 sec.get("items", []))
            elif key in ("esc", "left"):
                return

    def artist_screen(self, browse_id, fallback_title=None):
        res = self.api.generic_browse(browse_id)
        title = res.get("title") or fallback_title or "Artista"
        if not res.get("sections"):
            self.alert("Sin contenido para este artista", title="Vacío")
            return
        self.shelf_screen(title, res["sections"], res.get("subtitle", ""))

    def playlist_screen(self, playlist_id, fallback_title=None):
        self.browse_screen(playlist_id, fallback_title, is_playlist=True)

    def details_screen(self, song):
        t = song.get("type", "song")
        if t in ("album", "artist", "playlist"):
            getattr(self, {"album": "browse_screen", "artist": "artist_screen",
                           "playlist": "playlist_screen"}[t])(song["id"],
                                                              song.get("title"))
            return
        artists = song.get("artists") or []
        lines = [
            (f"Título:   {song.get('title', '?')}", C.BOLD + C.FG),
            ("Artista:  " + (", ".join(a[0] for a in artists) or "—"), C.FG),
            (f"Álbum:    {song.get('album') or '—'}", C.FG),
            (f"Duración: {config.fmt_secs(song.get('duration'))}", C.FG),
            ("URL:      " + config.yt_watch_url(song["id"]), C.FG_DIM),
            ("", 0),
            ("teclas: Enter reproducir · a encolar · f favorito · l letra · p playlist", C.FG_DIM),
        ]
        while True:
            y, x, h, w = self.draw_base(None,
                                        f"Detalles · {song.get('title', '?')}",
                                        HINT_BACK)
            for i, (txt, col) in enumerate(lines[:h - 2]):
                self.scr.move(y + i, x + 2)
                self.scr.write((col or "") + fit(txt, w - 4) + C.RESET)
            key = self.get_key(60.0)
            self._consume_auto()
            if key in ("esc", "left"):
                return
            if key == "enter":
                self.play_song(song)
            elif key == "a":
                self.enqueue(song)
            elif key == "f":
                self.db.upsert_songs([song])
                ok = self.db.toggle_favorite(song["id"])
                self.flash("★ añadido" if ok else "quitado", "info")
            elif key == "K":
                self.toggle_account_like(song)
            elif key == "l":
                self.lyrics_screen(song)
            elif key == "p":
                self.add_to_playlist_menu(song)

    def lyrics_screen(self, song):
        self.flash("buscando letra…")
        self.draw_rail(0)
        try:
            res = self.api.next(song["id"])
            lyr = None
            ep = res.get("lyrics_endpoint")
            if ep and ep.get("browseId"):
                lyr = self.api.lyrics(ep["browseId"], ep.get("params"))
        except (ApiError, NetworkError) as e:
            self.flash(None)
            self.alert(f"Letras no disponibles: {e}", title="Letras")
            return
        self.flash(None)
        if not lyr:
            self.alert("No hay letra para esta canción", title="Letras")
            return
        self.text_screen(f"Letras · {song.get('title', '?')}", lyr)

    def text_screen(self, title, text):
        lines = []
        for ln in text.split("\n"):
            lines.extend(wrap(ln, 76) or [""])
        pos = 0
        while True:
            y, x, h, w = self.draw_base(None, title,
                                        "↑↓/PgUp/PgDn · Esc volver")
            for i, ln in enumerate(lines[pos:pos + h - 2]):
                self.scr.move(y + i, x + 2)
                self.scr.write(C.FG + fit(ln, w - 4) + C.RESET)
            self.scr.move(y + h - 1, x + 2)
            self.scr.write(C.FG_DIM + fit(f"{pos + 1}/{len(lines)}", w - 4)
                           + C.RESET)
            key = self.get_key(60.0)
            if key in ("esc", "left", "enter", "q"):
                return
            if key in ("up", "k"):
                pos = max(0, pos - 1)
            elif key in ("down", "j"):
                pos = min(max(0, len(lines) - (h - 2)), pos + 1)
            elif key == "pgup":
                pos = max(0, pos - (h - 2))
            elif key == "pgdn":
                pos = min(max(0, len(lines) - (h - 2)), pos + (h - 2))

    def add_to_playlist_menu(self, song):
        if not song.get("id"):
            return
        pls = self.db.playlists()
        items = [(f"{p['name']}  ({p['count']})", None) for p in pls]
        items.append(("+ nueva playlist…", C.ACCENT))
        items.append(("Volver", C.FG_DIM))
        sel, offset = 0, 0
        while True:
            y, x, h, w = self.draw_base(None, "Añadir a playlist", HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4, items,
                      sel, offset)
            key = self.get_key(60.0)
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key in ("enter", "right"):
                if sel < len(pls):
                    pid = pls[sel]["pid"]
                    self.db.upsert_songs([song])
                    self.db.add_to_playlist(pid, song["id"])
                    self.flash(f"añadida a {pls[sel]['name']}", "info")
                    return
                if sel == len(items) - 2:
                    name = self.input_box("Nueva playlist", "Nombre:")
                    if name:
                        pid = self.db.create_playlist(name)
                        if pid:
                            self.db.upsert_songs([song])
                            self.db.add_to_playlist(pid, song["id"])
                            self.flash("playlist creada y canción añadida",
                                       "info")
                        else:
                            self.flash("ese nombre ya existe", "warn")
                    return
                return
            elif key in ("esc", "left", "q"):
                return

    # ================================================================= #
    # INICIO
    # ================================================================= #
    def scr_inicio(self):
        favs = self.db.favorites()
        hist = self.db.history(30)
        try:
            shelves, _ = self.api.home()
        except (ApiError, NetworkError) as e:
            shelves = []
            self.flash(f"inicio en vivo: {e}", "warn")
        items = [
            (f"★ Favoritos  ({len(favs)})", None),
            (f"Historial reciente  ({len(hist)})", None),
            ("Continuar última cola", None),
            ("Radio aleatoria", None),
        ]
        for sh in shelves:
            items.append((f"{sh['title']}  ({len(sh['items'])})", C.ACCENT2))
        items.append(("Volver", C.FG_DIM))
        sel, offset = 0, 0
        base = 4                                   # favoritos..radio
        while True:
            y, x, h, w = self.draw_base("inicio", "Inicio", HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4, items,
                      sel, offset)
            key = self.get_key(60.0)
            self._consume_auto()
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key in ("enter", "right"):
                if sel == 0:
                    if favs:
                        self.list_screen("★ Favoritos", favs,
                                         context="favoritos")
                        favs = self.db.favorites()
                        hist = self.db.history(30)
                        items[0] = (f"★ Favoritos  ({len(favs)})", None)
                    else:
                        self.flash("sin favoritos (pulsa f en una canción)",
                                   "warn")
                elif sel == 1:
                    if hist:
                        self.list_screen("Historial", hist, context="recientes")
                        hist = self.db.history(30)
                        items[1] = (f"Historial reciente  ({len(hist)})", None)
                    else:
                        self.flash("aún no escuchaste nada", "warn")
                elif sel == 2:
                    self.continue_last_queue()
                elif sel == 3:
                    self.random_radio()
                elif sel < len(items) - 1:
                    sh = shelves[sel - base]
                    self.db.upsert_songs(sh["items"])
                    self.list_screen(sh.get("title", "Inicio"), sh["items"])
                else:
                    return
            elif key in ("esc", "left"):
                return

    def continue_last_queue(self):
        saved = self.db.get_meta("queue_state")
        if isinstance(saved, dict) and saved.get("items"):
            self.q.restore(saved)
            cur = self.q.current()
            if cur:
                self.play_stream(cur)
                self.flash("cola restaurada", "info")
                return
        self.flash("no hay cola guardada", "warn")

    def random_radio(self):
        seed = None
        favs = self.db.favorites()
        if favs:
            seed = random.choice(favs)
        if not seed:
            try:
                _, bands = self.api.charts()
                songs = [s for b in bands for s in (b.get("items", []) or [])
                         if self._ok_song(s)]
                if songs:
                    seed = random.choice(songs)
            except (ApiError, NetworkError):
                seed = None
        if not seed:
            self.flash("sin punto de partida para la radio", "warn")
            return
        self.play_song(seed, fill_radio=True, context="radio aleatoria")

    # ================================================================= #
    # EXPLORAR
    # ================================================================= #
    def scr_explorar(self):
        buttons, sections = [], []
        try:
            buttons, sections = self.api.explore()
        except (ApiError, NetworkError) as e:
            self.flash(f"explore en vivo: {e}", "warn")
        n_static = 3
        items = [("Géneros y moods", None), ("Charts", None),
                 ("Nuevos lanzamientos", None)]
        for b in buttons:
            items.append((b.get("title", "?"), C.ACCENT2))
        for sh in sections:
            items.append((f"{sh['title']}  ({len(sh['items'])})", None))
        items.append(("Volver", C.FG_DIM))
        sel, offset = 0, 0
        while True:
            y, x, h, w = self.draw_base("explorar", "Explorar", HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4, items,
                      sel, offset)
            key = self.get_key(60.0)
            self._consume_auto()
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key in ("enter", "right"):
                if sel == 0:
                    self.moods_screen()
                elif sel == 1:
                    self.charts_screen()
                elif sel == 2:
                    self.new_releases()
                elif sel < len(items) - 1:
                    k = sel - n_static
                    if k < len(buttons):
                        self.mood_section(buttons[k])
                    else:
                        shi = sections[k - len(buttons)]
                        self.list_screen(shi.get("title", "Explorar"),
                                         shi.get("items", []))
                else:
                    return
            elif key in ("esc", "left"):
                return

    def moods_screen(self):
        try:
            buttons = self.api.moods_and_genres()
        except (ApiError, NetworkError) as e:
            self.alert(f"Fallo en moods: {e}", title="API")
            return
        items = [b.get("title", "?") for b in buttons]
        items.append("Volver")
        sel, offset = 0, 0
        while True:
            y, x, h, w = self.draw_base(None, "Géneros y moods", HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4,
                      [(t, None) for t in items], sel, offset)
            key = self.get_key(60.0)
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key in ("enter", "right"):
                if sel == len(items) - 1:
                    return
                self.mood_section(buttons[sel])
            elif key in ("esc", "left"):
                return

    def mood_section(self, button):
        try:
            title, sections = self.api.mood_section(button.get("browseId"),
                                                    button.get("params"))
        except (ApiError, NetworkError) as e:
            self.alert(f"Fallo en moods: {e}", title="API")
            return
        if not sections:
            self.alert(f"{button.get('title', '?')}: sin contenido", title="")
            return
        self.shelf_screen(title or button.get("title", "Mood"), sections)

    def charts_screen(self):
        try:
            title, bands = self.api.charts()
        except (ApiError, NetworkError) as e:
            self.alert(f"Fallo en charts: {e}", title="API")
            return
        if not bands:
            self.alert("Sin charts disponibles", title="Vacío")
            return
        self.shelf_screen(title, bands)

    def new_releases(self):
        try:
            _title, sections = self.api.new_releases()
        except (ApiError, NetworkError) as e:
            self.alert(f"Fallo en lanzamientos: {e}", title="API")
            return
        if not sections:
            self.alert("Sin lanzamientos", title="Vacío")
            return
        self.shelf_screen("Nuevos lanzamientos", sections)

    # ================================================================= #
    # BUSCAR
    # ================================================================= #
    def scr_buscar(self):
        text = self.input_box("Buscar", "Canción, artista, álbum…",
                              suggestion_fn=self.api.suggestions)
        if not text:
            return
        self.results_screen(text)

    def _tab_params(self, tab):
        key = self.TABS[tab][0]
        return dict(api_mod.SEARCH_FILTERS).get(key)

    def results_screen(self, query, tab=0):
        data = {}
        while True:
            tab = tab % len(self.TABS)
            tkey = self.TABS[tab][0]
            d = data.setdefault(tkey, {"items": [], "cont": None, "done": False})
            if not d["items"] and not d["done"]:
                self.flash("buscando…")
                self.draw_rail(0)
                try:
                    res = self.api.search(query, params=self._tab_params(tab))
                    d["items"] = res["items"]
                    d["cont"] = res["continuation"]
                except (ApiError, NetworkError) as e:
                    d["err"] = str(e)
                d["done"] = True
                self.db.upsert_songs([i for i in d["items"]
                                      if i.get("type") == "song"])
                self.flash("« encontradas "
                           f"{len(d['items'])} {self.TABS[tab][1].lower()} »",
                           "info")
            if not d["items"]:
                self.alert(f"Sin resultados para «{query}»"
                           + (f" ({d.get('err')})" if d.get("err") else ""),
                           title="Búsqueda")
                return
            page = d

            def load(off, _d=page, _q=query, _t=tab):
                if not _d.get("cont"):
                    return []
                try:
                    res = self.api.search(_q, params=self._tab_params(_t),
                                          continuation=_d["cont"])
                    _d["cont"] = res["continuation"]
                    return res["items"]
                except (ApiError, NetworkError):
                    _d["cont"] = None
                    return []

            def tabs_line(yy, xx, ww):
                self.scr.move(yy, xx)
                seg = ""
                for i, (_k, label) in enumerate(self.TABS):
                    if i == tab:
                        seg += C.ACCENT + C.BOLD + f"{label} "
                    else:
                        seg += C.FG_DIM + f"{label} · "
                self.scr.write(fit(seg, ww) + C.RESET)

            def on_key(key, _t=tab):
                nonlocal tab
                if key == "right":
                    tab = (tab + 1) % len(self.TABS)
                    return "switch"
                if key == "left":
                    tab = (tab - 1) % len(self.TABS)
                    return "switch"
                return None

            hint = "←→ pestañas · " + HINT_LIST
            r = self.list_screen(f"{self.TABS[tab][1]}: «{query}»", d["items"],
                                 hint=hint, context=query,
                                 loader=load, on_enter=self._tab_enter,
                                 header_draw=tabs_line, on_key=on_key)
            if r == "switch":
                continue
            return

    def _tab_enter(self, item, idx):
        t = item.get("type", "song")
        if t == "song":
            self.play_song(item, fill_radio=False)
        elif t == "album":
            self.browse_screen(item["id"], item.get("title"))
        elif t == "artist":
            self.artist_screen(item["id"], item.get("title"))
        elif t == "playlist":
            self.playlist_screen(item["id"], item.get("title"))

    # ================================================================= #
    # BIBLIOTECA
    # ================================================================= #
    def scr_biblioteca(self):
        sel, offset = 0, 0
        while True:
            favs = self.db.favorites()
            hist = self.db.history(50)
            pls = self.db.playlists()
            signed = self.api.signed_in
            acct = self._bib_acct if isinstance(self._bib_acct, dict) else {}
            if signed:
                li = acct.get("likes")
                lp = acct.get("playlists")
                lh = acct.get("yt_history")
                items = [
                    ("— tu cuenta de Google —", C.FG_DIM),
                    (f"♥ Me gusta de tu cuenta"
                     f"{'' if li is None else f'  ({len(li)})'}", None),
                    (f"Playlists de tu cuenta"
                     f"{'' if lp is None else f'  ({len(lp)})'}", None),
                    ("Historial de YouTube Music", None),
                    (f"★ Canciones favoritas  ({len(favs)})", None),
                    (f"Historial local  ({len(hist)})", None),
                    (f"Playlists locales  ({len(pls)})", None),
                    ("Borrar historial local", C.WARN),
                    ("Volver", C.FG_DIM),
                ]
                i_fav, i_hist, i_pls, i_borr, i_out = 4, 5, 6, 7, 8
                i_likes, i_plists, i_yth, i_div = 1, 2, 3, 0
            else:
                items = [
                    (f"★ Canciones favoritas  ({len(favs)})", None),
                    (f"Historial  ({len(hist)})", None),
                    (f"Playlists locales  ({len(pls)})", None),
                    ("★ Cuenta de Google: sin sesión · copiarla en "
                     "Ajustes", C.FG_DIM),
                    ("Borrar historial", C.WARN),
                    ("Volver", C.FG_DIM),
                ]
                i_fav, i_hist, i_pls, i_borr, i_out = 0, 1, 2, 4, 5
                i_likes = i_plists = i_yth = i_div = -1
            y, x, h, w = self.draw_base("biblioteca", "Biblioteca", HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4, items,
                      sel, offset)
            key = self.get_key(60.0)
            self._consume_auto()
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key in ("enter", "right"):
                if sel == i_div:
                    continue
                if sel == i_fav:
                    if favs:
                        self.list_screen("Favoritos", favs, context="favoritos")
                    else:
                        self.flash("sin favoritos (pulsa f en una canción)",
                                   "warn")
                elif sel == i_hist:
                    if hist:
                        self.list_screen("Historial", hist,
                                         context="recientes")
                    else:
                        self.flash("todavía sin historial", "warn")
                elif sel == i_pls:
                    self.playlists_screen()
                elif signed and sel == i_likes:
                    self.acct_likes()
                elif signed and sel == i_plists:
                    self.acct_playlists()
                elif signed and sel == i_yth:
                    self.acct_history()
                elif sel == i_borr:
                    if self.confirm("¿Borrar todo el historial local?",
                                    default=False):
                        self.db.clear_history()
                        self.flash("historial borrado", "info")
                else:
                    return
            elif key in ("esc", "left"):
                return

    # ---------------------------------------------------------------- #
    # biblioteca de la cuenta de Google
    # ---------------------------------------------------------------- #
    def toggle_account_like(self, song):
        """K: marca/quita Me gusta en la cuenta de YouTube Music."""
        if not self.api.signed_in:
            self.flash("inicia sesión con Google en Ajustes para usar "
                       "Me gusta de la cuenta", "warn")
            return
        vid = song.get("id")
        if not vid:
            return
        on = not bool(self._likes.get(vid))
        try:
            self.api.set_like(vid, on)
        except (ApiError, NetworkError) as e:
            self.flash(f"no se pudo enviar a tu cuenta: {e}", "err")
            return
        self._likes[vid] = on
        self.flash("♥ añadido a Me gusta de tu cuenta" if on
                   else "♥ quitado de Me gusta de tu cuenta", "ok")

    def _acct_fetch(self, key, fn, label):
        acorta = False
        acct = self._bib_acct if isinstance(self._bib_acct, dict) else {}
        val = acct.get(key)
        if val is not None:
            return val
        self.flash(f"cargando {label}…")
        self.draw_rail(0)
        result = {}

        def work():
            try:
                result["val"] = fn()
            except (ApiError, NetworkError) as e:
                result["err"] = e

        threading.Thread(target=work, name=f"acct-{key}",
                         daemon=True).start()
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        n = 0
        while "val" not in result and "err" not in result:
            self.flash(f"cargando {label}… {frames[n % len(frames)]}")
            self.draw_rail(0)
            self.get_key(0.08)
            n += 1
        self.flash(None)
        if "err" in result:
            self.alert(f"Tu cuenta no respondió:\n{result['err']}",
                       title="Cuenta Google")
            return None
        acct[key] = result["val"]
        self._bib_acct = acct
        return result["val"]

    def acct_likes(self):
        def fetch():
            def old_route():
                try:
                    data = self.api._post("browse",
                                          {"browseId": "FEmusic_liked_videos"})
                    return self.api._collect_with_continuations(data)
                except (ApiError, NetworkError):
                    return []
            try:
                return self.api.playlist_songs("LM")["items"]
            except (ApiError, NetworkError):
                return old_route()
        songs = self._acct_fetch("likes", fetch, "Me gusta de la cuenta")
        if songs is None:
            return
        if songs:
            self.list_screen("♥ Me gusta (cuenta)", songs,
                             context="me gusta · cuenta Google")
            acct = self._bib_acct if isinstance(self._bib_acct, dict) else {}
            acct["likes"] = songs
            self._bib_acct = acct
        else:
            self.flash("aún no has marcado Me gusta en tu cuenta", "info")

    def acct_playlists(self):
        pls = self._acct_fetch("playlists", self.api.library_playlists,
                               "playlists de la cuenta")
        if pls is None:
            return
        if not pls:
            self.flash("no tienes playlists en tu cuenta", "info")
            return
        while True:
            items = [(f"{p.get('title', '?')}  "
                      f"({(p.get('count') or '?').split('·')[-1].strip()})",
                      None) for p in pls]
            items.append(("Volver", C.FG_DIM))
            pista = ("↑↓ mover · Enter/→ abrir · d guardar en local "
                     "(canciones + playlist local) · Esc/← volver")
            y, x, h, w = self.draw_base(None, "Playlists de tu cuenta",
                                        pista)
            sel = getattr(self, "_acct_pls_sel", 0)
            sel = min(sel, len(items) - 1)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4, items,
                      sel, 0)
            key = self.get_key(60.0)
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            self._acct_pls_sel = sel
            if key == "up" or key == "down":
                continue
            if key in ("esc", "left", "q"):
                return
            if key == "d" and sel < len(pls):
                pid = pls[sel].get("id")
                if not pid:
                    continue
                self.flash("bajando la playlist a la copia local…")
                self.draw_rail(0)
                try:
                    datos = self.api.playlist_songs(pid)
                except (ApiError, NetworkError) as e:
                    self.flash(None)
                    self.alert(f"No se pudo guardar la playlist:\n{e}",
                               title="Cuenta Google")
                    continue
                self.flash(None)
                songs = datos.get("items") or []
                nombre = (datos.get("title")
                          or pls[sel].get("title") or "Playlist")
                n = self._dl_rows(songs, whole=True)
                nota = self._mirror_pl(nombre, songs)
                if nota:
                    if n:
                        self.flash(f"⤓ {n} copias en cola"
                                   f" · playlist local {nota}", "info")
                    else:
                        self.flash("todo ya está en la copia local"
                                   f" · playlist local {nota}", "info")
            if key in ("enter", "right"):
                if sel >= len(pls):
                    return
                pid = pls[sel].get("id")
                if not pid:
                    continue
                self.flash("cargando playlist…")
                self.draw_rail(0)
                try:
                    res = self.api.playlist_songs(pid)
                except (ApiError, NetworkError) as e:
                    self.flash(None)
                    self.alert(f"No se pudo abrir la playlist:\n{e}",
                               title="Cuenta Google")
                    continue
                self.flash(None)
                self.list_screen(res.get("title") or pls[sel].get("title", "?"),
                                 res.get("items") or [],
                                 context="playlist · cuenta Google",
                                 yt_pl=pid)

    def acct_history(self):
        res = self._acct_fetch("yt_history", self.api.yt_history,
                               "historial de YouTube")
        if res is None:
            return
        items = res.get("items") or []
        if items:
            self.list_screen("Historial de YouTube", items,
                             context="historial · cuenta Google")
        else:
            self.flash("tu cuenta no tiene historial", "info")

    def playlists_screen(self):
        while True:
            pls = self.db.playlists()
            items = [(f"{p['name']}  ({p['count']})", None) for p in pls]
            items.append(("+ nueva playlist…", C.ACCENT))
            items.append(("Volver", C.FG_DIM))
            sel, offset = 0, 0
            y, x, h, w = self.draw_base(None, "Playlists locales", HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(items)), w - 4, items,
                      sel, offset)
            key = self.get_key(60.0)
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key == "d" and sel < len(pls):
                if self.confirm(f"¿Borrar «{pls[sel]['name']}»?", default=False):
                    self.db.delete_playlist(pls[sel]["pid"])
                    self.flash("playlist borrada", "info")
            elif key == "e" and sel < len(pls):
                name = self.input_box("Renombrar playlist", "Nuevo nombre:",
                                      pls[sel]["name"])
                if name:
                    if self.db.rename_playlist(pls[sel]["pid"], name):
                        self.flash("renombrada", "info")
                    else:
                        self.flash("ese nombre ya existe", "warn")
            elif key in ("enter", "right"):
                if sel < len(pls):
                    songs = self.db.playlist_songs(pls[sel]["pid"])
                    if songs:
                        self.list_screen(pls[sel]["name"], songs,
                                         context="playlist local")
                    else:
                        self.flash("playlist vacía (pulsa p en una canción)",
                                   "warn")
                elif sel == len(items) - 2:
                    name = self.input_box("Nueva playlist", "Nombre:")
                    if name:
                        pid = self.db.create_playlist(name)
                        if pid:
                            self.flash("playlist creada", "info")
                        else:
                            self.flash("ese nombre ya existe", "warn")
                else:
                    return
            elif key in ("esc", "left", "q"):
                return

    # ================================================================= #
    # COLA
    # ================================================================= #
    def scr_cola(self):
        sel, offset = 0, 0
        while True:
            self._consume_auto()
            if not self.q.items:
                rows = [("cola vacía — reproduce algo", C.FG_DIM)]
            else:
                rows = []
                for i, it in enumerate(self.q.items):
                    mark = " ◀ sonando" if i == self.q.index else ""
                    rows.append(f"{i + 1}. {self._song_row(it)}{mark}")
            ctx = self.q.context if isinstance(self.q.context, str) else \
                (self.q.context or {}).get("title", "") if self.q.context else ""
            title = (f"Cola  ({len(self.q.items)})"
                     + (f" · {ctx}" if ctx else ""))
            hint = ("↑↓ navegar · Enter saltar · d quitar · c vaciar · s mezcla · "
                    "R repetir · n/v siguiente/anterior · b reproducir por fin")
            y, x, h, w = self.draw_base("cola", title, hint)
            self.menu(y + 1, x + 2, h - 2, w - 4, rows, sel, offset)
            key = self.get_key(60.0)
            if key is None:
                continue
            self._consume_auto()
            if key == "up":
                sel = (sel - 1) % max(1, len(rows))
            elif key == "down":
                sel = (sel + 1) % max(1, len(rows))
            elif key == "pgup":
                sel = max(0, sel - max(1, h - 4))
            elif key == "pgdn":
                sel = min(len(rows) - 1, sel + max(1, h - 4))
            elif key in ("enter", "right") and self.q.items:
                self.q.jump(sel)
                self.play_stream(self.q.items[sel])
            elif key == "d" and self.q.items:
                self.q.remove(sel)
                sel = min(sel, max(0, len(self.q.items) - 1))
            elif key == "c":
                if self.confirm("¿Vaciar la cola?", default=False):
                    self.q.clear()
                    self.flash("cola vaciada", "info")
            elif key == "s":
                on = self.q.toggle_shuffle()
                self.flash(("mezcla activada" if on else "mezcla desactivada"),
                           "info")
            elif key == "R":
                self.q.cycle_repeat()
                self.flash("repetición: "
                           + config.REPEAT_LABELS.get(self.q.mode, "?"), "info")
            elif key == "n":
                self.auto_next()
            elif key == "v":
                self.play_prev()
            elif key == "space" and self.player.proc:
                self.player.toggle()
            elif key in ("esc", "left"):
                return

    # ================================================================= #
    # AJUSTES
    # ================================================================= #
    def _dl_stats_row(self):
        """Fila de Ajustes con el estado de la carpeta de copias."""
        n, by = self.dl.store.stats()
        return (f"Copias sin internet: {n} pistas · {human_bytes(by)}"
                f" — Enter gestionar")

    def _downloads_screen(self):
        """Gestor de la carpeta de copias: liberar, vaciar, abrir."""
        sel = 0
        while True:
            vids = self.dl.store.all()
            rows = []
            for vid, path in vids:
                meta = self.db.get_song(vid) or {}
                tit = meta.get("title") or vid
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                rows.append((f"{tit}  —  {human_bytes(size)}", vid))
            n, _total = self.dl.store.stats()
            hint = ("↑↓ navegar · Enter liberar esta copia · A vaciar todas "
                    "· o abrir carpeta · Esc volver")
            y, x, h, w = self.draw_base(None, "Copias en local", hint)
            labels = [r[0] for r in rows] or \
                [f"(carpeta vacía) — {os.path.join(config.DATA_DIR,
                                                   'offline')}"]
            self.menu(y + 1, x + 2, min(h - 2, len(labels)), w - 4,
                      labels, sel, 0)
            key = self.get_key(60.0)
            self._consume_auto()
            if key in ("up", "k"):
                sel = (sel - 1) % max(1, len(rows))
            elif key in ("down", "j"):
                sel = (sel + 1) % max(1, len(rows))
            elif key == "space" and self.player.proc:
                self.player.toggle()
            elif key in ("esc", "left", "q"):
                return
            elif key == "o":
                if self._open_browser(os.path.join(config.DATA_DIR,
                                                   "offline")):
                    self.flash("carpeta abierta en el navegador de ficheros",
                               "info")
                else:
                    self.flash("sin xdg-open para abrir la carpeta", "warn")
            elif key == "A" and rows:
                if self.confirm(f"¿Vaciar TODAS las copias ({n})?",
                                default=False):
                    self.dl.store.wipe()
                    sel = 0
            elif key in ("enter", "right") and rows:
                tit, vid = rows[sel]
                if self.confirm(f"¿Liberar la copia de «{tit}»?",
                                default=False):
                    self.dl.store.wipe(vid)
                    sel = min(sel, max(0, len(rows) - 2))

    def scr_ajustes(self):
        sel = 0
        while True:
            s = self.settings
            helper = ("instalado" if self.extractor.helper_ready()
                      else "no instalado")
            rows = [
                (f"Región:          {s.get('region', 'ES')}", "region"),
                (f"Idioma:          {s.get('language', 'es')}", "lang"),
                (f"Calidad audio:   "
                 + config.QUALITY_LABELS.get(s.get('audio_quality', 'high'), "?"),
                 "quality"),
                (f"Repetición:      "
                 + config.REPEAT_LABELS.get(s.get('repeat', 'none'), "?"),
                 "repeat"),
                (f"Mezcla por omisión: "
                 + ("sí" if s.get('shuffle') else "no"), "shuffle"),
                (f"Extractor:       "
                 + config.EXTRACTOR_LABELS.get(s.get('extractor', 'auto'), "?"),
                 "extractor"),
                (f"Guardar en local lo reproducido: "
                 + ("sí" if s.get("offline_auto") else "no"), "offline_auto"),
                (self._dl_stats_row(), "downloads"),
                (f"Helper yt-dlp:   {helper}", "ytdlp"),
                ("Renovar sesión de YouTube (visitorData)", "visitor"),
                ("Cuenta de Google: "
                 + (f"{self.api.auth_name or 'cuenta'} — sesión activa"
                    if self.api.signed_in else "sin sesión (copia de tu "
                                               "navegador)"),
                 "account"),
                ("Vaciar historial", "purge"),
                ("Acerca de Music Terminal", "about"),
                ("Volver", "back"),
            ]
            y, x, h, w = self.draw_base("ajustes", "Ajustes", HINT_BACK)
            self.menu(y + 1, x + 2, min(h - 2, len(rows)), w - 4, rows,
                      sel, 0)
            key = self.get_key(60.0)
            if key == "up":
                sel = (sel - 1) % len(rows)
            elif key == "down":
                sel = (sel + 1) % len(rows)
            elif key in ("esc", "left", "q"):
                return
            elif key in ("enter", "right"):
                act = rows[sel][1]
                if act == "region":
                    i = list(REGIONS).index(s.get("region", "ES")) \
                        if s.get("region", "ES") in REGIONS else 0
                    self.settings["region"] = REGIONS[(i + 1) % len(REGIONS)]
                    self._apply_region()
                elif act == "lang":
                    i = list(LANGS).index(s.get("language", "es")) \
                        if s.get("language", "es") in LANGS else 0
                    self.settings["language"] = LANGS[(i + 1) % len(LANGS)]
                    self._apply_region()
                elif act == "quality":
                    order = ("high", "medium", "low")
                    i = order.index(s.get("audio_quality", "high")) \
                        if s.get("audio_quality", "high") in order else 0
                    s["audio_quality"] = order[(i + 1) % len(order)]
                    config.save_settings(self.settings)
                elif act == "repeat":
                    order = ("none", "all", "one")
                    i = order.index(s.get("repeat", "none")) \
                        if s.get("repeat", "none") in order else 0
                    self.q.mode = order[(i + 1) % len(order)]
                    config.save_settings(self.settings)
                elif act == "shuffle":
                    self.q.shuffle = not self.q.shuffle
                    if self.q.shuffle:
                        self.q._reshuffle(keep=self.q.index)
                    config.save_settings(self.settings)
                elif act == "extractor":
                    order = ("auto", "native", "ytdlp")
                    i = order.index(s.get("extractor", "auto")) \
                        if s.get("extractor", "auto") in order else 0
                    s["extractor"] = order[(i + 1) % len(order)]
                    config.save_settings(self.settings)
                elif act == "offline_auto":
                    s["offline_auto"] = not s.get("offline_auto", False)
                    config.save_settings(self.settings)
                    self.flash("copia automática al reproducir: "
                               + ("ON — todo lo que suene queda guardado"
                                  if s["offline_auto"] else "OFF"), "info")
                elif act == "downloads":
                    self._downloads_screen()
                elif act == "ytdlp":
                    self._setup_helper()
                elif act == "visitor":
                    self.api.reset_visitor()
                    try:
                        self.api.ensure_visitor()
                        self.flash("sesión renovada", "info")
                    except (ApiError, NetworkError) as e:
                        self.flash(f"no se renovó: {e}", "warn")
                elif act == "account":
                    if self.api.signed_in:
                        if self.confirm("¿Cerrar la sesión de Google en "
                                        "music-terminal?", default=False):
                            self.api.clear_auth()
                            self._bib_acct = None
                            self._likes = {}
                            self.flash("sesión de Google cerrada", "info")
                    else:
                        # login automático (o manual si no se autodetecta)
                        self.login_dialog()
                elif act == "purge":
                    if self.confirm("¿Borrar todo el historial local?",
                                    default=False):
                        self.db.clear_history()
                        self.flash("historial borrado", "info")
                elif act == "about":
                    self.about_screen()
                else:
                    return

    LOGIN_HELP = (
        "Automático (recomendado — Enter):\n"
        "  music-terminal abre music.youtube.com en tu navegador;\n"
        "  comprueba que estás con tu sesión de Google (avatar arriba\n"
        "  a la derecha) y vuelve aquí: la cookie se copia sola desde\n"
        "  Firefox / Zen / LibreWolf y navegadores Chromium.\n"
        "\n"
        "Manual (m): F12 → Red → recarga → clic en una petición\n"
        "youtubei → cabeceras → copia el encabezado «cookie»\n"
        "(o guárdala en el archivo:\n    " + config.COOKIE_TXT + ")"
    )

    @staticmethod
    def _open_browser(url):
        """Abre una URL en el navegador predeterminado del sistema."""
        exe = shutil.which("xdg-open")
        if not exe:
            return False
        try:
            subprocess.Popen([exe, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False

    def activate_cookie(self, raw, source_label=""):
        """Valida y activa una cookie de sesión de Google."""
        self.flash("activando la sesión…")
        self.draw_rail(0)
        try:
            self.api.set_auth(raw)
        except ApiError as e:
            self.flash(None)
            self.alert(f"Cookie inválida:\n{e}", title="Cuenta Google")
            return False
        ok, err = True, ""
        try:
            data = self.api._post("browse", {"browseId": "FEmusic_history"})
            st = ((data.get("playabilityStatus") or {}).get("status") or "")
            ok = st in ("", "OK")
        except (ApiError, NetworkError) as e:
            ok, err = False, str(e)
        if ok:
            name = self.api.account_name()
            self._bib_acct = None
            extra = f" ({source_label})" if source_label else ""
            if name:
                self.flash(f"sesión de Google iniciada: {name}{extra}", "ok")
            else:
                self.flash(f"sesión de Google iniciada{extra}", "ok")
            return True
        self.api.clear_auth()
        self.flash(None)
        self.alert(f"La cookie no es válida para YouTube Music:\n"
                   f"{err or 'el historial respondió con error'}",
                   title="Cuenta Google")
        return False

    def _paste_cookie_flow(self):
        # flujo manual clásico (input_box + cookie.txt)
        cookie = self.input_box(
            "Sesión de Google",
            "Pega aquí el encabezado «cookie» (o vacío → cookie.txt):")
        if cookie is None:
            return
        raw = cookie.strip()
        if not raw:
            try:
                with open(config.COOKIE_TXT, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
            except OSError:
                raw = ""
        if not raw:
            self.flash("no se proporcionó cookie", "warn")
            return
        self.activate_cookie(raw)

    def login_dialog(self):
        W, H = self._terminal()
        lines = wrap(self.LOGIN_HELP, 62)
        h = min(len(lines) + 3, H - 5)
        w = 64
        y0, x0, _, _ = self.centered(W, H, h, w)
        while True:
            self.draw_base(None, "Iniciar sesión con Google", HINT_BACK)
            draw_box(self.scr, y0 - 1, x0 - 1, h + 1, w + 2, "", C.WARN)
            for i in range(min(len(lines), h - 1)):
                self.scr.move(y0 + i, x0)
                self.scr.write(C.FG + fit(lines[i], w) + C.RESET)
            self.scr.move(y0 + min(len(lines), h - 1), x0)
            self.scr.write(C.FG_DIM + fit("Enter abrir navegador · m pegar "
                                          "a mano · Esc cancela", w)
                           + C.RESET)
            key = self.get_key(120.0)
            if key in ("esc", "left", "q"):
                return
            if key == "m":
                return self._paste_cookie_flow()
            if key in ("enter", "right"):
                break
        if not self._open_browser("https://music.youtube.com"):
            self.flash(None)
            if not self.confirm("No pude abrir el navegador en este "
                                "equipo.\n¿Pegar la cookie a mano?",
                                title="Cuenta Google"):
                return
            return self._paste_cookie_flow()
        if not self.confirm(
                "Se abrió music.youtube.com en tu navegador.\n"
                "Comprueba que estás con la sesión de Google iniciada\n"
                "(avatar arriba a la derecha).\n\n"
                "Pulsa Enter para copiar la cookie:",
                title="Copiar cookie del navegador"):
            return
        self.flash("copiando la cookie del navegador…")
        self.draw_rail(0)
        raw, src = browser_cookies.scan_sapisid_cookie()
        if raw:
            if self.activate_cookie(raw, f"cookie de {src}"):
                return
            if self.confirm("¿Pegar la cookie a mano?", title="Cuenta "
                            "Google"):
                self._paste_cookie_flow()
            return
        self.flash(None)
        if self.confirm("No encontré la cookie de YouTube en tus "
                        "navegadores.\n(¿te falta iniciar sesión, o usas "
                        "Chrome protegido por llavero?)\n¿Pegarla a mano?",
                        title="Cuenta Google"):
            self._paste_cookie_flow()

    def _apply_region(self):
        self.api.region = self.settings["region"]
        self.api.language = self.settings["language"]
        self.api.reset_visitor()
        config.save_settings(self.settings)
        self.flash("región/idioma aplicados (se renueva la sesión)", "info")

    def _setup_helper(self):
        if self.extractor.helper_ready():
            self.flash("el helper yt-dlp ya está instalado", "info")
            return
        if not self.confirm("Descargar helper yt-dlp (~80 MB) a "
                            + config.DATA_DIR + "?", default=True):
            return
        self.flash("descargando yt-dlp…")
        self.draw_rail(0)
        try:
            self.extractor.download_helper()
            self.flash("yt-dlp instalado", "info")
        except Exception as e:  # noqa: BLE001
            self.flash(None)
            self.alert(f"No se pudo descargar yt-dlp: {e}", title="yt-dlp")

    def about_screen(self):
        text = (
            "Music Terminal v" + config.APP_VERSION + "\n"
            "\n"
            "Cliente nativo de YouTube Music para la terminal de GNU/Linux,\n"
            "reescrito desde cero a partir de la app Android Metrolist\n"
            "13.7.0 (InnerTube · WEB_REMIX/ANDROID_VR · poToken via cloud).\n"
            "\n"
            "Interfaz inspirada en AniLinux Terminal (PedroSG08).\n"
            "Solo librería estándar de Python 3: sin dependencias.\n"
            "Requiere mpv para reproducir (sudo apt install mpv).\n"
            "\n"
            "Créditos: MetrolistGroup/Metrolist · AniLinux · yt-dlp (helper opcional)."
        )
        self.text_screen("Acerca de", text)

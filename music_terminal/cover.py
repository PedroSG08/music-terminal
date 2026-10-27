# -*- coding: utf-8 -*-
"""Portadas de canciones en ANSI (cuadrantes 2×2 + octavos verticales).

Descarga la miniatura en la mejor resolución disponible
(maxresdefault → hq720 → hqdefault → mqdefault), recorta el contenido
(quita las bandas negras del letterbox y centra al aspecto físico de la
región del panel), reescala con promedio de área (sin halos) más un
toque previo de nitidez (UnsharpMask) y renderiza con un **blitter
mixto** que elige por celda la representación con menos error:

- **cuadrantes** (U+2596–U+259F): 2×2 «píxeles» por celda con el par
  fg/bg que minimiza el error cuadrático de la celda,
- **octavos verticales** (U+2581–U+2588: ▁▂▃▄▅▆▇): la frontera fg/bg
  puede caer en cualquier múltiplo de 2 px dentro de la celda — los
  degradados salen con detalle vertical de 2 px en lugar de los 8 px
  de los medios bloques clásicos (▀/▄).

El resto del error se difunde por píxel (Floyd–Steinberg) para evitar
bandas.  Con terminales de color verdadero (COLORTERM=truecolor) los
colores salen en 24-bit exactos.  PIL es opcional; sin él se dibuja un
panel decorativo con la inicial.  Nada bloquea la TUI: la descarga y el
render van en un hilo y todo se cachea en disco (covers/*.ansi).
"""
import hashlib
import os
import re
import threading
import urllib.request

try:
    from PIL import Image, ImageFilter
    _PIL = True
except Exception:
    _PIL = False

# niveles de la paleta xterm-256 (solo cubos 16-255: constantes entre emuladores)
_CUBES = (0, 95, 135, 175, 215, 255)


def _boost_1200(url):
    """Variante de una portada de YouTube Music en tamaño mayor.

    Las miniaturas servidas por googleusercontent/ggpht aceptan un
    tamaño arbitrario en la cola de parámetros («…=w544-h544-l90-rj» o
    «…=s288»): reescribir a 1200 pide la portada nítida para cualquier
    panel.  Devuelve None cuando la URL no es de este tipo."""
    if not url or not re.search(r"(googleusercontent|ggpht)\.com", url):
        return None
    if re.search(r"=w\d+-h\d+", url):
        return re.sub(r"=w\d+-h\d+", "=w1200-h1200", url, count=1)
    if re.search(r"=[sS]\d+", url):
        return re.sub(r"=[sS]\d+", "=s1200", url, count=1)
    return None


def thumb_urls_for(video_id, fallback=None):
    """Candidatos de miniatura, de mayor a menor resolución."""
    urls = []
    boosted = _boost_1200(fallback)
    if boosted:
        urls.append(boosted)
    if fallback and fallback.startswith("http"):
        if "/default.jpg" in fallback:
            stem = fallback.split("/default.jpg")[0]
            urls += [stem + "/maxresdefault.jpg", stem + "/hq720.jpg",
                     stem + "/hqdefault.jpg"]
        urls.append(fallback)
    vid = (video_id or "").strip()
    if vid:
        urls.append(f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg")
        urls.append(f"https://i.ytimg.com/vi/{vid}/hq720.jpg")
        urls.append(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
        urls.append(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")
    dedup = []
    for u in urls:
        if u and u not in dedup:
            dedup.append(u)
    return dedup


def thumb_url_for(video_id, fallback=None):
    """Primera candidata (compatibilidad)."""
    ls = thumb_urls_for(video_id, fallback)
    return ls[0] if ls else f"https://i.ytimg.com/vi/{video_id or ''}/mqdefault.jpg"


def _key(url):
    return hashlib.md5(url.encode("utf-8", "replace")).hexdigest()[:16]


def _truecolor():
    """La terminal anuncia color de 24 bits (COLORTERM=truecolor/24bit)."""
    return os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")


# paleta 256 completa (cubo 16-231 + grises 232-255) para difundir el error
_PALETTE = []
for _ri, _cr in enumerate(_CUBES):
    for _gi, _cg in enumerate(_CUBES):
        for _bi, _cb in enumerate(_CUBES):
            _PALETTE.append((16 + 36 * _ri + 6 * _gi + _bi, (_cr, _cg, _cb)))
for _n in range(24):
    _v = 8 + _n * 10
    _PALETTE.append((232 + _n, (_v, _v, _v)))


def _nearest(r, g, b):
    """(índice256, rgb_real) del color más cercano."""
    best_i, best_p, best_d = 16, (0, 0, 0), 10 ** 9
    for idx, (pr, pg, pb) in _PALETTE:
        d = (pr - r) * (pr - r) + (pg - g) * (pg - g) + (pb - b) * (pb - b)
        if d < best_d:
            best_d, best_i, best_p = d, idx, (pr, pg, pb)
    return best_i, best_p


def _xterm256(r, g, b):
    """Color 256 más próximo (16-255)."""
    return _nearest(r, g, b)[0]


def _escapes(fg, bg):
    return f"\x1b[38;5;{fg};48;5;{bg}m"


def _escape_tc(fr, fgn, fb, br, bgn, bb):
    return f"\x1b[38;2;{fr};{fgn};{fb};48;2;{br};{bgn};{bb}m"


# máscaras 2×2 → carácter de cuadrante (bit0 arriba-izq, bit1 arriba-der,
# bit2 abajo-izq, bit3 abajo-der; bit=1 es «esta zona se pinta del color
# de fondo»; el carácter que se emite llena las zonas del PRIMER color).
_QUADS = {
    0b0000: " ",
    0b0001: "▘",   # arriba-izquierda
    0b0010: "▝",   # arriba-derecha
    0b0011: "▀",   # mitad superior
    0b0100: "▖",   # abajo-izquierda
    0b0101: "▌",   # columna izquierda (UL+LL)
    0b0110: "▞",   # UR+LL (diagonal antihoraria)
    0b0111: "▛",   # UL+UR+LL
    0b1000: "▗",   # abajo-derecha
    0b1001: "▚",   # UL+LR (diagonal horaria)
    0b1010: "▐",   # columna derecha (UR+LR)
    0b1011: "▜",   # UL+UR+LR
    0b1100: "▄",   # mitad inferior
    0b1101: "▙",   # UL+LL+LR
    0b1110: "▟",   # UR+LL+LR
    0b1111: "█",   # celda entera
}

# relleno vertical fraccionado (U+2581–2588): índice = octavos de la celda
# pintados del PRIMER PLANO, contando desde abajo.
_EIGHT = (" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")


def _dist2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _mean_of(vals):
    vals = list(vals)
    n = len(vals)
    return (sum(v[0] for v in vals) / n, sum(v[1] for v in vals) / n,
            sum(v[2] for v in vals) / n)


def _quant_cell8(pix, truecolor):
    """Reduce una celda (2 columnas × 8 filas de 4×2 px reales) al mejor
    (fg, bg, carácter, cols_por_píxel).

    `pix` viene en orden de filas (fila 0 = arriba), 16 tuplas.  Elige
    entre cuadrantes (4 zonas de 8 px) y octavos verticales (frontera
    cada 2 px) el que comete menos error cuadrático; devuelve el color
    pintado de cada píxel para difundir el error por píxel.
    """
    px = [(int(min(255., max(0., p[0]))), int(min(255., max(0., p[1]))),
           int(min(255., max(0., p[2])))) for p in pix]
    if not truecolor:
        px = [_nearest(*p)[1] for p in px]
    uniq = list(dict.fromkeys(px))
    if len(uniq) == 1:
        return uniq[0], uniq[0], " ", None          # celda plana
    best = None                                     # (cost, fg, bg, ch, cols)

    # familia cuadrantes: 4 grupos (UL UR LL LR), medios de 4 filas×1 col
    gm = (_mean_of(px[i] for i in range(16)
                   if (i // 2) < 4 and i % 2 == 0),          # UL
          _mean_of(px[i] for i in range(16)
                   if (i // 2) < 4 and i % 2 == 1),          # UR
          _mean_of(px[i] for i in range(16)
                   if (i // 2) >= 4 and i % 2 == 0),         # LL
          _mean_of(px[i] for i in range(16)
                   if (i // 2) >= 4 and i % 2 == 1))         # LR
    gu = list(dict.fromkeys(gm))
    for i in range(len(gu)):
        for j in range(i + 1, len(gu)):
            ca, cb = gu[i], gu[j]
            shown_g = [cb if _dist2(gm[k], cb) < _dist2(gm[k], ca)
                       else ca for k in range(4)]
            cost = 0.0
            cols = []
            for r in range(8):
                k = 0 if r < 4 else 2
                for c in range(2):
                    tgt = shown_g[k + c]
                    cost += _dist2(px[r * 2 + c], tgt)
                    cols.append(tgt)
            if best is None or cost < best[0]:
                bits = int("".join("1" if shown_g[k] == cb else "0"
                                   for k in range(3, -1, -1)), 2)
                best = (cost, ca, cb, _QUADS[(~bits) & 0b1111], cols, "q")

    # familia octavos: frontera horizontal cada 2 px (8 filas de 1 octavo)
    rm = [_mean_of((px[r * 2], px[r * 2 + 1])) for r in range(8)]
    for b in range(1, 8):
        ta = _mean_of(rm[:b])            # color de la parte superior
        tb = _mean_of(rm[b:])            # color de la parte inferior
        cost = 0.0
        cols = []
        for r in range(8):
            tgt = tb if r >= b else ta
            for c in range(2):
                cost += _dist2(px[r * 2 + c], tgt)
                cols.append(tgt)
        if best is None or cost < best[0]:
            best = (cost, tb, ta, _EIGHT[8 - b], cols, "e")

    _cost, fg, bg, ch, cols, _kind = best
    if truecolor:
        return (int(fg[0]), int(fg[1]), int(fg[2])), \
               (int(bg[0]), int(bg[1]), int(bg[2])), ch, cols
    fga = _nearest(*fg)[1]
    bga = _nearest(*bg)[1]
    return fga, bga, ch, cols


def _crop_source(path):
    """Carga y recorta una miniatura: quita bandas negras del letterbox.

    Devuelve la imagen RGB recortada de contenido (sin ajustar aspecto)."""
    with Image.open(path) as im_src:
        src = im_src.convert("RGB")
    sw, sh = src.size
    if sh >= 24:
        strip = src.resize((1, sh), Image.BOX)
        rows = [p[0] for p in list(strip.getdata())]
        lo = 0
        while lo < sh * 18 // 100 and rows[lo] < 14:
            lo += 1
        hi = sh - 1
        while hi > sh * 82 // 100 and rows[hi] < 14:
            hi -= 1
        if lo > sh * 3 // 100 or hi < sh * 97 // 100:
            src = src.crop((0, lo, sw, hi + 1))
    return src


def _png_for_kitty(img_path, w, h, out_path, px_w=0, px_h=0):
    """PNG para la colocación gráfica de kitty — COBIJA COMPLETA.

    La portada sale ENTERA, sin recortes: cuando su aspecto no es el de
    la caja, los huecos se rellenan con la propia portada difuminada y
    algo atenuada (estilo reproductor moderno).  Solo con aspecto casi
    idéntico se mantiene el ajuste directo (evita barras de 2-3 px).
    Con `px_w/px_h` (tamaño físico de la caja) el PNG va a resolución
    1:1 exacta — kitty no reescala.  Sin ellos, 8·w × 16·h px."""
    src = _crop_source(img_path)
    sw, sh = src.size
    tar = (px_w / float(px_h) if (px_w and px_h)
           else w / max(1, 2 * h))
    sar = sw / max(1, sh)
    tw, th = ((max(1, px_w), max(1, px_h)) if (px_w and px_h)
              else (8 * w, 16 * h))
    same = abs(sar - tar) <= 0.06 * max(1.0, tar)
    if same:                          # casi mismo aspecto: ajuste directo
        im = src.resize((tw, th), Image.LANCZOS)
    else:
        try:
            from PIL import ImageEnhance
        except Exception:
            ImageEnhance = None
        bg = src.resize((tw, th), Image.LANCZOS)   # llena toda la caja
        if ImageEnhance:
            try:                      # los huecos → bordes difuminados
                bg = bg.filter(ImageFilter.GaussianBlur(
                    max(2.0, tw / 40.0)))
                bg = ImageEnhance.Brightness(bg).enhance(0.82)
            except Exception:
                pass
        scale = min(tw / sw, th / sh)
        fgw = max(1, int(sw * scale + 0.5))
        fgh = max(1, int(sh * scale + 0.5))
        fg = src.resize((fgw, fgh), Image.LANCZOS)
        im = bg
        im.paste(fg, ((tw - fgw) // 2, (th - fgh) // 2))
    if tw > sw * 1.05 or th > sh * 1.05:
        try:                              # fuente por debajo del destino:
            im = im.filter(               # recupera el filo al agrandar
                ImageFilter.UnsharpMask(1.1, 60, 2))
        except Exception:
            pass
    tmp = out_path + ".part"
    with open(tmp, "wb") as fh:
        im.save(fh, "PNG", compress_level=8)
    os.replace(tmp, out_path)
    return out_path


def _render_pil(path, w, h):
    """Lista de líneas ANSI: w celdas × h filas de cuadrantes/octavos.

    Recorta el contenido real de la miniatura (bandas negras del
    letterbox), centra al aspecto físico de la región (w celdas de 8 px ×
    h celdas de 16 px → w/(2h)), reescala a 2×8 píxeles por celda con
    promedio de área y aplica la nitidez previa.
    """
    src = _crop_source(path)
    sw, sh = src.size
    # --- recorte centrado al aspecto físico de la región (w : 2h) ---
    tar = w / max(1, 2 * h)
    sar = sw / max(1, sh)
    if sar > tar:                     # fuente más ancha → recorte lateral
        cw = int(sh * tar + 0.5)
        x0 = (sw - cw) // 2
        src = src.crop((x0, 0, x0 + cw, sh))
    elif sar < tar:                   # fuente más alta → recorte vertical
        ch = int(sw / tar + 0.5)
        ch = min(sh, ch)
        y0 = (sh - ch) * 45 // 100
        src = src.crop((0, y0, sw, y0 + ch))
    tw, th = w * 2, h * 8
    im = src.resize((tw, th), Image.BOX)      # promedio de área: sin halos
    try:
        im = im.filter(ImageFilter.UnsharpMask(1.2, 70, 2))
    except Exception:
        pass
    px = list(im.getdata())
    tc = _truecolor()
    # mapa de error por píxel (difusión Floyd–Steinberg entre celdas)
    err = [[[0.0, 0.0, 0.0] for _ in range(tw)] for _ in range(th)]
    out = []
    for cy in range(h):
        line = []
        for cx in range(w):
            x0, y0 = cx * 2, cy * 8
            pix = [px[(y0 + r) * tw + x0 + c] for r in range(8)
                   for c in range(2)]
            adj = []
            for r in range(8):
                for c in range(2):
                    p = pix[r * 2 + c]
                    e = err[y0 + r][x0 + c]
                    adj.append((min(255., max(0., p[0] + e[0])),
                                min(255., max(0., p[1] + e[1])),
                                min(255., max(0., p[2] + e[2]))))
            fg, bg, glyph, cols = _quant_cell8(adj, tc)
            if tc:
                esc = _escape_tc(fg[0], fg[1], fg[2], bg[0], bg[1], bg[2])
            else:
                esc = _escapes(_nearest(*fg)[0], _nearest(*bg)[0])
            line.append(esc + glyph)
            # difusión del error de cada píxel a sus vecinos (16 px)
            if cols is None:
                continue
            for r in range(8):
                for c in range(2):
                    x, y = x0 + c, y0 + r
                    p = adj[r * 2 + c]
                    tgt = cols[r * 2 + c]
                    de = (p[0] - tgt[0], p[1] - tgt[1], p[2] - tgt[2])
                    if x + 1 < tw:
                        ne = err[y][x + 1]
                        ne[0] += de[0] * 7 / 16
                        ne[1] += de[1] * 7 / 16
                        ne[2] += de[2] * 7 / 16
                    if y + 1 < th:
                        if x >= 1:
                            nb = err[y + 1][x - 1]
                            nb[0] += de[0] * 3 / 16
                            nb[1] += de[1] * 3 / 16
                            nb[2] += de[2] * 3 / 16
                        nb = err[y + 1][x]
                        nb[0] += de[0] * 5 / 16
                        nb[1] += de[1] * 5 / 16
                        nb[2] += de[2] * 5 / 16
                        if x + 1 < tw:
                            nb = err[y + 1][x + 1]
                            nb[0] += de[0] * 1 / 16
                            nb[1] += de[1] * 1 / 16
                            nb[2] += de[2] * 1 / 16
        line.append("\x1b[0m")
        out.append("".join(line))
    return out


def _decorative(title, w, h):
    """Sin PIL: panel morado/fucsia con la inicial (mismo estilo de la TUI)."""
    ch = (title or "?").strip()[:1].upper() or "?"
    out = []
    for row in range(h):
        fade = max(0, h - 2 - row) * (156 // max(1, h))
        line = []
        for col in range(w):
            if 2 <= row < h - 2 and (w // 2 - 1) <= col <= (w // 2):
                line.append("\x1b[38;5;205m" + ch)
            else:
                line.append(f"\x1b[38;5;{max(53, 141 - fade)}m▄")
        line.append("\x1b[0m")
        out.append("".join(line))
    return out


def url2art(urls, w, h, cache_dir, title=""):
    """Candidata de imágenes → líneas ANSI (bloqueante; usar asíncrono).

    Descarga la primera URL que exista (404 → siguiente candidata) y
    devuelve el arte cacheado en disco si ya estaba renderizado.
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in (urls or []) if u]
    if not urls or not w or not h:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    for url in urls:
        img_path = os.path.join(cache_dir, _key(url) + ".img")
        if not os.path.exists(img_path):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = resp.read(6 * 1024 * 1024)
                tmp = img_path + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, img_path)
            except Exception:
                continue
        suffix = "tm" if _truecolor() else "m"
        ansi_path = os.path.join(
            cache_dir, f"{_key(url)}_{w}x{h}_{suffix}.ansi")
        if os.path.exists(ansi_path):
            try:
                with open(ansi_path, "r", encoding="utf-8") as fh:
                    lines = fh.read().split("\n")
                if len(lines) == h:
                    return lines
            except OSError:
                pass
        if _PIL:
            try:
                lines = _render_pil(img_path, w, h)
                tmp = ansi_path + ".part"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
                os.replace(tmp, ansi_path)
                return lines
            except Exception:
                continue
        break                    # sin PIL no hay más que probar
    return _decorative(title, w, h)


class CoverStore:
    """Caché en memoria de portadas; descarga en hilo sin bloquear la UI."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self._mem = {}                 # (video_id, w, h) → [líneas]
        self._pngs = {}                # (video_id, w, h, pw, ph) → ruta
        self._imgsz = {}               # ruta .img → (w, h) memoizadas
        self._busy = set()
        self._pngbusy = set()
        self._lock = threading.Lock()

    def get(self, video_id, w, h, title="", fallback=None):
        """Devuelve las líneas listas o None (descarga en segundo plano)."""
        key = (video_id, w, h)
        with self._lock:
            art = self._mem.get(key)
            if art:
                return art
        urls = thumb_urls_for(video_id, fallback)
        if not urls:
            with self._lock:
                self._mem[key] = _decorative(title, w, h)
            return self._mem.get(key)
        if key not in self._busy:
            self._busy.add(key)

            def work():
                try:
                    lines = url2art(urls, w, h, self.cache_dir, title)
                except Exception:
                    lines = _decorative(title, w, h)
                with self._lock:
                    self._mem[key] = lines or _decorative(title, w, h)
                try:
                    self._busy.discard(key)
                except KeyError:
                    pass

            threading.Thread(target=work, name=f"cover-{video_id}",
                             daemon=True).start()
        return None

    def _img_size(self, path):
        """(w, h) del .img en caché, memoizado (lee solo la cabecera)."""
        with self._lock:
            hit = self._imgsz.get(path)
            if hit is not None:
                return hit
        size = None
        try:
            with Image.open(path) as sx:
                size = sx.size
        except Exception:
            size = (0, 0)
        with self._lock:
            self._imgsz[path] = size
        return size

    def _queue_fetch(self, pkey, urls, video_id):
        """Descarga la escalera completa (la primera que exista) en hilo."""
        if pkey in self._pngbusy:
            return
        self._pngbusy.add(pkey)

        def work():
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                for url in urls:             # en orden de calidad
                    img_path = os.path.join(
                        self.cache_dir, _key(url) + ".img")
                    if os.path.isfile(img_path):
                        continue
                    try:
                        req = urllib.request.Request(
                            url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=12) as resp:
                            data = resp.read(6 * 1024 * 1024)
                    except Exception:
                        continue             # 404 u orden → siguiente
                    tmp = img_path + ".part"
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                    os.replace(tmp, img_path)
                    break                    # mejor candidata servida
            except Exception:
                pass
            with self._lock:
                self._pngbusy.discard(pkey)

        threading.Thread(target=work, name=f"coverpng-{video_id}",
                         daemon=True).start()

    def get_png(self, video_id, w, h, title="", fallback=None,
                px_w=0, px_h=0):
        """PNG para kitty (o None mientras).  Con px_w/px_h reales el
        PNG va a resolución física 1:1; la caché guarda por tamaño y
        POR FUENTE (un PNG hecho desde una miniatura de 120 px nunca se
        reutiliza como si fuera nítido)."""
        if not _PIL:
            return None
        pkey = (video_id, w, h, px_w, px_h)
        with self._lock:
            ready = self._pngs.get(pkey)
            if ready:
                return ready
        urls = thumb_urls_for(video_id, fallback)
        if not urls:
            return None
        want = int(px_w) if px_w else 8 * int(w)
        good = None          # (url, img) digna: ancho ≥ el del destino
        first = None         # primera en caché (puede ser indigna)
        for url in urls:
            cand = os.path.join(self.cache_dir, _key(url) + ".img")
            if not os.path.isfile(cand):
                continue
            if first is None:
                first = (url, cand)
            if self._img_size(cand)[0] >= want:
                good = (url, cand)
                break
        if not good:
            # solo queda en caché una fuente por debajo del destino
            # (p. ej. 120 px): pedir la escalera entera en vez de
            # asentar el panel con una imagen difuminada
            self._queue_fetch(pkey, urls, video_id)
            return None
        url, imgpth = good
        sz = (f"{px_w}x{px_h}" if (px_w and px_h) else f"{8 * w}x{16 * h}")
        out = os.path.join(
            self.cache_dir, f"{_key(url)}_{sz}.k2.png")
        if os.path.isfile(out):
            with self._lock:
                self._pngs[pkey] = out
            return out
        if pkey not in self._pngbusy:
            self._pngbusy.add(pkey)

            def render(png=imgpth, out=out):
                try:
                    _png_for_kitty(png, w, h, out, px_w, px_h)
                except Exception:
                    pass
                with self._lock:
                    if os.path.isfile(out):
                        self._pngs[pkey] = out
                    self._pngbusy.discard(pkey)

            threading.Thread(target=render, name=f"kpng-{video_id}",
                             daemon=True).start()
        return None

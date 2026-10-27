# -*- coding: utf-8 -*-
"""t32 — portadas a resolución digna (panel kitty).

Comprueba: ①la reescritura de portadas YTM a tamaño grande
(lh3/ggpht =w1200-h1200 / =s1200), ②que get_png ya NO se asienta con
una fuente pequeña (120 px) si queda escalera mejor por pedir, ③que la
selección elige fuente ≥ destino y renderiza PNG a pasos exactos con
nombre por fuente, ④la descarga en escalera con red simulada y ⑤que
los ficheros corruptos no rompen nada.
"""
import io
import os
import random
import shutil
import sys
import threading
import time
import urllib.request

for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    os.environ.pop(var, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"

TMP = "/tmp/opencode/musicterminal/t32"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, "covers"), exist_ok=True)

sys.path.insert(0, "/home/pedro/music-terminal")
from music_terminal import cover as cv                     # noqa: E402
from music_terminal.cover import (CoverStore, _boost_1200,  # noqa: E402
                                  _key, thumb_urls_for)
from PIL import Image                                      # noqa: E402

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


def _noise_rgb(size):
    rnd = random.Random(42)
    im = Image.new("RGB", size)
    px = im.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256),
                        rnd.randrange(256))
    return im


# ── ①boost de portadas YTM a tamaño grande ──────────────────────────── #
b1 = _boost_1200("https://lh3.googleusercontent.com/FcC=w544-h544-l90-rj")
check(b1 == "https://lh3.googleusercontent.com/FcC=w1200-h1200-l90-rj",
      f"=wN-hN reescrito a 1200: {b1}")
b2 = _boost_1200("https://yt3.ggpht.com/AbC=s288-c-k-c0x00ffffff-no-rj")
check(b2 == "https://yt3.ggpht.com/AbC=s1200-c-k-c0x00ffffff-no-rj",
      f"=sN reescrito a 1200: {b2}")
b3 = _boost_1200("https://i.ytimg.com/vi/abc/maxresdefault.jpg")
check(b3 is None, "i.ytimg no se toca (su maxres ya es el rango alto)")
b4 = _boost_1200("")
check(b4 is None, "URL vacía → None sin ruido")

U_SMALL = "https://lh3.googleusercontent.com/FcC=w544-h544-l90-rj"
ls = thumb_urls_for("vidTEST", U_SMALL)
check(ls[0].endswith("=w1200-h1200-l90-rj"),
      f"la variante boosteada va la primera: {ls[0]}")
check(U_SMALL in ls, "la original queda como respaldo en la escalera")
check(any("/maxresdefault.jpg" in u for u in ls),
      "la escalera i.ytimg sigue detrás")
check(ls == list(dict.fromkeys(ls)), "sin duplicados en la escalera")

# ── ②get_png NO se asienta con una fuente pequeña ───────────────────── #
DIR = os.path.join(TMP, "covers")
st = CoverStore(DIR)
img_small = os.path.join(DIR, _key(U_SMALL) + ".img")
_noise_rgb((120, 120)).save(img_small, "JPEG", quality=90)

out = st.get_png("vidTEST", 61, 22, px_w=610, px_h=462)
check(out is None, "con solo 120 px en caché no difumina: pide mejor")
deadline = time.time() + 10.0       # bajo carga la escalera puede ir lenta
while time.time() < deadline:      # la escalera falla (sin red) → libre
    with st._lock:
        if not st._pngbusy:
            break
    time.sleep(0.05)
with st._lock:
    busy_atrapado = bool(st._pngbusy)
check(not busy_atrapado, "el intento de escalera libera la señal busy")

# ── ③fuente digna → PNG exacto, nombrado POR fuente ─────────────────── #
U_MAXRES = "https://i.ytimg.com/vi/vidTEST/maxresdefault.jpg"
img_big = os.path.join(DIR, _key(U_MAXRES) + ".img")
_noise_rgb((1280, 720)).save(img_big, "JPEG", quality=90)
png = st.get_png("vidTEST", 61, 22, px_w=610, px_h=462)
deadline = time.time() + 5.0
while png is None and time.time() < deadline:
    time.sleep(0.05)
    png = st.get_png("vidTEST", 61, 22, px_w=610, px_h=462)
check(png is not None, "con fuente 1280 hay PNG a los pocos pases")
if png:
    check(_key(U_MAXRES) in os.path.basename(png),
          f"el PNG se nombra por su FUENTE: {os.path.basename(png)}")
    deadline = time.time() + 4.0
    done = None
    while time.time() < deadline:
        try:
            with Image.open(png) as rz:
                done = rz.size
            break
        except Exception:
            time.sleep(0.05)
    check(done == (610, 462), f"dimensiones exactas del destino: {done}")

# el 'primero-en-caché' pequeño NUNCA debe colarse como buena fuente
check(hasattr(st, "_img_size"), "helper de tamaño presente en el store")

# ── ④descarga en escalera con red simulada local ────────────────────── #
DIR2 = os.path.join(TMP, "covers2")
os.makedirs(DIR2, exist_ok=True)
big2 = os.path.join(TMP, "big2.jpg")
_noise_rgb((1280, 720)).save(big2, "JPEG", quality=90)
data_big = open(big2, "rb").read()
served = []
real_urlopen = urllib.request.urlopen


class _Resp:
    def __init__(self, data):
        self._d = data
        self.status = 200

    def read(self, n=-1):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=0):
    served.append(getattr(req, "full_url", req))
    if served and "/mqdefault.jpg" in served[-1]:
        raise IOError("404 simulado solo para mq")   # la escalera salta
    return _Resp(data_big)


ls_stuff = thumb_urls_for("vidDOWN", None)   # sin fallback
print("escalera de vidDOWN:", [u.split("/")[-1] for u in ls_stuff])
urllib.request.urlopen = fake_urlopen
try:
    art = cv.url2art(ls_stuff, 4, 4, DIR2, "T")
finally:
    urllib.request.urlopen = real_urlopen
check("vidDOWN/maxresdefault.jpg" in (served[0] if served else ""),
      f"la escalera empieza por maxres: {served[:2]}")
newpath = os.path.join(DIR2, _key(ls_stuff[0]) + ".img")
check(os.path.isfile(newpath), "la mejor candidata bajó a .img")
check(art is not None and len(art) == 4,
      f"render ANSI a 4x4 con la fuente grande ({len(art) if art else 0})")

# ── ⑤.img corrupto no rompe nada ────────────────────────────────────── #
DIR3 = os.path.join(TMP, "covers3")
st3 = CoverStore(DIR3)
os.makedirs(DIR3, exist_ok=True)
bad = os.path.join(DIR3, _key("https://i.ytimg.com/vi/vidBAD/maxresdefault.jpg")
                   + ".img")
with open(bad, "wb") as fh:
    fh.write(b"NOPENOPENOPE")
sz = st3._img_size(bad)              # el store lleva su propio lock
check(sz == (0, 0), f"tamaño de fichero corrupto → {sz}")
try:
    png3 = st3.get_png("vidBAD", 10, 6, px_w=120, px_h=90)
    ok3 = (png3 is None)
except Exception as e:
    ok3 = False
    check(False, f"get_png con corrupto reventó: {e}")
check(ok3, "con fuente corrupta pide mejor (None) sin reventar")

# ── ⑥estirado por debajo del destino lleva nitidez ──────────────────── #
tiny = os.path.join(TMP, "tiny.jpg")
_noise_rgb((96, 96)).save(tiny, "JPEG", quality=90)
outp = os.path.join(TMP, "ups.png")
cv._png_for_kitty(tiny, 40, 20, outp, px_w=300, px_h=300)
with Image.open(outp) as up:
    check(up.size == (300, 300), f"PNG estirado exacto: {up.size}")

# ── ⑦COBIJA COMPLETA: nada cortado ──────────────────────────────────── #
# Cuadrada 300×300 → caja 610×462 (aspecto 1.32): la portada entera
# centrada y los huecos del fondo difuminado.
srcq = os.path.join(TMP, "sq.jpg")
q = _noise_rgb((300, 300))
pxq = q.load()
for y in range(300):
    for x in range(300):
        if x < 100:
            pxq[x, y] = (220, 20, 20)
        elif x > 200:
            pxq[x, y] = (20, 20, 220)
        else:
            pxq[x, y] = (250, 250, 250)
q.save(srcq, "JPEG", quality=95)
outq = os.path.join(TMP, "sq_out.png")
cv._png_for_kitty(srcq, 61, 22, outq, px_w=610, px_h=462)
with Image.open(outq) as oq:
    check(oq.size == (610, 462), f"cuadrada → caja exacta: {oq.size}")
    pc = oq.getpixel((305, 6))            # franja blanca arriba…
    pc2 = oq.getpixel((305, 455))         # …y abajo (sin recorte vertical)
    check(pc[0] > 180 and pc[1] > 180 and pc[2] > 180 and
          pc2[0] > 180 and pc2[1] > 180 and pc2[2] > 180,
          f"franja central entera de arriba a abajo: {pc}/{pc2}")
    pb = oq.getpixel((8, 231))            # hueco izquierdo = rojo difuminado
    check(pb[0] > 120 and pb[0] > pb[1] and pb[0] > pb[2],
          f"fondo de hueco lateral con el color de la portada: {pb}")
    pf = oq.getpixel((85, 231))           # borde rojo de la portada ya dentro
    check(pf[0] > 120 and pf[0] > pf[1] and pf[0] > pf[2],
          f"la portada completa a la vista (rojo incluido): {pf}")

# 16:9 640×360 → caja 610×462: arriba y abajo visibles (antes ~26% cortado)
srcw = os.path.join(TMP, "wide.jpg")
wim = _noise_rgb((640, 360))
pxw_ = wim.load()
for y in range(min(14, 360)):
    for x in range(640):
        pxw_[x, y] = (255, 150, 10)       # naranja arriba
for y in range(max(0, 360 - 14), 360):
    for x in range(640):
        pxw_[x, y] = (90, 40, 150)        # púrpura abajo
wim.save(srcw, "JPEG", quality=95)
outw = os.path.join(TMP, "wide_out.png")
cv._png_for_kitty(srcw, 61, 22, outw, px_w=610, px_h=462)
with Image.open(outw) as ow:
    check(ow.size == (610, 462), f"16:9 → caja exacta: {ow.size}")
    pt = ow.getpixel((305, 70))           # franja naranja superior dentro
    check(pt[0] > 170 and pt[1] > 90 and pt[2] < 90,
          f"el borde SUPERIOR de la portada visible: {pt}")
    pbw = ow.getpixel((305, 440))         # franja púrpura inferior dentro
    check(pbw[0] < 150 and pbw[2] > 100,
          f"el borde INFERIOR de la portada visible: {pbw}")

# aspecto casi idéntico → ajuste directo, sin barras de 2 px
srcs = os.path.join(TMP, "same.jpg")
_noise_rgb((480, 360)).save(srcs, "JPEG", quality=95)
outs = os.path.join(TMP, "same_out.png")
cv._png_for_kitty(srcs, 61, 22, outs, px_w=610, px_h=462)
with Image.open(outs) as os_:
    check(os_.size == (610, 462), f"aspecto casi igual → directa: {os_.size}")

print("\n—", "TODOS OK" if not FAIL else f"{len(FAIL)} FALLOS", "—")
for f_ in FAIL:
    print("  ✗", f_)
sys.exit(1 if FAIL else 0)

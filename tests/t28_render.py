# -*- coding: utf-8 -*-
"""t28 — regresión del render ANSI (blitter mixto) tras el refactor.

Comprueba el render con PIL real: tamaños de líneas, celdas, errores
físicos y la ruta Completa de CoverStore.get con una .img ya sembrada,
más el panel de la app en modo NO-kitty (glifos y placeholder).
"""
import os
import shutil
import sys
import time

for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    os.environ.pop(var, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"

TMP = "/tmp/opencode/musicterminal/t28"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["MUSICTERMINAL_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["MUSICTERMINAL_CONFIG_DIR"] = os.path.join(TMP, "config")

sys.path.insert(0, "/home/pedro/music-terminal")
from music_terminal import cover, tui                       # noqa: E402
from music_terminal.tui import C                            # noqa: E402

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------------- #
# imagen fuente sintética: color plano + franja + degradado suave
# ---------------------------------------------------------------------- #
from PIL import Image                                       # noqa: E402
src = os.path.join(TMP, "src.png")
w0, h0 = 640, 480
im = Image.new("RGB", (w0, h0))
px = im.load()
BAND = 40
for y in range(h0):
    for x in range(w0):
        if y < BAND or y >= h0 - BAND:
            px[x, y] = (0, 0, 0)                     # letterbox
        elif x < w0 // 3:
            px[x, y] = ((x * 7) % 256, (x * 3) % 256,
                        (x * 5) % 256)               # franja viva
        else:
            px[x, y] = (140, 20 + (y * 60) // h0, 200)   # degradado
im.save(src, "PNG")

W, H = 58, 22
t = time.time()
lines = cover._render_pil(src, W, H)
dt = (time.time() - t) * 1000.0
check(len(lines) == H, f"líneas renderizadas: {len(lines)} == {H}")

import re                                                   # noqa: E402
bad = []
for ln in lines:
    vid = C.visible_len(ln)
    if vid != W:
        bad.append(vid)
check(not bad, f"ancho visible por línea ({W}); anomalías={bad[:3]}")

qchars = [ch for ch in set(cover._QUADS.values())]
eights = [ch for ch in set(cover._EIGHT) - {""}]
j = "".join(lines)
nq = sum(j.count(ch) for ch in qchars)
ne = sum(j.count(ch) for ch in ("▁", "▂", "▃", "▅", "▆", "▇"))
check(nq > 0, f"cuadrantes presentes ({nq})")
check(ne > 0, f"octavos presentes ({ne})")
print(f"  (mezcla: {nq} cuadrantes + {ne} octavos)")
print(f"  (render de {W}×{H}: {dt:.0f} ms)")

# duplicidad determinista (misma entrada → misma salida)
lines2 = cover._render_pil(src, W, H)
check(lines == lines2, "render determinista")

# familia de cuadrantes: franjas verticales duras (QA obligatoriamente gana)
src2 = os.path.join(TMP, "franja.png")
im2 = Image.new("RGB", (w0, h0))
px2 = im2.load()
for y in range(h0):
    for x in range(w0):
        px2[x, y] = (255, 40, 40) if (x // 40) % 2 else (30, 30, 90)
im2.save(src2, "PNG")
lines3 = cover._render_pil(src2, W, H)
j3 = "".join(lines3)
q2 = sum(j3.count(ch) for ch in qchars)
check(q2 > 100, f"cuadrantes con franjas verticales ({q2})")

# ---------------------------------------------------------------------- #
# CoverStore.get con la .img sembrada (hilo + caché .ansi)
# ---------------------------------------------------------------------- #
covers_dir = os.path.join(TMP, "data", "covers")
os.makedirs(covers_dir, exist_ok=True)
vid = "dQw4w9WgXcQ"
url0 = cover.thumb_urls_for(vid)[0]
with open(src, "rb") as f0:
    data0 = f0.read()
with open(os.path.join(covers_dir, cover._key(url0) + ".img"), "wb") as fh:
    fh.write(data0)
store = cover.CoverStore(covers_dir)
r1 = store.get(vid, W, H, "X")
check(r1 is None, "get: None mientras renderiza en hilo")
deadline, r2 = time.time() + 4.0, None
while time.time() < deadline:
    r2 = store.get(vid, W, H, "X")
    if r2:
        break
    time.sleep(0.02)
check(bool(r2) and all(C.visible_len(x) == W for x in r2),
      "get: arte listo desde el hilo")
ansis = [f for f in os.listdir(covers_dir) if f.endswith(".ansi")]
check(len(ansis) == 1, f"cacheado en disco: {ansis}")
r3 = store.get(vid, W, H, "X")
check(r3 == r2, "cache hit en memoria (sin renderizar otra vez)")

# ---------------------------------------------------------------------- #
# panel real en modo NO-kitty: glifos en el área
# ---------------------------------------------------------------------- #
from music_terminal import app as appmod                     # noqa: E402
from music_terminal.database import Library                  # noqa: E402

a = appmod.App(None, Library(os.path.join(TMP, "data", "t28.db")), {})
check(a._kg_ok is False, "en xterm sin variables kitty: sin panel gráfico")


class Qshim:
    items = []
    mode = "none"
    shuffle = False

    @staticmethod
    def current():
        return {"id": vid, "title": "Canción de prueba", "artists": [],
                "thumb": None, "duration": 0, "like": False}


a.q = Qshim
a.covers = store                                          # .img ya en caché
Wx, Hx = 140, 40
pw = a._panel_width(Wx)
ix, iw, top, bot = Wx - pw + 1, pw - 2, 2, 38
art_h = min(22, max(4, bot - top - 10))
# sembrar en la caché de render de la tienda (mismo tamaño del panel)
a.scr = tui.Screen.__new__(tui.Screen)
buf = []
a.scr.write = lambda s: buf.append(s)
a.scr.flush = lambda: None
a.scr.move = lambda y, x: buf.append(f"\x1b[{y+1};{x+1}H")
a.scr.size = (Wx, Hx)
dead, art = time.time() + 1.0, None
while time.time() < dead and not art:
    art = a.covers.get(vid, iw, art_h, "X")
    time.sleep(0.02)
if not art:
    # tamaño del panel ≠ 58×22: sembrar el .ansi correspondiente
    suffix = "tm" if cover._truecolor() else "m"
    ansi_path = os.path.join(
        covers_dir,
        f"{cover._key(cover.thumb_urls_for(vid)[0])}_{iw}x{art_h}_{suffix}.ansi")
    src_lines = cover._render_pil(src, iw, art_h)
    with open(ansi_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(src_lines))
    art = a.covers.get(vid, iw, art_h, "X")
check(bool(art), f"arte ANSI listo (w={iw}, h={art_h})")

buf.clear()
a.draw_panel(Wx, Hx)
s = "".join(buf)
blocks = re.findall(r"[\u2590-\u259F\u2581-\u2588]", s)
check(len(blocks) > 100, f"panel con bloques portada ({len(blocks)})")
check("Reproductor" in s, "panel del reproductor dibujado")
# placeholder (sin arte aún) cuando la tienda no tiene: nueva tienda vacía
a.covers = cover.CoverStore(os.path.join(TMP, "data", "covers_vacio"))
buf.clear()
a.draw_panel(Wx, Hx)
s2 = "".join(buf)
check("░" in s2 and "obteniendo portada" in s2,
      "placeholder ░ + obtendo portada cuando no hay arte")

print("\n—", "TODOS OK" if not FAIL else f"{len(FAIL)} FALLOS", "—")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)

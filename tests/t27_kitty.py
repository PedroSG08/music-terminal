# -*- coding: utf-8 -*-
"""t27 — verificación del canal gráfico de kitty (music-terminal 1.4.1).

Comprueba: detección kitty, troceado de escapes, kitty_show (ruta t=f y
recepción directa por SSH), kitty_delete, _png_for_kitty con imagen real,
CoverStore.get_png (hilo + caché de disco) y la integración en App
(colocación, cambio de página, borrados, placeholders).
"""
import base64
import os
import re
import shutil
import sys
import time

# entorno determinista: sin kitty, sin SSH
for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    os.environ.pop(var, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"

TMP = "/tmp/opencode/musicterminal/t27"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
os.environ["MUSICTERMINAL_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["MUSICTERMINAL_CONFIG_DIR"] = os.path.join(TMP, "config")

sys.path.insert(0, "/home/pedro/music-terminal")
from music_terminal import cover, tui                      # noqa: E402

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


class FakeScr:
    def __init__(self):
        self.buf = []
        self.size = (100, 30)

    def move(self, y, x):
        self.write(f"\x1b[{y + 1};{x + 1}H")

    def write(self, s):
        self.buf.append(s)

    def flush(self):
        pass


# ---------------------------------------------------------------------- #
# 1 · detección kitty
# ---------------------------------------------------------------------- #
os.environ["KITTY_WINDOW_ID"] = "3"
check(tui.kitty_ok(), "detección por KITTY_WINDOW_ID")
os.environ.pop("KITTY_WINDOW_ID")
os.environ["KITTY_PID"] = "99"
check(tui.kitty_ok(), "detección por KITTY_PID")
os.environ.pop("KITTY_PID")
check(not tui.kitty_ok(), "TERM xterm sin variables kitty → sin soporte")
os.environ["TERM"] = "xterm-kitty"
check(tui.kitty_ok(), "TERM=xterm-kitty → soporte")
os.environ["TERM"] = "xterm-256color"

# ---------------------------------------------------------------------- #
# 2 · troceado de la carga base64 (m=1/0)
# ---------------------------------------------------------------------- #
b64small = base64.b64encode(b"a" * 300).decode("ascii")
frames = tui._dcs_frames("a=T,f=100,i=1", b64small)
check(len(frames) == 1 and frames[0] == f"\x1b_Ga=T,f=100,i=1;{b64small}\x1b\\",
      "carga pequeña: un único frame sin m=…")

b64big = base64.b64encode(os.urandom(6000)).decode("ascii")   # 8_000 chars
frames = tui._dcs_frames("a=T,f=100,i=2", b64big)
n_expected = (len(b64big) + 3999) // 4000
check(len(frames) == n_expected,
      f"troceado: {len(frames)} frames (esperado {n_expected})")
recon = b""
for i, fr in enumerate(frames):
    check(len(fr) <= 4096 + 40, f"frame {i}:≤ 4 KB por escape")
    m = re.search(r"\x1b_G[^;]*;([A-Za-z0-9+/=]*)\x1b\\", fr)
    check(m is not None, f"frame {i}: payload extraíble")
    recon += base64.b64decode(m.group(1))
    if i == 0:
        check("a=T,f=100,i=2,m=1;" in fr, "frame 0 lleva metadatos+m=1")
    elif i < len(frames) - 1:
        check(fr.startswith("\x1b_Gm=1;"), f"frame {i}: m=1 continuación")
    else:
        check(fr.startswith("\x1b_Gm=0;"), f"frame último: m=0")
check(recon == base64.b64decode(b64big), "payload recompuesto intacto")

# ---------------------------------------------------------------------- #
# 3 · kitty_show local (t=f: solo la ruta en el escape)
# ---------------------------------------------------------------------- #
png_path = os.path.join(TMP, "fake.png")
with open(png_path, "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n")
scr = FakeScr()
ok = tui.kitty_show(scr, png_path, 5, 1, 58, 22, at=(2, 94))
data = "".join(scr.buf)
exp = ("\x1b_Ga=t,f=100,t=f,i=5,q=2;"
       + base64.b64encode(png_path.encode()).decode("ascii") + "\x1b\\")
check(ok and exp in data, "t=f: transmite la ruta del archivo")
check("\x1b7\x1b[3;95H" + exp in data,
      "origen at=(2,94): ESC7 → mover a fila 3 col 95 → transmitir")
check("\x1b_Ga=p,i=5,p=1,C=1,q=2,c=58,r=22\x1b\\" in data,
      "a=p con C=1 y celdas c=58,r=22")
check(data.startswith("\x1b7") and data.endswith("\x1b8"),
      "cursor guardado/restaurado (ESC7/ESC8)")
check(len(data) < 400, f"escape local diminuto ({len(data)} B) → repintados baratos")

# ---------------------------------------------------------------------- #
# 4 · kitty_show remoto (SSH → bytes directos troceados)
# ---------------------------------------------------------------------- #
os.environ["SSH_CLIENT"] = "1.2.3.4 4000 5.6.7.8"
scr2 = FakeScr()
payload = os.urandom(9000)
with open(os.path.join(TMP, "big.bin"), "wb") as fh:
    fh.write(payload)
ok = tui.kitty_show(scr2, os.path.join(TMP, "big.bin"), 3, 1, 10, 5)
data2 = "".join(scr2.buf)
check(ok and "t=f" not in data2, "SSH: sin medio archivo")
chunks = re.findall(r";([A-Za-z0-9+/=]+)\x1b\\", data2)
recon = base64.b64decode("".join(chunks))
check(recon == payload, "SSH: payload por trozos recompuesto intacto")
check(data2.count("m=1") >= 2 and "\x1b_Gm=0;" in data2,
      "SSH: m=1…m=0 correctos")
check(all(len(fr) <= 4096 for fr in re.findall(r"\x1b_G.*?\x1b\\", data2)),
      "SSH: cada escape ≤ 4096 B")
os.environ.pop("SSH_CLIENT")

# ---------------------------------------------------------------------- #
# 5 · kitty_delete
# ---------------------------------------------------------------------- #
scr3 = FakeScr()
tui.kitty_delete(scr3, 9)
tui.kitty_delete(scr3)
d = "".join(scr3.buf)
check("\x1b_Ga=d,d=I,i=9,q=2\x1b\\" in d, "borrado por id (d=I,i=…)")
check("\x1b_Ga=d,d=A,q=2\x1b\\" in d, "borrado global (d=A)")

# ---------------------------------------------------------------------- #
# 6 · _png_for_kitty con imagen real
# ---------------------------------------------------------------------- #
from PIL import Image                                        # noqa: E402
src_path = os.path.join(TMP, "src.png")
w0, h0 = 1280, 720
bands_top, bands_bot = 120, 130
im = Image.new("RGB", (w0, h0))
im.putdata([(0, 0, 0) if (y < bands_top or y >= h0 - bands_bot)
            else (255, 40 + (y - bands_top) % 90, 180)
            for y in range(h0) for x in range(w0)])
im.save(src_path)
kpng = os.path.join(TMP, "out.k2.png")
out = cover._png_for_kitty(src_path, 58, 22, kpng)
check(out == kpng and os.path.isfile(out), "png de kitty generado")
with Image.open(kpng) as r:
    check(r.size == (8 * 58, 16 * 22), f"tamaño exacto {r.size} == (464, 352)")
    rgb = r.convert("RGB")
check(open(kpng, "rb").read(8) == b"\x89PNG\r\n\x1a\n", "cabecera PNG válida")
# sin bandas negras (el recorte las eliminó)
mn = min(rgb.getpixel((x, y))[0] for x in (0, 200, 400) for y in
         (0, 100, 300))
check(mn > 150, f"sin bandas negras residuales (min R={mn})")
size_kb = os.path.getsize(kpng) // 1024
check(size_kb < 500, f"tamaño razonable para el escape ({size_kb} KB)")

# ---------------------------------------------------------------------- #
# 7 · CoverStore.get_png (hilo + reuso de caché)
# ---------------------------------------------------------------------- #
covers_dir = os.path.join(TMP, "data", "covers")
os.makedirs(covers_dir, exist_ok=True)
vid = "dQw4w9WgXcQ"
url0 = cover.thumb_urls_for(vid)[0]
shutil.copy(src_path, os.path.join(covers_dir,
                                   cover._key(url0) + ".img"))
store = cover.CoverStore(covers_dir)
r1 = store.get_png(vid, 58, 22, "T")
check(r1 is None, "get_png: None mientras el hilo renderiza")
deadline = time.time() + 4.0
r2 = None
while time.time() < deadline:
    r2 = store.get_png(vid, 58, 22, "T")
    if r2:
        break
    time.sleep(0.02)
check(bool(r2) and os.path.isfile(r2),
      f"get_png listo en el hilo: {r2 and os.path.basename(r2)}")
check(r2.endswith("464x352.k2.png"),
      "nombre con la resolución estimada por defecto (8·58 × 16·22)")
r3 = store.get_png(vid, 58, 22, "T")
check(r3 == r2, "segunda llamada: instantánea desde la caché de memoria")

# ---------------------------------------------------------------------- #
# 8 · integración en App: colocación, cambio de página, borrados
# ---------------------------------------------------------------------- #
from music_terminal import app as appmod                     # noqa: E402
from music_terminal.database import Library                  # noqa: E402

a = appmod.App(None, Library(os.path.join(TMP, "data", "t27.db")), {})
a._kg_ok = True

class CurShim:
    items = []
    mode = "none"
    shuffle = False

    @staticmethod
    def current():
        return {"id": vid, "title": "Prueba", "artists": [],
                "thumb": None, "duration": 0, "like": False}

a.q = CurShim()
a.covers = type("CoversStub", (), {
    "get_png": staticmethod(lambda *ar, **kw: kpng),
    "get": staticmethod(lambda *ar, **kw: None),
})()
W, H = 140, 40
pw = a._panel_width(W)                     # min(64, max(32, 140//3)) = 46
iw = pw - 2                                 # 44
art_h = min(22, max(4, 38 - 10))            # 22 es art_h con bot-top=38

a.scr = FakeScr()
a.draw_panel(W, H)
s1 = "".join(a.scr.buf)
exp_path = ("\x1b_Ga=t,f=100,t=f,i=1,q=2;"
            + base64.b64encode(kpng.encode()).decode("ascii") + "\x1b\\")
# origen de Колocación: fil= top+1 = 3, col = ix+1 = 95
# origen de colocación: fila top+1 = 3, columna ix+1 = 96 (1-based)
check("\x1b7\x1b[3;96H" + exp_path in s1,
      "app: la imagen se coloca en el ORIGEN del área (fila 3, col 96)")
check(exp_path in s1, "app: transmite el PNG por la ruta (i=1)")
check(f"\x1b_Ga=p,i=1,p=1,C=1,q=2,c={iw},r={art_h}\x1b\\" in s1,
      f"app: colocado en celdas c={iw},r={art_h}")
check(a._kg_id == 1 and a._kg_state == (vid, iw, art_h),
      "app: estado registrado (id, estado)")

# repintado SIN cambio: no debe reenviarse
a.scr.buf = []
a.draw_panel(W, H)
s2 = "".join(a.scr.buf)
check("a=t," not in s2, "app: repintado idéntico no reenvía la imagen")

# cambio de página → borrado de la colocación
a.scr.buf = []
a.panel["page"] = "cola"
a.draw_panel(W, H)
s3 = "".join(a.scr.buf)
check("\x1b_Ga=d,d=I,i=1,q=2\x1b\\" in s3 and a._kg_id == 0,
      "app: al pasar a cola borra la imagen")
# vuelta a portada → nueva colocación con id distinto + fondo pintado
a.scr.buf = []
a.panel["page"] = "cover"
a.draw_panel(W, H)
s4 = "".join(a.scr.buf)
check(f"i=2" in s4 and f"\x1b_Ga=p,i=2,p=1,C=1,q=2,c={iw},r={art_h}\x1b\\"
      in s4, "app: regreso a portada con id nuevo (i=2)")
check("\x1b[0m" + " " * iw in s4, "app: celdas del área limpiadas antes")

# canción distinta → delete del antiguo + colocación del nuevo
vid2 = "abc123XYZ_-1"
a.covers = type("CoversStub2", (), {
    "get_png": staticmethod(
        lambda *ar, **kw: os.path.join(TMP, "fake.png")),
    "get": staticmethod(lambda *ar, **kw: None),
})()
a.q = type("Qshim2", (), {"current": staticmethod(
    lambda: {"id": vid2, "title": "Otra", "artists": [], "thumb": None,
             "duration": 0, "like": False})})()
a.scr.buf = []
a.draw_panel(W, H)
s5 = "".join(a.scr.buf)
check("\x1b_Ga=d,d=I,i=2,q=2\x1b\\" in s5, "app: canción nueva borra id 2")
check("\x1b_Ga=t,f=100,t=f,i=3," in s5, "app: canción nueva transmite i=3")
check(a._kg_state[0] == vid2, "app: estado actualizado al videoId nuevo")

# panel oculto (terminal estrecha) → borrado
a.scr.buf = []
a.draw_panel(80, 40)
s6 = "".join(a.scr.buf)
check((not s6 or "a=d," in s6) and a._kg_id == 0,
      "app: panel oculto → imagen eliminada")

# por SSH la app transmite bytes ¡pero con el truco t=f activado igual!
# (kitty_show decide; aquí solo comprobamos que la ruta ssh no rompe)
os.environ["SSH_CLIENT"] = "9.9.9.9 22 8.8.8.8"
a.covers = type("CoversStub3", (), {
    "get_png": staticmethod(lambda *ar, **kw: kpng),
    "get": staticmethod(lambda *ar, **kw: None),
})()
a.q = type("Qshim3", (), {"current": staticmethod(
    lambda: {"id": vid, "title": "T", "artists": [], "thumb": None,
             "duration": 0, "like": False})})()
a.scr.buf = []
a.draw_panel(W, H)
s7 = "".join(a.scr.buf)
escapes = re.findall(r"\x1b_G[^\x1b]*\x1b\\", s7)
check("t=f," not in s7 and any("a=t,f=100,i=" in e for e in escapes)
      and any("a=p,i=" in e and "c=44,r=22" in e for e in escapes)
      and all(len(e) <= 4096 for e in escapes),
      "SSH: la app envía el PNG por bytes (a=t sin t=f, ≤ 4 KB/escape)")
os.environ.pop("SSH_CLIENT")

# ---------------------------------------------------------------------- #
# 9 · sonda positiva de soporte gráfico (kg_probe)
# ---------------------------------------------------------------------- #
import io                                                  # noqa: E402

real_out, real_in = sys.stdout, sys.stdin
try:
    # caso ok: la terminal contesta la consulta ESC G
    rfd, wfd = os.pipe()
    os.write(wfd, b"\x1b_Gi=4294967023;OK\x1b\\")
    sys.stdout = io.StringIO()
    sys.stdin = type("StdinShim", (), {"fileno": staticmethod(lambda: rfd)})()
    try:
        ok1 = tui.kg_probe()
    finally:
        sys.stdout, sys.stdin = real_out, real_in
    os.close(wfd)
    check(ok1, "sonda: respuesta ESC G → soporte confirmado")

    # caso no-soporte: solo responde al DA1 → False por timeout
    rfd2, wfd2 = os.pipe()
    os.write(wfd2, b"\x1b[?62;22c")
    sys.stdout = io.StringIO()
    sys.stdin = type("StdinShim2", (),
                     {"fileno": staticmethod(lambda: rfd2)})()
    try:
        t0 = time.time()
        ok2 = tui.kg_probe(timeout=0.2)
        dt = time.time() - t0
    finally:
        sys.stdout, sys.stdin = real_out, real_in
    os.close(wfd2)
    os.close(rfd2)
    check(ok2 is False, "sonda: solo DA1 (xterm) → sin gráficos")
    check(dt < 1.0, f"sonda: timeout razonable ({dt:.2f} s)")
finally:
    os.close(rfd)

# ---------------------------------------------------------------------- #
# 10 · resolución física exacta (px por celda)
# ---------------------------------------------------------------------- #
pxk = os.path.join(TMP, "physical.k2.png")
out_px = cover._png_for_kitty(src_path, 58, 22, pxk, 580, 440)
check(out_px == pxk and os.path.isfile(out_px),
      "png físico generado (celda 10×20 simulada)")
with Image.open(pxk) as r:
    check(r.size == (580, 440), f"tamaño exacto 1:1: {r.size} == (580, 440)")

# get_png propaga px y nombra la caché por tamaño físico
store2 = cover.CoverStore(covers_dir)
capture = {}


def fake_render(png, w, h, out, px_w, px_h):
    capture["px"] = (px_w, px_h)
    with open(out, "wb") as fh:          # el caché exige «existe»
        fh.write(b"\x89PNG\r\n")
    return out


real_png_for_kitty = cover._png_for_kitty
cover._png_for_kitty = fake_render
try:
    rpx = store2.get_png(vid, 44, 22, "T", px_w=440, px_h=440)
    deadline2 = time.time() + 3.0
    while time.time() < deadline2 and not rpx:
        rpx = store2.get_png(vid, 44, 22, "T", px_w=440, px_h=440)
        time.sleep(0.02)
finally:
    cover._png_for_kitty = real_png_for_kitty
check(capture.get("px") == (440, 440),
      f"get_png propaga los píxeles de la caja {capture.get('px')}")
check(bool(rpx) and "440x440.k2.png" in rpx,
      f"caché por resolución física: {rpx and os.path.basename(rpx)}")

# kg_cell_px: 16t como respaldo (ioctl falla en una tubería)
real_out, real_in = sys.stdout, sys.stdin
rfd_c, wfd_c = os.pipe()
os.write(wfd_c, b"\x1b[?62;22c\x1b[6;20;10t\x1b\\")   # ESC[6;<alto>;<ancho>t
sys.stdout = io.StringIO()
sys.stdin = type("StdinCell", (), {"fileno": staticmethod(lambda: rfd_c)})()
try:
    cell = tui.kg_cell_px()
finally:
    sys.stdout, sys.stdin = real_out, real_in
os.close(wfd_c)
os.close(rfd_c)
check(cell == (10, 20), f"kg_cell_px por 16t: {cell} == (10, 20)")

# sin respuesta → None
rfd_d, wfd_d = os.pipe()
sys.stdout = io.StringIO()
sys.stdin = type("StdinCell2", (), {"fileno": staticmethod(lambda: rfd_d)})()
try:
    none_cell = tui.kg_cell_px()
finally:
    sys.stdout, sys.stdin = real_out, real_in
os.close(wfd_d)
os.close(rfd_d)
check(none_cell is None, "kg_cell_px sin respuesta → None")

# app: _place_kg usa la celda física detectada en el arranque
a._kg_px = (10, 20)
seen = {}


def spy_get_png(*ar, **kw):
    seen["px"] = (kw.get("px_w"), kw.get("px_h"))
    return kpng


a.covers = type("CoversSpy", (), {
    "get_png": staticmethod(spy_get_png),
    "get": staticmethod(lambda *a2, **k: None)})()
a._kg_state = None
a.scr.buf = []
a.draw_panel(W, H)
check(seen.get("px") == (44 * 10, 22 * 20),
      f"app: caja física {seen.get('px')} == (440, 440)")

print("\n—", "TODOS OK" if not FAIL else f"{{}} FALLOS".format(len(FAIL)),
      "—")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)

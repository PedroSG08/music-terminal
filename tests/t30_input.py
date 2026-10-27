# -*- coding: utf-8 -*-
"""t30 — caja de entrada asíncrona (eco de teclado sin bloquear la red).

Comprueba: ①las dos letras se procesan MIENTRAS la búsqueda sugerida va
por red (eco inmediato), ②los resultados aterrizan en la caché y el
retorno a la consulta la reutiliza sin segunda llamada, ③las filas de
sugerencias se dibujan con el indicador →, ④los errores de red no
rompen el diálogo.
"""
import os
import shutil
import sys
import threading
import time

for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    os.environ.pop(var, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"

TMP = "/tmp/opencode/musicterminal/t30"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, "data"), exist_ok=True)
os.environ["MUSICTERMINAL_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["MUSICTERMINAL_CONFIG_DIR"] = os.path.join(TMP, "config")

sys.path.insert(0, "/home/pedro/music-terminal")
from music_terminal import app as appmod                     # noqa: E402
from music_terminal.database import Library                  # noqa: E402
from music_terminal.tui import C                             # noqa: E402

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


a = appmod.App(None, Library(os.path.join(TMP, "data", "t30.db")), {})

# pantalla falsa
a.scr = appmod.tui.Screen.__new__(appmod.tui.Screen)
buf = []
a.scr.write = lambda s: buf.append(s)
a.scr.flush = lambda: None
a.scr.move = lambda y, x: buf.append(f"\x1b[{y+1};{x+1}H")
a.scr.size = (140, 40)

# red simulada: 0.35 s por llamada, cuenta las consultas
calls = []
calls_lock = threading.Lock()


def fake_suggestions(text):
    time.sleep(0.35)
    with calls_lock:
        calls.append(text)
    return [f"result for «{text}»"]


# la secuencia de teclas se consume al instante (nosierra espera a la red)
keys = iter(["a", "b", "enter"])
a.get_key = lambda timeout=0.35: next(keys, "ctrl+c")

t0 = time.time()
res = a.input_box("Buscar", "Test…", suggestion_fn=fake_suggestions)
dt = time.time() - t0
check(res == "ab", f"valor de retorno: {res!r} == 'ab'")
# sync tardaría ≥ 0.7 s (dos llamadas); async debe bajar de ~0.55
check(dt < 0.55, f"eco sin bloqueo: el diálogo devolvió en {dt*100:.0f} ms")
# la consulta «ab» paga en el hilo y aterriza en caché
deadline = time.time() + 2.0
while time.time() < deadline:
    with calls_lock:
        if "ab" in calls:
            break
    time.sleep(0.02)
with calls_lock:
    n = len(calls)
check(n == 1, f"una única consulta por palabra: {calls}")
time.sleep(0.4)                     # drena el hilo de fase 1 del todo

# ── reentrada: la caché persistete en la App evita nueva red ────────── #
with calls_lock:
    calls.clear()
keys = iter(["a", "b", "enter"])
t1 = time.time()
res2 = a.input_box("Buscar", "Test…", suggestion_fn=fake_suggestions)
dt1 = time.time() - t1
check(res2 == "ab" and dt1 < 0.2,
      f"2.ª pasada instantánea ({dt1*1000:.0f} ms), eco sin esperar red")
time.sleep(0.4)                     # drena cualquier hilo pendiente
with calls_lock:
    n2 = len(calls)
check(n2 == 0, "sin llamadas extra (la caché persistente cubrió la consulta)")

# ── las filas de sugerencias se dibujan con el indicador ────────────── #
buf.clear()
# a, b → hilo en vuelo; `pausa` simula al humano esperando a que aterrice
# (la sonda None + el refresco de 0.25 s); luego ↓ selecciona la
# sugerencia ya visible y Enter la acepta
nav_keys = ["a", "b", "end", "pausa", "down", "enter"]


def nav_getkey(timeout=0.35):
    if not nav_keys:
        return "ctrl+c"
    k = nav_keys.pop(0)
    if k == "pausa":                # deja pasar el timeout de refresco
        time.sleep(0.5)
        return None
    return k


a.get_key = nav_getkey
res3 = a.input_box("Buscar", "T…", suggestion_fn=fake_suggestions)
s = "".join(buf)
check(res3 == "result for «ab»",
      f"acepta la sugerencia seleccionada con ↓: {res3!r}")
check("→ " in s, "indicador de selección dibujado")

# ── red rota: el diálogo sigue funcionando ──────────────────────────── #
broken = 0


def broken_suggestions(text):
    nonlocal_broken[0] += 1
    raise RuntimeError("red caída")


nonlocal_broken = [0]
keys = iter(["x", "y", "enter"])
a.get_key = lambda timeout=0.35: next(keys, "ctrl+c")
res4 = a.input_box("Buscar", "T…", suggestion_fn=broken_suggestions)
check(res4 == "xy", f"con red rota devuelve el texto escrito ({res4!r})")
check(nonlocal_broken[0] >= 1, "la búsqueda rota se intentó igualmente")

print("\n—", "TODOS OK" if not FAIL else f"{len(FAIL)} FALLOS", "—")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)

# -*- coding: utf-8 -*-
"""t31 — persistencia de la cola entre sesiones.

Comprueba: ①el round-trip _persist_state → _restore_queue conserva
ítems, posición, modo, aleatorio y contexto; ②el vigilante periódico
_guarda la cola aunque el proceso muera sin cierre limpio (ventana
de kitty cerrada, kill -TERM, apagón) al ritmo prometido y sin
reescribir si nada cambió; ③SIGTERM/SIGHUP guardan antes de morir.
"""
import json
import os
import shutil
import signal
import sys
import threading
import time

for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    os.environ.pop(var, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"

TMP = "/tmp/opencode/musicterminal/t31"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, "data"), exist_ok=True)
os.makedirs(os.path.join(TMP, "config"), exist_ok=True)
os.environ["MUSICTERMINAL_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["MUSICTERMINAL_CONFIG_DIR"] = os.path.join(TMP, "config")

sys.path.insert(0, "/home/pedro/music-terminal")
from music_terminal import app as appmod                 # noqa: E402
from music_terminal.database import Library              # noqa: E402

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


def song(n):
    return {"type": "song", "id": f"id{n}", "title": f"Canción {n}",
            "album": "Disco", "album_id": "alb", "duration": 90 + n,
            "thumb": f"https://i.ytimg.com/{n}.jpg", "sub": "dormant",
            "artists": [["Artista", f"u{n}"]]}


def wait_until(fn, timeout=4.0, step=0.05):
    """Espera activa hasta que fn() sea verídicamente cierto."""
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return fn()


DB = os.path.join(TMP, "data", "colas.db")

# ── ①round-trip completo del estado guardado ────────────────────────── #
a = appmod.App(None, Library(DB), {})
a.q.items = [song(1), song(2), song(3)]
a.q.index = 2
a.q.mode = "one"
a.q.shuffle = True
a.q.context = ("album", "alb")
a._persist_state()

b = appmod.App(None, Library(DB), {})
check(len(b.q.items) == 3, f"3 ítems restaurados ({len(b.q.items)})")
check(b.q.index == 2, f"posición restaurada: index={b.q.index}")
check(b.q.mode == "one", f"modo repetir restaurado: {b.q.mode}")
check(b.q.shuffle is True, "aleatorio restaurado")
check(b.q.context == ("album", "alb"), f"contexto restaurado: {b.q.context}")
cur = b.q.current()
check(cur is not None and cur.get("title") == "Canción 3",
      f"q.current() apunta a «{cur.get('title') if cur else None}»")
art = (b.q.items[0].get("artists") or [])
check(bool(art) and art[0][0] == "Artista",
      "artistas sobreviven con su forma [nombre, id]")

# ── ②vigilante: primer tick guarda, cambio de estado re-guarda, ────── #
#     estado estable no escribe
c = appmod.App(None, Library(DB), {})
c.q.items = [song(10)]
c.q.index = 0

writes = [0]
orig_persist = c._persist_state


def counted():
    writes[0] += 1
    orig_persist()


c._persist_state = counted

cv = threading.Thread(target=c._persist_watch,
                      kwargs={"interval": 0.3}, daemon=True)
cv.start()

ok1 = wait_until(lambda: len((c.db.get_meta("queue_state") or
                              {}).get("items") or []) == 1)
check(ok1, "primer tick del vigilante guarda la cola")

c.q.items.append(song(11))
c.q.index = 1
ok2 = wait_until(lambda: (lambda m: len(m.get("items") or []) == 2
                          and m.get("pos") == 1)
                 (c.db.get_meta("queue_state") or {}))
check(ok2, "cambio de cola se detecta y se re-guarda"
           " (ítems=posición al vuelo)")

writes[0] = 0
time.sleep(1.0)                     # ~3 ticks sin cambio de estado
check(writes[0] == 0, f"sin reescrituras con estado estable"
                      f" ({writes[0]} escrituras en 1 s)")

c._stopping = True
cv.join(timeout=2.0)
check(not cv.is_alive(), "el vigilante obedece _stopping y muere")

d = appmod.App(None, Library(DB), {})
check(len(d.q.items) == 2 and d.q.index == 1,
      f"sesión nueva recupera la última cola del vigilante"
      f" ({len(d.q.items)} ítems, index={d.q.index})")

# ── ③SIGTERM: el handler guarda la cola antes de morir ──────────────── #
e = appmod.App(None, Library(DB), {})
e._install_sig_save()
check(hasattr(e, "_sig_handler"), "handler de señal instalado")
e.q.items = [song(20), song(21)]
e.q.index = 1
ksi = []
try:
    e._sig_handler(signal.SIGTERM, None)      # interrupt_main → KeyboardInterrupt
except KeyboardInterrupt:
    ksi.append(True)
check(ksi, "SIGTERM interrumpe el bucle principal con KeyboardInterrupt")
check(e._sig_saved, "el handler guardó la cola y marcó la bandera")

f = appmod.App(None, Library(DB), {})
check(len(f.q.items) == 2 and f.q.index == 1,
      f"matar el proceso conserva la cola"
      f" ({len(f.q.items)} ítems, index={f.q.index})")

# ── ④caso límite: cola vacía no revienta nada ──────────────────────── #
g = appmod.App(None, Library(DB), {})
g.q.items = []
g.q.index = -1
g.q.mode = "none"
g.q.shuffle = False
g.q.context = None
g._persist_state()
h = appmod.App(None, Library(DB), {})
check(h.q.items == [], "cola vacía persiste como vacía (sin cuelgues)")
fp = g._q_fingerprint()
check(fp == (0, -1, "none", False, None, None),
      f"huella de cola vacía: {fp}")

# ── ⑤raw JSON en disco tiene la forma esperada ──────────────────────── #
raw = Library(DB).get_meta("queue_state")
check(isinstance(raw, dict) and "items" in raw and "pos" in raw,
      "queue_state es un dict con items/pos")
try:
    json.dumps(raw)
    ok_json = True
except (TypeError, ValueError):
    ok_json = False
check(ok_json, "queue_state es serializable JSON puro")

# ── ⑥matanza real: proceso vivo en PTY + SIGTERM ────────────────────── #
#        (como cerrar kitty con la app corriendo)
import pty
import subprocess

CHILD = os.path.join(TMP, "data", "kill.db")
lib0 = Library(CHILD)
lib0.set_meta("queue_state", {
    "items": [song(30), song(31)],
    "pos": 1, "mode": "none", "shuffle": False, "context": None,
})
lib0.close()

child_env = dict(os.environ)
for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_SESSION_ID"):
    child_env.pop(var, None)
child_env["MUSICTERMINAL_DATA_DIR"] = os.path.join(TMP, "data")
child_env["MUSICTERMINAL_CONFIG_DIR"] = os.path.join(TMP, "config")

m, s = pty.openpty()
drain_stop = threading.Event()


def _drain(fd):
    try:
        while not drain_stop.is_set():
            os.read(fd, 65536)
    except OSError:
        pass


dr = threading.Thread(target=_drain, args=(m,), daemon=True)
dr.start()
proc = subprocess.Popen(
    [sys.executable, "-m", "music_terminal.main"],
    stdin=s, stdout=s, stderr=s, cwd="/home/pedro/music-terminal",
    env=child_env)
time.sleep(3.0)                      # arranque completo
proc.send_signal(signal.SIGTERM)     # "cerrar kitty"
dead = None
for i in range(12):
    time.sleep(0.5)
    if proc.poll() is not None:
        dead = 0.5 * (i + 1)
        break
drain_stop.set()
check(dead is not None, f"SIGTERM mata el proceso en {dead}s "
                        f"(rc={proc.returncode})")
if proc.poll() is None:
    proc.kill()
survived = Library(CHILD).get_meta("queue_state")
n_surv = len((survived or {}).get("items") or [])
check(n_surv >= 2, f"la cola pre-sembrada sobrevive al arranque y la "
                   f"muerte por señal ({n_surv} ítems)")
surv = appmod.App(None, Library(CHILD), {})
check(len(surv.q.items) == 2 and surv.q.index == 1,
      "sesión nueva tras la matanza recupera la cola intacta")

print("\n—", "TODOS OK" if not FAIL else f"{len(FAIL)} FALLOS", "—")
for f_ in FAIL:
    print("  ✗", f_)
sys.exit(1 if FAIL else 0)

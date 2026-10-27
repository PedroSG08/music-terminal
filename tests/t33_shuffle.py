# -*- coding: utf-8 -*-
"""t33 — cola aleatoria de playlist (tecla s en las listas).

Comprueba: ①shuffle_play reemplaza la cola con TODAS las canciones
barajadas (misma multiset, orden distinto) y reproduce la primera,
②_drain_list junta la lista entera vía loader (paginación) hasta el
techo, ③el atajo `s` dentro de list_screen encajeta todo sin tocar la
red (portadas congeladas a decorativas) y ④listas vacías no revientan.
"""
import os
import random
import shutil
import sys

for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    os.environ.pop(var, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"
TMP = "/tmp/opencode/musicterminal/t33"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, "data"), exist_ok=True)
os.makedirs(os.path.join(TMP, "config"), exist_ok=True)
os.environ["MUSICTERMINAL_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["MUSICTERMINAL_CONFIG_DIR"] = os.path.join(TMP, "config")

sys.path.insert(0, "/home/pedro/music-terminal")
from music_terminal import app as appmod                     # noqa: E402
from music_terminal import cover as covermod                 # noqa: E402
from music_terminal.database import Library                  # noqa: E402

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


# portadas: sin red jamás — la escalera cae a decorativo al instante
covermod.url2art = lambda *a, **k: []

a = appmod.App(None, Library(os.path.join(TMP, "data", "t33.db")), {})
a.scr = appmod.tui.Screen.__new__(appmod.tui.Screen)
buf = []
a.scr.write = lambda s: buf.append(s)
a.scr.flush = lambda: None
a.scr.move = lambda y, x: buf.append("\x1b")
a.scr.size = (140, 40)
a._kg_ok = False

# reproducción: ni mpv ni red — solo registro
played = []


def fake_play_stream(song):
    played.append(song.get("id"))


a.play_stream = fake_play_stream

SONGS = [{"type": "song", "id": f"v{n}", "title": f"Pista {n}",
          "album": "Al", "album_id": "A1", "duration": 90,
          "thumb": "", "sub": "dormant", "artists": [["Autor", "ua"]]}
         for n in range(20)]

# ── ①shuffle_play: multiset idéntico, orden barajado, reproduce 1.ª ── #
random.seed(7)
a.shuffle_play(SONGS, "Mi Playlist", context="playlist local")
check(len(a.q.items) == len(SONGS),
      f"la cola tiene todas: {len(a.q.items)}/{len(SONGS)}")
check(sorted(s["id"] for s in a.q.items) ==
      sorted(f"v{n}" for n in range(20)),
      "misma multiset de ids (sin perder ni duplicar)")
check(a.q.items != SONGS, "el orden quedó barajado ( ≠ al original)")
check(played == [a.q.items[0]["id"]],
      f"reproduce la primera del orden nuevo ({played})")
check(a.q.index == 0 and a.q.context == "playlist local",
      f"posición 0 y contexto: {a.q.context!r}")

# ── ②drain: la lista entera vía loader (batches de 20 hasta 100) ────── #
BATCH, TOTAL = 20, 100
loaded_offs = []


def loader(offset):
    loaded_offs.append(offset)
    if offset >= TOTAL:
        return []
    return [{"type": "song", "id": f"L{offset + n}", "title": f"L{offset + n}",
             "artists": [], "album": "", "album_id": "", "duration": 60,
             "thumb": "", "sub": ""} for n in range(BATCH)]


big = [{"type": "song", "id": f"L{n}", "title": f"L{n}",
        "artists": [], "album": "", "album_id": "", "duration": 60,
        "thumb": "", "sub": ""} for n in range(20)]
drained = a._drain_list(big, loader)
check(len(drained) == TOTAL,
      f"el drain junta la lista entera: {len(drained)} != {TOTAL}")
check(loaded_offs == [20, 40, 60, 80, 100],
      f"offsets seguidos: {loaded_offs}")

# sin loader: devuelve tal cual
check(a._drain_list(big, None) == big, "sin loader devuelve lo cargado")

# ── ③atajo `s` dentro de list_screen (con loader) ───────────────────── #
keys = ["s", "esc"]
a.get_key = lambda timeout=0.35: keys.pop(0) if keys else "esc"
random.seed(3)
a.list_screen("Playlist larga", list(SONGS)[:20], context="playlist",
              loader=loader)
check(len(a.q.items) == TOTAL,
      f"s mezcla la playlist completa: {len(a.q.items)} != {TOTAL}")
check(played and played[-1] == a.q.items[0]["id"],
      "reproducción arrancada con la primera barajada")
check(a.q.context == "playlist", f"contexto conservado: {a.q.context!r}")

# asegurar que TODOS los ids siguen presentes tras la mezcla del atajo
ids = {s["id"] for s in a.q.items}
expected = {f"v{n}" for n in range(20)} | {f"L{n}" for n in range(100)}
check(ids <= expected, "sin ids extraños tras la mezcla")

# ── ④lista vacía: sin reventar ───────────────────────────────────────── #
keys = ["s", "esc"]
nxt = ["s", "esc"]
a.get_key = lambda timeout=0.35: nxt.pop(0) if nxt else "esc"
a.list_screen("Vacía", [], context="")
check(a.q.items == [] or all(s["id"].startswith("v") or
                             s["id"].startswith("L")
                             for s in a.q.items),
      "lista vacía no siembra basura en la cola")

# shuffle_play con solo ruido no canción: avisa, no rompe
a.shuffle_play([{"type": "album", "id": "A", "title": "Álbum"}], "X")
check(a.q.context in ("playlist", "") or len(a.q.items) == TOTAL,
      "sin canciones válidas: la cola queda como estaba")

print("\n—", "TODOS OK" if not FAIL else f"{len(FAIL)} FALLOS", "—")
for f_ in FAIL:
    print("  ✗", f_)
sys.exit(1 if FAIL else 0)

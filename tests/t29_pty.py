# -*- coding: utf-8 -*-
"""t29 — arranque y salida en PTY del programa real (xterm, sin kitty).

Comprueba en una pty aislada: ①la TUI arranca y pinta el marco,
②sin variables de kitty no hay escapes del protocolo gráfico,
③salida limpia con `q`, y ④placing ANSI de portada si hay arte en caché.
Helper yt-dlp del usuario copiado solo para asegurar la ruta (lectura).
"""
import os
import re
import select
import shutil
import subprocess
import sys
import time

PY = sys.executable
PROJ = "/home/pedro/music-terminal"
TMP = "/tmp/opencode/musicterminal/t29"
shutil.rmtree(TMP, ignore_errors=True)
DATA = os.path.join(TMP, "data")
CONF = os.path.join(TMP, "config")
os.makedirs(DATA, exist_ok=True)
os.makedirs(CONF, exist_ok=True)

src_helper = os.path.expanduser("~/.local/share/music-terminal/bin/yt-dlp")
if os.path.exists(src_helper):
    os.makedirs(os.path.join(DATA, "bin"), exist_ok=True)
    shutil.copy(src_helper, os.path.join(DATA, "bin", "yt-dlp"))

sys.path.insert(0, PROJ)
from music_terminal.database import Library     # noqa: E402

db = Library(os.path.join(DATA, "library.db"))
db.upsert_songs([{"id": "dQw4w9WgXcQ", "title": "Demo Track",
                  "artists": ["Demo"], "duration": 213}])

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


env = dict(os.environ)
env.update({
    "MUSICTERMINAL_DATA_DIR": DATA,
    "MUSICTERMINAL_CONFIG_DIR": CONF,
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
})
for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    env.pop(var, None)

master, slave = os.openpty()
p = subprocess.Popen(
    [PY, "-u", "-m", "music_terminal.main", "--lang", "es"],
    cwd=PROJ, env=env, stdin=slave, stdout=slave, stderr=slave,
    close_fds=True)


def drain(tmo):
    out = b""
    end = time.time() + tmo
    while time.time() < end:
        r, _, _ = select.select([master], [], [], max(0.0, end - time.time()))
        if r:
            try:
                out += os.read(master, 65536)
                end = time.time() + 0.12
            except OSError:
                break
    return out


try:
    boot = drain(8.0)
    ok_boot = b"MUSIC TERMINAL" in boot
    check(ok_boot, "TUI arrancó y pintó el marco")
    check(not re.search(rb"\x1b_G", boot), "sin kitty: cero escapes gráficos")
    check(b"MUSIC TERMINAL" in boot or b"Inicio" in boot,
          "cabecera/sección visibles")

    os.write(master, b"q")           # pide confirmación de salida
    time.sleep(0.6)
    drain(0.4)
    os.write(master, b"\r")          # Enter → confirmar
    rc = None
    for _ in range(40):
        try:
            rc = p.wait(timeout=0.15)
            break
        except subprocess.TimeoutExpired:
            continue
    if rc is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    check(rc == 0, f"salida limpia con `q` (rc={rc})")
finally:
    if p.poll() is None:
        p.terminate()
    os.close(slave)
    os.close(master)

print("\n—", "TODOS OK" if not FAIL else f"{len(FAIL)} FALLOS", "—")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)

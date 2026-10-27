# -*- coding: utf-8 -*-
"""t34 — motor de reproducción: fin de pista, rescate e IPC fiables.

Cubren:
 ① _on_end_file: fin natural ("eof" de mpv moderno, más las variantes
   antiguas "eos"/"reach-end") → on_ended exactamente una vez por pista
 ② "stop"/"quit"/"redirect"/"unknown" sin error se ignoran
 ③ "error"/"no-more-media" → on_error (y no on_ended si hay rescate);
   sin on_error cae en on_ended
 ④ _match_reply halla la respuesta entre los eventos que mpv difunde
 ⑤ IPC real bajo tormenta de eventos: get()/duration() nunca devuelven
   basura (era la fuente del «?:??» y de controles erráticos)
 ⑥ fin natural de un fichero local → on_ended una vez (mpv real)
 ⑦ seek absoluto: el rescate retoma en el segundo pedido (mpv real)

Todo local y sin red: WAV sintéticos en un directorio temporal.
"""
import itertools
import json
import math
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import wave

sys.path.insert(0, "/home/pedro/music-terminal")

from music_terminal.player import MpvPlayer  # noqa: E402


def _synth(path, secs, freq=440):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(i / freq)))
            for i in range(int(8000 * secs))))


def main():
    ok = []
    tmp = tempfile.mkdtemp(prefix="mt_t34_")
    wav10 = os.path.join(tmp, "diez.wav")
    wav4 = os.path.join(tmp, "cuatro.wav")
    _synth(wav10, 10.0)
    _synth(wav4, 4.0)

    # ① eof (moderno) y legacy → on_ended una sola vez
    fired = []
    p = MpvPlayer(on_ended=lambda: fired.append("end"))
    p._on_end_file({"event": "end-file", "reason": "eof"})
    p._on_end_file({"event": "end-file", "reason": "eof"})
    p._on_end_file({"event": "end-file", "reason": "eos"})  # legado tardío
    assert fired == ["end"], fired
    ok.append("①  fin natural notificado una sola vez (eof + legacy)")

    # ② mudanzas internas de mpv: ignoradas
    ign = []
    p2 = MpvPlayer(on_ended=lambda: ign.append("end"))
    for reason in ("stop", "quit", "redirect", "unknown"):
        p2._on_end_file({"event": "end-file", "reason": reason})
    assert ign == [], ign
    p2._on_end_file({"event": "end-file", "reason": "reach-end"})
    assert ign == ["end"], ign
    ok.append("②  stop/quit/redirect/unknown ignorados; reach-end cuenta")

    # ③ error → on_error; sin rescate → on_ended
    err = []
    p3 = MpvPlayer()
    p3.on_error = lambda: err.append("err")
    p3.on_ended = lambda: err.append("end")
    p3._on_end_file({"event": "end-file", "reason": "error",
                     "file_error": "demux failed"})
    assert err == ["err"], err
    assert p3.last_error == "demux failed", p3.last_error
    p3._on_end_file({"event": "end-file", "reason": "error"})  # repetido: no
    assert err == ["err"], err
    p3b = MpvPlayer(on_ended=lambda: err.append("end2"))  # sin on_error
    p3b._on_end_file({"event": "end-file", "reason": "no-more-media"})
    assert "end2" in err and "err2" not in err, err
    ok.append("③  error → on_error (o on_ended si no hay rescate), una vez")

    # ④ respuesta entre ruido de eventos
    data = (b'{"event":"end-file","reason":"eof"}\n'
            b'{"event":"property-change","name":"volume","data":66}\n'
            b'{"error":"success","request_id":7,"data":10.0}\n'
            b'{"event":"idle-active"}')
    msg = MpvPlayer._match_reply(data, 7)
    assert msg and msg.get("data") == 10.0, msg
    assert MpvPlayer._match_reply(b'{"event":"x"}', 9) is None
    ok.append("④  _match_reply halla la respuesta entre eventos difundidos")

    # ⑤ get() fiable bajo tormenta de eventos en el IPC real
    p5 = MpvPlayer(volume=77)
    assert p5.start() is True, p5.last_error or "mpv no arrancó"
    try:
        assert p5.play(wav10, "diez") is True
        fin = time.time() + 5
        while time.time() < fin and p5.duration() < 9.5:
            time.sleep(0.1)
        assert 9.0 < p5.duration() < 11.0, p5.duration()
        stop_storm = threading.Event()

        def storm():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect(p5.addr)
            except OSError:
                return
            for n in itertools.count():
                if stop_storm.is_set() or n > 600:
                    break
                try:
                    s.sendall((json.dumps(
                        {"command": ["set", "volume", n % 100]}
                    ) + "\n").encode())
                except OSError:
                    break
                try:
                    s.recv(65536)
                except OSError:
                    break
                time.sleep(0.01)
            try:
                s.close()
            except OSError:
                pass

        th = threading.Thread(target=storm, daemon=True)
        th.start()
        time.sleep(0.4)
        bad = 0
        for _ in range(24):
            d = p5.duration()
            pa = p5.get("pause")
            idle = p5.get("idle-active")
            if abs(float(d or 0) - 10.0) > 0.4 or not isinstance(pa, bool) \
                    or not isinstance(idle, bool):
                bad += 1
            time.sleep(0.03)
        stop_storm.set()
        th.join(timeout=4)
        p5.set_volume(77)
        assert bad == 0, f"get() mascarado por eventos {bad}/24 veces"
        ok.append("⑤  get()/duration() limpios bajo tormenta (24 consultas)")

        # ⑦ seek absoluto (base del rescate)
        p5.seek(3.0, absolute=True)
        time.sleep(0.5)
        pos = p5.position()
        assert 2.0 < pos < 4.6, pos
        ok.append("⑦  seek absoluto lleva la pista al segundo pedido")
    finally:
        p5.shutdown()

    # ⑥ fin natural de un fichero local → on_ended una vez (mpv real)
    ends = []
    p6 = MpvPlayer(volume=60, on_ended=lambda: ends.append(1))
    assert p6.play(wav4, "cuatro") is True
    fin = time.time() + 15
    while time.time() < fin and not ends:
        time.sleep(0.2)
    time.sleep(0.6)  # margen para descartar dobles disparos
    assert ends == [1], ends
    ok.append("⑥  fin natural real → on_ended exactamente una vez")
    p6.shutdown()

    print("\n".join(ok))
    print("t34 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

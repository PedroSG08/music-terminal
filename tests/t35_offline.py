# -*- coding: utf-8 -*-
"""t35 — copias locales sin internet (carpeta offline).

Comprueba local y sin red (servidor HTTP efímero en 127.0.0.1):
 ① LocalStore: ruta por id, extensión por mime y dedu por memo
 ② download(): baja el stream completo a la carpeta (.m4a) y repite
   instantáneo (el extractor no vuelve a llamarse)
 ③ worker: submit → fichero presente + aviso «⤓ guardada…»
 ④ play_stream con copia preexistente NO llama al extractor (sin red
   de verdad), reproduce con mpv y etiqueta la fuente como «copia»
 ⑤ sin copia y get_stream roto: play_stream devuelve False (aviso)
 ⑥ offline_auto: lo reproducido se encola en el Downloader (y OFF no)
 ⑦ _dl_rows: deduplica y filtra no-canciones
 ⑧ wipe/stats: liberar una copia y vaciar todo
 ⑨ settings: offline_auto por omisión y persistencia; db.get_song
 ⑩ D en playlist remota: espejo → playlist local (crea/actualiza)
"""
import http.server
import os
import shutil
import struct
import sys
import threading
import time
import wave

for var in ("KITTY_WINDOW_ID", "KITTY_PID", "SSH_CLIENT", "SSH_TTY",
            "SSH_CONNECTION"):
    os.environ.pop(var, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"
TMP = "/tmp/opencode/musicterminal/t35"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, "data"), exist_ok=True)
os.makedirs(os.path.join(TMP, "config"), exist_ok=True)
os.environ["MUSICTERMINAL_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["MUSICTERMINAL_CONFIG_DIR"] = os.path.join(TMP, "config")

sys.path.insert(0, "/home/pedro/music-terminal")

from music_terminal import app as appmod               # noqa: E402
from music_terminal import config                      # noqa: E402
from music_terminal.database import Library            # noqa: E402
from music_terminal.downloader import (Downloader,     # noqa: E402
                                       LocalStore, human_bytes)
from music_terminal.extractor import ExtractionError   # noqa: E402

FAIL = []


def check(cond, msg):
    print(("✓ " if cond else "✗ ") + msg)
    if not cond:
        FAIL.append(msg)


def synth(path, secs=3.0, freq=331):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * (i / freq // 1 - (i / freq) // 1)))
            for i in range(int(8000 * secs))))


def play_con_redoble(app, song, intentos=2):
    """play_stream con una segunda oportunidad (mpv frío bajo carga)."""
    res = app.play_stream(song)
    for _ in range(intentos - 1):
        if res is True:
            break
        time.sleep(1.5)
        res = app.play_stream(song)
    return res


class FalsoExtractor:
    """get_stream simulado: rota (HTTP local) o «sin red»."""

    def __init__(self, base, size):
        self.base = base
        self.size = size
        self.calls = 0
        self.broken = False

    def get_stream(self, vid):
        self.calls += 1
        if self.broken:
            raise ExtractionError("sin red (forzado)")
        return (f"{self.base}/flujo?id={vid}",
                f"audio/x-falso @1K (falso)", self.size, "audio/mp4", None)

    def helper_ready(self):
        return False

    def prefetch(self, vid):
        pass


class Recorder:
    """Downloader sustituto que solo anota lo encolado."""

    def __init__(self, store=None):
        self.store = store
        self.submitted = []
        self._seen = set()

    def has(self, vid):
        return self.store.path_for(vid) if self.store else None

    def submit(self, vid, title=""):
        if vid in self._seen:
            return False
        self._seen.add(vid)
        self.submitted.append((vid, title))
        return True


class Servidor(http.server.BaseHTTPRequestHandler):
    datos = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.datos)))
        self.end_headers()
        self.wfile.write(self.datos)

    def log_message(self, *a):
        pass


def main():
    ok = []
    # WAV de 90 KiB (por encima del mínimo de saneamiento del módulo)
    wav = os.path.join(TMP, "sintetico.wav")
    synth(wav, 6.0)
    with open(wav, "rb") as f:
        Servidor.datos = f.read()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Servidor)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    try:
        # ① LocalStore: rutas, walk de exts y memo
        store = LocalStore(os.path.join(TMP, "offline"))
        assert store.root.endswith("offline")
        assert store.path_for("nope") is None
        fake = store.path_for("d1", "m4a")
        with open(fake, "wb") as f:
            f.write(b"\x01" * 100)
        store.register("d1", fake)
        assert store.path_for("d1") == fake         # memo directo
        store2 = LocalStore(os.path.join(TMP, "offline"))   # abierta nueva
        assert store2.path_for("d1") == fake        # walk encuentra la m4a
        ok.append("①  LocalStore: ruta+xestión con walk y memo")

        # ② download() contra el servidor local
        fx = FalsoExtractor(base, 0)
        dl = Downloader(fx, store, notify=lambda *a: None)
        notes = []
        p = dl.download("a1", progress=lambda v, got, tot: notes.append(got))
        assert p.endswith(".m4a") and os.path.getsize(p) == len(Servidor.datos), \
            (p, os.path.getsize(p))
        assert fx.calls == 1 and notes, "sin progreso"
        fx2 = FalsoExtractor(base, 0)
        dl2 = Downloader(fx2, store, notify=lambda *a: None)
        assert dl2.download("a1") == p and fx2.calls == 0, "doble bajada"
        assert dl2.has("a1") == p
        ok.append("②  download() baja el stream (m4a) y repite instantáneo")

        # ③ worker asíncrono
        dlN = Downloader(FalsoExtractor(base, 0), store,
                         notify=lambda m, t="info": notes.append((m, t)))
        notes = []
        dlN.submit("b2", "Canción dos")
        fin = time.time() + 10
        while time.time() < fin and not dlN.has("b2"):
            time.sleep(0.1)
        assert dlN.has("b2"), "el worker no bajó la copia"
        assert any("⤓" in m for m, _ in notes), notes
        ok.append("③  worker: submit → fichero + aviso ⤓")

        # ④ reproducción OFFLINE con extractor roto
        db = Library(os.path.join(TMP, "data", "lib.db"))
        app = appmod.App(None, db, {})
        # sin TUI: los avisos modales no bloquean y nada se pinta
        app.alert = lambda *a, **k: None
        app.draw_rail = lambda *a, **k: None
        roto = FalsoExtractor(base, 0)
        roto.broken = True
        app.extractor = roto
        canción = {"type": "song", "id": "demo", "title": "Demo Offline",
                   "artists": [["Demo", ""]], "duration": 6}
        seed = app.dl.store.path_for("demo", "webm")
        shutil.copyfile(wav, seed)
        app.dl.store.register("demo", seed)
        app.q.set_items([dict(canción)], start=0, context=None)
        res = play_con_redoble(app, dict(canción))
        time.sleep(0.4)
        assert res is True, f"play offline falló: {app.player.last_error}"
        assert roto.calls == 0, "tocó el extractor con copia local"
        dur = app.player.duration()
        assert 5.0 < dur < 7.0, dur
        assert app._prov == "copia", app._prov
        app.player.shutdown()
        ok.append("④  play_stream sin red: la copia local suena en mpv "
                  "(etiqueta copia)")

        # ⑤ sin copia y sin red → fallo más aviso (nada de colgado)
        raro = dict(canción, id="inexistente")
        res = app.play_stream(raro)
        assert res is False
        assert roto.calls == 1
        ok.append("⑤  sin copia+sin red: play_stream devuelve False")

        # ⑥ offline_auto encola lo reproducido (y OFF no encola)
        st = app.dl.store                    # carpeta real, capturada antes
        rec = Recorder(st)                   # el replay de «demo» sigue local
        app.dl = rec
        app.settings["offline_auto"] = True
        res = play_con_redoble(app, dict(canción, id="demo"))  # copia en local
        assert res is True and rec.submitted and rec.submitted[-1] == \
            ("demo", "Demo Offline"), rec.submitted
        assert roto.calls == 1, roto.calls
        app.settings["offline_auto"] = False
        before = len(rec.submitted)
        app.play_stream(dict(canción, id="demo"))
        assert len(rec.submitted) == before
        app.player.shutdown()
        ok.append("⑥  toggle offline_auto: ON encola, OFF no")

        # ⑦ _dl_rows: dedupe + filtro
        rec2 = Recorder(st)
        app.dl = rec2
        fila_canc = dict(canción, id="s1")
        fila_pl = {"type": "playlist", "id": "PL", "title": "lista"}
        app._dl_rows([fila_canc, fila_canc, fila_pl])
        assert [v for v, _t in rec2.submitted] == ["s1"], rec2.submitted
        app._dl_rows([], whole=True)
        ok.append("⑦  _dl_rows: deduplicación y filtro de no-canciones")

        # ⑧ wipe/stats sobre la carpeta del app (vacía primero la «demo»)
        st.wipe()
        shutil.copyfile(wav, st.path_for("w1", "m4a"))
        shutil.copyfile(wav, st.path_for("w2", "webm"))
        nn, tt = st.stats()
        assert nn == 2 and tt == 2 * os.path.getsize(wav), (nn, tt)
        st.wipe("w1")
        assert st.path_for("w1") is None and st.stats()[0] == 1
        st.wipe()
        assert st.stats() == (0, 0)
        ok.append("⑧  wipe/stats: libera por id y vacía todo")

        # ⑨ settings por omisión + persistencia + db.get_song
        db2 = Library(os.path.join(TMP, "data", "lib.db"))
        db2.upsert_songs([dict(canción)])
        meta = db2.get_song("demo")
        assert meta and meta["title"] == "Demo Offline"
        s = config.load_settings()
        assert s.get("offline_auto") is False, s
        s["offline_auto"] = True
        config.save_settings(s)
        assert config.load_settings()["offline_auto"] is True
        config.save_settings({**config.load_settings(),
                              "offline_auto": False})
        ok.append("⑨  settings offline_auto + db.get_song")

        # ⑩ D en una playlist remota: espejo a playlist local
        filas = [dict(canción, id=f"m{i}", title=f"Pista {i}")
                 for i in (1, 2, 3)]
        nota = app._mirror_pl("Radio Espejo", filas)
        assert "creada" in nota, nota
        pid_m = next(p["pid"] for p in app.db.playlists()
                     if p["name"] == "Radio Espejo")
        assert [s["id"] for s in app.db.playlist_songs(pid_m)] == \
            ["m1", "m2", "m3"]
        nota2 = app._mirror_pl("Radio Espejo",
                               filas + [dict(canción, id="m4", title="X4"),
                                        dict(canción, id="m2", title="X2")])
        assert "actualizada" in nota2, nota2
        assert [s["id"] for s in app.db.playlist_songs(pid_m)] == \
            ["m1", "m2", "m3", "m4"]
        assert app._mirror_pl("Radio Espejo", []) == ""
        ok.append("⑩  D en playlist remota: espejo a playlist local")

        # ⑪ menú «Playlists de tu cuenta»: d → copia + playlist local
        class ApiCuenta:
            signed_in = True

            def library_playlists(self):
                return [{"id": "PLX", "title": "Lista Cuenta",
                         "count": "3 · lista"}]

            def playlist_songs(self, pid):
                assert pid == "PLX", pid
                return {"title": "Lista Cuenta", "items": [
                    dict(canción, id=f"c{i}", title=f"Cuenta {i}",
                         artists=[["Yo", ""]]) for i in (1, 2, 3)]}

        class ColaDL:
            def __init__(self):
                self.hechos = []

            def submit(self, vid, title=""):
                if any(v == vid for v, _ in self.hechos):
                    return False
                self.hechos.append((vid, title))
                return True

            def has(self, vid):
                return any(v == vid for v, _ in self.hechos)

        app2 = appmod.App(ApiCuenta(), db, {})
        rec3 = ColaDL()
        app2.dl = rec3

        def aviso(m, _tone="info"):
            if m:
                aviso.text = m
        aviso.text = ""
        app2.flash = aviso
        app2.alert = lambda *a, **k: aviso("ALERTA " + str(a))
        # sin TUI: nada se pinta; el menú no mira el retorno de draw_base
        app2.draw_rail = lambda *a, **k: None
        app2.draw_base = lambda *a, **k: (0, 0, 24, 80)
        app2.menu = lambda *a, **k: None
        sec = ["d", "d", "esc"]
        # spinner del fetch usa timeout 0.08 → None; el menú (60.0) → cola
        app2.get_key = lambda t=None, *a, **k: (
            sec.pop(0) if sec else None) if (t or 60) >= 1 else None
        app2.acct_playlists()
        pid_c = next(p["pid"] for p in db.playlists()
                     if p["name"] == "Lista Cuenta")
        assert [s["id"] for s in db.playlist_songs(pid_c)] == \
            ["c1", "c2", "c3"]
        assert sorted(v for v, _ in rec3.hechos) == ["c1", "c2", "c3"]
        assert "actualizada" in aviso.text, aviso.text
        ok.append("⑪  cuenta → «d»: copia + playlist local actualizada")

        print(" · tamano humano:", human_bytes(1024 * 1024 * 3.4))
        db.close(); db2.close()
    finally:
        srv.shutdown()

    if FAIL:
        print("FALLOS:", FAIL)
        return 1
    print("\n".join(ok))
    print("t35 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Configuración y rutas de Music Terminal."""
import json
import os

APP_NAME = "Music Terminal"
APP_VERSION = "1.4.12"

# Rutas de datos y configuración (mismo patrón que AniLinux Terminal).
def _env(var):
    return os.environ.get(var)

def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)

DATA_DIR = _env("MUSICTERMINAL_DATA_DIR") or _home(".local", "share", "music-terminal")
CONFIG_DIR = _env("MUSICTERMINAL_CONFIG_DIR") or _home(".config", "music-terminal")
DB_PATH = os.path.join(DATA_DIR, "library.db")
SESSION_PATH = os.path.join(CONFIG_DIR, "session.json")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
AUTH_PATH = os.path.join(CONFIG_DIR, "auth.json")
COOKIE_TXT = os.path.join(CONFIG_DIR, "cookie.txt")
COVER_DIR = os.path.join(DATA_DIR, "covers")
HELPER_BIN = os.path.join(DATA_DIR, "bin")

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)

# --- Preferencias -------------------------------------------------------
DEFAULT_SETTINGS = {
    "player": "auto",           # auto | mpv | browser
    "audio_quality": "high",    # high | medium | low
    "region": "ES",             # gl (país) para contenido
    "language": "es",           # hl (idioma de resultados)
    "volume": 80,
    "extractor": "auto",        # auto | native | ytdlp
    "repeat": "none",           # none | all | one
    "shuffle": False,
    "max_history": 500,
    "offline_auto": False,      # guardar copia local de lo reproducido
    "keep_screen_off": False,   # no apagar la caja al reproducir (no-op)
}

def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        s = dict(DEFAULT_SETTINGS)
        s.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        return s
    except Exception:
        return dict(DEFAULT_SETTINGS)

def save_settings(settings):
    ensure_dirs()
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass

# --- Utilidades -------------------------------------------------------
QUALITY_LABELS = {"high": "Alta (~160 kbps)", "medium": "Media (~96 kbps)",
                  "low": "Baja (~48 kbps)"}
REPEAT_LABELS = {"none": "No", "all": "Repetir lista", "one": "Repetir una"}
EXTRACTOR_LABELS = {"auto": "Automático (propio + yt-dlp si hace falta)",
                    "native": "Solo nativo (Innertube)",
                    "ytdlp": "Solo yt-dlp (binario opcional)"}

YT_WATCH_URL = "https://music.youtube.com/watch?v={} "

def fmt_secs(n):
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        return "?:??"
    return "%d:%02d" % (n // 60, n % 60)

def parse_time(text):
    if not text:
        return None
    parts = [p for p in str(text).split(":") if p != ""]
    if len(parts) > 3:
        return None
    try:
        vals = [int(p) for p in parts]
    except ValueError:
        return None
    sec = 0
    for v in vals:
        sec = sec * 60 + v
    return sec

def fmt_views(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def yt_watch_url(video_id):
    return YT_WATCH_URL.format(video_id).strip()

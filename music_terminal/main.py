# -*- coding: utf-8 -*-
"""Punto de entrada de Music Terminal."""
import argparse
import shutil
import sys

from . import config
from .app import App
from .database import Library
from .innertube import InnerTube


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="music-terminal",
        description="YouTube Music — cliente de terminal para Linux.")
    parser.add_argument("--version", action="version",
                        version=f"Music Terminal {config.APP_VERSION}")
    parser.add_argument("--region", metavar="COD",
                        help="código de región para el catálogo (gl), ej.: ES")
    parser.add_argument("--lang", metavar="COD",
                        help="código de idioma de resultados (hl), ej.: es")
    args = parser.parse_args(argv)

    config.ensure_dirs()
    settings = config.load_settings()
    if args.region:
        settings["region"] = args.region.upper()
    if args.lang:
        settings["language"] = args.lang.lower()
    if not shutil.which("mpv"):
        log("[aviso] mpv no está instalado; la reproducción no funcionará")
        log("         instálalo con: sudo apt install mpv")

    api = InnerTube(region=settings.get("region", "ES"),
                    language=settings.get("language", "es"),
                    session_path=config.SESSION_PATH,
                    auth_path=config.AUTH_PATH)
    db = Library(config.DB_PATH)

    try:
        try:
            api.ensure_visitor()
            log("[sesión] visitorData de YouTube Music listo")
        except Exception as e:  # noqa: BLE001
            log(f"[sesión] aviso: {e} (se reintentará al navegar)")

        app = App(api, db, settings)
        app.run()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

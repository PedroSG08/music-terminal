# -*- coding: utf-8 -*-
"""Cliente de la API InnerTube de YouTube Music (como hace la app Metrolist).

Solo librería estándar: urllib + gzip/json para las llamadas y parseo
defensivo con recorridos genéricos de las respuestas.
"""
import gzip
import hashlib
import json
import os
import random
import re
import string
import threading
import time
import urllib.error
import urllib.request
import zlib

_COUNT_RE = re.compile(r"^\d+([.,]\d+)?\s*[KM]?\s*(de\s*)?"
                       r"(suscriptores|escuchas|escuchas mensuales|"
                       r"visualizaciones|reproducciones|me\s+gusta)",
                       re.IGNORECASE)

# clientes de innertubex (com.metrolist.innertubex.models.YouTubeClient)
CLIENT = {
    # WEB_REMIX: núcleo de la app (navegación/búsqueda de música)
    "WEB_REMIX": {
        "clientName": "WEB_REMIX",
        "clientVersion": "1.20260707.12.00",
        "clientId": "67",
        "host": "music.youtube.com",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
               "Gecko/20100101 Firefox/140.0"),
        "origin": "https://music.youtube.com",
        "ctx": {},
    },
    # ANDROID_VR: solo útil como sonda (sus URLs mueren a partir de 1 MiB:
    # «every audio itag reached a CDN 403 after 1 MiB» en el catálogo de la
    # app).  La reproducción real va por VISIONOS.
    "ANDROID_VR": {
        "clientName": "ANDROID_VR",
        "clientVersion": "1.65.10",
        "clientId": "28",
        "host": "www.youtube.com",
        "ua": ("com.google.android.apps.youtube.vr.oculus/1.65.10 "
               "(Linux; U; Android 12L; eureka-user "
               "Build/SQ3A.220605.009.A1) gzip"),
        "origin": "https://www.youtube.com",
        "ctx": {"osName": "Android", "osVersion": "12L", "deviceMake": "Oculus",
                "deviceModel": "Quest 3", "androidSdkVersion": "32",
                "platform": "MOBILE"},
    },
    # VISIONOS 0.1: cliente principal de reproducción del catálogo de la
    # app («Anonymous playback passed ... sustained playback and seeks»):
    # entrega URLs de audio planas y de rango abierto (mpv directo).
    "VISIONOS_0_1": {
        "clientName": "VISIONOS",
        "clientVersion": "0.1",
        "clientId": "101",
        "host": "www.youtube.com",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) "
               "Version/17.5 Safari/605.1.15"),
        "origin": "https://www.youtube.com",
        "ctx": {"osName": "VISION_OS", "osVersion": "1.3",
                "deviceMake": "Apple", "deviceModel": "RealityDevice14,1"},
    },
    # IOS 20: cliente público que aún entrega `player` OK sin cookies
    # (los session-bound — VISIONOS/ANDROID_VR — ahora dan HTTP 400 con
    # sesión y bot-check anónimos según la IP; IOS es el primero de la
    # escalera de reproducción).
    "IOS": {
        "clientName": "IOS",
        "clientVersion": "20.10.4",
        "clientId": "5",
        "host": "www.youtube.com",
        "ua": ("com.google.ios.youtube/20.10.4 "
               "(iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)"),
        "origin": "https://www.youtube.com",
        "ctx": {"osName": "iPhone", "osVersion": "18.3.2.22D82",
                "deviceMake": "Apple", "deviceModel": "iPhone16,2"},
    },
}

SEARCH_FILTERS = {
    "songs": "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D",
    "videos": "EgWKAQIQAWoKEAkQChAFEAMQBA%3D%3D",
    "albums": "EgWKAQIYAWoKEAkQChAFEAMQBA%3D%3D",
    "artists": "EgWKAQIgAWoKEAkQChAFEAMQBA%3D%3D",
    "community_playlists": "EgeKAQQoAEABagoQAxAEEAoQCRAF",
    "featured_playlists": "EgeKAQQoADgBagwQDhAKEAMQBRAJEAQ%3D%3D",
}


class ApiError(Exception):
    pass


class NetworkError(ApiError):
    pass


# ---------------------------------------------------------------------- #
# helpers de JSON
# ---------------------------------------------------------------------- #
def dig(obj, *path):
    """Recorre un dict/lista siguiendo la ruta de claves; None si falta."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            key = int(key) if isinstance(key, str) and key.isdigit() else key
            if isinstance(key, int):
                if key < len(cur):
                    cur = cur[key]
                else:
                    return None
            elif isinstance(key, str):
                for alt in cur:
                    if isinstance(alt, dict) and key in alt:
                        cur = alt[key]
                        break
                else:
                    return None
        else:
            return None
    return cur


def walk(obj, key):
    """Todos los valores m[key] en cualquier profundidad."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, key)


def runs_text(node):
    if not node:
        return ""
    if isinstance(node, dict):
        if "runs" in node:
            return "".join(
                r.get("text", "") for r in node.get("runs", []) if isinstance(r, dict))
        return str(node.get("simpleText", "") or "")
    return str(node)


def parse_time(text):
    if not text:
        return None
    parts = str(text).strip().split(":")
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec


def thumb_url(node, size=544):
    """Mejor miniatura de un renderer thumbnail en respuesta MT."""
    if not node:
        return ""
    thumbs = []
    for n in walk(node, "thumbnails"):
        if isinstance(n, list):
            thumbs.extend(t for t in n if isinstance(t, dict) and t.get("url"))
    if not thumbs:
        return ""
    thumbs.sort(key=lambda t: int(t.get("width", 0) or 0), reverse=True)
    return thumbs[0].get("url", "")


def _first_video_id(obj):
    """Primer watchEndpoint.videoId hallado (approach innertubex AnyText)."""
    for we in walk(obj, "watchEndpoint"):
        if isinstance(we, dict) and we.get("videoId"):
            return we["videoId"]
    for q in walk(obj, "queueTarget"):
        if isinstance(q, dict) and q.get("videoId"):
            return q["videoId"]
    return None


def _first_browse_id(obj):
    for be in walk(obj, "browseEndpoint"):
        if isinstance(be, dict) and be.get("browseId"):
            return be["browseId"]
    return None


def _nav_buttons(data):
    """musicNavigationButtonRenderer → {'title','browseId','params'}."""
    out = []
    for nav in walk(data, "musicNavigationButtonRenderer"):
        if not isinstance(nav, dict):
            continue
        title = (runs_text(dig(nav, "buttonText"))
                 or runs_text(dig(nav, "title")))
        be = dig(nav, "clickCommand", "browseEndpoint") or {}
        bid = be.get("browseId")
        params = be.get("params")
        if title and (bid or params):
            out.append({"title": title, "params": params, "browseId": bid})
    return out


def _browse_type(browse_id):
    if not browse_id:
        return "song"
    if browse_id.startswith("MPRE"):
        return "album"
    if browse_id.startswith("UC"):
        return "artist"
    if browse_id.startswith("VL") or browse_id.startswith("PL"):
        return "playlist"
    return "song"


def _header_fallback(data, preferred=("musicImmersiveHeaderRenderer",
                                     "musicDetailHeaderRenderer",
                                     "musicVisualHeaderRenderer",
                                     "musicResponsiveHeaderRenderer")):
    """Título de la página buscando los banners en cualquier profundidad."""
    for key in preferred:
        for h in walk(data, key):
            t = runs_text((h or {}).get("title"))
            if t:
                return t
    return None


def _split_subtitle_groups(node):
    """Agrupa los runs del subtítulo por el separador ' • ' de la app MT.

    Acepta dos formas:
      · musicResponsiveListItemRenderer → runs en flexColumns[1]
      · musicTwoRowItemRenderer        → runs en subtitle.runs
    Devuelve [(texto, browseId | None), …].
    """
    if isinstance(node, dict) and "runs" in node:
        text_runs = node.get("runs") or []
    else:
        text_runs = (dig(node, "subtitle", "runs")
                     or dig(node, "flexColumns", "1",
                            "musicResponsiveListItemFlexColumnRenderer",
                            "text", "runs")
                     or [])
    groups, cur, cur_id = [], [], None
    for r in text_runs:
        if not isinstance(r, dict):
            continue
        t = r.get("text", "")
        be = dig(r, "navigationEndpoint", "browseEndpoint")
        if t == " • ":
            if cur:
                groups.append(("".join(x.get("text", "") for x in cur), cur_id))
                cur, cur_id = [], None
            continue
        cur.append(r)
        if be and (be or {}).get("browseId"):
            cur_id = be["browseId"]
    if cur:
        groups.append(("".join(x.get("text", "") for x in cur), cur_id))
    return groups


def _parse_subtitle(node):
    """(artista, artist_id, álbum, album_id, duración, extras)."""
    groups = _split_subtitle_groups(node)
    artist = artist_id = album = album_id = None
    duration = None
    extras = []
    for text, bid in groups:
        text = text.strip(" ·")
        if not text:
            continue
        t = parse_time(text)
        if t and text.count(":") in (1, 2):
            duration = t
            continue
        kind = _browse_type(bid) if bid else None
        if kind == "artist" and artist is None:
            artist, artist_id = text, bid
        elif kind == "album" and album is None:
            album, album_id = text, bid
        elif artist is None and not _COUNT_RE.match(text):
            artist = text
        else:
            extras.append(text)
    return artist, artist_id, album, album_id, duration, extras


def _song_from_renderer(r):
    """musicResponsiveListItemRenderer → dict unificado (búsqueda/listas)."""
    if not r:
        return None
    title = runs_text(dig(r, "flexColumns", "0", "musicResponsiveListItemFlexColumnRenderer", "text"))
    if not title:
        title = runs_text(dig(r, "title"))
    if not title:
        return None
    vid = None
    pid = r.get("playlistItemData")
    if isinstance(pid, dict) and pid.get("videoId"):
        vid = pid["videoId"]
    if not vid:
        vid = _first_video_id(r)
    artist, artist_id, album, album_id, duration, extras = _parse_subtitle(r)
    nav_be = dig(r, "navigationEndpoint", "browseEndpoint") or {}
    nav_id = nav_be.get("browseId") or _first_browse_id(r)
    if not artist_id and _browse_type(nav_id or "") == "artist":
        artist_id, artist = nav_id, artist or title
    if not album_id and not vid and _browse_type(nav_id or "") == "album":
        album_id = nav_id
    sub = runs_text(dig(r, "flexColumns", "2", "musicResponsiveListItemFlexColumnRenderer", "text"))
    itype = "song" if vid else _browse_type(artist_id or album_id or nav_id)
    item = {
        "type": itype,
        "id": vid or artist_id or album_id or nav_id,
        "title": title,
        "duration": duration,
        "thumb": thumb_url(r.get("thumbnail")),
    }
    if artist:
        item["artists"] = [(artist, artist_id)]
    if album:
        item["album"], item["album_id"] = album, album_id
    if sub:
        item["sub"] = sub
    elif extras:
        item["sub"] = " · ".join(extras)
    return item


def _song_from_two_row(r):
    """musicTwoRowItemRenderer → dict unificado (carruseles de inicio)."""
    if not r:
        return None
    title = runs_text(dig(r, "title"))
    if not title:
        return None
    nav = dig(r, "navigationEndpoint") or {}
    be = dig(nav, "browseEndpoint") or {}
    vid = None
    for we in walk(nav, "watchEndpoint"):
        if isinstance(we, dict) and we.get("videoId"):
            vid = we["videoId"]
            break
    if not vid:
        for we in walk(nav, "watchPlaylistEndpoint"):
            if isinstance(we, dict) and we.get("videoId"):
                vid = we["videoId"]
                break
    artist, artist_id, album, album_id, duration, extras = _parse_subtitle(r)
    sub = runs_text(dig(r, "subtitle"))
    if vid:
        itype, iid = "song", vid
    else:
        iid = be.get("browseId")
        itype = _browse_type(iid)
    item = {
        "type": itype,
        "id": iid,
        "title": title,
        "duration": duration if itype == "song" else None,
        "thumb": thumb_url(r.get("thumbnailRenderer") or r.get("thumbnail")),
        "sub": sub,
    }
    if album:
        item["album"], item["album_id"] = album, album_id
    return item


def _item_from_playlist_row(r):
    """playlistItemRenderer (historial / playlists de la cuenta) → canción."""
    if not isinstance(r, dict) or not r:
        return None
    vid = r.get("videoId")
    if not vid:
        pid = r.get("playlistItemData")
        vid = pid.get("videoId") if isinstance(pid, dict) else None
    if not vid:
        return None
    item = _song_from_two_row(r)
    if not item:
        # sin título útil → construir desde datos mínimos
        title = runs_text(r.get("title"))
        if not title:
            return None
        item = {"type": "song", "id": vid, "title": title,
                "thumb": thumb_url(r.get("thumbnail"))}
    item["type"], item["id"] = "song", vid
    return item


def _items_from_auth_shelves(data):
    """Canciones de páginas con sesión: estantes con filas de respuesta
    (historial de YT Music) o playlistItemRenderer (Playlists de cuenta)."""
    items, seen = [], set()

    def add(item):
        if isinstance(item, dict) and item.get("id") \
                and item.get("id") not in seen:
            seen.add(item["id"])
            items.append(item)

    for key in ("musicShelfRenderer", "musicPlaylistShelfRenderer"):
        for shelf in walk(data, key):
            for it in (shelf or {}).get("contents") or []:
                add(_row_any(it))
    for row in walk(data, "musicResponsiveListItemRenderer"):
        add(_row_any(row))
    for row in walk(data, "playlistItemRenderer"):
        add(_item_from_playlist_row(row))
    return items


def _row_any(it):
    if not isinstance(it, dict):
        return None
    row = None
    for r in walk(it, "musicResponsiveListItemRenderer"):
        row = _song_from_renderer(r)
        if row:
            break
    if not row:
        for r in walk(it, "playlistItemRenderer"):
            row = _item_from_playlist_row(r)
            if row:
                break
    return row


def _artist_runs_raw(sub):
    """Artista • Duración (twoRow) — versión simple sobre texto plano."""
    parts = [p.strip(" ·") for p in sub.split("•")]
    parts = [p for p in parts if p]
    artist = parts[0] if parts else None
    duration = None
    for p in parts:
        t = parse_time(p)
        if t:
            duration = t
            break
    return artist, None, duration, parts[1:]


# ---------------------------------------------------------------------- #
# HTTP
# ---------------------------------------------------------------------- #
def http(method, url, headers=None, body=None, timeout=30):
    h = dict(headers or {})
    data = body.encode() if isinstance(body, str) else body
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    h.setdefault("Accept-Encoding", "gzip")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            elif enc in ("deflate", "zlib"):
                try:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except Exception:
                    pass
            return raw
    except urllib.error.HTTPError as e:
        try:
            raw_err = e.read()
            enc = (e.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or raw_err[:2] == b"\x1f\x8b":
                try:
                    raw_err = gzip.decompress(raw_err)
                except Exception:
                    pass
            body_txt = raw_err.decode("utf-8", "replace")[:300]
        except Exception:
            body_txt = ""
        raise ApiError(f"HTTP {e.code} en {url[:80]}: {body_txt}") from None
    except (urllib.error.URLError, OSError) as e:
        raise NetworkError(f"Sin conexión: {e}") from None


# ---------------------------------------------------------------------- #
# cliente
# ---------------------------------------------------------------------- #
class InnerTube:
    def __init__(self, region="ES", language="es", session_path=None,
                 auth_path=None):
        self.region = region
        self.language = language
        self.session_path = session_path
        self.auth_path = auth_path
        self.visitor = None
        self.auth_cookie = None
        self.auth_sapisid = None
        self.auth_visitor = None
        self.auth_name = None
        self._lock = threading.Lock()
        self._load_visitor()
        self._load_auth()

    # ---- sesión de visitor (persistida como la app guarda la suya) ---- #
    def _load_visitor(self):
        if not self.session_path or not os.path.isfile(self.session_path):
            return
        try:
            with open(self.session_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("visitor") and data.get("region") == self.region:
                self.visitor = data["visitor"]
        except Exception:
            pass

    def save_visitor(self):
        if self.session_path and self.visitor:
            try:
                os.makedirs(os.path.dirname(self.session_path), exist_ok=True)
                with open(self.session_path, "w", encoding="utf-8") as f:
                    json.dump({"visitor": self.visitor, "region": self.region,
                               "saved": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
                os.chmod(self.session_path, 0o600)
            except OSError:
                pass

    def ensure_visitor(self):
        with self._lock:
            if self.visitor:
                return self.visitor
            # la app lo saca de la página de music.youtube.com (ytcfg)
            raw = http("GET", "https://music.youtube.com/", headers={
                "User-Agent": CLIENT["WEB_REMIX"]["ua"],
                "Accept-Language": f"{self.language},en;q=0.8"})
            html = raw.decode("utf-8", "replace")
            m = re.search(r'"visitorData":"([^"]+)"', html)
            if not m:
                raise ApiError("no se pudo obtener visitorData")
            self.visitor = m.group(1)
            self.save_visitor()
            return self.visitor

    def reset_visitor(self):
        with self._lock:
            self.visitor = None
        if self.session_path and os.path.isfile(self.session_path):
            try:
                os.remove(self.session_path)
            except OSError:
                pass

    # ---- cuenta Google (cookie del navegador, como ytmusicapi) -------- #
    @property
    def signed_in(self):
        return bool(self.auth_sapisid)

    def _load_auth(self):
        if not self.auth_path or not os.path.isfile(self.auth_path):
            return
        try:
            with open(self.auth_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._set_from_cookie(data.get("cookie") or "")
            self.auth_name = data.get("name") if data.get("name") else None
        except Exception:
            self.auth_cookie = None
            self.auth_sapisid = None
            self.auth_visitor = None

    def _set_from_cookie(self, raw):
        """Extrae SAPISID del texto pegado; False si no trae credenciales."""
        if not raw:
            self.auth_cookie, self.auth_sapisid = None, None
            return False
        raw = raw.strip()
        low = raw.lower()
        if low.startswith("cookie:"):
            raw = raw[7:].strip()
        sapisid = None
        for k in ("__Secure-3PAPISID", "SAPISID"):
            m = re.search(rf"(?:^|;\s*){k}=([^;\s]+)", raw)
            if m:
                sapisid = m.group(1)
                break
        if not sapisid:
            raise ApiError("la cookie no contiene SAPISID "
                           "(¿copiaste la cabecera completa de una "
                           "petición estando con la sesión iniciada?)")
        self.auth_cookie, self.auth_sapisid = raw, sapisid
        self.auth_visitor = None
        for part in raw.split(";"):
            name, _, val = part.strip().partition("=")
            if name == "VISITOR_INFO1_LIVE" and val:
                self.auth_visitor = val
                break
        return True

    def set_auth(self, cookie, name=None):
        """Activa y persiste la sesión (auth.json 0600)."""
        ok = self._set_from_cookie(cookie)
        self.auth_name = name if ok else None
        if ok and self.auth_path:
            try:
                os.makedirs(os.path.dirname(self.auth_path), exist_ok=True)
                with open(self.auth_path, "w", encoding="utf-8") as f:
                    json.dump({"cookie": self.auth_cookie,
                               "name": self.auth_name}, f)
                os.chmod(self.auth_path, 0o600)
            except OSError:
                pass
        return ok

    def clear_auth(self):
        self.auth_cookie, self.auth_sapisid, self.auth_name = None, None, None
        self.auth_visitor = None
        if self.auth_path and os.path.isfile(self.auth_path):
            try:
                os.remove(self.auth_path)
            except OSError:
                pass

    def _auth_headers(self, origin):
        """Cabeceras SAPISIDHASH (hash SHA1 de ts + SAPISID + origen)."""
        if not self.auth_sapisid:
            return {}
        ts = int(time.time())
        base = f"{ts} {self.auth_sapisid} {origin}"
        hash_ = hashlib.sha1(base.encode("utf-8")).hexdigest()
        return {"Authorization": f"SAPISIDHASH {ts}_{hash_}",
                "X-Origin": origin,
                "X-Goog-AuthUser": "0",
                "Cookie": self.auth_cookie}

    # ---- navegación con sesión ---------------------------------------- #
    def _require_auth(self):
        if not self.signed_in:
            raise ApiError("necesitas iniciar sesión con Google "
                           "(Ajustes ▸ Iniciar sesión con Google)")

    def account_name(self):
        """Alias visible de la cuenta (account/accounts_list); None si no."""
        if not self.signed_in:
            return None
        if self.auth_name:
            return self.auth_name
        try:
            d = self._post("account/accounts_list", {})
        except (ApiError, NetworkError):
            d = None
        name = None
        if d:
            for item in walk(d, "accountItem"):
                name = runs_text(item.get("accountName")) \
                    or runs_text(item.get("accountByline")) or name
                break
        if name and name.strip() and name != "Cuentas":
            self.auth_name = name.strip()
        else:
            try:
                raw = http("GET", "https://music.youtube.com/", headers={
                    "User-Agent": CLIENT["WEB_REMIX"]["ua"],
                    **self._auth_headers("https://music.youtube.com")})
                html = raw.decode("utf-8", "replace")
            except (ApiError, NetworkError):
                return None
            for pat in (r'"accountDisplayableName":"([^"]{1,40})"',
                        r'"displayName":"([^"]{1,40})"'):
                m = re.search(pat, html)
                if m and m.group(1).strip():
                    self.auth_name = m.group(1)
                    break
        if not self.auth_name:
            return None
        try:
            with open(self.auth_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["name"] = self.auth_name
            with open(self.auth_path, "w",
                      encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass
        return self.auth_name

    def library_playlists(self):
        """Playlists de la cuenta (pestaña «Listas», FEmusic_liked_playlists
        en la app Metrolist). → [{'title','id','count','thumb'}]."""
        self._require_auth()
        routes = ({"browseId": "FEmusic_liked_playlists"},
                  {"browseId": "FEmusic_library_privately_owned_playlist",
                   "params": "wgYCCAuCBAEIgA%3D"})
        data = None
        for body in routes:
            try:
                data = self._post("browse", body)
                break
            except ApiError:
                continue
        if not data:
            return []
        out, seen = [], set()
        for node in walk(data, "musicTwoRowItemRenderer"):
            it = _song_from_two_row(node)
            if not it:
                continue
            be = dig(node, "navigationEndpoint", "browseEndpoint") or {}
            we = dig(node, "navigationEndpoint", "watchPlaylistEndpoint") \
                or {}
            bid = (be.get("browseId") or "").strip()
            pid = (we.get("playlistId") or ""
                   or (bid[2:] if bid.startswith("VL") else ""))
            if not pid or pid in seen or bid == "VLSE":
                continue            # VLSE = episodios (podcast), no música
            seen.add(pid)
            sub = it.get("sub") or ""
            count = sub.split("•")[-1].strip() if "•" in sub else sub
            out.append({"type": "playlist", "id": pid,
                        "title": it.get("title", "?"),
                        "count": count, "thumb": it.get("thumb", "")})
        return out

    @staticmethod
    def _continuation_token(data):
        """Token de la siguiente página (continuationItemRenderer)."""
        for node in walk(data, "continuationItemRenderer"):
            tok = dig(node, "continuationEndpoint", "continuationCommand",
                      "token")
            if isinstance(tok, str) and tok:
                return tok
        return None

    def _page_rows(self, data, out, seen):
        """Añade a `out` las filas de música de una página (con ``seen``)."""
        def add(item):
            if isinstance(item, dict) and item.get("id") \
                    and item["id"] not in seen:
                seen.add(item["id"])
                out.append(item)
        for key in ("musicPlaylistShelfRenderer", "musicShelfRenderer"):
            for shelf in walk(data, key):
                for node in (shelf or {}).get("contents") or []:
                    add(_row_any(node))
        for row in walk(data, "musicResponsiveListItemRenderer"):
            add(_song_from_renderer(row))
        for row in walk(data, "playlistItemRenderer"):
            add(_item_from_playlist_row(row))

    MAX_PAGES = 60                       # ~60×100 ≈ 6000 canciones por página

    def _collect_with_continuations(self, data, max_pages=None):
        """Todas las filas de una página y sus continuaciones (paginación)."""
        if not isinstance(data, dict):
            return []
        items, seen = [], set()
        self._page_rows(data, items, seen)
        token = self._continuation_token(data)
        pages = 0
        while token and pages < (self.MAX_PAGES if max_pages is None
                                 else max_pages):
            try:
                nxt = self._post("browse", {"continuation": token})
            except (ApiError, NetworkError):
                break
            self._page_rows(nxt, items, seen)
            token = self._continuation_token(nxt)
            pages += 1
        return items

    def playlist_songs(self, playlist_id):
        """Canciones de una playlist de la cuenta (LM = Me gusta), con
        paginación completa (continuations).  Sin sesión no funciona."""
        self._require_auth()
        data = None
        try:
            data = self._post("browse", {"browseId": f"VL{playlist_id}"})
        except ApiError:
            data = None
        items = self._collect_with_continuations(data)
        if not items:          # variante playlistId (alguna playlists raras)
            try:
                data2 = self._post("browse", {"playlistId": playlist_id})
            except ApiError:
                data2 = None
            items2 = self._collect_with_continuations(data2)
            if items2:
                items = items2
        title = _header_fallback(data or {})
        return {"title": title, "items": items}

    def yt_history(self):
        """Historial de YouTube Music completo (continuations incluidos)."""
        self._require_auth()
        try:
            data = self._post("browse", {"browseId": "FEmusic_history"})
        except ApiError:
            data = None
        items = self._collect_with_continuations(data)
        return {"title": _header_fallback(data or {}) or
                "Historial de YouTube",
                "items": items}

    def set_like(self, video_id, on=True):
        """Me gusta / quitar (POST like/like) — necesita sesión."""
        self._require_auth()
        action = "ACTION_LIKE" if on else "ACTION_REMOVE_LIKE"
        body = {"target": {"videoId": video_id},
                "actions": [{"action": action}]}
        try:
            self._post("like/like", body)
            return True
        except ApiError:
            # v2: target envuelto en likeEntity igual de válido
            body2 = {"actions": [{"likeEntity": {"target": {"videoId": video_id}},
                                  "action": action}]}
            self._post("like/like", body2)
            return True

    def ping_played(self, player_data, video_id):
        """Ping opcional de reproducción (sincroniza visto-reciente)."""
        if not self.signed_in:
            return
        base = dig(player_data, "playbackTracking", "videostatsPlaybackUrl",
                   "baseUrl")
        if not base:
            return
        cpn = "".join(random.choices(string.ascii_letters + string.digits,
                                     k=16))
        url = (base + ("&" if "?" in base else "?")
               + f"cpn={cpn}&cmt=1")
        headers = {"User-Agent": CLIENT["WEB_REMIX"]["ua"],
                   **self._auth_headers("https://music.youtube.com")}
        try:
            http("GET", url, headers)
        except (ApiError, NetworkError, OSError):
            pass

    # ---- petición base ---- #
    def _post(self, endpoint, body, client_key="WEB_REMIX", path="youtubei/v1"):
        c = CLIENT[client_key]
        visitor = self.auth_visitor or self.visitor
        client_ctx = {"clientName": c["clientName"],
                      "clientVersion": c["clientVersion"],
                      "hl": self.language, "gl": self.region,
                      "platform": c["ctx"].get("platform")}
        client_ctx = {k: v for k, v in client_ctx.items() if v}
        client_ctx.update({k: v for k, v in c["ctx"].items() if k != "platform"})
        if visitor:
            client_ctx["visitorData"] = visitor
        payload = {"context": {"client": client_ctx}, **body}
        headers = {
            "User-Agent": c["ua"],
            "X-YouTube-Client-Name": c["clientId"],
            "X-YouTube-Client-Version": c["clientVersion"],
            "Origin": c["origin"],
            "Referer": c["origin"] + "/",
            "Accept": "application/json",
        }
        headers.update(self._auth_headers(c["origin"]))
        url = f"https://{c['host']}/{path}/{endpoint}?prettyPrint=false"
        raw = http("POST", url, headers, json.dumps(payload))
        data = json.loads(raw.decode("utf-8", "replace"))
        ps = data.get("playabilityStatus")
        if ps and ps.get("status") == "UNPLAYABLE" and endpoint == "player":
            raise ApiError(f"vídeo no disponible: {ps.get('reason', '…')[:60]}")
        return data

    def _retry(self, fn, attempts=2, backoff=1.2):
        last = None
        for i in range(attempts):
            try:
                self.ensure_visitor()
                return fn()
            except NetworkError:
                raise
            except ApiError as e:
                if "UNPLAYABLE" in str(e):
                    self.reset_visitor()
                    last = e
                    time.sleep(backoff if i else 0)
                    continue
                last = e
                break
        raise last if last else ApiError("fallo API")

    # ---- páginas ---- #
    def search(self, query, params=None, continuation=None):
        body = {"query": query}
        if params:
            body["params"] = params
        if continuation:
            body["continuation"] = continuation
        data = self._post("search", body)
        shots = list(walk(data, "musicResponsiveListItemRenderer"))
        items = [x for x in (_song_from_renderer(r) for r in shots) if x]
        tokens = list(walk(data, "continuationItemRenderer"))
        token = dig(tokens[0] if tokens else None,
                    "continuationEndpoint", "continuationCommand", "token")
        return {"items": items, "continuation": token}

    def suggestions(self, text):
        data = self._post("music/get_search_suggestions", {"input": text})
        out = []
        for c in data.get("contents", []):
            s = dig(c, "searchSuggestionsSectionRenderer", "contents") or []
            for node in s:
                sug = node.get("searchSuggestionRenderer")
                if sug:
                    out.append(runs_text(sug.get("suggestion")))
        return [s for s in out if s][:10]

    def _carousel_title(self, car):
        return runs_text(dig(car, "header", "musicCarouselShelfBasicHeaderRenderer",
                             "title")) or runs_text(dig(car, "header", "musicHeaderRenderer",
                                                        "title"))

    def _parse_section(self, content):
        """Música en una sección: carrusel o estante."""
        out = []
        car = content.get("musicCarouselShelfRenderer")
        if car:
            title = self._carousel_title(car)
            items = []
            for node in car.get("contents", []):
                tr = node.get("musicTwoRowItemRenderer")
                if tr:
                    item = _song_from_two_row(tr)
                else:
                    rl = node.get("musicResponsiveListItemRenderer")
                    item = _song_from_renderer(rl)
                if item and item.get("id"):
                    items.append(item)
            if items:
                out.append({"title": title, "items": items, "kind": "carousel"})
            return out
        shelf = (content.get("musicShelfRenderer")
                 or content.get("musicPlaylistShelfRenderer"))
        if shelf:
            title = runs_text(dig(shelf, "title"))
            items = []
            for node in shelf.get("contents", []):
                rl = node.get("musicResponsiveListItemRenderer")
                item = _song_from_renderer(rl)
                if item and item.get("id"):
                    items.append(item)
            if items:
                out.append({"title": title, "items": items, "kind": "shelf"})
            return out
        grid = content.get("gridRenderer")
        if grid:
            items = []
            for node in grid.get("items", []):
                tr = node.get("musicTwoRowItemRenderer")
                item = _song_from_two_row(tr)
                if item and item.get("id"):
                    items.append(item)
            if items:
                out.append({"title": "", "items": items, "kind": "grid"})
            return out
        return out

    def home(self):
        data = self._post("browse", {"browseId": "FEmusic_home"})
        sections = []
        singles = []
        for c in dig(data, "contents", "singleColumnBrowseResultsRenderer",
                     "tabs", "0", "tabRenderer", "content", "sectionListRenderer",
                     "contents") or []:
            sections.extend(self._parse_section(c))
        for c in dig(data, "contents", "sectionListRenderer", "contents") or []:
            sections.extend(self._parse_section(c))
        return sections, singles

    def explore(self):
        data = self._post("browse", {"browseId": "FEmusic_explore"})
        buttons = _nav_buttons(data)
        sections = []
        for c in dig(data, "contents", "sectionListRenderer", "contents") or []:
            sections.extend(self._parse_section(c))
        return buttons, sections

    def moods_and_genres(self):
        data = self._post("browse", {"browseId": "FEmusic_moods_and_genres"})
        return _nav_buttons(data)

    def mood_section(self, browse_id, params):
        body = {"browseId": browse_id or "MPROmoods_genres_playlist"}
        if params:
            body["params"] = params
        data = self._post("browse", body)
        sections = []
        for c in dig(data, "contents", "sectionListRenderer", "contents") or []:
            sections.extend(self._parse_section(c))
        title = runs_text(dig(data, "header", "musicImmersiveHeaderRenderer", "title"))
        return title, sections

    def charts(self):
        data = self._post("browse", {"browseId": "FEmusic_charts"})
        bands = []
        tr = dig(data, "contents", "singleColumnBrowseResultsRenderer",
                 "tabs", "0", "tabRenderer", "content", "sectionListRenderer",
                 "contents")
        if not tr:
            tr = dig(data, "contents", "sectionListRenderer", "contents") or []
        for c in tr:
            bands.extend(self._parse_section(c))
        title = runs_text(dig(data, "header", "musicVisualHeaderRenderer", "title")) or "Charts"
        return title, bands

    def new_releases(self):
        data = self._post("browse", {"browseId": "FEmusic_new_releases"})
        sections = []
        for c in dig(data, "contents", "singleColumnBrowseResultsRenderer",
                     "tabs", "0", "tabRenderer", "content", "sectionListRenderer",
                     "contents") or []:
            sections.extend(self._parse_section(c))
        for c in dig(data, "contents", "sectionListRenderer", "contents") or []:
            sections.extend(self._parse_section(c))
        title = "Nuevos lanzamientos"
        return title, sections

    def generic_browse(self, browse_id, params=None):
        """Cualquier browse (álbum/artista/playlist/mood/etc.)."""
        body = {"browseId": browse_id}
        if params:
            body["params"] = params
        data = self._post("browse", body)
        title = (_header_fallback(data)
                 or runs_text(dig(data, "header", "musicImmersiveHeaderRenderer",
                                  "title")))
        header = dig(data, "header") or {}
        subtitle = ""
        for key in ("musicDetailHeaderRenderer", "musicVisualHeaderRenderer",
                    "musicResponsiveHeaderRenderer",
                    "musicImmersiveHeaderRenderer"):
            node = header.get(key)
            if not node:
                continue
            for attr in ("subtitle", "description", "strappingText"):
                t = runs_text(dig(node, attr))
                if t:
                    subtitle = t
                    break
            if subtitle:
                break
        sections = []
        for c in dig(data, "contents", "sectionListRenderer", "contents") or []:
            sections.extend(self._parse_section(c))
        for c in dig(data, "contents", "singleColumnBrowseResultsRenderer",
                     "tabs", "0", "tabRenderer", "content", "sectionListRenderer",
                     "contents") or []:
            sections.extend(self._parse_section(c))
        for c in dig(data, "contents", "twoColumnBrowseResultsRenderer",
                     "secondaryContents", "sectionListRenderer", "contents") or []:
            sections.extend(self._parse_section(c))
        # playlist: shelf directo en contents (musicPlaylistShelfRenderer)
        if not sections:
            direct = list(walk(data, "musicPlaylistShelfRenderer"))
            for shelf in direct:
                items = []
                for node in shelf.get("contents", []):
                    rl = node.get("musicResponsiveListItemRenderer")
                    item = _song_from_renderer(rl)
                    if item and item.get("id"):
                        items.append(item)
                if items:
                    sections.append({"title": title or "Playlist", "items": items,
                                     "kind": "shelf"})
        # continuation para playlists
        token = None
        for t in walk(data, "continuationItemRenderer"):
            tok = dig(t, "continuationEndpoint", "continuationCommand", "token")
            if tok:
                token = tok
                break
        return {"title": title, "sections": sections, "continuation": token,
                "subtitle": subtitle}

    def continuation(self, token, browse_id=None):
        """Siguiente página de una playlist con continuationItemRenderer."""
        body = {"continuation": token}
        data = self._post("browse", body)
        items = []
        for c in dig(data, "continuationContents", "musicPlaylistShelfContinuation",
                     "contents") or []:
            rl = c.get("musicResponsiveListItemRenderer")
            item = _song_from_renderer(rl)
            if item and item.get("id"):
                items.append(item)
        for c in dig(data, "continuationContents", "sectionListContinuation",
                     "contents") or []:
            for node in dig(c, "musicShelfContinuation", "contents") or []:
                rl = node.get("musicResponsiveListItemRenderer")
                item = _song_from_renderer(rl)
                if item and item.get("id"):
                    items.append(item)
        token = None
        for t in walk(data, "continuationItemRenderer"):
            tok = dig(t, "continuationEndpoint", "continuationCommand", "token")
            if tok:
                token = tok
                break
        return {"items": items, "continuation": token}

    # ---- cola/ watcher ---- #
    def next(self, video_id, playlist_id=None, continuation=None):
        body = {"videoId": video_id, "playlistId": playlist_id}
        if continuation:
            body = {"continuation": continuation}
        data = self._post("next", body)
        pn = dig(data, "contents", "singleColumnMusicWatchNextResultsRenderer",
                 "tabbedRenderer", "watchNextTabbedResultsRenderer", "tabs")
        songs = []
        lyrics_ep = None
        related_ep = None
        if pn:
            qtab = pn[0]["tabRenderer"]["content"]["musicQueueRenderer"]
            panel = dig(qtab, "content", "playlistPanelRenderer")
            for node in (panel or {}).get("contents", []):
                pv = node.get("playlistPanelVideoRenderer")
                if not pv or not pv.get("videoId"):
                    continue
                title = runs_text(pv.get("title"))
                by = runs_text(pv.get("longBylineText")) or runs_text(pv.get("shortBylineText"))
                dur = parse_time(runs_text(pv.get("lengthText")))
                artist = by.split("•")[0].strip(" ·") if by else None
                songs.append({
                    "type": "song", "id": pv["videoId"], "title": title,
                    "duration": dur, "thumb": thumb_url(pv.get("thumbnail")),
                })
                if artist:
                    songs[-1]["artists"] = [(artist, None)]
            if len(pn) > 1:
                ep = dig(pn, "1", "tabRenderer", "endpoint", "browseEndpoint")
                if ep:
                    lyrics_ep = ep
            if len(pn) > 2:
                ep = dig(pn, "2", "tabRenderer", "endpoint", "browseEndpoint")
                if ep:
                    related_ep = ep
        return {"items": songs, "lyrics_endpoint": lyrics_ep}

    def lyrics(self, browse_id, params):
        body = {"browseId": browse_id}
        if params:
            body["params"] = params
        data = self._post("browse", body)
        for node in walk(data, "musicDescriptionShelfRenderer"):
            txt = runs_text(dig(node, "description"))
            if txt:
                return txt
        return None

    def radio_id(self, video_id):
        """playlistId de radio para reproducir continuando con automezcla."""
        return f"RDAMVM{video_id}"

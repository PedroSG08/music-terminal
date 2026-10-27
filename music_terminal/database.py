# -*- coding: utf-8 -*-
"""Biblioteca local (SQLite): favoritos, historial y playlists de Music Terminal.

Mismo enfoque que `anilinux_tui.database.Catalog`: sqlite3 puro,
esquema sencillo, todo el estado en un único fichero.
"""
import json
import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS song (
    id       TEXT PRIMARY KEY,
    title    TEXT,
    artist   TEXT,
    artist_id TEXT,
    album    TEXT,
    album_id TEXT,
    duration INTEGER,
    thumb    TEXT,
    sub      TEXT
);
CREATE TABLE IF NOT EXISTS favorite (
    id       TEXT PRIMARY KEY,
    ts       INTEGER
);
CREATE TABLE IF NOT EXISTS history (
    id       TEXT PRIMARY KEY,
    ts       INTEGER
);
CREATE TABLE IF NOT EXISTS playlist (
    pid      INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT UNIQUE,
    created  INTEGER
);
CREATE TABLE IF NOT EXISTS pl_song (
    pid      INTEGER,
    id       TEXT,
    pos      INTEGER,
    PRIMARY KEY (pid, id)
);
CREATE TABLE IF NOT EXISTS kv (
    k        TEXT PRIMARY KEY,
    v        TEXT
);
"""


class Library:
    def __init__(self, db_path):
        # apto para hilos: el vigilante de persistencia (_persist_watch)
        # escribe meta desde su propio hilo; el lock serializa el acceso
        # a la conexión compartida
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=3000")
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self.conn:
            self.conn.executescript(_SCHEMA)

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ #
    # canciones en caché (para listas offline)
    # ------------------------------------------------------------------ #
    def upsert_songs(self, items):
        for it in items:
            if not it.get("id") or it.get("type") not in (None, "song"):
                continue
            artists = it.get("artists") or []
            self.conn.execute(
                "INSERT INTO song (id,title,artist,artist_id,album,album_id,"
                "duration,thumb,sub) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (id) DO UPDATE SET "
                "title=COALESCE(NULLIF(excluded.title,''),song.title),"
                "artist=COALESCE(NULLIF(excluded.artist,''),song.artist),"
                "artist_id=COALESCE(excluded.artist_id,song.artist_id),"
                "album=COALESCE(excluded.album,song.album),"
                "album_id=COALESCE(excluded.album_id,song.album_id),"
                "duration=COALESCE(excluded.duration,song.duration),"
                "thumb=COALESCE(excluded.thumb,song.thumb),"
                "sub=COALESCE(COALESCE(NULLIF(excluded.sub,''),song.sub), song.sub)",
                (it["id"], it.get("title", ""), artists[0][0] if artists else "",
                 artists[0][1] if artists and artists[0][1] else None,
                 it.get("album") or "", it.get("album_id") or "",
                 it.get("duration"), it.get("thumb") or "", it.get("sub") or ""))
        self.conn.commit()

    def get_song(self, song_id):
        """Ficha de una canción por id (o None si no está en la base)."""
        rows = self.conn.execute(
            "SELECT * FROM song WHERE id=?", (song_id,)).fetchall()
        return self._rows_to_items(rows)[0] if rows else None

    def _rows_to_items(self, rows):
        out = []
        for r in rows:
            out.append({
                "type": "song", "id": r["id"], "title": r["title"] or "",
                "artists": [(r["artist"] or "Desconocido", r["artist_id"])],
                "album": r["album"] or None, "album_id": r["album_id"] or None,
                "duration": r["duration"], "thumb": r["thumb"] or "",
                "sub": r["sub"] or "",
            })
        return out

    def count_songs(self):
        return (self.conn.execute("SELECT COUNT(*) FROM song").fetchone()[0])

    def count_favorites(self):
        return (self.conn.execute("SELECT COUNT(*) FROM favorite").fetchone()[0])

    def count_playlists(self):
        return (self.conn.execute("SELECT COUNT(*) FROM playlist").fetchone()[0])

    # ------------------------------------------------------------------ #
    # favoritos
    # ------------------------------------------------------------------ #
    def is_favorite(self, song_id):
        row = self.conn.execute("SELECT 1 FROM favorite WHERE id=?",
                                (song_id,)).fetchone()
        return bool(row)

    def toggle_favorite(self, song_id):
        if self.is_favorite(song_id):
            self.conn.execute("DELETE FROM favorite WHERE id=?", (song_id,))
            self.conn.commit()
            return False
        self.upsert_songs([{"id": song_id}])
        self.conn.execute("INSERT INTO favorite (id,ts) VALUES (?,?)",
                          (song_id, int(time.time())))
        self.conn.commit()
        return True

    def favorites(self):
        rows = self.conn.execute(
            "SELECT s.* FROM favorite f JOIN song s USING (id) "
            "ORDER BY f.ts DESC").fetchall()
        return self._rows_to_items(rows)

    # ------------------------------------------------------------------ #
    # historial
    # ------------------------------------------------------------------ #
    def add_history(self, song_id, max_keep=500):
        self.upsert_songs([{"id": song_id}])
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO history (id,ts) VALUES (?,?) "
            "ON CONFLICT (id) DO UPDATE SET ts=excluded.ts", (song_id, now))
        older = [r[0] for r in self.conn.execute(
            "SELECT id FROM history ORDER BY ts DESC LIMIT -1 OFFSET ?",
            (max_keep,))]
        for oid in older:
            self.conn.execute("DELETE FROM history WHERE id=?", (oid,))
        self.conn.commit()

    def history(self, limit=200):
        rows = self.conn.execute(
            "SELECT s.* FROM history h JOIN song s USING (id) "
            "ORDER BY h.ts DESC LIMIT ?", (limit,)).fetchall()
        return self._rows_to_items(rows)

    def clear_history(self):
        self.conn.execute("DELETE FROM history")
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # playlists locales
    # ------------------------------------------------------------------ #
    def playlists(self):
        rows = self.conn.execute(
            "SELECT p.pid, p.name, p.created, COUNT(ps.id) AS n "
            "FROM playlist p LEFT JOIN pl_song ps USING (pid) "
            "GROUP BY p.pid ORDER BY p.created DESC").fetchall()
        return [{"pid": r["pid"], "name": r["name"],
                 "created": r["created"], "count": r["n"] or 0} for r in rows]

    def create_playlist(self, name):
        name = name.strip()
        if not name:
            return None
        try:
            cur = self.conn.execute(
                "INSERT INTO playlist (name,created) VALUES (?,?)",
                (name, int(time.time())))
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def rename_playlist(self, pid, name):
        name = name.strip()
        if not name:
            return False
        try:
            self.conn.execute("UPDATE playlist SET name=? WHERE pid=?",
                              (name, pid))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_playlist(self, pid):
        self.conn.execute("DELETE FROM pl_song WHERE pid=?", (pid,))
        self.conn.execute("DELETE FROM playlist WHERE pid=?", (pid,))
        self.conn.commit()

    def add_to_playlist(self, pid, song_id):
        if not self.upsert_song_once(song_id):
            pass
        pos = self.conn.execute(
            "SELECT COALESCE(MAX(pos),0)+1 FROM pl_song WHERE pid=?",
            (pid,)).fetchone()[0]
        self.conn.execute(
            "INSERT OR IGNORE INTO pl_song (pid,id,pos) VALUES (?,?,?)",
            (pid, song_id, pos))
        self.conn.commit()

    def upsert_song_once(self, song):
        return song

    def remove_from_playlist(self, pid, song_id):
        row = self.conn.execute("SELECT pos FROM pl_song WHERE pid=? AND id=?",
                                (pid, song_id)).fetchone()
        if not row:
            return False
        self.conn.execute("DELETE FROM pl_song WHERE pid=? AND id=?",
                          (pid, song_id))
        self.conn.execute(
            "UPDATE pl_song SET pos=pos-1 WHERE pid=? AND pos>?",
            (pid, row["pos"]))
        self.conn.commit()
        return True

    def mirror_playlist(self, pid, song_ids):
        """Sustituye el contenido de la playlist por song_ids (en orden).

        Para «D» en una playlist remota: la copia local queda igual que
        la de YouTube (el mismo id no se repite)."""
        self.conn.execute("DELETE FROM pl_song WHERE pid=?", (pid,))
        pos = 0
        vistos = set()
        for sid in song_ids:
            if not sid or sid in vistos:
                continue
            vistos.add(sid)
            pos += 1
            self.conn.execute(
                "INSERT OR IGNORE INTO pl_song (pid,id,pos) VALUES (?,?,?)",
                (pid, sid, pos))
        self.conn.commit()

    def playlist_songs(self, pid):
        rows = self.conn.execute(
            "SELECT s.* FROM pl_song ps JOIN song s USING (id) "
            "WHERE ps.pid=? ORDER BY ps.pos", (pid,)).fetchall()
        return self._rows_to_items(rows)

    # ------------------------------------------------------------------ #
    # kv/estado
    # ------------------------------------------------------------------ #
    def get_meta(self, key, default=None):
        with self._lock:
            row = self.conn.execute("SELECT v FROM kv WHERE k=?",
                                    (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except (ValueError, TypeError):
            return row["v"]

    def set_meta(self, key, value):
        with self._lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO kv (k,v) VALUES (?,?) "
                    "ON CONFLICT (k) DO UPDATE SET v=excluded.v",
                    (key, json.dumps(value, ensure_ascii=False)))

# -*- coding: utf-8 -*-
"""Cookie de sesión de YouTube desde los navegadores instalados.

Firefox y derivados (Zen, LibreWolf, WaterFox, Floorp): leen `cookies.sqlite`
que se guarda SIN cifrar en el perfil.
Chromium y derivados (Chrome, Brave, Edge, Vivaldi, Opera): descifran
`encrypted_value` (v10: PBKDF2 «peanuts», v11: contraseña del llavero vía
`secret-tool`) con AES-128-CBC implementado en puro Python.

Solo librería estándar + subprocesos para las herramientas del sistema.
"""
import glob
import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile

# ----------------------------------------------------------------- #
# AES-128 (solo descifrado de bloque, para las cookies de Chromium)
# ----------------------------------------------------------------- #
_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B,
    0xFE, 0xD7, 0xAB, 0x76, 0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0,
    0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0, 0xB7, 0xFD, 0x93, 0x26,
    0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2,
    0xEB, 0x27, 0xB2, 0x75, 0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0,
    0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84, 0x53, 0xD1, 0x00, 0xED,
    0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F,
    0x50, 0x3C, 0x9F, 0xA8, 0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5,
    0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2, 0xCD, 0x0C, 0x13, 0xEC,
    0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14,
    0xDE, 0x5E, 0x0B, 0xDB, 0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C,
    0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79, 0xE7, 0xC8, 0x37, 0x6D,
    0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F,
    0x4B, 0xBD, 0x8B, 0x8A, 0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E,
    0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E, 0xE1, 0xF8, 0x98, 0x11,
    0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F,
    0xB0, 0x54, 0xBB, 0x16)
_SBOX_INV = [0] * 256
for _i, _v in enumerate(_SBOX):
    _SBOX_INV[_v] = _i
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _key_sched(key):
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([x ^ y for x, y in zip(w[i - 4], t)])
    return [bytes(sum(w[r * 4:(r + 1) * 4], [])) for r in range(11)]


def _dec_block(block, rngs):
    s = list(block)
    ark = lambda k: [s.__setitem__(i, s[i] ^ rngs[k][i]) for i in range(16)]

    def _isr():
        out = s[:]
        for r in range(1, 4):
            for c in range(4):
                out[4 * c + r] = s[4 * ((c - r) % 4) + r]
        s[:] = out

    def _imc():
        for c in range(4):
            b = s[4 * c:4 * c + 4]
            s[4 * c] = (_gmul(b[0], 14) ^ _gmul(b[1], 11) ^ _gmul(b[2], 13)
                        ^ _gmul(b[3], 9))
            s[4 * c + 1] = (_gmul(b[0], 9) ^ _gmul(b[1], 14) ^ _gmul(b[2], 11)
                            ^ _gmul(b[3], 13))
            s[4 * c + 2] = (_gmul(b[0], 13) ^ _gmul(b[1], 9) ^ _gmul(b[2], 14)
                            ^ _gmul(b[3], 11))
            s[4 * c + 3] = (_gmul(b[0], 11) ^ _gmul(b[1], 13)
                            ^ _gmul(b[2], 9) ^ _gmul(b[3], 14))

    ark(10)
    for rnd in range(9, 0, -1):
        _isr()
        s[:] = [_SBOX_INV[b] for b in s]
        ark(rnd)
        _imc()
    _isr()
    s[:] = [_SBOX_INV[b] for b in s]
    ark(0)
    return bytes(s)


def _aes_cbc_decrypt(data, key, iv):
    rngs = _key_sched(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data) - len(data) % 16, 16):
        blk = data[i:i + 16]
        dec = _dec_block(blk, rngs)
        out.extend((a ^ b) for a, b in zip(dec, prev))
        prev = blk
    if out:
        p = out[-1]
        if 0 < p <= 16 and out[-p:] == bytes([p]) * p:
            del out[-p:]
    return bytes(out)


# ----------------------------------------------------------------- #
# detección de navegadores
# ----------------------------------------------------------------- #
FIREFOX_ROOTS = (
    ("~/.mozilla", "Firefox"),
    ("~/.zen", "Zen"),
    ("~/.config/zen", "Zen"),
    ("~/.librewolf", "LibreWolf"),
    ("~/.config/librewolf", "LibreWolf"),
    ("~/.waterfox", "Waterfox"),
    ("~/.config/waterfox", "Waterfox"),
    ("~/.config/floorp", "Floorp"),
    ("~/.floorp", "Floorp"),
    ("~/snap/firefox/common/.mozilla", "Firefox"),
    ("~/snap/zen-browser/common/.zen", "Zen"),
)

CHROMIUM_BROWSERS = (
    ("chrome", "~/.config/google-chrome"),
    ("chrome-beta", "~/.config/google-chrome-beta"),
    ("chromium", "~/.config/chromium"),
    ("brave", "~/.config/BraveSoftware/Brave-Browser"),
    ("edge", "~/.config/microsoft-edge"),
    ("vivaldi", "~/.config/vivaldi"),
    ("opera", "~/.config/opera"),
)

SAFE_STORAGE_NAMES = {
    "chrome": "Chrome Safe Storage",
    "chrome-beta": "Chrome Safe Storage",
    "chromium": "Chromium Safe Storage",
    "brave": "Brave Safe Storage",
    "edge": "Microsoft Edge Safe Storage",
    "vivaldi": "Vivaldi Safe Storage",
    "opera": "Opera Safe Storage",
}


def _firefox_cookie_files(roots=None):
    """[(ruta_de_cookies.sqlite, etiqueta), …] para las raíces indicadas.

    `roots` admite la lista por defecto (pares raíz/etiqueta) o una lista
    de rutas sueltas (la etiqueta se deduce del nombre si no hay pareja).
    """
    out = []
    for item in (roots or FIREFOX_ROOTS):
        if isinstance(item, str):
            root, label = item, None
        elif isinstance(item, (list, tuple)) and len(item) == 2 \
                and isinstance(item[0], str):
            root, label = item
        else:
            continue
        exp = os.path.expanduser(root)
        if not os.path.isdir(exp):
            continue
        guess = None
        low = root.lower()
        for name in ("zen", "librewolf", "waterfox", "floorp", "firefox"):
            if name in low:
                guess = name.capitalize()
                break
        label = label or guess or "Firefox"
        for f in (sorted(glob.glob(exp + "/*/cookies.sqlite"))
                  + sorted(glob.glob(exp + "/*/*/cookies.sqlite"))):
            out.append((f, label))
    return out


def _read_sqlite_copy(path, sql):
    """sqlite3 sobre una copia temporaria (la BD puede estar en uso)."""
    tmp = tempfile.mkstemp(prefix="mterm_ck")[1]
    try:
        shutil.copy2(path, tmp)
        if os.path.exists(path + "-wal"):
            shutil.copy2(path + "-wal", tmp + "-wal")
        con = sqlite3.connect(tmp)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return []
    finally:
        for extra in ("", "-wal"):
            try:
                os.remove(tmp + extra)
            except OSError:
                pass


def _sqlite_cookie_pairs(path):
    # Solo cookies del dominio YouTube: InnerTube valida la sesión contra
    # las que un navegador enviaría a music.youtube.com (.google.com no).
    sql = ("select name, host, value from moz_cookies "
           "where (host like '%.youtube.com' or host = 'youtube.com' "
           "or host = 'music.youtube.com') "
           "and (expiry = 0 or expiry > strftime('%s','now'))")
    return _read_sqlite_copy(path, sql)


def _secret_password(app):
    if shutil.which("secret-tool"):
        short = app.split("-")[0]
        variants = (
            ("application", short, "service",
             SAFE_STORAGE_NAMES.get(app, f"{app} Safe Storage")),
            ("application", short, "service", "chrome Safe Storage"),
            ("application", "chrome", "service",
             SAFE_STORAGE_NAMES.get(app, f"{app} Safe Storage")),
            (short, SAFE_STORAGE_NAMES.get(app, "Chrome Safe Storage")),
        )
        for attrs in variants:
            try:
                out = subprocess.run(["secret-tool", "lookup", *attrs],
                                     capture_output=True, timeout=6,
                                     text=True)
                if out.returncode == 0 and out.stdout:
                    return out.stdout.strip().encode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def _keyring_passwords(app):
    out = []
    p = _secret_password(app)
    if p:
        out.append(p)
    peanuts = b"peanuts"
    if peanuts not in out:
        out.append(peanuts)        # v10 clásica y último recurso
    return out


def _decrypt_value(blob, pwds):
    if len(blob) < 4 or blob[:3] not in (b"v10", b"v11"):
        return None
    raw = blob[3:]
    for pw in pwds:
        cand = hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1, 16)
        try:
            out = _aes_cbc_decrypt(raw, cand, b" " * 16)
        except Exception:
            continue
        txt = out.decode("utf-8", "replace").strip("\x00\x0b\x1f")
        if txt and all(32 <= ord(ch) < 0x10000 and ch != "\x7f"
                       for ch in txt):
            return txt
    return None


def _profile_cookie_pairs(app, cookies_file, pwds):
    sql = ("select name, host_key, encrypted_value, value from cookies "
           "where (host_key like '%.youtube.com' or host_key = 'youtube.com' "
           "or host_key = 'music.youtube.com')")
    rows = _read_sqlite_copy(cookies_file, sql)
    pairs = []
    for name, _host, enc, plain in rows:
        val = plain or _decrypt_value(enc, pwds or [])
        if val:
            pairs.append(f"{name}={val}")
    return pairs


ESSENTIAL_COOKIES = (
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    "LOGIN_INFO", "VISITOR_INFO1_LIVE",
)


def _essential(pairs):
    """Cabecera Cookie razonable: íntegras (el conjunto de youtube ya es
    pequeño) o núcleo + resto si desborda."""
    hdr = "; ".join(pairs)
    if len(hdr) <= 12000 or not any(
            p.startswith(("SAPISID=", "__Secure-3PAPISID=")) for p in pairs):
        return pairs
    return [p for p in pairs
            if p.split("=", 1)[0] in ESSENTIAL_COOKIES] or pairs


def scan_sapisid_cookie(roots=None, chromium_bases=None):
    """Busca la cookie de sesión de YouTube en los navegadores instalados.

    Devuelve (cookie_str, etiqueta_del_navegador) o (None, None).
    `roots`/`chromium_bases` permiten probar perfiles falsos en los tests.
    """
    best = None                     # (npairs, pairs, etiqueta, prioridad)

    def adopt(pairs, label, prio):
        nonlocal best
        if not pairs:
            return
        cand = (len(pairs), pairs, label, prio)
        if best is None or cand[0] > best[0] or \
                (cand[0], cand[3]) > (best[0], best[3]):
            best = cand

    for path, label in _firefox_cookie_files(roots):
        rows = _sqlite_cookie_pairs(path)
        names = {n for n, _h, _v in rows}
        if not any(n in names for n in ("SAPISID", "__Secure-3PAPISID",
                                        "__Secure-1PAPISID")):
            continue
        pairs = [f"{n}={v}" for n, _h, v in rows if v]
        host_is_yt = any(".youtube.com" in h for _n, h, _v in rows)
        src = os.path.basename(os.path.dirname(path))
        adopt(pairs, f"{label} ({src[:22]})", 3 if host_is_yt else 2)
    for app, base in CHROMIUM_BROWSERS:
        b = os.path.expanduser(chromium_bases.get(app, base)
                               if chromium_bases else base)
        if not os.path.isdir(b):
            continue
        pwds = None
        for cfile in sorted(glob.glob(b + "/*/Cookies")):
            if pwds is None:
                pwds = _keyring_passwords(app)
            adopt(_profile_cookie_pairs(app, cfile, pwds),
                  f"{app} ({os.path.basename(os.path.dirname(cfile))[:18]})",
                  2)
    if not best:
        return None, None
    return "; ".join(_essential(best[1])), best[2]

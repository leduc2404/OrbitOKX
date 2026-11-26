"""OKX Square x-sec-token generator (pure Python, stdlib only).

Reverse-engineered from okfe trade-pages asset 64889-DwjuZBs_.js (generateSecToken).

Token layout (4 dot-separated segments):
    seg0 = base64(JSON({rid, did, st, v:1}))
    seg1 = base64( AES-ECB(fingerprint_json + PKCS7) + 13 random bytes )
    seg2 = base64( AES-ECB(sha256hex(METHOD + path + st) + PKCS7) + 13 random bytes )
    seg3 = "web"

AES key = sha256hex(SALT + st)[:16] used as raw ASCII bytes (AES-128).
The cipher is standard AES-128-ECB except the S-box is custom (embedded below).
SALT = atob("Q29tT2tpbmNPa2V4Vmlld0NvbnRyb2xsZXI=") = "ComOkincOkexViewController".

Verified against the captured HAR publish token: decrypting recovers the exact
browser fingerprint and sha256("POST/priapi/v5/content/ugc/publish" + st).
"""
import base64
import hashlib
import json
import os
import time
import uuid

SALT = "ComOkincOkexViewController"

# Custom 256-byte S-box extracted from the SDK (first 256 of a 259-entry array).
SBOX = bytes.fromhex(
    "28c2e857482497c5188c9b72f799ded385587a0b44d1b047c7aef06d53e160b4"
    "4b5c10520139b990ab00d47364329fa16a1c6b21f1ef3f6795f279e00cd0667f"
    "2cc3125a34eb15a6bc7da0b85549c1fbe9083ce2562f309e0ecb25e3465dec4c"
    "09be870489d8193d71407e5f167b3a274376458a3596f9078f0f54cfba3883ce"
    "2aac68f48031a4fe93b37ccab675daa8d7b137c46c6fd54f231498aadc657442"
    "c8418da522ea4db517021e889133c6783b2dd9fc1b9acde61fed92294afa1aee"
    "0de7638eff063e1df8ade5360570a26984bfa98b03d6262e826281e494a7af5e"
    "9dd2b2862b51ccdd13f57750c0b70a594ec9bb11a36edbf6619c20dffdbd5bf3"
)
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i

# Real browser fingerprint captured from the HAR publish request. Static device
# fields are reused; m2/m3/m4 are refreshed per token, d0 is set to devId.
DEFAULT_FINGERPRINT = {
    "m0": "1.0", "m1": 1, "m2": "0", "m3": "", "m4": "",
    "d0": "", "d1": "Chrome/157.0.0.0", "d2": 5, "d3": 1032175,
    "d4": 0, "d5": 0, "d6": "Win32",
    "e0": -2, "e1": 0, "e2": 0, "e3": 0, "e4": 0, "e5": 0, "e6": 0,
    "e7": 0, "e8": 0, "e9": 0, "e10": 0, "e11": 0, "e12": 0,
}
DEFAULT_PATHNAME = "/vi/orbit"


def _xtime(a):
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _expand_key(key):
    w = list(key)
    rcon = 1
    for i in range(4, 44):
        t = w[(i - 1) * 4:i * 4]
        if i % 4 == 0:
            t = [SBOX[b] for b in (t[1:] + t[:1])]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        w += [w[(i - 4) * 4 + j] ^ t[j] for j in range(4)]
    return [w[i * 16:(i + 1) * 16] for i in range(11)]


def _enc_block(pt, rk):
    s = [pt[i] ^ rk[0][i] for i in range(16)]
    for rnd in range(1, 11):
        s = [SBOX[b] for b in s]
        t = s[:]
        for r in range(1, 4):
            for c in range(4):
                s[r + 4 * c] = t[r + 4 * ((c + r) % 4)]
        if rnd != 10:
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                s[4 * c + 0] = _gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3]
                s[4 * c + 1] = a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3]
                s[4 * c + 2] = a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3)
                s[4 * c + 3] = _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)
        s = [s[i] ^ rk[rnd][i] for i in range(16)]
    return bytes(s)


def _dec_block(ct, rk):
    s = [ct[i] ^ rk[10][i] for i in range(16)]
    for rnd in range(9, -1, -1):
        t = s[:]
        for r in range(1, 4):
            for c in range(4):
                s[r + 4 * c] = t[r + 4 * ((c - r) % 4)]
        s = [INV_SBOX[b] for b in s]
        s = [s[i] ^ rk[rnd][i] for i in range(16)]
        if rnd != 0:
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                s[4 * c + 0] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
                s[4 * c + 1] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
                s[4 * c + 2] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
                s[4 * c + 3] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)
    return bytes(s)


def _aes_ecb_encrypt(data, key):
    rk = _expand_key(key)
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    return b"".join(_enc_block(data[i:i + 16], rk) for i in range(0, len(data), 16))


def _aes_ecb_decrypt(ct, key):
    rk = _expand_key(key)
    out = b"".join(_dec_block(ct[i:i + 16], rk) for i in range(0, len(ct), 16))
    return out[:-out[-1]]


def _derive_key(ts):
    keyhex = hashlib.sha256((SALT + ts).encode()).hexdigest()
    return keyhex[:16].encode("ascii")


def _seal(plaintext, ts):
    """AES-ECB(plaintext + PKCS7) + 13 random bytes, base64-encoded."""
    body = _aes_ecb_encrypt(plaintext.encode("utf-8"), _derive_key(ts))
    return base64.b64encode(body + os.urandom(13)).decode("ascii")


def generate_sec_token(method, path, devid, fingerprint=None, pathname=DEFAULT_PATHNAME, ts=None):
    """Build a fresh x-sec-token for a request.

    method/path: e.g. "POST", "/priapi/v5/content/ugc/publish" (path only, no query).
    devid: device id (cookie devId).
    fingerprint: optional full fingerprint dict; defaults to the captured template.
    """
    ts = str(ts if ts is not None else int(time.time() * 1000))
    fp = dict(DEFAULT_FINGERPRINT if fingerprint is None else fingerprint)
    fp["m2"] = str(int(time.time() * 1000))
    fp["m3"] = uuid.uuid4().hex
    fp["m4"] = hashlib.sha256(pathname.encode()).hexdigest()
    fp["d0"] = devid

    meta = {"rid": str(uuid.uuid4()), "did": devid, "st": ts, "v": 1}
    seg0 = base64.b64encode(json.dumps(meta, separators=(",", ":")).encode()).decode("ascii")
    seg1 = _seal(json.dumps(fp, separators=(",", ":")), ts)
    req_hash = hashlib.sha256((method.upper() + path + ts).encode()).hexdigest()
    seg2 = _seal(req_hash, ts)
    return f"{seg0}.{seg1}.{seg2}.web"


def decrypt_sec_token(token):
    """Decode a token back to (meta, fingerprint_dict, request_hash). For verification."""
    seg0, seg1, seg2, _ = token.split(".")
    meta = json.loads(base64.b64decode(seg0))
    key = _derive_key(meta["st"])
    fp = json.loads(_aes_ecb_decrypt(base64.b64decode(seg1)[:-13], key))
    rh = _aes_ecb_decrypt(base64.b64decode(seg2)[:-13], key).decode()
    return meta, fp, rh
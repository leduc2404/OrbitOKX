"""Request-signing primitives reverse-engineered from OKX's ont.js SDK.

Three independent mechanisms:
  * x-client-signature : ECDSA P-256 / SHA-256, raw r||s (P1363), "{P1363}" prefix.
  * ok-verify-sign     : HMAC-SHA256 with a time-derived hex-index key (getTokenAndSign).
  * build_content      : the exact string that gets signed for a request.

All functions are pure (key material passed in) so they can be unit-tested offline.
"""
import base64
import hashlib
import hmac as _hmac
import time
import uuid
from urllib.parse import urlparse

from cryptography.hazmat.primitives.serialization import load_der_private_key
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives import hashes


def _b64d(s):
    return base64.b64decode(s)


def _b64e(b):
    return base64.b64encode(b).decode()


def ecdsa_sign_p1363(content: str, pkcs8_b64: str) -> str:
    key = load_der_private_key(_b64d(pkcs8_b64), password=None)
    der = key.sign(content.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return "{P1363}" + _b64e(raw)


def ecdsa_verify_p1363(content: str, sig_str: str, pkcs8_b64: str) -> bool:
    key = load_der_private_key(_b64d(pkcs8_b64), password=None)
    pub = key.public_key()
    raw = _b64d(sig_str.replace("{P1363}", ""))
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der = utils.encode_dss_signature(r, s)
    try:
        pub.verify(der, content.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def hmac_sign(content: str, raw_b64: str) -> str:
    return _b64e(_hmac.new(_b64d(raw_b64), content.encode("utf-8"), hashlib.sha256).digest())


def build_content(method, url, body, ts):
    p = urlparse(url)
    if method.upper() == "GET":
        return p.path + p.query + str(ts)
    return p.path + (body or "") + str(ts)


def okx_verify(path, body, ts_ms=None):
    """Reproduce getTokenAndSign -> (ok-verify-token, ok-timestamp, ok-verify-sign)."""
    token = str(uuid.uuid4())
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    l = ts // 1000
    f = (l // 600) % 32
    d = (l // 3600) % 32
    hexd = hashlib.sha256(token.encode()).hexdigest()
    r = "".join(hexd[(f + (d + p) * p) % 32] for p in range(32))
    sign = _b64e(_hmac.new(r.encode(), (path + (body or "")).encode(), hashlib.sha256).digest())
    return token, str(ts), sign
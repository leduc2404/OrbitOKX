"""Configuration + per-account secret loading for the OKX Square pipeline.

Secrets live OUTSIDE the package (gitignored), referenced per account:
    config/cookies/<account>.json  - browser cookies (token JWT, devId, ok_site_info, ...)
    config/keys/<account>.json     - {ecdsa_pkcs8_b64, hmac_raw_b64}

Module-level defaults (used by the CLI / selftest) can be overridden with env vars
OKX_COOKIE_FILE / OKX_KEYS_FILE / OKX_CAPTURE_FILE / OKX_HAR_FILE so verification can
point at existing capture files without copying secrets into the repo.

These files are private to the account owner; do not commit or share them.
"""
import base64
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _root_file(env: str, default_rel: str) -> Path:
    v = os.environ.get(env)
    return Path(v) if v else PROJECT_ROOT / default_rel


COOKIE_FILE = _root_file("OKX_COOKIE_FILE", "config/cookies/default.json")
KEYS_FILE = _root_file("OKX_KEYS_FILE", "config/keys/default.json")
CAPTURE_FILE = _root_file("OKX_CAPTURE_FILE", "okx-capture.json")
HAR_FILE = _root_file("OKX_HAR_FILE", "www.okx.com.har")

BASE = "https://www.okx.com"

PRESIGN_PATH = "/priapi/v5/content/image/presign"
PUBLISH_PATH = "/priapi/v5/content/ugc/publish"

# Client-side session tracing id captured from the browser (not secret).
TRACE_ID = "3120343947589070004"

LOCALE = "vi_VN"
UTC = "7"
REFERER_PATH = "/vi/orbit"


def resolve_path(p) -> Path:
    pp = Path(p)
    return pp if pp.is_absolute() else PROJECT_ROOT / pp


def load_cookies(path=None):
    return json.loads(Path(path or COOKIE_FILE).read_text(encoding="utf-8"))


def cookie_value(cookies, name):
    return next((c["value"] for c in cookies if c["name"] == name), None)


def load_keys(path=None):
    return json.loads(Path(path or KEYS_FILE).read_text(encoding="utf-8"))


def extract_keys():
    """Derive keys.json from the runtime capture (okx-capture.json)."""
    cap = json.loads(CAPTURE_FILE.read_text(encoding="utf-8"))
    ecdsa = next(x for x in cap["imports"] if x["algo"] == "ECDSA" and x["format"] == "pkcs8")
    hm = next(x for x in cap["imports"] if x["algo"] == "HMAC")
    keys = {"ecdsa_pkcs8_b64": ecdsa["keyB64"], "hmac_raw_b64": hm["keyB64"]}
    KEYS_FILE.write_text(json.dumps(keys, indent=1), encoding="utf-8")
    return keys


def token_expiry(token):
    """Decode the JWT `exp` claim (no signature verification). Epoch seconds or None."""
    if not token:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


class Config:
    """Resolved credentials + constants for ONE account session."""

    def __init__(self, cookies_file=None, keys_file=None):
        self.cookies_file = resolve_path(cookies_file) if cookies_file else COOKIE_FILE
        self.keys_file = resolve_path(keys_file) if keys_file else KEYS_FILE
        self.cookies = load_cookies(self.cookies_file)
        self.cookie_hdr = "; ".join(f"{c['name']}={c['value']}" for c in self.cookies)
        self.token = cookie_value(self.cookies, "token")
        self.devid = cookie_value(self.cookies, "devId")
        self.site_info = cookie_value(self.cookies, "ok_site_info")
        self.keys = load_keys(self.keys_file)
        self.base = BASE
        self.trace_id = TRACE_ID
        self.token_exp = token_expiry(self.token)

    @classmethod
    def from_account(cls, acc: dict):
        """Build from a posting_accounts entry (cookies_file / keys_file references)."""
        return cls(cookies_file=acc.get("cookies_file"), keys_file=acc.get("keys_file"))
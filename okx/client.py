"""Authenticated HTTP client for OKX Square.

Reproduces the browser's signed requests (x-client-signature) and, for publish,
the ok-verify-* trio plus x-sec-token. TLS is impersonated as Chrome via curl_cffi
to pass Cloudflare fingerprinting (plain Node/requests gets 403).

Upload flow per image: presign -> PUT raw bytes to the signed OSS URL (no auth,
no Content-Type) -> use the returned imageUrl (OSS key) in publish.imageList.
"""
import io
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from curl_cffi import requests as cffi
from PIL import Image

from . import config
from .crypto import ecdsa_sign_p1363, okx_verify
from .sectoken import generate_sec_token


class OkxError(RuntimeError):
    pass


def image_size(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        return im.size  # (width, height)


class OkxClient:
    def __init__(self, cfg=None, impersonate="chrome", proxy=None, accept_language=None):
        self.cfg = cfg or config.Config()
        self.impersonate = impersonate
        self.proxy = proxy or None
        self.accept_language = accept_language
        self._counter = 200
        self._session = None

    def _get_session(self):
        """One persistent impersonated session per account (own TLS fingerprint + proxy)."""
        if self._session is None:
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            self._session = cffi.Session(impersonate=self.impersonate, proxies=proxies)
        return self._session

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _next_id_group(self):
        self._counter += 1
        return f"{self.cfg.trace_id}-c-{self._counter}"

    def signed_request(self, method, path, body=None, query=None, publish=False):
        cfg = self.cfg
        ts = str(int(time.time() * 1000))
        method = method.upper()
        if method == "GET":
            q = dict(query or {})
            q["t"] = ts
            qs = urlencode(q)
            url = f"{cfg.base}{path}?{qs}"
            content = path + qs + ts
            body_str = None
        else:
            url = f"{cfg.base}{path}?t={ts}"
            body_str = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            content = path + body_str + ts

        sig = ecdsa_sign_p1363(content, cfg.keys["ecdsa_pkcs8_b64"])
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": cfg.token,
            "Cookie": cfg.cookie_hdr,
            "Devid": cfg.devid,
            "Platform": "web",
            "Origin": cfg.base,
            "Referer": cfg.base + config.REFERER_PATH,
            "X-Cdn": cfg.base,
            "X-Utc": config.UTC,
            "X-Locale": config.LOCALE,
            "X-Zkdex-Env": "0",
            "X-Simulated-Trading": "undefined",
            "X-Site-Info": cfg.site_info,
            "X-Request-Timestamp": ts,
            "X-Client-Signature": sig,
            "X-Client-Signature-Version": "1.3",
            "X-Id-Group": self._next_id_group(),
        }
        if self.accept_language:
            headers["Accept-Language"] = self.accept_language
        if publish:
            vt, ots, vs = okx_verify(path, body_str, ts_ms=int(ts))
            headers["Ok-Timestamp"] = ots
            headers["Ok-Verify-Sign"] = vs
            headers["Ok-Verify-Token"] = vt
            headers["X-Sec-Token"] = generate_sec_token(method, path, cfg.devid, ts=ts)

        s = self._get_session()
        resp = s.request(method, url, headers=headers,
                         data=body_str.encode("utf-8") if body_str else None, timeout=30)
        return resp

    def presign(self):
        """Request an OSS upload slot. Returns {imageUrl, presignUrl}."""
        resp = self.signed_request("POST", config.PRESIGN_PATH, body={"requestId": str(uuid.uuid4())})
        data = _okx_json(resp)
        return data

    def _put_bytes(self, url, data):
        # OSS presigned PUT: raw bytes only, no auth headers, no Content-Type
        # (curl_cffi/libcurl defaults to application/octet-stream unless explicitly
        # suppressed with Content-Type: None, which breaks the Aliyun STS signature).
        s = self._get_session()
        last_err = None
        for attempt in range(1, 4):
            try:
                resp = s.put(url, data=data, headers={"Content-Type": None}, timeout=60)
                if resp.status_code == 200:
                    return resp
                if resp.status_code < 500:
                    raise OkxError(f"OSS PUT failed: {resp.status_code} {resp.text[:200]}")
                last_err = OkxError(f"OSS PUT failed: {resp.status_code} {resp.text[:200]}")
            except OkxError:
                raise
            except Exception as e:
                last_err = OkxError(f"OSS PUT error: {e}")
            if attempt < 3:
                time.sleep(attempt)
        raise last_err

    def upload_image(self, path):
        """presign + PUT one local image; return the imageList entry dict."""
        data = Path(path).read_bytes()
        w, h = image_size(data)
        slot = self.presign()
        self._put_bytes(slot["presignUrl"], data)
        return {"url": slot["imageUrl"], "width": str(w), "height": str(h), "fileSize": str(len(data))}


def _okx_json(resp):
    if resp.status_code != 200:
        raise OkxError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    if str(payload.get("code")) != "0":
        raise OkxError(f"OKX code={payload.get('code')} msg={payload.get('msg')}")
    return payload["data"]
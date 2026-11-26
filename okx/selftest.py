"""Offline verification of every reversed mechanism against captured traffic.

No network. Run with: python -m okx selftest
  * ECDSA: verify captured x-client-signature values with the extracted public key.
  * Formula: rebuild signed content from each request and compare to the capture.
  * HMAC: reproduce ok-verify-sign for the captured publish request.
  * x-sec-token: decrypt the captured publish token, check the request hash and
    fingerprint round-trip.
"""
import hashlib
import json
from urllib.parse import urlparse

from . import config
from .crypto import ecdsa_sign_p1363, ecdsa_verify_p1363, hmac_sign, build_content
from .sectoken import decrypt_sec_token, generate_sec_token


def _hdr(req, name):
    for k, v in req.get("headers", {}).items():
        if k.lower() == name:
            return v
    return None


def verify_crypto():
    cap = json.loads(config.CAPTURE_FILE.read_text(encoding="utf-8"))
    keys = config.load_keys()
    signs, reqs = cap["signs"], cap["requests"]

    ok = bad = nofound = 0
    print("=== ECDSA: verify captured x-client-signature with extracted public key ===")
    for r in reqs:
        sig = _hdr(r, "x-client-signature")
        ts = _hdr(r, "x-request-timestamp")
        if not sig or not ts:
            continue
        path = urlparse(r["url"]).path
        cand = [s for s in signs if s.get("algo") == "ECDSA"
                and s["content"].startswith(path) and s["content"].endswith(str(ts))]
        if not cand:
            nofound += 1
            continue
        if ecdsa_verify_p1363(cand[0]["content"], sig, keys["ecdsa_pkcs8_b64"]):
            ok += 1
        else:
            bad += 1
            print("  INVALID", r["method"], path)
    print(f"  VALID={ok}  INVALID={bad}  NO_CONTENT_FOUND={nofound}")
    sample = signs[0]["content"]
    fresh = ecdsa_sign_p1363(sample, keys["ecdsa_pkcs8_b64"])
    print("  fresh-signature self-verify:", ecdsa_verify_p1363(sample, fresh, keys["ecdsa_pkcs8_b64"]))

    print("\n=== Formula: rebuild signed content from request, compare to capture ===")
    fok = fbad = 0
    for r in reqs:
        ts = _hdr(r, "x-request-timestamp")
        if not ts:
            continue
        body = r.get("body") if r.get("body") != "<non-string>" else ""
        c = build_content(r["method"], r["url"], body, ts)
        if any(s["content"] == c for s in signs):
            fok += 1
        else:
            fbad += 1
            if fbad <= 4:
                print("  FORMULA MISS", r["method"], urlparse(r["url"]).path, "=>", c[:90])
    print(f"  FORMULA_MATCH={fok}  FORMULA_MISS={fbad}")

    print("\n=== HMAC ok-verify-sign (publish) ===")
    for r in reqs:
        ov = _hdr(r, "ok-verify-sign")
        if not ov:
            continue
        path = urlparse(r["url"]).path
        body = r.get("body") if r.get("body") != "<non-string>" else ""
        mine = hmac_sign(path + (body or ""), keys["hmac_raw_b64"])
        print("  ", r["method"], path, "HMAC_MATCH=" + str(mine == ov))


def verify_sectoken():
    print("\n=== x-sec-token: decrypt captured publish token ===")
    har = json.loads(config.HAR_FILE.read_text(encoding="utf-8"))
    tok = next(h["value"] for h in har["log"]["entries"][25]["request"]["headers"]
               if h["name"].lower() == "x-sec-token")
    meta, fp, rh = decrypt_sec_token(tok)
    expect = hashlib.sha256(("POST/priapi/v5/content/ugc/publish" + meta["st"]).encode()).hexdigest()
    print("  meta did/st/v:", meta["did"], meta["st"], meta["v"])
    print("  fingerprint d1/d3/d6:", fp["d1"], fp["d3"], fp["d6"])
    print("  request-hash matches:", rh == expect)
    re_tok = generate_sec_token("POST", "/priapi/v5/content/ugc/publish",
                                meta["did"], fingerprint=fp, ts=meta["st"])
    _, fp2, rh2 = decrypt_sec_token(re_tok)
    # m2/m3 are intentionally refreshed per token; compare static fields only.
    static_equal = all(fp2[k] == fp[k] for k in fp if k not in ("m2", "m3"))
    print("  round-trip static fingerprint equal:", static_equal)
    print("  round-trip hash equal:", rh2 == rh)


def run():
    verify_crypto()
    verify_sectoken()
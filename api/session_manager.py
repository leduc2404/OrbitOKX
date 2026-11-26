"""Per-account TLS fingerprint diversity for OKX — pure Python, no Node.

OKX signs requests in pure Python (okx/crypto + okx/sectoken), so the Binance
Node header_generator is dropped. This module only diversifies the curl_cffi
TLS impersonation profile per account (sha256-seeded, stable across restarts)
and builds a ready-to-use OkxClient bound to that account's cookies/keys/proxy.
"""
import hashlib

from okx.config import Config
from okx.client import OkxClient

# TLS fingerprint pool (curl_cffi impersonate targets)
_SM_IMPERSONATE = [
    "chrome120", "chrome124", "chrome131", "chrome133", "chrome136",
    "edge99", "edge101", "safari17_0",
]

_SM_LANGUAGES = [
    "vi,en-US;q=0.9,en;q=0.8",
    "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "en-US,en;q=0.9,vi;q=0.8",
    "vi,vi-VN;q=0.9,en-US;q=0.8,en;q=0.7",
    "en,vi-VN;q=0.9,vi;q=0.8,en-US;q=0.7",
    "vi-VN,vi;q=0.9,en;q=0.8,en-GB;q=0.7",
    "vi,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.9,vi-VN;q=0.8,vi;q=0.7",
]

_SM_BRANDS = [
    '"Not_A Brand";v="8"',
    '"Not/A)Brand";v="8"',
    '"Not.A/Brand";v="8"',
    '"Not A(Brand";v="99"',
    '"Not)A;Brand";v="24"',
]


def _pick(h: str, pool, offset: int = 0):
    return pool[int(h[offset:offset + 4], 16) % len(pool)]


def fingerprint_for(account_id) -> dict:
    """Stable per-account fingerprint derived from sha256(account_id)."""
    h = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()
    return {
        "impersonate": _pick(h, _SM_IMPERSONATE, 0),
        "accept_language": _pick(h, _SM_LANGUAGES, 4),
        "not_a_brand": _pick(h, _SM_BRANDS, 8),
    }


def build_client(account: dict, account_id=None) -> OkxClient:
    """Create an OkxClient bound to one account (own cookies/keys/proxy/fingerprint).

    account: a posting_accounts entry. An explicit `fingerprint` overrides the
    auto-selected impersonate profile.
    """
    aid = account_id if account_id is not None else account.get("name", "account")
    fp = fingerprint_for(aid)
    impersonate = account.get("fingerprint") or fp["impersonate"]
    cfg = Config.from_account(account)
    return OkxClient(
        cfg=cfg,
        impersonate=impersonate,
        proxy=account.get("proxy") or None,
        accept_language=fp["accept_language"],
    )
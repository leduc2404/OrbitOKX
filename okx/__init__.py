"""OKX Square automation pipeline (personal-account use).

Public API:
    from okx import publish, OkxClient
    publish(text, images=["a.jpg", "b.png"], tags=["BTC", "ETH"], dry_run=True)

CLI:
    python -m okx post --text "..." --image a.jpg --image b.png --tag BTC --tag ETH
    python -m okx selftest      # offline verification against captured traffic
    python -m okx sectoken      # offline x-sec-token demo
    python -m okx presign       # live presign diagnostic
"""
from .client import OkxClient, OkxError
from .square import (
    publish,
    build_publish_body,
    build_cash_tag_list,
    normalize_tags,
    ensure_tags_in_content,
)

__version__ = "1.0.0"

__all__ = [
    "OkxClient", "OkxError", "publish", "build_publish_body",
    "build_cash_tag_list", "normalize_tags", "ensure_tags_in_content",
]
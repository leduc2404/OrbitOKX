"""Crawler bridge — runs OKXSquare's real crawler modules against TestStorage."""
from __future__ import annotations

import math
import re
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from services.crawler.binance_crawler import BinanceCrawler
from services.crawler.kucoin_crawler import KucoinCrawler
from services.crawler.okx_crawler import OkxCrawler
from services.crawler.soso_crawler import SosoValueCrawler

from test_rewrite.config_loader import StudioConfigLoader
from test_rewrite.log_buffer import LogBuffer
from test_rewrite.storage import TestStorage


def _parse_binance_authors(raw_input: Any, default_authors: List[dict]) -> List[dict]:
    """Accept list of dicts or string of UIDs (comma/newline separated)."""
    authors: List[dict] = []
    seen = set()

    for a in (default_authors or []):
        if isinstance(a, dict) and a.get("userid"):
            uid = str(a["userid"]).strip()
            if uid and uid not in seen:
                seen.add(uid)
                authors.append({"userid": uid, "username": a.get("username") or uid})
        elif isinstance(a, str) and a.strip():
            uid = a.strip()
            if uid not in seen:
                seen.add(uid)
                authors.append({"userid": uid, "username": uid})

    if isinstance(raw_input, str) and raw_input.strip():
        for token in re.split(r"[\n,;]+", raw_input):
            token = token.strip()
            if not token:
                continue
            # Allow "username:userid" or plain UID or profile URL
            m_url = re.search(r"profile/([^/?#]+)", token)
            if m_url:
                uid = m_url.group(1).strip()
                uname = uid
            elif ":" in token:
                uname, uid = [x.strip() for x in token.split(":", 1)]
            else:
                uid = token
                uname = token
            if uid and uid not in seen:
                seen.add(uid)
                authors.append({"userid": uid, "username": uname or uid})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, dict) and item.get("userid"):
                uid = str(item["userid"]).strip()
                if uid and uid not in seen:
                    seen.add(uid)
                    authors.append({"userid": uid, "username": item.get("username") or uid})
            elif isinstance(item, str) and item.strip():
                uid = item.strip()
                if uid not in seen:
                    seen.add(uid)
                    authors.append({"userid": uid, "username": uid})

    return authors


def _parse_binance_post_ids(raw_input: Any) -> List[str]:
    """Extract Binance Square post IDs from raw text, IDs, or URLs."""
    if not raw_input:
        return []
    text = raw_input if isinstance(raw_input, str) else "\n".join(str(x) for x in raw_input)
    ids: List[str] = []
    seen = set()
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        m = re.search(r"/post/(\d+)", token)
        pid = m.group(1) if m else (token if token.isdigit() else "")
        if pid and pid not in seen:
            seen.add(pid)
            ids.append(pid)
    return ids


def _parse_tokens(raw_input: Any) -> List[str]:
    """Parse comma/newline/semicolon/space separated tags or keywords."""
    if not raw_input:
        return []
    if isinstance(raw_input, list):
        raw_list = [str(x) for x in raw_input]
    else:
        raw_list = re.split(r"[\n,;\s]+", str(raw_input))
    out: List[str] = []
    seen = set()
    for tok in raw_list:
        val = tok.strip().lstrip("#$")
        if not val:
            continue
        key = val.lower()
        if key not in seen:
            seen.add(key)
            out.append(val)
    return out


class CrawlerBridge:
    """Executes real project crawlers (KuCoin, SoSoValue, Binance, OKX) into TestStorage."""

    def __init__(
        self,
        storage: TestStorage,
        config_loader: StudioConfigLoader,
        logs: LogBuffer,
    ):
        self.storage = storage
        self.config_loader = config_loader
        self.logs = logs
        self._topics_cache: Dict[str, Any] = {"ts": 0.0, "topics": []}

    def _build_okx_client(self, profiles_cfg: dict):
        accounts = profiles_cfg.get("posting_accounts") or []
        for idx, acc in enumerate(accounts, start=1):
            if not acc.get("enabled", True):
                continue
            try:
                from api.session_manager import build_client
                return build_client(acc, account_id=idx)
            except Exception as e:
                self.logs.warning("CRAWL", f"OKX account #{idx} không dựng được client: {e}")
        return None

    def get_okx_trending_topics(self, force: bool = False) -> Dict[str, Any]:
        """Return default OKX hashtags + live trending topics from OKX Orbit."""
        from services.crawler.okx_crawler import DEFAULT_OKX_HASHTAGS
        now = time.time()
        if not force and self._topics_cache["topics"] and (now - self._topics_cache["ts"] < 300):
            return {
                "ok": True,
                "default_hashtags": list(DEFAULT_OKX_HASHTAGS),
                "trending_topics": list(self._topics_cache["topics"]),
                "cached": True,
            }
        profiles_cfg = self.config_loader.get_profiles_config()
        client = self._build_okx_client(profiles_cfg)
        if client is None:
            return {
                "ok": False,
                "error": "Chưa có tài khoản OKX hợp lệ (cookies/keys trong config/)",
                "default_hashtags": list(DEFAULT_OKX_HASHTAGS),
                "trending_topics": [],
            }
        cfg = dict((profiles_cfg.get("crawl_sources") or {}).get("okx") or {})
        image_cfg = profiles_cfg.get("image_processing") or {}
        crawler = OkxCrawler(self.storage, cfg, image_cfg, client=client, max_retries=2, retry_delay=1)
        topics = crawler.get_trending_topics()
        self._topics_cache = {"ts": now, "topics": topics}
        return {
            "ok": True,
            "default_hashtags": list(DEFAULT_OKX_HASHTAGS),
            "trending_topics": topics,
            "cached": False,
        }

    def run_crawl(
        self,
        sources: List[str],
        max_posts_per_source: int = 8,
        lookback_hours: float = 24.0,
        max_pages: int = 2,
        ignore_watermark: bool = True,
        download_images: bool = False,
        binance_authors_raw: Any = None,
        binance_post_ids_raw: Any = None,
        binance_keywords_raw: Any = None,
        binance_featured: bool = False,
        okx_hashtags_raw: Any = None,
        okx_timeline_type: str = "1",
        okx_include_trending: bool = False,
        okx_include_feed: bool = False,
        okx_authors_raw: Any = None,
        progress_cb: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        profiles_cfg = self.config_loader.get_profiles_config()
        sources_cfg = profiles_cfg.get("crawl_sources") or {}
        image_cfg = profiles_cfg.get("image_processing") or {}
        proxy_cfg = profiles_cfg.get("crawl_proxies") or {}

        selected = [s.lower().strip() for s in (sources or []) if s]
        if not selected:
            raise ValueError("Chưa chọn nguồn crawl nào!")

        if ignore_watermark:
            self.storage.reset_crawl_state(selected)
            self.logs.info(
                "CRAWL",
                f"Đã reset watermark test cho {selected} (lookback={lookback_hours}h, max={max_posts_per_source}/nguồn, storage={self.storage.mode.upper()})",
            )

        summary: Dict[str, Any] = {
            "storage_mode": self.storage.mode,
            "total_new": 0,
            "by_source": {},
            "errors": [],
            "duration_ms": 0,
        }
        t0 = time.time()

        for idx, src in enumerate(selected):
            if progress_cb:
                progress_cb(idx, len(selected), f"Đang crawl {src}...")
            src_t0 = time.time()
            try:
                if src == "kucoin":
                    cfg = dict(sources_cfg.get("kucoin") or {})
                    crawler = KucoinCrawler(self.storage, cfg, image_cfg, proxy_config=proxy_cfg, max_retries=2, retry_delay=2)
                    if not download_images:
                        crawler.process_images = lambda pid, urls: []
                    n = crawler.crawl_news(
                        max_pages=max_pages,
                        max_new_posts=max_posts_per_source,
                        lookback_hours=lookback_hours,
                    )
                    summary["by_source"]["kucoin"] = {
                        "new": n,
                        "status": "ok",
                        "duration_ms": int((time.time() - src_t0) * 1000),
                    }
                    summary["total_new"] += n
                    self.logs.success("CRAWL", f"[KuCoin] Hoàn tất: +{n} bài mới ({summary['by_source']['kucoin']['duration_ms']}ms)")

                elif src == "sosovalue":
                    cfg = dict(sources_cfg.get("sosovalue") or {})
                    crawler = SosoValueCrawler(self.storage, cfg, image_cfg, proxy_config=proxy_cfg, max_retries=2, retry_delay=2)
                    if not download_images:
                        crawler.process_images = lambda pid, urls: []
                    n = crawler.crawl_news(
                        max_pages=max_pages,
                        max_new_posts=max_posts_per_source,
                        lookback_hours=lookback_hours,
                    )
                    summary["by_source"]["sosovalue"] = {
                        "new": n,
                        "status": "ok",
                        "duration_ms": int((time.time() - src_t0) * 1000),
                    }
                    summary["total_new"] += n
                    self.logs.success("CRAWL", f"[SoSoValue] Hoàn tất: +{n} bài mới ({summary['by_source']['sosovalue']['duration_ms']}ms)")

                elif src == "binance":
                    cfg = dict(sources_cfg.get("binance") or {})
                    crawler = BinanceCrawler(self.storage, cfg, image_cfg, proxy_config=proxy_cfg, max_retries=2, retry_delay=2)
                    if not download_images:
                        crawler.process_images = lambda pid, urls: []

                    # Tự tách Post ID nếu người dùng lỡ dán link /square/post/... vào ô author
                    extra_pids_from_authors = _parse_binance_post_ids(binance_authors_raw)
                    direct_pids = list(dict.fromkeys([*_parse_binance_post_ids(binance_post_ids_raw), *extra_pids_from_authors]))
                    authors_clean_raw = re.sub(r"https?://\S+/post/\d+\S*", "", str(binance_authors_raw or ""))
                    if authors_clean_raw.strip():
                        authors = _parse_binance_authors(authors_clean_raw, [])
                    elif not direct_pids:
                        authors = crawler.load_authors()
                    else:
                        authors = []
                    # Lọc bỏ các token toàn số dài (Post ID) khỏi danh sách author
                    authors = [a for a in authors if a.get("userid") not in set(direct_pids)]
                    n_total = 0

                    if direct_pids:
                        self.logs.info("CRAWL", f"[Binance] Fetch trực tiếp {len(direct_pids)} post ID: {direct_pids}")
                        for pid in direct_pids:
                            detail = crawler._crawl_post_detail(pid)
                            if not detail:
                                self.logs.warning("CRAWL", f"[Binance] Không lấy được chi tiết bài {pid} (hoặc text quá ngắn)")
                                continue
                            imgs = crawler.process_images(pid, detail.get("image_urls", [])) if download_images else []
                            if imgs is None:
                                self.logs.warning("CRAWL", f"[Binance] Bỏ qua bài {pid} do phát hiện QR")
                                continue
                            ok = crawler.save_post(
                                post_id=detail["post_id"],
                                url=f"https://www.binance.com/vi/square/post/{detail['post_id']}",
                                text=detail.get("text", ""),
                                image_paths=imgs,
                                create_time=detail.get("publish_time"),
                                crawled_from="binance_direct_id",
                                hashtags=detail.get("hashtags", ""),
                            )
                            if ok:
                                n_total += 1
                            else:
                                self.logs.info("CRAWL", f"[Binance] Bài {pid} đã tồn tại trong storage (bỏ qua trùng)")

                    if authors and (max_posts_per_source - n_total) > 0:
                        self.logs.info("CRAWL", f"[Binance] Crawl {len(authors)} tác giả (lookback={lookback_hours}h)...")
                        n_users = crawler.crawl_all_users(
                            authors,
                            max_workers=min(10, len(authors)),
                            max_new_posts=max(0, max_posts_per_source - n_total),
                            lookback_hours=lookback_hours,
                            ignore_state=ignore_watermark,
                        )
                        n_total += n_users

                    summary["by_source"]["binance"] = {
                        "new": n_total,
                        "status": "ok",
                        "duration_ms": int((time.time() - src_t0) * 1000),
                    }
                    summary["total_new"] += n_total
                    self.logs.success("CRAWL", f"[Binance] Hoàn tất: +{n_total} bài mới ({summary['by_source']['binance']['duration_ms']}ms)")

                elif src == "okx":
                    cfg = dict(sources_cfg.get("okx") or {})
                    authors = _parse_binance_authors(okx_authors_raw, cfg.get("authors") or [])
                    hashtags = _parse_tokens(okx_hashtags_raw)
                    client = self._build_okx_client(profiles_cfg)
                    if client is None:
                        msg = "OKX Orbit cần account đã đăng nhập (cookies + keys hợp lệ trong config/) — bỏ qua."
                        self.logs.warning("CRAWL", f"[OKX] {msg}")
                        summary["by_source"]["okx"] = {
                            "new": 0,
                            "status": "skipped",
                            "warning": msg,
                            "duration_ms": int((time.time() - src_t0) * 1000),
                        }
                    else:
                        crawler = OkxCrawler(self.storage, cfg, image_cfg, proxy_config=proxy_cfg, client=client, max_retries=2, retry_delay=2)
                        if not download_images:
                            crawler.process_images = lambda pid, urls: []

                        # Nếu người dùng không chọn gì, mặc định crawl sẵn 3 hashtag phổ biến BTC, ETH, SOL
                        if not hashtags and not authors and not okx_include_trending and not okx_include_feed:
                            hashtags = ["BTC", "ETH", "SOL"]

                        n_total = 0
                        if hashtags or okx_include_trending or okx_include_feed:
                            tag_cnt = max(1, len(hashtags))
                            okx_pages = max(max_pages, min(10, math.ceil(max_posts_per_source / (tag_cnt * 8)) + 1))
                            self.logs.info(
                                "CRAWL",
                                f"[OKX] Crawl Orbit Hashtags={hashtags} (type={okx_timeline_type}, pages={okx_pages}, trending={okx_include_trending}, feed={okx_include_feed})...",
                            )
                            n_tags = crawler.crawl_hashtags(
                                hashtags,
                                timeline_type=str(okx_timeline_type or "1"),
                                max_pages=okx_pages,
                                max_new_posts=max_posts_per_source,
                                lookback_hours=lookback_hours,
                                include_trending=okx_include_trending,
                                include_feed=okx_include_feed,
                            )
                            n_total += n_tags

                        if authors and (max_posts_per_source - n_total) > 0:
                            self.logs.info("CRAWL", f"[OKX] Crawl {len(authors)} tác giả Orbit...")
                            n_au = crawler.crawl_all_users(
                                authors,
                                max_workers=2,
                                max_new_posts=max(0, max_posts_per_source - n_total),
                                lookback_hours=lookback_hours,
                            )
                            n_total += n_au

                        summary["by_source"]["okx"] = {
                            "new": n_total,
                            "status": "ok",
                            "duration_ms": int((time.time() - src_t0) * 1000),
                        }
                        summary["total_new"] += n_total
                        self.logs.success("CRAWL", f"[OKX] Hoàn tất: +{n_total} bài mới ({summary['by_source']['okx']['duration_ms']}ms)")
                else:
                    msg = f"Nguồn crawl không hỗ trợ: {src}"
                    self.logs.warning("CRAWL", msg)
                    summary["errors"].append({"source": src, "error": msg})

            except Exception as e:
                tb = traceback.format_exc()
                self.logs.error("CRAWL", f"[{src.upper()}] Crawl lỗi: {e}", details=tb)
                summary["by_source"][src] = {
                    "new": 0,
                    "status": "error",
                    "error": str(e),
                    "duration_ms": int((time.time() - src_t0) * 1000),
                }
                summary["errors"].append({"source": src, "error": str(e)})

        if progress_cb:
            progress_cb(len(selected), len(selected), "Hoàn tất crawl")

        summary["duration_ms"] = int((time.time() - t0) * 1000)
        return summary

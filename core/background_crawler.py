"""BackgroundCrawler — điều phối pipeline Crawl -> Gemini -> Cleanup (học từ BinanceSquare).

Kiến trúc nâng cấp:
  1. Adaptive Fresh-First Crawling:
     - Kiểm tra ready buffer trước mỗi chu kỳ (`get_ready_to_post_count()` vs `max_ready_buffer`).
     - Nếu buffer đã đầy -> bỏ qua crawl để tránh tích tồn bài cũ.
     - Nếu buffer còn thiếu -> dọn sạch bài thô (`crawled`) tồn cũ (vẫn giữ nguyên `seen_post_ids`
       để chống trùng ID tuyệt đối), rồi crawl bài MỚI NHẤT từ các nguồn enabled.
  2. Hỗ trợ đủ 4 nguồn: Binance (authors hoặc Public News Feed), OKX (hashtags/hot/feed/authors),
     KuCoin, SoSoValue.
  3. Xử lý song song bằng shared `GeminiProcessorPool` (luôn xử lý bài mới nhất trước + retry lỗi).
  4. Tự động cleanup theo lịch độc lập (`cleanup_interval_seconds`, mặc định 1h):
     - Dọn bài quá hạn `crawled_keep_seconds` + ảnh mồ côi + WAL checkpoint + incremental vacuum.
     - Khôi phục bài treo `processing` (`cleanup_processing_timeout`).
     - Double `gc.collect()` giải phóng RAM sau mỗi chu kỳ.
  5. Heartbeat & `is_healthy()` để Orchestrator giám sát và tự khởi động lại nếu cần.
"""
import gc
import math
import os
import threading
import time
from datetime import datetime
from typing import Dict, Optional

from infra.config_helper import calculate_dynamic_buffer
from infra.database import DatabaseManager
from infra.logging_config import get_logger
from services.crawler.binance_crawler import BinanceCrawler
from services.crawler.kucoin_crawler import KucoinCrawler
from services.crawler.okx_crawler import DEFAULT_OKX_HASHTAGS, OkxCrawler
from services.crawler.soso_crawler import SosoValueCrawler
from services.processor.gemini_pool import GeminiProcessorPool

logger = get_logger(__name__)


class BackgroundCrawler:
    def __init__(
        self,
        config: dict,
        db: DatabaseManager,
        alerts=None,
        processor_pool: Optional[GeminiProcessorPool] = None,
    ):
        self.config = config or {}
        self.db = db
        self.alerts = alerts
        self.workers_cfg = self.config.get("crawl_workers", {}) or {}
        self.sources_cfg = self.config.get("crawl_sources", {}) or {}
        self.image_cfg = self.config.get("image_processing", {}) or {}
        self.proxy_cfg = self.config.get("crawl_proxies", {}) or {}

        self.reload_interval = int(self.workers_cfg.get("reload_interval", 600))
        self.keep_seconds = max(3600, int(self.workers_cfg.get("crawled_keep_seconds", 86400)))
        self.cleanup_interval_seconds = max(600, int(self.workers_cfg.get("cleanup_interval_seconds", 3600)))
        self.stale_crawled_seconds = max(0, int(self.workers_cfg.get("stale_crawled_seconds", 1200)))
        self.adaptive_mode = bool(self.workers_cfg.get("adaptive_mode", True))
        self.max_workers = int(self.workers_cfg.get("max_workers", 10))

        gem_cfg = self.config.get("gemini", {}) or {}
        cfg_file = gem_cfg.get("config_file", "config/gemini_config.yaml")
        self.processor_pool = processor_pool or GeminiProcessorPool(
            config=self.config,
            db=self.db,
            gemini_config_path=cfg_file,
        )

        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_heartbeat: Optional[datetime] = None
        self._last_cleanup_monotonic: Optional[float] = None
        self._okx_cookie_file: Optional[str] = None
        self._okx_cookie_mtime: float = 0.0
        self.stats = {
            "cycles": 0,
            "total_crawled": 0,
            "total_processed": 0,
            "total_cleaned": 0,
            "errors": 0,
            "last_crawl_time": None,
        }
        self._crawlers = self._build_crawlers()

    # ------------------------------------------------------------------
    # CRAWLER FACTORY
    # ------------------------------------------------------------------
    def _build_crawlers(self) -> Dict[str, object]:
        crawlers: Dict[str, object] = {}
        b = self.sources_cfg.get("binance", {}) or {}
        if b.get("enabled"):
            crawlers["binance"] = BinanceCrawler(self.db, b, self.image_cfg, proxy_config=self.proxy_cfg)
        k = self.sources_cfg.get("kucoin", {}) or {}
        if k.get("enabled"):
            crawlers["kucoin"] = KucoinCrawler(self.db, k, self.image_cfg, proxy_config=self.proxy_cfg)
        s = self.sources_cfg.get("sosovalue", {}) or {}
        if s.get("enabled"):
            crawlers["sosovalue"] = SosoValueCrawler(self.db, s, self.image_cfg, proxy_config=self.proxy_cfg)
        o = self.sources_cfg.get("okx", {}) or {}
        if o.get("enabled"):
            client = self._build_okx_client()
            if client is not None:
                crawlers["okx"] = OkxCrawler(self.db, o, self.image_cfg, proxy_config=self.proxy_cfg, client=client)
            else:
                logger.warning("[CRAWL] nguồn okx enabled nhưng không có account đăng nhập — bỏ qua")
        return crawlers

    def _build_okx_client(self):
        accounts = self.config.get("posting_accounts", []) or []
        for idx, acc in enumerate(accounts, start=1):
            if not acc.get("enabled", True):
                continue
            try:
                from api.session_manager import build_client
                client = build_client(acc, account_id=idx)
                cf = str(client.cfg.cookies_file)
                self._okx_cookie_file = cf
                self._okx_cookie_mtime = os.path.getmtime(cf) if os.path.exists(cf) else 0.0
                return client
            except Exception as e:
                logger.warning("[CRAWL] không dựng được OKX client từ account %s: %s", idx, e)
        return None

    def _refresh_okx_client_if_updated(self, crawler) -> None:
        if not self._okx_cookie_file or not os.path.exists(self._okx_cookie_file):
            return
        try:
            mtime = os.path.getmtime(self._okx_cookie_file)
        except OSError:
            return
        if mtime <= self._okx_cookie_mtime:
            return
        new_client = self._build_okx_client()
        if new_client is not None:
            old_client = getattr(crawler, "client", None)
            crawler.client = new_client
            if old_client is not None:
                try:
                    old_client.close()
                except Exception:
                    pass
            logger.info("[CRAWL] Đã nạp lại OkxClient sau khi file cookie cập nhật.")

    # ------------------------------------------------------------------
    # ADAPTIVE BUFFER & CLEANUP SCHEDULE
    # ------------------------------------------------------------------
    def _buffer_limits(self) -> Dict[str, int]:
        dyn = calculate_dynamic_buffer(self.config)
        max_ready = int(dyn.get("max_ready_buffer", 0))
        max_total = int(dyn.get("max_total_buffer", 0))
        if max_ready <= 0:
            fallback = sum(int((self.sources_cfg.get(s, {}) or {}).get("budget", 15))
                           for s in self._crawlers) or 20
            max_ready = fallback
            max_total = fallback + 6
        return {"max_ready_buffer": max_ready, "max_total_buffer": max_total}

    def run_cleanup_if_due(self, force: bool = False) -> int:
        """Chạy dọn dẹp DB + ảnh mồ côi + khôi phục bài treo theo lịch độc lập (mặc định mỗi 1 giờ)."""
        now_m = time.monotonic()
        if (not force and self._last_cleanup_monotonic is not None and
                now_m - self._last_cleanup_monotonic < self.cleanup_interval_seconds):
            return 0
        cleaned = 0
        try:
            self.db.cleanup_processing_timeout(timeout_seconds=300)
            cleaned = self.db.cleanup_expired_posts(self.keep_seconds)
            self.stats["total_cleaned"] += cleaned
            self._last_cleanup_monotonic = now_m
        except Exception as e:
            logger.warning("[CRAWL] cleanup lỗi: %s", e)
        return cleaned

    # ------------------------------------------------------------------
    # ONE CYCLE: Adaptive Fresh-First Crawl -> Parallel Gemini -> Scheduled Cleanup
    # ------------------------------------------------------------------
    def run_once(self, force_cleanup: bool = False) -> dict:
        self._last_heartbeat = datetime.now()
        summary = {"crawled": 0, "processed": 0, "stale_dropped": 0, "cleaned": 0, "by_source": {}}

        # 0. Khôi phục bài treo 'processing' quá 300s trước khi đếm buffer
        try:
            self.db.cleanup_processing_timeout(timeout_seconds=300)
        except Exception as e:
            logger.warning("[CRAWL] cleanup_processing_timeout lỗi: %s", e)

        limits = self._buffer_limits()
        ready_cnt = self.db.get_ready_to_post_count()
        max_ready = limits["max_ready_buffer"]
        max_total = limits["max_total_buffer"]

        should_crawl = True
        if self.adaptive_mode and ready_cnt >= max_ready:
            should_crawl = False
            logger.info(
                "[CRAWL] Ready buffer đã đầy (%d/%d) — bỏ qua crawl lượt này",
                ready_cnt, max_ready,
            )

        if should_crawl:
            # Chỉ dọn bài thô 'crawled' đã quá tuổi stale_crawled_seconds (mặc định 20 phút)
            # tránh xoá oan bài vừa crawl trước khi Gemini kịp xử lý
            if self.adaptive_mode:
                stale_dropped = self.db.delete_stale_crawled_posts(
                    keep_newest=0, min_age_seconds=self.stale_crawled_seconds,
                )
                summary["stale_dropped"] = stale_dropped

            num_sources = max(1, len(self._crawlers))
            default_cap = max(4, math.ceil(max(max_total, 8) / num_sources))
            logger.info(
                "[CRAWL] chu kỳ mới: ready=%d/%d, max_total=%d, nguồn=%s",
                ready_cnt, max_ready, max_total, list(self._crawlers.keys()),
            )

            for name, crawler in self._crawlers.items():
                src_cfg = self.sources_cfg.get(name, {}) or {}
                per_source_cap = int(src_cfg.get("max_posts_per_cycle") or default_cap)
                if per_source_cap <= 0:
                    continue
                try:
                    if name == "binance":
                        workers = int(src_cfg.get("max_workers") or self.max_workers)
                        n = crawler.crawl_all_users(
                            max_workers=workers,
                            max_new_posts=per_source_cap,
                        )
                    elif name == "okx":
                        self._refresh_okx_client_if_updated(crawler)
                        authors = src_cfg.get("authors", []) or []
                        hashtags = src_cfg.get("hashtags", []) or list(DEFAULT_OKX_HASHTAGS[:3])
                        inc_trend = bool(src_cfg.get("include_trending", False))
                        inc_feed = bool(src_cfg.get("include_feed", False))
                        t_type = str(src_cfg.get("timeline_type", "1"))
                        n = 0
                        if hashtags or inc_trend or inc_feed:
                            n += crawler.crawl_hashtags(
                                hashtags,
                                timeline_type=t_type,
                                max_pages=2,
                                max_new_posts=per_source_cap,
                                include_trending=inc_trend,
                                include_feed=inc_feed,
                            )
                        if authors and (per_source_cap - n) > 0:
                            n += crawler.crawl_all_users(
                                authors, max_workers=2, max_new_posts=max(0, per_source_cap - n),
                            )
                    else:
                        n = crawler.crawl_news(max_pages=2, max_new_posts=per_source_cap)

                    summary["by_source"][name] = n
                    summary["crawled"] += n
                except Exception as e:
                    self.stats["errors"] += 1
                    logger.error("[CRAWL] nguồn %s lỗi: %s", name, e)
                    if self.alerts:
                        self.alerts.alert(
                            None, f"Crawl {name} lỗi: {e}", "",
                            level="ERROR", event="crawl_error", component="crawler",
                        )
            self.stats["total_crawled"] += summary["crawled"]
            self.stats["last_crawl_time"] = datetime.now()
            self._last_heartbeat = datetime.now()

        # 2. Cleanup định kỳ (mỗi cleanup_interval_seconds, hoặc force_cleanup=True)
        summary["cleaned"] = self.run_cleanup_if_due(force=force_cleanup)

        # 3. Gemini xử lý song song bài MỚI NHẤT trước (bao gồm cả bài retry đã qua cooldown)
        proc_stats = self.processor_pool.process_batch(parallel=True)
        summary["processed"] = proc_stats.get("success", 0)
        summary["gemini_stats"] = proc_stats
        self.stats["total_processed"] += summary["processed"]
        self.stats["cycles"] += 1
        self._last_heartbeat = datetime.now()

        # Double GC giải phóng RAM sau mỗi chu kỳ (pattern từ BinanceSquare)
        gc.collect()
        gc.collect()

        logger.info("[CRAWL] xong chu kỳ #%d: %s", self.stats["cycles"], summary)
        return summary

    # ------------------------------------------------------------------
    # BACKGROUND LOOP & HEALTH MONITORING
    # ------------------------------------------------------------------
    def _sleep_with_heartbeat(self, seconds: int) -> bool:
        """Ngủ theo giây và cập nhật heartbeat liên tục; trả False nếu nhận lệnh stop."""
        for _ in range(max(1, int(seconds))):
            if self._stop_event.is_set():
                return False
            self._last_heartbeat = datetime.now()
            time.sleep(1)
        return not self._stop_event.is_set()

    def _loop(self, delay_first_cycle: bool = False):
        logger.info(
            "[CRAWL] background started (interval=%ss, cleanup_every=%ss, retention=%sh, adaptive=%s)",
            self.reload_interval, self.cleanup_interval_seconds,
            self.keep_seconds // 3600, self.adaptive_mode,
        )
        if delay_first_cycle:
            logger.info("[CRAWL] Đợi %ss trước chu kỳ nền tiếp theo (tránh trùng initial crawl)...",
                        self.reload_interval)
            if not self._sleep_with_heartbeat(self.reload_interval):
                logger.info("[CRAWL] background stopped before first delayed cycle")
                return

        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.stats["errors"] += 1
                self._last_heartbeat = datetime.now()
                logger.error("[CRAWL] run_once lỗi: %s", e)
            if not self._sleep_with_heartbeat(self.reload_interval):
                break
        logger.info("[CRAWL] background stopped")

    def start(self, delay_first_cycle: bool = False):
        if self._running and self._thread and self._thread.is_alive():
            return
        self._running = True
        self._stop_event.clear()
        self._last_heartbeat = datetime.now()
        self._thread = threading.Thread(
            target=self._loop,
            args=(delay_first_cycle,),
            daemon=True,
            name="BackgroundCrawler",
        )
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def is_healthy(self) -> bool:
        if not self.is_running():
            return False
        if self._last_heartbeat is None:
            return False
        elapsed = (datetime.now() - self._last_heartbeat).total_seconds()
        return elapsed < (self.reload_interval + 600)

    def get_stats(self) -> dict:
        out = dict(self.stats)
        out["is_running"] = self.is_running()
        out["is_healthy"] = self.is_healthy()
        out["last_heartbeat"] = self._last_heartbeat
        return out

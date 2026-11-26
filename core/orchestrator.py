"""Orchestrator — ráp nối toàn bộ hệ thống OKXSquare (học kiến trúc BinanceSquare).

Thành phần:
  * DatabaseManager (WAL, seen_post_ids chống trùng thuần ID, orphan image sweep)
  * TelegramAlerts (task_logs + Telegram)
  * GeminiProcessorPool (shared single API key pool, song song, ưu tiên bài mới nhất)
  * BackgroundCrawler (Adaptive Fresh-First Crawl -> Gemini -> Hourly Cleanup + Heartbeat)
  * MultiAccountPoster (Sign & Publish đa account, atomic claim bài mới nhất trước)
  * CookieExpiryChecker (JWT exp, cảnh báo sắp hết hạn)
  * HealthChecker (ping định kỳ)

Vòng đời chuẩn sản xuất:
  1. Startup cleanup (dọn bài quá hạn + ảnh mồ côi + khôi phục bài treo processing).
  2. Initial fresh-first crawl & Gemini processing nếu ready buffer đang thiếu.
  3. Khởi chạy BackgroundCrawler (delay 1 chu kỳ để không trùng initial) + Poster workers.
  4. Monitor loop liên tục:
     - Preemptive Gemini processing khi ready buffer tụt xuống dưới 25% mà còn bài thô.
     - Health-check & tự động restart BackgroundCrawler nếu thread chết/treo.
     - Định kỳ `gc.collect()` giữ RAM ổn định 24/7.
  5. Graceful shutdown + Final DB cleanup + `gc.collect()` trước khi thoát.
"""
import gc
import time
from typing import Optional

from core.background_crawler import BackgroundCrawler
from infra.config_helper import calculate_dynamic_buffer, load_profiles_config
from infra.cookie_checker import CookieExpiryChecker
from infra.database import DatabaseManager
from infra.healthcheck import start_health_check
from infra.logging_config import get_logger, setup_logging
from services.poster.multi_account_poster import MultiAccountPoster
from services.processor.gemini_pool import GeminiProcessorPool
from services.telegram.alerts import get_alerts

logger = get_logger(__name__)


class Orchestrator:
    def __init__(self, config_path: str = "config/profiles_config.yaml",
                 db: Optional[DatabaseManager] = None):
        setup_logging()
        self.config_path = config_path
        self.config = load_profiles_config(config_path)
        self.db = db or DatabaseManager()
        self.alerts = get_alerts(self.db)

        workers_cfg = self.config.get("crawl_workers", {}) or {}
        self.keep_seconds = max(3600, int(workers_cfg.get("crawled_keep_seconds", 86400)))
        gem_cfg = self.config.get("gemini", {}) or {}
        gem_cfg_file = gem_cfg.get("config_file", "config/gemini_config.yaml")

        # Shared GeminiProcessorPool: Orchestrator (preemptive) & BackgroundCrawler dùng chung 1 key pool
        self.processor_pool = GeminiProcessorPool(
            config=self.config,
            db=self.db,
            gemini_config_path=gem_cfg_file,
        )
        self.crawler = BackgroundCrawler(
            self.config, self.db, self.alerts, processor_pool=self.processor_pool,
        )
        self.poster: Optional[MultiAccountPoster] = None
        try:
            self.poster = MultiAccountPoster(config_path, db=self.db, alerts=self.alerts)
        except RuntimeError as e:
            logger.warning("Poster không khởi tạo (crawl-only): %s", e)

        self.cookie_checker = CookieExpiryChecker(config_path, db=self.db, alerts=self.alerts)
        self.health = start_health_check()
        self._stopped = False

    # ------------------------------------------------------------------
    # STARTUP CLEANUP & PREEMPTIVE PROCESSING
    # ------------------------------------------------------------------
    def _startup_cleanup(self):
        logger.info("→ Running startup database cleanup...")
        recovered = self.db.cleanup_processing_timeout(timeout_seconds=0)
        deleted = self.db.cleanup_expired_posts(self.keep_seconds)
        stats = self.db.get_stats()
        logger.info(
            "✓ Startup cleanup: deleted=%d expired, recovered=%d stuck | DB total=%d, ready=%d, seen_ids=%d",
            deleted, recovered, stats["total_posts"],
            self.db.get_ready_to_post_count(), stats.get("seen_post_ids", 0),
        )

    def _check_preemptive_processing(self):
        """Nếu ready buffer tụt < 25% mà DB còn bài thô -> chạy Gemini xử lý ngay bài mới nhất."""
        try:
            dyn = calculate_dynamic_buffer(self.config)
            max_ready = int(dyn.get("max_ready_buffer", 0))
            if max_ready <= 0:
                return
            ready_cnt = self.db.get_ready_to_post_count()
            low_water = max(1, round(max_ready * 0.25))
            if ready_cnt < low_water:
                unprocessed = self.db.get_unprocessed_count()
                if unprocessed > 0:
                    logger.info(
                        "[PREEMPTIVE] Ready buffer thấp (%d/%d < %d) & có %d bài chờ -> chạy Gemini ngay!",
                        ready_cnt, max_ready, low_water, unprocessed,
                    )
                    self.processor_pool.process_batch(parallel=True)
        except Exception as e:
            logger.warning("[PREEMPTIVE] lỗi kiểm tra buffer: %s", e)

    def _check_crawler_health(self):
        """Tự động restart BackgroundCrawler nếu phát hiện thread chết hoặc mất heartbeat."""
        if not self.crawler.is_healthy():
            logger.error("[MONITOR] BackgroundCrawler không healthy — đang tự động khởi động lại...")
            try:
                self.crawler.stop()
            except Exception:
                pass
            self.crawler = BackgroundCrawler(
                self.config, self.db, self.alerts, processor_pool=self.processor_pool,
            )
            self.crawler.start(delay_first_cycle=False)
            self.alerts.alert(
                None, "BackgroundCrawler mất heartbeat — đã tự động khởi động lại.",
                level="WARNING", event="crawler_restart", component="orchestrator",
            )

    # ------------------------------------------------------------------
    def start(self, run_initial_crawl: bool = True):
        self._stopped = False
        logger.info("=" * 60)
        logger.info("OKXSquare ORCHESTRATOR START")
        logger.info("=" * 60)

        # 1. Startup cleanup (dọn bài cũ + ảnh mồ côi + khôi phục bài treo)
        self._startup_cleanup()

        # 2. Initial fresh-first crawl + Gemini (trước khi bật background loop)
        if run_initial_crawl:
            logger.info("→ Running initial fresh-first crawl & Gemini cycle...")
            self.crawler.run_once(force_cleanup=False)

        # 3. Start background services (delay 1 chu kỳ nếu vừa chạy initial để tránh trùng)
        self.cookie_checker.cache_and_schedule()
        self.cookie_checker.start_background_checker()
        self.crawler.start(delay_first_cycle=run_initial_crawl)
        if self.poster:
            self.poster.start_workers()
        self.alerts.notify(
            "🚀 <b>OKXSquare</b> started "
            f"(dry_run={self.poster.dry_run if self.poster else 'n/a'})."
        )

    def run_forever(self):
        self.start(run_initial_crawl=True)
        tick = 0
        try:
            while True:
                time.sleep(10)
                tick += 1
                # Mỗi 30s: kiểm tra buffer thấp để xử lý preemptive ngay bài mới nhất
                if tick % 3 == 0:
                    self._check_preemptive_processing()
                # Mỗi 5 phút: kiểm tra sức khỏe BackgroundCrawler + dọn RAM
                if tick % 30 == 0:
                    self._check_crawler_health()
                    gc.collect()
                    gc.collect()
        except KeyboardInterrupt:
            logger.info("Received stop signal")
        finally:
            self.stop()

    def run_once(self, per_account: int = 1) -> dict:
        """Một chu kỳ startup cleanup + fresh crawl/Gemini + một batch đăng."""
        self._startup_cleanup()
        crawl_summary = self.crawler.run_once(force_cleanup=False)
        post_summary = self.poster.post_batch(per_account=per_account) if self.poster else {}
        return {"crawl": crawl_summary, "post": post_summary}

    def run_single_step(self, step: str, per_account: int = 1):
        """Chạy một bước rời cho CLI: crawl | post | stats | cookie | status."""
        if step == "crawl":
            return self.crawler.run_once(force_cleanup=True)
        if step == "post":
            if not self.poster:
                logger.warning("Không có poster (crawl-only) — bỏ qua post")
                return {}
            return self.poster.post_batch(per_account=per_account)
        if step == "stats":
            self.db.print_stats()
            return None
        if step == "cookie":
            self.cookie_checker.cache_and_schedule()
            self.cookie_checker.print_status()
            return None
        if step == "status":
            if self.poster:
                self.poster.print_status()
            self.db.print_stats()
            return None
        raise ValueError(f"step không hợp lệ: {step}")

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        logger.info("Stopping orchestrator (graceful shutdown)...")
        if self.poster:
            self.poster.stop()
        self.crawler.stop()
        self.cookie_checker.stop_background_checker()
        self.health.stop()
        try:
            logger.info("→ Running final database cleanup before exit...")
            self.db.cleanup_expired_posts(self.keep_seconds)
        except Exception as e:
            logger.warning("Final cleanup warning: %s", e)
        gc.collect()
        self.db.close()
        logger.info("Orchestrator stopped cleanly")

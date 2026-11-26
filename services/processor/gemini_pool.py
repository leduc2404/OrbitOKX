"""GeminiProcessorPool cho OKXSquare — kế thừa kiến trúc từ BinanceSquare.

Đặc điểm cốt lõi:
  * Luôn ưu tiên xử lý bài MỚI NHẤT trước (ORDER BY create_time DESC).
  * Adaptive Buffer Filtering: chỉ xử lý đúng lượng bài mới nhất cần thiết để lấp đầy
    `max_ready_buffer`, tránh lãng phí token AI vào bài tồn cũ.
  * Xử lý song song đa luồng (ThreadPoolExecutor) với mutex `_batch_lock` chống đụng độ
    giữa main orchestrator (preemptive) và background crawler.
  * Tự động retry bài lỗi sau cooldown và `mark_post_skipped` vĩnh viễn khi vượt `max_post_retries`.
  * Giải phóng bộ nhớ `gc.collect()` sau mỗi batch.
"""
import gc
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from typing import Dict, List, Optional, Tuple

from infra.config_helper import calculate_dynamic_buffer
from infra.database import DatabaseManager
from infra.logging_config import get_logger
from services.processor.content_tags import (
    build_text_fingerprint,
    extract_coin_tags,
    validate_rewritten_output,
)
from services.processor.gemini_processor import GeminiProcessor

logger = get_logger(__name__)


class GeminiProcessorPool:
    """Thread-safe parallel Gemini rewrite pool cho OKXSquare."""

    def __init__(
        self,
        config: dict,
        db: DatabaseManager,
        gemini_config_path: str = "config/gemini_config.yaml",
        max_workers: Optional[int] = None,
    ):
        self._batch_lock = threading.Lock()
        self.config = config or {}
        self.db = db
        self.gemini_config_path = gemini_config_path
        self.processor: Optional[GeminiProcessor] = None

        gem_cfg = self.config.get("gemini", {}) or {}
        if gem_cfg.get("enabled", True):
            try:
                self.processor = GeminiProcessor(gemini_config_path)
            except Exception as e:
                logger.warning("[GEMINI_POOL] không khởi tạo được GeminiProcessor: %s", e)
                self.processor = None

        # Tính số worker tối ưu theo provider config
        if max_workers is not None:
            self.max_workers = max(1, int(max_workers))
        elif self.processor is not None:
            p_cfg = self.processor.provider_config or {}
            proc_cfg = p_cfg.get("processing", {}) or {}
            cfg_workers = proc_cfg.get("max_workers")
            if cfg_workers is not None:
                self.max_workers = max(1, int(cfg_workers))
            elif self.processor.key_manager:
                self.max_workers = max(1, int(self.processor.key_manager.num_keys))
            elif self.processor.active_provider == "gemini_proxy":
                self.max_workers = 5
            else:
                valid_keys = [k for k in (p_cfg.get("api_keys") or []) if k and "YOUR_" not in k]
                self.max_workers = max(1, len(valid_keys))
        else:
            self.max_workers = 1

        global_proc = (self.processor.config.get("processing", {}) if self.processor else {}) or {}
        self.max_post_retries = int(global_proc.get("max_post_retries", 3))
        self.failed_post_cooldown_minutes = int(global_proc.get("failed_post_cooldown_minutes", 5))
        self.retry_failed_in_next_cycle = bool(global_proc.get("retry_failed_in_next_cycle", True))

        logger.info(
            "[GEMINI_POOL] sẵn sàng: enabled=%s workers=%d max_retries=%d cooldown=%dm",
            self.ready, self.max_workers, self.max_post_retries, self.failed_post_cooldown_minutes,
        )

    @property
    def ready(self) -> bool:
        return self.processor is not None and bool(getattr(self.processor, "ready", True))

    @staticmethod
    def _validate_output(output: str) -> bool:
        ok, _ = validate_rewritten_output(output)
        return ok

    def _run_post_task(self, post: Dict) -> Tuple[str, str, Optional[str]]:
        """Worker wrapper đảm bảo đóng SQLite connection trên từng thread."""
        try:
            return self._process_single_post(post)
        finally:
            try:
                self.db.close()
            except Exception:
                pass

    def _process_single_post(self, post: Dict) -> Tuple[str, str, Optional[str]]:
        """Xử lý 1 bài viết. Trả về (post_id, outcome='ok'|'rejected'|'failed'|'skipped', model)."""
        pid = post["id"]
        text = (post.get("text") or "").strip()
        raw_hashtags = [t.strip() for t in (post.get("hashtags") or "").split(",") if t.strip()]
        current_retry = int(post.get("retry_count") or 0)

        if not text:
            self.db.mark_post_skipped(pid, "empty_text")
            return (pid, "skipped", None)

        if current_retry >= self.max_post_retries:
            self.db.mark_post_skipped(pid, f"exceeded_max_retries ({self.max_post_retries})")
            return (pid, "skipped", None)

        if not self.ready:
            # Gemini bị tắt chủ động -> giữ nguyên text gốc để đăng
            coin_tags = extract_coin_tags(text, fallback_tags=raw_hashtags, max_tags=3)
            fp = build_text_fingerprint(text, coin_tags=coin_tags)
            self.db.mark_post_processed(
                pid,
                primary_coin=(coin_tags[0] if coin_tags else ""),
                hashtags=",".join(coin_tags),
                fingerprint_json=json.dumps(fp, ensure_ascii=False),
            )
            return (pid, "ok", "passthrough")

        max_attempts = 2
        last_error = "no_result"
        for attempt in range(max_attempts):
            try:
                meta = self.processor.process_text_with_meta(text, pid)
                if not meta:
                    last_error = "empty_result"
                else:
                    output = meta.get("text") or ""
                    model = meta.get("model")
                    prompt_id = meta.get("prompt_id") or ""
                    if output == "[REJECT]":
                        self.db.mark_post_skipped(pid, "gemini_reject")
                        return (pid, "rejected", model)
                    ok, reason = validate_rewritten_output(output)
                    if ok:
                        coin_tags = extract_coin_tags(output, fallback_tags=raw_hashtags, max_tags=3)
                        primary_coin = coin_tags[0] if coin_tags else ""
                        fp = build_text_fingerprint(output, coin_tags=coin_tags)
                        self.db.mark_post_processed(
                            pid,
                            gemini_output=output,
                            prompt_id=prompt_id,
                            primary_coin=primary_coin,
                            hashtags=",".join(coin_tags),
                            fingerprint_json=json.dumps(fp, ensure_ascii=False),
                        )
                        return (pid, "ok", model)
                    if reason == "refusal_leakage":
                        self.db.mark_post_skipped(pid, "gemini_refusal_leakage")
                        return (pid, "rejected", model)
                    last_error = f"invalid_output:{reason} (len={len(output.strip())})"
            except Exception as e:
                last_error = str(e)[:240]

            if attempt < max_attempts - 1:
                time.sleep(2 * (attempt + 1))

        self.db.mark_post_failed(pid, last_error, increment_retry=True)
        return (pid, "failed", None)

    def process_batch(self, batch_size: Optional[int] = None, parallel: bool = True) -> Dict:
        """Xử lý batch bài MỚI NHẤT trước (new + retry) có Adaptive Buffer Filtering + bù reject."""
        with self._batch_lock:
            workers_cfg = self.config.get("crawl_workers", {}) or {}
            adaptive_mode = bool(workers_cfg.get("adaptive_mode", True))
            max_rounds = 3 if adaptive_mode else 1

            agg = {"total": 0, "success": 0, "rejected": 0, "failed": 0, "skipped": 0}
            seen_ids = set()
            t0 = time.time()

            for round_idx in range(1, max_rounds + 1):
                new_posts = [
                    p for p in self.db.get_unprocessed_posts(limit=batch_size)
                    if p["id"] not in seen_ids
                ]
                failed_posts: List[Dict] = []
                if self.retry_failed_in_next_cycle:
                    failed_posts = [
                        p for p in self.db.get_failed_posts_for_retry(
                            cooldown_minutes=self.failed_post_cooldown_minutes,
                            max_retries=self.max_post_retries,
                            limit=batch_size,
                        )
                        if p["id"] not in seen_ids
                    ]

                if adaptive_mode:
                    dyn = calculate_dynamic_buffer(self.config)
                    max_ready = int(dyn.get("max_ready_buffer", 0))
                    if max_ready <= 0:
                        max_ready = max(10, int(batch_size or 20))
                    ready_cnt = self.db.get_ready_to_post_count()
                    slots = max(0, max_ready - ready_cnt)
                    if slots <= 0:
                        if round_idx == 1:
                            logger.info(
                                "[GEMINI_POOL] Ready buffer đã đầy (%d/%d) — bỏ qua xử lý lượt này",
                                ready_cnt, max_ready,
                            )
                        break
                    # Ưu tiên tuyệt đối bài MỚI NHẤT trong new_posts, còn dư slot mới lấy failed_posts
                    selected_new = new_posts[:slots]
                    rem_slots = max(0, slots - len(selected_new))
                    selected_failed = failed_posts[:rem_slots]
                    posts = selected_new + selected_failed
                    if not posts:
                        break
                    logger.info(
                        "[GEMINI_POOL] Ready buffer %d/%d (cần %d, vòng %d/%d) -> chọn %d/%d mới + %d/%d retry (mới nhất trước)",
                        ready_cnt, max_ready, slots, round_idx, max_rounds,
                        len(selected_new), len(new_posts),
                        len(selected_failed), len(failed_posts),
                    )
                else:
                    posts = new_posts + failed_posts
                    if not posts:
                        break

                for p in posts:
                    seen_ids.add(p["id"])

                round_stats = {"total": len(posts), "success": 0, "rejected": 0, "failed": 0, "skipped": 0}
                if parallel and len(posts) > 1 and self.max_workers > 1:
                    actual_workers = min(self.max_workers, len(posts))
                    executor = ThreadPoolExecutor(max_workers=actual_workers)
                    had_timeout = False
                    try:
                        fut_map = {executor.submit(self._run_post_task, p): p for p in posts}
                        for fut in as_completed(fut_map):
                            post = fut_map[fut]
                            try:
                                _, outcome, _ = fut.result(timeout=120)
                                if outcome == "ok":
                                    round_stats["success"] += 1
                                elif outcome == "rejected":
                                    round_stats["rejected"] += 1
                                elif outcome == "skipped":
                                    round_stats["skipped"] += 1
                                else:
                                    round_stats["failed"] += 1
                            except FuturesTimeoutError:
                                had_timeout = True
                                round_stats["failed"] += 1
                                self.db.mark_post_failed(post["id"], "pool_timeout_120s", increment_retry=True)
                                logger.warning("[GEMINI_POOL] timeout >120s cho bài %s", post["id"])
                            except Exception as e:
                                round_stats["failed"] += 1
                                self.db.mark_post_failed(post["id"], str(e)[:200], increment_retry=True)
                    finally:
                        executor.shutdown(wait=not had_timeout)
                else:
                    for p in posts:
                        _, outcome, _ = self._process_single_post(p)
                        if outcome == "ok":
                            round_stats["success"] += 1
                        elif outcome == "rejected":
                            round_stats["rejected"] += 1
                        elif outcome == "skipped":
                            round_stats["skipped"] += 1
                        else:
                            round_stats["failed"] += 1

                for k in agg:
                    agg[k] += round_stats[k]

                if not adaptive_mode:
                    break
                if round_stats["failed"] == len(posts) and round_stats["success"] == 0:
                    # API/proxy lỗi hàng loạt — dừng vòng lặp bù ngay
                    break
                if round_stats["rejected"] == 0 and round_stats["skipped"] == 0 and round_stats["failed"] == 0:
                    break

            if agg["total"] > 0:
                elapsed = time.time() - t0
                logger.info(
                    "[GEMINI_POOL] Xong %d bài trong %.1fs: ok=%d reject=%d fail=%d skip=%d",
                    agg["total"], elapsed, agg["success"], agg["rejected"], agg["failed"], agg["skipped"],
                )
            gc.collect()
            return agg

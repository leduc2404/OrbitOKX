"""Multi-account OKX Square poster — Sign & Publish engine.

Kế thừa cấu trúc AccountWorker / MultiAccountPoster / PostDistributor từ
BinanceSquare/services/poster/api_poster.py, nhưng:
  * Đăng bài bằng okx.square.publish (ECDSA + HMAC + x-sec-token, pure Python),
    KHÔNG Node header generator, KHÔNG browser.
  * Mỗi account một OkxClient riêng (TLS fingerprint + proxy + cookies/keys).
  * Hết hạn session phát hiện chủ động qua JWT `exp` (okx.config.token_expiry),
    kết hợp phát hiện file cookie cập nhật để nạp lại session.
  * Mọi lỗi nghiêm trọng -> TelegramAlerts (account ID + summary + stack trace)
    và ghi task_logs; quarantine khi quá ngưỡng lỗi liên tiếp.
  * dry_run MẶC ĐỊNH BẬT (config publish.dry_run) — chỉ dựng payload, không đăng.

Giãn cách đăng: Hourly Heatmap (GMT+7) + Gaussian/Uniform random distribution.
"""
import hashlib
import os
import random
import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
from typing import Dict, List, Optional

from api.session_manager import build_client
from infra.config_helper import load_profiles_config
from infra.database import DatabaseManager
from infra.logging_config import get_logger
from okx import square
from okx.client import OkxError
from services.processor.content_tags import extract_coin_tags
from services.telegram.alerts import TelegramAlerts, get_alerts

logger = get_logger(__name__)


# ======================================================================
# ACCOUNT MODEL
# ======================================================================
class PostingAccount:
    """Một account đăng bài + trạng thái runtime (đồng bộ xuống DB accounts)."""

    def __init__(self, raw: dict, config_index: int):
        self.raw = raw
        self.config_index = config_index
        self.name = raw.get("name", f"account_{config_index}")
        self.cookies_file = raw.get("cookies_file", "")
        self.keys_file = raw.get("keys_file", "")
        self.proxy = raw.get("proxy", "")
        self.weight = float(raw.get("weight", 1.0))
        self.fingerprint = raw.get("fingerprint", "")
        self.enabled = bool(raw.get("enabled", True))
        # runtime
        self.consecutive_failures = 0
        self.is_quarantined = False
        self.quarantine_time: Optional[float] = None
        self.last_error: Optional[str] = None
        self.posts_count = 0


class GlobalPostingGate:
    """Giới hạn concurrency VÀ giãn cách tối thiểu toàn hệ thống giữa các lần đăng."""

    def __init__(self, max_concurrent: int = 5, min_global_gap_seconds: float = 45.0):
        self._sem = threading.Semaphore(max(1, int(max_concurrent)))
        self.min_gap_seconds = max(0.0, float(min_global_gap_seconds))
        self._gap_lock = threading.Lock()
        self._next_allowed_mono = 0.0

    def request_posting(self, account_name: str, stop_event: Optional[threading.Event] = None) -> bool:
        del account_name
        if self.min_gap_seconds > 0:
            with self._gap_lock:
                now = time.monotonic()
                slot_at = max(now, self._next_allowed_mono)
                self._next_allowed_mono = slot_at + self.min_gap_seconds
            wait_s = slot_at - time.monotonic()
            if wait_s > 0:
                if stop_event is not None:
                    if stop_event.wait(wait_s):
                        return False
                else:
                    time.sleep(wait_s)
        self._sem.acquire()
        if stop_event is not None and stop_event.is_set():
            self._sem.release()
            return False
        return True

    def release_posting(self, account_name: str):
        del account_name
        self._sem.release()


class ResourceManager(GlobalPostingGate):
    """Alias tương thích ngược cho GlobalPostingGate."""
    pass


# ======================================================================
# ROUND-ROBIN DISTRIBUTOR
# ======================================================================
class PostDistributor:
    """Phân phối bài round-robin toàn cục: một con trỏ chung chia lần lượt
    cho các account active (bỏ qua account bị quarantine). Mỗi bài chỉ về
    đúng một account nhờ atomic claim trong DB."""

    def __init__(self, db: DatabaseManager, accounts: List[PostingAccount],
                 priority_profiles: Optional[List[str]] = None):
        self.db = db
        self.accounts = accounts
        self.priority_profiles = priority_profiles or []
        self.queues: Dict[str, Queue] = {a.name: Queue() for a in accounts}
        self.lock = threading.Lock()
        self.cursor = 0
        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("PostDistributor started (global round-robin)")

    def stop(self):
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._distribute()
                self.stop_event.wait(timeout=5)
            except Exception as e:
                logger.warning("PostDistributor error: %s", e)
                self.stop_event.wait(timeout=10)

    def _distribute(self):
        active = [a for a in self.accounts if not a.is_quarantined]
        if not active:
            return
        needing = [a for a in active if self.queues[a.name].qsize() < 2]
        if not needing:
            return
        with self.lock:
            posts = self.db.get_and_claim_posts_batch(
                count=len(needing), priority_profiles=self.priority_profiles)
        if not posts:
            return
        for i, post in enumerate(posts):
            if i >= len(needing):
                break
            idx = (self.cursor + i) % len(needing)
            self.queues[needing[idx].name].put(post)
        self.cursor = (self.cursor + len(posts)) % max(len(needing), 1)

    def get_post_for_account(self, account_name: str, timeout: float = 30) -> Optional[Dict]:
        try:
            return self.queues[account_name].get(timeout=timeout)
        except Empty:
            return None


# ======================================================================
# HELPERS
# ======================================================================
def _parse_tags(hashtags_str: str, default_tags: Optional[List[str]], content: str = "") -> List[str]:
    raw_fallback = list(default_tags or [])
    if hashtags_str:
        for t in re.split(r"[,\s#]+", hashtags_str):
            t = t.strip().lstrip("$")
            if t:
                raw_fallback.append(t)
    if content:
        tags = extract_coin_tags(content, fallback_tags=raw_fallback, max_tags=3)
        for dt in (default_tags or []):
            cand = str(dt or "").strip().lstrip("$#").upper()
            if cand and cand not in tags and len(tags) < 3:
                tags.append(cand)
        return tags[:3]
    return extract_coin_tags("", fallback_tags=raw_fallback, max_tags=3)


def _is_auth_error(msg: str) -> bool:
    """Heuristic phát hiện lỗi auth/session từ thông điệp OKX (best-effort)."""
    m = (msg or "").lower()
    return any(k in m for k in ("401", "403", "unauthorized", "token", "login",
                                "auth", "session", "expired", "not login"))


def _parse_error_code(msg: str) -> str:
    m = re.search(r"code=(\w+)", msg or "")
    if m:
        return m.group(1)
    m = re.search(r"HTTP (\d+)", msg or "")
    return m.group(1) if m else "UNKNOWN"


def _is_transient_failure(code: str, msg: str) -> tuple[bool, bool]:
    """Trả về (is_auth, is_transient) để phân biệt lỗi tạm thời (release_post) vs vĩnh viễn."""
    is_auth = _is_auth_error(msg) or code == "NO_SESSION"
    is_transient = is_auth or any(
        kw in (msg or "").lower()
        for kw in ("timeout", "timed out", "connection", "proxy", "temporarily", "429", "rate limit")
    )
    return is_auth, is_transient


def publish_post(client, content: str, image_paths: List[str], tags: List[str],
                 publish_cfg: dict, dry_run: bool) -> dict:
    """Sign & publish một bài qua okx.square. Trả dict kết quả chuẩn hoá.

    Dùng chung cho AccountWorker (loop) và MultiAccountPoster.post_batch (one-shot).
    """
    try:
        if client is None:
            return {"success": False, "error_code": "NO_SESSION",
                    "error_message": "No active session", "error_stage": "session"}
        requested_images = [p for p in (image_paths or []) if p]
        valid_images = [os.path.abspath(p) for p in requested_images if os.path.exists(p)]
        if requested_images and not valid_images:
            return {"success": False, "error_code": "MISSING_IMAGES",
                    "error_message": f"Image file(s) missing on disk: {requested_images}",
                    "error_stage": "image"}
        result = square.publish(
            content, images=valid_images, tags=tags,
            group=(publish_cfg or {}).get("group", "USDT"), dry_run=dry_run, client=client)
        if dry_run:
            return {"success": True, "dry_run": True, "okx_post_id": "DRY_RUN",
                    "body": result.get("body")}
        resp = result.get("response") or {}
        okx_id = resp.get("contentId") or resp.get("postId") or resp.get("id")
        return {"success": True, "okx_post_id": str(okx_id) if okx_id else None}
    except OkxError as e:
        return {"success": False, "error_code": _parse_error_code(str(e)),
                "error_message": str(e), "error_stage": "publish"}
    except Exception as e:
        return {"success": False, "error_code": "WORKER_EXCEPTION",
                "error_message": str(e), "error_stage": "exception",
                "stack": traceback.format_exc()}


# ======================================================================
# ACCOUNT WORKER
# ======================================================================
class AccountWorker(threading.Thread):
    """Worker thread cho 1 account: claim post -> sign & publish -> wait -> lặp."""

    def __init__(self, account: PostingAccount, db: DatabaseManager,
                 post_interval: int, stop_event: threading.Event,
                 resource_manager: ResourceManager, alerts: TelegramAlerts,
                 publish_cfg: dict, priority_profiles: Optional[List[str]] = None,
                 post_distributor: Optional[PostDistributor] = None,
                 distribution_mode: str = "lazy_smart",
                 random_distribution_config: Optional[dict] = None,
                 hourly_heatmap_config: Optional[dict] = None,
                 anti_duplicate_config: Optional[dict] = None,
                 startup_delay_seconds: float = 0.0,
                 account_tempo_jitter: bool = True,
                 max_consecutive_failures: int = 5, quarantine_minutes: int = 30,
                 session_refresh_threshold: int = 50):
        super().__init__(daemon=True)
        self.account = account
        self.db = db
        self.post_interval = post_interval
        self.stop_event = stop_event
        self.resource_manager = resource_manager
        self.alerts = alerts
        self.publish_cfg = publish_cfg or {}
        self.dry_run = bool(self.publish_cfg.get("dry_run", True))
        self.priority_profiles = priority_profiles or []
        self.post_distributor = post_distributor
        self.distribution_mode = distribution_mode
        self.random_cfg = random_distribution_config or {}
        self.heatmap_cfg = hourly_heatmap_config or {}
        self.anti_dup_cfg = anti_duplicate_config or {}
        self.startup_delay_seconds = max(0.0, float(startup_delay_seconds or 0.0))
        self.max_consecutive_failures = max_consecutive_failures
        self.quarantine_minutes = quarantine_minutes
        self.session_refresh_threshold = session_refresh_threshold

        # Nhịp cá nhân hóa ổn định theo tên account (tránh các account chạy nhịp y hệt nhau)
        if account_tempo_jitter:
            digest = hashlib.sha256(self.account.name.encode("utf-8")).digest()
            self.tempo_factor = round(0.90 + (digest[0] / 255.0) * 0.22, 3)
            self.hour_shift = (-1, 0, 1)[digest[1] % 3]
        else:
            self.tempo_factor = 1.0
            self.hour_shift = 0

        self.client = None
        self.is_ready = threading.Event()
        self.posts_since_session_create = 0
        self._cookie_mtime = 0.0
        self._token_exp: Optional[int] = None

    # ------------------------------------------------------------------
    # SESSION
    # ------------------------------------------------------------------
    def _create_session(self) -> bool:
        try:
            self.client = build_client(self.account.raw, account_id=self.account.config_index)
            self._token_exp = self.client.cfg.token_exp
            cf = self.account.cookies_file
            self._cookie_mtime = os.path.getmtime(cf) if cf and os.path.exists(cf) else 0.0
            return True
        except Exception as e:
            logger.error("[%s] tạo session lỗi: %s", self.account.name, e)
            self.client = None
            return False

    def _cookie_expired(self) -> bool:
        if self._token_exp is None:
            return False  # không xác định được -> dựa vào lỗi reactive
        return time.time() >= self._token_exp

    def _cookie_file_updated(self) -> bool:
        cf = self.account.cookies_file
        if not cf or not os.path.exists(cf):
            return False
        return os.path.getmtime(cf) > self._cookie_mtime

    # ------------------------------------------------------------------
    # QUARANTINE
    # ------------------------------------------------------------------
    def _enter_quarantine(self, reason: str, is_cookie_expired: bool = False):
        self.account.is_quarantined = True
        self.account.quarantine_time = time.time()
        self.account.last_error = f"QUARANTINED: {reason}"
        until = int(time.time() + self.quarantine_minutes * 60) if not is_cookie_expired else None
        self.db.update_account_status(self.account.config_index, "quarantined",
                                      quarantine_until=until, last_error=reason)
        logger.warning("[%s] QUARANTINED: %s", self.account.name, reason)
        self.alerts.alert(self.account.config_index,
                          f"Account quarantined: {reason}",
                          level="ERROR", event="quarantine", component="poster")

    def _resume(self):
        self.account.is_quarantined = False
        self.account.quarantine_time = None
        self.account.consecutive_failures = 0
        self.db.reset_account_failures(self.account.config_index)
        self.alerts.notify(f"✅ <b>{self.account.name}</b> resumed (session refreshed).")

    # ------------------------------------------------------------------
    # INTERVAL (heatmap + random distribution)
    # ------------------------------------------------------------------
    def _effective_interval(self):
        if not self.heatmap_cfg.get("enabled", False):
            return max(1, int(self.post_interval * self.tempo_factor)), 1.0, -1
        vn_hour = (datetime.now(timezone.utc) + timedelta(hours=7)).hour
        shifted_hour = (vn_hour + self.hour_shift) % 24
        mults = self.heatmap_cfg.get("hourly_multipliers", {})
        mult = mults.get(shifted_hour, mults.get(str(shifted_hour), 1.0))
        try:
            mult = float(mult)
        except (ValueError, TypeError):
            mult = 1.0
        min_floor = 1 if self.post_interval < 30 else 30
        return max(min_floor, int(self.post_interval * mult * self.tempo_factor)), mult, shifted_hour

    def _compute_delay(self) -> int:
        target, mult, vn_hour = self._effective_interval()
        if self.random_cfg.get("enabled", False):
            min_val = target * self.random_cfg.get("min_ratio", 0.75)
            max_val = target * self.random_cfg.get("max_ratio", 1.30)
            if self.random_cfg.get("type", "gaussian") == "gaussian":
                sigma = target * self.random_cfg.get("std_dev_ratio", 0.15)
                val = random.gauss(target, sigma)
                return int(max(min_val, min(val, max_val)))
            return random.randint(int(min_val), int(max_val))
        return random.randint(int(target * 0.8), int(target * 1.2))

    # ------------------------------------------------------------------
    # PUBLISH
    # ------------------------------------------------------------------
    def _post_single(self, content: str, image_paths: List[str], tags: List[str]) -> dict:
        return publish_post(self.client, content, image_paths, tags,
                            self.publish_cfg, self.dry_run)

    def _claim(self) -> Optional[Dict]:
        if self.distribution_mode == "round_robin" and self.post_distributor:
            return self.post_distributor.get_post_for_account(self.account.name, timeout=30)
        if self.anti_dup_cfg.get("enabled", True):
            return self.db.claim_post_smart(
                self.account.name,
                priority_profiles=self.priority_profiles,
                recent_similarity_window_hours=int(self.anti_dup_cfg.get("recent_similarity_window_hours", 12)),
                similarity_threshold=float(self.anti_dup_cfg.get("similarity_threshold", 0.58)),
                coin_cooldown_minutes=int(self.anti_dup_cfg.get("coin_cooldown_minutes", 30)),
            )
        return self.db.claim_post(self.account.name,
                                  priority_profiles=self.priority_profiles)

    # ------------------------------------------------------------------
    # MAIN LOOP
    # ------------------------------------------------------------------
    def run(self):
        logger.info("[%s] starting worker (dry_run=%s, tempo=%.3f, shift=%+dh, stagger=%.1fs)",
                    self.account.name, self.dry_run, self.tempo_factor, self.hour_shift,
                    self.startup_delay_seconds)
        if not self._create_session():
            logger.warning("[%s] session khởi tạo thất bại, sẽ thử lại trong loop", self.account.name)
        self.is_ready.set()

        if self.startup_delay_seconds > 0:
            if self.stop_event.wait(self.startup_delay_seconds):
                self._cleanup_worker()
                return

        try:
            while not self.stop_event.is_set():
                post_id = None
                try:
                    # 1. Cookie file cập nhật -> nạp lại session
                    if self._cookie_file_updated():
                        if self._create_session() and not self._cookie_expired():
                            if self.account.is_quarantined:
                                self._resume()
                            continue
                    # 2. JWT expiry -> quarantine
                    if not self.account.is_quarantined and self._cookie_expired():
                        self._enter_quarantine("JWT token expired", is_cookie_expired=True)
                    # 3. Quarantine -> chờ
                    if self.account.is_quarantined:
                        self.stop_event.wait(60)
                        continue
                    # 4. Session validity
                    if self.client is None:
                        if not self._create_session():
                            self.stop_event.wait(30)
                            continue
                    # 5. Claim post (lazy_smart / round_robin / racing)
                    post = self._claim()
                    if not post:
                        self.stop_event.wait(10)
                        continue
                    post_id = post["id"]
                    content = post.get("gemini_output") or post.get("text") or ""
                    img_str = post.get("image_paths") or ""
                    images = [p.strip() for p in img_str.split("|") if p.strip()]
                    tags = _parse_tags(
                        post.get("hashtags", ""),
                        self.publish_cfg.get("default_tags"),
                        content=content,
                    )

                    if not self.resource_manager.request_posting(self.account.name, stop_event=self.stop_event):
                        self.db.release_post(post_id)
                        break

                    result = None
                    try:
                        if not self.db.verify_claimed(post_id, self.account.name):
                            logger.warning("[%s] claim verification failed cho %s, bỏ qua",
                                           self.account.name, post_id)
                            continue
                        logger.info("[%s] posting %s (%s ảnh, tags=%s)",
                                    self.account.name, post_id, len(images), tags)
                        result = self._post_single(content, images, tags)
                    finally:
                        self.resource_manager.release_posting(self.account.name)

                    if result and result.get("success"):
                        self.db.mark_post_posted(post_id, self.account.name,
                                                 okx_post_id=result.get("okx_post_id"))
                        self.db.reset_account_failures(self.account.config_index)
                        self.db.touch_account_post(self.account.config_index)
                        self.account.posts_count += 1
                        self.account.consecutive_failures = 0
                        self.account.last_error = None
                        self.posts_since_session_create += 1
                        logger.info("[%s] ✓ posted %s (total %s)",
                                    self.account.name, post_id, self.account.posts_count)
                        if self.posts_since_session_create >= self.session_refresh_threshold:
                            if self._create_session():
                                self.posts_since_session_create = 0
                    elif result is not None:
                        self._handle_failure(post_id, result)

                    self.stop_event.wait(self._compute_delay())

                except Exception as e:
                    logger.error("[%s] loop error: %s", self.account.name, e)
                    if post_id:
                        self.db.release_post(post_id)
                    self.alerts.alert(self.account.config_index, f"Worker loop error: {e}",
                                      traceback.format_exc(), level="ERROR",
                                      event="worker_exception", component="poster", post_id=post_id)
                    self.stop_event.wait(30)
        finally:
            self._cleanup_worker()

    def _cleanup_worker(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        try:
            self.db.close()
        except Exception:
            pass
        logger.info("[%s] worker stopped", self.account.name)

    def _handle_failure(self, post_id: str, result: dict):
        code = result.get("error_code", "UNKNOWN")
        msg = result.get("error_message", "Unknown error")
        is_auth, is_transient = _is_transient_failure(code, msg)
        if is_transient:
            # Lỗi auth/session/mạng của 1 account -> trả bài lại queue cho account khác
            self.db.release_post(post_id)
        else:
            self.db.mark_post_failed(post_id, f"[{code}] {msg}")

        self.account.consecutive_failures += 1
        self.account.last_error = f"[{code}] {msg}"
        fails = self.db.increment_account_failure(self.account.config_index, f"[{code}] {msg}")
        logger.warning("[%s] ✗ %s [%s] %s (fail #%s/%s)",
                       self.account.name, post_id, code, msg, fails, self.max_consecutive_failures)

        if is_auth:
            self.client = None
            self.alerts.alert(self.account.config_index,
                              f"Auth/session error khi đăng {post_id}: [{code}] {msg}",
                              result.get("stack"), level="WARNING",
                              event="auth_error", component="poster", post_id=post_id)
        else:
            self.alerts.alert(self.account.config_index,
                              f"Đăng bài {post_id} thất bại: [{code}] {msg}",
                              result.get("stack"), level="ERROR",
                              event="post_failed", component="poster", post_id=post_id)

        if fails >= self.max_consecutive_failures:
            self._enter_quarantine(f"Too many consecutive failures [{code}]")


# ======================================================================
# MULTI-ACCOUNT POSTER
# ======================================================================
class MultiAccountPoster:
    """Quản lý nhiều AccountWorker + PostDistributor + cookie/alerts."""

    def __init__(self, config_path: str = "config/profiles_config.yaml",
                 db: Optional[DatabaseManager] = None, alerts: Optional[TelegramAlerts] = None):
        self.config_path = config_path
        self.config = load_profiles_config(config_path)
        self.db = db or DatabaseManager()
        self.alerts = alerts or get_alerts(self.db)
        self.posting_config = self.config.get("posting_config", {}) or {}
        self.publish_cfg = self.posting_config.get("publish", {}) or {}
        self.dry_run = bool(self.publish_cfg.get("dry_run", True))
        self.distribution_mode = self.posting_config.get("distribution_mode", "lazy_smart")
        self.priority_profiles = self._priority_profiles()

        self.accounts = self._build_accounts()
        if not self.accounts:
            raise RuntimeError("No active posting accounts found!")

        self.resource_manager = GlobalPostingGate(
            max_concurrent=int(self.posting_config.get("max_concurrent", 5)),
            min_global_gap_seconds=float(self.posting_config.get("min_global_gap_seconds", 45)),
        )
        self.post_distributor = None
        if self.distribution_mode == "round_robin":
            self.post_distributor = PostDistributor(self.db, self.accounts, self.priority_profiles)
        self.workers: List[AccountWorker] = []
        self.stop_event = threading.Event()

    def _priority_profiles(self) -> List[str]:
        out = []
        for p in self.config.get("crawl_profiles", []) or []:
            if p.get("priority") and p.get("username"):
                out.append(p["username"])
        return out

    def _build_accounts(self) -> List[PostingAccount]:
        acc_activity = self.posting_config.get("acc_activity", []) or []
        accounts = []
        for idx, acc in enumerate(self.config.get("posting_accounts", []) or [], start=1):
            if not acc.get("enabled", True):
                continue
            if acc_activity and idx not in acc_activity:
                continue
            accounts.append(PostingAccount(acc, idx))
        return accounts

    def _post_interval(self) -> int:
        return int(self.posting_config.get("post_interval", 900))

    # ------------------------------------------------------------------
    def start_workers(self):
        self.stop_event.clear()
        # Khôi phục toàn bộ bài kẹt ở processing khi khởi động lại
        self.db.cleanup_processing_timeout(timeout_seconds=0)
        # Đồng bộ accounts xuống DB
        for a in self.accounts:
            self.db.upsert_account(a.config_index, a.name,
                                   enabled=a.enabled, fingerprint=a.fingerprint)
        if self.post_distributor:
            self.post_distributor.start()

        random_cfg = self.posting_config.get("random_distribution", {})
        heatmap_cfg = self.posting_config.get("hourly_heatmap", {})
        anti_dup_cfg = self.posting_config.get("anti_duplicate", {"enabled": True})
        max_fails = int(self.posting_config.get("max_consecutive_failures", 5))
        quarantine_min = int(self.posting_config.get("quarantine_minutes", 30))
        refresh = int(self.posting_config.get("session_refresh_threshold", 50))
        interval = self._post_interval()
        stagger_enabled = bool(self.posting_config.get("startup_stagger_enabled", True))
        tempo_jitter = bool(self.posting_config.get("account_tempo_jitter", True))
        min_gap = float(self.posting_config.get("min_global_gap_seconds", 45))
        n_acc = max(1, len(self.accounts))

        for pos, a in enumerate(self.accounts):
            if any(w.account.name == a.name for w in self.workers if w.is_alive()):
                continue
            stagger_delay = 0.0
            if stagger_enabled and n_acc > 1:
                step = max(min_gap, float(interval) / float(n_acc))
                stagger_delay = max(0.0, pos * step + random.uniform(-0.15 * step, 0.15 * step))
                stagger_delay = min(stagger_delay, float(interval))
            worker = AccountWorker(
                a, self.db, interval, self.stop_event,
                self.resource_manager, self.alerts, self.publish_cfg,
                priority_profiles=self.priority_profiles, post_distributor=self.post_distributor,
                distribution_mode=self.distribution_mode, random_distribution_config=random_cfg,
                hourly_heatmap_config=heatmap_cfg, anti_duplicate_config=anti_dup_cfg,
                startup_delay_seconds=stagger_delay, account_tempo_jitter=tempo_jitter,
                max_consecutive_failures=max_fails, quarantine_minutes=quarantine_min,
                session_refresh_threshold=refresh)
            worker.start()
            self.workers.append(worker)
            logger.info("→ %s (interval %ss, stagger %.1fs) started", a.name, interval, stagger_delay)

        for w in self.workers:
            w.is_ready.wait(timeout=30)
        active = sum(1 for w in self.workers if not w.account.is_quarantined)
        logger.info("✓ %s/%s workers active (mode=%s, dry_run=%s)",
                    active, len(self.workers), self.distribution_mode, self.dry_run)

    def wait_for_completion(self):
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        logger.info("Stopping all workers...")
        self.stop_event.set()
        if self.post_distributor:
            self.post_distributor.stop()
        for w in self.workers:
            w.join(timeout=5)
        self.workers = []

    def get_stats(self) -> Dict:
        return {
            "accounts": len(self.accounts),
            "workers_alive": sum(1 for w in self.workers if w.is_alive()),
            "quarantined": sum(1 for a in self.accounts if a.is_quarantined),
            "posted_total": sum(a.posts_count for a in self.accounts),
            "distribution_mode": self.distribution_mode,
            "dry_run": self.dry_run,
            "db": self.db.get_stats(),
        }

    def print_status(self):
        s = self.get_stats()
        print("\n" + "=" * 60)
        print("OKXSquare POSTER STATUS")
        print("=" * 60)
        print(f"Accounts    : {s['accounts']}")
        print(f"Workers     : {s['workers_alive']} alive")
        print(f"Quarantined : {s['quarantined']}")
        print(f"Posted      : {s['posted_total']}")
        print(f"Mode        : {s['distribution_mode']} | dry_run={s['dry_run']}")
        print("=" * 60 + "\n")

    def post_batch(self, per_account: int = 1) -> Dict:
        """One-shot: mỗi account claim tối đa `per_account` bài và đăng đồng bộ.

        Hữu ích cho CLI --once / smoke test. Tôn trọng dry_run (mặc định bật).
        """
        results = {"posted": 0, "failed": 0, "accounts": 0, "dry_run": self.dry_run}
        anti_dup_cfg = self.posting_config.get("anti_duplicate", {"enabled": True}) or {}
        use_smart = bool(anti_dup_cfg.get("enabled", True))
        for a in self.accounts:
            if a.is_quarantined:
                continue
            try:
                client = build_client(a.raw, account_id=a.config_index)
            except Exception as e:
                logger.warning("[%s] không dựng được client: %s", a.name, e)
                continue
            try:
                for _ in range(max(1, per_account)):
                    if use_smart:
                        post = self.db.claim_post_smart(
                            a.name,
                            priority_profiles=self.priority_profiles,
                            recent_similarity_window_hours=int(anti_dup_cfg.get("recent_similarity_window_hours", 12)),
                            similarity_threshold=float(anti_dup_cfg.get("similarity_threshold", 0.58)),
                            coin_cooldown_minutes=int(anti_dup_cfg.get("coin_cooldown_minutes", 30)),
                        )
                    else:
                        post = self.db.claim_post(a.name,
                                                  priority_profiles=self.priority_profiles)
                    if not post:
                        break
                    content = post.get("gemini_output") or post.get("text") or ""
                    img_str = post.get("image_paths") or ""
                    images = [p.strip() for p in img_str.split("|") if p.strip()]
                    tags = _parse_tags(
                        post.get("hashtags", ""),
                        self.publish_cfg.get("default_tags"),
                        content=content,
                    )
                    res = publish_post(client, content, images, tags, self.publish_cfg, self.dry_run)
                    if res.get("success"):
                        self.db.mark_post_posted(post["id"], a.name, okx_post_id=res.get("okx_post_id"))
                        self.db.touch_account_post(a.config_index)
                        results["posted"] += 1
                    else:
                        err_code = res.get("error_code", "UNKNOWN")
                        err_msg = res.get("error_message", "")
                        logger.warning("[%s] ✗ post_batch %s [%s] %s",
                                       a.name, post["id"], err_code, err_msg)
                        _, is_transient = _is_transient_failure(err_code, err_msg)
                        if is_transient:
                            self.db.release_post(post["id"])
                        else:
                            self.db.mark_post_failed(post["id"], err_msg)
                        results["failed"] += 1
            finally:
                client.close()
            results["accounts"] += 1
        logger.info("post_batch: %s", results)
        return results
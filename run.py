"""
OKX Square Auto System - Main Orchestrator
============================================
Tích hợp toàn bộ: Crawl → Gemini Process → Post → Reload → Loop

Các chế độ hoạt động:
  1. Auto liên tục với Background Crawler (RECOMMENDED)
  2. Auto liên tục tuần tự (Sequential)
  3. Chạy 1 chu kỳ đầy đủ (Initial Cycle)
  4. Chạy 1 chu kỳ reload (Reload Cycle)
  5. Chỉ crawl
  6. Chỉ xử lý Gemini
  7. Chỉ đăng bài
  8. Xem thống kê
"""
import time
import os
import sys
import gc
import json
import yaml
import signal
import random
import threading
import traceback
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from collections import deque
import requests

# Force UTF-8 encoding for Windows console if not already wrapped
if sys.platform == 'win32':
    import io
    if getattr(sys.stdout, 'encoding', '').lower() != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    if getattr(sys.stderr, 'encoding', '').lower() != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)


from utils.database_manager import DatabaseManager
from utils.gemini_processor_pool import GeminiProcessorPool
from utils.gemini_processor import GeminiProcessor
from utils.post_scheduler import HourlyPostingScheduler
from core.okx_api import OKXApi
from core.crawler import OKXCrawler
from core.publisher import OKXPublisher
from core.binance_crawler import BinanceCrawler
from core.soso_crawler import SosoValueCrawler
from core.kucoin_crawler import KucoinCrawler
from core.farmer import OKXFarmer
from utils.check_cookie_expiry import CookieExpiryCheckerOKX


# ============================================================
# COOKIE PARSER — Trích xuất token quan trọng từ file JSON
# ============================================================

COOKIES_DIR = "cookies"

def parse_cookie_file(account_id: str) -> Optional[Dict]:
    """
    Đọc file cookie JSON (xuất từ extension) và trích xuất 4 token bắt buộc:
      - token        → authorization (JWT)
      - devId        → dev_id
      - tmx_session_id
      - ok_site_info → x_site_info
    """
    cookie_path = os.path.join(COOKIES_DIR, f"{account_id}.json")
    if not os.path.exists(cookie_path):
        print(f"  ⚠ Bỏ qua: Không tìm thấy file {cookie_path}")
        return None

    with open(cookie_path, "r", encoding="utf-8") as f:
        try:
            cookies = json.load(f)
        except Exception as e:
            print(f"  ❌ Lỗi đọc JSON cookies của {account_id}: {e}")
            return None

    extracted = {
        "id": account_id,
        "dev_id": "",
        "tmx_session_id": "",
        "x_site_info": "",
        "authorization": ""
    }

    for c in cookies:
        name = c.get("name")
        val = c.get("value")
        if name == "token":
            extracted["authorization"] = val
        elif name == "devId":
            extracted["dev_id"] = val
        elif name == "tmx_session_id":
            extracted["tmx_session_id"] = val
        elif name == "ok_site_info":
            extracted["x_site_info"] = val

    if not extracted["authorization"] or not extracted["dev_id"]:
        print(f"  ❌ Cookie của {account_id} thiếu trường 'token' hoặc 'devId'!")
        return None

    return extracted


# ============================================================
# SPIDER STATE — Lưu trạng thái last_crawl cho mỗi tác giả
# ============================================================

STATE_FILE = os.path.join("data", "spider_state.json")
_state_lock = Lock()


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with _state_lock:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)


def get_since_timestamp(state: dict, author_id: str, lookback_hours: int) -> int:
    last_crawl = state.get(author_id)
    if last_crawl:
        return int(last_crawl)
    lookback = datetime.now() - timedelta(hours=lookback_hours)
    return int(lookback.timestamp() * 1000)


# ============================================================
# BACKGROUND CRAWLER — Thread nền tự động Crawl + Gemini
# ============================================================

class BackgroundCrawler:
    """
    Background crawler: chạy Crawl → Gemini Processing định kỳ
    không block luồng posting chính.
    """

    def __init__(self, orchestrator, interval_seconds: int = 1200):
        self.orchestrator = orchestrator
        self.interval_seconds = interval_seconds
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()
        self._last_heartbeat = None
        self.stats = {
            'cycles': 0,
            'total_crawled': 0,
            'total_processed': 0,
            'errors': 0
        }

    def _crawl_loop(self):
        print(f"\n{'='*80}")
        print("BACKGROUND CRAWLER STARTED")
        print(f"Interval: {self.interval_seconds}s ({self.interval_seconds/60:.1f} phút)")
        print(f"{'='*80}\n")
        sys.stdout.flush()

        # Đợi trước khi crawl lần đầu (tránh trùng với initial crawl)
        print(f"⏳ Đợi {self.interval_seconds/60:.1f} phút trước khi background crawl lần đầu...")
        sys.stdout.flush()
        for _ in range(self.interval_seconds):
            if self._stop_event.is_set():
                return
            self._last_heartbeat = datetime.now()
            time.sleep(1)

        while not self._stop_event.is_set():
            self._last_heartbeat = datetime.now()
            cycle_start = datetime.now()

            try:
                print(f"\n{'='*80}")
                print(f"BACKGROUND CRAWL CYCLE #{self.stats['cycles'] + 1}")
                print(f"Time: {cycle_start.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*80}")
                sys.stdout.flush()

                # Crawl
                new_posts = self.orchestrator._do_crawl(mode="reload")
                self.stats['total_crawled'] += new_posts
                self._last_heartbeat = datetime.now()

                # Gemini Processing
                print(f"\n→ Running Gemini processing (background)...")
                sys.stdout.flush()
                process_stats = self.orchestrator.processor.process_batch(parallel=True)
                self.stats['total_processed'] += process_stats['success']
                self._last_heartbeat = datetime.now()

                self.stats['cycles'] += 1

                print(f"\n✓ Background cycle #{self.stats['cycles']}: "
                      f"+{new_posts} crawled, {process_stats['success']} processed")
                sys.stdout.flush()

                gc.collect()

            except Exception as e:
                self.stats['errors'] += 1
                self._last_heartbeat = datetime.now()
                print(f"\n✗ Background crawl error: {e}")
                sys.stdout.flush()
                import traceback
                traceback.print_exc()

            # Chờ interval
            for _ in range(self.interval_seconds):
                if self._stop_event.is_set():
                    break
                self._last_heartbeat = datetime.now()
                time.sleep(1)

        print("\n✓ Background crawler stopped gracefully")
        sys.stdout.flush()

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._last_heartbeat = datetime.now()
        self._thread = threading.Thread(
            target=self._crawl_loop, daemon=False, name="BackgroundCrawler"
        )
        self._thread.start()
        print(f"✓ Background crawler started (interval: {self.interval_seconds/60:.1f}m)")
        sys.stdout.flush()

    def stop(self):
        if not self._running:
            return
        print("\n→ Stopping background crawler...")
        sys.stdout.flush()
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._running = False
        print("✓ Background crawler stopped")
        sys.stdout.flush()

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def is_healthy(self) -> bool:
        if not self.is_running():
            return False
        if self._last_heartbeat:
            return (datetime.now() - self._last_heartbeat).total_seconds() < 120
        return False

    def get_stats(self) -> dict:
        stats = self.stats.copy()
        stats['is_running'] = self.is_running()
        stats['last_heartbeat'] = self._last_heartbeat
        stats['thread_alive'] = self._thread.is_alive() if self._thread else False
        return stats


# ============================================================
# ROUND-ROBIN POSTING ENGINE — Phân phối công bằng cấp thương mại
# ============================================================

class RoundRobinPostingEngine:
    """
    Production-grade Deficit-Based Round-Robin Posting Engine.

    Thuật toán:
      - Mỗi account có 1 "deficit counter" (số bài nợ so với trung bình).
      - Khi phân phối bài mới, account có deficit cao nhất được ưu tiên.
      - Đảm bảo phân phối TUYỆT ĐỐI CÔNG BẰNG ngay cả khi:
        · Số bài < số account (thiếu hụt)
        · Số bài >> số account (dư thừa)
        · Account bị lỗi liên tục → vẫn được ưu tiên lần sau
        · Account mới thêm vào giữa chừng
        · Scale 1 → ∞ account không cần thay đổi logic

    Thread-safe: Toàn bộ state được bảo vệ bởi Lock.
    Persistent: State được lưu xuống disk, survive restart.
    """

    STATE_FILE = os.path.join("data", "rr_posting_state.json")

    def __init__(self, account_ids: List[str]):
        self._lock = Lock()
        self._account_ids = list(account_ids)

        # Core state per account
        # deficit = (expected_posts - actual_posts). Càng cao → càng được ưu tiên
        self._deficit: Dict[str, float] = {}
        self._total_posted: Dict[str, int] = {}
        self._last_posted_at: Dict[str, float] = {}
        self._consecutive_failures: Dict[str, int] = {}

        # Global counters
        self._global_total = 0
        self._round_num = 0

        # Initialize
        self._load_state()
        for acc_id in self._account_ids:
            self._deficit.setdefault(acc_id, 0.0)
            self._total_posted.setdefault(acc_id, 0)
            self._last_posted_at.setdefault(acc_id, 0.0)
            self._consecutive_failures.setdefault(acc_id, 0)

        print(f"  ✓ Round-Robin Engine initialized | {len(self._account_ids)} accounts")
        self._print_state()

    # ---- STATE PERSISTENCE ----

    def _load_state(self):
        if not os.path.exists(self.STATE_FILE):
            return
        try:
            with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._deficit = {str(k): float(v) for k, v in data.get('deficit', {}).items()}
            self._total_posted = {str(k): int(v) for k, v in data.get('total_posted', {}).items()}
            self._last_posted_at = {str(k): float(v) for k, v in data.get('last_posted_at', {}).items()}
            self._consecutive_failures = {str(k): int(v) for k, v in data.get('consecutive_failures', {}).items()}
            self._global_total = int(data.get('global_total', 0))
            self._round_num = int(data.get('round_num', 0))
        except Exception as e:
            print(f"  ⚠ RR state load error (starting fresh): {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            data = {
                'deficit': self._deficit,
                'total_posted': self._total_posted,
                'last_posted_at': self._last_posted_at,
                'consecutive_failures': self._consecutive_failures,
                'global_total': self._global_total,
                'round_num': self._round_num,
                'updated_at': datetime.now().isoformat(),
            }
            with open(self.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"  ⚠ RR state save error: {e}")

    # ---- CORE ALGORITHM: DEFICIT-BASED DISTRIBUTION ----

    def distribute(self, posts: List[Dict], allowed_accounts: List[str] = None) -> Dict[str, List[Dict]]:
        """
        Phân phối N posts cho M accounts theo Deficit-Based Round-Robin.
        Cho phép lọc danh sách account được phép phân phối qua allowed_accounts.
        """
        with self._lock:
            self._round_num += 1
            target_accounts = list(allowed_accounts) if allowed_accounts is not None else self._account_ids
            num_accounts = len(target_accounts)

            if not posts or num_accounts == 0:
                return {acc_id: [] for acc_id in target_accounts}

            # Cap: mỗi account tối đa 5 bài/round (tránh 1 acc bị overload)
            max_per_account = 5

            # Khởi tạo queues
            queues: Dict[str, List[Dict]] = {acc_id: [] for acc_id in target_accounts}

            # Copy working deficit (sẽ mutate trong quá trình phân phối)
            working_deficit = {acc_id: self._deficit.get(acc_id, 0.0) for acc_id in target_accounts}

            # FAIR SHARE: mỗi account "nợ" thêm fair_share bài
            fair_share = len(posts) / num_accounts
            for acc_id in target_accounts:
                working_deficit[acc_id] += fair_share

            # Phân phối từng bài 1 theo greedy highest-deficit
            remaining_posts = list(posts)
            while remaining_posts:
                # Sort accounts theo working_deficit giảm dần
                # Tie-break: account có ít bài nhất tổng cộng → ưu tiên
                candidates = [
                    acc_id for acc_id in target_accounts
                    if len(queues[acc_id]) < max_per_account
                ]
                if not candidates:
                    # Tất cả accounts đã đạt max_per_account → dừng, release phần còn lại
                    break

                candidates.sort(key=lambda x: (
                    -working_deficit[x],
                    self._total_posted.get(x, 0),
                ))

                best = candidates[0]
                post = remaining_posts.pop(0)
                queues[best].append(post)
                working_deficit[best] -= 1.0

            # Log phân phối
            assigned = sum(len(q) for q in queues.values())
            unassigned = len(remaining_posts)

            dist_summary = []
            for acc_id in target_accounts:
                cnt = len(queues[acc_id])
                deficit_val = self._deficit.get(acc_id, 0)
                if cnt > 0:
                    dist_summary.append(f"{acc_id}={cnt} (debt={deficit_val:+.1f})")
            
            print(f"\n  🔄 Round #{self._round_num}: {assigned}/{assigned+unassigned} bài → "
                  f"{', '.join(dist_summary)}")

            self._save_state()
            return queues

    def report_success(self, account_id: str):
        """Gọi sau khi account đăng bài thành công."""
        with self._lock:
            self._total_posted[account_id] = self._total_posted.get(account_id, 0) + 1
            self._last_posted_at[account_id] = time.time()
            self._consecutive_failures[account_id] = 0
            self._global_total += 1

            # Cập nhật deficit: account này đã hoàn thành 1 bài → deficit -= 1
            self._deficit[account_id] = self._deficit.get(account_id, 0.0) - 1.0

            self._save_state()

    def report_failure(self, account_id: str):
        """Gọi khi account đăng bài thất bại (API error, content rejected, etc.)."""
        with self._lock:
            self._consecutive_failures[account_id] = \
                self._consecutive_failures.get(account_id, 0) + 1

            # KHÔNG giảm deficit → account vẫn giữ nguyên "nợ" → được ưu tiên lần sau
            self._save_state()

    def report_skip(self, account_id: str):
        """Gọi khi bài bị skip (content quá dài, validation fail, etc.)."""
        with self._lock:
            # Skip do content, không phải lỗi account → giảm deficit bình thường
            self._deficit[account_id] = self._deficit.get(account_id, 0.0) - 1.0
            self._save_state()

    def get_last_posted_at(self, account_id: str) -> float:
        """Trả về timestamp lần đăng cuối cùng của account."""
        with self._lock:
            return self._last_posted_at.get(account_id, 0.0)

    def get_stats(self) -> Dict:
        """Trả về thống kê toàn bộ engine."""
        with self._lock:
            return {
                'round_num': self._round_num,
                'global_total': self._global_total,
                'per_account': {
                    acc_id: {
                        'total_posted': self._total_posted.get(acc_id, 0),
                        'deficit': self._deficit.get(acc_id, 0.0),
                        'consecutive_failures': self._consecutive_failures.get(acc_id, 0),
                        'last_posted_at': self._last_posted_at.get(acc_id, 0.0),
                    }
                    for acc_id in self._account_ids
                }
            }

    def add_account(self, account_id: str):
        """Hot-add account mới mà không cần restart."""
        with self._lock:
            if account_id not in self._account_ids:
                self._account_ids.append(account_id)
                # Deficit mới = trung bình deficit hiện tại (công bằng với accounts cũ)
                avg_deficit = (
                    sum(self._deficit.values()) / len(self._deficit)
                    if self._deficit else 0.0
                )
                self._deficit[account_id] = avg_deficit
                self._total_posted[account_id] = 0
                self._last_posted_at[account_id] = 0.0
                self._consecutive_failures[account_id] = 0
                print(f"  ✓ RR: Hot-added {account_id} (initial deficit={avg_deficit:.1f})")
                self._save_state()

    def _print_state(self):
        """In trạng thái hiện tại ra console."""
        print(f"  📊 RR State: round={self._round_num}, total={self._global_total}")
        for acc_id in self._account_ids:
            posted = self._total_posted.get(acc_id, 0)
            deficit = self._deficit.get(acc_id, 0.0)
            last = self._last_posted_at.get(acc_id, 0)
            if last > 0:
                ago = time.time() - last
                ago_str = f"{ago/60:.0f}m ago"
            else:
                ago_str = "never"
            print(f"    {acc_id}: posted={posted}, deficit={deficit:+.1f}, last={ago_str}")



# ============================================================
# MAIN ORCHESTRATOR — Trái tim của toàn bộ hệ thống
# ============================================================

class MainOrchestrator:
    """
    Orchestrator chính điều phối toàn bộ quy trình OKX Square:
      Crawl → Gemini Processing → Auto Post → Loop
    """

    def __init__(self, config_path: str = "config/settings.yaml"):
        print("=" * 80)
        print("OKX SQUARE AUTO SYSTEM — INITIALIZING")
        print("=" * 80)

        self.config_path = config_path
        self.config = self._load_config(config_path)

        spider_cfg = self.config.get('spider', {})
        self.reload_interval = spider_cfg.get('crawl_interval_minutes', 20) * 60

        # ======= LỊCH ĐĂNG BÀI THEO KHUNG GIỜ =======
        self.post_scheduler = HourlyPostingScheduler(self.config)
        print(f"→ Posting schedule: {self.post_scheduler.describe()}")
        # Timestamp lần thử đăng gần nhất của mỗi account (sống qua ranh giới 2 batch)
        self._last_post_attempt: Dict[str, float] = {}

        # ======= INIT COMPONENTS =======
        try:
            print("\n→ Initializing Database Manager (WAL mode)...")
            self.db_manager = DatabaseManager()
            recovered = self.db_manager.cleanup_processing_timeout()
            if recovered > 0:
                print(f"  ✓ Khôi phục {recovered} bài 'processing' bị treo về trạng thái 'ready'")

            print("→ Loading account cookies...")
            self.accounts = self._load_accounts()
            if not self.accounts:
                raise RuntimeError("Không có tài khoản hợp lệ!")
            print(f"  ✓ {len(self.accounts)} tài khoản sẵn sàng")

            print("→ Initializing OKX Crawler...")
            crawl_acc = self.accounts[0]
            proxy = self._get_proxy_for_account(crawl_acc['id'])
            self.api_client = OKXApi(crawl_acc, proxy=proxy if proxy else None)
            self.crawler = OKXCrawler(
                self.api_client,
                max_retries=spider_cfg.get('max_retries', 3),
                retry_delay=spider_cfg.get('retry_delay', 5)
            )
            print(f"  ✓ OKX Crawler ready (API account: {crawl_acc['id']})")

            print("→ Initializing Binance Crawler...")
            self.binance_crawler = BinanceCrawler(
                db_manager=self.db_manager,
                max_retries=spider_cfg.get('max_retries', 3),
                retry_delay=spider_cfg.get('retry_delay', 5)
            )
            print("  ✓ Binance Crawler ready")

            print("→ Initializing SoSoValue Crawler...")
            self.soso_crawler = SosoValueCrawler(
                db_manager=self.db_manager,
                max_retries=spider_cfg.get('max_retries', 3),
                retry_delay=spider_cfg.get('retry_delay', 5)
            )
            print("  ✓ SoSoValue Crawler ready")

            print("→ Initializing KuCoin Crawler...")
            self.kucoin_crawler = KucoinCrawler(
                db_manager=self.db_manager,
                max_retries=spider_cfg.get('max_retries', 3),
                retry_delay=spider_cfg.get('retry_delay', 5)
            )
            print("  ✓ KuCoin Crawler ready")

            print("→ Initializing Gemini Processor Pool...")
            self.processor = GeminiProcessorPool()
            print("  ✓ Gemini processor ready")

            self.background_crawler = None
            self._crawl_lock = threading.Lock()

            # ======= ROUND-ROBIN POSTING ENGINE =======
            print("→ Initializing Round-Robin Posting Engine...")
            account_ids = [acc['id'] for acc in self.accounts]
            self.rr_engine = RoundRobinPostingEngine(account_ids)

            # Pre-create publishers (reuse across rounds, tránh khởi tạo lại)
            self._publishers: Dict[str, OKXPublisher] = {}
            for acc in self.accounts:
                proxy = self._get_proxy_for_account(acc['id'])
                self._publishers[acc['id']] = OKXPublisher(acc, proxy=proxy if proxy else None)
            print(f"  ✓ {len(self._publishers)} publishers ready")



            # Farming components (lazy init)
            self._farming_workers = []
            self._farming_threads = []
            self._farming_stop_event = threading.Event()
            self._farming_gemini = None  # Riêng cho farming (Gemma)

            print("\n✓ All core components initialized successfully!")

        except Exception as e:
            print(f"\n✗ Initialization failed: {e}")
            raise

        # Stats
        self.stats = {
            'cycles': 0,
            'total_crawled': 0,
            'total_processed': 0,
            'total_posted': 0,
            'start_time': datetime.now()
        }
        self._should_stop = False

        # Spider state (last_crawl per author)
        self.spider_state = load_state()

    # ==============================================================
    # CONFIG & ACCOUNT LOADING
    # ==============================================================

    def _load_config(self, path: str) -> dict:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config không tồn tại: {path}")
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _load_accounts(self) -> List[Dict]:
        """Load tất cả accounts từ acc_activity config → parse cookie files"""
        # Hỗ trợ format mới: acc_activity: [1, 2, 4]
        acc_activity = self.config.get('acc_activity', [])
        
        if acc_activity:
            # Format mới: số → account{N}
            account_ids = [f"account{n}" for n in acc_activity]
            print(f"  → acc_activity: {acc_activity} → {account_ids}")
        else:
            # Backward compatible: active_accounts: [account1, account2]
            account_ids = self.config.get('active_accounts', [])
            print(f"  → active_accounts (legacy): {account_ids}")
        
        accounts = []
        for acc_id in account_ids:
            acc_data = parse_cookie_file(acc_id)
            if acc_data:
                accounts.append(acc_data)
        return accounts

    def _get_proxy_for_account(self, account_id: str) -> Optional[str]:
        """
        Lấy proxy cho account.
        Hỗ trợ format mới (key là số nguyên, vd: 1) và backward compatible (key là string, vd: 'account1').
        """
        proxies_config = self.config.get('proxies', {})
        if not proxies_config:
            return None
            
        # Thử lấy theo parse index từ 'accountX' -> X
        try:
            if account_id.startswith('account'):
                acc_idx = int(account_id.replace('account', ''))
                if acc_idx in proxies_config and proxies_config[acc_idx]:
                    return proxies_config[acc_idx]
        except ValueError:
            pass
            
        # Thử lấy theo account_id string (backward compatible)
        return proxies_config.get(account_id)

    def _get_all_sources(self) -> List[str]:
        """Lấy toàn bộ danh sách nguồn dữ liệu trong pool hợp nhất."""
        pool_cfg = self.config.get('content_pool', {}) or {}
        sources = pool_cfg.get('sources')
        if sources and isinstance(sources, list):
            return list(sources)
        return ['binance', 'okx', 'sosovalue', 'KuCoin', 'okx_trending']

    def _get_buffer_plan(self) -> Dict:
        """Tính toán target_ready và low_watermark chính xác cho pool hợp nhất."""
        return self.post_scheduler.compute_buffer_target(
            active_account_count=len(self.accounts),
            settings=self.config,
        )

    def _load_group_config(self) -> dict:
        """Backward-compatible wrapper trả về cấu hình pool hợp nhất."""
        all_sources = self._get_all_sources()
        all_acc_nums = []
        for acc in self.accounts:
            try:
                all_acc_nums.append(int(acc['id'].replace('account', '')))
            except ValueError:
                pass
        return {
            'unified': {
                'accounts': all_acc_nums,
                'sources': all_sources,
            },
            'group1': {
                'accounts': all_acc_nums,
                'sources': all_sources,
            },
            'group2': {
                'accounts': all_acc_nums,
                'sources': all_sources,
            },
        }

    def _get_account_group(self, account_id: str) -> str:
        """Backward-compatible: mọi tài khoản đều thuộc pool 'unified'."""
        return 'unified'

    # ==============================================================
    # CRAWL — Thu thập bài viết mới từ các tác giả (Budgeted & Unified)
    # ==============================================================

    def _do_crawl(self, mode: str = "initial", max_new_posts: Optional[int] = None) -> int:
        """
        Thực hiện crawl bài viết thông minh theo ngân sách (crawl_deficit).
        - Tính tổng bài hiện có trong DB: ready (kể cả processing) + crawled.
        - Nếu ready + crawled >= target_ready -> Bỏ qua crawl hoàn toàn (0 HTTP requests).
        - Nếu thiếu bài -> Chỉ cào tối đa đúng số bài còn thiếu (crawl_deficit), dừng sớm ngay khi đủ.
        """
        with self._crawl_lock:
            try:
                reloaded = self._load_config(self.config_path)
                if isinstance(reloaded, dict):
                    self.config = reloaded
                    self.post_scheduler.update_from_settings(self.config)
            except Exception:
                pass

            all_sources = self._get_all_sources()
            buffer_plan = self._get_buffer_plan()
            target_ready = buffer_plan['target_ready']

            ready_count = self.db_manager.count_ready_posts_by_sources(
                all_sources, include_processing=True
            )
            crawled_count = self.db_manager.count_crawled_posts_by_sources(all_sources)
            total_buffered = ready_count + crawled_count

            crawl_deficit = max(0, target_ready - total_buffered)
            if max_new_posts is not None:
                crawl_deficit = min(crawl_deficit, max(0, int(max_new_posts)))

            if crawl_deficit <= 0:
                print(
                    f"  [⏳] Unified buffer đã đủ (ready={ready_count}, crawled={crawled_count}, "
                    f"target={target_ready} cho {len(self.accounts)} account). "
                    f"Bỏ qua crawl để tránh cào vô nghĩa."
                )
                return 0

            print(
                f"  [🕷️] Unified buffer cần thêm tối đa {crawl_deficit} bài "
                f"(ready={ready_count}, crawled={crawled_count}, target={target_ready}). "
                f"Bắt đầu cào có ngân sách..."
            )

            # Danh sách các bộ cào theo nguồn đã bật, xoay vòng ngẫu nhiên để đa dạng nguồn bài
            source_crawlers = []
            if "binance" in all_sources:
                source_crawlers.append(("Binance", self._crawl_binance))
            if "okx_trending" in all_sources:
                source_crawlers.append(("OKX Trending", self._crawl_okx_trending))
            if "sosovalue" in all_sources:
                source_crawlers.append(("SoSo", self._crawl_soso))
            if "KuCoin" in all_sources:
                source_crawlers.append(("KuCoin", self._crawl_kucoin))
            if "okx" in all_sources:
                source_crawlers.append(("OKX", self._crawl_okx))

            random.shuffle(source_crawlers)

            total_new = 0
            for name, crawler_fn in source_crawlers:
                remaining_budget = crawl_deficit - total_new
                if remaining_budget <= 0:
                    break
                try:
                    fetched = crawler_fn(mode, max_new_posts=remaining_budget)
                    total_new += max(0, int(fetched or 0))
                    if total_new >= crawl_deficit:
                        print(
                            f"  [🎯] Đã cào đủ ngân sách bài mới ({total_new}/{crawl_deficit}). "
                            f"Dừng sớm các nguồn còn lại."
                        )
                        break
                except Exception as e:
                    print(f"  ✗ Lỗi crawl {name} orchestrator: {e}")

            save_state(self.spider_state)
            return total_new

    def _check_and_trigger_active_crawl_processing(self):
        """
        Kiểm tra nếu số bài viết sẵn sàng (ready) của pool hợp nhất thấp hơn low_watermark:
        1. Nếu trong DB đang có sẵn bài thô (status='crawled') -> ưu tiên xử lý Gemini trước (không cần cào).
        2. Nếu sau đó (hoặc ban đầu) tổng ready + crawled vẫn chưa đủ target_ready -> kích hoạt crawl có ngân sách rồi xử lý Gemini.
        """
        try:
            reloaded = self._load_config(self.config_path)
            if isinstance(reloaded, dict):
                self.config = reloaded
                self.post_scheduler.update_from_settings(self.config)
        except Exception:
            pass

        if not self.accounts:
            return

        all_sources = self._get_all_sources()
        buffer_plan = self._get_buffer_plan()
        target_ready = buffer_plan['target_ready']
        low_watermark = buffer_plan['low_watermark']

        ready_count = self.db_manager.count_ready_posts_by_sources(
            all_sources, include_processing=True
        )
        if ready_count >= low_watermark:
            return

        crawled_in_db = self.db_manager.count_crawled_posts_by_sources(all_sources)
        print(
            f"\n🚨 [Unified Buffer Alert] Ready posts ({ready_count}) thấp hơn ngưỡng "
            f"low_watermark ({low_watermark}/{target_ready}, crawled_queue={crawled_in_db})!"
        )

        if crawled_in_db > 0:
            print(
                f"⚡ [Smart Handling] Có sẵn {crawled_in_db} bài thô (status='crawled') trong DB. "
                f"Ưu tiên chạy Gemini xử lý hàng chờ trước..."
            )
            process_stats = self.processor.process_batch(parallel=True)
            self.stats['total_processed'] += process_stats.get('success', 0)
            print(
                f"  ✓ Gemini xử lý xong hàng chờ: "
                f"{process_stats.get('success', 0)}/{process_stats.get('total', 0)} bài."
            )
            ready_count = self.db_manager.count_ready_posts_by_sources(
                all_sources, include_processing=True
            )
            crawled_in_db = self.db_manager.count_crawled_posts_by_sources(all_sources)

        if ready_count < low_watermark and (ready_count + crawled_in_db) < target_ready:
            deficit = max(0, target_ready - (ready_count + crawled_in_db))
            print(
                f"⚡ [Smart Handling] Hàng chờ vẫn thiếu {deficit} bài để đạt target={target_ready}. "
                f"Chủ động cào bài mới..."
            )
            new_posts = self._do_crawl(mode="reload", max_new_posts=deficit)
            self.stats['total_crawled'] += new_posts
            print(f"  ✓ Active crawl hoàn tất: +{new_posts} bài mới.")

            if new_posts > 0 or self.db_manager.count_crawled_posts_by_sources(all_sources) > 0:
                print(f"  → Chạy Gemini xử lý bài mới cào...")
                process_stats = self.processor.process_batch(parallel=True)
                self.stats['total_processed'] += process_stats.get('success', 0)
                print(
                    f"  ✓ Gemini xử lý hoàn tất: "
                    f"{process_stats.get('success', 0)}/{process_stats.get('total', 0)} bài."
                )

    def _crawl_okx(self, mode: str = "initial", max_new_posts: Optional[int] = None) -> int:
        spider_cfg = self.config.get('spider', {})
        target_authors = spider_cfg.get('target_authors', [])
        max_workers = spider_cfg.get('max_workers', 3)
        delay_between = spider_cfg.get('delay_between_authors', 2)
        lookback_hours = spider_cfg.get('default_lookback_hours', 2)

        if not target_authors:
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0

        total_new = 0
        count_lock = threading.Lock()
        authors_list = list(target_authors)
        random.shuffle(authors_list)

        def budget_reached() -> bool:
            if max_new_posts is None:
                return False
            with count_lock:
                return total_new >= max_new_posts

        def crawl_single(author_id):
            """Worker: crawl 1 tác giả → download ảnh → save DB"""
            nonlocal total_new
            if budget_reached():
                return {'new': 0, 'error': None}

            since_ts = get_since_timestamp(self.spider_state, author_id, lookback_hours)
            result = {'new': 0, 'error': None}

            try:
                posts = self.crawler.get_user_timeline(author_id, since_timestamp=since_ts)

                if posts is None or not posts:
                    self.spider_state[author_id] = int(time.time() * 1000)
                    return result

                # Batch check existing
                post_ids = [p['post_id'] for p in posts if p.get('post_id')]
                existing = self.db_manager.batch_check_existing_posts(post_ids)

                for post in posts:
                    if budget_reached():
                        break
                    pid = post.get('post_id')
                    if not pid or pid in existing:
                        continue

                    # Download images
                    local_images = self.crawler.download_images(pid, post.get('image_urls', []))

                    # ====== QR CODE CHECK ======
                    if local_images is None:
                        print(f"   [🚫] Bỏ qua bài {pid} vì chứa mã QR")
                        continue

                    url = f"https://www.okx.com/square/post/{pid}"
                    create_time = None
                    if post.get('publish_time'):
                        try:
                            create_time = int(post['publish_time'])
                        except (ValueError, TypeError):
                            pass

                    success = self.db_manager.insert_post_with_time(
                        post_id=pid,
                        url=url,
                        text=post.get('text', ''),
                        image_paths=local_images,
                        crawled_from=author_id,
                        create_time=create_time,
                        source="okx"
                    )
                    if success:
                        with count_lock:
                            total_new += 1
                            result['new'] += 1

                if not budget_reached():
                    self.spider_state[author_id] = int(time.time() * 1000)

            except Exception as e:
                result['error'] = str(e)
                print(f"  ✗ Lỗi crawl {author_id}: {e}")

            if delay_between > 0 and not budget_reached():
                time.sleep(delay_between)

            return result

        # Chạy đa luồng
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(crawl_single, aid): aid for aid in authors_list}
            for future in as_completed(futures):
                author_id = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"  ✗ Exception từ thread {author_id}: {e}")

        return total_new

    def _crawl_okx_trending(self, mode: str = "initial", max_new_posts: Optional[int] = None) -> int:
        spider_cfg = self.config.get('spider', {})
        if not spider_cfg.get('okx_trending_enabled', True):
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0
            
        print(f"  [🕷️] Bắt đầu cào OKX Trending Hashtags (budget={max_new_posts})...")
        top_n = spider_cfg.get('trending_top_n', 3)
        posts_per_topic = spider_cfg.get('trending_posts_per_topic', 6)
        if max_new_posts is not None:
            posts_per_topic = min(posts_per_topic, max(2, max_new_posts))
        
        try:
            posts = self.crawler.crawl_trending_posts(top_n=top_n, posts_per_topic=posts_per_topic)
            if not posts:
                return 0
                
            total_new = 0
            post_ids = [p['post_id'] for p in posts if p.get('post_id')]
            existing = self.db_manager.batch_check_existing_posts(post_ids)
            
            for post in posts:
                if max_new_posts is not None and total_new >= max_new_posts:
                    break
                pid = post.get('post_id')
                if not pid or pid in existing:
                    continue
                    
                local_images = self.crawler.download_images(pid, post.get('image_urls', []))
                if local_images is None:
                    print(f"   [🚫] Bỏ qua bài {pid} vì chứa mã QR")
                    continue
                    
                url = f"https://www.okx.com/square/post/{pid}"
                create_time = None
                if post.get('publish_time'):
                    try:
                        create_time = int(post['publish_time'])
                    except Exception:
                        pass
                        
                success = self.db_manager.insert_post_with_time(
                    post_id=pid,
                    url=url,
                    text=post.get('text', ''),
                    image_paths=local_images,
                    crawled_from=post.get('author_id', 'okx_trending'),
                    create_time=create_time,
                    source="okx_trending",
                    hashtags=post.get('hashtags')
                )
                if success:
                    total_new += 1
                    
            return total_new
        except Exception as e:
            print(f"  ✗ Lỗi crawl OKX Trending: {e}")
            return 0

    def _crawl_binance(self, mode: str = "initial", max_new_posts: Optional[int] = None) -> int:
        spider_cfg = self.config.get('spider', {})
        binance_authors = spider_cfg.get('binance_authors', [])
        max_workers = spider_cfg.get('max_workers', 3)
        lookback_hours = float(spider_cfg.get('default_lookback_hours', 2))
        if not binance_authors:
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0
        return self.binance_crawler.crawl_all_users(
            binance_authors,
            max_workers=max_workers,
            max_new_posts=max_new_posts,
            lookback_hours=lookback_hours,
        )

    def _crawl_soso(self, mode: str = "initial", max_new_posts: Optional[int] = None) -> int:
        spider_cfg = self.config.get('spider', {})
        if not spider_cfg.get('soso_enabled', False):
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0
        max_pages = 2 if mode == "initial" else 1
        return self.soso_crawler.crawl_news(max_pages=max_pages, max_new_posts=max_new_posts)

    def _crawl_kucoin(self, mode: str = "initial", max_new_posts: Optional[int] = None) -> int:
        spider_cfg = self.config.get('spider', {})
        if not spider_cfg.get('kucoin_enabled', True):
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0
        max_pages = 2 if mode == "initial" else 1
        return self.kucoin_crawler.crawl_news(max_pages=max_pages, max_new_posts=max_new_posts)

    # ==============================================================
    # POST — Đăng bài lên OKX Square (Round-Robin Fair Distribution)
    # ==============================================================

    # Giới hạn ký tự tối đa của OKX Square
    OKX_MAX_CONTENT_LENGTH = 2100

    def _account_poster_with_queue(self, acc_id: str, posts: List[Dict]) -> Dict:
        """
        Worker đăng bài cho 1 account từ queue đã được phân sẵn bởi Round-Robin.

        KHÔNG CÓ claim race condition — bài đã được phân công trước.
        Resilience:
          - Lỗi ở bài viết → skip bài, report_failure, KHÔNG dừng account
          - Content quá dài → skip, report_skip
          - Mọi kết quả đều được report lại cho RR engine
        """
        spider_cfg = self.config.get('spider', {})
        post_with_trending = spider_cfg.get('post_with_trending_hashtags', False)
        
        stats = {'total': 0, 'success': 0, 'failed': 0}

        # Timestamp bài gần nhất (kể từ batch trước) để nhịp đăng không bị
        # "reset" ở ranh giới 2 batch.
        last_attempt = self._last_post_attempt.get(acc_id) or self.rr_engine.get_last_posted_at(acc_id)

        publisher = self._publishers.get(acc_id)
        if not publisher:
            # Fallback: tạo publisher mới nếu chưa có
            acc_data = next((a for a in self.accounts if a['id'] == acc_id), None)
            if not acc_data:
                return stats
            proxy = self._get_proxy_for_account(acc_id)
            publisher = OKXPublisher(acc_data, proxy=proxy if proxy else None)

        for idx, post_data in enumerate(posts):
            if self._should_stop:
                # Release các bài chưa đăng
                remaining_ids = [p['id'] for p in posts[idx:]]
                if remaining_ids:
                    self.db_manager.release_batch(remaining_ids)
                break

            stats['total'] += 1
            post_id = post_data['id']
            content = post_data['gemini_output']

            # ====== PRE-VALIDATION: Kiểm tra nội dung chính trị / bị từ chối ======
            if content == "[REJECT]":
                print(f"  ⏭️ [{acc_id}] Skip {post_id}: Phát hiện nội dung nhạy cảm/chính trị [REJECT]")
                self.db_manager.skip_post_permanently(post_id, "Nội dung chính trị/nhạy cảm bị Gemini từ chối")
                self.rr_engine.report_skip(acc_id)
                stats['failed'] += 1
                continue

            # ====== PRE-VALIDATION: Kiểm tra độ dài content ======
            if len(content) > self.OKX_MAX_CONTENT_LENGTH:
                print(f"  ⏭️ [{acc_id}] Skip {post_id}: content quá dài "
                      f"({len(content)} > {self.OKX_MAX_CONTENT_LENGTH})")
                self.db_manager.mark_post_posted(post_id, acc_id)
                self.rr_engine.report_skip(acc_id)
                stats['failed'] += 1
                continue

            # Lấy ảnh đầu tiên (nếu có)
            image_path = None
            if post_data.get('image_paths'):
                paths = [p.strip() for p in post_data['image_paths'].split('|') if p.strip()]
                for p in paths:
                    if os.path.exists(p):
                        image_path = p
                        break

            # NGHỈ TRƯỚC KHI ĐĂNG để giữ nhịp theo khung giờ; tính cả bài đầu
            # của batch mới (so với bài cuối của batch trước).
            wait_time = int(round(self.post_scheduler.next_delay() - (time.time() - last_attempt)))
            if wait_time > 0:
                print(f"  ⏳ [{acc_id}] Nghỉ {wait_time}s trước bài tiếp theo "
                      f"[{self.post_scheduler.describe()}]")
                for _ in range(wait_time):
                    if self._should_stop:
                        break
                    time.sleep(1)
                if self._should_stop:
                    self.db_manager.release_batch([p['id'] for p in posts[idx:]])
                    stats['total'] -= 1
                    break

            try:
                print(f"\n[{acc_id}] Đăng post {post_id}...")
                print(f"  Content ({len(content)} chars): {content[:80]}...")
                
                trending_hashtags = None
                if post_with_trending and post_data.get('hashtags'):
                    try:
                        import json
                        trending_hashtags = json.loads(post_data['hashtags'])
                    except Exception as e:
                        print(f"  ⚠️ Lỗi parse hashtags: {e}")

                pub_res = publisher.publish(content, image_path=image_path, trending_hashtags=trending_hashtags)

                if pub_res.get("code") == "0":
                    content_id = pub_res.get("data", {}).get("contentId", "N/A")
                    print(f"  ✅ THÀNH CÔNG [{acc_id}]: "
                          f"https://www.okx.com/square/post/{content_id}")
                    self.db_manager.mark_post_posted(post_id, acc_id)
                    self.rr_engine.report_success(acc_id)
                    stats['success'] += 1
                else:
                    error_code = pub_res.get("code", "?")
                    error_msg = pub_res.get("msg", str(pub_res))
                    print(f"  ❌ [{acc_id}] code={error_code}: {error_msg}")
                    
                    # Phân loại lỗi: nếu lỗi do xác thực/chữ ký/hết hạn phiên/lỗi hệ thống -> trả bài về hàng chờ
                    is_transient_error = error_code in ["50110", "50004", "50111", "50013"] or any(
                        term in error_msg.lower() for term in ["token", "signature", "auth", "login", "expired", "system error"]
                    )
                    
                    if is_transient_error:
                        print(f"  ⏭️ [{acc_id}] Trả {post_id} về trạng thái ready do lỗi tài khoản/hệ thống tạm thời")
                        self.db_manager.release_post(post_id)
                    else:
                        print(f"  ⏭️ [{acc_id}] Skip {post_id} vĩnh viễn do lỗi nội dung/xác thực bài viết")
                        self.db_manager.mark_post_posted(post_id, acc_id)
                        
                    self.rr_engine.report_failure(acc_id)
                    stats['failed'] += 1

            except Exception as e:
                print(f"  ❌ EXCEPTION [{acc_id}]: {str(e)}")
                print(f"  ⏭️ [{acc_id}] Trả {post_id} về trạng thái ready do exception kết nối")
                self.db_manager.release_post(post_id)
                self.rr_engine.report_failure(acc_id)
                stats['failed'] += 1

            last_attempt = time.time()
            self._last_post_attempt[acc_id] = last_attempt

        return stats

    def _do_post_batch(self) -> Dict:
        """
        Đăng bài ROUND-ROBIN FAIR DISTRIBUTION trên POOL HỢP NHẤT:
        Toàn bộ các tài khoản active cùng lấy bài từ tất cả các nguồn (binance, okx, sosovalue, KuCoin, okx_trending).
        """
        overall_stats = {'total': 0, 'success': 0, 'failed': 0}

        active_accounts_data = self.accounts
        if not active_accounts_data:
            return overall_stats

        all_acc_ids = [acc['id'] for acc in active_accounts_data]
        all_sources = self._get_all_sources()

        # Mỗi vòng lấy 1 bài/account để không khoá hàng loạt bài ở trạng thái 'processing' suốt cả giờ.
        per_account_claim = 1
        max_claim = per_account_claim * len(all_acc_ids)
        print(f"  🕒 [{self.post_scheduler.describe()}] → claim {per_account_claim} bài/account/round (Unified Pool)")

        batch = self.db_manager.claim_batch_for_distribution(max_claim, all_sources)
        if not batch:
            return overall_stats

        print(f"\n  📦 [Unified Pool] Claimed {len(batch)} bài cho {len(all_acc_ids)} accounts")
        queues = self.rr_engine.distribute(batch, allowed_accounts=all_acc_ids)

        # Release unassigned
        assigned_ids = set()
        for q in queues.values():
            for p in q:
                assigned_ids.add(p['id'])
        unassigned = [p for p in batch if p['id'] not in assigned_ids]
        if unassigned:
            self.db_manager.release_batch([p['id'] for p in unassigned])
            print(f"  ↩️ [Unified Pool] Released {len(unassigned)} bài thừa về DB")

        active_queues = {acc_id: posts for acc_id, posts in queues.items() if posts}
        if not active_queues:
            return overall_stats

        # Dispatch workers song song — tất cả accounts đăng queue của mình cùng lúc
        with ThreadPoolExecutor(max_workers=len(active_queues)) as executor:
            futures = {
                executor.submit(self._account_poster_with_queue, acc_id, posts): acc_id
                for acc_id, posts in active_queues.items()
            }
            for future in as_completed(futures):
                acc_id = futures[future]
                try:
                    res = future.result()
                    overall_stats['total'] += res['total']
                    overall_stats['success'] += res['success']
                    overall_stats['failed'] += res['failed']
                except Exception as e:
                    print(f"  ✗ Worker [{acc_id}] error: {e}")

        # In tổng kết Round-Robin
        rr_stats = self.rr_engine.get_stats()
        print(f"\n  📊 RR Summary: round={rr_stats['round_num']}, "
              f"global_total={rr_stats['global_total']}")
        distribution = []
        for acc_id, acc_stats in rr_stats['per_account'].items():
            distribution.append(f"{acc_id}={acc_stats['total_posted']}")
        print(f"  📊 Distribution: {', '.join(distribution)}")

        return overall_stats

    # ==============================================================
    # MODE 1: Auto liên tục với Background Crawler (RECOMMENDED)
    # ==============================================================

    def run_continuous_with_background(self):
        """
        Flow:
          1. Initial Crawl + Gemini Processing
          2. Start Background Crawler (tự động crawl+process mỗi N phút)
          3. Posting liên tục không dừng
        """
        print(f"\n{'='*80}")
        print("STARTING CONTINUOUS SYSTEM WITH BACKGROUND CRAWLER")
        print(f"{'='*80}")
        print(f"Background crawl interval: {self.reload_interval}s ({self.reload_interval/60:.1f} phút)")
        print(f"Posting: Liên tục không dừng")
        print(f"{'='*80}")

        try:
            # ===== STARTUP CLEANUP =====
            print("\n→ Running startup database cleanup...")
            startup_deleted = self.db_manager.cleanup_old_posts(keep_rows=10000)
            if startup_deleted > 0:
                print(f"  ✓ Cleanup: Xóa {startup_deleted} posts cũ")
            else:
                print(f"  ✓ Database size OK")

            # ===== INITIAL CRAWL =====
            print(f"\n{'▶'*40}")
            print("STEP 1: INITIAL CRAWL")
            print(f"{'▶'*40}")
            new_posts = self._do_crawl(mode="initial")
            self.stats['total_crawled'] += new_posts
            print(f"\n✓ Crawled {new_posts} posts")

            # ===== GEMINI PROCESSING =====
            print(f"\n{'▶'*40}")
            print("STEP 2: GEMINI PROCESSING")
            print(f"{'▶'*40}")
            process_stats = self.processor.process_batch(parallel=True)
            self.stats['total_processed'] += process_stats['success']
            print(f"\n✓ Processed {process_stats['success']} posts")

            # ===== START BACKGROUND CRAWLER =====
            print(f"\n{'='*80}")
            print("INITIALIZING BACKGROUND CRAWLER")
            print(f"{'='*80}")
            self.background_crawler = BackgroundCrawler(
                self, interval_seconds=self.reload_interval
            )
            self.background_crawler.start()
            print(f"  → Background crawl đầu tiên sau {self.reload_interval/60:.1f} phút")
            print(f"  → Posting sẽ chạy liên tục KHÔNG BỊ GIÁN ĐOẠN\n")

            # ===== START FARMING WORKERS =====
            self._start_farming_workers()



            # ===== CONTINUOUS POSTING =====
            print(f"{'='*80}")
            print("STARTING CONTINUOUS POSTING (ROUND-ROBIN)")
            print(f"{'='*80}\n")
            sys.stdout.flush()

            post_cycle = 1
            while not self._should_stop:
                print(f"\n{'='*80}")
                print(f"POSTING CYCLE #{post_cycle}")
                print(f"{'='*80}")
                sys.stdout.flush()

                # Health check mỗi 10 posting cycles
                if self.background_crawler and post_cycle % 10 == 0:
                    self._print_background_health()

                # Farming health check mỗi 10 cycles
                if post_cycle % 10 == 0 and self._farming_threads:
                    self._restart_dead_farming_threads()

                # Check ready buffer and trigger active crawl + Gemini processing if below 20%
                self._check_and_trigger_active_crawl_processing()

                post_stats = self._do_post_batch()
                
                # Báo cáo số bài đăng thành công trong cycle
                if post_stats['total'] > 0:
                    print(f"\n✓ THỐNG KÊ CYCLE #{post_cycle}: Đăng thành công {post_stats['success']}/{post_stats['total']} bài")
                
                self.stats['total_posted'] += post_stats['success']
                sys.stdout.flush()

                post_cycle += 1

                # GC mỗi 3 cycles
                if post_cycle % 3 == 0:
                    gc.collect()

                # DB cleanup mỗi 5 cycles
                if post_cycle % 5 == 0:
                    deleted = self.db_manager.cleanup_old_posts(keep_rows=5000)
                    if deleted > 0:
                        print(f"🗑️ Cleaned up {deleted} old posts")

                # Nếu không có bài → đợi 60s (cho background crawler thêm thời gian)
                if post_stats['total'] == 0:
                    ready_count = self.db_manager.count_ready_posts()
                    print(f"\n○ Không có bài sẵn sàng (DB ready={ready_count}), đợi 60s...")
                    sys.stdout.flush()
                    for _ in range(60):
                        if self._should_stop:
                            break
                        time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted by user (Ctrl+C)")
        except Exception as e:
            print(f"\n✗ Critical error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._graceful_shutdown()

    # ==============================================================
    # MODE 2: Auto liên tục tuần tự (Sequential)
    # ==============================================================

    def run_continuous(self, max_cycles: Optional[int] = None):
        """
        Flow tuần tự: Crawl → Process → Post → Wait → Repeat
        """
        print(f"\n{'='*80}")
        print("STARTING CONTINUOUS AUTO SYSTEM (SEQUENTIAL)")
        print(f"{'='*80}")
        print(f"Reload interval: {self.reload_interval}s ({self.reload_interval/60:.1f} phút)")
        if max_cycles:
            print(f"Max cycles: {max_cycles}")
        else:
            print("Max cycles: Unlimited (Ctrl+C to stop)")
        print(f"{'='*80}")

        try:
            # Chu kỳ 1 — Initial
            print(f"\n{'='*80}")
            print("CYCLE 1 — INITIAL")
            print(f"{'='*80}")
            success = self.run_initial_cycle()
            if not success:
                print("\n✗ Initial cycle failed!")
                return

            cycle_num = 2
            while not self._should_stop:
                if max_cycles and cycle_num > max_cycles:
                    print(f"\n✓ Đã đạt max cycles ({max_cycles})")
                    break

                # Countdown
                print(f"\n{'='*80}")
                print(f"⏳ WAITING {self.reload_interval}s BEFORE NEXT CYCLE...")
                print(f"{'='*80}")
                self._countdown(self.reload_interval)

                if self._should_stop:
                    break

                # Reload cycle
                print(f"\n{'='*80}")
                print(f"CYCLE {cycle_num} — RELOAD")
                print(f"{'='*80}")
                self.run_reload_cycle()
                cycle_num += 1

        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted by user (Ctrl+C)")
        except Exception as e:
            print(f"\n✗ Critical error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._print_final_summary()

    # ==============================================================
    # MODE 3: Chạy 1 chu kỳ đầy đủ (Initial Cycle)
    # ==============================================================

    def run_initial_cycle(self) -> bool:
        """Crawl đầy đủ → Gemini → Post"""
        print(f"\n{'='*80}")
        print("INITIAL CYCLE — FULL CRAWL")
        print(f"{'='*80}")

        try:
            # Step 1: Crawl
            print(f"\n{'▶'*40}")
            print("STEP 1: INITIAL CRAWL")
            print(f"{'▶'*40}")
            new_posts = self._do_crawl(mode="initial")
            self.stats['total_crawled'] += new_posts
            print(f"\n✓ Crawled {new_posts} posts")

            # Step 2: Gemini
            print(f"\n{'▶'*40}")
            print("STEP 2: GEMINI PROCESSING")
            print(f"{'▶'*40}")
            process_stats = self.processor.process_batch(parallel=True)
            self.stats['total_processed'] += process_stats['success']
            print(f"\n✓ Processed {process_stats['success']} posts")

            # Step 3: Post
            print(f"\n{'▶'*40}")
            print("STEP 3: POSTING TO OKX SQUARE")
            print(f"{'▶'*40}")
            post_stats = self._do_post_batch()
            self.stats['total_posted'] += post_stats['success']
            print(f"\n✓ Published {post_stats['success']} posts")

            self.stats['cycles'] += 1
            self._print_cycle_summary()
            return True

        except Exception as e:
            print(f"\n✗ Error in initial cycle: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ==============================================================
    # MODE 4: Chạy 1 chu kỳ reload
    # ==============================================================

    def run_reload_cycle(self) -> bool:
        """Reload crawl (chỉ bài mới) → Gemini → Post nếu có"""
        print(f"\n{'='*80}")
        print(f"RELOAD CYCLE #{self.stats['cycles'] + 1}")
        print(f"{'='*80}")

        try:
            # Step 1: Reload Crawl
            print(f"\n{'▶'*40}")
            print("STEP 1: RELOAD CRAWL")
            print(f"{'▶'*40}")
            new_posts = self._do_crawl(mode="reload")
            self.stats['total_crawled'] += new_posts
            print(f"\n✓ Found {new_posts} new posts")

            # Step 2: Gemini (luôn chạy — xử lý cả retry)
            print(f"\n{'▶'*40}")
            print("STEP 2: GEMINI PROCESSING (new + retry)")
            print(f"{'▶'*40}")
            process_stats = self.processor.process_batch(parallel=True)
            self.stats['total_processed'] += process_stats['success']

            # Step 3: Post (nếu có bài sẵn sàng)
            ready_count = self.db_manager.count_ready_posts()
            if ready_count > 0:
                print(f"\n{'▶'*40}")
                print(f"STEP 3: POSTING TO OKX SQUARE ({ready_count} ready)")
                print(f"{'▶'*40}")
                post_stats = self._do_post_batch()
                self.stats['total_posted'] += post_stats['success']
                print(f"\n✓ Published {post_stats['success']} posts")
            else:
                print("\n○ Không có bài sẵn sàng đăng")

            self.stats['cycles'] += 1
            self._print_cycle_summary()
            return True

        except Exception as e:
            print(f"\n✗ Error in reload cycle: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ==============================================================
    # MODE 5-7: Single Step (cho test/debug)
    # ==============================================================

    def run_single_step(self, step: str):
        """Chạy từng bước riêng lẻ"""
        if step == 'crawl':
            new = self._do_crawl(mode="initial")
            print(f"\n✓ Crawled {new} posts")
        elif step == 'process':
            stats = self.processor.process_batch(parallel=True)
            print(f"\n✓ Processed {stats['success']}/{stats['total']} posts")
        elif step == 'post':
            stats = self._do_post_batch()
            print(f"\n✓ Posted {stats['success']}/{stats['total']} posts")
        else:
            print(f"✗ Unknown step: {step}")

    # ==============================================================
    # HELPER METHODS
    # ==============================================================

    def _countdown(self, seconds: int):
        """Đếm ngược thời gian chờ"""
        for i in range(seconds, 0, -1):
            if self._should_stop:
                break
            if i in [60, 30, 10, 5, 4, 3, 2, 1] or i == seconds:
                mins, secs = divmod(i, 60)
                if mins > 0:
                    print(f"  ⏱ {mins}m {secs}s remaining...", end='\r')
                else:
                    print(f"  ⏱ {secs}s remaining...     ", end='\r')
                sys.stdout.flush()
            time.sleep(1)
        print("\n")

    def _print_background_health(self):
        """In health status ngắn gọn của background crawler"""
        if not self.background_crawler:
            return
        try:
            stats = self.background_crawler.get_stats()
            is_healthy = self.background_crawler.is_healthy()
            icon = "🟢" if is_healthy else "🔴"
            print(f"\n{'-'*80}")
            print(f"{icon} BACKGROUND CRAWLER STATUS")
            print(f"{'-'*80}")
            print(f"Running: {'Yes' if stats['is_running'] else 'No'} | "
                  f"Cycles: {stats['cycles']} | "
                  f"Crawled: {stats['total_crawled']} | "
                  f"Processed: {stats['total_processed']} | "
                  f"Errors: {stats['errors']}")
            if stats['last_heartbeat']:
                secs_ago = (datetime.now() - stats['last_heartbeat']).total_seconds()
                print(f"Last Heartbeat: {secs_ago:.0f}s ago")
            print(f"{'-'*80}\n")
            sys.stdout.flush()
        except Exception as e:
            print(f"⚠ Error checking background crawler: {e}")

    def _print_cycle_summary(self):
        """In tóm tắt chu kỳ"""
        print(f"\n{'-'*80}")
        print(f"CYCLE #{self.stats['cycles']} SUMMARY")
        print(f"{'-'*80}")
        db_stats = self.db_manager.get_stats()
        print(f"Database: {db_stats['total_posts']} total posts")
        print(f"  Theo status:")
        for status, count in db_stats.get('by_status', {}).items():
            print(f"    {status}: {count}")
        print(f"{'-'*80}")

    def _print_final_summary(self):
        """In tổng kết cuối cùng"""
        elapsed = datetime.now() - self.stats['start_time']
        print(f"\n{'='*80}")
        print("FINAL SUMMARY")
        print(f"{'='*80}")
        print(f"Total cycles: {self.stats['cycles']}")
        print(f"Total runtime: {elapsed}")
        print(f"Total crawled: {self.stats['total_crawled']}")
        print(f"Total processed: {self.stats['total_processed']}")
        print(f"Total posted: {self.stats['total_posted']}")
        print(f"\nFinal database state:")
        self.db_manager.print_stats()
        print(f"\n✓ System stopped gracefully")
        print(f"{'='*80}")

    def _graceful_shutdown(self):
        """Shutdown sạch sẽ mọi component"""
        print(f"\n{'='*60}")
        print("GRACEFUL SHUTDOWN")
        print(f"{'='*60}")

        # Stop farming workers
        self._stop_farming_workers()



        if self.background_crawler:
            self.background_crawler.stop()

        # Release stuck 'processing' posts back to 'ready'
        self.db_manager.cleanup_processing_timeout()

        # Final DB cleanup
        print("→ Final database cleanup...")
        final_deleted = self.db_manager.cleanup_old_posts(keep_rows=10000)
        if final_deleted > 0:
            print(f"  ✓ Cleaned up {final_deleted} posts")

        gc.collect()
        print("→ Final garbage collection done")

        self._print_final_summary()

    def shutdown(self):
        """Dừng toàn bộ hệ thống"""
        self._should_stop = True
        self._graceful_shutdown()

    # ==============================================================
    # FARMING — Organic Engagement Engine
    # ==============================================================

    def _init_farming_gemini(self):
        """
        Tạo GeminiProcessor riêng cho farming.
        Provider được chỉ định bởi farming.ai_provider trong settings.yaml.
        Hỗ trợ: 'gemma', 'gemini_proxy', 'gemini_direct'
        """
        farming_cfg = self.config.get('farming', {})
        prompt_file = farming_cfg.get('comment_prompt_file', 'config/farming_comment_prompt.txt')
        ai_provider = farming_cfg.get('ai_provider', 'gemma').lower()

        valid_providers = ['gemma', 'gemini_proxy', 'gemini_direct']
        if ai_provider not in valid_providers:
            print(f"  ⚠ farming.ai_provider='{ai_provider}' không hợp lệ, fallback về 'gemma'")
            ai_provider = 'gemma'

        print(f"→ Initializing Farming AI (provider: {ai_provider})...")
        processor = GeminiProcessor(config_path='config/gemini.yaml')

        # Override active provider theo farming config
        target_config = processor.config.get(ai_provider, {})
        if not target_config.get('enabled', False):
            print(f"  ⚠ Provider '{ai_provider}' chưa enabled trong gemini.yaml! Fallback về gemma")
            ai_provider = 'gemma'
            target_config = processor.config.get('gemma', {})

        processor.active_provider = ai_provider
        processor.provider_config = target_config
        processor.key_manager = None
        processor.tracker = None

        # Init theo provider type
        if ai_provider == 'gemma':
            processor._init_gemma()
            processor.system_prompt = None  # Gemma không hỗ trợ system prompt
        elif ai_provider == 'gemini_proxy':
            processor._init_gemini_proxy()
        elif ai_provider == 'gemini_direct':
            processor._init_gemini_direct()

        # Load farming comment prompt
        if os.path.exists(prompt_file):
            with open(prompt_file, 'r', encoding='utf-8') as f:
                processor.prompt_template = f.read()
            print(f"  ✓ Loaded farming prompt from {prompt_file}")
        else:
            print(f"  ⚠ Farming prompt not found: {prompt_file}")

        self._farming_gemini = processor

        # Log chi tiết provider
        if ai_provider == 'gemma':
            model_name = target_config.get('models', [{}])[0].get('name', '?')
            print(f"  ✓ Farming AI ready (gemma, model={model_name})")
        elif ai_provider == 'gemini_proxy':
            model_name = target_config.get('model', '?')
            endpoint = target_config.get('endpoint', '?')
            max_workers = target_config.get('processing', {}).get('max_workers', '?')
            print(f"  ✓ Farming AI ready (proxy → {endpoint}, model={model_name}, workers={max_workers})")
        elif ai_provider == 'gemini_direct':
            model_name = target_config.get('model', '?')
            num_keys = len([k for k in target_config.get('api_keys', []) if k and 'YOUR_' not in k])
            print(f"  ✓ Farming AI ready (direct, model={model_name}, keys={num_keys})")

    def _start_farming_workers(self):
        """
        Khởi tạo và start farming worker threads.
        Mỗi account trong farming.accounts → 1 thread riêng biệt.
        """
        farming_cfg = self.config.get('farming', {})
        if not farming_cfg.get('enabled', False):
            print("\n⏭ Farming disabled trong config")
            return

        farm_accounts = farming_cfg.get('accounts', [])
        if not farm_accounts:
            print("\n⏭ Không có farming accounts")
            return

        print(f"\n{'='*80}")
        print("🌱 INITIALIZING FARMING ENGINE")
        print(f"{'='*80}")
        print(f"  Accounts: {farm_accounts}")

        # Init shared Gemini processor for farming
        if not self._farming_gemini:
            self._init_farming_gemini()

        self._farming_stop_event.clear()

        for acc_num in farm_accounts:
            acc_id = f"account{acc_num}"
            acc_data = parse_cookie_file(acc_id)
            if not acc_data:
                print(f"  ⚠ Skip farming account {acc_id} (cookie not found)")
                continue

            proxy = self._get_proxy_for_account(acc_id)

            farmer = OKXFarmer(
                account_data=acc_data,
                farming_config=farming_cfg,
                gemini_processor=self._farming_gemini,
                proxy=proxy if proxy else None,
                stop_event=self._farming_stop_event,
            )
            self._farming_workers.append(farmer)

            t = threading.Thread(
                target=farmer.run_forever,
                name=f"Farmer-{acc_id}",
                daemon=True
            )
            t.start()
            self._farming_threads.append(t)
            print(f"  ✓ Farming worker started: {acc_id}")

        print(f"\n🌱 {len(self._farming_threads)} farming worker(s) running")
        print(f"{'='*80}\n")
        sys.stdout.flush()

    def _stop_farming_workers(self):
        """Dừng tất cả farming workers"""
        if not self._farming_threads:
            return

        print("\n→ Stopping farming workers...")
        self._farming_stop_event.set()

        for t in self._farming_threads:
            if t.is_alive():
                t.join(timeout=10)

        # Print final farming stats
        for farmer in self._farming_workers:
            stats = farmer.get_stats()
            print(f"  [{farmer.account_id}] Final: "
                  f"views={stats['total_views']}, likes={stats['total_likes']}, "
                  f"comments={stats['total_comments']}, follows={stats['total_follows']}")

        self._farming_workers.clear()
        self._farming_threads.clear()
        print("✓ All farming workers stopped")

    def _restart_dead_farming_threads(self):
        """
        Kiểm tra và tự động restart farming threads đã chết.
        Tạo farmer mới với cookies mới từ disk, start thread mới.
        """
        farming_cfg = self.config.get('farming', {})
        restarted = 0

        for i in range(len(self._farming_threads) - 1, -1, -1):
            t = self._farming_threads[i]
            if t.is_alive():
                continue

            dead_farmer = self._farming_workers[i]
            acc_id = dead_farmer.account_id
            print(f"\n⚠ Farming thread [{acc_id}] died — restarting...")

            # Xoá thread/worker cũ
            self._farming_threads.pop(i)
            self._farming_workers.pop(i)

            # Tạo farmer mới với cookies mới từ disk
            acc_data = parse_cookie_file(acc_id)
            if not acc_data:
                print(f"  ✗ Cannot restart [{acc_id}]: cookie file not found")
                continue

            proxy = self._get_proxy_for_account(acc_id)
            new_farmer = OKXFarmer(
                account_data=acc_data,
                farming_config=farming_cfg,
                gemini_processor=self._farming_gemini,
                proxy=proxy if proxy else None,
                stop_event=self._farming_stop_event,
            )
            self._farming_workers.append(new_farmer)

            new_thread = threading.Thread(
                target=new_farmer.run_forever,
                name=f"Farmer-{acc_id}",
                daemon=True
            )
            new_thread.start()
            self._farming_threads.append(new_thread)
            restarted += 1
            print(f"  ✓ [{acc_id}] Restarted successfully")

        if restarted > 0:
            print(f"🔄 Restarted {restarted} farming thread(s)")
            sys.stdout.flush()

    def run_farming_standalone(self):
        """
        Mode 9: Chạy farming độc lập (không crawl, không post).
        Chỉ farming thuần tuý. Tự động restart thread chết.
        """
        print(f"\n{'='*80}")
        print("🌱 STANDALONE FARMING MODE")
        print(f"{'='*80}")

        try:
            self._start_farming_workers()

            if not self._farming_threads:
                print("\n✗ Không có farming worker nào được khởi tạo!")
                return

            # Main thread chờ — farmer tự in stats mỗi session
            while not self._should_stop:
                # Check thread health mỗi 5 phút
                for _ in range(300):
                    if self._should_stop:
                        break
                    time.sleep(1)

                if self._should_stop:
                    break

                # Auto-restart dead farming threads
                self._restart_dead_farming_threads()

        except KeyboardInterrupt:
            print("\n\n⚠ Interrupted by user (Ctrl+C)")
        except Exception as e:
            print(f"\n✗ Farming error: {e}")
            traceback.print_exc()
        finally:
            self._stop_farming_workers()


# ============================================================
# MAIN ENTRY POINT — Interactive Menu
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description='OKX Square Auto System')
    parser.add_argument('--mode', type=str, help='Mode number (1-8) to run directly')
    args = parser.parse_args()

    if args.mode:
        choice = args.mode.strip()
        print(f"\n🚀 Running mode {choice} (from command line)")
    else:
        print(f"\n{'='*80}")
        print("OKX SQUARE AUTO SYSTEM")
        print(f"{'='*80}")
        print("\nChọn chế độ hoạt động:\n")

        print("1. 🚀 Tự động liên tục + Background Crawler (RECOMMENDED)")
        print("   → Crawl đầy đủ → Xử lý Gemini")
        print("   → Bật Background Crawler (crawl mỗi N phút ở nền)")
        print("   → Đăng bài liên tục KHÔNG BỊ GIÁN ĐOẠN\n")

        print("2. 🔄 Tự động liên tục (Sequential)")
        print("   → Crawl → Gemini → Post → Đợi → Repeat")
        print("   → Chạy vô hạn cho đến khi Ctrl+C\n")

        print("3. 🚀 Chu kỳ đầu tiên (Initial Cycle)")
        print("   → Crawl toàn bộ → Gemini → Post\n")

        print("4. 🔃 Chu kỳ reload (Reload Cycle)")
        print("   → Chỉ check bài mới → Gemini → Post\n")

        print("5. 🕷️  Chỉ crawl (Crawl Only)")
        print("   → Chỉ thu thập bài viết, không xử lý\n")

        print("6. 🤖 Chỉ xử lý Gemini (Process Only)")
        print("   → Xử lý các bài đã crawl với AI\n")

        print("7. 📤 Chỉ đăng bài (Post Only)")
        print("   → Đăng các bài đã xử lý lên OKX Square\n")

        print("8. 📊 Xem thống kê (Statistics)")
        print("   → Hiển thị thống kê database\n")

        print("9. 🌱 Farming (Account Engagement)")
        print("   → Nuôi tài khoản: View, Like, AI Comment, Follow")
        print("   → 24/7 liên tục với nhịp sinh học\n")

        print("0. ❌ Thoát\n")
        print(f"{'='*80}")

        choice = input("\nNhập số để chọn (0-9): ").strip()

    if choice == '0':
        print("\n👋 Đã thoát")
        return

    if choice not in ['1', '2', '3', '4', '5', '6', '7', '8', '9']:
        print("\n✗ Lựa chọn không hợp lệ!")
        return

    # Khởi tạo orchestrator
    try:
        orchestrator = MainOrchestrator('config/settings.yaml')
    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return

    # Khởi chạy bộ kiểm tra Cookie Expiry Checker ngầm
    try:
        cookie_checker = CookieExpiryCheckerOKX('config/settings.yaml')
        cookie_checker.start_background_checker()
    except Exception as e:
        print(f"\n⚠ Lỗi khởi chạy Cookie Expiry Checker: {e}")

    # Thực hiện theo lựa chọn
    if choice == '1':
        orchestrator.run_continuous_with_background()

    elif choice == '2':
        max_input = input("\nSố chu kỳ tối đa (Enter = vô hạn): ").strip()
        max_cycles = int(max_input) if max_input else None
        orchestrator.run_continuous(max_cycles)

    elif choice == '3':
        orchestrator.run_initial_cycle()

    elif choice == '4':
        orchestrator.run_reload_cycle()

    elif choice == '5':
        orchestrator.run_single_step('crawl')

    elif choice == '6':
        orchestrator.run_single_step('process')

    elif choice == '7':
        orchestrator.run_single_step('post')

    elif choice == '8':
        orchestrator.db_manager.print_stats()

    elif choice == '9':
        orchestrator.run_farming_standalone()


if __name__ == "__main__":
    main()

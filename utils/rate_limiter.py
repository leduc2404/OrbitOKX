"""
Sliding Window Rate Limiter & Load Balancer
============================================
Quản lý API Key cho Google Gemini/Gemma API với thuật toán Sliding Window.

Kiến trúc:
- SlidingWindowRateLimiter: Quản lý state của 1 Key duy nhất (deque, lock, penalty)
- ParallelKeyManager: Load Balancer phân phối đều giữa các keys

Sliding Window Algorithm + Anti-Burst:
- Mỗi key duy trì deque(maxlen=RPM) lưu timestamp các request gần nhất
- min_interval = 60/RPM giây giữa 2 request liên tiếp (chống burst)
- Khi acquire_slot(): 
  + Nếu last_request quá gần (< min_interval) → wait min_interval
  + deque chưa đầy → cho phép gọi ngay, push timestamp
  + deque đầy → tính wait = 60 - (now - oldest_timestamp)
    * wait > 0 → caller phải sleep đúng khoảng thời gian còn thiếu
    * wait <= 0 → oldest slot đã "nhả", cho phép gọi, push timestamp mới

Failure Tracking (từ gemini-balance):
- Mỗi key theo dõi failure_count cho hard failures (401/403/invalid key)
- Khi failure_count >= MAX_FAILURES → key bị auto-disable, skip trong load balancing
- ParallelKeyManager tự động recovery check: test key disabled định kỳ

Thread-Safety: Mọi đọc/ghi vào deque được bảo vệ bằng threading.Lock
"""

import time
import threading
from collections import deque
from typing import List, Tuple, Optional, Dict


# === Key Redaction Utility ===
def redact_key(key: str) -> str:
    """Ẩn API key cho logging an toàn. Chỉ hiện 6 ký tự đầu + 4 cuối."""
    if not key:
        return "(empty)"
    if len(key) <= 12:
        return f"{key[:3]}...{key[-3:]}"
    return f"{key[:6]}...{key[-4:]}"


class SlidingWindowRateLimiter:
    """
    Rate limiter cho 1 API Key duy nhất, dùng thuật toán Sliding Window.
    
    - deque(maxlen=rpm) lưu timestamp của N request gần nhất
    - Lock bảo vệ mọi đọc/ghi (thread-safe)
    - Hỗ trợ Penalty Cooldown khi bị 429 (key bị "cấm" tạm thời)
    - Hoàn toàn độc lập về state với các key khác
    
    Ví dụ với rpm=10:
        Deque: [t1, t2, t3, ..., t10]  (10 timestamps gần nhất)
        Khi request mới đến:
        - len(deque) < 10 → GỌI NGAY, push time.time()
        - len(deque) == 10:
            oldest = deque[0]
            wait = 60 - (now - oldest)
            if wait > 0: phải đợi 'wait' giây
            else: oldest đã quá 60s, slot đã nhả → gọi ngay
    """
    
    # Kích thước cửa sổ cố định: 60 giây (1 phút)
    WINDOW_SIZE = 60.0
    
    # Hard failure threshold — key bị disable sau N lần lỗi 401/403/invalid
    MAX_FAILURES = 5
    
    def __init__(self, api_key: str, key_index: int, rpm: int = 10, 
                 penalty_cooldown: float = 60.0):
        """
        Args:
            api_key: API key string
            key_index: Index trong danh sách keys (cho display)
            rpm: Requests Per Minute - số request tối đa trong 60s (mặc định 10)
            penalty_cooldown: Thời gian penalty khi bị 429 (giây, mặc định 60)
        """
        self.api_key = api_key
        self.key_index = key_index
        self.key_display = f"key_{key_index + 1}"
        self.rpm = rpm
        self.penalty_cooldown = penalty_cooldown
        
        # === Anti-Burst: Khoảng cách tối thiểu giữa 2 requests ===
        # Google enforce per-second burst limits ngoài RPM.
        # VD: RPM=10 → min_interval = 60/10 = 6 giây giữa 2 requests cùng key.
        # Nếu không có min_interval, 10 workers có thể fire 10 requests/giây
        # vào cùng 1 key → Google trả 429 dù per-minute count còn dư.
        self.min_interval: float = self.WINDOW_SIZE / rpm
        
        # === Sliding Window State ===
        # deque(maxlen=rpm): tự động popleft khi vượt maxlen
        self._timestamps: deque = deque(maxlen=rpm)
        
        # Lock bảo vệ mọi đọc/ghi vào deque và penalty_until
        self._lock = threading.Lock()
        
        # Penalty state: timestamp khi key hết bị phạt (0.0 = không bị phạt)
        self._penalty_until: float = 0.0
        
        # === Stats ===
        self.success_count: int = 0
        self.failed_count: int = 0
        self.penalty_count: int = 0
        
        # === Failure Tracking (hard failures: 401/403/invalid key) ===
        self.hard_failure_count: int = 0
        self._disabled: bool = False
        self._disabled_at: float = 0.0
        self.total_wait_time: float = 0.0
    
    def acquire_slot(self) -> float:
        """
        Xin 1 slot để gọi API. Thread-safe.
        
        Logic: Anti-Burst + Sliding Window
        1. Nếu đang bị penalty → return thời gian chờ penalty còn lại
        2. Anti-Burst: Nếu request cuối quá gần (< min_interval) → return wait
        3. Sliding Window:
           - deque chưa đầy → push timestamp, return 0.0
           - deque đầy → tính wait = 60 - (now - oldest)
        
        Returns:
            float: Số giây phải đợi trước khi được gọi API
                   0.0 = được gọi ngay lập tức
                   > 0 = phải sleep đúng bằng giá trị này
        """
        with self._lock:
            now = time.time()
            
            # --- Check penalty ---
            if self._penalty_until > now:
                return self._penalty_until - now
            
            # --- Anti-Burst: Khoảng cách tối thiểu giữa 2 requests ---
            # Ngăn burst pattern: 10 requests/giây rồi chờ 60 giây
            if self._timestamps:
                last_request = self._timestamps[-1]  # Timestamp gần nhất
                elapsed_since_last = now - last_request
                if elapsed_since_last < self.min_interval:
                    # Quá gần → phải đợi
                    return self.min_interval - elapsed_since_last
            
            # --- Purge expired timestamps ---
            # Loại bỏ các timestamp đã quá 60s (ngoài cửa sổ)
            window_start = now - self.WINDOW_SIZE
            while self._timestamps and self._timestamps[0] < window_start:
                self._timestamps.popleft()
            
            # --- Sliding Window Logic ---
            if len(self._timestamps) < self.rpm:
                # Chưa đầy → GỌI NGAY
                self._timestamps.append(now)
                return 0.0
            else:
                # Đã đầy → tính thời gian chờ dựa trên oldest timestamp
                oldest = self._timestamps[0]
                wait = self.WINDOW_SIZE - (now - oldest)
                
                if wait > 0:
                    # Slot chưa nhả → caller phải đợi
                    return wait
                else:
                    # Slot đã nhả (oldest > 60s trước) → gọi ngay
                    self._timestamps.append(now)
                    return 0.0

    def record_request(self):
        """
        Ghi nhận 1 request đã thực sự được gửi đi.
        Dùng cho trường hợp caller đã sleep xong wait time và cần push timestamp.
        Thread-safe.
        """
        with self._lock:
            now = time.time()
            # Purge expired trước
            window_start = now - self.WINDOW_SIZE
            while self._timestamps and self._timestamps[0] < window_start:
                self._timestamps.popleft()
            self._timestamps.append(now)
    
    def apply_penalty(self, seconds: Optional[float] = None):
        """
        Đưa key vào trạng thái "Bị phạt" (Penalty Cooldown).
        Khi bị 429 hoặc Quota Error:
        1. Set penalty_until = now + cooldown
        2. Clear deque (reset sliding window)
        
        Args:
            seconds: Override penalty duration (None = dùng default)
        """
        cooldown = seconds if seconds is not None else self.penalty_cooldown
        with self._lock:
            now = time.time()
            self._penalty_until = now + cooldown
            self._timestamps.clear()
            self.penalty_count += 1
            print(f"  ⛔ {self.key_display} PENALTY {cooldown:.0f}s "
                  f"(total penalties: {self.penalty_count})")
    
    def is_penalized(self) -> bool:
        """Check key có đang bị penalty không. Thread-safe."""
        with self._lock:
            return self._penalty_until > time.time()
    
    def get_earliest_free_time(self) -> float:
        """
        Trả về timestamp sớm nhất key này có thể nhả 1 slot.
        Tính cả anti-burst min_interval.
        
        Returns:
            float: Unix timestamp khi key sẽ sẵn sàng
        """
        with self._lock:
            now = time.time()
            
            # Nếu đang penalty → return thời điểm hết penalty
            if self._penalty_until > now:
                return self._penalty_until
            
            # Anti-burst: phải đợi min_interval kể từ request cuối
            if self._timestamps:
                earliest_by_interval = self._timestamps[-1] + self.min_interval
                if earliest_by_interval > now:
                    # Purge expired
                    window_start = now - self.WINDOW_SIZE
                    while self._timestamps and self._timestamps[0] < window_start:
                        self._timestamps.popleft()
                    # Nếu vẫn đầy, phải đợi cả oldest nhả
                    if len(self._timestamps) >= self.rpm:
                        earliest_by_window = self._timestamps[0] + self.WINDOW_SIZE
                        return float(max(earliest_by_interval, earliest_by_window))
                    return float(earliest_by_interval)
            
            # Purge expired
            window_start = now - self.WINDOW_SIZE
            while self._timestamps and self._timestamps[0] < window_start:
                self._timestamps.popleft()
            
            # Chưa đầy → sẵn sàng ngay
            if len(self._timestamps) < self.rpm:
                return now
            
            # Đã đầy → slot nhả khi oldest + 60s
            return float(self._timestamps[0] + self.WINDOW_SIZE)
    
    def get_load(self) -> int:
        """
        Số request trong cửa sổ 60s hiện tại.
        Dùng cho Load Balancer scoring.
        
        Returns:
            int: 0 (rảnh nhất) đến rpm (đầy nhất)
        """
        with self._lock:
            now = time.time()
            window_start = now - self.WINDOW_SIZE
            # Đếm timestamps còn trong cửa sổ
            return sum(1 for t in self._timestamps if t >= window_start)
    
    def record_success(self):
        """Ghi nhận request thành công."""
        self.success_count += 1
    
    def record_failure(self):
        """Ghi nhận request thất bại (soft: 429/timeout)."""
        self.failed_count += 1
    
    def record_hard_failure(self):
        """
        Ghi nhận hard failure (401/403/invalid key).
        Khi vượt MAX_FAILURES → key bị auto-disable.
        """
        with self._lock:
            self.hard_failure_count += 1
            if self.hard_failure_count >= self.MAX_FAILURES and not self._disabled:
                self._disabled = True
                self._disabled_at = time.time()
                print(f"  🚫 {self.key_display} ({redact_key(self.api_key)}) AUTO-DISABLED "
                      f"after {self.hard_failure_count} hard failures")
    
    def is_disabled(self) -> bool:
        """Check key có bị disabled (hard failure quá nhiều) không."""
        return self._disabled
    
    def reset_failure_count(self):
        """Reset hard failure count và re-enable key (dùng khi recovery check thành công)."""
        with self._lock:
            old_count = self.hard_failure_count
            self.hard_failure_count = 0
            self._disabled = False
            self._disabled_at = 0.0
            if old_count > 0:
                print(f"  ✅ {self.key_display} ({redact_key(self.api_key)}) RE-ENABLED "
                      f"(was {old_count} failures)")
    
    def get_stats(self) -> Dict:
        """Lấy stats của key này."""
        with self._lock:
            now = time.time()
            is_penalized = self._penalty_until > now
            penalty_remaining = max(0, self._penalty_until - now) if is_penalized else 0
            current_load = sum(1 for t in self._timestamps 
                             if t >= now - self.WINDOW_SIZE)
        
        return {
            'key': self.key_display,
            'key_redacted': redact_key(self.api_key),
            'success': self.success_count,
            'failed': self.failed_count,
            'hard_failures': self.hard_failure_count,
            'is_disabled': self._disabled,
            'penalties': self.penalty_count,
            'total_wait': round(self.total_wait_time, 2),
            'current_load': current_load,
            'is_penalized': is_penalized,
            'penalty_remaining': round(penalty_remaining, 1),
        }
    
    def __repr__(self) -> str:
        return (f"SlidingWindowRateLimiter({self.key_display}, "
                f"rpm={self.rpm}, load={self.get_load()}/{self.rpm})")


class ParallelKeyManager:
    """
    Load Balancer phân phối đều requests giữa các API keys.
    
    Quản lý danh sách SlidingWindowRateLimiter, tự động:
    - Khởi tạo limiter cho mỗi key (dynamic, không cần sửa code)
    - Chọn key tối ưu nhất cho mỗi request (fair distribution)
    - Xử lý penalty khi key bị 429
    
    Tiêu chí chọn Key (get_best_available_key):
    ┌─────────────────────────────────────────────────────────────┐
    │ Ưu tiên 1: Key RẢNH ngay (load < rpm, không penalty)      │
    │   → Chọn key có load THẤP NHẤT                            │
    │                                                             │
    │ Ưu tiên 2: Tất cả đầy → chọn key nhả slot SỚM NHẤT       │
    │   → Dựa vào get_earliest_free_time()                       │
    │   → Return wait_time để caller sleep đúng số giây          │
    └─────────────────────────────────────────────────────────────┘
    """
    
    def __init__(self, api_keys: List[str], rpm: int = 10, 
                 penalty_cooldown: float = 60.0):
        """
        Args:
            api_keys: Danh sách API keys
            rpm: Requests Per Minute per key (mặc định 10)
            penalty_cooldown: Penalty duration khi bị 429 (giây)
        """
        if not api_keys:
            raise ValueError("api_keys cannot be empty")
        
        # Tạo SlidingWindowRateLimiter cho mỗi key
        self.limiters: List[SlidingWindowRateLimiter] = []
        for idx, key in enumerate(api_keys):
            limiter = SlidingWindowRateLimiter(
                api_key=key,
                key_index=idx,
                rpm=rpm,
                penalty_cooldown=penalty_cooldown
            )
            self.limiters.append(limiter)
        
        self.num_keys = len(self.limiters)
        self.rpm = rpm
        
        # Lock riêng cho assignment (tránh 2 thread chọn cùng key)
        self._assign_lock = threading.Lock()
        
        # Track disabled keys count
        self._disabled_count = 0
        
        print(f"[OK] ParallelKeyManager: {self.num_keys} keys, "
              f"rpm={rpm}/key, min_interval={60.0/rpm:.1f}s, penalty={penalty_cooldown}s")
        print(f"  → Theoretical max throughput: "
              f"{self.num_keys * rpm} requests/minute")
    
    def get_best_available_key(self) -> Tuple[SlidingWindowRateLimiter, float]:
        """
        Chọn key tối ưu nhất và trả về thời gian chờ.
        Thread-safe (dùng _assign_lock).
        
        Logic Load Balancing:
        1. Quét tất cả keys, tìm key RẢNH (load < rpm, không penalty)
           → Chọn key có load thấp nhất (fair distribution)
        2. Nếu KHÔNG có key rảnh → tìm key nhả slot SỚM NHẤT
           → Tính wait_time = earliest_free_time - now
        
        Returns:
            Tuple[SlidingWindowRateLimiter, float]:
                - limiter: Key được chọn
                - wait_seconds: Số giây phải đợi (0.0 = gọi ngay)
        """
        with self._assign_lock:
            now = time.time()
            
            # === Auto-recovery: Re-enable disabled keys after 30 minutes ===
            for limiter in self.limiters:
                if limiter.is_disabled() and limiter._disabled_at > 0 and (now - limiter._disabled_at > 1800):
                    print(f"  🔄 ParallelKeyManager: Re-enabling {limiter.key_display} for recovery test (disabled for >30m)")
                    limiter.reset_failure_count()
            
            # === Ưu tiên 1: Tìm key rảnh ngay lập tức (skip disabled + penalized) ===
            available_keys: List[Tuple[SlidingWindowRateLimiter, int]] = []
            
            for limiter in self.limiters:
                # Bỏ qua key bị disabled (hard failure)
                if limiter.is_disabled():
                    continue
                # Bỏ qua key đang penalty (429)
                if limiter.is_penalized():
                    continue
                
                load = limiter.get_load()
                if load < limiter.rpm:
                    available_keys.append((limiter, load))
            
            if available_keys:
                # Chọn key có load thấp nhất → phân phối đều
                best_limiter, _ = min(available_keys, key=lambda x: x[1])
                
                # Acquire slot ngay (đã biết chắc có slot)
                wait = best_limiter.acquire_slot()
                return best_limiter, wait
            
            # === Ưu tiên 2: Tất cả đầy → chọn key nhả slot sớm nhất (skip disabled) ===
            active_limiters = [l for l in self.limiters if not l.is_disabled()]
            if not active_limiters:
                # Tất cả key đều disabled → fallback về key đầu tiên
                print(f"  ⚠ ALL {self.num_keys} keys disabled! Falling back to first key.")
                active_limiters = self.limiters
            
            best_limiter = min(
                active_limiters,
                key=lambda l: l.get_earliest_free_time()
            )
            
            # Tính wait time
            free_time = best_limiter.get_earliest_free_time()
            wait = max(0.0, free_time - now)
            
            return best_limiter, wait
    
    def report_error_429(self, limiter: SlidingWindowRateLimiter, 
                         penalty_seconds: Optional[float] = None):
        """
        Báo cáo lỗi 429 / Quota Error cho 1 key (soft failure).
        Key vẫn valid, chỉ tạm penalty.
        """
        limiter.apply_penalty(penalty_seconds)
    
    def report_hard_failure(self, limiter: SlidingWindowRateLimiter):
        """
        Báo cáo hard failure (401/403/invalid key).
        Key có thể bị auto-disable nếu vượt MAX_FAILURES.
        """
        limiter.record_hard_failure()
        if limiter.is_disabled():
            self._disabled_count = sum(1 for l in self.limiters if l.is_disabled())
            active = self.num_keys - self._disabled_count
            print(f"  📊 Keys status: {active} active / {self._disabled_count} disabled / {self.num_keys} total")
    
    def get_all_stats(self) -> Dict:
        """Tổng hợp stats từ tất cả keys."""
        stats = {}
        total_success = 0
        total_failed = 0
        total_penalties = 0
        
        for limiter in self.limiters:
            s = limiter.get_stats()
            stats[s['key']] = s
            total_success += s['success']
            total_failed += s['failed']
            total_penalties += s['penalties']
        
        stats['_total'] = {
            'success': total_success,
            'failed': total_failed,
            'penalties': total_penalties,
            'num_keys': self.num_keys,
            'rpm_per_key': self.rpm,
        }
        return stats
    
    def reset_all_penalties(self):
        """Reset penalty cho tất cả keys (chỉ soft penalty, không reset hard failure)."""
        for limiter in self.limiters:
            with limiter._lock:
                limiter._penalty_until = 0.0
                limiter._timestamps.clear()
        print("[OK] All key penalties and sliding windows reset")
    
    def get_keys_status(self) -> Dict:
        """Lấy status tổng hợp: active/disabled/penalized."""
        disabled = sum(1 for l in self.limiters if l.is_disabled())
        penalized = sum(1 for l in self.limiters if l.is_penalized() and not l.is_disabled())
        active = self.num_keys - disabled - penalized
        return {
            'total': self.num_keys,
            'active': active,
            'penalized': penalized,
            'disabled': disabled,
            'disabled_keys': [
                {'key': l.key_display, 'key_redacted': redact_key(l.api_key), 
                 'failures': l.hard_failure_count}
                for l in self.limiters if l.is_disabled()
            ]
        }
    
    def print_status(self):
        """In trạng thái hiện tại của tất cả keys."""
        status_info = self.get_keys_status()
        print(f"\n{'='*60}")
        print(f"KEY STATUS ({self.num_keys} keys, {self.rpm} RPM/key) "
              f"| ✅{status_info['active']} ⏳{status_info['penalized']} 🚫{status_info['disabled']}")
        print(f"{'='*60}")
        for limiter in self.limiters:
            s = limiter.get_stats()
            if s['is_disabled']:
                status = "🚫 DISABLED"
                extra = f" (hard_failures={s['hard_failures']})"
            elif s['is_penalized']:
                status = "⛔ PENALTY"
                extra = f" ({s['penalty_remaining']}s left)"
            else:
                status = "✅ ACTIVE"
                extra = ""
            print(f"  {s['key']} ({s['key_redacted']}): {status}{extra} | "
                  f"load={s['current_load']}/{self.rpm} | "
                  f"✓{s['success']} ✗{s['failed']} | "
                  f"penalties={s['penalties']}")
        print(f"{'='*60}")

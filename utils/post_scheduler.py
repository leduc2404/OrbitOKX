"""
Hourly Posting Scheduler
========================
Điều phối nhịp đăng bài theo khung giờ thay vì interval phẳng.

Hệ số nhân (multiplier) áp lên interval giữa 2 bài:
  multiplier < 1  -> interval rút ngắn -> đăng dày hơn (khung giờ nhiều view)
  multiplier > 1  -> interval giãn ra   -> đăng thưa hơn (khung giờ tắc nghẽn)
  multiplier = 1  -> giữ nhịp chuẩn

Bảng hệ số mặc định suy ra từ thống kê median view/giờ của OKX Orbit
(UTC+7). Có thể override toàn bộ trong config/settings.yaml.

Bên trên hệ số theo giờ là CHẾ ĐỘ THEO NGÀY (daily_variation): mỗi ngày một cường độ
riêng và đường cong trong ngày cũng lệch nhẹ theo từng khung giờ, để nhiều ngày liên
tiếp không tạo ra cùng một mô hình lặp lại.
"""
import math
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# Khung giờ theo giờ Việt Nam (UTC+7) — 00:00 UTC đóng nến ngày = 07:00 UTC+7.
DEFAULT_HOURLY_MULTIPLIERS: Dict[int, float] = {
    0: 0.75,   # Phiên Mỹ sôi động
    1: 0.75,   # Sóng đêm
    2: 0.85,
    3: 0.80,
    4: 0.60,   # Đỉnh view toàn ngày
    5: 0.65,   # Giờ vàng
    6: 0.70,   # Trước giờ đóng nến ngày
    7: 1.00,   # Đóng nến ngày, nhịp chuẩn
    8: 0.85,
    9: 0.90,
    10: 1.00,
    11: 1.10,
    12: 1.10,
    13: 1.15,
    14: 1.20,
    15: 1.15,
    16: 1.10,
    17: 1.15,
    18: 1.20,
    19: 1.40,  # Bắt đầu tắc nghẽn
    20: 1.80,  # Giờ đỏ: cạnh tranh tăng vọt
    21: 2.20,  # Tắc nghẽn cực đại
    22: 0.85,  # Thoát nghẽn
    23: 0.80,  # Sóng đêm muộn
}

SECONDS_PER_HOUR = 3600.0


class HourlyPostingScheduler:
    """Sinh interval đăng bài theo khung giờ hiện tại. Thread-safe (state bất biến sau load)."""

    def __init__(self, settings: Optional[dict] = None):
        self._lock = threading.Lock()
        self._daily_cache: Dict[str, float] = {}
        self._load(settings or {})

    # ------------------------------------------------------------------
    # CONFIG
    # ------------------------------------------------------------------

    def _load(self, settings: dict):
        schedule_cfg = settings.get("posting_schedule") or {}
        delay_cfg = settings.get("delay_between_posts_seconds") or {}

        self.enabled = bool(schedule_cfg.get("enabled", True))
        self.base_min = float(delay_cfg.get("min", 400))
        self.base_max = float(delay_cfg.get("max", 1000))
        if self.base_max < self.base_min:
            self.base_min, self.base_max = self.base_max, self.base_min

        self.jitter = max(0.0, min(0.9, float(schedule_cfg.get("jitter", 0.15))))
        self.planning_horizon_hours = max(1, int(schedule_cfg.get("planning_horizon_hours", 3)))
        self.min_interval_seconds = float(schedule_cfg.get("min_interval_seconds", 60))

        offset = schedule_cfg.get("utc_offset_hours")
        self.utc_offset_hours = float(offset) if offset is not None else None
        self._tz = (
            timezone(timedelta(hours=self.utc_offset_hours))
            if self.utc_offset_hours is not None
            else None
        )

        self.multipliers = self._parse_multipliers(schedule_cfg.get("hourly_multipliers"))

        # ===== CHẾ ĐỘ THEO NGÀY =====
        # Mỗi ngày (theo múi giờ lịch) có 1 cường độ riêng + đường cong trong ngày
        # bị lệch nhẹ theo từng khung giờ. Random seed theo YYYY-MM-DD NÊN kết quả
        # ổn định trong ngày: restart / reload config giữa chừng không đổi nhịp.
        daily_cfg = schedule_cfg.get("daily_variation") or {}
        self.daily_enabled = bool(daily_cfg.get("enabled", True))
        self.daily_volume_min = max(0.2, float(daily_cfg.get("volume_min", 0.8)))
        self.daily_volume_max = max(self.daily_volume_min, float(daily_cfg.get("volume_max", 1.2)))
        self.daily_shape_noise = max(0.0, min(0.5, float(daily_cfg.get("hourly_shape_noise", 0.10))))
        self.daily_seed = str(daily_cfg.get("seed", "orbit-daily"))

    def _parse_multipliers(self, raw) -> Dict[int, float]:
        table = dict(DEFAULT_HOURLY_MULTIPLIERS)
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    hour = int(key)
                except (TypeError, ValueError):
                    continue
                if 0 <= hour <= 23:
                    table[hour] = max(0.05, float(value))
        return table

    def update_from_settings(self, settings: dict):
        """Nạp lại cấu hình (settings.yaml có thể được sửa lúc runtime)."""
        with self._lock:
            self._load(settings or {})

    # ------------------------------------------------------------------
    # TIME
    # ------------------------------------------------------------------

    def now(self) -> datetime:
        if self._tz is not None:
            return datetime.now(self._tz)
        return datetime.now().astimezone()

    def current_hour(self) -> int:
        return self.now().hour

    def multiplier_at(self, hour: int) -> float:
        return self.multipliers.get(hour % 24, 1.0)

    def current_multiplier(self) -> float:
        return self.multiplier_at(self.current_hour())

    # ------------------------------------------------------------------
    # DAILY REGIME (chế độ riêng cho từng ngày)
    # ------------------------------------------------------------------

    def day_key(self, dt: Optional[datetime] = None) -> str:
        return (dt or self.now()).strftime("%Y-%m-%d")

    def daily_intensity(self, day: Optional[str] = None) -> float:
        """
        Hệ số cường độ của cả ngày (1.0 = ngày chuẩn).
        0.8 = ngày chậm (interval dài hơn 25%), 1.2 = ngày vội (interval ngắn hơn).
        Deterministic theo YYYY-MM-DD + seed nên restart không đổi bài trong ngày.
        """
        if not self.daily_enabled:
            return 1.0
        key = day or self.day_key()
        cached = self._daily_cache.get(key)
        if cached is None:
            rng = random.Random(f"{self.daily_seed}|intensity|{key}")
            cached = rng.uniform(self.daily_volume_min, self.daily_volume_max)
            with self._lock:
                if len(self._daily_cache) > 64:
                    self._daily_cache.clear()
                self._daily_cache[key] = cached
        return cached

    def shape_wobble_at(self, hour: int, day: Optional[str] = None) -> float:
        """
        Dao động ±(daily_shape_noise) của riêng khung giờ đó trong ngày này,
        để biên độ giữa các giờ không lặp lại y hệt mỗi ngày.
        """
        if not self.daily_enabled or self.daily_shape_noise <= 0:
            return 1.0
        key = day or self.day_key()
        rng = random.Random(f"{self.daily_seed}|shape|{key}|{hour % 24}")
        return 1.0 + rng.uniform(-self.daily_shape_noise, self.daily_shape_noise)

    def effective_multiplier(self, hour: int, day: Optional[str] = None) -> float:
        """Hệ số thật của 1 khung giờ = hệ số lịch x nhiễu hình dáng / cường độ ngày."""
        base = self.multiplier_at(hour)
        if not self.enabled or not self.daily_enabled:
            return base
        return base * self.shape_wobble_at(hour, day) / self.daily_intensity(day)

    # ------------------------------------------------------------------
    # DELAY / CAPACITY
    # ------------------------------------------------------------------

    def base_delay(self) -> float:
        """Interval nền (chưa nhân hệ số khung giờ)."""
        return random.uniform(self.base_min, self.base_max)

    def next_delay(self, hour: Optional[int] = None) -> float:
        """Interval (giây) trước bài đăng kế tiếp: khung giờ + chế độ ngày + jitter."""
        delay = self.base_delay()
        if self.enabled:
            delay *= self.effective_multiplier(hour if hour is not None else self.current_hour())
            if self.jitter:
                delay *= 1.0 + random.uniform(-self.jitter, self.jitter)
        return max(self.min_interval_seconds, delay)

    def avg_delay_for_hour(self, hour: int) -> float:
        """Interval trung bình của 1 khung giờ (dùng để tính công suất buffer)."""
        base_avg = (self.base_min + self.base_max) / 2.0
        if not self.enabled:
            return base_avg
        return base_avg * self.effective_multiplier(hour)

    def hourly_capacity(self, hour: Optional[int] = None) -> float:
        """Số bài/giờ mỗi account đăng được trong khung giờ đã cho."""
        target_hour = hour if hour is not None else self.current_hour()
        return SECONDS_PER_HOUR / self.avg_delay_for_hour(target_hour)

    def planning_hourly_capacity(self) -> float:
        """
        Công suất dùng để định cỡ buffer crawl/Gemini.

        Lấy khung giờ đăng NHANH NHẤT trong planning_horizon_hours tới (kể cả giờ
        hiện tại) để buffer không bị hụt khi chuyển từ giờ nghẽn sang giờ vàng.
        """
        start_hour = self.current_hour()
        return max(
            self.hourly_capacity((start_hour + offset) % 24)
            for offset in range(self.planning_horizon_hours)
        )

    def compute_buffer_target(
        self,
        active_account_count: int = 1,
        settings: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """
        Tính toán kích thước buffer 'ready' tối ưu cho pool hợp nhất.
        - Dựa trên công suất đỉnh sắp tới (planning_hourly_capacity) x số account active x runway_hours.
        - Không cộng buffer_offset cứng gây dư thừa hay lãng phí Gemini token.
        """
        cfg = settings or {}
        pool_cfg = cfg.get("content_pool") or {}
        spider_cfg = cfg.get("spider") or {}

        runway_hours = max(
            0.5,
            float(pool_cfg.get("runway_hours", spider_cfg.get("runway_hours", 1.5))),
        )
        min_ready_buffer = max(
            1,
            int(pool_cfg.get("min_ready_buffer", spider_cfg.get("min_ready_buffer", 5))),
        )
        low_watermark_ratio = max(
            0.10,
            min(
                0.90,
                float(
                    pool_cfg.get(
                        "low_watermark_ratio",
                        spider_cfg.get("low_watermark_ratio", 0.35),
                    )
                ),
            ),
        )

        effective_accounts = max(1, int(active_account_count or 0))
        peak_per_account = self.planning_hourly_capacity()
        computed_target = max(
            min_ready_buffer,
            int(math.ceil(effective_accounts * peak_per_account * runway_hours)),
        )

        explicit_max = pool_cfg.get(
            "max_ready_buffer", spider_cfg.get("max_ready_buffer")
        )
        if explicit_max is not None:
            target_ready = max(1, int(explicit_max))
        else:
            target_ready = computed_target

        low_watermark = max(
            2, min(target_ready, int(math.ceil(target_ready * low_watermark_ratio)))
        )

        return {
            "target_ready": target_ready,
            "low_watermark": low_watermark,
            "peak_per_account": peak_per_account,
            "runway_hours": runway_hours,
            "min_ready_buffer": min_ready_buffer,
            "active_accounts": effective_accounts,
        }

    # ------------------------------------------------------------------
    # LOGGING
    # ------------------------------------------------------------------

    def describe(self) -> str:
        hour = self.current_hour()
        offset_label = (
            f"UTC{self.utc_offset_hours:+.0f}" if self.utc_offset_hours is not None else "local"
        )
        mode = "ON" if self.enabled else "OFF"
        day = self.day_key()
        intensity = self.daily_intensity(day)
        return (
            f"{day} {hour:02d}:00 {offset_label} | "
            f"multiplier={self.multiplier_at(hour):.2f} x day={intensity:.2f} x "
            f"shape={self.shape_wobble_at(hour, day):.2f} = {self.effective_multiplier(hour, day):.2f} ({mode}) | "
            f"interval≈{self.avg_delay_for_hour(hour):.0f}s | "
            f"capacity≈{self.hourly_capacity(hour):.2f} bài/giờ/account"
        )

    def __repr__(self) -> str:
        return f"HourlyPostingScheduler({self.describe()})"

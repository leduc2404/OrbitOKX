"""Cookie/Session Expiry Checker cho OKXSquare.

Nguồn hết hạn: giải mã JWT `exp` của cookie `token` (okx.config.token_expiry).
Fallback: mtime file + COOKIE_LIFETIME_DAYS nếu không có exp.
Scheduler heapq event-based (1h / 30min / expired / daily_report), chạy nền.
Alert đẩy qua services.telegram.alerts (KHÔNG hardcode bot token) + ghi DB.
"""
import heapq
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Set

import yaml

from infra.logging_config import get_logger
from okx.config import token_expiry

logger = get_logger(__name__)


class CookieExpiryChecker:
    COOKIE_LIFETIME_DAYS = 5
    NOTIFY_1_HOUR = 3600
    NOTIFY_30_MIN = 1800
    DAILY_REPORT_HOUR = 19

    def __init__(self, config_path: str = "config/profiles_config.yaml", db=None, alerts=None):
        self.config_path = config_path
        self.base_dir = Path(__file__).resolve().parents[1]
        self.db = db
        if alerts is None:
            from services.telegram.alerts import get_alerts
            alerts = get_alerts(db=db)
        self.alerts = alerts

        self._cached_expiry: Dict[int, Dict] = {}
        self._scheduled_events: List[tuple] = []
        self._notified: Set[str] = set()
        self._running = False
        self._stop_event = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        try:
            with open(self.base_dir / self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("cookie_checker load config: %s", e)
            return {}

    def _load_active_accounts(self) -> List[int]:
        config = self._load_config()
        posting_config = config.get("posting_config", {})
        acc_activity = posting_config.get("acc_activity", [])
        accounts = config.get("posting_accounts", [])
        target = acc_activity if acc_activity else list(range(1, len(accounts) + 1))
        active = []
        for idx in target:
            if 1 <= idx <= len(accounts) and accounts[idx - 1].get("enabled", True):
                active.append(idx)
        return active

    def _get_cookie_file_path(self, account_index: int) -> Path:
        config = self._load_config()
        accounts = config.get("posting_accounts", [])
        idx = account_index - 1
        if 0 <= idx < len(accounts):
            cookie_path = accounts[idx].get("cookies_file", "")
            if cookie_path:
                p = Path(cookie_path)
                return p if p.is_absolute() else self.base_dir / p
        return self.base_dir / "config" / "cookies" / f"account_{account_index}.json"

    def _get_cookie_expiry_info(self, account_index: int) -> Dict:
        cookie_path = self._get_cookie_file_path(account_index)
        result = {
            "account": account_index, "path": str(cookie_path),
            "exists": False, "modified_date": None, "expiry_date": None,
            "source": None,
        }
        if not cookie_path.exists():
            return result
        result["exists"] = True
        result["modified_date"] = datetime.fromtimestamp(cookie_path.stat().st_mtime)

        # Ưu tiên: JWT exp của cookie `token`
        try:
            cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
            if isinstance(cookies, list):
                token = next((c.get("value") for c in cookies if c.get("name") == "token"), None)
                exp = token_expiry(token)
                if exp:
                    result["expiry_date"] = datetime.fromtimestamp(exp)
                    result["source"] = "jwt"
                    return result
        except Exception:
            pass

        # Fallback: mtime + lifetime
        result["expiry_date"] = result["modified_date"] + timedelta(days=self.COOKIE_LIFETIME_DAYS)
        result["source"] = "mtime"
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _format_time_remaining(seconds: float) -> str:
        if seconds <= 0:
            return "ĐÃ HẾT HẠN"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        if hours > 24:
            return f"{hours // 24}d {hours % 24}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def _mark_db_expiry(self, account_idx: int, expiry_date: datetime, expired: bool):
        if self.db is None:
            return
        try:
            self.db.set_account_cookie_expiry(account_idx, int(expiry_date.timestamp()))
            if expired:
                self.db.update_account_status(account_idx, "expired",
                                              last_error="Cookie/JWT expired")
        except Exception as e:
            logger.warning("cookie_checker DB update failed: %s", e)

    def cache_and_schedule(self):
        active = self._load_active_accounts()
        now = datetime.now()
        self._cached_expiry.clear()
        self._scheduled_events.clear()
        self._notified.clear()

        for idx in active:
            info = self._get_cookie_expiry_info(idx)
            self._cached_expiry[idx] = info
            if not info["exists"]:
                logger.warning("Account %s: cookie file not found", idx)
                continue
            expiry = info["expiry_date"]
            self._mark_db_expiry(idx, expiry, expired=expiry <= now)
            logger.info("Account %s: expires %s (%s)", idx,
                        expiry.strftime("%d/%m/%Y %H:%M"), info["source"])

            t1h = expiry - timedelta(seconds=self.NOTIFY_1_HOUR)
            t30 = expiry - timedelta(seconds=self.NOTIFY_30_MIN)
            if t1h > now:
                heapq.heappush(self._scheduled_events, (t1h, "1hour", idx))
            if t30 > now:
                heapq.heappush(self._scheduled_events, (t30, "30min", idx))
            if expiry > now:
                heapq.heappush(self._scheduled_events, (expiry, "expired", idx))
            else:
                self._send_expired_notification(idx, expiry)

        self._schedule_next_daily_report()
        logger.info("Cookie checker scheduled %d events", len(self._scheduled_events))

    def _schedule_next_daily_report(self):
        now = datetime.now()
        nxt = now.replace(hour=self.DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        heapq.heappush(self._scheduled_events, (nxt, "daily_report", 0))

    def _send_expired_notification(self, idx: int, expiry_date: datetime):
        key = f"account_{idx}_expired"
        if key in self._notified:
            return
        self.alerts.alert(
            idx, f"Cookie/JWT ĐÃ HẾT HẠN lúc {expiry_date.strftime('%d/%m/%Y %H:%M:%S')}. "
                 "Đăng nhập lại để lấy cookie mới.",
            event="cookie_expired", component="cookie_checker", level="ERROR")
        self._mark_db_expiry(idx, expiry_date, expired=True)
        self._notified.add(key)

    def _process_event(self, event_type: str, idx: int):
        if event_type == "daily_report":
            self.send_daily_report()
            self.cache_and_schedule()
            return
        info = self._cached_expiry.get(idx)
        if not info or not info["exists"]:
            return
        key = f"account_{idx}_{event_type}"
        if key in self._notified:
            return
        expiry = info["expiry_date"]
        remaining = (expiry - datetime.now()).total_seconds()

        if event_type == "expired":
            self._send_expired_notification(idx, expiry)
            return
        label = "1 giờ" if event_type == "1hour" else "30 phút"
        self.alerts.alert(
            idx, f"Cookie sắp hết hạn — còn {self._format_time_remaining(remaining)} "
                 f"({label} nữa). Hết hạn lúc {expiry.strftime('%d/%m/%Y %H:%M:%S')}.",
            event=f"cookie_{event_type}", component="cookie_checker", level="WARN")
        self._notified.add(key)

    def send_daily_report(self):
        now = datetime.now()
        lines = ["📊 <b>BÁO CÁO COOKIE HÀNG NGÀY — OKXSquare</b>",
                 f"🕐 {now.strftime('%d/%m/%Y %H:%M')}", "━━━━━━━━━━━━━━━━━━━━━━"]
        ok = warn = expired = 0
        ordered = sorted(self._cached_expiry.items(),
                         key=lambda x: x[1].get("expiry_date") or datetime.max)
        for idx, info in ordered:
            if not info["exists"]:
                lines.append(f"❌ Account {idx}: FILE NOT FOUND")
                continue
            expiry = info["expiry_date"]
            rem = (expiry - now).total_seconds()
            days = rem / 86400
            es = expiry.strftime("%d/%m %H:%M")
            if rem <= 0:
                lines.append(f"🔴 <b>Account {idx}</b>: ĐÃ HẾT ({es})")
                expired += 1
            elif days < 1:
                lines.append(f"🔴 <b>Account {idx}</b>: {self._format_time_remaining(rem)} ({es})")
                warn += 1
            elif days < 4:
                lines.append(f"🟡 Account {idx}: {self._format_time_remaining(rem)} ({es})")
                warn += 1
            else:
                lines.append(f"🟢 Account {idx}: {self._format_time_remaining(rem)} ({es})")
                ok += 1
        lines += ["━━━━━━━━━━━━━━━━━━━━━━",
                  f"🟢 OK: {ok} | 🟡 Warning: {warn} | 🔴 Expired: {expired}"]
        self.alerts.notify("\n".join(lines))

    def print_status(self):
        if not self._cached_expiry:
            self.cache_and_schedule()
        now = datetime.now()
        print("\n" + "=" * 70)
        print("OKXSquare COOKIE / JWT EXPIRY STATUS")
        print("=" * 70)
        for idx, info in sorted(self._cached_expiry.items()):
            if not info["exists"]:
                print(f"❌ Account {idx}: FILE NOT FOUND")
                continue
            expiry = info["expiry_date"]
            rem = (expiry - now).total_seconds()
            icon = "💀" if rem <= 0 else ("🔴" if rem < 86400 else ("🟡" if rem < 4 * 86400 else "🟢"))
            print(f"{icon} Account {idx}: {self._format_time_remaining(rem)} "
                  f"[{info['source']}] expires {expiry.strftime('%d/%m/%Y %H:%M:%S')}")
        print("=" * 70 + "\n")

    def _background_loop(self):
        logger.info("Cookie expiry checker started (event-based)")
        while self._running and not self._stop_event.is_set():
            try:
                if not self._scheduled_events:
                    self._stop_event.wait(60)
                    continue
                next_time, event_type, idx = self._scheduled_events[0]
                wait = (next_time - datetime.now()).total_seconds()
                if wait > 0:
                    if self._stop_event.wait(wait):
                        break
                if not self._running or self._stop_event.is_set():
                    break
                heapq.heappop(self._scheduled_events)
                self._process_event(event_type, idx)
            except Exception as e:
                logger.error("Cookie checker loop error: %s", e)
                self._stop_event.wait(60)

    def start_background_checker(self):
        if self._running:
            return
        if not self._cached_expiry:
            self.cache_and_schedule()
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._background_loop, daemon=True,
                                        name="CookieExpiryChecker")
        self._thread.start()

    def stop_background_checker(self):
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)


if __name__ == "__main__":
    checker = CookieExpiryChecker()
    checker.cache_and_schedule()
    checker.print_status()
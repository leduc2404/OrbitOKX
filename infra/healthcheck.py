"""HealthCheck daemon — ping định kỳ báo trạng thái "đang hoạt động".

Tự dừng khi process chính bị kill (daemon thread) hoặc gọi stop().
Ping URL đọc từ config (healthcheck.ping_url); để trống = tắt.
"""
import os
import threading
from typing import Optional

import requests
import yaml

from infra.logging_config import get_logger

logger = get_logger(__name__)


class HealthChecker:
    def __init__(self, ping_url: str = "", interval_seconds: int = 60):
        self.ping_url = ping_url
        self.interval = interval_seconds
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def _load_proxy(self) -> Optional[dict]:
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base_dir, "config", "profiles_config.yaml")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
                proxy_str = ""
                for acc in config.get("posting_accounts", []):
                    if acc.get("proxy"):
                        proxy_str = acc["proxy"]
                        break
                if not proxy_str:
                    proxies = config.get("crawl_proxies", {}).get("proxies", [])
                    proxy_str = proxies[0] if proxies else ""
                if proxy_str:
                    return {"http": proxy_str, "https": proxy_str}
        except Exception:
            pass
        return None

    def _run(self):
        last_success = True
        error_logged = False
        proxies = self._load_proxy()

        while not self._stop_event.is_set():
            success = False
            error_msg = ""
            try:
                requests.get(self.ping_url, timeout=10)
                success = True
            except Exception as e:
                error_msg = str(e)

            if not success and proxies:
                try:
                    requests.get(self.ping_url, proxies=proxies, timeout=10)
                    success = True
                except Exception as e:
                    error_msg = str(e)

            if success:
                if not last_success:
                    logger.info("[HealthCheck] Kết nối mạng khôi phục, ping thành công.")
                last_success = True
                error_logged = False
            else:
                if last_success or not error_logged:
                    logger.warning("[HealthCheck] Ping thất bại: %s (hệ thống vẫn chạy nền)",
                                   error_msg[:120])
                    error_logged = True
                last_success = False

            self._stop_event.wait(self.interval)

    def start(self) -> bool:
        if not self.ping_url:
            return False
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="HealthCheckThread")
            self._thread.start()
            return True
        return False

    def stop(self):
        if self._thread is not None and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=2)


def start_health_check(ping_url: Optional[str] = None, interval: int = 60) -> HealthChecker:
    """Khởi chạy nhanh; ping_url=None sẽ đọc từ config healthcheck."""
    if ping_url is None:
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(base_dir, "config", "profiles_config.yaml"),
                      "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            hc = cfg.get("healthcheck", {})
            ping_url = hc.get("ping_url", "")
            interval = hc.get("interval_seconds", interval)
            if not hc.get("enabled", True):
                ping_url = ""
        except Exception:
            ping_url = ""
    checker = HealthChecker(ping_url=ping_url, interval_seconds=interval)
    checker.start()
    return checker
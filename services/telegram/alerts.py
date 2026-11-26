"""Centralized Telegram alerts for OKXSquare.

Every critical event (cookie/session expiry, quarantine, publish failure) routes
here: it records a structured row in task_logs AND pushes a Telegram message
carrying the account ID + a short summary + a truncated stack trace.

Secrets (bot_token) are read from config/telegram_config.yaml and never logged.
When the bot is not configured, alerts still persist to task_logs (no send).
"""
import html
from pathlib import Path
from typing import Optional

import requests
import yaml

from infra.logging_config import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PLACEHOLDERS = {"", "YOUR_BOT_TOKEN_HERE", "YOUR_REWARD_BOT_TOKEN_HERE"}
_MAX_STACK = 1500


class TelegramAlerts:
    def __init__(self, config_file: str = "config/telegram_config.yaml", db=None):
        self.db = db
        self.bot_token, self.reward_bot_token, self.chat_id = self._load(config_file)

    def _load(self, config_file):
        path = Path(config_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        token, reward_token, chat_id = "", "", 0
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    tg = (yaml.safe_load(f) or {}).get("telegram", {})
                token = (tg.get("bot_token") or "").strip()
                reward_token = (tg.get("reward_bot_token") or "").strip()
                chat_id = tg.get("alert_chat_id") or 0
                if not chat_id:
                    users = tg.get("allowed_users") or []
                    chat_id = users[0] if users else 0
        except Exception as e:
            logger.warning("TelegramAlerts config load failed: %s", e)
        return token, reward_token, chat_id

    @property
    def enabled(self) -> bool:
        return self.bot_token not in _PLACEHOLDERS and bool(self.chat_id)

    @property
    def reward_enabled(self) -> bool:
        return self.reward_bot_token not in _PLACEHOLDERS and bool(self.chat_id)

    def _post_message(self, token: str, text: str, parse_mode: str = "HTML") -> bool:
        if token in _PLACEHOLDERS or not self.chat_id:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = requests.post(url, json={
                "chat_id": self.chat_id, "text": text,
                "parse_mode": parse_mode, "disable_web_page_preview": True,
            }, timeout=10)
            if resp.status_code != 200:
                logger.warning("Telegram send failed (HTTP %s): %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as e:
            logger.warning("Telegram send failed: %s", e)
            return False

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        return self._post_message(self.bot_token, text, parse_mode=parse_mode)

    def send_reward(self, text: str, parse_mode: str = "HTML") -> bool:
        """Gửi báo cáo reward qua reward_bot_token (fallback sang bot_token nếu chưa cấu hình riêng)."""
        token = self.reward_bot_token if self.reward_enabled else self.bot_token
        return self._post_message(token, text, parse_mode=parse_mode)

    def notify(self, text: str) -> bool:
        """Non-error notification (daily report, resume, cookie updated)."""
        return self.send(text)

    def alert(self, account_id, summary: str, stack_trace: Optional[str] = None,
              level: str = "ERROR", event: str = "alert", component: str = "poster",
              post_id: Optional[str] = None) -> bool:
        """Record to task_logs and push a Telegram alert (account ID + summary + stack)."""
        if self.db is not None:
            try:
                self.db.log_task(level, component, event, summary,
                                 account_id=account_id, post_id=post_id,
                                 stack_trace=stack_trace)
            except Exception as e:
                logger.warning("alert task_logs write failed: %s", e)

        msg = (
            f"🚨 <b>OKXSquare Alert</b>\n"
            f"• Account: <b>{html.escape(str(account_id))}</b>\n"
            f"• Event: <code>{html.escape(event)}</code>\n"
            f"• {html.escape(summary)}"
        )
        if stack_trace:
            trimmed = stack_trace[-_MAX_STACK:]
            msg += f"\n<pre>{html.escape(trimmed)}</pre>"
        return self.send(msg)


_singleton: Optional[TelegramAlerts] = None


def get_alerts(db=None) -> TelegramAlerts:
    global _singleton
    if _singleton is None:
        _singleton = TelegramAlerts(db=db)
    elif db is not None and _singleton.db is None:
        _singleton.db = db
    return _singleton


def send_alert(account_id, summary: str, stack_trace: Optional[str] = None, **kwargs) -> bool:
    return get_alerts().alert(account_id, summary, stack_trace, **kwargs)
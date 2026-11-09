"""
Cookie Expiry Checker - Kiểm tra và thông báo cookie OKX sắp hết hạn
- Check lần đầu khi khởi chạy, ghi nhớ ngày hết hạn
- Lấy expirationDate từ cookie token (domain www.okx.com) trong file JSON.
- Gửi notification đến Telegram.
"""
import os
import time
import threading
import requests
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
import yaml
import heapq


class CookieExpiryCheckerOKX:
    """
    Kiểm tra thời gian hết hạn của cookie files và gửi thông báo Telegram
    Lấy expirationDate từ cookie token (domain www.okx.com) trong file JSON
    """
    
    # Telegram config (giống check_rewards.py)
    BOT_TOKEN = "8086838796:AAHQ4thOXnvH5VvPMORqERKL79EjHVItQS4"
    CHAT_ID = "5411129020"
    
    # Token cookie identifiers
    TOKEN_COOKIE_NAME = "token"
    TOKEN_COOKIE_DOMAIN = "www.okx.com"
    
    # Notification thresholds (in seconds)
    NOTIFY_30_MIN = 1800  # 30 min before expiry
    NOTIFY_1_HOUR = 3600  # 1 hour before expiry
    
    # Daily report time
    DAILY_REPORT_HOUR = 19  # 7 PM
    
    def __init__(self, config_path: str = "config/settings.yaml"):
        self.config_path = config_path
        self.base_dir = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
        # Cached expiry info
        self._cached_expiry: Dict[str, Dict] = {}
        
        # Scheduled events: (datetime, event_type, account_id)
        # event_type: "1day", "1hour", "expired", "daily_report"
        self._scheduled_events: List[tuple] = []
        
        # Track đã gửi notification nào
        self._notified: Set[str] = set()
        
        # Last daily report date
        self._last_daily_report_date: Optional[datetime] = None
        
        # Background thread
        self._running = False
        self._thread = None
        
        # Account names from config comments
        self._account_names: Dict[str, str] = {}
        
        # Lock for thread safety on scheduled events
        self._lock = threading.RLock()
    
    def _parse_account_names(self):
        """Parse tên account từ comment (#) trong phần proxies của YAML config"""
        import re
        names = {}
        try:
            config_file = self.base_dir / self.config_path
            with open(config_file, 'r', encoding='utf-8') as f:
                in_proxies = False
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith('proxies:'):
                        in_proxies = True
                        continue
                    if in_proxies:
                        if stripped and not stripped[0].isdigit() and not stripped.startswith('#'):
                            break
                        match = re.match(r'\s*(\d+):\s*"[^"]*"\s*#(.+)', line)
                        if match:
                            acc_idx = int(match.group(1))
                            name = match.group(2).strip()
                            names[f"account{acc_idx}"] = name
        except Exception as e:
            print(f"   ⚠ Lỗi parse account names: {e}")
        self._account_names = names
    
    def _get_account_label(self, account_id: str) -> str:
        """Lấy label account kèm tên, ví dụ: 'account1 (Chính)'"""
        name = self._account_names.get(account_id, "")
        return f"{account_id} ({name})" if name else account_id
        
    def _load_active_accounts(self) -> List[str]:
        """Load danh sách account active từ config"""
        try:
            config_file = self.base_dir / self.config_path
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            acc_activity = config.get('acc_activity', [])
            if acc_activity:
                return [f"account{n}" for n in acc_activity]
            return config.get('active_accounts', [])
        except Exception as e:
            print(f"✗ Error loading config: {e}")
            return []
    
    def _get_cookie_file_path(self, account_id: str) -> Path:
        """Lấy đường dẫn cookie file"""
        return self.base_dir / "cookies" / f"{account_id}.json"
        
    def _extract_cookie_info(self, cookie_path: Path) -> tuple:
        """Trích xuất token, userid, và expirationDate từ file cookie JSON"""
        token = "N/A"
        userid = "N/A"
        expiration_date = None
        try:
            with open(cookie_path, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            for c in cookies:
                name = c.get('name', '')
                val = c.get('value', '')
                domain = c.get('domain', '')
                if name == self.TOKEN_COOKIE_NAME and domain == self.TOKEN_COOKIE_DOMAIN:
                    token = val[:15] + "..." if len(val) > 15 else val
                    # Lấy expirationDate từ cookie token
                    exp_ts = c.get('expirationDate')
                    if exp_ts:
                        expiration_date = datetime.fromtimestamp(float(exp_ts))
                    # Parse jwt for uid if possible
                    parts = val.split('.')
                    if len(parts) >= 2:
                        import base64
                        b64_str = parts[1]
                        # JWT payload is base64url encoded: replace URL-safe chars and add padding
                        b64_str = b64_str.replace('-', '+').replace('_', '/')
                        b64_str += '=' * (-len(b64_str) % 4)
                        try:
                            payload = json.loads(base64.b64decode(b64_str).decode('utf-8'))
                            if 'uid' in payload:
                                userid = str(payload['uid'])
                        except Exception:
                            pass
                elif name == 'u_pid' and userid == "N/A":
                    userid = val
        except Exception:
            pass
        return token, userid, expiration_date
    
    def _get_cookie_expiry_info(self, account_id: str) -> Dict:
        """Lấy thông tin hết hạn của cookie file dựa trên expirationDate của token cookie"""
        cookie_path = self._get_cookie_file_path(account_id)
        
        result = {
            'account': account_id,
            'path': str(cookie_path),
            'exists': False,
            'modified_date': None,
            'expiry_date': None,
            'token_id': "N/A",
            'userid': "N/A"
        }
        
        if not cookie_path.exists():
            return result
        
        result['exists'] = True
        mod_timestamp = cookie_path.stat().st_mtime
        mod_date = datetime.fromtimestamp(mod_timestamp)
        result['modified_date'] = mod_date
        
        # Extract token id, userid, và expirationDate từ cookie token
        t, u, exp_date = self._extract_cookie_info(cookie_path)
        result['token_id'] = t
        result['userid'] = u
        
        if exp_date:
            result['expiry_date'] = exp_date
        else:
            # Fallback: không tìm thấy token cookie, đánh dấu không xác định
            print(f"   ⚠ {account_id}: Không tìm thấy cookie token (domain={self.TOKEN_COOKIE_DOMAIN}), không xác định ngày hết hạn")
            result['expiry_date'] = None
        
        return result
    
    def _send_telegram(self, message: str) -> bool:
        """Gửi message lên Telegram"""
        try:
            url = f"https://api.telegram.org/bot{self.BOT_TOKEN}/sendMessage"
            data = {
                'chat_id': self.CHAT_ID,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=data, timeout=10)
            return response.status_code == 200
        except Exception as e:
            print(f"✗ Lỗi gửi Telegram: {e}")
            return False
    
    def _format_time_remaining(self, seconds: float) -> str:
        """Format thời gian còn lại"""
        if seconds <= 0:
            return "ĐÃ HẾT HẠN"
        
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    
    def cache_and_schedule(self):
        """
        Cache ngày hết hạn và lên lịch notifications
        Gọi khi khởi chạy và sau mỗi 7h tối
        """
        active_accounts = self._load_active_accounts()
        now = datetime.now()
        
        # Parse account names
        self._parse_account_names()
        
        print(f"→ Caching expiry dates for {len(active_accounts)} accounts...")
        
        with self._lock:
            self._cached_expiry.clear()
            self._scheduled_events.clear()
            self._notified.clear()
            
            for account_id in active_accounts:
                info = self._get_cookie_expiry_info(account_id)
                self._cached_expiry[account_id] = info
                
                if not info['exists']:
                    print(f"   ❌ {self._get_account_label(account_id)}: FILE NOT FOUND")
                    continue
                
                expiry_date = info['expiry_date']
                if not expiry_date:
                    print(f"   ⚠️ {self._get_account_label(account_id)}: expiry date not set/found")
                    continue
                
                print(f"   {self._get_account_label(account_id)}: expires {expiry_date.strftime('%d/%m/%Y %H:%M')}")
                
                # Schedule notifications
                time_30m = expiry_date - timedelta(seconds=self.NOTIFY_30_MIN)
                time_1h = expiry_date - timedelta(seconds=self.NOTIFY_1_HOUR)
                
                # Only schedule future events
                if time_30m > now:
                    heapq.heappush(self._scheduled_events, (time_30m, "30min", account_id))
                if time_1h > now:
                    heapq.heappush(self._scheduled_events, (time_1h, "1hour", account_id))
                if expiry_date > now:
                    heapq.heappush(self._scheduled_events, (expiry_date, "expired", account_id))
                elif expiry_date <= now:
                    # Already expired - send notification immediately
                    self._send_expired_notification(account_id, expiry_date, info)
            
            # Schedule next daily report at 7 PM
            self._schedule_next_daily_report()
            
            print(f"✓ Scheduled {len(self._scheduled_events)} events")
    
    def _schedule_next_daily_report(self):
        """Lên lịch daily report tiếp theo lúc 7h tối"""
        now = datetime.now()
        next_report = now.replace(hour=self.DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
        
        if next_report <= now:
            next_report += timedelta(days=1)
        
        with self._lock:
            heapq.heappush(self._scheduled_events, (next_report, "daily_report", ""))
        print(f"   📅 Next daily report: {next_report.strftime('%d/%m/%Y %H:%M')}")
    
    def _send_expired_notification(self, account_id: str, expiry_date: datetime, info: dict):
        """Gửi notification đã hết hạn"""
        notify_key = f"{account_id}_expired"
        if notify_key in self._notified:
            return
        
        msg = (
            f"🚨 <b>COOKIE OKX ĐÃ HẾT HẠN!</b>\n\n"
            f"Account: <b>{self._get_account_label(account_id)}</b>\n"
            f"Token ID: <code>{info['token_id']}</code>\n"
            f"UserID: <code>{info['userid']}</code>\n"
            f"Hết hạn lúc: {expiry_date.strftime('%d/%m/%Y %H:%M:%S')}\n\n"
            f"⚠️ <b>ĐĂNG NHẬP NGAY ĐỂ LẤY COOKIE MỚI!</b>"
        )
        if self._send_telegram(msg):
            self._notified.add(notify_key)
            print(f"📱 Notification: Cookie {self._get_account_label(account_id)} HẾT HẠN")
    
    def _process_event(self, event_type: str, account_id: str):
        """Xử lý một event đã đến hạn"""
        if event_type == "daily_report":
            print(f"\n{'='*50}")
            print(f"⏰ 7PM - DAILY REPORT & RECHECK")
            print(f"{'='*50}\n")
            
            self.send_daily_report()
            self.cache_and_schedule()  # Recheck and reschedule
            return
        
        info = self._cached_expiry.get(account_id)
        if not info or not info['exists']:
            return
        
        expiry_date = info['expiry_date']
        if not expiry_date:
            return
            
        notify_key = f"{account_id}_{event_type}"
        
        if notify_key in self._notified:
            return
        
        if event_type == "1day":
            now = datetime.now()
            remaining = (expiry_date - now).total_seconds()
            msg = (
                f"⚠️ <b>COOKIE OKX SẮP HẾT HẠN!</b>\n\n"
                f"Account: <b>{self._get_account_label(account_id)}</b>\n"
                f"Token ID: <code>{info['token_id']}</code>\n"
                f"UserID: <code>{info['userid']}</code>\n"
                f"Còn lại: <b>{self._format_time_remaining(remaining)}</b>\n"
                f"Hết hạn lúc: {expiry_date.strftime('%d/%m/%Y %H:%M:%S')}\n\n"
                f"⏰ Còn 30 phút nữa!"
            )
        elif event_type == "1hour":
            now = datetime.now()
            remaining = (expiry_date - now).total_seconds()
            msg = (
                f"⚠️ <b>COOKIE OKX SẮP HẾT HẠN!</b>\n\n"
                f"Account: <b>{self._get_account_label(account_id)}</b>\n"
                f"Token ID: <code>{info['token_id']}</code>\n"
                f"UserID: <code>{info['userid']}</code>\n"
                f"Còn lại: <b>{self._format_time_remaining(remaining)}</b>\n"
                f"Hết hạn lúc: {expiry_date.strftime('%d/%m/%Y %H:%M:%S')}\n\n"
                f"⏰ Còn 1 tiếng nữa!"
            )
        elif event_type == "expired":
            msg = (
                f"🚨 <b>COOKIE OKX ĐÃ HẾT HẠN!</b>\n\n"
                f"Account: <b>{self._get_account_label(account_id)}</b>\n"
                f"Token ID: <code>{info['token_id']}</code>\n"
                f"UserID: <code>{info['userid']}</code>\n"
                f"Hết hạn lúc: {expiry_date.strftime('%d/%m/%Y %H:%M:%S')}\n\n"
                f"⚠️ <b>ĐĂNG NHẬP NGAY ĐỂ LẤY COOKIE MỚI!</b>"
            )
        else:
            return
        
        if self._send_telegram(msg):
            self._notified.add(notify_key)
            print(f"📱 Notification: Cookie {self._get_account_label(account_id)} - {event_type}")
    
    def send_daily_report(self):
        """Gửi báo cáo tổng hợp trạng thái tất cả accounts"""
        now = datetime.now()
        
        msg_lines = [
            f"📊 <b>BÁO CÁO COOKIE OKX HÀNG NGÀY</b>",
            f"🕐 {now.strftime('%d/%m/%Y %H:%M')}",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        expired_count = 0
        warning_count = 0
        ok_count = 0
        
        sorted_accounts = sorted(
            self._cached_expiry.items(),
            key=lambda x: (x[1].get('expiry_date') or datetime.max) if x[1].get('exists') else datetime.max
        )
        
        for account_id, info in sorted_accounts:
            acc_label = self._get_account_label(account_id)
            if not info['exists']:
                msg_lines.append(f"❌ {acc_label}: FILE NOT FOUND")
                continue
            
            expiry_date = info['expiry_date']
            if not expiry_date:
                msg_lines.append(f"⚠️ <b>{acc_label}</b>: Ngày hết hạn không xác định (Thiếu/sai cookie token)")
                warning_count += 1
                continue
                
            seconds_remaining = (expiry_date - now).total_seconds()
            days_remaining = seconds_remaining / 86400
            
            expiry_str = expiry_date.strftime('%d/%m %H:%M')
            
            if seconds_remaining <= 0:
                msg_lines.append(f"🔴 <b>{acc_label}</b>: ĐÃ HẾT ({expiry_str})")
                expired_count += 1
            elif days_remaining < 2:
                msg_lines.append(f"🔴 <b>{acc_label}</b>: {self._format_time_remaining(seconds_remaining)} ({expiry_str})")
                warning_count += 1
            elif days_remaining < 5:
                msg_lines.append(f"🟡 {acc_label}: {self._format_time_remaining(seconds_remaining)} ({expiry_str})")
                warning_count += 1
            else:
                msg_lines.append(f"🟢 {acc_label}: {self._format_time_remaining(seconds_remaining)} ({expiry_str})")
                ok_count += 1
        
        msg_lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"<b>Tổng kết:</b>",
            f"🟢 OK: {ok_count} | 🟡 Warning: {warning_count} | 🔴 Expired: {expired_count}"
        ])
        
        if self._send_telegram("\n".join(msg_lines)):
            print(f"📊 Daily report sent: OK={ok_count}, Warning={warning_count}, Expired={expired_count}")
    
    def print_status(self):
        """In trạng thái của tất cả cookies ra console"""
        if not self._cached_expiry:
            self.cache_and_schedule()
        
        now = datetime.now()
        
        print("\n" + "=" * 70)
        print("OKX COOKIE EXPIRY STATUS")
        print("=" * 70)
        
        for account_id, info in sorted(self._cached_expiry.items()):
            acc_label = self._get_account_label(account_id)
            if not info['exists']:
                print(f"❌ {acc_label}: FILE NOT FOUND")
                continue
            
            expiry_date = info['expiry_date']
            if not expiry_date:
                print(f"⚠️ {acc_label}: KHÔNG XÁC ĐỊNH NGÀY HẾT HẠN")
                print(f"   Modified: {info['modified_date'].strftime('%d/%m/%Y %H:%M:%S') if info['modified_date'] else 'N/A'}")
                print(f"   Token ID: {info['token_id']}")
                print(f"   UserID:   {info['userid']}")
                continue
                
            seconds_remaining = (expiry_date - now).total_seconds()
            days_remaining = seconds_remaining / 86400
            
            if seconds_remaining <= 0:
                status_icon = '💀'
            elif days_remaining < 2:
                status_icon = '🔴'
            elif days_remaining < 5:
                status_icon = '🟡'
            else:
                status_icon = '🟢'
            
            print(f"{status_icon} {acc_label}: {self._format_time_remaining(seconds_remaining)}")
            print(f"   Modified: {info['modified_date'].strftime('%d/%m/%Y %H:%M:%S') if info['modified_date'] else 'N/A'}")
            print(f"   Expires:  {expiry_date.strftime('%d/%m/%Y %H:%M:%S')}")
            print(f"   Token ID: {info['token_id']}")
            print(f"   UserID:   {info['userid']}")
        
        print("=" * 70 + "\n")
    
    def _background_loop(self):
        """
        Background loop - chờ đến thời điểm scheduled events
        Không poll, chỉ sleep đến event tiếp theo
        """
        print(f"🔄 OKX Cookie expiry checker started (event-based)")
        
        while self._running:
            try:
                has_event = False
                with self._lock:
                    if self._scheduled_events:
                        next_event_time, event_type, account_id = self._scheduled_events[0]
                        has_event = True
                
                if not has_event:
                    # Không có events, sleep 1 phút rồi check lại
                    time.sleep(60)
                    continue
                
                # Tính thời gian chờ
                now = datetime.now()
                wait_seconds = (next_event_time - now).total_seconds()
                
                if wait_seconds > 0:
                    # Chờ đến event, nhưng check _running mỗi 10 giây
                    while wait_seconds > 0 and self._running:
                        sleep_time = min(wait_seconds, 10)
                        time.sleep(sleep_time)
                        wait_seconds -= sleep_time
                
                if not self._running:
                    break
                
                # Pop event và xử lý
                should_process = False
                with self._lock:
                    if self._scheduled_events:
                        curr_event = self._scheduled_events[0]
                        if curr_event[0] <= datetime.now():
                            heapq.heappop(self._scheduled_events)
                            should_process = True
                            event_type, account_id = curr_event[1], curr_event[2]
                
                if should_process:
                    self._process_event(event_type, account_id)
                else:
                    time.sleep(1)
                
            except Exception as e:
                print(f"✗ OKX Cookie checker error: {e}")
                time.sleep(60)
        
        print("🛑 OKX Cookie expiry checker stopped")
    
    def start_background_checker(self, check_interval: int = None):
        """Bắt đầu background thread"""
        if self._running:
            return
        
        if not self._cached_expiry:
            self.cache_and_schedule()
        
        self._running = True
        self._thread = threading.Thread(
            target=self._background_loop,
            daemon=True
        )
        self._thread.start()
    
    def stop_background_checker(self):
        """Dừng background checker"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)


# Standalone test
if __name__ == "__main__":
    checker = CookieExpiryCheckerOKX()
    
    # Cache và in trạng thái
    checker.cache_and_schedule()
    checker.print_status()
    
    # Test daily report
    print("\nSending test daily report...")
    checker.send_daily_report()

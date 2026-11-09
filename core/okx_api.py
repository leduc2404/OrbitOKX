import json
import os
import time
from curl_cffi import requests
from core.client_profile import (
    ClientProfile,
    current_utc_offset,
    headers_order_string_hash,
)
from core.crypto_signer import CryptoSigner

COOKIES_DIR = "cookies"

class OKXApi:
    """
    HTTP Base Client xử lý mọi Headers chống gian lận của OKX.
    Sử dụng curl_cffi để ngụy trang kết nối TLS (chống Cloudflare chặn tay Python).

    Session Resilience:
      - Tự động refresh session sau MAX_SESSION_AGE_HOURS hoặc MAX_REQUESTS_BEFORE_REFRESH
      - health_check() để kiểm tra kết nối
      - reload_cookies_from_disk() để cập nhật token nếu file cookie thay đổi
    """

    MAX_SESSION_AGE_HOURS = 2.0
    MAX_REQUESTS_BEFORE_REFRESH = 500

    def __init__(self, account_data, proxy=None):
        self.account_id = account_data["id"]
        self.dev_id = account_data["dev_id"]
        self.tmx_session_id = account_data.get("tmx_session_id", "")
        self.x_site_info = account_data.get("x_site_info", "")
        self.authorization = account_data["authorization"]
        self.profile = ClientProfile.from_account(account_data)

        self.base_url = "https://www.okx.com"
        self._proxy = proxy

        # Khởi tạo Cryptography
        self.signer = CryptoSigner(self.account_id, self.dev_id)

        # Session tracking
        self._session_created_at = time.time()
        self._request_count = 0

        # Tạo session
        self._create_session()

    # =========================================================================
    # SESSION MANAGEMENT
    # =========================================================================

    def _create_session(self):
        """Tạo mới curl_cffi session với TLS fingerprint ngụy trang."""
        self.session = requests.Session(impersonate="chrome110")
        if self._proxy:
            self.session.proxies = {
                "http": self._proxy,
                "https": self._proxy
            }
        self._session_created_at = time.time()
        self._request_count = 0

    def refresh_session(self):
        """
        Tạo lại session hoàn toàn mới.
        Giữ nguyên auth tokens, chỉ reset connection pool + TLS state.
        """
        try:
            self.session.close()
        except Exception:
            pass
        self._create_session()

    def _maybe_refresh_session(self):
        """
        Auto-refresh session nếu quá cũ hoặc quá nhiều requests.
        Gọi trước mỗi request để đảm bảo session luôn fresh.
        """
        age_hours = (time.time() - self._session_created_at) / 3600
        if age_hours >= self.MAX_SESSION_AGE_HOURS:
            self.refresh_session()
            return

        if self._request_count >= self.MAX_REQUESTS_BEFORE_REFRESH:
            self.refresh_session()

    def health_check(self) -> bool:
        """
        Kiểm tra kết nối bằng API nhẹ nhất (server time).
        Returns True nếu OK, False nếu session/token có vấn đề.
        """
        try:
            path = "/priapi/v5/public/time"
            headers = self._build_headers(path, "GET")
            url = f"{self.base_url}{path}"
            res = self.session.get(url, headers=headers, timeout=10)
            data = res.json()
            return data and data.get("code") == "0"
        except Exception:
            return False

    def reload_cookies_from_disk(self) -> bool:
        """
        Đọc lại file cookie từ disk và cập nhật auth tokens.
        Dùng khi phát hiện token expired (nhiều lỗi liên tiếp).
        Returns True nếu đã cập nhật thành công.
        """
        cookie_path = os.path.join(COOKIES_DIR, f"{self.account_id}.json")
        if not os.path.exists(cookie_path):
            return False

        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception:
            return False

        new_auth = ""
        new_dev_id = ""
        new_tmx = ""
        new_site_info = ""

        for c in cookies:
            name = c.get("name")
            val = c.get("value")
            if name == "token":
                new_auth = val
            elif name == "devId":
                new_dev_id = val
            elif name == "tmx_session_id":
                new_tmx = val
            elif name == "ok_site_info":
                new_site_info = val

        if not new_auth or not new_dev_id:
            return False

        # Chỉ cập nhật nếu khác giá trị cũ
        changed = False
        if new_auth and new_auth != self.authorization:
            self.authorization = new_auth
            changed = True
        if new_dev_id and new_dev_id != self.dev_id:
            self.dev_id = new_dev_id
            self.signer = CryptoSigner(self.account_id, self.dev_id)
            changed = True
        if new_tmx:
            self.tmx_session_id = new_tmx
        if new_site_info:
            self.x_site_info = new_site_info

        if changed:
            self.refresh_session()

        return changed

    # =========================================================================
    # HTTP METHODS
    # =========================================================================

    def _build_headers(self, path, method="GET", body="", extra_headers=None):
        """
        Sinh headers theo đúng thứ tự interceptor của app 6.194.0.

        headers-order-string-hash được tính trên tập header trước DeviceInfoInterceptor
        (change + Host + caller headers), đúng như C5840Jei.EZpvd.
        """
        profile = self.profile
        timestamp = str(int(time.time() * 1000))
        url_path = path.split("?", 1)[0]
        x_id = self.signer.generate_xid(url_path)

        pre_device_headers = dict(extra_headers or {})
        pre_device_headers["change"] = "1"
        pre_device_headers["Host"] = "www.okx.com"

        headers = dict(pre_device_headers)
        headers["User-Agent"] = profile.user_agent()
        headers["devId"] = self.dev_id
        headers["platform"] = "android"
        headers["real-app-version"] = profile.real_app_version
        headers["x-utc"] = current_utc_offset()
        headers["risk-params"] = profile.risk_params
        headers["fingerprint-id"] = profile.fingerprint_id or self.dev_id
        headers["tmx-session-id"] = self.tmx_session_id
        headers["tmx-session-status"] = profile.tmx_session_status
        headers["headers-order-string-hash"] = headers_order_string_hash(pre_device_headers.keys())
        if profile.lua_version:
            headers["lua-version"] = profile.lua_version
        if profile.app_web_mode:
            headers["app_web_mode"] = profile.app_web_mode
        headers["x-site-info"] = self.x_site_info
        headers["Authorization"] = self.authorization
        headers["x-simulated-trading"] = str(profile.token_type)
        headers["Referer"] = f"{self.base_url}{url_path}"
        headers["x-cdn"] = profile.x_cdn

        # Đính kèm Signature cho POST/PUT
        if method.upper() in ("POST", "PUT"):
            sig = self.signer.generate_signature(path, body, timestamp)
            headers["X-Request-Timestamp"] = timestamp
            headers["X-Client-Signature"] = sig
            headers["X-Client-Public-Key"] = self.signer.pub_key_b64
            headers["Content-Type"] = "application/json; charset=utf-8"

        headers["x-id"] = x_id
        return headers

    def get(self, path, extra_headers=None):
        self._maybe_refresh_session()
        self._request_count += 1
        headers = self._build_headers(path, "GET", extra_headers=extra_headers)
        url = f"{self.base_url}{path}"
        return self.session.get(url, headers=headers)

    def post(self, path, body_json_str, extra_headers=None):
        self._maybe_refresh_session()
        self._request_count += 1
        headers = self._build_headers(path, "POST", body_json_str, extra_headers=extra_headers)
        url = f"{self.base_url}{path}"
        return self.session.post(url, data=body_json_str, headers=headers)

    def put_raw(self, full_url, data, content_type=None, max_retries=3):
        """Dùng riêng cho việc upload ảnh trực tiếp (như Aliyun OSS) không cần Sign của OKX"""
        import requests as std_requests
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        else:
            headers["Content-Type"] = None

        proxies = self.session.proxies if hasattr(self.session, 'proxies') and self.session.proxies else None

        # Danh sách các domain để fallback (Nhà mạng VN hay chặn oss-accelerate.aliyuncs.com)
        urls_to_try = [full_url]
        if "oss-accelerate.aliyuncs.com" in full_url:
            urls_to_try.append(full_url.replace("oss-accelerate.aliyuncs.com", "oss-cn-hongkong.aliyuncs.com"))
            urls_to_try.append(full_url.replace("oss-accelerate.aliyuncs.com", "oss-accelerate-overseas.aliyuncs.com"))

        last_error = None
        for attempt_url in urls_to_try:
            for attempt in range(1, max_retries + 1):
                try:
                    res = std_requests.put(attempt_url, data=data, headers=headers, proxies=proxies, timeout=30)
                    if res.status_code == 200:
                        return res
                    last_error = Exception(f"HTTP {res.status_code}: {res.text}")
                except Exception as e:
                    last_error = e
                    time.sleep(2)  # Delay trước khi thử lại

        # Nếu tất cả các domain và retry đều hỏng
        if last_error:
            raise last_error
        raise RuntimeError("Failed to upload image to OSS after all retries")

    # =========================================================================
    # REOACTIVE ENGAGEMENT APIs (Phản hồi tương tác)
    # =========================================================================

    def get_unread_notifications(self):
        """
        Lấy danh sách các thông báo vòng lặp (comments/replies/likes/follows)
        """
        path = "/priapi/v5/content/msg/unread/list"
        return self.get(path)

    def like_comment(self, comment_id: str):
        """
        Thả tim một bình luận.
        OKX dùng chung endpoint thả tim bài viết, chỉ khác ở contentId = commentId.
        """
        path = "/priapi/v5/content/follower/like"
        body = {
            "contentId": str(comment_id),
            "isLiked": True
        }
        body_str = json.dumps(body, separators=(',', ':'))
        return self.post(path, body_str)

    def reply_comment(self, post_id: str, comment_id: str, text: str):
        """
        Trả lời (Reply) lại một bình luận của người khác trên bài viết.
        """
        path = "/priapi/v5/content/user/comment"
        body = {
            "hashTagListStr": "[]",
            "contentId": str(post_id),
            "comment": text,
            "replayTo": str(comment_id)
        }
        body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
        return self.post(path, body_str)

    def mark_notifications_read(self, max_msg_id: str):
        """
        Đánh dấu tất cả thông báo từ max_msg_id trở xuống là đã đọc
        """
        path = "/priapi/v5/content/msg/mark-read"
        body = {
            "msgId": str(max_msg_id)
        }
        body_str = json.dumps(body, separators=(',', ':'))
        return self.post(path, body_str)

"""
Binance Square Crawler — Endpoint-based
========================================
Crawl bài viết từ Binance Square qua public API endpoint.
- KHÔNG cần cookie
- KHÔNG cần proxy
- KHÔNG crop ảnh (lưu nguyên gốc)
- State riêng: data/state_binance.json (chỉ ghi last crawl time chung)

Thiết kế để chạy song song với OKXCrawler trong cùng orchestrator.
"""

import requests
import re
import os
import time
import json
import random
import threading
from urllib.parse import urlparse
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.qr_detector import contains_qr_code
from utils.image_sanitizer import sanitize_image, ensure_sanitized_image


class BinanceCrawler:
    """Binance Square Crawler sử dụng public API endpoint"""

    IMAGE_DIR = os.path.join("data", "images")
    STATE_FILE = os.path.join("data", "state_binance.json")

    def __init__(self, db_manager, max_retries: int = 3, retry_delay: int = 5):
        """
        Khởi tạo crawler.

        Args:
            db_manager: DatabaseManager instance (shared với OKX)
            max_retries: Số lần retry khi gặp lỗi
            retry_delay: Delay cơ sở giữa các lần retry (giây)
        """
        self.db_manager = db_manager
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._state_lock = threading.Lock()

        # Đảm bảo thư mục ảnh tồn tại
        os.makedirs(self.IMAGE_DIR, exist_ok=True)

    # ==============================================================
    # STATE MANAGEMENT — Lưu last crawl time chung + theo từng author
    # ==============================================================

    def _load_state_data(self) -> dict:
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                pass
        return {}

    def _save_state_data(self, data: dict):
        try:
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(self.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"  ⚠ [BINANCE] Lỗi lưu state: {e}")

    def get_last_crawl_time(self, userid: Optional[str] = None, lookback_hours: float = 2.0) -> int:
        """
        Lấy timestamp lần crawl gần nhất (ms) cho userid (hoặc chung).
        Nếu chưa từng crawl → trả về lookback_hours giờ trước.
        """
        with self._state_lock:
            data = self._load_state_data()
        if userid:
            authors_map = data.get("authors") or {}
            if userid in authors_map and authors_map[userid]:
                return int(authors_map[userid])
        if data.get("last_crawl_time_ms"):
            return int(data["last_crawl_time_ms"])
        return int((datetime.now() - timedelta(hours=lookback_hours)).timestamp() * 1000)

    def save_last_crawl_time(self, time_ms: int, userid: Optional[str] = None):
        """Lưu timestamp lần crawl hiện tại (cho 1 author hoặc toàn cục)"""
        with self._state_lock:
            data = self._load_state_data()
            if userid:
                authors_map = data.setdefault("authors", {})
                authors_map[userid] = int(time_ms)
            else:
                data["last_crawl_time_ms"] = int(time_ms)
            self._save_state_data(data)

    # ==============================================================
    # HTTP REQUEST — Direct, no proxy, with retry
    # ==============================================================

    def _request_with_retry(self, url: str, headers: dict,
                            params: dict = None,
                            method: str = "GET",
                            max_retries: int = 3) -> Optional[requests.Response]:
        """
        HTTP request với retry và exponential backoff.
        Không dùng proxy — direct connection.
        """
        for attempt in range(1, max_retries + 1):
            try:
                if method == "GET":
                    r = requests.get(url, headers=headers, params=params, timeout=15)
                else:
                    r = requests.post(url, headers=headers, json=params, timeout=15)

                if r.status_code == 200:
                    return r
                elif r.status_code == 429:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] [BINANCE] Rate limit 429! Đợi {wait}s...")
                    time.sleep(wait)
                    continue
                elif r.status_code >= 500:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] [BINANCE] Server error {r.status_code}! Đợi {wait}s...")
                    time.sleep(wait)
                    continue
                else:
                    return r  # 4xx khác → trả về để xử lý bên ngoài

            except Exception as e:
                if attempt < max_retries:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] [BINANCE] Request error: {e}. Retry sau {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  [❌] [BINANCE] Request thất bại sau {max_retries} lần: {e}")

        return None

    # ==============================================================
    # TEXT CLEANING
    # ==============================================================

    def _clean_text(self, raw_text: str) -> str:
        """Làm sạch nội dung bài viết"""
        if not raw_text:
            return ""
        text = re.sub(r'\s+', ' ', raw_text).strip()
        text = re.sub(r'\{.*?\}\((.*?)\)', r'\1', text)
        text = re.sub(r'\b[A-Za-z]+USDT\b', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    # ==============================================================
    # IMAGE DOWNLOAD — KHÔNG CROP, lưu nguyên gốc
    # ==============================================================

    def download_images(self, post_id: str, image_urls: List[str]) -> List[str]:
        """
        Download ảnh về local NGUYÊN GỐC (không crop, không chỉnh).
        Tự động phát hiện mã QR và loại bỏ toàn bộ bài.

        Args:
            post_id: ID bài viết (dùng làm tên file)
            image_urls: Danh sách URL ảnh

        Returns:
            List[str]: Đường dẫn local đã tải thành công
            None: Nếu phát hiện mã QR → bài viết cần bị loại bỏ hoàn toàn
        """
        if not image_urls:
            return []

        saved = []
        for i, url in enumerate(image_urls):
            try:
                # Xác định extension
                parsed = urlparse(url)
                ext = os.path.splitext(parsed.path)[1] or ".jpg"
                if ext.lower() not in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                    ext = ".jpg"

                filename = f"{post_id}_{i}{ext}"
                filepath = os.path.join(self.IMAGE_DIR, filename)
                jpg_filepath = os.path.join(self.IMAGE_DIR, f"{post_id}_{i}.jpg")
                existing_path = jpg_filepath if os.path.exists(jpg_filepath) else (filepath if os.path.exists(filepath) else None)

                # Skip nếu đã tồn tại (vẫn check QR + đảm bảo đã sanitize)
                if existing_path:
                    if contains_qr_code(existing_path):
                        print(f"    [🚫] [BINANCE] Phát hiện mã QR trong ảnh {existing_path}! Loại bỏ toàn bộ bài {post_id}")
                        try: os.remove(existing_path)
                        except: pass
                        for p in saved:
                            try: os.remove(p)
                            except: pass
                        return None
                    sanitized = ensure_sanitized_image(existing_path)
                    if sanitized and sanitized.get("path"):
                        saved.append(str(sanitized["path"]))
                    continue

                # Download nguyên gốc
                with requests.get(url, timeout=15, stream=True) as r:
                    if r.status_code == 200:
                        with open(filepath, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                        
                        # ====== QR CODE CHECK (trên ảnh nguyên bản trước khi biến đổi) ======
                        if contains_qr_code(filepath):
                            print(f"    [🚫] [BINANCE] Phát hiện mã QR trong ảnh {filepath}! Loại bỏ toàn bộ bài {post_id}")
                            try: os.remove(filepath)
                            except: pass
                            for p in saved:
                                try: os.remove(p)
                                except: pass
                            return None
                        
                        # ====== SANITIZE METADATA & ANTI-DUPLICATE EVASION ======
                        sanitized = sanitize_image(filepath, seed=f"{post_id}_{i}")
                        if sanitized and sanitized.get("path"):
                            saved.append(str(sanitized["path"]))
                        else:
                            print(f"    [⚠️] [BINANCE] Bỏ qua ảnh lỗi/rỗng của bài {post_id}: {filepath}")
                    else:
                        print(f"    [⚠️] [BINANCE] Không tải được ảnh {url}: HTTP {r.status_code}")

            except Exception as e:
                print(f"    [⚠️] [BINANCE] Lỗi download ảnh {url}: {e}")

        return saved

    # ==============================================================
    # POST FILTERING
    # ==============================================================

    def _is_pinned(self, post_meta: dict) -> bool:
        """Kiểm tra xem bài viết có đang được ghim hay không"""
        if not isinstance(post_meta, dict):
            return False
        if post_meta.get("isStickyToTop"):
            return True
        detail = post_meta.get("contentDetail") or {}
        return bool(isinstance(detail, dict) and detail.get("isStickyToTop"))

    # ==============================================================
    # CRAWL — Lấy danh sách bài và chi tiết
    # ==============================================================

    def _get_post_ids(self, userid: str, cutoff_time_ms: int) -> List[str]:
        """
        Lấy danh sách ID bài viết mới nhất của user.
        Chỉ trả về bài tạo sau cutoff_time_ms.
        """
        url = "https://www.binance.com/bapi/composite/v1/friendly/pgc/content/queryUserProfilePageContentsWithFilter"
        params = {
            "targetSquareUid": userid,
            "timeOffset": "-1",
            "filterType": "ALL"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Clienttype": "web",
            "Referer": f"https://www.binance.com/vi/square/profile/{userid}"
        }

        r = self._request_with_retry(url, headers, params, max_retries=self.max_retries)

        if not r or r.status_code != 200:
            print(f"  [❌] [BINANCE] Không lấy được post IDs cho {userid}")
            return []

        try:
            data = r.json().get("data", {})
            contents = data.get("contents", []) or []
            ids = []

            # Buffer 5 phút để tránh miss bài
            safe_cutoff = cutoff_time_ms - 300000

            for item in contents:
                # Skip bài ghim
                if self._is_pinned(item):
                    continue

                if isinstance(item, dict):
                    create_time_raw = item.get("createTime")
                    is_within_window = False

                    if create_time_raw:
                        try:
                            if int(float(create_time_raw)) >= safe_cutoff:
                                is_within_window = True
                        except Exception:
                            pass

                    if is_within_window:
                        post_id = item.get("id") or item.get("postId")
                        if post_id:
                            ids.append(str(post_id))

            return ids
        except Exception as e:
            print(f"  [❌] [BINANCE] Lỗi parse post IDs cho {userid}: {e}")
            return []

    def _crawl_post_detail(self, post_id: str) -> Optional[dict]:
        """
        Lấy chi tiết 1 bài viết từ Binance API.

        Returns:
            dict với keys: post_id, text, image_urls, publish_time
            hoặc None nếu lỗi/invalid
        """
        url = f"https://www.binance.com/bapi/composite/v2/friendly/pgc/special/content/detail/{post_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Clienttype": "web",
            "Referer": f"https://www.binance.com/vi/square/post/{post_id}"
        }

        r = self._request_with_retry(url, headers, max_retries=self.max_retries)

        if not r:
            return None

        if r.status_code == 429:
            print(f"  [⚠️] [BINANCE] Rate limited cho post {post_id}")
            return None
        elif r.status_code != 200:
            print(f"  [⚠️] [BINANCE] HTTP {r.status_code} cho post {post_id}")
            return None

        try:
            data = r.json().get("data", {})
        except (ValueError, AttributeError):
            print(f"  [❌] [BINANCE] Invalid JSON cho post {post_id}")
            return None

        post_id_api = data.get("id")
        if not post_id_api:
            return None

        body_text = self._clean_text(data.get("bodyTextOnly", ""))

        # Skip bài quá ngắn
        if not body_text or len(body_text.strip()) < 10:
            return None

        # Parse create_time
        create_time_raw = data.get("createTime")
        create_time = None
        if create_time_raw:
            try:
                create_time = int(float(create_time_raw)) if isinstance(create_time_raw, str) else int(create_time_raw)
            except Exception:
                pass

        image_list = data.get("imageList", []) or []

        return {
            "post_id": str(post_id_api),
            "text": body_text,
            "image_urls": image_list,
            "publish_time": create_time
        }

    # ==============================================================
    # PUBLIC API — Được gọi bởi Orchestrator
    # ==============================================================

    def get_user_timeline(self, userid: str, username: str,
                          since_timestamp: int, limit: Optional[int] = None) -> List[dict]:
        """
        Crawl bài viết mới từ 1 user Binance.
        Interface tương tự OKXCrawler.get_user_timeline() để orchestrator
        xử lý thống nhất.

        Args:
            userid: Binance Square UID
            username: Username hiển thị
            since_timestamp: Chỉ lấy bài sau thời điểm này (ms)
            limit: Giới hạn tối đa số bài viết mới cần lấy chi tiết (ms)

        Returns:
            List[dict]: Danh sách bài viết mới
        """
        print(f"  [🕷️] [BINANCE] Đang cào {username} ({userid})...")

        # 1. Lấy danh sách post IDs mới
        post_ids = self._get_post_ids(userid, since_timestamp)

        if not post_ids:
            print(f"  [📭] [BINANCE] {username}: không có bài mới.")
            return []

        # 2. Batch check existing trong DB
        existing_ids = self.db_manager.batch_check_existing_posts(post_ids)
        new_ids = [pid for pid in post_ids if pid not in existing_ids]

        if not new_ids:
            print(f"  [⛔] [BINANCE] {username}: tất cả {len(post_ids)} bài đã có trong DB")
            return []

        if limit is not None:
            new_ids = new_ids[:limit]

        print(f"  [📋] [BINANCE] {username}: {len(post_ids)} bài → xử lý {len(new_ids)} mới, {len(existing_ids)} đã có")

        # 3. Crawl chi tiết từng bài mới
        results = []
        for pid in new_ids:
            detail = self._crawl_post_detail(pid)
            if detail:
                detail["author_id"] = userid
                detail["username"] = username
                results.append(detail)

            # Delay nhỏ giữa các request
            time.sleep(0.5)

        print(f"  [✅] [BINANCE] {username}: {len(results)} bài mới lấy thành công")
        return results

    def crawl_all_users(
        self,
        binance_authors: List[dict],
        max_workers: int = 5,
        max_new_posts: Optional[int] = None,
        lookback_hours: float = 2.0,
    ) -> int:
        """
        Crawl Binance users bằng ThreadPoolExecutor với cơ chế giới hạn ngân sách (budget).
        Khi đạt đủ `max_new_posts` bài mới cần cho buffer, dừng ngay các author còn lại
        để không lãng phí tài nguyên HTTP/ảnh. State được lưu theo từng author nên
        các author chưa duyệt trong chu kỳ này vẫn giữ mốc thời gian cũ cho chu kỳ sau.
        """
        if not binance_authors:
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0

        current_time_ms = int(time.time() * 1000)
        authors_list = list(binance_authors)
        random.shuffle(authors_list)

        budget_str = f" | budget={max_new_posts} bài" if max_new_posts is not None else ""
        print(f"\n{'─'*60}")
        print(f"[BINANCE] Crawling {len(authors_list)} users ({max_workers} threads{budget_str})")
        print(f"{'─'*60}")

        total_new = 0
        budget_lock = threading.Lock()

        def _remaining_budget() -> Optional[int]:
            if max_new_posts is None:
                return None
            with budget_lock:
                return max(0, max_new_posts - total_new)

        def crawl_single(author: dict) -> int:
            nonlocal total_new
            rem = _remaining_budget()
            if rem is not None and rem <= 0:
                return 0

            username = author.get("username", "Unknown")
            userid = author.get("userid")
            
            if not userid:
                print(f"  [⚠️] [BINANCE] Bỏ qua {username}: thiếu userid")
                return 0
                
            new_count = 0
            since_timestamp = self.get_last_crawl_time(userid=userid, lookback_hours=lookback_hours)
            try:
                posts = self.get_user_timeline(userid, username, since_timestamp, limit=rem)

                if not posts:
                    self.save_last_crawl_time(current_time_ms, userid=userid)
                    return 0

                completed_all = True
                for post in posts:
                    rem_now = _remaining_budget()
                    if rem_now is not None and rem_now <= 0:
                        completed_all = False
                        break

                    pid = post["post_id"]

                    # Download + QR check + Sanitize ảnh
                    local_images = self.download_images(pid, post.get("image_urls", []))

                    if local_images is None:
                        print(f"   [🚫] [BINANCE] Bỏ qua bài {pid} vì chứa mã QR")
                        continue

                    url = f"https://www.binance.com/vi/square/post/{pid}"

                    success = self.db_manager.insert_post_with_time(
                        post_id=pid,
                        url=url,
                        text=post.get("text", ""),
                        image_paths=local_images,
                        crawled_from=post.get("username", username),
                        create_time=post.get("publish_time"),
                        source="binance"
                    )

                    if success:
                        with budget_lock:
                            total_new += 1
                        new_count += 1
                        print(f"   ✓ [BINANCE] Lưu bài {pid}")

                # Nếu đã duyệt hết danh sách bài trả về của author này (không bị cắt bởi limit/budget)
                if completed_all and (rem is None or len(posts) < rem):
                    self.save_last_crawl_time(current_time_ms, userid=userid)

            except Exception as e:
                print(f"  [❌] [BINANCE] Lỗi crawl {username}: {e}")
                
            return new_count

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(crawl_single, author): author for author in authors_list}
            for future in as_completed(futures):
                author = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"  [❌] [BINANCE] Lỗi Thread {author.get('username')}: {e}")

        if max_new_posts is None:
            self.save_last_crawl_time(current_time_ms)

        print(f"\n[BINANCE] ✅ Hoàn tất: {total_new} bài mới")
        return total_new

    def cleanup(self):
        """Cleanup method — không cần làm gì cho endpoint crawler"""
        pass

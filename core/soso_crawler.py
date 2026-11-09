"""
SoSoValue Crawler 
========================================
Crawl bài viết từ SoSoValue Information API.
Sử dụng curl_cffi để bypass Cloudflare.
"""

import os
import json
import re
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from curl_cffi import requests

class SosoValueCrawler:
    STATE_FILE = os.path.join("data", "state_soso.json")

    def __init__(self, db_manager, max_retries: int = 3, retry_delay: int = 5):
        """
        Khởi tạo crawler SoSoValue.

        Args:
            db_manager: DatabaseManager instance
            max_retries: Số lần retry khi gặp lỗi mạng
            retry_delay: Delay giữa các lần retry
        """
        self.db_manager = db_manager
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    # ==============================================================
    # STATE MANAGEMENT
    # ==============================================================
    def get_last_crawl_time(self) -> int:
        """
        Lấy timestamp lần crawl gần nhất (ms).
        Nếu chưa từng crawl → trả về 2 giờ trước.
        """
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("last_crawl_time_ms", 0)
            except Exception:
                pass
        # Lần đầu: lùi 2 tiếng
        return int((datetime.now() - timedelta(hours=2)).timestamp() * 1000)

    def save_last_crawl_time(self, time_ms: int):
        """Lưu timestamp lần crawl hiện tại"""
        try:
            os.makedirs(os.path.dirname(self.STATE_FILE), exist_ok=True)
            with open(self.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump({"last_crawl_time_ms": int(time_ms)}, f)
        except Exception as e:
            print(f"  ⚠ [SOSO] Lỗi lưu state: {e}")

    # ==============================================================
    # TEXT CLEANING
    # ==============================================================
    def clean_tail_tags(self, text: str) -> str:
        """
        Làm sạch thẻ phía sau, ví dụ: \n\n[PANews]
        """
        if not text:
            return ""
        cleaned_text = re.sub(r'(?:\s*\[.*?\])+\s*$', '', text.strip())
        return cleaned_text

    # ==============================================================
    # HTTP REQUEST
    # ==============================================================
    def _fetch_page(self, page_num: int = 1, page_size: int = 20) -> Optional[List[Dict]]:
        api_url = "https://gw.sosovalue.com/contentAndSocial/content/information/v2/findPage"
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "vi",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://sosovalue.com",
            "referer": "https://sosovalue.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        }
        payload = {
            "pageNum": page_num,
            "pageSize": page_size,
            "categoryList": [1, 9],
            "userType": 1,
            "weight": 0.1,
            "sourcePlatIdList": [],
            "isExcludeDuplicate": True,
            "locationType": ""
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                # Sử dụng curl_cffi requests để impersonate chrome
                response = requests.post(api_url, headers=headers, json=payload, impersonate="chrome110", timeout=15)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("code") == 0:
                        return data.get("data", {}).get("list", [])
                    else:
                        print(f"  [⚠️] [SOSO] API code != 0: {data.get('msg')}")
                elif response.status_code >= 500 or response.status_code == 429:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] [SOSO] Lỗi {response.status_code}! Đợi {wait}s...")
                    time.sleep(wait)
                    continue
                else:
                    print(f"  [⚠️] [SOSO] HTTP {response.status_code}")
                    return None
            except Exception as e:
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    time.sleep(wait)
                else:
                    print(f"  [❌] [SOSO] Request thất bại: {e}")
        return None

    # ==============================================================
    # INTEGRATION LOGIC
    # ==============================================================
    def crawl_news(self, max_pages: int = 1, max_new_posts: Optional[int] = None) -> int:
        """
        Lấy các tin tức mới nhất từ hệ thống (sử dụng last_crawl_time).
        Returns số lượng bài viết mới được lưu vào DB.
        """
        if max_new_posts is not None and max_new_posts <= 0:
            return 0
        since_timestamp = self.get_last_crawl_time()
        since_str = datetime.fromtimestamp(since_timestamp / 1000).strftime("%H:%M:%S %d/%m/%Y")
        
        print(f"\n{'─'*60}")
        print(f"[SOSOVALUE] Bắt đầu cào tin mới (từ sau {since_str})")
        print(f"{'─'*60}")

        total_new = 0
        newest_timestamp = since_timestamp
        has_more = True

        for page in range(1, max_pages + 1):
            if not has_more or (max_new_posts is not None and total_new >= max_new_posts):
                break
            
            print(f"  [🕷️] [SOSO] Đang lấy trang {page}...")
            news_list = self._fetch_page(page_num=page, page_size=30)
            
            if not news_list:
                break

            post_ids = []
            valid_posts = []

            for item in news_list:
                # 1. Trích xuất timestamp
                create_time_raw = item.get("createTime", 0)
                try:
                    create_time_ms = int(create_time_raw)
                except ValueError:
                    continue

                if create_time_ms <= since_timestamp:
                    # Chạm ngưỡng bài cũ, có thể không cần lấy các bài sau
                    has_more = False
                    continue
                
                # 2. Xây dựng thông tin
                post_id = item.get("id") or str(create_time_ms)
                post_ids.append(post_id)
                valid_posts.append((post_id, create_time_ms, item))

            if not valid_posts:
                continue

            # Batch check DB chống trùng lặp (phòng hờ DB đã chạy bù trước đó)
            existing_ids = self.db_manager.batch_check_existing_posts(post_ids)
            
            # Xử lý và lưu
            for post_id, create_time_ms, item in valid_posts:
                if max_new_posts is not None and total_new >= max_new_posts:
                    has_more = False
                    break
                if post_id in existing_ids:
                    if create_time_ms > newest_timestamp:
                        newest_timestamp = create_time_ms
                    continue

                title = item.get("title", "Không có tiêu đề")
                content = item.get("content") or item.get("originalContent") or ""
                cleaned_content = self.clean_tail_tags(content)

                # Format text cho Gemini hoặc post
                final_text = f"{title}\n\n{cleaned_content}".strip()
                
                # URL cho database
                source_link = item.get("sourceLink")
                url = source_link if source_link else f"https://sosovalue.com/news/{post_id}"

                crawled_from = f"sosovalue_{item.get('source', '')}".strip("_")

                success = self.db_manager.insert_post_with_time(
                    post_id=post_id,
                    url=url,
                    text=final_text,
                    image_paths=[], # SoSoValue không dùng ảnh ở đây
                    crawled_from=crawled_from,
                    create_time=create_time_ms,
                    source="sosovalue"
                )

                if success:
                    total_new += 1
                    if create_time_ms > newest_timestamp:
                        newest_timestamp = create_time_ms
                    print(f"   ✓ [SOSO] Lưu bài mới (ID: {post_id}): {title[:40]}...")

        # Cất giữ state
        if newest_timestamp > since_timestamp:
            self.save_last_crawl_time(newest_timestamp)
            print(f"  [💾] [SOSO] Cập nhật last_crawl_time thành: {datetime.fromtimestamp(newest_timestamp / 1000).strftime('%H:%M:%S %d/%m/%Y')}")

        print(f"\n[SOSOVALUE] ✅ Hoàn tất: {total_new} tin tức mới")
        return total_new

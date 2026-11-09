"""
KuCoin Crawler 
========================================
Crawl bài viết từ KuCoin News Flash API.
Sử dụng curl_cffi để bypass Cloudflare.
"""

import os
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from curl_cffi import requests

class KucoinCrawler:
    STATE_FILE = os.path.join("data", "state_kucoin.json")

    def __init__(self, db_manager, max_retries: int = 3, retry_delay: int = 5):
        """
        Khởi tạo crawler KuCoin Flash News.

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
        Nếu chưa từng crawl → trả về 2 giờ trước theo cấu hình.
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
            print(f"  ⚠ [KUCOIN] Lỗi lưu state: {e}")

    # ==============================================================
    # HTTP REQUEST
    # ==============================================================
    def _fetch_page(self, page_num: int = 1, page_size: int = 20) -> Optional[List[Dict]]:
        url = "https://www.kucoin.com/_api/seo-content-eco-service/web/article/search"
        headers = {
            "accept": "application/json",
            "accept-language": "vi,vi-VN;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
            "referer": "https://www.kucoin.com/news/news-flash",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "x-site": "global"
        }
        cookies = {
            "lang": "en_US"
        }
        params = {
            "page": page_num,
            "zoneLv2Code": "news-flash",
            "pageSize": page_size,
            "c": "",
            "lang": "en_US"
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                # Sử dụng curl_cffi requests để impersonate chrome
                response = requests.get(
                    url, 
                    params=params, 
                    headers=headers, 
                    cookies=cookies,
                    impersonate="chrome120", 
                    timeout=15
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if str(data.get("code")) == "200":
                        return data.get("items", [])
                    else:
                        print(f"  [⚠️] [KUCOIN] API code != 200: {data.get('msg')}")
                elif response.status_code >= 500 or response.status_code == 429:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] [KUCOIN] Lỗi {response.status_code}! Đợi {wait}s...")
                    time.sleep(wait)
                    continue
                else:
                    print(f"  [⚠️] [KUCOIN] HTTP {response.status_code}")
                    return None
            except Exception as e:
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    time.sleep(wait)
                else:
                    print(f"  [❌] [KUCOIN] Request thất bại: {e}")
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
        print(f"[KUCOIN] Bắt đầu cào tin mới (từ sau {since_str})")
        print(f"{'─'*60}")

        total_new = 0
        newest_timestamp = since_timestamp
        has_more = True

        for page in range(1, max_pages + 1):
            if not has_more or (max_new_posts is not None and total_new >= max_new_posts):
                break
            
            print(f"  [🕷️] [KUCOIN] Đang lấy trang {page}...")
            news_list = self._fetch_page(page_num=page, page_size=20)
            
            if not news_list:
                break

            post_ids = []
            valid_posts = []

            for item in news_list:
                # 1. Trích xuất timestamp
                article = item.get("articleDetail", {}) 
                create_time_ms = item.get("publicAt", article.get("publicAt", 0))
                
                if create_time_ms <= since_timestamp:
                    # Chạm ngưỡng bài cũ, có thể không cần lấy các bài sau
                    has_more = False
                    continue
                
                # 2. Xây dựng thông tin
                detail_url = item.get("detailUrl", "")
                # Format url để làm ID
                post_id = f"kucoin_{create_time_ms}"
                post_ids.append(post_id)
                valid_posts.append((post_id, create_time_ms, item, article, detail_url))

            if not valid_posts:
                continue

            # Batch check DB chống trùng lặp (phòng hờ DB đã chạy bù trước đó)
            existing_ids = self.db_manager.batch_check_existing_posts(post_ids)
            
            # Xử lý và lưu
            for post_id, create_time_ms, item, article, detail_url in valid_posts:
                if max_new_posts is not None and total_new >= max_new_posts:
                    has_more = False
                    break
                if post_id in existing_ids:
                    if create_time_ms > newest_timestamp:
                        newest_timestamp = create_time_ms
                    continue

                title = article.get("title", "Không có tiêu đề")
                ai_summary = article.get("aiSummary", "")

                # Format text cho Gemini hoặc post
                final_text = f"{title}\n\n{ai_summary}".strip()
                if not final_text:
                    continue
                
                # URL cho database
                url = f"https://www.kucoin.com{detail_url}" if detail_url else f"https://www.kucoin.com/news/news-flash?id={post_id}"

                success = self.db_manager.insert_post_with_time(
                    post_id=post_id,
                    url=url,
                    text=final_text,
                    image_paths=[], # KuCoin flash news không dùng ảnh
                    crawled_from="kucoin_news",
                    create_time=create_time_ms,
                    source="KuCoin" # Đặt chung thành KuCoin theo yêu cầu
                )

                if success:
                    total_new += 1
                    if create_time_ms > newest_timestamp:
                        newest_timestamp = create_time_ms
                    print(f"   ✓ [KUCOIN] Lưu bài mới ({title[:40]}...)")

            # Nên sleep giữa các trang
            time.sleep(1)

        # Cất giữ state
        if newest_timestamp > since_timestamp:
            self.save_last_crawl_time(newest_timestamp)
            print(f"  [💾] [KUCOIN] Cập nhật last_crawl_time thành: {datetime.fromtimestamp(newest_timestamp / 1000).strftime('%H:%M:%S %d/%m/%Y')}")

        print(f"\n[KUCOIN] ✅ Hoàn tất: {total_new} tin tức mới")
        return total_new

if __name__ == "__main__":
    from utils.database_manager import DatabaseManager
    db = DatabaseManager()
    crawler = KucoinCrawler(db)
    crawler.crawl_news(max_pages=3)

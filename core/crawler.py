import json
import os
import time
import hashlib
import requests as http_requests  # stdlib requests cho download ảnh (không cần TLS bypass)
from urllib.parse import urlparse
from utils.qr_detector import contains_qr_code
from utils.image_sanitizer import sanitize_image, ensure_sanitized_image

class OKXCrawler:
    """
    Nghiệp vụ Thu thập dữ liệu (Crawling) - V2.
    Hỗ trợ:
    - Retry với Exponential Backoff khi gặp Rate Limit / Server Error
    - Download ảnh về local (data/images/)
    - Lọc bài viết theo thời gian (since_timestamp)
    """
    
    IMAGE_DIR = os.path.join("data", "images")
    
    def __init__(self, api_client, max_retries=3, retry_delay=5):
        self.api = api_client
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Đảm bảo thư mục ảnh tồn tại
        os.makedirs(self.IMAGE_DIR, exist_ok=True)
    
    def get_user_timeline(self, author_id, since_timestamp=None):
        """
        Cào danh sách bài viết từ trang cá nhân của một User cụ thể.
        
        Args:
            author_id: ID của tác giả (Ví dụ: 872870941263867906)
            since_timestamp: Unix timestamp (milliseconds). Chỉ lấy bài có 
                             publishTime > since_timestamp. None = lấy tất cả.
        
        Returns:
            List[dict] hoặc None nếu lỗi sau max_retries lần.
        """
        path = f"/priapi/v5/rubik/public/content/timeline/1?supportCeDeFi=true&queryName={author_id}&type=3&group=USDT"
        
        # ====== RETRY LOOP ======
        for attempt in range(1, self.max_retries + 1):
            try:
                print(f"  [🕷️] Đang cào tác giả {author_id} (lần {attempt}/{self.max_retries})...")
                res = self.api.get(path)
                
                # Rate Limit hoặc Server Error → Retry
                if res.status_code == 429:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] Rate Limit 429! Đợi {wait}s rồi thử lại...")
                    time.sleep(wait)
                    continue
                    
                if res.status_code >= 500:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] Server Error {res.status_code}! Đợi {wait}s rồi thử lại...")
                    time.sleep(wait)
                    continue
                
                if res.status_code != 200:
                    print(f"  [❌] Lỗi HTTP {res.status_code} cho tác giả {author_id}")
                    return None
                
                data = res.json()
                if data.get("code") != "0":
                    error_msg = data.get("msg", "Unknown")
                    # Nếu lỗi liên quan rate limit → retry
                    if "frequent" in error_msg.lower() or "limit" in error_msg.lower():
                        wait = self.retry_delay * (2 ** (attempt - 1))
                        print(f"  [⚠️] API Rate Limit: {error_msg}. Đợi {wait}s...")
                        time.sleep(wait)
                        continue
                    print(f"  [❌] Lỗi API OKX cho {author_id}: code={data.get('code')}, msg={error_msg}")
                    return None
                
                # ====== PARSE DATA ======
                content_list = data.get("data", {}).get("contentDataList", [])
                
                results = []
                skipped_pinned = 0
                skipped_short = 0
                
                for item in content_list:
                    # ====== FILTER 1: Bỏ qua bài GHIM (isPinned=1) ======
                    # Bài ghim luôn nằm đầu timeline dù đã cũ → gây crawl trùng lặp
                    if item.get("isPinned") == 1 or item.get("isPinned") is True:
                        skipped_pinned += 1
                        continue
                    
                    post_id = item.get("contentId")
                    text = item.get("content", "")
                    publish_time = item.get("publishTime", "")
                    
                    # ====== FILTER 2: Bỏ qua bài text quá ngắn ======
                    if not text or len(text.strip()) < 10:
                        skipped_short += 1
                        continue
                    
                    # ====== FILTER 3: Lọc theo thời gian ======
                    if since_timestamp and publish_time:
                        try:
                            pt = int(publish_time)
                            if pt <= since_timestamp:
                                continue  # Bài cũ → skip
                        except (ValueError, TypeError):
                            pass  # Không parse được → giữ lại
                    
                    # Lấy link ảnh gốc
                    image_urls = [img.get("url") for img in item.get("imageList", []) if img.get("url")]
                    
                    results.append({
                        "post_id": post_id,
                        "text": text,
                        "image_urls": image_urls,  # URL gốc, sẽ download sau
                        "publish_time": publish_time,
                        "author_id": author_id
                    })
                
                filter_info = []
                if skipped_pinned:
                    filter_info.append(f"{skipped_pinned} ghim")
                if skipped_short:
                    filter_info.append(f"{skipped_short} quá ngắn")
                filter_str = f" | Bỏ qua: {', '.join(filter_info)}" if filter_info else ""
                
                print(f"  [✅] Tác giả {author_id}: {len(results)} bài mới (API trả: {len(content_list)}{filter_str})")
                return results
                
            except Exception as e:
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    print(f"  [⚠️] Exception khi cào {author_id}: {e}. Retry sau {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  [❌] Thất bại hoàn toàn sau {self.max_retries} lần thử cho {author_id}: {e}")
                    return None
        
        print(f"  [❌] Hết số lần retry cho tác giả {author_id}")
        return None

    def crawl_trending_posts(self, top_n=3, posts_per_topic=10, since_timestamp=None):
        """
        Cào bài viết từ các hashtag thịnh hành nhất.
        """
        print(f"  [🕷️] Đang lấy top {top_n} hashtag thịnh hành...")
        tag_list_url = "/priapi/v5/content/ugc/activityTagList?scene=1"
        res = self.api.get(tag_list_url)
        
        if res.status_code != 200:
            print(f"  [❌] Lỗi lấy danh sách tag: {res.status_code}")
            return []
            
        data = res.json()
        tags = data.get("data", {}).get("activityTagList", [])
        
        if not tags:
            print("  [⚠️] Không tìm thấy hashtag thịnh hành nào.")
            return []
            
        top_tags = tags[:top_n]
        all_results = []
        
        for tag_info in top_tags:
            tag_name = str(tag_info.get("tag", "")).strip().lstrip("#").strip()
            activity_key = tag_info.get("activityKey", "")
            tag_type = tag_info.get("hashTagType", 0) or 0
            deep_link = tag_info.get("deepLinkUrl") or tag_info.get("deeplinkUrl") or ""
            
            if not activity_key or not tag_name:
                continue
                
            print(f"\n  [🕷️] Đang cào hashtag: #{tag_name} (ID: {activity_key})...")
            
            detail_url = f"/priapi/v5/rubik/content/topic/detail/1?id={activity_key}&type=0&group=USDT"
            
            for attempt in range(1, self.max_retries + 1):
                try:
                    res_detail = self.api.get(detail_url)
                    
                    if res_detail.status_code == 429 or res_detail.status_code >= 500:
                        wait = self.retry_delay * (2 ** (attempt - 1))
                        time.sleep(wait)
                        continue
                        
                    if res_detail.status_code != 200:
                        break
                        
                    detail_data = res_detail.json()
                    data_obj = detail_data.get("data", {})
                    
                    list_keys = [k for k, v in data_obj.items() if isinstance(v, list) and len(v) > 0]
                    if not list_keys:
                        break
                        
                    posts = data_obj[list_keys[0]]
                    top_posts = posts[:posts_per_topic]
                    
                    hashtag_metadata = [{
                        "tag": f"#{tag_name} ",
                        "activityKey": activity_key,
                        "position": [1],
                        "type": tag_type,
                    }]
                    if tag_type == 4 and deep_link:
                        hashtag_metadata[0]["deeplinkUrl"] = deep_link
                    
                    skipped_short = 0
                    
                    for item in top_posts:
                        post_id = item.get("id") or item.get("contentId")
                        text = item.get("content") or item.get("textContent") or item.get("text") or ""
                        publish_time = item.get("publishTime", "")
                        
                        if not text or len(text.strip()) < 10:
                            skipped_short += 1
                            continue
                            
                        if since_timestamp and publish_time:
                            try:
                                pt = int(publish_time)
                                if pt <= since_timestamp:
                                    continue
                            except:
                                pass
                                
                        image_urls = [img.get("url") for img in item.get("imageList", []) if img.get("url")]
                        
                        all_results.append({
                            "post_id": post_id,
                            "text": text,
                            "image_urls": image_urls,
                            "publish_time": publish_time,
                            "author_id": item.get("author", {}).get("authorId", ""),
                            "hashtags": json.dumps(hashtag_metadata, ensure_ascii=False)
                        })
                        
                    print(f"  [✅] Hashtag #{tag_name}: thêm {len(top_posts) - skipped_short} bài mới (Bỏ qua: {skipped_short} quá ngắn)")
                    break
                    
                except Exception as e:
                    if attempt < self.max_retries:
                        time.sleep(self.retry_delay * (2 ** (attempt - 1)))
                    else:
                        print(f"  [❌] Lỗi cào hashtag {tag_name}: {e}")
                        
        return all_results

    def download_images(self, post_id, image_urls):
        """
        Download danh sách ảnh về local.
        Tự động phát hiện và loại bỏ bài chứa mã QR.
        
        Args:
            post_id: ID bài viết (dùng làm tên file)
            image_urls: List URL ảnh gốc
            
        Returns:
            List[str]: Danh sách đường dẫn local đã tải thành công
            None: Nếu phát hiện mã QR → bài viết cần bị loại bỏ hoàn toàn
        """
        if not image_urls:
            return []
        
        local_paths = []
        for idx, url in enumerate(image_urls):
            try:
                # Xác định extension từ URL
                parsed = urlparse(url)
                path_part = parsed.path
                ext = os.path.splitext(path_part)[1] or ".jpg"
                # Chỉ giữ extension phổ biến
                if ext.lower() not in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                    ext = ".jpg"
                
                filename = f"{post_id}_{idx}{ext}"
                filepath = os.path.join(self.IMAGE_DIR, filename)
                jpg_filepath = os.path.join(self.IMAGE_DIR, f"{post_id}_{idx}.jpg")
                existing_path = jpg_filepath if os.path.exists(jpg_filepath) else (filepath if os.path.exists(filepath) else None)
                
                # Nếu file đã tồn tại (đã download trước đó) → kiểm tra QR + đảm bảo đã sanitize
                if existing_path:
                    if contains_qr_code(existing_path):
                        print(f"    [🚫] Phát hiện mã QR trong ảnh {existing_path}! Loại bỏ toàn bộ bài {post_id}")
                        try: os.remove(existing_path)
                        except: pass
                        for p in local_paths:
                            try: os.remove(p)
                            except: pass
                        return None
                    sanitized = ensure_sanitized_image(existing_path)
                    if sanitized and sanitized.get("path"):
                        local_paths.append(str(sanitized["path"]))
                    continue
                
                # Download
                with http_requests.get(url, timeout=15, stream=True) as resp:
                    if resp.status_code == 200:
                        with open(filepath, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=8192):
                                f.write(chunk)
                        
                        # ====== QR CODE CHECK (trên ảnh nguyên bản trước khi biến đổi) ======
                        if contains_qr_code(filepath):
                            print(f"    [🚫] Phát hiện mã QR trong ảnh {filepath}! Loại bỏ toàn bộ bài {post_id}")
                            try: os.remove(filepath)
                            except: pass
                            for p in local_paths:
                                try: os.remove(p)
                                except: pass
                            return None
                        
                        # ====== SANITIZE METADATA & ANTI-DUPLICATE EVASION ======
                        sanitized = sanitize_image(filepath, seed=f"{post_id}_{idx}")
                        if sanitized and sanitized.get("path"):
                            local_paths.append(str(sanitized["path"]))
                        else:
                            print(f"    [⚠️] Bỏ qua ảnh lỗi/rỗng của bài {post_id}: {filepath}")
                    else:
                        print(f"    [⚠️] Không tải được ảnh {url}: HTTP {resp.status_code}")
                    
            except Exception as e:
                print(f"    [⚠️] Lỗi download ảnh {url}: {e}")
        
        return local_paths

    def save_to_json(self, data, filename):
        """Lưu dữ liệu cào được ra file JSON (backward compatible)"""
        os.makedirs("data/crawled", exist_ok=True)
        filepath = os.path.join("data/crawled", filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print(f"  [✅] Đã lưu {len(data)} bài viết vào: {filepath}")

import json
import re
import time
import uuid
import os
from core.okx_api import OKXApi
from utils.image_sanitizer import ensure_sanitized_image

TOKEN_CATALOG_PATH = "/priapi/v5/rubik/public/token/getAllOnline"
TOPIC_CATALOG_PATH = "/priapi/v5/content/ugc/activityTagList?scene=1"
CATALOG_CACHE_TTL_SECONDS = 300
CATALOG_CACHE_HEADERS = {"Cache-Strategy": "FIRST_CACHE"}

# App chỉ gắn tag khi sau tag có whitespace (NVK.getHyperlinkPositionsForSymbol).
_TOKEN_PATTERN = re.compile(r"\$([A-Za-z0-9]+)\s")
_TOPIC_PATTERN = re.compile(r"#([A-Za-z0-9_]+)\s")


def _strip_tag_name(value):
    if not isinstance(value, str):
        return ""
    name = value.strip()
    while name[:1] in ("$", "#", "@"):
        name = name[1:].strip()
    return name


def _match_counts(content, pattern):
    counts = {}
    for match in pattern.finditer(content):
        name = match.group(1)
        counts[name] = counts.get(name, 0) + 1
    return counts


def _positions(count):
    return list(range(1, count + 1))


class OKXPublisher:
    """
    Nghiệp vụ Đăng Bài (Business Logic).
    Kết nối tuần tự các API của OKX để hoàn thành quy trình đăng (Presign -> Upload -> Publish)

    Tag được tạo theo đúng contract của app: chỉ tag token/topic tồn tại trong catalog
    server, vị trí 1-based tính bằng pattern "<tag>\\s", HashTag luôn có activityKey.
    """
    def __init__(self, account_data, proxy=None):
        self.api = OKXApi(account_data, proxy=proxy)
        self.account_id = account_data["id"]
        self._catalog_cache = None
        self._catalog_cached_at = 0.0

    def check_server_time(self):
        """Khởi động: Lấy Server Time để ping server"""
        path = "/priapi/v5/public/time"
        res = self.api.get(path)
        data = res.json()
        if data.get("code") == "0":
            ts = data["data"][0]["ts"]
            print(f"[{self.account_id}] Ping OK! Server time: {ts}")
            return True
        print(f"[{self.account_id}] ❌ Lỗi kết nối Server: {data}")
        return False

    def _get_presign_url(self):
        """Lấy URL Upload từ OSS Aliyun"""
        path = "/priapi/v5/content/image/presign"
        ts = str(int(time.time() * 1000))
        req_id = f"{uuid.uuid4().hex}_{ts}"
        
        payload = {"requestId": req_id}
        body = json.dumps(payload, separators=(",", ":"))
        
        res = self.api.post(path, body)
        return res.json()

    def _upload_to_oss(self, presign_url, image_path):
        """Đẩy file vật lý lên OSS thông qua Public API (Không xài auth của OKX)"""
        print(f"[{self.account_id}] Uploading {image_path}...")
        with open(image_path, "rb") as f:
            data = f.read()
        
        # Nhắc nhở Cảnh báo: Tái lập lỗi upload nếu Content-Type != empty 
        # API của OSS OKX yêu cầu KHÔNG ĐƯỢC gửi Content-Type ở endpoint này
        res = self.api.put_raw(presign_url, data, content_type=None)
        
        if res.status_code == 200:
            print(f"[{self.account_id}] ✅ Upload ảnh thành công!")
            return True
            
        print(f"[{self.account_id}] ❌ Lỗi upload: HTTP {res.status_code}")
        return False

    # =========================================================================
    # TAG CATALOG (server-driven)
    # =========================================================================

    def _fetch_token_catalog(self):
        try:
            res = self.api.get(TOKEN_CATALOG_PATH, extra_headers=CATALOG_CACHE_HEADERS)
            if res.status_code != 200:
                print(f"[{self.account_id}] ⚠️ Token catalog HTTP {res.status_code}")
                return {}
            data = res.json()
        except Exception as e:
            print(f"[{self.account_id}] ⚠️ Lỗi lấy token catalog: {e}")
            return {}

        if data.get("code") != "0" or not isinstance(data.get("data"), list):
            print(f"[{self.account_id}] ⚠️ Token catalog không hợp lệ: code={data.get('code')}")
            return {}

        catalog = {}
        for item in data["data"]:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if name:
                catalog[name] = item
        return catalog

    def _fetch_topic_catalog(self):
        try:
            res = self.api.get(TOPIC_CATALOG_PATH, extra_headers=CATALOG_CACHE_HEADERS)
            if res.status_code != 200:
                print(f"[{self.account_id}] ⚠️ Topic catalog HTTP {res.status_code}")
                return {}
            data = res.json()
        except Exception as e:
            print(f"[{self.account_id}] ⚠️ Lỗi lấy topic catalog: {e}")
            return {}

        payload = data.get("data") if data.get("code") == "0" else None
        items = payload.get("activityTagList") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            print(f"[{self.account_id}] ⚠️ Topic catalog không hợp lệ: code={data.get('code')}")
            return {}

        catalog = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = _strip_tag_name(item.get("tag", ""))
            if name:
                catalog[name] = {
                    "activityKey": item.get("activityKey") or "",
                    "type": item.get("hashTagType", 0) or 0,
                    "deeplinkUrl": item.get("deepLinkUrl") or item.get("deeplinkUrl") or "",
                    "linkUrl": item.get("linkUrl") or "",
                }
        return catalog

    def _get_catalogs(self):
        now = time.time()
        if (
            self._catalog_cache is not None
            and now - self._catalog_cached_at < CATALOG_CACHE_TTL_SECONDS
        ):
            return self._catalog_cache

        catalogs = (self._fetch_token_catalog(), self._fetch_topic_catalog())
        self._catalog_cache = catalogs
        self._catalog_cached_at = now
        return catalogs

    # =========================================================================
    # TAG BUILDING
    # =========================================================================

    @staticmethod
    def _build_hash_tag(name, metadata, positions):
        hash_tag_type = int(metadata.get("type") or 0)
        hash_tag = {
            "tag": f"#{name} ",
            "activityKey": metadata.get("activityKey") or "",
            "position": positions,
        }
        if metadata.get("linkUrl"):
            hash_tag["linkUrl"] = metadata["linkUrl"]
        if hash_tag_type == 4 and metadata.get("deeplinkUrl"):
            hash_tag["deeplinkUrl"] = metadata["deeplinkUrl"]
        hash_tag["type"] = hash_tag_type
        if metadata.get("authorId"):
            hash_tag["authorId"] = metadata["authorId"]
        return hash_tag

    def _normalize_trending_hashtags(self, trending_hashtags):
        normalized = []
        seen = set()
        for item in trending_hashtags or []:
            if isinstance(item, str):
                item = {"tag": item}
            if not isinstance(item, dict):
                continue
            name = _strip_tag_name(item.get("tag", ""))
            if not name or name in seen:
                continue
            seen.add(name)
            normalized.append({
                "name": name,
                "activityKey": item.get("activityKey") or "",
                "type": item.get("hashTagType", item.get("type")),
                "deeplinkUrl": item.get("deepLinkUrl") or item.get("deeplinkUrl") or "",
                "linkUrl": item.get("linkUrl") or "",
            })
        return normalized

    def _append_missing_trending(self, content, trending):
        missing = [
            item["name"]
            for item in trending
            if not re.search(r"#" + re.escape(item["name"]) + r"\s", content)
        ]
        if not missing:
            return content
        appended = " ".join(f"#{name} " for name in missing)
        return f"{content}\n\n{appended}"

    def _build_content_tags(self, content, trending):
        token_catalog, topic_catalog = self._get_catalogs()

        cash_tags = []
        for name, count in _match_counts(content, _TOKEN_PATTERN).items():
            if name in token_catalog:
                cash_tags.append({
                    "tag": f"${name} ",
                    "coinName": name,
                    "position": _positions(count),
                })

        hash_tags_by_name = {}
        for name, count in _match_counts(content, _TOPIC_PATTERN).items():
            metadata = topic_catalog.get(name)
            if metadata is None:
                continue
            hash_tags_by_name[name] = self._build_hash_tag(name, metadata, _positions(count))

        for item in trending:
            name = item["name"]
            catalog_metadata = topic_catalog.get(name, {})
            count = len(re.findall(r"#" + re.escape(name) + r"\s", content))
            metadata = {
                "activityKey": item["activityKey"] or catalog_metadata.get("activityKey", ""),
                "type": item["type"] if item["type"] is not None else catalog_metadata.get("type", 0),
                "deeplinkUrl": item["deeplinkUrl"] or catalog_metadata.get("deeplinkUrl", ""),
                "linkUrl": item["linkUrl"] or catalog_metadata.get("linkUrl", ""),
            }
            hash_tags_by_name[name] = self._build_hash_tag(name, metadata, _positions(count))

        content_tags = {}
        if cash_tags:
            content_tags["cashTagList"] = cash_tags
        if hash_tags_by_name:
            content_tags["hashTagList"] = list(hash_tags_by_name.values())
        return content_tags

    def publish(self, content, image_path=None, channel_id="1001", group="USDT", trending_hashtags=None):
        """Nghiệp vụ chính: Đăng bài Square hoàn chỉnh"""
        image_list = []

        trending = self._normalize_trending_hashtags(trending_hashtags)
        if trending:
            content = self._append_missing_trending(content, trending)
        
        # Có ảnh -> Đảm bảo đã xoá sạch metadata & biến đổi chống trùng trước khi Presign & Upload
        img_info = ensure_sanitized_image(image_path) if image_path else None
        if img_info and os.path.exists(str(img_info["path"])):
            clean_image_path = str(img_info["path"])
            presign_res = self._get_presign_url()
            if presign_res.get("code") == "0":
                ps_data = presign_res["data"]
                
                # Upload
                if self._upload_to_oss(ps_data["presignUrl"], clean_image_path):
                    image_list.append({
                        "url": ps_data["imageUrl"],
                        "width": str(img_info["width"]),
                        "height": str(img_info["height"]), 
                        "fileSize": str(img_info["file_size"])
                    })
            else:
                print(f"[{self.account_id}] ❌ Lỗi Presign: {presign_res}")

        # Finalize Publish Process
        path = "/priapi/v5/content/ugc/publish"
        publish_id = str(int(time.time() * 1000))
        
        payload = {
            "publishId": publish_id,
            "content": content,
            "channelId": channel_id,
            "group": group
        }
        
        # Gắn contentTags từ catalog server (token/topic) + trending đã chọn
        content_tags = self._build_content_tags(content, trending)
        if content_tags:
            payload["contentTags"] = content_tags
            tag_summary = []
            if content_tags.get("cashTagList"):
                coins = [t["coinName"] for t in content_tags["cashTagList"]]
                tag_summary.append(f"${', $'.join(coins)}")
            if content_tags.get("hashTagList"):
                tags = [t["tag"].strip() for t in content_tags["hashTagList"]]
                tag_summary.append(", ".join(tags))
            print(f"[{self.account_id}] 🏷️ Tags: {' | '.join(tag_summary)}")
        
        if image_list:
            payload["imageList"] = image_list
            
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        
        res = self.api.post(path, body)
        return res.json()


"""SoSoValue Information Crawler — curl_cffi (bypass Cloudflare), strip HTML, hỗ trợ ảnh + watermark ms.

State lưu atomic vào data/crawler_state.json (sync DB crawl_state), dedup post_id
và content_hash qua BaseCrawler.save_post.
"""
import html
import json
import re
import time
from typing import List, Optional

from infra.logging_config import get_logger
from services.crawler.base import BaseCrawler

logger = get_logger(__name__)

_URL = "https://gw.sosovalue.com/contentAndSocial/content/information/v2/findPage"
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|tr|h[1-6])\b[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_IMAGE_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp|gif)(?:\?|$)", re.IGNORECASE)


class SosoValueCrawler(BaseCrawler):
    source = "sosovalue"

    def __init__(self, db, config=None, image_config=None, proxy_config=None,
                 state_file: Optional[str] = None,
                 max_retries: int = 3, retry_delay: int = 5):
        super().__init__(
            db, config=config, image_config=image_config,
            proxy_config=proxy_config, state_file=state_file,
            max_retries=max_retries, retry_delay=retry_delay,
        )
        cfg = config or {}
        self.page_size = int(cfg.get("page_size", 30))
        self.delay_seconds = float(cfg.get("delay_seconds", 1.0))

    @staticmethod
    def _clean_tail_tags(text: str) -> str:
        if not text:
            return ""
        return re.sub(r"(?:\s*\[.*?\])+\s*$", "", text.strip())

    @staticmethod
    def _clean_html(raw: str) -> str:
        if not raw:
            return ""
        text = html.unescape(str(raw))
        text = _BLOCK_TAG_RE.sub("\n", text)
        text = _ANY_TAG_RE.sub("", text)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
        return "\n".join(ln for ln in lines if ln).strip()

    @classmethod
    def _extract_images(cls, item: dict) -> List[str]:
        raw_imgs: List = []
        cover = item.get("coverPicture")
        if isinstance(cover, str) and cover.strip():
            raw_imgs.append(cover.strip())

        for field in ("mediaInfoItemList", "contentMediaInfo"):
            val = item.get(field)
            if isinstance(val, str) and val.strip().startswith(("[", "{")):
                try:
                    val = json.loads(val)
                except ValueError:
                    val = None
            if not isinstance(val, list):
                continue
            for entry in val:
                if isinstance(entry, str) and _IMAGE_EXT_RE.search(entry):
                    raw_imgs.append(entry)
                elif isinstance(entry, dict):
                    mtype = str(entry.get("type") or entry.get("mediaType") or "").lower()
                    if mtype == "video":
                        continue
                    for k in ("url", "imageUrl", "imgUrl", "picUrl", "originUrl"):
                        u = entry.get(k)
                        if isinstance(u, str) and u.strip():
                            if mtype in ("image", "pic", "photo") or _IMAGE_EXT_RE.search(u):
                                raw_imgs.append(u.strip())
                                break
        return cls._normalize_image_urls(raw_imgs)

    def _fetch_page(self, page_num: int = 1) -> Optional[List[dict]]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "vi",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://sosovalue.com",
            "referer": "https://sosovalue.com/",
        }
        payload = {
            "pageNum": page_num, "pageSize": self.page_size,
            "categoryList": [1, 9], "userType": 1, "weight": 0.1,
            "sourcePlatIdList": [], "isExcludeDuplicate": True, "locationType": "",
        }
        resp = self._cffi_request("POST", _URL, headers=headers, json_body=payload,
                                  impersonate="chrome110")
        if not resp or resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if data.get("code") == 0:
            return (data.get("data") or {}).get("list") or []
        logger.warning("[SOSO] API code != 0: %s", data.get("msg"))
        return None

    def crawl_news(self, max_pages: int = 2, max_new_posts: Optional[int] = None,
                   lookback_hours: float = 2.0) -> int:
        if max_new_posts is not None and max_new_posts <= 0:
            return 0

        current_ms = int(time.time() * 1000)
        since_ms = self.get_last_crawl_time(lookback_hours=lookback_hours)
        cutoff_ms = since_ms - 300_000
        total_new = 0
        fetch_ok = False
        candidates_map = {}

        with self.crawl_run() as stats:
            for page in range(1, max_pages + 1):
                items = self._fetch_page(page_num=page)
                if items is None:
                    break
                fetch_ok = True
                if not items:
                    break

                stop_paging = False
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        create_ms = int(float(item.get("createTime", 0)))
                        if 0 < create_ms < 10**12:
                            create_ms *= 1000
                    except (ValueError, TypeError):
                        continue
                    if create_ms < cutoff_ms:
                        stop_paging = True
                        continue
                    post_id = str(item.get("id") or create_ms)
                    if post_id not in candidates_map:
                        candidates_map[post_id] = (post_id, create_ms, item)

                if stop_paging:
                    break
                if page < max_pages:
                    time.sleep(self.delay_seconds)

            candidates = list(candidates_map.values())
            if candidates:
                existing = self.check_existing_ids([v[0] for v in candidates])
                fresh = [v for v in candidates if v[0] not in existing]
                stats["found"] = len(fresh)
                stats["dup"] = len(candidates) - len(fresh)
                fresh.sort(key=lambda v: v[1], reverse=True)

                for post_id, create_ms, item in fresh:
                    if max_new_posts is not None and total_new >= max_new_posts:
                        break
                    title = self._clean_html(item.get("title") or "")
                    content = self._clean_html(
                        self._clean_tail_tags(item.get("content") or item.get("originalContent") or "")
                    )
                    if title and content and title.lower() not in content.lower()[:len(title) + 20]:
                        text = f"{title}\n\n{content}".strip()
                    else:
                        text = (content or title).strip()
                    if not text:
                        continue

                    img_urls = self._extract_images(item)
                    images = self.process_images(post_id, img_urls) if img_urls else []
                    if images is None:
                        stats["dup"] += 1
                        continue

                    url = item.get("sourceLink") or f"https://sosovalue.com/news/{post_id}"
                    crawled_from = f"sosovalue_{item.get('source', '')}".strip("_")
                    if self.save_post(post_id=post_id, url=url, text=text,
                                      image_paths=images,
                                      create_time=create_ms, crawled_from=crawled_from):
                        total_new += 1
                        stats["new"] += 1
                        if stats["last_item_time"] is None or create_ms > stats["last_item_time"]:
                            stats["last_item_time"] = create_ms
                    else:
                        stats["dup"] += 1

            if fetch_ok:
                self.save_last_crawl_time(current_ms)
        logger.info("[SOSO] hoàn tất: %s tin mới", total_new)
        return total_new

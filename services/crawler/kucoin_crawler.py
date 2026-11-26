"""KuCoin News Flash Crawler — curl_cffi (bypass Cloudflare), strip HTML, ID thật + watermark ms.

State lưu atomic vào data/crawler_state.json (sync DB crawl_state), dedup post_id
và content_hash qua BaseCrawler.save_post.
"""
import html
import re
import time
from typing import List, Optional

from infra.logging_config import get_logger
from services.crawler.base import BaseCrawler

logger = get_logger(__name__)

_URL = "https://www.kucoin.com/_api/seo-content-eco-service/web/article/search"
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|tr|h[1-6])\b[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


class KucoinCrawler(BaseCrawler):
    source = "kucoin"

    def __init__(self, db, config=None, image_config=None, proxy_config=None,
                 state_file: Optional[str] = None,
                 max_retries: int = 3, retry_delay: int = 5):
        super().__init__(
            db, config=config, image_config=image_config,
            proxy_config=proxy_config, state_file=state_file,
            max_retries=max_retries, retry_delay=retry_delay,
        )
        cfg = config or {}
        self.page_size = int(cfg.get("page_size", 20))
        self.delay_seconds = float(cfg.get("delay_seconds", 1.0))

    @staticmethod
    def _clean_html(raw: str) -> str:
        if not raw:
            return ""
        text = html.unescape(str(raw))
        text = _BLOCK_TAG_RE.sub("\n", text)
        text = _ANY_TAG_RE.sub("", text)
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
        cleaned = "\n".join(ln for ln in lines if ln)
        return cleaned.strip()

    def _fetch_page(self, page_num: int = 1) -> Optional[List[dict]]:
        headers = {
            "accept": "application/json",
            "accept-language": "vi,vi-VN;q=0.9,en-US;q=0.6,en;q=0.5",
            "referer": "https://www.kucoin.com/news/news-flash",
            "x-site": "global",
        }
        params = {
            "page": page_num, "zoneLv2Code": "news-flash",
            "pageSize": self.page_size, "c": "", "lang": "en_US",
        }
        resp = self._cffi_request("GET", _URL, headers=headers, params=params,
                                  cookies={"lang": "en_US"}, impersonate="chrome120")
        if not resp or resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if str(data.get("code")) == "200":
            return data.get("items") or []
        logger.warning("[KUCOIN] API code != 200: %s", data.get("msg"))
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
                    article = item.get("articleDetail") or {}
                    create_ms = item.get("publicAt") or article.get("publicAt") or 0
                    try:
                        create_ms = int(float(create_ms))
                        if 0 < create_ms < 10**12:
                            create_ms *= 1000
                    except (ValueError, TypeError):
                        continue
                    if create_ms < cutoff_ms:
                        stop_paging = True
                        continue

                    raw_id = item.get("id") or item.get("contentId") or create_ms
                    post_id = f"kucoin_{raw_id}"
                    if post_id not in candidates_map:
                        candidates_map[post_id] = (post_id, create_ms, item, article)

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

                for post_id, create_ms, item, article in fresh:
                    if max_new_posts is not None and total_new >= max_new_posts:
                        break

                    title = self._clean_html(article.get("title") or "")
                    summary = self._clean_html(article.get("aiSummary") or "")
                    if not summary:
                        summary = self._clean_html(article.get("content") or "")
                    if title and summary and title.lower() not in summary.lower()[:len(title) + 20]:
                        text = f"{title}\n\n{summary}".strip()
                    else:
                        text = (summary or title).strip()
                    if not text:
                        continue

                    # Trích ảnh bài viết nếu có (KHÔNG lấy sourcePicture vì chỉ là logo nguồn tin)
                    raw_imgs: List = []
                    if article.get("mainImg"):
                        raw_imgs.append(article.get("mainImg"))
                    cimgs = article.get("contentImgs")
                    if isinstance(cimgs, list):
                        raw_imgs.extend(cimgs)
                    img_urls = self._normalize_image_urls(raw_imgs)
                    images = self.process_images(post_id, img_urls) if img_urls else []
                    if images is None:
                        stats["dup"] += 1
                        continue

                    detail_url = item.get("detailUrl", "")
                    url = (f"https://www.kucoin.com{detail_url}" if detail_url
                           else f"https://www.kucoin.com/news/news-flash?id={post_id}")
                    if self.save_post(post_id=post_id, url=url, text=text,
                                      image_paths=images,
                                      create_time=create_ms, crawled_from="kucoin_news"):
                        total_new += 1
                        stats["new"] += 1
                        if stats["last_item_time"] is None or create_ms > stats["last_item_time"]:
                            stats["last_item_time"] = create_ms
                    else:
                        stats["dup"] += 1

            if fetch_ok:
                self.save_last_crawl_time(current_ms)
        logger.info("[KUCOIN] hoàn tất: %s tin mới", total_new)
        return total_new

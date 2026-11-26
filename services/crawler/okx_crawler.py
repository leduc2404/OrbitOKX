"""OKX Orbit Crawler — ký request bằng okx/client (hỗ trợ Hashtag/Coin, Trending Topics, Feed & Author).

Endpoints đã kiểm chứng trực tiếp:
  * Hashtag / Coin / Author Timeline: GET /priapi/v5/content/public/timeline/1
      - type=1: bài mới nhất theo coin/hashtag (queryName=BTC, ETH, SOL...)
      - type=2: bài nổi bật / media theo coin/hashtag
      - type=3: bài theo authorId
  * Trending Topics (Hot Hashtags):   GET /priapi/v5/rubik/content/topic/list/1
  * Trending Topic Detail Posts:      GET /priapi/v5/rubik/content/topic/detail/1?id=<topicId>
  * Public Orbit Feed:                GET /priapi/v5/rubik/public/content/feed/1
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from infra.logging_config import get_logger
from services.crawler.base import BaseCrawler

logger = get_logger(__name__)

_TIMELINE_PATH = "/priapi/v5/rubik/public/content/timeline/1"
_PUBLIC_TIMELINE_PATH = "/priapi/v5/content/public/timeline/1"
_TOPIC_LIST_PATH = "/priapi/v5/rubik/content/topic/list/1"
_TOPIC_DETAIL_PATH = "/priapi/v5/rubik/content/topic/detail/1"
_PUBLIC_FEED_PATH = "/priapi/v5/rubik/public/content/feed/1"

DEFAULT_OKX_HASHTAGS = ["BTC", "ETH", "SOL", "OKB", "XRP", "DOGE", "SUI", "PEPE"]


class OkxCrawler(BaseCrawler):
    source = "okx"

    def __init__(self, db, config=None, image_config=None, proxy_config=None,
                 state_file: Optional[str] = None, client=None,
                 max_retries: int = 3, retry_delay: int = 5):
        super().__init__(
            db, config=config, image_config=image_config,
            proxy_config=proxy_config, state_file=state_file,
            max_retries=max_retries, retry_delay=retry_delay,
        )
        cfg = config or {}
        self.delay_seconds = float(cfg.get("delay_seconds", 1.2))
        self.group = cfg.get("group", "USDT")
        self.client = client  # OkxClient đã đăng nhập (bắt buộc để crawl)

    # ------------------------------------------------------------------
    # INTERNAL SIGNED GET + NORMALIZATION
    # ------------------------------------------------------------------
    def _signed_get_json(self, path: str, query: Optional[dict] = None) -> Optional[dict]:
        if self.client is None:
            logger.error("[OKX] chưa có client đăng nhập — bỏ qua crawl")
            return None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.client.signed_request("GET", path, query=query)
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(self.retry_delay * (2 ** (attempt - 1)))
                    continue
                if resp.status_code != 200:
                    logger.error("[OKX] HTTP %s cho %s query=%s", resp.status_code, path, query)
                    return None
                payload = resp.json()
                if str(payload.get("code")) != "0":
                    msg = payload.get("msg", "")
                    if "frequent" in msg.lower() or "limit" in msg.lower():
                        time.sleep(self.retry_delay * (2 ** (attempt - 1)))
                        continue
                    logger.error("[OKX] API code=%s msg=%s (%s)", payload.get("code"), msg, path)
                    return None
                data = payload.get("data")
                return data if isinstance(data, dict) else {}
            except Exception as e:
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * (2 ** (attempt - 1)))
                else:
                    logger.error("[OKX] thất bại sau %s lần cho %s: %s", self.max_retries, path, e)
                    return None
        return None

    @staticmethod
    def _parse_content_items(content_list: List[dict], since_ms: Optional[int] = None,
                             default_author: str = "", default_tag: str = "") -> List[dict]:
        results: List[dict] = []
        for raw_item in (content_list or []):
            if not isinstance(raw_item, dict):
                continue
            item = raw_item.get("contentData") if isinstance(raw_item.get("contentData"), dict) else raw_item
            if item.get("isPinned") in (1, True):
                continue
            post_id = item.get("contentId")
            title = (item.get("title") or "").strip()
            body = (item.get("content") or "").strip()
            if title and body and title.lower() not in body.lower()[:len(title) + 20]:
                text = f"{title}\n\n{body}"
            else:
                text = body or title
            if not post_id or not text or len(text.strip()) < 10:
                continue

            pt = None
            publish_time = item.get("publishTime")
            if publish_time:
                try:
                    val = int(float(publish_time))
                    pt = val * 1000 if val < 10**12 else val
                except (ValueError, TypeError):
                    pt = None
            if since_ms and pt is not None and pt < since_ms:
                continue

            author_obj = item.get("author") if isinstance(item.get("author"), dict) else {}
            author_name = (
                author_obj.get("nickName")
                or item.get("nickName")
                or default_author
                or "okx_orbit"
            )
            author_id = str(author_obj.get("authorId") or default_author or "")

            tags: List[str] = []
            if default_tag:
                tags.append(default_tag.lstrip("#$").upper())
            ctags = item.get("contentTags") or {}
            if isinstance(ctags, dict):
                for ct in (ctags.get("cashTagList") or []):
                    cn = (ct.get("coinName") or ct.get("tag") or "").lstrip("#$").strip().upper()
                    if cn and cn not in tags:
                        tags.append(cn)
                for ht in (ctags.get("topicList") or ctags.get("hashTagList") or []):
                    tn = (ht.get("name") or ht.get("title") or "").lstrip("#$").strip()
                    if tn and tn not in tags:
                        tags.append(tn)

            image_urls = BaseCrawler._normalize_image_urls(item.get("imageList") or [])
            results.append({
                "post_id": str(post_id),
                "text": text,
                "image_urls": image_urls,
                "publish_time": pt,
                "author_id": author_id,
                "author_name": str(author_name),
                "hashtags": ",".join(tags),
            })
        return results

    # ------------------------------------------------------------------
    # TRENDING TOPICS / HASHTAGS / PUBLIC FEED / AUTHOR TIMELINE
    # ------------------------------------------------------------------
    def get_trending_topics(self) -> List[Dict]:
        """Lấy danh sách Trending Topics (Hot Hashtags) hiện tại trên OKX Orbit."""
        data = self._signed_get_json(_TOPIC_LIST_PATH, {"group": self.group})
        if not isinstance(data, dict):
            return []
        out: List[Dict] = []
        for tp in (data.get("contentTopicList") or []):
            if not isinstance(tp, dict) or not tp.get("id"):
                continue
            out.append({
                "id": str(tp["id"]),
                "en_title": str(tp.get("enTitle") or "").strip(),
                "title": str(tp.get("title") or "").strip(),
                "sub_text": str(tp.get("subText") or "").strip(),
                "rank": tp.get("rank"),
            })
        return out

    def _get_hashtag_timeline_checked(
        self,
        hashtag: str,
        *,
        timeline_type: str = "1",
        since_ms: Optional[int] = None,
        max_pages: int = 2,
        limit: Optional[int] = None,
    ) -> Tuple[bool, List[dict]]:
        clean_tag = str(hashtag or "").strip().lstrip("#$").upper()
        if not clean_tag:
            return (False, [])

        all_posts: List[dict] = []
        seen_ids = set()
        cursor = None
        fetch_ok = False

        for _ in range(max(1, max_pages)):
            query = {
                "supportCeDeFi": "true",
                "queryName": clean_tag,
                "type": str(timeline_type or "1"),
                "group": self.group,
            }
            if cursor:
                query["cursor"] = cursor
            data = self._signed_get_json(_PUBLIC_TIMELINE_PATH, query)
            if data is None:
                break
            fetch_ok = True
            content_list = data.get("contentDataList") or []
            if not content_list:
                break
            parsed = self._parse_content_items(content_list, since_ms=since_ms, default_tag=clean_tag)
            for p in parsed:
                if p["post_id"] not in seen_ids:
                    seen_ids.add(p["post_id"])
                    all_posts.append(p)
            if limit is not None and len(all_posts) >= limit:
                return (fetch_ok, all_posts[:limit])
            next_cursor = data.get("nextCursor")
            if not next_cursor or str(next_cursor) == "0" or next_cursor == cursor:
                break
            cursor = str(next_cursor)
            time.sleep(min(self.delay_seconds, 0.6))

        return (fetch_ok, all_posts)

    def get_hashtag_timeline(self, hashtag: str, *, timeline_type: str = "1",
                             since_ms: Optional[int] = None,
                             max_pages: int = 2,
                             limit: Optional[int] = None) -> List[dict]:
        """Lấy danh sách bài viết theo hashtag/coin (VD: BTC, ETH, SOL) trên OKX Orbit."""
        _, posts = self._get_hashtag_timeline_checked(
            hashtag, timeline_type=timeline_type, since_ms=since_ms,
            max_pages=max_pages, limit=limit,
        )
        return posts

    def _get_topic_posts_checked(
        self,
        topic_id: str,
        *,
        topic_label: str = "",
        since_ms: Optional[int] = None,
        size: int = 20,
    ) -> Tuple[bool, List[dict]]:
        query = {
            "id": str(topic_id),
            "group": self.group,
            "size": str(max(5, min(50, int(size)))),
            "supportCeDeFi": "true",
        }
        data = self._signed_get_json(_TOPIC_DETAIL_PATH, query)
        if data is None:
            return (False, [])
        content_list = data.get("contentDataList") or []
        return (True, self._parse_content_items(
            content_list,
            since_ms=since_ms,
            default_tag=topic_label or str(topic_id),
        ))

    def get_topic_posts(self, topic_id: str, *, topic_label: str = "",
                        since_ms: Optional[int] = None, size: int = 20) -> List[dict]:
        """Lấy bài viết thuộc một Trending Topic ID trên OKX Orbit."""
        _, posts = self._get_topic_posts_checked(
            topic_id, topic_label=topic_label, since_ms=since_ms, size=size,
        )
        return posts

    def _get_public_feed_checked(self, *, since_ms: Optional[int] = None) -> Tuple[bool, List[dict]]:
        data = self._signed_get_json(_PUBLIC_FEED_PATH, {
            "group": self.group,
            "supportCeDeFi": "true",
        })
        if data is None:
            return (False, [])
        data_list = data.get("dataList") or data.get("contentDataList") or []
        return (True, self._parse_content_items(data_list, since_ms=since_ms, default_author="okx_feed"))

    def get_public_feed(self, *, since_ms: Optional[int] = None) -> List[dict]:
        """Lấy bài viết từ OKX Orbit Public Feed tổng hợp."""
        _, posts = self._get_public_feed_checked(since_ms=since_ms)
        return posts

    def _get_user_timeline_checked(self, author_id: str,
                                   since_ms: Optional[int] = None) -> Tuple[bool, List[dict]]:
        query = {
            "supportCeDeFi": "true",
            "queryName": str(author_id),
            "type": "3",
            "group": self.group,
        }
        data = self._signed_get_json(_PUBLIC_TIMELINE_PATH, query)
        if data is None or not (data.get("contentDataList") or []):
            alt = self._signed_get_json(_TIMELINE_PATH, query)
            if alt is not None:
                data = alt
        if data is None:
            return (False, [])
        return (True, self._parse_content_items(
            data.get("contentDataList") or [],
            since_ms=since_ms,
            default_author=str(author_id),
        ))

    def get_user_timeline(self, author_id: str, since_ms: Optional[int] = None) -> List[dict]:
        _, posts = self._get_user_timeline_checked(author_id, since_ms=since_ms)
        return posts

    def _save_parsed_posts(self, posts: List[dict], *, crawled_from_prefix: str = "",
                           max_new: Optional[int] = None, stats: dict) -> int:
        if not posts:
            return 0
        existing = self.check_existing_ids([p["post_id"] for p in posts])
        fresh = [p for p in posts if p["post_id"] not in existing]
        stats["found"] += len(fresh)
        stats["dup"] += len(posts) - len(fresh)
        fresh.sort(key=lambda p: p.get("publish_time") or 0, reverse=True)

        saved_count = 0
        for post in fresh:
            if max_new is not None and saved_count >= max_new:
                break
            pid = post["post_id"]
            images = self.process_images(pid, post.get("image_urls", []))
            if images is None:
                stats["dup"] += 1
                continue
            url = f"https://www.okx.com/vi/orbit/post/{pid}"
            author_label = post.get("_label") or crawled_from_prefix or post.get("author_name") or "okx_orbit"
            if self.save_post(
                post_id=pid,
                url=url,
                text=post.get("text", ""),
                image_paths=images,
                create_time=post.get("publish_time"),
                crawled_from=author_label,
                hashtags=post.get("hashtags", ""),
            ):
                saved_count += 1
                stats["new"] += 1
                pt = post.get("publish_time")
                if pt and (stats["last_item_time"] is None or pt > stats["last_item_time"]):
                    stats["last_item_time"] = pt
            else:
                stats["dup"] += 1
        return saved_count

    def crawl_hashtags(self, hashtags: List[str], *, timeline_type: str = "1",
                       max_pages: int = 2,
                       max_new_posts: Optional[int] = None,
                       lookback_hours: float = 2.0,
                       include_trending: bool = False,
                       include_feed: bool = False) -> int:
        """Crawl bài viết OKX Orbit theo danh sách hashtag/coin, trending topics hoặc public feed."""
        if self.client is None:
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0

        current_ms = int(time.time() * 1000)
        tags = [str(t).strip().lstrip("#$") for t in (hashtags or []) if str(t).strip()]
        merged: Dict[str, dict] = {}
        ok_subkeys: List[str] = []

        with self.crawl_run(author_id=f"hashtags:{','.join(tags[:5]) or 'feed'}") as stats:
            # 1. Crawl từng hashtag / coin với watermark subkey độc lập
            for tag in tags:
                if tag.isdigit() and len(tag) >= 10:
                    sk = f"topic:{tag}"
                    since_ms = self.get_last_crawl_time(lookback_hours=lookback_hours, subkey=sk)
                    cutoff_ms = since_ms - 300_000
                    ok_fetch, posts = self._get_topic_posts_checked(
                        tag, since_ms=cutoff_ms, size=max(10, max_new_posts or 20),
                    )
                    label = f"okx:topic:{tag}"
                else:
                    sk = f"hashtag:{tag.upper()}"
                    since_ms = self.get_last_crawl_time(lookback_hours=lookback_hours, subkey=sk)
                    cutoff_ms = since_ms - 300_000
                    ok_fetch, posts = self._get_hashtag_timeline_checked(
                        tag,
                        timeline_type=timeline_type,
                        since_ms=cutoff_ms,
                        max_pages=max_pages,
                        limit=(max_new_posts * 2) if max_new_posts is not None else None,
                    )
                    label = f"okx:#{tag.upper()}"

                if ok_fetch:
                    ok_subkeys.append(sk)
                for p in posts:
                    if p["post_id"] not in merged:
                        p["_label"] = label
                        merged[p["post_id"]] = p
                time.sleep(min(self.delay_seconds, 0.5))

            # 2. Crawl Trending Topics (Hot Hashtags) nếu được bật
            if include_trending:
                sk_trend = "trending"
                since_ms = self.get_last_crawl_time(lookback_hours=lookback_hours, subkey=sk_trend)
                cutoff_ms = since_ms - 300_000
                topics = self.get_trending_topics()
                trend_ok = False
                for tp in topics[:3]:
                    ok_tp, posts = self._get_topic_posts_checked(
                        tp["id"],
                        topic_label=tp.get("en_title") or tp["id"],
                        since_ms=cutoff_ms,
                        size=max(10, max_new_posts or 15),
                    )
                    if ok_tp:
                        trend_ok = True
                    lbl = f"okx:#{tp['en_title']}" if tp.get("en_title") else f"okx:topic:{tp['id']}"
                    for p in posts:
                        if p["post_id"] not in merged:
                            p["_label"] = lbl
                            merged[p["post_id"]] = p
                    time.sleep(min(self.delay_seconds, 0.5))
                if trend_ok:
                    ok_subkeys.append(sk_trend)

            # 3. Crawl Public Feed tổng hợp nếu được bật
            if include_feed:
                sk_feed = "feed"
                since_ms = self.get_last_crawl_time(lookback_hours=lookback_hours, subkey=sk_feed)
                cutoff_ms = since_ms - 300_000
                ok_feed, posts = self._get_public_feed_checked(since_ms=cutoff_ms)
                if ok_feed:
                    ok_subkeys.append(sk_feed)
                for p in posts:
                    if p["post_id"] not in merged:
                        p["_label"] = "okx:feed"
                        merged[p["post_id"]] = p

            all_posts = list(merged.values())
            total_new = self._save_parsed_posts(
                all_posts,
                max_new=max_new_posts,
                stats=stats,
            )

            if ok_subkeys:
                for sk in ok_subkeys:
                    self.save_last_crawl_time(current_ms, subkey=sk)
                self.save_last_crawl_time(current_ms)

        with_img = sum(1 for p in all_posts if p.get("image_urls"))
        logger.info("[OKX] crawl_hashtags hoàn tất: %d bài mới (candidates=%d, có_ảnh=%d, tags=%s)",
                    total_new, len(all_posts), with_img, tags)
        return total_new

    def crawl_all_users(self, authors: List[dict], max_workers: int = 2,
                        max_new_posts: Optional[int] = None,
                        lookback_hours: float = 2.0) -> int:
        if not authors or self.client is None:
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0

        current_ms = int(time.time() * 1000)
        authors_list = list(authors)
        random.shuffle(authors_list)
        total_new = 0
        budget_lock = threading.Lock()

        def remaining() -> Optional[int]:
            if max_new_posts is None:
                return None
            with budget_lock:
                return max(0, max_new_posts - total_new)

        def crawl_single(author: dict) -> int:
            nonlocal total_new
            rem = remaining()
            if rem is not None and rem <= 0:
                return 0
            author_id = author.get("userid") or author.get("author_id")
            username = author.get("username", str(author_id))
            if not author_id:
                return 0
            since_ms = self.get_last_crawl_time(author_id=str(author_id), lookback_hours=lookback_hours)
            cutoff_ms = since_ms - 300_000
            local_new = 0
            with self.crawl_run(author_id=str(author_id)) as stats:
                ok_fetch, posts = self._get_user_timeline_checked(str(author_id), cutoff_ms)
                if not ok_fetch:
                    return 0
                if not posts:
                    self.save_last_crawl_time(current_ms, author_id=str(author_id))
                    return 0

                # Check trùng ID TRƯỚC khi process_images để tránh tải ảnh thừa
                existing = self.check_existing_ids([p["post_id"] for p in posts])
                fresh = [p for p in posts if p["post_id"] not in existing]
                stats["found"] = len(fresh)
                stats["dup"] = len(posts) - len(fresh)
                fresh.sort(key=lambda p: p.get("publish_time") or 0, reverse=True)
                if rem is not None:
                    fresh = fresh[:rem]

                for post in fresh:
                    r = remaining()
                    if r is not None and r <= 0:
                        break
                    pid = post["post_id"]
                    images = self.process_images(pid, post.get("image_urls", []))
                    if images is None:
                        stats["dup"] += 1
                        continue
                    url = f"https://www.okx.com/vi/orbit/post/{pid}"
                    if self.save_post(post_id=pid, url=url, text=post.get("text", ""),
                                      image_paths=images, create_time=post.get("publish_time"),
                                      crawled_from=username, hashtags=post.get("hashtags", "")):
                        local_new += 1
                        stats["new"] += 1
                        pt = post.get("publish_time")
                        if pt and (stats["last_item_time"] is None or pt > stats["last_item_time"]):
                            stats["last_item_time"] = pt
                    else:
                        stats["dup"] += 1
                    time.sleep(self.delay_seconds)
                self.save_last_crawl_time(current_ms, author_id=str(author_id))
            with budget_lock:
                total_new += local_new
            return local_new

        logger.info("[OKX] crawling %s authors (budget=%s)", len(authors_list), max_new_posts)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(crawl_single, a) for a in authors_list]
            for _ in as_completed(futures):
                pass
        logger.info("[OKX] hoàn tất: %s bài mới", total_new)
        return total_new

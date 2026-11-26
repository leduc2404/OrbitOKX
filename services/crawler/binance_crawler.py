"""Binance Square Crawler — crawl theo danh sách authors (chuẩn BinanceSquare).

Kiến trúc chuẩn hoá từ D:\\OKX2\\BinanceSquare\\services\\crawler\\endpoint_crawler.py:
  * Load danh sách authors từ config/crawl.yaml (hỗ trợ format authors: [...] hoặc G1..G4).
  * Endpoint v2: /bapi/composite/v2/friendly/pgc/content/queryUserProfilePageContentsWithFilter
    + /bapi/composite/v2/friendly/pgc/special/content/detail/{pid}.
  * Quản lý mốc thời gian crawl dạng mili-giây (ms) lưu atomic vào data/crawler_state.json
    (cutoff_ms = last_crawl_time_ms - 300_000, buffer 5 phút chống lọt bài).
  * Gom ảnh từ imageList + imageMetaList + cover + coverMeta, chạy pipeline ảnh
    (download -> QR check -> watermark -> sanitize), ưu tiên bài mới nhất trước.
"""
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml
from curl_cffi import requests as cffi

from infra.logging_config import get_logger
from services.crawler.base import BaseCrawler

logger = get_logger(__name__)

_LIST_URL = (
    "https://www.binance.com/bapi/composite/v2/friendly/pgc/content/"
    "queryUserProfilePageContentsWithFilter"
)
_DETAIL_URL = (
    "https://www.binance.com/bapi/composite/v2/friendly/pgc/special/content/detail/{pid}"
)
_PROFILE_URL = "https://www.binance.com/vi/square/profile/{username}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BinanceCrawler(BaseCrawler):
    source = "binance"

    def __init__(self, db, config=None, image_config=None, proxy_config=None,
                 state_file: Optional[str] = None,
                 max_retries: int = 3, retry_delay: int = 3):
        super().__init__(
            db, config=config, image_config=image_config,
            proxy_config=proxy_config, state_file=state_file,
            max_retries=max_retries, retry_delay=retry_delay,
        )
        cfg = config or {}
        self.delay_seconds = float(cfg.get("delay_seconds", 0.3))
        self.authors_file = str(cfg.get("authors_file") or "config/crawl.yaml")
        self.default_lookback_minutes = float(cfg.get("default_lookback_minutes", 30))
        self.cookie = cfg.get("cookie")
        self._yaml_lock = threading.Lock()

    # ------------------------------------------------------------------
    # HTTP (Proxy immediate-fallback -> Direct + curl_cffi fallback)
    # ------------------------------------------------------------------
    def _request(self, url: str, headers: dict, params: Optional[dict] = None,
                 timeout: int = 15):
        proxy_url = self._get_random_proxy()
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
            try:
                r = requests.get(url, headers=headers, params=params,
                                 proxies=proxies, timeout=self.proxy_timeout)
                if r.status_code == 200:
                    return r
                if r.status_code in (429, 503):
                    self._mark_proxy_failed(proxy_url)
            except Exception:
                self._mark_proxy_failed(proxy_url)

        for attempt in range(1, self.max_retries + 1):
            r = None
            try:
                r = requests.get(url, headers=headers, params=params, timeout=timeout)
                if r.status_code == 403:
                    r = cffi.get(url, headers=headers, params=params,
                                 impersonate="chrome120", timeout=timeout)
            except Exception:
                try:
                    r = cffi.get(url, headers=headers, params=params,
                                 impersonate="chrome120", timeout=timeout)
                except Exception as e:
                    r = None
                    if attempt >= self.max_retries:
                        logger.error("[BINANCE] request thất bại %s: %s", url, e)
                        return None

            if r is not None:
                if r.status_code == 200:
                    return r
                if r.status_code != 429 and r.status_code < 500:
                    return r
                logger.warning("[BINANCE] HTTP %s cho %s (lần %d/%d)",
                               r.status_code, url, attempt, self.max_retries)

            if attempt < self.max_retries:
                wait = min(10.0, self.retry_delay * (2 ** (attempt - 1)))
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HELPERS: Text, Timestamps, Pinned, Authors YAML
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_text(raw_text: str) -> str:
        if not raw_text:
            return ""
        text = re.sub(r"\s+", " ", raw_text).strip()
        text = re.sub(r"\{.*?\}\((.*?)\)", r"\1", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _normalize_ts_ms(raw_val) -> Optional[int]:
        if raw_val is None or raw_val == "":
            return None
        try:
            val = int(float(raw_val))
            if val <= 0:
                return None
            return val * 1000 if val < 10**12 else val
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_pinned(item: dict) -> bool:
        if not isinstance(item, dict):
            return False
        if item.get("isStickyToTop"):
            return True
        detail = item.get("contentDetail") or {}
        return bool(isinstance(detail, dict) and detail.get("isStickyToTop"))

    def _resolve_authors_path(self) -> Optional[Path]:
        if not self.authors_file:
            return None
        p = Path(self.authors_file)
        if p.exists():
            return p
        project_root = Path(__file__).resolve().parents[2]
        alt = project_root / self.authors_file
        if alt.exists():
            return alt
        return None

    def load_authors(self) -> List[dict]:
        """Đọc danh sách authors từ config/crawl.yaml + authors inline, dedup theo userid."""
        raw_entries: List[dict] = []
        authors_path = self._resolve_authors_path()
        if authors_path is not None:
            try:
                with open(authors_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, list):
                    raw_entries.extend(data)
                elif isinstance(data, dict):
                    if isinstance(data.get("authors"), list):
                        raw_entries.extend(data["authors"])
                    else:
                        for val in data.values():
                            if isinstance(val, list):
                                raw_entries.extend(val)
            except Exception as e:
                logger.warning("[BINANCE] lỗi đọc %s: %s", authors_path, e)

        inline = self.config.get("authors") or []
        if isinstance(inline, list):
            raw_entries.extend(inline)

        dedup: Dict[str, dict] = {}
        for item in raw_entries:
            if isinstance(item, str):
                tok = item.strip()
                if not tok:
                    continue
                entry = {"username": tok, "userid": tok, "weight": 5, "priority": True}
            elif isinstance(item, dict):
                uid = str(item.get("userid") or "").strip()
                uname = str(item.get("username") or uid).strip()
                if not uid and not uname:
                    continue
                try:
                    w = int(item.get("weight", 5))
                except (ValueError, TypeError):
                    w = 5
                entry = {
                    "username": uname or uid,
                    "userid": uid,
                    "weight": max(1, w),
                    "priority": bool(item.get("priority", True)),
                }
            else:
                continue

            key = entry["userid"] or f"uname:{entry['username'].lower()}"
            prev = dedup.get(key)
            if prev is None or entry["weight"] > prev["weight"]:
                dedup[key] = entry

        return list(dedup.values())

    def _get_uid_from_username(self, username: str) -> Optional[str]:
        if not username:
            return None
        headers = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        r = self._request(_PROFILE_URL.format(username=username), headers)
        if not r or r.status_code != 200:
            return None
        m = re.search(r"targetSquareUid=([a-zA-Z0-9_-]+)", r.text)
        return m.group(1) if m else None

    def _save_resolved_userid(self, username: str, userid: str) -> None:
        authors_path = self._resolve_authors_path()
        if authors_path is None:
            return
        with self._yaml_lock:
            try:
                with open(authors_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                updated = False
                targets = []
                if isinstance(data, list):
                    targets.append(data)
                elif isinstance(data, dict):
                    for v in data.values():
                        if isinstance(v, list):
                            targets.append(v)
                for lst in targets:
                    for item in lst:
                        if (isinstance(item, dict)
                                and str(item.get("username", "")).lower() == username.lower()
                                and not item.get("userid")):
                            item["userid"] = userid
                            updated = True
                if updated:
                    with open(authors_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False,
                                       default_flow_style=False)
            except Exception as e:
                logger.warning("[BINANCE] không lưu được userid cho %s: %s", username, e)

    # ------------------------------------------------------------------
    # FETCH AUTHOR POST IDS & POST DETAIL
    # ------------------------------------------------------------------
    def _get_post_ids(
        self,
        userid: str,
        cutoff_ms: int,
        limit: int = 20,
        ignore_state: bool = False,
    ) -> Tuple[bool, List[Tuple[str, Optional[int]]]]:
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Clienttype": "web",
            "Referer": f"https://www.binance.com/vi/square/profile/{userid}",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        page_size = max(20, int(limit or 20))
        params = {
            "targetSquareUid": userid,
            "timeOffset": "-1",
            "filterType": "ALL",
            "pageSize": page_size,
        }
        r = self._request(_LIST_URL, headers, params)
        if not r or r.status_code != 200:
            return (False, [])
        try:
            payload = r.json()
        except ValueError:
            return (False, [])

        data = payload.get("data")
        if not isinstance(data, dict):
            return (False, [])
        contents = data.get("contents") or []
        now_ms = int(time.time() * 1000)
        results: List[Tuple[str, Optional[int]]] = []

        for item in contents:
            if not isinstance(item, dict):
                continue
            detail_obj = item.get("contentDetail") or {}
            pid = (
                item.get("id")
                or item.get("postId")
                or (detail_obj.get("contentId") if isinstance(detail_obj, dict) else None)
            )
            if not pid:
                continue

            create_ms = self._normalize_ts_ms(item.get("createTime"))
            is_pinned = self._is_pinned(item)

            if ignore_state:
                if is_pinned and create_ms is not None and (now_ms - create_ms > 172_800_000):
                    continue
            else:
                if create_ms is not None:
                    if create_ms < cutoff_ms:
                        continue
                elif is_pinned:
                    continue

            results.append((str(pid), create_ms))
            if limit and len(results) >= limit:
                break

        return (True, results)

    def _crawl_post_detail(self, post_id: str) -> Optional[dict]:
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Clienttype": "web",
            "Referer": f"https://www.binance.com/vi/square/post/{post_id}",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        r = self._request(_DETAIL_URL.format(pid=post_id), headers)
        if not r or r.status_code != 200:
            return None
        try:
            data = (r.json().get("data") or {})
        except (ValueError, AttributeError):
            return None
        pid = data.get("id")
        if not pid:
            return None
        text = self._clean_text(data.get("bodyTextOnly", ""))
        if not text or len(text.strip()) < 10:
            return None

        create_time = self._normalize_ts_ms(data.get("createTime"))

        # Gom đầy đủ nguồn ảnh: imageList + imageMetaList + cover + coverMeta
        raw_imgs: List = []
        for field in ("imageList", "imageMetaList"):
            val = data.get(field)
            if isinstance(val, list):
                raw_imgs.extend(val)
        if data.get("cover"):
            raw_imgs.append(data.get("cover"))
        cover_meta = data.get("coverMeta")
        if isinstance(cover_meta, dict) and cover_meta.get("url"):
            raw_imgs.append(cover_meta.get("url"))
        image_urls = self._normalize_image_urls(raw_imgs)

        tags: List[str] = []
        for ht in (data.get("hashtagList") or []):
            if isinstance(ht, str):
                t = ht.strip().lstrip("#$")
            elif isinstance(ht, dict):
                t = str(ht.get("name") or ht.get("tag") or ht.get("title") or "").strip().lstrip("#$")
            else:
                t = ""
            if t and t not in tags:
                tags.append(t)

        return {
            "post_id": str(pid),
            "text": text,
            "image_urls": image_urls,
            "publish_time": create_time,
            "hashtags": ",".join(tags),
        }

    def get_user_timeline(self, userid: str, username: str, since_ms: int,
                          limit: Optional[int] = None,
                          ignore_state: bool = False) -> List[dict]:
        cutoff_ms = int(since_ms) - 300_000
        ok, id_pairs = self._get_post_ids(
            userid, cutoff_ms, limit=int(limit or 20), ignore_state=ignore_state,
        )
        if not ok or not id_pairs:
            return []
        existing = self.check_existing_ids([p[0] for p in id_pairs])
        new_ids = [p[0] for p in id_pairs if p[0] not in existing]
        if limit is not None:
            new_ids = new_ids[:limit]
        if not new_ids:
            return []
        results = []
        for pid in new_ids:
            detail = self._crawl_post_detail(pid)
            if detail:
                detail["author_id"] = userid
                detail["username"] = username
                results.append(detail)
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
        return results

    # ------------------------------------------------------------------
    # ORCHESTRATION (chuẩn BinanceSquare: scan song song -> sort mới nhất -> lưu state ms)
    # ------------------------------------------------------------------
    def crawl_all_users(
        self,
        authors: Optional[List[dict]] = None,
        max_workers: Optional[int] = None,
        max_new_posts: Optional[int] = None,
        lookback_hours: Optional[float] = None,
        ignore_state: bool = False,
    ) -> int:
        authors_list = list(authors) if authors is not None else self.load_authors()
        if not authors_list:
            logger.warning("[BINANCE] Không có authors nào trong config/crawl.yaml hoặc config inline")
            return 0
        if max_new_posts is not None and max_new_posts <= 0:
            return 0

        if lookback_hours is None:
            lookback_hours = self.default_lookback_minutes / 60.0

        current_ms = int(time.time() * 1000)
        last_crawl_ms = self.get_last_crawl_time(lookback_hours=lookback_hours)
        cutoff_ms = last_crawl_ms - 300_000

        last_str = datetime.fromtimestamp(last_crawl_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
        cutoff_str = datetime.fromtimestamp(cutoff_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            "[BINANCE] Bắt đầu crawl %d authors | last_crawl=%d (%s) | cutoff=%d (%s) | ignore_state=%s",
            len(authors_list), last_crawl_ms, last_str, cutoff_ms, cutoff_str, ignore_state,
        )

        # Xáo trộn nhẹ để tránh luôn đập cùng thứ tự đầu vào nếu bị rate limit
        random.shuffle(authors_list)
        scan_workers = max(1, min(
            int(max_workers or self.config.get("max_workers", 10)),
            len(authors_list),
        ))

        def scan_author(author: dict):
            uid = str(author.get("userid") or "").strip()
            uname = str(author.get("username") or uid or "Unknown").strip()
            if not uid:
                uid = self._get_uid_from_username(uname) or ""
                if uid:
                    self._save_resolved_userid(uname, uid)
                else:
                    return (False, [], uname, "")
            try:
                limit = max(5, int(author.get("weight", 5)))
            except (ValueError, TypeError):
                limit = 5
            ok, pairs = self._get_post_ids(
                uid, cutoff_ms, limit=limit, ignore_state=ignore_state,
            )
            return (ok, pairs, uname, uid)

        ok_authors = 0
        candidates_map: Dict[str, dict] = {}
        total_new = 0
        with_images = 0

        with self.crawl_run(author_id="all_users") as stats:
            with ThreadPoolExecutor(max_workers=scan_workers) as ex:
                futures = [ex.submit(scan_author, a) for a in authors_list]
                for fut in as_completed(futures):
                    try:
                        ok, pairs, uname, uid = fut.result()
                    except Exception:
                        continue
                    if ok:
                        ok_authors += 1
                    for pid, create_ms in pairs:
                        prev = candidates_map.get(pid)
                        if prev is None or (create_ms or 0) > (prev.get("create_ms") or 0):
                            candidates_map[pid] = {
                                "pid": pid,
                                "create_ms": create_ms,
                                "username": uname,
                                "userid": uid,
                            }

            candidates = list(candidates_map.values())
            if candidates:
                existing = self.check_existing_ids([c["pid"] for c in candidates])
                fresh = [c for c in candidates if c["pid"] not in existing]
                stats["found"] = len(fresh)
                stats["dup"] = len(candidates) - len(fresh)

                # Luôn ưu tiên bài MỚI NHẤT trước
                fresh.sort(key=lambda c: c.get("create_ms") or 0, reverse=True)

                cursor = 0
                while cursor < len(fresh):
                    if max_new_posts is not None and total_new >= max_new_posts:
                        break
                    remaining_need = (
                        (max_new_posts - total_new) if max_new_posts is not None else len(fresh)
                    )
                    chunk_size = min(len(fresh) - cursor, max(remaining_need + 4, int(remaining_need * 1.5)))
                    chunk = fresh[cursor:cursor + chunk_size]
                    cursor += chunk_size

                    details_map: Dict[str, Optional[dict]] = {}
                    with ThreadPoolExecutor(max_workers=min(6, len(chunk))) as dex:
                        fut_map = {dex.submit(self._crawl_post_detail, c["pid"]): c["pid"] for c in chunk}
                        for dfut in as_completed(fut_map):
                            pid_k = fut_map[dfut]
                            try:
                                details_map[pid_k] = dfut.result()
                            except Exception:
                                details_map[pid_k] = None

                    for cand in chunk:
                        if max_new_posts is not None and total_new >= max_new_posts:
                            break
                        pid = cand["pid"]
                        detail = details_map.get(pid)
                        if not detail:
                            continue

                        images = self.process_images(pid, detail.get("image_urls") or [])
                        if images is None:
                            stats["dup"] += 1
                            continue

                        pt = detail.get("publish_time") or cand.get("create_ms")
                        url = f"https://www.binance.com/vi/square/post/{pid}"
                        ok_save = self.save_post(
                            post_id=pid,
                            url=url,
                            text=detail.get("text", ""),
                            image_paths=images,
                            create_time=pt,
                            crawled_from=cand["username"],
                            hashtags=detail.get("hashtags", ""),
                        )
                        if ok_save:
                            total_new += 1
                            stats["new"] += 1
                            if images:
                                with_images += 1
                            if pt and (stats["last_item_time"] is None or pt > stats["last_item_time"]):
                                stats["last_item_time"] = pt
                        else:
                            stats["dup"] += 1

            if ok_authors > 0:
                self.save_last_crawl_time(current_ms)

        logger.info(
            "[BINANCE] hoàn tất: %d bài mới (%d bài có ảnh) | authors_ok=%d/%d | candidates=%d",
            total_new, with_images, ok_authors, len(authors_list), len(candidates_map),
        )
        return total_new

    def initial_crawl(self, max_new_posts: Optional[int] = None) -> int:
        ignore_state = not self.has_crawl_state()
        return self.crawl_all_users(max_new_posts=max_new_posts, ignore_state=ignore_state)

    def reload_crawl(self, max_new_posts: Optional[int] = None) -> int:
        return self.crawl_all_users(max_new_posts=max_new_posts, ignore_state=False)

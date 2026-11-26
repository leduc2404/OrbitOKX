"""BaseCrawler — hạ tầng chung cho mọi nguồn crawl của OKXSquare.

Cung cấp:
  * State watermark lưu nguyên tử vào `data/crawler_state.json` (dạng ms, atomic
    .tmp -> flush -> fsync -> os.replace) và đồng bộ sang bảng DB `crawl_state`.
  * Proxy rotation + immediate fallback về direct giống BinanceSquare.
  * Pipeline ảnh thống nhất: download bền vững -> QR check (loại cả bài) ->
    watermark removal -> sanitize (xoá metadata + chống trùng perceptual hash).
  * Dedup qua DatabaseManager.insert_post + tự dọn ảnh nếu bài bị trùng.
  * Ghi crawl_history qua context manager crawl_run().
  * HTTP retry/backoff dùng curl_cffi (bypass Cloudflare) cho các nguồn cần.

Secret material KHÔNG xuất hiện ở đây; proxy (nếu có) truyền qua cấu hình.
"""
import json
import os
import random
import threading
import time
from contextlib import contextmanager
from typing import Iterable, List, Optional
from urllib.parse import urlparse

import requests
from curl_cffi import requests as cffi
from PIL import Image, ImageOps

from infra.logging_config import get_logger
from services.processor import image_sanitizer
from services.processor.qr_detector import contains_qr_code
from services.processor.watermark import remove_watermark

logger = get_logger(__name__)

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_STATE_FILE_LOCK = threading.Lock()
_DEFAULT_IMG_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BaseCrawler:
    """Lớp cơ sở. Lớp con đặt `source` và cài `_fetch_*` + `crawl_*`."""

    source = "base"

    def __init__(self, db, config: Optional[dict] = None,
                 image_config: Optional[dict] = None,
                 proxy_config: Optional[dict] = None,
                 state_file: Optional[str] = None,
                 max_retries: int = 3, retry_delay: int = 5):
        self.db = db
        self.config = config or {}
        self.image_config = image_config or {}
        self.proxy_config = proxy_config or {}
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.image_dir = db.image_dir
        os.makedirs(self.image_dir, exist_ok=True)

        # File state mốc thời gian crawl (chuẩn BinanceSquare: data/crawler_state.json)
        if state_file is not None:
            self.state_file: Optional[str] = state_file or None
        else:
            db_path = getattr(db, "db_path", None)
            if db_path and str(db_path).strip() and str(db_path).strip() != ":memory:":
                self.state_file = os.path.join(
                    os.path.dirname(str(db_path)) or "data", "crawler_state.json"
                )
            else:
                self.state_file = None

        # Proxy rotation + immediate fallback
        self.proxy_list: List[str] = [
            str(p).strip()
            for p in (self.proxy_config.get("proxies") or [])
            if p and str(p).strip()
        ]
        self.proxy_enabled = bool(self.proxy_config.get("enabled", False)) and bool(self.proxy_list)
        self.fallback_on_failure = bool(self.proxy_config.get("fallback_on_failure", True))
        self.proxy_timeout = float(self.proxy_config.get("proxy_timeout", 10))
        self.max_proxy_retries = int(self.proxy_config.get("max_proxy_retries", 2))
        self._failed_proxies = set()
        self._proxy_lock = threading.Lock()

        # Knob xử lý ảnh (idempotent, dùng chung cho mọi crawler)
        image_sanitizer.configure(self.image_config)
        sanitize_cfg = self.image_config.get("sanitize", {}) or {}
        self.sanitize_enabled = bool(sanitize_cfg.get("enabled", True))
        self.min_valid_bytes = int(self.image_config.get("min_valid_bytes", 128))
        self.max_dimension = int(self.image_config.get("max_dimension", 1920))
        qr_cfg = self.image_config.get("qr_detection", {}) or {}
        self.qr_enabled = bool(qr_cfg.get("enabled", True))
        self.drop_on_qr = bool(qr_cfg.get("drop_post_on_qr", True))
        self.watermark_cfg = self.image_config.get("watermark", {}) or {}

    # ------------------------------------------------------------------
    # PROXY ROTATION + IMMEDIATE FALLBACK
    # ------------------------------------------------------------------
    def _get_random_proxy(self) -> Optional[str]:
        if not self.proxy_enabled or not self.proxy_list:
            return None
        with self._proxy_lock:
            available = [p for p in self.proxy_list if p not in self._failed_proxies]
        if not available:
            return None
        return random.choice(available)

    def _mark_proxy_failed(self, proxy_url: Optional[str]) -> None:
        if not proxy_url:
            return
        with self._proxy_lock:
            if proxy_url not in self._failed_proxies:
                self._failed_proxies.add(proxy_url)
                remaining = len(self.proxy_list) - len(self._failed_proxies)
                logger.warning("[%s] proxy failed -> còn %d proxy khả dụng", self.source, remaining)

    # ------------------------------------------------------------------
    # STATE WATERMARK (atomic JSON data/crawler_state.json + DB sync)
    # ------------------------------------------------------------------
    def _read_state_file(self) -> dict:
        if not self.state_file:
            return {}
        with _STATE_FILE_LOCK:
            if not os.path.exists(self.state_file):
                return {}
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception as e:
                logger.warning("[%s] không đọc được %s: %s", self.source, self.state_file, e)
                return {}

    def _write_state_file_atomic(self, data: dict) -> None:
        if not self.state_file:
            return
        with _STATE_FILE_LOCK:
            parent = os.path.dirname(self.state_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp_path = f"{self.state_file}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.state_file)
            except Exception as e:
                logger.warning("[%s] lỗi ghi atomic %s: %s", self.source, self.state_file, e)
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _lookup_state_dict(data: dict, source: str,
                           author_id: Optional[str] = None,
                           subkey: Optional[str] = None) -> Optional[int]:
        if not isinstance(data, dict):
            return None
        sources = data.get("sources")
        if not isinstance(sources, dict):
            return None
        src_entry = sources.get(source)
        if not isinstance(src_entry, dict):
            return None
        subkeys = src_entry.get("subkeys") if isinstance(src_entry.get("subkeys"), dict) else {}
        if subkey:
            val = subkeys.get(subkey)
            if val:
                return int(val)
            return None
        if author_id:
            val = subkeys.get(f"author:{author_id}")
            if val:
                return int(val)
        val = src_entry.get("last_crawl_time_ms")
        return int(val) if val else None

    def has_crawl_state(self, author_id: Optional[str] = None,
                        subkey: Optional[str] = None) -> bool:
        """True nếu nguồn (hoặc subkey/author) đã từng lưu mốc thời gian crawl."""
        data = self._read_state_file()
        if self._lookup_state_dict(data, self.source, author_id=author_id, subkey=subkey) is not None:
            return True
        state = self.db.get_crawl_state(self.source)
        if not state:
            return False
        key = subkey or author_id
        if key and state.get("authors_json"):
            try:
                authors = json.loads(state["authors_json"]) or {}
                if authors.get(key):
                    return True
            except (ValueError, TypeError):
                pass
        if subkey:
            return False
        return bool(state.get("last_crawl_time_ms"))

    def get_last_crawl_time(self, author_id: Optional[str] = None,
                            lookback_hours: float = 0.5,
                            subkey: Optional[str] = None) -> int:
        """Lấy mốc crawl gần nhất (ms). Ưu tiên file crawler_state.json -> DB -> fallback lookback."""
        data = self._read_state_file()
        file_val = self._lookup_state_dict(data, self.source, author_id=author_id, subkey=subkey)
        if file_val:
            return file_val

        state = self.db.get_crawl_state(self.source)
        key = subkey or author_id
        if key and state and state.get("authors_json"):
            try:
                authors = json.loads(state["authors_json"]) or {}
                if authors.get(key):
                    return int(authors[key])
            except (ValueError, TypeError):
                pass
        if not subkey and state and state.get("last_crawl_time_ms"):
            return int(state["last_crawl_time_ms"])
        return int(time.time() * 1000) - int(float(lookback_hours) * 3600 * 1000)

    def save_last_crawl_time(self, time_ms: int,
                             author_id: Optional[str] = None,
                             subkey: Optional[str] = None) -> None:
        """Lưu mốc crawl (ms) atomic vào crawler_state.json và đồng bộ xuống DB crawl_state."""
        ts = int(time_ms)
        if self.state_file:
            data = self._read_state_file()
            sources = data.get("sources")
            if not isinstance(sources, dict):
                sources = {}
                data["sources"] = sources
            src_entry = sources.get(self.source)
            if not isinstance(src_entry, dict):
                src_entry = {}
                sources[self.source] = src_entry

            if subkey or author_id:
                subkeys = src_entry.get("subkeys")
                if not isinstance(subkeys, dict):
                    subkeys = {}
                    src_entry["subkeys"] = subkeys
                k = subkey if subkey else f"author:{author_id}"
                subkeys[k] = ts
                prev_src = int(src_entry.get("last_crawl_time_ms") or 0)
                if ts > prev_src:
                    src_entry["last_crawl_time_ms"] = ts
            else:
                src_entry["last_crawl_time_ms"] = ts

            max_root = max(
                [int(s.get("last_crawl_time_ms") or 0) for s in sources.values() if isinstance(s, dict)]
                or [ts]
            )
            data["last_crawl_time_ms"] = max_root
            self._write_state_file_atomic(data)

        db_key = subkey or author_id
        if db_key:
            state = self.db.get_crawl_state(self.source)
            authors = {}
            if state and state.get("authors_json"):
                try:
                    authors = json.loads(state["authors_json"]) or {}
                except (ValueError, TypeError):
                    authors = {}
            authors[db_key] = ts
            prev_db_ms = int((state or {}).get("last_crawl_time_ms") or 0)
            self.db.set_crawl_state(
                self.source,
                last_crawl_time_ms=max(prev_db_ms, ts),
                authors_json=json.dumps(authors),
            )
        else:
            self.db.set_crawl_state(self.source, last_crawl_time_ms=ts)

    # ------------------------------------------------------------------
    # CRAWL HISTORY
    # ------------------------------------------------------------------
    @contextmanager
    def crawl_run(self, author_id: str = ""):
        """Ghi một lượt crawl vào crawl_history. Yield dict stats để lớp con cập nhật."""
        run_id = self.db.start_crawl_run(self.source, author_id)
        stats = {"found": 0, "new": 0, "dup": 0,
                 "last_item_time": None, "status": "ok", "error": ""}
        try:
            yield stats
        except Exception as e:
            stats["status"] = "error"
            stats["error"] = str(e)[:500]
            logger.error("[%s] crawl_run error: %s", self.source, e)
            raise
        finally:
            self.db.finish_crawl_run(
                run_id, posts_found=stats["found"], posts_new=stats["new"],
                posts_dup=stats["dup"], last_item_time=stats["last_item_time"],
                status=stats["status"], error=stats["error"])

    # ------------------------------------------------------------------
    # HTTP RETRY (curl_cffi — bypass Cloudflare, proxy + immediate fallback)
    # ------------------------------------------------------------------
    def _cffi_request(self, method: str, url: str, *, headers: Optional[dict] = None,
                      params: Optional[dict] = None, json_body: Optional[dict] = None,
                      cookies: Optional[dict] = None, impersonate: str = "chrome120",
                      proxy: Optional[str] = None, timeout: int = 15):
        """Request với proxy immediate-fallback + retry exponential backoff. Trả response hoặc None."""
        # Bước 1: Thử proxy 1 lần nếu chưa truyền proxy tường minh và crawl_proxies đang bật
        chosen_proxy = proxy or (self._get_random_proxy() if self.proxy_enabled else None)
        if chosen_proxy:
            proxies = {"http": chosen_proxy, "https": chosen_proxy}
            try:
                resp = cffi.request(
                    method, url, headers=headers, params=params, json=json_body,
                    cookies=cookies, impersonate=impersonate, proxies=proxies,
                    timeout=min(float(timeout), self.proxy_timeout),
                )
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 503):
                    self._mark_proxy_failed(chosen_proxy)
                elif not self.fallback_on_failure:
                    return resp
            except Exception:
                self._mark_proxy_failed(chosen_proxy)
            if not self.fallback_on_failure and proxy:
                return None

        # Bước 2: Direct request với exponential backoff
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = cffi.request(
                    method, url, headers=headers, params=params, json=json_body,
                    cookies=cookies, impersonate=impersonate, proxies=None, timeout=timeout)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    logger.warning("[%s] HTTP %s, đợi %ss (lần %s/%s)",
                                   self.source, resp.status_code, wait, attempt, self.max_retries)
                    time.sleep(wait)
                    continue
                logger.warning("[%s] HTTP %s cho %s", self.source, resp.status_code, url)
                return resp
            except Exception as e:
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** (attempt - 1))
                    logger.warning("[%s] request lỗi: %s, retry sau %ss", self.source, e, wait)
                    time.sleep(wait)
                else:
                    logger.error("[%s] request thất bại sau %s lần: %s",
                                 self.source, self.max_retries, e)
        return None

    # ------------------------------------------------------------------
    # IMAGE PIPELINE: download -> QR -> watermark -> sanitize
    # ------------------------------------------------------------------
    @staticmethod
    def _image_headers(url: str) -> dict:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if "binance.com" in host or "bnbstatic.com" in host:
            referer = "https://www.binance.com/"
        elif "okx.com" in host:
            referer = "https://www.okx.com/"
        elif parsed.scheme and parsed.netloc:
            referer = f"{parsed.scheme}://{parsed.netloc}/"
        else:
            referer = ""
        headers = {
            "User-Agent": _DEFAULT_IMG_UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _download_raw(self, url: str, filepath: str) -> bool:
        headers = self._image_headers(url)
        tmp_path = f"{filepath}.dl_tmp"

        def _save_bytes(chunks: Iterable[bytes]) -> bool:
            written = 0
            with open(tmp_path, "wb") as f:
                for chunk in chunks:
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            if written < max(1, self.min_valid_bytes):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return False
            os.replace(tmp_path, filepath)
            return True

        # Thử proxy 1 lần nếu bật, sau đó direct retry + curl_cffi fallback
        proxy_url = self._get_random_proxy()
        if proxy_url:
            try:
                proxies = {"http": proxy_url, "https": proxy_url}
                with requests.get(url, headers=headers, proxies=proxies,
                                  timeout=self.proxy_timeout, stream=True) as r:
                    if r.status_code == 200 and _save_bytes(r.iter_content(chunk_size=8192)):
                        return True
                if r.status_code in (429, 503):
                    self._mark_proxy_failed(proxy_url)
            except Exception:
                self._mark_proxy_failed(proxy_url)

        last_status = None
        last_err = None
        for attempt in range(2):
            try:
                with requests.get(url, headers=headers, timeout=20, stream=True) as r:
                    last_status = r.status_code
                    if r.status_code == 200:
                        if _save_bytes(r.iter_content(chunk_size=8192)):
                            return True
                        logger.warning("[%s] ảnh tải về quá nhỏ (< %d bytes): %s",
                                       self.source, self.min_valid_bytes, url)
                        return False
                    if r.status_code == 403:
                        break
            except Exception as e:
                last_err = e
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            if attempt == 0:
                time.sleep(1)

        # Fallback sang curl_cffi nếu requests bị 403/TLS block hoặc lỗi kết nối
        try:
            r_cf = cffi.get(url, headers=headers, impersonate="chrome120", timeout=20)
            if r_cf.status_code == 200 and r_cf.content:
                if _save_bytes([r_cf.content]):
                    return True
            last_status = r_cf.status_code
        except Exception as e:
            last_err = e
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        if last_err is not None and last_status is None:
            logger.warning("[%s] lỗi download ảnh %s: %s", self.source, url, last_err)
        else:
            logger.warning("[%s] không tải được ảnh %s: HTTP %s (%s)",
                           self.source, url, last_status, last_err or "failed")
        return False

    def _purge(self, paths: List[str]) -> None:
        for p in paths:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    @staticmethod
    def _normalize_image_urls(image_urls) -> List[str]:
        out: List[str] = []
        seen = set()
        for item in (image_urls or []):
            url = ""
            if isinstance(item, str):
                url = item.strip()
            elif isinstance(item, dict):
                for k in ("url", "imageUrl", "src", "originUrl", "imgUrl"):
                    val = item.get(k)
                    if isinstance(val, str) and val.strip():
                        url = val.strip()
                        break
            if not url or not url.startswith(("http://", "https://")):
                continue
            if url not in seen:
                seen.add(url)
                out.append(url)
        return out

    def _validate_and_normalize_no_sanitize(self, filepath: str) -> Optional[str]:
        """Kiểm tra file hợp lệ khi sanitize.enabled=False và chuẩn hoá về .jpg sạch."""
        if not filepath or not os.path.exists(filepath):
            return None
        try:
            if os.path.getsize(filepath) < self.min_valid_bytes:
                os.remove(filepath)
                return None
        except OSError:
            return None

        stem, ext = os.path.splitext(filepath)
        jpg_path = f"{stem}.jpg"
        try:
            with Image.open(filepath) as src:
                src.load()
                rgb = ImageOps.exif_transpose(src)
                if rgb.mode in ("RGBA", "LA") or (rgb.mode == "P" and "transparency" in rgb.info):
                    rgba = rgb.convert("RGBA")
                    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    rgb = Image.alpha_composite(bg, rgba).convert("RGB")
                elif rgb.mode != "RGB":
                    rgb = rgb.convert("RGB")
                if ext.lower() in (".jpg", ".jpeg"):
                    if os.path.normcase(os.path.abspath(filepath)) != os.path.normcase(os.path.abspath(jpg_path)):
                        os.replace(filepath, jpg_path)
                    return jpg_path
                tmp = f"{jpg_path}.nosan_tmp.jpg"
                rgb.save(tmp, format="JPEG", quality=92, optimize=True)
                os.replace(tmp, jpg_path)
                if os.path.normcase(os.path.abspath(filepath)) != os.path.normcase(os.path.abspath(jpg_path)):
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                return jpg_path
        except Exception:
            self._purge([filepath])
            return None

    def process_images(self, post_id: str, image_urls) -> Optional[List[str]]:
        """Download + xử lý ảnh: download -> QR check -> watermark -> sanitize.

        Trả None nếu phát hiện QR và drop_on_qr=True (loại toàn bộ bài).
        """
        urls = self._normalize_image_urls(image_urls)
        if not urls:
            return []
        saved: List[str] = []
        wm_enabled = bool(self.watermark_cfg.get("enabled", True)) and (
            str(self.watermark_cfg.get("mode", "off")).lower() != "off"
        )
        for i, url in enumerate(urls):
            try:
                ext = os.path.splitext(urlparse(url).path)[1].lower()
                if ext not in _IMAGE_EXTS:
                    ext = ".jpg"
                raw_path = os.path.join(self.image_dir, f"{post_id}_{i}{ext}")
                jpg_path = os.path.join(self.image_dir, f"{post_id}_{i}.jpg")

                existing = jpg_path if os.path.exists(jpg_path) else (
                    raw_path if os.path.exists(raw_path) else None)
                if not existing:
                    if not self._download_raw(url, raw_path):
                        continue
                    existing = raw_path

                try:
                    if os.path.getsize(existing) < self.min_valid_bytes:
                        self._purge([existing])
                        continue
                except OSError:
                    continue

                # 1. QR check trên ảnh gốc trước khi biến đổi
                if self.qr_enabled and contains_qr_code(existing):
                    logger.warning("[%s] phát hiện QR trong ảnh bài %s", self.source, post_id)
                    self._purge([existing, *saved])
                    if self.drop_on_qr:
                        return None
                    continue

                # 2. Watermark removal TRƯỚC khi sanitize (tránh làm lệch index .sanitized.json)
                if wm_enabled:
                    remove_watermark(existing, self.watermark_cfg)

                # 3. Sanitize SAU CÙNG (nếu bật) để file .jpg cuối khớp hoàn toàn .sanitized.json
                if self.sanitize_enabled:
                    sanitized = image_sanitizer.sanitize_image(existing, seed=f"{post_id}_{i}")
                    if not sanitized or not sanitized.get("path"):
                        logger.warning("[%s] bỏ qua ảnh lỗi/rỗng bài %s: %s",
                                       self.source, post_id, existing)
                        self._purge([existing])
                        continue
                    clean_path = str(sanitized["path"])
                else:
                    clean_path = self._validate_and_normalize_no_sanitize(existing)
                    if not clean_path:
                        logger.warning("[%s] bỏ qua ảnh lỗi bài %s: %s",
                                       self.source, post_id, existing)
                        continue

                saved.append(clean_path)
            except Exception as e:
                logger.warning("[%s] lỗi xử lý ảnh %s: %s", self.source, url, e)
        return saved

    # ------------------------------------------------------------------
    # DEDUP + STORE (thuần theo source + post_id trên DB)
    # ------------------------------------------------------------------
    def check_existing_ids(self, post_ids: List[str]) -> set:
        """Tra cứu hàng loạt post_id đã từng crawl của nguồn hiện tại (qua seen_post_ids + posts)."""
        try:
            return self.db.batch_check_existing_posts(post_ids, source=self.source)
        except TypeError:
            return self.db.batch_check_existing_posts(post_ids)

    def _cleanup_unreferenced_paths(self, image_paths: Optional[List[str]]) -> None:
        if not image_paths:
            return
        for p in image_paths:
            if not p or not os.path.exists(p):
                continue
            try:
                if hasattr(self.db, "is_image_referenced") and self.db.is_image_referenced(p):
                    continue
                os.remove(p)
            except OSError:
                pass

    def save_post(self, post_id: str, url: str, text: str,
                  image_paths: Optional[List[str]] = None,
                  create_time: Optional[int] = None, crawled_from: str = "",
                  hashtags: str = "") -> bool:
        """Lưu bài qua DB. Nếu bài trùng bị từ chối, tự dọn ảnh không còn tham chiếu."""
        ok = self.db.insert_post(
            post_id=post_id, source=self.source, url=url, text=text,
            image_paths=image_paths or [], hashtags=hashtags,
            create_time=create_time, crawled_from=crawled_from)
        if not ok and image_paths:
            self._cleanup_unreferenced_paths(image_paths)
        return ok

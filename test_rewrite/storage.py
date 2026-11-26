"""Dual-mode storage (RAM or Disk) implementing the BaseCrawler DB interface.

Isolated completely from production data/okx_square.db so crawling and rewriting
in Test Studio never corrupt live posting state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional


class TestStorage:
    """Thread-safe RAM / Disk post store compatible with services.crawler.BaseCrawler."""

    def __init__(self, cache_dir: Path, initial_mode: str = "ram"):
        self.cache_dir = Path(cache_dir).resolve()
        self.store_file = self.cache_dir / "posts_store.json"
        self._disk_image_dir = self.cache_dir / "images"
        self._ram_image_dir = Path(tempfile.gettempdir()) / "okxsquare_test_rewrite_images"

        self._lock = threading.RLock()
        self._mode: str = "disk" if initial_mode == "disk" else "ram"
        self._posts: Dict[str, dict] = {}
        self._seen_ids: Dict[str, dict] = {}  # key = f"{source}:{post_id}" (chống trùng thuần theo ID)
        self._crawl_state: Dict[str, dict] = {}
        self._crawl_history: List[dict] = []
        self._sessions: Dict[str, Optional[dict]] = {"matrix": None, "rotate": None}
        self._next_run_id: int = 1

        if self._mode == "disk":
            self._load_from_disk_unlocked()

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def image_dir(self) -> str:
        """Attribute required by BaseCrawler.__init__."""
        d = self._disk_image_dir if self._mode == "disk" else self._ram_image_dir
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    # ------------------------------------------------------------------
    # Mode management & Persistence
    # ------------------------------------------------------------------
    def _serialize_unlocked(self) -> dict:
        return {
            "version": 2,
            "saved_at": time.time(),
            "posts": self._posts,
            "seen_ids": self._seen_ids,
            "crawl_state": self._crawl_state,
            "crawl_history": self._crawl_history[-100:],
            "sessions": self._sessions,
            "next_run_id": self._next_run_id,
        }

    def _save_to_disk_unlocked(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = self._serialize_unlocked()
        tmp_path = self.store_file.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.store_file)

    def _load_from_disk_unlocked(self) -> int:
        if not self.store_file.exists():
            return 0
        with open(self.store_file, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        self._posts = data.get("posts") or {}
        self._seen_ids = data.get("seen_ids") or {}
        # Đồng bộ ngược từ posts đã lưu (nâng cấp từ file v1 cũ)
        for pid, p in self._posts.items():
            src = str(p.get("source") or "unknown").strip().lower()
            key = f"{src}:{pid}"
            if key not in self._seen_ids:
                ts = int(p.get("crawled_at") or time.time())
                self._seen_ids[key] = {"source": src, "post_id": str(pid), "first_seen": ts, "last_seen": ts}
        self._crawl_state = data.get("crawl_state") or {}
        self._crawl_history = data.get("crawl_history") or []
        saved_sess = data.get("sessions") or {}
        self._sessions = {
            "matrix": saved_sess.get("matrix"),
            "rotate": saved_sess.get("rotate"),
        }
        self._next_run_id = int(data.get("next_run_id", len(self._crawl_history) + 1))
        return len(self._posts)

    def _persist_if_disk_unlocked(self) -> None:
        if self._mode == "disk":
            self._save_to_disk_unlocked()

    def set_mode(self, new_mode: str, carry_over: bool = True) -> dict:
        target = "disk" if str(new_mode).lower() == "disk" else "ram"
        with self._lock:
            if target == self._mode:
                return self.get_info()
            if target == "disk":
                self._mode = "disk"
                if carry_over and (self._posts or any(self._sessions.values())):
                    # Merge existing disk posts with current RAM posts (RAM wins on conflict)
                    ram_posts = dict(self._posts)
                    ram_seen = dict(self._seen_ids)
                    ram_state = dict(self._crawl_state)
                    ram_hist = list(self._crawl_history)
                    ram_sessions = dict(self._sessions)
                    if self.store_file.exists():
                        self._load_from_disk_unlocked()
                    self._posts.update(ram_posts)
                    self._seen_ids.update(ram_seen)
                    self._crawl_state.update(ram_state)
                    self._crawl_history = (self._crawl_history + ram_hist)[-100:]
                    for k in ("matrix", "rotate"):
                        if ram_sessions.get(k) is not None:
                            self._sessions[k] = ram_sessions[k]
                    self._save_to_disk_unlocked()
                else:
                    self._posts.clear()
                    self._seen_ids.clear()
                    self._crawl_state.clear()
                    self._crawl_history.clear()
                    self._sessions = {"matrix": None, "rotate": None}
                    self._load_from_disk_unlocked()
            else:
                # Switching disk -> ram
                self._mode = "ram"
                if not carry_over:
                    self._posts.clear()
                    self._seen_ids.clear()
                    self._crawl_state.clear()
                    self._crawl_history.clear()
                    self._sessions = {"matrix": None, "rotate": None}
            return self.get_info()

    def sync_ram_to_disk(self) -> int:
        with self._lock:
            self._save_to_disk_unlocked()
            return len(self._posts)

    def load_disk_to_ram(self) -> int:
        with self._lock:
            return self._load_from_disk_unlocked()

    def clear(self, scope: str = "all") -> int:
        """Clear posts ('all' | 'rewrites_only' | 'crawled_only')."""
        with self._lock:
            if scope == "rewrites_only":
                count = 0
                for p in self._posts.values():
                    if p.get("latest_rewrite") or p.get("rewrite_history"):
                        p["latest_rewrite"] = None
                        p["rewrite_history"] = []
                        p["status"] = "crawled"
                        count += 1
                self._sessions = {"matrix": None, "rotate": None}
                self._persist_if_disk_unlocked()
                return count

            if scope == "crawled_only":
                removed = [pid for pid, p in self._posts.items() if p.get("status") == "crawled"]
                for pid in removed:
                    p = self._posts.pop(pid, None)
                    if p:
                        src = str(p.get("source") or "unknown").strip().lower()
                        self._seen_ids.pop(f"{src}:{pid}", None)
                self._persist_if_disk_unlocked()
                return len(removed)

            count = len(self._posts)
            self._posts.clear()
            self._seen_ids.clear()
            self._crawl_state.clear()
            self._sessions = {"matrix": None, "rotate": None}
            self._persist_if_disk_unlocked()
            return count

    def save_session(self, kind: str, payload: Optional[dict]) -> None:
        if kind not in ("matrix", "rotate"):
            return
        with self._lock:
            self._sessions[kind] = payload
            self._persist_if_disk_unlocked()

    def get_sessions(self) -> Dict[str, Optional[dict]]:
        with self._lock:
            return {
                "matrix": self._sessions.get("matrix"),
                "rotate": self._sessions.get("rotate"),
            }

    def reset_crawl_state(self, sources: Optional[List[str]] = None) -> None:
        with self._lock:
            if not sources:
                self._crawl_state.clear()
            else:
                for src in sources:
                    self._crawl_state.pop(src, None)
            self._persist_if_disk_unlocked()

    # ------------------------------------------------------------------
    # BaseCrawler DB Interface (100% compatible with DatabaseManager)
    # ------------------------------------------------------------------
    @staticmethod
    def compute_content_hash(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        normalized = re.sub(r"\s+", " ", text).strip().lower()
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get_crawl_state(self, source: str) -> Optional[Dict]:
        with self._lock:
            st = self._crawl_state.get(source)
            return dict(st) if st else None

    def set_crawl_state(
        self,
        source: str,
        last_crawl_time_ms: Optional[int] = None,
        authors_json: Optional[str] = None,
    ) -> None:
        with self._lock:
            prev = self._crawl_state.get(source) or {"source": source}
            if last_crawl_time_ms is not None:
                prev["last_crawl_time_ms"] = int(last_crawl_time_ms)
            if authors_json is not None:
                prev["authors_json"] = authors_json
            prev["updated_at"] = int(time.time())
            self._crawl_state[source] = prev
            self._persist_if_disk_unlocked()

    def start_crawl_run(self, source: str, author_id: str = "") -> int:
        with self._lock:
            run_id = self._next_run_id
            self._next_run_id += 1
            self._crawl_history.append({
                "id": run_id,
                "source": source,
                "author_id": author_id,
                "started_at": int(time.time()),
                "finished_at": None,
                "posts_found": 0,
                "posts_new": 0,
                "posts_dup": 0,
                "last_item_time": None,
                "status": "running",
                "error": "",
            })
            if len(self._crawl_history) > 100:
                self._crawl_history = self._crawl_history[-100:]
            return run_id

    def finish_crawl_run(
        self,
        run_id: int,
        posts_found: int = 0,
        posts_new: int = 0,
        posts_dup: int = 0,
        last_item_time: Optional[int] = None,
        status: str = "ok",
        error: str = "",
    ) -> None:
        with self._lock:
            for item in reversed(self._crawl_history):
                if item["id"] == run_id:
                    item.update({
                        "finished_at": int(time.time()),
                        "posts_found": posts_found,
                        "posts_new": posts_new,
                        "posts_dup": posts_dup,
                        "last_item_time": last_item_time,
                        "status": status,
                        "error": error,
                    })
                    break
            self._persist_if_disk_unlocked()

    def batch_check_existing_posts(self, post_ids: List[str],
                                   source: Optional[str] = None) -> set:
        src_norm = str(source or "").strip().lower()
        with self._lock:
            existing = set()
            for raw_pid in (post_ids or []):
                pid = str(raw_pid).strip()
                if not pid:
                    continue
                if src_norm:
                    if f"{src_norm}:{pid}" in self._seen_ids:
                        existing.add(pid)
                    elif pid in self._posts and str(self._posts[pid].get("source") or "").strip().lower() == src_norm:
                        existing.add(pid)
                else:
                    if pid in self._posts or any(v.get("post_id") == pid for v in self._seen_ids.values()):
                        existing.add(pid)
            return existing

    def insert_post(
        self,
        post_id: str,
        source: str,
        url: str = "",
        text: str = "",
        image_paths=None,
        hashtags: str = "",
        create_time: Optional[int] = None,
        crawled_from: str = "",
        content_hash: Optional[str] = None,
    ) -> bool:
        """Chống trùng lặp thuần theo (source, post_id) — KHÔNG so trùng text."""
        pid = str(post_id or "").strip()
        if not pid:
            return False
        src_norm = str(source or "manual").strip().lower() or "manual"
        seen_key = f"{src_norm}:{pid}"

        if isinstance(image_paths, str):
            img_list = [p.strip() for p in image_paths.split("|") if p.strip()]
        elif isinstance(image_paths, (list, tuple)):
            img_list = [str(p) for p in image_paths if p]
        else:
            img_list = []

        if content_hash is None:
            content_hash = self.compute_content_hash(text)

        now = int(time.time())
        with self._lock:
            if seen_key in self._seen_ids or pid in self._posts:
                if seen_key in self._seen_ids:
                    self._seen_ids[seen_key]["last_seen"] = now
                return False

            self._seen_ids[seen_key] = {
                "source": src_norm,
                "post_id": pid,
                "first_seen": now,
                "last_seen": now,
            }
            self._posts[pid] = {
                "id": pid,
                "source": src_norm,
                "url": str(url or ""),
                "text": str(text or ""),
                "original_text_initial": str(text or ""),
                "image_paths": img_list,
                "hashtags": str(hashtags or ""),
                "content_hash": content_hash,
                "crawled_from": str(crawled_from or src_norm),
                "create_time": int(create_time) if create_time else now * 1000,
                "crawled_at": now,
                "status": "crawled",
                "latest_rewrite": None,
                "rewrite_history": [],
            }
            self._persist_if_disk_unlocked()
            return True

    # ------------------------------------------------------------------
    # Studio CRUD & Rewrite History
    # ------------------------------------------------------------------
    def add_manual_post(
        self,
        text: str,
        source: str = "manual",
        author: str = "manual_input",
        url: str = "",
    ) -> dict:
        post_id = f"manual_{int(time.time() * 1000)}_{uuid.uuid4().hex[:5]}"
        now = int(time.time())
        content_hash = self.compute_content_hash(text)
        post = {
            "id": post_id,
            "source": source or "manual",
            "url": url or "",
            "text": text,
            "original_text_initial": text,
            "image_paths": [],
            "hashtags": "",
            "content_hash": content_hash,
            "crawled_from": author or "manual_input",
            "create_time": now * 1000,
            "crawled_at": now,
            "status": "crawled",
            "latest_rewrite": None,
            "rewrite_history": [],
        }
        with self._lock:
            self._posts[post_id] = post
            self._persist_if_disk_unlocked()
            return dict(post)

    def update_post_text(self, post_id: str, new_text: str) -> Optional[dict]:
        with self._lock:
            post = self._posts.get(post_id)
            if not post:
                return None
            post["text"] = new_text
            post["content_hash"] = self.compute_content_hash(new_text)
            self._persist_if_disk_unlocked()
            return dict(post)

    def delete_post(self, post_id: str) -> bool:
        with self._lock:
            post = self._posts.pop(post_id, None)
            if not post:
                return False
            src = str(post.get("source") or "unknown").strip().lower()
            self._seen_ids.pop(f"{src}:{post_id}", None)
            self._persist_if_disk_unlocked()
            return True

    def record_rewrite(self, post_id: str, result: dict) -> Optional[dict]:
        with self._lock:
            post = self._posts.get(post_id)
            if not post:
                return None
            status_map = {
                "ok": "rewritten",
                "rejected": "rejected",
                "error": "error",
            }
            post["status"] = status_map.get(result.get("status", "error"), "error")
            post["latest_rewrite"] = result
            history = post.setdefault("rewrite_history", [])
            history.insert(0, result)
            if len(history) > 25:
                del history[25:]
            self._persist_if_disk_unlocked()
            return dict(post)

    def get_post(self, post_id: str) -> Optional[dict]:
        with self._lock:
            post = self._posts.get(post_id)
            return dict(post) if post else None

    def list_posts(
        self,
        source: Optional[str] = None,
        status: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[dict]:
        with self._lock:
            posts = list(self._posts.values())

        if source and source != "all":
            posts = [p for p in posts if p.get("source") == source]
        if status and status != "all":
            posts = [p for p in posts if p.get("status") == status]
        if query:
            q = query.strip().lower()
            posts = [
                p
                for p in posts
                if q in (p.get("text") or "").lower()
                or q in (p.get("id") or "").lower()
                or q in (p.get("crawled_from") or "").lower()
                or q in ((p.get("latest_rewrite") or {}).get("output_text") or "").lower()
            ]

        posts.sort(
            key=lambda p: (p.get("create_time") or 0, p.get("crawled_at") or 0),
            reverse=True,
        )
        # Return lightweight list items (still include latest_rewrite summary)
        result = []
        for p in posts:
            item = dict(p)
            item["history_count"] = len(p.get("rewrite_history") or [])
            item.pop("rewrite_history", None)
            result.append(item)
        return result

    def export_all(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode,
                "exported_at": time.time(),
                "total": len(self._posts),
                "posts": list(self._posts.values()),
            }

    def import_from_production_db(self, prod_db_path: Path, limit: int = 25) -> dict:
        """Read-only import of recent posts from production data/okx_square.db."""
        if not prod_db_path.exists():
            raise FileNotFoundError(f"Không tìm thấy production DB: {prod_db_path}")

        uri = f"file:{prod_db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, source, url, text, image_paths, hashtags, crawled_from,
                       create_time, crawled_at, gemini_output, status
                FROM posts
                WHERE text IS NOT NULL AND TRIM(text) != ''
                ORDER BY COALESCE(create_time, crawled_at * 1000) DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        finally:
            conn.close()

        imported = 0
        skipped = 0
        for r in rows:
            ok = self.insert_post(
                post_id=str(r["id"]),
                source=str(r["source"] or "prod"),
                url=str(r["url"] or ""),
                text=str(r["text"] or ""),
                image_paths=r["image_paths"] or "",
                hashtags=str(r["hashtags"] or ""),
                create_time=r["create_time"],
                crawled_from=str(r["crawled_from"] or ""),
            )
            if ok:
                imported += 1
            else:
                skipped += 1
        return {"fetched": len(rows), "imported": imported, "skipped": skipped}

    def get_info(self, prod_db_path: Optional[Path] = None) -> dict:
        with self._lock:
            by_source: Dict[str, int] = {}
            by_status: Dict[str, int] = {}
            for p in self._posts.values():
                s = p.get("source", "unknown")
                st = p.get("status", "crawled")
                by_source[s] = by_source.get(s, 0) + 1
                by_status[st] = by_status.get(st, 0) + 1
            disk_exists = self.store_file.exists()
            disk_bytes = self.store_file.stat().st_size if disk_exists else 0
            return {
                "mode": self._mode,
                "total_posts": len(self._posts),
                "by_source": by_source,
                "by_status": by_status,
                "disk_path": str(self.store_file),
                "disk_exists": disk_exists,
                "disk_bytes": disk_bytes,
                "prod_db_exists": bool(prod_db_path and prod_db_path.exists()),
                "crawl_history": self._crawl_history[-10:],
            }

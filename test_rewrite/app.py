# -*- coding: utf-8 -*-
"""OKXSquare Prompt Tuning Lab — Flask Web Application.

3 chế độ chuyên sâu để tinh chỉnh 20 prompt (group1 + group2):
  1. 1 Bài x 1 Prompt  — tập trung sửa prompt trực tiếp, so sánh bản trước/sau & lưu file .txt
  2. 1 Bài x 20 Prompt — chạy 1 bài qua toàn bộ G1+G2 song song, chấm điểm & radar chống lặp
  3. Xoay đủ 20 Prompt — 1 bài 1 prompt xoay vòng phủ đủ 20 prompt, theo dõi coverage & chất lượng

Chạy:
    python test_rewrite/app.py
    python test_rewrite/app.py --port 5055 --storage ram
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

STUDIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = STUDIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        if sys.stdout is not None and hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if sys.stderr is not None and hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from flask import Flask, Response, jsonify, render_template, request

from infra.logging_config import setup_logging
from test_rewrite.config_loader import StudioConfigLoader
from test_rewrite.crawler_bridge import CrawlerBridge
from test_rewrite.log_buffer import BufferLoggingHandler, LogBuffer
from test_rewrite.rewrite_engine import RewriteEngine, analyze_matrix_results
from test_rewrite.storage import TestStorage


class JobManager:
    """Tracks async crawl / all-prompts / rotation jobs with live partial results & cancellation."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, dict] = {}
        self._cancel_events: Dict[str, threading.Event] = {}

    def create(self, kind: str, total: int = 0, label: str = "") -> tuple[dict, threading.Event]:
        job_id = f"{kind}_{uuid.uuid4().hex[:8]}"
        cancel_ev = threading.Event()
        job = {
            "id": job_id,
            "kind": kind,
            "label": label,
            "status": "running",
            "progress": 0,
            "total": total,
            "message": "Đang khởi chạy...",
            "started_at": time.time(),
            "finished_at": None,
            "items": [],
            "analytics": None,
            "result": None,
            "error": "",
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = cancel_ev
            if len(self._jobs) > 30:
                oldest = sorted(self._jobs.values(), key=lambda j: j["started_at"])[:8]
                for o in oldest:
                    self._jobs.pop(o["id"], None)
                    self._cancel_events.pop(o["id"], None)
        return dict(job), cancel_ev

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.update(kwargs)

    def append_item(self, job_id: str, item: dict, message: str = "") -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["items"].append(item)
            job["progress"] = len(job["items"])
            if message:
                job["message"] = message

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            ev = self._cancel_events.get(job_id)
            job = self._jobs.get(job_id)
            if ev and job and job["status"] == "running":
                ev.set()
                job["message"] = "Đang dừng theo yêu cầu..."
                return True
            return False

    def finish(
        self,
        job_id: str,
        result: Any = None,
        analytics: Optional[dict] = None,
        error: str = "",
        cancelled: bool = False,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                if cancelled:
                    job["status"] = "cancelled"
                    job["message"] = f"Đã dừng ({job['progress']}/{job['total']})"
                elif error:
                    job["status"] = "error"
                    job["error"] = error
                    job["message"] = f"Lỗi: {error}"
                else:
                    job["status"] = "done"
                    job["progress"] = job["total"] or job["progress"] or 1
                    job["message"] = "Hoàn tất"
                job["result"] = result
                if analytics is not None:
                    job["analytics"] = analytics
                job["finished_at"] = time.time()

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            copy = dict(job)
            copy["items"] = list(job["items"])
            return copy


def create_app(initial_storage_mode: str = "ram") -> Flask:
    cache_dir = STUDIO_DIR / "cache"
    setup_logging(log_dir=str(cache_dir / "logs"), console_level=logging.INFO)

    logs = LogBuffer(maxlen=2000)
    root_logger = logging.getLogger()
    if not any(isinstance(h, BufferLoggingHandler) for h in root_logger.handlers):
        root_logger.addHandler(BufferLoggingHandler(logs))

    config_loader = StudioConfigLoader(PROJECT_ROOT)
    storage = TestStorage(cache_dir=cache_dir, initial_mode=initial_storage_mode)
    crawler = CrawlerBridge(storage=storage, config_loader=config_loader, logs=logs)
    rewriter = RewriteEngine(
        project_root=PROJECT_ROOT,
        cache_dir=cache_dir,
        config_loader=config_loader,
        logs=logs,
    )
    jobs = JobManager()
    prod_db_path = PROJECT_ROOT / "data" / "okx_square.db"

    cfg_snap = config_loader.get_public_config()
    logs.success(
        "SYSTEM",
        f"Prompt Tuning Lab sẵn sàng | provider={cfg_snap['active_provider']} | storage={storage.mode.upper()} | {rewriter.get_prompts_catalog()['total']} prompts",
    )

    app = Flask(
        __name__,
        template_folder=str(STUDIO_DIR / "templates"),
        static_folder=str(STUDIO_DIR / "static"),
    )
    app.config["JSON_AS_ASCII"] = False

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        return render_template("index.html")

    # ------------------------------------------------------------------
    # Config & Prompt Management Endpoints
    # ------------------------------------------------------------------
    @app.route("/api/config", methods=["GET"])
    def api_get_config():
        return jsonify({"ok": True, "config": config_loader.get_public_config()})

    @app.route("/api/config/reload", methods=["POST"])
    def api_reload_config():
        try:
            snap = config_loader.reload()
            prompts = rewriter.reload_prompts()
            logs.success("CONFIG", f"Đã reload gemini_config.yaml & {prompts['total']} prompts")
            return jsonify({"ok": True, "config": snap, "prompts": prompts})
        except Exception as e:
            tb = traceback.format_exc()
            logs.error("CONFIG", f"Reload config thất bại: {e}", details=tb)
            return jsonify({"ok": False, "error": str(e), "traceback": tb}), 500

    @app.route("/api/config/ping", methods=["POST"])
    def api_ping_provider():
        body = request.get_json(silent=True) or {}
        res = rewriter.ping_provider(
            provider_name=body.get("provider"),
            overrides=body.get("overrides") or {},
        )
        return jsonify(res), (200 if res.get("ok") else 502)

    @app.route("/api/prompts", methods=["GET"])
    def api_get_prompts():
        return jsonify({"ok": True, "prompts": rewriter.get_prompts_catalog()})

    @app.route("/api/prompts/<variant_id>", methods=["PUT"])
    def api_save_prompt(variant_id: str):
        body = request.get_json(silent=True) or {}
        content = body.get("content")
        if content is None:
            return jsonify({"ok": False, "error": "Thiếu nội dung prompt (content)"}), 400
        try:
            updated = rewriter.save_prompt_file(variant_id, str(content))
            return jsonify({"ok": True, "prompt": updated, "prompts": rewriter.get_prompts_catalog()})
        except Exception as e:
            logs.error("CONFIG", f"Lưu prompt [{variant_id}] thất bại: {e}")
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/prompts/<variant_id>/restore", methods=["POST"])
    def api_restore_prompt(variant_id: str):
        try:
            restored = rewriter.restore_prompt_backup(variant_id)
            return jsonify({"ok": True, "prompt": restored, "prompts": rewriter.get_prompts_catalog()})
        except Exception as e:
            logs.error("CONFIG", f"Khôi phục prompt [{variant_id}] thất bại: {e}")
            return jsonify({"ok": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Storage Endpoints (RAM / Disk)
    # ------------------------------------------------------------------
    @app.route("/api/storage", methods=["GET"])
    def api_get_storage():
        return jsonify({"ok": True, "storage": storage.get_info(prod_db_path)})

    @app.route("/api/storage/mode", methods=["POST"])
    def api_set_storage_mode():
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "ram")
        carry_over = bool(body.get("carry_over", True))
        try:
            info = storage.set_mode(mode, carry_over=carry_over)
            logs.info("STORAGE", f"Chuyển lưu trữ crawl sang {info['mode'].upper()} ({info['total_posts']} bài)")
            return jsonify({"ok": True, "storage": info})
        except Exception as e:
            logs.error("STORAGE", f"Chuyển storage mode lỗi: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/storage/sync", methods=["POST"])
    def api_sync_storage():
        body = request.get_json(silent=True) or {}
        direction = body.get("direction", "to_disk")
        try:
            if direction == "from_disk":
                n = storage.load_disk_to_ram()
                logs.success("STORAGE", f"Đã nạp {n} bài từ ổ cứng vào RAM")
            else:
                n = storage.sync_ram_to_disk()
                logs.success("STORAGE", f"Đã lưu {n} bài từ RAM xuống ổ cứng")
            return jsonify({"ok": True, "count": n, "storage": storage.get_info(prod_db_path)})
        except Exception as e:
            logs.error("STORAGE", f"Đồng bộ storage lỗi: {e}")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/storage/clear", methods=["POST"])
    def api_clear_storage():
        body = request.get_json(silent=True) or {}
        scope = body.get("scope", "all")
        removed = storage.clear(scope=scope)
        logs.warning("STORAGE", f"Đã dọn storage (scope={scope}): {removed} bài")
        return jsonify({"ok": True, "removed": removed, "storage": storage.get_info(prod_db_path)})

    @app.route("/api/storage/import-prod", methods=["POST"])
    def api_import_prod():
        body = request.get_json(silent=True) or {}
        limit = max(1, min(200, int(body.get("limit", 25))))
        try:
            res = storage.import_from_production_db(prod_db_path, limit=limit)
            logs.success("STORAGE", f"Import Production DB: +{res['imported']} bài mới")
            return jsonify({"ok": True, "result": res, "storage": storage.get_info(prod_db_path)})
        except Exception as e:
            logs.error("STORAGE", f"Import Production DB lỗi: {e}")
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.route("/api/storage/export", methods=["GET"])
    def api_export_storage():
        payload = json.dumps(storage.export_all(), ensure_ascii=False, indent=2)
        return Response(
            payload,
            mimetype="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=okxsquare_prompt_lab_export.json"},
        )

    # ------------------------------------------------------------------
    # Posts CRUD
    # ------------------------------------------------------------------
    @app.route("/api/posts", methods=["GET"])
    def api_list_posts():
        source = request.args.get("source", "all")
        status = request.args.get("status", "all")
        q = request.args.get("q", "")
        items = storage.list_posts(source=source, status=status, query=q)
        return jsonify({"ok": True, "posts": items, "storage": storage.get_info(prod_db_path)})

    @app.route("/api/posts/<post_id>", methods=["GET"])
    def api_get_post(post_id: str):
        post = storage.get_post(post_id)
        if not post:
            return jsonify({"ok": False, "error": f"Không tìm thấy bài {post_id}"}), 404
        return jsonify({"ok": True, "post": post})

    @app.route("/api/posts/manual", methods=["POST"])
    def api_add_manual_post():
        body = request.get_json(silent=True) or {}
        text = str(body.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "Nội dung bài viết không được rỗng"}), 400
        post = storage.add_manual_post(
            text=text,
            source=str(body.get("source") or "manual").strip(),
            author=str(body.get("author") or "manual_input").strip(),
            url=str(body.get("url") or "").strip(),
        )
        logs.info("STORAGE", f"Thêm bài thủ công {post['id']} ({len(text.split())} từ)")
        return jsonify({"ok": True, "post": post, "storage": storage.get_info(prod_db_path)})

    @app.route("/api/posts/<post_id>", methods=["PUT"])
    def api_update_post(post_id: str):
        body = request.get_json(silent=True) or {}
        post = storage.update_post_text(post_id, str(body.get("text") or ""))
        if not post:
            return jsonify({"ok": False, "error": "Không tìm thấy bài viết"}), 404
        return jsonify({"ok": True, "post": post})

    @app.route("/api/posts/<post_id>", methods=["DELETE"])
    def api_delete_post(post_id: str):
        ok = storage.delete_post(post_id)
        if not ok:
            return jsonify({"ok": False, "error": "Không tìm thấy bài viết"}), 404
        return jsonify({"ok": True, "storage": storage.get_info(prod_db_path)})

    # ------------------------------------------------------------------
    # Crawl Endpoint (Async Job) & OKX Trending Topics
    # ------------------------------------------------------------------
    @app.route("/api/okx/trending-topics", methods=["GET"])
    def api_okx_trending_topics():
        force = request.args.get("force", "0") in ("1", "true", "yes")
        try:
            res = crawler.get_okx_trending_topics(force=force)
            return jsonify(res), (200 if res.get("ok") else 502)
        except Exception as e:
            logs.error("CRAWL", f"Lấy OKX trending topics lỗi: {e}")
            return jsonify({"ok": False, "error": str(e), "default_hashtags": ["BTC", "ETH", "SOL", "OKB", "XRP", "DOGE", "SUI", "PEPE"], "trending_topics": []}), 500

    @app.route("/api/crawl", methods=["POST"])
    def api_start_crawl():
        body = request.get_json(silent=True) or {}
        sources = body.get("sources") or ["binance", "okx"]
        max_posts = max(1, min(200, int(body.get("max_posts_per_source", 10))))
        lookback_hours = max(0.5, min(720.0, float(body.get("lookback_hours", 48.0))))
        max_pages = max(1, min(15, int(body.get("max_pages", 2))))
        ignore_watermark = bool(body.get("ignore_watermark", True))
        binance_authors = body.get("binance_authors", "")
        binance_post_ids = body.get("binance_post_ids", "")
        binance_keywords = body.get("binance_keywords", "")
        binance_featured = bool(body.get("binance_featured", False))
        okx_hashtags = body.get("okx_hashtags", "")
        okx_timeline_type = str(body.get("okx_timeline_type", "1"))
        okx_include_trending = bool(body.get("okx_include_trending", False))
        okx_include_feed = bool(body.get("okx_include_feed", False))
        okx_authors = body.get("okx_authors", "")

        job, _ = jobs.create("crawl", total=len(sources), label=f"Crawl {', '.join(sources)}")
        job_id = job["id"]

        def _worker():
            try:
                summary = crawler.run_crawl(
                    sources=sources,
                    max_posts_per_source=max_posts,
                    lookback_hours=lookback_hours,
                    max_pages=max_pages,
                    ignore_watermark=ignore_watermark,
                    download_images=False,
                    binance_authors_raw=binance_authors,
                    binance_post_ids_raw=binance_post_ids,
                    binance_keywords_raw=binance_keywords,
                    binance_featured=binance_featured,
                    okx_hashtags_raw=okx_hashtags,
                    okx_timeline_type=okx_timeline_type,
                    okx_include_trending=okx_include_trending,
                    okx_include_feed=okx_include_feed,
                    okx_authors_raw=okx_authors,
                    progress_cb=lambda cur, tot, msg: jobs.update(job_id, progress=cur, total=tot, message=msg),
                )
                jobs.finish(job_id, result=summary)
            except Exception as e:
                tb = traceback.format_exc()
                logs.error("CRAWL", f"Crawl job {job_id} lỗi: {e}", details=tb)
                jobs.finish(job_id, error=str(e))

        threading.Thread(target=_worker, daemon=True, name=f"crawl-{job_id}").start()
        return jsonify({"ok": True, "job": job})

    # ------------------------------------------------------------------
    # Mode 1: Single Rewrite (1 Bài x 1 Prompt)
    # ------------------------------------------------------------------
    @app.route("/api/rewrite", methods=["POST"])
    def api_rewrite_single():
        body = request.get_json(silent=True) or {}
        post_id = str(body.get("post_id") or "").strip()
        post = storage.get_post(post_id) if post_id else None

        text = body.get("text")
        if text is None and post:
            text = post.get("text", "")
        text = str(text or "")

        source = body.get("source") or (post.get("source") if post else "manual")
        custom_tpl = body.get("custom_prompt_template")
        is_draft = bool(body.get("is_draft", bool(custom_tpl)))

        result = rewriter.rewrite(
            text=text,
            post_id=post_id,
            source=source,
            provider=body.get("provider"),
            overrides=body.get("overrides") or {},
            group=body.get("group", "auto"),
            variant_id=body.get("variant_id", "auto"),
            custom_prompt_template=custom_tpl,
            custom_system_prompt=body.get("custom_system_prompt"),
            enable_prescreen=bool(body.get("enable_prescreen", True)),
            enable_clean_output=bool(body.get("enable_clean_output", True)),
            is_draft=is_draft,
        )

        updated_post = None
        if post and body.get("record_to_post", True):
            updated_post = storage.record_rewrite(post_id, result)

        return jsonify({
            "ok": result.get("status") != "error",
            "result": result,
            "post": updated_post,
            "storage": storage.get_info(prod_db_path),
        })

    # ------------------------------------------------------------------
    # Mode 2: 1 Bài x Tất cả 20 Prompt (All-Prompts Matrix)
    # ------------------------------------------------------------------
    @app.route("/api/test/all-prompts", methods=["POST"])
    def api_test_all_prompts():
        body = request.get_json(silent=True) or {}
        post_id = str(body.get("post_id") or "").strip()
        post = storage.get_post(post_id) if post_id else None
        text = str(body.get("text") if body.get("text") is not None else (post.get("text", "") if post else "")).strip()
        if not text:
            return jsonify({"ok": False, "error": "Chưa có nội dung bài viết gốc để chạy 20 prompt!"}), 400

        variant_ids = rewriter.resolve_variant_list(body.get("variant_ids") or body.get("scope") or "all")
        if not variant_ids:
            return jsonify({"ok": False, "error": "Không tìm thấy prompt nào để chạy!"}), 400

        draft_overrides: Dict[str, str] = body.get("draft_overrides") or {}
        concurrency = max(1, min(20, int(body.get("concurrency", 5))))
        source = body.get("source") or (post.get("source") if post else "manual")

        job, cancel_ev = jobs.create(
            "all_prompts",
            total=len(variant_ids),
            label=f"1 Bài × {len(variant_ids)} Prompt",
        )
        job_id = job["id"]
        jobs.update(job_id, message=f"Đang chạy {len(variant_ids)} prompt ({concurrency} luồng đồng thời)...")

        def _worker():
            t0 = time.time()
            logs.info("REWRITE", f"[Mode 1×{len(variant_ids)}] Bắt đầu chạy song song {concurrency} luồng...")
            order_map = {vid: idx for idx, vid in enumerate(variant_ids)}
            collected: List[dict] = []

            def _run_one(vid: str) -> Optional[dict]:
                if cancel_ev.is_set():
                    return None
                draft_tpl = draft_overrides.get(vid)
                res = rewriter.rewrite(
                    text=text,
                    post_id=post_id,
                    source=source,
                    provider=body.get("provider"),
                    overrides=body.get("overrides") or {},
                    variant_id=vid,
                    custom_prompt_template=draft_tpl,
                    custom_system_prompt=body.get("custom_system_prompt"),
                    enable_prescreen=bool(body.get("enable_prescreen", True)),
                    enable_clean_output=bool(body.get("enable_clean_output", True)),
                    is_draft=bool(draft_tpl),
                )
                res["order"] = order_map.get(vid, 0)
                return res

            try:
                with ThreadPoolExecutor(max_workers=concurrency) as ex:
                    futures = [ex.submit(_run_one, vid) for vid in variant_ids]
                    for fut in as_completed(futures):
                        if cancel_ev.is_set():
                            for f in futures:
                                f.cancel()
                        if fut.cancelled():
                            continue
                        item = fut.result()
                        if item is None:
                            continue
                        collected.append(item)
                        jobs.append_item(
                            job_id,
                            item,
                            message=f"Xong {len(collected)}/{len(variant_ids)}: {item.get('variant_id')}",
                        )

                collected.sort(key=lambda x: x.get("order", 0))
                analytics = analyze_matrix_results(collected)
                duration_ms = int((time.time() - t0) * 1000)
                storage.save_session(
                    "matrix",
                    {
                        "post_id": post_id,
                        "source": source,
                        "original_text": text,
                        "items": collected,
                        "analytics": analytics,
                        "duration_ms": duration_ms,
                        "updated_at": time.time(),
                    },
                )
                logs.success(
                    "REWRITE",
                    f"[Mode 1×{len(variant_ids)}] Hoàn tất {len(collected)}/{len(variant_ids)} ({duration_ms / 1000:.1f}s) | TB {analytics['avg_score']}đ | Đa dạng {analytics['diversity_score']}%",
                )
                jobs.finish(
                    job_id,
                    result={"post_id": post_id, "original_text": text, "duration_ms": duration_ms},
                    analytics=analytics,
                    cancelled=cancel_ev.is_set(),
                )
            except Exception as e:
                tb = traceback.format_exc()
                logs.error("REWRITE", f"[Mode 1×N] Lỗi: {e}", details=tb)
                jobs.finish(job_id, error=str(e))

        threading.Thread(target=_worker, daemon=True, name=f"matrix-{job_id}").start()
        return jsonify({"ok": True, "job": job})

    # ------------------------------------------------------------------
    # Mode 3: N Bài x Xoay đủ 20 Prompt (1 Bài 1 Prompt Round-Robin)
    # ------------------------------------------------------------------
    @app.route("/api/test/rotate-prompts", methods=["POST"])
    def api_test_rotate_prompts():
        body = request.get_json(silent=True) or {}
        variant_ids = rewriter.resolve_variant_list(body.get("variant_ids") or body.get("scope") or "all")
        if not variant_ids:
            return jsonify({"ok": False, "error": "Không có prompt nào được chọn!"}), 400

        req_post_ids = body.get("post_ids") or []
        if req_post_ids:
            posts = [p for pid in req_post_ids if (p := storage.get_post(pid))]
        else:
            posts = storage.list_posts()

        if not posts:
            return jsonify({"ok": False, "error": "Chưa có bài viết nào trong bộ nhớ! Hãy Crawl trước."}), 400

        fill_all_variants = bool(body.get("fill_all_variants", False))
        if fill_all_variants:
            pair_count = len(variant_ids)
            pairs = [
                {"idx": i, "post": posts[i % len(posts)], "variant_id": variant_ids[i]}
                for i in range(pair_count)
            ]
        else:
            pair_count = min(len(posts), len(variant_ids))
            pairs = [
                {"idx": i, "post": posts[i], "variant_id": variant_ids[i]}
                for i in range(pair_count)
            ]

        draft_overrides: Dict[str, str] = body.get("draft_overrides") or {}
        concurrency = max(1, min(20, int(body.get("concurrency", 5))))

        job, cancel_ev = jobs.create(
            "rotate_prompts",
            total=len(pairs),
            label=f"Xoay {len(pairs)} cặp / {len(variant_ids)} prompt",
        )
        job_id = job["id"]
        jobs.update(job_id, message=f"Đang xoay 1-1 ({len(pairs)} cặp, {concurrency} luồng đồng thời)...")

        def _worker():
            t0 = time.time()
            logs.info("REWRITE", f"[Mode Xoay 1-1] Bắt đầu {len(pairs)} cặp xoay vòng ({concurrency} luồng)...")
            collected: List[dict] = []

            def _run_pair(pair: dict) -> Optional[dict]:
                if cancel_ev.is_set():
                    return None
                p = pair["post"]
                vid = pair["variant_id"]
                draft_tpl = draft_overrides.get(vid)
                res = rewriter.rewrite(
                    text=p.get("text", ""),
                    post_id=p["id"],
                    source=p.get("source", ""),
                    provider=body.get("provider"),
                    overrides=body.get("overrides") or {},
                    variant_id=vid,
                    custom_prompt_template=draft_tpl,
                    custom_system_prompt=body.get("custom_system_prompt"),
                    enable_prescreen=bool(body.get("enable_prescreen", True)),
                    enable_clean_output=bool(body.get("enable_clean_output", True)),
                    is_draft=bool(draft_tpl),
                )
                storage.record_rewrite(p["id"], res)
                res["order"] = pair["idx"]
                res["original_text"] = p.get("text", "")
                res["post_source"] = p.get("source", "")
                res["post_author"] = p.get("crawled_from", "")
                res["post_url"] = p.get("url", "")
                return res

            try:
                with ThreadPoolExecutor(max_workers=concurrency) as ex:
                    futures = [ex.submit(_run_pair, pr) for pr in pairs]
                    for fut in as_completed(futures):
                        if cancel_ev.is_set():
                            for f in futures:
                                f.cancel()
                        if fut.cancelled():
                            continue
                        item = fut.result()
                        if item is None:
                            continue
                        collected.append(item)
                        jobs.append_item(
                            job_id,
                            item,
                            message=f"Xong {len(collected)}/{len(pairs)}: {item.get('variant_id')}",
                        )

                collected.sort(key=lambda x: x.get("order", 0))
                analytics = analyze_matrix_results(collected)
                duration_ms = int((time.time() - t0) * 1000)
                storage.save_session(
                    "rotate",
                    {
                        "variant_ids": variant_ids,
                        "items": collected,
                        "analytics": analytics,
                        "duration_ms": duration_ms,
                        "updated_at": time.time(),
                    },
                )
                logs.success(
                    "REWRITE",
                    f"[Mode Xoay 1-1] Hoàn tất {len(collected)}/{len(pairs)} cặp ({duration_ms / 1000:.1f}s) | TB {analytics['avg_score']}đ | Đa dạng {analytics['diversity_score']}%",
                )
                jobs.finish(
                    job_id,
                    result={"variant_ids": variant_ids, "duration_ms": duration_ms},
                    analytics=analytics,
                    cancelled=cancel_ev.is_set(),
                )
            except Exception as e:
                tb = traceback.format_exc()
                logs.error("REWRITE", f"[Mode Xoay 1-1] Lỗi: {e}", details=tb)
                jobs.finish(job_id, error=str(e))

        threading.Thread(target=_worker, daemon=True, name=f"rotate-{job_id}").start()
        return jsonify({"ok": True, "job": job})

    @app.route("/api/sessions", methods=["GET"])
    def api_get_sessions():
        return jsonify({"ok": True, "sessions": storage.get_sessions()})

    @app.route("/api/sessions/<kind>", methods=["PUT"])
    def api_save_session(kind: str):
        body = request.get_json(silent=True) or {}
        storage.save_session(kind, body.get("session"))
        return jsonify({"ok": True})

    @app.route("/api/test/analyze", methods=["POST"])
    def api_analyze_items():
        body = request.get_json(silent=True) or {}
        items = body.get("items") or []
        return jsonify({"ok": True, "analytics": analyze_matrix_results(items)})

    # ------------------------------------------------------------------
    # Jobs & Logs (REST + SSE)
    # ------------------------------------------------------------------
    @app.route("/api/jobs/<job_id>", methods=["GET"])
    def api_get_job(job_id: str):
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Không tìm thấy job"}), 404
        return jsonify({"ok": True, "job": job})

    @app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
    def api_cancel_job(job_id: str):
        ok = jobs.cancel(job_id)
        if ok:
            logs.warning("REWRITE", f"Người dùng yêu cầu dừng job {job_id}")
        return jsonify({"ok": ok})

    @app.route("/api/logs", methods=["GET"])
    def api_get_logs():
        limit = int(request.args.get("limit", 250))
        level = request.args.get("level", "ALL")
        source = request.args.get("source", "ALL")
        since_id = int(request.args.get("since_id", 0))
        return jsonify({
            "ok": True,
            "logs": logs.list_entries(limit=limit, level=level, source=source, since_id=since_id),
        })

    @app.route("/api/logs/stream", methods=["GET"])
    def api_stream_logs():
        return Response(
            logs.stream_sse(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/logs/clear", methods=["POST"])
    def api_clear_logs():
        logs.clear()
        return jsonify({"ok": True})

    return app


def main():
    parser = argparse.ArgumentParser(description="OKXSquare Prompt Tuning Lab (Flask)")
    parser.add_argument("--host", default="127.0.0.1", help="Host bind (mặc định 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5055, help="Port lắng nghe (mặc định 5055)")
    parser.add_argument("--storage", choices=["ram", "disk"], default="ram", help="Lưu trữ crawl (ram | disk)")
    args = parser.parse_args()

    app = create_app(initial_storage_mode=args.storage)
    print(f"\n{'=' * 68}")
    print(f"  OKXSquare PROMPT TUNING LAB")
    print(f"  URL     : http://{args.host}:{args.port}")
    print(f"  Config  : {PROJECT_ROOT / 'config' / 'gemini_config.yaml'}")
    print(f"  Prompts : {PROJECT_ROOT / 'config' / 'prompts'} (G1 + G2)")
    print(f"  Storage : {args.storage.upper()}")
    print(f"{'=' * 68}\n")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

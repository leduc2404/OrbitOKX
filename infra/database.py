"""DatabaseManager cho OKXSquare — SQLite WAL, thread-local pool, atomic claim.

Schema (6 bảng): accounts, posts, dedup_hashes, crawl_history, crawl_state, task_logs.
Pattern WAL/pool/retry/atomic-claim kế thừa từ BinanceSquare/infra/database.py.
Secret material KHÔNG lưu trong DB — chỉ trạng thái vận hành + tham chiếu file.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from threading import Lock, local
from typing import Dict, List, Optional

from infra.logging_config import get_logger
from services.processor.content_tags import build_text_fingerprint, compute_similarity

logger = get_logger(__name__)

# Post status machine: crawled -> processed -> processing -> posted | failed | skipped
STATUS_CRAWLED = "crawled"
STATUS_PROCESSED = "processed"
STATUS_PROCESSING = "processing"
STATUS_POSTED = "posted"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


class DatabaseManager:
    """Connection pooling + WAL mode, tối ưu đọc/ghi đồng thời nhiều thread."""

    def __init__(self, db_path: str = "data/okx_square.db", image_dir: str = "data/images"):
        self.db_path = db_path
        self.image_dir = image_dir
        self.lock = Lock()
        self._local = local()
        self._ensure_db_directory()
        self._init_database()

    # ------------------------------------------------------------------
    # Infrastructure: directory, schema, connection pool, retry
    # ------------------------------------------------------------------
    def _ensure_db_directory(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        if self.image_dir and not os.path.exists(self.image_dir):
            os.makedirs(self.image_dir, exist_ok=True)

    def _init_database(self):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute('PRAGMA journal_mode=WAL')
            cur.execute('PRAGMA synchronous=NORMAL')
            cur.execute('PRAGMA cache_size=10000')
            cur.execute('PRAGMA temp_store=MEMORY')
            cur.execute('PRAGMA auto_vacuum=INCREMENTAL')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id                   INTEGER PRIMARY KEY,
                    name                 TEXT NOT NULL UNIQUE,
                    enabled              INTEGER NOT NULL DEFAULT 1,
                    fingerprint          TEXT,
                    status               TEXT NOT NULL DEFAULT 'active',
                    quarantine_until     INTEGER,
                    consecutive_failures INTEGER DEFAULT 0,
                    cookie_expiry        INTEGER,
                    last_post_at         INTEGER,
                    last_error           TEXT,
                    created_at           INTEGER NOT NULL,
                    updated_at           INTEGER NOT NULL
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    id               TEXT PRIMARY KEY,
                    source           TEXT NOT NULL,
                    url              TEXT,
                    text             TEXT,
                    image_paths      TEXT,
                    hashtags         TEXT,
                    content_hash     TEXT,
                    gemini_output    TEXT,
                    status           TEXT NOT NULL DEFAULT 'crawled',
                    posted_by        TEXT,
                    posted_at        INTEGER,
                    okx_post_id      TEXT,
                    crawled_from     TEXT,
                    create_time      INTEGER,
                    crawled_at       INTEGER NOT NULL,
                    retry_count      INTEGER DEFAULT 0,
                    last_error       TEXT,
                    failed_at        INTEGER,
                    claimed_at       INTEGER,
                    claimed_by       TEXT,
                    prompt_id        TEXT,
                    primary_coin     TEXT,
                    fingerprint_json TEXT
                )
            ''')

            existing_cols = {
                r['name'] for r in cur.execute('PRAGMA table_info(posts)').fetchall()
            }
            for col_name, col_type in (
                ('claimed_at', 'INTEGER'),
                ('claimed_by', 'TEXT'),
                ('prompt_id', 'TEXT'),
                ('primary_coin', 'TEXT'),
                ('fingerprint_json', 'TEXT'),
            ):
                if col_name not in existing_cols:
                    cur.execute(f'ALTER TABLE posts ADD COLUMN {col_name} {col_type}')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS posted_fingerprints (
                    post_id          TEXT PRIMARY KEY,
                    posted_by        TEXT,
                    posted_at        INTEGER NOT NULL,
                    primary_coin     TEXT,
                    fingerprint_json TEXT NOT NULL
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS dedup_hashes (
                    hash       TEXT PRIMARY KEY,
                    post_id    TEXT NOT NULL,
                    source     TEXT,
                    first_seen INTEGER NOT NULL
                )
            ''')

            # Bảng chống trùng lặp thuần theo ID (tồn tại dài hạn, không bị xóa khi cleanup_expired_posts)
            cur.execute('''
                CREATE TABLE IF NOT EXISTS seen_post_ids (
                    source     TEXT NOT NULL,
                    post_id    TEXT NOT NULL,
                    first_seen INTEGER NOT NULL,
                    last_seen  INTEGER NOT NULL,
                    PRIMARY KEY (source, post_id)
                ) WITHOUT ROWID
            ''')

            # Tự động đồng bộ các ID đã có sẵn trong bảng posts sang seen_post_ids (an toàn khi nâng cấp DB cũ)
            cur.execute('''
                INSERT OR IGNORE INTO seen_post_ids (source, post_id, first_seen, last_seen)
                SELECT LOWER(COALESCE(source, 'unknown')), id, COALESCE(crawled_at, 0), COALESCE(crawled_at, 0)
                FROM posts
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS crawl_history (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    source         TEXT NOT NULL,
                    author_id      TEXT,
                    started_at     INTEGER NOT NULL,
                    finished_at    INTEGER,
                    posts_found    INTEGER DEFAULT 0,
                    posts_new      INTEGER DEFAULT 0,
                    posts_dup      INTEGER DEFAULT 0,
                    last_item_time INTEGER,
                    status         TEXT DEFAULT 'ok',
                    error          TEXT
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS crawl_state (
                    source             TEXT PRIMARY KEY,
                    last_crawl_time_ms INTEGER,
                    authors_json       TEXT,
                    updated_at         INTEGER
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS task_logs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          INTEGER NOT NULL,
                    level       TEXT NOT NULL,
                    component   TEXT,
                    account_id  INTEGER,
                    post_id     TEXT,
                    event       TEXT,
                    message     TEXT,
                    stack_trace TEXT
                )
            ''')

            for stmt in (
                'CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)',
                'CREATE INDEX IF NOT EXISTS idx_posts_content_hash ON posts(content_hash)',
                'CREATE INDEX IF NOT EXISTS idx_posts_create_time ON posts(create_time)',
                'CREATE INDEX IF NOT EXISTS idx_posts_crawled_from ON posts(crawled_from)',
                'CREATE INDEX IF NOT EXISTS idx_posts_retry ON posts(status, retry_count, failed_at)',
                'CREATE INDEX IF NOT EXISTS idx_posts_processing_claimed ON posts(status, claimed_at)',
                'CREATE INDEX IF NOT EXISTS idx_posted_fp_at ON posted_fingerprints(posted_at)',
                'CREATE INDEX IF NOT EXISTS idx_posted_fp_coin ON posted_fingerprints(primary_coin, posted_at)',
                'CREATE INDEX IF NOT EXISTS idx_seen_post_id ON seen_post_ids(post_id)',
                'CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_post_ids(first_seen)',
                'CREATE INDEX IF NOT EXISTS idx_crawl_source_time ON crawl_history(source, started_at)',
                'CREATE INDEX IF NOT EXISTS idx_task_logs_ts ON task_logs(ts)',
                'CREATE INDEX IF NOT EXISTS idx_task_logs_account ON task_logs(account_id, ts)',
            ):
                cur.execute(stmt)

            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    def _get_connection(self, timeout: float = 30.0) -> sqlite3.Connection:
        conn = getattr(self._local, 'connection', None)
        if conn is not None:
            try:
                conn.execute('SELECT 1')
                return conn
            except sqlite3.Error:
                self._local.connection = None
        conn = sqlite3.connect(
            self.db_path, timeout=timeout,
            isolation_level=None, check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout = 30000')
        self._local.connection = conn
        return conn

    @contextmanager
    def get_connection(self):
        conn = self._get_connection()
        try:
            yield conn
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    def execute_with_retry(self, query: str, params: tuple = (), max_retries: int = 5) -> Optional[list]:
        for attempt in range(max_retries):
            try:
                with self.get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute(query, params)
                    if query.strip().upper().startswith('SELECT'):
                        return cur.fetchall()
                    conn.commit()
                    return None
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
                else:
                    raise

    def close(self):
        conn = getattr(self._local, 'connection', None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.connection = None

    def reset_database(self):
        self.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self._init_database()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def compute_content_hash(text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        normalized = re.sub(r'\s+', ' ', text).strip().lower()
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

    @staticmethod
    def _paths(value: Optional[str]) -> List[str]:
        return [p.strip() for p in (value or '').split('|') if p.strip()]

    @staticmethod
    def _now() -> int:
        return int(time.time())

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    def upsert_account(self, account_id: int, name: str,
                       enabled: bool = True, fingerprint: str = "") -> None:
        now = self._now()
        self.execute_with_retry('''
            INSERT INTO accounts (id, name, enabled, fingerprint, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, enabled=excluded.enabled,
                fingerprint=excluded.fingerprint,
                updated_at=excluded.updated_at
        ''', (account_id, name, int(enabled), fingerprint, now, now))

    def get_account(self, account_id: int) -> Optional[Dict]:
        rows = self.execute_with_retry('SELECT * FROM accounts WHERE id=?', (account_id,))
        return dict(rows[0]) if rows else None

    def get_accounts(self, enabled_only: bool = False) -> List[Dict]:
        q = 'SELECT * FROM accounts'
        if enabled_only:
            q += ' WHERE enabled=1'
        q += ' ORDER BY id'
        return [dict(r) for r in (self.execute_with_retry(q) or [])]

    def update_account_status(self, account_id: int, status: str,
                              quarantine_until: Optional[int] = None,
                              last_error: Optional[str] = None) -> None:
        self.execute_with_retry('''
            UPDATE accounts SET status=?, quarantine_until=?, last_error=?, updated_at=?
            WHERE id=?
        ''', (status, quarantine_until, last_error, self._now(), account_id))

    def set_account_cookie_expiry(self, account_id: int, expiry: Optional[int]) -> None:
        self.execute_with_retry(
            'UPDATE accounts SET cookie_expiry=?, updated_at=? WHERE id=?',
            (expiry, self._now(), account_id))

    def increment_account_failure(self, account_id: int, last_error: str = "") -> int:
        self.execute_with_retry('''
            UPDATE accounts SET consecutive_failures=consecutive_failures+1,
                last_error=?, updated_at=? WHERE id=?
        ''', (last_error, self._now(), account_id))
        row = self.get_account(account_id)
        return row['consecutive_failures'] if row else 0

    def reset_account_failures(self, account_id: int) -> None:
        self.execute_with_retry('''
            UPDATE accounts SET consecutive_failures=0, status='active',
                quarantine_until=NULL, updated_at=? WHERE id=?
        ''', (self._now(), account_id))

    def touch_account_post(self, account_id: int) -> None:
        now = self._now()
        self.execute_with_retry(
            'UPDATE accounts SET last_post_at=?, updated_at=? WHERE id=?',
            (now, now, account_id))

    # ------------------------------------------------------------------
    # dedup (thuần theo post_id trên DB — atomic, tách nguồn, bền vững qua cleanup)
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_source(source: Optional[str]) -> str:
        return str(source or "unknown").strip().lower() or "unknown"

    def batch_check_existing_posts(self, post_ids: List[str],
                                   source: Optional[str] = None) -> set:
        """Kiểm tra hàng loạt post_id đã tồn tại trong seen_post_ids hoặc posts (chunk 400).

        Nếu truyền `source`, tra cứu trực tiếp trên clustered PK (source, post_id) — O(log N),
        không xung đột ID giữa các nguồn khác nhau và vẫn nhớ ID sau khi cleanup_expired_posts.
        """
        clean_ids = list(dict.fromkeys(str(pid).strip() for pid in (post_ids or []) if str(pid).strip()))
        if not clean_ids:
            return set()
        existing = set()
        src_norm = self._norm_source(source) if source else None
        for i in range(0, len(clean_ids), 400):
            chunk = clean_ids[i:i + 400]
            placeholders = ','.join('?' for _ in chunk)
            if src_norm:
                rows_seen = self.execute_with_retry(
                    f'SELECT post_id FROM seen_post_ids WHERE source=? AND post_id IN ({placeholders})',
                    (src_norm, *chunk),
                )
                existing.update(r['post_id'] for r in (rows_seen or []))
                rows_posts = self.execute_with_retry(
                    f'SELECT id FROM posts WHERE LOWER(source)=? AND id IN ({placeholders})',
                    (src_norm, *chunk),
                )
                existing.update(r['id'] for r in (rows_posts or []))
            else:
                rows_seen = self.execute_with_retry(
                    f'SELECT post_id FROM seen_post_ids WHERE post_id IN ({placeholders})',
                    tuple(chunk),
                )
                existing.update(r['post_id'] for r in (rows_seen or []))
                rows_posts = self.execute_with_retry(
                    f'SELECT id FROM posts WHERE id IN ({placeholders})',
                    tuple(chunk),
                )
                existing.update(r['id'] for r in (rows_posts or []))
        return existing

    def content_hash_exists(self, content_hash: Optional[str]) -> bool:
        if not content_hash:
            return False
        rows = self.execute_with_retry(
            'SELECT 1 FROM dedup_hashes WHERE hash=? LIMIT 1', (content_hash,))
        return bool(rows)

    def is_duplicate(self, post_id: str, content_hash: Optional[str] = None,
                     source: Optional[str] = None) -> bool:
        """Kiểm tra trùng lặp thuần theo post_id (không so trùng text)."""
        pid = str(post_id or "").strip()
        if not pid:
            return True
        return pid in self.batch_check_existing_posts([pid], source=source)

    def register_hash(self, content_hash: Optional[str], post_id: str, source: str) -> None:
        if not content_hash:
            return
        self.execute_with_retry('''
            INSERT OR IGNORE INTO dedup_hashes (hash, post_id, source, first_seen)
            VALUES (?, ?, ?, ?)
        ''', (content_hash, post_id, source, self._now()))

    def prune_seen_post_ids(self, retention_days: int = 90) -> int:
        """Dọn bớt ID cũ hơn N ngày trong seen_post_ids (mặc định giữ 90 ngày, gọi chủ động)."""
        cutoff = self._now() - int(retention_days) * 86400
        with self.get_connection() as conn:
            cur = conn.execute('DELETE FROM seen_post_ids WHERE last_seen < ?', (cutoff,))
            conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # posts: insert + claim + mark
    # ------------------------------------------------------------------
    def insert_post(self, post_id: str, source: str, url: str = "", text: str = "",
                    image_paths=None, hashtags: str = "", create_time: Optional[int] = None,
                    crawled_from: str = "", content_hash: Optional[str] = None,
                    dedup_content: bool = True) -> bool:
        """Atomic ID + content_hash deduplication trong BEGIN IMMEDIATE.

        Chặn trùng 100% theo (source, post_id) qua seen_post_ids và chặn trùng nội dung
        xuyên nguồn trong 24h qua bảng dedup_hashes. Trả True nếu là bài mới.
        """
        pid = str(post_id or "").strip()
        if not pid:
            return False
        src_norm = self._norm_source(source)
        if isinstance(image_paths, (list, tuple)):
            image_paths = '|'.join(image_paths)
        if content_hash is None:
            content_hash = self.compute_content_hash(text)
        now = self._now()

        for attempt in range(5):
            try:
                with self.get_connection() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    cur_seen = conn.execute('''
                        INSERT OR IGNORE INTO seen_post_ids (source, post_id, first_seen, last_seen)
                        VALUES (?, ?, ?, ?)
                    ''', (src_norm, pid, now, now))
                    if cur_seen.rowcount == 0:
                        # Đã từng thấy (source, post_id) này rồi -> cập nhật last_seen và bỏ qua
                        conn.execute(
                            'UPDATE seen_post_ids SET last_seen=? WHERE source=? AND post_id=?',
                            (now, src_norm, pid),
                        )
                        conn.commit()
                        return False

                    if dedup_content and content_hash:
                        dup_row = conn.execute(
                            'SELECT post_id FROM dedup_hashes WHERE hash=? AND first_seen >= ? LIMIT 1',
                            (content_hash, now - 86400),
                        ).fetchone()
                        if dup_row:
                            conn.commit()
                            return False

                    cur = conn.execute('''
                        INSERT OR IGNORE INTO posts
                            (id, source, url, text, image_paths, hashtags, content_hash,
                             status, crawled_from, create_time, crawled_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (pid, src_norm, url, text, image_paths or '', hashtags, content_hash,
                          STATUS_CRAWLED, crawled_from, create_time, now))
                    inserted = cur.rowcount > 0
                    if inserted and content_hash:
                        conn.execute('''
                            INSERT OR REPLACE INTO dedup_hashes (hash, post_id, source, first_seen)
                            VALUES (?, ?, ?, ?)
                        ''', (content_hash, pid, src_norm, now))
                    conn.commit()
                return inserted
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 4:
                    time.sleep(0.1 * (2 ** attempt))
                else:
                    raise
        return False

    def _ready_filter(self, priority_profiles: Optional[List[str]]):
        clauses = ["status = ?"]
        params: list = [STATUS_PROCESSED]
        if priority_profiles:
            clauses.append("crawled_from IN (%s)" % ','.join('?' for _ in priority_profiles))
            params.extend(priority_profiles)
        return " AND ".join(clauses), params

    def get_ready_to_post_count(self) -> int:
        """Đếm số bài đã xử lý sẵn sàng đăng (status='processed')."""
        rows = self.execute_with_retry(
            "SELECT COUNT(*) AS c FROM posts WHERE status=?", (STATUS_PROCESSED,))
        return int(rows[0]['c']) if rows else 0

    def get_unprocessed_count(self) -> int:
        """Đếm số bài thô vừa crawl đang chờ Gemini xử lý (status='crawled')."""
        rows = self.execute_with_retry(
            "SELECT COUNT(*) AS c FROM posts WHERE status=? AND text IS NOT NULL AND text != ''",
            (STATUS_CRAWLED,))
        return int(rows[0]['c']) if rows else 0

    def get_unprocessed_posts(self, limit: Optional[int] = None) -> List[Dict]:
        """Lấy bài thô chưa xử lý Gemini — LUÔN ưu tiên bài MỚI NHẤT trước (create_time DESC)."""
        q = '''
            SELECT id, source, url, text, image_paths, hashtags, crawled_from,
                   create_time, crawled_at, retry_count
            FROM posts
            WHERE status = ?
              AND text IS NOT NULL AND text != ''
            ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC
        '''
        params: list = [STATUS_CRAWLED]
        if limit:
            q += ' LIMIT ?'
            params.append(int(limit))
        rows = self.execute_with_retry(q, tuple(params))
        return [dict(r) for r in (rows or [])]

    def get_posts_ready_to_post(self, limit: Optional[int] = None) -> List[Dict]:
        where, params = self._ready_filter(None)
        q = (f'SELECT * FROM posts WHERE {where} '
             'ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC')
        if limit:
            q += ' LIMIT ?'
            params.append(limit)
        return [dict(r) for r in (self.execute_with_retry(q, tuple(params)) or [])]

    def claim_post(self, worker_name: str,
                   priority_profiles: Optional[List[str]] = None) -> Optional[Dict]:
        """ATOMIC: BEGIN IMMEDIATE -> SELECT 1 bài processed MỚI NHẤT -> UPDATE processing -> COMMIT."""
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = None
                if priority_profiles:
                    where, params = self._ready_filter(priority_profiles)
                    row = conn.execute(
                        f'SELECT * FROM posts WHERE {where} '
                        'ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC LIMIT 1',
                        tuple(params)).fetchone()
                if not row:
                    where, params = self._ready_filter(None)
                    row = conn.execute(
                        f'SELECT * FROM posts WHERE {where} '
                        'ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC LIMIT 1',
                        tuple(params)).fetchone()
                if not row:
                    conn.rollback()
                    return None
                now = self._now()
                wname = str(worker_name or '')
                conn.execute(
                    "UPDATE posts SET status=?, claimed_at=?, claimed_by=? WHERE id=?",
                    (STATUS_PROCESSING, now, wname, row['id']),
                )
                conn.commit()
                res = dict(row)
                res['status'] = STATUS_PROCESSING
                res['claimed_at'] = now
                res['claimed_by'] = wname
                return res
        except Exception as e:
            logger.error("claim_post error: %s", e)
            return None

    def claim_post_smart(
        self,
        worker_name: str,
        priority_profiles: Optional[List[str]] = None,
        recent_similarity_window_hours: int = 12,
        similarity_threshold: float = 0.58,
        coin_cooldown_minutes: int = 30,
    ) -> Optional[Dict]:
        """ATOMIC smart claim: tránh trùng chủ đề gần nhau giữa nhiều account.

        1. Đọc danh sách bài `processed` mới nhất (tối đa 40 bài).
        2. So sánh fingerprint với `posted_fingerprints` trong `recent_similarity_window_hours`:
           - Nếu similarity >= `similarity_threshold`: tự động skip bài trùng và xét bài tiếp theo.
        3. Tránh chọn coin vừa được đăng trong `coin_cooldown_minutes` nếu còn bài coin khác;
           fallback sang bài không trùng nội dung mới nhất nếu toàn bộ queue chỉ còn coin đó.
        4. Đánh dấu `processing`, ghi `claimed_at` & `claimed_by` trong cùng transaction.
        """
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                now = self._now()
                wname = str(worker_name or '')

                candidates = []
                seen_ids = set()
                if priority_profiles:
                    where, params = self._ready_filter(priority_profiles)
                    for r in conn.execute(
                        f'SELECT * FROM posts WHERE {where} '
                        'ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC LIMIT 30',
                        tuple(params),
                    ).fetchall():
                        candidates.append(r)
                        seen_ids.add(r['id'])

                where_all, params_all = self._ready_filter(None)
                for r in conn.execute(
                    f'SELECT * FROM posts WHERE {where_all} '
                    'ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC LIMIT 40',
                    tuple(params_all),
                ).fetchall():
                    if r['id'] not in seen_ids:
                        candidates.append(r)
                        seen_ids.add(r['id'])

                if not candidates:
                    conn.rollback()
                    return None

                sim_cutoff = now - max(1, int(recent_similarity_window_hours)) * 3600
                recent_fps = conn.execute(
                    'SELECT post_id, posted_by, posted_at, primary_coin, fingerprint_json '
                    'FROM posted_fingerprints WHERE posted_at >= ? '
                    'ORDER BY posted_at DESC LIMIT 120',
                    (sim_cutoff,),
                ).fetchall()

                coin_cutoff = now - max(0, int(coin_cooldown_minutes)) * 60
                hot_coins = {
                    (r['primary_coin'] or '').upper()
                    for r in recent_fps
                    if int(r['posted_at'] or 0) >= coin_cutoff and (r['primary_coin'] or '').strip()
                }
                # Cũng coi coin đang được worker khác giữ trong processing (chưa post xong) là hot
                for r in conn.execute(
                    'SELECT primary_coin FROM posts WHERE status=? AND claimed_at >= ? AND primary_coin IS NOT NULL',
                    (STATUS_PROCESSING, coin_cutoff),
                ).fetchall():
                    c = (r['primary_coin'] or '').strip().upper()
                    if c:
                        hot_coins.add(c)

                last_self_coin = ''
                if wname:
                    last_row = conn.execute(
                        'SELECT primary_coin FROM posted_fingerprints WHERE posted_by=? ORDER BY posted_at DESC LIMIT 1',
                        (wname,),
                    ).fetchone()
                    if last_row and last_row['primary_coin']:
                        last_self_coin = str(last_row['primary_coin']).strip().upper()

                chosen_ideal = None
                chosen_fallback = None
                skipped_dups = []

                for row in candidates:
                    body = row['gemini_output'] or row['text'] or ''
                    fp_raw = row['fingerprint_json']
                    cand_fp = fp_raw if fp_raw else build_text_fingerprint(body)
                    cand_coin = (row['primary_coin'] or '').strip().upper()
                    if not cand_coin and isinstance(cand_fp, dict):
                        cand_coin = str(cand_fp.get('primary_coin') or '').upper()

                    max_sim = 0.0
                    for prev in recent_fps:
                        sim = compute_similarity(cand_fp, prev['fingerprint_json'])
                        if sim > max_sim:
                            max_sim = sim
                        if max_sim >= similarity_threshold:
                            break

                    if max_sim >= similarity_threshold:
                        skipped_dups.append((row['id'], f"near_duplicate:{max_sim:.2f}"))
                        continue

                    if chosen_fallback is None:
                        chosen_fallback = row

                    coin_conflict = bool(
                        cand_coin and (cand_coin in hot_coins or (last_self_coin and cand_coin == last_self_coin))
                    )
                    if not coin_conflict and chosen_ideal is None:
                        chosen_ideal = row
                        break

                for dup_id, reason in skipped_dups:
                    conn.execute(
                        "UPDATE posts SET status=?, last_error=?, claimed_at=NULL, claimed_by=NULL WHERE id=?",
                        (STATUS_SKIPPED, reason, dup_id),
                    )

                chosen = chosen_ideal or chosen_fallback
                if not chosen:
                    conn.commit()
                    return None

                cur = conn.execute(
                    "UPDATE posts SET status=?, claimed_at=?, claimed_by=? WHERE id=? AND status=?",
                    (STATUS_PROCESSING, now, wname, chosen['id'], STATUS_PROCESSED),
                )
                if cur.rowcount != 1:
                    conn.commit()
                    return None
                conn.commit()
                res = dict(chosen)
                res['status'] = STATUS_PROCESSING
                res['claimed_at'] = now
                res['claimed_by'] = wname
                return res
        except Exception as e:
            logger.error("claim_post_smart error: %s", e)
            return None

    def verify_claimed(self, post_id: str, worker_name: str) -> bool:
        """Double-check bài vẫn thuộc quyền claim hợp lệ của worker trước khi đăng."""
        rows = self.execute_with_retry(
            "SELECT status, claimed_by FROM posts WHERE id=?",
            (post_id,),
        )
        if not rows:
            return False
        row = rows[0]
        if row['status'] != STATUS_PROCESSING:
            return False
        owner = row['claimed_by'] or ''
        return owner == '' or owner == str(worker_name or '')

    def get_and_claim_posts_batch(self, count: int,
                                  priority_profiles: Optional[List[str]] = None) -> List[Dict]:
        """ATOMIC batch claim MỚI NHẤT trước cho round-robin distribution."""
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                rows = []
                if priority_profiles:
                    where, params = self._ready_filter(priority_profiles)
                    rows.extend(conn.execute(
                        f'SELECT * FROM posts WHERE {where} '
                        'ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC LIMIT ?',
                        tuple(params) + (count,)).fetchall())
                remaining = count - len(rows)
                if remaining > 0:
                    where, params = self._ready_filter(None)
                    if rows:
                        ids = [r['id'] for r in rows]
                        where += " AND id NOT IN (%s)" % ','.join('?' for _ in ids)
                        params.extend(ids)
                    rows.extend(conn.execute(
                        f'SELECT * FROM posts WHERE {where} '
                        'ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC LIMIT ?',
                        tuple(params) + (remaining,)).fetchall())
                if not rows:
                    conn.rollback()
                    return []
                now = self._now()
                ids = [r['id'] for r in rows]
                conn.execute(
                    "UPDATE posts SET status=?, claimed_at=? WHERE id IN (%s)" %
                    (','.join('?' for _ in ids)),
                    (STATUS_PROCESSING, now, *ids))
                conn.commit()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("batch claim error: %s", e)
            return []

    def mark_post_processing(self, post_id: str, worker_name: str = "") -> bool:
        self.execute_with_retry(
            "UPDATE posts SET status=?, claimed_at=?, claimed_by=? WHERE id=? AND status NOT IN (?,?)",
            (STATUS_PROCESSING, self._now(), str(worker_name or ''), post_id, STATUS_POSTED, STATUS_PROCESSING))
        return True

    def mark_post_processed(
        self,
        post_id: str,
        gemini_output: Optional[str] = None,
        prompt_id: Optional[str] = None,
        primary_coin: Optional[str] = None,
        hashtags: Optional[str] = None,
        fingerprint_json: Optional[str] = None,
    ) -> bool:
        sets = ["status=?", "claimed_at=NULL", "claimed_by=NULL"]
        params: list = [STATUS_PROCESSED]
        for col, val in (
            ("gemini_output", gemini_output),
            ("prompt_id", prompt_id),
            ("primary_coin", primary_coin),
            ("hashtags", hashtags),
            ("fingerprint_json", fingerprint_json),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        params.append(post_id)
        self.execute_with_retry(
            f"UPDATE posts SET {', '.join(sets)} WHERE id=?",
            tuple(params),
        )
        return True

    def mark_post_posted(self, post_id: str, posted_by: str,
                         okx_post_id: Optional[str] = None) -> bool:
        try:
            with self.get_connection() as conn:
                now = self._now()
                row = conn.execute(
                    "SELECT primary_coin, fingerprint_json, gemini_output, text FROM posts WHERE id=?",
                    (post_id,),
                ).fetchone()
                fp_json = None
                p_coin = ""
                if row:
                    p_coin = row['primary_coin'] or ''
                    fp_json = row['fingerprint_json']
                    if not fp_json:
                        body = row['gemini_output'] or row['text'] or ''
                        if body:
                            fp = build_text_fingerprint(body)
                            fp_json = json.dumps(fp, ensure_ascii=False)
                            if not p_coin:
                                p_coin = fp.get('primary_coin', '')
                if fp_json:
                    conn.execute('''
                        INSERT OR REPLACE INTO posted_fingerprints
                            (post_id, posted_by, posted_at, primary_coin, fingerprint_json)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (post_id, posted_by, now, p_coin, fp_json))

                self._unlink_post_images(post_id, conn)
                conn.execute('''
                    UPDATE posts SET status=?, posted_by=?, posted_at=?, okx_post_id=?,
                        claimed_at=NULL, claimed_by=NULL,
                        gemini_output=NULL, image_paths=NULL, text=NULL
                    WHERE id=?
                ''', (STATUS_POSTED, posted_by, now, okx_post_id, post_id))
                conn.commit()
            return True
        except Exception as e:
            logger.error("mark_post_posted %s: %s", post_id, e)
            return False

    def mark_post_failed(self, post_id: str, error: str = "",
                         increment_retry: bool = True) -> bool:
        now = self._now()
        if increment_retry:
            self.execute_with_retry('''
                UPDATE posts SET status=?, last_error=?, failed_at=?, retry_count=retry_count+1
                WHERE id=?
            ''', (STATUS_FAILED, error, now, post_id))
        else:
            self.execute_with_retry(
                "UPDATE posts SET status=?, last_error=?, failed_at=? WHERE id=?",
                (STATUS_FAILED, error, now, post_id))
        return True

    def mark_post_skipped(self, post_id: str, reason: str = "") -> bool:
        try:
            with self.get_connection() as conn:
                self._unlink_post_images(post_id, conn)
                conn.execute(
                    "UPDATE posts SET status=?, last_error=?, image_paths=NULL WHERE id=?",
                    (STATUS_SKIPPED, reason, post_id))
                conn.commit()
            return True
        except Exception as e:
            logger.error("mark_post_skipped %s: %s", post_id, e)
            return False

    def release_post(self, post_id: str) -> bool:
        """Trả bài về processed để worker khác claim lại."""
        self.execute_with_retry(
            "UPDATE posts SET status=?, claimed_at=NULL, claimed_by=NULL WHERE id=? AND status=?",
            (STATUS_PROCESSED, post_id, STATUS_PROCESSING))
        return True

    def get_failed_posts_for_retry(self, cooldown_minutes: int = 5,
                                   max_retries: int = 3,
                                   limit: Optional[int] = None) -> List[Dict]:
        """Lấy bài lỗi Gemini đủ cooldown để retry — ưu tiên bài MỚI NHẤT trước."""
        cutoff = self._now() - int(cooldown_minutes) * 60
        q = '''
            SELECT * FROM posts
            WHERE status=?
              AND text IS NOT NULL AND text != ''
              AND (gemini_output IS NULL OR gemini_output = '')
              AND COALESCE(retry_count, 0) < ?
              AND (failed_at IS NULL OR failed_at <= ?)
            ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC,
                     COALESCE(retry_count, 0) ASC,
                     COALESCE(failed_at, 0) ASC
        '''
        params: list = [STATUS_FAILED, int(max_retries), cutoff]
        if limit:
            q += ' LIMIT ?'
            params.append(int(limit))
        rows = self.execute_with_retry(q, tuple(params))
        return [dict(r) for r in (rows or [])]

    def get_retry_stats(self) -> Dict:
        """Thống kê bài đang chờ retry và bài đã bị skip vĩnh viễn."""
        failed_rows = self.execute_with_retry(
            "SELECT COALESCE(retry_count, 0) AS rc, COUNT(*) AS c FROM posts WHERE status=? GROUP BY rc ORDER BY rc",
            (STATUS_FAILED,),
        ) or []
        by_retry = {int(r['rc']): int(r['c']) for r in failed_rows}
        skipped_rows = self.execute_with_retry(
            "SELECT COUNT(*) AS c FROM posts WHERE status=?", (STATUS_SKIPPED,),
        ) or []
        return {
            'failed_awaiting_retry': sum(by_retry.values()),
            'permanently_skipped': int(skipped_rows[0]['c']) if skipped_rows else 0,
            'by_retry_count': by_retry,
        }

    def cleanup_processing_timeout(self, timeout_seconds: int = 300) -> int:
        """Khôi phục bài bị treo ở trạng thái 'processing' (timeout_seconds=0: recovery tất cả khi khởi động)."""
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                timeout_seconds = max(0, int(timeout_seconds))
                if timeout_seconds > 0:
                    stale_cutoff = self._now() - timeout_seconds
                    stale_filter = "AND (claimed_at IS NULL OR claimed_at <= ?)"
                    extra_params: tuple = (stale_cutoff,)
                else:
                    stale_filter = ""
                    extra_params = ()

                c1 = conn.execute(f'''
                    UPDATE posts SET status = ?, claimed_at = NULL, claimed_by = NULL
                    WHERE status = ? AND gemini_output IS NOT NULL AND gemini_output != '' {stale_filter}
                ''', (STATUS_PROCESSED, STATUS_PROCESSING, *extra_params)).rowcount
                c2 = conn.execute(f'''
                    UPDATE posts SET status = ?, claimed_at = NULL, claimed_by = NULL
                    WHERE status = ? AND (gemini_output IS NULL OR gemini_output = '') {stale_filter}
                ''', (STATUS_CRAWLED, STATUS_PROCESSING, *extra_params)).rowcount
                conn.commit()
                total = c1 + c2
                if total > 0:
                    logger.info("[CLEANUP] Đã khôi phục %d bài treo processing (%d->processed, %d->crawled, timeout=%ds)",
                                total, c1, c2, timeout_seconds)
                return total
        except Exception as e:
            logger.error("cleanup_processing_timeout: %s", e)
            return 0
            return 0

    @staticmethod
    def _normalized_path(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def _referenced_image_paths(self, conn: sqlite3.Connection) -> set:
        referenced = set()
        rows = conn.execute('''
            SELECT image_paths FROM posts
            WHERE image_paths IS NOT NULL AND image_paths != ''
        ''').fetchall()
        for row in rows:
            for path in self._paths(row['image_paths']):
                referenced.add(self._normalized_path(path))
        return referenced

    def _remove_unreferenced_images(self, candidates: Dict[str, str],
                                    referenced: set) -> Dict[str, int]:
        """Xóa ảnh chỉ khi không còn bài viết nào trong DB tham chiếu (ref-count sau commit)."""
        images = 0
        errors = 0
        freed = 0
        for normalized, path in candidates.items():
            if normalized in referenced or not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
                images += 1
                freed += size
            except OSError as exc:
                errors += 1
                logger.warning("Không xóa được ảnh không tham chiếu path=%s error=%s", path, exc)
        return {'images': images, 'errors': errors, 'freed_bytes': freed}

    def _orphan_sweep(self, retention_seconds: int) -> Dict[str, int]:
        """Quét dọn ảnh mồ côi trong image_dir cũ hơn retention_seconds mà DB không tham chiếu."""
        candidates: Dict[str, str] = {}
        if self.image_dir and os.path.isdir(self.image_dir):
            cutoff = time.time() - max(0, int(retention_seconds))
            for name in os.listdir(self.image_dir):
                path = os.path.join(self.image_dir, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) <= cutoff:
                        candidates[self._normalized_path(path)] = path
                except OSError:
                    continue
        if not candidates:
            return {'images': 0, 'errors': 0, 'freed_bytes': 0}
        with self.get_connection() as conn:
            referenced = self._referenced_image_paths(conn)
        return self._remove_unreferenced_images(candidates, referenced)

    def _unlink_post_images(self, post_id: str, conn: sqlite3.Connection) -> int:
        row = conn.execute('SELECT image_paths FROM posts WHERE id=?', (post_id,)).fetchone()
        if not row:
            return 0
        paths = self._paths(row['image_paths'])
        if not paths:
            return 0
        removed = 0
        for path in paths:
            other = conn.execute(
                "SELECT 1 FROM posts WHERE id!=? AND image_paths LIKE ? LIMIT 1",
                (post_id, f'%{path}%')).fetchone()
            if other:
                continue
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        return removed

    def is_image_referenced(self, path: str) -> bool:
        """True nếu file ảnh còn được bất kỳ dòng nào trong bảng posts tham chiếu."""
        if not path:
            return False
        target = self._normalized_path(path)
        with self.get_connection() as conn:
            return target in self._referenced_image_paths(conn)

    def delete_stale_crawled_posts(self, keep_newest: int = 0,
                                   min_age_seconds: int = 0) -> int:
        """Dọn bài thô (status='crawled') tồn cũ trước chu kỳ crawl mới để luôn ưu tiên bài MỚI NHẤT.

        Nếu min_age_seconds > 0, chỉ dọn các bài đã crawl quá min_age_seconds (tránh xoá oan bài vừa crawl).
        Lưu ý: KHÔNG xóa bảng seen_post_ids nên các ID đã từng crawl sẽ không bao giờ bị crawl lặp.
        """
        candidates: Dict[str, str] = {}
        deleted = 0
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if min_age_seconds and int(min_age_seconds) > 0:
                    cutoff = self._now() - int(min_age_seconds)
                    rows = conn.execute('''
                        SELECT id, image_paths FROM posts
                        WHERE status = ? AND crawled_at <= ?
                        ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC
                    ''', (STATUS_CRAWLED, cutoff)).fetchall()
                else:
                    rows = conn.execute('''
                        SELECT id, image_paths FROM posts
                        WHERE status = ?
                        ORDER BY COALESCE(create_time, crawled_at * 1000, 0) DESC, id DESC
                    ''', (STATUS_CRAWLED,)).fetchall()
                to_drop = rows[max(0, int(keep_newest)):]
                ids = [r['id'] for r in to_drop]
                for r in to_drop:
                    for path in self._paths(r['image_paths']):
                        candidates[self._normalized_path(path)] = path
                for i in range(0, len(ids), 400):
                    chunk = ids[i:i + 400]
                    placeholders = ','.join('?' for _ in chunk)
                    deleted += conn.execute(
                        f'DELETE FROM posts WHERE status=? AND id IN ({placeholders})',
                        (STATUS_CRAWLED, *chunk),
                    ).rowcount
                conn.commit()

            if candidates:
                with self.get_connection() as conn:
                    referenced = self._referenced_image_paths(conn)
                self._remove_unreferenced_images(candidates, referenced)
            if deleted > 0:
                logger.info("[CLEANUP] Dọn %d bài thô (crawled) tồn cũ để đón bài mới nhất", deleted)
            return deleted
        except Exception as e:
            logger.error("delete_stale_crawled_posts: %s", e)
            return 0

    def cleanup_expired_posts(self, retention_seconds: int = 86400) -> int:
        """Dọn bài hết hạn (>= retention_seconds) kiểu BinanceSquare:
        1. Atomic DELETE trong BEGIN IMMEDIATE (chunk 400). KHÔNG xóa seen_post_ids!
        2. Ref-count xóa ảnh không còn tham chiếu sau commit.
        3. Quét ảnh mồ côi (_orphan_sweep) trong data/images/.
        4. WAL checkpoint + incremental_vacuum trả dung lượng đĩa.
        """
        retention_seconds = max(0, int(retention_seconds))
        cutoff = self._now() - retention_seconds
        candidates: Dict[str, str] = {}
        deleted = 0
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                rows = conn.execute(
                    'SELECT id, image_paths FROM posts WHERE crawled_at <= ?', (cutoff,)
                ).fetchall()
                ids = [r['id'] for r in rows]
                for r in rows:
                    for path in self._paths(r['image_paths']):
                        candidates[self._normalized_path(path)] = path
                for i in range(0, len(ids), 400):
                    chunk = ids[i:i + 400]
                    placeholders = ','.join('?' for _ in chunk)
                    deleted += conn.execute(
                        f'DELETE FROM posts WHERE crawled_at <= ? AND id IN ({placeholders})',
                        (cutoff, *chunk),
                    ).rowcount
                conn.execute(
                    'DELETE FROM dedup_hashes WHERE first_seen <= ?',
                    (self._now() - 7 * 86400,),
                )
                conn.execute(
                    'DELETE FROM posted_fingerprints WHERE posted_at <= ?',
                    (self._now() - 48 * 3600,),
                )
                conn.commit()

            # Ref-count ảnh chỉ chạy SAU khi xóa row đã commit bền vững
            with self.get_connection() as conn:
                referenced = self._referenced_image_paths(conn)
            img_res = self._remove_unreferenced_images(candidates, referenced)
            orphan_res = self._orphan_sweep(retention_seconds)

            total_images = img_res['images'] + orphan_res['images']
            total_errors = img_res['errors'] + orphan_res['errors']
            total_freed = img_res['freed_bytes'] + orphan_res['freed_bytes']

            with self.get_connection() as conn:
                conn.execute('PRAGMA wal_checkpoint(PASSIVE)')
                conn.execute('PRAGMA incremental_vacuum(200)')

            logger.info(
                "CLEANUP retention=%.1fh rows=%d images=%d freed_mb=%.2f errors=%d (seen_post_ids preserved)",
                retention_seconds / 3600.0, deleted, total_images,
                total_freed / (1024 * 1024), total_errors,
            )
            return deleted
        except Exception as e:
            logger.error("cleanup_expired_posts: %s", e)
            return 0

    def cleanup_old_posts(self, keep_rows: int = 10000,
                          crawled_keep_seconds: int = 86400) -> int:
        """Wrapper tương thích BinanceSquare (chỉ xóa bài đã hết hạn retention)."""
        del keep_rows
        return self.cleanup_expired_posts(retention_seconds=max(86400, int(crawled_keep_seconds)))

    # ------------------------------------------------------------------
    # crawl_history + crawl_state
    # ------------------------------------------------------------------
    def start_crawl_run(self, source: str, author_id: str = "") -> int:
        with self.get_connection() as conn:
            cur = conn.execute(
                'INSERT INTO crawl_history (source, author_id, started_at) VALUES (?, ?, ?)',
                (source, author_id, self._now()))
            conn.commit()
            return cur.lastrowid

    def finish_crawl_run(self, run_id: int, posts_found: int = 0, posts_new: int = 0,
                         posts_dup: int = 0, last_item_time: Optional[int] = None,
                         status: str = "ok", error: str = "") -> None:
        self.execute_with_retry('''
            UPDATE crawl_history SET finished_at=?, posts_found=?, posts_new=?, posts_dup=?,
                last_item_time=?, status=?, error=? WHERE id=?
        ''', (self._now(), posts_found, posts_new, posts_dup, last_item_time,
              status, error, run_id))

    def get_crawl_state(self, source: str) -> Optional[Dict]:
        rows = self.execute_with_retry('SELECT * FROM crawl_state WHERE source=?', (source,))
        return dict(rows[0]) if rows else None

    def set_crawl_state(self, source: str, last_crawl_time_ms: Optional[int] = None,
                        authors_json: Optional[str] = None) -> None:
        self.execute_with_retry('''
            INSERT INTO crawl_state (source, last_crawl_time_ms, authors_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_crawl_time_ms=COALESCE(excluded.last_crawl_time_ms, crawl_state.last_crawl_time_ms),
                authors_json=COALESCE(excluded.authors_json, crawl_state.authors_json),
                updated_at=excluded.updated_at
        ''', (source, last_crawl_time_ms, authors_json, self._now()))

    # ------------------------------------------------------------------
    # task_logs
    # ------------------------------------------------------------------
    def log_task(self, level: str, component: str, event: str, message: str = "",
                 account_id: Optional[int] = None, post_id: Optional[str] = None,
                 stack_trace: Optional[str] = None) -> None:
        try:
            self.execute_with_retry('''
                INSERT INTO task_logs (ts, level, component, account_id, post_id, event, message, stack_trace)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (self._now(), level, component, account_id, post_id, event, message, stack_trace))
        except Exception as e:
            logger.error("log_task failed: %s", e)

    def get_recent_logs(self, limit: int = 50, account_id: Optional[int] = None) -> List[Dict]:
        if account_id is not None:
            rows = self.execute_with_retry(
                'SELECT * FROM task_logs WHERE account_id=? ORDER BY ts DESC LIMIT ?',
                (account_id, limit))
        else:
            rows = self.execute_with_retry(
                'SELECT * FROM task_logs ORDER BY ts DESC LIMIT ?', (limit,))
        return [dict(r) for r in (rows or [])]

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------
    def get_stats(self) -> Dict:
        total = self.execute_with_retry('SELECT COUNT(*) AS c FROM posts')[0]['c']
        by_status_rows = self.execute_with_retry(
            'SELECT status, COUNT(*) AS c FROM posts GROUP BY status')
        by_status = {r['status']: r['c'] for r in (by_status_rows or [])}
        by_source_rows = self.execute_with_retry(
            'SELECT source, COUNT(*) AS c FROM posts GROUP BY source')
        by_source = {r['source']: r['c'] for r in (by_source_rows or [])}
        has_gemini = self.execute_with_retry(
            "SELECT COUNT(*) AS c FROM posts WHERE gemini_output IS NOT NULL AND gemini_output!=''")[0]['c']
        accounts = self.execute_with_retry('SELECT COUNT(*) AS c FROM accounts')[0]['c']
        seen_ids = self.execute_with_retry('SELECT COUNT(*) AS c FROM seen_post_ids')[0]['c']
        return {
            'total_posts': total,
            'seen_post_ids': seen_ids,
            'by_status': by_status,
            'by_source': by_source,
            'has_gemini_output': has_gemini,
            'accounts': accounts,
        }

    def print_stats(self):
        s = self.get_stats()
        print("\n" + "=" * 60)
        print("OKXSquare DATABASE STATS")
        print("=" * 60)
        print(f"Total posts : {s['total_posts']}")
        print(f"Accounts    : {s['accounts']}")
        print(f"Gemini done : {s['has_gemini_output']}")
        print("By status   :")
        for k, v in s['by_status'].items():
            print(f"  {k:12s}: {v}")
        print("By source   :")
        for k, v in s['by_source'].items():
            print(f"  {k:12s}: {v}")
        print("=" * 60 + "\n")
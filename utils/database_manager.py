"""
Database Manager với tối ưu concurrent access cho OKX
Hỗ trợ SQLite với WAL mode và connection pooling
Không sử dụng Post Grouping như Binance mà cập nhật trực tiếp trên Post ID gốc.
"""
import sqlite3
import os
import time
from contextlib import contextmanager
from threading import Lock
from typing import Optional, List, Dict, Tuple
from utils.image_sanitizer import cleanup_image_files

class DatabaseManager:
    """
    Database manager với connection pooling và WAL mode
    Tối ưu cho đọc/ghi đồng thời từ nhiều threads
    """
    
    def reset_database(self):
        """Xóa toàn bộ dữ liệu và khởi tạo lại DB từ đầu"""
        print("\n⚠ RESETTING DATABASE...")
        
        # Xóa file database
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                print(f"  ✓ Deleted old database: {self.db_path}")
            except Exception as e:
                print(f"  ✗ Failed to delete database: {e}")
                raise
        
        # Khởi tạo lại
        self._init_database()
        print("  ✓ Database reset complete!\n")

    
    def __init__(self, db_path: str = "data/okx_square.db"):
        self.db_path = db_path
        self.lock = Lock()
        self._ensure_db_directory()
        self._init_database()
    
    def _ensure_db_directory(self):
        """Tạo thư mục database nếu chưa có"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
    
    def _init_database(self):
        """Khởi tạo database với schema và WAL mode"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # Bật WAL mode để hỗ trợ concurrent reads/writes
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA synchronous=NORMAL')
            cursor.execute('PRAGMA cache_size=10000')
            cursor.execute('PRAGMA temp_store=MEMORY')
            cursor.execute('PRAGMA auto_vacuum=INCREMENTAL')
            
            # Tạo bảng posts với các trường cần thiết (OKX + Binance)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY,
                    url TEXT,
                    text TEXT,
                    image_paths TEXT,
                    hashtags TEXT,
                    gemini_output TEXT,
                    status TEXT DEFAULT 'crawled', -- crawled, processing, failed, skipped, ready, posted
                    posted_by TEXT,
                    posted_at TIMESTAMP,
                    crawled_from TEXT,
                    create_time INTEGER,
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    failed_at TIMESTAMP,
                    source TEXT DEFAULT 'okx' -- okx, binance
                )
            ''')
            
            self._migrate_schema(cursor)
            
            # Tạo indexes
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON posts(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gemini_output ON posts(gemini_output)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_crawled_from ON posts(crawled_from)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_create_time ON posts(create_time)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_source ON posts(source)')
            
            # Composite indexes
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ready_to_post ON posts(status, gemini_output)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_retry ON posts(status, retry_count, failed_at)')
            
            conn.commit()
            
        finally:
            conn.close()
    
    def _migrate_schema(self, cursor):
        """Migrate database schema to add missing columns"""
        cursor.execute("PRAGMA table_info(posts)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        
        columns_to_add = [
            ('crawled_from', 'TEXT', None),
            ('posted_by', 'TEXT', None),
            ('posted_at', 'TIMESTAMP', None),
            ('create_time', 'INTEGER', None),
            ('retry_count', 'INTEGER', 0),
            ('last_error', 'TEXT', None),
            ('failed_at', 'TIMESTAMP', None),
            ('source', 'TEXT', "'okx'"),
            ('hashtags', 'TEXT', None),
        ]
        
        for col_name, col_type, default_value in columns_to_add:
            if col_name not in existing_columns:
                if default_value is not None:
                    cursor.execute(f'ALTER TABLE posts ADD COLUMN {col_name} {col_type} DEFAULT {default_value}')
                else:
                    cursor.execute(f'ALTER TABLE posts ADD COLUMN {col_name} {col_type}')
                print(f"→ Added '{col_name}' column to posts table")
    
    def _get_connection(self, timeout: float = 30.0) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=timeout,
            isolation_level=None,  # Autocommit mode
            check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        return conn
    
    @contextmanager
    def get_connection(self):
        conn = self._get_connection()
        try:
            yield conn
        finally:
            conn.close()
    
    def execute_with_retry(self, query: str, params: tuple = (), max_retries: int = 5) -> Optional[list]:
        for attempt in range(max_retries):
            try:
                with self.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(query, params)
                    
                    if query.strip().upper().startswith('SELECT'):
                        return cursor.fetchall()
                    else:
                        conn.commit()
                        return None
                        
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < max_retries - 1:
                    wait_time = 0.1 * (2 ** attempt)
                    time.sleep(wait_time)
                else:
                    raise
            except Exception as e:
                raise
    
    # ==================== CRAWLER OPERATIONS ====================
    
    def batch_check_existing_posts(self, post_ids: List[str]) -> set:
        if not post_ids:
            return set()
        
        existing = set()
        chunk_size = 500
        
        for i in range(0, len(post_ids), chunk_size):
            chunk = post_ids[i:i + chunk_size]
            placeholders = ','.join(['?' for _ in chunk])
            
            result = self.execute_with_retry(
                f'SELECT id FROM posts WHERE id IN ({placeholders})',
                tuple(chunk)
            )
            
            if result:
                for row in result:
                    existing.add(row['id'])
        
        return existing
    
    def insert_post_with_time(self, post_id: str, url: str, text: str,
                              image_paths: List[str], crawled_from: str,
                              create_time: int = None,
                              post_group: int = 0,
                              source: str = 'okx',
                              hashtags: str = None) -> bool:
        """
        Thêm post mới vào database.
        source: 'okx' hoặc 'binance' để phân biệt nguồn crawl.
        """
        try:
            image_paths_str = '|'.join(image_paths) if image_paths else ''
            
            self.execute_with_retry('''
                INSERT INTO posts (id, url, text, image_paths, crawled_from, create_time, status, source, hashtags)
                VALUES (?, ?, ?, ?, ?, ?, 'crawled', ?, ?)
            ''', (post_id, url, text, image_paths_str, crawled_from, create_time, source, hashtags))
            
            return True
        except sqlite3.IntegrityError:
            return False
        except Exception as e:
            print(f"✗ Lỗi insert post {post_id}: {e}")
            return False
            
    # ==================== GEMINI PROCESSING OPERATIONS ====================
    
    def get_unprocessed_posts(self, limit: Optional[int] = None, sources: Optional[List[str]] = None) -> List[Dict]:
        """Lấy danh sách posts để đưa qua Gemini (ưu tiên bài mới nhất trước)"""
        if sources is not None and not sources:
            return []
        params: list = []
        source_clause = ""
        if sources:
            placeholders = ','.join(['?'] * len(sources))
            source_clause = f"AND source IN ({placeholders})"
            params.extend(sources)

        query = f'''
            SELECT id, text, image_paths, retry_count, hashtags, source
            FROM posts 
            WHERE status = 'crawled'
            AND text IS NOT NULL 
            AND text != ''
            {source_clause}
            ORDER BY COALESCE(create_time, 0) DESC, id DESC
        '''
        if limit is not None:
            query += f' LIMIT {int(limit)}'
        
        rows = self.execute_with_retry(query, tuple(params))
        return [dict(row) for row in rows] if rows else []

    def update_gemini_output(self, post_id: str, output: str, model: str = None) -> bool:
        """Cập nhật kết quả Gemini trực tiếp vào bài gốc và đánh dấu sẵn sàng đăng"""
        try:
            self.execute_with_retry('''
                UPDATE posts
                SET gemini_output = ?,
                    status = 'ready'
                WHERE id = ?
            ''', (output, post_id))
            return True
        except Exception as e:
            print(f"✗ Lỗi update gemini output {post_id}: {e}")
            return False
            
    def get_failed_posts_for_retry(self, limit: Optional[int] = None, 
                                    cooldown_minutes: int = 5,
                                    max_retries: int = 3) -> List[Dict]:
        """Lấy danh sách các bài xử lý Gemini lỗi để retry"""
        query = '''
            SELECT id, text, image_paths, retry_count, last_error, source
            FROM posts 
            WHERE status = 'failed'
            AND text IS NOT NULL 
            AND text != ''
            AND COALESCE(retry_count, 0) < ?
            AND (
                failed_at IS NULL 
                OR datetime(failed_at, '+' || ? || ' minutes') <= datetime('now')
            )
            ORDER BY retry_count ASC, failed_at ASC
        '''
        params = (max_retries, cooldown_minutes)
        if limit:
            query += f' LIMIT {limit}'
        
        rows = self.execute_with_retry(query, params)
        return [dict(row) for row in rows] if rows else []

    def mark_post_gemini_failed(self, post_id: str, error: str, increment_retry: bool = True) -> bool:
        try:
            if increment_retry:
                self.execute_with_retry('''
                    UPDATE posts
                    SET retry_count = COALESCE(retry_count, 0) + 1,
                        last_error = ?,
                        failed_at = CURRENT_TIMESTAMP,
                        status = 'failed'
                    WHERE id = ?
                ''', (error, post_id))
            else:
                self.execute_with_retry('''
                    UPDATE posts
                    SET last_error = ?,
                        failed_at = CURRENT_TIMESTAMP,
                        status = 'failed'
                    WHERE id = ?
                ''', (error, post_id))
            return True
        except Exception as e:
            print(f"✗ Lỗi mark gemini failed {post_id}: {e}")
            return False

    def skip_post_permanently(self, post_id: str, error: str) -> bool:
        try:
            rows = self.execute_with_retry(
                'SELECT image_paths FROM posts WHERE id = ?', (post_id,)
            )
            if rows and rows[0]['image_paths']:
                cleanup_image_files(rows[0]['image_paths'])

            self.execute_with_retry('''
                UPDATE posts
                SET status = 'skipped',
                    image_paths = NULL,
                    last_error = ?,
                    failed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (error, post_id))
            print(f"⚠ Post {post_id} skipped permanently: {error}")
            return True
        except Exception as e:
            return False

    # ==================== POSTING OPERATIONS ====================

    def claim_post_for_posting(self, worker_name: str) -> Optional[Dict]:
        """
        ATOMIC: Lấy 1 post đã sẵn sàng và mark processing ngay lập tức dành cho OKX
        """
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                
                cursor = conn.execute('''
                    SELECT id, gemini_output, image_paths, crawled_from, hashtags
                    FROM posts 
                    WHERE status = 'ready'
                    AND gemini_output IS NOT NULL 
                    AND gemini_output != ''
                    ORDER BY COALESCE(create_time, 0) DESC, id DESC
                    LIMIT 1
                ''')
                
                row = cursor.fetchone()
                
                if not row:
                    conn.rollback()
                    return None
                
                post_id = row['id']
                
                # Cập nhật status để khoá tài nguyên
                conn.execute('''
                    UPDATE posts
                    SET status = 'processing'
                    WHERE id = ?
                ''', (post_id,))
                
                conn.commit()
                return dict(row)
        except Exception as e:
            print(f"✗ Error claiming post: {e}")
            return None

    def claim_batch_for_distribution(self, count: int, sources: List[str] = None) -> List[Dict]:
        """
        ATOMIC: Claim N bài sẵn sàng cùng lúc trong 1 transaction.
        Dùng cho Round-Robin distribution — orchestrator claim batch rồi chia đều.
        
        Returns: list of post dicts đã được mark 'processing'
        """
        if count <= 0:
            return []
        try:
            with self.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                
                if sources:
                    placeholders = ','.join(['?'] * len(sources))
                    query = f'''
                        SELECT id, gemini_output, image_paths, crawled_from, hashtags, source
                        FROM posts 
                        WHERE status = 'ready'
                        AND gemini_output IS NOT NULL 
                        AND gemini_output != ''
                        AND source IN ({placeholders})
                        ORDER BY COALESCE(create_time, 0) DESC, id DESC
                        LIMIT ?
                    '''
                    params = tuple(sources) + (count,)
                else:
                    query = '''
                        SELECT id, gemini_output, image_paths, crawled_from, hashtags, source
                        FROM posts 
                        WHERE status = 'ready'
                        AND gemini_output IS NOT NULL 
                        AND gemini_output != ''
                        ORDER BY COALESCE(create_time, 0) DESC, id DESC
                        LIMIT ?
                    '''
                    params = (count,)
                
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
                
                if not rows:
                    conn.rollback()
                    return []
                
                # Mark tất cả thành 'processing' trong 1 transaction
                ids = [r['id'] for r in rows]
                ids_placeholders = ','.join(['?'] * len(ids))
                conn.execute(f'''
                    UPDATE posts
                    SET status = 'processing'
                    WHERE id IN ({ids_placeholders})
                ''', ids)
                
                conn.commit()
                return [dict(r) for r in rows]
        except Exception as e:
            print(f"✗ Error claiming batch ({count}): {e}")
            return []

    def release_batch(self, post_ids: List[str]) -> int:
        """
        Trả lại batch posts về 'ready' nếu không được đăng.
        Returns: số posts đã release
        """
        if not post_ids:
            return 0
        try:
            placeholders = ','.join(['?'] * len(post_ids))
            self.execute_with_retry(f'''
                UPDATE posts
                SET status = 'ready'
                WHERE id IN ({placeholders})
                AND status = 'processing'
            ''', tuple(post_ids))
            return len(post_ids)
        except Exception as e:
            print(f"✗ Error releasing batch: {e}")
            return 0

    def count_ready_posts(self) -> int:
        """Đếm số bài sẵn sàng đăng"""
        try:
            result = self.execute_with_retry('''
                SELECT COUNT(*) as cnt FROM posts
                WHERE status = 'ready'
                AND gemini_output IS NOT NULL AND gemini_output != ''
            ''')
            return result[0]['cnt'] if result else 0
        except Exception:
            return 0

    def count_ready_posts_by_sources(
        self, sources: List[str], include_processing: bool = False
    ) -> int:
        """Đếm số bài sẵn sàng đăng theo danh sách nguồn (sources)"""
        if not sources:
            return 0
        try:
            placeholders = ','.join(['?' for _ in sources])
            statuses = "('ready', 'processing')" if include_processing else "('ready')"
            result = self.execute_with_retry(f'''
                SELECT COUNT(*) as cnt FROM posts
                WHERE status IN {statuses}
                AND gemini_output IS NOT NULL AND gemini_output != ''
                AND source IN ({placeholders})
            ''', tuple(sources))
            return result[0]['cnt'] if result else 0
        except Exception as e:
            print(f"✗ Lỗi count_ready_posts_by_sources: {e}")
            return 0

    def count_crawled_posts_by_sources(self, sources: List[str]) -> int:
        """Đếm số bài viết đã cào nhưng chưa xử lý (status = 'crawled') theo sources"""
        if not sources:
            return 0
        try:
            placeholders = ','.join(['?' for _ in sources])
            result = self.execute_with_retry(f'''
                SELECT COUNT(*) as cnt FROM posts
                WHERE status = 'crawled'
                AND source IN ({placeholders})
            ''', tuple(sources))
            return result[0]['cnt'] if result else 0
        except Exception as e:
            print(f"✗ Lỗi count_crawled_posts_by_sources: {e}")
            return 0

    def delete_crawled_posts_by_sources(self, sources: List[str]) -> int:
        """Xóa các bài thô (status='crawled') của các nguồn cụ thể để dọn đường cho bài mới"""
        if not sources:
            return 0
        try:
            placeholders = ','.join(['?' for _ in sources])
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f'''
                    SELECT image_paths FROM posts
                    WHERE status = 'crawled'
                    AND source IN ({placeholders})
                ''', tuple(sources))
                for row in cursor.fetchall():
                    if row['image_paths']:
                        cleanup_image_files(row['image_paths'])

                cursor.execute(f'''
                    DELETE FROM posts
                    WHERE status = 'crawled'
                    AND source IN ({placeholders})
                ''', tuple(sources))
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            print(f"✗ Lỗi delete_crawled_posts_by_sources: {e}")
            return 0

    def mark_post_posted(self, post_id: str, posted_by: str) -> bool:
        try:
            rows = self.execute_with_retry(
                'SELECT image_paths FROM posts WHERE id = ?', (post_id,)
            )
            if rows and rows[0]['image_paths']:
                cleanup_image_files(rows[0]['image_paths'])

            self.execute_with_retry('''
                UPDATE posts
                SET status = 'posted',
                    posted_by = ?,
                    posted_at = CURRENT_TIMESTAMP,
                    gemini_output = NULL,
                    image_paths = NULL,
                    text = NULL
                WHERE id = ?
            ''', (posted_by, post_id))
            return True
        except Exception as e:
            return False

    def release_post(self, post_id: str) -> bool:
        """Nếu worker lỗi lúc đăng bài, trả post về `ready` để luồng khác đăng"""
        try:
            self.execute_with_retry('''
                UPDATE posts
                SET status = 'ready'
                WHERE id = ?
            ''', (post_id,))
            return True
        except Exception as e:
            return False

    def cleanup_processing_timeout(self) -> int:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE posts
                    SET status = 'ready'
                    WHERE status = 'processing'
                ''')
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            return 0

    # ==================== STATISTICS & CLEANUP ====================
            
    def get_stats(self) -> Dict:
        """Lấy thống kê tổng quan của Database"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            stats = {}
            
            cursor.execute('SELECT COUNT(*) as count FROM posts')
            stats['total_posts'] = cursor.fetchone()['count']
            
            cursor.execute('SELECT status, COUNT(*) as count FROM posts GROUP BY status')
            stats['by_status'] = {row['status']: row['count'] for row in cursor.fetchall()}
            
            cursor.execute('SELECT crawled_from, COUNT(*) as count FROM posts WHERE crawled_from IS NOT NULL GROUP BY crawled_from')
            stats['by_source'] = {row['crawled_from']: row['count'] for row in cursor.fetchall()}
            
            return stats
            
    def get_retry_stats(self) -> Dict:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            stats = {}
            cursor.execute("SELECT COUNT(*) as count FROM posts WHERE status = 'failed'")
            stats['failed_awaiting_retry'] = cursor.fetchone()['count']
            cursor.execute("SELECT COUNT(*) as count FROM posts WHERE status = 'skipped'")
            stats['permanently_skipped'] = cursor.fetchone()['count']
            return stats

    def print_stats(self):
        stats = self.get_stats()
        print("\n" + "=" * 60)
        print("DATABASE OKX STATISTICS")
        print("=" * 60)
        print(f"Tổng số posts: {stats['total_posts']}")
        print("\nTheo status:")
        for status, count in stats['by_status'].items():
            print(f"  {status}: {count}")
        
        if stats['by_source']:
            print("\nTheo nguồn:")
            for source, count in stats['by_source'].items():
                print(f"  {source}: {count}")
        print("=" * 60)

    def cleanup_old_posts(self, keep_rows: int = 10000) -> int:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) as total FROM posts')
                total = cursor.fetchone()['total']
                
                if total <= keep_rows:
                    return 0
                
                rows_to_delete = total - keep_rows
                
                cursor.execute('''
                    SELECT id, image_paths 
                    FROM posts 
                    ORDER BY COALESCE(create_time, 0) ASC, id ASC
                    LIMIT ?
                ''', (rows_to_delete,))
                
                posts_to_delete = cursor.fetchall()
                post_ids = []
                
                for row in posts_to_delete:
                    post_ids.append(row['id'])
                    image_paths_str = row['image_paths']
                    if image_paths_str:
                        for path in image_paths_str.split('|'):
                            path = path.strip()
                            if path and os.path.exists(path):
                                try: os.remove(path)
                                except: pass
                
                if post_ids:
                    placeholders = ','.join(['?' for _ in post_ids])
                    cursor.execute(f'DELETE FROM posts WHERE id IN ({placeholders})', post_ids)
                    conn.commit()
                
                try: cursor.execute('PRAGMA incremental_vacuum(1000)')
                except: pass
                
                return len(post_ids)
        except Exception as e:
            return 0

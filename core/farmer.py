"""
OKX Organic Engagement Engine — OKXFarmer
==========================================
Class chính điều phối farming cho 1 account.
Mỗi instance là 1 worker thread độc lập:
  - OKXApi riêng, session riêng, delay riêng
  - GeminiProcessor shared (thread-safe)
  - Gaussian delay system
  - Circadian rest (nhịp sinh học)
  - AI Comment generation (BLOCKING — chờ AI trả kết quả)

Session Resilience:
  - OKXApi tự auto-refresh session mỗi 2h hoặc 500 requests
  - Khi gặp API error liên tiếp, chủ động refresh session + reload cookies

Tích hợp vào run.py Mode 1 (Background) hoặc Mode 9 (Standalone).
"""

import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional, Dict, List, Tuple

from core.okx_api import OKXApi


# ============================================================
# OKXFarmer — CORE CLASS
# ============================================================

class OKXFarmer:
    """
    Organic Engagement Engine cho 1 account OKX.
    Thread-safe. Mỗi instance chạy trong 1 thread riêng.
    """

    # Ngưỡng lỗi API liên tiếp trước khi chủ động recovery
    API_ERROR_RECOVERY_THRESHOLD = 10

    def __init__(
        self,
        account_data: Dict,
        farming_config: Dict,
        gemini_processor,  # GeminiProcessor instance (shared, thread-safe)
        proxy: str = None,
        stop_event=None,     # threading.Event để dừng
    ):
        self.account_id = account_data['id']
        self.config = farming_config
        self.gemini = gemini_processor
        self._stop_event = stop_event

        # OKXApi riêng cho account này
        self.api = OKXApi(account_data, proxy=proxy)

        # Load prompt template cho comment
        self.comment_prompt = self._load_comment_prompt()

        # Delays config
        self.delays = self.config.get('delays', {})

        # Interaction rates
        self.followed_rates = self.config.get('followed_author', {})
        self.unfollowed_rates = self.config.get('unfollowed_author', {})

        # Session config
        self.posts_per_session = self.config.get('posts_per_session', 60)
        self.min_interval = self.config.get('min_interval_seconds', 7)

        # Circadian config
        self.session_break = self.config.get('session_break_minutes', [3, 8])
        self.sessions_before_long = self.config.get('sessions_before_long_break', 4)
        self.long_break = self.config.get('long_break_minutes', [15, 30])

        # Comment validation
        self.comment_max_len = self.config.get('comment_max_length', 150)
        self.comment_min_len = self.config.get('comment_min_length', 5)

        # Deep dive config
        self.dd_config = self.config.get('deep_dive', {})

        # Stats (thread-safe)
        self._stats_lock = Lock()
        self.stats = {
            'sessions': 0,
            'total_views': 0,
            'total_likes': 0,
            'total_comments': 0,
            'total_follows': 0,
            'total_deep_dives': 0,
            'total_skipped': 0,
            'ai_calls': 0,
            'ai_success': 0,
            'ai_failed': 0,
            'api_errors': 0,
            'start_time': None,
            'total_posts_processed': 0,
            'feed_fetches': 0,
            'total_dd_views': 0,
            'total_dd_likes': 0,
        }

        # Memory (Short-term)
        self._memory_file = os.path.join("data", f"farming_memory_{self.account_id}.json")
        self.memory = {}
        
        self.st_config = self.config.get('short_term_memory', {})
        self.kol_config = self.config.get('kol_focus', {})
        self._load_memory()

        # Consecutive API error tracking for recovery
        self._consecutive_api_errors = 0

        self._print(f"🌱 Farmer initialized | {self.posts_per_session} posts/session | "
                    f"interval={self.min_interval}s | break={self.session_break} | "
                    f"long_break={self.long_break} (every {self.sessions_before_long})")

    # ==================================================================
    # LOGGING — Console only
    # ==================================================================

    def _print(self, msg: str):
        """Console log — chỉ in những thứ có giá trị phân tích"""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [{self.account_id}] {msg}")
        sys.stdout.flush()

    def _load_memory(self):
        if os.path.exists(self._memory_file):
            try:
                with open(self._memory_file, 'r', encoding='utf-8') as f:
                    self.memory = json.load(f)
            except Exception:
                self.memory = {}

    def _save_memory(self):
        try:
            with open(self._memory_file, 'w', encoding='utf-8') as f:
                json.dump(self.memory, f)
        except Exception as e:
            self._print(f"❌ Failed to save memory: {e}")

    # ==================================================================
    # SESSION RECOVERY
    # ==================================================================

    def _handle_api_error(self):
        """
        Đếm lỗi API liên tiếp. Khi vượt ngưỡng → chủ động recovery session.
        """
        self._consecutive_api_errors += 1
        with self._stats_lock:
            self.stats['api_errors'] += 1

        if self._consecutive_api_errors >= self.API_ERROR_RECOVERY_THRESHOLD:
            self._print(f"🔌 {self._consecutive_api_errors} API errors liên tiếp — recovery session...")

            # Step 1: Refresh session
            self.api.refresh_session()
            if self.api.health_check():
                self._print("  ↳ Session refresh OK")
                self._consecutive_api_errors = 0
                return

            # Step 2: Reload cookies
            self._print("  ↳ Refresh failed → reload cookies...")
            changed = self.api.reload_cookies_from_disk()
            if changed and self.api.health_check():
                self._print("  ↳ Cookies reloaded OK")
                self._consecutive_api_errors = 0
            else:
                self._print("  ⛔ Recovery failed — session/token có thể expired!")
                # Reset counter để thử lại sau thêm N errors nữa
                self._consecutive_api_errors = 0

    def _reset_api_errors(self):
        """Reset consecutive error count khi có API call thành công."""
        if self._consecutive_api_errors > 0:
            self._consecutive_api_errors = 0

    # ==================================================================
    # GAUSSIAN DELAY
    # ==================================================================

    def _gaussian_delay(self, delay_type: str) -> float:
        """
        Sleep theo phân phối Gaussian.
        Config format: [mean, std, min]
        Returns: actual delay applied (seconds)
        """
        params = self.delays.get(delay_type, [2.0, 0.5, 0.5])
        mean, std, min_val = params[0], params[1], params[2] if len(params) > 2 else 0.3
        delay = max(min_val, random.gauss(mean, std))
        delay = min(delay, mean * 3)  # Cap outlier
        time.sleep(delay)
        return delay

    def _sleep_interruptible(self, seconds: float):
        """Sleep có thể bị interrupt bởi stop_event"""
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self._should_stop():
                return
            time.sleep(min(1.0, end_time - time.time()))

    def _should_stop(self) -> bool:
        if self._stop_event and self._stop_event.is_set():
            return True
        return False

    # ==================================================================
    # COMMENT PROMPT
    # ==================================================================

    def _load_comment_prompt(self) -> str:
        prompt_file = self.config.get('comment_prompt_file', 'config/farming_comment_prompt.txt')
        if os.path.exists(prompt_file):
            with open(prompt_file, 'r', encoding='utf-8') as f:
                return f.read()
        else:
            self._print(f"⚠ Prompt not found: {prompt_file}")
            return "Viết 1 comment ngắn gọn cho bài đăng sau:\n{text}\nComment:"

    # ==================================================================
    # FEED & API actions
    # ==================================================================

    def _fetch_feed(self, cursor: str = None) -> Tuple[List[Dict], Optional[str]]:
        path = "/priapi/v5/rubik/public/content/feed/1?supportCeDeFi=true&type=1&group=USDT"
        if cursor:
            path += f"&cursor={cursor}"

        try:
            res = self.api.get(path)
            data = res.json()
        except Exception as e:
            self._print(f"❌ Feed API error: {e}")
            self._handle_api_error()
            return [], None

        posts = []
        next_cursor = None

        if data and data.get("code") == "0":
            self._reset_api_errors()
            feed_data = data.get("data", {})
            next_cursor = feed_data.get("nextCursor")
            data_list = feed_data.get("dataList", [])

            for wrapper in data_list:
                if wrapper.get("type", "1") != "1":
                    continue
                item = wrapper.get("contentData", wrapper)
                author = item.get("author", {})
                posts.append({
                    'contentId': item.get('contentId', ''),
                    'authorId': author.get('authorId', item.get('authorId', '')),
                    'nickName': author.get('nickName', 'Unknown'),
                    'followStatus': author.get('followStatus', '0'),
                    'content': item.get('content', ''),
                    'isLiked': item.get('isLiked', False),
                    'viewCount': item.get('viewCount', 0),
                    'likeCount': item.get('likeCount', 0),
                    'commentNum': item.get('commentNum', item.get('commentCount', 0)),
                })

            with self._stats_lock:
                self.stats['feed_fetches'] += 1
        else:
            code = data.get("code", "?") if data else "?"
            msg = data.get("msg", "") if data else "No response"
            self._print(f"❌ Feed error code={code}: {msg}")
            self._handle_api_error()

        return posts, next_cursor

    def _view_post(self, content_id: str) -> bool:
        path = f"/priapi/v5/rubik/public/content/detail/1?supportCeDeFi=true&contentId={content_id}&group=USDT"
        try:
            res = self.api.get(path)
            data = res.json()
            if data and data.get("code") == "0":
                self._reset_api_errors()
                return True
            else:
                return False
        except Exception as e:
            self._handle_api_error()
            return False

    def _like_post(self, content_id: str) -> bool:
        path = "/priapi/v5/content/follower/like"
        body = json.dumps({"contentId": content_id, "isLiked": True}, separators=(",", ":"))
        try:
            res = self.api.post(path, body)
            data = res.json()
            if data and data.get("code") == "0":
                self._reset_api_errors()
                return True
            else:
                return False
        except Exception as e:
            self._print(f"❌ Like API error: {e}")
            self._handle_api_error()
            return False

    def _post_comment(self, content_id: str, comment_text: str) -> bool:
        path = "/priapi/v5/content/user/comment"
        body = json.dumps({
            "hashTagListStr": "[]",
            "contentId": content_id,
            "comment": comment_text
        }, separators=(",", ":"))
        try:
            res = self.api.post(path, body)
            data = res.json()
            if data and data.get("code") == "0":
                self._reset_api_errors()
                return True
            else:
                code = data.get('code', '?') if data else '?'
                msg = data.get('msg', '') if data else ''
                self._print(f"❌ Comment rejected by OKX: code={code} msg={msg}")
                return False
        except Exception as e:
            self._print(f"❌ Comment API error: {e}")
            self._handle_api_error()
            return False

    def _follow_author(self, author_id: str) -> bool:
        path = "/priapi/v5/content/user/follow"
        body = json.dumps({"status": "1", "authorId": author_id}, separators=(",", ":"))
        try:
            res = self.api.post(path, body)
            data = res.json()
            if data and data.get("code") == "0":
                self._reset_api_errors()
                return True
            else:
                return False
        except Exception as e:
            self._print(f"❌ Follow API error: {e}")
            self._handle_api_error()
            return False

    # ==================================================================
    # AI COMMENT GENERATION (BLOCKING)
    # ==================================================================

    def _generate_ai_comment(self, post_content: str) -> Optional[str]:
        """
        BLOCKING — chờ AI trả kết quả xong mới return.
        Không có fallback. AI fail → return None → skip.
        """
        if not post_content or len(post_content.strip()) < 10:
            return None

        # Pre-screen raw content to avoid checking negative constraints in the prompt
        if self.gemini._is_sensitive_content(post_content):
            return None

        with self._stats_lock:
            self.stats['ai_calls'] += 1

        try:
            prompt = self.comment_prompt.replace("{text}", post_content[:2000])
            result = self.gemini.process_text(prompt, post_id=f"farming_{self.account_id}", is_raw_text=False)

            if result:
                raw_comment, model_used = result
                comment = self._validate_comment(raw_comment)
                if comment:
                    with self._stats_lock:
                        self.stats['ai_success'] += 1
                    return comment
                else:
                    with self._stats_lock:
                        self.stats['ai_failed'] += 1
                    return None
            else:
                with self._stats_lock:
                    self.stats['ai_failed'] += 1
                return None

        except Exception as e:
            with self._stats_lock:
                self.stats['ai_failed'] += 1
            return None

    def _validate_comment(self, raw: str) -> Optional[str]:
        if not raw:
            return None

        comment = raw.strip().replace('*', '')
        # Strip wrapping quotes
        if (comment.startswith('"') and comment.endswith('"')) or \
           (comment.startswith("'") and comment.endswith("'")):
            comment = comment[1:-1].strip()
        # Only first line
        lines = [l.strip() for l in comment.split('\n') if l.strip()]
        comment = lines[0] if lines else ''
        if not comment:
            return None

        # Strip bot prefixes
        for prefix in ["comment:", "comment là:", "dưới đây", "here's",
                        "here is", "sure!", "sure,", "okay,", "ok,", "alright,"]:
            if comment.lower().startswith(prefix):
                comment = comment[len(prefix):].strip()

        if len(comment) < self.comment_min_len:
            return None
        if len(comment) > self.comment_max_len:
            comment = comment[:self.comment_max_len]
        if re.search(r'https?://', comment, re.IGNORECASE):
            return None
        for ex in ['binance', 'bybit', 'kucoin', 'htx', 'okx', 'mexc', 'gate.io']:
            if ex in comment.lower():
                return None
        return comment

    # ==================================================================
    # DEEP DIVE
    # ==================================================================

    def _deep_dive(self, author_id: str, nick_name: str) -> int:
        """
        Mô phỏng vào trang cá nhân tác giả.
        Returns: số bài cũ đã view
        """
        # 1. Author detail
        path = f"/priapi/v5/content/public/author/detail?authorId={author_id}"
        try:
            res = self.api.get(path)
            data = res.json()
            if not data or data.get("code") != "0":
                return 0
        except Exception:
            return 0

        self._gaussian_delay('deep_dive_scan')

        # 2. Timeline
        timeline_path = (f"/priapi/v5/rubik/public/content/timeline/1"
                         f"?queryName={author_id}&type=3&supportCeDeFi=true")
        try:
            res = self.api.get(timeline_path)
            tl_data = res.json()
        except Exception:
            return 0

        old_posts = []
        if tl_data and tl_data.get("code") == "0":
            content_list = tl_data.get("data", {}).get("contentDataList", [])
            old_posts = content_list[:self.dd_config.get('max_old_posts', 2)]

        like_rate = self.dd_config.get('like_old_post_rate', 0.15)
        views_count = 0
        likes_count = 0

        for post in old_posts:
            if self._should_stop():
                break
            cid = post.get('contentId')
            if not cid:
                continue

            self._gaussian_delay('deep_dive_read')
            if self._view_post(cid):
                views_count += 1
                with self._stats_lock:
                    self.stats['total_views'] += 1
                    self.stats['total_dd_views'] += 1

            self._gaussian_delay('deep_dive_read')

            if random.random() < like_rate and not post.get('isLiked', False):
                if self._like_post(cid):
                    likes_count += 1
                    with self._stats_lock:
                        self.stats['total_likes'] += 1
                        self.stats['total_dd_likes'] += 1

        return views_count

    # ==================================================================
    # SOFT SHUFFLE
    # ==================================================================

    def _soft_shuffle(self, posts: list) -> list:
        if len(posts) < 4:
            return posts
        result = posts.copy()
        for _ in range(random.randint(2, 3)):
            i = random.randint(0, len(result) - 2)
            j = random.randint(i, min(i + 3, len(result) - 1))
            result[i], result[j] = result[j], result[i]
        return result

    # ==================================================================
    # PROCESS SINGLE POST
    # ==================================================================

    def _process_post(self, post: Dict, session_stats: Dict) -> Dict:
        content_id = post['contentId']
        author_id = post['authorId']
        nick_name = post['nickName']
        content = post.get('content', '')
        is_liked = post.get('isLiked', False)
        is_followed = post.get('followStatus', '0') in ('1', '3')
        view_count = post.get('viewCount', 0)
        content_len = len(content)

        rates = self.followed_rates.copy() if is_followed else self.unfollowed_rates.copy()

        # KOL Focus Multipliers
        if self.kol_config.get('enabled', False):
            threshold = self.kol_config.get('high_view_threshold', 10000)
            if view_count > threshold:
                mult = self.kol_config.get('multiplier_high_view', 2.0)
                if not is_followed:
                    mult = self.kol_config.get('multiplier_high_view_unfollowed', 3.0)
                for k in ['like_rate', 'comment_rate', 'follow_rate', 'deep_dive_rate']:
                    rates[k] = min(1.0, rates.get(k, 0) * mult)

        # SKIP CHECK
        if random.random() < rates.get('skip_rate', 0.08):
            session_stats['skipped'] += 1
            with self._stats_lock:
                self.stats['total_skipped'] += 1
            return session_stats

        # SCROLL DELAY
        self._gaussian_delay('scroll')

        # VIEW (always)
        now = time.time()
        post_start = now
        if self._view_post(content_id):
            session_stats['views'] += 1
            with self._stats_lock:
                self.stats['total_views'] += 1

        # READ DELAY
        self._gaussian_delay('read_short' if content_len < 200 else 'read_long')

        # COOLDOWN MULTIPLIER — followed authors get 0.5x cooldown (= gấp đôi tần suất)
        cooldown_mult = 0.5 if is_followed else 1.0

        # LIKE
        like_rate = rates.get('like_rate', 0.05)
        can_like = True
        if self.st_config.get('enabled', False):
            last_like = self.memory.get(author_id, {}).get('last_like', 0)
            if (now - last_like) < self.st_config.get('like_cooldown_hours', 1.0) * 3600 * cooldown_mult:
                can_like = False

        if can_like and random.random() < like_rate:
            if not is_liked:
                if self._like_post(content_id):
                    self.memory.setdefault(author_id, {})['last_like'] = now
                    session_stats['likes'] += 1
                    with self._stats_lock:
                        self.stats['total_likes'] += 1
                    self._print(f"  ❤️ Like [{nick_name}] views={view_count}")
                    self._gaussian_delay('after_like')

        # COMMENT (BLOCKING)
        comment_rate = rates.get('comment_rate', 0.02)
        can_comment = True
        if self.st_config.get('enabled', False):
            last_comment = self.memory.get(author_id, {}).get('last_comment', 0)
            if (now - last_comment) < self.st_config.get('comment_cooldown_hours', 2.0) * 3600 * cooldown_mult:
                can_comment = False

        if can_comment and random.random() < comment_rate:
            ai_start = time.time()
            comment = self._generate_ai_comment(content)
            ai_duration = time.time() - ai_start

            if comment:
                if self._post_comment(content_id, comment):
                    self.memory.setdefault(author_id, {})['last_comment'] = now
                    session_stats['comments'] += 1
                    with self._stats_lock:
                        self.stats['total_comments'] += 1
                    # Console: comment luôn log vì quý giá
                    self._print(f"  💬 Comment [{nick_name}] ({ai_duration:.1f}s): \"{comment[:80]}\"")
                self._gaussian_delay('after_comment')

        # FOLLOW (chỉ unfollowed)
        if not is_followed:
            if random.random() < rates.get('follow_rate', 0.01):
                if self._follow_author(author_id):
                    session_stats['follows'] += 1
                    with self._stats_lock:
                        self.stats['total_follows'] += 1
                    self._print(f"  ➕ Follow [{nick_name}]")
                    self._gaussian_delay('after_follow')

        # DEEP DIVE
        dd_rate = rates.get('deep_dive_rate', 0.03)
        can_dd = True
        if self.st_config.get('enabled', False):
            last_dd = self.memory.get(author_id, {}).get('last_deep_dive', 0)
            if (now - last_dd) < self.st_config.get('deep_dive_cooldown_hours', 8.0) * 3600 * cooldown_mult:
                can_dd = False

        if can_dd and random.random() < dd_rate:
            dd_views = self._deep_dive(author_id, nick_name)
            if dd_views > 0:
                self.memory.setdefault(author_id, {})['last_deep_dive'] = now
            session_stats['deep_dives'] += 1
            with self._stats_lock:
                self.stats['total_deep_dives'] += 1
            self._print(f"  🔍 DeepDive [{nick_name}] +{dd_views} views")

        # ENFORCE MIN INTERVAL
        elapsed = time.time() - post_start
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        with self._stats_lock:
            self.stats['total_posts_processed'] += 1

        return session_stats

    # ==================================================================
    # RUN SESSION
    # ==================================================================

    def _run_session(self, session_num: int) -> Dict:
        session_stats = {
            'views': 0, 'likes': 0, 'comments': 0,
            'follows': 0, 'deep_dives': 0, 'skipped': 0,
            'posts_processed': 0, 'duration': 0,
        }

        session_start = time.time()

        cursor = None
        total_processed = 0

        while total_processed < self.posts_per_session:
            if self._should_stop():
                break

            posts, next_cursor = self._fetch_feed(cursor)
            if not posts:
                if cursor:
                    cursor = None
                    time.sleep(5)
                    continue
                else:
                    self._print(f"⚠ Empty feed, waiting 30s...")
                    self._sleep_interruptible(30)
                    continue

            cursor = next_cursor
            posts = self._soft_shuffle(posts)

            for post in posts:
                if self._should_stop() or total_processed >= self.posts_per_session:
                    break
                self._process_post(post, session_stats)
                total_processed += 1
                session_stats['posts_processed'] = total_processed

        duration = time.time() - session_start
        session_stats['duration'] = duration

        with self._stats_lock:
            self.stats['sessions'] += 1

        # SESSION SUMMARY
        effective_posts = session_stats['views']
        avg_per_post = duration / effective_posts if effective_posts > 0 else 0
        views_per_h = effective_posts / (duration / 3600) if duration > 0 else 0

        self._print(f"─── Session #{session_num} done ───")
        self._print(f"  ⏱ {duration:.0f}s ({duration/60:.1f}m) | "
                    f"{session_stats['posts_processed']} posts | "
                    f"avg {avg_per_post:.1f}s/post")
        self._print(f"  👁 {session_stats['views']} views ({views_per_h:.0f}/h) | "
                    f"❤️ {session_stats['likes']} likes | "
                    f"💬 {session_stats['comments']} comments")
        self._print(f"  ➕ {session_stats['follows']} follows | "
                    f"🔍 {session_stats['deep_dives']} dives | "
                    f"⏭ {session_stats['skipped']} skipped")

        # Save short-term memory persistent to disk
        self._save_memory()

        return session_stats

    # ==================================================================
    # RUN FOREVER — Main loop
    # ==================================================================
    # REACTIVE ENGAGEMENT
    # ==================================================================

    def _check_reactive_interactions(self):
        """
        Quét thông báo Phản hồi / Comment trên các bài viết của mình
        và dùng Gemini để tự động trả lời, duy trì tương tác 2 chiều.
        """
        try:
            self._print("  🔔 Kiểm tra thông báo Phản hồi...")
            res = self.api.get_unread_notifications()
            if res.status_code != 200:
                return

            data = res.json()
            if data.get("code") != "0":
                return
                
            # --- THỰC HIỆN DỌN DẸP TRÍ NHỚ (SLIDING WINDOW 24H TỐI ƯU HIỆU NĂNG) ---
            now = time.time()
            one_day_ago = now - 24 * 3600
            
            # 1. Dọn my_recent_replies (list chứa dict có text và timestamp)
            raw_replies = self.memory.get('my_recent_replies', [])
            my_recent_replies = [r for r in raw_replies if isinstance(r, dict) and r.get('timestamp', 0) > one_day_ago]
            self.memory['my_recent_replies'] = my_recent_replies
            
            # 2. Dọn replied_users_per_post (dict chứa {post_id: {nickname: timestamp}})
            # Cấu trúc này giúp nhớ TẤT CẢ các user đã rep trong 1 bài viết thay vì chỉ người cuối cùng
            raw_users = self.memory.get('replied_users_per_post', self.memory.get('last_replied_users', {}))
            replied_users_per_post = {}
            for pid, user_data in raw_users.items():
                # migration from old structure (post_id: {nickname, timestamp}) to new (post_id: {nickname: timestamp})
                if isinstance(user_data, dict):
                    if "nickname" in user_data: 
                        # old structure
                        if user_data.get('timestamp', 0) > one_day_ago:
                            replied_users_per_post[str(pid)] = {user_data['nickname']: user_data.get('timestamp', 0)}
                    else:
                        # new structure
                        valid_users = {nick: ts for nick, ts in user_data.items() if ts > one_day_ago}
                        if valid_users:
                            replied_users_per_post[str(pid)] = valid_users
                            
            self.memory['replied_users_per_post'] = replied_users_per_post
            
            processed_msgs = self.memory.get('processed_msgs', [])
            is_first_run = len(processed_msgs) == 0
            replies_done = 0
            
            # Lấy list text thuần tuý để so sánh nhanh O(1) hoặc O(N)
            my_recent_texts = {r.get('text', '') for r in my_recent_replies}
            
            msg_list = data.get("data", {}).get("msgList", [])
            for msg in msg_list:
                # 1 = message, subType 1 = comment/reply => Mở rộng thêm SourceType=3 (Reply cho Comment)
                # subType 2 cũng chứa các comment trực tiếp lên bài viết của mình
                if msg.get("eventSourceType") in (1, 3) and msg.get("eventSubType") in (1, 2):
                    comment_text = msg.get("msgText", "")
                    author_info = msg.get("relatedAuthorInfo", {})
                    nickname = author_info.get("nickname", "Ai đó")
                    
                    post_id = str(msg.get("relatedContentId", 0))
                    reply_to_id = msg.get("replyRootId")
                    if not reply_to_id or reply_to_id == 0:
                        reply_to_id = msg.get("relatedCommentId")
                        
                    msg_id = msg.get("msgId")
                    
                    if post_id == "0" or not reply_to_id:
                        continue

                    # 1. Check msg_id vĩnh viễn (Tránh xử lý lại 1 thông báo)
                    if str(msg_id) in processed_msgs:
                        continue
                        
                    # 2. Check Tránh Self-Reply (nếu msgText là nội dung do chính bot sinh ra)
                    if comment_text in my_recent_texts:
                        self.api.mark_notifications_read(msg_id)
                        processed_msgs.append(str(msg_id))
                        continue
                        
                    # 3. Check "Mỗi user trong 1 bài viết chỉ được reply 1 lần, nếu comment tiếp chỉ thả tim"
                    post_users_map = replied_users_per_post.get(post_id, {})
                    if nickname in post_users_map:
                        self._print(f"  ❤️ {nickname} reply lại ở bài {post_id} (Giới hạn 1 rep/user). Chỉ thả tim, skip reply.")
                        self.api.like_comment(reply_to_id)
                        time.sleep(1.0)
                        
                        self.api.mark_notifications_read(msg_id)
                        processed_msgs.append(str(msg_id))
                        continue
                        
                    # Yêu cầu: Không khoá giới hạn người dùng.
                    # Nhưng với lần đầu chạy thuật toán (lịch sử rỗng), chỉ xử lý 10 comment mới nhất
                    if is_first_run and replies_done >= 10:
                        self.api.mark_notifications_read(msg_id)
                        processed_msgs.append(str(msg_id))
                        self.memory['processed_msgs'] = processed_msgs[-1000:]
                        self._save_memory()
                        continue
                    self._print(f"  💬 [Reactive] Trả lời comment của {nickname}: \"{comment_text[:50]}...\"")
                    
                    # Tránh load file prompt liên tục
                    reply_text = self.gemini.generate_reply(
                        post_content="Bài viết phân tích thị trường Crypto",
                        commenter_info=nickname,
                        comment_text=comment_text
                    )
                    
                    if reply_text:
                        reply_text = reply_text.strip().strip('"\'')
                        self.api.like_comment(reply_to_id)
                        time.sleep(1.5)
                        
                        r_res = self.api.reply_comment(post_id, reply_to_id, reply_text)
                        try:
                            r_data = r_res.json()
                        except Exception:
                            r_data = {}
                        if r_res.status_code == 200 and r_data.get("code") == "0":
                            self._print(f"  ✅ [Reactive] Đã trả lời: {reply_text}")
                            
                            # Lưu text vào trí nhớ để lần sau tránh tự reply lại chính mình
                            my_recent_replies.append({"text": reply_text, "timestamp": time.time()})
                            self.memory['my_recent_replies'] = my_recent_replies
                            my_recent_texts.add(reply_text)
                            
                            # Ghi nhớ: Đã trả lời user này trên bài viết này
                            if post_id not in replied_users_per_post:
                                replied_users_per_post[post_id] = {}
                            replied_users_per_post[post_id][nickname] = time.time()
                            self.memory['replied_users_per_post'] = replied_users_per_post
                            
                            # Lưu vào danh sách đã xử lý vĩnh viễn, tránh trùng
                            processed_msgs.append(str(msg_id))
                            self.memory['processed_msgs'] = processed_msgs[-1000:] # Giữ tối đa 1000 ID
                            self._save_memory()
                            
                            replies_done += 1
                                
                    # Cuối cùng đánh dấu đã đọc
                    self.api.mark_notifications_read(msg_id)
                    time.sleep(2)

            # ĐẢM BẢO LƯU TOÀN BỘ TRẠNG THÁI XUỐNG ĐĨA SAU MỖI LẦN QUÉT
            # (Tránh mất processed_msgs, replied_users_per_post khi bot restart)
            self.memory['processed_msgs'] = processed_msgs[-1000:]
            self._save_memory()

        except Exception as e:
            self._print(f"  ❌ Lỗi khi kiểm tra thông báo: {e}")
            # Cố gắng lưu memory ngay cả khi gặp lỗi, tránh mất trạng thái
            try:
                self._save_memory()
            except Exception:
                pass

    # ==================================================================

    def run_forever(self):
        self._print(f"🌱 FARMING STARTED | {self.posts_per_session} posts/session | "
                    f"break={self.session_break}m | "
                    f"long_break={self.long_break}m every {self.sessions_before_long}")

        with self._stats_lock:
            self.stats['start_time'] = datetime.now()

        session_num = 0
        sessions_since_long_break = 0

        while not self._should_stop():
            session_num += 1
            sessions_since_long_break += 1

            try:
                # 0. Check reactive first
                self._check_reactive_interactions()
                self._gaussian_delay('scroll') # fake some UI delay

                session_stats = self._run_session(session_num)

                if self._should_stop():
                    break

                # Cumulative stats — chỉ in khi có giá trị (mỗi session)
                self._print_cumulative()

                # CIRCADIAN REST
                if sessions_since_long_break >= self.sessions_before_long:
                    lb_min, lb_max = self.long_break
                    break_mins = max(lb_min, min(lb_max, random.gauss(
                        (lb_min + lb_max) / 2, (lb_max - lb_min) / 4
                    )))
                    self._print(f"💤 Long break {break_mins:.0f}m "
                                f"(after {sessions_since_long_break} sessions)")
                    self._sleep_interruptible(break_mins * 60)
                    sessions_since_long_break = 0
                else:
                    sb_min, sb_max = self.session_break
                    break_mins = max(sb_min, min(sb_max, random.gauss(
                        (sb_min + sb_max) / 2, (sb_max - sb_min) / 4
                    )))
                    self._print(f"☕ Break {break_mins:.0f}m")
                    self._sleep_interruptible(break_mins * 60)

            except Exception as e:
                self._print(f"❌ Session error: {e}")
                # Thử recovery session trước khi chờ
                self.api.refresh_session()
                self._sleep_interruptible(30)

        # FINAL
        self._print(f"🛑 FARMING STOPPED")
        self._print_cumulative()

    # ==================================================================
    # STATS
    # ==================================================================

    def _print_cumulative(self):
        """In thống kê tích luỹ — so sánh target vs actual"""
        with self._stats_lock:
            s = self.stats.copy()

        if not s['start_time']:
            return

        elapsed = (datetime.now() - s['start_time']).total_seconds()
        if elapsed < 60:
            return  # Không in nếu chưa đủ 1 phút
        hours = elapsed / 3600

        # Tính rate/h thực tế
        v_h = s['total_views'] / hours
        l_h = s['total_likes'] / hours
        c_h = s['total_comments'] / hours
        f_h = s['total_follows'] / hours

        # So sánh với target
        def delta(actual, target):
            if target == 0:
                return ""
            pct = ((actual - target) / target) * 100
            sign = "+" if pct >= 0 else ""
            return f" ({sign}{pct:.0f}%)"

        ai_rate = f"{s['ai_success']}/{s['ai_calls']}" if s['ai_calls'] > 0 else "0/0"
        ai_pct = f" ({s['ai_success']/s['ai_calls']*100:.0f}%)" if s['ai_calls'] > 0 else ""

        self._print(f"📊 {elapsed/60:.0f}m | "
                    f"👁 {s['total_views']} ({v_h:.0f}/h{delta(v_h, 500)}) | "
                    f"❤️ {s['total_likes']} ({l_h:.0f}/h{delta(l_h, 30)}) | "
                    f"💬 {s['total_comments']} ({c_h:.0f}/h{delta(c_h, 15)}) | "
                    f"➕ {s['total_follows']} ({f_h:.0f}/h{delta(f_h, 5)}) | "
                    f"AI={ai_rate}{ai_pct} | "
                    f"Err={s['api_errors']}")

    def get_stats(self) -> Dict:
        with self._stats_lock:
            return self.stats.copy()

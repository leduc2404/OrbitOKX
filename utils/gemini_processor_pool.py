"""
Gemini Processor Pool cho OKX
Xử lý posts với Gemini API sử dụng thread pool
Chỉ xử lý 1 lần cho mỗi bài (không chia group như Binance)
"""
import gc
import os
import random
import time
import threading
from collections import deque
from typing import Optional, Tuple, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

from utils.database_manager import DatabaseManager
from utils.gemini_processor import GeminiProcessor
from utils.post_scheduler import HourlyPostingScheduler

class GeminiProcessorPool:
    """
    Pool xử lý posts với Gemini
    Hỗ trợ concurrent processing
    """
    
    def __init__(self, max_workers: int = None):
        """
        Args:
            max_workers: Số workers xử lý đồng thời (None = auto-calculate)
        """
        self._batch_lock = threading.Lock()
        self.db_manager = DatabaseManager()
        
        try:
            self.processor = GeminiProcessor()
            print(f"✓ Gemini processor initialized")
        except Exception as e:
            raise Exception(f"Failed to initialize Gemini processor: {e}")
        
        if max_workers is None:
            provider_config = self.processor.provider_config
            proc_config = provider_config.get('processing', {})
            active = self.processor.active_provider
            
            config_workers = proc_config.get('max_workers')
            if config_workers is not None:
                self.max_workers = config_workers
                print(f"✓ [{active}] Workers from config: {self.max_workers}")
            else:
                if self.processor.key_manager:
                    self.max_workers = self.processor.key_manager.num_keys
                    print(f"✓ [{active}] Workers = {self.max_workers} (matching API keys)")
                elif active == 'gemini_proxy':
                    self.max_workers = 5
                    print(f"✓ [{active}] Workers = {self.max_workers} (proxy default)")
                else:
                    valid_keys = [k for k in provider_config.get('api_keys', []) if k and 'YOUR_' not in k]
                    self.max_workers = max(len(valid_keys), 1)
                    print(f"✓ [{active}] Workers = {self.max_workers} (key count)")
        else:
            self.max_workers = max_workers
        
        processing_config = self.processor.config.get('processing', {})
        self.max_post_retries = processing_config.get('max_post_retries', 3)
        self.failed_post_cooldown_minutes = processing_config.get('failed_post_cooldown_minutes', 5)
        self.retry_failed_in_next_cycle = processing_config.get('retry_failed_in_next_cycle', True)
        
        print(f"✓ Retry config: max_retries={self.max_post_retries}, cooldown={self.failed_post_cooldown_minutes}min")
        print(f"✓ Processing mode: SINGLE OUTPUT for OKX")
        self._load_prompt_templates()

    def _load_prompt_templates(self):
        """
        Nạp pool prompt hợp nhất (Unified Pool) từ tất cả thư mục variants (group1 + group2).

        Ưu tiên thư mục variant (mỗi file .txt là một prompt hoàn chỉnh chứa {text}).
        Nếu thư mục không tồn tại/rỗng -> fallback về file template đơn legacy.
        """
        import yaml
        self.templates: Dict[str, List[Dict[str, str]]] = {}
        self._rotation_lock = threading.Lock()

        variant_dirs = ['config/prompts/group1', 'config/prompts/group2']
        fallback_paths = [
            'config/gemini_prompt_template_group1.txt',
            'config/gemini_prompt_template.txt',
        ]
        rotation_cfg = {}
        self.all_sources = ["binance", "okx", "sosovalue", "KuCoin", "okx_trending"]

        if os.path.exists("config/settings.yaml"):
            try:
                with open("config/settings.yaml", "r", encoding="utf-8") as f:
                    settings = yaml.safe_load(f) or {}
                    pool_cfg = settings.get('content_pool', {}) or {}
                    if pool_cfg:
                        variant_dirs = pool_cfg.get('prompt_variants_dirs', variant_dirs) or variant_dirs
                        fallback_paths = pool_cfg.get('prompt_templates_fallback', fallback_paths) or fallback_paths
                        self.all_sources = pool_cfg.get('sources', self.all_sources) or self.all_sources
                    else:
                        groups_cfg = settings.get('account_groups', {}) or {}
                        if groups_cfg:
                            g1 = groups_cfg.get('group1', {}) or {}
                            g2 = groups_cfg.get('group2', {}) or {}
                            v1_dir = g1.get('prompt_variants_dir', variant_dirs[0])
                            v2_dir = g2.get('prompt_variants_dir', variant_dirs[1])
                            variant_dirs = [v1_dir, v2_dir]
                            t1_path = g1.get('prompt_template', fallback_paths[0])
                            t2_path = g2.get('prompt_template', fallback_paths[1])
                            fallback_paths = [t1_path, t2_path]
                    rotation_cfg = settings.get('prompt_rotation', {}) or {}
            except Exception as e:
                print(f"  ⚠ Lỗi đọc settings.yaml cho prompt rotation: {e}")

        self.rotation_enabled = bool(rotation_cfg.get('enabled', True))
        self.avoid_recent = max(0, int(rotation_cfg.get('avoid_recent', 8)))

        unified_pool: List[Dict[str, str]] = []
        seen_ids = set()
        for idx, v_dir in enumerate(variant_dirs):
            fb_path = fallback_paths[idx] if idx < len(fallback_paths) else fallback_paths[-1]
            sub_pool = self._load_variant_pool(v_dir, fb_path, idx == 0)
            folder_tag = os.path.basename(os.path.normpath(v_dir))
            for item in sub_pool:
                vid = f"{folder_tag}/{item['id']}" if item['id'] != 'legacy' else f"{folder_tag}/legacy"
                if vid not in seen_ids:
                    seen_ids.add(vid)
                    unified_pool.append({'id': vid, 'template': item['template']})

        if len(unified_pool) > 1:
            self.avoid_recent = min(self.avoid_recent, len(unified_pool) - 1)

        self._recent_variants = {
            'unified': deque(maxlen=max(1, self.avoid_recent)),
        }

        self.templates['unified'] = unified_pool
        # Giữ alias tương thích ngược
        self.templates['group1'] = unified_pool
        self.templates['group2'] = unified_pool

        mode = "rotation" if self.rotation_enabled else "fixed"
        print(
            f"✓ Unified Prompt Pool: {len(unified_pool)} prompt variants "
            f"({mode}, avoid_recent={self.avoid_recent}) từ {', '.join(variant_dirs)}"
        )

    def _load_variant_pool(self, variants_dir: str, fallback_path: str,
                           fallback_is_group1: bool = False) -> List[Dict[str, str]]:
        """Đọc tất cả .txt trong thư mục variant thành pool; rỗng thì dùng template legacy."""
        pool: List[Dict[str, str]] = []

        if os.path.isdir(variants_dir):
            for name in sorted(os.listdir(variants_dir)):
                if not name.lower().endswith('.txt'):
                    continue
                path = os.path.join(variants_dir, name)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        content = f.read()
                except Exception as e:
                    print(f"  ⚠ Không đọc được prompt variant {path}: {e}")
                    continue
                if '{text}' not in content:
                    print(f"  ⚠ Bỏ qua {path}: thiếu placeholder input")
                    continue
                pool.append({'id': os.path.splitext(name)[0], 'template': content})

        if pool:
            return pool

        return [{'id': 'legacy', 'template': self._read_template_file(fallback_path, fallback_is_group1)}]

    def _pick_variant(self, group_key: str = 'unified') -> Tuple[str, str]:
        """
        Chọn 1 prompt variant từ pool hợp nhất, ngẫu nhiên nhưng không lặp lại
        avoid_recent variant vừa dùng (tránh Gemini nhận diện nội dung trùng lặp).
        """
        pool = self.templates.get('unified') or self.templates.get(group_key) or []
        if not pool:
            return "", "none"

        if len(pool) == 1:
            return pool[0]['template'], pool[0]['id']

        with self._rotation_lock:
            if not self.rotation_enabled:
                variant = pool[0]
            else:
                recent = self._recent_variants.setdefault(
                    'unified', deque(maxlen=max(1, self.avoid_recent))
                )
                blocked = set(recent)
                candidates = [v for v in pool if v['id'] not in blocked] or pool
                variant = random.choice(candidates)
                recent.append(variant['id'])

        return variant['template'], variant['id']

    def _read_template_file(self, file_path: str, fallback_is_group1: bool = False) -> str:
        import os
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                print(f"  ⚠ Cannot read prompt template {file_path}: {e}")
        
        # Fallback to default
        if not fallback_is_group1:
            default_path = "config/gemini_prompt_template.txt"
            if os.path.exists(default_path):
                try:
                    with open(default_path, 'r', encoding='utf-8') as f:
                        return f.read()
                except Exception:
                    pass
        # Hardcoded default
        return """Viết lại nội dung sau cho Binance Square:

{text}

Yêu cầu:
- Thêm emoji phù hợp
- Thêm 3-5 hashtags
- Độ dài 200-400 từ
- Xuống dòng sau mỗi Entry/Target/Stop Loss
"""
    
    def _process_single_post(self, post: Dict) -> Tuple[str, bool, Optional[str]]:
        post_start_time = time.time()
        post_id = post['id']
        text = post['text']
        source = post.get('source', 'okx')
        
        # Chọn prompt variant từ pool hợp nhất (20 variants) cho mọi nguồn bài
        template, variant_id = self._pick_variant('unified')
        print(f"  🎲 {post_id} [{source}]: prompt variant [{variant_id}]")

        image_paths_str = post.get('image_paths', '')
        image_paths = [p.strip() for p in image_paths_str.split('|') if p.strip()] if image_paths_str else []
        current_retry = post.get('retry_count', 0) or 0
        
        if current_retry >= self.max_post_retries:
            error_msg = f"Exceeded max retries ({self.max_post_retries})"
            self.db_manager.skip_post_permanently(post_id, error_msg)
            return (post_id, False, None)
        
        max_attempts = 3
        last_error = None

        # Thread-safe prompt override cho worker hiện tại
        try:
            formatted_prompt = template.format(text=text) if '{text}' in template else f"{template}\n\n{text}"
        except Exception:
            formatted_prompt = template.replace('{text}', text)

        self.processor._thread_local.prompt_override = formatted_prompt
        try:
            for attempt in range(max_attempts):
                try:
                    # XỬ LÝ LẦN 1 - DUY NHẤT CHO OKX
                    result = self.processor.process_text(text, f"{post_id}", image_paths=image_paths)
                    
                    if not result:
                        last_error = "Failed - no result from Gemini"
                        if attempt < max_attempts - 1:
                            print(f"  ⚠ {post_id}: Gemini failed, retry {attempt+1}/{max_attempts}")
                            time.sleep(3 * (attempt + 1))
                            continue
                        else:
                            self.db_manager.mark_post_gemini_failed(post_id, last_error)
                            return (post_id, False, None)
                    
                    output_text, model = result
                    
                    # Bỏ qua ngay lập tức nếu Gemini từ chối do chính sách/chính trị
                    if output_text == "[REJECT]":
                        print(f"  [🚫] {post_id}: Gemini từ chối xử lý (nội dung chính trị/vi phạm chính sách)!")
                        self.db_manager.skip_post_permanently(post_id, "Nội dung chính trị/nhạy cảm bị Gemini từ chối")
                        return (post_id, False, None)
                    
                    if not self._validate_output(output_text):
                        last_error = "Gemini output too short or invalid"
                        if attempt < max_attempts - 1:
                            print(f"  ⚠ {post_id}: output invalid, retry {attempt+1}/{max_attempts}")
                            time.sleep(3 * (attempt + 1))
                            continue
                        else:
                            self.db_manager.mark_post_gemini_failed(post_id, last_error)
                            return (post_id, False, None)
                    
                    # Cập nhật kết quả vào DB
                    success = self.db_manager.update_gemini_output(post_id, output_text, model)
                    if not success:
                        error_msg = "Failed to update Gemini output in DB"
                        self.db_manager.mark_post_gemini_failed(post_id, error_msg)
                        return (post_id, False, None)
                    
                    post_duration = time.time() - post_start_time
                    if post_duration > 60:
                        print(f"  ⚠ {post_id}: Slow post processing: {post_duration:.1f}s")
                        
                    return (post_id, True, model)
                
                except Exception as e:
                    last_error = str(e)
                    if attempt < max_attempts - 1:
                        print(f"  ⚠ {post_id}: Exception, retry {attempt+1}/{max_attempts}: {e}")
                        time.sleep(3 * (attempt + 1))
                    else:
                        post_duration = time.time() - post_start_time
                        print(f"  ✗ {post_id}: Failed after {post_duration:.1f}s: {last_error[:100]}")
                        self.db_manager.mark_post_gemini_failed(post_id, last_error)
                        return (post_id, False, None)
        finally:
            self.processor._thread_local.prompt_override = None
        
        self.db_manager.mark_post_gemini_failed(post_id, last_error or "Unknown error")
        return (post_id, False, None)
    
    def _validate_output(self, output: str) -> bool:
        if not output:
            return False
        stripped = output.strip()
        if len(stripped) < 50:
            return False
        error_indicators = ['error', 'exception', 'failed', 'unable to']
        if any(indicator in stripped.lower()[:100] for indicator in error_indicators):
            return False
        return True
    
    def process_batch(self, batch_size: Optional[int] = None, parallel: bool = True) -> Dict:
        with self._batch_lock:
            print("\n" + "=" * 80)
            print("GEMINI PROCESSING (UNIFIED POOL MODE)")
            print("=" * 80)

            import yaml

            settings = {}
            all_sources = list(self.all_sources)
            active_account_count = 1

            if os.path.exists("config/settings.yaml"):
                try:
                    with open("config/settings.yaml", "r", encoding="utf-8") as f:
                        settings = yaml.safe_load(f) or {}
                    pool_cfg = settings.get("content_pool") or {}
                    if pool_cfg.get("sources"):
                        all_sources = list(pool_cfg["sources"])
                    acc_activity = settings.get("acc_activity") or settings.get("active_accounts") or []
                    active_account_count = max(1, len(acc_activity))
                except Exception as e:
                    print(f"  ⚠ Lỗi load settings.yaml trong processor pool: {e}")

            scheduler = HourlyPostingScheduler(settings)
            buffer_plan = scheduler.compute_buffer_target(active_account_count, settings)
            target_ready = buffer_plan["target_ready"]

            ready_count = self.db_manager.count_ready_posts_by_sources(
                all_sources, include_processing=True
            )
            slots_avail = max(0, target_ready - ready_count)
            if batch_size is not None:
                slots_avail = min(slots_avail, max(0, int(batch_size)))

            if slots_avail <= 0:
                print(
                    f"  [⏳] Unified ready buffer đã đủ ({ready_count}/{target_ready} bài, "
                    f"accounts={active_account_count}, peak≈{buffer_plan['peak_per_account']:.1f} bài/h/acc). "
                    f"Bỏ qua xử lý Gemini (0 token tiêu thụ)."
                )
                return {'total': 0, 'success': 0, 'failed': 0, 'retried': 0}

            print(
                f"  [📥] Unified ready buffer: {ready_count}/{target_ready} "
                f"(accounts={active_account_count}, peak≈{buffer_plan['peak_per_account']:.1f} bài/h/acc). "
                f"Chỉ xử lý tối đa {slots_avail} bài mới nhất."
            )

            new_posts = self.db_manager.get_unprocessed_posts(
                limit=slots_avail, sources=all_sources
            )

            failed_posts = []
            remaining_slots = max(0, slots_avail - len(new_posts))
            if remaining_slots > 0 and self.retry_failed_in_next_cycle:
                failed_posts = self.db_manager.get_failed_posts_for_retry(
                    limit=remaining_slots,
                    cooldown_minutes=self.failed_post_cooldown_minutes,
                    max_retries=self.max_post_retries
                )
                if failed_posts:
                    print(f"\n🔄 Found {len(failed_posts)} failed posts ready for retry")

            posts = (new_posts + failed_posts)[:slots_avail]

            if not posts:
                print("\n○ No posts to process")
                return {'total': 0, 'success': 0, 'failed': 0, 'retried': 0}

            print(f"\n→ Processing {len(new_posts)} new posts + {len(failed_posts)} retry posts = {len(posts)} total")

            if parallel:
                print(f"→ Processing with {self.max_workers} workers (parallel)")
                return self._process_parallel(posts)
            else:
                print(f"→ Processing sequentially")
                return self._process_sequential(posts)
    
    def _process_sequential(self, posts: List[Dict]) -> Dict:
        stats = {'total': len(posts), 'success': 0, 'failed': 0, 'models': {}}
        start_time = time.time()
        
        for idx, post in enumerate(posts, 1):
            print(f"\n[{idx}/{len(posts)}] Processing {post['id']}...", end=' ')
            post_id, success, model = self._process_single_post(post)
            
            if success:
                stats['success'] += 1
                stats['models'][model] = stats['models'].get(model, 0) + 1
                print(f"✓ {model}")
            else:
                stats['failed'] += 1
                print("✗")
            
            if idx < len(posts):
                time.sleep(1)
        
        elapsed = time.time() - start_time
        self._print_results(stats, elapsed)
        gc.collect()
        return stats
    
    def _process_parallel(self, posts: List[Dict]) -> Dict:
        stats = {'total': len(posts), 'success': 0, 'failed': 0, 'models': {}}
        start_time = time.time()
        
        actual_workers = min(self.max_workers, len(posts))
        if actual_workers < self.max_workers:
            print(f"  → Adjusted workers from {self.max_workers} to {actual_workers} (matching post count)")
        
        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            futures = {executor.submit(self._process_single_post, post): post for post in posts}
            completed = 0
            for future in as_completed(futures):
                post = futures[future]
                completed += 1
                try:
                    post_id, success, model = future.result(timeout=60)
                    if success:
                        stats['success'] += 1
                        stats['models'][model] = stats['models'].get(model, 0) + 1
                        print(f"[{completed}/{len(posts)}] ✓ {post_id} - {model}")
                    else:
                        stats['failed'] += 1
                        print(f"[{completed}/{len(posts)}] ✗ {post_id}")
                except FuturesTimeoutError:
                    stats['failed'] += 1
                    print(f"[{completed}/{len(posts)}] ⏰ {post['id']}: POOL TIMEOUT (>60s)")
                except Exception as e:
                    stats['failed'] += 1
                    print(f"[{completed}/{len(posts)}] ✗ {post['id']}: {e}")
        
        elapsed = time.time() - start_time
        self._print_results(stats, elapsed)
        gc.collect()
        return stats
    
    def _print_results(self, stats: Dict, elapsed: float):
        print("\n" + "=" * 80)
        print("PROCESSING RESULTS")
        print("=" * 80)
        total = stats['total']
        success = stats['success']
        failed = stats['failed']
        
        print(f"Total: {total}")
        if total > 0:
            print(f"✓ Success: {success} ({success/total*100:.1f}%)")
            print(f"✗ Failed: {failed} ({failed/total*100:.1f}%)")
            print(f"⏱ Time: {elapsed:.1f}s ({elapsed/total:.1f}s/post)")
        
        if stats.get('models') and success > 0:
            print("\nModels used:")
            for model, count in sorted(stats['models'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {model}: {count} ({count/success*100:.1f}%)")
        
        retry_stats = self.db_manager.get_retry_stats()
        if retry_stats['failed_awaiting_retry'] > 0 or retry_stats['permanently_skipped'] > 0:
            print(f"\n🔄 Retry Stats:")
            print(f"  → Awaiting retry: {retry_stats['failed_awaiting_retry']}")
            print(f"  → Permanently skipped: {retry_stats['permanently_skipped']}")
        
        print("=" * 80)
        print("\nDETAILED PROCESSOR STATS:")
        self.processor.print_stats()

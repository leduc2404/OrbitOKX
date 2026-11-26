"""
Gemini API Processor - 3 Provider Architecture
================================================
Hỗ trợ 3 provider độc lập:
  - gemma: Sliding Window Rate Limiter (đã ổn định)
  - gemini_proxy: Gọi qua proxy trung gian (OpenAI-compatible protocol)
  - gemini_direct: Gọi trực tiếp Google API (google.genai Client)

Mỗi provider có config riêng: key, model, rate limit, generation params.
"""
from openai import OpenAI
from google import genai as new_genai
from google.genai import types as new_genai_types
import yaml
import time
import os
import gc
import re
import random
from threading import Lock, local
from collections import defaultdict, deque
from typing import Any, Optional, Dict, List, Tuple

# Sliding Window Rate Limiter cho Gemma
from infra.rate_limiter import ParallelKeyManager
from infra.logging_config import get_logger, make_log_print
from services.processor.content_tags import REJECTION_KEYWORDS, classify_content_angles

logger = get_logger(__name__)
print = make_log_print(logger)

# ======================================================================
# OKXSquare: pre-screen nhạy cảm + nhận diện refusal (dùng chung từ content_tags)
# ======================================================================
_REJECTION_KEYWORDS = list(REJECTION_KEYWORDS)

_SENSITIVE_PATTERNS = [
    r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bputin\b', r'\bzelensky\b',
    r'\bnetanyahu\b', r'\bxi jinping\b', r'\bisrael\b', r'\biran\b', r'\bgaza\b',
    r'\bpalestine\b', r'\bhamas\b', r'\bhezbollah\b', r'\bukraine\b', r'\brussia\b',
    r'\btaiwan\b', r'\bu\.?s\.?\s+president\b', r'\bpresident\s+(?:trump|biden|xi|putin|zelensky)\b', r'\bprime minister\b', r'\bwhite house\b',
    r'\bkremlin\b', r'\belection\b', r'\belections\b', r'\bparliament\b',
    r'\bcongress\b', r'\bsenate\b', r'\bclarity act\b', r'\bgeopolitical\b',
    r'\bgeopolitics\b', r'\bcentral bank\b', r'\bcentral banks\b',
    r'\bgold reserves\b', r'\bfed\b',
    r'\btổng thống\b', r'\bbầu cử\b', r'\bchính phủ\b', r'\bnghị viện\b',
    r'\bquốc hội\b', r'\bchính trị\b', r'\bđịa chính trị\b', r'\bchiến tranh\b',
    r'\bxung đột\b', r'\bngân hàng trung ương\b', r'\bdự trữ vàng\b',
    r'\btrung quốc\b', r'\bnước mỹ\b', r'\bhoa kỳ\b',
    r'\bporn\b', r'\bpornography\b', r'\bsexual\b', r'\bsuicide\b', r'\bself-harm\b',
    r'\bkhiêu dâm\b', r'\bbạo lực\b', r'\btự hại\b',
    r'\bmoney laundering\b', r'\bpump\s*&\s*dump\b', r'\bpump\s*and\s*dump\b',
    r'\brửa tiền\b', r'\bđa cấp\b', r'\bbơm xả\b', r'\blừa đảo\b',
    r'\bico\b', r'\bido\b', r'\bieo\b', r'\bairdrop\b', r'\bgiveaway\b',
    r'\bhuy động vốn\b',
]


# ===========================================================================
# SimpleModelTracker: Rate tracking cho Gemini Proxy / Gemini Direct
# (Sequential mode fallback, single-key proxy, hoặc multi-key direct)
# ===========================================================================

class SimpleModelTracker:
    """
    Track models với cooldown và time-based rate limiting.
    Dùng cho gemini_proxy (single key) và gemini_direct (multi-key sequential).
    """
    def __init__(self, cooldown_seconds: int = 60, min_delay_seconds: float = 2.0):
        self.lock = Lock()
        self.cooldown_seconds = cooldown_seconds
        self.min_delay_seconds = min_delay_seconds
        self.model_cooldown: Dict[str, float] = {}
        self.key_exhausted: Dict[str, Dict[str, float]] = {}
        self.last_request_time: Dict[str, float] = {}
        self.total_calls = defaultdict(int)
        self.model_calls = defaultdict(int)
        self.key_index = 0
        self._max_stats_entries = 50
    
    def wait_for_slot(self, api_key: str) -> float:
        """
        Đợi đến khi có thể gọi API với key này.
        Đảm bảo khoảng cách min_delay_seconds giữa các requests cùng key.
        """
        key_short = api_key[:10]
        waited = 0.0
        
        with self.lock:
            now = time.time()
            last_time = self.last_request_time.get(key_short, 0)
            elapsed = now - last_time
            
            if elapsed < self.min_delay_seconds:
                wait_time = self.min_delay_seconds - elapsed
                self.lock.release()
                try:
                    time.sleep(wait_time)
                    waited = wait_time
                finally:
                    self.lock.acquire()
            
            self.last_request_time[key_short] = time.time()
        
        return waited
    
    def is_model_available(self, model_name: str, api_key: str = None) -> bool:
        """Kiểm tra model còn dùng được không"""
        with self.lock:
            now = time.time()
            
            if api_key:
                key_short = api_key[:10]
                if key_short in self.key_exhausted:
                    if model_name in self.key_exhausted[key_short]:
                        exhausted_time = self.key_exhausted[key_short][model_name]
                        if now - exhausted_time < self.cooldown_seconds:
                            return False
                        else:
                            del self.key_exhausted[key_short][model_name]
            
            if model_name in self.model_cooldown:
                exhausted_time = self.model_cooldown[model_name]
                if now - exhausted_time < self.cooldown_seconds:
                    return False
                else:
                    del self.model_cooldown[model_name]
            
            return True
    
    def mark_model_exhausted(self, model_name: str, api_key: str = None):
        """Đánh dấu model đã hết quota với cooldown"""
        with self.lock:
            now = time.time()
            
            if api_key:
                key_short = api_key[:10]
                if key_short not in self.key_exhausted:
                    self.key_exhausted[key_short] = {}
                self.key_exhausted[key_short][model_name] = now
                print(f"  ⚠ Key {key_short}... exhausted for {model_name}, cooldown {self.cooldown_seconds}s")
            else:
                self.model_cooldown[model_name] = now
    
    def get_next_key_index(self, num_keys: int) -> int:
        """Round-robin key index"""
        with self.lock:
            idx = self.key_index
            self.key_index = (self.key_index + 1) % num_keys
            return idx
    
    def record_success(self, api_key: str, model_name: str):
        """Ghi nhận thành công"""
        with self.lock:
            if len(self.total_calls) > self._max_stats_entries:
                self.total_calls.clear()
            if len(self.model_calls) > self._max_stats_entries:
                self.model_calls.clear()
            self.total_calls[api_key[:10]] += 1
            self.model_calls[model_name] += 1
    
    def reset_all(self):
        """Reset tất cả"""
        with self.lock:
            self.model_cooldown.clear()
            self.key_exhausted.clear()
            self.last_request_time.clear()
    
    def get_stats(self) -> Dict:
        """Lấy thống kê"""
        with self.lock:
            now = time.time()
            cooling_models = [
                f"{m} ({int(self.cooldown_seconds - (now - t))}s left)" 
                for m, t in self.model_cooldown.items() 
                if now - t < self.cooldown_seconds
            ]
        return {
            'cooling_models': cooling_models,
            'total_calls': dict(self.total_calls),
            'model_calls': dict(self.model_calls)
        }


class GeminiProcessor:
    """
    Xử lý text với 3 provider:
      - gemma: Sliding Window Rate Limiter (ParallelKeyManager)
      - gemini_proxy: Proxy trung gian (genai + transport="rest")
      - gemini_direct: Trực tiếp Google API (google.genai Client)
    """
    
    def __init__(self, config_path: str = "config/gemini_config.yaml", _provider_override: Optional[str] = None):
        """Khởi tạo processor dựa trên active_provider trong config"""
        self._config_path = config_path
        self.config = self._load_config(config_path)
        
        # Xác định provider đang active
        self.active_provider = (_provider_override or self.config.get('active_provider', 'gemma')).lower()
        
        # Validate active_provider
        valid_providers = ['gemma', 'gemini_proxy', 'gemini_direct']
        if self.active_provider not in valid_providers:
            raise ValueError(f"active_provider phải là: {valid_providers}, got: '{self.active_provider}'")
        
        # Load provider-specific config
        self.provider_config = self.config.get(self.active_provider, {})
        if not self.provider_config.get('enabled', False):
            raise ValueError(f"Provider '{self.active_provider}' chưa enabled trong config!")
        
        # Load processing config chung
        self.global_processing = self.config.get('processing', {})
        
        # Init provider-specific components
        self.key_manager = None  # Chỉ dùng cho gemma và gemini_direct (parallel)
        self.tracker = None      # Dùng cho gemini_proxy và gemini_direct (sequential)
        
        # ============================================================
        # INIT THEO TỪNG PROVIDER
        # ============================================================
        if self.active_provider == 'gemma':
            self._init_gemma()
        elif self.active_provider == 'gemini_proxy':
            self._init_gemini_proxy()
        elif self.active_provider == 'gemini_direct':
            self._init_gemini_direct()
        
        # Load prompt template và system prompt
        self.prompts_dir = "config/prompts"
        self.prompt_template = self._load_prompt_template()  # fallback chung
        self.system_prompt = self._load_system_prompt()

        # OKXSquare: 1 pool thống nhất gồm 10 prompt tinh gọn trong config/prompts/variants/*.txt
        # Routing thông minh theo nội dung bài gốc + avoid_recent để đa dạng góc nhìn.
        self._rotation_lock = Lock()
        self._tls = local()
        self._avoid_recent_cfg = 6
        self._variant_pool = self._load_variant_pool(
            os.path.join(self.prompts_dir, "variants"),
            os.path.join(self.prompts_dir, "gemini_prompt_template.txt"),
        )
        self._variant_pools = {1: self._variant_pool, 2: self._variant_pool}
        cap = max(1, min(self._avoid_recent_cfg, len(self._variant_pool) - 1)) if self._variant_pool else 1
        self._recent_variants = deque(maxlen=cap)
        print(f"[OK] Prompt pool: {len(self._variant_pool)} variants (avoid_recent={cap})")
        
        # Stats (thread-safe)
        self._stats_lock = Lock()
        self.stats = {
            'total_processed': 0,
            'success': 0,
            'failed': 0,
            'truncated': 0,
            'api_key_usage': defaultdict(int),
            'model_usage': defaultdict(int),
            'finish_reasons': defaultdict(int)
        }
        
        # Cache for genai model instances (thread-safe)
        self._cache_lock = Lock()
        self._model_cache = {}
        self._max_cache_size = 30
        self._fallback_lock = Lock()
        self._fallback_gemma = None
        self._fallback_attempted = bool(_provider_override)

    def _get_gemma_fallback(self):
        """Khởi tạo lazy Gemma fallback nếu active_provider='gemini_proxy' gặp lỗi."""
        if self._fallback_gemma is not None or self._fallback_attempted:
            return self._fallback_gemma
        with self._fallback_lock:
            if self._fallback_gemma is not None or self._fallback_attempted:
                return self._fallback_gemma
            self._fallback_attempted = True
            gemma_cfg = self.config.get('gemma') or {}
            keys = [k for k in (gemma_cfg.get('api_keys') or []) if k and 'YOUR_' not in k]
            if gemma_cfg.get('enabled') and keys:
                try:
                    self._fallback_gemma = GeminiProcessor(
                        config_path=self._config_path,
                        _provider_override='gemma',
                    )
                    print("  ↪ Đã kích hoạt fallback sang GEMMA (khi gemini_proxy lỗi)")
                except Exception as e:
                    print(f"  ⚠ Không khởi tạo được Gemma fallback: {e}")
                    self._fallback_gemma = None
            return self._fallback_gemma
    
    # ==================================================================
    # PROVIDER INITIALIZATION
    # ==================================================================
    
    def _init_gemma(self):
        """Init Gemma: Sliding Window + ParallelKeyManager"""
        valid_keys = [k for k in self.provider_config.get('api_keys', []) 
                      if k and 'YOUR_' not in k]
        
        if not valid_keys:
            raise ValueError("Gemma: Không có API keys hợp lệ!")
        
        rate_config = self.provider_config.get('rate_limiting', {})
        proc_config = self.provider_config.get('processing', {})
        
        parallel_mode = proc_config.get('parallel_mode', True)
        
        if parallel_mode and len(valid_keys) > 1:
            rpm_per_key = rate_config.get('rpm_per_key', 10)
            penalty_cooldown = rate_config.get('penalty_cooldown', 60.0)
            
            self.key_manager = ParallelKeyManager(
                api_keys=valid_keys,
                rpm=rpm_per_key,
                penalty_cooldown=penalty_cooldown
            )
            print(f"[OK] GEMMA Sliding Window Mode: {len(valid_keys)} keys, "
                  f"{rpm_per_key} RPM/key, penalty={penalty_cooldown}s")
        else:
            cooldown = proc_config.get('model_cooldown_seconds', 40)
            min_delay = rate_config.get('min_delay_seconds', 4.0)
            self.tracker = SimpleModelTracker(
                cooldown_seconds=cooldown,
                min_delay_seconds=min_delay
            )
            print(f"✓ Gemma Sequential mode: min_delay={min_delay}s, cooldown={cooldown}s")
    
    def _init_gemini_proxy(self):
        """Init Gemini Proxy: single key + endpoint + SimpleModelTracker"""
        api_key = self.provider_config.get('api_key', '')
        endpoint = self.provider_config.get('endpoint', '')
        model = self.provider_config.get('model', '')
        
        if not api_key:
            raise ValueError("gemini_proxy: Thiếu api_key!")
        if not endpoint:
            raise ValueError("gemini_proxy: Thiếu endpoint!")
        if not model:
            raise ValueError("gemini_proxy: Thiếu model!")
        
        rate_config = self.provider_config.get('rate_limiting', {})
        proc_config = self.provider_config.get('processing', {})
        
        cooldown = rate_config.get('cooldown_seconds', 40)
        min_delay = rate_config.get('min_delay_seconds', 2.0)
        max_workers = proc_config.get('max_workers', 5)
        
        self.tracker = SimpleModelTracker(
            cooldown_seconds=cooldown,
            min_delay_seconds=min_delay
        )
        
        print("[OK] GEMINI PROXY Mode:")
        print(f"  → Endpoint: {endpoint}")
        print(f"  → Model: {model}")
        print(f"  → Key: ***redacted*** (len={len(api_key)})")
        print(f"  → Workers: {max_workers}, min_delay={min_delay}s, cooldown={cooldown}s")
    
    def _init_gemini_direct(self):
        """Init Gemini Direct: multi-key hoặc single-key, gọi trực tiếp Google"""
        valid_keys = [k for k in self.provider_config.get('api_keys', []) 
                      if k and 'YOUR_' not in k]
        
        if not valid_keys:
            raise ValueError("gemini_direct: Không có API keys hợp lệ!")
        
        model = self.provider_config.get('model', '')
        if not model:
            raise ValueError("gemini_direct: Thiếu model!")
        
        rate_config = self.provider_config.get('rate_limiting', {})
        proc_config = self.provider_config.get('processing', {})
        
        parallel_mode = proc_config.get('parallel_mode', True)
        
        if parallel_mode and len(valid_keys) > 1:
            # Dùng ParallelKeyManager giống Gemma
            rpm_per_key = rate_config.get('rpm_per_key', 10)
            penalty_cooldown = rate_config.get('penalty_cooldown', 60.0)
            
            self.key_manager = ParallelKeyManager(
                api_keys=valid_keys,
                rpm=rpm_per_key,
                penalty_cooldown=penalty_cooldown
            )
            print(f"[OK] GEMINI DIRECT Parallel Mode: {len(valid_keys)} keys, "
                  f"{rpm_per_key} RPM/key, model={model}")
        else:
            cooldown = proc_config.get('cooldown_seconds', 40)
            min_delay = rate_config.get('min_delay_seconds', 4.0)
            self.tracker = SimpleModelTracker(
                cooldown_seconds=cooldown,
                min_delay_seconds=min_delay
            )
            print(f"✓ Gemini Direct Sequential mode: {len(valid_keys)} keys, "
                  f"min_delay={min_delay}s, model={model}")
    
    # ==================================================================
    # CONFIG LOADING
    # ==================================================================
    
    def _load_config(self, config_path: str) -> dict:
        """Load config từ YAML"""
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # Validate: phải có active_provider
        active = config.get('active_provider', '').lower()
        if not active:
            raise ValueError("Config thiếu 'active_provider'! "
                             "Chọn: 'gemma', 'gemini_proxy', hoặc 'gemini_direct'")
        
        # Validate provider config exists
        if active not in config:
            raise ValueError(f"Config thiếu section '{active}'!")
        
        return config
    
    def _load_prompt_template(self) -> str:
        """Load prompt template fallback chung (legacy group2/default)."""
        template_file = os.path.join(self.prompts_dir, "gemini_prompt_template.txt")
        if os.path.exists(template_file):
            with open(template_file, 'r', encoding='utf-8') as f:
                prompt = f.read()
                print(f"✓ Đã load fallback prompt template từ {template_file}")
                return prompt
        print("⚠ Không tìm thấy prompt template, dùng prompt mặc định")
        return """Viết lại nội dung sau cho OKX Orbit:

{text}

Yêu cầu:
- Thêm emoji phù hợp
- Thêm 3-5 hashtags
- Độ dài 200-400 từ
- Xuống dòng sau mỗi Entry/Target/Stop Loss
"""

    def _load_variant_pool(self, variants_dir: str, fallback_path: str) -> List[Dict[str, str]]:
        """Đọc mọi .txt trong thư mục variant thành pool; rỗng thì dùng template legacy.

        Mỗi file là một prompt hoàn chỉnh chứa placeholder {text}.
        """
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
                    print(f"  ⚠ Bỏ qua {path}: thiếu placeholder {{text}}")
                    continue
                pool.append({'id': os.path.splitext(name)[0], 'template': content})
        if pool:
            return pool
        # Fallback legacy
        if os.path.exists(fallback_path):
            with open(fallback_path, 'r', encoding='utf-8') as f:
                return [{'id': 'legacy', 'template': f.read()}]
        return [{'id': 'default', 'template': self.prompt_template}]

    def _pick_variant(self, group: int = None, text: str = "", variant_id: Optional[str] = None) -> Tuple[str, str]:
        """Chọn 1 variant từ pool 10 prompt theo góc nhìn nội dung + tránh avoid_recent."""
        pool = self._variant_pool
        if not pool:
            return self.prompt_template, "none"
        if variant_id:
            for v in pool:
                if v['id'] == variant_id:
                    return v['template'], v['id']
        if len(pool) == 1:
            return pool[0]['template'], pool[0]['id']

        preferred_order = classify_content_angles(text)
        rank_map = {vid: idx for idx, vid in enumerate(preferred_order)}
        with self._rotation_lock:
            blocked = set(self._recent_variants)
            candidates = [v for v in pool if v['id'] not in blocked] or pool
            ranked = sorted(candidates, key=lambda v: rank_map.get(v['id'], 999))
            top_k = ranked[: min(3, len(ranked))]
            variant = random.choice(top_k)
            self._recent_variants.append(variant['id'])
        return variant['template'], variant['id']

    def _load_system_prompt(self) -> Optional[str]:
        """Load system prompt riêng biệt từ file"""
        system_prompt_file = os.path.join(self.prompts_dir, "gemini_system_prompt.txt")
        if os.path.exists(system_prompt_file):
            with open(system_prompt_file, 'r', encoding='utf-8') as f:
                system_prompt = f.read().strip()
                if system_prompt:
                    print(f"✓ Đã load system prompt từ {system_prompt_file}")
                    return system_prompt
        return None

    def _create_prompt(self, text: str, group: int = None, variant_id: Optional[str] = None) -> str:
        """Tạo prompt từ pool thống nhất theo góc nhìn nội dung và lưu prompt_id vào TLS."""
        forced_id = variant_id or getattr(self._tls, "forced_variant_id", None)
        template, picked_id = self._pick_variant(group=group, text=text, variant_id=forced_id)
        self._tls.last_prompt_id = picked_id
        print(f"  🎲 prompt variant [{picked_id}]")
        try:
            return template.format(text=text)
        except (KeyError, IndexError):
            return f"{template}\n\n{text}"

    def _clean_output(self, text: str) -> str:
        """
        Clean output text:
        - Phát hiện refusal leakage -> trả '[REJECT]'
        - Loại bỏ markdown (`*`, `` ` ``, heading `# `) và `#hashtag` thừa
        - Loại bỏ các dòng giới thiệu không mong muốn ở đầu
        """
        if not text:
            return text

        low = text.lower()
        if any(kw in low for kw in _REJECTION_KEYWORDS):
            return "[REJECT]"

        text = text.replace('*', '').replace('`', '')

        unwanted_prefixes = [
            "okay, here's", "here is the", "here's the",
            "sure!", "sure,", "i'll", "based on",
            "this is", "let me", "alright,",
            "dưới đây là", "sau đây là",
        ]

        lines = text.strip().split('\n')
        while lines:
            first_line_lower = lines[0].strip().lower()
            if any(first_line_lower.startswith(prefix) for prefix in unwanted_prefixes):
                lines.pop(0)
                while lines and not lines[0].strip():
                    lines.pop(0)
            else:
                break

        cleaned_lines = []
        for line in lines:
            line = re.sub(r"^\s*#{1,6}\s+", "", line)
            line = re.sub(r"(?:^|\s)#[A-Za-z0-9_]{2,30}\b", "", line)
            if line.strip():
                line = line.rstrip()
            cleaned_lines.append(line)

        return '\n'.join(cleaned_lines).strip()
    
    # ==================================================================
    # HELPER: Lấy generation config từ provider
    # ==================================================================
    
    def _get_generation_config(self, override: dict = None) -> dict:
        """Lấy generation config từ provider config hiện tại, cho phép override"""
        gen = self.provider_config.get('generation', {})
        config = {
            'temperature': gen.get('temperature', 0.65),
            'top_p': gen.get('top_p', 0.8),
            'top_k': gen.get('top_k', 40),
            'max_output_tokens': gen.get('max_output_tokens', 2048),
            'response_mime_type': 'text/plain'
        }
        if override:
            for k, v in override.items():
                if v is not None and v != "":
                    # Convert to appropriate types
                    if k in ['temperature', 'top_p']:
                        config[k] = float(v)
                    elif k in ['top_k', 'max_output_tokens']:
                        config[k] = int(v)
                    else:
                        config[k] = v
        return config
    
    def _get_request_timeout(self) -> int:
        """Lấy request timeout từ provider config hiện tại"""
        return self.provider_config.get('processing', {}).get('request_timeout', 60)
    
    def _get_retry_attempts(self) -> int:
        """Lấy retry attempts từ provider config hiện tại"""
        return self.provider_config.get('processing', {}).get('retry_attempts', 3)
    
    def _get_retry_delay(self) -> int:
        """Lấy retry delay từ provider config hiện tại"""
        return self.provider_config.get('processing', {}).get('retry_delay', 2)
    
    # ==================================================================
    # API CALL: GEMINI PROXY (OpenAI-compatible protocol)
    # ==================================================================
    
    def _try_gemini_proxy_call(
        self, 
        text: str,
        attempt: int = 1,
        group: int = None,
        gen_config_override: dict = None,
        model_override: str = None
    ) -> Optional[Tuple[str, str]]:
        """
        Gọi Gemini qua proxy trung gian (OpenAI-compatible).
        Dùng openai.OpenAI client với chat.completions.create.
        Gửi system_prompt (nếu có) + user_prompt đúng chuẩn OpenAI.
        """
        max_attempts = self._get_retry_attempts()
        request_timeout = self._get_request_timeout()
        gen_config = self._get_generation_config(gen_config_override)
        
        api_key = self.provider_config['api_key']
        endpoint = self.provider_config['endpoint']
        model_name = model_override or self.provider_config['model']
        
        for current_attempt in range(attempt, max_attempts + 1):
            try:
                prompt = self._create_prompt(text, group)
                
                # Cache OpenAI client (thread-safe)
                cache_key = f"proxy_{api_key[:10]}"
                with self._cache_lock:
                    if cache_key not in self._model_cache:
                        if len(self._model_cache) >= self._max_cache_size:
                            oldest_key = next(iter(self._model_cache))
                            del self._model_cache[oldest_key]
                        self._model_cache[cache_key] = OpenAI(
                            base_url=endpoint,
                            api_key=api_key,
                            timeout=request_timeout
                        )
                    client = self._model_cache[cache_key]
                
                # Build messages: system prompt + user prompt
                messages = []
                if self.system_prompt:
                    messages.append({"role": "system", "content": self.system_prompt})
                messages.append({"role": "user", "content": prompt})
                
                response_format = {"type": "json_object"} if gen_config.get('response_mime_type') == 'application/json' else None
                
                call_start = time.time()
                response = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=gen_config['temperature'],
                    top_p=gen_config['top_p'],
                    max_tokens=gen_config['max_output_tokens'],
                    response_format=response_format
                )
                call_duration = time.time() - call_start
                
                if call_duration > 30:
                    print(f"  ⚠ Slow API call: {call_duration:.1f}s for {model_name}")
                
                # Check finish_reason
                if response.choices and response.choices[0].finish_reason:
                    finish_reason = response.choices[0].finish_reason
                    with self._stats_lock:
                        self.stats['finish_reasons'][finish_reason] += 1
                        if finish_reason in ['length', 'content_filter']:
                            self.stats['truncated'] += 1
                
                # Extract text
                output_text = None
                if response.choices and response.choices[0].message:
                    output_text = response.choices[0].message.content
                
                if output_text:
                    return output_text, model_name
                else:
                    return None
                    
            except Exception as e:
                error_msg = str(e).lower()
                
                if 'timeout' in error_msg or 'deadline' in error_msg:
                    print(f"  ⏰ TIMEOUT ({request_timeout}s) for {model_name} "
                          f"(attempt {current_attempt}/{max_attempts})")
                    if current_attempt < max_attempts:
                        time.sleep(2)
                        continue
                    return None
                
                if 'quota' in error_msg or 'resource_exhausted' in error_msg:
                    print(f"  ⚠ Quota exhausted for proxy {model_name}")
                    return None
                
                elif 'rate' in error_msg or 'too many' in error_msg or '429' in str(e):
                    if current_attempt < max_attempts:
                        delay = 5 * current_attempt
                        print(f"  ⏳ Rate limited, waiting {delay}s... "
                              f"(attempt {current_attempt}/{max_attempts})")
                        time.sleep(delay)
                        continue
                    print(f"  ✗ Rate limit exhausted after {max_attempts} retries for {model_name}")
                    return None
                
                else:
                    if current_attempt < max_attempts:
                        delay = self._get_retry_delay()
                        print(f"  ⚠ Error: {str(e)[:100]} "
                              f"(attempt {current_attempt}/{max_attempts}), retry in {delay}s")
                        time.sleep(delay)
                        continue
                    else:
                        print(f"  ✗ Failed after {max_attempts} attempts: {str(e)[:100]}")
                        return None
        
        return None
    
    # ==================================================================
    # API CALL: GEMINI DIRECT (google.genai Client, trực tiếp Google)
    # ==================================================================
    
    def _try_gemini_direct_call(
        self, 
        text: str, 
        api_key: str,
        group: int = None,
        gen_config_override: dict = None,
        model_override: str = None
    ) -> Optional[Tuple[str, str]]:
        """
        Gọi Gemini trực tiếp qua Google API (google.genai Client).
        Giống cách gọi Gemma nhưng model khác.
        """
        max_attempts = self._get_retry_attempts()
        request_timeout = self._get_request_timeout()
        gen_config = self._get_generation_config(gen_config_override)
        model_name = model_override or self.provider_config['model']
        
        for current_attempt in range(1, max_attempts + 1):
            try:
                prompt = self._create_prompt(text, group)
                
                cache_key = f"direct_{api_key[:10]}"
                with self._cache_lock:
                    if cache_key not in self._model_cache:
                        if len(self._model_cache) >= self._max_cache_size:
                            oldest_key = next(iter(self._model_cache))
                            del self._model_cache[oldest_key]
                        self._model_cache[cache_key] = new_genai.Client(
                            api_key=api_key,
                            http_options={'timeout': request_timeout}
                        )
                    client = self._model_cache[cache_key]
                
                call_start = time.time()
                gemma_contents = (
                    f"{self.system_prompt}\n\n{prompt}"
                    if (self.system_prompt and "gemma" in model_name.lower())
                    else prompt
                )
                gemma_cfg_kwargs = {
                    'temperature': gen_config['temperature'],
                    'top_p': gen_config['top_p'],
                    'top_k': gen_config['top_k'],
                    'max_output_tokens': gen_config['max_output_tokens'],
                    'response_mime_type': gen_config.get('response_mime_type', 'text/plain'),
                }
                if self.system_prompt and "gemma" not in model_name.lower():
                    gemma_cfg_kwargs['system_instruction'] = self.system_prompt
                response = client.models.generate_content(
                    model=model_name,
                    contents=gemma_contents,
                    config=new_genai_types.GenerateContentConfig(**gemma_cfg_kwargs)
                )
                call_duration = time.time() - call_start
                
                if call_duration > 30:
                    print(f"  ⚠ Slow API call: {call_duration:.1f}s for {model_name}")
                
                if response.text:
                    return response.text, model_name
                else:
                    return None
                    
            except Exception as e:
                error_msg = str(e).lower()
                
                if 'timeout' in error_msg or 'deadline' in error_msg:
                    print(f"  ⏰ TIMEOUT for {model_name} (attempt {current_attempt}/{max_attempts})")
                    if current_attempt < max_attempts:
                        time.sleep(2)
                        continue
                    return None
                
                if 'quota' in error_msg or 'resource_exhausted' in error_msg:
                    return None
                
                if 'rate' in error_msg or 'too many' in error_msg or '429' in str(e):
                    return None  # Fail-fast cho parallel mode
                
                if current_attempt < max_attempts:
                    delay = self._get_retry_delay()
                    print(f"  ⚠ Error: {str(e)[:100]} "
                          f"(attempt {current_attempt}/{max_attempts}), retry in {delay}s")
                    time.sleep(delay)
                    continue
                else:
                    print(f"  ✗ Failed after {max_attempts} attempts: {str(e)[:100]}")
                    return None
        
        return None
    
    # ==================================================================
    # API CALL: GEMMA (Sliding Window, fail-fast 429) - GIỮ NGUYÊN
    # ==================================================================
    
    # Hằng số cho error types (Fail-Fast pattern)
    GEMMA_OK = 'ok'
    GEMMA_RATE_LIMITED = '429'
    GEMMA_QUOTA_ERROR = 'quota'
    GEMMA_TIMEOUT = 'timeout'
    GEMMA_OTHER_ERROR = 'error'
    
    def _try_gemma_call(
        self, 
        text: str, 
        api_key: str, 
        model_name: str,
        group: int = None,
        gen_config_override: dict = None
    ) -> Tuple[Optional[Tuple[str, str]], str]:
        """
        Gọi Gemma API - FAIL-FAST pattern.
        KHÔNG sleep khi 429/quota error.
        """
        max_attempts = self._get_retry_attempts()
        request_timeout = self._get_request_timeout()
        gen_config = self._get_generation_config(gen_config_override)
        
        for current_attempt in range(1, max_attempts + 1):
            try:
                prompt = self._create_prompt(text, group)
                cache_key = f"gemma_{api_key[:10]}"
                with self._cache_lock:
                    if cache_key not in self._model_cache:
                        if len(self._model_cache) >= self._max_cache_size:
                            oldest_key = next(iter(self._model_cache))
                            del self._model_cache[oldest_key]
                        self._model_cache[cache_key] = new_genai.Client(
                            api_key=api_key,
                            http_options={'timeout': request_timeout}
                        )
                    client = self._model_cache[cache_key]
                
                call_start = time.time()
                gemma_contents = (
                    f"{self.system_prompt}\n\n{prompt}"
                    if (self.system_prompt and "gemma" in model_name.lower())
                    else prompt
                )
                gemma_cfg_kwargs = {
                    'temperature': gen_config['temperature'],
                    'top_p': gen_config['top_p'],
                    'top_k': gen_config['top_k'],
                    'max_output_tokens': gen_config['max_output_tokens'],
                    'response_mime_type': gen_config.get('response_mime_type', 'text/plain'),
                }
                if self.system_prompt and "gemma" not in model_name.lower():
                    gemma_cfg_kwargs['system_instruction'] = self.system_prompt
                response = client.models.generate_content(
                    model=model_name,
                    contents=gemma_contents,
                    config=new_genai_types.GenerateContentConfig(**gemma_cfg_kwargs)
                )
                call_duration = time.time() - call_start
                
                if call_duration > 30:
                    print(f"  ⚠ Slow API call: {call_duration:.1f}s for {model_name}")
                
                if response.text:
                    return (response.text, model_name), self.GEMMA_OK
                else:
                    return None, self.GEMMA_OTHER_ERROR
                    
            except Exception as e:
                error_msg = str(e).lower()
                
                if 'rate' in error_msg or 'too many' in error_msg or '429' in str(e):
                    print(f"  ⚡ 429 Rate Limited on {api_key[:10]}... → FAIL-FAST (no sleep)")
                    return None, self.GEMMA_RATE_LIMITED
                
                if 'quota' in error_msg or 'resource_exhausted' in error_msg:
                    print(f"  ⚡ Quota Exhausted on {api_key[:10]}... → FAIL-FAST (no sleep)")
                    return None, self.GEMMA_QUOTA_ERROR
                
                if 'timeout' in error_msg or 'deadline' in error_msg:
                    print(f"  ⏰ TIMEOUT for {model_name} (attempt {current_attempt}/{max_attempts})")
                    if current_attempt < max_attempts:
                        time.sleep(2)
                        continue
                    return None, self.GEMMA_TIMEOUT
                
                if current_attempt < max_attempts:
                    delay = self._get_retry_delay()
                    print(f"  ⚠ Error: {str(e)[:100]} "
                          f"(attempt {current_attempt}/{max_attempts}), retry in {delay}s")
                    time.sleep(delay)
                    continue
                else:
                    print(f"  ✗ Failed after {max_attempts} attempts: {str(e)[:100]}")
                    return None, self.GEMMA_OTHER_ERROR
        
        return None, self.GEMMA_OTHER_ERROR
    
    # ==================================================================
    # PROCESS TEXT - Điều phối chính
    # ==================================================================
    
    @property
    def ready(self) -> bool:
        """Engine đã khởi tạo thành công (provider enabled + key hợp lệ) hay chưa."""
        return getattr(self, "active_provider", None) in ("gemma", "gemini_proxy", "gemini_direct")

    @staticmethod
    def _is_sensitive_content(text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        return any(re.search(p, low) for p in _SENSITIVE_PATTERNS)

    def process_text_with_meta(
        self,
        text: str,
        post_id: str = None,
        image_paths: List[str] = None,
        group: int = None,
        gen_config_override: dict = None,
        model_override: str = None,
        variant_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Xử lý text và trả dict {'text', 'model', 'prompt_id'} hoặc None."""
        if self._is_sensitive_content(text):
            if post_id:
                print(f"  ⛔ {post_id}: nội dung nhạy cảm -> [REJECT] (pre-screen)")
            return {"text": "[REJECT]", "model": "safety_filter", "prompt_id": "safety_filter"}

        if post_id:
            print(f"\n→ Xử lý post {post_id}...")

        self._tls.forced_variant_id = variant_id
        self._tls.last_prompt_id = variant_id or ""
        try:
            if self.active_provider == 'gemma':
                res = self._process_gemma(text, group=group, gen_config_override=gen_config_override, model_override=model_override)
            elif self.active_provider == 'gemini_proxy':
                res = self._process_gemini_proxy(text, group=group, gen_config_override=gen_config_override, model_override=model_override)
                if not res:
                    fb = self._get_gemma_fallback()
                    if fb is not None:
                        fb._tls.forced_variant_id = variant_id
                        fb._tls.last_prompt_id = getattr(self._tls, "last_prompt_id", "") or ""
                        try:
                            res = fb._process_gemma(text, group=group, gen_config_override=gen_config_override)
                            if res:
                                self._tls.last_prompt_id = getattr(fb._tls, "last_prompt_id", "") or ""
                        finally:
                            fb._tls.forced_variant_id = None
            elif self.active_provider == 'gemini_direct':
                res = self._process_gemini_direct(text, group=group, gen_config_override=gen_config_override, model_override=model_override)
            else:
                print(f"  ✗ Unknown provider: {self.active_provider}")
                return None

            if not res:
                return None
            out_text, used_model = res
            picked_prompt = getattr(self._tls, "last_prompt_id", "") or ""
            return {"text": out_text, "model": used_model, "prompt_id": picked_prompt}
        finally:
            self._tls.forced_variant_id = None

    def process_text(self, text: str, post_id: str = None, image_paths: List[str] = None, group: int = None, gen_config_override: dict = None, model_override: str = None, variant_id: Optional[str] = None) -> Optional[Tuple[str, str]]:
        """
        Xử lý text bằng provider đang active.
        Returns: (output_text, model_name) | ("[REJECT]", reason) | None
        """
        meta = self.process_text_with_meta(
            text,
            post_id=post_id,
            image_paths=image_paths,
            group=group,
            gen_config_override=gen_config_override,
            model_override=model_override,
            variant_id=variant_id,
        )
        if not meta:
            return None
        return meta["text"], meta["model"]
    
    # ==================================================================
    # PROCESS: GEMMA (giữ nguyên logic cũ)
    # ==================================================================
    
    def _process_gemma(self, text: str, group: int = None, gen_config_override: dict = None, model_override: str = None) -> Optional[Tuple[str, str]]:
        """Xử lý bằng Gemma - Sliding Window + Fail-Fast 429"""
        models = sorted(
            self.provider_config.get('models', []), 
            key=lambda x: x.get('priority', 99)
        )
        if model_override:
            models = [{'name': model_override, 'priority': 1}]
        
        if self.key_manager:
            # ========== PARALLEL MODE ==========
            max_key_attempts = self.key_manager.num_keys * 3
            
            for key_try in range(max_key_attempts):
                limiter, wait = self.key_manager.get_best_available_key()
                
                if wait > 0.0:
                    if wait > 1.0:
                        print(f"  ⏳ SlidingWindow: {limiter.key_display} "
                              f"chờ {wait:.1f}s (load={limiter.get_load()}/{limiter.rpm})")
                    time.sleep(wait)
                    limiter.total_wait_time += wait
                    retry_wait = limiter.acquire_slot()
                    if retry_wait > 0:
                        time.sleep(retry_wait)
                        limiter.total_wait_time += retry_wait
                
                if wait > 0.0:
                    limiter.record_request()
                
                for model_config in models:
                    model_name = model_config['name']
                    
                    result, error_type = self._try_gemma_call(
                        text, limiter.api_key, model_name, group=group, gen_config_override=gen_config_override
                    )
                    
                    if error_type == self.GEMMA_OK and result:
                        output_text, used_model = result
                        limiter.record_success()
                        with self._stats_lock:
                            self.stats['success'] += 1
                            self.stats['api_key_usage'][limiter.key_display] += 1
                            self.stats['model_usage'][model_name] += 1
                        print(f"  ✓ {limiter.key_display} → {model_name}")
                        return self._clean_output(output_text), used_model
                    
                    elif error_type in (self.GEMMA_RATE_LIMITED, self.GEMMA_QUOTA_ERROR):
                        self.key_manager.report_error_429(limiter)
                        limiter.record_failure()
                        print(f"  ⚡ {limiter.key_display} → penalty, "
                              f"switching key... ({key_try+1}/{max_key_attempts})")
                        break
                    
                    else:
                        limiter.record_failure()
                        print(f"  ✗ {limiter.key_display} → {model_name} failed, "
                              f"trying next model...")
                        continue
            
            with self._stats_lock:
                self.stats['failed'] += 1
            print(f"  ✗ All keys exhausted after {max_key_attempts} attempts")
            return None
        
        else:
            # ========== SEQUENTIAL MODE ==========
            valid_keys = [k for k in self.provider_config.get('api_keys', []) 
                          if k and 'YOUR_' not in k]
            if not valid_keys:
                self.stats['failed'] += 1
                return None
            
            start_index = self.tracker.get_next_key_index(len(valid_keys))
            
            for key_attempt in range(len(valid_keys)):
                key_index = (start_index + key_attempt) % len(valid_keys)
                api_key = valid_keys[key_index]
                key_display = f"key_{key_index + 1}"
                
                waited = self.tracker.wait_for_slot(api_key)
                if waited > 0.5:
                    print(f"  ⏳ Waited {waited:.1f}s for {key_display}")
                
                for model_config in models:
                    model_name = model_config['name']
                    
                    if not self.tracker.is_model_available(model_name, api_key):
                        continue
                    
                    result, error_type = self._try_gemma_call(text, api_key, model_name, group=group, gen_config_override=gen_config_override)
                    
                    if error_type == self.GEMMA_OK and result:
                        output_text, used_model = result
                        self.tracker.record_success(api_key, model_name)
                        self.stats['success'] += 1
                        self.stats['api_key_usage'][key_display] += 1
                        self.stats['model_usage'][model_name] += 1
                        print(f"  ✓ {key_display} → {model_name}")
                        return self._clean_output(output_text), used_model
                    else:
                        self.tracker.mark_model_exhausted(model_name, api_key)
                
                if key_attempt < len(valid_keys) - 1:
                    print(f"  ⚠ {key_display} failed all models, trying next key...")
            
            self.stats['failed'] += 1
            print(f"  ✗ All {len(valid_keys)} keys exhausted")
            return None
    
    # ==================================================================
    # PROCESS: GEMINI PROXY (single key, proxy endpoint)
    # ==================================================================
    
    def _process_gemini_proxy(self, text: str, group: int = None, gen_config_override: dict = None, model_override: str = None) -> Optional[Tuple[str, str]]:
        """Xử lý bằng Gemini Proxy - single key qua proxy trung gian"""
        api_key = self.provider_config['api_key']
        model_name = model_override or self.provider_config['model']
        
        # Wait for rate limit slot
        waited = self.tracker.wait_for_slot(api_key)
        if waited > 0.5:
            print(f"  ⏳ Proxy waited {waited:.1f}s")
        
        if not self.tracker.is_model_available(model_name, api_key):
            print(f"  ⚠ Proxy model {model_name} in cooldown")
            with self._stats_lock:
                self.stats['failed'] += 1
            return None
        
        result = self._try_gemini_proxy_call(text, group=group, gen_config_override=gen_config_override, model_override=model_override)
        
        if result:
            output_text, used_model = result
            self.tracker.record_success(api_key, model_name)
            with self._stats_lock:
                self.stats['success'] += 1
                self.stats['api_key_usage']['proxy'] += 1
                self.stats['model_usage'][model_name] += 1
            print(f"  ✓ proxy → {model_name}")
            return self._clean_output(output_text), used_model
        else:
            self.tracker.mark_model_exhausted(model_name, api_key)
            with self._stats_lock:
                self.stats['failed'] += 1
            return None
    
    # ==================================================================
    # PROCESS: GEMINI DIRECT (multi-key, trực tiếp Google)
    # ==================================================================
    
    def _process_gemini_direct(self, text: str, group: int = None, gen_config_override: dict = None, model_override: str = None) -> Optional[Tuple[str, str]]:
        """Xử lý bằng Gemini Direct - gọi trực tiếp Google API"""
        model_name = model_override or self.provider_config['model']
        
        if self.key_manager:
            # ========== PARALLEL MODE (multi-key) ==========
            max_key_attempts = self.key_manager.num_keys * 3
            
            for key_try in range(max_key_attempts):
                limiter, wait = self.key_manager.get_best_available_key()
                
                if wait > 0.0:
                    if wait > 1.0:
                        print(f"  ⏳ Direct: {limiter.key_display} "
                              f"chờ {wait:.1f}s (load={limiter.get_load()}/{limiter.rpm})")
                    time.sleep(wait)
                    limiter.total_wait_time += wait
                    retry_wait = limiter.acquire_slot()
                    if retry_wait > 0:
                        time.sleep(retry_wait)
                        limiter.total_wait_time += retry_wait
                
                if wait > 0.0:
                    limiter.record_request()
                
                result = self._try_gemini_direct_call(text, limiter.api_key, group=group, gen_config_override=gen_config_override, model_override=model_override)
                
                if result:
                    output_text, used_model = result
                    limiter.record_success()
                    with self._stats_lock:
                        self.stats['success'] += 1
                        self.stats['api_key_usage'][limiter.key_display] += 1
                        self.stats['model_usage'][model_name] += 1
                    print(f"  ✓ {limiter.key_display} → {model_name}")
                    return self._clean_output(output_text), used_model
                else:
                    self.key_manager.report_error_429(limiter)
                    limiter.record_failure()
                    print(f"  ⚡ {limiter.key_display} → penalty, "
                          f"switching key... ({key_try+1}/{max_key_attempts})")
            
            with self._stats_lock:
                self.stats['failed'] += 1
            print(f"  ✗ All direct keys exhausted after {max_key_attempts} attempts")
            return None
        
        else:
            # ========== SEQUENTIAL MODE ==========
            valid_keys = [k for k in self.provider_config.get('api_keys', []) 
                          if k and 'YOUR_' not in k]
            if not valid_keys:
                self.stats['failed'] += 1
                return None
            
            start_index = self.tracker.get_next_key_index(len(valid_keys))
            
            for key_attempt in range(len(valid_keys)):
                key_index = (start_index + key_attempt) % len(valid_keys)
                api_key = valid_keys[key_index]
                key_display = f"key_{key_index + 1}"
                
                waited = self.tracker.wait_for_slot(api_key)
                if waited > 0.5:
                    print(f"  ⏳ Waited {waited:.1f}s for {key_display}")
                
                if not self.tracker.is_model_available(model_name, api_key):
                    continue
                
                result = self._try_gemini_direct_call(text, api_key, group=group, gen_config_override=gen_config_override, model_override=model_override)
                
                if result:
                    output_text, used_model = result
                    self.tracker.record_success(api_key, model_name)
                    self.stats['success'] += 1
                    self.stats['api_key_usage'][key_display] += 1
                    self.stats['model_usage'][model_name] += 1
                    print(f"  ✓ {key_display} → {model_name}")
                    return self._clean_output(output_text), used_model
                else:
                    self.tracker.mark_model_exhausted(model_name, api_key)
                
                if key_attempt < len(valid_keys) - 1:
                    print(f"  ⚠ {key_display} failed, trying next key...")
            
            self.stats['failed'] += 1
            print(f"  ✗ All {len(valid_keys)} direct keys exhausted")
            return None
    
    # ==================================================================
    # STATS & CLEANUP
    # ==================================================================
    
    def _cleanup_stats(self):
        """Clear accumulated stats periodically"""
        if self.stats['total_processed'] > 0 and self.stats['total_processed'] % 100 == 0:
            self.stats['api_key_usage'].clear()
            self.stats['model_usage'].clear()
            self.stats['finish_reasons'].clear()
            
            if self.key_manager:
                self.key_manager.reset_all_penalties()
            elif self.tracker:
                self.tracker.total_calls.clear()
                self.tracker.model_calls.clear()
            
            gc.collect()
            print(f"    🧹 Stats cleanup completed (total processed: {self.stats['total_processed']})")
    
    def print_stats(self):
        """In thống kê"""
        print("\n" + "=" * 60)
        print(f"THỐNG KÊ XỬ LÝ [{self.active_provider.upper()}]")
        print("=" * 60)
        print(f"Provider: {self.active_provider}")
        print(f"Tổng số posts: {self.stats['total_processed']}")
        print(f"✓ Thành công: {self.stats['success']}")
        print(f"✗ Thất bại: {self.stats['failed']}")
        
        if self.stats['truncated'] > 0:
            print(f"⚠️ Bị cắt/chặn: {self.stats['truncated']}")
        
        if self.stats['model_usage']:
            print("\nModel usage:")
            for model, count in sorted(self.stats['model_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {model}: {count}")
        
        if self.stats['api_key_usage']:
            print("\nAPI key usage:")
            for key, count in sorted(self.stats['api_key_usage'].items(), key=lambda x: x[1], reverse=True):
                print(f"  {key}: {count}")
        
        # Mode-specific stats
        if self.key_manager:
            label = "GEMMA" if self.active_provider == 'gemma' else "GEMINI DIRECT"
            print(f"\n[OK] {label} Sliding Window Stats ({self.key_manager.num_keys} keys):")
            self.key_manager.print_status()
        elif self.tracker:
            tracker_stats = self.tracker.get_stats()
            if tracker_stats['cooling_models']:
                print(f"\n⏳ Models đang cooldown: {', '.join(tracker_stats['cooling_models'])}")
        
        print("=" * 60)


if __name__ == "__main__":
    # Test
    processor = GeminiProcessor()
    
    test_text = """
    Tăng Trưởng Altcoin – Động Lực Xanh Mạnh Mẽ Trên Các Đồng Coin Trung Bình
    Cài Đặt Giao Dịch: Không có tín hiệu vào cụ thể – giai đoạn quan sát.
    Điểm Nổi Bật:
    💎 $1000SATS — Giao dịch tại 0.00002510, tăng +3.16%.
    """
    
    result = processor.process_text(test_text, "test_001")
    
    if result:
        output, model = result
        print(f"\n{'='*60}")
        print(f"Kết quả từ {model}:")
        print(f"{'='*60}")
        print(output)
    
    processor.print_stats()

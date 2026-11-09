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
import re
import time
import os
import gc
from datetime import datetime
from threading import Lock
import threading
from collections import defaultdict
from typing import Optional, Dict, List, Tuple, Set
import random

# Sliding Window Rate Limiter cho Gemma
from utils.rate_limiter import SlidingWindowRateLimiter, ParallelKeyManager, redact_key


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
        Bảo vệ thread-safe bằng cách giữ chỗ (reserve) timestamp tương lai ngay trong lock.
        """
        key_short = api_key[:10]
        wait_time = 0.0
        
        with self.lock:
            now = time.time()
            last_time = self.last_request_time.get(key_short, 0.0)
            reference_time = max(now, last_time)
            
            if reference_time == last_time:
                # Thread khác đang giữ chỗ ở tương lai, bắt đầu từ mốc đó
                target_time = last_time + self.min_delay_seconds
            else:
                elapsed = now - last_time
                if elapsed < self.min_delay_seconds:
                    target_time = last_time + self.min_delay_seconds
                else:
                    target_time = now
            
            if target_time > now:
                wait_time = target_time - now
                
            self.last_request_time[key_short] = target_time
            
        if wait_time > 0.0:
            time.sleep(wait_time)
            
        return wait_time
    
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
                print(f"  ⚠ Key {redact_key(api_key)} exhausted for {model_name}, cooldown {self.cooldown_seconds}s")
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
    
    def __init__(self, config_path: str = "config/gemini.yaml"):
        """Khởi tạo processor dựa trên active_provider trong config"""
        self.config = self._load_config(config_path)
        
        # Xác định provider đang active
        self.active_provider = self.config.get('active_provider', 'gemma').lower()
        
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
        
        # Thread-local storage cho prompt override (thread-safe generate_reply)
        self._thread_local = threading.local()

        # Load prompt template và system prompt
        self._prompt_template = self._load_prompt_template()
        self._system_prompt = self._load_system_prompt()
        
        # Cache reply prompt template (load 1 lần, dùng mãi)
        self._reply_prompt_template = None
        self._reply_prompt_lock = Lock()
        
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

    @property
    def system_prompt(self) -> Optional[str]:
        if hasattr(self, '_thread_local'):
            val = getattr(self._thread_local, 'system_prompt_override', None)
            if val is not None:
                return val
        return getattr(self, '_system_prompt', None)

    @system_prompt.setter
    def system_prompt(self, value: Optional[str]):
        if hasattr(self, '_thread_local'):
            self._thread_local.system_prompt_override = value
        else:
            self._system_prompt = value

    @property
    def prompt_template(self) -> str:
        if hasattr(self, '_thread_local'):
            val = getattr(self._thread_local, 'prompt_template_override', None)
            if val is not None:
                return val
        return getattr(self, '_prompt_template', '')

    @prompt_template.setter
    def prompt_template(self, value: str):
        if hasattr(self, '_thread_local'):
            self._thread_local.prompt_template_override = value
        else:
            self._prompt_template = value
    
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
        
        print(f"[OK] GEMINI PROXY Mode:")
        print(f"  → Endpoint: {endpoint}")
        print(f"  → Model: {model}")
        print(f"  → Key: {redact_key(api_key)}")
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
        """Load prompt template từ file"""
        template_file = "config/gemini_prompt_template.txt"
        if os.path.exists(template_file):
            with open(template_file, 'r', encoding='utf-8') as f:
                prompt = f.read()
                print(f"✓ Đã load prompt template từ {template_file}")
                return prompt
        
        # Fallback
        print("⚠ Không tìm thấy prompt template, dùng prompt mặc định")
        return """Viết lại nội dung sau cho Binance Square:

{text}

Yêu cầu:
- Thêm emoji phù hợp
- Thêm 3-5 hashtags
- Độ dài 200-400 từ
- Xuống dòng sau mỗi Entry/Target/Stop Loss
"""
    
    def _load_system_prompt(self) -> Optional[str]:
        """Load system prompt riêng biệt từ file"""
        system_prompt_file = "config/gemini_system_prompt.txt"
        if os.path.exists(system_prompt_file):
            with open(system_prompt_file, 'r', encoding='utf-8') as f:
                system_prompt = f.read().strip()
                print(f"✓ Đã load system prompt từ {system_prompt_file}")
                return system_prompt
        return None
    
    def _create_prompt(self, text: str) -> str:
        """Tạo prompt từ template. Hỗ trợ thread-local override cho generate_reply."""
        # Kiểm tra thread-local override (dùng cho generate_reply thread-safe)
        override = getattr(self._thread_local, 'prompt_override', None)
        if override is not None:
            return override  # Trả về prompt đã format sẵn, không cần template
        return self.prompt_template.format(text=text)
    
    def _clean_output(self, text: str) -> str:
        """
        Clean output text:
        - Phát hiện và gắn nhãn [REJECT] cho các bài viết bị từ chối/chính trị
        - Loại bỏ tất cả ký tự * (markdown bold/italic)
        - Loại bỏ các dòng giới thiệu không mong muốn ở đầu
        - Đảm bảo mỗi dòng kết thúc bằng dấu cách
        """
        if not text:
            return text
            
        # Kiểm tra nội dung chính trị / bị từ chối từ bộ lọc an toàn của Gemini
        rejection_keywords = [
            "từ chối do", "vi phạm chính sách", "vi phạm điều khoản",
            "bộ lọc nội dung", "bộ lọc thông tin", "yêu cầu bị từ chối",
            "yêu cầu này bị từ chối", "chứa thông tin liên quan đến chính trị",
            "địa chính trị", "xung đột quân sự", "chính trị, chính phủ",
            "I cannot fulfill", "violates the safety", "safety guidelines",
            "as an AI", "cannot generate content"
        ]
        text_lower = text.lower()
        if any(kw in text_lower for kw in rejection_keywords):
            return "[REJECT]"
        
        text = text.replace('*', '')
        
        unwanted_prefixes = [
            "okay, here's", "here is the", "here's the",
            "sure!", "sure,", "i'll", "based on",
            "this is", "let me", "alright,",
        ]
        
        lines = text.strip().split('\n')
        while lines:
            first_line_lower = lines[0].strip().lower()
            should_remove = False
            for prefix in unwanted_prefixes:
                if first_line_lower.startswith(prefix):
                    should_remove = True
                    break
            if should_remove:
                lines.pop(0)
                while lines and not lines[0].strip():
                    lines.pop(0)
            else:
                break
        
        cleaned_lines = []
        for line in lines:
            if line.strip():
                line = line.rstrip() + ' '
            cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines).lstrip()
    
    # ==================================================================
    # HELPER: Lấy generation config từ provider
    # ==================================================================
    
    def _get_generation_config(self) -> dict:
        """Lấy generation config từ provider config hiện tại"""
        gen = self.provider_config.get('generation', {})
        return {
            'temperature': gen.get('temperature', 0.65),
            'top_p': gen.get('top_p', 0.8),
            'top_k': gen.get('top_k', 40),
            'max_output_tokens': gen.get('max_output_tokens', 2048),
        }
    
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
        attempt: int = 1
    ) -> Optional[Tuple[str, str]]:
        """
        Gọi Gemini qua proxy trung gian (OpenAI-compatible).
        Dùng openai.OpenAI client với chat.completions.create.
        Gửi system_prompt (nếu có) + user_prompt đúng chuẩn OpenAI.
        """
        max_attempts = self._get_retry_attempts()
        request_timeout = self._get_request_timeout()
        gen_config = self._get_generation_config()
        
        api_key = self.provider_config['api_key']
        endpoint = self.provider_config['endpoint']
        model_name = self.provider_config['model']
        
        for current_attempt in range(attempt, max_attempts + 1):
            try:
                prompt = self._create_prompt(text)
                
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
                
                call_start = time.time()
                response = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=gen_config['temperature'],
                    top_p=gen_config['top_p'],
                    max_tokens=gen_config['max_output_tokens'],
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
    ) -> Optional[Tuple[str, str]]:
        """
        Gọi Gemini trực tiếp qua Google API (google.genai Client).
        Giống cách gọi Gemma nhưng model khác.
        """
        max_attempts = self._get_retry_attempts()
        request_timeout = self._get_request_timeout()
        gen_config = self._get_generation_config()
        model_name = self.provider_config['model']
        
        for current_attempt in range(1, max_attempts + 1):
            try:
                prompt = self._create_prompt(text)
                
                cache_key = f"direct_{api_key[:10]}"
                with self._cache_lock:
                    if cache_key not in self._model_cache:
                        if len(self._model_cache) >= self._max_cache_size:
                            oldest_key = next(iter(self._model_cache))
                            del self._model_cache[oldest_key]
                        self._model_cache[cache_key] = new_genai.Client(
                            api_key=api_key,
                            http_options={'timeout': request_timeout * 1000}
                        )
                    client = self._model_cache[cache_key]
                
                call_start = time.time()
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=new_genai_types.GenerateContentConfig(
                        temperature=gen_config['temperature'],
                        top_p=gen_config['top_p'],
                        top_k=gen_config['top_k'],
                        max_output_tokens=gen_config['max_output_tokens'],
                        system_instruction=self.system_prompt if self.system_prompt else None,
                    )
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
    GEMMA_HARD_FAILURE = 'hard_failure'  # 401/403/invalid key → auto-disable
    
    def _try_gemma_call(
        self, 
        text: str, 
        api_key: str, 
        model_name: str,
    ) -> Tuple[Optional[Tuple[str, str]], str]:
        """
        Gọi Gemma API - FAIL-FAST pattern.
        KHÔNG sleep khi 429/quota error.
        """
        max_attempts = self._get_retry_attempts()
        request_timeout = self._get_request_timeout()
        gen_config = self._get_generation_config()
        
        for current_attempt in range(1, max_attempts + 1):
            try:
                prompt = self._create_prompt(text)
                if self.system_prompt:
                    prompt = f"{self.system_prompt}\n\n{prompt}"
                    
                cache_key = f"gemma_{api_key[:10]}"
                with self._cache_lock:
                    if cache_key not in self._model_cache:
                        if len(self._model_cache) >= self._max_cache_size:
                            oldest_key = next(iter(self._model_cache))
                            del self._model_cache[oldest_key]
                        self._model_cache[cache_key] = new_genai.Client(
                            api_key=api_key,
                            http_options={'timeout': request_timeout * 1000}
                        )
                    client = self._model_cache[cache_key]
                
                call_start = time.time()
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=new_genai_types.GenerateContentConfig(
                        temperature=gen_config['temperature'],
                        top_p=gen_config['top_p'],
                        top_k=gen_config['top_k'],
                        max_output_tokens=gen_config['max_output_tokens'],
                    )
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
                error_str = str(e)
                
                # === Hard Failures: 401/403/invalid key → auto-disable ===
                if ('403' in error_str or 'permission' in error_msg or 
                    '401' in error_str or 'invalid' in error_msg and 'api_key' in error_msg or
                    'api key not valid' in error_msg or 'api_key_invalid' in error_msg):
                    print(f"  🚫 HARD FAILURE on {redact_key(api_key)} → {error_str[:100]}")
                    return None, self.GEMMA_HARD_FAILURE
                
                # === Soft Failures: 429/quota → penalty but key still valid ===
                if 'rate' in error_msg or 'too many' in error_msg or '429' in error_str:
                    print(f"  ⚡ 429 Rate Limited on {redact_key(api_key)} → FAIL-FAST (no sleep)")
                    return None, self.GEMMA_RATE_LIMITED
                
                if 'quota' in error_msg or 'resource_exhausted' in error_msg:
                    print(f"  ⚡ Quota Exhausted on {redact_key(api_key)} → FAIL-FAST (no sleep)")
                    return None, self.GEMMA_QUOTA_ERROR
                
                if 'timeout' in error_msg or 'deadline' in error_msg:
                    print(f"  ⏰ TIMEOUT for {model_name} (attempt {current_attempt}/{max_attempts})")
                    if current_attempt < max_attempts:
                        time.sleep(2)
                        continue
                    return None, self.GEMMA_TIMEOUT
                
                if current_attempt < max_attempts:
                    delay = self._get_retry_delay()
                    print(f"  ⚠ Error: {error_str[:100]} "
                          f"(attempt {current_attempt}/{max_attempts}), retry in {delay}s")
                    time.sleep(delay)
                    continue
                else:
                    print(f"  ✗ Failed after {max_attempts} attempts: {error_str[:100]}")
                    return None, self.GEMMA_OTHER_ERROR
        
        return None, self.GEMMA_OTHER_ERROR
    
    # ==================================================================
    # PROCESS TEXT - Điều phối chính
    # ==================================================================
    
    def _is_sensitive_content(self, text: str) -> bool:
        """
        Kiểm tra nội dung có vi phạm bộ quy chuẩn an toàn hay không (pre-screen).
        """
        if not text:
            return False
        text_lower = text.lower()
        patterns = [
            # 1. Chính trị, địa chính trị, chính phủ, lãnh đạo, chiến tranh
            r'\btrump\b', r'\bbiden\b', r'\bharris\b', r'\bputin\b', r'\bzelensky\b', r'\bnetanyahu\b', r'\bxi jinping\b',
            r'\bisrael\b', r'\biran\b', r'\bgaza\b', r'\bpalestine\b', r'\bhamas\b', r'\bhezbollah\b', r'\bukraine\b', r'\brussia\b', r'\btaiwan\b',
            r'\bpresident\b', r'\bprime minister\b', r'\bwhite house\b', r'\bkremlin\b', r'\belection\b', r'\belections\b',
            r'\bparliament\b', r'\bcongress\b', r'\bsenate\b', r'\bclarity act\b', r'\bgeopolitical\b', r'\bgeopolitics\b',
            r'\bcentral bank\b', r'\bcentral banks\b', r'\bgold reserves\b', r'\bfed\b',
            # Tiếng Việt chính trị
            r'\btổng thống\b', r'\bbầu cử\b', r'\bchính phủ\b', r'\bnghị viện\b', r'\bquốc hội\b', r'\bchính trị\b', r'\bđịa chính trị\b',
            r'\bchiến tranh\b', r'\bxung đột\b', r'\bngân hàng trung ương\b', r'\bdự trữ vàng\b', r'\btrung quốc\b', r'\bnước mỹ\b', r'\bhoa kỳ\b',
            
            # 2. Nội dung khiêu dâm, bạo lực, tự hại
            r'\bporn\b', r'\bpornography\b', r'\bsexual\b', r'\bsuicide\b', r'\bself-harm\b',
            r'\bkhiêu dâm\b', r'\bbạo lực\b', r'\btự hại\b',
            
            # 3. Hoạt động bất hợp pháp, MLM, Pump & Dump
            r'\bmoney laundering\b', r'\bpump\s*&\s*dump\b', r'\bpump\s*and\s*dump\b',
            r'\brửa tiền\b', r'\bđa cấp\b', r'\bbơm xả\b', r'\blừa đảo\b',
            
            # 4. ICO/IDO/IEO, Airdrop, Giveaway
            r'\bico\b', r'\bido\b', r'\bieo\b', r'\bairdrop\b', r'\bgiveaway\b',
            r'\bhuy động vốn\b'
        ]
        for pattern in patterns:
            if re.search(pattern, text_lower):
                return True
        return False

    def process_text(self, text: str, post_id: str = None, image_paths: List[str] = None, is_raw_text: bool = True) -> Optional[Tuple[str, str]]:
        """
        Xử lý text bằng provider đang active.
        Returns: (output_text, model_name) hoặc None
        """
        if post_id:
            print(f"\n→ Xử lý post {post_id}...")
            
        if is_raw_text and self._is_sensitive_content(text):
            print(f"  [🚫] Phát hiện nội dung nhạy cảm/chính trị qua bộ lọc pre-screen!")
            return "[REJECT]", "safety_filter"
        
        with self._stats_lock:
            self.stats['total_processed'] += 1
            self._cleanup_stats()
        
        if self.active_provider == 'gemma':
            return self._process_gemma(text)
        elif self.active_provider == 'gemini_proxy':
            return self._process_gemini_proxy(text)
        elif self.active_provider == 'gemini_direct':
            return self._process_gemini_direct(text)
        else:
            print(f"  ✗ Unknown provider: {self.active_provider}")
            return None
            
    def generate_reply(self, post_content: str, commenter_info: str, comment_text: str) -> Optional[str]:
        """
        Tạo nội dung reply dùng template đặc biệt.
        THREAD-SAFE: Dùng thread-local override thay vì mutate shared state.
        Có thể gọi đồng thời từ nhiều farming worker threads.
        """
        # Load và cache reply prompt template (thread-safe, chỉ load từ disk 1 lần)
        if self._reply_prompt_template is None:
            with self._reply_prompt_lock:
                if self._reply_prompt_template is None:
                    try:
                        with open("config/farming_reply_prompt.txt", "r", encoding="utf-8") as f:
                            self._reply_prompt_template = f.read()
                    except Exception:
                        return None
        
        if self._reply_prompt_template is None:
            return None
            
        # Format prompt hoàn chỉnh
        full_prompt = self._reply_prompt_template.format(
            post_content=post_content,
            commenter_info=commenter_info,
            comment_text=comment_text
        )
        
        # Dùng thread-local override để bypass _create_prompt template
        # Mỗi thread có biến riêng → không ảnh hưởng thread khác
        self._thread_local.prompt_override = full_prompt
        try:
            res = self.process_text(full_prompt)
            if res:
                return res[0]
            return None
        finally:
            # Xóa override sau khi dùng xong
            self._thread_local.prompt_override = None

    # ==================================================================
    # PROCESS: GEMMA (giữ nguyên logic cũ)
    # ==================================================================
    
    def _process_gemma(self, text: str) -> Optional[Tuple[str, str]]:
        """
        Xử lý bằng Gemma — Production-Grade Model Cascade + Fail-Fast 429.

        Kiến trúc:
        ┌─────────────────────────────────────────────────────────────────┐
        │  TIER 1: Primary Model (gemma-4-31b-it)                       │
        │  → Quét TẤT CẢ keys với Sliding Window load-balancing         │
        │  → Key bị 429 → penalty CHỈ cho model này, thử key khác      │
        │                                                                 │
        │  Nếu TẤT CẢ keys bị penalty cho primary model:                │
        │                                                                 │
        │  TIER 2: Fallback Model (gemma-3-27b-it)                      │
        │  → RESET penalty tracking (model khác = quota riêng)          │
        │  → Quét TẤT CẢ keys lại từ đầu với fallback model            │
        │                                                                 │
        │  Nếu TẤT CẢ keys + TẤT CẢ models đều exhausted:              │
        │  → Đợi all_keys_exhausted_wait rồi thử lại 1 lần cuối       │
        └─────────────────────────────────────────────────────────────────┘
        """
        models = sorted(
            self.provider_config.get('models', []), 
            key=lambda x: x.get('priority', 99)
        )
        
        rate_config = self.provider_config.get('rate_limiting', {})
        model_fallback_enabled = rate_config.get('model_fallback_enabled', True)
        all_keys_wait = rate_config.get('all_keys_exhausted_wait', 15)

        if self.key_manager:
            # ========== PARALLEL MODE: Model Cascade ==========
            
            for model_idx, model_config in enumerate(models):
                model_name = model_config['name']
                is_fallback = model_idx > 0
                
                if is_fallback and not model_fallback_enabled:
                    break
                
                if is_fallback:
                    print(f"  🔄 FALLBACK → {model_name} (all keys exhausted for previous models)")
                
                # Mỗi model được thử tối đa num_keys * 2 lần
                # (đủ để quét hết keys + retry sau penalty ngắn)
                max_attempts = self.key_manager.num_keys * 2
                
                for key_try in range(max_attempts):
                    limiter, wait = self.key_manager.get_best_available_key()
                    
                    while wait > 0.0:
                        if wait > 1.0:
                            print(f"  ⏳ SlidingWindow: {limiter.key_display} "
                                  f"chờ {wait:.1f}s (load={limiter.get_load()}/{limiter.rpm})")
                        time.sleep(wait)
                        limiter.total_wait_time += wait
                        wait = limiter.acquire_slot()
                    
                    result, error_type = self._try_gemma_call(
                        text, limiter.api_key, model_name
                    )
                    
                    if error_type == self.GEMMA_OK and result:
                        output_text, used_model = result
                        limiter.record_success()
                        with self._stats_lock:
                            self.stats['success'] += 1
                            self.stats['api_key_usage'][limiter.key_display] += 1
                            self.stats['model_usage'][model_name] += 1
                        tier_label = "fallback" if is_fallback else "primary"
                        print(f"  ✓ {limiter.key_display} → {model_name} ({tier_label})")
                        return self._clean_output(output_text), used_model
                    
                    elif error_type == self.GEMMA_HARD_FAILURE:
                        # Hard failure (401/403) → report để auto-disable key
                        self.key_manager.report_hard_failure(limiter)
                        limiter.record_failure()
                        print(f"  🚫 {limiter.key_display} ({redact_key(limiter.api_key)}) → hard failure, switching key...")
                        continue
                    
                    elif error_type in (self.GEMMA_RATE_LIMITED, self.GEMMA_QUOTA_ERROR):
                        self.key_manager.report_error_429(limiter)
                        limiter.record_failure()
                        
                        # Kiểm tra: TẤT CẢ keys đang bị penalty không?
                        all_penalized = all(l.is_penalized() for l in self.key_manager.limiters)
                        
                        if all_penalized:
                            if model_idx < len(models) - 1 and model_fallback_enabled:
                                # Còn fallback model → break sang model tiếp theo
                                print(f"  ⚡ ALL {self.key_manager.num_keys} keys penalty "
                                      f"for {model_name} → trying next model...")
                                # Reset ALL penalties khi chuyển model
                                # (model khác có quota riêng, penalty cũ không áp dụng)
                                self.key_manager.reset_all_penalties()
                                break
                            else:
                                # Hết model fallback → đợi rồi thử 1 lần cuối
                                print(f"  ⚡ ALL keys + ALL models exhausted → "
                                      f"waiting {all_keys_wait}s for recovery...")
                                time.sleep(all_keys_wait)
                                # Reset penalties và thử lại loop
                                self.key_manager.reset_all_penalties()
                                continue
                        else:
                            print(f"  ⚡ {limiter.key_display} → penalty for {model_name}, "
                                  f"switching key... ({key_try+1}/{max_attempts})")
                            continue
                    
                    else:
                        # TIMEOUT / OTHER_ERROR → thử key khác nhưng KHÔNG penalty
                        limiter.record_failure()
                        print(f"  ✗ {limiter.key_display} → {model_name} failed, "
                              f"trying next key...")
                        continue
                else:
                    # for-else: hết max_attempts cho model này mà không break
                    # → chuyển sang fallback model (nếu có)
                    continue
            
            # Nếu chạy hết tất cả models mà vẫn fail
            with self._stats_lock:
                self.stats['failed'] += 1
            model_names = [m['name'] for m in models]
            print(f"  ✗ All keys exhausted across all models: {model_names}")
            return None
        
        else:
            # ========== SEQUENTIAL MODE (giữ nguyên + model cascade) ==========
            valid_keys = [k for k in self.provider_config.get('api_keys', []) 
                          if k and 'YOUR_' not in k]
            if not valid_keys:
                self.stats['failed'] += 1
                return None
            
            for model_config in models:
                model_name = model_config['name']
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
                    
                    result, error_type = self._try_gemma_call(text, api_key, model_name)
                    
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
                
            self.stats['failed'] += 1
            print(f"  ✗ All keys exhausted across all models (sequential)")
            return None
    
    # ==================================================================
    # PROCESS: GEMINI PROXY (single key, proxy endpoint)
    # ==================================================================
    
    def _process_gemini_proxy(self, text: str) -> Optional[Tuple[str, str]]:
        """Xử lý bằng Gemini Proxy - single key qua proxy trung gian"""
        api_key = self.provider_config['api_key']
        model_name = self.provider_config['model']
        
        # Wait for rate limit slot
        waited = self.tracker.wait_for_slot(api_key)
        if waited > 0.5:
            print(f"  ⏳ Proxy waited {waited:.1f}s")
        
        if not self.tracker.is_model_available(model_name, api_key):
            print(f"  ⚠ Proxy model {model_name} in cooldown")
            with self._stats_lock:
                self.stats['failed'] += 1
            return None
        
        result = self._try_gemini_proxy_call(text)
        
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
    
    def _process_gemini_direct(self, text: str) -> Optional[Tuple[str, str]]:
        """Xử lý bằng Gemini Direct - gọi trực tiếp Google API"""
        model_name = self.provider_config['model']
        
        if self.key_manager:
            # ========== PARALLEL MODE (multi-key) ==========
            max_key_attempts = self.key_manager.num_keys * 3
            
            for key_try in range(max_key_attempts):
                limiter, wait = self.key_manager.get_best_available_key()
                
                while wait > 0.0:
                    if wait > 1.0:
                        print(f"  ⏳ Direct: {limiter.key_display} "
                              f"chờ {wait:.1f}s (load={limiter.get_load()}/{limiter.rpm})")
                    time.sleep(wait)
                    limiter.total_wait_time += wait
                    wait = limiter.acquire_slot()
                
                result = self._try_gemini_direct_call(text, limiter.api_key)
                
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
                
                result = self._try_gemini_direct_call(text, api_key)
                
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

"""Reads and manages configuration from config/gemini_config.yaml and config/profiles_config.yaml."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

import yaml


def mask_secret(value: str) -> str:
    """Mask an API key safely while preserving recognizable prefix/suffix."""
    if not value:
        return ""
    s = str(value).strip()
    if len(s) <= 10:
        return "*" * len(s)
    return f"{s[:8]}••••{s[-4:]}"


def _extract_yaml_key_labels(raw_text: str) -> Dict[str, str]:
    """Extract inline YAML comments next to API keys (e.g. - 'AIza...' #label | email)."""
    labels: Dict[str, str] = {}
    pattern = re.compile(r'^\s*-\s*["\']([^"\']+)["\']\s*#\s*(.+)$')
    for line in raw_text.splitlines():
        m = pattern.match(line)
        if m:
            labels[m.group(1).strip()] = m.group(2).strip()
    return labels


class StudioConfigLoader:
    """Loads OKXSquare YAML configs and resolves provider parameters safely."""

    VALID_PROVIDERS = ("gemini_proxy", "gemma", "gemini_direct")

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.gemini_config_path = self.project_root / "config" / "gemini_config.yaml"
        self.profiles_config_path = self.project_root / "config" / "profiles_config.yaml"
        self._lock = Lock()
        self._gemini_cfg: Dict[str, Any] = {}
        self._profiles_cfg: Dict[str, Any] = {}
        self._key_labels: Dict[str, str] = {}
        self._last_loaded: float = 0.0
        self._last_error: Optional[str] = None
        self.reload()

    def reload(self) -> Dict[str, Any]:
        """Reload both YAML config files from disk."""
        with self._lock:
            self._last_error = None
            if not self.gemini_config_path.exists():
                self._last_error = f"Không tìm thấy file config: {self.gemini_config_path}"
                raise FileNotFoundError(self._last_error)

            raw_gemini = self.gemini_config_path.read_text(encoding="utf-8")
            self._key_labels = _extract_yaml_key_labels(raw_gemini)
            self._gemini_cfg = yaml.safe_load(raw_gemini) or {}

            if self.profiles_config_path.exists():
                raw_profiles = self.profiles_config_path.read_text(encoding="utf-8")
                self._profiles_cfg = yaml.safe_load(raw_profiles) or {}
            else:
                self._profiles_cfg = {}

            self._last_loaded = time.time()
            return self._build_public_snapshot_unlocked()

    def _build_provider_public(self, name: str, raw: dict) -> dict:
        raw = raw or {}
        gen = raw.get("generation") or {}
        proc = raw.get("processing") or {}
        rate = raw.get("rate_limiting") or {}

        # Models normalization
        models_list: List[dict] = []
        if isinstance(raw.get("models"), list):
            for item in raw["models"]:
                if isinstance(item, dict) and item.get("name"):
                    models_list.append({
                        "name": str(item["name"]),
                        "rpm": item.get("rpm"),
                        "tpm": item.get("tpm"),
                        "rpd": item.get("rpd"),
                        "priority": item.get("priority", 1),
                    })
                elif isinstance(item, str):
                    models_list.append({"name": item, "priority": 1})
        default_model = str(raw.get("model") or (models_list[0]["name"] if models_list else ""))

        # API key(s) metadata (masked only!)
        single_key = str(raw.get("api_key") or "").strip()
        raw_keys = [
            str(k).strip()
            for k in (raw.get("api_keys") or [])
            if k and "YOUR_" not in str(k)
        ]
        keys_meta = [
            {
                "index": idx,
                "masked": mask_secret(k),
                "label": self._key_labels.get(k, f"Key #{idx + 1}"),
            }
            for idx, k in enumerate(raw_keys)
        ]

        endpoint = str(raw.get("endpoint") or "")
        if not endpoint and name in ("gemma", "gemini_direct"):
            endpoint = "https://generativelanguage.googleapis.com (Google GenAI SDK)"

        return {
            "name": name,
            "enabled": bool(raw.get("enabled", False)),
            "protocol": "openai_compatible" if name == "gemini_proxy" else "google_genai",
            "endpoint": endpoint,
            "has_custom_endpoint": bool(raw.get("endpoint")),
            "model": default_model,
            "models": models_list,
            "has_api_key": bool(single_key) or len(raw_keys) > 0,
            "api_key_masked": mask_secret(single_key) if single_key else "",
            "api_keys_count": len(raw_keys) if raw_keys else (1 if single_key else 0),
            "api_keys_meta": keys_meta,
            "generation": {
                "temperature": float(gen.get("temperature", 0.65)),
                "top_p": float(gen.get("top_p", 0.8)),
                "top_k": int(gen.get("top_k", 40)),
                "max_output_tokens": int(gen.get("max_output_tokens", 2048)),
            },
            "processing": {
                "parallel_mode": bool(proc.get("parallel_mode", False)),
                "max_workers": proc.get("max_workers"),
                "retry_attempts": int(proc.get("retry_attempts", 3)),
                "retry_delay": int(proc.get("retry_delay", 2)),
                "request_timeout": int(proc.get("request_timeout", 60)),
                "cooldown_seconds": int(
                    proc.get("cooldown_seconds", proc.get("model_cooldown_seconds", 40))
                ),
            },
            "rate_limiting": dict(rate),
        }

    def _build_public_snapshot_unlocked(self) -> Dict[str, Any]:
        active = str(self._gemini_cfg.get("active_provider", "gemini_proxy")).lower()
        providers = {
            p: self._build_provider_public(p, self._gemini_cfg.get(p) or {})
            for p in self.VALID_PROVIDERS
        }

        crawl_sources = self._profiles_cfg.get("crawl_sources") or {}
        bin_cfg = crawl_sources.get("binance") or {}
        bin_authors = bin_cfg.get("authors") or []
        bin_authors_file = str(bin_cfg.get("authors_file") or "")
        bin_authors_count = len(bin_authors)
        if not bin_authors_count and bin_authors_file:
            af_path = Path(bin_authors_file)
            if not af_path.is_absolute():
                af_path = self.project_root / af_path
            if af_path.exists():
                try:
                    af_data = yaml.safe_load(af_path.read_text(encoding="utf-8")) or {}
                    if isinstance(af_data, list):
                        bin_authors_count = len(af_data)
                    elif isinstance(af_data, dict):
                        if isinstance(af_data.get("authors"), list):
                            bin_authors_count = len(af_data["authors"])
                        else:
                            bin_authors_count = sum(
                                len(v) for v in af_data.values() if isinstance(v, list)
                            )
                except Exception:
                    pass

        crawl_defaults = {
            "binance": {
                "enabled": bool(bin_cfg.get("enabled", True)),
                "authors": bin_authors,
                "authors_file": bin_authors_file,
                "authors_count": bin_authors_count,
                "delay_seconds": float(bin_cfg.get("delay_seconds", 0.3)),
            },
            "kucoin": {
                "enabled": bool((crawl_sources.get("kucoin") or {}).get("enabled", False)),
                "page_size": int((crawl_sources.get("kucoin") or {}).get("page_size", 20)),
                "delay_seconds": float((crawl_sources.get("kucoin") or {}).get("delay_seconds", 1.0)),
            },
            "sosovalue": {
                "enabled": bool((crawl_sources.get("sosovalue") or {}).get("enabled", False)),
                "page_size": int((crawl_sources.get("sosovalue") or {}).get("page_size", 30)),
                "delay_seconds": float((crawl_sources.get("sosovalue") or {}).get("delay_seconds", 1.0)),
            },
            "okx": {
                "enabled": bool((crawl_sources.get("okx") or {}).get("enabled", True)),
                "authors": (crawl_sources.get("okx") or {}).get("authors") or [],
                "delay_seconds": float((crawl_sources.get("okx") or {}).get("delay_seconds", 2.0)),
            },
        }

        mtime = (
            os.path.getmtime(self.gemini_config_path)
            if self.gemini_config_path.exists()
            else 0.0
        )

        return {
            "config_path": str(self.gemini_config_path),
            "profiles_path": str(self.profiles_config_path),
            "active_provider": active,
            "providers": providers,
            "global_processing": dict(self._gemini_cfg.get("processing") or {}),
            "crawl_sources": crawl_defaults,
            "last_loaded": self._last_loaded,
            "file_mtime": mtime,
            "error": self._last_error,
        }

    def get_public_config(self) -> Dict[str, Any]:
        with self._lock:
            return self._build_public_snapshot_unlocked()

    def get_profiles_config(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._profiles_cfg)

    def resolve_provider_runtime(
        self,
        provider_name: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Resolve real (unmasked) runtime settings for executing an API call."""
        overrides = overrides or {}
        with self._lock:
            active = str(self._gemini_cfg.get("active_provider", "gemini_proxy")).lower()
            target = str(provider_name or active).lower()
            if target not in self.VALID_PROVIDERS:
                raise ValueError(f"Provider không hợp lệ: '{target}' (chọn {self.VALID_PROVIDERS})")

            raw = dict(self._gemini_cfg.get(target) or {})
            gen = dict(raw.get("generation") or {})
            proc = dict(raw.get("processing") or {})
            key_labels = dict(self._key_labels)

        # Resolve model
        models = raw.get("models") or []
        default_model = raw.get("model") or (
            models[0]["name"] if models and isinstance(models[0], dict) else ""
        )
        model = str(overrides.get("model") or default_model).strip()

        # Resolve endpoint (for proxy)
        endpoint = str(overrides.get("endpoint") or raw.get("endpoint") or "").strip()

        # Resolve API keys
        api_key_override = str(overrides.get("api_key_override") or "").strip()
        if target == "gemini_proxy":
            single_key = api_key_override or str(raw.get("api_key") or "").strip()
            api_keys = [single_key] if single_key else []
            selected_key_index = 0
        else:
            if api_key_override:
                api_keys = [api_key_override]
                selected_key_index = 0
            else:
                api_keys = [
                    str(k).strip()
                    for k in (raw.get("api_keys") or [])
                    if k and "YOUR_" not in str(k)
                ]
                raw_idx = overrides.get("key_index")
                if raw_idx is not None and str(raw_idx) != "" and str(raw_idx) != "auto":
                    selected_key_index = max(0, min(int(raw_idx), max(0, len(api_keys) - 1)))
                else:
                    selected_key_index = None  # auto rotate

        def _f(key: str, default: float) -> float:
            val = overrides.get(key)
            if val is not None and val != "":
                return float(val)
            return float(gen.get(key, default))

        def _i(key: str, source_dict: dict, default: int) -> int:
            val = overrides.get(key)
            if val is not None and val != "":
                return int(float(val))
            return int(source_dict.get(key, default))

        return {
            "provider": target,
            "enabled_in_yaml": bool(raw.get("enabled", False)),
            "endpoint": endpoint,
            "model": model,
            "api_keys": api_keys,
            "key_labels": key_labels,
            "selected_key_index": selected_key_index,
            "temperature": _f("temperature", 0.65),
            "top_p": _f("top_p", 0.8),
            "top_k": _i("top_k", gen, 40),
            "max_output_tokens": _i("max_output_tokens", gen, 2048),
            "request_timeout": _i("request_timeout", proc, 60),
            "retry_attempts": max(1, _i("retry_attempts", proc, 2)),
            "retry_delay": max(0, _i("retry_delay", proc, 2)),
        }

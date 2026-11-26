import os
import yaml
from typing import Dict


def load_profiles_config(config_path: str = "config/profiles_config.yaml") -> dict:
    """Load config từ profiles_config.yaml (fallback relative to project root)."""
    if not os.path.exists(config_path):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(project_root, 'config', 'profiles_config.yaml')

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"✗ Error loading config: {e}")
        return {}


def get_active_account_count(config: dict) -> int:
    """Số account active thực tế (enabled + acc_activity), không phân nhóm."""
    posting_config = config.get('posting_config', {}) or {}
    acc_activity = posting_config.get('acc_activity', []) or []

    count = 0
    for idx, acc_config in enumerate(config.get('posting_accounts', []) or [], start=1):
        if not acc_config.get('enabled', True):
            continue
        if acc_activity and idx not in acc_activity:
            continue
        count += 1
    return count


def calculate_dynamic_buffer(config: dict) -> Dict[str, int]:
    """Buffer động toàn cục (max_ready_buffer, max_total_buffer) theo số account active + post_interval."""
    num_accounts = get_active_account_count(config)
    posting_config = config.get('posting_config', {}) or {}
    crawl_config = config.get('crawl_workers', {}) or {}

    reload_interval = crawl_config.get('reload_interval', 300)

    if num_accounts == 0:
        return {'max_ready_buffer': 0, 'max_total_buffer': 0}

    interval = posting_config.get('post_interval', 900)
    if not isinstance(interval, (int, float)) or interval <= 0:
        interval = 900

    expected_posts_per_cycle = (reload_interval * num_accounts) / interval
    max_ready = max(round(expected_posts_per_cycle * 4.0), 4)
    max_total = max_ready + 6

    return {'max_ready_buffer': max_ready, 'max_total_buffer': max_total}
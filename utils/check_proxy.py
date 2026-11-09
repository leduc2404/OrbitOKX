"""
Proxy Checker Tool - Kiểm tra nhanh tất cả proxy từ profiles_config.yaml
Đa luồng, nhanh, đơn giản - chỉ check live/dead
Tự động gửi thông báo Telegram nếu có proxy die
"""

import yaml
import requests
import concurrent.futures
from urllib.parse import urlparse
from datetime import datetime
import sys
import os

# Config mặc định
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'profiles_config.yaml')
TEST_URL = "http://httpbin.org/ip"  # URL chính để test
TEST_URL_BACKUP = "http://www.google.com/generate_204"  # URL backup nhẹ để double-check
TIMEOUT = 10  # seconds
TIMEOUT_BACKUP = 8  # Timeout cho backup check (nhẹ hơn)

# Telegram config mặc định
TELEGRAM_BOT_TOKEN = "7301696915:AAF4qSvb9bjq7H4YwvU1BkdYxP5IOKuFgp8"
TELEGRAM_CHAT_ID = "5411129020"


def load_proxies(config_path=None):
    """Load tất cả proxy từ config - tự động nhận diện thêm/xoá"""
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
        
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    proxies = []
    for account in config.get('posting_accounts', []):
        proxy = account.get('proxy', '').strip()
        if proxy:  # Chỉ lấy proxy không rỗng
            proxies.append({
                'name': account.get('name', 'unknown'),
                'proxy': proxy,
                'enabled': account.get('enabled', False)
            })
    
    return proxies


def _backup_check_proxy(proxy):
    """
    Backup check nhẹ - dùng Google 204 endpoint
    Chỉ kiểm tra kết nối, không parse response
    Returns: True nếu proxy live, False nếu dead
    """
    try:
        proxies = {
            'http': proxy,
            'https': proxy
        }
        response = requests.get(TEST_URL_BACKUP, proxies=proxies, timeout=TIMEOUT_BACKUP)
        # Google 204 trả về status code 204 khi thành công
        return response.status_code in [200, 204]
    except:
        return False


def check_single_proxy(proxy_info):
    """
    Check 1 proxy - trả về kết quả
    Double-check: Nếu lần 1 fail, check lại bằng endpoint khác
    Chỉ đánh dead nếu cả 2 lần đều fail
    """
    name = proxy_info['name']
    proxy = proxy_info['proxy']
    enabled = proxy_info['enabled']
    
    try:
        proxies = {
            'http': proxy,
            'https': proxy
        }
        
        start_time = datetime.now()
        response = requests.get(TEST_URL, proxies=proxies, timeout=TIMEOUT)
        elapsed = (datetime.now() - start_time).total_seconds()
        
        if response.status_code == 200:
            ip_data = response.json()
            return {
                'name': name,
                'proxy': proxy,
                'enabled': enabled,
                'status': 'LIVE ✓',
                'response_time': f"{elapsed:.2f}s",
                'ip': ip_data.get('origin', 'N/A'),
                'success': True
            }
        else:
            # Lần 1 fail (HTTP error) -> Double check
            if _backup_check_proxy(proxy):
                return {
                    'name': name,
                    'proxy': proxy,
                    'enabled': enabled,
                    'status': 'LIVE ✓ (backup)',
                    'response_time': f"{elapsed:.2f}s",
                    'ip': 'N/A (backup check)',
                    'success': True
                }
            return {
                'name': name,
                'proxy': proxy,
                'enabled': enabled,
                'status': f'DEAD ✗ (HTTP {response.status_code})',
                'response_time': 'N/A',
                'ip': 'N/A',
                'success': False
            }
            
    except requests.exceptions.Timeout:
        # Lần 1 fail (Timeout) -> Double check
        if _backup_check_proxy(proxy):
            return {
                'name': name,
                'proxy': proxy,
                'enabled': enabled,
                'status': 'LIVE ✓ (backup)',
                'response_time': 'slow',
                'ip': 'N/A (backup check)',
                'success': True
            }
        return {
            'name': name,
            'proxy': proxy,
            'enabled': enabled,
            'status': 'DEAD ✗ (Timeout x2)',
            'response_time': 'N/A',
            'ip': 'N/A',
            'success': False
        }
    except requests.exceptions.ProxyError as e:
        # Lần 1 fail (Proxy Error) -> Double check
        if _backup_check_proxy(proxy):
            return {
                'name': name,
                'proxy': proxy,
                'enabled': enabled,
                'status': 'LIVE ✓ (backup)',
                'response_time': 'N/A',
                'ip': 'N/A (backup check)',
                'success': True
            }
        return {
            'name': name,
            'proxy': proxy,
            'enabled': enabled,
            'status': 'DEAD ✗ (Proxy Error x2)',
            'response_time': 'N/A',
            'ip': 'N/A',
            'success': False
        }
    except Exception as e:
        # Lần 1 fail (Other Error) -> Double check
        if _backup_check_proxy(proxy):
            return {
                'name': name,
                'proxy': proxy,
                'enabled': enabled,
                'status': 'LIVE ✓ (backup)',
                'response_time': 'N/A',
                'ip': 'N/A (backup check)',
                'success': True
            }
        return {
            'name': name,
            'proxy': proxy,
            'enabled': enabled,
            'status': f'DEAD ✗ ({type(e).__name__} x2)',
            'response_time': 'N/A',
            'ip': 'N/A',
            'success': False
        }


def mask_proxy(proxy_url):
    """Ẩn password trong proxy URL"""
    try:
        parsed = urlparse(proxy_url)
        if parsed.password:
            masked = proxy_url.replace(parsed.password, '****')
            return masked
        return proxy_url
    except:
        return proxy_url


def send_telegram_alert(dead_proxies, bot_token=None, chat_id=None):
    """Gửi thông báo Telegram khi có proxy die"""
    if bot_token is None:
        bot_token = TELEGRAM_BOT_TOKEN
    if chat_id is None:
        chat_id = TELEGRAM_CHAT_ID
    
    if not dead_proxies:
        return True
    
    # Tạo message
    message = "🔴 *PROXY ALERT - CÓ PROXY ĐÃ DIE!*\n\n"
    message += f"⏰ Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
    for p in dead_proxies:
        enabled_warning = "⚠️ ĐANG BẬT!" if p['enabled'] else ""
        message += f"• *{p['name']}*: {p['status']} {enabled_warning}\n"
        message += f"  Proxy: `{mask_proxy(p['proxy'])}`\n\n"
    
    message += f"📊 Tổng số proxy die: {len(dead_proxies)}\n"
    message += "\n⚠️ *Vui lòng thay thế proxy ngay!*"
    
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        response = requests.post(url, json=payload, timeout=10)
        
        if response.status_code == 200:
            print("📤 Đã gửi thông báo Telegram về proxy die!")
            return True
        else:
            print(f"⚠️ Không thể gửi Telegram: {response.text}")
            return False
            
    except Exception as e:
        print(f"⚠️ Lỗi gửi Telegram: {e}")
        return False


def run_check(config_path=None, send_telegram=True, bot_token=None, chat_id=None):
    """
    Chạy check tất cả proxy
    
    Args:
        config_path: Đường dẫn config file (None = mặc định)
        send_telegram: Có gửi Telegram khi có proxy die không
        bot_token: Bot token Telegram (None = mặc định)
        chat_id: Chat ID Telegram (None = mặc định)
    
    Returns:
        dict: Kết quả check {'live': int, 'dead': int, 'total': int, 'dead_proxies': list, 'all_live': bool}
    """
    print("\n" + "="*70)
    print("🔍 PROXY CHECKER - Binance Square Bot")
    print("="*70)
    
    start_time = datetime.now()
    
    # Load proxies
    proxies = load_proxies(config_path)
    
    if not proxies:
        print("\n⚠️  Không tìm thấy proxy nào trong config!")
        return {'live': 0, 'dead': 0, 'total': 0, 'dead_proxies': [], 'all_live': True}
    
    # Số luồng = số proxy
    max_workers = len(proxies)
    
    print(f"\n📋 Tìm thấy {len(proxies)} proxy cần kiểm tra")
    print(f"⚡ Đang check với {max_workers} luồng đồng thời...")
    print("-"*70)
    
    # Check đồng thời với ThreadPoolExecutor
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_proxy = {executor.submit(check_single_proxy, p): p for p in proxies}
        
        for future in concurrent.futures.as_completed(future_to_proxy):
            result = future.result()
            results.append(result)
            
            # In kết quả ngay khi có
            status_icon = "🟢" if result['success'] else "🔴"
            enabled_tag = "[ON]" if result['enabled'] else "[OFF]"
            print(f"{status_icon} {result['name']:12} {enabled_tag:6} | {result['status']:25} | Time: {result['response_time']:8}")
    
    # Tổng kết
    elapsed = (datetime.now() - start_time).total_seconds()
    live_count = sum(1 for r in results if r['success'])
    dead_count = len(results) - live_count
    dead_proxies = [r for r in results if not r['success']]
    
    print("-"*70)
    print("\n📊 KẾT QUẢ TỔNG HỢP:")
    print(f"   ✅ Live:  {live_count}/{len(results)}")
    print(f"   ❌ Dead:  {dead_count}/{len(results)}")
    print(f"   ⏱️  Thời gian check: {elapsed:.2f}s")
    
    # Chi tiết proxy live
    if live_count > 0:
        print("\n🟢 PROXY LIVE:")
        for r in results:
            if r['success']:
                print(f"   • {r['name']}: {mask_proxy(r['proxy'])}")
                print(f"     IP: {r['ip']} | Response: {r['response_time']}")
    
    # Cảnh báo proxy dead
    if dead_count > 0:
        print("\n🔴 PROXY DEAD (Cần thay thế):")
        for r in results:
            if not r['success']:
                enabled_warning = " ⚠️ ĐANG BẬT!" if r['enabled'] else ""
                print(f"   • {r['name']}: {r['status']}{enabled_warning}")
        
        # Gửi Telegram nếu có proxy die
        if send_telegram:
            send_telegram_alert(dead_proxies, bot_token, chat_id)
    
    print("\n" + "="*70)
    
    return {
        'live': live_count,
        'dead': dead_count,
        'total': len(results),
        'dead_proxies': dead_proxies,
        'all_live': dead_count == 0
    }


def startup_check(config_path=None, bot_token=None, chat_id=None):
    """
    Check proxy khi startup - trả về True nếu tất cả proxy live
    Dùng cho run.py để check trước khi chạy hệ thống
    
    Args:
        config_path: Đường dẫn config file
        bot_token: Bot token Telegram
        chat_id: Chat ID Telegram
    
    Returns:
        bool: True nếu tất cả proxy live, False nếu có proxy die
    """
    result = run_check(config_path, send_telegram=True, bot_token=bot_token, chat_id=chat_id)
    return result['all_live']


if __name__ == "__main__":
    run_check()

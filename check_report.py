"""
OKX Checker (Rewards & Balance)
===============================
Gộp 2 tính năng:
1. Check Rewards
2. Check Balance
"""

import os
import sys
import json
import time
import yaml
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, List

# Force UTF-8 encoding for Windows console
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)

try:
    from curl_cffi import requests
except ImportError:
    import requests

# ============================================================
# CONFIG
# ============================================================

CONFIG_PATH = "config/settings.yaml"
COOKIES_DIR = "cookies"
VN_TZ = timezone(timedelta(hours=7))  # UTC+7
TARGET_NOTE = "Phần thưởng nhà sáng tạo"

# Telegram Config - Mới
TELEGRAM_BOT_TOKEN = "7568683663:AAHBtLQyqyZwIGXWOVZq7Jh_a42KalUQ-6I"
TELEGRAM_CHAT_ID = "5411129020"

# Tỷ lệ chia phần thưởng (key: account index, value: tỷ lệ nhận)
# VD: 1: 1 (100%), 2: 0.6 (60%), 3: 0.7 (70%).
# Mặc định nếu account không có trong danh sách này sẽ nhận 1 (100%).
SHARE_RATIOS = {
    1: 1,
    2: 1,
    3: 0.7,
    4: 1,
    5: 0.6,
    6: 0.6,
    7: 1,
    8: 0.7,
    9: 0.6,
    10: 1,
    11: 1,
    12: 1,
    13: 1,
    14: 1,
    15: 1,
}

# ============================================================
# SHARED UTILS
# ============================================================

def parse_account_names(config_path: str = CONFIG_PATH) -> Dict[str, str]:
    import re
    names = {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            in_proxies = False
            for line in f:
                stripped = line.strip()
                if stripped.startswith('proxies:'):
                    in_proxies = True
                    continue
                if in_proxies:
                    if stripped and not stripped[0].isdigit() and not stripped.startswith('#'):
                        break
                    match = re.match(r'\s*(\d+):\s*"[^"]*"\s*#(.+)', line)
                    if match:
                        acc_idx = int(match.group(1))
                        name = match.group(2).strip()
                        names[f"account{acc_idx}"] = name
    except Exception as e:
        print(f"  ⚠ Lỗi parse account names: {e}")
    return names

def get_share_ratio(account_id: str) -> float:
    try:
        acc_idx = int(account_id.replace('account', ''))
        return SHARE_RATIOS.get(acc_idx, 1.0)
    except ValueError:
        return 1.0

def format_share_label(ratio: float) -> str:
    return f"{int(round(ratio * 100))}%"

def format_num_2(val) -> str:
    v = round(float(val), 2)
    if v.is_integer():
        return str(int(v))
    return f"{v:.2f}".rstrip('0').rstrip('.')

def format_num_1(val) -> str:
    v = round(float(val), 1)
    if v.is_integer():
        return str(int(v))
    return str(v)

def load_config(path: str = CONFIG_PATH) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config không tồn tại: {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def get_active_accounts(config: dict) -> List[str]:
    acc_activity = config.get('acc_activity', [])
    if acc_activity:
        return [f"account{n}" for n in acc_activity]
    return config.get('active_accounts', [])

def get_proxy_for_account(config: dict, account_id: str) -> Optional[str]:
    proxies_config = config.get('proxies', {})
    if not proxies_config:
        return None
    try:
        if account_id.startswith('account'):
            acc_idx = int(account_id.replace('account', ''))
            proxy = proxies_config.get(acc_idx)
            if proxy:
                return proxy
    except ValueError:
        pass
    return proxies_config.get(account_id)

def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        import requests as std_requests
        resp = std_requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"  ❌ Lỗi gửi Telegram: {resp.text}")
        else:
            print(f"  ✅ Đã gửi thông báo Telegram thành công!")
    except ImportError:
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                print(f"  ❌ Lỗi gửi Telegram: {resp.text}")
            else:
                print(f"  ✅ Đã gửi thông báo Telegram thành công!")
        except Exception as e:
            print(f"  ❌ Lỗi kết nối Telegram: {e}")
    except Exception as e:
        print(f"  ❌ Lỗi kết nối Telegram: {e}")

def parse_cookie_file_web(account_id: str) -> Optional[Dict]:
    cookie_path = os.path.join(COOKIES_DIR, f"{account_id}.json")
    if not os.path.exists(cookie_path):
        print(f"  ⚠ Bỏ qua: Không tìm thấy file {cookie_path}")
        return None
    with open(cookie_path, "r", encoding="utf-8") as f:
        try:
            cookies = json.load(f)
        except Exception as e:
            print(f"  ❌ Lỗi đọc JSON cookies của {account_id}: {e}")
            return None
    result = {
        "id": account_id,
        "token": "",
        "dev_id": "",
        "x_site_info": "",
        "cookie_string": "",
    }
    def _sanitize_cookie_value(val: str) -> str:
        try:
            val.encode('latin-1')
            return val
        except UnicodeEncodeError:
            from urllib.parse import quote
            return quote(val, safe='=/:@!$&\'()*+,;-._~')
    cookie_parts = []
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name and value:
            cookie_parts.append(f"{name}={_sanitize_cookie_value(value)}")
        if name == "token":
            result["token"] = value
        elif name == "devId":
            result["dev_id"] = value
        elif name == "ok_site_info":
            result["x_site_info"] = value
    result["cookie_string"] = "; ".join(cookie_parts)
    if not result["token"] or not result["dev_id"]:
        print(f"  ❌ Cookie của {account_id} thiếu 'token' hoặc 'devId'!")
        return None
    return result

def build_web_headers(acc_data: dict, timestamp_ms: int, referer: str) -> dict:
    return {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "vi,vi-VN;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5",
        "app-type": "web",
        "authorization": acc_data["token"],
        "cookie": acc_data["cookie_string"],
        "devid": acc_data["dev_id"],
        "referer": referer,
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "x-cdn": "https://www.okx.com",
        "x-locale": "vi_VN",
        "x-request-timestamp": str(timestamp_ms),
        "x-site-info": acc_data["x_site_info"],
        "x-utc": "7",
        "x-zkdex-env": "0",
    }

# ============================================================
# REWARDS CHECKER LOGIC
# ============================================================

def format_timestamp(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=VN_TZ)
    return dt.strftime("%d/%m/%Y %H:%M:%S")

def fetch_asset_bills(acc_data: dict, start_ms: int, end_ms: int, proxy: Optional[str] = None, page_size: int = 100) -> list:
    all_items = []
    page_index = 1
    try:
        session = requests.Session(impersonate="chrome110")
    except TypeError:
        session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        while True:
            timestamp_ms = int(time.time() * 1000)
            headers = build_web_headers(acc_data, timestamp_ms, "https://www.okx.com/vi/balance/report-center/unified/account-fund")
            url = (f"https://www.okx.com/v2/asset/bill/asset-bills"
                   f"?start={start_ms}&end={end_ms}"
                   f"&pageSize={page_size}&pageIndex={page_index}"
                   f"&t={timestamp_ms}")
            try:
                resp = session.get(url, headers=headers, timeout=15)
                data = resp.json()
            except Exception as e:
                print(f"  ❌ [{acc_data['id']}] Lỗi request page {page_index}: {e}")
                raise RuntimeError(f"Request error (page {page_index}): {e}")
            if data.get("code") != 0:
                error_code = data.get("code", "unknown")
                error_msg = data.get("msg") or data.get("error_message") or data.get("detailMsg", "")
                print(f"  ❌ [{acc_data['id']}] API error code={error_code}: {error_msg}")
                raise RuntimeError(f"API error code={error_code}: {error_msg}")
            page_data = data.get("data", {})
            items = page_data.get("list", [])
            total = page_data.get("total", 0)
            all_items.extend(items)
            if len(all_items) >= total or not items:
                break
            page_index += 1
            time.sleep(0.3)
    finally:
        try:
            session.close()
        except Exception:
            pass
    return all_items

def check_rewards_for_account(acc_data: dict, proxy: Optional[str] = None, weeks_back: int = 4) -> dict:
    result = {
        "id": acc_data["id"],
        "status": "error",
        "reward": None,
        "error": None,
        "error_code": None,
    }
    now = datetime.now(tz=VN_TZ)
    start_dt = now - timedelta(weeks=weeks_back)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    try:
        bills = fetch_asset_bills(acc_data, start_ms, end_ms, proxy=proxy)
    except Exception as e:
        error_str = str(e)
        result["error"] = error_str
        if "code=" in error_str:
            try:
                result["error_code"] = error_str.split("code=")[1].split(":")[0].strip()
            except:
                pass
        return result
    creator_rewards = [bill for bill in bills if bill.get("note") == TARGET_NOTE]
    if not creator_rewards:
        result["status"] = "no_reward"
        return result
    latest = max(creator_rewards, key=lambda x: x.get("createTime", 0))
    result["status"] = "ok"
    result["reward"] = {
        "amount": latest.get("size", "0"),
        "currency": latest.get("currency", "?"),
        "balance_after": latest.get("afterBalance", "?"),
        "balance_before": latest.get("beforeBalance", "?"),
        "time": format_timestamp(latest["createTime"]),
        "time_raw": latest["createTime"],
        "note": latest.get("note", ""),
        "type_name": latest.get("typeName", ""),
        "total_found": len(creator_rewards),
    }
    return result

def run_rewards_check(config: dict, accounts: List[dict], account_names: dict):
    results = []
    with ThreadPoolExecutor(max_workers=len(accounts)) as executor:
        future_map = {}
        for acc in accounts:
            proxy = get_proxy_for_account(config, acc["id"])
            future = executor.submit(check_rewards_for_account, acc, proxy)
            future_map[future] = acc["id"]
        for future in as_completed(future_map):
            acc_id = future_map[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"id": acc_id, "status": "error", "reward": None, "error": str(e)})

    results.sort(key=lambda x: int(x["id"].replace("account", "")))

    print("=" * 70)
    print("  KẾT QUẢ PHẦN THƯỞNG NHÀ SÁNG TẠO")
    print("=" * 70)

    total_reward_gross = 0.0
    total_reward_net = 0.0
    tele_msg = "<b>💰 BÁO CÁO PHẦN THƯỞNG NHÀ SÁNG TẠO OKX</b>\n\n"

    for r in results:
        acc_id = r["id"]
        acc_num = acc_id.replace("account", "Account ")
        acc_name = account_names.get(acc_id, "")
        acc_label = f"{acc_num} ({acc_name})" if acc_name else acc_num

        if r["status"] == "ok" and r["reward"]:
            rw = r["reward"]
            amount = float(rw["amount"])
            ratio = get_share_ratio(acc_id)
            actual_received = amount * ratio
            total_reward_gross += amount
            total_reward_net += actual_received
            
            am_fmt = format_num_1(amount)
            actual_fmt = format_num_1(actual_received)
            bal_fmt = format_num_1(rw["balance_after"]) if rw["balance_after"] != "?" else "?"

            print(f"\n  ✅ {acc_label}")
            print(f"     💰 Phần thưởng: +{am_fmt} {rw['currency']}")
            if ratio < 1.0:
                print(f"     💵 Thực nhận ({format_share_label(ratio)}): +{actual_fmt} {rw['currency']}")
            print(f"     📅 Thời gian:   {rw['time']}")
            print(f"     💳 Số dư sau:   {bal_fmt} {rw['currency']}")
            if rw["total_found"] > 1:
                print(f"     📊 Tổng {rw['total_found']} lần nhận (hiển thị lần gần nhất)")

            tele_msg += f"👤 <b>{acc_label}</b>\n"
            tele_msg += f"├ 🎁 Thưởng: <b>+{am_fmt} {rw['currency']}</b>\n"
            if ratio < 1.0:
                tele_msg += f"├ 💵 Thực nhận ({format_share_label(ratio)}): <b>+{actual_fmt} {rw['currency']}</b>\n"
            tele_msg += f"├ 💳 Số dư sau: {bal_fmt} {rw['currency']}\n"
            tele_msg += f"└ ⏱ Thời gian: {rw['time']}\n\n"

        elif r["status"] == "no_reward":
            print(f"\n  ⚠️  {acc_label}")
            print(f"     Không có phần thưởng nhà sáng tạo trong 4 tuần qua")
            tele_msg += f"⚠️ <b>{acc_label}</b>: Không có phần thưởng trong 4 tuần qua\n\n"

        else:
            error_msg = r.get('error', 'Unknown error')
            error_code = r.get('error_code')
            print(f"\n  ❌ {acc_label}")
            print(f"     Lỗi: {error_msg}")
            if error_code:
                print(f"     Mã lỗi: {error_code}")
                tele_msg += f"❌ <b>{acc_label}</b>: Mã lỗi <code>{error_code}</code>\n  • {error_msg}\n\n"
            else:
                tele_msg += f"❌ <b>{acc_label}</b>: Lỗi - {error_msg}\n\n"

    gross_fmt = format_num_1(total_reward_gross)
    net_fmt = format_num_1(total_reward_net)
    ok_count = sum(1 for r in results if r['status'] == 'ok')
    print(f"\n{'─' * 70}")
    print(f"  📊 TỔNG KẾT: {gross_fmt} USDT gốc → {net_fmt} USDT thực nhận")
    print(f"     từ {ok_count} accounts")
    print(f"{'─' * 70}\n")
    
    tele_msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
    tele_msg += f"📊 <b>TỔNG CỘNG: {gross_fmt} USDT</b>\n"
    tele_msg += f"💵 <b>THỰC NHẬN: {net_fmt} USDT</b>"
    send_telegram_message(tele_msg)

# ============================================================
# BALANCE CHECKER LOGIC
# ============================================================

def fetch_balance_portfolio(acc_data: dict, proxy: Optional[str] = None) -> dict:
    try:
        session = requests.Session(impersonate="chrome110")
    except TypeError:
        session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    timestamp_ms = int(time.time() * 1000)
    headers = build_web_headers(acc_data, timestamp_ms, "https://www.okx.com/vi/balance/overview")
    url = (f"https://www.okx.com/v2/asset/balance/balance-portfolio"
           f"?valuationUnit=USD&operationControl=true"
           f"&cedefiLimit=false&includeCedefi=true"
           f"&t={timestamp_ms}")
    try:
        resp = session.get(url, headers=headers, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"  ❌ [{acc_data['id']}] Lỗi request: {e}")
        raise RuntimeError(f"Request error: {e}")
    finally:
        try:
            session.close()
        except:
            pass
    if data.get("code") != 0:
        error_code = data.get("code", "unknown")
        error_msg = data.get("msg") or data.get("error_message") or data.get("detailMsg", "")
        print(f"  ❌ [{acc_data['id']}] API error code={error_code}: {error_msg}")
        raise RuntimeError(f"API error code={error_code}: {error_msg}")
    return data.get("data", {})

def check_balance_for_account(acc_data: dict, proxy: Optional[str] = None) -> dict:
    result = {
        "id": acc_data["id"],
        "status": "error",
        "total_valuation": 0.0,
        "crypto_valuation": 0.0,
        "tokens": [],
        "error": None,
        "error_code": None,
    }
    try:
        portfolio = fetch_balance_portfolio(acc_data, proxy=proxy)
    except Exception as e:
        error_str = str(e)
        result["error"] = error_str
        if "code=" in error_str:
            try:
                result["error_code"] = error_str.split("code=")[1].split(":")[0].strip()
            except:
                pass
        return result
    try:
        result["total_valuation"] = float(portfolio.get("totalValuation", "0"))
    except:
        result["total_valuation"] = 0.0
    crypto = portfolio.get("crypto", {})
    try:
        result["crypto_valuation"] = float(crypto.get("totalValuation", "0"))
    except:
        result["crypto_valuation"] = 0.0
    balances = crypto.get("balances", [])
    for bal in balances:
        try:
            token_balance = float(bal.get("balance", "0"))
            if token_balance <= 0:
                continue
            token_info = {
                "currency": bal.get("currency", "?"),
                "balance": token_balance,
                "valuation_usd": float(bal.get("valuation", "0")),
                "unit_price": float(bal.get("unitPrice", "0")),
                "funding_balance": float(bal.get("fundingBalance", "0")),
                "trading_balance": float(bal.get("tradingBalance", "0")),
                "grow_balance": float(bal.get("growBalance", "0")),
            }
            result["tokens"].append(token_info)
        except:
            continue
    result["tokens"].sort(key=lambda x: x["valuation_usd"], reverse=True)
    result["status"] = "ok"
    return result

def run_balance_check(config: dict, accounts: List[dict], account_names: dict):
    results = []
    with ThreadPoolExecutor(max_workers=len(accounts)) as executor:
        future_map = {}
        for acc in accounts:
            proxy = get_proxy_for_account(config, acc["id"])
            future = executor.submit(check_balance_for_account, acc, proxy)
            future_map[future] = acc["id"]
        for future in as_completed(future_map):
            acc_id = future_map[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append({
                    "id": acc_id, "status": "error", "total_valuation": 0.0,
                    "crypto_valuation": 0.0, "tokens": [], "error": str(e), "error_code": None
                })
    results.sort(key=lambda x: int(x["id"].replace("account", "")))

    print("=" * 70)
    print("  SỐ DƯ TÀI SẢN CÁC ACCOUNT")
    print("=" * 70)

    total_gross = 0.0
    total_net = 0.0
    ok_count = 0
    tele_msg = f"<b>💰 BÁO CÁO SỐ DƯ TÀI SẢN OKX</b>\n"
    tele_msg += f"🕐 {datetime.now(tz=VN_TZ).strftime('%d/%m/%Y %H:%M')}\n\n"

    for r in results:
        acc_id = r["id"]
        acc_num = acc_id.replace("account", "Account ")
        acc_name = account_names.get(acc_id, "")
        acc_label = f"{acc_num} ({acc_name})" if acc_name else acc_num

        if r["status"] == "ok":
            ok_count += 1
            total_val = r["total_valuation"]
            ratio = get_share_ratio(acc_id)
            actual_val = total_val * ratio
            total_gross += total_val
            total_net += actual_val

            val_fmt = format_num_2(total_val)
            actual_fmt = format_num_2(actual_val)

            print(f"\n  💼 {acc_label}")
            print(f"     💰 Tổng tài sản: {val_fmt} USD")
            if ratio < 1.0:
                print(f"     💵 Thực nhận ({format_share_label(ratio)}): {actual_fmt} USD")

            for tok in r["tokens"]:
                tok_bal = format_num_2(tok["balance"])
                tok_val = format_num_2(tok["valuation_usd"])
                print(f"     • {tok['currency']}: {tok_bal} (≈{tok_val} USD)")

            tele_msg += f"👤 <b>{acc_label}</b>\n"
            tele_msg += f"├ 💰 Tổng: <b>{val_fmt} USD</b>\n"
            if ratio < 1.0:
                tele_msg += f"├ 💵 Thực nhận ({format_share_label(ratio)}): <b>{actual_fmt} USD</b>\n"
            
            top_tokens = r["tokens"][:3]
            for i, tok in enumerate(top_tokens):
                tok_bal = format_num_2(tok["balance"])
                prefix = "└" if i == len(top_tokens) - 1 else "├"
                tele_msg += f"{prefix} 🔹 {tok['currency']}: {tok_bal}\n"
            tele_msg += "\n"
        else:
            error_msg = r.get('error', 'Unknown error')
            error_code = r.get('error_code')
            print(f"\n  ❌ {acc_label}")
            print(f"     Lỗi: {error_msg}")
            if error_code:
                print(f"     Mã lỗi: {error_code}")
                tele_msg += f"❌ <b>{acc_label}</b>: Mã lỗi <code>{error_code}</code>\n  • {error_msg}\n\n"
            else:
                tele_msg += f"❌ <b>{acc_label}</b>: Lỗi - {error_msg}\n\n"

    gross_fmt = format_num_2(total_gross)
    net_fmt = format_num_2(total_net)
    print(f"\n{'─' * 70}")
    print(f"  📊 TỔNG KẾT: {gross_fmt} USD gốc → {net_fmt} USD thực nhận")
    print(f"     từ {ok_count} accounts")
    print(f"{'─' * 70}\n")

    tele_msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
    tele_msg += f"📊 <b>TỔNG CỘNG: {gross_fmt} USD</b>\n"
    tele_msg += f"💵 <b>THỰC NHẬN: {net_fmt} USD</b>"
    send_telegram_message(tele_msg)

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 70)
    print("  OKX CHECKER (REWARDS & BALANCE)")
    print(f"  Thời gian: {datetime.now(tz=VN_TZ).strftime('%d/%m/%Y %H:%M:%S')} (UTC+7)")
    print("=" * 70)

    print("Chọn chức năng:")
    print("1 là check rewards")
    print("2 là check ballance")
    choice = input("Nhập lựa chọn (1/2): ").strip()

    if choice not in ['1', '2']:
        print("Lựa chọn không hợp lệ. Thoát.")
        return

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"\n❌ {e}")
        return

    active_account_ids = get_active_accounts(config)
    if not active_account_ids:
        print("\n❌ Không có account active nào trong config!")
        return

    print(f"\n→ Active accounts từ config: {active_account_ids}")

    accounts = []
    for acc_id in active_account_ids:
        acc_data = parse_cookie_file_web(acc_id)
        if acc_data:
            accounts.append(acc_data)

    if not accounts:
        print("\n❌ Không có account nào hợp lệ!")
        return

    print(f"→ {len(accounts)} accounts sẵn sàng: {[a['id'] for a in accounts]}")
    account_names = parse_account_names()

    if choice == '1':
        print(f"→ Đang kiểm tra phần thưởng nhà sáng tạo (4 tuần gần nhất)...\n")
        run_rewards_check(config, accounts, account_names)
    else:
        print(f"→ Đang kiểm tra số dư tài sản...\n")
        run_balance_check(config, accounts, account_names)

if __name__ == "__main__":
    main()

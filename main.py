# -*- coding: utf-8 -*-
"""OKXSquare — main entry point (interactive menu + --mode).

Pure-Python pipeline: Crawl & Dedup -> Image/Gemini -> Sign & Publish (OKX).
Đăng bài mặc định dry_run=True (config posting_config.publish.dry_run) cho tới
khi được duyệt live. Farming chưa triển khai (placeholder).
"""
import argparse
import os
import subprocess
import sys

from infra.logging_config import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

# Force UTF-8 cho console Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        import io
        if sys.stdout is not None and hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                          errors="replace", line_buffering=True)
        if sys.stderr is not None and hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                          errors="replace", line_buffering=True)

from core.orchestrator import Orchestrator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = "config/profiles_config.yaml"
MODES = ("1", "2", "3", "4", "5", "6", "7")


def auto_start_telegram_bot():
    """Khởi động Telegram control bot nền nếu chưa chạy (best-effort)."""
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = proc.info.get("cmdline") or []
                if any("telegram_bot_main.py" in part for part in cmd):
                    print(f"ℹ️ Telegram Bot đang chạy (PID {proc.pid}).")
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        bot_path = os.path.join(BASE_DIR, "services", "telegram", "telegram_bot_main.py")
        if not os.path.exists(bot_path):
            return
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        subprocess.Popen([sys.executable, bot_path], cwd=BASE_DIR, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("🤖 Đã khởi động Telegram Bot nền.")
    except Exception as e:
        print(f"⚠️ Không thể auto-start Telegram Bot: {e}")


def print_menu():
    print("\n" + "=" * 80)
    print("OKX SQUARE AUTO SYSTEM")
    print("=" * 80)
    print("\nChọn chế độ hoạt động:\n")
    print("1. 🚀 Tự động liên tục (Background Crawler + Poster workers)")
    print("   → Crawl nền mỗi chu kỳ → Gemini → Sign & Publish đa account")
    print("   → Chạy vô hạn tới khi Ctrl+C\n")
    print("2. 🔄 Một chu kỳ (Once)")
    print("   → Crawl + Gemini + gán nhóm → đăng một batch (dry_run mặc định)\n")
    print("3. 🕷️  Crawl & chuẩn bị (không đăng)")
    print("   → Crawl (Binance authors / OKX) → Ảnh → Gemini\n")
    print("4. 📤 Chỉ đăng bài (Post Only)")
    print("   → Đăng các bài đã xử lý (dry_run mặc định)\n")
    print("5. 📊 Thống kê Database\n")
    print("6. 🍪 Trạng thái cookie/JWT từng account\n")
    print("7. 🖥️  Trạng thái Poster + Database\n")
    print("0. ❌ Thoát\n")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="OKXSquare Auto System")
    parser.add_argument("--mode", type=str, help="Chạy thẳng mode (1-7) không cần menu")
    parser.add_argument("--per-account", type=int, default=1,
                        help="Số bài mỗi account trong mode 2/4 (mặc định 1)")
    parser.add_argument("--no-bot", action="store_true",
                        help="Không auto-start Telegram bot nền")
    args = parser.parse_args()

    if not args.no_bot:
        print("\n" + "=" * 80)
        print("🤖 KHỞI ĐỘNG DỊCH VỤ NỀN")
        print("=" * 80)
        auto_start_telegram_bot()
        print("=" * 80)

    if args.mode:
        choice = args.mode.strip()
        print(f"\n🚀 Chạy mode {choice} (command line)")
    else:
        print_menu()
        choice = input("\nNhập số để chọn (0-7): ").strip()

    if choice == "0":
        print("\n👋 Đã thoát")
        return
    if choice not in MODES:
        print("\n✗ Lựa chọn không hợp lệ!")
        return

    try:
        orch = Orchestrator(CONFIG_PATH)
    except Exception as e:
        print(f"\n✗ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return

    try:
        if choice == "1":
            orch.run_forever()
        elif choice == "2":
            summary = orch.run_once(per_account=args.per_account)
            print(f"\n✅ Chu kỳ hoàn tất: {summary}")
        elif choice == "3":
            print(f"\n✅ Crawl: {orch.run_single_step('crawl')}")
        elif choice == "4":
            print(f"\n✅ Post: {orch.run_single_step('post', per_account=args.per_account)}")
        elif choice == "5":
            orch.run_single_step("stats")
        elif choice == "6":
            orch.run_single_step("cookie")
        elif choice == "7":
            orch.run_single_step("status")
    except KeyboardInterrupt:
        print("\n🛑 Đã dừng theo yêu cầu.")
    finally:
        if choice != "1":  # run_forever tự stop trong finally của nó
            orch.stop()


if __name__ == "__main__":
    main()
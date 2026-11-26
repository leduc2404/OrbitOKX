# -*- coding: utf-8 -*-
"""OKXSquare — Telegram Control Bot daemon.

Điều khiển hệ thống từ xa: /status /run /stop /logs /cookie /stats /screenshot.
Chỉ user trong allowed_users (telegram_config.yaml) được phép. Bot token đọc từ
config gitignored, KHÔNG log ra nội dung secret.
"""
import asyncio
import html
import logging
import os
import subprocess
import sys
from datetime import datetime

# Force UTF-8 cho console Windows / background process
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

import psutil
import yaml
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.append(project_root)

from infra.cookie_checker import CookieExpiryChecker
from infra.database import DatabaseManager

log_dir = os.path.join(project_root, "data", "logs")
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "telegram_bot.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

MODES = ("1", "2", "3", "4", "5", "6", "7")


def load_config():
    path = os.path.join(project_root, "config", "telegram_config.yaml")
    if not os.path.exists(path):
        logger.error("Không tìm thấy config: %s", path)
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("telegram", {})
    except Exception as e:
        logger.error("Lỗi đọc Telegram config: %s", e)
        return None


tg_config = load_config()
if not tg_config:
    logger.error("Không đọc được Telegram configuration. Exiting.")
    sys.exit(1)

BOT_TOKEN = (tg_config.get("bot_token") or "").strip()
ALLOWED_USERS = tg_config.get("allowed_users", []) or []

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logger.error("bot_token chưa cấu hình (placeholder). Exiting.")
    sys.exit(1)


def restricted(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USERS:
            logger.warning("Unauthorized access attempt by user ID: %s", user_id)
            await update.message.reply_text("❌ Bạn không có quyền sử dụng bot này.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def get_auto_poster_process():
    """Tìm tiến trình python đang chạy main.py của OKXSquare."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if cmd and any("main.py" in part for part in cmd) and \
               any("OKXSquare" in part for part in cmd):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None


@restricted
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 <b>OKX SQUARE CONTROL BOT</b>\n\n"
        "<b>⚙️ Điều khiển hệ thống:</b>\n"
        "/status - Trạng thái hoạt động & Database\n"
        "/run [mode] - Chạy hệ thống (mặc định 1; ví dụ /run 2)\n"
        "/stop - Dừng hệ thống an toàn\n"
        "/logs - 25 dòng log mới nhất\n"
        "/cookie - Thời gian hết hạn cookie/JWT\n"
        "/stats - Thống kê Database\n"
        "/screenshot - Chụp màn hình\n"
        "/help - Hướng dẫn\n\n"
        "<b>Mode:</b> 1=liên tục · 2=một chu kỳ · 3=crawl · 4=post · "
        "5=stats · 6=cookie · 7=status"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


@restricted
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_handler(update, context)


@restricted
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proc = get_auto_poster_process()
    status_str = "🟢 <b>Đang chạy</b>" if proc else "🔴 <b>Đã dừng</b>"
    pid_str = f" (PID: {proc.pid})" if proc else ""
    db = DatabaseManager()
    try:
        stats = db.get_stats()
        ready = len(db.get_posts_ready_to_post())
        msg = (
            f"🖥️ <b>Trạng thái hệ thống:</b>\n"
            f"  • {status_str}{pid_str}\n\n"
            f"📊 <b>Database:</b>\n"
            f"  • Tổng bài: <b>{stats['total_posts']}</b>\n"
            f"  • Sẵn sàng đăng: <b>{ready}</b>\n"
            f"  • Đã đăng: <b>{stats['by_status'].get('posted', 0)}</b>\n"
            f"  • Accounts: <b>{stats['accounts']}</b>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
    finally:
        db.close()


@restricted
async def run_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proc = get_auto_poster_process()
    if proc:
        await update.message.reply_text(f"⚠️ Hệ thống đang chạy ở PID: {proc.pid}.")
        return
    mode = "1"
    if context.args and context.args[0].strip() in MODES:
        mode = context.args[0].strip()
    await update.message.reply_text(f"🚀 Đang khởi động hệ thống --mode {mode}...")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "auto_poster.log")
    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            subprocess.Popen(
                [sys.executable, os.path.join(project_root, "main.py"),
                 "--mode", mode, "--no-bot"],
                stdout=log_file, stderr=subprocess.STDOUT, cwd=project_root,
                creationflags=flags,
            )
        await asyncio.sleep(3)
        proc_check = get_auto_poster_process()
        if proc_check:
            await update.message.reply_text(
                f"✅ Đã khởi động!\n  • PID: <b>{proc_check.pid}</b>\n"
                f"  • Mode: <b>{mode}</b>\n"
                f"  • Log: <code>data/logs/auto_poster.log</code>", parse_mode="HTML")
        else:
            await update.message.reply_text(
                "❌ Khởi động thất bại. Kiểm tra <code>data/logs/auto_poster.log</code>",
                parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Ngoại lệ khi khởi động: {e}")


@restricted
async def stop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proc = get_auto_poster_process()
    if not proc:
        await update.message.reply_text("⚠️ Không có hệ thống nào đang chạy.")
        return
    await update.message.reply_text("🛑 Đang dừng hệ thống...")
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
        await update.message.reply_text("✅ Đã dừng hệ thống.")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi khi dừng: {e}")


@restricted
async def logs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_path = os.path.join(log_dir, "auto_poster.log")
    if not os.path.exists(log_path):
        await update.message.reply_text(
            "⚠️ Chưa có <code>data/logs/auto_poster.log</code>.", parse_mode="HTML")
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        content = "".join(lines[-25:])
        escaped = html.escape(content)
        if len(escaped) > 3900:
            escaped = "..." + escaped[-3900:]
        await update.message.reply_text(f"<pre>{escaped}</pre>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Không thể đọc log: {e}")


@restricted
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = DatabaseManager()
    try:
        s = db.get_stats()
        msg = (
            f"📊 <b>THỐNG KÊ DATABASE</b>\n\n"
            f"• Tổng bài: <b>{s['total_posts']}</b>\n"
            f"• Đã crawl: <b>{s['by_status'].get('crawled', 0)}</b>\n"
            f"• Gemini done: <b>{s['has_gemini_output']}</b>\n"
            f"• Đã đăng: <b>{s['by_status'].get('posted', 0)}</b>\n"
            f"• Thất bại: <b>{s['by_status'].get('failed', 0)}</b>\n"
            f"• Accounts: <b>{s['accounts']}</b>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")
    finally:
        db.close()


@restricted
async def cookie_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🍪 Đang kiểm tra cookie/JWT...")
    try:
        checker = CookieExpiryChecker()
        checker.cache_and_schedule()
        now = datetime.now()
        lines = ["🍪 <b>TRẠNG THÁI COOKIE/JWT</b>\n"]
        ordered = sorted(checker._cached_expiry.items(),
                         key=lambda x: x[1].get("expiry_date") or datetime.max)
        for idx, info in ordered:
            if not info.get("exists"):
                lines.append(f"❌ <b>Account {idx}</b>: không tìm thấy file cookie\n")
                continue
            expiry = info["expiry_date"]
            rem = (expiry - now).total_seconds()
            days = rem / 86400
            time_left = checker._format_time_remaining(rem)
            if rem <= 0:
                icon, time_left = "🔴", "Đã hết hạn"
            elif days < 1:
                icon = "🔴"
            elif days < 4:
                icon = "🟡"
            else:
                icon = "🟢"
            lines.append(
                f"{icon} <b>Account {idx}</b>:\n"
                f"  • Còn lại: <b>{time_left}</b>\n"
                f"  • Hết hạn: <code>{expiry.strftime('%d/%m/%Y %H:%M')}</code>\n")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.error("cookie_handler lỗi: %s", e, exc_info=True)
        await update.message.reply_text(f"❌ Lỗi kiểm tra cookie: {e}")


@restricted
async def screenshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Đang chụp màn hình...")
    try:
        from PIL import ImageGrab
        shot = ImageGrab.grab()
        path = os.path.join(project_root, "data", "screenshot.png")
        shot.save(path)
        with open(path, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption="🖥️ Màn hình hiện tại")
        try:
            os.remove(path)
        except Exception:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ Không thể chụp màn hình: {e}")


async def post_init(application: Application):
    from telegram import BotCommand
    commands = [
        BotCommand("start", "Menu điều khiển"),
        BotCommand("status", "Trạng thái hệ thống & Database"),
        BotCommand("run", "Chạy hệ thống (ví dụ /run 1)"),
        BotCommand("stop", "Dừng hệ thống an toàn"),
        BotCommand("logs", "25 dòng log mới nhất"),
        BotCommand("cookie", "Hết hạn cookie/JWT"),
        BotCommand("stats", "Thống kê Database"),
        BotCommand("screenshot", "Chụp màn hình"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.error("Không set được bot commands: %s", e)


def main():
    logger.info("Initializing OKXSquare Telegram Bot...")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for name, handler in (
        ("start", start_handler), ("help", help_handler), ("status", status_handler),
        ("run", run_handler), ("stop", stop_handler), ("logs", logs_handler),
        ("stats", stats_handler), ("cookie", cookie_handler),
        ("screenshot", screenshot_handler),
    ):
        application.add_handler(CommandHandler(name, handler))
    logger.info("Starting Telegram Bot long polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
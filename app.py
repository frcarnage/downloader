import asyncio
import os
import re
import sys
import uuid
import time
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from collections import defaultdict, deque
from typing import Dict, Any, Optional, List

# Ensure UTF-8 output on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BotCommand, BotCommandScopeChat, InputMediaPhoto,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    LabeledPrice, PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from dotenv import load_dotenv

from downloader import (
    download_video, download_image, get_info, probe_formats, is_image_only,
    cleanup, cleanup_old_files, is_supported, friendly_error, DOWNLOAD_DIR,
    trim_video, convert_to_voice, convert_to_gif, change_audio_speed,
    download_subtitles, get_playlist_info, update_ytdlp,
)
from database import db, today_key
from locales import t, LANG_NAMES


# ============================================================
#                     CONFIGURATION
# ============================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://horizontal-agnese-1carnage1-4f81ce61.koyeb.app").strip().rstrip("/")

FORCE_CHANNELS = [
    {"id": "-1004300796325", "username": "botupdatesor", "name": "Updates"},
]

HARDCODED_ADMINS = {8472371058}
_admin_env = os.getenv("ADMIN_IDS", "").strip()
if _admin_env:
    for part in _admin_env.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            HARDCODED_ADMINS.add(int(part))

SHOW_PROGRESS = True
SHOW_SIZE_BUTTONS = True
ENABLE_QUEUE = True
MAX_CONCURRENT = 3
RATE_LIMIT_PER_MIN = 5
RATE_LIMIT_VIP_PER_MIN = 25
CLEANUP_INTERVAL = 3600
DAILY_REPORT_HOUR_UTC = 0

PROGRESS_UPDATE_INTERVAL = 1.5
SPEED_WINDOW = 5.0
MAX_TELEGRAM_SIZE_MB = 50.0
INFO_TIMEOUT = 70
FSUB_CACHE_TTL = 600

BOT_USERNAME = "ChatAutomationnBot"


# ============================================================
#                 SESSION & CACHE MANAGER
# ============================================================
class SessionManager:
    def __init__(self, ttl_seconds: int = 1800):
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._ttl = ttl_seconds

    def create(self, user_id: int, data: Dict[str, Any]) -> str:
        self.purge_expired()
        token = uuid.uuid4().hex[:8]
        data["created_at"] = time.time()
        data["user_id"] = user_id
        self._sessions[token] = data
        return token

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        self.purge_expired()
        return self._sessions.get(token)

    def pop(self, token: str) -> Optional[Dict[str, Any]]:
        return self._sessions.pop(token, None)

    def purge_expired(self):
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v.get("created_at", 0) > self._ttl]
        for k in expired:
            self._sessions.pop(k, None)


sessions = SessionManager(ttl_seconds=1800)
FSUB_CACHE: Dict[int, float] = {}


# ============================================================
#                   HEALTH-CHECK SERVER
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, code: int = 200):
        import json
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, code: int = 200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path in ("/ping", "/ping/"):
                self._send_text("OK")
                return
            if self.path in ("/health", "/health/"):
                self._send_json({"status": "healthy"})
                return

            if self.path.startswith("/dashboard"):
                import psutil
                cpu = psutil.cpu_percent()
                ram = psutil.virtual_memory().percent
                disk = psutil.disk_usage("/").percent
                html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Universal Downloader — Live Telemetry</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
        .card {{ background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
        .metric {{ font-size: 28px; font-weight: bold; color: #38bdf8; }}
        .label {{ color: #94a3b8; font-size: 13px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }}
        h1 {{ color: #f1f5f9; font-size: 24px; }}
        .badge {{ background: #059669; color: white; padding: 4px 10px; border-radius: 9999px; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>🎬 Universal Downloader Pro — Live Telemetry <span class="badge">ONLINE</span></h1>
    <div class="grid">
        <div class="card"><div class="label">CPU Load</div><div class="metric">{cpu}%</div></div>
        <div class="card"><div class="label">RAM Usage</div><div class="metric">{ram}%</div></div>
        <div class="card"><div class="label">Disk Usage</div><div class="metric">{disk}%</div></div>
        <div class="card"><div class="label">Queue Depth</div><div class="metric">{len(QUEUE)}</div></div>
    </div>
    <div class="card">
        <h3>System & Runtime Environment</h3>
        <p>• <b>Public URL:</b> {PUBLIC_URL}</p>
        <p>• <b>Engine:</b> yt-dlp Core + FFmpeg 7.1</p>
        <p>• <b>Database:</b> SQLite 3 WAL Mode with Automated Backups</p>
        <p>• <b>Features:</b> 50MB Bypass, 9:16 Vertical, Voice Notes, Batch DL, Telegram Stars</p>
    </div>
</body>
</html>"""
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            payload = {
                "status": "ok",
                "queue": len(QUEUE) if ENABLE_QUEUE else 0,
                "uptime_check": datetime.utcnow().isoformat() + "Z",
            }
            self._send_json(payload)
        except Exception:
            try:
                self._send_text("ERROR", 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info(f"Health server listening on port {port}")
    server.serve_forever()


# ============================================================
#                       BOT SETUP
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

router = Router()
URL_REGEX = re.compile(r"https?://[^\s]+")

QUALITY_EMOJI = {
    "360": "📱", "480": "📺", "720": "🎥",
    "1080": "💎", "best": "🚀", "audio": "🎵",
    "voice": "🎙", "gif": "🎞", "speed": "⚡", "srt": "📝",
}

RATE = defaultdict(deque)
QUEUE_SEM = asyncio.Semaphore(MAX_CONCURRENT)
QUEUE: deque = deque()


def rate_ok(user_id: int, is_vip: bool = False) -> tuple[bool, int]:
    limit = RATE_LIMIT_VIP_PER_MIN if is_vip else RATE_LIMIT_PER_MIN
    now = time.time()
    dq = RATE[user_id]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= limit:
        return False, int(60 - (now - dq[0])) + 1
    dq.append(now)
    return True, 0


# ============================================================
#                       KEYBOARDS
# ============================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Search Videos", callback_data="search_prompt")
    kb.button(text="⭐ Buy VIP (Stars)", callback_data="buy_vip")
    kb.button(text="📥 How to Use", callback_data="help")
    kb.button(text="⚡ Supported Sites", callback_data="sites")
    kb.button(text="👑 Invite & Free VIP", callback_data="ref")
    kb.button(text="⚙️ Settings", callback_data="settings")
    kb.button(text="🌐 Language", callback_data="lang")
    kb.button(text="ℹ️ About", callback_data="about")
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()


def quality_kb(token: str, sizes: dict | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    def label(q, name, emoji):
        if SHOW_SIZE_BUTTONS and sizes:
            mb = sizes.get(int(q)) if q.isdigit() else None
            if mb:
                return f"{emoji} {name} · ~{mb:.0f}MB"
        return f"{emoji} {name}"

    # Row 1 & 2: Video Resolutions
    kb.button(text=label("360", "360p", "📱"), callback_data=f"dl:{token}:360")
    kb.button(text=label("480", "480p", "📺"), callback_data=f"dl:{token}:480")
    kb.button(text=label("720", "720p", "🎥"), callback_data=f"dl:{token}:720")
    kb.button(text=label("1080", "1080p", "💎"), callback_data=f"dl:{token}:1080")
    kb.button(text="🚀 Best Available", callback_data=f"dl:{token}:best")
    kb.button(text="🎵 MP3 Audio", callback_data=f"dl:{token}:audio")

    # Row 3 & 4: Pro Formats (Vertical 9:16, Summary, Voice, GIF, Speed, Subtitles)
    kb.button(text="📱 9:16 Vertical", callback_data=f"dl:{token}:vertical")
    kb.button(text="🤖 AI Summary", callback_data=f"dl:{token}:summary")
    kb.button(text="🎙 Voice Note", callback_data=f"dl:{token}:voice")
    kb.button(text="🎞 GIF Loop", callback_data=f"dl:{token}:gif")
    kb.button(text="⚡ 1.5x Speed", callback_data=f"dl:{token}:speed")
    kb.button(text="📝 Subtitles", callback_data=f"dl:{token}:srt")
    kb.button(text="✂️ Trim Clip", callback_data=f"trim_prompt:{token}")
    kb.button(text="❌ Cancel", callback_data=f"cancel:{token}")

    kb.adjust(2, 2, 2, 2, 2, 2, 2)
    return kb.as_markup()


def lang_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, name in LANG_NAMES.items():
        kb.button(text=name, callback_data=f"setlang:{code}")
    kb.button(text="🔙 Back to Menu", callback_data="menu")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def settings_kb(current_pref: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    options = [
        ("ask", "🔄 Always Ask (Default)"),
        ("720", "🎥 Always 720p (Fast Mode)"),
        ("480", "📱 Always 480p (Data Saver)"),
        ("1080", "💎 Always 1080p (HD)"),
        ("audio", "🎵 Always MP3 (Audio Only)"),
    ]
    for key, text in options:
        prefix = "✅ " if current_pref == key else ""
        kb.button(text=f"{prefix}{text}", callback_data=f"setpref:{key}")
    kb.button(text="🌐 Change Language", callback_data="lang")
    kb.button(text="🔙 Back to Menu", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_dashboard_kb(maintenance_on: bool, ad_on: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    m_text = "🛠 Maintenance: [ON]" if maintenance_on else "🛠 Maintenance: [OFF]"
    ad_text = "📢 Ads: [ON]" if ad_on else "📢 Ads: [OFF]"
    kb.button(text="📊 Statistics", callback_data="adm:stats")
    kb.button(text=m_text, callback_data="adm:toggle_maint")
    kb.button(text=ad_text, callback_data="adm:toggle_ad")
    kb.button(text="🔄 Update yt-dlp Engine", callback_data="adm:update_engine")
    kb.button(text="📣 Broadcast", callback_data="adm:broadcast_prompt")
    kb.button(text="🧹 Purge Temp Files", callback_data="adm:cleanup")
    kb.button(text="❌ Close", callback_data="adm:close")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Back to Menu", callback_data="menu")
    return kb.as_markup()


def join_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ch in FORCE_CHANNELS:
        kb.button(
            text=f"📢 Join {ch.get('name', 'Channel')}",
            url=f"https://t.me/{ch['username']}",
        )
    kb.button(text="✅ I Joined", callback_data="check_join")
    kb.adjust(*([1] * len(FORCE_CHANNELS)), 1)
    return kb.as_markup()


# ============================================================
#                    CHANNEL MEMBERSHIP
# ============================================================
async def is_joined_all(bot: Bot, user_id: int) -> bool:
    now = time.time()
    if now - FSUB_CACHE.get(user_id, 0) < FSUB_CACHE_TTL:
        return True

    for ch in FORCE_CHANNELS:
        try:
            member = await bot.get_chat_member(ch["id"], user_id)
            if member.status not in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.RESTRICTED,
            ):
                return False
        except Exception as e:
            log.warning(f"membership check failed for {ch['id']}: {e}")
            continue

    FSUB_CACHE[user_id] = now
    return True


async def require_join(msg_or_cb, bot: Bot, user_id: int) -> bool:
    if await is_joined_all(bot, user_id):
        return True

    lang = await db.get_lang(user_id)
    text = t("access_locked", lang)

    if isinstance(msg_or_cb, CallbackQuery):
        try:
            await msg_or_cb.message.edit_text(text, reply_markup=join_kb())
        except TelegramBadRequest:
            await msg_or_cb.message.answer(text, reply_markup=join_kb())
    else:
        await msg_or_cb.answer(text, reply_markup=join_kb())
    return False


# ============================================================
#                 AUTO-DELETE & ALERT SCHEDULER
# ============================================================
async def schedule_auto_delete(bot: Bot, chat_id: int, message_ids: List[int], minutes: int = 30):
    total_seconds = minutes * 60
    warn_before_seconds = 300  # 5 minutes before auto-deletion
    sleep_first = max(0, total_seconds - warn_before_seconds)

    warn_msg = None
    try:
        if sleep_first > 0:
            await asyncio.sleep(sleep_first)
            lang = await db.get_lang(chat_id)
            try:
                warn_msg = await bot.send_message(
                    chat_id,
                    t("autodelete_warn", lang),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            await asyncio.sleep(warn_before_seconds)
        else:
            await asyncio.sleep(total_seconds)

        for mid in message_ids:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass

        if warn_msg:
            try:
                await bot.delete_message(chat_id, warn_msg.message_id)
            except Exception:
                pass

        lang = await db.get_lang(chat_id)
        done_msg = await bot.send_message(
            chat_id,
            t("autodelete_done", lang, minutes=minutes),
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(20)
        try:
            await bot.delete_message(chat_id, done_msg.message_id)
        except Exception:
            pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning(f"auto-delete error for chat {chat_id}: {e}")


# ============================================================
#                 ADMIN & HELPER FUNCTIONS
# ============================================================
async def notify_admins(bot: Bot, text: str):
    admins = await db.get_admins(HARDCODED_ADMINS)
    for aid in admins:
        try:
            await bot.send_message(aid, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def safe_status_edit(message, text: str, reply_markup=None) -> bool:
    try:
        await message.edit_caption(caption=text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest:
        pass
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest:
        pass
    try:
        await message.answer(text, reply_markup=reply_markup)
        return True
    except TelegramBadRequest:
        return False


async def animate_status(msg, stages: list[str], interval: float = 2.0, stop_event: asyncio.Event = None):
    i = 0
    while stop_event and not stop_event.is_set():
        text = stages[i % len(stages)]
        try:
            await msg.edit_caption(caption=text)
        except TelegramBadRequest:
            try:
                await msg.edit_text(text)
            except TelegramBadRequest:
                pass
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return


ANALYZE_STAGES = [
    "🔍 <b>Link detected...</b>\n<i>Checking source platform...</i>",
    "🌐 <b>Fetching metadata...</b>\n<i>Almost there...</i>",
    "🎬 <b>Parsing video streams...</b>\n<i>Getting title & thumbnail...</i>",
    "📊 <b>Reading available formats...</b>\n<i>Finding best quality...</i>",
    "✨ <b>Almost done...</b>\n<i>Preparing your options...</i>",
]

IMAGE_DOWNLOAD_STAGES = [
    "🖼 <b>Downloading image...</b>\n<i>Fetching from source...</i>",
    "🖼 <b>Downloading image...</b>\n<i>Almost there...</i>",
]


def fmt_size(b: float) -> str:
    if b < 1024:
        return f"{b:.0f} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def fmt_time(seconds: float) -> str:
    if seconds <= 0 or seconds > 86400:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def build_size_warning(sizes: dict) -> str:
    if not sizes:
        return ""
    mb_1080 = sizes.get(1080)
    if mb_1080 and mb_1080 > MAX_TELEGRAM_SIZE_MB:
        return (
            f"\n\n💡 <i>1080p is ~{mb_1080:.0f} MB. Our smart compressor will "
            f"automatically optimize it to fit Telegram!</i>"
        )
    return ""


# ============================================================
#                     COMMAND HANDLERS
# ============================================================
@router.message(CommandStart())
async def cmd_start(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        await msg.answer("🚫 <b>You are banned from using this bot.</b>")
        return

    # Check for referral link: /start ref_12345678
    referrer_id = 0
    parts = msg.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        ref_str = parts[1].replace("ref_", "")
        if ref_str.isdigit():
            referrer_id = int(ref_str)

    is_new, vip_unlocked_for = await db.register_user(msg.from_user, referrer_id=referrer_id)
    lang = await db.get_lang(msg.from_user.id)
    is_vip = await db.is_vip(msg.from_user.id)

    if is_new:
        total = await db.get_total_users_count()
        asyncio.create_task(notify_admins(
            bot,
            "🆕 <b>NEW USER</b>\n\n"
            f"👤 <b>{msg.from_user.first_name}</b>\n"
            f"🔗 @{msg.from_user.username or '—'}\n"
            f"🆔 <code>{msg.from_user.id}</code>\n"
            f"👥 Total: <b>{total}</b>"
        ))

        # Notify referrer if friend joined
        if referrer_id > 0:
            ref_count, _ = await db.get_referral_info(referrer_id)
            ref_lang = await db.get_lang(referrer_id)
            try:
                await bot.send_message(referrer_id, t("ref_new", ref_lang, count=ref_count), parse_mode=ParseMode.HTML)
            except Exception:
                pass

        # If referrer reached 3 and unlocked VIP
        if vip_unlocked_for:
            ref_lang = await db.get_lang(vip_unlocked_for)
            try:
                await bot.send_message(vip_unlocked_for, t("ref_vip_unlocked", ref_lang), parse_mode=ParseMode.HTML)
            except Exception:
                pass

    if not await require_join(msg, bot, msg.from_user.id):
        return

    vip_badge = f"\n{t('vip_badge', lang)}" if is_vip else ""
    welcome_text = (
        "╔══════════════════════╗\n"
        "   🎬 <b>UNIVERSAL DOWNLOADER</b>\n"
        "╚══════════════════════╝\n\n"
        f"👋 Welcome, <b>{msg.from_user.first_name}</b>!{vip_badge}\n\n"
        "Download videos, audio & media from <b>anywhere</b> — fast, free, no watermarks.\n\n"
        "▫️ <b>YouTube</b> · Videos, Shorts & MP3\n"
        "▫️ <b>TikTok</b> · No watermark\n"
        "▫️ <b>Instagram</b> · Reels, Posts & Albums\n"
        "▫️ <b>Twitter / X</b> · Facebook · Reddit\n"
        "▫️ <b>Smart 50 MB Bypass</b> · Auto-compression\n"
        "▫️ <b>Pro Tools</b> · Voice notes, GIFs, Trimmer, Subtitles\n\n"
        "🎯 <b>Just send me a link</b> to start!\n\n"
        "💡 <i>Tip: Type @ChatAutomationnBot in any chat to search inline!</i>"
    )
    await msg.answer(welcome_text, reply_markup=main_menu_kb())


@router.message(Command("ref"))
@router.message(Command("invite"))
async def cmd_ref(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{msg.from_user.id}"
    count, is_vip = await db.get_referral_info(msg.from_user.id)
    lang = await db.get_lang(msg.from_user.id)

    text = t("ref_title", lang, count=count, is_vip="YES ✅" if is_vip else "NO (invite 3) ⏳", link=link)

    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Share Link", url=f"https://t.me/share/url?url={link}&text=Best%20Universal%20Downloader%20Bot%20on%20Telegram!")
    kb.button(text="🔙 Back", callback_data="menu")
    kb.adjust(1)
    await msg.answer(text, reply_markup=kb.as_markup())


@router.message(Command("help"))
async def cmd_help(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    lang = await db.get_lang(msg.from_user.id)
    await msg.answer(t("how_to_use", lang), reply_markup=back_kb())


@router.message(Command("sites"))
async def cmd_sites(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    await msg.answer(
        "╔══════════════════════╗\n"
        "   ⚡ <b>SUPPORTED PLATFORMS</b>\n"
        "╚══════════════════════╝\n\n"
        "✅ YouTube (Videos, Shorts, Music, Playlists)\n"
        "✅ TikTok (Without watermark)\n"
        "✅ Instagram (Reels, Posts, Carousels)\n"
        "✅ Twitter / X\n"
        "✅ Facebook & FB Watch\n"
        "✅ Reddit & Threads\n"
        "✅ Pinterest (Videos & Photos)\n"
        "✅ Vimeo, Dailymotion, Twitch & Soundcloud\n\n"
        "<i>Powered by yt-dlp — 1000+ platforms supported</i>",
        reply_markup=back_kb()
    )


@router.message(Command("settings"))
async def cmd_settings(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    current_pref = await db.get_preferred_quality(msg.from_user.id)
    await msg.answer(
        "╔══════════════════════╗\n"
        "     ⚙️ <b>USER SETTINGS</b>\n"
        "╚══════════════════════╝\n\n"
        "Choose your default download quality or update language:",
        reply_markup=settings_kb(current_pref)
    )


@router.message(Command("lang"))
async def cmd_lang(msg: Message):
    await msg.answer("🌐 <b>Select your language / Выберите язык:</b>", reply_markup=lang_kb())


# ---------- Video Trimmer Command ----------
@router.message(Command("trim"))
async def cmd_trim(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return

    # Syntax: /trim 00:15 01:30 <url>
    parts = msg.text.split()
    if len(parts) < 4:
        lang = await db.get_lang(msg.from_user.id)
        await msg.reply(t("trim_help", lang))
        return

    start_str = parts[1]
    end_str = parts[2]
    url = parts[3]

    if not is_supported(url):
        await msg.reply("❌ Unsupported URL for trimming.")
        return

    status = await msg.reply(f"✂️ <b>Trimming clip [{start_str} - {end_str}]...</b>\n<i>Please wait...</i>")
    try:
        result = await trim_video(url, start_str, end_str)
        filepath = result["file"]
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        title = result["title"]
        await bot.send_chat_action(msg.from_user.id, ChatAction.UPLOAD_VIDEO)
        await msg.answer_video(
            FSInputFile(filepath),
            caption=f"✂️ <b>{title}</b>\n\n📦 {size_mb:.1f} MB\n✨ <i>Enjoy your clip!</i>",
            supports_streaming=True
        )
        cleanup(filepath)
        await status.delete()
        await db.increment_user_downloads(msg.from_user.id, quality="trim")
    except Exception as e:
        log.exception("trim error")
        await status.edit_text(friendly_error(e))


# ---------- Admin commands & Dashboard ----------
@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    maint = await db.get_maintenance()
    ad_info = await db.get_ad_settings()
    await msg.answer(
        "╔══════════════════════╗\n"
        "     👮 <b>ADMIN PANEL</b>\n"
        "╚══════════════════════╝\n\n"
        "Select an administrative action below:",
        reply_markup=admin_dashboard_kb(maint, ad_info.get("enabled", False))
    )


@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    stats = await db.get_stats_summary()
    by_q = stats.get("by_quality", {})
    quality_lines = "\n".join(
        f"   {QUALITY_EMOJI.get(k, '📥')} <b>{k}</b>: {v}"
        for k, v in sorted(by_q.items())
    ) or "   <i>none</i>"

    today = stats.get("today", {})
    maint = await db.get_maintenance()

    await msg.reply(
        "╔══════════════════════╗\n"
        "     📊 <b>BOT STATISTICS</b>\n"
        "╚══════════════════════╝\n\n"
        f"👥 Total Users: <code>{stats.get('total_users', 0)}</code>\n"
        f"👑 VIP Users: <code>{stats.get('vip_count', 0)}</code>\n"
        f"👮 Admins: <code>{len(admins)}</code>\n"
        f"🚫 Banned: <code>{stats.get('banned_count', 0)}</code>\n\n"
        f"✅ Total Downloads: <code>{stats.get('total_downloads', 0)}</code>\n"
        f"❌ Total Errors: <code>{stats.get('total_errors', 0)}</code>\n\n"
        f"📅 <b>Today ({today_key()})</b>\n"
        f"   New users: <code>{today.get('users', 0)}</code>\n"
        f"   Downloads: <code>{today.get('downloads', 0)}</code>\n"
        f"   Errors: <code>{today.get('errors', 0)}</code>\n\n"
        "📥 <b>By quality / format:</b>\n"
        f"{quality_lines}\n\n"
        f"🛠 Maintenance: <b>{'ON' if maint else 'OFF'}</b>"
    )


@router.message(Command("setvip"))
async def cmd_setvip(msg: Message):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    parts = msg.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or parts[2] not in ("0", "1"):
        await msg.reply("⚠️ Usage: <code>/setvip &lt;user_id&gt; 1|0</code>")
        return
    uid = int(parts[1])
    is_vip = (parts[2] == "1")
    await db.set_vip(uid, is_vip)
    await msg.reply(f"✅ User <code>{uid}</code> VIP status set to: <b>{is_vip}</b>")


@router.message(Command("setad"))
async def cmd_setad(msg: Message):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    # Syntax: /setad Button Text | https://link.com
    parts = msg.text.replace("/setad", "").strip().split("|")
    if len(parts) < 2:
        await msg.reply("⚠️ Usage: <code>/setad Button Text | https://t.me/channel</code>")
        return
    text = parts[0].strip()
    url = parts[1].strip()
    await db.set_ad_settings(enabled=True, text=text, url=url)
    await msg.reply(f"✅ Sponsored Ad saved and enabled!\n\nText: <b>{text}</b>\nURL: <code>{url}</code>")


@router.message(Command("autodelete"))
async def cmd_autodelete(msg: Message):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    parts = msg.text.split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await msg.reply("⚠️ Usage: <code>/autodelete on|off [minutes]</code>\nExample: <code>/autodelete on 30</code>")
        return
    enabled = (parts[1].lower() == "on")
    mins = 30
    if len(parts) >= 3 and parts[2].isdigit():
        mins = max(1, int(parts[2]))
    await db.set_autodelete_settings(enabled=enabled, minutes=mins)
    await msg.reply(f"🗑 Auto-delete settings updated: <b>{'ON (' + str(mins) + ' min)' if enabled else 'OFF'}</b>")


# ---------- In-Chat YouTube Search ----------
@router.message(Command("search"))
@router.message(Command("yt"))
async def cmd_search(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        lang = await db.get_lang(msg.from_user.id)
        await msg.reply(t("search_help", lang))
        return
    query = parts[1].strip()
    status = await msg.reply(f"🔍 <b>Searching YouTube for:</b> <i>{query}</i>...")
    from downloader import search_youtube
    results = await search_youtube(query, max_results=5)
    if not results:
        await status.edit_text("❌ No results found. Try another query.")
        return

    await status.delete()
    for item in results:
        token = sessions.create(msg.from_user.id, {
            "url": item["url"], "title": item["title"], "duration": item["duration"]
        })
        mins, secs = divmod(int(item["duration"]), 60)
        card_text = (
            f"🎬 <b>{item['title']}</b>\n"
            f"👤 {item['uploader']} · ⏱ {mins}:{secs:02d}\n"
            f"🔗 {item['url']}"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🎥 720p", callback_data=f"dl:{token}:720")
        kb.button(text="🎵 MP3", callback_data=f"dl:{token}:audio")
        kb.button(text="🎙 Voice", callback_data=f"dl:{token}:voice")
        kb.adjust(3)
        await msg.answer(card_text, reply_markup=kb.as_markup())


@router.callback_query(F.data == "search_prompt")
async def cb_search_prompt(cb: CallbackQuery):
    await cb.message.answer(
        "🔍 <b>In-Chat YouTube Search:</b>\n\n"
        "Send <code>/search &lt;song or video name&gt;</code> to find and download any video instantly!\n\n"
        "Example: <code>/search lofi beats</code>"
    )
    await cb.answer()


# ---------- Telegram Stars VIP Payments ----------
@router.message(Command("buyvip"))
async def cmd_buyvip(msg: Message):
    await msg.answer_invoice(
        title="👑 1 Month VIP Membership",
        description="Instant zero-wait priority queue, 1080p speed, 9:16 vertical repurposer, and ad-free downloads!",
        payload=f"vip_{msg.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label="VIP Membership", amount=50)],
    )


@router.callback_query(F.data == "buy_vip")
async def cb_buy_vip(cb: CallbackQuery):
    await cb.message.answer_invoice(
        title="👑 1 Month VIP Membership",
        description="Instant zero-wait priority queue, 1080p speed, 9:16 vertical repurposer, and ad-free downloads!",
        payload=f"vip_{cb.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label="VIP Membership", amount=50)],
    )
    await cb.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(msg: Message, bot: Bot):
    await db.set_vip(msg.from_user.id, True)
    await msg.answer(
        "🎉 <b>PAYMENT SUCCESSFUL!</b>\n\n"
        "👑 You are now an official <b>VIP Member</b> for 30 days!\n"
        "Enjoy zero queue wait times, 1080p priority, 9:16 vertical repurposer, and ad-free downloads."
    )
    await notify_admins(bot, f"⭐ <b>VIP PURCHASED!</b>\nUser: {msg.from_user.first_name} (<code>{msg.from_user.id}</code>)\nAmount: 50 Telegram Stars")


# ---------- Support & Feedback ----------
@router.message(Command("feedback"))
async def cmd_feedback(msg: Message, bot: Bot):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("✍️ <b>Usage:</b> <code>/feedback &lt;your message or broken link&gt;</code>")
        return
    fb_text = parts[1].strip()
    lang = await db.get_lang(msg.from_user.id)
    await msg.reply(t("feedback_sent", lang))
    await notify_admins(
        bot,
        f"📩 <b>USER FEEDBACK / SUPPORT</b>\n\n"
        f"👤 From: <b>{msg.from_user.first_name}</b> (<code>{msg.from_user.id}</code>)\n"
        f"🔗 @{msg.from_user.username or '—'}\n\n"
        f"💬 <b>Message:</b>\n{fb_text}\n\n"
        f"👉 To reply, use: <code>/reply {msg.from_user.id} &lt;response&gt;</code>"
    )


@router.message(Command("reply"))
async def cmd_reply(msg: Message, bot: Bot):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await msg.reply("⚠️ Usage: <code>/reply &lt;user_id&gt; &lt;message&gt;</code>")
        return
    target_id = int(parts[1])
    reply_body = parts[2]
    try:
        await bot.send_message(
            target_id,
            f"💬 <b>Support Response from Admin:</b>\n\n{reply_body}\n\n<i>Universal Downloader Support</i>",
            parse_mode=ParseMode.HTML
        )
        await msg.reply(f"✅ Reply delivered to user <code>{target_id}</code>.")
    except Exception as e:
        await msg.reply(f"❌ Failed to deliver: {e}")


# ---------- Database Backup Commands ----------
async def send_db_backup(bot: Bot):
    db_file = Path("data/bot.db")
    if not db_file.exists():
        return
    admins = await db.get_admins(HARDCODED_ADMINS)
    stats = await db.get_stats_summary()
    caption = (
        f"💾 <b>AUTOMATED DATABASE BACKUP</b>\n\n"
        f"📅 Date: <code>{datetime.utcnow().isoformat()}Z</code>\n"
        f"👥 Total Users: <code>{stats.get('total_users', 0)}</code>\n"
        f"📥 Total Downloads: <code>{stats.get('total_downloads', 0)}</code>"
    )
    for aid in admins:
        try:
            await bot.send_document(aid, FSInputFile(str(db_file)), caption=caption)
        except Exception as e:
            log.warning(f"backup send failed for {aid}: {e}")


@router.message(Command("backup"))
async def cmd_backup(msg: Message, bot: Bot):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    await msg.reply("💾 Generating and sending database backup...")
    await send_db_backup(bot)


async def daily_backup_task(bot: Bot):
    sent_day = None
    while True:
        try:
            now = datetime.utcnow()
            if now.hour == 2 and sent_day != today_key():
                sent_day = today_key()
                await send_db_backup(bot)
        except Exception as e:
            log.warning(f"daily backup task error: {e}")
        await asyncio.sleep(600)


@router.message(Command("update_engine"))
async def cmd_update_engine(msg: Message):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    status = await msg.reply("🔄 <b>Updating yt-dlp core engine...</b>\n<i>Please wait...</i>")
    ok, output = await update_ytdlp()
    if ok:
        await status.edit_text(f"✅ <b>yt-dlp Engine Updated Successfully!</b>\n\n<code>{output[-200:]}</code>")
    else:
        await status.edit_text(f"❌ <b>Update failed:</b>\n<code>{output[-200:]}</code>")


@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if msg.from_user.id not in admins:
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("⚠️ Usage: <code>/broadcast &lt;message&gt;</code>")
        return
    text = parts[1]
    user_ids = await db.get_all_user_ids()
    status = await msg.reply(f"📣 Broadcasting to {len(user_ids)} users...")

    sent, failed = 0, 0
    for i, uid in enumerate(user_ids):
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramForbiddenError:
            failed += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        if i % 30 == 0:
            await asyncio.sleep(1)

    await status.edit_text(
        f"✅ <b>Broadcast complete</b>\n\n"
        f"Sent: <code>{sent}</code>\nFailed/Blocked: <code>{failed}</code>"
    )


# ============================================================
#                      CALLBACKS
# ============================================================
@router.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery, bot: Bot):
    FSUB_CACHE.pop(cb.from_user.id, None)
    if await is_joined_all(bot, cb.from_user.id):
        await cb.answer("✅ Verified!", show_alert=True)
        await cb.message.edit_text(
            "✅ <b>Access granted!</b>\n\nSend me a video or post link to download 👇",
            reply_markup=main_menu_kb()
        )
    else:
        await cb.answer("❌ You must join all channels first.", show_alert=True)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, bot: Bot):
    if not await require_join(cb, bot, cb.from_user.id):
        return
    await cb.message.edit_text(
        "🏠 <b>Main Menu</b>\n\nSend me a link to download 👇",
        reply_markup=main_menu_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "ref")
async def cb_ref(cb: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{cb.from_user.id}"
    count, is_vip = await db.get_referral_info(cb.from_user.id)
    lang = await db.get_lang(cb.from_user.id)

    text = t("ref_title", lang, count=count, is_vip="YES ✅" if is_vip else "NO (invite 3) ⏳", link=link)

    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Share Link", url=f"https://t.me/share/url?url={link}&text=Best%20Universal%20Downloader%20Bot%20on%20Telegram!")
    kb.button(text="🔙 Back", callback_data="menu")
    kb.adjust(1)
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    lang = await db.get_lang(cb.from_user.id)
    await cb.message.edit_text(t("how_to_use", lang), reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data == "sites")
async def cb_sites(cb: CallbackQuery):
    await cb.message.edit_text(
        "⚡ <b>SUPPORTED PLATFORMS</b>\n\n"
        "▫️ YouTube (Videos, Shorts, MP3, Playlists)\n"
        "▫️ TikTok (Without watermark)\n"
        "▫️ Instagram (Reels, Posts & Carousels)\n"
        "▫️ Twitter / X, Facebook, Reddit\n"
        "▫️ Pinterest (Photos & Videos)\n"
        "▫️ Soundcloud, Twitch, Vimeo & more",
        reply_markup=back_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    await cb.message.edit_text(
        "ℹ️ <b>Universal Downloader v2.5</b>\n\n"
        "🚀 Fast · Free · No Ads · Smart Compression\n"
        "Features: MP3 Cover Art, Voice Notes, GIFs, Trimming & Inline Mode",
        reply_markup=back_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "settings")
async def cb_settings(cb: CallbackQuery):
    current_pref = await db.get_preferred_quality(cb.from_user.id)
    await cb.message.edit_text(
        "⚙️ <b>USER SETTINGS</b>\n\nSelect your default download quality or language:",
        reply_markup=settings_kb(current_pref)
    )
    await cb.answer()


@router.callback_query(F.data == "lang")
async def cb_lang(cb: CallbackQuery):
    await cb.message.edit_text("🌐 <b>Select your language / Выберите язык:</b>", reply_markup=lang_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(cb: CallbackQuery):
    code = cb.data.split(":")[1]
    await db.set_lang(cb.from_user.id, code)
    lang_name = LANG_NAMES.get(code, code)
    await cb.answer(f"Language set to: {lang_name}", show_alert=True)
    await cb.message.edit_text(t("lang_saved", code, lang_name=lang_name), reply_markup=main_menu_kb())


@router.callback_query(F.data.startswith("setpref:"))
async def cb_setpref(cb: CallbackQuery):
    quality = cb.data.split(":")[1]
    await db.set_preferred_quality(cb.from_user.id, quality)
    await cb.answer(f"✅ Preference updated to: {quality.upper()}", show_alert=True)
    await cb.message.edit_reply_markup(reply_markup=settings_kb(quality))


@router.callback_query(F.data.startswith("trim_prompt:"))
async def cb_trim_prompt(cb: CallbackQuery):
    token = cb.data.split(":")[1]
    session = sessions.get(token)
    if session:
        url = session.get("url")
        await cb.message.reply(
            f"✂️ <b>To trim this video, copy and send:</b>\n\n"
            f"<code>/trim 00:00 00:30 {url}</code>\n\n"
            f"<i>(Change the start and end timestamps as needed!)</i>"
        )
    await cb.answer()


@router.callback_query(F.data.startswith("cancel"))
async def cb_cancel(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) > 1:
        sessions.pop(parts[1])
    await safe_status_edit(cb.message, "❌ <b>Download cancelled.</b>", reply_markup=back_kb())
    await cb.answer("Cancelled")


# ---------- Admin Callbacks ----------
@router.callback_query(F.data.startswith("adm:"))
async def cb_admin_panel(cb: CallbackQuery, bot: Bot):
    admins = await db.get_admins(HARDCODED_ADMINS)
    if cb.from_user.id not in admins:
        await cb.answer("Unauthorized", show_alert=True)
        return

    action = cb.data.split(":")[1]
    maint = await db.get_maintenance()
    ad_info = await db.get_ad_settings()

    if action == "close":
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass
        return

    if action == "stats":
        stats = await db.get_stats_summary()
        today = stats.get("today", {})
        text = (
            "📊 <b>STATISTICS SNAPSHOT</b>\n\n"
            f"👥 Total Users: <code>{stats.get('total_users', 0)}</code>\n"
            f"👑 VIP Users: <code>{stats.get('vip_count', 0)}</code>\n"
            f"📥 Total Downloads: <code>{stats.get('total_downloads', 0)}</code>\n"
            f"❌ Total Errors: <code>{stats.get('total_errors', 0)}</code>\n\n"
            f"📅 Today: <code>+{today.get('users', 0)} users</code> | <code>{today.get('downloads', 0)} dls</code>"
        )
        await cb.message.edit_text(text, reply_markup=admin_dashboard_kb(maint, ad_info.get("enabled", False)))
        await cb.answer()
        return

    if action == "toggle_maint":
        new_val = not maint
        await db.set_maintenance(new_val)
        await cb.answer(f"Maintenance: {'ON' if new_val else 'OFF'}", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=admin_dashboard_kb(new_val, ad_info.get("enabled", False)))
        return

    if action == "toggle_ad":
        new_ad = not ad_info.get("enabled", False)
        await db.set_ad_settings(enabled=new_ad)
        await cb.answer(f"Sponsored Ads: {'ON' if new_ad else 'OFF'}", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=admin_dashboard_kb(maint, new_ad))
        return

    if action == "update_engine":
        await cb.answer("🔄 Updating engine...", show_alert=True)
        ok, out = await update_ytdlp()
        res_text = "✅ Updated!" if ok else "❌ Failed!"
        await cb.message.answer(f"{res_text}\n<code>{out[-250:]}</code>")
        return

    if action == "cleanup":
        removed_dl = cleanup_old_files(CLEANUP_INTERVAL, DOWNLOAD_DIR)
        await cb.answer(f"🧹 Purged {removed_dl} temporary files!", show_alert=True)
        return

    if action == "broadcast_prompt":
        await cb.message.answer("📣 To send a broadcast, use:\n<code>/broadcast Your message here</code>")
        await cb.answer()


# ============================================================
#                      INLINE MODE
# ============================================================
@router.inline_query()
async def inline_download_handler(query: InlineQuery):
    text = query.query.strip()
    match = URL_REGEX.search(text)
    if not match:
        # Prompt user with how to use inline query
        await query.answer(
            results=[
                InlineQueryResultArticle(
                    id="help",
                    title="🎬 Paste any video or music link",
                    description="Example: @ChatAutomationnBot https://youtu.be/...",
                    input_message_content=InputTextMessageContent(
                        message_text="Send any video or audio link to @ChatAutomationnBot to download!"
                    )
                )
            ],
            cache_time=10,
            is_personal=True,
        )
        return

    url = match.group(0)
    if not is_supported(url):
        return

    try:
        info = await get_info(url)
        title = info.get("title", "Video")[:60]
        thumb = info.get("thumbnail")
        duration = info.get("duration", 0)
        mins, secs = divmod(int(duration), 60)

        results = [
            InlineQueryResultArticle(
                id="vid",
                title=f"🎬 Download Video: {title}",
                description=f"⏱ {mins}:{secs:02d} · Click to start download in chat",
                thumbnail_url=thumb,
                input_message_content=InputTextMessageContent(
                    message_text=f"🎬 <b>{title}</b>\n🔗 {url}\n\n<i>Downloading via @ChatAutomationnBot...</i>",
                    parse_mode=ParseMode.HTML
                )
            ),
            InlineQueryResultArticle(
                id="audio",
                title=f"🎵 Download MP3 Audio: {title}",
                description="Extract audio track with cover art",
                thumbnail_url=thumb,
                input_message_content=InputTextMessageContent(
                    message_text=f"🎵 <b>{title} (MP3)</b>\n🔗 {url}\n\n<i>Extracting audio via @ChatAutomationnBot...</i>",
                    parse_mode=ParseMode.HTML
                )
            )
        ]
        await query.answer(results, cache_time=30, is_personal=True)
    except Exception:
        pass


# ============================================================
#                      LINK PROCESSING
# ============================================================
@router.message(F.text.regexp(URL_REGEX))
async def handle_link(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        await msg.reply("🚫 <b>You are banned from using this bot.</b>")
        return

    maint = await db.get_maintenance()
    admins = await db.get_admins(HARDCODED_ADMINS)
    if maint and msg.from_user.id not in admins:
        await msg.reply("🛠 <b>Bot is currently under maintenance.</b> Please check back soon.")
        return

    if not await require_join(msg, bot, msg.from_user.id):
        return

    is_vip = await db.is_vip(msg.from_user.id)
    allowed, wait = rate_ok(msg.from_user.id, is_vip=is_vip)
    if not allowed:
        await msg.reply(f"⏳ <b>Slow down!</b> Try again in <b>{wait}s</b>.\n<i>(VIP members have unlimited speed)</i>")
        return

    # Check for multi-link batch
    matches = URL_REGEX.findall(msg.text)
    if len(matches) > 1:
        max_batch = 10 if is_vip else 5
        urls_to_process = matches[:max_batch]
        status = await msg.reply(f"📥 <b>Batch Detected:</b> Processing {len(urls_to_process)} links in sequence...")
        pref = await db.get_preferred_quality(msg.from_user.id)
        chosen_q = "720" if pref == "ask" else pref
        for idx, u in enumerate(urls_to_process, 1):
            if is_supported(u):
                try:
                    res = await download_video(u, quality=chosen_q)
                    fpath = res["file"]
                    size_mb = os.path.getsize(fpath) / (1024 * 1024)
                    await msg.answer_video(
                        FSInputFile(fpath),
                        caption=f"🎬 <b>{res['title']}</b> ({idx}/{len(urls_to_process)})\n📦 {size_mb:.1f} MB\n\n🎬 <i>Downloaded via @{BOT_USERNAME}</i>",
                        supports_streaming=True
                    )
                    cleanup(fpath)
                    await db.increment_user_downloads(msg.from_user.id, quality="batch")
                except Exception as e:
                    log.warning(f"Batch item failed: {e}")
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        return

    match = URL_REGEX.search(msg.text)
    url = match.group(0)

    if not is_supported(url):
        await msg.reply("❌ <b>Unsupported link.</b>", reply_markup=back_kb())
        return

    # Check for Playlist
    if "playlist" in url.lower() or "&list=" in url.lower():
        pl_info = await get_playlist_info(url, max_items=10)
        if pl_info and pl_info.get("tracks"):
            lang = await db.get_lang(msg.from_user.id)
            tracks = pl_info["tracks"]
            t_lines = "\n".join([f"• <code>{t['title'][:40]}</code>" for t in tracks[:5]])
            caption = (
                f"{t('playlist_title', lang, title=pl_info['title'])}\n\n"
                f"📊 Found <b>{pl_info['total']}</b> videos. First tracks:\n"
                f"{t_lines}\n\n"
                "👇 <b>Select download action:</b>"
            )
            kb = InlineKeyboardBuilder()
            # Generate token for batch
            pl_token = sessions.create(msg.from_user.id, {"url": url, "playlist": tracks})
            kb.button(text="📥 Download First 5", callback_data=f"pl:5:{pl_token}")
            kb.button(text="📥 Download First 10", callback_data=f"pl:10:{pl_token}")
            kb.button(text="❌ Cancel", callback_data=f"cancel:{pl_token}")
            kb.adjust(2, 1)
            await msg.reply(caption, reply_markup=kb.as_markup())
            return

    status = await msg.reply("🔍 <b>Analyzing link...</b>")

    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(animate_status(
        status, ANALYZE_STAGES, interval=2.0, stop_event=stop_event
    ))

    try:
        info = await asyncio.wait_for(get_info(url), timeout=INFO_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning(f"info timeout for {url}")
        await db.record_error()
        stop_event.set()
        anim_task.cancel()
        await safe_status_edit(
            status,
            "⏱️ <b>Request took too long.</b>\n\nThe source platform is slow right now — please try again in a moment.",
            reply_markup=back_kb()
        )
        return
    except Exception as e:
        log.exception("info error")
        await db.record_error()
        stop_event.set()
        anim_task.cancel()
        await safe_status_edit(status, friendly_error(e), reply_markup=back_kb())
        return
    finally:
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (asyncio.CancelledError, Exception):
            pass

    # ---------- Image or Carousel path ----------
    if is_image_only(info, url):
        title = (info.get("title") or "Image")[:80]
        thumb_url = info.get("thumbnail") or url
        token = sessions.create(msg.from_user.id, {"url": url, "title": title, "image": True})

        thumb_path = None
        video_id = info.get("id") or "img"
        if thumb_url:
            thumb_path = await _fetch_thumb(thumb_url, video_id)

        try:
            await status.delete()
        except TelegramBadRequest:
            pass

        caption = (
            "╔══════════════════════╗\n"
            "   🖼 <b>IMAGE DETECTED</b>\n"
            "╚══════════════════════╝\n\n"
            f"📌 <b>{title}</b>\n\n"
            "👇 <b>Send this to chat?</b>"
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="🖼 Download Image", callback_data=f"img:dl:{token}")
        kb.button(text="❌ Cancel", callback_data=f"cancel:{token}")
        kb.adjust(1, 1)

        if thumb_path:
            try:
                await msg.answer_photo(
                    FSInputFile(thumb_path),
                    caption=caption,
                    reply_markup=kb.as_markup(),
                )
                return
            except TelegramBadRequest:
                pass
        await msg.answer(caption, reply_markup=kb.as_markup())
        return

    # ---------- Video path ----------
    title = (info.get("title") or "Video")[:80]
    duration = info.get("duration") or 0
    mins, secs = divmod(int(duration), 60)
    uploader = (info.get("uploader") or info.get("channel") or "Unknown")[:40]
    video_id = info.get("id") or "v"
    thumb_url = info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url")

    user_pref = await db.get_preferred_quality(msg.from_user.id)
    token = sessions.create(msg.from_user.id, {
        "url": url, "title": title, "video_id": video_id, "duration": duration
    })

    if user_pref != "ask":
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        status_msg = await msg.reply(f"🚀 <b>Fast Mode:</b> Starting download in <b>{user_pref.upper()}</b>...")
        await start_video_download(bot, msg.from_user.id, status_msg, token, user_pref)
        return

    sizes = {}
    if SHOW_SIZE_BUTTONS:
        try:
            sizes = await asyncio.wait_for(probe_formats(url), timeout=18)
        except Exception:
            sizes = {}

    size_warning = build_size_warning(sizes)

    caption = (
        "╔══════════════════════╗\n"
        "   🎬 <b>MEDIA OPTIONS</b>\n"
        "╚══════════════════════╝\n\n"
        f"📌 <b>{title}</b>\n"
        f"👤 {uploader}\n"
        f"⏱ {mins}:{secs:02d}\n\n"
        "👇 <b>Choose video, audio, voice note, or GIF:</b>"
        f"{size_warning}"
    )

    thumb_path = None
    if thumb_url:
        thumb_path = await _fetch_thumb(thumb_url, video_id)

    try:
        await status.delete()
    except TelegramBadRequest:
        pass

    if thumb_path:
        try:
            await msg.answer_photo(
                FSInputFile(thumb_path),
                caption=caption,
                reply_markup=quality_kb(token, sizes),
            )
            return
        except TelegramBadRequest:
            pass
    await msg.answer(caption, reply_markup=quality_kb(token, sizes))


async def _fetch_thumb(url: str, vid: str) -> Optional[str]:
    thumbs_dir = Path("data/thumbs")
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    safe_vid = re.sub(r"[^a-zA-Z0-9_\-]", "", vid)[:32] or "thumb"
    dest = thumbs_dir / f"{safe_vid}.jpg"

    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)

    def _go():
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r, open(dest, "wb") as f:
            f.write(r.read())

    try:
        await asyncio.wait_for(asyncio.to_thread(_go), timeout=20)
        return str(dest) if dest.exists() else None
    except Exception:
        return None


# ============================================================
#               PLAYLIST BATCH DOWNLOAD FLOW
# ============================================================
@router.callback_query(F.data.startswith("pl:"))
async def cb_playlist_download(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    count = int(parts[1])
    token = parts[2]
    session = sessions.get(token)
    if not session or not session.get("playlist"):
        await cb.answer("⚠️ Session expired.", show_alert=True)
        return

    tracks = session["playlist"][:count]
    await cb.answer(f"Starting batch download of {len(tracks)} tracks!", show_alert=True)
    await safe_status_edit(cb.message, f"📥 <b>Downloading batch ({len(tracks)} tracks)...</b>")

    for i, t_item in enumerate(tracks, 1):
        try:
            res = await download_video(t_item["url"], quality="720")
            fpath = res["file"]
            size_mb = os.path.getsize(fpath) / (1024 * 1024)
            await cb.message.answer_video(
                FSInputFile(fpath),
                caption=f"🎬 <b>{res['title']}</b> ({i}/{len(tracks)})\n📦 {size_mb:.1f} MB\n\n🎬 <i>Downloaded via @ChatAutomationnBot</i>"
            )
            cleanup(fpath)
            await db.increment_user_downloads(cb.from_user.id, quality="playlist")
        except Exception as e:
            log.warning(f"batch track error: {e}")

    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    sessions.pop(token)


# ============================================================
#                      IMAGE DOWNLOAD FLOW
# ============================================================
@router.callback_query(F.data.startswith("img:dl:"))
async def cb_image_download(cb: CallbackQuery, bot: Bot):
    if await db.is_banned(cb.from_user.id):
        await cb.answer("🚫 Banned.", show_alert=True)
        return

    token = cb.data.split(":")[2]
    session = sessions.get(token)
    if not session or not session.get("image"):
        await cb.answer("⚠️ Session expired. Please send the link again.", show_alert=True)
        return

    url = session["url"]
    title = session.get("title", "Image")

    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(animate_status(
        cb.message, IMAGE_DOWNLOAD_STAGES, interval=2.0, stop_event=stop_event
    ))
    await cb.answer()

    try:
        result = await download_image(url)
    except Exception as e:
        log.exception("image download error")
        await db.record_error()
        stop_event.set()
        anim_task.cancel()
        err_text = friendly_error(e)
        await safe_status_edit(cb.message, err_text, reply_markup=back_kb())
        sessions.pop(token)
        return
    finally:
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (asyncio.CancelledError, Exception):
            pass

    # Album / Carousel
    if result.get("is_album") and result.get("files"):
        files = result["files"]
        try:
            media_group = [
                InputMediaPhoto(media=FSInputFile(f), caption=f"🖼 <b>{title}</b> ({i+1}/{len(files)})\n\n🎬 <i>Downloaded via @ChatAutomationnBot</i>" if i == 0 else "")
                for i, f in enumerate(files)
            ]
            await cb.message.answer_media_group(media=media_group)
            await db.increment_user_downloads(cb.from_user.id, quality="image_album")
            try:
                await cb.message.delete()
            except TelegramBadRequest:
                pass
        except Exception as e:
            log.error("album upload failed: %s", e)
            await safe_status_edit(cb.message, f"❌ Upload failed: <code>{str(e)[:100]}</code>", reply_markup=back_kb())
        finally:
            for f in files:
                cleanup(f)
            sessions.pop(token)
        return

    # Single Image
    filepath = result["file"]
    try:
        await cb.message.answer_photo(
            FSInputFile(filepath),
            caption=f"🖼 <b>{title}</b>\n\n🎬 <i>Downloaded via @ChatAutomationnBot</i>",
        )
        await db.increment_user_downloads(cb.from_user.id, quality="image")
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass
    except TelegramBadRequest as e:
        log.error("image upload failed: %s", e)
        await db.record_error()
        await safe_status_edit(cb.message, f"❌ Upload failed: <code>{str(e)[:120]}</code>", reply_markup=back_kb())
    finally:
        cleanup(filepath)
        sessions.pop(token)


# ============================================================
#                      MEDIA DOWNLOAD FLOW
# ============================================================
@router.callback_query(F.data.startswith("dl:"))
async def cb_download(cb: CallbackQuery, bot: Bot):
    if await db.is_banned(cb.from_user.id):
        await cb.answer("🚫 Banned.", show_alert=True)
        return

    maint = await db.get_maintenance()
    admins = await db.get_admins(HARDCODED_ADMINS)
    if maint and cb.from_user.id not in admins:
        await cb.answer("🛠 Maintenance mode.", show_alert=True)
        return

    if not await require_join(cb, bot, cb.from_user.id):
        return

    parts = cb.data.split(":")
    token = parts[1]
    quality = parts[2]

    await cb.answer()
    await start_video_download(bot, cb.from_user.id, cb.message, token, quality)


async def start_video_download(bot: Bot, user_id: int, message, token: str, quality: str):
    session = sessions.get(token)
    if not session:
        await safe_status_edit(message, "⚠️ <b>Session expired.</b> Send the link again.", reply_markup=back_kb())
        return

    url = session["url"]
    is_vip = await db.is_vip(user_id)
    ad_info = await db.get_ad_settings()

    # Subtitles Extraction Flow
    if quality == "srt":
        lang = await db.get_lang(user_id)
        await safe_status_edit(message, "📝 <b>Extracting subtitles (.srt)...</b>")
        srt_file = await download_subtitles(url, lang=lang)
        if srt_file and os.path.exists(srt_file):
            await message.answer_document(FSInputFile(srt_file), caption=f"📝 <b>Subtitles ({lang.upper()})</b>\n\n🎬 <i>Downloaded via @ChatAutomationnBot</i>")
            cleanup(srt_file)
            await db.increment_user_downloads(user_id, quality="subtitles")
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
        else:
            await safe_status_edit(message, "⚠️ <b>No subtitles found for this video.</b>", reply_markup=back_kb())
        sessions.pop(token)
        return

    # AI Summary Extraction Flow
    if quality == "summary":
        await safe_status_edit(message, "🤖 <b>Generating AI summary & chapters...</b>")
        from downloader import get_video_summary
        sum_info = await get_video_summary(url)
        ch_text = "\n".join(sum_info.get("chapters", [])) or "<i>No chapter markers found in this video.</i>"
        txt = (
            f"🤖 <b>AI VIDEO SUMMARY & CHAPTERS</b>\n\n"
            f"📌 <b>{sum_info['title']}</b>\n\n"
            f"📑 <b>Chapters & Timestamps:</b>\n{ch_text}\n\n"
            f"💡 <b>Description / Highlights:</b>\n{sum_info['description']}\n\n"
            f"🎬 <i>Generated via @{BOT_USERNAME}</i>"
        )
        await safe_status_edit(message, txt, reply_markup=back_kb())
        sessions.pop(token)
        return

    audio_only = (quality in ("audio", "voice", "speed"))
    emoji = QUALITY_EMOJI.get(quality, "📥")
    label = quality.upper()

    position = len(QUEUE) + 1 if (ENABLE_QUEUE and not is_vip) else 1
    queue_msg = "⚡ <b>VIP Priority Queue (Instant)</b>" if is_vip else f"⏳ <i>Queue position: {position}</i>"

    status_text = f"{emoji} <b>Preparing {label}...</b>\n\n{queue_msg}"
    await safe_status_edit(message, status_text)

    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(animate_status(
        message,
        [
            f"{emoji} <b>Preparing {label}...</b>\n<i>Connecting to server...</i>",
            f"{emoji} <b>Starting download...</b>\n<i>Please wait...</i>",
            f"{emoji} <b>Downloading stream...</b>\n<i>Getting ready...</i>",
        ],
        interval=2.5,
        stop_event=stop_event,
    ))

    async def _stop_anim():
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (asyncio.CancelledError, Exception):
            pass

    progress_started = {"v": False}
    start_time = {"t": time.time()}
    samples: deque = deque()
    last_edit = {"t": 0.0, "pct": -1}

    async def _progress(d: dict):
        if not SHOW_PROGRESS:
            return

        now = time.time()
        if not progress_started["v"]:
            progress_started["v"] = True
            stop_event.set()
            anim_task.cancel()
            start_time["t"] = now

        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

        if not samples or samples[-1][1] != done:
            samples.append((now, done))
        while samples and now - samples[0][0] > SPEED_WINDOW:
            samples.popleft()

        if now - last_edit["t"] < PROGRESS_UPDATE_INTERVAL:
            return
        last_edit["t"] = now

        if len(samples) >= 2:
            t0, b0 = samples[0]
            t1, b1 = samples[-1]
            dt = max(0.5, t1 - t0)
            speed_bps = (b1 - b0) / dt
        else:
            elapsed = max(0.5, now - start_time["t"])
            speed_bps = done / elapsed

        if speed_bps <= 0:
            speed_bps = 1.0

        pct = min(100, int(done / total * 100)) if total > 0 else 0
        eta_s = (max(0, total - done) / speed_bps) if (total > 0 and speed_bps > 0) else 0

        if pct == last_edit["pct"] and pct < 100:
            return
        last_edit["pct"] = pct

        bar_len = 10
        filled = int(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s"
        size_str = f"{fmt_size(done)} / {fmt_size(total)}" if total else fmt_size(done)
        eta_str = fmt_time(eta_s) if eta_s else "—"

        txt = (
            f"{emoji} <b>Downloading {label}...</b>\n\n"
            f"<code>[{bar}] {pct}%</code>\n"
            f"⚡ {speed_str}\n"
            f"📦 {size_str}\n"
            f"⏱ ETA: {eta_str}"
        )
        await safe_status_edit(message, txt)

    async def _status_callback(txt: str):
        await safe_status_edit(message, txt)

    result = None
    err = None

    # VIP users bypass the concurrency queue
    if ENABLE_QUEUE and not is_vip:
        QUEUE.append(user_id)
        async with QUEUE_SEM:
            try:
                QUEUE.remove(user_id)
            except ValueError:
                pass
            try:
                await bot.send_chat_action(
                    user_id,
                    ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT
                )
                result = await download_video(
                    url, quality="best" if quality == "best" else ("720" if quality in ("voice", "gif", "speed") else quality),
                    audio_only=audio_only, progress_cb=_progress, status_msg_cb=_status_callback,
                )
            except Exception as e:
                err = e
    else:
        try:
            await bot.send_chat_action(
                user_id,
                ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT
            )
            result = await download_video(
                url, quality="best" if quality == "best" else ("720" if quality in ("voice", "gif", "speed") else quality),
                audio_only=audio_only, progress_cb=_progress, status_msg_cb=_status_callback,
            )
        except Exception as e:
            err = e

    await _stop_anim()

    if err or not result:
        if err:
            log.exception("download error")
        await db.record_error()
        err_text = friendly_error(err) if err else "❌ Download failed"
        await safe_status_edit(message, err_text, reply_markup=back_kb())
        sessions.pop(token)
        return

    title = result["title"][:100]
    uploader = result.get("uploader") or "Universal DL"
    filepath = result["file"]

    # Optional Sponsored Ad inline button
    reply_markup = None
    if ad_info.get("enabled") and ad_info.get("url"):
        ad_kb = InlineKeyboardBuilder()
        ad_kb.button(text=ad_info.get("text") or "📢 Sponsor", url=ad_info.get("url"))
        reply_markup = ad_kb.as_markup()

    lang = await db.get_lang(user_id)
    autodel_on, autodel_mins = await db.get_autodelete_settings()
    autodel_note = t("autodelete_notice", lang, minutes=autodel_mins) if autodel_on else ""
    promo = f"\n\n🎬 <i>Downloaded via @{BOT_USERNAME}</i>{autodel_note}"

    # 0. 9:16 Vertical Reel Delivery
    if quality == "vertical":
        await safe_status_edit(message, "📱 <b>Converting to 9:16 Vertical format...</b>")
        from downloader import convert_to_vertical_916
        vert_path = await convert_to_vertical_916(filepath)
        size_mb = os.path.getsize(vert_path) / (1024 * 1024)
        sent = await message.answer_video(
            FSInputFile(vert_path),
            caption=f"📱 <b>{title} [9:16 Vertical Reel]</b>\n📦 {size_mb:.1f} MB{promo}",
            supports_streaming=True,
            reply_markup=reply_markup
        )
        cleanup(vert_path)
        cleanup(filepath)
        if autodel_on and sent:
            asyncio.create_task(schedule_auto_delete(bot, user_id, [sent.message_id], minutes=autodel_mins))
        await db.increment_user_downloads(user_id, quality="vertical_916")
        sessions.pop(token)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # 1. Voice Note Delivery
    if quality == "voice":
        await safe_status_edit(message, "🎙 <b>Converting to Telegram Voice Note...</b>")
        voice_path = await convert_to_voice(filepath)
        sent = await message.answer_voice(
            FSInputFile(voice_path),
            caption=f"🎙 <b>{title}</b>{promo}",
            reply_markup=reply_markup
        )
        cleanup(voice_path)
        cleanup(filepath)
        if autodel_on and sent:
            asyncio.create_task(schedule_auto_delete(bot, user_id, [sent.message_id], minutes=autodel_mins))
        await db.increment_user_downloads(user_id, quality="voice")
        sessions.pop(token)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # 2. GIF / Animation Delivery
    if quality == "gif":
        await safe_status_edit(message, "🎞 <b>Creating animated GIF loop...</b>")
        gif_path = await convert_to_gif(filepath)
        sent = await message.answer_animation(
            FSInputFile(gif_path),
            caption=f"🎞 <b>{title}</b>{promo}",
            reply_markup=reply_markup
        )
        cleanup(gif_path)
        cleanup(filepath)
        if autodel_on and sent:
            asyncio.create_task(schedule_auto_delete(bot, user_id, [sent.message_id], minutes=autodel_mins))
        await db.increment_user_downloads(user_id, quality="gif")
        sessions.pop(token)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # 3. Audio 1.5x Speed Delivery
    if quality == "speed":
        await safe_status_edit(message, "⚡ <b>Speeding up audio (1.5x)...</b>")
        speed_path = await change_audio_speed(filepath, speed=1.5)
        size_mb = os.path.getsize(speed_path) / (1024 * 1024)
        sent = await message.answer_audio(
            FSInputFile(speed_path),
            title=f"{title} (1.5x)",
            performer=uploader,
            caption=f"⚡ <b>{title} [1.5x Speed]</b>\n📦 {size_mb:.1f} MB{promo}",
            reply_markup=reply_markup
        )
        cleanup(speed_path)
        cleanup(filepath)
        if autodel_on and sent:
            asyncio.create_task(schedule_auto_delete(bot, user_id, [sent.message_id], minutes=autodel_mins))
        await db.increment_user_downloads(user_id, quality="speed_audio")
        sessions.pop(token)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # 4. Multi-part split video delivery
    if result.get("is_split") and result.get("chunks"):
        chunks = result["chunks"]
        total_parts = len(chunks)
        await safe_status_edit(message, f"📤 <b>Uploading {total_parts} parts to Telegram...</b>")
        sent_mids = []
        for i, chunk_path in enumerate(chunks, 1):
            size_mb = os.path.getsize(chunk_path) / (1024 * 1024)
            try:
                media = FSInputFile(chunk_path)
                s_part = await message.answer_video(
                    media,
                    caption=(
                        f"🎬 <b>{title}</b>\n\n"
                        f"✂️ <b>Part {i} of {total_parts}</b> (<code>{size_mb:.1f} MB</code>)\n"
                        f"{emoji} {label}{promo}"
                    ),
                    supports_streaming=True,
                    reply_markup=reply_markup
                )
                if s_part:
                    sent_mids.append(s_part.message_id)
            except Exception as e:
                log.error("Part upload error: %s", e)
            finally:
                cleanup(chunk_path)

        if autodel_on and sent_mids:
            asyncio.create_task(schedule_auto_delete(bot, user_id, sent_mids, minutes=autodel_mins))

        await db.increment_user_downloads(user_id, quality=quality)
        sessions.pop(token)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        return

    # 5. Normal Video or MP3 Delivery
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    compressed_note = ""
    if result.get("compressed"):
        orig = result.get("orig_size_mb", size_mb)
        compressed_note = f"\n💡 <i>Auto-compressed from {orig:.0f} MB to fit Telegram!</i>"

    try:
        media = FSInputFile(filepath)
        sent = None
        if audio_only:
            sent = await message.answer_audio(
                media, title=title, performer=uploader,
                caption=f"🎵 <b>{title}</b>\n\n👤 {uploader}\n📦 {size_mb:.1f} MB{promo}",
                reply_markup=reply_markup
            )
        else:
            sent = await message.answer_video(
                media,
                caption=(
                    f"🎬 <b>{title}</b>\n\n"
                    f"📦 {size_mb:.1f} MB\n"
                    f"{emoji} {label}"
                    f"{compressed_note}{promo}"
                ),
                supports_streaming=True,
                reply_markup=reply_markup
            )

        if autodel_on and sent:
            asyncio.create_task(schedule_auto_delete(bot, user_id, [sent.message_id], minutes=autodel_mins))

        await db.increment_user_downloads(user_id, quality=quality)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    except TelegramBadRequest as e:
        log.error("upload failed: %s", e)
        await db.record_error()
        await safe_status_edit(
            message,
            f"❌ Upload failed: <code>{str(e)[:120]}</code>",
            reply_markup=back_kb()
        )
    finally:
        cleanup(filepath)
        sessions.pop(token)


# ---------- Fallback ----------
@router.message()
async def fallback(msg: Message, bot: Bot):
    if await db.is_banned(msg.from_user.id):
        return
    maint = await db.get_maintenance()
    admins = await db.get_admins(HARDCODED_ADMINS)
    if maint and msg.from_user.id not in admins:
        await msg.reply("🛠 <b>Under maintenance.</b>")
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    await msg.reply(
        "🤔 Send me a <b>video, music, or media link</b> to download!",
        reply_markup=main_menu_kb()
    )


# ============================================================
#                    BACKGROUND TASKS
# ============================================================
async def cleanup_task():
    while True:
        try:
            removed = cleanup_old_files(CLEANUP_INTERVAL, DOWNLOAD_DIR)
            removed_thumbs = cleanup_old_files(CLEANUP_INTERVAL * 6, Path("data/thumbs"))
            sessions.purge_expired()
            if removed or removed_thumbs:
                log.info(f"🧹 Removed {removed} old downloads, {removed_thumbs} old thumbnails")
        except Exception as e:
            log.warning(f"cleanup error: {e}")
        await asyncio.sleep(CLEANUP_INTERVAL)


async def daily_report_task(bot: Bot):
    sent_for = None
    while True:
        try:
            now = datetime.utcnow()
            if now.hour == DAILY_REPORT_HOUR_UTC and sent_for != today_key():
                sent_for = today_key()
                stats = await db.get_stats_summary()
                today = stats.get("today", {})
                top_q = max(
                    stats.get("by_quality", {}).items(),
                    key=lambda kv: kv[1], default=("—", 0)
                )[0]
                await notify_admins(
                    bot,
                    "╔══════════════════════╗\n"
                    "   📊 <b>DAILY REPORT</b>\n"
                    "╚══════════════════════╝\n\n"
                    f"👥 New users: <code>{today.get('users', 0)}</code>\n"
                    f"👑 VIP users: <code>{stats.get('vip_count', 0)}</code>\n"
                    f"📥 Downloads: <code>{today.get('downloads', 0)}</code>\n"
                    f"❌ Errors: <code>{today.get('errors', 0)}</code>\n"
                    f"🏆 Top quality: <b>{top_q}p</b>\n"
                    f"📈 Total users: <code>{stats.get('total_users', 0)}</code>"
                )
        except Exception as e:
            log.warning(f"daily report error: {e}")
        await asyncio.sleep(600)


async def self_ping_task():
    if not PUBLIC_URL:
        return
    ping_url = f"{PUBLIC_URL}/ping"
    await asyncio.sleep(30)
    import urllib.request
    while True:
        try:
            def _go():
                req = urllib.request.Request(ping_url, headers={"User-Agent": "SelfPing/1.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    return r.status
            await asyncio.to_thread(_go)
        except Exception:
            pass
        await asyncio.sleep(180)


# ============================================================
#                          MAIN
# ============================================================
async def set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Start bot"),
        BotCommand(command="search", description="🔍 Search YouTube in chat"),
        BotCommand(command="buyvip", description="⭐ Buy VIP with Telegram Stars"),
        BotCommand(command="ref", description="👑 Invite & get VIP"),
        BotCommand(command="trim", description="✂️ Trim video clip"),
        BotCommand(command="feedback", description="✍️ Send feedback/support"),
        BotCommand(command="settings", description="⚙️ Default quality"),
        BotCommand(command="lang", description="🌐 Change language"),
        BotCommand(command="help", description="📖 How to use"),
    ])


async def set_admin_commands(bot: Bot):
    admins = await db.get_admins(HARDCODED_ADMINS)
    admin_cmds = [
        BotCommand(command="start", description="🏠 Start"),
        BotCommand(command="admin", description="🎛 Admin Dashboard"),
        BotCommand(command="stats", description="📊 Bot statistics"),
        BotCommand(command="backup", description="💾 Send database backup"),
        BotCommand(command="reply", description="💬 Reply to user feedback"),
        BotCommand(command="broadcast", description="📣 Broadcast message"),
        BotCommand(command="setvip", description="👑 Grant/Revoke VIP"),
        BotCommand(command="setad", description="📢 Configure Sponsored Ad"),
        BotCommand(command="autodelete", description="🗑 Configure Auto-delete"),
        BotCommand(command="update_engine", description="🔄 Update yt-dlp core"),
        BotCommand(command="maintenance", description="🛠 Toggle maintenance"),
    ]
    for aid in admins:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            pass


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing in environment")

    await db.init_db()
    threading.Thread(target=start_health_server, daemon=True).start()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await set_commands(bot)
    await set_admin_commands(bot)

    admins = await db.get_admins(HARDCODED_ADMINS)
    total_users = await db.get_total_users_count()

    log.info(f"Admins: {sorted(admins)}")
    log.info(f"Loaded users from SQLite: {total_users}")
    log.info("Universal Downloader Enterprise v3.5 Started Successfully")

    asyncio.create_task(cleanup_task())
    asyncio.create_task(daily_report_task(bot))
    asyncio.create_task(daily_backup_task(bot))
    asyncio.create_task(self_ping_task())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

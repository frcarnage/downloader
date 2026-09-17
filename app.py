import asyncio
import os
import re
import json
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BotCommand
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv

from downloader import download_video, get_info, cleanup, is_supported


# ============================================================
#                        CONFIGURATION
# ============================================================

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# 🔒 Force-join channel — users must join before using the bot
# Make sure the bot is an ADMIN in this channel, or verification will fail.
FORCE_CHANNEL = "-1004300796325"          # private channel ID
FORCE_CHANNEL_USERNAME = "botupdatesor"   # public username (used for join link)

# 👮 Admin Telegram user IDs (hardcoded)
ADMIN_IDS = {8472371058}

# Optional: add more admins via env var (comma-separated)
_admin_env = os.getenv("ADMIN_IDS", "").strip()
if _admin_env:
    for part in _admin_env.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ADMIN_IDS.add(int(part))


# ============================================================
#                    PERSISTENT STORAGE (JSON)
# ============================================================

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
STATS_FILE = DATA_DIR / "stats.json"
ADMINS_FILE = DATA_DIR / "admins.json"


def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"Could not load {path}: {e}")
    return default


def _save_json(path: Path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Could not save {path}: {e}")


USERS: dict = _load_json(USERS_FILE, {})

STATS: dict = _load_json(STATS_FILE, {
    "success": 0,
    "failed": 0,
    "by_quality": {},
})

# Load additional admins added via /addadmin (persisted across restarts)
_admins_raw = _load_json(ADMINS_FILE, [])
for a in _admins_raw:
    try:
        ADMIN_IDS.add(int(a))
    except (ValueError, TypeError):
        pass


def save_users():
    _save_json(USERS_FILE, USERS)


def save_stats():
    _save_json(STATS_FILE, STATS)


def save_admins():
    # Save only the extra admins added at runtime (not the hardcoded ones)
    hardcoded = {8472371058}
    extra = sorted(ADMIN_IDS - hardcoded)
    _save_json(ADMINS_FILE, extra)


def register_user(user) -> bool:
    """Returns True if the user is new."""
    uid = str(user.id)
    if uid not in USERS:
        USERS[uid] = {
            "username": user.username or "",
            "first_name": user.first_name or "",
            "joined": datetime.utcnow().isoformat(),
            "downloads": 0,
        }
        save_users()
        return True
    return False


def increment_user_downloads(user_id: int):
    uid = str(user_id)
    if uid in USERS:
        USERS[uid]["downloads"] = USERS[uid].get("downloads", 0) + 1
        save_users()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ============================================================
#                 HEALTH-CHECK HTTP SERVER
# ============================================================

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info(f"💚 Health server listening on port {port}")
    server.serve_forever()


# ============================================================
#                         BOT SETUP
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

router = Router()
sessions: dict[int, dict] = {}
URL_REGEX = re.compile(r"https?://[^\s]+")

QUALITY_EMOJI = {
    "360": "📱",
    "480": "📺",
    "720": "🎥",
    "1080": "💎",
    "best": "🚀",
    "audio": "🎵",
}


# ============================================================
#                       KEYBOARDS
# ============================================================

def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 How to Use", callback_data="help")
    kb.button(text="⚡ Supported Sites", callback_data="sites")
    kb.button(text="ℹ️ About", callback_data="about")
    kb.adjust(2, 1)
    return kb.as_markup()


def quality_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 360p", callback_data="dl:360")
    kb.button(text="📺 480p", callback_data="dl:480")
    kb.button(text="🎥 720p HD", callback_data="dl:720")
    kb.button(text="💎 1080p Full HD", callback_data="dl:1080")
    kb.button(text="🚀 Best Available", callback_data="dl:best")
    kb.button(text="🎵 Audio Only (MP3)", callback_data="dl:audio")
    kb.button(text="❌ Cancel", callback_data="cancel")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Back to Menu", callback_data="menu")
    return kb.as_markup()


def join_kb() -> InlineKeyboardMarkup:
    """Keyboard prompting the user to join the required channel."""
    kb = InlineKeyboardBuilder()
    if FORCE_CHANNEL_USERNAME:
        kb.button(text="📢 Join Channel", url=f"https://t.me/{FORCE_CHANNEL_USERNAME}")
    kb.button(text="✅ I Joined", callback_data="check_join")
    kb.adjust(1, 1)
    return kb.as_markup()


# ============================================================
#                    CHANNEL MEMBERSHIP
# ============================================================

async def is_user_joined(bot: Bot, user_id: int) -> bool:
    """Check if user is a member of FORCE_CHANNEL. Returns True if no channel configured."""
    if not FORCE_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(FORCE_CHANNEL, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        )
    except Exception as e:
        log.warning(f"get_chat_member failed for {user_id}: {e}")
        # Fail-open: allow user if the bot can't check (e.g. not admin in channel)
        return True


async def require_join(msg_or_cb, bot: Bot, user_id: int) -> bool:
    """Sends join prompt if user hasn't joined. Returns True if user can proceed."""
    if await is_user_joined(bot, user_id):
        return True

    text = (
        "╔══════════════════════╗\n"
        "   🔒 <b>ACCESS LOCKED</b>\n"
        "╚══════════════════════╝\n\n"
        "To use this bot, you must join our official channel first.\n\n"
        "👇 Tap <b>Join Channel</b>, then come back and hit <b>I Joined ✅</b>"
    )

    if isinstance(msg_or_cb, CallbackQuery):
        try:
            await msg_or_cb.message.edit_text(text, reply_markup=join_kb())
        except TelegramBadRequest:
            await msg_or_cb.message.answer(text, reply_markup=join_kb())
    else:
        await msg_or_cb.answer(text, reply_markup=join_kb())
    return False


# ============================================================
#                 ADMIN NOTIFICATIONS
# ============================================================

async def notify_admins(bot: Bot, text: str):
    for admin_id in list(ADMIN_IDS):
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Failed to notify admin {admin_id}: {e}")


# ============================================================
#                        HANDLERS
# ============================================================

@router.message(CommandStart())
async def cmd_start(msg: Message, bot: Bot):
    # Register + notify on new user
    if register_user(msg.from_user):
        asyncio.create_task(notify_admins(
            bot,
            "🆕 <b>NEW USER</b>\n\n"
            f"👤 Name: <b>{msg.from_user.first_name}</b>\n"
            f"🔗 Username: @{msg.from_user.username or '—'}\n"
            f"🆔 ID: <code>{msg.from_user.id}</code>\n"
            f"📊 Total users: <b>{len(USERS)}</b>"
        ))

    # Force-join check
    if not await require_join(msg, bot, msg.from_user.id):
        return

    text = (
        "╔══════════════════════╗\n"
        "   🎬 <b>UNIVERSAL DOWNLOADER</b>\n"
        "╚══════════════════════╝\n\n"
        f"👋 Welcome, <b>{msg.from_user.first_name}</b>!\n\n"
        "Download videos from <b>anywhere</b> — fast, free, without watermarks.\n\n"
        "▫️ <b>YouTube</b> · Videos, Shorts & Music\n"
        "▫️ <b>TikTok</b> · No watermark\n"
        "▫️ <b>Instagram</b> · Reels & Posts\n"
        "▫️ <b>Twitter/X</b> · Facebook · Reddit\n\n"
        "🎯 <b>Just send me a link</b> and pick your quality!\n\n"
        "💡 <i>Use /help for tips and limits</i>"
    )
    await msg.answer(text, reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(msg: Message, bot: Bot):
    if not await require_join(msg, bot, msg.from_user.id):
        return
    text = (
        "╔══════════════════════╗\n"
        "     📖 <b>HOW TO USE</b>\n"
        "╚══════════════════════╝\n\n"
        "<b>1.</b> Copy a video link from any supported app\n"
        "<b>2.</b> Paste it here and hit Send\n"
        "<b>3.</b> Choose your quality:\n"
        "   📱 360p · 📺 480p · 🎥 720p\n"
        "   💎 1080p · 🚀 Best · 🎵 MP3\n"
        "<b>4.</b> Wait a few seconds — done! 🎉\n\n"
        "⚠️ <b>Limits</b>\n"
        "• Max file size: <b>50 MB</b> (Telegram bot limit)\n"
        "• One link per message\n\n"
        "💡 <b>Pro tips</b>\n"
        "• TikTok videos download without watermark\n"
        "• Instagram private posts won't work — only public\n"
        "• Pick <b>720p</b> if 1080p exceeds the 50 MB limit"
    )
    await msg.answer(text, reply_markup=back_kb())


@router.message(Command("sites"))
async def cmd_sites(msg: Message, bot: Bot):
    if not await require_join(msg, bot, msg.from_user.id):
        return
    text = (
        "╔══════════════════════╗\n"
        "   ⚡ <b>SUPPORTED SITES</b>\n"
        "╚══════════════════════╝\n\n"
        "✅ YouTube — Videos, Shorts, Music\n"
        "✅ TikTok — No watermark\n"
        "✅ Instagram — Reels, Posts, IGTV\n"
        "✅ Twitter / X\n"
        "✅ Facebook / FB Watch\n"
        "✅ Reddit\n"
        "✅ Vimeo\n"
        "✅ Dailymotion\n"
        "✅ Pinterest\n\n"
        "<i>Powered by yt-dlp — 1000+ sites supported!</i>"
    )
    await msg.answer(text, reply_markup=back_kb())


# ---------- Admin commands ----------

@router.message(Command("users"))
async def cmd_users(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("🚫 <b>Admin only.</b>")
        return
    await msg.reply(
        f"👥 <b>Total users:</b> <code>{len(USERS)}</code>\n\n"
        "Use /stats for download statistics."
    )


@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("🚫 <b>Admin only.</b>")
        return

    by_q = STATS.get("by_quality", {})
    quality_lines = "\n".join(
        f"   {QUALITY_EMOJI.get(k, '📥')} <b>{k}</b>: {v}"
        for k, v in sorted(by_q.items())
    ) or "   <i>No downloads yet</i>"

    await msg.reply(
        "╔══════════════════════╗\n"
        "     📊 <b>BOT STATISTICS</b>\n"
        "╚══════════════════════╝\n\n"
        f"👥 <b>Users:</b> <code>{len(USERS)}</code>\n"
        f"👮 <b>Admins:</b> <code>{len(ADMIN_IDS)}</code>\n\n"
        f"✅ <b>Successful downloads:</b> <code>{STATS.get('success', 0)}</code>\n"
        f"❌ <b>Failed downloads:</b> <code>{STATS.get('failed', 0)}</code>\n\n"
        "📥 <b>By quality:</b>\n"
        f"{quality_lines}"
    )


@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        await msg.reply("🚫 <b>Admin only.</b>")
        return

    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply(
            "⚠️ <b>Usage:</b> <code>/addadmin &lt;user_id&gt;</code>\n\n"
            "Example: <code>/addadmin 123456789</code>"
        )
        return

    new_id = int(parts[1])
    if new_id in ADMIN_IDS:
        await msg.reply(f"ℹ️ <code>{new_id}</code> is already an admin.")
        return

    ADMIN_IDS.add(new_id)
    save_admins()
    await msg.reply(f"✅ <b>Added admin:</b> <code>{new_id}</code>")

    await notify_admins(
        bot,
        "👮 <b>New admin added</b>\n\n"
        f"🆔 <code>{new_id}</code>\n"
        f"👤 By: <b>{msg.from_user.first_name}</b>"
    )


# ---------- Callback: check join ----------

@router.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery, bot: Bot):
    if await is_user_joined(bot, cb.from_user.id):
        await cb.answer("✅ Verified! You can use the bot now.", show_alert=True)
        await cb.message.edit_text(
            "✅ <b>Access granted!</b>\n\n"
            "Send me a video link to get started 👇",
            reply_markup=main_menu_kb()
        )
    else:
        await cb.answer("❌ You haven't joined yet. Please join the channel first.", show_alert=True)


# ---------- Menu callbacks ----------

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, bot: Bot):
    if not await require_join(cb, bot, cb.from_user.id):
        return
    await cb.message.edit_text(
        "🏠 <b>Main Menu</b>\n\nSend me a video link to start 👇",
        reply_markup=main_menu_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    text = (
        "📖 <b>HOW TO USE</b>\n\n"
        "1️⃣ Copy a video link\n"
        "2️⃣ Paste it here\n"
        "3️⃣ Pick quality\n"
        "4️⃣ Receive your video 🎉\n\n"
        "⚠️ Max size: <b>50 MB</b>"
    )
    await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data == "sites")
async def cb_sites(cb: CallbackQuery):
    text = (
        "⚡ <b>SUPPORTED</b>\n\n"
        "▫️ YouTube / Shorts\n▫️ TikTok\n▫️ Instagram\n"
        "▫️ Twitter / X\n▫️ Facebook\n▫️ Reddit\n"
        "▫️ Vimeo · Dailymotion · Pinterest"
    )
    await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    text = (
        "ℹ️ <b>ABOUT</b>\n\n"
        "🤖 <b>Universal Downloader Bot</b>\n"
        "Fast · Free · No ads\n\n"
        "Powered by <code>yt-dlp</code> + <code>aiogram</code>\n"
        "Made with ❤️"
    )
    await cb.message.edit_text(text, reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    sessions.pop(cb.from_user.id, None)
    await cb.message.edit_text(
        "❌ <b>Cancelled.</b>\n\nSend a new link anytime!",
        reply_markup=back_kb()
    )
    await cb.answer("Cancelled")


# ---------- Link handling ----------

@router.message(F.text.regexp(URL_REGEX))
async def handle_link(msg: Message, bot: Bot):
    if not await require_join(msg, bot, msg.from_user.id):
        return

    match = URL_REGEX.search(msg.text)
    url = match.group(0)

    if not is_supported(url):
        await msg.reply(
            "❌ <b>Unsupported link</b>\n\n"
            "Try YouTube, TikTok, Instagram, Twitter, Facebook or Reddit.",
            reply_markup=back_kb()
        )
        return

    status = await msg.reply("🔍 <b>Analyzing link...</b>")

    try:
        info = await asyncio.wait_for(get_info(url), timeout=30)
    except Exception as e:
        log.exception("info error")
        await status.edit_text(
            f"❌ <b>Couldn't fetch info</b>\n\n<code>{str(e)[:150]}</code>",
            reply_markup=back_kb()
        )
        asyncio.create_task(notify_admins(
            bot,
            "⚠️ <b>INFO FETCH ERROR</b>\n\n"
            f"👤 By: <b>{msg.from_user.first_name}</b> (<code>{msg.from_user.id}</code>)\n"
            f"🔗 URL: <code>{url[:120]}</code>\n"
            f"❗ Error: <code>{str(e)[:180]}</code>"
        ))
        return

    title = (info.get("title") or "Video")[:80]
    duration = info.get("duration") or 0
    mins, secs = divmod(int(duration), 60)
    uploader = (info.get("uploader") or "Unknown")[:40]

    sessions[msg.from_user.id] = {"url": url, "title": title}

    caption = (
        "╔══════════════════════╗\n"
        "   🎬 <b>VIDEO FOUND</b>\n"
        "╚══════════════════════╝\n\n"
        f"📌 <b>Title:</b> {title}\n"
        f"👤 <b>By:</b> {uploader}\n"
        f"⏱ <b>Duration:</b> {mins}:{secs:02d}\n\n"
        "👇 <b>Choose your quality:</b>"
    )
    await status.edit_text(caption, reply_markup=quality_kb())


@router.callback_query(F.data.startswith("dl:"))
async def cb_download(cb: CallbackQuery, bot: Bot):
    if not await require_join(cb, bot, cb.from_user.id):
        return

    quality = cb.data.split(":")[1]
    session = sessions.get(cb.from_user.id)
    if not session:
        await cb.answer("⚠️ Session expired. Send the link again.", show_alert=True)
        return

    url = session["url"]
    audio_only = quality == "audio"
    emoji = QUALITY_EMOJI.get(quality, "📥")
    label = "MP3 Audio" if audio_only else f"{quality}p"

    await cb.message.edit_text(
        f"{emoji} <b>Downloading {label}...</b>\n\n"
        "⏳ <i>Please wait, this may take a moment.</i>",
        reply_markup=None
    )
    await cb.answer()

    try:
        await cb.bot.send_chat_action(
            cb.from_user.id,
            ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT
        )
        result = await download_video(url, quality=quality, audio_only=audio_only)
    except Exception as e:
        log.exception("download error")
        STATS["failed"] = STATS.get("failed", 0) + 1
        save_stats()

        await cb.message.edit_text(
            f"❌ <b>Download failed</b>\n\n<code>{str(e)[:200]}</code>",
            reply_markup=back_kb()
        )
        sessions.pop(cb.from_user.id, None)

        asyncio.create_task(notify_admins(
            bot,
            "❌ <b>DOWNLOAD FAILED</b>\n\n"
            f"👤 User: <b>{cb.from_user.first_name}</b> (<code>{cb.from_user.id}</code>)\n"
            f"🎚 Quality: <b>{label}</b>\n"
            f"🔗 URL: <code>{url[:120]}</code>\n"
            f"❗ Error: <code>{str(e)[:180]}</code>"
        ))
        return

    filepath = result["file"]
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    title = result["title"][:100]

    try:
        await cb.bot.send_chat_action(
            cb.from_user.id,
            ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT
        )
        media = FSInputFile(filepath)

        if audio_only:
            await cb.message.answer_audio(
                media, title=title, performer="Universal DL",
                caption=f"🎵 <b>{title}</b>\n\n📦 {size_mb:.1f} MB"
            )
        else:
            await cb.message.answer_video(
                media,
                caption=(
                    f"🎬 <b>{title}</b>\n\n"
                    f"📦 <b>Size:</b> {size_mb:.1f} MB\n"
                    f"{emoji} <b>Quality:</b> {label}\n\n"
                    "✨ <i>Enjoy! Send another link anytime.</i>"
                ),
                supports_streaming=True,
                width=None, height=None
            )

        # Update stats
        STATS["success"] = STATS.get("success", 0) + 1
        STATS.setdefault("by_quality", {})
        STATS["by_quality"][quality] = STATS["by_quality"].get(quality, 0) + 1
        save_stats()
        increment_user_downloads(cb.from_user.id)

        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass

    except TelegramBadRequest as e:
        log.error("upload failed: %s", e)
        STATS["failed"] = STATS.get("failed", 0) + 1
        save_stats()

        await cb.message.edit_text(
            "❌ <b>Upload failed</b>\n\n"
            "File might be too big for Telegram bot API (max 50 MB).\n"
            "Try a lower quality.\n\n"
            f"<code>{str(e)[:120]}</code>",
            reply_markup=back_kb()
        )
        asyncio.create_task(notify_admins(
            bot,
            "⚠️ <b>UPLOAD FAILED</b>\n\n"
            f"👤 User: <b>{cb.from_user.first_name}</b> (<code>{cb.from_user.id}</code>)\n"
            f"🎚 Quality: <b>{label}</b>\n"
            f"📦 Size: <b>{size_mb:.1f} MB</b>\n"
            f"❗ Error: <code>{str(e)[:180]}</code>"
        ))
    finally:
        cleanup(filepath)
        sessions.pop(cb.from_user.id, None)


# ---------- Fallback ----------

@router.message()
async def fallback(msg: Message, bot: Bot):
    if not await require_join(msg, bot, msg.from_user.id):
        return
    await msg.reply(
        "🤔 <b>I didn't catch a link.</b>\n\n"
        "Send me a <b>video URL</b> (YouTube, TikTok, Instagram, etc.) "
        "and I'll download it for you!",
        reply_markup=main_menu_kb()
    )


# ============================================================
#                          MAIN
# ============================================================

async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🏠 Start"),
        BotCommand(command="help", description="📖 How to use"),
        BotCommand(command="sites", description="⚡ Supported sites"),
    ]
    commands += [
        BotCommand(command="users", description="👥 Total users (admin)"),
        BotCommand(command="stats", description="📊 Bot stats (admin)"),
        BotCommand(command="addadmin", description="👮 Add admin (admin)"),
    ]
    await bot.set_my_commands(commands)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing in env")

    # Start health server (for Koyeb)
    threading.Thread(target=start_health_server, daemon=True).start()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await set_commands(bot)

    log.info(f"👮 Admins: {sorted(ADMIN_IDS) or 'none'}")
    log.info(f"📢 Force-join channel: {FORCE_CHANNEL} (@{FORCE_CHANNEL_USERNAME})")
    log.info(f"👥 Loaded users: {len(USERS)}")
    log.info("🚀 Bot started")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

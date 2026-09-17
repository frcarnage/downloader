import asyncio
import os
import re
import json
import time
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from collections import defaultdict, deque

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

from downloader import (
    download_video, get_info, probe_formats, cleanup, cleanup_old_files,
    is_supported, friendly_error,
)


# ============================================================
#                     CONFIGURATION
# ============================================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

FORCE_CHANNELS = [
    {"id": "-1004300796325", "username": "botupdatesor", "name": "Updates"},
]

ADMIN_IDS = {8472371058}
_admin_env = os.getenv("ADMIN_IDS", "").strip()
if _admin_env:
    for part in _admin_env.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ADMIN_IDS.add(int(part))

SHOW_PROGRESS = True
SHOW_SIZE_BUTTONS = True
ENABLE_QUEUE = True
MAX_CONCURRENT = 2
RATE_LIMIT_PER_MIN = 5
CLEANUP_INTERVAL = 3600
DAILY_REPORT_HOUR_UTC = 0


# ============================================================
#                    PERSISTENT STORAGE
# ============================================================
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
THUMBS_DIR = DATA_DIR / "thumbs"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
STATS_FILE = DATA_DIR / "stats.json"
ADMINS_FILE = DATA_DIR / "admins.json"
BANNED_FILE = DATA_DIR / "banned.json"
STATE_FILE = DATA_DIR / "state.json"


def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"load {path}: {e}")
    return default


def _save_json(path: Path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"save {path}: {e}")


USERS = _load_json(USERS_FILE, {})
STATS = _load_json(STATS_FILE, {
    "success": 0, "failed": 0, "by_quality": {}, "daily": {},
})
BANNED = set(_load_json(BANNED_FILE, []))
STATE = _load_json(STATE_FILE, {"maintenance": False})

_admins_raw = _load_json(ADMINS_FILE, [])
for a in _admins_raw:
    try:
        ADMIN_IDS.add(int(a))
    except (ValueError, TypeError):
        pass


def save_users(): _save_json(USERS_FILE, USERS)
def save_stats(): _save_json(STATS_FILE, STATS)
def save_banned(): _save_json(BANNED_FILE, sorted(BANNED))
def save_state(): _save_json(STATE_FILE, STATE)


def save_admins():
    hardcoded = {8472371058}
    _save_json(ADMINS_FILE, sorted(ADMIN_IDS - hardcoded))


def today_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def register_user(user) -> bool:
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


def is_banned(user_id: int) -> bool:
    return user_id in BANNED


def bump_daily(field: str, amount: int = 1):
    d = STATS.setdefault("daily", {})
    day = today_key()
    entry = d.setdefault(day, {"users": 0, "downloads": 0, "errors": 0})
    entry[field] = entry.get(field, 0) + amount
    if len(d) > 30:
        for k in sorted(d.keys())[:-30]:
            d.pop(k, None)
    save_stats()


# ============================================================
#                   HEALTH-CHECK SERVER
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            payload = {
                "status": "maintenance" if STATE.get("maintenance") else "ok",
                "users": len(USERS),
                "downloads_today": STATS.get("daily", {}).get(today_key(), {}).get("downloads", 0),
                "queue": len(QUEUE) if ENABLE_QUEUE else 0,
                "success_total": STATS.get("success", 0),
                "failed_total": STATS.get("failed", 0),
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_response(500)
            self.end_headers()

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
#                       BOT SETUP
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

router = Router()
sessions: dict[int, dict] = {}
URL_REGEX = re.compile(r"https?://[^\s]+")

QUALITY_EMOJI = {
    "360": "📱", "480": "📺", "720": "🎥",
    "1080": "💎", "best": "🚀", "audio": "🎵",
}

RATE = defaultdict(deque)
QUEUE_SEM = asyncio.Semaphore(MAX_CONCURRENT)
QUEUE: deque = deque()


def rate_ok(user_id: int) -> tuple[bool, int]:
    now = time.time()
    dq = RATE[user_id]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT_PER_MIN:
        return False, int(60 - (now - dq[0])) + 1
    dq.append(now)
    return True, 0


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


def quality_kb(sizes: dict | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    def label(q, name, emoji):
        if SHOW_SIZE_BUTTONS and sizes:
            mb = sizes.get(int(q)) if q.isdigit() else None
            if mb:
                return f"{emoji} {name} · ~{mb:.0f} MB"
        return f"{emoji} {name}"

    kb.button(text=label("360", "360p", "📱"), callback_data="dl:360")
    kb.button(text=label("480", "480p", "📺"), callback_data="dl:480")
    kb.button(text=label("720", "720p", "🎥"), callback_data="dl:720")
    kb.button(text=label("1080", "1080p", "💎"), callback_data="dl:1080")
    kb.button(text="🚀 Best Available", callback_data="dl:best")
    kb.button(text="🎵 Audio Only", callback_data="dl:audio")
    kb.button(text="❌ Cancel", callback_data="cancel")
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
    return True


async def require_join(msg_or_cb, bot: Bot, user_id: int) -> bool:
    if await is_joined_all(bot, user_id):
        return True

    text = (
        "╔══════════════════════╗\n"
        "   🔒 <b>ACCESS LOCKED</b>\n"
        "╚══════════════════════╝\n\n"
        "Join <b>all</b> required channels to use this bot.\n\n"
        "👇 Tap below, then hit <b>I Joined ✅</b>"
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
    for aid in list(ADMIN_IDS):
        try:
            await bot.send_message(aid, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ============================================================
#                 ROTATING STATUS ANIMATION
# ============================================================
async def animate_status(msg, stages: list[str], interval: float = 2.0, stop_event: asyncio.Event = None):
    """Cycle through stages editing the message. Stops when stop_event is set."""
    i = 0
    while stop_event and not stop_event.is_set():
        try:
            await msg.edit_text(stages[i % len(stages)])
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
    "🔍 <b>Link detected...</b>\n<i>Checking source...</i>",
    "🌐 <b>Fetching metadata...</b>\n<i>Almost there...</i>",
    "🎬 <b>Parsing video...</b>\n<i>Getting title & thumbnail...</i>",
    "📊 <b>Reading formats...</b>\n<i>Finding best quality...</i>",
    "✨ <b>Almost done...</b>\n<i>Preparing your options...</i>",
]


# ============================================================
#                        HANDLERS
# ============================================================
@router.message(CommandStart())
async def cmd_start(msg: Message, bot: Bot):
    if is_banned(msg.from_user.id):
        await msg.answer("🚫 <b>You are banned from using this bot.</b>")
        return

    if register_user(msg.from_user):
        bump_daily("users")
        asyncio.create_task(notify_admins(
            bot,
            "🆕 <b>NEW USER</b>\n\n"
            f"👤 <b>{msg.from_user.first_name}</b>\n"
            f"🔗 @{msg.from_user.username or '—'}\n"
            f"🆔 <code>{msg.from_user.id}</code>\n"
            f"👥 Total: <b>{len(USERS)}</b>"
        ))

    if not await require_join(msg, bot, msg.from_user.id):
        return

    text = (
        "╔══════════════════════╗\n"
        "   🎬 <b>UNIVERSAL DOWNLOADER</b>\n"
        "╚══════════════════════╝\n\n"
        f"👋 Welcome, <b>{msg.from_user.first_name}</b>!\n\n"
        "Download videos from <b>anywhere</b> — fast, free, no watermarks.\n\n"
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
    if is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    await msg.answer(
        "╔══════════════════════╗\n"
        "     📖 <b>HOW TO USE</b>\n"
        "╚══════════════════════╝\n\n"
        "<b>1.</b> Copy a video link\n"
        "<b>2.</b> Paste it here\n"
        "<b>3.</b> Pick quality\n"
        "<b>4.</b> Wait — done! 🎉\n\n"
        "⚠️ <b>Limits</b>\n"
        "• Max size: <b>50 MB</b>\n"
        "• One link per message\n"
        "• 5 downloads/min per user\n\n"
        "💡 <i>Pick 720p if 1080p exceeds the 50 MB limit</i>",
        reply_markup=back_kb()
    )


@router.message(Command("sites"))
async def cmd_sites(msg: Message, bot: Bot):
    if is_banned(msg.from_user.id):
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    await msg.answer(
        "╔══════════════════════╗\n"
        "   ⚡ <b>SUPPORTED SITES</b>\n"
        "╚══════════════════════╝\n\n"
        "✅ YouTube · TikTok · Instagram\n"
        "✅ Twitter/X · Facebook · Reddit\n"
        "✅ Vimeo · Dailymotion · Pinterest\n\n"
        "<i>Powered by yt-dlp — 1000+ sites</i>",
        reply_markup=back_kb()
    )


# ---------- Admin commands ----------
@router.message(Command("users"))
async def cmd_users(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.reply(f"👥 <b>Total users:</b> <code>{len(USERS)}</code>")


@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    by_q = STATS.get("by_quality", {})
    quality_lines = "\n".join(
        f"   {QUALITY_EMOJI.get(k, '📥')} <b>{k}</b>: {v}"
        for k, v in sorted(by_q.items())
    ) or "   <i>none</i>"

    today = STATS.get("daily", {}).get(today_key(), {})
    await msg.reply(
        "╔══════════════════════╗\n"
        "     📊 <b>BOT STATISTICS</b>\n"
        "╚══════════════════════╝\n\n"
        f"👥 Users: <code>{len(USERS)}</code>\n"
        f"👮 Admins: <code>{len(ADMIN_IDS)}</code>\n"
        f"🚫 Banned: <code>{len(BANNED)}</code>\n\n"
        f"✅ Success: <code>{STATS.get('success', 0)}</code>\n"
        f"❌ Failed: <code>{STATS.get('failed', 0)}</code>\n\n"
        f"📅 <b>Today</b>\n"
        f"   New users: <code>{today.get('users', 0)}</code>\n"
        f"   Downloads: <code>{today.get('downloads', 0)}</code>\n"
        f"   Errors: <code>{today.get('errors', 0)}</code>\n\n"
        "📥 <b>By quality:</b>\n"
        f"{quality_lines}\n\n"
        f"🛠 Maintenance: <b>{'ON' if STATE.get('maintenance') else 'OFF'}</b>"
    )


@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply("⚠️ Usage: <code>/addadmin &lt;user_id&gt;</code>")
        return
    new_id = int(parts[1])
    if new_id in ADMIN_IDS:
        await msg.reply(f"ℹ️ <code>{new_id}</code> is already admin.")
        return
    ADMIN_IDS.add(new_id)
    save_admins()
    await msg.reply(f"✅ Added admin: <code>{new_id}</code>")
    await notify_admins(bot, f"👮 <b>New admin</b>: <code>{new_id}</code>")


@router.message(Command("user"))
async def cmd_user(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply("⚠️ Usage: <code>/user &lt;user_id&gt;</code>")
        return
    uid = parts[1]
    u = USERS.get(uid)
    if not u:
        await msg.reply(f"❌ User <code>{uid}</code> not found.")
        return
    await msg.reply(
        "╔══════════════════════╗\n"
        "     👤 <b>USER LOOKUP</b>\n"
        "╚══════════════════════╝\n\n"
        f"🆔 <code>{uid}</code>\n"
        f"👤 <b>{u.get('first_name','—')}</b>\n"
        f"🔗 @{u.get('username') or '—'}\n"
        f"📅 Joined: <code>{u.get('joined','—')[:19]}</code>\n"
        f"📥 Downloads: <code>{u.get('downloads',0)}</code>\n"
        f"🚫 Banned: <b>{'yes' if int(uid) in BANNED else 'no'}</b>"
    )


@router.message(Command("ban"))
async def cmd_ban(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply("⚠️ Usage: <code>/ban &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    if uid in ADMIN_IDS:
        await msg.reply("⚠️ Can't ban an admin.")
        return
    BANNED.add(uid)
    save_banned()
    await msg.reply(f"🚫 Banned: <code>{uid}</code>")


@router.message(Command("unban"))
async def cmd_unban(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await msg.reply("⚠️ Usage: <code>/unban &lt;user_id&gt;</code>")
        return
    uid = int(parts[1])
    BANNED.discard(uid)
    save_banned()
    await msg.reply(f"✅ Unbanned: <code>{uid}</code>")


@router.message(Command("broadcast"))
async def cmd_broadcast(msg: Message, bot: Bot):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply("⚠️ Usage: <code>/broadcast &lt;message&gt;</code>")
        return
    text = parts[1]
    status = await msg.reply(f"📣 Broadcasting to {len(USERS)} users...")

    sent, failed = 0, 0
    for i, uid in enumerate(list(USERS.keys())):
        try:
            await bot.send_message(int(uid), text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            failed += 1
        if i % 25 == 0:
            await asyncio.sleep(1)

    await status.edit_text(
        f"✅ <b>Broadcast complete</b>\n\n"
        f"Sent: <code>{sent}</code>\nFailed: <code>{failed}</code>"
    )


@router.message(Command("maintenance"))
async def cmd_maintenance(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await msg.reply("⚠️ Usage: <code>/maintenance on</code> or <code>off</code>")
        return
    STATE["maintenance"] = (parts[1].lower() == "on")
    save_state()
    await msg.reply(f"🛠 Maintenance: <b>{'ON' if STATE['maintenance'] else 'OFF'}</b>")


@router.message(Command("admin_help"))
async def cmd_admin_help(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.reply(
        "╔══════════════════════╗\n"
        "     👮 <b>ADMIN COMMANDS</b>\n"
        "╚══════════════════════╝\n\n"
        "/stats — 📊 Bot statistics\n"
        "/users — 👥 Total user count\n"
        "/user &lt;id&gt; — 🔍 Look up a user\n"
        "/addadmin &lt;id&gt; — 👮 Add admin\n"
        "/ban &lt;id&gt; — 🚫 Ban a user\n"
        "/unban &lt;id&gt; — ✅ Unban a user\n"
        "/broadcast &lt;msg&gt; — 📣 Send to all\n"
        "/maintenance on|off — 🛠 Toggle maintenance"
    )


# ---------- Callbacks ----------
@router.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery, bot: Bot):
    if await is_joined_all(bot, cb.from_user.id):
        await cb.answer("✅ Verified!", show_alert=True)
        await cb.message.edit_text(
            "✅ <b>Access granted!</b>\n\nSend me a video link 👇",
            reply_markup=main_menu_kb()
        )
    else:
        await cb.answer("❌ Join all channels first.", show_alert=True)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, bot: Bot):
    if not await require_join(cb, bot, cb.from_user.id):
        return
    await cb.message.edit_text(
        "🏠 <b>Main Menu</b>\n\nSend me a link 👇",
        reply_markup=main_menu_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.message.edit_text(
        "📖 <b>HOW TO USE</b>\n\n1️⃣ Copy link\n2️⃣ Paste\n3️⃣ Pick quality\n4️⃣ Get video 🎉",
        reply_markup=back_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "sites")
async def cb_sites(cb: CallbackQuery):
    await cb.message.edit_text(
        "⚡ <b>SUPPORTED</b>\n\n▫️ YouTube · TikTok\n▫️ Instagram · Twitter\n▫️ Facebook · Reddit",
        reply_markup=back_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "about")
async def cb_about(cb: CallbackQuery):
    await cb.message.edit_text(
        "ℹ️ <b>Universal Downloader</b>\n\nFast · Free · No ads\nPowered by yt-dlp",
        reply_markup=back_kb()
    )
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    sessions.pop(cb.from_user.id, None)
    await cb.message.edit_text("❌ <b>Cancelled.</b>", reply_markup=back_kb())
    await cb.answer("Cancelled")


# ---------- Link handling ----------
@router.message(F.text.regexp(URL_REGEX))
async def handle_link(msg: Message, bot: Bot):
    if is_banned(msg.from_user.id):
        await msg.reply("🚫 <b>You are banned.</b>")
        return

    if STATE.get("maintenance") and not is_admin(msg.from_user.id):
        await msg.reply("🛠 <b>Bot under maintenance.</b> Please try again later.")
        return

    if not await require_join(msg, bot, msg.from_user.id):
        return

    allowed, wait = rate_ok(msg.from_user.id)
    if not allowed:
        await msg.reply(f"⏳ <b>Slow down!</b> Try again in <b>{wait}s</b>.")
        return

    match = URL_REGEX.search(msg.text)
    url = match.group(0)

    if not is_supported(url):
        await msg.reply("❌ <b>Unsupported link.</b>", reply_markup=back_kb())
        return

    status = await msg.reply("🔍 <b>Analyzing link...</b>")

    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(animate_status(
        status, ANALYZE_STAGES, interval=2.0, stop_event=stop_event
    ))

    try:
        info = await asyncio.wait_for(get_info(url), timeout=45)
    except Exception as e:
        log.exception("info error")
        bump_daily("errors")
        stop_event.set()
        anim_task.cancel()
        try:
            await status.edit_text(friendly_error(e), reply_markup=back_kb())
        except TelegramBadRequest:
            await msg.reply(friendly_error(e), reply_markup=back_kb())
        asyncio.create_task(notify_admins(
            bot,
            f"⚠️ <b>INFO ERROR</b>\n\n"
            f"👤 {msg.from_user.first_name} (<code>{msg.from_user.id}</code>)\n"
            f"🔗 <code>{url[:100]}</code>\n"
            f"❗ <code>{str(e)[:180]}</code>"
        ))
        return
    finally:
        stop_event.set()
        anim_task.cancel()
        try:
            await anim_task
        except (asyncio.CancelledError, Exception):
            pass

    title = (info.get("title") or "Video")[:80]
    duration = info.get("duration") or 0
    mins, secs = divmod(int(duration), 60)
    uploader = (info.get("uploader") or "Unknown")[:40]
    video_id = info.get("id") or "v"
    thumb_url = info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url")

    sizes = {}
    if SHOW_SIZE_BUTTONS:
        try:
            sizes = await asyncio.wait_for(probe_formats(url), timeout=20)
        except Exception:
            sizes = {}

    sessions[msg.from_user.id] = {"url": url, "title": title, "video_id": video_id}

    caption = (
        "╔══════════════════════╗\n"
        "   🎬 <b>VIDEO FOUND</b>\n"
        "╚══════════════════════╝\n\n"
        f"📌 <b>{title}</b>\n"
        f"👤 {uploader}\n"
        f"⏱ {mins}:{secs:02d}\n\n"
        "👇 <b>Choose quality:</b>"
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
                reply_markup=quality_kb(sizes),
            )
            return
        except TelegramBadRequest:
            pass
    await msg.answer(caption, reply_markup=quality_kb(sizes))


async def _fetch_thumb(url: str, vid: str) -> str | None:
    try:
        dest = THUMBS_DIR / f"{vid}.jpg"
        if dest.exists() and dest.stat().st_size > 0:
            return str(dest)

        def _go():
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r, open(dest, "wb") as f:
                f.write(r.read())

        await asyncio.to_thread(_go)
        return str(dest) if dest.exists() else None
    except Exception:
        return None


# ---------- Download ----------
@router.callback_query(F.data.startswith("dl:"))
async def cb_download(cb: CallbackQuery, bot: Bot):
    if is_banned(cb.from_user.id):
        await cb.answer("🚫 Banned.", show_alert=True)
        return

    if STATE.get("maintenance") and not is_admin(cb.from_user.id):
        await cb.answer("🛠 Maintenance mode.", show_alert=True)
        return

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
    label = "MP3" if audio_only else f"{quality}p"

    position = len(QUEUE) + 1 if ENABLE_QUEUE else 1
    status_text = (
        f"{emoji} <b>Preparing {label}...</b>\n\n"
        f"⏳ <i>Queue position: {position}</i>"
    )

    try:
        await cb.message.edit_caption(caption=status_text)
    except TelegramBadRequest:
        try:
            await cb.message.edit_text(status_text)
        except TelegramBadRequest:
            pass
    await cb.answer()

    stop_event = asyncio.Event()
    anim_task = asyncio.create_task(animate_status(
        cb.message,
        [
            f"{emoji} <b>Preparing {label}...</b>\n<i>Connecting to server...</i>",
            f"{emoji} <b>Starting download...</b>\n<i>Please wait...</i>",
            f"{emoji} <b>Initializing...</b>\n<i>Getting ready...</i>",
        ],
        interval=2.5,
        stop_event=stop_event,
    ))

    start_time = {"t": time.time()}
    last_pct = {"v": -1, "t": 0.0}
    progress_started = {"v": False}

    async def _progress(d: dict):
        if not SHOW_PROGRESS:
            return

        # Kill the animation on first real progress
        if not progress_started["v"]:
            progress_started["v"] = True
            stop_event.set()
            anim_task.cancel()
            start_time["t"] = time.time()

        now = time.time()
        if now - last_pct["t"] < 1.0:
            return
        last_pct["t"] = now

        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        pct = int(done / total * 100) if total > 0 else 0

        elapsed = max(0.1, now - start_time["t"])
        speed_bps = done / elapsed
        speed_mb = speed_bps / (1024 * 1024)
        speed_str = f"{speed_mb:.1f} MB/s"

        remaining = max(0, total - done)
        eta_s = int(remaining / speed_bps) if speed_bps > 0 else 0
        eta_str = f"{eta_s}s" if eta_s < 60 else f"{eta_s // 60}m {eta_s % 60}s"

        if pct == last_pct["v"] and pct < 100:
            return
        last_pct["v"] = pct

        bar_len = 10
        filled = int(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        txt = (
            f"{emoji} <b>Downloading {label}...</b>\n\n"
            f"<code>[{bar}] {pct}%</code>\n"
            f"⚡ {speed_str}  ·  ⏱ ETA {eta_str}"
        )
        try:
            await cb.message.edit_caption(caption=txt)
        except TelegramBadRequest:
            try:
                await cb.message.edit_text(txt)
            except TelegramBadRequest:
                pass

    if ENABLE_QUEUE:
        if position > MAX_CONCURRENT:
            QUEUE.append(cb.from_user.id)
        async with QUEUE_SEM:
            if cb.from_user.id in QUEUE:
                try:
                    QUEUE.remove(cb.from_user.id)
                except ValueError:
                    pass
            try:
                await cb.bot.send_chat_action(
                    cb.from_user.id,
                    ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT
                )
                result = await download_video(
                    url, quality=quality, audio_only=audio_only, progress_cb=_progress
                )
            except Exception as e:
                log.exception("download error")
                STATS["failed"] = STATS.get("failed", 0) + 1
                bump_daily("errors")
                save_stats()
                stop_event.set()
                anim_task.cancel()
                err_text = friendly_error(e)
                try:
                    await cb.message.edit_caption(caption=err_text, reply_markup=back_kb())
                except TelegramBadRequest:
                    try:
                        await cb.message.edit_text(err_text, reply_markup=back_kb())
                    except TelegramBadRequest:
                        await cb.message.answer(err_text, reply_markup=back_kb())
                sessions.pop(cb.from_user.id, None)
                asyncio.create_task(notify_admins(
                    bot,
                    f"❌ <b>DOWNLOAD FAILED</b>\n\n"
                    f"👤 {cb.from_user.first_name} (<code>{cb.from_user.id}</code>)\n"
                    f"🎚 {label}\n"
                    f"🔗 <code>{url[:100]}</code>\n"
                    f"❗ <code>{str(e)[:180]}</code>"
                ))
                return
    else:
        try:
            await cb.bot.send_chat_action(
                cb.from_user.id,
                ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT
            )
            result = await download_video(
                url, quality=quality, audio_only=audio_only, progress_cb=_progress
            )
        except Exception as e:
            log.exception("download error")
            STATS["failed"] = STATS.get("failed", 0) + 1
            bump_daily("errors")
            save_stats()
            stop_event.set()
            anim_task.cancel()
            err_text = friendly_error(e)
            try:
                await cb.message.edit_caption(caption=err_text, reply_markup=back_kb())
            except TelegramBadRequest:
                try:
                    await cb.message.edit_text(err_text, reply_markup=back_kb())
                except TelegramBadRequest:
                    await cb.message.answer(err_text, reply_markup=back_kb())
            sessions.pop(cb.from_user.id, None)
            return

    stop_event.set()
    anim_task.cancel()

    filepath = result["file"]
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    title = result["title"][:100]

    try:
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
                    f"📦 {size_mb:.1f} MB\n"
                    f"{emoji} {label}\n\n"
                    "✨ <i>Enjoy!</i>"
                ),
                supports_streaming=True,
            )

        STATS["success"] = STATS.get("success", 0) + 1
        STATS.setdefault("by_quality", {})
        STATS["by_quality"][quality] = STATS["by_quality"].get(quality, 0) + 1
        save_stats()
        bump_daily("downloads")
        increment_user_downloads(cb.from_user.id)

        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass

    except TelegramBadRequest as e:
        log.error("upload failed: %s", e)
        STATS["failed"] = STATS.get("failed", 0) + 1
        bump_daily("errors")
        save_stats()
        try:
            await cb.message.edit_caption(
                caption=f"❌ Upload failed: <code>{str(e)[:120]}</code>",
                reply_markup=back_kb()
            )
        except TelegramBadRequest:
            pass
    finally:
        cleanup(filepath)
        sessions.pop(cb.from_user.id, None)


# ---------- Fallback ----------
@router.message()
async def fallback(msg: Message, bot: Bot):
    if is_banned(msg.from_user.id):
        return
    if STATE.get("maintenance") and not is_admin(msg.from_user.id):
        await msg.reply("🛠 <b>Under maintenance.</b>")
        return
    if not await require_join(msg, bot, msg.from_user.id):
        return
    await msg.reply(
        "🤔 Send me a <b>video link</b> to download!",
        reply_markup=main_menu_kb()
    )


# ============================================================
#                    BACKGROUND TASKS
# ============================================================
async def cleanup_task():
    while True:
        try:
            removed = cleanup_old_files(CLEANUP_INTERVAL)
            if removed:
                log.info(f"🧹 Removed {removed} old files")
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
                today = STATS.get("daily", {}).get(today_key(), {})
                top_q = max(
                    STATS.get("by_quality", {}).items(),
                    key=lambda kv: kv[1], default=("—", 0)
                )[0]
                await notify_admins(
                    bot,
                    "╔══════════════════════╗\n"
                    "   📊 <b>DAILY REPORT</b>\n"
                    "╚══════════════════════╝\n\n"
                    f"👥 New users: <code>{today.get('users', 0)}</code>\n"
                    f"📥 Downloads: <code>{today.get('downloads', 0)}</code>\n"
                    f"❌ Errors: <code>{today.get('errors', 0)}</code>\n"
                    f"🏆 Top quality: <b>{top_q}p</b>\n"
                    f"📈 Total users: <code>{len(USERS)}</code>"
                )
        except Exception as e:
            log.warning(f"daily report error: {e}")
        await asyncio.sleep(600)


# ============================================================
#                          MAIN
# ============================================================
async def set_commands(bot: Bot):
    """Register commands in the bot's / menu."""
    # Default visible to everyone
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Start"),
        BotCommand(command="help", description="📖 How to use"),
        BotCommand(command="sites", description="⚡ Supported sites"),
    ])


async def set_admin_commands(bot: Bot):
    """Set the full admin command menu for each admin."""
    from aiogram.types import BotCommandScopeChat
    admin_cmds = [
        BotCommand(command="start", description="🏠 Start"),
        BotCommand(command="help", description="📖 How to use"),
        BotCommand(command="sites", description="⚡ Supported sites"),
        BotCommand(command="admin_help", description="👮 Admin commands list"),
        BotCommand(command="stats", description="📊 Bot statistics"),
        BotCommand(command="users", description="👥 Total user count"),
        BotCommand(command="user", description="🔍 Look up user by ID"),
        BotCommand(command="addadmin", description="👮 Add a new admin"),
        BotCommand(command="ban", description="🚫 Ban a user"),
        BotCommand(command="unban", description="✅ Unban a user"),
        BotCommand(command="broadcast", description="📣 Broadcast to all users"),
        BotCommand(command="maintenance", description="🛠 Toggle maintenance mode"),
    ]
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
        except Exception as e:
            log.warning(f"Couldn't set admin commands for {aid}: {e}")


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing")

    threading.Thread(target=start_health_server, daemon=True).start()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await set_commands(bot)
    await set_admin_commands(bot)

    log.info(f"👮 Admins: {sorted(ADMIN_IDS)}")
    log.info(f"📢 Force channels: {[c['username'] for c in FORCE_CHANNELS]}")
    log.info(f"👥 Loaded users: {len(USERS)}")
    log.info("🚀 Bot started")

    asyncio.create_task(cleanup_task())
    asyncio.create_task(daily_report_task(bot))

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

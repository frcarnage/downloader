import asyncio
import os
import re
import logging
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BotCommand
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

from downloader import download_video, get_info, cleanup, is_supported

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

router = Router()

# In-memory session store: user_id -> {'url': ..., 'title': ...}
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


# ---------- Keyboards ----------

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


# ---------- Handlers ----------

@router.message(CommandStart())
async def cmd_start(msg: Message):
    text = (
        "╔══════════════════════╗\n"
        "   🎬 <b>UNIVERSAL DOWNLOADER</b>\n"
        "╚══════════════════════╝\n\n"
        "👋 Hey <b>{name}</b>!\n\n"
        "I can download videos from:\n"
        "▫️ <b>YouTube</b> · Shorts & Videos\n"
        "▫️ <b>TikTok</b> · No watermark\n"
        "▫️ <b>Instagram</b> · Reels & Posts\n"
        "▫️ <b>Twitter/X</b> · Facebook · Reddit\n\n"
        "🎯 <b>Just send me a link</b> and pick your quality!\n\n"
        "💡 <i>Tip: Use /help for more info</i>"
    ).format(name=msg.from_user.first_name)
    await msg.answer(text, reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(msg: Message):
    text = (
        "📖 <b>HOW TO USE</b>\n\n"
        "<b>1.</b> Copy a video link from any supported app\n"
        "<b>2.</b> Paste it here and hit Send\n"
        "<b>3.</b> Choose your quality:\n"
        "   📱 360p · 📺 480p · 🎥 720p\n"
        "   💎 1080p · 🚀 Best · 🎵 MP3\n"
        "<b>4.</b> Wait a few seconds — done! 🎉\n\n"
        "<b>⚠️ Limits:</b>\n"
        "• Max file size: <b>50 MB</b> (Telegram bot limit)\n"
        "• One link per message\n\n"
        "<b>💡 Pro tips:</b>\n"
        "• TikTok videos download without watermark\n"
        "• For Instagram private posts, only public links work\n"
        "• Pick <b>720p</b> if 1080p exceeds the 50 MB limit"
    )
    await msg.answer(text, reply_markup=back_kb())


@router.message(Command("sites"))
async def cmd_sites(msg: Message):
    text = (
        "⚡ <b>SUPPORTED SITES</b>\n\n"
        "✅ YouTube (Videos, Shorts, Music)\n"
        "✅ TikTok (No watermark)\n"
        "✅ Instagram (Reels, Posts, IGTV)\n"
        "✅ Twitter / X\n"
        "✅ Facebook / FB Watch\n"
        "✅ Reddit\n"
        "✅ Vimeo\n"
        "✅ Dailymotion\n"
        "✅ Pinterest\n\n"
        "<i>Powered by yt-dlp — 1000+ sites supported!</i>"
    )
    await msg.answer(text, reply_markup=back_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
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
        "⚠️ Max size: 50 MB"
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
    await cb.message.edit_text("❌ <b>Cancelled.</b>\n\nSend a new link anytime!", reply_markup=back_kb())
    await cb.answer("Cancelled")


# ---------- Link handling ----------

@router.message(F.text.regexp(URL_REGEX))
async def handle_link(msg: Message):
    match = URL_REGEX.search(msg.text)
    url = match.group(0)

    if not is_supported(url):
        await msg.reply(
            "❌ <b>Unsupported link</b>\n\nTry YouTube, TikTok, Instagram, Twitter, Facebook or Reddit.",
            reply_markup=back_kb()
        )
        return

    status = await msg.reply("🔍 <b>Analyzing link...</b>")

    try:
        info = await asyncio.wait_for(get_info(url), timeout=30)
    except Exception as e:
        log.exception("info error")
        await status.edit_text(f"❌ <b>Couldn't fetch info</b>\n\n<code>{str(e)[:150]}</code>", reply_markup=back_kb())
        return

    title = (info.get("title") or "Video")[:80]
    duration = info.get("duration") or 0
    mins, secs = divmod(int(duration), 60)
    uploader = (info.get("uploader") or "Unknown")[:40]

    sessions[msg.from_user.id] = {"url": url, "title": title}

    caption = (
        "🎬 <b>VIDEO FOUND</b>\n\n"
        f"📌 <b>Title:</b> {title}\n"
        f"👤 <b>By:</b> {uploader}\n"
        f"⏱ <b>Duration:</b> {mins}:{secs:02d}\n\n"
        "👇 <b>Choose quality:</b>"
    )
    await status.edit_text(caption, reply_markup=quality_kb())


@router.callback_query(F.data.startswith("dl:"))
async def cb_download(cb: CallbackQuery):
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
        await cb.bot.send_chat_action(cb.from_user.id, ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT)
        result = await download_video(url, quality=quality, audio_only=audio_only)
    except Exception as e:
        log.exception("download error")
        await cb.message.edit_text(
            f"❌ <b>Download failed</b>\n\n<code>{str(e)[:200]}</code>",
            reply_markup=back_kb()
        )
        sessions.pop(cb.from_user.id, None)
        return

    filepath = result["file"]
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    title = result["title"][:100]

    try:
        await cb.bot.send_chat_action(cb.from_user.id, ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_DOCUMENT)
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

        # Cleanup the "downloading" status message
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass

    except TelegramBadRequest as e:
        log.error("upload failed: %s", e)
        await cb.message.edit_text(
            f"❌ <b>Upload failed</b>\n\nFile might be too big for Telegram bot API (max 50 MB).\n"
            f"Try lower quality.\n\n<code>{str(e)[:120]}</code>",
            reply_markup=back_kb()
        )
    finally:
        cleanup(filepath)
        sessions.pop(cb.from_user.id, None)


# ---------- Fallback ----------

@router.message()
async def fallback(msg: Message):
    await msg.reply(
        "🤔 <b>Hmm, I didn't get a link.</b>\n\n"
        "Send me a <b>video URL</b> (YouTube, TikTok, Instagram, etc.) and I'll download it for you!",
        reply_markup=main_menu_kb()
    )


# ---------- Main ----------

async def set_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="🏠 Start"),
        BotCommand(command="help", description="📖 How to use"),
        BotCommand(command="sites", description="⚡ Supported sites"),
    ])


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN missing in .env")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await set_commands(bot)
    log.info("🚀 Bot started")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

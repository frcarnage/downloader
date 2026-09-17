import os
import asyncio
import yt_dlp
from pathlib import Path

import imageio_ffmpeg

# ------------------------------------------------------------
# FFmpeg setup (via imageio-ffmpeg — no system install needed)
# ------------------------------------------------------------
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
print(f"🎞 FFmpeg binary: {FFMPEG_PATH}", flush=True)

# ------------------------------------------------------------
# Cookies setup (cookies.txt at repo root avoids YouTube bot check)
# ------------------------------------------------------------
COOKIES_PATH = Path("cookies.txt")
if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
    print(f"🍪 Cookies loaded: {COOKIES_PATH}", flush=True)
else:
    COOKIES_PATH = None
    print("🍪 Cookies: not found (may hit YouTube bot checks)", flush=True)

# ------------------------------------------------------------
# Directories
# ------------------------------------------------------------
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ------------------------------------------------------------
# YouTube extractor fallback — try multiple clients to bypass
# the "Sign in to confirm you're not a bot" gate when no cookies
# are provided. Cookies take priority when present.
# ------------------------------------------------------------
YOUTUBE_EXTRACTOR_ARGS = {
    "youtube": {
        "player_client": ["android", "ios", "web_safari", "tv_embedded"],
    },
}


def _progress_hook(d, loop=None, status_msg=None):
    if d["status"] == "finished":
        pass


def _base_opts() -> dict:
    """Common yt-dlp options shared by get_info and download_video."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
        },
    }
    if COOKIES_PATH:
        opts["cookiefile"] = str(COOKIES_PATH)
    return opts


async def get_info(url: str) -> dict:
    """Fetch metadata without downloading."""
    def _extract():
        opts = _base_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await asyncio.to_thread(_extract)


async def download_video(url: str, quality: str = "1080", audio_only: bool = False) -> dict:
    """
    Download video with selected quality.
    quality: '360', '480', '720', '1080', 'best'
    Returns: {'file': path, 'title': str, 'thumbnail': url, 'duration': int}
    """
    outtmpl = str(DOWNLOAD_DIR / "%(id)s_%(height)s.%(ext)s")

    if audio_only:
        fmt = "bestaudio/best"
    elif quality == "best":
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    else:
        h = quality
        fmt = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
        )

    ydl_opts = _base_opts()
    ydl_opts.update({
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_PATH,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 5,
        "writethumbnail": False,
        "postprocessors": (
            [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            if audio_only else []
        ),
    })

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if audio_only:
                filepath = os.path.splitext(filepath)[0] + ".mp3"
            elif not os.path.exists(filepath):
                # merged output mp4
                base = os.path.splitext(filepath)[0]
                if os.path.exists(base + ".mp4"):
                    filepath = base + ".mp4"
            return {
                "file": filepath,
                "title": info.get("title", "video"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration", 0),
            }

    result = await asyncio.to_thread(_download)

    # Safety: check size < 50 MB (Telegram bot API limit for upload)
    size_mb = os.path.getsize(result["file"]) / (1024 * 1024)
    if size_mb > 50:
        raise ValueError(f"File too large ({size_mb:.1f} MB). Telegram bot limit is 50 MB.")
    return result


def cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def is_supported(url: str) -> bool:
    supported = ["youtube.com", "youtu.be", "tiktok.com", "instagram.com",
                 "twitter.com", "x.com", "facebook.com", "fb.watch",
                 "reddit.com", "vimeo.com", "dailymotion.com", "pinterest."]
    return any(s in url.lower() for s in supported)

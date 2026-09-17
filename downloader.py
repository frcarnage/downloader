import os
import asyncio
import yt_dlp
from pathlib import Path

import imageio_ffmpeg

# Resolve FFmpeg binary that ships with imageio-ffmpeg (works without system ffmpeg)
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
print(f"🎞 FFmpeg binary: {FFMPEG_PATH}", flush=True)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def _progress_hook(d, loop, status_msg=None):
    if d["status"] == "finished":
        pass


async def get_info(url: str) -> dict:
    """Fetch metadata without downloading."""
    def _extract():
        opts = {"quiet": True, "skip_download": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await asyncio.to_thread(_extract)


async def download_video(url: str, quality: str = "1080", audio_only: bool = False) -> dict:
    """
    Download video with selected quality.
    quality: '360', '480', '720', '1080', 'best'
    Returns: {'file': path, 'title': str, 'thumbnail': url}
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

    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_PATH,          # ← magic line for FFmpeg
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 5,
        "writethumbnail": False,
        "postprocessors": (
            [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            if audio_only else []
        ),
    }

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

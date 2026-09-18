import os
import time
import asyncio
import yt_dlp
from pathlib import Path

import imageio_ffmpeg

# ------------------------------------------------------------
# FFmpeg
# ------------------------------------------------------------
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
print(f"🎞 FFmpeg binary: {FFMPEG_PATH}", flush=True)

# ------------------------------------------------------------
# Cookies
# ------------------------------------------------------------
COOKIES_PATH = Path("cookies.txt")
if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
    print(f"🍪 Cookies loaded: {COOKIES_PATH}", flush=True)
else:
    COOKIES_PATH = None
    print("🍪 Cookies: not found", flush=True)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


# ------------------------------------------------------------
# yt-dlp option builders
# ------------------------------------------------------------
def _base_opts(fast: bool = False) -> dict:
    """
    fast=True  → fewer clients, quicker failure (used on retry)
    fast=False → full client list (default)
    """
    if fast:
        clients = ["web_safari", "web"]
    else:
        clients = ["web_safari", "web", "android", "ios", "tv"]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 15,
        "ignore_no_formats_error": True,
        "extractor_args": {
            "youtube": {
                "player_client": clients,
            },
        },
        # Give up on hopeless URLs fast
        "extractor_retries": 1,
        "file_access_retries": 1,
    }
    if COOKIES_PATH:
        opts["cookiefile"] = str(COOKIES_PATH)
    return opts


def friendly_error(e: Exception) -> str:
    msg = str(e).lower()

    if isinstance(e, asyncio.TimeoutError) or "timeout" in msg:
        return "⏱️ Request timed out. The source is slow right now — please try again in a moment."
    if "sign in to confirm" in msg or "not a bot" in msg:
        return "🤖 YouTube blocked this request. Try another link or wait a moment."
    if "page needs to be reloaded" in msg:
        return "🔄 YouTube needs a reload. Please try again in a few seconds."
    if "requested format is not available" in msg:
        return "⚠️ No compatible format found. Try a different quality."
    if "video unavailable" in msg or "private" in msg or "removed" in msg or "deleted" in msg:
        return "🚫 This video is private, deleted, or unavailable."
    if "too large" in msg or "50 mb" in msg:
        return "📦 File too big (max 50 MB). Try a lower quality."
    if "unable to download" in msg:
        return "❌ Couldn't download this video. It may be region-locked."
    if "unsupported url" in msg or "no video" in msg:
        return "🔗 Unsupported link. Try a different URL."
    if "geo" in msg and "block" in msg:
        return "🌍 This video is geo-blocked in our region."
    if "cancelled" in msg:
        return "⏱️ Request cancelled. Please try again."
    return f"❌ Error: {str(e)[:180]}"


def build_format(quality: str, audio_only: bool = False) -> str:
    """Robust format string — always falls back to /best."""
    if audio_only:
        return "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"

    if quality == "best":
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/best"
        )

    try:
        h = int(quality)
    except ValueError:
        h = 720

    return (
        f"best[height<={h}][ext=mp4][vcodec!=none][acodec!=none]/"
        f"best[height<={h}][ext=mp4]/"
        f"best[height<={h}][vcodec!=none][acodec!=none]/"
        f"best[height<={h}]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[ext=mp4]/best"
    )


# ------------------------------------------------------------
# Info / probe / download
# ------------------------------------------------------------
async def get_info(url: str) -> dict:
    """Fetch metadata. Tries full client list first, then fast fallback."""

    def _extract(fast: bool):
        opts = _base_opts(fast=fast)
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    # Attempt 1 — full client list, 35s timeout
    try:
        return await asyncio.wait_for(asyncio.to_thread(_extract, False), timeout=35)
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass

    # Attempt 2 — faster fallback with fewer clients, 25s
    return await asyncio.wait_for(asyncio.to_thread(_extract, True), timeout=25)


def is_image_only(info: dict) -> bool:
    if not info:
        return False
    ext = (info.get("ext") or "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif"):
        return True
    formats = info.get("formats") or []
    has_video = any((f.get("vcodec") and f["vcodec"] != "none") for f in formats)
    if not has_video and info.get("thumbnail"):
        return True
    return False


async def probe_formats(url: str) -> dict:
    """Return {height: estimated_mb}. Fails silently — returns {} on error."""

    def _probe(fast: bool):
        opts = _base_opts(fast=fast)
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_probe, True), timeout=20)
    except Exception:
        return {}

    formats = info.get("formats") or []
    best: dict[int, tuple[float, bool]] = {}

    for f in formats:
        h = f.get("height")
        if not h:
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if not size:
            continue
        size_mb = size / (1024 * 1024)
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        progressive = vcodec != "none" and acodec != "none"

        if h not in best:
            best[h] = (size_mb, progressive)
        else:
            old_mb, old_prog = best[h]
            if progressive and not old_prog:
                best[h] = (size_mb, progressive)
            elif progressive == old_prog and size_mb < old_mb:
                best[h] = (size_mb, progressive)

    return {h: round(v[0], 1) for h, v in best.items()}


async def download_video(
    url: str,
    quality: str = "720",
    audio_only: bool = False,
    progress_cb=None,
    watermark: str = "",
) -> dict:
    outtmpl = str(DOWNLOAD_DIR / "%(id)s_%(height)s.%(ext)s")
    fmt = build_format(quality, audio_only=audio_only)

    ydl_opts = _base_opts(fast=False)
    ydl_opts.update({
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_PATH,
        "concurrent_fragment_downloads": 3,
        "format_sort": ["res", "ext:mp4:m4a"],
        "format_sort_force": False,
    })

    if audio_only:
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    if watermark and not audio_only:
        safe_wm = watermark.replace("'", "").replace(":", "").replace("\\", "")[:30]
        ydl_opts.setdefault("postprocessor_args", {})
        ydl_opts["postprocessor_args"]["ffmpeg"] = [
            "-vf",
            f"drawtext=text='{safe_wm}':fontsize=24:fontcolor=white@0.7:x=w-tw-20:y=h-th-20",
        ]

    loop = asyncio.get_event_loop()

    if progress_cb:
        def _hook(d):
            if d.get("status") != "downloading":
                return
            try:
                asyncio.run_coroutine_threadsafe(progress_cb(d), loop)
            except Exception:
                pass

        ydl_opts["progress_hooks"] = [_hook]

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if audio_only:
                filepath = os.path.splitext(filepath)[0] + ".mp3"
            elif not os.path.exists(filepath):
                base = os.path.splitext(filepath)[0]
                for ext in (".mp4", ".mkv", ".webm", ".m4a", ".jpg", ".png"):
                    if os.path.exists(base + ext):
                        filepath = base + ext
                        break
            return {
                "file": filepath,
                "title": info.get("title", "video"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration", 0),
                "ext": info.get("ext", "mp4"),
            }

    last_err = None
    for attempt in range(3):
        try:
            result = await asyncio.to_thread(_download)
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(2)
            else:
                raise last_err
    else:
        raise last_err

    size_mb = os.path.getsize(result["file"]) / (1024 * 1024)
    if size_mb > 50:
        cleanup(result["file"])
        raise ValueError(f"File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")
    return result


async def download_image(url: str) -> dict:
    outtmpl = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")

    ydl_opts = _base_opts(fast=True)
    ydl_opts.update({
        "outtmpl": outtmpl,
        "skip_download": False,
        "writethumbnail": False,
    })

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = None
            for key in ("filepath", "_filename"):
                if info.get(key) and os.path.exists(info[key]):
                    filepath = info[key]
                    break
            if not filepath:
                fp = ydl.prepare_filename(info)
                if os.path.exists(fp):
                    filepath = fp
            return {
                "file": filepath,
                "title": info.get("title", "image"),
                "thumbnail": info.get("thumbnail"),
                "ext": info.get("ext", "jpg"),
            }

    result = await asyncio.wait_for(asyncio.to_thread(_download), timeout=60)

    if not result["file"] or not os.path.exists(result["file"]):
        raise ValueError("Could not download image.")

    size_mb = os.path.getsize(result["file"]) / (1024 * 1024)
    if size_mb > 10:
        cleanup(result["file"])
        raise ValueError(f"Image too large ({size_mb:.1f} MB). Telegram photo limit is 10 MB.")
    return result


def cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_old_files(max_age_seconds: int = 3600) -> int:
    now = time.time()
    removed = 0
    try:
        for p in DOWNLOAD_DIR.iterdir():
            if p.is_file() and (now - p.stat().st_mtime) > max_age_seconds:
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


def is_supported(url: str) -> bool:
    supported = ["youtube.com", "youtu.be", "tiktok.com", "instagram.com",
                 "twitter.com", "x.com", "facebook.com", "fb.watch",
                 "reddit.com", "vimeo.com", "dailymotion.com", "pinterest."]
    return any(s in url.lower() for s in supported)

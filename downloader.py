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


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    if COOKIES_PATH:
        opts["cookiefile"] = str(COOKIES_PATH)
    return opts


def friendly_error(e: Exception) -> str:
    """Map yt-dlp exceptions to friendly user messages."""
    msg = str(e).lower()
    if "sign in to confirm" in msg or "not a bot" in msg:
        return "🤖 YouTube blocked this request. Try another link or wait a moment."
    if "video unavailable" in msg or "private" in msg or "removed" in msg or "deleted" in msg:
        return "🚫 This video is private, deleted, or unavailable."
    if "requested format is not available" in msg:
        return "⚠️ No compatible format found. Try a different quality."
    if "too large" in msg or "50 mb" in msg:
        return "📦 File too big (max 50 MB). Try a lower quality."
    if "timed out" in msg or "timeout" in msg:
        return "⏱️ Network timeout. Please try again."
    if "unable to download" in msg:
        return "❌ Couldn't download this video. It may be region-locked."
    if "unsupported url" in msg or "no video" in msg:
        return "🔗 Unsupported link. Try a different URL."
    if "geo" in msg and "block" in msg:
        return "🌍 This video is geo-blocked in our region."
    if "live" in msg and "not" in msg:
        return "🔴 Live streams aren't supported yet."
    return f"❌ Error: {str(e)[:180]}"


async def get_info(url: str) -> dict:
    """Fetch metadata only."""
    def _extract():
        opts = _base_opts()
        opts["skip_download"] = True
        opts["ignore_no_formats_error"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    return await asyncio.to_thread(_extract)


async def probe_formats(url: str) -> dict:
    """Return {height: estimated_mb} for available combined formats."""
    def _probe():
        opts = _base_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.to_thread(_probe)
    formats = info.get("formats") or []

    best = {}
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
        combined = vcodec != "none" and acodec != "none"
        # Prefer combined; only overwrite if we have a smaller combined
        if h not in best or (combined and best[h][1]):
            if h not in best or size_mb < best[h][0]:
                best[h] = (size_mb, combined)
    return {h: round(v[0], 1) for h, v in best.items()}


async def download_video(
    url: str,
    quality: str = "720",
    audio_only: bool = False,
    progress_cb=None,
    watermark: str = "",
) -> dict:
    """
    Download video with selected quality.
    progress_cb: async callable(percent: int, speed: str, eta: str)
    watermark: optional text to overlay (uses ffmpeg drawtext)
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
        "concurrent_fragment_downloads": 3,
    })

    if audio_only:
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    # Watermark via ffmpeg postprocessor
    if watermark and not audio_only:
        safe_wm = watermark.replace("'", "").replace(":", "").replace("\\", "")[:30]
        ydl_opts.setdefault("postprocessor_args", {})
        ydl_opts["postprocessors"] = ydl_opts.get("postprocessors", []) + [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }]
        # Note: full drawtext requires ffmpeg -vf; use a simple approach:
        ydl_opts.setdefault("postprocessor_args", {})["ffmpeg"] = [
            "-vf", f"drawtext=text='{safe_wm}':fontsize=24:fontcolor=white@0.7:x=w-tw-20:y=h-th-20"
        ]

    loop = asyncio.get_event_loop()

    if progress_cb:
        last = {"t": 0.0}

        def _hook(d):
            if d.get("status") != "downloading":
                return
            now = time.time()
            if now - last["t"] < 2.0:
                return
            last["t"] = now
            try:
                pct_str = (d.get("_percent_str") or "0%").strip().replace("%", "")
                pct = int(float(pct_str)) if pct_str.replace(".", "").isdigit() else 0
                speed = (d.get("_speed_str") or "?").strip()
                eta = (d.get("_eta_str") or "?").strip()
                asyncio.run_coroutine_threadsafe(
                    progress_cb(pct, speed, eta), loop
                )
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
                if os.path.exists(base + ".mp4"):
                    filepath = base + ".mp4"
            return {
                "file": filepath,
                "title": info.get("title", "video"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration", 0),
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

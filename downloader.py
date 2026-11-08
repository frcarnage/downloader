import os
import re
import sys
import time
import asyncio
import subprocess
import yt_dlp
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import imageio_ffmpeg

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ------------------------------------------------------------
# FFmpeg Configuration
# ------------------------------------------------------------
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
print(f"[FFmpeg] binary: {FFMPEG_PATH}", flush=True)

# ------------------------------------------------------------
# Cookies Configuration
# ------------------------------------------------------------
COOKIES_PATH = Path("cookies.txt")
if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
    print(f"[Cookies] loaded: {COOKIES_PATH}", flush=True)
else:
    COOKIES_PATH = None
    print("[Cookies] not found or empty", flush=True)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_TELEGRAM_MB = 50.0


# ------------------------------------------------------------
# yt-dlp Base Options
# ------------------------------------------------------------
def _base_opts(fast: bool = False) -> dict:
    clients = ["android", "ios"] if fast else ["android", "ios", "tv"]
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 15,
        "ignore_no_formats_error": True,
        "extractor_args": {
            "youtube": {
                "player_client": clients,
                "player_skip": ["webpage", "configs", "js"],
            },
        },
        "extractor_retries": 2,
        "file_access_retries": 2,
    }
    if COOKIES_PATH:
        opts["cookiefile"] = str(COOKIES_PATH)
    return opts


def friendly_error(e: Exception) -> str:
    msg = str(e).lower()
    if isinstance(e, asyncio.TimeoutError) or "timeout" in msg:
        return "⏱️ Request timed out. The source is slow right now — please try again in a moment."
    if "sign in to confirm" in msg or "not a bot" in msg or "bot" in msg:
        return "🤖 Platform temporarily challenged this request. Please try again in a few moments."
    if "page needs to be reloaded" in msg:
        return "🔄 Source requested a reload. Please try again in a few seconds."
    if "requested format is not available" in msg:
        return "⚠️ Selected quality is not available for this video. Try a different resolution or Best Available."
    if "no video formats found" in msg or "no formats found" in msg:
        return "⚠️ <b>No downloadable formats found.</b> The platform might be blocking automated downloads for this video right now."
    if "video unavailable" in msg or "private" in msg or "removed" in msg or "deleted" in msg:
        return "🚫 This video is private, deleted, or unavailable."
    if "live event" in msg or "is a live" in msg:
        return "🔴 Live streams cannot be downloaded until the stream has ended."
    if "unable to download" in msg:
        return "❌ Couldn't download this video. It may be geo-restricted or copyright-protected."
    if "unsupported url" in msg or "no video" in msg:
        return "🔗 Unsupported link. Please check the URL and try again."
    if "cancelled" in msg:
        return "⏱️ Request was cancelled."
    return f"❌ Error: {str(e)[:160]}"


def build_format(quality: str, audio_only: bool = False) -> str:
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
# Time & Duration Utilities
# ------------------------------------------------------------
def parse_time_str(s: str) -> float:
    """Parses '00:15', '01:30', or '1:05:20' to float seconds."""
    s = s.strip()
    parts = s.split(":")
    if len(parts) == 1:
        return float(parts[0])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Invalid time format: {s}")


async def get_video_duration(filepath: str) -> float:
    def _probe():
        cmd = [FFMPEG_PATH, "-i", filepath, "-hide_banner"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+|\d+)", res.stderr)
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
        return 0.0
    return await asyncio.to_thread(_probe)


# ------------------------------------------------------------
# FFmpeg Advanced Processing: Compress, Split, Trim, Voice, GIF, Speed
# ------------------------------------------------------------
async def compress_video(filepath: str, duration: float, target_mb: float = 46.5) -> str:
    if duration <= 0:
        duration = await get_video_duration(filepath)
    if duration <= 0:
        return filepath

    target_bytes = target_mb * 1024 * 1024
    total_bitrate = (target_bytes * 8) / duration
    audio_bitrate = min(96_000, max(48_000, int(total_bitrate * 0.15)))
    video_bitrate = max(160_000, total_bitrate - audio_bitrate)
    video_kbps = int(video_bitrate / 1000)
    audio_kbps = int(audio_bitrate / 1000)

    base, _ = os.path.splitext(filepath)
    out_path = f"{base}_comp.mp4"

    cmd = [
        FFMPEG_PATH, "-y", "-i", filepath,
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.35)}k",
        "-bufsize", f"{int(video_kbps * 2)}k",
        "-vf", "scale='min(1280,iw)':-2",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart", out_path
    ]

    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0 and os.path.exists(out_path)

    success = await asyncio.to_thread(_run)
    if success and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        cleanup(filepath)
        return out_path
    if os.path.exists(out_path):
        cleanup(out_path)
    return filepath


async def split_video(filepath: str, duration: float, chunk_seconds: float = 600.0) -> List[str]:
    if duration <= 0:
        duration = await get_video_duration(filepath)
    if duration <= chunk_seconds:
        return [filepath]

    chunks = []
    base, ext = os.path.splitext(filepath)
    start = 0.0
    part = 1

    while start < duration:
        chunk_path = f"{base}_part{part}{ext}"
        cmd = [
            FFMPEG_PATH, "-y", "-ss", str(start), "-i", filepath,
            "-t", str(chunk_seconds), "-c", "copy", "-movflags", "+faststart",
            chunk_path
        ]
        def _run(c=cmd, cp=chunk_path):
            res = subprocess.run(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return res.returncode == 0 and os.path.exists(cp)

        ok = await asyncio.to_thread(_run)
        if ok and os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunks.append(chunk_path)
        start += chunk_seconds
        part += 1

    if chunks:
        cleanup(filepath)
        return chunks
    return [filepath]


async def trim_video(url: str, start_str: str, end_str: str, quality: str = "720") -> dict:
    """Downloads and trims only the specified segment of a video."""
    start_s = parse_time_str(start_str)
    end_s = parse_time_str(end_str)
    if end_s <= start_s:
        raise ValueError("End time must be greater than start time.")
    duration = end_s - start_s
    if duration > 900:  # 15 minutes limit on trim
        raise ValueError("Trimmed clip cannot exceed 15 minutes.")

    # First download segment using download_ranges
    outtmpl = str(DOWNLOAD_DIR / "%(id)s_trim.%(ext)s")
    fmt = build_format(quality, audio_only=False)

    ydl_opts = _base_opts(fast=False)
    ydl_opts.update({
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG_PATH,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start_s, end_s)]),
        "force_keyframes_at_cuts": True,
    })

    def _dl():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if not os.path.exists(filepath):
                base = os.path.splitext(filepath)[0]
                for ext in (".mp4", ".mkv", ".webm"):
                    if os.path.exists(base + ext):
                        filepath = base + ext
                        break
            return {
                "file": filepath,
                "title": f"{info.get('title', 'clip')} [{start_str}-{end_str}]",
                "duration": duration,
                "ext": "mp4",
            }

    return await asyncio.to_thread(_dl)


async def convert_to_voice(audio_path: str) -> str:
    """Converts audio file to Telegram Voice format (.ogg with libopus)."""
    base, _ = os.path.splitext(audio_path)
    out_path = f"{base}_voice.ogg"

    cmd = [
        FFMPEG_PATH, "-y", "-i", audio_path,
        "-c:a", "libopus", "-b:a", "48k", "-vbr", "on",
        out_path
    ]

    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(out_path)

    ok = await asyncio.to_thread(_run)
    if ok and os.path.getsize(out_path) > 0:
        return out_path
    return audio_path


async def convert_to_gif(video_path: str, max_duration: int = 15) -> str:
    """Converts a video clip into a silent MP4 loop for Telegram native GIF animations."""
    base, _ = os.path.splitext(video_path)
    out_path = f"{base}_loop.mp4"

    cmd = [
        FFMPEG_PATH, "-y", "-i", video_path, "-t", str(max_duration),
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "26",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path
    ]

    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(out_path)

    ok = await asyncio.to_thread(_run)
    if ok and os.path.getsize(out_path) > 0:
        return out_path
    return video_path


async def change_audio_speed(audio_path: str, speed: float = 1.5) -> str:
    """Changes playback speed of audio file (e.g. 1.25x, 1.5x, 2.0x)."""
    base, ext = os.path.splitext(audio_path)
    out_path = f"{base}_{speed}x{ext}"

    cmd = [
        FFMPEG_PATH, "-y", "-i", audio_path,
        "-filter:a", f"atempo={speed}", "-vn",
        out_path
    ]

    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(out_path)

    ok = await asyncio.to_thread(_run)
    if ok and os.path.getsize(out_path) > 0:
        return out_path
    return audio_path


# ------------------------------------------------------------
# Viral Audio Effects Studio (Slowed+Reverb, Nightcore, Bass Boost)
# ------------------------------------------------------------
async def apply_audio_effect(audio_path: str, effect: str) -> str:
    """
    Applies viral audio studio effects using FFmpeg:
    - 'slowed': Slowed + Reverb
    - 'nightcore': Sped up + high pitch
    - 'bassboost': Deep rich punchy low-frequency boost
    """
    filters = {
        "slowed": "asetrate=44100*0.85,aresample=44100,aecho=0.8:0.88:60:0.4",
        "nightcore": "asetrate=44100*1.25,aresample=44100",
        "bassboost": "equalizer=f=60:width_type=h:width=50:g=10,equalizer=f=100:width_type=h:width=50:g=6",
    }
    filter_str = filters.get(effect)
    if not filter_str:
        return audio_path

    base, _ = os.path.splitext(audio_path)
    out_path = f"{base}_{effect}.mp3"

    cmd = [
        FFMPEG_PATH, "-y", "-i", audio_path,
        "-af", filter_str, "-b:a", "192k",
        out_path
    ]

    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(out_path)

    ok = await asyncio.to_thread(_run)
    if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    return audio_path


async def embed_mp3_metadata_and_art(
    audio_path: str, title: str, artist: str = "Universal Downloader", thumb_path: Optional[str] = None
) -> str:
    """
    Embeds clean ID3v2 tags (title, artist, album) and high-res cover artwork
    so downloaded tracks display natively in Telegram music player.
    """
    base, _ = os.path.splitext(audio_path)
    out_path = f"{base}_tagged.mp3"

    clean_title = title[:80].replace('"', '\\"')
    clean_artist = artist[:50].replace('"', '\\"')

    if thumb_path and os.path.exists(thumb_path):
        cmd = [
            FFMPEG_PATH, "-y",
            "-i", audio_path,
            "-i", thumb_path,
            "-map", "0:a",
            "-map", "1:v",
            "-c:a", "copy",
            "-c:v", "copy",
            "-id3v2_version", "3",
            "-metadata", f"title={clean_title}",
            "-metadata", f"artist={clean_artist}",
            "-metadata", "album=Universal Downloader Pro",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            out_path
        ]
    else:
        cmd = [
            FFMPEG_PATH, "-y",
            "-i", audio_path,
            "-c:a", "copy",
            "-id3v2_version", "3",
            "-metadata", f"title={clean_title}",
            "-metadata", f"artist={clean_artist}",
            "-metadata", "album=Universal Downloader Pro",
            out_path
        ]

    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(out_path)

    ok = await asyncio.to_thread(_run)
    if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        cleanup(audio_path)
        return out_path
    return audio_path


async def test_cookie_health() -> Tuple[bool, str]:
    """
    Proactively checks YouTube cookie validity and platform health.
    """
    opts = _base_opts(fast=True)
    opts.update({
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
    })

    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    def _test():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(test_url, download=False)
                title = info.get("title", "")
                if title:
                    return True, f"✅ Cookies Active & Healthy! (Verified on: {title[:30]})"
                return False, "⚠️ Warning: YouTube returned empty metadata."
        except Exception as e:
            return False, f"❌ Cookie/Platform Error: {str(e)[:120]}"

    return await asyncio.to_thread(_test)


async def download_subtitles(url: str, lang: str = "en") -> Optional[str]:
    """Extracts and writes closed captions / subtitles as .srt file."""
    ydl_opts = _base_opts(fast=True)
    ydl_opts.update({
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang, "en", "all"],
        "subtitlesformat": "srt",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
    })

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            vid = info.get("id", "sub")
            # Look for generated .srt files in DOWNLOAD_DIR
            for p in DOWNLOAD_DIR.glob(f"{vid}*.srt"):
                return str(p)
            return None

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=30)
    except Exception:
        return None


# ------------------------------------------------------------
# YouTube Timestamp & Transcript Extractor (.txt & Chat)
# ------------------------------------------------------------
def extract_youtube_id(url: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from various URL patterns."""
    patterns = [
        r'(?:https?:\/\/)?(?:www\.|m\.)?youtube\.com\/(?:watch\?.*v=|embed\/|v\/|shorts\/|live\/)([a-zA-Z0-9_-]{11})',
        r'(?:https?:\/\/)?(?:www\.)?youtu\.be\/([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$'
    ]
    for p in patterns:
        m = re.search(p, url)
        if m and len(m.group(1)) == 11:
            return m.group(1)
    return None


def format_timestamp(seconds: float) -> str:
    """Formats seconds into [MM:SS] or [HH:MM:SS] timestamp."""
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 0:
        return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"
    return f"[{minutes:02d}:{secs:02d}]"


async def get_youtube_transcript(url: str, preferred_lang: str = "en", bot_username: str = "ChatAutomationnBot") -> Dict[str, Any]:
    """
    Extracts transcript for a YouTube video with exact timestamps (e.g. [16:01]).
    Generates a .txt file and a copyable snippet for Telegram chat.
    """
    video_id = extract_youtube_id(url)
    if not video_id:
        raise ValueError("Could not extract a valid YouTube video ID from link.")

    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
    )

    def _fetch():
        api = YouTubeTranscriptApi()
        try:
            t_list = api.list(video_id)
            target_tr = None
            try:
                target_tr = t_list.find_transcript([preferred_lang, "en", "hi", "es", "ru", "uz", "ar"])
            except Exception:
                target_tr = next(iter(t_list))

            fetched = target_tr.fetch()
            return target_tr.language, target_tr.language_code, fetched.snippets
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
            raise
        except Exception:
            fetched = api.fetch(video_id, languages=(preferred_lang, "en"))
            return "English", "en", fetched.snippets

    try:
        lang_name, lang_code, snippets = await asyncio.wait_for(
            asyncio.to_thread(_fetch), timeout=25
        )
    except TranscriptsDisabled:
        raise ValueError("Transcripts are disabled for this YouTube video.")
    except NoTranscriptFound:
        raise ValueError("No transcript found in available languages for this video.")
    except VideoUnavailable:
        raise ValueError("This YouTube video is unavailable or private.")
    except asyncio.TimeoutError:
        raise ValueError("Fetching transcript timed out. Please try again.")
    except Exception as e:
        raise ValueError(f"Could not retrieve transcript: {str(e)}")

    if not snippets:
        raise ValueError("Transcript was empty.")

    title = f"YouTube Video ({video_id})"
    try:
        info = await asyncio.wait_for(get_info(url), timeout=8)
        if info and info.get("title"):
            title = info["title"]
    except Exception:
        pass

    txt_lines = [
        "======================================================================",
        "📜 YOUTUBE VIDEO TRANSCRIPT",
        f"🎬 Title: {title}",
        f"🆔 Video ID: {video_id}",
        f"🌐 Language: {lang_name} ({lang_code})",
        f"📊 Total Segments: {len(snippets)}",
        f"🔗 URL: https://youtu.be/{video_id}",
        f"🤖 Extracted via @{bot_username}",
        "======================================================================\n",
    ]

    chat_snippet_lines = []
    current_chat_len = 0

    for s in snippets:
        ts_str = format_timestamp(s.start)
        clean_text = s.text.replace("\n", " ").strip()
        line = f"{ts_str} {clean_text}"
        txt_lines.append(line)

        if current_chat_len < 2500:
            chat_snippet_lines.append(line)
            current_chat_len += len(line) + 1

    out_file = DOWNLOAD_DIR / f"{video_id}_transcript.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))

    chat_snippet = "\n".join(chat_snippet_lines)
    if len(txt_lines) > len(chat_snippet_lines):
        chat_snippet += "\n\n... [Full transcript continued in attached .txt file]"

    return {
        "file": str(out_file),
        "snippet": chat_snippet,
        "total_lines": len(snippets),
        "title": title,
        "lang": lang_name,
        "video_id": video_id,
    }


# ------------------------------------------------------------
# In-Chat YouTube Search
# ------------------------------------------------------------
async def search_youtube(query: str, max_results: int = 5) -> List[dict]:
    """Searches YouTube and returns top results with title, url, thumb, duration."""
    ydl_opts = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    search_query = f"ytsearch{max_results}:{query}"

    def _search():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(search_query, download=False)
            entries = res.get("entries") or []
            results = []
            for e in entries:
                if e and e.get("id"):
                    results.append({
                        "id": e.get("id"),
                        "title": e.get("title") or "Video",
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                        "duration": e.get("duration", 0),
                        "uploader": e.get("uploader") or e.get("channel") or "YouTube",
                        "thumbnail": e.get("thumbnail") or f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
                    })
            return results

    try:
        return await asyncio.wait_for(asyncio.to_thread(_search), timeout=20)
    except Exception as e:
        print(f"[Search Error]: {e}", flush=True)
        return []


# ------------------------------------------------------------
# Social Media Repurposer: 16:9 to 9:16 Vertical Reel Maker
# ------------------------------------------------------------
async def convert_to_vertical_916(video_path: str) -> str:
    """Converts a standard horizontal video to 9:16 vertical with blurred background padding."""
    base, _ = os.path.splitext(video_path)
    out_path = f"{base}_vertical.mp4"

    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:10[bg];"
        "[0:v]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )

    cmd = [
        FFMPEG_PATH, "-y", "-i", video_path,
        "-vf", filter_complex,
        "-c:v", "libx264", "-preset", "fast", "-crf", "24",
        "-c:a", "copy", "-movflags", "+faststart",
        out_path
    ]

    def _run():
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(out_path)

    ok = await asyncio.to_thread(_run)
    if ok and os.path.getsize(out_path) > 0:
        return out_path
    return video_path


# ------------------------------------------------------------
# Video Summary & Chapter Extractor
# ------------------------------------------------------------
async def get_video_summary(url: str) -> dict:
    """Extracts chapter breakdown, description highlights, and key takeaways."""
    info = await get_info(url)
    title = info.get("title") or "Video"
    chapters = info.get("chapters") or []
    description = (info.get("description") or "").strip()
    duration = info.get("duration", 0)

    # Format chapter breakdown
    chapter_lines = []
    if chapters:
        for c in chapters[:12]:
            st = c.get("start_time", 0)
            mins, secs = divmod(int(st), 60)
            chapter_lines.append(f"⏱ <code>{mins:02d}:{secs:02d}</code> — <b>{c.get('title', 'Chapter')}</b>")

    # Clean description snippet
    desc_clean = description[:350].replace("<", "&lt;").replace(">", "&gt;") if description else "No description provided."

    return {
        "title": title,
        "duration": duration,
        "chapters": chapter_lines,
        "description": desc_clean,
    }


# ------------------------------------------------------------
# Playlist Batch Extraction
# ------------------------------------------------------------
async def get_playlist_info(url: str, max_items: int = 10) -> Optional[dict]:
    """Inspects if URL is a playlist and extracts track list."""
    ydl_opts = _base_opts(fast=True)
    ydl_opts["extract_flat"] = "in_playlist"
    ydl_opts["skip_download"] = True

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info or info.get("_type") != "playlist" and not info.get("entries"):
                return None
            entries = info.get("entries") or []
            tracks = []
            for e in entries[:max_items]:
                if e and e.get("id"):
                    tracks.append({
                        "id": e.get("id"),
                        "title": e.get("title") or "Track",
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                        "duration": e.get("duration", 0),
                    })
            return {
                "title": info.get("title", "Playlist"),
                "total": len(entries),
                "tracks": tracks,
            }

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=25)
    except Exception:
        return None


# ------------------------------------------------------------
# Engine Auto-Updater
# ------------------------------------------------------------
async def update_ytdlp() -> Tuple[bool, str]:
    """Updates yt-dlp to latest upstream release."""
    def _run():
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--no-cache-dir", "yt-dlp"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return (res.returncode == 0, res.stdout[-400:])

    return await asyncio.to_thread(_run)


# ------------------------------------------------------------
# Image & Video Download Pipeline
# ------------------------------------------------------------
def is_image_only(info: dict, url: str = "") -> bool:
    if not info:
        return False
    ext = (info.get("ext") or "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif"):
        return True
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    if "pinterest" in extractor or any(h in url.lower() for h in ("pinterest.", "pin.it", "pinimg.com")):
        formats = info.get("formats") or []
        has_video = any((f.get("vcodec") and f["vcodec"] != "none") for f in formats)
        return not has_video
    return False


async def get_info(url: str) -> dict:
    def _extract(fast: bool):
        opts = _base_opts(fast=fast)
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await asyncio.wait_for(asyncio.to_thread(_extract, False), timeout=30)
    except Exception:
        pass
    return await asyncio.wait_for(asyncio.to_thread(_extract, True), timeout=25)


async def probe_formats(url: str) -> dict:
    def _probe(fast: bool):
        opts = _base_opts(fast=fast)
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.wait_for(asyncio.to_thread(_probe, True), timeout=18)
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
    status_msg_cb=None,
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
        ydl_opts["writethumbnail"] = True
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
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
                "uploader": info.get("uploader") or info.get("channel") or "Universal DL",
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

    filepath = result["file"]
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    duration = result.get("duration") or 0
    chunks = []

    if size_mb > MAX_TELEGRAM_MB:
        if audio_only:
            cleanup(filepath)
            raise ValueError(f"Audio file is too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")

        if size_mb <= 140 and duration > 0 and duration < 3600:
            if status_msg_cb:
                try:
                    await status_msg_cb(f"⚙️ <b>File is {size_mb:.1f} MB</b>\n<i>Compressing to fit Telegram 50 MB limit...</i>")
                except Exception:
                    pass

            comp_file = await compress_video(filepath, duration, target_mb=47.0)
            new_size = os.path.getsize(comp_file) / (1024 * 1024)
            if new_size <= MAX_TELEGRAM_MB:
                result["file"] = comp_file
                result["compressed"] = True
                result["orig_size_mb"] = size_mb
            else:
                if status_msg_cb:
                    try:
                        await status_msg_cb("✂️ <b>Large video</b>\n<i>Splitting into parts for Telegram...</i>")
                    except Exception:
                        pass
                chunks = await split_video(comp_file, duration, chunk_seconds=600.0)
        else:
            if status_msg_cb:
                try:
                    await status_msg_cb(f"✂️ <b>Large video ({size_mb:.0f} MB)</b>\n<i>Splitting into parts for Telegram...</i>")
                except Exception:
                    pass
            chunks = await split_video(filepath, duration, chunk_seconds=600.0)

    if chunks:
        result["chunks"] = chunks
        result["file"] = chunks[0]
        result["is_split"] = True

    return result


def _best_image_url(info: dict) -> Tuple[Optional[str], str]:
    formats = info.get("formats") or []
    image_formats = [
        f for f in formats
        if f.get("url") and f.get("vcodec") in (None, "none") and f.get("acodec") in (None, "none")
    ]
    if image_formats:
        def _score(f):
            return (f.get("width") or 0) * (f.get("height") or 0) or (f.get("filesize") or 0)
        best = max(image_formats, key=_score)
        return best["url"], (best.get("ext") or "jpg")

    if info.get("url") and not formats:
        return info["url"], (info.get("ext") or "jpg")

    thumbs = info.get("thumbnails") or []
    if thumbs:
        if any(t.get("width") for t in thumbs):
            best_thumb = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        else:
            best_thumb = thumbs[-1]
        if best_thumb.get("url"):
            return best_thumb["url"], "jpg"

    if info.get("thumbnail"):
        return info["thumbnail"], "jpg"

    return None, "jpg"


async def download_image(url: str) -> dict:
    def _extract():
        opts = _base_opts(fast=True)
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=30)
    entries = info.get("entries")
    if entries and isinstance(entries, list) and len(entries) > 1:
        images_list = []
        for i, entry in enumerate(entries[:10]):
            img_url, ext = _best_image_url(entry)
            if img_url:
                dest = DOWNLOAD_DIR / f"{entry.get('id') or f'img_{i}'}.{ext}"
                try:
                    def _fetch(u=img_url, d=dest):
                        import urllib.request
                        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=25) as r, open(d, "wb") as f:
                            f.write(r.read())
                    await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=30)
                    if dest.exists() and dest.stat().st_size > 0:
                        images_list.append(str(dest))
                except Exception:
                    pass

        if images_list:
            return {
                "file": images_list[0],
                "files": images_list,
                "is_album": True,
                "title": info.get("title", "Album"),
                "thumbnail": info.get("thumbnail"),
                "ext": "jpg",
            }

    image_url, ext = _best_image_url(info)
    if not image_url:
        raise ValueError("Could not find an image to download.")

    name_part = image_url.split("?")[0].rsplit("/", 1)[-1]
    if "." in name_part:
        guessed_ext = name_part.rsplit(".", 1)[-1].lower()
        if guessed_ext in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = guessed_ext

    dest = DOWNLOAD_DIR / f"{info.get('id') or 'img'}.{ext}"

    def _fetch_single():
        import urllib.request
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            f.write(r.read())

    await asyncio.wait_for(asyncio.to_thread(_fetch_single), timeout=45)

    if not dest.exists() or dest.stat().st_size == 0:
        raise ValueError("Could not download image.")

    size_mb = os.path.getsize(dest) / (1024 * 1024)
    if size_mb > 10:
        cleanup(str(dest))
        raise ValueError(f"Image too large ({size_mb:.1f} MB). Telegram limit is 10 MB.")

    return {
        "file": str(dest),
        "title": info.get("title", "image"),
        "thumbnail": info.get("thumbnail"),
        "ext": ext,
        "is_album": False,
    }


def cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_old_files(max_age_seconds: int = 3600, directory: Path = DOWNLOAD_DIR) -> int:
    now = time.time()
    removed = 0
    try:
        for p in Path(directory).iterdir():
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
    supported = [
        "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
        "twitter.com", "x.com", "facebook.com", "fb.watch", "fb.com",
        "reddit.com", "vimeo.com", "dailymotion.com",
        "pinterest.", "pin.it", "pinimg.com", "threads.net",
        "twitch.tv", "soundcloud.com", "bilibili.com", "vk.com",
    ]
    return any(s in url.lower() for s in supported)

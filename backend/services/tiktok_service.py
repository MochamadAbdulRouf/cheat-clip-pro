import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from backend.config import (
    TEMP_DIR,
    UPLOADS_DIR,
    ephemeral_cookies_file,
    get_effective_cookies_path,
    is_valid_mp4,
    logger,
)


def is_tiktok_url(url: str) -> bool:
    """Checks whether the given URL is a valid TikTok video or shortlink."""
    if not url:
        return False
    u = url.strip()
    return bool(re.search(r'(?:tiktok\.com|vt\.tiktok\.com|vm\.tiktok\.com)', u, re.IGNORECASE))


def extract_tiktok_id(url: str) -> Optional[str]:
    """Extracts a recognizable video ID or shortcode from various TikTok URL patterns."""
    if not url:
        return None
    u = url.strip()

    # 1. Standard video URL: /video/1234567890123456789
    m = re.search(r'/video/(\d+)', u)
    if m:
        return m.group(1)

    # 2. Mobile web: /v/1234567890123456789
    m_v = re.search(r'/v/(\d+)', u)
    if m_v:
        return m_v.group(1)

    # 3. Short URLs: vt.tiktok.com/xxxxxx or vm.tiktok.com/xxxxxx
    m_short = re.search(r'(?:vt|vm)\.tiktok\.com/([a-zA-Z0-9_-]+)', u)
    if m_short:
        return m_short.group(1)

    # 4. TikTok t links: tiktok.com/t/xxxxxx
    m_t = re.search(r'tiktok\.com/t/([a-zA-Z0-9_-]+)', u)
    if m_t:
        return m_t.group(1)

    # 5. Raw numeric ID
    if re.match(r'^\d{15,25}$', u):
        return u

    return None


def download_tiktok_video(
    url_or_id: str,
    on_progress: Optional[Callable[[str, str, int], None]] = None,
    cookies_content: Optional[str] = None
) -> Path:
    """
    Downloads a TikTok video into UPLOADS_DIR.
    1. Checks if a file for this ID is already cached.
    2. Uses yt-dlp to extract and download the best quality video + audio merged as MP4.
    Returns the Path to the local video file.
    """
    if not url_or_id or not is_tiktok_url(url_or_id):
        raise ValueError("Invalid TikTok URL provided. Please provide a link like: https://www.tiktok.com/@user/video/... or vt.tiktok.com/...")

    clean_url = url_or_id.strip()
    if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
        clean_url = f"https://{clean_url}"

    file_id = extract_tiktok_id(clean_url) or re.sub(r'[^a-zA-Z0-9_-]', '_', clean_url)[-20:]

    # 1. Cache check: Has this TikTok video already been downloaded and verified?
    existing = list(UPLOADS_DIR.glob(f"tiktok_{file_id}*.*"))
    for f in existing:
        if f.exists() and f.is_file() and f.stat().st_size > 100 * 1024 and is_valid_mp4(f):
            logger.info(f"Using cached TikTok video: {f.name} ({f.stat().st_size} bytes)")
            if on_progress:
                on_progress("Video Found in Cache", f"Using cached TikTok video: {f.name}", 100)
            return f

    if on_progress:
        on_progress("Connecting to TikTok", "Resolving TikTok media stream & metadata...", 10)

    # 2. yt-dlp download configuration
    def ydl_hook(d):
        if d.get("status") == "downloading" and on_progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total > 0:
                pct = int(min(90, 10 + (downloaded / total) * 80))
                mb_down = downloaded / (1024 * 1024)
                mb_tot = total / (1024 * 1024)
                on_progress("Downloading Video", f"Downloading TikTok: {mb_down:.1f} MB / {mb_tot:.1f} MB ({pct}%)", pct)
            else:
                mb_down = downloaded / (1024 * 1024)
                on_progress("Downloading Video", f"Downloading TikTok: {mb_down:.1f} MB...", 40)

    out_template = str(UPLOADS_DIR / f"tiktok_{file_id}_%(title).50B.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "progress_hooks": [ydl_hook],
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
    }

    # Optional user cookies context
    cookie_ctx = ephemeral_cookies_file(cookies_content) if cookies_content else None
    if cookie_ctx:
        ydl_opts["cookiefile"] = str(cookie_ctx)
    else:
        system_cookies = get_effective_cookies_path()
        if system_cookies and system_cookies.exists():
            ydl_opts["cookiefile"] = str(system_cookies)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([clean_url])
    except Exception as e:
        logger.error(f"yt-dlp TikTok download error: {e}")
        raise RuntimeError(f"Could not download TikTok video: {str(e)}")

    # 3. Locate downloaded file
    candidates = list(UPLOADS_DIR.glob(f"*tiktok_{file_id}*.*")) + [
        f for f in UPLOADS_DIR.iterdir() if file_id in f.name and f.is_file()
    ]
    for c in candidates:
        if c.exists() and c.is_file() and c.stat().st_size > 50 * 1024:
            # Fix double .mp4.mp4 extension if present
            if c.name.endswith(".mp4.mp4"):
                fixed_name = c.name[:-4]
                fixed_path = c.parent / fixed_name
                try:
                    c.rename(fixed_path)
                    c = fixed_path
                except Exception:
                    pass

            if is_valid_mp4(c):
                logger.info(f"yt-dlp successfully downloaded TikTok video: {c.name}")
                if on_progress:
                    on_progress("Download Complete", f"TikTok video ready: {c.name}", 100)
                return c

    raise RuntimeError("Failed to verify TikTok video on disk after download.")

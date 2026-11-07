"""Find browser-compatible progressive video formats from yt-dlp metadata.

A 'direct' link is best-effort: sites may still tie media URLs to IP, session,
headers, or expire a signed URL. This module does not bypass restrictions.
"""
from __future__ import annotations

import ipaddress
import math
import re
from urllib.parse import urlsplit

BROWSER_EXTENSIONS = {"mp4", "m4v", "mov"}
BROWSER_PROTOCOLS = {"http", "https"}
SENSITIVE_HEADERS = {"authorization", "cookie", "referer", "x-forwarded-for"}
MAX_MEDIA_URL_LENGTH = 3000


def human_size(size: int | float | None) -> str:
    if not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
        return "размер неизвестен"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return "размер неизвестен"


def looks_public_media_url(url: str) -> bool:
    if not isinstance(url, str) or not 0 < len(url) <= MAX_MEDIA_URL_LENGTH:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return False
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            return False
        host = parsed.hostname.lower().strip(".")
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return bool(re.fullmatch(r"[a-z0-9.-]+", host)) and "." in host
    except (ValueError, UnicodeError):
        return False


def has_browser_blocking_headers(info: dict, fmt: dict) -> bool:
    # A browser cannot freely set these custom headers while following a link.
    for headers in (info.get("http_headers"), fmt.get("http_headers")):
        if not isinstance(headers, dict):
            continue
        for key, value in headers.items():
            if not value:
                continue
            lower = str(key).lower()
            if lower in SENSITIVE_HEADERS or lower.startswith("x-"):
                return True
    return False


def direct_formats(info: dict, max_options: int = 8) -> list[dict]:
    """Only expose candidate audio+video MP4-like progressive links.

    Does not claim that a remote website will allow the iPhone's IP/session.
    This is intentionally conservative; HLS/DASH and video-only are excluded.
    """
    formats = info.get("formats")
    if not isinstance(formats, list) or not formats:
        formats = [info]
    candidates = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        url = fmt.get("url")
        if not looks_public_media_url(url):
            continue
        ext = str(fmt.get("ext") or "").lower()
        if ext not in BROWSER_EXTENSIONS:
            continue
        protocol = str(fmt.get("protocol") or urlsplit(url).scheme).lower()
        if protocol not in BROWSER_PROTOCOLS:
            continue
        if fmt.get("vcodec") == "none" or fmt.get("acodec") == "none":
            continue
        # The format must carry both video and audio, except a generic direct
        # MP4 whose codecs are not exposed at all by the extractor.
        if fmt is not info and (not fmt.get("vcodec") or not fmt.get("acodec")):
            continue
        if has_browser_blocking_headers(info, fmt):
            continue
        height = fmt.get("height")
        if not isinstance(height, (int, float)) or height <= 0:
            height = 0
        else:
            height = int(height)
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        size = int(size) if isinstance(size, (int, float)) and size > 0 else None
        candidates.append({"url": url, "height": height, "ext": ext,
                           "size": size, "format_id": fmt.get("format_id")})
    # Prefer MP4, then known filesize, avoid duplicate heights.
    candidates.sort(key=lambda v: (v["height"], v["ext"] == "mp4", v["size"] is not None), reverse=True)
    seen_heights = set()
    result = []
    for fmt in candidates:
        if fmt["height"] in seen_heights:
            continue
        seen_heights.add(fmt["height"])
        result.append(fmt)
        if len(result) >= max_options:
            break
    return result

"""On-demand video muxing to Safari without storing a completed file.

Stream response is a fragmented MP4 produced by FFmpeg. It has no arbitrary
file-size cap; downloads are bounded by time/concurrency, Railway's HTTP request
limits, supported codecs, and the origin's restrictions. HTTP Range/resume is
not supported here; use an object-store backend for durable large downloads.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import yt_dlp
from aiohttp import web

log = logging.getLogger("video_stream")
STREAM_LINK_TTL = 10 * 60
# Railway currently caps a non-idle HTTP request at 15 minutes.
MAX_STREAM_SECONDS = min(max(int(os.getenv("STREAM_MAX_SECONDS", "780")), 60), 840)
SLOTS = asyncio.Semaphore(1)
STREAMS: dict[str, "StreamRequest"] = {}

ALLOWED_PROTOCOLS = {"http", "https", "m3u8", "m3u8_native", "http_dash_segments"}
VIDEO_CODECS = ("avc1", "h264", "hev1", "hvc1", "hevc", "h265", "av01")
AUDIO_CODECS = ("aac", "mp4a", "mp3", "opus")
MAX_URL_LENGTH = 4096


class StreamUnavailable(Exception):
    """Source cannot be prepared as a browser-friendly MP4."""


@dataclass
class StreamRequest:
    url: str
    height: int | None
    expires: float


def register_stream(url: str, height: int | None) -> str:
    """Make a one-use link; source is inspected only after a user opens it."""
    expire_old()
    if len(STREAMS) > 200:
        raise StreamUnavailable("Сейчас слишком много ссылок. Попробуй позже.")
    token = secrets.token_urlsafe(24)
    STREAMS[token] = StreamRequest(url=url, height=height, expires=time.monotonic() + STREAM_LINK_TTL)
    return token


def expire_old() -> None:
    now = time.monotonic()
    for token, req in list(STREAMS.items()):
        if req.expires <= now:
            STREAMS.pop(token, None)


def _public_http_url(value: str) -> bool:
    if not isinstance(value, str) or not 0 < len(value) < MAX_URL_LENGTH:
        return False
    try:
        u = urlsplit(value)
        if u.scheme not in ("https", "http") or not u.hostname:
            return False
        if u.username or u.password or u.port not in (None, 80, 443):
            return False
        host = u.hostname.lower().strip(".")
        if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except (ValueError, UnicodeError):
        return False


def _validate_dns(urls: list[str]) -> None:
    # Initial DNS check is defense-in-depth only: server-wide outbound egress
    # restrictions are still needed to mitigate redirects / DNS rebinding.
    for value in urls:
        if not _public_http_url(value):
            raise StreamUnavailable("Источник видео содержит недопустимый адрес.")
        u = urlsplit(value)
        try:
            addresses = socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise StreamUnavailable("Источник видео недоступен по публичному адресу.")
        except (OSError, ValueError) as exc:
            raise StreamUnavailable("Не удалось проверить адрес источника видео.") from exc


def _valid_format(fmt: dict) -> bool:
    protocol = str(fmt.get("protocol") or "https").lower()
    return (protocol in ALLOWED_PROTOCOLS and _public_http_url(fmt.get("url"))
            and not fmt.get("has_drm"))


def _codec_ok(fmt: dict, kinds: tuple[str, ...]) -> bool:
    return str(fmt.get("vcodec") or "").lower().startswith(kinds)


def select_inputs(info: dict, requested_height: int | None) -> list[dict]:
    """Choose a public non-DRM progressive/HLS/DASH stream with audio.

    For video-only DASH, select one audio-only stream to mux in FFmpeg.
    Best-effort: extractor metadata may not include embedded player codecs.
    """
    if info.get("has_drm") or info.get("is_live"):
        raise StreamUnavailable("Защищённые и прямые трансляции не поддерживаются.")
    formats = info.get("formats") or [info]
    full, video_only, audio_only = [], [], []
    for fmt in formats:
        if not isinstance(fmt, dict) or not _valid_format(fmt):
            continue
        h = fmt.get("height")
        if requested_height is not None and isinstance(h, (int, float)) and h > requested_height:
            continue
        v = str(fmt.get("vcodec") or "").lower()
        a = str(fmt.get("acodec") or "").lower()
        if v not in ("", "none") and not v.startswith(VIDEO_CODECS):
            continue
        if v == "none" and a not in ("", "none"):
            audio_only.append(fmt)
        elif v not in ("", "none") and a not in ("", "none"):
            full.append(fmt)
        elif v not in ("", "none"):
            video_only.append(fmt)
        elif fmt is info and (fmt.get("ext") or "").lower() in ("mp4", "m4v"):
            full.append(fmt)

    def rank_video(f: dict) -> tuple:
        h = f.get("height") or 0
        tbr = f.get("tbr") or 0
        return (h if isinstance(h, (int, float)) else 0,
                tbr if isinstance(tbr, (int, float)) else 0,
                str(f.get("ext", "")).lower() == "mp4")
    full.sort(key=rank_video, reverse=True)
    video_only.sort(key=rank_video, reverse=True)
    audio_only.sort(key=lambda f: (str(f.get("ext", "")) in ("m4a", "mp4"),
                                   f.get("abr") or 0), reverse=True)
    if full and (not video_only or rank_video(full[0])[0] >= rank_video(video_only[0])[0]):
        return [full[0]]
    if video_only and audio_only:
        return [video_only[0], audio_only[0]]
    if full:
        return [full[0]]
    raise StreamUnavailable("Не найден доступный MP4/HLS/DASH с поддерживаемым видео и звуком.")


def _input_options(fmt: dict, info: dict) -> list[str]:
    headers = {}
    headers.update(info.get("http_headers") if isinstance(info.get("http_headers"), dict) else {})
    headers.update(fmt.get("http_headers") if isinstance(fmt.get("http_headers"), dict) else {})
    # Do not forward private session headers. They require a separate secure
    # authorization design; intentionally no DRM or account-access bypass.
    if any(str(k).lower() in ("authorization", "cookie", "proxy-authorization")
           for k, val in headers.items() if val):
        raise StreamUnavailable("Для этого потока требуется авторизация.")
    opts = ["-rw_timeout", "20000000",
            "-protocol_whitelist", "http,https,tcp,tls,crypto,data"]
    agent = headers.get("User-Agent") or headers.get("user-agent")
    referer = headers.get("Referer") or headers.get("referer")
    if agent:
        opts += ["-user_agent", str(agent)[:500]]
    if referer and _public_http_url(str(referer)):
        opts += ["-referer", str(referer)[:1000]]
    return opts + ["-i", str(fmt["url"])]


def ffmpeg_args(info: dict, height: int | None) -> list[str]:
    sources = select_inputs(info, height)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    for src in sources:
        cmd += _input_options(src, info)
    if len(sources) == 1:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    # Remux only where feasible: avoids heavy CPU/temporary storage.
    audio = str(sources[-1].get("acodec") or "").lower()
    use_aac = audio not in ("", "none") and not audio.startswith(("aac", "mp4a", "mp3"))
    cmd += ["-c:v", "copy", "-c:a", "aac" if use_aac else "copy"]
    if use_aac:
        cmd += ["-b:a", "128k"]
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1"]
    return cmd


def _extract_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": 20,
        "retries": 2, "js_runtimes": {"node": {}},
    }) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict) or info.get("_type") in ("playlist", "multi_video"):
        raise StreamUnavailable("Отправь ссылку на отдельное видео.")
    return info


def register_routes(app: web.Application) -> None:
    # GET is single-use; HEAD must never consume the link.
    app.router.add_head("/stream/{token}", stream_head)
    app.router.add_get("/stream/{token}", stream_video, allow_head=False)


async def stream_head(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    expire_old()
    if token not in STREAMS:
        raise web.HTTPNotFound(text="Ссылка устарела.")
    return web.Response(headers={"Content-Type": "video/mp4",
                                 "Content-Disposition": 'attachment; filename="video.mp4"',
                                 "Cache-Control": "private, no-store"})


async def stream_video(request: web.Request) -> web.StreamResponse:
    expire_old()
    token = request.match_info.get("token", "")
    job = STREAMS.get(token)
    if not job:
        raise web.HTTPNotFound(text="Ссылка уже использована или устарела.")
    if SLOTS.locked():
        raise web.HTTPTooManyRequests(text="Сервер занят. Открой ссылку чуть позже.")
    STREAMS.pop(token, None)
    async with SLOTS:
        proc = None
        response = None
        stderr_task = None
        try:
            info = await asyncio.to_thread(_extract_info, job.url)
            sources = select_inputs(info, job.height)
            await asyncio.to_thread(_validate_dns, [s["url"] for s in sources])
            args = ffmpeg_args(info, job.height)
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=256 * 1024)
            stderr_task = asyncio.create_task(proc.stderr.read(65536))
            try:
                first = await asyncio.wait_for(proc.stdout.read(65536), timeout=75)
            except asyncio.TimeoutError:
                raise StreamUnavailable("Сайт слишком долго готовит видеопоток.")
            if not first:
                raise StreamUnavailable("FFmpeg не смог прочитать этот поток.")
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Disposition": 'attachment; filename="video.mp4"',
                         "Cache-Control": "private, no-store",
                         "X-Content-Type-Options": "nosniff"})
            await response.prepare(request)
            await response.write(first)
            deadline = time.monotonic() + MAX_STREAM_SECONDS
            while time.monotonic() < deadline:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(128 * 1024), timeout=60)
                except asyncio.TimeoutError:
                    raise StreamUnavailable("Источник перестал передавать данные.")
                if not chunk:
                    break
                await response.write(chunk)
            if proc.returncode is None:
                await asyncio.wait_for(proc.wait(), timeout=5)
            if proc.returncode:
                log.warning("FFmpeg ended with code %s (source redacted)", proc.returncode)
            await response.write_eof()
            return response
        except (yt_dlp.utils.DownloadError, StreamUnavailable) as exc:
            # User-facing status is possible only before streaming begins.
            if response is None:
                raise web.HTTPBadGateway(text="Не удалось подготовить этот видеопоток.") from exc
            log.warning("Stream interrupted (source redacted)")
            return response
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            # iPhone may disconnect or the user may cancel download.
            raise
        except Exception:
            log.exception("Stream request failed (media URLs not logged)")
            if response is None:
                raise web.HTTPBadGateway(text="Ошибка потоковой загрузки.")
            return response
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if stderr_task:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

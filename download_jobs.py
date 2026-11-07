"""Temporary bounded server-side video preparation for Safari downloads.

The public HTTP endpoint serves only random, short-lived file tokens.
This is a limited beta: apply outbound egress restrictions before mass use.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

import yt_dlp
from aiohttp import web

log = logging.getLogger("video_jobs")
ROOT = Path("/tmp/video-bot-downloads")
MAX_MB = min(max(int(os.getenv("MAX_DOWNLOAD_MB", "250")), 25), 600)
MAX_BYTES = MAX_MB * 1024 * 1024
FILE_TTL = 20 * 60
MAX_SECONDS = 12 * 60
SLOTS = asyncio.Semaphore(1)
FILES: dict[str, tuple[Path, str, float]] = {}
HOURLY: dict[int, list[float]] = {}
DAILY: list[float] = []
CLEAN_TASK: asyncio.Task | None = None
RUNNER: web.AppRunner | None = None


class DownloadTooBig(Exception):
    pass


class DownloadTooSlow(Exception):
    pass


def quota(user_id: int) -> bool:
    now = time.monotonic()
    user = [t for t in HOURLY.get(user_id, []) if now - t < 3600]
    HOURLY[user_id] = user
    DAILY[:] = [t for t in DAILY if now - t < 86400]
    if len(user) >= 3 or len(DAILY) >= 40:
        return False
    user.append(now)
    DAILY.append(now)
    return True


def format_selector(height: int | None) -> str:
    limit = f"[height<={height}]" if height else ""
    # Progressive MP4, split MP4/M4A, then adaptive HLS/DASH fallback.
    return (f"b{limit}[ext=mp4]/bv{limit}[ext=mp4]+ba[ext=m4a]/"
            f"bv{limit}+ba/b{limit}" + ("/b" if not height else ""))


def prepare_video(url: str, height: int | None, work: Path) -> Path:
    started = time.monotonic()

    def limit_progress(data: dict) -> None:
        if time.monotonic() - started > MAX_SECONDS:
            raise DownloadTooSlow("Истекло время подготовки файла.")
        if data.get("status") != "downloading":
            return
        # Estimates from HLS playlists can be inaccurate; use known size and
        # actual accumulated bytes only.
        known = data.get("total_bytes")
        if isinstance(known, (int, float)) and known > MAX_BYTES:
            raise DownloadTooBig(f"Размер превышает текущий лимит {MAX_MB} МБ.")
        current = data.get("downloaded_bytes", 0)
        if isinstance(current, (int, float)) and current > MAX_BYTES:
            raise DownloadTooBig(f"Размер превышает текущий лимит {MAX_MB} МБ.")
        if sum(p.stat().st_size for p in work.iterdir() if p.is_file()) > MAX_BYTES * 2:
            raise DownloadTooBig(f"Размер превышает текущий лимит {MAX_MB} МБ.")

    with yt_dlp.YoutubeDL({
        "format": format_selector(height),
        "format_sort": ["codec:avc:m4a", "ext:mp4:m4a"],
        "outtmpl": str(work / "video.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}},
        "socket_timeout": 18,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 2,
        "max_filesize": MAX_BYTES,
        "merge_output_format": "mp4",
        "progress_hooks": [limit_progress],
        "restrictfilenames": True,
        "writethumbnail": False,
        "writesubtitles": False,
    }) as ydl:
        ydl.download([url])
    candidates = [p for p in work.iterdir()
                  if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if not candidates:
        raise RuntimeError("Сайт не предоставил подходящий видеофайл.")
    video = max(candidates, key=lambda p: p.stat().st_size)
    if not 0 < video.stat().st_size <= MAX_BYTES:
        raise DownloadTooBig(f"Готовый файл превышает {MAX_MB} МБ.")
    return video


def register_file(file: Path, height: int | None) -> str:
    token = secrets.token_urlsafe(24)
    nice = f"video-{height}p{file.suffix.lower()}" if height else f"video{file.suffix.lower()}"
    FILES[token] = (file, nice, time.monotonic() + FILE_TTL)
    return token


def cleanup() -> None:
    now = time.monotonic()
    for token, (file, _, expiry) in list(FILES.items()):
        if expiry > now:
            continue
        FILES.pop(token, None)
        shutil.rmtree(file.parent, ignore_errors=True)


async def periodic_cleanup() -> None:
    try:
        while True:
            await asyncio.sleep(60)
            cleanup()
    except asyncio.CancelledError:
        pass


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="video-bot ok", content_type="text/plain")


async def send_file(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token", "")
    rec = FILES.get(token)
    if not rec:
        raise web.HTTPNotFound(text="Ссылка недоступна или устарела.")
    path, filename, expires = rec
    if expires < time.monotonic() or not path.is_file():
        cleanup()
        raise web.HTTPNotFound(text="Ссылка устарела. Отправь видео боту ещё раз.")
    return web.FileResponse(
        path,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/octet-stream",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })


async def start_server(_application) -> None:
    global RUNNER, CLEAN_TASK
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True, exist_ok=True)
    app = web.Application(client_max_size=2048)
    app.router.add_get("/health", health)
    app.router.add_get("/f/{token}", send_file)
    RUNNER = web.AppRunner(app)
    await RUNNER.setup()
    site = web.TCPSite(RUNNER, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
    await site.start()
    CLEAN_TASK = asyncio.create_task(periodic_cleanup())
    log.info("Temporary download server listening on port %s; cap %d MiB",
             os.getenv("PORT", "8080"), MAX_MB)


async def stop_server(_application) -> None:
    if CLEAN_TASK:
        CLEAN_TASK.cancel()
        await asyncio.gather(CLEAN_TASK, return_exceptions=True)
    if RUNNER:
        await RUNNER.cleanup()
    shutil.rmtree(ROOT, ignore_errors=True)


def public_base_url() -> str | None:
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured.startswith("https://"):
        return configured
    host = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{host}" if host and "/" not in host else None


async def prepare_and_register(url: str, height: int | None) -> tuple[str, str, int]:
    folder = Path(tempfile.mkdtemp(prefix="job-", dir=ROOT))
    try:
        file = await asyncio.to_thread(prepare_video, url, height, folder)
        token = register_file(file, height)
        return token, FILES[token][1], file.stat().st_size
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise

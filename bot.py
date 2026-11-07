"""Telegram video downloader MVP (public, non-DRM videos only)."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("video_bot")

MAX_FILE_BYTES = 49 * 1024 * 1024  # Telegram public Bot API: 50 MB upload cap
MAX_URL_CHARS = 2048
DOWNLOAD_SLOTS = asyncio.Semaphore(2)
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts"}


class VideoAccessError(Exception):
    """User-friendly error."""


def validate_public_url(url: str) -> str:
    """Reject obviously unsafe URL inputs. NOT a substitute for network egress policy."""
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        raise VideoAccessError("Ссылка слишком длинная.")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("scheme/host")
        if parsed.username or parsed.password:
            raise ValueError("credentials")
        if parsed.port not in (None, 80, 443):
            raise ValueError("port")
        hostname = parsed.hostname
        if hostname.lower() == "localhost" or hostname.lower().endswith((".local", ".internal")):
            raise ValueError("local host")
        # Reject internal/private DNS results, as well as private literal IPs.
        addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("no DNS")
        if any(not ipaddress.ip_address(result[4][0]).is_global for result in addresses):
            raise ValueError("private DNS")
    except (ValueError, socket.gaierror, UnicodeError, OSError) as exc:
        raise VideoAccessError("Пришлите обычную публичную ссылку на страницу с видео (http/https).") from exc
    return url


def extract_video(url: str) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "ignoreerrors": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"}:
        raise VideoAccessError("Нужна ссылка на конкретное видео, а не на плейлист.")
    if info.get("is_live"):
        raise VideoAccessError("Прямые трансляции в этой версии не поддерживаются.")
    return info


def available_heights(info: dict) -> list[int]:
    """Expose only resolutions actually reported by the source."""
    heights = {
        int(item["height"])
        for item in info.get("formats", [])
        if isinstance(item, dict)
        and item.get("vcodec") != "none"
        and isinstance(item.get("height"), (int, float))
        and item["height"] > 0
    }
    if not heights and isinstance(info.get("height"), (int, float)):
        heights.add(int(info["height"]))
    return sorted(heights)


def quality_keyboard(key: str, heights: list[int], url: str) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for height in sorted(heights, reverse=True)[:12]:
        row.append(InlineKeyboardButton(f"⬇️ {height}p", callback_data=f"dl:{key}:{height}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⭐ Лучшее доступное", callback_data=f"dl:{key}:best")])
    buttons.append([InlineKeyboardButton("🔗 Открыть страницу видео", url=url)])
    return InlineKeyboardMarkup(buttons)


def download_video(url: str, height: int | None, folder: str) -> Path:
    def stop_large_download(state: dict) -> None:
        if state.get("status") == "downloading" and state.get("downloaded_bytes", 0) > MAX_FILE_BYTES:
            raise VideoAccessError("Файл превышает лимит Telegram (50 МБ). Выберите качество ниже.")

    fmt = "bv+ba/b" if height is None else f"bv[height<={height}]+ba/b[height<={height}]"
    options = {
        "format": fmt,
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "max_filesize": MAX_FILE_BYTES,
        "progress_hooks": [stop_large_download],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS]
    if not files:
        raise VideoAccessError("Видео не удалось сохранить. Возможно, сайту нужен вход или доступ ограничен.")
    # Prefer the combined output (video.mp4/video.mkv) over temporary split streams.
    completed = [p for p in files if p.stem == "video"]
    path = max(completed or files, key=lambda p: p.stat().st_size)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise VideoAccessError("Готовый файл больше 50 МБ. Выберите более низкое качество.")
    if path.stat().st_size == 0:
        raise VideoAccessError("Видео оказалось пустым.")
    return path


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 Привет! Пришли ссылку на общедоступное видео. "
        "Я проверю доступные качества и предложу скачать файл.\n\n"
        "Сайты с DRM-защитой и закрытые видео не поддерживаются. "
        "Через стандартный Telegram Bot API можно отправить файл до 50 МБ."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    if context.user_data.get("busy"):
        await message.reply_text("⏳ Предыдущее видео ещё обрабатывается.")
        return
    context.user_data["busy"] = True
    try:
        url = await asyncio.to_thread(validate_public_url, message.text)
        status = await message.reply_text("🔎 Проверяю ссылку и доступные качества…")
        info = await asyncio.to_thread(extract_video, url)
        heights = available_heights(info)
        key = secrets.token_hex(5)
        # Keep only recent links per user; callback data contains a key, never the URL.
        videos = context.user_data.setdefault("videos", {})
        if len(videos) >= 10:
            videos.pop(next(iter(videos)))
        videos[key] = {"url": url, "title": str(info.get("title") or "Видео")[:150]}
        title = videos[key]["title"]
        choices = ", ".join(f"{x}p" for x in heights) if heights else "авто"
        await status.edit_text(
            f"🎬 {title}\n\nДоступные качества: {choices}.\nВыберите нужное:",
            reply_markup=quality_keyboard(key, heights, url),
        )
    except VideoAccessError as exc:
        await message.reply_text(f"⚠️ {exc}")
    except yt_dlp.utils.DownloadError:
        await message.reply_text("⚠️ Не удалось прочитать видео. Проверьте ссылку и доступность видео без входа.")
    except Exception:
        log.exception("Video inspection failed")
        await message.reply_text("⚠️ Ошибка при обработке ссылки. Попробуйте другую.")
    finally:
        context.user_data["busy"] = False


async def handle_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    _, key, quality = query.data.split(":", 2)
    video = context.user_data.get("videos", {}).get(key)
    if not video:
        await query.message.reply_text("Ссылка устарела. Пришлите её ещё раз.")
        return
    if context.user_data.get("busy"):
        await query.message.reply_text("⏳ Подождите завершения предыдущей загрузки.")
        return
    context.user_data["busy"] = True
    try:
        height = None if quality == "best" else int(quality)
        status = await query.message.reply_text(f"⏬ Скачиваю {quality if height is None else str(height) + 'p'}…")
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="video-bot-") as folder:
                path = await asyncio.to_thread(download_video, video["url"], height, folder)
                await status.edit_text("📤 Отправляю файл…")
                await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_DOCUMENT)
                with path.open("rb") as handle:
                    await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=handle,
                        filename=path.name,
                        caption=video["title"][:1024],
                        read_timeout=120,
                        write_timeout=120,
                    )
        await status.edit_text("✅ Готово! Можете выбрать другое качество по кнопкам выше.")
    except VideoAccessError as exc:
        await query.message.reply_text(f"⚠️ {exc}")
    except (yt_dlp.utils.DownloadError, TelegramError):
        log.exception("Download/send failed")
        await query.message.reply_text("⚠️ Не удалось скачать или отправить видео. Попробуйте другое качество.")
    except Exception:
        log.exception("Unexpected download failure")
        await query.message.reply_text("⚠️ Произошла ошибка. Попробуйте другое качество или ссылку.")
    finally:
        context.user_data["busy"] = False


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN в файле .env")
    app = ApplicationBuilder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_link))
    app.add_handler(CallbackQueryHandler(handle_quality, pattern=r"^dl:[0-9a-f]{10}:(best|[0-9]+)$"))
    print("Бот запущен. Откройте его в Telegram и отправьте ссылку.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

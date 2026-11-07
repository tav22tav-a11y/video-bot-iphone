"""Video Bot 2.0 (beta): prefer direct-to-iPhone links; Telegram file is optional.

IMPORTANT: direct URLs are best-effort and limited to public progressive files.
No DRM/private access bypass. Media is not saved on our server in direct mode.
"""
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
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from format_selector import human_size
import download_jobs
import stream_jobs

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# Redact HTTP URLs (especially Telegram bot token) from library logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("video_bot")

MAX_TELEGRAM_FILE_BYTES = 49 * 1024 * 1024
MAX_URL_LENGTH = 2048
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts"}
DOWNLOAD_SLOTS = asyncio.Semaphore(2)
ALLOW_TELEGRAM_FALLBACK = os.getenv("ALLOW_TELEGRAM_FALLBACK", "true").lower() == "true"


class FriendlyError(Exception):
    pass


def check_public_url(url: str) -> str:
    """Fast validation. For a public bot also restrict outbound network at host level.

    yt-dlp may follow other URLs returned by the source; this does not protect
    against every SSRF/DNS-rebinding path. Use an outbound firewall in prod.
    """
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise FriendlyError("Слишком длинная ссылка.")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("scheme/host")
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError("credentials/port")
        host = parsed.hostname.lower().strip(".")
        if host == "localhost" or host.endswith((".local", ".internal")):
            raise ValueError("private hostname")
        answers = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        if not answers or any(not ipaddress.ip_address(x[4][0]).is_global for x in answers):
            raise ValueError("private DNS")
    except (ValueError, socket.gaierror, OSError, UnicodeError) as exc:
        raise FriendlyError("Нужна публичная ссылка на видео (https://...).") from exc
    return url


def inspect_video(url: str) -> dict:
    with yt_dlp.YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "ignoreerrors": False,
        "js_runtimes": {"node": {}},
    }) as ydl:
        data = ydl.extract_info(url, download=False)
    if not isinstance(data, dict) or data.get("_type") in ("playlist", "multi_video"):
        raise FriendlyError("Отправь ссылку на одно видео, не на плейлист.")
    if data.get("is_live"):
        raise FriendlyError("Прямые трансляции не поддерживаются.")
    return data


def available_heights(info: dict) -> list[int]:
    formats = info.get("formats") or []
    heights = {int(x["height"]) for x in formats if isinstance(x, dict)
               and x.get("vcodec") != "none" and isinstance(x.get("height"), (float, int))
               and x["height"] > 0}
    if not heights and isinstance(info.get("height"), (float, int)):
        heights.add(int(info["height"]))
    return sorted(heights, reverse=True)


def quality_keyboard(key: str, info: dict, original_url: str) -> tuple[InlineKeyboardMarkup, int]:
    """Choose a resolution BEFORE beginning server-side preparation."""
    heights = available_heights(info)[:9]
    buttons = [[InlineKeyboardButton(f"📱 Скачать {height}p в Файлы",
                  callback_data=f"save:{key}:{height}")]
               for height in heights]
    buttons.append([InlineKeyboardButton("📱 Авто / лучшее качество",
                                         callback_data=f"save:{key}:best")])
    if ALLOW_TELEGRAM_FALLBACK:
        buttons.append([InlineKeyboardButton("📨 Маленький файл в Telegram (до 49 МБ)",
                                            callback_data=f"tg:{key}")])
    buttons.append([InlineKeyboardButton("❔ Где найти скачанное видео?", callback_data="help:files")])
    return InlineKeyboardMarkup(buttons), len(heights)


def download_small_video(url: str, height: int | None, folder: str) -> Path:
    def check_size(d: dict) -> None:
        if d.get("status") == "downloading" and d.get("downloaded_bytes", 0) > MAX_TELEGRAM_FILE_BYTES:
            raise FriendlyError("Видео больше лимита Telegram. Попробуй прямую загрузку в Файлы.")
    format_spec = "bv+ba/b" if height is None else f"bv[height<={height}]+ba/b[height<={height}]"
    with yt_dlp.YoutubeDL({
        "format": format_spec,
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "js_runtimes": {"node": {}},
        "max_filesize": MAX_TELEGRAM_FILE_BYTES,
        "merge_output_format": "mp4",
        "progress_hooks": [check_size],
    }) as ydl:
        ydl.download([url])
    files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS]
    if not files:
        raise FriendlyError("Не получилось сохранить маленький файл. Попробуй другое качество.")
    path = max(files, key=lambda p: p.stat().st_size)
    if not 0 < path.stat().st_size <= MAX_TELEGRAM_FILE_BYTES:
        raise FriendlyError("Готовый файл превышает 49 МБ. Попробуй прямую загрузку в Файлы.")
    return path


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Привет! 📱 Пришли ссылку на публичное видео.\n\n"
        "1. Выбери качество (360p, 720p, 1080p и другие).\n"
        "2. Нажми «Скачать в Файлы». В Safari начнётся загрузка прямо в память телефона.\n"
        "3. Для HLS/DASH я попробую собирать поток на лету — без сохранения файла на сервере.\n\n"
        "Для потоковой загрузки нет лимита размера файла в мегабайтах, "
        "но Railway ограничивает продолжительность соединения. "
        "Прерванную загрузку пока нельзя продолжить с того же места. "
        "DRM, закрытый доступ и некоторые сайты не поддерживаются."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if context.user_data.get("busy"):
        await msg.reply_text("⏳ Предыдущее видео ещё обрабатывается.")
        return
    context.user_data["busy"] = True
    try:
        url = await asyncio.to_thread(check_public_url, msg.text)
        status = await msg.reply_text("🔎 Проверяю доступные форматы…")
        info = await asyncio.to_thread(inspect_video, url)
        key = secrets.token_hex(5)
        videos = context.user_data.setdefault("videos", {})
        if len(videos) >= 10:
            videos.pop(next(iter(videos)))
        videos[key] = {"url": url, "title": str(info.get("title") or "Видео")[:150],
                       "heights": available_heights(info)}
        kb, quality_count = quality_keyboard(key, info, url)
        title = videos[key]["title"]
        text = (f"🎬 {title}\n\n"
                "Выбери качество ниже, затем нажми кнопку загрузки. "
                "Safari получит доступный MP4, HLS или DASH по потоку, "
                "не сохраняя полное видео на сервере.\n\n"
                "Нет фиксированного ограничения на размер файла. "
                "Ссылка начнёт загрузку при открытии в Safari и действительна 10 минут.")
        await status.edit_text(text, reply_markup=kb)
    except FriendlyError as exc:
        await msg.reply_text(f"⚠️ {exc}")
    except yt_dlp.utils.DownloadError:
        await msg.reply_text("⚠️ Сайт не дал доступ к видео или пока не поддерживается. Отправь прямую ссылку на видеоролик.")
    except Exception:
        log.exception("Failed to inspect video")
        await msg.reply_text("⚠️ Ошибка при проверке ссылки.")
    finally:
        context.user_data["busy"] = False


async def help_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.reply_text(
            "📱 Как сохранить на iPhone:\n"
            "1. Отправь ссылку и выбери качество.\n"
            "2. После выбора качества нажми «📥 Скачать в Файлы».\n"
            "3. Открой ссылку в Safari, если Telegram открывает встроенный браузер.\n"
            "4. В Safari начнётся загрузка; файл найдёшь в «Файлы» → «Загрузки».\n"
            "5. Если интернет оборвётся, нужно запустить загрузку заново.\n"
            "Если видео не обрабатывается, сайт может не отдавать публичный поток или использовать DRM."
        )


async def save_to_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate an on-demand HTTPS streaming link without creating a temp file."""
    query = update.callback_query
    await query.answer()
    _, key, quality = query.data.split(":", 2)
    video = context.user_data.get("videos", {}).get(key)
    if not video:
        await query.message.reply_text("Ссылка устарела. Пришли видео снова.")
        return
    base = download_jobs.public_base_url()
    if not base:
        await query.message.reply_text("⚠️ Публичный адрес сервера пока не настроен.")
        return
    if not download_jobs.quota(query.from_user.id):
        await query.message.reply_text("⚠️ Слишком много загрузок за короткое время. Попробуй позже.")
        return
    height = None if quality == "best" else int(quality)
    try:
        token = stream_jobs.register_stream(video["url"], height)
        address = f"{base}/stream/{token}"
        label = f"{height}p" if height else "лучшее доступное"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"📥 Скачать {label} в Файлы", url=address)
        ]])
        await query.message.reply_text(
            f"✅ Выбрано качество: {label}.\\n\\n"
            "Нажми кнопку ниже и открой ссылку в Safari. "
            "Сервер будет передавать видео прямо на телефон по мере обработки. "
            "Файл не загружается в Telegram и не сохраняется целиком на сервере.\\n\\n"
            "Нет фиксированного лимита размера, но время загрузки ограничено примерно 13 минутами. "
            "При обрыве загрузку придётся начать заново. "
            "Ссылка действует 10 минут и рассчитана на одно скачивание.",
            reply_markup=markup)
    except stream_jobs.StreamUnavailable as exc:
        await query.message.reply_text(f"⚠️ {exc}")


async def telegram_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    video = context.user_data.get("videos", {}).get(key)
    if not video:
        await query.message.reply_text("Ссылка устарела. Пришли её заново.")
        return
    buttons = []
    heights = video["heights"][:10]
    row = []
    for height in heights:
        row.append(InlineKeyboardButton(f"📨 {height}p", callback_data=f"dl:{key}:{height}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("📨 Авто (до 49 МБ)", callback_data=f"dl:{key}:best")])
    await query.message.reply_text("Выбери качество. Telegram ограничивает размер файла 50 МБ.",
                                   reply_markup=InlineKeyboardMarkup(buttons))


async def telegram_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not ALLOW_TELEGRAM_FALLBACK:
        await query.message.reply_text("Отправка файлов в Telegram отключена.")
        return
    _, key, quality = query.data.split(":", 2)
    video = context.user_data.get("videos", {}).get(key)
    if not video:
        await query.message.reply_text("Ссылка устарела. Пришли её заново.")
        return
    if context.user_data.get("busy"):
        await query.message.reply_text("⏳ Сначала заверши прошлую загрузку.")
        return
    context.user_data["busy"] = True
    try:
        status = await query.message.reply_text("⏬ Скачиваю маленький файл…")
        height = int(quality) if quality != "best" else None
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="video-bot-") as folder:
                file = await asyncio.to_thread(download_small_video, video["url"], height, folder)
                await status.edit_text("📤 Отправляю видео…")
                await context.bot.send_chat_action(query.message.chat_id, ChatAction.UPLOAD_DOCUMENT)
                with file.open("rb") as fp:
                    await context.bot.send_document(chat_id=query.message.chat_id,
                                                    document=fp, filename=file.name,
                                                    caption=video["title"],
                                                    read_timeout=120, write_timeout=120)
        await status.edit_text("✅ Готово!")
    except FriendlyError as exc:
        await query.message.reply_text(f"⚠️ {exc}")
    except (TelegramError, yt_dlp.utils.DownloadError):
        log.exception("Small download failed")
        await query.message.reply_text("⚠️ Не удалось отправить файл; попробуй меньший размер.")
    except Exception:
        log.exception("Unexpected download failure")
        await query.message.reply_text("⚠️ Ошибка скачивания.")
    finally:
        context.user_data["busy"] = False


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN как секретную переменную Railway.")
    app = (ApplicationBuilder().token(token).concurrent_updates(True)
           .post_init(download_jobs.start_server)
           .post_shutdown(download_jobs.stop_server).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_link))
    app.add_handler(CallbackQueryHandler(help_files, pattern=r"^help:files$"))
    app.add_handler(CallbackQueryHandler(save_to_files, pattern=r"^save:[0-9a-f]{10}:(best|[0-9]+)$"))
    app.add_handler(CallbackQueryHandler(telegram_menu, pattern=r"^tg:[0-9a-f]{10}$"))
    app.add_handler(CallbackQueryHandler(telegram_download, pattern=r"^dl:[0-9a-f]{10}:(best|[0-9]+)$"))
    print("Video Bot 2.1: server-side stream preparation started", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

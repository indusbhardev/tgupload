import os
import re
import time
import math
import base64
import asyncio
import mimetypes
import urllib.parse

# CRITICAL FIX FOR PYTHON 3.14+:
# Create and register the event loop BEFORE importing Pyrogram
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from aiohttp import web, ClientSession, ClientTimeout
from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import FloodWait, RPCError

# ==================== CONFIGURATION ====================
API_ID_RAW = os.environ.get("API_ID", "0").strip()
API_ID = int(API_ID_RAW) if API_ID_RAW.isdigit() else 0
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

# Render automatically populates RENDER_EXTERNAL_URL on Web Services
RENDER_EXTERNAL_URL = (
    os.environ.get("BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")
DETECTED_BASE_URL = ""

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

ACTIVE_TASKS = set()

app = Client(
    "url_uploader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)


# ==================== HELPER FUNCTIONS ====================
def get_base_url() -> str:
    return RENDER_EXTERNAL_URL or DETECTED_BASE_URL


def human_readable_size(size_bytes: int) -> str:
    if size_bytes is None or size_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    return f"{round(size_bytes / p, 2)} {units[i]}"


def format_time(seconds: float) -> str:
    if seconds <= 0 or math.isinf(seconds) or math.isnan(seconds):
        return "0s"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def extract_filename(url: str, headers: dict) -> str:
    cd = headers.get("Content-Disposition", "")
    if cd:
        match = re.findall(
            r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)',
            cd,
            flags=re.IGNORECASE,
        )
        if match:
            return urllib.parse.unquote(match[0].strip())

    parsed_path = urllib.parse.urlparse(url).path
    filename = os.path.basename(urllib.parse.unquote(parsed_path))

    if not filename or "." not in filename:
        content_type = headers.get("Content-Type", "").split(";")[0].strip()
        ext = mimetypes.guess_extension(content_type) or ".bin"
        filename = (filename or f"file_{int(time.time())}") + ext

    filename = re.sub(r'[\\/*?:"<>|]', "_", filename)
    return filename[:180]


def detect_format_info(filename: str, content_type_header: str) -> tuple[str, str]:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    guessed_mime, _ = mimetypes.guess_type(filename)
    mime_type = (
        (content_type_header.split(";")[0].strip() if content_type_header else "")
        or guessed_mime
        or "application/octet-stream"
    )
    ext_label = ext.upper() if ext else "UNKNOWN"
    return f"{ext_label} ({mime_type})", ext


def extract_telegram_media_details(message: Message) -> tuple[str, str, int, str]:
    """
    Extracts (filename, format_label, file_size, mime_type) from any Telegram media message.
    """
    media = (
        message.document
        or message.video
        or message.audio
        or message.animation
        or message.voice
        or message.video_note
        or message.sticker
        or message.photo
    )
    if not media:
        raise ValueError("Unsupported or empty media object in message.")

    file_size = getattr(media, "file_size", 0) or 0
    mime_type = getattr(media, "mime_type", "") or ""
    filename = getattr(media, "file_name", None)

    if message.photo and not filename:
        mime_type = "image/jpeg"
        filename = f"photo_{message.id}.jpg"
    elif message.voice and not filename:
        mime_type = mime_type or "audio/ogg"
        filename = f"voice_{message.id}.ogg"
    elif message.video_note and not filename:
        mime_type = mime_type or "video/mp4"
        filename = f"video_note_{message.id}.mp4"
    elif message.sticker and not filename:
        ext = mimetypes.guess_extension(mime_type) or ".webp"
        filename = f"sticker_{message.id}{ext}"
    elif not filename:
        ext = mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"file_{message.id}{ext}"

    filename = re.sub(r'[\\/*?:"<>|]', "_", filename)
    format_label, _ = detect_format_info(filename, mime_type)
    resolved_mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

    return filename, format_label, file_size, resolved_mime


def encode_file_payload(chat_id: int, message_id: int) -> str:
    raw = f"{chat_id}:{message_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def decode_file_payload(payload: str) -> tuple[int, int]:
    padding = "=" * (-len(payload) % 4)
    raw = base64.urlsafe_b64decode((payload + padding).encode("utf-8")).decode("utf-8")
    chat_id_str, msg_id_str = raw.split(":")
    return int(chat_id_str), int(msg_id_str)


def build_server_links(bot_username: str, chat_id: int, message_id: int, filename: str) -> tuple[str, str]:
    """
    Returns (direct_dl_link, tg_file_link) pointing to this Render server and Telegram bot.
    """
    payload = encode_file_payload(chat_id, message_id)
    tg_file_link = f"https://t.me/{bot_username}?start=f_{payload}"

    base_url = get_base_url()
    safe_url_filename = urllib.parse.quote(filename)
    if base_url:
        direct_dl_link = f"{base_url}/dl/{payload}/{safe_url_filename}"
    else:
        direct_dl_link = tg_file_link

    return direct_dl_link, tg_file_link


async def update_progress_ui(
    status_msg: Message,
    stage: str,
    current: int,
    total: int,
    start_time: float,
    last_edit: dict,
    task_id: str,
    filename: str,
):
    if task_id not in ACTIVE_TASKS:
        raise asyncio.CancelledError("Transfer cancelled by user.")

    now = time.time()
    if (now - last_edit["time"] < 3.5) and (current != total):
        return

    elapsed = max(now - start_time, 0.001)
    speed = current / elapsed
    percentage = (current * 100 / total) if total > 0 else 0
    eta = ((total - current) / speed) if (total > 0 and speed > 0) else 0

    filled = int(percentage // 10)
    bar = "█" * filled + "░" * (10 - filled)

    text = (
        f"**{stage}**\n\n"
        f"📄 **File:** `{filename}`\n"
        f"**[{bar}]** `{percentage:.1f}%`\n"
        f"📦 **Size:** `{human_readable_size(current)} / {human_readable_size(total) if total else 'Unknown'}`\n"
        f"⚡ **Speed:** `{human_readable_size(int(speed))}/s`\n"
        f"⏳ **ETA:** `{format_time(eta)}`"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🔄 {stage}: {percentage:.1f}% ({human_readable_size(int(speed))}/s)",
                    callback_data="noop",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel Transfer", callback_data=f"cancel_{task_id}"
                )
            ],
        ]
    )

    try:
        await status_msg.edit_text(text, reply_markup=keyboard)
        last_edit["time"] = now
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
    except Exception:
        pass


# ==================== BOT HANDLERS ====================
@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    try:
        if len(message.command) > 1 and message.command[1].startswith("f_"):
            payload = message.command[1][2:]
            try:
                origin_chat_id, origin_msg_id = decode_file_payload(payload)
                await client.copy_message(
                    chat_id=message.chat.id,
                    from_chat_id=origin_chat_id,
                    message_id=origin_msg_id,
                )
                return
            except Exception as e:
                await message.reply_text(
                    f"❌ **Failed to retrieve Telegram file link.**\n"
                    f"**Error Type:** `{type(e).__name__}`\n"
                    f"**Details:** `{str(e)}`"
                )
                return

        await message.reply_text(
            "👋 **Welcome to the File & Direct Link Uploader Bot!**\n\n"
            "**How to use me:**\n"
            "1️⃣ **Send a Direct Download Link** (`http://` or `https://`) — I will download and upload it to Telegram with live progress and generate your Server Direct Download Link.\n"
            "2️⃣ **Upload any Local File** (Document, ZIP, RAR, Video, Audio, Photo, APK, etc.) — I will generate its **Server Direct Download Link**, **Telegram File Link**, **Format Type**, and **File Size**!"
        )
    except Exception as e:
        await message.reply_text(
            f"⚠️ **Error in /start handler**\n"
            f"**Error Type:** `{type(e).__name__}`\n"
            f"**Message:** `{str(e)}`"
        )


@app.on_callback_query(filters.regex(r"^cancel_"))
async def cancel_callback(client: Client, query: CallbackQuery):
    task_id = query.data.split("_", 1)[1]
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS.discard(task_id)
        await query.answer("Cancelling transfer...", show_alert=False)
    else:
        await query.answer("Transfer is no longer active.", show_alert=False)


@app.on_callback_query(filters.regex(r"^noop$"))
async def noop_callback(client: Client, query: CallbackQuery):
    await query.answer("Processing...", show_alert=False)


# ==================== HANDLER 1: LOCAL FILES UPLOADED TO BOT ====================
MEDIA_FILTER = (
    filters.document
    | filters.video
    | filters.audio
    | filters.photo
    | filters.animation
    | filters.voice
    | filters.video_note
    | filters.sticker
)


@app.on_message(MEDIA_FILTER)
async def local_file_handler(client: Client, message: Message):
    status_msg = None
    try:
        status_msg = await message.reply_text(
            "⚙️ **Processing your local file and generating links...**",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔄 Processing Local File...", callback_data="noop")]]
            ),
            quote=True,
        )
    except Exception as e:
        await message.reply_text(
            f"❌ **Message Error (Failed to send status message)**\n"
            f"🔸 **Error Type:** `{type(e).__name__}`\n"
            f"🔸 **Error Details:** `{str(e)}`"
        )
        return

    try:
        filename, format_label, file_size, _ = extract_telegram_media_details(message)
        readable_size = human_readable_size(file_size)

        sent_msg = await message.copy(
            chat_id=message.chat.id,
            reply_to_message_id=message.id,
        )

        bot_me = await client.get_me()
        direct_dl_link, tg_file_link = build_server_links(
            bot_username=bot_me.username,
            chat_id=sent_msg.chat.id,
            message_id=sent_msg.id,
            filename=filename,
        )

        final_caption = (
            f"📄 **Title:** `{filename}`\n"
            f"🗂 **Format Type:** `{format_label}`\n"
            f"📦 **File Size:** `{readable_size}`\n"
            f"🌐 **Direct Download Link:** [Download File]({direct_dl_link})\n"
            f"✈️ **Telegram File Link:** [Get File in Telegram]({tg_file_link})"
        )

        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🌐 Direct Download Link", url=direct_dl_link),
                    InlineKeyboardButton("✈️ Telegram File Link", url=tg_file_link),
                ]
            ]
        )

        await sent_msg.edit_caption(final_caption, reply_markup=buttons)
        await status_msg.delete()

    except Exception as e:
        err_text = (
            f"❌ **Local File Processing Error**\n\n"
            f"🔸 **Error Type:** `{type(e).__name__}`\n"
            f"🔸 **Error Details:** `{str(e)}`"
        )
        try:
            await status_msg.edit_text(err_text)
        except Exception as msg_err:
            await message.reply_text(
                f"{err_text}\n🔸 **Secondary Message Error:** `{type(msg_err).__name__}: {msg_err}`"
            )


# ==================== HANDLER 2: DIRECT DOWNLOADABLE URLS ====================
@app.on_message(filters.text & filters.regex(r"https?://[^\s]+"))
async def url_upload_handler(client: Client, message: Message):
    url_match = re.search(r"https?://[^\s]+", message.text)
    if not url_match:
        return

    url = url_match.group(0)
    task_id = f"{message.chat.id}_{message.id}"
    ACTIVE_TASKS.add(task_id)
    local_filepath = None
    status_msg = None

    try:
        status_msg = await message.reply_text(
            "🔍 **Connecting to URL and inspecting headers...**",
            quote=True,
        )
    except Exception as e:
        ACTIVE_TASKS.discard(task_id)
        await message.reply_text(
            f"❌ **Message Error (Failed to initialize status message)**\n"
            f"**Error Type:** `{type(e).__name__}`\n"
            f"**Details:** `{str(e)}`"
        )
        return

    # ---------------- STAGE 1: DOWNLOAD FROM DIRECT LINK ----------------
    try:
        timeout = ClientTimeout(total=3600, connect=30)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        async with ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status} - {response.reason}")

                filename = extract_filename(str(response.url), response.headers)
                content_type = response.headers.get("Content-Type", "")
                format_label, ext = detect_format_info(filename, content_type)
                total_size = int(response.headers.get("Content-Length", 0))

                if total_size > 2000 * 1024 * 1024:
                    raise ValueError(
                        f"File size ({human_readable_size(total_size)}) exceeds Telegram's 2 GB limit."
                    )

                local_filepath = os.path.join(DOWNLOAD_DIR, f"{task_id}_{filename}")
                downloaded = 0
                dl_start = time.time()
                last_edit = {"time": 0.0}

                with open(local_filepath, "wb") as f:
                    async for chunk in response.content.iter_chunked(512 * 1024):
                        if task_id not in ACTIVE_TASKS:
                            raise asyncio.CancelledError("Download cancelled by user.")
                        f.write(chunk)
                        downloaded += len(chunk)
                        await update_progress_ui(
                            status_msg=status_msg,
                            stage="⬇️ Downloading from Link",
                            current=downloaded,
                            total=total_size,
                            start_time=dl_start,
                            last_edit=last_edit,
                            task_id=task_id,
                            filename=filename,
                        )

        actual_size = os.path.getsize(local_filepath)
        readable_size = human_readable_size(actual_size)

    except Exception as e:
        ACTIVE_TASKS.discard(task_id)
        if local_filepath and os.path.exists(local_filepath):
            os.remove(local_filepath)
        err_text = (
            f"❌ **Download Error**\n\n"
            f"🔸 **Error Type:** `{type(e).__name__}`\n"
            f"🔸 **Error Details:** `{str(e)}`\n"
            f"🔗 **Source Link:** {url}"
        )
        try:
            await status_msg.edit_text(err_text, disable_web_page_preview=True)
        except Exception as msg_err:
            await message.reply_text(
                f"{err_text}\n🔸 **Secondary Message Error:** `{type(msg_err).__name__}: {msg_err}`",
                disable_web_page_preview=True,
            )
        return

    # ---------------- STAGE 2: UPLOAD TO TELEGRAM ----------------
    try:
        ul_start = time.time()
        last_edit = {"time": 0.0}

        async def upload_progress(current: int, total: int):
            await update_progress_ui(
                status_msg=status_msg,
                stage="⬆️ Uploading to Telegram",
                current=current,
                total=total,
                start_time=ul_start,
                last_edit=last_edit,
                task_id=task_id,
                filename=filename,
            )

        initial_caption = (
            f"📄 **Title:** `{filename}`\n"
            f"🗂 **Format Type:** `{format_label}`\n"
            f"📦 **File Size:** `{readable_size}`\n"
            f"⏳ **Generating Server Direct Download Link...**"
        )

        sent_msg = None
        try:
            if ext in ("jpg", "jpeg", "png", "webp") and actual_size <= 10 * 1024 * 1024:
                sent_msg = await client.send_photo(
                    chat_id=message.chat.id,
                    photo=local_filepath,
                    caption=initial_caption,
                    reply_to_message_id=message.id,
                    progress=upload_progress,
                )
            elif ext in ("mp4", "mkv", "mov", "webm"):
                sent_msg = await client.send_video(
                    chat_id=message.chat.id,
                    video=local_filepath,
                    caption=initial_caption,
                    file_name=filename,
                    supports_streaming=True,
                    reply_to_message_id=message.id,
                    progress=upload_progress,
                )
            elif ext in ("mp3", "m4a", "flac", "wav", "ogg", "aac"):
                sent_msg = await client.send_audio(
                    chat_id=message.chat.id,
                    audio=local_filepath,
                    caption=initial_caption,
                    file_name=filename,
                    reply_to_message_id=message.id,
                    progress=upload_progress,
                )
            else:
                sent_msg = await client.send_document(
                    chat_id=message.chat.id,
                    document=local_filepath,
                    caption=initial_caption,
                    file_name=filename,
                    force_document=True,
                    reply_to_message_id=message.id,
                    progress=upload_progress,
                )
        except RPCError:
            sent_msg = await client.send_document(
                chat_id=message.chat.id,
                document=local_filepath,
                caption=initial_caption,
                file_name=filename,
                force_document=True,
                reply_to_message_id=message.id,
                progress=upload_progress,
            )

    except Exception as e:
        err_text = (
            f"❌ **Upload Error**\n\n"
            f"🔸 **Error Type:** `{type(e).__name__}`\n"
            f"🔸 **Error Details:** `{str(e)}`\n"
            f"📄 **File:** `{filename}`"
        )
        try:
            await status_msg.edit_text(err_text, disable_web_page_preview=True)
        except Exception as msg_err:
            await message.reply_text(
                f"{err_text}\n🔸 **Secondary Message Error:** `{type(msg_err).__name__}: {msg_err}`",
                disable_web_page_preview=True,
            )
        return
    finally:
        ACTIVE_TASKS.discard(task_id)
        if local_filepath and os.path.exists(local_filepath):
            os.remove(local_filepath)

    # ---------------- STAGE 3: ATTACH SERVER DIRECT LINK & TELEGRAM LINK ----------------
    try:
        bot_me = await client.get_me()
        direct_dl_link, tg_file_link = build_server_links(
            bot_username=bot_me.username,
            chat_id=sent_msg.chat.id,
            message_id=sent_msg.id,
            filename=filename,
        )

        final_caption = (
            f"📄 **Title:** `{filename}`\n"
            f"🗂 **Format Type:** `{format_label}`\n"
            f"📦 **File Size:** `{readable_size}`\n"
            f"🌐 **Direct Download Link:** [Download File]({direct_dl_link})\n"
            f"✈️ **Telegram File Link:** [Get File in Telegram]({tg_file_link})"
        )

        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🌐 Direct Download Link", url=direct_dl_link),
                    InlineKeyboardButton("✈️ Telegram File Link", url=tg_file_link),
                ]
            ]
        )

        await sent_msg.edit_caption(final_caption, reply_markup=buttons)
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(
            f"⚠️ **File uploaded, but failed to update final metadata message.**\n"
            f"🔸 **Error Type:** `{type(e).__name__}`\n"
            f"🔸 **Error Details:** `{str(e)}`"
        )


# ==================== RENDER WEB SERVER & DIRECT STREAMING ====================
async def health_check(request: web.Request):
    global DETECTED_BASE_URL
    host = request.headers.get("Host", "")
    if host and not host.startswith(("127.0.0.1", "localhost", "0.0.0.0")):
        proto = request.headers.get("X-Forwarded-Proto", "https")
        DETECTED_BASE_URL = f"{proto}://{host}"
    return web.Response(text="Telegram File & URL Uploader Bot is running!", status=200)


async def stream_telegram_file(request: web.Request):
    """
    Streams any Telegram file uploaded to the bot directly to a web browser.
    URL pattern: /dl/{payload}/{filename}
    """
    payload = request.match_info.get("payload", "")
    try:
        chat_id, msg_id = decode_file_payload(payload)
        msg = await app.get_messages(chat_id, msg_id)
        if not msg or not msg.media:
            return web.Response(text="404: File not found on Telegram.", status=404)

        filename, _, file_size, mime_type = extract_telegram_media_details(msg)

        headers = {
            "Content-Type": mime_type,
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if file_size > 0:
            headers["Content-Length"] = str(file_size)

        response = web.StreamResponse(status=200, reason="OK", headers=headers)
        await response.prepare(request)

        async for chunk in app.stream_media(msg):
            await response.write(chunk)

        await response.write_eof()
        return response

    except Exception as e:
        return web.Response(
            text=f"Download Error ({type(e).__name__}): {str(e)}",
            status=500,
        )


async def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:
        raise ValueError(
            "Missing API_ID, API_HASH, or BOT_TOKEN in Render Environment Variables."
        )

    server = web.Application()
    server.router.add_get("/", health_check)
    server.router.add_get("/dl/{payload}/{filename}", stream_telegram_file)

    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web & Direct-Stream server listening on port {PORT}")

    await app.start()
    me = await app.get_me()
    print(f"Bot @{me.username} started successfully!")
    await idle()
    await app.stop()


if __name__ == "__main__":
    loop.run_until_complete(main())

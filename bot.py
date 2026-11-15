import os
import re
import time
import math
import base64
import asyncio
import mimetypes
import urllib.parse
from aiohttp import web, ClientSession, ClientTimeout
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import FloodWait, RPCError

# ==================== CONFIGURATION ====================
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Track active tasks to allow users to cancel in-progress transfers
ACTIVE_TASKS = set()

app = Client(
    "url_uploader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ==================== HELPER FUNCTIONS ====================
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
    # 1. Try Content-Disposition header
    cd = headers.get("Content-Disposition", "")
    if cd:
        match = re.findall(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', cd, flags=re.IGNORECASE)
        if match:
            return urllib.parse.unquote(match[0].strip())

    # 2. Fallback to URL path
    parsed_path = urllib.parse.urlparse(url).path
    filename = os.path.basename(urllib.parse.unquote(parsed_path))

    # 3. Fallback if URL has no filename or extension
    if not filename or "." not in filename:
        content_type = headers.get("Content-Type", "").split(";")[0].strip()
        ext = mimetypes.guess_extension(content_type) or ".bin"
        filename = (filename or f"file_{int(time.time())}") + ext

    # Sanitize illegal filesystem characters
    filename = re.sub(r'[\\/*?:"<>|]', "_", filename)
    return filename[:180]


def detect_format_info(filename: str, content_type_header: str) -> tuple[str, str]:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    guessed_mime, _ = mimetypes.guess_type(filename)
    mime_type = (content_type_header.split(";")[0].strip() if content_type_header else "") or guessed_mime or "application/octet-stream"
    ext_label = ext.upper() if ext else "UNKNOWN"
    return f"{ext_label} ({mime_type})", ext


def encode_file_payload(chat_id: int, message_id: int) -> str:
    raw = f"{chat_id}:{message_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def decode_file_payload(payload: str) -> tuple[int, int]:
    padding = "=" * (-len(payload) % 4)
    raw = base64.urlsafe_b64decode((payload + padding).encode("utf-8")).decode("utf-8")
    chat_id_str, msg_id_str = raw.split(":")
    return int(chat_id_str), int(msg_id_str)


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
    # Throttle edits to once every 3.5 seconds to prevent Telegram FloodWait
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
        # Handle deep-link Telegram File Link clicks
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
            "👋 **Welcome to the Direct Link Uploader Bot!**\n\n"
            "Send me any **direct downloadable URL** (`http://` or `https://`) and I will:\n"
            "1. Download and upload it to Telegram with live progress\n"
            "2. Support all formats (Documents, ZIP, RAR, Video, Audio, Images, APK, etc.)\n"
            "3. Return the file with its **Title**, **Format Type**, **Size**, **Direct Link**, and **Telegram File Link**"
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
    await query.answer("Transfer in progress...", show_alert=False)


@app.on_message(filters.text & filters. regex(r"https?://[^\s]+"))
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
                    raise RuntimeError(
                        f"HTTP {response.status} - {response.reason}"
                    )

                filename = extract_filename(str(response.url), response.headers)
                content_type = response.headers.get("Content-Type", "")
                format_label, ext = detect_format_info(filename, content_type)
                total_size = int(response.headers.get("Content-Length", 0))

                # Telegram bot MTProto limit check (2 GB)
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
            f"🔗 **Direct Link:** [Click Here]({url})"
        )

        # Smart format routing with automatic fallback to Document for universal format support
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
            # If media-specific upload fails (e.g., unindexed video/photo header), fallback to raw document
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

    # ---------------- STAGE 3: ATTACH TELEGRAM FILE LINK & FINAL DETAILS ----------------
    try:
        bot_me = await client.get_me()
        payload = encode_file_payload(sent_msg.chat.id, sent_msg.id)
        tg_file_link = f"https://t.me/{bot_me.username}?start=f_{payload}"

        final_caption = (
            f"📄 **Title:** `{filename}`\n"
            f"🗂 **Format Type:** `{format_label}`\n"
            f"📦 **File Size:** `{readable_size}`\n"
            f"🌐 **Direct Download Link:** [Download Original]({url})\n"
            f"✈️ **Telegram File Link:** [Get File in Telegram]({tg_file_link})"
        )

        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🌐 Direct Download Link", url=url),
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


# ==================== RENDER HEALTH-CHECK WEB SERVER ====================
async def health_check(request):
    return web.Response(text="Telegram URL Uploader Bot is running!", status=200)


async def main():
    # Start lightweight web server required by Render Web Services
    server = web.Application()
    server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health-check server listening on port {PORT}")

    # Start Pyrogram Bot
    await app.start()
    me = await app.get_me()
    print(f"Bot @{me.username} started successfully!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

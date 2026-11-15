import os
import sqlite3
import string
import random
import threading
import requests
from flask import Flask, Response, request, stream_with_context
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# Configuration from Environment Variables
TOKEN = os.getenv("BOT_TOKEN")
WEB_DOMAIN = os.getenv("WEB_DOMAIN", "http://localhost:5000")
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot_username")

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 5000))

# --- SQLite Database Setup for File Mapping ---
DB_NAME = "files.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            short_id TEXT PRIMARY KEY,
            file_id TEXT,
            file_name TEXT,
            file_type TEXT,
            file_size TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def generate_short_id(length=8):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def save_file_to_db(file_id, file_name, file_type, file_size):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    while True:
        short_id = generate_short_id()
        cursor.execute("SELECT 1 FROM files WHERE short_id = ?", (short_id,))
        if not cursor.fetchone():
            break
    cursor.execute(
        "INSERT INTO files (short_id, file_id, file_name, file_type, file_size) VALUES (?, ?, ?, ?, ?)",
        (short_id, file_id, file_name, file_type, file_size)
    )
    conn.commit()
    conn.close()
    return short_id

def get_file_from_db(short_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT file_id, file_name, file_type, file_size FROM files WHERE short_id = ?", (short_id,))
    row = cursor.fetchone()
    conn.close()
    return row

# --- File Metadata & Format Detection Helper ---
def get_file_metadata(filename):
    ext = os.path.splitext(filename)[1].lower()
    
    video_exts = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v']
    audio_exts = ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma', '.opus']
    image_exts = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff']
    archive_exts = ['.zip', '.rar', '.7z', '.tar', '.gz', '.bz2']
    doc_exts = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.odt', '.rtf']
    
    if ext in video_exts:
        file_type = "Video File"
        format_desc = f"Video Format ({ext.upper()[1:]})"
    elif ext in audio_exts:
        file_type = "Music / Audio File"
        format_desc = f"Audio Format ({ext.upper()[1:]})"
    elif ext in image_exts:
        file_type = "Image File"
        format_desc = f"Image Format ({ext.upper()[1:]})"
    elif ext in archive_exts:
        file_type = "Compressed Archive"
        format_desc = f"Archive Format ({ext.upper()[1:]})"
    elif ext in doc_exts:
        file_type = "Document"
        format_desc = f"Document Format ({ext.upper()[1:]})"
    else:
        file_type = "General File"
        format_desc = f"Custom Format ({ext.upper()[1:] if ext else 'Unknown'})"
        
    return file_type, format_desc


# --- Flask Web Server (Direct Download Links) ---
@app.route("/download/<short_id>")
def download_file(short_id):
    try:
        file_data = get_file_from_db(short_id)
        if not file_data:
            return "❌ Error: File not found or link has expired.", 404
        
        file_id, file_name, _, _ = file_data
        
        # Fetch file path securely from Telegram API
        file_info_res = requests.get(
            f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file_id}", timeout=15
        ).json()
        
        if not file_info_res.get("ok"):
            return "❌ Error: Unable to retrieve file from Telegram servers.", 404

        telegram_file_path = file_info_res["result"]["file_path"]
        telegram_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{telegram_file_path}"

        req = requests.get(telegram_file_url, stream=True, timeout=30)
        if req.status_code != 200:
            return "❌ Error: Failed to stream file from Telegram storage.", 500

        return Response(
            stream_with_context(req.iter_content(chunk_size=8192)),
            content_type=req.headers.get("content-type", "application/octet-stream"),
            headers={"Content-Disposition": f"attachment; filename={file_name}"},
        )
    except Exception as e:
        return f"❌ Server Error: {str(e)}", 500


# --- Telegram Bot Handlers ---

# 1. /start command & deep-linking file retrieval
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if args:
            short_id = args[0]
            file_data = get_file_from_db(short_id)
            if file_data:
                file_id, file_name, file_type, file_size = file_data
                _, format_desc = get_file_metadata(file_name)
                
                bot_username = context.bot.username or BOT_USERNAME
                direct_link = f"{WEB_DOMAIN}/download/{short_id}"
                tg_link = f"https://t.me/{bot_username}?start={short_id}"
                
                caption = (
                    f"📁 File Name: {file_name}\n"
                    f"📂 Type: {file_type}\n"
                    f"📌 Format: {format_desc}\n"
                    f"📦 Size: {file_size}\n\n"
                    f"🔗 Direct Download Link (Browser):\n{direct_link}\n\n"
                    f"🤖 Telegram Bot Share Link:\n{tg_link}"
                )
                
                await update.message.reply_document(document=file_id, caption=caption)
                return
            else:
                await update.message.reply_text("❌ Error: The requested file link is invalid or expired.")
                return

        # Default /start welcome message displaying all services
        welcome_text = (
            "👋 Welcome to the Universal File & Link Bot!\n\n"
            "Here are the services I provide completely free:\n\n"
            "1️⃣ Local File -> Links: Upload any file directly (PDFs, docs, ZIP/RAR archives, music, videos, documents), and I will generate a Direct Browser Download Link and a Telegram Bot Share Link instantly!\n"
            "2️⃣ Link -> File -> Links: Send any direct downloadable URL, and I will download it, host it on Telegram, and provide both download links!\n"
            "3️⃣ Instant Retrieval via Bot Link: Click any shared Telegram bot link (t.me/...) to instantly receive the file along with its download links.\n\n"
            "🚀 Send a file or a direct download link to get started!"
        )
        await update.message.reply_text(welcome_text)
    except Exception as e:
        await update.message.reply_text(f"❌ An error occurred: {str(e)}")


# 2. Handle direct file uploads locally from device
async def handle_local_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        

import os
import threading
import requests
from flask import Flask, Response, request, stream_with_context
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# Configuration from Environment Variables
TOKEN = os.getenv("BOT_TOKEN")
# Clean up WEB_DOMAIN to prevent trailing slash issues
WEB_DOMAIN = os.getenv("WEB_DOMAIN", "").strip().rstrip("/")
if not WEB_DOMAIN:
  WEB_DOMAIN = "http://localhost:5000"

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 5000))


# --- Flask Web Server ---
@app.route("/")
def home():
  return "Telegram File Download Bot is active and running!", 200


@app.route("/download/<file_id>")
def download_file(file_id):
  try:
    file_info_res = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getFile?file_id={file_id}"
    ).json()
    if not file_info_res.get("ok"):
      return "File not found or expired", 404

    file_path = file_info_res["result"]["file_path"]
    telegram_file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    file_name = file_path.split("/")[-1]

    req = requests.get(telegram_file_url, stream=True)
    return Response(
        stream_with_context(req.iter_content(chunk_size=1024)),
        content_type=req.headers.get("content-type", "application/octet-stream"),
        headers={"Content-Disposition": f"attachment; filename={file_name}"},
    )
  except Exception as e:
    return str(e), 500


# --- Telegram Bot Handlers ---
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
  document = (
      update.message.document
      or update.message.video
      or update.message.audio
      or update.message.photo[-1]
  )
  file_id = document.file_id

  download_link = f"{WEB_DOMAIN}/download/{file_id}"
  await update.message.reply_text(
      f"File received successfully!\n\nDirect Download Link:\n{download_link}"
  )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
  text = update.message.text
  if text.startswith("http://") or text.startswith("https://"):
    msg = await update.message.reply_text(
        "Downloading file from your link..."
    )
    try:
      response = requests.get(text, stream=True)
      filename = text.split("/")[-1].split("?")[0] or "downloaded_file"
      local_path = os.path.join("/tmp", filename)

      with open(local_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
          f.write(chunk)

      await msg.edit_text("Uploading file to Telegram...")

      with open(local_path, "rb") as f:
        sent_message = await context.bot.send_document(
            chat_id=update.effective_chat.id, document=f
        )

      file_id = sent_message.document.file_id
      download_link = f"{WEB_DOMAIN}/download/{file_id}"

      await msg.edit_text(
          f"File uploaded & re-hosted successfully!\n\nDirect Download Link:\n"
          f"{download_link}"
      )
      os.remove(local_path)
    except Exception as e:
      await msg.edit_text(f"Failed to process link: {str(e)}")


def run_flask():
  app.run(host="0.0.0.0", port=PORT)


def main():
  flask_thread = threading.Thread(target=run_flask)
  flask_thread.daemon = True
  flask_thread.start()

  application = ApplicationBuilder().token(TOKEN).build()
  application.add_handler(
      MessageHandler(
          filters.Document.ALL
          | filters.VIDEO
          | filters.AUDIO
          | filters.PHOTO,
          handle_file,
      )
  )
  application.add_handler(
      MessageHandler(filters.TEXT & (~filters.COMMAND), handle_url)
  )

  application.run_polling()


if __name__ == "__main__":
  main()

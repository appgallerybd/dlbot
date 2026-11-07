import asyncio
import logging
import os
import re
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dlbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_MB = int(os.getenv("MAX_FILE_MB", "50"))  # সাধারণ Bot API সীমা 50MB
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")  # ঐচ্ছিক
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "2"))
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

URL_RE = re.compile(r"https?://\S+")
SUPPORTED = (
    "youtube.com", "youtu.be", "music.youtube.com",
    "facebook.com", "fb.watch", "fb.com",
    "instagram.com",
)
semaphore = asyncio.Semaphore(MAX_PARALLEL)


def is_supported(url: str) -> bool:
    return any(d in url.lower() for d in SUPPORTED)


def download_media(url: str, mode: str, quality: int, out_dir: Path):
    opts = {
        "outtmpl": str(out_dir / "%(title).70s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": MAX_MB * 1024 * 1024,
    }
    if Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE

    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        h = quality
        opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
        )
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [f for f in out_dir.iterdir() if f.is_file()]
    if not files:
        raise FileNotFoundError("too_large_or_failed")
    return files[0], info


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "আসসালামু আলাইকুম! 👋\n\n"
        "ইউটিউব, ফেসবুক বা ইনস্টাগ্রামের ভিডিও লিংক পাঠান। "
        "আমি ভিডিও বা অডিও (MP3) ডাউনলোড করে দেব।"
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = URL_RE.search(update.message.text or "")
    if not match:
        return
    url = match.group(0)
    if not is_supported(url):
        await update.message.reply_text("❌ শুধু YouTube, Facebook ও Instagram লিংক সাপোর্টেড।")
        return

    key = uuid.uuid4().hex[:8]
    context.user_data[key] = url
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 360p", callback_data=f"v:360:{key}"),
            InlineKeyboardButton("🎬 720p", callback_data=f"v:720:{key}"),
            InlineKeyboardButton("🎬 1080p", callback_data=f"v:1080:{key}"),
        ],
        [InlineKeyboardButton("🎵 অডিও (MP3)", callback_data=f"a:0:{key}")],
    ])
    await update.message.reply_text("কী ডাউনলোড করতে চান?", reply_markup=kb)


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        kind, quality, key = q.data.split(":")
    except ValueError:
        return
    url = context.user_data.get(key)
    if not url:
        await q.edit_message_text("⚠️ লিংকটি মেয়াদোত্তীর্ণ, আবার পাঠান।")
        return

    mode = "audio" if kind == "a" else "video"
    await q.edit_message_text("⏳ ডাউনলোড হচ্ছে, একটু অপেক্ষা করুন...")

    out_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    out_dir.mkdir(parents=True)
    chat_id = q.message.chat_id
    try:
        async with semaphore:
            loop = asyncio.get_running_loop()
            path, info = await loop.run_in_executor(
                None, download_media, url, mode, int(quality), out_dir
            )

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_MB:
            await q.edit_message_text(
                f"❌ ফাইলটি {size_mb:.0f}MB, যা সীমা ({MAX_MB}MB) ছাড়িয়েছে। কম কোয়ালিটি বেছে নিন।"
            )
            return

        await q.edit_message_text("📤 আপলোড হচ্ছে...")
        title = (info.get("title") or "")[:200]
        with open(path, "rb") as f:
            if mode == "audio":
                await context.bot.send_audio(
                    chat_id, f, title=title, caption=title,
                    read_timeout=300, write_timeout=300,
                )
            else:
                await context.bot.send_video(
                    chat_id, f, caption=title, supports_streaming=True,
                    read_timeout=300, write_timeout=300,
                )
        await q.delete_message()
    except FileNotFoundError:
        await q.edit_message_text(
            f"❌ ফাইল {MAX_MB}MB এর বেশি হতে পারে বা ডাউনলোড ব্যর্থ। কম কোয়ালিটি চেষ্টা করুন।"
        )
    except yt_dlp.utils.DownloadError as e:
        log.warning("download error: %s", e)
        raw = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        low = raw.lower()
        if "sign in to confirm" in low or "not a bot" in low:
            hint = "ইউটিউব এই সার্ভারের আইপি ব্লক করেছে। cookies.txt লাগবে।"
        elif "login" in low or "cookies" in low or "rate-limit" in low:
            hint = "এই সাইট লগইন চাইছে। cookies.txt লাগবে।"
        elif "ffmpeg" in low:
            hint = "ffmpeg পাওয়া যাচ্ছে না।"
        elif "private" in low:
            hint = "ভিডিওটি প্রাইভেট।"
        elif "unsupported url" in low:
            hint = "এই লিংক সাপোর্টেড নয়।"
        else:
            hint = "কারণ অজানা।"
        await q.edit_message_text(f"❌ ডাউনলোড ব্যর্থ। {hint}\n\nত্রুটি:\n{raw[-300:]}")
    except Exception:
        log.exception("unexpected error")
        await q.edit_message_text("❌ কিছু একটা সমস্যা হয়েছে, পরে আবার চেষ্টা করুন।")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        context.user_data.pop(key, None)


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def start_health_server():
    # Render/Koyeb এর মতো হোস্টের জন্য একটি পোর্ট খোলা রাখা
    port = os.getenv("PORT")
    if port:
        srv = HTTPServer(("0.0.0.0", int(port)), _Health)
        threading.Thread(target=srv.serve_forever, daemon=True).start()


def main():
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(on_choice))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()

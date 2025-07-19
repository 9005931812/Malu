import asyncio
import subprocess
import os
import logging
import time
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified, FloodWait

# Load environment variables from .env file
load_dotenv()

# Environment variables
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_API_TOKEN")  # Fixed BOT_TOKEN issue
VIDEO_DIR = os.getenv("VIDEO_DIR", "/workspaces/rgbb/videos")
CHAPTERS_FILE = os.getenv("CHAPTERS_FILE", "chapters.txt")

OWNER_ID = int(os.getenv("OWNER_ID"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Pyrogram Client
app = Client("anime_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

last_update_time = time.time()

@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text("Welcome! Use /download [anime_id] [other_options] to start downloading.")

def get_latest_file(directory):
    """Retrieve the latest .mkv file from the directory."""
    try:
        files = [f for f in os.listdir(directory) if f.endswith(".mkv")]
        if not files:
            return None
        files.sort(key=lambda x: os.path.getmtime(os.path.join(directory, x)), reverse=True)
        return os.path.join(directory, files[0])
    except Exception as e:
        logger.error(f"Error getting latest file: {e}")
        return None

def mux_with_chapters(video_file, chapters_file):
    """Mux the video file with chapters using ffmpeg."""
    output_file = video_file.replace(".mkv", "_with_chapters.mkv")
    command = ["ffmpeg", "-i", video_file, "-i", chapters_file, "-map_metadata", "1", "-c:v", "copy", "-c:a", "copy", output_file]
    
    result = subprocess.run(command, capture_output=True, text=True)
    return result, output_file

def is_owner_or_admin(user_id):
    """Check if the user is the owner or an admin."""
    return user_id == OWNER_ID or user_id in ADMIN_IDS

async def progress(current, total, message: Message, filename, start_time):
    """Progress callback for file upload."""
    global last_update_time
    current_time = time.time()
    
    if current_time - last_update_time >= 2:  # Update every 2 seconds
        try:
            elapsed_time = current_time - start_time
            speed = current / elapsed_time if elapsed_time > 0 else 0
            eta = (total - current) / speed if speed > 0 else 0
            progress_percentage = (current / total) * 100

            await message.edit_text(
                f"File: `{filename}`\n"
                f"Progress: {progress_percentage:.2f}%\n"
                f"{current / (1024 ** 2):.2f} MB of {total / (1024 ** 2):.2f} MB\n"
                f"Speed: {speed / (1024 ** 2):.2f} MB/s\n"
                f"ETA: {int(eta)}s\n"
                f"Elapsed: {int(elapsed_time)}s"
            )
            last_update_time = current_time
        except MessageNotModified:
            pass

async def safe_edit_message(message, text):
    """Safely edit a message with retry logic to handle FloodWait errors."""
    try:
        await message.edit_text(text)
    except FloodWait as e:
        logger.warning(f"Flood wait: Sleeping for {e.x} seconds.")
        await asyncio.sleep(e.x)
        await message.edit_text(text)

@app.on_message(filters.command("download"))
async def download_anime(client, message):
    """Download anime and upload the latest video."""
    if not is_owner_or_admin(message.from_user.id):
        await message.reply_text("❌ You do not have permission to use this command.")
        return

    if len(message.command) < 2:
        await message.reply_text("Usage: /download [anime_id] [other_options].")
        return

    anime_id = message.command[1]
    other_options = ' '.join(message.command[2:])
    
    if "hidive" in other_options.lower():
        command = ["./aniDL", "--service", "hidive", "-s", anime_id] + other_options.split()
    else:
        command = ["./aniDL", "--service", "crunchy", "--srz", anime_id] + other_options.split()

    status_message = await message.reply_text("⚙️ Starting download...")

    try:
        if not os.path.isfile("./aniDL"):
            await status_message.edit_text("❌ Error: aniDL tool not found.")
            return

        process = await asyncio.create_subprocess_exec(
            *command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line = line.decode().strip()
            if "Progress:" in line:
                await safe_edit_message(status_message, f"⚙️ {line}")

        await process.wait()
        if process.returncode == 0:
            latest_file = get_latest_file(VIDEO_DIR)
            if latest_file:
                if os.path.isfile(CHAPTERS_FILE):
                    mux_result, output_file = mux_with_chapters(latest_file, CHAPTERS_FILE)
                    if mux_result.returncode == 0:
                        latest_file = output_file
                    else:
                        logger.error(f"Muxing failed: {mux_result.stderr}")
                        await safe_edit_message(status_message, f"⚠️ Error during muxing:\n{mux_result.stderr}")

                await asyncio.sleep(5)

                try:
                    await safe_edit_message(status_message, "📤 Uploading to Telegram...")
                    file_size = os.path.getsize(latest_file)
                    if file_size > 2 * 1024 ** 3:
                        await safe_edit_message(status_message, "❌ File size exceeds Telegram's 2GB limit.")
                        return

                    start_time = time.time()
                    await client.send_document(
                        chat_id=message.chat.id,
                        document=latest_file,
                        caption=f"🎥 **Uploaded:** `{os.path.basename(latest_file)}`\n✅ **Download complete!**",
                        progress=progress,
                        progress_args=(status_message, os.path.basename(latest_file), start_time),
                    )
                    await safe_edit_message(status_message, "✅ **Upload complete!**")

                    os.remove(latest_file)
                    logger.info(f"File deleted: {latest_file}")
                except Exception as e:
                    logger.error(f"Error during upload: {e}")
                    await safe_edit_message(status_message, f"❌ Error during upload: {e}")
            else:
                await safe_edit_message(status_message, "❌ No .mkv files found in the videos directory.")
        else:
            stderr = (await process.stderr.read()).decode()
            logger.error(f"Download command failed: {stderr}")
            await safe_edit_message(status_message, f"❌ Error occurred during download:\n{stderr}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await safe_edit_message(status_message, f"❌ An unexpected error occurred: {e}")

if __name__ == "__main__":
    logger.info("Bot is running...")
    app.run()

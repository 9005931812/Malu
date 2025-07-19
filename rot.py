import os
import subprocess
import time
import logging
import asyncio
import re
import anitopy
import requests
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified
from dotenv import load_dotenv
from typing import Optional, Tuple

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load environment variables from .env file
load_dotenv()

# Get environment variables
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
VIDEO_DIR = os.getenv("VIDEO_DIR", "./videos")
OWNER_ID = int(os.getenv("OWNER_ID"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

# Create the Client
app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global variable to handle progress updates
last_update_time = time.time()

# --- Helper Functions ---
def shorten_anime_name(name: str, max_length: int = 25) -> str:
    """Shortens the anime name if it exceeds the specified maximum length."""
    if len(name) <= max_length:
        return name

    parts = re.split(r'[:|]', name)
    shortened_name = parts[0].strip()

    if len(shortened_name) > max_length:
        shortened_name = shortened_name[:max_length - 3] + "..."
    return shortened_name

def auto_rename_with_anitopy(file_path: str, service: str = "crunchy") -> Tuple[str, Optional[str], Optional[str]]:
    """Renames the file using anitopy and adds service prefix."""
    try:
        filename = os.path.basename(file_path)
        anime_title, season, episode = extract_anime_info(filename)
        if not anime_title:
            return file_path, None, None

        prefix = "[HD]" if service.lower() == "hidive" else "[CR]"
        resolution_match = re.search(r"\[(\d+p)\]", filename)
        resolution = resolution_match.group(1) if resolution_match else "1080p"
        shortened_title = shorten_anime_name(anime_title, max_length=25)

        output_name = f"{prefix} {shortened_title} - S{season}E{episode} [{resolution}].mkv"
        new_file_path = os.path.join(os.path.dirname(file_path), output_name)
        os.rename(file_path, new_file_path)

        cover_url = fetch_anilist_cover(anime_title)
        if not cover_url:
            fallback_title = re.sub(r'[-:]', ' ', anime_title).strip()
            cover_url = fetch_anilist_cover(fallback_title)

        thumbnail_path = None
        if cover_url:
            thumbnail_path = f"{os.path.splitext(new_file_path)[0]}_cover.jpg"
            if not download_cover_image(cover_url, thumbnail_path):
                thumbnail_path = None

        return new_file_path, thumbnail_path, shortened_title
    except Exception as e:
        logging.error(f"Renaming error: {e}")
        return file_path, None, None

def extract_anime_info(filename: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extracts anime title, season, and episode number."""
    try:
        filename = filename.replace("_", " ")
        video = anitopy.parse(filename)
        if not video:
            return None, None, None

        anime_title = video.get("anime_title", "Unknown")
        if not anime_title:
            return None, None, None

        anime_title = re.sub(r'\[.*?\]', '', anime_title).strip()
        anime_title = re.sub(r'\(.*?\)', '', anime_title).strip()
        anime_title = re.sub(r'[^a-zA-Z0-9\s]', ' ', anime_title).strip()

        season = video.get("anime_season", "1").zfill(2)
        episode = video.get("episode_number", "01").zfill(2)

        return anime_title, season, episode
    except Exception as e:
        logging.error(f"Error extracting anime info: {e}")
        return None, None, None

def fetch_anilist_cover(anime_title: str, retries: int = 3) -> Optional[str]:
    """Fetches the cover image URL from AniList."""
    query = '''
    query ($search: String) {
        Media (search: $search, type: ANIME) {
            title { romaji english }
            coverImage { large }
        }
    }
    '''
    variables = {'search': anime_title}
    for _ in range(retries):
        try:
            response = requests.post('https://graphql.anilist.co', 
                                  json={'query': query, 'variables': variables})
            if response.status_code == 200:
                data = response.json()
                return data.get('data', {}).get('Media', {}).get('coverImage', {}).get('large')
        except requests.exceptions.RequestException as e:
            logging.error(f"AniList API Error (retrying): {e}")
            time.sleep(2)
    return None

def download_cover_image(url: str, save_path: str) -> bool:
    """Downloads the cover image from the given URL."""
    try:
        response = requests.get(url)
        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                f.write(response.content)
            return True
    except Exception as e:
        logging.error(f"Error downloading cover image: {e}")
    return False

async def progress(current: int, total: int, message, filename: str, start_time: float):
    """Progress callback for file upload."""
    global last_update_time
    current_time = time.time()
    if current_time - last_update_time >= 2:
        try:
            elapsed_time = current_time - start_time
            speed = current / elapsed_time if elapsed_time > 0 else 0
            eta = (total - current) / speed if speed > 0 else 0
            progress_percentage = (current / total) * 100
            bar_length = 20
            progress_blocks = int(bar_length * current / total)
            empty_blocks = bar_length - progress_blocks
            progress_bar = f"█" * progress_blocks + f"░" * empty_blocks

            await message.edit_text(
                f"File: `{filename}`\n"
                f"Progress: {progress_percentage:.2f}%\n"
                f"{current / (1024 ** 2):.2f} MB of {total / (1024 ** 2):.2f} MB\n"
                f"Speed: {speed / (1024 ** 2):.2f} MB/s\n"
                f"ETA: {int(eta)}s\n"
                f"Elapsed: {int(elapsed_time)}s\n"
                f"[{progress_bar}] {progress_percentage:.2f}%"
            )
            last_update_time = current_time
        except MessageNotModified:
            pass

async def safe_edit_message(message, text: str):
    """Safely edit a message with retry logic."""
    try:
        if message.text == text:
            return
        await message.edit_text(text)
    except FloodWait as e:
        logging.warning(f"Flood wait: Sleeping for {e.x} seconds.")
        await asyncio.sleep(e.x)
        await message.edit_text(text)

@app.on_message(filters.command("download"))
async def download_anime(client, message):
    """Download anime and upload the latest video to Telegram."""
    if message.from_user.id not in [OWNER_ID] + ADMIN_IDS:
        await message.reply_text("❌ You do not have permission to use this command.")
        return

    if len(message.command) < 2:
        await message.reply_text("Usage: /download [anime_id] [other_options].")
        return

    anime_id = message.command[1]
    other_options = ' '.join(message.command[2:])

    if "hidive" in other_options.lower():
        service = "hidive"
        command = ["./aniDL", "--service", "hidive", "-s", anime_id] + other_options.split()
    else:
        service = "crunchy"
        command = ["./aniDL", "--service", "crunchy", "--srz", anime_id] + other_options.split()

    status_message = await message.reply_text("⚙️ Starting download...")

    try:
        if not os.path.isfile("./aniDL"):
            await status_message.edit_text("❌ Error: aniDL tool not found.")
            return

        start_time = time.time()
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
                latest_file, thumbnail_path, _ = auto_rename_with_anitopy(latest_file, service)
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
                        thumb=thumbnail_path if thumbnail_path and os.path.exists(thumbnail_path) else None,
                        progress=progress,
                        progress_args=(status_message, os.path.basename(latest_file), start_time),
                    )
                    await safe_edit_message(status_message, "✅ **Upload complete!**")

                    # Clean up files after upload
                    if latest_file and os.path.exists(latest_file):
                        os.remove(latest_file)
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        os.remove(thumbnail_path)
                except Exception as e:
                    logging.error(f"Error during upload: {e}")
                    await safe_edit_message(status_message, f"❌ Error during upload: {e}")
            else:
                await safe_edit_message(status_message, "❌ No .mkv files found in the videos directory.")
        else:
            stderr = (await process.stderr.read()).decode()
            logging.error(f"Download command failed: {stderr}")
            await safe_edit_message(status_message, f"❌ Error occurred during download:\n{stderr}")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        await safe_edit_message(status_message, f"❌ An unexpected error occurred: {e}")

def get_latest_file(directory: str) -> Optional[str]:
    """Get the most recent file from the specified directory."""
    try:
        files = [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".mkv")]
        return max(files, key=os.path.getctime) if files else None
    except Exception as e:
        logging.error(f"Error getting latest file: {e}")
        return None

if __name__ == "__main__":
    logging.info("Bot is running...")
    app.run()
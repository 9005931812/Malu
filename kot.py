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
from concurrent.futures import ThreadPoolExecutor

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load environment variables from .env file
load_dotenv()

# Get environment variables
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
VIDEO_DIR = os.getenv("VIDEO_DIR", "./videos")
CHAPTERS_FILE = os.getenv("CHAPTERS_FILE", "./chapters.txt")
OWNER_ID = int(os.getenv("OWNER_ID"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))
RCLONE_CONFIG_PATH = os.getenv("RCLONE_CONFIG_PATH")
SOURCE_DIR = os.getenv("SOURCE_DIR")
REMOTE_NAME = os.getenv("REMOTE_NAME")

# Create the Client
app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global variable to handle progress updates
last_update_time = time.time()

# --- Helper Functions ---
def shorten_anime_name(name, max_length=25):
    """
    Shortens the anime name if it exceeds the specified maximum length.
    Tries to preserve meaningful parts of the name (e.g., main title).
    """
    if len(name) <= max_length:
        return name

    # Split the name into parts (e.g., "Anime Title: Subtitle" -> ["Anime Title", "Subtitle"])
    parts = re.split(r'[:|]', name)

    # Keep the first part (main title) and truncate if necessary
    shortened_name = parts[0].strip()

    if len(shortened_name) > max_length:
        # Truncate the main title and add an ellipsis
        shortened_name = shortened_name[:max_length - 3] + "..."

    return shortened_name

def auto_rename_with_anitopy(file_path, service="crunchy"):
    """Renames the file using anitopy, adds service prefix, shortens the title, and fetches AniList cover image."""
    try:
        filename = os.path.basename(file_path)
        anime_title, season, episode = extract_anime_info(filename)
        if not anime_title:
            return file_path, None, None

        # Determine service prefix
        if service.lower() == "hidive":
            prefix = "[HD]"
        else:
            prefix = "[CR]"

        # Extract resolution from the original filename
        resolution_match = re.search(r"\[(\d+p)\]", filename)
        resolution = resolution_match.group(1) if resolution_match else "1080p"  # Default to 1080p if not found

        # Shorten the anime name to a maximum of 25 characters
        shortened_title = shorten_anime_name(anime_title, max_length=25)

        # Create new filename
        output_name = f"{prefix} {shortened_title} - S{season}E{episode} [{resolution}].mkv"
        new_file_path = os.path.join(os.path.dirname(file_path), output_name)
        os.rename(file_path, new_file_path)

        # Fetch AniList cover image
        cover_url = fetch_anilist_cover(anime_title)
        if not cover_url:
            # Fallback: Try searching without subtitles or special characters
            fallback_title = re.sub(r'[-:]', ' ', anime_title).strip()
            cover_url = fetch_anilist_cover(fallback_title)

        if not cover_url:
            return new_file_path, None, shortened_title

        # Download and save the cover image
        thumbnail_path = f"{os.path.splitext(new_file_path)[0]}_cover.jpg"
        if download_cover_image(cover_url, thumbnail_path):
            return new_file_path, thumbnail_path, shortened_title
        else:
            return new_file_path, None, shortened_title

    except Exception as e:
        logging.error(f"Renaming error: {e}")
        return file_path, None, None

def extract_anime_info(filename):
    """Extracts anime title, season, and episode number using anitopy and custom logic."""
    try:
        # Replace underscores with spaces for better parsing
        filename = filename.replace("_", " ")

        # Parse using anitopy
        video = anitopy.parse(filename)
        if not video:
            return None, None, None

        # Extract anime title
        anime_title = video.get("anime_title", "Unknown")
        if not anime_title:
            return None, None, None

        # Clean up title (remove brackets, special characters, etc.)
        anime_title = re.sub(r'\[.*?\]', '', anime_title).strip()
        anime_title = re.sub(r'\(.*?\)', '', anime_title).strip()
        anime_title = re.sub(r'[^a-zA-Z0-9\s]', ' ', anime_title).strip()

        # Extract season and episode
        season = video.get("anime_season", "1").zfill(2)
        episode = video.get("episode_number", "01").zfill(2)

        return anime_title, season, episode
    except Exception as e:
        logging.error(f"Error extracting anime info: {e}")
        return None, None, None

def fetch_anilist_cover(anime_title, retries=3):
    """Fetches the cover image URL from AniList based on the anime title."""
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
            response = requests.post('https://graphql.anilist.co', json={'query': query, 'variables': variables})
            if response.status_code == 200:
                data = response.json()
                return data.get('data', {}).get('Media', {}).get('coverImage', {}).get('large')
        except requests.exceptions.RequestException as e:
            logging.error(f"AniList API Error (retrying): {e}")
            time.sleep(2)
    return None

def download_cover_image(url, save_path):
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

def upload_to_rclone(file_path, remote_name, rclone_config_path):
    """Function to upload a single file using rclone."""
    try:
        logging.info(f"Uploading to rclone: {file_path}")
        command = [
            "rclone", "copy", file_path, f"{remote_name}:",
            "--config", rclone_config_path, "--progress"
        ]
        subprocess.run(command, check=True)
        logging.info(f"Upload completed: {file_path}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Error uploading {file_path}: {e}")
        return False

def generate_onedrive_share_link(file_name, rclone_config_path):
    """Generate a public OneDrive shareable link with the downloadable parameter."""
    try:
        # Generate the rclone link using the file name and remote
        command = [
            "rclone", "link", f"onedi:{file_name}",
            "--config", rclone_config_path
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        share_link = result.stdout.strip()

        # Append the ?download=1 to make it a direct download link
        download_link = f"{share_link}?download=1"
        return download_link
    except subprocess.CalledProcessError as e:
        logging.error(f"Error generating share link for {file_name}: {e}")
        return None

async def progress(current, total, message, filename, start_time):
    """Progress callback for file upload."""
    global last_update_time
    current_time = time.time()
    if current_time - last_update_time >= 2:  # Update every 2 seconds
        try:
            elapsed_time = current_time - start_time
            speed = current / elapsed_time if elapsed_time > 0 else 0
            eta = (total - current) / speed if speed > 0 else 0
            progress_percentage = (current / total) * 100
            bar_length = 20  # Length of the progress bar
            progress_blocks = int(bar_length * current / total)  # Number of filled blocks
            empty_blocks = bar_length - progress_blocks  # Empty blocks
            progress_bar = f"█" * progress_blocks + f"░" * empty_blocks

            # Message content with progress bar
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

async def safe_edit_message(message, text):
    """Safely edit a message with retry logic to handle FloodWait errors."""
    try:
        # Avoid unnecessary edits
        if message.text == text:
            return
        await message.edit_text(text)
    except FloodWait as e:
        logging.warning(f"Flood wait: Sleeping for {e.x} seconds.")
        await asyncio.sleep(e.x)
        await message.edit_text(text)

@app.on_message(filters.command("download"))
async def download_anime(client, message):
    """Download anime and upload the latest video to both Telegram and rclone."""
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
                # Rename file and fetch cover image
                latest_file, thumbnail_path, _ = auto_rename_with_anitopy(latest_file, service)

                if os.path.isfile(CHAPTERS_FILE):
                    mux_result, output_file = mux_with_chapters(latest_file, CHAPTERS_FILE)
                    if mux_result.returncode == 0:
                        latest_file = output_file
                    else:
                        logging.error(f"Muxing failed: {mux_result.stderr}")
                        await status_message.edit_text(f"⚠️ Error during muxing:\n{mux_result.stderr}")

                await asyncio.sleep(5)

                try:
                    await safe_edit_message(status_message, "📤 Uploading to Telegram...")

                    # Telegram upload
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
                    await safe_edit_message(status_message, "✅ **Upload complete on Telegram!**")

                    # Upload to rclone
                    if upload_to_rclone(latest_file, REMOTE_NAME, RCLONE_CONFIG_PATH):
                        # Use the file name for generating the share link
                        file_name = os.path.basename(latest_file)
                        share_link = generate_onedrive_share_link(file_name, RCLONE_CONFIG_PATH)
                        if share_link:
                            await status_message.edit_text(f"✅ **Uploaded to rclone!**\n{share_link}")
                        else:
                            await status_message.edit_text(f"❌ Failed to generate rclone share link for {latest_file}")

                    # Auto-delete the file after upload
                    os.remove(latest_file)
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        os.remove(thumbnail_path)
                    logging.info(f"File deleted: {latest_file}")
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

def get_latest_file(directory):
    """Get the most recent file from the specified directory."""
    try:
        files = [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".mkv")]
        return max(files, key=os.path.getctime) if files else None
    except Exception as e:
        logging.error(f"Error getting latest file: {e}")
        return None

def mux_with_chapters(input_file, chapters_file):
    """Mux the file with chapters."""
    output_file = f"{os.path.splitext(input_file)[0]}_muxed.mkv"
    command = ["mkvmerge", "-o", output_file, input_file, "--chapters", chapters_file]
    return subprocess.run(command, capture_output=True, text=True), output_file

# Start the bot
if __name__ == "__main__":
    logging.info("Bot is running...")
    app.run()
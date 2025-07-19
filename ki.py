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
from collections import deque
from threading import Lock
from pathlib import Path

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

# Create the Client
app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global variables for queue and task management
task_queue = deque()
queue_lock = Lock()
current_task = None
last_update_time = time.time()  # Initialize global variable

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

# --- Subtitle Extraction and Muxing ---
def extract_sign_subtitles(input_file, output_sub_file):
    """Extracts sign subtitles marked with specific keywords from the .ass subtitle file."""
    try:
        # Extract the first subtitle track
        extract_command = [
            "ffmpeg", "-y", "-i", input_file, "-map", "0:s:0", "-c:s", "ass", output_sub_file
        ]
        result = subprocess.run(extract_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        if result.returncode != 0:
            logging.error(f"FFmpeg extraction failed for {input_file}: {result.stderr.decode()}")
            return False

        # Read the extracted ASS file
        with open(output_sub_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Process ASS file content
        in_events = False
        header = []
        sign_lines = []

        # Keywords to filter (style names and effects)
        style_keywords = ["BW Phone Bubble", "Text Date"]  # Filter by style name
        effect_text_keywords = ["\\an", "\\fad", "\\pos", "\\fs"]  # Added \fs
        actor_keyword = "Sign"  # Added actor keyword

        for line in lines:
            line = line.strip()
            if line.startswith("[Events]"):
                in_events = True
                header.append(line)
                header.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
                continue
                
            if in_events and line.startswith("Dialogue:"):
                parts = line.split(",", 9)
                if len(parts) < 10:
                    continue
                    
                style = parts[3].strip()  # 4th field is the style name
                name = parts[4].strip()  # 5th field is the actor name
                effect = parts[8].strip()  # 9th field is the effect
                text = parts[9].strip()  # 10th field is the text
                
                # Check if the style name matches any of the style keywords
                if any(keyword in style for keyword in style_keywords):
                    sign_lines.append(line)
                    logging.debug(f"Filtered by style: {line}")  # Debugging
                
                # Check if any effect or text keywords are present
                elif any(keyword in effect or keyword in text for keyword in effect_text_keywords):
                    sign_lines.append(line)
                    logging.debug(f"Filtered by effect/text: {line}")  # Debugging
                
                # Check if the actor name matches the keyword
                elif actor_keyword in name:
                    sign_lines.append(line)
                    logging.debug(f"Filtered by actor: {line}")  # Debugging
                    
            elif line.startswith("["):
                in_events = False
                header.append(line)
            elif not in_events:
                header.append(line)

        # Rebuild the ASS file
        with open(output_sub_file, "w", encoding="utf-8") as f:
            f.write("\n".join(header))
            if sign_lines:
                f.write("\n" + "\n".join(sign_lines))

        logging.info(f"Sign subtitles extracted to: {output_sub_file}")
        return True

    except Exception as e:
        logging.error(f"Error extracting sign subtitles: {str(e)}")
        return False

def add_sign_subtitles(input_file, sign_sub_file):
    """Adds sign subtitles as the first subtitle track."""
    try:
        temp_output = f"{input_file}.temp.mkv"

        if not os.path.exists(sign_sub_file):
            logging.error(f"Sign subtitle file not found: {sign_sub_file}")
            return False

        command = [
            "mkvmerge", "-o", temp_output,
            input_file,  # Keep the original input file first
            "--language", "0:eng", "--track-name", "0:English Sign", "--default-track", "0:yes", sign_sub_file
        ]

        logging.info(f"Running mkvmerge command: {' '.join(command)}")
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        if result.returncode != 0:
            logging.error(f"mkvmerge failed:\n{result.stderr.decode()}")
            os.remove(temp_output)
            return False

        os.replace(temp_output, input_file)
        logging.info(f"Successfully added sign subtitles to: {input_file}")
        return True

    except Exception as e:
        logging.error(f"Error in add_sign_subtitles(): {str(e)}")
        if os.path.exists(temp_output):
            os.remove(temp_output)
        return False

# --- Queue Management ---
async def process_queue():
    """Processes tasks from the queue one at a time."""
    global current_task
    while True:
        with queue_lock:
            if not task_queue:
                current_task = None
                break
            current_task = task_queue.popleft()

        try:
            await current_task()
        except Exception as e:
            logging.error(f"Task error: {e}")
        finally:
            current_task = None

# --- Helper Functions ---
def get_latest_file(directory):
    """Get the most recent file from the specified directory."""
    try:
        files = [f for f in os.listdir(directory) if f.endswith(".mkv")]
        if not files:
            return None
        newest = max(files, key=lambda x: os.path.getctime(os.path.join(directory, x)))
        return os.path.join(directory, newest)
    except Exception as e:
        logging.error(f"Error getting latest file: {e}")
        return None

def mux_with_chapters(input_file, chapters_file):
    """Mux the file with chapters."""
    output_file = f"{os.path.splitext(input_file)[0]}_muxed.mkv"
    command = ["mkvmerge", "-o", output_file, input_file, "--chapters", chapters_file]
    return subprocess.run(command, capture_output=True, text=True), output_file

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

async def safe_edit_message(message, text):
    """Safely edit a message with retry logic to handle FloodWait errors."""
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

    async def task():
        status_message = await message.reply_text("⚙️ Starting download...")

        try:
            if not os.path.isfile("./aniDL"):
                await status_message.edit_text("❌ Error: aniDL tool not found.")
                return

            start_time = time.time()
            if "hidive" in other_options.lower():
                service = "hidive"
                command = ["./aniDL", "--service", "hidive", "-s", anime_id] + other_options.split()
            else:
                service = "crunchy"
                command = ["./aniDL", "--service", "crunchy", "--srz", anime_id] + other_options.split()

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

                    # Extract and mux sign subtitles
                    sign_sub_file = f"{os.path.splitext(latest_file)[0]}_sign.ass"
                    if extract_sign_subtitles(latest_file, sign_sub_file):
                        if os.path.getsize(sign_sub_file) > 0:
                            add_sign_subtitles(latest_file, sign_sub_file)
                            os.remove(sign_sub_file)
                            logging.info(f"Sign subtitles added to: {latest_file}")
                        else:
                            os.remove(sign_sub_file)
                            logging.warning(f"No sign subtitles found in: {latest_file}")

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
                            await status_message.edit_text("❌ File size exceeds Telegram's 2GB limit.")
                            return

                        start_time = time.time()
                        await client.send_document(
                            chat_id=message.chat.id,
                            document=latest_file,
                            caption=f"`{os.path.basename(latest_file)}`",
                            thumb=thumbnail_path if thumbnail_path and os.path.exists(thumbnail_path) else None,
                            progress=progress,
                            progress_args=(status_message, os.path.basename(latest_file), start_time),
                        )
                        await safe_edit_message(status_message, "✅ **Upload complete on Telegram!**")

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

    # Add task to queue
    with queue_lock:
        task_queue.append(task)
        if not current_task:
            asyncio.create_task(process_queue())

# Start the bot
if __name__ == "__main__":
    logging.info("Bot is running...")
    app.run()
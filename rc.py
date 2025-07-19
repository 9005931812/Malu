import os
import subprocess
import time
import logging
import asyncio  # Import asyncio here
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
    """Generate a public OneDrive shareable link via rclone."""
    try:
        # Generate the rclone link using the file name and remote
        command = [
            "rclone", "link", f"onedi:{file_name}",
            "--config", rclone_config_path
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return result.stdout.strip()
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
        command = ["./aniDL", "--service", "hidive", "-s", anime_id] + other_options.split()
    else:
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

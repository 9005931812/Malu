import os
import subprocess
import threading
import psutil
import requests
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
from time import time
import re
import anitopy
from collections import deque
from threading import Lock

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
OWNER_IDS = list(map(int, os.getenv("OWNER_IDS", "").split(",")))

# Pyrogram Client
app = Client("encoder_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Global variables for CPU monitoring and task queue
monitor_flag = True
task_queue = deque()
queue_lock = Lock()
current_task = None

def monitor_cpu_usage():
    """Monitors CPU usage in a separate thread."""
    while monitor_flag:
        cpu_usage = psutil.cpu_percent(interval=1, percpu=False)
        print(f"Current CPU Usage: {cpu_usage}%")

def sanitize_filename(filename):
    """Sanitize the filename to remove unsafe characters."""
    return re.sub(r'[<>:"/\\|?*]', '_', filename)

def get_audio_streams_count(file_path):
    """Returns the number of audio streams in a video file using FFprobe."""
    try:
        ffprobe_command = [
            "ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", 
            "stream=codec_type", "-of", "default=noprint_wrappers=1", file_path
        ]
        result = subprocess.run(ffprobe_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return len(result.stdout.splitlines()) if result.returncode == 0 else 0
    except Exception as e:
        print(f"Error checking audio streams: {e}")
        return 0

def fetch_anilist_cover(anime_title):
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
    try:
        response = requests.post('https://graphql.anilist.co', json={'query': query, 'variables': variables})
        if response.status_code == 200:
            data = response.json()
            return data.get('data', {}).get('Media', {}).get('coverImage', {}).get('large')
    except Exception as e:
        print(f"AniList API Error: {e}")
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
        print(f"Error downloading cover image: {e}")
    return False

def auto_rename_with_anitopy(file_path):
    """Renames the file and fetches AniList cover image."""
    try:
        video = anitopy.parse(os.path.basename(file_path))
        if not video:
            return file_path, None

        # Extract anime title
        series_title = sanitize_filename(video.get("anime_title", "Unknown"))
        if len(series_title) > 30:
            series_title = series_title[:30].strip()

        # Create new filename
        season = video.get("anime_season", "1").zfill(2)
        episode = video.get("episode_number", "01").zfill(2)
        audio_type = ["Sub", "Dual", "Tri"][min(get_audio_streams_count(file_path), 2)]
        output_name = f"{series_title} S{season}E{episode} [{audio_type}].mkv"
        new_file_path = os.path.join(os.path.dirname(file_path), output_name)
        os.rename(file_path, new_file_path)

        # Get AniList cover
        cover_url = fetch_anilist_cover(series_title)
        if not cover_url:
            return new_file_path, None

        thumbnail_path = f"{os.path.splitext(new_file_path)[0]}_cover.jpg"
        return new_file_path, thumbnail_path if download_cover_image(cover_url, thumbnail_path) else None

    except Exception as e:
        print(f"Renaming error: {e}")
        return file_path, None

def download_video_with_actual_name(url, progress_message):
    """Downloads a video file from a URL while preserving the actual filename."""
    try:
        progress_message.edit("📥 Starting download...")
        result = subprocess.run(
            ["wget", "--content-disposition", url, "--progress=dot:mega"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True
        )
        if result.returncode == 0:
            for line in result.stderr.split('\n'):
                if "‘" in line and "’ saved" in line:
                    filename = sanitize_filename(line.split("‘")[1].split("’")[0])
                    progress_message.edit(f"✅ Download completed: {filename}")
                    return os.path.abspath(filename)
        progress_message.edit("❌ Download failed")
        return None
    except Exception as e:
        progress_message.edit(f"❌ Download error: {e}")
        return None

def encode_video(input_file, output_file, progress_message):
    """Encodes a video using FFmpeg with progress updates."""
    global monitor_flag
    monitor_flag = True
    cpu_thread = threading.Thread(target=monitor_cpu_usage)
    cpu_thread.start()

    try:
        ffmpeg_command = [
            "ffmpeg", "-i", input_file, "-preset", "faster", "-c:v", "libx265",
            "-crf", "20", "-tune", "animation", "-pix_fmt", "yuv420p10le",
            "-threads", "16", "-metadata", "title=Encoded By @THECIDANIME",
            "-c:a", "aac", "-c:s", "copy", output_file
        ]
        process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, universal_newlines=True)
        
        last_update = time()
        for line in process.stderr:
            if time() - last_update > 10 and "frame=" in line:
                progress_message.edit(f"⚙️ Encoding...\n{line.strip()}")
                last_update = time()
        
        if process.wait() == 0:
            progress_message.edit("✅ Encoding completed!")
        else:
            raise RuntimeError("Encoding failed")
    finally:
        monitor_flag = False
        cpu_thread.join()

def process_queue():
    """Processes tasks from the queue one at a time."""
    global current_task
    while True:
        with queue_lock:
            if not task_queue:
                current_task = None
                break
            current_task = task_queue.popleft()

        try:
            current_task()
        except Exception as e:
            print(f"Task error: {e}")
        finally:
            current_task = None

def cleanup_files(*paths):
    """Handles file cleanup for multiple paths"""
    for path in paths:
        if path and os.path.exists(path):
            try: os.remove(path)
            except: pass

@app.on_message(filters.private & (filters.text | filters.command("queue")))
def handle_message(client, message):
    """Handles video URL messages."""
    if message.from_user.id not in OWNER_IDS:
        return message.reply("❌ Access denied!")

    if message.command and message.command[0] == "queue":
        with queue_lock:
            status = "Current Queue:\n" + "\n".join(
                [f"{i+1}. {t.__name__}" for i, t in enumerate(task_queue)]
            ) if task_queue else "Queue is empty"
        return message.reply(status)

    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        return message.reply("❌ Invalid URL!")

    progress = message.reply("📥 Added to queue...")

    def task():
        try:
            # Download
            file_path = download_video_with_actual_name(url, progress)
            if not file_path: return

            # Encode
            output_file = f"{os.path.splitext(file_path)[0]}_encoded.mkv"
            encode_video(file_path, output_file, progress)

            # Rename and get thumbnail
            output_file, thumbnail = auto_rename_with_anitopy(output_file)

            # Upload
            progress.edit("📤 Uploading...")
            client.send_document(
                message.chat.id,
                output_file,
                thumb=thumbnail,
                caption=os.path.basename(output_file)
            )
            progress.edit("✅ Done!")

        except Exception as e:
            progress.edit(f"❌ Error: {e}")
        finally:
            cleanup_files(file_path, output_file, thumbnail)

    with queue_lock:
        task_queue.append(task)
        if not current_task:
            threading.Thread(target=process_queue).start()

@app.on_message(filters.private & filters.document)
def handle_file_upload(client, message):
    """Handles video file uploads."""
    if message.from_user.id not in OWNER_IDS:
        return message.reply("❌ Access denied!")

    progress = message.reply("📥 Added to queue...")

    def task():
        try:
            # Download
            file_path = message.download(file_name=message.document.file_name)
            
            # Encode
            output_file = f"{os.path.splitext(file_path)[0]}_encoded.mkv"
            encode_video(file_path, output_file, progress)

            # Rename and get thumbnail
            output_file, thumbnail = auto_rename_with_anitopy(output_file)

            # Upload
            progress.edit("📤 Uploading...")
            client.send_document(
                message.chat.id,
                output_file,
                thumb=thumbnail,
                caption=os.path.basename(output_file)
            )
            progress.edit("✅ Done!")

        except Exception as e:
            progress.edit(f"❌ Error: {e}")
        finally:
            cleanup_files(file_path, output_file, thumbnail)

    with queue_lock:
        task_queue.append(task)
        if not current_task:
            threading.Thread(target=process_queue).start()

if __name__ == "__main__":
    app.run()
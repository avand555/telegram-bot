import os
import re
import gc
import io
import math
import time
import uuid
import shutil
import random
import secrets
import asyncio
import mimetypes
import subprocess
import datetime
import threading
import zipfile
import base64
import html
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse, parse_qs

# ============================================================
# MIME TYPES
# ============================================================
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")

# ============================================================
# TELEGRAM
# ============================================================
from telethon import TelegramClient, events, Button
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import (
    SaveBigFilePartRequest,
    SaveFilePartRequest,
)
from telethon.tl.types import InputFileBig, InputFile

# ============================================================
# WEB / STORAGE
# ============================================================
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector
import aiohttp
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

import yt_dlp
import nest_asyncio

nest_asyncio.apply()

# ============================================================
# 1. CONFIGURATION
# ============================================================

def required_env(name, default=None):
    value = os.environ.get(name, "").strip()
    if not value:
        if default is not None:
            print(f"⚠️ Warning: {name} not set, using default")
            return default
        raise RuntimeError(f"❌ Missing required environment variable: {name}")
    return value

API_ID_RAW = required_env("API_ID")
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    raise RuntimeError("API_ID must be a number")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "716887656"))

# R2
R2_ACCOUNT_ID = required_env("R2_ACCOUNT_ID").strip()
R2_ACCESS_KEY_ID = required_env("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = required_env("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = required_env("R2_BUCKET_NAME")
R2_PUBLIC_URL = required_env("R2_PUBLIC_URL").rstrip("/")

# Dashboard
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin").strip()
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "admin123").strip()

if DASHBOARD_USER == "admin":
    print("⚠️ SECURITY WARNING: Using default DASHBOARD_USER='admin'. Please set a custom DASHBOARD_USER.")
if DASHBOARD_PASS == "admin123":
    print("⚠️ SECURITY WARNING: Using default DASHBOARD_PASS='admin123'. Please set a strong DASHBOARD_PASS.")

# Koyeb
PORT = int(os.environ.get("PORT", "8000"))
KOYEB_PUBLIC_URL = os.environ.get("KOYEB_PUBLIC_URL", "").strip().rstrip("/")
KOYEB_APP_NAME = os.environ.get("KOYEB_APP_NAME", "").strip()

PUBLIC_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce,"
    "http://tracker.openbittorrent.com:80/announce,"
    "udp://opentracker.i2p.rocks:6969/announce"
)

global_semaphore = asyncio.Semaphore(4)
routes = web.RouteTableDef()
link_storage = {}
active_tasks = {}

# ============================================================
# TELEGRAM CLIENT
# ============================================================
client = TelegramClient(
    "bot_session",
    API_ID,
    API_HASH,
    connection=ConnectionTcpFull,
    use_ipv6=False,
    request_retries=15,
    connection_retries=15,
    retry_delay=3,
)

# ============================================================
# 2. SYSTEM HELPERS
# ============================================================

def force_system_ram_purge():
    try:
        gc.collect()
    except Exception:
        pass
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass

def free_memory():
    force_system_ram_purge()

def human_size(value):
    try:
        value = float(value)
    except Exception:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"

def get_status_text(action, filename, current, total, start_time):
    elapsed = max(time.time() - start_time, 0.001)
    try:
        current = int(current)
    except Exception:
        current = 0
    try:
        total = int(total)
    except Exception:
        total = 0
    percent = (current / total) * 100 if total else 0
    speed = current / elapsed
    blocks = min(10, int(percent // 10))
    p_bar = "■" * blocks + "□" * (10 - blocks)
    return (
        f"🚀 **{action}**\n"
        f"📦 `{filename}`\n\n"
        f"🌀 **Progress:** `[{p_bar}] {percent:.2f}%`\n"
        f"⚡ **Speed:** `{human_size(speed)}/s`\n"
        f"📂 **Size:** `{human_size(current)} / {human_size(total)}`"
    )

def format_saas_progress(action, filename, percent, downloaded, total, speed, eta, cn, elapsed, task_code):
    percent = int(percent or 0)
    done = int(percent // 10)
    p_bar = "●" * done + ("◔" if percent % 10 >= 5 else "")
    p_bar += "○" * (10 - len(p_bar))
    return (
        f"🧲 **{action}...**\n"
        f"╭ `[{p_bar[:10]}]` » `{percent}%`\n"
        f"├ **Processed:** `{downloaded} of {total}`\n"
        f"├ **Speed:** `{speed}`\n"
        f"├ **ETA:** `{eta}`\n"
        f"├ **Peers:** `{cn}`\n"
        f"├ **Elapsed:** `{elapsed}`\n"
        f"╰ **Cancel:** `/c_{task_code}`"
    )

def get_readable_time(seconds):
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    result = ""
    if days: result += f"{days}d "
    if hours: result += f"{hours}h "
    if minutes: result += f"{minutes}m "
    result += f"{seconds}s"
    return result

def get_largest_file(folder_path):
    largest = None
    max_size = 0
    for root, _, files in os.walk(folder_path):
        for filename in files:
            path = os.path.join(root, filename)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > max_size:
                max_size = size
                largest = path
    return largest

def clean_double_extension(filename):
    if not filename: return filename
    while filename.lower().endswith((".mp4.mp4", ".mkv.mkv", ".zip.zip", ".webm.webm")):
        filename = filename.rsplit(".", 1)[0]
    return filename

def sanitize_filename(filename):
    filename = unquote(filename or "")
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = filename.strip().strip(".")
    return clean_double_extension(filename)

def get_unique_filename(filepath):
    filepath = clean_double_extension(filepath)
    if not os.path.exists(filepath):
        return filepath
    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"

# ============================================================
# 3. R2
# ============================================================

def get_r2_client():
    account_id = R2_ACCOUNT_ID.strip()
    # Clean account ID - remove any URL prefixes if mistakenly provided
    if "://" in account_id:
        account_id = account_id.split("://")[-1].split(".")[0].split("/")[0]
    
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    
    r2_config = Config(
        region_name="auto",
        signature_version="s3v4",
        retries={"max_attempts": 10, "mode": "adaptive"},
        connect_timeout=30,
        read_timeout=300,
    )
    
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=r2_config,
    )

def sync_get_smart_dashboard_data(prefix=""):
    s3 = get_r2_client()
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix, Delimiter="/")
    
    all_objects = []
    common_prefixes = []
    for page in pages:
        if "Contents" in page:
            all_objects.extend(page["Contents"])
        if "CommonPrefixes" in page:
            common_prefixes.extend(page["CommonPrefixes"])
            
    hls_bases = set()
    for obj in all_objects:
        key = obj["Key"]
        if key.endswith("master.m3u8"):
            hls_bases.add(os.path.dirname(key))
            
    hls_packages = {}
    for base in hls_bases:
        hls_packages[base] = {"name": base, "size": 0, "date": None, "type": "HLS", "url_key": f"{base}/master.m3u8"}
        
    standalone_files = []
    total_size = 0
    mp4_count = 0
    sorted_bases = sorted(list(hls_bases), key=len, reverse=True)
    
    for obj in all_objects:
        key = obj["Key"]
        size = obj["Size"]
        date = obj["LastModified"]
        total_size += size
        if key.endswith("/") and size == 0:
            continue
            
        is_hls_part = False
        for base in sorted_bases:
            if key.startswith(base + "/") or key == base:
                hls_packages[base]["size"] += size
                current_date = hls_packages[base]["date"]
                if current_date is None or date > current_date:
                    hls_packages[base]["date"] = date
                is_hls_part = True
                break
                
        if not is_hls_part and not key.endswith("/"):
            standalone_files.append({"name": key, "size": size, "date": date, "type": "FILE", "url_key": key})
            if key.lower().endswith(".mp4"):
                mp4_count += 1
                
    items = list(hls_packages.values()) + standalone_files
    items.sort(key=lambda x: (x["date"] if x["date"] else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)), reverse=True)
    
    return {
        "total_size": total_size,
        "mp4_count": mp4_count,
        "hls_count": len(hls_packages),
        "items": items,
        "common_prefixes": common_prefixes,
    }

def sync_delete_r2_file(s3_key):
    s3 = get_r2_client()
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)

def sync_delete_r2_folder(prefix):
    s3 = get_r2_client()
    prefix = prefix.rstrip("/") + "/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
        contents = page.get("Contents", [])
        if not contents: continue
        objects = [{"Key": obj["Key"]} for obj in contents]
        s3.delete_objects(Bucket=R2_BUCKET_NAME, Delete={"Objects": objects})

def sync_rename_r2_file(old_key, new_key):
    s3 = get_r2_client()
    s3.copy({"Bucket": R2_BUCKET_NAME, "Key": old_key}, R2_BUCKET_NAME, new_key)
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=old_key)

def sync_rename_r2_folder(old_prefix, new_prefix):
    s3 = get_r2_client()
    old_prefix = old_prefix.rstrip("/") + "/"
    new_prefix = new_prefix.rstrip("/") + "/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=old_prefix):
        for obj in page.get("Contents", []):
            old_key = obj["Key"]
            new_key = new_prefix + old_key[len(old_prefix):]
            s3.copy({"Bucket": R2_BUCKET_NAME, "Key": old_key}, R2_BUCKET_NAME, new_key)
            s3.delete_object(Bucket=R2_BUCKET_NAME, Key=old_key)

def sync_r2_upload(file_path, s3_key, loop, msg, start_t):
    s3 = get_r2_client()
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or "application/octet-stream"
    
    class ProgressCallback:
        def __init__(self):
            self.seen = 0
            self.last = 0
        def __call__(self, amount):
            self.seen += amount
            if time.time() - self.last < 4: return
            self.last = time.time()
            try:
                asyncio.run_coroutine_threadsafe(
                    msg.edit(get_status_text("R2 Uploading", filename, self.seen, file_size, start_t)), loop
                )
            except Exception:
                pass
                
    extra_args = {"ContentType": mime_type}
    if not filename.lower().endswith((".m3u8", ".ts")):
        extra_args["ContentDisposition"] = "inline"
        
    config = TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024, max_concurrency=4)
    s3.upload_file(file_path, R2_BUCKET_NAME, s3_key, Callback=ProgressCallback(), ExtraArgs=extra_args, Config=config)

def sync_r2_upload_folder(folder_path, s3_prefix, loop, msg, start_t):
    s3 = get_r2_client()
    files = []
    total_size = 0
    for root, _, filenames in os.walk(folder_path):
        for filename in filenames:
            path = os.path.join(root, filename)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            files.append(path)
            total_size += size
            
    class ProgressCallback:
        def __init__(self):
            self.seen = 0
            self.last = 0
            self.lock = threading.Lock()
        def __call__(self, amount):
            with self.lock:
                self.seen += amount
                if time.time() - self.last < 4: return
                self.last = time.time()
                try:
                    asyncio.run_coroutine_threadsafe(
                        msg.edit(get_status_text("R2 HLS Sync", s3_prefix, self.seen, total_size, start_t)), loop
                    )
                except Exception:
                    pass
                    
    callback = ProgressCallback()
    def upload_one(path):
        relative = os.path.relpath(path, folder_path)
        s3_key = f"{s3_prefix.strip('/')}/{relative.replace(os.sep, '/')}"
        content_type, _ = mimetypes.guess_type(path)
        extra_args = {"ContentType": content_type or "application/octet-stream"}
        ext = os.path.splitext(path)[1].lower()
        if ext not in [".m3u8", ".ts"]:
            extra_args["ContentDisposition"] = "inline"
        s3.upload_file(path, R2_BUCKET_NAME, s3_key, Callback=callback, ExtraArgs=extra_args)
        
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(upload_one, files))

async def upload_to_r2(file_path, msg, target_folder=None):
    start_t = time.time()
    loop = asyncio.get_running_loop()
    basename = os.path.basename(file_path)
    
    if target_folder:
        s3_key = f"{target_folder.strip('/')}/{basename}"
    else:
        now = datetime.datetime.now()
        s3_key = f"{now.year}/{now.month:02d}/{now.day:02d}/{basename}"
        
    await msg.edit("⬆️ **Connecting to Cloudflare R2...**\n" f"🎬 `{basename}`")
    await asyncio.to_thread(sync_r2_upload, file_path, s3_key, loop, msg, start_t)
    
    code = secrets.token_urlsafe(8)
    link_storage[code] = {"s3_key": s3_key, "timestamp": time.time()}
    url = f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}"
    return url, code

# ============================================================
# 4. ARIA2 / TORRENT
# ============================================================

def get_aria2_executable():
    system_aria = shutil.which("aria2c")
    if system_aria: return system_aria
    
    local = os.path.abspath("./aria2c")
    if os.path.exists(local): return local
    
    print("⚠️ aria2c not found. Attempting to install...")
    try:
        subprocess.run(["apt-get", "update"], check=True, capture_output=True, timeout=60)
        subprocess.run(["apt-get", "install", "-y", "aria2"], check=True, capture_output=True, timeout=120)
        system_aria = shutil.which("aria2c")
        if system_aria:
            print("✅ aria2c installed successfully via apt")
            return system_aria
    except Exception as apt_error:
        print(f"⚠️ apt installation failed: {apt_error}")
        try:
            import platform
            machine = platform.machine()
            if machine in ("x86_64", "AMD64"):
                aria_url = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-linux-gnu-64bit-build1.tar.bz2"
            else:
                aria_url = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-linux-gnu-32bit-build1.tar.bz2"
            
            subprocess.run(
                f"wget -q {aria_url} -O aria2.tar.bz2 && "
                "tar -xjf aria2.tar.bz2 && "
                "mv aria2-1.37.0-linux-gnu-*/aria2c ./aria2c && "
                "chmod +x ./aria2c && "
                "rm -rf aria2.tar.bz2 aria2-1.37.0-linux-gnu-*",
                shell=True, check=True, timeout=180
            )
            if os.path.exists(local):
                print("✅ aria2c downloaded successfully")
                return local
        except Exception as download_error:
            print(f"❌ aria2c installation failed: {download_error}")
            return None
    return shutil.which("aria2c")

async def download_magnet(url, workspace, custom_name, msg, start_t):
    aria2 = get_aria2_executable()
    if not aria2:
        raise RuntimeError("aria2c is not available. Cannot download magnet links.")
        
    cmd = [
        aria2, "--seed-time=0", "--max-connection-per-server=16", "--split=16",
        "--summary-interval=3", "--bt-stop-timeout=120", f"--bt-tracker={PUBLIC_TRACKERS}",
        f"--dir={workspace}", url
    ]
    
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    task_code = secrets.token_urlsafe(8)
    active_tasks[task_code] = {"process": process, "cancel_event": asyncio.Event(), "dir": workspace}
    
    aria_re = re.compile(r"\[#(?P<gid>\w+)\s+(?P<downloaded>[^\s/]+)/(?P<total>[^\s\(\)]+)(?:\((?P<percent>\d+)%\))?\s+CN:(?P<cn>\d+)\s+SPD:(?P<speed>[^\s\]]+)(?:\s+ETA:(?P<eta>[^\s\]]+))?\]")
    last_update = 0
    
    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes: break
        line = line_bytes.decode("utf-8", errors="ignore").strip()
        match = aria_re.search(line)
        
        if match and time.time() - last_update > 4:
            elapsed = get_readable_time(time.time() - start_t)
            active_file = "Fetching Metadata..."
            for _, _, files in os.walk(workspace):
                for filename in files:
                    if not filename.endswith(".aria2"):
                        active_file = filename
                        break
                        
            p_text = format_saas_progress("Download", active_file, int(match.group("percent") or 0), match.group("downloaded"), match.group("total"), match.group("speed") + "/s", match.group("eta") or "Calc...", match.group("cn"), elapsed, task_code)
            try:
                await msg.edit(p_text, buttons=[[Button.inline("❌ Cancel", data=f"canceltask_{task_code}")]])
                last_update = time.time()
            except Exception:
                pass
                
    await process.wait()
    active_tasks.pop(task_code, None)
    
    if process.returncode not in (0, None):
        raise RuntimeError(f"aria2c failed with exit code {process.returncode}")
        
    largest = get_largest_file(workspace)
    if not largest:
        raise ValueError("Torrent failed: no downloaded file.")
        
    target = custom_name or os.path.basename(largest)
    final_name = get_unique_filename(os.path.join(workspace, sanitize_filename(target)))
    
    if os.path.abspath(largest) != os.path.abspath(final_name):
        shutil.move(largest, final_name)
    return final_name

# ============================================================
# 5. YT-DLP
# ============================================================

def sync_yt_dlp_download(url, workspace, custom_name=None):
    if custom_name:
        output = os.path.join(workspace, sanitize_filename(custom_name))
    else:
        output = os.path.join(workspace, "%(title)s.%(ext)s")
        
    ydl_opts = {
        "outtmpl": output, "quiet": True, "no_warnings": True, "nocheckcertificate": True,
        "noplaylist": True, "retries": 5, "fragment_retries": 5, "socket_timeout": 60,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = ydl.prepare_filename(info)
        return clean_double_extension(prepared)

# ============================================================
# 6. GOOGLE DRIVE
# ============================================================

def extract_gdrive_id(url):
    if not url: return None
    patterns = [r"/file/d/([a-zA-Z0-9_-]+)", r"/d/([a-zA-Z0-9_-]+)", r"[?&]id=([a-zA-Z0-9_-]+)"]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match: return match.group(1)
    return None

async def close_response(resp):
    try: resp.release()
    except Exception: pass
    try: await resp.wait_for_close()
    except Exception: pass

async def gdrive_request(session, file_id):
    urls_to_try = [
        {"url": "https://drive.usercontent.google.com/download", "params": {"id": file_id, "export": "download"}},
        {"url": "https://drive.google.com/uc", "params": {"id": file_id, "export": "download", "confirm": "t"}},
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    
    last_error = None
    for attempt, request_config in enumerate(urls_to_try, 1):
        try:
            async with session.get(
                request_config["url"], params=request_config["params"], headers=headers,
                allow_redirects=True, timeout=ClientTimeout(total=300)
            ) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if response.status == 200 and "text/html" not in content_type:
                    return response
                
                html_content = await response.text(errors="ignore")
                
                confirm_token = None
                token_patterns = [r'name="confirm"\s+value="([^"]+)"', r'confirm=([a-zA-Z0-9_-]{10,})', r'"confirm"\s*:\s*"([^"]+)"']
                for pattern in token_patterns:
                    match = re.search(pattern, html_content, re.IGNORECASE)
                    if match:
                        confirm_token = match.group(1)
                        break
                
                if "quota" in html_content.lower() or "too many users" in html_content.lower():
                    raise RuntimeError("Google Drive download quota exceeded. Try again later.")
                if "permission" in html_content.lower() or "request access" in html_content.lower():
                    raise RuntimeError("Google Drive file is not publicly accessible.")
                
                if confirm_token:
                    params_with_confirm = request_config["params"].copy()
                    params_with_confirm["confirm"] = confirm_token
                    
                    async with session.get(
                        request_config["url"], params=params_with_confirm, headers=headers,
                        allow_redirects=True, timeout=ClientTimeout(total=300)
                    ) as confirm_response:
                        content_type = confirm_response.headers.get("Content-Type", "").lower()
                        if confirm_response.status == 200 and "text/html" not in content_type:
                            return confirm_response
                        
                        error_text = await confirm_response.text(errors="ignore")
                        if "virus scan" in error_text.lower():
                            raise RuntimeError("Google Drive requires virus scan confirmation. File is too large for automatic download.")
                
                last_error = RuntimeError("Google Drive returned HTML instead of file")
        except asyncio.TimeoutError:
            last_error = RuntimeError(f"Google Drive request timed out (attempt {attempt})")
            continue
        except aiohttp.ClientError as e:
            last_error = RuntimeError(f"Google Drive connection error: {str(e)}")
            continue
        except Exception as e:
            last_error = RuntimeError(f"Google Drive error: {str(e)}")
            continue
            
    raise last_error or RuntimeError("Google Drive download failed after all attempts")

async def get_gdrive_stream(session, file_id):
    return await gdrive_request(session, file_id)

# ============================================================
# 7. DIRECT DOWNLOADER
# ============================================================

async def download_direct(url, workspace, msg, start_t, custom_name=None, gdrive_id=None):
    timeout = ClientTimeout(total=None, sock_connect=60, sock_read=600)
    connector = TCPConnector(limit=16, ttl_dns_cache=300, use_dns_cache=True)
    
    async with ClientSession(timeout=timeout, connector=connector, cookie_jar=aiohttp.CookieJar()) as session:
        if gdrive_id:
            await msg.edit("🅿️ **Google Drive detected**\n🔄 Getting direct file stream...")
            response = await get_gdrive_stream(session, gdrive_id)
        else:
            max_retries = 3
            last_error = None
            for attempt in range(max_retries):
                try:
                    response = await session.get(
                        url, allow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}
                    )
                    if response.status < 400:
                        break
                    await close_response(response)
                    last_error = RuntimeError(f"HTTP {response.status}")
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue
            else:
                raise last_error or RuntimeError("Download failed after retries")
            
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                await close_response(response)
                raise ValueError("HTML webpage detected. This is not a direct file URL.")

        content_length = response.headers.get("Content-Length")
        try:
            file_size = int(content_length) if content_length else 0
        except Exception:
            file_size = 0

        filename = sanitize_filename(custom_name) if custom_name else None
        if not filename:
            content_disposition = response.headers.get("Content-Disposition", "")
            match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
            if match:
                filename = sanitize_filename(match.group(1))
            if not filename:
                match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
                if match:
                    filename = sanitize_filename(match.group(1))
        
        if not filename and not gdrive_id:
            parsed = urlparse(url)
            filename = sanitize_filename(os.path.basename(unquote(parsed.path)))
        
        if not filename:
            filename = "download.bin"
        if "." not in filename:
            filename += ".mp4"
            
        file_path = os.path.join(workspace, filename)
        file_path = get_unique_filename(file_path)
        
        await msg.edit(f"⬇️ **Downloading...**\n🎬 `{os.path.basename(file_path)}`")
        
        downloaded = 0
        last_update = 0
        
        try:
            with open(file_path, "wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    if not chunk: continue
                    file.write(chunk)
                    downloaded += len(chunk)
                    if time.time() - last_update >= 4:
                        try:
                            await msg.edit(get_status_text("Leeching", os.path.basename(file_path), downloaded, file_size, start_t))
                        except Exception:
                            pass
                        last_update = time.time()
        finally:
            await close_response(response)
            
        if not os.path.exists(file_path):
            raise RuntimeError("Download produced no file.")
            
        actual_size = os.path.getsize(file_path)
        if actual_size <= 0:
            try: os.remove(file_path)
            except Exception: pass
            raise RuntimeError("Downloaded file is empty.")
            
        return file_path

# ============================================================
# 8. URL ROUTER
# ============================================================

async def download_any_url(url, workspace, custom_name, msg, start_t):
    if url.lower().startswith("magnet:?"):
        return await download_magnet(url, workspace, custom_name, msg, start_t)
        
    gdrive_id = extract_gdrive_id(url)
    is_zip = (custom_name and custom_name.lower().endswith(".zip")) or url.lower().split("?")[0].endswith(".zip")
    
    if gdrive_id:
        return await download_direct(url, workspace, msg, start_t, custom_name, gdrive_id)
    if is_zip:
        return await download_direct(url, workspace, msg, start_t, custom_name)
        
    try:
        await msg.edit("🅿️ **Extracting file information...**")
        filename = await asyncio.to_thread(sync_yt_dlp_download, url, workspace, custom_name)
        if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
            return filename
    except Exception:
        pass
        
    return await download_direct(url, workspace, msg, start_t, custom_name)

# ============================================================
# 9. DASHBOARD AUTH
# ============================================================

def check_dashboard_auth(request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        encoded = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        user, password = decoded.split(":", 1)
        return user == DASHBOARD_USER and password == DASHBOARD_PASS
    except Exception:
        return False

# ============================================================
# 10. DASHBOARD CSS / JS
# ============================================================

DASHBOARD_CSS = """
:root { --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --border: #334155; }
* { box-sizing: border-box; }
body { font-family: Segoe UI, system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
.container { max-width: 1200px; margin: auto; }
h2 { margin: 0 0 20px; border-bottom: 2px solid var(--border); padding-bottom: 10px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
.stat-card { background: var(--card); padding: 15px; border-radius: 10px; border: 1px solid var(--border); }
.stat-title { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: bold; }
.stat-val { color: var(--accent); font-size: 20px; font-weight: bold; margin-top: 5px; }
.controls { display: flex; gap: 10px; margin-bottom: 15px; }
.search-box { flex-grow: 1; padding: 14px 20px; border-radius: 8px; border: 1px solid var(--border); background: var(--card); color: var(--text); font-size: 15px; }
.table-wrapper { overflow-x: auto; background: var(--card); border-radius: 10px; border: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; min-width: 900px; }
th { background: #0f172a; color: var(--muted); padding: 16px; text-align: left; cursor: pointer; }
td { padding: 16px; border-bottom: 1px solid var(--border); word-break: break-word; }
tr:hover { background: #334155; }
.folder-link { color: #fbbf24; text-decoration: none; font-weight: bold; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.btn { display: inline-flex; align-items: center; justify-content: center; padding: 8px 14px; border-radius: 6px; border: none; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; }
.btn-create { background: rgba(16,185,129,.2); color: #34d399; padding: 14px 20px; border: 1px solid #10b981; }
.btn-copy { background: rgba(59,130,246,.1); color: var(--accent); }
.btn-view { background: rgba(16,185,129,.1); color: #34d399; }
.btn-move { background: rgba(167,139,250,.1); color: #c084fc; }
.btn-rename { background: rgba(251,191,36,.1); color: #fbbf24; }
.btn-delete { background: rgba(244,63,94,.1); color: #fb7185; }
.breadcrumbs { margin-bottom: 15px; color: var(--muted); }
.breadcrumb-link { color: var(--accent); text-decoration: none; }
"""

DASHBOARD_JS = r"""
function copyText(text) { navigator.clipboard.writeText(text).then(() => alert("✅ URL Copied!")).catch(() => prompt("Copy URL:", text)); }
function deleteItem(key, isHLS, prefix) {
    const dec = decodeURIComponent(key);
    const message = isHLS ? "⚠️ DELETE ENTIRE HLS FOLDER:\n" + dec : "⚠️ DELETE FILE:\n" + dec;
    if (!confirm(message)) return;
    const url = isHLS ? "/delete_folder?prefix=" : "/delete_file?key=";
    window.location.href = url + encodeURIComponent(dec) + "&curr_prefix=" + encodeURIComponent(prefix);
}
function renameItem(key, isHLS, prefix) {
    const dec = decodeURIComponent(key);
    const newKey = prompt("✏️ Rename (Full Path):", dec);
    if (!newKey || newKey === dec) return;
    if (isHLS) {
        window.location.href = "/rename_folder?old_prefix=" + encodeURIComponent(dec) + "&new_prefix=" + encodeURIComponent(newKey) + "&prefix=" + encodeURIComponent(prefix);
    } else {
        window.location.href = "/rename_file?old_key=" + encodeURIComponent(dec) + "&new_key=" + encodeURIComponent(newKey) + "&prefix=" + encodeURIComponent(prefix);
    }
}
function moveItem(key, isHLS, prefix) {
    const dec = decodeURIComponent(key);
    const currentDir = dec.includes("/") ? dec.substring(0, dec.lastIndexOf("/")) : "";
    const target = prompt("📁 Move to Folder:", currentDir);
    if (target === null) return;
    window.location.href = "/move_item?old_key=" + encodeURIComponent(dec) + "&target_folder=" + encodeURIComponent(target) + "&type=" + (isHLS ? "FOLDER" : "FILE") + "&prefix=" + encodeURIComponent(prefix);
}
function createFolder() {
    const path = prompt("📁 Enter new folder path:");
    if (path) window.location.href = "/create_folder?path=" + encodeURIComponent(path);
}
function filterTable() {
    const input = document.getElementById("searchInput").value.toLowerCase();
    document.querySelectorAll("tbody tr").forEach(row => {
        const filename = row.querySelector(".file-name")?.innerText.toLowerCase() || "";
        row.style.display = filename.includes(input) ? "" : "none";
    });
}
let currentSort = { col: -1, dir: "asc" };
function sortTable(colIndex, type) {
    const table = document.querySelector("tbody");
    const rows = Array.from(table.querySelectorAll("tr"));
    if (!rows.length) return;
    const dir = currentSort.col === colIndex && currentSort.dir === "asc" ? "desc" : "asc";
    currentSort = { col: colIndex, dir: dir };
    rows.sort((a, b) => {
        const valA = a.children[colIndex]?.getAttribute("data-val") || "";
        const valB = b.children[colIndex]?.getAttribute("data-val") || "";
        if (type === "num") return dir === "asc" ? parseFloat(valA) - parseFloat(valB) : parseFloat(valB) - parseFloat(valA);
        return dir === "asc" ? valA.localeCompare(valB) : valB.localeCompare(valA);
    });
    table.innerHTML = "";
    rows.forEach(row => table.appendChild(row));
}
"""

# ============================================================
# 11. DASHBOARD
# ============================================================

@routes.get("/dashboard")
async def dashboard_handler(request):
    if not check_dashboard_auth(request):
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Cloudflare R2 Dashboard"'}, text="🔒 Access Denied")
        
    prefix = request.query.get("prefix", "").strip("/")
    prefix = prefix + "/" if prefix else ""
    parts = [p for p in prefix.split("/") if p]
    
    breadcrumbs = '<a href="/dashboard" class="breadcrumb-link">🏠 Home</a>'
    current = ""
    for part in parts:
        current += part + "/"
        breadcrumbs += f' <span>/</span> <a href="/dashboard?prefix={quote(current)}" class="breadcrumb-link">{html.escape(part)}</a>'
        
    try:
        data = await asyncio.to_thread(sync_get_smart_dashboard_data, prefix)
        file_rows = ""
        
        for pref in data.get("common_prefixes", []):
            folder_path = pref["Prefix"]
            folder_name = folder_path.rstrip("/").split("/")[-1]
            file_rows += f"""
<tr>
<td data-val="{html.escape(folder_name)}"><a href="/dashboard?prefix={quote(folder_path)}" class="folder-link">📁 <span class="file-name">{html.escape(folder_name)}</span></a></td>
<td data-val="0">-</td><td data-val="0">Folder</td>
<td><div class="actions">
<button class="btn btn-move" onclick="moveItem('{quote(folder_path)}', true, '{quote(prefix)}')">📁 Move</button>
<button class="btn btn-rename" onclick="renameItem('{quote(folder_path)}', true, '{quote(prefix)}')">✏️ Rename</button>
<button class="btn btn-delete" onclick="deleteItem('{quote(folder_path)}', true, '{quote(prefix)}')">🗑️ Delete</button>
</div></td></tr>"""
            
        for item in data["items"]:
            name = item["name"]
            size = item["size"]
            size_str = human_size(size) if size > 0 else "-"
            date = item["date"]
            date_str = date.strftime("%Y-%m-%d %H:%M") if date else "-"
            date_val = date.timestamp() if date else 0
            item_type = item["type"]
            
            display_name = name.rstrip("/").split("/")[-1] if item_type == "HLS" else name.split("/")[-1]
            url = f"{R2_PUBLIC_URL}/{quote(item['url_key'], safe='/')}"
            safe_name = html.escape(display_name)
            
            actions = f"""
<button class="btn btn-copy" onclick="copyText('{html.escape(url)}')">🔗 Copy {'Master.m3u8' if item_type == 'HLS' else 'URL'}</button>
<a href="{html.escape(url)}" target="_blank" class="btn btn-view">▶️ {'Play' if item_type == 'HLS' else 'Open'}</a>
<button class="btn btn-move" onclick="moveItem('{quote(name)}', {str(item_type == 'HLS').lower()}, '{quote(prefix)}')">📁 Move</button>
<button class="btn btn-rename" onclick="renameItem('{quote(name)}', {str(item_type == 'HLS').lower()}, '{quote(prefix)}')">✏️ Rename</button>
<button class="btn btn-delete" onclick="deleteItem('{quote(name)}', {str(item_type == 'HLS').lower()}, '{quote(prefix)}')">🗑️ Delete</button>
"""
            file_rows += f"""
<tr>
<td data-val="{safe_name}">{'📦' if item_type == 'HLS' else '🎬'} <span class="file-name">{'<b>' + safe_name + '</b> (HLS)' if item_type == 'HLS' else safe_name}</span></td>
<td data-val="{size}">{size_str}</td>
<td data-val="{date_val}">{date_str}</td>
<td><div class="actions">{actions}</div></td>
</tr>"""
            
        if not file_rows:
            file_rows = '<tr><td colspan="4" style="text-align:center;">Directory is empty.</td></tr>'
            
        html_page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Cloudflare Studio</title>
<style>{DASHBOARD_CSS}</style><script>{DASHBOARD_JS}</script></head><body><div class="container">
<h2>🛡️ Cloudflare Studio Dashboard</h2>
<div class="stats-grid">
<div class="stat-card"><div class="stat-title">Total Storage</div><div class="stat-val">{human_size(data.get('total_size', 0))}</div></div>
<div class="stat-card"><div class="stat-title">HLS Packages</div><div class="stat-val">{data.get('hls_count', 0)}</div></div>
<div class="stat-card"><div class="stat-title">MP4 Files</div><div class="stat-val">{data.get('mp4_count', 0)}</div></div>
</div>
<div class="breadcrumbs">{breadcrumbs}</div>
<div class="controls"><input type="text" id="searchInput" class="search-box" onkeyup="filterTable()" placeholder="🔍 Search current folder...">
<button class="btn btn-create" onclick="createFolder()">📁 Create Folder</button></div>
<div class="table-wrapper"><table><thead><tr><th onclick="sortTable(0, 'str')">Name</th><th onclick="sortTable(1, 'num')">Size</th><th onclick="sortTable(2, 'num')">Date Uploaded</th><th>Actions</th></tr></thead><tbody>{file_rows}</tbody></table></div>
</div></body></html>"""
        return web.Response(text=html_page, content_type="text/html")
    except Exception as exc:
        return web.Response(text=f"<h2>Dashboard error</h2><pre>{html.escape(str(exc))}</pre>", content_type="text/html", status=500)

# ============================================================
# 12. DASHBOARD ACTIONS
# ============================================================

@routes.get("/delete_file")
async def web_delete_file(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    key = request.query.get("key")
    if key:
        try:
            await asyncio.to_thread(sync_delete_r2_file, key)
            force_system_ram_purge()
        except Exception: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("curr_prefix", "")))

@routes.get("/delete_folder")
async def web_delete_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    prefix = request.query.get("prefix")
    if prefix:
        try:
            await asyncio.to_thread(sync_delete_r2_folder, prefix)
            force_system_ram_purge()
        except Exception: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("curr_prefix", "")))

@routes.get("/rename_file")
async def web_rename_file(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key, new_key = request.query.get("old_key"), request.query.get("new_key")
    if old_key and new_key and old_key != new_key:
        try:
            await asyncio.to_thread(sync_rename_r2_file, old_key, new_key)
            force_system_ram_purge()
        except Exception: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("prefix", "")))

@routes.get("/rename_folder")
async def web_rename_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_prefix, new_prefix = request.query.get("old_prefix"), request.query.get("new_prefix")
    if old_prefix and new_prefix and old_prefix != new_prefix:
        try:
            await asyncio.to_thread(sync_rename_r2_folder, old_prefix, new_prefix)
            force_system_ram_purge()
        except Exception: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("prefix", "")))

@routes.get("/move_item")
async def web_move_item(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key = request.query.get("old_key")
    target = request.query.get("target_folder", "").strip().strip("/")
    item_type = request.query.get("type", "FILE")
    
    if old_key is not None:
        basename = old_key.rstrip("/").split("/")[-1]
        if item_type in ("HLS", "FOLDER"):
            new_key = f"{target}/{basename}/" if target else f"{basename}/"
            if old_key.rstrip("/") != new_key.rstrip("/"):
                try: await asyncio.to_thread(sync_rename_r2_folder, old_key, new_key)
                except Exception: pass
        else:
            new_key = f"{target}/{basename}" if target else basename
            if old_key != new_key:
                try: await asyncio.to_thread(sync_rename_r2_file, old_key, new_key)
                except Exception: pass
        force_system_ram_purge()
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("prefix", "")))

def sync_create_r2_folder(folder_path):
    s3 = get_r2_client()
    folder_path = folder_path.strip("/") + "/"
    s3.put_object(Bucket=R2_BUCKET_NAME, Key=folder_path)

@routes.get("/create_folder")
async def web_create_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    path = request.query.get("path")
    if path:
        try: await asyncio.to_thread(sync_create_r2_folder, path)
        except Exception: pass
    raise web.HTTPFound("/dashboard")

# ============================================================
# 13. HEALTH CHECK
# ============================================================

@routes.get("/health")
async def health_handler(request):
    health_status = {
        "status": "ok", "service": "telegram-r2-bot", "timestamp": datetime.datetime.utcnow().isoformat(),
        "checks": {"telegram": "unknown", "r2": "unknown"}
    }
    try:
        if client.is_connected(): health_status["checks"]["telegram"] = "connected"
        else:
            health_status["checks"]["telegram"] = "disconnected"
            health_status["status"] = "degraded"
    except Exception:
        health_status["checks"]["telegram"] = "error"
        health_status["status"] = "degraded"
        
    try:
        s3 = get_r2_client()
        s3.head_bucket(Bucket=R2_BUCKET_NAME)
        health_status["checks"]["r2"] = "connected"
    except Exception:
        health_status["checks"]["r2"] = "error"
        health_status["status"] = "degraded"
        
    status_code = 200 if health_status["status"] == "ok" else 503
    return web.json_response(health_status, status=status_code)

@routes.get("/")
async def root_handler(request):
    return web.Response(text="""
<html><body style="background:#0f172a; color:#38bdf8; text-align:center; padding-top:120px; font-family:sans-serif;">
<h1>✅ System Online</h1><p>Telegram R2 Leech Bot</p>
<p><a href="/health" style="color:#0f172a; background:#38bdf8; padding:12px 20px; text-decoration:none; border-radius:6px;">Health Check</a></p>
<p><a href="/dashboard" style="color:#0f172a; background:#34d399; padding:12px 20px; text-decoration:none; border-radius:6px;">Dashboard</a></p>
</body></html>""", content_type="text/html")

# ============================================================
# 14. TELEGRAM DIRECT LINK
# ============================================================

def get_public_base_url():
    if KOYEB_PUBLIC_URL: return KOYEB_PUBLIC_URL
    if KOYEB_APP_NAME: return f"https://{KOYEB_APP_NAME}.koyeb.app"
    return ""

@routes.get("/{code}/{filename}")
async def stream_handler(request):
    code = request.match_info["code"]
    data = link_storage.get(code)
    if not data: return web.Response(text="Expired", status=410)
    
    if time.time() - data.get("timestamp", time.time()) > 86400:
        link_storage.pop(code, None)
        return web.Response(text="Expired", status=410)
        
    msg = data.get("msg")
    if not msg: return web.Response(text="File unavailable", status=410)
    
    filename = sanitize_filename(request.match_info.get("filename", "video.mp4")) or "video.mp4"
    file_size = msg.file.size if msg.file else 0
    range_header = request.headers.get("Range")
    
    start, end = 0, file_size - 1
    if range_header:
        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            if match.group(1): start = int(match.group(1))
            if match.group(2): end = int(match.group(2))
            if end >= file_size: end = file_size - 1
            
    if start >= file_size:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})
        
    content_length = end - start + 1
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"', "Accept-Ranges": "bytes",
        "Content-Type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "Content-Length": str(content_length)
    }
    if range_header: headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    
    response = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await response.prepare(request)
    
    try:
        offset = (start // 1048576) * 1048576
        skipped = start - offset
        remaining = content_length
        
        async for chunk in client.iter_download(msg.media, offset=offset, request_size=1048576):
            if skipped:
                chunk = chunk[skipped:]
                skipped = 0
            if not chunk: continue
            if len(chunk) > remaining: chunk = chunk[:remaining]
            await response.write(chunk)
            remaining -= len(chunk)
            if remaining <= 0: break
    except Exception: pass
    
    try: await response.write_eof()
    except Exception: pass
    return response

# ============================================================
# 15. TELEGRAM FAST UPLOAD
# ============================================================

async def fast_upload(tg_client, file_path, msg, filename):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024
    file_id = random.getrandbits(63)
    total_parts = math.ceil(file_size / part_size)
    uploaded_bytes = 0
    start_time = time.time()
    sem = asyncio.Semaphore(15)
    
    async def upload_part(index):
        nonlocal uploaded_bytes
        async with sem:
            with open(file_path, "rb") as file:
                file.seek(index * part_size)
                chunk = file.read(part_size)
            for attempt in range(5):
                try:
                    if file_size > 10 * 1024 * 1024:
                        await tg_client(SaveBigFilePartRequest(file_id, index, total_parts, chunk))
                    else:
                        await tg_client(SaveFilePartRequest(file_id, index, chunk))
                    uploaded_bytes += len(chunk)
                    return
                except Exception:
                    if attempt >= 4: raise
                    await asyncio.sleep(2)
                    
    async def updater():
        while uploaded_bytes < file_size:
            await asyncio.sleep(4)
            try:
                await msg.edit(get_status_text("Uploading to TG", filename, uploaded_bytes, file_size, start_time))
            except Exception: pass
            
    updater_task = asyncio.create_task(updater())
    try:
        await asyncio.gather(*[upload_part(i) for i in range(total_parts)])
    finally:
        updater_task.cancel()
        
    if file_size > 10 * 1024 * 1024:
        return InputFileBig(file_id, total_parts, filename)
    return InputFile(file_id, total_parts, filename, "")

# ============================================================
# 16. TELEGRAM MASTER HANDLER
# ============================================================

@client.on(events.NewMessage(incoming=True, func=lambda e: e.sender_id == ADMIN_ID))
async def master_handler(event):
    if event.file:
        await event.reply(f"📂 **File Detected:** `{event.file.name or 'file.bin'}`", buttons=[
            [Button.inline("🔗 Generate Direct Link", data=f"link_{event.id}")],
            [Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]
        ])
        return
        
    if event.text and (event.text.startswith("http") or event.text.startswith("magnet:?")):
        async with global_semaphore:
            raw = event.text.strip()
            url = raw
            custom_name, target_folder = None, None
            
            match_name = re.search(r"\s+-n\s+(.+?)(?=\s+-f\s+|$)", raw, re.IGNORECASE)
            if match_name: custom_name = match_name.group(1).strip()
            
            match_folder = re.search(r"\s+-f\s+(.+)$", raw, re.IGNORECASE)
            if match_folder: target_folder = match_folder.group(1).strip()
            
            url = re.split(r"\s+-n\s+|\s+-f\s+", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            msg = await event.reply("🔗 **Processing Request...**")
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            start_t = time.time()
            
            try:
                final_path = await download_any_url(url, workspace, custom_name, msg, start_t)
                if not final_path or not os.path.exists(final_path):
                    raise ValueError("Download failed.")
                    
                filename = os.path.basename(final_path)
                
                if filename.lower().endswith(".zip"):
                    await msg.edit("📦 **Extracting HLS ZIP Archive...**")
                    extract_dir = os.path.join(workspace, "extracted")
                    os.makedirs(extract_dir, exist_ok=True)
                    
                    def extract_zip():
                        with zipfile.ZipFile(final_path, "r") as z: z.extractall(extract_dir)
                    await asyncio.to_thread(extract_zip)
                    os.remove(final_path)
                    
                    project_name = os.path.splitext(filename)[0]
                    extracted_items = os.listdir(extract_dir)
                    
                    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
                        upload_source_dir = os.path.join(extract_dir, extracted_items[0])
                        s3_prefix = target_folder if target_folder else extracted_items[0]
                    else:
                        upload_source_dir = extract_dir
                        s3_prefix = target_folder if target_folder else project_name
                        
                    await msg.edit(f"⬆️ **Uploading HLS Pack to R2...**\n📂 `{s3_prefix}`")
                    await asyncio.to_thread(sync_r2_upload_folder, upload_source_dir, s3_prefix, asyncio.get_running_loop(), msg, time.time())
                    
                    master_url = f"{R2_PUBLIC_URL}/{quote(s3_prefix, safe='/')}/master.m3u8"
                    await msg.edit(f"✅ **HLS Uploaded to R2!**\n\n🎬 `{project_name}`\n📺 **Stream Link:**\n`{master_url}`", link_preview=False)
                else:
                    r2_url, code = await upload_to_r2(final_path, msg, target_folder)
                    await msg.edit(f"✅ **Leeched & Uploaded!**\n\n🎬 `{filename}`\n🔗 `{r2_url}`", buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]], link_preview=False)
                    
            except Exception as exc:
                await msg.edit(f"❌ **Error:** `{exc}`")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                free_memory()

# ============================================================
# 17. TELEGRAM CALLBACKS
# ============================================================

@client.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id != ADMIN_ID: return
    data = event.data.decode("utf-8", errors="ignore")
    
    if data.startswith("canceltask_"):
        code = data.split("_", 1)[1]
        item = active_tasks.get(code)
        if item:
            item["cancel_event"].set()
            process = item.get("process")
            if process:
                try: process.terminate()
                except Exception: pass
            await event.answer("Task cancelled.", alert=True)
            try: await event.edit("🛑 **Task Cancelled.**")
            except Exception: pass
        return
        
    if data.startswith("delr2_"):
        code = data.split("_", 1)[1]
        item = link_storage.get(code)
        if item and item.get("s3_key"):
            await event.answer("Deleting...", alert=False)
            try:
                await asyncio.to_thread(sync_delete_r2_file, item["s3_key"])
                link_storage.pop(code, None)
                await event.edit(f"🗑️ **File Deleted from R2!**\nKey: `{item['s3_key']}`")
            except Exception as exc:
                await event.edit(f"❌ Delete Error: {exc}")
        return
        
    if data.startswith("link_"):
        msg_id = int(data.split("_", 1)[1])
        await event.answer("Generating Direct Link...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file:
            await event.respond("❌ Error: File not found.")
            return
            
        code = secrets.token_urlsafe(8)
        link_storage[code] = {"msg": tg_msg, "timestamp": time.time()}
        base = get_public_base_url()
        if not base:
            await event.respond("❌ KOYEB_PUBLIC_URL or KOYEB_APP_NAME is not configured.")
            return
            
        filename = sanitize_filename(tg_msg.file.name or "video.mp4")
        link = f"{base}/{code}/{quote(filename)}"
        await event.respond(f"🚀 **Direct Link:**\n`{link}`\n\n💡 *Valid for 24 hours.*")
        return
        
    if data.startswith("r2_"):
        msg_id = int(data.split("_", 1)[1])
        await event.answer("Uploading...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file:
            await event.respond("❌ File not found.")
            return
            
        async with global_semaphore:
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            filename = sanitize_filename(tg_msg.file.name or "video.mp4")
            filename = os.path.basename(get_unique_filename(os.path.join(workspace, filename)))
            file_path = os.path.join(workspace, filename)
            status = await event.respond("⬇️ **Downloading from Telegram...**")
            start_t = time.time()
            
            try:
                with open(file_path, "wb") as file:
                    downloaded = 0
                    async for chunk in client.iter_download(tg_msg.media, request_size=1024 * 1024):
                        file.write(chunk)
                        downloaded += len(chunk)
                        if time.time() - start_t > 3 and downloaded % (10 * 1024 * 1024) < len(chunk):
                            try:
                                await status.edit(get_status_text("TG Download", filename, downloaded, tg_msg.file.size, start_t))
                            except Exception: pass
                            
                if filename.lower().endswith(".zip"):
                    await status.edit("📦 **Extracting HLS ZIP Archive...**")
                    extract_dir = os.path.join(workspace, "extracted")
                    os.makedirs(extract_dir, exist_ok=True)
                    
                    def extract_zip():
                        with zipfile.ZipFile(file_path, "r") as z: z.extractall(extract_dir)
                    await asyncio.to_thread(extract_zip)
                    os.remove(file_path)
                    
                    project = os.path.splitext(filename)[0]
                    items = os.listdir(extract_dir)
                    if len(items) == 1 and os.path.isdir(os.path.join(extract_dir, items[0])):
                        source_dir = os.path.join(extract_dir, items[0])
                        prefix = items[0]
                    else:
                        source_dir = extract_dir
                        prefix = project
                        
                    await status.edit(f"⬆️ **Uploading HLS...**\n📂 `{prefix}`")
                    await asyncio.to_thread(sync_r2_upload_folder, source_dir, prefix, asyncio.get_running_loop(), status, time.time())
                    
                    master_url = f"{R2_PUBLIC_URL}/{quote(prefix, safe='/')}/master.m3u8"
                    await status.edit(f"✅ **HLS Uploaded!**\n🎬 `{project}`\n📺 **Stream Link:**\n`{master_url}`", link_preview=False)
                else:
                    r2_url, code = await upload_to_r2(file_path, status)
                    await status.edit(f"✅ **Cloudflare R2 Complete!**\n🎬 `{filename}`\n🔗 `{r2_url}`", buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]], link_preview=False)
                    
            except Exception as exc:
                try: await status.edit(f"❌ **Error:** `{exc}`")
                except Exception: pass
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                free_memory()

# ============================================================
# 18. STARTUP
# ============================================================

async def main():
    print("====================================")
    print("Telegram R2 Leech Bot")
    print("Starting...")
    print(f"Port: {PORT}")
    print(f"Admin ID: {ADMIN_ID}")
    print("====================================")
    
    app = web.Application(client_max_size=1024 ** 3)
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP server listening on 0.0.0.0:{PORT}")
    
    try:
        print("Connecting to Telegram...")
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        print(f"Telegram connected: @{getattr(me, 'username', None)}")
        print("====================================")
        print("BOT ONLINE")
        print("====================================")
        await client.run_until_disconnected()
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print("FATAL STARTUP ERROR:", repr(exc), flush=True)
        raise

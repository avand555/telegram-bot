import os
import secrets
import asyncio
import mimetypes
import time
import re
import math
import random
import io
import base64
import subprocess
import datetime
import gc
import ctypes
import shutil
import uuid
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote

# Add HLS Specific MIME Types
mimetypes.add_type('application/vnd.apple.mpegurl', '.m3u8')
mimetypes.add_type('video/MP2T', '.ts')

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest, GetFileRequest
from telethon.tl.types import InputFileBig, InputFile

# Web & Storage
from aiohttp import web, ClientSession
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
import yt_dlp
import nest_asyncio

nest_asyncio.apply()

# ============================================
# --- 1. CONFIGURATION ---
# ============================================
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

ADMIN_ID = 716887656  , 2094838510

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "").strip()
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip('/')

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin").strip()
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "admin123").strip()

PUBLIC_TRACKERS = "udp://tracker.opentrackr.org:1337/announce,http://tracker.openbittorrent.com:80/announce,udp://opentracker.i2p.rocks:6969/announce"

global_semaphore = asyncio.Semaphore(4)
routes = web.RouteTableDef()
link_storage = {}
active_tasks = {}

client = TelegramClient('bot_session', int(API_ID), API_HASH, connection=ConnectionTcpFull)

# ============================================
# --- 2. CORE SYSTEM HELPERS ---
# ============================================
def free_memory():
    gc.collect()
    try: ctypes.CDLL('libc.so.6').malloc_trim(0)
    except: pass

def human_size(bytes_val):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024: return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024
    return "0 B"

def get_status_text(action, filename, current, total, start_time):
    diff = max(time.time() - start_time, 0.001)
    perc = (current / total) * 100 if total > 0 else 0
    speed = current / diff
    blocks = int(perc // 10)
    p_bar = "■" * blocks + "□" * (10 - blocks)
    return (f"🚀 **{action}**\n📦 `{filename}`\n\n"
            f"🌀 **Progress:** `[{p_bar}] {perc:.2f}%`\n"
            f"⚡ **Speed:** `{human_size(speed)}/s`\n"
            f"📂 **Size:** `{human_size(current)} / {human_size(total)}`")

def format_saas_progress(action, filename, percent, downloaded, total, speed, eta, cn, elapsed, task_code):
    done = int(percent // 10)
    p_bar = "●" * done + "◔" * (1 if percent % 10 >= 5 else 0)
    p_bar += "○" * (10 - len(p_bar.replace("●", "a").replace("◔", "b")))
    return (f"🧲 **{action}...**\n╭ `[{p_bar[:10]}]` » `{percent}%`\n"
            f"├ **Processed:** `{downloaded} of {total}`\n├ **Speed:** `{speed}`\n"
            f"├ **ETA:** `{eta}`\n├ **Peers:** `{cn}`\n├ **Elapsed:** `{elapsed}`\n"
            f"╰ **Cancel:** `/c_{task_code}`")

def get_readable_time(seconds: int) -> str:
    result = ""
    (days, remainder) = divmod(seconds, 86400)
    if days: result += f"{int(days)}d "
    (hours, remainder) = divmod(remainder, 3600)
    if hours: result += f"{int(hours)}h "
    (minutes, seconds) = divmod(remainder, 60)
    if minutes: result += f"{int(minutes)}m "
    result += f"{int(seconds)}s"
    return result

def get_largest_file(folder_path):
    largest, max_size = None, 0
    for r, _, files in os.walk(folder_path):
        for f in files:
            fp = os.path.join(r, f)
            sz = os.path.getsize(fp)
            if sz > max_size: max_size, largest = sz, fp
    return largest

def clean_double_extension(filename):
    while filename.lower().endswith(('.mp4.mp4', '.mkv.mkv', '.zip.zip')):
        filename = filename[:-4]
    return filename

def get_unique_filename(filepath):
    filepath = clean_double_extension(filepath)
    if not os.path.exists(filepath): return filepath
    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"): counter += 1
    return f"{base}_{counter}{ext}"

# ============================================
# --- 3. CLOUDFLARE R2 ENGINES ---
# ============================================
def get_r2_client():
    clean_id = R2_ACCOUNT_ID.replace("https://", "").replace("http://", "").split(".")[0].strip('/')
    endpoint = f"https://{clean_id}.r2.cloudflarestorage.com"
    r2_config = Config(region_name='auto', signature_version='s3v4', retries={'max_attempts': 3, 'mode': 'standard'})
    return boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, config=r2_config)

def sync_get_smart_dashboard_data():
    """Fetches all files, groups HLS chunks into single items, and calculates stats."""
    s3 = get_r2_client()
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=R2_BUCKET_NAME)
    
    all_objects = []
    for page in pages:
        if 'Contents' in page:
            all_objects.extend(page['Contents'])
            
    # Find all HLS bases
    hls_bases = set()
    for obj in all_objects:
        if obj['Key'].endswith('master.m3u8'):
            hls_bases.add(os.path.dirname(obj['Key']))
            
    hls_packages = {base: {'name': base, 'size': 0, 'date': None, 'type': 'HLS', 'url_key': f"{base}/master.m3u8"} for base in hls_bases}
    standalone_files = []
    
    total_size = 0
    mp4_count = 0
    
    # Sort bases by length to match subfolders correctly
    sorted_bases = sorted(list(hls_bases), key=len, reverse=True)
    
    for obj in all_objects:
        key = obj['Key']
        size = obj['Size']
        date = obj['LastModified']
        total_size += size
        
        is_hls_part = False
        for base in sorted_bases:
            if key.startswith(base + '/') or key == base:
                hls_packages[base]['size'] += size
                if hls_packages[base]['date'] is None or date > hls_packages[base]['date']:
                    hls_packages[base]['date'] = date
                is_hls_part = True
                break
                
        if not is_hls_part and not key.endswith('/'):
            standalone_files.append({'name': key, 'size': size, 'date': date, 'type': 'FILE', 'url_key': key})
            if key.lower().endswith('.mp4'):
                mp4_count += 1
                
    hls_count = len(hls_packages)
    all_items = list(hls_packages.values()) + standalone_files
    all_items.sort(key=lambda x: x['date'], reverse=True)
    
    return {
        'total_size': total_size,
        'mp4_count': mp4_count,
        'hls_count': hls_count,
        'items': all_items
    }

def sync_delete_r2_file(s3_key):
    get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)

def sync_delete_r2_folder(prefix):
    s3 = get_r2_client()
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix)
    for page in pages:
        if 'Contents' in page:
            objects_to_delete = [{'Key': obj['Key']} for obj in page['Contents']]
            s3.delete_objects(Bucket=R2_BUCKET_NAME, Delete={'Objects': objects_to_delete})

def sync_rename_r2_file(old_key, new_key):
    s3 = get_r2_client()
    s3.copy({'Bucket': R2_BUCKET_NAME, 'Key': old_key}, R2_BUCKET_NAME, new_key)
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=old_key)

def sync_r2_upload(file_path, s3_key, loop, msg, start_t):
    s3 = get_r2_client()
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or 'application/octet-stream'

    class ProgressCallback:
        def __init__(self): self.seen = 0; self.last = 0
        def __call__(self, bytes_amount):
            self.seen += bytes_amount
            if time.time() - self.last > 4:
                self.last = time.time()
                try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("R2 Uploading", filename, self.seen, file_size, start_t)), loop)
                except: pass
    
    extra_args = {'ContentType': mime_type}
    if not filename.endswith('.m3u8') and not filename.endswith('.ts'):
        extra_args['ContentDisposition'] = 'inline'

    t_config = TransferConfig(multipart_threshold=8*1024*1024, multipart_chunksize=8*1024*1024, max_concurrency=4)
    s3.upload_file(file_path, R2_BUCKET_NAME, s3_key, Callback=ProgressCallback(), ExtraArgs=extra_args, Config=t_config)

def sync_r2_upload_folder(folder_path, s3_prefix, loop, msg, start_t):
    s3 = get_r2_client()
    all_files = []
    total_size = 0
    for root_dir, _, files in os.walk(folder_path):
        for f in files:
            fp = os.path.join(root_dir, f)
            all_files.append(fp)
            total_size += os.path.getsize(fp)

    class ProgressCallback:
        def __init__(self): self.seen = 0; self.last = 0; self.lock = threading.Lock()
        def __call__(self, bytes_amount):
            with self.lock:
                self.seen += bytes_amount
                if time.time() - self.last > 4:
                    self.last = time.time()
                    try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("R2 HLS Sync", s3_prefix, self.seen, total_size, start_t)), loop)
                    except: pass
    prog_cb = ProgressCallback()

    def upload_single_file(file_path):
        rel_path = os.path.relpath(file_path, folder_path)
        s3_key = f"{s3_prefix.strip('/')}/{rel_path.replace(os.sep, '/')}"
        ext = os.path.splitext(file_path)[1].lower()
        content_type, _ = mimetypes.guess_type(file_path)
        extra_args = {'ContentType': content_type or 'application/octet-stream'}
        if ext not in ['.m3u8', '.ts']: extra_args['ContentDisposition'] = 'inline'
        s3.upload_file(file_path, R2_BUCKET_NAME, s3_key, Callback=prog_cb, ExtraArgs=extra_args)

    with ThreadPoolExecutor(max_workers=15) as executor:
        executor.map(upload_single_file, all_files)

async def upload_to_r2(file_path, msg, target_folder=None):
    start_t = time.time()
    loop = asyncio.get_running_loop()
    filename = os.path.basename(file_path)
    if target_folder: s3_key = f"{target_folder.strip('/')}/{filename}"
    else: s3_key = f"{datetime.datetime.now().year}/{datetime.datetime.now().month}/{datetime.datetime.now().day}/{filename}"
    
    await msg.edit(f"⬆️ **Connecting to Cloudflare R2...**\n🎬 `{filename}`")
    await asyncio.to_thread(sync_r2_upload, file_path, s3_key, loop, msg, start_t)
    
    code = secrets.token_urlsafe(8)
    link_storage[code] = {'s3_key': s3_key}
    return f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}", code

# ============================================
# --- 4. DOWNLOAD ENGINES (MAGNET/YT-DLP/GDRIVE) ---
# ============================================

def get_aria2_executable():
    if shutil.which('aria2c'):
        return 'aria2c'
    
    local = os.path.abspath('./aria2c')
    if os.path.exists(local):
        return local
    
    # Only attempt to download the Linux static binary on Linux x86_64 systems
    if sys.platform == 'linux' and platform.machine() == 'x86_64':
        try:
            subprocess.run(
                "wget -qO- https://github.com/P3TERX/aria2-builder/releases/download/1.36.0/aria2-1.36.0-static-linux-amd64.tar.gz | tar -xz",
                shell=True, check=True, capture_output=True
            )
            os.chmod('./aria2c', 0o755)
            return local
        except subprocess.CalledProcessError:
            pass  # Fall back to default 'aria2c', which will raise a clear FileNotFoundError if missing
            
    return 'aria2c'


async def download_magnet(url, workspace, custom_name, msg, start_t):
    cmd = [
        get_aria2_executable(), "--seed-time=0", "--max-connection-per-server=16", "--split=16",
        "--summary-interval=3", "--bt-stop-timeout=120", f"--bt-tracker={PUBLIC_TRACKERS}", f"--dir={workspace}", url
    ]
    # Capture stderr as well for better debugging if aria2c fails
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    task_code = secrets.token_urlsafe(8)
    active_tasks[task_code] = {'process': process, 'cancel_event': asyncio.Event(), 'dir': workspace}
    
    aria_re = re.compile(r'\[#(?P<gid>\w+)\s+(?P<downloaded>[^\s/]+)/(?P<total>[^\s\(\)]+)(?:\((?P<percent>\d+)%\))?\s+CN:(?P<cn>\d+)\s+SPD:(?P<speed>[^\s\]]+)(?:\s+ETA:(?P<eta>[^\s\]]+))?\]')
    
    last_update = 0
    while process.returncode is None:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break
        
        line = line_bytes.decode('utf-8', errors='ignore').strip()
        match = aria_re.search(line)
        
        if match and time.time() - last_update > 4:
            elapsed = get_readable_time(int(time.time() - start_t))
            active_file = "Fetching Metadata..."
            
            for _, _, files in os.walk(workspace):
                for f in files:
                    if not f.endswith('.aria2'):
                        active_file = f
                        break
            
            p_text = format_saas_progress(
                "Download", active_file, int(match.group('percent') or 0),
                match.group('downloaded'), match.group('total'), match.group('speed')+"/s",
                match.group('eta') or "Calc...", match.group('cn'), elapsed, task_code
            )
            try:
                await msg.edit(p_text, buttons=[[Button.inline("❌ Cancel", data=f"canceltask_{task_code}")]])
                last_update = time.time()
            except Exception:
                pass  # Ignore Telegram message edit flood limits or deletion errors
                
    await process.wait()
    active_tasks.pop(task_code, None)
    
    largest = get_largest_file(workspace)
    if not largest:
        raise ValueError("Torrent failed or no files were downloaded.")
        
    final_name = get_unique_filename(os.path.join(workspace, custom_name if custom_name else os.path.basename(largest)))
    shutil.move(largest, final_name)
    return final_name


def sync_yt_dlp_download(url, workspace, custom_name=None):
    # Note: If custom_name is a fixed filename (e.g., "video.mp4"), yt-dlp may append 
    # format IDs (e.g., "video.mp4.f137") during separate A/V downloads before merging.
    # ydl.prepare_filename(info) at the end correctly resolves the final merged filename.
    out_tmpl = os.path.join(workspace, custom_name if custom_name else '%(title)s.%(ext)s')
    ydl_opts = {
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return clean_double_extension(ydl.prepare_filename(info))


def extract_gdrive_id(url):
    match = re.search(r'(?:file/d/|id=|/d/)([a-zA-Z0-9_-]{25,})', url)
    return match.group(1) if match else None


async def get_gdrive_stream(session, file_id):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    base_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    
    resp = await session.get(base_url, headers=headers, allow_redirects=True)
    
    if "text/html" in resp.headers.get("Content-Type", ""):
        text = await resp.text()
        resp.close()
        
        # Look for href or action containing 'confirm='
        match = re.search(r'(?:href|action)="([^"]+confirm=[^"]+)"', text)
        if match:
            # Use html.unescape for robust cleaning of &amp;, &#39;, etc.
            next_url = html.unescape(match.group(1))
            if next_url.startswith('/'):
                next_url = "https://drive.google.com" + next_url
            return await session.get(next_url, headers=headers, allow_redirects=True)
            
        # Fallback: check cookies for download_warning token
        cookies = session.cookie_jar.filter_cookies(resp.url)
        confirm_token = None
        for k, v in cookies.items():
            if k.startswith('download_warning'):
                confirm_token = v.value
                break
        
        if confirm_token:
            return await session.get(f"{base_url}&confirm={confirm_token}", headers=headers, allow_redirects=True)
            
        # If all bypasses fail (e.g., file is Private or Quota Exceeded), 
        # return a fresh request to let download_direct throw the proper error.
        return await session.get(base_url, headers=headers, allow_redirects=True)
        
    return resp


async def download_direct(url, workspace, msg, start_t, custom_name=None, gdrive_id=None):
    # Prevent indefinite hanging on slow/dead servers
    timeout = aiohttp.ClientTimeout(total=3600)  # 1 hour max
    
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        if gdrive_id:
            await msg.edit("🅿️ **Google Drive Fallback Detected...**")
            r = await get_gdrive_stream(sess, gdrive_id)
        else:
            r = await sess.get(url, allow_redirects=True)

        if "text/html" in r.headers.get("Content-Type", ""): 
            r.close()
            raise ValueError("Google Drive blocked the download (Virus Scan warning or Private File). Try sharing it publicly.")
            
        f_size = int(r.headers.get("Content-Length", 0))
        filename = custom_name
        
        if not filename and "Content-Disposition" in r.headers:
            matches = re.findall('filename="?([^"]+)"?', r.headers["Content-Disposition"])
            if matches:
                filename = matches[0]
        
        filename = filename or unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
        if filename.lower() == "view" or "." not in filename: 
            filename += ".mp4"
        
        # Sanitize filename
        safe_filename = re.sub(r'[\\/*?:"<>|]', "", filename)
        file_path = os.path.join(workspace, clean_double_extension(safe_filename))
        
        await msg.edit(f"⬇️ **Leeching Direct Link...**\n🎬 `{os.path.basename(file_path)}`")
        
        with open(file_path, 'wb') as f:
            async for chunk in r.content.iter_chunked(1024 * 1024):  # 1MB chunks
                f.write(chunk)
                if f.tell() % (10 * 1024 * 1024) == 0:  # Update every 10MB
                    try:
                        await msg.edit(get_status_text("Leeching", os.path.basename(file_path), f.tell(), f_size, start_t))
                    except Exception:
                        pass  # Ignore Telegram edit limits
        r.close()
        
    return file_path


async def download_any_url(url, workspace, custom_name, msg, start_t):
    if url.startswith("magnet:?"):
        return await download_magnet(url, workspace, custom_name, msg, start_t)

    is_zip = (custom_name and custom_name.lower().endswith('.zip')) or url.lower().endswith('.zip')
    gdrive_id = extract_gdrive_id(url)

    # Let yt-dlp handle Google Drive links (it bypasses warnings perfectly)
    if not is_zip:
        try:
            if gdrive_id:
                await msg.edit("🅿️ **Google Drive Link Detected! Extracting via yt-dlp...**")
            else:
                await msg.edit("🅿️ **Extracting File Info via yt-dlp...**")
                
            filename = await asyncio.to_thread(sync_yt_dlp_download, url, workspace, custom_name)
            if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
                return filename
        except Exception:
            pass  # If yt-dlp fails, it will fall back to direct download below

    return await download_direct(url, workspace, msg, start_t, custom_name, gdrive_id)
# ============================================
# --- 5. SECURED SMART WEB DASHBOARD ---
# ============================================
def check_dashboard_auth(request):
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Basic '): return False
    try:
        user, password = base64.b64decode(auth_header.split(' ', 1)[1]).decode('utf-8').split(':', 1)
        return user == DASHBOARD_USER and password == DASHBOARD_PASS
    except: return False

DASHBOARD_CSS = """
    :root { --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --border: #334155; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
    .container { max-width: 1200px; margin: auto; }
    .header-bar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; margin-bottom: 20px; gap: 15px; }
    h2 { margin: 0; color: var(--text); font-size: 24px; display: flex; align-items: center; gap: 10px; border-bottom: 2px solid var(--border); padding-bottom: 10px; width: 100%; }
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
    .stat-card { background: var(--card); padding: 15px; border-radius: 10px; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    .stat-title { font-size: 12px; color: var(--muted); text-transform: uppercase; font-weight: bold; margin-bottom: 5px; }
    .stat-val { font-size: 20px; font-weight: bold; color: var(--accent); }
    .controls { display: flex; gap: 10px; margin-bottom: 15px; width: 100%; }
    .search-box { flex-grow: 1; padding: 14px 20px; border-radius: 8px; border: 1px solid var(--border); background: var(--card); color: var(--text); font-size: 15px; outline: none; transition: 0.2s; }
    .search-box:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2); }
    .table-wrapper { overflow-x: auto; background: var(--card); border-radius: 10px; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    table { width: 100%; border-collapse: collapse; min-width: 900px; }
    th { background: #0f172a; color: var(--muted); padding: 16px; text-align: left; font-size: 13px; font-weight: 600; cursor: pointer; user-select: none; }
    th:hover { color: var(--text); }
    td { padding: 16px; border-bottom: 1px solid var(--border); font-size: 14px; word-break: break-all; color: #cbd5e1; }
    tr:last-child td { border-bottom: none; }
    tr:hover { background: #334155; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 5px; padding: 8px 14px; border-radius: 6px; border: none; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; transition: 0.2s; }
    .btn-copy { background: rgba(59, 130, 246, 0.1); color: var(--accent); }
    .btn-view { background: rgba(16, 185, 129, 0.1); color: #34d399; }
    .btn-delete { background: rgba(244, 63, 94, 0.1); color: #fb7185; }
"""

DASHBOARD_JS = """
    function copyText(t) { navigator.clipboard.writeText(t); alert('✅ URL Copied!'); }
    function deleteItem(key, isHLS) {
        let dec = decodeURIComponent(key);
        let msg = isHLS ? '⚠️ DELETE ENTIRE HLS FOLDER:\\n' + dec : '⚠️ DELETE FILE:\\n' + dec;
        if (confirm(msg)) {
            window.location.href = (isHLS ? '/delete_folder?prefix=' : '/delete_file?key=') + encodeURIComponent(dec);
        }
    }
    function filterTable() {
        let input = document.getElementById("searchInput").value.toLowerCase();
        let rows = document.querySelectorAll("tbody tr");
        rows.forEach(row => {
            let filename = row.querySelector(".file-name")?.innerText.toLowerCase() || "";
            row.style.display = filename.includes(input) ? "" : "none";
        });
    }
"""

@routes.get('/dashboard')
async def dashboard_handler(request):
    if not check_dashboard_auth(request):
        return web.Response(status=401, headers={'WWW-Authenticate': 'Basic realm="Dashboard"'}, text="🔒 Access Denied")

    try:
        data = await asyncio.to_thread(sync_get_smart_dashboard_data)
        file_rows = ""
        
        for item in data['items']:
            name = item['name']
            size_str = human_size(item['size'])
            date_str = item['date'].strftime("%Y-%m-%d %H:%M") if item['date'] else "-"
            url = f"{R2_PUBLIC_URL}/{quote(item['url_key'], safe='/')}"
            
            if item['type'] == 'HLS':
                file_rows += f"""<tr style="background: rgba(251, 191, 36, 0.05);">
                    <td>📦 <span class="file-name"><b>{name}</b> (HLS Package)</span></td>
                    <td>{size_str}</td><td>{date_str}</td>
                    <td><div class="actions">
                        <button class="btn btn-copy" onclick="copyText('{url}')">🔗 Copy Master.m3u8</button>
                        <a href="{url}" target="_blank" class="btn btn-view">▶️ Play Stream</a>
                        <button class="btn btn-delete" onclick="deleteItem('{quote(name)}', true)">🗑️ Delete Package</button>
                    </div></td></tr>"""
            else:
                file_rows += f"""<tr>
                    <td>🎬 <span class="file-name">{name}</span></td>
                    <td>{size_str}</td><td>{date_str}</td>
                    <td><div class="actions">
                        <button class="btn btn-copy" onclick="copyText('{url}')">🔗 Copy MP4</button>
                        <a href="{url}" target="_blank" class="btn btn-view">▶️ Play MP4</a>
                        <button class="btn btn-delete" onclick="deleteItem('{quote(name)}', false)">🗑️ Delete File</button>
                    </div></td></tr>"""
                    
        if not file_rows: file_rows = "<tr><td colspan='4' style='text-align:center;'>No files found.</td></tr>"
    except Exception as e: file_rows = f"<tr><td colspan='4' style='color:red;'>Error: {e}</td></tr>"

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Cloudflare R2 Manager</title><style>{DASHBOARD_CSS}</style><script>{DASHBOARD_JS}</script></head><body>
        <div class="container"><div class="header-bar"><h2>🛡️ Cloudflare R2 Smart Dashboard</h2></div>
            <div class="stats-grid">
                <div class="stat-card"><div class="stat-title">Total Storage</div><div class="stat-val">{human_size(data.get('total_size', 0))}</div></div>
                <div class="stat-card"><div class="stat-title">HLS Packages</div><div class="stat-val">{data.get('hls_count', 0)}</div></div>
                <div class="stat-card"><div class="stat-title">MP4 Files</div><div class="stat-val">{data.get('mp4_count', 0)}</div></div>
            </div>
            <div class="controls"><input type="text" id="searchInput" class="search-box" onkeyup="filterTable()" placeholder="🔍 Global Search (Find any MP4 or HLS Package)..."></div>
            <div class="table-wrapper"><table><thead><tr><th>Name</th><th>Total Size</th><th>Date Uploaded</th><th>Actions</th></tr></thead><tbody>{file_rows}</tbody></table></div>
        </div></body></html>"""
    return web.Response(text=html, content_type='text/html')

@routes.get('/delete_file')
async def web_delete_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    if key := request.query.get('key'):
        try: await asyncio.to_thread(sync_delete_r2_file, key); force_system_ram_purge()
        except: pass
    raise web.HTTPFound('/dashboard')

@routes.get('/delete_folder')
async def web_delete_folder_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    if pref := request.query.get('prefix'):
        try: await asyncio.to_thread(sync_delete_r2_folder, pref); force_system_ram_purge()
        except: pass
    raise web.HTTPFound('/dashboard')

@routes.get('/')
async def root(request):
    return web.Response(text="<html><body style='background:#0f172a;color:#38bdf8;text-align:center;padding-top:150px;font-family:sans-serif;'><h1 style='font-size:40px;'>✅ System Online</h1><a href='/dashboard' style='display:inline-block;margin-top:20px;padding:15px 30px;background:#38bdf8;color:#0f172a;text-decoration:none;border-radius:8px;font-weight:bold;font-size:18px;'>Go to Dashboard</a></body></html>", content_type='text/html')

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code, data = request.match_info['code'], link_storage.get(request.match_info['code'])
    if not data: return web.Response(text="Expired", status=410)
    msg, file_name = data['msg'], unquote(request.match_info['filename'])
    start = int(re.search(r'bytes=(\d+)-', request.headers.get('Range')).group(1)) if request.headers.get('Range') else 0
    resp = web.StreamResponse(status=206 if start else 200, headers={'Content-Disposition': f'attachment; filename="{file_name}"', 'Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4', 'Content-Length': str(msg.file.size - start)})
    await resp.prepare(request)
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576): await resp.write(chunk)
    except: pass
    return resp

# ============================================
# --- 6. TELEGRAM HANDLERS ---
# ============================================
@client.on(events.NewMessage(incoming=True, func=lambda e: e.sender_id == ADMIN_ID))
async def master_handler(event):
    
    if event.file:
        await event.reply(
            f"📂 **File Detected:** `{event.file.name or 'file.bin'}`",
            buttons=[
                [Button.inline("🔗 Generate Direct Link", data=f"link_{event.id}")],
                [Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]
            ]
        )
        return

    if event.text and (event.text.startswith("http") or event.text.startswith("magnet:?")):
        async with global_semaphore:
            input_text = event.text.strip()
            
            # Split the text to extract the base links (removing -n and -f args)
            clean_text = input_text.split(" -n ")[0].split(" -f ")[0].strip()
            
            # Extract multiple links separated by commas, spaces, or newlines
            links = [l.strip().strip(',') for l in re.split(r'[\n, ]+', clean_text) if l.strip()]

            custom_name, target_folder = None, None
            if " -n " in input_text: custom_name = input_text.split(" -n ")[1].split(" -f ")[0].strip()
            if " -f " in input_text: target_folder = input_text.split(" -f ")[1].strip()

            # Process EACH link found in the message
            for url in links:
                msg = await event.reply(f"🔗 **Processing Link:**\n`{url[:70]}...`")
                workspace = f"dl_{uuid.uuid4().hex[:8]}"
                os.makedirs(workspace, exist_ok=True)
                start_t = time.time()
                
                try:
                    final_path = await download_any_url(url, workspace, custom_name, msg, start_t)
                    if not final_path or not os.path.exists(final_path): 
                        raise ValueError("Download failed.")
                    
                    filename = os.path.basename(final_path)

                    if filename.lower().endswith('.zip'):
                        await msg.edit("📦 **Extracting HLS ZIP Archive...**")
                        extract_dir = os.path.join(workspace, "extracted")
                        os.makedirs(extract_dir, exist_ok=True)
                        await asyncio.to_thread(lambda: zipfile.ZipFile(final_path, 'r').extractall(extract_dir))

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

                except Exception as e: 
                    await msg.edit(f"❌ **Error with link:**\n`{url[:70]}...`\n\n**Reason:** {e}")
                finally:
                    shutil.rmtree(workspace, ignore_errors=True)
                    free_memory()
async def on_callback(event):
    if event.sender_id != ADMIN_ID: return
    data = event.data.decode()

    if data.startswith("canceltask_"):
        code = data.split("_")[1]
        item = active_tasks.get(code)
        if item:
            item['cancel_event'].set()
            if item['process']:
                try: item['process'].terminate()
                except: pass
            await event.answer("Task cancelled.", alert=True)
            try: await event.edit("🛑 **Task Cancelled.**")
            except: pass
        return

    if data.startswith("delr2_"):
        code = data.split("_")[1]
        item = link_storage.get(code)
        if item and 's3_key' in item:
            await event.answer("Deleting...", alert=False)
            try:
                await asyncio.to_thread(sync_delete_r2_file, item['s3_key'])
                await event.edit(f"🗑️ **File Deleted from R2!**\nKey: `{item['s3_key']}`")
            except Exception as e: await event.edit(f"❌ Delete Error: {e}")
        return

    if data.startswith("link_"):
        msg_id = int(data.split("_")[1]); await event.answer("Generating Direct Link...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file: return await event.respond("❌ Error: File not found.")
        
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': tg_msg, 'timestamp': time.time()}
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/') or f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        filename = clean_double_extension(re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4"))
        
        await event.respond(f"🚀 **Direct Link:**\n`{base}/{code}/{quote(filename)}`\n\n💡 *Valid for 24 hours.*")

    if data.startswith("r2_"):
        msg_id = int(data.split("_")[1]); await event.answer("Uploading...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        async with global_semaphore:
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            filename = clean_double_extension(re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4"))
            file_path = os.path.join(workspace, filename)
            
            status = await event.respond(f"⬇️ Downloading from Telegram...")
            start_t = time.time()
            try:
                with open(file_path, 'wb') as f:
                    async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                        f.write(chunk)
                        if f.tell() % (10 * 1024 * 1024) == 0: await status.edit(get_status_text("TG Down", filename, f.tell(), tg_msg.file.size, start_t))
                
                if filename.lower().endswith('.zip'):
                    await status.edit("📦 **Extracting HLS ZIP Archive...**")
                    extract_dir = os.path.join(workspace, "extracted"); os.makedirs(extract_dir, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(file_path, 'r').extractall(extract_dir))
                    proj = os.path.splitext(filename)[0]
                    items = os.listdir(extract_dir)
                    s_dir = os.path.join(extract_dir, items[0]) if len(items)==1 and os.path.isdir(os.path.join(extract_dir, items[0])) else extract_dir
                    s_pref = items[0] if len(items)==1 and os.path.isdir(os.path.join(extract_dir, items[0])) else proj
                    
                    await status.edit(f"⬆️ **Uploading HLS...**\n📂 `{s_pref}`")
                    await asyncio.to_thread(sync_r2_upload_folder, s_dir, s_pref, asyncio.get_running_loop(), status, time.time())
                    m_url = f"{R2_PUBLIC_URL}/{quote(s_pref, safe='/')}/master.m3u8"
                    await status.edit(f"✅ **HLS Uploaded!**\n🎬 `{proj}`\n📺 **Stream Link:**\n`{m_url}`", link_preview=False)
                else:
                    r2_url, code = await upload_to_r2(file_path, status)
                    await status.edit(f"✅ **R2 Complete!**\n🎬 `{filename}`\n🔗 `{r2_url}`", buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]])
            except Exception as e: await status.edit(f"❌ Error: {e}")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                free_memory()

# --- 7. STARTUP ---
async def main():
    app = web.Application(); app.add_routes(routes); runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__': asyncio.run(main())

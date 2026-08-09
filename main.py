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

ADMIN_ID = 716887656  

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

client = TelegramClient('bot_session', int(API_ID), API_HASH, connection=ConnectionTcpFull, use_ipv6=False)

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
    p_bar = "●" * done
    if done < 10:
        p_bar += "◔" if (percent % 10) >= 5 else "○"
        p_bar += "○" * (9 - done)
    return (
        f"🧲 **{action}...**\n"
        f"╭ `[{p_bar[:10]}]` » `{percent}%`\n"
        f"├ **Processed:** `{downloaded} of {total}`\n"
        f"├ **Speed:** `{speed}`\n"
        f"├ **ETA:** `{eta}`\n"
        f"├ **Peers:** `{cn}`\n"
        f"├ **Elapsed:** `{elapsed}`\n"
        f"├ **Engine:** `Aria2 v1.36.0`\n"
        f"╰ **Cancel:** `/c_{task_code}`"
    )

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

def sync_get_r2_files():
    return get_r2_client().list_objects_v2(Bucket=R2_BUCKET_NAME)

def sync_delete_r2_file(s3_key):
    get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)

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
# --- 4. DOWNLOAD ENGINES (ISOLATED) ---
# ============================================
def get_aria2_executable():
    if shutil.which('aria2c'): return 'aria2c'
    local = os.path.abspath('./aria2c')
    if os.path.exists(local): return local
    try:
        subprocess.run("wget -qO- https://github.com/P3TERX/aria2-builder/releases/download/1.36.0/aria2-1.36.0-static-linux-amd64.tar.gz | tar -xz", shell=True, check=True)
        os.chmod('./aria2c', 0o755)
        return local
    except: return 'aria2c'

async def download_magnet(url, workspace, custom_name, msg, start_t):
    cmd = [get_aria2_executable(), "--seed-time=0", "--max-connection-per-server=16", "--split=16",
           "--summary-interval=3", "--bt-stop-timeout=120", f"--bt-tracker={PUBLIC_TRACKERS}", f"--dir={workspace}", url]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
    task_code = secrets.token_urlsafe(8)
    active_tasks[task_code] = {'process': process, 'cancel_event': asyncio.Event(), 'dir': workspace}
    aria_re = re.compile(r'\[#(?P<gid>\w+)\s+(?P<downloaded>[^\s/]+)/(?P<total>[^\s\(\)]+)(?:\((?P<percent>\d+)%\))?\s+CN:(?P<cn>\d+)\s+SPD:(?P<speed>[^\s\]]+)(?:\s+ETA:(?P<eta>[^\s\]]+))?\]')
    
    last_update = 0
    while process.returncode is None:
        line_bytes = await process.stdout.readline()
        if not line_bytes: break
        line = line_bytes.decode('utf-8', errors='ignore').strip()
        match = aria_re.search(line)
        if match and time.time() - last_update > 4:
            elapsed = get_readable_time(int(time.time() - start_t))
            active_file = "Fetching Metadata..."
            for _, _, files in os.walk(workspace):
                for f in files:
                    if not f.endswith('.aria2'): active_file = f; break
            p_text = format_saas_progress("Download", active_file, int(match.group('percent') or 0), match.group('downloaded'), match.group('total'), match.group('speed')+"/s", match.group('eta') or "Calc...", match.group('cn'), elapsed, task_code)
            try: await msg.edit(p_text, buttons=[[Button.inline("❌ Cancel", data=f"canceltask_{task_code}")]]); last_update = time.time()
            except: pass

    await process.wait()
    active_tasks.pop(task_code, None)
    
    largest = get_largest_file(workspace)
    if not largest: raise ValueError("Torrent failed or returned no files.")
    final_name = get_unique_filename(os.path.join(workspace, custom_name if custom_name else os.path.basename(largest)))
    shutil.move(largest, final_name)
    return final_name

def sync_yt_dlp_download(url, workspace, custom_name=None):
    out_tmpl = os.path.join(workspace, custom_name if custom_name else '%(title)s.%(ext)s')
    ydl_opts = {'outtmpl': out_tmpl, 'quiet': True, 'no_warnings': True, 'nocheckcertificate': True, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return clean_double_extension(ydl.prepare_filename(info))

async def download_direct(url, workspace, msg, start_t, custom_name=None):
    async with ClientSession() as sess:
        async with sess.get(url, allow_redirects=True) as r:
            if "text/html" in r.headers.get("Content-Type", ""): raise ValueError("HTML webpage detected.")
            f_size = int(r.headers.get("Content-Length", 0))
            filename = custom_name or unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
            if not "." in filename: filename += ".mp4"
            file_path = os.path.join(workspace, clean_double_extension(re.sub(r'[\\/*?:"<>|]', "", filename)))
            with open(file_path, 'wb') as f:
                async for chunk in r.content.iter_chunked(1024*1024):
                    f.write(chunk)
                    if f.tell() % (10 * 1024 * 1024) == 0:
                        try: await msg.edit(get_status_text("Leeching", filename, f.tell(), f_size, start_t))
                        except: pass
    return file_path

async def download_any_url(url, workspace, custom_name, msg, start_t):
    if url.startswith("magnet:?"):
        return await download_magnet(url, workspace, custom_name, msg, start_t)

    is_zip = (custom_name and custom_name.lower().endswith('.zip')) or url.lower().endswith('.zip')

    if not is_zip:
        try:
            await msg.edit("🅿️ **Extracting File Info via yt-dlp...**")
            filename = await asyncio.to_thread(sync_yt_dlp_download, url, workspace, custom_name)
            if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
                return filename
        except Exception: pass

    return await download_direct(url, workspace, msg, start_t, custom_name)

# ============================================
# --- 5. SECURED WEB DASHBOARD (UI/CSS/JS) ---
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
    h2 { margin: 0; color: var(--text); font-size: 26px; display: flex; align-items: center; gap: 10px; border-bottom: 2px solid var(--border); padding-bottom: 10px; width: 100%; }
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
    .stat-card { background: var(--card); padding: 15px; border-radius: 10px; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    .stat-title { font-size: 12px; color: var(--muted); text-transform: uppercase; font-weight: bold; margin-bottom: 5px; }
    .stat-val { font-size: 20px; font-weight: bold; color: var(--accent); }
    .controls { display: flex; gap: 10px; margin-bottom: 15px; width: 100%; }
    .search-box { flex-grow: 1; padding: 14px 20px; border-radius: 8px; border: 1px solid var(--border); background: var(--card); color: var(--text); font-size: 15px; outline: none; transition: 0.2s; }
    .search-box:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2); }
    .table-wrapper { overflow-x: auto; background: var(--card); border-radius: 10px; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    table { width: 100%; border-collapse: collapse; min-width: 900px; }
    th { background: #0f172a; color: var(--muted); padding: 16px; text-align: left; font-size: 13px; font-weight: 600; cursor: pointer; user-select: none; }
    th:hover { color: var(--text); }
    td { padding: 16px; border-bottom: 1px solid var(--border); font-size: 14px; word-break: break-all; color: #cbd5e1; }
    tr:last-child td { border-bottom: none; }
    tr:hover { background: #334155; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .btn { display: inline-flex; align-items: center; justify-content: center; gap: 5px; padding: 8px 14px; border-radius: 6px; border: none; font-size: 12px; font-weight: 600; cursor: pointer; text-decoration: none; transition: 0.2s; }
    .btn-copy { background: rgba(56, 189, 248, 0.1); color: var(--accent); }
    .btn-copy:hover { background: rgba(56, 189, 248, 0.2); }
    .btn-view { background: rgba(16, 185, 129, 0.1); color: #34d399; }
    .btn-view:hover { background: rgba(16, 185, 129, 0.2); }
    .btn-move { background: rgba(167, 139, 250, 0.1); color: #c084fc; }
    .btn-move:hover { background: rgba(167, 139, 250, 0.2); }
    .btn-rename { background: rgba(251, 191, 36, 0.1); color: #fbbf24; }
    .btn-rename:hover { background: rgba(251, 191, 36, 0.2); }
    .btn-delete { background: rgba(244, 63, 94, 0.1); color: #fb7185; }
    .btn-delete:hover { background: rgba(244, 63, 94, 0.2); }
"""

DASHBOARD_JS = """
    function copyText(t) { navigator.clipboard.writeText(t); alert('✅ URL Copied!'); }
    function deleteFile(key) {
        let decodedKey = decodeURIComponent(key);
        if (confirm('⚠️ PERMANENTLY DELETE: \\n' + decodedKey)) {
            window.location.href = '/delete_file?key=' + encodeURIComponent(decodedKey);
        }
    }
    function renameFile(key) {
        let decodedKey = decodeURIComponent(key);
        let newKey = prompt('✏️ Rename File (Full Path):', decodedKey);
        if (newKey && newKey !== decodedKey) {
            window.location.href = '/rename_file?old_key=' + encodeURIComponent(decodedKey) + '&new_key=' + encodeURIComponent(newKey);
        }
    }
    function moveFolder(key) {
        let decodedKey = decodeURIComponent(key);
        let currentDir = decodedKey.includes('/') ? decodedKey.substring(0, decodedKey.lastIndexOf('/')) : '';
        let targetFolder = prompt('📁 Enter target folder path (e.g. Movies/2026):', currentDir);
        if (targetFolder !== null) {
            window.location.href = '/move_file?old_key=' + encodeURIComponent(decodedKey) + '&target_folder=' + encodeURIComponent(targetFolder);
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
    let currentSort = { col: -1, dir: 'asc' };
    function sortTable(colIndex, type) {
        let table = document.querySelector("tbody");
        let rows = Array.from(table.querySelectorAll("tr"));
        if (rows.length === 0 || rows[0].querySelector("td[colspan]")) return;
        let dir = (currentSort.col === colIndex && currentSort.dir === 'asc') ? 'desc' : 'asc';
        currentSort = { col: colIndex, dir: dir };
        rows.sort((a, b) => {
            let valA = a.children[colIndex].getAttribute("data-val");
            let valB = b.children[colIndex].getAttribute("data-val");
            if (type === 'num') { return dir === 'asc' ? parseFloat(valA) - parseFloat(valB) : parseFloat(valB) - parseFloat(valA); }
            else { return dir === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA); }
        });
        table.innerHTML = "";
        rows.forEach(row => table.appendChild(row));
        document.querySelectorAll("th span").forEach(span => span.innerText = "");
        document.getElementById("th-" + colIndex).querySelector("span").innerText = dir === 'asc' ? ' 🔼' : ' 🔽';
    }
"""

@routes.get('/dashboard')
async def dashboard_handler(request):
    if not check_dashboard_auth(request):
        return web.Response(status=401, headers={'WWW-Authenticate': 'Basic realm="Cloudflare R2 Dashboard"'}, text="🔒 Access Denied")

    file_rows = ""
    total_size_bytes = 0
    total_files = 0
    
    try:
        response = await asyncio.to_thread(sync_get_r2_files)
        if 'Contents' in response:
            total_files = len(response['Contents'])
            for obj in sorted(response['Contents'], key=lambda x: x['LastModified'], reverse=True):
                name = obj['Key']
                size_bytes = obj['Size']
                total_size_bytes += size_bytes
                
                size_str = human_size(size_bytes)
                timestamp = obj['LastModified'].timestamp()
                date_str = obj['LastModified'].strftime("%Y-%m-%d %H:%M")
                url = f"{R2_PUBLIC_URL}/{quote(name, safe='/')}"
                
                file_rows += f"""
                <tr>
                    <td data-val="{name}"><span class="file-name">{name}</span></td>
                    <td data-val="{size_bytes}">{size_str}</td>
                    <td data-val="{timestamp}">{date_str}</td>
                    <td>
                        <div class="actions">
                            <button class="btn btn-copy" onclick="copyText('{url}')" title="Copy URL">🔗 Copy</button>
                            <a href="{url}" target="_blank" class="btn btn-view" title="Play Video">▶️ Play</a>
                            <button class="btn btn-move" onclick="moveFolder('{quote(name)}')" title="Move to Folder">📁 Move</button>
                            <button class="btn btn-rename" onclick="renameFile('{quote(name)}')" title="Rename File">✏️ Rename</button>
                            <button class="btn btn-delete" onclick="deleteFile('{quote(name)}')" title="Delete File">🗑️ Delete</button>
                        </div>
                    </td>
                </tr>"""
        else:
            file_rows = "<tr><td colspan='4' style='text-align:center; color:#94a3b8;'>No files found in your bucket.</td></tr>"
    except Exception as e:
        file_rows = f"<tr><td colspan='4' style='color:#fb7185;'>Error connecting to R2: {str(e)}</td></tr>"

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Cloudflare R2 Manager</title>
        <style>{DASHBOARD_CSS}</style>
        <script>{DASHBOARD_JS}</script>
    </head>
    <body>
        <div class="container">
            <div class="header-bar">
                <h2>🛡️ Cloudflare R2 Manager</h2>
            </div>
            
            <div class="stats-grid">
                <div class="stat-card"><div class="stat-title">Storage Used</div><div class="stat-val">{human_size(total_size_bytes) if total_size_bytes else '0 B'}</div></div>
                <div class="stat-card"><div class="stat-title">Total Files</div><div class="stat-val">{total_files}</div></div>
                <div class="stat-card"><div class="stat-title">Active Bucket</div><div class="stat-val">{R2_BUCKET_NAME}</div></div>
            </div>

            <div class="controls">
                <input type="text" id="searchInput" class="search-box" onkeyup="filterTable()" placeholder="🔍 Search files by name or folder...">
            </div>

            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th id="th-0" onclick="sortTable(0, 'str')">File Path / Name <span></span></th>
                            <th id="th-1" onclick="sortTable(1, 'num')">Size <span></span></th>
                            <th id="th-2" onclick="sortTable(2, 'num')">Date Uploaded <span>🔽</span></th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>{file_rows}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html')

@routes.get('/delete_file')
async def web_delete_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    key = request.query.get('key')
    if key:
        try: 
            await asyncio.to_thread(sync_delete_r2_file, key)
            force_system_ram_purge()
        except: pass
    raise web.HTTPFound('/dashboard')

@routes.get('/rename_file')
async def web_rename_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key, new_key = request.query.get('old_key'), request.query.get('new_key')
    if old_key and new_key and old_key != new_key:
        try: 
            await asyncio.to_thread(sync_rename_r2_file, old_key, new_key)
            force_system_ram_purge()
        except: pass
    raise web.HTTPFound('/dashboard')

@routes.get('/move_file')
async def web_move_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key = request.query.get('old_key')
    target_folder = request.query.get('target_folder', '').strip().strip('/')
    if old_key and target_folder is not None:
        filename = old_key.split('/')[-1]
        new_key = f"{target_folder}/{filename}" if target_folder else filename
        if old_key != new_key:
            try: 
                await asyncio.to_thread(sync_rename_r2_file, old_key, new_key)
                force_system_ram_purge()
            except: pass
    raise web.HTTPFound('/dashboard')

@routes.get('/')
async def root(request):
    html = """
    <html><body style="background:#0f172a;color:#38bdf8;text-align:center;padding-top:150px;font-family:sans-serif;">
    <h1 style="font-size:40px;">✅ System Online</h1>
    <a href="/dashboard" style="display:inline-block;margin-top:20px;padding:15px 30px;background:#38bdf8;color:#0f172a;text-decoration:none;border-radius:8px;font-weight:bold;font-size:18px;">Go to Dashboard</a>
    </body></html>
    """
    return web.Response(text=html, content_type='text/html')

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data: return web.Response(text="Expired", status=410)
    msg, file_name = data['msg'], unquote(request.match_info['filename'])
    range_header = request.headers.get('Range')
    start = 0
    if range_header:
        match = re.search(r'bytes=(\d+)-', range_header)
        if match: start = int(match.group(1))
    resp = web.StreamResponse(status=206 if start else 200, headers={'Content-Disposition': f'attachment; filename="{file_name}"', 'Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4', 'Content-Length': str(msg.file.size - start)})
    await resp.prepare(request)
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576): await resp.write(chunk)
    except: pass
    return resp

# --- 6. TELEGRAM HANDLERS ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id != ADMIN_ID: return

    if event.text and event.text.startswith('/c_'):
        code = event.text.split('/c_')[1].strip()
        item = active_tasks.get(code)
        if item:
            item['cancel_event'].set()
            if item['process']:
                try: item['process'].terminate()
                except: pass
            await event.reply("🛑 **Torrent download cancelled successfully.**")
        else: await event.reply("❌ **Active task not found.**")
        return

    if event.file:
        await event.reply(
            f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`",
            buttons=[
                [Button.inline("🔗 Generate Direct Link", data=f"link_{event.id}")],
                [Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]
            ]
        )
        return

    if event.text and (event.text.startswith("http") or event.text.startswith("magnet:?")):
        async with global_semaphore:
            raw_text = event.text.strip()
            url = raw_text.split(" -n ")[0].strip()
            custom_name = raw_text.split(" -n ")[1].strip() if " -n " in raw_text else None
            
            target_folder = None
            if " -f " in url:
                url, target_folder = url.split(" -f ", 1)
            elif custom_name and " -f " in custom_name:
                custom_name, target_folder = custom_name.split(" -f ", 1)

            msg = await event.reply("🔗 **Processing Request...**")
            start_t = time.time()
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            
            try:
                final_path = await download_any_url(url, workspace, custom_name, msg, start_t)
                
                # Check if it is a ZIP HLS archive
                if final_path.lower().endswith('.zip'):
                    await msg.edit("📦 **Extracting HLS ZIP Archive...**")
                    extract_dir = os.path.join(workspace, "extracted")
                    os.makedirs(extract_dir, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(final_path, 'r').extractall(extract_dir))

                    project_name = os.path.splitext(os.path.basename(final_path))[0]
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
                    upload_result = await upload_to_r2(final_path, msg, target_folder)
                    r2_url, code = upload_result if isinstance(upload_result, tuple) else (upload_result, "unknown")

                    await msg.edit(
                        f"✅ **Leeched & Uploaded to R2!**\n\n🎬 `{os.path.basename(final_path)}`\n🔗 `{r2_url}`",
                        buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]] if code != "unknown" else None,
                        link_preview=False
                    )
            except Exception as e: 
                await msg.edit(f"❌ Error: {e}")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                force_system_ram_purge()

@client.on(events.CallbackQuery)
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
            await event.answer("Task cancelled successfully.", alert=True)
            try: await event.edit("🛑 **Task Cancelled by User.**")
            except: pass
        else: await event.answer("Task already finished.", alert=True)
        return

    if data.startswith("link_"):
        msg_id = int(data.split("_")[1])
        await event.answer("Generating Direct Link...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file: return await event.respond("❌ Error: File not found.")
        
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': tg_msg, 'timestamp': time.time()}
        
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/')
        if not base:
            app_name = os.environ.get('KOYEB_APP_NAME')
            base = f"https://{app_name}.koyeb.app" if app_name else "https://your-bot-name.koyeb.app"
            
        filename = get_unique_filename(clean_double_extension(re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")))
        hotlink = f"{base}/{code}/{quote(filename)}"
        
        await event.respond(f"🚀 **Direct Download Link:**\n\n`{hotlink}`\n\n💡 *Valid for 24 hours. Paste into IDM for max speed.*")
        return

    if data.startswith("delr2_"):
        code = data.split("_")[1]
        item = link_storage.get(code)
        if item and 's3_key' in item:
            s3_key = item['s3_key']
            await event.answer("Deleting file from R2...", alert=False)
            try:
                await asyncio.to_thread(sync_delete_r2_file, s3_key)
                await event.edit(f"🗑️ **File Deleted from Cloudflare R2!**\n\nKey: `{s3_key}`")
                force_system_ram_purge()
            except Exception as e:
                await event.edit(f"❌ Delete Error: {e}")
        else:
            await event.answer("❌ Reference expired or already deleted.", alert=True)
        return

    if data.startswith("r2_"):
        msg_id = int(data.split("_")[1])
        await event.answer("Processing R2 Upload...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        
        async with global_semaphore:
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            raw_filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
            filename = get_unique_filename(clean_double_extension(raw_filename))
            file_path = os.path.join(workspace, filename)
            
            status = await event.respond(f"⬇️ Downloading from Telegram...")
            start_t = time.time()
            try:
                with open(file_path, 'wb') as f:
                    async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                        f.write(chunk)
                        if f.tell() % (10 * 1024 * 1024) == 0: 
                            await status.edit(get_status_text("TG Down", filename, f.tell(), tg_msg.file.size, start_t))
                
                # Check if it's a ZIP HLS archive
                if filename.lower().endswith('.zip'):
                    await status.edit("📦 **Extracting HLS ZIP Archive...**")
                    extract_dir = os.path.join(workspace, "extracted")
                    os.makedirs(extract_dir, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(file_path, 'r').extractall(extract_dir))

                    project_name = os.path.splitext(filename)[0]
                    extracted_items = os.listdir(extract_dir)
                    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
                        upload_source_dir = os.path.join(extract_dir, extracted_items[0])
                        s3_prefix = extracted_items[0]
                    else:
                        upload_source_dir = extract_dir
                        s3_prefix = project_name

                    await status.edit(f"⬆️ **Uploading HLS Pack to R2...**\n📂 `{s3_prefix}`")
                    await asyncio.to_thread(sync_r2_upload_folder, upload_source_dir, s3_prefix, asyncio.get_running_loop(), status, time.time())
                    
                    master_url = f"{R2_PUBLIC_URL}/{quote(s3_prefix, safe='/')}/master.m3u8"
                    await status.edit(f"✅ **HLS Uploaded!**\n\n🎬 `{project_name}`\n📺 **Stream Link:**\n`{master_url}`", link_preview=False)

                else:
                    r2_url, code = await upload_to_r2(file_path, status)
                    await status.edit(
                        f"✅ **Cloudflare R2 Complete!**\n\n🎬 `{filename}`\n🔗 `{r2_url}`",
                        buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]]
                    )
                
            except Exception as e: 
                await status.edit(f"❌ Error: {e}")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                force_system_ram_purge()

# --- 7. STARTUP ---
async def main():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

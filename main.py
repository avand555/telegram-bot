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
mimetypes.add_type('video/mp2t', '.ts')

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
    return (f"🧲 **{action}...**\n"
            f"╭ `[{p_bar[:10]}]` » `{percent}%`\n"
            f"├ **Processed:** `{downloaded} of {total}`\n"
            f"├ **Speed:** `{speed}`\n"
            f"├ **ETA:** `{eta}`\n"
            f"├ **Peers:** `{cn}`\n"
            f"├ **Elapsed:** `{elapsed}`\n"
            f"├ **Engine:** `Aria2 v1.36.0`\n"
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

# R2 Directory List supporting dynamic prefixes
def sync_get_r2_files(prefix=""):
    s3 = get_r2_client()
    return s3.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix, Delimiter='/')

def sync_delete_r2_file(s3_key):
    s3 = get_r2_client()
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)

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
        content_type = content_type or 'application/octet-stream'

        extra_args = {'ContentType': content_type}
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
# --- 4. DOWNLOAD ENGINES ---
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
    if not largest: raise ValueError("Torrent failed.")
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
# --- 5. SECURED WEB DASHBOARD & UI ---
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
    
    /* Breadcrumbs Navigation */
    .breadcrumbs { display: flex; align-items: center; gap: 8px; background: var(--card); padding: 12px 20px; border-radius: 8px; border: 1px solid var(--border); margin-bottom: 20px; font-size: 15px; }
    .breadcrumb-link { color: var(--accent); text-decoration: none; font-weight: 600; }
    .breadcrumb-link:hover { text-decoration: underline; }
    .divider { color: var(--muted); }

    /* Controls */
    .controls { display: flex; gap: 10px; margin-bottom: 15px; width: 100%; }
    .search-box { flex-grow: 1; padding: 14px 20px; border-radius: 8px; border: 1px solid var(--border); background: var(--card); color: var(--text); font-size: 15px; outline: none; transition: 0.2s; }
    .search-box:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.2); }

    /* Table */
    .table-wrapper { overflow-x: auto; background: var(--card); border-radius: 10px; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    table { width: 100%; border-collapse: collapse; min-width: 900px; }
    th { background: #0f172a; color: var(--muted); padding: 16px; text-align: left; font-size: 13px; font-weight: 600; cursor: pointer; user-select: none; }
    th:hover { color: var(--text); }
    td { padding: 16px; border-bottom: 1px solid var(--border); font-size: 14px; word-break: break-all; color: #cbd5e1; }
    tr:last-child td { border-bottom: none; }
    tr:hover { background: #334155; }

    /* Folder Links */
    .folder-link { color: #ffb703; text-decoration: none; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
    .folder-link:hover { text-decoration: underline; }

    /* Actions */
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
    function deleteFile(key, prefix) {
        let decodedKey = decodeURIComponent(key);
        if (confirm('⚠️ PERMANENTLY DELETE: \\n' + decodedKey)) {
            window.location.href = '/delete_file?key=' + encodeURIComponent(decodedKey) + '&prefix=' + encodeURIComponent(prefix);
        }
    }
    function renameFile(key, prefix) {
        let decodedKey = decodeURIComponent(key);
        let newKey = prompt('✏️ Rename File (Full Path):', decodedKey);
        if (newKey && newKey !== decodedKey) {
            window.location.href = '/rename_file?old_key=' + encodeURIComponent(decodedKey) + '&new_key=' + encodeURIComponent(newKey) + '&prefix=' + encodeURIComponent(prefix);
        }
    }
    function moveFolder(key, prefix) {
        let decodedKey = decodeURIComponent(key);
        let currentDir = decodedKey.includes('/') ? decodedKey.substring(0, decodedKey.lastIndexOf('/')) : '';
        let targetFolder = prompt('📁 Enter target folder path (e.g. Movies/2026):', currentDir);
        if (targetFolder !== null) {
            window.location.href = '/move_file?old_key=' + encodeURIComponent(decodedKey) + '&target_folder=' + encodeURIComponent(targetFolder) + '&prefix=' + encodeURIComponent(prefix);
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
            let valA = a.children[colIndex].getAttribute("data-val") || "";
            let valB = b.children[colIndex].getAttribute("data-val") || "";
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

    prefix = request.query.get('prefix', '').strip()
    file_rows = ""
    
    # Generate breadcrumb links for navigation
    parts = [p for p in prefix.split('/') if p]
    breadcrumbs_html = '<a href="/dashboard" class="breadcrumb-link">🏠 Home</a>'
    curr_path = ""
    for part in parts:
        curr_path += part + "/"
        breadcrumbs_html += f' <span class="divider">/</span> <a href="/dashboard?prefix={quote(curr_path)}" class="breadcrumb-link">{part}</a>'

    try:
        response = await asyncio.to_thread(sync_get_r2_files, prefix)
        
        # 1. ADD FOLDER ROWS (CommonPrefixes)
        if 'CommonPrefixes' in response:
            for pref in response['CommonPrefixes']:
                folder_path = pref['Prefix']
                folder_name = folder_path.rstrip('/').split('/')[-1]
                encoded_folder = quote(folder_path)
                file_rows += f"""
                <tr style="background: rgba(255, 183, 3, 0.03);">
                    <td data-val="{folder_name}"><a href="/dashboard?prefix={encoded_folder}" class="folder-link">📁 {folder_name}/</a></td>
                    <td data-val="0">-</td>
                    <td data-val="0">Folder</td>
                    <td>-</td>
                </tr>"""

        # 2. ADD FILE ROWS (Contents)
        if 'Contents' in response:
            for obj in sorted(response['Contents'], key=lambda x: x['LastModified'], reverse=True):
                name = obj['Key']
                if name == prefix: continue # Skip root directory key
                
                size_bytes = obj['Size']
                size_str = human_size(size_bytes)
                timestamp = obj['LastModified'].timestamp()
                date_str = obj['LastModified'].strftime("%Y-%m-%d %H:%M")
                url = f"{R2_PUBLIC_URL}/{quote(name, safe='/')}"
                display_name = name.split('/')[-1]
                
                file_rows += f"""
                <tr>
                    <td data-val="{display_name}"><span class="file-name">📄 {display_name}</span></td>
                    <td data-val="{size_bytes}">{size_str}</td>
                    <td data-val="{timestamp}">{date_str}</td>
                    <td>
                        <div class="actions">
                            <button class="btn btn-copy" onclick="copyText('{url}')">🔗 Copy</button>
                            <a href="{url}" target="_blank" class="btn btn-view">▶️ Play</a>
                            <button class="btn btn-move" onclick="moveFolder('{quote(name)}', '{quote(prefix)}')">📁 Move</button>
                            <button class="btn btn-rename" onclick="renameFile('{quote(name)}', '{quote(prefix)}')">✏️ Rename</button>
                            <button class="btn btn-delete" onclick="deleteFile('{quote(name)}', '{quote(prefix)}')">🗑️ Delete</button>
                        </div>
                    </td>
                </tr>"""
        
        if not file_rows:
            file_rows = "<tr><td colspan='4' style='text-align:center; color:#94a3b8;'>Empty directory.</td></tr>"
            
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
                <h2>🛡️ Cloudflare R2 Control Panel</h2>
            </div>
            
            <div class="breadcrumbs">
                {breadcrumbs_html}
            </div>

            <div class="controls">
                <input type="text" id="searchInput" class="search-box" onkeyup="filterTable()" placeholder="🔍 Search current directory...">
            </div>

            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th id="th-0" onclick="sortTable(0, 'str')">Name <span></span></th>
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
    prefix = request.query.get('prefix', '')
    if key:
        try: await asyncio.to_thread(sync_delete_r2_file, key); force_system_ram_purge()
        except: pass
    raise web.HTTPFound(f'/dashboard?prefix={quote(prefix)}')

@routes.get('/rename_file')
async def web_rename_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key, new_key = request.query.get('old_key'), request.query.get('new_key')
    prefix = request.query.get('prefix', '')
    if old_key and new_key and old_key != new_key:
        try: await asyncio.to_thread(sync_rename_r2_file, old_key, new_key); force_system_ram_purge()
        except: pass
    raise web.HTTPFound(f'/dashboard?prefix={quote(prefix)}')

@routes.get('/move_file')
async def web_move_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key = request.query.get('old_key')
    target_folder = request.query.get('target_folder', '').strip().strip('/')
    prefix = request.query.get('prefix', '')
    if old_key and target_folder is not None:
        filename = old_key.split('/')[-1]
        new_key = f"{target_folder}/{filename}" if target_folder else filename
        if old_key != new_key:
            try: await asyncio.to_thread(sync_rename_r2_file, old_key, new_key); force_system_ram_purge()
            except: pass
    raise web.HTTPFound(f'/dashboard?prefix={quote(prefix)}')

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

# --- 10. RE-ENABLED BOT CODE WITH FIXES ---
# ... (All Bot handling logic remains intact exactly as in previous version, including /c_, yt-dlp, and folder uploads) ...

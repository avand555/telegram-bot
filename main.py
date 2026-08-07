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
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import InputFileBig, InputFile

# Web, Storage & Engine Imports
from aiohttp import web, ClientSession
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
import yt_dlp
import nest_asyncio

# CRITICAL FOR COLAB & DOCKER ASYNC
nest_asyncio.apply()

# ============================================
# --- 1. SECURE CONFIGURATION (FROM KOYEB) ---
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

PUBLIC_TRACKERS = ",".join([
    "udp://tracker.opentrackr.org:1337/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "udp://opentracker.i2p.rocks:6969/announce"
])

global_semaphore = asyncio.Semaphore(4)
link_storage = {}
active_tasks = {}
routes = web.RouteTableDef()

# --- 2. C-LEVEL MEMORY PURGE ---
def force_system_ram_purge():
    gc.collect()
    try: ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception: pass

def get_dir_size(path):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                total += os.path.getsize(fp)
    return total

# --- 3. AUTO-BINARY DOWNLOADER (ARIA2C) ---
def get_aria2_executable():
    """Ensures aria2c exists, downloads static binary if missing."""
    if shutil.which('aria2c'): return 'aria2c'
    local_aria = os.path.abspath('./aria2c')
    if os.path.exists(local_aria): return local_aria
    print("📥 Downloading static aria2c binary...")
    try:
        tar_url = "https://github.com/P3TERX/aria2-builder/releases/download/1.36.0/aria2-1.36.0-static-linux-amd64.tar.gz"
        subprocess.run(f"wget -qO- {tar_url} | tar -xz", shell=True, check=True)
        if os.path.exists('./aria2c'):
            os.chmod('./aria2c', 0o755)
            return local_aria
    except Exception as e: print(f"Failed to download aria2c: {e}")
    return 'aria2c'

aria_re = re.compile(r'\[#(?P<gid>\w+)\s+(?P<downloaded>[^\s/]+)/(?P<total>[^\s\(\)]+)(?:\((?P<percent>\d+)%\))?\s+CN:(?P<cn>\d+)\s+SPD:(?P<speed>[^\s\]]+)(?:\s+ETA:(?P<eta>[^\s\]]+))?\]')

# --- 4. SETUP TELEGRAM CLIENT ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection=ConnectionTcpFull, use_ipv6=False)

# --- 5. UI HELPERS ---
def human_size(bytes_val):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024: return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024
    return "0 B"

def get_status_text(action, filename, current, total, start_time):
    now = time.time()
    diff = now - start_time or 0.001
    perc = (current / total) * 100 if total > 0 else 0
    speed = current / diff 
    done = int(perc // 10)
    p_bar = "■" * done + "□" * (10 - done)
    total_str = human_size(total) if total > 0 else "?? MB"
    return (f"🚀 **{action}**\n📦 `{filename}`\n\n"
            f"🌀 **Progress:** `[{p_bar}] {perc:.2f}%`\n"
            f"⚡ **Speed:** `{human_size(speed)}/s`\n"
            f"📂 **Size:** `{human_size(current)} / {total_str}`")

def format_saas_progress(action, filename, percent, downloaded, total, speed, eta, cn, elapsed, task_code):
    done = int(percent // 10)
    p_bar = "●" * done
    if done < 10:
        p_bar += "◔" if (percent % 10) >= 5 else "○"
        p_bar += "○" * (9 - done)
    return (
        f"🧲 **{action}...**\n"
        f"╭ `[{p_bar}]` » `{percent}%`\n"
        f"├ **Processed:** `{downloaded} of {total}`\n"
        f"├ **Speed:** `{speed}`\n"
        f"├ **ETA:** `{eta}`\n"
        f"├ **Peers (CN):** `{cn}`\n"
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

# --- 6. CLOUDFLARE R2 CLIENT (BOTO3) ---
def get_r2_client():
    clean_id = R2_ACCOUNT_ID.replace("https://", "").replace("http://", "").split(".")[0].strip('/')
    endpoint = f"https://{clean_id}.r2.cloudflarestorage.com"
    r2_config = Config(region_name='auto', signature_version='s3v4')
    return boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, config=r2_config)

def sync_get_r2_files():
    s3 = get_r2_client()
    return s3.list_objects_v2(Bucket=R2_BUCKET_NAME)

def sync_delete_r2_file(s3_key):
    s3 = get_r2_client()
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)

def sync_rename_r2_file(old_key, new_key):
    s3 = get_r2_client()
    s3.copy({'Bucket': R2_BUCKET_NAME, 'Key': old_key}, R2_BUCKET_NAME, new_key)
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=old_key)

def sync_r2_upload(filename, s3_key, loop, status_msg, start_t):
    s3 = get_r2_client()
    file_size = os.path.getsize(filename)
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type: mime_type = 'video/mp4'

    class ProgressCallback:
        def __init__(self):
            self.seen = 0
            self.last_update = 0
        def __call__(self, bytes_amount):
            self.seen += bytes_amount
            now = time.time()
            if now - self.last_update > 4:
                self.last_update = now
                try:
                    text = get_status_text("R2 Uploading", os.path.basename(filename), self.seen, file_size, start_t)
                    asyncio.run_coroutine_threadsafe(status_msg.edit(text), loop)
                except: pass

    extra_args = {'ContentType': mime_type, 'ContentDisposition': 'inline'}
    transfer_config = TransferConfig(multipart_threshold=8*1024*1024, multipart_chunksize=8*1024*1024, max_concurrency=4)
    s3.upload_file(filename, R2_BUCKET_NAME, s3_key, Callback=ProgressCallback(), ExtraArgs=extra_args, Config=transfer_config)

async def upload_to_r2(filename, status_msg, target_folder=None):
    start_t = time.time()
    loop = asyncio.get_running_loop()
    basename = os.path.basename(filename)
    if target_folder: s3_key = f"{target_folder.strip('/')}/{basename}"
    else:
        now = datetime.datetime.now()
        s3_key = f"{now.year}/{now.month}/{now.day}/{basename}"
    
    await status_msg.edit(f"⬆️ **Connecting to Cloudflare R2...**\n🎬 `{basename}`")
    await asyncio.to_thread(sync_r2_upload, filename, s3_key, loop, status_msg, start_t)
    
    code = secrets.token_urlsafe(8)
    link_storage[code] = {'s3_key': s3_key}
    return f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}", code

# ============================================
# --- 7. SECURED WEB DASHBOARD (UI/CSS/JS) ---
# ============================================
def check_dashboard_auth(request):
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Basic '): return False
    try:
        decoded = base64.b64decode(auth_header.split(' ', 1)[1]).decode('utf-8')
        user, password = decoded.split(':', 1)
        return user == DASHBOARD_USER and password == DASHBOARD_PASS
    except Exception: return False

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
        if (confirm('⚠️ PERMANENTLY DELETE:\\n' + decodedKey)) {
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
        let targetFolder = prompt('📁 Enter target folder (e.g. Movies/2026):', currentDir);
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
            if (type === 'num') return dir === 'asc' ? parseFloat(valA) - parseFloat(valB) : parseFloat(valB) - parseFloat(valA);
            else return dir === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA);
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

    file_rows, total_size_bytes, total_files = "", 0, 0
    try:
        response = await asyncio.to_thread(sync_get_r2_files)
        if 'Contents' in response:
            total_files = len(response['Contents'])
            for obj in sorted(response['Contents'], key=lambda x: x['LastModified'], reverse=True):
                name, size_bytes = obj['Key'], obj['Size']
                total_size_bytes += size_bytes
                size_str, timestamp = human_size(size_bytes), obj['LastModified'].timestamp()
                date_str = obj['LastModified'].strftime("%Y-%m-%d %H:%M")
                url = f"{R2_PUBLIC_URL}/{quote(name, safe='/')}"
                file_rows += f"""<tr>
                    <td data-val="{name}"><span class="file-name">{name}</span></td>
                    <td data-val="{size_bytes}">{size_str}</td><td data-val="{timestamp}">{date_str}</td>
                    <td><div class="actions">
                        <button class="btn btn-copy" onclick="copyText('{url}')">🔗 Copy</button>
                        <a href="{url}" target="_blank" class="btn btn-view">▶️ Play</a>
                        <button class="btn btn-move" onclick="moveFolder('{quote(name)}')">📁 Move</button>
                        <button class="btn btn-rename" onclick="renameFile('{quote(name)}')">✏️ Rename</button>
                        <button class="btn btn-delete" onclick="deleteFile('{quote(name)}')">🗑️ Delete</button>
                    </div></td></tr>"""
        else: file_rows = "<tr><td colspan='4' style='text-align:center; color:#94a3b8;'>No files found.</td></tr>"
    except Exception as e: file_rows = f"<tr><td colspan='4' style='color:#fb7185;'>Error connecting to R2: {str(e)}</td></tr>"

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>R2 Dash</title><style>{DASHBOARD_CSS}</style><script>{DASHBOARD_JS}</script></head><body>
        <div class="container"><div class="header-bar"><h2>🛡️ Cloudflare R2 Manager</h2></div>
            <div class="stats-grid">
                <div class="stat-card"><div class="stat-title">Storage Used</div><div class="stat-val">{human_size(total_size_bytes) if total_size_bytes else '0 B'}</div></div>
                <div class="stat-card"><div class="stat-title">Total Files</div><div class="stat-val">{total_files}</div></div>
                <div class="stat-card"><div class="stat-title">Active Bucket</div><div class="stat-val">{R2_BUCKET_NAME}</div></div>
            </div>
            <div class="controls"><input type="text" id="searchInput" class="search-box" onkeyup="filterTable()" placeholder="🔍 Search files by name or folder..."></div>
            <div class="table-wrapper"><table><thead><tr><th id="th-0" onclick="sortTable(0, 'str')">File Path / Name <span></span></th><th id="th-1" onclick="sortTable(1, 'num')">Size <span></span></th><th id="th-2" onclick="sortTable(2, 'num')">Date Uploaded <span>🔽</span></th><th>Actions</th></tr></thead><tbody>{file_rows}</tbody></table></div>
        </div></body></html>"""
    return web.Response(text=html, content_type='text/html')

@routes.get('/delete_file')
async def web_delete_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    if key := request.query.get('key'):
        try: await asyncio.to_thread(sync_delete_r2_file, key); force_system_ram_purge()
        except: pass
    raise web.HTTPFound('/dashboard')

@routes.get('/rename_file')
async def web_rename_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key, new_key = request.query.get('old_key'), request.query.get('new_key')
    if old_key and new_key and old_key != new_key:
        try: await asyncio.to_thread(sync_rename_r2_file, old_key, new_key); force_system_ram_purge()
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
            try: await asyncio.to_thread(sync_rename_r2_file, old_key, new_key); force_system_ram_purge()
            except: pass
    raise web.HTTPFound('/dashboard')

@routes.get('/')
async def root(request):
    html = "<html><body style='background:#0f172a;color:#38bdf8;text-align:center;padding-top:150px;font-family:sans-serif;'><h1 style='font-size:40px;'>✅ System Online</h1><a href='/dashboard' style='display:inline-block;margin-top:20px;padding:15px 30px;background:#38bdf8;color:#0f172a;text-decoration:none;border-radius:8px;font-weight:bold;font-size:18px;'>Go to Dashboard</a></body></html>"
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
    resp = web.StreamResponse(status=206 if range_header else 200, headers={'Content-Disposition': f'attachment; filename="{file_name}"', 'Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4', 'Content-Length': str(msg.file.size - start)})
    await resp.prepare(request)
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576): await resp.write(chunk)
    except: pass
    return resp

# --- 9. DOWNLOAD ENGINES (MAGNET + DIRECT + YT-DLP) ---
async def download_magnet(url, workspace, custom_name, msg, start_t):
    aria_cmd = get_aria2_executable()
    await msg.edit("🧲 **Initializing Torrent Engine (aria2c)...**")
    process = await asyncio.create_subprocess_exec(
        aria_cmd, '--seed-time=0', '--max-connection-per-server=16', '--split=16',
        '--summary-interval=3', '--bt-stop-timeout=120', f'--bt-tracker={PUBLIC_TRACKERS}',
        f'--dir={workspace}', url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    task_code = secrets.token_urlsafe(8)
    active_tasks[task_code] = {'process': process, 'cancel_event': asyncio.Event(), 'dir': workspace}

    last_update = 0
    while process.returncode is None:
        line_bytes = await process.stdout.readline()
        if not line_bytes: break
        line = line_bytes.decode('utf-8', errors='ignore').strip()
        match = aria_re.search(line)
        if match:
            percent = int(match.group('percent') or 0)
            downloaded, total, cn, speed = match.group('downloaded'), match.group('total'), match.group('cn'), match.group('speed') + "/s"
            eta = match.group('eta') or "Calculating..."
            
            active_file = "Fetching Metadata..."
            for r_dir, dirs, files in os.walk(workspace):
                for f in files:
                    if not f.endswith('.aria2'): active_file = f; break
            
            now = time.time()
            if now - last_update > 4:
                elapsed = get_readable_time(int(now - start_t))
                p_text = format_saas_progress("Download", active_file, percent, downloaded, total, speed, eta, cn, elapsed, task_code)
                try:
                    await msg.edit(p_text, buttons=[[Button.inline("❌ Cancel", data=f"canceltask_{task_code}")]])
                    last_update = now
                except: pass

    await process.wait()
    largest_file, max_size = None, 0
    for root_dir, dirs, files in os.walk(workspace):
        for f in files:
            fp = os.path.join(root_dir, f)
            sz = os.path.getsize(fp)
            if sz > max_size: max_size, largest_file = sz, fp

    if not largest_file or max_size == 0:
        active_tasks.pop(task_code, None)
        raise ValueError("Torrent failed or returned no files.")

    final_name = custom_name if custom_name else os.path.basename(largest_file)
    final_path = os.path.join(workspace, final_name)
    shutil.move(largest_file, final_path)
    active_tasks.pop(task_code, None)
    return final_path

async def download_any_url(url, workspace, custom_name, msg, start_t):
    if url.startswith("magnet:?"):
        return await download_magnet(url, workspace, custom_name, msg, start_t)

    try:
        await msg.edit("🅿️ **Extracting File Info via yt-dlp...**")
        filename = await asyncio.to_thread(sync_yt_dlp_download, url, custom_name)
        if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
            final_path = os.path.join(workspace, os.path.basename(filename))
            shutil.move(filename, final_path)
            return final_path
    except Exception: pass

    async with ClientSession() as sess:
        async with sess.get(url, allow_redirects=True) as r:
            if "text/html" in r.headers.get("Content-Type", ""): raise ValueError("HTML webpage detected, not a direct file.")
            f_size = int(r.headers.get("Content-Length", 0))
            filename = custom_name or unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
            if not "." in filename: filename += ".mp4"
            filename = re.sub(r'[\\/*?:"<>|]', "", filename)

            final_path = os.path.join(workspace, filename)
            await msg.edit(f"⬇️ **Leeching Direct Link...**\n🎬 `{filename}`")
            with open(final_path, 'wb') as f:
                async for chunk in r.content.iter_chunked(1024*1024):
                    f.write(chunk)
                    if f.tell() % (10 * 1024 * 1024) == 0:
                        await msg.edit(get_status_text("Leeching", filename, f.tell(), f_size, start_t))
            return final_path

# --- 10. BOT HANDLERS ---
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
                upload_result = await upload_to_r2(final_path, msg, target_folder)
                r2_url, code = upload_result if isinstance(upload_result, tuple) else (upload_result, "unknown")

                await msg.edit(
                    f"✅ **Leeched & Uploaded to R2!**\n\n🎬 `{os.path.basename(final_path)}`\n🔗 `{r2_url}`",
                    buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]] if code != "unknown" else None
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
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/') or f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
        
        await event.respond(f"🚀 **Direct Download Link:**\n\n`{base}/{code}/{quote(filename)}`\n\n💡 *Valid for 24 hours.*")
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
            except Exception as e: await event.edit(f"❌ Delete Error: {e}")
        else: await event.answer("❌ Reference expired or already deleted.", alert=True)
        return

    if data.startswith("r2_"):
        msg_id = int(data.split("_")[1])
        await event.answer("Processing R2 Upload...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        
        async with global_semaphore:
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
            file_path = os.path.join(workspace, filename)
            
            status = await event.respond(f"⬇️ Downloading from Telegram...")
            start_t = time.time()
            try:
                with open(file_path, 'wb') as f:
                    async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                        f.write(chunk)
                        if f.tell() % (10 * 1024 * 1024) == 0: 
                            await status.edit(get_status_text("TG Down", filename, f.tell(), tg_msg.file.size, start_t))
                
                upload_result = await upload_to_r2(file_path, status)
                r2_url, code = upload_result if isinstance(upload_result, tuple) else (upload_result, "unknown")

                await status.edit(f"✅ **Cloudflare R2 Complete!**\n\n🎬 `{filename}`\n🔗 `{r2_url}`", buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]])
            except Exception as e: await status.edit(f"❌ Error: {e}")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                force_system_ram_purge()

# --- 11. STARTUP ---
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

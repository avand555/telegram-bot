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
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest, GetFileRequest
from telethon.tl.types import InputFileBig, InputFile

# Web, Storage & Engine Imports
from aiohttp import web, ClientSession, FormData
import aiohttp
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
import yt_dlp

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

global_semaphore = asyncio.Semaphore(4)
link_storage = {}
routes = web.RouteTableDef()

# --- 2. FILENAME CLEANERS ---
def clean_double_extension(filename):
    while filename.lower().endswith('.mp4.mp4') or filename.lower().endswith('.mkv.mkv'):
        filename = filename[:-4]
    return filename

def get_unique_filename(filepath):
    filepath = clean_double_extension(filepath)
    if not os.path.exists(filepath): return filepath
    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"

# --- 3. YT-DLP / GDRIVE ENGINE ---
def sync_yt_dlp_download(url, custom_name=None):
    if custom_name:
        custom_name = get_unique_filename(custom_name)
        out_tmpl = custom_name
    else:
        out_tmpl = '%(title)s.%(ext)s'

    ydl_opts = {
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'overwrites': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        cleaned = clean_double_extension(filename)
        if cleaned != filename and os.path.exists(filename):
            os.rename(filename, cleaned)
            filename = cleaned
        return filename

# --- 4. C-LEVEL MEMORY PURGE ---
def force_system_ram_purge():
    gc.collect()
    try: ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception: pass

# --- 5. SETUP CLIENT ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection=ConnectionTcpFull, use_ipv6=False)

# --- 6. UI HELPERS ---
def human_size(bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes < 1024: return f"{bytes:.2f} {unit}"
        bytes /= 1024
    return "0 B"

def get_status_text(action, filename, current, total, start_time):
    now = time.time()
    diff = now - start_time or 0.001
    perc = (current / total) * 100 if total > 0 else 0
    speed = current / diff 
    done = int(perc // 10)
    p_bar = "■" * done + "□" * (10 - done)
    return (f"🚀 **{action}**\n📦 `{filename}`\n\n"
            f"🌀 **Progress:** `[{p_bar}] {perc:.2f}%`\n"
            f"⚡ **Speed:** `{human_size(speed)}/s`\n"
            f"📂 **Size:** `{human_size(current)} / {human_size(total)}`")

# --- 7. R2 CLIENT & SYNC S3 OPERATIONS ---
def get_r2_client():
    clean_id = R2_ACCOUNT_ID.replace("https://", "").replace("http://", "").split(".")[0].strip('/')
    endpoint = f"https://{clean_id}.r2.cloudflarestorage.com"
    r2_config = Config(region_name='auto', signature_version='s3v4')
    return boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=r2_config
    )

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

    extra_args = {
        'ContentType': mime_type,
        'ContentDisposition': 'inline'
    }

    s3.upload_file(
        filename, 
        R2_BUCKET_NAME, 
        s3_key, 
        Callback=ProgressCallback(), 
        ExtraArgs=extra_args
    )

async def upload_to_r2(filename, status_msg):
    start_t = time.time()
    loop = asyncio.get_running_loop()
    
    now = datetime.datetime.now()
    basename = os.path.basename(filename)
    s3_key = f"{now.year}/{now.month}/{now.day}/{basename}"
    
    await status_msg.edit(f"⬆️ **Connecting to Cloudflare R2...**\n🎬 `{basename}`")
    await asyncio.to_thread(sync_r2_upload, filename, s3_key, loop, status_msg, start_t)
    
    code = secrets.token_urlsafe(8)
    link_storage[code] = {'s3_key': s3_key}
    
    public_link = f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}"
    return public_link, code


# ============================================
# --- 8. SECURED WEB DASHBOARD & UI ---
# ============================================
DASHBOARD_CSS = """
    :root { --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --accent: #38bdf8; --border: #334155; }
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 20px; }
    .container { max-width: 1200px; margin: auto; }
    .header-bar { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; margin-bottom: 20px; gap: 15px; }
    h2 { margin: 0; color: var(--text); font-size: 26px; display: flex; align-items: center; gap: 10px; border-bottom: 2px solid var(--border); padding-bottom: 10px; width: 100%; }
    
    /* Stats Cards */
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
    .stat-card { background: var(--card); padding: 15px; border-radius: 10px; border: 1px solid var(--border); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    .stat-title { font-size: 12px; color: var(--muted); text-transform: uppercase; font-weight: bold; margin-bottom: 5px; }
    .stat-val { font-size: 20px; font-weight: bold; color: var(--accent); }

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
    function copyText(t) { navigator.clipboard.writeText(t); alert('✅ URL Copied to clipboard!'); }
    
    function deleteFile(key) {
        let decodedKey = decodeURIComponent(key);
        if (confirm('⚠️ PERMANENTLY DELETE: \\n' + decodedKey + '\\n\\nAre you sure?')) {
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
            if (type === 'num') {
                return dir === 'asc' ? parseFloat(valA) - parseFloat(valB) : parseFloat(valB) - parseFloat(valA);
            } else {
                return dir === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA);
            }
        });

        table.innerHTML = "";
        rows.forEach(row => table.appendChild(row));

        document.querySelectorAll("th span").forEach(span => span.innerText = "");
        document.getElementById("th-" + colIndex).querySelector("span").innerText = dir === 'asc' ? ' 🔼' : ' 🔽';
    }
"""

def check_dashboard_auth(request):
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Basic '):
        return False
    try:
        encoded_credentials = auth_header.split(' ', 1)[1]
        decoded = base64.b64decode(encoded_credentials).decode('utf-8')
        user, password = decoded.split(':', 1)
        return user == DASHBOARD_USER and password == DASHBOARD_PASS
    except Exception:
        return False

@routes.get('/dashboard')
async def dashboard_handler(request):
    if not check_dashboard_auth(request):
        return web.Response(
            status=401,
            headers={'WWW-Authenticate': 'Basic realm="Cloudflare R2 Dashboard"'},
            text="🔒 Access Denied"
        )

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
        <title>R2 Cloud Manager</title>
        <style>{DASHBOARD_CSS}</style>
        <script>{DASHBOARD_JS}</script>
    </head>
    <body>
        <div class="container">
            <div class="header-bar">
                <h2>🛡️ Cloudflare R2 Control Panel</h2>
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
    resp = web.StreamResponse(status=206 if range_header else 200, 
                              headers={'Content-Disposition': f'attachment; filename="{file_name}"',
                                       'Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4',
                                       'Content-Length': str(msg.file.size - start)})
    await resp.prepare(request)
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576):
            await resp.write(chunk)
    except: pass
    return resp

# --- 9. TG FAST UPLOAD ---
async def fast_upload(client, file_path, msg, filename):
    file_size = os.path.getsize(file_path)
    part_size, file_id = 512 * 1024, random.getrandbits(63)
    start_time, uploaded_bytes = time.time(), 0
    sem = asyncio.Semaphore(15) 
    async def upload_part(idx):
        nonlocal uploaded_bytes
        async with sem:
            with open(file_path, 'rb') as f:
                f.seek(idx * part_size); chunk = f.read(part_size)
            if file_size > 10*1024*1024: await client(SaveBigFilePartRequest(file_id, idx, math.ceil(file_size/part_size), chunk))
            else: await client(SaveFilePartRequest(file_id, idx, chunk))
            uploaded_bytes += len(chunk)
    tasks = [upload_part(i) for i in range(math.ceil(file_size/part_size))]
    async def updater():
        while uploaded_bytes < file_size:
            await asyncio.sleep(4)
            try: await msg.edit(get_status_text("Uploading to TG", filename, uploaded_bytes, file_size, start_time))
            except: pass
    u_task = asyncio.create_task(updater())
    await asyncio.gather(*tasks); u_task.cancel()
    return InputFileBig(file_id, math.ceil(file_size/part_size), filename) if file_size > 10*1024*1024 else InputFile(file_id, math.ceil(file_size/part_size), filename, '')

# --- 10. HYBRID DOWNLOADER ---
async def download_any_url(url, custom_name, msg, start_t):
    try:
        await msg.edit("🅿️ **Extracting File Info via yt-dlp...**")
        filename = await asyncio.to_thread(sync_yt_dlp_download, url, custom_name)
        if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
            return filename
    except Exception: pass

    async with ClientSession() as sess:
        async with sess.get(url, allow_redirects=True) as r:
            if "text/html" in r.headers.get("Content-Type", ""): raise ValueError("HTML webpage detected, not a file.")
            f_size = int(r.headers.get("Content-Length", 0))
            filename = custom_name or unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
            if not "." in filename: filename += ".mp4"
            filename = get_unique_filename(re.sub(r'[\\/*?:"<>|]', "", clean_double_extension(filename)))

            await msg.edit(f"⬇️ **Leeching Direct Link...**\n🎬 `{filename}`")
            with open(filename, 'wb') as f:
                async for chunk in r.content.iter_chunked(1024*1024):
                    f.write(chunk)
                    if f.tell() % (10 * 1024 * 1024) == 0:
                        await msg.edit(get_status_text("Leeching", filename, f.tell(), f_size, start_t))
            return filename

# --- 11. BOT HANDLERS ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id != ADMIN_ID: return

    if event.file:
        await event.reply(
            f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`",
            buttons=[
                [Button.inline("🔗 Get Direct Link", data=f"link_{event.id}")],
                [Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]
            ]
        )
        return

    if event.text and event.text.startswith("http"):
        async with global_semaphore:
            raw_text = event.text.strip()
            url = raw_text.split(" -n ")[0].strip()
            custom_name = raw_text.split(" -n ")[1].strip() if " -n " in raw_text else None
            
            msg = await event.reply("🔗 **Processing URL...**")
            start_t = time.time()
            filename = None
            
            try:
                filename = await download_any_url(url, custom_name, msg, start_t)
                
                upload_result = await upload_to_r2(filename, msg)
                if isinstance(upload_result, tuple):
                    r2_url, code = upload_result
                else:
                    r2_url = upload_result
                    code = "unknown"

                await msg.edit(
                    f"✅ **Leeched & Uploaded to R2!**\n\n🎬 `{os.path.basename(filename)}`\n🔗 `{r2_url}`",
                    buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]] if code != "unknown" else None
                )
            except Exception as e: 
                await msg.edit(f"❌ Error: {e}")
            finally:
                if filename and os.path.exists(filename): os.remove(filename)
                force_system_ram_purge()

@client.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id != ADMIN_ID: return
    data = event.data.decode()
    
    if data.startswith("link_"):
        msg_id = int(data.split("_")[1])
        await event.answer("Generating Direct Link...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file:
            return await event.respond("❌ Error: File not found.")
        
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
            raw_filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
            filename = get_unique_filename(clean_double_extension(raw_filename))
            
            status = await event.respond(f"⬇️ Downloading from Telegram...")
            start_t = time.time()
            try:
                with open(filename, 'wb') as f:
                    async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                        f.write(chunk)
                        if f.tell() % (10 * 1024 * 1024) == 0: 
                            await status.edit(get_status_text("TG Down", filename, f.tell(), tg_msg.file.size, start_t))
                
                upload_result = await upload_to_r2(filename, status)
                if isinstance(upload_result, tuple):
                    r2_url, code = upload_result
                else:
                    r2_url = upload_result
                    code = "unknown"

                await status.edit(
                    f"✅ **Cloudflare R2 Complete!**\n\n🎬 `{os.path.basename(filename)}`\n🔗 `{r2_url}`",
                    buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]] if code != "unknown" else None
                )
                
            except Exception as e: 
                await status.edit(f"❌ Error: {e}")
            finally:
                if os.path.exists(filename): os.remove(filename)
                force_system_ram_purge()

# --- 12. STARTUP ---
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

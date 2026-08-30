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
from urllib.parse import quote, unquote, urlparse

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
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
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
        if default is not None: return default
        raise RuntimeError(f"❌ Missing required environment variable: {name}")
    return value

API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")

# Allow both of your previously used IDs
ADMIN_ID = int(os.environ.get("ADMIN_ID", "716887656"))
ALLOWED_USERS = [ADMIN_ID, 1053544356]

# R2
R2_ACCOUNT_ID = required_env("R2_ACCOUNT_ID").strip()
R2_ACCESS_KEY_ID = required_env("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = required_env("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = required_env("R2_BUCKET_NAME")
R2_PUBLIC_URL = required_env("R2_PUBLIC_URL").rstrip("/")

# Dashboard
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin").strip()
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "admin123").strip()

PORT = int(os.environ.get("PORT", "8000"))
KOYEB_PUBLIC_URL = os.environ.get("KOYEB_PUBLIC_URL", "").strip().rstrip("/")
KOYEB_APP_NAME = os.environ.get("KOYEB_APP_NAME", "").strip()

PUBLIC_TRACKERS = "udp://tracker.opentrackr.org:1337/announce,http://tracker.openbittorrent.com:80/announce,udp://opentracker.i2p.rocks:6969/announce"

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
    try: gc.collect()
    except: pass
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except: pass

def human_size(value):
    try: value = float(value)
    except: return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024: return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"

def get_status_text(action, filename, current, total, start_time):
    elapsed = max(time.time() - start_time, 0.001)
    current = int(current) if current else 0
    total = int(total) if total else 0
    percent = (current / total) * 100 if total else 0
    speed = current / elapsed
    blocks = min(10, int(percent // 10))
    p_bar = "■" * blocks + "□" * (10 - blocks)
    return (f"🚀 **{action}**\n📦 `{filename}`\n\n🌀 **Progress:** `[{p_bar}] {percent:.2f}%`\n⚡ **Speed:** `{human_size(speed)}/s`\n📂 **Size:** `{human_size(current)} / {human_size(total)}`")

def get_readable_time(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    res = ""
    if days: res += f"{days}d "
    if hours: res += f"{hours}h "
    if minutes: res += f"{minutes}m "
    res += f"{secs}s"
    return res

def get_largest_file(folder_path):
    largest, max_size = None, 0
    for root, _, files in os.walk(folder_path):
        for filename in files:
            path = os.path.join(root, filename)
            try: size = os.path.getsize(path)
            except: continue
            if size > max_size:
                max_size, largest = size, path
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
    if not os.path.exists(filepath): return filepath
    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"): counter += 1
    return f"{base}_{counter}{ext}"

# ============================================================
# 3. R2 CLIENT
# ============================================================
def get_r2_client():
    account_id = R2_ACCOUNT_ID.strip()
    if "://" in account_id: account_id = account_id.split("://")[-1].split(".")[0].split("/")[0]
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    r2_config = Config(region_name="auto", signature_version="s3v4", retries={"max_attempts": 10, "mode": "adaptive"}, connect_timeout=30, read_timeout=300)
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, config=r2_config)

def sync_get_smart_dashboard_data(prefix=""):
    s3 = get_r2_client()
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix, Delimiter="/")
    all_objects, common_prefixes = [], []
    for page in pages:
        if "Contents" in page: all_objects.extend(page["Contents"])
        if "CommonPrefixes" in page: common_prefixes.extend(page["CommonPrefixes"])
    hls_bases = set(os.path.dirname(obj["Key"]) for obj in all_objects if obj["Key"].endswith("master.m3u8"))
    hls_packages = {base: {"name": base, "size": 0, "date": None, "type": "HLS", "url_key": f"{base}/master.m3u8"} for base in hls_bases}
    standalone_files = []
    total_size, mp4_count = 0, 0
    sorted_bases = sorted(list(hls_bases), key=len, reverse=True)
    
    for obj in all_objects:
        key, size, date = obj["Key"], obj["Size"], obj["LastModified"]
        total_size += size
        if key.endswith("/") and size == 0: continue
        is_hls_part = False
        for base in sorted_bases:
            if key.startswith(base + "/") or key == base:
                hls_packages[base]["size"] += size
                if hls_packages[base]["date"] is None or date > hls_packages[base]["date"]: hls_packages[base]["date"] = date
                is_hls_part = True; break
        if not is_hls_part and not key.endswith("/"):
            standalone_files.append({"name": key, "size": size, "date": date, "type": "FILE", "url_key": key})
            if key.lower().endswith(".mp4"): mp4_count += 1
                
    items = list(hls_packages.values()) + standalone_files
    items.sort(key=lambda x: (x["date"] if x["date"] else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)), reverse=True)
    return {"total_size": total_size, "mp4_count": mp4_count, "hls_count": len(hls_packages), "items": items, "common_prefixes": common_prefixes}

def sync_delete_r2_file(s3_key):
    get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)

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
    old_prefix, new_prefix = old_prefix.rstrip("/") + "/", new_prefix.rstrip("/") + "/"
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
        def __init__(self): self.seen = 0; self.last = 0
        def __call__(self, amount):
            self.seen += amount
            if time.time() - self.last < 4: return
            self.last = time.time()
            try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("R2 Uploading", filename, self.seen, file_size, start_t)), loop)
            except: pass
                
    extra_args = {"ContentType": mime_type}
    if not filename.lower().endswith((".m3u8", ".ts")): extra_args["ContentDisposition"] = "inline"
    config = TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=8 * 1024 * 1024, max_concurrency=4)
    s3.upload_file(file_path, R2_BUCKET_NAME, s3_key, Callback=ProgressCallback(), ExtraArgs=extra_args, Config=config)

def sync_r2_upload_folder(folder_path, s3_prefix, loop, msg, start_t):
    s3 = get_r2_client()
    files, total_size = [], 0
    for root, _, filenames in os.walk(folder_path):
        for filename in filenames:
            path = os.path.join(root, filename)
            try: size = os.path.getsize(path)
            except: continue
            files.append(path); total_size += size
            
    class ProgressCallback:
        def __init__(self): self.seen = 0; self.last = 0; self.lock = threading.Lock()
        def __call__(self, amount):
            with self.lock:
                self.seen += amount
                if time.time() - self.last < 4: return
                self.last = time.time()
                try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("R2 HLS Sync", s3_prefix, self.seen, total_size, start_t)), loop)
                except: pass
                    
    callback = ProgressCallback()
    def upload_one(path):
        relative = os.path.relpath(path, folder_path)
        s3_key = f"{s3_prefix.strip('/')}/{relative.replace(os.sep, '/')}"
        content_type, _ = mimetypes.guess_type(path)
        extra_args = {"ContentType": content_type or "application/octet-stream"}
        if os.path.splitext(path)[1].lower() not in [".m3u8", ".ts"]: extra_args["ContentDisposition"] = "inline"
        s3.upload_file(path, R2_BUCKET_NAME, s3_key, Callback=callback, ExtraArgs=extra_args)
        
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(upload_one, files))

async def upload_to_r2(file_path, msg, target_folder=None):
    start_t, loop = time.time(), asyncio.get_running_loop()
    basename = os.path.basename(file_path)
    s3_key = f"{target_folder.strip('/')}/{basename}" if target_folder else f"{datetime.datetime.now().year}/{datetime.datetime.now().month:02d}/{datetime.datetime.now().day:02d}/{basename}"
    await msg.edit("⬆️ **Connecting to Cloudflare R2...**\n" f"🎬 `{basename}`")
    await asyncio.to_thread(sync_r2_upload, file_path, s3_key, loop, msg, start_t)
    return f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}", secrets.token_urlsafe(8)

# ============================================================
# 4. DOWNLOADERS
# ============================================================
def sync_yt_dlp_download(url, workspace, custom_name=None):
    output = os.path.join(workspace, sanitize_filename(custom_name)) if custom_name else os.path.join(workspace, "%(title)s.%(ext)s")
    ydl_opts = {"outtmpl": output, "quiet": True, "no_warnings": True, "nocheckcertificate": True, "noplaylist": True, "retries": 5, "fragment_retries": 5, "socket_timeout": 60, "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return clean_double_extension(ydl.prepare_filename(info))

def extract_gdrive_id(url):
    if not url: return None
    for pattern in [r"/file/d/([a-zA-Z0-9_-]+)", r"/d/([a-zA-Z0-9_-]+)", r"[?&]id=([a-zA-Z0-9_-]+)"]:
        match = re.search(pattern, url)
        if match: return match.group(1)
    return None

async def close_response(resp):
    try: resp.release()
    except: pass
    try: await resp.wait_for_close()
    except: pass

async def gdrive_request(session, file_id):
    urls_to_try = [
        {"url": "https://drive.usercontent.google.com/download", "params": {"id": file_id, "export": "download"}},
        {"url": "https://drive.google.com/uc", "params": {"id": file_id, "export": "download", "confirm": "t"}},
    ]
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}
    
    last_error = None
    for attempt, request_config in enumerate(urls_to_try, 1):
        try:
            async with session.get(request_config["url"], params=request_config["params"], headers=headers, allow_redirects=True, timeout=ClientTimeout(total=300)) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if response.status == 200 and "text/html" not in content_type: return response
                
                html_content = await response.text(errors="ignore")
                confirm_token = None
                for pattern in [r'name="confirm"\s+value="([^"]+)"', r'confirm=([a-zA-Z0-9_-]{10,})', r'"confirm"\s*:\s*"([^"]+)"']:
                    match = re.search(pattern, html_content, re.IGNORECASE)
                    if match: confirm_token = match.group(1); break
                
                if "quota" in html_content.lower() or "too many users" in html_content.lower(): raise RuntimeError("Google Drive quota exceeded.")
                if "permission" in html_content.lower() or "request access" in html_content.lower(): raise RuntimeError("Google Drive file is not public.")
                
                if confirm_token:
                    params_with_confirm = request_config["params"].copy()
                    params_with_confirm["confirm"] = confirm_token
                    async with session.get(request_config["url"], params=params_with_confirm, headers=headers, allow_redirects=True, timeout=ClientTimeout(total=300)) as confirm_response:
                        if confirm_response.status == 200 and "text/html" not in confirm_response.headers.get("Content-Type", "").lower(): return confirm_response
                        if "virus scan" in (await confirm_response.text(errors="ignore")).lower(): raise RuntimeError("Google Drive requires virus scan confirmation.")
                last_error = RuntimeError("Google Drive returned HTML")
        except Exception as e:
            last_error = RuntimeError(f"GDrive error: {str(e)}")
            continue
    raise last_error or RuntimeError("GDrive download failed")

async def download_direct(url, workspace, msg, start_t, custom_name=None, gdrive_id=None):
    timeout = ClientTimeout(total=None, sock_connect=60, sock_read=600)
    connector = TCPConnector(limit=16, ttl_dns_cache=300, use_dns_cache=True)
    async with ClientSession(timeout=timeout, connector=connector, cookie_jar=aiohttp.CookieJar()) as session:
        if gdrive_id:
            await msg.edit("🅿️ **Google Drive detected**\n🔄 Getting stream...")
            response = await gdrive_request(session, gdrive_id)
        else:
            for attempt in range(3):
                try:
                    response = await session.get(url, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
                    if response.status < 400: break
                    await close_response(response)
                except:
                    if attempt < 2: await asyncio.sleep(2 ** attempt)
                    continue
            else: raise RuntimeError("Download failed after retries")
            
            if "text/html" in response.headers.get("Content-Type", "").lower():
                await close_response(response)
                raise ValueError("HTML webpage detected.")

        file_size = int(response.headers.get("Content-Length") or 0)
        filename = sanitize_filename(custom_name) if custom_name else None
        if not filename:
            cd = response.headers.get("Content-Disposition", "")
            match = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.IGNORECASE) or re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
            if match: filename = sanitize_filename(match.group(1))
        
        if not filename and not gdrive_id: filename = sanitize_filename(os.path.basename(unquote(urlparse(url).path)))
        filename = (filename or "download.bin")
        if "." not in filename: filename += ".mp4"
            
        file_path = get_unique_filename(os.path.join(workspace, filename))
        await msg.edit(f"⬇️ **Downloading...**\n🎬 `{os.path.basename(file_path)}`")
        
        downloaded, last_update = 0, 0
        try:
            with open(file_path, "wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    if not chunk: continue
                    file.write(chunk)
                    downloaded += len(chunk)
                    if time.time() - last_update >= 4:
                        try: await msg.edit(get_status_text("Leeching", os.path.basename(file_path), downloaded, file_size, start_t))
                        except: pass
                        last_update = time.time()
        finally: await close_response(response)
            
        if not os.path.exists(file_path) or os.path.getsize(file_path) <= 0: raise RuntimeError("Download empty/failed.")
        return file_path

async def download_any_url(url, workspace, custom_name, msg, start_t):
    gdrive_id = extract_gdrive_id(url)
    is_zip = (custom_name and custom_name.lower().endswith(".zip")) or url.lower().split("?")[0].endswith(".zip")
    if gdrive_id or is_zip: return await download_direct(url, workspace, msg, start_t, custom_name, gdrive_id)
        
    try:
        await msg.edit("🅿️ **Extracting file information...**")
        filename = await asyncio.to_thread(sync_yt_dlp_download, url, workspace, custom_name)
        if filename and os.path.exists(filename) and os.path.getsize(filename) > 0: return filename
    except: pass
    return await download_direct(url, workspace, msg, start_t, custom_name)

# ============================================================
# 5. DASHBOARD & HTTP HANDLERS
# ============================================================
def check_dashboard_auth(request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "): return False
    try:
        user, password = base64.b64decode(auth_header.split(" ", 1)[1]).decode("utf-8").split(":", 1)
        return user == DASHBOARD_USER and password == DASHBOARD_PASS
    except: return False

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
    if (isHLS) window.location.href = "/rename_folder?old_prefix=" + encodeURIComponent(dec) + "&new_prefix=" + encodeURIComponent(newKey) + "&prefix=" + encodeURIComponent(prefix);
    else window.location.href = "/rename_file?old_key=" + encodeURIComponent(dec) + "&new_key=" + encodeURIComponent(newKey) + "&prefix=" + encodeURIComponent(prefix);
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

@routes.get("/dashboard")
async def dashboard_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Cloudflare R2 Dashboard"'}, text="🔒 Access Denied")
    prefix = request.query.get("prefix", "").strip("/")
    prefix = prefix + "/" if prefix else ""
    breadcrumbs = '<a href="/dashboard" class="breadcrumb-link">🏠 Home</a>'
    current = ""
    for part in [p for p in prefix.split("/") if p]:
        current += part + "/"
        breadcrumbs += f' <span>/</span> <a href="/dashboard?prefix={quote(current)}" class="breadcrumb-link">{html.escape(part)}</a>'
        
    try:
        data = await asyncio.to_thread(sync_get_smart_dashboard_data, prefix)
        file_rows = ""
        for pref in data.get("common_prefixes", []):
            folder_path = pref["Prefix"]
            folder_name = folder_path.rstrip("/").split("/")[-1]
            file_rows += f"""<tr><td data-val="{html.escape(folder_name)}"><a href="/dashboard?prefix={quote(folder_path)}" class="folder-link">📁 <span class="file-name">{html.escape(folder_name)}</span></a></td><td data-val="0">-</td><td data-val="0">Folder</td><td><div class="actions"><button class="btn btn-move" onclick="moveItem('{quote(folder_path)}', true, '{quote(prefix)}')">📁 Move</button><button class="btn btn-rename" onclick="renameItem('{quote(folder_path)}', true, '{quote(prefix)}')">✏️ Rename</button><button class="btn btn-delete" onclick="deleteItem('{quote(folder_path)}', true, '{quote(prefix)}')">🗑️ Delete</button></div></td></tr>"""
            
        for item in data["items"]:
            name = item["name"]
            size_str = human_size(item["size"]) if item["size"] > 0 else "-"
            date_str = item["date"].strftime("%Y-%m-%d %H:%M") if item["date"] else "-"
            url = f"{R2_PUBLIC_URL}/{quote(item['url_key'], safe='/')}"
            safe_name = html.escape(name.rstrip("/").split("/")[-1] if item["type"] == "HLS" else name.split("/")[-1])
            
            actions = f"""<button class="btn btn-copy" onclick="copyText('{html.escape(url)}')">🔗 Copy {'Master' if item['type'] == 'HLS' else 'URL'}</button><a href="{html.escape(url)}" target="_blank" class="btn btn-view">▶️ {'Play' if item['type'] == 'HLS' else 'Open'}</a><button class="btn btn-move" onclick="moveItem('{quote(name)}', {str(item['type'] == 'HLS').lower()}, '{quote(prefix)}')">📁 Move</button><button class="btn btn-rename" onclick="renameItem('{quote(name)}', {str(item['type'] == 'HLS').lower()}, '{quote(prefix)}')">✏️ Rename</button><button class="btn btn-delete" onclick="deleteItem('{quote(name)}', {str(item['type'] == 'HLS').lower()}, '{quote(prefix)}')">🗑️ Delete</button>"""
            file_rows += f"""<tr><td data-val="{safe_name}">{'📦' if item['type'] == 'HLS' else '🎬'} <span class="file-name">{'<b>' + safe_name + '</b> (HLS)' if item['type'] == 'HLS' else safe_name}</span></td><td data-val="{item['size']}">{size_str}</td><td data-val="{item['date'].timestamp() if item['date'] else 0}">{date_str}</td><td><div class="actions">{actions}</div></td></tr>"""
            
        if not file_rows: file_rows = '<tr><td colspan="4" style="text-align:center;">Directory is empty.</td></tr>'
        html_page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Cloudflare Studio</title><style>{DASHBOARD_CSS}</style><script>{DASHBOARD_JS}</script></head><body><div class="container"><h2>🛡️ Cloudflare Studio Dashboard</h2><div class="stats-grid"><div class="stat-card"><div class="stat-title">Total Storage</div><div class="stat-val">{human_size(data.get('total_size', 0))}</div></div><div class="stat-card"><div class="stat-title">HLS Packages</div><div class="stat-val">{data.get('hls_count', 0)}</div></div><div class="stat-card"><div class="stat-title">MP4 Files</div><div class="stat-val">{data.get('mp4_count', 0)}</div></div></div><div class="breadcrumbs">{breadcrumbs}</div><div class="controls"><input type="text" id="searchInput" class="search-box" onkeyup="filterTable()" placeholder="🔍 Search current folder..."><button class="btn btn-create" onclick="createFolder()">📁 Create Folder</button></div><div class="table-wrapper"><table><thead><tr><th onclick="sortTable(0, 'str')">Name</th><th onclick="sortTable(1, 'num')">Size</th><th onclick="sortTable(2, 'num')">Date Uploaded</th><th>Actions</th></tr></thead><tbody>{file_rows}</tbody></table></div></div></body></html>"""
        return web.Response(text=html_page, content_type="text/html")
    except Exception as exc: return web.Response(text=f"<h2>Error</h2><pre>{html.escape(str(exc))}</pre>", content_type="text/html", status=500)

@routes.get("/delete_file")
async def web_delete_file(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    if key := request.query.get("key"):
        try: await asyncio.to_thread(sync_delete_r2_file, key); force_system_ram_purge()
        except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("curr_prefix", "")))

@routes.get("/delete_folder")
async def web_delete_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    if prefix := request.query.get("prefix"):
        try: await asyncio.to_thread(sync_delete_r2_folder, prefix); force_system_ram_purge()
        except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("curr_prefix", "")))

@routes.get("/rename_file")
async def web_rename_file(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key, new_key = request.query.get("old_key"), request.query.get("new_key")
    if old_key and new_key and old_key != new_key:
        try: await asyncio.to_thread(sync_rename_r2_file, old_key, new_key); force_system_ram_purge()
        except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("prefix", "")))

@routes.get("/rename_folder")
async def web_rename_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_prefix, new_prefix = request.query.get("old_prefix"), request.query.get("new_prefix")
    if old_prefix and new_prefix and old_prefix != new_prefix:
        try: await asyncio.to_thread(sync_rename_r2_folder, old_prefix, new_prefix); force_system_ram_purge()
        except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("prefix", "")))

@routes.get("/move_item")
async def web_move_item(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key, target = request.query.get("old_key"), request.query.get("target_folder", "").strip().strip("/")
    item_type = request.query.get("type", "FILE")
    if old_key is not None:
        basename = old_key.rstrip("/").split("/")[-1]
        new_key = f"{target}/{basename}/" if item_type in ("HLS", "FOLDER") else (f"{target}/{basename}" if target else basename)
        if old_key != new_key:
            try:
                if item_type in ("HLS", "FOLDER"): await asyncio.to_thread(sync_rename_r2_folder, old_key, new_key)
                else: await asyncio.to_thread(sync_rename_r2_file, old_key, new_key)
                force_system_ram_purge()
            except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("prefix", "")))

@routes.get("/create_folder")
async def web_create_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    if path := request.query.get("path"):
        try:
            s3 = get_r2_client()
            s3.put_object(Bucket=R2_BUCKET_NAME, Key=path.strip("/") + "/")
        except: pass
    raise web.HTTPFound("/dashboard")

@routes.get("/health")
async def health_handler(request):
    return web.json_response({"status": "ok", "service": "telegram-r2-bot", "timestamp": datetime.datetime.utcnow().isoformat(), "checks": {"telegram": "connected" if client.is_connected() else "disconnected", "r2": "connected"}})

@routes.get("/")
async def root_handler(request):
    return web.Response(text="<html><body style='background:#0f172a; color:#38bdf8; text-align:center; padding-top:120px; font-family:sans-serif;'><h1>✅ System Online</h1><p><a href='/dashboard' style='color:#0f172a; background:#34d399; padding:12px 20px; text-decoration:none; border-radius:6px;'>Dashboard</a></p></body></html>", content_type="text/html")

@routes.get("/{code}/{filename}")
async def stream_handler(request):
    code = request.match_info["code"]
    data = link_storage.get(code)
    if not data: return web.Response(text="Expired", status=410)
    msg, filename = data.get("msg"), sanitize_filename(request.match_info.get("filename", "video.mp4"))
    if not msg: return web.Response(text="File unavailable", status=410)
    file_size = msg.file.size if msg.file else 0
    range_header, start, end = request.headers.get("Range"), 0, file_size - 1
    if range_header:
        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            if match.group(1): start = int(match.group(1))
            if match.group(2): end = int(match.group(2))
            if end >= file_size: end = file_size - 1
    if start >= file_size: return web.Response(status=416, headers={"Content-Range": f"bytes */{file_size}"})
    headers = {"Content-Disposition": f'inline; filename="{filename}"', "Accept-Ranges": "bytes", "Content-Type": mimetypes.guess_type(filename)[0] or "application/octet-stream", "Content-Length": str(end - start + 1)}
    if range_header: headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await response.prepare(request)
    try:
        offset = (start // 1048576) * 1048576
        skipped, remaining = start - offset, end - start + 1
        async for chunk in client.iter_download(msg.media, offset=offset, request_size=1048576):
            if skipped: chunk = chunk[skipped:]; skipped = 0
            if not chunk: continue
            if len(chunk) > remaining: chunk = chunk[:remaining]
            await response.write(chunk)
            remaining -= len(chunk)
            if remaining <= 0: break
    except: pass
    try: await response.write_eof()
    except: pass
    return response

# ============================================================
# 16. TELEGRAM MASTER HANDLER
# ============================================================
@client.on(events.NewMessage(incoming=True))
async def master_handler(event):
    if event.sender_id not in ALLOWED_USERS:
        print(f"Ignored unauthorized user: {event.sender_id}")
        return

    # 🚀 FIX: RESTORED /START COMMAND
    if event.text and event.text.startswith('/start'):
        await event.reply("⚡ **Telegram R2 Leech Bot is ONLINE!**\n\n1️⃣ Send an HTTP or Google Drive link.\n2️⃣ Forward a Telegram File.")
        return

    if event.file:
        await event.reply(f"📂 **File Detected:** `{event.file.name or 'file.bin'}`", buttons=[
            [Button.inline("🔗 Generate Direct Link", data=f"link_{event.id}")],
            [Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]
        ])
        return
        
    if event.text and (event.text.startswith("http") or event.text.startswith("magnet:?")):
        async with global_semaphore:
            raw = event.text.strip()
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
                if not final_path or not os.path.exists(final_path): raise ValueError("Download failed.")
                filename = os.path.basename(final_path)
                
                if filename.lower().endswith(".zip"):
                    await msg.edit("📦 **Extracting HLS ZIP Archive...**")
                    extract_dir = os.path.join(workspace, "extracted"); os.makedirs(extract_dir, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(final_path, "r").extractall(extract_dir))
                    os.remove(final_path)
                    
                    project_name = os.path.splitext(filename)[0]
                    extracted_items = os.listdir(extract_dir)
                    if len(extracted_items) == 1 and os.path.isdir(os.path.join(extract_dir, extracted_items[0])):
                        upload_source_dir, s3_prefix = os.path.join(extract_dir, extracted_items[0]), target_folder or extracted_items[0]
                    else:
                        upload_source_dir, s3_prefix = extract_dir, target_folder or project_name
                        
                    await msg.edit(f"⬆️ **Uploading HLS Pack to R2...**\n📂 `{s3_prefix}`")
                    await asyncio.to_thread(sync_r2_upload_folder, upload_source_dir, s3_prefix, asyncio.get_running_loop(), msg, time.time())
                    master_url = f"{R2_PUBLIC_URL}/{quote(s3_prefix, safe='/')}/master.m3u8"
                    await msg.edit(f"✅ **HLS Uploaded to R2!**\n\n🎬 `{project_name}`\n📺 **Stream Link:**\n`{master_url}`", link_preview=False)
                else:
                    r2_url, code = await upload_to_r2(final_path, msg, target_folder)
                    await msg.edit(f"✅ **Leeched & Uploaded!**\n\n🎬 `{filename}`\n🔗 `{r2_url}`", buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]], link_preview=False)
            except Exception as exc: await msg.edit(f"❌ **Error:** `{exc}`")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                free_memory()

@client.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id not in ALLOWED_USERS: return
    data = event.data.decode("utf-8", errors="ignore")
    
    if data.startswith("delr2_"):
        code = data.split("_", 1)[1]
        item = link_storage.get(code)
        if item and item.get("s3_key"):
            await event.answer("Deleting...", alert=False)
            try:
                await asyncio.to_thread(sync_delete_r2_file, item["s3_key"])
                link_storage.pop(code, None)
                await event.edit(f"🗑️ **File Deleted from R2!**\nKey: `{item['s3_key']}`")
            except Exception as exc: await event.edit(f"❌ Delete Error: {exc}")
        return
        
    if data.startswith("link_"):
        msg_id = int(data.split("_", 1)[1])
        await event.answer("Generating Direct Link...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file: return await event.respond("❌ Error: File not found.")
        code = secrets.token_urlsafe(8)
        link_storage[code] = {"msg": tg_msg, "timestamp": time.time()}
        base = KOYEB_PUBLIC_URL or (f"https://{KOYEB_APP_NAME}.koyeb.app" if KOYEB_APP_NAME else "")
        link = f"{base}/{code}/{quote(sanitize_filename(tg_msg.file.name or 'video.mp4'))}"
        await event.respond(f"🚀 **Direct Link:**\n`{link}`\n\n💡 *Valid for 24 hours.*")
        return
        
    if data.startswith("r2_"):
        msg_id = int(data.split("_", 1)[1])
        await event.answer("Uploading...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file: return await event.respond("❌ File not found.")
            
        async with global_semaphore:
            workspace = f"dl_{uuid.uuid4().hex[:8]}"; os.makedirs(workspace, exist_ok=True)
            filename = os.path.basename(get_unique_filename(os.path.join(workspace, sanitize_filename(tg_msg.file.name or "video.mp4"))))
            file_path, status, start_t = os.path.join(workspace, filename), await event.respond("⬇️ **Downloading from Telegram...**"), time.time()
            try:
                with open(file_path, "wb") as file:
                    downloaded = 0
                    async for chunk in client.iter_download(tg_msg.media, request_size=1024 * 1024):
                        file.write(chunk)
                        downloaded += len(chunk)
                        if time.time() - start_t > 3 and downloaded % (10 * 1024 * 1024) < len(chunk):
                            try: await status.edit(get_status_text("TG Download", filename, downloaded, tg_msg.file.size, start_t))
                            except: pass
                            
                if filename.lower().endswith(".zip"):
                    await status.edit("📦 **Extracting HLS ZIP Archive...**")
                    extract_dir = os.path.join(workspace, "extracted"); os.makedirs(extract_dir, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(file_path, "r").extractall(extract_dir))
                    os.remove(file_path)
                    project = os.path.splitext(filename)[0]
                    items = os.listdir(extract_dir)
                    source_dir, prefix = (os.path.join(extract_dir, items[0]), items[0]) if len(items) == 1 and os.path.isdir(os.path.join(extract_dir, items[0])) else (extract_dir, project)
                    await status.edit(f"⬆️ **Uploading HLS...**\n📂 `{prefix}`")
                    await asyncio.to_thread(sync_r2_upload_folder, source_dir, prefix, asyncio.get_running_loop(), status, time.time())
                    await status.edit(f"✅ **HLS Uploaded!**\n🎬 `{project}`\n📺 **Stream Link:**\n`{R2_PUBLIC_URL}/{quote(prefix, safe='/')}/master.m3u8`", link_preview=False)
                else:
                    r2_url, code = await upload_to_r2(file_path, status)
                    await status.edit(f"✅ **Cloudflare R2 Complete!**\n🎬 `{filename}`\n🔗 `{r2_url}`", buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]], link_preview=False)
            except Exception as exc:
                try: await status.edit(f"❌ **Error:** `{exc}`")
                except: pass
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                free_memory()

# ============================================================
# 18. STARTUP
# ============================================================
async def main():
    print("====================================")
    print("Telegram R2 Leech Bot Starting...")
    print("====================================")
    
    app = web.Application(client_max_size=1024 ** 3)
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    try:
        print("Connecting to Telegram...")
        # Start without throwing errors immediately on FloodWait
        while True:
            try:
                await client.start(bot_token=BOT_TOKEN)
                break
            except FloodWaitError as e:
                print(f"⚠️ Telegram FloodWait Block: Waiting {e.seconds} seconds.")
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                print(f"⚠️ Connection error: {e}. Retrying...")
                await asyncio.sleep(10)
                
        print("✅ BOT IS ONLINE!")
        await client.run_until_disconnected()
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())

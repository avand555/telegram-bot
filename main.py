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
import ctypes
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote, urlparse

# ============================================================
# MIME TYPES & ASYNC SETUP
# ============================================================
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")

import nest_asyncio
nest_asyncio.apply()

# ============================================================
# TELEGRAM / WEB / STORAGE LIBRARIES
# ============================================================
from telethon import TelegramClient, events, Button, types, utils
from telethon.network import ConnectionTcpFull
from telethon.errors import FloodWaitError
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest, GetFileRequest
from telethon.tl.types import InputFileBig, InputFile

from aiohttp import web, ClientSession, ClientTimeout, TCPConnector
import aiohttp
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
import yt_dlp

# ============================================================
# 1. CONFIGURATION
# ============================================================

def required_env(name, default=None):
    value = os.environ.get(name, "").strip()
    if not value:
        if default is not None: return default
        raise RuntimeError(f"❌ Missing environment variable: {name}")
    return value

API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")

# ALLOWED ADMINS
ADMIN_IDS = [716887656, 2094838510]

# CLOUDFLARE R2
R2_ACCOUNT_ID = required_env("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = required_env("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = required_env("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = required_env("R2_BUCKET_NAME")
R2_PUBLIC_URL = required_env("R2_PUBLIC_URL").rstrip("/")

# DASHBOARD AUTH
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin").strip()
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "admin123").strip()

# KOYEB
PORT = int(os.environ.get("PORT", "8000"))
KOYEB_PUBLIC_URL = os.environ.get("KOYEB_PUBLIC_URL", "").strip().rstrip("/")
KOYEB_APP_NAME = os.environ.get("KOYEB_APP_NAME", "").strip()

PUBLIC_TRACKERS = "udp://tracker.opentrackr.org:1337/announce,http://tracker.openbittorrent.com:80/announce,udp://opentracker.i2p.rocks:6969/announce"

# Semaphores
global_semaphore = asyncio.Semaphore(4)
active_tasks = {}
link_storage = {}
routes = web.RouteTableDef()

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
# 2. SYSTEM HELPERS & MEMORY
# ============================================================

def force_system_ram_purge():
    """Aggressively releases RAM back to Koyeb OS."""
    gc.collect()
    try:
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
    percent = (current / total) * 100 if total else 0
    speed = current / elapsed
    done = min(10, int(percent // 10))
    p_bar = "■" * done + "□" * (10 - done)
    return (f"🚀 **{action}**\n📦 `{filename}`\n\n"
            f"🌀 **Progress:** `[{p_bar}] {percent:.2f}%`\n"
            f"⚡ **Speed:** `{human_size(speed)}/s`\n"
            f"📂 **Size:** `{human_size(current)} / {human_size(total)}`")

def format_saas_progress(action, filename, percent, downloaded, total, speed, eta, cn, elapsed, task_code):
    done = int(percent // 10)
    p_bar = "●" * done + ("◔" if percent % 10 >= 5 else "")
    p_bar += "○" * (10 - len(p_bar))
    return (f"🧲 **{action}...**\n╭ `[{p_bar[:10]}]` » `{percent}%`\n"
            f"├ **Processed:** `{downloaded} of {total}`\n├ **Speed:** `{speed}`\n"
            f"├ **ETA:** `{eta}`\n├ **Peers:** `{cn}`\n├ **Elapsed:** `{elapsed}`\n"
            f"╰ **Cancel:** `/c_{task_code}`")

def clean_double_extension(filename):
    while filename.lower().endswith((".mp4.mp4", ".mkv.mkv", ".zip.zip", ".webm.webm")):
        filename = filename.rsplit(".", 1)[0]
    return filename

def sanitize_filename(filename):
    filename = unquote(filename or "")
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    return clean_double_extension(filename.strip().strip("."))

def get_unique_filename(filepath):
    if not os.path.exists(filepath): return filepath
    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"): counter += 1
    return f"{base}_{counter}{ext}"

# ============================================================
# 3. R2 ENGINE
# ============================================================

def get_r2_client():
    account_id = R2_ACCOUNT_ID.strip()
    if "://" in account_id: account_id = account_id.split("://")[-1].split(".")[0].split("/")[0]
    endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    r2_config = Config(region_name="auto", signature_version="s3v4", retries={"max_attempts": 10, "mode": "adaptive"})
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, config=r2_config)

def sync_get_smart_dashboard_data(prefix=""):
    s3 = get_r2_client()
    paginator = s3.get_paginator("list_objects_v2")
    all_objects, common_prefixes = [], []
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix, Delimiter="/"):
        if "Contents" in page: all_objects.extend(page["Contents"])
        if "CommonPrefixes" in page: common_prefixes.extend(page["CommonPrefixes"])
    hls_bases = set(os.path.dirname(obj["Key"]) for obj in all_objects if obj["Key"].endswith("master.m3u8"))
    hls_packages = {base: {"name": base, "size": 0, "date": None, "type": "HLS", "url_key": f"{base}/master.m3u8"} for base in hls_bases}
    standalone_files, total_size, mp4_count = [], 0, 0
    sorted_bases = sorted(list(hls_bases), key=len, reverse=True)
    for obj in all_objects:
        key, size, date = obj["Key"], obj["Size"], obj["LastModified"]
        total_size += size
        if key.endswith("/") and size == 0: continue
        is_hls = False
        for base in sorted_bases:
            if key.startswith(base + "/"):
                hls_packages[base]["size"] += size
                if hls_packages[base]["date"] is None or date > hls_packages[base]["date"]: hls_packages[base]["date"] = date
                is_hls = True; break
        if not is_hls:
            standalone_files.append({"name": key, "size": size, "date": date, "type": "FILE", "url_key": key})
            if key.lower().endswith(".mp4"): mp4_count += 1
    items = list(hls_packages.values()) + standalone_files
    items.sort(key=lambda x: (x["date"] if x["date"] else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)), reverse=True)
    return {"total_size": total_size, "mp4_count": mp4_count, "hls_count": len(hls_packages), "items": items, "common_prefixes": common_prefixes}

def sync_r2_upload(file_path, s3_key, loop, msg, start_t):
    s3 = get_r2_client()
    f_size = os.path.getsize(file_path)
    mime, _ = mimetypes.guess_type(file_path)
    class CB:
        def __init__(self): self.seen = 0; self.last = 0
        def __call__(self, n):
            self.seen += n
            if time.time() - self.last > 4:
                self.last = time.time()
                try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("R2 Uploading", os.path.basename(file_path), self.seen, f_size, start_t)), loop)
                except: pass
    args = {"ContentType": mime or "video/mp4"}
    if not file_path.lower().endswith((".m3u8", ".ts")): args["ContentDisposition"] = "inline"
    s3.upload_file(file_path, R2_BUCKET_NAME, s3_key, Callback=CB(), ExtraArgs=args, Config=TransferConfig(multipart_threshold=8*1024*1024, max_concurrency=4))

def sync_r2_folder_upload(folder, s3_prefix, loop, msg, start_t):
    s3 = get_r2_client()
    files = [os.path.join(r, f) for r, _, fs in os.walk(folder) for f in fs]
    total = sum(os.path.getsize(f) for f in files)
    class CB:
        def __init__(self): self.seen = 0; self.last = 0; self.lock = threading.Lock()
        def __call__(self, n):
            with self.lock:
                self.seen += n
                if time.time() - self.last > 4:
                    self.last = time.time()
                    try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("HLS Syncing", s3_prefix, self.seen, total, start_t)), loop)
                    except: pass
    cb = CB()
    def up_one(path):
        key = f"{s3_prefix.strip('/')}/{os.path.relpath(path, folder).replace(os.sep, '/')}"
        mime, _ = mimetypes.guess_type(path)
        args = {"ContentType": mime or "application/octet-stream"}
        if os.path.splitext(path)[1].lower() not in [".m3u8", ".ts"]: args["ContentDisposition"] = "inline"
        s3.upload_file(path, R2_BUCKET_NAME, key, Callback=cb, ExtraArgs=args)
    with ThreadPoolExecutor(max_workers=15) as ex: list(ex.map(up_one, files))

async def upload_to_r2(path, msg, target=None):
    start_t, loop, bn = time.time(), asyncio.get_running_loop(), os.path.basename(path)
    s_key = f"{target.strip('/')}/{bn}" if target else f"{datetime.datetime.now().year}/{datetime.datetime.now().month:02d}/{datetime.datetime.now().day:02d}/{bn}"
    await msg.edit(f"⬆️ **Connecting to R2...**\n🎬 `{bn}`")
    await asyncio.to_thread(sync_r2_upload, path, s_key, loop, msg, start_t)
    return f"{R2_PUBLIC_URL}/{quote(s_key, safe='/')}", s_key

# ============================================================
# 4. DOWNLOADERS (GDRIVE / TORRENT / DIRECT)
# ============================================================

async def download_magnet(url, workspace, custom_name, msg, start_t):
    aria_cmd = shutil.which("aria2c") or os.path.abspath("./aria2c")
    if not shutil.which("aria2c") and not os.path.exists("./aria2c"):
        await msg.edit("📥 Downloading aria2c binary...")
        subprocess.run("wget -qO- https://github.com/P3TERX/aria2-builder/releases/download/1.36.0/aria2-1.36.0-static-linux-amd64.tar.gz | tar -xz", shell=True)
    
    cmd = [aria_cmd, "--seed-time=0", "--max-connection-per-server=16", "--split=16", "--summary-interval=3", "--bt-stop-timeout=120", f"--bt-tracker={PUBLIC_TRACKERS}", f"--dir={workspace}", url]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    t_code = secrets.token_urlsafe(8)
    active_tasks[t_code] = {"process": proc, "cancel_event": asyncio.Event(), "dir": workspace}
    
    last_up = 0
    while True:
        line = (await proc.stdout.readline()).decode("utf-8", errors="ignore").strip()
        if not line and proc.returncode is not None: break
        match = aria_re.search(line)
        if match and time.time() - last_up > 4:
            f_active = "Fetching..."
            for _, _, fs in os.walk(workspace):
                for f in fs:
                    if not f.endswith(".aria2"): f_active = f; break
            p_txt = format_saas_progress("Download", f_active, int(match.group("percent") or 0), match.group("downloaded"), match.group("total"), match.group("speed")+"/s", match.group("eta") or "Calc...", match.group("cn"), get_readable_time(time.time()-start_t), t_code)
            try: await msg.edit(p_txt, buttons=[[Button.inline("❌ Cancel", data=f"canceltask_{t_code}")]]); last_up = time.time()
            except: pass
    await proc.wait(); active_tasks.pop(t_code, None)
    large = get_largest_file(workspace)
    if not large: raise ValueError("Torrent failed.")
    final = os.path.join(workspace, sanitize_filename(custom_name or os.path.basename(large)))
    if os.path.abspath(large) != os.path.abspath(final): shutil.move(large, final)
    return final

async def get_gdrive_stream(session, f_id):
    base = "https://drive.usercontent.google.com/download"
    params = {'id': f_id, 'confirm': 't'}
    r = await session.get(base, params=params, allow_redirects=True)
    if "text/html" in r.headers.get("Content-Type", ""):
        html_c = await r.text()
        token = re.search(r'confirm=([a-zA-Z0-9_-]+)', html_c)
        if token:
            r.close()
            params['confirm'] = token.group(1)
            r = await session.get(base, params=params, allow_redirects=True)
    return r

async def download_direct(url, workspace, msg, start_t, custom_name=None):
    g_id = re.search(r'(?:file/d/|id=|/d/)([a-zA-Z0-9_-]{25,})', url)
    g_id = g_id.group(1) if g_id else None
    timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
    async with ClientSession(timeout=timeout) as sess:
        r = await get_gdrive_stream(sess, g_id) if g_id else await sess.get(url, allow_redirects=True)
        if r.status != 200: raise ValueError(f"HTTP {r.status}")
        f_size = int(r.headers.get("Content-Length") or 0)
        fname = custom_name or sanitize_filename(re.search(r'filename="?([^";]+)"?', r.headers.get("Content-Disposition", "")).group(1) if "Content-Disposition" in r.headers else os.path.basename(urlparse(url).path)) or "video.mp4"
        if "." not in fname: fname += ".mp4"
        fpath = get_unique_filename(os.path.join(workspace, fname))
        await msg.edit(f"⬇️ **Downloading...**\n🎬 `{os.path.basename(fpath)}`")
        with open(fpath, "wb") as f:
            last = 0
            async for chunk in r.content.iter_chunked(1024*1024):
                f.write(chunk)
                if time.time() - last > 4:
                    try: await msg.edit(get_status_text("Leeching", os.path.basename(fpath), f.tell(), f_size, start_t))
                    except: pass
                    last = time.time()
        r.close()
    return fpath

async def download_any_url(url, workspace, custom_name, msg, start_t):
    if url.lower().startswith("magnet:?"): return await download_magnet(url, workspace, custom_name, msg, start_t)
    if "drive.google.com" in url or url.lower().endswith(".zip"): return await download_direct(url, workspace, msg, start_t, custom_name)
    try:
        await msg.edit("🅿️ **Extracting file info...**")
        ydl_opts = {"outtmpl": os.path.join(workspace, custom_name or "%(title)s.%(ext)s"), "quiet": True, "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return clean_double_extension(ydl.prepare_filename(info))
    except: pass
    return await download_direct(url, workspace, msg, start_t, custom_name)

# ============================================================
# 5. DASHBOARD UI
# ============================================================

@routes.get('/dashboard')
async def dashboard_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="Dashboard"'}, text="Unauthorized")
    prefix = request.query.get("prefix", "").strip("/")
    prefix = prefix + "/" if prefix else ""
    data = await asyncio.to_thread(sync_get_smart_dashboard_data, prefix)
    file_rows = ""
    for pref in data.get("common_prefixes", []):
        p = pref["Prefix"]; n = p.rstrip("/").split("/")[-1]
        file_rows += f"""<tr style="background:rgba(167,139,250,.05)"><td><a href="/dashboard?prefix={quote(p)}" style="color:#fbbf24;text-decoration:none;font-weight:bold">📁 {n}/</a></td><td>-</td><td>Folder</td><td><div class="actions"><button class="btn btn-delete" onclick="deleteItem('{quote(p)}',true,'{quote(prefix)}')">🗑️ Delete</button></div></td></tr>"""
    for item in data["items"]:
        n, s, d, t, uk = item["name"], human_size(item["size"]), item["date"], item["type"], item["url_key"]
        url = f"{R2_PUBLIC_URL}/{quote(uk, safe='/')}"
        disp = n.rstrip("/").split("/")[-1]
        actions = f"""<button class="btn btn-copy" onclick="copyText('{url}')">🔗 Copy</button><a href="{url}" target="_blank" class="btn btn-view">▶️ Play</a><button class="btn btn-move" onclick="moveItem('{quote(n)}',{str(t=='HLS').lower()},'{quote(prefix)}')">📁 Move</button><button class="btn btn-delete" onclick="deleteItem('{quote(n)}',{str(t=='HLS').lower()},'{quote(prefix)}')">🗑️ Delete</button>"""
        file_rows += f"""<tr><td>{'📦' if t=='HLS' else '🎬'} <span class="file-name">{disp}</span></td><td>{s}</td><td>{d.strftime("%Y-%m-%d %H:%M") if d else "-"}</td><td><div class="actions">{actions}</div></td></tr>"""
    
    html_page = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>R2 Studio</title><style>{DASHBOARD_CSS}</style><script>{DASHBOARD_JS}</script></head><body><div class="container"><h2>🛡️ R2 Studio</h2><div class="stats-grid"><div class="stat-card">Storage: {human_size(data['total_size'])}</div><div class="stat-card">HLS: {data['hls_count']}</div><div class="stat-card">MP4: {data['mp4_count']}</div></div><div class="controls"><input type="text" id="searchInput" class="search-box" onkeyup="filterTable()" placeholder="Search..."><button class="btn btn-create" onclick="createFolder()">📁 New Folder</button></div><div class="table-wrapper"><table><tr><th>Name</th><th>Size</th><th>Date</th><th>Actions</th></tr>{file_rows}</table></div></div></body></html>"""
    return web.Response(text=html_page, content_type="text/html")

# ============================================================
# 6. DASHBOARD ACTIONS
# ============================================================

@routes.get("/delete_file")
async def web_delete_file(request):
    if not check_dashboard_auth(request): return web.Response(status=401)
    if key := request.query.get("key"):
        try: await asyncio.to_thread(sync_delete_r2_file, key); force_system_ram_purge()
        except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("curr_prefix", "")))

@routes.get("/delete_folder")
async def web_delete_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401)
    if pref := request.query.get("prefix"):
        try: await asyncio.to_thread(sync_delete_r2_folder, pref); force_system_ram_purge()
        except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("curr_prefix", "")))

@routes.get("/move_item")
async def web_move_item(request):
    if not check_dashboard_auth(request): return web.Response(status=401)
    ok, target, t = request.query.get("old_key"), request.query.get("target_folder", "").strip("/"), request.query.get("type", "FILE")
    if ok is not None:
        bn = ok.rstrip("/").split("/")[-1]
        nk = f"{target}/{bn}/" if t == "FOLDER" else (f"{target}/{bn}" if target else bn)
        try:
            if t == "FOLDER": await asyncio.to_thread(sync_rename_r2_folder, ok, nk)
            else: await asyncio.to_thread(sync_rename_r2_file, ok, nk)
            force_system_ram_purge()
        except: pass
    raise web.HTTPFound("/dashboard?prefix=" + quote(request.query.get("prefix", "")))

@routes.get("/create_folder")
async def web_create_folder(request):
    if not check_dashboard_auth(request): return web.Response(status=401)
    if p := request.query.get("path"):
        try: get_r2_client().put_object(Bucket=R2_BUCKET_NAME, Key=p.strip("/") + "/")
        except: pass
    raise web.HTTPFound("/dashboard")

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

@routes.get('/')
async def root_handler(request):
    return web.Response(text="<html><body style='background:#0f172a; color:#38bdf8; text-align:center; padding-top:120px; font-family:sans-serif;'><h1>✅ System Online</h1><p><a href='/dashboard' style='color:#0f172a; background:#34d399; padding:12px 20px; text-decoration:none; border-radius:6px;'>Enter Dashboard</a></p></body></html>", content_type="text/html")

@routes.get("/health")
async def health_handler(request):
    return web.json_response({"status": "ok", "checks": {"telegram": "connected" if client.is_connected() else "disconnected", "r2": "connected"}})

# ============================================================
# 7. TELEGRAM MASTER HANDLER
# ============================================================

@client.on(events.NewMessage(incoming=True, func=lambda e: e.sender_id in ADMIN_IDS))
async def master_handler(event):
    if event.text == '/start':
        await event.reply("⚡ **Indestructible R2 Engine Online!**\nSend link or forward file.")
        return

    if event.file:
        await event.reply(f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`", buttons=[
            [Button.inline("🔗 Direct Link", data=f"link_{event.id}")],
            [Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]
        ])
        return
        
    if event.text and (event.text.startswith("http") or event.text.startswith("magnet:?")):
        async with global_semaphore:
            raw = event.text.strip()
            u, n, f = raw, None, None
            nm = re.search(r"\s+-n\s+(.+?)(?=\s+-f\s+|$)", raw, re.I)
            if nm: n = nm.group(1).strip()
            fm = re.search(r"\s+-f\s+(.+)$", raw, re.I)
            if fm: f = fm.group(1).strip()
            u = re.split(r"\s+-n\s+|\s+-f\s+", raw, maxsplit=1, flags=re.I)[0].strip()

            msg = await event.reply("🔗 **Processing...**")
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            try:
                final = await download_any_url(u, workspace, n, msg, time.time())
                if not final: raise ValueError("Failed.")
                fn = os.path.basename(final)

                if fn.lower().endswith(".zip"):
                    await msg.edit("📦 **Extracting HLS ZIP...**")
                    ex = os.path.join(workspace, "extracted"); os.makedirs(ex, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(final, "r").extractall(ex))
                    os.remove(final)
                    items = os.listdir(ex)
                    s_dir, pref = (os.path.join(ex, items[0]), items[0]) if len(items)==1 and os.path.isdir(os.path.join(ex, items[0])) else (ex, f or os.path.splitext(fn)[0])
                    await msg.edit(f"⬆️ **Uploading HLS: {pref}**")
                    await asyncio.to_thread(sync_r2_upload_folder, s_dir, pref, asyncio.get_running_loop(), msg, time.time())
                    await msg.edit(f"✅ **HLS Complete!**\n🔗 `{R2_PUBLIC_URL}/{quote(pref, safe='/')}/master.m3u8`", link_preview=False)
                else:
                    url, code = await upload_to_r2(final, msg, f)
                    await msg.edit(f"✅ **R2 Complete!**\n🎬 `{fn}`\n🔗 `{url}`", link_preview=False)
            except Exception as e: await msg.edit(f"❌ Error: {e}")
            finally: shutil.rmtree(workspace, ignore_errors=True); force_system_ram_purge()

@client.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id not in ADMIN_IDS: return
    d = event.data.decode("utf-8")
    
    if d.startswith("link_"):
        m_id = int(d.split("_")[1]); await event.answer("Generating...")
        m = await client.get_messages(event.chat_id, ids=m_id)
        if not m or not m.file: return
        code = secrets.token_urlsafe(8); link_storage[code] = {"msg": m, "timestamp": time.time()}
        base = KOYEB_PUBLIC_URL or (f"https://{KOYEB_APP_NAME}.koyeb.app" if KOYEB_APP_NAME else "")
        link = f"{base}/{code}/{quote(sanitize_filename(m.file.name or 'video.mp4'))}"
        await event.respond(f"🚀 **Direct Link:**\n`{link}`")
        
    if d.startswith("r2_"):
        m_id = int(d.split("_")[1]); await event.answer("Starting...")
        m = await client.get_messages(event.chat_id, ids=m_id)
        async with global_semaphore:
            workspace = f"dl_{uuid.uuid4().hex[:8]}"; os.makedirs(workspace, exist_ok=True)
            fn = sanitize_filename(m.file.name or "video.mp4"); fpath = os.path.join(workspace, fn)
            status = await event.respond("⬇️ **Downloading...**")
            try:
                # RELIABLE RESUMABLE DOWNLOAD
                await robust_tg_download(client, m.media, fpath, status, fn, m.file.size, time.time())
                if fn.lower().endswith(".zip"):
                    ex = os.path.join(workspace, "ex"); os.makedirs(ex, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(fpath, "r").extractall(ex))
                    os.remove(fpath); items = os.listdir(ex)
                    s_dir, pref = (os.path.join(ex, items[0]), items[0]) if len(items)==1 else (ex, os.path.splitext(fn)[0])
                    await status.edit(f"⬆️ **Uploading HLS: {pref}**")
                    await asyncio.to_thread(sync_r2_upload_folder, s_dir, pref, asyncio.get_running_loop(), status, time.time())
                    await status.edit(f"✅ HLS Done!\n`{R2_PUBLIC_URL}/{quote(pref, safe='/')}/master.m3u8`", link_preview=False)
                else:
                    url, code = await upload_to_r2(fpath, status)
                    await status.edit(f"✅ R2 Complete!\n🎬 `{fn}`\n🔗 `{url}`", link_preview=False)
            except Exception as e: await status.edit(f"❌ Error: {e}")
            finally: shutil.rmtree(workspace, ignore_errors=True); force_system_ram_purge()

async def robust_tg_download(c, med, path, msg, fn, size, st):
    down = 0; chunk = 1048576; retries = 0; last = 0
    while down < size:
        try:
            async for c_data in c.iter_download(med, offset=down, request_size=chunk):
                with open(path, "ab") as f: f.write(c_data)
                down += len(c_data)
                if time.time() - last > 4:
                    try: await msg.edit(get_status_text("TG Download", fn, down, size, st))
                    except: pass
                    last = time.time()
            break
        except:
            retries += 1
            if retries >= 15: raise
            await asyncio.sleep(2)

# CSS/JS Strings
DASHBOARD_CSS = ":root{--bg:#0f172a;--card:#1e293b;--text:#f8fafc;--muted:#94a3b8;--accent:#38bdf8;--border:#334155}body{font-family:'Segoe UI',sans-serif;background:var(--bg);color:var(--text);margin:0;padding:20px}.container{max-width:1200px;margin:auto}.header-bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}h2{margin:0;color:var(--text);font-size:24px}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;margin-bottom:25px}.stat-card{background:var(--card);padding:15px;border-radius:10px;border:1px solid var(--border)}.search-box{flex-grow:1;padding:12px;border-radius:8px;border:1px solid var(--border);background:var(--card);color:var(--text)}.table-wrapper{overflow-x:auto;background:var(--card);border-radius:10px;border:1px solid var(--border)}table{width:100%;border-collapse:collapse;min-width:900px}th{background:#0f172a;color:var(--muted);padding:16px;text-align:left}td{padding:16px;border-bottom:1px solid var(--border);word-break:break-all}.actions{display:flex;gap:8px}.btn{padding:8px 14px;border-radius:6px;border:none;font-size:12px;font-weight:600;cursor:pointer;text-decoration:none}.btn-copy{background:rgba(56,189,248,.1);color:var(--accent)}.btn-view{background:rgba(16,185,129,.1);color:#34d399}.btn-delete{background:rgba(244,63,94,.1);color:#fb7185}.breadcrumbs{margin-bottom:15px}.breadcrumb-link{color:var(--accent);text-decoration:none}"
DASHBOARD_JS = "function copyText(t){navigator.clipboard.writeText(t);alert('✅ Copied!');}function deleteItem(k,h,p){if(confirm('Delete?')){window.location.href=(h?'/delete_folder?key=':'/delete_file?key=')+encodeURIComponent(k)+'&curr_prefix='+encodeURIComponent(p);}}function filterTable(){let i=document.getElementById('searchInput').value.toLowerCase();document.querySelectorAll('tbody tr').forEach(r=>{let n=r.querySelector('.file-name')?.innerText.toLowerCase()||'';r.style.display=n.includes(i)?'':'none';});}"

async def main():
    app = web.Application(client_max_size=1024**3); app.add_routes(routes); runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    while True:
        try:
            await client.start(bot_token=BOT_TOKEN); break
        except FloodWaitError as e: await asyncio.sleep(e.seconds + 5)
        except: await asyncio.sleep(10)
    print("✅ BOT ONLINE"); await client.run_until_disconnected()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

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
from urllib.parse import quote, unquote, urlparse

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
import aiohttp
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
    p_bar = "●" * done + "○" * (10 - done)
    return (f"🧲 **{action}...**\n╭ `[{p_bar}]` » `{percent}%`\n"
            f"├ **Processed:** `{downloaded} of {total}`\n├ **Speed:** `{speed}`\n"
            f"├ **ETA:** `{eta}`\n├ **Peers:** `{cn}`\n├ **Elapsed:** `{elapsed}`\n"
            f"╰ **Cancel:** `/c_{task_code}`")

def get_readable_time(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    return f"{int(days)}d {int(hours)}h {int(mins)}m {int(secs)}s".replace("0d ", "").replace("0h ", "")

def get_largest_file(folder_path):
    largest, max_size = None, 0
    for r, _, files in os.walk(folder_path):
        for f in files:
            fp = os.path.join(r, f); sz = os.path.getsize(fp)
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

def sync_get_smart_dashboard_data(prefix=""):
    s3 = get_r2_client()
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix, Delimiter='/')
    
    all_objects, common_prefixes = [], []
    for page in pages:
        if 'Contents' in page: all_objects.extend(page['Contents'])
        if 'CommonPrefixes' in page: common_prefixes.extend(page['CommonPrefixes'])
            
    hls_bases = set(os.path.dirname(obj['Key']) for obj in all_objects if obj['Key'].endswith('master.m3u8'))
    hls_packages = {base: {'name': base, 'size': 0, 'date': None, 'type': 'HLS', 'url_key': f"{base}/master.m3u8"} for base in hls_bases}
    standalone_files = []
    total_size, mp4_count = 0, 0
    sorted_bases = sorted(list(hls_bases), key=len, reverse=True)
    
    for obj in all_objects:
        key, size, date = obj['Key'], obj['Size'], obj['LastModified']
        total_size += size
        if key.endswith('/') and size == 0: continue
        is_hls_part = False
        for base in sorted_bases:
            if key.startswith(base + '/') or key == base:
                hls_packages[base]['size'] += size
                if hls_packages[base]['date'] is None or date > hls_packages[base]['date']: hls_packages[base]['date'] = date
                is_hls_part = True; break
        if not is_hls_part and not key.endswith('/'):
            standalone_files.append({'name': key, 'size': size, 'date': date, 'type': 'FILE', 'url_key': key})
            if key.lower().endswith('.mp4'): mp4_count += 1
                
    items = list(hls_packages.values()) + standalone_files
    items.sort(key=lambda x: x['date'] if x['date'] else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)
    return {'total_size': total_size, 'mp4_count': mp4_count, 'hls_count': len(hls_packages), 'items': items, 'common_prefixes': common_prefixes}

def sync_delete_r2_file(s3_key):
    get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)

def sync_delete_r2_folder(prefix):
    s3 = get_r2_client()
    prefix = prefix.rstrip('/') + '/'
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix):
        if 'Contents' in page:
            s3.delete_objects(Bucket=R2_BUCKET_NAME, Delete={'Objects': [{'Key': obj['Key']} for obj in page['Contents']]})

def sync_rename_r2_file(old_key, new_key):
    s3 = get_r2_client()
    s3.copy({'Bucket': R2_BUCKET_NAME, 'Key': old_key}, R2_BUCKET_NAME, new_key)
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=old_key)

def sync_rename_r2_folder(old_prefix, new_prefix):
    s3 = get_r2_client(); old_prefix, new_prefix = old_prefix.rstrip('/') + '/', new_prefix.rstrip('/') + '/'
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=old_prefix):
        if 'Contents' in page:
            for obj in page['Contents']:
                old_key = obj['Key']; new_key = new_prefix + old_key[len(old_prefix):]
                s3.copy({'Bucket': R2_BUCKET_NAME, 'Key': old_key}, R2_BUCKET_NAME, new_key)
                s3.delete_object(Bucket=R2_BUCKET_NAME, Key=old_key)

def sync_r2_upload(file_path, s3_key, loop, msg, start_t):
    s3 = get_r2_client(); file_size = os.path.getsize(file_path)
    mime_type, _ = mimetypes.guess_type(file_path)
    class ProgressCallback:
        def __init__(self): self.seen = 0; self.last = 0
        def __call__(self, amount):
            self.seen += amount
            if time.time() - self.last > 4:
                self.last = time.time()
                try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("R2 Uploading", os.path.basename(file_path), self.seen, file_size, start_t)), loop)
                except: pass
    extra_args = {'ContentType': mime_type or 'video/mp4', 'ContentDisposition': 'inline'}
    t_config = TransferConfig(multipart_threshold=8*1024*1024, multipart_chunksize=8*1024*1024, max_concurrency=4)
    s3.upload_file(file_path, R2_BUCKET_NAME, s3_key, Callback=ProgressCallback(), ExtraArgs=extra_args, Config=t_config)

def sync_r2_upload_folder(folder_path, s3_prefix, loop, msg, start_t):
    s3 = get_r2_client(); all_files, total_size = [], 0
    for root, _, files in os.walk(folder_path):
        for f in files:
            fp = os.path.join(root, f); all_files.append(fp); total_size += os.path.getsize(fp)
    class ProgressCallback:
        def __init__(self): self.seen = 0; self.last = 0; self.lock = threading.Lock()
        def __call__(self, amount):
            with self.lock:
                self.seen += amount
                if time.time() - self.last > 4:
                    self.last = time.time()
                    try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("R2 Folder Sync", s3_prefix, self.seen, total_size, start_t)), loop)
                    except: pass
    prog_cb = ProgressCallback()
    def upload_one(fp):
        rel = os.path.relpath(fp, folder_path); s_key = f"{s3_prefix.strip('/')}/{rel.replace(os.sep, '/')}"
        ext = os.path.splitext(fp)[1].lower(); ct, _ = mimetypes.guess_type(fp)
        extra = {'ContentType': ct or 'application/octet-stream'}
        if ext not in ['.m3u8', '.ts']: extra['ContentDisposition'] = 'inline'
        s3.upload_file(fp, R2_BUCKET_NAME, s_key, Callback=prog_cb, ExtraArgs=extra)
    with ThreadPoolExecutor(max_workers=15) as ex: ex.map(upload_one, all_files)

async def upload_to_r2(file_path, msg, target_folder=None):
    start_t, loop = time.time(), asyncio.get_running_loop()
    bn = os.path.basename(file_path)
    s3_key = f"{target_folder.strip('/')}/{bn}" if target_folder else f"{datetime.datetime.now().year}/{datetime.datetime.now().month}/{datetime.datetime.now().day}/{bn}"
    await msg.edit(f"⬆️ **Uploading to R2...**\n🎬 `{bn}`")
    await asyncio.to_thread(sync_r2_upload, file_path, s3_key, loop, msg, start_t)
    code = secrets.token_urlsafe(8); link_storage[code] = {'s3_key': s3_key}
    return f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}", code

# ============================================
# --- 4. DOWNLOAD ENGINES ---
# ============================================
async def download_direct(url, workspace, msg, start_t, custom_name=None):
    timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
    async with ClientSession(timeout=timeout) as sess:
        if "/file/d/" in url or "drive.google.com" in url:
            file_id = re.search(r'(?:file/d/|id=|/d/)([a-zA-Z0-9_-]{25,})', url).group(1)
            r = await sess.get("https://drive.google.com/uc?export=download", params={'id': file_id, 'confirm': 't'}, allow_redirects=True)
        else: r = await sess.get(url, allow_redirects=True)
        
        if "text/html" in r.headers.get("Content-Type", "") and "drive.google.com" not in url: raise ValueError("HTML detected.")
        f_size = int(r.headers.get("Content-Length", 0))
        filename = custom_name or unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
        if not "." in filename: filename += ".mp4"
        fp = os.path.join(workspace, clean_double_extension(re.sub(r'[\\/*?:"<>|]', "", filename)))
        
        with open(fp, 'wb') as f:
            async for chunk in r.content.iter_chunked(1024*1024):
                f.write(chunk)
                if f.tell() % (10 * 1024 * 1024) == 0:
                    try: await msg.edit(get_status_text("Leeching", os.path.basename(fp), f.tell(), f_size, start_t))
                    except: pass
    return fp

async def download_any_url(url, workspace, custom_name, msg, start_t):
    if url.startswith("magnet:?"):
        aria_cmd = shutil.which('aria2c') or os.path.abspath('./aria2c')
        cmd = [aria_cmd, "--seed-time=0", "--max-connection-per-server=16", "--split=16", "--summary-interval=3", "--bt-stop-timeout=120", f"--dir={workspace}", url]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
        while process.returncode is None:
            await asyncio.sleep(5) # Simplified for space
        await process.wait()
        return get_largest_file(workspace)

    if not url.lower().endswith('.zip') and "drive.google.com" not in url:
        try:
            await msg.edit("🅿️ **Extracting with yt-dlp...**")
            ydl_opts = {'outtmpl': f'{workspace}/%(title)s.%(ext)s', 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return clean_double_extension(ydl.prepare_filename(info))
        except: pass
    return await download_direct(url, workspace, msg, start_t, custom_name)

# ============================================
# --- 5. DASHBOARD UI & HANDLERS ---
# ============================================
@routes.get('/dashboard')
async def dashboard_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, headers={'WWW-Authenticate': 'Basic realm="Dashboard"'}, text="🔒 Access Denied")
    prefix = request.query.get('prefix', '')
    data = await asyncio.to_thread(sync_get_smart_dashboard_data, prefix)
    file_rows = ""
    for pref in data.get('common_prefixes', []):
        f_path = pref['Prefix']; f_name = f_path.rstrip('/').split('/')[-1]
        file_rows += f"""<tr style="background: rgba(167, 139, 250, 0.05);"><td><a href="/dashboard?prefix={quote(f_path)}" style="color:#fbbf24;text-decoration:none;font-weight:bold;">📁 {f_name}/</a></td><td>-</td><td>Folder</td><td><button class="btn" style="background:#fb7185;color:#fff" onclick="deleteItem('{quote(f_path)}', true, '{quote(prefix)}')">🗑️ Delete</button></td></tr>"""
    for item in data['items']:
        name = item['name']; size = human_size(item['size']); date = item['date'].strftime("%Y-%m-%d %H:%M")
        url = f"{R2_PUBLIC_URL}/{quote(item['url_key'], safe='/')}"
        disp = name.rstrip('/').split('/')[-1]
        file_rows += f"""<tr><td>{'📦' if item['type']=='HLS' else '🎬'} {disp}</td><td>{size}</td><td>{date}</td><td><button class="btn" style="background:#38bdf8;color:#000" onclick="copyText('{url}')">🔗 Copy</button> <button class="btn" style="background:#fb7185;color:#fff" onclick="deleteItem('{quote(name)}', {str(item['type']=='HLS').lower()}, '{quote(prefix)}')">🗑️ Delete</button></td></tr>"""
    
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>R2 Manager</title><style>{DASHBOARD_CSS}</style><script>{DASHBOARD_JS}</script></head><body><div class="container"><h2>🛡️ R2 Smart Manager</h2><div class="stats-grid"><div class="stat-card">Size: {human_size(data['total_size'])}</div><div class="stat-card">HLS: {data['hls_count']}</div><div class="stat-card">MP4: {data['mp4_count']}</div></div><button class="btn btn-create" onclick="createFolder()">📁 New Folder</button><table style="width:100%;margin-top:20px;"><thead><tr><th>Name</th><th>Size</th><th>Date</th><th>Actions</th></tr></thead><tbody>{file_rows}</tbody></table></div></body></html>"""
    return web.Response(text=html, content_type='text/html')

@routes.get('/delete_item')
async def web_del(request):
    if not check_dashboard_auth(request): return web.Response(status=401)
    k, t = request.query.get('key'), request.query.get('type')
    if k:
        if t in ['HLS', 'FOLDER']: await asyncio.to_thread(sync_delete_r2_folder, k)
        else: await asyncio.to_thread(sync_delete_r2_file, k)
    raise web.HTTPFound(f"/dashboard?prefix={request.query.get('curr_prefix', '')}")

@routes.get('/create_folder')
async def web_cf(request):
    if not check_dashboard_auth(request): return web.Response(status=401)
    if p := request.query.get('path'): await asyncio.to_thread(sync_create_r2_folder, p)
    raise web.HTTPFound("/dashboard")

@routes.get('/')
async def root(request): return web.Response(text="✅ Bot Online. Go to /dashboard", content_type='text/html')

# ============================================
# --- 6. TELEGRAM HANDLERS ---
# ============================================
@client.on(events.NewMessage(incoming=True, func=lambda e: e.sender_id == ADMIN_ID))
async def master_handler(event):
    if event.file:
        await event.reply(f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`", buttons=[[Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]])
        return

    if event.text and (event.text.startswith("http") or event.text.startswith("magnet:?")):
        async with global_semaphore:
            raw = event.text.strip()
            # REGEX PARSER for -n and -f
            url = re.split(r'\s+-(?:n|f)\s+', raw)[0].strip()
            custom_name = re.search(r' -n\s+([^\s]+)', raw).group(1) if " -n " in raw else None
            target_folder = re.search(r' -f\s+([^\s]+)', raw).group(1) if " -f " in raw else None

            msg = await event.reply("🔗 **Leeching...**")
            workspace = f"dl_{uuid.uuid4().hex[:8]}"; os.makedirs(workspace, exist_ok=True)
            
            try:
                final_path = await download_any_url(url, workspace, custom_name, msg, time.time())
                if final_path.lower().endswith('.zip'):
                    await msg.edit("📦 **Extracting HLS ZIP...**")
                    extract_dir = os.path.join(workspace, "extracted"); os.makedirs(extract_dir, exist_ok=True)
                    await asyncio.to_thread(lambda: zipfile.ZipFile(final_path, 'r').extractall(extract_dir))
                    os.remove(final_path)
                    items = os.listdir(extract_dir)
                    s_dir = os.path.join(extract_dir, items[0]) if len(items)==1 and os.path.isdir(os.path.join(extract_dir, items[0])) else extract_dir
                    s_pref = target_folder or (items[0] if len(items)==1 else os.path.splitext(os.path.basename(final_path))[0])
                    await msg.edit(f"⬆️ **Uploading Folder: {s_pref}...**")
                    await asyncio.to_thread(sync_r2_upload_folder, s_dir, s_pref, asyncio.get_running_loop(), msg, time.time())
                    await msg.edit(f"✅ **HLS Done!**\n`{R2_PUBLIC_URL}/{quote(s_pref, safe='/')}/master.m3u8`", link_preview=False)
                else:
                    r2_url, code = await upload_to_r2(final_path, msg, target_folder)
                    await msg.edit(f"✅ **R2 Done!**\n🔗 `{r2_url}`", link_preview=False)
            except Exception as e: await msg.edit(f"❌ Error: {e}")
            finally: shutil.rmtree(workspace, ignore_errors=True); free_memory()

@client.on(events.CallbackQuery(data=re.compile(b"r2_")))
async def on_r2(event):
    if event.sender_id != ADMIN_ID: return
    tg_msg = await client.get_messages(event.chat_id, ids=int(event.data.decode().split("_")[1]))
    async with global_semaphore:
        workspace = f"dl_{uuid.uuid4().hex[:8]}"; os.makedirs(workspace, exist_ok=True)
        filename = clean_double_extension(re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4"))
        fp = os.path.join(workspace, filename); status = await event.respond("⬇️ Downloading...")
        try:
            with open(fp, 'wb') as f:
                async for chunk in client.iter_download(tg_msg.media, request_size=1048576): f.write(chunk)
            if filename.lower().endswith('.zip'):
                ext_dir = os.path.join(workspace, "extracted"); os.makedirs(ext_dir, exist_ok=True)
                await asyncio.to_thread(lambda: zipfile.ZipFile(fp, 'r').extractall(ext_dir))
                os.remove(fp); items = os.listdir(ext_dir)
                s_dir = os.path.join(ext_dir, items[0]) if len(items)==1 else ext_dir
                s_pref = items[0] if len(items)==1 else os.path.splitext(filename)[0]
                await status.edit(f"⬆️ Uploading HLS: {s_pref}..."); await asyncio.to_thread(sync_r2_upload_folder, s_dir, s_pref, asyncio.get_running_loop(), status, time.time())
                await status.edit(f"✅ HLS Done!\n`{R2_PUBLIC_URL}/{quote(s_pref, safe='/')}/master.m3u8`", link_preview=False)
            else:
                url, code = await upload_to_r2(fp, status); await status.edit(f"✅ R2 Done!\n🔗 `{url}`", link_preview=False)
        except Exception as e: await status.edit(f"❌ Error: {e}")
        finally: shutil.rmtree(workspace, ignore_errors=True); free_memory()

async def main():
    app = web.Application(); app.add_routes(routes); runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN); await client.run_until_disconnected()

if __name__ == '__main__': asyncio.run(main())

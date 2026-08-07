import os
import asyncio
import time
import re
import math
import random
import base64
import datetime
import gc
import ctypes
import shutil
import uuid
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, Button, types
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import InputFileBig, InputFile

# Web & Storage
from aiohttp import web, ClientSession
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
import yt_dlp
import aiofiles
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

PUBLIC_TRACKERS = "udp://tracker.opentrackr.org:1337/announce,http://tracker.openbittorrent.com:80/announce"

global_semaphore = asyncio.Semaphore(4)
routes = web.RouteTableDef()
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

def generate_progress_text(action, filename, current, total, start_time):
    diff = max(time.time() - start_time, 0.001)
    perc = (current / total) * 100 if total > 0 else 0
    speed = current / diff
    blocks = int(perc // 10)
    p_bar = "■" * blocks + "□" * (10 - blocks)
    return (f"🚀 **{action}**\n📦 `{filename}`\n\n"
            f"🌀 **Progress:** `[{p_bar}] {perc:.2f}%`\n"
            f"⚡ **Speed:** `{human_size(speed)}/s`\n"
            f"📂 **Size:** `{human_size(current)} / {human_size(total)}`")

def get_largest_file(folder_path):
    """Finds the biggest file in a folder (ignores .nfo, .txt, etc)"""
    largest_file = None
    max_size = 0
    for root, _, files in os.walk(folder_path):
        for f in files:
            fp = os.path.join(root, f)
            sz = os.path.getsize(fp)
            if sz > max_size:
                max_size, largest_file = sz, fp
    return largest_file

# ============================================
# --- 3. DOWNLOAD ENGINES (ISOLATED) ---
# ============================================
async def download_magnet(url, workspace, custom_name, msg, start_t):
    await msg.edit("🧲 **Initializing Torrent Engine...**")
    
    # Run Aria2c natively in the workspace
    cmd = [
        "aria2c", "--seed-time=0", "--max-connection-per-server=16", "--split=16",
        "--summary-interval=5", "--bt-stop-timeout=120", f"--bt-tracker={PUBLIC_TRACKERS}",
        f"--dir={workspace}", url
    ]
    
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    
    last_update = 0
    while process.returncode is None:
        await asyncio.sleep(4)
        if process.returncode is not None: break
        
        # Calculate size of workspace folder
        current_size = sum(os.path.getsize(os.path.join(w_root, f)) for w_root, _, w_files in os.walk(workspace) for f in w_files)
        now = time.time()
        
        if now - last_update > 5 and current_size > 0:
            speed = current_size / (now - start_t)
            try:
                await msg.edit(f"🧲 **Downloading Torrent...**\n⚡ **Speed:** `{human_size(speed)}/s`\n📂 **Size:** `{human_size(current_size)}`\n*(Fetching Metadata & Blocks)*")
                last_update = now
            except: pass

    await process.wait()
    
    largest_file = get_largest_file(workspace)
    if not largest_file:
        raise ValueError("Torrent failed or returned no video files.")
        
    return largest_file

def sync_yt_dlp(url, workspace):
    ydl_opts = {
        'outtmpl': f'{workspace}/%(title)s.%(ext)s',
        'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return get_largest_file(workspace)

async def download_direct(url, workspace, msg, start_t):
    filename = unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
    if not "." in filename: filename += ".mp4"
    file_path = os.path.join(workspace, filename)
    
    async with ClientSession() as sess:
        async with sess.get(url, allow_redirects=True) as r:
            if "text/html" in r.headers.get("Content-Type", ""):
                raise ValueError("Link is an HTML webpage, not a file.")
            f_size = int(r.headers.get("Content-Length", 0))
            with open(file_path, 'wb') as f:
                async for chunk in r.content.iter_chunked(1024 * 1024):
                    f.write(chunk)
                    if f.tell() % (10 * 1024 * 1024) == 0:
                        try: await msg.edit(get_status_text("Leeching Direct Link", filename, f.tell(), f_size, start_t))
                        except: pass
    return file_path

# ============================================
# --- 4. UPLOAD ENGINES ---
# ============================================
def sync_r2_upload(file_path, s3_key, loop, msg, start_t):
    endpoint = f"https://{R2_ACCOUNT_ID.split('.')[0]}.r2.cloudflarestorage.com"
    r2_config = Config(region_name='auto', signature_version='s3v4')
    s3 = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, config=r2_config)
    
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    
    class ProgressCallback:
        def __init__(self):
            self.seen = 0
            self.last_up = 0
        def __call__(self, bytes_amount):
            self.seen += bytes_amount
            now = time.time()
            if now - self.last_up > 4:
                self.last_up = now
                try: asyncio.run_coroutine_threadsafe(msg.edit(get_status_text("Uploading to R2", filename, self.seen, file_size, start_t)), loop)
                except: pass

    transfer_config = TransferConfig(multipart_threshold=8*1024*1024, multipart_chunksize=8*1024*1024, max_concurrency=4)
    s3.upload_file(file_path, R2_BUCKET_NAME, s3_key, Callback=ProgressCallback(), ExtraArgs={'ContentType': 'video/mp4', 'ContentDisposition': 'inline'}, Config=transfer_config)

async def upload_to_r2(file_path, msg, target_folder=None):
    start_t = time.time()
    loop = asyncio.get_running_loop()
    filename = os.path.basename(file_path)
    
    if target_folder:
        s3_key = f"{target_folder.strip('/')}/{filename}"
    else:
        now = datetime.datetime.now()
        s3_key = f"{now.year}/{now.month}/{now.day}/{filename}"
    
    await msg.edit(f"⬆️ **Connecting to Cloudflare R2...**")
    await asyncio.to_thread(sync_r2_upload, file_path, s3_key, loop, msg, start_t)
    
    return f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}"

async def upload_to_tg(file_path, msg):
    filename = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    part_size, file_id = 512 * 1024, random.getrandbits(63)
    start_t, uploaded_bytes = time.time(), 0
    sem = asyncio.Semaphore(15) 
    
    async def upload_part(idx):
        nonlocal uploaded_bytes
        async with sem:
            async with aiofiles.open(file_path, 'rb') as f:
                await f.seek(idx * part_size)
                chunk = await f.read(part_size)
            if file_size > 10*1024*1024: await client(SaveBigFilePartRequest(file_id, idx, math.ceil(file_size/part_size), chunk))
            else: await client(SaveFilePartRequest(file_id, idx, chunk))
            uploaded_bytes += len(chunk)
            
    tasks = [upload_part(i) for i in range(math.ceil(file_size/part_size))]
    
    async def updater():
        while uploaded_bytes < file_size:
            await asyncio.sleep(4)
            try: await msg.edit(get_status_text("Uploading to TG", filename, uploaded_bytes, file_size, start_t))
            except: pass
            
    u_task = asyncio.create_task(updater())
    await asyncio.gather(*tasks)
    u_task.cancel()
    
    return InputFileBig(file_id, math.ceil(file_size/part_size), filename) if file_size > 10*1024*1024 else InputFile(file_id, math.ceil(file_size/part_size), filename, '')

# ============================================
# --- 5. TELEGRAM HANDLERS ---
# ============================================
@client.on(events.NewMessage(incoming=True, func=lambda e: e.sender_id == ADMIN_ID))
async def master_handler(event):
    if event.file:
        await event.reply(
            f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`",
            buttons=[[Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]]
        )
        return

    if event.text and (event.text.startswith("http") or event.text.startswith("magnet:?")):
        async with global_semaphore:
            # Parse input string
            raw = event.text.strip()
            url = raw.split(" -n ")[0].split(" -f ")[0].strip()
            
            custom_name = None
            target_folder = None
            
            if " -n " in raw:
                custom_name = raw.split(" -n ")[1].split(" -f ")[0].strip()
                if not "." in custom_name: custom_name += ".mp4"
            if " -f " in raw:
                target_folder = raw.split(" -f ")[1].strip()

            msg = await event.reply("🔗 **Processing Request...**")
            
            # CREATE ISOLATED WORKSPACE (Prevents file collisions)
            workspace = f"dl_{uuid.uuid4().hex[:8]}"
            os.makedirs(workspace, exist_ok=True)
            start_t = time.time()
            
            try:
                # ROUTE 1: MAGNET
                if url.startswith("magnet:?"):
                    final_path = await download_magnet(url, workspace, custom_name, msg, start_t)
                
                # ROUTE 2: YT-DLP / DIRECT
                else:
                    try:
                        await msg.edit("🅿️ **Extracting with yt-dlp...**")
                        final_path = await asyncio.to_thread(sync_yt_dlp_download, url, workspace)
                    except Exception:
                        final_path = await download_direct(url, workspace, msg, start_t)

                if not final_path or not os.path.exists(final_path):
                    raise ValueError("Download failed to generate a file.")

                # Apply Custom Name if requested
                if custom_name:
                    new_path = os.path.join(workspace, custom_name.replace("/", "_"))
                    os.rename(final_path, new_path)
                    final_path = new_path

                # Upload to R2
                r2_url = await upload_to_r2(final_path, msg, target_folder)
                
                await msg.edit(f"✅ **Leeched & Uploaded to R2!**\n\n🎬 `{os.path.basename(final_path)}`\n🔗 `{r2_url}`")
                
            except Exception as e:
                await msg.edit(f"❌ Error: {e}")
            finally:
                shutil.rmtree(workspace, ignore_errors=True) # Destroy workspace completely
                free_memory()

@client.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id != ADMIN_ID: return
    data = event.data.decode()
    
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
                
                r2_url = await upload_to_r2(file_path, status)
                await status.edit(f"✅ **Cloudflare R2 Complete!**\n\n🎬 `{filename}`\n🔗 `{r2_url}`")
            except Exception as e: 
                await status.edit(f"❌ Error: {e}")
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
                free_memory()

# ============================================
# --- 6. SECURE WEB DASHBOARD ---
# ============================================
def check_dashboard_auth(request):
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Basic '): return False
    try:
        user, password = base64.b64decode(auth_header.split(' ', 1)[1]).decode('utf-8').split(':', 1)
        return user == DASHBOARD_USER and password == DASHBOARD_PASS
    except: return False

@routes.get('/dashboard')
async def dashboard_handler(request):
    if not check_dashboard_auth(request):
        return web.Response(status=401, headers={'WWW-Authenticate': 'Basic realm="Dashboard"'}, text="Access Denied")
    
    s3 = get_r2_client()
    try:
        response = await asyncio.to_thread(s3.list_objects_v2, Bucket=R2_BUCKET_NAME)
        file_rows = "".join([
            f"<tr><td>{o['Key']}</td><td>{human_size(o['Size'])}</td><td><a href='{R2_PUBLIC_URL}/{quote(o['Key'], safe='/')}' target='_blank' style='color:#00d2ff'>Link</a></td></tr>" 
            for o in sorted(response.get('Contents', []), key=lambda x: x['LastModified'], reverse=True)
        ]) or "<tr><td colspan='3'>No files.</td></tr>"
    except Exception as e: file_rows = f"<tr><td colspan='3' style='color:red'>Error: {e}</td></tr>"

    html = f"<html><body style='background:#0f172a;color:#eee;font-family:sans-serif;padding:20px;'><h2>🛡️ R2 Dashboard</h2><table style='width:100%;text-align:left;'><tr><th>File</th><th>Size</th><th>Link</th></tr>{file_rows}</table></body></html>"
    return web.Response(text=html, content_type='text/html')

@routes.get('/')
async def root(request):
    return web.Response(text="<html><body style='background:#0f172a;color:#38bdf8;text-align:center;padding-top:150px;font-family:sans-serif;'><h1 style='font-size:40px;'>✅ System Online</h1><a href='/dashboard' style='color:#0f172a;background:#38bdf8;padding:15px;text-decoration:none;border-radius:5px;'>Dashboard</a></body></html>", content_type='text/html')

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

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

# --- 2. UNIQUE FILENAME PREVENT CRASH ---
def get_unique_filename(filepath):
    """Generates a unique name BEFORE creating a file if one already exists."""
    if not os.path.exists(filepath):
        return filepath
    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"

# --- 3. YT-DLP / GDRIVE ENGINE ---
def sync_yt_dlp_download(url, custom_name=None):
    """Extracts real titles and downloads file natively."""
    if custom_name:
        custom_name = get_unique_filename(custom_name)
        out_tmpl = custom_name
        if not '.' in out_tmpl:
            out_tmpl += '.%(ext)s'
    else:
        out_tmpl = '%(title)s.%(ext)s'

    ydl_opts = {
        'outtmpl': out_tmpl,
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'overwrites': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return filename

# --- 4. C-LEVEL MEMORY PURGE HELPER ---
def force_system_ram_purge():
    gc.collect()
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception:
        pass

# --- 5. SETUP CLIENT ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection=ConnectionTcpFull, use_ipv6=False)

# --- 6. UI HELPERS ---
def human_size(bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes < 1024: return f"{bytes:.2f} {unit}"
        bytes /= 1024

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
    s3.copy_object(
        Bucket=R2_BUCKET_NAME,
        CopySource={'Bucket': R2_BUCKET_NAME, 'Key': old_key},
        Key=new_key
    )
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

# --- 8. SECURED WEB DASHBOARD & ACTION ENDPOINTS ---
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
    try:
        response = await asyncio.to_thread(sync_get_r2_files)
        if 'Contents' in response:
            for obj in sorted(response['Contents'], key=lambda x: x['LastModified'], reverse=True):
                name = obj['Key']
                url = f"{R2_PUBLIC_URL}/{quote(name, safe='/')}"
                file_rows += f"""
                <tr>
                    <td>{name}</td>
                    <td>{human_size(obj['Size'])}</td>
                    <td>
                        <button class="btn btn-copy" onclick="copyText('{url}')">Copy URL</button>
                        <a href="{url}" target="_blank" class="btn btn-view">Play Video</a>
                        <button class="btn btn-move" onclick="moveFolder('{quote(name)}')">📁 Move</button>
                        <button class="btn btn-rename" onclick="renameFile('{quote(name)}')">Rename</button>
                        <button class="btn btn-delete" onclick="deleteFile('{quote(name)}')">Delete</button>
                    </td>
                </tr>"""
        else:
            file_rows = "<tr><td colspan='3' style='text-align:center'>No files found in bucket.</td></tr>"
    except Exception as e:
        file_rows = f"<tr><td colspan='3' style='color:red'>Error: {str(e)}</td></tr>"

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>R2 Dash</title>
        <style>
            body {{ background:#0f0f0f; color:#eee; font-family:'Segoe UI', sans-serif; padding:20px; margin:0; }}
            .container {{ max-width: 1100px; margin: auto; background: #151a21; padding: 20px; border-radius: 10px; }}
            h2 {{ color: #00d2ff; margin-top: 0; }}
            table {{ width:100%; border-collapse:collapse; margin-top: 15px; }}
            th {{ background:#00d2ff; color:#000; padding:12px; text-align:left; }}
            td {{ padding:10px; border-bottom:1px solid #222; font-size:14px; word-break: break-all; }}
            tr:hover {{ background:#1c232d; }}
            .btn {{ border:none; padding:6px 12px; cursor:pointer; font-weight:bold; border-radius:4px; text-decoration:none; font-size:12px; display:inline-block; margin:2px; }}
            .btn-copy {{ background:#00d2ff; color:#000; }}
            .btn-view {{ background:#00ff88; color:#000; }}
            .btn-move {{ background:#9d4edd; color:#fff; }}
            .btn-rename {{ background:#ffb703; color:#000; }}
            .btn-delete {{ background:#e63946; color:#fff; }}
        </style>
        <script>
            function copyText(t) {{ navigator.clipboard.writeText(t); alert('Copied!'); }}
            function deleteFile(key) {{
                let decodedKey = decodeURIComponent(key);
                if (confirm('Delete: ' + decodedKey + '?')) {{
                    window.location.href = '/delete_file?key=' + encodeURIComponent(decodedKey);
                }}
            }}
            function renameFile(key) {{
                let decodedKey = decodeURIComponent(key);
                let newKey = prompt('New filename/path:', decodedKey);
                if (newKey && newKey !== decodedKey) {{
                    window.location.href = '/rename_file?old_key=' + encodeURIComponent(decodedKey) + '&new_key=' + encodeURIComponent(newKey);
                }}
            }}
            function moveFolder(key) {{
                let decodedKey = decodeURIComponent(key);
                let currentDir = decodedKey.includes('/') ? decodedKey.substring(0, decodedKey.lastIndexOf('/')) : '';
                let targetFolder = prompt('Enter target folder path (e.g. Movies/2026 or Series/S01):', currentDir);
                if (targetFolder !== null) {{
                    window.location.href = '/move_file?old_key=' + encodeURIComponent(decodedKey) + '&target_folder=' + encodeURIComponent(targetFolder);
                }}
            }}
        </script>
    </head>
    <body>
        <div class="container">
            <h2>🛡️ Cloudflare R2 Dashboard</h2>
            <div style="background:#222; padding:10px; border-radius:5px; font-size:13px;"><b>Bucket:</b> {R2_BUCKET_NAME}</div>
            <table>
                <thead><tr><th>File Path</th><th>Size</th><th>Actions</th></tr></thead>
                <tbody>{file_rows}</tbody>
            </table>
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
        except Exception as e: print(f"Web Delete Error: {e}")
    raise web.HTTPFound('/dashboard')

@routes.get('/rename_file')
async def web_rename_handler(request):
    if not check_dashboard_auth(request): return web.Response(status=401, text="Unauthorized")
    old_key, new_key = request.query.get('old_key'), request.query.get('new_key')
    if old_key and new_key and old_key != new_key:
        try: 
            await asyncio.to_thread(sync_rename_r2_file, old_key, new_key)
            force_system_ram_purge()
        except Exception as e: print(f"Web Rename Error: {e}")
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
            except Exception as e: print(f"Web Move Error: {e}")
    raise web.HTTPFound('/dashboard')

@routes.get('/')
async def root(request):
    return web.Response(text="✅ Cloudflare R2 Bot Active.", content_type='text/html')

# --- 9. HYBRID DOWNLOADER (YT-DLP + AIOHTTP FALLBACK) ---
async def download_any_url(url, custom_name, msg, start_t):
    # Method 1: Try yt-dlp
    try:
        await msg.edit("🅿️ **Extracting File Info via yt-dlp...**")
        filename = await asyncio.to_thread(sync_yt_dlp_download, url, custom_name)
        if filename and os.path.exists(filename) and os.path.getsize(filename) > 0:
            return filename
    except Exception as yt_err:
        print(f"yt-dlp fallback to aiohttp: {yt_err}")

    # Method 2: Fallback to Direct HTTP Leeching
    async with ClientSession() as sess:
        async with sess.get(url, allow_redirects=True) as r:
            if "text/html" in r.headers.get("Content-Type", ""):
                raise ValueError("Link is a webpage (HTML), not a direct file.")
            
            f_size = int(r.headers.get("Content-Length", 0))
            if custom_name:
                filename = custom_name
            else:
                fname = None
                if "Content-Disposition" in r.headers:
                    matches = re.findall('filename="?([^"]+)"?', r.headers["Content-Disposition"])
                    if matches: fname = matches[0]
                if not fname:
                    fname = unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
                filename = fname

            if not "." in filename: filename += ".mp4"
            filename = re.sub(r'[\\/*?:"<>|]', "", filename)
            filename = get_unique_filename(filename)

            await msg.edit(f"⬇️ **Leeching Direct Link...**\n🎬 `{filename}`")

            with open(filename, 'wb') as f:
                async for chunk in r.content.iter_chunked(1024*1024):
                    f.write(chunk)
                    if f.tell() % (10 * 1024 * 1024) == 0:
                        await msg.edit(get_status_text("Leeching", filename, f.tell(), f_size, start_t))
            return filename

# --- 10. BOT HANDLERS (ADMIN ONLY) ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id != ADMIN_ID: return

    if event.file:
        await event.reply(
            f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`",
            buttons=[[Button.inline("🛡️ Upload to Cloudflare R2", data=f"r2_{event.id}")]]
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
                # Use Hybrid Downloader (yt-dlp + aiohttp)
                filename = await download_any_url(url, custom_name, msg, start_t)
                
                # Upload to Cloudflare R2
                r2_url, code = await upload_to_r2(filename, msg)
                await msg.edit(
                    f"✅ **Leeched & Uploaded to R2!**\n\n🎬 `{os.path.basename(filename)}`\n🔗 `{r2_url}`",
                    buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]]
                )
                
            except Exception as e: 
                await msg.edit(f"❌ Error: {e}")
            finally:
                if filename and os.path.exists(filename): 
                    os.remove(filename)
                force_system_ram_purge()

@client.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id != ADMIN_ID: return
    data = event.data.decode()
    
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
        await event.answer("Processing R2 Upload...")
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        
        async with global_semaphore:
            raw_filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
            # Generate unique filename BEFORE creating the file on disk
            filename = get_unique_filename(raw_filename)
            
            status = await event.respond(f"⬇️ Downloading from Telegram...")
            start_t = time.time()
            try:
                # Download from Telegram
                with open(filename, 'wb') as f:
                    async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                        f.write(chunk)
                        if f.tell() % (10 * 1024 * 1024) == 0: 
                            await status.edit(get_status_text("TG Down", filename, f.tell(), tg_msg.file.size, start_t))
                
                # Upload to Cloudflare R2
                r2_url, code = await upload_to_r2(filename, status)
                await status.edit(
                    f"✅ **Cloudflare R2 Complete!**\n\n🎬 `{os.path.basename(filename)}`\n🔗 `{r2_url}`",
                    buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]]
                )
                
            except Exception as e: 
                await status.edit(f"❌ Error: {e}")
            finally:
                if os.path.exists(filename): os.remove(filename)
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

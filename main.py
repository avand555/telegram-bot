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

# Web & Storage Imports
from aiohttp import web, ClientSession, FormData
import aiohttp
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

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

# 4 Parallel Heavy Tasks Allowed on 1GB RAM
global_semaphore = asyncio.Semaphore(4)
link_storage = {}
routes = web.RouteTableDef()

# --- 2. C-LEVEL MEMORY PURGE HELPER ---
def force_system_ram_purge():
    gc.collect()
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception:
        pass

# --- 3. SETUP CLIENT ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection=ConnectionTcpFull, use_ipv6=False)

# --- 4. UI HELPERS ---
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

# --- 5. R2 CLIENT & SYNC S3 OPERATIONS ---
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
                    text = get_status_text("R2 Uploading", filename, self.seen, file_size, start_t)
                    asyncio.run_coroutine_threadsafe(status_msg.edit(text), loop)
                except: pass

    transfer_config = TransferConfig(multipart_threshold=8*1024*1024, multipart_chunksize=8*1024*1024, max_concurrency=4)
    s3.upload_file(filename, R2_BUCKET_NAME, s3_key, Callback=ProgressCallback(), Config=transfer_config)

async def upload_to_r2(filename, status_msg):
    start_t = time.time()
    loop = asyncio.get_running_loop()
    
    now = datetime.datetime.now()
    s3_key = f"{now.year}/{now.month}/{now.day}/{filename}"
    
    await status_msg.edit(f"⬆️ **Connecting to Cloudflare R2...**\n🎬 `{filename}`")
    await asyncio.to_thread(sync_r2_upload, filename, s3_key, loop, status_msg, start_t)
    
    code = secrets.token_urlsafe(8)
    link_storage[code] = {'s3_key': s3_key}
    
    public_link = f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}"
    return public_link, code

# --- 6. 🚀 PARALLEL TELEGRAM DOWNLOAD ENGINE (SPEED BOOST) ---
async def fast_tg_download(client, message, file_path, msg, filename):
    file_size = message.file.size
    part_size = 1024 * 1024 # 1 MB Chunks
    total_parts = math.ceil(file_size / part_size)
    start_time = time.time()
    downloaded_bytes = 0

    doc = message.document
    if not doc:
        raise ValueError("Media is not a valid document.")

    location = types.InputDocumentFileLocation(
        id=doc.id,
        access_hash=doc.access_hash,
        file_reference=doc.file_reference,
        thumb_size=''
    )

    # Pre-allocate file space
    with open(file_path, 'wb') as f:
        if file_size > 0:
            f.seek(file_size - 1)
            f.write(b'\0')

    # 10 Parallel Workers pulling 1MB chunks concurrently
    sem = asyncio.Semaphore(10)

    async def download_part(idx):
        nonlocal downloaded_bytes
        offset = idx * part_size
        async with sem:
            res = await client(GetFileRequest(location=location, offset=offset, limit=part_size))
            with open(file_path, 'r+b') as f:
                f.seek(offset)
                f.write(res.bytes)
            downloaded_bytes += len(res.bytes)

    tasks = [download_part(i) for i in range(total_parts)]

    async def updater():
        while downloaded_bytes < file_size:
            await asyncio.sleep(3)
            try:
                await msg.edit(get_status_text("Fast TG Down", filename, downloaded_bytes, file_size, start_time))
            except Exception: pass

    u_task = asyncio.create_task(updater())
    await asyncio.gather(*tasks)
    u_task.cancel()
    force_system_ram_purge()

# --- 7. SECURED WEB DASHBOARD ---
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
                        <a href="{url}" target="_blank" class="btn btn-view">View</a>
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
            .btn-view {{ background:#333; color:#fff; }}
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

@routes.get('/')
async def root(request):
    return web.Response(text="✅ Cloudflare R2 Bot Active.", content_type='text/html')

# --- 8. TG FAST UPLOAD ---
async def fast_upload(client, file_path, msg, filename):
    file_size = os.path.getsize(file_path)
    part_size, file_id = 512 * 1024, random.getrandbits(63)
    start_time, uploaded_bytes = time.time(), 0
    
    # 15 Parallel Workers for fast uploads on 1GB RAM
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

# --- 9. BOT HANDLERS (ADMIN ONLY) ---
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
            url = event.text.split(" -n ")[0].strip()
            name = event.text.split(" -n ")[1].strip() if " -n " in event.text else "video.mp4"
            if not "." in name: name += ".mp4"
            
            name = re.sub(r'[\\/*?:"<>|]', "", name)
            msg = await event.reply("🔗 **Downloading from URL...**")
            start_t = time.time()
            
            try:
                async with ClientSession() as sess:
                    async with sess.get(url) as r:
                        f_size = int(r.headers.get("Content-Length", 0))
                        with open(name, 'wb') as f:
                            async for chunk in r.content.iter_chunked(1024*1024):
                                f.write(chunk)
                                if f.tell() % (10 * 1024 * 1024) == 0: 
                                    await msg.edit(get_status_text("Leeching URL", name, f.tell(), f_size, start_t))
                
                r2_url, code = await upload_to_r2(name, msg)
                await msg.edit(
                    f"✅ **Leeched & Uploaded to R2!**\n\n🎬 `{name}`\n🔗 `{r2_url}`",
                    buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]]
                )
                
            except Exception as e: 
                await msg.edit(f"❌ Error: {e}")
            finally:
                if os.path.exists(name): os.remove(name)
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
            filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
            status = await event.respond(f"⬇️ Downloading from Telegram...")
            try:
                # 🚀 USE PARALLEL TG DOWNLOAD ENGINE
                await fast_tg_download(client, tg_msg, filename, status, filename)
                
                r2_url, code = await upload_to_r2(filename, status)
                await status.edit(
                    f"✅ **Cloudflare R2 Complete!**\n\n🎬 `{filename}`\n🔗 `{r2_url}`",
                    buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]]
                )
                
            except Exception as e: 
                await status.edit(f"❌ Error: {e}")
            finally:
                if os.path.exists(filename): os.remove(filename)
                force_system_ram_purge()

# --- 10. STARTUP ---
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

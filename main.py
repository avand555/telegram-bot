import os
import secrets
import asyncio
import mimetypes
import time
import re
import math
import random
import io
import subprocess
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
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
VIDMOLY_API_KEY = os.environ.get("VIDMOLY_API_KEY", "547285kdjw3pg3e303au64")

# --- CLOUDFLARE R2 CREDENTIALS ---
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "").strip()
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").strip().rstrip('/')

ALLOWED_USERS = {716887656, 1053544356} 
ADMIN_ID = 716887656  

global_semaphore = asyncio.Semaphore(2)
link_storage = {}
routes = web.RouteTableDef()

# --- 2. SETUP CLIENT ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection=ConnectionTcpFull, use_ipv6=False)

# --- 3. UI HELPERS ---
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

# --- 4. R2 DASHBOARD (SYNC FETCH) ---
def sync_get_r2_files():
    clean_id = R2_ACCOUNT_ID.replace("https://", "").replace("http://", "").split(".")[0]
    endpoint = f"https://{clean_id}.r2.cloudflarestorage.com"
    r2_config = Config(region_name='us-east-1', signature_version='s3v4')
    s3 = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, config=r2_config)
    return s3.list_objects_v2(Bucket=R2_BUCKET_NAME)

@routes.get('/dashboard')
async def dashboard_handler(request):
    file_rows = ""
    try:
        response = await asyncio.to_thread(sync_get_r2_files)
        if 'Contents' in response:
            for obj in sorted(response['Contents'], key=lambda x: x['LastModified'], reverse=True):
                name = obj['Key']
                url = f"{R2_PUBLIC_URL}/{quote(name)}"
                file_rows += f"<tr><td>{name}</td><td>{human_size(obj['Size'])}</td><td><button onclick=\"copyText('{url}')\">Copy</button></td></tr>"
        else:
            file_rows = "<tr><td colspan='3' style='text-align:center'>No files found.</td></tr>"
    except Exception as e:
        file_rows = f"<tr><td colspan='3' style='color:red'>Error: {str(e)}</td></tr>"

    html = f"<html><head><title>R2 Dash</title><style>body{{background:#0f0f0f;color:#eee;font-family:sans-serif;padding:20px;}}table{{width:100%;border-collapse:collapse;}}th,td{{padding:10px;border:1px solid #333;text-align:left;}}th{{background:#00d2ff;color:#000;}}button{{background:#00d2ff;border:none;padding:5px;cursor:pointer;font-weight:bold;}}</style><script>function copyText(t){{navigator.clipboard.writeText(t);alert('Copied!');}}</script></head><body><h2>🛡️ R2 Dashboard</h2><div style='background:#222;padding:10px;margin-bottom:10px;'>Bucket: {R2_BUCKET_NAME}</div><table><thead><tr><th>Name</th><th>Size</th><th>Action</th></tr></thead><tbody>{file_rows}</tbody></table></body></html>"
    return web.Response(text=html, content_type='text/html')

# --- 5. R2 UPLOAD ENGINE (OFFICIAL BOTO3 FIX) ---
def sync_r2_upload(filename, loop, status_msg, start_t):
    clean_id = R2_ACCOUNT_ID.replace("https://", "").replace("http://", "").split(".")[0]
    endpoint = f"https://{clean_id}.r2.cloudflarestorage.com"
    file_size = os.path.getsize(filename)
    
    # 1. Strict Config overriding Koyeb default regions
    r2_config = Config(region_name='us-east-1', signature_version='s3v4', retries={'max_attempts': 3, 'mode': 'standard'})
    s3 = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, config=r2_config)
    
    # 2. Thread-Safe Progress Callback
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

    # 3. Upload Execution (Forced 10MB Chunks for Cloudflare Stability)
    transfer_config = TransferConfig(multipart_threshold=10*1024*1024, multipart_chunksize=10*1024*1024)
    s3.upload_file(filename, R2_BUCKET_NAME, filename, Callback=ProgressCallback(), Config=transfer_config)

async def upload_to_r2(filename, status_msg):
    start_t = time.time()
    loop = asyncio.get_running_loop()
    await status_msg.edit(f"⬆️ **Connecting to Cloudflare R2...**\n🎬 `{filename}`")
    
    # Send the heavy upload task to a background thread to prevent crashing
    await asyncio.to_thread(sync_r2_upload, filename, loop, status_msg, start_t)
    return f"{R2_PUBLIC_URL}/{quote(filename)}"

# --- 6. VIDMOLY UPLOAD ---
async def upload_to_vidmoly(filename, status_msg, vidmoly_api_key):
    start_t = time.time()
    file_size = os.path.getsize(filename)
    async with ClientSession() as sess:
        async with sess.get(f"https://vidmoly.me/api/upload/server?key={vidmoly_api_key}") as r:
            upload_url = (await r.json(content_type=None))['result']
        with open(filename, 'rb') as f:
            data = FormData()
            data.add_field('api_key', vidmoly_api_key)
            data.add_field('file', f, filename=filename)
            async def progress_check():
                while not upload_task.done():
                    await asyncio.sleep(4); current = f.tell()
                    try: await status_msg.edit(get_status_text("Vidmoly Uploading", filename, current, file_size, start_t))
                    except: pass
            upload_task = asyncio.create_task(sess.post(upload_url, data=data))
            asyncio.create_task(progress_check())
            res = await (await upload_task).text()
            match = re.search(r'name="fn">([a-zA-Z0-9]+)<', res)
            return match.group(1) if match else None

# --- 7. TG FAST UPLOAD ---
async def fast_upload(client, file_path, msg, filename):
    file_size = os.path.getsize(file_path)
    part_size, file_id = 512 * 1024, random.getrandbits(63)
    start_time, uploaded_bytes = time.time(), 0
    sem = asyncio.Semaphore(10) 
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

# --- 8. WEB SERVER (IDM) ---
@routes.get('/')
async def root(request):
    html = "<html><body style='background:#000;color:#00d2ff;text-align:center;padding-top:100px;'><h1>✅ Bot is Active</h1><a href='/dashboard' style='color:#fff;'>Go to R2 Dashboard</a></body></html>"
    return web.Response(text=html, content_type='text/html')

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']; data = link_storage.get(code)
    if not data: return web.Response(text="Expired", status=410)
    msg, file_name, start = data['msg'], unquote(request.match_info['filename']), 0
    if request.headers.get('Range'):
        match = re.search(r'bytes=(\d+)-', request.headers.get('Range'))
        if match: start = int(match.group(1))
    resp = web.StreamResponse(status=206 if start else 200, headers={'Content-Disposition': f'attachment; filename="{file_name}"','Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4','Content-Length': str(msg.file.size - start)})
    await resp.prepare(request)
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576): await resp.write(chunk)
    except: pass
    return resp

# --- 9. BOT HANDLERS ---
@client.on(events.NewMessage(incoming=True, func=lambda e: e.sender_id in ALLOWED_USERS))
async def handle_new_message(event):
    if event.file:
        await event.reply(f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`",
            buttons=[[Button.inline("🔗 Direct Link", data=f"link_{event.id}")],
                     [Button.inline("☁️ Vidmoly", data=f"moly_{event.id}"), Button.inline("🛡️ Cloudflare R2", data=f"r2_{event.id}")]])
    elif event.text and event.text.startswith("http"):
        async with global_semaphore:
            url = event.text.split(" -n ")[0].strip()
            name = event.text.split(" -n ")[1].strip() if " -n " in event.text else "video.mp4"
            if not "." in name: name += ".mp4"
            msg = await event.reply("🔗 **Leeching URL...**")
            try:
                async with ClientSession() as sess:
                    async with sess.get(url) as r:
                        f_size = int(r.headers.get("Content-Length", 0))
                        start_t = time.time()
                        with open(name, 'wb') as f:
                            async for chunk in r.content.iter_chunked(1024*1024):
                                f.write(chunk)
                                if f.tell() % (10 * 1024 * 1024) == 0: await msg.edit(get_status_text("Leeching", name, f.tell(), f_size, start_t))
                up_file = await fast_upload(client, name, msg, name)
                await client.send_file(event.chat_id, file=up_file, caption=f"✅ `{name}`", supports_streaming=True, attributes=[types.DocumentAttributeVideo(duration=0, w=1280, h=720, supports_streaming=True)])
                await msg.delete()
            except Exception as e: await event.reply(f"❌ Error: {e}")
            finally:
                if os.path.exists(name): os.remove(name)

@client.on(events.CallbackQuery)
async def on_callback(event):
    data = event.data.decode(); msg_id = int(data.split("_")[1]); await event.answer("Processing...")
    tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
    if data.startswith("link"):
        code = secrets.token_urlsafe(8); link_storage[code] = {'msg': tg_msg}
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/') or f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        await client.send_message(event.chat_id, f"🚀 **Link:**\n\n`{base}/{code}/{quote(tg_msg.file.name or 'video.mp4')}`")
    elif data.startswith("r2") or data.startswith("moly"):
        async with global_semaphore:
            filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
            status = await event.respond(f"⬇️ Downloading from Telegram..."); start_t = time.time()
            try:
                with open(filename, 'wb') as f:
                    async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                        f.write(chunk)
                        if f.tell() % (10 * 1024 * 1024) == 0: await status.edit(get_status_text("TG Down", filename, f.tell(), tg_msg.file.size, start_t))
                
                if data.startswith("r2"):
                    r2_url = await upload_to_r2(filename, status)
                    await status.edit(f"✅ **R2 Upload Complete!**\n\n🔗 `{r2_url}`")
                else:
                    code = await upload_to_vidmoly(filename, status, VIDMOLY_API_KEY)
                    if code: await status.edit(f"✅ **Vidmoly Done!**\n🔗 `https://vidmoly.biz/embed-{code}.html`", buttons=[[Button.url("🖼 Open", f"https://vidmoly.biz/embed-{code}.html")]])
                    else: await status.edit("❌ Vidmoly Error: Failed to parse URL.")
            except Exception as e: await status.edit(f"❌ Error: {e}")
            finally:
                if os.path.exists(filename): os.remove(filename)

async def main():
    app = web.Application(); app.add_routes(routes)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

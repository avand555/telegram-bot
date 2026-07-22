import os
import secrets
import asyncio
import mimetypes
import time
import re
import math
import random
import io
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import InputFileBig, InputFile

# Web & API Imports
from aiohttp import web, ClientSession, FormData
import aiohttp
import aioboto3 # For Cloudflare R2

# --- 1. CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
VIDMOLY_API_KEY = "547285kdjw3pg3e303au64"

# Cloudflare R2 Config
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip('/')

BOT_START_TIME = time.time()
ALLOWED_USERS = {716887656, 1053544356} 
ADMIN_ID = 716887656  
EXPIRATION_TIME = 24 * 60 * 60 

global_semaphore = asyncio.Semaphore(2)
link_storage = {}
routes = web.RouteTableDef()

# --- 2. SETUP CLIENT ---
client = TelegramClient(
    'bot_session', int(API_ID), API_HASH,
    connection=ConnectionTcpFull, use_ipv6=False
)

# --- 3. SPEED METER HELPER ---
def get_status_text(action, filename, current, total, start_time):
    now = time.time()
    diff = now - start_time
    if diff <= 0: diff = 0.001
    perc = (current / total) * 100 if total > 0 else 0
    speed = current / diff 
    def human_size(bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes < 1024: return f"{bytes:.2f} {unit}"
            bytes /= 1024
    done = int(perc // 10)
    p_bar = "■" * done + "□" * (10 - done)
    return (f"🚀 **{action}**\n📦 `{filename}`\n\n"
            f"🌀 **Progress:** `[{p_bar}] {perc:.2f}%`\n"
            f"⚡ **Speed:** `{human_size(speed)}/s`\n"
            f"📂 **Size:** `{human_size(current)} / {human_size(total)}`")

# --- 4. CLOUDFLARE R2 UPLOAD ENGINE ---
async def upload_to_r2(filename, status_msg):
    start_t = time.time()
    file_size = os.path.getsize(filename)
    endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    
    session = aioboto3.Session()
    async with session.client('s3', endpoint_url=endpoint_url,
                              aws_access_key_id=R2_ACCESS_KEY_ID,
                              aws_secret_access_key=R2_SECRET_ACCESS_KEY) as s3:
        
        # Background task to check progress
        async def track_progress():
            while True:
                await asyncio.sleep(4)
                try:
                    # S3 doesn't give native progress easily in aioboto3 without complexity,
                    # so we monitor the file read position or just show a "Blasting" message
                    await status_msg.edit(f"⬆️ **Cloudflare Uploading...**\n🎬 `{filename}`\n*(Cloudflare is processing at high speed)*")
                except: break

        p_task = asyncio.create_task(track_progress())
        try:
            await s3.upload_file(filename, R2_BUCKET_NAME, filename)
            p_task.cancel()
            return f"{R2_PUBLIC_URL}/{quote(filename)}" if R2_PUBLIC_URL else f"Successfully uploaded to {R2_BUCKET_NAME}"
        except Exception as e:
            p_task.cancel()
            raise e

# --- 5. SAFE VIDMOLY UPLOADER ---
async def upload_to_vidmoly(filename, status_msg, vidmoly_api_key):
    start_t = time.time()
    file_size = os.path.getsize(filename)
    async with ClientSession() as sess:
        async with sess.get(f"https://vidmoly.me/api/upload/server?key={vidmoly_api_key}") as r:
            res = await r.json(content_type=None)
            upload_url = res['result']
        with open(filename, 'rb') as f:
            data = FormData()
            data.add_field('api_key', vidmoly_api_key)
            data.add_field('file', f, filename=filename)
            async def progress_check():
                while not upload_task.done():
                    await asyncio.sleep(4)
                    try: await status_msg.edit(get_status_text("Vidmoly Uploading", filename, f.tell(), file_size, start_t))
                    except: pass
            upload_task = asyncio.create_task(sess.post(upload_url, data=data))
            asyncio.create_task(progress_check())
            response = await upload_task
            res_text = await response.text()
            match = re.search(r'name="fn">([a-zA-Z0-9]+)<', res_text)
            return match.group(1) if match else None

# --- 6. FAST TG UPLOAD ---
async def fast_upload(client, file_path, msg, filename):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024
    total_parts = math.ceil(file_size / part_size)
    file_id = random.getrandbits(63)
    start_time = time.time()
    uploaded_bytes = 0
    sem = asyncio.Semaphore(10) 
    async def upload_part(idx):
        nonlocal uploaded_bytes
        async with sem:
            with open(file_path, 'rb') as f:
                f.seek(idx * part_size)
                chunk = f.read(part_size)
            if file_size > 10 * 1024 * 1024: await client(SaveBigFilePartRequest(file_id, idx, total_parts, chunk))
            else: await client(SaveFilePartRequest(file_id, idx, chunk))
            uploaded_bytes += len(chunk)
    tasks = [upload_part(i) for i in range(total_parts)]
    async def updater():
        while uploaded_bytes < file_size:
            await asyncio.sleep(4)
            try: await msg.edit(get_status_text("Uploading to TG", filename, uploaded_bytes, file_size, start_time))
            except: pass
    u_task = asyncio.create_task(updater())
    await asyncio.gather(*tasks)
    u_task.cancel()
    return InputFileBig(file_id, total_parts, filename) if file_size > 10*1024*1024 else InputFile(file_id, total_parts, filename, '')

# --- 7. WEB SERVER ---
@routes.get('/')
async def root(request): return web.Response(text="✅ Bot Online", status=200)

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data: return web.Response(text="Link Expired", status=410)
    msg, url_filename = data['msg'], unquote(request.match_info['filename'])
    range_header = request.headers.get('Range')
    start = 0
    if range_header:
        match = re.search(r'bytes=(\d+)-', range_header)
        if match: start = int(match.group(1))
    headers = {
        'Content-Disposition': f'attachment; filename="{url_filename}"',
        'Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4',
        'Content-Length': str(msg.file.size - start), 'Connection': 'keep-alive'
    }
    if request.method == "HEAD": return web.Response(headers=headers)
    resp = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await resp.prepare(request)
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576):
            await resp.write(chunk)
    except: pass
    return resp

# --- 8. BOT HANDLERS ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id not in ALLOWED_USERS: return
    if event.file:
        await event.reply(f"📂 **File Detected:** `{event.file.name or 'video.mp4'}`",
            buttons=[
                [Button.inline("🔗 Get Direct Link", data=f"link_{event.id}")],
                [Button.inline("☁️ Vidmoly", data=f"moly_{event.id}"), Button.inline("🛡️ Cloudflare R2", data=f"r2_{event.id}")]
            ])
        return

    if event.text and event.text.startswith("http"):
        async with global_semaphore:
            url = event.text.split(" -n ")[0].strip()
            name = event.text.split(" -n ")[1].strip() if " -n " in event.text else "video.mp4"
            if not "." in name: name += ".mp4"
            msg = await event.reply("🔗 **Queued: Leeching...**")
            try:
                async with ClientSession() as sess:
                    async with sess.get(url) as r:
                        f_size = int(r.headers.get("Content-Length", 0))
                        start_t = time.time()
                        with open(name, 'wb') as f:
                            async for chunk in r.content.iter_chunked(1024*1024):
                                f.write(chunk)
                                if f.tell() % (10 * 1024 * 1024) == 0:
                                    await msg.edit(get_status_text("Leeching URL", name, f.tell(), f_size, start_t))
                up_file = await fast_upload(client, name, msg, name)
                await client.send_file(event.chat_id, file=up_file, caption=f"✅ `{name}`", supports_streaming=True, attributes=[types.DocumentAttributeVideo(duration=0, w=1280, h=720, supports_streaming=True)])
                await msg.delete()
            except Exception as e: await event.reply(f"❌ Error: {e}")
            finally:
                if os.path.exists(name): os.remove(name)

@client.on(events.CallbackQuery)
async def on_callback(event):
    data = event.data.decode()
    msg_id = int(data.split("_")[1])
    await event.answer("Processing Request...")

    if data.startswith("link"):
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': tg_msg, 'timestamp': time.time()}
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/')
        if not base: base = f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        await client.send_message(event.chat_id, f"🚀 **Direct Link:**\n\n`{base}/{code}/{quote(tg_msg.file.name or 'video.mp4')}`", reply_to=msg_id)
        
    elif data.startswith("r2") or data.startswith("moly"):
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        async with global_semaphore:
            filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
            status = await event.respond(f"⬇️ **Downloading from TG...**")
            start_t = time.time()
            try:
                with open(filename, 'wb') as f:
                    async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                        f.write(chunk)
                        if f.tell() % (10 * 1024 * 1024) == 0:
                            await status.edit(get_status_text("TG Download", filename, f.tell(), tg_msg.file.size, start_t))

                if data.startswith("r2"):
                    r2_link = await upload_to_r2(filename, status)
                    await status.edit(f"✅ **Cloudflare R2 Done!**\n\n🎬 `{filename}`\n🔗 `{r2_link}`")
                else:
                    code = await upload_to_vidmoly(filename, status, VIDMOLY_API_KEY)
                    await status.edit(f"✅ **Vidmoly Done!**\n🔗 `https://vidmoly.biz/embed-{code}.html`", buttons=[[Button.url("🖼 Open", f"https://vidmoly.biz/embed-{code}.html")]])
            except Exception as e: await status.edit(f"❌ Error: {e}")
            finally:
                if os.path.exists(filename): os.remove(filename)

# --- 9. STARTUP ---
async def main():
    app = web.Application(); app.add_routes(routes)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

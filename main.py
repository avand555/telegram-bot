import os
import secrets
import asyncio
import mimetypes
import time
import re
import math
import random
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest
from telethon.tl.types import InputFileBig, InputFile

# Web & API Imports
from aiohttp import web, ClientSession, FormData
import aiohttp

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
VIDMOLY_API_KEY = "547285kdjw3pg3e303au64"

ALLOWED_USERS = {716887656, 1053544356} 
ADMIN_ID = 716887656  
EXPIRATION_TIME = 24 * 60 * 60 

cancel_tasks = {}
link_storage = {}
routes = web.RouteTableDef()

# --- SPEED METER HELPER ---
def get_status_text(action, filename, current, total, start_time):
    now = time.time()
    diff = now - start_time
    if diff <= 0: diff = 0.001
    
    perc = (current / total) * 100 if total > 0 else 0
    speed = current / diff # bytes per second
    
    # Progress Bar
    finished_blocks = int(perc // 10)
    remaining_blocks = 10 - finished_blocks
    p_bar = "■" * finished_blocks + "□" * remaining_blocks
    
    # Formatting
    def human_size(bytes):
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes < 1024: return f"{bytes:.2f} {unit}"
            bytes /= 1024
    
    return (
        f"🚀 **{action}**\n"
        f"📦 `{filename}`\n\n"
        f"🌀 **Progress:** `[{p_bar}] {perc:.2f}%`\n"
        f"⚡ **Speed:** `{human_size(speed)}/s`\n"
        f"📂 **Size:** `{human_size(current)} / {human_size(total)}`"
    )

# --- VIDMOLY PROGRESS WRAPPER ---
class ProgressFile:
    def __init__(self, filename, callback):
        self._filename = filename
        self._size = os.path.getsize(filename)
        self._read_bytes = 0
        self._callback = callback
    def __len__(self): return self._size
    def read(self, size):
        with open(self._filename, 'rb') as f:
            f.seek(self._read_bytes)
            chunk = f.read(size)
        self._read_bytes += len(chunk)
        asyncio.create_task(self._callback(self._read_bytes, self._size))
        return chunk

# --- FAST TG UPLOADER (PARALLEL) ---
async def fast_upload(client, file_path, msg, filename):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024
    total_parts = math.ceil(file_size / part_size)
    file_id = random.getrandbits(63)
    start_time = time.time()
    
    uploaded_bytes = 0
    # Use semaphore to limit parallel chunks (saves Koyeb RAM)
    sem = asyncio.Semaphore(10) 

    async def upload_part(idx, chunk):
        nonlocal uploaded_bytes
        async with sem:
            if file_size > 10 * 1024 * 1024:
                await client(SaveBigFilePartRequest(file_id, idx, total_parts, chunk))
            else:
                await client(SaveFilePartRequest(file_id, idx, chunk))
            uploaded_bytes += len(chunk)

    tasks = []
    last_update = 0
    with open(file_path, 'rb') as f:
        for i in range(total_parts):
            chunk = f.read(part_size)
            tasks.append(asyncio.create_task(upload_part(i, chunk)))
            
            # Throttle UI updates to avoid Telegram FloodWait
            if time.time() - last_update > 4:
                try:
                    await msg.edit(get_status_text("Uploading to TG", filename, uploaded_bytes, file_size, start_time))
                    last_update = time.time()
                except: pass

    await asyncio.gather(*tasks)
    return InputFileBig(file_id, total_parts, filename) if file_size > 10*1024*1024 else InputFile(file_id, total_parts, filename, '')

# --- WEB SERVER (DIRECT LINKS) ---
@routes.get('/')
async def root(request): return web.Response(text="✅ Bot is Online")

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data: return web.Response(text="Link Expired", status=410)
    
    msg = data['msg']
    file_name = unquote(request.match_info['filename'])
    file_size = msg.file.size
    range_header = request.headers.get('Range')
    start = 0
    if range_header:
        match = re.search(r'bytes=(\d+)-', range_header)
        if match: start = int(match.group(1))

    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"',
        'Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4',
        'Content-Length': str(file_size - start)
    }
    resp = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await resp.prepare(request)
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576):
            await resp.write(chunk)
    except: pass
    return resp

# --- VIDMOLY UPLOAD ---
async def do_vidmoly_upload(event, tg_msg):
    filename = re.sub(r'[\\/*?:"<>|]', "", tg_msg.file.name or "video.mp4")
    status = await event.edit(f"⬇️ **Starting TG Download...**")
    start_t = time.time()
    
    try:
        with open(filename, 'wb') as f:
            last_up = 0
            async for chunk in client.iter_download(tg_msg.media, request_size=1048576):
                f.write(chunk)
                if time.time() - last_up > 4:
                    await status.edit(get_status_text("Downloading from TG", filename, f.tell(), tg_msg.file.size, start_t))
                    last_up = time.time()

        await status.edit(f"⬆️ **Connecting to Vidmoly...**")
        async with ClientSession() as sess:
            async with sess.get(f"https://vidmoly.me/api/upload/server?key={VIDMOLY_API_KEY}") as r:
                upload_url = (await r.json(content_type=None))['result']
            
            start_t = time.time()
            last_up = 0
            async def moly_cb(curr, tot):
                nonlocal last_up
                if time.time() - last_up > 4:
                    try:
                        await status.edit(get_status_text("Vidmoly Blasting", filename, curr, tot, start_t))
                        last_up = time.time()
                    except: pass

            data = FormData()
            data.add_field('api_key', VIDMOLY_API_KEY)
            data.add_field('file', ProgressFile(filename, moly_cb), filename=filename)
            
            async with sess.post(upload_url, data=data) as r:
                res = await r.text()
                match = re.search(r'name="fn">([a-zA-Z0-9]+)<', res)
                if match:
                    code = match.group(1)
                    await status.edit(f"✅ **Vidmoly Uploaded!**\n\n🎬 `{filename}`\n🔗 `https://vidmoly.biz/embed-{code}.html`", buttons=[[Button.url("🖼 Open Player", f"https://vidmoly.biz/embed-{code}.html")]])
                else: await status.edit("❌ Vidmoly Error: API issue.")
    except Exception as e: await status.edit(f"❌ Error: {e}")
    finally:
        if os.path.exists(filename): os.remove(filename)

# --- BOT HANDLERS ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id not in ALLOWED_USERS: return

    if event.file:
        await event.reply(
            f"📂 **File:** `{event.file.name or 'video.mp4'}`",
            buttons=[
                [Button.inline("🔗 Direct Link", data=f"link_{event.id}")],
                [Button.inline("☁️ Vidmoly Upload", data=f"moly_{event.id}")]
            ]
        )
        return

    if event.text and event.text.startswith("http"):
        url = event.text.split(" -n ")[0].strip()
        name = event.text.split(" -n ")[1].strip() if " -n " in event.text else "video.mp4"
        if not "." in name: name += ".mp4"
        
        msg = await event.reply("🔗 **Connecting to URL...**")
        start_t = time.time()
        try:
            async with ClientSession() as sess:
                async with sess.get(url) as r:
                    f_size = int(r.headers.get("Content-Length", 0))
                    last_up = 0
                    with open(name, 'wb') as f:
                        async for chunk in r.content.iter_chunked(1024*1024):
                            f.write(chunk)
                            if time.time() - last_up > 4:
                                await msg.edit(get_status_text("Leeching URL", name, f.tell(), f_size, start_t))
                                last_up = time.time()
            
            up_file = await fast_upload(client, name, msg, name)
            await client.send_file(event.chat_id, file=up_file, caption=f"✅ `{name}`", supports_streaming=True)
            await msg.delete()
        except Exception as e: await event.reply(f"❌ Error: {e}")
        finally:
            if os.path.exists(name): os.remove(name)

@client.on(events.CallbackQuery)
async def on_callback(event):
    data = event.data.decode()
    msg_id = int(data.split("_")[1])
    tg_msg = await client.get_messages(event.chat_id, ids=msg_id)

    if data.startswith("link"):
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': tg_msg, 'timestamp': time.time()}
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/')
        if not base: base = f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        hotlink = f"{base}/{code}/{quote(tg_msg.file.name or 'video.mp4')}"
        await event.respond(f"🚀 **Direct Link:**\n`{hotlink}`", list_alerts=True)
    elif data.startswith("moly"):
        await do_vidmoly_upload(event, tg_msg)

async def main():
    app = web.Application(); app.add_routes(routes)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

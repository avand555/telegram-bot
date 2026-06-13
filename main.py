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

# Web Server Imports
from aiohttp import web, ClientSession, TCPConnector

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_START_TIME = time.time()

ALLOWED_USERS = {716887656, 1053544356} 
ADMIN_ID = 716887656  

cancel_tasks = {}
routes = web.RouteTableDef()
link_storage = {}

# --- SETUP CLIENT ---
client = TelegramClient(
    'bot_session', int(API_ID), API_HASH,
    connection=ConnectionTcpFull,
    device_model="Koyeb HighSpeed",
    system_version="Linux",
    app_version="3.0"
)

# --- SPEED UPLOAD ENGINE ---
async def upload_file_fast(client, file_path, msg, start_time, filename, cancel_event):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024 
    total_parts = math.ceil(file_size / part_size)
    file_id = random.getrandbits(63)
    uploaded_bytes = 0
    
    async def upload_part(idx, chunk):
        nonlocal uploaded_bytes
        if cancel_event.is_set(): raise asyncio.CancelledError()
        if file_size > 10 * 1024 * 1024:
            await client(SaveBigFilePartRequest(file_id, idx, total_parts, chunk))
        else:
            await client(SaveFilePartRequest(file_id, idx, chunk))
        uploaded_bytes += len(chunk)

    # 15 workers for fast upload on Koyeb
    semaphore = asyncio.Semaphore(15)
    
    async def worker(idx, chunk):
        async with semaphore:
            await upload_part(idx, chunk)

    tasks = []
    with open(file_path, 'rb') as f:
        for i in range(total_parts):
            chunk = f.read(part_size)
            tasks.append(worker(i, chunk))
            if len(tasks) >= 15:
                await asyncio.gather(*tasks)
                tasks = []
                await progress_callback(uploaded_bytes, file_size, msg, start_time, filename, "⬆️ Uploading")
    
    if tasks: await asyncio.gather(*tasks)
    return InputFileBig(file_id, total_parts, os.path.basename(file_path)) if file_size > 10*1024*1024 else InputFile(file_id, total_parts, os.path.basename(file_path), '')

async def progress_callback(current, total, event, start_time, filename, action="🔄"):
    now = time.time()
    if now - start_time['last_update'] < 5: return
    start_time['last_update'] = now
    perc = (current / total) * 100 if total else 0
    speed = (current / (now - start_time['start'])) / 1024 / 1024
    try:
        await event.edit(f"{action} `{filename}`\n📊 `{perc:.2f}%` 🚀 `{speed:.2f} MB/s`")
    except: pass

# --- WEB SERVER (STREAMING) ---
@routes.get('/')
async def root(request): return web.Response(text="✅ Online")

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data: return web.Response(text="Link Expired", status=410)
    
    # This allows you to change the name in the URL and have IDM/Browser see it
    file_name = unquote(request.match_info['filename'])
    msg = data['msg']
    file_size = msg.file.size
    
    range_header = request.headers.get('Range')
    start = 0
    if range_header:
        match = re.search(r'bytes=(\d+)-', range_header)
        if match: start = int(match.group(1))

    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"',
        'Accept-Ranges': 'bytes',
        'Content-Type': 'video/mp4',
        'Content-Length': str(file_size - start)
    }
    
    resp = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await resp.prepare(request)
    
    # 1MB alignment fix for IDM
    try:
        async for chunk in client.iter_download(msg.media, offset=(start//1048576)*1048576, request_size=1048576):
            await resp.write(chunk)
    except: pass
    return resp

# --- BOT LOGIC ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id not in ALLOWED_USERS: return
    
    # 1. GENERATE LINK (Forward file to bot)
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': event, 'timestamp': time.time()}
        name = quote(event.file.name or "video.mp4")
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/')
        if not base: base = f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        await event.reply(f"🔗 **Link:** `{base}/{code}/{name}`\n\n💡 *Change the name at the end of the link to rename!*")
        return

    # 2. LEECH (URL -n name)
    if event.text and event.text.startswith("http"):
        text = event.text.strip()
        url = text.split(" -n ")[0]
        filename = text.split(" -n ")[1] if " -n " in text else "video.mp4"
        if not "." in filename: filename += ".mp4"

        msg = await event.reply("🔗 **Leeching...**")
        cancel_event = asyncio.Event()
        cancel_tasks[event.chat_id] = cancel_event

        try:
            async with ClientSession() as sess:
                async with sess.get(url) as r:
                    f_size = int(r.headers.get("Content-Length", 0))
                    with open(filename, 'wb') as f:
                        start_t = {'start': time.time(), 'last_update': 0}
                        async for chunk in r.content.iter_chunked(1024*1024):
                            if cancel_event.is_set(): raise asyncio.CancelledError()
                            f.write(chunk)
                            await progress_callback(f.tell(), f_size, msg, start_t, filename, "⬇️ Down")
            
            start_t = {'start': time.time(), 'last_update': 0}
            up_file = await upload_file_fast(client, filename, msg, start_t, filename, cancel_event)
            
            # Use specific attributes to force video player
            await client.send_file(
                event.chat_id, file=up_file, caption=f"✅ `{filename}`", 
                supports_streaming=True,
                attributes=[types.DocumentAttributeVideo(
                    duration=0, w=1280, h=720, supports_streaming=True
                )]
            )
            await msg.delete()
        except Exception as e: await event.reply(f"❌ Error: {e}")
        finally:
            if os.path.exists(filename): os.remove(filename)
            cancel_tasks.pop(event.chat_id, None)

# --- START ---
async def main():
    app = web.Application(); app.add_routes(routes)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

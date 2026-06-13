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

# Web Server & Vidmoly Imports
from aiohttp import web, ClientSession, FormData
import aiohttp

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
VIDMOLY_API_KEY = os.environ.get("VIDMOLY_API_KEY", "547285kdjw3pg3e303au64")

BOT_START_TIME = time.time()
ALLOWED_USERS = {716887656, 1053544356} 
ADMIN_ID = 716887656  
EXPIRATION_TIME = 24 * 60 * 60 

cancel_tasks = {}
routes = web.RouteTableDef()
link_storage = {}

# --- HELPER FUNCTIONS ---
def get_readable_time(seconds: int) -> str:
    result = ""
    (days, remainder) = divmod(seconds, 86400)
    if days: result += f"{int(days)}d "
    (hours, remainder) = divmod(remainder, 3600)
    if hours: result += f"{int(hours)}h "
    (minutes, seconds) = divmod(remainder, 60)
    if minutes: result += f"{int(minutes)}m "
    result += f"{int(seconds)}s"
    return result

async def progress_callback(current, total, event, start_time, filename, action="🔄 Processing"):
    now = time.time()
    if now - start_time['last_update'] < 4: return
    start_time['last_update'] = now
    percentage = current * 100 / total if total else 0
    speed = (current / (now - start_time['start'])) / 1024 / 1024 if (now - start_time['start']) > 0 else 0
    uploaded = current / 1024 / 1024
    total_size = total / 1024 / 1024 if total else 0
    try:
        await event.edit(
            f"{action}...\n\n🎬 `{filename}`\n📊 `{percentage:.2f}%` 🚀 `{speed:.2f} MB/s`\n💾 `{uploaded:.2f} / {total_size:.2f} MB`",
            buttons=[[Button.inline("❌ Cancel", data="cancel_leech")]]
        )
    except: pass

# --- SETUP CLIENT ---
client = TelegramClient(
    'bot_session', int(API_ID), API_HASH,
    connection=ConnectionTcpFull, use_ipv6=False,
    device_model="Koyeb Server", system_version="Linux", app_version="10.0.0"
)

# --- UPLOAD ENGINE ---
async def upload_file_fast(client, file_path, msg, start_time, filename, cancel_event):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024 
    total_parts = math.ceil(file_size / part_size)
    file_id = random.getrandbits(63)
    uploaded_bytes = 0
    async def upload_part(idx, chunk):
        nonlocal uploaded_bytes
        if cancel_event.is_set(): raise asyncio.CancelledError()
        if file_size > 10 * 1024 * 1024: await client(SaveBigFilePartRequest(file_id, idx, total_parts, chunk))
        else: await client(SaveFilePartRequest(file_id, idx, chunk))
        uploaded_bytes += len(chunk)
    
    tasks = []
    with open(file_path, 'rb') as f:
        for i in range(total_parts):
            if cancel_event.is_set(): raise asyncio.CancelledError()
            chunk = f.read(part_size)
            tasks.append(upload_part(i, chunk))
            if len(tasks) >= 8:
                await asyncio.gather(*tasks)
                tasks = []
                await progress_callback(uploaded_bytes, file_size, msg, start_time, filename, "⬆️ **Uploading**")
    if tasks: await asyncio.gather(*tasks)
    return InputFileBig(file_id, total_parts, os.path.basename(file_path)) if file_size > 10*1024*1024 else InputFile(file_id, total_parts, os.path.basename(file_path), '')

# --- WEB SERVER ROUTES ---
@routes.get('/')
async def root(request):
    return web.Response(text="✅ Bot is Online", status=200)

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data: return web.Response(text="❌ Expired", status=410)
    msg, file_name, file_size = data['msg'], unquote(request.match_info['filename']), data['msg'].file.size
    range_header = request.headers.get('Range')
    start, end = 0, file_size - 1
    if range_header:
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            if match.group(2): end = int(match.group(2))
    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"',
        'Content-Type': mimetypes.guess_type(file_name)[0] or 'application/octet-stream',
        'Content-Length': str(end - start + 1), 'Accept-Ranges': 'bytes'
    }
    response = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await response.prepare(request)
    offset = (start // 1048576) * 1048576
    skip, bytes_to_send = start - offset, end - start + 1
    try:
        async for chunk in client.iter_download(msg.media, offset=offset, request_size=1048576):
            if skip > 0:
                chunk = chunk[skip:]; skip = 0
            if bytes_to_send <= len(chunk):
                await response.write(chunk[:bytes_to_send]); break
            await response.write(chunk); bytes_to_send -= len(chunk)
    except: pass
    return response

# --- BOT HANDLERS ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id not in ALLOWED_USERS: return
    if event.text == '/start':
        await event.reply("✅ **Bot Ready!**\nSend link to Leech or forward file for IDM link.")
        return

    # VIDMOLY
    if event.text and event.text.startswith('/vidmoly'):
        if not event.is_reply: return await event.reply("⚠️ Reply to a file.")
        target = await event.get_reply_message()
        if not target or not target.file: return
        filename = re.sub(r'[\\/*?:"<>|]', "", target.file.name or "video.mp4")
        msg = await event.reply(f"⬇️ **Downloading...**")
        start_time = {'start': time.time(), 'last_update': 0}
        try:
            with open(filename, 'wb') as f:
                async for chunk in client.iter_download(target.media, request_size=1048576):
                    f.write(chunk)
                    await progress_callback(f.tell(), target.file.size, msg, start_time, filename, "⬇️ **TG Download**")
            await msg.edit(f"⬆️ **Uploading to Vidmoly...**")
            async with ClientSession() as session:
                async with session.get(f"https://vidmoly.me/api/upload/server?key={VIDMOLY_API_KEY}") as r:
                    res = await r.json(content_type=None)
                    upload_url = res['result']
                data = FormData()
                data.add_field('api_key', VIDMOLY_API_KEY)
                data.add_field('file', open(filename, 'rb'), filename=filename)
                async with session.post(upload_url, data=data) as r:
                    res_text = await r.text()
                    match = re.search(r'name="fn">([a-zA-Z0-9]+)<', res_text)
                    if match:
                        f_code = match.group(1)
                        await msg.edit(f"✅ **Complete!**\n\n🔗 **Embed:** https://vidmoly.biz/embed-{f_code}.html")
        except Exception as e: await msg.edit(f"❌ Error: {e}")
        finally:
            if os.path.exists(filename): os.remove(filename)
        return

    # LEECH (URL)
    if event.text and event.text.startswith("http"):
        url = event.text.strip()
        msg = await event.reply("🔗 **Connecting...**")
        cancel_event = asyncio.Event()
        cancel_tasks[event.chat_id] = cancel_event
        try:
            async with ClientSession() as session:
                async with session.get(url, timeout=15) as resp:
                    # CHECK FOR HTML OR ERRORS
                    content_type = resp.headers.get("Content-Type", "")
                    if "text/html" in content_type:
                        return await msg.edit("❌ **Error:** The link provided is a webpage (HTML), not a direct video file.")
                    if resp.status != 200:
                        return await msg.edit(f"❌ **Link Error:** Server returned status `{resp.status}`")

                    filename = unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
                    # Ensure extension is video for player compatibility
                    if not any(filename.lower().endswith(x) for x in ['.mp4', '.mkv', '.avi', '.mov']):
                        filename += ".mp4"
                    
                    file_size = int(resp.headers.get("Content-Length", 0))
                    
                    with open(filename, 'wb') as f:
                        start_time = {'start': time.time(), 'last_update': 0}
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            if cancel_event.is_set(): raise asyncio.CancelledError()
                            f.write(chunk)
                            await progress_callback(f.tell(), file_size, msg, start_time, filename, "⬇️ **Downloading**")
            
            # UPLOAD AS VIDEO
            start_time = {'start': time.time(), 'last_update': 0}
            up_file = await upload_file_fast(client, filename, msg, start_time, filename, cancel_event)
            
            await client.send_file(
                event.chat_id, 
                file=up_file, 
                caption=f"✅ `{filename}`", 
                supports_streaming=True, # Makes it playable
                attributes=[types.DocumentAttributeVideo(
                    duration=0, # Duration 0 is fine, Telegram calculates it on play
                    w=1280, h=720, # Dummy size to trigger video player
                    supports_streaming=True
                )]
            )
            await msg.delete()
        except Exception as e: await msg.edit(f"❌ Error: {e}")
        finally:
            if os.path.exists(filename): os.remove(filename)
            cancel_tasks.pop(event.chat_id, None)
        return

    # DIRECT LINK / RENAME
    if event.file or (event.text and event.text.startswith('/rename')):
        target = await event.get_reply_message() if event.is_reply else event
        if not target or not target.file: return
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': target, 'timestamp': time.time()}
        name = event.text.split(' ', 1)[1] if event.text.startswith('/rename') else target.file.name or "video.mp4"
        if not '.' in name: name += ".mp4"
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/')
        if not base: base = f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        await event.reply(f"✅ **Link:** `{base}/{code}/{quote(name)}`")

# --- STARTUP ---
async def main():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8000))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot Connected")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

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
from aiohttp import web, ClientSession, FormData, MultipartWriter
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

# --- CUSTOM TRACKER FOR VIDMOLY UPLOAD SPEED ---
class ProgressFile(object):
    def __init__(self, filename, callback):
        self._filename = filename
        self._size = os.path.getsize(filename)
        self._read_bytes = 0
        self._callback = callback

    def __len__(self):
        return self._size

    def read(self, size):
        chunk = open(self._filename, 'rb')
        chunk.seek(self._read_bytes)
        data = chunk.read(size)
        self._read_bytes += len(data)
        asyncio.create_task(self._callback(self._read_bytes, self._size))
        return data

# --- HELPER FUNCTIONS ---
def get_readable_time(seconds: int) -> str:
    result = ""
    (days, remainder) = divmod(seconds, 86400)
    days = int(days)
    if days != 0: result += f"{days}d "
    (hours, remainder) = divmod(remainder, 3600)
    hours = int(hours)
    if hours != 0: result += f"{hours}h "
    (minutes, seconds) = divmod(remainder, 60)
    minutes = int(minutes)
    if minutes != 0: result += f"{minutes}m "
    seconds = int(seconds)
    result += f"{seconds}s"
    return result

async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        current_time = time.time()
        keys_to_delete =[k for k, v in link_storage.items() if current_time - v['timestamp'] > EXPIRATION_TIME]
        for key in keys_to_delete: del link_storage[key]

# --- UI PROGRESS BAR ---
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
            f"{action}...\n\n"
            f"🎬 `{filename}`\n"
            f"📊 `{percentage:.2f}%`\n"
            f"🚀 `{speed:.2f} MB/s`\n"
            f"💾 `{uploaded:.2f} / {total_size:.2f} MB`",
            buttons=[[Button.inline("❌ Cancel", data="cancel_leech")]]
        )
    except Exception: pass

# --- SETUP CLIENT ---
client = TelegramClient(
    'bot_session', 
    int(API_ID), 
    API_HASH, 
    connection=ConnectionTcpFull,
    use_ipv6=False, 
    device_model="Koyeb Fast Server",
    system_version="Linux",
    app_version="9.0.0"
)

# --- IDM MULTI-PART DOWNLOAD ENGINE (Leech) ---
async def download_chunk(url, start, end, filename, progress, cancel_event):
    headers = {'Range': f'bytes={start}-{end}'}
    async with ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            with open(filename, 'r+b') as f:
                f.seek(start)
                async for chunk in resp.content.iter_chunked(1024 * 512):
                    if cancel_event.is_set(): raise asyncio.CancelledError()
                    f.write(chunk)
                    progress['downloaded'] += len(chunk)

# --- FAST UPLOAD ENGINE (Telegram) ---
async def upload_file_fast(client, file_path, msg, start_time, filename, cancel_event):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024 
    total_parts = math.ceil(file_size / part_size)
    is_big = file_size > 10 * 1024 * 1024
    file_id = random.getrandbits(63)
    uploaded_bytes = 0
    
    async def upload_part(part_index, chunk):
        nonlocal uploaded_bytes
        if cancel_event.is_set(): raise asyncio.CancelledError()
        if is_big: await client(SaveBigFilePartRequest(file_id, part_index, total_parts, chunk))
        else: await client(SaveFilePartRequest(file_id, part_index, chunk))
        uploaded_bytes += len(chunk)

    tasks =[]
    with open(file_path, 'rb') as f:
        for i in range(total_parts):
            if cancel_event.is_set(): raise asyncio.CancelledError()
            chunk = f.read(part_size)
            tasks.append(upload_part(i, chunk))
            if len(tasks) >= 12:
                await asyncio.gather(*tasks)
                tasks =[]
                await progress_callback(uploaded_bytes, file_size, msg, start_time, filename, "⬆️ **Uploading**")
        if tasks: await asyncio.gather(*tasks)
    return InputFileBig(file_id, total_parts, os.path.basename(file_path)) if is_big else InputFile(file_id, total_parts, os.path.basename(file_path), '')

# --- WEB SERVER (Direct Links) ---
@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data: return web.Response(text="❌ Expired", status=410)
    
    message = data['msg']
    file_name = unquote(request.match_info['filename']) 
    file_size = message.file.size
    
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
    skip = start - offset
    bytes_to_send = end - start + 1

    try:
        async for chunk in client.iter_download(message.media, offset=offset, request_size=1048576):
            if skip > 0:
                chunk = chunk[skip:]; skip = 0
            if bytes_to_send <= len(chunk):
                await response.write(chunk[:bytes_to_send]); break
            await response.write(chunk); bytes_to_send -= len(chunk)
    except: pass
    return response

# --- BOT HANDLERS ---
@client.on(events.CallbackQuery(pattern="cancel_leech"))
async def cancel_handler(event):
    if event.chat_id in cancel_tasks:
        cancel_tasks[event.chat_id].set()
        await event.edit("🛑 **Task Cancelled.**")

@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id not in ALLOWED_USERS: return
    
    if event.text == '/start':
        await event.reply("✅ **IDM & Vidmoly Bot Ready!**\nReply to any video with `/vidmoly` or `/rename name.mp4`")
        return

    # 1. VIDMOLY UPLOAD (Reply with /vidmoly)
    if event.text and event.text.startswith('/vidmoly'):
        if not event.is_reply:
            return await event.reply("⚠️ Reply to a file with `/vidmoly`")
            
        target_msg = await event.get_reply_message()
        if not target_msg or not target_msg.file: return
        
        filename = re.sub(r'[\\/*?:"<>|]', "", target_msg.file.name or "video.mp4")
        msg = await event.reply(f"⬇️ **Downloading from Telegram...**")
        start_time = {'start': time.time(), 'last_update': 0}
        
        try:
            # Step 1: TG -> Server
            with open(filename, 'wb') as f:
                async for chunk in client.iter_download(target_msg.media, request_size=1048576):
                    f.write(chunk)
                    await progress_callback(f.tell(), target_msg.file.size, msg, start_time, filename, "⬇️ **TG Download**")

            # Step 2: Server -> Vidmoly
            await msg.edit(f"⬆️ **Uploading to Vidmoly...**")
            start_time = {'start': time.time(), 'last_update': 0}
            
            async with ClientSession() as session:
                async with session.get(f"https://vidmoly.me/api/upload/server?key={VIDMOLY_API_KEY}") as r:
                    res = await r.json(content_type=None)
                    upload_url = res['result']
                
                # Wrap file to track upload speed
                wrapped_file = ProgressFile(filename, lambda c, t: progress_callback(c, t, msg, start_time, filename, "⬆️ **Vidmoly Upload**"))
                
                data = FormData()
                data.add_field('api_key', VIDMOLY_API_KEY)
                data.add_field('file', wrapped_file, filename=filename)
                
                async with session.post(upload_url, data=data) as r:
                    text_res = await r.text()
                    match = re.search(r'name="fn">([a-zA-Z0-9]+)<', text_res)
                    if match:
                        f_code = match.group(1)
                        # EXACT EMBED LINK AS REQUESTED
                        embed = f"https://vidmoly.biz/embed-{f_code}.html"
                        direct = f"https://vidmoly.me/{f_code}.html"
                        await msg.edit(f"✅ **Upload Complete!**\n\n🎬 `{filename}`\n\n🔗 **Direct:** {direct}\n🖼 **Embed:** {embed}")
                    else:
                        await msg.edit("❌ Upload failed. Check Vidmoly API.")
        except Exception as e: await msg.edit(f"❌ Error: {e}")
        finally:
            if os.path.exists(filename): os.remove(filename)
        return

    # 2. LEECH (URL -> TG)
    if event.text and event.text.startswith("http"):
        url = event.text.strip()
        msg = await event.reply("🔗 **Connecting...**")
        cancel_event = asyncio.Event()
        cancel_tasks[event.chat_id] = cancel_event
        
        try:
            async with ClientSession() as session:
                async with session.get(url) as resp:
                    filename = unquote(url.split("/")[-1].split("?")[0]) or "video.mp4"
                    file_size = int(resp.headers.get("Content-Length", 0))
                    
                    with open(filename, 'wb') as f:
                        start_time = {'start': time.time(), 'last_update': 0}
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            if cancel_event.is_set(): raise asyncio.CancelledError()
                            f.write(chunk)
                            await progress_callback(f.tell(), file_size, msg, start_time, filename, "⬇️ **Downloading**")
            
            start_time = {'start': time.time(), 'last_update': 0}
            up_file = await upload_file_fast(client, filename, msg, start_time, filename, cancel_event)
            await client.send_file(event.chat_id, file=up_file, caption=f"✅ `{filename}`", supports_streaming=True)
            await msg.delete()
        except Exception as e: await msg.edit(f"❌ Error: {e}")
        finally:
            if os.path.exists(filename): os.remove(filename)
            cancel_tasks.pop(event.chat_id, None)
        return

    # 3. STREAM/RENAME (Forward -> Direct Link)
    if event.file or (event.text and event.text.startswith('/rename')):
        target = await event.get_reply_message() if event.is_reply else event
        if not target or not target.file: return
        
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': target, 'timestamp': time.time()}
        
        name = event.text.split(' ', 1)[1] if event.text.startswith('/rename') else target.file.name or "video.mp4"
        if not '.' in name: name += ".mp4"
        
        base = os.environ.get("KOYEB_PUBLIC_URL", "").rstrip('/')
        if not base: base = f"https://{os.environ.get('KOYEB_APP_NAME')}.koyeb.app"
        
        hotlink = f"{base}/{code}/{quote(name)}"
        await event.reply(f"✅ **Direct Link Generated!**\n\n📂 `{name}`\n🔗 `{hotlink}`")

# --- STARTUP ---
async def main():
    asyncio.create_task(cleanup_loop())
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8000))).start()
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

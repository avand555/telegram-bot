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
from aiohttp import web, ClientSession

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_START_TIME = time.time()

# --- USER AND ADMIN MANAGEMENT ---
ALLOWED_USERS = {716887656, 1053544356} 
ADMIN_ID = 716887656  
EXPIRATION_TIME = 24 * 60 * 60 

# Global Dictionary
cancel_tasks = {}
routes = web.RouteTableDef()
link_storage = {}

# --- SETUP ---
client = TelegramClient(
    'bot_session', 
    int(API_ID), 
    API_HASH, 
    connection=ConnectionTcpFull,
    use_ipv6=False, 
    device_model="Koyeb Fast Server",
    system_version="Linux",
    app_version="7.0.0 (IDM Bulletproof)"
)

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

# --- IDM MULTI-PART DOWNLOAD ENGINE ---
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

async def update_download_progress(msg, progress, total, filename, start_time, cancel_event):
    while progress['downloaded'] < total:
        if cancel_event.is_set(): break
        await progress_callback(progress['downloaded'], total, msg, start_time, filename, "⬇️ **Downloading (IDM Engine)**")
        await asyncio.sleep(4)
    await progress_callback(total, total, msg, start_time, filename, "⬇️ **Downloading (IDM Engine)**")

# --- FAST UPLOAD ENGINE ---
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
        if is_big:
            await client(SaveBigFilePartRequest(file_id, part_index, total_parts, chunk))
        else:
            await client(SaveFilePartRequest(file_id, part_index, chunk))
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
                await progress_callback(uploaded_bytes, file_size, msg, start_time, filename, "⬆️ **Uploading (Fast Engine)**")
        
        if tasks:
            await asyncio.gather(*tasks)
            await progress_callback(uploaded_bytes, file_size, msg, start_time, filename, "⬆️ **Uploading (Fast Engine)**")
            
    name = os.path.basename(file_path)
    return InputFileBig(file_id, total_parts, name) if is_big else InputFile(file_id, total_parts, name, '')


# --- WEB SERVER ROUTES (COMPLETELY REWRITTEN FOR IDM) ---
@routes.get('/')
async def root(request):
    return web.Response(text="✅ Fast Bot is Online on Koyeb")

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    
    if not data or (time.time() - data['timestamp'] > EXPIRATION_TIME):
        if data: del link_storage[code]
        return web.Response(text="❌ Link Expired or Bot Restarted", status=410)
    
    message = data['msg']
    file_name = unquote(request.match_info['filename']) 
    file_size = message.file.size if getattr(message, 'file', None) else 0
    
    if file_size == 0:
        return web.Response(text="❌ Invalid Telegram File", status=400)

    mime_type, _ = mimetypes.guess_type(file_name)
    if not mime_type: mime_type = 'application/octet-stream'

    range_header = request.headers.get('Range')
    start = 0
    end = file_size - 1

    if range_header:
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            if match.group(2):
                end = int(match.group(2))
                
    if start >= file_size:
        return web.Response(status=416, headers={'Content-Range': f'bytes */{file_size}'})

    content_length = end - start + 1
    
    # "attachment" forces IDM and Browsers to treat it as a pure download
    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"', 
        'Content-Type': mime_type,
        'Content-Length': str(content_length),
        'Accept-Ranges': 'bytes'
    }

    if range_header:
        headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response = web.StreamResponse(status=206, headers=headers)
    else:
        response = web.StreamResponse(status=200, headers=headers)
        
    try:
        await response.prepare(request)
    except Exception:
        return response

    # --- PERFECT TELEGRAM 1MB ALIGNMENT ---
    request_size = 1048576 # Exactly 1 MB
    offset = (start // request_size) * request_size
    skip = start - offset
    bytes_to_send = content_length

    try:
        # Pass request_size directly to Telegram API
        async for chunk in client.iter_download(message.media, offset=offset, request_size=request_size):
            if skip > 0:
                chunk = chunk[skip:]
                skip = 0
            
            if bytes_to_send <= len(chunk):
                await response.write(chunk[:bytes_to_send])
                break
            else:
                await response.write(chunk)
                bytes_to_send -= len(chunk)
                
    except (asyncio.CancelledError, ConnectionResetError):
        # IDM opens multiple connections and closes them constantly. This is normal, do not crash.
        pass 
    except Exception as e:
        print(f"Stream Download Error: {e}") 
        
    return response

# --- TELEGRAM EVENTS ---
@client.on(events.CallbackQuery(pattern="cancel_leech"))
async def cancel_handler(event):
    if event.sender_id not in ALLOWED_USERS: return
    if event.chat_id in cancel_tasks:
        cancel_tasks[event.chat_id].set()
        await event.edit("🛑 **Task Cancelled.**")

@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.sender_id not in ALLOWED_USERS: return
    
    if event.text == '/start':
        await event.reply("👋 **Koyeb Fast Bot Ready**\n\nSend a link to leech, forward a file to stream, or reply to a file with `/rename new_name.mp4`")
        return

    if event.text == '/status' and event.sender_id == ADMIN_ID:
        uptime = get_readable_time(int(time.time() - BOT_START_TIME))
        await event.reply(f"🤖 **Status**\n✅ Online\n⏳ Uptime: `{uptime}`\n⚙️ Active Tasks: `{len(cancel_tasks)}`")
        return

    # LEECH 
    if event.text and event.text.startswith("http"):
        if event.chat_id in cancel_tasks:
            await event.reply("⚠️ You already have an active process. Wait or cancel it.")
            return
        
        url = event.text.strip()
        msg = await event.reply("🔗 **Analyzing Link...**")
        cancel_event = asyncio.Event()
        cancel_tasks[event.chat_id] = cancel_event

        filename = "video.mp4"
        file_size = 0
        accept_ranges = False

        try:
            async with ClientSession() as session:
                try:
                    async with session.head(url, allow_redirects=True, timeout=10) as resp:
                        file_size = int(resp.headers.get("Content-Length", 0))
                        accept_ranges = resp.headers.get("Accept-Ranges") == "bytes"
                        
                        if "Content-Disposition" in resp.headers:
                            fname = re.findall('filename="?([^"]+)"?', resp.headers["Content-Disposition"])
                            if fname: filename = fname[0]
                        else:
                            filename = unquote(url.split("/")[-1].split("?")[0]) or "video"
                except: pass
            
            filename = re.sub(r'[\\/*?:"<>|]', "", filename)
            if not any(filename.lower().endswith(ext) for ext in['.mp4', '.mkv', '.avi', '.mov', '.webm']):
                filename += ".mp4"

            if file_size > 4000 * 10

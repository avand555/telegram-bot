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
    app_version="6.0.0 (IDM Offset Fix)"
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


# --- WEB SERVER ROUTES (FIXED FOR IDM) ---
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
    file_size = message.file.size if message.file else 0
    
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
                
    if start >= file_size or end >= file_size:
        return web.Response(status=416, headers={'Content-Range': f'bytes */{file_size}'})

    content_length = end - start + 1
    
    headers = {
        'Content-Disposition': f'inline; filename="{file_name}"', 
        'Content-Type': mime_type,
        'Content-Length': str(content_length),
        'Accept-Ranges': 'bytes'
    }

    if range_header:
        headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response = web.StreamResponse(status=206, headers=headers)
    else:
        response = web.StreamResponse(status=200, headers=headers)
        
    await response.prepare(request)

    # --- CRITICAL FIX FOR IDM: 1MB STRICT ALIGNMENT ---
    chunk_size = 1024 * 1024 # Exactly 1 MB
    offset = (start // chunk_size) * chunk_size
    skip = start - offset
    bytes_to_send = content_length

    try:
        async for chunk in client.iter_download(message, offset=offset, chunk_size=chunk_size):
            if skip > 0:
                chunk = chunk[skip:]
                skip = 0
            
            if bytes_to_send <= len(chunk):
                await response.write(chunk[:bytes_to_send])
                break
            else:
                await response.write(chunk)
                bytes_to_send -= len(chunk)
    except Exception as e:
        print(f"IDM Stream Error: {e}") 
        
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

            # 4GB limit for Premium, 2GB for normal
            if file_size > 4000 * 1024 * 1024:
                await msg.edit("❌ Error: File is larger than 4GB.")
                del cancel_tasks[event.chat_id]
                return

            progress = {'downloaded': 0}
            start_time = {'start': time.time(), 'last_update': 0}

            if accept_ranges and file_size > (5 * 1024 * 1024):
                CONNECTIONS = 8
                chunk_size = file_size // CONNECTIONS
                
                with open(filename, 'wb') as f:
                    f.seek(file_size - 1)
                    f.write(b'\0')

                tasks =[]
                for i in range(CONNECTIONS):
                    start = i * chunk_size
                    end = start + chunk_size - 1 if i < CONNECTIONS - 1 else file_size - 1
                    tasks.append(download_chunk(url, start, end, filename, progress, cancel_event))

                updater_task = asyncio.create_task(update_download_progress(msg, progress, file_size, filename, start_time, cancel_event))
                await asyncio.gather(*tasks)
                updater_task.cancel()
            else:
                updater_task = asyncio.create_task(update_download_progress(msg, progress, file_size or 1, filename, start_time, cancel_event))
                async with ClientSession() as session:
                    async with session.get(url) as resp:
                        with open(filename, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                if cancel_event.is_set(): raise asyncio.CancelledError()
                                f.write(chunk)
                                progress['downloaded'] += len(chunk)
                updater_task.cancel()

            if cancel_event.is_set(): raise asyncio.CancelledError()

            await msg.edit(f"⬆️ **Starting Fast Upload...**\n🎬 `{filename}`", buttons=[[Button.inline("❌ Cancel", data="cancel_leech")]])
            start_time = {'start': time.time(), 'last_update': 0}

            uploaded_file = await upload_file_fast(client, filename, msg, start_time, filename, cancel_event)
            await msg.edit(f"⏳ **Finalizing Video processing...**\n🎬 `{filename}`")
            
            await client.send_file(
                event.chat_id,
                file=uploaded_file,
                caption=f"✅ `{filename}`",
                supports_streaming=True, 
                attributes=[types.DocumentAttributeVideo(duration=0, w=1280, h=720, supports_streaming=True)]
            )
            await msg.delete()

        except asyncio.CancelledError:
            await msg.edit("🛑 **Task Cancelled.**")
        except Exception as e:
            await msg.edit(f"❌ Error: {e}")
        finally:
            if os.path.exists(filename):
                os.remove(filename) 
            if event.chat_id in cancel_tasks:
                del cancel_tasks[event.chat_id]
        return

    # STREAM / RENAME 
    if event.file or (event.is_reply and event.text and event.text.startswith('/rename')):
        target_msg = event
        custom_name = None

        if event.text and event.text.startswith('/rename'):
            target_msg = await event.get_reply_message()
            if not target_msg or not target_msg.file:
                await event.reply("⚠️ Please reply to a video/file to rename it.")
                return
            
            custom_name = event.text.split(' ', 1)[1].strip() if ' ' in event.text else 'video.mp4'
            if not '.' in custom_name: custom_name += '.mp4' 

        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': target_msg, 'timestamp': time.time()}
        
        app_url = os.environ.get("KOYEB_PUBLIC_URL", "")
        if not app_url:
             app_name = os.environ.get("KOYEB_APP_NAME", "your-app-name")
             app_url = f"https://{app_name}.koyeb.app"
             
        app_url = app_url.rstrip('/')

        original_name = target_msg.file.name or 'video.mp4'
        final_name = custom_name if custom_name else original_name
        
        hotlink = f"{app_url}/{code}/{quote(final_name)}"
        
        text = f"✅ **Link Generated!**\n\n"
        if custom_name:
            text += f"✏️ **Renamed to:** `{final_name}`\n\n"
        else:
            text += f"📂 `{final_name}`\n💡 *Tip: Reply to this file with `/rename new_name.mp4` to change its name!*\n\n"
            
        text += f"🔗 `{hotlink}`"
        
        await event.reply(text)

# --- MAIN EXECUTION ---
async def main():
    asyncio.create_task(cleanup_loop())
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8000)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Server started on port {port}")

    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

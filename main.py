import os
import secrets
import asyncio
import mimetypes
import time
import re
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcpFull

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
    device_model="Koyeb Server",
    system_version="Linux",
    app_version="2.0.0 (IDM Engine)"
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
        keys_to_delete = [k for k, v in link_storage.items() if current_time - v['timestamp'] > EXPIRATION_TIME]
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
    # Ensure it says 100% at the end
    await progress_callback(total, total, msg, start_time, filename, "⬇️ **Downloading (IDM Engine)**")

# --- WEB SERVER ROUTES ---
@routes.get('/')
async def root(request):
    return web.Response(text="✅ IDM Bot is Online on Koyeb")

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data or (time.time() - data['timestamp'] > EXPIRATION_TIME):
        if data: del link_storage[code]
        return web.Response(text="❌ Link Expired", status=410)
    
    message = data['msg']
    file_name = request.match_info['filename']
    file_size = message.file.size if message.file else 0
    mime_type, _ = mimetypes.guess_type(file_name)
    if not mime_type: mime_type = 'application/octet-stream'

    headers = {
        'Content-Disposition': f'inline; filename="{file_name}"',
        'Content-Type': mime_type,
        'Content-Length': str(file_size),
        'Accept-Ranges': 'none'
    }
    response = web.StreamResponse(headers=headers)
    await response.prepare(request)
    try:
        async for chunk in client.iter_download(message, chunk_size=512 * 1024):
            await response.write(chunk)
    except Exception: pass
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
    
    # 1. HELP
    if event.text == '/start':
        await event.reply("👋 **Koyeb IDM Bot Ready**\n\nSend a link to download fast and upload as Video.")
        return

    # 2. STATUS
    if event.text == '/status' and event.sender_id == ADMIN_ID:
        uptime = get_readable_time(int(time.time() - BOT_START_TIME))
        await event.reply(f"🤖 **Status**\n✅ Online\n⏳ Uptime: `{uptime}`\n⚙️ Active Tasks: `{len(cancel_tasks)}`")
        return

    # 3. LEECH (URL to Telegram)
    if event.text.startswith("http"):
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
            # Check Headers
            async with ClientSession() as session:
                try:
                    async with session.head(url, allow_redirects=True, timeout=10) as resp:
                        file_size = int(resp.headers.get("Content-Length", 0))
                        accept_ranges = resp.headers.get("Accept-Ranges") == "bytes"
                        
                        # Get filename
                        if "Content-Disposition" in resp.headers:
                            fname = re.findall('filename="?([^"]+)"?', resp.headers["Content-Disposition"])
                            if fname: filename = fname[0]
                        else:
                            filename = unquote(url.split("/")[-1].split("?")[0]) or "video"
                except:
                    pass
            
            # Clean filename and FORCE .mp4 extension for Video Upload
            filename = re.sub(r'[\\/*?:"<>|]', "", filename)
            if not any(filename.lower().endswith(ext) for ext in['.mp4', '.mkv', '.avi', '.mov', '.webm']):
                filename += ".mp4"

            if file_size > 2000 * 1024 * 1024:
                await msg.edit("❌ Error: File is larger than 2GB.")
                del cancel_tasks[event.chat_id]
                return

            progress = {'downloaded': 0}
            start_time = {'start': time.time(), 'last_update': 0}

            # MULTI-PART DOWNLOAD
            if accept_ranges and file_size > (5 * 1024 * 1024):
                CONNECTIONS = 8
                chunk_size = file_size // CONNECTIONS
                
                # Pre-allocate file on disk
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

            # SINGLE PART FALLBACK
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

            # UPLOAD TO TELEGRAM AS VIDEO
            await msg.edit(f"⬆️ **Uploading to Telegram...**\n🎬 `{filename}`", buttons=[[Button.inline("❌ Cancel", data="cancel_leech")]])
            start_time = {'start': time.time(), 'last_update': 0}

            await client.send_file(
                event.chat_id,
                file=filename,
                caption=f"✅ `{filename}`",
                supports_streaming=True, # MAKES IT A PLAYABLE VIDEO
                attributes=[types.DocumentAttributeVideo(
                    duration=0, w=1280, h=720, supports_streaming=True
                )],
                progress_callback=lambda c, t: progress_callback(c, t, msg, start_time, filename, "⬆️ **Uploading**")
            )
            await msg.delete()

        except asyncio.CancelledError:
            await msg.edit("🛑 **Task Cancelled.**")
        except Exception as e:
            await msg.edit(f"❌ Error: {e}")
        finally:
            if os.path.exists(filename):
                os.remove(filename) # Clean up server memory
            if event.chat_id in cancel_tasks:
                del cancel_tasks[event.chat_id]
        return

    # 4. STREAM (File -> URL)
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': event.message, 'timestamp': time.time()}
        
        app_url = os.environ.get("KOYEB_PUBLIC_URL", "")
        if not app_url:
             app_name = os.environ.get("KOYEB_APP_NAME", "your-app-name")
             app_url = f"https://{app_name}.koyeb.app"

        hotlink = f"{app_url}/{code}/{quote(event.file.name or 'video.mp4')}"
        await event.reply(f"✅ **Link:**\n`{hotlink}`")

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

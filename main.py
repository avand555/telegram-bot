import os
import secrets
import asyncio
import mimetypes
import time
import re
from urllib.parse import quote, unquote

# Telegram Imports
from telethon import TelegramClient, events, types, Button
from telethon.network import ConnectionTcp

# Web Server Imports
from aiohttp import web, ClientSession

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Track start time
BOT_START_TIME = time.time()

# --- USER AND ADMIN MANAGEMENT ---
# REPLACE THESE WITH YOUR IDs
ALLOWED_USERS = {716887656, 1053544356} 
ADMIN_ID = 716887656  

# Expiration: 24 Hours
EXPIRATION_TIME = 24 * 60 * 60 

# Global Dictionary
cancel_tasks = {}

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

class CustomStreamReader:
    def __init__(self, response, cancel_event):
        self.response = response
        self.cancel_event = cancel_event

    async def read(self, size):
        if self.cancel_event.is_set(): raise asyncio.CancelledError("Task Cancelled")
        data = b''
        while len(data) < size:
            if self.cancel_event.is_set(): raise asyncio.CancelledError("Task Cancelled")
            chunk = await self.response.content.read(size - len(data))
            if not chunk: break
            data += chunk
        return data

# --- SETUP ---
# Koyeb connects better with Standard TCP + IPv4
client = TelegramClient(
    'bot_session', 
    int(API_ID), 
    API_HASH, 
    connection=ConnectionTcp,
    use_ipv6=False, 
    device_model="Koyeb Server",
    system_version="Linux",
    app_version="1.0.0"
)

# Web Server Setup
routes = web.RouteTableDef()
link_storage = {}

# --- BACKGROUND TASKS ---
async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        current_time = time.time()
        keys_to_delete = [k for k, v in link_storage.items() if current_time - v['timestamp'] > EXPIRATION_TIME]
        for key in keys_to_delete: del link_storage[key]

async def progress_callback(current, total, event, start_time, filename):
    now = time.time()
    if now - start_time['last_update'] < 5: return
    start_time['last_update'] = now
    percentage = current * 100 / total
    speed = (current / (now - start_time['start'])) / 1024 / 1024
    uploaded = current / 1024 / 1024
    total_size = total / 1024 / 1024
    try:
        await event.edit(
            f"📥 **Leeching...**\n📂 `{filename}`\n📊 `{percentage:.2f}%`\n🚀 `{speed:.2f} MB/s`\n💾 `{uploaded:.2f} / {total_size:.2f} MB`",
            buttons=[[Button.inline("❌ Cancel", data="cancel_leech")]]
        )
    except Exception: pass

# --- WEB SERVER ROUTES ---
@routes.get('/')
async def root(request):
    return web.Response(text="✅ Bot is Online on Koyeb")

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
    
    if event.text == '/start':
        await event.reply("👋 **Koyeb Bot Ready**")
        return

    if event.text == '/status' and event.sender_id == ADMIN_ID:
        uptime = get_readable_time(int(time.time() - BOT_START_TIME))
        await event.reply(f"🤖 **Status**\n✅ Online\n⏳ Uptime: `{uptime}`\n⚙️ Tasks: `{len(cancel_tasks)}`")
        return

    # LEECH
    if event.text.startswith("http"):
        if event.chat_id in cancel_tasks:
            await event.reply("⚠️ Wait for current task.")
            return
        
        url = event.text.strip()
        msg = await event.reply("🔗 **Connecting...**")
        cancel_event = asyncio.Event()
        cancel_tasks[event.chat_id] = cancel_event

        async with ClientSession() as session:
            try:
                async with session.get(url, timeout=None) as response:
                    if response.status != 200:
                        await msg.edit(f"❌ Error: {response.status}")
                        del cancel_tasks[event.chat_id]
                        return
                    
                    filename = unquote(url.split("/")[-1].split("?")[0]) or "file.bin"
                    if "Content-Disposition" in response.headers:
                        fname = re.findall('filename="?([^"]+)"?', response.headers["Content-Disposition"])
                        if fname: filename = fname[0]

                    file_size = int(response.headers.get("Content-Length", 0))
                    
                    await msg.edit(f"⬇️ **Downloading...**\n`{filename}`", buttons=[[Button.inline("❌ Cancel", data="cancel_leech")]])
                    stream_reader = CustomStreamReader(response, cancel_event)
                    start_time = {'start': time.time(), 'last_update': 0}
                    
                    await client.send_file(
                        event.chat_id, file=stream_reader, caption=f"✅ `{filename}`", file_size=file_size,
                        attributes=[types.DocumentAttributeFilename(file_name=filename)],
                        progress_callback=lambda c, t: progress_callback(c, t, msg, start_time, filename)
                    )
                    await msg.delete()
            except Exception as e:
                await msg.edit(f"❌ Error: {e}")
            finally:
                if event.chat_id in cancel_tasks: del cancel_tasks[event.chat_id]
        return

    # STREAM
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': event.message, 'timestamp': time.time()}
        # AUTOMATICALLY GET KOYEB URL
        app_url = os.environ.get("KOYEB_PUBLIC_URL", "http://localhost:8000")
        hotlink = f"{app_url}/{code}/{quote(event.file.name or 'file')}"
        await event.reply(f"✅ **Link:**\n`{hotlink}`")

# --- MAIN EXECUTION ---
async def main():
    asyncio.create_task(cleanup_loop())
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    # Koyeb passes PORT env var, defaults to 8000
    port = int(os.environ.get("PORT", 8000)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Server on port {port}")
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

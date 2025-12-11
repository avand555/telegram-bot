import os
import secrets
import asyncio
import mimetypes
import time
import re
from urllib.parse import quote, unquote
from telethon import TelegramClient, events, types
from aiohttp import web, ClientSession

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Expiration: 24 Hours
EXPIRATION_TIME = 24 * 60 * 60 

# --- HELPER CLASS FOR STREAMING UPLOAD ---
# This fixes the "Cannot use async_generator" error
class CustomStreamReader:
    def __init__(self, response):
        self.response = response

    async def read(self, size):
        # Read the specified chunk size from the aiohttp stream
        return await self.response.content.read(size)

# --- SETUP ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection_retries=None)
routes = web.RouteTableDef()
link_storage = {} 

# --- BACKGROUND CLEANER ---
async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        current_time = time.time()
        keys_to_delete = []
        for code, data in link_storage.items():
            if current_time - data['timestamp'] > EXPIRATION_TIME:
                keys_to_delete.append(code)
        for key in keys_to_delete:
            del link_storage[key]

# --- PROGRESS BAR ---
async def progress_callback(current, total, event, start_time, filename):
    now = time.time()
    if now - start_time['last_update'] < 5:
        return

    start_time['last_update'] = now
    percentage = current * 100 / total
    speed = (current / (now - start_time['start'])) / 1024 / 1024
    uploaded = current / 1024 / 1024
    total_size = total / 1024 / 1024

    try:
        await event.edit(
            f"📥 **Leeching in progress...**\n\n"
            f"📂 `{filename}`\n"
            f"📊 `{percentage:.2f}%`\n"
            f"🚀 `{speed:.2f} MB/s`\n"
            f"💾 `{uploaded:.2f} MB / {total_size:.2f} MB`"
        )
    except Exception:
        pass

# --- WEB SERVER (STREAMER) ---
@routes.get('/')
async def root(request):
    return web.Response(text="✅ Bot is Online")

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
    except Exception:
        pass
    return response

# --- TELEGRAM HANDLERS ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    
    # 1. HELP
    if event.text == '/start':
        await event.reply(
            "👋 **Combined Bot Ready**\n\n"
            "1️⃣ **Send Link:** I will upload the file here.\n"
            "2️⃣ **Send File:** I will create a Direct Download Link."
        )
        return

    # 2. LEECHER (Link -> Telegram)
    if event.text and event.text.startswith(("http://", "https://")):
        url = event.text.strip()
        msg = await event.reply("🔗 **Connecting...**")
        
        async with ClientSession() as session:
            try:
                # We do NOT use 'timeout' here so large files don't fail
                async with session.get(url, timeout=None) as response:
                    if response.status != 200:
                        await msg.edit(f"❌ HTTP Error: {response.status}")
                        return

                    # Detect Filename
                    filename = "downloaded_file"
                    if "Content-Disposition" in response.headers:
                        fname = re.findall('filename="?([^"]+)"?', response.headers["Content-Disposition"])
                        if fname: filename = fname[0]
                    else:
                        filename = unquote(url.split("/")[-1].split("?")[0])
                    if not filename: filename = "file.bin"

                    # Get Size
                    content_length = response.headers.get("Content-Length")
                    file_size = int(content_length) if content_length else 0
                    
                    if file_size > 2000 * 1024 * 1024:
                        await msg.edit("❌ Error: File > 2GB.")
                        return

                    await msg.edit(f"⬇️ **Downloading...**\n`{filename}`")

                    # --- FIX IS HERE ---
                    # Instead of a generator, we wrap response in a Class
                    stream_reader = CustomStreamReader(response)

                    start_time = {'start': time.time(), 'last_update': 0}
                    
                    await client.send_file(
                        event.chat_id,
                        file=stream_reader,  # <--- Now passing the Class object
                        caption=f"✅ **Done:** `{filename}`",
                        file_size=file_size,
                        attributes=[types.DocumentAttributeFilename(file_name=filename)],
                        progress_callback=lambda c, t: progress_callback(c, t, msg, start_time, filename)
                    )
                    await msg.delete()

            except Exception as e:
                await msg.edit(f"❌ Error: {str(e)}")
        return

    # 3. STREAMER (File -> Link)
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': event.message, 'timestamp': time.time()}
        
        original_name = event.file.name if event.file.name else "file"
        safe_name = quote(original_name.replace(" ", "_"))
        
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        hotlink = f"{base_url}/{code}/{safe_name}"
        
        size_mb = event.file.size / 1024 / 1024
        
        await event.reply(
            f"✅ **Link Generated!**\n"
            f"📂 `{original_name}`\n"
            f"💾 `{size_mb:.2f} MB`\n\n"
            f"🔗 `{hotlink}`",
            parse_mode='markdown'
        )

async def main():
    asyncio.create_task(cleanup_loop())
    
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Web Server started on port {port}")

    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot Connected")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

import os
import secrets
import asyncio
from urllib.parse import quote
from telethon import TelegramClient, events
from aiohttp import web

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# --- SETUP ---
client = TelegramClient('bot_session', int(API_ID), API_HASH)
routes = web.RouteTableDef()
link_storage = {} 

@routes.get('/')
async def root(request):
    return web.Response(text="✅ Bot is Online")

# --- NEW ROUTE HANDLER (Handles /code/filename.mp4) ---
@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    # We don't actually need 'filename' for logic, but it's required for the URL to work
    
    message = link_storage.get(code)

    if not message:
        return web.Response(text="Link Expired or Invalid", status=404)

    # --- FILENAME EXTRACTION (For Content-Disposition) ---
    file_name = request.match_info['filename'] # Use the name from the URL
    
    # Try to find real size
    file_size = None
    if message.file:
        file_size = message.file.size

    # --- HEADERS ---
    # We Force the filename in headers too, just to be safe
    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"',
        'Content-Type': 'application/octet-stream'
    }
    if file_size:
        headers['Content-Length'] = str(file_size)

    response = web.StreamResponse(headers=headers)
    await response.prepare(request)

    # --- STREAMING ---
    async for chunk in client.iter_download(message):
        await response.write(chunk)

    return response

@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    # 1. Handle /start
    if event.text == '/start':
        await event.reply(
            "👋 **Hello!**\n\n"
            "Send me a file, and I will generate a **Direct Hotlink** (ending in .mp4/.jpg/etc).",
            parse_mode='markdown'
        )
        return

    # 2. Handle Files
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = event.message
        
        # Get Filename
        original_name = "file"
        if event.file.name:
            original_name = event.file.name
        else:
            # Fallback if name is missing (e.g. some photos)
            ext = event.file.ext if event.file.ext else ""
            original_name = f"download{ext}"

        # Make URL Safe (Replace spaces with _)
        safe_name = quote(original_name.replace(" ", "_"))
        
        # Construct Hotlink
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        download_link = f"{base_url}/{code}/{safe_name}"
        
        # Size calc
        size_mb = event.file.size / 1024 / 1024
        
        await event.reply(
            f"✅ **File Ready!**\n"
            f"📂 `{original_name}`\n"
            f"💾 `{size_mb:.2f} MB`\n\n"
            f"🔗 **Hotlink:**\n`{download_link}`",
            parse_mode='markdown'
        )

async def main():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Server started on port {port}")

    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot Connected")
    
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

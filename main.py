import os
import secrets
import asyncio
import mimetypes
import time
from urllib.parse import quote
from telethon import TelegramClient, events
from aiohttp import web

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Expiration: 24 Hours (in seconds)
EXPIRATION_TIME = 24 * 60 * 60 

# --- SETUP ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection_retries=None)
routes = web.RouteTableDef()
link_storage = {} 

# --- BACKGROUND CLEANER TASK ---
async def cleanup_loop():
    """
    Runs forever in the background.
    Every 10 minutes, it deletes links older than 24 hours to free up RAM.
    """
    while True:
        await asyncio.sleep(600)  # Wait 10 minutes
        
        current_time = time.time()
        keys_to_delete = []

        # Find expired links
        for code, data in link_storage.items():
            if current_time - data['timestamp'] > EXPIRATION_TIME:
                keys_to_delete.append(code)

        # Delete them
        for key in keys_to_delete:
            del link_storage[key]
            print(f"🗑️ Auto-deleted expired link: {key}")

@routes.get('/')
async def root(request):
    return web.Response(text="✅ Bot is Online & Auto-Cleaning")

@routes.get('/{code}/{filename}')
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)

    # 1. Check if Link Exists
    if not data:
        return web.Response(text="❌ Link Invalid or Expired", status=404)

    # 2. Double-Check Expiration (Just in case)
    if time.time() - data['timestamp'] > EXPIRATION_TIME:
        del link_storage[code]
        return web.Response(text="⏳ This link has expired.", status=410)

    message = data['msg']
    file_name = request.match_info['filename']
    file_size = message.file.size if message.file else 0

    # Auto-detect Content-Type
    mime_type, _ = mimetypes.guess_type(file_name)
    if not mime_type:
        mime_type = 'application/octet-stream'

    headers = {
        'Content-Disposition': f'inline; filename="{file_name}"',
        'Content-Type': mime_type,
        'Content-Length': str(file_size),
        'Accept-Ranges': 'none'
    }

    response = web.StreamResponse(headers=headers)
    await response.prepare(request)

    try:
        # High speed chunks
        async for chunk in client.iter_download(message, chunk_size=512 * 1024):
            await response.write(chunk)
    except Exception:
        pass

    return response

@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.text == '/start':
        await event.reply("👋 Send me a file. Links work for 24 hours, then they are auto-deleted.")
        return

    if event.file:
        code = secrets.token_urlsafe(8)
        
        # Save Data + Timestamp
        link_storage[code] = {
            'msg': event.message,
            'timestamp': time.time()
        }
        
        original_name = event.file.name if event.file.name else "video.mp4"
        safe_name = quote(original_name.replace(" ", "_"))
        
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        hotlink = f"{base_url}/{code}/{safe_name}"
        
        size_mb = event.file.size / 1024 / 1024
        
        await event.reply(
            f"✅ **Link Generated!**\n"
            f"📂 `{original_name}`\n"
            f"💾 `{size_mb:.2f} MB`\n\n"
            f"🔗 `{hotlink}`\n\n"
            f"⏳ _Expires in 24 hours_",
            parse_mode='markdown'
        )

async def main():
    # 1. Start the Auto-Cleaner Logic in background
    asyncio.create_task(cleanup_loop())

    # 2. Start Web Server
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Web Server started on port {port}")

    # 3. Start Bot
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot Connected")
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

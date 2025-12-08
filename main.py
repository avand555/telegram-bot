import os
import secrets
import asyncio
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

@routes.get('/{code}')
async def stream_handler(request):
    code = request.match_info['code']
    message = link_storage.get(code)

    if not message:
        return web.Response(text="Link Expired or Invalid", status=404)

    file_name = "download.file"
    for attr in message.file.attributes:
        if hasattr(attr, 'file_name'):
            file_name = attr.file_name

    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"',
        'Content-Type': 'application/octet-stream',
        'Content-Length': str(message.file.size)
    }

    response = web.StreamResponse(headers=headers)
    await response.prepare(request)

    async for chunk in client.download_media(message, file=bytes, offset=0):
        await response.write(chunk)

    return response

@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    # 1. Handle /start command
    if event.text == '/start':
        await event.reply(
            "👋 **Hello!**\n\n"
            "I am your **Big File Streamer** running on Render.\n"
            "Send me a file to get a link!",
            parse_mode='markdown'
        )
        return

    # 2. Handle Files
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = event.message
        
        # Get Render URL
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        download_link = f"{base_url}/{code}"
        
        await event.reply(
            f"✅ **File Ready!**\n"
            f"💾 `{event.file.size / 1024 / 1024:.2f} MB`\n"
            f"🔗 {download_link}",
            parse_mode='markdown'
        )

async def main():
    # Start Web Server (Required for Render)
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Web Server started on port {port}")

    # Start Telegram Client
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot Connected to Telegram")
    
    # Run forever
    await client.run_until_disconnected()

if __name__ == '__main__':
    # This specifically fixes the 'DeprecationWarning' in your logs
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

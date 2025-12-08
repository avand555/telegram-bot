import os
import secrets
import asyncio
from telethon import TelegramClient, events
from aiohttp import web

# --- CONFIGURATION (Load from Environment Variables) ---
# You will set these in Render Dashboard settings later
API_ID = int(os.environ.get("18523213", 0))
API_HASH = os.environ.get("c510774d05fbb47d1a6b222500940ee2
", "")
BOT_TOKEN = os.environ.get("7806492319:AAEPVCiqYZgsVe81cxP4o1X7XGhM_6mlTVk", "")

# --- SETUP ---
# We use a file-based session. On free Render, you will need to login 
# once locally and upload the session file, OR use a StringSession.
client = TelegramClient('bot_session', API_ID, API_HASH)
routes = web.RouteTableDef()
link_storage = {} 

@routes.get('/')
async def root(request):
    return web.Response(text="Render Bot is Running")

@routes.get('/{code}')
async def stream_handler(request):
    code = request.match_info['code']
    message = link_storage.get(code)

    if not message:
        return web.Response(text="Link Expired", status=404)

    file_name = "video.mp4"
    # Try to find real filename
    for attr in message.file.attributes:
        if hasattr(attr, 'file_name'):
            file_name = attr.file_name

    # Headers to force download
    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"',
        'Content-Type': 'application/octet-stream',
        'Content-Length': str(message.file.size)
    }

    response = web.StreamResponse(headers=headers)
    await response.prepare(request)

    # Stream file chunk by chunk
    async for chunk in client.download_media(message, file=bytes, offset=0):
        await response.write(chunk)

    return response

@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = event.message
        
        # Get the Render URL (We will get this later)
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
        download_link = f"{base_url}/{code}"
        
        await event.reply(
            f"✅ <b>Link Generated!</b>\n"
            f"💾 Size: {event.file.size / 1024 / 1024:.2f} MB\n"
            f"🔗 {download_link}",
            parse_mode='html'
        )

async def start_server():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render requires us to listen on port 10000 or the PORT env var
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    # Start the Bot
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_server())

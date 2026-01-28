import os
import secrets
import asyncio
import mimetypes
import time
import re
from urllib.parse import quote, unquote
from telethon import TelegramClient, events, types, Button
from aiohttp import web, ClientSession

# --- CONFIGURATION ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_START_TIME = time.time()
# --- USER AND ADMIN MANAGEMENT ---
# IMPORTANT: Replace these with the actual integer User IDs.
# You can get any user's ID by forwarding their message to @userinfobot.
# The set should contain the IDs of the two users allowed to use the bot.
ALLOWED_USERS = {716887656, 1053544356} 
# This is your User ID for receiving notifications.
ADMIN_ID = 716887656  

# Expiration: 24 Hours
EXPIRATION_TIME = 24 * 60 * 60 

# Global Dictionary to track cancel events
# Format: { chat_id: asyncio.Event() }
cancel_tasks = {}

# --- HELPER CLASS WITH CANCEL SUPPORT ---
class CustomStreamReader:
    def __init__(self, response, cancel_event):
        self.response = response
        self.cancel_event = cancel_event

    async def read(self, size):
        if self.cancel_event.is_set():
            raise asyncio.CancelledError("Task Cancelled")

        data = b''
        while len(data) < size:
            if self.cancel_event.is_set():
                raise asyncio.CancelledError("Task Cancelled")
                
            chunk = await self.response.content.read(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

# --- SETUP ---
client = TelegramClient('bot_session', int(API_ID), API_HASH, connection_retries=None)
routes = web.RouteTableDef()
link_storage = {} 

# --- HELPER FUNCTION FOR UPTIME ---
def get_readable_time(seconds: int) -> str:
    result = ""
    (days, remainder) = divmod(seconds, 86400)
    days = int(days)
    if days != 0:
        result += f"{days}d "
    (hours, remainder) = divmod(remainder, 3600)
    hours = int(hours)
    if hours != 0:
        result += f"{hours}h "
    (minutes, seconds) = divmod(remainder, 60)
    minutes = int(minutes)
    if minutes != 0:
        result += f"{minutes}m "
    seconds = int(seconds)
    result += f"{seconds}s"
    return result
    
# --- BACKGROUND CLEANER ---
async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        current_time = time.time()
        keys_to_delete = [k for k, v in link_storage.items() if current_time - v['timestamp'] > EXPIRATION_TIME]
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
            f"💾 `{uploaded:.2f} MB / {total_size:.2f} MB`",
            buttons=[[Button.inline("❌ Cancel Task", data="cancel_leech")]]
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

# --- CANCEL BUTTON HANDLER ---
@client.on(events.CallbackQuery(pattern="cancel_leech"))
async def cancel_handler(event):
    # --- AUTHORIZATION CHECK ---
    if event.sender_id not in ALLOWED_USERS:
        await event.answer("🚫 You are not authorized to perform this action.", alert=True)
        return

    if event.chat_id in cancel_tasks:
        cancel_tasks[event.chat_id].set()
        await event.answer("Cancelling task...", alert=False)
        await event.edit("🛑 **Task Cancelled by User.**")
    else:
        await event.answer("No active task to cancel.", alert=True)

# --- TELEGRAM MESSAGE HANDLERS ---
@client.on(events.NewMessage(incoming=True))
async def handle_new_message(event):
    
    sender = await event.get_sender()
    sender_username = f"@{sender.username}" if sender.username else f"User ID: {sender.id}"

    # --- AUTHORIZATION CHECK ---
    if event.sender_id not in ALLOWED_USERS:
        # Notify Admin of unauthorized access attempt
        await client.send_message(
            ADMIN_ID,
            f"⚠️ **Unauthorized Access Denied**\n\n"
            f"👤 **User:** {sender_username} (`{event.sender_id}`)\n"
            f"📝 **Message:** `{event.text}`"
        )
        await event.reply("🚫 **Access Denied!**\n\nI am a private bot and you are not authorized to use me.")
        return

    # --- ADMIN ACTIVITY NOTIFICATION ---
    if event.sender_id != ADMIN_ID:
        await client.send_message(
            ADMIN_ID,
            f"🔔 **Bot Activity**\n\n"
            f"👤 **User:** {sender_username}\n"
            f"💬 **Action:** User sent a message/command."
        )


    # 1. HELP
    if event.text == '/start':
        await event.reply(
            "👋 **Combined Bot Ready**\n\n"
            "1️⃣ **Send Link:** Upload file here (with Cancel button).\n"
            "2️⃣ **Send File:** Get Direct Download Link."
        )
        return

    # --- ADD THE NEW /status COMMAND HERE ---
    # 2. STATUS (Admin Only)
    if event.text == '/status' and event.sender_id == ADMIN_ID:
        current_time = time.time()
        uptime_seconds = int(current_time - BOT_START_TIME)
        uptime = get_readable_time(uptime_seconds)

        # Count active leeching tasks
        active_tasks = len(cancel_tasks)

        status_message = (
            f"🤖 **Bot Status**\n\n"
            f"**• Status:** `Online` ✅\n"
            f"**• Uptime:** `{uptime}` ⏳\n"
            f"**• Active Processes:** `{active_tasks}` ⚙️"
        )
        await event.reply(status_message)
        return

    # 2. LEECHER (Link -> Telegram)
    if event.text and event.text.startswith(("http://", "https://")):
        if event.chat_id in cancel_tasks:
            await event.reply("⚠️ **You already have a process running.**\nPlease wait or cancel it.")
            return

        url = event.text.strip()
        msg = await event.reply("🔗 **Connecting...**")
        
        cancel_event = asyncio.Event()
        cancel_tasks[event.chat_id] = cancel_event

        async with ClientSession() as session:
            try:
                async with session.get(url, timeout=None) as response:
                    if response.status != 200:
                        await msg.edit(f"❌ HTTP Error: {response.status}")
                        del cancel_tasks[event.chat_id]
                        return

                    filename = "downloaded_file"
                    if "Content-Disposition" in response.headers:
                        fname = re.findall('filename="?([^"]+)"?', response.headers["Content-Disposition"])
                        if fname: filename = fname[0]
                    else:
                        filename = unquote(url.split("/")[-1].split("?")[0])
                    if not filename: filename = "file.bin"

                    content_length = response.headers.get("Content-Length")
                    file_size = int(content_length) if content_length else 0
                    
                    if file_size > 2000 * 1024 * 1024:
                        await msg.edit("❌ Error: File > 2GB.")
                        del cancel_tasks[event.chat_id]
                        return

                    await msg.edit(
                        f"⬇️ **Downloading...**\n`{filename}`",
                        buttons=[[Button.inline("❌ Cancel Task", data="cancel_leech")]]
                    )

                    stream_reader = CustomStreamReader(response, cancel_event)
                    start_time = {'start': time.time(), 'last_update': 0}
                    
                    try:
                        await client.send_file(
                            event.chat_id,
                            file=stream_reader,
                            caption=f"✅ **Done:** `{filename}`",
                            file_size=file_size,
                            attributes=[types.DocumentAttributeFilename(file_name=filename)],
                            progress_callback=lambda c, t: progress_callback(c, t, msg, start_time, filename)
                        )
                        await msg.delete()
                    
                    except asyncio.CancelledError:
                        await msg.edit("🛑 **Task Cancelled.**")
                    except Exception as e:
                        if "Task Cancelled" in str(e):
                            await msg.edit("🛑 **Task Cancelled.**")
                        else:
                            await msg.edit(f"❌ Error: {str(e)}")

            except Exception as e:
                await msg.edit(f"❌ Network Error: {str(e)}")
            finally:
                if event.chat_id in cancel_tasks:
                    del cancel_tasks[event.chat_id]
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
        
        # --- ADMIN NOTIFICATION FOR LINK GENERATION ---
        await client.send_message(
            ADMIN_ID,
            f"✅ **Admin Log: Link Generated**\n\n"
            f"👤 **User:** {sender_username}\n"
            f"📂 **File:** `{original_name}`\n"
            f"🔗 **Link:** `{hotlink}`"
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

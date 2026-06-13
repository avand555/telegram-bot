import os
import time
import asyncio
import math
import random
import re
import subprocess
import nest_asyncio
import aiofiles
import mimetypes
import secrets
import cv2 # Pre-installed in Colab
from urllib.parse import quote, unquote
from aiohttp import web, ClientSession, TCPConnector

# Telegram Imports
from telethon import TelegramClient, events, types, utils
from telethon.network import ConnectionTcpFull
from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest

# CRITICAL FOR COLAB
nest_asyncio.apply()

# --- CONFIGURATION ---
API_ID = 12345678  # Replace
API_HASH = "your_hash" # Replace
PHONE = "+964..."   # Your phone number
ADMIN_ID = 716887656 

client = TelegramClient(
    'user_session', 
    API_ID, 
    API_HASH, 
    connection=ConnectionTcpFull,
    device_model="Premium T4 Engine",
    system_version="Linux",
    app_version="25.0"
)

link_storage = {}
PUBLIC_URL = ""

# --- VIDEO HELPER: FIX & THUMBNAIL ---
def get_video_metadata(file_path):
    """Extacts duration, width, height and creates a thumbnail."""
    v_info = {'duration': 0, 'w': 1280, 'h': 720, 'thumb': None}
    try:
        cap = cv2.VideoCapture(file_path)
        if cap.isOpened():
            v_info['w'] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            v_info['h'] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if fps > 0:
                v_info['duration'] = int(count / fps)
            
            # Save a frame from the middle of the video as thumbnail
            cap.set(cv2.CAP_PROP_POS_FRAMES, count // 2)
            ret, frame = cap.read()
            if ret:
                thumb_path = file_path + ".jpg"
                cv2.imwrite(thumb_path, frame)
                v_info['thumb'] = thumb_path
        cap.release()
    except: pass
    return v_info

def fix_video_header(input_path):
    """Moves moov atom to start so video is streamable (FastStart)."""
    output_path = "fixed_" + input_path
    print(f"🛠 Optimizing video for streaming...")
    try:
        # Use FFmpeg to move metadata to the front (instantly playable)
        subprocess.run([
            'ffmpeg', '-y', '-i', input_path, 
            '-c', 'copy', '-map', '0', 
            '-movflags', 'faststart', output_path
        ], check=True, capture_output=True)
        return output_path
    except:
        return input_path

# --- PORT CLEANUP & CLOUDFLARE ---
def setup_tunnel():
    global PUBLIC_URL
    subprocess.run("fuser -k 8000/tcp", shell=True, capture_output=True)
    subprocess.run("pkill cloudflared", shell=True, capture_output=True)
    if not os.path.exists('./cloudflared-linux-amd64'):
        subprocess.run(['wget', '-q', 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64'])
        subprocess.run(['chmod', '+x', './cloudflared-linux-amd64'])
    process = subprocess.Popen(['stdbuf', '-oL', './cloudflared-linux-amd64', 'tunnel', '--url', 'http://localhost:8000'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    for line in process.stdout:
        if "trycloudflare.com" in line:
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match:
                PUBLIC_URL = match.group(0)
                print(f"✅ Cloudflare Active: {PUBLIC_URL}")
                break
    return PUBLIC_URL

# --- WEB SERVER ---
async def stream_handler(request):
    code = request.match_info['code']
    data = link_storage.get(code)
    if not data: return web.Response(text="Link Expired", status=404)
    message, file_name = data['msg'], unquote(request.match_info['filename']) 
    file_size = message.file.size
    range_header = request.headers.get('Range')
    start, end = 0, file_size - 1
    if range_header:
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            if match.group(2): end = int(match.group(2))
    headers = {
        'Content-Disposition': f'attachment; filename="{file_name}"', 
        'Content-Type': mimetypes.guess_type(file_name)[0] or 'application/octet-stream',
        'Content-Length': str(end - start + 1), 'Accept-Ranges': 'bytes', 'Connection': 'keep-alive'
    }
    if request.method == "HEAD": return web.Response(headers=headers)
    response = web.StreamResponse(status=206 if range_header else 200, headers=headers)
    await response.prepare(request)
    chunk_size = 1048576
    offset = (start // chunk_size) * chunk_size
    skip, bytes_to_send = start - offset, end - start + 1
    try:
        async for chunk in client.iter_download(message.media, offset=offset, request_size=chunk_size):
            if skip > 0:
                chunk = chunk[skip:]; skip = 0
            if bytes_to_send <= len(chunk):
                await response.write(chunk[:bytes_to_send]); break
            await response.write(chunk); bytes_to_send -= len(chunk)
    except: pass
    return response

# --- PREMIUM UPLOAD ENGINE ---
async def premium_upload(client, file_path, msg, filename):
    file_size = os.path.getsize(file_path)
    part_size = 512 * 1024
    total_parts = math.ceil(file_size / part_size)
    file_id = random.getrandbits(63)
    state, start_time = {'bytes': 0}, time.time()
    semaphore = asyncio.Semaphore(100) 
    
    async def upload_part(idx):
        async with semaphore:
            async with aiofiles.open(file_path, 'rb') as f:
                await f.seek(idx * part_size)
                chunk = await f.read(part_size)
            await client(SaveBigFilePartRequest(file_id, idx, total_parts, chunk))
            state['bytes'] += len(chunk)

    async def updater():
        while state['bytes'] < file_size:
            await asyncio.sleep(3)
            speed = (state['bytes'] / (time.time() - start_time)) / 1024 / 1024
            perc = (state['bytes'] / file_size) * 100
            try: await msg.edit(f"⬆️ **Premium Uploading**\n📊 `{perc:.2f}%` 🚀 `{speed:.2f} MB/s`")
            except: pass

    u_task = asyncio.create_task(updater())
    await asyncio.gather(*[upload_part(i) for i in range(total_parts)])
    u_task.cancel()
    return InputFileBig(file_id, total_parts, os.path.basename(file_path))

# --- MESSAGE HANDLER ---
@client.on(events.NewMessage(incoming=True, from_users=ADMIN_ID))
async def handle_new_message(event):
    if event.file:
        code = secrets.token_urlsafe(8)
        link_storage[code] = {'msg': event, 'timestamp': time.time()}
        safe_name = quote(event.file.name or 'video.mp4')
        await event.reply(f"🚀 **Fast Stream Link:**\n`{PUBLIC_URL}/{code}/{safe_name}`")
        return
        
    if event.text and event.text.startswith("http"):
        text = event.text.strip()
        url = text.split(" -n ")[0]
        filename = text.split(" -n ")[1] if " -n " in text else "video.mp4"
        if not '.' in filename: filename += ".mp4"
        
        msg = await event.reply("⚡ **Downloading to T4 NVMe...**")
        
        try:
            # 1. Download URL
            async with ClientSession(connector=TCPConnector(limit=0)) as sess:
                async with sess.get(url) as resp:
                    with open(filename, 'wb') as f:
                        async for c in resp.content.iter_chunked(4*1024*1024): f.write(c)
            
            # 2. Fix Video & Metadata (THE FIX FOR BLACK SCREEN)
            fixed_file = fix_video_header(filename)
            meta = get_video_metadata(fixed_file)
            
            # 3. Fast Upload
            await msg.edit("⬆️ **Blasting to Telegram...**")
            tg_file = await premium_upload(client, fixed_file, msg, filename)
            
            # 4. Send with Attributes
            await client.send_file(
                "me", 
                file=tg_file, 
                thumb=meta['thumb'], # Real Thumbnail
                caption=f"✅ `{filename}`", 
                supports_streaming=True,
                attributes=[types.DocumentAttributeVideo(
                    duration=meta['duration'], # Real Duration
                    w=meta['w'], h=meta['h'], 
                    supports_streaming=True
                )]
            )
            await msg.delete()
        except Exception as e: await event.reply(f"❌ Error: {e}")
        finally:
            for f in [filename, "fixed_"+filename, filename+".jpg"]:
                if os.path.exists(f): os.remove(f)

# --- STARTUP ---
async def start_all():
    setup_tunnel()
    app = web.Application()
    app.router.add_get('/{code}/{filename}', stream_handler)
    app.router.add_get('/', lambda r: web.Response(text="Online"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 8000).start()
    
    print("Connecting to Telegram...")
    await client.start(phone=PHONE) 
    print("✅ SYSTEM READY")
    await client.run_until_disconnected()

await start_all()

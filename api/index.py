from flask import Flask, request, redirect, abort
import telebot
from telebot.types import Update
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

# --- CONFIGURATION ---
BOT_TOKEN = '7806492319:AAEPVCiqYZgsVe81cxP4o1X7XGhM_6mlTVk'
SECRET_KEY = 'super_secret_vercel_key_999' 
EXPIRATION_SECONDS = 24 * 60 * 60  # 24 Hours

# --- SETUP ---
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
s = URLSafeTimedSerializer(SECRET_KEY)

@app.route('/')
def home():
    return "Bot is running on Vercel.", 200

# --- DOWNLOAD ROUTE ---
@app.route('/d/<token>')
def download(token):
    try:
        # 1. Verify 24-hour expiration
        file_id = s.loads(token, max_age=EXPIRATION_SECONDS)
        
        # 2. Get a FRESH link from Telegram
        file_info = bot.get_file(file_id)
        
        # 3. Redirect user to the actual file
        direct_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        return redirect(direct_url)
        
    except SignatureExpired:
        return "❌ Error: This link has expired (24 hour limit).", 410
    except BadSignature:
        return "❌ Error: Invalid link.", 400
    except Exception as e:
        return f"System Error: {str(e)}", 500

# --- WEBHOOK ROUTE ---
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

# --- BOT HANDLERS ---
@bot.message_handler(content_types=['document', 'audio', 'video', 'photo'])
def handle_file(message):
    try:
        file_id = None
        file_name = "file"
        file_size = 0
        
        # Detect file type and get ID
        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name
            file_size = message.document.file_size
        elif message.video:
            file_id = message.video.file_id
            file_name = "video.mp4"
            file_size = message.video.file_size
        elif message.audio:
            file_id = message.audio.file_id
            file_name = "audio.mp3"
            file_size = message.audio.file_size
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_name = "photo.jpg"
            file_size = message.photo[-1].file_size
            
        # --- CHECK FILE SIZE (Vercel Limit) ---
        # 20MB = 20 * 1024 * 1024 bytes approx
        if file_size > 19 * 1024 * 1024: 
            bot.reply_to(message, "⚠️ <b>File too big!</b>\n\nTelegram Bots on Vercel can only handle files under <b>20MB</b>.\nPlease send a smaller file.", parse_mode='HTML')
            return

        # Generate Link
        if file_id:
            token = s.dumps(file_id)
            host_url = request.host_url.rstrip('/')
            download_link = f"{host_url}/d/{token}"
            
            reply_text = (
                f"✅ <b>Link Generated!</b>\n\n"
                f"📂 <b>Name:</b> {file_name}\n"
                f"🔗 <b>Link:</b> {download_link}\n\n"
                f"⏳ <i>Expires in 24 hours</i>"
            )
            bot.reply_to(message, reply_text, parse_mode='HTML')
            
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Send me a file (Max 20MB) to get a 24h direct link.")

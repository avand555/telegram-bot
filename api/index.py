from flask import Flask, request, redirect, abort
import telebot
from telebot.types import Update
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

# --- CONFIGURATION ---
# I have inserted your specific token below:
BOT_TOKEN = '7806492319:AAEPVCiqYZgsVe81cxP4o1X7XGhM_6mlTVk'

# I generated a random secret key for you (keeps links secure):
SECRET_KEY = 'super_secret_key_x92_vm3_kLQ_992' 

EXPIRATION_SECONDS = 24 * 60 * 60 # 24 Hours

# --- SETUP ---
app = Flask(__name__)
# threaded=False is important for Vercel/Serverless
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# Serializer to encode/decode data into the URL
s = URLSafeTimedSerializer(SECRET_KEY)

@app.route('/')
def home():
    return "Bot is running.", 200

# --- DOWNLOAD ROUTE ---
@app.route('/d/<token>')
def download(token):
    try:
        # Verify token and expiration
        file_id = s.loads(token, max_age=EXPIRATION_SECONDS)
        
        # Get fresh link from Telegram
        file_info = bot.get_file(file_id)
        direct_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        
        # Redirect user
        return redirect(direct_url)
        
    except SignatureExpired:
        return "Error: Link has expired (24h limit).", 410
    except BadSignature:
        return "Error: Invalid link.", 400
    except Exception as e:
        return f"Error: {str(e)}", 500

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

# --- BOT LOGIC ---
@bot.message_handler(content_types=['document', 'audio', 'video', 'photo'])
def handle_file(message):
    try:
        # 1. Extract File ID and Name
        file_id = None
        file_name = "file"
        
        if message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name
        elif message.video:
            file_id = message.video.file_id
            file_name = "video.mp4"
        elif message.audio:
            file_id = message.audio.file_id
            file_name = "audio.mp3"
        elif message.photo:
            file_id = message.photo[-1].file_id
            file_name = "photo.jpg"
            
        if not file_id:
            bot.reply_to(message, "Could not identify file content.")
            return

        # 2. Create the stateless token
        token = s.dumps(file_id)
        
        # 3. Get the Vercel URL
        host_url = request.host_url.rstrip('/')
        download_link = f"{host_url}/d/{token}"
        
        reply_text = (
            f"✅ <b>File Ready!</b>\n"
            f"📂 {file_name}\n\n"
            f"🔗 <b>Link:</b> {download_link}\n\n"
            f"⏳ <i>Expires in 24 hours.</i>"
        )
        
        bot.reply_to(message, reply_text, parse_mode='HTML')
        
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Send me a file to get a direct download link.")

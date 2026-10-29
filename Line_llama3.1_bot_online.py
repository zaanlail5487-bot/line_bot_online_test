import os
import uuid
import requests
import yt_dlp
import imageio_ffmpeg
from flask import Flask, request, abort, send_from_directory, make_response, jsonify
from dotenv import load_dotenv
from openai import OpenAI

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    BroadcastRequest,
    TextMessage,
    AudioMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# 載入 .env 設定
load_dotenv()

app = Flask(__name__)

# LINE Bot SDK (v3) 設定
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))

# OpenRouter AI 設定 (替代本地 Ollama)
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=("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"),
)
# 這裡使用 OpenRouter 上的 Llama 3.1 免費模型（或依需求更換）
MODEL_NAME = "meta-llama/llama-3.1-8b-instruct:free"

# 伺服器網址與音樂暫存目錄
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:5000").rstrip('/')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)


def send_broadcast_message(text_content: str) -> bool:
    """廣播/群發訊息給所有好友"""
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            broadcast_request = BroadcastRequest(
                messages=[TextMessage(text=text_content)]
            )
            line_bot_api.broadcast(broadcast_request)
            print(f"[群發成功]: {text_content}")
            return True
    except Exception as e:
        print(f"[群發失敗]: {e}")
        return False


def download_youtube_audio(query: str):
    """搜尋 YouTube 並轉碼為標準 AAC (.m4a)，使用 Render 內建的 imageio-ffmpeg"""
    file_id = str(uuid.uuid4())[:8]
    output_template = os.path.join(AUDIO_DIR, f"{file_id}.%(ext)s")
    
    # 自動取得套件內建的 ffmpeg 執行檔路徑，適配 Render 雲端環境
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

    ydl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'outtmpl': output_template,
        'ffmpeg_location': ffmpeg_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '192',
        }],
        'postprocessor_args': ['-c:a', 'aac', '-b:a', '128k'],
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            if 'entries' in info and len(info['entries']) > 0:
                video = info['entries'][0]
                filename = f"{file_id}.m4a"
                duration_ms = int(video.get('duration', 0) * 1000)
                return {
                    'title': video.get('title'),
                    'filename': filename,
                    'duration': duration_ms
                }
    except Exception as e:
        print(f"[yt-dlp 錯誤]: {e}")
    return None


def get_ai_response(user_message: str) -> str:
    """透過 OpenRouter API 進行繁體中文對話"""
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "你是一個 Line 上的繁體中文 AI 助手，請簡潔親切地回答問題。"},
                {"role": "user", "content": user_message}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[OpenRouter 錯誤]: {e}")
        return "AI 回應失敗，請確認 OpenRouter API Key 是否正確設定。"


# ---------------- HTTP 群發 API (供外部網址/腳本觸發) ----------------

@app.route('/broadcast', methods=['POST', 'GET'])
def http_broadcast():
    msg = request.args.get('msg')
    if not msg and request.is_json:
        msg = request.json.get('message')

    if not msg:
        return jsonify({"status": "error", "reason": "未提供訊息內容 (msg)"}), 400

    success = send_broadcast_message(msg)
    if success:
        return jsonify({"status": "success", "message": f"成功群發：{msg}"}), 200
    else:
        return jsonify({"status": "error", "reason": "群發失敗，請確認 Access Token"}), 500


# ---------------- 靜態音訊檔案路由 (繞過防護) ----------------

@app.route('/static/audio/<filename>')
def serve_audio(filename):
    response = make_response(send_from_directory(AUDIO_DIR, filename, mimetype="audio/mp4"))
    response.headers['ngrok-skip-browser-warning'] = 'true'
    response.headers['User-Agent'] = 'LINE-Bot'
    response.headers['Accept-Ranges'] = 'bytes'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


# ---------------- LINE Webhook 接收端 ----------------

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # 1. 無權限限制的群發功能
        if user_text.startswith("群發 "):
            broadcast_msg = user_text.replace("群發 ", "").strip()
            
            if not broadcast_msg:
                reply_text = "請輸入要群發的內容，例如：群發 大家好！"
            elif send_broadcast_message(broadcast_msg):
                reply_text = f"📢 群發發送成功！\n內容：{broadcast_msg}"
            else:
                reply_text = "❌ 群發失敗，請檢查 API 設定。"

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
            return

        # 2. 點歌模式
        if user_text.startswith("點歌"):
            song_name = user_text.replace("點歌", "").strip()
            if not song_name:
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text="請輸入歌名，例如：點歌 周杰倫 稻香")]
                    )
                )
                return

            song_info = download_youtube_audio(song_name)
            if song_info:
                audio_url = f"{SERVER_URL}/static/audio/{song_info['filename']}"
                print(f"[發送音訊網址]: {audio_url}")
                reply_messages = [
                    TextMessage(text=f"🎵 為您播放：{song_info['title']}"),
                    AudioMessage(
                        original_content_url=audio_url,
                        duration=song_info['duration']
                    )
                ]
            else:
                reply_messages = [TextMessage(text="抱歉，找不到或無法處理該首歌曲！")]

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=reply_messages
                )
            )
        else:
            # 3. AI 聊天模式 (透過 OpenRouter)
            ai_reply = get_ai_response(user_text)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=ai_reply)]
                )
            )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

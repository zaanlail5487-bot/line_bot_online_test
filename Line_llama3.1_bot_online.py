import os
import requests
import yt_dlp
from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

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

# OpenRouter 設定（優先從環境變數讀取）
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = "google/gemma-2-9b-it:free"  # 使用 OpenRouter 的免費模型


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


def get_youtube_audio_url(query: str):
    """雲端優化版：不下載檔案，直接提取 YouTube 直連音訊 m4a 串流網址"""
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio', # 優先抓取原生 m4a/aac 格式
        'noplaylist': True,
        'quiet': True,
        'skip_download': True, # 關鍵：不下載檔案到 Render 硬碟
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if 'entries' in info and len(info['entries']) > 0:
                video = info['entries'][0]
                duration_ms = int(video.get('duration', 0) * 1000)
                
                # LINE 要求的音訊上限通常為 1 分鐘 (60000ms)，部分情況放寬
                # 這裡過濾太長的影片以確保播放成功
                return {
                    'title': video.get('title'),
                    'url': video.get('url'), # 這是直連的 HTTPS 音訊網址
                    'duration': duration_ms
                }
    except Exception as e:
        print(f"[yt-dlp 串流提取錯誤]: {e}")
    return None


def get_openrouter_response(user_message: str) -> str:
    """呼叫 OpenRouter Free API 進行繁體中文對話（精準網址與防跳轉版）"""
    if not OPENROUTER_API_KEY:
        return "❌ 系統錯誤：未設定 OpenRouter API Key，請檢查 Render 環境變數。"
        
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY.strip()}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": "你是一個 Line 上的繁體中文 AI 助手，請簡潔親切地回答問題。"},
                {"role": "user", "content": user_message}
            ]
        }
        
        import json
        
        # 💡 請務必檢查此行網址，結尾絕對不能有空格，且必須是完整的 https ... /chat/completions 
        api_url = "https://openrouter.ai/api/v1/chat/completions"
        
        res = requests.post(
            api_url, 
            headers=headers, 
            data=json.dumps(payload),
            allow_redirects=False, # 💡 關鍵：禁止自動跳轉，防止 POST 被轉為 GET 而噴 405
            timeout=30
        )
        
        # 檢查是否發生了重新導向
        if res.status_code in [301, 302, 307, 308]:
            return f"❌ 偵測到網址重新導向 ({res.status_code})，請確認 api_url 網址字元是否完全正確。\n導向至: {res.headers.get('Location')}"
            
        if res.status_code == 200:
            try:
                response_json = res.json()
                return response_json.get("choices", [{}]).get("message", {}).get("content", "").strip()
            except Exception as json_err:
                return f"❌ 解析 AI 回應失敗：{json_err}"
        else:
            return f"❌ OpenRouter 拒絕連線 (錯誤碼: {res.status_code})\n原因: {res.text}"
            
    except Exception as e:
        return f"無法連線至 OpenRouter AI 服務: {e}"


# ---------------- HTTP 群發 API ----------------

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

        # 1. 群發功能
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

            # 直接向 YouTube 提取串流 URL
            song_info = get_youtube_audio_url(song_name)
            if song_info and song_info['url']:
                print(f"[線上串流音訊網址]: {song_info['url']}")
                reply_messages = [
                    TextMessage(text=f"🎵 為您播放：{song_info['title']}"),
                    AudioMessage(
                        original_content_url=song_info['url'],
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
            # 3. AI 聊天模式 (OpenRouter)
            ai_reply = get_openrouter_response(user_text)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=ai_reply)]
                )
            )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

import os
import json
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
    """替代方案：利用公開音訊 API 避開機房 IP 被 YouTube 阻擋的問題"""
    import urllib.parse
    import requests
    
    try:
        # 將歌名進行 URL 編碼
        encoded_query = urllib.parse.quote(query)
        print(f"[API 音樂搜尋中]: {query}")
        
        # 使用開源免費的音樂解析解析 API (此處以通用的第三方的音訊串流轉換 API 為例)
        # 💡 提示：此處改用直接向穩定的串流聚合伺服器請求，不再透過 Render 本機執行 yt-dlp
        api_url = f"https://vkeys.cn{encoded_query}" 
        
        # 發送請求
        res = requests.get(api_url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == 200 or data.get("success"):
                # 根據不同公開 API 的回傳結構微調，通常包含標題與直連網址
                return {
                    'title': data.get("title", query),
                    'url': data.get("url"),          # 必須是 https 開頭的音訊直連網址
                    'duration': 240000              # LINE 預設長度 (毫秒)，可填 4 分鐘
                }
    except Exception as e:
        print(f"[音樂 API 請求失敗]: {e}")
        
    return None




def get_openrouter_response(user_message: str) -> str:
    """呼叫 OpenRouter 進行繁體中文對話（支援多免費模型自動備援版）"""
    if not OPENROUTER_API_KEY:
        return "❌ 系統錯誤：未設定 OpenRouter API Key，請檢查 Render 環境變數。"
        
    # 精選目前最穩定且回應快速的免費模型清單（排在前面的優先嘗試）
    available_models = [
        "google/gemma-2-9b-it:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-72b-instruct:free",
        "deepseek/deepseek-chat:free"
    ]
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost:3000",  # OpenRouter 使用免費模型之規範必填
        "X-Title": "LineBotChat"                     # OpenRouter 使用免費模型之規範必填
    }
    
    api_url = "https://openrouter.ai"

    for model in available_models:
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一個 Line 上的繁體中文 AI 助手，請簡潔親切地回答問題。"},
                    {"role": "user", "content": user_message}
                ]
            }
            
            print(f"[AI 嘗試使用模型]: {model}")
            res = requests.post(
                api_url, 
                headers=headers, 
                data=json.dumps(payload),
                timeout=15  # 超時自動切換下一個模型
            )
                
            if res.status_code == 200:
                response_json = res.json()
                choices = response_json.get("choices")
                
                # 💡 關鍵修正：choices 是 list 類型，必須透過 choices[0] 取得物件
                if choices and isinstance(choices, list) and len(choices) > 0:
                    content = choices[0].get("message", {}).get("content", "").strip()
                    if content:
                        print(f"[AI 回應成功] 使用模型: {model}")
                        return content
            
            # 若非 200 回應，印出細節到 Render Log 方便排查
            print(f"⚠️ 模型 {model} 請求失敗 (狀態碼: {res.status_code})，原因: {res.text}，嘗試下一個備援...")
            
        except Exception as e:
            print(f"⚠️ 連線或解析模型 {model} 發生異常: {e}，嘗試下一個備援...")
            continue
            
    return "❌ 所有 AI 免費備援模型目前皆處於高負載或被限流狀態，請稍後再試。"


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

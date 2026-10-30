import os
import json
import requests
import urllib.parse
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

# OpenRouter 設定
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


def get_online_audio_url(query: str):
    """【全新防封鎖版】完全不用 yt-dlp，聚合多個獨立高防護音樂 API 管道"""
    encoded_query = urllib.parse.quote(query)
    print(f"[全網音樂搜尋中]: {query}")
    
    # ---------------- 管道一：網易雲/QQ 音樂聚合解析 (極度穩定、速度極快) ----------------
    try:
        netease_api = f"https://uomg.com{encoded_query}&format=json"
        res = requests.get(netease_api, timeout=7)
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == 1 and data.get("data", {}).get("url"):
                music_info = data.get("data", {})
                print(f"[管道一成功] 成功提取: {music_info.get('name')}")
                return {
                    'title': f"{music_info.get('name')} - {music_info.get('artistsname')}",
                    'url': music_info.get('url'),
                    'duration': 240000  # 預設 4 分鐘
                }
    except Exception as e:
        print(f"⚠️ 管道一嘗試失敗: {e}，切換至下一管道...")

    # ---------------- 管道二：通用公共影音 API (免 Key 轉 YouTube 音訊網址) ----------------
    try:
        youtube_api = f"https://p6p.cn{encoded_query}"
        res = requests.get(youtube_api, timeout=7)
        if res.status_code == 200:
            data = res.json()
            if (data.get("code") == 200 or data.get("success")) and data.get("url"):
                print(f"[管道二成功] 從開源 YouTube API 成功提取: {query}")
                return {
                    'title': data.get("title", query),
                    'url': data.get("url"),
                    'duration': 240000
                }
    except Exception as e:
        print(f"⚠️ 管道二嘗試失敗: {e}，切換至下一管道...")

    # ---------------- 管道三：雲端影音直連備援介面 ----------------
    try:
        backup_api = f"https://nexmoe.com{encoded_query}"
        res = requests.get(backup_api, timeout=7)
        if res.status_code == 200:
            data = res.json()
            if data and data.get("url"):
                print(f"[管道三成功] 從備援影音介面成功提取: {query}")
                return {
                    'title': data.get("title", query),
                    'url': data.get("url"),
                    'duration': int(data.get("duration", 240)) * 1000 if "duration" in data else 240000
                }
    except Exception as e:
        print(f"⚠️ 管道三嘗試失敗: {e}")

    print(f"❌ [全網音樂搜尋失敗]: {query} 無法在任何開源管道中解析音訊。")
    return None


def get_openrouter_response(user_message: str) -> str:
    """呼叫 OpenRouter 進行繁體中文對話（多免費模型自動輪詢備援機制）"""
    if not OPENROUTER_API_KEY:
        return "❌ 系統錯誤：未設定 OpenRouter API Key，請檢查 Render 環境變數。"
        
    available_models = [
        "google/gemma-2-9b-it:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "qwen/qwen-2.5-72b-instruct:free",
        "deepseek/deepseek-chat:free"
    ]
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost:3000",
        "X-Title": "LineBotChat"
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
            res = requests.post(api_url, headers=headers, data=json.dumps(payload), timeout=15)
                
            if res.status_code == 200:
                response_json = res.json()
                choices = response_json.get("choices")
                if choices and isinstance(choices, list) and len(choices) > 0:
                    content = choices[0].get("message", {}).get("content", "").strip()
                    if content:
                        print(f"[AI 回應成功] 使用模型: {model}")
                        return content
            
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

            # 💡 修正：呼叫全新的線上聚合解析函式
            song_info = get_online_audio_url(song_name)
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
                reply_messages = [TextMessage(text="抱歉，全網音樂管道目前暫時無法解析該首歌曲！")]

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

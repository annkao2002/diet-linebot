import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import storage  # our local storage module

app = Flask(__name__)

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

# ── 時段判斷 ──────────────────────────────────────────────
def get_meal_type_by_time():
    hour = datetime.now().hour
    if 5 <= hour < 10:
        return "早餐"
    elif 10 <= hour < 14:
        return "午餐"
    elif 14 <= hour < 17:
        return "下午茶"
    elif 17 <= hour < 21:
        return "晚餐"
    else:
        return "宵夜"

# ── 解析食材輸入 ──────────────────────────────────────────
def parse_food_input(text):
    """
    解析如: 雞胸肉150g 白米100g 花椰菜80g 油5g
    支援: g, kg, ml, cc, 克, 毫升, 公克
    """
    ingredients = []
    # 匹配 食材名稱 + 數字 + 單位
    pattern = r'([^\d\s，,、\n]+?)\s*(\d+(?:\.\d+)?)\s*(g|kg|ml|cc|克|毫升|公克|公升)?'
    matches = re.findall(pattern, text)
    for name, amount, unit in matches:
        name = name.strip('、，, ')
        if name and amount:
            unit = unit or 'g'
            ingredients.append({
                'name': name,
                'amount': float(amount),
                'unit': unit
            })
    return ingredients

# ── 判斷輸入類型 ──────────────────────────────────────────
def classify_input(text):
    t = text.strip()

    # 體重: 純數字 50~150，或含kg/公斤
    if re.match(r'^(\d{2,3}(?:\.\d{1,2})?)\s*(kg|公斤|KG)$', t, re.I):
        return 'weight'
    if re.match(r'^(\d{2,3}(?:\.\d{1,2})?)$', t):
        val = float(t)
        if 30 <= val <= 200:
            return 'weight'

    # 飲水: 含ml/cc/毫升/杯/升
    if re.search(r'\d+\s*(ml|cc|毫升|公升|升|杯)', t, re.I):
        return 'water'

    # 睡眠
    if re.search(r'睡(了|覺|眠)?|起床|就寢|\d+小時', t):
        return 'sleep'
    if re.search(r'\d{1,2}[：:點]\d{2}.*[到至~\-].*\d{1,2}[：:點]\d{2}', t):
        return 'sleep'

    # 排便
    if re.search(r'排便|大便|便便|上大號|便秘|拉肚子|順暢|硬便|軟便', t):
        return 'poop'

    # 心得/筆記
    if re.search(r'心得|感覺|覺得|狀態|今天很|身體|飽|餓|不舒服|備註|筆記', t):
        return 'note'

    # 指令
    if re.search(r'^(今日|今天)?(報告|摘要|總結|記錄|日誌|查看|看一下)', t):
        return 'summary'
    if t in ['說明', '幫助', 'help', '怎麼用', '功能']:
        return 'help'

    # 食物（有食材+克數 或 含常見餐食關鍵字）
    if re.search(r'\d+\s*(g|克|公克|份|碗|片|顆|條)', t):
        return 'meal'
    if re.search(r'吃了|早餐|午餐|晚餐|下午茶|宵夜|點心', t):
        return 'meal'

    # 預設也當餐食記錄（純文字描述）
    return 'meal'

# ── 解析體重 ──────────────────────────────────────────────
def parse_weight(text):
    m = re.search(r'(\d{2,3}(?:\.\d{1,2})?)', text)
    return float(m.group(1)) if m else None

# ── 解析飲水 ──────────────────────────────────────────────
def parse_water(text):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(ml|cc|毫升|公升|升|杯)', text, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ['杯']:
        val *= 250
    elif unit in ['升', '公升']:
        val *= 1000
    return int(val)

# ── 解析睡眠 ──────────────────────────────────────────────
def parse_sleep(text):
    # 格式: 7小時 / 7.5小時
    m = re.search(r'(\d+(?:\.\d+)?)\s*小時', text)
    if m:
        return {'hours': float(m.group(1))}
    # 格式: 23:00到6:30
    m = re.search(r'(\d{1,2})[：:點](\d{2})?\s*[到至~\-]\s*(\d{1,2})[：:點](\d{2})?', text)
    if m:
        bed_h, bed_m = int(m.group(1)), int(m.group(2) or 0)
        wake_h, wake_m = int(m.group(3)), int(m.group(4) or 0)
        mins = (wake_h * 60 + wake_m) - (bed_h * 60 + bed_m)
        if mins < 0:
            mins += 1440
        hours = round(mins / 60, 1)
        return {
            'hours': hours,
            'bed': f'{bed_h:02d}:{bed_m:02d}',
            'wake': f'{wake_h:02d}:{wake_m:02d}'
        }
    return None

# ── 解析排便 ──────────────────────────────────────────────
def parse_poop(text):
    if re.search(r'便秘', text):
        status = '便秘 ⚠️'
    elif re.search(r'拉肚子|稀|水', text):
        status = '稀軟 ⚠️'
    elif re.search(r'硬', text):
        status = '偏硬'
    elif re.search(r'順暢|正常|ok|OK', text):
        status = '順暢 ✓'
    elif re.search(r'軟', text):
        status = '偏軟'
    else:
        status = '有記錄 ✓'
    return status

# ── 產生回覆文字 ──────────────────────────────────────────
def format_summary(user_id):
    today = datetime.now().strftime('%Y-%m-%d')
    data = storage.get_day(user_id, today)

    lines = [f"📋 {today} 飲控日誌\n"]

    # 體重
    if data.get('weight'):
        lines.append(f"⚖️ 體重：{data['weight']} kg")

    # 飲水
    if data.get('water'):
        lines.append(f"💧 飲水：{data['water']} ml")

    # 睡眠
    if data.get('sleep'):
        s = data['sleep']
        if s.get('bed'):
            lines.append(f"🌙 睡眠：{s['bed']}～{s['wake']}（{s['hours']}小時）")
        else:
            lines.append(f"🌙 睡眠：{s['hours']} 小時")

    # 排便
    if data.get('poop'):
        lines.append(f"🚽 排便：{data['poop']}")

    # 餐食
    meals = data.get('meals', [])
    if meals:
        lines.append("\n🍱 餐食記錄：")
        for meal in meals:
            lines.append(f"  【{meal['type']}】{meal['name']}")
            for ing in meal.get('ingredients', []):
                lines.append(f"    · {ing['name']} {ing['amount']}{ing['unit']}")
            if meal.get('oil'):
                lines.append(f"    · 用油 {meal['oil']}")

    # 心得
    notes = data.get('notes', [])
    if notes:
        lines.append("\n📝 心得筆記：")
        for note in notes:
            lines.append(f"  {note['text']}")

    if len(lines) == 1:
        lines.append("今天還沒有記錄喔，快開始吧！")

    return '\n'.join(lines)

def format_help():
    return """🌿 飲控日記使用說明

━━ 餐食記錄 ━━
直接輸入食材即可！
例：雞胸肉150g 白米100g 花椰菜80g 油5g

系統會依時段自動判斷
早餐(5-10點)、午餐(10-14點)
下午茶(14-17點)、晚餐(17-21點)
宵夜(21點後)

━━ 其他記錄 ━━
體重：65.5  或  65.5kg
飲水：500ml  或  喝了2杯水
睡眠：睡了7小時  或  23:00到6:30
排便：排便順暢  或  便秘
心得：今天感覺很飽足

━━ 查看記錄 ━━
輸入「今日報告」查看今天所有紀錄"""

# ── 主要處理邏輯 ──────────────────────────────────────────
def handle_text(user_id, text):
    today = datetime.now().strftime('%Y-%m-%d')
    now_time = datetime.now().strftime('%H:%M')
    input_type = classify_input(text)

    if input_type == 'help':
        return format_help()

    if input_type == 'summary':
        return format_summary(user_id)

    if input_type == 'weight':
        val = parse_weight(text)
        if val:
            storage.save_weight(user_id, today, val)
            return f"⚖️ 體重記錄：{val} kg\n時間：{now_time}\n\n輸入「今日報告」可查看今天所有紀錄"

    if input_type == 'water':
        ml = parse_water(text)
        if ml:
            total = storage.add_water(user_id, today, ml)
            return f"💧 飲水記錄：+{ml} ml\n今日累計：{total} ml\n目標：2000 ml（{'✓ 達標！' if total >= 2000 else f'還差 {2000-total} ml'}）"

    if input_type == 'sleep':
        info = parse_sleep(text)
        if info:
            storage.save_sleep(user_id, today, info)
            hours = info['hours']
            eval_text = '充足 👍' if hours >= 7 else ('尚可' if hours >= 6 else '不足，注意休息 ⚠️')
            if info.get('bed'):
                return f"🌙 睡眠記錄\n就寢：{info['bed']}\n起床：{info['wake']}\n時長：{hours} 小時（{eval_text}）"
            return f"🌙 睡眠記錄：{hours} 小時（{eval_text}）"

    if input_type == 'poop':
        status = parse_poop(text)
        storage.save_poop(user_id, today, status)
        return f"🚽 排便記錄：{status}\n時間：{now_time}"

    if input_type == 'note':
        storage.add_note(user_id, today, text)
        return f"📝 心得已記錄：\n「{text}」"

    # 預設：餐食記錄
    meal_type = get_meal_type_by_time()
    # 偵測是否有明確指定餐別
    for keyword in ['早餐', '午餐', '晚餐', '下午茶', '宵夜', '點心']:
        if keyword in text:
            meal_type = keyword
            break

    ingredients = parse_food_input(text)
    # 抓取油量
    oil = None
    oil_match = re.search(r'(?:食用)?油\s*(\d+(?:\.\d+)?)\s*(g|克|ml)?', text)
    if oil_match:
        oil = f"{oil_match.group(1)}{oil_match.group(2) or 'g'}"

    # 若完全無法解析食材，把整段文字當作餐食名稱
    meal_name = text
    if ingredients:
        # 用第一個食材名稱或全文當標題
        meal_name = ingredients[0]['name'] if len(ingredients) == 1 else text[:20]

    meal = {
        'type': meal_type,
        'name': meal_name,
        'ingredients': ingredients,
        'oil': oil,
        'time': now_time
    }
    storage.add_meal(user_id, today, meal)

    # 組回覆
    reply_lines = [f"🍱 {meal_type}已記錄！（{now_time}）"]
    if ingredients:
        for ing in ingredients:
            reply_lines.append(f"  · {ing['name']} {ing['amount']}{ing['unit']}")
        if oil:
            reply_lines.append(f"  · 用油 {oil}")
    else:
        reply_lines.append(f"  · {meal_name}")
    reply_lines.append("\n輸入「今日報告」查看完整記錄")
    return '\n'.join(reply_lines)

# ── Webhook ───────────────────────────────────────────────
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text
    reply_token = event.reply_token

    response_text = handle_text(user_id, text)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=response_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

import os
import json
import re
from datetime import datetime
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    QuickReply, QuickReplyItem, MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import storage

app = Flask(__name__)
configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))

# ── 用戶對話狀態 ─────────────────────────────────────────
user_state = {}

def get_state(uid):
    if uid not in user_state:
        user_state[uid] = {'step': None, 'data': {}}
    return user_state[uid]

def clear_state(uid):
    user_state[uid] = {'step': None, 'data': {}}

def today():
    return datetime.now().strftime('%Y-%m-%d')

def now_time():
    return datetime.now().strftime('%H:%M')

# ── 解析函式 ─────────────────────────────────────────────
def parse_ingredients(text):
    out = []
    for m in re.finditer(r'([^\d\s，,、\n]+?)\s*(\d+(?:\.\d+)?)\s*(g|kg|ml|cc|克|毫升|公克)?', text):
        name = m.group(1).strip('、，, ')
        if name and m.group(2):
            out.append({'name': name, 'amount': float(m.group(2)), 'unit': m.group(3) or 'g'})
    return out

def parse_weight(text):
    m = re.search(r'(\d{2,3}(?:\.\d{1,2})?)', text)
    return float(m.group(1)) if m else None

def parse_water(text):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(ml|cc|毫升|升|杯)', text, re.I)
    if not m: return None
    val = float(m.group(1))
    u = m.group(2).lower()
    if u == '杯': val *= 250
    elif u == '升': val *= 1000
    return int(val)

def parse_sleep(text):
    m = re.search(r'(\d+(?:\.\d+)?)\s*小時', text)
    if m: return {'hours': float(m.group(1))}
    m = re.search(r'(\d{1,2})[：:點](\d{2})?\s*[到至~\-]\s*(\d{1,2})[：:點](\d{2})?', text)
    if m:
        bh, bm = int(m.group(1)), int(m.group(2) or 0)
        wh, wm = int(m.group(3)), int(m.group(4) or 0)
        mins = (wh*60+wm) - (bh*60+bm)
        if mins < 0: mins += 1440
        return {'hours': round(mins/60, 1),
                'bed': f'{bh:02d}:{bm:02d}', 'wake': f'{wh:02d}:{wm:02d}'}
    return None

def parse_poop(text):
    if re.search(r'便秘', text): return '便秘 ⚠️'
    if re.search(r'拉肚子|稀|水便', text): return '稀軟 ⚠️'
    if re.search(r'硬', text): return '偏硬'
    if re.search(r'順暢|正常', text): return '順暢 ✓'
    if re.search(r'軟', text): return '偏軟'
    return '有記錄 ✓'

def parse_nutrition(text):
    nut = {}
    for key, pat in {
        'kcal': r'(?:熱量|卡路里|卡|kcal)\s*[:：]?\s*(\d+(?:\.\d+)?)',
        'carb': r'(?:碳水化合物|碳水|醣類)\s*[:：]?\s*(\d+(?:\.\d+)?)',
        'protein': r'(?:蛋白質|蛋白)\s*[:：]?\s*(\d+(?:\.\d+)?)',
        'fat': r'(?:脂肪|油脂)\s*[:：]?\s*(\d+(?:\.\d+)?)',
    }.items():
        m = re.search(pat, text, re.I)
        if m: nut[key] = float(m.group(1))
    return nut

# ── Quick Reply ──────────────────────────────────────────
def meal_quick_reply():
    items = [QuickReplyItem(action=MessageAction(label=m, text=f'__餐別__{m}'))
             for m in ['早餐', '午餐', '晚餐', '下午茶', '宵夜', '點心']]
    return QuickReply(items=items)

# ── 回覆 ─────────────────────────────────────────────────
def reply(reply_token, text, qr=None):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=reply_token,
                                messages=[TextMessage(text=text, quick_reply=qr)])
        )

# ── 今日總覽 ─────────────────────────────────────────────
def build_summary(uid):
    data = storage.get_day(uid, today())
    lines = [f'📋 {today()} 飲控日誌', '─'*18]
    if data.get('weight'): lines.append(f'⚖️ 體重：{data["weight"]} kg')
    if data.get('water'):
        pct = min(100, round(data['water']/2000*100))
        lines.append(f'💧 飲水：{data["water"]} ml（目標 {pct}%）')
    if data.get('sleep'):
        s = data['sleep']
        if s.get('bed'): lines.append(f'🌙 睡眠：{s["bed"]}～{s["wake"]}（{s["hours"]}h）')
        else: lines.append(f'🌙 睡眠：{s["hours"]} 小時')
    if data.get('poop'): lines.append(f'🚽 排便：{data["poop"]}')

    meals = data.get('meals', [])
    if meals:
        lines.append(f'\n🍱 餐食（{len(meals)} 餐）')
        for meal in meals:
            lines.append(f'【{meal["type"]}】{meal["name"]}　{meal.get("time","")}')
            for ing in meal.get('ingredients', []):
                lines.append(f'  · {ing["name"]} {ing["amount"]}{ing["unit"]}')
            if meal.get('oil'): lines.append(f'  · 用油 {meal["oil"]}')
            n = meal.get('nutrition', {})
            parts = []
            if n.get('kcal'): parts.append(f'{n["kcal"]}kcal')
            if n.get('carb'): parts.append(f'碳水{n["carb"]}g')
            if n.get('protein'): parts.append(f'蛋白{n["protein"]}g')
            if n.get('fat'): parts.append(f'脂肪{n["fat"]}g')
            if parts: lines.append(f'  📊 {" | ".join(parts)}')
            if meal.get('note'): lines.append(f'  💬 {meal["note"]}')

    notes = data.get('notes', [])
    if notes:
        lines.append('\n📝 備註')
        for note in notes: lines.append(f'  「{note["text"]}」')

    if len(lines) == 2:
        lines.append('今天還沒有記錄，請使用底部選單開始！')
    return '\n'.join(lines)

# ── 身體數據流程 ─────────────────────────────────────────
def start_body(uid, reply_token):
    s = get_state(uid)
    s['step'] = 'body_weight'
    s['data'] = {}
    reply(reply_token, '⚖️ 記錄身體數據\n\n第 1/4：請輸入今日體重\n例如：65.5\n\n（輸入「跳過」略過此項）')

def handle_body(uid, text, reply_token):
    s = get_state(uid)
    step = s['step']

    if step == 'body_weight':
        if text != '跳過':
            val = parse_weight(text)
            if not val:
                reply(reply_token, '請輸入有效數字（例如：65.5），或輸入「跳過」')
                return
            storage.save_weight(uid, today(), val)
            s['data']['weight'] = val
        s['step'] = 'body_water'
        reply(reply_token, '💧 第 2/4：請輸入今日飲水量\n例如：1500ml、6杯\n\n（輸入「跳過」略過此項）')

    elif step == 'body_water':
        if text != '跳過':
            ml = parse_water(text)
            if not ml:
                reply(reply_token, '請輸入有效格式（例如：1500ml、6杯），或輸入「跳過」')
                return
            storage.add_water(uid, today(), ml)
            s['data']['water'] = ml
        s['step'] = 'body_sleep'
        reply(reply_token, '🌙 第 3/4：請輸入睡眠資訊\n例如：睡了7.5小時\n例如：23:00到6:30\n\n（輸入「跳過」略過此項）')

    elif step == 'body_sleep':
        if text != '跳過':
            info = parse_sleep(text)
            if not info:
                reply(reply_token, '請輸入有效格式（例如：睡了7小時），或輸入「跳過」')
                return
            storage.save_sleep(uid, today(), info)
            s['data']['sleep'] = info
        s['step'] = 'body_poop'
        reply(reply_token, '🚽 第 4/4：請輸入排便狀況\n可輸入：順暢、正常、偏硬、偏軟、便秘、拉肚子\n\n（輸入「跳過」略過此項）')

    elif step == 'body_poop':
        if text != '跳過':
            status = parse_poop(text)
            storage.save_poop(uid, today(), status)
            s['data']['poop'] = status
        d = s['data'].copy()
        clear_state(uid)
        lines = ['✅ 身體數據記錄完成！\n']
        if d.get('weight'): lines.append(f'⚖️ 體重：{d["weight"]} kg')
        if d.get('water'): lines.append(f'💧 飲水：{d["water"]} ml')
        if d.get('sleep'):
            sv = d['sleep']
            if sv.get('bed'): lines.append(f'🌙 睡眠：{sv["bed"]}～{sv["wake"]}（{sv["hours"]}h）')
            else: lines.append(f'🌙 睡眠：{sv["hours"]}小時')
        if d.get('poop'): lines.append(f'🚽 排便：{d["poop"]}')
        reply(reply_token, '\n'.join(lines))

# ── 餐食記錄流程 ─────────────────────────────────────────
def start_meal(uid, reply_token):
    s = get_state(uid)
    s['step'] = 'meal_type'
    s['data'] = {}
    reply(reply_token, '🍱 記錄餐食\n請選擇餐別 👇', qr=meal_quick_reply())

def handle_meal(uid, text, reply_token):
    s = get_state(uid)
    step = s['step']

    if step == 'meal_type':
        meal_type = text.replace('__餐別__', '')
        s['data']['type'] = meal_type
        s['data']['time'] = now_time()
        s['step'] = 'meal_name'
        reply(reply_token, f'【{meal_type}】\n\n第 1/4：請輸入餐食名稱\n例如：雞胸肉便當、水煮餐')

    elif step == 'meal_name':
        s['data']['name'] = text
        s['step'] = 'meal_ingredients'
        reply(reply_token,
            '第 2/4：請輸入食材與克數\n\n'
            '格式：食材名稱+數量，每行一項\n'
            '例如：\n雞胸肉 150g\n白米飯 100g\n花椰菜 80g\n食用油 5g\n\n'
            '（輸入「跳過」略過此項）')

    elif step == 'meal_ingredients':
        if text != '跳過':
            ings = parse_ingredients(text)
            oil_m = re.search(r'(?:食用)?油\s*(\d+(?:\.\d+)?)\s*(g|克|ml)?', text)
            s['data']['ingredients'] = ings
            s['data']['oil'] = f'{oil_m.group(1)}{oil_m.group(2) or "g"}' if oil_m else None
        else:
            s['data']['ingredients'] = []
            s['data']['oil'] = None
        s['step'] = 'meal_nutrition'
        reply(reply_token,
            '第 3/4：請輸入營養資訊\n\n'
            '格式範例（每行一項）：\n'
            '卡路里 500\n碳水 60\n蛋白質 30\n脂肪 10\n\n'
            '（輸入「跳過」略過此項）')

    elif step == 'meal_nutrition':
        if text != '跳過':
            s['data']['nutrition'] = parse_nutrition(text)
        else:
            s['data']['nutrition'] = {}
        s['step'] = 'meal_note'
        reply(reply_token, '第 4/4：餐後心得（選填）\n\n例如：飽足感不錯、有點油膩\n\n（輸入「跳過」完成記錄）')

    elif step == 'meal_note':
        if text != '跳過':
            s['data']['note'] = text
        meal = s['data'].copy()
        storage.add_meal(uid, today(), meal)
        clear_state(uid)

        lines = [f'✅ {meal["type"]}記錄完成！（{meal.get("time","")}）\n']
        lines.append(f'🍽 {meal["name"]}')
        for ing in meal.get('ingredients', []):
            lines.append(f'  · {ing["name"]} {ing["amount"]}{ing["unit"]}')
        if meal.get('oil'): lines.append(f'  · 用油 {meal["oil"]}')
        n = meal.get('nutrition', {})
        parts = []
        if n.get('kcal'): parts.append(f'{n["kcal"]}kcal')
        if n.get('carb'): parts.append(f'碳水{n["carb"]}g')
        if n.get('protein'): parts.append(f'蛋白{n["protein"]}g')
        if n.get('fat'): parts.append(f'脂肪{n["fat"]}g')
        if parts: lines.append(f'\n📊 {" | ".join(parts)}')
        if meal.get('note'): lines.append(f'\n💬 {meal["note"]}')
        reply(reply_token, '\n'.join(lines))

# ── 主處理 ───────────────────────────────────────────────
def handle_text(uid, text, reply_token):
    s = get_state(uid)

    # 圖文選單觸發
    if text == '__記錄身體數據__':
        start_body(uid, reply_token); return
    if text == '__記錄餐食__':
        start_meal(uid, reply_token); return
    if text in ['__今日總覽__', '今日總覽', '今日報告', '查看']:
        reply(reply_token, build_summary(uid)); return

    # 取消
    if text in ['取消', '重來', 'cancel']:
        clear_state(uid)
        reply(reply_token, '已取消目前操作\n請使用底部選單繼續'); return

    # 說明
    if text in ['說明', 'help']:
        reply(reply_token,
            '🌿 飲控日記使用說明\n\n'
            '請使用底部選單按鈕：\n\n'
            '🏥 記錄身體數據\n  體重 → 飲水 → 睡眠 → 排便\n\n'
            '🍱 記錄餐食\n  選擇餐別 → 餐食名稱 → 食材克數\n  → 營養素 → 餐後心得\n\n'
            '📋 今日總覽\n  查看今天所有記錄\n\n'
            '💡 每步驟都可輸入「跳過」略過\n'
            '💡 輸入「取消」中斷目前操作'); return

    # 進行中流程
    if s['step'] and s['step'].startswith('body_'):
        handle_body(uid, text, reply_token); return
    if s['step'] and s['step'].startswith('meal_'):
        handle_meal(uid, text, reply_token); return

    # 無流程預設
    reply(reply_token,
        '請使用底部選單開始 👇\n\n'
        '輸入「說明」查看使用方法\n'
        '輸入「今日總覽」查看今天記錄')

# ── Webhook ───────────────────────────────────────────────
@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

import os
import re
import json
from datetime import datetime
from flask import Flask, request, abort
import hashlib
import hmac
import base64
import urllib.request

app = Flask(__name__)

TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')

user_state = {}

def today():
    return datetime.now().strftime('%Y-%m-%d')

def now_time():
    return datetime.now().strftime('%H:%M')

# ── 儲存 ─────────────────────────────────────────────────
DATA_DIR = '/tmp/diet_data'
os.makedirs(DATA_DIR, exist_ok=True)

def load(uid):
    path = f'{DATA_DIR}/{uid}.json'
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save(uid, data):
    path = f'{DATA_DIR}/{uid}.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_day(uid, date):
    data = load(uid)
    if date not in data:
        data[date] = {'meals': [], 'weight': None, 'water': 0, 'sleep': None, 'poop': None, 'notes': []}
    return data, data[date]

def save_field(uid, date, key, val):
    data = load(uid)
    if date not in data:
        data[date] = {'meals': [], 'weight': None, 'water': 0, 'sleep': None, 'poop': None, 'notes': []}
    data[date][key] = val
    save(uid, data)

def add_meal(uid, date, meal):
    data = load(uid)
    if date not in data:
        data[date] = {'meals': [], 'weight': None, 'water': 0, 'sleep': None, 'poop': None, 'notes': []}
    data[date]['meals'].append(meal)
    save(uid, data)

def add_water(uid, date, ml):
    data = load(uid)
    if date not in data:
        data[date] = {'meals': [], 'weight': None, 'water': 0, 'sleep': None, 'poop': None, 'notes': []}
    data[date]['water'] = data[date].get('water', 0) + ml
    save(uid, data)
    return data[date]['water']

# ── 回覆 LINE ────────────────────────────────────────────
def reply_message(reply_token, text, quick_replies=None):
    msg = {'type': 'text', 'text': text}
    if quick_replies:
        msg['quickReply'] = {
            'items': [
                {'type': 'action', 'action': {'type': 'message', 'label': label, 'text': text_val}}
                for label, text_val in quick_replies
            ]
        }
    body = json.dumps({
        'replyToken': reply_token,
        'messages': [msg]
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.line.me/v2/bot/message/reply',
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {TOKEN}'
        },
        method='POST'
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f'Reply error: {e}')

# ── 解析 ─────────────────────────────────────────────────
def parse_weight(text):
    m = re.search(r'(\d{2,3}(?:\.\d{1,2})?)', text)
    return float(m.group(1)) if m else None

def parse_water(text):
    m = re.search(r'(\d+(?:\.\d+)?)\s*(ml|cc|毫升|升|杯)', text, re.I)
    if not m:
        return None
    val = float(m.group(1))
    u = m.group(2).lower()
    if u == '杯': val *= 250
    elif u == '升': val *= 1000
    return int(val)

def parse_sleep(text):
    m = re.search(r'(\d+(?:\.\d+)?)\s*小時', text)
    if m:
        return {'hours': float(m.group(1))}
    m = re.search(r'(\d{1,2})[：:點](\d{2})?\s*[到至~\-]\s*(\d{1,2})[：:點](\d{2})?', text)
    if m:
        bh, bm = int(m.group(1)), int(m.group(2) or 0)
        wh, wm = int(m.group(3)), int(m.group(4) or 0)
        mins = (wh*60+wm) - (bh*60+bm)
        if mins < 0: mins += 1440
        return {'hours': round(mins/60, 1),
                'bed': '%02d:%02d' % (bh, bm),
                'wake': '%02d:%02d' % (wh, wm)}
    return None

def parse_poop(text):
    if '便秘' in text: return '便秘 ⚠️'
    if '拉肚子' in text or '稀' in text: return '稀軟 ⚠️'
    if '硬' in text: return '偏硬'
    if '順暢' in text or '正常' in text: return '順暢 ✓'
    if '軟' in text: return '偏軟'
    return '有記錄 ✓'

def parse_ingredients(text):
    out = []
    for m in re.finditer(r'([^\d\s，,、\n]+?)\s*(\d+(?:\.\d+)?)\s*(g|kg|ml|cc|克|毫升|公克)?', text):
        name = m.group(1).strip('、，, ')
        if name and m.group(2):
            out.append({'name': name, 'amount': float(m.group(2)), 'unit': m.group(3) or 'g'})
    return out

def parse_nutrition(text):
    nut = {}
    patterns = {
        'kcal': r'(?:熱量|卡路里|卡|kcal)\s*[:：]?\s*(\d+(?:\.\d+)?)',
        'carb': r'(?:碳水化合物|碳水|醣類)\s*[:：]?\s*(\d+(?:\.\d+)?)',
        'protein': r'(?:蛋白質|蛋白)\s*[:：]?\s*(\d+(?:\.\d+)?)',
        'fat': r'(?:脂肪)\s*[:：]?\s*(\d+(?:\.\d+)?)',
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.I)
        if m:
            nut[key] = float(m.group(1))
    return nut

# ── 狀態 ─────────────────────────────────────────────────
def get_state(uid):
    if uid not in user_state:
        user_state[uid] = {'step': None, 'data': {}}
    return user_state[uid]

def clear_state(uid):
    user_state[uid] = {'step': None, 'data': {}}

# ── 今日總覽 ─────────────────────────────────────────────
def build_summary(uid):
    data, day = get_day(uid, today())
    lines = ['📋 %s 飲控日誌' % today(), '─'*16]
    if day.get('weight'): lines.append('⚖️ 體重：%s kg' % day['weight'])
    if day.get('water'):
        pct = min(100, round(day['water']/2000*100))
        lines.append('💧 飲水：%s ml（目標 %s%%）' % (day['water'], pct))
    if day.get('sleep'):
        s = day['sleep']
        if s.get('bed'):
            lines.append('🌙 睡眠：%s～%s（%sh）' % (s['bed'], s['wake'], s['hours']))
        else:
            lines.append('🌙 睡眠：%s 小時' % s['hours'])
    if day.get('poop'): lines.append('🚽 排便：%s' % day['poop'])
    meals = day.get('meals', [])
    if meals:
        lines.append('\n🍱 餐食（%d 餐）' % len(meals))
        for meal in meals:
            lines.append('【%s】%s　%s' % (meal.get('type',''), meal.get('name',''), meal.get('time','')))
            for ing in meal.get('ingredients', []):
                lines.append('  · %s %s%s' % (ing['name'], ing['amount'], ing['unit']))
            if meal.get('oil'): lines.append('  · 用油 %s' % meal['oil'])
            n = meal.get('nutrition', {})
            parts = []
            if n.get('kcal'): parts.append('%skcal' % n['kcal'])
            if n.get('carb'): parts.append('碳水%sg' % n['carb'])
            if n.get('protein'): parts.append('蛋白%sg' % n['protein'])
            if n.get('fat'): parts.append('脂肪%sg' % n['fat'])
            if parts: lines.append('  📊 %s' % ' | '.join(parts))
            if meal.get('note'): lines.append('  💬 %s' % meal['note'])
    if len(lines) == 2:
        lines.append('今天還沒有記錄，請使用底部選單開始！')
    return '\n'.join(lines)

# ── 身體數據流程 ─────────────────────────────────────────
def start_body(uid, reply_token):
    s = get_state(uid)
    s['step'] = 'body_weight'
    s['data'] = {}
    reply_message(reply_token, '⚖️ 記錄身體數據\n\n第 1/4：請輸入今日體重\n例如：65.5\n\n（輸入「跳過」略過此項）')

def handle_body(uid, text, reply_token):
    s = get_state(uid)
    step = s['step']
    d = today()

    if step == 'body_weight':
        if text != '跳過':
            val = parse_weight(text)
            if not val:
                reply_message(reply_token, '請輸入有效數字（例如：65.5），或輸入「跳過」')
                return
            save_field(uid, d, 'weight', val)
            s['data']['weight'] = val
        s['step'] = 'body_water'
        reply_message(reply_token, '💧 第 2/4：請輸入今日飲水量\n例如：1500ml、6杯\n\n（輸入「跳過」略過此項）')

    elif step == 'body_water':
        if text != '跳過':
            ml = parse_water(text)
            if not ml:
                reply_message(reply_token, '請輸入有效格式（例如：1500ml、6杯），或輸入「跳過」')
                return
            total = add_water(uid, d, ml)
            s['data']['water'] = total
        s['step'] = 'body_sleep'
        reply_message(reply_token, '🌙 第 3/4：請輸入睡眠資訊\n例如：睡了7.5小時\n例如：23:00到6:30\n\n（輸入「跳過」略過此項）')

    elif step == 'body_sleep':
        if text != '跳過':
            info = parse_sleep(text)
            if not info:
                reply_message(reply_token, '請輸入有效格式（例如：睡了7小時），或輸入「跳過」')
                return
            save_field(uid, d, 'sleep', info)
            s['data']['sleep'] = info
        s['step'] = 'body_poop'
        reply_message(reply_token, '🚽 第 4/4：請輸入排便狀況\n可輸入：順暢、正常、偏硬、偏軟、便秘、拉肚子\n\n（輸入「跳過」略過此項）')

    elif step == 'body_poop':
        if text != '跳過':
            status = parse_poop(text)
            save_field(uid, d, 'poop', status)
            s['data']['poop'] = status
        dd = s['data'].copy()
        clear_state(uid)
        lines = ['✅ 身體數據記錄完成！\n']
        if dd.get('weight'): lines.append('⚖️ 體重：%s kg' % dd['weight'])
        if dd.get('water'): lines.append('💧 飲水：%s ml' % dd['water'])
        if dd.get('sleep'):
            sv = dd['sleep']
            if sv.get('bed'):
                lines.append('🌙 睡眠：%s～%s（%sh）' % (sv['bed'], sv['wake'], sv['hours']))
            else:
                lines.append('🌙 睡眠：%sh' % sv['hours'])
        if dd.get('poop'): lines.append('🚽 排便：%s' % dd['poop'])
        reply_message(reply_token, '\n'.join(lines))

# ── 餐食流程 ─────────────────────────────────────────────
def start_meal(uid, reply_token):
    s = get_state(uid)
    s['step'] = 'meal_type'
    s['data'] = {}
    reply_message(reply_token, '🍱 記錄餐食\n請選擇餐別 👇',
        quick_replies=[
            ('早餐', '早餐'), ('午餐', '午餐'), ('晚餐', '晚餐'),
            ('下午茶', '下午茶'), ('宵夜', '宵夜'), ('點心', '點心')
        ])

def handle_meal(uid, text, reply_token):
    s = get_state(uid)
    step = s['step']

    if step == 'meal_type':
        s['data']['type'] = text
        s['data']['time'] = now_time()
        s['step'] = 'meal_name'
        reply_message(reply_token, '【%s】\n\n第 1/4：請輸入餐食名稱\n例如：雞胸肉便當、水煮餐' % text)

    elif step == 'meal_name':
        s['data']['name'] = text
        s['step'] = 'meal_ingredients'
        reply_message(reply_token,
            '第 2/4：請輸入食材與克數\n\n每行一項，例如：\n雞胸肉 150g\n白米飯 100g\n花椰菜 80g\n食用油 5g\n\n（輸入「跳過」略過）')

    elif step == 'meal_ingredients':
        if text != '跳過':
            ings = parse_ingredients(text)
            oil_m = re.search(r'(?:食用)?油\s*(\d+(?:\.\d+)?)\s*(g|克|ml)?', text)
            s['data']['ingredients'] = ings
            s['data']['oil'] = '%s%s' % (oil_m.group(1), oil_m.group(2) or 'g') if oil_m else None
        else:
            s['data']['ingredients'] = []
            s['data']['oil'] = None
        s['step'] = 'meal_nutrition'
        reply_message(reply_token,
            '第 3/4：請輸入營養資訊\n\n每行一項，例如：\n卡路里 500\n碳水 60\n蛋白質 30\n脂肪 10\n\n（輸入「跳過」略過）')

    elif step == 'meal_nutrition':
        if text != '跳過':
            s['data']['nutrition'] = parse_nutrition(text)
        else:
            s['data']['nutrition'] = {}
        s['step'] = 'meal_note'
        reply_message(reply_token, '第 4/4：餐後心得（選填）\n例如：飽足感不錯\n\n（輸入「跳過」完成記錄）')

    elif step == 'meal_note':
        if text != '跳過':
            s['data']['note'] = text
        meal = s['data'].copy()
        add_meal(uid, today(), meal)
        clear_state(uid)
        lines = ['✅ %s記錄完成！（%s）\n' % (meal.get('type',''), meal.get('time',''))]
        lines.append('🍽 %s' % meal.get('name', ''))
        for ing in meal.get('ingredients', []):
            lines.append('  · %s %s%s' % (ing['name'], ing['amount'], ing['unit']))
        if meal.get('oil'): lines.append('  · 用油 %s' % meal['oil'])
        n = meal.get('nutrition', {})
        parts = []
        if n.get('kcal'): parts.append('%skcal' % n['kcal'])
        if n.get('carb'): parts.append('碳水%sg' % n['carb'])
        if n.get('protein'): parts.append('蛋白%sg' % n['protein'])
        if n.get('fat'): parts.append('脂肪%sg' % n['fat'])
        if parts: lines.append('\n📊 %s' % ' | '.join(parts))
        if meal.get('note'): lines.append('\n💬 %s' % meal['note'])
        reply_message(reply_token, '\n'.join(lines))

# ── 主處理 ───────────────────────────────────────────────
def handle_text(uid, text, reply_token):
    s = get_state(uid)

    if text == '__記錄身體數據__':
        start_body(uid, reply_token); return
    if text == '__記錄餐食__':
        start_meal(uid, reply_token); return
    if text in ['__今日總覽__', '今日總覽', '今日報告']:
        reply_message(reply_token, build_summary(uid)); return
    if text in ['取消', '重來']:
        clear_state(uid)
        reply_message(reply_token, '已取消，請重新選擇功能'); return
    if text in ['說明', 'help']:
        reply_message(reply_token,
            '🌿 飲控日記\n\n'
            '請使用底部選單按鈕：\n'
            '🏥 記錄身體數據\n'
            '🍱 記錄餐食\n'
            '📋 今日總覽\n\n'
            '每步驟可輸入「跳過」略過\n'
            '輸入「取消」中斷操作'); return

    # 餐別快速回覆
    if text in ['早餐', '午餐', '晚餐', '下午茶', '宵夜', '點心'] and s.get('step') == 'meal_type':
        handle_meal(uid, text, reply_token); return

    # 進行中流程
    if s.get('step') and s['step'].startswith('body_'):
        handle_body(uid, text, reply_token); return
    if s.get('step') and s['step'].startswith('meal_'):
        handle_meal(uid, text, reply_token); return

    reply_message(reply_token, '請使用底部選單開始記錄 👇\n\n輸入「說明」查看使用方法')

# ── Webhook ───────────────────────────────────────────────
@app.route('/callback', methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    # 驗證簽名
    hash_val = hmac.new(SECRET.encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
    expected = base64.b64encode(hash_val).decode('utf-8')
    if signature != expected:
        abort(400)

    data = json.loads(body)
    for event in data.get('events', []):
        if event.get('type') == 'message' and event['message'].get('type') == 'text':
            uid = event['source']['userId']
            text = event['message']['text']
            reply_token = event['replyToken']
            try:
                handle_text(uid, text, reply_token)
            except Exception as e:
                print('Error handling message: %s' % e)
    return 'OK'

@app.route('/', methods=['GET'])
def index():
    return '飲控日記 Bot is running!'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

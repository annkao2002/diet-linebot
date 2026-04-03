import os
import re
import json
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
from datetime import datetime
from flask import Flask, request, abort

app = Flask(__name__)

TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
SHEET_WEBHOOK = os.environ.get('GOOGLE_SHEET_WEBHOOK', '')  # Apps Script URL

user_state = {}

def today():
    return datetime.now().strftime('%Y-%m-%d')

def now_time():
    return datetime.now().strftime('%H:%M')

# ── 儲存（每個欄位獨立更新，不會互相覆蓋） ──────────────
DATA_DIR = '/tmp/diet_data'
os.makedirs(DATA_DIR, exist_ok=True)

def _path(uid):
    return '%s/%s.json' % (DATA_DIR, uid.replace('/', '_'))

def _load(uid):
    p = _path(uid)
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _save(uid, data):
    with open(_path(uid), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _ensure_day(data, date):
    if date not in data:
        data[date] = {
            'meals': [], 'weight': None,
            'water': 0, 'sleep': None,
            'poop': None, 'notes': []
        }
    return data

def get_day(uid):
    data = _load(uid)
    data = _ensure_day(data, today())
    return data[today()]

def set_weight(uid, val):
    data = _load(uid)
    data = _ensure_day(data, today())
    data[today()]['weight'] = val
    _save(uid, data)

def set_sleep(uid, val):
    data = _load(uid)
    data = _ensure_day(data, today())
    data[today()]['sleep'] = val
    _save(uid, data)

def set_poop(uid, val):
    data = _load(uid)
    data = _ensure_day(data, today())
    data[today()]['poop'] = val
    _save(uid, data)

def add_water(uid, ml):
    data = _load(uid)
    data = _ensure_day(data, today())
    data[today()]['water'] = data[today()].get('water', 0) + ml
    _save(uid, data)
    return data[today()]['water']

def add_meal(uid, meal):
    data = _load(uid)
    data = _ensure_day(data, today())
    data[today()]['meals'].append(meal)
    _save(uid, data)

def add_note(uid, text):
    data = _load(uid)
    data = _ensure_day(data, today())
    data[today()]['notes'].append({'text': text, 'time': now_time()})
    _save(uid, data)

def get_all_data(uid):
    return _load(uid)

# ── LINE 回覆 ────────────────────────────────────────────
def reply_message(reply_token, text, quick_replies=None):
    msg = {'type': 'text', 'text': str(text)}
    if quick_replies:
        msg['quickReply'] = {
            'items': [
                {'type': 'action', 'action': {
                    'type': 'message', 'label': label, 'text': text_val
                }} for label, text_val in quick_replies
            ]
        }
    body = json.dumps({'replyToken': reply_token, 'messages': [msg]}).encode('utf-8')
    req = urllib.request.Request(
        'https://api.line.me/v2/bot/message/reply',
        data=body,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN},
        method='POST'
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print('Reply error: %s' % e)

# ── 解析 ─────────────────────────────────────────────────
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
                'bed': '%02d:%02d' % (bh, bm),
                'wake': '%02d:%02d' % (wh, wm)}
    return None

def parse_poop(text):
    if '便秘' in text: return '便秘'
    if '拉肚子' in text or '稀' in text: return '稀軟'
    if '硬' in text: return '偏硬'
    if '順暢' in text or '正常' in text: return '順暢'
    if '軟' in text: return '偏軟'
    return '有記錄'

def parse_ingredients(text):
    out = []
    for m in re.finditer(r'([^\d\s，,、\n]+?)\s*(\d+(?:\.\d+)?)\s*(g|kg|ml|cc|克|毫升|公克)?', text):
        name = m.group(1).strip('、，, \t')
        if name and m.group(2):
            out.append({'name': name, 'amount': float(m.group(2)), 'unit': m.group(3) or 'g'})
    return out

def parse_nutrition(text):
    nut = {}
    for key, pat in [
        ('kcal', r'(?:熱量|卡路里|卡|kcal)\s*[:：]?\s*(\d+(?:\.\d+)?)'),
        ('carb', r'(?:碳水化合物|碳水|醣類)\s*[:：]?\s*(\d+(?:\.\d+)?)'),
        ('protein', r'(?:蛋白質|蛋白)\s*[:：]?\s*(\d+(?:\.\d+)?)'),
        ('fat', r'(?:脂肪)\s*[:：]?\s*(\d+(?:\.\d+)?)'),
    ]:
        m = re.search(pat, text, re.I)
        if m: nut[key] = float(m.group(1))
    return nut

# ── 狀態管理 ─────────────────────────────────────────────
def get_state(uid):
    if uid not in user_state:
        user_state[uid] = {'step': None, 'data': {}}
    return user_state[uid]

def clear_state(uid):
    user_state[uid] = {'step': None, 'data': {}}

# ── 今日總覽 ─────────────────────────────────────────────
def build_summary(uid):
    day = get_day(uid)
    lines = ['📋 %s 飲控日誌' % today(), '─' * 16]
    if day.get('weight'):
        lines.append('⚖️ 體重：%s kg' % day['weight'])
    if day.get('water'):
        pct = min(100, round(day['water'] / 2000 * 100))
        lines.append('💧 飲水：%s ml（目標 %s%%）' % (day['water'], pct))
    if day.get('sleep'):
        s = day['sleep']
        if s.get('bed'):
            lines.append('🌙 睡眠：%s～%s（%sh）' % (s['bed'], s['wake'], s['hours']))
        else:
            lines.append('🌙 睡眠：%s 小時' % s['hours'])
    if day.get('poop'):
        lines.append('🚽 排便：%s' % day['poop'])
    meals = day.get('meals', [])
    if meals:
        lines.append('\n🍱 餐食（%d 餐）' % len(meals))
        for meal in meals:
            lines.append('【%s】%s　%s' % (
                meal.get('type', ''), meal.get('name', ''), meal.get('time', '')))
            for ing in meal.get('ingredients', []):
                lines.append('  · %s %s%s' % (ing['name'], ing['amount'], ing['unit']))
            if meal.get('oil'):
                lines.append('  · 用油 %s' % meal['oil'])
            n = meal.get('nutrition', {})
            parts = []
            if n.get('kcal'): parts.append('%skcal' % n['kcal'])
            if n.get('carb'): parts.append('碳水%sg' % n['carb'])
            if n.get('protein'): parts.append('蛋白%sg' % n['protein'])
            if n.get('fat'): parts.append('脂肪%sg' % n['fat'])
            if parts: lines.append('  📊 %s' % ' | '.join(parts))
            if meal.get('note'): lines.append('  💬 %s' % meal['note'])
    if day.get('notes'):
        lines.append('\n📝 備註')
        for note in day['notes']:
            lines.append('  「%s」' % note['text'])
    if len(lines) == 2:
        lines.append('今天還沒有記錄，請使用底部選單開始！')
    return '\n'.join(lines)

# ── Google Sheets 匯出 ───────────────────────────────────
def export_to_sheets(uid, reply_token):
    if not SHEET_WEBHOOK:
        reply_message(reply_token,
            '⚠️ 尚未設定 Google Sheets\n\n'
            '請依照說明設定後，在 Render 環境變數加入：\n'
            'GOOGLE_SHEET_WEBHOOK = 你的 Apps Script 網址')
        return

    all_data = get_all_data(uid)
    if not all_data:
        reply_message(reply_token, '目前沒有任何資料可以匯出')
        return

    # 整理成列表格式傳給 Google Apps Script
    rows = []
    for date in sorted(all_data.keys()):
        day = all_data[date]
        # 身體數據（每天一行）
        sleep_text = ''
        if day.get('sleep'):
            s = day['sleep']
            if s.get('bed'):
                sleep_text = '%s～%s（%sh）' % (s['bed'], s['wake'], s['hours'])
            else:
                sleep_text = '%sh' % s['hours']

        base_row = {
            'date': date,
            'weight': day.get('weight') or '',
            'water': day.get('water') or '',
            'sleep': sleep_text,
            'poop': day.get('poop') or '',
        }

        meals = day.get('meals', [])
        if meals:
            for meal in meals:
                n = meal.get('nutrition', {})
                ings = '、'.join(['%s %s%s' % (i['name'], i['amount'], i['unit'])
                                  for i in meal.get('ingredients', [])])
                if meal.get('oil'):
                    ings += ('、' if ings else '') + '油 ' + meal['oil']
                row = dict(base_row)
                row.update({
                    'meal_type': meal.get('type', ''),
                    'meal_name': meal.get('name', ''),
                    'meal_time': meal.get('time', ''),
                    'ingredients': ings,
                    'kcal': n.get('kcal', ''),
                    'carb': n.get('carb', ''),
                    'protein': n.get('protein', ''),
                    'fat': n.get('fat', ''),
                    'note': meal.get('note', ''),
                })
                rows.append(row)
        else:
            base_row.update({
                'meal_type': '', 'meal_name': '', 'meal_time': '',
                'ingredients': '', 'kcal': '', 'carb': '',
                'protein': '', 'fat': '', 'note': ''
            })
            rows.append(base_row)

    # 送到 Google Apps Script
    payload = json.dumps({'rows': rows}).encode('utf-8')
    req = urllib.request.Request(
        SHEET_WEBHOOK,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        reply_message(reply_token,
            '✅ 已成功匯出到 Google Sheets！\n\n'
            '共匯出 %d 天、%d 筆餐食記錄\n'
            '請到你的 Google Sheet 查看' % (
                len(all_data),
                sum(len(d.get('meals', [])) for d in all_data.values())
            ))
    except Exception as e:
        reply_message(reply_token, '❌ 匯出失敗：%s' % str(e))

# ── 身體數據流程 ─────────────────────────────────────────
def start_body(uid, reply_token):
    s = get_state(uid)
    s['step'] = 'body_weight'
    s['data'] = {}
    reply_message(reply_token,
        '⚖️ 記錄身體數據\n\n'
        '第 1/4：請輸入今日體重\n'
        '例如：65.5\n\n'
        '（輸入「跳過」略過此項）')

def handle_body(uid, text, reply_token):
    s = get_state(uid)
    step = s['step']

    if step == 'body_weight':
        if text != '跳過':
            val = parse_weight(text)
            if not val:
                reply_message(reply_token, '請輸入有效數字（例如：65.5），或輸入「跳過」')
                return
            set_weight(uid, val)
            s['data']['weight'] = val
        s['step'] = 'body_water'
        reply_message(reply_token,
            '💧 第 2/4：請輸入今日飲水量\n'
            '例如：1500ml、6杯\n\n'
            '（輸入「跳過」略過此項）')

    elif step == 'body_water':
        if text != '跳過':
            ml = parse_water(text)
            if not ml:
                reply_message(reply_token, '請輸入有效格式（例如：1500ml、6杯），或輸入「跳過」')
                return
            total = add_water(uid, ml)
            s['data']['water'] = total
        s['step'] = 'body_sleep'
        reply_message(reply_token,
            '🌙 第 3/4：請輸入睡眠資訊\n'
            '例如：睡了7.5小時\n'
            '例如：23:00到6:30\n\n'
            '（輸入「跳過」略過此項）')

    elif step == 'body_sleep':
        if text != '跳過':
            info = parse_sleep(text)
            if not info:
                reply_message(reply_token, '請輸入有效格式（例如：睡了7小時），或輸入「跳過」')
                return
            set_sleep(uid, info)
            s['data']['sleep'] = info
        s['step'] = 'body_poop'
        reply_message(reply_token,
            '🚽 第 4/4：請輸入排便狀況\n'
            '可輸入：順暢、正常、偏硬、偏軟、便秘、拉肚子\n\n'
            '（輸入「跳過」略過此項）')

    elif step == 'body_poop':
        if text != '跳過':
            status = parse_poop(text)
            set_poop(uid, status)
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
        reply_message(reply_token,
            '【%s】\n\n第 1/4：請輸入餐食名稱\n例如：雞胸肉便當、水煮餐' % text)

    elif step == 'meal_name':
        s['data']['name'] = text
        s['step'] = 'meal_ingredients'
        reply_message(reply_token,
            '第 2/4：請輸入食材與克數\n\n'
            '每行一項，例如：\n'
            '雞胸肉 150g\n白米飯 100g\n花椰菜 80g\n食用油 5g\n\n'
            '（輸入「跳過」略過）')

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
            '第 3/4：請輸入營養資訊\n\n'
            '每行一項，例如：\n'
            '卡路里 500\n碳水 60\n蛋白質 30\n脂肪 10\n\n'
            '（輸入「跳過」略過）')

    elif step == 'meal_nutrition':
        s['data']['nutrition'] = parse_nutrition(text) if text != '跳過' else {}
        s['step'] = 'meal_note'
        reply_message(reply_token,
            '第 4/4：餐後心得（選填）\n'
            '例如：飽足感不錯\n\n'
            '（輸入「跳過」完成記錄）')

    elif step == 'meal_note':
        if text != '跳過':
            s['data']['note'] = text
        meal = s['data'].copy()
        clear_state(uid)
        add_meal(uid, meal)
        lines = ['✅ %s記錄完成！（%s）\n' % (meal.get('type', ''), meal.get('time', ''))]
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
    if text in ['__匯出__', '匯出', '匯出到Google Sheets', '匯出資料']:
        export_to_sheets(uid, reply_token); return
    if text in ['取消', '重來']:
        clear_state(uid)
        reply_message(reply_token, '已取消，請重新選擇功能'); return
    if text in ['說明', 'help']:
        reply_message(reply_token,
            '🌿 飲控日記使用說明\n\n'
            '底部選單按鈕：\n'
            '🏥 記錄身體數據\n'
            '🍱 記錄餐食\n'
            '📋 今日總覽\n'
            '📤 匯出 Google Sheets\n\n'
            '每步驟可輸入「跳過」略過\n'
            '輸入「取消」中斷操作'); return

    # 餐別快速回覆
    if text in ['早餐', '午餐', '晚餐', '下午茶', '宵夜', '點心'] and s.get('step') == 'meal_type':
        handle_meal(uid, text, reply_token); return

    # 進行中的流程
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
                print('Error: %s' % e)
    return 'OK'

@app.route('/', methods=['GET'])
def index():
    return '飲控日記 Bot is running!'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

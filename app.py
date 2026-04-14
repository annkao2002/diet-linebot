import os
import re
import json
import hashlib
import hmac
import base64
import urllib.request
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort

app = Flask(__name__)

TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
SHEET_WEBHOOK = os.environ.get('GOOGLE_SHEET_WEBHOOK', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')

user_state = {}

# ── 台灣時間 UTC+8 ───────────────────────────────────────
TW = timezone(timedelta(hours=8))

def now_tw():
    return datetime.now(TW)

def today():
    return now_tw().strftime('%Y-%m-%d')

def now_time():
    return now_tw().strftime('%H:%M')

# ── Supabase API ─────────────────────────────────────────
def sb_request(method, path, data=None, params=None):
    url = SUPABASE_URL + '/rest/v1/' + path
    if params:
        url += '?' + urllib.parse.urlencode(params) if params else ''
    body = json.dumps(data).encode('utf-8') if data else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + SUPABASE_KEY,
            'apikey': SUPABASE_KEY,
            'Prefer': 'return=representation'
        }
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('Supabase error %s: %s' % (e.code, e.read().decode()))
        return None
    except Exception as e:
        print('Supabase error: %s' % e)
        return None

import urllib.parse

def sb_get(path, **params):
    url = SUPABASE_URL + '/rest/v1/' + path
    if params:
        url += '?' + '&'.join('%s=%s' % (k, urllib.parse.quote(str(v))) for k, v in params.items())
    req = urllib.request.Request(url, method='GET', headers={
        'Authorization': 'Bearer ' + SUPABASE_KEY,
        'apikey': SUPABASE_KEY,
    })
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        print('Supabase GET error: %s' % e)
        return []

def sb_upsert(path, data):
    url = SUPABASE_URL + '/rest/v1/' + path
    req = urllib.request.Request(
        url, data=json.dumps(data).encode('utf-8'), method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + SUPABASE_KEY,
            'apikey': SUPABASE_KEY,
            'Prefer': 'resolution=merge-duplicates,return=representation'
        }
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('Supabase upsert error %s: %s' % (e.code, e.read().decode()))
        return None
    except Exception as e:
        print('Supabase upsert error: %s' % e)
        return None

def sb_insert(path, data):
    url = SUPABASE_URL + '/rest/v1/' + path
    req = urllib.request.Request(
        url, data=json.dumps(data).encode('utf-8'), method='POST',
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + SUPABASE_KEY,
            'apikey': SUPABASE_KEY,
            'Prefer': 'return=representation'
        }
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('Supabase insert error %s: %s' % (e.code, e.read().decode()))
        return None
    except Exception as e:
        print('Supabase insert error: %s' % e)
        return None

# ── 資料存取 ─────────────────────────────────────────────
def get_diet_record(uid, date):
    rows = sb_get('diet_records',
                  **{'user_id': 'eq.' + uid, 'date': 'eq.' + date})
    return rows[0] if rows else None

def upsert_diet_record(uid, date, fields):
    fields['user_id'] = uid
    fields['date'] = date
    fields['updated_at'] = now_tw().isoformat()
    return sb_upsert('diet_records', fields)

def get_meals(uid, date):
    return sb_get('meal_records',
                  **{'user_id': 'eq.' + uid, 'date': 'eq.' + date,
                     'order': 'created_at.asc'})

def insert_meal(uid, date, meal):
    ings = meal.get('ingredients', [])
    return sb_insert('meal_records', {
        'user_id': uid,
        'date': date,
        'meal_type': meal.get('type', ''),
        'meal_name': meal.get('name', ''),
        'meal_time': meal.get('time', ''),
        'ingredients': json.dumps(ings, ensure_ascii=False),
        'oil': meal.get('oil', ''),
        'kcal': meal.get('nutrition', {}).get('kcal') or None,
        'carb': meal.get('nutrition', {}).get('carb') or None,
        'protein': meal.get('nutrition', {}).get('protein') or None,
        'fat': meal.get('nutrition', {}).get('fat') or None,
        'note': meal.get('note', ''),
    })

def get_all_dates(uid):
    rows = sb_get('diet_records', **{'user_id': 'eq.' + uid, 'order': 'date.asc', 'select': 'date'})
    meal_rows = sb_get('meal_records', **{'user_id': 'eq.' + uid, 'order': 'date.asc', 'select': 'date'})
    dates = set(r['date'] for r in rows) | set(r['date'] for r in meal_rows)
    return sorted(dates)

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
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + TOKEN},
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
    if m: return {'hours': float(m.group(1)), 'bed': None, 'wake': None}
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
        mm = re.search(pat, text, re.I)
        if mm: nut[key] = float(mm.group(1))
    return nut

def parse_date(text):
    if text in ['今天', '今日']: return today()
    if text in ['昨天', '昨日']:
        return (now_tw() - timedelta(days=1)).strftime('%Y-%m-%d')
    if text in ['前天']:
        return (now_tw() - timedelta(days=2)).strftime('%Y-%m-%d')
    m = re.match(r'(\d+)\s*天前', text)
    if m:
        return (now_tw() - timedelta(days=int(m.group(1)))).strftime('%Y-%m-%d')
    m = re.match(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', text)
    if m:
        return '%s-%02d-%02d' % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.match(r'(\d{1,2})[/-](\d{1,2})$', text)
    if m:
        return '%d-%02d-%02d' % (now_tw().year, int(m.group(1)), int(m.group(2)))
    return None

# ── 餐別 ─────────────────────────────────────────────────
MEAL_TYPES = ['早餐', '午餐', '晚餐', '運動前', '運動後', '下午茶', '宵夜', '點心']
MEAL_QR = [(m, m) for m in MEAL_TYPES]

# ── 狀態 ─────────────────────────────────────────────────
def get_state(uid):
    if uid not in user_state:
        user_state[uid] = {'step': None, 'data': {}}
    return user_state[uid]

def clear_state(uid):
    user_state[uid] = {'step': None, 'data': {}}

# ── 今日總覽（含營養加總）───────────────────────────────
def build_summary(uid, date=None):
    date = date or today()
    dt = datetime.strptime(date, '%Y-%m-%d')
    weekdays = ['一','二','三','四','五','六','日']
    date_label = '%s（週%s）' % (date, weekdays[dt.weekday()])

    rec = get_diet_record(uid, date)
    meals = get_meals(uid, date)

    lines = ['📋 %s 飲控日誌' % date_label, '─' * 16]

    # 身體數據
    if rec:
        if rec.get('weight'):
            lines.append('⚖️ 體重：%s kg' % rec['weight'])
        if rec.get('water'):
            pct = min(100, round(rec['water'] / 2000 * 100))
            lines.append('💧 飲水：%s ml（目標 %s%%）' % (rec['water'], pct))
        if rec.get('sleep_hours'):
            if rec.get('sleep_bed'):
                lines.append('🌙 睡眠：%s～%s（%sh）' % (
                    rec['sleep_bed'], rec['sleep_wake'], rec['sleep_hours']))
            else:
                lines.append('🌙 睡眠：%s 小時' % rec['sleep_hours'])
        if rec.get('poop'):
            lines.append('🚽 排便：%s' % rec['poop'])

    # 餐食
    if meals:
        lines.append('\n🍱 餐食（%d 餐）' % len(meals))
        total_kcal = total_carb = total_protein = total_fat = 0
        has_nutrition = False

        for meal in meals:
            lines.append('【%s】%s　%s' % (
                meal.get('meal_type', ''),
                meal.get('meal_name', ''),
                meal.get('meal_time', '')))
            # 食材
            ings = meal.get('ingredients', [])
            if isinstance(ings, str):
                try: ings = json.loads(ings)
                except: ings = []
            for ing in ings:
                lines.append('  · %s %s%s' % (ing['name'], ing['amount'], ing['unit']))
            if meal.get('oil'):
                lines.append('  · 用油 %s' % meal['oil'])
            # 營養
            parts = []
            if meal.get('kcal'):
                parts.append('%skcal' % meal['kcal'])
                total_kcal += float(meal['kcal'])
                has_nutrition = True
            if meal.get('carb'):
                parts.append('碳水%sg' % meal['carb'])
                total_carb += float(meal['carb'])
            if meal.get('protein'):
                parts.append('蛋白%sg' % meal['protein'])
                total_protein += float(meal['protein'])
            if meal.get('fat'):
                parts.append('脂肪%sg' % meal['fat'])
                total_fat += float(meal['fat'])
            if parts:
                lines.append('  📊 %s' % ' | '.join(parts))
            if meal.get('note'):
                lines.append('  💬 %s' % meal['note'])

        # 每日營養加總
        if has_nutrition:
            lines.append('\n📊 今日營養加總')
            lines.append('  熱量：%s kcal' % round(total_kcal))
            if total_carb: lines.append('  碳水：%s g' % round(total_carb))
            if total_protein: lines.append('  蛋白質：%s g' % round(total_protein))
            if total_fat: lines.append('  脂肪：%s g' % round(total_fat))

    if len(lines) == 2:
        lines.append('這天還沒有記錄')
    return '\n'.join(lines)

# ── Google Sheets 匯出 ───────────────────────────────────

def get_line_display_name(uid):
    """取得 LINE 用戶顯示名稱"""
    req = urllib.request.Request(
        'https://api.line.me/v2/bot/profile/' + uid,
        headers={'Authorization': 'Bearer ' + TOKEN},
        method='GET'
    )
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read().decode('utf-8'))
            name = data.get('displayName', '')
            # 移除不合法的工作表名稱字元
            import re as _re
            name = _re.sub(r'[\\/*?:\[\]]', '', name).strip()[:20]
            return name if name else uid[-6:]
    except Exception:
        return uid[-6:]

def export_to_sheets(uid, reply_token):
    if not SHEET_WEBHOOK:
        reply_message(reply_token, '⚠️ 尚未設定 GOOGLE_SHEET_WEBHOOK 環境變數')
        return
    dates = get_all_dates(uid)
    if not dates:
        reply_message(reply_token, '目前沒有任何資料可以匯出')
        return
    rows = []
    for date in dates:
        rec = get_diet_record(uid, date) or {}
        meals = get_meals(uid, date)
        sleep_text = ''
        if rec.get('sleep_hours'):
            if rec.get('sleep_bed'):
                sleep_text = '%s～%s（%sh）' % (rec['sleep_bed'], rec['sleep_wake'], rec['sleep_hours'])
            else:
                sleep_text = '%sh' % rec['sleep_hours']
        base = {
            'date': date,
            'weight': rec.get('weight') or '',
            'water': rec.get('water') or '',
            'sleep': sleep_text,
            'poop': rec.get('poop') or '',
        }
        if meals:
            for meal in meals:
                ings = meal.get('ingredients', [])
                if isinstance(ings, str):
                    try: ings = json.loads(ings)
                    except: ings = []
                ings_text = '、'.join(['%s %s%s' % (i['name'], i['amount'], i['unit']) for i in ings])
                if meal.get('oil'):
                    ings_text += ('、' if ings_text else '') + '油 ' + meal['oil']
                row = dict(base)
                row.update({
                    'meal_type': meal.get('meal_type', ''),
                    'meal_name': meal.get('meal_name', ''),
                    'meal_time': meal.get('meal_time', ''),
                    'ingredients': ings_text,
                    'kcal': meal.get('kcal', ''),
                    'carb': meal.get('carb', ''),
                    'protein': meal.get('protein', ''),
                    'fat': meal.get('fat', ''),
                    'note': meal.get('note', ''),
                })
                rows.append(row)
        else:
            base.update({'meal_type':'','meal_name':'','meal_time':'',
                         'ingredients':'','kcal':'','carb':'','protein':'','fat':'','note':''})
            rows.append(base)

    display_name = get_line_display_name(uid)
    payload = json.dumps({
        'rows': rows,
        'uid': uid,
        'display_name': display_name
    }).encode('utf-8')
    req = urllib.request.Request(
        SHEET_WEBHOOK, data=payload,
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        urllib.request.urlopen(req, timeout=15)
        reply_message(reply_token,
            '✅ 已成功匯出到 Google Sheets！\n\n'
            '工作表名稱：%s_飲控記錄\n'
            '共 %d 天、%d 筆餐食記錄' % (display_name, len(dates), len(rows)))
    except Exception as e:
        reply_message(reply_token, '❌ 匯出失敗：%s' % str(e))

# ── 身體數據流程 ─────────────────────────────────────────
def start_body(uid, reply_token, date=None):
    s = get_state(uid)
    s['step'] = 'body_weight'
    s['data'] = {'date': date or today()}
    date_label = '今天' if (date or today()) == today() else (date or today())
    reply_message(reply_token,
        '⚖️ 記錄身體數據（%s）\n\n'
        '第 1/4：請輸入體重\n例如：65.5\n\n'
        '（輸入「跳過」略過）' % date_label)

def handle_body(uid, text, reply_token):
    s = get_state(uid)
    step = s['step']
    date = s['data'].get('date', today())

    if step == 'body_weight':
        if text != '跳過':
            val = parse_weight(text)
            if not val:
                reply_message(reply_token, '請輸入有效數字（例如：65.5），或輸入「跳過」')
                return
            s['data']['weight'] = val
        s['step'] = 'body_water'
        reply_message(reply_token,
            '💧 第 2/4：請輸入飲水量\n例如：1500ml、6杯\n\n（輸入「跳過」略過）')

    elif step == 'body_water':
        if text != '跳過':
            ml = parse_water(text)
            if not ml:
                reply_message(reply_token, '請輸入有效格式（例如：1500ml、6杯），或輸入「跳過」')
                return
            # 累加現有飲水量
            rec = get_diet_record(uid, date)
            existing = rec['water'] if rec and rec.get('water') else 0
            s['data']['water'] = existing + ml
        s['step'] = 'body_sleep'
        reply_message(reply_token,
            '🌙 第 3/4：請輸入睡眠資訊\n'
            '例如：睡了7.5小時\n例如：23:00到6:30\n\n（輸入「跳過」略過）')

    elif step == 'body_sleep':
        if text != '跳過':
            info = parse_sleep(text)
            if not info:
                reply_message(reply_token, '請輸入有效格式（例如：睡了7小時），或輸入「跳過」')
                return
            s['data']['sleep'] = info
        s['step'] = 'body_poop'
        reply_message(reply_token,
            '🚽 第 4/4：請輸入排便狀況\n'
            '可輸入：順暢、正常、偏硬、偏軟、便秘、拉肚子\n\n（輸入「跳過」略過）')

    elif step == 'body_poop':
        if text != '跳過':
            s['data']['poop'] = parse_poop(text)

        dd = s['data'].copy()
        date = dd.pop('date', today())
        sleep = dd.pop('sleep', None)
        clear_state(uid)

        # 組成 upsert 資料
        fields = {}
        if dd.get('weight'): fields['weight'] = dd['weight']
        if dd.get('water'): fields['water'] = dd['water']
        if dd.get('poop'): fields['poop'] = dd['poop']
        if sleep:
            fields['sleep_hours'] = sleep['hours']
            fields['sleep_bed'] = sleep.get('bed')
            fields['sleep_wake'] = sleep.get('wake')

        if fields:
            upsert_diet_record(uid, date, fields)

        lines = ['✅ 身體數據記錄完成！\n']
        if dd.get('weight'): lines.append('⚖️ 體重：%s kg' % dd['weight'])
        if dd.get('water'): lines.append('💧 飲水：%s ml' % dd['water'])
        if sleep:
            if sleep.get('bed'):
                lines.append('🌙 睡眠：%s～%s（%sh）' % (sleep['bed'], sleep['wake'], sleep['hours']))
            else:
                lines.append('🌙 睡眠：%sh' % sleep['hours'])
        if dd.get('poop'): lines.append('🚽 排便：%s' % dd['poop'])
        reply_message(reply_token, '\n'.join(lines))

# ── 餐食流程 ─────────────────────────────────────────────
def start_meal(uid, reply_token, date=None):
    s = get_state(uid)
    s['step'] = 'meal_type'
    s['data'] = {'date': date or today()}
    date_label = '今天' if (date or today()) == today() else (date or today())
    reply_message(reply_token,
        '🍱 記錄餐食（%s）\n請選擇餐別 👇' % date_label,
        quick_replies=MEAL_QR)

def handle_meal(uid, text, reply_token):
    s = get_state(uid)
    step = s['step']

    if step == 'meal_type':
        s['data']['type'] = text
        s['data']['time'] = now_time()
        s['step'] = 'meal_name'
        reply_message(reply_token,
            '【%s】\n\n第 1/4：請輸入餐食名稱\n例如：雞胸肉便當' % text)

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
            '第 4/4：餐後心得（選填）\n例如：飽足感不錯\n\n（輸入「跳過」完成）')

    elif step == 'meal_note':
        if text != '跳過':
            s['data']['note'] = text
        meal = s['data'].copy()
        date = meal.pop('date', today())
        clear_state(uid)
        insert_meal(uid, date, meal)

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

# ── 補記流程 ─────────────────────────────────────────────
def start_backfill(uid, reply_token):
    s = get_state(uid)
    s['step'] = 'backfill_date'
    s['data'] = {}
    reply_message(reply_token,
        '📅 補記過去的資料\n\n請輸入日期：\n\n'
        '· 昨天\n· 前天\n· 3天前\n· 4/1\n· 2026-04-01\n\n'
        '（輸入「取消」返回）')

def handle_backfill(uid, text, reply_token):
    date = parse_date(text)
    if not date:
        reply_message(reply_token, '無法辨識日期，請重新輸入\n例如：昨天、3天前、4/1')
        return
    if date > today():
        reply_message(reply_token, '不能補記未來的日期')
        return
    clear_state(uid)
    reply_message(reply_token, '📅 補記 %s\n\n要補記什麼？' % date,
        quick_replies=[
            ('身體數據', '__補記身體_%s__' % date),
            ('餐食記錄', '__補記餐食_%s__' % date),
            ('查看當天', '__查看_%s__' % date),
        ])

# ── 主處理 ───────────────────────────────────────────────
def handle_text(uid, text, reply_token):
    s = get_state(uid)

    if text == '__記錄身體數據__':
        start_body(uid, reply_token); return
    if text == '__記錄餐食__':
        start_meal(uid, reply_token); return
    if text in ['__今日總覽__', '今日總覽', '今日報告']:
        reply_message(reply_token, build_summary(uid)); return
    if text in ['__補記__', '補記']:
        start_backfill(uid, reply_token); return
    if text in ['__匯出__', '匯出', '匯出資料']:
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
            '📅 補記\n'
            '📤 匯出 Google Sheets\n\n'
            '每步驟可輸入「跳過」略過\n'
            '輸入「取消」中斷操作'); return

    m = re.match(r'__補記身體_(.+)__', text)
    if m: start_body(uid, reply_token, date=m.group(1)); return
    m = re.match(r'__補記餐食_(.+)__', text)
    if m: start_meal(uid, reply_token, date=m.group(1)); return
    m = re.match(r'__查看_(.+)__', text)
    if m:
        reply_message(reply_token, build_summary(uid, date=m.group(1))); return

    if text in MEAL_TYPES and s.get('step') == 'meal_type':
        handle_meal(uid, text, reply_token); return
    if s.get('step') == 'backfill_date':
        handle_backfill(uid, text, reply_token); return
    if s.get('step') and s['step'].startswith('body_'):
        handle_body(uid, text, reply_token); return
    if s.get('step') and s['step'].startswith('meal_'):
        handle_meal(uid, text, reply_token); return

    reply_message(reply_token,
        '請使用底部選單開始記錄 👇\n\n'
        '輸入「說明」查看使用方法')

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

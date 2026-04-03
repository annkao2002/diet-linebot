"""
storage.py - 使用 JSON 檔案儲存資料
資料存在 data/ 目錄，每個用戶一個檔案
"""
import json
import os
from pathlib import Path

DATA_DIR = Path('data')
DATA_DIR.mkdir(exist_ok=True)

def _user_file(user_id: str) -> Path:
    safe_id = user_id.replace('/', '_').replace('\\', '_')
    return DATA_DIR / f'{safe_id}.json'

def _load(user_id: str) -> dict:
    path = _user_file(user_id)
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _save(user_id: str, data: dict):
    with open(_user_file(user_id), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_day(user_id: str, date: str) -> dict:
    data = _load(user_id)
    return data.get(date, {})

def _ensure_day(data: dict, date: str) -> dict:
    if date not in data:
        data[date] = {
            'weight': None,
            'water': 0,
            'sleep': None,
            'poop': None,
            'meals': [],
            'notes': []
        }
    return data

def save_weight(user_id: str, date: str, weight: float):
    data = _load(user_id)
    data = _ensure_day(data, date)
    data[date]['weight'] = weight
    _save(user_id, data)

def add_water(user_id: str, date: str, ml: int) -> int:
    data = _load(user_id)
    data = _ensure_day(data, date)
    data[date]['water'] = data[date].get('water', 0) + ml
    _save(user_id, data)
    return data[date]['water']

def save_sleep(user_id: str, date: str, info: dict):
    data = _load(user_id)
    data = _ensure_day(data, date)
    data[date]['sleep'] = info
    _save(user_id, data)

def save_poop(user_id: str, date: str, status: str):
    data = _load(user_id)
    data = _ensure_day(data, date)
    data[date]['poop'] = status
    _save(user_id, data)

def add_meal(user_id: str, date: str, meal: dict):
    data = _load(user_id)
    data = _ensure_day(data, date)
    data[date]['meals'].append(meal)
    _save(user_id, data)

def add_note(user_id: str, date: str, text: str):
    data = _load(user_id)
    data = _ensure_day(data, date)
    data[date]['notes'].append({'text': text})
    _save(user_id, data)

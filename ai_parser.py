# features/ai_parser.py - Gemini 自然語言解析（邏輯同原版）

import os
import json
import logging
import re
from datetime import datetime
import google.generativeai as genai

logger = logging.getLogger(__name__)

genai.configure(api_key=os.environ.get("GOOGLE_API_KEY", ""))
_model = genai.GenerativeModel("gemini-2.0-flash")


def _parse_current_date(current_time_str: str):
    try:
        return datetime.strptime(current_time_str[:16], "%Y-%m-%d %H:%M").date()
    except Exception:
        return datetime.now().date()


def _parse_date_token(text: str, current_time_str: str):
    today = _parse_current_date(current_time_str)

    m = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None

    m = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?!\d)", text)
    if m:
        try:
            d = datetime(today.year, int(m.group(1)), int(m.group(2))).date()
            if d < today:
                d = d.replace(year=d.year + 1)
            return d
        except ValueError:
            return None

    m = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
    if m:
        try:
            month = int(m.group(1)[:2])
            day = int(m.group(1)[2:])
            d = datetime(today.year, month, day).date()
            if d < today:
                d = d.replace(year=d.year + 1)
            return d
        except ValueError:
            return None

    return None


def _number_after(pattern: str, text: str):
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def _parse_time_token(text: str):
    m = re.search(r"(?:(?:提醒|時間)\s*)?([01]?\d|2[0-3])[:：]([0-5]\d)", text)
    if not m:
        return "08:00"
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"


def _parse_tracker_locally(text: str, current_time_str: str) -> dict | None:
    text = text.strip()
    parts = text.split(maxsplit=1)
    if not parts:
        return None

    trigger = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if trigger not in ("訂閱", "合約", "租約", "紀念日", "藥物"):
        return None

    remind_days = int(_number_after(r"提前\s*(\d+)\s*天", text) or 7)
    remind_time = _parse_time_token(text)
    amount = _number_after(r"(\d+(?:\.\d+)?)\s*(?:元|塊|NTD|TWD)", text)

    if trigger == "訂閱":
        cycle = "yearly" if any(w in text for w in ("每年", "年繳", "一年")) else "monthly"
        name = re.sub(r"(每月|每年|月繳|年繳|一個月|一年).*", "", rest).strip()
        name = re.sub(r"\s*(20\d{2}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{4})\s*", " ", name).strip()
        name = re.sub(r"\s*\d+(?:\.\d+)?\s*(?:元|塊|NTD|TWD).*", "", name).strip()

        expire_date = _parse_date_token(text, current_time_str)
        if not expire_date:
            m = re.search(r"每月\s*(\d{1,2})\s*(?:號|日)?", text)
            if m:
                today = _parse_current_date(current_time_str)
                day = min(int(m.group(1)), 28)
                expire_date = today.replace(day=day)
                if expire_date < today:
                    month = expire_date.month + 1 if expire_date.month < 12 else 1
                    year = expire_date.year if expire_date.month < 12 else expire_date.year + 1
                    expire_date = expire_date.replace(year=year, month=month)

        return {
            "category": "subscription",
            "name": name or rest,
            "expire_date": expire_date.isoformat() if expire_date else None,
            "is_recurring": 0,
            "recurring_month": None,
            "recurring_day": None,
            "cycle": cycle,
            "amount": amount,
            "remind_days": remind_days,
            "remind_time": remind_time,
            "stock_total": None,
            "stock_daily": None,
        }

    if trigger in ("合約", "租約"):
        expire_date = _parse_date_token(text, current_time_str)
        name = re.sub(r"(20\d{2}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{4}).*", "", rest).strip()
        name = re.sub(r"提前\s*\d+\s*天", "", name).strip()
        return {
            "category": "contract",
            "name": name or rest,
            "expire_date": expire_date.isoformat() if expire_date else None,
            "is_recurring": 0,
            "recurring_month": None,
            "recurring_day": None,
            "cycle": "once",
            "amount": amount,
            "remind_days": remind_days,
            "remind_time": remind_time,
            "stock_total": None,
            "stock_daily": None,
        }

    if trigger == "紀念日":
        d = _parse_date_token(text, current_time_str)
        name = re.sub(r"(20\d{2}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{4}).*", "", rest).strip()
        return {
            "category": "anniversary",
            "name": name or rest,
            "expire_date": None,
            "is_recurring": 1,
            "recurring_month": d.month if d else None,
            "recurring_day": d.day if d else None,
            "cycle": "yearly",
            "amount": None,
            "remind_days": remind_days,
            "remind_time": remind_time,
            "stock_total": None,
            "stock_daily": None,
        }

    total = _number_after(r"(\d+(?:\.\d+)?)\s*(?:顆|錠|粒|包|瓶|ml|毫升)", rest)
    daily = _number_after(r"(?:每天|每日|一天)\s*(\d+(?:\.\d+)?)", rest)
    name = re.sub(r"\d+(?:\.\d+)?\s*(?:顆|錠|粒|包|瓶|ml|毫升).*", "", rest).strip()
    return {
        "category": "medicine",
        "name": name or rest,
        "expire_date": None,
        "is_recurring": 0,
        "recurring_month": None,
        "recurring_day": None,
        "cycle": "once",
        "amount": None,
        "remind_days": remind_days,
        "remind_time": remind_time,
        "stock_total": total,
        "stock_daily": daily,
    }


def parse_natural_language(text: str, current_time_str: str) -> dict | None:
    """
    將自然語言提醒解析成 {"event_datetime": "YYYY-MM-DD HH:MM", "event_content": "..."}
    失敗或非提醒意圖時回傳 None。
    """
    prompt = f"""你是一個行事曆助理。現在時間是 {current_time_str}（台灣時間）。
使用者說：「{text}」

請判斷這是否是一個「設定提醒」的請求。
如果是，請回傳 JSON（不要加 markdown）：
{{"event_datetime": "YYYY-MM-DD HH:MM", "event_content": "事件內容"}}

如果不是，請回傳：
{{"event_datetime": null, "event_content": null}}

注意：
- 「明天」指明天，「後天」指後天，「下週一」指下週一，以此類推
- 「早上」=08:00，「上午」=09:00，「中午」=12:00，「下午」=14:00，「晚上」=19:00，若有具體時間以具體為準
- 只回傳 JSON，不要任何說明文字"""

    try:
        response = _model.generate_content(prompt)
        raw = response.text.strip()
        # 去掉 markdown code block
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        if data.get("event_datetime") and data.get("event_content"):
            return data
        return None
    except Exception as e:
        logger.error(f"AI parse error: {e}")
        return None


def parse_tracker(text: str, current_time_str: str) -> dict | None:
    local = _parse_tracker_locally(text, current_time_str)
    if local and local.get("name"):
        return local

    """
    解析追蹤項目輸入（訂閱/合約/紀念日/藥物）
    成功回傳 dict，失敗回傳 None
    """
    prompt = f"""你是個人助理。現在時間是 {current_time_str}（台灣時間）。
使用者說：「{text}」

判斷是否為新增追蹤項目請求（訂閱/合約/租約/紀念日/藥物）。
若是，只回傳 JSON（不加 markdown）：
{{
  "category": "subscription|contract|anniversary|medicine",
  "name": "項目名稱",
  "expire_date": "YYYY-MM-DD 或 null",
  "is_recurring": 0或1,
  "recurring_month": 月份整數或null,
  "recurring_day": 日期整數或null,
  "cycle": "monthly|yearly|once|null",
  "amount": 金額數字或null,
  "remind_days": 提前提醒天數整數（預設7）,
  "stock_total": 總庫存數字或null,
  "stock_daily": 每日用量數字或null
}}

若不是追蹤請求，只回傳：{{"category": null}}

規則：
- subscription：Netflix、Disney+、Spotify 等定期付費服務，cycle 必填
- contract：租約、貸款、合約等固定期限，expire_date 必填
- anniversary：生日、紀念日等每年重複日期，is_recurring=1，recurring_month 和 recurring_day 必填，expire_date=null
- medicine：藥物補貨，stock_total 和 stock_daily 必填，expire_date=null
- 月份日期如 0520 或 05/20 均表示 5月20日
- 提前天數如「提前30天」則 remind_days=30
- 只回傳 JSON，不要任何說明"""

    try:
        response = _model.generate_content(prompt)
        raw = response.text.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        return data if data.get("category") else None
    except Exception as e:
        logger.error(f"parse_tracker error: {e}")
        return None

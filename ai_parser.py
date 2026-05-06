# features/ai_parser.py - Gemini 自然語言解析（邏輯同原版）

import os
import json
import logging
import google.generativeai as genai

logger = logging.getLogger(__name__)

genai.configure(api_key=os.environ.get("GOOGLE_API_KEY", ""))
_model = genai.GenerativeModel("gemini-2.0-flash")


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

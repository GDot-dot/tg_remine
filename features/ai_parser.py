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

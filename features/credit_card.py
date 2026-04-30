# features/credit_card.py - 信用卡回饋分析

import os
import logging
import requests
import google.generativeai as genai
from db import get_user_cards

logger = logging.getLogger(__name__)

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
SEARCH_ENGINE_ID = os.environ.get("GOOGLE_SEARCH_ENGINE_ID", "")
genai.configure(api_key=GOOGLE_API_KEY)
_model = genai.GenerativeModel("gemini-2.0-flash")


def _search_card_benefits(merchant: str) -> str:
    """Google Custom Search 搜尋商家信用卡回饋（限一年內）"""
    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": GOOGLE_API_KEY,
            "cx": SEARCH_ENGINE_ID,
            "q": f"{merchant} 信用卡 回饋 推薦",
            "num": 5,
            "dateRestrict": "y1",
            "lr": "lang_zh-TW",
        }
        resp = requests.get(url, params=params, timeout=10)
        items = resp.json().get("items", [])
        if not items:
            return "（找不到相關搜尋結果）"
        snippets = [f"- {it.get('title','')}: {it.get('snippet','')}" for it in items]
        return "\n".join(snippets)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return "（搜尋失敗）"


def analyze_best_card(user_id: str, merchant: str) -> str:
    """搜尋 + AI 推薦最佳刷卡策略"""
    cards = get_user_cards(user_id)
    search_result = _search_card_benefits(merchant)

    if not cards:
        cards_info = "（使用者尚未設定任何信用卡）"
    else:
        cards_info = "、".join(cards)

    prompt = f"""你是信用卡回饋專家。

使用者持有的卡片：{cards_info}
消費商家：{merchant}
最新網路資訊：
{search_result}

請根據以上資訊，推薦最適合在「{merchant}」消費的信用卡，並說明理由。
如果使用者沒有最佳卡片，可以說明哪類卡比較好。
回答請簡潔，不超過 200 字。"""

    try:
        resp = _model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        logger.error(f"Gemini card error: {e}")
        return "❌ AI 分析失敗，請稍後再試。"

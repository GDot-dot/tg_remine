# handlers/stickers.py
import asyncio
import logging
from collections import deque

import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from sticker_converter import convert_and_upload

logger = logging.getLogger(__name__)
sticker_users: set[int] = set()
sticker_queues: dict[int, deque] = {}
sticker_queue_active: set[int] = set()


def is_sticker_url(text: str) -> bool:
    return "store.line.me" in text or "line.me/S/sticker" in text


async def _reply(update: Update, text: str):
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    elif update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_sticker_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in sticker_users:
        sticker_users.discard(user_id)
        await _reply(update, "🔴 貼圖轉換模式已關閉。")
        return
    sticker_users.add(user_id)
    await _reply(
        update,
        "🟢 貼圖轉換模式已開啟！\n"
        "請把 LINE 貼圖商店網址傳給我，例如：\n"
        "https://store.line.me/stickershop/product/XXXXX",
    )


async def run_sticker_queue(user_id: int):
    queue = sticker_queues.setdefault(user_id, deque())
    try:
        while queue:
            bot, chat_id, line_url, status = queue.popleft()
            try:
                await status.edit_text("🔍 正在抓取 LINE 網頁資料...")
                link = await convert_and_upload(bot, user_id, chat_id, line_url, status)
                if link:
                    await status.edit_text(f"🎉 轉換完成！\n👉 {link}")
                else:
                    await status.edit_text("❌ 找不到貼圖資料，請確認網址是否正確。")
            except Exception as e:
                logger.error("sticker convert: %s", e, exc_info=True)
                try:
                    await status.edit_text(f"❌ 發生錯誤：{e}")
                except Exception:
                    pass
    finally:
        sticker_queue_active.discard(user_id)
        if not queue:
            sticker_queues.pop(user_id, None)


async def handle_sticker_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not is_sticker_url(text):
        return False

    user_id = update.effective_user.id
    line_url = text.strip()
    if "store.line.me" not in line_url:
        try:
            response = requests.get(
                line_url,
                allow_redirects=True,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
                stream=True,
            )
            response.close()
            line_url = response.url
            logger.info("短網址展開: %s → %s", text.strip(), line_url)
        except Exception as e:
            logger.warning("短網址展開失敗: %s", e)

    if "store.line.me" not in line_url:
        await _reply(update, "❌ 無法識別此 LINE 網址，請確認是貼圖商店的連結。")
        return True
    if user_id not in sticker_users:
        await _reply(update, "💡 請先輸入「貼圖轉換」開啟功能，再貼網址。")
        return True

    queue = sticker_queues.setdefault(user_id, deque())
    position = len(queue) + (1 if user_id in sticker_queue_active else 0)
    status_text = "🔍 正在抓取 LINE 網頁資料..." if position == 0 else f"⏳ 已加入轉換佇列，目前前面有 {position} 個任務。"
    status = await update.message.reply_text(status_text)
    queue.append((ctx.bot, update.effective_chat.id, line_url, status))
    if user_id not in sticker_queue_active:
        sticker_queue_active.add(user_id)
        asyncio.create_task(run_sticker_queue(user_id))
    return True

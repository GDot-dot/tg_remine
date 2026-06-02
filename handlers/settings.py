# handlers/settings.py
import html
import re
from datetime import datetime

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from db import get_user_setting, update_user_setting
from scheduler import build_daily_summary, fetch_weather_summary

TAIPEI_TZ = pytz.timezone("Asia/Taipei")


def parse_hhmm(value: str) -> str | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_snooze_setting(raw: str | None) -> list[int]:
    values = []
    for part in (raw or "5,30,60").split(","):
        try:
            minutes = int(part.strip())
        except ValueError:
            continue
        if 1 <= minutes <= 1440 and minutes not in values:
            values.append(minutes)
    return values[:3] or [5, 30, 60]


def parse_snooze_setting_input(text: str) -> str | None:
    values = []
    for raw in re.split(r"[,，、\s]+", text.strip()):
        if not raw:
            continue
        m = re.match(r"^(\d+)(小時|時|h)$", raw, re.I)
        if m:
            minutes = int(m.group(1)) * 60
        else:
            m = re.match(r"^(\d+)(分|分鐘|m|min)?$", raw, re.I)
            if not m:
                return None
            minutes = int(m.group(1))
        if not (1 <= minutes <= 1440):
            return None
        if minutes not in values:
            values.append(minutes)
    if not 1 <= len(values) <= 3:
        return None
    return ",".join(str(v) for v in values)


def settings_text(user_id: int) -> str:
    setting = get_user_setting(user_id)
    weather = "開啟" if setting.weather_enabled else "關閉"
    morning = setting.morning_summary_time if setting.morning_summary_enabled else "關閉"
    evening = setting.evening_summary_time if setting.evening_summary_enabled else "關閉"
    snooze = "、".join(f"{m}分" for m in parse_snooze_setting(setting.snooze_buttons))
    return (
        "⚙️ <b>設定中心</b>\n\n"
        f"📍 地區/城市：{html.escape(setting.city or '台北')}\n"
        f"🌅 今日摘要：{morning}\n"
        f"🌙 明日預告：{evening}\n"
        f"🌦 天氣資訊：{weather}\n"
        f"💤 常用延後：{snooze}\n\n"
        "天氣來源只使用中央氣象署 CWA；包含天氣狀態、最高/最低溫、降雨機率、出門建議。"
    )


def settings_kb(user_id: int) -> InlineKeyboardMarkup:
    setting = get_user_setting(user_id)
    weather_label = "🌦 關閉天氣" if setting.weather_enabled else "🌦 開啟天氣"
    morning_label = "🌅 關閉今日摘要" if setting.morning_summary_enabled else "🌅 開啟今日摘要"
    evening_label = "🌙 關閉明日預告" if setting.evening_summary_enabled else "🌙 開啟明日預告"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 修改地區", callback_data="set:city")],
        [
            InlineKeyboardButton("🌅 早上時間", callback_data="set:morning_time"),
            InlineKeyboardButton(morning_label, callback_data="set:morning_toggle"),
        ],
        [
            InlineKeyboardButton("🌙 晚上時間", callback_data="set:evening_time"),
            InlineKeyboardButton(evening_label, callback_data="set:evening_toggle"),
        ],
        [InlineKeyboardButton(weather_label, callback_data="set:weather_toggle")],
        [InlineKeyboardButton("💤 常用延後按鈕", callback_data="set:snooze")],
        [
            InlineKeyboardButton("🌤 預覽天氣", callback_data="set:weather_preview"),
            InlineKeyboardButton("📋 預覽摘要", callback_data="set:summary_preview"),
        ],
        [InlineKeyboardButton("❌ 關閉", callback_data="cancel")],
    ])


async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.edit_message_text(
            settings_text(user_id),
            reply_markup=settings_kb(user_id),
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            settings_text(user_id),
            reply_markup=settings_kb(user_id),
            parse_mode=ParseMode.HTML,
        )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_settings(update, ctx)


async def handle_settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str, user_states: dict):
    query = update.callback_query
    user_id = update.effective_user.id
    setting = get_user_setting(user_id)
    await query.answer()

    if action == "city":
        user_states[user_id] = {"action": "setting_city"}
        await query.edit_message_text("📍 請輸入台灣縣市或鄉鎮市區，例如：台北、新北市淡水區、臺中市、宜蘭縣")
        return
    if action == "morning_time":
        user_states[user_id] = {"action": "setting_morning_time"}
        await query.edit_message_text("🌅 請輸入今日摘要時間（HH:MM），例如 <code>08:00</code>", parse_mode=ParseMode.HTML)
        return
    if action == "evening_time":
        user_states[user_id] = {"action": "setting_evening_time"}
        await query.edit_message_text("🌙 請輸入明日預告時間（HH:MM），例如 <code>21:30</code>", parse_mode=ParseMode.HTML)
        return
    if action == "snooze":
        user_states[user_id] = {"action": "setting_snooze"}
        await query.edit_message_text(
            "💤 請輸入 1-3 個常用延後按鈕。\n"
            "例如：<code>5 30 60</code> 或 <code>10分 1小時</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "weather_toggle":
        update_user_setting(user_id, weather_enabled=0 if setting.weather_enabled else 1)
        await show_settings(update, ctx)
        return
    if action == "morning_toggle":
        update_user_setting(user_id, morning_summary_enabled=0 if setting.morning_summary_enabled else 1)
        await show_settings(update, ctx)
        return
    if action == "evening_toggle":
        update_user_setting(user_id, evening_summary_enabled=0 if setting.evening_summary_enabled else 1)
        await show_settings(update, ctx)
        return
    if action == "weather_preview":
        weather = fetch_weather_summary(setting.city) or "暫時查不到天氣資料，請確認城市名稱。"
        await query.message.reply_text(weather)
        return
    if action == "summary_preview":
        text = build_daily_summary(user_id, datetime.now(TAIPEI_TZ).date(), "🌅 今日摘要預覽", bool(setting.weather_enabled), setting.city)
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_settings_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str, user_states: dict, text: str) -> bool:
    user_id = update.effective_user.id

    if action == "setting_city":
        city = text.strip()
        if len(city) < 2 or len(city) > 50:
            await update.message.reply_text("❌ 城市名稱請輸入 2-50 個字。", parse_mode=ParseMode.HTML)
            return True
        user_states.pop(user_id, None)
        update_user_setting(user_id, city=city)
        await update.message.reply_text(f"✅ 地區已更新為：{html.escape(city)}", parse_mode=ParseMode.HTML)
        await show_settings(update, ctx)
        return True

    if action == "setting_morning_time":
        time_str = parse_hhmm(text)
        if not time_str:
            await update.message.reply_text("❌ 請輸入 HH:MM，例如 <code>08:00</code>。", parse_mode=ParseMode.HTML)
            return True
        user_states.pop(user_id, None)
        update_user_setting(user_id, morning_summary_time=time_str, morning_summary_enabled=1)
        await update.message.reply_text(f"✅ 今日摘要時間已更新為：{time_str}", parse_mode=ParseMode.HTML)
        await show_settings(update, ctx)
        return True

    if action == "setting_evening_time":
        time_str = parse_hhmm(text)
        if not time_str:
            await update.message.reply_text("❌ 請輸入 HH:MM，例如 <code>21:30</code>。", parse_mode=ParseMode.HTML)
            return True
        user_states.pop(user_id, None)
        update_user_setting(user_id, evening_summary_time=time_str, evening_summary_enabled=1)
        await update.message.reply_text(f"✅ 明日預告時間已更新為：{time_str}", parse_mode=ParseMode.HTML)
        await show_settings(update, ctx)
        return True

    if action == "setting_snooze":
        snooze_buttons = parse_snooze_setting_input(text)
        if not snooze_buttons:
            await update.message.reply_text("❌ 請輸入 1-3 個 1-1440 分鐘內的值，例如 <code>5 30 60</code>。", parse_mode=ParseMode.HTML)
            return True
        user_states.pop(user_id, None)
        update_user_setting(user_id, snooze_buttons=snooze_buttons)
        label = "、".join(f"{m}分" for m in parse_snooze_setting(snooze_buttons))
        await update.message.reply_text(f"✅ 常用延後按鈕已更新：{label}", parse_mode=ParseMode.HTML)
        await show_settings(update, ctx)
        return True

    return False

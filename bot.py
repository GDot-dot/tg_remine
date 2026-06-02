# bot.py - Telegram 智慧管家（Flask + Gunicorn）
# 邏輯對齊原 LINE 版本

import os
import re
import html
import threading
import asyncio
import logging
import requests
from collections import deque
from datetime import datetime, timedelta
from flask import Flask, request, render_template_string

import pytz
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from db import (
    init_db,
    add_event, get_event, get_user_events,
    update_event_content, update_event_fields, delete_event_by_id,
    add_location, get_locations, get_location_by_name, delete_location,
    save_memory, query_memory, get_memory_by_id, update_memory_by_id,
    delete_memory_by_id, forget_memory, list_memories,
    get_user_setting, update_user_setting, get_trackers,
)
from scheduler import (
    scheduler, safe_start, safe_add_job, safe_add_cron,
    remove_job, send_reminder, TAIPEI_TZ, PRIORITY_RULES,
    fetch_weather_summary, build_daily_summary,
)
from sticker_converter import convert_and_upload
from handlers.tracker import (
    handle_tracker_input, handle_tracker_list,
    handle_monthly_cost, handle_tracker_delete,
    handle_tracker_detail, handle_tracker_edit_prompt,
    handle_tracker_toggle_notify, handle_tracker_edit_value,
    TRIGGER_MAP as TRACKER_TRIGGER_MAP,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT        = int(os.environ.get("PORT", 8080))

app = Flask(__name__)
_loop: asyncio.AbstractEventLoop = None
_ptb_app: Application = None
user_states: dict[int, dict] = {}
sticker_users: set[int] = set()  # 已開啟貼圖轉換模式的 user_id
sticker_queues: dict[int, deque] = {}
sticker_queue_active: set[int] = set()


# ── asyncio bridge ────────────────────────────────────────────────────────────

def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def _async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=30)


# ── 鍵盤工具 ─────────────────────────────────────────────────────────────────

def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )

WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

# 提早提醒選項（對齊 LINE 版）
EARLY_OPTIONS = [
    ("準時提醒",  0),
    ("前 10 分鐘", 10),
    ("前 30 分鐘", 30),
    ("1 天前",    1440),
    ("不提醒",   -1),
]

# 重要提醒優先等級選項（對齊 LINE 版）
PRIORITY_OPTIONS = [
    ("🔴 高（5分重提/12次）",  3),
    ("🟡 中（10分重提/6次）",  2),
    ("🟢 低（30分重提/3次）",  1),
    ("🔧 自訂間隔與次數",       0),
]


def recurring_kb(selected: set) -> InlineKeyboardMarkup:
    rows = []
    for chunk in [WEEKDAY_CODES[:4], WEEKDAY_CODES[4:]]:
        row = []
        for code in chunk:
            idx   = WEEKDAY_CODES.index(code)
            label = f"✅{WEEKDAY_NAMES[idx]}" if code in selected else WEEKDAY_NAMES[idx]
            row.append(InlineKeyboardButton(label, callback_data=f"rec:toggle:{code}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("⏰ 設定時間", callback_data="rec:settime")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def parse_recurring_rule(rule: str | None) -> tuple[set[str], str]:
    if not rule or "|" not in rule:
        return set(), "09:00"
    days_str, time_str = rule.split("|", 1)
    days = {d for d in days_str.split(",") if d in WEEKDAY_CODES}
    if not re.match(r"^\d{2}:\d{2}$", time_str or ""):
        time_str = "09:00"
    return days, time_str


def weekday_names(days: set[str]) -> list[str]:
    return [WEEKDAY_NAMES[WEEKDAY_CODES.index(d)] for d in sorted(days) if d in WEEKDAY_CODES]


def parse_hhmm(value: str) -> str | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not m:
        return None
    h, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        return None
    return f"{h:02d}:{minute:02d}"


def parse_snooze_input(value: str) -> tuple[datetime, str] | None:
    text = value.strip()
    now = now_taipei()

    m = re.match(r"^(?:延後\s*)?(\d{1,4})(?:\s*(?:分|分鐘|m|min))?$", text, re.I)
    if m:
        minutes = int(m.group(1))
        if 1 <= minutes <= 1440:
            return now + timedelta(minutes=minutes), f"{minutes} 分鐘"
        return None

    time_str = parse_hhmm(text)
    if time_str:
        hour, minute = map(int, time_str.split(":"))
        run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        return run_at, run_at.strftime("%m/%d %H:%M")

    return None


def parse_custom_reminder_time(value: str, event_dt: datetime) -> tuple[datetime, str] | None:
    text = value.strip()

    m = re.match(r"^(?:提前\s*)?(\d{1,5})(?:\s*(?:分|分鐘|m|min))?$", text, re.I)
    if m:
        minutes = int(m.group(1))
        if 0 <= minutes <= 10080:
            return event_dt - timedelta(minutes=minutes), f"提前 {minutes} 分鐘"
        return None

    time_str = parse_hhmm(text)
    if time_str:
        hour, minute = map(int, time_str.split(":"))
        reminder_dt = event_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return reminder_dt, reminder_dt.strftime("%Y/%m/%d %H:%M")

    return None


def priority_interval(ev) -> int:
    if ev.recurrence_rule and ev.recurrence_rule.startswith("custom:"):
        try:
            return int(ev.recurrence_rule.split(":", 1)[1])
        except (TypeError, ValueError):
            pass
    return PRIORITY_RULES.get(ev.priority_level, PRIORITY_RULES[1])["interval"]


def reminder_list_kb(events: list, page: int = 0, page_size: int = 5):
    total = len(events)
    chunk = events[page * page_size:(page + 1) * page_size]
    lines = [f"📋 <b>提醒清單</b>（共 {total} 筆）\n"]
    buttons = []
    for ev in chunk:
        if ev.is_recurring:
            line = f"🔁 {ev.event_content} [{ev.recurrence_rule}]"
        else:
            rt       = ev.reminder_time.astimezone(TAIPEI_TZ)
            snoozing = ev.reminder_time != ev.event_datetime
            icon     = "💤" if snoozing else "⏰"
            if ev.priority_level == 3: icon = "🔴"
            elif ev.priority_level == 2: icon = "🟡"
            elif ev.priority_level == 1: icon = "🟢"
            prefix = "(延) " if snoozing else ""
            line   = f"{icon} {rt.strftime('%m/%d %H:%M')} {prefix}{ev.event_content}"
        lines.append(line)
        buttons.append([
            InlineKeyboardButton(f"✏️ {ev.event_content[:12]}", callback_data=f"re:edit:{ev.id}"),
            InlineKeyboardButton("🗑️ 刪除", callback_data=f"re:del:{ev.id}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上頁", callback_data=f"re:page:{page-1}"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton("下頁 ▶", callback_data=f"re:page:{page+1}"))
    if nav:
        buttons.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ── 工具 ─────────────────────────────────────────────────────────────────────

def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)

def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")

def chat_type(update: Update) -> str:
    return "group" if is_group(update) else "private"

async def reply(update: Update, text: str, keyboard=None):
    kwargs = {"parse_mode": ParseMode.HTML}
    if keyboard:
        kwargs["reply_markup"] = keyboard
    if update.callback_query:
        await update.callback_query.message.reply_text(text, **kwargs)
    else:
        await update.message.reply_text(text, **kwargs)


def parse_dt_from_parts(date_str: str, time_str: str | None) -> datetime | None:
    """
    對齊 LINE 版：支援 今天/明天/後天 及 YYYY-MM-DD / MM/DD 格式
    time_str 可為 None（預設 00:00）
    """
    now = now_taipei()
    day_map = {"今天": 0, "明天": 1, "後天": 2}

    if date_str in day_map:
        base = now + timedelta(days=day_map[date_str])
        date_part = base.strftime("%Y-%m-%d")
    else:
        date_part = date_str.replace("/", "-")
        if date_part.count("-") == 1:          # MM-DD → 補年
            date_part = f"{now.year}-{date_part}"

    time_part = time_str if time_str else "00:00"
    try:
        naive = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M")
        return TAIPEI_TZ.localize(naive)
    except ValueError:
        return None


# ── /start & /help ────────────────────────────────────────────────────────────

HELP_TEXT = """🤖 <b>Telegram 智慧管家</b>

<b>📅 提醒功能</b>
<code>提醒 [誰] [日期] [時間] [事件]</code>
<i>日期可用：今天 / 明天 / 後天 / MM/DD / YYYY-MM-DD（空格可省略）</i>
<code>重要提醒 [誰] [日期] [時間] [事件]</code>
<code>週期提醒</code> — 設定每週重複
<code>提醒清單</code> — 管理所有提醒

<b>📍 地點功能</b>
<code>找地點 [名稱]</code> / <code>地點清單</code>
<code>刪除地點 [名稱]</code>
（傳送位置訊息可儲存）

<b>🧠 記憶功能</b>
<code>記住 [關鍵字] [內容]</code>
<code>查詢 [關鍵字]</code>
<code>忘記 [關鍵字]</code> / <code>記憶清單</code>
內容格式：<code>**粗體**</code>、<code>__斜體__</code>、<code>||防劇透||</code>、<code>`等寬`</code>

<b>📌 追蹤功能</b>
<code>訂閱 Netflix 每月15號 390元</code>
<code>合約 租約 2026/12/31 提前30天</code>
<code>紀念日 媽媽生日 0520</code>
<code>藥物 魚油 60顆 每天2顆</code>
<code>追蹤清單</code> / <code>訂閱清單</code> / <code>紀念日清單</code>
<code>每月支出</code> — 訂閱費用統計
<code>刪除追蹤 [名稱]</code>

<b>🎨 LINE 貼圖轉換</b>
<code>貼圖轉換</code> — 開啟／關閉轉換模式
開啟後貼上 LINE 商店網址即自動轉換至 Telegram

<b>⚙️ 設定中心</b>
<code>設定</code> / <code>/settings</code>
可設定地區/城市、早上今日摘要、晚上明日預告、是否附上天氣、常用延後按鈕。
天氣來源只使用中央氣象署 CWA；包含天氣狀態、最高/最低溫、降雨機率、出門建議。

<b>通用</b>：<code>取消</code> — 中斷操作"""

# 浮動快捷鍵盤（Reply Keyboard）
_REPLY_KB = ReplyKeyboardMarkup(
    [
        ["📋 提醒清單", "🧠 記憶清單"],
        ["📌 追蹤清單", "💳 每月支出"],
        ["📍 地點清單", "🎨 貼圖轉換"],
        ["🌐 Web 清單", "⌨️ 隱藏鍵盤"],
        ["⚙️ 設定中心", "❓ 說明"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

def _public_base_url():
    url = WEBHOOK_URL or ""
    if not url:
        return ""
    if url.endswith("/webhook"):
        return url[:-8]
    return url.rstrip("/")

def _web_lists_url(user_id):
    base_url = _public_base_url()
    if not base_url:
        return ""
    return f"{base_url}/web/lists/{user_id}"

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 歡迎使用 Telegram 智慧管家！\n\n{HELP_TEXT}",
        parse_mode=ParseMode.HTML,
        reply_markup=_REPLY_KB,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_TEXT)

async def cmd_hide_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌨️ 已隱藏快捷鍵盤。輸入 /start 可以重新顯示。", reply_markup=ReplyKeyboardRemove())

async def send_web_lists_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = _web_lists_url(update.effective_user.id)
    if not url:
        await reply(update, "🌐 WEBHOOK_URL 尚未設定，暫時無法產生 Web 清單連結。")
        return
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 開啟 Web 清單", url=url)
    ]])
    await reply(update, "🌐 用 Web 版查看清單紀錄：", markup)


# ── 設定中心 ────────────────────────────────────────────────────────────────

def _settings_text(user_id: int) -> str:
    s = get_user_setting(user_id)
    weather = "開啟" if s.weather_enabled else "關閉"
    morning = s.morning_summary_time if s.morning_summary_enabled else "關閉"
    evening = s.evening_summary_time if s.evening_summary_enabled else "關閉"
    snooze = "、".join(f"{m}分" for m in _parse_snooze_setting(s.snooze_buttons))
    return (
        "⚙️ <b>設定中心</b>\n\n"
        f"📍 地區/城市：{html.escape(s.city or '台北')}\n"
        f"🌅 今日摘要：{morning}\n"
        f"🌙 明日預告：{evening}\n"
        f"🌦 天氣資訊：{weather}\n"
        f"💤 常用延後：{snooze}\n\n"
        "天氣來源只使用中央氣象署 CWA；包含天氣狀態、最高/最低溫、降雨機率、出門建議。"
    )

def _settings_kb(user_id: int) -> InlineKeyboardMarkup:
    s = get_user_setting(user_id)
    weather_label = "🌦 關閉天氣" if s.weather_enabled else "🌦 開啟天氣"
    morning_label = "🌅 關閉今日摘要" if s.morning_summary_enabled else "🌅 開啟今日摘要"
    evening_label = "🌙 關閉明日預告" if s.evening_summary_enabled else "🌙 開啟明日預告"
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

def _parse_snooze_setting(raw: str | None) -> list[int]:
    values = []
    for part in (raw or "5,30,60").split(","):
        try:
            minutes = int(part.strip())
        except ValueError:
            continue
        if 1 <= minutes <= 1440 and minutes not in values:
            values.append(minutes)
    return values[:3] or [5, 30, 60]

def _parse_snooze_setting_input(text: str) -> str | None:
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

async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.callback_query:
        await update.callback_query.edit_message_text(
            _settings_text(user_id),
            reply_markup=_settings_kb(user_id),
            parse_mode=ParseMode.HTML,
        )
    else:
        await reply(update, _settings_text(user_id), _settings_kb(user_id))

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_settings(update, ctx)

async def handle_settings_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str):
    q = update.callback_query
    user_id = update.effective_user.id
    s = get_user_setting(user_id)
    await q.answer()

    if action == "city":
        user_states[user_id] = {"action": "setting_city"}
        await q.edit_message_text("📍 請輸入台灣縣市或鄉鎮市區，例如：台北、新北市淡水區、臺中市、宜蘭縣")
        return
    if action == "morning_time":
        user_states[user_id] = {"action": "setting_morning_time"}
        await q.edit_message_text("🌅 請輸入今日摘要時間（HH:MM），例如 <code>08:00</code>", parse_mode=ParseMode.HTML)
        return
    if action == "evening_time":
        user_states[user_id] = {"action": "setting_evening_time"}
        await q.edit_message_text("🌙 請輸入明日預告時間（HH:MM），例如 <code>21:30</code>", parse_mode=ParseMode.HTML)
        return
    if action == "snooze":
        user_states[user_id] = {"action": "setting_snooze"}
        await q.edit_message_text(
            "💤 請輸入 1-3 個常用延後按鈕。\n"
            "例如：<code>5 30 60</code> 或 <code>10分 1小時</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "weather_toggle":
        update_user_setting(user_id, weather_enabled=0 if s.weather_enabled else 1)
        await show_settings(update, ctx)
        return
    if action == "morning_toggle":
        update_user_setting(user_id, morning_summary_enabled=0 if s.morning_summary_enabled else 1)
        await show_settings(update, ctx)
        return
    if action == "evening_toggle":
        update_user_setting(user_id, evening_summary_enabled=0 if s.evening_summary_enabled else 1)
        await show_settings(update, ctx)
        return
    if action == "weather_preview":
        weather = fetch_weather_summary(s.city) or "暫時查不到天氣資料，請確認城市名稱。"
        await q.message.reply_text(weather)
        return
    if action == "summary_preview":
        text = build_daily_summary(user_id, now_taipei().date(), "🌅 今日摘要預覽", bool(s.weather_enabled), s.city)
        await q.message.reply_text(text, parse_mode=ParseMode.HTML)
        return


# ── 提醒解析（對齊 LINE regex 邏輯）─────────────────────────────────────────

_REMINDER_RE = re.compile(
    r"^(?:提醒|重要提醒)(.*?)\s*"           # 前綴 + 誰（可空，空格可選）
    r"(今天|明天|後天|\d{1,4}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)"  # 日期
    r"\s*(\d{1,2}:\d{2})?"                # 時間（可選）
    r"\s*(.+)$"                           # 事件內容
)

def parse_reminder_text(text: str):
    """回傳 (who, event_dt, content) 或 (None, None, None)"""
    m = _REMINDER_RE.match(text)
    if not m:
        return None, None, None
    who_raw, date_s, time_s, content = m.groups()
    who = who_raw.strip() or "我"
    dt  = parse_dt_from_parts(date_s, time_s)
    return who, dt, content.strip()


# ── 一般提醒：建立後問提早時間（對齊 LINE 版）────────────────────────────────

async def handle_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    who, dt, content = parse_reminder_text(text)

    if not (who and dt and content):
        await reply(update,
            "格式：<code>提醒 [誰] [日期] [時間] [事件]</code>\n"
            "日期：今天 / 明天 / 後天 / MM/DD / YYYY-MM-DD\n"
            "例：<code>提醒 我 明天 09:00 開會</code>")
        return
    if dt <= now_taipei():
        await reply(update, "⚠️ 提醒時間不能設定在過去喔！")
        return

    display = update.effective_user.first_name or who
    chat_id = str(update.effective_chat.id)
    ctype   = chat_type(update)

    event_id = add_event(creator_user_id=user_id, target_id=chat_id,
                         target_type=ctype, display_name=display,
                         content=content, event_datetime=dt)
    if not event_id:
        await reply(update, "❌ 建立提醒失敗。")
        return

    # 問提早時間（對齊 LINE 版 QuickReply）
    rows = []
    row  = []
    for label, minutes in EARLY_OPTIONS:
        cb = f"sr:{event_id}:{minutes}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🕒 自訂提醒時間", callback_data=f"src:{event_id}")])

    markup = InlineKeyboardMarkup(rows)
    await reply(update,
        f"✅ 已記錄！\n\n👤 {who}\n📅 {dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}\n\n"
        f"希望什麼時候提醒您呢？", markup)


async def cb_set_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                           event_id: int, minutes: int):
    """
    對齊 LINE 的 set_reminder postback：
    -1 = 不提醒（刪除事件）
     0 = 準時（reminder_time = event_datetime）
    >0 = 提前 N 分鐘
    """
    q = update.callback_query
    await q.answer()

    if minutes == -1:
        # 不提醒 → 刪除事件（對齊 LINE 的 type=none）
        delete_event_by_id(event_id, str(update.effective_user.id))
        await q.edit_message_text("🗑️ OK，已取消記錄。")
        return

    ev = get_event(event_id)
    if not ev:
        await q.edit_message_text("❌ 找不到事件。")
        return

    event_dt    = ev.event_datetime.astimezone(TAIPEI_TZ)
    reminder_dt = event_dt - timedelta(minutes=minutes)

    if reminder_dt <= now_taipei():
        await q.edit_message_text("⚠️ 提醒時間已過，無法設定。")
        return

    update_event_fields(event_id, reminder_time=reminder_dt, reminder_sent=0)
    safe_add_job(send_reminder, reminder_dt, [event_id], f"reminder_{event_id}")

    early_txt = f"（{[l for l,m in EARLY_OPTIONS if m==minutes][0]}）" if minutes > 0 else "（準時）"
    await q.edit_message_text(
        f"✅ 設定完成！\n"
        f"將於 {reminder_dt.strftime('%Y/%m/%d %H:%M')} {early_txt} 提醒您。")


async def cb_custom_reminder_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    if not get_event(event_id):
        await q.edit_message_text("❌ 找不到事件。")
        return
    user_states[update.effective_user.id] = {
        "action": "reminder_custom_time",
        "event_id": event_id,
    }
    await q.edit_message_text(
        "🕒 請輸入自訂提醒時間：\n"
        "可輸入提前分鐘數，例如 <code>45</code>、<code>提前120分鐘</code>；\n"
        "或輸入當天提醒時間，例如 <code>09:30</code>。"
    )


# ── 重要提醒：兩步驟流程（對齊 LINE 版）─────────────────────────────────────

async def handle_priority_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """第一步：解析指令 → 問提早時間"""
    user_id  = update.effective_user.id
    who, dt, content = parse_reminder_text(text)

    if not (who and dt and content):
        await reply(update,
            "格式：<code>重要提醒 [誰] [日期] [時間] [事件]</code>\n"
            "例：<code>重要提醒 我 明天 10:00 搶票</code>")
        return
    if dt <= now_taipei():
        await reply(update, "⚠️ 提醒時間不能設定在過去！")
        return

    display = update.effective_user.first_name or who
    # 存入 state，等使用者選提早時間
    user_states[user_id] = {
        "action":   "priority_pick_early",
        "who":      who,
        "display":  display,
        "dt":       dt,
        "content":  content,
        "chat_id":  str(update.effective_chat.id),
        "ctype":    chat_type(update),
    }

    rows = []
    row  = []
    for label, minutes in EARLY_OPTIONS:
        if minutes == -1: continue  # 重要提醒不提供「不提醒」
        cb = f"pe:{minutes}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])

    await reply(update,
        f"❗ 重要提醒設定\n\n"
        f"👤 {who}\n📅 {dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}\n\n"
        "您希望在事件發生前多久收到通知？",
        InlineKeyboardMarkup(rows))


async def cb_priority_early(update: Update, ctx: ContextTypes.DEFAULT_TYPE, minutes: int):
    """第二步：選提早時間 → 問優先等級"""
    q       = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    state   = user_states.get(user_id, {})
    if state.get("action") != "priority_pick_early":
        return

    state["minutes_early"] = minutes
    state["action"]        = "priority_pick_level"

    rows = [[InlineKeyboardButton(label, callback_data=f"pl:{level}")]
            for label, level in PRIORITY_OPTIONS]
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])

    early_txt = next((l for l,m in EARLY_OPTIONS if m==minutes), "準時")
    await q.edit_message_text(
        f"提前 {early_txt} 提醒。\n\n請選擇重複提醒的頻率：",
        reply_markup=InlineKeyboardMarkup(rows))


async def cb_priority_level(update: Update, ctx: ContextTypes.DEFAULT_TYPE, level: int):
    q       = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    state   = user_states.get(user_id, {})
    if state.get("action") != "priority_pick_level":
        return

    # 自訂：不 pop state，等使用者輸入文字
    if level == 0:
        state["action"] = "priority_input_custom"
        await q.edit_message_text(
            "🔧 請輸入自訂設定：\n"
            "格式：<code>間隔分鐘 重複次數</code>\n"
            "例：<code>15 4</code> → 每 15 分鐘重提，共 4 次\n\n"
            "間隔上限 1440 分鐘，次數上限 50 次",
            parse_mode=ParseMode.HTML,
        )
        return

    # 預設等級：pop state，建立事件
    state = user_states.pop(user_id, {})
    rule  = PRIORITY_RULES[level]
    await _create_priority_event(update, ctx, q, state, level,
                                  rule["interval"], rule["repeats"])


# ── 抽出共用建立邏輯 ──────────────────────────────────────────────────────────

async def _create_priority_event(update, ctx, q, state, level, interval, repeats):
    dt: datetime   = state["dt"]
    minutes_early  = state["minutes_early"]
    reminder_dt    = dt - timedelta(minutes=minutes_early)

    if reminder_dt <= now_taipei():
        await q.edit_message_text("⚠️ 計算出的提醒時間已過，無法設定。")
        return

    rule_icon = PRIORITY_RULES.get(level, {}).get("icon", "🔧") if level else "🔧"

    event_id = add_event(
        creator_user_id=state["creator_user_id"] if "creator_user_id" in state else update.effective_user.id,
        target_id=state["chat_id"],
        target_type=state["ctype"],
        display_name=state["display"],
        content=state["content"],
        event_datetime=dt,
        recurrence_rule=f"custom:{interval}" if level == 0 else None,
        priority_level=level or 1,      # 自訂存 1，interval/repeats 自行控制
        remaining_repeats=repeats,
    )
    if not event_id:
        await q.edit_message_text("❌ 建立失敗。")
        return

    # 自訂 interval 會存進 recurrence_rule，scheduler 會依 custom:N 重排。

    safe_add_job(send_reminder, reminder_dt, [event_id], f"reminder_{event_id}")
    early_txt = next((l for l, m in EARLY_OPTIONS if m == minutes_early), "準時")
    await q.edit_message_text(
        f"{rule_icon} 重要提醒已設定！\n\n"
        f"📅 {dt.strftime('%Y/%m/%d %H:%M')}（{early_txt}開始提醒）\n"
        f"📝 {state['content']}\n"
        f"⏱️ 每 {interval} 分鐘重提，共 {repeats} 次"
    )


# ── 確認 / 延後（對齊 LINE：非週期確認後刪除）────────────────────────────────

async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer("✅ 已確認！")
    ev = get_event(event_id)
    if ev:
        if ev.is_recurring:
            # 週期提醒：只確認這次，cron job 保持，下週繼續
            await q.edit_message_text("✅ 提醒已確認收到！（下個週期會繼續提醒）")
        else:
            # 非週期（含重要提醒）：停止並刪除
            remove_job(event_id)
            delete_event_by_id(event_id, str(update.effective_user.id))
            await q.edit_message_text("✅ 任務已完成並移除！")
    else:
        await q.edit_message_text("✅ 提醒已確認（任務已結束）。")


async def cb_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int, minutes: int):
    q = update.callback_query
    await q.answer(f"💤 延後 {minutes} 分鐘")
    ev = get_event(event_id)
    if not ev:
        await q.edit_message_text("❌ 找不到事件。")
        return
    new_time = now_taipei() + timedelta(minutes=minutes)
    update_event_fields(event_id, reminder_time=new_time, reminder_sent=0)
    # 內容加上 (延) 標記（對齊 LINE 版）
    content = ev.event_content
    if not content.startswith("(延)"):
        update_event_content(event_id, f"(延) {content}")
    safe_add_job(send_reminder, new_time, [event_id], f"reminder_{event_id}")
    await q.edit_message_text(f"💤 已延後 {minutes} 分鐘\n新提醒時間：{new_time.strftime('%H:%M')}")


async def cb_snooze_custom_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    if not get_event(event_id):
        await q.edit_message_text("❌ 找不到事件。")
        return
    user_states[update.effective_user.id] = {
        "action": "snooze_custom",
        "event_id": event_id,
    }
    await q.edit_message_text(
        "🕒 請輸入要延後多久，或指定提醒時間：\n"
        "例如 15(15分鐘)、90分鐘、14:30"
    )


# ── 提醒清單 ─────────────────────────────────────────────────────────────────

async def handle_reminder_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    user_id = update.effective_user.id
    # 對齊 LINE 版：過濾掉已發送且非週期的
    all_events = get_user_events(str(user_id))
    events = [ev for ev in all_events
              if ev.is_recurring or (not ev.reminder_sent and ev.reminder_time is not None)]
    if not events:
        await reply(update, "📋 目前沒有進行中的提醒。")
        return
    text, markup = reminder_list_kb(events, page)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await reply(update, text, markup)


async def cb_delete_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("⚠️ 確定要刪除此提醒？",
        reply_markup=kb([("✅ 確認刪除", f"re:delok:{event_id}"), ("❌ 取消", "cancel")]))

async def cb_delete_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    remove_job(event_id)
    ok = delete_event_by_id(event_id, str(update.effective_user.id))
    await q.edit_message_text("🗑️ 已刪除。" if ok else "❌ 找不到該提醒。")

async def cb_edit_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    ev = get_event(event_id)
    if not ev:
        await q.edit_message_text("❌ 找不到該提醒。")
        return
    rows = [[
        InlineKeyboardButton("✏️ 改內容", callback_data=f"re:edit_content:{event_id}"),
        InlineKeyboardButton("⏰ 改時間", callback_data=f"re:edit_time:{event_id}"),
    ]]
    if ev.priority_level > 0 and not ev.is_recurring:
        rows.append([InlineKeyboardButton("🔁 改重提規則", callback_data=f"re:edit_priority:{event_id}")])
    kb = InlineKeyboardMarkup(rows)
    if ev.is_recurring:
        days, time_str = parse_recurring_rule(ev.recurrence_rule)
        schedule = f"每{'、'.join(weekday_names(days))} {time_str}" if days else ev.recurrence_rule
    else:
        rt = ev.reminder_time.astimezone(TAIPEI_TZ)
        schedule = rt.strftime("%Y/%m/%d %H:%M")
        if ev.priority_level > 0:
            schedule += f"\n🔁 每 {priority_interval(ev)} 分鐘重提，剩餘 {ev.remaining_repeats} 次"
    await q.edit_message_text(
        f"📝 <b>{ev.event_content}</b>\n"
        f"⏰ {schedule}\n\n"
        "要修改哪個項目？",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

async def cb_edit_content_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    ev = get_event(event_id)
    if not ev:
        await q.edit_message_text("❌ 找不到該提醒。")
        return
    user_states[update.effective_user.id] = {
        "action":   "edit_reminder_content",
        "event_id": event_id,
        "original": ev.event_content,
    }
    await q.edit_message_text(
        f"✏️ 請輸入新的提醒內容：\n目前：{ev.event_content}\n\n"
        "💡 以 <code>+</code> 開頭可補充而非覆蓋",
        parse_mode=ParseMode.HTML)

async def cb_edit_time_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    ev = get_event(event_id)
    if not ev:
        await q.edit_message_text("❌ 找不到該提醒。")
        return
    if ev.is_recurring:
        days, _ = parse_recurring_rule(ev.recurrence_rule)
        user_states[update.effective_user.id] = {
            "action":   "recurring_edit_select_days",
            "event_id": event_id,
            "days":     days,
        }
        await q.edit_message_text(
            "🔁 重新選擇要提醒的星期（可多選）：",
            reply_markup=recurring_kb(days))
        return
    rt = ev.reminder_time.astimezone(TAIPEI_TZ)
    user_states[update.effective_user.id] = {
        "action":   "edit_reminder_time",
        "event_id": event_id,
    }
    await q.edit_message_text(
        f"⏰ 請輸入新的提醒時間：\n目前：{rt.strftime('%Y/%m/%d %H:%M')}\n\n"
        "格式：<code>MM/DD HH:MM</code> 或 <code>今天/明天 HH:MM</code>",
        parse_mode=ParseMode.HTML)

async def cb_edit_priority_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer()
    ev = get_event(event_id)
    if not ev or ev.priority_level <= 0 or ev.is_recurring:
        await q.edit_message_text("❌ 找不到可編輯的重要提醒。")
        return
    user_states[update.effective_user.id] = {
        "action":   "edit_priority_rule",
        "event_id": event_id,
    }
    await q.edit_message_text(
        "🔁 請輸入新的重提規則：\n"
        f"目前：每 {priority_interval(ev)} 分鐘，剩餘 {ev.remaining_repeats} 次\n\n"
        "格式：<code>分鐘 次數</code>\n"
        "例如：<code>15 4</code>",
        parse_mode=ParseMode.HTML)


# ── 週期提醒（邏輯同原版，UI 改 InlineKeyboard）──────────────────────────────

async def handle_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = {"action": "recurring_select_days", "days": set()}
    await reply(update,
        "🔁 週期提醒設定\n請選擇要提醒的星期（可多選）：",
        recurring_kb(set()))

async def cb_rec_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE, day: str):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    state   = user_states.setdefault(user_id, {"action": "recurring_select_days", "days": set()})
    days: set = state.setdefault("days", set())
    days.discard(day) if day in days else days.add(day)
    prompt = "🔁 重新選擇要提醒的星期（可多選）：" if state.get("action") == "recurring_edit_select_days" else "🔁 週期提醒設定\n請選擇要提醒的星期（可多選）："
    await q.edit_message_text(
        prompt,
        reply_markup=recurring_kb(days))

async def cb_rec_settime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    days    = user_states.get(user_id, {}).get("days", set())
    if not days:
        await q.answer("⚠️ 請至少選一天！", show_alert=True)
        return
    if user_states[user_id].get("action") == "recurring_edit_select_days":
        user_states[user_id]["action"] = "recurring_edit_set_time"
    else:
        user_states[user_id]["action"] = "recurring_set_time"
    await q.edit_message_text("⏰ 請輸入提醒時間（格式：HH:MM，如 09:00）：")

async def _finish_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                             user_id: int, time_str: str, content: str):
    state    = user_states.pop(user_id, {})
    days: set = state.get("days", set())
    days_str  = ",".join(sorted(days))
    rule_str  = f"{days_str}|{time_str}"
    h, minute = map(int, time_str.split(":"))
    display   = update.effective_user.first_name or "您"
    event_id  = add_event(
        creator_user_id=user_id,
        target_id=str(update.effective_chat.id),
        target_type=chat_type(update),
        display_name=display, content=content,
        event_datetime=now_taipei(),
        is_recurring=1, recurrence_rule=rule_str,
    )
    if not event_id:
        await reply(update, "❌ 建立失敗。")
        return
    safe_add_cron(send_reminder, [event_id], f"recurring_{event_id}", days_str, h, minute)
    day_names = [WEEKDAY_NAMES[WEEKDAY_CODES.index(d)] for d in sorted(days) if d in WEEKDAY_CODES]
    await reply(update,
        f"✅ 週期提醒已設定！\n\n📆 每{'、'.join(day_names)} {time_str}\n📝 {content}")


# ── 地點 ─────────────────────────────────────────────────────────────────────

async def handle_location_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loc     = update.message.location
    user_states[user_id] = {"action": "await_location_name",
                             "lat": loc.latitude, "lng": loc.longitude}
    await update.message.reply_text(
        "📍 收到位置！請輸入地點名稱（如：公司、家）：",
        reply_markup=ReplyKeyboardRemove())

async def handle_location_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    locs    = get_locations(user_id)
    if not locs:
        await reply(update, "📍 沒有儲存地點。請傳送位置訊息來新增！")
        return
    lines   = ["📍 <b>我的地點</b>\n"]
    buttons = []
    for loc in locs:
        lines.append(f"• {loc.name}")
        buttons.append([
            InlineKeyboardButton(f"📌 {loc.name}", callback_data=f"loc:send:{loc.id}"),
            InlineKeyboardButton("🗑️", callback_data=f"loc:del:{loc.id}"),
        ])
    await reply(update, "\n".join(lines), InlineKeyboardMarkup(buttons))

async def cb_loc_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE, loc_id: int):
    q = update.callback_query
    await q.answer()
    from db import SessionLocal, Location as LocModel
    db  = SessionLocal()
    loc = db.query(LocModel).filter(LocModel.id == loc_id).first()
    db.close()
    if not loc:
        await q.edit_message_text("❌ 找不到該地點。")
        return
    await ctx.bot.send_location(update.effective_chat.id,
                                latitude=loc.latitude, longitude=loc.longitude)
    await ctx.bot.send_message(update.effective_chat.id, f"📍 {loc.name}")

async def cb_loc_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE, loc_id: int):
    q = update.callback_query
    await q.answer()
    from db import SessionLocal, Location as LocModel
    db  = SessionLocal()
    loc = db.query(LocModel).filter(LocModel.id == loc_id).first()
    if loc:
        name = loc.name
        db.delete(loc); db.commit(); db.close()
        await q.edit_message_text(f"🗑️ 地點「{name}」已刪除。")
    else:
        db.close()
        await q.edit_message_text("❌ 找不到該地點。")


# ── 記憶庫（對齊 LINE：多筆結果用按鈕選擇）──────────────────────────────────

MEMORY_HTML_PREFIX = "__TG_MEMORY_HTML__\n"


def format_memory_content(content: str) -> str:
    """
    將記憶內容轉成 Telegram HTML。
    先 escape 使用者輸入，再支援常用捷徑：
    **粗體**、__斜體__、~~刪除線~~、||防劇透||、`等寬`。
    """
    placeholders: list[tuple[str, str]] = []

    def stash(pattern: str, repl):
        nonlocal content
        def _replace(m):
            token = f"\u0000MEMFMT{len(placeholders)}\u0000"
            placeholders.append((token, repl(m)))
            return token
        content = re.sub(pattern, _replace, content, flags=re.S)

    stash(r"```(.+?)```", lambda m: f"<pre>{html.escape(m.group(1).strip())}</pre>")
    stash(r"`([^`\n]+?)`", lambda m: f"<code>{html.escape(m.group(1))}</code>")
    escaped = html.escape(content)
    rules = [
        (r"\*\*(.+?)\*\*", r"<b>\1</b>"),
        (r"__(.+?)__", r"<i>\1</i>"),
        (r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>"),
        (r"(?<!_)_([^_\n]+?)_(?!_)", r"<i>\1</i>"),
        (r"~~(.+?)~~", r"<s>\1</s>"),
        (r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>"),
    ]
    for pattern, replacement in rules:
        escaped = re.sub(pattern, replacement, escaped, flags=re.S)
    for token, value in placeholders:
        escaped = escaped.replace(html.escape(token), value)
    return escaped


def stored_memory_content(content: str) -> str:
    if content.startswith(MEMORY_HTML_PREFIX):
        return content[len(MEMORY_HTML_PREFIX):]
    return format_memory_content(content)


def extract_message_html(update: Update, plain_content: str, prefix: str = "") -> str:
    """
    Telegram 原生格式會放在 message entities 裡；text_html 可保留這些格式。
    若使用者只是手打 **粗體** / ||防劇透||，則走本地格式轉換。
    """
    msg = update.message
    if not msg:
        return format_memory_content(plain_content)

    html_text = getattr(msg, "text_html", None)
    if html_text and msg.entities and (not prefix or html_text.startswith(prefix)):
        return html_text[len(prefix):]

    return format_memory_content(plain_content)


def extract_memory_content_html(update: Update, keyword: str, plain_content: str) -> str:
    return extract_message_html(update, plain_content, f"記住 {keyword} ")


def memory_text(keyword: str, content: str) -> str:
    return f"🧠 <b>{html.escape(keyword)}</b>\n{stored_memory_content(content)}"


def memory_kb(mem_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ 編輯", callback_data=f"mem:edit:{mem_id}"),
            InlineKeyboardButton("🗑️ 刪除", callback_data=f"mem:del:{mem_id}"),
        ],
    ])


async def handle_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id

    if text.startswith("記住"):
        parts = text[2:].strip().split(" ", 1)
        if len(parts) < 2:
            await reply(update,
                "格式：<code>記住 [關鍵字] [內容]</code>\n"
                "內容可用：<code>**粗體**</code>、<code>__斜體__</code>、"
                "<code>~~刪除線~~</code>、<code>||防劇透||</code>、<code>`等寬`</code>")
            return
        kw, content = parts
        kw = kw.strip().replace("\n", "").replace("\r", "")  # 防止換行污染清單
        content_html = extract_memory_content_html(update, kw, content)
        stored_content = MEMORY_HTML_PREFIX + content_html
        existing = query_memory(user_id, kw)
        # 找完全匹配的（query_memory 用 ilike 模糊，這裡再精確比對）
        exact = next((m for m in existing if m.keyword == kw), None)
        if save_memory(user_id, kw, stored_content):
            if exact:
                await reply(update, f"🔄 已更新：<b>{html.escape(kw)}</b>\n{content_html}")
            else:
                await reply(update, f"🧠 已記住：<b>{html.escape(kw)}</b>\n{content_html}")
        else:
            await reply(update, "❌ 儲存失敗。")

    elif text.startswith("查詢"):
        kw      = text[2:].strip()
        if not kw:
            await reply(update, "格式：<code>查詢 [關鍵字]</code>")
            return
        results = query_memory(user_id, kw)
        if not results:
            await reply(update, f"🔍 找不到「{html.escape(kw)}」的記憶。")
        elif len(results) == 1:
            await reply(update, memory_text(results[0].keyword, results[0].content), memory_kb(results[0].id))
        else:
            # 多筆 → 按鈕選擇（對齊 LINE QuickReply 邏輯）
            btns = [[InlineKeyboardButton(m.keyword, callback_data=f"mem:view:{m.id}")]
                    for m in results]
            await reply(update,
                f"🔍 找到 {len(results)} 筆關於「{html.escape(kw)}」的記憶，請選擇：",
                InlineKeyboardMarkup(btns))

    elif text.startswith("忘記"):
        kw = text[2:].strip()
        if forget_memory(user_id, kw):
            await reply(update, f"🗑️ 已忘記「{html.escape(kw)}」。")
        else:
            await reply(update, f"❌ 找不到「{html.escape(kw)}」。")

    elif text == "記憶清單":
        mems = list_memories(user_id)
        if not mems:
            await reply(update, "🧠 記憶庫是空的。")
        else:
            valid = [m for m in mems if m.keyword and m.keyword.strip()]
            lines = ["🧠 <b>記憶清單</b>\n"] + [f"• {html.escape(m.keyword.strip())}" for m in valid]
            await reply(update, "\n".join(lines))

async def cb_mem_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int):
    q = update.callback_query
    await q.answer()
    from db import SessionLocal, Memory as MemModel
    db  = SessionLocal()
    mem = db.query(MemModel).filter(MemModel.id == mem_id).first()
    db.close()
    if mem:
        await q.edit_message_text(memory_text(mem.keyword, mem.content),
                                   parse_mode=ParseMode.HTML,
                                   reply_markup=memory_kb(mem.id))
    else:
        await q.edit_message_text("❌ 找不到。")


async def cb_mem_edit_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int):
    q = update.callback_query
    await q.answer()
    mem = get_memory_by_id(update.effective_user.id, mem_id)
    if not mem:
        await q.edit_message_text("❌ 找不到這筆記憶。")
        return
    user_states[update.effective_user.id] = {
        "action": "edit_memory_content",
        "memory_id": mem_id,
        "keyword": mem.keyword,
    }
    await q.edit_message_text(
        f"✏️ 請輸入「{html.escape(mem.keyword)}」的新內容：\n"
        f"{stored_memory_content(mem.content)}",
        parse_mode=ParseMode.HTML,
    )


async def cb_mem_delete_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int):
    q = update.callback_query
    await q.answer()
    mem = get_memory_by_id(update.effective_user.id, mem_id)
    if not mem:
        await q.edit_message_text("❌ 找不到這筆記憶。")
        return
    await q.edit_message_text(
        f"⚠️ 確定要刪除記憶「{html.escape(mem.keyword)}」？",
        parse_mode=ParseMode.HTML,
        reply_markup=kb([("✅ 確認刪除", f"mem:delok:{mem_id}"), ("❌ 取消", "cancel")]),
    )


async def cb_mem_delete_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int):
    q = update.callback_query
    await q.answer()
    ok = delete_memory_by_id(update.effective_user.id, mem_id)
    await q.edit_message_text("🗑️ 已刪除記憶。" if ok else "❌ 找不到這筆記憶。")


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
                logger.error(f"sticker convert: {e}", exc_info=True)
                try:
                    await status.edit_text(f"❌ 發生錯誤：{e}")
                except Exception:
                    pass
    finally:
        sticker_queue_active.discard(user_id)
        if not queue:
            sticker_queues.pop(user_id, None)


# ── 主訊息 Handler ────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text    = update.message.text.strip()
    user_id = update.effective_user.id
    grp     = is_group(update)

    # 取消
    if text == "取消":
        if user_id in user_states:
            del user_states[user_id]
            await reply(update, "✅ 操作已取消。")
        else:
            await reply(update, "目前沒有進行中的操作。")
        return

    # 狀態機
    if user_id in user_states:
        action = user_states[user_id].get("action")

        if action == "await_location_name":
            state = user_states.pop(user_id)
            if add_location(user_id, text.strip(), state["lat"], state["lng"]):
                await reply(update, f"✅ 地點「{text.strip()}」已儲存！")
            else:
                await reply(update, f"⚠️ 地點「{text.strip()}」已存在。")
            return

        elif action == "setting_city":
            city = text.strip()
            if len(city) < 2 or len(city) > 50:
                await reply(update, "❌ 城市名稱請輸入 2-50 個字。")
                return
            user_states.pop(user_id, None)
            update_user_setting(user_id, city=city)
            await reply(update, f"✅ 地區已更新為：{html.escape(city)}")
            await show_settings(update, ctx)
            return

        elif action == "setting_morning_time":
            time_str = parse_hhmm(text)
            if not time_str:
                await reply(update, "❌ 請輸入 HH:MM，例如 <code>08:00</code>。")
                return
            user_states.pop(user_id, None)
            update_user_setting(user_id, morning_summary_time=time_str, morning_summary_enabled=1)
            await reply(update, f"✅ 今日摘要時間已更新為：{time_str}")
            await show_settings(update, ctx)
            return

        elif action == "setting_evening_time":
            time_str = parse_hhmm(text)
            if not time_str:
                await reply(update, "❌ 請輸入 HH:MM，例如 <code>21:30</code>。")
                return
            user_states.pop(user_id, None)
            update_user_setting(user_id, evening_summary_time=time_str, evening_summary_enabled=1)
            await reply(update, f"✅ 明日預告時間已更新為：{time_str}")
            await show_settings(update, ctx)
            return

        elif action == "setting_snooze":
            snooze_buttons = _parse_snooze_setting_input(text)
            if not snooze_buttons:
                await reply(update, "❌ 請輸入 1-3 個 1-1440 分鐘內的值，例如 <code>5 30 60</code>。")
                return
            user_states.pop(user_id, None)
            update_user_setting(user_id, snooze_buttons=snooze_buttons)
            label = "、".join(f"{m}分" for m in _parse_snooze_setting(snooze_buttons))
            await reply(update, f"✅ 常用延後按鈕已更新：{label}")
            await show_settings(update, ctx)
            return

        elif action == "snooze_custom":
            state = user_states[user_id]
            parsed = parse_snooze_input(text)
            if not parsed:
                await reply(update, "❌ 請輸入 1-1440 分鐘，或 HH:MM，例如 <code>15</code>、<code>14:30</code>。")
                return
            event_id = state["event_id"]
            ev = get_event(event_id)
            if not ev:
                user_states.pop(user_id, None)
                await reply(update, "❌ 找不到事件。")
                return
            new_time, label = parsed
            update_event_fields(event_id, reminder_time=new_time, reminder_sent=0)
            content = ev.event_content
            if not content.startswith("(延)"):
                update_event_content(event_id, f"(延) {content}")
            safe_add_job(send_reminder, new_time, [event_id], f"reminder_{event_id}")
            user_states.pop(user_id, None)
            await reply(update, f"💤 已延後 {label}\n新提醒時間：{new_time.strftime('%Y/%m/%d %H:%M')}")
            return

        elif action == "reminder_custom_time":
            state = user_states[user_id]
            event_id = state["event_id"]
            ev = get_event(event_id)
            if not ev:
                user_states.pop(user_id, None)
                await reply(update, "❌ 找不到事件。")
                return
            event_dt = ev.event_datetime.astimezone(TAIPEI_TZ)
            parsed = parse_custom_reminder_time(text, event_dt)
            if not parsed:
                await reply(update, "❌ 請輸入提前分鐘數，或 HH:MM，例如 <code>45</code>、<code>09:30</code>。")
                return
            reminder_dt, label = parsed
            if reminder_dt <= now_taipei() or reminder_dt > event_dt:
                await reply(update, "⚠️ 自訂提醒時間必須在現在之後，且不能晚於事件時間。")
                return
            update_event_fields(event_id, reminder_time=reminder_dt, reminder_sent=0)
            safe_add_job(send_reminder, reminder_dt, [event_id], f"reminder_{event_id}")
            user_states.pop(user_id, None)
            await reply(update, f"✅ 設定完成！\n將於 {reminder_dt.strftime('%Y/%m/%d %H:%M')}（{label}）提醒您。")
            return

        elif action == "recurring_set_time":
            time_str = parse_hhmm(text)
            if not time_str:
                await reply(update, "❌ 格式錯誤，請輸入 HH:MM，如 09:00")
                return
            user_states[user_id]["time"]   = time_str
            user_states[user_id]["action"] = "recurring_input_content"
            await reply(update, "📝 請輸入提醒事項內容：")
            return

        elif action == "recurring_edit_set_time":
            time_str = parse_hhmm(text)
            if not time_str:
                await reply(update, "❌ 格式錯誤，請輸入 HH:MM，如 09:00")
                return
            state = user_states.pop(user_id)
            event_id = state["event_id"]
            days: set = state.get("days", set())
            days_str = ",".join(sorted(days))
            h, minute = map(int, time_str.split(":"))
            rule_str = f"{days_str}|{time_str}"
            if update_event_fields(event_id, recurrence_rule=rule_str, reminder_time=now_taipei()):
                remove_job(event_id)
                safe_add_cron(send_reminder, [event_id], f"recurring_{event_id}", days_str, h, minute)
                await reply(update, f"✅ 已更新週期提醒時間：每{'、'.join(weekday_names(days))} {time_str}")
            else:
                await reply(update, "❌ 更新失敗。")
            return

        elif action == "recurring_input_content":
            time_str = user_states[user_id].get("time", "09:00")
            await _finish_recurring(update, ctx, user_id, time_str, text.strip())
            return

        elif action == "edit_reminder_content":
            event_id = user_states[user_id]["event_id"]
            original = user_states[user_id]["original"]
            if text.startswith("+") or text.startswith("＋"):
                new_content, mode = f"{original} ({text[1:].strip()})", "補充"
            else:
                new_content, mode = text, "修改"
            del user_states[user_id]
            if update_event_content(event_id, new_content):
                await reply(update, f"✅ 已{mode}提醒：\n{new_content}")
            else:
                await reply(update, "❌ 更新失敗。")
            return

        elif action == "edit_reminder_time":
            event_id = user_states.pop(user_id)["event_id"]
            # 支援：今天/明天 HH:MM、MM/DD HH:MM、YYYY-MM-DD HH:MM
            t = text.strip()
            now = datetime.now(TAIPEI_TZ)
            new_dt = None
            for pat, delta in [("今天", 0), ("明天", 1), ("後天", 2)]:
                m = re.match(rf"^{pat}\s*(\d{{1,2}}):(\d{{2}})$", t)
                if m:
                    new_dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                         second=0, microsecond=0) + timedelta(days=delta)
                    break
            if not new_dt:
                m = re.match(r"^(\d{1,2})[/\-](\d{1,2})\s+(\d{1,2}):(\d{2})$", t)
                if m:
                    mo, dy, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                    yr = now.year if (mo, dy) >= (now.month, now.day) else now.year + 1
                    new_dt = TAIPEI_TZ.localize(datetime(yr, mo, dy, hh, mm))
            if not new_dt:
                m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\s+(\d{1,2}):(\d{2})$", t)
                if m:
                    new_dt = TAIPEI_TZ.localize(datetime(int(m.group(1)), int(m.group(2)),
                                                          int(m.group(3)), int(m.group(4)), int(m.group(5))))
            if not new_dt:
                await reply(update,
                    "❌ 格式錯誤，請輸入如：\n"
                    "<code>今天 14:00</code>　<code>明天 09:30</code>\n"
                    "<code>05/20 18:00</code>　<code>2026-06-01 10:00</code>")
                user_states[user_id] = {"action": "edit_reminder_time", "event_id": event_id}
                return
            if new_dt <= now:
                await reply(update, "❌ 時間必須在現在之後。")
                user_states[user_id] = {"action": "edit_reminder_time", "event_id": event_id}
                return
            update_event_fields(event_id, reminder_time=new_dt, reminder_sent=0)
            safe_add_job(send_reminder, new_dt, [event_id], f"reminder_{event_id}")
            await reply(update, f"✅ 已更新提醒時間：{new_dt.strftime('%Y/%m/%d %H:%M')}")
            return

        elif action == "edit_priority_rule":
            state = user_states.pop(user_id)
            event_id = state["event_id"]
            m = re.match(r"^(\d+)\s+(\d+)$", text.strip())
            if not m:
                await reply(update, "❌ 格式錯誤，請輸入如：<code>15 4</code>")
                user_states[user_id] = state
                return
            interval, repeats = int(m.group(1)), int(m.group(2))
            if interval <= 0 or repeats <= 0 or interval > 1440 or repeats > 50:
                await reply(update, "❌ 分鐘需為 1-1440，次數需為 1-50。")
                user_states[user_id] = state
                return
            if update_event_fields(event_id, recurrence_rule=f"custom:{interval}", remaining_repeats=repeats, reminder_sent=0):
                await reply(update, f"✅ 已更新重要提醒：每 {interval} 分鐘重提，共 {repeats} 次")
            else:
                await reply(update, "❌ 更新失敗。")
            return

        elif action == "edit_tracker_field":
            await handle_tracker_edit_value(update, ctx, user_states, text)
            return

        elif action == "edit_memory_content":
            state = user_states.pop(user_id)
            content_html = extract_message_html(update, text.strip())
            stored_content = MEMORY_HTML_PREFIX + content_html
            if update_memory_by_id(user_id, state["memory_id"], stored_content):
                await reply(update, f"✅ 已更新記憶：<b>{html.escape(state['keyword'])}</b>\n{content_html}")
            else:
                await reply(update, "❌ 更新失敗，找不到這筆記憶。")
            return

    # 固定指令路由
    if text in ("隱藏鍵盤", "⌨️ 隱藏鍵盤", "收起鍵盤", "關閉鍵盤"):
        await cmd_hide_keyboard(update, ctx)
        return
    if text in ("顯示鍵盤", "快捷鍵盤"):
        await update.message.reply_text("⌨️ 已顯示快捷鍵盤。", reply_markup=_REPLY_KB)
        return
    if text in ("Web 清單", "🌐 Web 清單", "網頁清單"):
        await send_web_lists_link(update, ctx)
        return
    if text in ("貼圖轉換", "🎨 貼圖轉換"):
        if user_id in sticker_users:
            sticker_users.discard(user_id)
            await reply(update, "🔴 貼圖轉換模式已關閉。")
        else:
            sticker_users.add(user_id)
            await reply(update,
                "🟢 貼圖轉換模式已開啟！\n"
                "請把 LINE 貼圖商店網址傳給我，例如：\n"
                "https://store.line.me/stickershop/product/XXXXX")
        return
    if "store.line.me" in text or "line.me/S/sticker" in text:
        # 展開手機短網址
        line_url = text.strip()
        if "store.line.me" not in line_url:
            try:
                # HEAD 可能被 LINE 擋，用 GET stream 模式只取 header
                r = requests.get(line_url, allow_redirects=True, timeout=10,
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 stream=True)
                r.close()
                line_url = r.url
                logger.info(f"短網址展開: {text.strip()} → {line_url}")
            except Exception as e:
                logger.warning(f"短網址展開失敗: {e}")
        if "store.line.me" not in line_url:
            await reply(update, "❌ 無法識別此 LINE 網址，請確認是貼圖商店的連結。")
            return
        if user_id not in sticker_users:
            await reply(update, "💡 請先輸入「貼圖轉換」開啟功能，再貼網址。")
            return
        queue = sticker_queues.setdefault(user_id, deque())
        position = len(queue) + (1 if user_id in sticker_queue_active else 0)
        status_text = "🔍 正在抓取 LINE 網頁資料..." if position == 0 else f"⏳ 已加入轉換佇列，目前前面有 {position} 個任務。"
        status = await update.message.reply_text(status_text)
        queue.append((ctx.bot, update.effective_chat.id, line_url, status))
        if user_id not in sticker_queue_active:
            sticker_queue_active.add(user_id)
            asyncio.create_task(run_sticker_queue(user_id))
        return
    # ── Tracker 追蹤功能 ──────────────────────────────────────────────────────
    tracker_text = text.removeprefix("📌 ").removeprefix("💳 ")
    if tracker_text in ("追蹤清單", "訂閱清單", "合約清單", "紀念日清單", "藥物清單"):
        await handle_tracker_list(update, ctx, tracker_text); return
    if tracker_text in ("每月支出", "月費總計", "訂閱費用"):
        await handle_monthly_cost(update, ctx); return
    if text.startswith("刪除追蹤"):
        await handle_tracker_delete(update, ctx, text[4:].strip()); return
    if any(text.startswith(p + " ") or text.startswith(p + "\u3000")
           for p in TRACKER_TRIGGER_MAP):
        await handle_tracker_input(update, ctx, text); return

    if text in ("提醒清單", "📋 提醒清單"):
        await handle_reminder_list(update, ctx); return
    if text in ("設定", "設定中心", "⚙️ 設定中心"):
        await show_settings(update, ctx); return
    if text.startswith("重要提醒"):
        await handle_priority_reminder(update, ctx, text); return
    if text.startswith("提醒"):
        await handle_reminder(update, ctx, text); return
    if text == "週期提醒":
        await handle_recurring(update, ctx); return
    if text in ("地點", "地點清單", "📍 地點清單"):
        await handle_location_list(update, ctx); return
    if text.startswith("找地點"):
        name = text[3:].strip()
        loc  = get_location_by_name(user_id, name)
        if loc:
            await ctx.bot.send_location(update.effective_chat.id,
                                        latitude=loc.latitude, longitude=loc.longitude)
            await reply(update, f"📍 {loc.name}")
        else:
            await reply(update, f"❌ 找不到「{name}」。")
        return
    if text.startswith("刪除地點"):
        name = text[4:].strip()
        if delete_location(user_id, name):
            await reply(update, f"🗑️ 地點「{name}」已刪除。")
        else:
            await reply(update, f"❌ 找不到「{name}」。")
        return
    if any(text.startswith(k) for k in ["記住", "查詢", "忘記"]) or text in ("記憶清單", "🧠 記憶清單"):
        # 把 emoji 前綴去掉再傳入
        clean = text.removeprefix("🧠 ")
        await handle_memory(update, ctx, clean); return
    if text.lower() in ("help", "說明", "幫助", "❓ 說明"):
        await cmd_help(update, ctx); return

    # 群組靜默，私訊才提示
    if not grp:
        await reply(update,
            "🤔 我聽不太懂，您可以試著說：\n"
            "「提醒 我 明天 09:00 開會」\n"
            "或輸入「說明」查看指令。")


# ── Callback 統一入口 ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    try:
        if data == "cancel":
            user_states.pop(update.effective_user.id, None)
            await q.answer("已取消")
            await q.edit_message_text("❌ 操作已取消。")
            return

        parts = data.split(":")

        # ── 主選單按鈕（模擬使用者輸入對應指令）
        if parts[0] == "menu":
            await q.answer()
            cmd = parts[1]
            if cmd == "提醒清單":
                await handle_reminder_list(update, ctx)
            elif cmd == "記憶清單":
                await handle_memory(update, ctx, "記憶清單")
            elif cmd == "地點清單":
                await handle_location_list(update, ctx)
            elif cmd == "設定中心":
                await show_settings(update, ctx)
            elif cmd == "貼圖轉換":
                uid = update.effective_user.id
                if uid in sticker_users:
                    sticker_users.discard(uid)
                    await q.message.reply_text("🔴 貼圖轉換模式已關閉。")
                else:
                    sticker_users.add(uid)
                    await q.message.reply_text(
                        "🟢 貼圖轉換模式已開啟！\n"
                        "請把 LINE 貼圖商店網址傳給我，例如：\n"
                        "https://store.line.me/stickershop/product/XXXXX"
                    )
            elif cmd == "說明":
                await q.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)
            return

        # ── 提醒確認 / 延後
        if parts[0] == "cr":
            await cb_confirm(update, ctx, int(parts[1]))
        elif parts[0] == "sn":
            await cb_snooze(update, ctx, int(parts[1]), int(parts[2]))
        elif parts[0] == "snc":
            await cb_snooze_custom_prompt(update, ctx, int(parts[1]))

        # ── 一般提醒：選提早時間  sr:event_id:minutes
        elif parts[0] == "sr":
            await cb_set_reminder(update, ctx, int(parts[1]), int(parts[2]))
        elif parts[0] == "src":
            await cb_custom_reminder_prompt(update, ctx, int(parts[1]))

        elif parts[0] == "set":
            await handle_settings_callback(update, ctx, parts[1])

        # ── 重要提醒：選提早時間  pe:minutes
        elif parts[0] == "pe":
            await cb_priority_early(update, ctx, int(parts[1]))

        # ── 重要提醒：選等級  pl:level
        elif parts[0] == "pl":
            await cb_priority_level(update, ctx, int(parts[1]))

        # ── 提醒清單操作
        elif parts[0] == "re":
            action = parts[1]
            if action == "page":         await handle_reminder_list(update, ctx, int(parts[2]))
            elif action == "del":        await cb_delete_prompt(update, ctx, int(parts[2]))
            elif action == "delok":      await cb_delete_ok(update, ctx, int(parts[2]))
            elif action == "edit":       await cb_edit_prompt(update, ctx, int(parts[2]))
            elif action == "edit_content": await cb_edit_content_prompt(update, ctx, int(parts[2]))
            elif action == "edit_time":  await cb_edit_time_prompt(update, ctx, int(parts[2]))
            elif action == "edit_priority": await cb_edit_priority_prompt(update, ctx, int(parts[2]))

        # ── 週期提醒
        elif parts[0] == "rec":
            if parts[1] == "toggle":    await cb_rec_toggle(update, ctx, parts[2])
            elif parts[1] == "settime": await cb_rec_settime(update, ctx)

        # ── 追蹤清單操作
        elif parts[0] == "tr":
            action = parts[1]
            if action == "view":
                await handle_tracker_detail(update, ctx, int(parts[2]))
            elif action == "edit":
                await handle_tracker_edit_prompt(update, ctx, user_states, parts[2], int(parts[3]))
            elif action == "notify":
                await handle_tracker_toggle_notify(update, ctx, int(parts[2]))

        # ── 地點
        elif parts[0] == "loc":
            if parts[1] == "send": await cb_loc_send(update, ctx, int(parts[2]))
            elif parts[1] == "del":await cb_loc_del(update, ctx, int(parts[2]))

        # ── 記憶庫
        elif parts[0] == "mem":
            if parts[1] == "view": await cb_mem_view(update, ctx, int(parts[2]))
            elif parts[1] == "edit": await cb_mem_edit_prompt(update, ctx, int(parts[2]))
            elif parts[1] == "del": await cb_mem_delete_prompt(update, ctx, int(parts[2]))
            elif parts[1] == "delok": await cb_mem_delete_ok(update, ctx, int(parts[2]))

        else:
            await q.answer("❓ 未知操作", show_alert=True)

    except Exception as e:
        logger.error(f"Callback error ({data}): {e}", exc_info=True)
        try:
            await q.answer("❌ 發生錯誤", show_alert=True)
        except Exception:
            pass


# ── PTB Application ───────────────────────────────────────────────────────────

def build_ptb_app() -> Application:
    a = Application.builder().token(BOT_TOKEN).build()
    a.add_handler(CommandHandler("start", cmd_start))
    a.add_handler(CommandHandler("help",  cmd_help))
    a.add_handler(CommandHandler("settings", cmd_settings))
    a.add_handler(CommandHandler("hide_keyboard", cmd_hide_keyboard))
    a.add_handler(MessageHandler(filters.LOCATION, handle_location_msg))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    a.add_handler(CallbackQueryHandler(handle_callback))
    return a


# ── Flask routes ──────────────────────────────────────────────────────────────

WEB_LISTS_TEMPLATE = """
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>清單紀錄</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #fff;
      color: #222;
      font: 18px/1.58 Georgia, "Times New Roman", "Noto Serif TC", "PMingLiU", serif;
    }
    main {
      max-width: 720px;
      margin: 0 auto;
      padding: 36px 18px 64px;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 34px;
      line-height: 1.16;
      font-weight: 700;
      letter-spacing: 0;
    }
    .byline {
      color: #8b8b8b;
      font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin-bottom: 30px;
    }
    .toc {
      border-left: 3px solid #e6e6e6;
      margin: 0 0 30px;
      padding: 2px 0 2px 16px;
      font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .toc a {
      color: #444;
      display: inline-block;
      margin-right: 12px;
      text-decoration: none;
    }
    h2 {
      border-top: 1px solid #ededed;
      margin: 32px 0 14px;
      padding-top: 24px;
      font-size: 24px;
      line-height: 1.25;
      letter-spacing: 0;
    }
    h3 {
      margin: 22px 0 8px;
      font-size: 20px;
      line-height: 1.3;
    }
    ul {
      margin: 0 0 18px;
      padding-left: 26px;
    }
    li {
      margin: 0 0 12px;
      overflow-wrap: anywhere;
    }
    .meta {
      color: #777;
      display: block;
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin-top: 2px;
    }
    .memory {
      margin: 0 0 18px;
      overflow-wrap: anywhere;
    }
    .memory-title {
      display: block;
      font-weight: 700;
      margin-bottom: 3px;
    }
    .empty {
      color: #888;
      font-style: italic;
      margin: 0 0 18px;
    }
    @media (max-width: 560px) {
      main { padding-top: 28px; }
      h1 { font-size: 30px; }
      body { font-size: 17px; }
    }
  </style>
</head>
<body>
  <main>
    <h1>清單紀錄</h1>
    <div class="byline">GD牌提醒機器人 · {{ generated_at }}</div>
    <nav class="toc">
      <a href="#reminders">提醒 {{ counts.reminders }}</a>
      <a href="#trackers">追蹤 {{ counts.trackers }}</a>
      <a href="#memories">記憶 {{ counts.memories }}</a>
      <a href="#locations">地點 {{ counts.locations }}</a>
    </nav>

    <section id="reminders">
      <h2>提醒清單</h2>
      {% if reminders %}
        <ul>
        {% for ev in reminders %}
          <li>
            {{ ev.title }}
            <span class="meta">{{ ev.badge }} · {{ ev.time }}</span>
          </li>
        {% endfor %}
        </ul>
      {% else %}<p class="empty">目前沒有進行中的提醒。</p>{% endif %}
    </section>

    <section id="trackers">
      <h2>追蹤清單</h2>
      {% if tracker_groups %}
        {% for group in tracker_groups %}
          <h3>{{ group.label }}</h3>
          <ul>
          {% for item in group.entries %}
            <li>
              {{ item.name }}
              <span class="meta">{{ item.meta }}</span>
              {% if item.notes %}<span class="meta">{{ item.notes }}</span>{% endif %}
            </li>
          {% endfor %}
          </ul>
        {% endfor %}
      {% else %}<p class="empty">追蹤清單是空的。</p>{% endif %}
    </section>

    <section id="memories">
      <h2>記憶清單</h2>
      {% if memories %}
        {% for mem in memories %}
          <p class="memory">
            <span class="memory-title">{{ mem.keyword }}</span>
            {{ mem.content|safe }}
          </p>
        {% endfor %}
      {% else %}<p class="empty">記憶庫是空的。</p>{% endif %}
    </section>

    <section id="locations">
      <h2>地點清單</h2>
      {% if locations %}
        <ul>
        {% for loc in locations %}
          <li>
            {{ loc.name }}
            <span class="meta">{{ "%.6f"|format(loc.latitude) }}, {{ "%.6f"|format(loc.longitude) }}</span>
            {% if loc.address %}<span class="meta">{{ loc.address }}</span>{% endif %}
          </li>
        {% endfor %}
        </ul>
      {% else %}<p class="empty">目前沒有儲存地點。</p>{% endif %}
    </section>
  </main>
</body>
</html>
"""

def _fmt_dt(value):
    if not value:
        return "未設定時間"
    try:
        return value.astimezone(TAIPEI_TZ).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return str(value)

def _web_reminders(user_id):
    events = [
        ev for ev in get_user_events(str(user_id))
        if ev.is_recurring or (not ev.reminder_sent and ev.reminder_time is not None)
    ]
    result = []
    for ev in events:
        if ev.is_recurring:
            days, time_str = parse_recurring_rule(ev.recurrence_rule)
            day_label = "、".join(weekday_names(days)) if days else "週期"
            time_label = f"每{day_label} {time_str}"
            badge = "週期"
        else:
            time_label = _fmt_dt(ev.reminder_time)
            badge = "重要" if ev.priority_level else "提醒"
        result.append({"title": ev.event_content or "(無內容)", "time": time_label, "badge": badge})
    return result

def _web_memories(user_id):
    return [
        {
            "keyword": (m.keyword or "").strip(),
            "content": stored_memory_content(m.content or ""),
        }
        for m in list_memories(user_id)
        if m.keyword and m.keyword.strip()
    ]

def _web_tracker_groups(user_id):
    category_label = {
        "subscription": "💳 訂閱",
        "contract": "📄 合約",
        "anniversary": "🎂 紀念日",
        "medicine": "💊 藥物",
    }
    cycle_label = {"monthly": "每月", "yearly": "每年", "once": "一次"}
    groups = []
    trackers = get_trackers(user_id)
    for category in ("subscription", "contract", "anniversary", "medicine"):
        items = []
        for t in [item for item in trackers if item.category == category]:
            bits = []
            if t.expire_date:
                bits.append(f"到期 {t.expire_date.strftime('%Y/%m/%d')}")
            if t.recurring_month and t.recurring_day:
                bits.append(f"每年 {t.recurring_month:02d}/{t.recurring_day:02d}")
            if t.amount is not None:
                cycle = cycle_label.get(t.cycle, t.cycle or "")
                bits.append(f"{t.amount:.0f} 元{('/' + cycle) if cycle else ''}")
            if t.stock_total and t.stock_daily:
                bits.append(f"庫存 {t.stock_total:g} / 每日 {t.stock_daily:g}")
            if t.remind_days is not None:
                bits.append("不提醒" if t.remind_days < 0 else f"提前 {t.remind_days} 天 {t.remind_time or '08:00'}")
            items.append({
                "name": t.name,
                "badge": category_label.get(t.category, "追蹤").split(" ", 1)[-1],
                "meta": " · ".join(bits) if bits else "未設定細節",
                "notes": t.notes or "",
            })
        if items:
            groups.append({"label": category_label.get(category, category), "entries": items})
    return groups

@app.route("/web/lists/<user_id>", methods=["GET"])
def web_lists(user_id):
    reminders = _web_reminders(user_id)
    memories = _web_memories(user_id)
    tracker_groups = _web_tracker_groups(user_id)
    locations = get_locations(user_id)
    counts = {
        "reminders": len(reminders),
        "memories": len(memories),
        "trackers": sum(len(group["entries"]) for group in tracker_groups),
        "locations": len(locations),
    }
    return render_template_string(
        WEB_LISTS_TEMPLATE,
        generated_at=now_taipei().strftime("%Y/%m/%d %H:%M"),
        counts=counts,
        reminders=reminders,
        memories=memories,
        tracker_groups=tracker_groups,
        locations=locations,
    )

@app.route("/", methods=["GET"])
def root():
    return {"service": "TG Reminder Bot", "status": "running"}, 200

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "scheduler": scheduler.running}, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data   = request.get_json(force=True)
    update = Update.de_json(data, _ptb_app.bot)
    asyncio.run_coroutine_threadsafe(
        _ptb_app.process_update(update), _loop
    ).result(timeout=30)
    return "OK", 200

@app.route("/set_webhook", methods=["GET"])
def set_webhook_route():
    if not WEBHOOK_URL or not BOT_TOKEN:
        return {"error": "WEBHOOK_URL 或 BOT_TOKEN 未設定"}, 400
    try:
        _async(_ptb_app.bot.set_webhook(
            url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        ))
        return {"result": f"✅ Webhook 已設定：{WEBHOOK_URL}"}, 200
    except Exception as e:
        return {"error": str(e)}, 500

@app.route("/webhook_info", methods=["GET"])
def webhook_info():
    try:
        info = _async(_ptb_app.bot.get_webhook_info())
        return {"url": info.url, "pending": info.pending_update_count,
                "last_error": info.last_error_message}, 200
    except Exception as e:
        return {"error": str(e)}, 500


# ── 啟動 ─────────────────────────────────────────────────────────────────────

def start():
    global _loop, _ptb_app

    logger.info("=" * 50)
    logger.info(f"  TELEGRAM_BOT_TOKEN: {'✅' if BOT_TOKEN else '❌ MISSING'}")
    logger.info(f"  DATABASE_URL: {'✅' if os.environ.get('DATABASE_URL') else '❌ MISSING'}")
    logger.info(f"  WEBHOOK_URL: {WEBHOOK_URL or '❌ MISSING'}")
    logger.info("=" * 50)

    init_db()
    safe_start()
    from scheduler import start_daily_summary_scan, start_tracker_scan
    start_daily_summary_scan()
    start_tracker_scan()

    _loop = asyncio.new_event_loop()
    threading.Thread(target=_run_loop, args=(_loop,), daemon=True).start()

    _ptb_app = build_ptb_app()
    _async(_ptb_app.initialize())
    _async(_ptb_app.start())

    if WEBHOOK_URL:
        try:
            _async(_ptb_app.bot.set_webhook(
                url=WEBHOOK_URL,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            ))
            logger.info(f"✅ Webhook 設定成功：{WEBHOOK_URL}")
        except Exception as e:
            logger.warning(f"⚠️ Webhook 設定失敗（可用 /set_webhook 手動補設）：{e}")
    else:
        logger.warning("⚠️ WEBHOOK_URL 未設定")

    logger.info("🤖 Bot 啟動完成")


start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

# bot.py - Telegram AI 智慧管家機器人（LINE 完整移植版）
# python-telegram-bot v20+（async）

import os
import logging
import threading
from datetime import datetime, timedelta

import pytz
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from db import (
    init_db,
    add_event, get_event, get_user_events,
    mark_reminder_sent, update_reminder_time,
    update_event_content, delete_event_by_id,
    add_location, get_locations, get_location_by_name, delete_location,
    save_memory, query_memory, forget_memory, list_memories,
    add_user_card, delete_user_card, get_user_cards,
)
from scheduler import (
    scheduler, safe_start, safe_add_job, safe_add_cron,
    remove_job, send_reminder, TAIPEI_TZ, PRIORITY_RULES,
)
from features.ai_parser import parse_natural_language
from features.credit_card import analyze_best_card

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # https://your-app.fly.dev

# user_states: {user_id: {"action": str, ...}}
user_states: dict[int, dict] = {}

# ── Keyboards ─────────────────────────────────────────────────────────────────

def kb(*rows) -> InlineKeyboardMarkup:
    """快速建立 InlineKeyboard，rows 是 List[List[(text, data)]]"""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def recurring_kb(selected: set[str]) -> InlineKeyboardMarkup:
    """週期提醒星期選擇鍵盤"""
    row1 = []
    for code, name in zip(WEEKDAY_CODES[:4], WEEKDAY_NAMES[:4]):
        label = f"✅{name}" if code in selected else name
        row1.append(InlineKeyboardButton(label, callback_data=f"rec:toggle:{code}"))
    row2 = []
    for code, name in zip(WEEKDAY_CODES[4:], WEEKDAY_NAMES[4:]):
        label = f"✅{name}" if code in selected else name
        row2.append(InlineKeyboardButton(label, callback_data=f"rec:toggle:{code}"))
    row3 = [InlineKeyboardButton("⏰ 設定時間", callback_data="rec:settime")]
    row4 = [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
    return InlineKeyboardMarkup([row1, row2, row3, row4])


def reminder_list_kb(events: list, page: int = 0, page_size: int = 5) -> tuple[str, InlineKeyboardMarkup]:
    """提醒清單（分頁）"""
    total = len(events)
    start = page * page_size
    chunk = events[start:start + page_size]

    lines = [f"📋 <b>提醒清單</b> ({total} 筆)\n"]
    buttons = []
    for ev in chunk:
        if ev.is_recurring:
            t = f"🔁 {ev.event_content} [{ev.recurrence_rule}]"
        else:
            rt = ev.reminder_time.astimezone(TAIPEI_TZ)
            snoozing = ev.reminder_time != ev.event_datetime
            icon = "💤" if snoozing else "⏰"
            t = f"{icon} {rt.strftime('%m/%d %H:%M')} {ev.event_content}"
        lines.append(t)
        buttons.append([
            InlineKeyboardButton(f"✏️ {ev.event_content[:10]}", callback_data=f"re:edit:{ev.id}"),
            InlineKeyboardButton("🗑️", callback_data=f"re:del:{ev.id}"),
        ])

    # 分頁按鈕
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上頁", callback_data=f"re:page:{page-1}"))
    if start + page_size < total:
        nav.append(InlineKeyboardButton("下頁 ▶", callback_data=f"re:page:{page+1}"))
    if nav:
        buttons.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_group(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup")


def chat_type(update: Update) -> str:
    t = update.effective_chat.type
    return "group" if t in ("group", "supergroup") else "private"


async def reply(update: Update, text: str,
                keyboard=None, parse_mode=ParseMode.HTML):
    kwargs = {"parse_mode": parse_mode}
    if keyboard:
        kwargs["reply_markup"] = keyboard
    if update.callback_query:
        await update.callback_query.message.reply_text(text, **kwargs)
    else:
        await update.message.reply_text(text, **kwargs)


def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


def parse_datetime_text(text: str) -> datetime | None:
    """解析『日期 時間』格式，例如 '2025-12-25 09:00' 或 '12/25 09:00'"""
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%m/%d %H:%M",
        "%m-%d %H:%M",
        "%H:%M",  # 今天的時間
    ]
    now = now_taipei()
    for fmt in formats:
        try:
            dt = datetime.strptime(text.strip(), fmt)
            # 補年月
            if fmt == "%H:%M":
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            elif fmt in ("%m/%d %H:%M", "%m-%d %H:%M"):
                dt = dt.replace(year=now.year)
            return TAIPEI_TZ.localize(dt)
        except ValueError:
            continue
    return None


# ── /start & /help ─────────────────────────────────────────────────────────────

HELP_TEXT = """🤖 <b>AI 智慧管家</b>

<b>📅 提醒功能</b>
<code>提醒 [誰] [日期] [時間] [事件]</code>
<code>重要提醒 [誰] [日期] [時間] [事件]</code>
<code>週期提醒</code> — 設定每週重複
<code>提醒清單</code> — 管理所有提醒

<b>📍 地點功能</b>
<code>地點</code> / <code>地點清單</code> — 查看儲存地點
<code>找地點 [名稱]</code> — 回傳位置
<code>刪除地點 [名稱]</code>
（傳送 Telegram 位置訊息可儲存）

<b>🧠 記憶功能</b>
<code>記住 [關鍵字] [內容]</code>
<code>查詢 [關鍵字]</code>
<code>忘記 [關鍵字]</code>
<code>記憶清單</code>

<b>💳 信用卡小幫手</b>
<code>新增卡片 [名稱]</code>
<code>刪除卡片 [名稱]</code>
<code>我的卡包</code>
<code>刷 [商家]</code> — AI 推薦最佳刷卡

<b>通用</b>
<code>取消</code> — 中斷目前操作"""


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, f"👋 歡迎使用 AI 智慧管家！\n\n{HELP_TEXT}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_TEXT)


# ── 提醒指令 ──────────────────────────────────────────────────────────────────

def _parse_reminder_command(text: str) -> tuple[str | None, str | None, str | None]:
    """解析『提醒 [誰] [日期] [時間] [事件]』
    回傳 (display_name, datetime_str, content) 或 (None, None, None)"""
    import re
    # 格式：提醒 誰 YYYY-MM-DD HH:MM 事件
    # 也支援：提醒 誰 MM/DD HH:MM 事件
    pattern = r"^(?:提醒|重要提醒)\s+(\S+)\s+(\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[/-]\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+)$"
    m = re.match(pattern, text)
    if m:
        who, date_str, time_str, content = m.groups()
        dt_str = f"{date_str} {time_str}".replace("/", "-")
        return who, dt_str, content
    return None, None, None


async def handle_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    is_priority = text.startswith("重要提醒")

    who, dt_str, content = _parse_reminder_command(text)
    if not (who and dt_str and content):
        await reply(update,
                    "格式：<code>提醒 [誰] [日期] [時間] [事件]</code>\n"
                    "例：<code>提醒 我 2025-08-01 09:00 開會</code>")
        return

    dt = parse_datetime_text(f"{dt_str}")
    if dt is None:
        await reply(update, "❌ 時間格式無法解析，請用 YYYY-MM-DD HH:MM 格式。")
        return
    if dt <= now_taipei():
        await reply(update, "❌ 設定的時間已經過了！")
        return

    display = update.effective_user.first_name or who
    chat_id = str(update.effective_chat.id)
    ctype = chat_type(update)

    priority = 0
    remaining = 0
    if is_priority:
        # 先問優先等級
        user_states[user_id] = {
            "action": "set_priority",
            "who": who, "dt": dt, "content": content,
            "chat_id": chat_id, "ctype": ctype, "display": display,
        }
        await reply(update, "❗ 請選擇重要程度：",
                    kb([("🟢 低（30分重提）", "prio:1"),
                        ("🟡 中（10分重提）", "prio:2")],
                       [("🔴 高（5分重提）",  "prio:3")]))
        return

    event_id = add_event(
        creator_user_id=user_id,
        target_id=chat_id,
        target_type=ctype,
        display_name=display,
        content=content,
        event_datetime=dt,
    )
    if not event_id:
        await reply(update, "❌ 建立提醒失敗，請稍後再試。")
        return

    safe_add_job(send_reminder, dt, [event_id], f"reminder_{event_id}")
    await reply(update,
                f"✅ 提醒已設定！\n\n"
                f"👤 {who}\n📅 {dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}")


async def handle_priority_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str):
    """處理重要提醒優先等級選擇"""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    if state.get("action") != "set_priority":
        return

    level = int(data.split(":")[1])  # prio:1
    rule = PRIORITY_RULES[level]

    dt = state["dt"]
    content = state["content"]
    display = state["display"]
    chat_id = state["chat_id"]
    ctype = state["ctype"]

    event_id = add_event(
        creator_user_id=user_id,
        target_id=chat_id,
        target_type=ctype,
        display_name=display,
        content=content,
        event_datetime=dt,
        priority_level=level,
        remaining_repeats=rule["repeats"],
    )
    del user_states[user_id]

    if not event_id:
        await query.edit_message_text("❌ 建立失敗，請稍後再試。")
        return

    safe_add_job(send_reminder, dt, [event_id], f"reminder_{event_id}")
    icon = rule["icon"]
    await query.edit_message_text(
        f"{icon} 重要提醒已設定！\n\n"
        f"👤 {display}\n📅 {dt.strftime('%Y/%m/%d %H:%M')}\n"
        f"📝 {content}\n⏱️ 未確認將每 {rule['interval']} 分鐘重提")


async def handle_reminder_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    user_id = update.effective_user.id
    events = get_user_events(str(user_id))
    if not events:
        await reply(update, "📋 目前沒有任何提醒。")
        return
    text, markup = reminder_list_kb(events, page)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await reply(update, text, markup)


# ── 提醒 Callback（確認/延後/刪除/編輯）─────────────────────────────────────────

async def cb_confirm_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    query = update.callback_query
    await query.answer("✅ 已確認！")
    ev = get_event(event_id)
    if ev:
        mark_reminder_sent(event_id)
        remove_job(event_id)
        if ev.priority_level > 0:
            delete_event_by_id(event_id, str(update.effective_user.id))
    await query.edit_message_text("✅ 提醒已確認，任務完成！")


async def cb_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int, minutes: int):
    query = update.callback_query
    await query.answer(f"💤 延後 {minutes} 分鐘")
    new_time = now_taipei() + timedelta(minutes=minutes)
    update_reminder_time(event_id, new_time)
    safe_add_job(send_reminder, new_time, [event_id], f"reminder_{event_id}")
    await query.edit_message_text(
        f"💤 已延後 {minutes} 分鐘\n"
        f"⏰ 新提醒時間：{new_time.strftime('%H:%M')}")


async def cb_delete_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    # 確認提示
    await query.edit_message_text(
        "⚠️ 確定要刪除此提醒？",
        reply_markup=kb(
            [("✅ 確認刪除", f"re:delok:{event_id}"), ("❌ 取消", "cancel")]
        )
    )


async def cb_delete_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    ev = get_event(event_id)
    if ev:
        remove_job(event_id)
        delete_event_by_id(event_id, str(user_id))
        await query.edit_message_text("🗑️ 提醒已刪除。")
    else:
        await query.edit_message_text("❌ 找不到該提醒。")


async def cb_edit_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    ev = get_event(event_id)
    if not ev:
        await query.edit_message_text("❌ 找不到該提醒。")
        return
    user_states[user_id] = {"action": "edit_reminder_content", "event_id": event_id,
                             "original": ev.event_content}
    await query.edit_message_text(
        f"✏️ 請輸入新的提醒內容：\n（目前：{ev.event_content}）\n\n"
        f"💡 以 <code>+</code> 開頭可補充而非覆蓋")


# ── 週期提醒 ─────────────────────────────────────────────────────────────────

async def handle_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = {"action": "recurring_select_days", "days": set()}
    await reply(update, "🔁 週期提醒設定\n\n請選擇要提醒的星期（可多選）：",
                recurring_kb(set()))


async def cb_recurring_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE, day: str):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = user_states.setdefault(user_id, {"action": "recurring_select_days", "days": set()})
    days: set = state.get("days", set())
    if day in days:
        days.discard(day)
    else:
        days.add(day)
    state["days"] = days
    await query.edit_message_text("🔁 週期提醒設定\n\n請選擇要提醒的星期（可多選）：",
                                  reply_markup=recurring_kb(days))


async def cb_recurring_settime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    days = state.get("days", set())
    if not days:
        await query.answer("⚠️ 請至少選擇一天！", show_alert=True)
        return
    state["action"] = "recurring_set_time"
    await query.edit_message_text("⏰ 請輸入提醒時間（格式：HH:MM，如 09:00）：")


async def handle_recurring_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                 user_id: int, text: str):
    """使用者輸入週期提醒時間"""
    state = user_states.get(user_id, {})
    import re
    m = re.match(r"^(\d{1,2}):(\d{2})$", text.strip())
    if not m:
        await reply(update, "❌ 格式錯誤，請輸入 HH:MM，如 09:00")
        return
    h, minute = int(m.group(1)), int(m.group(2))
    state["time"] = f"{h:02d}:{minute:02d}"
    state["action"] = "recurring_input_content"
    await reply(update, "📝 請輸入提醒事項內容：")


async def handle_recurring_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                    user_id: int, text: str):
    """使用者輸入週期提醒內容，完成設定"""
    state = user_states.pop(user_id, {})
    days: set = state.get("days", set())
    time_str: str = state.get("time", "09:00")
    content = text.strip()

    days_str = ",".join(sorted(days))  # "mon,wed"
    rule = f"{days_str}|{time_str}"
    h, minute = map(int, time_str.split(":"))
    display = update.effective_user.first_name or "您"
    chat_id = str(update.effective_chat.id)

    event_id = add_event(
        creator_user_id=user_id,
        target_id=chat_id,
        target_type=chat_type(update),
        display_name=display,
        content=content,
        event_datetime=now_taipei(),
        is_recurring=1,
        recurrence_rule=rule,
    )
    if not event_id:
        await reply(update, "❌ 建立失敗，請稍後再試。")
        return

    safe_add_cron(send_reminder, [event_id], f"recurring_{event_id}",
                  days_str, h, minute)

    day_names = [WEEKDAY_NAMES[WEEKDAY_CODES.index(d)] for d in sorted(days) if d in WEEKDAY_CODES]
    await reply(update,
                f"✅ 週期提醒已設定！\n\n"
                f"📆 每{'/'.join(day_names)} {time_str}\n"
                f"📝 {content}")


# ── 地點功能 ─────────────────────────────────────────────────────────────────

async def handle_location_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """收到位置訊息，進入等待命名狀態"""
    user_id = update.effective_user.id
    loc = update.message.location
    user_states[user_id] = {
        "action": "await_location_name",
        "lat": loc.latitude,
        "lng": loc.longitude,
    }
    await update.message.reply_text(
        "📍 收到位置！\n請輸入此地點的名稱（如：公司、家）：",
        reply_markup=ReplyKeyboardRemove()
    )


async def handle_save_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                user_id: int, text: str):
    state = user_states.pop(user_id, {})
    name = text.strip()
    ok = add_location(user_id, name, state["lat"], state["lng"])
    if ok:
        await reply(update, f"✅ 地點「{name}」已儲存！")
    else:
        await reply(update, f"⚠️ 地點「{name}」已存在。")


async def handle_location_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    locs = get_locations(user_id)
    if not locs:
        await reply(update, "📍 還沒有儲存任何地點。\n請傳送 Telegram 位置訊息來新增！")
        return
    lines = ["📍 <b>我的地點</b>\n"]
    buttons = []
    for loc in locs:
        lines.append(f"• {loc.name}")
        buttons.append([
            InlineKeyboardButton(f"📌 {loc.name}", callback_data=f"loc:send:{loc.id}"),
            InlineKeyboardButton("🗑️", callback_data=f"loc:del:{loc.name[:20]}"),
        ])
    await reply(update, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cb_location_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE, loc_id: int):
    query = update.callback_query
    await query.answer()
    from db import SessionLocal, Location
    db = SessionLocal()
    loc = db.query(Location).filter(Location.id == loc_id).first()
    db.close()
    if not loc:
        await query.edit_message_text("❌ 找不到該地點。")
        return
    await ctx.bot.send_location(
        chat_id=update.effective_chat.id,
        latitude=loc.latitude,
        longitude=loc.longitude,
    )
    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📍 {loc.name}"
    )


async def cb_location_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE, name: str):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    ok = delete_location(user_id, name)
    if ok:
        await query.edit_message_text(f"🗑️ 地點「{name}」已刪除。")
    else:
        await query.edit_message_text("❌ 找不到該地點。")


# ── 記憶庫 ──────────────────────────────────────────────────────────────────

async def handle_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id

    if text.startswith("記住"):
        rest = text[2:].strip()
        parts = rest.split(" ", 1)
        if len(parts) < 2:
            await reply(update, "格式：<code>記住 [關鍵字] [內容]</code>")
            return
        kw, content = parts
        if save_memory(user_id, kw, content):
            await reply(update, f"🧠 已記住：\n<b>{kw}</b> → {content}")
        else:
            await reply(update, "❌ 儲存失敗。")

    elif text.startswith("查詢"):
        kw = text[2:].strip()
        results = query_memory(user_id, kw)
        if not results:
            await reply(update, f"🔍 找不到「{kw}」的記憶。")
        elif len(results) == 1:
            await reply(update, f"🧠 <b>{results[0].keyword}</b>\n{results[0].content}")
        else:
            # 多筆結果 → 列表選擇
            lines = ["🔍 找到多筆記憶，請選擇："]
            btns = [[InlineKeyboardButton(m.keyword, callback_data=f"mem:view:{m.keyword}")]
                    for m in results]
            await reply(update, "\n".join(lines), InlineKeyboardMarkup(btns))

    elif text.startswith("忘記"):
        kw = text[2:].strip()
        if forget_memory(user_id, kw):
            await reply(update, f"🗑️ 已忘記「{kw}」。")
        else:
            await reply(update, f"❌ 找不到「{kw}」。")

    elif text == "記憶清單":
        mems = list_memories(user_id)
        if not mems:
            await reply(update, "🧠 記憶庫是空的。")
        else:
            lines = ["🧠 <b>記憶清單</b>\n"] + [f"• {m.keyword}" for m in mems]
            await reply(update, "\n".join(lines))


async def cb_memory_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE, keyword: str):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    results = query_memory(user_id, keyword)
    for m in results:
        if m.keyword == keyword:
            await query.edit_message_text(f"🧠 <b>{m.keyword}</b>\n{m.content}",
                                          parse_mode=ParseMode.HTML)
            return
    await query.edit_message_text("❌ 找不到。")


# ── 信用卡 ──────────────────────────────────────────────────────────────────

async def handle_credit_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id

    if text.startswith("新增卡片"):
        name = text[4:].strip()
        if not name:
            await reply(update, "格式：<code>新增卡片 [名稱]</code>")
            return
        if add_user_card(user_id, name):
            await reply(update, f"💳 已新增卡片：{name}")
        else:
            await reply(update, f"⚠️ 「{name}」已存在。")

    elif text.startswith("刪除卡片"):
        name = text[4:].strip()
        if delete_user_card(user_id, name):
            await reply(update, f"🗑️ 已刪除：{name}")
        else:
            await reply(update, f"❌ 找不到卡片「{name}」。")

    elif text == "我的卡包":
        cards = get_user_cards(user_id)
        if cards:
            lines = "\n".join([f"💳 {c}" for c in cards])
            await reply(update, f"💳 <b>我的卡包</b>\n\n{lines}")
        else:
            await reply(update, "💳 還沒有任何卡片。\n請輸入：<code>新增卡片 [名稱]</code>")

    elif text.startswith("刷 ") or text.startswith("刷"):
        merchant = text.lstrip("刷").strip()
        if not merchant:
            await reply(update, "格式：<code>刷 [商家名稱]</code>")
            return
        msg = await reply(update, f"🔍 正在查詢「{merchant}」最佳刷卡策略…")
        # 在 executor 裡跑（避免 block event loop）
        result = await ctx.application.loop.run_in_executor(
            None, analyze_best_card, str(user_id), merchant
        )
        await update.effective_message.reply_text(result, parse_mode=ParseMode.HTML)


# ── 主訊息 Handler ────────────────────────────────────────────────────────────

TIME_KEYWORDS = ["明天", "後天", "今天", "下週", "下周", "禮拜", "星期",
                 "點", "分", "早上", "下午", "晚上", "中午", "半",
                 "提醒", "幫我", "記得", "後"]


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    is_grp = is_group(update)

    # ── 1. 取消 ────────────────────────────────────────────────────────────
    if text == "取消":
        if user_id in user_states:
            del user_states[user_id]
            await reply(update, "✅ 操作已取消。")
        else:
            await reply(update, "目前沒有進行中的操作。")
        return

    # ── 2. 狀態機（進行中的流程）──────────────────────────────────────────
    if user_id in user_states:
        state = user_states[user_id]
        action = state.get("action")

        if action == "await_location_name":
            await handle_save_location(update, ctx, user_id, text)
            return
        elif action == "recurring_set_time":
            await handle_recurring_time(update, ctx, user_id, text)
            return
        elif action == "recurring_input_content":
            await handle_recurring_content(update, ctx, user_id, text)
            return
        elif action == "edit_reminder_content":
            event_id = state["event_id"]
            original = state["original"]
            if text.startswith("+") or text.startswith("＋"):
                new_content = f"{original} ({text[1:].strip()})"
                mode = "補充"
            else:
                new_content = text
                mode = "修改"
            if update_event_content(event_id, new_content):
                del user_states[user_id]
                await reply(update, f"✅ 已{mode}提醒內容：\n{new_content}")
            else:
                await reply(update, "❌ 更新失敗。")
            return

    # ── 3. 信用卡指令 ───────────────────────────────────────────────────────
    if any(text.startswith(kw) for kw in ["新增卡片", "刪除卡片", "我的卡包", "刷 ", "刷"]):
        await handle_credit_card(update, ctx, text)
        return

    # ── 4. 提醒指令 ─────────────────────────────────────────────────────────
    if text == "提醒清單":
        await handle_reminder_list(update, ctx)
        return
    if text.startswith("重要提醒"):
        await handle_reminder(update, ctx, text)
        return
    if text.startswith("提醒"):
        await handle_reminder(update, ctx, text)
        return
    if text == "週期提醒":
        await handle_recurring(update, ctx)
        return

    # ── 5. 地點指令 ─────────────────────────────────────────────────────────
    if text in ("地點", "地點清單"):
        await handle_location_list(update, ctx)
        return
    if text.startswith("找地點"):
        name = text[3:].strip()
        loc = get_location_by_name(user_id, name)
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

    # ── 6. 記憶指令 ─────────────────────────────────────────────────────────
    if any(text.startswith(kw) for kw in ["記住", "查詢", "忘記"]) or text == "記憶清單":
        await handle_memory(update, ctx, text)
        return

    # ── 7. 說明 ─────────────────────────────────────────────────────────────
    if text.lower() in ("help", "說明", "幫助"):
        await cmd_help(update, ctx)
        return

    # ── 8. AI 自然語言解析（僅含時間關鍵字才觸發）────────────────────────────
    has_time_hint = any(k in text for k in TIME_KEYWORDS) or any(c.isdigit() for c in text)
    if len(text) > 1 and has_time_hint:
        try:
            cur_time = now_taipei().strftime("%Y-%m-%d %H:%M:%S")
            result = await ctx.application.loop.run_in_executor(
                None, parse_natural_language, text, cur_time
            )
            if result:
                from datetime import datetime as dt_cls
                naive = dt_cls.strptime(result["event_datetime"], "%Y-%m-%d %H:%M")
                event_dt = TAIPEI_TZ.localize(naive)

                if event_dt <= now_taipei():
                    await reply(update, "😅 AI 解析出的時間已過，請再說一次。")
                    return

                display = update.effective_user.first_name or "您"
                chat_id = str(update.effective_chat.id)
                content = result["event_content"]

                event_id = add_event(
                    creator_user_id=user_id,
                    target_id=chat_id,
                    target_type=chat_type(update),
                    display_name=display,
                    content=content,
                    event_datetime=event_dt,
                )
                if event_id:
                    safe_add_job(send_reminder, event_dt, [event_id], f"reminder_{event_id}")
                    await reply(update,
                                f"🤖 AI 已幫您設定提醒！\n\n"
                                f"📅 {event_dt.strftime('%Y/%m/%d %H:%M')}\n"
                                f"📝 {content}",
                                kb([("✅ 確認", f"cr:{event_id}"),
                                    ("🗑️ 刪除", f"re:del:{event_id}")]))
                    return
        except Exception as e:
            logger.error(f"AI parse failed: {e}")

    # ── 9. 群組靜默 / 私訊才回饋 ─────────────────────────────────────────────
    if not is_grp:
        await reply(update, "🤔 我聽不懂，請輸入「說明」查看指令列表。")


# ── Callback Query Handler（統一入口）──────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    try:
        # cancel
        if data == "cancel":
            user_id = update.effective_user.id
            user_states.pop(user_id, None)
            await query.answer("已取消")
            await query.edit_message_text("❌ 操作已取消。")
            return

        parts = data.split(":")

        # ── 提醒確認 / 延後 cr:id  sn:id:min ───────────────────────────────
        if parts[0] == "cr":
            await cb_confirm_reminder(update, ctx, int(parts[1]))

        elif parts[0] == "sn":
            await cb_snooze(update, ctx, int(parts[1]), int(parts[2]))

        # ── 重要提醒優先等級 prio:level ──────────────────────────────────────
        elif parts[0] == "prio":
            await handle_priority_callback(update, ctx, data)

        # ── 提醒清單操作 re:action:id ────────────────────────────────────────
        elif parts[0] == "re":
            action = parts[1]
            if action == "page":
                await handle_reminder_list(update, ctx, int(parts[2]))
            elif action == "del":
                await cb_delete_reminder(update, ctx, int(parts[2]))
            elif action == "delok":
                await cb_delete_ok(update, ctx, int(parts[2]))
            elif action == "edit":
                await cb_edit_reminder(update, ctx, int(parts[2]))

        # ── 週期提醒 rec:action[:day] ────────────────────────────────────────
        elif parts[0] == "rec":
            action = parts[1]
            if action == "toggle":
                await cb_recurring_toggle(update, ctx, parts[2])
            elif action == "settime":
                await cb_recurring_settime(update, ctx)

        # ── 地點 loc:action:id_or_name ───────────────────────────────────────
        elif parts[0] == "loc":
            action = parts[1]
            if action == "send":
                await cb_location_send(update, ctx, int(parts[2]))
            elif action == "del":
                await cb_location_delete(update, ctx, parts[2])

        # ── 記憶庫 mem:view:keyword ──────────────────────────────────────────
        elif parts[0] == "mem":
            if parts[1] == "view":
                await cb_memory_view(update, ctx, parts[2])

        else:
            await query.answer("❓ 未知操作", show_alert=True)

    except Exception as e:
        logger.error(f"Callback error ({data}): {e}", exc_info=True)
        try:
            await query.answer("❌ 發生錯誤", show_alert=True)
        except Exception:
            pass


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    import asyncio

    # 環境變數檢查
    print("=" * 50)
    for key in ["TELEGRAM_BOT_TOKEN", "DATABASE_URL", "GOOGLE_API_KEY",
                "GOOGLE_SEARCH_ENGINE_ID", "WEBHOOK_URL"]:
        val = os.environ.get(key)
        status = f"✅ 存在（長度 {len(val)}）" if val else "❌ 缺少！"
        print(f"{key}: {status}")
    print("=" * 50)

    # DB 初始化
    init_db()

    # 排程器啟動
    safe_start()

    # PTB Application
    app = Application.builder().token(BOT_TOKEN).build()

    # 指令 handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))

    # 位置訊息
    app.add_handler(MessageHandler(filters.LOCATION, handle_location_message))

    # 文字訊息
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Callback（按鈕點擊）
    app.add_handler(CallbackQueryHandler(handle_callback))

    # 啟動 webhook 或 polling（本機開發用 polling，Fly.io 用 webhook）
    if WEBHOOK_URL:
        logger.info(f"Starting webhook at {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 8080)),
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            secret_token=os.environ.get("WEBHOOK_SECRET", ""),
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting polling (local dev)...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

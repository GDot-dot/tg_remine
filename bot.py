# bot.py - Telegram 智慧管家（Flask + Gunicorn，對齊原 LINE 專案架構）
#
# 架構：Flask 處理 HTTP（Gunicorn 啟動），PTB async 跑在背景 event loop thread

import os
import re
import threading
import asyncio
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort

import pytz
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove,
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
)
from scheduler import (
    scheduler, safe_start, safe_add_job, safe_add_cron,
    remove_job, send_reminder, TAIPEI_TZ, PRIORITY_RULES,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")  # 完整含路徑，如 https://xxx.fly.dev/bot
PORT        = int(os.environ.get("PORT", 8080))

app = Flask(__name__)

# ── PTB async 跑在獨立 thread ─────────────────────────────────────────────────
_loop: asyncio.AbstractEventLoop = None
_ptb_app: Application = None
user_states: dict[int, dict] = {}


def _run_event_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _async(coro):
    """在 PTB event loop 裡執行 coroutine（供 Flask sync thread 呼叫）"""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=30)


# ── 鍵盤工具 ─────────────────────────────────────────────────────────────────

def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )

WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


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
            prefix   = "(延) " if snoozing else ""
            line     = f"{icon} {rt.strftime('%m/%d %H:%M')} {prefix}{ev.event_content}"
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


# ── 工具函式 ─────────────────────────────────────────────────────────────────

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

def parse_dt(text: str) -> datetime | None:
    formats = ["%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m/%d %H:%M", "%m-%d %H:%M", "%H:%M"]
    now = now_taipei()
    for fmt in formats:
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if fmt == "%H:%M":
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            elif fmt in ("%m/%d %H:%M", "%m-%d %H:%M"):
                dt = dt.replace(year=now.year)
            return TAIPEI_TZ.localize(dt)
        except ValueError:
            continue
    return None


# ── /start & /help ────────────────────────────────────────────────────────────

HELP_TEXT = """🤖 <b>Telegram 智慧管家</b>

<b>📅 提醒功能</b>
<code>提醒 [誰] [日期] [時間] [事件]</code>
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

<b>通用</b>：<code>取消</code> — 中斷操作"""

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, f"👋 歡迎使用 Telegram 智慧管家！\n\n{HELP_TEXT}")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, HELP_TEXT)


# ── 提醒 ─────────────────────────────────────────────────────────────────────

def _parse_reminder_text(text: str):
    pat = r"^(?:提醒|重要提醒)\s+(\S+)\s+(\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[/-]\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+)$"
    m = re.match(pat, text)
    if not m:
        return None, None, None
    who, date_s, time_s, content = m.groups()
    dt = parse_dt(f"{date_s.replace('/','-')} {time_s}")
    return who, dt, content

async def handle_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id     = update.effective_user.id
    is_priority = text.startswith("重要提醒")
    who, dt, content = _parse_reminder_text(text)
    if not (who and dt and content):
        await reply(update,
            "格式：<code>提醒 [誰] [日期] [時間] [事件]</code>\n"
            "例：<code>提醒 我 2025-08-01 09:00 開會</code>")
        return
    if dt <= now_taipei():
        await reply(update, "❌ 設定的時間已經過了！")
        return
    display = update.effective_user.first_name or who
    chat_id = str(update.effective_chat.id)
    ctype   = chat_type(update)
    if is_priority:
        user_states[user_id] = {
            "action": "set_priority",
            "who": who, "dt": dt, "content": content,
            "chat_id": chat_id, "ctype": ctype, "display": display,
        }
        await reply(update, "❗ 請選擇重要程度：",
            kb([("🟢 低（30分重提）", "prio:1"), ("🟡 中（10分重提）", "prio:2")],
               [("🔴 高（5分重提）", "prio:3")]))
        return
    event_id = add_event(creator_user_id=user_id, target_id=chat_id,
                         target_type=ctype, display_name=display,
                         content=content, event_datetime=dt)
    if not event_id:
        await reply(update, "❌ 建立提醒失敗。")
        return
    safe_add_job(send_reminder, dt, [event_id], f"reminder_{event_id}")
    await reply(update,
        f"✅ 提醒已設定！\n\n👤 {who}\n📅 {dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}")

async def handle_reminder_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    user_id = update.effective_user.id
    events  = get_user_events(str(user_id))
    if not events:
        await reply(update, "📋 目前沒有任何提醒。")
        return
    text, markup = reminder_list_kb(events, page)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await reply(update, text, markup)

async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int):
    q = update.callback_query
    await q.answer("✅ 已確認！")
    ev = get_event(event_id)
    if ev:
        mark_reminder_sent(event_id)
        remove_job(event_id)
        if ev.priority_level > 0:
            delete_event_by_id(event_id, str(update.effective_user.id))
    await q.edit_message_text("✅ 提醒已確認，任務完成！")

async def cb_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE, event_id: int, minutes: int):
    q = update.callback_query
    await q.answer(f"💤 延後 {minutes} 分鐘")
    new_time = now_taipei() + timedelta(minutes=minutes)
    update_reminder_time(event_id, new_time)
    safe_add_job(send_reminder, new_time, [event_id], f"reminder_{event_id}")
    await q.edit_message_text(f"💤 已延後 {minutes} 分鐘\n新提醒時間：{new_time.strftime('%H:%M')}")

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
    user_states[update.effective_user.id] = {
        "action": "edit_reminder_content",
        "event_id": event_id, "original": ev.event_content,
    }
    await q.edit_message_text(
        f"✏️ 請輸入新的提醒內容：\n（目前：{ev.event_content}）\n\n"
        "💡 以 <code>+</code> 開頭可補充而非覆蓋", parse_mode=ParseMode.HTML)

async def cb_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE, level: int):
    q       = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    state   = user_states.get(user_id, {})
    if state.get("action") != "set_priority":
        return
    rule    = PRIORITY_RULES[level]
    dt, content, display = state["dt"], state["content"], state["display"]
    chat_id, ctype = state["chat_id"], state["ctype"]
    del user_states[user_id]
    event_id = add_event(creator_user_id=user_id, target_id=chat_id,
                         target_type=ctype, display_name=display,
                         content=content, event_datetime=dt,
                         priority_level=level, remaining_repeats=rule["repeats"])
    if not event_id:
        await q.edit_message_text("❌ 建立失敗。")
        return
    safe_add_job(send_reminder, dt, [event_id], f"reminder_{event_id}")
    await q.edit_message_text(
        f"{rule['icon']} 重要提醒已設定！\n\n"
        f"📅 {dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}\n"
        f"⏱️ 未確認將每 {rule['interval']} 分鐘重提")


# ── 週期提醒 ─────────────────────────────────────────────────────────────────

async def handle_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = {"action": "recurring_select_days", "days": set()}
    await reply(update, "🔁 週期提醒設定\n請選擇要提醒的星期（可多選）：", recurring_kb(set()))

async def cb_rec_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE, day: str):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    state   = user_states.setdefault(user_id, {"action": "recurring_select_days", "days": set()})
    days: set = state.setdefault("days", set())
    days.discard(day) if day in days else days.add(day)
    await q.edit_message_text(
        "🔁 週期提醒設定\n請選擇要提醒的星期（可多選）：",
        reply_markup=recurring_kb(days))

async def cb_rec_settime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    days    = user_states.get(user_id, {}).get("days", set())
    if not days:
        await q.answer("⚠️ 請至少選一天！", show_alert=True)
        return
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
        event_datetime=now_taipei(), is_recurring=1, recurrence_rule=rule_str,
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
        db.delete(loc)
        db.commit()
        db.close()
        await q.edit_message_text(f"🗑️ 地點「{name}」已刪除。")
    else:
        db.close()
        await q.edit_message_text("❌ 找不到該地點。")


# ── 記憶庫 ───────────────────────────────────────────────────────────────────

async def handle_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if text.startswith("記住"):
        parts = text[2:].strip().split(" ", 1)
        if len(parts) < 2:
            await reply(update, "格式：<code>記住 [關鍵字] [內容]</code>")
            return
        kw, content = parts
        if save_memory(user_id, kw, content):
            await reply(update, f"🧠 已記住：<b>{kw}</b>\n{content}")
        else:
            await reply(update, "❌ 儲存失敗。")
    elif text.startswith("查詢"):
        kw      = text[2:].strip()
        results = query_memory(user_id, kw)
        if not results:
            await reply(update, f"🔍 找不到「{kw}」的記憶。")
        elif len(results) == 1:
            await reply(update, f"🧠 <b>{results[0].keyword}</b>\n{results[0].content}")
        else:
            btns = [[InlineKeyboardButton(m.keyword, callback_data=f"mem:view:{m.id}")]
                    for m in results]
            await reply(update, "🔍 找到多筆，請選擇：", InlineKeyboardMarkup(btns))
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

async def cb_mem_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE, mem_id: int):
    q = update.callback_query
    await q.answer()
    from db import SessionLocal, Memory as MemModel
    db  = SessionLocal()
    mem = db.query(MemModel).filter(MemModel.id == mem_id).first()
    db.close()
    if mem:
        await q.edit_message_text(f"🧠 <b>{mem.keyword}</b>\n{mem.content}",
                                   parse_mode=ParseMode.HTML)
    else:
        await q.edit_message_text("❌ 找不到。")


# ── 主訊息 Handler ────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text    = update.message.text.strip()
    user_id = update.effective_user.id
    grp     = is_group(update)

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
        elif action == "recurring_set_time":
            m = re.match(r"^(\d{1,2}):(\d{2})$", text.strip())
            if not m:
                await reply(update, "❌ 格式錯誤，請輸入 HH:MM，如 09:00")
                return
            h, minute = int(m.group(1)), int(m.group(2))
            user_states[user_id]["time"]   = f"{h:02d}:{minute:02d}"
            user_states[user_id]["action"] = "recurring_input_content"
            await reply(update, "📝 請輸入提醒事項內容：")
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

    # 固定指令路由
    if text == "提醒清單":
        await handle_reminder_list(update, ctx); return
    if text.startswith("重要提醒") or text.startswith("提醒"):
        await handle_reminder(update, ctx, text); return
    if text == "週期提醒":
        await handle_recurring(update, ctx); return
    if text in ("地點", "地點清單"):
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
    if any(text.startswith(k) for k in ["記住", "查詢", "忘記"]) or text == "記憶清單":
        await handle_memory(update, ctx, text); return
    if text.lower() in ("help", "說明", "幫助"):
        await cmd_help(update, ctx); return

    if not grp:
        await reply(update, "🤔 我聽不懂，輸入「說明」查看指令。")


# ── Callback ─────────────────────────────────────────────────────────────────

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
        if parts[0] == "cr":
            await cb_confirm(update, ctx, int(parts[1]))
        elif parts[0] == "sn":
            await cb_snooze(update, ctx, int(parts[1]), int(parts[2]))
        elif parts[0] == "prio":
            await cb_priority(update, ctx, int(parts[1]))
        elif parts[0] == "re":
            action = parts[1]
            if action == "page":    await handle_reminder_list(update, ctx, int(parts[2]))
            elif action == "del":   await cb_delete_prompt(update, ctx, int(parts[2]))
            elif action == "delok": await cb_delete_ok(update, ctx, int(parts[2]))
            elif action == "edit":  await cb_edit_prompt(update, ctx, int(parts[2]))
        elif parts[0] == "rec":
            if parts[1] == "toggle":   await cb_rec_toggle(update, ctx, parts[2])
            elif parts[1] == "settime":await cb_rec_settime(update, ctx)
        elif parts[0] == "loc":
            if parts[1] == "send": await cb_loc_send(update, ctx, int(parts[2]))
            elif parts[1] == "del":await cb_loc_del(update, ctx, int(parts[2]))
        elif parts[0] == "mem":
            if parts[1] == "view": await cb_mem_view(update, ctx, int(parts[2]))
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
    a.add_handler(MessageHandler(filters.LOCATION, handle_location_msg))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    a.add_handler(CallbackQueryHandler(handle_callback))
    return a


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def root():
    return {"service": "TG Reminder Bot", "status": "running"}, 200

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "scheduler": scheduler.running}, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram 推送更新的端點"""
    data   = request.get_json(force=True)
    update = Update.de_json(data, _ptb_app.bot)
    asyncio.run_coroutine_threadsafe(
        _ptb_app.process_update(update), _loop
    ).result(timeout=30)
    return "OK", 200

@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    """手動設定 Webhook（WEBHOOK_URL 直接包含完整路徑）"""
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

    # 啟動 asyncio event loop（背景 thread）
    _loop = asyncio.new_event_loop()
    t = threading.Thread(target=_run_event_loop, args=(_loop,), daemon=True)
    t.start()

    # 初始化 PTB
    _ptb_app = build_ptb_app()
    _async(_ptb_app.initialize())
    _async(_ptb_app.start())

    # 設定 Webhook（失敗只警告，不 crash）
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

    logger.info("🤖 Bot 啟動完成，等待訊息...")


# Gunicorn 載入時執行
start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

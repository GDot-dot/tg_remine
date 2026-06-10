# handlers/reminders.py
import re
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from db import (
    add_event, get_event, get_user_events,
    update_event_content, update_event_fields, delete_event_by_id,
    is_active_event,
)
from scheduler import (
    safe_add_job, safe_add_cron, remove_job, send_reminder,
    TAIPEI_TZ, PRIORITY_RULES,
)

user_states: dict[int, dict] = {}


def bind_user_states(store: dict[int, dict]):
    global user_states
    user_states = store

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


def parse_absolute_datetime_input(value: str) -> datetime | None:
    text = value.strip()
    now = now_taipei()

    for word, delta in [("今天", 0), ("明天", 1), ("後天", 2)]:
        m = re.match(rf"^{word}\s*(\d{{1,2}}):(\d{{2}})$", text)
        if m:
            return (now + timedelta(days=delta)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)),
                second=0, microsecond=0,
            )

    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if m:
        mo, dy, hh, mm = map(int, m.groups())
        year = now.year if (mo, dy) >= (now.month, now.day) else now.year + 1
        try:
            return TAIPEI_TZ.localize(datetime(year, mo, dy, hh, mm))
        except ValueError:
            return None

    m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if m:
        year, mo, dy, hh, mm = map(int, m.groups())
        try:
            return TAIPEI_TZ.localize(datetime(year, mo, dy, hh, mm))
        except ValueError:
            return None

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

    absolute_dt = parse_absolute_datetime_input(text)
    if absolute_dt:
        return absolute_dt, absolute_dt.strftime("%Y/%m/%d %H:%M")

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


ZH_WEEKDAYS = {
    "一": 0, "1": 0,
    "二": 1, "2": 1,
    "三": 2, "3": 2,
    "四": 3, "4": 3,
    "五": 4, "5": 4,
    "六": 5, "6": 5,
    "日": 6, "天": 6, "7": 6,
}

ZH_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９：", "0123456789:")


def _zh_number(value: str) -> int | None:
    value = (value or "").strip().translate(FULLWIDTH_DIGITS)
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = ZH_DIGITS.get(left, 1) if left else 1
        ones = ZH_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return ZH_DIGITS.get(value)


def _next_weekday(target_idx: int, explicit_next_week: bool) -> datetime:
    today = now_taipei().date()
    if explicit_next_week:
        days_until_next_monday = 7 - today.weekday()
        return datetime.combine(today + timedelta(days=days_until_next_monday + target_idx), datetime.min.time())
    delta = (target_idx - today.weekday()) % 7
    return datetime.combine(today + timedelta(days=delta), datetime.min.time())


def _parse_natural_date(text: str) -> tuple[datetime | None, str | None]:
    now = now_taipei()
    date_patterns = [
        (r"今天", lambda _m: now),
        (r"明天", lambda _m: now + timedelta(days=1)),
        (r"後天", lambda _m: now + timedelta(days=2)),
        (r"下(?:週|星期|禮拜)([一二三四五六日天1-7])", lambda m: TAIPEI_TZ.localize(_next_weekday(ZH_WEEKDAYS[m.group(1)], True))),
        (r"(?:這)?(?:週|星期|禮拜)([一二三四五六日天1-7])", lambda m: TAIPEI_TZ.localize(_next_weekday(ZH_WEEKDAYS[m.group(1)], False))),
    ]
    for pattern, builder in date_patterns:
        m = re.search(pattern, text)
        if m:
            return builder(m), m.group(0)

    m = re.search(r"(20\d{2})[/\-](\d{1,2})[/\-](\d{1,2})", text)
    if m:
        try:
            return TAIPEI_TZ.localize(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))), m.group(0)
        except ValueError:
            return None, None

    m = re.search(r"(?<!\d)(\d{1,2})[/\-](\d{1,2})(?!\d)", text)
    if m:
        try:
            mo, dy = int(m.group(1)), int(m.group(2))
            year = now.year if (mo, dy) >= (now.month, now.day) else now.year + 1
            return TAIPEI_TZ.localize(datetime(year, mo, dy)), m.group(0)
        except ValueError:
            return None, None

    return None, None


def _adjust_hour_by_period(hour: int, period: str | None) -> int:
    if period in ("下午", "傍晚", "晚上", "今晚") and 1 <= hour < 12:
        return hour + 12
    if period == "中午" and 1 <= hour < 11:
        return hour + 12
    if period == "凌晨" and hour == 12:
        return 0
    return hour


def _parse_natural_time(text: str) -> tuple[tuple[int, int] | None, str | None]:
    period_re = r"(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|今晚)?"
    m = re.search(rf"{period_re}\s*([01]?\d|2[0-3])[:：]([0-5]\d)", text)
    if m:
        hour = _adjust_hour_by_period(int(m.group(2)), m.group(1))
        return (hour, int(m.group(3))), m.group(0)

    m = re.search(rf"{period_re}\s*([零〇一二兩三四五六七八九十\d]{{1,3}})\s*(?:點|時)(半)?\s*([零〇一二兩三四五六七八九十\d]{{1,3}})?\s*(?:分)?", text)
    if m:
        hour = _zh_number(m.group(2))
        minute = 30 if m.group(3) else (_zh_number(m.group(4)) if m.group(4) else 0)
        if hour is None or minute is None:
            return None, None
        hour = _adjust_hour_by_period(hour, m.group(1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute), m.group(0)

    m = re.search(r"(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|今晚)", text)
    if m:
        defaults = {
            "凌晨": (0, 0), "清晨": (7, 0), "早上": (8, 0), "上午": (9, 0),
            "中午": (12, 0), "下午": (14, 0), "傍晚": (18, 0),
            "晚上": (19, 0), "今晚": (19, 0),
        }
        return defaults[m.group(1)], m.group(0)

    return None, None


def parse_natural_reminder_text(text: str):
    raw = text.strip().translate(FULLWIDTH_DIGITS)
    if not re.search(r"(提醒|記得|叫我)", raw):
        return None, None, None

    date_base, date_token = _parse_natural_date(raw)
    time_parts, time_token = _parse_natural_time(raw)
    if not date_base or not time_parts:
        return None, None, None

    hour, minute = time_parts
    dt = date_base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    content = raw
    for token in (date_token, time_token):
        if token:
            content = content.replace(token, " ", 1)
    content = re.sub(r"^(重要提醒|提醒)\s*", " ", content)
    content = re.sub(r"(提醒我|提醒一下我|提醒一下|提醒|叫我|記得要|記得)", " ", content)
    content = re.sub(r"[，,。；;：:]\s*", " ", content)
    content = re.sub(r"\s+", " ", content).strip()
    content = content or "提醒事項"
    return "我", dt, content




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
        return parse_natural_reminder_text(text)
    who_raw, date_s, time_s, content = m.groups()
    who = who_raw.strip() or "我"
    dt  = parse_dt_from_parts(date_s, time_s)
    if dt and not time_s:
        time_parts, time_token = _parse_natural_time(content.translate(FULLWIDTH_DIGITS))
        if time_parts:
            hour, minute = time_parts
            dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if time_token:
                content = content.replace(time_token, " ", 1)
                content = re.sub(r"\s+", " ", content).strip()
    return who, dt, content.strip()


def looks_like_natural_reminder(text: str) -> bool:
    if not re.search(r"(提醒|記得|叫我)", text):
        return False
    date_base, _ = _parse_natural_date(text.translate(FULLWIDTH_DIGITS))
    time_parts, _ = _parse_natural_time(text.translate(FULLWIDTH_DIGITS))
    return bool(date_base and time_parts)


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

    update_event_fields(event_id, reminder_time=reminder_dt, reminder_sent=0, event_status="pending", completed_at=None)
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
        "或輸入指定日期時間，例如：\n"
        "<code>今天 14:00</code>、<code>明天 09:30</code>\n"
        "<code>06/10 18:00</code>、<code>2026-06-10 18:00</code>\n\n"
        "只輸入 <code>09:30</code> 時，會用事件當天的 09:30。",
        parse_mode=ParseMode.HTML,
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
    rows.append([InlineKeyboardButton("🕒 自訂提醒時間", callback_data="pec")])
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


async def cb_priority_custom_reminder_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    if state.get("action") != "priority_pick_early":
        return
    state["action"] = "priority_custom_time"
    await q.edit_message_text(
        "🕒 請輸入重要提醒的自訂提醒時間：\n"
        "可輸入提前分鐘數，例如 <code>45</code>、<code>提前120分鐘</code>；\n"
        "或輸入指定日期時間，例如：\n"
        "<code>今天 14:00</code>、<code>明天 09:30</code>\n"
        "<code>06/10 18:00</code>、<code>2026-06-10 18:00</code>\n\n"
        "自訂時間必須在現在之後，且不能晚於事件時間。",
        parse_mode=ParseMode.HTML,
    )


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
    minutes_early  = state.get("minutes_early")
    reminder_dt    = state.get("reminder_dt") or (dt - timedelta(minutes=minutes_early))

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
    update_event_fields(event_id, reminder_time=reminder_dt, reminder_sent=0, event_status="pending", completed_at=None)
    early_txt = state.get("reminder_label") or next((l for l, m in EARLY_OPTIONS if m == minutes_early), "準時")
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
            update_event_fields(event_id, event_status="confirmed", completed_at=now_taipei())
            await q.edit_message_text("✅ 提醒已確認收到！（下個週期會繼續提醒）")
        else:
            # 非週期（含重要提醒）：確認後直接移除，避免累積已完成資料。
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
    update_event_fields(event_id, reminder_time=new_time, reminder_sent=0, event_status="snoozed", completed_at=None)
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
    events = [ev for ev in all_events if is_active_event(ev)]
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




async def handle_reminder_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE, state_store: dict, action: str, text: str) -> bool:
    handled = await _handle_reminder_state(update, ctx, state_store, action, text)
    return handled is not False


async def _handle_reminder_state(update: Update, ctx: ContextTypes.DEFAULT_TYPE, state_store: dict, action: str, text: str):
    user_id = update.effective_user.id
    if action == "snooze_custom":
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
        update_event_fields(event_id, reminder_time=new_time, reminder_sent=0, event_status="snoozed", completed_at=None)
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
            await reply(update,
                "❌ 格式錯誤，請輸入：\n"
                "<code>45</code>、<code>提前120分鐘</code>\n"
                "<code>今天 14:00</code>、<code>明天 09:30</code>\n"
                "<code>06/10 18:00</code>、<code>2026-06-10 18:00</code>")
            return
        reminder_dt, label = parsed
        if reminder_dt <= now_taipei() or reminder_dt > event_dt:
            await reply(update, "⚠️ 自訂提醒時間必須在現在之後，且不能晚於事件時間。")
            return
        update_event_fields(event_id, reminder_time=reminder_dt, reminder_sent=0, event_status="pending", completed_at=None)
        safe_add_job(send_reminder, reminder_dt, [event_id], f"reminder_{event_id}")
        user_states.pop(user_id, None)
        await reply(update, f"✅ 設定完成！\n將於 {reminder_dt.strftime('%Y/%m/%d %H:%M')}（{label}）提醒您。")
        return

    elif action == "priority_custom_time":
        state = user_states[user_id]
        event_dt = state["dt"]
        parsed = parse_custom_reminder_time(text, event_dt)
        if not parsed:
            await reply(update,
                "❌ 格式錯誤，請輸入：\n"
                "<code>45</code>、<code>提前120分鐘</code>\n"
                "<code>今天 14:00</code>、<code>明天 09:30</code>\n"
                "<code>06/10 18:00</code>、<code>2026-06-10 18:00</code>")
            return
        reminder_dt, label = parsed
        if reminder_dt <= now_taipei() or reminder_dt > event_dt:
            await reply(update, "⚠️ 自訂提醒時間必須在現在之後，且不能晚於事件時間。")
            return
        state["reminder_dt"] = reminder_dt
        state["reminder_label"] = label
        state["action"] = "priority_pick_level"

        rows = [[InlineKeyboardButton(label_text, callback_data=f"pl:{level}")]
                for label_text, level in PRIORITY_OPTIONS]
        rows.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
        await reply(update,
            f"將於 {reminder_dt.strftime('%Y/%m/%d %H:%M')} 提醒。\n\n請選擇重複提醒的頻率：",
            InlineKeyboardMarkup(rows))
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
        if update_event_fields(event_id, recurrence_rule=rule_str, reminder_time=now_taipei(), event_status="pending", completed_at=None):
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
        update_event_fields(event_id, reminder_time=new_dt, reminder_sent=0, event_status="pending", completed_at=None)
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
        if update_event_fields(event_id, recurrence_rule=f"custom:{interval}", remaining_repeats=repeats, reminder_sent=0, event_status="pending", completed_at=None):
            await reply(update, f"✅ 已更新重要提醒：每 {interval} 分鐘重提，共 {repeats} 次")
        else:
            await reply(update, "❌ 更新失敗。")
        return

    return False

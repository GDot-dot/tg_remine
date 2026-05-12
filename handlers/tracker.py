# handlers/tracker.py
# 訂閱 / 合約 / 紀念日 / 藥物 追蹤功能

import logging
import html
import re
from datetime import date, timedelta, datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
import pytz

from db import (
    add_tracker, get_trackers, get_tracker_by_id, update_tracker,
    delete_tracker_by_name,
)
from ai_parser import parse_tracker

logger = logging.getLogger(__name__)
TAIPEI_TZ = pytz.timezone("Asia/Taipei")

CATEGORY_ICON = {
    "subscription": "💳",
    "contract":     "📄",
    "anniversary":  "🎂",
    "medicine":     "💊",
}
CATEGORY_NAME = {
    "subscription": "訂閱",
    "contract":     "合約",
    "anniversary":  "紀念日",
    "medicine":     "藥物",
}

# 觸發詞對應 category（讓 Gemini 知道意圖）
TRIGGER_MAP = {
    "訂閱": "subscription",
    "合約": "contract",
    "租約": "contract",
    "紀念日": "anniversary",
    "藥物": "medicine",
}


async def reply(update: Update, text: str, **kwargs):
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, **kwargs)


def today_taipei() -> date:
    return datetime.now(TAIPEI_TZ).date()


def to_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_cycle(value):
    return value if value in ("monthly", "yearly", "once") else None


def clean_remind_time(value: str | None) -> str:
    if not value:
        return "08:00"
    try:
        hour, minute = str(value).strip().replace("：", ":").split(":", 1)
        hour_i, minute_i = int(hour), int(minute)
        if 0 <= hour_i <= 23 and 0 <= minute_i <= 59:
            return f"{hour_i:02d}:{minute_i:02d}"
    except (TypeError, ValueError):
        pass
    return "08:00"


def tracker_detail_text(t) -> str:
    icon = CATEGORY_ICON.get(t.category, "📌")
    cname = CATEGORY_NAME.get(t.category, t.category)
    lines = [f"{icon} <b>{cname}</b>：<b>{html.escape(t.name)}</b>"]
    today = today_taipei()
    nd = calc_next_date(t, today)
    if nd:
        date_str = nd.strftime("%m/%d") if t.is_recurring else nd.strftime("%Y/%m/%d")
        lines.append(f"📅 下次日期：{date_str}（{days_left_str(nd, today)}）")
    if t.amount is not None:
        cycle_zh = {"monthly": "月", "yearly": "年", "once": "次"}.get(t.cycle or "", "")
        suffix = f"/{cycle_zh}" if cycle_zh else ""
        lines.append(f"💰 費用：{t.amount:.0f} 元{suffix}")
    if t.category == "medicine" and t.stock_total and t.stock_daily:
        lines.append(f"💊 庫存：{t.stock_total:.0f}，每日 {t.stock_daily:.0f}")
    if t.remind_days is not None and t.remind_days < 0:
        lines.append("🔕 提醒：關閉")
    else:
        lines.append(f"⏰ 提醒：提前 {t.remind_days or 0} 天，{clean_remind_time(t.remind_time)}")
    return "\n".join(lines)


def tracker_detail_kb(t) -> InlineKeyboardMarkup:
    notify_label = "🔔 開啟提醒" if (t.remind_days is not None and t.remind_days < 0) else "🔕 關閉提醒"
    rows = [
        [InlineKeyboardButton("✏️ 名稱", callback_data=f"tr:edit:name:{t.id}"),
         InlineKeyboardButton("⏰ 時間", callback_data=f"tr:edit:time:{t.id}")],
        [InlineKeyboardButton("📆 提前天數", callback_data=f"tr:edit:days:{t.id}"),
         InlineKeyboardButton("💰 費用", callback_data=f"tr:edit:amount:{t.id}")],
        [InlineKeyboardButton(notify_label, callback_data=f"tr:notify:{t.id}")],
        [InlineKeyboardButton("❌ 取消", callback_data="cancel")],
    ]
    return InlineKeyboardMarkup(rows)


# ── 計算下次到期/提醒日 ────────────────────────────────────────────────────────

def calc_next_date(tracker, today: date) -> "date | None":
    """計算 tracker 下次相關日期"""
    try:
        if tracker.is_recurring and tracker.recurring_month and tracker.recurring_day:
            # 紀念日：找今年或明年
            try:
                d = today.replace(month=tracker.recurring_month, day=tracker.recurring_day)
            except ValueError:
                return None
            if d < today:
                d = d.replace(year=d.year + 1)
            return d

        if tracker.category == "medicine" and tracker.stock_total and tracker.stock_daily:
            # 藥物：從建立日計算耗盡日
            days = int(tracker.stock_total / tracker.stock_daily)
            return tracker.created_at.date() + timedelta(days=days)

        if tracker.expire_date:
            d = tracker.expire_date
            if tracker.cycle == "monthly":
                while d < today:
                    month = d.month + 1 if d.month < 12 else 1
                    year  = d.year if d.month < 12 else d.year + 1
                    try:
                        d = d.replace(year=year, month=month)
                    except ValueError:
                        d = d.replace(year=year, month=month, day=28)
            elif tracker.cycle == "yearly":
                while d < today:
                    d = d.replace(year=d.year + 1)
            return d
    except Exception as e:
        logger.error(f"calc_next_date: {e}")
    return None


def days_left_str(d: date, today: date) -> str:
    diff = (d - today).days
    if diff < 0:
        return "已過期"
    elif diff == 0:
        return "今天！"
    elif diff == 1:
        return "明天"
    return f"還有 {diff} 天"


# ── 新增 tracker ──────────────────────────────────────────────────────────────

async def handle_tracker_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    from datetime import datetime as dt
    now_str = dt.now().strftime("%Y-%m-%d %H:%M")

    parsed = parse_tracker(text, now_str)
    if not parsed:
        await reply(update,
            "❌ 無法解析，請試試：\n"
            "<code>訂閱 Netflix 每月15號 390元</code>\n"
            "<code>合約 租約 2026/12/31 提前30天</code>\n"
            "<code>紀念日 媽媽生日 0520</code>\n"
            "<code>藥物 魚油 60顆 每天2顆</code>")
        return

    cat = parsed.get("category")
    if cat not in CATEGORY_NAME:
        await reply(update, "❌ 目前支援：訂閱、合約/租約、紀念日、藥物。")
        return

    name = parsed.get("name", "").strip()
    if not name:
        await reply(update, "❌ 請輸入項目名稱。")
        return

    recurring_month = to_int(parsed.get("recurring_month"))
    recurring_day = to_int(parsed.get("recurring_day"))
    amount = to_float(parsed.get("amount"))
    remind_days = max(0, to_int(parsed.get("remind_days"), 7))
    remind_time = clean_remind_time(parsed.get("remind_time"))
    stock_total = to_float(parsed.get("stock_total"))
    stock_daily = to_float(parsed.get("stock_daily"))
    cycle = clean_cycle(parsed.get("cycle"))

    # 日期處理
    expire_date = None
    if parsed.get("expire_date"):
        try:
            expire_date = date.fromisoformat(parsed["expire_date"])
        except Exception:
            pass

    if cat in ("subscription", "contract") and not expire_date:
        await reply(update, "❌ 請加上日期，例如：<code>訂閱 Netflix 每月15號 390元</code> 或 <code>合約 租約 2026/12/31</code>")
        return
    if cat == "anniversary" and not (recurring_month and recurring_day):
        await reply(update, "❌ 請加上紀念日日期，例如：<code>紀念日 媽媽生日 0520</code>")
        return
    if cat == "medicine" and not (stock_total and stock_daily):
        await reply(update, "❌ 請加上庫存和每日用量，例如：<code>藥物 魚油 60顆 每天2顆</code>")
        return

    tracker_id = add_tracker(
        user_id        = user_id,
        category       = cat,
        name           = name,
        expire_date    = expire_date,
        is_recurring   = 1 if cat == "anniversary" else int(parsed.get("is_recurring") or 0),
        recurring_month= recurring_month,
        recurring_day  = recurring_day,
        cycle          = cycle,
        amount         = amount,
        remind_days    = remind_days,
        remind_time    = remind_time,
        stock_total    = stock_total,
        stock_daily    = stock_daily,
    )

    if not tracker_id:
        await reply(update, "❌ 儲存失敗，請再試一次。")
        return

    icon = CATEGORY_ICON.get(cat, "📌")
    cname = CATEGORY_NAME.get(cat, cat)
    safe_name = html.escape(name)
    lines = [f"{icon} 已新增{cname}：<b>{safe_name}</b>"]

    if expire_date:
        today = today_taipei()
        diff = (expire_date - today).days
        lines.append(f"📅 到期：{expire_date.strftime('%Y/%m/%d')}（{days_left_str(expire_date, today)}）")
    if amount:
        lines.append(f"💰 金額：{amount:.0f} 元")
    if recurring_month and recurring_day:
        lines.append(f"📅 日期：每年 {recurring_month:02d}/{recurring_day:02d}")
    if stock_total and stock_daily:
        days = int(stock_total / stock_daily)
        lines.append(f"💊 預計 {days} 天後耗盡")
    lines.append(f"⏰ 提前 {remind_days} 天，{remind_time} 提醒")

    await reply(update, "\n".join(lines))


# ── 追蹤清單 ──────────────────────────────────────────────────────────────────

async def handle_tracker_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, filter_cat: str = None):
    user_id = update.effective_user.id
    today = today_taipei()

    cat_filter = {
        "訂閱清單": "subscription",
        "合約清單": "contract",
        "紀念日清單": "anniversary",
        "藥物清單": "medicine",
    }.get(filter_cat)

    trackers = get_trackers(user_id, cat_filter)
    if not trackers:
        cat_label = CATEGORY_NAME.get(cat_filter, "追蹤") if cat_filter else "追蹤"
        await reply(update, f"📋 {cat_label}清單是空的。")
        return

    # 按 category 分組
    groups: dict[str, list] = {}
    for t in trackers:
        groups.setdefault(t.category, []).append(t)

    lines = ["📋 <b>追蹤清單</b>\n"]
    buttons = []

    for cat in ["subscription", "contract", "anniversary", "medicine"]:
        items = groups.get(cat, [])
        if not items:
            continue
        icon = CATEGORY_ICON[cat]
        cname = CATEGORY_NAME[cat]

        # 訂閱：加總月費
        if cat == "subscription":
            monthly_total = sum(
                t.amount for t in items
                if t.amount and t.cycle == "monthly"
            )
            header = f"{icon} <b>{cname}</b>"
            if monthly_total:
                header += f"（每月 {monthly_total:.0f} 元）"
        else:
            header = f"{icon} <b>{cname}</b>"
        lines.append(header)

        for t in items:
            nd = calc_next_date(t, today)
            if nd:
                dl = days_left_str(nd, today)
                date_str = nd.strftime("%m/%d") if t.is_recurring else nd.strftime("%Y/%m/%d")
                suffix = f"  {date_str}（{dl}）"
            else:
                suffix = ""

            amount_str = f"  {t.amount:.0f}元/{t.cycle[:2] if t.cycle else ''}" if t.amount else ""
            recurring_str = " ⭐每年" if t.is_recurring else ""
            lines.append(f"• {html.escape(t.name)}{suffix}{amount_str}{recurring_str}")
            buttons.append([InlineKeyboardButton(f"編輯 {t.name}", callback_data=f"tr:view:{t.id}")])

        lines.append("")

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    await reply(update, "\n".join(lines).strip(), reply_markup=markup)


async def handle_tracker_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tracker_id: int):
    q = update.callback_query
    await q.answer()
    t = get_tracker_by_id(update.effective_user.id, tracker_id)
    if not t:
        await q.edit_message_text("❌ 找不到這筆追蹤項目。")
        return
    await q.edit_message_text(
        tracker_detail_text(t),
        parse_mode=ParseMode.HTML,
        reply_markup=tracker_detail_kb(t),
    )


async def handle_tracker_edit_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                     state_store: dict, field: str, tracker_id: int):
    q = update.callback_query
    await q.answer()
    t = get_tracker_by_id(update.effective_user.id, tracker_id)
    if not t:
        await q.edit_message_text("❌ 找不到這筆追蹤項目。")
        return

    prompts = {
        "name": f"✏️ 請輸入新的名稱：\n目前：{html.escape(t.name)}",
        "time": f"⏰ 請輸入提醒時間（HH:MM）：\n目前：{clean_remind_time(t.remind_time)}",
        "days": f"📆 請輸入提前幾天提醒：\n目前：{t.remind_days if t.remind_days is not None and t.remind_days >= 0 else '不提醒'}\n\n輸入 <code>不提醒</code> 可關閉。",
        "amount": f"💰 請輸入新的費用：\n目前：{t.amount:.0f} 元" if t.amount is not None else "💰 請輸入新的費用：\n目前：未設定",
    }
    if field not in prompts:
        await q.answer("❓ 未知欄位", show_alert=True)
        return

    state_store[update.effective_user.id] = {
        "action": "edit_tracker_field",
        "tracker_id": tracker_id,
        "field": field,
    }
    await q.edit_message_text(prompts[field], parse_mode=ParseMode.HTML)


async def handle_tracker_toggle_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE, tracker_id: int):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    t = get_tracker_by_id(user_id, tracker_id)
    if not t:
        await q.edit_message_text("❌ 找不到這筆追蹤項目。")
        return
    new_days = 7 if (t.remind_days is not None and t.remind_days < 0) else -1
    if not update_tracker(user_id, tracker_id, remind_days=new_days):
        await q.edit_message_text("❌ 更新失敗。")
        return
    t = get_tracker_by_id(user_id, tracker_id)
    await q.edit_message_text(
        tracker_detail_text(t),
        parse_mode=ParseMode.HTML,
        reply_markup=tracker_detail_kb(t),
    )


async def handle_tracker_edit_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                    state_store: dict, text: str):
    user_id = update.effective_user.id
    state = state_store.pop(user_id, {})
    tracker_id = state.get("tracker_id")
    field = state.get("field")
    t = get_tracker_by_id(user_id, tracker_id)
    if not t:
        await reply(update, "❌ 找不到這筆追蹤項目。")
        return

    raw = text.strip()
    updates = {}
    if field == "name":
        if not raw:
            await reply(update, "❌ 名稱不能空白，請重新輸入。")
            state_store[user_id] = state
            return
        updates["name"] = raw[:100]
    elif field == "time":
        cleaned = clean_remind_time(raw)
        if cleaned == "08:00" and raw.replace("：", ":") not in ("8:00", "08:00"):
            await reply(update, "❌ 格式錯誤，請輸入 HH:MM，例如 <code>09:30</code>。")
            state_store[user_id] = state
            return
        updates["remind_time"] = cleaned
    elif field == "days":
        if raw in ("不提醒", "關閉", "不要", "取消提醒", "off", "OFF"):
            updates["remind_days"] = -1
        else:
            days = to_int(raw)
            if days is None or days < 0:
                await reply(update, "❌ 請輸入 0 以上的天數，或輸入 <code>不提醒</code>。")
                state_store[user_id] = state
                return
            updates["remind_days"] = days
    elif field == "amount":
        if raw in ("無", "沒有", "不記", "不記費用", "清空", "0"):
            updates["amount"] = None
        else:
            number_match = re.search(r"\d+(?:\.\d+)?", raw.replace(",", ""))
            amount = to_float(number_match.group(0) if number_match else None)
            if amount is None or amount < 0:
                await reply(update, "❌ 請輸入費用數字，例如 <code>390</code>，或輸入 <code>清空</code>。")
                state_store[user_id] = state
                return
            updates["amount"] = amount
            if "年" in raw:
                updates["cycle"] = "yearly"
            elif "月" in raw:
                updates["cycle"] = "monthly"
    else:
        await reply(update, "❌ 未知的編輯欄位。")
        return

    if not update_tracker(user_id, tracker_id, **updates):
        await reply(update, "❌ 更新失敗。")
        return

    t = get_tracker_by_id(user_id, tracker_id)
    await reply(update, "✅ 已更新追蹤項目：\n" + tracker_detail_text(t))


# ── 每月支出 ──────────────────────────────────────────────────────────────────

async def handle_monthly_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subs = get_trackers(user_id, "subscription")

    if not subs:
        await reply(update, "💳 目前沒有訂閱項目。")
        return

    monthly = [(t.name, t.amount) for t in subs if t.amount and t.cycle == "monthly"]
    yearly  = [(t.name, t.amount) for t in subs if t.amount and t.cycle == "yearly"]
    other   = [(t.name, t.amount) for t in subs if t.amount and t.cycle not in ("monthly", "yearly")]
    monthly_total = sum(a for _, a in monthly)
    yearly_total = sum(a for _, a in yearly)
    annual_total = monthly_total * 12 + yearly_total
    monthly_average = annual_total / 12 if annual_total else 0

    lines = ["💳 <b>訂閱費用統計</b>\n"]

    if monthly:
        lines.append("每月：")
        for name, amt in monthly:
            lines.append(f"• {html.escape(name)}  {amt:.0f} 元")
        lines.append(f"<b>小計：{monthly_total:.0f} 元/月</b>\n")

    if yearly:
        lines.append("每年：")
        for name, amt in yearly:
            lines.append(f"• {html.escape(name)}  {amt:.0f} 元（約 {amt / 12:.0f} 元/月）")
        lines.append(f"<b>小計：{yearly_total:.0f} 元/年</b>\n")

    if other:
        lines.append("其他：")
        for name, amt in other:
            lines.append(f"• {html.escape(name)}  {amt:.0f} 元")
        lines.append("")

    if annual_total:
        lines.append("總覽：")
        lines.append(f"• 月費合計：{monthly_total:.0f} 元/月")
        lines.append(f"• 年費合計：{yearly_total:.0f} 元/年")
        lines.append(f"<b>• 年化總額：{annual_total:.0f} 元/年</b>")
        lines.append(f"<b>• 月均成本：約 {monthly_average:.0f} 元/月</b>")

    await reply(update, "\n".join(lines))


# ── 刪除 tracker ──────────────────────────────────────────────────────────────

async def handle_tracker_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE, name: str):
    user_id = update.effective_user.id
    if not name:
        await reply(update, "格式：<code>刪除追蹤 [名稱]</code>")
        return
    if delete_tracker_by_name(user_id, name):
        await reply(update, f"🗑️ 已刪除：{html.escape(name)}")
    else:
        await reply(update, f"❌ 找不到「{html.escape(name)}」。")

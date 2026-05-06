# handlers/tracker.py
# 訂閱 / 合約 / 紀念日 / 藥物 追蹤功能

import logging
from datetime import date, timedelta, datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from db import add_tracker, get_trackers, delete_tracker_by_name
from ai_parser import parse_tracker

logger = logging.getLogger(__name__)

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
    name = parsed.get("name", "").strip()
    if not name:
        await reply(update, "❌ 請輸入項目名稱。")
        return

    # 日期處理
    expire_date = None
    if parsed.get("expire_date"):
        try:
            expire_date = date.fromisoformat(parsed["expire_date"])
        except Exception:
            pass

    tracker_id = add_tracker(
        user_id        = user_id,
        category       = cat,
        name           = name,
        expire_date    = expire_date,
        is_recurring   = int(parsed.get("is_recurring") or 0),
        recurring_month= parsed.get("recurring_month"),
        recurring_day  = parsed.get("recurring_day"),
        cycle          = parsed.get("cycle"),
        amount         = parsed.get("amount"),
        remind_days    = int(parsed.get("remind_days") or 7),
        stock_total    = parsed.get("stock_total"),
        stock_daily    = parsed.get("stock_daily"),
    )

    if not tracker_id:
        await reply(update, "❌ 儲存失敗，請再試一次。")
        return

    icon = CATEGORY_ICON.get(cat, "📌")
    cname = CATEGORY_NAME.get(cat, cat)
    lines = [f"{icon} 已新增{cname}：<b>{name}</b>"]

    if expire_date:
        today = date.today()
        diff = (expire_date - today).days
        lines.append(f"📅 到期：{expire_date.strftime('%Y/%m/%d')}（{days_left_str(expire_date, today)}）")
    if parsed.get("amount"):
        lines.append(f"💰 金額：{parsed['amount']:.0f} 元")
    if parsed.get("recurring_month") and parsed.get("recurring_day"):
        lines.append(f"📅 日期：每年 {parsed['recurring_month']:02d}/{parsed['recurring_day']:02d}")
    if parsed.get("stock_total") and parsed.get("stock_daily"):
        days = int(parsed["stock_total"] / parsed["stock_daily"])
        lines.append(f"💊 預計 {days} 天後耗盡")
    lines.append(f"⏰ 提前 {parsed.get('remind_days', 7)} 天提醒")

    await reply(update, "\n".join(lines))


# ── 追蹤清單 ──────────────────────────────────────────────────────────────────

async def handle_tracker_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, filter_cat: str = None):
    user_id = update.effective_user.id
    today = date.today()

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
            lines.append(f"• {t.name}{suffix}{amount_str}{recurring_str}")

        lines.append("")

    await reply(update, "\n".join(lines).strip())


# ── 每月支出 ──────────────────────────────────────────────────────────────────

async def handle_monthly_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    subs = get_trackers(user_id, "subscription")

    if not subs:
        await reply(update, "💳 目前沒有訂閱項目。")
        return

    monthly = [(t.name, t.amount) for t in subs if t.amount and t.cycle == "monthly"]
    yearly  = [(t.name, t.amount) for t in subs if t.amount and t.cycle == "yearly"]

    lines = ["💳 <b>訂閱費用統計</b>\n"]

    if monthly:
        lines.append("每月：")
        for name, amt in monthly:
            lines.append(f"• {name}  {amt:.0f} 元")
        lines.append(f"<b>小計：{sum(a for _, a in monthly):.0f} 元/月</b>\n")

    if yearly:
        lines.append("每年：")
        for name, amt in yearly:
            lines.append(f"• {name}  {amt:.0f} 元")
        lines.append(f"<b>小計：{sum(a for _, a in yearly):.0f} 元/年</b>")

    await reply(update, "\n".join(lines))


# ── 刪除 tracker ──────────────────────────────────────────────────────────────

async def handle_tracker_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE, name: str):
    user_id = update.effective_user.id
    if not name:
        await reply(update, "格式：<code>刪除追蹤 [名稱]</code>")
        return
    if delete_tracker_by_name(user_id, name):
        await reply(update, f"🗑️ 已刪除：{name}")
    else:
        await reply(update, f"❌ 找不到「{name}」。")

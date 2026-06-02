# telegraph_pages.py
import html
import json
import os
from datetime import datetime

import pytz
import requests

from db import (
    get_locations, get_trackers, get_user_events, get_user_setting,
    list_memories, update_user_setting,
)

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TELEGRAPH_ACCESS_TOKEN = os.environ.get("TELEGRAPH_ACCESS_TOKEN", "")
TELEGRAPH_AUTHOR_NAME = os.environ.get("TELEGRAPH_AUTHOR_NAME", "GD牌提醒機器人")
TELEGRAPH_API = "https://api.telegra.ph"
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]
MEMORY_HTML_PREFIX = "__TG_MEMORY_HTML__\n"


def _now_taipei():
    return datetime.now(TAIPEI_TZ)


def _node(tag, children=None, attrs=None):
    result = {"tag": tag}
    if attrs:
        result["attrs"] = attrs
    if children is not None:
        result["children"] = children
    return result


def _p(*children):
    return _node("p", list(children))


def _h3(text):
    return _node("h3", [text])


def _h4(text):
    return _node("h4", [text])


def _li(*children):
    return _node("li", list(children))


def _ul(items):
    return _node("ul", items)


def _fmt_dt(value):
    if not value:
        return "未設定時間"
    try:
        return value.astimezone(TAIPEI_TZ).strftime("%Y/%m/%d %H:%M")
    except Exception:
        return str(value)


def _parse_recurring_rule(rule):
    if not rule or "|" not in rule:
        return set(), "09:00"
    days_str, time_str = rule.split("|", 1)
    return {d for d in days_str.split(",") if d in WEEKDAY_CODES}, time_str or "09:00"


def _weekday_names(days):
    return [WEEKDAY_NAMES[WEEKDAY_CODES.index(d)] for d in sorted(days) if d in WEEKDAY_CODES]


def _memory_plain(content):
    content = content or ""
    if content.startswith(MEMORY_HTML_PREFIX):
        content = content[len(MEMORY_HTML_PREFIX):]
    return html.unescape(content.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n"))


def _dashboard_counts(reminders, memories, trackers, locations):
    return _p(
        "📋 提醒 ", _node("b", [str(len(reminders))]), "　",
        "📌 追蹤 ", _node("b", [str(len(trackers))]), "　",
        "🧠 記憶 ", _node("b", [str(len(memories))]), "　",
        "📍 地點 ", _node("b", [str(len(locations))]),
    )


def _reminder_nodes(reminders):
    if not reminders:
        return [_p("目前沒有進行中的提醒。")]
    items = []
    for ev in reminders:
        if ev.is_recurring:
            days, time_str = _parse_recurring_rule(ev.recurrence_rule)
            day_label = "、".join(_weekday_names(days)) if days else "週期"
            meta = f"週期 · 每{day_label} {time_str}"
        else:
            meta = f"{'重要 · ' if ev.priority_level else ''}{_fmt_dt(ev.reminder_time)}"
        items.append(_li(_node("b", [ev.event_content or "(無內容)"]), _node("br"), meta))
    return [_ul(items)]


def _tracker_meta(t):
    cycle_label = {"monthly": "每月", "yearly": "每年", "once": "一次"}
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
    if t.notes:
        bits.append(t.notes)
    return " · ".join(bits) if bits else "未設定細節"


def _tracker_nodes(trackers):
    if not trackers:
        return [_p("追蹤清單是空的。")]
    category_order = ("subscription", "contract", "anniversary", "medicine")
    category_label = {
        "subscription": "💳 訂閱",
        "contract": "📄 合約",
        "anniversary": "🎂 紀念日",
        "medicine": "💊 藥物",
    }
    nodes = []
    for category in category_order:
        items = [t for t in trackers if t.category == category]
        if not items:
            continue
        nodes.append(_h4(category_label.get(category, category)))
        nodes.append(_ul([
            _li(_node("b", [t.name]), _node("br"), _tracker_meta(t))
            for t in items
        ]))
    return nodes


def _memory_nodes(memories):
    if not memories:
        return [_p("記憶庫是空的。")]
    nodes = []
    for mem in memories:
        keyword = (mem.keyword or "").strip()
        if not keyword:
            continue
        nodes.append(_p(_node("b", [keyword]), _node("br"), _memory_plain(mem.content)))
    return nodes or [_p("記憶庫是空的。")]


def _location_nodes(locations):
    if not locations:
        return [_p("目前沒有儲存地點。")]
    return [_ul([
        _li(
            _node("b", [loc.name]),
            _node("br"),
            f"{loc.latitude:.6f}, {loc.longitude:.6f}",
            *([_node("br"), loc.address] if loc.address else []),
        )
        for loc in locations
    ])]


def _get_access_token(user_id, setting):
    if TELEGRAPH_ACCESS_TOKEN:
        return TELEGRAPH_ACCESS_TOKEN
    if getattr(setting, "telegraph_access_token", None):
        return setting.telegraph_access_token
    response = requests.post(
        f"{TELEGRAPH_API}/createAccount",
        data={"short_name": "tg_remine", "author_name": TELEGRAPH_AUTHOR_NAME},
        timeout=10,
    )
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Telegraph createAccount failed"))
    access_token = data["result"]["access_token"]
    update_user_setting(user_id, telegraph_access_token=access_token)
    return access_token


def _telegraph_request(method, payload):
    response = requests.post(f"{TELEGRAPH_API}/{method}", data=payload, timeout=15)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", f"Telegraph {method} failed"))
    return data["result"]


def publish_telegraph_list_page(user_id):
    setting = get_user_setting(user_id)
    reminders = [
        ev for ev in get_user_events(str(user_id))
        if ev.is_recurring or (not ev.reminder_sent and ev.reminder_time is not None)
    ]
    memories = list_memories(user_id)
    trackers = get_trackers(user_id)
    locations = get_locations(user_id)
    generated_at = _now_taipei().strftime("%Y/%m/%d %H:%M")

    content = [
        _p("GD牌提醒機器人 · ", generated_at),
        _dashboard_counts(reminders, memories, trackers, locations),
        _node("hr"),
        _h3("提醒清單"),
        *_reminder_nodes(reminders),
        _h3("追蹤清單"),
        *_tracker_nodes(trackers),
        _h3("記憶清單"),
        *_memory_nodes(memories),
        _h3("地點清單"),
        *_location_nodes(locations),
    ]

    access_token = _get_access_token(user_id, setting)
    content_json = json.dumps(content, ensure_ascii=False)
    common_payload = {
        "access_token": access_token,
        "title": "清單紀錄",
        "author_name": TELEGRAPH_AUTHOR_NAME,
        "content": content_json,
        "return_content": "false",
    }

    path = getattr(setting, "telegraph_path", None)
    if path:
        try:
            page = _telegraph_request("editPage", {"path": path, **common_payload})
            update_user_setting(user_id, telegraph_path=page.get("path"), telegraph_url=page.get("url"))
            return page["url"]
        except RuntimeError:
            update_user_setting(user_id, telegraph_path=None, telegraph_url=None)

    page = _telegraph_request("createPage", common_payload)
    update_user_setting(user_id, telegraph_path=page.get("path"), telegraph_url=page.get("url"))
    return page["url"]

# telegraph_pages.py
import html
import json
import os
from datetime import date, datetime, timedelta

import pytz
import requests

from db import (
    get_locations, get_trackers, get_user_events, get_user_setting,
    event_effective_status, is_active_event, list_memories, update_user_setting,
)

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TELEGRAPH_ACCESS_TOKEN = os.environ.get("TELEGRAPH_ACCESS_TOKEN", "")
TELEGRAPH_AUTHOR_NAME = os.environ.get("TELEGRAPH_AUTHOR_NAME", "GD牌提醒機器人")
TELEGRAPH_API = "https://api.telegra.ph"
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]
MEMORY_HTML_PREFIX = "__TG_MEMORY_HTML__\n"
STATUS_LABELS = {
    "pending": "待提醒",
    "sent": "已提醒",
    "snoozed": "已延後",
    "confirmed": "已確認",
    "completed": "已完成",
    "failed": "失敗",
}


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


def _a(text, href):
    return _node("a", [text], {"href": href})


def _li(*children):
    return _node("li", list(children))


def _ul(items):
    return _node("ul", items)


def _section_nav(reminder_url=None, tracker_url=None, memory_url=None, location_url=None):
    reminder = _a("提醒", reminder_url) if reminder_url else _node("b", ["提醒"])
    tracker = _a("追蹤", tracker_url) if tracker_url else "追蹤"
    memory = _a("記憶", memory_url) if memory_url else "記憶"
    location = _a("地點", location_url) if location_url else "地點"
    return _p(
        reminder, "　",
        tracker, "　",
        memory, "　",
        location,
    )


def _section_title(text):
    return _h3(text)


def _page_header(title, generated_at):
    return [
        _p(_node("b", [title])),
        _p("更新 ", generated_at),
    ]


def _snapshot_note():
    return _p("這是唯讀快照，用來快速閱讀與分享；新增、修改、完成提醒請回 Telegram 或 Web 儀表板操作。")


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


def _as_taipei(value):
    if not value:
        return None
    try:
        return value.astimezone(TAIPEI_TZ)
    except Exception:
        return value


def _short_dt(value):
    dt = _as_taipei(value)
    if not dt:
        return "未設定"
    try:
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(value)


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


def _reminder_line(ev, show_date=False):
    dt = _as_taipei(ev.reminder_time)
    if show_date:
        time_label = _short_dt(dt)
    else:
        time_label = dt.strftime("%H:%M") if dt else "未設定"
    status = STATUS_LABELS.get(event_effective_status(ev), "待提醒")
    priority = "重要 · " if ev.priority_level else ""
    return _li(_node("b", [time_label]), "　", priority, status, "　", ev.event_content or "(無內容)")


def _recurring_line(ev):
    days, time_str = _parse_recurring_rule(ev.recurrence_rule)
    day_label = "、".join(_weekday_names(days)) if days else "週期"
    return _li(_node("b", [time_str]), "　每", day_label, "　", ev.event_content or "(無內容)")


def _reminder_nodes(reminders):
    if not reminders:
        return [_p("目前沒有進行中的提醒。")]

    now = _now_taipei()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)
    dated = []
    recurring = []

    for ev in reminders:
        if ev.is_recurring:
            recurring.append(ev)
        elif ev.reminder_time:
            dated.append(ev)

    dated.sort(key=lambda ev: _as_taipei(ev.reminder_time) or datetime.max.replace(tzinfo=TAIPEI_TZ))
    nodes = []

    groups = [
        ("逾期/未處理", lambda d: d < today, True),
        ("今天", lambda d: d == today, False),
        ("明天", lambda d: d == tomorrow, False),
        ("接下來 7 天", lambda d: tomorrow < d <= next_week, True),
        ("更晚", lambda d: d > next_week, True),
    ]
    for title, pred, show_date in groups:
        items = []
        for ev in dated:
            dt = _as_taipei(ev.reminder_time)
            status = event_effective_status(ev)
            matched = status != "sent" and bool(dt and pred(dt.date()))
            if matched:
                items.append(_reminder_line(ev, show_date=show_date))
        if items:
            nodes.append(_h4(title))
            nodes.append(_ul(items))

    if recurring:
        recurring_items = []
        for ev in recurring:
            recurring_items.append(_recurring_line(ev))
        nodes.append(_h4("週期提醒"))
        nodes.append(_ul(recurring_items))

    return nodes or [_p("目前沒有進行中的提醒。")]


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


def _tracker_due_label(t):
    if t.expire_date:
        return t.expire_date.strftime("%m/%d")
    if t.recurring_month and t.recurring_day:
        return f"{int(t.recurring_month):02d}/{int(t.recurring_day):02d}"
    return "未設定"


def _tracker_brief(t):
    bits = [_tracker_due_label(t)]
    if t.amount is not None:
        cycle_label = {"monthly": "月", "yearly": "年", "once": "次"}
        cycle = cycle_label.get(t.cycle, "")
        bits.append(f"{t.amount:.0f}元/{cycle}" if cycle else f"{t.amount:.0f}元")
    elif t.stock_total and t.stock_daily:
        bits.append(f"{t.stock_total:g}/{t.stock_daily:g}")
    return "　".join(bits)


def _tracker_sort_key(t):
    if t.expire_date:
        return (0, t.expire_date)
    if t.recurring_month and t.recurring_day:
        today = _now_taipei().date()
        candidate = date(today.year, int(t.recurring_month), int(t.recurring_day))
        if candidate < today:
            candidate = date(today.year + 1, int(t.recurring_month), int(t.recurring_day))
        return (1, candidate)
    return (2, date.max)


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
        items = sorted([t for t in trackers if t.category == category], key=_tracker_sort_key)
        if not items:
            continue
        nodes.append(_h4(category_label.get(category, category)))
        nodes.append(_ul([
            _li(_node("b", [t.name]), _node("br"), _tracker_meta(t))
            for t in items
        ]))
    return nodes


def _tracker_preview_nodes(trackers, limit=6):
    if not trackers:
        return [_p("追蹤清單是空的。")]
    items = sorted(trackers, key=_tracker_sort_key)[:limit]
    nodes = [_ul([
        _li(_node("b", [t.name]), "　", _tracker_brief(t))
        for t in items
    ])]
    if len(trackers) > limit:
        nodes.append(_p(f"還有 {len(trackers) - limit} 筆，請開啟追蹤頁查看。"))
    return nodes


def _memory_nodes(memories):
    if not memories:
        return [_p("記憶庫是空的。")]
    items = []
    for mem in memories:
        keyword = (mem.keyword or "").strip()
        if not keyword:
            continue
        plain = _memory_plain(mem.content).strip().replace("\n", " ")
        if len(plain) > 36:
            plain = plain[:36].rstrip() + "..."
        items.append(_li(_node("b", [keyword]), *([_node("br"), plain] if plain else [])))
    return [_ul(items)] if items else [_p("記憶庫是空的。")]


def _memory_preview_nodes(memories, limit=12):
    keywords = [(mem.keyword or "").strip() for mem in memories]
    keywords = [keyword for keyword in keywords if keyword][:limit]
    if not keywords:
        return [_p("記憶庫是空的。")]
    nodes = [_ul([_li(keyword) for keyword in keywords])]
    if len(memories) > limit:
        nodes.append(_p(f"還有 {len(memories) - limit} 筆，請開啟記憶頁查看。"))
    return nodes


def _location_nodes(locations):
    if not locations:
        return [_p("目前沒有儲存地點。")]
    return [_ul([
        _li(
            _node("b", [loc.name]),
            *([_node("br"), loc.address] if loc.address else [
                _node("br"), f"{loc.latitude:.6f}, {loc.longitude:.6f}",
            ]),
        )
        for loc in locations
    ])]


def _location_preview_nodes(locations, limit=8):
    items = list(locations)[:limit]
    if not items:
        return [_p("目前沒有儲存地點。")]
    nodes = [_ul([
        _li(_node("b", [loc.name]))
        for loc in items
    ])]
    if len(locations) > limit:
        nodes.append(_p(f"還有 {len(locations) - limit} 筆，請開啟地點頁查看。"))
    return nodes


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


def _upsert_page(user_id, title, content, access_token, path_attr, url_attr):
    common_payload = {
        "access_token": access_token,
        "title": title,
        "author_name": TELEGRAPH_AUTHOR_NAME,
        "content": json.dumps(content, ensure_ascii=False),
        "return_content": "false",
    }
    setting = get_user_setting(user_id)
    path = getattr(setting, path_attr, None)
    if path:
        try:
            page = _telegraph_request("editPage", {"path": path, **common_payload})
            update_user_setting(user_id, **{path_attr: page.get("path"), url_attr: page.get("url")})
            return page["url"]
        except RuntimeError:
            update_user_setting(user_id, **{path_attr: None, url_attr: None})

    page = _telegraph_request("createPage", common_payload)
    update_user_setting(user_id, **{path_attr: page.get("path"), url_attr: page.get("url")})
    return page["url"]


def publish_telegraph_list_page(user_id):
    setting = get_user_setting(user_id)
    reminders = [
        ev for ev in get_user_events(str(user_id))
        if is_active_event(ev)
    ]
    memories = list_memories(user_id)
    trackers = get_trackers(user_id)
    locations = get_locations(user_id)
    generated_at = _now_taipei().strftime("%m/%d %H:%M")
    access_token = _get_access_token(user_id, setting)
    main_url = getattr(setting, "telegraph_url", None)

    tracker_url = _upsert_page(
        user_id,
        "追蹤清單",
        [
            *_page_header("追蹤清單", generated_at),
            _snapshot_note(),
            _section_nav(reminder_url=main_url),
            _node("hr"),
            *_tracker_nodes(trackers),
        ],
        access_token,
        "telegraph_trackers_path",
        "telegraph_trackers_url",
    )
    memory_url = _upsert_page(
        user_id,
        "記憶清單",
        [
            *_page_header("記憶清單", generated_at),
            _snapshot_note(),
            _section_nav(reminder_url=main_url, tracker_url=tracker_url),
            _node("hr"),
            *_memory_nodes(memories),
        ],
        access_token,
        "telegraph_memories_path",
        "telegraph_memories_url",
    )
    location_url = _upsert_page(
        user_id,
        "地點清單",
        [
            *_page_header("地點清單", generated_at),
            _snapshot_note(),
            _section_nav(reminder_url=main_url, tracker_url=tracker_url, memory_url=memory_url),
            _node("hr"),
            *_location_nodes(locations),
        ],
        access_token,
        "telegraph_locations_path",
        "telegraph_locations_url",
    )

    content = [
        *_page_header("生活快照", generated_at),
        _snapshot_note(),
        _dashboard_counts(reminders, memories, trackers, locations),
        _section_nav(tracker_url=tracker_url, memory_url=memory_url, location_url=location_url),
        _node("hr"),
        _section_title("提醒"),
        *_reminder_nodes(reminders),
        _section_title("追蹤"),
        *_tracker_preview_nodes(trackers),
        _p(_a("開啟追蹤頁", tracker_url)),
        _section_title("記憶"),
        *_memory_preview_nodes(memories),
        _p(_a("開啟記憶頁", memory_url)),
        _section_title("地點"),
        *_location_preview_nodes(locations),
        _p(_a("開啟地點頁", location_url)),
    ]

    content_json = json.dumps(content, ensure_ascii=False)
    common_payload = {
        "access_token": access_token,
        "title": "生活快照",
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

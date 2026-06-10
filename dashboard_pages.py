# dashboard_pages.py
import html
import os
import secrets
from datetime import date, datetime, timedelta

import pytz

from db import (
    get_locations,
    get_trackers,
    get_user_events,
    get_user_setting,
    get_user_setting_by_dashboard_token,
    event_effective_status,
    is_active_event,
    list_memories,
    update_user_setting,
)

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
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


def _base_url():
    explicit = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
    if explicit:
        return explicit
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if webhook_url.endswith("/webhook"):
        return webhook_url[:-8]
    return webhook_url or "https://tg-remine.fly.dev"


def ensure_dashboard_url(user_id):
    setting = get_user_setting(user_id)
    token = getattr(setting, "dashboard_token", None)
    if not token:
        token = secrets.token_urlsafe(24)
        update_user_setting(user_id, dashboard_token=token)
    return f"{_base_url()}/dashboard/{token}"


def _now_taipei():
    return datetime.now(TAIPEI_TZ)


def _as_taipei(value):
    if not value:
        return None
    try:
        return value.astimezone(TAIPEI_TZ)
    except Exception:
        return value


def _fmt_date(value):
    if not value:
        return "未設定"
    if isinstance(value, datetime):
        value = _as_taipei(value)
        return value.strftime("%m/%d %H:%M")
    return value.strftime("%m/%d")


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


def _tracker_due_date(t):
    if t.expire_date:
        return t.expire_date
    if t.recurring_month and t.recurring_day:
        today = _now_taipei().date()
        candidate = date(today.year, int(t.recurring_month), int(t.recurring_day))
        if candidate < today:
            candidate = date(today.year + 1, int(t.recurring_month), int(t.recurring_day))
        return candidate
    return None


def _tracker_sort_key(t):
    due = _tracker_due_date(t)
    return due or date.max


def _tracker_type(t):
    return {
        "subscription": "訂閱",
        "contract": "合約",
        "anniversary": "紀念日",
        "medicine": "藥物",
    }.get(t.category, t.category or "追蹤")


def _tracker_detail(t):
    bits = []
    if t.amount is not None:
        cycle = {"monthly": "月", "yearly": "年", "once": "次"}.get(t.cycle, "")
        bits.append(f"{t.amount:.0f}元/{cycle}" if cycle else f"{t.amount:.0f}元")
    if t.stock_total and t.stock_daily:
        bits.append(f"庫存 {t.stock_total:g} / 每日 {t.stock_daily:g}")
    if t.remind_days is not None:
        bits.append("不提醒" if t.remind_days < 0 else f"提前 {t.remind_days} 天")
    return " · ".join(bits) or "未設定"


def _event_groups(reminders):
    today = _now_taipei().date()
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)
    groups = {
        "逾期/未處理": [],
        "今天": [],
        "明天": [],
        "接下來 7 天": [],
        "更晚": [],
        "週期": [],
    }
    for ev in reminders:
        if ev.is_recurring:
            groups["週期"].append(ev)
            continue
        dt = _as_taipei(ev.reminder_time)
        if not dt:
            continue
        ev_date = dt.date()
        status = event_effective_status(ev)
        if ev_date < today:
            groups["逾期/未處理"].append(ev)
        elif ev_date == today:
            groups["今天"].append(ev)
        elif ev_date == tomorrow:
            groups["明天"].append(ev)
        elif ev_date <= next_week:
            groups["接下來 7 天"].append(ev)
        else:
            groups["更晚"].append(ev)
    for name in groups:
        groups[name].sort(key=lambda ev: _as_taipei(ev.reminder_time) or datetime.max.replace(tzinfo=TAIPEI_TZ))
    return groups


def _esc(value):
    return html.escape(str(value or ""))


def _reminder_rows(groups):
    rows = []
    for group, events in groups.items():
        if not events:
            continue
        group_key = {
            "今天": "today",
            "逾期/未處理": "overdue",
            "明天": "tomorrow",
            "接下來 7 天": "week",
            "更晚": "later",
            "週期": "recurring",
        }.get(group, "other")
        rows.append(f'<tr class="group" data-group="{group_key}"><th colspan="3">{_esc(group)}</th></tr>')
        for ev in events:
            searchable = f"{ev.event_content or ''} {group}"
            row_scope = group_key
            if ev.is_recurring:
                days, time_str = _parse_recurring_rule(ev.recurrence_rule)
                day_label = "、".join(_weekday_names(days)) if days else "週期"
                when = f"{time_str} / 每{day_label}"
            else:
                dt = _as_taipei(ev.reminder_time)
                when = dt.strftime("%H:%M") if group in ("今天", "明天") else _fmt_date(dt)
            status = "週期" if ev.is_recurring else STATUS_LABELS.get(event_effective_status(ev), "待提醒")
            priority = "重要" if ev.priority_level else ""
            meta = " · ".join([item for item in (priority, status) if item])
            rows.append(
                f'<tr class="data-row reminder-row" data-kind="reminder" data-group="{row_scope}" '
                f'data-important="{1 if ev.priority_level else 0}" data-search="{_esc(searchable.lower())}">'
                f"<td class=\"when\">{_esc(when)}</td>"
                f"<td>{_esc(ev.event_content or '(無內容)')}</td>"
                f"<td class=\"muted\">{_esc(meta)}</td>"
                "</tr>"
            )
    return "\n".join(rows) or '<tr><td colspan="3" class="empty">目前沒有進行中的提醒。</td></tr>'


def _tracker_rows(trackers):
    rows = []
    for t in sorted(trackers, key=_tracker_sort_key):
        due = _tracker_due_date(t)
        rows.append(
            f'<tr class="data-row tracker-row" data-kind="tracker" data-type="{_esc(t.category)}" '
            f'data-search="{_esc((t.name or "").lower())}">'
            f"<td>{_esc(t.name)}</td>"
            f"<td>{_esc(_tracker_type(t))}</td>"
            f"<td>{_esc(_fmt_date(due))}</td>"
            f"<td class=\"muted\">{_esc(_tracker_detail(t))}</td>"
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="4" class="empty">追蹤清單是空的。</td></tr>'


def _memory_rows(memories):
    rows = []
    for mem in memories:
        plain = _memory_plain(mem.content).strip().replace("\n", " ")
        if len(plain) > 80:
            plain = plain[:80].rstrip() + "..."
        rows.append(
            f'<tr class="data-row memory-row" data-kind="memory" data-search="{_esc((mem.keyword or plain).lower())}">'
            f"<td>{_esc(mem.keyword)}</td>"
            f"<td class=\"muted\">{_esc(plain)}</td>"
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="2" class="empty">記憶庫是空的。</td></tr>'


def _location_rows(locations):
    rows = []
    for loc in locations:
        place = loc.address or f"{loc.latitude:.6f}, {loc.longitude:.6f}"
        rows.append(
            f'<tr class="data-row location-row" data-kind="location" data-search="{_esc((loc.name or place).lower())}">'
            f"<td>{_esc(loc.name)}</td>"
            f"<td class=\"muted\">{_esc(place)}</td>"
            "</tr>"
        )
    return "\n".join(rows) or '<tr><td colspan="2" class="empty">目前沒有儲存地點。</td></tr>'


def render_dashboard_page(token, notice=None):
    setting = get_user_setting_by_dashboard_token(token)
    if not setting:
        return None
    user_id = setting.user_id
    reminders = [
        ev for ev in get_user_events(str(user_id))
        if is_active_event(ev)
    ]
    trackers = get_trackers(user_id)
    memories = list_memories(user_id)
    locations = get_locations(user_id)
    groups = _event_groups(reminders)
    generated_at = _now_taipei().strftime("%Y/%m/%d %H:%M")
    upcoming = [t for t in sorted(trackers, key=_tracker_sort_key) if _tracker_due_date(t)][:5]

    focus_items = []
    for ev in groups["今天"][:3]:
        dt = _as_taipei(ev.reminder_time)
        focus_items.append(f"<li><b>{dt.strftime('%H:%M')}</b> {_esc(ev.event_content)}</li>")
    for t in upcoming[:3]:
        focus_items.append(f"<li><b>{_esc(_fmt_date(_tracker_due_date(t)))}</b> {_esc(t.name)}</li>")
    focus_html = "\n".join(focus_items) or "<li>目前沒有需要特別注意的項目。</li>"

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <title>生活儀表板</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #20242a;
      --muted: #6b7280;
      --line: #e5e7eb;
      --accent: #176b87;
      --accent-soft: #e8f4f7;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    .shell {{ max-width: 1120px; margin: 0 auto; padding: 24px 16px 48px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; margin-bottom: 18px; }}
    h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    .updated {{ color: var(--muted); font-size: 14px; }}
    nav {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }}
    nav a {{
      color: var(--accent);
      background: var(--accent-soft);
      padding: 7px 11px;
      border-radius: 6px;
      text-decoration: none;
      font-size: 14px;
    }}
    .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) auto auto auto;
      gap: 8px;
      align-items: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      margin-bottom: 14px;
    }}
    input, select, button {{
      font: inherit;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      min-height: 34px;
    }}
    input, select {{ padding: 6px 8px; }}
    button {{
      padding: 6px 10px;
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      cursor: pointer;
    }}
    label.inline {{ color: var(--muted); font-size: 14px; white-space: nowrap; }}
    .notice {{
      background: #ecfdf3;
      border: 1px solid #b7ebc6;
      color: #166534;
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 14px;
    }}
    .stat, section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, .04);
    }}
    .stat {{ padding: 12px 14px; }}
    .stat b {{ display: block; font-size: 24px; }}
    .stat span {{ color: var(--muted); font-size: 13px; }}
    .grid {{ display: grid; grid-template-columns: 1.35fr .9fr; gap: 14px; align-items: start; }}
    section {{ padding: 16px; margin-bottom: 14px; overflow: hidden; }}
    .focus ul {{ margin: 0; padding-left: 20px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; font-size: 12px; }}
    tr:last-child td {{ border-bottom: 0; }}
    .group th {{ background: #fafafa; color: var(--text); font-size: 13px; }}
    .when {{ white-space: nowrap; font-weight: 650; }}
    .muted {{ color: var(--muted); }}
    .empty {{ color: var(--muted); text-align: center; padding: 24px 8px; }}
    .is-hidden {{ display: none; }}
    @media (max-width: 760px) {{
      .shell {{ padding: 18px 12px 36px; }}
      header {{ display: block; }}
      h1 {{ font-size: 24px; margin-bottom: 4px; }}
      .stats {{ grid-template-columns: repeat(2, 1fr); }}
      .grid {{ display: block; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      table {{ font-size: 13px; }}
      th, td {{ padding: 8px 6px; }}
      .hide-mobile {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <h1>生活儀表板</h1>
        <div class="updated">更新 {generated_at}</div>
      </div>
      <div class="updated">私人連結</div>
    </header>
    <nav>
      <a href="#reminders">提醒</a>
      <a href="#trackers">追蹤</a>
      <a href="#memories">記憶</a>
      <a href="#locations">地點</a>
    </nav>
    <div class="stats">
      <div class="stat"><b>{len(reminders)}</b><span>提醒</span></div>
      <div class="stat"><b>{len(trackers)}</b><span>追蹤</span></div>
      <div class="stat"><b>{len(memories)}</b><span>記憶</span></div>
      <div class="stat"><b>{len(locations)}</b><span>地點</span></div>
    </div>
    {f'<div class="notice">{_esc(notice)}</div>' if notice else ''}
    <div class="toolbar">
      <input id="searchBox" type="search" placeholder="搜尋提醒、追蹤、記憶、地點">
      <select id="reminderRange" aria-label="提醒範圍">
        <option value="all">提醒：全部</option>
        <option value="today">只看今天</option>
        <option value="week">只看本週</option>
      </select>
      <select id="trackerType" aria-label="追蹤類型">
        <option value="all">追蹤：全部</option>
        <option value="subscription">訂閱</option>
        <option value="contract">合約</option>
        <option value="anniversary">紀念日</option>
        <option value="medicine">藥物</option>
      </select>
      <label class="inline"><input id="importantOnly" type="checkbox"> 只看重要提醒</label>
    </div>
    <div class="grid">
      <main>
        <section id="reminders">
          <h2>提醒</h2>
          <table>
            <tbody>
              {_reminder_rows(groups)}
            </tbody>
          </table>
        </section>
        <section id="trackers">
          <h2>追蹤</h2>
          <table>
            <thead><tr><th>名稱</th><th>類型</th><th>日期</th><th class="hide-mobile">細節</th></tr></thead>
            <tbody>
              {_tracker_rows(trackers)}
            </tbody>
          </table>
        </section>
      </main>
      <aside>
        <section class="focus">
          <h2>今日焦點</h2>
          <ul>{focus_html}</ul>
        </section>
        <section id="memories">
          <h2>記憶</h2>
          <table>
            <tbody>
              {_memory_rows(memories)}
            </tbody>
          </table>
        </section>
        <section id="locations">
          <h2>地點</h2>
          <table>
            <tbody>
              {_location_rows(locations)}
            </tbody>
          </table>
        </section>
      </aside>
    </div>
  </div>
  <script>
    const searchBox = document.getElementById('searchBox');
    const reminderRange = document.getElementById('reminderRange');
    const trackerType = document.getElementById('trackerType');
    const importantOnly = document.getElementById('importantOnly');

    function rowMatchesSearch(row, query) {{
      if (!query) return true;
      return (row.dataset.search || row.textContent || '').toLowerCase().includes(query);
    }}

    function reminderMatches(row) {{
      if (!row.classList.contains('reminder-row')) return true;
      const range = reminderRange.value;
      const group = row.dataset.group || '';
      if (importantOnly.checked && row.dataset.important !== '1') return false;
      if (range === 'today') return group === 'today';
      if (range === 'week') return ['today', 'tomorrow', 'week'].includes(group);
      return true;
    }}

    function trackerMatches(row) {{
      if (!row.classList.contains('tracker-row')) return true;
      return trackerType.value === 'all' || row.dataset.type === trackerType.value;
    }}

    function refreshGroups() {{
      document.querySelectorAll('tr.group').forEach(groupRow => {{
        const group = groupRow.dataset.group;
        let next = groupRow.nextElementSibling;
        let hasVisible = false;
        while (next && !next.classList.contains('group')) {{
          if (next.classList.contains('reminder-row') && !next.classList.contains('is-hidden')) {{
            hasVisible = true;
            break;
          }}
          next = next.nextElementSibling;
        }}
        groupRow.classList.toggle('is-hidden', !hasVisible && group);
      }});
    }}

    function applyFilters() {{
      const query = searchBox.value.trim().toLowerCase();
      document.querySelectorAll('tr.data-row').forEach(row => {{
        const visible = rowMatchesSearch(row, query) && reminderMatches(row) && trackerMatches(row);
        row.classList.toggle('is-hidden', !visible);
      }});
      refreshGroups();
    }}

    [searchBox, reminderRange, trackerType, importantOnly].forEach(control => {{
      control.addEventListener('input', applyFilters);
      control.addEventListener('change', applyFilters);
    }});
  </script>
</body>
</html>"""

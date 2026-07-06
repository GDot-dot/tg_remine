# handlers/reminder_parsing.py
import re
from datetime import datetime, timedelta

from scheduler import TAIPEI_TZ

WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


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


def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


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


def parse_event_datetime_input(value: str, base_dt: datetime | None = None) -> datetime | None:
    text = normalize_reminder_text(value)

    date_base, _ = _parse_natural_date(text)
    time_parts, _ = _parse_natural_time(text)
    if date_base and time_parts:
        hour, minute = time_parts
        return date_base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    absolute_dt = parse_absolute_datetime_input(text)
    if absolute_dt:
        return absolute_dt

    time_str = parse_hhmm(text)
    if time_str and base_dt:
        hour, minute = map(int, time_str.split(":"))
        return base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)

    return None


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
        if date_part.count("-") == 1:
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


def normalize_reminder_text(text: str) -> str:
    text = text.strip().translate(FULLWIDTH_DIGITS)
    replacements = [
        ("下周", "下週"),
        ("本周", "這週"),
        ("今早", "今天早上"),
        ("今晨", "今天清晨"),
        ("今午", "今天中午"),
        ("今晚", "今天晚上"),
        ("明早", "明天早上"),
        ("明晨", "明天清晨"),
        ("明午", "明天中午"),
        ("明晚", "明天晚上"),
        ("後早", "後天早上"),
        ("後晚", "後天晚上"),
    ]
    for src, dst in replacements:
        text = text.replace(src, dst)
    return text


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
        (r"下(?:週|周|星期|禮拜)([一二三四五六日天1-7])", lambda m: TAIPEI_TZ.localize(_next_weekday(ZH_WEEKDAYS[m.group(1)], True))),
        (r"(?:這|本)?(?:週|周|星期|禮拜)([一二三四五六日天1-7])", lambda m: TAIPEI_TZ.localize(_next_weekday(ZH_WEEKDAYS[m.group(1)], False))),
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
    if period in ("下午", "傍晚", "晚上", "晚間") and 1 <= hour < 12:
        return hour + 12
    if period == "中午" and 1 <= hour < 11:
        return hour + 12
    if period in ("凌晨", "半夜") and hour == 12:
        return 0
    return hour


def _parse_natural_time(text: str) -> tuple[tuple[int, int] | None, str | None]:
    period_re = r"(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|晚間|半夜)?"
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

    m = re.search(r"(凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|晚間|半夜)", text)
    if m:
        defaults = {
            "凌晨": (0, 0), "清晨": (7, 0), "早上": (8, 0), "上午": (9, 0),
            "中午": (12, 0), "下午": (14, 0), "傍晚": (18, 0),
            "晚上": (19, 0), "晚間": (19, 0), "半夜": (0, 0),
        }
        return defaults[m.group(1)], m.group(0)

    return None, None


def parse_natural_reminder_text(text: str):
    raw = normalize_reminder_text(text)
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
    content = re.sub(r"^(請|麻煩|幫我|幫忙)\s*", " ", content)
    content = re.sub(r"^(重要提醒|提醒)\s*(我|自己|本人)?\s*", " ", content)
    content = re.sub(r"(提醒我|提醒一下我|提醒一下|提醒|叫我|記得要|記得)", " ", content)
    content = re.sub(r"^(請|麻煩|幫我|幫忙)\s*", " ", content)
    content = re.sub(r"[，,。；;：:]\s*", " ", content)
    content = re.sub(r"\s+", " ", content).strip()
    content = content or "提醒事項"
    return "我", dt, content


_REMINDER_RE = re.compile(
    r"^(?:提醒|重要提醒)(.*?)\s*"
    r"(今天|明天|後天|\d{1,4}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)"
    r"\s*(\d{1,2}:\d{2})?"
    r"\s*(.+)$"
)


def parse_reminder_text(text: str):
    """回傳 (who, event_dt, content) 或 (None, None, None)"""
    m = _REMINDER_RE.match(text)
    if not m:
        return parse_natural_reminder_text(text)
    who_raw, date_s, time_s, content = m.groups()
    who = who_raw.strip() or "我"
    dt = parse_dt_from_parts(date_s, time_s)
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
    normalized = normalize_reminder_text(text)
    date_base, _ = _parse_natural_date(normalized)
    time_parts, _ = _parse_natural_time(normalized)
    return bool(date_base and time_parts)


def looks_like_important_reminder(text: str) -> bool:
    normalized = normalize_reminder_text(text)
    if "重要提醒" not in normalized:
        return False
    return looks_like_natural_reminder(normalized)

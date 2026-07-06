# scheduler.py
import os, logging, threading, html, time, re
from datetime import datetime, timedelta
import requests, pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from db import (
    engine, SessionLocal, Event, mark_reminder_sent,
    decrease_remaining_repeats, update_event_fields,
    get_user_setting, list_user_settings, update_user_setting,
)

logger = logging.getLogger(__name__)
TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CWA_AUTHORIZATION = os.environ.get("CWA_AUTHORIZATION") or os.environ.get("CWA_API_KEY", "")
TG_RETRY_DELAYS = (1, 3, 5)
DB_OPERATION_RETRY_DELAYS = (1, 3, 5)
REMINDER_RETRY_DELAY = 5
RECURRING_RETRY_LIMIT = 3
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CWA_CITY_ALIASES = {
    "台北": "臺北市", "臺北": "臺北市", "台北市": "臺北市",
    "新北": "新北市", "桃園": "桃園市", "台中": "臺中市", "臺中": "臺中市",
    "台南": "臺南市", "臺南": "臺南市", "高雄": "高雄市",
    "基隆": "基隆市", "新竹": "新竹市", "嘉義": "嘉義市",
    "宜蘭": "宜蘭縣", "苗栗": "苗栗縣", "彰化": "彰化縣",
    "南投": "南投縣", "雲林": "雲林縣", "屏東": "屏東縣",
    "花蓮": "花蓮縣", "台東": "臺東縣", "臺東": "臺東縣",
    "澎湖": "澎湖縣", "金門": "金門縣", "連江": "連江縣", "馬祖": "連江縣",
}
CWA_COUNTY_DATASET_IDS = {
    "宜蘭縣": "F-D0047-001", "桃園市": "F-D0047-005", "新竹縣": "F-D0047-009",
    "苗栗縣": "F-D0047-013", "彰化縣": "F-D0047-017", "南投縣": "F-D0047-021",
    "雲林縣": "F-D0047-025", "嘉義縣": "F-D0047-029", "屏東縣": "F-D0047-033",
    "臺東縣": "F-D0047-037", "花蓮縣": "F-D0047-041", "澎湖縣": "F-D0047-045",
    "基隆市": "F-D0047-049", "新竹市": "F-D0047-053", "嘉義市": "F-D0047-057",
    "臺北市": "F-D0047-061", "高雄市": "F-D0047-065", "新北市": "F-D0047-069",
    "臺中市": "F-D0047-073", "臺南市": "F-D0047-077", "連江縣": "F-D0047-081",
    "金門縣": "F-D0047-085",
}

PRIORITY_RULES = {
    1: {"icon": "🟢", "interval": 30, "repeats": 3},
    2: {"icon": "🟡", "interval": 10, "repeats": 6},
    3: {"icon": "🔴", "interval":  5, "repeats": 12},
}

jobstores    = {"default": SQLAlchemyJobStore(engine=engine)}
executors    = {"default": ThreadPoolExecutor(max_workers=5)}
job_defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 60}
scheduler_lock = threading.RLock()
scheduler = BackgroundScheduler(jobstores=jobstores, executors=executors, job_defaults=job_defaults, timezone=TAIPEI_TZ)

def _tg(method, **kwargs):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    last_error = None
    for attempt, delay in enumerate(TG_RETRY_DELAYS, start=1):
        try:
            response = requests.post(url, json=kwargs, timeout=10)
            data = response.json()
            if data.get("ok"):
                return data
            error_code = data.get("error_code", response.status_code)
            description = data.get("description", "")
            retryable = response.status_code >= 500 or error_code == 429
            logger.warning(
                "TG API %s failed attempt %s/%s: %s %s",
                method, attempt, len(TG_RETRY_DELAYS), error_code, description,
            )
            if not retryable:
                return data
            last_error = data
        except Exception as e:
            last_error = e
            logger.warning(
                "TG API %s error attempt %s/%s: %s",
                method, attempt, len(TG_RETRY_DELAYS), e,
            )
        if attempt < len(TG_RETRY_DELAYS):
            time.sleep(delay)
    logger.error(f"TG API ({method}) exhausted retries: {last_error}")
    return {}

def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    result = _tg("sendMessage", **payload)
    return bool(result.get("ok")), result

def _is_retryable_tg_result(result):
    if not result:
        return True
    error_code = result.get("error_code")
    return error_code == 429 or (isinstance(error_code, int) and error_code >= 500)

def _retry_reminder(event_id, reason):
    retry_at = datetime.now(TAIPEI_TZ) + timedelta(minutes=REMINDER_RETRY_DELAY)
    update_event_fields(event_id, reminder_time=retry_at, reminder_sent=0, event_status="pending", completed_at=None)
    safe_add_job(send_reminder, retry_at, [event_id], f"reminder_{event_id}")
    logger.warning(
        "Reminder %s send failed; retry scheduled at %s. reason=%s",
        event_id, retry_at.isoformat(), reason,
    )

def _finish_failed_reminder(db, event, reason):
    logger.error("Reminder %s failed permanently: %s", event.id, reason)
    update_event_fields(
        event.id,
        reminder_sent=1,
        event_status="failed",
        last_reminded_at=datetime.now(TAIPEI_TZ),
    )
    _remove_job(event.id)

def _parse_snooze_buttons(raw):
    buttons = []
    for part in (raw or "5,30,60").split(","):
        try:
            minutes = int(part.strip())
        except ValueError:
            continue
        if 1 <= minutes <= 1440 and minutes not in buttons:
            buttons.append(minutes)
    return buttons[:3] or [5, 30, 60]

def _snooze_label(minutes):
    if minutes % 1440 == 0:
        return f"延後{minutes // 1440}天"
    if minutes % 60 == 0:
        return f"延後{minutes // 60}小時"
    return f"延後{minutes}分"

def _confirm_kb(event):
    event_id = event.id if hasattr(event, "id") else event
    user_id = getattr(event, "creator_user_id", None)
    buttons = _parse_snooze_buttons(get_user_setting(user_id).snooze_buttons) if user_id else [5, 30, 60]
    rows = [[{"text": "✅ 確認收到", "callback_data": f"cr:{event_id}"}]]
    rows.append([
        {"text": f"💤 {_snooze_label(minutes)}", "callback_data": f"sn:{event_id}:{minutes}"}
        for minutes in buttons
    ])
    rows.append([{"text": "🕒 自訂延後", "callback_data": f"snc:{event_id}"}])
    return {"inline_keyboard": rows}

def _priority_interval(event):
    if event.recurrence_rule and event.recurrence_rule.startswith("custom:"):
        try:
            return int(event.recurrence_rule.split(":", 1)[1])
        except (TypeError, ValueError):
            pass
    return PRIORITY_RULES[event.priority_level]["interval"]

def _priority_kb(event):
    event_id = event.id
    user_id = getattr(event, "creator_user_id", None)
    buttons = _parse_snooze_buttons(get_user_setting(user_id).snooze_buttons) if user_id else [5, 30, 60]
    rows = [[{"text": "✅ 完成，停止重提", "callback_data": f"cr:{event_id}"}]]
    rows.append([
        {"text": f"💤 {_snooze_label(minutes)}", "callback_data": f"sn:{event_id}:{minutes}"}
        for minutes in buttons
    ])
    rows.append([{"text": "🕒 自訂延後", "callback_data": f"snc:{event_id}"}])
    return {"inline_keyboard": rows}

def _schedule_recurring_retry(event_id, retry_attempt, reason):
    if retry_attempt >= RECURRING_RETRY_LIMIT:
        logger.error(
            "Recurring reminder %s exhausted %s retries. reason=%s",
            event_id, RECURRING_RETRY_LIMIT, reason,
        )
        return
    next_attempt = retry_attempt + 1
    retry_at = datetime.now(TAIPEI_TZ) + timedelta(minutes=REMINDER_RETRY_DELAY)
    job_id = f"recurring_retry_{event_id}"
    scheduled = safe_add_job(send_reminder, retry_at, [event_id, next_attempt], job_id)
    if scheduled:
        logger.warning(
            "Recurring reminder %s failed; retry %s/%s scheduled at %s. reason=%s",
            event_id, next_attempt, RECURRING_RETRY_LIMIT,
            retry_at.isoformat(), reason,
        )
    else:
        logger.error("Could not schedule recurring retry for reminder %s", event_id)


def send_reminder(event_id, retry_attempt=0):
    db = SessionLocal()
    try:
        event = db.query(Event).filter(Event.id == event_id).first()
        if not event: _remove_job(event_id); return
        if getattr(event, "event_status", None) in ("completed", "failed", "deleted"):
            _remove_job(event_id)
            return
        if not event.is_recurring and event.reminder_sent: return

        chat_id = event.target_id
        name    = event.target_display_name or "您"
        content = event.event_content

        sent = False
        send_result = {}

        if event.priority_level > 0:
            icon = PRIORITY_RULES[event.priority_level]["icon"]
            interval = _priority_interval(event)
            repeats = max(event.remaining_repeats or 0, 0)
            repeat_line = (
                f"\n\n未完成會在 {interval} 分鐘後重提，剩餘 {repeats} 次。"
                if repeats > 0
                else "\n\n這是最後一次重提。"
            )
            sent, send_result = tg_send(
                chat_id,
                f"{icon} <b>重要提醒！</b>\n\n@{name}\n記得要「{content}」！{repeat_line}",
                _priority_kb(event),
            )
        elif event.is_recurring:
            sent, send_result = tg_send(chat_id, f"⏰ 週期提醒\n\n@{name}\n記得要「{content}」喔！", _confirm_kb(event))
        else:
            event_dt  = event.event_datetime.astimezone(TAIPEI_TZ)
            sent, send_result = tg_send(chat_id,
                f"⏰ 提醒時間到！\n\n@{name}\n📅 {event_dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}",
                _confirm_kb(event))

        if not sent:
            if not event.is_recurring:
                if _is_retryable_tg_result(send_result):
                    _retry_reminder(event_id, send_result)
                else:
                    _finish_failed_reminder(db, event, send_result)
            else:
                _schedule_recurring_retry(event_id, retry_attempt, send_result)
            return

        message_id = (send_result.get("result") or {}).get("message_id")
        logger.info(
            "Reminder delivered event_id=%s chat_id=%s message_id=%s recurring=%s retry_attempt=%s",
            event_id, chat_id, message_id, bool(event.is_recurring), retry_attempt,
        )

        reminded_at = datetime.now(TAIPEI_TZ)
        if event.is_recurring:
            update_event_fields(event_id, last_reminded_at=reminded_at)
            retry_job_id = f"recurring_retry_{event_id}"
            if scheduler.get_job(retry_job_id):
                scheduler.remove_job(retry_job_id)
        else:
            if event.priority_level > 0 and event.remaining_repeats > 0:
                decrease_remaining_repeats(event_id)
                interval = _priority_interval(event)
                next_run = reminded_at + timedelta(minutes=interval)
                update_event_fields(
                    event_id,
                    reminder_time=next_run,
                    event_status="pending",
                    last_reminded_at=reminded_at,
                )
                safe_add_job(send_reminder, next_run, [event_id], f"reminder_{event_id}")
            else:
                update_event_fields(event_id, event_status="sent", last_reminded_at=reminded_at)
                mark_reminder_sent(event_id)
                _remove_job(event_id)
    except Exception as e:
        logger.error(f"send_reminder (id={event_id}): {e}", exc_info=True)
    finally:
        db.close()

def safe_add_job(func, run_date, args, job_id):
    with scheduler_lock:
        try:
            if not scheduler.running: safe_start()
            scheduler.add_job(func, "date", run_date=run_date.astimezone(pytz.UTC),
                              args=args, id=job_id, replace_existing=True)
            return True
        except Exception as e:
            logger.error(f"safe_add_job ({job_id}): {e}"); return False

def safe_add_cron(func, args, job_id, day_of_week, hour, minute):
    with scheduler_lock:
        try:
            scheduler.add_job(func, "cron", args=args, id=job_id,
                              day_of_week=day_of_week, hour=hour, minute=minute,
                              timezone=TAIPEI_TZ, replace_existing=True)
            return True
        except Exception as e:
            logger.error(f"safe_add_cron ({job_id}): {e}"); return False

def safe_remove_job_id(job_id):
    with scheduler_lock:
        try:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            return True
        except Exception as e:
            logger.error(f"safe_remove_job_id ({job_id}): {e}"); return False

def _remove_job(event_id):
    for prefix in ("reminder_", "recurring_", "recurring_retry_"):
        jid = f"{prefix}{event_id}"
        if scheduler.get_job(jid):
            try: scheduler.remove_job(jid)
            except: pass

def remove_job(event_id):
    _remove_job(event_id)

def _restore_jobs_once():
    db = SessionLocal()
    try:
        now = datetime.now(TAIPEI_TZ)
        recurring  = db.query(Event).filter(Event.is_recurring == 1).all()
        pending_one = db.query(Event).filter(
            Event.reminder_sent == 0,
            Event.is_recurring == 0,
            Event.reminder_time.isnot(None),
            Event.event_status.in_(("pending", "snoozed")),
        ).all()
        count = 0
        overdue_count = 0
        for ev in recurring + pending_one:
            is_rec = ev.is_recurring
            job_id = f"{'recurring' if is_rec else 'reminder'}_{ev.id}"
            if scheduler.get_job(job_id): continue
            try:
                if is_rec and ev.recurrence_rule:
                    days, t = ev.recurrence_rule.split("|")
                    h, m = map(int, t.split(":"))
                    restored = safe_add_cron(send_reminder, [ev.id], job_id, days.lower(), h, m)
                else:
                    run_at = ev.reminder_time.astimezone(TAIPEI_TZ)
                    if run_at <= now:
                        run_at = now + timedelta(seconds=10 + overdue_count * 2)
                        overdue_count += 1
                        logger.warning("Restoring missed reminder %s for immediate retry.", ev.id)
                    restored = safe_add_job(send_reminder, run_at, [ev.id], job_id)
                if not restored:
                    raise RuntimeError(f"Could not restore scheduler job {job_id}")
                count += 1
            except RuntimeError:
                raise
            except Exception as e:
                logger.error(f"restore event {ev.id}: {e}")
        logger.info(f"✅ 還原完成，共 {count} 個排程。")
    finally:
        db.close()


def restore_jobs():
    last_error = None
    for attempt, delay in enumerate(DB_OPERATION_RETRY_DELAYS, start=1):
        try:
            _restore_jobs_once()
            return
        except Exception as e:
            last_error = e
            logger.warning(
                "restore_jobs failed attempt %s/%s: %s",
                attempt, len(DB_OPERATION_RETRY_DELAYS), e,
                exc_info=attempt == len(DB_OPERATION_RETRY_DELAYS),
            )
            if attempt < len(DB_OPERATION_RETRY_DELAYS):
                time.sleep(delay)
    logger.error("restore_jobs exhausted retries: %s", last_error)

def safe_start():
    with scheduler_lock:
        if not scheduler.running:
            scheduler.start()
            logger.info("Scheduler started.")
            threading.Thread(target=restore_jobs, daemon=True).start()

# ── Daily summaries and weather ──────────────────────────────────────────────

def _valid_hhmm(value):
    try:
        hour, minute = map(int, (value or "").split(":"))
        return 0 <= hour <= 23 and 0 <= minute <= 59
    except Exception:
        return False

def _normalize_cwa_city(city):
    cleaned = (city or "").strip().replace("台", "臺")
    return CWA_CITY_ALIASES.get(city, CWA_CITY_ALIASES.get(cleaned, cleaned))

def _parse_cwa_location(city):
    raw = (city or "").strip()
    if not raw:
        return "", ""
    normalized = raw.replace("台", "臺")
    parts = [p for p in normalized.replace("　", " ").split() if p]
    if len(parts) >= 2:
        return _normalize_cwa_city(parts[0]), parts[1]
    for county in sorted(CWA_COUNTY_DATASET_IDS, key=len, reverse=True):
        if normalized.startswith(county):
            district = normalized[len(county):].strip()
            return county, district
    return _normalize_cwa_city(normalized), ""

def _weather_advice(description, rain, temp_max, comfort=None):
    if rain is not None and rain >= 60:
        return "出門建議帶傘。"
    if any(word in (description or "") for word in ("雨", "雷")):
        return "留意雨勢，交通時間抓鬆一點。"
    if temp_max is not None and temp_max >= 30:
        return "天氣偏熱，記得補水防曬。"
    if temp_max is not None and temp_max <= 18:
        return "氣溫偏涼，可以多帶一件外套。"
    if comfort:
        return f"體感{comfort}。"
    return "天氣看起來穩定。"

def _cwa_get(data, *keys):
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data:
            return data.get(key)
    return None

def _cwa_as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def _cwa_locations(data):
    records = _cwa_get(data, "records", "Records") or {}
    locations = _cwa_get(records, "location", "Location")
    if locations is not None:
        return _cwa_as_list(locations)

    groups = _cwa_get(records, "locations", "Locations") or []
    result = []
    for group in _cwa_as_list(groups):
        result.extend(_cwa_as_list(_cwa_get(group, "location", "Location")))
    return result

def _cwa_location_name(location):
    return _cwa_get(location, "locationName", "LocationName")

def _normalize_cwa_name(value):
    return (value or "").strip().replace("　", "").replace(" ", "")

def _find_cwa_location(locations, location_name):
    normalized_name = _normalize_cwa_name(location_name)
    if not normalized_name:
        return locations[0] if locations else None

    for location in locations:
        if _cwa_location_name(location) == location_name:
            return location

    for location in locations:
        if _normalize_cwa_name(_cwa_location_name(location)) == normalized_name:
            return location

    return None

def _cwa_scalar(value):
    if value in (None, ""):
        return None
    if not isinstance(value, (dict, list)):
        return value
    return None

def _cwa_element_value(element_value):
    preferred_keys = (
        "value", "Value", "parameterName", "ParameterName",
        "PoP", "PoP12h", "PoP6h",
        "Weather", "WeatherDescription",
        "ProbabilityOfPrecipitation",
        "Temperature", "MaxTemperature", "MinTemperature",
        "ComfortIndexDescription", "ComfortIndex",
        "UVIndex", "UVI", "ExposureLevel",
    )
    if isinstance(element_value, dict):
        for key in preferred_keys:
            scalar = _cwa_scalar(element_value.get(key))
            if scalar is not None:
                return str(scalar).strip()
        for value in element_value.values():
            scalar = _cwa_scalar(value)
            if scalar is not None:
                return str(scalar).strip()
    else:
        scalar = _cwa_scalar(element_value)
        if scalar is not None:
            return str(scalar).strip()
    return None

def _cwa_element_values(location, element_name):
    elements = _cwa_as_list(_cwa_get(location, "weatherElement", "WeatherElement"))
    normalized_element_name = _normalize_cwa_name(element_name)
    element = next(
        (
            e for e in elements
            if _normalize_cwa_name(_cwa_get(e, "elementName", "ElementName")) == normalized_element_name
        ),
        None,
    )
    if not element:
        return []
    values = []
    for item in _cwa_as_list(_cwa_get(element, "time", "Time")):
        parameter = _cwa_get(item, "parameter", "Parameter") or {}
        parameter_name = _cwa_get(parameter, "parameterName", "ParameterName")
        if parameter_name is not None:
            values.append(str(parameter_name).strip())
            continue
        for element_value in _cwa_as_list(_cwa_get(item, "elementValue", "ElementValue")):
            value = _cwa_element_value(element_value)
            if value is not None:
                values.append(value)
                break
    return values

def _cwa_element_values_any(location, names):
    for name in names:
        values = _cwa_element_values(location, name)
        if values:
            return values
    return []

def _cwa_element_time_values(location, element_name):
    elements = _cwa_as_list(_cwa_get(location, "weatherElement", "WeatherElement"))
    normalized_element_name = _normalize_cwa_name(element_name)
    element = next(
        (
            e for e in elements
            if _normalize_cwa_name(_cwa_get(e, "elementName", "ElementName")) == normalized_element_name
        ),
        None,
    )
    if not element:
        return []

    values = []
    for item in _cwa_as_list(_cwa_get(element, "time", "Time")):
        value = None
        parameter = _cwa_get(item, "parameter", "Parameter") or {}
        parameter_name = _cwa_get(parameter, "parameterName", "ParameterName")
        if parameter_name is not None:
            value = str(parameter_name).strip()
        else:
            for element_value in _cwa_as_list(_cwa_get(item, "elementValue", "ElementValue")):
                value = _cwa_element_value(element_value)
                if value is not None:
                    break
        if value is None:
            continue
        values.append({
            "start": _cwa_get(item, "startTime", "StartTime", "dataTime", "DataTime"),
            "end": _cwa_get(item, "endTime", "EndTime"),
            "value": value,
        })
    return values

def _cwa_element_time_values_any(location, names):
    for name in names:
        values = _cwa_element_time_values(location, name)
        if values:
            return values
    return []

def _to_int(value):
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            value = match.group(0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _to_float(value):
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            value = match.group(0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _parse_cwa_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

def _format_uv_time(entry):
    start = _parse_cwa_time(entry.get("start"))
    end = _parse_cwa_time(entry.get("end"))
    if start and end:
        return f"{start.astimezone(TAIPEI_TZ).strftime('%H:%M')}-{end.astimezone(TAIPEI_TZ).strftime('%H:%M')}"
    if start:
        return start.astimezone(TAIPEI_TZ).strftime("%H:%M")
    return "時間未定"

def _uv_level(value):
    if value >= 11:
        return "危險"
    if value >= 8:
        return "過量"
    if value >= 6:
        return "高量"
    if value >= 3:
        return "中量"
    return "低量"

def _uv_advice(max_uv):
    if max_uv >= 8:
        return "避免久曬，帽子、太陽眼鏡、防曬都要上。"
    if max_uv >= 6:
        return "中午前後減少曝曬，外出記得防曬。"
    if max_uv >= 3:
        return "長時間戶外仍建議防曬。"
    return "紫外線偏低，基本防曬即可。"

def _uv_curve(values):
    blocks = "▁▂▃▄▅▆▇█"
    points = []
    for item in values[:8]:
        uv = _to_float(item.get("value"))
        if uv is None:
            continue
        index = min(len(blocks) - 1, max(0, int(round(uv / 11 * (len(blocks) - 1)))))
        points.append(blocks[index])
    return "".join(points)

def build_uv_summary(location):
    uv_values = _cwa_element_time_values_any(
        location,
        ["紫外線指數", "紫外線", "UVI", "UVIndex", "UV Index"],
    )
    parsed = [
        {**item, "uv": _to_float(item.get("value"))}
        for item in uv_values
    ]
    parsed = [item for item in parsed if item["uv"] is not None]
    if not parsed:
        return ""

    max_item = max(parsed, key=lambda item: item["uv"])
    max_uv = max_item["uv"]
    risky = [item for item in parsed if item["uv"] >= 6]
    if risky:
        risk_time = "、".join(_format_uv_time(item) for item in risky[:3])
    else:
        risk_time = _format_uv_time(max_item)
    curve = _uv_curve(parsed)
    value_text = f"{max_uv:.0f}" if max_uv.is_integer() else f"{max_uv:.1f}"
    return (
        f"\n☀️ 紫外線 {curve} 最高 {value_text}（{_uv_level(max_uv)}），"
        f"注意時間：{risk_time}。{_uv_advice(max_uv)}"
    )

def fetch_weather_summary(city):
    if not city:
        return None
    if not CWA_AUTHORIZATION:
        return "🌤 尚未設定中央氣象署 CWA 授權碼，無法取得天氣。"
    county, district = _parse_cwa_location(city)
    try:
        if district:
            dataset_id = CWA_COUNTY_DATASET_IDS.get(county)
            if not dataset_id:
                return f"🌤 中央氣象署查不到「{city}」的縣市資料，請輸入例如：新北市淡水區。"
            data = requests.get(
                f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{dataset_id}",
                params={
                    "Authorization": CWA_AUTHORIZATION,
                    "format": "JSON",
                    "locationName": district,
                },
                timeout=8,
            ).json()
            locations = _cwa_locations(data)
            location = _find_cwa_location(locations, district)
            if not location:
                return f"🌤 中央氣象署查不到「{city}」的鄉鎮市區預報，請確認格式如：新北市淡水區。"
            location_label = f"{county}{_cwa_location_name(location) or district}"
            wx = (_cwa_element_values_any(location, ["天氣現象", "Wx"]) or ["天氣資料"])[0]
            pops = [_to_int(v) for v in _cwa_element_values_any(location, ["12小時降雨機率", "6小時降雨機率", "3小時降雨機率", "降雨機率", "PoP12h", "PoP6h", "PoP"])]
            temps = [_to_int(v) for v in _cwa_element_values_any(location, ["溫度", "平均溫度", "T"])]
            min_ts = [_to_int(v) for v in _cwa_element_values_any(location, ["最低溫度", "MinT"])]
            max_ts = [_to_int(v) for v in _cwa_element_values_any(location, ["最高溫度", "MaxT"])]
            comfort = (_cwa_element_values_any(location, ["舒適度指數", "舒適度", "CI"]) or [None])[0]
        else:
            location_name = county
            data = requests.get(
                "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001",
                params={
                    "Authorization": CWA_AUTHORIZATION,
                    "format": "JSON",
                    "locationName": location_name,
                },
                timeout=8,
            ).json()
            locations = _cwa_locations(data)
            if not locations:
                return f"🌤 中央氣象署查不到「{city}」的縣市預報，請輸入例如：臺北市，或新北市淡水區。"
            location = locations[0]
            location_label = location_name
            wx = (_cwa_element_values(location, "Wx") or ["天氣資料"])[0]
            pops = [_to_int(v) for v in _cwa_element_values(location, "PoP")]
            min_ts = [_to_int(v) for v in _cwa_element_values(location, "MinT")]
            max_ts = [_to_int(v) for v in _cwa_element_values(location, "MaxT")]
            temps = []
            comfort = (_cwa_element_values(location, "CI") or [None])[0]

        rain_values = [v for v in pops if v is not None]
        min_values = [v for v in min_ts if v is not None]
        max_values = [v for v in max_ts if v is not None]
        temp_values = [v for v in temps if v is not None]
        rain = max(rain_values) if rain_values else None
        temp_min = min(min_values or temp_values) if (min_values or temp_values) else None
        temp_max = max(max_values or temp_values) if (max_values or temp_values) else None
        temp = f"{temp_min}-{temp_max}°C" if temp_min is not None and temp_max is not None else "溫度未知"
        rain_text = f"降雨機率 {rain}%" if rain is not None else "降雨機率未知"
        uv_summary = build_uv_summary(location)
        return f"🌤 {location_label}今日天氣：{wx}，{temp}，{rain_text}。{_weather_advice(wx, rain, temp_max, comfort)}{uv_summary}"
    except Exception as e:
        logger.warning("fetch CWA weather failed for %s: %s", city, e)
        return "🌤 中央氣象署天氣資料暫時讀取失敗。"

def _event_occurs_on(event, target_date):
    if event.is_recurring and event.recurrence_rule:
        try:
            days, _ = event.recurrence_rule.split("|", 1)
            return WEEKDAY_CODES[target_date.weekday()] in {d.strip() for d in days.split(",")}
        except Exception:
            return False
    if not event.event_datetime:
        return False
    return event.event_datetime.astimezone(TAIPEI_TZ).date() == target_date

def _event_summary_line(event):
    content = html.escape(event.event_content or "")
    if event.is_recurring:
        try:
            _, time_str = event.recurrence_rule.split("|", 1)
        except Exception:
            time_str = "時間未定"
        return f"{time_str} 🔁 {content}"
    event_dt = event.event_datetime.astimezone(TAIPEI_TZ)
    return f"{event_dt.strftime('%H:%M')} {content}"

def build_daily_summary(user_id, target_date, title, include_weather=False, city=None):
    db = SessionLocal()
    try:
        events = db.query(Event).filter(
            Event.creator_user_id == str(user_id),
            Event.reminder_sent == 0,
        ).all()
        lines = []
        if include_weather:
            weather = fetch_weather_summary(city)
            if weather:
                lines.append(weather)
                lines.append("")
        matches = [ev for ev in events if _event_occurs_on(ev, target_date)]
        lines.append(title)
        if not matches:
            lines.append("今天沒有待提醒事項。" if target_date == datetime.now(TAIPEI_TZ).date() else "這天沒有待提醒事項。")
        else:
            for ev in sorted(matches, key=lambda e: _event_summary_line(e)):
                lines.append(f"• {_event_summary_line(ev)}")
        return "\n".join(lines)
    finally:
        db.close()

def scan_daily_summaries():
    now = datetime.now(TAIPEI_TZ)
    today = now.date()
    now_hhmm = now.strftime("%H:%M")
    for setting in list_user_settings():
        try:
            if (
                setting.morning_summary_enabled
                and _valid_hhmm(setting.morning_summary_time)
                and now_hhmm >= setting.morning_summary_time
                and setting.last_morning_summary_date != today
            ):
                text = build_daily_summary(
                    setting.user_id, today, "🌅 今日摘要",
                    bool(setting.weather_enabled), setting.city,
                )
                sent, result = tg_send(setting.user_id, text)
                if sent:
                    update_user_setting(setting.user_id, last_morning_summary_date=today)
                else:
                    logger.warning("morning summary failed for %s: %s", setting.user_id, result)
            tomorrow = today + timedelta(days=1)
            if (
                setting.evening_summary_enabled
                and _valid_hhmm(setting.evening_summary_time)
                and now_hhmm >= setting.evening_summary_time
                and setting.last_evening_summary_date != today
            ):
                text = build_daily_summary(setting.user_id, tomorrow, "🌙 明日預告")
                sent, result = tg_send(setting.user_id, text)
                if sent:
                    update_user_setting(setting.user_id, last_evening_summary_date=today)
                else:
                    logger.warning("evening summary failed for %s: %s", setting.user_id, result)
        except Exception as e:
            logger.error("scan_daily_summary user=%s: %s", getattr(setting, "user_id", "?"), e, exc_info=True)

def _hhmm_parts(value):
    if not _valid_hhmm(value):
        return None
    hour, minute = map(int, value.split(":"))
    return hour, minute

def send_daily_summary(user_id, period):
    setting = get_user_setting(user_id)
    today = datetime.now(TAIPEI_TZ).date()
    try:
        if period == "morning":
            if not setting.morning_summary_enabled or setting.last_morning_summary_date == today:
                return
            text = build_daily_summary(
                setting.user_id, today, "🌅 今日摘要",
                bool(setting.weather_enabled), setting.city,
            )
            sent, result = tg_send(setting.user_id, text)
            if sent:
                update_user_setting(setting.user_id, last_morning_summary_date=today)
            else:
                logger.warning("morning summary failed for %s: %s", setting.user_id, result)
            return

        if period == "evening":
            if not setting.evening_summary_enabled or setting.last_evening_summary_date == today:
                return
            text = build_daily_summary(setting.user_id, today + timedelta(days=1), "🌙 明日預告")
            sent, result = tg_send(setting.user_id, text)
            if sent:
                update_user_setting(setting.user_id, last_evening_summary_date=today)
            else:
                logger.warning("evening summary failed for %s: %s", setting.user_id, result)
    except Exception as e:
        logger.error("send_daily_summary user=%s period=%s: %s", user_id, period, e, exc_info=True)

def schedule_user_summary_jobs(user_id):
    setting = get_user_setting(user_id)
    safe_remove_job_id(f"summary_morning_{setting.user_id}")
    safe_remove_job_id(f"summary_evening_{setting.user_id}")

    if setting.morning_summary_enabled:
        parts = _hhmm_parts(setting.morning_summary_time)
        if parts:
            h, m = parts
            safe_add_cron(send_daily_summary, [setting.user_id, "morning"],
                          f"summary_morning_{setting.user_id}", "*", h, m)
    if setting.evening_summary_enabled:
        parts = _hhmm_parts(setting.evening_summary_time)
        if parts:
            h, m = parts
            safe_add_cron(send_daily_summary, [setting.user_id, "evening"],
                          f"summary_evening_{setting.user_id}", "*", h, m)

def start_daily_summary_scan():
    with scheduler_lock:
        safe_remove_job_id("daily_summary_scan")
        for setting in list_user_settings():
            schedule_user_summary_jobs(setting.user_id)
        safe_add_cron(scan_daily_summaries, [], "daily_summary_catchup_scan", "*", 23, 55)
        logger.info("✅ 每日摘要掃描排程已啟動")

# ── Tracker 每日掃描 ──────────────────────────────────────────────────────────

def scan_trackers(target_hhmm=None):
    """定期掃描追蹤項目，到期前依各項目的 remind_time 發提醒。"""
    from db import get_all_trackers, mark_tracker_reminded
    from datetime import date, timedelta

    now = datetime.now(TAIPEI_TZ)
    today = now.date()
    now_hhmm = now.strftime("%H:%M")
    trackers = get_all_trackers()

    for t in trackers:
        try:
            remind_days = 7 if t.remind_days is None else t.remind_days
            if remind_days < 0:
                continue
            remind_time = (t.remind_time or "08:00")[:5]
            if target_hhmm and remind_time != target_hhmm:
                continue
            if not target_hhmm and now_hhmm < remind_time:
                continue
            if t.last_reminded_date == today:
                continue
            nd = _calc_tracker_next_date(t, today)
            if nd is None:
                continue
            days = (nd - today).days
            if 0 <= days <= remind_days:
                _send_tracker_alert(t, nd, days)
                mark_tracker_reminded(t.id, today)
        except Exception as e:
            logger.error(f"scan_tracker id={t.id}: {e}")

def _calc_tracker_next_date(t, today):
    from datetime import date, timedelta
    try:
        if t.is_recurring and t.recurring_month and t.recurring_day:
            d = today.replace(month=t.recurring_month, day=t.recurring_day)
            if d < today:
                d = d.replace(year=d.year + 1)
            return d
        if t.category == "medicine" and t.stock_total and t.stock_daily:
            days = int(t.stock_total / t.stock_daily)
            return t.created_at.date() + timedelta(days=days)
        if t.expire_date:
            d = t.expire_date
            if t.cycle == "monthly":
                while d < today:
                    m = d.month + 1 if d.month < 12 else 1
                    y = d.year if d.month < 12 else d.year + 1
                    try:
                        d = d.replace(year=y, month=m)
                    except ValueError:
                        d = d.replace(year=y, month=m, day=28)
            elif t.cycle == "yearly":
                while d < today:
                    d = d.replace(year=d.year + 1)
            return d
    except Exception:
        pass
    return None

def _send_tracker_alert(t, next_date, days_left):
    icons = {"subscription": "💳", "contract": "📄", "anniversary": "🎂", "medicine": "💊"}
    names = {"subscription": "訂閱到期", "contract": "合約到期",
             "anniversary": "紀念日快到了", "medicine": "藥物即將耗盡"}
    icon  = icons.get(t.category, "📌")
    title = names.get(t.category, "提醒")

    if days_left == 0:
        dl_str = "今天！"
    elif days_left == 1:
        dl_str = "明天"
    else:
        dl_str = f"還有 {days_left} 天"

    date_str = next_date.strftime("%m/%d") if t.is_recurring else next_date.strftime("%Y/%m/%d")

    lines = [f"{icon} <b>{title}</b>", f"📌 {html.escape(t.name)}  {date_str}（{dl_str}）"]
    if t.amount:
        cycle_zh = {"monthly": "月", "yearly": "年"}.get(t.cycle, "")
        lines.append(f"💰 {t.amount:.0f} 元/{cycle_zh}" if cycle_zh else f"💰 {t.amount:.0f} 元")
    if t.category == "medicine":
        lines.append("🛒 記得補貨！")

    tg_send(t.user_id, "\n".join(lines))

def start_tracker_scan():
    """Restore tracker scans at the distinct reminder times currently in use."""
    from db import get_all_trackers

    with scheduler_lock:
        for job in list(scheduler.get_jobs()):
            if job.id == "daily_tracker_scan" or job.id.startswith("tracker_scan_"):
                safe_remove_job_id(job.id)

        times = set()
        for tracker in get_all_trackers():
            try:
                if tracker.remind_days is not None and tracker.remind_days < 0:
                    continue
                remind_time = (tracker.remind_time or "08:00")[:5]
                if _valid_hhmm(remind_time):
                    times.add(remind_time)
            except Exception:
                continue

        for remind_time in sorted(times):
            h, m = map(int, remind_time.split(":"))
            safe_add_cron(
                scan_trackers, [remind_time],
                f"tracker_scan_{remind_time.replace(':', '')}",
                "*", h, m,
            )
        safe_add_cron(scan_trackers, [], "tracker_catchup_scan", "*", 23, 50)
        logger.info("Tracker precise scan jobs restored for %s reminder times.", len(times))

def _old_start_tracker_scan():
    """啟動 tracker 掃描排程。"""
    with scheduler_lock:
        safe_add_cron(scan_trackers, [], "daily_tracker_scan", "*", "*", "*/15")
        logger.info("✅ Tracker 每日掃描排程已啟動")

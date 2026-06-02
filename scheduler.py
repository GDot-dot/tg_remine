# scheduler.py
import os, logging, threading, html, time
from datetime import datetime, timedelta
import requests, pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from db import DATABASE_URL, SessionLocal, Event, mark_reminder_sent, decrease_remaining_repeats, update_event_fields

logger = logging.getLogger(__name__)
TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_RETRY_DELAYS = (1, 3, 5)
REMINDER_RETRY_DELAY = 5

PRIORITY_RULES = {
    1: {"icon": "🟢", "interval": 30, "repeats": 3},
    2: {"icon": "🟡", "interval": 10, "repeats": 6},
    3: {"icon": "🔴", "interval":  5, "repeats": 12},
}

jobstores    = {"default": SQLAlchemyJobStore(url=DATABASE_URL, engine_options={"pool_pre_ping": True, "pool_recycle": 300})}
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
    update_event_fields(event_id, reminder_time=retry_at, reminder_sent=0)
    safe_add_job(send_reminder, retry_at, [event_id], f"reminder_{event_id}")
    logger.warning(
        "Reminder %s send failed; retry scheduled at %s. reason=%s",
        event_id, retry_at.isoformat(), reason,
    )

def _finish_failed_reminder(db, event, reason):
    logger.error("Reminder %s failed permanently: %s", event.id, reason)
    mark_reminder_sent(event.id)
    _remove_job(event.id)
    if event.priority_level > 0:
        db.query(Event).filter(Event.id == event.id).delete()
        db.commit()

def _confirm_kb(event_id):
    return {"inline_keyboard": [
        [
            {"text": "✅ 確認收到", "callback_data": f"cr:{event_id}"},
            {"text": "💤 延後5分", "callback_data": f"sn:{event_id}:5"},
            {"text": "⏰ 延後30分", "callback_data": f"sn:{event_id}:30"},
        ],
        [{"text": "🕒 自訂延後", "callback_data": f"snc:{event_id}"}],
    ]}

def _priority_kb(event_id):
    return {"inline_keyboard": [[{"text": "✅ 收到，停止提醒", "callback_data": f"cr:{event_id}"}]]}

def send_reminder(event_id):
    db = SessionLocal()
    try:
        event = db.query(Event).filter(Event.id == event_id).first()
        if not event: _remove_job(event_id); return
        if not event.is_recurring and event.reminder_sent: return

        chat_id = event.target_id
        name    = event.target_display_name or "您"
        content = event.event_content

        sent = False
        send_result = {}

        if event.priority_level > 0:
            icon = PRIORITY_RULES[event.priority_level]["icon"]
            sent, send_result = tg_send(chat_id, f"{icon} <b>重要提醒！</b>\n\n@{name}\n記得要「{content}」！\n(未確認將繼續提醒)", _priority_kb(event_id))
        elif event.is_recurring:
            sent, send_result = tg_send(chat_id, f"⏰ 週期提醒\n\n@{name}\n記得要「{content}」喔！", _confirm_kb(event_id))
        else:
            event_dt  = event.event_datetime.astimezone(TAIPEI_TZ)
            sent, send_result = tg_send(chat_id,
                f"⏰ 提醒時間到！\n\n@{name}\n📅 {event_dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}",
                _confirm_kb(event_id))

        if not sent:
            if not event.is_recurring:
                if _is_retryable_tg_result(send_result):
                    _retry_reminder(event_id, send_result)
                else:
                    _finish_failed_reminder(db, event, send_result)
            else:
                logger.warning("Recurring reminder %s send failed: %s", event_id, send_result)
            return

        if not event.is_recurring:
            if event.priority_level > 0 and event.remaining_repeats > 0:
                decrease_remaining_repeats(event_id)
                # 支援自訂 interval（recurrence_rule = "custom:N"）
                if event.recurrence_rule and event.recurrence_rule.startswith("custom:"):
                    interval = int(event.recurrence_rule.split(":")[1])
                else:
                    interval = PRIORITY_RULES[event.priority_level]["interval"]
                next_run = datetime.now(TAIPEI_TZ) + timedelta(minutes=interval)
                safe_add_job(send_reminder, next_run, [event_id], f"reminder_{event_id}")
            else:
                mark_reminder_sent(event_id)
                _remove_job(event_id)
                if event.priority_level > 0:
                    db.query(Event).filter(Event.id == event_id).delete(); db.commit()
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

def _remove_job(event_id):
    for prefix in ("reminder_", "recurring_"):
        jid = f"{prefix}{event_id}"
        if scheduler.get_job(jid):
            try: scheduler.remove_job(jid)
            except: pass

def remove_job(event_id):
    _remove_job(event_id)

def restore_jobs():
    db = SessionLocal()
    try:
        now = datetime.now(TAIPEI_TZ)
        recurring  = db.query(Event).filter(Event.is_recurring == 1).all()
        pending_one = db.query(Event).filter(
            Event.reminder_sent == 0,
            Event.is_recurring == 0,
            Event.reminder_time.isnot(None),
        ).all()
        count = 0
        for ev in recurring + pending_one:
            is_rec = ev.is_recurring
            job_id = f"{'recurring' if is_rec else 'reminder'}_{ev.id}"
            if scheduler.get_job(job_id): continue
            try:
                if is_rec and ev.recurrence_rule:
                    days, t = ev.recurrence_rule.split("|")
                    h, m = map(int, t.split(":"))
                    safe_add_cron(send_reminder, [ev.id], job_id, days.lower(), h, m)
                else:
                    run_at = ev.reminder_time.astimezone(TAIPEI_TZ)
                    if run_at <= now:
                        run_at = now + timedelta(seconds=10)
                        logger.warning("Restoring missed reminder %s for immediate retry.", ev.id)
                    safe_add_job(send_reminder, run_at, [ev.id], job_id)
                count += 1
            except Exception as e:
                logger.error(f"restore event {ev.id}: {e}")
        logger.info(f"✅ 還原完成，共 {count} 個排程。")
    except Exception as e:
        logger.error(f"restore_jobs: {e}", exc_info=True)
    finally:
        db.close()

def safe_start():
    with scheduler_lock:
        if not scheduler.running:
            scheduler.start()
            logger.info("Scheduler started.")
            threading.Thread(target=restore_jobs, daemon=True).start()

# ── Tracker 每日掃描 ──────────────────────────────────────────────────────────

def scan_trackers():
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
            if now_hhmm < remind_time:
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
    """啟動 tracker 掃描排程。"""
    with scheduler_lock:
        if not scheduler.get_job("daily_tracker_scan"):
            safe_add_cron(scan_trackers, [], "daily_tracker_scan", "*", "*", "*/5")
            logger.info("✅ Tracker 每日掃描排程已啟動")

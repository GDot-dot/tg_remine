# scheduler.py
import os, logging, threading
from datetime import datetime, timedelta
import requests, pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from db import DATABASE_URL, SessionLocal, Event, mark_reminder_sent, delete_event_by_id, decrease_remaining_repeats, update_reminder_time

logger = logging.getLogger(__name__)
TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")

PRIORITY_RULES = {
    1: {"icon": "🟢", "interval": 30, "repeats": 3},
    2: {"icon": "🟡", "interval": 10, "repeats": 6},
    3: {"icon": "🔴", "interval":  5, "repeats": 12},
}

jobstores    = {"default": SQLAlchemyJobStore(url=DATABASE_URL, engine_options={"pool_pre_ping": True, "pool_recycle": 300})}
executors    = {"default": ThreadPoolExecutor(max_workers=5)}
job_defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 60}
scheduler_lock = threading.Lock()
scheduler = BackgroundScheduler(jobstores=jobstores, executors=executors, job_defaults=job_defaults, timezone=TAIPEI_TZ)

def _tg(method, **kwargs):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    try:
        return requests.post(url, json=kwargs, timeout=10).json()
    except Exception as e:
        logger.error(f"TG API ({method}): {e}"); return {}

def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    return _tg("sendMessage", **payload)

def _confirm_kb(event_id):
    return {"inline_keyboard": [[
        {"text": "✅ 確認收到", "callback_data": f"cr:{event_id}"},
        {"text": "💤 延後5分",  "callback_data": f"sn:{event_id}:5"},
        {"text": "⏰ 延後30分", "callback_data": f"sn:{event_id}:30"},
    ]]}

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

        if event.priority_level > 0:
            icon = PRIORITY_RULES[event.priority_level]["icon"]
            tg_send(chat_id, f"{icon} <b>重要提醒！</b>\n\n@{name}\n記得要「{content}」！\n(未確認將繼續提醒)", _priority_kb(event_id))
        elif event.is_recurring:
            tg_send(chat_id, f"⏰ 週期提醒\n\n@{name}\n記得要「{content}」喔！", _confirm_kb(event_id))
        else:
            event_dt  = event.event_datetime.astimezone(TAIPEI_TZ)
            tg_send(chat_id,
                f"⏰ 提醒時間到！\n\n@{name}\n📅 {event_dt.strftime('%Y/%m/%d %H:%M')}\n📝 {content}",
                _confirm_kb(event_id))

        if not event.is_recurring:
            if event.priority_level > 0 and event.remaining_repeats > 0:
                decrease_remaining_repeats(event_id)
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
        future_one = db.query(Event).filter(Event.reminder_sent == 0, Event.is_recurring == 0, Event.reminder_time > now).all()
        count = 0
        for ev in recurring + future_one:
            is_rec = ev.is_recurring
            job_id = f"{'recurring' if is_rec else 'reminder'}_{ev.id}"
            if scheduler.get_job(job_id): continue
            try:
                if is_rec and ev.recurrence_rule:
                    days, t = ev.recurrence_rule.split("|")
                    h, m = map(int, t.split(":"))
                    safe_add_cron(send_reminder, [ev.id], job_id, days.lower(), h, m)
                else:
                    safe_add_job(send_reminder, ev.reminder_time.astimezone(TAIPEI_TZ), [ev.id], job_id)
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

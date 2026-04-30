# scheduler.py - APScheduler + send_reminder（同步版，用 requests 直接打 TG API）

import os
import logging
import threading
from datetime import datetime, timedelta

import requests
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

from db import (
    DATABASE_URL, SessionLocal, Event,
    mark_reminder_sent, delete_event_by_id,
    decrease_remaining_repeats, update_reminder_time,
)

logger = logging.getLogger(__name__)

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")

PRIORITY_RULES = {
    1: {"color": "green",  "icon": "🟢", "interval": 30, "repeats": 3},
    2: {"color": "yellow", "icon": "🟡", "interval": 10, "repeats": 6},
    3: {"color": "red",    "icon": "🔴", "interval": 5,  "repeats": 12},
}

# ── 排程器初始化 ───────────────────────────────────────────────────────────────

jobstores  = {"default": SQLAlchemyJobStore(
    url=DATABASE_URL,
    engine_options={"pool_pre_ping": True, "pool_recycle": 300},
)}
executors  = {"default": ThreadPoolExecutor(max_workers=5)}
job_defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 60}
scheduler_lock = threading.Lock()

scheduler = BackgroundScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults=job_defaults,
    timezone=TAIPEI_TZ,
)


# ── Telegram API helpers（同步，供 APScheduler thread 呼叫）─────────────────────

def _tg(method: str, **kwargs) -> dict:
    """呼叫 Telegram Bot API（同步）"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=kwargs, timeout=10)
        return resp.json()
    except Exception as e:
        logger.error(f"TG API error ({method}): {e}")
        return {}


def tg_send(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _tg("sendMessage", **payload)


def _build_confirm_keyboard(event_id: int) -> dict:
    """普通提醒的 InlineKeyboard"""
    return {
        "inline_keyboard": [[
            {"text": "✅ 確認收到", "callback_data": f"cr:{event_id}"},
            {"text": "💤 延後5分", "callback_data": f"sn:{event_id}:5"},
            {"text": "⏰ 延後30分", "callback_data": f"sn:{event_id}:30"},
        ]]
    }


def _build_priority_keyboard(event_id: int) -> dict:
    """重要提醒的 InlineKeyboard"""
    return {
        "inline_keyboard": [[
            {"text": "✅ 收到，停止提醒", "callback_data": f"cr:{event_id}"}
        ]]
    }


# ── 核心：發送提醒 ─────────────────────────────────────────────────────────────

def send_reminder(event_id: int):
    db = SessionLocal()
    try:
        event = db.query(Event).filter(Event.id == event_id).first()
        if not event:
            _remove_job(event_id)
            return

        # 一次性提醒：已發送就跳過
        if not event.is_recurring and event.reminder_sent:
            return

        chat_id = event.target_id
        name    = event.target_display_name or "您"
        content = event.event_content

        if event.priority_level > 0:
            rule  = PRIORITY_RULES[event.priority_level]
            icon  = rule["icon"]
            text  = (f"{icon} <b>重要提醒！</b>\n\n"
                     f"@{name}\n記得要「{content}」！\n"
                     f"(未確認將繼續提醒)")
            tg_send(chat_id, text, _build_priority_keyboard(event_id))

        elif event.is_recurring:
            text = f"⏰ 週期提醒\n\n@{name}\n記得要「{content}」喔！"
            tg_send(chat_id, text, _build_confirm_keyboard(event_id))

        else:
            event_dt  = event.event_datetime.astimezone(TAIPEI_TZ)
            time_info = event_dt.strftime("%Y/%m/%d %H:%M")
            text = (f"⏰ 提醒時間到！\n\n"
                    f"@{name}\n"
                    f"📅 {time_info}\n"
                    f"📝 {content}")
            tg_send(chat_id, text, _build_confirm_keyboard(event_id))

        logger.info(f"✅ 發送提醒 event_id={event_id}")

        # ── 後續處理 ──
        if not event.is_recurring:
            if event.priority_level > 0 and event.remaining_repeats > 0:
                # 重要提醒：重試
                decrease_remaining_repeats(event_id)
                interval = PRIORITY_RULES[event.priority_level]["interval"]
                next_run = datetime.now(TAIPEI_TZ) + timedelta(minutes=interval)
                safe_add_job(send_reminder, next_run, [event_id], f"reminder_{event_id}")
            else:
                mark_reminder_sent(event_id)
                _remove_job(event_id)
                if event.priority_level > 0:
                    db.query(Event).filter(Event.id == event_id).delete()
                    db.commit()

    except Exception as e:
        logger.error(f"send_reminder error (id={event_id}): {e}", exc_info=True)
    finally:
        db.close()


# ── 排程器工具 ────────────────────────────────────────────────────────────────

def safe_add_job(func, run_date, args: list, job_id: str) -> bool:
    """新增一次性排程（thread-safe）"""
    with scheduler_lock:
        try:
            if not scheduler.running:
                safe_start()
            run_date_utc = run_date.astimezone(pytz.UTC)
            scheduler.add_job(
                func, "date",
                run_date=run_date_utc,
                args=args,
                id=job_id,
                replace_existing=True,
            )
            return True
        except Exception as e:
            logger.error(f"safe_add_job error ({job_id}): {e}")
            return False


def safe_add_cron(func, args: list, job_id: str,
                  day_of_week: str, hour: int, minute: int) -> bool:
    """新增週期性排程"""
    with scheduler_lock:
        try:
            scheduler.add_job(
                func, "cron",
                args=args,
                id=job_id,
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone=TAIPEI_TZ,
                replace_existing=True,
            )
            return True
        except Exception as e:
            logger.error(f"safe_add_cron error ({job_id}): {e}")
            return False


def _remove_job(event_id: int):
    for prefix in ("reminder_", "recurring_"):
        job_id = f"{prefix}{event_id}"
        if scheduler.get_job(job_id):
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass


def remove_job(event_id: int):
    _remove_job(event_id)


# ── 重啟自癒：從 DB 還原排程 ───────────────────────────────────────────────────

def restore_jobs():
    db = SessionLocal()
    try:
        logger.info("♻️ 正在還原排程任務...")
        now = datetime.now(TAIPEI_TZ)

        recurring = db.query(Event).filter(Event.is_recurring == 1).all()
        future_one = db.query(Event).filter(
            Event.reminder_sent == 0,
            Event.is_recurring == 0,
            Event.reminder_time > now,
        ).all()

        count = 0
        for ev in recurring + future_one:
            is_rec = ev.is_recurring
            job_id = f"{'recurring' if is_rec else 'reminder'}_{ev.id}"
            if scheduler.get_job(job_id):
                continue
            try:
                if is_rec and ev.recurrence_rule:
                    days, t = ev.recurrence_rule.split("|")
                    h, m = map(int, t.split(":"))
                    safe_add_cron(send_reminder, [ev.id], job_id, days.lower(), h, m)
                else:
                    run_date = ev.reminder_time.astimezone(TAIPEI_TZ)
                    safe_add_job(send_reminder, run_date, [ev.id], job_id)
                count += 1
            except Exception as e:
                logger.error(f"  restore event {ev.id} failed: {e}")

        logger.info(f"✅ 還原完成，共 {count} 個排程。")
    except Exception as e:
        logger.error(f"restore_jobs error: {e}", exc_info=True)
    finally:
        db.close()


def safe_start():
    with scheduler_lock:
        if not scheduler.running:
            scheduler.start()
            logger.info("Scheduler started.")
            threading.Thread(target=restore_jobs, daemon=True).start()

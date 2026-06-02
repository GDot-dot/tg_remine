# db.py
import os, logging
from datetime import datetime, date as date_type
from sqlalchemy import (create_engine, Column, Integer, String, Float, DateTime, Date, Text, UniqueConstraint, inspect, text as sql_text)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Event(Base):
    __tablename__ = "events"
    id                  = Column(Integer, primary_key=True, autoincrement=True)
    creator_user_id     = Column(String(50), nullable=False)
    target_id           = Column(String(50), nullable=False)
    target_type         = Column(String(20), default="private")
    target_display_name = Column(String(100), default="")
    event_content       = Column(Text, nullable=False)
    event_datetime      = Column(DateTime(timezone=True), nullable=True)
    reminder_time       = Column(DateTime(timezone=True), nullable=True)
    reminder_sent       = Column(Integer, default=0)
    is_recurring        = Column(Integer, default=0)
    recurrence_rule     = Column(String(100), nullable=True)
    priority_level      = Column(Integer, default=0)
    remaining_repeats   = Column(Integer, default=0)

class Location(Base):
    __tablename__ = "locations"
    id        = Column(Integer, primary_key=True, autoincrement=True)
    user_id   = Column(String(50), nullable=False)
    name      = Column(String(100), nullable=False)
    latitude  = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    address   = Column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_location"),)

class Memory(Base):
    __tablename__ = "memories"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(50), nullable=False)
    keyword = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "keyword", name="uq_user_memory"),)

class UserSetting(Base):
    __tablename__ = "user_settings"
    id                        = Column(Integer, primary_key=True, autoincrement=True)
    user_id                   = Column(String(50), nullable=False, unique=True)
    city                      = Column(String(100), default="台北")
    weather_enabled           = Column(Integer, default=1)
    morning_summary_enabled   = Column(Integer, default=1)
    evening_summary_enabled   = Column(Integer, default=1)
    morning_summary_time      = Column(String(5), default="08:00")
    evening_summary_time      = Column(String(5), default="21:30")
    snooze_buttons            = Column(String(50), default="5,30,60")
    last_morning_summary_date = Column(Date, nullable=True)
    last_evening_summary_date = Column(Date, nullable=True)
    telegraph_path            = Column(String(255), nullable=True)
    telegraph_url             = Column(String(255), nullable=True)
    telegraph_access_token    = Column(String(255), nullable=True)

class Tracker(Base):
    __tablename__ = "trackers"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(String(50), nullable=False)
    category        = Column(String(20), nullable=False)   # subscription/contract/anniversary/medicine
    name            = Column(String(100), nullable=False)
    expire_date     = Column(Date, nullable=True)           # 訂閱/合約到期日
    is_recurring    = Column(Integer, default=0)            # 1=每年重複（紀念日）
    recurring_month = Column(Integer, nullable=True)        # 紀念日月份
    recurring_day   = Column(Integer, nullable=True)        # 紀念日日期
    cycle           = Column(String(20), nullable=True)     # monthly/yearly/once
    amount          = Column(Float, nullable=True)          # 金額
    remind_days     = Column(Integer, default=7)            # 提前提醒天數
    remind_time     = Column(String(5), default="08:00")    # 每日提醒時間 HH:MM
    stock_total     = Column(Float, nullable=True)          # 藥物總量
    stock_daily     = Column(Float, nullable=True)          # 藥物每日用量
    notes           = Column(Text, nullable=True)
    last_reminded_date = Column(Date, nullable=True)        # 避免同一天重複推送
    created_at      = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)
    _ensure_user_setting_columns()
    _ensure_tracker_columns()
    logger.info("✅ DB tables created/verified.")

def _ensure_user_setting_columns():
    try:
        inspector = inspect(engine)
        if "user_settings" not in inspector.get_table_names():
            return
        columns = {c["name"] for c in inspector.get_columns("user_settings")}
        dialect = engine.dialect.name
        statements = []
        wanted = {
            "telegraph_path": "VARCHAR(255)",
            "telegraph_url": "VARCHAR(255)",
            "telegraph_access_token": "VARCHAR(255)",
        }
        for column, column_type in wanted.items():
            if column in columns:
                continue
            if dialect == "postgresql":
                statements.append(f"ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS {column} {column_type}")
            else:
                statements.append(f"ALTER TABLE user_settings ADD COLUMN {column} {column_type}")
        if not statements:
            return
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(sql_text(statement))
        logger.info("✅ User setting columns migrated/verified.")
    except Exception as e:
        logger.error(f"ensure user setting columns: {e}", exc_info=True)

def _ensure_tracker_columns():
    try:
        inspector = inspect(engine)
        if "trackers" not in inspector.get_table_names():
            return
        columns = {c["name"] for c in inspector.get_columns("trackers")}
        dialect = engine.dialect.name
        statements = []
        if "remind_time" not in columns:
            if dialect == "postgresql":
                statements.append("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS remind_time VARCHAR(5) DEFAULT '08:00'")
            else:
                statements.append("ALTER TABLE trackers ADD COLUMN remind_time VARCHAR(5) DEFAULT '08:00'")
        if "last_reminded_date" not in columns:
            if dialect == "postgresql":
                statements.append("ALTER TABLE trackers ADD COLUMN IF NOT EXISTS last_reminded_date DATE")
            else:
                statements.append("ALTER TABLE trackers ADD COLUMN last_reminded_date DATE")
        if not statements:
            return
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(sql_text(statement))
        logger.info("✅ Tracker columns migrated/verified.")
    except Exception as e:
        logger.error(f"ensure tracker columns: {e}", exc_info=True)

def add_event(creator_user_id, target_id, target_type, display_name,
              content, event_datetime, is_recurring=0,
              recurrence_rule=None, priority_level=0, remaining_repeats=0):
    db = SessionLocal()
    try:
        ev = Event(creator_user_id=str(creator_user_id), target_id=str(target_id),
                   target_type=target_type, target_display_name=display_name,
                   event_content=content, event_datetime=event_datetime,
                   reminder_time=event_datetime, reminder_sent=0,
                   is_recurring=is_recurring, recurrence_rule=recurrence_rule,
                   priority_level=priority_level, remaining_repeats=remaining_repeats)
        db.add(ev); db.commit(); db.refresh(ev)
        return ev.id
    except Exception as e:
        db.rollback(); logger.error(f"add_event: {e}"); return None
    finally:
        db.close()

def get_event(event_id):
    db = SessionLocal()
    try:
        return db.query(Event).filter(Event.id == event_id).first()
    finally:
        db.close()

def get_user_events(user_id):
    db = SessionLocal()
    try:
        return db.query(Event).filter(Event.creator_user_id == str(user_id)).order_by(Event.reminder_time).all()
    finally:
        db.close()

def mark_reminder_sent(event_id):
    db = SessionLocal()
    try:
        db.query(Event).filter(Event.id == event_id).update({"reminder_sent": 1}); db.commit()
    finally:
        db.close()

def update_reminder_time(event_id, new_time):
    db = SessionLocal()
    try:
        db.query(Event).filter(Event.id == event_id).update({"reminder_time": new_time}); db.commit()
    finally:
        db.close()

def update_event_content(event_id, new_content):
    db = SessionLocal()
    try:
        rows = db.query(Event).filter(Event.id == event_id).update({"event_content": new_content})
        db.commit(); return rows > 0
    finally:
        db.close()

def update_event_fields(event_id, **fields):
    allowed = {
        "event_datetime", "reminder_time", "reminder_sent", "is_recurring",
        "recurrence_rule", "priority_level", "remaining_repeats",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    db = SessionLocal()
    try:
        rows = db.query(Event).filter(Event.id == event_id).update(updates)
        db.commit(); return rows > 0
    finally:
        db.close()

def delete_event_by_id(event_id, user_id):
    db = SessionLocal()
    try:
        rows = db.query(Event).filter(Event.id == event_id, Event.creator_user_id == str(user_id)).delete()
        db.commit(); return rows > 0
    finally:
        db.close()

def decrease_remaining_repeats(event_id):
    db = SessionLocal()
    try:
        ev = db.query(Event).filter(Event.id == event_id).first()
        if ev and ev.remaining_repeats > 0:
            ev.remaining_repeats -= 1; db.commit()
    finally:
        db.close()

def get_user_setting(user_id):
    db = SessionLocal()
    try:
        setting = db.query(UserSetting).filter(UserSetting.user_id == str(user_id)).first()
        if setting:
            return setting
        setting = UserSetting(user_id=str(user_id))
        db.add(setting); db.commit(); db.refresh(setting)
        db.expunge(setting)
        return setting
    finally:
        db.close()

def update_user_setting(user_id, **fields):
    allowed = {
        "city", "weather_enabled", "morning_summary_enabled", "evening_summary_enabled",
        "morning_summary_time", "evening_summary_time", "snooze_buttons",
        "last_morning_summary_date", "last_evening_summary_date",
        "telegraph_path", "telegraph_url", "telegraph_access_token",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    db = SessionLocal()
    try:
        setting = db.query(UserSetting).filter(UserSetting.user_id == str(user_id)).first()
        if not setting:
            setting = UserSetting(user_id=str(user_id))
            db.add(setting)
            db.flush()
        for key, value in updates.items():
            setattr(setting, key, value)
        db.commit()
        return True
    finally:
        db.close()

def list_user_settings():
    db = SessionLocal()
    try:
        return db.query(UserSetting).all()
    finally:
        db.close()

def add_location(user_id, name, lat, lng, address=""):
    db = SessionLocal()
    try:
        db.add(Location(user_id=str(user_id), name=name, latitude=lat, longitude=lng, address=address))
        db.commit(); return True
    except IntegrityError:
        db.rollback(); return False
    finally:
        db.close()

def get_locations(user_id):
    db = SessionLocal()
    try:
        return db.query(Location).filter(Location.user_id == str(user_id)).all()
    finally:
        db.close()

def get_location_by_name(user_id, name):
    db = SessionLocal()
    try:
        return db.query(Location).filter(Location.user_id == str(user_id), Location.name.ilike(f"%{name}%")).first()
    finally:
        db.close()

def delete_location(user_id, name):
    db = SessionLocal()
    try:
        rows = db.query(Location).filter(Location.user_id == str(user_id), Location.name == name).delete()
        db.commit(); return rows > 0
    finally:
        db.close()

def save_memory(user_id, keyword, content):
    db = SessionLocal()
    try:
        existing = db.query(Memory).filter(Memory.user_id == str(user_id), Memory.keyword == keyword).first()
        if existing:
            existing.content = content
        else:
            db.add(Memory(user_id=str(user_id), keyword=keyword, content=content))
        db.commit(); return True
    except Exception as e:
        db.rollback(); logger.error(f"save_memory: {e}"); return False
    finally:
        db.close()

def query_memory(user_id, keyword):
    db = SessionLocal()
    try:
        return db.query(Memory).filter(Memory.user_id == str(user_id), Memory.keyword.ilike(f"%{keyword}%")).all()
    finally:
        db.close()

def get_memory_by_id(user_id, memory_id):
    db = SessionLocal()
    try:
        return db.query(Memory).filter(
            Memory.user_id == str(user_id),
            Memory.id == memory_id,
        ).first()
    finally:
        db.close()

def update_memory_by_id(user_id, memory_id, content):
    db = SessionLocal()
    try:
        rows = db.query(Memory).filter(
            Memory.user_id == str(user_id),
            Memory.id == memory_id,
        ).update({"content": content}, synchronize_session=False)
        db.commit()
        return rows > 0
    except Exception as e:
        db.rollback(); logger.error(f"update_memory_by_id: {e}"); return False
    finally:
        db.close()

def delete_memory_by_id(user_id, memory_id):
    db = SessionLocal()
    try:
        rows = db.query(Memory).filter(
            Memory.user_id == str(user_id),
            Memory.id == memory_id,
        ).delete(synchronize_session=False)
        db.commit(); return rows > 0
    except Exception as e:
        db.rollback(); logger.error(f"delete_memory_by_id: {e}"); return False
    finally:
        db.close()

def forget_memory(user_id, keyword):
    db = SessionLocal()
    try:
        rows = db.query(Memory).filter(Memory.user_id == str(user_id), Memory.keyword == keyword).delete()
        db.commit(); return rows > 0
    finally:
        db.close()

def list_memories(user_id):
    db = SessionLocal()
    try:
        return db.query(Memory).filter(Memory.user_id == str(user_id)).all()
    finally:
        db.close()

# ── Tracker CRUD ──────────────────────────────────────────────────────────────

def add_tracker(user_id, category, name, expire_date=None, is_recurring=0,
                recurring_month=None, recurring_day=None, cycle=None,
                amount=None, remind_days=7, remind_time="08:00",
                stock_total=None, stock_daily=None, notes=None):
    db = SessionLocal()
    try:
        t = Tracker(
            user_id=str(user_id), category=category, name=name,
            expire_date=expire_date, is_recurring=is_recurring,
            recurring_month=recurring_month, recurring_day=recurring_day,
            cycle=cycle, amount=amount, remind_days=remind_days, remind_time=remind_time,
            stock_total=stock_total, stock_daily=stock_daily, notes=notes,
        )
        db.add(t); db.commit(); db.refresh(t)
        return t.id
    except Exception as e:
        db.rollback(); logger.error(f"add_tracker: {e}"); return None
    finally:
        db.close()

def get_trackers(user_id, category=None):
    db = SessionLocal()
    try:
        q = db.query(Tracker).filter(Tracker.user_id == str(user_id))
        if category:
            q = q.filter(Tracker.category == category)
        return q.order_by(Tracker.category, Tracker.name).all()
    finally:
        db.close()

def get_tracker_by_id(user_id, tracker_id):
    db = SessionLocal()
    try:
        return db.query(Tracker).filter(
            Tracker.user_id == str(user_id),
            Tracker.id == tracker_id,
        ).first()
    finally:
        db.close()

def update_tracker(user_id, tracker_id, **fields):
    allowed = {
        "name", "amount", "remind_days", "remind_time", "expire_date",
        "recurring_month", "recurring_day", "stock_total", "stock_daily",
        "cycle", "notes", "last_reminded_date",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    db = SessionLocal()
    try:
        rows = db.query(Tracker).filter(
            Tracker.user_id == str(user_id),
            Tracker.id == tracker_id,
        ).update(updates, synchronize_session=False)
        db.commit()
        return rows > 0
    except Exception as e:
        db.rollback(); logger.error(f"update_tracker: {e}"); return False
    finally:
        db.close()

def delete_tracker_by_name(user_id, name):
    db = SessionLocal()
    try:
        rows = db.query(Tracker).filter(
            Tracker.user_id == str(user_id),
            Tracker.name.ilike(f"%{name}%"),
        ).delete(synchronize_session=False)
        db.commit(); return rows > 0
    except Exception as e:
        db.rollback(); logger.error(f"delete_tracker: {e}"); return False
    finally:
        db.close()

def get_all_trackers():
    """供 scheduler 每日掃描用"""
    db = SessionLocal()
    try:
        return db.query(Tracker).all()
    finally:
        db.close()

def mark_tracker_reminded(tracker_id, reminded_date):
    db = SessionLocal()
    try:
        rows = db.query(Tracker).filter(Tracker.id == tracker_id).update(
            {"last_reminded_date": reminded_date},
            synchronize_session=False,
        )
        db.commit()
        return rows > 0
    except Exception as e:
        db.rollback(); logger.error(f"mark_tracker_reminded: {e}"); return False
    finally:
        db.close()

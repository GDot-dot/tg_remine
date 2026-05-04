# db.py
import os, logging
from datetime import datetime
from sqlalchemy import (create_engine, Column, Integer, String, Float, DateTime, Text, UniqueConstraint)
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

def init_db():
    Base.metadata.create_all(bind=engine)
    logger.info("✅ DB tables created/verified.")

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

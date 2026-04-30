# db.py - SQLAlchemy models + CRUD (Neon PostgreSQL)

import os
import logging
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=5,
    max_overflow=10,
)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ── Models ──────────────────────────────────────────────────────────────────

class Event(Base):
    """提醒事件（一次性 & 週期性）"""
    __tablename__ = "events"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    creator_user_id    = Column(String(50), nullable=False)   # TG user id (int→str)
    target_id          = Column(String(50), nullable=False)   # TG chat_id
    target_type        = Column(String(20), default="private") # private / group / supergroup
    target_display_name = Column(String(100), default="")
    event_content      = Column(Text, nullable=False)
    event_datetime     = Column(DateTime(timezone=True), nullable=True)
    reminder_time      = Column(DateTime(timezone=True), nullable=True)
    reminder_sent      = Column(Integer, default=0)
    is_recurring       = Column(Integer, default=0)
    recurrence_rule    = Column(String(100), nullable=True)   # "mon,wed|09:00"
    priority_level     = Column(Integer, default=0)           # 0=普通 1=綠 2=黃 3=紅
    remaining_repeats  = Column(Integer, default=0)


class Location(Base):
    """地點記憶"""
    __tablename__ = "locations"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    user_id   = Column(String(50), nullable=False)
    name      = Column(String(100), nullable=False)
    latitude  = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    address   = Column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_user_location"),)


class Memory(Base):
    """記憶庫 Key-Value"""
    __tablename__ = "memories"

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(50), nullable=False)
    keyword = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "keyword", name="uq_user_memory"),)


class CreditCard(Base):
    """信用卡記錄"""
    __tablename__ = "credit_cards"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    user_id   = Column(String(50), nullable=False)
    card_name = Column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "card_name", name="uq_user_card"),)


# ── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    Base.metadata.create_all(bind=engine)
    logger.info("✅ DB tables created/verified.")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Event CRUD ───────────────────────────────────────────────────────────────

def add_event(creator_user_id, target_id, target_type, display_name,
              content, event_datetime, is_recurring=0,
              recurrence_rule=None, priority_level=0, remaining_repeats=0) -> int | None:
    db = SessionLocal()
    try:
        ev = Event(
            creator_user_id=str(creator_user_id),
            target_id=str(target_id),
            target_type=target_type,
            target_display_name=display_name,
            event_content=content,
            event_datetime=event_datetime,
            reminder_time=event_datetime,
            reminder_sent=0,
            is_recurring=is_recurring,
            recurrence_rule=recurrence_rule,
            priority_level=priority_level,
            remaining_repeats=remaining_repeats,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        return ev.id
    except Exception as e:
        db.rollback()
        logger.error(f"add_event error: {e}")
        return None
    finally:
        db.close()


def get_event(event_id: int) -> Event | None:
    db = SessionLocal()
    try:
        return db.query(Event).filter(Event.id == event_id).first()
    finally:
        db.close()


def get_user_events(user_id: str, include_recurring=True) -> list[Event]:
    db = SessionLocal()
    try:
        q = db.query(Event).filter(Event.creator_user_id == str(user_id))
        if not include_recurring:
            q = q.filter(Event.is_recurring == 0)
        return q.order_by(Event.reminder_time).all()
    finally:
        db.close()


def mark_reminder_sent(event_id: int):
    db = SessionLocal()
    try:
        db.query(Event).filter(Event.id == event_id).update({"reminder_sent": 1})
        db.commit()
    finally:
        db.close()


def update_reminder_time(event_id: int, new_time):
    db = SessionLocal()
    try:
        db.query(Event).filter(Event.id == event_id).update({"reminder_time": new_time})
        db.commit()
    finally:
        db.close()


def update_event_content(event_id: int, new_content: str) -> bool:
    db = SessionLocal()
    try:
        rows = db.query(Event).filter(Event.id == event_id).update({"event_content": new_content})
        db.commit()
        return rows > 0
    finally:
        db.close()


def delete_event_by_id(event_id: int, user_id: str) -> bool:
    db = SessionLocal()
    try:
        rows = db.query(Event).filter(
            Event.id == event_id,
            Event.creator_user_id == str(user_id)
        ).delete()
        db.commit()
        return rows > 0
    finally:
        db.close()


def decrease_remaining_repeats(event_id: int):
    db = SessionLocal()
    try:
        ev = db.query(Event).filter(Event.id == event_id).first()
        if ev and ev.remaining_repeats > 0:
            ev.remaining_repeats -= 1
            db.commit()
    finally:
        db.close()


# ── Location CRUD ─────────────────────────────────────────────────────────────

def add_location(user_id, name, lat, lng, address="") -> bool:
    db = SessionLocal()
    try:
        loc = Location(user_id=str(user_id), name=name,
                       latitude=lat, longitude=lng, address=address)
        db.add(loc)
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


def get_locations(user_id) -> list[Location]:
    db = SessionLocal()
    try:
        return db.query(Location).filter(Location.user_id == str(user_id)).all()
    finally:
        db.close()


def get_location_by_name(user_id, name) -> Location | None:
    db = SessionLocal()
    try:
        return db.query(Location).filter(
            Location.user_id == str(user_id),
            Location.name.ilike(f"%{name}%")
        ).first()
    finally:
        db.close()


def delete_location(user_id, name) -> bool:
    db = SessionLocal()
    try:
        rows = db.query(Location).filter(
            Location.user_id == str(user_id),
            Location.name == name
        ).delete()
        db.commit()
        return rows > 0
    finally:
        db.close()


# ── Memory CRUD ───────────────────────────────────────────────────────────────

def save_memory(user_id, keyword, content) -> bool:
    db = SessionLocal()
    try:
        existing = db.query(Memory).filter(
            Memory.user_id == str(user_id),
            Memory.keyword == keyword
        ).first()
        if existing:
            existing.content = content
        else:
            db.add(Memory(user_id=str(user_id), keyword=keyword, content=content))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"save_memory error: {e}")
        return False
    finally:
        db.close()


def query_memory(user_id, keyword) -> list[Memory]:
    db = SessionLocal()
    try:
        return db.query(Memory).filter(
            Memory.user_id == str(user_id),
            Memory.keyword.ilike(f"%{keyword}%")
        ).all()
    finally:
        db.close()


def forget_memory(user_id, keyword) -> bool:
    db = SessionLocal()
    try:
        rows = db.query(Memory).filter(
            Memory.user_id == str(user_id),
            Memory.keyword == keyword
        ).delete()
        db.commit()
        return rows > 0
    finally:
        db.close()


def list_memories(user_id) -> list[Memory]:
    db = SessionLocal()
    try:
        return db.query(Memory).filter(Memory.user_id == str(user_id)).all()
    finally:
        db.close()


# ── Credit Card CRUD ──────────────────────────────────────────────────────────

def add_user_card(user_id, card_name) -> bool:
    db = SessionLocal()
    try:
        db.add(CreditCard(user_id=str(user_id), card_name=card_name))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


def delete_user_card(user_id, card_name) -> bool:
    db = SessionLocal()
    try:
        rows = db.query(CreditCard).filter(
            CreditCard.user_id == str(user_id),
            CreditCard.card_name == card_name
        ).delete()
        db.commit()
        return rows > 0
    finally:
        db.close()


def get_user_cards(user_id) -> list[str]:
    db = SessionLocal()
    try:
        cards = db.query(CreditCard).filter(CreditCard.user_id == str(user_id)).all()
        return [c.card_name for c in cards]
    finally:
        db.close()

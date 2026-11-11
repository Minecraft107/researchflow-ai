"""SQLAlchemy models + session + a tiny key/value cache.

Portable to PostgreSQL: set DATABASE_URL=postgresql+psycopg://... (only generic column types are used).
ponytail: sync engine called from async code - fine for SQLite and a handful of concurrent
runs; switch to create_async_engine + asyncpg if write volume ever blocks the event loop.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic: Mapped[str] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|running|completed|failed
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    failed_sources: Mapped[list] = mapped_column(JSON, default=list)
    critiques: Mapped[list] = mapped_column(JSON, default=list)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    research_id: Mapped[str] = mapped_column(String(36), index=True)
    agent: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CacheEntry(Base):
    __tablename__ = "cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_url = get_settings().database_url
engine = create_engine(_url, connect_args={"check_same_thread": False} if _url.startswith("sqlite") else {})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def _cache_key(namespace: str, raw: str) -> str:
    return hashlib.sha256(f"{namespace}:{raw}".encode()).hexdigest()


def cache_get(namespace: str, raw_key: str, ttl: int) -> dict | None:
    """`ttl` in seconds; 0 disables the cache."""
    if ttl <= 0:
        return None
    with SessionLocal() as db:
        entry = db.get(CacheEntry, _cache_key(namespace, raw_key))
        if entry is None:
            return None
        created = entry.created_at if entry.created_at.tzinfo else entry.created_at.replace(tzinfo=timezone.utc)
        return entry.value if utcnow() - created < timedelta(seconds=ttl) else None


def cache_set(namespace: str, raw_key: str, value: dict, ttl: int) -> None:
    if ttl <= 0:
        return
    with SessionLocal() as db:
        db.merge(CacheEntry(key=_cache_key(namespace, raw_key), value=value, created_at=utcnow()))
        db.commit()


def events_after(research_id: str, after_id: int) -> list[AgentEvent]:
    with SessionLocal() as db:
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.research_id == research_id, AgentEvent.id > after_id)
            .order_by(AgentEvent.id)
        )
        return list(db.scalars(stmt))

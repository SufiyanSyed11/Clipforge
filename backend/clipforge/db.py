"""Database models and session handling.

SQLite by default (one file in the data dir, no service to install). Set DATABASE_URL to a
Postgres URL for the cloud deployment; nothing else changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from clipforge.settings import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    qa: Mapped[list[Any] | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(16))  # "url" | "upload"
    source: Mapped[str] = mapped_column(Text)  # URL or stored upload path
    rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    rights_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
    clip_count: Mapped[int] = mapped_column(Integer, default=8)
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_info: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    transcript_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    speakers: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    host_speaker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    host_suggestion: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    host_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Permitted YouTube B-roll for this project: [{url, rights_confirmed, license}]
    broll_sources: Mapped[list[Any] | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    plan: Mapped[dict[str, Any]] = mapped_column()
    qa: Mapped[list[Any] | None] = mapped_column(nullable=True)
    # picked -> building -> ready (rendered, gate passed) | needs_review (gate failed) | failed
    status: Mapped[str] = mapped_column(String(32), default="picked")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    # Owner edits: trims, caption word fixes, B-roll swaps/approvals, host-insert toggle.
    edits: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    render_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    copy_pack: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    export_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Scheduled uploads: [{platform, video_id, publish_at, privacy}]
    posts: Mapped[list[Any] | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AppSetting(Base):
    """Owner-editable settings (style preset, brand tag, banned content, ...)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column()


@lru_cache
def get_engine() -> Engine:
    url = get_settings().resolved_database_url
    is_sqlite = url.startswith("sqlite")
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30} if is_sqlite else {},
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            # WAL lets the API read while the worker writes.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Tiny forward-only migration: add columns that newer versions define (all nullable or
    defaulted), so an existing local database keeps working after an upgrade."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    v = col.default.arg
                    default = f" DEFAULT {int(v) if isinstance(v, bool) else repr(v)}"
                conn.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}{default}')
                )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_caches() -> None:
    """Drop cached engine/session so tests can point at a fresh database."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    _session_factory.cache_clear()

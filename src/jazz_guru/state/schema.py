from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from jazz_guru.config import get_settings


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class TurnRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class EventType(enum.StrEnum):
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    MEMORY_WRITE = "memory_write"
    MEMORY_READ = "memory_read"
    REFLEXION = "reflexion"
    ERROR = "error"
    # Tier-2 tool improvement loop (plan §B.8)
    TOOL_IMPROVE_PROPOSED = "tool_improve_proposed"
    TOOL_IMPROVE_PASSED = "tool_improve_passed"
    TOOL_IMPROVE_FAILED = "tool_improve_failed"
    TOOL_ROLLBACK = "tool_rollback"
    # Auto-distillation lifecycle (close / idle-sweep / new-session predecessor scan)
    DISTILLATION_QUEUED = "distillation_queued"
    DISTILLATION_INLINE = "distillation_inline"
    DISTILLATION_SKIPPED = "distillation_skipped"


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    goal_profile: Mapped[str] = mapped_column(String(64), default="default")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    turns: Mapped[list[Turn]] = relationship(back_populates="session", cascade="all, delete-orphan")
    events: Mapped[list[Event]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[Session] = relationship(back_populates="turns")

    __table_args__ = (Index("ix_turns_session_idx", "session_id", "idx"),)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("turns.id", ondelete="SET NULL"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    session: Mapped[Session] = relationship(back_populates="events")


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("turns.id", ondelete="SET NULL"), nullable=True)
    path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    score: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PlaybookEntry(Base):
    __tablename__ = "playbook_entries"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GeneratedTool(Base):
    __tablename__ = "generated_tools"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16), default="global", index=True)
    owner_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    deprecated: Mapped[bool] = mapped_column(default=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GeneratedToolVersion(Base):
    """Historical snapshot of a generated_tools row.

    Written by store.upsert BEFORE mutating the live row, so rollback is a
    single-row read. ``version`` is the value generated_tools.version held at
    snapshot time; ``superseded_by`` is the version number that replaced it
    (NULL while this row is still the live one, though normally we snapshot
    only when superseding).
    """

    __tablename__ = "generated_tool_versions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    tool_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generated_tools.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(32), default="manual")
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tool_id", "version", name="uq_generated_tool_versions_tool_version"),
    )


class GeneratedToolTest(Base):
    """One test case attached to a Tier-2 tool.

    ``spec`` is the parsed YAML/JSON case+predicate+(optional)rubric described
    in docs/plans/tier2-tool-tests-and-improvement.md §A.2. Cases run against
    the live source via the same subprocess sandbox as the tool itself.
    """

    __tablename__ = "generated_tool_tests"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    tool_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generated_tools.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(96))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(32), default="agent_authored")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tool_id", "name", name="uq_generated_tool_tests_tool_name"),
    )


class GeneratedToolTestRun(Base):
    """One execution of one test case against one version of a tool.

    Append-only log; never updated in place. ``tool_version`` records which
    version was under test so historical runs remain interpretable after
    later upserts/rollbacks.
    """

    __tablename__ = "generated_tool_test_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    tool_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generated_tools.id", ondelete="CASCADE"), index=True
    )
    tool_version: Mapped[int] = mapped_column(Integer)
    test_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generated_tool_tests.id", ondelete="CASCADE"), index=True
    )
    passed: Mapped[bool] = mapped_column(Boolean)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ms: Mapped[int] = mapped_column(Integer, default=0)
    judge_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_generated_tool_test_runs_tool_ran_at", "tool_id", "ran_at"),
    )


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    rubric: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

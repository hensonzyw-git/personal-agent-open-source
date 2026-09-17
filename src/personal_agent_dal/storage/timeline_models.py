"""Durable, encrypted Timeline intake and complete query snapshots."""
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from personal_agent_core.sqlite import UtcTimestamp
from personal_agent_core.sqlite import EncryptedEnvelope
from personal_agent_dal.storage.models import Base


class DevelopmentRequest(Base):
    __tablename__ = 'development_requests'
    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_message_ref: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    __table_args__ = (
        CheckConstraint('version >= 1', name='request_version'),
        CheckConstraint("status IN ('clarifying','ready','linked','cancelled')", name='request_status'),
    )


class DevelopmentRequestRevision(Base):
    __tablename__ = 'development_request_revisions'
    revision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey('development_requests.request_id', ondelete='RESTRICT'), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_body: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)
    __table_args__ = (UniqueConstraint('request_id', 'revision', name='request_revision'),)


class DevelopmentCommand(Base):
    __tablename__ = 'development_commands'
    command_id: Mapped[str] = mapped_column(Text, primary_key=True)
    body_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_result: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)


class DevelopmentEventStream(Base):
    __tablename__ = 'development_event_streams'
    stream_id: Mapped[str] = mapped_column(Text, primary_key=True)
    singleton: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tail_digest: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (CheckConstraint('singleton = 1 AND next_seq >= 1', name='stream_singleton'),)


class DevelopmentEvent(Base):
    __tablename__ = 'development_events'
    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    stream_id: Mapped[str] = mapped_column(ForeignKey('development_event_streams.stream_id'), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(ForeignKey('development_requests.request_id'), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body_digest: Mapped[str] = mapped_column(Text, nullable=False)
    prev_digest: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    sealed_body: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)
    __table_args__ = (UniqueConstraint('stream_id', 'seq', name='event_sequence'), CheckConstraint('seq >= 1', name='sequence_positive'))


class DevelopmentQuerySnapshot(Base):
    __tablename__ = 'development_query_snapshots'
    snapshot_id: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    view: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    sealed_body: Mapped[dict[str, Any]] = mapped_column(EncryptedEnvelope, nullable=False)


class DevelopmentEventConsumer(Base):
    __tablename__ = 'development_event_consumers'
    stream_id: Mapped[str] = mapped_column(ForeignKey('development_event_streams.stream_id'), primary_key=True)
    consumer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ack_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tail_digest: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (CheckConstraint("consumer_id = 'pa-timeline' AND ack_seq >= 0 AND version >= 1", name='consumer_cursor'),)


class DevelopmentWorkflow(Base):
    __tablename__ = 'development_workflows'
    workflow_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey('development_requests.request_id', ondelete='RESTRICT'), nullable=False, unique=True)
    feature_id: Mapped[str | None] = mapped_column(ForeignKey('features.feature_id', ondelete='RESTRICT'), unique=True)
    contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("contract_version = 'dal.timeline-workflow/1.0' AND version >= 1", name='workflow_contract'),
        CheckConstraint("status IN ('active','blocked','paused','completed','cancelled')", name='workflow_status'),
        CheckConstraint("(phase = 'accepted' AND status = 'completed') OR (phase != 'accepted' AND status != 'completed')", name='workflow_completion'),
    )

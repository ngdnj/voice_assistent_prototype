"""PostgreSQL schema for records and creation operations; migrations must match it."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import TSVECTOR

# Stable constraint names so Alembic migrations and error handling can refer to them.
metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
    }
)

# One table for tasks, meetings and reminders: a period or a text search covers all types in one query.
# scheduled_at is the record's moment: meeting start, reminder time or exact task deadline (due_at).
events = Table(
    "events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("owner_id", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=True),
    Column("due_date", Date, nullable=True),
    Column("scheduled_at", DateTime(timezone=True), nullable=True),
    Column("ends_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    # Lexical full-text search (Russian stemming), not semantic search.
    Column(
        "search",
        TSVECTOR,
        Computed("to_tsvector('russian', name || ' ' || description)", persisted=True),
        nullable=False,
    ),
    CheckConstraint("event_type IN ('task', 'meeting', 'reminder')", name="event_type"),
    CheckConstraint("btrim(owner_id) <> ''", name="owner_id_not_blank"),
    CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
    CheckConstraint("char_length(name) <= 200", name="name_length"),
    CheckConstraint("char_length(description) <= 2000", name="description_length"),
    CheckConstraint(
        "(event_type = 'task') = (status IS NOT NULL)"
        " AND (status IS NULL OR status IN ('todo', 'in_progress', 'done'))",
        name="status",
    ),
    CheckConstraint("due_date IS NULL OR event_type = 'task'", name="due_date_task_only"),
    CheckConstraint("due_date IS NULL OR scheduled_at IS NULL", name="due_date_xor_scheduled_at"),
    CheckConstraint("event_type = 'task' OR scheduled_at IS NOT NULL", name="scheduled_at_required"),
    CheckConstraint("ends_at IS NULL OR (event_type = 'meeting' AND ends_at > scheduled_at)", name="ends_at"),
    CheckConstraint("event_type <> 'reminder' OR scheduled_at >= created_at", name="reminder_not_in_past"),
)

Index("ix_events_owner_scheduled_at", events.c.owner_id, events.c.scheduled_at, events.c.id)
Index("ix_events_owner_due_date", events.c.owner_id, events.c.due_date, events.c.id)
Index(
    "ix_events_owner_type_created_at",
    events.c.owner_id,
    events.c.event_type,
    events.c.created_at,
    events.c.id,
)
Index("ix_events_search", events.c.search, postgresql_using="gin")

# Result of a logical creation: one (owner, operation_id) creates at most one record of any type.
operations = Table(
    "operations",
    metadata,
    Column("owner_id", Text, nullable=False),
    Column("operation_type", Text, nullable=False),
    Column("operation_id", Text, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("event_id", Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("owner_id", "operation_id"),
    CheckConstraint(
        "operation_type IN ('create_task', 'create_meeting', 'create_reminder', "
        "'reschedule_meeting', 'update_event', 'delete_event')",
        name="operation_type",
    ),
    CheckConstraint("char_length(operation_id) BETWEEN 1 AND 200", name="operation_id_length"),
    CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_sha256"),
)

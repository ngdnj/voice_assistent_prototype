"""Create events and operations tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-23

Written by hand to match app/storage/postgres/tables.py; tests/storage_pg/test_migrations.py
compares the migrated schema with the metadata.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('russian', name || ' ' || description)", persisted=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.CheckConstraint(
            "event_type IN ('task', 'meeting', 'reminder')", name=op.f("ck_events_event_type")
        ),
        sa.CheckConstraint("btrim(owner_id) <> ''", name=op.f("ck_events_owner_id_not_blank")),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_events_name_not_blank")),
        sa.CheckConstraint("char_length(name) <= 200", name=op.f("ck_events_name_length")),
        sa.CheckConstraint("char_length(description) <= 2000", name=op.f("ck_events_description_length")),
        sa.CheckConstraint(
            "(event_type = 'task') = (status IS NOT NULL)"
            " AND (status IS NULL OR status IN ('todo', 'in_progress', 'done'))",
            name=op.f("ck_events_status"),
        ),
        sa.CheckConstraint(
            "due_date IS NULL OR event_type = 'task'", name=op.f("ck_events_due_date_task_only")
        ),
        sa.CheckConstraint(
            "due_date IS NULL OR scheduled_at IS NULL", name=op.f("ck_events_due_date_xor_scheduled_at")
        ),
        sa.CheckConstraint(
            "event_type = 'task' OR scheduled_at IS NOT NULL", name=op.f("ck_events_scheduled_at_required")
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR (event_type = 'meeting' AND ends_at > scheduled_at)",
            name=op.f("ck_events_ends_at"),
        ),
        sa.CheckConstraint(
            "event_type <> 'reminder' OR scheduled_at >= created_at",
            name=op.f("ck_events_reminder_not_in_past"),
        ),
    )
    op.create_index("ix_events_owner_scheduled_at", "events", ["owner_id", "scheduled_at", "id"])
    op.create_index("ix_events_owner_due_date", "events", ["owner_id", "due_date", "id"])
    op.create_index(
        "ix_events_owner_type_created_at", "events", ["owner_id", "event_type", "created_at", "id"]
    )
    op.create_index("ix_events_search", "events", ["search"], postgresql_using="gin")

    op.create_table(
        "operations",
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("operation_type", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "operation_id", name=op.f("pk_operations")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_operations_event_id_events"), ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "operation_type IN ('create_task', 'create_meeting', 'create_reminder')",
            name=op.f("ck_operations_operation_type"),
        ),
        sa.CheckConstraint(
            "char_length(operation_id) BETWEEN 1 AND 200", name=op.f("ck_operations_operation_id_length")
        ),
        sa.CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name=op.f("ck_operations_fingerprint_sha256")),
    )


def downgrade() -> None:
    op.drop_table("operations")
    op.drop_index("ix_events_search", table_name="events")
    op.drop_index("ix_events_owner_type_created_at", table_name="events")
    op.drop_index("ix_events_owner_due_date", table_name="events")
    op.drop_index("ix_events_owner_scheduled_at", table_name="events")
    op.drop_table("events")

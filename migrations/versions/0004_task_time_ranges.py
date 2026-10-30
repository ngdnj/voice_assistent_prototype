"""Allow explicit task time ranges, preserving existing records.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_events_ends_at"), "events", type_="check")
    op.create_check_constraint(
        op.f("ck_events_ends_at"),
        "events",
        "ends_at IS NULL OR (event_type IN ('task', 'meeting') "
        "AND scheduled_at IS NOT NULL AND ends_at > scheduled_at)",
    )


def downgrade() -> None:
    used = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM events WHERE event_type='task' AND ends_at IS NOT NULL)")
    )
    if used:
        raise RuntimeError("Cannot downgrade while task time ranges exist")
    op.drop_constraint(op.f("ck_events_ends_at"), "events", type_="check")
    op.create_check_constraint(
        op.f("ck_events_ends_at"),
        "events",
        "ends_at IS NULL OR (event_type = 'meeting' AND ends_at > scheduled_at)",
    )
